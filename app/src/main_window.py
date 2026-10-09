"""PdfDesk — главное окно программы (просмотр и правка PDF).

Модуль отвечает только за интерфейс:

* работу с документом выполняет ``PdfSession`` (``src/pdf_backend.py``);
* отрисовку страницы и заметок выполняет ``PageCanvas`` (``src/canvas.py``).

Соседние модули подключаются обычными (статическими) импортами, чтобы сборщик
EXE видел их и включал в пакет. Ошибка импорта не скрывается: без холста или
модуля работы с PDF приложение не сможет работать, поэтому сбой виден сразу.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QTimer, QStringListModel
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:  # обычный запуск приложения: пакет src
    from . import canvas as canvas_module
    from . import pdf_backend as pdf_backend_module
    from . import recovery as recovery_module
    from . import theme as theme_module
    from .canvas import PageCanvas
    from .pdf_backend import PdfSession
except ImportError:  # запуск, когда sys.path указывает на папку src
    import canvas as canvas_module  # type: ignore[no-redef]
    import pdf_backend as pdf_backend_module
    import recovery as recovery_module  # type: ignore[no-redef]
    import theme as theme_module  # type: ignore[no-redef]
    from canvas import PageCanvas  # type: ignore[no-redef]
    from pdf_backend import PdfSession  # type: ignore[no-redef]

__all__ = ["MainWindow"]

APP_NAME = "PdfDesk"
PDF_FILTER = "PDF-файлы (*.pdf);;Все файлы (*)"

#: Пределы масштаба, чтобы случайно не запросить гигантскую картинку.
MIN_ZOOM = 0.10
MAX_ZOOM = 6.00
ZOOM_STEP = 1.25
#: Ограничения на размер отрисованной страницы (защита памяти).
MAX_RENDER_SIDE = 9000
MAX_RENDER_PIXELS = 36_000_000
#: Отступ вокруг страницы на холсте (пиксели).
PAGE_MARGIN = 28
#: Цвет фона вокруг страницы. Пустое состояние холст (src/canvas.py) закрашивает
#: сам своим CANVAS_BACKGROUND, поэтому окно использует светлый тон макета.
DEFAULT_CANVAS_BG = theme_module.STAGE

DEFAULT_COLOR = theme_module.ACCENT
DEFAULT_FONT_FAMILY = "Arial"
DEFAULT_FONT_SIZE = 14.0
DEFAULT_STROKE_WIDTH = 2.0

SAVE_HINT = (
    "Заметки сохраняются как обычные пометки PDF: их видно в других программах, "
    "можно выделять, перемещать и удалять. Поля формы остаются интерактивными."
)

#: Период автоматических temporary-снимков для восстановления после сбоя.
RECOVERY_INTERVAL_MS = recovery_module.DEFAULT_INTERVAL_MS


def _running_under_test_framework() -> bool:
    """True при прогоне тестов.

    Авто-предложение восстановления при старте показывает модальное окно;
    в тестах его не показываем, чтобы прогон не зависал (тесты вызывают
    :meth:`MainWindow.offer_recovery` напрямую).
    """
    return "pytest" in sys.modules or "unittest" in sys.modules

# --------------------------------------------------------------------------
# Режимы работы (modes) и пометки о неготовых возможностях
# --------------------------------------------------------------------------
MODE_VIEW = "view"
MODE_COMMENT = "comment"
MODE_FILL = "fill"
MODE_PAGES = "pages"

#: ключ, подпись, подсказка
MODE_SPECS: tuple[tuple[str, str, str], ...] = (
    (MODE_VIEW, "Просмотр", "Выделять заметки и менять их стиль"),
    (MODE_COMMENT, "Заметки", "Текст, выделение и рисунок поверх страницы"),
    (MODE_FILL, "Формы", "Заполнять поля формы документа"),
    (MODE_PAGES, "Страницы", "Поворачивать, перемещать и удалять страницы"),
)
MODE_TITLES: dict[str, str] = {key: title for key, title, _tip in MODE_SPECS}

MODE_STATUS: dict[str, str] = {
    MODE_VIEW: "Режим просмотра: выделяйте заметки и меняйте их стиль.",
    MODE_COMMENT: "Режим заметок: выберите инструмент и щёлкните по странице.",
    MODE_FILL: "Режим форм: заполняйте поля формы документа.",
    MODE_PAGES: "Режим страниц: поворот, порядок и удаление страниц.",
}

#: Возможности, которых в программе пока нет. Они показаны в интерфейсе
#: отключёнными и с явной пометкой, чтобы макет не выдавал их за готовые.
UNAVAILABLE_ACTIONS: tuple[tuple[str, str], ...] = (
    (
        "Правка оригинала",
        "Изменение исходного текста PDF пока недоступно: программа добавляет "
        "заметки и заполняет формы, не переписывая страницу.",
    ),
    (
        "Несколько документов",
        "Открыт один документ: вкладки с несколькими файлами появятся позже.",
    ),
    (
        "Печать",
        "Печать пока недоступна: сохраните копию и распечатайте её другой программой.",
    ),
)

#: Миниатюры страниц в левой колонке (размер, масштаб отрисовки и предел).
THUMBNAIL_WIDTH = 96
THUMBNAIL_HEIGHT = 128
THUMBNAIL_SCALE = 0.18
THUMBNAIL_MAX_PAGES = 60

#: Инструменты, которые холст понимает в любом случае.
BASE_CANVAS_TOOLS: tuple[str, ...] = ("select", "text", "highlight", "ink")

#: Поля описания формы — читаются у объектов, если сессия вернула не словари.
FORM_WIDGET_KEYS: tuple[str, ...] = (
    "key", "rect", "kind", "value", "options",
    "readonly", "multiline", "export_value",
)


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------
@contextmanager
def _busy_cursor() -> Iterator[None]:
    """Показывает курсор ожидания на время долгой операции."""
    app = QApplication.instance()
    if app is not None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        if app is not None:
            QApplication.restoreOverrideCursor()


def _error_text(exc: BaseException) -> str:
    """Текст исключения без пустых строк."""
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def _is_pdf_url(url: Any) -> bool:
    try:
        if not url.isLocalFile():
            return False
        return str(url.toLocalFile()).lower().endswith(".pdf")
    except Exception:  # noqa: BLE001
        return False


def _same_path(first: str, second: str) -> bool:
    """Сравнивает пути без учёта регистра и лишних ссылок."""
    try:
        left = os.path.normcase(os.path.realpath(os.path.abspath(first)))
        right = os.path.normcase(os.path.realpath(os.path.abspath(second)))
    except Exception:  # noqa: BLE001
        return os.path.normcase(first) == os.path.normcase(second)
    return left == right


def _looks_like_password_error(exc: BaseException) -> bool:
    text = f"{exc} {exc.__class__.__name__}".lower()
    markers = ("парол", "password", "encrypt", "decrypt", "шифр")
    return any(marker in text for marker in markers)


def _color_icon(color: QColor, size: int = 15) -> QIcon:
    """Небольшой квадратик выбранного цвета для кнопки."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor("#64748B"))
        painter.setBrush(color)
        painter.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
    finally:
        painter.end()
    return QIcon(pixmap)


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _points_are_nested(points: Any) -> bool:
    """True, если точки заданы списком штрихов, а не плоским списком."""
    try:
        for item in points or []:
            if item:
                return isinstance(item[0], (list, tuple))
    except TypeError:
        return False
    return False


def _ink_points_flat(points: Any) -> list[tuple[float, float]]:
    """Плоский список точек (формат из contracts.Annotation)."""
    flat: list[tuple[float, float]] = []
    for item in points or []:
        try:
            if item and isinstance(item[0], (list, tuple)):
                for pair in item:
                    flat.append((float(pair[0]), float(pair[1])))
            else:
                flat.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return flat


def _ink_points_as_strokes(points: Any) -> list[list[tuple[float, float]]]:
    """Список штрихов — запасной формат, который понимают некоторые сборки.

    Плоский список точек — это один непрерывный штрих (так рисует холст),
    поэтому он переносится в один вложенный список целиком.
    """
    if not points:
        return []
    if not _points_are_nested(points):
        stroke = _ink_points_flat(points)
        return [stroke] if stroke else []
    strokes: list[list[tuple[float, float]]] = []
    for item in points:
        stroke = _ink_points_flat([item])
        if stroke:
            strokes.append(stroke)
    return strokes


def _annotation_for_canvas(annotation: Any) -> Any:
    """Готовит заметку к отрисовке: холст работает с плоскими точками."""
    try:
        if getattr(annotation, "kind", "") != "ink":
            return annotation
        points = getattr(annotation, "points", None)
        if not points or not _points_are_nested(points):
            return annotation
        flat = _ink_points_flat(points)
        if not flat:
            return annotation
        return dataclasses.replace(annotation, points=flat)
    except Exception:  # noqa: BLE001 - чужой объект лучше показать как есть
        return annotation


def _annotation_as_strokes(annotation: Any) -> Optional[Any]:
    """Копия заметки с точками-штрихами (запасной формат для сессии)."""
    try:
        if getattr(annotation, "kind", "") != "ink":
            return None
        strokes = _ink_points_as_strokes(getattr(annotation, "points", None))
        if not strokes:
            return None
        return dataclasses.replace(annotation, points=strokes)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Диалог полей формы
# --------------------------------------------------------------------------
class _FormsPanel(QWidget):
    """Правая панель полей формы — запасной способ заполнения.

    Стоит в правом доке и нужна, пока холст не умеет рисовать поля прямо
    поверх страницы (``PageCanvas.set_form_widgets``). Публичное поведение
    прежнее: ``reload()``, ``_rows``, ``_count_label``, ``_on_apply()``.
    """

    def __init__(self, owner: "MainWindow") -> None:
        super().__init__(owner)
        self.setObjectName("formsPanel")
        self._owner = owner
        self._rows: list[tuple[Any, QWidget]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(9)

        self._header = QLabel("Поля формы")
        self._header.setObjectName("formsTitle")
        root.addWidget(self._header)

        self._place_label = QLabel("")
        self._place_label.setObjectName("hintLabel")
        self._place_label.setWordWrap(True)
        root.addWidget(self._place_label)

        self._count_label = QLabel("Полей на странице: 0")
        self._count_label.setObjectName("badgeLabel")
        self._count_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        root.addWidget(self._count_label, alignment=Qt.AlignmentFlag.AlignLeft)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        holder.setObjectName("formsHolder")
        self._form_layout = QFormLayout(holder)
        self._form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._form_layout.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self._form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._form_layout.setContentsMargins(2, 2, 2, 2)
        self._form_layout.setSpacing(8)
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll, 1)

        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        self._info_label.setObjectName("hintLabel")
        root.addWidget(self._info_label)

        hint = QLabel(SAVE_HINT)
        hint.setWordWrap(True)
        hint.setObjectName("hintLabel")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._apply_button = QPushButton("Применить")
        self._apply_button.setDefault(True)
        self._apply_button.setToolTip("Записать значения в документ и обновить страницу")
        self._apply_button.clicked.connect(self._on_apply)
        self._reload_button = QPushButton("Обновить")
        self._reload_button.setToolTip("Заново прочитать поля из документа")
        self._reload_button.clicked.connect(self.reload)
        self._close_button = QPushButton("Закрыть")
        self._close_button.clicked.connect(self.close)
        buttons.addWidget(self._apply_button)
        buttons.addWidget(self._reload_button)
        buttons.addStretch(1)
        buttons.addWidget(self._close_button)
        root.addLayout(buttons)

        self.reload()

    # -- наполнение --------------------------------------------------------
    def reload(self) -> None:
        """Перечитывает поля текущей страницы и перестраивает редакторы."""
        fields = self._owner.form_fields(self._owner.current_page_index)
        page_number = self._owner.current_page_index + 1

        self._clear_rows()
        self._header.setText(f"Поля формы — страница {page_number}")

        editable = 0
        if not fields:
            empty = QLabel("На этой странице нет полей формы.")
            empty.setObjectName("hintLabel")
            empty.setWordWrap(True)
            self._form_layout.addRow(empty)
        for field in fields:
            readonly = bool(getattr(field, "readonly", False))
            kind = str(getattr(field, "kind", "") or "unsupported")
            label_text = str(getattr(field, "label", "") or getattr(field, "key", "") or "Поле")
            if readonly:
                label_text += "  (только чтение)"
            editor = self._build_editor(field, kind, readonly)
            if editor is None:
                continue
            if not readonly and kind != "unsupported":
                editable += 1
            key = str(getattr(field, "key", "") or "")
            label = QLabel(label_text)
            label.setWordWrap(True)
            label.setToolTip(key)
            self._form_layout.addRow(label, self._editor_row(key, editor, readonly))
            self._rows.append((field, editor))

        self._count_label.setText(
            f"Полей на странице: {len(fields)} · доступно для правки: {editable}"
        )
        self._apply_button.setEnabled(editable > 0)
        self._info_label.setText("")
        if self._owner.canvas_draws_forms():
            self._place_label.setText(
                "Поля можно заполнять прямо на странице; панель остаётся запасным способом."
            )
        else:
            self._place_label.setText(
                "Наложение полей на страницу ещё не подключено — заполняйте их в этой панели."
            )

    def _editor_row(self, key: str, editor: QWidget, readonly: bool) -> QWidget:
        """Редактор поля вместе с кнопкой «показать это поле на странице»."""
        if not key or readonly:
            return editor
        holder = QWidget(self)
        holder.setObjectName("formsRow")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(editor, 1)
        focus = QToolButton(holder)
        focus.setText("→")
        focus.setAutoRaise(True)
        focus.setToolTip("Показать это поле на странице")
        focus.clicked.connect(
            lambda _checked=False, name=key: self._owner._focus_form_field(name)
        )
        row.addWidget(focus)
        return holder

    def _clear_rows(self) -> None:
        self._rows.clear()
        while self._form_layout.count():
            item = self._form_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _build_editor(self, field: Any, kind: str, readonly: bool) -> Optional[QWidget]:
        value = getattr(field, "value", "")
        if kind == "checkbox" or (kind == "radio" and isinstance(value, bool)):
            editor = QCheckBox()
            editor.setChecked(bool(value))
            editor.setEnabled(not readonly)
            editor.setToolTip("Отметьте, чтобы включить")
            return editor
        if kind in ("choice", "radio"):
            editor = QComboBox()
            options = [str(option) for option in (getattr(field, "options", None) or [])]
            current = str(value or "")
            if current and current not in options:
                options.insert(0, current)
            editor.addItems(options)
            if current:
                position = editor.findText(current)
                if position >= 0:
                    editor.setCurrentIndex(position)
            editor.setEnabled(not readonly)
            return editor
        if kind == "text":
            multiline = bool(getattr(field, "multiline", False))
            if multiline:
                editor: QWidget = QPlainTextEdit()
                editor.setPlainText(str(value or ""))
                editor.setFixedHeight(84)
            else:
                editor = QLineEdit()
                editor.setText(str(value or ""))
            editor.setReadOnly(readonly)
            editor.setToolTip("Только чтение" if readonly else "Введите значение")
            return editor
        # Неизвестный тип поля — показываем значение без правки.
        label = QLabel(str(value or "—"))
        label.setObjectName("hintLabel")
        label.setWordWrap(True)
        label.setToolTip("Такое поле нельзя изменить в этой версии программы")
        return label

    # -- применение --------------------------------------------------------
    def _read_value(self, field: Any, editor: QWidget) -> Any:
        kind = str(getattr(field, "kind", "") or "")
        if kind == "checkbox" or (kind == "radio" and isinstance(editor, QCheckBox)):
            return bool(editor.isChecked())
        if kind in ("choice", "radio"):
            return str(editor.currentText())
        if kind == "text":
            if isinstance(editor, QPlainTextEdit):
                return editor.toPlainText()
            if isinstance(editor, QLineEdit):
                return editor.text()
        return ""

    def _on_apply(self) -> None:
        items: list[tuple[str, Any]] = []
        for field, editor in self._rows:
            if bool(getattr(field, "readonly", False)):
                continue
            if str(getattr(field, "kind", "") or "") == "unsupported":
                continue
            key = str(getattr(field, "key", "") or "")
            if not key:
                continue
            try:
                items.append((key, self._read_value(field, editor)))
            except Exception:  # noqa: BLE001 - виджет мог быть удалён
                continue
        if not items:
            self._info_label.setText("Нет полей, доступных для правки.")
            return

        applied, failed, error = self._owner.apply_form_values(items)
        if failed:
            reason = _error_text(error) if error is not None else "неизвестная причина"
            self._info_label.setText(
                f"Применено: {applied}, не удалось: {failed}. {reason}"
            )
        else:
            self._info_label.setText(f"Применено полей: {applied}. Страница обновлена.")
        self.reload()


#: Прежнее имя панели: код и тесты этапа 0 обращались к ``_FormsDialog``.
_FormsDialog = _FormsPanel


# --------------------------------------------------------------------------
# Главное окно
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Главное окно PdfDesk: холст, список страниц, панель инструментов."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        recovery_root: Optional[Any] = None,
        recovery_interval_ms: Optional[int] = None,
        recovery_prompt: bool = True,
    ) -> None:
        super().__init__(parent)

        self._session: Any = None
        self._canvas: Optional[PageCanvas] = None
        self._scroll_area: Optional[QScrollArea] = None
        self._canvas_background = DEFAULT_CANVAS_BG
        self._min_zoom = MIN_ZOOM
        self._max_zoom = MAX_ZOOM

        self._page_index = 0
        self._zoom = 1.0
        self._fit_width = False
        self._syncing = False
        self._rendering = False
        self._render_scale_used = 1.0
        self._selected_annotation = -1
        self._source_path: Optional[str] = None
        self._field_counts: dict[int, int] = {}
        self._reported_warnings: set[str] = set()
        self._forms_dialog: Optional[_FormsPanel] = None
        self._forms_dock: Optional[QDockWidget] = None
        self._last_dir = str(Path.home())

        # Авто-восстановление: снимки живут в отдельном служебном каталоге,
        # исходные PDF не перезаписываются.
        self._recovery_enabled = True
        self._recovery_root: Optional[Path] = Path(recovery_root) if recovery_root else None
        self._recovery_interval_ms = int(
            RECOVERY_INTERVAL_MS if recovery_interval_ms is None else recovery_interval_ms
        )
        self._recovery_timer: Optional[QTimer] = None
        self._recovery_key: Optional[str] = None
        self._recovery_copy_path: Optional[str] = None
        self._recovery_source_paths: list[str] = []
        self._recovery_restored = False
        self._recovery_snapshot_revision: Optional[int] = None
        self._recovery_last_path: Optional[str] = None
        self._recovery_prompt_enabled = bool(recovery_prompt)
        self._recovery_prompt_pending = True

        self._tool = "select"
        self._mode = MODE_VIEW
        self._full_mode = False
        self._last_comment_tool = "text"
        self._canvas_forms_supported = False
        self._canvas_form_widgets: list[dict[str, Any]] = []
        self._group_items: dict[str, list[tuple[Any, bool]]] = {}
        self._unavailable_actions: list[QAction] = []
        self._style: dict[str, Any] = {
            "font_family": DEFAULT_FONT_FAMILY,
            "font_size": DEFAULT_FONT_SIZE,
            "color": DEFAULT_COLOR,
            "stroke_width": DEFAULT_STROKE_WIDTH,
        }

        self.setWindowTitle(APP_NAME)
        self.resize(1280, 880)
        self.setMinimumSize(940, 620)
        self.setAcceptDrops(True)

        self._create_components()
        self.setStyleSheet(theme_module.stylesheet(self._canvas_background))
        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._wire_canvas()
        self._push_style()
        self._select_mode(MODE_VIEW, tool="select")
        self._update_style_enabled()
        self._refresh_all()

        # Таймер temporary-снимков: по умолчанию каждые 3 минуты.
        self._recovery_timer = QTimer(self)
        self._recovery_timer.timeout.connect(self._on_recovery_timer)
        self.set_recovery_interval_ms(self._recovery_interval_ms)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        super().showEvent(event)
        if not self._recovery_prompt_pending:
            return
        self._recovery_prompt_pending = False
        if not self._recovery_prompt_enabled or _running_under_test_framework():
            return
        # Предлагаем восстановление после сбоя, когда окно уже показано.
        QTimer.singleShot(0, self._offer_recovery_on_startup)

    # ------------------------------------------------------------------
    # Создание составных частей
    # ------------------------------------------------------------------
    def _create_components(self) -> None:
        # Обычные прямые импорты: если модуль недоступен, ошибка видна сразу,
        # а сборщик EXE находит зависимости статически.
        self._session = PdfSession()
        self._canvas = PageCanvas()

        self._canvas_background = DEFAULT_CANVAS_BG
        canvas_min = _as_float(getattr(canvas_module, "MIN_ZOOM", MIN_ZOOM), MIN_ZOOM)
        canvas_max = _as_float(getattr(canvas_module, "MAX_ZOOM", MAX_ZOOM), MAX_ZOOM)
        self._min_zoom = max(MIN_ZOOM, min(canvas_min, MAX_ZOOM))
        self._max_zoom = min(MAX_ZOOM, max(canvas_max, self._min_zoom))
        self._zoom = max(self._min_zoom, min(self._max_zoom, self._zoom))

    # ------------------------------------------------------------------
    # Интерфейс
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._build_central()
        self._build_document_bar()
        # Каждая строка макета занимает свою полосу: документ, режимы,
        # контекстные инструменты. Иначе Qt сжимает полосы в одну кнопку.
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self._build_mode_bar()
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self._build_context_bar()
        self._build_page_dock()
        self._build_forms_dock()
        self._build_status_bar()
        self._update_context_visibility()

    def _build_central(self) -> None:
        # Холст рассчитан на размещение внутри QScrollArea с
        # setWidgetResizable(False): так работают прокрутка и зум колесом.
        area = QScrollArea(self)
        area.setObjectName("canvasScroll")
        area.setWidgetResizable(False)
        area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setWidget(self._canvas)
        self._scroll_area = area
        self.setCentralWidget(area)

    # ------------------------------------------------------------------
    # Строка документа (верхняя)
    # ------------------------------------------------------------------
    def _build_document_bar(self) -> None:
        """Имя открытого документа и файловые действия одной строкой."""
        bar = QToolBar("Документ", self)
        bar.setObjectName("documentBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(16, 16))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self._document_bar = bar

        style = self.style()

        self.act_open = QAction(style.standardIcon(style.StandardPixmap.SP_DialogOpenButton), "Открыть", self)
        self.act_open.setToolTip("Открыть PDF-файл (Ctrl+O)")
        self.act_open.triggered.connect(self._on_open)

        self.act_append = QAction(style.standardIcon(style.StandardPixmap.SP_FileIcon), "Добавить", self)
        self.act_append.setToolTip("Добавить страницы из других PDF-файлов")
        self.act_append.triggered.connect(self._on_append)

        self.act_save = QAction(style.standardIcon(style.StandardPixmap.SP_DialogSaveButton), "Сохранить копию", self)
        self.act_save.setToolTip(
            "Сохранить результат в новый файл (Ctrl+Shift+S). Заметки сохраняются "
            "как пометки PDF"
        )
        self.act_save.triggered.connect(lambda: self._on_save_copy())

        for action in (self.act_open, self.act_append, self.act_save):
            action.setPriority(QAction.Priority.HighPriority)

        badge = QLabel("PDF")
        badge.setObjectName("brandBadge")
        brand = QLabel(APP_NAME)
        brand.setObjectName("brandName")
        bar.addWidget(badge)
        bar.addWidget(brand)
        bar.addSeparator()

        self._doc_title_label = QLabel("документ не открыт")
        self._doc_title_label.setObjectName("documentTitle")
        self._doc_title_label.setToolTip("Имя открытого файла")
        bar.addWidget(self._doc_title_label)

        self._doc_meta_label = QLabel("")
        self._doc_meta_label.setObjectName("documentMeta")
        self._doc_meta_action = bar.addWidget(self._doc_meta_label)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        for action in (self.act_open, self.act_append, self.act_save):
            bar.addAction(action)
        bar.addSeparator()

        # Неготовые возможности видно сразу: кнопки отключены и подписаны.
        for title, tip in UNAVAILABLE_ACTIONS:
            action = QAction(title, self)
            action.setEnabled(False)
            action.setToolTip(f"{tip} Пока недоступно.")
            bar.addAction(action)
            button = bar.widgetForAction(action)
            if button is not None:
                button.setToolTip(action.toolTip())
            self._unavailable_actions.append(action)

    # ------------------------------------------------------------------
    # Строка режимов и основных кнопок
    # ------------------------------------------------------------------
    def _build_mode_bar(self) -> None:
        """Режимы (modes) и основные кнопки — одной строкой под документом."""
        bar = QToolBar("Режимы", self)
        bar.setObjectName("modeBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(16, 16))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self._mode_bar = bar
        self._main_toolbar = bar  # прежнее имя полосы основных действий

        style = self.style()

        self.act_undo = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowBack), "Отменить", self)
        self.act_undo.setToolTip("Отменить последнее действие (Ctrl+Z)")
        self.act_undo.triggered.connect(self._on_undo)

        self.act_redo = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowForward), "Вернуть", self)
        self.act_redo.setToolTip("Вернуть отменённое действие (Ctrl+Y)")
        self.act_redo.triggered.connect(self._on_redo)

        self.act_zoom_out = QAction("−", self)
        self.act_zoom_out.setToolTip("Уменьшить масштаб (Ctrl+-)")
        self.act_zoom_out.triggered.connect(self._on_zoom_out)

        self.act_zoom_in = QAction("+", self)
        self.act_zoom_in.setToolTip("Увеличить масштаб (Ctrl++)")
        self.act_zoom_in.triggered.connect(self._on_zoom_in)

        self.act_fit_width = QAction("По ширине", self)
        self.act_fit_width.setToolTip("Вписать страницу по ширине окна")
        self.act_fit_width.triggered.connect(self._on_fit_width)

        self.act_zoom_reset = QAction("100 %", self)
        self.act_zoom_reset.setToolTip("Показать страницу в натуральную величину (Ctrl+0)")
        self.act_zoom_reset.triggered.connect(self._on_zoom_reset)

        for action in (self.act_undo, self.act_redo):
            action.setPriority(QAction.Priority.HighPriority)
        for action in (self.act_zoom_out, self.act_zoom_in, self.act_fit_width, self.act_zoom_reset):
            action.setPriority(QAction.Priority.LowPriority)

        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        self._mode_actions: dict[str, QAction] = {}
        for key, title, tip in MODE_SPECS:
            action = QAction(title, self)
            action.setCheckable(True)
            action.setToolTip(f"{tip}. Режим «{title}».")
            action.setPriority(QAction.Priority.HighPriority)
            action.triggered.connect(
                lambda _checked=False, name=key: self._on_mode_selected(name)
            )
            mode_group.addAction(action)
            self._mode_actions[key] = action
            bar.addAction(action)
            button = bar.widgetForAction(action)
            if button is not None:
                button.setObjectName("modeButton")
        self._mode_actions[MODE_VIEW].setChecked(True)

        bar.addSeparator()
        bar.addAction(self.act_undo)
        bar.addAction(self.act_redo)
        bar.addSeparator()
        bar.addAction(self.act_zoom_out)

        self._zoom_value_label = QLabel("100%")
        self._zoom_value_label.setObjectName("zoomValue")
        self._zoom_value_label.setMinimumWidth(44)
        self._zoom_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_value_label.setToolTip("Текущий масштаб страницы")
        bar.addWidget(self._zoom_value_label)

        bar.addAction(self.act_zoom_in)
        bar.addAction(self.act_fit_width)
        bar.addAction(self.act_zoom_reset)
        bar.addSeparator()

        self.act_forms = QAction("Формы", self)
        self.act_forms.setToolTip("Показать поля формы текущей страницы в панели справа")
        self.act_forms.triggered.connect(self._on_forms)
        bar.addAction(self.act_forms)

        self._forms_badge = QLabel("Полей: 0")
        self._forms_badge.setObjectName("badgeLabel")
        bar.addWidget(self._forms_badge)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        hint = QLabel("Заметки сохраняются как пометки PDF")
        hint.setObjectName("hintLabel")
        hint.setToolTip(SAVE_HINT)
        bar.addWidget(hint)

    def _build_context_bar(self) -> None:
        """Контекстные инструменты текущего режима и закрепление полного вида."""
        bar = QToolBar("Контекстные инструменты", self)
        bar.setObjectName("contextBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(16, 16))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self._context_bar = bar
        self._style_toolbar = bar  # прежнее имя полосы инструментов
        self._group_items = {"tools": [], "style": [], "forms": [], "pages": []}

        def register(group: str, item: Any, full_only: bool = False) -> Any:
            """Запоминает элемент полосы: группа + принадлежность полному виду."""
            self._group_items[group].append((item, bool(full_only)))
            return item

        # 1. Инструменты заметок — для режимов «Просмотр» и «Заметки».
        tool_group = QActionGroup(self)
        tool_group.setExclusive(True)
        self._tool_actions: dict[str, QAction] = {}
        for key, title, tip in (
            ("select", "Просмотр", "Выделять и перемещать заметки"),
            ("text", "Текст", "Поставить текстовую заметку"),
            ("highlight", "Выделение", "Выделить область цветом"),
            ("ink", "Карандаш", "Рисовать линию от руки"),
        ):
            action = QAction(title, self)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.setPriority(QAction.Priority.HighPriority)
            action.triggered.connect(lambda _checked=False, name=key: self._on_tool_selected(name))
            tool_group.addAction(action)
            self._tool_actions[key] = action
            bar.addAction(action)
            register("tools", action)
            button = bar.widgetForAction(action)
            if button is not None:
                button.setObjectName("toolButton")
        self._tool_actions["select"].setChecked(True)
        register("tools", bar.addSeparator())

        # 2. Стиль заметки. Шрифт, размер и толщина — тонкая настройка,
        #    поэтому в компактном виде они скрыты до закрепления.
        self._font_label = QLabel("Шрифт:")
        self._font_combo = QFontComboBox(self)
        font_list = getattr(pdf_backend_module, 'available_font_families',
                            lambda: ['Arial', 'Calibri', 'Times New Roman'])()
        font_list = [name for name in font_list if name.lower() not in ('cambria', 'franklin gothic')]
        self._font_combo.setModel(QStringListModel(font_list or ['Arial'], self._font_combo))
        self._font_combo.setToolTip("Шрифт текстовых заметок")
        self._font_combo.setMinimumWidth(150)
        self._font_combo.setCurrentFont(QFont(DEFAULT_FONT_FAMILY))
        self._font_combo.currentFontChanged.connect(self._on_font_changed)

        self._size_label = QLabel("Размер:")
        self._size_spin = QDoubleSpinBox(self)
        self._size_spin.setRange(6.0, 144.0)
        self._size_spin.setDecimals(0)
        self._size_spin.setSingleStep(1.0)
        self._size_spin.setValue(DEFAULT_FONT_SIZE)
        self._size_spin.setSuffix(" pt")
        self._size_spin.setToolTip("Размер текста заметки")
        self._size_spin.valueChanged.connect(self._on_size_changed)

        self._color_button = QToolButton(self)
        self._color_button.setToolTip("Цвет заметки")
        self._color_button.setAutoRaise(True)
        self._color_button.clicked.connect(self._on_pick_color)

        self._thickness_label = QLabel("Толщина:")
        self._thickness_label.setVisible(False)  # подпись заменена подсказкой
        self._thickness_spin = QDoubleSpinBox(self)
        self._thickness_spin.setRange(0.5, 12.0)
        self._thickness_spin.setDecimals(1)
        self._thickness_spin.setSingleStep(0.5)
        self._thickness_spin.setValue(DEFAULT_STROKE_WIDTH)
        self._thickness_spin.setSuffix(" pt")
        self._thickness_spin.setToolTip("Толщина линии карандаша")
        self._thickness_spin.valueChanged.connect(self._on_thickness_changed)

        for widget, full_only in (
            (self._font_label, True),
            (self._font_combo, True),
            (self._size_label, True),
            (self._size_spin, True),
            (self._color_button, False),
            (self._thickness_spin, True),
        ):
            # Скрывать нужно действие полосы (QWidgetAction): у самого виджета
            # внутри QToolBar видимость сбрасывается при пересборке раскладки.
            register("style", bar.addWidget(widget), full_only)
        register("style", bar.addSeparator())
        self._apply_color_button()

        # 3. Формы — для режима «Формы»: панель справа и подсказка о наложении.
        self.act_forms_show = QAction("Панель полей", self)
        self.act_forms_show.setToolTip("Показать поля формы текущей страницы справа")
        self.act_forms_show.triggered.connect(self._on_forms)
        bar.addAction(self.act_forms_show)
        register("forms", self.act_forms_show)

        self.act_forms_refresh = QAction("Обновить поля", self)
        self.act_forms_refresh.setToolTip("Заново прочитать поля формы из документа")
        self.act_forms_refresh.triggered.connect(self._reload_forms_panel)
        bar.addAction(self.act_forms_refresh)
        register("forms", self.act_forms_refresh)

        self._forms_hint = QLabel("")
        self._forms_hint.setObjectName("hintLabel")
        register("forms", bar.addWidget(self._forms_hint))
        register("forms", bar.addSeparator())

        # 4. Страницы — для режима «Страницы».
        style = self.style()
        self.act_rotate_left = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowBack), "Влево", self)
        self.act_rotate_left.setToolTip("Повернуть страницу против часовой стрелки")
        self.act_rotate_left.triggered.connect(lambda: self._on_rotate(False))

        self.act_rotate_right = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowForward), "Вправо", self)
        self.act_rotate_right.setToolTip("Повернуть страницу по часовой стрелке")
        self.act_rotate_right.triggered.connect(lambda: self._on_rotate(True))

        self.act_move_up = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowUp), "Выше", self)
        self.act_move_up.setToolTip("Переместить страницу выше")
        self.act_move_up.triggered.connect(lambda: self._on_move_page(-1))

        self.act_move_down = QAction(style.standardIcon(style.StandardPixmap.SP_ArrowDown), "Ниже", self)
        self.act_move_down.setToolTip("Переместить страницу ниже")
        self.act_move_down.triggered.connect(lambda: self._on_move_page(1))

        self.act_delete_page = QAction(style.standardIcon(style.StandardPixmap.SP_TrashIcon), "Удалить", self)
        self.act_delete_page.setToolTip("Удалить текущую страницу (отменяется через Ctrl+Z)")
        self.act_delete_page.triggered.connect(self._on_delete_page)

        for action in (
            self.act_rotate_left,
            self.act_rotate_right,
            self.act_move_up,
            self.act_move_down,
            self.act_delete_page,
        ):
            action.setPriority(QAction.Priority.HighPriority)
            bar.addAction(action)
            register("pages", action)
        register("pages", bar.addSeparator())

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        # Закрепление: по умолчанию компактный вид, пин открывает полный набор.
        self.act_full_mode = QAction("Полный набор", self)
        self.act_full_mode.setCheckable(True)
        self.act_full_mode.setPriority(QAction.Priority.HighPriority)
        self.act_full_mode.setToolTip("Показать полный набор инструментов (сейчас компактный вид)")
        self.act_full_mode.toggled.connect(self._on_full_mode_toggled)
        bar.addAction(self.act_full_mode)

    def _build_page_dock(self) -> None:
        """Левая колонка — миниатюры страниц документа."""
        dock = QDockWidget("Страницы", self)
        dock.setObjectName("pagesDock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        holder = QWidget(dock)
        holder.setObjectName("pagesPanel")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        heading = QLabel("Миниатюры")
        heading.setObjectName("sectionLabel")
        layout.addWidget(heading)

        self._page_list = QListWidget(holder)
        self._page_list.setObjectName("pageList")
        self._page_list.setUniformItemSizes(True)
        self._page_list.setIconSize(QSize(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT))
        self._page_list.setMinimumWidth(170)
        self._page_list.setAlternatingRowColors(False)
        self._page_list.currentRowChanged.connect(self._on_page_row_changed)
        layout.addWidget(self._page_list, 1)

        hint = QLabel("Страницы открытого документа")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        dock.setWidget(holder)
        dock.setMinimumWidth(190)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._page_dock = dock

    def _build_forms_dock(self) -> None:
        """Правая колонка — поля формы (запасной способ заполнения)."""
        dock = QDockWidget("Поля формы", self)
        dock.setObjectName("formsDock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        panel = _FormsPanel(self)
        dock.setWidget(panel)
        dock.setMinimumWidth(260)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.hide()  # панель появляется в режиме «Формы» или по кнопке «Формы»
        self._forms_dock = dock
        self._forms_dialog = panel

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self._ready_label = QLabel("Готово")
        bar.addWidget(self._ready_label, 1)
        bar.messageChanged.connect(lambda message: self._ready_label.setVisible(not bool(message)))
        self._pages_label = QLabel("Страниц: 0")
        self._zoom_label = QLabel("Масштаб: 100 %")
        self._mode_status_label = QLabel(MODE_TITLES[MODE_VIEW])
        bar.addPermanentWidget(self._pages_label)
        bar.addPermanentWidget(self._zoom_label)
        bar.addPermanentWidget(self._mode_status_label)

    # ------------------------------------------------------------------
    # Режимы, плотность и видимость групп инструментов
    # ------------------------------------------------------------------
    def _update_context_visibility(self) -> None:
        """Показывает только те группы контекстных инструментов, что нужны режиму."""
        visible_groups = {
            "tools": self._mode in (MODE_VIEW, MODE_COMMENT),
            "style": self._mode in (MODE_VIEW, MODE_COMMENT),
            "forms": self._mode == MODE_FILL,
            "pages": self._mode == MODE_PAGES,
        }
        for group, items in self._group_items.items():
            group_visible = bool(visible_groups.get(group))
            for item, full_only in items:
                try:
                    item.setVisible(group_visible and (self._full_mode or not full_only))
                except RuntimeError:  # pragma: no cover - виджет уже удалён
                    continue
        meta_action = getattr(self, "_doc_meta_action", None)
        if meta_action is not None:
            meta_action.setVisible(self._full_mode)

    def _on_full_mode_toggled(self, checked: bool) -> None:
        """Компактный вид по умолчанию, «пин» открывает полный набор."""
        self._full_mode = bool(checked)
        self.act_full_mode.setText("Свернуть" if self._full_mode else "Полный набор")
        self.act_full_mode.setToolTip(
            "Вернуть компактный вид" if self._full_mode
            else "Показать полный набор инструментов (сейчас компактный вид)"
        )
        self._update_context_visibility()
        self.show_status(
            "Полный набор инструментов." if self._full_mode
            else "Компактный вид: только нужные инструменты."
        )

    def set_full_mode(self, enabled: bool) -> None:
        """Включает полный вид программно (кнопка «пин» и настройки)."""
        self.act_full_mode.setChecked(bool(enabled))

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("Файл")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_append)
        file_menu.addAction(self.act_save)
        file_menu.addSeparator()
        quit_action = QAction("Выход", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = menu_bar.addMenu("Правка")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_rotate_left)
        edit_menu.addAction(self.act_rotate_right)
        edit_menu.addAction(self.act_move_up)
        edit_menu.addAction(self.act_move_down)
        edit_menu.addAction(self.act_delete_page)

        view_menu = menu_bar.addMenu("Вид")
        mode_menu = view_menu.addMenu("Режим")
        for key, _title, _tip in MODE_SPECS:
            action = self._mode_actions.get(key)
            if action is not None:
                mode_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_fit_width)
        view_menu.addAction(self.act_zoom_reset)
        view_menu.addSeparator()
        view_menu.addAction(self.act_full_mode)
        view_menu.addAction(self._page_dock.toggleViewAction())
        if self._forms_dock is not None:
            view_menu.addAction(self._forms_dock.toggleViewAction())

        help_menu = menu_bar.addMenu("Справка")
        about_action = QAction("О программе", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _build_shortcuts(self) -> None:
        def bind(action: QAction, sequences: Sequence[str]) -> None:
            keys = []
            for sequence in sequences:
                try:
                    keys.append(QKeySequence(sequence))
                except Exception:  # noqa: BLE001
                    continue
            if keys:
                action.setShortcuts(keys)

        bind(self.act_open, ["Ctrl+O"])
        bind(self.act_save, ["Ctrl+Shift+S", "Ctrl+S"])
        bind(self.act_undo, ["Ctrl+Z"])
        bind(self.act_redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        bind(self.act_zoom_in, ["Ctrl++", "Ctrl+="])
        bind(self.act_zoom_out, ["Ctrl+-"])
        bind(self.act_zoom_reset, ["Ctrl+0"])
        bind(self.act_delete_page, ["Ctrl+Delete"])

        self.act_prev_page = QAction("Предыдущая страница", self)
        self.act_prev_page.setShortcuts([QKeySequence("PgUp"), QKeySequence("Ctrl+PgUp")])
        self.act_prev_page.triggered.connect(lambda: self._step_page(-1))
        self.addAction(self.act_prev_page)

        self.act_next_page = QAction("Следующая страница", self)
        self.act_next_page.setShortcuts([QKeySequence("PgDown"), QKeySequence("Ctrl+PgDown")])
        self.act_next_page.triggered.connect(lambda: self._step_page(1))
        self.addAction(self.act_next_page)

        self.act_first_page = QAction("Первая страница", self)
        self.act_first_page.setShortcut(QKeySequence("Ctrl+Home"))
        self.act_first_page.triggered.connect(lambda: self._goto_page(0))
        self.addAction(self.act_first_page)

        self.act_last_page = QAction("Последняя страница", self)
        self.act_last_page.setShortcut(QKeySequence("Ctrl+End"))
        self.act_last_page.triggered.connect(lambda: self._goto_page(self.page_count - 1))
        self.addAction(self.act_last_page)

    def _wire_canvas(self) -> None:
        if self._canvas is None:
            return
        handlers = (
            ("annotation_created", self._on_annotation_created),
            ("annotation_edited", self._on_annotation_edited),
            ("annotation_deleted", self._on_annotation_deleted),
            ("selection_changed", self._on_selection_changed),
            ("zoom_changed", self._on_canvas_zoom_changed),
            # Появится вместе с правкой полей прямо на странице: сигнал
            # холста «значение поля изменено» → запись в документ.
            ("form_value_changed", self._on_canvas_form_value_changed),
        )
        for name, handler in handlers:
            signal = getattr(self._canvas, name, None)
            if signal is None:
                continue
            try:
                signal.connect(handler)
            except Exception:  # noqa: BLE001 - сигнал может быть не сигналом
                continue

    # ------------------------------------------------------------------
    # Безопасные обёртки над сессией и холстом
    # ------------------------------------------------------------------
    def _canvas_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        canvas = self._canvas
        if canvas is None:
            return None
        method = getattr(canvas, name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"Сбой при обращении к области просмотра ({name})", exc)
            return None

    def _try_canvas_call(self, name: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        """Тихий вызов возможности холста, которая может быть ещё не готова.

        Возвращает ``(получилось, результат)``: неготовый API — это не сбой
        программы, поэтому окно не показывает ошибку, а предлагает запасной путь.
        """
        canvas = self._canvas
        method = getattr(canvas, name, None) if canvas is not None else None
        if method is None:
            return (False, None)
        try:
            return (True, method(*args, **kwargs))
        except Exception:  # noqa: BLE001 - чужой холст
            return (False, None)

    def _note_error(self, context: str, exc: BaseException) -> None:
        self.show_status(f"{context}: {_error_text(exc)}")

    def _report_error(self, title: str, exc: BaseException, extra: str = "") -> None:
        message = f"{title}.\n\nПричина: {_error_text(exc)}"
        if extra:
            message += f"\n\n{extra}"
        QMessageBox.critical(self, title, message)
        self.show_status(title)

    def _report_message(self, title: str, message: str, icon: Any = None) -> None:
        self.show_status(title)
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(icon if icon is not None else QMessageBox.Icon.Warning)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def show_status(self, text: str, timeout: int = 6000) -> None:
        """Короткое сообщение в строке состояния."""
        self.statusBar().showMessage(text, timeout)

    def _show_document_warnings(self) -> None:
        messages = list(getattr(self._session, 'warnings', []) or [])
        unseen = [str(message) for message in messages if str(message) not in self._reported_warnings]
        if unseen:
            self._reported_warnings.update(unseen)
            self._report_message('Особенности документа', '\n\n'.join(unseen))

    # ------------------------------------------------------------------
    # Состояние документа
    # ------------------------------------------------------------------
    @property
    def session(self) -> Any:
        return self._session

    @property
    def page_count(self) -> int:
        session = self._session
        if session is None:
            return 0
        try:
            value = session.page_count
        except Exception:  # noqa: BLE001
            return 0
        if callable(value):  # защита от реализации в виде метода
            try:
                value = value()
            except Exception:  # noqa: BLE001
                return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @property
    def current_page_index(self) -> int:
        return self._page_index

    def is_dirty(self) -> bool:
        session = self._session
        if session is None:
            return False
        try:
            value = session.dirty
        except Exception:  # noqa: BLE001
            return False
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                return False
        return bool(value)

    def _page_size(self, index: int) -> tuple[float, float]:
        session = self._session
        if session is None:
            return (0.0, 0.0)
        try:
            size = session.page_size(index)
        except Exception as exc:  # noqa: BLE001
            self._note_error("Не удалось определить размер страницы", exc)
            return (0.0, 0.0)
        try:
            width, height = size  # type: ignore[misc]
            return (_as_float(width, 595.0), _as_float(height, 842.0))
        except Exception:  # noqa: BLE001
            return (595.0, 842.0)

    def _annotations(self, index: int) -> list[Any]:
        session = self._session
        if session is None:
            return []
        try:
            stored = list(session.get_annotations(index) or [])
        except Exception as exc:  # noqa: BLE001
            self._note_error("Не удалось прочитать заметки страницы", exc)
            return []
        return [_annotation_for_canvas(item) for item in stored]

    def _store_annotation(self, call: Any, *args: Any) -> None:
        """Вызывает метод сессии, при необходимости меняя формат точек заметки.

        Контракт ``Annotation`` описывает ``points`` плоским списком, но
        отдельные сборки хранят штрихи списком списков. Сначала пробуем
        контрактный вариант, затем — запасной, чтобы рисунок не терялся.
        Последний аргумент — сама заметка.
        """
        try:
            call(*args)
            return
        except Exception as first:  # noqa: BLE001
            annotation = args[-1] if args else None
            adapted = _annotation_as_strokes(annotation)
            if adapted is None:
                raise
            try:
                call(*args[:-1], adapted)
            except Exception:  # noqa: BLE001
                raise first

    def form_fields(self, index: Optional[int] = None) -> list[Any]:
        """Поля формы страницы (для панели форм и индикатора количества)."""
        session = self._session
        if session is None:
            return []
        if index is None:
            index = self._page_index
        try:
            return list(session.form_fields(index) or [])
        except Exception as exc:  # noqa: BLE001
            self._note_error("Не удалось прочитать поля формы", exc)
            return []

    # ------------------------------------------------------------------
    # Поля формы: наложение на страницу (новый API) и запасная панель
    # ------------------------------------------------------------------
    def canvas_draws_forms(self) -> bool:
        """True, если холст умеет рисовать поля поверх страницы."""
        return callable(getattr(self._canvas, "set_form_widgets", None))

    def _session_form_widgets(self, index: int) -> list[dict[str, Any]]:
        """Поля формы в координатах страницы (API ``form_widgets`` сессии).

        Пока сессия его не реализовала, список пуст, а поля заполняются через
        правую панель — она работает на старом ``form_fields``.
        """
        session = self._session
        getter = getattr(session, "form_widgets", None) if session is not None else None
        if getter is None:
            return []
        try:
            raw = list(getter(index) or [])
        except Exception as exc:  # noqa: BLE001
            self._note_error("Не удалось прочитать поля формы", exc)
            return []
        widgets: list[dict[str, Any]] = []
        for item in raw:
            data = self._form_widget_data(item)
            if data:
                widgets.append(data)
        return widgets

    @staticmethod
    def _form_widget_data(item: Any) -> dict[str, Any]:
        """Копия описания поля; ``{}`` — поле без ключа или геометрии не годится.

        Лишние ключи сохраняются: описание поля принадлежит сессии, а холст
        сам решает, какие из них ему нужны.
        """
        if isinstance(item, dict):
            data = dict(item)
        else:
            data = {
                name: getattr(item, name)
                for name in FORM_WIDGET_KEYS
                if hasattr(item, name)
            }
        if not str(data.get("key") or ""):
            return {}
        rect = data.get("rect")
        if rect is not None:
            try:
                if len(rect) != 4:
                    return {}  # без прямоугольника наложение не нарисовать
            except TypeError:
                return {}
        return data

    def _sync_form_widgets(self) -> bool:
        """Передаёт холсту поля формы текущей страницы.

        ``False`` — холст ещё не умеет их рисовать, значит работает правая
        панель. Вне режима «Формы» холсту уходит пустой список, чтобы наложение
        не мешало просмотру и заметкам.
        """
        self._canvas_forms_supported = self.canvas_draws_forms()
        if not self._canvas_forms_supported:
            return False
        widgets: list[dict[str, Any]] = []
        if self._mode == MODE_FILL and self.page_count:
            widgets = self._session_form_widgets(self._page_index)
        self._canvas_form_widgets = widgets
        self._canvas_call("set_form_widgets", widgets)
        return True

    def _export_value_for(self, key: str, value: Any) -> Any:
        """Значение для записи в PDF, если оно отличается от показанного.

        ``export_value`` заполнен только у переключателей (radio): там в
        документ уходит служебная строка, а не подпись кнопки. У остальных
        видов полей он пуст, и в документ идёт введённое значение.
        """
        for widget in self._canvas_form_widgets:
            if str(widget.get("key") or "") != key:
                continue
            export = widget.get("export_value")
            if not export:
                return value
            shown = widget.get("value")
            if shown is None or shown == value or str(shown) == str(value):
                return export
            return value
        return value

    def _focus_form_field(self, key: Any) -> bool:
        """Просит холст показать поле на странице (кнопка «→» в панели)."""
        text = str(key or "")
        if not text:
            return False
        ok, _result = self._try_canvas_call("focus_form_field", text)
        if not ok:
            self.show_status(
                "Переход к полю на странице ещё не подключён — заполняйте поле в правой панели.",
                8000,
            )
        return ok

    def _on_canvas_form_value_changed(self, key: Any, value: Any) -> None:
        """Холст сообщил новое значение поля: пишем в документ и обновляем вид."""
        if self._syncing or self._session is None:
            return
        text = str(key or "")
        if not text:
            return
        applied, failed, error = self.apply_form_values([(text, value)])
        if failed and error is not None:
            self._report_error("Не удалось записать значение поля", error)
            return
        if applied:
            self.show_status(f"Поле «{text}» записано в документ.")
            if self._forms_dialog is not None and self._forms_dialog.isVisible():
                self._forms_dialog.reload()

    def _reload_forms_panel(self) -> None:
        """Обновляет правую панель полей и заново читает поля из документа."""
        self._field_counts.pop(self._page_index, None)
        if self._forms_dialog is not None:
            self._forms_dialog.reload()
        self._refresh_forms_badge()
        self._sync_form_widgets()
        self.show_status("Поля формы перечитаны.")

    def _ensure_forms_panel_visible(self) -> None:
        """В режиме форм правая панель нужна, если холст не рисует поля сам."""
        if self.canvas_draws_forms() or self._forms_dock is None or not self.page_count:
            return
        if self._forms_dialog is not None:
            # Поля читаются заново: панель — единственный способ их заполнить.
            self._forms_dialog.reload()
        self._forms_dock.show()

    # ------------------------------------------------------------------
    # Обновление интерфейса
    # ------------------------------------------------------------------
    def _refresh_all(self) -> None:
        self._page_index = self._clamp_index(self._page_index)
        self._rebuild_page_list()
        self._render_page()
        self._update_actions()
        self._update_status()
        self._refresh_title()
        self._refresh_forms_badge()

    def _clamp_index(self, index: int) -> int:
        count = self.page_count
        if count <= 0:
            return 0
        return max(0, min(int(index), count - 1))

    def _refresh_title(self) -> None:
        if self._source_path:
            name = os.path.basename(self._source_path)
        else:
            name = "документ не открыт"
        mark = " *" if self.is_dirty() else ""
        self.setWindowTitle(f"{APP_NAME} — {name}{mark}")
        self._doc_title_label.setText(name)
        self._doc_title_label.setToolTip(self._source_path or "Файл ещё не открыт")
        count = self.page_count
        if count:
            meta = f"{count} стр."
            if self.is_dirty():
                meta += " · есть изменения"
        else:
            meta = ""
        self._doc_meta_label.setText(meta)

    def _update_status(self) -> None:
        count = self.page_count
        if self._session is None:
            text = "Модуль работы с PDF недоступен"
        elif count == 0:
            text = "Готово. Откройте PDF-файл (Ctrl+O) или перетащите его в окно."
        else:
            text = f"Готово. Страница {self._page_index + 1} из {count}."
            if self._selected_annotation >= 0:
                text += f" Выбрана заметка №{self._selected_annotation + 1}."
            if self._render_scale_used + 1e-6 < self._zoom:
                text += " Предпросмотр ограничен размером страницы."
        self._ready_label.setText(text)
        self._pages_label.setText(f"Страниц: {count}")
        self._zoom_label.setText(f"Масштаб: {int(round(self._zoom * 100))} %")
        self._zoom_value_label.setText(f"{int(round(self._zoom * 100))}%")
        self._mode_status_label.setText(MODE_TITLES.get(self._mode, ""))

    def _refresh_forms_badge(self) -> None:
        count = self.page_count
        if count == 0:
            self._forms_badge.setText("Полей: 0")
            self.act_forms.setEnabled(False)
        else:
            self.act_forms.setEnabled(True)
            index = self._page_index
            if index not in self._field_counts:
                self._field_counts[index] = len(self.form_fields(index))
            self._forms_badge.setText(f"Полей: {self._field_counts[index]}")
        if getattr(self, "_forms_hint", None) is not None:
            if self.canvas_draws_forms():
                self._forms_hint.setText("Поля заполняются прямо на странице.")
            else:
                self._forms_hint.setText("Наложение полей ещё не подключено — панель справа.")

    def _update_actions(self) -> None:
        count = self.page_count
        has_doc = count > 0
        session_ready = self._session is not None

        self.act_open.setEnabled(session_ready)
        self.act_append.setEnabled(session_ready)
        self.act_save.setEnabled(has_doc)
        self.act_undo.setEnabled(has_doc)
        self.act_redo.setEnabled(has_doc)
        for action in (self.act_zoom_in, self.act_zoom_out, self.act_fit_width, self.act_zoom_reset):
            action.setEnabled(has_doc or self._canvas is not None)
        for action in self._tool_actions.values():
            action.setEnabled(has_doc)
        for action in self._mode_actions.values():
            action.setEnabled(has_doc)
        self.act_forms_show.setEnabled(has_doc)
        self.act_forms_refresh.setEnabled(has_doc)
        self.act_rotate_left.setEnabled(has_doc)
        self.act_rotate_right.setEnabled(has_doc)
        self.act_move_up.setEnabled(count > 1 and self._page_index > 0)
        self.act_move_down.setEnabled(count > 1 and self._page_index < count - 1)
        self.act_delete_page.setEnabled(count > 1)
        self.act_prev_page.setEnabled(self._page_index > 0)
        self.act_next_page.setEnabled(has_doc and self._page_index < count - 1)
        self.act_first_page.setEnabled(has_doc and self._page_index > 0)
        self.act_last_page.setEnabled(has_doc and self._page_index < count - 1)

    def _rebuild_page_list(self) -> None:
        widget = self._page_list
        count = self.page_count
        thumbnails = 0 < count <= THUMBNAIL_MAX_PAGES
        previous = widget.blockSignals(True)
        try:
            widget.clear()
            for index in range(count):
                item = QListWidgetItem(f"Страница {index + 1}")
                item.setData(Qt.ItemDataRole.UserRole, index)
                item.setToolTip(f"Открыть страницу {index + 1}")
                if thumbnails:
                    pixmap = self._thumbnail_pixmap(index)
                    if pixmap is not None:
                        item.setIcon(QIcon(pixmap))
                        item.setSizeHint(QSize(THUMBNAIL_WIDTH + 24, THUMBNAIL_HEIGHT + 24))
                widget.addItem(item)
            if count:
                widget.setCurrentRow(self._clamp_index(self._page_index))
        finally:
            widget.blockSignals(previous)

    def _thumbnail_pixmap(self, index: int) -> Optional[QPixmap]:
        """Миниатюра страницы для левой колонки; ``None`` — если не вышло.

        Страница рисуется мелким масштабом и только для документов разумного
        размера (THUMBNAIL_MAX_PAGES), чтобы открытие файла не тормозило.
        """
        session = self._session
        canvas = self._canvas
        if session is None or canvas is None:
            return None
        converter = getattr(canvas, "_to_pixmap", None)
        if converter is None:
            converter = getattr(type(canvas), "_to_pixmap", None)
        if converter is None:
            return None  # холст не умеет превращать растр страницы в картинку
        try:
            image = session.render(index, scale=THUMBNAIL_SCALE)
        except Exception:  # noqa: BLE001 - миниатюра не важнее самого документа
            return None
        try:
            pixmap = converter(image)
        except Exception:  # noqa: BLE001
            return None
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap.scaled(
            QSize(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _update_page_item(self, index: int) -> None:
        item = self._page_list.item(index)
        if item is None:
            return
        count = len(self._annotations(index))
        if count:
            item.setToolTip(f"Страница {index + 1} · заметок: {count}")
        else:
            item.setToolTip(f"Открыть страницу {index + 1}")

    # ------------------------------------------------------------------
    # Отрисовка страницы
    # ------------------------------------------------------------------
    def _render_scale(self, size: tuple[float, float]) -> float:
        scale = max(self._min_zoom, min(self._max_zoom, float(self._zoom)))
        width, height = size
        if width <= 0 or height <= 0:
            return scale
        limit = scale
        longest = max(width, height) * scale
        if longest > MAX_RENDER_SIDE:
            limit = min(limit, MAX_RENDER_SIDE / max(width, height))
        pixels = (width * scale) * (height * scale)
        if pixels > MAX_RENDER_PIXELS:
            limit = min(limit, (MAX_RENDER_PIXELS / (width * height)) ** 0.5)
        return max(0.05, min(scale, limit))

    def _render_page(self, preserve_selection: Optional[int] = None) -> None:
        count = self.page_count
        if self._session is None or self._canvas is None or count == 0:
            self._page_index = 0
            self._render_scale_used = self._zoom
            self._syncing = True
            try:
                self._canvas_call("set_page", None, (0.0, 0.0), [])
            finally:
                self._syncing = False
            self._update_status()
            self._update_actions()
            return
        if self._rendering:
            return

        self._rendering = True
        try:
            index = self._clamp_index(self._page_index)
            self._page_index = index
            size = self._page_size(index)
            scale = self._render_scale(size)
            self._render_scale_used = scale

            image = None
            try:
                with _busy_cursor():
                    image = self._session.render(index, scale=scale)
            except Exception as exc:  # noqa: BLE001
                self._report_error(
                    "Не удалось показать страницу",
                    exc,
                    "Документ остался открытым — можно перейти на другую страницу "
                    "или сохранить копию.",
                )

            annotations = self._annotations(index)
            self._syncing = True
            try:
                self._canvas_call("set_page", image, size, annotations)
                self._canvas_call("set_zoom", self._zoom)
                # Поля формы живут поверх страницы: после set_page их нужно
                # отдать холсту заново, иначе наложение пропадёт.
                self._sync_form_widgets()
            finally:
                self._syncing = False
        finally:
            self._rendering = False

        if preserve_selection is not None:
            # Холст сбрасывает выделение при set_page — возвращаем его,
            # чтобы дальнейшая правка стиля относилась к той же заметке.
            self._selected_annotation = preserve_selection
            self._canvas_call('select_annotation', preserve_selection)
        self._update_status()
        self._update_actions()
        self._update_page_item(self._page_index)

    def _goto_page(self, index: int) -> None:
        count = self.page_count
        if count == 0:
            return
        index = max(0, min(int(index), count - 1))
        if index == self._page_index:
            return
        self._page_index = index
        widget = self._page_list
        previous = widget.blockSignals(True)
        try:
            widget.setCurrentRow(index)
        finally:
            widget.blockSignals(previous)
        self._clear_selection()
        self._render_page()
        self._refresh_forms_badge()
        self._reset_scroll()
        if self._forms_dialog is not None and self._forms_dialog.isVisible():
            self._forms_dialog.reload()

    def _step_page(self, delta: int) -> None:
        self._goto_page(self._page_index + int(delta))

    def _on_page_row_changed(self, row: int) -> None:
        if self._syncing:
            return
        if row < 0 or row >= self.page_count or row == self._page_index:
            self._update_actions()
            return
        self._page_index = row
        self._clear_selection()
        self._render_page()
        self._refresh_forms_badge()
        self._reset_scroll()
        if self._forms_dialog is not None and self._forms_dialog.isVisible():
            self._forms_dialog.reload()

    # ------------------------------------------------------------------
    # Инструменты и стиль
    # ------------------------------------------------------------------
    def _on_tool_selected(self, name: str) -> None:
        """Инструмент заметок выбран на контекстной полосе."""
        if name not in self._tool_actions:
            return
        mode = MODE_VIEW if name == "select" else MODE_COMMENT
        self._select_mode(mode, tool=name)
        titles = {
            "select": "Режим просмотра: выделяйте и перемещайте заметки.",
            "text": "Режим текста: щёлкните по странице, чтобы поставить заметку.",
            "highlight": "Режим выделения: обведите область, чтобы выделить её цветом.",
            "ink": "Режим карандаша: рисуйте линию, удерживая кнопку мыши.",
        }
        self.show_status(titles.get(name, "Инструмент выбран."))

    def _select_mode(self, name: str, *, tool: Optional[str] = None) -> None:
        """Переключает режим окна и, если нужно, инструмент холста."""
        if name not in MODE_TITLES:
            return
        self._mode = name
        action = self._mode_actions.get(name)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        if tool is not None:
            self._apply_tool(tool)
        self._update_context_visibility()
        self._refresh_forms_badge()
        self._sync_form_widgets()

    def _on_mode_selected(self, name: str) -> None:
        """Режим выбран кнопкой: подбираем инструмент и показываем подсказку."""
        if name not in MODE_TITLES:
            return
        if name == MODE_COMMENT:
            tool = self._last_comment_tool if self._last_comment_tool in self._tool_actions else "text"
        elif name == MODE_FILL:
            tool = "form"
        else:
            tool = "select"
        self._select_mode(name, tool=tool)
        if name == MODE_FILL:
            self._ensure_forms_panel_visible()
        self.show_status(MODE_STATUS.get(name, "Режим выбран."))

    def _apply_tool(self, name: str) -> None:
        """Запоминает инструмент и передаёт его холсту, если тот его знает."""
        self._tool = name
        action = self._tool_actions.get(name)
        if action is not None:
            if not action.isChecked():
                action.setChecked(True)
            self._last_comment_tool = name
        self._push_canvas_tool(name)
        self._update_style_enabled()

    def _push_canvas_tool(self, name: str) -> bool:
        """Отдаёт инструмент холсту; ``False`` — холст такого ещё не знает.

        Инструмент ``form`` (режим заполнения прямо на странице) пишется
        другим агентом. Пока холст его не принимает, окно не трогает холст
        и работает через панель полей — без ошибок и ложных обещаний.
        """
        canvas = self._canvas
        method = getattr(canvas, "set_tool", None) if canvas is not None else None
        if method is None:
            return False
        known = getattr(canvas_module, "TOOLS", None)
        if known is not None:
            try:
                if name not in tuple(known):
                    return False
            except TypeError:  # pragma: no cover - чужой формат константы
                pass
        try:
            method(name)
            return True
        except Exception as exc:  # noqa: BLE001 - чужой холст
            if name in BASE_CANVAS_TOOLS:
                self._note_error("Не удалось переключить инструмент", exc)
            return False

    def _canvas_accepts_tool(self, name: str) -> bool:
        """True, если холст объявляет такой инструмент (без вызова set_tool)."""
        known = getattr(canvas_module, "TOOLS", None)
        if known is None:
            canvas = self._canvas
            return canvas is not None and hasattr(canvas, "set_tool")
        try:
            return name in tuple(known)
        except TypeError:  # pragma: no cover - чужой формат константы
            return False

    def _update_style_enabled(self) -> None:
        """Доступность элементов стиля: по инструменту и по выбранной заметке."""
        tool = self._tool
        kind = self._selected_kind()
        font_enabled = tool == "text" or kind == "text"
        color_enabled = tool in ("text", "highlight", "ink") or kind in (
            "text",
            "highlight",
            "ink",
        )
        thickness_enabled = tool == "ink" or kind == "ink"
        self._font_label.setEnabled(font_enabled)
        self._font_combo.setEnabled(font_enabled)
        self._size_label.setEnabled(font_enabled)
        self._size_spin.setEnabled(font_enabled)
        self._color_button.setEnabled(color_enabled)
        self._thickness_label.setEnabled(thickness_enabled)
        self._thickness_spin.setEnabled(thickness_enabled)

    def _push_style(self) -> None:
        self._canvas_call(
            "set_style",
            font_family=self._style["font_family"],
            font_size=float(self._style["font_size"]),
            color=self._style["color"],
            stroke_width=float(self._style["stroke_width"]),
        )

    def _on_font_changed(self, font: QFont) -> None:
        family = font.family()
        self._style["font_family"] = family
        self._apply_style_to_selection("font_family", family)
        self._push_style()

    def _on_size_changed(self, value: float) -> None:
        self._style["font_size"] = float(value)
        self._apply_style_to_selection("font_size", float(value))
        self._push_style()

    def _on_thickness_changed(self, value: float) -> None:
        self._style["stroke_width"] = float(value)
        self._apply_style_to_selection("stroke_width", float(value))
        self._push_style()

    def _on_pick_color(self) -> None:
        current = QColor(str(self._style["color"]))
        chosen = QColorDialog.getColor(current, self, "Цвет заметки")
        if not chosen.isValid():
            return
        self._style["color"] = chosen.name()
        self._apply_color_button()
        self._apply_style_to_selection("color", chosen.name())
        self._push_style()

    def _apply_color_button(self) -> None:
        color = QColor(str(self._style["color"]))
        self._color_button.setIcon(_color_icon(color))
        self._color_button.setToolTip(f"Цвет заметки: {color.name()}")

    # ------------------------------------------------------------------
    # Выбранная заметка и стиль
    # ------------------------------------------------------------------
    def _stored_annotations(self, index: int) -> list[Any]:
        """Заметки страницы как они лежат в документе (без адаптации)."""
        session = self._session
        if session is None:
            return []
        try:
            return list(session.get_annotations(index) or [])
        except Exception as exc:  # noqa: BLE001
            self._note_error("Не удалось прочитать заметки страницы", exc)
            return []

    def _selected_annotation_object(self) -> Optional[Any]:
        index = self._selected_annotation
        if index < 0:
            return None
        stored = self._stored_annotations(self._page_index)
        if 0 <= index < len(stored):
            return stored[index]
        return None

    def _selected_kind(self) -> str:
        annotation = self._selected_annotation_object()
        if annotation is None:
            return ""
        return str(getattr(annotation, "kind", "") or "")

    def _style_field_applies(self, kind: str, field: str) -> bool:
        """Какие поля стиля применимы к заметке данного типа."""
        if not kind:
            return False
        if field in ("font_family", "font_size"):
            return kind == "text"
        if field == "stroke_width":
            return kind == "ink"
        if field == "color":
            return kind in ("text", "highlight", "ink")
        return False

    def _apply_style_to_selection(self, field: str, value: Any) -> bool:
        """Переносит изменение стиля на выбранную заметку.

        Возвращает True, если заметка действительно обновлена. Выделение
        сохраняется, а страница перерисовывается, чтобы новый стиль был виден.
        """
        if self._session is None or self._selected_annotation < 0:
            return False
        annotation = self._selected_annotation_object()
        if annotation is None:
            return False
        kind = str(getattr(annotation, "kind", "") or "")
        if not self._style_field_applies(kind, field):
            return False
        try:
            updated = dataclasses.replace(annotation, **{field: value})
        except Exception:  # noqa: BLE001 - чужой объект заметки
            return False

        selected = self._selected_annotation
        try:
            self._store_annotation(
                self._session.update_annotation, self._page_index, selected, updated
            )
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось изменить заметку", exc)
            return False
        self._render_page(preserve_selection=selected)
        self._refresh_title()
        return True

    def _sync_style_from_selection(self) -> None:
        """Показывает в элементах стиля значения выбранной заметки.

        Сигналы элементов блокируются, чтобы выбор заметки сам по себе
        не изменял документ.
        """
        annotation = self._selected_annotation_object()
        if annotation is None:
            return
        kind = str(getattr(annotation, "kind", "") or "")

        if self._style_field_applies(kind, "font_family"):
            family = str(getattr(annotation, "font_family", "") or "")
            if family and family != self._font_combo.currentFont().family():
                self._style["font_family"] = family
                blocked = self._font_combo.blockSignals(True)
                try:
                    self._font_combo.setCurrentFont(QFont(family))
                finally:
                    self._font_combo.blockSignals(blocked)

        if self._style_field_applies(kind, "font_size"):
            size = _as_float(getattr(annotation, "font_size", 0.0), 0.0)
            if size > 0 and abs(size - self._size_spin.value()) > 1e-6:
                self._style["font_size"] = size
                blocked = self._size_spin.blockSignals(True)
                try:
                    self._size_spin.setValue(size)
                finally:
                    self._size_spin.blockSignals(blocked)

        if self._style_field_applies(kind, "stroke_width"):
            width = _as_float(getattr(annotation, "stroke_width", 0.0), 0.0)
            if width > 0 and abs(width - self._thickness_spin.value()) > 1e-6:
                self._style["stroke_width"] = width
                blocked = self._thickness_spin.blockSignals(True)
                try:
                    self._thickness_spin.setValue(width)
                finally:
                    self._thickness_spin.blockSignals(blocked)

        color = QColor(str(getattr(annotation, "color", "") or ""))
        if color.isValid() and self._style_field_applies(kind, "color"):
            name = color.name()
            if name != str(self._style["color"]):
                self._style["color"] = name
                self._apply_color_button()
        self._push_style()

    # ------------------------------------------------------------------
    # Заметки
    # ------------------------------------------------------------------
    def _on_annotation_created(self, annotation: Any) -> None:
        if self._syncing or self._session is None or annotation is None:
            return
        try:
            self._store_annotation(self._session.add_annotation, self._page_index, annotation)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось добавить заметку", exc)
            return
        self._refresh_title()
        self._update_status()
        self._update_page_item(self._page_index)

    def _on_annotation_edited(self, index: int, annotation: Any) -> None:
        if self._syncing or self._session is None:
            return
        try:
            self._store_annotation(
                self._session.update_annotation, self._page_index, int(index), annotation
            )
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось изменить заметку", exc)
            return
        self._refresh_title()
        self._update_status()
        self._update_page_item(self._page_index)

    def _on_annotation_deleted(self, index: int) -> None:
        if self._syncing or self._session is None:
            return
        try:
            self._session.remove_annotation(self._page_index, int(index))
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось удалить заметку", exc)
            return
        self._clear_selection()
        self._refresh_title()
        self._update_status()
        self._update_page_item(self._page_index)

    def _on_selection_changed(self, index: int) -> None:
        if self._syncing:
            return
        try:
            self._selected_annotation = int(index)
        except (TypeError, ValueError):
            self._clear_selection()
        # Выбор заметки только показывает её стиль — документ не меняется.
        self._sync_style_from_selection()
        self._update_style_enabled()
        self._update_status()

    def _clear_selection(self) -> None:
        self._selected_annotation = -1  # выделения нет
        self._update_style_enabled()

    # ------------------------------------------------------------------
    # Масштаб
    # ------------------------------------------------------------------
    def _set_zoom(self, scale: float, *, render: bool = True) -> None:
        scale = max(self._min_zoom, min(self._max_zoom, float(scale)))
        changed = abs(scale - self._zoom) > 1e-6
        self._zoom = scale
        self._canvas_call("set_zoom", self._zoom)
        self._update_status()
        if changed and render:
            self._render_page()

    def _on_zoom_in(self) -> None:
        self._fit_width = False
        self._set_zoom(self._zoom * ZOOM_STEP)

    def _on_zoom_out(self) -> None:
        self._fit_width = False
        self._set_zoom(self._zoom / ZOOM_STEP)

    def _on_zoom_reset(self) -> None:
        self._fit_width = False
        self._set_zoom(1.0)

    def _on_fit_width(self) -> None:
        self._fit_width = True
        self._apply_fit_width()

    def _canvas_width(self) -> int:
        area = self._scroll_area
        if area is not None:
            try:
                return int(area.viewport().width())
            except Exception:  # noqa: BLE001
                pass
        canvas = self._canvas
        if canvas is None:
            return 0
        viewport = getattr(canvas, "viewport", None)
        if callable(viewport):
            try:
                return int(viewport().width())
            except Exception:  # noqa: BLE001
                pass
        try:
            return int(canvas.width())
        except Exception:  # noqa: BLE001
            return 0

    def _reset_scroll(self) -> None:
        """Возвращает прокрутку к началу страницы (после смены страницы)."""
        area = self._scroll_area
        if area is None:
            return

        def apply() -> None:
            try:
                area.horizontalScrollBar().setValue(0)
                area.verticalScrollBar().setValue(0)
            except RuntimeError:  # pragma: no cover - виджет уже удалён
                pass

        QTimer.singleShot(0, apply)

    def _apply_fit_width(self) -> None:
        count = self.page_count
        if count == 0:
            return
        width = self._page_size(self._page_index)[0]
        area = self._scroll_area
        if width <= 0:
            return
        available = self._canvas_width() - 2 * PAGE_MARGIN
        if available <= 40:
            self._update_status()
            return
        scale = available / width
        # Если страница окажется выше окна, появится полоса прокрутки.
        viewport_height = 0
        if area is not None:
            try:
                viewport_height = int(area.viewport().height())
            except Exception:  # noqa: BLE001
                viewport_height = 0
        height = self._page_size(self._page_index)[1]
        if viewport_height and height * scale > viewport_height - 2 * PAGE_MARGIN:
            extent = 0
            try:
                extent = int(area.verticalScrollBar().sizeHint().width())
            except Exception:  # noqa: BLE001
                extent = 0
            if extent > 0:
                scale = max(60.0, available - extent) / width
        self._set_zoom(scale)

    def _on_canvas_zoom_changed(self, scale: Any) -> None:
        if self._syncing or self._canvas is None:
            return
        try:
            value = float(scale)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self._fit_width = False
        if abs(value - self._zoom) < 1e-6:
            return
        self._zoom = max(self._min_zoom, min(self._max_zoom, value))
        self._update_status()
        self._render_page()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        super().resizeEvent(event)
        if self._fit_width and self.page_count:
            QTimer.singleShot(0, self._apply_fit_width)

    # ------------------------------------------------------------------
    # Файловые операции
    # ------------------------------------------------------------------
    def _dialog_dir(self) -> str:
        if self._source_path:
            folder = os.path.dirname(self._source_path)
            if folder and os.path.isdir(folder):
                return folder
        return self._last_dir

    def _guard_dirty(self, action_text: str) -> bool:
        """Спрашивает о несохранённых изменениях. True — можно продолжать."""
        if self._session is None or self.page_count == 0 or not self.is_dirty():
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Есть несохранённые изменения")
        box.setText(
            f"В документе есть несохранённые изменения.\n\n"
            f"Сохранить копию перед тем, как {action_text}?"
        )
        save_button = box.addButton("Сохранить копию", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return self._on_save_copy()
        if clicked is discard_button:
            return True
        return False

    def _on_open(self) -> None:
        if self._session is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PDF", self._dialog_dir(), PDF_FILTER)
        if not path:
            return
        # Защита от потери изменений выполняется внутри open_path.
        self.open_path(path)

    def open_path(self, path: str) -> bool:
        """Открывает файл по пути (используется в том числе при запуске)."""
        text = str(path or "").strip().strip('"')
        if not text:
            return False
        if not os.path.isfile(text):
            self._report_message(
                "Файл не найден",
                f"Не удалось найти файл:\n{text}",
                QMessageBox.Icon.Critical,
            )
            return False
        if self._session is None:
            return False
        if self.page_count and not self._guard_dirty("открыть другой файл"):
            return False
        return self._open_file(text)

    def _restore_previous_document(self, path: Optional[str], pages: int) -> bool:
        """Возвращает прежний документ, если неудачное открытие его выгрузило."""
        if not path or pages <= 0 or self._session is None or self.page_count > 0:
            return False
        try:
            with _busy_cursor():
                self._session.open(path)
        except Exception:  # noqa: BLE001
            return False
        self._source_path = path
        self._field_counts.clear()
        self._page_index = self._clamp_index(self._page_index)
        self._refresh_all()
        return True

    def _open_file(self, path: str, password: Optional[str] = None) -> bool:
        if self._session is None:
            return False
        if self.page_count:
            # Документ заменяется: судьба его временной копии решается здесь.
            self._release_recovery_on_leave()
        previous_path = self._source_path
        previous_pages = self.page_count
        attempts = 0
        current_password = password
        while True:
            try:
                with _busy_cursor():
                    self._session.open(path, current_password)
            except Exception as exc:  # noqa: BLE001
                if _looks_like_password_error(exc) and attempts < 3:
                    attempts += 1
                    entered, accepted = QInputDialog.getText(
                        self,
                        "Требуется пароль",
                        f"Файл защищён паролем:\n{os.path.basename(path)}\n\n"
                        "Введите пароль:",
                        QLineEdit.EchoMode.Password,
                    )
                    if not accepted:
                        self.show_status("Открытие отменено.")
                        return False
                    current_password = entered
                    continue
                extra = "Возможно, файл повреждён, защищён паролем или это не PDF."
                if previous_pages and self.page_count == 0:
                    # Некоторые сборки очищают документ перед разбором файла.
                    if self._restore_previous_document(previous_path, previous_pages):
                        extra += "\n\nПрежний документ открыт заново — работа не потеряна."
                    else:
                        self._source_path = None
                        self._clear_selection()
                        self._field_counts.clear()
                        self._refresh_all()
                        extra += "\n\nПрежний документ закрылся, откройте его снова."
                self._report_error("Не удалось открыть файл", exc, extra)
                return False
            break

        self._source_path = os.path.abspath(path)
        self._last_dir = os.path.dirname(self._source_path) or self._last_dir
        # Обычное открытие: документ снова привязан к своим исходникам.
        self._recovery_key = None
        self._recovery_copy_path = None
        self._recovery_source_paths = []
        self._recovery_restored = False
        self._recovery_snapshot_revision = self._session_revision()
        self._recovery_last_path = None
        self._page_index = 0
        self._zoom = 1.0
        self._fit_width = False
        self._clear_selection()
        self._field_counts.clear()
        self._refresh_all()
        self._reset_scroll()
        self.show_status(f"Открыт файл: {os.path.basename(path)}")
        self._reported_warnings.clear()
        self._show_document_warnings()
        return True

    def _on_append(self) -> None:
        if self._session is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить PDF-файлы", self._dialog_dir(), PDF_FILTER
        )
        if not paths:
            return
        self.append_files(paths)

    def append_files(self, paths: Sequence[str]) -> bool:
        """Добавляет страницы из выбранных файлов в конец документа."""
        if self._session is None:
            return False
        files = [str(path) for path in paths if str(path or "").strip()]
        if not files:
            return False

        if self.page_count == 0:
            first, files = files[0], files[1:]
            if not self._open_file(first):
                return False
            if not files:
                return True

        before = self.page_count
        added: list[str] = []
        failed: list[tuple[str, str]] = []
        for path in files:
            try:
                with _busy_cursor():
                    self._session.append([path])
            except Exception as exc:  # noqa: BLE001
                failed.append((os.path.basename(path), _error_text(exc)))
                continue
            added.append(path)

        if added:
            self._field_counts.clear()
            self._page_index = min(before, max(0, self.page_count - 1))
            self._refresh_all()
            self.show_status(f"Добавлено файлов: {len(added)}. Страниц: {self.page_count}.")
            self._show_document_warnings()
        if failed:
            details = "\n".join(f"• {name}: {reason}" for name, reason in failed[:8])
            if len(failed) > 8:
                details += f"\n• и ещё {len(failed) - 8}…"
            self._report_message(
                "Часть файлов не добавлена",
                "Следующие файлы добавить не удалось "
                "(возможно, они защищены паролем или повреждены):\n\n" + details,
            )
        return bool(added)

    def _on_save_copy(self) -> bool:
        if self._session is None:
            return False
        if self.page_count == 0:
            self._report_message("Нет документа", "Сначала откройте PDF-файл.")
            return False

        if self._recovery_restored and self._recovery_source_paths:
            # Документ открыт из временной копии: предлагаем сохранить рядом
            # с исходным файлом, а не внутри служебной папки.
            origin = self._recovery_source_paths[0]
            folder = os.path.dirname(origin)
            stem = Path(origin).stem
            suggested = os.path.join(folder, f"{stem} (восстановлено).pdf")
        elif self._source_path:
            folder = os.path.dirname(self._source_path)
            stem = Path(self._source_path).stem
            suggested = os.path.join(folder, f"{stem} (копия).pdf")
        else:
            suggested = os.path.join(self._dialog_dir(), "Документ.pdf")

        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить копию PDF", suggested, PDF_FILTER
        )
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        protected = list(self._recovery_source_paths)
        if self._source_path:
            protected.append(self._source_path)
        if any(_same_path(path, item) for item in protected):
            self._report_message(
                "Исходный файл защищён",
                "PdfDesk никогда не перезаписывает исходный файл — он всегда "
                "сохраняет копию.\n\nВыберите другое имя или другую папку.",
            )
            return False
        if self._recovery_contains(path):
            self._report_message(
                "Служебная папка восстановления",
                "Нельзя сохранять копию внутрь служебной папки временного "
                "восстановления — выберите другую папку.",
            )
            return False

        try:
            with _busy_cursor():
                self._session.save(path)
        except Exception as exc:  # noqa: BLE001
            # Сохранение не удалось: документ остаётся открытым, а временная
            # копия — на диске (обновляем её, ничего не теряя).
            self._snapshot_recovery(force=True)
            self._report_error(
                "Не удалось сохранить копию",
                exc,
                "Документ остался открытым в программе — работа не потеряна.\n"
                "Попробуйте другое имя файла или другую папку "
                "(возможно, файл занят другой программой).",
            )
            return False

        self._last_dir = os.path.dirname(os.path.abspath(path)) or self._last_dir
        # Успешное сохранение: временная копия этого документа больше не нужна.
        self._on_user_save_succeeded(path)
        self._refresh_title()
        self._update_status()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Копия сохранена")
        box.setText(f"Файл сохранён:\n{path}")
        box.setInformativeText(SAVE_HINT)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        self.show_status(f"Копия сохранена: {os.path.basename(path)}")
        self._show_document_warnings()
        return True

    # ------------------------------------------------------------------
    # Правка страниц
    # ------------------------------------------------------------------
    def _after_mutation(self, *, pages_changed: bool = False) -> None:
        if pages_changed:
            self._field_counts.clear()
            self._page_index = self._clamp_index(self._page_index)
            self._rebuild_page_list()
        self._render_page()
        self._update_actions()
        self._refresh_title()
        self._refresh_forms_badge()
        if self._forms_dialog is not None and self._forms_dialog.isVisible():
            self._forms_dialog.reload()

    def _on_rotate(self, clockwise: bool) -> None:
        if self._session is None or self.page_count == 0:
            return
        try:
            with _busy_cursor():
                self._session.rotate(self._page_index, clockwise=clockwise)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось повернуть страницу", exc)
            return
        self._after_mutation()

    def _on_delete_page(self) -> None:
        if self._session is None or self.page_count == 0:
            return
        if self.page_count <= 1:
            self._report_message(
                "Нельзя удалить страницу",
                "В документе осталась одна страница — удалить её нельзя.",
            )
            return
        number = self._page_index + 1
        answer = QMessageBox.question(
            self,
            "Удалить страницу",
            f"Удалить страницу {number}?\n\nДействие можно отменить через Ctrl+Z.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with _busy_cursor():
                self._session.delete_page(self._page_index)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось удалить страницу", exc)
            return
        if self._page_index >= self.page_count:
            self._page_index = self.page_count - 1
        self._after_mutation(pages_changed=True)

    def _on_move_page(self, delta: int) -> None:
        if self._session is None or self.page_count < 2:
            return
        target = self._page_index + int(delta)
        if target < 0 or target >= self.page_count:
            return
        try:
            self._session.move_page(self._page_index, target)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось переместить страницу", exc)
            return
        self._page_index = target
        self._after_mutation(pages_changed=True)

    def _on_undo(self) -> None:
        if self._session is None or self.page_count == 0:
            return
        try:
            done = bool(self._session.undo())
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось отменить действие", exc)
            return
        if not done:
            self.show_status("Отменять нечего.")
            return
        self._field_counts.clear()
        self._page_index = self._clamp_index(self._page_index)
        self._clear_selection()
        self._refresh_all()
        self.show_status("Действие отменено.")

    def _on_redo(self) -> None:
        if self._session is None or self.page_count == 0:
            return
        try:
            done = bool(self._session.redo())
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось вернуть действие", exc)
            return
        if not done:
            self.show_status("Возвращать нечего.")
            return
        self._field_counts.clear()
        self._page_index = self._clamp_index(self._page_index)
        self._clear_selection()
        self._refresh_all()
        self.show_status("Действие возвращено.")

    # ------------------------------------------------------------------
    # Формы
    # ------------------------------------------------------------------
    def apply_form_values(self, items: Sequence[tuple[str, Any]]) -> tuple[int, int, Optional[BaseException]]:
        """Записывает значения полей формы. Возвращает (применено, ошибок, ошибка)."""
        if self._session is None:
            return (0, len(items), RuntimeError("Модуль работы с PDF недоступен."))
        applied = 0
        failed = 0
        last_error: Optional[BaseException] = None
        for key, value in items:
            text_key = str(key)
            try:
                # Если для поля есть отдельное значение экспорта (например,
                # служебная строка списка), в документ уходит именно оно.
                self._session.set_form_value(text_key, self._export_value_for(text_key, value))
                applied += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                last_error = exc
        if applied:
            self._field_counts.pop(self._page_index, None)
            self._render_page()  # страница заново получает поля формы
            self._refresh_title()
            self._refresh_forms_badge()
        return (applied, failed, last_error)

    def _on_forms(self) -> None:
        """Открывает правую панель полей и включает режим «Формы»."""
        if self._session is None:
            return
        if self.page_count == 0:
            self._report_message("Нет документа", "Сначала откройте PDF-файл.")
            return
        if self._forms_dialog is None:
            return
        self._forms_dialog.reload()
        if self._forms_dock is not None:
            self._forms_dock.show()
            self._forms_dock.raise_()
        if self._mode != MODE_FILL:
            self._on_mode_selected(MODE_FILL)

    # ------------------------------------------------------------------
    # Перетаскивание файлов
    # ------------------------------------------------------------------
    def _dropped_paths(self, event: Any) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if _is_pdf_url(url)]

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        if self.page_count == 0:
            self.open_path(paths[0])
            if len(paths) > 1:
                self.append_files(paths[1:])
            return

        if len(paths) == 1:
            question = f"Добавить файл «{os.path.basename(paths[0])}» к текущему документу?"
        else:
            question = f"Добавить файлов к текущему документу: {len(paths)}?"
        answer = QMessageBox.question(
            self,
            "Добавить страницы",
            question + "\n\nТекущий документ останется открытым.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.append_files(paths)

    # ------------------------------------------------------------------
    # Прочее
    # ------------------------------------------------------------------
    def _on_about(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(f"О программе {APP_NAME}")
        box.setText(f"{APP_NAME} — просмотр и правка PDF-файлов.")
        box.setInformativeText(
            "Можно открывать документы, добавлять страницы из других файлов, "
            "поворачивать и удалять их, ставить текстовые заметки, выделять "
            "фрагменты и рисовать от руки, а также заполнять поля формы.\n\n"
            f"{SAVE_HINT}\n\n"
            "Исходный файл никогда не изменяется: результат сохраняется как копия."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    # ------------------------------------------------------------------
    # Авто-восстановление (temporary snapshots)
    # ------------------------------------------------------------------
    def set_recovery_interval_ms(self, interval_ms: int) -> None:
        """Период авто-снимков в мс (0 — выключить)."""
        try:
            value = max(0, int(interval_ms))
        except (TypeError, ValueError):
            value = RECOVERY_INTERVAL_MS
        self._recovery_interval_ms = value
        timer = self._recovery_timer
        if timer is None:
            return
        if value:
            timer.setInterval(value)
            timer.start()
        else:
            timer.stop()

    def _recovery_root_path(self) -> Path:
        """Каталог восстановления (свой, отдельный от исходных файлов)."""
        if self._recovery_root is None:
            self._recovery_root = Path(recovery_module.default_recovery_root())
        return self._recovery_root

    def _recovery_supported(self) -> bool:
        """Умеет ли текущая сессия отдавать байты PDF (заглушки — нет)."""
        session = self._session
        if session is None:
            return False
        return all(
            callable(getattr(session, name, None))
            for name in (
                "_attach_annotations",
                "_attach_text_layer",
                "_write_bytes",
                "_detach_text_layer",
                "_detach_annotations",
            )
        )

    def _session_source_paths(self) -> list[str]:
        paths = getattr(self._session, "source_paths", None)
        if not paths:
            return []
        return [str(item) for item in paths if str(item or "").strip()]

    def _session_revision(self) -> Optional[int]:
        try:
            return int(getattr(self._session, "_revision", -1))
        except (TypeError, ValueError):
            return None

    def _current_recovery_key(self) -> Optional[str]:
        """Ключ документа: у восстановленной копии — из метаданных."""
        if self._recovery_key:
            return self._recovery_key
        paths = self._recovery_source_paths or self._session_source_paths()
        if not paths:
            return None
        try:
            return recovery_module.document_key(paths)
        except Exception:  # noqa: BLE001 - ключ не должен ломать интерфейс
            return None

    def _snapshot_recovery(self, *, force: bool = False) -> Optional[str]:
        """Записать temporary-снимок. None — снимок не потребовался или не удался.

        Документ при этом не помечается сохранённым: сессия остаётся «грязной»,
        ``saved_path`` не меняется, исходный PDF только читается.
        """
        if not self._recovery_enabled or not self._recovery_supported():
            return None
        if self.page_count <= 0 or not self._recovery_root_is_safe():
            return None
        if not force and not self.is_dirty():
            return None
        key = self._current_recovery_key()
        if not key:
            return None
        revision = self._session_revision()
        if not force and revision is not None and revision == self._recovery_snapshot_revision:
            return None
        try:
            record = recovery_module.snapshot(
                self._session,
                root=self._recovery_root_path(),
                key=key,
                source_paths=self._recovery_source_paths or self._session_source_paths(),
                saved_path=getattr(self._session, "saved_path", None),
                copy_path=self._recovery_copy_path,
                revision=revision,
            )
        except Exception as exc:  # noqa: BLE001 - таймер не должен ронять окно
            self._note_error("Авто-восстановление", exc)
            self.show_status(f"Временную копию сохранить не удалось: {_error_text(exc)}")
            return None
        self._recovery_snapshot_revision = revision
        self._recovery_last_path = str(record.document_path)
        if force:
            self.show_status(
                "Сохранена временная копия для восстановления: "
                f"{os.path.basename(str(record.document_path))}"
            )
        return str(record.document_path)

    def _recovery_root_is_safe(self) -> bool:
        """Каталог восстановления не должен совпадать с папкой исходников."""
        try:
            root = self._recovery_root_path()
        except Exception:  # noqa: BLE001
            return False
        for path in self._session_source_paths():
            try:
                if _same_path(str(root), path):
                    return False
            except Exception:  # noqa: BLE001
                continue
        return True

    def _remove_recovery_for_current_document(self) -> bool:
        """Удалить временную копию текущего документа (только внутри своего каталога)."""
        key = self._recovery_key or self._current_recovery_key()
        if not key or self._recovery_root is None:
            return False
        try:
            removed = recovery_module.remove_record(self._recovery_root, key)
        except Exception as exc:  # noqa: BLE001
            self._note_error("Авто-восстановление", exc)
            return False
        if removed:
            self._recovery_snapshot_revision = None
            self._recovery_last_path = None
        return removed

    def _release_recovery_on_leave(self) -> None:
        """Документ закрывают или заменяют: решаем судьбу временной копии.

        Если пользователь отказался сохранять изменения — копия удаляется
        (осознанный отказ). Восстановленную, но ещё не сохранённую копию
        не удаляем: её нельзя терять до успешного сохранения.
        """
        if self._recovery_restored and not self.is_dirty():
            return
        self._remove_recovery_for_current_document()
        self._recovery_key = None
        self._recovery_copy_path = None
        self._recovery_source_paths = []
        self._recovery_snapshot_revision = None

    def _on_user_save_succeeded(self, path: str) -> None:
        """Пользователь успешно сохранил копию — временный снимок больше не нужен."""
        self._recovery_restored = False
        self._recovery_snapshot_revision = self._session_revision()
        self._remove_recovery_for_current_document()
        self._recovery_last_path = None

    def _recovery_contains(self, path: str) -> bool:
        """True, если путь ведёт внутрь нашего каталога восстановления."""
        if self._recovery_root is None:
            return False
        try:
            return recovery_module.is_inside_root(self._recovery_root, path)
        except Exception:  # noqa: BLE001
            return False

    def _on_recovery_timer(self) -> None:
        try:
            self._snapshot_recovery()
        except Exception as exc:  # noqa: BLE001 - слот таймера не должен падать
            self._note_error("Авто-восстановление", exc)

    def _offer_recovery_on_startup(self) -> None:
        try:
            self.offer_recovery()
        except Exception as exc:  # noqa: BLE001
            self._note_error("Восстановление", exc)

    def offer_recovery(self) -> bool:
        """Предложить восстановление копий, оставшихся после сбоя.

        True — копия открыта. Копию НЕ удаляем: она исчезнет только после
        успешного сохранения пользователем.
        """
        if not self._recovery_enabled or self.page_count > 0:
            return False
        try:
            records = recovery_module.load_records(self._recovery_root_path())
        except Exception as exc:  # noqa: BLE001
            self._note_error("Восстановление", exc)
            return False
        if not records:
            return False
        record = records[0]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Восстановление после сбоя")
        box.setText(
            "Найдена временная копия несохранённого документа:\n"
            f"{record.source_label} — {record.metadata.get('created_at') or 'без даты'}"
        )
        details = [
            f"Страниц: {record.page_count}",
            f"Файл копии: {record.document_path}",
        ]
        if len(records) > 1:
            details.append(f"Всего копий: {len(records)} (показана последняя).")
        details.append("Открыть копию? Она останется на диске до успешного сохранения.")
        box.setInformativeText("\n".join(details))
        restore_button = box.addButton("Открыть копию", QMessageBox.ButtonRole.AcceptRole)
        delete_button = box.addButton("Удалить копию", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restore_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restore_button:
            return self.restore_recovery(record)
        if clicked is delete_button:
            try:
                recovery_module.remove_record(self._recovery_root_path(), record.key)
            except Exception as exc:  # noqa: BLE001
                self._note_error("Восстановление", exc)
                return False
            self.show_status("Временная копия удалена.")
        return False

    def restore_recovery(self, record: Any) -> bool:
        """Открыть копию восстановления (файл копии не изменяется и не удаляется)."""
        path = str(getattr(record, "document_path", "") or "")
        if not path or not os.path.isfile(path):
            self._report_message(
                "Копия не найдена",
                f"Не удалось открыть временную копию:\n{path}",
                QMessageBox.Icon.Critical,
            )
            return False
        key = str(getattr(record, "key", "") or "")
        source_paths = [str(item) for item in getattr(record, "source_paths", []) or []]
        if not self.open_path(path):
            return False
        # Восстановленный документ остаётся привязанным к своему ключу, чтобы
        # успешное сохранение убрало именно эту временную копию.
        self._recovery_key = key or None
        self._recovery_copy_path = path
        self._recovery_source_paths = source_paths
        self._recovery_restored = True
        self._recovery_snapshot_revision = self._session_revision()
        self.show_status(
            f"Открыта временная копия: {os.path.basename(path)}. "
            "Сохраните её под новым именем — исходный файл не перезаписывается."
        )
        return True

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - имя из Qt
        if self._forms_dialog is not None:
            self._forms_dialog.close()
        if not self._guard_dirty("закрыть программу"):
            event.ignore()
            return
        # Штатное закрытие: при отказе от сохранения временная копия удаляется,
        # а при сбое (закрытие не дошло до этой точки) — остаётся на диске.
        self._release_recovery_on_leave()
        event.accept()


def main() -> int:
    """Запуск окна без аргументов (используется для проверки интерфейса)."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return int(app.exec())


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
