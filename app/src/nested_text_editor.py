"""Bounded native text/image editing inside one shared Form invocation.

The leaf stream is edited on an independent native page and rebound through
private ancestor clones. No source page is passed through PDFium's writer.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import io
import math
from typing import Any

from pypdf.generic import ContentStream

from .content_editor import (ContentBlock, NESTED_OBJECT_REASON,
                             apply_content_edit, inspect_content)
from .nested_content import (FormOccurrence, NestedContentError, _resolved,
                             _resources, _form_xobjects, _transform_bbox,
                             extract_form_page, replace_form_occurrence_content,
                             resolve_form_occurrence)


@dataclass(frozen=True)
class NestedContentBlock:
    """One exact document revision + invocation + leaf-local native block.

    ``bbox`` is a page-user-space hull for selection. All editor properties,
    including rich spans, sizes and move deltas, use ``local_block`` coordinates.
    """

    id: str
    occurrence_identity: str
    revision: str
    occurrence: FormOccurrence
    local_block: ContentBlock
    local_bbox: tuple[float, float, float, float]
    bbox: tuple[float, float, float, float]

    @property
    def editable(self) -> bool:
        return self.local_block.editable

    @property
    def reason(self) -> str:
        return self.local_block.reason


# PDFium may silently discard extension operators, marked-content properties,
# unusual text modes or inline-image metadata while regenerating a page. Keep
# such leaves read-only instead of claiming arbitrary stream preservation.
_SUPPORTED_OPERATORS = frozenset(
    b"q Q cm w J j M d ri i m l c v y h re S s f F f* B B* b b* n "
    b"G g RG rg K k CS cs SC SCN sc scn sh Do BT ET Tc Tw Tz TL Tf "
    b"Tr Ts Td TD Tm T* Tj TJ ' \" gs".split())
_UNSUPPORTED_REASON = (
    "Содержимое Form использует неподдерживаемые операторы, маску, "
    "прозрачность или маркированное содержимое. Правка отменена для сохранения PDF."
)


def _inherited_state_reason(operations) -> str:
    """Refuse a nondefault inherited paint/text state on the selected edge.

    An isolated page has the PDF default state. Form invocations inherit the
    caller's colors, stroke settings and text spacing, so silently treating
    those as black/default would change untouched native leaf objects.
    """
    state = {}
    stack = []
    defaults = {b"w": (1.0,), b"J": (0.0,), b"j": (0.0,), b"M": (10.0,),
                b"Tc": (0.0,), b"Tw": (0.0,), b"Tz": (100.0,), b"Ts": (0.0,),
                b"Tr": (0.0,)}
    for operands, operator in operations:
        if operator == b"q":
            stack.append(dict(state))
        elif operator == b"Q":
            if stack:
                state = stack.pop()
        elif operator in (b"g", b"rg", b"k", b"G", b"RG", b"K"):
            values = tuple(float(v) for v in operands)
            black = ((0.0,) if operator in (b"g", b"G") else
                     (0.0, 0.0, 0.0) if operator in (b"rg", b"RG") else
                     (0.0, 0.0, 0.0, 1.0))
            state['stroke' if operator.isupper() else 'fill'] = values != black
        elif operator in defaults:
            state[operator] = tuple(float(v) for v in operands) != defaults[operator]
        elif operator == b"d":
            state[operator] = not (len(operands) == 2 and not operands[0] and float(operands[1]) == 0)
        elif operator in (b"CS", b"cs", b"SC", b"SCN", b"sc", b"scn", b"gs", b"BDC", b"BMC"):
            state[operator] = True
    return _UNSUPPORTED_REASON if any(state.values()) else ""


def _support_reason(pdf_bytes: bytes, occurrence: FormOccurrence) -> str:
    reader, leaf = resolve_form_occurrence(pdf_bytes, occurrence)
    if not all(math.isfinite(v) for v in occurrence.page_matrix + occurrence.bbox):
        return _UNSUPPORTED_REASON
    a, b, c, d, _e, _f = occurrence.page_matrix
    if abs(a * d - b * c) <= 1e-12:
        return "Form имеет вырожденную матрицу; правка недоступна."
    owner = reader.pages[occurrence.page_index]
    for depth, step in enumerate(occurrence.path):
        # Unsupported graphics states/marked content on ancestors cannot be
        # recreated faithfully on the isolated page. Existing clipping outside
        # the leaf remains on the parent and is preserved by path cloning.
        stream = owner.get_contents() if depth == 0 else owner
        operations = ContentStream(stream, reader).operations
        reason = _inherited_state_reason(operations[:step.operation_index])
        if reason:
            return reason
        owner = _resolved(_form_xobjects(_resources(owner, page=depth == 0))[step.resource_name])
        if owner.get("/Group") is not None or owner.get("/SMask") is not None:
            return _UNSUPPORTED_REASON
    for operands, operator in ContentStream(leaf, reader).operations:
        if operator not in _SUPPORTED_OPERATORS:
            return _UNSUPPORTED_REASON
        if operator == b"Tr" and (not operands or int(operands[0]) != 0):
            return _UNSUPPORTED_REASON
        if operator == b"gs":
            states = _resolved(_resources(leaf, page=False).get("/ExtGState", {}))
            state = _resolved(states.get(str(operands[0]), {})) if operands else {}
            # PDFium writes an exact opaque Normal state after native edits.
            # Any mask, transfer function or nondefault alpha remains guarded.
            if (not state or set(state) - {"/Type", "/BM", "/CA", "/ca"}
                    or str(state.get("/BM", "/Normal")) != "/Normal"
                    or float(state.get("/CA", 1)) != 1
                    or float(state.get("/ca", 1)) != 1):
                return _UNSUPPORTED_REASON
    return ""


def inspect_nested_content(pdf_bytes: bytes, occurrence: FormOccurrence) -> list[NestedContentBlock]:
    """List leaf blocks with local properties and page selection bounds.

    Objects inside deeper Forms keep the native editor's read-only explanation;
    selecting that deeper invocation separately can expose its own leaf blocks.
    """
    data = bytes(pdf_bytes)
    reason = _support_reason(data, occurrence)
    isolated = extract_form_page(data, occurrence)
    revision = hashlib.sha256(data).hexdigest()
    result = []
    for block in inspect_content(isolated, 0):
        if reason:
            block = replace(block, editable=False, reason=reason)
        elif not block.editable and block.reason == NESTED_OBJECT_REASON:
            block = replace(block, reason="Объект находится в более глубокой группе. "
                            "Вернитесь к группе PDF и выберите вложенный экземпляр отдельно.")
        material = (revision + ":" + occurrence.identity + ":" + block.id).encode("ascii")
        ident = "nested:" + hashlib.sha256(material).hexdigest()[:24]
        result.append(NestedContentBlock(ident, occurrence.identity, revision, occurrence,
                                        block, block.bbox,
                                        _transform_bbox(occurrence.page_matrix, block.bbox)))
    return result


def _semantic_block(block: ContentBlock):
    return (block.kind, block.text, block.font_name, round(block.font_size, 3),
            block.color, tuple(round(value, 3) for value in block.bbox),
            block.editable)


def _verify_untouched_pixels(before: bytes, after: bytes, exempt_boxes) -> None:
    """Reject native regeneration changes outside the edited object's hull."""
    import pypdfium2 as pdfium
    from pypdf import PdfReader
    from PIL import ImageChops, ImageDraw

    def render(data):
        doc = pdfium.PdfDocument(data)
        page = doc[0]
        bitmap = page.render(scale=2, draw_annots=False, may_draw_forms=False)
        try:
            return bitmap.to_pil().convert('RGB').copy()
        finally:
            bitmap.close()
            page.close()
            doc.close()

    original, edited = render(before), render(after)
    if original.size != edited.size:
        raise NestedContentError("Правка изменила геометрию изолированной страницы.")
    difference = ImageChops.difference(original, edited)
    draw = ImageDraw.Draw(difference)
    page = PdfReader(io.BytesIO(before)).pages[0]
    origin_x, origin_y = float(page.mediabox.left), float(page.mediabox.bottom)
    for x0, y0, x1, y1 in exempt_boxes:
        # Antialiasing at the selected glyph edges is part of the intended edit.
        draw.rectangle((math.floor(2 * (x0 - origin_x)) - 2,
                        math.floor(original.height - 2 * (y1 - origin_y)) - 2,
                        math.ceil(2 * (x1 - origin_x)) + 2,
                        math.ceil(original.height - 2 * (y0 - origin_y)) + 2),
                       fill=(0, 0, 0))
    if difference.getbbox() is not None:
        raise NestedContentError("Правка изменила соседнюю графику Form; операция отменена.")


def local_bbox_to_page(occurrence: FormOccurrence, bbox: tuple[float, float, float, float]):
    """Project a leaf-local rectangle to an unrotated page-user-space hull."""
    return _transform_bbox(occurrence.page_matrix, bbox)


def page_delta_to_nested_local(occurrence: FormOccurrence, dx: float, dy: float):
    """Invert all ancestor and leaf transforms for a page-user-space vector."""
    a, b, c, d, _e, _f = occurrence.page_matrix
    det = a * d - b * c
    if abs(det) <= 1e-12 or not all(math.isfinite(v) for v in (dx, dy, det)):
        raise NestedContentError("Невозможно преобразовать координаты Form.")
    return ((d * dx - c * dy) / det, (-b * dx + a * dy) / det)


def nested_inline_support_reason(pdf_bytes: bytes, target: NestedContentBlock) -> str:
    """Validate local glyph axes before embedding a normal Qt text document.

    The proxy handles arbitrary nonsingular ancestor/leaf affines. Individual
    glyph runs must share ordinary upright local axes; a QTextEdit cannot model
    unrelated transforms on separate runs within its own rich paragraph.
    """
    if hashlib.sha256(pdf_bytes).hexdigest() != target.revision:
        raise NestedContentError("Выбранный текст Form устарел. Выберите его заново.")
    if not target.editable:
        return target.reason
    if target.local_block.kind != 'text':
        return "Выбранный объект не является текстом."
    from .content_editor import _describe
    import pypdfium2 as pdfium
    isolated = extract_form_page(pdf_bytes, target.occurrence)
    doc = pdfium.PdfDocument(isolated)
    page = doc[0]
    textpage = page.get_textpage()
    try:
        wanted = set(target.local_block.object_ids or (target.local_block.id,))
        found = set()
        for index, obj in enumerate(page.get_objects(textpage=textpage)):
            block = _describe(0, index, obj, textpage)
            if block is None or block.id not in wanted:
                continue
            found.add(block.id)
            matrix = obj.get_matrix()
            tolerance = 1e-5 * max(1, abs(matrix.a), abs(matrix.d))
            if (abs(matrix.b) > tolerance or abs(matrix.c) > tolerance
                    or matrix.a <= 0 or matrix.d <= 0
                    or abs(matrix.a - matrix.d) > tolerance):
                return "Отдельные строки имеют собственный поворот или растяжение. " \
                       "Правьте этот текст в панели свойств, чтобы сохранить его геометрию."
        if found != wanted:
            return "Не удалось проверить геометрию строк; используйте панель свойств."
        return ""
    finally:
        obj = None
        textpage.close()
        page.close()
        doc.close()


def apply_nested_content_edit(pdf_bytes: bytes, target: NestedContentBlock,
                              operation: str, **kwargs: Any) -> bytes:
    """Edit one exact leaf-local block and return the complete cloned PDF.

    Accepts the same replace/move/delete arguments as ``apply_content_edit``.
    IDs are private to this revision. Failure returns no bytes and cannot
    mutate the supplied immutable document, source file, or session history.
    """
    data = bytes(pdf_bytes)
    if hashlib.sha256(data).hexdigest() != target.revision:
        raise NestedContentError("Выбранный текст Form устарел. Выберите его заново.")
    current = next((block for block in inspect_nested_content(data, target.occurrence)
                    if block.id == target.id), None)
    if current is None or current != target:
        raise NestedContentError("Выбранный объект Form устарел. Выберите его заново.")
    if not current.editable:
        raise NestedContentError(current.reason)
    supplied_ids = kwargs.pop("object_ids", None)
    if supplied_ids is not None and tuple(supplied_ids) != current.local_block.object_ids:
        raise NestedContentError("Правка должна относиться к одному локальному блоку Form.")
    if operation.strip().lower() == "move" and float(kwargs.get('dx', 0)) == 0 and float(kwargs.get('dy', 0)) == 0:
        return data
    isolated = extract_form_page(data, current.occurrence)
    edited = apply_content_edit(isolated, 0, current.local_block.id, operation, **kwargs)
    # A regeneration must not change the native properties of other blocks.
    # This also catches missing text/glyph maps and accidental deeper edits.
    edited_blocks = inspect_content(edited, 0)
    after = [_semantic_block(block) for block in edited_blocks]
    for block in inspect_content(isolated, 0):
        if block.id == current.local_block.id:
            continue
        signature = _semantic_block(block)
        if signature not in after:
            raise NestedContentError("Правка изменила соседний объект Form; операция отменена.")
        after.remove(signature)
    boxes = [current.local_bbox]
    selected_after = [block for block in edited_blocks if _semantic_block(block) in after]
    boxes.extend(block.bbox for block in selected_after)
    for block in [current.local_block] + selected_after:
        for underline in block.underlines:
            x0, y0, x1, y1 = underline['coords']
            margin = float(underline.get('width', 1))
            boxes.append((min(x0, x1) - margin, min(y0, y1) - margin,
                          max(x0, x1) + margin, max(y0, y1) + margin))
    _verify_untouched_pixels(isolated, edited, boxes)
    return replace_form_occurrence_content(data, current.occurrence, edited)
