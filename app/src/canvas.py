"""Холст страницы PDF: растр страницы плюс слой аннотаций.

Координаты аннотаций — PDF-пункты (points) в системе ТЕКУЩЕЙ отображаемой
страницы, начало отсчёта в левом верхнем углу. Холст ничего не знает о
PdfSession: любые изменения модели отправляются наружу сигналами
``annotation_created`` / ``annotation_edited`` / ``annotation_deleted``, а
мутацию выполняет main_window.

Гарантия неизменяемости: холст никогда не изменяет объекты ``Annotation``,
переданные в :meth:`PageCanvas.set_page`, и не отдаёт наружу ссылки на свою
внутреннюю копию — и внутрь, и наружу данные идут через ``copy.deepcopy``.

Поля формы (AcroForm): :meth:`PageCanvas.set_form_widgets` принимает список
словарей-описаний ``PdfSession.form_widgets`` и создаёт поверх страницы
настоящие Qt-редакторы (``QLineEdit``/``QTextEdit``/``QCheckBox``/
``QRadioButton``/``QComboBox``), привязанные к странице и масштабу. Редакторы
активны только при ``set_tool("form")``; изменение значения пользователем
уходит наружу сигналом ``form_value_changed(key, export_value)``, а мутацию
PDF выполняет main_window — холст модель не меняет.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QInputDialog,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QStyle,
    QStyleOptionButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:  # импорт как пакета: from src.canvas import PageCanvas
    from .contracts import Annotation
    from .text_selection import SelectableChar, hit_test, selected_range, selected_text
except ImportError:  # импорт при sys.path, указывающем на src/
    from contracts import Annotation  # type: ignore[no-redef]
    from text_selection import SelectableChar, hit_test, selected_range, selected_text  # type: ignore[no-redef]


# --- Инструменты -----------------------------------------------------------
TOOL_SELECT = "select"
TOOL_TEXT = "text"
TOOL_HIGHLIGHT = "highlight"
TOOL_INK = "ink"
TOOL_FORM = "form"
TOOLS: tuple[str, ...] = (TOOL_SELECT, TOOL_TEXT, TOOL_HIGHLIGHT, TOOL_INK, TOOL_FORM)

# --- Поля формы ------------------------------------------------------------
FORM_KINDS: tuple[str, ...] = ("text", "checkbox", "radio", "choice", "unsupported")
FORM_FONT_MIN_PX = 8
FORM_FONT_MAX_PX = 36

#: Значение отсутствует (в отличие от ``None`` — легального значения поля).
_MISSING = object()


def _to_bool(value: Any) -> bool:
    """Привести значение поля-флажка к bool (строки "false"/"off"/"" — False)."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no", "нет")
    return bool(value)


class _FormIndicatorMixin:
    """Paint a checked glyph over stylesheets that fill the whole indicator."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        sub_element = (QStyle.SubElement.SE_RadioButtonIndicator
                       if isinstance(self, QRadioButton)
                       else QStyle.SubElement.SE_CheckBoxIndicator)
        rect = self.style().subElementRect(sub_element, option, self)
        if rect.width() < 2 or rect.height() < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("white"))
        side = min(rect.width(), rect.height())
        if isinstance(self, QRadioButton):
            radius = max(1.0, side * 0.19)
            painter.drawEllipse(rect.center(), radius, radius)
        else:
            pen = QPen(QColor("white"), max(1.2, side * 0.16))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            left, top = rect.left(), rect.top()
            painter.drawLine(QPointF(left + side * 0.22, top + side * 0.52),
                             QPointF(left + side * 0.43, top + side * 0.72))
            painter.drawLine(QPointF(left + side * 0.43, top + side * 0.72),
                             QPointF(left + side * 0.79, top + side * 0.29))
        painter.end()


class FormCheckBox(_FormIndicatorMixin, QCheckBox):
    """Native checkbox with an explicit visible checkmark."""


class FormRadioButton(_FormIndicatorMixin, QRadioButton):
    """Native radio button with an explicit visible selected dot."""

# --- Геометрия и масштаб ---------------------------------------------------
DEFAULT_ZOOM = 1.0
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
ZOOM_STEP = 1.15

HANDLE_SIZE = 8.0      # сторона маркера выделения, px экрана
HIT_TOLERANCE = 3.0    # допуск попадания мышью, px экрана
MIN_DRAW_SIZE = 2.0    # минимальный размер фигуры при рисовании, px экрана
MIN_ANNOT_SIZE = 4.0   # минимальный размер аннотации при масштабировании, пункты
MIN_TEXT_SIZE = 4.0
MAX_TEXT_SIZE = 400.0

HANDLE_CURSORS: dict[str, Qt.CursorShape] = {
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "e": Qt.CursorShape.SizeHorCursor,
    "w": Qt.CursorShape.SizeHorCursor,
}

DEFAULT_COLOR = "#2457D6"
DEFAULT_HIGHLIGHT_COLOR = "#FFD400"
DEFAULT_FONT_FAMILY = "Arial"
DEFAULT_FONT_SIZE = 14.0
HIGHLIGHT_OPACITY = 0.3

SELECTION_COLOR = "#1E88E5"
PAGE_BORDER_COLOR = "#9AA0A6"
CANVAS_BACKGROUND = "#5A5A5A"


class PageCanvas(QWidget):
    """Виджет страницы: растр + аннотации + интерактивное редактирование.

    Виджет рассчитан на размещение внутри ``QScrollArea`` с
    ``setWidgetResizable(False)``: его ``sizeHint``/``minimumSizeHint`` равны
    размеру страницы в пунктах, умноженному на текущий масштаб.
    """

    annotation_created = Signal(object)      # Annotation
    annotation_edited = Signal(int, object)  # index, Annotation (копия)
    annotation_deleted = Signal(int)         # index
    selection_changed = Signal(int)          # index или -1
    zoom_changed = Signal(float)             # новый масштаб (float)
    form_value_changed = Signal(str, object)  # key, export_value
    text_selected = Signal(str)                # selected source PDF text

    # ------------------------------------------------------------------ init
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._page_pixmap: Optional[QPixmap] = None
        self._page_size: tuple[float, float] = (0.0, 0.0)
        self._annotations: list[Annotation] = []

        self._tool: str = TOOL_SELECT
        self._font_family: str = DEFAULT_FONT_FAMILY
        self._font_size: float = DEFAULT_FONT_SIZE
        self._color: str = DEFAULT_COLOR
        self._stroke_width: float = 2.0
        self._style_explicit: bool = False

        self._zoom: float = DEFAULT_ZOOM
        self._selected: int = -1

        # Незавершённые жесты.
        self._drawing: Optional[str] = None                  # highlight | ink
        self._drag_start: Optional[tuple[float, float]] = None
        self._drag_current: Optional[tuple[float, float]] = None
        self._ink_points: list[tuple[float, float]] = []

        self._move_index: int = -1
        self._move_start: Optional[tuple[float, float]] = None
        self._move_delta: tuple[float, float] = (0.0, 0.0)
        self._move_original: Optional[Annotation] = None
        self._move_preview: Optional[Annotation] = None

        self._resize_index: int = -1
        self._resize_handle: Optional[str] = None
        self._resize_original: Optional[Annotation] = None
        self._resize_preview: Optional[Annotation] = None

        self._hover_handle: Optional[str] = None
        self._zoom_anchor: Optional[tuple[tuple[float, float], float, float]] = None

        # Поля формы: описания + inline-редакторы (параллельные списки).
        self._form_widgets: list[dict[str, Any]] = []
        self._form_editors: list[Optional[QWidget]] = []
        self._form_groups: list[QButtonGroup] = []
        self._form_groups_by_key: dict[str, QButtonGroup] = {}
        self._form_last_value: dict[int, Any] = {}
        self._form_loading: bool = False
        self._text_chars: tuple[SelectableChar, ...] = ()
        self._text_anchor: int = -1
        self._text_range: tuple[int, int] = (0, 0)
        self._text_dragging = False
        self._text_start_pos: Optional[QPointF] = None
        self._search_rects: tuple[tuple[float, float, float, float], ...] = ()

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(QFont(self._font_family))
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._sync_geometry()

    # ------------------------------------------------------------- публичное
    def set_page(
        self,
        image: Any,
        page_size: tuple[float, float],
        annotations: list[Annotation],
    ) -> None:
        """Показать новую страницу.

        ``image`` — растр страницы (PIL.Image) либо ``None``; ``page_size`` —
        размер страницы в пунктах; ``annotations`` — аннотации страницы
        (холст сохраняет глубокие копии).
        """
        self._page_pixmap = self._to_pixmap(image)
        self._page_size = self._normalize_page_size(page_size)
        self._annotations = [copy.deepcopy(item) for item in (annotations or [])]
        self._text_chars = ()
        self._text_anchor = -1
        self._text_range = (0, 0)
        self._text_dragging = False
        self._text_start_pos = None
        self._search_rects = ()

        self._cancel_interaction()
        self._zoom_anchor = None
        self._set_selected(-1)
        self._sync_geometry()
        self._layout_form_widgets()
        self._apply_cursor()
        self.update()

    def set_text_chars(self, chars: Any) -> None:
        """Attach source PDF glyph text and displayed-page point rectangles."""
        self._text_chars = tuple(
            char if isinstance(char, SelectableChar)
            else SelectableChar(str(char.value), tuple(float(v) for v in char.rect))
            for char in (chars or ())
        )
        self._text_anchor = -1
        self._text_range = (0, 0)
        self.update()

    def set_search_highlights(self, rects: Any) -> None:
        self._search_rects = tuple(tuple(float(v) for v in rect) for rect in (rects or ()))
        self.update()

    def _text_index_at(self, pos: QPointF) -> int:
        scale = max(self._zoom, 1e-6)
        return hit_test(self._text_chars, pos.x() / scale, pos.y() / scale, 6.0 / scale)

    def _begin_text_selection(self, pos: QPointF) -> bool:
        index = self._text_index_at(pos)
        if index < 0:
            return False
        self._text_anchor = index
        self._text_range = (index, index + 1)
        self._text_dragging = False
        self._text_start_pos = QPointF(pos)
        return True

    def _extend_text_selection(self, pos: QPointF) -> bool:
        if self._text_anchor < 0:
            return False
        index = self._text_index_at(pos)
        if self._text_start_pos is not None:
            self._text_dragging = self._text_dragging or math.hypot(
                pos.x() - self._text_start_pos.x(), pos.y() - self._text_start_pos.y()
            ) >= 2.0
        if index >= 0:
            self._text_range = selected_range(self._text_anchor, index, len(self._text_chars))
            self.update()
        return True

    def _finish_text_selection(self) -> bool:
        if self._text_anchor < 0:
            return False
        selected = self._text_dragging
        text = selected_text(self._text_chars, *self._text_range)
        if selected and text:
            self.text_selected.emit(text)
        self._text_anchor = -1
        self._text_dragging = False
        self._text_start_pos = None
        self.update()
        return selected

    def copy_selected_text(self) -> bool:
        text = selected_text(self._text_chars, *self._text_range)
        if not text:
            return False
        QApplication.clipboard().setText(text)
        return True

    def set_tool(self, tool: str) -> None:
        """Активировать инструмент: select | text | highlight | ink | form."""
        if tool not in TOOLS:
            raise ValueError(
                "Неизвестный инструмент: {0!r}. Допустимые значения: {1}.".format(
                    tool, ", ".join(TOOLS)
                )
            )
        if tool == self._tool:
            return
        self._cancel_interaction()  # превью незавершённого жеста скрывается
        self._tool = tool
        if tool != TOOL_SELECT:
            self._set_selected(-1)
        self._apply_tool_state()  # поля формы видны и активны только в "form"
        self._apply_cursor()
        self.update()

    def set_style(
        self,
        font_family: str,
        font_size: float,
        color: str,
        stroke_width: float = 2.0,
    ) -> None:
        """Задать стиль создаваемых аннотаций."""
        family = str(font_family or "").strip() or DEFAULT_FONT_FAMILY
        try:
            size = float(font_size)
        except (TypeError, ValueError):
            raise ValueError("Размер шрифта должен быть числом.") from None
        if not math.isfinite(size) or not (MIN_TEXT_SIZE <= size <= MAX_TEXT_SIZE):
            raise ValueError(
                "Размер шрифта должен быть в диапазоне от {0:g} до {1:g} пунктов.".format(
                    MIN_TEXT_SIZE, MAX_TEXT_SIZE
                )
            )
        qcolor = QColor(str(color or "").strip())
        if not qcolor.isValid():
            raise ValueError(
                "Некорректный цвет: {0!r}. Ожидается значение вида '#2457D6'.".format(color)
            )
        try:
            width = float(stroke_width)
        except (TypeError, ValueError):
            raise ValueError("Толщина линии должна быть числом.") from None
        if not math.isfinite(width) or not (0.1 <= width <= 50.0):
            raise ValueError("Толщина линии должна быть в диапазоне от 0.1 до 50 пунктов.")

        self._font_family = family
        self._font_size = size
        self._color = qcolor.name(QColor.NameFormat.HexRgb)
        self._stroke_width = width
        self._style_explicit = True
        self.setFont(QFont(family))
        self.update()

    def set_zoom(self, zoom: float) -> None:
        """Установить масштаб (ограничивается диапазоном MIN_ZOOM..MAX_ZOOM)."""
        try:
            value = float(zoom)
        except (TypeError, ValueError):
            raise ValueError("Масштаб должен быть числом.") from None
        if not math.isfinite(value):
            raise ValueError("Масштаб должен быть конечным числом.")
        value = min(max(value, MIN_ZOOM), MAX_ZOOM)
        if abs(value - self._zoom) < 1e-9:
            return
        self._zoom = value
        self._sync_geometry()
        self._layout_form_widgets()
        self._apply_cursor()
        self.update()

    @property
    def zoom(self) -> float:
        """Текущий масштаб отображения (1.0 = 100%)."""
        return self._zoom

    @zoom.setter
    def zoom(self, value: float) -> None:
        self.set_zoom(value)

    # --------------------------------------------------------- мелкие геттеры
    def page_rect(self) -> QRectF:
        """Прямоугольник страницы в координатах виджета (px)."""
        return QRectF(
            0.0,
            0.0,
            self._page_size[0] * self._zoom,
            self._page_size[1] * self._zoom,
        )

    def page_size(self) -> tuple[float, float]:
        """Размер отображаемой страницы в PDF-пунктах."""
        return self._page_size

    def selected_index(self) -> int:
        """Индекс выделенной аннотации или -1."""
        return self._selected

    def select_annotation(self, index: int) -> None:
        """Restore selection after the owner refreshes the page."""
        self._set_selected(index if 0 <= index < len(self._annotations) else -1)
        self.update()

    def annotation_count(self) -> int:
        """Количество аннотаций, отображаемых на странице."""
        return len(self._annotations)

    def current_tool(self) -> str:
        """Активный инструмент."""
        return self._tool

    # ------------------------------------------------------------- геометрия
    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        width = self._page_size[0] * self._zoom
        height = self._page_size[1] * self._zoom
        return QSize(max(1, int(math.ceil(width))), max(1, int(math.ceil(height))))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return self.sizeHint()

    def _sync_geometry(self) -> None:
        hint = self.sizeHint()
        if self.size() != hint:
            self.resize(hint)
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._layout_form_widgets()

    @staticmethod
    def _normalize_page_size(page_size: Any) -> tuple[float, float]:
        try:
            width = float(page_size[0])
            height = float(page_size[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return (0.0, 0.0)
        if not math.isfinite(width) or not math.isfinite(height):
            return (0.0, 0.0)
        return (max(0.0, width), max(0.0, height))

    def _has_page(self) -> bool:
        return self._page_size[0] > 0.0 and self._page_size[1] > 0.0

    def _to_pdf(self, pos: QPointF, clamp: bool = True) -> tuple[float, float]:
        """Экранные координаты виджета -> PDF-пункты страницы."""
        zoom = self._zoom if self._zoom > 1e-9 else 1.0
        x = pos.x() / zoom
        y = pos.y() / zoom
        if clamp:
            return self._clamp_point(x, y)
        return (x, y)

    def _to_widget(self, x: float, y: float) -> QPointF:
        """PDF-пункты страницы -> экранные координаты виджета."""
        return QPointF(x * self._zoom, y * self._zoom)

    def _clamp_point(self, x: float, y: float) -> tuple[float, float]:
        width, height = self._page_size
        return (min(max(x, 0.0), width), min(max(y, 0.0), height))

    def _clamp_rect(
        self, x: float, y: float, width: float, height: float
    ) -> tuple[float, float, float, float]:
        """Вписать прямоугольник в страницу, сохранив его размер."""
        page_w, page_h = self._page_size
        width = min(max(width, 0.0), page_w)
        height = min(max(height, 0.0), page_h)
        x = min(max(x, 0.0), max(page_w - width, 0.0))
        y = min(max(y, 0.0), max(page_h - height, 0.0))
        return (x, y, width, height)

    def _pdf_rect_to_widget(
        self, x: float, y: float, width: float, height: float
    ) -> QRectF:
        """Прямоугольник в PDF-пунктах -> прямоугольник в координатах виджета."""
        return QRectF(
            x * self._zoom,
            y * self._zoom,
            max(width, 0.0) * self._zoom,
            max(height, 0.0) * self._zoom,
        )

    def _rect_to_widget(self, annotation: Annotation) -> QRectF:
        return self._pdf_rect_to_widget(
            annotation.x,
            annotation.y,
            max(annotation.width, 0.0),
            max(annotation.height, 0.0),
        )

    # ------------------------------------------------------------- шрифт/текст
    def _annotation_font(self, annotation: Annotation, zoom: float = 1.0) -> QFont:
        family = annotation.font_family or self._font_family or DEFAULT_FONT_FAMILY
        font = QFont(family)
        try:
            size = float(annotation.font_size)
        except (TypeError, ValueError):
            size = DEFAULT_FONT_SIZE
        if not math.isfinite(size) or size <= 0.0:
            size = DEFAULT_FONT_SIZE
        font.setPixelSize(max(1, int(round(size * max(zoom, 1e-6)))))
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    def _measure_text(self, annotation: Annotation) -> tuple[float, float]:
        """Габариты текста аннотации в PDF-пунктах (1 пункт = 1 px метрик)."""
        text = annotation.text or ""
        if not text.strip():
            return (0.0, 0.0)
        font = self._annotation_font(annotation, zoom=1.0)
        try:
            metrics = QFontMetricsF(font, self)
        except TypeError:  # pragma: no cover - запасной вариант
            metrics = QFontMetricsF(font)
        flags = int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft)
        rect = metrics.boundingRect(QRectF(0.0, 0.0, 5000.0, 5000.0), flags, text)
        return (float(rect.width()), float(rect.height()))

    def _annotation_bounds(self, annotation: Annotation) -> tuple[float, float, float, float]:
        """Габариты аннотации в PDF-пунктах: (x, y, width, height)."""
        if annotation.kind == TOOL_INK and annotation.points:
            xs = [float(p[0]) for p in annotation.points]
            ys = [float(p[1]) for p in annotation.points]
            pad = max(float(annotation.stroke_width), 1.0) / 2.0
            return (
                min(xs) - pad,
                min(ys) - pad,
                max(xs) - min(xs) + 2.0 * pad,
                max(ys) - min(ys) + 2.0 * pad,
            )
        width = max(float(annotation.width), 0.0)
        height = max(float(annotation.height), 0.0)
        if annotation.kind == TOOL_TEXT:
            measured_w, measured_h = self._measure_text(annotation)
            width = max(width, measured_w)
            height = max(height, measured_h)
        return (float(annotation.x), float(annotation.y), width, height)

    # --------------------------------------------------------------- растр
    @staticmethod
    def _to_pixmap(image: Any) -> Optional[QPixmap]:
        """PIL.Image -> QPixmap (без потери резкости, с копией буфера)."""
        if image is None:
            return None
        try:
            mode = getattr(image, "mode", "RGB")
            source = image
            if mode not in ("RGB", "RGBA"):
                source = image.convert("RGBA")
                mode = "RGBA"
            width, height = int(source.size[0]), int(source.size[1])
            if width <= 0 or height <= 0:
                return None
            stride = width * (4 if mode == "RGBA" else 3)
            fmt = (
                QImage.Format.Format_RGBA8888
                if mode == "RGBA"
                else QImage.Format.Format_RGB888
            )
            data = source.tobytes("raw", mode)
            qimage = QImage(data, width, height, stride, fmt)
            if qimage.isNull():
                return None
            return QPixmap.fromImage(qimage.copy())
        except Exception:  # pragma: no cover - растр не критичен для работы
            return None

    # ------------------------------------------------------------- отрисовка
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if not self._has_page():
            painter.fillRect(self.rect(), QColor(CANVAS_BACKGROUND))
            painter.end()
            return

        page = self.page_rect()
        painter.fillRect(page, QColor("#FFFFFF"))
        if self._page_pixmap is not None and not self._page_pixmap.isNull():
            painter.drawPixmap(page, self._page_pixmap, QRectF(self._page_pixmap.rect()))

        # Слой аннотаций: перемещаемая/масштабируемая рисуется превью ниже.
        for index, annotation in enumerate(self._annotations):
            if index in (self._move_index, self._resize_index):
                continue
            self._paint_annotation(painter, annotation)

        self._paint_text_rects(painter, self._search_rects, QColor(255, 204, 0, 100),
                               QColor(220, 150, 0, 150))
        start, end = self._text_range
        if self._text_dragging and end > start:
            rects = [char.rect for char in self._text_chars[start:end]]
            self._paint_text_rects(painter, rects, QColor(70, 145, 245, 105),
                                   QColor(42, 110, 220, 160))

        preview: Optional[Annotation] = None
        if self._move_preview is not None:
            preview = self._move_preview
        elif self._resize_preview is not None:
            preview = self._resize_preview
        if preview is not None:
            self._paint_annotation(painter, preview, ghost=True)

        self._paint_drawing_preview(painter)

        if self._tool == TOOL_SELECT:
            if preview is not None:
                self._paint_selection(painter, self._bounds_to_widget(preview))
            elif 0 <= self._selected < len(self._annotations):
                self._paint_selection(
                    painter, self._bounds_to_widget(self._annotations[self._selected])
                )

        self._paint_form_fields(painter)

        painter.setPen(QPen(QColor(PAGE_BORDER_COLOR), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(page.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def _paint_text_rects(self, painter: QPainter, rects: Any, fill: QColor,
                          outline: QColor) -> None:
        painter.save()
        painter.setPen(QPen(outline, 0.7))
        painter.setBrush(QBrush(fill))
        for x1, y1, x2, y2 in rects:
            if x2 > x1 and y2 > y1:
                painter.drawRect(QRectF(x1 * self._zoom, y1 * self._zoom,
                                        (x2 - x1) * self._zoom, (y2 - y1) * self._zoom))
        painter.restore()

    def _bounds_to_widget(self, annotation: Annotation) -> QRectF:
        x, y, width, height = self._annotation_bounds(annotation)
        return QRectF(x * self._zoom, y * self._zoom, width * self._zoom, height * self._zoom)

    def _paint_annotation(
        self, painter: QPainter, annotation: Annotation, ghost: bool = False
    ) -> None:
        base = QColor(annotation.color or DEFAULT_COLOR)
        if not base.isValid():
            base = QColor(DEFAULT_COLOR)
        try:
            opacity = float(annotation.opacity)
        except (TypeError, ValueError):
            opacity = 1.0
        opacity = min(max(opacity, 0.0), 1.0)
        if ghost:
            opacity *= 0.8
        color = QColor(base)
        color.setAlphaF(opacity)

        if annotation.kind == TOOL_INK:
            self._paint_ink(painter, annotation, color)
        elif annotation.kind == TOOL_HIGHLIGHT:
            rect = self._rect_to_widget(annotation)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRect(rect)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif annotation.kind == TOOL_TEXT:
            self._paint_text(painter, annotation, color)
        else:  # неизвестный тип — показываем рамку-заглушку
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._rect_to_widget(annotation))

    def _paint_text(self, painter: QPainter, annotation: Annotation, color: QColor) -> None:
        text = annotation.text or ""
        if not text:
            return
        painter.setPen(QPen(color))
        painter.setFont(self._annotation_font(annotation, self._zoom))
        rect = self._rect_to_widget(annotation)
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            width, height = self._measure_text(annotation)
            rect = QRectF(
                rect.x(),
                rect.y(),
                max(width, 1.0) * self._zoom,
                max(height, 1.0) * self._zoom,
            )
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        if annotation.width > 0.0:
            flags |= int(Qt.TextFlag.TextWordWrap)
        painter.drawText(rect, flags, text)

    def _paint_ink(self, painter: QPainter, annotation: Annotation, color: QColor) -> None:
        points = [self._to_widget(float(p[0]), float(p[1])) for p in annotation.points or []]
        if not points:
            return
        try:
            width = float(annotation.stroke_width)
        except (TypeError, ValueError):
            width = 2.0
        pen = QPen(
            color,
            max(1.0, width * self._zoom),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if len(points) == 1:
            painter.drawPoint(points[0])
        else:
            painter.drawPolyline(QPolygonF(points))

    def _paint_drawing_preview(self, painter: QPainter) -> None:
        if self._drawing == TOOL_HIGHLIGHT and self._drag_start and self._drag_current:
            rect = self._rect_from_points(self._drag_start, self._drag_current)
            if rect is None:
                return
            fill = QColor(self._style_color(TOOL_HIGHLIGHT))
            fill.setAlphaF(HIGHLIGHT_OPACITY)
            painter.setPen(QPen(QColor(SELECTION_COLOR), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QBrush(fill))
            painter.drawRect(self._pdf_rect_to_widget(*rect))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            return

        if self._drawing == TOOL_INK and self._ink_points:
            points = [self._to_widget(x, y) for x, y in self._ink_points]
            preview = Annotation(
                kind=TOOL_INK,
                color=self._style_color(TOOL_INK),
                stroke_width=self._stroke_width,
                opacity=0.85,
                points=list(self._ink_points),
            )
            if len(points) == 1:
                pen = QPen(QColor(preview.color), max(1.0, self._stroke_width * self._zoom))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawPoint(points[0])
            else:
                self._paint_ink(painter, preview, QColor(preview.color))
            painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_selection(self, painter: QPainter, bounds: QRectF) -> None:
        painter.setPen(QPen(QColor(SELECTION_COLOR), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(bounds)
        painter.setPen(QPen(QColor(SELECTION_COLOR), 1))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        for rect in self._handle_rects(bounds).values():
            painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    # ------------------------------------------------------- выделение/маркеры
    @staticmethod
    def _handle_rects(bounds: QRectF) -> dict[str, QRectF]:
        half = HANDLE_SIZE / 2.0
        left, top = bounds.left(), bounds.top()
        right, bottom = bounds.right(), bounds.bottom()
        middle_x = (left + right) / 2.0
        middle_y = (top + bottom) / 2.0
        anchors = {
            "nw": (left, top),
            "n": (middle_x, top),
            "ne": (right, top),
            "e": (right, middle_y),
            "se": (right, bottom),
            "s": (middle_x, bottom),
            "sw": (left, bottom),
            "w": (left, middle_y),
        }
        return {
            key: QRectF(px - half, py - half, HANDLE_SIZE, HANDLE_SIZE)
            for key, (px, py) in anchors.items()
        }

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        if self._tool != TOOL_SELECT:
            return None
        bounds = self._selection_bounds_widget()
        if bounds is None:
            return None
        for key, rect in self._handle_rects(bounds).items():
            if rect.adjusted(-1.0, -1.0, 1.0, 1.0).contains(pos):
                return key
        return None

    def _selection_bounds_widget(self) -> Optional[QRectF]:
        if self._move_preview is not None:
            return self._bounds_to_widget(self._move_preview)
        if self._resize_preview is not None:
            return self._bounds_to_widget(self._resize_preview)
        if 0 <= self._selected < len(self._annotations):
            return self._bounds_to_widget(self._annotations[self._selected])
        return None

    def _annotation_at(self, pos: QPointF) -> int:
        """Индекс верхней аннотации под курсором или -1."""
        if not self._has_page():
            return -1
        x, y = self._to_pdf(pos, clamp=False)
        tolerance = HIT_TOLERANCE / max(self._zoom, 1e-6)
        for index in range(len(self._annotations) - 1, -1, -1):
            bx, by, bw, bh = self._annotation_bounds(self._annotations[index])
            if (
                bx - tolerance <= x <= bx + bw + tolerance
                and by - tolerance <= y <= by + bh + tolerance
            ):
                return index
        return -1

    # ------------------------------------------------------------- курсоры
    def _apply_cursor(self, pos: Optional[QPointF] = None) -> None:
        if not self._has_page():
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self._drawing:
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self._move_index >= 0:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self._resize_handle:
            self.setCursor(HANDLE_CURSORS.get(self._resize_handle, Qt.CursorShape.ArrowCursor))
            return
        if self._tool == TOOL_FORM:
            # Поля формы перехватывают клики сами; фон — обычная стрелка.
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self._tool == TOOL_TEXT:
            self.setCursor(Qt.CursorShape.IBeamCursor)
            return
        if self._tool in (TOOL_HIGHLIGHT, TOOL_INK):
            self.setCursor(Qt.CursorShape.CrossCursor)
            return

        if pos is None:
            pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        handle = self._handle_at(pos)
        self._hover_handle = handle
        if handle:
            self.setCursor(HANDLE_CURSORS.get(handle, Qt.CursorShape.ArrowCursor))
        elif self._annotation_at(pos) >= 0:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._hover_handle = None
        if self._move_index < 0 and self._resize_handle is None and not self._drawing:
            self._apply_cursor()
        super().leaveEvent(event)

    # ------------------------------------------------------------ мышь: нажатие
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if not self._has_page() or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        if not self.page_rect().contains(pos):
            # вне страницы ничего не создаём
            super().mousePressEvent(event)
            return

        self.setFocus(Qt.FocusReason.MouseFocusReason)
        tool = self._tool

        if tool == TOOL_FORM:
            # Редакторы полей — дочерние виджеты, они получают клики первыми;
            # клик по фону лишь снимает фокус, аннотации не создаются.
            event.accept()
            return

        if tool == TOOL_SELECT:
            handle = self._handle_at(pos)
            if handle is not None and 0 <= self._selected < len(self._annotations):
                self._begin_resize(self._selected, handle)
                event.accept()
                return
            index = self._annotation_at(pos)
            if index >= 0:
                self._set_selected(index)
                self._begin_move(index, self._to_pdf(pos))
                event.accept()
                return
            self._set_selected(-1)
            event.accept()
            return

        if tool == TOOL_TEXT:
            self._create_text_at(pos)
            event.accept()
            return

        if tool == TOOL_HIGHLIGHT:
            point = self._to_pdf(pos)
            self._drawing = TOOL_HIGHLIGHT
            self._drag_start = point
            self._drag_current = point
            self._apply_cursor(pos)
            self.update()
            event.accept()
            return

        if tool == TOOL_INK:
            point = self._to_pdf(pos)
            self._drawing = TOOL_INK
            self._ink_points = [point]
            self._drag_start = point
            self._drag_current = point
            self._apply_cursor(pos)
            self.update()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        pos = event.position()

        if self._drawing == TOOL_INK:
            point = self._to_pdf(pos)
            threshold = 1.0 / max(self._zoom, 1e-6)
            if not self._ink_points or self._distance(self._ink_points[-1], point) >= threshold:
                self._ink_points.append(point)
                self._drag_current = point
                self.update()
            event.accept()
            return

        if self._drawing == TOOL_HIGHLIGHT:
            self._drag_current = self._to_pdf(pos)
            self.update()
            event.accept()
            return

        if self._move_index >= 0 and self._move_start is not None:
            current = self._to_pdf(pos)
            self._move_delta = (
                current[0] - self._move_start[0],
                current[1] - self._move_start[1],
            )
            self._update_move_preview()
            self.update()
            event.accept()
            return

        if self._resize_index >= 0:
            self._update_resize_preview(self._to_pdf(pos))
            self.update()
            event.accept()
            return

        self._apply_cursor(pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        handled = False

        if self._drawing == TOOL_INK:
            points = list(self._ink_points)
            self._drawing = None
            self._ink_points = []
            self._drag_start = None
            self._drag_current = None
            handled = True
            if len(points) >= 2:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                annotation = Annotation(
                    kind=TOOL_INK,
                    x=min(xs),
                    y=min(ys),
                    width=max(xs) - min(xs),
                    height=max(ys) - min(ys),
                    color=self._style_color(TOOL_INK),
                    font_family=self._font_family,
                    font_size=self._font_size,
                    stroke_width=self._stroke_width,
                    opacity=1.0,
                    points=points,
                )
                self._emit_created(annotation)

        elif self._drawing == TOOL_HIGHLIGHT:
            start, end = self._drag_start, self._drag_current
            self._drawing = None
            self._drag_start = None
            self._drag_current = None
            handled = True
            rect = self._rect_from_points(start, end)
            if rect is not None:
                x, y, width, height = rect
                if (
                    width * self._zoom >= MIN_DRAW_SIZE
                    and height * self._zoom >= MIN_DRAW_SIZE
                ):
                    annotation = Annotation(
                        kind=TOOL_HIGHLIGHT,
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        color=self._style_color(TOOL_HIGHLIGHT),
                        font_family=self._font_family,
                        font_size=self._font_size,
                        stroke_width=self._stroke_width,
                        opacity=HIGHLIGHT_OPACITY,
                    )
                    self._emit_created(annotation)

        elif self._move_index >= 0:
            handled = True
            index = self._move_index
            preview = self._move_preview
            original = self._move_original
            self._move_index = -1
            self._move_start = None
            self._move_delta = (0.0, 0.0)
            self._move_original = None
            self._move_preview = None
            if preview is not None and original is not None and self._changed(original, preview):
                self._emit_edited(index, preview)

        elif self._resize_index >= 0:
            handled = True
            index = self._resize_index
            preview = self._resize_preview
            original = self._resize_original
            self._resize_index = -1
            self._resize_handle = None
            self._resize_original = None
            self._resize_preview = None
            if preview is not None and original is not None and self._changed(original, preview):
                self._emit_edited(index, preview)

        self._apply_cursor(event.position())
        self.update()
        if not handled:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if (
            self._tool != TOOL_SELECT
            or not self._has_page()
            or event.button() != Qt.MouseButton.LeftButton
        ):
            super().mouseDoubleClickEvent(event)
            return
        index = self._annotation_at(event.position())
        if index < 0 or self._annotations[index].kind != TOOL_TEXT:
            super().mouseDoubleClickEvent(event)
            return
        self._set_selected(index)
        self._edit_text(index)
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().y() * 8
            if delta == 0:
                event.ignore()
                return
            steps = delta / 120.0
            target = self._zoom * (ZOOM_STEP ** steps)
            target = min(max(target, MIN_ZOOM), MAX_ZOOM)
            if abs(target - self._zoom) > 1e-9:
                self._prepare_zoom_anchor(event.position())
                self.set_zoom(target)
                self._schedule_zoom_anchor()
                # Масштаб уже применён холстом; значение в сигнале — итоговое.
                self.zoom_changed.emit(self._zoom)
            event.accept()
            return
        event.ignore()  # обычная прокрутка уходит в QScrollArea

    # --------------------------------------------- привязка зума к курсору
    def _scroll_area(self) -> Optional[QScrollArea]:
        """Ближайший QScrollArea-предок (холст живёт внутри viewport)."""
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QScrollArea):
                return widget
            widget = widget.parentWidget()
        return None

    def _prepare_zoom_anchor(self, pos: QPointF) -> None:
        """Запомнить точку страницы под курсором, чтобы сохранить её при зуме."""
        self._zoom_anchor = None
        area = self._scroll_area()
        if area is None:
            return
        try:
            viewport_pos = self.mapTo(
                area.viewport(), QPoint(int(round(pos.x())), int(round(pos.y())))
            )
        except RuntimeError:  # pragma: no cover - виджет уже удалён
            return
        self._zoom_anchor = (
            self._to_pdf(pos, clamp=False),
            float(viewport_pos.x()),
            float(viewport_pos.y()),
        )

    def _schedule_zoom_anchor(self) -> None:
        if self._zoom_anchor is not None:
            QTimer.singleShot(0, self._apply_zoom_anchor)

    def _apply_zoom_anchor(self) -> None:
        anchor = self._zoom_anchor
        self._zoom_anchor = None
        if anchor is None:
            return
        area = self._scroll_area()
        if area is None:
            return
        (pdf_x, pdf_y), viewport_x, viewport_y = anchor
        target_x = pdf_x * self._zoom - viewport_x
        target_y = pdf_y * self._zoom - viewport_y
        try:
            area.horizontalScrollBar().setValue(int(round(target_x)))
            area.verticalScrollBar().setValue(int(round(target_y)))
        except RuntimeError:  # pragma: no cover - виджет уже удалён
            return

    # ------------------------------------------------------------- клавиатура
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        key = event.key()

        if self._tool == TOOL_FORM and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            back = key == Qt.Key.Key_Backtab or bool(
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            if self._focus_form_step(back):
                event.accept()
                return

        if key == Qt.Key.Key_Escape:
            self._cancel_interaction()
            self._set_selected(-1)
            self._apply_cursor()
            self.update()
            event.accept()
            return

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected >= 0:
            self._delete_selected()
            event.accept()
            return

        if self._selected >= 0 and key in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx = (-step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0.0)
            dy = (-step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0.0)
            self._nudge_selected(dx, dy)
            event.accept()
            return

        super().keyPressEvent(event)

    # ------------------------------------------------------- жесты: черновики
    def _cancel_interaction(self) -> None:
        """Сбросить незавершённый жест и скрыть превью."""
        self._drawing = None
        self._drag_start = None
        self._drag_current = None
        self._ink_points = []
        self._move_index = -1
        self._move_start = None
        self._move_delta = (0.0, 0.0)
        self._move_original = None
        self._move_preview = None
        self._resize_index = -1
        self._resize_handle = None
        self._resize_original = None
        self._resize_preview = None

    def _begin_move(self, index: int, pointer: tuple[float, float]) -> None:
        self._move_index = index
        self._move_start = pointer
        self._move_delta = (0.0, 0.0)
        self._move_original = copy.deepcopy(self._annotations[index])
        self._move_preview = None
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _begin_resize(self, index: int, handle: str) -> None:
        self._resize_index = index
        self._resize_handle = handle
        self._resize_original = copy.deepcopy(self._annotations[index])
        self._resize_preview = None
        self.setCursor(HANDLE_CURSORS.get(handle, Qt.CursorShape.ArrowCursor))

    def _update_move_preview(self) -> None:
        if self._move_original is None:
            return
        self._move_preview = self._translate(
            self._move_original, self._move_delta[0], self._move_delta[1]
        )

    def _update_resize_preview(self, pointer: tuple[float, float]) -> None:
        if self._resize_original is None or self._resize_handle is None:
            return
        rect = self._resized_rect(self._resize_original, self._resize_handle, pointer)
        self._resize_preview = self._apply_resize(
            self._resize_original, self._resize_handle, rect
        )

    # ------------------------------------------------------ преобразования
    def _translate(self, annotation: Annotation, dx: float, dy: float) -> Annotation:
        """Копия аннотации, сдвинутая на (dx, dy) и вписанная в страницу."""
        bx, by, bw, bh = self._annotation_bounds(annotation)
        nx, ny, _, _ = self._clamp_rect(bx + dx, by + dy, bw, bh)
        real_dx, real_dy = nx - bx, ny - by
        result = copy.deepcopy(annotation)
        result.x = float(annotation.x) + real_dx
        result.y = float(annotation.y) + real_dy
        if result.kind == TOOL_INK and result.points:
            result.points = [
                (float(px) + real_dx, float(py) + real_dy) for px, py in result.points
            ]
        return result

    def _resized_rect(
        self,
        annotation: Annotation,
        handle: str,
        pointer: tuple[float, float],
    ) -> tuple[float, float, float, float]:
        bx, by, bw, bh = self._annotation_bounds(annotation)
        left, top = bx, by
        right, bottom = bx + bw, by + bh
        px, py = pointer

        if "n" in handle:
            top = min(py, bottom - MIN_ANNOT_SIZE)
        if "s" in handle:
            bottom = max(py, top + MIN_ANNOT_SIZE)
        if "w" in handle:
            left = min(px, right - MIN_ANNOT_SIZE)
        if "e" in handle:
            right = max(px, left + MIN_ANNOT_SIZE)

        page_w, page_h = self._page_size
        left = min(max(left, 0.0), page_w)
        right = min(max(right, 0.0), page_w)
        top = min(max(top, 0.0), page_h)
        bottom = min(max(bottom, 0.0), page_h)

        if right - left < MIN_ANNOT_SIZE:
            if "w" in handle:
                left = max(0.0, right - MIN_ANNOT_SIZE)
            else:
                right = min(page_w, left + MIN_ANNOT_SIZE)
        if bottom - top < MIN_ANNOT_SIZE:
            if "n" in handle:
                top = max(0.0, bottom - MIN_ANNOT_SIZE)
            else:
                bottom = min(page_h, top + MIN_ANNOT_SIZE)

        return (left, top, max(right - left, 0.0), max(bottom - top, 0.0))

    def _apply_resize(
        self,
        annotation: Annotation,
        handle: str,
        rect: tuple[float, float, float, float],
    ) -> Annotation:
        """Копия аннотации с новой геометрией (для ink точки пересчитываются)."""
        x, y, width, height = rect
        result = copy.deepcopy(annotation)
        bx, by, bw, bh = self._annotation_bounds(annotation)

        if result.kind == TOOL_INK and result.points:
            scale_x = (width / bw) if bw > 1e-6 else 1.0
            scale_y = (height / bh) if bh > 1e-6 else 1.0
            points = [
                (x + (float(px) - bx) * scale_x, y + (float(py) - by) * scale_y)
                for px, py in result.points
            ]
            result.points = points
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            result.x, result.y = min(xs), min(ys)
            result.width, result.height = max(xs) - min(xs), max(ys) - min(ys)
            return result

        # Для текста вертикальные маркеры меняют кегль, горизонтальные — ширину переноса.
        if result.kind == TOOL_TEXT and bh > 1e-6 and ("n" in handle or "s" in handle):
            scaled = float(annotation.font_size) * (height / bh)
            result.font_size = min(max(scaled, MIN_TEXT_SIZE), MAX_TEXT_SIZE)

        result.x, result.y, result.width, result.height = x, y, width, height
        return result

    def _rect_from_points(
        self,
        start: Optional[tuple[float, float]],
        end: Optional[tuple[float, float]],
    ) -> Optional[tuple[float, float, float, float]]:
        if start is None or end is None:
            return None
        left, right = sorted((start[0], end[0]))
        top, bottom = sorted((start[1], end[1]))
        page_w, page_h = self._page_size
        left = min(max(left, 0.0), page_w)
        right = min(max(right, 0.0), page_w)
        top = min(max(top, 0.0), page_h)
        bottom = min(max(bottom, 0.0), page_h)
        if right < left or bottom < top:
            return None
        return (left, top, right - left, bottom - top)

    @staticmethod
    def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(b[0] - a[0], b[1] - a[1])

    @staticmethod
    def _changed(before: Annotation, after: Annotation) -> bool:
        if before.kind == TOOL_INK or after.kind == TOOL_INK:
            return before.points != after.points or (before.x, before.y) != (after.x, after.y)
        return (
            abs(before.x - after.x) > 1e-6
            or abs(before.y - after.y) > 1e-6
            or abs(before.width - after.width) > 1e-6
            or abs(before.height - after.height) > 1e-6
            or abs(before.font_size - after.font_size) > 1e-6
        )

    # ------------------------------------------------------------- действия
    def _create_text_at(self, pos: QPointF) -> None:
        text = self._prompt_text("Добавить текст", "")
        if text is None:
            return
        x, y = self._to_pdf(pos)
        annotation = Annotation(
            kind=TOOL_TEXT,
            x=x,
            y=y,
            color=self._style_color(TOOL_TEXT),
            font_family=self._font_family,
            font_size=self._font_size,
            stroke_width=self._stroke_width,
            opacity=1.0,
            text=text,
        )
        width, height = self._measure_text(annotation)
        annotation.width = max(width, 1.0)
        annotation.height = max(height, 1.0)
        box = self._clamp_rect(annotation.x, annotation.y, annotation.width, annotation.height)
        annotation.x, annotation.y, annotation.width, annotation.height = box
        self._emit_created(annotation)

    def _edit_text(self, index: int) -> None:
        if not (0 <= index < len(self._annotations)):
            return
        annotation = self._annotations[index]
        text = self._prompt_text("Изменить текст", annotation.text or "")
        if text is None:
            return
        updated = copy.deepcopy(annotation)
        updated.text = text
        width, height = self._measure_text(updated)
        updated.width = max(width, 1.0)
        updated.height = max(height, 1.0)
        box = self._clamp_rect(updated.x, updated.y, updated.width, updated.height)
        updated.x, updated.y, updated.width, updated.height = box
        self._emit_edited(index, updated)

    def _prompt_text(self, title: str, initial: str) -> Optional[str]:
        """Многострочный ввод с русским интерфейсом; None — отмена/пусто."""
        text, accepted = QInputDialog.getMultiLineText(
            self, title, "Текст аннотации:", initial or ""
        )
        if not accepted or text is None:
            return None
        text = text.rstrip()
        if not text.strip():
            return None
        return text

    def _delete_selected(self) -> None:
        index = self._selected
        if not (0 <= index < len(self._annotations)):
            return
        self._cancel_interaction()
        del self._annotations[index]
        self._selected = -1
        self.annotation_deleted.emit(index)
        self.selection_changed.emit(-1)
        self._apply_cursor()
        self.update()

    def _nudge_selected(self, dx: float, dy: float) -> None:
        index = self._selected
        if not (0 <= index < len(self._annotations)):
            return
        moved = self._translate(self._annotations[index], dx, dy)
        if self._changed(self._annotations[index], moved):
            self._emit_edited(index, moved)

    def _emit_created(self, annotation: Annotation) -> None:
        """Локальная копия + сигнал с независимой копией (модель меняет main_window)."""
        self._annotations.append(copy.deepcopy(annotation))
        self.annotation_created.emit(copy.deepcopy(annotation))
        self.update()

    def _emit_edited(self, index: int, annotation: Annotation) -> None:
        if 0 <= index < len(self._annotations):
            self._annotations[index] = copy.deepcopy(annotation)
        self.annotation_edited.emit(index, copy.deepcopy(annotation))
        self.update()

    def _set_selected(self, index: int) -> None:
        if not (0 <= index < len(self._annotations)):
            index = -1
        if index == self._selected:
            return
        self._selected = index
        self.selection_changed.emit(index)

    # ------------------------------------------------------------------ формы
    def set_form_widgets(self, widgets: Any) -> None:
        """Показать поля формы текущей страницы (заменяет предыдущий набор).

        ``widgets`` — список словарей контракта ``PdfSession.form_widgets``::

            {"key": str, "rect": [x, y, w, h], "kind": str, "value": Any,
             "options": list[str], "readonly": bool, "multiline": bool,
             "export_value": Any}

        ``rect`` — PDF-пункты в системе отображаемой страницы (начало в левом
        верхнем углу), те же координаты, что у аннотаций. Для ``kind="radio"``
        ``value`` — bool (выбран ли этот переключатель); виджеты с одинаковым
        ``key`` образуют одну взаимоисключающую группу. Если у radio-поля задан
        непустой ``options`` и строковое ``value``, внутри прямоугольника
        создаётся группа переключателей по вариантам.

        Некорректные элементы молча пропускаются, данные копируются. Редакторы
        создаются сразу, но видны и активны только при ``set_tool("form")``.
        """
        if widgets is None:
            source: list[Any] = []
        elif isinstance(widgets, (list, tuple)):
            source = list(widgets)
        else:
            try:
                source = list(widgets)
            except TypeError:
                source = []

        items: list[dict[str, Any]] = []
        for raw in source:
            normalized = self._normalize_form_widget(raw)
            if normalized is not None:
                items.append(normalized)

        focus_key = self._focused_form_key()
        # Appearance and navigation metadata can become available after the
        # first value edit (for example when the backend creates an /AP stream).
        # They affect layout, but not the editor's control type. Rebuilding the
        # controls for those metadata-only changes during textChanged destroys
        # the active IME/cursor and drops subsequent keystrokes.
        editor_shape_keys = (
            "key", "kind", "options", "readonly", "multiline", "editable",
        )
        shape = lambda widget: tuple(widget.get(key) for key in editor_shape_keys)
        if (len(items) == len(self._form_widgets) == len(self._form_editors)
                and all(shape(new) == shape(old) for new, old in zip(items, self._form_widgets))):
            self._form_widgets = items
            self._form_loading = True
            controls = [editor for editor in self._form_editors if editor is not None]
            controls += [child for editor in controls for child in editor.findChildren(QRadioButton)]
            blocked = [(control, control.blockSignals(True)) for control in controls]
            try:
                for widget, editor in zip(items, self._form_editors):
                    value = widget['value']
                    if isinstance(editor, QLineEdit) and editor.text() != str(value):
                        editor.setText(str(value))
                    elif isinstance(editor, QTextEdit) and editor.toPlainText() != str(value):
                        editor.setPlainText(str(value))
                    elif isinstance(editor, (QCheckBox, QRadioButton)):
                        editor.setChecked(bool(value))
                    elif isinstance(editor, QComboBox) and editor.currentText() != str(value):
                        editor.setCurrentText(str(value))
                    elif editor is not None and widget['kind'] == 'radio':
                        for button in editor.findChildren(QRadioButton):
                            button.setChecked(button.text() == str(value))
            finally:
                for control, previous in blocked:
                    control.blockSignals(previous)
                self._form_loading = False
            self._layout_form_widgets()
            self.update()
            return
        self._form_widgets = items
        self._rebuild_form_editors()
        self._layout_form_widgets()
        if focus_key is not None:
            self.focus_form_field(focus_key)
        self.update()

    def form_widget_count(self) -> int:
        """Количество полей формы, принятых последним ``set_form_widgets``."""
        return len(self._form_widgets)

    def form_value(self, key: str) -> Any:
        """Текущее значение поля (копия) или ``None``, если поля нет."""
        wanted = str(key)
        for widget in self._form_widgets:
            if widget["key"] == wanted:
                return copy.deepcopy(widget["value"])
        return None

    def form_editor(self, key: str) -> Optional[QWidget]:
        """Inline-редактор поля (для диагностики/тестов) или ``None``."""
        wanted = str(key)
        for index, widget in enumerate(self._form_widgets):
            if widget["key"] != wanted or index >= len(self._form_editors):
                continue
            return self._form_editors[index]
        return None

    def focus_form_field(self, key: str) -> bool:
        """Перевести фокус на поле ``key``; ``False`` — поля нет.

        Если активен другой инструмент, формы включаются (``set_tool("form")``),
        иначе редактор был бы скрыт и не смог получить фокус.
        """
        wanted = str(key)
        target: Optional[int] = None
        for index, widget in enumerate(self._form_widgets):
            if widget["key"] == wanted and index < len(self._form_editors):
                if self._form_editors[index] is not None:
                    target = index
                    break
        if target is None:
            return False
        if self._tool != TOOL_FORM:
            self.set_tool(TOOL_FORM)
        editor = self._form_editors[target]
        if editor is None:  # pragma: no cover - защита от гонки
            return False
        self._focus_editor(editor)
        return True

    # --------------------------------------------------- нормализация полей
    @staticmethod
    def _normalize_form_widget(raw: Any) -> Optional[dict[str, Any]]:
        """Проверить и скопировать один словарь-описание поля."""
        if not isinstance(raw, dict):
            return None
        key = raw.get("key")
        key = "" if key is None else str(key).strip()
        if not key:
            return None
        rect = raw.get("rect")
        try:
            x, y, width, height = (float(rect[i]) for i in range(4))
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if not all(math.isfinite(value) for value in (x, y, width, height)):
            return None

        kind = raw.get("kind")
        kind = "" if kind is None else str(kind)
        if kind not in FORM_KINDS:
            kind = "unsupported"

        options: list[str] = []
        raw_options = raw.get("options")
        if isinstance(raw_options, (list, tuple)):
            for option in raw_options:
                if option is None:
                    continue
                text = str(option)
                if text not in options:
                    options.append(text)

        value = raw.get("value")
        if kind == "radio":
            # A widget with its own export value represents one physical button,
            # even when the backend exposes the group's selected string.
            export = raw.get("export_value")
            if isinstance(value, str) and isinstance(export, str) and export:
                value = value == export
            else:
                value = value if isinstance(value, str) else _to_bool(value)
        elif kind == "checkbox":
            value = _to_bool(value)
        elif kind in ("text", "choice"):
            value = "" if value is None else str(value)

        normalized = {
            "key": key,
            "x": max(x, 0.0),
            "y": max(y, 0.0),
            "width": max(width, 0.0),
            "height": max(height, 0.0),
            "kind": kind,
            "value": value,
            "options": options,
            "readonly": bool(raw.get("readonly", False)),
            "multiline": bool(raw.get("multiline", False)),
            "editable": bool(raw.get("editable", False)),
            "export": copy.deepcopy(raw.get("export_value", None)),
            "off_export": copy.deepcopy(raw.get("off_value", None)),
        }
        if raw.get("tab_order") is not None:
            try:
                normalized["tab_order"] = int(raw["tab_order"])
            except (TypeError, ValueError, OverflowError):
                pass
        if raw.get("font_size") is not None:
            try:
                normalized["font_size"] = max(0.0, float(raw["font_size"]))
            except (TypeError, ValueError):
                pass
        if raw.get("font_family"):
            normalized["font_family"] = str(raw["font_family"])
        if raw.get("alignment") in ("left", "center", "right"):
            normalized["alignment"] = str(raw["alignment"])
        return normalized

    # --------------------------------------------------- создание редакторов
    def _rebuild_form_editors(self) -> None:
        self._form_loading = True
        try:
            for editor in self._form_editors:
                if editor is None:
                    continue
                for child in editor.findChildren(QWidget):
                    child.removeEventFilter(self)
                editor.removeEventFilter(self)
                editor.hide()
                editor.setParent(None)
                editor.deleteLater()
            self._form_editors = []
            for group in self._form_groups:
                group.deleteLater()
            self._form_groups = []
            self._form_groups_by_key = {}
            self._form_last_value = {}
            for index, widget in enumerate(self._form_widgets):
                self._form_editors.append(self._create_form_editor(index, widget))
        finally:
            self._form_loading = False

    def _create_form_editor(self, index: int, widget: dict[str, Any]) -> Optional[QWidget]:
        kind = widget["kind"]
        if kind == "unsupported":
            return None  # такое поле только подсказываем рамкой в paintEvent

        editor: Optional[QWidget] = None
        if kind == "text":
            if widget["multiline"]:
                text_edit = QTextEdit(self)
                text_edit.setAcceptRichText(False)
                text_edit.setTabChangesFocus(True)
                text_edit.setPlainText(str(widget["value"] or ""))
                text_edit.setReadOnly(bool(widget["readonly"]))
                text_edit.textChanged.connect(
                    lambda i=index, e=text_edit: self._on_multiline_edited(i, e.toPlainText())
                )
                editor = text_edit
            else:
                line_edit = QLineEdit(self)
                line_edit.setText(str(widget["value"] or ""))
                line_edit.setReadOnly(bool(widget["readonly"]))
                line_edit.textEdited.connect(
                    lambda text, i=index: self._on_line_edited(i, text)
                )
                line_edit.editingFinished.connect(lambda i=index: self._on_editing_finished(i))
                editor = line_edit
        elif kind == "checkbox":
            check = FormCheckBox(self)
            check.setText("")
            check.setChecked(_to_bool(widget["value"]))
            check.setEnabled(not widget["readonly"])
            check.toggled.connect(
                lambda state, i=index: self._on_checkbox_toggled(i, bool(state))
            )
            editor = check
        elif kind == "radio":
            editor = self._create_radio_editor(index, widget)
        elif kind == "choice":
            editor = self._create_choice_editor(index, widget)

        if editor is not None:
            # Фильтр нужен и вложенным редакторам (QLineEdit внутри
            # редактируемого QComboBox, переключатели внутри группы-контейнера).
            editor.installEventFilter(self)
            for child in editor.findChildren(QWidget):
                child.installEventFilter(self)
            editor.setVisible(False)
        return editor

    def _create_radio_editor(self, index: int, widget: dict[str, Any]) -> QWidget:
        options = list(widget["options"])
        value = widget["value"]
        readonly = bool(widget["readonly"])

        if options and isinstance(value, str):
            # Один словарь описывает всю группу: переключатели по вариантам.
            holder = QWidget(self)
            holder.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            group = QButtonGroup(holder)
            group.setExclusive(True)
            for option in options:
                button = FormRadioButton(option, holder)
                button.setChecked(option == value)
                button.setEnabled(not readonly)
                group.addButton(button)
                layout.addWidget(button)
                button.toggled.connect(
                    lambda state, i=index, o=option: self._on_radio_option_toggled(
                        i, o, bool(state)
                    )
                )
            self._form_groups.append(group)
            return holder

        button = FormRadioButton(self)
        button.setChecked(_to_bool(value))
        button.setEnabled(not readonly)
        self._button_group_for(widget["key"]).addButton(button)
        button.toggled.connect(lambda state, i=index: self._on_radio_toggled(i, bool(state)))
        return button

    def _create_choice_editor(self, index: int, widget: dict[str, Any]) -> QComboBox:
        combo = QComboBox(self)
        options = list(widget["options"])
        value = str(widget["value"] or "")
        combo.addItems(options)
        editable = (not widget["readonly"]) and (
            bool(widget["editable"]) or (bool(value) and value not in options)
        )
        combo.setEditable(editable)
        if value:
            position = combo.findText(value)
            if position >= 0:
                combo.setCurrentIndex(position)
            elif editable:
                combo.setEditText(value)
            else:
                combo.addItem(value)
                combo.setCurrentIndex(combo.count() - 1)
        combo.setEnabled(not widget["readonly"])
        combo.currentTextChanged.connect(
            lambda text, i=index: self._on_choice_changed(i, text)
        )
        if editable:
            # Ввод в строку редактируемого комбобокса (дубли гасит дедупликация).
            combo.editTextChanged.connect(
                lambda text, i=index: self._on_choice_changed(i, text)
            )
        return combo

    def _button_group_for(self, key: str) -> QButtonGroup:
        group = self._form_groups_by_key.get(key)
        if group is None:
            group = QButtonGroup(self)
            group.setExclusive(True)
            self._form_groups_by_key[key] = group
            self._form_groups.append(group)
        return group

    # ------------------------------------------------------ геометрия/видимость
    def _layout_form_widgets(self) -> None:
        """Привязать редакторы к странице и текущему масштабу."""
        zoom = self._zoom
        active = self._tool == TOOL_FORM and self._has_page()
        for index, widget in enumerate(self._form_widgets):
            if index >= len(self._form_editors):
                break
            editor = self._form_editors[index]
            if editor is None:
                continue
            x, y, width, height = self._clamp_rect(
                widget["x"], widget["y"], widget["width"], widget["height"]
            )
            rect = QRectF(
                x * zoom, y * zoom, max(width * zoom, 1.0), max(height * zoom, 1.0)
            ).toAlignedRect()
            editor.setGeometry(rect)
            self._apply_form_font(editor, widget, zoom)
            editor.setVisible(active)

    def _apply_form_font(
        self, editor: QWidget, widget: dict[str, Any], zoom: float
    ) -> None:
        kind = widget["kind"]
        if kind not in ("text", "choice"):
            return  # флажки/переключатели рисуются без подписи

        declared_size = widget.get("font_size")
        if declared_size is None:
            factor = (0.35 if widget.get("multiline") else 0.6) if kind == "text" else 0.6
            size = int(round(widget["height"] * zoom * factor))
        else:
            try:
                declared_size = max(0.0, float(declared_size))
            except (TypeError, ValueError):
                declared_size = 0.0
            if declared_size > 0:
                # PDF /DA sizes are points. The page uses one raster pixel per
                # point at 100%, so scale by zoom without Qt's screen-DPI factor.
                size = int(round(declared_size * zoom))
            else:
                # /DA size zero requests automatic fitting. Choose a compact
                # size from the field geometry, then reduce it until the live
                # value fits horizontally, matching PDF appearance behavior.
                size = int(round(min(widget["height"] * zoom * 0.82,
                                     FORM_FONT_MAX_PX * zoom)))
                text = (editor.text() if isinstance(editor, QLineEdit) else
                        editor.toPlainText() if isinstance(editor, QTextEdit) else
                        editor.currentText() if isinstance(editor, QComboBox) else "")
                available_width = max(1, editor.width() - 4)
                while size > 1:
                    probe = QFont(str(widget.get("font_family") or self._font_family))
                    probe.setPixelSize(size)
                    if QFontMetricsF(probe).horizontalAdvance(str(text)) <= available_width:
                        break
                    size -= 1
        size = min(max(size, FORM_FONT_MIN_PX), FORM_FONT_MAX_PX)
        family = str(widget.get("font_family") or self._font_family)
        editor.setProperty("pdfFormFontSize", size)
        editor.setProperty("pdfFormFontFamily", family)
        font = QFont(family)
        font.setPixelSize(size)
        selector = "QComboBox" if isinstance(editor, QComboBox) else (
            "QTextEdit" if isinstance(editor, QTextEdit) else "QLineEdit")
        font_css = (f'{selector} {{ font-size: {size}px; '
                    f'font-family: "{family}"; }}')
        if editor.styleSheet() != font_css:
            editor.setStyleSheet(font_css)
        editor.setFont(font)
        alignment = widget.get("alignment")
        if alignment in ("left", "center", "right"):
            horizontal = {
                "left": Qt.AlignmentFlag.AlignLeft,
                "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight,
            }[alignment]
            if isinstance(editor, QLineEdit):
                editor.setAlignment(horizontal | Qt.AlignmentFlag.AlignVCenter)
            elif isinstance(editor, QTextEdit):
                editor.setAlignment(horizontal)
            elif isinstance(editor, QComboBox) and editor.isEditable() and editor.lineEdit():
                editor.lineEdit().setAlignment(horizontal | Qt.AlignmentFlag.AlignVCenter)

    def _apply_tool_state(self) -> None:
        """Показать/скрыть поля формы и увести фокус из скрытых редакторов."""
        self._layout_form_widgets()
        if self._tool == TOOL_FORM:
            return
        focused = self.focusWidget()
        for editor in self._form_editors:
            if editor is not None and self._editor_owns(editor, focused):
                self.setFocus(Qt.FocusReason.OtherFocusReason)
                return

    @staticmethod
    def _editor_owns(editor: QWidget, widget: Optional[QWidget]) -> bool:
        if widget is None:
            return False
        return widget is editor or editor.isAncestorOf(widget)

    # ---------------------------------------------------------- навигация Tab
    @staticmethod
    def _editor_editable(editor: QWidget) -> bool:
        """Может ли пользователь изменить значение этого редактора."""
        if not editor.isEnabled():
            return False
        if isinstance(editor, (QLineEdit, QTextEdit)) and editor.isReadOnly():
            return False
        return True

    def _form_focus_order(self) -> list[int]:
        """PDF /Tabs /S sequence when supplied, otherwise visual geometry order."""
        entries: list[tuple[int, dict[str, Any]]] = []
        for index, widget in enumerate(self._form_widgets):
            if index >= len(self._form_editors):
                break
            editor = self._form_editors[index]
            if editor is None or not editor.isVisibleTo(self):
                continue
            if not self._editor_editable(editor):
                continue  # readonly/недоступные поля не участвуют в обходе
            entries.append((index, widget))
        if not entries:
            return []

        # The caller only supplies tab_order for pages whose /Tabs value is /S.
        # Keep the geometry-based fallback intact for legacy documents and tests.
        if all(widget.get("tab_order") is not None for _, widget in entries):
            entries.sort(key=lambda item: (item[1]["tab_order"], item[0]))
            return [index for index, _ in entries]

        heights = sorted(widget["height"] for _, widget in entries)
        tolerance = max(1.0, 0.5 * heights[len(heights) // 2])
        entries.sort(key=lambda item: (item[1]["y"], item[1]["x"]))

        order: list[int] = []
        row: list[tuple[int, dict[str, Any]]] = []
        row_top = entries[0][1]["y"]
        for entry in entries:
            if row and abs(entry[1]["y"] - row_top) > tolerance:
                row.sort(key=lambda item: item[1]["x"])
                order.extend(index for index, _ in row)
                row = []
            if not row:
                row_top = entry[1]["y"]
            row.append(entry)
        if row:
            row.sort(key=lambda item: item[1]["x"])
            order.extend(index for index, _ in row)
        return order

    def _focus_form_step(self, back: bool = False) -> bool:
        order = self._form_focus_order()
        if not order:
            return False
        focused = self.focusWidget()
        current: Optional[int] = None
        for position, index in enumerate(order):
            editor = self._form_editors[index]
            if editor is not None and self._editor_owns(editor, focused):
                current = position
                break
        if current is None:
            target = order[-1] if back else order[0]
        else:
            step = -1 if back else 1
            target = order[(current + step) % len(order)]
        editor = self._form_editors[target]
        if editor is None:  # pragma: no cover - защита от гонки
            return False
        self._focus_editor(editor)
        return True

    def _focus_editor(self, editor: QWidget) -> None:
        """Передать фокус редактору (для контейнера — первому переключателю)."""
        if editor.isEnabled() and editor.focusPolicy() != Qt.FocusPolicy.NoFocus:
            editor.setFocus(Qt.FocusReason.TabFocusReason)
            return
        for child in editor.findChildren(QWidget):
            if (
                child.isEnabled()
                and child.focusPolicy() != Qt.FocusPolicy.NoFocus
                and child.isVisibleTo(editor)
            ):
                child.setFocus(Qt.FocusReason.TabFocusReason)
                return

    def _focused_form_key(self) -> Optional[str]:
        focused = self.focusWidget()
        if focused is None:
            return None
        for index, editor in enumerate(self._form_editors):
            if editor is not None and self._editor_owns(editor, focused):
                if index < len(self._form_widgets):
                    return str(self._form_widgets[index]["key"])
        return None

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
        """Tab/Shift+Tab внутри полей формы — обход по геометрии, не по порядку создания."""
        if event.type() == QEvent.Type.KeyPress and self._tool == TOOL_FORM:
            key = event.key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and not (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                back = key == Qt.Key.Key_Backtab or bool(
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                )
                if self._focus_form_step(back):
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------ сигналы значений
    def _on_line_edited(self, index: int, text: str) -> None:
        self._emit_form_value(index, str(text))

    def _on_editing_finished(self, index: int) -> None:
        if index >= len(self._form_editors):
            return
        editor = self._form_editors[index]
        if isinstance(editor, QLineEdit):
            self._emit_form_value(index, editor.text())

    def _on_multiline_edited(self, index: int, text: str) -> None:
        self._emit_form_value(index, str(text))

    def _on_checkbox_toggled(self, index: int, state: bool) -> None:
        self._emit_form_value(index, bool(state))

    def _on_radio_toggled(self, index: int, state: bool) -> None:
        if state:  # снятие происходит только программно/при переключении группы
            self._emit_form_value(index, True)

    def _on_radio_option_toggled(self, index: int, option: str, state: bool) -> None:
        if state:
            self._emit_form_value(index, str(option))

    def _on_choice_changed(self, index: int, text: str) -> None:
        self._emit_form_value(index, str(text))

    def _emit_form_value(self, index: int, raw_value: Any) -> None:
        """Отправить наружу ``(key, export_value)``; повторы гасятся."""
        if self._form_loading or not (0 <= index < len(self._form_widgets)):
            return
        widget = self._form_widgets[index]
        widget["value"] = copy.deepcopy(raw_value)
        value = self._resolve_export(widget, raw_value)
        if index in self._form_last_value:
            try:
                if self._form_last_value[index] == value:
                    return
            except Exception:  # pragma: no cover - экзотические значения
                pass
        self._form_last_value[index] = copy.deepcopy(value)
        self.form_value_changed.emit(str(widget["key"]), value)

    @staticmethod
    def _resolve_export(widget: dict[str, Any], raw_value: Any) -> Any:
        """Какое значение писать в PDF для нового состояния поля.

        * ``export_value``-словарь — отображение состояния на экспорт-значение
          (ключи ``True/False``, ``on/off``, ``yes/no``, ``1/0``);
        * ``export_value``-список — пары/варианты (для bool: ``[on, off]``,
          для строк: значение на каждый вариант из ``options``);
        * ``export_value``-скаляр — экспорт-значение включённого состояния
          (для строковых полей значение не подменяется — экспортируется текст).
        """
        export = widget.get("export")
        if raw_value is None:
            return copy.deepcopy(export) if export is not None else None

        if isinstance(raw_value, bool):
            # Native checkbox widgets expose a boolean value and an empty
            # export_value. An empty name is not their checked PDF state.
            if widget.get("kind") == "checkbox" and export == "":
                export = None
            mapped = PageCanvas._map_bool_export(export, raw_value)
            if mapped is not _MISSING:
                return copy.deepcopy(mapped)
            if not raw_value and widget.get("off_export") is not None:
                return copy.deepcopy(widget["off_export"])
            if isinstance(export, str):
                return export if raw_value else False
            if export is not None and not isinstance(export, (dict, list, tuple)):
                return copy.deepcopy(export) if raw_value else False
            return raw_value

        if isinstance(export, dict):
            if raw_value in export:
                return copy.deepcopy(export[raw_value])
            text = str(raw_value)
            for candidate, mapped in export.items():
                if str(candidate) == text:
                    return copy.deepcopy(mapped)
            return raw_value
        if isinstance(export, (list, tuple)):
            options = widget.get("options") or []
            if raw_value in options:
                position = options.index(raw_value)
                if position < len(export):
                    return copy.deepcopy(export[position])
            return raw_value
        return raw_value

    @staticmethod
    def _map_bool_export(export: Any, state: bool) -> Any:
        if isinstance(export, dict):
            candidates: list[Any] = [
                state,
                "on" if state else "off",
                "true" if state else "false",
                "yes" if state else "no",
                1 if state else 0,
            ]
            for candidate in candidates:
                if candidate in export:
                    return export[candidate]
            wanted = {str(candidate).strip().lower() for candidate in candidates}
            for candidate, mapped in export.items():
                if str(candidate).strip().lower() in wanted:
                    return mapped
            return _MISSING
        if isinstance(export, (list, tuple)) and len(export) >= 2:
            return export[0] if state else export[1]
        return _MISSING

    # ------------------------------------------------------- отрисовка полей
    def _paint_form_fields(self, painter: QPainter) -> None:
        """В режиме формы подсказать рамкой readonly/неподдерживаемые поля."""
        if self._tool != TOOL_FORM:
            return
        for widget in self._form_widgets:
            if widget["kind"] != "unsupported" and not widget["readonly"]:
                continue
            rect = self._pdf_rect_to_widget(
                widget["x"], widget["y"], widget["width"], widget["height"]
            )
            color = QColor("#B0BEC5") if widget["readonly"] else QColor("#E57373")
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

    def _style_color(self, kind: str) -> str:
        if kind == TOOL_HIGHLIGHT and not self._style_explicit:
            return DEFAULT_HIGHLIGHT_COLOR
        return self._color


def wrap_in_scroll_area(canvas: PageCanvas) -> QScrollArea:
    """Поместить холст в QScrollArea с прокруткой и тёмным фоном.

    Вспомогательная функция для main_window: холст сам сообщает нужный размер
    через sizeHint, поэтому ``widgetResizable`` выключен, а полосы прокрутки
    появляются автоматически.
    """
    area = QScrollArea()
    area.setWidgetResizable(False)
    area.setAlignment(Qt.AlignmentFlag.AlignCenter)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(canvas)
    viewport = area.viewport()
    palette = viewport.palette()
    palette.setColor(viewport.backgroundRole(), QColor(CANVAS_BACKGROUND))
    viewport.setPalette(palette)
    viewport.setAutoFillBackground(True)
    canvas.resize(canvas.sizeHint())
    return area
