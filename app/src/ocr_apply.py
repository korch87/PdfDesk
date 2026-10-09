"""Apply searchable OCR text overlays to a live :class:`PdfSession`.

OCR pages are authored in rendered display coordinates. This module maps each
overlay back through the PDF page's CropBox and /Rotate, merges invisible text
over the existing page content, and records the whole batch as one undo step.
"""
from __future__ import annotations

import dataclasses
import io
import json
import math
from typing import Any, Mapping

from pypdf import PdfReader, PdfWriter
from pypdf._page import Transformation
from pypdf.generic import (
    ArrayObject, BooleanObject, ContentStream, DecodedStreamObject,
    DictionaryObject, FloatObject, NameObject, NumberObject,
)

from .pdf_backend import _display_to_user_point


class OCRApplyError(ValueError):
    """An OCR result cannot safely be applied to the selected page."""


def _overlay_from_result(value: Any) -> tuple[bytes, tuple[int, int], tuple[float, float] | None]:
    """Accept OCRResult and the dialog's page wrapper without mutating either."""
    wrapper = value
    result = getattr(value, "result", value)
    overlay = getattr(result, "overlay_pdf", None)
    image_size = getattr(result, "image_size", None)
    display_size = getattr(wrapper, "display_size_points", None)
    if not isinstance(overlay, (bytes, bytearray)) or not overlay.startswith(b"%PDF"):
        raise OCRApplyError("Результат OCR не содержит корректный PDF-слой.")
    try:
        width_px, height_px = (int(item) for item in image_size)
    except (TypeError, ValueError):
        raise OCRApplyError("Результат OCR не содержит размер изображения.") from None
    if width_px <= 0 or height_px <= 0:
        raise OCRApplyError("Размер изображения OCR должен быть положительным.")
    words = getattr(result, "words", None)
    if not words:
        raise OCRApplyError("Результат OCR не содержит распознанных слов.")
    normalized_display = None
    if display_size is not None:
        try:
            dw, dh = (float(item) for item in display_size)
        except (TypeError, ValueError):
            raise OCRApplyError("Некорректный размер страницы в результате OCR.") from None
        if not all(math.isfinite(item) and item > 0 for item in (dw, dh)):
            raise OCRApplyError("Некорректный размер страницы в результате OCR.")
        normalized_display = (dw, dh)
    return bytes(overlay), (width_px, height_px), normalized_display


def _ocr_metadata_json(value: Any, image_size: tuple[int, int]) -> str:
    """Serialize bounded word geometry for correction after save/reopen."""
    from .ocr_correction import encode_ocr_metadata

    result = getattr(value, "result", value)
    return encode_ocr_metadata(result, image_size)


def _read_overlay(data: bytes) -> Any:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise OCRApplyError("PDF-слой OCR не должен быть защищён паролем.")
        if len(reader.pages) != 1:
            raise OCRApplyError("PDF-слой OCR должен содержать ровно одну страницу.")
        page = reader.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        text = page.extract_text() or ""
        if (not all(math.isfinite(item) and item > 0 for item in (width, height))
                or not text.strip()):
            raise OCRApplyError("PDF-слой OCR пуст или имеет неверный размер.")
        return reader, page, width, height
    except OCRApplyError:
        raise
    except Exception as exc:  # noqa: BLE001 - report malformed/unreadable overlay
        raise OCRApplyError("Не удалось прочитать PDF-слой OCR: %s" % exc) from exc


def _transformation_for_display_overlay(geom: Any, overlay_width: float,
                                        overlay_height: float) -> Transformation:
    """Map overlay PDF user points to original page PDF user coordinates."""
    display_width = float(geom.display_width)
    display_height = float(geom.display_height)

    def mapped(x: float, y: float) -> tuple[float, float]:
        display_x = x * display_width / overlay_width
        display_y = (overlay_height - y) * display_height / overlay_height
        return _display_to_user_point(geom, display_x, display_y)

    origin = mapped(0.0, 0.0)
    x_axis = mapped(1.0, 0.0)
    y_axis = mapped(0.0, 1.0)
    return Transformation((
        x_axis[0] - origin[0], x_axis[1] - origin[1],
        y_axis[0] - origin[0], y_axis[1] - origin[1],
        origin[0], origin[1],
    ))


def _marked_xobjects(page: Any) -> dict[NameObject, Any]:
    resources = page.get("/Resources")
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    if not isinstance(resources, DictionaryObject):
        return {}
    xobjects = resources.get("/XObject")
    if hasattr(xobjects, "get_object"):
        xobjects = xobjects.get_object()
    if not isinstance(xobjects, DictionaryObject):
        return {}
    found = {}
    for name, reference in xobjects.items():
        obj = reference.get_object() if hasattr(reference, "get_object") else reference
        if (str(obj.get("/Subtype", "")) == "/Form"
                and bool(obj.get("/PdfDeskOCROverlay", False))):
            found[name] = obj
    return found


def _remove_owned_layer(page: Any, writer: PdfWriter) -> bool:
    """Remove one or more explicitly marked PdfDesk forms, leaving other ops."""
    marked = _marked_xobjects(page)
    contents = page.get_contents()
    if not marked:
        if contents is not None:
            for operands, operator in contents.operations:
                if (operator == b"BMC" and operands
                        and str(operands[0]) == "/PdfDeskOCRLayer"):
                    raise OCRApplyError(
                        "Слой OCR изменён вне PdfDesk; безопасно заменить его нельзя."
                    )
        return False
    if contents is None:
        raise OCRApplyError("Не удалось найти содержимое ранее добавленного слоя OCR.")

    names = set(marked)
    operations = list(contents.operations)
    removed = set()
    keep: list[tuple[Any, bytes]] = []
    cursor = 0
    while cursor < len(operations):
        operands, operator = operations[cursor]
        if (operator == b"BMC" and operands
                and str(operands[0]) == "/PdfDeskOCRLayer"):
            end = cursor + 1
            depth = 1
            invoked = set()
            while end < len(operations) and depth:
                inner_operands, inner_operator = operations[end]
                if inner_operator == b"BMC":
                    depth += 1
                elif inner_operator == b"EMC":
                    depth -= 1
                elif inner_operator == b"Do" and inner_operands:
                    name = inner_operands[0]
                    if name in names:
                        invoked.add(name)
                end += 1
            if depth:
                raise OCRApplyError("Повреждена разметка существующего слоя OCR.")
            if invoked:
                block = operations[cursor + 1:end - 1]
                # PDFium's indexed-content rewrite may normalize an existing
                # marked invocation by inserting its initial black stroke and
                # fill colors inside the surrounding q/Q pair. Those are
                # harmless defaults for our invisible Form XObject. Accept
                # only that exact normalization (in either order around cm),
                # while refusing every drawing/text operator and every other
                # resource invocation inside the owned marker.
                operators = [operator for _args, operator in block]
                if (not operators or operators[0] != b"q"
                        or operators[-1] != b"Q"
                        or operators.count(b"cm") != 1
                        or operators.count(b"Do") != 1
                        or any(operator not in {b"q", b"Q", b"cm", b"Do", b"rg", b"RG"}
                               for operator in operators)):
                    raise OCRApplyError(
                        "Содержимое слоя OCR изменено; безопасно заменить его нельзя."
                    )
                matrix_ops = [i for i, operator in enumerate(operators) if operator == b"cm"]
                do_ops = [i for i, operator in enumerate(operators) if operator == b"Do"]
                color_ops_valid = all(
                    len(args) == 3 and all(float(value) == 0.0 for value in args)
                    for args, operator in block if operator in {b"rg", b"RG"}
                )
                matrix = block[matrix_ops[0]][0]
                if (operators.count(b"q") != 1 or operators.count(b"Q") != 1
                        or matrix_ops[0] >= do_ops[0]
                        or len(matrix) != 6
                        or not all(math.isfinite(float(value)) for value in matrix)
                        or block[do_ops[0]][0] != [next(iter(invoked))]
                        or len(invoked) != 1 or not color_ops_valid
                        or operators.count(b"rg") > 1 or operators.count(b"RG") > 1):
                    raise OCRApplyError(
                        "Содержимое слоя OCR изменено; безопасно заменить его нельзя."
                    )
                removed.update(invoked)
                cursor = end
                continue
        keep.append(operations[cursor])
        cursor += 1

    if removed != names:
        raise OCRApplyError(
            "Слой OCR был изменён после распознавания; безопасно заменить его нельзя."
        )
    content = ContentStream(None, writer)
    content.operations = keep
    page.replace_contents(content)

    resources_ref = page.get("/Resources")
    resources = resources_ref.get_object() if hasattr(resources_ref, "get_object") else resources_ref
    copied_resources = DictionaryObject()
    for key, value in resources.items():
        copied_resources[key] = value
    xobjects_ref = copied_resources.get("/XObject")
    xobjects = xobjects_ref.get_object() if hasattr(xobjects_ref, "get_object") else xobjects_ref
    copied_xobjects = DictionaryObject()
    for key, value in xobjects.items():
        if key not in names:
            copied_xobjects[key] = value
    copied_resources[NameObject("/XObject")] = copied_xobjects
    page[NameObject("/Resources")] = copied_resources
    return True


def _add_owned_layer(writer: PdfWriter, page: Any, overlay_page: Any,
                     overlay_width: float, overlay_height: float,
                     transform: Transformation, metadata_json: str) -> None:
    """Attach OCR as a marked Form XObject and an isolated content stream."""
    # Let pypdf import/rename overlay fonts and other resources into the output
    # writer. We then package that merged overlay page as a reusable Form.
    temp_index = len(writer.pages)
    temp_page = writer.add_blank_page(width=overlay_width, height=overlay_height)
    temp_page.merge_page(overlay_page, over=True, expand=False)
    merged_content = temp_page.get_contents()
    resources = temp_page.get("/Resources", DictionaryObject())
    form = DecodedStreamObject()
    form.set_data(merged_content.get_data() if merged_content is not None else b"")
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/FormType")] = NumberObject(1)
    form[NameObject("/BBox")] = ArrayObject([
        FloatObject(0), FloatObject(0), FloatObject(overlay_width), FloatObject(overlay_height)
    ])
    form[NameObject("/Resources")] = resources
    form[NameObject("/PdfDeskOCROverlay")] = BooleanObject(True)
    from pypdf.generic import TextStringObject
    form[NameObject("/PdfDeskOCRMetadata")] = TextStringObject(metadata_json)
    form_ref = writer._add_object(form)
    writer.remove_page(temp_index)

    resources_ref = page.get("/Resources")
    resources = resources_ref.get_object() if hasattr(resources_ref, "get_object") else resources_ref
    copied_resources = DictionaryObject()
    if isinstance(resources, DictionaryObject):
        for key, value in resources.items():
            copied_resources[key] = value
    old_xobjects_ref = copied_resources.get("/XObject", DictionaryObject())
    old_xobjects = (old_xobjects_ref.get_object() if hasattr(old_xobjects_ref, "get_object")
                    else old_xobjects_ref)
    xobjects = DictionaryObject()
    if isinstance(old_xobjects, DictionaryObject):
        for key, value in old_xobjects.items():
            xobjects[key] = value
    suffix = 1
    name = NameObject("/PdfDeskOCRLayer%d" % suffix)
    while name in xobjects:
        suffix += 1
        name = NameObject("/PdfDeskOCRLayer%d" % suffix)
    xobjects[name] = form_ref
    copied_resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = copied_resources

    ctm = transform.ctm
    stream = DecodedStreamObject()
    marker = b"/PdfDeskOCRLayer BMC\nq\n" + b" ".join(
        repr(float(value)).encode("ascii") for value in ctm
    ) + b" cm\n" + str(name).encode("ascii") + b" Do\nQ\nEMC\n"
    stream.set_data(marker)
    stream_ref = writer._add_object(stream)
    contents_ref = page.get("/Contents")
    contents = contents_ref.get_object() if hasattr(contents_ref, "get_object") else contents_ref
    if isinstance(contents, ArrayObject):
        merged_contents = ArrayObject(list(contents))
    elif contents is None:
        merged_contents = ArrayObject()
    else:
        merged_contents = ArrayObject([contents_ref])
    merged_contents.append(stream_ref)
    page[NameObject("/Contents")] = writer._add_object(merged_contents)


def apply_ocr_results(session: Any, results: Mapping[int, Any]) -> None:
    """Add OCR's invisible searchable text overlays as one undoable batch.

    ``results`` maps zero-based page indices to ``OCRResult`` instances. The
    OCR render grid can use any DPI; it is scaled to the current displayed page
    dimensions, then mapped through the source page's CropBox and rotation.
    The original page content streams, forms, annotations, boxes and rotation
    remain intact. The caller's source file is never written.
    """
    if session is None:
        raise TypeError("Нужна открытая PDF-сессия.")
    session._require_open()
    session._require_edit_permission()
    if not isinstance(results, Mapping) or not results:
        raise OCRApplyError("Нет результатов OCR для применения.")

    prepared: list[tuple[int, Any, Any, float, float, Any, str]] = []
    page_count = session.page_count
    for raw_index, value in results.items():
        if isinstance(raw_index, bool):
            raise OCRApplyError("Индекс страницы OCR должен быть целым числом.")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            raise OCRApplyError("Индекс страницы OCR должен быть целым числом.") from None
        if index != raw_index or not (0 <= index < page_count):
            raise OCRApplyError("Страница OCR не найдена: %s." % raw_index)
        overlay_data, image_size, expected_display = _overlay_from_result(value)
        metadata_json = _ocr_metadata_json(value, image_size)
        reader, overlay_page, overlay_width, overlay_height = _read_overlay(overlay_data)
        geom = session._geometry(index)
        if expected_display is not None:
            actual = (float(geom.display_width), float(geom.display_height))
            if any(abs(got - want) > max(0.75, want * 0.002)
                   for got, want in zip(actual, expected_display)):
                raise OCRApplyError(
                    "Размер страницы изменился после распознавания OCR; повторите распознавание."
                )
        image_ratio = image_size[0] / image_size[1]
        overlay_ratio = overlay_width / overlay_height
        page_ratio = float(geom.display_width) / float(geom.display_height)
        if abs(image_ratio / page_ratio - 1.0) > 0.015 or abs(overlay_ratio / page_ratio - 1.0) > 0.015:
            raise OCRApplyError(
                "Размер слоя OCR не соответствует отображаемой странице %d." % (index + 1)
            )
        prepared.append((index, reader, overlay_page, overlay_width,
                         overlay_height, _transformation_for_display_overlay(
                             geom, overlay_width, overlay_height), metadata_json))

    # Build and validate the entire changed PDF off-session. No history or
    # document state is touched until every overlay has merged successfully.
    before = session._make_snapshot()
    try:
        source_reader = PdfReader(io.BytesIO(before.data), strict=False)
        writer = PdfWriter(clone_from=source_reader)
        for index, reader, overlay_page, overlay_width, overlay_height, transform, metadata_json in prepared:
            target = writer.pages[index]
            _remove_owned_layer(target, writer)
            _add_owned_layer(writer, target, overlay_page, overlay_width,
                             overlay_height, transform, metadata_json)
        output = io.BytesIO()
        writer.write(output)
        changed = output.getvalue()
        verify = PdfReader(io.BytesIO(changed), strict=False)
        if len(verify.pages) != page_count:
            raise OCRApplyError("Слой OCR изменил число страниц документа.")
        for index in range(page_count):
            old_geom = session._geometry(index)
            check_geom_page = verify.pages[index]
            if int(float(check_geom_page.get("/Rotate", 0) or 0)) % 360 != old_geom.rotation:
                raise OCRApplyError("Слой OCR изменил поворот страницы.")
    except OCRApplyError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolate pypdf mutations
        raise OCRApplyError("Не удалось применить результаты OCR: %s" % exc) from exc

    session._push_undo()
    replacement = dataclasses.replace(before, data=changed, size=len(changed))
    session._restore(replacement)
    session._touch(doc=True)
