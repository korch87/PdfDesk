# -*- coding: utf-8 -*-
"""PdfDesk — интерфейс библиотеки штампов: компактный выбор миниатюрами.

Модуль намеренно тонкий: вся работа с файлами и манифестом — в
:mod:`src.stamp_library` (без Qt). Здесь только русский интерфейс нового
оформления PdfDesk (бело-серая основа, бирюзовый акцент) и хуки для
родительского окна.

Что предоставляется родителю (``editor_window``)::

    from .stamp_library import StampLibrary
    from .stamp_library_ui import choose_stamp, StampPickerButton

    # 1) просто получить данные и разместить их самому:
    choice = choose_stamp(self, window.stamp_library)      # (base64 PNG, имя)
    if choice:
        data, name = choice
        tab.style["image_data"] = data
        tab.style["image_name"] = name
        self._call_view(tab, "set_image_data", data, "stamp")

    # 2) кнопка-инструмент, которая сама открывает библиотеку:
    button = StampPickerButton(self, library)
    button.stampChosen.connect(lambda data, name: place_stamp(data, name))

Повторное размещение берёт те же данные: :meth:`StampLibrary.selection`
возвращает base64 PNG, который уже принимает ``set_image_data``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:  # импорт как пакета: from src.stamp_library_ui import choose_stamp
    from . import editor_theme as _theme
    from .stamp_library import (
        StampEntry,
        StampLibrary,
        StampLibraryError,
        sanitize_stamp_name,
    )
except ImportError:  # pragma: no cover - импорт при sys.path, указывающем на src/
    import editor_theme as _theme  # type: ignore[no-redef]
    from stamp_library import (  # type: ignore[no-redef]
        StampEntry,
        StampLibrary,
        StampLibraryError,
        sanitize_stamp_name,
    )

__all__ = [
    "TEXT_TITLE",
    "TEXT_HEADING",
    "TEXT_EMPTY",
    "TEXT_HINT",
    "TEXT_ADD",
    "TEXT_RENAME",
    "TEXT_DELETE",
    "TEXT_INSERT",
    "TEXT_CANCEL",
    "TEXT_BUTTON",
    "StampTile",
    "StampPicker",
    "StampLibraryDialog",
    "StampPickerButton",
    "choose_stamp",
]

# ---------------------------------------------------------------------------
# Русские подписи (стабильные строки интерфейса)
# ---------------------------------------------------------------------------
TEXT_TITLE = "Библиотека штампов"
TEXT_HEADING = "Мои штампы"
TEXT_SUB = "Изображение сохраняется в библиотеке PdfDesk и доступно после перезапуска."
TEXT_EMPTY = "Библиотека пуста — добавьте изображение PNG или JPEG"
TEXT_HINT = "Щёлкните миниатюру и нажмите «Вставить»; двойной щёлчок вставляет сразу"
TEXT_COUNT = "Штампов: %d"
TEXT_ADD = "Добавить изображение…"
TEXT_RENAME = "Переименовать"
TEXT_DELETE = "Удалить"
TEXT_INSERT = "Вставить"
TEXT_CANCEL = "Отмена"
TEXT_BUTTON = "Штамп"
TEXT_BUTTON_TOOLTIP = "Библиотека штампов: выбрать или добавить изображение"
TEXT_ADD_DIALOG = "Выберите изображения штампов"
TEXT_IMAGE_FILTER = "Изображения PNG/JPEG (*.png *.jpg *.jpeg)"
TEXT_SELECT_FIRST = "Сначала выберите штамп в списке."
TEXT_RENAME_PROMPT = "Новое имя штампа"
TEXT_DELETE_TITLE = "Удаление штампа"
TEXT_DELETE_CONFIRM = ("Удалить штамп «%s» из библиотеки?\n"
                       "Изображения, уже размещённые в документах, не изменяются.")
TEXT_ERROR_TITLE = "Библиотека штампов"
TEXT_ADDED = "Добавлено штампов: %d"
TEXT_ADD_ERRORS = "Не удалось добавить изображения:\n%s"

# ---------------------------------------------------------------------------
# Оформление: значения темы PdfDesk с безопасными запасными значениями
# ---------------------------------------------------------------------------
ACCENT = getattr(_theme, "ACCENT", "#167d99")
ACCENT_SOFT = getattr(_theme, "ACCENT_SOFT", "#e7f3f6")
INK = getattr(_theme, "INK", "#26313d")
MUTED = getattr(_theme, "MUTED", "#7a8591")
LINE = getattr(_theme, "LINE", "#e1e5e9")
SURFACE = getattr(_theme, "SURFACE", "#ffffff")
CHROME = getattr(_theme, "CHROME", "#f8f9fb")
HOVER = getattr(_theme, "HOVER", "#edf0f3")
DANGER = getattr(_theme, "DANGER", "#b54d49")

#: Компактные миниатюры (меньше миниатюр страниц 102x139 — это палитра).
TILE_WIDTH = 78
TILE_HEIGHT = 92
THUMB_SIZE = 56
PICKER_COLUMNS = 4
PICKER_MIN_HEIGHT = 268

_QSS = """
#StampHeading { color: %(ink)s; font-size: 13px; font-weight: 600; }
#StampCount { color: %(muted)s; font-size: 10px; }
#StampScroll { background: %(chrome)s; border: 1px solid %(line)s; border-radius: 8px; }
#StampDropArea { background: transparent; }
#StampEmpty { color: %(muted)s; font-size: 11px; }
#StampTile {
    background: %(surface)s;
    border: 1px solid %(line)s;
    border-radius: 6px;
    padding: 3px;
    color: %(ink)s;
    font-size: 10px;
}
#StampTile:hover { background: %(hover)s; }
#StampTile:checked { border: 2px solid %(accent)s; background: %(accent_soft)s; }
#StampRenameEdit {
    border: 1px solid %(accent)s;
    border-radius: 4px;
    padding: 3px 6px;
    background: %(surface)s;
    color: %(ink)s;
}
""" % {
    "ink": INK, "muted": MUTED, "line": LINE, "surface": SURFACE,
    "chrome": CHROME, "hover": HOVER, "accent": ACCENT, "accent_soft": ACCENT_SOFT,
}


def _report(parent: Optional[QWidget], message: str, handler: Optional[Callable[[str], None]] = None,
            title: str = TEXT_ERROR_TITLE) -> None:
    """Показать ошибку: обработчик родителя (строка состояния) или QMessageBox."""
    if handler is not None:
        try:
            handler(message)
        except Exception:  # noqa: BLE001 - интерфейс не должен падать из-за хука
            pass
        return
    QMessageBox.warning(parent, title, message)


class StampTile(QToolButton):
    """Компактная миниатюра штампа: квадратная картинка и имя под ней."""

    activated = Signal(str)  # id штампа при двойном щелчке

    def __init__(self, entry: StampEntry, image_bytes: bytes,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("StampTile")
        self.setProperty("stamp_id", entry.id)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAutoRaise(False)
        pixmap = QPixmap()
        if image_bytes:
            pixmap.loadFromData(bytes(image_bytes))
        if pixmap.isNull():
            pixmap = QPixmap(THUMB_SIZE, THUMB_SIZE)
            pixmap.fill(Qt.GlobalColor.transparent)
        self.setIcon(QIcon(pixmap.scaled(
            THUMB_SIZE, THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)))
        self.setText(self.fontMetrics().elidedText(
            entry.name, Qt.TextElideMode.ElideMiddle, TILE_WIDTH - 10))
        self.setToolTip("%s\n%d×%d · %s%s" % (
            entry.name, entry.width, entry.height, entry.format,
            "\nАльфа-канал сохранён" if entry.has_alpha else ""))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        self.activated.emit(self.entry.id)
        super().mouseDoubleClickEvent(event)


class StampPicker(QWidget):
    """Переиспользуемый выборщик штампов (без модальности — можно встроить).

    Сигналы:

    * ``stampChosen(str, str)`` — ``(base64 PNG, имя)`` при выборе;
    * ``selectionChanged(object)`` — текущая :class:`StampEntry` или ``None``;
    * ``libraryChanged()`` — после добавления/переименования/удаления.
    """

    stampChosen = Signal(str, str)
    selectionChanged = Signal(object)
    libraryChanged = Signal()

    def __init__(self, library: Optional[StampLibrary] = None,
                 parent: Optional[QWidget] = None, *,
                 on_choose: Optional[Callable[[str, str], None]] = None,
                 columns: int = PICKER_COLUMNS,
                 error_handler: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StampPicker")
        self.library = library if library is not None else StampLibrary()
        self.on_choose = on_choose
        self.error_handler = error_handler
        self.columns = max(1, int(columns))
        self._tiles: dict[str, StampTile] = {}
        self._selected_id: Optional[str] = None
        self._last_dir = ""
        self._stretch_row = -1
        self.setStyleSheet(_QSS)
        self._build()

    # ------------------------------------------------------------ построение
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.heading = QLabel(TEXT_HEADING)
        self.heading.setObjectName("StampHeading")
        self.count_label = QLabel(TEXT_COUNT % 0)
        self.count_label.setObjectName("StampCount")
        header.addWidget(self.heading)
        header.addStretch(1)
        header.addWidget(self.count_label)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("StampScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(PICKER_MIN_HEIGHT)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.drop_area = QWidget()
        self.drop_area.setObjectName("StampDropArea")
        self.grid = QGridLayout(self.drop_area)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.drop_area)

        self.empty_label = QLabel(TEXT_EMPTY)
        self.empty_label.setObjectName("StampEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.empty_label)
        outer.addWidget(self.stack, 1)

        self.hint = QLabel(TEXT_HINT)
        self.hint.setProperty("role", "tiny")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self.add_button = QPushButton(TEXT_ADD)
        self.add_button.setObjectName("StampAddImage")
        self.add_button.setProperty("role", "ghost")
        self.add_button.clicked.connect(lambda _checked=False: self.add_images())
        self.rename_button = QPushButton(TEXT_RENAME)
        self.rename_button.setObjectName("StampRename")
        self.rename_button.setProperty("role", "ghost")
        self.rename_button.clicked.connect(lambda _checked=False: self.begin_rename())
        self.delete_button = QPushButton(TEXT_DELETE)
        self.delete_button.setObjectName("StampDelete")
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(lambda _checked=False: self.delete_current())
        tools.addWidget(self.add_button)
        tools.addStretch(1)
        tools.addWidget(self.rename_button)
        tools.addWidget(self.delete_button)
        outer.addLayout(tools)

        self.rename_edit = QLineEdit()
        self.rename_edit.setObjectName("StampRenameEdit")
        self.rename_edit.setPlaceholderText(TEXT_RENAME_PROMPT)
        self.rename_edit.returnPressed.connect(self.apply_rename)
        self.rename_edit.installEventFilter(self)
        self.rename_edit.hide()
        outer.addWidget(self.rename_edit)

        self.reload()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - API Qt
        if watched is self.rename_edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_rename()
                return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------- состояние
    @property
    def tiles(self) -> dict[str, StampTile]:
        """Миниатюры по id (для тестов и родительского окна)."""
        return dict(self._tiles)

    def entries(self) -> list[StampEntry]:
        return [tile.entry for tile in self._tiles.values()]

    def current_entry(self) -> Optional[StampEntry]:
        if self._selected_id is None:
            return None
        tile = self._tiles.get(self._selected_id)
        return tile.entry if tile is not None else None

    def selection_data(self) -> Optional[tuple[str, str]]:
        """``(base64 PNG, имя)`` текущего выбора без создания аннотации."""
        entry = self.current_entry()
        if entry is None:
            return None
        try:
            return self.library.selection(entry.id)
        except StampLibraryError as exc:
            _report(self, str(exc), self.error_handler)
            return None

    def select_entry(self, entry_id: Any) -> bool:
        tile = self._tiles.get(str(entry_id or ""))
        if tile is None:
            return False
        self._selected_id = tile.entry.id
        for other in self._tiles.values():
            other.setChecked(other is tile)
        self.selectionChanged.emit(tile.entry)
        return True

    def choose_entry(self, entry_id: Any) -> Optional[tuple[str, str]]:
        """Выбрать штамп и отдать данные: сигнал + callback родителя."""
        if not self.select_entry(entry_id):
            _report(self, TEXT_SELECT_FIRST, self.error_handler)
            return None
        data = self.selection_data()
        if data is None:
            return None
        self.stampChosen.emit(data[0], data[1])
        if self.on_choose is not None:
            self.on_choose(data[0], data[1])
        return data

    def choose_current(self) -> Optional[tuple[str, str]]:
        """Разместить текущий выбранный штамп (кнопка «Вставить»)."""
        if self._selected_id is None:
            _report(self, TEXT_SELECT_FIRST, self.error_handler)
            return None
        return self.choose_entry(self._selected_id)

    # -------------------------------------------------------------- перечень
    def reload(self, *, select: Any = None) -> list[StampEntry]:
        """Перестроить миниатюры по текущему состоянию библиотеки."""
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._tiles.clear()
        entries = self.library.entries()
        for index, entry in enumerate(entries):
            try:
                raw = self.library.image_bytes(entry.id)
            except StampLibraryError:
                raw = b""
            tile = StampTile(entry, raw, self.drop_area)
            tile.clicked.connect(
                lambda _checked=False, entry_id=entry.id: self.select_entry(entry_id))
            tile.activated.connect(self.choose_entry)
            self.grid.addWidget(tile, index // self.columns, index % self.columns)
            self._tiles[entry.id] = tile
        if self._stretch_row >= 0:
            self.grid.setRowStretch(self._stretch_row, 0)
        self._stretch_row = max(1, (len(entries) + self.columns - 1) // self.columns)
        self.grid.setRowStretch(self._stretch_row, 1)
        self.stack.setCurrentIndex(0 if entries else 1)
        self.count_label.setText(TEXT_COUNT % len(entries))
        keep = str(select) if select else self._selected_id
        self._selected_id = None
        if keep and keep in self._tiles:
            self.select_entry(keep)
        elif entries:
            self.select_entry(entries[0].id)
        else:
            self.selectionChanged.emit(None)
        return entries

    # ------------------------------------------------------- добавление/правка
    def add_images(self) -> list[StampEntry]:
        """Открыть файловый диалог и импортировать PNG/JPEG/JPG в библиотеку."""
        paths, _selected = QFileDialog.getOpenFileNames(
            self, TEXT_ADD_DIALOG, self._last_dir, TEXT_IMAGE_FILTER)
        if not paths:
            return []
        self._last_dir = os.path.dirname(paths[0])
        imported, errors = self.library.import_many(paths)
        if errors:
            lines = "\n".join("• %s: %s" % (Path(path).name, message)
                              for path, message in errors)
            _report(self, TEXT_ADD_ERRORS % lines, self.error_handler)
        if imported:
            self.reload(select=imported[-1].id)
            self.libraryChanged.emit()
        return imported

    def begin_rename(self) -> bool:
        """Показать встроенное поле нового имени (без модальных окон)."""
        entry = self.current_entry()
        if entry is None:
            _report(self, TEXT_SELECT_FIRST, self.error_handler)
            return False
        self.rename_edit.setText(entry.name)
        self.rename_edit.show()
        self.rename_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.rename_edit.selectAll()
        return True

    def apply_rename(self) -> bool:
        entry = self.current_entry()
        if entry is None:
            self.cancel_rename()
            return False
        text = sanitize_stamp_name(self.rename_edit.text(), fallback="")
        if not text:
            _report(self, "Имя штампа не может быть пустым.", self.error_handler)
            self.rename_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            return False
        try:
            updated = self.library.rename(entry.id, self.rename_edit.text())
        except StampLibraryError as exc:
            _report(self, str(exc), self.error_handler)
            self.rename_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self.rename_edit.selectAll()
            return False
        self.cancel_rename()
        self.reload(select=updated.id)
        self.libraryChanged.emit()
        return True

    def cancel_rename(self) -> None:
        self.rename_edit.clear()
        self.rename_edit.hide()

    def delete_current(self, *, confirm: bool = True) -> bool:
        """Удалить выбранный штамп (файл документа не затрагивается)."""
        entry = self.current_entry()
        if entry is None:
            _report(self, TEXT_SELECT_FIRST, self.error_handler)
            return False
        if confirm:
            answer = QMessageBox.question(
                self, TEXT_DELETE_TITLE, TEXT_DELETE_CONFIRM % entry.name,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False
        try:
            self.library.delete(entry.id)
        except StampLibraryError as exc:
            _report(self, str(exc), self.error_handler)
            return False
        self.reload()
        self.libraryChanged.emit()
        return True

    def set_library(self, library: StampLibrary) -> None:
        self.library = library
        self._selected_id = None
        self.reload()


class StampLibraryDialog(QDialog):
    """Модальное окно библиотеки штампов: выбор возвращается родителю."""

    def __init__(self, parent: Optional[QWidget] = None,
                 library: Optional[StampLibrary] = None, *,
                 on_select: Optional[Callable[[str, str], None]] = None,
                 error_handler: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StampLibraryDialog")
        self.setWindowTitle(TEXT_TITLE)
        self.setModal(True)
        self.setMinimumSize(460, 470)
        self._selected: Optional[tuple[str, str]] = None
        self._on_select = on_select
        self._error_handler = error_handler

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        title = QLabel(TEXT_TITLE)
        title.setProperty("role", "heading")
        outer.addWidget(title)
        subtitle = QLabel(TEXT_SUB)
        subtitle.setProperty("role", "sub")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.picker = StampPicker(library, self, error_handler=error_handler)
        self.picker.stampChosen.connect(self._on_stamp_chosen)
        outer.addWidget(self.picker, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self.path_label = QLabel()
        self.path_label.setProperty("role", "tiny")
        self.path_label.setObjectName("StampLibraryPath")
        self.path_label.setToolTip("Каталог библиотеки: %s" % self.picker.library.root)
        self.path_label.setText(self.path_label.fontMetrics().elidedText(
            str(self.picker.library.root), Qt.TextElideMode.ElideMiddle, 240))
        footer.addWidget(self.path_label, 1)
        self.cancel_button = QPushButton(TEXT_CANCEL)
        self.cancel_button.setObjectName("StampCancel")
        self.cancel_button.setProperty("role", "ghost")
        self.cancel_button.clicked.connect(lambda _checked=False: self.reject())
        self.insert_button = QPushButton(TEXT_INSERT)
        self.insert_button.setObjectName("StampInsert")
        self.insert_button.setProperty("role", "primary")
        self.insert_button.setDefault(True)
        self.insert_button.setAutoDefault(True)
        self.insert_button.clicked.connect(lambda _checked=False: self.accept_selection())
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.insert_button)
        outer.addLayout(footer)

    # ------------------------------------------------------------------ выбор
    def _on_stamp_chosen(self, data: str, name: str) -> None:
        self._selected = (data, name)
        if self._on_select is not None:
            self._on_select(data, name)
        self.accept()

    def accept_selection(self) -> Optional[tuple[str, str]]:
        """Подтвердить выбор кнопкой «Вставить» (без выбора — подсказка)."""
        data = self.picker.choose_current()
        if data is None:
            return None
        self._selected = data
        self.accept()
        return data

    def selected_stamp(self) -> Optional[tuple[str, str]]:
        """``(base64 PNG, имя)`` после принятия окна или ``None``."""
        return self._selected


class StampPickerButton(QToolButton):
    """Кнопка-инструмент родителя: открывает библиотеку и отдаёт данные.

    ``stampChosen(base64 PNG, имя)`` — родитель сам размещает штамп вызовом
    ``set_image_data(data, "stamp")``.
    """

    stampChosen = Signal(str, str)

    def __init__(self, parent: Optional[QWidget] = None,
                 library: Optional[StampLibrary] = None, *,
                 text: str = TEXT_BUTTON, icon: Optional[QIcon] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StampPickerButton")
        self.setText(text)
        self.setToolTip(TEXT_BUTTON_TOOLTIP)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setProperty("role", "tool")
        if icon is not None:
            self.setIcon(icon)
        self._library = library
        self.clicked.connect(lambda _checked=False: self.open_picker())

    def set_library(self, library: Optional[StampLibrary]) -> None:
        self._library = library

    def open_picker(self) -> Optional[tuple[str, str]]:
        """Открыть окно библиотеки; при выборе испустить ``stampChosen``."""
        data = choose_stamp(self, self._library, on_select=self._emit_choice)
        return data

    def _emit_choice(self, data: str, name: str) -> None:
        self.stampChosen.emit(data, name)


def choose_stamp(parent: Optional[QWidget] = None,
                 library: Optional[StampLibrary] = None, *,
                 on_select: Optional[Callable[[str, str], None]] = None,
                 error_handler: Optional[Callable[[str], None]] = None
                 ) -> Optional[tuple[str, str]]:
    """Открыть библиотеку и вернуть ``(base64 PNG, имя)`` или ``None``.

    Готовый хук для ``EditorWindow``: родитель размещает штамп через
    ``set_image_data(data, "stamp")`` — данные те же при каждом размещении.
    """
    dialog = StampLibraryDialog(parent, library, on_select=on_select,
                                error_handler=error_handler)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.selected_stamp()
        return None
    finally:
        dialog.deleteLater()
