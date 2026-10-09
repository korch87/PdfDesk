# -*- coding: utf-8 -*-
"""Тема нового интерфейса PdfDesk: цвета, размеры, шрифты и QSS из макета.

Единственный источник значений — утверждённый макет
``outputs/design/pdfdesk-concept.html`` (блок ``:root`` и правила раскладки):
бирюзовый акцент ``#167d99``, бело-серая основа, строки 44/56/45, миниатюры
188, свойства 248, статус 31. Модуль намеренно не зависит от Qt: это чистые
данные и строки, поэтому тему можно проверять тестами без графической среды.

Палитра тёмной темы повторяет ``body.dark`` макета и не является отдельным
оформлением: меняются только цвета, размеры остаются прежними.
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------------------
# Палитра (светлая тема — значения :root и правил макета)
# ---------------------------------------------------------------------------
ACCENT = "#167d99"
ACCENT_SOFT = "#e7f3f6"
INK = "#26313d"
MUTED = "#7a8591"
LINE = "#e1e5e9"
SURFACE = "#ffffff"
CHROME = "#f8f9fb"
STAGE = "#e9ecef"
BRAND = "#ed655d"
HOVER = "#edf0f3"
HOVER_ROW = "#eef1f3"
TOOLTIP_BG = "#28333e"
SWITCH_BORDER = "#edf0f3"
SWITCH_OFF = "#cbd3db"
DANGER = "#b54d49"
STATUS_DOT = "#74a78e"

PAPER_INK = "#27313c"
PAPER_MUTED = "#7d8791"
PAPER_RULE = "#293c47"
FIELD_BORDER = "#dae2e8"
FIELD_BG = "#fbfcfd"
FIELD_BG_FILL = "#f5fbfe"
FIELD_BORDER_FILL = "#c6dde7"
THUMB_BORDER = "#d5dbe0"
THUMB_LINE = "#d6dce2"
THUMB_TITLE = "#8994a0"
THUMB_FIELD_BG = "#eaf3f7"
THUMB_FIELD_BORDER = "#dce8ee"
NOTE_BG = "#fff5d9"
NOTE_BORDER = "#efdda9"
NOTE_INK = "#b28729"
RULER_LINE = "#aab4be"
RULER_TEXT = "#6a7784"
TOAST_BG = "#263744"

#: Пять цветов палитры свойств из макета (``data-color``).
PALETTE: tuple[str, ...] = ("#26313d", "#167d99", "#cb625e", "#55a087", "#c8a448")
#: Цвет пометки по умолчанию (активный образец макета — бирюзовый).
DEFAULT_ANNOTATION_COLOR = ACCENT
#: Тёмная тема макета: ``body.dark`` переопределяет только цвета.
DARK_OVERRIDES: dict[str, str] = {
    "surface": "#252c34",
    "chrome": "#20262d",
    "stage": "#171e25",
    "ink": "#e2e7ec",
    "muted": "#9ba6b1",
    "line": "#39424b",
    "accent_soft": "#23424e",
}

# ---------------------------------------------------------------------------
# Размеры строк и панелей (px макета при 1440x1000)
# ---------------------------------------------------------------------------
DOC_BAR_HEIGHT = 44          # строка документов (вкладки и бренд)
MODE_BAR_HEIGHT = 56         # режимы, основные действия и «Сохранить»
TOOLS_HEIGHT = 45            # контекстные инструменты режима
SIDEBAR_WIDTH = 188          # миниатюры страниц
INSPECTOR_WIDTH = 248        # контекстные свойства и поля формы
STATUS_HEIGHT = 31           # нижняя строка состояния

ICON_SIZE = 19               # размер символа SVG (css svg{width:19px})
TOOL_ICON_SIZE = 17          # .tool svg{width:17px}
ICON_BOX = 34                # .icon{width:34px;height:34px}
ICON_BOX_HEADER = 30         # .header .icon{width:30px}
ICON_BOX_WIDTH_HEADER = 30
ICON_BOX_HEIGHT_HEADER = 32
BRAND_ICON_WIDTH = 23        # .documentbar .brand-icon
BRAND_ICON_HEIGHT = 27
BRAND_ICON_RADIUS = 5
SIDEBAR_HEADING_ICON = 26

THUMB_WIDTH = 102            # .thumb{width:102px;height:139px}
THUMB_HEIGHT = 139
THUMB_RADIUS = 2
MODE_RADIUS = 7
MODE_SWITCH_RADIUS = 10

RULER_THICKNESS = 16         # .ruler-top{height:16px}

#: Панель свойств и инструментов в макете — фиксированные ширины.
PANEL_MARGIN = 20
PANEL_PADDING = 19
TOOLS_PADDING = 16

# ---------------------------------------------------------------------------
# Типографика
# ---------------------------------------------------------------------------
#: Стек шрифтов макета; для Qt берём первый доступный.
FONT_STACK: tuple[str, ...] = ("Segoe UI", "Arial", "Helvetica", "sans-serif")
FONT_BASE_PX = 13
FONT_MODE_PX = 12
FONT_SMALL_PX = 11
FONT_TINY_PX = 10
FONT_HEADING_PX = 13
FONT_TITLE_PX = 17
FONT_STATUS_PX = 10
FONT_ZOOM_PX = 11

#: Список шрифтов панели свойств макета (используется как запасной).
FONT_FAMILIES: tuple[str, ...] = ("Arial", "Georgia", "Times New Roman", "Verdana")
#: Кегли из макета (``#font-size``).
FONT_SIZES: tuple[int, ...] = (10, 12, 14, 16, 18, 22, 28)
#: Толщины линии: макет (1/2/3/5) плюс расширение рисования (8/12).
STROKE_WIDTHS: tuple[int, ...] = (1, 2, 3, 5, 8, 12)

#: Масштаб: шаг макета 0,1; диапазон расширен под реальные документы.
ZOOM_MIN = 0.4
ZOOM_MAX = 3.0
ZOOM_STEP = 0.1
ZOOM_PRESETS: tuple[int, ...] = (50, 75, 100, 125, 150, 200, 300)

#: Формат страницы A4 в пунктах PDF (макет: 594x840).
PAPER_WIDTH = 594.0
PAPER_HEIGHT = 840.0

#: Подсказки инструментов появляются примерно через 1,2 c (ТЗ: 1–1,5 c).
TOOLTIP_DELAY_MS = 1200
#: Сообщение-подсказка снизу исчезает через 3,2 c (макет: 3200).
TOAST_MS = 3200
#: Интервалы автосохранения из настроек макета.
AUTOSAVE_INTERVALS_MIN: tuple[int, ...] = (1, 3, 5)
AUTOSAVE_DEFAULT_MIN = 3

#: Имена режимов и их статусы (русские подписи макета).
MODE_LABELS: dict[str, str] = {
    "read": "Просмотр",
    "fill": "Заполнение",
    "comment": "Пометки",
    "edit": "Редактирование",
    "pages": "Страницы",
}
MODE_STATUS: dict[str, str] = {
    "read": "Просмотр документа",
    "fill": "Заполнение формы",
    "comment": "Пометки",
    "edit": "Редактирование документа",
    "pages": "Управление страницами",
}
MODE_ORDER: tuple[str, ...] = ("read", "fill", "comment", "edit", "pages")


def font_family_stack() -> str:
    """Строка семейства шрифтов для QSS (первый доступный выбирает Qt)."""
    return ", ".join('"%s"' % name for name in FONT_STACK)


# ---------------------------------------------------------------------------
# Таблица стилей
# ---------------------------------------------------------------------------
_QSS = Template(
    """
* { outline: none; }
QWidget { color: $ink; font-family: $font_stack; font-size: ${base_px}px; }
QMainWindow, QDialog { background: $chrome; }
QToolTip {
    background: $tooltip_bg; color: #ffffff; border: 0; border-radius: 6px;
    padding: 7px 10px; font-size: ${small_px}px;
}
QFrame#DocBar { background: $chrome; border-bottom: 1px solid $line; }
QFrame#ModeBar { background: $surface; border-bottom: 1px solid $line; }
QFrame#ToolsBar { background: $surface; border-bottom: 1px solid $line; }
QFrame#Sidebar { background: $chrome; border-right: 1px solid $line; }
QFrame#Inspector { background: $surface; border-left: 1px solid $line; }
QWidget#InspectorHost { background: $surface; }
QFrame#StatusBar { background: $surface; border-top: 1px solid $line; }
QWidget#Stage { background: $stage; }
QWidget#PaperArea { background: $stage; }
QScrollArea { background: $stage; border: 0; }

QLabel#Brand { font-size: 14px; font-weight: 600; letter-spacing: -0.4px; }
QLabel#BrandIcon {
    background: $brand; color: #ffffff; border-radius: ${brand_radius}px;
}
QLabel#DocMeta { color: $muted; font-size: ${small_px}px; }
QLabel#DocTitleHint { color: $muted; font-size: ${small_px}px; }
QLabel[role="heading"] { font-size: ${heading_px}px; font-weight: 600; }
QLabel[role="sub"] { color: $muted; font-size: ${small_px}px; }
QLabel[role="property"] { color: #88929d; font-size: ${tiny_px}px; }
QLabel[role="tiny"] { color: $muted; font-size: ${tiny_px}px; }
QLabel[role="hint"] {
    color: $muted; font-size: ${small_px}px; border-top: 1px solid $line;
    padding-top: 12px;
}
QLabel[role="sidebar-heading"] {
    color: #6f7a86; font-size: ${small_px}px; font-weight: 600;
    letter-spacing: 0.9px;
}
QLabel[role="mode"] { font-size: ${mode_px}px; color: $muted; }
QLabel[role="empty-title"] { font-size: 15px; font-weight: 600; }
QLabel#Toast {
    background: $toast_bg; color: #ffffff; border-radius: 8px; padding: 11px 18px;
    font-size: ${mode_px}px;
}
QLabel#StatusText { color: #89939c; font-size: ${status_px}px; }
QLabel#StatusPage, QLabel#StatusMode { color: #89939c; font-size: ${status_px}px; }
QLabel#StatusDot {
    background: $status_dot; border-radius: 3px; min-width: 5px; max-width: 5px;
    min-height: 5px; max-height: 5px;
}

QPushButton {
    border: 0; background: transparent; color: $ink; padding: 8px 10px;
    border-radius: ${mode_radius}px; font-size: ${mode_px}px;
}
QPushButton:hover { background: $hover; }
QPushButton[role="mode"] { color: $mode_text; padding: 8px 10px; font-size: ${mode_px}px; }
QPushButton[role="mode"]:hover { background: $hover; }
QPushButton[role="mode"]:checked {
    color: $ink; background: $surface; font-weight: 600; border: 1px solid $line;
}
QPushButton[role="primary"] {
    background: $accent; color: #ffffff; border-radius: ${mode_radius}px;
    padding: 8px 11px; font-weight: 600;
}
QPushButton[role="primary"]:hover { background: $accent_hover; }
QPushButton[role="primary"]:disabled { background: $switch_off; color: #f2f4f6; }
QPushButton[role="danger"] { color: $danger; }
QPushButton[role="danger"]:hover { background: $accent_soft; }
QPushButton[role="swatch"] {
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    border-radius: 11px; border: 3px solid $surface; padding: 0;
}
QPushButton[role="swatch"][swatch="on"] { border: 3px solid $surface; outline: none;
    border-color: $swatch_on; }
QPushButton[role="align"] { border: 1px solid $line; border-radius: 6px; padding: 5px; }
QPushButton[role="align"]:checked { background: $accent_soft; border-color: $accent; }
QPushButton[role="bold"] { border: 1px solid $line; border-radius: 6px; font-weight: 700; }
QPushButton[role="bold"]:checked { background: $accent_soft; border-color: $accent; color: $accent; }
QPushButton[role="ghost"] { border: 1px solid $line; border-radius: 6px; background: $chrome; }
QPushButton[role="nav"] { text-align: left; border-radius: 6px; padding: 10px; }
QPushButton[role="nav"]:checked { background: $accent_soft; color: $accent; font-weight: 600; }
QPushButton[role="close"] { padding: 2px; border-radius: 6px; }

QToolButton { border: 0; background: transparent; border-radius: ${mode_radius}px; color: $ink; }
QToolButton:hover { background: $hover; }
QToolButton:checked { background: $accent_soft; color: $accent; }
QToolButton:disabled { color: $disabled; }
QToolButton[role="tool"] {
    padding: 7px 9px; font-size: ${mode_px}px; color: $ink;
}
QToolButton[role="tool"]:checked { background: $accent_soft; color: $accent; font-weight: 600; }
QToolButton[role="icon"] { padding: 5px; }
QToolButton[role="pin"] { padding: 5px; }
QToolButton[role="pin"]:checked { background: $accent_soft; color: $accent; }

QWidget#ModeSwitch { background: $chrome; border: 1px solid $switch_border;
    border-radius: ${mode_switch_radius}px; }
QFrame[role="sep"] { background: $line; border: 0; }

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QKeySequenceEdit {
    border: 1px solid $line; background: $surface; border-radius: 6px;
    padding: 6px 8px; font-size: ${mode_px}px; color: $ink;
    selection-background-color: $accent; selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QKeySequenceEdit:focus {
    border-color: $accent;
}
QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled {
    background: $chrome; color: $muted;
}
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    border: 1px solid $line; background: $surface; selection-background-color: $accent_soft;
    selection-color: $accent; outline: none;
}
QKeySequenceEdit { font-family: $font_stack; }

QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent; color: $muted; padding: 4px 10px; margin: 2px 3px 2px 0;
    border: 1px solid $line; border-top: 2px solid transparent; border-radius: 6px;
    font-size: ${mode_px}px;
}
QTabBar::tab:selected {
    background: $surface; color: $ink; font-weight: 600; border-top: 2px solid $accent;
}
QTabBar::tab:hover { background: $hover; }
QTabBar::close-button { subcontrol-position: right; }

QScrollArea#ThumbScroll { background: $chrome; border: 0; }
QWidget#ThumbHost { background: $chrome; }
QFrame#ThumbRow { background: transparent; border-radius: 4px; }
QFrame#ThumbRow[active="true"] { background: $hover_row; }
QLabel#ThumbImage {
    background: #ffffff; border: 1px solid $thumb_border; border-radius: ${thumb_radius}px;
}
QLabel#ThumbNumber { color: $muted; font-size: ${tiny_px}px; }
QLabel#ThumbNumber[active="true"] { color: $accent; font-weight: 600; }
QLabel#SideBottom {
    color: #929ba5; font-size: ${tiny_px}px; border-top: 1px solid $line; padding-top: 12px;
}
QFrame#PageCard {
    background: $chrome; border: 1px solid $line; border-radius: 9px;
}
QFrame#PageCard[active="true"] { border: 2px solid $accent; }
QLabel#PageCardLabel { color: $muted; font-size: ${small_px}px; }
QLabel#PageCardLabel[active="true"] { color: $accent; font-weight: 600; }

QFrame#Placeholder { background: $stage; border: 0; }
QFrame#SearchResult { background: $surface; border: 1px solid $line; border-radius: 6px; }
QFrame#SearchResult:hover { background: $hover; }
QLabel#SearchTitle { font-weight: 600; font-size: ${mode_px}px; }
QLabel#SearchMeta { color: $muted; font-size: ${tiny_px}px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #bdc6ce; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #bdc6ce; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QMenu { background: $surface; border: 1px solid $line; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 5px; }
QMenu::item:selected { background: $accent_soft; color: $accent; }
QMenu::separator { height: 1px; background: $line; margin: 4px 6px; }

QDialog#Settings { background: $surface; border: 1px solid $line; border-radius: 14px; }
QLabel#SettingsTitle { font-size: ${title_px}px; font-weight: 600; }
QFrame#SettingsTop { border-bottom: 1px solid $line; }
QFrame#SettingsFooter { border-top: 1px solid $line; }
QWidget#SettingsNav { background: $chrome; border-right: 1px solid $line; }
QWidget#SettingsContent { background: $surface; }
QLabel#SettingRowLabel { font-size: ${mode_px}px; }
QFrame#SettingRow { border-bottom: 1px solid $line; }
QLabel[role="key"] {
    border: 1px solid $line; border-radius: 5px; background: $chrome; color: #657180;
    padding: 5px 9px; font-size: ${small_px}px;
}
"""
)


def _values(dark: bool = False) -> dict[str, object]:
    """Значения для подстановки в QSS (тёмная тема повторяет ``body.dark``)."""
    surface = DARK_OVERRIDES["surface"] if dark else SURFACE
    chrome = DARK_OVERRIDES["chrome"] if dark else CHROME
    stage = DARK_OVERRIDES["stage"] if dark else STAGE
    ink = DARK_OVERRIDES["ink"] if dark else INK
    muted = DARK_OVERRIDES["muted"] if dark else MUTED
    line = DARK_OVERRIDES["line"] if dark else LINE
    accent_soft = DARK_OVERRIDES["accent_soft"] if dark else ACCENT_SOFT
    values: dict[str, object] = {
        "accent": ACCENT,
        "accent_hover": "#12697f" if not dark else "#1d8fae",
        "accent_soft": accent_soft,
        "ink": ink,
        "muted": muted,
        "line": line,
        "surface": surface,
        "chrome": chrome,
        "stage": stage,
        "brand": BRAND,
        "hover": "#35414c" if dark else HOVER,
        "hover_row": "#2e3842" if dark else HOVER_ROW,
        "tooltip_bg": TOOLTIP_BG,
        "switch_border": line if dark else SWITCH_BORDER,
        "switch_off": SWITCH_OFF,
        "danger": DANGER,
        "status_dot": STATUS_DOT,
        "toast_bg": TOAST_BG,
        "thumb_border": "#39424b" if dark else THUMB_BORDER,
        "thumb_radius": THUMB_RADIUS,
        "swatch_on": "#8594a0" if not dark else "#c3ccd4",
        "disabled": "#5d6873" if dark else "#a8b0b8",
        "mode_text": "#a6b3bf" if dark else "#6d7883",
        "font_stack": font_family_stack(),
        "base_px": FONT_BASE_PX,
        "mode_px": FONT_MODE_PX,
        "small_px": FONT_SMALL_PX,
        "tiny_px": FONT_TINY_PX,
        "heading_px": FONT_HEADING_PX,
        "title_px": FONT_TITLE_PX,
        "status_px": FONT_STATUS_PX,
        "brand_radius": BRAND_ICON_RADIUS,
        "mode_radius": MODE_RADIUS,
        "mode_switch_radius": MODE_SWITCH_RADIUS,
    }
    return values


def build_qss(dark: bool = False) -> str:
    """Таблица стилей окна редактора (светлая или тёмная тема макета)."""
    return _QSS.substitute(_values(dark))


def dimensions() -> dict[str, int]:
    """Ключевые размеры строк и панелей — для проверок соответствия макету."""
    return {
        "doc_bar": DOC_BAR_HEIGHT,
        "mode_bar": MODE_BAR_HEIGHT,
        "tools": TOOLS_HEIGHT,
        "sidebar": SIDEBAR_WIDTH,
        "inspector": INSPECTOR_WIDTH,
        "status": STATUS_HEIGHT,
        "icon": ICON_SIZE,
        "tool_icon": TOOL_ICON_SIZE,
        "thumb_width": THUMB_WIDTH,
        "thumb_height": THUMB_HEIGHT,
        "ruler": RULER_THICKNESS,
    }
