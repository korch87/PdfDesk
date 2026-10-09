"""Copy-on-write edits of individual nested Form XObject invocations.

This is a research prototype for moving or deleting one painted Form instance.
It never changes an original shared Form in place: each edge on the selected
``Do`` path is rebound to a private clone. The form's existing BBox, Matrix,
resources, content and clipping behavior stay intact. Coordinates passed to
``translate_form_occurrence`` are in the immediate parent form's user space
(or page user space for a top-level invocation).

The nested text editor uses the isolated-page and stream replacement helpers
below to edit supported leaf content without changing a shared source Form.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
from typing import Any, Literal

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ContentStream, DecodedStreamObject,
                           DictionaryObject, FloatObject, NameObject)


class NestedContentError(ValueError):
    """An unsupported or stale Form invocation path was supplied."""


@dataclass(frozen=True)
class DoStep:
    """One ``/Name Do`` operation in a page or Form content stream."""

    operation_index: int
    resource_name: str


@dataclass(frozen=True)
class FormOccurrence:
    """A Form invocation, including its exact path from the page stream."""

    page_index: int
    path: tuple[DoStep, ...]
    bbox: tuple[float, float, float, float]
    matrix: tuple[float, float, float, float, float, float]
    object_id: tuple[int, int] | None
    page_bbox: tuple[float, float, float, float]
    page_matrix: tuple[float, float, float, float, float, float]
    identity: str


def _resolved(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _resources(container: Any, *, page: bool) -> DictionaryObject:
    resources = container.get("/Resources")
    if resources is None and page:
        parent = _resolved(container.get("/Parent"))
        depth = 0
        while parent is not None and depth < 64 and resources is None:
            resources = parent.get("/Resources")
            parent = _resolved(parent.get("/Parent"))
            depth += 1
    resources = _resolved(resources)
    if not isinstance(resources, DictionaryObject):
        raise NestedContentError("У страницы или Form XObject нет корректных /Resources.")
    return resources


def _form_xobjects(resources: DictionaryObject) -> DictionaryObject:
    raw = _resolved(resources.get("/XObject"))
    return raw if isinstance(raw, DictionaryObject) else DictionaryObject()


def _is_form(value: Any) -> bool:
    obj = _resolved(value)
    return isinstance(obj, DictionaryObject) and str(obj.get("/Subtype", "")) == "/Form"


def _object_id(value: Any) -> tuple[int, int] | None:
    ref = getattr(value, "indirect_reference", None)
    if ref is None:
        ref = value if hasattr(value, "idnum") and hasattr(value, "generation") else None
    if ref is None:
        return None
    return (int(ref.idnum), int(ref.generation))


def _form_metadata(form_ref: Any) -> tuple[tuple[float, float, float, float],
                                             tuple[float, float, float, float, float, float]]:
    form = _resolved(form_ref)
    raw_bbox = form.get("/BBox")
    if raw_bbox is None or len(raw_bbox) != 4:
        raise NestedContentError("У Form XObject нет корректного /BBox.")
    bbox = tuple(float(v) for v in raw_bbox)
    raw_matrix = form.get("/Matrix", (1, 0, 0, 1, 0, 0))
    if len(raw_matrix) != 6:
        raise NestedContentError("У Form XObject матрица должна содержать шесть чисел.")
    matrix = tuple(float(v) for v in raw_matrix)
    return bbox, matrix  # type: ignore[return-value]


_IDENTITY_MATRIX = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _compose(outer: tuple[float, ...], inner: tuple[float, ...]
             ) -> tuple[float, float, float, float, float, float]:
    """Compose affine matrices so the inner mapping is applied first."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _transform_bbox(matrix: tuple[float, ...], bbox: tuple[float, ...]
                    ) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = matrix
    x0, y0, x1, y1 = bbox
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    xs = [a * x + c * y + e for x, y in corners]
    ys = [b * x + d * y + f for x, y in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _occurrence_identity(page_index: int, path: tuple[DoStep, ...],
                        object_id: tuple[int, int] | None,
                        bbox: tuple[float, ...],
                        matrix: tuple[float, ...]) -> str:
    material = repr((page_index,
                     tuple((step.operation_index, step.resource_name) for step in path),
                     object_id,
                     tuple(round(value, 5) for value in bbox),
                     tuple(round(value, 5) for value in matrix))).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def discover_form_occurrences(pdf_bytes: bytes, page_index: int) -> list[FormOccurrence]:
    """Return every Form ``Do`` path on a page, including nested invocations.

    ``DoStep.operation_index`` indexes pypdf's parsed, concatenated content
    stream operations. This distinguishes two uses of the same resource name
    in one stream and lets callers keep selection identity separate from the
    shared Form object identity.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_index < 0 or page_index >= len(reader.pages):
        raise IndexError("Номер страницы вне диапазона.")
    page = reader.pages[page_index]
    result: list[FormOccurrence] = []

    def walk(owner: Any, *, is_page: bool, path: tuple[DoStep, ...],
             ancestry: frozenset[tuple[int, int]], depth: int,
             owner_to_page: tuple[float, ...]) -> None:
        if depth > 48:
            raise NestedContentError("Вложенность Form XObject превышает безопасный предел.")
        resources = _resources(owner, page=is_page)
        xobjects = _form_xobjects(resources)
        stream = owner.get_contents() if is_page else owner
        content = ContentStream(stream, reader)
        ctm = _IDENTITY_MATRIX
        graphics_stack: list[tuple[float, ...]] = []
        for op_index, (operands, operator) in enumerate(content.operations):
            if operator == b"q":
                graphics_stack.append(ctm)
                continue
            if operator == b"Q":
                if graphics_stack:
                    ctm = graphics_stack.pop()
                continue
            if operator == b"cm" and len(operands) == 6:
                try:
                    local = tuple(float(value) for value in operands)
                    ctm = _compose(ctm, local)
                except (TypeError, ValueError):
                    continue
                continue
            if operator != b"Do" or not operands:
                continue
            name = str(operands[0])
            form_ref = xobjects.get(name)
            if form_ref is None or not _is_form(form_ref):
                continue
            form = _resolved(form_ref)
            step = DoStep(op_index, name)
            current_path = path + (step,)
            bbox, matrix = _form_metadata(form_ref)
            page_matrix = _compose(owner_to_page, _compose(ctm, matrix))
            page_bbox = _transform_bbox(page_matrix, bbox)
            ident = _object_id(form_ref)
            occurrence_id = _occurrence_identity(page_index, current_path, ident,
                                                  bbox, page_matrix)
            result.append(FormOccurrence(page_index, current_path, bbox, matrix,
                                         ident, page_bbox, page_matrix, occurrence_id))
            ident = _object_id(form_ref)
            if ident is not None and ident in ancestry:
                continue
            next_ancestry = ancestry | ({ident} if ident is not None else set())
            walk(form, is_page=False, path=current_path,
                 ancestry=frozenset(next_ancestry), depth=depth + 1,
                 owner_to_page=page_matrix)

    walk(page, is_page=True, path=(), ancestry=frozenset(), depth=0,
         owner_to_page=_IDENTITY_MATRIX)
    return result


def _clone_form(form_ref: Any, writer: PdfWriter) -> Any:
    source = _resolved(form_ref)
    if not isinstance(source, DictionaryObject) or not hasattr(source, "get_data"):
        raise NestedContentError("Выбранный XObject не является корректным потоком Form.")
    clone = DecodedStreamObject()
    for key, value in source.items():
        if str(key) in ("/Length", "/Filter", "/DecodeParms"):
            continue
        clone[key] = value
    clone.set_data(source.get_data())
    return writer._add_object(clone)


def _clone_resources(source: DictionaryObject, writer: PdfWriter) -> tuple[
        DictionaryObject, DictionaryObject]:
    """Shallow-copy resource categories and privately copy the /XObject map."""
    copied = DictionaryObject()
    for key, value in source.items():
        if str(key) != "/XObject":
            copied[key] = value
    xobjects = DictionaryObject()
    for key, value in _form_xobjects(source).items():
        xobjects[key] = value
    copied[NameObject("/XObject")] = xobjects
    return copied, xobjects


def _unused_name(xobjects: DictionaryObject) -> NameObject:
    index = 1
    while True:
        name = NameObject("/PdfDeskNested%d" % index)
        if name not in xobjects:
            return name
        index += 1


def _set_content(owner: Any, content: ContentStream, resources: DictionaryObject,
                 writer: PdfWriter, *, page: bool) -> None:
    content.operations = list(content.operations)
    if page:
        owner.replace_contents(content)
        owner[NameObject("/Resources")] = writer._add_object(resources)
    else:
        owner[NameObject("/Resources")] = writer._add_object(resources)
        owner.set_data(content.get_data())


def _matrix_with_translation(form_ref: Any, dx: float, dy: float) -> ArrayObject:
    _bbox, matrix = _form_metadata(form_ref)
    a, b, c, d, e, f = matrix
    return ArrayObject([FloatObject(a), FloatObject(b), FloatObject(c), FloatObject(d),
                        FloatObject(e + dx), FloatObject(f + dy)])


def _matrix_with_resize(form_ref: Any, sx: float, sy: float,
                        origin: tuple[float, float]) -> ArrayObject:
    _bbox, matrix = _form_metadata(form_ref)
    a, b, c, d, e, f = matrix
    ox, oy = origin
    return ArrayObject([FloatObject(sx * a), FloatObject(sy * b),
                        FloatObject(sx * c), FloatObject(sy * d),
                        FloatObject(ox + sx * (e - ox)),
                        FloatObject(oy + sy * (f - oy))])


def _edit_invocation(pdf_bytes: bytes, page_index: int, path: tuple[DoStep, ...], *,
                     action: Literal["delete", "translate", "resize"], dx: float = 0.0,
                     dy: float = 0.0, sx: float = 1.0, sy: float = 1.0,
                     origin: tuple[float, float] = (0.0, 0.0)) -> bytes:
    if not path:
        raise NestedContentError("Путь к Form XObject пуст.")
    if not math.isfinite(dx) or not math.isfinite(dy):
        raise ValueError("Смещение должно быть конечным числом.")
    if action == "translate" and dx == 0 and dy == 0:
        return bytes(pdf_bytes)
    if action == "resize":
        if not math.isfinite(sx) or not math.isfinite(sy) or sx <= 0 or sy <= 0:
            raise ValueError("Коэффициенты изменения размера должны быть положительными числами.")
        if not all(math.isfinite(value) for value in origin):
            raise ValueError("Начало изменения размера должно быть конечными координатами.")

    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_index < 0 or page_index >= len(reader.pages):
        raise IndexError("Номер страницы вне диапазона.")
    writer = PdfWriter(clone_from=reader)
    page = writer.pages[page_index]

    def edit_owner(owner: Any, step_index: int, *, is_page: bool) -> Any:
        working = owner if is_page else _clone_form(owner, writer)
        working_obj = working if is_page else _resolved(working)
        source_resources = _resources(working_obj, page=is_page)
        resources, xobjects = _clone_resources(source_resources, writer)
        source_content = working_obj.get_contents() if is_page else working_obj
        content = ContentStream(source_content, writer)
        operations = list(content.operations)
        step = path[step_index]
        if not (0 <= step.operation_index < len(operations)):
            raise NestedContentError("Путь устарел: номер Do-операции не найден.")
        operands, operator = operations[step.operation_index]
        if operator != b"Do" or not operands or str(operands[0]) != step.resource_name:
            raise NestedContentError("Путь устарел: выбранная Do-операция изменилась.")
        child_ref = _form_xobjects(source_resources).get(step.resource_name)
        if child_ref is None or not _is_form(child_ref):
            raise NestedContentError("Путь устарел: ресурс больше не является Form XObject.")

        if step_index == len(path) - 1 and action == "delete":
            operations.pop(step.operation_index)
        else:
            if step_index == len(path) - 1:
                child_clone = _clone_form(child_ref, writer)
                child = _resolved(child_clone)
                matrix = (_matrix_with_translation(child_ref, dx, dy)
                          if action == "translate"
                          else _matrix_with_resize(child_ref, sx, sy, origin))
                child[NameObject("/Matrix")] = matrix
                rebound = child_clone
            else:
                rebound = edit_owner(child_ref, step_index + 1, is_page=False)
            new_name = _unused_name(xobjects)
            xobjects[new_name] = rebound
            operations[step.operation_index] = (ArrayObject([new_name]), b"Do")

        content.operations = operations
        _set_content(working_obj, content, resources, writer, page=is_page)
        return working if is_page else working

    edit_owner(page, 0, is_page=True)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    if not data.startswith(b"%PDF"):
        raise RuntimeError("Не удалось записать PDF после правки Form XObject.")
    return data


def delete_form_occurrence(pdf_bytes: bytes, occurrence: FormOccurrence) -> bytes:
    """Remove one Form ``Do`` occurrence by copy-on-write cloning its path."""
    return _edit_invocation(bytes(pdf_bytes), occurrence.page_index, occurrence.path,
                            action="delete")


def translate_form_occurrence(pdf_bytes: bytes, occurrence: FormOccurrence,
                              dx: float, dy: float) -> bytes:
    """Translate one Form occurrence in its immediate parent's user space.

    Existing form matrices, BBox clipping and ancestor transforms are retained.
    This small prototype does not convert page-space drag deltas through a
    rotated/scaled ancestor; callers must express the delta in the parent
    coordinate system identified by the last Do step.
    """
    return _edit_invocation(bytes(pdf_bytes), occurrence.page_index, occurrence.path,
                            action="translate", dx=float(dx), dy=float(dy))


def resize_form_occurrence(pdf_bytes: bytes, occurrence: FormOccurrence, sx: float, sy: float,
                           *, origin: tuple[float, float] = (0.0, 0.0)) -> bytes:
    """Resize one Form occurrence in its immediate parent's coordinate system.

    Scaling is applied to the Form's existing Matrix, so its BBox clipping,
    content stream and ancestor transforms remain intact. ``origin`` is a
    parent-space point; the default scales relative to that space's origin.
    """
    return _edit_invocation(bytes(pdf_bytes), occurrence.page_index, occurrence.path,
                            action="resize", sx=float(sx), sy=float(sy),
                            origin=(float(origin[0]), float(origin[1])))


def page_delta_to_form_parent(occurrence: FormOccurrence,
                              dx: float, dy: float) -> tuple[float, float]:
    """Convert a page-user-space drag delta to the Form Matrix's input space."""
    a, b, c, d, _e, _f = occurrence.page_matrix
    la, lb, lc, ld, _le, _lf = occurrence.matrix
    # page_matrix = parent_to_page * form_matrix. Recover the parent mapping,
    # then invert only its linear part because this is a vector delta.
    det_leaf = la * ld - lb * lc
    if abs(det_leaf) <= 1e-12:
        raise NestedContentError("Невозможно переместить Form с вырожденной матрицей.")
    inv_leaf = (ld / det_leaf, -lb / det_leaf, -lc / det_leaf,
                la / det_leaf, 0.0, 0.0)
    parent = _compose(occurrence.page_matrix, inv_leaf)
    pa, pb, pc, pd, _pe, _pf = parent
    det = pa * pd - pb * pc
    if abs(det) <= 1e-12:
        raise NestedContentError("Невозможно переместить Form через вырожденный transform.")
    return ((pd * dx - pc * dy) / det, (-pb * dx + pa * dy) / det)


def resolve_form_occurrence(pdf_bytes: bytes, occurrence: FormOccurrence):
    """Resolve an exact, current invocation; return its reader and leaf stream.

    The reader must remain alive while its returned objects are being used.
    Identity validation precedes all cloning and mutation.
    """
    current = next((item for item in discover_form_occurrences(pdf_bytes, occurrence.page_index)
                    if item.path == occurrence.path), None)
    if current != occurrence:
        raise NestedContentError("Выбранный экземпляр Form устарел. Выберите его заново.")
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    owner = reader.pages[occurrence.page_index]
    for index, step in enumerate(occurrence.path):
        owner = _resolved(_form_xobjects(_resources(owner, page=index == 0))[step.resource_name])
    return reader, owner


def extract_form_page(pdf_bytes: bytes, occurrence: FormOccurrence) -> bytes:
    """Expose leaf content as native page objects in unchanged leaf coordinates.

    Form BBox / Matrix and the ancestor transforms are deliberately *not*
    painted into this page: the caller maps local bounds through page_matrix.
    Copy-on-write replacement retains those original Form attributes.
    """
    reader, leaf = resolve_form_occurrence(pdf_bytes, occurrence)
    writer = PdfWriter()
    x0, y0, x1, y1 = occurrence.bbox
    page = writer.add_blank_page(width=x1 - x0, height=y1 - y0)
    # PDFium's native object bounds retain original user coordinates even for
    # nonzero page boxes. Use the exact Form clip, including negative origins,
    # so preservation rendering observes every visible part of the leaf.
    page[NameObject("/MediaBox")] = ArrayObject([FloatObject(v) for v in occurrence.bbox])
    page[NameObject("/Resources")] = _resources(leaf, page=False).clone(writer)
    stream = DecodedStreamObject()
    stream.set_data(leaf.get_data())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if "/PieceInfo" in leaf:
        page[NameObject("/PieceInfo")] = leaf["/PieceInfo"].clone(writer)
    catalog = _resolved(reader.trailer["/Root"])
    if "/OCProperties" in catalog:
        writer._root_object[NameObject("/OCProperties")] = catalog["/OCProperties"].clone(writer)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def replace_form_occurrence_content(pdf_bytes: bytes, occurrence: FormOccurrence,
                                    edited_page_bytes: bytes) -> bytes:
    """Rebind one path to a cloned leaf containing edited native page content.

    Only content, resources and PdfDesk editing metadata are replaced. All
    original Form attributes (including BBox, Matrix, OC and unknown keys),
    siblings, other pages, annotations and the original catalog are retained.
    The higher-level editor validates support before calling this helper.
    """
    resolve_form_occurrence(pdf_bytes, occurrence)
    edited_reader = PdfReader(io.BytesIO(edited_page_bytes), strict=False)
    if len(edited_reader.pages) != 1:
        raise NestedContentError("Ожидалась одна изолированная страница Form.")
    edited = edited_reader.pages[0]
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    writer = PdfWriter(clone_from=reader)

    def rebind(owner, depth, *, page):
        working = owner if page else _clone_form(owner, writer)
        obj = _resolved(working)
        resources, xobjects = _clone_resources(_resources(obj, page=page), writer)
        content = ContentStream(obj.get_contents() if page else obj, writer)
        step = occurrence.path[depth]
        child = xobjects[step.resource_name]
        if depth + 1 == len(occurrence.path):
            rebound = _clone_form(child, writer)
            leaf = _resolved(rebound)
            leaf.set_data(ContentStream(edited.get_contents(), edited_reader).get_data())
            replacement = _resources(edited, page=True).clone(writer)
            # PDFium may omit unknown or unused resource entries. Retain them,
            # including their original glyph maps and private metadata graphs.
            original = _resources(leaf, page=False)
            for category, value in original.items():
                if category not in replacement:
                    replacement[category] = value
                elif isinstance(_resolved(value), DictionaryObject):
                    target = _resolved(replacement[category])
                    if isinstance(target, DictionaryObject):
                        for name, resource in _resolved(value).items():
                            if name not in target:
                                target[name] = resource
            leaf[NameObject("/Resources")] = writer._add_object(replacement)
            if "/PieceInfo" in edited:
                # Only the application's private editing metadata is adopted;
                # foreign PieceInfo entries remain on the original clone.
                original_piece = _resolved(leaf.get("/PieceInfo", DictionaryObject()))
                copied_piece = DictionaryObject(dict(original_piece))
                edited_piece = _resolved(edited["/PieceInfo"])
                if "/PdfDesk" in edited_piece:
                    def merge_private_metadata(original, changed):
                        original, changed = _resolved(original), _resolved(changed)
                        if isinstance(original, DictionaryObject) and isinstance(changed, DictionaryObject):
                            merged = DictionaryObject(dict(original))
                            for key, value in changed.items():
                                merged[key] = merge_private_metadata(original.get(key), value)
                            return merged
                        return changed.clone(writer)
                    copied_piece[NameObject("/PdfDesk")] = merge_private_metadata(
                        original_piece.get("/PdfDesk"), edited_piece["/PdfDesk"])
                leaf[NameObject("/PieceInfo")] = writer._add_object(copied_piece)
        else:
            rebound = rebind(child, depth + 1, page=False)
        name = _unused_name(xobjects)
        xobjects[name] = rebound
        operations = list(content.operations)
        operations[step.operation_index] = (ArrayObject([name]), b"Do")
        content.operations = operations
        _set_content(obj, content, resources, writer, page=page)
        return working

    rebind(writer.pages[occurrence.page_index], 0, page=True)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
