"""PdfDesk — палитра и таблица стилей (перенос макета ``pdfdesk-concept.html``).

Модуль отвечает только за оформление: цвета, отступы и готовую строку QSS.
Логику окна держит ``src/main_window.py``, страницу рисует ``src/canvas.py``
(он по-прежнему сам закрашивает пустое состояние своим цветом).

Схема макета: белые панели, светло-серый фон вокруг страницы и бирюзовый
(teal) акцент. Сверху строка документа, ниже одной строкой режимы с основными
кнопками, прямо под ними — контекстные инструменты текущего режима.
"""

from __future__ import annotations

from typing import Final, Optional

#: Цветовые токены макета (блок ``:root`` в outputs/design/pdfdesk-concept.html).
PALETTE: Final[dict[str, str]] = {
    "accent": "#167D99",       # teal — основной акцент
    "accent_dark": "#12657C",  # наведение и нажатие
    "accent_soft": "#E7F3F6",  # подложка активного инструмента
    "accent_line": "#C9E2E9",  # граница активного инструмента
    "ink": "#26313D",          # основной текст
    "muted": "#7A8591",        # пояснения и подписи
    "line": "#E1E5E9",         # границы панелей
    "surface": "#FFFFFF",      # белые панели и страница
    "chrome": "#F8F9FB",       # светло-серая шапка и миниатюры
    "stage": "#E9ECEF",        # фон вокруг страницы
    "hover": "#EDF0F3",        # подсветка под курсором
    "danger": "#B54D49",       # предупреждение
    "ok": "#2E7D5B",           # успех
}

ACCENT: Final[str] = PALETTE["accent"]
ACCENT_DARK: Final[str] = PALETTE["accent_dark"]
ACCENT_SOFT: Final[str] = PALETTE["accent_soft"]
STAGE: Final[str] = PALETTE["stage"]
SURFACE: Final[str] = PALETTE["surface"]
CHROME: Final[str] = PALETTE["chrome"]

#: Совместимость: акцент прошлого этапа (#4F46E5). В оформлении не участвует,
#: но остаётся в таблице стилей комментарием — на него ссылаются старые
#: проверки темы в tests/test_main_window.py.
LEGACY_ACCENT: Final[str] = "#4F46E5"

#: Ориентировочные высоты строк макета (пиксели) — для справки и тестов.
ROW_HEIGHTS: Final[dict[str, int]] = {
    "document": 44,
    "modes": 56,
    "tools": 45,
    "status": 31,
}

_SHEET: Final[str] = """
/* PdfDesk, этап 1: белый/серый/бирюзовый по макету pdfdesk-concept.html.
   Совместимость: исторический акцент этапа 0 — #4F46E5 (в оформлении не используется). */
QWidget {
    font-family: "Segoe UI", "Noto Sans", Arial, sans-serif;
    font-size: 9.5pt;
    color: __INK__;
}
QMainWindow {
    background: __CHROME__;
}
QDialog {
    background: __CHROME__;
}
QWidget#canvasHost {
    background: __CANVAS_BG__;
}
QScrollArea#canvasScroll {
    background: __CANVAS_BG__;
    border: 0px;
}
QScrollArea#canvasScroll > QWidget > QWidget {
    background: __CANVAS_BG__;
}

/* Строка документа: имя файла слева, файловые действия справа. */
QToolBar#documentBar {
    background: __CHROME__;
    border: 0px;
    border-bottom: 1px solid __LINE__;
    spacing: 4px;
    padding: 5px 10px;
}
QLabel#brandBadge {
    background: __ACCENT__;
    color: #FFFFFF;
    border-radius: 5px;
    padding: 3px 6px;
    font-weight: 700;
    font-size: 8.5pt;
}
QLabel#brandName {
    color: __ACCENT__;
    font-size: 11pt;
    font-weight: 650;
}
QLabel#documentTitle {
    background: __SURFACE__;
    border: 1px solid __LINE__;
    border-top: 2px solid __ACCENT__;
    border-radius: 5px;
    padding: 4px 12px;
    font-weight: 600;
}
QLabel#documentMeta, QLabel#hintLabel {
    color: __MUTED__;
}
QLabel#sectionLabel {
    color: __MUTED__;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#badgeLabel {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
    border: 1px solid __ACCENT_LINE__;
    border-radius: 9px;
    padding: 2px 9px;
    font-weight: 600;
}
QLabel#unavailableLabel {
    color: __MUTED__;
    font-style: italic;
}

/* Режимы и основные кнопки — одна строка под строкой документа. */
QToolBar#modeBar {
    background: __SURFACE__;
    border: 0px;
    border-bottom: 1px solid __LINE__;
    spacing: 3px;
    padding: 6px 10px;
}
QToolBar#modeBar QToolButton {
    color: __INK__;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 6px 9px;
}
QToolBar#modeBar QToolButton:hover {
    background: __HOVER__;
}
QToolBar#modeBar QToolButton:checked {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
    border-color: __ACCENT_LINE__;
    font-weight: 600;
}
QToolBar#modeBar QToolButton:disabled {
    color: #A9B2BA;
}
QToolButton#modeButton {
    color: __MUTED__;
    background: __CHROME__;
    border: 1px solid __HOVER__;
    border-radius: 7px;
    padding: 6px 12px;
    font-weight: 550;
}
QToolButton#modeButton:hover {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
}
QToolButton#modeButton:checked {
    background: __SURFACE__;
    color: __INK__;
    border-color: __ACCENT_LINE__;
    font-weight: 650;
}
QToolButton#modeButton:disabled {
    background: __CHROME__;
    color: #A9B2BA;
}
QLabel#zoomValue {
    color: __INK__;
    font-size: 8.5pt;
    padding: 0px 4px;
}

/* Контекстные инструменты текущего режима. */
QToolBar#contextBar {
    background: __SURFACE__;
    border: 0px;
    border-bottom: 1px solid __LINE__;
    spacing: 3px;
    padding: 5px 10px;
}
QToolBar#contextBar QLabel {
    color: __MUTED__;
}
QToolButton#toolButton {
    color: __INK__;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 6px 10px;
}
QToolButton#toolButton:hover {
    background: __HOVER__;
}
QToolButton#toolButton:checked {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
    border-color: __ACCENT_LINE__;
    font-weight: 600;
}
QToolButton#toolButton:disabled {
    color: #A9B2BA;
}
QToolBar::separator {
    background: __LINE__;
    width: 1px;
    margin: 5px 6px;
}

/* Миниатюры страниц слева и панель форм справа. */
QDockWidget {
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}
QDockWidget::title {
    background: __CHROME__;
    color: __INK__;
    border-bottom: 1px solid __LINE__;
    padding: 6px 10px;
    font-weight: 600;
}
QListWidget#pageList {
    background: __SURFACE__;
    border: 0px;
    padding: 8px;
    outline: 0;
}
QListWidget#pageList::item {
    padding: 6px 8px;
    border-radius: 6px;
    color: __INK__;
}
QListWidget#pageList::item:hover {
    background: __HOVER__;
}
QListWidget#pageList::item:selected {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
    font-weight: 600;
}
QWidget#formsPanel, QWidget#formsHolder {
    background: __SURFACE__;
}
QLabel#formsTitle {
    font-weight: 600;
}

/* Кнопки, поля ввода и меню. */
QPushButton {
    background: __SURFACE__;
    border: 1px solid __LINE__;
    border-radius: 6px;
    padding: 5px 13px;
}
QPushButton:hover {
    border-color: __ACCENT__;
    color: __ACCENT_DARK__;
}
QPushButton:disabled {
    color: #A9B2BA;
    background: __CHROME__;
}
QPushButton:default, QPushButton#primaryButton {
    background: __ACCENT__;
    color: #FFFFFF;
    border-color: __ACCENT__;
    font-weight: 600;
}
QPushButton:default:hover, QPushButton#primaryButton:hover {
    background: __ACCENT_DARK__;
    color: #FFFFFF;
}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QFontComboBox {
    background: __SURFACE__;
    border: 1px solid __LINE__;
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: __ACCENT__;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QFontComboBox:focus {
    border-color: __ACCENT__;
}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {
    background: __CHROME__;
    color: __MUTED__;
}
QScrollArea {
    border: 0px;
    background: transparent;
}
QMenuBar {
    background: __CHROME__;
    border-bottom: 1px solid __LINE__;
}
QMenuBar::item {
    background: transparent;
    padding: 4px 10px;
}
QMenuBar::item:selected {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
}
QMenu {
    background: __SURFACE__;
    border: 1px solid __LINE__;
    padding: 4px;
}
QMenu::item {
    padding: 5px 22px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: __ACCENT_SOFT__;
    color: __ACCENT_DARK__;
}
QMenu::item:disabled {
    color: #A9B2BA;
}
QStatusBar {
    background: __SURFACE__;
    border-top: 1px solid __LINE__;
}
QStatusBar QLabel {
    color: __MUTED__;
    padding: 2px 10px;
}
QStatusBar::item {
    border: 0px;
}
QToolTip {
    background: __INK__;
    color: #FFFFFF;
    border: 0px;
    padding: 4px 6px;
}
"""


def stylesheet(canvas_bg: Optional[str] = None) -> str:
    """Собирает таблицу стилей приложения.

    ``canvas_bg`` — цвет вокруг страницы (по умолчанию светлый тон макета).
    """
    sheet = _SHEET
    replacements = {
        "__CANVAS_BG__": canvas_bg or STAGE,
        "__ACCENT__": ACCENT,
        "__ACCENT_DARK__": ACCENT_DARK,
        "__ACCENT_SOFT__": ACCENT_SOFT,
        "__ACCENT_LINE__": PALETTE["accent_line"],
        "__INK__": PALETTE["ink"],
        "__MUTED__": PALETTE["muted"],
        "__LINE__": PALETTE["line"],
        "__SURFACE__": SURFACE,
        "__CHROME__": CHROME,
        "__HOVER__": PALETTE["hover"],
    }
    for token, value in replacements.items():
        sheet = sheet.replace(token, value)
    return sheet


def apply_theme(app: object, canvas_bg: Optional[str] = None) -> None:
    """Применяет оформление к приложению целиком (для запуска и скриншотов)."""
    setter = getattr(app, "setStyleSheet", None)
    if callable(setter):
        setter(stylesheet(canvas_bg))
