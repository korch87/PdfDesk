"""``DocumentView`` — непрерывная лента страниц редактора PdfDesk.

Виджет владеет списком ``EditorCanvas`` (по одному на страницу), показывает их
в одной прокручиваемой ленте, лениво рендерит растры для длинных документов и
превращает сигналы холстов в мутации ``PdfSession``.

Контракт (docs/rebuild-contract.txt):

* ``DocumentView(session, parent=None)``; ``reload(preserve_scroll=True)``;
  ``set_zoom(float)``; ``set_tool(str)``; ``set_style(**kwargs)``;
  ``set_image_data(str, kind='image')``; ``focus_page(index)``;
  ``current_page_index``; ``canvases: list[EditorCanvas]``;
* сигналы ``page_changed(int)``, ``document_changed()``,
  ``selection_changed(int, int)`` — (страница, индекс аннотации);
* инструменты, стиль, масштаб и режим «только чтение» расходятся по всем
  страницам; обратные вызовы переводят индекс страницы, обновляют растры и
  данные форм, а сам текст правок уходит в сессию;
* выделенный исходный текст подсвечивается пакетом нативных аннотаций:
  ``EditorCanvas.text_highlight_requested`` → ``PdfSession.add_annotations``
  (одна ступень отмены на всё выделение), а право правки видно снаружи через
  ``text_highlight_available``;
* ``undo``/``redo`` вызываются окном, после чего окно вызывает ``reload()``.

Формы: данные берутся из ``PdfSession.form_widgets(index)`` без изменения
контракта (``{key,rect,kind,value,options,readonly,multiline,export_value}``),
значения уходят в ``PdfSession.set_form_value(key, export_value)``. При
обновлении полей активный редактор и позиция курсора сохраняются, поэтому
набор текста не сбивается. Ошибочные (пока не поддержанные backend-ом) типы
аннотаций не «проглатываются»: исключение сессии показывается сигналом
``error``, а страница перечитывается из модели.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:  # импорт как пакета: from src.document_view import DocumentView
    from .canvas import (
        CANVAS_BACKGROUND,
        DEFAULT_HIGHLIGHT_COLOR,
        HIGHLIGHT_OPACITY,
        MAX_ZOOM,
        MIN_ZOOM,
        TOOL_FORM,
        TOOL_SELECT,
    )
    from .contracts import Annotation
    from .editor_canvas import (
        EDITOR_TOOLS,
        IMAGE_TOOLS,
        TOOL_IMAGE,
        EditorCanvas,
    )
    from .pdf_text import TextChar, page_chars, search_text
    from .view_rotation import (
        ViewRotation, normalize_rotation, rotate_annotation_geometry, rotate_point,
        rotate_image, rotate_rect, rotated_size,
    )
except ImportError:  # импорт при sys.path, указывающем на src/
    from canvas import (  # type: ignore[no-redef]
        CANVAS_BACKGROUND,
        DEFAULT_HIGHLIGHT_COLOR,
        HIGHLIGHT_OPACITY,
        MAX_ZOOM,
        MIN_ZOOM,
        TOOL_FORM,
        TOOL_SELECT,
    )
    from contracts import Annotation  # type: ignore[no-redef]
    from editor_canvas import (  # type: ignore[no-redef]
        EDITOR_TOOLS,
        IMAGE_TOOLS,
        TOOL_IMAGE,
        EditorCanvas,
    )
    from pdf_text import TextChar, page_chars, search_text  # type: ignore[no-redef]
    from view_rotation import (  # type: ignore[no-redef]
        ViewRotation, normalize_rotation, rotate_annotation_geometry, rotate_point,
        rotate_image, rotate_rect, rotated_size,
    )


class DocumentView(QWidget):
    """Непрерывная лента страниц с аннотациями, формами и ленивым рендером."""

    page_changed = Signal(int)            # индекс текущей страницы
    document_changed = Signal()           # модель изменилась (dirty)
    selection_changed = Signal(int, int)  # (страница, индекс аннотации) или (-1,-1)
    error = Signal(str)                   # честное сообщение об ошибке
    notice = Signal(str)                  # необязательная подсказка
    zoom_changed = Signal(float)          # общий масштаб ленты (в т.ч. Ctrl+колесо)
    native_text_selected = Signal(int, str)  # страница, выделенный исходный текст

    #: До какого числа страниц рендерить всё сразу (иначе — ленивая лента).
    LAZY_PAGE_LIMIT = 20
    #: Сколько первых страниц отрисовать сразу в ленивом режиме.
    LAZY_INITIAL_PAGES = 3
    #: Запас в пикселях вокруг видимой области для предзагрузки страниц.
    LAZY_MARGIN = 240
    #: Максимальный масштаб растра (1 пункт = 1 px при scale=1.0).
    MAX_RASTER_SCALE = 2.0
    #: Задержка перерисовки растра после правки формы, мс.
    RASTER_REFRESH_DELAY_MS = 400
    PAGE_GAP = 14
    PAGE_MARGIN = 16

    def __init__(self, session: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._session = session
        self._zoom = 1.0
        self._tool = TOOL_SELECT
        self._readonly = False
        self._view_rotation = ViewRotation()
        self._native_image_mode = False
        self._style: dict[str, Any] = {
            "font_family": "Arial",
            "font_size": 14.0,
            "color": "#2457D6",
            "stroke_width": 2.0,
            "bold": False,
            "alignment": "left",
            "fill_color": "",
            "opacity": 1.0,
        }
        self._style_set = False
        self._pending_image: tuple[str, str] = ("", TOOL_IMAGE)

        self._page_sizes: list[tuple[float, float]] = []
        self._source_page_sizes: list[tuple[float, float]] = []
        self._canvas_page_ids: list[int] = []
        self._canvas_rotations: list[int] = []
        self._guide_cache: dict[int, tuple[list, bool, tuple, int]] = {}
        self._loaded: list[bool] = []
        self._render_scales: list[float] = []
        self._form_cache: dict[int, list[dict]] = {}
        self._stale_pages: set[int] = set()

        self._current = 0
        self._selected: tuple[int, int] = (-1, -1)
        self._syncing = False
        self._last_error = ""
        #: Есть ли право правки PDF (без него подсветка текста запрещена).
        self._text_highlight_available = False

        self._placeholder = QLabel("Документ не открыт", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(
            "QLabel { color: #C7CDD4; font-size: 15px; }"
        )

        self._area = QScrollArea(self)
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.Shape.NoFrame)
        self._area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._area.viewport().setAutoFillBackground(True)
        palette = self._area.viewport().palette()
        palette.setColor(self._area.viewport().backgroundRole(), QColor(CANVAS_BACKGROUND))
        self._area.viewport().setPalette(palette)

        self._container = QWidget()
        self._container.setAutoFillBackground(True)
        container_palette = self._container.palette()
        container_palette.setColor(self._container.backgroundRole(), QColor(CANVAS_BACKGROUND))
        self._container.setPalette(container_palette)
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, self.PAGE_MARGIN, 0, self.PAGE_MARGIN)
        self._layout.setSpacing(self.PAGE_GAP)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._area.setWidget(self._container)
        self._container.installEventFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._placeholder)
        outer.addWidget(self._area)

        self._raster_timer = QTimer(self)
        self._raster_timer.setSingleShot(True)
        self._raster_timer.timeout.connect(self.flush_pending_refreshes)

        self._area.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self._area.horizontalScrollBar().valueChanged.connect(self._on_scrolled)

        #: Публичный список холстов: ``canvases[index]`` — страница ``index``.
        self.canvases: list[EditorCanvas] = []

        self._rebuild_pages()

    # =========================================================== публичный API
    @property
    def session(self) -> Any:
        """Активная сессия документа (может быть None)."""
        return self._session

    @property
    def page_count(self) -> int:
        """Количество страниц документа."""
        return len(self.canvases)

    @property
    def current_page_index(self) -> int:
        """Индекс страницы, видимой в центре области прокрутки."""
        return self._current

    @property
    def zoom(self) -> float:
        """Текущий масштаб отображения."""
        return self._zoom

    @property
    def current_tool(self) -> str:
        """Активный инструмент (общий для всех страниц)."""
        return self._tool

    @property
    def readonly(self) -> bool:
        """Режим «только чтение»."""
        return self._readonly

    @property
    def view_rotation(self) -> int:
        """Clockwise rotation of this view in degrees; never persisted to PDF."""
        return self._view_rotation.degrees

    def rotate_view(self, degrees: int = 90) -> int:
        """Rotate the read-only view clockwise by a multiple of 90 degrees."""
        turn = normalize_rotation(degrees)
        if not self._readonly:
            self.notice.emit("Поворот вида доступен только в режиме просмотра.")
            return self.view_rotation
        return self.set_view_rotation((self.view_rotation + turn) % 360)

    def set_view_rotation(self, degrees: int = 0) -> int:
        """Set this view's absolute rotation without changing document state."""
        turn = normalize_rotation(degrees)
        if turn == self.view_rotation:
            return turn
        if not self._readonly and turn:
            self.notice.emit("Поворот вида доступен только в режиме просмотра.")
            return self.view_rotation
        self._remember_guides()
        old_turn = self.view_rotation
        rotation_delta = (turn - old_turn) % 360
        anchor = self._scroll_anchor()
        self._view_rotation.degrees = turn
        self._loaded = [False] * len(self.canvases)
        self._render_scales = [0.0] * len(self.canvases)
        for index, canvas in enumerate(self.canvases):
            old_width, old_height = self._page_sizes[index]
            old_guides = list(getattr(canvas, "guides", ()))
            guides_visible = bool(getattr(canvas, "guides_visible", False))
            transformed_guides = []
            for axis, position in old_guides:
                position = float(position)
                if axis == "v":
                    first = rotate_point((position, 0.0), old_width, old_height,
                                         rotation_delta)
                    second = rotate_point((position, old_height), old_width,
                                          old_height, rotation_delta)
                else:
                    first = rotate_point((0.0, position), old_width, old_height,
                                         rotation_delta)
                    second = rotate_point((old_width, position), old_width,
                                          old_height, rotation_delta)
                if math.isclose(first[0], second[0], abs_tol=1e-9):
                    transformed_guides.append(("v", (first[0] + second[0]) / 2.0))
                else:
                    transformed_guides.append(("h", (first[1] + second[1]) / 2.0))
            self._page_sizes[index] = rotated_size(
                *self._source_page_sizes[index], turn
            )
            canvas.set_raster_includes_annotations(bool(turn and self._readonly))
            canvas.set_page(None, self._page_sizes[index], self._visual_annotations(index))
            if old_guides:
                canvas.set_guides(transformed_guides, guides_visible)
        self._layout.activate()
        self._render_initial()
        self._ensure_visible_rendered()
        self._restore_guides()
        self._layout.activate()
        self._restore_scroll_anchor(anchor)
        return turn

    @property
    def text_highlight_available(self) -> bool:
        """Можно ли подсвечивать выделенный исходный текст этого документа.

        Флаг отражает **право правки** PDF: для файла, открытого с паролем
        пользователя и ограниченными правами, он ``False``, и пункт меню
        «Выделить текст» не появляется.
        """
        return self._text_highlight_available

    @property
    def last_error(self) -> str:
        """Текст последней ошибки (пусто, если ошибок не было)."""
        return self._last_error

    def canvas_at(self, index: int) -> Optional[EditorCanvas]:
        """Холст страницы ``index`` или None."""
        if 0 <= index < len(self.canvases):
            return self.canvases[index]
        return None

    def rendered_pages(self) -> list[int]:
        """Индексы страниц, для которых уже загружен растр."""
        return [index for index, flag in enumerate(self._loaded) if flag]

    def pending_raster_pages(self) -> list[int]:
        """Страницы, ожидающие отложенной перерисовки растра."""
        return sorted(self._stale_pages)

    def selected_annotation(self) -> tuple[int, int]:
        """Текущее выделение ``(страница, индекс)`` или ``(-1, -1)``."""
        return self._selected

    # --------------------------------------------------------------- страницы
    def reload(self, preserve_scroll: bool = True) -> None:
        """Перечитать документ из сессии (после undo/redo, поворотов, правок)."""
        bar = self._area.verticalScrollBar()
        value = bar.value() if preserve_scroll else 0

        # Права PDF могли измениться (снятие защиты владельцем) — пересчитываем.
        self._refresh_text_highlight_capability()
        self._remember_guides()
        count = self._session_page_count()
        if count != len(self.canvases) or self._page_identities() != self._canvas_page_ids:
            self._rebuild_pages()
        else:
            for index in range(count):
                source_size = self._session_page_size(index)
                size = rotated_size(*source_size, self.view_rotation)
                if source_size != self._source_page_sizes[index]:
                    self._source_page_sizes[index] = source_size
                if size != self._page_sizes[index]:
                    self._page_sizes[index] = size
                    self.canvases[index].set_page(
                        None, size, self._visual_annotations(index)
                    )
                self.canvases[index].set_raster_includes_annotations(
                    bool(self.view_rotation and self._readonly)
                )
            self._loaded = [False] * count
            self._render_scales = [0.0] * count
            self._form_cache.clear()
            self._stale_pages.clear()
            self._raster_timer.stop()
            self._layout.activate()
            self._render_initial()

        self._restore_guides()

        self._layout.activate()
        bar.setValue(value)
        self._layout.activate()
        self._update_current_page()

    def focus_page(self, index: int) -> bool:
        """Показать страницу ``index`` и передать ей фокус клавиатуры."""
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            raise ValueError("Индекс страницы должен быть целым числом.") from None
        canvas = self.canvas_at(wanted)
        if canvas is None:
            return False
        self._render_page(wanted)
        self._scroll_to_canvas(canvas)
        self._set_current(wanted, emit=True)
        canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def set_zoom(self, zoom: float) -> None:
        """Общий масштаб ленты: 1.0 = 100 %."""
        try:
            value = float(zoom)
        except (TypeError, ValueError):
            raise ValueError("Масштаб должен быть числом.") from None
        if not math.isfinite(value):
            raise ValueError("Масштаб должен быть конечным числом.")
        value = min(max(value, MIN_ZOOM), MAX_ZOOM)
        if abs(value - self._zoom) < 1e-9:
            return
        anchor = self._scroll_anchor()
        self._zoom = value
        for canvas in self.canvases:
            canvas.set_zoom(value)
        self._layout.activate()
        self._invalidate_rasters()
        self._ensure_visible_rendered()
        self._layout.activate()
        self._restore_scroll_anchor(anchor)
        self.zoom_changed.emit(self._zoom)

    def set_tool(self, tool: str) -> None:
        """Активировать инструмент на всех страницах."""
        if tool not in EDITOR_TOOLS:
            raise ValueError(
                "Неизвестный инструмент: {0!r}. Допустимые значения: {1}.".format(
                    tool, ", ".join(EDITOR_TOOLS)
                )
            )
        if tool == TOOL_FORM and self.view_rotation:
            self.set_view_rotation(0)
            self.notice.emit("Поворот вида сброшен перед заполнением формы.")
        previous_draw_forms = self._tool != TOOL_FORM
        self._tool = tool
        for canvas in self.canvases:
            canvas.set_tool(tool)
        if previous_draw_forms != (self._tool != TOOL_FORM):
            # PDFium owns appearances while the Qt editors are hidden; while
            # editing forms the raster omits widgets beneath those editors.
            for index, loaded in enumerate(self._loaded):
                if loaded:
                    self._loaded[index] = False
            self._ensure_visible_rendered()

    def set_style(self, **kwargs: Any) -> None:
        """Задать стиль новых объектов (и выбранного объекта на активной странице)."""
        if not kwargs:
            return
        self._style.update(kwargs)
        self._style_set = True
        for canvas in self.canvases:
            canvas.set_style(**kwargs)

    def set_image_data(self, image_data: str, kind: str = TOOL_IMAGE) -> None:
        """Передать base64-картинку для вставки по клику на любой странице."""
        if kind not in IMAGE_TOOLS:
            raise ValueError(
                "Недопустимый тип вставки: {0!r}. Ожидается {1}.".format(
                    kind, " или ".join(IMAGE_TOOLS)
                )
            )
        self._pending_image = (str(image_data or ""), kind)
        if self._pending_image[0]:
            self.set_tool(kind)
        for canvas in self.canvases:
            canvas.set_image_data(image_data, kind)

    def set_readonly(self, flag: bool) -> None:
        """Режим просмотра: правка аннотаций запрещена, формы работают по флагам полей."""
        flag = bool(flag)
        if not flag and self.view_rotation:
            self.set_view_rotation(0)
            self.notice.emit("Поворот вида сброшен перед редактированием.")
        if flag != self._readonly:
            self._readonly = flag
            for canvas in self.canvases:
                canvas.set_readonly(flag)
            if flag:
                for index, loaded in enumerate(self._loaded):
                    if loaded:
                        self._load_text_layer(index)
        self._refresh_text_highlight_capability()

    # --------------------------------------------- подсветка исходного текста
    def _has_edit_permission(self) -> bool:
        """Право менять аннотации/содержимое (свойство или метод сессии)."""
        if self._session is None:
            return False
        allowed = getattr(self._session, "has_edit_permission", True)
        if callable(allowed):
            try:
                allowed = allowed()
            except Exception:  # noqa: BLE001 - заглушка сессии не должна ронять вид
                return False
        return bool(allowed)

    def _refresh_text_highlight_capability(self) -> bool:
        """Пересчитать право подсветки и раздать его всем холстам."""
        available = bool(not self._readonly and self._has_edit_permission())
        self._text_highlight_available = available
        for canvas in self.canvases:
            canvas.set_text_highlight_available(available)
        return available

    def _compose_text_highlights(self, rects: Any) -> list[Any]:
        """Жёлтые нативные подсветки: по одной аннотации на строку выделения."""
        annotations: list[Any] = []
        for rect in rects or ():
            try:
                x1, y1, x2, y2 = (float(value) for value in rect)
            except (TypeError, ValueError):
                continue
            if x2 - x1 <= 0.0 or y2 - y1 <= 0.0:
                continue
            annotations.append(Annotation(
                kind="highlight",
                x=x1,
                y=y1,
                width=x2 - x1,
                height=y2 - y1,
                color=DEFAULT_HIGHLIGHT_COLOR,
                opacity=HIGHLIGHT_OPACITY,
            ))
        return annotations

    def _on_text_highlight_requested(self, page: int, rects: Any) -> None:
        """Один пакет аннотаций на всё выделение: одно действие отмены."""
        if self._syncing or self._session is None:
            return
        canvas = self.canvas_at(page)
        annotations = self._compose_text_highlights(rects)
        if not annotations:
            return
        if not self._refresh_text_highlight_capability():
            self.notice.emit("Подсветка текста недоступна: нет права изменять PDF.")
            return
        try:
            batch = getattr(self._session, "add_annotations", None)
            if callable(batch):
                # Пакет проверяется целиком и попадает в одну ступень отмены.
                batch(int(page), annotations)
            else:  # облегчённые адаптеры сессии без пакетного метода
                for annotation in annotations:
                    self._session.add_annotation(int(page), annotation)
        except Exception as exc:  # noqa: BLE001 - ограничения прав и сбои backend-а
            self._report(
                "Не удалось подсветить выделенный текст на странице %d" % (page + 1), exc
            )
            self._reload_page(int(page))
            return
        if canvas is not None:
            canvas.clear_text_selection()
        self._sync_page_annotations(int(page))
        self.document_changed.emit()
        self.notice.emit("Подсвечено строк: %d." % len(annotations))

    def find_text(self, query: str) -> list[Any]:
        """Return every source-text match with its actual displayed glyph boxes."""
        if self._session is None:
            return []
        try:
            return search_text(self._session, query)
        except Exception as exc:  # noqa: BLE001 - keep a useful UI error
            self._report("Не удалось выполнить поиск по тексту PDF", exc)
            return []

    def show_search_result(self, page: int, rects: Any) -> bool:
        """Highlight real match rectangles and scroll the first one into view."""
        canvas = self.canvas_at(int(page))
        if canvas is None:
            return False
        self._render_page(int(page))
        for item in self.canvases:
            item.set_search_highlights(())
        visual_rects = self._visual_search_rects(int(page), rects)
        canvas.set_search_highlights(visual_rects)
        self._layout.activate()
        if visual_rects:
            x1, y1, x2, y2 = visual_rects[0]
            center_y = canvas.y() + int(round((float(y1) + float(y2)) * self._zoom / 2.0))
            target = center_y - self._area.viewport().height() // 2
            bar = self._area.verticalScrollBar()
            bar.setValue(max(0, min(bar.maximum(), int(target))))
        self._set_current(int(page), emit=True)
        return True

    def style(self) -> dict[str, Any]:
        """Текущий стиль новых объектов (копия)."""
        return dict(self._style)

    # ---------------------------------------------------------------- формы
    def focus_form_field(self, key: str, page: Optional[int] = None) -> bool:
        """Показать и сфокусировать поле формы по техническому ключу.

        Поле может быть на невидимой (ещё не отрисованной) странице: она
        отрисовывается, включается инструмент ``form``, лента прокручивается к
        самому виджету, фокус уходит в него.
        """
        wanted = str(key)
        if not wanted:
            return False
        candidates: list[int] = []
        if page is not None:
            try:
                index = int(page)
            except (TypeError, ValueError):
                return False
            if self.canvas_at(index) is None:
                return False
            candidates = [index] if self._page_has_form_key(index, wanted) else []
        else:
            candidates = [index for index in range(len(self.canvases))
                          if self._page_has_form_key(index, wanted)]
        if not candidates:
            return False
        index = self._current if self._current in candidates else candidates[0]
        if not self._render_page(index):
            return False
        canvas = self.canvases[index]
        if not canvas.focus_form_field(wanted):
            return False
        self._scroll_widget_into_view(canvas, canvas.form_editor(wanted))
        self._set_current(index, emit=True)
        return True

    def form_field_entries(self) -> list[dict[str, Any]]:
        """Технические описания полей всех страниц для правой панели окна."""
        entries: list[dict[str, Any]] = []
        for index in range(len(self.canvases)):
            for widget in self._form_widgets(index):
                entries.append({
                    "page": index,
                    "key": str(widget.get("key", "")),
                    "kind": str(widget.get("kind", "")),
                    "value": widget.get("value"),
                    "export_value": widget.get("export_value"),
                    "readonly": bool(widget.get("readonly", False)),
                    "multiline": bool(widget.get("multiline", False)),
                })
        return entries

    # --------------------------------------------------------- отложенный растр
    def flush_pending_refreshes(self) -> list[int]:
        """Немедленно перерисовать растры страниц, ждущих обновления после форм."""
        self._raster_timer.stop()
        pages = sorted(self._stale_pages)
        self._stale_pages.clear()
        refreshed: list[int] = []
        for index in pages:
            canvas = self.canvas_at(index)
            if canvas is None or not self._loaded[index]:
                continue
            image = self._session_render(index)
            if image is None and self._session is not None:
                continue
            canvas.refresh_raster(image)
            refreshed.append(index)
        return refreshed

    # =========================================================== построение
    def _page_identities(self) -> list[int]:
        identity = getattr(self._session, "page_view_id", None)
        return [identity(i) if callable(identity) else i
                for i in range(self._session_page_count())]

    def _page_rotation(self, index: int) -> int:
        geometry = getattr(self._session, "_geometry", None)
        rotation = geometry(index).rotation if callable(geometry) else 0
        return (int(rotation) + self.view_rotation) % 360

    def _remember_guides(self) -> None:
        for index, identity in enumerate(self._canvas_page_ids):
            if index >= len(self.canvases):
                break
            canvas = self.canvases[index]
            self._guide_cache[identity] = (
                list(canvas.guides), canvas.guides_visible,
                self._page_sizes[index], self._canvas_rotations[index])

    def _restore_guides(self) -> None:
        identities = self._page_identities()
        rotations = [self._page_rotation(i) for i in range(len(identities))]
        for index, identity in enumerate(identities):
            cached = self._guide_cache.get(identity)
            if cached is None:
                continue
            guides, visible, (width, height), rotation = cached
            delta = (rotations[index] - rotation) % 360
            transformed = []
            for axis, position in guides:
                start, end = (((position, 0.0), (position, height)) if axis == "v"
                              else ((0.0, position), (width, position)))
                first = rotate_point(start, width, height, delta)
                second = rotate_point(end, width, height, delta)
                if math.isclose(first[0], second[0], abs_tol=1e-9):
                    transformed.append(("v", (first[0] + second[0]) / 2.0))
                else:
                    transformed.append(("h", (first[1] + second[1]) / 2.0))
            self.canvases[index].set_guides(transformed, visible)
        self._canvas_page_ids = identities
        self._canvas_rotations = rotations

    def _rebuild_pages(self) -> None:
        for canvas in self.canvases:
            self._layout.removeWidget(canvas)
            canvas.setParent(None)
            canvas.deleteLater()
        self.canvases = []
        self._page_sizes = []
        self._source_page_sizes = []
        self._loaded = []
        self._render_scales = []
        self._form_cache = {}
        self._stale_pages.clear()
        self._selected = (-1, -1)

        count = self._session_page_count()
        for index in range(count):
            canvas = EditorCanvas(self._container)
            canvas.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            source_size = self._session_page_size(index)
            self._source_page_sizes.append(source_size)
            size = rotated_size(*source_size, self.view_rotation)
            self._page_sizes.append(size)
            canvas.set_page(None, size, [])
            canvas.set_raster_includes_annotations(bool(self.view_rotation and self._readonly))
            canvas.set_zoom(self._zoom)
            canvas.set_tool(self._tool)
            canvas.set_readonly(self._readonly)
            if self._style_set:
                canvas.set_style(**self._style)
            if self._pending_image[0]:
                canvas.set_image_data(*self._pending_image)
            canvas.installEventFilter(self)
            canvas.annotation_created.connect(
                lambda annotation, i=index: self._on_annotation_created(i, annotation)
            )
            canvas.annotation_edited.connect(
                lambda position, annotation, i=index: self._on_annotation_edited(
                    i, position, annotation
                )
            )
            canvas.annotation_deleted.connect(
                lambda position, i=index: self._on_annotation_deleted(i, position)
            )
            canvas.selection_changed.connect(
                lambda position, i=index: self._on_selection_changed(i, position)
            )
            canvas.form_value_changed.connect(
                lambda key, value, i=index: self._on_form_value_changed(i, key, value)
            )
            canvas.zoom_changed.connect(
                lambda value, i=index: self._on_canvas_zoom(i, value)
            )
            canvas.notice.connect(self.notice)
            canvas.text_selected.connect(
                lambda text, i=index: self.native_text_selected.emit(i, text)
            )
            canvas.text_highlight_requested.connect(
                lambda rects, i=index: self._on_text_highlight_requested(i, rects)
            )
            self._layout.addWidget(canvas, 0, Qt.AlignmentFlag.AlignHCenter)
            self.canvases.append(canvas)
            self._loaded.append(False)
            self._render_scales.append(0.0)

        self._current = min(self._current, max(count - 1, 0))
        empty = count == 0
        self._placeholder.setVisible(empty)
        self._area.setVisible(not empty)
        self._refresh_text_highlight_capability()
        self._layout.activate()
        self._render_initial()
        self._restore_guides()
        self._layout.activate()

    def _render_initial(self) -> None:
        """Отрисовать стартовый набор страниц (для длинных файлов — лениво).

        Геометрия ленты до первого показа ещё не посчитана, поэтому «видимый
        диапазон» здесь не используется: видимые страницы догружает
        ``showEvent``/``resizeEvent`` и обработчик прокрутки.
        """
        count = len(self.canvases)
        if count == 0:
            return
        if count <= self.LAZY_PAGE_LIMIT:
            for index in range(count):
                self._render_page(index)
            return
        start = max(0, self._current - 1)
        for index in range(start, min(count, start + self.LAZY_INITIAL_PAGES)):
            self._render_page(index)

    def _render_page(self, index: int, force: bool = False) -> bool:
        """Загрузить страницу (растр + аннотации + формы), если ещё не загружена."""
        if not (0 <= index < len(self.canvases)):
            return False
        if self._loaded[index] and not force:
            return True
        canvas = self.canvases[index]
        # Не теряем текст, который пользователь набирает прямо сейчас.
        canvas.commit_inline_text()
        canvas.close_note_popover()
        image = self._session_render(index)
        self._syncing = True
        try:
            canvas.set_page(image, self._page_sizes[index],
                            self._visual_annotations(index))
            canvas.set_form_widgets(self._form_widgets(index, refresh=True))
            if self._selected[0] == index and self._selected[1] >= 0:
                canvas.select_annotation(self._selected[1])
        finally:
            self._syncing = False
        self._render_scales[index] = self._raster_scale()
        self._loaded[index] = True
        # Исходные глифы нужны и в правке (для «Выделить текст»), и в просмотре.
        self._load_text_layer(index)
        return True

    def _text_layer_supported(self) -> bool:
        """Может ли сессия отдать исходные глифы (реальный ``PdfSession``).

        Облегчённые адаптеры сессии не умеют сериализовать байты, поэтому
        текстовый слой для них просто пуст — без ложной ошибки в интерфейсе.
        """
        session = self._session
        if session is None:
            return False
        return (callable(getattr(session, "_write_bytes", None))
                and callable(getattr(session, "_geometry", None)))

    def _text_extraction_allowed(self) -> bool:
        """Разрешения PDF: ограниченному пользователю извлечение текста запрещено."""
        info = getattr(self._session, "security_info", None)
        if callable(info):
            try:
                info = info()
            except Exception:  # noqa: BLE001 - заглушка сессии
                info = None
        if not isinstance(info, dict) or not info.get("encrypted"):
            return True
        if info.get("authenticated_as") == "owner":
            return True
        permissions = info.get("permissions", {}) or {}
        return not (permissions.get("extract") is False or
                    permissions.get("extract_text_and_graphics") is False)

    def _load_text_layer(self, index: int) -> None:
        canvas = self.canvas_at(index)
        if canvas is None or self._session is None:
            return
        if not self._text_layer_supported() or not self._text_extraction_allowed():
            # Search still reads native glyph geometry through find_text(), but
            # the canvas receives no selectable text for a restricted user.
            canvas.set_text_chars(())
            return
        try:
            chars = page_chars(self._session, index)
            if self.view_rotation:
                width, height = self._source_page_sizes[index]
                chars = [TextChar(char.value, self._visual_xyxy(index, char.rect))
                         for char in chars]
            canvas.set_text_chars(chars)
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось прочитать текст страницы %d" % (index + 1), exc)

    def _visible_range(self) -> list[int]:
        if not self.canvases:
            return []
        if self._geometry_stale():
            # Лента ещё не разложена (до первого показа): «видимое» неизвестно,
            # поэтому ничего лишнего не рендерим — это делает Resize/Scroll.
            return []
        bar = self._area.verticalScrollBar()
        top = float(bar.value())
        bottom = top + float(max(self._area.viewport().height(), 1))
        margin = float(self.LAZY_MARGIN)
        visible: list[int] = []
        for index, canvas in enumerate(self.canvases):
            canvas_top = float(canvas.y())
            canvas_bottom = canvas_top + float(canvas.height())
            if canvas_bottom >= top - margin and canvas_top <= bottom + margin:
                visible.append(index)
        return visible

    def _geometry_stale(self) -> bool:
        """Контейнер ещё не разложен: высота меньше, чем нужно страницам."""
        expected = sum(canvas.sizeHint().height() for canvas in self.canvases)
        expected += self.PAGE_GAP * max(len(self.canvases) - 1, 0)
        expected += 2 * self.PAGE_MARGIN
        return self._container.height() < expected * 0.5

    def _ensure_visible_rendered(self) -> None:
        for index in self._visible_range():
            self._render_page(index)

    def _invalidate_rasters(self) -> None:
        """Пометить растры устаревшими, если разрешение не совпадает с масштабом."""
        target = self._raster_scale()
        for index in range(len(self.canvases)):
            if self._loaded[index] and abs(self._render_scales[index] - target) > 0.05:
                self._loaded[index] = False

    def _raster_scale(self) -> float:
        return max(1.0, min(self._zoom, self.MAX_RASTER_SCALE))

    # ------------------------------------------------------------- прокрутка
    def _on_scrolled(self, _value: int = 0) -> None:
        self._ensure_visible_rendered()
        self._update_current_page()

    def _update_current_page(self) -> None:
        if not self.canvases:
            return
        bar = self._area.verticalScrollBar()
        top = float(bar.value())
        bottom = top + float(max(self._area.viewport().height(), 1))
        best_index = self._current
        best_area = -math.inf
        best_distance = None
        for index, canvas in enumerate(self.canvases):
            canvas_top = float(canvas.y())
            canvas_bottom = canvas_top + float(canvas.height())
            overlap = min(bottom, canvas_bottom) - max(top, canvas_top)
            center = (canvas_top + canvas_bottom) / 2.0
            distance = abs(center - (top + bottom) / 2.0)
            if overlap > best_area or (overlap == best_area
                                       and (best_distance is None or distance < best_distance)):
                best_area = overlap
                best_distance = distance
                best_index = index
        self._set_current(best_index, emit=True)

    def _set_current(self, index: int, emit: bool = True) -> None:
        if not (0 <= index < len(self.canvases)):
            return
        if index == self._current:
            return
        self._current = index
        if emit:
            self.page_changed.emit(index)

    def _scroll_anchor(self) -> tuple[int, float]:
        canvas = self.canvas_at(self._current)
        if canvas is None or canvas.height() <= 0:
            return (self._current, 0.0)
        value = float(self._area.verticalScrollBar().value())
        fraction = (value - float(canvas.y())) / float(canvas.height())
        return (self._current, min(max(fraction, 0.0), 1.0))

    def _restore_scroll_anchor(self, anchor: tuple[int, float]) -> None:
        index, fraction = anchor
        canvas = self.canvas_at(index)
        if canvas is None:
            return
        target = float(canvas.y()) + fraction * float(canvas.height())
        self._area.verticalScrollBar().setValue(int(round(target)))

    def _scroll_to_canvas(self, canvas: EditorCanvas) -> None:
        """Поставить страницу к верхнему краю области прокрутки."""
        self._layout.activate()
        bar = self._area.verticalScrollBar()
        bar.setValue(max(0, int(canvas.y()) - self.PAGE_MARGIN))

    def _scroll_widget_into_view(self, canvas: EditorCanvas, widget: Optional[QWidget]) -> None:
        self._layout.activate()
        bar = self._area.verticalScrollBar()
        if widget is None:
            self._scroll_to_canvas(canvas)
            return
        rect = widget.geometry()
        center_y = canvas.y() + rect.y() + rect.height() // 2
        target = center_y - self._area.viewport().height() // 2
        bar.setValue(max(0, min(bar.maximum(), int(target))))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        self._layout.activate()
        self._ensure_visible_rendered()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        if self.canvases:
            self._ensure_visible_rendered()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
        if watched is self._container and event.type() == QEvent.Type.Resize:
            # Лента получила настоящую геометрию — догружаем видимые страницы.
            self._layout.activate()
            self._ensure_visible_rendered()
        elif event.type() == QEvent.Type.FocusIn and not self._syncing:
            for index, canvas in enumerate(self.canvases):
                if canvas is watched:
                    self._set_current(index, emit=True)
                    break
        return super().eventFilter(watched, event)

    # ====================================================== обратные вызовы
    def _report(self, message: str, exc: Optional[BaseException] = None) -> None:
        text = message if exc is None else "{0}: {1}".format(message, exc)
        self._last_error = text
        self.error.emit(text)

    def _sync_page_annotations(self, page: int, selected: Optional[int] = None) -> None:
        """Перечитать аннотации страницы из сессии, не трогая формы и фокус."""
        canvas = self.canvas_at(page)
        if canvas is None:
            return
        if selected is None:
            keep = self._selected[1] if self._selected[0] == page else -1
        else:
            keep = int(selected)
        self._syncing = True
        try:
            canvas.set_annotations(self._visual_annotations(page), selected=keep)
        finally:
            self._syncing = False

    def set_native_image_mode(self, enabled: bool) -> None:
        """Insert image content in Edit mode; other modes retain annotations."""
        self._native_image_mode = bool(enabled)

    def _on_annotation_created(self, page: int, annotation: Any) -> None:
        if self._syncing or self._session is None:
            return
        if self._native_image_mode and getattr(annotation, 'kind', None) in ('image', 'text'):
            try:
                from .source_session import SourceContentSession
                from .pdf_backend import _display_rect_to_user
                rect = _display_rect_to_user(self._session._geometry(page),
                                            annotation.x, annotation.y,
                                            annotation.width, annotation.height)
                if annotation.kind == 'image':
                    SourceContentSession.add_image(self._session, page, annotation.image_data, rect)
                else:
                    SourceContentSession.add_text(self._session, page, annotation.text, rect,
                                                  font_name=annotation.font_family,
                                                  font_size=annotation.font_size,
                                                  color=annotation.color,
                                                  bold=annotation.bold,
                                                  alignment=annotation.alignment,
                                                  orientation='display')
            except Exception as exc:  # noqa: BLE001 - failed insertion leaves history unchanged
                self._report('Не удалось добавить содержимое в PDF', exc)
                self._reload_page(page)
                return
            self._selected = (-1, -1)
            self._reload_page(page)
            self.document_changed.emit()
            self.selection_changed.emit(page, -1)
            return
        try:
            index = self._session.add_annotation(page, annotation)
        except Exception as exc:  # noqa: BLE001 - backend может не знать новый тип
            self._report("Не удалось добавить объект на страницу %d" % (page + 1), exc)
            self._reload_page(page)
            return
        if not isinstance(index, int):
            index = max(len(self._session_annotations(page)) - 1, 0)
        self._sync_page_annotations(page, selected=index)
        self._selected = (page, index)
        self.document_changed.emit()
        self.selection_changed.emit(page, index)

    def _on_annotation_edited(self, page: int, position: int, annotation: Any) -> None:
        if self._syncing or self._session is None:
            return
        try:
            self._session.update_annotation(page, int(position), annotation)
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось изменить объект на странице %d" % (page + 1), exc)
            self._reload_page(page)
            return
        self._sync_page_annotations(page, selected=int(position))
        self._selected = (page, int(position))
        self.document_changed.emit()

    def _on_annotation_deleted(self, page: int, position: int) -> None:
        if self._syncing or self._session is None:
            return
        try:
            self._session.remove_annotation(page, int(position))
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось удалить объект со страницы %d" % (page + 1), exc)
            self._reload_page(page)
            return
        self._sync_page_annotations(page)
        self._selected = (-1, -1)
        self.document_changed.emit()
        self.selection_changed.emit(page, -1)

    def _on_selection_changed(self, page: int, position: int) -> None:
        if self._syncing:
            return
        if position >= 0:
            self._set_current(page, emit=True)
            for index, canvas in enumerate(self.canvases):
                if index != page and canvas.selected_index() >= 0:
                    self._syncing = True
                    try:
                        canvas.select_annotation(-1)
                    finally:
                        self._syncing = False
        self._selected = (int(page), int(position))
        self.selection_changed.emit(int(page), int(position))

    def _on_form_value_changed(self, page: int, key: str, value: Any) -> None:
        if self._syncing or self._session is None:
            return
        try:
            self._session.set_form_value(str(key), value)
        except Exception as exc:  # noqa: BLE001 - readonly/неподдерживаемое поле
            self._report("Не удалось заполнить поле «%s»" % key, exc)
            self._refresh_form_widgets(page, force_values=True)
            return
        self._refresh_form_widgets(page)
        self._stale_pages.add(page)
        self._raster_timer.start(self.RASTER_REFRESH_DELAY_MS)
        self.document_changed.emit()

    def _on_canvas_zoom(self, page: int, zoom: float) -> None:  # noqa: ARG002
        """Ctrl+колесо на любой странице задаёт общий масштаб ленты."""
        self.set_zoom(zoom)

    def _reload_page(self, page: int) -> None:
        """Полный перечит страницы из сессии (после отказа модели)."""
        if 0 <= page < len(self._loaded):
            self._loaded[page] = False
        self._render_page(page, force=True)

    def _refresh_form_widgets(self, page: int, force_values: bool = False) -> None:
        canvas = self.canvas_at(page)
        if canvas is None:
            return
        self._syncing = True
        try:
            canvas.set_form_widgets(self._form_widgets(page, refresh=True),
                                    force_values=force_values)
        finally:
            self._syncing = False

    # =========================================================== доступ к сессии
    def _session_page_count(self) -> int:
        if self._session is None:
            return 0
        try:
            raw = getattr(self._session, "page_count", 0)
            if callable(raw):
                raw = raw()
            return max(int(raw or 0), 0)
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось получить число страниц", exc)
            return 0

    def _session_page_size(self, index: int) -> tuple[float, float]:
        if self._session is None:
            return (612.0, 792.0)
        try:
            width, height = self._session.page_size(index)
            width = float(width)
            height = float(height)
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось получить размер страницы %d" % (index + 1), exc)
            return (612.0, 792.0)
        if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
            return (612.0, 792.0)
        return (width, height)

    def _session_render(self, index: int) -> Any:
        if self._session is None:
            return None
        try:
            include_annotations = bool(self.view_rotation and self._readonly)
            try:
                image = self._session.render(
                    index, scale=self._raster_scale(),
                    include_forms=(self._tool != TOOL_FORM),
                    include_annotations=include_annotations)
            except TypeError as exc:
                # Preserve compatibility with lightweight session adapters.
                if "include_annotations" not in str(exc) and "include_forms" not in str(exc):
                    raise
                try:
                    image = self._session.render(
                        index, scale=self._raster_scale(),
                        include_forms=(self._tool != TOOL_FORM))
                except TypeError:
                    image = self._session.render(index, scale=self._raster_scale())
            if self.view_rotation:
                image = rotate_image(image, self.view_rotation) if image is not None else None
            return image
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось отрисовать страницу %d" % (index + 1), exc)
            return None

    def _session_annotations(self, index: int) -> list[Any]:
        if self._session is None:
            return []
        try:
            return list(self._session.get_annotations(index) or [])
        except Exception as exc:  # noqa: BLE001
            self._report("Не удалось прочитать объекты страницы %d" % (index + 1), exc)
            return []

    def _visual_annotations(self, index: int) -> list[Any]:
        annotations = self._session_annotations(index)
        if not self.view_rotation or not (0 <= index < len(self._source_page_sizes)):
            return annotations
        width, height = self._source_page_sizes[index]
        return [rotate_annotation_geometry(item, width, height, self.view_rotation)
                for item in annotations]

    def _visual_xyxy(self, index: int, rect: Any) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = (float(value) for value in rect)
        if not self.view_rotation:
            return x1, y1, x2, y2
        width, height = self._source_page_sizes[index]
        x, y, w, h = rotate_rect((x1, y1, x2 - x1, y2 - y1),
                                 width, height, self.view_rotation)
        return x, y, x + w, y + h

    def _visual_search_rects(self, index: int, rects: Any) -> list[tuple[float, float, float, float]]:
        return [self._visual_xyxy(index, rect) for rect in (rects or ())]

    def _form_widgets(self, index: int, refresh: bool = False) -> list[dict]:
        if index in self._form_cache and not refresh:
            return self._form_cache[index]
        widgets: list[dict] = []
        if self._session is not None:
            try:
                for kwargs in (
                    {"include_appearance": True, "include_authoring_metadata": True},
                    {"include_appearance": True},
                    {},
                ):
                    try:
                        widgets = list(self._session.form_widgets(index, **kwargs) or [])
                        break
                    except TypeError as exc:
                        if not any(key in str(exc) for key in kwargs):
                            raise
            except Exception as exc:  # noqa: BLE001
                self._report("Не удалось прочитать поля формы страницы %d" % (index + 1), exc)
                widgets = []
        self._form_cache[index] = widgets
        return widgets

    def _page_has_form_key(self, index: int, key: str) -> bool:
        for widget in self._form_widgets(index):
            if str(widget.get("key", "")) == key:
                return True
        return False
