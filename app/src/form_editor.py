"""Interactive authoring gestures for native AcroForm fields.

The controller is intentionally separate from EditorCanvas so the accepted
document shell can attach it to every visible page canvas without changing the
canvas' fill/edit behavior. Rectangles use the backend's displayed-page points.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class _FieldOverlay(QWidget):
    def __init__(self, canvas: QWidget, owner: "FormAuthorController") -> None:
        super().__init__(canvas)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setGeometry(canvas.rect())
        self.show()

    def paintEvent(self, event) -> None:  # noqa: N802
        canvas = self.owner.canvas
        if canvas is None:
            return
        zoom = max(float(getattr(canvas, "zoom", 1.0)), .01)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, item in enumerate(self.owner.widgets):
            x, y, width, height = item["rect"]
            rect = QRectF(x*zoom, y*zoom, width*zoom, height*zoom)
            selected = index == self.owner.selected_index
            color = QColor("#007e91" if selected else "#16a6b5")
            color.setAlpha(45 if selected else 20)
            painter.fillRect(rect, color)
            pen = QPen(QColor("#007e91" if selected else "#148999"), 2 if selected else 1)
            if not selected:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect)
            if selected:
                painter.setBrush(QColor("white"))
                for px, py in ((rect.left(), rect.top()), (rect.right(), rect.bottom())):
                    painter.drawRect(QRectF(px-3, py-3, 6, 6))
        zoom = max(float(getattr(canvas, "zoom", 1.0)), .01)
        if self.owner._radio_rects:
            painter.setPen(QPen(QColor("#007e91"), 2, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(0, 126, 145, 28))
            for x, y, width, height in self.owner._radio_rects:
                painter.drawRect(QRectF(x*zoom, y*zoom, width*zoom, height*zoom))
        if self.owner.drag_rect is not None:
            painter.setPen(QPen(QColor("#007e91"), 2, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(0, 126, 145, 35))
            painter.drawRect(self.owner.drag_rect.normalized())
        painter.end()


class FormAuthorController(QObject):
    """Adds drag-to-create and select/move/resize gestures to a page canvas."""

    field_selected = Signal(str, int)
    fields_changed = Signal()
    creation_requested = Signal(str, int, object)
    error = Signal(str)

    def __init__(self, session: Any, canvas: QWidget, page_index: int = 0,
                 parent: QObject | None = None) -> None:
        super().__init__(parent or canvas)
        self.session = session
        self.canvas = canvas
        self.page_index = int(page_index)
        self.widgets: list[dict[str, Any]] = []
        self.tool = "select"
        self.field_kind = "text"
        self.field_name = "Field1"
        self.default_value: str | bool = ""
        self.readonly = False
        self.multiline = False
        self.options: list[str] = []
        self.selected_index = -1
        self.drag_rect: QRectF | None = None
        self._start: tuple[float, float] | None = None
        self._original: list[float] | None = None
        self._drag_mode = ""
        self._move_offset = (0.0, 0.0)
        self._radio_rects: list[list[float]] = []
        self.overlay = _FieldOverlay(canvas, self)
        canvas.installEventFilter(self)
        self.refresh()

    def set_page(self, page_index: int) -> None:
        self.page_index = int(page_index)
        self.selected_index = -1
        self.refresh()

    def detach(self) -> None:
        """Remove filters and overlay when the owner replaces its page canvas."""
        if self.canvas is not None:
            self.canvas.removeEventFilter(self)
        if self.overlay is not None:
            self.overlay.hide()
            self.overlay.deleteLater()
        self.canvas = None
        self.overlay = None

    def set_tool(self, tool: str) -> None:
        allowed = {"select", "text", "multiline", "checkbox", "radio", "choice"}
        if tool not in allowed:
            raise ValueError("Unknown form authoring tool: %s" % tool)
        self.tool = tool
        if tool != "radio":
            self._radio_rects.clear()
        if tool in ("text", "multiline", "checkbox", "radio", "choice"):
            self.field_kind = "text" if tool in ("text", "multiline") else tool
            self.multiline = tool == "multiline"
        self.canvas.setCursor(Qt.CursorShape.CrossCursor if tool != "select" else Qt.CursorShape.ArrowCursor)

    def configure(self, *, name: str | None = None, kind: str | None = None,
                  default_value: str | bool | None = None, readonly: bool | None = None,
                  multiline: bool | None = None, options: list[str] | None = None) -> None:
        if name is not None:
            self.field_name = str(name)
        if kind is not None:
            self.field_kind = str(kind)
        if default_value is not None:
            self.default_value = default_value
        if readonly is not None:
            self.readonly = bool(readonly)
        if multiline is not None:
            self.multiline = bool(multiline)
        if options is not None:
            self.options = list(options)

    def refresh(self) -> None:
        try:
            widgets = self.session.form_widgets(self.page_index, include_authoring_metadata=True)
        except TypeError:
            widgets = self.session.form_widgets(self.page_index)
        self.widgets = [item for item in widgets
                        if item.get("rect") and item.get("kind") != "unsupported"]
        if self.selected_index >= len(self.widgets):
            self.selected_index = -1
        self.overlay.update()

    def selected(self) -> dict[str, Any] | None:
        return self.widgets[self.selected_index] if 0 <= self.selected_index < len(self.widgets) else None

    def delete_selected(self) -> bool:
        item = self.selected()
        if item is None:
            return False
        try:
            self.session.delete_form_field(item["key"])
        except (RuntimeError, ValueError, IndexError) as exc:
            self.error.emit(str(exc))
            return False
        self.selected_index = -1
        self.refresh()
        self.fields_changed.emit()
        return True

    def update_selected(self, **properties: Any) -> bool:
        item = self.selected()
        if item is None:
            return False
        try:
            self.session.update_form_field(item["key"], **properties)
        except (RuntimeError, ValueError, IndexError) as exc:
            self.error.emit(str(exc))
            return False
        self.refresh()
        self.fields_changed.emit()
        return True

    def selected_widget_index(self) -> int | None:
        item = self.selected()
        if item is None:
            return None
        try:
            from .pdf_backend import _user_rect_to_display
        except ImportError:
            from pdf_backend import _user_rect_to_display  # type: ignore[no-redef]
        ref = self.session._fields()[item["key"]]
        widget_index = None
        for position, (page_index, widget) in enumerate(ref.widgets):
            if page_index != self.page_index:
                continue
            raw = widget.get("/Rect")
            if raw is None:
                continue
            rect = _user_rect_to_display(self.session._geometry(page_index), float(raw[0]),
                float(raw[1]), float(raw[2])-float(raw[0]), float(raw[3])-float(raw[1]))
            if all(abs(a-b) < .1 for a, b in zip(rect, item["rect"])):
                widget_index = position
                break
        if widget_index is None:
            return None
        return widget_index

    def set_selected_tab_order(self, order: int) -> bool:
        item = self.selected()
        widget_index = self.selected_widget_index()
        if item is None or widget_index is None:
            return False
        try:
            self.session.set_form_tab_order(item["key"], widget_index, int(order))
        except (RuntimeError, ValueError, IndexError) as exc:
            self.error.emit(str(exc))
            return False
        self.refresh()
        self.fields_changed.emit()
        return True

    def _page_point(self, pos) -> tuple[float, float]:
        return self.canvas._to_pdf(pos)

    def _hit(self, point: tuple[float, float]) -> int:
        x, y = point
        for index in range(len(self.widgets)-1, -1, -1):
            rx, ry, rw, rh = self.widgets[index]["rect"]
            if rx <= x <= rx+rw and ry <= y <= ry+rh:
                return index
        return -1

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is not self.canvas:
            return False
        event_type = event.type()
        if event_type == QEvent.Type.Resize:
            self.overlay.setGeometry(self.canvas.rect())
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            point = self._page_point(event.position())
            if self.tool != "select":
                self._start = point
                self.drag_rect = QRectF(event.position(), event.position())
                self._drag_mode = "create"
                self.overlay.update()
                return True
            hit = self._hit(point)
            self.selected_index = hit
            if hit < 0:
                self._drag_mode = ""
                self._start = None
            else:
                self._start = point
                self._original = list(self.widgets[hit]["rect"])
                x, y, w, h = self._original
                self._drag_mode = "resize" if abs(point[0]-(x+w)) < 7 or abs(point[1]-(y+h)) < 7 else "move"
                self._move_offset = (point[0]-x, point[1]-y)
                self.field_selected.emit(self.widgets[hit]["key"], hit)
            self.overlay.update()
            return True
        if event_type == QEvent.Type.MouseMove and self._start is not None:
            point = self._page_point(event.position())
            if self._drag_mode == "create":
                zoom = max(float(getattr(self.canvas, "zoom", 1.0)), .01)
                start_pos = self._start
                self.drag_rect = QRectF(start_pos[0]*zoom, start_pos[1]*zoom,
                                        (point[0]-start_pos[0])*zoom,
                                        (point[1]-start_pos[1])*zoom)
                self.overlay.update()
                return True
            if self._drag_mode in ("move", "resize") and self.selected_index >= 0:
                x, y, width, height = self._original or self.widgets[self.selected_index]["rect"]
                if self._drag_mode == "move":
                    x, y = point[0]-self._move_offset[0], point[1]-self._move_offset[1]
                    page_width, page_height = self.canvas.page_size()
                    x = min(max(0.0, x), max(0.0, page_width-width))
                    y = min(max(0.0, y), max(0.0, page_height-height))
                else:
                    width, height = max(4, point[0]-x), max(4, point[1]-y)
                self.widgets[self.selected_index]["rect"] = [x, y, width, height]
                self.overlay.update()
                return True
        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._drag_mode == "create" and self._start is not None:
                point = self._page_point(event.position())
                x, y = self._start
                rect = [min(x, point[0]), min(y, point[1]), max(4, abs(point[0]-x)), max(4, abs(point[1]-y))]
                if self.field_kind == "radio":
                    if len(self.options) < 2:
                        self._radio_rects.clear()
                        self.error.emit("Укажите не меньше двух вариантов группы переключателей.")
                        self._start = None
                        self._drag_mode = ""
                        self.drag_rect = None
                        self.overlay.update()
                        return True
                    self._radio_rects.append(rect)
                    if len(self._radio_rects) >= len(self.options):
                        try:
                            self.session.create_form_field("radio", self.field_name, self.page_index,
                                rects=self._radio_rects[:len(self.options)], default_value=self.default_value,
                                readonly=self.readonly, options=self.options)
                        except (RuntimeError, ValueError, IndexError) as exc:
                            self.error.emit(str(exc))
                            self._radio_rects.clear()
                        else:
                            self._radio_rects.clear()
                            self.refresh()
                            self.fields_changed.emit()
                else:
                    try:
                        key = self.session.create_form_field(self.field_kind, self.field_name,
                            self.page_index, rect, default_value=self.default_value,
                            readonly=self.readonly, multiline=self.multiline, options=self.options)
                    except (RuntimeError, ValueError, IndexError) as exc:
                        self.error.emit(str(exc))
                    else:
                        self.refresh()
                        self.selected_index = next((i for i, item in enumerate(self.widgets) if item["key"] == key), -1)
                        self.fields_changed.emit()
            elif self._drag_mode in ("move", "resize") and self.selected_index >= 0:
                item = self.widgets[self.selected_index]
                ref = self.session._fields()[item["key"]]
                widget_index = 0
                for position, (page_index, widget) in enumerate(ref.widgets):
                    if page_index != self.page_index:
                        continue
                    rect = widget.get("/Rect")
                    if rect is not None:
                        try:
                            from .pdf_backend import _user_rect_to_display
                        except ImportError:
                            from pdf_backend import _user_rect_to_display  # type: ignore[no-redef]
                        display = _user_rect_to_display(self.session._geometry(page_index),
                            float(rect[0]), float(rect[1]), float(rect[2])-float(rect[0]), float(rect[3])-float(rect[1]))
                        old = self._original or item["rect"]
                        # Match the widget selected when dragging began; the overlay record
                        # is per physical widget, while the key may span many pages.
                        if abs(display[0]-old[0]) < .1 and abs(display[1]-old[1]) < .1:
                            widget_index = position
                            break
                self.session.set_form_widget_rect(item["key"], widget_index, item["rect"])
                self.refresh()
                self.fields_changed.emit()
            self._start = None
            self._original = None
            self._drag_mode = ""
            self.drag_rect = None
            self.overlay.update()
            return True
        return False
