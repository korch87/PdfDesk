"""Direct editing of top-level PDF text and image page objects.

Coordinates are PDF points in the unrotated page coordinate system (origin at
the lower-left). This module edits native page objects; it never covers text
with a white rectangle or converts a page to a bitmap.
"""
from __future__ import annotations

import ctypes
import base64
from dataclasses import dataclass, replace as dataclass_replace
import hashlib
import io
import re
import math
from pathlib import Path
from typing import Any
import uuid

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdfium2._helpers.matrix import PdfMatrix


@dataclass(frozen=True)
class ContentBlock:
    id: str
    kind: str
    bbox: tuple[float, float, float, float]
    text: str = ""
    font_name: str = ""
    font_size: float = 0.0
    color: str = "#000000"
    editable: bool = True
    reason: str = ""
    object_ids: tuple[str, ...] = ()
    alignment: str = "left"
    bold: bool = False
    group_id: str = ""
    block_width: float = 0.0
    block_height: float = 0.0
    orientation: str = "page"
    rich_spans: tuple[dict[str, Any], ...] = ()
    line_spacing: float = 0.0
    underlines: tuple[dict[str, Any], ...] = ()


def _color(obj: Any) -> str:
    values = [ctypes.c_uint() for _ in range(4)]
    if not pdfium_c.FPDFPageObj_GetFillColor(obj, *(ctypes.byref(v) for v in values)):
        return "#000000"
    r, g, b, _a = (v.value for v in values)
    return f"#{r:02X}{g:02X}{b:02X}"


def _font_name(obj: Any) -> str:
    try:
        return obj.get_font().get_base_name()
    except Exception:
        return ""


def _matrix_size_scale(obj: Any) -> float:
    """Text matrix scale which PDF object bounds already include."""
    try:
        matrix = obj.get_matrix()
        return max(0.0001, math.hypot(matrix.c, matrix.d))
    except Exception:
        return 1.0


# Objects nested in Form XObjects must never be edited in place: the same form
# can be painted by several pages and their bounds are reported in the form's
# own coordinate system. They are listed read-only with this explanation.
NESTED_OBJECT_REASON = (
    "Объект входит в Form XObject и может отображаться на других страницах или "
    "несколько раз на этой. Прямая правка изменила бы все такие места, поэтому "
    "PdfDesk не редактирует его: измените исходный документ или разгруппируйте "
    "форму в программе, создавшей PDF."
)
INVISIBLE_TEXT_REASON = (
    "Невидимый текстовый слой (режим отображения 3 или 7) — например распознанный "
    "OCR-текст поверх скана. Правка не изменит видимое изображение страницы: "
    "измените сам скан или исправьте текст в режиме OCR."
)

_MATRIX_DEPTH_LIMIT = 64
# Only modes 3 (neither fill nor stroke) and 7 (add to path for clipping) paint
# nothing. Modes 4/5/6 fill and/or stroke the glyphs before adding them to the
# clipping path, so their text is visible and stays editable.
_INVISIBLE_RENDER_MODES = (
    pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE,
    pdfium_c.FPDF_TEXTRENDERMODE_CLIP,
)


def _matrix_values(obj: Any) -> tuple[float, float, float, float, float, float]:
    matrix = obj.get_matrix()
    return (float(matrix.a), float(matrix.b), float(matrix.c),
            float(matrix.d), float(matrix.e), float(matrix.f))


def _concat_matrices(outer: tuple[float, ...], inner: tuple[float, ...]
                     ) -> tuple[float, float, float, float, float, float]:
    """Compose PDF matrices so that ``inner`` is applied first, then ``outer``."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _container_matrix(obj: Any) -> tuple[float, float, float, float, float, float] | None:
    """Combined matrix of the Form XObjects containing ``obj``, innermost first."""
    matrix = None
    container = getattr(obj, "container", None)
    depth = 0
    while container is not None and depth < _MATRIX_DEPTH_LIMIT:
        try:
            values = _matrix_values(container)
        except Exception:
            break
        matrix = values if matrix is None else _concat_matrices(values, matrix)
        container = getattr(container, "container", None)
        depth += 1
    return matrix


def _object_page_matrix(obj: Any) -> tuple[float, float, float, float, float, float]:
    """Matrix mapping the object's own text/image space to page user space."""
    values = _matrix_values(obj)
    containers = _container_matrix(obj)
    return values if containers is None else _concat_matrices(containers, values)


def _transform_bbox(matrix: tuple[float, ...], bbox: tuple[float, ...]
                    ) -> tuple[float, float, float, float]:
    """Axis-aligned page-space hull of a rectangle transformed by ``matrix``."""
    a, b, c, d, e, f = matrix
    x0, y0, x1, y1 = (float(value) for value in bbox)
    xs, ys = [], []
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return (min(xs), min(ys), max(xs), max(ys))


def _text_render_mode(obj: Any) -> int:
    """PDF text rendering mode; unknown objects count as normally painted text."""
    try:
        return int(pdfium_c.FPDFTextObj_GetTextRenderMode(obj))
    except Exception:
        return int(pdfium_c.FPDF_TEXTRENDERMODE_FILL)


def _objects(page: Any, textpage: Any):
    # Object order is part of the opaque ID and lets us reject stale selections.
    return list(page.get_objects(textpage=textpage))


def _object_pointer(obj: Any) -> int | None:
    """Raw pointer identity of a page object, stable across helper wrappers."""
    try:
        return ctypes.cast(obj.raw, ctypes.c_void_p).value
    except Exception:
        return None


def _page_object_index(page: Any, target: Any) -> int | None:
    """Index of ``target`` in the page's own draw order (Form contents excluded)."""
    wanted = _object_pointer(target)
    if wanted is None:
        return None
    try:
        candidates = page.get_objects(max_depth=1)
    except Exception:
        return None
    for index, candidate in enumerate(candidates):
        if _object_pointer(candidate) == wanted:
            return index
    return None


def _first_page_object_index(page: Any, objects: list[Any]) -> int | None:
    """Lowest page-level draw index occupied by any of ``objects``."""
    indexes = [_page_object_index(page, item) for item in objects]
    if any(index is None for index in indexes):
        raise ValueError("Не удалось определить порядок объектов; изменение отменено")
    return min(indexes) if indexes else None


def _page_object_indexes(page: Any, objects: list[Any]) -> list[int]:
    """Draw indexes of ``objects`` in page order, refusing unknown positions."""
    indexes = []
    for item in objects:
        index = _page_object_index(page, item)
        if index is None:
            raise ValueError("Не удалось определить порядок объектов; изменение отменено")
        indexes.append(index)
    return sorted(indexes)


def _line_object_counts(lines: list[list[dict[str, Any]]]) -> list[int]:
    """Page objects each wrapped line creates: one per run plus one per stroke."""
    return [len(line) + sum(1 for span in line if span.get("underline"))
            for line in lines]


def _group_line_partition(group: dict[str, Any] | None,
                          expected_objects: int) -> list[list[int]] | None:
    """Indexes of a stored block's object records grouped into their lines.

    Group metadata keeps one record per drawn run and one per wrapped line, but
    not the link between them; the link is recovered from the recorded line text
    (a line's text is the concatenation of its runs) and rejected when ambiguity
    remains, so a wrong mapping can never be applied silently.
    """
    if not group:
        return None
    records = list(group.get("objects") or ())
    lines = list(group.get("lines") or ())
    if not records or not lines or len(records) != expected_objects:
        return None
    if len(records) == len(lines):
        return [[index] for index in range(len(records))]
    partition: list[list[int]] = []
    cursor = 0
    for line in lines:
        target = str(line.get("text", ""))
        if not target:
            return None
        consumed: list[int] = []
        text = ""
        while cursor < len(records) and len(text) < len(target):
            text += str(records[cursor].get("text", ""))
            consumed.append(cursor)
            cursor += 1
            if text == target:
                break
        if text != target or not consumed:
            return None
        partition.append(consumed)
    if cursor != len(records):
        return None
    return partition


def _replacement_paint_slots(page: Any, group: dict[str, Any] | None,
                             selected: list[Any], underline_objects: list[Any],
                             spans: list[dict[str, Any]],
                             width: float) -> list[int]:
    """Insertion positions for a replacement, one per created page object.

    Each replaced part keeps the paint slot of the object it replaces, so foreign
    artwork drawn between the parts of a block stays between the corresponding
    new parts. A block whose parts are interleaved is only reflowed when the new
    line structure maps one-to-one onto the original slots (same number of lines,
    or one new line per original part); every other reflow is refused atomically
    instead of silently changing the page's overlap order or splitting a run from
    its own underline stroke.
    """
    object_indexes = _page_object_indexes(page, selected)
    removed = sorted(object_indexes + _page_object_indexes(page, underline_objects))
    contiguous = not removed or removed[-1] - removed[0] + 1 == len(removed)

    def after_removal(old_index: int) -> int:
        return old_index - sum(1 for value in removed if value < old_index)

    counts = [count for count in _line_object_counts(_wrap_rich_spans(spans, width))
              if count]
    total = sum(counts)
    if not total:
        raise ValueError("Введите хотя бы один символ в текстовом блоке")
    if contiguous:
        base = after_removal(removed[0])
        return list(range(base, base + total))
    removed_set = set(removed)
    if any(index - 1 not in removed_set
           for index in _page_object_indexes(page, underline_objects)):
        raise ValueError("Между текстом и подчёркиванием есть другой объект; "
                         "замена отменена для сохранения наложения")
    partition = _group_line_partition(group, len(selected))
    if partition is None:
        partition = [[index] for index in range(len(selected))]
    # A logical line can itself contain separated painting slots. Recreating
    # all its runs at the first slot would move them across foreign artwork.
    # Line-level replacement is valid only inside a contiguous removed range.
    line_slots_contiguous = all(
        all(index in removed_set for index in range(
            min(object_indexes[item] for item in line),
            max(object_indexes[item] for item in line) + 1))
        for line in partition)
    if len(counts) == len(partition) and line_slots_contiguous:
        positions: list[int] = []
        inserted = 0
        for line_indexes, count in zip(partition, counts):
            base = after_removal(object_indexes[line_indexes[0]]) + inserted
            positions.extend(range(base, base + count))
            inserted += count
        return positions
    if (total == len(object_indexes) and not underline_objects
            and not any(span.get('underline') for span in spans)):
        # Exact run-to-run mapping has no additional path objects to move
        # across an intervening paint slot. Keep every run in its own slot.
        return [after_removal(index) + offset
                for offset, index in enumerate(object_indexes)]
    if len(counts) == len(object_indexes):
        # Same number of parts, but their stored line metadata could not be used
        # (or was ambiguous): each new line group takes one original slot and its
        # runs stay together with their underline strokes inside that slot.
        positions: list[int] = []
        inserted = 0
        for count, index in zip(counts, object_indexes):
            base = after_removal(index) + inserted
            positions.extend(range(base, base + count))
            inserted += count
        return positions
    raise ValueError(
        "Между частями блока нарисованы другие объекты, а новое число строк "
        "не совпадает с исходным. Измените текст без изменения числа строк "
        "или отредактируйте части по отдельности, чтобы сохранить наложение")


def _insert_page_object(page: Any, pageobject: Any, index: int | None) -> None:
    """Insert a page object at ``index`` so the original draw order is preserved.

    PDFium paints page-level objects bottom-up in list order, so a replacement
    must take over the position of the object it replaced instead of being
    appended above later artwork. Refuse the whole edit when indexed insertion
    fails; silently appending would change the document's visible layer order.
    """
    if index is not None:
        try:
            inserted = bool(pdfium_c.FPDFPage_InsertObjectAtIndex(page, pageobject,
                                                                  int(index)))
        except Exception:
            inserted = False
        if inserted:
            pageobject._detach_finalizer()
            pageobject.page = page
            pageobject.pdf = page.pdf
            return
        raise ValueError("Не удалось сохранить порядок наложения; изменение отменено")
    page.insert_obj(pageobject)


def _block_id(page_index: int, index: int, kind: str, bbox: tuple[float, ...],
              text: str, font_name: str, font_size: float) -> str:
    material = repr((page_index, index, kind, tuple(round(v, 4) for v in bbox),
                     text, font_name, round(font_size, 4))).encode("utf-8", "replace")
    return hashlib.sha256(material).hexdigest()[:24]


def _read_text_groups(pdf_bytes: bytes, page_index: int) -> list[dict[str, Any]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_index < 0 or page_index >= len(reader.pages):
        raise IndexError("Номер страницы вне диапазона")
    return _decode_text_groups(reader.pages[page_index])


def _read_all_text_groups(pdf_bytes: bytes) -> list[list[dict[str, Any]]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    return [_decode_text_groups(page) for page in reader.pages]


def _decode_image_records(page: Any) -> list[dict[str, Any]]:
    piece = page.get("/PieceInfo")
    if piece is None:
        return []
    app_info = piece.get_object().get("/PdfDesk")
    if app_info is None:
        return []
    private = app_info.get_object().get("/Private")
    if private is None:
        return []
    records = []
    for reference in private.get_object().get("/ImageBlocks", ()):
        record = reference.get_object()
        bbox = tuple(float(value) for value in record.get("/BBox", ()))
        ident = str(record.get("/Id", ""))
        if ident and len(bbox) == 4:
            records.append({"id": ident, "bbox": bbox})
    return records


def _read_all_image_records(pdf_bytes: bytes) -> list[list[dict[str, Any]]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    return [_decode_image_records(page) for page in reader.pages]


def _write_image_records(pdf_bytes: bytes, page_index: int,
                         records: list[dict[str, Any]]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject,
                               NameObject, TextStringObject)
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    writer = PdfWriter(clone_from=reader)
    page = writer.pages[page_index]
    piece_ref = page.get("/PieceInfo")
    if piece_ref is None:
        piece = DictionaryObject()
        page[NameObject("/PieceInfo")] = piece
    else:
        piece = piece_ref.get_object()
    app_ref = piece.get("/PdfDesk")
    if app_ref is None:
        app_info = DictionaryObject()
        piece[NameObject("/PdfDesk")] = app_info
    else:
        app_info = app_ref.get_object()
    private_ref = app_info.get("/Private")
    if private_ref is None:
        private = DictionaryObject()
        app_info[NameObject("/Private")] = private
    else:
        private = private_ref.get_object()
    records_array = ArrayObject()
    for entry in records:
        records_array.append(DictionaryObject({
            NameObject("/Id"): TextStringObject(entry["id"]),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in entry["bbox"]]),
        }))
    private[NameObject("/ImageBlocks")] = records_array
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _apply_image_records(blocks: list[ContentBlock],
                         records: list[dict[str, Any]]) -> list[ContentBlock]:
    # Only page-level objects can carry PdfDesk image metadata; nested Form
    # XObject images are read-only and must never absorb a record.
    available = {index for index, block in enumerate(blocks)
                 if block.kind == "image" and block.editable}
    replacements = {}
    for record in records:
        candidates = [index for index in available if
                      max(abs(a - b) for a, b in zip(blocks[index].bbox, record["bbox"])) <= 1.25]
        if not candidates:
            continue
        index = min(candidates, key=lambda item: sum(
            abs(a - b) for a, b in zip(blocks[item].bbox, record["bbox"])))
        available.remove(index)
        block = blocks[index]
        replacements[index] = dataclass_replace(block, id="pdfdesk-image:" + record["id"],
                                                group_id=record["id"])
    return [replacements.get(index, block) for index, block in enumerate(blocks)]


def _decode_text_groups(page: Any) -> list[dict[str, Any]]:
    piece_info = page.get("/PieceInfo")
    if piece_info is None:
        return []
    app_info = piece_info.get_object().get("/PdfDesk")
    if app_info is None:
        return []
    private = app_info.get_object().get("/Private")
    if private is None:
        return []
    records = private.get_object().get("/TextBlocks", ())
    groups = []
    for reference in records:
        record = reference.get_object()
        bold_value = record.get("/Bold", False)
        lines = []
        for line_ref in record.get("/Lines", ()):
            line = line_ref.get_object()
            lines.append({"text": str(line.get("/Text", "")),
                          "bbox": tuple(float(x) for x in line.get("/BBox", ()))})
        objects = []
        for object_ref in record.get("/Objects", ()):
            item = object_ref.get_object()
            objects.append({"text": str(item.get("/Text", "")),
                            "bbox": tuple(float(x) for x in item.get("/BBox", ()))})
        spans = []
        for span_ref in record.get("/Spans", ()):
            item = span_ref.get_object()
            spans.append({
                "text": str(item.get("/Text", "")),
                "font_name": str(item.get("/Font", record.get("/Font", "Arial"))),
                "font_size": float(item.get("/FontSize", record.get("/FontSize", 12))),
                "color": str(item.get("/Color", record.get("/Color", "#000000"))),
                "bold": bool(getattr(item.get("/Bold", False), "value", item.get("/Bold", False))),
                "italic": bool(getattr(item.get("/Italic", False), "value", item.get("/Italic", False))),
                "underline": bool(getattr(item.get("/Underline", False), "value", item.get("/Underline", False))),
            })
        underlines = []
        for underline_ref in record.get("/Underlines", ()):
            item = underline_ref.get_object()
            coords = tuple(float(value) for value in item.get("/Coords", ()))
            if len(coords) == 4:
                underlines.append({"coords": coords,
                                   "width": float(item.get("/Width", 0.7)),
                                   "color": str(item.get("/Color", "#000000"))})
        if not lines or any(len(line["bbox"]) != 4 for line in lines):
            continue
        groups.append({
            "id": str(record.get("/Id", "")),
            "text": str(record.get("/Text", "")),
            "font_name": str(record.get("/Font", "Arial")),
            "font_size": float(record.get("/FontSize", 12)),
            "color": str(record.get("/Color", "#000000")),
            "alignment": str(record.get("/Alignment", "left")),
            "orientation": str(record.get("/Orientation", "page")),
            "bold": bool(getattr(bold_value, "value", bold_value)),
            "width": float(record.get("/Width", 0)),
            "height": float(record.get("/Height", 0)),
            "line_spacing": float(record.get("/LineSpacing", 0)),
            "lines": lines,
            "objects": objects,
            "spans": spans,
            "underlines": underlines,
        })
    return groups


def _write_text_groups(pdf_bytes: bytes, page_index: int,
                       groups: list[dict[str, Any]]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                               DictionaryObject, FloatObject, NameObject, TextStringObject)
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    writer = PdfWriter(clone_from=reader)
    page = writer.pages[page_index]
    piece_ref = page.get("/PieceInfo")
    if piece_ref is None:
        piece = DictionaryObject()
        page[NameObject("/PieceInfo")] = piece
    else:
        piece = piece_ref.get_object()
    app_ref = piece.get("/PdfDesk")
    if app_ref is None:
        app_info = DictionaryObject()
        piece[NameObject("/PdfDesk")] = app_info
    else:
        app_info = app_ref.get_object()
    private_ref = app_info.get("/Private")
    if private_ref is None:
        private = DictionaryObject()
        app_info[NameObject("/Private")] = private
    else:
        private = private_ref.get_object()
    records = ArrayObject()
    for group in groups:
        record = DictionaryObject({
            NameObject("/Id"): TextStringObject(group["id"]),
            NameObject("/Text"): TextStringObject(group["text"]),
            NameObject("/Font"): TextStringObject(group["font_name"]),
            NameObject("/FontSize"): FloatObject(group["font_size"]),
            NameObject("/Color"): TextStringObject(group["color"]),
            NameObject("/Alignment"): TextStringObject(group["alignment"]),
            NameObject("/Orientation"): TextStringObject(group.get("orientation", "page")),
            NameObject("/Bold"): BooleanObject(group["bold"]),
            NameObject("/Width"): FloatObject(group["width"]),
            NameObject("/Height"): FloatObject(group["height"]),
            NameObject("/LineSpacing"): FloatObject(group.get("line_spacing", 0)),
        })
        spans = ArrayObject()
        for span in group.get("spans", ()):
            spans.append(DictionaryObject({
                NameObject("/Text"): TextStringObject(span["text"]),
                NameObject("/Font"): TextStringObject(span.get("font_name", group["font_name"])),
                NameObject("/FontSize"): FloatObject(span.get("font_size", group["font_size"])),
                NameObject("/Color"): TextStringObject(span.get("color", group["color"])),
                NameObject("/Bold"): BooleanObject(bool(span.get("bold", False))),
                NameObject("/Italic"): BooleanObject(bool(span.get("italic", False))),
                NameObject("/Underline"): BooleanObject(bool(span.get("underline", False))),
            }))
        if spans:
            record[NameObject("/Spans")] = spans
        lines = ArrayObject()
        for line in group["lines"]:
            line_record = DictionaryObject({
                NameObject("/Text"): TextStringObject(line["text"]),
                NameObject("/BBox"): ArrayObject([FloatObject(v) for v in line["bbox"]]),
            })
            lines.append(line_record)
        record[NameObject("/Lines")] = lines
        objects = ArrayObject()
        for item in group.get("objects", ()):
            objects.append(DictionaryObject({
                NameObject("/Text"): TextStringObject(item["text"]),
                NameObject("/BBox"): ArrayObject([FloatObject(v) for v in item["bbox"]]),
            }))
        if objects:
            record[NameObject("/Objects")] = objects
        underlines = ArrayObject()
        for line in group.get("underlines", ()):
            underlines.append(DictionaryObject({
                NameObject("/Coords"): ArrayObject([FloatObject(v) for v in line["coords"]]),
                NameObject("/Width"): FloatObject(line["width"]),
                NameObject("/Color"): TextStringObject(line["color"]),
            }))
        if underlines:
            record[NameObject("/Underlines")] = underlines
        records.append(record)
    private[NameObject("/TextBlocks")] = records

    # Keep underline strokes as native PDF content streams associated with the
    # PdfDesk block metadata, so replacement/deletion can remove them atomically.
    old_refs = private.get("/UnderlineStreams", ())
    old_keys = set()
    for reference in old_refs:
        if hasattr(reference, "idnum"):
            old_keys.add((reference.idnum, reference.generation))
    contents = page.get("/Contents")
    if contents is not None and old_keys:
        resolved_contents = contents.get_object() if hasattr(contents, "get_object") else contents
        content_array = (resolved_contents if isinstance(resolved_contents, ArrayObject)
                         else ArrayObject([contents]))
        kept = ArrayObject()
        for reference in content_array:
            key = (getattr(reference, "idnum", None), getattr(reference, "generation", None))
            if key not in old_keys:
                kept.append(reference)
        if kept:
            page[NameObject("/Contents")] = kept
        else:
            page.pop(NameObject("/Contents"), None)

    # Underlines are native path objects created next to their text runs, so
    # they inherit the paragraph's position in the page stacking order. Only
    # streams written by older PdfDesk versions are swept here; keeping the
    # /UnderlineStreams key (always empty) preserves the on-disk contract.
    private[NameObject("/UnderlineStreams")] = ArrayObject()
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _apply_text_groups(blocks: list[ContentBlock], groups: list[dict[str, Any]]) -> list[ContentBlock]:
    available = set(range(len(blocks)))
    replacements: list[tuple[int, set[int], ContentBlock]] = []
    for group in groups:
        matched: list[int] = []
        records = group.get("objects") or group["lines"]
        for line in records:
            candidates = [index for index in available if index not in matched and
                          blocks[index].kind == "text" and blocks[index].editable and
                          blocks[index].text.strip() == line["text"].strip() and
                          max(abs(a - b) for a, b in zip(blocks[index].bbox, line["bbox"])) <= 1.25]
            if not candidates:
                matched = []
                break
            matched.append(min(candidates, key=lambda index: sum(
                abs(a - b) for a, b in zip(blocks[index].bbox, line["bbox"]))))
        if not matched:
            continue
        selected = [blocks[index] for index in matched]
        box = (min(item.bbox[0] for item in selected), min(item.bbox[1] for item in selected),
               max(item.bbox[2] for item in selected), max(item.bbox[3] for item in selected))
        group_id = "pdfdesk:" + group["id"]
        aggregate = ContentBlock(
            group_id, "text", box, group["text"], group["font_name"],
            group["font_size"], group["color"], object_ids=tuple(item.id for item in selected),
            alignment=group["alignment"], bold=group["bold"], group_id=group["id"],
            block_width=group["width"], block_height=group["height"],
            orientation=group.get("orientation", "page"),
            rich_spans=tuple(group.get("spans", ())),
            line_spacing=float(group.get("line_spacing", 0)),
            underlines=tuple(group.get("underlines", ())))
        replacements.append((min(matched), set(matched), aggregate))
        available.difference_update(matched)
    if not replacements:
        return blocks
    first_by_index = {first: aggregate for first, _members, aggregate in replacements}
    grouped_indices = set().union(*(members for _first, members, _aggregate in replacements))
    result = []
    for index, block in enumerate(blocks):
        if index in first_by_index:
            result.append(first_by_index[index])
        elif index not in grouped_indices:
            result.append(block)
    return result


def _describe(page_index: int, index: int, obj: Any, textpage: Any) -> ContentBlock | None:
    kind_code = obj.type
    if kind_code not in (pdfium_c.FPDF_PAGEOBJ_TEXT, pdfium_c.FPDF_PAGEOBJ_IMAGE):
        return None
    level = int(getattr(obj, "level", 0) or 0)
    try:
        bbox = tuple(float(v) for v in obj.get_bounds())
    except Exception:
        bbox = (0.0, 0.0, 0.0, 0.0)
    if level > 0:
        # Nested objects report bounds in their Form XObject's own space; map
        # them to page space so selection rectangles stay where the user sees
        # the artwork. Nested objects are read-only (shared forms).
        containers = _container_matrix(obj)
        if containers is not None:
            bbox = _transform_bbox(containers, bbox)
    nested = level > 0
    if kind_code == pdfium_c.FPDF_PAGEOBJ_TEXT:
        try:
            text = obj.extract()
        except Exception:
            text = ""
        if not text.strip() or bbox[2] - bbox[0] < 0.01 or bbox[3] - bbox[1] < 0.01:
            return None
        fname = _font_name(obj)
        try:
            if nested:
                matrix = _object_page_matrix(obj)
                scale = max(0.0001, math.hypot(matrix[2], matrix[3]))
            else:
                scale = _matrix_size_scale(obj)
            size = float(obj.get_font_size()) * scale
        except Exception:
            size = 0.0
        if nested:
            editable, reason = False, NESTED_OBJECT_REASON
        elif _text_render_mode(obj) in _INVISIBLE_RENDER_MODES:
            editable, reason = False, INVISIBLE_TEXT_REASON
        else:
            editable, reason = True, ""
        ident = _block_id(page_index, index, "text", bbox, text, fname, size)
        return ContentBlock(ident, "text", bbox, text, fname, size, _color(obj),
                            editable=editable, reason=reason, object_ids=(ident,))
    ident = _block_id(page_index, index, "image", bbox, "", "", 0.0)
    return ContentBlock(ident, "image", bbox, object_ids=(ident,),
                        editable=not nested, reason=NESTED_OBJECT_REASON if nested else "")


def inspect_content(pdf_bytes: bytes, page_index: int) -> list[ContentBlock]:
    """List native text and image objects on a page.

    Objects inside Form XObjects and invisible (recognized OCR) text runs are
    listed with ``editable=False`` and an actionable ``reason``: they are shown
    so the user knows they exist, but editing them would either change other
    pages or leave the visible scan untouched.
    """
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        if page_index < 0 or page_index >= len(doc):
            raise IndexError("Номер страницы вне диапазона")
        page = doc.get_page(page_index)
        textpage = page.get_textpage()
        try:
            result = []
            objs = _objects(page, textpage)
            for index, obj in enumerate(objs):
                block = _describe(page_index, index, obj, textpage)
                if block is not None:
                    result.append(block)
            objs.clear()
            obj = None
            result = _apply_text_groups(result, _read_text_groups(pdf_bytes, page_index))
            return _apply_image_records(result, _read_all_image_records(pdf_bytes)[page_index])
        finally:
            textpage.close()
            page.close()
    finally:
        doc.close()


def _find_object(doc: Any, page_index: int, block_id: str):
    if page_index < 0 or page_index >= len(doc):
        raise IndexError("Номер страницы вне диапазона")
    page = doc.get_page(page_index)
    textpage = page.get_textpage()
    for index, obj in enumerate(_objects(page, textpage)):
        block = _describe(page_index, index, obj, textpage)
        if block and block.id == block_id:
            return page, textpage, obj, block
    textpage.close()
    page.close()
    raise ValueError("Объект устарел или не найден; обновите список объектов")


def _set_text(obj: Any, value: str, *, charcodes: bool = False) -> None:
    if charcodes:
        values = (ctypes.c_uint32 * len(value))(*(ord(char) for char in value))
        if not pdfium_c.FPDFText_SetCharcodes(obj, values, len(value)):
            raise RuntimeError("PDFium не смог записать кодовые точки текста")
        return
    encoded = (value + "\x00").encode("utf-16-le")
    wide = ctypes.cast(encoded, pdfium_c.FPDF_WIDESTRING)
    if not pdfium_c.FPDFText_SetText(obj, wide):
        raise RuntimeError("PDFium не смог заменить исходный текстовый объект")


def _font_from_file(doc: Any, font_name: str):
    # Reuse the application's font lookup and embed a Unicode-capable TrueType font.
    from .pdf_backend import find_font_file
    path = find_font_file(font_name)
    with open(path, "rb") as source:
        data = source.read()
    storage = ctypes.create_string_buffer(data)
    raw = pdfium_c.FPDFText_LoadFont(
        doc, ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint8)), len(data),
        pdfium_c.FPDF_FONT_TRUETYPE, 1,
    )
    if not raw:
        raise ValueError(f"Не удалось загрузить шрифт «{font_name}»")
    from pypdfium2._helpers.pageobjects import PdfFont
    font = PdfFont(raw, parent=doc, needs_free=True)
    doc._add_kid(font)
    # Keep native font wrappers alive until PdfDocument.close() explicitly
    # closes its children; otherwise their weakref finalizers can run after the
    # owning document has already closed.
    owned = getattr(doc, "_pdfdesk_source_fonts", None)
    if owned is None:
        owned = []
        doc._pdfdesk_source_fonts = owned
    owned.append(font)
    return font


def _unicode_glyph_ids(font_name: str, value: str) -> list[int]:
    """Return glyph IDs so PDFium's CID font writes Unicode glyphs correctly."""
    from .pdf_backend import find_font_file
    from reportlab.pdfbase.ttfonts import TTFont
    face = TTFont("PdfDeskSource", find_font_file(font_name)).face
    cmap = face.charToGlyph
    codes = []
    for char in value:
        glyph_id = cmap.get(ord(char))
        if glyph_id is None:
            raise ValueError("Шрифт «%s» не содержит символ %s" % (font_name, char))
        codes.append(int(glyph_id))
    return codes


def _font_resource_keys(pdf_bytes: bytes, page_index: int) -> set[str]:
    """Return the page font resource names before this edit."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_index < 0 or page_index >= len(reader.pages):
        raise IndexError("Номер страницы вне диапазона")
    resources = reader.pages[page_index].get("/Resources")
    if resources is None:
        return set()
    fonts = resources.get_object().get("/Font")
    return set(map(str, fonts.get_object().keys())) if fonts is not None else set()


def _is_pdfdesk_unicode(target: Any) -> bool:
    """Whether a /ToUnicode stream was written by PdfDesk itself."""
    if target is None:
        return False
    try:
        return b"PdfDeskUnicode" in target.get_object().get_data()
    except Exception:
        return False


def _patch_unicode_map(pdf_bytes: bytes, font_name: str, value: str,
                       page_index: int, original_font_keys: set[str],
                       *, target_font_name: str | None = None,
                       include_pdfdesk_existing: bool = False) -> bytes:
    """Patch the font resource(s) which carry this replacement text.

    ``include_pdfdesk_existing`` also extends resources that already existed but
    hold a PdfDesk-written ToUnicode map: PDFium may rebuild the page's font
    dictionary (reusing or renaming keys) when objects are inserted inside the
    page, and several PdfDesk paragraphs can then share one rebuilt resource.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from .pdf_backend import find_font_file
    from reportlab.pdfbase.ttfonts import TTFont

    face = TTFont("PdfDeskUnicodeMap", find_font_file(font_name)).face
    cmap = face.charToGlyph
    mappings = {}
    for char in value:
        gid = cmap.get(ord(char))
        if gid is None:
            continue
        unicode_hex = char.encode("utf-16-be").hex().upper()
        mappings[int(gid)] = unicode_hex
    if not mappings:
        return pdf_bytes

    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    writer = PdfWriter(clone_from=reader)
    patched = False
    find_font_file(font_name)  # Validate the exact face used for the glyph mapping.
    page = writer.pages[page_index]
    target_name = ""
    if target_font_name:
        target_face = TTFont("PdfDeskUnicodeTarget", find_font_file(target_font_name)).face
        target_name = re.sub(r"[^a-z0-9]", "", target_face.name.decode("latin1").lower())
        target_name = re.sub(r"^[a-f0-9]{6}", "", target_name)
        target_name = target_name.removeprefix("pdfdesk")
    candidates = []
    for page_number, page in enumerate(writer.pages):
        if page_number != page_index:
            continue
        resources = page.get("/Resources")
        if resources is None:
            continue
        fonts = resources.get_object().get("/Font")
        if fonts is None:
            continue
        for key, reference in fonts.get_object().items():
            if str(key) in original_font_keys:
                if not include_pdfdesk_existing:
                    continue
                # Never touch a source-document font: only extend a map that
                # PdfDesk wrote itself.
                if not _is_pdfdesk_unicode(reference.get_object().get("/ToUnicode")):
                    continue
            if not str(key).startswith("/FXF"):
                continue
            font = reference.get_object()
            if target_name:
                base = str(font.get("/BaseFont", "")).lstrip("/")
                normalized = re.sub(r"[^a-z0-9]", "", base.lower())
                normalized = re.sub(r"^[a-f0-9]{6}", "", normalized)
                normalized = normalized.removeprefix("pdfdesk")
                descriptor = font.get("/FontDescriptor")
                if descriptor is not None:
                    font_name_value = str(descriptor.get_object().get("/FontName", "")).lstrip("/")
                    descriptor_name = re.sub(r"[^a-z0-9]", "", font_name_value.lower())
                    normalized += descriptor_name.removeprefix("pdfdesk")
                if target_name not in normalized and normalized not in target_name:
                    continue
            # This resource was absent before the operation and belongs to its
            # newly embedded face, or it is a PdfDesk map being extended with the
            # glyphs of another paragraph that shares the rebuilt resource.
            # PDFium normalizes bold PostScript names inconsistently (for example
            # Arial,Bold vs. arialbd.ttf), so scoping is done by the before/after
            # resource set plus the face-name comparison above.
            entries = []
            for gid, unicode_hex in mappings.items():
                entries.append("<%04X> <%s>" % (gid & 0xFFFF, unicode_hex))
            # PDF CMaps limit each bfchar section to 100 entries.
            chunks = [entries[i:i + 100] for i in range(0, len(entries), 100)]
            bfchar = "\n".join("%d beginbfchar\n%s\nendbfchar" %
                               (len(chunk), "\n".join(chunk)) for chunk in chunks)
            cmap_text = ("/CIDInit /ProcSet findresource begin\n12 dict begin\n"
                         "begincmap\n/CIDSystemInfo <</Registry (Adobe) "
                         "/Ordering (UCS) /Supplement 0>> def\n"
                         "/CMapName /PdfDeskUnicode def\n/CMapType 2 def\n"
                         "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
                         "%s\n"
                         "endcmap\nCMapName currentdict /CMap defineresource pop\n"
                         "end\nend\n" % bfchar)
            replacement = DecodedStreamObject()
            replacement.set_data(cmap_text.encode("latin1"))
            font[NameObject("/ToUnicode")] = writer._add_object(replacement)
            patched = True
            candidates.append(str(key))
    if not patched:
        return pdf_bytes
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _replace_font(page: Any, obj: Any, text: str, font_name: str, size: float,
                  color: str) -> None:
    doc = page.pdf
    font = _font_from_file(doc, font_name)
    matrix = obj.get_matrix()
    local_size = float(size) / max(0.0001, math.hypot(matrix.c, matrix.d))
    rgba = tuple(int(color[i:i+2], 16) for i in (1, 3, 5)) + (255,)
    new_obj = pdfium_c.FPDFPageObj_CreateTextObj(doc, font, local_size)
    if not new_obj:
        raise RuntimeError("Не удалось создать текстовый объект")
    from pypdfium2._helpers.pageobjects import PdfTextObj
    wrapper = PdfTextObj(new_obj, pdf=doc)
    try:
        values = _unicode_glyph_ids(font_name, text)
        codes = (ctypes.c_uint32 * len(values))(*values)
        if not pdfium_c.FPDFText_SetCharcodes(wrapper, codes, len(values)):
            raise RuntimeError("PDFium не смог записать символы встроенного шрифта")
        if not pdfium_c.FPDFPageObj_SetFillColor(wrapper, *rgba):
            raise RuntimeError("Не удалось установить цвет текста")
        wrapper.set_matrix(matrix)
        insert_index = _page_object_index(page, obj)
        page.remove_obj(obj)
        _insert_page_object(page, wrapper, insert_index)
    except Exception:
        if wrapper.page is None:
            wrapper.close()
        raise
    finally:
        if obj.raw and obj.page is None:
            obj.close()
    return wrapper


def _validate_color(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Цвет должен быть в формате #RRGGBB")
    return value.upper()


def _subset_replacement_font(font_name: str) -> str:
    """Resolve a stable installed family for a PDF subset font name."""
    base_name = re.sub(r"^[A-Z]{6}\+", "", font_name or "")
    try:
        from .pdf_backend import _family_from_font_name, find_font_file
        family = _family_from_font_name(base_name)
        find_font_file(family)
        return family
    except Exception:
        return "Arial"


def _font_width(font_name: str, text: str, size: float) -> float:
    from .pdf_backend import find_font_file
    from reportlab.pdfbase.ttfonts import TTFont
    face = TTFont("PdfDeskMeasure", find_font_file(font_name)).face
    return sum(float(face.charWidths.get(ord(ch), face.charWidths.get(32, 500)))
               for ch in text) * size / 1000.0


def _wrap_lines(text: str, font_name: str, size: float, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = paragraph.split()
        line = ""
        for word in words:
            candidate = word if not line else line + " " + word
            if line and _font_width(font_name, candidate, size) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines or [""]


_RICH_KEYS = ("font_name", "font_size", "color", "bold", "italic", "underline")


def _styled_font_name(family: str, bold: bool, italic: bool) -> str:
    """Resolve a family face for native text while keeping style metadata separate."""
    from .pdf_backend import find_font_file
    base = re.sub(r"\s+(bold\s+italic|italic\s+bold|bold|italic)$", "", str(family or "Arial"),
                  flags=re.I).strip() or "Arial"
    suffix = "Bold Italic" if bold and italic else "Bold" if bold else "Italic" if italic else ""
    requested = (base + (" " + suffix if suffix else "")).strip()
    try:
        find_font_file(requested)
        return requested
    except Exception:
        fallback = "Arial" + (" " + suffix if suffix else "")
        find_font_file(fallback)
        return fallback


def _normalize_rich_spans(value: str, spans: Any, defaults: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in spans or ():
        if not isinstance(item, dict):
            raise ValueError("Форматированный фрагмент должен быть словарём")
        text = str(item.get("text", ""))
        if not text:
            continue
        span = {
            "text": text,
            "font_name": str(item.get("font_name", defaults["font_name"])) or defaults["font_name"],
            "font_size": float(item.get("font_size", defaults["font_size"])),
            "color": _validate_color(item.get("color", defaults["color"])),
            "bold": bool(item.get("bold", defaults["bold"])),
            "italic": bool(item.get("italic", defaults.get("italic", False))),
            "underline": bool(item.get("underline", defaults.get("underline", False))),
        }
        if not math.isfinite(span["font_size"]) or not 0.1 <= span["font_size"] <= 1000:
            raise ValueError("Размер шрифта должен быть от 0.1 до 1000 пунктов")
        if result and all(result[-1][key] == span[key] for key in _RICH_KEYS):
            result[-1]["text"] += text
        else:
            result.append(span)
    if "".join(item["text"] for item in result) != value:
        raise ValueError("Форматированные фрагменты не совпадают с текстом абзаца")
    return result


def _remap_rich_spans(old_text: str, old_spans: Any, new_text: str,
                      defaults: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep existing character formatting through plain-text edits where possible."""
    import difflib
    normalized = _normalize_rich_spans(old_text, old_spans, defaults) if old_spans else []
    if not normalized:
        return [{"text": new_text, **{key: defaults[key] for key in _RICH_KEYS}}]
    old_formats = []
    for span in normalized:
        old_formats.extend([{key: span[key] for key in _RICH_KEYS if key != "text"}]
                           * len(span["text"]))
    mapped = [None] * len(new_text)
    matcher = difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(b1 - b0):
                mapped[b0 + offset] = old_formats[a0 + offset]
        elif tag == "replace" and b1 > b0:
            style = old_formats[a0] if a0 < len(old_formats) else defaults
            for index in range(b0, b1):
                mapped[index] = style
    for index, style in enumerate(mapped):
        if style is None:
            mapped[index] = {key: defaults[key] for key in _RICH_KEYS if key != "text"}
    result = []
    for char, style in zip(new_text, mapped):
        if result and all(result[-1][key] == style[key] for key in _RICH_KEYS if key != "text"):
            result[-1]["text"] += char
        else:
            result.append({"text": char, **style})
    return result


def _wrap_rich_spans(spans: list[dict[str, Any]], width: float) -> list[list[dict[str, Any]]]:
    """Wrap styled text while preserving style boundaries and hard line breaks."""
    from .pdf_backend import find_font_file
    from reportlab.pdfbase.ttfonts import TTFont
    metric_cache = {}

    def char_width(char: str, style: dict[str, Any]) -> float:
        face = _styled_font_name(style["font_name"], style["bold"], style["italic"])
        key = (face, style["font_size"])
        if key not in metric_cache:
            metric_cache[key] = TTFont("PdfDeskRichMeasure", find_font_file(face)).face
        face_metrics = metric_cache[key]
        return float(face_metrics.charWidths.get(ord(char), face_metrics.charWidths.get(32, 500))) * \
            style["font_size"] / 1000.0

    characters = []
    for span in spans:
        style = {key: span[key] for key in _RICH_KEYS if key != "text"}
        for char in span["text"].replace("\r\n", "\n").replace("\r", "\n"):
            characters.append((char, style))

    lines: list[list[dict[str, Any]]] = []
    paragraph: list[tuple[str, dict[str, Any]]] = []

    def append_line(chars):
        line = []
        for char, style in chars:
            if line and all(line[-1][key] == style[key] for key in _RICH_KEYS if key != "text"):
                line[-1]["text"] += char
            else:
                line.append({"text": char, **style})
        lines.append(line)

    def wrap_paragraph(chars):
        if not chars:
            lines.append([])
            return
        current = []
        current_width = 0.0
        index = 0
        while index < len(chars):
            char, style = chars[index]
            if char.isspace():
                run = []
                run_width = 0.0
                while index < len(chars) and chars[index][0].isspace():
                    item = chars[index]
                    run.append(item)
                    run_width += char_width(item[0], item[1])
                    index += 1
                if current and current_width + run_width > width:
                    append_line(current)
                    current, current_width = [], 0.0
                elif current:
                    current.extend(run)
                    current_width += run_width
                continue
            word = []
            word_width = 0.0
            while index < len(chars) and not chars[index][0].isspace():
                item = chars[index]
                word.append(item)
                word_width += char_width(item[0], item[1])
                index += 1
            if word_width > width:
                # Preserve unbroken words as searchable words. Splitting an
                # oversized token into glyph-sized PDF objects makes ordinary
                # text extraction return fragments such as "PdfD\nesk". The
                # caller rejects a width that cannot contain this token.
                if current:
                    append_line(current)
                    current, current_width = [], 0.0
                append_line(word)
                continue
            if current and current_width + word_width > width:
                append_line(current)
                current, current_width = [], 0.0
            for item in word:
                item_width = char_width(item[0], item[1])
                if current and current_width + item_width > width:
                    append_line(current)
                    current, current_width = [], 0.0
                current.append(item)
                current_width += item_width
        append_line(current)

    for char, style in characters:
        if char == "\n":
            wrap_paragraph(paragraph)
            paragraph = []
        else:
            paragraph.append((char, style))
    wrap_paragraph(paragraph)
    return lines


def _add_text_run(page: Any, template: Any, text: str, font_name: str,
                  size: float, color: str, *, baseline_offset: float = 0.0,
                  width: float = 0.0, alignment: str = "left",
                  line_direction: float | None = None,
                  x_offset: float = 0.0, insert_index: int | None = None):
    doc = page.pdf
    font = _font_from_file(doc, font_name)
    matrix = template.get_matrix()
    scale = max(0.0001, math.hypot(matrix.a, matrix.b))
    local_size = float(size) / max(0.0001, math.hypot(matrix.c, matrix.d))
    line_width = _font_width(font_name, text, size)
    align = alignment.lower()
    offset = float(x_offset)
    if align in ("right", "center"):
        offset += max(0.0, width - line_width)
    if align == "center":
        offset /= 2.0
    # On 90/270 degree pages PDFium's text extraction axis is reversed relative
    # to the unrotated user-space y axis. Advance rows in the opposite direction
    # so extracted/read order still follows the editor's top-to-bottom order.
    if line_direction is None:
        line_direction = 1.0 if page.get_rotation() in (90, 270) else -1.0
    matrix = PdfMatrix(matrix.a, matrix.b, matrix.c, matrix.d,
                       matrix.e + matrix.a * offset / scale + matrix.c * baseline_offset * line_direction / scale,
                       matrix.f + matrix.b * offset / scale + matrix.d * baseline_offset * line_direction / scale)
    raw = pdfium_c.FPDFPageObj_CreateTextObj(doc, font, local_size)
    if not raw:
        raise RuntimeError("Не удалось создать текстовый объект")
    from pypdfium2._helpers.pageobjects import PdfTextObj
    wrapper = PdfTextObj(raw, pdf=doc)
    try:
        values = _unicode_glyph_ids(font_name, text)
        codes = (ctypes.c_uint32 * len(values))(*values)
        if values and not pdfium_c.FPDFText_SetCharcodes(wrapper, codes, len(values)):
            raise RuntimeError("PDFium не смог записать символы встроенного шрифта")
        rgba = tuple(int(color[i:i+2], 16) for i in (1, 3, 5)) + (255,)
        if not pdfium_c.FPDFPageObj_SetFillColor(wrapper, *rgba):
            raise RuntimeError("Не удалось установить цвет текста")
        wrapper.set_matrix(matrix)
        _insert_page_object(page, wrapper, insert_index)
        return wrapper
    except Exception:
        if wrapper.page is None:
            wrapper.close()
        raise


def _add_underline_object(page: Any, coords: tuple[float, float, float, float],
                          width: float, color: str, insert_index: int | None = None):
    """Draw one underline stroke as a native path object.

    A path object inherits the draw position of the paragraph it belongs to,
    unlike a separate content stream, which is always painted above everything
    that was drawn before it.
    """
    doc = page.pdf
    x1, y1, x2, y2 = (float(value) for value in coords)
    raw = pdfium_c.FPDFPageObj_CreateNewPath(x1, y1)
    if not raw:
        raise RuntimeError("Не удалось создать объект подчёркивания")
    from pypdfium2._helpers.pageobjects import PdfObject
    wrapper = PdfObject(raw, pdf=doc)
    try:
        if not pdfium_c.FPDFPath_LineTo(wrapper, x2, y2):
            raise RuntimeError("Не удалось построить линию подчёркивания")
        if not pdfium_c.FPDFPath_SetDrawMode(wrapper, pdfium_c.FPDF_FILLMODE_NONE, True):
            raise RuntimeError("Не удалось включить обводку подчёркивания")
        rgba = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        if not pdfium_c.FPDFPageObj_SetStrokeColor(wrapper, *rgba):
            raise RuntimeError("Не удалось установить цвет подчёркивания")
        if not pdfium_c.FPDFPageObj_SetStrokeWidth(wrapper, max(0.1, float(width))):
            raise RuntimeError("Не удалось установить толщину подчёркивания")
        _insert_page_object(page, wrapper, insert_index)
        return wrapper
    except Exception:
        if wrapper.page is None:
            wrapper.close()
        raise


def _underline_bounds(coords: tuple[float, float, float, float], width: float
                      ) -> tuple[float, float, float, float]:
    """Page-space bounds PDFium reports for a stroked underline segment.

    PDFium expands the bounds of a stroked path by the full stroke width on both
    sides, so the recorded geometry is padded by ``width`` (not half of it).
    """
    x1, y1, x2, y2 = (float(value) for value in coords)
    padding = max(0.1, float(width))
    return (min(x1, x2) - padding, min(y1, y2) - padding,
            max(x1, x2) + padding, max(y1, y2) + padding)


def _find_underline_objects(page: Any, records: Any) -> list[Any]:
    """Locate PdfDesk underline strokes that are drawn as page path objects.

    Strokes are identified by their exact recorded geometry, stroke colour and
    stroke width; documents written by older PdfDesk versions kept underlines in
    separate content streams instead, and those are removed through the
    ``/UnderlineStreams`` metadata during the save pass.
    """
    wanted = []
    for record in records or ():
        coords = tuple(record.get("coords") or ())
        if len(coords) != 4:
            continue
        color = str(record.get("color", "#000000"))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            continue
        width = max(0.1, float(record.get("width", 0.7)))
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        wanted.append((_underline_bounds(coords, width), rgb, width))
    if not wanted:
        return []
    found = []
    try:
        candidates = list(page.get_objects(max_depth=1))
    except Exception:
        return []
    for candidate in candidates:
        if int(candidate.type) != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        try:
            bounds = tuple(float(value) for value in candidate.get_bounds())
        except Exception:
            continue
        for box, rgb, width in wanted:
            if any(abs(a - b) > 0.4 for a, b in zip(bounds, box)):
                continue
            fill_mode = ctypes.c_int()
            stroke_mode = ctypes.c_int()
            if not pdfium_c.FPDFPath_GetDrawMode(candidate, ctypes.byref(fill_mode),
                                                 ctypes.byref(stroke_mode)):
                continue
            if not stroke_mode.value:
                continue
            values = [ctypes.c_uint() for _ in range(4)]
            if not pdfium_c.FPDFPageObj_GetStrokeColor(
                    candidate, *(ctypes.byref(value) for value in values)):
                continue
            if tuple(value.value for value in values[:3]) != rgb:
                continue
            stroke_width = ctypes.c_float()
            if not pdfium_c.FPDFPageObj_GetStrokeWidth(candidate,
                                                       ctypes.byref(stroke_width)):
                continue
            if abs(float(stroke_width.value) - width) > 0.05:
                continue
            found.append(candidate)
            break
    return found


def _rich_line_width(line: list[dict[str, Any]]) -> float:
    total = 0.0
    for span in line:
        face = _styled_font_name(span["font_name"], span["bold"], span["italic"])
        total += _font_width(face, span["text"], span["font_size"])
    return total


def _add_rich_paragraph(page: Any, template: Any, spans: list[dict[str, Any]],
                        width: float, alignment: str, orientation: str,
                        line_spacing: float = 0.0,
                        insert_index: int | None = None,
                        insert_positions: list[int] | None = None):
    """Create the paragraph's runs (and underline strokes) in draw order.

    ``insert_index`` inserts every created object at that page position in
    sequence. ``insert_positions`` (one page position per created object, in
    creation order) keeps each part of a block in the paint slot of the object it
    replaces, including when foreign objects are drawn between the old parts.
    """
    lines = _wrap_rich_spans(spans, width)
    line_heights = [max(line_spacing,
                        max((span["font_size"] * 1.2 for span in line), default=0.0))
                    for line in lines]
    objects = []
    line_records = []
    created = []
    unicode_by_font: dict[str, str] = {}
    underlines = []
    cursor = insert_index
    slots = list(insert_positions) if insert_positions is not None else None
    slot_cursor = 0

    def next_slot() -> int | None:
        nonlocal slot_cursor
        if slots is None:
            return None
        if slot_cursor >= len(slots):
            raise ValueError("Не удалось распределить объекты по слоям наложения")
        value = slots[slot_cursor]
        slot_cursor += 1
        return value

    baseline_offset = 0.0
    for line, line_height in zip(lines, line_heights):
        if not line:
            baseline_offset += line_height or max(span["font_size"] for span in spans) * 1.2
            continue
        run_width = _rich_line_width(line)
        line_offset = max(0.0, width - run_width) if alignment in ("center", "right") else 0.0
        item_bboxes = []
        line_text = ""
        offset = line_offset
        for span in line:
            face = _styled_font_name(span["font_name"], span["bold"], span["italic"])
            target = next_slot() if slots is not None else cursor
            run = _add_text_run(page, template, span["text"], face,
                                span["font_size"], span["color"],
                                baseline_offset=baseline_offset,
                                line_direction=(-1 if orientation == "display" else None),
                                x_offset=offset, insert_index=target)
            if slots is None and cursor is not None:
                cursor += 1
            created.append(run)
            bbox = tuple(float(item) for item in run.get_bounds())
            objects.append({"text": span["text"], "bbox": bbox})
            if span["underline"]:
                matrix = run.get_matrix()
                underline_offset = float(span["font_size"]) * 0.12
                underline_width = _font_width(face, span["text"], span["font_size"])
                stroke = {
                    "coords": (
                        matrix.e - matrix.c * underline_offset,
                        matrix.f - matrix.d * underline_offset,
                        matrix.e - matrix.c * underline_offset + matrix.a * underline_width,
                        matrix.f - matrix.d * underline_offset + matrix.b * underline_width,
                    ),
                    "width": max(0.45, float(span["font_size"]) * 0.055),
                    "color": span["color"],
                }
                # The stroke is inserted right after its run, so it shares the
                # paragraph's stacking position instead of floating above the
                # artwork that was drawn later on the page.
                target = next_slot() if slots is not None else cursor
                created.append(_add_underline_object(
                    page, stroke["coords"], stroke["width"], stroke["color"],
                    target))
                if slots is None and cursor is not None:
                    cursor += 1
                underlines.append(stroke)
            item_bboxes.append(bbox)
            line_text += span["text"]
            offset += _font_width(face, span["text"], span["font_size"])
            unicode_by_font[face] = unicode_by_font.get(face, "") + span["text"]
        line_records.append({
            "text": line_text,
            "bbox": (min(box[0] for box in item_bboxes), min(box[1] for box in item_bboxes),
                     max(box[2] for box in item_bboxes), max(box[3] for box in item_bboxes)),
        })
        baseline_offset += line_height
    if slots is not None and slot_cursor != len(slots):
        raise ValueError("Не удалось распределить объекты по слоям наложения")
    return lines, line_records, objects, created, unicode_by_font, baseline_offset, underlines


def _image_payload(image_data: Any):
    """Decode and validate bytes/base64 image input before opening a PDFium handle."""
    from PIL import Image, ImageOps
    if isinstance(image_data, str):
        encoded = image_data.strip()
        if encoded.lower().startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError("Данные изображения должны быть base64")
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("Некорректная base64 строка изображения") from exc
    elif isinstance(image_data, (bytes, bytearray, memoryview)):
        data = bytes(image_data)
    else:
        raise TypeError("Изображение должно быть bytes или base64 строкой")
    if not data or len(data) > 30 * 1024 * 1024:
        raise ValueError("Размер изображения должен быть от 1 байта до 30 МБ")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ("PNG", "JPEG"):
                raise ValueError("Поддерживаются изображения PNG и JPEG")
            if probe.width <= 0 or probe.height <= 0 or probe.width * probe.height > 120_000_000:
                raise ValueError("Недопустимый размер изображения")
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            oriented = ImageOps.exif_transpose(source)
            has_alpha = ("A" in oriented.getbands() or
                         "transparency" in oriented.info)
            return oriented.convert("RGBA" if has_alpha else "RGB")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Не удалось прочитать изображение PNG или JPEG") from exc


def _insert_image_object(page: Any, image: Any, rect: tuple[float, float, float, float]):
    from pypdfium2._helpers.bitmap import PdfBitmap
    from pypdfium2._helpers.pageobjects import PdfImage
    x, y, width, height = rect
    if not all(math.isfinite(value) for value in rect) or width <= 0 or height <= 0:
        raise ValueError("Прямоугольник изображения должен иметь положительные размеры")
    if max(abs(x), abs(y), width, height) > 100000:
        raise ValueError("Прямоугольник изображения выходит за допустимые пределы")
    image_obj = PdfImage.new(page.pdf)
    bitmap = PdfBitmap.from_pil(image)
    try:
        image_obj.set_bitmap(bitmap, pages=[page])
    finally:
        bitmap.close()
    image_obj.set_matrix(PdfMatrix(width, 0, 0, height, x, y))
    page.insert_obj(image_obj)
    owned = getattr(page.pdf, "_pdfdesk_source_images", None)
    if owned is None:
        owned = []
        page.pdf._pdfdesk_source_images = owned
    owned.append(image_obj)
    return image_obj


def _set_image_data(obj: Any, page: Any, image_data: Any) -> None:
    from pypdfium2._helpers.bitmap import PdfBitmap
    image = _image_payload(image_data)
    bitmap = PdfBitmap.from_pil(image)
    try:
        # This replaces the image stream in place; PDFium leaves its transform,
        # clipping, and object position intact.
        obj.set_bitmap(bitmap, pages=[page])
    finally:
        bitmap.close()


def _store_image_record(records: list[dict[str, Any]], block: ContentBlock,
                        obj: Any) -> None:
    ident = block.group_id or uuid.uuid4().hex
    bbox = tuple(float(value) for value in obj.get_bounds())
    replacement = {"id": ident, "bbox": bbox}
    for index, record in enumerate(records):
        if record["id"] == ident:
            records[index] = replacement
            break
    else:
        records.append(replacement)


def add_native_image(pdf_bytes: bytes, page_index: int, image_data: Any,
                     rect: tuple[float, float, float, float]) -> bytes:
    """Insert an editable image page object using unrotated PDF points (x,y,w,h)."""
    image = _image_payload(image_data)
    if len(rect) != 4:
        raise ValueError("Укажите x, y, ширину и высоту изображения")
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        if page_index < 0 or page_index >= len(doc):
            raise IndexError("Номер страницы вне диапазона")
        page = doc.get_page(page_index)
        try:
            image_obj = _insert_image_object(page, image, tuple(map(float, rect)))
            page.gen_content()
            bbox = tuple(float(value) for value in image_obj.get_bounds())
            output = io.BytesIO()
            doc.save(output)
            result = output.getvalue()
        finally:
            page.close()
        records_by_page = _read_all_image_records(pdf_bytes)
        records = records_by_page[page_index]
        records.append({"id": uuid.uuid4().hex, "bbox": bbox})
        return _write_image_records(result, page_index, records)
    finally:
        doc.close()
        owned = getattr(doc, "_pdfdesk_source_images", None)
        if owned is not None:
            owned.clear()


def _page_display_geometry(pdf_bytes: bytes, page_index: int):
    from pypdf import PdfReader
    from .pdf_backend import _PageGeom, _norm_rotation
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_index < 0 or page_index >= len(reader.pages):
        raise IndexError("Номер страницы вне диапазона")
    page = reader.pages[page_index]
    media = page.mediabox
    crop = page.cropbox
    left, bottom = max(float(media.left), float(crop.left)), max(float(media.bottom), float(crop.bottom))
    right, top = min(float(media.right), float(crop.right)), min(float(media.top), float(crop.top))
    if right - left < 1 or top - bottom < 1:
        raise ValueError("У страницы недопустимая область отображения")
    rotation = _norm_rotation(page.get_inherited("/Rotate", 0) or 0)
    return _PageGeom(left, bottom, right - left, top - bottom, rotation)


def add_native_text(pdf_bytes: bytes, page_index: int, text: str,
                    rect: tuple[float, float, float, float], *,
                    font_name: str = "Arial", font_size: float = 14.0,
                    color: str = "#000000", bold: bool = False,
                    alignment: str = "left", orientation: str = "display") -> bytes:
    """Add searchable native text runs grouped as one editable paragraph.

    ``rect`` is (x, y, width, height) in unrotated PDF user points. ``display``
    orientation counter-rotates the page's /Rotate so new text remains horizontal
    on screen; ``page`` orientation follows the page's existing content axes.
    """
    value = str(text or "")
    if not value.strip():
        raise ValueError("Введите текст перед добавлением")
    if len(rect) != 4:
        raise ValueError("Укажите x, y, ширину и высоту текстового блока")
    x, y, width, height = map(float, rect)
    if (not all(math.isfinite(item) for item in (x, y, width, height)) or
            width <= 0 or height <= 0 or max(abs(x), abs(y), width, height) > 100000):
        raise ValueError("Недопустимый размер или координаты текстового блока")
    size = float(font_size)
    if not math.isfinite(size) or not 0.1 <= size <= 1000:
        raise ValueError("Размер шрифта должен быть от 0.1 до 1000 пунктов")
    color_value = _validate_color(color)
    alignment_value = str(alignment).lower().strip()
    if alignment_value not in ("left", "center", "right"):
        raise ValueError("Выравнивание должно быть left, center или right")
    orientation_value = str(orientation).lower().strip()
    if orientation_value not in ("display", "page"):
        raise ValueError("Ориентация должна быть display или page")
    family = str(font_name or "Arial").strip() or "Arial"
    if bold and not re.search(r"\b(bold|semibold|black|heavy|bd)\b", family, re.I):
        family += " Bold"
    try:
        from .pdf_backend import find_font_file
        find_font_file(family)
    except Exception:
        family = "Arial Bold" if bold else "Arial"
        find_font_file(family)
    geometry = _page_display_geometry(pdf_bytes, page_index) if orientation_value == "display" else None
    if geometry is not None:
        from .pdf_backend import _display_rect_to_user, _user_to_display_point
        corners = [_user_to_display_point(geometry, px, py) for px, py in (
            (x, y), (x + width, y), (x, y + height), (x + width, y + height))]
        display_x = min(point[0] for point in corners)
        display_y = min(point[1] for point in corners)
        display_width = max(point[0] for point in corners) - display_x
        display_height = max(point[1] for point in corners) - display_y
        if (display_x < -0.01 or display_y < -0.01 or
                display_x + display_width > geometry.display_width + 0.01 or
                display_y + display_height > geometry.display_height + 0.01):
            raise ValueError("Текстовый блок выходит за границы страницы")
        layout_width = display_width
        layout_height = display_height
    else:
        layout_width, layout_height = width, height
    lines = _wrap_lines(value, family, size, layout_width)
    line_height = size * 1.2
    required_height = line_height * len(lines)
    if required_height > layout_height + 0.01:
        if geometry is None:
            raise ValueError("Текст не помещается в указанную высоту блока")
        # Qt's measured text box can be a little shorter than PDF font leading.
        # Extend the block down from the same display-top anchor, then reject if
        # that would place any row outside the visible page.
        display_height = required_height
        if display_y + display_height > geometry.display_height + 0.01:
            raise ValueError("Текст не помещается на странице")
        x, y, width, height = _display_rect_to_user(
            geometry, display_x, display_y, display_width, display_height)
        layout_height = display_height

    original_font_keys = _font_resource_keys(pdf_bytes, page_index)
    groups_by_page = _read_all_text_groups(pdf_bytes)
    doc = pdfium.PdfDocument(pdf_bytes)
    created = []
    template = None
    try:
        if page_index < 0 or page_index >= len(doc):
            raise IndexError("Номер страницы вне диапазона")
        page = doc.get_page(page_index)
        try:
            if geometry is not None:
                from .pdf_backend import _display_to_user_point
                # One display-space font size below the visual top edge gives
                # native glyphs a baseline while keeping the paragraph upright.
                user_x, user_y = _display_to_user_point(geometry, display_x,
                                                        display_y + size)
                rotation = geometry.rotation
                matrices = {
                    0: PdfMatrix(1, 0, 0, 1, user_x, user_y),
                    90: PdfMatrix(0, 1, -1, 0, user_x, user_y),
                    180: PdfMatrix(-1, 0, 0, -1, user_x, user_y),
                    270: PdfMatrix(0, -1, 1, 0, user_x, user_y),
                }
                matrix = matrices[rotation]
            else:
                # Preserve source-page axes when explicitly requested.
                baseline = (y + size if page.get_rotation() in (90, 270)
                            else y + height - size)
                matrix = PdfMatrix(1, 0, 0, 1, x, baseline)
            font = _font_from_file(doc, family)
            raw_template = pdfium_c.FPDFPageObj_CreateTextObj(doc, font, size)
            if not raw_template:
                raise RuntimeError("Не удалось создать шаблон текстового объекта")
            from pypdfium2._helpers.pageobjects import PdfTextObj
            template = PdfTextObj(raw_template, pdf=doc)
            template.set_matrix(matrix)
            line_records = []
            object_records = []
            for row, line in enumerate(lines):
                if not line:
                    continue
                obj = _add_text_run(page, template, line, family, size, color_value,
                                    baseline_offset=row * line_height,
                                    width=layout_width, alignment=alignment_value,
                                    line_direction=-1 if geometry is not None else None)
                created.append(obj)
                line_records.append({"text": line,
                                    "bbox": tuple(float(item) for item in obj.get_bounds())})
                object_records.append(line_records[-1])
            if not line_records:
                raise ValueError("Введите хотя бы один символ в текстовый блок")
            page.gen_content()
            output = io.BytesIO()
            doc.save(output)
            result = _patch_unicode_map(output.getvalue(), family, "".join(lines),
                                        page_index, original_font_keys)
        finally:
            if template is not None:
                template.close()
            page.close()
        ident = uuid.uuid4().hex
        groups_by_page[page_index].append({
            "id": ident, "text": value, "font_name": family, "font_size": size,
            "color": color_value, "alignment": alignment_value, "bold": bool(bold),
            "width": layout_width, "height": layout_height, "lines": line_records,
            "objects": object_records, "line_spacing": line_height,
            "spans": [{"text": value, "font_name": family, "font_size": size,
                       "color": color_value, "bold": bool(bold),
                       "italic": False, "underline": False}],
            "orientation": orientation_value,
        })
        return _write_text_groups(result, page_index, groups_by_page[page_index])
    finally:
        created.clear()
        doc.close()
        owned = getattr(doc, "_pdfdesk_source_fonts", None)
        if owned is not None:
            owned.clear()


def apply_content_edit(pdf_bytes: bytes, page_index: int, block_id: str,
                       operation: str, **kwargs: Any) -> bytes:
    """Apply replace, move, or delete to one selected source text/image object.

    ``replace`` accepts text/font_name/font_size/color/width. ``move`` accepts
    dx/dy in PDF points. ``delete`` removes the source object.
    """
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        unicode_mappings: dict[str, str] = {}
        original_font_keys = _font_resource_keys(pdf_bytes, page_index)
        visible = inspect_content(pdf_bytes, page_index)
        groups_by_page = _read_all_text_groups(pdf_bytes)
        had_group_pages = [bool(groups) for groups in groups_by_page]
        updated_groups = [dict(group) for group in groups_by_page[page_index]]
        images_by_page = _read_all_image_records(pdf_bytes)
        updated_images = [dict(record) for record in images_by_page[page_index]]
        requested_block = next((item for item in visible if item.id == block_id), None)
        if requested_block is not None and not requested_block.editable:
            # Reject before any PDFium mutation: shared Form XObject content and
            # invisible OCR layers are reported read-only with guidance.
            raise ValueError(requested_block.reason or
                             "Этот объект нельзя изменить напрямую")
        anchor_id = (requested_block.object_ids[0] if requested_block and
                     requested_block.group_id else block_id)
        page, textpage, obj, block = _find_object(doc, page_index, anchor_id)
        detached_objects = []
        if requested_block is not None:
            block = requested_block
        elif not getattr(block, "editable", True):
            raise ValueError(getattr(block, "reason", "") or
                             "Этот объект нельзя изменить напрямую")
        try:
            requested_ids = kwargs.get("object_ids") or block.object_ids or (block_id,)
            expanded = []
            by_id = {item.id: item for item in visible}
            for ident in requested_ids:
                member = by_id.get(ident)
                expanded.extend(member.object_ids if member and member.group_id else (ident,))
            ids = tuple(dict.fromkeys(expanded))
            selected = []
            for index, candidate in enumerate(_objects(page, textpage)):
                described = _describe(page_index, index, candidate, textpage)
                if described is not None and described.id in ids:
                    selected.append(candidate)
            if len(selected) != len(ids):
                raise ValueError("Часть выбранных объектов устарела; обновите список")
            # PDFium requires text-page handles to be closed before source text
            # objects are removed or re-written.
            textpage.close()
            textpage = None
            op = operation.lower().strip()
            # Underline strokes of a PdfDesk paragraph are page path objects that
            # must travel with their runs (delete/move/replace).
            underline_objects = (_find_underline_objects(page, block.underlines)
                                 if block.kind == "text" else [])
            if op == "delete":
                for candidate in selected + underline_objects:
                    page.remove_obj(candidate)
                    detached_objects.append(candidate)
                if block.group_id:
                    if block.kind == "text":
                        updated_groups = [group for group in updated_groups
                                          if group["id"] != block.group_id]
                    else:
                        updated_images = [record for record in updated_images
                                          if record["id"] != block.group_id]
            elif op == "move":
                dx, dy = float(kwargs.get("dx", 0)), float(kwargs.get("dy", 0))
                if abs(dx) > 100000 or abs(dy) > 100000:
                    raise ValueError("Слишком большое смещение")
                for candidate in selected + underline_objects:
                    candidate.set_matrix(candidate.get_matrix().translate(dx, dy))
                if block.group_id:
                    if block.kind == "text":
                        moved_group = None
                        for group in updated_groups:
                            if group["id"] == block.group_id:
                                for line in group["lines"]:
                                    line["bbox"] = (line["bbox"][0] + dx, line["bbox"][1] + dy,
                                                    line["bbox"][2] + dx, line["bbox"][3] + dy)
                                for item in group.get("objects", ()):
                                    item["bbox"] = (item["bbox"][0] + dx, item["bbox"][1] + dy,
                                                    item["bbox"][2] + dx, item["bbox"][3] + dy)
                                for underline in group.get("underlines", ()):
                                    x1, y1, x2, y2 = underline["coords"]
                                    underline["coords"] = (x1 + dx, y1 + dy, x2 + dx, y2 + dy)
                                moved_group = group
                                break
                        if moved_group is not None and not underline_objects:
                            # Legacy documents kept underlines in separate
                            # content streams; recreate them as path objects so
                            # the stroke follows the moved paragraph and keeps
                            # its draw position.
                            anchor = (_page_object_index(page, selected[-1])
                                      if selected else None)
                            cursor = None if anchor is None else anchor + 1
                            for record in moved_group.get("underlines", ()):
                                _add_underline_object(page, record["coords"],
                                                      record["width"], record["color"],
                                                      cursor)
                                if cursor is not None:
                                    cursor += 1
                    elif selected:
                        _store_image_record(updated_images, block, selected[0])
                elif block.kind == "image" and selected:
                    _store_image_record(updated_images, block, selected[0])
            elif op == "replace":
                if block.kind == "image":
                    width = float(kwargs.get("width") or (block.bbox[2] - block.bbox[0]))
                    height = float(kwargs.get("height") or (block.bbox[3] - block.bbox[1]))
                    if width <= 0 or height <= 0:
                        raise ValueError("Размеры изображения должны быть положительными")
                    old_width = max(0.001, block.bbox[2] - block.bbox[0])
                    old_height = max(0.001, block.bbox[3] - block.bbox[1])
                    sx, sy = width / old_width, height / old_height
                    if not kwargs.get("allow_distortion", False):
                        factor = sx if abs(sx - 1) >= abs(sy - 1) else sy
                        sx = sy = factor
                    matrix = obj.get_matrix()
                    obj.set_matrix(PdfMatrix(matrix.a * sx, matrix.b * sx,
                                             matrix.c * sy, matrix.d * sy,
                                             matrix.e, matrix.f))
                    if kwargs.get("image_data") is not None:
                        _set_image_data(obj, page, kwargs["image_data"])
                    _store_image_record(updated_images, block, obj)
                else:
                    value = str(kwargs.get("text", block.text))
                    base_family = _subset_replacement_font(block.font_name)
                    defaults = {
                        "font_name": base_family,
                        "font_size": float(block.font_size or 12.0),
                        "color": block.color,
                        "bold": bool(block.bold), "italic": False, "underline": False,
                    }
                    explicit_style = any(key in kwargs for key in
                                         ("font_name", "font_size", "color", "bold", "italic", "underline"))
                    if kwargs.get("rich_spans") is not None:
                        raw_spans = list(kwargs["rich_spans"])
                        if "text" not in kwargs:
                            value = "".join(str(span.get("text", "")) for span in raw_spans)
                        spans = _normalize_rich_spans(value, raw_spans, defaults)
                    else:
                        old_spans = list(block.rich_spans) or [{"text": block.text, **defaults}]
                        spans = _remap_rich_spans(block.text, old_spans, value, defaults)
                        if explicit_style:
                            style_overrides = {
                                "font_name": str(kwargs.get("font_name", defaults["font_name"])).strip(),
                                "font_size": float(kwargs.get("font_size", defaults["font_size"])),
                                "color": kwargs.get("color", defaults["color"]),
                                "bold": bool(kwargs.get("bold", defaults["bold"])),
                                "italic": bool(kwargs.get("italic", defaults["italic"])),
                                "underline": bool(kwargs.get("underline", defaults["underline"])),
                            }
                            for span in spans:
                                span.update({key: _validate_color(value) if key == "color" else value
                                             for key, value in style_overrides.items()})
                            spans = _normalize_rich_spans(value, spans, defaults)
                    if not spans:
                        raise ValueError("Введите хотя бы один символ в текстовом блоке")
                    for span in spans:
                        _styled_font_name(span["font_name"], span["bold"], span["italic"])
                    fname = spans[0]["font_name"]
                    size = spans[0]["font_size"]
                    color = spans[0]["color"]
                    bold = spans[0]["bold"]
                    default_width = (block.block_width if block.group_id and block.block_width > 0
                                     else block.bbox[2] - block.bbox[0])
                    width = float(kwargs.get("width") or max(1.0, default_width))
                    if not 0 < width <= 100000:
                        raise ValueError("Ширина должна быть от 0 до 100000 пунктов")
                    alignment = str(kwargs.get("alignment", "left")).lower()
                    if alignment not in ("left", "center", "right"):
                        raise ValueError("Выравнивание должно быть left, center или right")
                    lines = _wrap_rich_spans(spans, width)
                    widest_line = max((_rich_line_width(line) for line in lines), default=0.0)
                    if widest_line > width:
                        raise ValueError(
                            "Ширины текстового блока недостаточно для самого длинного слова; "
                            "увеличьте ширину блока"
                        )
                    requested_height = kwargs.get("height")
                    if requested_height is None and block.group_id and block.block_height > 0:
                        requested_height = block.block_height
                    line_height = float(kwargs.get("line_height") or block.line_spacing or
                                        max(span["font_size"] for span in spans) * 1.2)
                    if not 0 < line_height <= 5000:
                        raise ValueError("Высота строки недопустима")
                    template = selected[0]
                    # Every new object takes over the paint slot of the object it
                    # replaces; foreign artwork between the old parts stays
                    # between the corresponding new parts (or the edit is
                    # refused when the new line structure cannot map onto them).
                    insert_positions = _replacement_paint_slots(
                        page,
                        next((item for item in updated_groups
                              if item.get("id") == block.group_id), None),
                        selected, underline_objects, spans, width)
                    for candidate in selected + underline_objects:
                        page.remove_obj(candidate)
                        detached_objects.append(candidate)
                    (lines, line_records, object_records, created, unicode_mappings,
                     content_height, underlines) = _add_rich_paragraph(
                        page, template, spans, width, alignment, block.orientation, line_height,
                        insert_positions=insert_positions)
                    if not object_records:
                        raise ValueError("Введите хотя бы один символ в текстовый блок")
                    group_id = block.group_id or hashlib.sha256(
                        (str(page_index) + ":" + ":".join(ids)).encode("utf-8")).hexdigest()[:24]
                    updated_groups = [group for group in updated_groups
                                      if group["id"] != group_id]
                    updated_groups.append({
                        "id": group_id, "text": value, "font_name": fname,
                        "font_size": size, "color": color, "alignment": alignment,
                        "bold": bold, "width": width,
                        "height": max(float(kwargs.get("height") or block.block_height or 0),
                                      content_height),
                        "line_spacing": line_height, "spans": spans,
                        "objects": object_records,
                        "underlines": underlines,
                        "orientation": block.orientation,
                        "lines": line_records,
                    })
                    had_group_pages[page_index] = True
            else:
                raise ValueError("Операция должна быть replace, move или delete")
            page.gen_content()
            output = io.BytesIO()
            doc.save(output)
            result = output.getvalue()
            # A rebuilt font resource can be shared by several PdfDesk
            # paragraphs of the same face, so every map gets the glyphs of all
            # PdfDesk text on this page written with that face (extra entries for
            # unused glyph ids are inert). Without this, an earlier replacement
            # would lose its searchable text as soon as another edit recreated
            # the shared resource.
            mapped_faces: dict[str, str] = {}
            for group in updated_groups:
                for span in group.get("spans", ()) or ():
                    try:
                        face = _styled_font_name(str(span.get("font_name", "")),
                                                 bool(span.get("bold", False)),
                                                 bool(span.get("italic", False)))
                    except Exception:  # noqa: BLE001 - keep the stored face name
                        face = str(span.get("font_name", "")) or "Arial"
                    mapped_faces[face] = mapped_faces.get(face, "") + str(span.get("text", ""))
            for face, mapped_text in unicode_mappings.items():
                mapped_faces[face] = mapped_faces.get(face, "") + mapped_text
            for font_name, mapped_text in mapped_faces.items():
                result = _patch_unicode_map(result, font_name, mapped_text, page_index,
                                            original_font_keys, target_font_name=font_name,
                                            include_pdfdesk_existing=True)
            groups_by_page[page_index] = updated_groups
            for target_page, groups in enumerate(groups_by_page):
                if had_group_pages[target_page]:
                    result = _write_text_groups(result, target_page, groups)
            if block.kind == "image":
                result = _write_image_records(result, page_index, updated_images)
            return result
        finally:
            if textpage is not None:
                textpage.close()
            # remove_obj transfers ownership to a standalone wrapper. Destroy
            # those old objects while their document is still alive, including
            # partial removals when replacement fails.
            for detached in detached_objects:
                if detached.raw and detached.page is None:
                    detached.close()
            detached_objects.clear()
            obj = None
            candidate = None
            described = None
            template = None
            created = []
            selected = []
            all_objects = []
            page.close()
    finally:
        doc.close()
        owned_fonts = getattr(doc, "_pdfdesk_source_fonts", None)
        if owned_fonts is not None:
            owned_fonts.clear()
