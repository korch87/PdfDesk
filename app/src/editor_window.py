# -*- coding: utf-8 -*-
"""PdfDesk — новый интерфейс редактора (пересборка после отклонения макета).

Окно собрано заново по утверждённому макету
``outputs/design/pdfdesk-concept.html`` и контракту ``docs/rebuild-contract.txt``:
прежняя оболочка ``main_window.py`` не наследуется и не переиспользуется.

Раскладка (1440x1000, значения из макета):

* 44 px — строка документов: бренд PdfDesk, кнопка миниатюр, настоящие вкладки
  документов, сведения о документе;
* 56 px — режимы «Просмотр / Заполнение / Пометки / Редактирование / Страницы»
  и справа иконки: отмена, повтор, масштаб, страница целиком, линейки, поиск,
  печать, настройки и «Сохранить»;
* 45 px — контекстные инструменты текущего режима; кнопка-«пин» раскрывает
  полную панель (группы «Пометки» и «Документ») как в макете;
* 188 px — миниатюры страниц (номер ПОД миниатюрой), центр — документ,
  248 px — контекстные свойства и резервная панель полей формы;
* 31 px — строка состояния.

Принципы, заложенные в код:

* только собственные иконки SVG из макета (:mod:`src.editor_icons`), без эмодзи
  и системных «стоковых» значков;
* честные действия: сохраняется КОПИЯ (исходный PDF никогда не
  перезаписывается), несохранённые изменения защищены, а нереализованное
  (правка исходного содержимого, добавление страниц, поиск по исходному тексту)
  не показывается кнопками-заглушками, а остаётся в перечне задач ТЗ;
* вкладки — настоящие сессии: у каждой свои грязность, отмена/повтор,
  сохранение и временное восстановление;
* холст документа принадлежит другому модулю (``src/document_view.py``),
  поэтому импорт отложенный и защищённый: пока модуля нет, в центре честная
  заглушка с объяснением, а не поддельная страница.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib
import math
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal, QPoint, QThread, QSignalBlocker
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSplitter,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QMenu,
)

try:  # импорт как пакета: from src.editor_window import EditorWindow
    from . import editor_icons
    from . import editor_theme as theme
    from . import recovery as recovery_module
    from .contracts import Annotation
    from .pdf_backend import PdfSession
except ImportError:  # pragma: no cover - импорт при sys.path, указывающем на src/
    import editor_icons  # type: ignore[no-redef]
    import editor_theme as theme  # type: ignore[no-redef]
    import recovery as recovery_module  # type: ignore[no-redef]
    from contracts import Annotation  # type: ignore[no-redef]
    from pdf_backend import PdfSession  # type: ignore[no-redef]

PDF_FILTER = "PDF-документы (*.pdf)"
IMAGE_FILTER = "Изображения (*.png *.jpg *.jpeg *.webp)"
IMPORT_IMAGE_FILTER = "Изображения PNG/JPEG (*.png *.jpg *.jpeg)"
IMPORT_DOCUMENT_FILTER = (
    "Office и текстовые документы (*.docx *.doc *.xlsx *.xls *.pptx *.ppt *.txt);;"
    "Все файлы (*)"
)
#: Сколько страниц отрисовывается в миниатюры (защита от гигантских файлов).
MAX_THUMBNAILS = 80
#: Потолок разрешения печати, чтобы не «съесть» память на больших страницах.
MAX_PRINT_SCALE = 4.0

# ---------------------------------------------------------------------------
# Режимы, инструменты и подписи (русские подписи макета)
# ---------------------------------------------------------------------------
MODE_READ = "read"
MODE_FILL = "fill"
MODE_COMMENT = "comment"
MODE_EDIT = "edit"
MODE_PAGES = "pages"

TOOL_CURSOR = "cursor"
TOOL_TEXT = "text"
TOOL_SIGNATURE = "signature"
TOOL_STAMP = "stamp"
TOOL_FORMS = "forms"
TOOL_HIGHLIGHT = "highlight"
TOOL_NOTE = "note"
TOOL_PEN = "pen"
TOOL_SHAPES = "shapes"
TOOL_IMAGE = "image"
TOOL_RULER = "ruler"
TOOL_CONTENT = "content"
TOOL_FORM_AUTHOR = "form_author"
TOOL_SEARCH = "search"
TOOL_ROTATE = "rotate"
TOOL_TRASH = "trash"

#: Инструменты режима: (инструмент, подпись, иконка макета).
MODE_TOOLS: dict[str, tuple[tuple[str, str, str], ...]] = {
    MODE_READ: (
        (TOOL_CURSOR, "Выбор", "cursor"),
        (TOOL_SEARCH, "Поиск", "search"),
        ('fit_width','По ширине','fit'),
        ('jump_page','Перейти к странице','file'),
        ('rotate_view', 'Повернуть вид', 'rotate'),
    ),
    MODE_FILL: (
        (TOOL_CURSOR, "Поля формы", "cursor"),
        (TOOL_TEXT, "Текст", "text"),
        (TOOL_SIGNATURE, "Подпись", "signature"),
        (TOOL_STAMP, "Штамп", "stamp"),
    ),
    MODE_COMMENT: (
        (TOOL_CURSOR, "Выбор", "cursor"),
        (TOOL_HIGHLIGHT, "Выделение", "highlight"),
        (TOOL_NOTE, "Комментарий", "note"),
        (TOOL_PEN, "Карандаш", "pen"),
        (TOOL_SHAPES, "Фигуры", "shapes"),
    ),
    MODE_EDIT: (
        (TOOL_CONTENT, "Содержимое PDF", "text"),
        (TOOL_FORM_AUTHOR, "Создать форму", "check"),
        (TOOL_CURSOR, "Выбор", "cursor"),
        (TOOL_TEXT, "Текст", "text"),
        (TOOL_IMAGE, "Изображение", "image"),
        (TOOL_RULER, "Линейки", "ruler"),
    ),
    MODE_PAGES: (
        (TOOL_CURSOR, "Выбор", "cursor"),
        ('blank_page', 'Добавить', 'plus'),
        ('merge_pdf', 'Объединить', 'open'),
        ('page_back', 'Раньше', 'undo'),
        ('page_forward', 'Позже', 'redo'),
        (TOOL_ROTATE, "Повернуть", "rotate"),
        (TOOL_TRASH, "Удалить", "trash"),
    ),
}

#: Заголовок группы контекстных инструментов (макет ``#current-tools``).
MODE_TOOL_HEADING: dict[str, str] = {
    MODE_READ: "Инструменты",
    MODE_FILL: "Заполнение",
    MODE_COMMENT: "Инструменты",
    MODE_EDIT: "Правка",
    MODE_PAGES: "Инструменты",
}

#: Полная панель (раскрывается «пином»): группы макета ``.full-only``.
FULL_TOOL_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Пометки", (
        (TOOL_HIGHLIGHT, "Выделение", "highlight"),
        (TOOL_NOTE, "Комментарий", "note"),
        (TOOL_PEN, "Карандаш", "pen"),
        (TOOL_SHAPES, "Фигуры", "shapes"),
    )),
    ("Документ", (
        (TOOL_IMAGE, "Изображение", "image"),
        (TOOL_STAMP, "Штамп", "stamp"),
        (TOOL_ROTATE, "Поворот", "rotate"),
    )),
)

#: Инструмент окна → инструмент холста (контракт DocumentView.set_tool).
CANVAS_TOOLS: dict[str, str] = {
    TOOL_CURSOR: "select",
    TOOL_TEXT: "text",
    TOOL_SIGNATURE: "text",
    TOOL_HIGHLIGHT: "highlight",
    TOOL_NOTE: "note",
    TOOL_PEN: "ink",
    TOOL_SHAPES: "rect",
    TOOL_IMAGE: "image",
    TOOL_STAMP: "stamp",
}

#: Типы фигур панели «Фигуры» (значения — инструменты холста).
SHAPE_KINDS: tuple[tuple[str, str], ...] = (
    ("rect", "Прямоугольник"),
    ("ellipse", "Эллипс"),
    ("line", "Линия"),
    ("arrow", "Стрелка"),
    ("polygon", "Многоугольник"),
    ("cloud", "Облако"),
)

#: Подсказка в строке состояния после выбора инструмента (макет chooseTool).
TOOL_HINTS: dict[str, str] = {
    TOOL_TEXT: "Щёлкните на странице, чтобы добавить текст",
    TOOL_NOTE: "Щёлкните на странице, чтобы оставить комментарий",
    TOOL_STAMP: "Выберите изображение штампа и щёлкните по странице",
    TOOL_HIGHLIGHT: "Щёлкните на странице, чтобы отметить область",
    TOOL_PEN: "Рисуйте на странице, удерживая кнопку мыши",
    TOOL_SHAPES: "Выберите тип фигуры справа и протяните мышь по странице",
    TOOL_IMAGE: "Выберите изображение и щёлкните по странице",
    TOOL_SIGNATURE: "Выберите изображение подписи и щёлкните по странице",
    TOOL_CURSOR: "Выберите поле или объект на странице",
    TOOL_RULER: "Линейки включены",
    TOOL_CONTENT: "Щёлкните по исходному тексту или изображению; перетащите объект для перемещения",
    TOOL_ROTATE: "Поворот страницы сохраняется в PDF",
    TOOL_TRASH: "Удаление страницы с подтверждением",
    TOOL_SEARCH: "Поиск по полям формы и пометкам",
}

#: Честное напоминание о том, что ещё не реализовано (не кнопка-заглушка).
UNSUPPORTED_NOTES: dict[str, str] = {}

def shortcut_defaults_for_platform(platform_name: str | None = None) -> tuple[tuple[str, str, str], ...]:
    """Return portable default shortcuts with the platform-standard redo key."""
    host = str(platform_name or sys.platform).lower()
    # Qt's portable Ctrl modifier maps to Command on macOS unless
    # AA_MacDontSwapCtrlAndMeta is enabled (PdfDesk does not enable it).
    # Meta is the physical Control key on macOS, so keep Ctrl in the stored
    # PortableText representation on both platforms.
    modifier = "Ctrl"
    is_macos = host in {"darwin", "mac", "macos"}
    redo = f"{modifier}+Shift+Z" if is_macos else f"{modifier}+Y"
    return (
        ('new', 'Создать PDF', f'{modifier}+N'),
        ("open", "Открыть PDF", f"{modifier}+O"),
        ("save", "Сохранить копию", f"{modifier}+S"),
        ("save_as", "Сохранить как…", f"{modifier}+Shift+S"),
        ("close_tab", "Закрыть вкладку", f"{modifier}+W"),
        ("print", "Печать", f"{modifier}+P"),
        ("undo", "Отменить", f"{modifier}+Z"),
        ("redo", "Повторить", redo),
        ("search", "Поиск", f"{modifier}+F"),
        ("zoom_in", "Увеличить", f"{modifier}++"),
        ("zoom_out", "Уменьшить", f"{modifier}+-"),
        ("fit", "Страница целиком", f"{modifier}+0"),
        ("next_page", "Следующая страница", "PgDown"),
        ("prev_page", "Предыдущая страница", "PgUp"),
    )


#: Standard defaults use Command on macOS and Control on Windows/Linux.
SHORTCUT_DEFAULTS: tuple[tuple[str, str, str], ...] = shortcut_defaults_for_platform()

SETTINGS_ORG = "PdfDesk"
SETTINGS_APP = "PdfDesk"
SETTINGS_FILE_ENV = "PDFDESK_SETTINGS"


def settings_store() -> QSettings:
    """Хранилище настроек: ini-файл из PDFDESK_SETTINGS либо реестр пользователя."""
    path = os.environ.get(SETTINGS_FILE_ENV, "").strip()
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


# ---------------------------------------------------------------------------
# Служебные функции
# ---------------------------------------------------------------------------
def _same_path(first: Any, second: Any) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
            os.path.abspath(str(second))
        )
    except (TypeError, ValueError):
        return False


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def pages_label(count: int) -> str:
    """«1 страница», «2 страницы», «5 страниц» — как в макете."""
    count = int(count)
    if count % 10 == 1 and count % 100 != 11:
        return "%d страница" % count
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "%d страницы" % count
    return "%d страниц" % count


def size_label(width: float, height: float) -> str:
    """Подпись формата страницы: A4/A5/Letter либо размер в пунктах."""
    try:
        w = float(width)
        h = float(height)
    except (TypeError, ValueError):
        return ""
    for name, (pw, ph) in (("A4", (595.0, 842.0)), ("A5", (420.0, 595.0)),
                           ("A3", (842.0, 1191.0)), ("Letter", (612.0, 792.0))):
        if abs(w - pw) <= 3 and abs(h - ph) <= 3:
            return name
        if abs(w - ph) <= 3 and abs(h - pw) <= 3:
            return name + " (альбомная)"
    return "%d×%d pt" % (round(w), round(h))


def _pil_to_pixmap(image: Any) -> Optional[QPixmap]:
    """PIL.Image → QPixmap без зависимости от привязок PIL к Qt."""
    if image is None:
        return None
    try:
        rgb = image.convert("RGB")
    except Exception:  # noqa: BLE001 - неожиданный формат растра
        return None
    width, height = int(rgb.width), int(rgb.height)
    if width <= 0 or height <= 0:
        return None
    data = rgb.tobytes("raw", "RGB")
    qimage = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


def _dispose_widget(widget: Optional[QWidget]) -> None:
    """Убрать элемент управления, не создавая временное окно верхнего уровня.

    ``setParent(None)`` у показанного виджета делает его отдельным окном, а
    Windows при этом может передать активацию другому окну — из-за этого
    терялся фокус в поле, где пользователь печатает. Поэтому сначала скрываем.
    """
    if widget is None:
        return
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


# ---------------------------------------------------------------------------
# Отложенный (защищённый) импорт холста документа
# ---------------------------------------------------------------------------
_VIEW_CLASS: Any = None
_VIEW_ERROR: Optional[str] = None
_VIEW_TRIED = False


def document_view_class() -> Any:
    """Класс ``DocumentView`` агента холста; None, пока модуль не готов.

    Импорт отложенный: интерфейс обязан собираться и проверяться, пока
    ``src/document_view.py`` ещё пишется другим агентом. Ошибка импорта не
    прячется — :func:`document_view_status` отдаёт её текст для честной
    заглушки в центре окна.
    """
    global _VIEW_CLASS, _VIEW_ERROR, _VIEW_TRIED
    if _VIEW_TRIED:
        return _VIEW_CLASS
    _VIEW_TRIED = True
    package = __package__ or "src"
    attempts = ("%s.document_view" % package, "src.document_view", "document_view")
    for name in attempts:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - модуль может быть ещё не готов
            _VIEW_ERROR = "%s: %s" % (name, _error_text(exc))
            continue
        candidate = getattr(module, "DocumentView", None)
        if candidate is None:
            _VIEW_ERROR = "%s: нет класса DocumentView" % name
            continue
        _VIEW_CLASS = candidate
        _VIEW_ERROR = None
        return _VIEW_CLASS
    return None


def document_view_status() -> tuple[bool, str]:
    """(доступен ли холст, текст последней ошибки импорта)."""
    available = document_view_class() is not None
    return available, (_VIEW_ERROR or "")


# ---------------------------------------------------------------------------
# Простые элементы интерфейса
# ---------------------------------------------------------------------------
class SwitchButton(QAbstractButton):
    """Переключатель макета ``.switch`` (33x19, бирюзовый в положении «вкл»)."""

    WIDTH = 33
    HEIGHT = 19
    KNOB = 15

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self) -> QSize:  # noqa: N802 - API Qt
        return QSize(self.WIDTH, self.HEIGHT)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            track = QColor(theme.ACCENT if self.isChecked() else theme.SWITCH_OFF)
            painter.setBrush(track)
            painter.drawRoundedRect(
                QRectF(0.0, 0.0, float(self.WIDTH), float(self.HEIGHT)),
                self.HEIGHT / 2.0,
                self.HEIGHT / 2.0,
            )
            painter.setBrush(QColor("#ffffff"))
            left = self.WIDTH - self.KNOB - 2 if self.isChecked() else 2
            painter.drawEllipse(QRectF(float(left), 2.0, float(self.KNOB), float(self.KNOB)))
            if self.hasFocus():
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.ACCENT), 1.0))
                painter.drawRoundedRect(
                    QRectF(0.5, 0.5, self.WIDTH - 1.0, self.HEIGHT - 1.0), 9.0, 9.0
                )
        finally:
            painter.end()


class RulerWidget(QWidget):
    """Линейка с шагом 20 мм (макет: ``.ruler-top`` / ``.ruler-left``)."""

    MM_TO_PT = 72.0 / 25.4
    guide_dropped = Signal(str,QPoint)

    def __init__(self, orientation: Qt.Orientation, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._orientation = orientation
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedHeight(theme.RULER_THICKNESS)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setFixedWidth(theme.RULER_THICKNESS)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setObjectName("Ruler")
        self.setToolTip('Потяните с линейки на страницу, чтобы создать направляющую')

    def mousePressEvent(self,event) -> None:
        if event.button()==Qt.MouseButton.LeftButton:
            event.accept()

    def mouseReleaseEvent(self,event) -> None:
        if event.button()==Qt.MouseButton.LeftButton:
            axis='h' if self._orientation==Qt.Orientation.Horizontal else 'v'
            self.guide_dropped.emit(axis,event.globalPosition().toPoint())
            event.accept()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(theme.CHROME))
            painter.setPen(QPen(QColor(theme.RULER_LINE), 1.0))
            step = 20.0 * self.MM_TO_PT
            font = QFont(self.font())
            font.setPixelSize(8)
            painter.setFont(font)
            if self._orientation == Qt.Orientation.Horizontal:
                painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
                position = 0.0
                while position <= self.width():
                    x = int(round(position))
                    painter.drawLine(x, self.height() - 6, x, self.height() - 1)
                    painter.setPen(QColor(theme.RULER_TEXT))
                    painter.drawText(x + 2, 9, "%d" % round(position / self.MM_TO_PT))
                    painter.setPen(QPen(QColor(theme.RULER_LINE), 1.0))
                    position += step
            else:
                painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
                position = 0.0
                while position <= self.height():
                    y = int(round(position))
                    painter.drawLine(self.width() - 6, y, self.width() - 1, y)
                    position += step
        finally:
            painter.end()


class ThumbRow(QFrame):
    """Миниатюра страницы: изображение, а НОМЕР — под ним (как в макете)."""

    def __init__(self, index: int, pixmap: Optional[QPixmap],
                 panel: "ThumbnailPanel") -> None:
        super().__init__(panel)
        self.index = int(index)
        self.panel = panel
        self.setObjectName("ThumbRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("active", "false")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)
        self.image = QLabel()
        self.image.setObjectName("ThumbImage")
        self.image.setFixedSize(theme.THUMB_WIDTH, theme.THUMB_HEIGHT)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if pixmap is not None and not pixmap.isNull():
            self.image.setPixmap(
                pixmap.scaled(
                    theme.THUMB_WIDTH - 2,
                    theme.THUMB_HEIGHT - 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(self.image, 0, Qt.AlignmentFlag.AlignHCenter)
        self.number = QLabel("%d" % (self.index + 1))
        self.number.setObjectName("ThumbNumber")
        self.number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.number, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_active(self, active: bool) -> None:
        value = "true" if active else "false"
        for widget in (self, self.number):
            widget.setProperty("active", value)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.page_clicked(self.index)
        super().mousePressEvent(event)


class PageCard(QFrame):
    """Карточка страницы в режиме «Страницы» (макет ``.grid-page``)."""

    def __init__(self, index: int, pixmap: Optional[QPixmap], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.index = int(index)
        self.setObjectName("PageCard")
        self.setProperty("active", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.image = QLabel()
        self.image.setObjectName("ThumbImage")
        self.image.setFixedSize(theme.THUMB_WIDTH, theme.THUMB_HEIGHT)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if pixmap is not None and not pixmap.isNull():
            self.image.setPixmap(
                pixmap.scaled(
                    theme.THUMB_WIDTH - 2,
                    theme.THUMB_HEIGHT - 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(self.image, 0, Qt.AlignmentFlag.AlignHCenter)
        self.label = QLabel("Страница %d" % (self.index + 1))
        self.label.setObjectName("PageCardLabel")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self._host: Optional[Any] = None

    def set_active(self, active: bool) -> None:
        value = "true" if active else "false"
        for widget in (self, self.label):
            widget.setProperty("active", value)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if self._host is not None and event.button() == Qt.MouseButton.LeftButton:
            self._host.page_clicked(self.index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if self._host is not None and event.button() == Qt.MouseButton.LeftButton:
            self._host.page_activated(self.index)
        super().mouseDoubleClickEvent(event)


class ThumbnailPanel(QFrame):
    """Левая панель миниатюр: заголовок «СТРАНИЦЫ» и список страниц."""

    def __init__(self, window: "EditorWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("Sidebar")
        self.setFixedWidth(theme.SIDEBAR_WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QWidget()
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(15, 13, 15, 8)
        head_layout.setSpacing(4)
        heading = QLabel("СТРАНИЦЫ")
        heading.setProperty("role", "sidebar-heading")
        head_layout.addWidget(heading)
        head_layout.addStretch(1)
        self.page_badge = QLabel("")
        self.page_badge.setProperty("role", "tiny")
        head_layout.addWidget(self.page_badge)
        outer.addWidget(head)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ThumbScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._host = QWidget()
        self._host.setObjectName("ThumbHost")
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(0, 0, 0, 8)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        self.scroll.setWidget(self._host)
        outer.addWidget(self.scroll, 1)

        self.bottom = QLabel("Документ не открыт")
        self.bottom.setObjectName("SideBottom")
        self.bottom.setWordWrap(True)
        self.bottom.setContentsMargins(16, 0, 16, 16)
        outer.addWidget(self.bottom)

        self.rows: list[ThumbRow] = []
        self._active = -1

    def page_clicked(self, index: int) -> None:
        self.window.goto_page(index)

    def rebuild(self, thumbnails: Sequence[Optional[QPixmap]], active: int,
                title: str, meta: str) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)
        self.rows = []
        for index, pixmap in enumerate(thumbnails):
            row = ThumbRow(index, pixmap, self)
            self._layout.insertWidget(self._layout.count() - 1, row)
            self.rows.append(row)
        self.page_badge.setText(str(len(thumbnails)) if thumbnails else "")
        self.bottom.setText("%s\n%s" % (title, meta) if title else meta)
        self.set_active(active)

    def set_active(self, index: int) -> None:
        self._active = int(index)
        for row in self.rows:
            row.set_active(row.index == self._active)
        if 0 <= self._active < len(self.rows):
            self.scroll.ensureWidgetVisible(self.rows[self._active], 0, 24)


class PagesGrid(QWidget):
    """Сетка страниц режима «Страницы» (макет ``#pages-grid``)."""

    COLUMNS = 3

    def __init__(self, window: "EditorWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("PagesGrid")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 32)
        outer.setSpacing(18)
        self._grid = QWidget()
        self._grid_layout = QVBoxLayout(self._grid)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(0)
        outer.addWidget(self._grid, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self.cards: list[PageCard] = []
        self.note = QLabel("")
        self.note.setProperty("role", "tiny")
        self.note.setWordWrap(True)
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.note)

    def page_clicked(self, index: int) -> None:
        self.window.goto_page(index)
        self.set_active(index)

    def page_activated(self, index: int) -> None:
        self.window.goto_page(index)
        self.window.set_mode(MODE_READ)

    def rebuild(self, thumbnails: Sequence[Optional[QPixmap]], active: int) -> None:
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            child = item.widget()
            if child is not None:
                _dispose_widget(child)
            elif item.layout() is not None:
                sub = item.layout()
                while sub.count():
                    sub_item = sub.takeAt(0)
                    widget = sub_item.widget()
                    if widget is not None:
                        _dispose_widget(widget)
                sub.deleteLater()
        self.cards = []
        row_layout: Optional[QHBoxLayout] = None
        for index, pixmap in enumerate(thumbnails):
            if index % self.COLUMNS == 0:
                row_layout = QHBoxLayout()
                row_layout.setSpacing(26)
                self._grid_layout.addLayout(row_layout)
            card = PageCard(index, pixmap, self._grid)
            card._host = self
            row_layout.addWidget(card)  # type: ignore[union-attr]
            self.cards.append(card)
        self.set_active(active)

    def set_active(self, index: int) -> None:
        for card in self.cards:
            card.set_active(card.index == int(index))

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))


class ToastLabel(QLabel):
    """Всплывающее сообщение снизу по центру (макет ``#toast``)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, timeout_ms: int = theme.TOAST_MS) -> None:
        if not text:
            self.hide()
            return
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(max(500, int(timeout_ms)))

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = self.width()
        height = self.height()
        self.move(max(0, (parent.width() - width) // 2), max(0, parent.height() - height - 50))


class SearchResultRow(QFrame):
    """Строка результата поиска: нажатие переводит к полю или пометке."""

    def __init__(self, result: dict, handler: Callable[[dict], Any],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.result = dict(result)
        self._handler = handler
        self.setObjectName("SearchResult")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        title = QLabel(str(self.result.get("title") or ""))
        title.setObjectName("SearchTitle")
        title.setWordWrap(True)
        meta = QLabel(str(self.result.get("meta") or ""))
        meta.setObjectName("SearchMeta")
        layout.addWidget(title)
        layout.addWidget(meta)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if event.button() == Qt.MouseButton.LeftButton:
            self._handler(self.result)
        super().mousePressEvent(event)


class FieldNameLabel(QLabel):
    """Техническое имя поля из PDF: щелчок переводит к полю на странице."""

    def __init__(self, key: str, handler: Callable[[str], Any],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(key, parent)
        self.field_key = key
        self._handler = handler
        self.setProperty("role", "property")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Перейти к полю «%s» на странице" % key)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if event.button() == Qt.MouseButton.LeftButton:
            self._handler(self.field_key)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Панель свойств и полей формы
# ---------------------------------------------------------------------------
#: Наименьшая прозрачность пометки: полностью невидимый объект нельзя ни
#: выбрать, ни вернуть, поэтому инспектор предлагает диапазон 5–100 %.
MIN_ANNOTATION_OPACITY = 0.05

#: Типы объектов, у которых ЗАЛИВКА холста и сохранённого /AP совпадают
#: (проверено попиксельным сравнением, см. docs/annotation-properties-verification.txt).
#: ``highlight`` исключён: ``fill_color`` у него не рисуется ни холстом, ни /AP.
FILL_EDIT_KINDS: tuple[str, ...] = ("rect", "ellipse", "polygon", "cloud")

#: Типы объектов, у которых ПРОЗРАЧНОСТЬ холста и сохранённого /AP совпадают.
#: ``ink`` исключён: /AP применяет /CA и /GS0 дважды (alpha² вместо alpha);
#: ``text`` — /FreeText игнорирует прозрачность при рендере; ``note`` — значок
#: рисуется фиксированными цветами.
OPACITY_EDIT_KINDS: tuple[str, ...] = (
    "rect", "ellipse", "polygon", "cloud", "highlight", "line", "arrow",
    "image", "stamp",
)

#: Какой тип объекта создаёт инструмент (для стиля новых объектов).
TOOL_ANNOTATION_KINDS: dict[str, str] = {
    TOOL_PEN: "ink",
    TOOL_HIGHLIGHT: "highlight",
    TOOL_TEXT: "text",
    TOOL_NOTE: "note",
    TOOL_IMAGE: "image",
    TOOL_STAMP: "stamp",
    TOOL_SIGNATURE: "image",
}


def annotation_opacity_value(value: Any, default: float = 1.0) -> float:
    """Прозрачность из модели как конечное число (без ограничения диапазона)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def normalize_annotation_opacity(value: Any) -> float:
    """Прозрачность для записи в PDF: конечное число в диапазоне 5–100 %."""
    number = annotation_opacity_value(value)
    return round(min(max(number, MIN_ANNOTATION_OPACITY), 1.0), 4)


def normalize_fill_color(value: Any) -> str:
    """Цвет заливки ``#rrggbb``; пустая строка — «без заливки»."""
    text = str(value or "").strip()
    if not text:
        return ""
    color = QColor(text)
    if not color.isValid():
        return ""
    return color.name()


def is_image_payload(data: bytes) -> bool:
    """Декодируется ли файл как изображение (PNG/JPEG и другие форматы Qt)."""
    if not data:
        return False
    return not QImage.fromData(data).isNull()


class InspectorPanel(QFrame):
    """Правая панель 248 px: свойства объектов, рисования и резервные поля формы."""

    def __init__(self, window: "EditorWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("Inspector")
        self.setFixedWidth(theme.INSPECTOR_WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._host = QWidget()
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(theme.PANEL_PADDING, theme.PANEL_PADDING + 1,
                                        theme.PANEL_PADDING, theme.PANEL_PADDING)
        self._host.setObjectName('InspectorHost')
        self._layout.setSpacing(0)
        # Высота содержимого — натуральная: иначе 79 полей анкеты сжимались в
        # высоту окна и подписи с полями накладывались друг на друга.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._layout.addStretch(1)
        self.scroll.setWidget(self._host)
        outer.addWidget(self.scroll, 1)
        self._all_pages = False
        self._editors: list[QWidget] = []
        self._signature: Optional[tuple] = None

    # -- построение -------------------------------------------------------
    def rebuild(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)
            elif item.layout() is not None:
                self._drop_layout(item.layout())
        self._editors = []
        tab = self.window.current_tab
        if self.window.mode in (MODE_READ, MODE_PAGES):
            # В макете справа нет свойств в режимах просмотра и страниц.
            self._signature = self.signature()
            self.setVisible(False)
            return
        self.setVisible(True)
        if tab is None or self.window.page_count == 0:
            self._add_heading("Свойства")
            self._add_sub("Документ не открыт.\nОткройте PDF, чтобы работать со "
                          "свойствами и полями формы.")
            self._signature = self.signature()
            return
        if self.window.mode == MODE_EDIT and self.window.tool == TOOL_FORM_AUTHOR:
            self._add_heading("Создание формы")
            self._add_sub("Выберите тип поля и нарисуйте его рамку на странице. "
                          "Для изменения поля включите «Выбор».")
            controller = getattr(tab, 'form_author', None)
            if controller is not None:
                from .form_author_ui import FormAuthorPanel
                self._insert(FormAuthorPanel(controller), 8)
            self._signature = self.signature()
            return
        if self.window.mode == MODE_EDIT and self.window.tool == TOOL_CONTENT:
            self._build_source_content_panel(tab)
            self._signature = self.signature()
            return
        if self.window.mode == MODE_FILL and self.window.tool in (TOOL_CURSOR, TOOL_FORMS):
            self._build_forms_panel(tab)
        else:
            self._build_object_panel(tab)
        self._signature = self.signature()

    def signature(self) -> tuple:
        """Отпечаток структуры панели: пока он не менялся, пересборка не нужна.

        Значения полей в отпечаток не входят: иначе набор текста на странице
        пересобирал бы панель и отбирал фокус у активного поля.
        """
        tab = self.window.current_tab
        if tab is None:
            return ("empty",)
        selection = self.window.selected_annotation()
        controller = getattr(tab, "source_controller", None)
        source_id = getattr(controller, "selected_id", None)
        kinds: tuple = ()
        if self.window.page_count:
            try:
                kinds = tuple(annotation.kind for annotation in
                              tab.session.get_annotations(tab.page_index))
            except Exception:  # noqa: BLE001
                kinds = ()
        try:
            form_keys = tuple(row["key"] for row in self.window.form_rows(
                all_pages=self._all_pages))
        except Exception:  # noqa: BLE001
            form_keys = ()
        return (
            self.window.mode,
            self.window.tool,
            tab.page_index,
            self._all_pages,
            -1 if selection is None else selection[1],
            source_id,
            kinds,
            form_keys,
        )

    def refresh(self) -> None:
        """Обновить панель после изменения документа, не мешая вводу."""
        if self.signature() != self._signature:
            self.rebuild()
        else:
            self.sync_values()

    def sync_values(self) -> None:
        """Перенести значения модели в уже показанные поля (без пересборки)."""
        tab = self.window.current_tab
        if tab is None or not self._editors:
            return
        session = self.window._session
        if session is None:
            return
        values: dict[str, Any] = {}
        for row in self.window.form_rows(all_pages=self._all_pages):
            values[row["key"]] = row.get("value")
        for editor in self._editors:
            key = editor.property("pd_field_key")
            if not key or key not in values:
                continue
            value = values[key]
            blocked = editor.blockSignals(True)
            try:
                if isinstance(editor, QLineEdit):
                    text = str(value if value is not None else "")
                    if editor.text() != text:
                        editor.setText(text)
                elif isinstance(editor, QPlainTextEdit):
                    text = str(value if value is not None else "")
                    if editor.toPlainText() != text:
                        editor.setPlainText(text)
                elif isinstance(editor, QCheckBox):
                    editor.setChecked(bool(value))
                elif isinstance(editor, QComboBox):
                    index = editor.findData(value)
                    if index < 0:
                        index = editor.findText(str(value))
                    if index >= 0 and editor.currentIndex() != index:
                        editor.setCurrentIndex(index)
            finally:
                editor.blockSignals(blocked)

    def _drop_layout(self, layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)
        layout.deleteLater()

    # -- мелкие строители -------------------------------------------------
    def _insert(self, widget: QWidget, spacing: int = 0) -> QWidget:
        self._layout.insertWidget(self._layout.count() - 1, widget)
        if spacing:
            spacer = QWidget()
            spacer.setFixedHeight(spacing)
            self._layout.insertWidget(self._layout.count() - 1, spacer)
        return widget

    def _add_heading(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "heading")
        return self._insert(label)  # type: ignore[return-value]

    def _add_sub(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "sub")
        label.setWordWrap(True)
        return self._insert(label, 16)  # type: ignore[return-value]

    def _add_property(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "property")
        return self._insert(label, 6)  # type: ignore[return-value]

    def _add_hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "hint")
        label.setWordWrap(True)
        return self._insert(label, 18)  # type: ignore[return-value]

    # -- панель полей формы (резервный способ, технические имена) ---------
    def _build_source_content_panel(self, tab: "EditorTab") -> None:
        self._add_heading("Содержимое PDF")
        controller = getattr(tab, "source_controller", None)
        if controller is not None and getattr(controller, "in_nested_content", False):
            back = QPushButton("К группе PDF")
            back.setObjectName("ExitNestedContent")
            back.clicked.connect(controller.exit_nested_content)
            self._insert(back, 6)
            self._add_hint("Правка относится только к этому экземпляру группы. "
                           "Размеры и шрифт задаются внутри выбранного блока.")
        group = getattr(controller, "selected_form_occurrence", None)
        if group is not None:
            self._add_sub("Вложенная группа PDF. Можно изменить этот экземпляр "
                          "целиком или перейти к его тексту и изображениям.")
            enter = QPushButton("Редактировать содержимое группы")
            enter.setObjectName("EnterNestedContent")
            enter.clicked.connect(controller.enter_nested_content)
            self._insert(enter, 6)
            self._add_property("МАСШТАБ ГРУППЫ")
            dimensions = []
            for name, label in (("FormGroupScaleX", "По ширине"),
                                ("FormGroupScaleY", "По высоте")):
                self._insert(QLabel(label))
                spin = QDoubleSpinBox()
                spin.setObjectName(name)
                spin.setRange(1, 10000)
                spin.setDecimals(2)
                spin.setSuffix(" %")
                spin.setValue(100)
                self._insert(spin, 6)
                dimensions.append(spin)
            linked = QCheckBox("Сохранять пропорции")
            linked.setChecked(True)
            self._insert(linked, 6)
            def link_scale(value, other):
                if linked.isChecked():
                    with QSignalBlocker(other):
                        other.setValue(value)
            dimensions[0].valueChanged.connect(lambda value: link_scale(value, dimensions[1]))
            dimensions[1].valueChanged.connect(lambda value: link_scale(value, dimensions[0]))
            apply = QPushButton("Изменить размер группы")
            apply.setObjectName("ResizeFormGroup")
            apply.clicked.connect(lambda: controller.resize_selected_form_group(
                dimensions[0].value() / 100, dimensions[1].value() / 100))
            self._insert(apply, 6)
            remove = QPushButton("Удалить этот экземпляр")
            remove.setObjectName("DeleteFormGroup")
            remove.setProperty("role", "danger")
            remove.clicked.connect(controller.delete_selected_form_group)
            self._insert(remove, 6)
            self._add_hint("Другие экземпляры этой группы в документе сохраняются. "
                           "Ctrl+Z — отменить изменение.")
            return
        block = (getattr(controller, "selected_local_block", None) or controller.selected_block
                 if controller is not None else None)
        if block is None:
            self._add_sub("Щёлкните по исходному тексту или изображению на странице. "
                          "Перетащите выбранный объект, чтобы переместить его.")
            self._add_hint("Ctrl/Shift + щелчок объединяет текстовые строки в блок "
                           "на странице. Для вложенных объектов выберите группу "
                           "и перейдите к её содержимому.")
            return
        page_label = QLabel("Страница %d · %s" % (
            controller.selected_page + 1,
            "Текст" if block.kind == "text" else "Изображение"))
        page_label.setProperty("role", "tiny")
        self._insert(page_label, 10)
        if block.kind == "text":
            from .source_edit_ui import populate_rich_text_edit
            self._add_property("ИСХОДНЫЙ ТЕКСТ")
            editor = QTextEdit()
            populate_rich_text_edit(editor, block)
            editor.setObjectName("SourceTextEditor")
            editor.setFixedHeight(112)
            self._source_format_target = editor
            self._source_font_scale = 1.0
            self._source_pixel_sizes = False
            editor.selectionChanged.connect(lambda editor=editor: self._set_source_format_target(editor))
            self._insert(editor, 7)

            self._add_property("ШРИФТ")
            fonts = self.window.font_families()
            family = QComboBox()
            family.setObjectName("SourceFont")
            for item in fonts:
                family.addItem(item)
            if family.findText(block.font_name) < 0 and block.font_name:
                family.insertItem(0, block.font_name)
            if block.font_name:
                family.setCurrentText(block.font_name)
            self._insert(family, 7)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(7)
            size = QDoubleSpinBox()
            size.setObjectName("SourceFontSize")
            size.setRange(0.1, 1000.0)
            size.setDecimals(1)
            size.setSuffix(" pt")
            size.setValue(max(0.1, min(1000.0, float(block.font_size or 12.0))))
            color = QLineEdit(block.color)
            color.setObjectName("SourceTextColor")
            color.setPlaceholderText("#RRGGBB")
            row_layout.addWidget(size, 1)
            row_layout.addWidget(color, 1)
            self._insert(row, 8)

            self._add_property("РАЗМЕР БЛОКА")
            dimensions = QWidget()
            dimension_layout = QHBoxLayout(dimensions)
            dimension_layout.setContentsMargins(0, 0, 0, 0)
            width = QDoubleSpinBox()
            width.setObjectName('SourceWidth')
            width.setRange(1, 100000)
            width.setValue(max(1, getattr(block, 'block_width', 0) or block.bbox[2] - block.bbox[0]))
            width.setToolTip('Ширина блока, пункты PDF')
            height = QDoubleSpinBox()
            height.setObjectName('SourceHeight')
            height.setRange(1, 100000)
            height.setValue(max(1, getattr(block, 'block_height', 0) or block.bbox[3] - block.bbox[1]))
            height.setToolTip('Высота блока, пункты PDF')
            dimension_layout.addWidget(width)
            dimension_layout.addWidget(height)
            self._insert(dimensions, 7)
            alignment = QComboBox()
            alignment.setObjectName('SourceAlignment')
            for caption, value in [('По левому краю', 'left'), ('По центру', 'center'),
                                   ('По правому краю', 'right')]:
                alignment.addItem(caption, value)
            alignment.setCurrentIndex(max(0, alignment.findData(getattr(block, 'alignment', 'left'))))
            self._insert(alignment, 7)
            bold = QCheckBox('Полужирный')
            bold.setObjectName('SourceBold')
            bold.setChecked(bool(getattr(block, 'bold', False)))
            self._insert(bold, 7)
            emphasis = QWidget()
            emphasis_layout = QHBoxLayout(emphasis)
            emphasis_layout.setContentsMargins(0, 0, 0, 0)
            italic = QCheckBox('Курсив')
            italic.setObjectName('SourceItalic')
            underline = QCheckBox('Подчеркнуть')
            underline.setObjectName('SourceUnderline')
            emphasis_layout.addWidget(italic)
            emphasis_layout.addWidget(underline)
            self._insert(emphasis, 7)
            family.currentTextChanged.connect(lambda value: self._format_source_selection(font_name=value))
            size.valueChanged.connect(lambda value: self._format_source_selection(font_size=value))
            color.editingFinished.connect(lambda: self._format_source_selection(color=color.text()))
            bold.toggled.connect(lambda value: self._format_source_selection(bold=value))
            italic.toggled.connect(lambda value: self._format_source_selection(italic=value))
            underline.toggled.connect(lambda value: self._format_source_selection(underline=value))
            self._add_hint('Выделите часть текста, чтобы изменить её оформление. Без выделения оформление применяется ко всему блоку.')

            apply = QPushButton("Применить")
            apply.setProperty("role", "primary")
            apply.setObjectName("ApplySourceText")
            apply.clicked.connect(lambda _checked=False, editor=editor, family=family,
                                  controller=controller, block=block,
                                  width=width, height=height, alignment=alignment:
                                  self._apply_source_rich_panel(controller, editor, block,
                                                               width.value(), height.value(),
                                                               alignment.currentData()))
            self._insert(apply, 7)
        if block.kind == 'image':
            if getattr(controller, 'in_nested_content', False):
                self._add_property('РАЗМЕР ИЗОБРАЖЕНИЯ')
                image_dimensions = []
                for name, label, value in (
                        ('NestedImageWidth', 'Ширина', block.bbox[2] - block.bbox[0]),
                        ('NestedImageHeight', 'Высота', block.bbox[3] - block.bbox[1])):
                    self._insert(QLabel(label))
                    dimension = QDoubleSpinBox()
                    dimension.setObjectName(name)
                    dimension.setRange(0.1, 10000)
                    dimension.setDecimals(2)
                    dimension.setSuffix(' pt')
                    dimension.setValue(value)
                    self._insert(dimension, 6)
                    image_dimensions.append(dimension)
                resize_image = QPushButton('Изменить размер изображения')
                resize_image.setObjectName('ResizeNestedImage')
                resize_image.clicked.connect(lambda: controller.apply_selected(
                    width=image_dimensions[0].value(), height=image_dimensions[1].value()))
                self._insert(resize_image, 6)
            replace_image = QPushButton('Заменить изображение…')
            replace_image.setObjectName('ReplaceSourceImage')
            replace_image.setProperty('role', 'primary')
            replace_image.clicked.connect(self.window.replace_source_image)
            self._insert(replace_image, 9)
            self._add_hint('Перетаскивайте объект для перемещения. Размер внутри группы задаётся выше.'
                           if getattr(controller, 'in_nested_content', False) else
                           'Перетаскивайте объект для перемещения и ручку в углу для изменения размера.')
        delete = QPushButton("Удалить объект")
        delete.setProperty("role", "danger")
        delete.setObjectName("DeleteSourceObject")
        delete.clicked.connect(controller.delete_selected)
        self._insert(delete, 9)
        bounds = QLabel("Размер: %.1f × %.1f pt" % (
            max(0.0, block.bbox[2] - block.bbox[0]),
            max(0.0, block.bbox[3] - block.bbox[1])))
        bounds.setProperty("role", "tiny")
        bounds.setWordWrap(True)
        self._insert(bounds, 4)

    def focus_content_text(self) -> None:
        editor = self.findChild(QTextEdit, "SourceTextEditor")
        if editor is not None:
            editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_source_format_target(self, editor: QTextEdit) -> None:
        self._source_format_target = editor
        try:
            cursor = QTextCursor(editor.textCursor())
            if cursor.hasSelection():
                cursor.setPosition(cursor.selectionStart())
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter,
                                    QTextCursor.MoveMode.KeepAnchor)
            char_format = cursor.charFormat()
            font = QFont(char_format.font())
            families = font.families()
            family = families[0] if families else ''
            scale = getattr(self, '_source_font_scale', 1.0)
            size = (font.pixelSize() / scale
                    if getattr(self, '_source_pixel_sizes', False) and font.pixelSize() > 0
                    else char_format.fontPointSize() / scale)
            controls = [self.findChild(QComboBox, 'SourceFont'),
                        self.findChild(QDoubleSpinBox, 'SourceFontSize'),
                        self.findChild(QLineEdit, 'SourceTextColor'),
                        self.findChild(QCheckBox, 'SourceBold'),
                        self.findChild(QCheckBox, 'SourceItalic'),
                        self.findChild(QCheckBox, 'SourceUnderline')]
            blockers = [QSignalBlocker(control) for control in controls if control is not None]
            family_control, size_control, color_control, bold, italic, underline = controls
            if family_control is not None and family:
                if family_control.findText(family) < 0:
                    family_control.addItem(family)
                family_control.setCurrentText(family)
            if size_control is not None and size > 0:
                size_control.setValue(size)
            if color_control is not None:
                color_control.setText(char_format.foreground().color().name().upper())
            for control, value in ((bold, font.bold()), (italic, font.italic()),
                                   (underline, font.underline())):
                if control is not None:
                    control.setChecked(value)
            del blockers
        except RuntimeError:
            return

    def bind_source_editor(self, inline: QTextEdit) -> None:
        """Show the unsaved paragraph without changing affine editor wrapping."""
        panel = self.findChild(QTextEdit, 'SourceTextEditor')
        if panel is not None:
            controller = getattr(self.window.current_tab, 'source_controller', None)
            affine = getattr(controller, '_inline_proxy', None) is not None
            if affine:
                panel.setDocument(inline.document().clone(panel))
                panel.setEnabled(False)

                def mirror_affine_text():
                    try:
                        panel.setDocument(inline.document().clone(panel))
                        self._set_source_format_target(inline)
                    except RuntimeError:
                        pass  # inspector rebuilt while the inline editor closes

                inline.textChanged.connect(mirror_affine_text)
                self._source_inline_mirror = (inline, mirror_affine_text)
            else:
                panel.setDocument(inline.document())
            self._source_font_scale = float(getattr(controller, 'inline_font_scale',
                getattr(inline.parentWidget(), 'zoom', 1.0)) or 1.0)
            self._source_pixel_sizes = True
            inline.selectionChanged.connect(lambda: self._set_source_format_target(inline))
            self._set_source_format_target(inline)

    def unbind_source_editor(self, inline: QTextEdit) -> None:
        panel = self.findChild(QTextEdit, 'SourceTextEditor')
        mirror = getattr(self, '_source_inline_mirror', None)
        affine = mirror is not None and mirror[0] is inline
        if affine:
            inline.textChanged.disconnect(mirror[1])
            self._source_inline_mirror = None
        if panel is not None and (affine or panel.document() is inline.document()):
            panel.setDocument(inline.document().clone(panel))
            panel.setEnabled(True)
            self._source_format_target = panel

    def _format_source_selection(self, **style: Any) -> None:
        from .source_edit_ui import apply_rich_format
        editor = getattr(self, '_source_format_target', None)
        if editor is None:
            return
        if 'color' in style and not QColor(str(style['color'])).isValid():
            self.window.show_status('Введите цвет в формате #RRGGBB')
            return
        if 'font_size' in style and getattr(self, '_source_pixel_sizes', False):
            style['font_pixel_size'] = max(1, round(float(style.pop('font_size')) * self._source_font_scale))
        try:
            apply_rich_format(editor, **style)
        except RuntimeError:
            return  # the inspector may have been rebuilt after a selection change

    def _apply_source_rich_panel(self, controller: Any, editor: QTextEdit, block: Any,
                                 width: float, height: float, alignment: str) -> None:
        from .source_edit_ui import extract_rich_spans
        active_inline = getattr(controller, '_inline_editor', None)
        if active_inline is not None:
            editor = active_inline
        defaults = {'font_name': block.font_name, 'font_size': block.font_size,
                    'color': block.color, 'bold': block.bold}
        text = editor.toPlainText()
        spans = extract_rich_spans(editor, defaults,
                                   font_scale=getattr(self, '_source_font_scale', 1.0),
                                   pixel_sizes=getattr(self, '_source_pixel_sizes', False))
        controller._close_inline(commit=False)
        controller.apply_selected(text=text, rich_spans=spans,
                                  width=width, height=height, alignment=alignment)

    # -- панель полей формы (резервный способ, технические имена) ---------
    def _build_forms_panel(self, tab: "EditorTab") -> None:
        count = len(self.window.form_rows(all_pages=True))
        self._add_heading("Поля формы")
        self._add_sub("Заполняйте на странице.\nЗдесь — все поля документа: "
                      "подписи берутся из PDF.")
        toggle = QCheckBox("Все поля документа (%d)" % count)
        toggle.setChecked(self._all_pages)
        toggle.setMinimumHeight(26)
        toggle.toggled.connect(self._on_all_pages)
        self._insert(toggle, 12)

        rows = self.window.form_rows(all_pages=self._all_pages)
        if not rows:
            self._add_property("ТЕКУЩАЯ СТРАНИЦА")
            empty = QLabel("На этой странице полей формы нет.")
            empty.setProperty("role", "tiny")
            empty.setWordWrap(True)
            self._insert(empty, 8)
        for row in rows:
            self._insert(self._field_group(row), 12)
        self._add_hint("Поле в этой панели — техническое имя из PDF. Щелчок по "
                       "имени переводит к тому же полю на странице; ввод здесь "
                       "сразу попадает в документ.")

    def _field_group(self, row: dict) -> QWidget:
        """Строка поля: имя из PDF, редактор и пояснение — своей высоты.

        Каждая строка — отдельный контейнер с минимальной высотой по
        содержимому, поэтому подписи и поля не накладываются даже при 79 полях.
        """
        group = QWidget()
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = FieldNameLabel(row["key"], self.window.focus_form_widget, group)
        label.setMinimumHeight(16)
        layout.addWidget(label)
        editor = self._form_editor(row)
        if editor is not None:
            editor.setParent(group)
            editor.setMinimumHeight(30 if not isinstance(editor, QPlainTextEdit) else 70)
            layout.addWidget(editor)
        meta = QLabel(self._form_meta(row), group)
        meta.setProperty("role", "tiny")
        meta.setWordWrap(True)
        meta.setMinimumHeight(14)
        layout.addWidget(meta)
        group.setMinimumHeight(layout.sizeHint().height())
        return group

    def _on_all_pages(self, checked: bool) -> None:
        self._all_pages = bool(checked)
        self.rebuild()

    def _form_meta(self, row: dict) -> str:
        kinds = {"text": "текстовое поле", "checkbox": "флажок", "radio": "переключатель",
                 "choice": "список", "unsupported": "неподдерживаемый тип"}
        parts = ["%s · стр. %d" % (kinds.get(row["kind"], row["kind"]), row["page"] + 1)]
        if row.get("count", 1) > 1:
            parts.append("виджетов: %d" % row["count"])
        if row.get("readonly"):
            parts.append("только чтение")
        if row.get("label") and row["label"] != row["key"]:
            parts.append(row["label"])
        return " · ".join(parts)

    def _form_editor(self, row: dict) -> Optional[QWidget]:
        key = row["key"]
        readonly = bool(row.get("readonly"))
        if row["kind"] == "unsupported":
            label = QLabel("Тип поля не поддерживается — значение не изменяется.")
            label.setProperty("role", "tiny")
            label.setWordWrap(True)
            return label
        if row["kind"] == "checkbox":
            editor = QCheckBox("Отмечено")
            editor.setChecked(bool(row.get("value")))
            editor.setEnabled(not readonly)
            editor.toggled.connect(
                lambda state, key=key: self.window.commit_form_value(key, bool(state))
            )
        elif row["kind"] in ("radio", "choice"):
            editor = QComboBox()
            editor.setEnabled(not readonly)
            options = list(row.get("options") or [])
            exports = list(row.get("exports") or [])
            for position, option in enumerate(options):
                editor.addItem(option, exports[position] if position < len(exports) else option)
            current = row.get("value")
            if current:
                index = editor.findData(current)
                if index < 0:
                    index = editor.findText(str(current))
                if index >= 0:
                    editor.setCurrentIndex(index)
            editor.currentIndexChanged.connect(
                lambda _index, key=key, editor=editor:
                self.window.commit_form_value(key, editor.currentData())
            )
        elif row["kind"] == "text" and row.get("multiline"):
            editor = QPlainTextEdit()
            editor.setPlainText(str(row.get("value") or ""))
            editor.setFixedHeight(70)
            editor.setEnabled(not readonly)
            editor.textChanged.connect(
                lambda key=key, editor=editor:
                self.window.commit_form_value(key, editor.toPlainText())
            )
        else:
            editor = QLineEdit(str(row.get("value") or ""))
            editor.setEnabled(not readonly)
            editor.textEdited.connect(
                lambda text, key=key: self.window.commit_form_value(key, text)
            )
        editor.setToolTip("Поле PDF: %s" % key)
        editor.installEventFilter(self)
        editor.setProperty("pd_field_key", key)
        self._editors.append(editor)
        return editor

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - API Qt
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            key = watched.property("pd_field_key")
            if key:
                # Фокус остаётся в панели (как в макете): страница лишь
                # прокручивается к этому полю, ввод идёт в оба места сразу.
                self.window.reveal_form_widget(str(key))
        return super().eventFilter(watched, event)

    # -- панель свойств объектов и рисования ------------------------------
    def _property_editable(self) -> bool:
        """Разрешена ли правка объектов в текущем документе (права PDF)."""
        return self.window.annotation_edit_allowed()

    def _shows_property(self, name: str) -> bool:
        """Показывать ли заливку/прозрачность в текущем контексте.

        Контекст — тип выбранного объекта либо тип, который создаст активный
        инструмент. Неизвестный контекст («Выбор» без выделения) показывает оба
        свойства, как и цвет с толщиной: они задают стиль следующих объектов.
        """
        context = self.window.style_context_kind()
        if not context:
            return True
        kinds = FILL_EDIT_KINDS if name == "fill" else OPACITY_EDIT_KINDS
        return context in kinds

    def _fill_row(self) -> QWidget:
        """Заливка: цвета палитры плюс явное «без заливки»."""
        style = self.window.effective_style()
        current = normalize_fill_color(style.get("fill_color"))
        editable = self._property_editable()
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        for value in theme.PALETTE:
            button = QPushButton()
            button.setProperty("role", "swatch")
            button.setProperty("pd_fill", value)
            button.setProperty("swatch", "on" if value.lower() == current else "off")
            button.setFixedSize(22, 22)
            button.setToolTip("Заливка %s" % value)
            button.setAccessibleName("Заливка %s" % value)
            button.setStyleSheet("background:%s;border-radius:11px;" % value)
            button.setEnabled(editable)
            button.clicked.connect(
                lambda _checked=False, value=value: self.window.apply_style("fill_color", value)
            )
            layout.addWidget(button)
        none_button = QPushButton()
        none_button.setObjectName("AnnotationFillNone")
        none_button.setProperty("role", "swatch")
        none_button.setProperty("pd_fill", "")
        none_button.setProperty("swatch", "on" if not current else "off")
        none_button.setFixedSize(22, 22)
        none_button.setToolTip("Без заливки")
        none_button.setAccessibleName("Без заливки")
        # Только фон: рамку и кольцо выбранного состояния рисует тема.
        none_button.setStyleSheet("background:#EEF1F4;border-radius:11px;")
        none_button.setEnabled(editable)
        none_button.clicked.connect(
            lambda _checked=False: self.window.apply_style("fill_color", "")
        )
        layout.addWidget(none_button)
        layout.addStretch(1)
        return row

    def _opacity_row(self) -> QWidget:
        """Непрозрачность: ползунок 5–100 % с подписью значения.

        Во время перетаскивания значение только показывается на холсте: каждая
        запись в документ — отдельная ступень отмены, а в истории их всего 24.
        В документ прозрачность попадает при отпускании ползунка (или сразу при
        изменении с клавиатуры).
        """
        style = self.window.effective_style()
        percent = int(round(min(max(annotation_opacity_value(style.get("opacity")), 0.0), 1.0) * 100))
        percent = max(percent, int(MIN_ANNOTATION_OPACITY * 100))
        editable = self._property_editable()
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName("AnnotationOpacity")
        slider.setRange(int(MIN_ANNOTATION_OPACITY * 100), 100)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setValue(percent)
        slider.setToolTip("Непрозрачность объекта: %d%%" % percent)
        slider.setAccessibleName("Непрозрачность объекта")
        slider.setEnabled(editable)
        value_label = QLabel("%d%%" % percent)
        value_label.setObjectName("AnnotationOpacityValue")
        value_label.setAccessibleName("Непрозрачность: %d%%" % percent)
        value_label.setProperty("role", "tiny")
        value_label.setFixedWidth(34)

        def show_value(value: int) -> None:
            value_label.setText("%d%%" % value)
            value_label.setAccessibleName("Непрозрачность: %d%%" % value)
            slider.setToolTip("Непрозрачность объекта: %d%%" % value)

        def on_value(value: int) -> None:
            show_value(value)
            if slider.isSliderDown():
                self.window.preview_style("opacity", value / 100.0)
            else:
                # Щелчок по дорожке, колесо, стрелки: значение сразу в документе.
                self.window.apply_style("opacity", value / 100.0, rebuild=False)

        def on_released() -> None:
            show_value(slider.value())
            self.window.apply_style("opacity", slider.value() / 100.0, rebuild=False)
            self.window.show_status("Непрозрачность: %d%%. Ctrl+Z — отменить" % slider.value())

        slider.valueChanged.connect(on_value)
        slider.sliderReleased.connect(on_released)
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        return row

    def _replace_image_block(self) -> None:
        """Замена картинки ВЫБРАННОГО объекта — без нового размещения."""
        replace = QPushButton("Заменить изображение…")
        replace.setObjectName("ReplaceSelectedImage")
        replace.setProperty("role", "primary")
        replace.setEnabled(self._property_editable())
        replace.setToolTip("Заменить картинку выбранного объекта, сохранив его позицию и размер")
        replace.clicked.connect(self.window.replace_selected_image)
        self._insert(replace, 6)
        hint = QLabel("Новый файл заменит картинку выбранного объекта: позиция и "
                      "размер не изменятся, второй объект не создаётся.")
        hint.setProperty("role", "tiny")
        hint.setWordWrap(True)
        self._insert(hint, 4)

    def _build_object_panel(self, tab: "EditorTab") -> None:
        tool = self.window.tool
        selection = self.window.selected_annotation()
        kind = selection[2].kind if selection else ""
        if kind == "note" or (tool == TOOL_NOTE and not selection):
            heading = "Комментарий"
            sub = ("Выбранный комментарий" if selection
                   else "Щёлкните на странице, чтобы прикрепить заметку.")
        elif kind in ("ink",) or tool == TOOL_PEN:
            heading = "Карандаш"
            sub = ("Выбранный штрих" if selection
                   else "Рисуйте прямо на странице, удерживая кнопку мыши.")
        elif kind in ("rect", "ellipse", "line", "arrow", "polygon", "cloud") or tool == TOOL_SHAPES:
            heading = "Фигуры"
            sub = ("Выбранный объект" if selection
                   else "Выберите тип и протяните мышь по странице.")
        elif kind in ("image", "stamp") or tool in (TOOL_IMAGE, TOOL_STAMP, TOOL_SIGNATURE):
            heading = "Изображение" if tool != TOOL_STAMP else "Штамп"
            sub = ("Выбранный объект" if selection
                   else "Выберите файл и щёлкните по странице.")
        else:
            heading = "Текст и объекты"
            sub = "Выбранный объект" if selection else "Параметры нового объекта"
        self._add_heading(heading)
        self._add_sub(sub)

        if selection and selection[2].kind == "note":
            self._add_property("ТЕКСТ КОММЕНТАРИЯ")
            body = QPlainTextEdit(selection[2].text)
            body.setFixedHeight(96)
            body.textChanged.connect(
                lambda: self.window.update_selected_text(body.toPlainText())
            )
            self._insert(body, 6)

        style = self.window.effective_style()
        if kind == "text" or (not selection and tool == TOOL_TEXT):
            self._add_property("ШРИФТ")
            fonts = self.window.font_families()
            family = QComboBox()
            family.addItems(fonts)
            if style.get("font_family") in fonts:
                family.setCurrentText(str(style["font_family"]))
            family.currentTextChanged.connect(
                lambda value: self.window.apply_style("font_family", value)
            )
            self._insert(family, 6)

            self._add_property("РАЗМЕР И НАЧЕРТАНИЕ")
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(7)
            sizes = QComboBox()
            for value in theme.FONT_SIZES:
                sizes.addItem("%d pt" % value, float(value))
            current_size = float(style.get("font_size") or 14.0)
            index = sizes.findData(current_size)
            sizes.setCurrentIndex(index if index >= 0 else 2)
            sizes.currentIndexChanged.connect(
                lambda _i, sizes=sizes: self.window.apply_style("font_size", sizes.currentData())
            )
            bold = QPushButton("Полужирный")
            bold.setProperty("role", "bold")
            bold.setCheckable(True)
            bold.setChecked(bool(style.get("bold")))
            bold.toggled.connect(lambda state: self.window.apply_style("bold", bool(state)))
            row_layout.addWidget(sizes, 1)
            row_layout.addWidget(bold, 1)
            self._insert(row, 6)

            self._add_property("ВЫРАВНИВАНИЕ")
            align_row = QWidget()
            align_layout = QHBoxLayout(align_row)
            align_layout.setContentsMargins(0, 0, 0, 0)
            align_layout.setSpacing(3)
            group = QButtonGroup(align_row)
            group.setExclusive(True)
            for name, icon_name, value in (("Слева", "align", "left"),
                                           ("По центру", "center", "center"),
                                           ("Справа", "right", "right")):
                button = QToolButton()
                button.setProperty("role", "align")
                button.setCheckable(True)
                button.setIcon(self.window.icon(icon_name, size=17))
                button.setToolTip(name)
                button.setIconSize(QSize(17, 17))
                button.setFixedHeight(30)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                button.setChecked(str(style.get("alignment") or "left") == value)
                button.clicked.connect(
                    lambda _checked=False, value=value: self.window.apply_style("alignment", value)
                )
                group.addButton(button)
                align_layout.addWidget(button)
            self._insert(align_row, 6)

        self._add_property("ЦВЕТ")
        colors = QWidget()
        colors_layout = QHBoxLayout(colors)
        colors_layout.setContentsMargins(0, 0, 0, 0)
        colors_layout.setSpacing(9)
        selected_color = str(style.get("color") or theme.DEFAULT_ANNOTATION_COLOR).lower()
        for value in theme.PALETTE:
            button = QPushButton()
            button.setProperty("role", "swatch")
            button.setProperty("swatch", "on" if value.lower() == selected_color else "off")
            button.setFixedSize(22, 22)
            button.setToolTip("Цвет %s" % value)
            button.setStyleSheet("background:%s;border-radius:11px;" % value)
            button.clicked.connect(
                lambda _checked=False, value=value: self.window.apply_style("color", value)
            )
            colors_layout.addWidget(button)
        colors_layout.addStretch(1)
        self._insert(colors, 6)

        self._add_property("ТОЛЩИНА ЛИНИИ")
        widths = QComboBox()
        for value in theme.STROKE_WIDTHS:
            widths.addItem("%d pt" % value, float(value))
        index = widths.findData(float(style.get("stroke_width") or 2.0))
        widths.setCurrentIndex(index if index >= 0 else 1)
        widths.currentIndexChanged.connect(
            lambda _i, widths=widths: self.window.apply_style(
                "stroke_width", widths.currentData()
            )
        )
        self._insert(widths, 6)

        if self._shows_property("fill"):
            self._add_property("ЗАЛИВКА")
            self._insert(self._fill_row(), 6)

        if self._shows_property("opacity"):
            self._add_property("НЕПРОЗРАЧНОСТЬ")
            self._insert(self._opacity_row(), 6)

        if tool == TOOL_SHAPES or heading == "Фигуры":
            self._add_property("ТИП ФИГУРЫ")
            shapes = QComboBox()
            for value, label in SHAPE_KINDS:
                shapes.addItem(label, value)
            current_kind = kind if kind in [value for value, _ in SHAPE_KINDS] else None
            if current_kind is None:
                current_kind = self.window.shape_kind()
            index = shapes.findData(current_kind)
            shapes.setCurrentIndex(index if index >= 0 else 0)
            shapes.currentIndexChanged.connect(
                lambda _i, shapes=shapes: self.window.set_shape_kind(shapes.currentData())
            )
            self._insert(shapes, 6)

        if selection and kind in ("image", "stamp"):
            # У выбранной картинки выбирать файл нельзя: это было бы новое
            # размещение. Явное действие — замена содержимого объекта.
            self._add_property("ЗАМЕНА ИЗОБРАЖЕНИЯ")
            self._replace_image_block()
        elif tool in (TOOL_IMAGE, TOOL_STAMP, TOOL_SIGNATURE) or kind in ("image", "stamp"):
            self._add_property("ФАЙЛ ИЗОБРАЖЕНИЯ")
            pick = QPushButton("Выбрать файл…")
            pick.setProperty("role", "ghost")
            pick.clicked.connect(lambda: self.window.choose_image(self.window.tool))
            self._insert(pick, 6)
            width_property = self.window.image_width_hint()
            if width_property:
                hint = QLabel(width_property)
                hint.setProperty("role", "tiny")
                hint.setWordWrap(True)
                self._insert(hint, 4)

        if selection:
            page, index, annotation = selection
            self._add_property("ДЕЙСТВИЯ")
            geometry = QPushButton("Точное размещение…")
            geometry.setObjectName("AnnotationGeometry")
            geometry.setEnabled(self.window.annotation_edit_allowed())
            geometry.clicked.connect(self.window.edit_annotation_geometry)
            self._insert(geometry, 6)
            remove = QPushButton("Удалить объект")
            remove.setProperty("role", "danger")
            remove.clicked.connect(self.window.delete_selected_annotation)
            self._insert(remove, 6)
            position = QLabel("Страница %d · объект %d · %s" % (
                page + 1, index + 1, self.window.kind_label(annotation.kind)))
            position.setProperty("role", "tiny")
            self._insert(position, 4)

        note = UNSUPPORTED_NOTES.get(self.window.mode)
        if note and self.window.mode == MODE_EDIT:
            self._add_hint(note)
        else:
            self._add_hint("Выберите объект для изменения. Перемещайте мышью или "
                           "стрелками на клавиатуре, Delete — удалить, Ctrl+Z — отменить.")


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    """Окно настроек макета: внешний вид, горячие клавиши, сохранение."""

    def __init__(self, window: "EditorWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("Settings")
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.setMinimumWidth(660)
        self.setMinimumHeight(470)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QFrame()
        top.setObjectName("SettingsTop")
        top.setFixedHeight(65)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(23, 0, 15, 0)
        title = QLabel("Настройки")
        title.setObjectName("SettingsTitle")
        top_layout.addWidget(title)
        top_layout.addStretch(1)
        close = QToolButton()
        close.setProperty("role", "icon")
        close.setIcon(window.icon("close"))
        close.setToolTip("Закрыть настройки")
        close.clicked.connect(self.reject)
        top_layout.addWidget(close)
        outer.addWidget(top)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        nav = QWidget()
        nav.setObjectName("SettingsNav")
        nav.setFixedWidth(164)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 18, 12, 18)
        nav_layout.setSpacing(2)
        self._nav_group = QButtonGroup(nav)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        for name, label in (("appearance", "Внешний вид"), ("shortcuts", "Горячие клавиши"),
                            ("recovery", "Сохранение")):
            button = QPushButton(label)
            button.setProperty("role", "nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, name=name: self.show_tab(name))
            nav_layout.addWidget(button)
            self._nav_group.addButton(button)
            self._nav_buttons[name] = button
        nav_layout.addStretch(1)
        body_layout.addWidget(nav)

        content_host = QWidget()
        content_host.setObjectName("SettingsContent")
        self._content_layout = QVBoxLayout(content_host)
        self._content_layout.setContentsMargins(25, 25, 25, 25)
        self._content_layout.setSpacing(0)
        body_layout.addWidget(content_host, 1)
        outer.addWidget(body, 1)

        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        footer.setFixedHeight(57)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(23, 0, 23, 0)
        footer_layout.addStretch(1)
        done = QPushButton("Готово")
        done.setProperty("role", "primary")
        done.clicked.connect(self.accept)
        footer_layout.addWidget(done)
        outer.addWidget(footer)

        self.show_tab("appearance")

    # -- содержимое вкладок ----------------------------------------------
    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)

    def _title(self, text: str) -> None:
        label = QLabel(text)
        label.setProperty("role", "heading")
        self._content_layout.addWidget(label)

    def _paragraph(self, text: str) -> None:
        label = QLabel(text)
        label.setProperty("role", "sub")
        label.setWordWrap(True)
        self._content_layout.addWidget(label)
        self._content_layout.addSpacing(18)

    def _row(self, label: str, control: QWidget) -> QFrame:
        frame = QFrame()
        frame.setObjectName("SettingRow")
        frame.setMinimumHeight(45)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 8, 0, 8)
        text = QLabel(label)
        text.setObjectName("SettingRowLabel")
        layout.addWidget(text)
        layout.addStretch(1)
        layout.addWidget(control)
        self._content_layout.addWidget(frame)
        return frame

    def show_tab(self, name: str) -> None:
        for key, button in self._nav_buttons.items():
            button.setChecked(key == name)
        self._clear_content()
        if name == "appearance":
            self._build_appearance()
        elif name == "shortcuts":
            self._build_shortcuts()
        else:
            self._build_recovery()

    def _build_appearance(self) -> None:
        self._title("Внешний вид")
        self._paragraph("Настройте рабочее пространство под себя.")
        pinned = SwitchButton(self.window.is_pinned)
        pinned.toggled.connect(self.window.set_pinned)
        self._row("Показывать полную панель", pinned)
        dark = SwitchButton(self.window.is_dark)
        dark.toggled.connect(self.window.set_dark)
        self._row("Тёмная тема", dark)
        rulers = SwitchButton(self.window.rulers_visible)
        rulers.toggled.connect(self.window.set_rulers)
        self._row("Показывать линейки", rulers)
        sidebar = SwitchButton(self.window.sidebar_visible)
        sidebar.toggled.connect(self.window.set_sidebar_visible)
        self._row("Показывать миниатюры", sidebar)
        note = QLabel("Подсказки инструментов: через 1,2 секунды")
        note.setProperty("role", "tiny")
        self._row("Подсказки инструментов", note)

    def _build_shortcuts(self) -> None:
        self._title("Горячие клавиши")
        self._paragraph("Стандартные сочетания можно переназначить; конфликт "
                        "обнаруживается сразу.")
        for name, label, _default in SHORTCUT_DEFAULTS:
            editor = QKeySequenceEdit(QKeySequence(self.window.shortcut_text(name)))
            editor.setMaximumWidth(190)
            editor.editingFinished.connect(
                lambda name=name, editor=editor: self._on_shortcut(name, editor)
            )
            self._row(label, editor)

    def _on_shortcut(self, name: str, editor: QKeySequenceEdit) -> None:
        text = editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        if not self.window.set_shortcut(name, text):
            editor.setKeySequence(QKeySequence(self.window.shortcut_text(name)))

    def _build_recovery(self) -> None:
        self._title("Сохранение и восстановление")
        self._paragraph("Временные копии помогают восстановить изменения после "
                        "сбоя. Исходный PDF не изменяется.")
        enabled = SwitchButton(self.window.autosave_enabled)
        enabled.toggled.connect(self.window.set_autosave_enabled)
        self._row("Автосохранение", enabled)
        interval = QComboBox()
        interval.setMaximumWidth(140)
        for minutes in theme.AUTOSAVE_INTERVALS_MIN:
            interval.addItem("%d минуты" % minutes if minutes in (2, 3, 4)
                             else "%d минута" % minutes, minutes)
        index = interval.findData(int(self.window.autosave_interval_min))
        interval.setCurrentIndex(index if index >= 0 else 1)
        interval.currentIndexChanged.connect(
            lambda _i, interval=interval: self.window.set_autosave_interval(
                int(interval.currentData())
            )
        )
        self._row("Интервал", interval)
        note = QLabel("Удалять временную копию")
        note.setProperty("role", "tiny")
        self._row("После сохранения", note)

        restore = QPushButton("Восстановить из копии…")
        restore.setProperty("role", "ghost")
        restore.clicked.connect(self._restore_from_copy)
        self._row("Восстановление", restore)
        remove = QPushButton("Удалить копию документа")
        remove.setProperty("role", "ghost")
        remove.clicked.connect(self._remove_copy)
        self._row("Временная копия", remove)
        folder = QLabel("Папка: %s" % self.window.recovery_root())
        folder.setProperty("role", "tiny")
        folder.setWordWrap(True)
        self._content_layout.addSpacing(14)
        self._content_layout.addWidget(folder)

    def _restore_from_copy(self) -> None:
        restored = self.window.offer_recovery()
        if restored:
            self.accept()

    def _remove_copy(self) -> None:
        removed = self.window.remove_recovery_for_current()
        self.window.toast("Временная копия удалена" if removed
                          else "Временной копии для этого документа нет")


class SearchDialog(QDialog):
    """Поиск по полям формы и пометкам с честной оговоркой об исходном тексте."""

    def __init__(self, window: "EditorWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("SearchDialog")
        self.setWindowTitle("Поиск")
        self.setModal(True)
        self.setMinimumWidth(460)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Найти в полях формы и пометках")
        self.field.returnPressed.connect(self.run_search)
        find = QPushButton("Найти")
        find.setProperty("role", "primary")
        find.clicked.connect(self.run_search)
        row.addWidget(self.field, 1)
        row.addWidget(find)
        outer.addLayout(row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(240)
        self._host = QWidget()
        self._results_layout = QVBoxLayout(self._host)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(6)
        self._results_layout.addStretch(1)
        self.scroll.setWidget(self._host)
        outer.addWidget(self.scroll, 1)

        self.note = QLabel('Поиск по исходному текстовому слою, полям и пометкам. Для сканов требуется OCR.')
        self.note.setProperty("role", "tiny")
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        self.field.setFocus()

    def run_search(self) -> None:
        term = self.field.text()
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)
        results = self.window.search_document(term)
        if not term.strip():
            return
        if not results:
            empty = QLabel("Совпадений не найдено.")
            empty.setProperty("role", "tiny")
            self._results_layout.insertWidget(0, empty)
            return
        for position, result in enumerate(results[:200]):
            self._results_layout.insertWidget(
                position, SearchResultRow(result, self.window.jump_to_result, self._host)
            )


# ---------------------------------------------------------------------------
# Вкладка документа
# ---------------------------------------------------------------------------
@dataclass
class RecoveryState:
    """Состояние временного восстановления одной вкладки (источник не меняется)."""

    key: Optional[str] = None
    source_paths: list[str] = field(default_factory=list)
    restored: bool = False
    copy_path: Optional[str] = None
    last_revision: Optional[int] = None
    last_path: Optional[str] = None


@dataclass
class EditorTab:
    """Одна вкладка: своя сессия, вкладка независима по dirty/undo/save."""

    session: Any
    path: Optional[str] = None
    title: str = "Без имени"
    mode: str = MODE_READ
    tool: str = TOOL_CURSOR
    page_index: int = 0
    zoom: float = 1.0
    selection: Optional[tuple[int, int]] = None
    style: dict = field(default_factory=lambda: {
        "font_family": "Arial",
        "font_size": 14.0,
        "color": theme.DEFAULT_ANNOTATION_COLOR,
        "stroke_width": 2.0,
        "bold": False,
        "alignment": "left",
    })
    shape_kind: str = "rect"
    page_widget: Optional[QWidget] = None
    view: Any = None
    placeholder: Optional[QWidget] = None
    thumbs: dict = field(default_factory=dict)
    thumbs_revision: Optional[int] = None
    recovery: RecoveryState = field(default_factory=RecoveryState)
    source_controller: Any = None
    form_author: Any = None

    @property
    def has_document(self) -> bool:
        try:
            return int(getattr(self.session, "page_count", 0)) > 0
        except Exception:  # noqa: BLE001 - чужая сессия
            return False

    @property
    def dirty(self) -> bool:
        try:
            return bool(getattr(self.session, "dirty", False))
        except Exception:  # noqa: BLE001
            return False


_ACTIVE_IMPORT_WORKERS: set["_DocumentImportWorker"] = set()


class _DocumentImportWorker(QThread):
    """Run image/Office conversion away from the Qt event loop."""

    converted = Signal(bytes, str, str)  # PDF bytes, destination title, source folder
    failed = Signal(str)

    def __init__(self, service: Any, paths: Sequence[str], title: str, *,
                 images: bool, page_size: Any) -> None:
        super().__init__(None)
        self.service = service
        self.paths = list(paths)
        self.title = str(title)
        self.images = bool(images)
        self.page_size = page_size
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            if self.images:
                data = self.service.images_to_pdf(
                    self.paths, page_size=self.page_size, cancel_event=self.cancel_event
                )
            else:
                data = self.service.convert_to_pdf(self.paths[0], cancel_event=self.cancel_event)
            if self.cancel_event.is_set():
                raise RuntimeError("Импорт отменён.")
            if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
                raise RuntimeError("Конвертер не вернул корректный PDF-файл.")
            source_folder = str(Path(self.paths[0]).resolve().parent) if self.paths else ""
            self.converted.emit(data, self.title, source_folder)
        except Exception as exc:  # noqa: BLE001 - report converter errors in the UI
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class EditorWindow(QMainWindow):
    """Новый интерфейс PdfDesk по утверждённому макету."""

    def __init__(self, parent: Optional[QWidget] = None, session_factory: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PdfDesk")
        self.setMinimumSize(QSize(900, 620))
        self.resize(QSize(1440, 1000))
        self.setAcceptDrops(True)
        self._icons = editor_icons.default_library()
        self._session_factory: Callable[[], Any] = session_factory or PdfSession
        self._tabs: list[EditorTab] = []
        self._pinned = False
        self._dark = False
        self._rulers = False
        self._sidebar_visible = True
        self._last_dir = os.path.expanduser("~")
        self._last_saved_dir = self._last_dir
        self._document_import_service: Any = None
        self._import_worker: Optional[_DocumentImportWorker] = None
        self._import_cursor_active = False
        self._closing = False
        self._shortcuts: dict[str, QShortcut] = {}
        self._shortcut_text: dict[str, str] = {}
        self._settings = settings_store()
        self._autosave_enabled = True
        self._autosave_interval_min = theme.AUTOSAVE_DEFAULT_MIN
        self._recovery_root: Optional[Path] = None
        self._compare_splitter: Optional[QSplitter] = None
        self._compare_panel: Optional[QWidget] = None
        self._compare_selector: Optional[QComboBox] = None
        self._compare_view: Any = None
        self._compare_session: Any = None
        self._compare_tab: Optional[EditorTab] = None
        self._compare_title: Optional[QLabel] = None
        self._compare_syncing = False
        self._compare_ratio = 0.5
        self._compare_layout_pending = False
        self._compare_programmatic_resize = False
        self._status_timeout = QTimer(self)
        self._status_timeout.setSingleShot(True)
        self._status_timeout.timeout.connect(lambda: self._reset_status())
        self._panel_refresh_timer = QTimer(self)
        self._panel_refresh_timer.setSingleShot(True)
        self._panel_refresh_timer.timeout.connect(self._flush_view_refresh)
        self._pending_refresh_tab: Optional[EditorTab] = None

        self._build_ui()
        self._load_settings()
        self._build_shortcuts()
        self._apply_theme()
        self.new_tab()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self.autosave_now)
        self._apply_autosave_timer()
        QTimer.singleShot(0, self.offer_recovery)

    # ------------------------------------------------------------------
    # Публичный контракт (используется main.py и проверками)
    # ------------------------------------------------------------------
    @property
    def _session(self) -> Any:
        tab = self.current_tab
        return tab.session if tab is not None else None

    @property
    def _canvas(self) -> Any:
        tab = self.current_tab
        if tab is None or tab.view is None:
            return None
        canvases = self._view_canvases(tab)
        if not canvases:
            return None
        index = self.view_page_index(tab) or tab.page_index
        if index < 0 or index >= len(canvases):
            index = 0
        return canvases[index]

    @property
    def canvas(self) -> Any:
        return self._canvas

    @property
    def page_count(self) -> int:
        tab = self.current_tab
        if tab is None:
            return 0
        try:
            return int(getattr(tab.session, "page_count", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    @property
    def current_tab(self) -> Optional[EditorTab]:
        stack = getattr(self, "stack", None)
        index = stack.currentIndex() if stack is not None else -1
        if 0 <= index < len(self._tabs):
            return self._tabs[index]
        return self._tabs[0] if self._tabs else None

    @property
    def tabs(self) -> list[EditorTab]:
        return list(self._tabs)

    @property
    def mode(self) -> str:
        tab = self.current_tab
        return tab.mode if tab is not None else MODE_READ

    @property
    def tool(self) -> str:
        tab = self.current_tab
        return tab.tool if tab is not None else TOOL_CURSOR

    @property
    def is_pinned(self) -> bool:
        return self._pinned

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def rulers_visible(self) -> bool:
        return self._rulers

    @property
    def sidebar_visible(self) -> bool:
        return self._sidebar_visible

    @property
    def autosave_enabled(self) -> bool:
        return self._autosave_enabled

    @property
    def autosave_interval_min(self) -> int:
        return self._autosave_interval_min

    # ------------------------------------------------------------------
    # Сборка интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_doc_bar())
        layout.addWidget(self._build_mode_bar())
        layout.addWidget(self._build_tools_bar())
        layout.addWidget(self._build_workspace(), 1)
        layout.addWidget(self._build_status_bar())
        self.setCentralWidget(central)
        self._toast = ToastLabel(self)

    def _build_doc_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("DocBar")
        bar.setFixedHeight(theme.DOC_BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)

        brand_box = QWidget()
        brand_layout = QHBoxLayout(brand_box)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(9)
        brand_icon = QLabel()
        brand_icon.setObjectName("BrandIcon")
        brand_icon.setFixedSize(theme.BRAND_ICON_WIDTH, theme.BRAND_ICON_HEIGHT)
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setPixmap(self._icons.pixmap("file", size=18, color="#ffffff"))
        brand_layout.addWidget(brand_icon)
        brand = QLabel("PdfDesk")
        brand.setObjectName("Brand")
        brand_layout.addWidget(brand)
        layout.addWidget(brand_box)

        self.sidebar_button = self._icon_button(
            "sidebar", "Миниатюры страниц", checkable=True, checked=True,
            slot=self.toggle_sidebar,
        )
        layout.addWidget(self.sidebar_button)
        document_button = self._icon_button('plus', 'Создать или открыть документ')
        document_menu = QMenu(document_button)
        document_menu.addAction('Создать PDF…',self.create_document_dialog)
        document_menu.addAction('Создать PDF из файла…', self.import_document_dialog)
        document_menu.addAction('Изображения → один PDF…', self.import_images_dialog)
        document_menu.addAction('Открыть PDF…',self.open_dialog)
        document_menu.addAction('Сканировать в PDF…', self.scan_document_dialog)
        document_menu.addAction('Распознать текст (OCR)…', self.recognize_text_dialog)
        document_menu.addAction('Исправить распознанный текст…', self.correct_ocr_text_dialog)
        document_menu.addSeparator()
        document_menu.addAction('Защита PDF…', self.configure_pdf_security)
        document_menu.addAction('Разрешить изменение…', self.unlock_pdf_editing)
        document_menu.addAction('Снять защиту PDF', self.remove_pdf_security)
        document_menu.addSeparator()
        self.compare_action = document_menu.addAction(
            'Сравнить документы рядом…', self.compare_documents
        )
        document_button.setMenu(document_menu)
        document_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        layout.addWidget(document_button)

        self.tabbar = QTabBar()
        self.tabbar.setExpanding(False)
        self.tabbar.setMovable(True)
        self.tabbar.setTabsClosable(True)
        self.tabbar.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabbar.setDrawBase(False)
        self.tabbar.setUsesScrollButtons(True)
        self.tabbar.setIconSize(QSize(14, 14))
        self.tabbar.currentChanged.connect(self._on_tab_changed)
        self.tabbar.tabCloseRequested.connect(self.close_tab)
        self.tabbar.tabMoved.connect(self._move_tab)
        layout.addWidget(self.tabbar, 1)

        self.doc_meta = QLabel("")
        self.doc_meta.setObjectName("DocMeta")
        layout.addWidget(self.doc_meta)
        return bar

    def _build_mode_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("ModeBar")
        bar.setFixedHeight(theme.MODE_BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(8)

        switch = QWidget()
        switch.setObjectName("ModeSwitch")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(3, 3, 3, 3)
        switch_layout.setSpacing(2)
        self.mode_group = QButtonGroup(switch)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for name in theme.MODE_ORDER:
            button = QPushButton(theme.MODE_LABELS[name])
            button.setProperty("role", "mode")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, name=name: self.set_mode(name))
            switch_layout.addWidget(button)
            self.mode_group.addButton(button)
            self.mode_buttons[name] = button
        layout.addWidget(switch)

        self.viewer_controls = QWidget()
        controls = QHBoxLayout(self.viewer_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(0)
        layout.addStretch(1)
        layout.addWidget(self.viewer_controls)

        self.undo_button = self._icon_button(
            "undo", "Отменить · Ctrl+Z", slot=self.undo, header=True
        )
        self.redo_button = self._icon_button(
            "redo", "Повторить · Ctrl+Y", slot=self.redo, header=True
        )
        controls.addWidget(self.undo_button)
        controls.addWidget(self.redo_button)
        controls.addWidget(self._separator())
        self.zoom_out_button = self._icon_button(
            "minus", "Уменьшить", slot=self.zoom_out, header=True
        )
        controls.addWidget(self.zoom_out_button)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("ZoomValue")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(43)
        self.zoom_label.setProperty("role", "tiny")
        controls.addWidget(self.zoom_label)
        self.zoom_in_button = self._icon_button(
            "plus", "Увеличить", slot=self.zoom_in, header=True
        )
        controls.addWidget(self.zoom_in_button)
        self.fit_button = self._icon_button(
            "fit", "Страница целиком · Ctrl+0", slot=self.fit_page, header=True
        )
        controls.addWidget(self.fit_button)
        controls.addWidget(self._separator())
        self.ruler_button = self._icon_button(
            "ruler", "Линейки", checkable=True, slot=self.set_rulers, header=True
        )
        self.ruler_menu = QMenu(self.ruler_button)
        self.guides_action = self.ruler_menu.addAction("Направляющие…")
        self.guides_action.triggered.connect(self.open_precise_guides)
        self.ruler_button.setMenu(self.ruler_menu)
        self.ruler_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        controls.addWidget(self.ruler_button)
        self.search_button = self._icon_button(
            "search", "Поиск · Ctrl+F", slot=self.open_search, header=True
        )
        controls.addWidget(self.search_button)

        self.header_actions = QWidget()
        actions = QHBoxLayout(self.header_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(2)
        layout.addWidget(self.header_actions)
        self.print_button = self._icon_button(
            "print", "Печать · Ctrl+P", slot=self.print_document, header=True
        )
        actions.addWidget(self.print_button)
        self.settings_button = self._icon_button(
            "settings", "Настройки", slot=self.open_settings, header=True
        )
        actions.addWidget(self.settings_button)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setProperty("role", "primary")
        self.save_button.setIcon(self._icons.icon("save", size=16, color="#ffffff"))
        self.save_button.setIconSize(QSize(16, 16))
        self.save_button.setToolTip("Сохранить копию PDF (исходный файл не изменяется)")
        self.save_button.clicked.connect(self.save_current)
        actions.addWidget(self.save_button)
        return bar

    def _build_tools_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("ToolsBar")
        bar.setFixedHeight(theme.TOOLS_HEIGHT)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.tool_heading = QLabel("")
        self.tool_heading.setProperty("role", "property")
        self.tool_heading.setMinimumWidth(80)
        self.tool_heading.hide()
        row.addWidget(self.tool_heading)
        self.tool_host = QWidget()
        self.tool_layout = QHBoxLayout(self.tool_host)
        self.tool_layout.setContentsMargins(0, 0, 0, 0)
        self.tool_layout.setSpacing(3)
        row.addWidget(self.tool_host)
        row.addStretch(1)
        self.pin_button = self._icon_button(
            "pin", "Закрепить полную панель", checkable=True, slot=self.set_pinned
        )
        self.pin_button.setProperty("role", "pin")
        row.addWidget(self.pin_button)
        layout.addLayout(row)

        self.full_tools = QWidget()
        full_layout = QVBoxLayout(self.full_tools)
        full_layout.setContentsMargins(0, 0, 0, 0)
        full_layout.setSpacing(4)
        for heading, tools in FULL_TOOL_GROUPS:
            group_row = QHBoxLayout()
            group_row.setContentsMargins(0, 0, 0, 0)
            group_row.setSpacing(3)
            label = QLabel(heading)
            label.setProperty("role", "property")
            label.setMinimumWidth(80)
            group_row.addWidget(label)
            for tool_id, text, icon_name in tools:
                group_row.addWidget(self._make_tool_button(tool_id, text, icon_name))
            group_row.addStretch(1)
            full_layout.addLayout(group_row)
        self.full_tools.hide()
        layout.addWidget(self.full_tools)

        self.tools_bar = bar
        self.tool_buttons: dict[str, QToolButton] = {}
        return bar

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        workspace.setObjectName("Workspace")
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.thumbnails = ThumbnailPanel(self)
        layout.addWidget(self.thumbnails)

        stage = QWidget()
        stage.setObjectName("Stage")
        stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        self._stage = stage

        paper_area = QWidget()
        paper_area.setObjectName("PaperArea")
        paper_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        paper_layout = QVBoxLayout(paper_area)
        paper_layout.setContentsMargins(0, 0, 0, 0)
        paper_layout.setSpacing(0)
        self.top_ruler = RulerWidget(Qt.Orientation.Horizontal)
        self.left_ruler = RulerWidget(Qt.Orientation.Vertical)
        self.top_ruler.guide_dropped.connect(self._drop_guide)
        self.left_ruler.guide_dropped.connect(self._drop_guide)
        ruler_grid = QWidget()
        ruler_layout = QVBoxLayout(ruler_grid)
        ruler_layout.setContentsMargins(0, 0, 0, 0)
        ruler_layout.setSpacing(0)
        ruler_layout.addWidget(self.top_ruler)
        middle = QWidget()
        middle_layout = QHBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        middle_layout.addWidget(self.left_ruler)
        self.stack = QStackedWidget()
        self._compare_splitter = QSplitter(Qt.Orientation.Horizontal, middle)
        self._compare_splitter.setObjectName("DocumentCompareSplitter")
        self._compare_splitter.setChildrenCollapsible(False)
        self._compare_splitter.addWidget(self.stack)
        self._compare_splitter.setStretchFactor(0, 1)
        self._compare_splitter.splitterMoved.connect(self._on_compare_splitter_moved)
        middle_layout.addWidget(self._compare_splitter, 1)
        ruler_layout.addWidget(middle, 1)
        paper_layout.addWidget(ruler_grid, 1)
        self.top_ruler.hide()
        self.left_ruler.hide()

        self.pages_grid = PagesGrid(self)
        grid_scroll = QScrollArea()
        grid_scroll.setWidgetResizable(True)
        grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        grid_scroll.setWidget(self.pages_grid)
        self.stage_stack = QStackedWidget()
        self.stage_stack.addWidget(paper_area)
        self.stage_stack.addWidget(grid_scroll)
        stage_layout.addWidget(self.stage_stack, 1)
        layout.addWidget(stage, 1)

        self.inspector = InspectorPanel(self)
        layout.addWidget(self.inspector)
        return workspace

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(theme.STATUS_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(0)
        left = QWidget()
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(9)
        dot = QLabel()
        dot.setObjectName("StatusDot")
        dot.setFixedSize(5, 5)
        left_layout.addWidget(dot)
        self.status_label = QLabel("Откройте PDF, чтобы начать работу")
        self.status_label.setObjectName("StatusText")
        left_layout.addWidget(self.status_label)
        layout.addWidget(left)
        layout.addStretch(1)
        self.page_status = QLabel("")
        self.page_status.setObjectName("StatusPage")
        self.mode_status = QLabel(theme.MODE_STATUS[MODE_READ])
        self.mode_status.setObjectName("StatusMode")
        layout.addWidget(self.page_status)
        layout.addSpacing(21)
        layout.addWidget(self.mode_status)
        return bar

    # -- мелкие строители -------------------------------------------------
    def icon(self, name: str, *, size: Optional[int] = None, color: Optional[str] = None):
        """QIcon из символов макета (без системных значков Qt)."""
        return self._icons.icon(name, size=size, color=color)

    def _icon_button(
        self,
        name: str,
        tooltip: str,
        *,
        slot: Optional[Callable[..., Any]] = None,
        checkable: bool = False,
        checked: bool = False,
        header: bool = False,
    ) -> QToolButton:
        button = QToolButton()
        button.setProperty("role", "icon")
        button.setProperty("pd_icon", name)
        button.setIcon(self._icons.icon(name))
        button.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setChecked(checked)
        if header:
            button.setFixedSize(theme.ICON_BOX_WIDTH_HEADER, theme.ICON_BOX_HEIGHT_HEADER)
        else:
            button.setFixedSize(theme.ICON_BOX, theme.ICON_BOX)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def _separator(self) -> QFrame:
        line = QFrame()
        line.setProperty("role", "sep")
        line.setFixedSize(1, 22)
        line.setFrameShape(QFrame.Shape.VLine)
        return line

    def _make_tool_button(self, tool_id: str, text: str, icon_name: str) -> QToolButton:
        button = QToolButton()
        button.setProperty("role", "tool")
        button.setProperty("pd_icon", icon_name)
        button.setProperty("pd_tool", tool_id)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setIcon(self._icons.icon(icon_name, size=theme.TOOL_ICON_SIZE))
        button.setIconSize(QSize(theme.TOOL_ICON_SIZE, theme.TOOL_ICON_SIZE))
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip("%s — %s" % (text, TOOL_HINTS.get(tool_id, "")))
        button.clicked.connect(lambda _checked=False, tool_id=tool_id: self.set_tool(tool_id))
        return button

    # ------------------------------------------------------------------
    # Тема, режим, инструменты
    # ------------------------------------------------------------------
    def _apply_theme(self) -> None:
        stylesheet = theme.build_qss(self._dark)
        self.setStyleSheet(stylesheet)
        instance = QApplication.instance()
        if instance is not None:
            instance.setStyleSheet(stylesheet)
        self._apply_stage_palette()

    def stage_color(self) -> QColor:
        """Цвет фона рабочей области: светлый из макета либо тёмный вариант."""
        value = theme.DARK_OVERRIDES["stage"] if self._dark else theme.STAGE
        return QColor(value)

    def _apply_stage_palette(self, view: Any = None) -> None:
        """Перенести цвет макета на холст документа и его прокручиваемые слои.

        ``DocumentView`` пришёл из отклонённой основы и красит фон в тёмно-серый
        ``CANVAS_BACKGROUND``. Палитра задаётся снаружи — чужой модуль не
        правится, — поэтому светлая тема макета и тёмная тема применяются к
        документу так же, как к остальным частям окна.
        """
        color = self.stage_color()
        targets: list[QWidget] = []
        if view is not None:
            targets.append(view)
        stage = self._stage
        for area in self.findChildren(QScrollArea):
            if area is self.thumbnails.scroll or area is self.inspector.scroll:
                continue  # у миниатюр и свойств свои поверхности макета
            if stage is None or not stage.isAncestorOf(area):
                continue
            targets.append(area)
            targets.append(area.viewport())
            inner = area.widget()
            if inner is not None:
                targets.append(inner)
        for target in targets:
            if target is None:
                continue
            target.setAutoFillBackground(True)
            palette = target.palette()
            palette.setColor(QPalette.ColorRole.Window, color)
            palette.setColor(QPalette.ColorRole.Base, color)
            target.setPalette(palette)
            target.update()

    def set_dark(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._dark:
            return
        self._dark = enabled
        self._apply_theme()
        self._settings.setValue("appearance/dark", enabled)
        self.toast("Тёмная тема включена" if enabled else "Тёмная тема выключена")

    def set_rulers(self, enabled: bool) -> None:
        self._rulers = bool(enabled)
        self.ruler_button.setChecked(self._rulers)
        self.top_ruler.setVisible(self._rulers)
        self.left_ruler.setVisible(self._rulers)
        for tab in self._tabs:
            for canvas in self._view_canvases(tab):
                set_visible = getattr(canvas, "set_guides_visible", None)
                if callable(set_visible):
                    set_visible(self._rulers)
                else:
                    canvas.guides_visible = self._rulers
                    canvas.update()
        self._settings.setValue("appearance/rulers", self._rulers)
        self.show_status("Линейки включены" if self._rulers else "Линейки выключены")

    def open_precise_guides(self, _checked: bool = False) -> bool:
        """Открыть менеджер направляющих страницы активной вкладки."""
        tab = self.current_tab
        if tab is None or tab.view is None:
            self.show_status("Сначала откройте документ")
            return False
        canvases = self._view_canvases(tab)
        page_index = self.view_page_index(tab)
        if not 0 <= page_index < len(canvases):
            self.show_status("Активная страница недоступна")
            return False
        canvas = canvases[page_index]
        open_dialog = getattr(canvas, "open_guide_dialog", None)
        if not callable(open_dialog):
            self.show_status("Управление направляющими недоступно")
            return False
        return bool(open_dialog())

    def set_sidebar_visible(self, visible: bool) -> None:
        self._sidebar_visible = bool(visible)
        self.thumbnails.setVisible(self._sidebar_visible)
        self.sidebar_button.setChecked(self._sidebar_visible)
        self._settings.setValue("appearance/sidebar", self._sidebar_visible)
        self.show_status("Миниатюры показаны" if self._sidebar_visible
                         else "Миниатюры скрыты")

    def toggle_sidebar(self, _checked: bool = False) -> None:
        self.set_sidebar_visible(not self._sidebar_visible)

    def set_pinned(self, pinned: bool) -> None:
        self._pinned = bool(pinned)
        self.pin_button.setChecked(self._pinned)
        self.full_tools.setVisible(self._pinned)
        self.tool_heading.setVisible(self._pinned)
        if self._pinned:
            height = theme.TOOLS_HEIGHT + self.full_tools.sizeHint().height() + 10
            self.tools_bar.setFixedHeight(max(theme.TOOLS_HEIGHT, height))
            self.tools_bar.setMinimumHeight(theme.TOOLS_HEIGHT)
        else:
            self.tools_bar.setMinimumHeight(0)
            self.tools_bar.setFixedHeight(theme.TOOLS_HEIGHT)
        self._settings.setValue("appearance/pinned", self._pinned)
        self.show_status("Полная панель закреплена" if self._pinned
                         else "Компактная панель инструментов")

    def set_mode(self, mode: str) -> None:
        tab = self.current_tab
        if tab is None or mode not in theme.MODE_ORDER:
            return
        if mode in (MODE_EDIT, MODE_COMMENT, MODE_PAGES) and not getattr(tab.session, 'has_edit_permission', True):
            self.show_status('Для изменения документа введите пароль владельца в меню документа')
            return
        if mode == MODE_FILL and not getattr(tab.session, 'can_fill_forms', True):
            self.show_status('Заполнение форм запрещено защитой документа')
            return
        tab.mode = mode
        tab.tool = TOOL_CURSOR
        tab.selection = None
        self.mode_buttons[mode].setChecked(True)
        self._rebuild_tools()
        self._push_tool()
        self._push_readonly(tab)
        self._sync_stage()
        self._sync_source_controllers()
        self.inspector.rebuild()
        self.mode_status.setText(theme.MODE_STATUS[mode])
        note = UNSUPPORTED_NOTES.get(mode)
        if mode == MODE_FILL:
            self.show_status("Выберите поле на странице, чтобы начать заполнение")
        else:
            self.show_status(note or theme.MODE_STATUS[mode])

    def set_tool(self, tool_id: str) -> None:
        tab = self.current_tab
        if tab is None:
            return
        if (tab.has_document and not getattr(tab.session, 'has_edit_permission', True)
                and tool_id not in (TOOL_CURSOR, TOOL_FORMS, TOOL_SEARCH, TOOL_RULER,
                                    'fit_width', 'jump_page', 'rotate_view')):
            self.show_status('Этот инструмент требует пароля владельца PDF')
            return
        page_actions = {
            'blank_page': self.insert_blank_page,
            'merge_pdf': self.merge_pdf_dialog,
            'page_back': lambda:self.move_current_page(-1),
            'page_forward': lambda:self.move_current_page(1),
        }
        if tool_id in page_actions:
            page_actions[tool_id]()
            return
        if tool_id=='fit_width':
            self.fit_width()
            return
        if tool_id == 'rotate_view':
            if tab.mode == MODE_READ and tab.view is not None:
                angle = tab.view.rotate_view(90)
                self.show_status('Поворот просмотра: %d°. Сам PDF не изменён' % angle)
            return
        if tool_id=='jump_page':
            page,accepted=QInputDialog.getInt(self,'Перейти к странице','Номер страницы',tab.page_index+1,1,self.page_count)
            if accepted:
                self.goto_page(page-1)
            return
        if tool_id == TOOL_RULER:
            self.set_rulers(not self._rulers)
            self._rebuild_tools()
            return
        if tool_id == TOOL_SEARCH:
            self.open_search()
            return
        if tool_id == TOOL_ROTATE:
            self.rotate_page()
            return
        if tool_id == TOOL_TRASH:
            self.delete_page()
            return
        if tool_id == TOOL_FORMS:
            tab.tool = TOOL_CURSOR
            self._ensure_form_tool()
        else:
            tab.tool = tool_id
        tab.selection = None
        self._rebuild_tools()
        self._push_tool()
        self._sync_source_controllers()
        self.inspector.rebuild()
        hint = TOOL_HINTS.get(tool_id, "")
        if tool_id == TOOL_SIGNATURE:
            hint = "Подпись размещается как изображение (не криптографическая подпись)"
        if tool_id == TOOL_IMAGE and self.image_width_hint() is None:
            hint = "Сначала выберите изображение в панели справа"
        self.show_status(hint or "Инструмент выбран")

    def _rebuild_tools(self) -> None:
        while self.tool_layout.count():
            item = self.tool_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                _dispose_widget(widget)
        self.tool_buttons = {}
        tab = self.current_tab
        mode = tab.mode if tab is not None else MODE_READ
        active = tab.tool if tab is not None else TOOL_CURSOR
        tools = list(MODE_TOOLS.get(mode, ()))
        if mode == MODE_FILL:
            forms_count = self._form_count()
            tools.append((TOOL_FORMS, "Все поля · %d" % forms_count, "check"))
        for tool_id, text, icon_name in tools:
            button = self._make_tool_button(tool_id, text, icon_name)
            button.setChecked(tool_id == active)
            if (tab is not None and tab.has_document and
                    not getattr(tab.session, 'has_edit_permission', True) and
                    tool_id not in (TOOL_CURSOR, TOOL_FORMS, TOOL_SEARCH, TOOL_RULER,
                                    'fit_width', 'jump_page', 'rotate_view')):
                button.setEnabled(False)
            self.tool_layout.addWidget(button)
            self.tool_buttons[tool_id] = button
        self.tool_heading.setText(MODE_TOOL_HEADING.get(mode, "Инструменты"))
        self.ruler_button.setChecked(self._rulers and mode == MODE_EDIT)

    def _ensure_form_tool(self) -> None:
        """В режиме «Заполнение» поля формы на странице должны быть активны."""
        tab = self.current_tab
        if tab is None or tab.view is None:
            return
        self._call_view(tab, "set_tool", "form", fallback="select")

    def _push_readonly(self, tab: Optional[EditorTab] = None) -> None:
        """Просмотр и страницы — без правки объектов; формы работают по флагам полей."""
        tab = tab or self.current_tab
        if tab is None or tab.view is None:
            return
        readonly = tab.mode in (MODE_READ, MODE_PAGES)
        self._call_view(tab, "set_readonly", readonly)

    def _push_tool(self) -> None:
        tab = self.current_tab
        if tab is None or tab.view is None:
            return
        self._call_view(tab, 'set_native_image_mode', tab.mode == MODE_EDIT)
        canvas_tool = CANVAS_TOOLS.get(tab.tool, "select")
        if tab.tool == TOOL_SHAPES:
            canvas_tool = tab.shape_kind
        elif tab.tool == TOOL_SIGNATURE:
            canvas_tool = "image"
        elif tab.tool == TOOL_STAMP:
            canvas_tool = "stamp"
        elif tab.tool == TOOL_IMAGE:
            canvas_tool = "image"
        elif tab.tool == TOOL_CURSOR and tab.mode == MODE_FILL:
            canvas_tool = "form"
        self._call_view(tab, "set_tool", canvas_tool, fallback="select")
        self._push_style(tab)
        if tab.tool in (TOOL_IMAGE, TOOL_STAMP, TOOL_SIGNATURE):
            data = tab.style.get("image_data")
            if data:
                image_kind = "stamp" if tab.tool == TOOL_STAMP else "image"
                self._call_view(tab, "set_image_data", data, image_kind)

    def _push_style(self, tab: Optional[EditorTab] = None) -> None:
        tab = tab or self.current_tab
        if tab is None or tab.view is None:
            return
        style = dict(tab.style)
        fill_color = str(style.get('fill_color') or "")
        opacity = annotation_opacity_value(style.get('opacity'), 1.0)
        if tab.selection is not None:
            # Выделенный объект получает СВОИ значения: иначе холст применил бы
            # к нему стиль инструмента (и это записалось бы в документ).
            annotation = self._annotation_at(tab, tab.selection[0], tab.selection[1])
            if annotation is not None:
                fill_color = str(getattr(annotation, "fill_color", "") or "")
                opacity = annotation_opacity_value(getattr(annotation, "opacity", 1.0))
        else:
            context = self.style_context_kind(tab)
            if context:
                # Заливка и прозрачность уходят холсту только для типов, где
                # предпросмотр совпадает с сохранённым внешним видом.
                if context not in FILL_EDIT_KINDS:
                    fill_color = ""
                if context not in OPACITY_EDIT_KINDS:
                    opacity = 1.0
        kwargs = {
            "font_family": style.get("font_family", "Arial"),
            "font_size": float(style.get("font_size") or 14.0),
            "color": style.get("color", theme.DEFAULT_ANNOTATION_COLOR),
            "stroke_width": float(style.get("stroke_width") or 2.0),
            'bold': bool(style.get('bold',False)),
            'alignment': style.get('alignment','left'),
            'fill_color': fill_color,
            'opacity': opacity,
        }
        try:
            tab.view.set_style(**kwargs)
        except TypeError:
            self._call_view(tab, "set_style", kwargs["font_family"], kwargs["font_size"],
                            kwargs["color"], kwargs["stroke_width"])
        except Exception as exc:  # noqa: BLE001 - чужой холст
            self._note_error("Стиль инструмента", exc)

    def style_context_kind(self, tab: Optional[EditorTab] = None) -> str:
        """Тип объекта, к которому сейчас относятся заливка и прозрачность.

        Выбранный объект важнее инструмента; для «Фигур» тип задаёт список
        фигур, для остальных инструментов — таблица ``TOOL_ANNOTATION_KINDS``.
        Пустая строка — контекст неизвестен (инструмент «Выбор» без выделения).
        """
        tab = tab or self.current_tab
        if tab is None:
            return ""
        if tab.selection is not None:
            annotation = self._annotation_at(tab, tab.selection[0], tab.selection[1])
            if annotation is not None:
                return str(getattr(annotation, "kind", "") or "")
            return ""
        if tab.tool == TOOL_SHAPES:
            return str(tab.shape_kind or "rect")
        return TOOL_ANNOTATION_KINDS.get(tab.tool, "")

    # ------------------------------------------------------------------
    # Вкладки
    # ------------------------------------------------------------------
    def new_tab(self, path: Optional[str] = None, *, silent: bool = False) -> EditorTab:
        """Создать вкладку с собственной сессией и (при необходимости) открыть файл."""
        session = self._session_factory()
        tab = EditorTab(session=session)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        placeholder = self._make_placeholder()
        page_layout.addWidget(placeholder)
        tab.page_widget = page
        tab.placeholder = placeholder
        self._tabs.append(tab)
        self.stack.addWidget(page)
        index = self.tabbar.addTab("Без имени")
        close_button = QToolButton(self.tabbar)
        close_button.setIcon(self.icon('close'))
        close_button.setIconSize(QSize(12,12))
        close_button.setFixedSize(20,20)
        close_button.setToolTip('Закрыть вкладку (Ctrl+W)')
        close_button.clicked.connect(lambda _checked=False, target=tab: self.close_tab(self._tabs.index(target)) if target in self._tabs else None)
        self.tabbar.setTabButton(index,QTabBar.ButtonPosition.RightSide,close_button)
        self.tabbar.setTabToolTip(index, "Новый документ")
        self.stack.setCurrentIndex(index)
        self.tabbar.setCurrentIndex(index)
        if path:
            self._open_into(tab, path)
        elif not silent:
            self.show_status("Новый документ: откройте PDF (Ctrl+O) или перетащите файл")
        self._sync_shell()
        return tab

    def close_tab(self, index: int) -> bool:
        if not (0 <= index < len(self._tabs)):
            return False
        tab = self._tabs[index]
        decision = "discard"
        if tab.has_document and tab.dirty:
            decision = self._confirm_dirty(tab, "Закрыть документ")
            if decision == "cancel":
                return False
        if decision == "save" and not self.save_tab(tab):
            return False
        if tab is self._compare_tab:
            self.close_comparison()
        self._release_recovery(tab, discard=True)
        self._remove_tab(tab)
        self.show_status("Вкладка закрыта")
        return True

    @property
    def comparison_tab(self) -> Optional[EditorTab]:
        """Open tab mirrored by the independent, read-only comparison view."""
        return self._compare_tab

    @property
    def comparison_view(self) -> Any:
        return self._compare_view

    @property
    def comparison_session(self) -> Any:
        return self._compare_session

    def compare_documents(self) -> bool:
        """Show another open PDF beside the active document."""
        current = self.current_tab
        candidates = [tab for tab in self._tabs
                      if tab is not current and tab.has_document]
        if current is None or not current.has_document:
            self.show_status("Сначала откройте документ для сравнения")
            return False
        if not candidates:
            self.show_status("Откройте ещё один PDF, чтобы сравнивать документы рядом")
            return False
        if self._compare_panel is not None and self._compare_tab in candidates:
            self._compare_panel.show()
            self._schedule_compare_layout()
            self._compare_selector.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return self.set_comparison_tab(candidates[0])

    def _ensure_comparison_panel(self) -> None:
        if self._compare_panel is not None:
            return
        panel = QFrame()
        panel.setObjectName("CompareDocumentPanel")
        panel.setMinimumWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 0)
        layout.setSpacing(6)
        header = QWidget(panel)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(6)
        title = QLabel("Сравнение · только чтение", header)
        title.setObjectName("CompareDocumentTitle")
        title.setProperty("role", "property")
        header_layout.addWidget(title, 1)
        selector = QComboBox(header)
        selector.setObjectName("CompareDocumentSelector")
        selector.setAccessibleName("Документ для сравнения")
        selector.setMinimumContentsLength(12)
        selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        header_layout.addWidget(selector, 2)
        close_button = QToolButton(header)
        close_button.setObjectName("CloseComparison")
        close_button.setText("×")
        close_button.setToolTip("Закрыть сравнение")
        close_button.setAccessibleName("Закрыть сравнение")
        close_button.clicked.connect(self.close_comparison)
        header_layout.addWidget(close_button)
        layout.addWidget(header)
        self._compare_title = title
        self._compare_selector = selector
        selector.currentIndexChanged.connect(self._on_comparison_selection)
        panel.hide()
        self._compare_splitter.addWidget(panel)
        self._compare_splitter.setStretchFactor(1, 1)
        self._compare_panel = panel

    def set_comparison_tab(self, tab: EditorTab) -> bool:
        """Clone the current PDF state into an independent right-hand reader."""
        source = self.current_tab
        if source is None or not source.has_document or tab is source or tab not in self._tabs:
            self.show_status("Выберите другой открытый PDF для сравнения")
            return False
        if not tab.has_document:
            self.show_status("В выбранной вкладке нет открытого PDF")
            return False
        view_class = document_view_class()
        if view_class is None:
            self.show_status("Область сравнения недоступна: модуль просмотра не подключён")
            return False
        self._ensure_comparison_panel()
        self._close_comparison_copy()
        session = self._session_factory()
        try:
            snapshot = tab.session._make_snapshot()
            session.open_bytes(snapshot.data, title=tab.title or "Документ.pdf")
            session._restore(snapshot)
            view = view_class(session, parent=self._compare_panel)
            view.set_readonly(True)
            view.set_tool("select")
            view.set_zoom(float(getattr(tab.view, "zoom", 1.0) or 1.0))
        except Exception as exc:  # noqa: BLE001 - comparison must not affect source tabs
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            self.show_status("Не удалось открыть копию для сравнения: %s" % exc,
                             timeout=9000)
            return False
        layout = self._compare_panel.layout()
        layout.addWidget(view, 1)
        self._compare_session = session
        self._compare_view = view
        self._compare_tab = tab
        view.destroyed.connect(lambda _obj=None, session=session: session.close())
        self._compare_panel.show()
        self._sync_comparison_choices()
        self._schedule_compare_layout()
        self.show_status("Сравнение открыто: %s" % (tab.title or "Документ"))
        return True

    def _close_comparison_copy(self) -> None:
        view = self._compare_view
        self._compare_view = None
        self._compare_session = None
        if view is not None:
            view.hide()
            view.deleteLater()
        self._compare_tab = None

    def close_comparison(self) -> None:
        if self._compare_panel is None:
            return
        self._close_comparison_copy()
        self._compare_panel.hide()
        if self._compare_splitter is not None:
            self._compare_programmatic_resize = True
            self._compare_splitter.setSizes([1, 0])
            self._compare_programmatic_resize = False
        self.show_status("Сравнение закрыто")

    def _on_compare_splitter_moved(self, position: int, index: int) -> None:
        if self._compare_programmatic_resize or self._compare_splitter is None:
            return
        sizes = self._compare_splitter.sizes()
        total = sum(sizes)
        if total > 0 and len(sizes) == 2:
            self._compare_ratio = max(0.2, min(0.8, sizes[0] / total))

    def _schedule_compare_layout(self) -> None:
        if (self._compare_layout_pending or self._compare_splitter is None or
                self._compare_panel is None or not self._compare_panel.isVisible()):
            return
        self._compare_layout_pending = True
        QTimer.singleShot(0, self._apply_compare_layout)

    def _apply_compare_layout(self) -> None:
        self._compare_layout_pending = False
        splitter = self._compare_splitter
        if splitter is None or self._compare_panel is None or not self._compare_panel.isVisible():
            return
        sizes = splitter.sizes()
        available = sum(sizes)
        if available <= 0:
            available = splitter.width() - splitter.handleWidth()
        if available <= 0:
            return
        # Start with equal document widths. After the user moves the divider,
        # keep that proportion as the window grows or shrinks. Clamp each pane
        # to a useful width when the surrounding sidebar/panel leave less room.
        minimum_each = min(240, available // 2)
        left = round(available * self._compare_ratio)
        left = max(minimum_each, min(available - minimum_each, left))
        right = max(0, available - left)
        self._compare_programmatic_resize = True
        try:
            splitter.setSizes([left, right])
        finally:
            self._compare_programmatic_resize = False


    def _on_comparison_selection(self, index: int) -> None:
        if self._compare_syncing or self._compare_selector is None or index < 0:
            return
        tab = self._compare_selector.itemData(index, Qt.ItemDataRole.UserRole)
        if isinstance(tab, EditorTab) and tab is not self.current_tab:
            self.set_comparison_tab(tab)

    def _sync_comparison_choices(self) -> None:
        selector = self._compare_selector
        if selector is None or self._compare_view is None:
            return
        primary = self.current_tab
        candidates = [tab for tab in self._tabs
                      if tab is not primary and tab.has_document]
        target = self._compare_tab
        if target not in candidates:
            if candidates:
                target = candidates[0]
            else:
                self.close_comparison()
                return
        self._compare_syncing = True
        try:
            selector.clear()
            selected = -1
            for tab in candidates:
                label = tab.title or "Документ"
                if tab.dirty:
                    label += " · не сохранено"
                selector.addItem(label, tab)
                if tab is target:
                    selected = selector.count() - 1
            selector.setCurrentIndex(selected)
        finally:
            self._compare_syncing = False
        if self._compare_tab is not target:
            self.set_comparison_tab(target)

    def _refresh_comparison(self, tab: EditorTab) -> None:
        if tab is not self._compare_tab or self._compare_session is None:
            return
        view = self._compare_view
        if view is None:
            return
        try:
            zoom = float(getattr(view, "zoom", 1.0) or 1.0)
            snapshot = tab.session._make_snapshot()
            self._compare_session.open_bytes(snapshot.data, title=tab.title or "Документ.pdf")
            self._compare_session._restore(snapshot)
            view.reload(True)
            view.set_zoom(zoom)
        except Exception as exc:  # noqa: BLE001 - leave primary editing untouched
            self.show_status("Не удалось обновить сравнение: %s" % exc, timeout=8000)


    def _remove_tab(self, tab: EditorTab) -> None:
        """Убрать вкладку и её виджет (без проверки несохранённых изменений)."""
        if tab not in self._tabs:
            return
        index = self._tabs.index(tab)
        self.tabbar.blockSignals(True)
        self.tabbar.removeTab(index)
        self.tabbar.blockSignals(False)
        self._tabs.pop(index)
        page = tab.page_widget
        if page is not None:
            self.stack.removeWidget(page)
            _dispose_widget(page)
        self._drop_view(tab)
        if not self._tabs:
            self.new_tab(silent=True)
        self.stack.setCurrentIndex(min(index, len(self._tabs) - 1))
        self.tabbar.setCurrentIndex(self.stack.currentIndex())
        self._sync_shell()

    def close_current_tab(self) -> bool:
        return self.close_tab(self._tabs.index(self.current_tab)) if self.current_tab else False

    def _on_tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._tabs):
            self.stack.setCurrentIndex(index)
            self._sync_shell()

    def _move_tab(self, previous: int, target: int) -> None:
        active = self.current_tab
        tab = self._tabs.pop(previous)
        self._tabs.insert(target,tab)
        self.stack.blockSignals(True)
        self.stack.removeWidget(tab.page_widget)
        self.stack.insertWidget(target,tab.page_widget)
        if active is not None:
            self.stack.setCurrentWidget(active.page_widget)
        self.stack.blockSignals(False)
        self._sync_shell()

    def create_document_dialog(self) -> None:
        choice, accepted = QInputDialog.getItem(self,'Создать PDF','Размер страницы',
            ['A4 — книжная','A4 — альбомная','A5 — книжная'],0,False)
        if accepted:
            sizes={'A4 — книжная':(595.28,841.89),'A4 — альбомная':(841.89,595.28),
                   'A5 — книжная':(419.53,595.28)}
            self.new_document(*sizes[choice])

    def import_document_dialog(self) -> bool:
        """Choose an Office/TXT document and convert it to an unsaved PDF tab."""
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Создать PDF из файла", self._last_dir, IMPORT_DOCUMENT_FILTER
        )
        if not path:
            return False
        self._last_dir = os.path.dirname(path) or self._last_dir
        self._last_saved_dir = self._last_dir
        title = Path(path).stem + ".pdf"
        return self._start_document_import([path], title)

    def import_images_dialog(self) -> bool:
        """Choose images, target page size, and create one multi-page PDF."""
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Изображения в один PDF", self._last_dir, IMPORT_IMAGE_FILTER
        )
        if not paths:
            return False
        self._last_dir = os.path.dirname(paths[0]) or self._last_dir
        self._last_saved_dir = self._last_dir
        labels = ["A4 — книжная", "A4 — альбомная", "A5 — книжная", "Letter"]
        choice, accepted = QInputDialog.getItem(
            self, "Размер страницы PDF", "Размер каждой страницы:", labels, 0, False
        )
        if not accepted:
            return False
        sizes = {
            "A4 — книжная": "A4",
            "A4 — альбомная": (841.89, 595.28),
            "A5 — книжная": "A5",
            "Letter": "Letter",
        }
        title = Path(paths[0]).stem + ".pdf"
        return self._start_document_import(paths, title, images=True, page_size=sizes[choice])

    def _import_service(self) -> Any:
        if self._document_import_service is None:
            try:
                from .document_import import DocumentImportService
            except ImportError:  # pragma: no cover - direct src imports
                from document_import import DocumentImportService  # type: ignore[no-redef]
            self._document_import_service = DocumentImportService()
        return self._document_import_service

    def _start_document_import(self, paths: Sequence[str], title: str, *,
                               images: bool = False, page_size: Any = "A4") -> bool:
        if not paths:
            return False
        if self._import_worker is not None and self._import_worker.isRunning():
            self.show_status("Дождитесь завершения текущего импорта")
            return False
        worker = _DocumentImportWorker(
            self._import_service(), paths, title, images=images, page_size=page_size
        )
        self._import_worker = worker
        _ACTIVE_IMPORT_WORKERS.add(worker)
        worker.converted.connect(self._on_import_conversion_succeeded)
        worker.failed.connect(self._on_import_conversion_failed)
        worker.finished.connect(self._on_import_worker_finished)
        worker.finished.connect(lambda target=worker: _ACTIVE_IMPORT_WORKERS.discard(target))
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._import_cursor_active = True
        self.show_status("Подготовка PDF из файла…")
        worker.start()
        return True

    def _on_import_conversion_succeeded(self, data: bytes, title: str,
                                        source_folder: str) -> None:
        if self._closing:
            return
        tab = self.new_tab(silent=True)
        try:
            tab.session.open_bytes(data, title=title)
            # Imported content has no source path and must be saved as a new file.
            tab.session._touch()
        except Exception as exc:  # noqa: BLE001
            self._remove_tab(tab)
            self._report_error("Не удалось открыть созданный PDF", exc)
            return
        tab.path = None
        tab.title = title
        tab.page_index = 0
        tab.zoom = 1.0
        tab.selection = None
        tab.thumbs = {}
        tab.thumbs_revision = None
        tab.mode = MODE_READ
        tab.tool = TOOL_CURSOR
        tab.recovery = RecoveryState(key=uuid.uuid4().hex)
        if source_folder:
            self._last_dir = source_folder
            self._last_saved_dir = source_folder
        self._ensure_view(tab)
        self._refresh_tab(tab, pages_changed=True)
        self._sync_shell(pages_changed=True)
        self.show_status("Создан PDF: %s. Сохраните его в выбранную папку." % title)

    def _on_import_conversion_failed(self, message: str) -> None:
        if self._closing:
            return
        self._report_message("Не удалось создать PDF", str(message or "Конвертация завершилась с ошибкой."))

    def _on_import_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._import_worker:
            self._import_worker = None
        if self._import_cursor_active:
            QApplication.restoreOverrideCursor()
            self._import_cursor_active = False

    def new_document(self, width: float=595.28, height: float=841.89) -> EditorTab:
        tab=self.current_tab
        if tab is None or tab.has_document:
            tab=self.new_tab(silent=True)
        tab.session.new(width,height)
        tab.session._touch()
        tab.title='Документ.pdf'
        tab.recovery.key=uuid.uuid4().hex
        self._ensure_view(tab)
        self._sync_shell(pages_changed=True)
        self.set_mode(MODE_EDIT)
        self.show_status('Создан новый PDF. Можно добавлять текст и изображения.')
        return tab

    def insert_blank_page(self) -> None:
        tab=self.current_tab
        if tab is None or not tab.has_document:
            return
        position=tab.session.insert_blank_page(tab.page_index+1)
        self._after_change(tab,pages_changed=True)
        self.goto_page(position)

    def move_current_page(self, delta: int) -> None:
        tab=self.current_tab
        if tab is None or not tab.has_document:
            return
        target=tab.page_index+delta
        if not 0 <= target < self.page_count:
            return
        tab.session.move_page(tab.page_index,target)
        self._after_change(tab,pages_changed=True)
        self.goto_page(target)

    def merge_pdf_dialog(self) -> None:
        paths,_=QFileDialog.getOpenFileNames(self,'Добавить страницы из PDF',self._last_dir,PDF_FILTER)
        if paths:
            self.append_documents(paths)

    def append_documents(self, paths: Sequence[str]) -> bool:
        tab=self.current_tab
        if tab is None or not tab.has_document:
            return False
        try:
            tab.session.append(list(paths))
        except Exception as exc:
            self._report_error('Не удалось добавить страницы',exc)
            return False
        self._after_change(tab,pages_changed=True)
        return True

    def _drop_view(self, tab: EditorTab) -> None:
        controller = getattr(tab, "source_controller", None)
        if controller is not None:
            controller.sync(False)
        view = tab.view
        tab.view = None
        if view is not None:
            try:
                view.setParent(None)
                view.deleteLater()
            except Exception:  # noqa: BLE001 - чужой виджет
                pass

    # ------------------------------------------------------------------
    # Открытие документа
    # ------------------------------------------------------------------
    def open_path(self, path: str, *, new_tab: bool = False) -> bool:
        """Открыть PDF в текущей пустой вкладке или в новой (контракт main.py)."""
        target = str(path or "").strip()
        if not target:
            return False
        tab = self.current_tab
        created = False
        if tab is None or new_tab or tab.has_document:
            tab = self.new_tab(silent=True)
            created = True
        opened = self._open_into(tab, target)
        if not opened and created and not tab.has_document:
            # Неудачное открытие не оставляет пустую вкладку-призрак.
            self._remove_tab(tab)
            self.show_status("Файл не открыт: %s" % os.path.basename(target))
        return opened

    def open_files(self, paths: Sequence[str]) -> int:
        opened = 0
        for position, path in enumerate(paths):
            if self.open_path(path, new_tab=position > 0 or opened > 0):
                opened += 1
        return opened

    def _open_into(self, tab: EditorTab, path: str, password: Optional[str] = None) -> bool:
        target = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(target):
            self._report_message("Файл не найден", "Не удалось открыть файл:\n%s" % target)
            return False
        if tab.has_document:
            self._release_recovery(tab, discard=True)
        attempts = 0
        current = password
        while True:
            try:
                tab.session.open(target, current)
            except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо падения
                if self._looks_like_password_error(exc) and attempts < 2:
                    entered = self._ask_password(target)
                    attempts += 1
                    if entered is None:
                        self.show_status("Открытие отменено")
                        return False
                    current = entered
                    continue
                self._report_error(
                    "Не удалось открыть файл", exc,
                    "Возможно, файл повреждён, защищён паролем или это не PDF.",
                )
                return False
            break
        tab.path = target
        tab.title = os.path.basename(target)
        tab.page_index = 0
        tab.zoom = 1.0
        tab.selection = None
        tab.thumbs = {}
        tab.thumbs_revision = None
        tab.recovery = RecoveryState()
        self._last_dir = os.path.dirname(target) or self._last_dir
        self._last_saved_dir = self._last_dir
        self._ensure_view(tab)
        self._refresh_tab(tab, pages_changed=True)
        self.show_status("Открыт файл: %s" % tab.title)
        self.toast("Открыт файл: %s" % tab.title)
        self._show_warnings(tab)
        return True

    def _make_placeholder(self) -> QWidget:
        """Честная заглушка центра: приглашение открыть файл или ожидание холста."""
        frame = QFrame()
        frame.setObjectName("Placeholder")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(10)
        layout.addStretch(1)
        title = QLabel("Документ не открыт")
        title.setProperty("role", "empty-title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        hint = QLabel("Откройте PDF кнопкой ниже, сочетанием Ctrl+O или перетащите "
                      "файл в окно. Исходный файл остаётся неизменным.")
        hint.setProperty("role", "sub")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        open_button = QPushButton("Открыть PDF…")
        open_button.setProperty("role", "primary")
        open_button.setIcon(self._icons.icon("open", size=16, color="#ffffff"))
        open_button.clicked.connect(self.open_dialog)
        button_row.addWidget(open_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        note = QLabel("")
        note.setProperty("role", "tiny")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        frame._pd_note = note  # type: ignore[attr-defined]
        return frame

    def _update_placeholder(self, tab: EditorTab) -> None:
        self._update_placeholder_note(tab.placeholder)

    def _update_placeholder_note(self, frame: Optional[QWidget]) -> None:
        """Подсказка под заглушкой: ждём холст документа или просим открыть файл."""
        if frame is None:
            return
        note = getattr(frame, "_pd_note", None)
        if note is None:
            return
        available, error = document_view_status()
        if available:
            note.setText("")
        else:
            note.setText("Холст документа (src/document_view.py) ещё не подключён — "
                         "страницы появятся после его готовности. Причина: %s"
                         % (error or "модуль не найден"))

    def _ensure_view(self, tab: EditorTab) -> Any:
        """Подключить холст документа (отложенный импорт) или оставить заглушку."""
        cls = document_view_class()
        if cls is None:
            self._update_placeholder(tab)
            return None
        if tab.view is None:
            try:
                view = cls(tab.session, parent=tab.page_widget)
            except Exception as exc:  # noqa: BLE001 - модуль чужого агента
                self._note_error("Холст документа", exc)
                self._update_placeholder(tab)
                return None
            tab.view = view
            layout = tab.page_widget.layout() if tab.page_widget is not None else None
            if layout is not None:
                layout.addWidget(view)
            if tab.placeholder is not None:
                tab.placeholder.hide()
            self._connect_view(tab, view)
            self._apply_stage_palette(view)
            try:
                from .source_edit_ui import SourceContentController
                tab.source_controller = SourceContentController(self, tab)
            except Exception as exc:  # noqa: BLE001
                self._note_error("Редактор содержимого PDF", exc)
            if tab.source_controller is not None:
                tab.source_controller.sync(
                    tab is self.current_tab and tab.mode == MODE_EDIT and tab.tool == TOOL_CONTENT
                )
        return tab.view

    def _drop_guide(self,axis: str,global_position: QPoint) -> None:
        tab=self.current_tab
        if tab is None:
            return
        for canvas in self._view_canvases(tab):
            local=canvas.mapFromGlobal(global_position)
            if canvas.page_rect().contains(local):
                point=canvas._to_pdf(local)
                canvas.add_guide(axis,point[0] if axis=='v' else point[1])
                self.set_tool(TOOL_CURSOR)
                self.show_status('Направляющая: перетащите для перемещения, Delete для удаления.')
                return

    def _connect_view(self, tab: EditorTab, view: Any) -> None:
        def connect(name: str, handler: Callable[..., Any]) -> None:
            signal = getattr(view, name, None)
            if signal is None:
                return
            try:
                signal.connect(handler)
            except Exception as exc:  # noqa: BLE001 - необязательный сигнал
                self._note_error("Сигнал %s" % name, exc)

        connect("page_changed", lambda index, tab=tab: self._on_view_page_changed(tab, index))
        connect("document_changed", lambda tab=tab: self._on_view_document_changed(tab))
        connect("selection_changed",
                lambda page, index, tab=tab: self._on_view_selection(tab, page, index))
        connect("error", lambda message: self._on_view_message(message, error=True))
        connect("notice", lambda message: self._on_view_message(message, error=False))

    def _on_view_message(self, message: Any, *, error: bool) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self.show_status(("Ошибка: " if error else "") + text, timeout=9000)

    # -- чтение состояния чужого холста (защищённые обращения) ------------
    def _view_canvases(self, tab: EditorTab) -> list[Any]:
        view = tab.view
        if view is None:
            return []
        canvases = getattr(view, "canvases", None)
        if callable(canvases):
            try:
                canvases = canvases()
            except Exception:  # noqa: BLE001
                return []
        try:
            return list(canvases or [])
        except TypeError:
            return []

    def view_page_index(self, tab: EditorTab) -> int:
        view = tab.view
        if view is None:
            return tab.page_index
        value = getattr(view, "current_page_index", tab.page_index)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                return tab.page_index
        try:
            return int(value)
        except (TypeError, ValueError):
            return tab.page_index

    def _call_view(self, tab: EditorTab, name: str, *args: Any,
                   fallback: Optional[str] = None) -> Any:
        view = tab.view
        if view is None:
            return None
        method = getattr(view, name, None)
        if method is None:
            return None
        try:
            return method(*args)
        except Exception as exc:  # noqa: BLE001 - чужой модуль не должен ронять окно
            if fallback is not None:
                try:
                    return method(fallback, *args[1:])
                except Exception:  # noqa: BLE001
                    pass
            self._note_error("%s()" % name, exc)
            return None

    # ------------------------------------------------------------------
    # Обновление интерфейса
    # ------------------------------------------------------------------
    def _refresh_tab(self, tab: EditorTab, *, pages_changed: bool = False,
                     preserve_scroll: bool = True) -> None:
        if pages_changed:
            tab.thumbs = {}
            tab.thumbs_revision = None
            tab.page_index = max(0, min(tab.page_index, max(0, self._tab_page_count(tab) - 1)))
        view = tab.view
        if view is not None:
            self._call_view(tab, "reload", preserve_scroll)
            self._call_view(tab, "set_zoom", float(tab.zoom))
            self._push_tool()
            self._push_style(tab)
            if tab.selection is not None:
                self._select_canvas_annotation(tab)
        if tab.has_document:
            if tab.placeholder is not None:
                tab.placeholder.hide()
        else:
            if tab.placeholder is not None:
                tab.placeholder.show()
            self._update_placeholder(tab)
        if tab is self.current_tab:
            self._sync_shell(pages_changed=pages_changed)

    def _on_view_page_changed(self, tab: EditorTab, index: Any) -> None:
        try:
            tab.page_index = int(index)
        except (TypeError, ValueError):
            return
        if tab is self.current_tab:
            self._sync_shell()

    def _on_view_document_changed(self, tab: EditorTab) -> None:
        """Модель изменилась (в том числе набор текста на странице).

        Панели не пересобираются «на каждый знак»: обновляются только строка
        вкладок, сведения и кнопки, а правая панель — лишь при смене структуры.
        Иначе элементы управления отсоединялись и поле теряло фокус.
        """
        if tab is self._compare_tab:
            self._refresh_comparison(tab)
        if tab is self.current_tab:
            self._sync_chrome()
            self.inspector.refresh()

    def _on_view_selection(self, tab: EditorTab, page: Any, index: Any) -> None:
        try:
            page_index = int(page)
            annotation_index = int(index)
        except (TypeError, ValueError):
            return
        selection = None if annotation_index < 0 else (page_index, annotation_index)
        changed = selection != tab.selection
        tab.selection = selection
        if tab is self.current_tab:
            # Панель пересобирается только при смене выбора: иначе выделение
            # объекта на холсте уничтожало бы поле, в котором идёт ввод.
            if changed:
                self.inspector.refresh()
            if tab.selection is not None:
                self._sync_style_from_selection(tab)

    def _sync_shell(self, *, pages_changed: bool = False) -> None:
        """Синхронизировать все части окна с текущей вкладкой."""
        tab = self.current_tab
        self._sync_tabbar()
        if hasattr(self, "compare_action"):
            self.compare_action.setEnabled(
                tab is not None and tab.has_document and
                any(candidate is not tab and candidate.has_document for candidate in self._tabs)
            )
        if tab is None:
            self.doc_meta.setText("")
            self.page_status.setText("")
            self.mode_status.setText(theme.MODE_STATUS[MODE_READ])
            self.inspector.rebuild()
            self._update_actions()
            return
        self._sync_comparison_choices()
        for name, button in self.mode_buttons.items():
            button.setChecked(name == tab.mode)
            allowed = (name not in (MODE_EDIT, MODE_COMMENT, MODE_PAGES) or
                       getattr(tab.session, 'has_edit_permission', True))
            if name == MODE_FILL:
                allowed = getattr(tab.session, 'can_fill_forms', True)
            button.setEnabled(allowed)
        self.mode_status.setText(theme.MODE_STATUS[tab.mode])
        self.zoom_label.setText("%d%%" % round(tab.zoom * 100))
        self._rebuild_tools()
        self._sync_stage()
        self._sync_source_controllers()
        if pages_changed or not self.thumbnails.rows:
            self._rebuild_thumbs(tab)
        else:
            self.thumbnails.set_active(tab.page_index)
        self.inspector.rebuild()
        self._update_placeholder(tab)
        self._sync_chrome()

    def _sync_source_controllers(self) -> None:
        active_tab = self.current_tab
        for item in self._tabs:
            controller = getattr(item, "source_controller", None)
            if controller is None:
                continue
            controller.sync(item is active_tab and item.mode == MODE_EDIT
                            and item.tool == TOOL_CONTENT)
        self._sync_form_authoring()

    def _sync_form_authoring(self) -> None:
        active_tab = self.current_tab
        for tab in self._tabs:
            active = (tab is active_tab and tab.has_document and tab.mode == MODE_EDIT
                      and tab.tool == TOOL_FORM_AUTHOR)
            controller = tab.form_author
            canvases = self._view_canvases(tab) if active else []
            canvas = (canvases[tab.page_index] if 0 <= tab.page_index < len(canvases)
                      else None)
            if controller is not None and (not active or controller.canvas is not canvas):
                controller.detach()
                controller.deleteLater()
                tab.form_author = controller = None
            if canvas is None:
                continue
            if controller is None:
                from .form_editor import FormAuthorController
                controller = FormAuthorController(tab.session, canvas, tab.page_index)
                tab.form_author = controller
                controller.fields_changed.connect(
                    lambda tab=tab: QTimer.singleShot(0, lambda: self._form_author_changed(tab)))
                controller.error.connect(lambda message: self.show_status(message, timeout=8000))
            else:
                controller.refresh()

    def _form_author_changed(self, tab: EditorTab) -> None:
        if tab not in self._tabs or not tab.has_document:
            return
        self._after_change(tab)
        self.show_status('Форма изменена — проверьте поля в режиме «Заполнение»')

    def _sync_chrome(self) -> None:
        """Лёгкое обновление: вкладки, сведения, кнопки — без пересборки панелей."""
        tab = self.current_tab
        if tab is None:
            return
        self._sync_tabbar()
        self._update_doc_meta(tab)
        self._update_page_status(tab)
        self._update_actions()
        self._apply_stage_palette(tab.view)

    def _schedule_inspector_rebuild(self) -> None:
        """Пересобрать панель свойств после обработки текущего сигнала.

        Так элемент, из которого пришло изменение (список, поле заметки,
        кнопка выбора файла), не уничтожается во время собственного сигнала.
        """
        QTimer.singleShot(0, self.inspector.rebuild)

    def _sync_tabbar(self) -> None:
        for index, tab in enumerate(self._tabs):
            if index >= self.tabbar.count():
                break
            title = tab.title if tab.has_document else "Без имени"
            self.tabbar.setTabText(index, title)
            self.tabbar.setTabToolTip(index, tab.path or "Новый документ")
            color = theme.ACCENT if tab.dirty else editor_icons.INK
            self.tabbar.setTabIcon(index, self._icons.icon("file", size=12, color=color))

    def _update_doc_meta(self, tab: EditorTab) -> None:
        if not tab.has_document:
            self.doc_meta.setText("")
            return
        count = self._tab_page_count(tab)
        label = pages_label(count)
        size = ""
        try:
            size = size_label(*tab.session.page_size(0))
        except Exception:  # noqa: BLE001
            size = ""
        text = "%s · %s" % (label, size) if size else label
        if tab.dirty:
            text += " · не сохранено"
        self.doc_meta.setText(text)

    def _update_page_status(self, tab: EditorTab) -> None:
        count = self._tab_page_count(tab)
        if count <= 0:
            self.page_status.setText("")
            return
        self.page_status.setText("Страница %d из %d" % (tab.page_index + 1, count))

    def _tab_page_count(self, tab: EditorTab) -> int:
        try:
            return int(getattr(tab.session, "page_count", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _update_actions(self) -> None:
        session = self._session
        has_document = self.page_count > 0 and session is not None
        self.save_button.setEnabled(has_document)
        self.print_button.setEnabled(has_document and getattr(session, 'can_print', True))
        self.search_button.setEnabled(has_document)
        self.fit_button.setEnabled(has_document)
        self.zoom_in_button.setEnabled(has_document)
        self.zoom_out_button.setEnabled(has_document)
        can_undo = bool(self._session_call("can_undo"))
        can_redo = bool(self._session_call("can_redo"))
        self.undo_button.setEnabled(can_undo)
        self.redo_button.setEnabled(can_redo)

    def _session_call(self, name: str, *args: Any) -> Any:
        session = self._session
        method = getattr(session, name, None)
        if method is None:
            return None
        try:
            return method(*args)
        except Exception as exc:  # noqa: BLE001
            self._note_error(name, exc)
            return None

    def _sync_stage(self) -> None:
        tab = self.current_tab
        mode = tab.mode if tab is not None else MODE_READ
        self.stage_stack.setCurrentIndex(1 if mode == MODE_PAGES else 0)
        if mode == MODE_PAGES and tab is not None:
            self._rebuild_pages_grid(tab)

    # ------------------------------------------------------------------
    # Миниатюры
    # ------------------------------------------------------------------
    def _thumbnails(self, tab: EditorTab) -> list[Optional[QPixmap]]:
        revision = self._session_revision(tab)
        if tab.thumbs and tab.thumbs_revision == revision:
            return [tab.thumbs.get(index) for index in range(self._tab_page_count(tab))]
        tab.thumbs = {}
        tab.thumbs_revision = revision
        count = min(self._tab_page_count(tab), MAX_THUMBNAILS)
        for index in range(count):
            tab.thumbs[index] = self._render_thumb(tab, index)
        return [tab.thumbs.get(index) for index in range(self._tab_page_count(tab))]

    def _render_thumb(self, tab: EditorTab, index: int) -> Optional[QPixmap]:
        try:
            width, height = tab.session.page_size(index)
        except Exception:  # noqa: BLE001
            return None
        if width <= 0 or height <= 0:
            return None
        scale = min(theme.THUMB_WIDTH / width, theme.THUMB_HEIGHT / height)
        try:
            image = tab.session.render(index, max(0.05, scale))
        except Exception as exc:  # noqa: BLE001 - миниатюра не критична
            self._note_error("Миниатюра стр. %d" % (index + 1), exc)
            return None
        return _pil_to_pixmap(image)

    def _rebuild_thumbs(self, tab: EditorTab) -> None:
        title = tab.title if tab.has_document else "Документ не открыт"
        meta = "Исходный файл только для чтения"
        self.thumbnails.rebuild(self._thumbnails(tab), tab.page_index, title, meta)

    def _rebuild_pages_grid(self, tab: EditorTab) -> None:
        self.pages_grid.rebuild(self._thumbnails(tab), tab.page_index)
        self.pages_grid.set_note(UNSUPPORTED_NOTES.get(MODE_PAGES, ""))

    # ------------------------------------------------------------------
    # Навигация, масштаб, страницы
    # ------------------------------------------------------------------
    def goto_page(self, index: int) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        count = self._tab_page_count(tab)
        index = max(0, min(int(index), count - 1))
        tab.page_index = index
        if tab.view is not None:
            self._call_view(tab, "focus_page", index)
        self.thumbnails.set_active(index)
        self.pages_grid.set_active(index)
        self._update_page_status(tab)

    def step_page(self, delta: int) -> None:
        tab = self.current_tab
        if tab is None:
            return
        self.goto_page(tab.page_index + int(delta))

    def set_zoom(self, value: float) -> None:
        tab = self.current_tab
        if tab is None:
            return
        try:
            zoom = float(value)
        except (TypeError, ValueError):
            return
        zoom = max(theme.ZOOM_MIN, min(theme.ZOOM_MAX, zoom))
        tab.zoom = zoom
        self.zoom_label.setText("%d%%" % round(zoom * 100))
        self._call_view(tab, "set_zoom", zoom)
        canvas = self._canvas
        if canvas is not None and tab.view is None:
            try:
                canvas.set_zoom(zoom)
            except Exception:  # noqa: BLE001
                pass

    def zoom_in(self) -> None:
        tab = self.current_tab
        if tab is not None:
            self.set_zoom(tab.zoom + theme.ZOOM_STEP)

    def zoom_out(self) -> None:
        tab = self.current_tab
        if tab is not None:
            self.set_zoom(tab.zoom - theme.ZOOM_STEP)

    def fit_page(self) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        try:
            width, height = tab.session.page_size(tab.page_index)
        except Exception:  # noqa: BLE001
            return
        available_h = max(120, self._stage.height() - 70)
        available_w = max(120, self._stage.width() - 60)
        scale = min(1.0, available_h / max(1.0, height), available_w / max(1.0, width))
        self.set_zoom(scale)
        self.show_status("Страница целиком: %d%%" % round(scale * 100))

    def fit_width(self) -> None:
        tab=self.current_tab
        if tab is None or not tab.has_document:
            return
        width,_=tab.session.page_size(tab.page_index)
        view=tab.view
        available=max(120,(view.width() if view is not None else self._stage.width())-46)
        self.set_zoom(available/max(1,width))

    # ------------------------------------------------------------------
    # Отмена/повтор
    # ------------------------------------------------------------------
    def undo(self) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        if not self._session_call("can_undo"):
            self.toast("Нет действий для отмены")
            return
        self._session_call("undo")
        self._after_change(tab)
        self.show_status("Действие отменено")

    def redo(self) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        if not self._session_call("can_redo"):
            self.toast("Нет действий для повтора")
            return
        self._session_call("redo")
        self._after_change(tab)
        self.show_status("Действие повторено")

    def _after_change(self, tab: Optional[EditorTab] = None, *, pages_changed: bool = False) -> None:
        tab = tab or self.current_tab
        if tab is None:
            return
        if tab.view is not None:
            self._call_view(tab, "reload", True)
            self._push_tool()
            if tab.selection is not None:
                self._select_canvas_annotation(tab)
        if tab is self._compare_tab:
            self._refresh_comparison(tab)
        if pages_changed:
            tab.thumbs = {}
            tab.thumbs_revision = None
        if tab is self.current_tab:
            self._sync_shell(pages_changed=pages_changed)

    def rotate_page(self) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        try:
            tab.session.rotate(tab.page_index, clockwise=True)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось повернуть страницу", exc)
            return
        self._after_change(tab, pages_changed=True)
        self.show_status("Страница %d повёрнута на 90°" % (tab.page_index + 1))

    def delete_page(self) -> None:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return
        count = self._tab_page_count(tab)
        if count <= 1:
            self._report_message(
                "Нельзя удалить страницу",
                "В документе осталась одна страница. Удаление последней страницы "
                "запрещено — сохранять было бы нечего.",
            )
            return
        if not self._confirm_delete_page(tab.page_index):
            self.show_status("Удаление страницы отменено")
            return
        try:
            tab.session.delete_page(tab.page_index)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось удалить страницу", exc)
            return
        tab.page_index = max(0, min(tab.page_index, self._tab_page_count(tab) - 1))
        self._after_change(tab, pages_changed=True)
        self.show_status("Страница удалена. Ctrl+Z — вернуть")

    # ------------------------------------------------------------------
    # Аннотации: выбор, стиль, удаление
    # ------------------------------------------------------------------
    def selected_annotation(self) -> Optional[tuple[int, int, Any]]:
        tab = self.current_tab
        if tab is None or tab.selection is None:
            return None
        page, index = tab.selection
        try:
            annotations = list(tab.session.get_annotations(page))
        except Exception:  # noqa: BLE001
            return None
        if 0 <= index < len(annotations):
            return page, index, annotations[index]
        return None

    def effective_style(self) -> dict:
        tab = self.current_tab
        style = dict(tab.style) if tab is not None else {}
        selection = self.selected_annotation()
        if selection is not None:
            annotation = selection[2]
            style.update({
                "font_family": annotation.font_family,
                "font_size": annotation.font_size,
                "color": annotation.color,
                "stroke_width": annotation.stroke_width,
                "bold": bool(annotation.bold),
                "alignment": annotation.alignment,
                "fill_color": str(getattr(annotation, "fill_color", "") or ""),
                "opacity": annotation_opacity_value(getattr(annotation, "opacity", 1.0)),
            })
        return style

    def _sync_style_from_selection(self, tab: EditorTab) -> None:
        selection = self.selected_annotation()
        if selection is None:
            return
        annotation = selection[2]
        tab.style.update({
            "font_family": annotation.font_family,
            "font_size": annotation.font_size,
            "color": annotation.color,
            "stroke_width": annotation.stroke_width,
            "bold": bool(annotation.bold),
            "alignment": annotation.alignment,
            "fill_color": str(getattr(annotation, "fill_color", "") or ""),
            "opacity": annotation_opacity_value(getattr(annotation, "opacity", 1.0)),
        })

    def annotation_edit_allowed(self, tab: Optional[EditorTab] = None) -> bool:
        """Разрешена ли правка пометок: права PDF важнее кнопок интерфейса."""
        tab = tab or self.current_tab
        if tab is None:
            return False
        return bool(getattr(tab.session, "has_edit_permission", True))

    def apply_style(self, field_name: str, value: Any, *, rebuild: bool = True) -> None:
        """Изменить свойство выбранного объекта либо стиль новых объектов.

        ``rebuild=False`` — для непрерывных элементов (ползунок прозрачности):
        пересборка панели уничтожила бы элемент прямо во время перетаскивания.
        """
        tab = self.current_tab
        if tab is None:
            return
        if field_name == "fill_color":
            text = str(value or "").strip()
            if text and not QColor(text).isValid():
                return  # некорректный цвет не меняет ни модель, ни историю
            value = normalize_fill_color(text)
        elif field_name == "opacity":
            value = normalize_annotation_opacity(value)
        if field_name not in ("font_family", "font_size", "color", "stroke_width",
                              "bold", "alignment", "fill_color", "opacity"):
            return
        if (field_name in ("fill_color", "opacity")
                and not self.annotation_edit_allowed(tab)):
            # Элементы панели уже выключены; это защита от вызова в обход неё.
            self.show_status("Документ защищён: изменение свойств недоступно")
            return
        tab.style[field_name] = value
        selection = self.selected_annotation()
        if selection is None:
            self._push_style(tab)
            if rebuild:
                self._schedule_inspector_rebuild()
            return
        page, index, annotation = selection
        if (field_name in ("fill_color", "opacity")
                and getattr(annotation, field_name, None) == value):
            # Значение уже такое: не тратим ступень отмены на пустую запись.
            if rebuild:
                self._schedule_inspector_rebuild()
            return
        updated = dataclasses.replace(annotation, **{field_name: value})
        if not self._apply_annotation_update(tab, page, index, updated,
                                             fields=(field_name,)):
            return
        if rebuild:
            self._schedule_inspector_rebuild()
        self.show_status("Свойство объекта изменено. Ctrl+Z — отменить")

    def preview_style(self, field_name: str, value: Any) -> bool:
        """Показать будущее значение на холсте, не записывая его в документ.

        Нужно при перетаскивании ползунка: промежуточные значения иначе заняли
        бы всю историю отмены (в ней всего 24 ступени), а документ стал бы
        «изменённым» до того, как пользователь отпустил элемент.
        """
        if field_name == "fill_color":
            value = normalize_fill_color(value)
        elif field_name == "opacity":
            value = normalize_annotation_opacity(value)
        else:
            return False
        tab = self.current_tab
        selection = self.selected_annotation()
        if tab is None or selection is None:
            return False
        page, index, annotation = selection
        canvases = self._view_canvases(tab)
        if not (0 <= page < len(canvases)):
            return False
        canvas = canvases[page]
        setter = getattr(canvas, "set_annotations", None)
        if not callable(setter):
            return False
        try:
            annotations = list(tab.session.get_annotations(page))
        except Exception as exc:  # noqa: BLE001
            self._note_error("Предпросмотр свойства", exc)
            return False
        if not (0 <= index < len(annotations)):
            return False
        annotations[index] = dataclasses.replace(annotation, **{field_name: value})
        try:
            setter(annotations, index)
        except TypeError:
            setter(annotations)
        except Exception as exc:  # noqa: BLE001
            self._note_error("Предпросмотр свойства", exc)
            return False
        return True

    def update_selected_text(self, text: str) -> None:
        """Изменить текст выбранного объекта, не пересобирая панель под курсором."""
        tab = self.current_tab
        selection = self.selected_annotation()
        if tab is None or selection is None:
            return
        page, index, annotation = selection
        if annotation.text == text:
            return
        updated = dataclasses.replace(annotation, text=text)
        self._apply_annotation_update(tab, page, index, updated, fields=("text",))

    def _store_annotation(self, tab: EditorTab, page: int, index: int,
                          annotation: Any) -> bool:
        try:
            tab.session.update_annotation(page, index, annotation)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось изменить объект", exc)
            return False
        return True

    def _annotation_at(self, tab: EditorTab, page: int, index: int) -> Any:
        try:
            annotations = list(tab.session.get_annotations(page))
        except Exception:  # noqa: BLE001
            return None
        if 0 <= index < len(annotations):
            return annotations[index]
        return None

    def _apply_annotation_update(self, tab: EditorTab, page: int, index: int,
                                 updated: Any, *, fields: Sequence[str] = ()) -> bool:
        """Записать аннотацию и удержать новое состояние в модели.

        Холст может держать свою (устаревшую) копию объекта в открытом
        редакторе и вернуть её при обновлении страницы. Поэтому после записи
        значение подтверждается в модели и при необходимости восстанавливается.
        """
        if not self._store_annotation(tab, page, index, updated):
            return False
        self._sync_canvas_annotations(tab, page, index)
        if fields and not self._annotation_matches(tab, page, index, updated, fields):
            if not self._store_annotation(tab, page, index, updated):
                return False
            self._sync_canvas_annotations(tab, page, index)
        self._sync_chrome()
        return True

    def _annotation_matches(self, tab: EditorTab, page: int, index: int,
                            updated: Any, fields: Sequence[str]) -> bool:
        current = self._annotation_at(tab, page, index)
        if current is None:
            return False
        return all(getattr(current, name, None) == getattr(updated, name, None)
                   for name in fields)

    def _sync_canvas_annotations(self, tab: EditorTab, page: int, index: int) -> None:
        """Обновить объекты на холсте без полной перезагрузки страниц."""
        canvases = self._view_canvases(tab)
        if not (0 <= page < len(canvases)):
            return
        canvas = canvases[page]
        setter = getattr(canvas, "set_annotations", None)
        if not callable(setter):
            return
        try:
            annotations = list(tab.session.get_annotations(page))
        except Exception as exc:  # noqa: BLE001
            self._note_error("Обновление объектов", exc)
            return
        try:
            setter(annotations, index)
        except TypeError:
            try:
                setter(annotations)
            except Exception as exc:  # noqa: BLE001
                self._note_error("Обновление объектов", exc)
        except Exception as exc:  # noqa: BLE001
            self._note_error("Обновление объектов", exc)

    def edit_annotation_geometry(self) -> None:
        from .annotation_geometry import AnnotationGeometryDialog
        tab = self.current_tab
        selection = self.selected_annotation()
        if tab is None or selection is None or not self.annotation_edit_allowed(tab):
            return
        page, index, annotation = selection
        dialog = AnnotationGeometryDialog(annotation, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.result_annotation()
        if updated == annotation:
            return
        self._apply_annotation_update(tab, page, index, updated,
                                      fields=("x", "y", "width", "height", "points", "font_size"))

    def delete_selected_annotation(self) -> None:
        tab = self.current_tab
        selection = self.selected_annotation()
        if tab is None or selection is None:
            return
        page, index, annotation = selection
        try:
            tab.session.remove_annotation(page, index)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Не удалось удалить объект", exc)
            return
        tab.selection = None
        self._after_change(tab)
        self.show_status("%s удалён. Ctrl+Z — вернуть" % self.kind_label(annotation.kind).capitalize())

    def _select_canvas_annotation(self, tab: EditorTab) -> None:
        if tab.selection is None:
            return
        page, index = tab.selection
        canvases = self._view_canvases(tab)
        if 0 <= page < len(canvases):
            canvas = canvases[page]
            select = getattr(canvas, "select_annotation", None)
            if callable(select):
                try:
                    select(index)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def kind_label(kind: str) -> str:
        return {
            "text": "текст", "highlight": "выделение", "ink": "штрих", "note": "комментарий",
            "rect": "прямоугольник", "ellipse": "эллипс", "line": "линия",
            "arrow": "стрелка", "polygon": "многоугольник", "cloud": "облако", "image": "изображение", "stamp": "штамп",
        }.get(str(kind), str(kind))

    def shape_kind(self) -> str:
        tab = self.current_tab
        return tab.shape_kind if tab is not None else "rect"

    def set_shape_kind(self, kind: str) -> None:
        tab = self.current_tab
        if tab is None or kind not in [value for value, _ in SHAPE_KINDS]:
            return
        tab.shape_kind = kind
        selection = self.selected_annotation()
        if selection is not None and selection[2].kind in dict(SHAPE_KINDS):
            page, index, annotation = selection
            points = list(annotation.points)
            if kind in ('polygon', 'cloud') and annotation.kind not in ('polygon', 'cloud'):
                from .shape_geometry import rectangle_points
                points = rectangle_points(annotation.x, annotation.y, annotation.width, annotation.height)
            elif kind in ('line', 'arrow') and annotation.kind not in ('line', 'arrow'):
                points = [(annotation.x, annotation.y),
                          (annotation.x + annotation.width, annotation.y + annotation.height)]
            elif kind in ('rect', 'ellipse'):
                points = []
            updated = dataclasses.replace(annotation, kind=kind, points=points)
            if not self._apply_annotation_update(tab, page, index, updated, fields=("kind", "points")):
                return
        self._push_tool()
        self._schedule_inspector_rebuild()
        self.show_status('Щёлкайте вершины; Enter — завершить, Escape — отменить'
                         if kind == 'polygon' else "Тип фигуры: %s" % dict(SHAPE_KINDS)[kind])

    def image_width_hint(self) -> Optional[str]:
        tab = self.current_tab
        if tab is None:
            return None
        data = tab.style.get("image_data")
        if not data:
            return "Файл не выбран"
        name = tab.style.get("image_name") or "файл"
        return "Выбрано: %s" % name

    def replace_selected_image(self) -> bool:
        """Заменить картинку ВЫБРАННОГО объекта image/stamp.

        Новое размещение при этом не создаётся: меняются только растровые
        данные, а ``x/y/width/height`` и вершины остаются прежними, поэтому
        объект не переезжает и не меняет размер. Запись в сессию одна — одна
        ступень отмены. Отмена выбора файла и ошибки чтения ничего не меняют.
        """
        tab = self.current_tab
        selection = self.selected_annotation()
        if tab is None or selection is None:
            return False
        page, index, annotation = selection
        if str(getattr(annotation, "kind", "")) not in ("image", "stamp"):
            return False
        if not self.annotation_edit_allowed(tab):
            self.show_status("Документ защищён: замена изображения недоступна")
            return False
        path, _filter = QFileDialog.getOpenFileName(
            self, "Заменить изображение объекта", self._last_dir, IMAGE_FILTER
        )
        if not path:
            return False  # отмена: ни модель, ни история не меняются
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            self._report_error("Не удалось прочитать изображение", exc)
            return False
        if not raw:
            self._report_message("Пустой файл", "Выбранный файл изображения пуст.")
            return False
        if not is_image_payload(raw):
            self._report_message(
                "Не удалось прочитать изображение",
                "Файл «%s» не является изображением PNG/JPEG." % os.path.basename(path),
            )
            return False
        data = base64.b64encode(raw).decode("ascii")
        updated = dataclasses.replace(annotation, image_data=data)
        if not self._apply_annotation_update(tab, page, index, updated,
                                             fields=("image_data",)):
            return False
        self._last_dir = os.path.dirname(path) or self._last_dir
        self._schedule_inspector_rebuild()
        self.show_status("Изображение заменено: позиция и размер объекта сохранены. "
                         "Ctrl+Z — отменить")
        return True

    def replace_source_image(self) -> bool:
        tab = self.current_tab
        controller = getattr(tab, 'source_controller', None)
        block = (getattr(controller, 'selected_local_block', None) or controller.selected_block
                 if controller is not None else None)
        if block is None or block.kind != 'image':
            return False
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Заменить изображение PDF', self._last_dir, IMAGE_FILTER)
        if not path:
            return False
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            self._report_error('Не удалось прочитать изображение', exc)
            return False
        revision = self._session_revision(tab)
        controller.apply_selected(image_data=raw)
        return self._session_revision(tab) != revision

    def choose_image(self, tool_id: str = TOOL_IMAGE) -> bool:
        """Выбрать PNG/JPEG для изображения, штампа или подписи (реальный файл)."""
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return False
        if tool_id == TOOL_STAMP:
            from .stamp_library import StampLibrary
            from .stamp_library_ui import choose_stamp
            try:
                choice = choose_stamp(self, StampLibrary(), error_handler=self.show_status)
            except (OSError, ValueError) as exc:
                self._report_error("Не удалось открыть библиотеку штампов", exc)
                return False
            if choice is None:
                return False
            data, name = choice
            tab.style["image_data"] = data
            tab.style["image_name"] = name
            tab.tool = TOOL_STAMP
            self._push_tool()
            self._call_view(tab, "set_image_data", data, "stamp")
            self._rebuild_tools()
            self._schedule_inspector_rebuild()
            self.show_status("Штамп выбран: щёлкните по странице, чтобы разместить")
            return True
        path, _filter = QFileDialog.getOpenFileName(
            self, "Выберите изображение", self._last_dir, IMAGE_FILTER
        )
        if not path:
            return False
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            self._report_error("Не удалось прочитать изображение", exc)
            return False
        if not raw:
            self._report_message("Пустой файл", "Выбранный файл изображения пуст.")
            return False
        data = base64.b64encode(raw).decode("ascii")
        kind = "stamp" if tool_id == TOOL_STAMP else "image"
        tab.style["image_data"] = data
        tab.style["image_name"] = os.path.basename(path)
        tab.tool = tool_id if tool_id in (TOOL_IMAGE, TOOL_STAMP, TOOL_SIGNATURE) else TOOL_IMAGE
        self._push_tool()
        if tab.view is not None:
            self._call_view(tab, "set_image_data", data, kind)
        self._rebuild_tools()
        self._schedule_inspector_rebuild()
        self.show_status("Изображение выбрано: щёлкните по странице, чтобы разместить")
        return True

    # ------------------------------------------------------------------
    # Поля формы
    # ------------------------------------------------------------------
    def _form_count(self) -> int:
        session = self._session
        if session is None or self.page_count == 0:
            return 0
        try:
            return len(session.form_widgets(None) or [])
        except Exception:  # noqa: BLE001
            return 0

    def form_rows(self, *, all_pages: bool = False) -> list[dict]:
        """Строки панели полей: технические имена ключей из PDF.

        Виджеты запрашиваются постранично: так у каждой строки точно известна
        страница, даже если одно поле занимает несколько виджетов.
        """
        session = self._session
        tab = self.current_tab
        if session is None or tab is None or self.page_count == 0:
            return []
        pages = list(range(self.page_count)) if all_pages else [tab.page_index]
        labels = self._field_labels()
        rows: list[dict] = []
        seen: dict[tuple[str, int], dict] = {}
        for page in pages:
            try:
                entries = session.form_widgets(page) or []
            except Exception as exc:  # noqa: BLE001
                self._note_error("Поля формы", exc)
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("key") or "").strip()
                if not key:
                    continue
                row = seen.get((key, page))
                if row is None:
                    row = {
                        "key": key,
                        "kind": str(entry.get("kind") or "text"),
                        "page": page,
                        "value": entry.get("value"),
                        "options": list(entry.get("options") or []),
                        "exports": [],
                        "readonly": bool(entry.get("readonly")),
                        "multiline": bool(entry.get("multiline")),
                        "label": labels.get(key, ""),
                        "count": 0,
                    }
                    seen[(key, page)] = row
                    rows.append(row)
                row["count"] += 1
                if row["kind"] == "radio":
                    export = str(entry.get("export_value") or "")
                    if export:
                        row["exports"].append(export)
                    if entry.get("value"):
                        row["value"] = entry.get("value")
        return rows

    def _field_labels(self) -> dict[str, str]:
        session = self._session
        if session is None:
            return {}
        try:
            fields = session.form_fields(None) or []
        except Exception:  # noqa: BLE001
            return {}
        labels: dict[str, str] = {}
        for item in fields:
            key = str(getattr(item, "key", "") or "")
            label = str(getattr(item, "label", "") or "")
            if key and label and label != key:
                labels[key] = label
        return labels

    def commit_form_value(self, key: str, value: Any) -> bool:
        """Записать значение поля в PDF-сессию и синхронизировать холст.

        Панель при этом НЕ пересобирается: поле ввода сохраняет фокус и
        положение курсора, пока пользователь печатает.
        """
        tab = self.current_tab
        session = self._session
        if tab is None or session is None or not key:
            return False
        try:
            session.set_form_value(key, value)
        except Exception as exc:  # noqa: BLE001 - поле может быть только для чтения
            self.show_status("Поле «%s» не изменено: %s" % (key, _error_text(exc)))
            return False
        if tab.view is not None:
            self._schedule_view_refresh(tab)
        self._sync_chrome()
        self.show_status("Поле «%s» обновлено" % key)
        return True

    def _schedule_view_refresh(self, tab: EditorTab, delay_ms: int = 250) -> None:
        """Отложить перерисовку холста после ввода в панели полей.

        Значение уходит в PDF-сессию сразу, а холст обновляется, когда ввод
        приостановился: поле в панели не теряет фокус и курсор при каждом знаке.
        """
        self._pending_refresh_tab = tab
        timer = getattr(self, "_panel_refresh_timer", None)
        if timer is not None:
            timer.start(max(0, int(delay_ms)))

    def flush_view_refresh(self) -> None:
        """Немедленно применить отложенное обновление холста (для проверок)."""
        timer = getattr(self, "_panel_refresh_timer", None)
        if timer is not None:
            timer.stop()
        self._flush_view_refresh()

    def _flush_view_refresh(self) -> None:
        tab = getattr(self, "_pending_refresh_tab", None)
        self._pending_refresh_tab = None
        if tab is None or tab.view is None:
            return
        self._call_view(tab, "reload", True)
        self._push_tool()
        self._push_readonly(tab)

    def reveal_form_widget(self, key: str, *, steal_focus: bool = False) -> bool:
        """Показать, где находится поле, не забирая фокус из панели свойств.

        Панель — резервный способ заполнения: печатать в ней можно, не теряя
        курсор, поэтому фокус забирает только явный щелчок по имени поля
        (:meth:`focus_form_widget`).
        """
        tab = self.current_tab
        if tab is None or not key:
            return False
        page = self._page_of_field(key)
        if page is None:
            self.show_status("Поле «%s» не найдено на страницах документа" % key)
            return False
        if steal_focus:
            return self.focus_form_widget(key)
        marker = "" if page == tab.page_index else " (перейдите на страницу %d)" % (
            page + 1)
        self.show_status("Поле «%s» — страница %d%s" % (key, page + 1, marker))
        return True

    def focus_form_widget(self, key: str) -> bool:
        """Перевести к полю с этим техническим именем и передать ему фокус."""
        tab = self.current_tab
        if tab is None or not key:
            return False
        page = self._page_of_field(key)
        if page is None:
            self.show_status("Поле «%s» не найдено на страницах документа" % key)
            return False
        if page != tab.page_index:
            self.goto_page(page)
        canvas = self._canvas
        if canvas is not None:
            focus = getattr(canvas, "focus_form_field", None)
            if callable(focus):
                try:
                    if focus(key):
                        self.show_status("Поле «%s» на странице %d" % (key, page + 1))
                        return True
                except Exception as exc:  # noqa: BLE001
                    self._note_error("focus_form_field", exc)
        view = tab.view
        if view is not None:
            focus_view = getattr(view, "focus_form_field", None)
            if callable(focus_view):
                try:
                    if focus_view(key, page):
                        self.show_status("Поле «%s» на странице %d" % (key, page + 1))
                        return True
                except Exception as exc:  # noqa: BLE001
                    self._note_error("focus_form_field", exc)
        self.show_status("Поле «%s» — страница %d" % (key, page + 1))
        return False

    def _page_of_field(self, key: str) -> Optional[int]:
        session = self._session
        if session is None:
            return None
        for page in range(self.page_count):
            try:
                entries = session.form_widgets(page) or []
            except Exception:  # noqa: BLE001
                continue
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("key")) == key:
                    return page
        return None

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def search_document(self, term: str) -> list[dict]:
        """Поиск по полям формы, пометкам и (если появится API) тексту страниц."""
        text = str(term or "").strip().lower()
        if not text:
            return []
        results: list[dict] = []
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return results
        for row in self.form_rows(all_pages=True):
            haystack = " ".join([
                row["key"], row.get("label") or "", str(row.get("value") or ""),
                " ".join(row.get("options") or []),
            ]).lower()
            if text in haystack:
                results.append({
                    "kind": "form",
                    "key": row["key"],
                    "page": row["page"],
                    "title": row["key"],
                    "meta": "Поле формы · страница %d" % (row["page"] + 1),
                })
        for page in range(self.page_count):
            try:
                annotations = tab.session.get_annotations(page)
            except Exception:  # noqa: BLE001
                annotations = []
            for index, annotation in enumerate(annotations):
                body = str(getattr(annotation, "text", "") or "")
                if body and text in body.lower():
                    results.append({
                        "kind": "annotation",
                        "page": page,
                        "index": index,
                        "title": body.strip().splitlines()[0][:80],
                        "meta": "Пометка «%s» · страница %d" % (
                            self.kind_label(annotation.kind), page + 1),
                    })
        results.extend(self._search_page_text(text))
        return results

    def _search_page_text(self, term: str) -> list[dict]:
        """Поиск по исходному тексту — только если бэкенд отдаёт такой API."""
        session = self._session
        if session is None:
            return []
        tab = self.current_tab
        if tab is not None and tab.view is not None and hasattr(tab.view, 'find_text'):
            hits = tab.view.find_text(term)
            return [{"kind": "text", "page": hit.page, "title": hit.text[:100],
                     "rects": hit.rects,
                     "meta": "Текст документа · страница %d" % (hit.page + 1)}
                    for hit in hits[:500]]
        extractor = None
        for name in ("page_text", "extract_text", "search_text"):
            candidate = getattr(session, name, None)
            if callable(candidate):
                extractor = candidate
                break
        if extractor is None:
            return []
        results: list[dict] = []
        for page in range(self.page_count):
            try:
                raw = extractor(page)
            except Exception:  # noqa: BLE001
                continue
            body = raw if isinstance(raw, str) else str(raw or "")
            position=body.lower().find(term)
            while position>=0 and len(results)<500:
                snippet = body[max(0, position - 30):position + 50].strip()
                results.append({
                    "kind": "text",
                    "page": page,
                    "title": snippet,
                    "meta": "Текст документа · страница %d" % (page + 1),
                })
                position=body.lower().find(term,position+len(term))
        return results

    def jump_to_result(self, result: dict) -> None:
        page = int(result.get("page") or 0)
        self.goto_page(page)
        if result.get('kind') == 'text' and result.get('rects'):
            tab = self.current_tab
            if tab is not None and tab.view is not None:
                self._call_view(tab, 'show_search_result', page, result['rects'])
        if result.get("kind") == "form" and result.get("key"):
            self.focus_form_widget(str(result["key"]))
        elif result.get("kind") == "annotation" and result.get("index") is not None:
            tab = self.current_tab
            if tab is not None:
                tab.selection = (page, int(result["index"]))
                if tab.view is not None:
                    self._call_view(tab, "focus_page", page)
                    self._select_canvas_annotation(tab)
                self.inspector.rebuild()

    def open_search(self) -> None:
        if self.page_count <= 0:
            self.toast("Сначала откройте PDF")
            return
        dialog = SearchDialog(self)
        dialog.exec()

    # ------------------------------------------------------------------
    # Печать
    # ------------------------------------------------------------------
    def _choose_printer(self) -> Any:
        """Диалог печати (в проверках подменяется, чтобы не открывать окно)."""
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return printer

    def print_document(self) -> bool:
        """Реальная печать страниц через QPrinter (без «показанных» действий)."""
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self.toast("Сначала откройте PDF")
            return False
        if not getattr(tab.session, 'can_print', True):
            self.show_status('Печать запрещена защитой документа')
            return False
        printer = self._choose_printer()
        if printer is None:
            self.show_status("Печать отменена")
            return False
        painter = QPainter()
        if not painter.begin(printer):
            self._report_message("Печать не выполнена",
                                 "Не удалось начать печать: принтер недоступен.")
            return False
        try:
            resolution = float(printer.resolution() or 300)
            scale = min(MAX_PRINT_SCALE, max(0.5, resolution / 72.0))
            rect = QRectF(printer.pageLayout().paintRectPixels(int(resolution)))
            for index in range(self.page_count):
                if index:
                    printer.newPage()
                image = tab.session.render(index, scale, include_annotations=True)
                pixmap = _pil_to_pixmap(image)
                if pixmap is None:
                    raise RuntimeError("Страница %d не отрисована" % (index + 1))
                target = QRectF(rect)
                source = QRectF(pixmap.rect())
                ratio = min(target.width() / max(1.0, source.width()),
                            target.height() / max(1.0, source.height()))
                width = source.width() * ratio
                height = source.height() * ratio
                target = QRectF(target.left() + (target.width() - width) / 2.0,
                                target.top() + (target.height() - height) / 2.0,
                                width, height)
                painter.drawPixmap(target, pixmap, source)
        except Exception as exc:  # noqa: BLE001
            self._report_error("Печать не выполнена", exc,
                               "Документ остался открытым, изменения не потеряны.")
            return False
        finally:
            painter.end()
        self.show_status("Отправлено на печать: %s" % pages_label(self.page_count))
        return True

    # ------------------------------------------------------------------
    # Сохранение (всегда копия; исходник не перезаписывается)
    # ------------------------------------------------------------------
    def _is_source_path(self, session: Any, path: str) -> bool:
        try:
            sources = list(getattr(session, "source_paths", []) or [])
        except Exception:  # noqa: BLE001
            sources = []
        return any(_same_path(path, source) for source in sources)

    def suggested_save_path(self, tab: EditorTab) -> str:
        base_dir = self._last_saved_dir or self._last_dir
        if tab.path:
            folder = os.path.dirname(tab.path) or base_dir
            stem = Path(tab.path).stem
            suffix = " (восстановлено)" if tab.recovery.restored else " (копия)"
            return os.path.join(folder, "%s%s.pdf" % (stem, suffix))
        return os.path.join(base_dir, "Документ.pdf")

    def _choose_save_path(self, suggested: str) -> Optional[str]:
        """Диалог выбора файла копии (в проверках подменяется)."""
        path, _filter = QFileDialog.getSaveFileName(
            self, "Сохранить копию PDF", suggested, PDF_FILTER
        )
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        return path

    def save_current(self) -> bool:
        tab = self.current_tab
        return self.save_tab(tab) if tab is not None else False

    def save_tab(self, tab: EditorTab) -> bool:
        """Сохранить вкладку: повторно — в свою копию, иначе спросить новый путь."""
        if tab is None or not tab.has_document:
            self._report_message("Нет документа", "Сначала откройте PDF-файл.")
            return False
        session = tab.session
        saved = getattr(session, "saved_path", None)
        if saved and not self._is_source_path(session, saved):
            return self._save_to(tab, str(saved))
        path = self._choose_save_path(self.suggested_save_path(tab))
        if not path:
            self.show_status("Сохранение отменено")
            return False
        return self._save_to(tab, path)

    def save_copy_as(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self._report_message("Нет документа", "Сначала откройте PDF-файл.")
            return False
        path = self._choose_save_path(self.suggested_save_path(tab))
        if not path:
            self.show_status("Сохранение отменено")
            return False
        return self._save_to(tab, path)

    def _save_to(self, tab: EditorTab, path: str) -> bool:
        session = tab.session
        target = os.path.abspath(os.path.expanduser(str(path)))
        if self._is_source_path(session, target):
            self._report_message(
                "Исходный файл защищён",
                "PdfDesk не перезаписывает исходный PDF.\n\nВыберите другое имя "
                "или другую папку — будет сохранена копия.",
            )
            return False
        if self._inside_recovery_root(target):
            self._report_message(
                "Служебная папка",
                "Нельзя сохранять копию внутрь папки временного восстановления.",
            )
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            session.save(target)
        except Exception as exc:  # noqa: BLE001 - документ обязан остаться открытым
            self._snapshot_tab(tab, force=True)
            self._report_error(
                "Не удалось сохранить копию", exc,
                "Документ остался открытым, изменения не потеряны — временная копия "
                "обновлена. Попробуйте другое имя или папку.",
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self._last_saved_dir = os.path.dirname(target) or self._last_dir
        self._remove_recovery(tab)
        self._sync_shell()
        self.toast("Копия сохранена: %s (исходный файл не изменён)" % os.path.basename(target))
        self.show_status("Сохранена копия: %s" % target)
        self._show_warnings(tab)
        return True

    # ------------------------------------------------------------------
    # Настройки, горячие клавиши
    # ------------------------------------------------------------------
    def recognize_text_dialog(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self.show_status('Сначала откройте PDF')
            return False
        if not getattr(tab.session, 'has_edit_permission', True):
            self.show_status('Для распознавания требуется разрешение на изменение PDF')
            return False
        from .ocr_dialog import OCRDialog
        from .ocr_apply import apply_ocr_results
        revision = self._session_revision(tab)
        dialog = OCRDialog(tab.session, current_page=tab.page_index, parent=self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result is None:
                return False
            if tab not in self._tabs or self._session_revision(tab) != revision:
                self.show_status('Документ изменился во время распознавания. Повторите OCR')
                return False
            results = {page.page_index: page for page in dialog.result.pages
                       if page.result.words}
            if not results:
                self.show_status('На выбранных страницах текст не найден')
                return False
            apply_ocr_results(tab.session, results)
            self._after_change(tab, pages_changed=True)
            self.show_status('Распознано страниц: %d. Текст доступен для поиска и копирования' % len(results))
            return True
        except Exception as exc:
            self._report_error('Не удалось применить распознанный текст', exc)
            return False
        finally:
            dialog.deleteLater()

    def correct_ocr_text_dialog(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self.show_status('Сначала откройте PDF')
            return False
        if not getattr(tab.session, 'has_edit_permission', True):
            self.show_status('Для исправления требуется разрешение на изменение PDF')
            return False
        from .ocr_correction_dialog import OCRCorrectionDialog
        from .ocr_correction import OCRCorrectionError
        dialog = None
        try:
            dialog = OCRCorrectionDialog(tab.session, tab.page_index, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self._after_change(tab, pages_changed=True)
            self.show_status('Поисковый текст исправлен. Изображение скана осталось прежним')
            return True
        except OCRCorrectionError as exc:
            self.show_status(str(exc))
            return False
        except Exception as exc:
            self._report_error('Не удалось исправить распознанный текст', exc)
            return False
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def scan_document_dialog(self) -> bool:
        from .scanner_dialog import ScannerDialog
        dialog = ScannerDialog(self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.pdf_bytes:
                return False
            self._on_import_conversion_succeeded(dialog.pdf_bytes, 'Сканирование.pdf', '')
            return True
        finally:
            dialog.deleteLater()

    def configure_pdf_security(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self.show_status('Сначала откройте PDF')
            return False
        protection = getattr(tab.session, 'security_info', {})
        if protection.get('encrypted') and protection.get('authenticated_as') != 'owner':
            if not self.unlock_pdf_editing():
                return False
        from .security_dialog import PdfSecurityDialog
        dialog = PdfSecurityDialog(self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            tab.session.set_security(**dialog.values())
        except (ValueError, RuntimeError, PermissionError) as exc:
            self._report_error('Не удалось настроить защиту', exc)
            return False
        finally:
            dialog.deleteLater()
        self._after_change(tab)
        self.show_status('Защита настроена. Сохраните PDF, чтобы применить пароли к файлу')
        return True

    def unlock_pdf_editing(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            self.show_status('Сначала откройте PDF')
            return False
        password, accepted = QInputDialog.getText(
            self, 'Разрешить изменение', 'Пароль владельца PDF:', QLineEdit.EchoMode.Password)
        if not accepted:
            return False
        try:
            tab.session.unlock_security(password)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self._report_error('Изменение не разрешено', exc)
            return False
        self._sync_shell()
        self.show_status('Изменение документа разрешено')
        return True

    def remove_pdf_security(self) -> bool:
        tab = self.current_tab
        if tab is None or not tab.has_document:
            return False
        protection = getattr(tab.session, 'security_info', {})
        if protection.get('encrypted') and protection.get('authenticated_as') != 'owner':
            if not self.unlock_pdf_editing():
                return False
        try:
            tab.session.set_security()
        except (ValueError, RuntimeError, PermissionError) as exc:
            self._report_error('Не удалось снять защиту', exc)
            return False
        self._after_change(tab)
        self.show_status('Защита снята. Сохраните PDF, чтобы применить изменение к файлу')
        return True

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        dialog.exec()

    def _load_settings(self) -> None:
        store = self._settings
        self._pinned = str(store.value("appearance/pinned", "false")).lower() in ("true", "1")
        self._dark = str(store.value("appearance/dark", "false")).lower() in ("true", "1")
        self._rulers = str(store.value("appearance/rulers", "false")).lower() in ("true", "1")
        self._sidebar_visible = str(
            store.value("appearance/sidebar", "true")).lower() in ("true", "1")
        self._autosave_enabled = str(
            store.value("recovery/enabled", "true")).lower() in ("true", "1")
        try:
            self._autosave_interval_min = int(
                store.value("recovery/interval", theme.AUTOSAVE_DEFAULT_MIN))
        except (TypeError, ValueError):
            self._autosave_interval_min = theme.AUTOSAVE_DEFAULT_MIN
        if self._autosave_interval_min not in theme.AUTOSAVE_INTERVALS_MIN:
            self._autosave_interval_min = theme.AUTOSAVE_DEFAULT_MIN
        self.pin_button.setChecked(self._pinned)
        self.full_tools.setVisible(self._pinned)
        self.tool_heading.setVisible(self._pinned)
        if self._pinned:
            self.tools_bar.setFixedHeight(
                theme.TOOLS_HEIGHT + self.full_tools.sizeHint().height() + 10)
        self.ruler_button.setChecked(self._rulers)
        self.top_ruler.setVisible(self._rulers)
        self.left_ruler.setVisible(self._rulers)
        self.thumbnails.setVisible(self._sidebar_visible)
        self.sidebar_button.setChecked(self._sidebar_visible)
        self._shortcut_text = {}
        for name, _label, default in SHORTCUT_DEFAULTS:
            value = store.value("shortcuts/%s" % name, default)
            self._shortcut_text[name] = str(value or default)

    def shortcut_text(self, name: str) -> str:
        return self._shortcut_text.get(name, "")

    def shortcut_default(self, name: str) -> str:
        for key, _label, default in SHORTCUT_DEFAULTS:
            if key == name:
                return default
        return ""

    def _build_shortcuts(self) -> None:
        for name, _label, _default in SHORTCUT_DEFAULTS:
            shortcut = QShortcut(QKeySequence(self.shortcut_text(name)), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            handler = self._shortcut_handler(name)
            if handler is not None:
                shortcut.activated.connect(handler)
            self._shortcuts[name] = shortcut

    def _shortcut_handler(self, name: str) -> Optional[Callable[[], Any]]:
        return {
            'new': self.create_document_dialog,
            "open": self.open_dialog,
            "save": self.save_current,
            "save_as": self.save_copy_as,
            "close_tab": self.close_current_tab,
            "print": self.print_document,
            "undo": self.undo,
            "redo": self.redo,
            "search": self.open_search,
            "zoom_in": self.zoom_in,
            "zoom_out": self.zoom_out,
            "fit": self.fit_page,
            "next_page": lambda: self.step_page(1),
            "prev_page": lambda: self.step_page(-1),
        }.get(name)

    def set_shortcut(self, name: str, text: str) -> bool:
        """Назначить сочетание; конфликт обнаруживается и отклоняется."""
        text = str(text or "").strip()
        if not text:
            return False
        sequence = QKeySequence(text, QKeySequence.SequenceFormat.PortableText)
        canonical = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if (not canonical or sequence.isEmpty()
                or any(sequence[index].key() == Qt.Key.Key_unknown
                       for index in range(sequence.count()))):
            self.show_status("Некорректное сочетание клавиш")
            return False
        for other, value in self._shortcut_text.items():
            other_sequence = QKeySequence(value, QKeySequence.SequenceFormat.PortableText)
            if (other != name and value
                    and (sequence.matches(other_sequence) != QKeySequence.SequenceMatch.NoMatch
                         or other_sequence.matches(sequence) != QKeySequence.SequenceMatch.NoMatch)):
                self.toast("Это сочетание уже назначено: %s" % other)
                self.show_status("Сочетание %s уже используется" % text)
                return False
        shortcut = self._shortcuts.get(name)
        if shortcut is None:
            return False
        shortcut.setKey(sequence)
        self._shortcut_text[name] = canonical
        self._settings.setValue("shortcuts/%s" % name, canonical)
        self.toast("Сочетание обновлено: %s" % canonical)
        return True

    # ------------------------------------------------------------------
    # Временное восстановление (по вкладкам, исходник только читается)
    # ------------------------------------------------------------------
    def recovery_root(self) -> Path:
        if self._recovery_root is None:
            try:
                self._recovery_root = Path(recovery_module.default_recovery_root())
            except Exception:  # noqa: BLE001
                self._recovery_root = Path(os.path.expanduser("~")) / ".pdfdesk-recovery"
        return self._recovery_root

    def _inside_recovery_root(self, path: str) -> bool:
        try:
            return recovery_module.is_inside_root(self.recovery_root(), path)
        except Exception:  # noqa: BLE001
            return False

    def _session_revision(self, tab: EditorTab) -> Optional[int]:
        try:
            return int(getattr(tab.session, "_revision", -1))
        except (TypeError, ValueError):
            return None

    def _recovery_supported(self, tab: EditorTab) -> bool:
        session = tab.session
        return all(callable(getattr(session, name, None)) for name in (
            "_attach_annotations", "_attach_text_layer", "_write_bytes",
            "_detach_text_layer", "_detach_annotations",
        ))

    def _tab_recovery_key(self, tab: EditorTab) -> Optional[str]:
        state = tab.recovery
        if state.key:
            return state.key
        paths = list(state.source_paths)
        if not paths:
            try:
                paths = [str(item) for item in getattr(tab.session, "source_paths", []) or []]
            except Exception:  # noqa: BLE001
                paths = []
        if not paths:
            return None
        try:
            return recovery_module.document_key(paths)
        except Exception:  # noqa: BLE001
            return None

    def _snapshot_tab(self, tab: EditorTab, *, force: bool = False) -> Optional[str]:
        """Снимок вкладки. Документ остаётся «грязным», исходник не меняется."""
        if not self._autosave_enabled and not force:
            return None
        if not tab.has_document or not self._recovery_supported(tab):
            return None
        if not force and not tab.dirty:
            return None
        key = self._tab_recovery_key(tab)
        if not key:
            return None
        revision = self._session_revision(tab)
        if not force and revision is not None and revision == tab.recovery.last_revision:
            return None
        root = self.recovery_root()
        try:
            for source in getattr(tab.session, "source_paths", []) or []:
                if _same_path(str(root), source):
                    return None
            record = recovery_module.snapshot(
                tab.session,
                root=root,
                key=key,
                source_paths=tab.recovery.source_paths or list(
                    getattr(tab.session, "source_paths", []) or []),
                saved_path=getattr(tab.session, "saved_path", None),
                copy_path=tab.recovery.copy_path,
                revision=revision,
                extra={"tab_title": tab.title},
            )
        except Exception as exc:  # noqa: BLE001 - таймер не должен ронять окно
            self._note_error("Автосохранение", exc)
            self.show_status("Временную копию сохранить не удалось: %s" % _error_text(exc))
            return None
        tab.recovery.key = record.key
        tab.recovery.last_revision = revision
        tab.recovery.last_path = str(record.document_path)
        return str(record.document_path)

    def autosave_now(self, force: bool = False) -> int:
        """Сделать временные снимки всех изменённых вкладок (без пометки «сохранено»)."""
        saved = 0
        for tab in self._tabs:
            if self._snapshot_tab(tab, force=force):
                saved += 1
        return saved

    def set_autosave_enabled(self, enabled: bool) -> None:
        self._autosave_enabled = bool(enabled)
        self._settings.setValue("recovery/enabled", self._autosave_enabled)
        self._apply_autosave_timer()
        self.show_status("Автосохранение включено" if self._autosave_enabled
                         else "Автосохранение выключено")

    def set_autosave_interval(self, minutes: int) -> None:
        try:
            value = int(minutes)
        except (TypeError, ValueError):
            return
        if value not in theme.AUTOSAVE_INTERVALS_MIN:
            value = theme.AUTOSAVE_DEFAULT_MIN
        self._autosave_interval_min = value
        self._settings.setValue("recovery/interval", value)
        self._apply_autosave_timer()
        self.show_status("Интервал автосохранения: %d мин" % value)

    def _apply_autosave_timer(self) -> None:
        timer = getattr(self, "_autosave_timer", None)
        if timer is None:
            return
        if self._autosave_enabled:
            timer.setInterval(self._autosave_interval_min * 60 * 1000)
            timer.start()
        else:
            timer.stop()

    def recovery_candidates(self) -> list[Any]:
        try:
            return list(recovery_module.load_records(self.recovery_root()))
        except Exception as exc:  # noqa: BLE001
            self._note_error("Восстановление", exc)
            return []

    def _confirm_recovery(self, record: Any) -> bool:
        """Спросить пользователя о восстановлении (в проверках подменяется)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Восстановление после сбоя")
        box.setText("Найдена временная копия документа:\n%s" % record.source_label)
        box.setInformativeText(
            "Создана: %s\nИсходный файл не изменялся. Восстановить копию в новой вкладке?"
            % record.metadata.get("created_at", "—")
        )
        restore = box.addButton("Восстановить", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Не сейчас", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restore)
        box.exec()
        return box.clickedButton() is restore

    def offer_recovery(self) -> bool:
        """Предложить восстановление самой свежей временной копии (если есть)."""
        for record in self.recovery_candidates():
            if record.page_count <= 0 and not record.source_paths:
                continue
            if self._confirm_recovery(record):
                return self.restore_recovery(record)
        return False

    def restore_recovery(self, record: Any) -> bool:
        """Открыть временную копию в новой вкладке; исходник остаётся нетронутым."""
        tab = self.new_tab(silent=True)
        password = None
        attempts = 0
        while True:
            try:
                tab.session.open(str(record.document_path), password)
                break
            except Exception as exc:  # noqa: BLE001
                if self._looks_like_password_error(exc) and attempts < 3:
                    password = self._ask_password(str(record.document_path))
                    attempts += 1
                    if password is None:
                        self._remove_tab(tab)
                        self.show_status('Восстановление отменено; временная копия сохранена')
                        return False
                    continue
                self._report_error("Не удалось восстановить копию", exc)
                self._remove_tab(tab)
                return False
        tab.path = record.source_paths[0] if record.source_paths else None
        tab.title = "%s (восстановлено)" % (record.source_label or "Документ")
        tab.recovery = RecoveryState(
            key=record.key,
            source_paths=list(record.source_paths),
            restored=True,
            copy_path=str(record.document_path),
            last_revision=self._session_revision(tab),
            last_path=str(record.document_path),
        )
        tab.page_index = 0
        tab.zoom = 1.0
        tab.thumbs = {}
        tab.thumbs_revision = None
        self._ensure_view(tab)
        self._refresh_tab(tab, pages_changed=True)
        self.show_status("Восстановлена временная копия: %s" % record.source_label)
        self.toast("Временная копия открыта. Сохраните её как отдельный файл.")
        return True

    def _remove_recovery(self, tab: EditorTab) -> bool:
        key = tab.recovery.key
        if not key:
            return False
        try:
            removed = recovery_module.remove_record(self.recovery_root(), key)
        except Exception as exc:  # noqa: BLE001
            self._note_error("Восстановление", exc)
            return False
        if removed:
            tab.recovery.last_revision = None
            tab.recovery.last_path = None
        return removed

    def remove_recovery_for_current(self) -> bool:
        tab = self.current_tab
        if tab is None:
            return False
        if not tab.recovery.key:
            tab.recovery.key = self._tab_recovery_key(tab)
        return self._remove_recovery(tab)

    def _release_recovery(self, tab: EditorTab, *, discard: bool) -> None:
        """Закрытие вкладки: решаем судьбу временной копии без потери работы."""
        if tab.recovery.restored and tab.dirty:
            return
        if tab.dirty and not discard:
            return
        self._remove_recovery(tab)

    # ------------------------------------------------------------------
    # Открытие через диалог, перетаскивание
    # ------------------------------------------------------------------
    def open_dialog(self) -> bool:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Открыть PDF", self._last_dir, PDF_FILTER
        )
        if not paths:
            return False
        self._last_dir = os.path.dirname(paths[0]) or self._last_dir
        self._last_saved_dir = self._last_dir
        opened = 0
        for position, path in enumerate(paths):
            if self.open_path(path, new_tab=position > 0 or opened > 0):
                opened += 1
        return opened > 0

    def _dropped_paths(self, event: Any) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if isinstance(url, QUrl) and url.isLocalFile():
                local = url.toLocalFile()
                if local.lower().endswith(".pdf"):
                    paths.append(local)
        return paths

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        paths = self._dropped_paths(event)
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        opened = 0
        for position, path in enumerate(paths):
            if self.open_path(path, new_tab=position > 0 or opened > 0):
                opened += 1
        if opened:
            self.show_status("Открыто файлов: %d" % opened)

    # ------------------------------------------------------------------
    # Сообщения и ошибки
    # ------------------------------------------------------------------
    def show_status(self, text: str, timeout: int = 6000) -> None:
        self.status_label.setText(str(text))
        if timeout > 0:
            self._status_timeout.start(timeout)

    def _reset_status(self) -> None:
        tab = self.current_tab
        if tab is not None and tab.has_document:
            self.status_label.setText("Документ открыт: %s" % tab.title)
        else:
            self.status_label.setText("Откройте PDF, чтобы начать работу")

    def toast(self, text: str) -> None:
        self._toast.show_message(text)

    def _note_error(self, context: str, exc: BaseException) -> None:
        """Мягкая ошибка: попадает в строку состояния, окно не падает."""
        self.show_status("%s: %s" % (context, _error_text(exc)), timeout=9000)
        print("[PdfDesk] %s: %s" % (context, _error_text(exc)))

    def _report_error(self, title: str, exc: BaseException, extra: str = "") -> None:
        self._note_error(title, exc)
        message = _error_text(exc)
        if extra:
            message += "\n\n" + extra
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _report_message(self, title: str, message: str) -> None:
        self.show_status("%s: %s" % (title, message.splitlines()[0]))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _show_warnings(self, tab: EditorTab) -> None:
        try:
            warnings = list(getattr(tab.session, "warnings", []) or [])
        except Exception:  # noqa: BLE001
            return
        if warnings:
            self.show_status("Документ открыт с предупреждениями: %s" % warnings[0], timeout=9000)

    def _ask_password(self, path: str) -> Optional[str]:
        text, accepted = QInputDialog.getText(
            self, "Требуется пароль",
            "Файл защищён паролем:\n%s\n\nВведите пароль:" % os.path.basename(path),
            QLineEdit.EchoMode.Password,
        )
        return text if accepted else None

    @staticmethod
    def _looks_like_password_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return "password" in text or "парол" in text or "encrypted" in text

    def font_families(self) -> list[str]:
        try:
            from .pdf_backend import available_font_families  # type: ignore
        except ImportError:  # pragma: no cover
            available_font_families = None  # type: ignore
        families: list[str] = []
        if callable(available_font_families):
            try:
                families = [str(name) for name in available_font_families()]
            except Exception:  # noqa: BLE001
                families = []
        if not families:
            families = list(theme.FONT_FAMILIES)
        for name in theme.FONT_FAMILIES:
            if name not in families:
                families.append(name)
        return families

    # -- проверки-заглушки диалогов (переопределяются в тестах) -----------
    def _confirm_dirty(self, tab: EditorTab, action_text: str) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Несохранённые изменения")
        box.setText("Документ «%s» содержит несохранённые изменения." % tab.title)
        box.setInformativeText("%s без сохранения?\nКнопка «Сохранить» создаёт копию "
                               "PDF — исходный файл не изменяется." % action_text)
        save_button = box.addButton("Сохранить копию", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        text = clicked.text() if clicked is not None else ""
        return "discard" if "Не сохранять" in text else "cancel"

    def _confirm_delete_page(self, index: int) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Удалить страницу")
        box.setText("Удалить страницу %d?" % (index + 1))
        box.setInformativeText("Действие можно отменить сочетанием Ctrl+Z до сохранения.")
        remove = box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(remove)
        box.exec()
        return box.clickedButton() is remove

    # ------------------------------------------------------------------
    # Снимок окна (сравнение с макетом)
    # ------------------------------------------------------------------
    def save_screenshot(self, path: str, *, size: tuple[int, int] = (1440, 1000),
                        settle_ms: int = 350) -> bool:
        """Сохранить снимок окна ровно заданного размера (например 1440x1000)."""
        target = os.path.abspath(os.path.expanduser(str(path)))
        self.resize(QSize(int(size[0]), int(size[1])))
        self.show()
        deadline = time.monotonic() + max(0.0, settle_ms / 1000.0)
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        pixmap = self.grab()
        if pixmap.isNull():
            return False
        if pixmap.width() != size[0] or pixmap.height() != size[1]:
            pixmap = pixmap.scaled(QSize(int(size[0]), int(size[1])),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        return bool(pixmap.save(target, "PNG"))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        super().resizeEvent(event)
        self._schedule_compare_layout()
        toast = getattr(self, "_toast", None)
        if toast is not None:
            toast.reposition()

    # ------------------------------------------------------------------
    # Завершение работы
    # ------------------------------------------------------------------
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        # Mark closing before a dirty-confirmation dialog starts its nested Qt
        # event loop. An import can finish while that dialog is open; its queued
        # success signal must not create a new tab during the close attempt.
        self._closing = True
        worker = self._import_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
        for tab in list(self._tabs):
            if not tab.has_document or not tab.dirty:
                continue
            decision = self._confirm_dirty(tab, "Закрыть окно")
            if decision == "cancel":
                self._closing = False
                event.ignore()
                return
            if decision == "save" and not self.save_tab(tab):
                self._closing = False
                event.ignore()
                return
        for tab in list(self._tabs):
            self._release_recovery(tab, discard=True)
        timer = getattr(self, "_autosave_timer", None)
        if timer is not None:
            timer.stop()
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(2000)
        if self._import_cursor_active:
            QApplication.restoreOverrideCursor()
            self._import_cursor_active = False
        event.accept()


#: Совместимость с точкой входа: main.py импортирует MainWindow.
MainWindow = EditorWindow
