# -*- coding: utf-8 -*-
"""Иконки PdfDesk: символы SVG из утверждённого макета, отрисованные Qt.

Правила модуля (из контракта пересборки):

* никаких эмодзи и системных «стоковых» иконок Qt — только собственные
  символы ``<symbol id="icon-…">`` файла ``outputs/design/pdfdesk-concept.html``;
* источник символов читается из макета, если файл доступен; иначе используется
  встроенная копия ТЕХ ЖЕ символов (сборка без папки outputs продолжает
  работать и не подменяет иконки);
* отрисовка — :class:`PySide6.QtSvg.QSvgRenderer` в прозрачный растр
  (19 px, обводка 1,65 px, ``stroke-linecap/linejoin: round`` как в CSS макета);
* состояние (обычное/активное/выключенное) задаётся цветом обводки, потому что
  в макете цвет идёт от ``currentColor``.

Модуль не зависит от остальных модулей приложения.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: Переменная окружения с путём к макету (используется проверками и сборкой).
CONCEPT_HTML_ENV = "PDFDESK_CONCEPT_HTML"
#: Путь к макету относительно корня репозитория.
CONCEPT_HTML_RELPATH = ("outputs", "design", "pdfdesk-concept.html")
#: Область отрисовки всех символов макета.
VIEW_BOX = "0 0 24 24"
#: Значения обводки из CSS макета (``svg{stroke-width:1.65}``).
STROKE_WIDTH = 1.65
#: Цвет обычной иконки — ``--ink`` макета.
INK = "#26313d"
#: Цвет активной иконки — ``--accent`` макета.
ACCENT = "#167d99"
#: Цвет выключенной иконки.
DISABLED = "#a8b0b8"

#: Встроенные символы: точные копии ``<symbol>`` из pdfdesk-concept.html.
EMBEDDED_SYMBOLS: dict[str, str] = {
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6M8 13h8M8 17h5"/>',
    "open": '<path d="M3 7V5h6l2 2h9v4M3 9h18l-3 10H3z"/>',
    "save": '<path d="M4 3h13l3 3v15H4zM8 3v6h8V3M8 21v-8h8v8"/>',
    "print": '<path d="M7 8V3h10v5M7 17H4V9h16v8h-3M7 14h10v7H7zM17 11h.01"/>',
    "settings": '<path d="m9 3-1 3-3 1-2 3 2 2-1 3 3 2 2-1 3 2 3-1 1-3 3-1 2-3-2-2 1-3-3-2-2 1-3-2z"/><circle cx="12" cy="11" r="3"/>',
    "undo": '<path d="M8 4 3 9l5 5M3 9h10a6 6 0 0 1 0 12"/>',
    "redo": '<path d="m16 4 5 5-5 5M21 9H11a6 6 0 0 0 0 12"/>',
    "minus": '<path d="M5 12h14"/>',
    "plus": '<path d="M5 12h14M12 5v14"/>',
    "fit": '<path d="M5 3h14v18H5zM8 12h8m-6-2-2 2 2 2m4-4 2 2-2 2"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "text": '<path d="M4 5h16M12 5v15M8 20h8M4 5v3M20 5v3"/>',
    "cursor": '<path d="m5 3 14 10-7 1-3 7z"/>',
    "pen": '<path d="m15 4 5 5-10 10-6 1 1-6zM13 6l5 5M4 20l3-3"/>',
    "highlight": '<path d="m15 3 6 6-9 9-6-6zM6 12l-2 5 3 3 5-2M3 22h18"/>',
    "note": '<path d="M4 4h16v12l-5 5H4zM15 21v-5h5M8 8h8M8 12h5"/>',
    "signature": '<path d="M3 17c7-2 12-10 11-13-1-4-8 10-5 13 2 2 7-7 6-3-1 3 1 4 6 0M3 21h18"/>',
    "stamp": '<path d="M5 15h14l2 5H3zM8 15v-3c0-2 2-2 2-4V5a2 2 0 0 1 4 0v3c0 2 2 2 2 4v3M5 23h14"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "pin": '<path d="m9 3 7 1-1 6 4 4-7 1-4 5-1-7-4-4 5-1zM12 15l-4 6"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 4-5 4 6"/>',
    "shapes": '<rect x="3" y="3" width="11" height="11" rx="1"/><circle cx="16" cy="16" r="5"/>',
    "rotate": '<path d="M3 10a9 9 0 1 1 3 9M3 3v7h7"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    "ruler": '<path d="m3 15 12-12 6 6L9 21zM6 12l3 3M9 9l2 2M12 6l3 3"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "align": '<path d="M4 5h16M4 10h10M4 15h16M4 20h10"/>',
    "center": '<path d="M4 5h16M7 10h10M4 15h16M7 20h10"/>',
    "right": '<path d="M4 5h16M10 10h10M4 15h16M10 20h10"/>',
}

_SYMBOL_RE = re.compile(
    r"<symbol\s+id=\"icon-(?P<name>[A-Za-z0-9_-]+)\"\s+viewBox=\"(?P<viewbox>[^\"]+)\"\s*>"
    r"(?P<body>.*?)</symbol>",
    re.S,
)


def parse_symbols(markup: str) -> dict[str, str]:
    """Разобрать ``<symbol id="icon-…">`` макета в словарь имя → содержимое."""
    result: dict[str, str] = {}
    for match in _SYMBOL_RE.finditer(markup or ""):
        name = match.group("name")
        body = match.group("body").strip()
        if name and body:
            result[name] = body
    return result


def repository_root() -> Path:
    """Корень репозитория (на два уровня выше ``app/src``)."""
    return Path(__file__).resolve().parents[2]


def concept_html_path(explicit: Optional[os.PathLike | str] = None) -> Optional[Path]:
    """Найти файл макета: явный путь → переменная окружения → корень репозитория."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_value = os.environ.get(CONCEPT_HTML_ENV, "").strip()
    if env_value:
        candidates.append(Path(env_value))
    candidates.append(repository_root().joinpath(*CONCEPT_HTML_RELPATH))
    candidates.append(Path.cwd().joinpath(*CONCEPT_HTML_RELPATH))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - недоступный путь просто пропускаем
            continue
    return None


def load_symbols(explicit: Optional[os.PathLike | str] = None) -> tuple[dict[str, str], Optional[Path]]:
    """Символы макета: из файла, если он есть, иначе встроенная копия.

    Возвращает пару «словарь символов, путь к макету или None».
    """
    path = concept_html_path(explicit)
    if path is not None:
        try:
            markup = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - файл найден, но не читается
            markup = ""
        parsed = parse_symbols(markup)
        if parsed:
            return parsed, path
    return dict(EMBEDDED_SYMBOLS), None


def symbol_names() -> tuple[str, ...]:
    """Имена всех символов макета (порядок встроенной копии)."""
    return tuple(EMBEDDED_SYMBOLS)


def difference_from_embedded(explicit: Optional[os.PathLike | str] = None) -> dict[str, tuple[str, str]]:
    """Расхождения между символами макета и встроенной копией.

    Пустой словарь означает, что встроенные иконки совпадают с макетом.
    Используется проверками, чтобы встроенная копия не «уплыла» от макета.
    """
    parsed, _path = load_symbols(explicit)
    diff: dict[str, tuple[str, str]] = {}
    for name, body in EMBEDDED_SYMBOLS.items():
        other = parsed.get(name)
        if other is None:
            diff[name] = (body, "")
        elif other.strip() != body.strip():
            diff[name] = (body, other.strip())
    for name, body in parsed.items():
        if name not in EMBEDDED_SYMBOLS:
            diff[name] = ("", body.strip())
    return diff


def svg_markup(
    name: str,
    *,
    color: str = INK,
    stroke_width: float = STROKE_WIDTH,
    view_box: str = VIEW_BOX,
    symbols: Optional[dict[str, str]] = None,
) -> str:
    """Полный SVG-документ одного символа (то, что отдаётся QSvgRenderer)."""
    body = (symbols or EMBEDDED_SYMBOLS).get(name)
    if body is None:
        raise KeyError("Неизвестная иконка макета: %r" % (name,))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" fill="none" '
        'stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round">'
        "%s</svg>" % (view_box, color, _fmt(stroke_width), body)
    )


def _fmt(value: float) -> str:
    text = ("%.3f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


class IconLibrary:
    """Собственные иконки приложения: SVG макета → QPixmap/QIcon с кэшем."""

    def __init__(
        self,
        html_path: Optional[os.PathLike | str] = None,
        *,
        size: int = 19,
        color: str = INK,
        stroke_width: float = STROKE_WIDTH,
        ratio: float = 1.0,
    ) -> None:
        self._symbols, self._source = load_symbols(html_path)
        if not self._symbols:  # pragma: no cover - защита от пустого макета
            self._symbols = dict(EMBEDDED_SYMBOLS)
        self.size = int(size)
        self.color = color
        self.stroke_width = float(stroke_width)
        self.ratio = max(1.0, float(ratio))
        self._pixmaps: dict[tuple[str, int, str, float, float], QPixmap] = {}
        self._icons: dict[tuple[str, int, str, float], QIcon] = {}

    # -- свойства ---------------------------------------------------------
    @property
    def source_path(self) -> Optional[Path]:
        """Откуда взяты символы: файл макета или None для встроенной копии."""
        return self._source

    @property
    def source_kind(self) -> str:
        return "html" if self._source is not None else "embedded"

    def names(self) -> tuple[str, ...]:
        return tuple(self._symbols)

    def body(self, name: str) -> str:
        try:
            return self._symbols[name]
        except KeyError:
            raise KeyError("Неизвестная иконка макета: %r" % (name,)) from None

    def markup(self, name: str, *, color: Optional[str] = None,
               stroke_width: Optional[float] = None) -> str:
        return svg_markup(
            name,
            color=color or self.color,
            stroke_width=self.stroke_width if stroke_width is None else stroke_width,
            symbols=self._symbols,
        )

    # -- отрисовка --------------------------------------------------------
    def pixmap(
        self,
        name: str,
        *,
        size: Optional[int] = None,
        color: Optional[str] = None,
        stroke_width: Optional[float] = None,
        ratio: Optional[float] = None,
    ) -> QPixmap:
        """Отрисовать символ в прозрачный растр (по умолчанию 19 px)."""
        px = int(size or self.size)
        tint = color or self.color
        stroke = self.stroke_width if stroke_width is None else float(stroke_width)
        scale = max(1.0, float(ratio if ratio is not None else self.ratio))
        key = (name, px, tint, stroke, scale)
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached
        renderer = QSvgRenderer(self.markup(name, color=tint, stroke_width=stroke).encode("utf-8"))
        if not renderer.isValid():  # pragma: no cover - символы проверены тестами
            raise ValueError("Qt не смог разобрать SVG иконки %r" % (name,))
        side = max(1, int(round(px * scale)))
        image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            renderer.render(painter, QRectF(0.0, 0.0, float(side), float(side)))
        finally:
            painter.end()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(scale)
        self._pixmaps[key] = pixmap
        return pixmap

    def icon(
        self,
        name: str,
        *,
        size: Optional[int] = None,
        color: Optional[str] = None,
        stroke_width: Optional[float] = None,
        active_color: str = ACCENT,
    ) -> QIcon:
        """QIcon с отдельной отрисовкой для обычного и активного состояния."""
        px = int(size or self.size)
        tint = color or self.color
        stroke = self.stroke_width if stroke_width is None else float(stroke_width)
        key = (name, px, tint, stroke)
        cached = self._icons.get(key)
        if cached is not None:
            return cached
        icon = QIcon()
        icon.addPixmap(
            self.pixmap(name, size=px, color=tint, stroke_width=stroke),
            QIcon.Mode.Normal,
            QIcon.State.Off,
        )
        icon.addPixmap(
            self.pixmap(name, size=px, color=DISABLED, stroke_width=stroke),
            QIcon.Mode.Disabled,
            QIcon.State.Off,
        )
        icon.addPixmap(
            self.pixmap(name, size=px, color=active_color, stroke_width=stroke),
            QIcon.Mode.Normal,
            QIcon.State.On,
        )
        icon.addPixmap(
            self.pixmap(name, size=px, color=active_color, stroke_width=stroke),
            QIcon.Mode.Active,
            QIcon.State.Off,
        )
        self._icons[key] = icon
        return icon

    def dot(self, *, color: str = ACCENT, size: int = 6) -> QPixmap:
        """Точка «есть несохранённые изменения» (CSS ``.file-dot`` макета)."""
        side = max(2, int(size))
        image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(0.0, 0.0, float(side), float(side)))
        finally:
            painter.end()
        return QPixmap.fromImage(image)

    def clear_cache(self) -> None:
        self._pixmaps.clear()
        self._icons.clear()


_DEFAULT: Optional[IconLibrary] = None


def default_library() -> IconLibrary:
    """Общая библиотека иконок приложения (символы макета из файла или копии)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = IconLibrary()
    return _DEFAULT
