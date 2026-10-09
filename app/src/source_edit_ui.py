"""Canvas selection and overlays for editing native PDF content objects."""
from __future__ import annotations

import dataclasses
import math
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPainter, QPen, QPolygon, QRegion,
                           QTextCharFormat, QTextCursor, QTextOption, QTransform)
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView, QTextEdit, QWidget

from .pdf_backend import (_display_to_user_point, _user_rect_to_display,
                          _family_from_font_name)
from .nested_content import FormOccurrence, page_delta_to_form_parent
from .nested_text_editor import (NestedContentBlock, nested_inline_support_reason,
                                 page_delta_to_nested_local)
from .source_session import SourceContentSession


def _qt_text_length(text: str) -> int:
    return len(str(text).encode("utf-16-le")) // 2


def block_editable(block: Any) -> bool:
    """Whether a source-content block may be changed in place."""
    return bool(getattr(block, "editable", True))


def block_reason(block: Any) -> str:
    """Actionable explanation for a read-only source-content block."""
    return str(getattr(block, "reason", "") or
               "Этот объект нельзя изменить напрямую в исходном PDF")


def _rich_char_format(span: dict[str, Any], *, font_scale: float = 1.0,
                      pixel_sizes: bool = False) -> QTextCharFormat:
    char_format = QTextCharFormat()
    font = char_format.font()
    font.setFamilies([_family_from_font_name(str(span.get("font_name", "Arial")))])
    if span.get("font_size") and pixel_sizes:
        font.setPixelSize(max(1, round(float(span["font_size"]) * font_scale)))
    char_format.setFont(font)
    if span.get("font_size") and not pixel_sizes:
        char_format.setFontPointSize(float(span["font_size"]) * font_scale)
    if span.get("color"):
        char_format.setForeground(QColor(str(span["color"])))
    char_format.setFontWeight(QFont.Weight.Bold if span.get("bold", False)
                              else QFont.Weight.Normal)
    char_format.setFontItalic(bool(span.get("italic", False)))
    char_format.setFontUnderline(bool(span.get("underline", False)))
    return char_format


def populate_rich_text_edit(editor: QTextEdit, block: Any, *, font_scale: float = 1.0,
                            pixel_sizes: bool = False) -> None:
    """Populate a QTextEdit from a native paragraph's persisted rich spans."""
    editor.setPlainText(str(getattr(block, "text", "")))
    spans = tuple(getattr(block, "rich_spans", ()) or ())
    if not spans:
        spans = ({"text": block.text, "font_name": getattr(block, "font_name", "Arial"),
                  "font_size": getattr(block, "font_size", 12.0),
                  "color": getattr(block, "color", "#000000"),
                  "bold": getattr(block, "bold", False)},)
    cursor = QTextCursor(editor.document())
    position = 0
    for span in spans:
        length = _qt_text_length(str(span.get("text", "")))
        if length:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(_rich_char_format(span, font_scale=font_scale,
                                                      pixel_sizes=pixel_sizes))
        position += length


def extract_rich_spans(editor: QTextEdit,
                       defaults: dict[str, Any] | None = None, *,
                       font_scale: float = 1.0, pixel_sizes: bool = False) -> list[dict[str, Any]]:
    """Read text and per-run formatting from a QTextEdit in its document order."""
    defaults = {"font_name": "Arial", "font_size": 12.0, "color": "#000000",
                "bold": False, "italic": False, "underline": False,
                **(defaults or {})}
    result: list[dict[str, Any]] = []

    def append(text: str, fmt: QTextCharFormat) -> None:
        if not text:
            return
        font = QFont(fmt.font())
        family = next((item for item in font.families() if item), defaults["font_name"])
        pixel_size = font.pixelSize()
        size = ((pixel_size / font_scale) if pixel_sizes and pixel_size > 0
                else fmt.fontPointSize() / font_scale if fmt.fontPointSize() > 0
                else defaults["font_size"])
        foreground = fmt.foreground()
        color = foreground.color().name().upper() if foreground.style() != Qt.BrushStyle.NoBrush \
            else defaults["color"]
        style = {"font_name": family, "font_size": float(size), "color": color,
                 "bold": font.weight() >= QFont.Weight.Bold,
                 "italic": font.italic(), "underline": fmt.fontUnderline()}
        if result and all(result[-1][key] == style[key] for key in style):
            result[-1]["text"] += text
        else:
            result.append({"text": text, **style})

    block = editor.document().begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                append(fragment.text(), fragment.charFormat())
            iterator += 1
        next_block = block.next()
        if next_block.isValid():
            append("\n", _rich_char_format(result[-1] if result else defaults))
        block = next_block
    return result


def apply_rich_format(editor: QTextEdit, *, cursor: QTextCursor | None = None,
                      whole_block_if_unselected: bool = True, **style: Any) -> str | None:
    """Apply rich formatting to a saved/current selection or the whole document.

    Keep a copy of ``editor.textCursor()`` before focusing a toolbar control and
    pass it back as ``cursor=...`` to reliably format the original selection.
    """
    target = QTextCursor(cursor if cursor is not None else editor.textCursor())
    scope = "selection" if target.hasSelection() else "block"
    if not target.hasSelection():
        if not whole_block_if_unselected:
            return None
        target.select(QTextCursor.SelectionType.Document)
    pixel_size = style.get("font_pixel_size")
    if pixel_size is not None:
        doc = editor.document()
        start, end = target.selectionStart(), target.selectionEnd()
        block = doc.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    fragment_start = fragment.position()
                    fragment_end = fragment_start + _qt_text_length(fragment.text())
                    selected_start = max(start, fragment_start)
                    selected_end = min(end, fragment_end)
                    if selected_start < selected_end:
                        current_format = QTextCharFormat(fragment.charFormat())
                        current_font = QFont(current_format.font())
                        current_font.setPixelSize(int(pixel_size))
                        current_format.setFont(current_font)
                        run_cursor = QTextCursor(doc)
                        run_cursor.setPosition(selected_start)
                        run_cursor.setPosition(selected_end, QTextCursor.MoveMode.KeepAnchor)
                        run_cursor.mergeCharFormat(current_format)
                iterator += 1
            block = block.next()
    fmt = QTextCharFormat()
    if "font_name" in style:
        # Changing only the family must retain each run's pixel/point size.
        # setFont() on a default QFont silently resets a simultaneously
        # applied pixel size to the default point size.
        fmt.setFontFamilies([_family_from_font_name(str(style["font_name"]))])
    if "font_size" in style:
        fmt.setFontPointSize(float(style["font_size"]))
    if "color" in style:
        fmt.setForeground(QColor(str(style["color"])))
    if "bold" in style:
        fmt.setFontWeight(QFont.Weight.Bold if bool(style["bold"]) else QFont.Weight.Normal)
    if "italic" in style:
        fmt.setFontItalic(bool(style["italic"]))
    if "underline" in style:
        fmt.setFontUnderline(bool(style["underline"]))
    target.mergeCharFormat(fmt)
    editor.setTextCursor(target)
    return scope


class _ContentOverlay(QWidget):
    def __init__(self, controller: "SourceContentController", page: int, parent: QWidget):
        super().__init__(parent)
        self.controller = controller
        self.page = page
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: N802
        controller = self.controller
        if not controller.active:
            return
        canvas = self.parentWidget()
        if canvas is None:
            return
        blocks = controller.blocks_by_page.get(self.page, ())
        zoom = float(getattr(canvas, "zoom", 1.0) or 1.0)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            for occurrence in controller.form_occurrences_by_page.get(self.page, ()):
                x, y, width, height = controller.display_rect(self.page, occurrence.page_bbox)
                rect = QRectF(x * zoom, y * zoom, width * zoom, height * zoom).normalized()
                selected = (occurrence.identity == controller.selected_form_identity
                            and self.page == controller.selected_page)
                if selected and controller.drag_display_delta is not None:
                    delta = controller.drag_display_delta
                    rect.translate(delta.x(), delta.y())
                pen = QPen(QColor("#087E91" if selected else "#607D8B"),
                           2.0 if selected else 1.0)
                pen.setStyle(Qt.PenStyle.SolidLine if selected else Qt.PenStyle.DotLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)
                tag = QRectF(rect.left(), max(0.0, rect.top() - 15.0), 18.0, 14.0)
                painter.fillRect(tag, QColor("#087E91" if selected else "#607D8B"))
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, "Г")
                if selected:
                    painter.setPen(QPen(QColor("#087E91"), 1.0))
                    painter.setBrush(QColor("#FFFFFF"))
                    painter.drawRect(QRectF(rect.right() - 5, rect.bottom() - 5, 10, 10))
            for block in blocks:
                x, y, width, height = controller.display_rect(self.page, block.bbox)
                rect = QRectF(x * zoom, y * zoom, width * zoom, height * zoom)
                editable = block_editable(block)
                selected = (editable and block.id in controller.selected_ids
                            and self.page == controller.selected_page)
                if not editable:
                    # Read-only layers (invisible OCR text, shared Form XObject
                    # content) are shown muted so they never look editable.
                    color = QColor("#98A2A6")
                    color.setAlpha(120)
                    pen = QPen(color, 1.0)
                    pen.setStyle(Qt.PenStyle.DotLine)
                else:
                    color = QColor("#087E91" if selected else "#37A0AC")
                    color.setAlpha(230 if selected else 95)
                    pen = QPen(color, 2.0 if selected else 1.0)
                    if not selected:
                        pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if selected and controller.drag_display_delta is not None:
                    delta = controller.drag_display_delta
                    rect.translate(delta.x() * zoom, delta.y() * zoom)
                painter.drawRect(rect.normalized())
                if selected and block.id == controller.selected_id and self.page == controller.selected_page:
                    painter.setBrush(QColor("#FFFFFF"))
                    painter.setPen(QPen(QColor("#087E91"), 2.0))
                    corner = controller.nested_resize_corner(canvas) if controller.in_nested_content else None
                    if corner is not None:
                        if controller._resize_start is not None and controller.drag_display_delta is not None:
                            corner += controller.drag_display_delta
                        painter.drawRect(QRectF(corner.x() - 5, corner.y() - 5, 10, 10))
                    elif not controller.in_nested_content:
                        painter.drawRect(QRectF(rect.right() - 5, rect.bottom() - 5, 10, 10))
        finally:
            painter.end()


class SourceContentController(QObject):
    """Selection controller attached as an event filter to EditorCanvas widgets."""

    def __init__(self, window: Any, tab: Any):
        super().__init__(window)
        self.window = window
        self.tab = tab
        self.active = False
        self.selected_id: str | None = None
        self.selected_ids: list[str] = []
        self.selected_page = -1
        self.blocks_by_page: dict[int, list[Any]] = {}
        self.form_occurrences_by_page: dict[int, list[FormOccurrence]] = {}
        self.selected_form_identity: str | None = None
        self._overlays: dict[Any, _ContentOverlay] = {}
        self._drag_start_widget: QPointF | None = None
        self._drag_start_pdf: tuple[float, float] | None = None
        self._drag_block_id: str | None = None
        self.drag_display_delta: QPointF | None = None
        self._inline_editor: QTextEdit | None = None
        self._inline_view: QGraphicsView | None = None
        self._inline_proxy = None
        self._inline_font_scale = 1.0
        self._fitting_inline = False
        self._resize_start: tuple[QPointF, tuple[float, float, float, float]] | None = None
        self._form_drag_identity: str | None = None
        self._seen_doc_revision = getattr(tab.session, "_doc_revision", None)
        self.nested_occurrence: FormOccurrence | None = None
        self.nested_targets_by_id: dict[str, NestedContentBlock] = {}
        self._nested_axes_reasons: dict[str, str] = {}

    @property
    def inline_font_scale(self) -> float:
        return self._inline_font_scale

    @property
    def inline_is_affine(self) -> bool:
        return self._inline_proxy is not None

    def _local_to_widget(self, canvas, target, x, y):
        a, b, c, d, e, f = target.occurrence.page_matrix
        page_x, page_y = a * x + c * y + e, b * x + d * y + f
        shown_x, shown_y, _w, _h = self.display_rect(
            target.occurrence.page_index, (page_x, page_y, page_x, page_y))
        return QPointF(shown_x * canvas.zoom, shown_y * canvas.zoom)

    def nested_resize_corner(self, canvas) -> QPointF | None:
        target = self.selected_nested_target
        if target is None:
            return None
        if target.local_block.kind == 'text':
            if target.id not in self._nested_axes_reasons:
                try:
                    self._nested_axes_reasons[target.id] = nested_inline_support_reason(
                        self.tab.session._make_snapshot().data, target)
                except Exception as exc:  # noqa: BLE001 - a stale selection has no resize handle
                    self._nested_axes_reasons[target.id] = str(exc)
            if self._nested_axes_reasons[target.id]:
                return None
        local = target.local_block
        width = local.block_width or target.local_bbox[2] - target.local_bbox[0]
        height = local.block_height or target.local_bbox[3] - target.local_bbox[1]
        return self._local_to_widget(canvas, target, target.local_bbox[0] + width,
                                     target.local_bbox[3] - height)

    @property
    def in_nested_content(self) -> bool:
        return self.nested_occurrence is not None

    @property
    def selected_nested_target(self) -> NestedContentBlock | None:
        return self.nested_targets_by_id.get(self.selected_id)

    @property
    def selected_local_block(self):
        target = self.selected_nested_target
        return target.local_block if target is not None else self.selected_block

    def enter_nested_content(self) -> None:
        """Explicitly enter one selected invocation's bounded native contents."""
        occurrence = self.selected_form_occurrence
        if occurrence is None:
            return
        try:
            targets = SourceContentSession.inspect_nested(
                self.tab.session, occurrence.page_index, occurrence.identity)
        except Exception as exc:  # noqa: BLE001
            self.window.show_status("Не удалось открыть содержимое группы: %s" % exc, timeout=12000)
            return
        self.nested_occurrence = occurrence
        self.nested_targets_by_id = {target.id: target for target in targets}
        self.selected_form_identity = None
        self.selected_id = None
        self.selected_ids = []
        self.blocks_by_page[occurrence.page_index] = self._project_nested_blocks(targets)
        self._update_overlays()
        self.window.inspector.rebuild()
        self.window.show_status("Содержимое выбранного экземпляра группы: выберите текст или изображение. "
                                "Размеры и шрифт в свойствах указаны внутри группы.", timeout=12000)

    @staticmethod
    def _project_nested_blocks(targets):
        return [dataclasses.replace(target.local_block, id=target.id, bbox=target.bbox,
                                    object_ids=(target.id,),
                                    block_width=target.local_block.block_width or
                                    target.local_bbox[2] - target.local_bbox[0],
                                    block_height=target.local_block.block_height or
                                    target.local_bbox[3] - target.local_bbox[1])
                for target in targets]

    def exit_nested_content(self) -> None:
        """Return to ordinary native page objects and selected Form group."""
        occurrence = self.nested_occurrence
        self._close_inline(commit=False)
        self.nested_occurrence = None
        self.nested_targets_by_id.clear()
        self.blocks_by_page.clear()
        self.selected_id = None
        self.selected_ids = []
        if occurrence is not None:
            self._ensure_page(occurrence.page_index, force=True)
            self.select_form_group(occurrence.page_index, occurrence)

    @property
    def selected_form_occurrence(self) -> FormOccurrence | None:
        if self.selected_form_identity is None:
            return None
        matches = [item for item in self.form_occurrences_by_page.get(self.selected_page, ())
                   if item.identity == self.selected_form_identity]
        return matches[0] if len(matches) == 1 else None

    @property
    def selected_block(self):
        if self.selected_id is None:
            return None
        selected = [block for block in self.blocks_by_page.get(self.selected_page, ())
                    if block.id in self.selected_ids]
        primary = next((block for block in selected if block.id == self.selected_id), None)
        if primary is None:
            return None
        if len(selected) == 1:
            return primary
        x0 = min(item.bbox[0] for item in selected)
        y0 = min(item.bbox[1] for item in selected)
        x1 = max(item.bbox[2] for item in selected)
        y1 = max(item.bbox[3] for item in selected)
        ordered = sorted(selected, key=lambda item: (-item.bbox[1], item.bbox[0]))
        return dataclasses.replace(primary, bbox=(x0, y0, x1, y1),
                                   text="\n".join(item.text for item in ordered),
                                   object_ids=tuple(item.id for item in selected))

    def display_rect(self, page: int, bbox: tuple[float, float, float, float]):
        geom = self.tab.session._geometry(page)
        x0, y0, x1, y1 = bbox
        return _user_rect_to_display(geom, x0, y0, x1 - x0, y1 - y0)

    def _ensure_page(self, page: int, force: bool = False) -> list[Any]:
        if page not in self.blocks_by_page or force:
            if self.nested_occurrence is not None and page == self.nested_occurrence.page_index:
                targets = SourceContentSession.inspect_nested(self.tab.session, page,
                                                              self.nested_occurrence.identity)
                self.nested_targets_by_id = {target.id: target for target in targets}
                self.blocks_by_page[page] = self._project_nested_blocks(targets)
            else:
                self.blocks_by_page[page] = SourceContentSession.inspect(self.tab.session, page)
        return self.blocks_by_page[page]

    def _ensure_form_occurrences(self, page: int, force: bool = False) -> list[FormOccurrence]:
        if page not in self.form_occurrences_by_page or force:
            self.form_occurrences_by_page[page] = SourceContentSession.inspect_form_occurrences(
                self.tab.session, page)
        return self.form_occurrences_by_page[page]

    def sync(self, active: bool, *, refresh: bool = False) -> None:
        self.active = bool(active and self.tab.has_document and self.tab.view is not None)
        revision = getattr(self.tab.session, "_doc_revision", None)
        if revision != self._seen_doc_revision:
            self._close_inline(commit=False)
            self.blocks_by_page.clear()
            self.form_occurrences_by_page.clear()
            self.selected_id = None
            self.selected_ids = []
            self.selected_form_identity = None
            self.selected_page = -1
            self._seen_doc_revision = revision
            self.nested_occurrence = None
            self.nested_targets_by_id.clear()
        canvases = self.window._view_canvases(self.tab)
        current = set(canvases)
        for canvas in list(self._overlays):
            if canvas not in current or not self.active:
                try:
                    canvas.removeEventFilter(self)
                except RuntimeError:
                    pass
                overlay = self._overlays.pop(canvas)
                overlay.hide()
                overlay.deleteLater()
        if not self.active:
            self._close_inline(commit=False)
            self._drag_start_widget = self._drag_start_pdf = None
            self.drag_display_delta = None
            self.nested_occurrence = None
            self.nested_targets_by_id.clear()
            return
        for page, canvas in enumerate(canvases):
            overlay = self._overlays.get(canvas)
            if overlay is None:
                canvas.installEventFilter(self)
                overlay = _ContentOverlay(self, page, canvas)
                self._overlays[canvas] = overlay
            overlay.page = page
            overlay.setGeometry(canvas.rect())
            overlay.show()
            overlay.raise_()
        page = max(0, min(int(self.tab.page_index), max(len(canvases) - 1, 0)))
        if refresh:
            self.blocks_by_page.clear()
            self.form_occurrences_by_page.clear()
            self.selected_id = None
            self.selected_ids = []
            self.selected_form_identity = None
            self.selected_page = -1
            self.nested_occurrence = None
            self.nested_targets_by_id.clear()
        if canvases:
            try:
                self._ensure_page(page)
                self._ensure_form_occurrences(page)
            except Exception as exc:  # noqa: BLE001
                self.window.show_status("Не удалось прочитать содержимое PDF: %s" % exc, timeout=8000)
        self._update_overlays()

    def _open_inline(self, canvas: QWidget, page: int, block: Any) -> None:
        self._close_inline(commit=False)
        if self.in_nested_content:
            try:
                self._open_nested_inline(canvas)
            except Exception as exc:  # noqa: BLE001 - native revision/Qt failures remain atomic
                self.window.show_status('Не удалось открыть редактор группы: %s' % exc, timeout=12000)
            return
        if not block_editable(block):
            self.window.show_status(block_reason(block), timeout=12000)
            return
        x, y, ink_width, ink_height = self.display_rect(page, block.bbox)
        zoom = float(getattr(canvas, "zoom", 1.0) or 1.0)
        self._inline_font_scale = zoom
        geom = self.tab.session._geometry(page)
        # The stored dimensions describe the logical paragraph box. For text
        # that follows page axes, a quarter turn swaps its displayed extents;
        # display-oriented paragraphs already store screen-space dimensions.
        width = float(getattr(block, "block_width", 0.0) or ink_width)
        height = float(getattr(block, "block_height", 0.0) or ink_height)
        if (getattr(block, "orientation", "page") != "display" and
                geom.rotation in (90, 270)):
            width, height = height, width

        x_px = x * zoom
        y_px = y * zoom
        available_width = max(1.0, canvas.width() - x_px - 2.0)
        editor_width = min(max(80.0, width * zoom + 10.0), available_width)
        editor = QTextEdit(canvas)
        editor.setObjectName("InlineSourceTextEditor")
        editor.setPlainText(block.text)
        family = _family_from_font_name(block.font_name or "Arial")
        pixels = max(1, round(block.font_size * zoom))
        safe_family = family.replace('"', '').replace('\\', '')
        editor.setStyleSheet("QTextEdit { background: #FFFFFF; color: %s; "
                             "font-family: \"%s\"; font-size: %dpx; "
                             "font-weight: %s; border: 2px solid #087E91; "
                             "padding: 2px; }"
                             % (block.color, safe_family, pixels,
                                "bold" if getattr(block, "bold", False) else "normal"))
        font = QFont(family)
        font.setPixelSize(pixels)
        font.setBold(bool(getattr(block, "bold", False)))
        editor.setFont(font)
        editor.document().setDocumentMargin(0.0)
        editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setGeometry(QRectF(x_px, y_px, editor_width, 28.0).toAlignedRect())

        # Measure after applying the stored display width and font. Ink bounds
        # often contain only glyph tops/bottoms, so using them as editor height
        # clips later lines when a grouped paragraph is reopened.
        frame_padding = 10.0
        content_width = max(1.0, editor_width - frame_padding)
        editor.document().setTextWidth(content_width)
        measured_height = editor.document().documentLayout().documentSize().height()
        metrics = QFontMetricsF(font)
        minimum_height = metrics.lineSpacing() * max(1, block.text.count("\n") + 1)
        logical_height = height * zoom
        editor_height = max(28.0, logical_height + frame_padding,
                            measured_height + frame_padding,
                            minimum_height + frame_padding)
        editor_height = min(editor_height, max(1.0, canvas.height() - 2.0))
        # Keep the grown editor on the page even when a paragraph sits near
        # the lower edge; moving the temporary editor does not move PDF text.
        y_px = min(y_px, max(1.0, canvas.height() - editor_height - 1.0))
        editor.setGeometry(QRectF(x_px, y_px, editor_width, editor_height).toAlignedRect())
        populate_rich_text_edit(editor, block, font_scale=zoom, pixel_sizes=True)
        editor.installEventFilter(self)
        self._inline_editor = editor
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.selectAll()
        bind_editor = getattr(self.window.inspector, "bind_source_editor", None)
        if callable(bind_editor):
            bind_editor(editor)
        self.window.show_status("Правьте текст прямо в блоке; Ctrl+Enter применяет, Esc отменяет")

    def _open_nested_inline(self, canvas: QWidget) -> None:
        target = self.selected_nested_target
        if target is None:
            return
        reason = nested_inline_support_reason(self.tab.session._make_snapshot().data, target)
        if reason:
            self.window.show_status(reason, timeout=12000)
            return
        block = target.local_block
        width = block.block_width or block.bbox[2] - block.bbox[0]
        height = block.block_height or block.bbox[3] - block.bbox[1]
        editor = QTextEdit()
        editor.setObjectName('InlineSourceTextEditor')
        editor.setStyleSheet('QTextEdit { background: #FFFFFF; border: 1px solid #087E91; padding: 0px; }')
        editor.document().setDocumentMargin(0)
        editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Logical editor pixels are leaf PDF points. The graphics proxy applies
        # the full affine once, including visible font scaling and page rotation.
        font = QFont(_family_from_font_name(block.font_name or 'Arial'))
        font.setPixelSize(max(1, round(block.font_size or 12)))
        font.setBold(block.bold)
        editor.setFont(font)
        self._inline_font_scale = 1.0
        populate_rich_text_edit(editor, block, font_scale=1, pixel_sizes=True)
        if not block.block_width:
            width = max(width, max((QFontMetricsF(font).horizontalAdvance(line)
                                    for line in block.text.split('\n')), default=width))
        # Size the viewport before measuring. QTextEdit owns its layout width;
        # measuring a detached document can produce a spurious second line.
        editor.resize(max(3, math.ceil(width + 4)), 3)
        measured = editor.document().documentLayout().documentSize().height()
        editor.resize(editor.width(), max(3, round(max(height, measured) + 2)))
        view = QGraphicsView(canvas)
        view.setObjectName('NestedInlineAffineView')
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setStyleSheet('QGraphicsView { background: transparent; border: none; }')
        view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        view.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        view.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        view.viewport().setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        view.viewport().setAutoFillBackground(False)
        view.setBackgroundBrush(Qt.BrushStyle.NoBrush)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        view.setGeometry(canvas.rect())
        scene = QGraphicsScene(view)
        scene.setSceneRect(QRectF(canvas.rect()))
        view.setScene(scene)
        proxy = scene.addWidget(editor)
        # QGraphicsProxyWidget otherwise promotes QTextEdit's 62-pixel size
        # hint to a hard minimum, covering unrelated native text on small runs.
        proxy.setMinimumSize(0, 0)
        proxy.resize(editor.width(), editor.height())
        x0, _y0, _x1, y1 = block.bbox
        origin = self._local_to_widget(canvas, target, x0, y1)
        horizontal = self._local_to_widget(canvas, target, x0 + 1, y1) - origin
        vertical = self._local_to_widget(canvas, target, x0, y1 - 1) - origin
        proxy.setTransform(QTransform(horizontal.x(), horizontal.y(), vertical.x(), vertical.y(),
                                      origin.x(), origin.y()))
        self._inline_view, self._inline_proxy, self._inline_editor = view, proxy, editor
        editor.textChanged.connect(self._fit_nested_inline)
        editor.document().documentLayout().documentSizeChanged.connect(self._fit_nested_inline)
        editor.installEventFilter(self)
        view.show()
        view.raise_()
        self._refresh_nested_inline_geometry()
        self._fit_nested_inline()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.selectAll()
        bind = getattr(self.window.inspector, 'bind_source_editor', None)
        if callable(bind):
            bind(editor)
        self.window.show_status('Правьте текст прямо в группе; Ctrl+Enter применяет, Esc отменяет')

    def _fit_nested_inline(self, *_args) -> None:
        editor, proxy, target = self._inline_editor, self._inline_proxy, self.selected_nested_target
        if editor is None or proxy is None or target is None or self._fitting_inline:
            return
        self._fitting_inline = True
        try:
            height = max(target.local_block.block_height or
                         target.local_bbox[3] - target.local_bbox[1],
                         editor.document().documentLayout().documentSize().height()) + 2
            if editor.height() != math.ceil(height):
                proxy.resize(editor.width(), math.ceil(height))
        finally:
            self._fitting_inline = False

    def _refresh_nested_inline_geometry(self) -> None:
        view, proxy, target = self._inline_view, self._inline_proxy, self.selected_nested_target
        if view is None or proxy is None or target is None:
            return
        canvas = view.parentWidget()
        view.setGeometry(canvas.rect())
        view.scene().setSceneRect(QRectF(canvas.rect()))
        x0, _y0, _x1, y1 = target.local_bbox
        origin = self._local_to_widget(canvas, target, x0, y1)
        horizontal = self._local_to_widget(canvas, target, x0 + 1, y1) - origin
        vertical = self._local_to_widget(canvas, target, x0, y1 - 1) - origin
        proxy.setTransform(QTransform(horizontal.x(), horizontal.y(), vertical.x(), vertical.y(),
                                      origin.x(), origin.y()))
        # Respect each enclosing Form's BBox clip, including sheared polygons.
        # This prevents the temporary native editor showing text which would
        # be clipped in the saved PDF. Page clipping is supplied by the canvas.
        region = QRegion(canvas.rect())
        for occurrence in self._ensure_form_occurrences(target.occurrence.page_index):
            if target.occurrence.path[:len(occurrence.path)] != occurrence.path:
                continue
            a, b, c, d, e, f = occurrence.page_matrix
            bx0, by0, bx1, by1 = occurrence.bbox
            points = []
            for x, y in ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)):
                ux, uy = a * x + c * y + e, b * x + d * y + f
                sx, sy, _width, _height = self.display_rect(
                    occurrence.page_index, (ux, uy, ux, uy))
                points.append(QPointF(sx * canvas.zoom, sy * canvas.zoom).toPoint())
            region &= QRegion(QPolygon(points))
        view.setMask(region)

    def _close_inline(self, *, commit: bool, text: str | None = None) -> None:
        editor = self._inline_editor
        if editor is None:
            return
        if text is None:
            text = editor.toPlainText()
        rich_spans = self.inline_rich_spans() if commit else None
        unbind_editor = getattr(self.window.inspector, "unbind_source_editor", None)
        if callable(unbind_editor):
            unbind_editor(editor)
        self._inline_editor = None
        editor.removeEventFilter(self)
        editor.hide()
        if self._inline_view is not None:
            view = self._inline_view
            self._inline_view = self._inline_proxy = None
            view.hide()
            view.deleteLater()
        else:
            editor.deleteLater()
        if commit:
            self.apply_selected(text=text, rich_spans=rich_spans)

    def inline_rich_spans(self) -> list[dict[str, Any]]:
        """Return inline text and typed per-character formatting for apply_selected()."""
        editor = self._inline_editor
        if editor is None:
            return []
        block = self.selected_local_block
        defaults = {"font_name": getattr(block, "font_name", "Arial") or "Arial",
                    "font_size": getattr(block, "font_size", 12.0) or 12.0,
                    "color": getattr(block, "color", "#000000"), "bold": getattr(block, "bold", False),
                    "italic": False, "underline": False}
        zoom = self.inline_font_scale
        return extract_rich_spans(editor, defaults, font_scale=zoom, pixel_sizes=True)

    def format_inline_selection(self, **style: Any) -> str | None:
        """Apply a text style to the selection, or the whole block if none is selected.

        Parent formatting controls can call this with font_name, font_size, color,
        bold, italic, or underline. Returns ``selection`` or ``block`` when applied.
        """
        editor = self._inline_editor
        if editor is None:
            return None
        formatted = dict(style)
        if "font_size" in formatted:
            zoom = self.inline_font_scale
            formatted["font_pixel_size"] = max(1, round(float(formatted.pop("font_size")) * zoom))
        scope = apply_rich_format(editor, **formatted)
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        return scope

    def _update_overlays(self) -> None:
        self._refresh_nested_inline_geometry()
        for canvas, overlay in list(self._overlays.items()):
            try:
                overlay.setGeometry(canvas.rect())
                overlay.update()
            except RuntimeError:
                pass

    def select(self, page: int, block: Any | None, *, extend: bool = False) -> None:
        if self.in_nested_content:
            extend = False
        self.selected_form_identity = None
        if block is not None and not block_editable(block):
            # Read-only objects (shared Form XObject content, invisible OCR
            # text) are never selected: the user must not believe that editing
            # them would change what the page shows.
            self.selected_page = -1
            self.selected_id = None
            self.selected_ids = []
            self.drag_display_delta = None
            self._update_overlays()
            if self.tab is self.window.current_tab:
                self.window.inspector.rebuild()
                self.window.show_status(block_reason(block), timeout=12000)
            return
        previous_page = self.selected_page
        self.selected_page = page if block is not None else -1
        self.selected_id = block.id if block is not None else None
        if block is None:
            self.selected_ids = []
        elif extend and page == previous_page and block.kind == "text":
            if block.id in self.selected_ids:
                self.selected_ids.remove(block.id)
            else:
                self.selected_ids.append(block.id)
            if not self.selected_ids:
                self.selected_ids = [block.id]
        else:
            self.selected_ids = [block.id]
        self.drag_display_delta = None
        self._update_overlays()
        if self.tab is self.window.current_tab:
            self.window.inspector.rebuild()
            if block is None:
                self.window.show_status("Щёлкните по тексту или изображению исходного PDF")
            else:
                kind = "Текст" if block.kind == "text" else "Изображение"
                self.window.show_status("Выбран объект «%s» на странице %d" % (kind, page + 1))

    def _display_point_to_pdf(self, canvas: QWidget, page: int, point: QPointF):
        geom = self.tab.session._geometry(page)
        shown = canvas._to_pdf(point, clamp=False)
        return _display_to_user_point(geom, shown[0], shown[1])

    def _pick(self, page: int, point: tuple[float, float]):
        blocks = self._ensure_page(page)
        x, y = point
        for block in reversed(blocks):
            x0, y0, x1, y1 = block.bbox
            if x0 <= x <= x1 and y0 <= y <= y1:
                return block
        return None

    def _pick_form_group(self, page: int, point: tuple[float, float]) -> FormOccurrence | None:
        x, y = point
        candidates = []
        for item in self._ensure_form_occurrences(page):
            x0, y0, x1, y1 = item.page_bbox
            if x0 <= x <= x1 and y0 <= y <= y1:
                area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
                order = tuple(-step.operation_index for step in item.path)
                candidates.append((area, -len(item.path), order, item))
        return min(candidates, key=lambda value: value[:3])[3] if candidates else None

    def _select_group_at(self, page: int, point: tuple[float, float]) -> bool:
        occurrence = self._pick_form_group(page, point)
        if occurrence is None:
            return False
        self.select_form_group(page, occurrence)
        return self.selected_form_occurrence is not None

    def select_form_group(self, page: int, occurrence: FormOccurrence) -> None:
        """Select a nested PDF group while keeping its text read-only."""
        current = next((item for item in self._ensure_form_occurrences(page)
                        if item.identity == occurrence.identity), None)
        if current is None:
            self.window.show_status("Группа устарела. Выберите её заново.", timeout=8000)
            return
        self._close_inline(commit=False)
        self.nested_occurrence = None
        self.nested_targets_by_id.clear()
        self.blocks_by_page.pop(page, None)
        self.selected_page = page
        self.selected_form_identity = current.identity
        self.selected_id = None
        self.selected_ids = []
        self.drag_display_delta = None
        self._update_overlays()
        if self.tab is self.window.current_tab:
            self.window.inspector.rebuild()
            self.window.show_status(
                "Выбрана вложенная группа PDF. Откройте её содержимое в панели свойств; "
                "перетаскивайте рамку для перемещения. Alt+щелчок выбирает группу "
                "в любой точке её рамки.")

    def _apply_form_group(self, operation: str, **kwargs: Any) -> None:
        occurrence = self.selected_form_occurrence
        if occurrence is None:
            self.window.show_status("Выбранная группа устарела. Выберите её заново.", timeout=8000)
            return
        if (operation == "translate" and float(kwargs["dx"]) == 0.0
                and float(kwargs["dy"]) == 0.0):
            return
        if (operation == "resize" and float(kwargs["sx"]) == 1.0
                and float(kwargs["sy"]) == 1.0):
            return
        try:
            if operation == "delete":
                SourceContentSession.delete_form_occurrence(
                    self.tab.session, self.selected_page, occurrence.identity)
            elif operation == "translate":
                dx, dy = page_delta_to_form_parent(
                    occurrence, float(kwargs["dx"]), float(kwargs["dy"]))
                SourceContentSession.translate_form_occurrence(
                    self.tab.session, self.selected_page, occurrence.identity, dx, dy)
            elif operation == "resize":
                x0, y0, _x1, _y1 = occurrence.bbox
                a, b, c, d, e, f = occurrence.matrix
                origin = (a * x0 + c * y0 + e, b * x0 + d * y0 + f)
                SourceContentSession.resize_form_occurrence(
                    self.tab.session, self.selected_page, occurrence.identity,
                    float(kwargs["sx"]), float(kwargs["sy"]), origin=origin)
            else:
                raise ValueError("Неподдерживаемое действие с группой.")
        except Exception as exc:  # noqa: BLE001 - retain session state on refusal
            self.window.show_status("Не удалось изменить группу PDF: %s" % exc,
                                    timeout=10000)
            self._ensure_form_occurrences(self.selected_page, force=True)
            self._update_overlays()
            return
        page = self.selected_page
        self.blocks_by_page.clear()
        self.form_occurrences_by_page.clear()
        self.selected_id = None
        self.selected_ids = []
        self.selected_form_identity = None
        self.selected_page = -1
        self.window._refresh_tab(self.tab, preserve_scroll=True)
        self.sync(self.tab is self.window.current_tab and self.window.mode == "edit"
                  and self.window.tool == "content", refresh=True)
        self.window.inspector.rebuild()
        self.window.show_status("Группа PDF изменена — можно отменить через Ctrl+Z")

    def delete_selected_form_group(self) -> None:
        self._apply_form_group("delete")

    def resize_selected_form_group(self, sx: float, sy: float) -> None:
        self._apply_form_group("resize", sx=sx, sy=sy)

    def _edit(self, operation: str, **kwargs: Any) -> None:
        block = self.selected_block
        if block is None:
            return
        if not block_editable(block):
            self.window.show_status(block_reason(block), timeout=12000)
            return
        try:
            target = self.selected_nested_target
            nested_path = (tuple(step.operation_index for step in target.occurrence.path)
                           if target is not None else None)
            page_index = self.selected_page
            if target is not None:
                if operation == "move":
                    kwargs["dx"], kwargs["dy"] = page_delta_to_nested_local(
                        target.occurrence, float(kwargs.get("dx", 0)), float(kwargs.get("dy", 0)))
                SourceContentSession.apply_nested(self.tab.session, target, operation, **kwargs)
            else:
                SourceContentSession.apply(self.tab.session, self.selected_page,
                                           block.id, operation,
                                           object_ids=tuple(self.selected_ids), **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.window.show_status("Не удалось изменить содержимое PDF: %s" % exc,
                                    timeout=10000)
            return
        self.blocks_by_page.clear()
        self.selected_id = None
        self.selected_ids = []
        self.selected_page = -1
        self.window._refresh_tab(self.tab, preserve_scroll=True)
        self.sync(self.tab is self.window.current_tab and self.window.mode == "edit"
                  and self.window.tool == "content", refresh=True)
        if nested_path is not None:
            # Copy-on-write changes resource names but preserves operator
            # positions. Re-enter only the path belonging to our own edit;
            # unrelated revisions/undo leave mode and require a fresh selection.
            fresh = next((item for item in self._ensure_form_occurrences(page_index, force=True)
                          if tuple(step.operation_index for step in item.path) == nested_path), None)
            if fresh is not None:
                self.select_form_group(page_index, fresh)
                self.enter_nested_content()
        self.window.inspector.rebuild()
        self.window.show_status("Содержимое PDF изменено — можно отменить через Ctrl+Z")

    def apply_selected(self, **kwargs: Any) -> None:
        self._edit("replace", **kwargs)

    def delete_selected(self) -> None:
        self._edit("delete")

    def eventFilter(self, watched: QWidget, event: QEvent) -> bool:  # noqa: N802
        if watched is self._inline_editor:
            if event.type() == QEvent.Type.KeyPress:
                if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                        and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                    self._close_inline(commit=True)
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self._close_inline(commit=False)
                    return True
            return False
        if not self.active or watched not in self._overlays:
            return False
        page = self._overlays[watched].page
        event_type = event.type()
        if event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            point = QPointF(event.position())
            try:
                pdf_point = self._display_point_to_pdf(watched, page, point)
                group = self._pick_form_group(page, pdf_point)
                alt_group = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                if self.in_nested_content and page != self.nested_occurrence.page_index:
                    self.exit_nested_content()
                block = self._pick(page, pdf_point)
                # Preserve ordinary selection for editable page-level objects
                # that happen to overlap a Form's bounding box. Alt-click is
                # the explicit group-selection gesture anywhere in that box;
                # otherwise read-only nested content/background selects the
                # enclosing Form invocation.
                if group is not None and (alt_group or (not self.in_nested_content and
                                          (block is None or not block_editable(block)))):
                    self.select_form_group(page, group)
                    self._drag_start_widget = point
                    self._drag_start_pdf = pdf_point
                    self._form_drag_identity = group.identity
                    self._drag_block_id = None
                    return True
            except Exception as exc:  # noqa: BLE001
                self.window.show_status("Не удалось выбрать объект: %s" % exc, timeout=8000)
                return True
            extend = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                Qt.KeyboardModifier.ShiftModifier))
            if (self.selected_id is not None and self.selected_page == page and
                    self.selected_block is not None and not extend):
                x, y, width, height = self.display_rect(page, self.selected_block.bbox)
                zoom = float(getattr(watched, "zoom", 1.0) or 1.0)
                corner = self.nested_resize_corner(watched) if self.in_nested_content else None
                if not self.in_nested_content:
                    corner = QPointF((x + width) * zoom, (y + height) * zoom)
                if corner is not None and abs(point.x() - corner.x()) <= 9 and abs(point.y() - corner.y()) <= 9:
                    local = self.selected_local_block
                    bbox = local.bbox if self.in_nested_content else self.selected_block.bbox
                    self._resize_start = (point, bbox)
                    self._drag_start_widget = self._drag_start_pdf = None
                    return True
            self.select(page, block, extend=extend)
            if block is not None and not block_editable(block):
                # Read-only content (shared forms, invisible OCR text) is never
                # dragged, resized or opened for editing.
                self._drag_start_widget = self._drag_start_pdf = None
                return True
            if block is not None:
                watched.setFocus(Qt.FocusReason.MouseFocusReason)
            if block is None:
                self._drag_start_widget = self._drag_start_pdf = None
            else:
                self._drag_start_widget = point
                self._drag_start_pdf = pdf_point
                self._drag_block_id = block.id
            if event_type == QEvent.Type.MouseButtonDblClick and block and block.kind == "text":
                self._open_inline(watched, page, block)
            return True
        if event_type == QEvent.Type.MouseMove and self._resize_start is not None:
            point = QPointF(event.position())
            start, bbox = self._resize_start
            delta = point - start
            self.drag_display_delta = delta
            self._update_overlays()
            return True
        if event_type == QEvent.Type.MouseButtonRelease and self._resize_start is not None:
            point = QPointF(event.position())
            start, bbox = self._resize_start
            self._resize_start = None
            self.drag_display_delta = None
            canvas = watched
            zoom = float(getattr(canvas, "zoom", 1.0) or 1.0)
            if (point - start).manhattanLength() < 3:
                self._update_overlays()
                return True
            if self.in_nested_content:
                target = self.selected_nested_target
                if target is None:
                    return True
                begin = self._display_point_to_pdf(canvas, page, start)
                end = self._display_point_to_pdf(canvas, page, point)
                dx, dy = page_delta_to_nested_local(target.occurrence, end[0] - begin[0], end[1] - begin[1])
                local = target.local_block
                width = max(1.0, (local.block_width or bbox[2] - bbox[0]) + dx)
                height = max(1.0, (local.block_height or bbox[3] - bbox[1]) - dy)
            else:
                width = max(1.0, bbox[2] - bbox[0] + (point.x() - start.x()) / zoom)
                height = max(1.0, bbox[3] - bbox[1] + (point.y() - start.y()) / zoom)
            self._edit("replace", text=self.selected_block.text, width=width, height=height)
            return True
        if event_type == QEvent.Type.MouseMove and self._drag_start_widget is not None:
            point = QPointF(event.position())
            delta = point - self._drag_start_widget
            if abs(delta.x()) + abs(delta.y()) >= 3:
                self.drag_display_delta = delta
                self._update_overlays()
            return True
        if event_type == QEvent.Type.MouseButtonRelease and self._drag_start_widget is not None:
            start_pdf = self._drag_start_pdf
            point = QPointF(event.position())
            start_widget = self._drag_start_widget
            form_identity = self._form_drag_identity
            self._drag_start_widget = self._drag_start_pdf = None
            self._form_drag_identity = None
            if start_pdf is None:
                return True
            delta_px = point - start_widget
            if abs(delta_px.x()) + abs(delta_px.y()) >= 3:
                try:
                    end_pdf = self._display_point_to_pdf(watched, page, point)
                    dx, dy = end_pdf[0] - start_pdf[0], end_pdf[1] - start_pdf[1]
                    if form_identity is not None:
                        self.selected_form_identity = form_identity
                        self._apply_form_group("translate", dx=dx, dy=dy)
                    else:
                        self._edit("move", dx=dx, dy=dy)
                except Exception as exc:  # noqa: BLE001
                    self.window.show_status("Не удалось переместить объект: %s" % exc,
                                            timeout=8000)
            else:
                self.drag_display_delta = None
                self._update_overlays()
            return True
        if event_type == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self.selected_block is not None and event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                return False
            if self.selected_block is not None:
                step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
                direction = {
                    Qt.Key.Key_Left: QPointF(-step, 0),
                    Qt.Key.Key_Right: QPointF(step, 0),
                    Qt.Key.Key_Up: QPointF(0, -step),
                    Qt.Key.Key_Down: QPointF(0, step),
                }[event.key()]
                origin = self._display_point_to_pdf(watched, page, QPointF(0, 0))
                target = self._display_point_to_pdf(watched, page, direction)
                self._edit("move", dx=target[0] - origin[0], dy=target[1] - origin[1])
                return True
        if event_type == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Delete,
                                                                   Qt.Key.Key_Backspace):
            if self.selected_form_occurrence is not None:
                self.delete_selected_form_group()
                return True
            if self.selected_block is not None:
                self.delete_selected()
                return True
        if event_type == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape and self.in_nested_content:
            self.exit_nested_content()
            return True
        if event_type == QEvent.Type.Resize:
            overlay = self._overlays.get(watched)
            if overlay is not None:
                overlay.setGeometry(watched.rect())
        return False
