"""Холст новой редакции: ``EditorCanvas`` — наследник ``PageCanvas``.

Наследуются и переиспользуются без изменений:

* форма страницы и вся геометрия выделения (рамка, восемь маркеров,
  масштабирование, перемещение, ``HANDLE_CURSORS``);
* сигналы ``annotation_created`` / ``annotation_edited`` /
  ``annotation_deleted`` / ``selection_changed`` / ``zoom_changed`` /
  ``form_value_changed``;
* inline-редакторы полей AcroForm (``set_form_widgets``, ``focus_form_field``,
  Tab-обход, экспорт значений) — контракт backend-а
  ``{key,rect,kind,value,options,readonly,multiline,export_value}`` не меняется.

Добавляется то, чего не было в отклонённом варианте:

* инструменты ``select/text/note/highlight/ink/rect/ellipse/line/arrow/
  polygon/cloud/image/stamp`` (плюс сохранённый ``form``);
* многоугольник ``polygon`` строится щелчками по вершинам (Enter или двойной
  щелчок завершают, Escape отменяет, Backspace убирает последнюю вершину), а
  облако ``cloud`` рисуется протяжкой и получает зубчатый контур из тех же
  дуг, что уходят в поток внешнего вида PDF;
* текст правится **прямо на странице** inline-``QTextEdit`` — никаких
  ``QInputDialog`` (``_prompt_text`` переопределён и всегда возвращает None);
* комментарий — маленький значок (``note``), текст живёт в поповере
  «Мой комментарий / Только что / текст / Удалить / Закрыть», как в макете
  ``outputs/design/pdfdesk-concept.html`` + ``comment-controls.js``;
* фигуры ``rect/ellipse/line/arrow`` рисуются протяжкой, у ``line/arrow``
  ``points`` — абсолютные координаты отображаемой страницы;
* ``image/stamp`` — вставка по клику из ``set_image_data(base64, kind)``,
  выбор, перемещение и масштабирование как у остальных объектов;
* ``readonly`` — просмотр без правки (выделять и открывать комментарий можно,
  создавать/двигать/удалять нельзя);
* выделение исходного текста мышью работает и в правке (``select``), а пункт
  «Выделить текст» контекстного меню отдаёт геометрию строк владельцу страницы
  сигналом ``text_highlight_requested`` — сам холст модель не меняет.

Гарантия неизменяемости ``PageCanvas`` сохраняется: наружу уходят только
глубокие копии ``Annotation``, модель документа меняет владелец (DocumentView).
"""
from __future__ import annotations

import base64
import copy
import math
from typing import Any, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:  # импорт как пакета: from src.editor_canvas import EditorCanvas
    from .canvas import (
        DEFAULT_COLOR,
        DEFAULT_FONT_FAMILY,
        DEFAULT_FONT_SIZE,
        MIN_DRAW_SIZE,
        SELECTION_COLOR,
        TOOL_FORM,
        TOOL_HIGHLIGHT,
        TOOL_INK,
        TOOL_SELECT,
        TOOL_TEXT,
        PageCanvas,
    )
    from .contracts import Annotation, ink_strokes
    from .shape_geometry import (
        cloud_bounds,
        cloud_segments,
        normalize_polygon,
        polygon_bounds,
        rectangle_points,
    )
    from .text_selection import get_selected_text_rects, selected_text
except ImportError:  # импорт при sys.path, указывающем на src/
    from canvas import (  # type: ignore[no-redef]
        DEFAULT_COLOR,
        DEFAULT_FONT_FAMILY,
        DEFAULT_FONT_SIZE,
        MIN_DRAW_SIZE,
        SELECTION_COLOR,
        TOOL_FORM,
        TOOL_HIGHLIGHT,
        TOOL_INK,
        TOOL_SELECT,
        TOOL_TEXT,
        PageCanvas,
    )
    from contracts import Annotation, ink_strokes  # type: ignore[no-redef]
    from shape_geometry import (  # type: ignore[no-redef]
        cloud_bounds,
        cloud_segments,
        normalize_polygon,
        polygon_bounds,
        rectangle_points,
    )
    from text_selection import get_selected_text_rects, selected_text  # type: ignore[no-redef]

# --- Инструменты новой редакции -------------------------------------------
TOOL_NOTE = "note"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"
TOOL_LINE = "line"
TOOL_ARROW = "arrow"
TOOL_POLYGON = "polygon"
TOOL_CLOUD = "cloud"
TOOL_IMAGE = "image"
TOOL_STAMP = "stamp"

#: Фигуры, которые рисуются протяжкой мыши (облако — прямоугольной протяжкой).
SHAPE_TOOLS: tuple[str, ...] = (TOOL_RECT, TOOL_ELLIPSE, TOOL_LINE, TOOL_ARROW, TOOL_CLOUD)
#: Фигуры, у которых вершины набираются щелчками.
VERTEX_TOOLS: tuple[str, ...] = (TOOL_POLYGON,)
#: Инструменты, вставляющие картинку по клику.
IMAGE_TOOLS: tuple[str, ...] = (TOOL_IMAGE, TOOL_STAMP)
#: Полный список инструментов ``set_tool`` (порядок — для сообщений об ошибке).
EDITOR_TOOLS: tuple[str, ...] = (
    TOOL_SELECT,
    TOOL_TEXT,
    TOOL_NOTE,
    TOOL_HIGHLIGHT,
    TOOL_INK,
    TOOL_RECT,
    TOOL_ELLIPSE,
    TOOL_LINE,
    TOOL_ARROW,
    TOOL_POLYGON,
    TOOL_CLOUD,
    TOOL_IMAGE,
    TOOL_STAMP,
    TOOL_FORM,
)
#: Инструменты, которые полностью обслуживает ``PageCanvas``.
BASE_TOOLS: tuple[str, ...] = (TOOL_SELECT, TOOL_TEXT, TOOL_HIGHLIGHT, TOOL_INK, TOOL_FORM)
#: Типы, для которых осмысленно менять цвет обводки.
STROKE_KINDS: tuple[str, ...] = (
    TOOL_TEXT,
    TOOL_NOTE,
    TOOL_HIGHLIGHT,
    TOOL_INK,
    TOOL_RECT,
    TOOL_ELLIPSE,
    TOOL_LINE,
    TOOL_ARROW,
    TOOL_POLYGON,
    TOOL_CLOUD,
)
#: Типы, у которых есть заливка.
FILL_KINDS: tuple[str, ...] = (
    TOOL_RECT,
    TOOL_ELLIPSE,
    TOOL_HIGHLIGHT,
    TOOL_POLYGON,
    TOOL_CLOUD,
)
#: Типы, у которых есть толщина линии.
WIDTH_KINDS: tuple[str, ...] = (
    TOOL_INK,
    TOOL_RECT,
    TOOL_ELLIPSE,
    TOOL_LINE,
    TOOL_ARROW,
    TOOL_POLYGON,
    TOOL_CLOUD,
)
#: Типы, у которых есть шрифт/начертание/выравнивание.
FONT_KINDS: tuple[str, ...] = (TOOL_TEXT,)

ALIGNMENTS: tuple[str, ...] = ("left", "center", "right")
_ALIGN_FLAGS: dict[str, Qt.AlignmentFlag] = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
}

# --- Размеры по умолчанию (PDF-пункты) -------------------------------------
NOTE_ICON_SIZE = 22.0
NOTE_ICON_FILL = "#FFF5D9"
NOTE_ICON_BORDER = "#EFDDA9"
NOTE_ICON_COLOR = "#B28729"
NOTE_POPOVER_WIDTH = 226
NOTE_POPOVER_MARGIN = 8.0
NOTE_SAVE_DELAY_MS = 400
TEXT_EDITOR_WIDTH = 200.0
TEXT_EDITOR_MIN_HEIGHT = 30.0
TEXT_EDITOR_MIN_WIDTH = 70.0
STAMP_DEFAULT_SIZE = (140.0, 56.0)
IMAGE_MAX_PAGE_FRACTION = 0.5
ARROW_HEAD_LENGTH = 11.0
IMAGE_CACHE_LIMIT = 32
#: Допуск «щелчка по первой вершине» многоугольника, px экрана.
POLYGON_CLOSE_TOLERANCE = 8.0
#: Минимальное расстояние между соседними вершинами при щелчке, px экрана.
POLYGON_STEP_TOLERANCE = 2.0
#: Сторона квадратика-маркера вершины многоугольника, px экрана.
POLYGON_VERTEX_SIZE = 6.0

#: Оформление поповера повторяет макет (светлая карточка, красная «Удалить»).
NOTE_POPOVER_STYLE = """
#notePopover { background: #FFFFFF; border: 1px solid #E2E6EA; border-radius: 9px; }
#notePopover QLabel#noteAuthor { color: #26313D; font-weight: 600; }
#notePopover QLabel#noteTime { color: #98A1AA; }
#notePopover QLabel#noteHint { color: #7A8591; }
#notePopover QTextEdit#noteText {
    color: #26313D; background: transparent; border: 0px; padding: 0px;
}
#notePopover QPushButton {
    background: #F4F6F8; color: #26313D; border: 0px; border-radius: 5px; padding: 6px 8px;
}
#notePopover QPushButton:hover { background: #E9EDF1; }
#notePopover QPushButton#noteDelete { color: #B54D49; }
#notePopover QPushButton:disabled { color: #B9C0C7; }
"""


def _valid_color(value: Any) -> Optional[str]:
    """Нормализовать цвет в ``#RRGGBB`` или вернуть None."""
    text = str(value or "").strip()
    if not text:
        return None
    color = QColor(text)
    if not color.isValid():
        return None
    return color.name(QColor.NameFormat.HexRgb)


def _same_color(first: Any, second: Any) -> bool:
    """Сравнить цвета без учёта регистра и формы записи."""
    left = _valid_color(first)
    right = _valid_color(second)
    if left is None or right is None:
        return str(first or "") == str(second or "")
    return left == right


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


class EditorCanvas(PageCanvas):
    """Страница редактора: растр, аннотации всех типов, формы и правка на месте."""

    #: Необязательная подсказка для окна (например, «изображение не задано»).
    notice = Signal(str)
    #: Запрос подсветить выделенный исходный текст: список прямоугольников
    #: строк в пунктах отображаемой страницы ``(x1, y1, x2, y2)``.
    text_highlight_requested = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._readonly: bool = False
        self._raster_includes_annotations: bool = False
        self._text_highlight_available: bool = False
        self._bold: bool = False
        self._alignment: str = "left"
        self._fill_color: str = ""
        self._opacity: float = 1.0

        # Правка текста прямо на странице.
        self._text_editor: Optional[QTextEdit] = None
        self._inline_index: int = -1            # -1 — создаётся новая аннотация
        self._inline_target: Optional[Annotation] = None
        self._inline_origin: tuple[float, float] = (0.0, 0.0)
        self._inline_box: tuple[float, float] = (0.0, 0.0)
        self._inline_style: dict[str, Any] = {}
        self._inline_committing: bool = False
        self._text_commit_timer = QTimer(self)
        self._text_commit_timer.setSingleShot(True)
        self._text_commit_timer.timeout.connect(self._commit_inline_text)

        # Комментарий: значок + поповер.
        self._note_popover: Optional[QFrame] = None
        self._note_editor: Optional[QTextEdit] = None
        self._note_delete: Optional[QPushButton] = None
        self._note_index: int = -1
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._commit_note_text)

        # Отложенная вставка изображения/штампа.
        self._pending_image_data: str = ""
        self._pending_image_kind: str = TOOL_IMAGE
        self._image_cache: dict[str, Optional[QPixmap]] = {}
        # Многоугольник: вершины набираются щелчками, контур не замкнут до конца.
        self._polygon_points: list[tuple[float, float]] = []
        self._polygon_cursor: Optional[tuple[float, float]] = None
        # Направляющие — состояние холста, а не документа: они не входят в
        # аннотации и не создают записи undo/dirty.
        self.guides: list[tuple[str, float]] = []
        self.guides_visible = False
        self._guide_selected = -1
        self._guide_dragging = False

    def add_guide(self, axis: str, position: float) -> None:
        """Добавить линию по координате в пунктах отображаемой страницы."""
        axis = str(axis).lower()
        position = float(position)
        if axis not in ("v", "h"):
            raise ValueError("Ось направляющей должна быть 'v' или 'h'.")
        if not math.isfinite(position):
            raise ValueError("Координата направляющей должна быть конечной.")
        page_w, page_h = self._page_size
        limit = page_w if axis == "v" else page_h
        if not self._has_page() or not 0.0 <= position <= limit:
            raise ValueError("Координата направляющей выходит за границы страницы.")
        self.guides.append((axis, position))
        self._guide_selected = len(self.guides) - 1
        self.guides_visible = True
        self.update()

    @staticmethod
    def _points_to_mm(points: float) -> float:
        return float(points) * 25.4 / 72.0

    @staticmethod
    def _mm_to_points(millimeters: float) -> float:
        value = float(millimeters)
        if not math.isfinite(value):
            raise ValueError("Размер должен быть конечным числом.")
        return value * 72.0 / 25.4

    def guide_page_size_mm(self) -> tuple[float, float]:
        """Габариты текущей отображаемой страницы в миллиметрах."""
        return tuple(self._points_to_mm(value) for value in self._page_size)

    def guide_position_mm(self, index: int) -> float:
        """Точное положение направляющей в миллиметрах от верхнего/левого края."""
        return self._points_to_mm(self.guides[index][1])

    def add_guide_mm(self, axis: str, millimeters: float) -> None:
        """Добавить направляющую по точному пользовательскому значению в мм."""
        self.add_guide(axis, self._mm_to_points(millimeters))

    def set_guide_mm(self, index: int, millimeters: float) -> None:
        """Переместить направляющую, проверяя границу соответствующей оси."""
        axis, _ = self.guides[index]
        points = self._mm_to_points(millimeters)
        page_w, page_h = self._page_size
        limit = page_w if axis == "v" else page_h
        if not 0.0 <= points <= limit:
            raise ValueError("Координата направляющей выходит за границы страницы.")
        self.guides[index] = (axis, points)
        self._guide_selected = index
        self.update()

    def set_guides(self, guides: list[tuple[str, float]], visible: bool = True) -> None:
        """Заменить направляющие одним действием после подтверждения диалога."""
        checked: list[tuple[str, float]] = []
        page_w, page_h = self._page_size
        for axis, raw_position in guides:
            axis = str(axis).lower()
            position = float(raw_position)
            limit = page_w if axis == "v" else page_h if axis == "h" else -1.0
            if not math.isfinite(position) or not 0.0 <= position <= limit:
                raise ValueError("Некорректная координата направляющей.")
            checked.append((axis, position))
        self.guides = checked
        self._guide_selected = min(self._guide_selected, len(checked) - 1)
        self.guides_visible = bool(visible)
        self.update()

    def set_guides_visible(self, visible: bool) -> None:
        self.guides_visible = bool(visible)
        self.update()

    def remove_guide(self, index: int) -> None:
        if not 0 <= index < len(self.guides):
            return
        del self.guides[index]
        self._guide_selected = -1 if not self.guides else min(index, len(self.guides) - 1)
        self.update()

    def _guide_at(self, pos: QPointF, tolerance_px: float = 5.0) -> int:
        if not self.guides_visible or not self.page_rect().contains(pos):
            return -1
        x, y = self._to_pdf(pos)
        for index, (axis, position) in enumerate(self.guides):
            coordinate = x if axis == "v" else y
            if abs(coordinate - position) * self._zoom <= tolerance_px:
                return index
        return -1

    def open_guide_dialog(self, selected_index: int = -1) -> bool:
        """Открыть редактор; применить весь набор только по кнопке «ОК»."""
        if not self._has_page():
            return False
        try:
            from .guide_dialog import GuideDialog
        except ImportError:
            from guide_dialog import GuideDialog  # type: ignore[no-redef]
        dialog = GuideDialog(
            self._page_size, self.guides, self.guides_visible,
            selected_index=selected_index, parent=self,
        )
        if dialog.exec():
            self.set_guides(dialog.guides_points(), dialog.guides_visible())
            return True
        return False

    def clear_guides(self) -> None:
        self.guides.clear()
        self._guide_selected=-1
        self.update()

    def paintEvent(self,event) -> None:
        super().paintEvent(event)
        if not self.guides_visible or not self._has_page():
            return
        painter=QPainter(self)
        page=self.page_rect()
        for index,(axis,position) in enumerate(self.guides):
            painter.setPen(QPen(QColor('#167d99'),2 if index==self._guide_selected else 1,Qt.PenStyle.DashLine))
            if axis == 'v':
                x=page.left()+position*self._zoom
                painter.drawLine(QPointF(x,page.top()),QPointF(x,page.bottom()))
            else:
                y=page.top()+position*self._zoom
                painter.drawLine(QPointF(page.left(),y),QPointF(page.right(),y))
        painter.end()

    # =================================================================== API
    # ---------------------------------------------------------- страница/слой
    def set_page(self, image: Any, page_size: tuple[float, float],
                 annotations: list[Annotation]) -> None:
        """Показать страницу. Открытые редакторы закрываются без потери текста."""
        self._commit_inline_text()
        self._close_note_popover(flush=True)
        super().set_page(image, page_size, annotations)

    def refresh_raster(self, image: Any) -> None:
        """Обновить только растр страницы, не трогая аннотации, формы и фокус."""
        pixmap = self._to_pixmap(image)
        if pixmap is None and image is not None:
            return  # неудачный рендер не должен стирать уже показанную страницу
        self._page_pixmap = pixmap
        self.update()

    def set_raster_includes_annotations(self, included: bool) -> None:
        """Hide the Qt annotation paint layer when the raster already contains it."""
        included = bool(included)
        if included == self._raster_includes_annotations:
            return
        self._raster_includes_annotations = included
        self.update()

    def set_annotations(self, annotations: list[Annotation],
                        selected: Optional[int] = None,
                        preserve_selection: bool = True) -> None:
        """Заменить слой аннотаций, сохранив выделение и открытый комментарий."""
        self._annotations = [copy.deepcopy(item) for item in (annotations or [])]
        if selected is not None:
            self._set_selected(int(selected))
        elif preserve_selection:
            self._set_selected(self._selected)
        if self._text_editor is not None and self._inline_index >= len(self._annotations):
            self._cancel_inline_text()
        self._sync_note_popover()
        self.update()

    # ------------------------------------------------------------- инструмент
    def set_tool(self, tool: str) -> None:
        """Активировать инструмент редактора (полный список — ``EDITOR_TOOLS``)."""
        if tool not in EDITOR_TOOLS:
            raise ValueError(
                "Неизвестный инструмент: {0!r}. Допустимые значения: {1}.".format(
                    tool, ", ".join(EDITOR_TOOLS)
                )
            )
        if tool == self._tool:
            return
        self._commit_inline_text()
        self._close_note_popover(flush=True)
        if tool in BASE_TOOLS:
            super().set_tool(tool)
            return
        # Новые инструменты обслуживает сам EditorCanvas: повторяем штатное
        # поведение PageCanvas (сброс жеста, снятие выделения, курсор).
        self._cancel_interaction()
        self._tool = tool
        self._set_selected(-1)
        self._apply_tool_state()
        self._apply_cursor()
        self.update()

    def set_style(self, font_family: Optional[str] = None,
                  font_size: Optional[float] = None,
                  color: Optional[str] = None,
                  stroke_width: Optional[float] = None,
                  **extra: Any) -> None:
        """Стиль новых объектов; при выделении — ещё и правка выбранного.

        Дополнительные ключи (совместимо с ``DocumentView.set_style(**kwargs)``):
        ``bold`` (bool), ``alignment`` (left/center/right), ``fill_color``
        (``#RRGGBB`` или пусто), ``opacity`` (0..1). Неизвестные ключи молча
        игнорируются, чтобы панель свойств окна не ломала холст.
        """
        if "bold" in extra:
            self._bold = bool(extra.pop("bold"))
        if "alignment" in extra:
            value = str(extra.pop("alignment") or "left").strip().lower()
            if value not in ALIGNMENTS:
                raise ValueError(
                    "Некорректное выравнивание: {0!r}. Допустимо: {1}.".format(
                        value, ", ".join(ALIGNMENTS)
                    )
                )
            self._alignment = value
        if "fill_color" in extra:
            raw = extra.pop("fill_color")
            if str(raw or "").strip():
                normalized = _valid_color(raw)
                if normalized is None:
                    raise ValueError(
                        "Некорректный цвет заливки: {0!r}. Ожидается '#RRGGBB'.".format(raw)
                    )
                self._fill_color = normalized
            else:
                self._fill_color = ""
        if "opacity" in extra:
            value = _as_float(extra.pop("opacity"), self._opacity)
            self._opacity = min(max(value, 0.0), 1.0)
        extra.clear()

        super().set_style(
            self._font_family if font_family is None else font_family,
            self._font_size if font_size is None else font_size,
            self._color if color is None else color,
            self._stroke_width if stroke_width is None else stroke_width,
        )
        if self._text_editor is not None:
            self._apply_inline_font()
        self._apply_style_to_selection()

    def style(self) -> dict[str, Any]:
        """Текущий стиль новых объектов (копия)."""
        return {
            "font_family": self._font_family,
            "font_size": self._font_size,
            "color": self._color,
            "stroke_width": self._stroke_width,
            "bold": self._bold,
            "alignment": self._alignment,
            "fill_color": self._fill_color,
            "opacity": self._opacity,
        }

    def set_readonly(self, flag: bool) -> None:
        """Режим просмотра: без создания, перемещения, масштабирования и удаления."""
        flag = bool(flag)
        if flag == self._readonly:
            return
        self._readonly = flag
        if flag:
            self._cancel_inline_text()
            self._pending_image_data = ""
            self._cancel_interaction()
            self._update_note_readonly_state()
        self._apply_cursor()
        self.update()

    def readonly(self) -> bool:
        """Активен ли режим «только чтение»."""
        return self._readonly

    # ------------------------------------------- выделение исходного текста
    def set_text_highlight_available(self, flag: bool) -> None:
        """Разрешить подсветку выделенного текста (право правки документа).

        Флаг ставит владелец страницы (``DocumentView``): для PDF, открытого
        с ограниченными правами, пункт «Выделить текст» не появляется вовсе.
        """
        flag = bool(flag)
        if flag == self._text_highlight_available:
            return
        self._text_highlight_available = flag
        self.update()

    def text_highlight_available(self) -> bool:
        """Доступна ли подсветка выделенного исходного текста."""
        return self._text_highlight_available

    def selected_text_rects(self) -> tuple[tuple[float, float, float, float], ...]:
        """Прямоугольники строк текущего выделения (пункты отображаемой страницы).

        Несколько строк дают несколько прямоугольников; поворот и обрезка
        страницы уже учтены геометрией глифов.
        """
        start, end = self._text_range
        if end <= start:
            return ()
        return get_selected_text_rects(self._text_chars, start, end)

    def selected_source_text(self) -> str:
        """Текст текущего выделения (пустая строка, если выделения нет)."""
        return selected_text(self._text_chars, *self._text_range)

    def can_highlight_selected_text(self) -> bool:
        """Есть ли что подсвечивать и разрешена ли правка документа."""
        if self._readonly or not self._text_highlight_available:
            return False
        start, end = self._text_range
        if end <= start:
            return False
        return bool(selected_text(self._text_chars, start, end).strip())

    def clear_text_selection(self) -> None:
        """Снять выделение исходного текста."""
        self._text_anchor = -1
        self._text_dragging = False
        self._text_start_pos = None
        self._text_range = (0, 0)
        self.update()

    def request_text_highlight(self) -> bool:
        """Попросить владельца страницы подсветить выделенный текст.

        Холст ничего не меняет в модели: он отдаёт геометрию строк наружу
        сигналом ``text_highlight_requested``, аннотации создаёт ``DocumentView``.
        """
        if self._readonly:
            self._readonly_notice()
            return False
        if not self._text_highlight_available:
            self.notice.emit("Подсветка текста недоступна: нет права изменять PDF.")
            return False
        if not self.can_highlight_selected_text():
            self.notice.emit("Сначала выделите текст на странице.")
            return False
        rects = self.selected_text_rects()
        if not rects:
            self.notice.emit("Выделенный текст не имеет геометрии для подсветки.")
            return False
        self.text_highlight_requested.emit(list(rects))
        return True

    # ------------------------------------------------------- картинки/штампы
    def set_image_data(self, image_data: str, kind: str = TOOL_IMAGE) -> None:
        """Задать base64-картинку для вставки по клику.

        Непустые данные включают соответствующий инструмент (``image`` или
        ``stamp``); пустая строка отменяет отложенную вставку.
        """
        if kind not in IMAGE_TOOLS:
            raise ValueError(
                "Недопустимый тип вставки: {0!r}. Ожидается {1}.".format(
                    kind, " или ".join(IMAGE_TOOLS)
                )
            )
        self._pending_image_data = str(image_data or "")
        self._pending_image_kind = kind
        if self._pending_image_data and kind != self._tool:
            self.set_tool(kind)
        self._apply_cursor()
        self.update()

    def pending_image(self) -> tuple[str, str]:
        """Отложенная вставка: ``(base64, kind)``."""
        return (self._pending_image_data, self._pending_image_kind)

    # ------------------------------------------------------- многоугольник
    def is_drawing_polygon(self) -> bool:
        """Идёт ли набор вершин многоугольника (для строки состояния окна)."""
        return bool(self._polygon_points)

    def polygon_points(self) -> list[tuple[float, float]]:
        """Уже поставленные вершины строящегося многоугольника (копия)."""
        return list(self._polygon_points)

    def finish_polygon(self) -> bool:
        """Завершить многоугольник (публичная обёртка для владельца страницы)."""
        return self._finish_polygon()

    def cancel_polygon(self) -> None:
        """Отменить незавершённый многоугольник без создания объекта."""
        if self._polygon_points or self._polygon_cursor is not None:
            self._polygon_points = []
            self._polygon_cursor = None
            self.update()

    # ------------------------------------------------------------- текст
    def has_inline_editor(self) -> bool:
        """Открыт ли inline-редактор текста (диагностика/владелец страницы)."""
        return self._text_editor is not None

    def inline_editor(self) -> Optional[QTextEdit]:
        """Сам inline-редактор текста или None."""
        return self._text_editor

    def commit_inline_text(self) -> None:
        """Завершить правку текста (публичная обёртка для владельца страницы)."""
        self._commit_inline_text()

    # ------------------------------------------------------------- примечания
    def note_popover_index(self) -> int:
        """Индекс комментария с открытым поповером или -1."""
        return self._note_index if self._note_popover is not None else -1

    def note_popover(self) -> Optional[QFrame]:
        """Открытый поповер комментария или None."""
        return self._note_popover

    def open_note_popover(self, index: int) -> bool:
        """Открыть поповер комментария ``index`` (публичная обёртка)."""
        return self._open_note_popover(int(index))

    def close_note_popover(self) -> None:
        """Закрыть поповер комментария, сохранив набранный текст."""
        self._close_note_popover(flush=True)

    # =================================================== геометрия и отрисовка
    @staticmethod
    def _cloud_points(annotation: Annotation) -> list[tuple[float, float]]:
        """Контур-«хорда» облака: вершины или прямоугольник аннотации."""
        points = normalize_polygon(annotation.points or ())
        if len(points) >= 3:
            return points
        return rectangle_points(annotation.x, annotation.y,
                                annotation.width, annotation.height)

    def _annotation_bounds(self, annotation: Annotation) -> tuple[float, float, float, float]:
        """Габариты объекта в PDF-пунктах (у фигур по точкам — по вершинам)."""
        kind = str(annotation.kind or "")
        if kind in (TOOL_LINE, TOOL_ARROW) and annotation.points:
            xs = [float(p[0]) for p in annotation.points]
            ys = [float(p[1]) for p in annotation.points]
            pad = max(float(annotation.stroke_width or 0.0), 1.0) / 2.0
            return (
                min(xs) - pad,
                min(ys) - pad,
                max(xs) - min(xs) + 2.0 * pad,
                max(ys) - min(ys) + 2.0 * pad,
            )
        if kind == TOOL_POLYGON:
            box = polygon_bounds(annotation.points or ())
            if box is not None:
                pad = max(float(annotation.stroke_width or 0.0), 1.0) / 2.0
                return (box[0] - pad, box[1] - pad,
                        box[2] + 2.0 * pad, box[3] + 2.0 * pad)
        if kind == TOOL_CLOUD:
            pad = max(float(annotation.stroke_width or 0.0), 1.0) / 2.0
            box = cloud_bounds(self._cloud_points(annotation), pad=pad)
            if box is not None:
                return box
        if kind == TOOL_NOTE:
            width = max(float(annotation.width), 0.0) or NOTE_ICON_SIZE
            height = max(float(annotation.height), 0.0) or NOTE_ICON_SIZE
            return (float(annotation.x), float(annotation.y), width, height)
        return super()._annotation_bounds(annotation)

    def _annotation_font(self, annotation: Annotation, zoom: float = 1.0) -> QFont:
        font = super()._annotation_font(annotation, zoom)
        if bool(getattr(annotation, "bold", False)):
            font.setBold(True)
        return font

    def _translate(self, annotation: Annotation, dx: float, dy: float) -> Annotation:
        if annotation.kind in (TOOL_LINE, TOOL_ARROW) and annotation.points:
            bx, by, bw, bh = self._annotation_bounds(annotation)
            nx, ny, _, _ = self._clamp_rect(bx + dx, by + dy, bw, bh)
            real_dx, real_dy = nx - bx, ny - by
            result = copy.deepcopy(annotation)
            result.points = [
                (float(px) + real_dx, float(py) + real_dy)
                for px, py in result.points
            ]
            result.x = float(annotation.x) + real_dx
            result.y = float(annotation.y) + real_dy
            return result
        if annotation.kind in (TOOL_POLYGON, TOOL_CLOUD) and annotation.points:
            bx, by, bw, bh = self._annotation_bounds(annotation)
            nx, ny, _, _ = self._clamp_rect(bx + dx, by + dy, bw, bh)
            real_dx, real_dy = nx - bx, ny - by
            result = copy.deepcopy(annotation)
            result.points = [
                (float(px) + real_dx, float(py) + real_dy)
                for px, py in result.points
            ]
            result.x = float(annotation.x) + real_dx
            result.y = float(annotation.y) + real_dy
            return result
        return super()._translate(annotation, dx, dy)

    def _apply_resize(self, annotation: Annotation, handle: str,
                      rect: tuple[float, float, float, float]) -> Annotation:
        if annotation.kind in (TOOL_LINE, TOOL_ARROW) and annotation.points:
            x, y, width, height = rect
            bx, by, bw, bh = self._annotation_bounds(annotation)
            scale_x = (width / bw) if bw > 1e-6 else 1.0
            scale_y = (height / bh) if bh > 1e-6 else 1.0
            result = copy.deepcopy(annotation)
            result.points = [
                (x + (float(px) - bx) * scale_x, y + (float(py) - by) * scale_y)
                for px, py in result.points
            ]
            result.x, result.y, result.width, result.height = x, y, width, height
            return result
        if annotation.kind in (TOOL_POLYGON, TOOL_CLOUD) and annotation.points:
            x, y, width, height = rect
            bx, by, bw, bh = self._annotation_bounds(annotation)
            scale_x = (width / bw) if bw > 1e-6 else 1.0
            scale_y = (height / bh) if bh > 1e-6 else 1.0
            result = copy.deepcopy(annotation)
            points = [
                (x + (float(px) - bx) * scale_x, y + (float(py) - by) * scale_y)
                for px, py in result.points
            ]
            result.points = points
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            result.x, result.y = min(xs), min(ys)
            result.width, result.height = max(xs) - min(xs), max(ys) - min(ys)
            return result
        return super()._apply_resize(annotation, handle, rect)

    # --------------------------------------------------------------- отрисовка
    def _paint_annotation(self, painter: QPainter, annotation: Annotation,
                          ghost: bool = False) -> None:
        if self._raster_includes_annotations and self._readonly and not ghost:
            return
        kind = str(annotation.kind or "")
        if kind == TOOL_NOTE:
            self._paint_note(painter, annotation, ghost)
            return
        if kind in (TOOL_RECT, TOOL_ELLIPSE):
            self._paint_shape(painter, annotation, ghost)
            return
        if kind == TOOL_POLYGON:
            self._paint_polygon(painter, annotation, ghost)
            return
        if kind == TOOL_CLOUD:
            self._paint_cloud(painter, annotation, ghost)
            return
        if kind in (TOOL_LINE, TOOL_ARROW):
            self._paint_line(painter, annotation, ghost)
            return
        if kind in IMAGE_TOOLS:
            self._paint_image(painter, annotation, ghost)
            return
        if kind == TOOL_TEXT:
            self._paint_text_editor_aware(painter, annotation, ghost)
            return
        super()._paint_annotation(painter, annotation, ghost)

    def _annotation_color(self, annotation: Annotation, ghost: bool) -> QColor:
        base = QColor(str(annotation.color or DEFAULT_COLOR))
        if not base.isValid():
            base = QColor(DEFAULT_COLOR)
        opacity = min(max(_as_float(annotation.opacity, 1.0), 0.0), 1.0)
        if ghost:
            opacity *= 0.8
        color = QColor(base)
        color.setAlphaF(opacity)
        return color

    def _stroke_px(self, annotation: Annotation) -> float:
        return max(1.0, _as_float(annotation.stroke_width, 2.0) * self._zoom)

    def _paint_ink(self, painter: QPainter, annotation: Annotation,
                   color: QColor) -> None:
        """Рисунок: каждый штрих — отдельная полилиния.

        ``points`` хранит штрихи подряд, ``stroke_lengths`` — их границы. Между
        штрихами не проводится отрезок, поэтому подъёмы пера видны и на холсте,
        и в сохранённом PDF (там каждый штрих — свой подпуть потока ``/AP``).
        Одиночный штрих и прежние аннотации без границ рисуются как раньше.
        """
        strokes = ink_strokes(annotation.points,
                              getattr(annotation, "stroke_lengths", None))
        if not strokes:
            return
        pen = QPen(
            color,
            self._stroke_px(annotation),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for stroke in strokes:
            points = [self._to_widget(float(px), float(py)) for px, py in stroke]
            if len(points) == 1:
                painter.drawPoint(points[0])
            else:
                painter.drawPolyline(QPolygonF(points))

    def _paint_note(self, painter: QPainter, annotation: Annotation, ghost: bool = False) -> None:
        x, y, width, height = self._annotation_bounds(annotation)
        rect = QRectF(x * self._zoom, y * self._zoom,
                      max(width, 1.0) * self._zoom, max(height, 1.0) * self._zoom)
        painter.save()
        if ghost:
            painter.setOpacity(0.8)
        painter.setPen(QPen(QColor(NOTE_ICON_BORDER), 1))
        painter.setBrush(QBrush(QColor(NOTE_ICON_FILL)))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3.0, 3.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        pen = QPen(QColor(NOTE_ICON_COLOR), max(1.0, min(1.8, rect.width() / 13.0)))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        left = rect.left() + rect.width() * 0.26
        right = rect.right() - rect.width() * 0.26
        first = rect.top() + rect.height() * 0.36
        second = rect.top() + rect.height() * 0.62
        painter.drawLine(QPointF(left, first), QPointF(right, first))
        painter.drawLine(QPointF(left, second), QPointF(left + (right - left) * 0.62, second))

        selected = (0 <= self._selected < len(self._annotations)
                    and annotation is self._annotations[self._selected])
        if selected:
            painter.setPen(QPen(QColor(SELECTION_COLOR), 1, Qt.PenStyle.DashLine))
            painter.drawRect(rect.adjusted(-2.0, -2.0, 2.0, 2.0))
        painter.restore()

    def _paint_shape(self, painter: QPainter, annotation: Annotation, ghost: bool = False) -> None:
        color = self._annotation_color(annotation, ghost)
        pen = QPen(color, self._stroke_px(annotation))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        fill = _valid_color(annotation.fill_color)
        if fill:
            brush_color = QColor(fill)
            brush_color.setAlphaF(min(max(_as_float(annotation.opacity, 1.0), 0.0), 1.0))
            painter.setBrush(QBrush(brush_color))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = self._rect_to_widget(annotation)
        if annotation.kind == TOOL_ELLIPSE:
            painter.drawEllipse(rect)
        else:
            painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_fill_brush(self, annotation: Annotation, ghost: bool = False) -> QBrush:
        """Кисть заливки фигуры: цвет заливки с прозрачностью самой аннотации.

        Та же прозрачность уходит в /GS0 потока внешнего вида, поэтому заливка
        на холсте, в сохранённом файле и на печати выглядит одинаково.
        """
        fill = _valid_color(annotation.fill_color)
        if not fill:
            return QBrush(Qt.BrushStyle.NoBrush)
        color = QColor(fill)
        opacity = min(max(_as_float(annotation.opacity, 1.0), 0.0), 1.0)
        if ghost:
            opacity *= 0.8
        color.setAlphaF(opacity)
        return QBrush(color)

    def _paint_polygon(self, painter: QPainter, annotation: Annotation,
                       ghost: bool = False) -> None:
        """Замкнутый многоугольник по вершинам ``points``."""
        points = normalize_polygon(annotation.points or ())
        if not points:
            return
        color = self._annotation_color(annotation, ghost)
        pen = QPen(color, self._stroke_px(annotation), Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(self._paint_fill_brush(annotation, ghost))
        polygon = QPolygonF([self._to_widget(px, py) for px, py in points])
        if len(points) >= 3:
            painter.drawPolygon(polygon)
        else:  # вырожденный контур: показываем хотя бы ломаную
            painter.drawPolyline(polygon)
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            for point in polygon:
                painter.drawEllipse(point, 2.0, 2.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_cloud(self, painter: QPainter, annotation: Annotation,
                     ghost: bool = False) -> None:
        """Облако: зубчатый контур из тех же дуг, что пишутся в /AP."""
        points = self._cloud_points(annotation)
        segments = cloud_segments(points)
        if not segments:
            self._paint_shape(painter, annotation, ghost)
            return
        path = QPainterPath()
        start = self._to_widget(segments[0][0][0], segments[0][0][1])
        path.moveTo(start)
        for _, control1, control2, end in segments:
            path.cubicTo(self._to_widget(control1[0], control1[1]),
                         self._to_widget(control2[0], control2[1]),
                         self._to_widget(end[0], end[1]))
        path.closeSubpath()
        color = self._annotation_color(annotation, ghost)
        pen = QPen(color, self._stroke_px(annotation), Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(self._paint_fill_brush(annotation, ghost))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_line(self, painter: QPainter, annotation: Annotation, ghost: bool = False) -> None:
        points = list(annotation.points or [])
        if len(points) < 2:
            # Одна точка — рисуем маркер, чтобы объект не «исчезал».
            if points:
                point = self._to_widget(float(points[0][0]), float(points[0][1]))
                painter.setPen(QPen(self._annotation_color(annotation, ghost), 1))
                painter.drawEllipse(point, 2.0, 2.0)
            return
        color = self._annotation_color(annotation, ghost)
        pen = QPen(color, self._stroke_px(annotation),
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start = self._to_widget(float(points[0][0]), float(points[0][1]))
        end = self._to_widget(float(points[-1][0]), float(points[-1][1]))
        painter.drawLine(start, end)
        if annotation.kind != TOOL_ARROW:
            return
        length = math.hypot(end.x() - start.x(), end.y() - start.y())
        if length < 1e-6:
            return
        head = max(4.0, ARROW_HEAD_LENGTH * self._zoom)
        head = min(head, length)
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        spread = math.radians(24.0)
        left = QPointF(end.x() - head * math.cos(angle - spread),
                       end.y() - head * math.sin(angle - spread))
        right = QPointF(end.x() - head * math.cos(angle + spread),
                        end.y() - head * math.sin(angle + spread))
        painter.setBrush(QBrush(color))
        painter.drawPolygon(QPolygonF([end, left, right]))

    def _paint_image(self, painter: QPainter, annotation: Annotation, ghost: bool = False) -> None:
        pixmap = self._image_pixmap(annotation.image_data)
        rect = self._rect_to_widget(annotation)
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            width, height = self._natural_size(annotation.image_data)
            rect = QRectF(float(annotation.x) * self._zoom, float(annotation.y) * self._zoom,
                          width * self._zoom, height * self._zoom)
        if pixmap is None:
            painter.setPen(QPen(QColor("#E57373"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            painter.drawLine(rect.topLeft(), rect.bottomRight())
            painter.drawLine(rect.topRight(), rect.bottomLeft())
            return
        painter.save()
        if ghost:
            painter.setOpacity(0.8)
        else:
            painter.setOpacity(min(max(_as_float(annotation.opacity, 1.0), 0.0), 1.0))
        painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        painter.restore()

    def _paint_text_editor_aware(self, painter: QPainter, annotation: Annotation,
                                 ghost: bool = False) -> None:
        """Текст с учётом ``bold``/``alignment`` (правка идёт в QTextEdit поверх)."""
        if self._text_editor is not None and annotation is self._inline_target:
            return  # пока идёт правка, текст рисует сам редактор
        text = annotation.text or ""
        if not text:
            return
        color = self._annotation_color(annotation, ghost)
        painter.setPen(QPen(color))
        painter.setFont(self._annotation_font(annotation, self._zoom))
        rect = self._rect_to_widget(annotation)
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            width, height = self._measure_text(annotation)
            rect = QRectF(rect.x(), rect.y(), max(width, 1.0) * self._zoom,
                          max(height, 1.0) * self._zoom)
        flags = int(Qt.AlignmentFlag.AlignTop
                    | _ALIGN_FLAGS.get(str(annotation.alignment or "left"),
                                       Qt.AlignmentFlag.AlignLeft))
        if annotation.width > 0.0:
            flags |= int(Qt.TextFlag.TextWordWrap)
        painter.drawText(rect, flags, text)

    def _paint_drawing_preview(self, painter: QPainter) -> None:
        if self._polygon_points:
            self._paint_polygon_preview(painter)
            return
        if self._drawing in SHAPE_TOOLS and self._drag_start and self._drag_current:
            preview = self._shape_preview(self._drawing, self._drag_start, self._drag_current)
            if preview is not None:
                self._paint_annotation(painter, preview, ghost=True)
                painter.setPen(QPen(QColor(SELECTION_COLOR), 1, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self._bounds_to_widget(preview))
            return
        super()._paint_drawing_preview(painter)

    def _paint_polygon_preview(self, painter: QPainter) -> None:
        """Незавершённый многоугольник: вершины, ломаная и «резиновая» нить."""
        polygon = [self._to_widget(px, py) for px, py in self._polygon_points]
        if not polygon:
            return
        color = QColor(_valid_color(self._style_color(TOOL_POLYGON)) or DEFAULT_COLOR)
        color.setAlphaF(min(max(self._opacity, 0.0), 1.0) * 0.8)
        pen = QPen(color, max(1.0, self._stroke_width * self._zoom),
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if len(polygon) >= 2:
            painter.drawPolyline(QPolygonF(polygon))
        cursor = self._polygon_cursor
        if cursor is not None:
            tip = self._to_widget(float(cursor[0]), float(cursor[1]))
            painter.drawLine(polygon[-1], tip)
            painter.setPen(QPen(QColor(SELECTION_COLOR), 1, Qt.PenStyle.DashLine))
            painter.drawLine(tip, polygon[0])
        painter.setPen(QPen(QColor(SELECTION_COLOR), 1))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        for index, point in enumerate(polygon):
            size = POLYGON_VERTEX_SIZE if index else POLYGON_VERTEX_SIZE + 2.0
            painter.drawRect(QRectF(point.x() - size / 2.0, point.y() - size / 2.0,
                                    size, size))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    # ------------------------------------------------------------- картинки
    def _image_pixmap(self, image_data: str) -> Optional[QPixmap]:
        text = str(image_data or "").strip()
        if not text:
            return None
        if text in self._image_cache:
            return self._image_cache[text]
        payload = text
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        pixmap: Optional[QPixmap] = None
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:  # noqa: BLE001 - битые данные не должны ломать холст
            raw = b""
        if raw:
            candidate = QPixmap()
            if candidate.loadFromData(raw):
                pixmap = candidate
        if len(self._image_cache) >= IMAGE_CACHE_LIMIT:
            self._image_cache.clear()
        self._image_cache[text] = pixmap
        return pixmap

    def _natural_size(self, image_data: str) -> tuple[float, float]:
        """Размер картинки в PDF-пунктах (1 px = 1 pt) с запасным значением."""
        pixmap = self._image_pixmap(image_data)
        if pixmap is None or pixmap.isNull():
            return STAMP_DEFAULT_SIZE
        width = float(max(pixmap.width(), 1))
        height = float(max(pixmap.height(), 1))
        page_w, page_h = self._page_size
        limit_w = max(page_w * IMAGE_MAX_PAGE_FRACTION, 40.0)
        limit_h = max(page_h * IMAGE_MAX_PAGE_FRACTION, 40.0)
        factor = min(1.0, limit_w / width if width > 0 else 1.0,
                     limit_h / height if height > 0 else 1.0)
        return (max(width * factor, 1.0), max(height * factor, 1.0))

    # ======================================================= мышь и клавиатура
    def _readonly_notice(self) -> None:
        self.notice.emit("Документ открыт только для чтения: правка недоступна.")

    def _handle_readonly_press(self, pos: QPointF) -> None:
        """В режиме просмотра можно выбирать объекты и читать комментарии."""
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        index = self._annotation_at(pos)
        if index < 0:
            self._set_selected(-1)
            return
        self._set_selected(index)
        if self._annotations[index].kind == TOOL_NOTE:
            self._open_note_popover(index)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._tool == TOOL_SELECT and not self._readonly and event.button() == Qt.MouseButton.LeftButton:
            index = self._guide_at(event.position())
            if index >= 0:
                self._guide_selected = index
                self._guide_dragging = True
                self.setFocus()
                self.update()
                event.accept()
                return
            self._guide_selected=-1
        if (event.button() != Qt.MouseButton.LeftButton or not self._has_page()
                or not self.page_rect().contains(event.position())):
            super().mousePressEvent(event)
            return
        pos = event.position()
        tool = self._tool

        if self._readonly:
            if tool == TOOL_SELECT and self._begin_text_selection(pos):
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                event.accept()
                return
            self._handle_readonly_press(pos)
            event.accept()
            return

        if tool == TOOL_POLYGON:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._add_polygon_vertex(pos)
            event.accept()
            return

        if tool in SHAPE_TOOLS:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            point = self._to_pdf(pos)
            self._drawing = tool
            self._drag_start = point
            self._drag_current = point
            self._apply_cursor(pos)
            self.update()
            event.accept()
            return

        if tool in IMAGE_TOOLS:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            if self._place_pending_image(pos):
                event.accept()
                return
            self.notice.emit("Сначала выберите изображение или штамп.")
            event.accept()
            return

        if tool == TOOL_NOTE:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            index = self._annotation_at(pos)
            if index >= 0 and self._annotations[index].kind == TOOL_NOTE:
                self._set_selected(index)
                self._begin_move(index, self._to_pdf(pos))
                self._open_note_popover(index)
            else:
                self._create_note_at(pos)
            event.accept()
            return

        if tool == TOOL_TEXT:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            index = self._annotation_at(pos)
            if index >= 0 and self._annotations[index].kind == TOOL_TEXT:
                self._set_selected(index)
                self._begin_inline_edit(index)
            else:
                self._create_text_at(pos)
            event.accept()
            return

        if tool == TOOL_SELECT:
            handle = self._handle_at(pos)
            if handle is None:
                index = self._annotation_at(pos)
                if index >= 0 and self._annotations[index].kind == TOOL_NOTE:
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self._set_selected(index)
                    self._begin_move(index, self._to_pdf(pos))
                    self._open_note_popover(index)
                    event.accept()
                    return
                # Правка: клик мимо объектов начинает выделение исходного текста.
                if index < 0 and self._begin_text_selection(pos):
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self._set_selected(-1)
                    event.accept()
                    return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._text_anchor >= 0:
            self._extend_text_selection(event.position())
            event.accept()
            return
        if self._guide_dragging:
            axis,_=self.guides[self._guide_selected]
            point=self._to_pdf(event.position())
            limit = self._page_size[0] if axis == 'v' else self._page_size[1]
            position = point[0] if axis == 'v' else point[1]
            self.guides[self._guide_selected] = (axis, min(max(position, 0.0), limit))
            self.update()
            event.accept()
            return
        if self._polygon_points:
            self._polygon_cursor = self._to_pdf(event.position())
            self.update()
            event.accept()
            return
        if self._drawing in SHAPE_TOOLS:
            self._drag_current = self._to_pdf(event.position())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton and self._text_anchor >= 0:
            self._extend_text_selection(event.position())
            selected = self._finish_text_selection()
            if not selected:
                # Простой клик: прежнее поведение (в просмотре — выбор объекта,
                # в правке — снятие выделения аннотации).
                if self._readonly:
                    self._handle_readonly_press(event.position())
                else:
                    self._set_selected(-1)
            event.accept()
            return
        if self._guide_dragging:
            self._guide_dragging=False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._drawing in SHAPE_TOOLS:
            kind = self._drawing
            self._drawing = None
            event.accept()
            self._finish_shape(kind, self._drag_start, self._drag_current)
            self._drag_start = None
            self._drag_current = None
            self._apply_cursor(event.position())
            self.update()
            return
        super().mouseReleaseEvent(event)
        self._sync_note_popover()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if (event.button() == Qt.MouseButton.LeftButton and self._has_page()
                and self.page_rect().contains(event.position())):
            if self._tool == TOOL_SELECT:
                guide_index = self._guide_at(event.position())
                if guide_index >= 0:
                    self._guide_dragging = False
                    self._guide_selected = guide_index
                    self.open_guide_dialog(guide_index)
                    event.accept()
                    return
            if self._tool == TOOL_POLYGON and not self._readonly:
                # Двойной щелчок завершает контур: точка пары кликов становится
                # последней вершиной (повтор той же точки не добавляется).
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._add_polygon_vertex(event.position(), allow_close=False)
                self._finish_polygon()
                event.accept()
                return
            index = self._annotation_at(event.position())
            if index >= 0 and self._annotations[index].kind == TOOL_NOTE:
                self._set_selected(index)
                self._open_note_popover(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        key = event.key()
        if key == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.copy_selected_text():
                event.accept()
                return
        if key in (Qt.Key.Key_Delete,Qt.Key.Key_Backspace) and self.guides_visible and self._guide_selected>=0:
            self.guides.pop(self._guide_selected)
            self._guide_selected=-1
            self.update()
            event.accept()
            return

        # Незавершённый многоугольник: Backspace убирает последнюю вершину,
        # Enter/Return замыкает контур, Escape отменяет построение.
        if self._polygon_points and self._tool == TOOL_POLYGON:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_polygon()
                event.accept()
                return
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self._polygon_points.pop()
                self._polygon_cursor = self._polygon_points[-1] if self._polygon_points else None
                self.update()
                event.accept()
                return

        if key == Qt.Key.Key_Escape:
            if self._text_editor is not None:
                self._cancel_inline_text()
                event.accept()
                return
            if self._note_popover is not None:
                self._close_note_popover(flush=True)
                event.accept()
                return
            if self._polygon_points:
                self.cancel_polygon()
                event.accept()
                return

        if self._readonly:
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected >= 0:
                self._readonly_notice()
                event.accept()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                event.accept()
                return

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected >= 0:
            self._delete_selected()
            event.accept()
            return

        super().keyPressEvent(event)

    def _build_context_menu(self) -> QMenu:
        """Меню выделенного исходного текста (копирование и подсветка)."""
        menu = QMenu(self)
        copy_action = menu.addAction("Копировать")
        copy_action.triggered.connect(self.copy_selected_text)
        if self.can_highlight_selected_text():
            highlight_action = menu.addAction("Выделить текст")
            highlight_action.triggered.connect(self.request_text_highlight)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._text_range[1] > self._text_range[0]:
            menu = self._build_context_menu()
            menu.exec(event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def _apply_cursor(self, pos: Optional[QPointF] = None) -> None:
        if self._has_page():
            if self._drawing in SHAPE_TOOLS or self._tool in SHAPE_TOOLS:
                self.setCursor(Qt.CursorShape.CrossCursor)
                return
            if self._tool in VERTEX_TOOLS:
                self.setCursor(Qt.CursorShape.CrossCursor)
                return
            if self._tool in IMAGE_TOOLS:
                self.setCursor(Qt.CursorShape.CrossCursor)
                return
            if self._tool == TOOL_NOTE:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        super()._apply_cursor(pos)

    # ====================================================== создание объектов
    def _new_annotation(self, kind: str, **overrides: Any) -> Annotation:
        data: dict[str, Any] = {
            "kind": kind,
            "color": self._style_color(kind),
            "font_family": self._font_family,
            "font_size": self._font_size,
            "stroke_width": self._stroke_width,
            "opacity": self._opacity,
            "bold": self._bold,
            "alignment": self._alignment,
            "fill_color": self._fill_color,
            "image_data": "",
        }
        data.update(overrides)
        return Annotation(**data)

    def _shape_preview(self, kind: str, start: tuple[float, float],
                       end: tuple[float, float]) -> Optional[Annotation]:
        if kind in (TOOL_LINE, TOOL_ARROW):
            if self._distance(start, end) * self._zoom < 1.0:
                return None
            return self._new_annotation(
                kind,
                x=min(start[0], end[0]),
                y=min(start[1], end[1]),
                width=abs(end[0] - start[0]),
                height=abs(end[1] - start[1]),
                points=[(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))],
                opacity=0.8,
            )
        rect = self._rect_from_points(start, end)
        if rect is None:
            return None
        x, y, width, height = rect
        if width <= 0.0 and height <= 0.0:
            return None
        annotation = self._new_annotation(kind, x=x, y=y, width=width, height=height,
                                          opacity=0.8)
        return annotation

    def _finish_shape(self, kind: str, start: Optional[tuple[float, float]],
                      end: Optional[tuple[float, float]]) -> None:
        if start is None or end is None:
            return
        if kind in (TOOL_LINE, TOOL_ARROW):
            if self._distance(start, end) * self._zoom < MIN_DRAW_SIZE:
                return
            annotation = self._new_annotation(
                kind,
                x=min(start[0], end[0]),
                y=min(start[1], end[1]),
                width=abs(end[0] - start[0]),
                height=abs(end[1] - start[1]),
                points=[(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))],
            )
            self._emit_created(annotation)
            return
        rect = self._rect_from_points(start, end)
        if rect is None:
            return
        x, y, width, height = rect
        if width * self._zoom < MIN_DRAW_SIZE or height * self._zoom < MIN_DRAW_SIZE:
            return
        if kind == TOOL_CLOUD:
            # Контур-«хорда» облака — прямоугольник протяжки; зубчатый периметр
            # строится из него одинаково на холсте и в потоке внешнего вида.
            self._emit_created(self._new_annotation(
                kind, x=x, y=y, width=width, height=height,
                points=rectangle_points(x, y, width, height)))
            return
        self._emit_created(self._new_annotation(kind, x=x, y=y, width=width, height=height))

    # ---------------------------------------------------------- многоугольник
    def _add_polygon_vertex(self, pos: QPointF, allow_close: bool = True) -> None:
        """Добавить вершину; щелчок по первой вершине замыкает контур."""
        point = self._to_pdf(pos)
        step = POLYGON_STEP_TOLERANCE / max(self._zoom, 1e-6)
        if self._polygon_points:
            if self._distance(self._polygon_points[-1], point) < step:
                return
            if (allow_close and len(self._polygon_points) >= 3
                    and self._distance(self._polygon_points[0], point) * self._zoom
                    <= POLYGON_CLOSE_TOLERANCE):
                self._finish_polygon()
                return
        self._polygon_points.append(point)
        self._polygon_cursor = point
        self.update()

    def _finish_polygon(self) -> bool:
        """Замкнуть многоугольник и создать аннотацию (False — вершин мало)."""
        points = normalize_polygon(self._polygon_points)
        was_drawing = bool(self._polygon_points)
        self._polygon_points = []
        self._polygon_cursor = None
        if len(points) < 3:
            self.update()
            if was_drawing:
                self.notice.emit("Многоугольник: нужно не меньше трёх вершин.")
            return False
        box = polygon_bounds(points)
        if box is None or max(box[2], box[3]) * self._zoom < MIN_DRAW_SIZE:
            self.update()
            self.notice.emit("Многоугольник слишком мал: разнесите вершины дальше.")
            return False
        annotation = self._new_annotation(
            TOOL_POLYGON, x=box[0], y=box[1], width=box[2], height=box[3], points=points)
        self._emit_created(annotation)
        index = len(self._annotations) - 1
        if index >= 0:
            self._set_selected(index)
        self.update()
        return True

    def _cancel_interaction(self) -> None:
        """Сбросить жест и незавершённое построение многоугольника."""
        self._polygon_points = []
        self._polygon_cursor = None
        super()._cancel_interaction()

    def _create_note_at(self, pos: QPointF) -> None:
        x, y = self._to_pdf(pos)
        page_w, page_h = self._page_size
        size = NOTE_ICON_SIZE
        x = min(max(x, 0.0), max(page_w - size, 0.0))
        y = min(max(y, 0.0), max(page_h - size, 0.0))
        annotation = self._new_annotation(
            TOOL_NOTE, x=x, y=y, width=size, height=size, text="",
            color=NOTE_ICON_COLOR, stroke_width=1.0,
        )
        self._emit_created(annotation)
        index = len(self._annotations) - 1
        if index >= 0:
            self._set_selected(index)
            self._open_note_popover(index)

    def _place_pending_image(self, pos: QPointF) -> bool:
        data = self._pending_image_data
        if not data:
            return False
        kind = self._pending_image_kind if self._pending_image_kind in IMAGE_TOOLS else self._tool
        width, height = self._natural_size(data)
        x, y = self._to_pdf(pos)
        x, y, width, height = self._clamp_rect(x, y, width, height)
        annotation = self._new_annotation(
            kind, x=x, y=y, width=width, height=height, image_data=data,
        )
        self._emit_created(annotation)
        index = len(self._annotations) - 1
        if index >= 0:
            self._set_selected(index)
        return True

    # ============================================================ текстовый ввод
    def _prompt_text(self, title: str, initial: str) -> Optional[str]:  # noqa: ARG002
        """Модальный ввод запрещён: текст правится прямо на странице."""
        return None

    def _create_text_at(self, pos: QPointF) -> None:
        """Клик инструментом «Текст» — inline-редактор вместо QInputDialog."""
        x, y = self._to_pdf(pos)
        page_w, page_h = self._page_size
        width = min(TEXT_EDITOR_WIDTH, max(page_w - x, TEXT_EDITOR_MIN_WIDTH))
        height = min(max(self._font_size * 2.2, TEXT_EDITOR_MIN_HEIGHT),
                     max(page_h - y, TEXT_EDITOR_MIN_HEIGHT))
        self._begin_inline_editor(-1, "", (x, y), (width, height), None)

    def _edit_text(self, index: int) -> None:
        """Правка существующего текста — тот же inline-редактор."""
        self._begin_inline_edit(index)

    def _begin_inline_edit(self, index: int) -> bool:
        if not (0 <= index < len(self._annotations)):
            return False
        annotation = self._annotations[index]
        if annotation.kind != TOOL_TEXT:
            return False
        self._set_selected(index)
        width = max(_as_float(annotation.width, 0.0), TEXT_EDITOR_MIN_WIDTH)
        height = max(_as_float(annotation.height, 0.0), TEXT_EDITOR_MIN_HEIGHT)
        return self._begin_inline_editor(
            index, str(annotation.text or ""),
            (float(annotation.x), float(annotation.y)), (width, height), annotation,
        )

    def _begin_inline_editor(self, index: int, text: str,
                             origin: tuple[float, float],
                             box: tuple[float, float],
                             source: Optional[Annotation]) -> bool:
        self._commit_inline_text()
        self._close_note_popover(flush=True)
        if self._readonly:
            self._readonly_notice()
            return False

        editor = QTextEdit(self)
        editor.setObjectName("inlineTextEditor")
        editor.setAcceptRichText(False)
        editor.setTabChangesFocus(True)
        editor.setPlainText(text)
        editor.setStyleSheet(
            "QTextEdit#inlineTextEditor { background: rgba(255,255,255,240);"
            " border: 1px dashed %s; padding: 1px; }" % SELECTION_COLOR
        )
        editor.installEventFilter(self)

        self._text_editor = editor
        self._inline_index = int(index)
        self._inline_target = source
        self._inline_origin = (float(origin[0]), float(origin[1]))
        self._inline_box = (max(float(box[0]), 1.0), max(float(box[1]), 1.0))
        self._inline_style = {
            "font_family": (source.font_family if source is not None else self._font_family),
            "font_size": _as_float(source.font_size if source is not None else self._font_size,
                                   DEFAULT_FONT_SIZE),
            "color": _valid_color(source.color if source is not None else self._style_color(TOOL_TEXT))
            or DEFAULT_COLOR,
            "bold": bool(getattr(source, "bold", self._bold)) if source is not None else self._bold,
            "alignment": (str(getattr(source, "alignment", self._alignment) or "left")
                          if source is not None else self._alignment),
        }
        self._layout_inline_editor()
        self._apply_inline_font()
        editor.show()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        if text:
            cursor = editor.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            editor.setTextCursor(cursor)
        self.update()
        return True

    def _apply_inline_font(self) -> None:
        editor = self._text_editor
        if editor is None:
            return
        font = QFont(str(self._inline_style.get("font_family") or DEFAULT_FONT_FAMILY))
        size = _as_float(self._inline_style.get("font_size"), DEFAULT_FONT_SIZE)
        font.setPixelSize(max(1, int(round(size * self._zoom))))
        font.setBold(bool(self._inline_style.get("bold", False)))
        editor.setFont(font)

    def _layout_inline_editor(self) -> None:
        editor = self._text_editor
        if editor is None:
            return
        width, height = self._inline_box
        x, y = self._inline_origin
        x, y, width, height = self._clamp_rect(x, y, width, height)
        rect = QRectF(x * self._zoom, y * self._zoom,
                      max(width * self._zoom, 24.0), max(height * self._zoom, 18.0))
        editor.setGeometry(rect.toAlignedRect())

    def _cancel_inline_text(self) -> None:
        self._text_commit_timer.stop()
        editor = self._text_editor
        self._text_editor = None
        self._inline_index = -1
        self._inline_target = None
        self._inline_style = {}
        if editor is not None:
            editor.removeEventFilter(self)
            editor.hide()
            editor.setParent(None)
            editor.deleteLater()
        self.update()

    def _commit_inline_text(self) -> None:
        self._text_commit_timer.stop()
        if self._text_editor is None or self._inline_committing:
            return
        self._inline_committing = True
        try:
            editor = self._text_editor
            text = editor.toPlainText().rstrip()
            index = self._inline_index
            origin = self._inline_origin
            style = dict(self._inline_style)
            self._cancel_inline_text()

            if index >= 0:
                if not (0 <= index < len(self._annotations)):
                    return
                annotation = self._annotations[index]
                if annotation.kind != TOOL_TEXT:
                    return
                if not text.strip():
                    self._delete_annotation_at(index)
                    return
                if text == str(annotation.text or ""):
                    return
                updated = copy.deepcopy(annotation)
                updated.text = text
                measured_w, measured_h = self._measure_text(updated)
                updated.width = max(_as_float(annotation.width, 0.0), measured_w, 1.0)
                updated.height = max(_as_float(annotation.height, 0.0), measured_h, 1.0)
                updated.x, updated.y, updated.width, updated.height = self._clamp_rect(
                    updated.x, updated.y, updated.width, updated.height
                )
                self._emit_edited(index, updated)
                return

            if not text.strip():
                return
            annotation = self._new_annotation(
                TOOL_TEXT,
                x=float(origin[0]),
                y=float(origin[1]),
                text=text,
                color=str(style.get("color") or self._style_color(TOOL_TEXT)),
                font_family=str(style.get("font_family") or self._font_family),
                font_size=_as_float(style.get("font_size"), self._font_size),
                bold=bool(style.get("bold", self._bold)),
                alignment=str(style.get("alignment") or self._alignment),
            )
            measured_w, measured_h = self._measure_text(annotation)
            annotation.width = max(measured_w, 1.0)
            annotation.height = max(measured_h, 1.0)
            annotation.x, annotation.y, annotation.width, annotation.height = self._clamp_rect(
                annotation.x, annotation.y, annotation.width, annotation.height
            )
            self._emit_created(annotation)
            index = len(self._annotations) - 1
            if index >= 0:
                self._set_selected(index)
        finally:
            self._inline_committing = False

    # ============================================================ комментарии
    def _create_icon_label(self, text: str, object_name: str, frame: QWidget) -> QLabel:
        label = QLabel(text, frame)
        label.setObjectName(object_name)
        return label

    def _open_note_popover(self, index: int) -> bool:
        if not (0 <= index < len(self._annotations)):
            return False
        annotation = self._annotations[index]
        if annotation.kind != TOOL_NOTE:
            return False
        if self._note_popover is not None and self._note_index == index:
            self._sync_note_popover()
            if self._note_editor is not None:
                self._note_editor.setFocus(Qt.FocusReason.OtherFocusReason)
            return True

        self._close_note_popover(flush=True)

        frame = QFrame(self)
        frame.setObjectName("notePopover")
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setStyleSheet(NOTE_POPOVER_STYLE)
        frame.setFixedWidth(NOTE_POPOVER_WIDTH)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        author = self._create_icon_label("Мой комментарий", "noteAuthor", frame)
        author_font = QFont(self.font())
        author_font.setPixelSize(11)
        author_font.setWeight(QFont.Weight.DemiBold)
        author.setFont(author_font)

        when = self._create_icon_label("Только что", "noteTime", frame)
        when_font = QFont(self.font())
        when_font.setPixelSize(9)
        when.setFont(when_font)

        editor = QTextEdit(frame)
        editor.setObjectName("noteText")
        editor.setAcceptRichText(False)
        editor.setTabChangesFocus(True)
        editor.setPlainText(str(annotation.text or ""))
        editor.setPlaceholderText("Введите текст комментария")
        editor.setMinimumHeight(58)
        editor_font = QFont(self.font())
        editor_font.setPixelSize(11)
        editor.setFont(editor_font)

        hint = QLabel(
            "<b>Как переместить значок:</b> перетащите его мышью в нужное место "
            "страницы. Выделенный значок двигают стрелки ← ↑ → ↓, а Delete "
            "удаляет комментарий.",
            frame,
        )
        hint.setObjectName("noteHint")
        hint.setWordWrap(True)
        hint_font = QFont(self.font())
        hint_font.setPixelSize(10)
        hint.setFont(hint_font)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 6, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        remove = QPushButton("Удалить", frame)
        remove.setObjectName("noteDelete")
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setToolTip("Удалить комментарий (Ctrl+Z вернёт)")
        close = QPushButton("Закрыть", frame)
        close.setObjectName("noteClose")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        button_font = QFont(self.font())
        button_font.setPixelSize(11)
        remove.setFont(button_font)
        close.setFont(button_font)
        actions.addWidget(remove)
        actions.addWidget(close)

        layout.addWidget(author)
        layout.addWidget(when)
        layout.addWidget(editor)
        layout.addWidget(hint)
        layout.addLayout(actions)

        editor.textChanged.connect(self._on_note_text_changed)
        remove.clicked.connect(self._on_note_delete_clicked)
        close.clicked.connect(self._close_note_popover)

        self._note_popover = frame
        self._note_editor = editor
        self._note_delete = remove
        self._note_index = index
        self._update_note_readonly_state()
        frame.show()
        self._layout_note_popover()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        if annotation.text:
            cursor = editor.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            editor.setTextCursor(cursor)
        return True

    def _update_note_readonly_state(self) -> None:
        if self._note_editor is not None:
            self._note_editor.setReadOnly(self._readonly)
        if self._note_delete is not None:
            self._note_delete.setEnabled(not self._readonly)
            self._note_delete.setToolTip(
                "Документ открыт только для чтения" if self._readonly
                else "Удалить комментарий (Ctrl+Z вернёт)"
            )

    def _close_note_popover(self, flush: bool = True) -> None:
        frame = self._note_popover
        if frame is None:
            return
        if flush and self._note_timer.isActive():
            self._note_timer.stop()
            self._commit_note_text()
        elif not flush:
            self._note_timer.stop()
        self._note_popover = None
        self._note_editor = None
        self._note_delete = None
        self._note_index = -1
        frame.hide()
        frame.setParent(None)
        frame.deleteLater()

    def _note_annotation(self) -> Optional[Annotation]:
        index = self._note_index
        if self._note_popover is None or not (0 <= index < len(self._annotations)):
            return None
        annotation = self._annotations[index]
        if annotation.kind != TOOL_NOTE:
            return None
        return annotation

    def _note_display_annotation(self) -> Optional[Annotation]:
        """Аннотация для позиционирования поповера: превью жеста важнее модели."""
        index = self._note_index
        if index == self._move_index and self._move_preview is not None:
            return self._move_preview
        if index == self._resize_index and self._resize_preview is not None:
            return self._resize_preview
        return self._note_annotation()

    def _on_note_text_changed(self) -> None:
        self._note_timer.start(NOTE_SAVE_DELAY_MS)

    def _commit_note_text(self) -> None:
        self._note_timer.stop()
        annotation = self._note_annotation()
        editor = self._note_editor
        if annotation is None or editor is None:
            return
        text = editor.toPlainText()
        if text == str(annotation.text or ""):
            return
        index = self._note_index
        updated = copy.deepcopy(annotation)
        updated.text = text
        self._emit_edited(index, updated)

    def _on_note_delete_clicked(self) -> None:
        index = self._note_index
        if index < 0:
            return
        self._close_note_popover(flush=False)
        self._delete_annotation_at(index)

    def _layout_note_popover(self) -> None:
        frame = self._note_popover
        if frame is None:
            return
        annotation = self._note_display_annotation()
        if annotation is None:
            self._close_note_popover(flush=False)
            return
        x, y, width, height = self._annotation_bounds(annotation)
        icon = QRectF(x * self._zoom, y * self._zoom,
                      width * self._zoom, height * self._zoom)
        frame.adjustSize()
        pop_w = float(frame.width())
        pop_h = float(frame.height())
        margin = NOTE_POPOVER_MARGIN
        left = icon.right() + 10.0
        if left + pop_w > self.width() - margin:
            left = icon.left() - pop_w - 10.0
        max_left = self.width() - pop_w - margin
        left = margin if max_left <= margin else min(max(left, margin), max_left)
        max_top = self.height() - pop_h - margin
        top = icon.top() - 8.0
        top = margin if max_top <= margin else min(max(top, margin), max_top)
        frame.move(int(round(left)), int(round(top)))

    def _sync_note_popover(self) -> None:
        """Переставить поповер вслед за значком; закрыть, если комментарий исчез."""
        if self._note_popover is None:
            return
        if self._note_annotation() is None:
            self._close_note_popover(flush=False)
            return
        self._layout_note_popover()

    # ====================================================== общие переопределения
    def _apply_style_to_selection(self) -> None:
        """Смена стиля при выделенном объекте меняет сам объект (и сигнал наружу)."""
        index = self._selected
        if not (0 <= index < len(self._annotations)):
            return
        annotation = self._annotations[index]
        kind = str(annotation.kind or "")
        updated = copy.deepcopy(annotation)
        changed = False
        if kind in FONT_KINDS:
            if str(updated.font_family) != self._font_family:
                updated.font_family = self._font_family
                changed = True
            if abs(_as_float(updated.font_size, 0.0) - self._font_size) > 1e-9:
                updated.font_size = self._font_size
                changed = True
            if bool(getattr(updated, "bold", False)) != self._bold:
                updated.bold = self._bold
                changed = True
            if str(getattr(updated, "alignment", "left") or "left") != self._alignment:
                updated.alignment = self._alignment
                changed = True
        if kind in STROKE_KINDS and not _same_color(updated.color, self._color):
            updated.color = self._color
            changed = True
        if kind in WIDTH_KINDS and abs(_as_float(updated.stroke_width, 0.0) - self._stroke_width) > 1e-9:
            updated.stroke_width = self._stroke_width
            changed = True
        if kind in FILL_KINDS and not _same_color(
            getattr(updated, "fill_color", ""), self._fill_color
        ):
            updated.fill_color = self._fill_color
            changed = True
        if changed:
            self._emit_edited(index, updated)

    def _delete_selected(self) -> None:
        self._delete_annotation_at(self._selected)

    def _delete_annotation_at(self, index: int) -> None:
        if not (0 <= index < len(self._annotations)):
            return
        self._close_note_popover(flush=False)
        self._cancel_interaction()
        del self._annotations[index]
        if self._selected == index:
            self._set_selected(-1)
        elif self._selected > index:
            self._selected -= 1
        self.annotation_deleted.emit(index)
        self._apply_cursor()
        self.update()

    def _update_move_preview(self) -> None:
        super()._update_move_preview()
        self._sync_note_popover()

    def _set_selected(self, index: int) -> None:
        before = self._selected
        super()._set_selected(index)
        if (self._note_popover is not None and self._selected != self._note_index
                and self._selected != before):
            self._close_note_popover(flush=True)

    def set_zoom(self, zoom: float) -> None:
        before = self._zoom
        super().set_zoom(zoom)
        if abs(self._zoom - before) > 1e-9:
            self._layout_inline_editor()
            self._sync_note_popover()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._sync_note_popover()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
        editor = self._text_editor
        if editor is not None and (watched is editor or watched is editor.viewport()):
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    self._cancel_inline_text()
                    event.accept()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier
                ):
                    self._commit_inline_text()
                    event.accept()
                    return True
            elif event.type() == QEvent.Type.FocusOut:
                # Коммит отложен: клик по другому месту страницы не должен
                # обрывать обработку события у самого холста.
                self._text_commit_timer.start(0)
        return super().eventFilter(watched, event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._polygon_cursor is not None:
            self._polygon_cursor = None
            self.update()
        super().leaveEvent(event)

    # -------------------------------------------------------- формы (наследие)
    def set_form_widgets(self, widgets: Any, force_values: bool = False) -> None:
        """Обновить поля формы, не сбивая набор в активном редакторе.

        ``PageCanvas`` при неизменной геометрии обновляет значения на месте и
        сохраняет сами виджеты. Чтобы текст, который пользователь печатает
        прямо сейчас, не «прыгал» в конец строки, значение активного поля
        берётся из редактора, а не из модели. ``force_values=True`` отключает
        это послабление: так владелец страницы возвращает в поле настоящее
        значение модели после отказа backend-а.
        """
        items = widgets
        if isinstance(widgets, (list, tuple)) and not force_values:
            live_key, live_value = self._focused_form_value()
            if live_key is not None:
                patched: list[Any] = []
                for raw in widgets:
                    if isinstance(raw, dict) and str(raw.get("key")) == live_key:
                        patched.append(dict(raw, value=live_value))
                    else:
                        patched.append(raw)
                items = patched
        super().set_form_widgets(items)

    def _apply_form_font(self, editor: QWidget, widget: dict[str, Any], zoom: float) -> None:
        """Keep page fields at PDF size, independent of toolbar input styling."""
        super()._apply_form_font(editor, widget, zoom)
        kind = widget.get('kind')
        if kind in ('text', 'choice'):
            size = int(editor.property('pdfFormFontSize') or 1)
            family = str(editor.property('pdfFormFontFamily') or 'Arial')
            css = (
                'QLineEdit, QTextEdit, QComboBox { padding: 0px; margin: 0px; '
                'min-width: 0px; min-height: 0px; border-radius: 1px; '
                'border: 1px solid rgba(22,125,153,65); '
                'background: rgba(230,244,249,180); color: #143744; '
                f'font-size: {size}px; font-family: "{family}"; }} '
                'QLineEdit:focus, QTextEdit:focus, QComboBox:focus { '
                'border: 1px solid #167d99; background: #f2fbff; } '
                'QComboBox::drop-down { width: 10px; border: 0px; } '
                'QScrollBar:vertical { width: 4px; }'
            )
        elif kind in ('checkbox', 'radio'):
            size = max(4, round(min(widget['width'], widget['height']) * zoom) - 2)
            radius = (size + 1) // 2 if kind == 'radio' else 1
            css = (
                'QCheckBox, QRadioButton { padding: 0px; margin: 0px; spacing: 0px; '
                'background: transparent; min-width: 0px; min-height: 0px; } '
                f'QCheckBox::indicator, QRadioButton::indicator {{ width: {size}px; '
                f'height: {size}px; border-radius: {radius}px; '
                'border: 1px solid #167d99; background: #f2fbff; } '
                'QCheckBox::indicator:checked, QRadioButton::indicator:checked { '
                'background: #167d99; }'
            )
        else:
            return
        targets = ([editor] if isinstance(editor, (QCheckBox, QRadioButton)) else
                   editor.findChildren(QRadioButton) if kind == 'radio' else [editor])
        for target in targets:
            if target.styleSheet() != css:
                target.setStyleSheet(css)
        if kind in ('text', 'choice'):
            # Keep local QSS in lockstep with the requested PDF font. Reading
            # editor.font() here is unsafe because the app stylesheet may have
            # already substituted its global UI font and size.
            font = editor.font()
            font.setFamily(family)
            font.setPixelSize(size)
            editor.setFont(font)

    def _focused_form_value(self) -> tuple[Optional[str], Any]:
        key = self._focused_form_key()
        if key is None:
            return (None, None)
        editor = self.form_editor(key)
        if editor is None:
            return (None, None)
        if isinstance(editor, QLineEdit):
            return (key, editor.text())
        if isinstance(editor, QTextEdit):
            return (key, editor.toPlainText())
        if isinstance(editor, (QCheckBox, QRadioButton)):
            return (key, bool(editor.isChecked()))
        if isinstance(editor, QComboBox):
            return (key, editor.currentText())
        for child in editor.findChildren(QRadioButton):
            if child.isChecked():
                return (key, child.text())
        return (key, None)
