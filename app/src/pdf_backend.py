# -*- coding: utf-8 -*-
"""PdfDesk — backend-сессия PDF.

Модуль реализует :class:`PdfSession`: единый объект, через который интерфейс
приложения открывает PDF, рисует страницы (pypdfium2), работает с аннотациями
(текст / заметка / выделение / чернила / прямоугольник / эллипс / линия /
стрелка / многоугольник / облако / изображение / штамп), заполняет формы
AcroForm, поворачивает, удаляет, перемещает и добавляет страницы (в том числе
пустые и новые документы с нуля), отменяет/повторяет правки и безопасно
сохраняет результат в новый файл, не затрагивая исходники.

Многоугольник сохраняется как настоящая аннотация ``/Polygon`` с ``/Vertices``,
облако — как ``/Polygon`` с облачным контуром ``/BE << /S /C >>``; в обоих
случаях пишется поток внешнего вида ``/AP``, построенный по той же геометрии,
что и холст (:mod:`shape_geometry`), поэтому вид при правке, сохранении и
печати совпадает.

Аннотации сохраняются как НАСТОЯЩИЕ аннотации PDF со своими потоками внешнего
вида (/AP), поэтому видны в сторонних просмотрщиках; приватный словарь
``/PdfDesk`` хранит extras (начертание, выравнивание, заливку и т.п.), чтобы
после повторного открытия объекты оставались редактируемыми.

Внешний API (координаты — PDF-пункты, начало в ЛЕВОМ ВЕРХНЕМ углу
*отображаемой* страницы, т.е. с учётом /CropBox и /Rotate):

* ``open(path, password=None)``      — открыть документ;
* ``new(width, height, pages)``      — создать пустой документ с нуля;
* ``insert_blank_page(index, ...)``  — вставить пустую страницу;
* ``append(paths)``                  — добавить страницы других PDF;
* ``page_count``                     — число страниц;
* ``dirty``                          — есть ли несохранённые правки;
* ``page_size(index)``               — размер отображаемой страницы в пунктах;
* ``render(index, scale=1.0)``       — PIL-изображение страницы (формы видны,
  пользовательские аннотации — нет: их рисует холст);
* ``get_annotations / add_annotation / update_annotation / remove_annotation``;
* ``rotate / delete_page / move_page``;
* ``undo / redo``;
* ``form_fields(index=None) / form_widgets(index) / set_form_value(key, value)``;
* ``save(path)``                     — атомарное сохранение в НОВЫЙ файл.

Ошибки: ``ValueError`` — неверные аргументы/формат, ``RuntimeError`` — состояние
документа и операции ввода-вывода. Тексты ошибок — на русском языке.
"""
from __future__ import annotations

import base64
import binascii
import copy
import datetime as _dt
import hashlib
import io
import logging
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import pypdfium2 as _pdfium
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import DependencyError, PdfReadError
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

try:  # контракты лежат рядом (пакет src) либо в корне (PyInstaller/плоский запуск)
    from .contracts import (
        Annotation,
        FormField,
        ink_strokes as _ink_strokes,
        normalize_ink_geometry as _normalize_ink_geometry,
        pack_ink_strokes as _pack_ink_strokes,
    )
    from . import pdf_security as _pdf_security
    from .shape_geometry import (
        cloud_bounds as _cloud_bounds,
        cloud_segments as _cloud_segments,
        normalize_polygon as _normalize_polygon,
        polygon_bounds as _polygon_bounds,
        rectangle_points as _rectangle_points,
    )
except ImportError:  # pragma: no cover - зависит от способа запуска
    from contracts import (  # type: ignore[no-redef]
        Annotation,
        FormField,
        ink_strokes as _ink_strokes,
        normalize_ink_geometry as _normalize_ink_geometry,
        pack_ink_strokes as _pack_ink_strokes,
    )
    import pdf_security as _pdf_security  # type: ignore[no-redef]
    from shape_geometry import (  # type: ignore[no-redef]
        cloud_bounds as _cloud_bounds,
        cloud_segments as _cloud_segments,
        normalize_polygon as _normalize_polygon,
        polygon_bounds as _polygon_bounds,
        rectangle_points as _rectangle_points,
    )

# pypdf-шрифты (Type0/CIDFontType2) — приватный, но стабильный API 6.x.
try:
    from pypdf._font import Font as _PypdfFont
    from pypdf._font import FontDescriptor as _PypdfFontDescriptor

    _PYPDF_FONT_API = True
except Exception:  # pragma: no cover - защита от смены внутреннего API
    _PypdfFont = None  # type: ignore[assignment]
    _PypdfFontDescriptor = None  # type: ignore[assignment]
    _PYPDF_FONT_API = False

_LOG = logging.getLogger("pdfdesk.backend")

__all__ = ["PdfSession", "resource_path", "available_font_families"]

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_MAX_RENDER_PIXELS = 80_000_000      # защита от «сожрём всю память» при рендере
_MAX_RENDER_SCALE = 20.0
_MAX_UNDO_STEPS = 24                 # снимков состояния в стеке отмены
_MAX_UNDO_BYTES = 192 * 1024 * 1024  # суммарный лимит памяти под снимки
_FORM_VALUE_COALESCE_SEC = 1.2       # склейка быстрых правок одного поля в одну отмену
_TEXT_INSET = 2.0                    # внутренний отступ текстовой аннотации, пт
_TEXT_LEADING = 1.18                 # межстрочный интервал (доля кегля)

#: Лимиты новых типов аннотаций (текст, изображения, страницы).
_MAX_TEXT_LENGTH = 20_000            # символов в одной аннотации/заметке
_MAX_FONT_SIZE = 400.0               # пт
_MAX_STROKE_WIDTH = 100.0            # пт
_MAX_IMAGE_BYTES = 24 * 1024 * 1024  # распакованные данные картинки
_MAX_IMAGE_PIXELS = 40_000_000       # пикселей в картинке
_MAX_IMAGE_SIDE = 12_000             # пикселей по стороне
_MAX_IMAGE_PLACEMENT = 2_000.0       # пт: разумный предел размера на странице
_NOTE_DEFAULT_SIZE = 20.0            # значок заметки по умолчанию, пт
_MIN_PAGE_SIZE = 3.0                 # пт
_MAX_PAGE_SIZE = 20_000.0            # пт
_MAX_NEW_PAGES = 500                 # страниц за один вызов new()

_ANNOT_KINDS = ("text", "highlight", "ink", "note", "rect", "ellipse", "line",
                "arrow", "polygon", "cloud", "image", "stamp")
_LINE_KINDS = ("line", "arrow")
_SHAPE_KINDS = ("rect", "ellipse")
#: Фигуры, у которых геометрию задают вершины ``points`` (/Polygon в PDF).
_POLYGON_KINDS = ("polygon", "cloud")
_IMAGE_KINDS = ("image", "stamp")
#: Минимальный размер многоугольника/облака по любой стороне, пт.
_MIN_POLYGON_SIZE = 0.5
#: Выравнивание текста → код /Q в PDF.
_ALIGNMENT_CODES = {"left": 0, "center": 1, "right": 2}
_UNSUPPORTED_FT = {"/Sig": "цифровая подпись", "/Btn:radio": "группа переключателей",
                   "/Btn:push": "кнопка"}

#: Биты поля /Ff, важные для разбора кнопок.
_BTN_NO_TOGGLE_TO_OFF = 1 << 14      # повторный щелчок не снимает выбор
_BTN_RADIO = 1 << 15                 # группа переключателей
_BTN_PUSHBUTTON = 1 << 16            # кнопка-действие (не значение)

#: Виды записей :meth:`PdfSession.form_widgets`.
_WIDGET_KINDS = ("text", "checkbox", "radio", "choice", "unsupported")

# Базовый набор символов, который кладётся в PDF-шрифт: латиница, кириллица,
# греческий, типографика. Нужен, чтобы пользователь мог позже печатать другие
# символы без пересборки встроенного шрифта.
_BASE_CHARS = (
    "".join(chr(c) for c in range(0x20, 0x7F))
    + "".join(chr(c) for c in range(0xA0, 0x180))
    + "".join(chr(c) for c in range(0x400, 0x460))
    + "".join(chr(c) for c in range(0x490, 0x4A0))
    + "".join(chr(c) for c in range(0x370, 0x400))
    + "".join(chr(c) for c in range(0x2000, 0x2070))
    + "".join(chr(c) for c in range(0x20A0, 0x20C0))
    + "".join(chr(c) for c in range(0x2190, 0x2200))
    + "«»„“”‘’–—…•№→←↑↓±×÷µ€₽™©®°"
)

#: Соответствие «человеческое имя семейства» → файлы (regular, bold, italic, bold-italic).
_FONT_FAMILIES: dict[str, tuple[str, str, str, str]] = {
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "times new roman": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "segoe ui": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "tahoma": ("tahoma.ttf", "tahomabd.ttf", "tahoma.ttf", "tahomabd.ttf"),
    "georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "courier new": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "consolas": ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    "cambria": ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "candara": ("candara.ttf", "candarab.ttf", "candarai.ttf", "candaraz.ttf"),
    "comic sans ms": ("comic.ttf", "comicbd.ttf", "comici.ttf", "comicz.ttf"),
    "trebuchet ms": ("trebuc.ttf", "trebucbd.ttf", "trebucit.ttf", "trebucbi.ttf"),
    "palatino linotype": ("pala.ttf", "palab.ttf", "palai.ttf", "palabi.ttf"),
    "franklin gothic": ("framd.ttf", "framd.ttf", "framd.ttf", "framd.ttf"),
    "dejavu sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
                    "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
}

#: Синонимы, которые может прислать интерфейс.
_FONT_ALIASES = {
    "helvetica": "arial",
    "helv": "arial",
    "sans": "arial",
    "sans-serif": "arial",
    "times": "times new roman",
    "courier": "courier new",
    "mono": "consolas",
    "monospace": "consolas",
    "segoe": "segoe ui",
    "system": "segoe ui",
    "calibri light": "calibri",
    "arial narrow": "arial",
    "microsoft sans serif": "arial",
    "ms sans serif": "arial",
    "serif": "times new roman",
}


def resource_path(name: str) -> str:
    """Путь к ресурсу приложения (рядом со скриптом, в бандле PyInstaller и т.п.).

    Используется как запасной вариант для поставляемых вместе с программой
    шрифтов (``assets/DejaVuSans.ttf``), если в системе не нашлось нужного.
    """
    name = str(name).replace("\\", "/").lstrip("/")
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)
    here = Path(__file__).resolve().parent
    candidates.append(here / name)
    candidates.append(here.parent / name)
    candidates.append(here.parent / "assets" / name)
    if getattr(sys, "frozen", False):  # pragma: no cover - только в собранном EXE
        candidates.append(Path(sys.executable).resolve().parent / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:  # недоступный путь — просто пропускаем
            continue
    return str(candidates[0])


def _fonts_dir() -> Path:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:\\Windows"
    return Path(windir) / "Fonts"


def _font_search_dirs() -> tuple[Path, ...]:
    """Known system and per-user font directories for the current platform."""
    if sys.platform == "win32":
        roots = (_fonts_dir(), Path.home() / "AppData/Local/Microsoft/Windows/Fonts")
    elif sys.platform == "darwin":
        roots = (Path("/System/Library/Fonts/Supplemental"),
                 Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                 Path.home() / "Library/Fonts")
    else:
        roots = (Path("/usr/share/fonts/truetype/dejavu"),
                 Path("/usr/share/fonts/truetype/liberation"),
                 Path("/usr/share/fonts/truetype/liberation2"),
                 Path.home() / ".local/share/fonts")
    return tuple(dict.fromkeys(roots))


_MAC_FONT_FAMILIES: dict[str, tuple[str, str, str, str]] = {
    "arial": ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf"),
    "times new roman": ("Times New Roman.ttf", "Times New Roman Bold.ttf",
                        "Times New Roman Italic.ttf", "Times New Roman Bold Italic.ttf"),
    "courier new": ("Courier New.ttf", "Courier New Bold.ttf",
                    "Courier New Italic.ttf", "Courier New Bold Italic.ttf"),
    "georgia": ("Georgia.ttf", "Georgia Bold.ttf", "Georgia Italic.ttf",
                "Georgia Bold Italic.ttf"),
    "verdana": ("Verdana.ttf", "Verdana Bold.ttf", "Verdana Italic.ttf",
                "Verdana Bold Italic.ttf"),
    "tahoma": ("Tahoma.ttf", "Tahoma Bold.ttf", "Tahoma Italic.ttf",
               "Tahoma Bold Italic.ttf"),
}


def _split_family(family: str) -> tuple[str, bool, bool]:
    """«Arial Bold Italic» → («arial», bold=True, italic=True)."""
    text = (family or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    bold = bool(re.search(r"\b(bold|semibold|black|heavy|bd)\b", text))
    italic = bool(re.search(r"\b(italic|oblique|it)\b", text))
    text = re.sub(r"\b(bold|semibold|black|heavy|italic|oblique|regular|normal|bd|it)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, bold, italic


def _candidate_font_files(family: str) -> list[str]:
    base, bold, italic = _split_family(family)
    canonical = _FONT_ALIASES.get(base, base)
    files = _FONT_FAMILIES.get(canonical)
    result: list[str] = []
    mac_files = _MAC_FONT_FAMILIES.get(canonical)
    if sys.platform == "darwin" and mac_files:
        index = (2 if italic else 0) + (1 if bold else 0)
        result.append(mac_files[index])
    if files:
        index = (2 if italic else 0) + (1 if bold else 0)
        result.append(files[index])
        result.extend(f for f in files if f not in result)
    # Запасные варианты: поставляемый шрифт и типовые имена.
    result.extend(["DejaVuSans.ttf", "segoeui.ttf", "arial.ttf"])
    if base and re.fullmatch(r"[a-z0-9 .+-]{1,64}", base):
        result.append(base.replace(" ", "") + ".ttf")
        result.append(base.replace(" ", "_") + ".ttf")
    return result


def find_font_file(family: str) -> str:
    """Найти шрифт в каталогах системы/пользователя или ресурсах программы.

    Возвращает путь к ``.ttf``/``.ttc``. Бросает ``ValueError``, если шрифт не
    найден — интерфейс должен показать это сообщение пользователю.
    """
    font_dirs = _font_search_dirs()
    tried: list[str] = []
    for candidate in _candidate_font_files(family):
        tried.append(candidate)
        paths = [str(folder / candidate) for folder in font_dirs]
        paths.extend((resource_path(candidate), str(_fonts_dir() / ".." / candidate)))
        for path in paths:
            try:
                if os.path.isfile(path):
                    return path
            except OSError:
                continue
    raise ValueError(
        "Шрифт «%s» не найден в системных каталогах %s или ресурсах приложения. "
        "Установите шрифт или выберите другой (Arial, Calibri, Times New Roman)." %
        (family, ", ".join(map(str, font_dirs)))
    )


def available_font_families() -> list[str]:
    """Список семейств, доступных в системе (для выпадающего списка в UI)."""
    font_dirs = _font_search_dirs()
    present: list[str] = []
    for name, files in sorted(_FONT_FAMILIES.items()):
        mac_files = _MAC_FONT_FAMILIES.get(name, ())
        candidates = (*files, *mac_files)
        for font_dir in font_dirs:
            try:
                if any((font_dir / fname).is_file() for fname in candidates):
                    present.append(name.title() if name.islower() else name)
                    break
            except OSError:
                continue
    return present


# ---------------------------------------------------------------------------
# Разбор TTF и встроенный Unicode-шрифт
# ---------------------------------------------------------------------------


@dataclass
class _TtfInfo:
    """Метрики TrueType-шрифта, приведённые к системе PDF (1000 единиц на em)."""

    path: str
    family: str
    ps_name: str
    units_per_em: float
    ascent: float
    descent: float
    cap_height: float
    x_height: float
    bbox: tuple[float, float, float, float]
    italic_angle: float
    flags: int
    char_to_gid: dict[int, int]
    char_width: dict[int, float]

    @property
    def safe_name(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", self.ps_name) or "PdfDeskFont"


_TTF_CACHE: dict[str, _TtfInfo] = {}
_TTF_CACHE_LIMIT = 8


def _load_ttf(path: str) -> _TtfInfo:
    """Разобрать TTF при помощи reportlab (без внешних зависимостей)."""
    key = os.path.normcase(os.path.abspath(path))
    cached = _TTF_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from reportlab.pdfbase.ttfonts import TTFontFile
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Не удалось загрузить модуль разбора шрифтов (reportlab).") from exc

    try:
        face = TTFontFile(path)
    except Exception as exc:
        raise ValueError("Не удалось прочитать файл шрифта «%s»: %s" % (path, exc)) from exc

    char_to_gid: dict[int, int] = {}
    char_width: dict[int, float] = {}
    for code, gid in (face.charToGlyph or {}).items():
        if not isinstance(code, int) or not isinstance(gid, int):
            continue
        if gid <= 0 or gid > 0xFFFF or 0xD800 <= gid <= 0xDFFF:
            continue
        char_to_gid[code] = gid
        width = (face.charWidths or {}).get(code)
        if width is None:
            width = (face.charWidths or {}).get(32, 500.0)
        char_width[code] = float(width)

    if not char_to_gid:
        raise ValueError("Шрифт «%s» не содержит таблицы символов (cmap)." % path)

    ascent = float(getattr(face, "ascent", 700.0) or 700.0)
    descent = float(getattr(face, "descent", -200.0) or -200.0)
    if ascent <= 0:
        ascent = 700.0
    if descent >= 0:
        descent = -200.0
    cap_height = float(getattr(face, "capHeight", 0.0) or 0.0) or ascent * 0.7
    italic_angle = float(getattr(face, "italicAngle", 0.0) or 0.0)
    raw_bbox = getattr(face, "bbox", None) or (-200.0, -300.0, 1200.0, 1000.0)
    try:
        bbox = tuple(float(v) for v in raw_bbox)  # type: ignore[assignment]
        if len(bbox) != 4:
            raise TypeError
    except (TypeError, ValueError):
        bbox = (-200.0, -300.0, 1200.0, 1000.0)

    flags = 32  # Nonsymbolic
    if abs(italic_angle) > 0.01:
        flags |= 64  # Italic

    ps_name = getattr(face, "name", b"") or getattr(face, "fullName", b"") or b"PdfDeskFont"
    family = getattr(face, "familyName", b"") or ps_name
    if isinstance(ps_name, bytes):
        ps_name = ps_name.decode("latin-1", "replace")
    if isinstance(family, bytes):
        family = family.decode("latin-1", "replace")

    info = _TtfInfo(
        path=path,
        family=str(family) or "PdfDeskFont",
        ps_name=str(ps_name) or "PdfDeskFont",
        units_per_em=float(getattr(face, "unitsPerEm", 1000) or 1000),
        ascent=ascent,
        descent=descent,
        cap_height=cap_height,
        x_height=float(getattr(face, "capHeight", 0.0) or 0.0) * 0.72 or ascent * 0.5,
        bbox=bbox,  # type: ignore[arg-type]
        italic_angle=italic_angle,
        flags=flags,
        char_to_gid=char_to_gid,
        char_width=char_width,
    )
    if len(_TTF_CACHE) >= _TTF_CACHE_LIMIT:
        _TTF_CACHE.pop(next(iter(_TTF_CACHE)))
    _TTF_CACHE[key] = info
    return info


class _EmbeddedFont:
    """Unicode-шрифт (Type0 / Identity-H), встраиваемый в сохраняемый PDF.

    Кириллица и другие не-Latin1 символы не представимы стандартными шрифтами
    PDF, поэтому мы встраиваем выбранный пользователем системный TTF целиком и
    строим для него карты «символ → глиф», ширины и CMap /ToUnicode.
    """

    def __init__(self, info: _TtfInfo, resource_name: str, label: str, extra_chars: str = "") -> None:
        if not _PYPDF_FONT_API:
            raise RuntimeError(
                "Встроенная поддержка Unicode-шрифтов недоступна: несовместимая версия pypdf."
            )
        self.info = info
        self.resource_name = resource_name
        self.label = label

        chars = set(_BASE_CHARS)
        chars.update(extra_chars)
        char_map: dict[str, str] = {}
        widths: dict[str, float] = {}
        for ch in sorted(chars):
            # pypdf строит /ToUnicode с NFKC-нормализацией: символы, которые
            # нормализуются в другой символ (например, узкие пробелы U+2000…),
            # дают дубликаты и ломают обратное отображение Unicode → CID.
            if unicodedata.normalize("NFKC", ch) != ch:
                continue
            gid = info.char_to_gid.get(ord(ch))
            if gid is None:
                continue
            key = chr(gid)
            if key in char_map:
                # один глиф — один символ (пробел и неразрывный пробел и т.п.);
                # иначе в /ToUnicode появляются дубликаты.
                continue
            char_map[key] = ch
            widths[key] = float(round(info.char_width.get(ord(ch), 500.0)))

        if not char_map:
            raise ValueError("В шрифте «%s» нет символов для встраивания." % info.family)

        space_gid = info.char_to_gid.get(0x20)
        space_char = chr(space_gid) if space_gid else " "
        space_width = float(widths.get(space_char) or 500.0)
        width_default = float(round(2 * space_width)) if space_width else 500.0
        widths.setdefault("default", width_default)

        font_file = StreamObject()
        try:
            with open(info.path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise ValueError("Не удалось прочитать файл шрифта «%s»: %s" % (info.path, exc)) from exc
        font_file.set_data(raw)
        font_file[NameObject("/Length1")] = NumberObject(len(raw))

        descriptor = _PypdfFontDescriptor(  # type: ignore[misc]
            name=info.safe_name,
            family=info.family,
            weight="Regular",
            ascent=info.ascent,
            descent=info.descent,
            cap_height=info.cap_height,
            x_height=info.x_height,
            italic_angle=info.italic_angle,
            flags=info.flags,
            bbox=info.bbox,
            font_file=font_file,
        )
        self.font = _PypdfFont(  # type: ignore[misc]
            name=info.safe_name,
            encoding="utf_16_be",
            character_map=char_map,
            sub_type="Type0",
            font_descriptor=descriptor,
            character_widths=widths,
            space_width=space_width,
            space_char=space_char,
            interpretable=True,
        )
        self._char_map = char_map
        self._widths = widths
        self._question_gid = info.char_to_gid.get(0x3F, 0)

    # -- кодирование -------------------------------------------------------
    def encode(self, text: str) -> bytes:
        """Unicode → двухбайтовые коды CID (=GID) для Identity-H."""
        out = bytearray()
        for ch in text:
            gid = self.info.char_to_gid.get(ord(ch))
            if gid is None or gid <= 0 or gid > 0xFFFF or 0xD800 <= gid <= 0xDFFF:
                gid = self._question_gid
                if not gid:
                    continue
            out += gid.to_bytes(2, "big")
        return bytes(out)

    def can_encode(self, text: str) -> bool:
        return all(ord(ch) in self.info.char_to_gid for ch in text)

    def char_width(self, ch: str) -> float:
        gid = self.info.char_to_gid.get(ord(ch))
        if gid is None:
            return float(self._widths["default"])
        return float(self._widths.get(chr(gid), self._widths["default"]))

    def text_width(self, text: str, size: float) -> float:
        return sum(self.char_width(ch) for ch in text) * size / 1000.0

    @property
    def ascent(self) -> float:
        return self.info.ascent

    def wrap_lines(self, text: str, size: float, max_width: float) -> list[str]:
        """Разбить текст по ширине (словами, при необходимости — по символам)."""
        max_width = max(1.0, float(max_width))
        lines: list[str] = []
        for paragraph in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                candidate = word if not current else current + " " + word
                if self.text_width(candidate, size) <= max_width or not current:
                    if self.text_width(candidate, size) <= max_width:
                        current = candidate
                        continue
                    # Слово само длиннее строки — режем по символам.
                    piece = ""
                    for ch in word:
                        if piece and self.text_width(piece + ch, size) > max_width:
                            lines.append(piece)
                            piece = ch
                        else:
                            piece += ch
                    current = piece
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines or [""]


_FONT_INSTANCES: dict[str, _EmbeddedFont] = {}


def get_embedded_font(family: str = "Arial", extra_chars: str = "") -> _EmbeddedFont:
    """Встроенный Unicode-шрифт для указанного семейства (кэшируется)."""
    base, bold, italic = _split_family(family)
    key = "%s|%d%d" % (_FONT_ALIASES.get(base, base), int(bold), int(italic))
    cached = _FONT_INSTANCES.get(key)
    if cached is not None and (not extra_chars or cached.can_encode(extra_chars)):
        return cached
    path = find_font_file(family)
    info = _load_ttf(path)
    label = _FONT_ALIASES.get(base, base) or "arial"
    label = label.title() if label.islower() else label
    resource_name = "PdfDesk" + re.sub(r"[^A-Za-z0-9]", "", label) + ("Bold" if bold else "")
    instance = _EmbeddedFont(info, resource_name, label, extra_chars=extra_chars)
    _FONT_INSTANCES[key] = instance
    return instance


# ---------------------------------------------------------------------------
# Геометрия страницы: отображаемые координаты <-> координаты PDF
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PageGeom:
    """CropBox страницы и её поворот; всё в пунктах PDF."""

    left: float
    bottom: float
    width: float
    height: float
    rotation: int

    @property
    def display_width(self) -> float:
        return self.height if self.rotation in (90, 270) else self.width

    @property
    def display_height(self) -> float:
        return self.width if self.rotation in (90, 270) else self.height


def _norm_rotation(value: Any) -> int:
    try:
        rotation = int(float(value))
    except (TypeError, ValueError):
        return 0
    rotation %= 360
    if rotation < 0:
        rotation += 360
    return rotation if rotation in (0, 90, 180, 270) else 0


def _display_to_user_point(geom: _PageGeom, x: float, y: float) -> tuple[float, float]:
    """Точка (x, y) отображаемой страницы (сверху-слева) → координаты PDF."""
    x_db, y_db = float(x), geom.display_height - float(y)
    rot = geom.rotation
    if rot == 0:
        ux, uy = x_db, y_db
    elif rot == 90:
        ux, uy = geom.width - y_db, x_db
    elif rot == 180:
        ux, uy = geom.width - x_db, geom.height - y_db
    else:  # 270
        ux, uy = y_db, geom.height - x_db
    return geom.left + ux, geom.bottom + uy


def _user_to_display_point(geom: _PageGeom, x: float, y: float) -> tuple[float, float]:
    """Координаты PDF → точка отображаемой страницы (сверху-слева)."""
    ux, uy = float(x) - geom.left, float(y) - geom.bottom
    rot = geom.rotation
    if rot == 0:
        x_db, y_db = ux, uy
    elif rot == 90:
        x_db, y_db = uy, geom.width - ux
    elif rot == 180:
        x_db, y_db = geom.width - ux, geom.height - uy
    else:  # 270
        x_db, y_db = geom.height - uy, ux
    return x_db, geom.display_height - y_db


def _display_rect_to_user(geom: _PageGeom, x: float, y: float, w: float, h: float
                          ) -> tuple[float, float, float, float]:
    """Прямоугольник отображаемой страницы → охватывающий прямоугольник PDF (x, y, w, h)."""
    x, y, w, h = float(x), float(y), float(w), float(h)
    corners = [
        (x, y), (x + w, y), (x, y + h), (x + w, y + h),
    ]
    pts = [_display_to_user_point(geom, px, py) for px, py in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _user_rect_to_display(geom: _PageGeom, x: float, y: float, w: float, h: float
                          ) -> tuple[float, float, float, float]:
    """Прямоугольник PDF → охватывающий прямоугольник отображаемой страницы."""
    x, y, w, h = float(x), float(y), float(w), float(h)
    corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    pts = [_user_to_display_point(geom, px, py) for px, py in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _rotation_matrix(rotation: int) -> tuple[float, float, float, float, float, float]:
    """Матрица /Matrix для потока внешнего вида аннотации.

    Содержимое потока рисуется в «отображаемой» системе координат аннотации, а
    матрица разворачивает его так, чтобы после поворота страницы (/Rotate)
    текст и штрихи выглядели прямо.
    """
    angle = math.radians(rotation % 360)
    cos_a = round(math.cos(angle), 6)
    sin_a = round(math.sin(angle), 6)
    return (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)


def _fmt(value: float) -> str:
    """Короткая запись числа для контент-потока."""
    text = "%.4f" % float(value)
    text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _parse_color_optional(color: str) -> tuple[float, float, float] | None:
    """«#2457D6» → (r, g, b) в долях единицы; ``None`` для пустой/битой строки."""
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 8:  # #AARRGGBB — альфу игнорируем
        text = text[2:]
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return (int(text[0:2], 16) / 255.0, int(text[2:4], 16) / 255.0, int(text[4:6], 16) / 255.0)
    return None


def _parse_color(color: str, default: tuple[float, float, float] = (0.14, 0.34, 0.84)
                 ) -> tuple[float, float, float]:
    """«#2457D6» → (r, g, b) в долях единицы."""
    parsed = _parse_color_optional(color)
    return parsed if parsed is not None else default


def _alignment_code(alignment: str) -> int:
    """Выравнивание текста → код /Q (неизвестное значение считается левым)."""
    return _ALIGNMENT_CODES.get(str(alignment or "").strip().lower(), 0)


def _alignment_from_q(value: Any) -> str:
    """Код /Q из PDF → выравнивание контракта."""
    try:
        code = int(value or 0)
    except (TypeError, ValueError):
        return "left"
    return {1: "center", 2: "right"}.get(code, "left")


def _color_operator(rgb: tuple[float, float, float], stroke: bool = False) -> str:
    op = "RG" if stroke else "rg"
    return "%s %s %s %s" % (_fmt(rgb[0]), _fmt(rgb[1]), _fmt(rgb[2]), op)


def _pdf_date() -> str:
    return _dt.datetime.now().strftime("D:%Y%m%d%H%M%S")


# ---------------------------------------------------------------------------
# Потоки внешнего вида (appearance streams) для аннотаций
# ---------------------------------------------------------------------------


def _ap_text_content(font: _EmbeddedFont, text: str, size: float, color: str,
                     width: float, height: float, alignment: str = "left") -> bytes:
    """Контент потока внешнего вида текстовой аннотации (система «сверху-слева»)."""
    rgb = _parse_color(color)
    inset = _TEXT_INSET
    usable = max(4.0, width - 2 * inset)
    lines = font.wrap_lines(text, size, usable)
    leading = size * _TEXT_LEADING
    ascent = font.ascent * size / 1000.0
    code = _alignment_code(alignment)
    parts = ["q", _color_operator(rgb), "BT"]
    parts.append("/%s %s Tf" % (font.resource_name, _fmt(size)))

    def _line_x(line: str) -> float:
        line_width = font.text_width(line, size)
        if code == 1:      # по центру
            return inset + max(0.0, (usable - line_width) / 2.0)
        if code == 2:      # по правому краю
            return inset + max(0.0, usable - line_width)
        return inset

    y = height - inset - ascent
    drawn = 0
    for line in lines:
        if not line:
            y -= leading
            continue
        if y < -leading:
            break
        parts.append("1 0 0 1 %s %s Tm" % (_fmt(_line_x(line)), _fmt(y)))
        parts.append("<%s> Tj" % font.encode(line).hex().upper())
        y -= leading
        drawn += 1
    if drawn == 0 and text:
        # Текст не поместился по высоте — рисуем хотя бы первую строку.
        line = lines[0]
        parts.append("1 0 0 1 %s %s Tm" % (
            _fmt(_line_x(line)), _fmt(max(inset, height - inset - ascent))))
        parts.append("<%s> Tj" % font.encode(line).hex().upper())
    parts.extend(["ET", "Q"])
    return ("\n".join(parts) + "\n").encode("ascii")


def _ap_highlight_content(color: str, width: float, height: float) -> bytes:
    rgb = _parse_color(color, (1.0, 0.92, 0.23))
    return (
        "q\n%s\n/GS0 gs\n0 0 %s %s re f\nQ\n"
        % (_color_operator(rgb), _fmt(width), _fmt(height))
    ).encode("ascii")


def _ap_ink_content(strokes: Sequence[Sequence[tuple[float, float]]], color: str,
                    stroke_width: float, pad: float, width: float, height: float) -> bytes:
    """Поток внешнего вида рисунка: каждый штрих — свой подпуть ``m…l`` и ``S``.

    Штрихи не соединяются: между подъёмами пера в потоке нет отрезка, поэтому и
    сохранённый вид, и печать показывают те же разрывы, что и холст.
    ``strokes`` — точки относительно угла данных без запаса; запас ``pad``
    добавляется здесь ровно один раз, чтобы штрих не обрезался рамкой /BBox.
    """
    rgb = _parse_color(color, (0.1, 0.1, 0.1))
    parts = ["q", _color_operator(rgb, stroke=True), "/GS0 gs",
             "%s w" % _fmt(max(0.2, stroke_width)), "1 J", "1 j"]
    for stroke in strokes:
        if not stroke:
            continue
        first = True
        for px, py in stroke:
            # Точки хранятся в отображаемой системе, внутри потока — тоже,
            # только с компенсацией отступа, чтобы штрих не обрезался.
            x = float(px) + pad
            y = height - (float(py) + pad)
            parts.append("%s %s %s" % (_fmt(x), _fmt(y), "m" if first else "l"))
            first = False
        if not first:
            parts.append("S")
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("ascii")


def _ap_note_icon_content(color: str, width: float, height: float) -> bytes:
    """Значок заметки (/Text): жёлтый квадрат с рамкой в цвете аннотации."""
    border = _parse_color(color, (0.72, 0.53, 0.04))
    body = (1.0, 0.84, 0.25)
    width = max(4.0, float(width))
    height = max(4.0, float(height))
    stroke = max(0.5, min(width, height) * 0.07)
    inset = stroke / 2.0
    parts = [
        "q",
        "/GS0 gs",
        _color_operator(body),
        "0 0 %s %s re f" % (_fmt(width), _fmt(height)),
        _color_operator(border, stroke=True),
        "%s w" % _fmt(stroke),
        "%s %s %s %s re S" % (_fmt(inset), _fmt(inset),
                              _fmt(max(1.0, width - stroke)), _fmt(max(1.0, height - stroke))),
    ]
    # Три «строки текста» внутри значка.
    line_color = (0.55, 0.40, 0.05)
    parts.append(_color_operator(line_color, stroke=True))
    parts.append("%s w" % _fmt(max(0.4, stroke * 0.7)))
    x0 = width * 0.22
    x1 = width * 0.78
    for step in range(3):
        y = height * (0.30 + 0.17 * step)
        parts.append("%s %s m %s %s l S" % (_fmt(x0), _fmt(y), _fmt(x1), _fmt(y)))
    # Загнутый уголок.
    fold = min(width, height) * 0.28
    parts.append(_color_operator(border))
    parts.append("%s %s m %s %s l %s %s l h f" % (
        _fmt(width - fold), _fmt(height), _fmt(width), _fmt(height - fold),
        _fmt(width - fold), _fmt(height - fold)))
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("ascii")


def _shape_path(kind: str, x: float, y: float, width: float, height: float) -> str:
    """Путь прямоугольника или эллипса в системе потока внешнего вида."""
    if kind != "ellipse":
        return "%s %s %s %s re" % (_fmt(x), _fmt(y), _fmt(width), _fmt(height))
    cx = x + width / 2.0
    cy = y + height / 2.0
    rx = width / 2.0
    ry = height / 2.0
    k = 0.5522847498
    return "\n".join([
        "%s %s m" % (_fmt(cx - rx), _fmt(cy)),
        "%s %s %s %s %s %s c" % (_fmt(cx - rx), _fmt(cy + k * ry),
                                 _fmt(cx - k * rx), _fmt(cy + ry), _fmt(cx), _fmt(cy + ry)),
        "%s %s %s %s %s %s c" % (_fmt(cx + k * rx), _fmt(cy + ry),
                                 _fmt(cx + rx), _fmt(cy + k * ry), _fmt(cx + rx), _fmt(cy)),
        "%s %s %s %s %s %s c" % (_fmt(cx + rx), _fmt(cy - k * ry),
                                 _fmt(cx + k * rx), _fmt(cy - ry), _fmt(cx), _fmt(cy - ry)),
        "%s %s %s %s %s %s c" % (_fmt(cx - k * rx), _fmt(cy - ry),
                                 _fmt(cx - rx), _fmt(cy - k * ry), _fmt(cx - rx), _fmt(cy)),
        "h",
    ])


def _ap_shape_content(kind: str, stroke_color: str, fill_color: str, stroke_width: float,
                      width: float, height: float) -> bytes:
    """Контент внешнего вида прямоугольника (rect) или эллипса (ellipse)."""
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    stroke_width = max(0.2, float(stroke_width))
    pad = stroke_width / 2.0 + 0.5
    bw = max(1.0, width - 2 * pad)
    bh = max(1.0, height - 2 * pad)
    path = _shape_path(kind, pad, pad, bw, bh)
    parts = ["q", "/GS0 gs", "%s w" % _fmt(stroke_width), "1 J", "1 j"]
    fill_rgb = _parse_color_optional(fill_color)
    if fill_rgb is not None:
        parts.append(_color_operator(fill_rgb))
        parts.append(path + " f")
    parts.append(_color_operator(_parse_color(stroke_color), stroke=True))
    parts.append(path + " S")
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("ascii")


def _polygon_bbox(points: Sequence[tuple[float, float]], kind: str, stroke_width: float
                  ) -> tuple[float, float, float, float]:
    """Габариты многоугольника/облака в отображаемых пунктах с запасом под обводку.

    Для облака запас включает выступ зубчатого контура, поэтому ``/Rect`` и
    ``/BBox`` потока внешнего вида вмещают весь рисунок без обрезки.
    """
    stroke = max(0.2, float(stroke_width))
    pad = stroke / 2.0 + 0.5
    if kind == "cloud":
        box = _cloud_bounds(points, pad=pad)
    else:
        box = _polygon_bounds(points)
        if box is not None:
            box = (box[0] - pad, box[1] - pad, box[2] + 2.0 * pad, box[3] + 2.0 * pad)
    if box is None:  # pragma: no cover - отсекается проверкой вершин
        return (0.0, 0.0, 0.0, 0.0)
    return (box[0], box[1], max(box[2], 0.0), max(box[3], 0.0))


def _ap_polygon_content(kind: str, points: Sequence[tuple[float, float]],
                        stroke_color: str, fill_color: str, stroke_width: float,
                        width: float, height: float) -> bytes:
    """Контент внешнего вида многоугольника и облака.

    ``points`` — вершины относительно левого верхнего угла ``/BBox`` (то есть
    уже с запасом под обводку). Ось ``y`` потока направлена вверх, поэтому
    точка отображаемой страницы переводится как ``height - y`` — так же, как у
    линий и чернил. Для облака стороны заменяются цепочкой дуг
    :func:`shape_geometry.cloud_segments`, то есть тем же контуром, что рисует
    холст.
    """
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    stroke_width = max(0.2, float(stroke_width))

    def _point(x: float, y: float) -> str:
        return "%s %s" % (_fmt(float(x)), _fmt(height - float(y)))

    ops: list[str] = []
    if kind == "cloud":
        segments = _cloud_segments([(float(px), float(py)) for px, py in points])
        if segments:
            ops.append("%s m" % _point(segments[0][0][0], segments[0][0][1]))
            for _, control1, control2, end in segments:
                ops.append("%s %s %s c" % (_point(*control1), _point(*control2),
                                           _point(*end)))
            ops.append("h")
    if not ops:  # многоугольник (и запасной вариант для вырожденного облака)
        for index, (px, py) in enumerate(points):
            ops.append("%s %s" % (_point(px, py), "m" if index == 0 else "l"))
        if ops:
            ops.append("h")
    if not ops:  # pragma: no cover - защита от пустого контура
        return b""
    path = "\n".join(ops)
    parts = ["q", "/GS0 gs", "%s w" % _fmt(stroke_width), "1 J", "1 j"]
    fill_rgb = _parse_color_optional(fill_color)
    if fill_rgb is not None:
        parts.append(_color_operator(fill_rgb))
        parts.append(path + " f")
    parts.append(_color_operator(_parse_color(stroke_color), stroke=True))
    parts.append(path + " S")
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("ascii")


def _arrow_head_size(stroke_width: float) -> float:
    """Длина наконечника стрелки (та же формула, что в :func:`_arrow_head_path`)."""
    return max(7.0, float(stroke_width) * 3.6)


def _arrow_head_path(x0: float, y0: float, x1: float, y1: float, stroke_width: float) -> str:
    """Замкнутый треугольник наконечника стрелки в точке (x1, y1)."""
    length = _arrow_head_size(stroke_width)
    angle = math.atan2(y1 - y0, x1 - x0)
    spread = math.radians(25.0)
    left = (x1 - length * math.cos(angle - spread), y1 - length * math.sin(angle - spread))
    right = (x1 - length * math.cos(angle + spread), y1 - length * math.sin(angle + spread))
    return "%s %s m %s %s l %s %s l h" % (
        _fmt(x1), _fmt(y1), _fmt(left[0]), _fmt(left[1]), _fmt(right[0]), _fmt(right[1]))


def _ap_line_content(points: Sequence[tuple[float, float]], color: str, stroke_width: float,
                     arrow: bool, width: float, height: float) -> bytes:
    """Контент внешнего вида линии/стрелки; точки — относительные, «сверху-слева»."""
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    stroke_width = max(0.2, float(stroke_width))
    rgb = _parse_color(color, (0.07, 0.07, 0.07))
    pts = [(float(px), height - float(py)) for px, py in points]
    parts = ["q", "/GS0 gs", _color_operator(rgb, stroke=True),
             "%s w" % _fmt(stroke_width), "1 J", "1 j"]
    parts.append("%s %s m" % (_fmt(pts[0][0]), _fmt(pts[0][1])))
    parts.append("%s %s l" % (_fmt(pts[-1][0]), _fmt(pts[-1][1])))
    parts.append("S")
    if arrow and len(pts) >= 2:
        parts.append(_color_operator(rgb))
        parts.append(_arrow_head_path(pts[-2][0], pts[-2][1], pts[-1][0], pts[-1][1],
                                      stroke_width) + " f")
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("ascii")


def _ap_image_content(width: float, height: float) -> bytes:
    """Контент внешнего вида изображения/штампа: растянуть XObject /Im0 на BBox."""
    return ("q\n/GS0 gs\n%s 0 0 %s 0 0 cm\n/Im0 Do\nQ\n"
            % (_fmt(max(1.0, float(width))), _fmt(max(1.0, float(height))))).encode("ascii")


# -- изображения (base64 → PIL → XObject) -----------------------------------

_DATA_URL_RE = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)
_IMAGE_CACHE: dict[str, Image.Image] = {}
_IMAGE_CACHE_LIMIT = 8


def _decode_image_data(image_data: str) -> Image.Image:
    """Декодировать base64 PNG/JPEG в PIL-изображение (RGBA) с проверкой лимитов."""
    text = str(image_data or "").strip()
    if not text:
        raise ValueError("Не заданы данные изображения (image_data в base64).")
    text = _DATA_URL_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    max_chars = ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 64
    if len(text) > max_chars:
        raise ValueError("Изображение слишком большое: данные превышают %d Мбайт."
                         % (_MAX_IMAGE_BYTES // (1024 * 1024)))
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Данные изображения повреждены: base64 не декодируется (%s)."
                         % exc) from exc
    if not raw:
        raise ValueError("Данные изображения пусты.")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("Изображение слишком большое: %d Мбайт (лимит %d Мбайт)."
                         % (len(raw) // (1024 * 1024), _MAX_IMAGE_BYTES // (1024 * 1024)))
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        converted = image.convert("RGBA")
    except Exception as exc:
        raise ValueError("Не удалось прочитать изображение (поддерживаются PNG/JPEG): %s"
                         % exc) from exc
    width, height = converted.size
    if width <= 0 or height <= 0:
        raise ValueError("Изображение имеет нулевой размер.")
    if width * height > _MAX_IMAGE_PIXELS or max(width, height) > _MAX_IMAGE_SIDE:
        raise ValueError("Изображение слишком большое: %dx%d пикселей (лимит %d Мпикс, "
                         "сторона %d)." % (width, height, _MAX_IMAGE_PIXELS // 1_000_000,
                                           _MAX_IMAGE_SIDE))
    return converted


def _load_image_cached(image_data: str) -> Image.Image:
    """Разобрать image_data с небольшим кэшем (повторные правки не тормозят)."""
    key = hashlib.md5(str(image_data or "").encode("utf-8")).hexdigest()
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    image = _decode_image_data(image_data)
    if len(_IMAGE_CACHE) >= _IMAGE_CACHE_LIMIT:
        _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
    _IMAGE_CACHE[key] = image
    return image


def _image_data_from_xobject(obj: Any, _depth: int = 0) -> str:
    """Восстановить base64 PNG из картинки-потока внешнего вида (для правки).

    Возвращает пустую строку, если поток не является растровым изображением
    (например, векторный штамп другой программы) — такой AP не перезаписываем.
    """
    if _depth > 4:
        return ""
    if not isinstance(obj, DictionaryObject):
        return ""
    subtype = str(obj.get("/Subtype", ""))
    if subtype == "/Image":
        return _image_data_from_image_stream(obj)
    resources = obj.get("/Resources")
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    if not isinstance(resources, DictionaryObject):
        return ""
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    if not isinstance(xobjects, DictionaryObject):
        return ""
    for value in xobjects.values():
        nested = value.get_object() if hasattr(value, "get_object") else value
        found = _image_data_from_xobject(nested, _depth + 1)
        if found:
            return found
    return ""


def _image_data_from_image_stream(stream: StreamObject) -> str:
    """Поток изображения PDF → base64 PNG (Flate/DCT, RGB/Gray + /SMask)."""
    try:
        width = int(stream.get("/Width", 0) or 0)
        height = int(stream.get("/Height", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
        return ""
    filters = stream.get("/Filter")
    filter_name = ""
    if filters is not None:
        if isinstance(filters, (ArrayObject, list, tuple)) and filters:
            filter_name = str(filters[0])
        else:
            filter_name = str(filters)
    image: Image.Image | None = None
    try:
        if filter_name in ("/DCTDecode", "/JPXDecode"):
            raw = stream._data if isinstance(stream._data, bytes) else b""
            if not raw:
                return ""
            image = Image.open(io.BytesIO(raw))
            image.load()
        else:
            data = stream.get_data()
            color_space = str(stream.get("/ColorSpace", "/DeviceRGB"))
            bits = int(stream.get("/BitsPerComponent", 8) or 8)
            if bits != 8:
                return ""
            if "Gray" in color_space:
                image = Image.frombytes("L", (width, height), data[: width * height])
                image = image.convert("RGB")
            else:
                image = Image.frombytes("RGB", (width, height), data[: width * height * 3])
    except Exception as exc:  # повреждённый поток — не ломаем открытие документа
        _LOG.warning("Не удалось прочитать изображение аннотации: %s", exc)
        return ""
    if image is None:
        return ""
    smask = stream.get("/SMask")
    smask = smask.get_object() if hasattr(smask, "get_object") else smask
    if isinstance(smask, StreamObject):
        try:
            mask_width = int(smask.get("/Width", 0) or 0)
            mask_height = int(smask.get("/Height", 0) or 0)
            mask = Image.frombytes("L", (mask_width, mask_height),
                                   smask.get_data()[: mask_width * mask_height])
            if (mask_width, mask_height) != image.size:
                mask = mask.resize(image.size)
            image = image.convert("RGBA")
            image.putalpha(mask)
        except Exception as exc:  # pragma: no cover - битая маска
            _LOG.warning("Не удалось прочитать маску прозрачности: %s", exc)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _extgstate(alpha: float) -> DictionaryObject:
    alpha = min(1.0, max(0.0, float(alpha)))
    return DictionaryObject({
        NameObject("/Type"): NameObject("/ExtGState"),
        NameObject("/ca"): FloatObject(alpha),
        NameObject("/CA"): FloatObject(alpha),
    })


# ---------------------------------------------------------------------------
# Разбор существующих аннотаций (обратное преобразование)
# ---------------------------------------------------------------------------

_DA_RE = re.compile(r"/([^\s/]+)\s+([-+0-9.]+)\s+Tf")
_DA_RGB_RE = re.compile(r"([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+rg")
_DA_GRAY_RE = re.compile(r"(?<![0-9.])([-+0-9.]+)\s+g(?![a-zA-Z])")

_FAMILY_FROM_RESOURCE = {
    "helv": "Arial", "helvetica": "Arial", "arial": "Arial", "arialmt": "Arial",
    "tiro": "Times New Roman", "times": "Times New Roman", "timesnewroman": "Times New Roman",
    "cour": "Courier New", "courier": "Courier New", "consola": "Consolas",
    "calibri": "Calibri", "segoeui": "Segoe UI", "verdana": "Verdana",
}


def _family_from_font_name(name: str) -> str:
    text = str(name or "").lstrip("/")
    if text.startswith("PdfDesk"):
        text = text[len("PdfDesk"):]
        text = re.sub(r"(Bold|Italic)+$", "", text)
    key = re.sub(r"[^a-z]", "", text.lower())
    if key in _FAMILY_FROM_RESOURCE:
        return _FAMILY_FROM_RESOURCE[key]
    for known in _FONT_FAMILIES:
        if re.sub(r"[^a-z]", "", known) == key:
            return known.title() if known.islower() else known
    return text or "Arial"


def _parse_da(da: str, default_size: float = 14.0) -> tuple[str, float, str]:
    """Разобрать строку /DA: (имя ресурса шрифта, кегль, цвет #RRGGBB)."""
    text = str(da or "")
    size = default_size
    font_name = ""
    match = _DA_RE.search(text)
    if match:
        font_name = match.group(1)
        try:
            size = float(match.group(2)) or default_size
        except ValueError:
            size = default_size
    color = "#000000"
    rgb = _DA_RGB_RE.search(text)
    if rgb:
        try:
            values = [min(1.0, max(0.0, float(rgb.group(i)))) for i in (1, 2, 3)]
            color = "#%02X%02X%02X" % tuple(int(round(v * 255)) for v in values)
        except ValueError:
            color = "#000000"
    else:
        gray = _DA_GRAY_RE.search(text)
        if gray:
            try:
                value = min(1.0, max(0.0, float(gray.group(1))))
                color = "#%02X%02X%02X" % ((int(round(value * 255)),) * 3)
            except ValueError:
                color = "#000000"
    return font_name, size, color


def _replace_da_font(da: str, resource_name: str) -> str:
    """Заменить имя шрифта в строке /DA, сохранив кегль и цвет."""
    text = str(da or "").strip()
    if _DA_RE.search(text):
        return _DA_RE.sub(lambda match: "/%s %s Tf" % (resource_name, match.group(2)), text, count=1)
    return "/%s 0 Tf 0 g" % resource_name


def _is_latin1(text: str) -> bool:
    """Представим ли текст стандартной кодировкой PDF (cp1252/WinAnsi)."""
    if not text:
        return True
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _pdf_color_to_hex(raw: Any, default: str) -> str:
    try:
        values = [float(v) for v in raw]  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return default
    if len(values) == 1:
        values = values * 3
    if len(values) == 3:
        return "#%02X%02X%02X" % tuple(
            int(round(min(1.0, max(0.0, v)) * 255)) for v in values)
    if len(values) == 4:  # CMYK
        c, m, y, k = (min(1.0, max(0.0, v)) for v in values)
        return "#%02X%02X%02X" % tuple(
            int(round(255 * (1 - min(1.0, ch + k)))) for ch in (c, m, y))
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    """Логическое значение из pypdf: BooleanObject хранит флаг в атрибуте .value."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    inner = getattr(value, "value", None)
    if isinstance(inner, bool):
        return inner
    if isinstance(inner, str):
        return inner.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(value)
    except Exception:  # pragma: no cover - защита от экзотических объектов
        return False


def _bold_from_font_name(name: str) -> bool:
    """Определить начертание по имени ресурса шрифта (PdfDeskArialBold, Helv-Bold…)."""
    text = str(name or "")
    return bool(re.search(r"(?i)(bold|semibold|black|heavy|bd)\b", text))


def _font_family_for(data: Annotation) -> str:
    """Семейство шрифта с учётом флага bold («Arial» + bold → «Arial Bold»)."""
    family = str(data.font_family or "Arial").strip() or "Arial"
    if getattr(data, "bold", False) and not re.search(
            r"(?i)\b(bold|semibold|black|heavy)\b", family):
        family = family + " Bold"
    return family


def _annotation_extras(data: Annotation) -> DictionaryObject:
    """Приватный словарь /PdfDesk: extras, чтобы объект остался редактируемым."""
    extras = DictionaryObject()
    extras[NameObject("/Kind")] = TextStringObject(str(data.kind))
    extras[NameObject("/Bold")] = BooleanObject(bool(data.bold))
    extras[NameObject("/Alignment")] = TextStringObject(str(data.alignment or "left"))
    extras[NameObject("/FillColor")] = TextStringObject(str(data.fill_color or ""))
    extras[NameObject("/FontFamily")] = TextStringObject(str(data.font_family or "Arial"))
    extras[NameObject("/FontSize")] = FloatObject(float(data.font_size))
    extras[NameObject("/Color")] = TextStringObject(str(data.color or "#2457D6"))
    extras[NameObject("/StrokeWidth")] = FloatObject(float(data.stroke_width))
    extras[NameObject("/Opacity")] = FloatObject(float(data.opacity))
    extras[NameObject("/Version")] = NumberObject(1)
    return extras


_ANNOTATION_METADATA_PROTECTED_KEYS = frozenset((
    "/Type", "/Subtype", "/Rect", "/F", "/AP", "/PdfDesk", "/P",
    "/Parent", "/Popup", "/IRT", "/OC", "/StructParent", "/A", "/Dest", "/AA",
    "/C", "/IC", "/CA", "/Border", "/BS", "/BE", "/QuadPoints",
    "/L", "/InkList", "/Vertices", "/RD", "/CL", "/LE", "/IT",
    "/Q", "/DA", "/DR",
))


def _safe_annotation_metadata(obj: DictionaryObject) -> dict[str, Any]:
    """Copy only scalar author/comment metadata, never geometry or object links."""
    metadata: dict[str, Any] = {}
    for raw_key, value in obj.items():
        key = str(raw_key)
        if key in _ANNOTATION_METADATA_PROTECTED_KEYS:
            continue
        if isinstance(value, IndirectObject):
            continue
        if isinstance(value, (str, int, float, bool, NameObject,
                              TextStringObject, BooleanObject, NumberObject,
                              FloatObject, NullObject)):
            metadata[key] = copy.deepcopy(value)
    return metadata


def _extras_from_pdf(obj: DictionaryObject) -> dict[str, Any]:
    """Прочитать /PdfDesk (наши extras); пустой словарь, если его нет."""
    raw = obj.get("/PdfDesk")
    if raw is None:
        return {}
    extras = raw.get_object() if hasattr(raw, "get_object") else raw
    if not isinstance(extras, DictionaryObject):
        return {}
    result: dict[str, Any] = {}
    kind = str(extras.get("/Kind", "")).strip().lower()
    if kind in _ANNOT_KINDS:
        result["kind"] = kind
    if "/Bold" in extras:
        result["bold"] = _as_bool(extras.get("/Bold"))
    alignment = str(extras.get("/Alignment", "")).strip().lower()
    if alignment in _ALIGNMENT_CODES:
        result["alignment"] = alignment
    for key, name in (("fill_color", "/FillColor"), ("color", "/Color"),
                      ("font_family", "/FontFamily")):
        if extras.get(name) is not None:
            result[key] = str(extras.get(name))
    for key, name in (("font_size", "/FontSize"), ("stroke_width", "/StrokeWidth"),
                      ("opacity", "/Opacity")):
        if extras.get(name) is not None:
            result[key] = _as_float(extras.get(name), 0.0)
    return result


def _is_cloudy_border(obj: DictionaryObject) -> bool:
    """Есть ли у аннотации облачный контур ``/BE << /S /C >>``."""
    raw = obj.get("/BE")
    be = raw.get_object() if hasattr(raw, "get_object") else raw
    if not isinstance(be, DictionaryObject):
        return False
    return str(be.get("/S", "")).strip() in ("/C", "C")


def _kind_from_subtype(obj: DictionaryObject, subtype: str) -> str:
    """Определить наш kind по стандартному /Subtype (для чужих аннотаций)."""
    if subtype == "/FreeText":
        return "text"
    if subtype == "/Text":
        return "note"
    if subtype == "/Highlight":
        return "highlight"
    if subtype == "/Ink":
        return "ink"
    if subtype == "/Square":
        return "cloud" if _is_cloudy_border(obj) else "rect"
    if subtype == "/Circle":
        return "ellipse"
    if subtype == "/Polygon":
        # Облако — тот же /Polygon, но с облачным эффектом границы.
        return "cloud" if _is_cloudy_border(obj) else "polygon"
    if subtype == "/Line":
        endings = obj.get("/LE")
        if isinstance(endings, (ArrayObject, list, tuple)) and len(endings) >= 2:
            tail = str(endings[1])
            if tail not in ("/None", "None", ""):
                return "arrow"
        return "line"
    if subtype == "/Stamp":
        return "stamp"
    return ""


def _point_pairs(points: Any, label: str = "аннотации") -> list[tuple[float, float]]:
    """Проверить и привести список пар координат к ``list[tuple[float, float]]``."""
    flat: list[tuple[float, float]] = []
    for point in (points or []):
        try:
            px, py = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ValueError(
                "Точки %s должны быть парами чисел (x, y)." % label
            ) from None
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError("Координаты точек %s должны быть числами." % label)
        flat.append((px, py))
    return flat


def _ink_strokes_from_pdf(obj: DictionaryObject,
                          geom: "_PageGeom") -> list[list[tuple[float, float]]] | None:
    """Штрихи ``/InkList`` в координатах отображаемой страницы.

    Каждый массив списка — отдельный штрих, поэтому подъёмы пера сохраняются.
    ``None`` означает, что список отсутствует или испорчен (нет массива, меньше
    двух точек, нечётное число координат, не числа): такая аннотация остаётся в
    исходном массиве страницы нетронутой и в модель редактора не попадает.
    """
    ink_list = obj.get("/InkList")
    ink_list = ink_list.get_object() if hasattr(ink_list, "get_object") else ink_list
    if not isinstance(ink_list, (ArrayObject, list, tuple)) or not len(ink_list):
        return None
    strokes: list[list[tuple[float, float]]] = []
    for item in ink_list:
        stroke = item.get_object() if hasattr(item, "get_object") else item
        if not isinstance(stroke, (ArrayObject, list, tuple)):
            return None
        try:
            values = [float(value) for value in stroke]
        except (TypeError, ValueError, OverflowError):
            return None
        if (len(values) < 4 or len(values) % 2
                or not all(math.isfinite(value) for value in values)):
            return None
        strokes.append([
            _user_to_display_point(geom, values[i], values[i + 1])
            for i in range(0, len(values), 2)
        ])
    return strokes


def _stroke_width_from_annot(obj: DictionaryObject, default: float = 2.0) -> float:
    """Толщина линии из /BS /W или /Border."""
    width = default
    bs = obj.get("/BS")
    bs_obj = bs.get_object() if hasattr(bs, "get_object") else bs
    if isinstance(bs_obj, DictionaryObject):
        width = _as_float(bs_obj.get("/W"), default) or default
    border = obj.get("/Border")
    if border is not None:
        try:
            values = [float(v) for v in border]
            if len(values) >= 3 and values[2] > 0:
                width = values[2]
        except (TypeError, ValueError):
            pass
    return max(0.2, float(width))


# ---------------------------------------------------------------------------
# Внутренние структуры сессии
# ---------------------------------------------------------------------------


@dataclass
class _AnnRecord:
    """Аннотация в модели + её объекты в PDF (создаются при сохранении)."""

    data: Annotation
    pdf_dict: DictionaryObject | None = None
    pdf_ref: IndirectObject | None = None
    ap_stream: StreamObject | None = None
    ap_ref: IndirectObject | None = None
    img_stream: StreamObject | None = None
    img_ref: IndirectObject | None = None
    smask_stream: StreamObject | None = None
    smask_ref: IndirectObject | None = None
    popup_dict: DictionaryObject | None = None
    popup_ref: IndirectObject | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    popup_metadata: dict[str, Any] = field(default_factory=dict)
    modified: bool = False
    dirty: bool = True


@dataclass
class _FieldRef:
    """Описание поля формы и его виджетов."""

    key: str
    qualified_name: str
    partial_name: str
    label: str
    kind: str
    page_index: int
    source_index: int
    field: DictionaryObject
    widgets: list[tuple[int, DictionaryObject]] = field(default_factory=list)
    extra_fields: list[DictionaryObject] = field(default_factory=list)
    ft: str = ""
    ff: int = 0

    @property
    def readonly(self) -> bool:
        return bool(self.ff & 1)

    @property
    def multiline(self) -> bool:
        return bool(self.ff & (1 << 12))

    @property
    def combo(self) -> bool:
        return bool(self.ff & (1 << 17))

    @property
    def editable_choice(self) -> bool:
        return bool(self.ff & (1 << 18))

    def options(self) -> list[str]:
        raw = self.field.get("/Opt")
        result: list[str] = []
        if isinstance(raw, (ArrayObject, list, tuple)):
            for item in raw:
                if isinstance(item, (ArrayObject, list, tuple)) and len(item) >= 2:
                    result.append(str(item[1]))
                else:
                    result.append(str(item))
        return result

    def value(self) -> str | bool:
        raw = self.field.get("/V")
        if self.kind == "checkbox":
            on_state = self.on_state()
            name = str(raw) if raw is not None else "/Off"
            return bool(on_state) and name not in ("/Off", "Off", "")
        if isinstance(raw, (ArrayObject, list, tuple)):
            return ", ".join(str(item) for item in raw)
        return "" if raw is None else str(raw)

    def on_state(self) -> str:
        """Имя «включённого» состояния флажка (ключ /AP /N кроме /Off)."""
        for _, widget in self.widgets:
            states = _widget_on_state(widget)
            if states:
                return states
        states = _widget_on_state(self.field)
        return states or "/Yes"

    def to_form_field(self) -> FormField:
        return FormField(
            key=self.key,
            label=self.label,
            kind=self.kind,
            value=self.value(),
            options=self.options(),
            page_index=self.page_index,
            readonly=self.readonly,
            multiline=self.multiline,
        )


def _widget_on_states(widget: DictionaryObject) -> list[str]:
    """Имена «включённых» состояний виджета: ключи /AP /N кроме /Off, по порядку."""
    ap = widget.get("/AP")
    if ap is None:
        return []
    try:
        normal = ap.get_object().get("/N")  # type: ignore[union-attr]
    except AttributeError:
        return []
    if normal is None:
        return []
    normal = normal.get_object() if hasattr(normal, "get_object") else normal
    if not isinstance(normal, DictionaryObject):
        return []
    return [str(key) for key in normal if str(key) not in ("/Off", "Off")]


def _widget_on_state(widget: DictionaryObject) -> str:
    """Имя «включённого» состояния флажка (ключ /AP /N кроме /Off)."""
    states = _widget_on_states(widget)
    return states[0] if states else ""


def _qualified_field_name(field: DictionaryObject, _depth: int = 0) -> str:
    """Полное имя поля (как в pypdf: учитывается /TM и цепочка /Parent)."""
    if _depth > 32:
        return ""
    if "/TM" in field:
        return str(field["/TM"])
    parent = field.get("/Parent")
    if parent is not None:
        parent_obj = parent.get_object() if hasattr(parent, "get_object") else parent
        if isinstance(parent_obj, DictionaryObject):
            prefix = _qualified_field_name(parent_obj, _depth + 1)
            own = str(field.get("/T", ""))
            return (prefix + "." + own) if prefix else own
    return str(field.get("/T", ""))


def _field_dict_for_widget(widget: DictionaryObject) -> DictionaryObject | None:
    """Найти словарь поля для виджета (виджет может быть слит с полем)."""
    if "/FT" in widget:
        return widget
    current: Any = widget
    for _ in range(32):
        parent = current.get("/Parent") if isinstance(current, DictionaryObject) else None
        if parent is None:
            return None
        current = parent.get_object() if hasattr(parent, "get_object") else parent
        if not isinstance(current, DictionaryObject):
            return None
        if "/FT" in current:
            return current
    return None


def _rect_tuple(raw: Any) -> tuple[float, float, float, float] | None:
    try:
        values = [float(v) for v in raw]  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    x0, x1 = sorted((values[0], values[2]))
    y0, y1 = sorted((values[1], values[3]))
    return x0, y0, x1 - x0, y1 - y0


@dataclass
class _Snapshot:
    revision: int
    data: bytes
    annots: list[list[Annotation]]
    page_sources: list[int]
    size: int
    refreshed_form_appearances: set[str] = field(default_factory=set)
    annotation_metadata: list[list[dict[str, Any]]] = field(default_factory=list)
    annotation_popup_metadata: list[list[dict[str, Any]]] = field(default_factory=list)
    annotation_modified: list[list[bool]] = field(default_factory=list)
    view_page_ids: list[int] = field(default_factory=list)


@dataclass
class _Source:
    index: int
    path: str
    data: bytes
    buffer: io.BytesIO
    reader: PdfReader
    prefix: str


# ---------------------------------------------------------------------------
# Основной класс сессии
# ---------------------------------------------------------------------------


class PdfSession:
    """Сессия работы с одним PDF-документом.

    Документ собирается из исходных файлов (первый — базовый, остальные
    добавляются через :meth:`append`) и живёт в памяти как дерево объектов
    pypdf; на диск ничего не пишется до вызова :meth:`save`, причём save всегда
    создаёт НОВЫЙ файл и никогда не перезаписывает исходники.
    """

    def __init__(self) -> None:
        self._writer: PdfWriter | None = None
        self._sources: list[_Source] = []
        self._source_paths: list[str] = []
        self._page_sources: list[int] = []
        self._view_page_ids: list[int] = []
        self._next_view_page_id = 0
        self._annots: list[list[_AnnRecord]] = []

        self._revision = 0        # любое изменение состояния (для dirty/undo)
        self._doc_revision = 0    # изменения структуры PDF (кэш pdfium/полей)
        self._saved_revision = 0

        self._undo: list[_Snapshot] = []
        self._redo: list[_Snapshot] = []
        self._undo_bytes = 0
        self._last_edit_key: str | None = None
        self._last_edit_time = 0.0

        self._pdfium: _pdfium.PdfDocument | None = None
        self._pdfium_revision = -1
        self._serialized: bytes | None = None
        self._serialized_revision = -1

        self._field_map: dict[str, _FieldRef] | None = None
        self._field_map_revision = -1
        self._font_refs: dict[str, tuple[_EmbeddedFont, IndirectObject]] = {}
        self._font_holders: list[DictionaryObject] = []
        self._saved_path: str | None = None
        # Credentials exist only on this live session. They are deliberately
        # excluded from snapshots, logs, settings and recovery metadata.
        self._security_info: dict[str, Any] = self._plain_security_info()
        self._security_open_password = ""
        self._security_owner_password = ""
        self._security_encryption_required = False
        self._security_source_data: bytes | None = None
        self._security_preserve_encryption = False
        self._warnings: list[str] = []
        self._has_xfa = False
        self._initial_need_appearances = False
        self._refreshed_form_appearances: set[str] = set()
        self._attached: list[tuple[ArrayObject, Any]] = []
        self._keepalive: list[Any] = []
        self._layer_cache: dict[int, tuple[StreamObject, IndirectObject, Any]] = {}
        self._layer_restore: list[Any] = []

    # -- служебное ---------------------------------------------------------
    def __del__(self) -> None:  # pragma: no cover - страховка от утечек pdfium
        try:
            self._close_pdfium()
        except Exception:
            pass

    @property
    def warnings(self) -> list[str]:
        """Предупреждения, накопленные при открытии/работе (для UI)."""
        return list(self._warnings)

    @property
    def source_paths(self) -> list[str]:
        """Пути исходных файлов (их запрещено перезаписывать при сохранении)."""
        return list(self._source_paths)

    @property
    def saved_path(self) -> str | None:
        """Путь последнего успешного сохранения (если было)."""
        return self._saved_path

    @staticmethod
    def _plain_security_info() -> dict[str, Any]:
        permissions = {name: True for name in (
            "print", "fill_forms", "modify", "annotate", "extract",
            "extract_text_and_graphics", "assemble", "print_high_quality")}
        return {"encrypted": False, "algorithm": None, "authenticated_as": None,
                "permissions": permissions, "permissions_valid": None}

    @property
    def security_info(self) -> dict[str, Any]:
        """Sanitized encryption details; never includes credentials."""
        return copy.deepcopy(self._security_info)

    @property
    def has_edit_permission(self) -> bool:
        """Whether content, page structure, annotations and fields may change."""
        if not self._security_info.get("encrypted"):
            return True
        permissions = self._security_info.get("permissions", {})
        return (self._security_info.get("authenticated_as") == "owner" or
                all(bool(permissions.get(name)) for name in ("modify", "annotate", "assemble")))

    @property
    def can_print(self) -> bool:
        return (not self._security_info.get("encrypted") or
                bool(self._security_info.get("permissions", {}).get("print")) or
                self._security_info.get("authenticated_as") == "owner")

    @property
    def can_change_security(self) -> bool:
        """Whether this session may set or remove document protection."""
        return (not self._security_info.get("encrypted") or
                self._security_info.get("authenticated_as") == "owner")

    @property
    def can_fill_forms(self) -> bool:
        """Whether the authenticated role may change form values."""
        if not self._security_info.get("encrypted"):
            return True
        return (self._security_info.get("authenticated_as") == "owner" or
                bool(self._security_info.get("permissions", {}).get("fill_forms")))

    def _require_edit_permission(self, operation: str = "edit") -> None:
        allowed = self.can_fill_forms if operation == "fill" else self.has_edit_permission
        if not allowed:
            raise PermissionError("Для этой операции требуется пароль владельца PDF.")

    def set_security(self, open_password: str = "", owner_password: str = "",
                     allow_print: bool = True, allow_fill_forms: bool = True) -> None:
        """Set or remove protection for the next save, without storing secrets externally."""
        self._require_open()
        if not self.can_change_security:
            raise PermissionError("Изменить защиту PDF может только владелец документа.")
        opening = _pdf_security._password(open_password, "open_password")
        owner = _pdf_security._password(owner_password, "owner_password")
        if not opening and not owner:
            self._security_encryption_required = False
            self._security_open_password = ""
            self._security_owner_password = ""
            self._security_info = self._plain_security_info()
            self._security_source_data = None
            self._security_preserve_encryption = False
        else:
            # Validate options before changing session state. Encryption itself
            # is deferred until save, which keeps a failed save non-destructive.
            if opening and owner and opening == owner:
                raise ValueError("Пароль открытия и пароль владельца должны различаться.")
            if not isinstance(allow_print, bool) or not isinstance(allow_fill_forms, bool):
                raise TypeError("Права печати и заполнения должны быть логическими значениями.")
            self._security_encryption_required = True
            self._security_open_password = opening
            self._security_owner_password = owner
            self._security_source_data = None
            self._security_preserve_encryption = False
            permissions = self._plain_security_info()["permissions"]
            if owner:
                permissions.update({"print": allow_print, "fill_forms": allow_fill_forms,
                                    "modify": False, "annotate": False,
                                    "extract": False, "extract_text_and_graphics": False,
                                    "assemble": False, "print_high_quality": False})
            self._security_info = {"encrypted": True, "algorithm": "AES-256",
                "authenticated_as": "owner", "permissions": permissions,
                "permissions_valid": True}
        # Security changes cannot be rolled back by a document-content snapshot.
        self._undo.clear()
        self._redo.clear()
        self._undo_bytes = 0
        self._last_edit_key = None
        self._touch(doc=False)

    def unlock_security(self, owner_password: str) -> None:
        """Upgrade a user-authenticated session after validating owner password."""
        self._require_open()
        candidate = _pdf_security._password(owner_password, "owner_password")
        if not candidate:
            raise ValueError("Введите пароль владельца PDF.")
        if not self._security_info.get("encrypted"):
            raise ValueError("У документа нет защиты для снятия.")
        authenticated = False
        if self._security_source_data:
            try:
                info = _pdf_security.inspect_pdf_protection(self._security_source_data, candidate)
                authenticated = info.get("authenticated_as") == "owner"
            except _pdf_security.PdfSecurityError:
                authenticated = False
        elif self._security_owner_password:
            import hmac
            authenticated = hmac.compare_digest(candidate, self._security_owner_password)
        if not authenticated:
            raise _pdf_security.InvalidPdfPassword("Неверный пароль владельца PDF.")
        self._security_owner_password = candidate
        self._security_info["authenticated_as"] = "owner"
        self._security_info["permissions_valid"] = True
        self._security_encryption_required = True
        self._touch(doc=False)

    def _protect_serialized_bytes(self, data: bytes) -> bytes:
        if not self._security_encryption_required:
            return data
        if self._security_preserve_encryption:
            return self._encrypt_with_original_policy(data)
        return _pdf_security.protect_pdf(
            data, open_password=self._security_open_password,
            owner_password=self._security_owner_password,
            allow_print=bool(self._security_info["permissions"].get("print", True)),
            allow_fill_forms=bool(self._security_info["permissions"].get("fill_forms", True)))

    def _encrypt_with_original_policy(self, data: bytes) -> bytes:
        """Re-encrypt rebuilt bytes with the source PDF's existing file key."""
        if not self._sources or not self._sources[0].reader.is_encrypted:
            raise RuntimeError("Не удалось сохранить исходную защиту PDF.")
        original = self._sources[0].reader
        encryption = getattr(original, "_encryption", None)
        encrypt_entry = original.trailer.get("/Encrypt")
        if encryption is None or encrypt_entry is None:
            raise RuntimeError("Механизм исходной защиты PDF недоступен.")
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            writer = PdfWriter(clone_from=reader)
            # /ID[0] is part of the password-derived key for legacy revisions.
            writer._ID = original._ID.clone(writer)
            entry_object = (encrypt_entry.get_object() if hasattr(encrypt_entry, "get_object")
                            else encrypt_entry)
            entry = entry_object.clone(writer)
            writer._add_object(entry)
            writer._encryption = encryption
            writer._encrypt_entry = entry
            output = io.BytesIO()
            writer.write(output)
        except Exception as exc:
            raise RuntimeError("Не удалось сохранить защиту исходного PDF: %s" % exc) from exc
        protected = output.getvalue()
        if not protected.startswith(b"%PDF"):
            raise RuntimeError("Не удалось создать защищённый PDF.")
        return protected

    def _require_open(self) -> PdfWriter:
        if self._writer is None:
            raise RuntimeError("PDF-документ не открыт. Сначала вызовите open().")
        return self._writer

    def _touch(self, *, doc: bool = True) -> None:
        self._revision += 1
        if doc:
            self._doc_revision += 1
            self._serialized = None

    def _page_index(self, index: Any) -> int:
        writer = self._require_open()
        try:
            value = int(index)
        except (TypeError, ValueError):
            raise ValueError("Индекс страницы должен быть целым числом.") from None
        count = len(writer.pages)
        if not (0 <= value < count):
            raise IndexError(
                "Страница %d не найдена: в документе %d стр. (нумерация с нуля)." % (value, count)
            )
        return value

    def _page(self, index: int) -> Any:
        return self._require_open().pages[index]

    # -- открытие ----------------------------------------------------------
    @staticmethod
    def _read_source(path: Any) -> tuple[str, bytes]:
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Не указан путь к PDF-файлу.")
        resolved = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(resolved):
            raise ValueError("Файл не найден: %s" % resolved)
        if os.path.isdir(resolved):
            raise ValueError("Указан каталог, а не файл: %s" % resolved)
        try:
            with open(resolved, "rb") as handle:
                data = handle.read()
        except PermissionError as exc:
            raise RuntimeError("Нет доступа к файлу «%s»: %s" % (resolved, exc)) from exc
        except OSError as exc:
            raise RuntimeError("Не удалось прочитать файл «%s»: %s" % (resolved, exc)) from exc
        if not data:
            raise ValueError("Файл «%s» пуст." % resolved)
        return resolved, data

    def _make_reader(self, data: bytes, path: str, password: str | None) -> PdfReader:
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
        except PdfReadError as exc:
            raise RuntimeError("Файл «%s» не является корректным PDF: %s" % (path, exc)) from exc
        except Exception as exc:  # pragma: no cover - экзотические сбои парсера
            raise RuntimeError("Не удалось разобрать PDF «%s»: %s" % (path, exc)) from exc

        if reader.is_encrypted:
            try:
                ok = reader.decrypt(password or "")
            except DependencyError as exc:
                raise RuntimeError(
                    "PDF «%s» зашифрован алгоритмом AES, который недоступен в этой сборке. "
                    "Снимите защиту в другой программе." % path
                ) from exc
            except Exception as exc:
                raise RuntimeError("Не удалось снять защиту с «%s»: %s" % (path, exc)) from exc
            if not ok:
                raise ValueError(
                    "PDF «%s» защищён паролем. Укажите верный пароль." % path
                )
        return reader

    def open(self, path: str, password: str | None = None) -> None:
        """Открыть PDF-файл (заменяет текущий документ)."""
        self.close()
        resolved, data = self._read_source(path)
        self._open_data(data, title=resolved, password=password, source_path=resolved)

    def open_bytes(self, data: bytes | bytearray | memoryview,
                   title: str = "Документ.pdf", password: str | None = None) -> None:
        """Open a PDF byte stream without creating a temporary source file."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("PDF data must be bytes-like.")
        raw = bytes(data)
        if not raw:
            raise ValueError("PDF data is empty.")
        if not isinstance(title, str) or not title.strip():
            title = "Документ.pdf"
        self.close()
        self._open_data(raw, title=title, password=password, source_path=None)

    def _open_data(self, data: bytes, *, title: str, password: str | None,
                   source_path: str | None) -> None:
        protection = _pdf_security.inspect_pdf_protection(data)
        reader = self._make_reader(data, title, password)
        if protection["encrypted"]:
            protection = _pdf_security.inspect_pdf_protection(data, password or "")
            authenticated_as = protection["authenticated_as"]
            if authenticated_as == "owner":
                self._security_open_password = ""
                self._security_owner_password = password or ""
            else:
                self._security_open_password = password or ""
                self._security_owner_password = ""
            self._security_info = protection
            self._security_encryption_required = True
            self._security_source_data = data
            self._security_preserve_encryption = True
        else:
            self._security_info = self._plain_security_info()
            self._security_open_password = ""
            self._security_owner_password = ""
            self._security_encryption_required = False
            self._security_source_data = None
            self._security_preserve_encryption = False
        try:
            page_count = len(reader.pages)
        except Exception as exc:
            raise RuntimeError("Не удалось прочитать страницы «%s»: %s" % (title, exc)) from exc
        if page_count == 0:
            raise ValueError("В документе «%s» нет страниц." % title)

        try:
            writer = PdfWriter(clone_from=reader)
        except Exception as exc:
            raise RuntimeError("Не удалось подготовить документ к правке: %s" % exc) from exc

        source = _Source(index=0, path=source_path or title, data=data, buffer=io.BytesIO(data),
                         reader=reader, prefix="")
        self._writer = writer
        source_acro = writer._root_object.get("/AcroForm")
        source_acro = source_acro.get_object() if hasattr(source_acro, "get_object") else source_acro
        self._initial_need_appearances = (
            isinstance(source_acro, DictionaryObject)
            and _as_bool(source_acro.get("/NeedAppearances")))
        self._refreshed_form_appearances = set()
        self._sources = [source]
        self._source_paths = [source_path] if source_path else []
        self._page_sources = [0] * page_count
        self._view_page_ids = self._new_view_page_ids(page_count)
        self._annots = [[] for _ in range(page_count)]
        self._keepalive = [source.buffer, reader]
        self._revision = 0
        self._doc_revision = 0
        self._saved_revision = 0
        self._saved_path = None
        self._has_xfa = False
        self._check_document_features(title)
        # Аннотации поддерживаемых типов переносим в модель: их рисует холст, а
        # не PDFium, поэтому они не удваиваются и остаются редактируемыми.
        self._layer_cache = {}
        self._layer_restore = []
        for page_index in range(page_count):
            self._extract_annotations(page_index)

    def _check_document_features(self, path: str) -> None:
        """Проверить XFA/подписи и честно предупредить о неподдерживаемом."""
        assert self._writer is not None
        root = self._writer._root_object
        acro = root.get("/AcroForm")
        if acro is None:
            return
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if isinstance(acro, DictionaryObject) and acro.get("/XFA") is not None:
            self._has_xfa = True
            self._warn(
                "Файл «%s» содержит XFA-форму. Редактирование таких форм не поддерживается: "
                "поля показаны только для просмотра." % os.path.basename(path)
            )
        fields = self._acro_fields(acro) if isinstance(acro, DictionaryObject) else []
        for field in fields:
            self._check_signature(field, set())

    def _check_signature(self, field: Any, seen: set[int]) -> None:
        if not isinstance(field, DictionaryObject):
            return
        if id(field) in seen:
            return
        seen.add(id(field))
        if str(field.get("/FT", "")) == "/Sig":
            self._warn("Документ содержит поля цифровой подписи — они не поддерживаются.")
            return
        for kid in field.get("/Kids") or []:
            obj = kid.get_object() if hasattr(kid, "get_object") else kid
            self._check_signature(obj, seen)

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
            _LOG.warning(message)

    # -- добавление файлов -------------------------------------------------
    def new(self, width: float = 595.28, height: float = 841.89, pages: int = 1) -> None:
        """Создать новый пустой документ (по умолчанию A4, книжная ориентация)."""
        try:
            page_width = float(width)
            page_height = float(height)
            page_count = int(pages)
        except (TypeError, ValueError):
            raise ValueError("Размер страницы и число страниц должны быть числами.") from None
        for label, value in (("ширина", page_width), ("высота", page_height)):
            if not math.isfinite(value) or not (_MIN_PAGE_SIZE <= value <= _MAX_PAGE_SIZE):
                raise ValueError("Недопустимая %s страницы: %s пт (допустимо %g…%g)."
                                 % (label, value, _MIN_PAGE_SIZE, _MAX_PAGE_SIZE))
        if not (1 <= page_count <= _MAX_NEW_PAGES):
            raise ValueError("Число страниц должно быть от 1 до %d." % _MAX_NEW_PAGES)
        self.close()
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=page_width, height=page_height)
        self._writer = writer
        self._initial_need_appearances = False
        self._refreshed_form_appearances = set()
        self._annots = [[] for _ in range(page_count)]
        self._page_sources = [0] * page_count
        self._view_page_ids = self._new_view_page_ids(page_count)
        self._sources = []
        self._source_paths = []

    def insert_blank_page(self, index: int, width: float | None = None,
                          height: float | None = None) -> int:
        """Вставить пустую страницу; по умолчанию — размер соседней страницы.

        ``index`` может быть равен числу страниц (вставка в конец). Возвращает
        позицию вставленной страницы.
        """
        writer = self._require_open()
        try:
            position = int(index)
        except (TypeError, ValueError):
            raise ValueError("Индекс страницы должен быть целым числом.") from None
        count = len(writer.pages)
        if not (0 <= position <= count):
            raise IndexError("Нельзя вставить страницу на позицию %d: всего страниц %d."
                             % (position, count))
        default_w, default_h = 595.28, 841.89
        if count:
            default_w, default_h = self.page_size(min(position, count - 1))
        try:
            page_width = float(width) if width is not None else float(default_w)
            page_height = float(height) if height is not None else float(default_h)
        except (TypeError, ValueError):
            raise ValueError("Размер вставляемой страницы должен быть числом.") from None
        for label, value in (("ширина", page_width), ("высота", page_height)):
            if not math.isfinite(value) or not (_MIN_PAGE_SIZE <= value <= _MAX_PAGE_SIZE):
                raise ValueError("Недопустимая %s страницы: %s пт (допустимо %g…%g)."
                                 % (label, value, _MIN_PAGE_SIZE, _MAX_PAGE_SIZE))
        self._push_undo()
        writer.insert_blank_page(width=page_width, height=page_height, index=position)
        self._annots.insert(position, [])
        source = 0
        if self._page_sources:
            source = self._page_sources[min(position, len(self._page_sources) - 1)]
        self._page_sources.insert(position, source)
        self._view_page_ids.insert(position, self._new_view_page_ids(1)[0])
        self._touch()
        return position

    def append(self, paths: list[str]) -> None:
        """Добавить страницы других PDF-файлов в конец документа."""
        writer = self._require_open()
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]  # type: ignore[list-item]
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ValueError("Укажите список PDF-файлов для добавления.")
        prepared: list[tuple[str, bytes]] = []
        for path in paths:
            prepared.append(self._read_source(path))
        # Сначала проверяем все файлы, потом меняем документ.
        readers: list[tuple[str, PdfReader, bytes]] = []
        seen_paths = {os.path.normcase(os.path.realpath(path)) for path in self._source_paths}
        for resolved, data in prepared:
            canonical = os.path.normcase(os.path.realpath(resolved))
            if canonical in seen_paths:
                raise ValueError("Файл «%s» уже добавлен в документ или выбран дважды." % resolved)
            seen_paths.add(canonical)
            reader = self._make_reader(data, resolved, None)
            try:
                new_count = len(reader.pages)
            except Exception as exc:
                raise RuntimeError("Не удалось прочитать страницы «%s»: %s" % (resolved, exc)) from exc
            if new_count == 0:
                raise ValueError("В документе «%s» нет страниц." % resolved)
            readers.append((resolved, reader, data))

        history = (list(self._undo), list(self._redo), self._undo_bytes,
                   self._last_edit_key, self._last_edit_time)
        sources = (list(self._sources), list(self._source_paths),
                   list(self._keepalive), list(self._warnings), self._has_xfa)
        doc_revision = self._doc_revision
        self._push_undo()
        before_merge = self._undo[-1]
        try:
            for resolved, reader, data in readers:
                source_index = len(self._sources)
                source = _Source(index=source_index, path=resolved, data=data,
                                 buffer=io.BytesIO(data), reader=reader,
                                 prefix="pdfdesk%d_" % source_index)
                before = self._field_idnums()
                before_pages = len(writer.pages)
                try:
                    writer.append(reader)
                except Exception as exc:
                    raise RuntimeError("Не удалось добавить «%s»: %s" % (resolved, exc)) from exc
                added = len(writer.pages) - before_pages
                if added <= 0:
                    raise RuntimeError("Из файла «%s» не удалось добавить страницы." % resolved)
                self._rename_new_fields(before, source.prefix)
                self._sources.append(source)
                self._source_paths.append(resolved)
                self._keepalive.extend([source.buffer, reader])
                self._page_sources.extend([source_index] * added)
                self._view_page_ids.extend(self._new_view_page_ids(added))
                self._annots.extend([] for _ in range(added))
                self._check_appended_features(resolved)
                for page_index in range(before_pages, before_pages + added):
                    self._extract_annotations(page_index)
        except Exception:
            # A malformed later file must not leave a partially merged document
            # or consume the user's undo/redo history.
            (self._sources, self._source_paths, self._keepalive,
             self._warnings, self._has_xfa) = sources
            self._restore(before_merge)
            self._doc_revision = doc_revision
            (self._undo, self._redo, self._undo_bytes,
             self._last_edit_key, self._last_edit_time) = history
            raise
        self._touch()

    def _check_appended_features(self, path: str) -> None:
        root = self._writer._root_object  # type: ignore[union-attr]
        acro = root.get("/AcroForm")
        if acro is None:
            return
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if isinstance(acro, DictionaryObject) and acro.get("/XFA") is not None:
            self._has_xfa = True
            self._warn("Добавленный файл «%s» содержит XFA-форму — её поля не редактируются."
                       % os.path.basename(path))

    def _field_idnums(self) -> set[int]:
        result: set[int] = set()
        for field in self._acro_fields(None):
            ref = getattr(field, "indirect_reference", None)
            if ref is not None:
                result.add(ref.idnum)
        return result

    def _rename_new_fields(self, before: set[int], prefix: str) -> None:
        """Префикс имён импортированных полей: защита от конфликтов имён."""
        if not prefix:
            return
        for field in self._acro_fields(None):
            ref = getattr(field, "indirect_reference", None)
            if ref is None or ref.idnum in before:
                continue
            name = field.get("/T")
            if name is None:
                continue
            field[NameObject("/T")] = TextStringObject(prefix + str(name))

    def _acro_fields(self, acro: Any) -> list[Any]:
        writer = self._writer
        if writer is None:
            return []
        root = writer._root_object
        if acro is None:
            acro = root.get("/AcroForm")
        if acro is None:
            return []
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if not isinstance(acro, DictionaryObject):
            return []
        fields = acro.get("/Fields")
        if fields is None:
            return []
        fields = fields.get_object() if hasattr(fields, "get_object") else fields
        if not isinstance(fields, (ArrayObject, list, tuple)):
            return []
        result: list[Any] = []
        for item in fields:
            obj = item.get_object() if hasattr(item, "get_object") else item
            if isinstance(obj, DictionaryObject):
                result.append(obj)
        return result

    # -- аннотации: извлечение из файла ------------------------------------
    def _page_annots_array(self, page: Any, create: bool = False) -> ArrayObject | None:
        raw = page.get("/Annots")
        if raw is None:
            if not create:
                return None
            array = ArrayObject()
            page[NameObject("/Annots")] = array
            return array
        obj = raw.get_object() if hasattr(raw, "get_object") else raw
        if not isinstance(obj, ArrayObject):
            if not create:
                return None
            array = ArrayObject()
            page[NameObject("/Annots")] = array
            return array
        return obj

    def _extract_annotations(self, page_index: int) -> None:
        """Перенести «наши» типы аннотаций из PDF в модель (чтобы холст рисовал их сам)."""
        page = self._page(page_index)
        # Remove our previous searchable overlay; saving rebuilds it from
        # current annotations, so deleted/edited comments leave no stale text.
        raw_contents = page.get('/Contents')
        if raw_contents is not None:
            contents = raw_contents.get_object()
            entries = list(contents) if isinstance(contents, ArrayObject) else [raw_contents]
            kept = []
            for item in entries:
                stream = item.get_object()
                if not isinstance(stream, DictionaryObject) or not stream.get('/PdfDeskTextLayer', False):
                    kept.append(item)
            if len(kept) != len(entries):
                page[NameObject('/Contents')] = ArrayObject(kept)
        array = self._page_annots_array(page, create=False)
        if array is None:
            return
        geom = self._geometry(page_index)
        items = list(array)
        records: list[tuple[Any, _AnnRecord]] = []
        owned: set[int] = set()
        skipped: set[int] = set()
        for item in items:
            obj = item.get_object() if hasattr(item, "get_object") else item
            record = None
            if isinstance(obj, DictionaryObject):
                try:
                    record = self._record_from_pdf(obj, item, geom)
                except Exception as exc:  # повреждённая аннотация — не падаем
                    _LOG.warning("Не удалось разобрать аннотацию: %s", exc)
                    record = None
            if record is None:
                continue
            records.append((item, record))
            owned.add(id(item))
        # Replies are editable only when their /IRT target can be rebound by a
        # unique /NM after serialization. Leave replies with missing/ambiguous
        # targets in the original page array rather than carrying stale refs.
        names: dict[str, list[Any]] = {}
        for item in items:
            obj = item.get_object() if hasattr(item, "get_object") else item
            if not isinstance(obj, DictionaryObject):
                continue
            name = str(obj.get("/NM", "") or "")
            if name:
                names.setdefault(name, []).append(item)
        safe_records: list[tuple[Any, _AnnRecord]] = []
        for item, record in records:
            target_name = record.metadata.get("irt_nm")
            if target_name and len(names.get(target_name, ())) != 1:
                owned.discard(id(item))
                continue
            safe_records.append((item, record))
        records = safe_records
        skipped = {id(record.popup_dict) for _, record in records
                   if record.popup_dict is not None}
        keep: list[Any] = []
        note_ids = {id(record.pdf_dict) for _, record in records
                    if record.popup_dict is not None}
        for item in items:
            if id(item) in owned:
                continue
            obj = item.get_object() if hasattr(item, "get_object") else item
            if isinstance(obj, DictionaryObject) and id(obj) in skipped:
                continue
            if isinstance(obj, DictionaryObject) and str(obj.get("/Subtype", "")) == "/Popup":
                parent = obj.get("/Parent")
                parent = parent.get_object() if hasattr(parent, "get_object") else parent
                if isinstance(parent, DictionaryObject) and id(parent) in note_ids:
                    continue
            keep.append(item)
        self._annots[page_index].extend(record for _, record in records)
        if len(keep) != len(array):
            array[:] = keep
            if not array and page.get("/Annots") is not None:
                # пустой массив оставляем: структура документа не меняется
                pass

    def _record_from_pdf(self, obj: DictionaryObject, item: Any,
                         geom: _PageGeom) -> _AnnRecord | None:
        subtype = str(obj.get("/Subtype", ""))
        opacity = _as_float(obj.get("/CA"), 1.0) or 1.0
        ref = item if isinstance(item, IndirectObject) else None
        ap = None
        ap_ref = None
        ap_raw = obj.get("/AP")
        if ap_raw is not None:
            ap_dict = ap_raw.get_object() if hasattr(ap_raw, "get_object") else ap_raw
            if isinstance(ap_dict, DictionaryObject):
                normal = ap_dict.get("/N")
                normal_obj = normal.get_object() if hasattr(normal, "get_object") else normal
                if isinstance(normal_obj, StreamObject):
                    ap = normal_obj
                    ap_ref = normal if isinstance(normal, IndirectObject) else None

        extras = _extras_from_pdf(obj)
        kind = str(extras.get("kind") or "") or _kind_from_subtype(obj, subtype)
        if kind not in _ANNOT_KINDS:
            return None
        metadata = _safe_annotation_metadata(obj)
        irt_raw = obj.get("/IRT")
        if irt_raw is not None:
            irt_obj = irt_raw.get_object() if hasattr(irt_raw, "get_object") else irt_raw
            if not isinstance(irt_obj, DictionaryObject):
                return None
            irt_nm = str(irt_obj.get("/NM", "") or "")
            if not irt_nm:
                return None
            metadata["irt_nm"] = irt_nm
        popup_dict: DictionaryObject | None = None
        popup_ref: IndirectObject | None = None
        popup_metadata: dict[str, Any] = {}

        def _override(data: Annotation) -> Annotation:
            """Применить extras (они старше стандартных ключей PDF)."""
            if extras.get("bold") is not None:
                data.bold = bool(extras["bold"])
            if extras.get("alignment"):
                data.alignment = str(extras["alignment"])
            if extras.get("fill_color") is not None:
                data.fill_color = str(extras["fill_color"])
            if extras.get("color") is not None:
                data.color = str(extras["color"])
            if extras.get("font_family"):
                data.font_family = str(extras["font_family"])
            if extras.get("font_size"):
                data.font_size = float(extras["font_size"])
            if extras.get("stroke_width"):
                data.stroke_width = float(extras["stroke_width"])
            if extras.get("opacity") is not None:
                data.opacity = float(extras["opacity"])
            else:
                data.opacity = opacity
            return data

        if kind == "text":
            rect = _rect_tuple(obj.get("/Rect"))
            if rect is None:
                return None
            x, y, w, h = _user_rect_to_display(geom, *rect)
            font_name, size, color = _parse_da(str(obj.get("/DA", "")))
            data = Annotation(
                kind="text", x=x, y=y, width=w, height=h,
                text=str(obj.get("/Contents", "")),
                color=color, font_family=_family_from_font_name(font_name),
                font_size=size, opacity=opacity,
                bold=_bold_from_font_name(font_name),
                alignment=_alignment_from_q(obj.get("/Q")),
            )
        elif kind == "note":
            rect = _rect_tuple(obj.get("/Rect"))
            if rect is None:
                return None
            x, y, w, h = _user_rect_to_display(geom, *rect)
            if w < 1.0 or h < 1.0:
                w = h = _NOTE_DEFAULT_SIZE
            font_name, size, color = _parse_da(str(obj.get("/DA", "")))
            data = Annotation(
                kind="note", x=x, y=y, width=w, height=h,
                text=str(obj.get("/Contents", "")),
                color=color, font_family=_family_from_font_name(font_name),
                font_size=size, opacity=opacity,
                bold=_bold_from_font_name(font_name),
                alignment=_alignment_from_q(obj.get("/Q")),
            )
            popup_raw = obj.get("/Popup")
            if popup_raw is not None:
                popup_obj = (popup_raw.get_object() if hasattr(popup_raw, "get_object")
                             else popup_raw)
                if (isinstance(popup_obj, DictionaryObject)
                        and str(popup_obj.get("/Subtype", "")) == "/Popup"):
                    popup_dict = popup_obj
                    popup_ref = popup_raw if isinstance(popup_raw, IndirectObject) else None
                    popup_metadata = _safe_annotation_metadata(popup_obj)
        elif kind == "highlight":
            quad = obj.get("/QuadPoints")
            if quad is not None:
                try:
                    values = [float(v) for v in quad]
                except (TypeError, ValueError):
                    values = []
                if len(values) >= 8:
                    pts = [_user_to_display_point(geom, values[i], values[i + 1])
                           for i in range(0, 8, 2)]
                else:
                    pts = []
            else:
                pts = []
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                x, y, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            else:
                rect = _rect_tuple(obj.get("/Rect"))
                if rect is None:
                    return None
                x, y, w, h = _user_rect_to_display(geom, *rect)
            data = Annotation(
                kind="highlight", x=x, y=y, width=w, height=h,
                color=_pdf_color_to_hex(obj.get("/C"), "#FFEB3B"), opacity=opacity,
            )
        elif kind == "ink":
            strokes = _ink_strokes_from_pdf(obj, geom)
            if strokes is None:
                # Чужие и битые списки штрихов остаются в PDF как есть: без
                # потерь их не переносим в модель редактора.
                return None
            points, stroke_lengths = _pack_ink_strokes(strokes)
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            x, y, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            data = Annotation(
                kind="ink", x=x, y=y, width=w, height=h, points=points,
                stroke_lengths=stroke_lengths,
                color=_pdf_color_to_hex(obj.get("/C"), "#111111"),
                stroke_width=_stroke_width_from_annot(obj), opacity=opacity,
            )
        elif kind in _POLYGON_KINDS:
            vertices: list[tuple[float, float]] = []
            raw_vertices = obj.get("/Vertices")
            if isinstance(raw_vertices, (ArrayObject, list, tuple)):
                try:
                    values = [float(v) for v in raw_vertices]
                except (TypeError, ValueError):
                    values = []
                vertices = [_user_to_display_point(geom, values[i], values[i + 1])
                            for i in range(0, len(values) - 1, 2)]
            vertices = _normalize_polygon(vertices)
            if len(vertices) < 3:
                # Облачный /Square и битый /Polygon: берём прямоугольник /Rect.
                rect = _rect_tuple(obj.get("/Rect"))
                if rect is None:
                    return None
                x, y, w, h = _user_rect_to_display(geom, *rect)
                if w < _MIN_POLYGON_SIZE or h < _MIN_POLYGON_SIZE:
                    return None
                vertices = _rectangle_points(x, y, w, h)
            xs = [point[0] for point in vertices]
            ys = [point[1] for point in vertices]
            fill_color = ""
            if obj.get("/IC") is not None:
                fill_color = _pdf_color_to_hex(obj.get("/IC"), "")
            data = Annotation(
                kind=kind, x=min(xs), y=min(ys),
                width=max(xs) - min(xs), height=max(ys) - min(ys), points=vertices,
                color=(_pdf_color_to_hex(obj.get("/C"), "#2457D6")
                       if obj.get("/C") is not None else "#2457D6"),
                fill_color=fill_color, stroke_width=_stroke_width_from_annot(obj),
                opacity=opacity,
            )
        elif kind in _SHAPE_KINDS:
            rect = _rect_tuple(obj.get("/Rect"))
            if rect is None:
                return None
            x, y, w, h = _user_rect_to_display(geom, *rect)
            fill_color = ""
            if obj.get("/IC") is not None:
                fill_color = _pdf_color_to_hex(obj.get("/IC"), "")
            data = Annotation(
                kind=kind, x=x, y=y, width=w, height=h,
                color=(_pdf_color_to_hex(obj.get("/C"), "#2457D6")
                       if obj.get("/C") is not None else "#2457D6"),
                fill_color=fill_color, stroke_width=_stroke_width_from_annot(obj),
                opacity=opacity,
            )
        elif kind in _LINE_KINDS:
            endpoints: list[tuple[float, float]] = []
            raw_line = obj.get("/L")
            if isinstance(raw_line, (ArrayObject, list, tuple)):
                try:
                    values = [float(v) for v in raw_line]
                except (TypeError, ValueError):
                    values = []
                if len(values) >= 4:
                    endpoints = [_user_to_display_point(geom, values[0], values[1]),
                                 _user_to_display_point(geom, values[2], values[3])]
            if endpoints:
                xs = [p[0] for p in endpoints]
                ys = [p[1] for p in endpoints]
                x, y, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            else:
                rect = _rect_tuple(obj.get("/Rect"))
                if rect is None:
                    return None
                x, y, w, h = _user_rect_to_display(geom, *rect)
                endpoints = [(x, y), (x + w, y + h)]
            data = Annotation(
                kind=kind, x=x, y=y, width=w, height=h, points=endpoints,
                color=(_pdf_color_to_hex(obj.get("/C"), "#111111")
                       if obj.get("/C") is not None else "#111111"),
                stroke_width=_stroke_width_from_annot(obj), opacity=opacity,
            )
        elif kind in _IMAGE_KINDS:
            rect = _rect_tuple(obj.get("/Rect"))
            if rect is None:
                return None
            x, y, w, h = _user_rect_to_display(geom, *rect)
            image_data = _image_data_from_xobject(ap) if ap is not None else ""
            data = Annotation(
                kind=kind, x=x, y=y, width=w, height=h, image_data=image_data,
                color=(_pdf_color_to_hex(obj.get("/C"), "#2457D6")
                       if obj.get("/C") is not None else "#2457D6"),
                stroke_width=_stroke_width_from_annot(obj), opacity=opacity,
            )
        else:  # pragma: no cover - отсекается выше
            return None
        record = _AnnRecord(data=_override(data), pdf_dict=obj, pdf_ref=ref, ap_stream=ap,
                            ap_ref=ap_ref, popup_dict=popup_dict, popup_ref=popup_ref,
                            metadata=metadata, popup_metadata=popup_metadata, dirty=True)
        return record

    # -- базовые свойства --------------------------------------------------
    @property
    def page_count(self) -> int:
        """Количество страниц (0, если документ не открыт)."""
        if self._writer is None:
            return 0
        return len(self._writer.pages)

    @property
    def dirty(self) -> bool:
        """Есть ли несохранённые изменения."""
        return self._revision != self._saved_revision

    # -- геометрия ---------------------------------------------------------
    def _geometry(self, index: int) -> _PageGeom:
        page = self._page(index)
        try:
            media = page.mediabox
        except Exception:  # pragma: no cover - битый /MediaBox
            media = None
        try:
            crop = page.cropbox
        except Exception:  # pragma: no cover - битый /CropBox
            crop = None
        if crop is None and media is None:
            rect = (0.0, 0.0, 612.0, 792.0)
        else:
            box = crop if crop is not None else media
            left = float(box[0])
            bottom = float(box[1])
            right = float(box[2])
            top = float(box[3])
            if media is not None and crop is not None:
                left = max(left, float(media[0]))
                bottom = max(bottom, float(media[1]))
                right = min(right, float(media[2]))
                top = min(top, float(media[3]))
            if right - left < 1.0 or top - bottom < 1.0:
                box = media if media is not None else (0.0, 0.0, 612.0, 792.0)
                left, bottom, right, top = (float(box[0]), float(box[1]),
                                            float(box[2]), float(box[3]))
            if right - left < 1.0 or top - bottom < 1.0:
                left, bottom, right, top = 0.0, 0.0, 612.0, 792.0
            rect = (left, bottom, right, top)
        rotation = _norm_rotation(page.get_inherited("/Rotate", 0) or 0)
        return _PageGeom(left=rect[0], bottom=rect[1],
                         width=rect[2] - rect[0], height=rect[3] - rect[1],
                         rotation=rotation)

    def page_size(self, index: int) -> tuple[float, float]:
        """Размер отображаемой страницы в пунктах (с учётом /CropBox и /Rotate)."""
        geom = self._geometry(self._page_index(index))
        return (geom.display_width, geom.display_height)

    # -- рендер ------------------------------------------------------------
    def _close_pdfium(self) -> None:
        document = self._pdfium
        self._pdfium = None
        self._pdfium_revision = -1
        if document is None:
            return
        for closer in ("close_forms", "close"):
            try:
                getattr(document, closer)()
            except Exception:  # pragma: no cover - закрытие не должно ломать работу
                pass

    def _write_bytes(self) -> bytes:
        writer = self._require_open()
        buffer = io.BytesIO()
        try:
            writer.write(buffer)
        except Exception as exc:
            raise RuntimeError("Не удалось собрать PDF: %s" % exc) from exc
        return buffer.getvalue()

    def _serialize(self) -> bytes:
        if self._serialized is not None and self._serialized_revision == self._doc_revision:
            return self._serialized
        data = self._write_bytes()
        self._serialized = data
        self._serialized_revision = self._doc_revision
        return data

    def _serialize_for_render(self) -> bytes:
        """Build an unencrypted render-only PDF in memory, including overlays."""
        from .recovery import preserved_session_state
        with preserved_session_state(self):
            try:
                self._attach_annotations()
                self._attach_text_layer()
                return self._write_bytes()
            finally:
                try:
                    self._detach_text_layer()
                finally:
                    self._detach_annotations()

    def _pdfium_document(self) -> "_pdfium.PdfDocument":
        self._require_open()
        if self._pdfium is not None and self._pdfium_revision == self._doc_revision:
            return self._pdfium
        self._close_pdfium()
        data = self._serialize()
        try:
            document = _pdfium.PdfDocument(data)
        except Exception as exc:
            raise RuntimeError("Не удалось подготовить документ к отображению: %s" % exc) from exc
        try:
            # Формы нужно инициализировать до первого обращения к страницам.
            document.init_forms()
        except Exception as exc:  # pragma: no cover - зависит от конкретного файла
            _LOG.warning("Инициализация форм PDFium не удалась: %s", exc)
        self._pdfium = document
        self._pdfium_revision = self._doc_revision
        return document

    def render(self, index: int, scale: float = 1.0, *, include_annotations: bool = False,
               include_forms: bool = True) -> Image.Image:
        """Отрисовать страницу в PIL-изображение.

        Формы отображаются заполненными по умолчанию. ``include_forms=False``
        предназначен для интерактивного редактора, который рисует свои Qt-
        редакторы поверх страницы. Поворот страницы учитывается.
        Пользовательские аннотации (текст/выделение/чернила) в изображение НЕ
        попадают — их рисует холст приложения.
        """
        page_index = self._page_index(index)
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            raise ValueError("Масштаб должен быть числом.") from None
        if not (0.0 < scale <= _MAX_RENDER_SCALE):
            raise ValueError("Масштаб должен быть больше 0 и не больше %g." % _MAX_RENDER_SCALE)
        geom = self._geometry(page_index)
        pixels = (geom.display_width * scale) * (geom.display_height * scale)
        if pixels > _MAX_RENDER_PIXELS:
            raise ValueError(
                "Слишком большой масштаб: изображение займёт %.1f Мп (лимит %d Мп)."
                % (pixels / 1e6, _MAX_RENDER_PIXELS // 1_000_000)
            )
        if include_annotations:
            # PDFium has no password here. Use an unencrypted, in-memory render
            # stream; persistent saves and recovery snapshots stay protected.
            document = _pdfium.PdfDocument(self._serialize_for_render())
            document.init_forms()
        else:
            document = self._pdfium_document()
        try:
            page = document[page_index]
        except Exception as exc:
            raise RuntimeError("Не удалось открыть страницу %d: %s" % (page_index, exc)) from exc
        bitmap = None
        try:
            bitmap = page.render(scale=scale, may_draw_forms=bool(include_forms), draw_annots=True,
                                 fill_color=(255, 255, 255, 255))
            image = bitmap.to_pil()
            detached = image.convert("RGB")
        except Exception as exc:
            raise RuntimeError("Не удалось отрисовать страницу %d: %s" % (page_index, exc)) from exc
        finally:
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception:  # pragma: no cover
                    pass
            try:
                page.close()
            except Exception:  # pragma: no cover
                pass
            if include_annotations:
                document.close()
        return detached

    def page_text(self, index: int) -> str:
        """Original searchable text layer; scanned pages return empty text."""
        page=self._pdfium_document()[self._page_index(index)]
        textpage=None
        try:
            textpage=page.get_textpage()
            return textpage.get_text_range()
        finally:
            if textpage is not None:
                textpage.close()
            page.close()

    # -- аннотации (внешний API) -------------------------------------------
    def get_annotations(self, index: int) -> list[Annotation]:
        """Список аннотаций страницы (копии: правьте через update_annotation)."""
        page_index = self._page_index(index)
        return [copy.deepcopy(record.data) for record in self._annots[page_index]]

    def add_annotation(self, index: int, annotation: Annotation) -> int:
        """Добавить аннотацию; возвращает её индекс на странице."""
        page_index = self._page_index(index)
        prepared = self._validate_annotation(annotation)
        self._push_undo()
        self._annots[page_index].append(_AnnRecord(data=prepared, dirty=True))
        self._touch(doc=False)
        return len(self._annots[page_index]) - 1

    def add_annotations(self, index: int, annotations: Sequence[Annotation]) -> list[int]:
        """Атомарно добавить несколько аннотаций; вернуть их индексы по порядку.

        Сначала проверяются **все** аннотации, и только потом берётся одна
        ступень отмены: многострочная подсветка текста отменяется одним
        действием, а неудачная проверка не оставляет мусора в истории и в
        модели. Права PDF проверяются в ``_push_undo`` до любой мутации.
        """
        page_index = self._page_index(index)
        items = list(annotations or ())
        if not items:
            return []
        prepared = [self._validate_annotation(item) for item in items]
        self._push_undo()
        records = self._annots[page_index]
        positions: list[int] = []
        for item in prepared:
            records.append(_AnnRecord(data=item, dirty=True))
            positions.append(len(records) - 1)
        self._touch(doc=False)
        return positions

    def update_annotation(self, index: int, annotation_index: int, annotation: Annotation) -> None:
        """Заменить аннотацию по индексу."""
        page_index = self._page_index(index)
        position = self._annotation_index(page_index, annotation_index)
        prepared = self._validate_annotation(annotation)
        self._push_undo()
        previous = self._annots[page_index][position]
        self._annots[page_index][position] = _AnnRecord(
            data=prepared, metadata=copy.deepcopy(previous.metadata),
            popup_metadata=copy.deepcopy(previous.popup_metadata),
            modified=True, dirty=True)
        self._touch(doc=False)

    def remove_annotation(self, index: int, annotation_index: int) -> None:
        """Удалить аннотацию по индексу."""
        page_index = self._page_index(index)
        position = self._annotation_index(page_index, annotation_index)
        self._push_undo()
        self._annots[page_index].pop(position)
        self._touch(doc=False)

    def _annotation_index(self, page_index: int, annotation_index: Any) -> int:
        try:
            position = int(annotation_index)
        except (TypeError, ValueError):
            raise ValueError("Индекс аннотации должен быть целым числом.") from None
        count = len(self._annots[page_index])
        if not (0 <= position < count):
            raise IndexError("Аннотация %d не найдена: на странице %d аннотаций."
                             % (position, count))
        return position

    def _validate_annotation(self, annotation: Annotation) -> Annotation:
        if not isinstance(annotation, Annotation):
            raise ValueError("Ожидается объект Annotation.")
        prepared = copy.deepcopy(annotation)
        kind = str(prepared.kind or "").strip().lower()
        if kind not in _ANNOT_KINDS:
            raise ValueError("Неизвестный тип аннотации «%s» (допустимо: %s)."
                             % (prepared.kind, ", ".join(_ANNOT_KINDS)))
        prepared.kind = kind
        for name in ("x", "y", "width", "height", "font_size", "stroke_width", "opacity"):
            try:
                value = float(getattr(prepared, name))
            except (TypeError, ValueError):
                raise ValueError("Поле «%s» аннотации должно быть числом." % name) from None
            if not math.isfinite(value):
                raise ValueError("Поле «%s» аннотации должно быть конечным числом." % name)
            setattr(prepared, name, value)
        prepared.width = abs(prepared.width)
        prepared.height = abs(prepared.height)
        prepared.opacity = min(1.0, max(0.0, prepared.opacity))
        prepared.font_size = max(1.0, min(_MAX_FONT_SIZE, prepared.font_size))
        prepared.stroke_width = max(0.1, min(_MAX_STROKE_WIDTH, prepared.stroke_width))
        prepared.text = str(prepared.text or "")
        if len(prepared.text) > _MAX_TEXT_LENGTH:
            raise ValueError("Текст аннотации слишком длинный: %d символов (лимит %d)."
                             % (len(prepared.text), _MAX_TEXT_LENGTH))
        alignment = str(prepared.alignment or "left").strip().lower()
        prepared.alignment = alignment if alignment in _ALIGNMENT_CODES else "left"
        prepared.bold = bool(prepared.bold)
        fill = str(prepared.fill_color or "").strip()
        if fill.lower() in ("none", "transparent"):
            fill = ""
        if fill and _parse_color_optional(fill) is None:
            raise ValueError("Недопустимый цвет заливки «%s»: ожидается #RRGGBB."
                             % prepared.fill_color)
        prepared.fill_color = fill
        if prepared.kind == "text" and not prepared.text:
            raise ValueError("Текстовая аннотация не может быть пустой.")
        if prepared.kind == "ink":
            try:
                flat, stroke_lengths = _normalize_ink_geometry(
                    prepared.points, prepared.stroke_lengths)
            except ValueError:
                raise ValueError(
                    "Чернильная аннотация: точки должны быть парами чисел (x, y), "
                    "границы штрихов — совпадать с их числом.") from None
            if not flat:
                raise ValueError("Чернильная аннотация должна содержать точки (points).")
            prepared.points = flat
            prepared.stroke_lengths = stroke_lengths
            xs = [point[0] for point in flat]
            ys = [point[1] for point in flat]
            prepared.x = min(xs)
            prepared.y = min(ys)
            prepared.width = max(xs) - min(xs)
            prepared.height = max(ys) - min(ys)
        elif prepared.kind in _LINE_KINDS:
            flat = _point_pairs(prepared.points, "линии")
            if len(flat) < 2:
                raise ValueError("Линия/стрелка требуют двух точек (points).")
            endpoints = [flat[0], flat[-1]]
            if (abs(endpoints[0][0] - endpoints[1][0]) < 0.01
                    and abs(endpoints[0][1] - endpoints[1][1]) < 0.01):
                raise ValueError("Линия должна иметь ненулевую длину.")
            prepared.points = endpoints
            xs = [point[0] for point in endpoints]
            ys = [point[1] for point in endpoints]
            prepared.x = min(xs)
            prepared.y = min(ys)
            prepared.width = max(xs) - min(xs)
            prepared.height = max(ys) - min(ys)
        elif prepared.kind in _POLYGON_KINDS:
            flat = _point_pairs(prepared.points, "многоугольника")
            if not flat:
                # Совместимость: смена типа фигуры (rect/ellipse/line → polygon
                # или cloud) приходит без вершин — берём прямоугольник объекта.
                flat = _rectangle_points(prepared.x, prepared.y,
                                         max(prepared.width, _MIN_POLYGON_SIZE),
                                         max(prepared.height, _MIN_POLYGON_SIZE))
            flat = _normalize_polygon(flat)
            if len(flat) < 3:
                raise ValueError(
                    "Многоугольник и облако требуют не меньше трёх вершин (points).")
            xs = [point[0] for point in flat]
            ys = [point[1] for point in flat]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            if max(width, height) < _MIN_POLYGON_SIZE:
                raise ValueError("Многоугольник должен иметь ненулевой размер.")
            prepared.points = flat
            prepared.x = min(xs)
            prepared.y = min(ys)
            prepared.width = width
            prepared.height = height
        elif prepared.kind in _SHAPE_KINDS:
            if prepared.width < 1.0 or prepared.height < 1.0:
                raise ValueError("Прямоугольник/эллипс должны иметь ненулевой размер.")
        elif prepared.kind == "note":
            if prepared.width < 1.0 or prepared.height < 1.0:
                prepared.width = _NOTE_DEFAULT_SIZE
                prepared.height = _NOTE_DEFAULT_SIZE
        elif prepared.kind in _IMAGE_KINDS:
            image = _load_image_cached(prepared.image_data)
            natural_w, natural_h = image.size
            if prepared.width < 1.0 and prepared.height < 1.0:
                scale = min(1.0, 400.0 / max(natural_w, natural_h))
                prepared.width = max(1.0, natural_w * scale)
                prepared.height = max(1.0, natural_h * scale)
            elif prepared.width < 1.0:
                prepared.width = max(1.0, prepared.height * natural_w / natural_h)
            elif prepared.height < 1.0:
                prepared.height = max(1.0, prepared.width * natural_h / natural_w)
            if max(prepared.width, prepared.height) > _MAX_IMAGE_PLACEMENT:
                raise ValueError("Слишком большой размер изображения на странице: "
                                 "%.0f×%.0f пт (лимит %.0f)."
                                 % (prepared.width, prepared.height, _MAX_IMAGE_PLACEMENT))
            prepared.image_data = str(prepared.image_data)
        prepared.color = str(prepared.color or "#2457D6")
        prepared.font_family = str(prepared.font_family or "Arial")
        return prepared

    # -- шрифты для аннотаций ----------------------------------------------
    def _all_text_chars(self) -> str:
        chars: set[str] = set()
        for page in self._annots:
            for record in page:
                if record.data.kind in ("text", "note"):
                    chars.update(str(record.data.text or ""))
        fields = self._field_map or {}
        for ref in fields.values():
            if ref.kind in ("text", "choice"):
                try:
                    chars.update(str(ref.value()))
                except Exception:  # pragma: no cover
                    continue
        return "".join(sorted(chars))

    def _font_for_family(self, family: str) -> tuple[_EmbeddedFont, IndirectObject]:
        writer = self._require_open()
        font = get_embedded_font(family or "Arial", extra_chars=self._all_text_chars())
        name = font.resource_name
        existing = self._font_refs.get(name)
        if existing is not None and existing[0] is font:
            return existing[0], existing[1]
        if existing is not None:  # имя занято другим шрифтом — разводим по суффиксу
            suffix = 2
            while ("%s%d" % (name, suffix)) in self._font_refs:
                suffix += 1
            font = _EmbeddedFont(font.info, "%s%d" % (name, suffix), font.label,
                                 extra_chars=self._all_text_chars())
            name = font.resource_name
        holder = DictionaryObject()
        try:
            ref = font.font._add_to_writer(writer, holder, NameObject("/" + name))
        except Exception as exc:
            raise RuntimeError("Не удалось встроить шрифт «%s»: %s" % (font.label, exc)) from exc
        self._font_holders.append(holder)
        self._font_refs[name] = (font, ref)
        return font, ref

    # -- сборка PDF-объектов аннотаций -------------------------------------
    def _refresh_annotation(self, record: _AnnRecord, geom: _PageGeom) -> None:
        writer = self._require_open()
        data = record.data
        dictionary = record.pdf_dict
        previous_modified = dictionary.get("/M") if dictionary is not None else None
        if dictionary is None:
            dictionary = DictionaryObject()
            record.pdf_dict = dictionary
            record.pdf_ref = writer._add_object(dictionary)
        else:
            dictionary.clear()
        stream = record.ap_stream
        if stream is None:
            stream = StreamObject()
            record.ap_stream = stream
            record.ap_ref = writer._add_object(stream)

        dictionary[NameObject("/Type")] = NameObject("/Annot")
        dictionary[NameObject("/F")] = NumberObject(4)  # Print
        dictionary[NameObject("/M")] = (
            previous_modified if previous_modified is not None
            else TextStringObject(_pdf_date())
        )

        if data.kind == "text":
            self._build_text_annotation(record, dictionary, stream, geom)
        elif data.kind == "note":
            self._build_note_annotation(record, dictionary, stream, geom)
        elif data.kind == "highlight":
            self._build_highlight_annotation(record, dictionary, stream, geom)
        elif data.kind == "ink":
            self._build_ink_annotation(record, dictionary, stream, geom)
        elif data.kind in _SHAPE_KINDS:
            self._build_shape_annotation(record, dictionary, stream, geom)
        elif data.kind in _POLYGON_KINDS:
            self._build_polygon_annotation(record, dictionary, stream, geom)
        elif data.kind in _LINE_KINDS:
            self._build_line_annotation(record, dictionary, stream, geom)
        elif data.kind in _IMAGE_KINDS:
            self._build_image_annotation(record, dictionary, stream, geom)
        else:  # pragma: no cover - отсекается при добавлении
            raise ValueError("Неизвестный тип аннотации «%s»." % data.kind)
        self._restore_annotation_metadata(record, dictionary)
        dictionary[NameObject("/PdfDesk")] = _annotation_extras(data)
        dictionary[NameObject("/AP")] = DictionaryObject({
            NameObject("/N"): record.ap_ref,
        })
        if record.popup_dict is not None:
            self._restore_popup_metadata(record.popup_dict, record.popup_metadata)
        record.dirty = False

    @staticmethod
    def _restore_annotation_metadata(record: _AnnRecord,
                                     dictionary: DictionaryObject) -> None:
        """Restore safe author/comment keys without replacing edited content."""
        for key, value in record.metadata.items():
            if key == "irt_nm":
                continue
            if key == "/Contents" and record.data.kind in ("text", "note"):
                continue  # builders emitted the current editor text
            if key == "/M" and record.modified:
                continue  # _refresh_annotation wrote the new modification date
            if key not in _ANNOTATION_METADATA_PROTECTED_KEYS:
                dictionary[NameObject(key)] = copy.deepcopy(value)

    @staticmethod
    def _restore_popup_metadata(popup: DictionaryObject,
                                metadata: dict[str, Any]) -> None:
        for key, value in metadata.items():
            if key not in _ANNOTATION_METADATA_PROTECTED_KEYS:
                popup[NameObject(key)] = copy.deepcopy(value)

    def _build_text_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                               stream: StreamObject, geom: _PageGeom) -> None:
        data = record.data
        font, font_ref = self._font_for_family(_font_family_for(data))
        size = max(1.0, float(data.font_size))
        x, y = float(data.x), float(data.y)
        width = max(1.0, float(data.width))
        height = max(1.0, float(data.height))
        content = _ap_text_content(font, str(data.text or ""), size, data.color, width, height,
                                   data.alignment)
        rx, ry, rw, rh = _display_rect_to_user(geom, x, y, width, height)
        dictionary[NameObject("/Subtype")] = NameObject("/FreeText")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/Contents")] = TextStringObject(str(data.text or ""))
        dictionary[NameObject("/DA")] = TextStringObject(
            "/%s %s Tf %s" % (font.resource_name, _fmt(size),
                              _color_operator(_parse_color(data.color))))
        dictionary[NameObject("/Q")] = NumberObject(_alignment_code(data.alignment))
        dictionary[NameObject("/BS")] = DictionaryObject({
            NameObject("/W"): NumberObject(0), NameObject("/S"): NameObject("/S")})
        font_dict = DictionaryObject({NameObject("/" + font.resource_name): font_ref})
        dictionary[NameObject("/DR")] = DictionaryObject({NameObject("/Font"): font_dict})
        if float(data.opacity) < 0.999:
            dictionary[NameObject("/CA")] = FloatObject(float(data.opacity))
        stream.set_data(content)
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/" + font.resource_name): font_ref}),
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(float(data.opacity))}),
        })

    def _build_highlight_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                                    stream: StreamObject, geom: _PageGeom) -> None:
        data = record.data
        x, y = float(data.x), float(data.y)
        width = max(1.0, float(data.width))
        height = max(1.0, float(data.height))
        opacity = min(1.0, max(0.05, float(data.opacity) or 0.35))
        rgb = _parse_color(data.color, (1.0, 0.92, 0.23))
        quad = [
            _display_to_user_point(geom, x, y),
            _display_to_user_point(geom, x + width, y),
            _display_to_user_point(geom, x, y + height),
            _display_to_user_point(geom, x + width, y + height),
        ]
        rx, ry, rw, rh = _display_rect_to_user(geom, x, y, width, height)
        dictionary[NameObject("/Subtype")] = NameObject("/Highlight")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/QuadPoints")] = ArrayObject(
            [FloatObject(value) for point in quad for value in point])
        dictionary[NameObject("/C")] = ArrayObject([FloatObject(value) for value in rgb])
        dictionary[NameObject("/CA")] = FloatObject(opacity)
        stream.set_data(_ap_highlight_content(data.color, width, height))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(opacity)}),
        })

    def _build_ink_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                              stream: StreamObject, geom: _PageGeom) -> None:
        data = record.data
        stroke_width = max(0.2, float(data.stroke_width))
        opacity = min(1.0, max(0.05, float(data.opacity)))
        # Контракт: points — ПЛОСКИЙ список пар (x, y); stroke_lengths делит его
        # на штрихи. Каждый штрих уходит отдельным массивом /InkList и отдельным
        # путём потока /AP, поэтому подъёмы пера не склеиваются линией.
        strokes = _ink_strokes(data.points, data.stroke_lengths)
        if not strokes:
            raise ValueError("Чернильная аннотация должна содержать точки (points).")
        flat = [point for stroke in strokes for point in stroke]
        xs = [px for px, _ in flat]
        ys = [py for _, py in flat]
        pad = stroke_width / 2.0 + 0.5
        min_x, min_y = min(xs), min(ys)
        bx, by = min_x - pad, min_y - pad
        bw = (max(xs) - min_x) + 2 * pad
        bh = (max(ys) - min_y) + 2 * pad
        # Точки потока — от угла данных без запаса: единственную компенсацию
        # запаса внутрь /BBox делает _ap_ink_content. Так /AP, /InkList и холст
        # показывают штрихи в одних и тех же точках страницы.
        relative = [[(px - min_x, py - min_y) for px, py in stroke] for stroke in strokes]
        rgb = _parse_color(data.color, (0.07, 0.07, 0.07))
        ink_list = ArrayObject()
        for stroke in strokes:
            ink_stroke = ArrayObject()
            for px, py in stroke:
                ux, uy = _display_to_user_point(geom, px, py)
                ink_stroke.append(FloatObject(ux))
                ink_stroke.append(FloatObject(uy))
            ink_list.append(ink_stroke)
        rx, ry, rw, rh = _display_rect_to_user(geom, bx, by, bw, bh)
        dictionary[NameObject("/Subtype")] = NameObject("/Ink")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/InkList")] = ink_list
        dictionary[NameObject("/C")] = ArrayObject([FloatObject(value) for value in rgb])
        dictionary[NameObject("/CA")] = FloatObject(opacity)
        dictionary[NameObject("/BS")] = DictionaryObject({
            NameObject("/W"): FloatObject(stroke_width), NameObject("/S"): NameObject("/S")})
        stream.set_data(_ap_ink_content(relative, data.color, stroke_width, pad, bw, bh))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(bw), FloatObject(bh)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(opacity)}),
        })

    def _build_note_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                               stream: StreamObject, geom: _PageGeom) -> None:
        """Заметка: настоящая аннотация /Text с Unicode-текстом и попапом."""
        data = record.data
        writer = self._require_open()
        font, font_ref = self._font_for_family(_font_family_for(data))
        size = max(1.0, float(data.font_size))
        x, y = float(data.x), float(data.y)
        width = max(4.0, float(data.width) or _NOTE_DEFAULT_SIZE)
        height = max(4.0, float(data.height) or _NOTE_DEFAULT_SIZE)
        rx, ry, rw, rh = _display_rect_to_user(geom, x, y, width, height)
        dictionary[NameObject("/Subtype")] = NameObject("/Text")
        dictionary[NameObject("/Name")] = NameObject("/Comment")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/Contents")] = TextStringObject(str(data.text or ""))
        dictionary[NameObject("/DA")] = TextStringObject(
            "/%s %s Tf %s" % (font.resource_name, _fmt(size),
                              _color_operator(_parse_color(data.color))))
        dictionary[NameObject("/Q")] = NumberObject(_alignment_code(data.alignment))
        dictionary[NameObject("/Open")] = BooleanObject(False)
        dictionary[NameObject("/DR")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/" + font.resource_name): font_ref}),
        })
        if float(data.opacity) < 0.999:
            dictionary[NameObject("/CA")] = FloatObject(float(data.opacity))
        # Попап — отдельный объект /Popup со ссылкой /Parent на заметку.
        if record.popup_dict is None:
            record.popup_dict = DictionaryObject()
        else:
            record.popup_dict.clear()
        if record.popup_ref is None:
            record.popup_ref = writer._add_object(record.popup_dict)
        popup = record.popup_dict
        display_w, display_h = geom.display_width, geom.display_height
        popup_w = min(220.0, max(60.0, display_w - 4.0))
        popup_h = min(90.0, max(40.0, display_h - 4.0))
        popup_x = min(max(0.0, x), max(0.0, display_w - popup_w))
        popup_y = min(max(0.0, y + height + 4.0), max(0.0, display_h - popup_h))
        prx, pry, prw, prh = _display_rect_to_user(geom, popup_x, popup_y, popup_w, popup_h)
        popup[NameObject("/Type")] = NameObject("/Annot")
        popup[NameObject("/Subtype")] = NameObject("/Popup")
        popup[NameObject("/Rect")] = ArrayObject([
            FloatObject(prx), FloatObject(pry), FloatObject(prx + prw), FloatObject(pry + prh)])
        popup[NameObject("/Open")] = BooleanObject(False)
        if record.pdf_ref is not None:
            popup[NameObject("/Parent")] = record.pdf_ref
        dictionary[NameObject("/Popup")] = record.popup_ref
        stream.set_data(_ap_note_icon_content(data.color, width, height))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(float(data.opacity))}),
        })

    def _build_shape_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                                stream: StreamObject, geom: _PageGeom) -> None:
        """Прямоугольник (/Square) и эллипс (/Circle) с заливкой и толщиной."""
        data = record.data
        stroke = max(0.2, float(data.stroke_width))
        opacity = min(1.0, max(0.0, float(data.opacity)))
        x, y = float(data.x), float(data.y)
        width = max(1.0, float(data.width))
        height = max(1.0, float(data.height))
        rx, ry, rw, rh = _display_rect_to_user(geom, x, y, width, height)
        dictionary[NameObject("/Subtype")] = NameObject(
            "/Square" if data.kind == "rect" else "/Circle")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        rgb = _parse_color(data.color)
        dictionary[NameObject("/C")] = ArrayObject([FloatObject(value) for value in rgb])
        fill_rgb = _parse_color_optional(data.fill_color)
        if fill_rgb is not None:
            dictionary[NameObject("/IC")] = ArrayObject(
                [FloatObject(value) for value in fill_rgb])
        dictionary[NameObject("/BS")] = DictionaryObject({
            NameObject("/W"): FloatObject(stroke), NameObject("/S"): NameObject("/S")})
        dictionary[NameObject("/CA")] = FloatObject(opacity)
        stream.set_data(_ap_shape_content(data.kind, data.color, data.fill_color,
                                          stroke, width, height))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(opacity)}),
        })

    def _build_polygon_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                                  stream: StreamObject, geom: _PageGeom) -> None:
        """Многоугольник (/Polygon + /Vertices) и облако (тот же /Polygon + /BE).

        Вершины пишутся в координатах PDF, поэтому объект остаётся настоящей
        стандартной аннотацией и редактируется в сторонних программах. Поток
        внешнего вида строится по той же геометрии, что и на холсте.
        """
        data = record.data
        stroke = max(0.2, float(data.stroke_width))
        opacity = min(1.0, max(0.0, float(data.opacity)))
        points = [(float(px), float(py)) for px, py in (data.points or [])]
        if len(points) < 3:
            raise ValueError("Многоугольник/облако требуют не меньше трёх вершин.")
        bx, by, bw, bh = _polygon_bbox(points, data.kind, stroke)
        bw = max(bw, 1.0)
        bh = max(bh, 1.0)
        # Вершины относительно /BBox: контур целиком лежит внутри рамки.
        relative = [(px - bx, py - by) for px, py in points]
        vertices = ArrayObject()
        for px, py in points:
            ux, uy = _display_to_user_point(geom, px, py)
            vertices.append(FloatObject(ux))
            vertices.append(FloatObject(uy))
        rx, ry, rw, rh = _display_rect_to_user(geom, bx, by, bw, bh)
        dictionary[NameObject("/Subtype")] = NameObject("/Polygon")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/Vertices")] = vertices
        rgb = _parse_color(data.color, (0.14, 0.34, 0.84))
        dictionary[NameObject("/C")] = ArrayObject([FloatObject(value) for value in rgb])
        fill_rgb = _parse_color_optional(data.fill_color)
        if fill_rgb is not None:
            dictionary[NameObject("/IC")] = ArrayObject(
                [FloatObject(value) for value in fill_rgb])
        dictionary[NameObject("/BS")] = DictionaryObject({
            NameObject("/W"): FloatObject(stroke), NameObject("/S"): NameObject("/S")})
        dictionary[NameObject("/CA")] = FloatObject(opacity)
        if data.kind == "cloud":
            # Стандартный облачный контур: /BorderEffect со стилем Cloudy.
            dictionary[NameObject("/BE")] = DictionaryObject({
                NameObject("/S"): NameObject("/C"), NameObject("/I"): NumberObject(1)})
        stream.set_data(_ap_polygon_content(data.kind, relative, data.color, data.fill_color,
                                            stroke, bw, bh))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(bw), FloatObject(bh)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(opacity)}),
        })

    def _build_line_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                               stream: StreamObject, geom: _PageGeom) -> None:
        """Линия (/Line) и стрелка (/Line + /LE наконечник)."""
        data = record.data
        points = [(float(px), float(py)) for px, py in (data.points or [])]
        if len(points) < 2:
            raise ValueError("Линия/стрелка требуют двух точек (points).")
        stroke = max(0.2, float(data.stroke_width))
        opacity = min(1.0, max(0.0, float(data.opacity)))
        arrow = data.kind == "arrow"
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        pad = stroke / 2.0 + 0.5
        if arrow:
            # /BBox потока внешнего вида обрезает содержимое — наконечник
            # стрелки должен целиком попадать внутрь прямоугольника.
            pad = max(pad, _arrow_head_size(stroke) + 0.5)
        bx, by = min(xs) - pad, min(ys) - pad
        bw = (max(xs) - min(xs)) + 2 * pad
        bh = (max(ys) - min(ys)) + 2 * pad
        relative = [(px - bx, py - by) for px, py in points]
        start = _display_to_user_point(geom, points[0][0], points[0][1])
        end = _display_to_user_point(geom, points[-1][0], points[-1][1])
        rx, ry, rw, rh = _display_rect_to_user(geom, bx, by, bw, bh)
        dictionary[NameObject("/Subtype")] = NameObject("/Line")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        dictionary[NameObject("/L")] = ArrayObject([
            FloatObject(start[0]), FloatObject(start[1]),
            FloatObject(end[0]), FloatObject(end[1])])
        dictionary[NameObject("/LE")] = ArrayObject([
            NameObject("/None"),
            NameObject("/ClosedArrow" if arrow else "/None")])
        rgb = _parse_color(data.color, (0.07, 0.07, 0.07))
        dictionary[NameObject("/C")] = ArrayObject([FloatObject(value) for value in rgb])
        dictionary[NameObject("/BS")] = DictionaryObject({
            NameObject("/W"): FloatObject(stroke), NameObject("/S"): NameObject("/S")})
        dictionary[NameObject("/CA")] = FloatObject(opacity)
        stream.set_data(_ap_line_content(relative, data.color, stroke, arrow, bw, bh))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(bw), FloatObject(bh)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(opacity)}),
        })

    def _build_image_annotation(self, record: _AnnRecord, dictionary: DictionaryObject,
                                stream: StreamObject, geom: _PageGeom) -> None:
        """Изображение/штамп: /Stamp с потоком внешнего вида и растровым XObject."""
        data = record.data
        x, y = float(data.x), float(data.y)
        width = max(1.0, float(data.width))
        height = max(1.0, float(data.height))
        rx, ry, rw, rh = _display_rect_to_user(geom, x, y, width, height)
        dictionary[NameObject("/Subtype")] = NameObject("/Stamp")
        dictionary[NameObject("/Name")] = NameObject(
            "/PdfDeskImage" if data.kind == "image" else "/PdfDeskStamp")
        dictionary[NameObject("/Rect")] = ArrayObject([
            FloatObject(rx), FloatObject(ry), FloatObject(rx + rw), FloatObject(ry + rh)])
        if float(data.opacity) < 0.999:
            dictionary[NameObject("/CA")] = FloatObject(float(data.opacity))
        image_ref = None
        if str(data.image_data or ""):
            try:
                image = _load_image_cached(data.image_data)
                image_ref = self._embed_image(record, image)
            except ValueError as exc:
                self._warn("Изображение аннотации не сохранено: %s" % exc)
                image_ref = None
        if image_ref is None:
            # Чужой штамп без растровых данных — сохраняем исходный /AP как есть.
            return
        stream.set_data(_ap_image_content(width, height))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)])
        stream[NameObject("/Matrix")] = ArrayObject(
            [FloatObject(value) for value in _rotation_matrix(geom.rotation)])
        stream[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref}),
            NameObject("/ExtGState"): DictionaryObject({
                NameObject("/GS0"): _extgstate(float(data.opacity))}),
        })

    def _embed_image(self, record: _AnnRecord, image: "Image.Image") -> IndirectObject:
        """Упаковать PIL-изображение в XObject (/FlateDecode + /SMask при альфе)."""
        writer = self._require_open()
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        rgb = rgba.convert("RGB")
        width, height = rgb.size
        if record.img_stream is None:
            record.img_stream = StreamObject()
            record.img_ref = writer._add_object(record.img_stream)
        stream = record.img_stream
        stream.clear()
        stream.set_data(zlib.compress(rgb.tobytes()))
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Image")
        stream[NameObject("/Width")] = NumberObject(width)
        stream[NameObject("/Height")] = NumberObject(height)
        stream[NameObject("/ColorSpace")] = NameObject("/DeviceRGB")
        stream[NameObject("/BitsPerComponent")] = NumberObject(8)
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        if alpha.getextrema() != (255, 255):
            if record.smask_stream is None:
                record.smask_stream = StreamObject()
                record.smask_ref = writer._add_object(record.smask_stream)
            smask = record.smask_stream
            smask.clear()
            smask.set_data(zlib.compress(alpha.tobytes()))
            smask[NameObject("/Type")] = NameObject("/XObject")
            smask[NameObject("/Subtype")] = NameObject("/Image")
            smask[NameObject("/Width")] = NumberObject(width)
            smask[NameObject("/Height")] = NumberObject(height)
            smask[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
            smask[NameObject("/BitsPerComponent")] = NumberObject(8)
            smask[NameObject("/Filter")] = NameObject("/FlateDecode")
            stream[NameObject("/SMask")] = record.smask_ref
        if record.img_ref is None:  # pragma: no cover - защита
            raise RuntimeError("Не удалось создать объект изображения аннотации.")
        return record.img_ref

    def _attach_annotations(self) -> None:
        """Временно вернуть аннотации модели в PDF (только на время сохранения)."""
        self._attached = []
        for page_index, records in enumerate(self._annots):
            if not records:
                continue
            page = self._page(page_index)
            geom = self._geometry(page_index)
            for record in records:
                self._refresh_annotation(record, geom)
                page_ref = page.indirect_reference
                if record.pdf_dict is not None and page_ref is not None:
                    record.pdf_dict[NameObject("/P")] = page_ref
                if record.popup_dict is not None and page_ref is not None:
                    record.popup_dict[NameObject("/P")] = page_ref
                ref = record.pdf_ref
                if ref is None:  # pragma: no cover - защита
                    continue
                array = self._page_annots_array(page, create=True)
                if array is None:  # pragma: no cover - защита
                    continue
                array.append(ref)
                self._attached.append((array, ref))
                if record.popup_ref is not None:
                    # Попап заметки — самостоятельная аннотация страницы.
                    array.append(record.popup_ref)
                    self._attached.append((array, record.popup_ref))
            self._restore_reply_relationships(array, records)

    @staticmethod
    def _restore_reply_relationships(array: ArrayObject,
                                    records: Sequence[_AnnRecord]) -> None:
        by_name: dict[str, list[Any]] = {}
        for item in array:
            obj = item.get_object() if hasattr(item, "get_object") else item
            if not isinstance(obj, DictionaryObject):
                continue
            name = str(obj.get("/NM", "") or "")
            if name:
                by_name.setdefault(name, []).append(item)
        for record in records:
            target_name = record.metadata.get("irt_nm")
            if not target_name or record.pdf_dict is None:
                continue
            targets = by_name.get(str(target_name), ())
            if len(targets) == 1:
                record.pdf_dict[NameObject("/IRT")] = targets[0]

    def _detach_annotations(self) -> None:
        for array, ref in self._attached:
            try:
                array[:] = [item for item in array if item is not ref]
            except Exception:  # pragma: no cover - защита от нестандартных массивов
                pass
        self._attached = []

    # -- невидимый текстовый слой (поиск/копирование текста заметок) --------
    def _page_fonts(self, page: Any) -> tuple[DictionaryObject, DictionaryObject]:
        resources = page.get("/Resources")
        if resources is None:
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        else:
            resources = resources.get_object() if hasattr(resources, "get_object") else resources
        if not isinstance(resources, DictionaryObject):  # pragma: no cover - битый файл
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        fonts = resources.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            resources[NameObject("/Font")] = fonts
        else:
            fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
        if not isinstance(fonts, DictionaryObject):  # pragma: no cover - битый файл
            fonts = DictionaryObject()
            resources[NameObject("/Font")] = fonts
        return resources, fonts

    def _layer_stream(self, page: Any) -> tuple[StreamObject, IndirectObject]:
        key = id(page)
        cached = self._layer_cache.get(key)
        if cached is not None and cached[2] is page:
            return cached[0], cached[1]
        writer = self._require_open()
        stream = StreamObject()
        ref = writer._add_object(stream)
        self._layer_cache[key] = (stream, ref, page)
        return stream, ref

    def _invisible_text_lines(self, font: _EmbeddedFont, data: Annotation,
                              geom: _PageGeom, size: float) -> list[str]:
        """Строки невидимого текста (режим 3 Tr) в координатах страницы."""
        # У заметки видимый значок маленький, но текст попапа должен извлекаться
        # читаемыми строками, поэтому для слоя поиска берём разумную ширину.
        wrap_width = float(data.width)
        if getattr(data, "kind", "") == "note":
            wrap_width = max(wrap_width, 200.0)
        lines = font.wrap_lines(str(data.text or ""), size,
                                max(4.0, wrap_width - 2 * _TEXT_INSET))
        leading = size * _TEXT_LEADING
        ascent = font.ascent * size / 1000.0
        matrix = _rotation_matrix(geom.rotation)
        usable = max(4.0, wrap_width - 2 * _TEXT_INSET)
        code = _alignment_code(getattr(data, "alignment", "left"))
        result: list[str] = []
        offset = _TEXT_INSET + ascent
        for line in lines:
            if not line:
                offset += leading
                continue
            line_x = _TEXT_INSET
            if code:
                line_width = font.text_width(line, size)
                if code == 1:
                    line_x += max(0.0, (usable - line_width) / 2.0)
                else:
                    line_x += max(0.0, usable - line_width)
            ux, uy = _display_to_user_point(geom, float(data.x) + line_x,
                                            float(data.y) + offset)
            result.append("/%s %s Tf" % (font.resource_name, _fmt(size)))
            result.append("%s %s %s %s %s %s Tm" % tuple(
                _fmt(value) for value in (matrix[0], matrix[1], matrix[2], matrix[3], ux, uy)))
            result.append("<%s> Tj" % font.encode(line).hex().upper())
            offset += leading
        return result

    def _attach_text_layer(self) -> None:
        """Добавить на страницы невидимый текст заметок (для поиска и копирования).

        Визуально слой не виден (режим отрисовки 3), он нужен, чтобы русский
        текст заметок извлекался из сохранённого PDF и находился поиском.
        """
        writer = self._require_open()
        self._layer_restore = []
        for page_index, records in enumerate(self._annots):
            texts = [record.data for record in records
                     if record.data.kind in ("text", "note")
                     and str(record.data.text or "")]
            if not texts:
                continue
            page = self._page(page_index)
            geom = self._geometry(page_index)
            lines: list[str] = []
            used: dict[str, IndirectObject] = {}
            for data in texts:
                try:
                    font, font_ref = self._font_for_family(data.font_family)
                except (ValueError, RuntimeError) as exc:
                    self._warn("Текст заметки не добавлен в слой поиска: %s" % exc)
                    continue
                used[font.resource_name] = font_ref
                lines.extend(self._invisible_text_lines(
                    font, data, geom, max(1.0, float(data.font_size))))
            if not lines:
                continue
            stream, ref = self._layer_stream(page)
            stream.set_data(("q\nBT\n3 Tr\n" + "\n".join(lines) + "\nET\nQ\n").encode("ascii"))
            stream[NameObject('/PdfDeskTextLayer')] = BooleanObject(True)

            raw_contents = page.get("/Contents")
            if raw_contents is None:
                page[NameObject("/Contents")] = ref

                def undo_contents(p=page):  # noqa: ANN001
                    try:
                        del p[NameObject("/Contents")]
                    except KeyError:  # pragma: no cover
                        pass
            else:
                contents = (raw_contents.get_object()
                            if hasattr(raw_contents, "get_object") else raw_contents)
                if isinstance(contents, ArrayObject):
                    contents.append(ref)

                    def undo_contents(a=contents, r=ref):  # noqa: ANN001
                        a[:] = [item for item in a if item is not r]
                else:
                    page[NameObject("/Contents")] = ArrayObject([raw_contents, ref])

                    def undo_contents(p=page, original=raw_contents):  # noqa: ANN001
                        p[NameObject("/Contents")] = original
            self._layer_restore.append(undo_contents)

            _resources, fonts = self._page_fonts(page)
            for name, font_ref in used.items():
                key = NameObject("/" + name)
                if key in fonts:
                    continue
                fonts[key] = font_ref

                def undo_font(f=fonts, k=key):  # noqa: ANN001
                    try:
                        del f[k]
                    except KeyError:  # pragma: no cover
                        pass
                self._layer_restore.append(undo_font)
        _LOG.debug("Текстовый слой добавлен: %d изменений", len(self._layer_restore))

    def _detach_text_layer(self) -> None:
        for undo in reversed(self._layer_restore):
            try:
                undo()
            except Exception:  # pragma: no cover - откат не должен ломать сохранение
                pass
        self._layer_restore = []

    def _drop_annotation_objects(self) -> None:
        """Обнулить объекты аннотаций в старом дереве (перед откатом состояния)."""
        writer = self._writer
        if writer is None:
            return
        for page in self._annots:
            for record in page:
                for ref in (record.pdf_ref, record.ap_ref, record.img_ref,
                            record.smask_ref, record.popup_ref):
                    if ref is None:
                        continue
                    try:
                        writer._replace_object(ref, NullObject())
                    except Exception:  # pragma: no cover
                        pass
                record.pdf_dict = None
                record.pdf_ref = None
                record.ap_stream = None
                record.ap_ref = None
                record.img_stream = None
                record.img_ref = None
                record.smask_stream = None
                record.smask_ref = None
                record.popup_dict = None
                record.popup_ref = None

    # -- операции со страницами --------------------------------------------
    def rotate(self, index: int, clockwise: bool = True) -> None:
        """Повернуть страницу на 90° (по часовой стрелке или против)."""
        page_index = self._page_index(index)
        page = self._page(page_index)
        current = _norm_rotation(page.get_inherited("/Rotate", 0) or 0)
        target = (current + (90 if clockwise else -90)) % 360
        self._push_undo()
        page[NameObject("/Rotate")] = NumberObject(target)
        # A page rotation is not an edit to each annotation. Leaving records
        # unmodified lets the serializer retain their original /M metadata.
        self._touch()

    def delete_page(self, index: int) -> None:
        """Удалить страницу (последнюю страницу удалить нельзя)."""
        page_index = self._page_index(index)
        if self.page_count <= 1:
            raise ValueError("Нельзя удалить единственную страницу документа.")
        writer = self._require_open()
        page = writer.pages[page_index]
        labels = self._page_labels()
        if self._page_has_external_references(page):
            raise ValueError(
                "Страница используется внутренними ссылками или закладками. "
                "Удалите эти переходы перед удалением страницы."
            )
        self._push_undo()
        self._remove_page_widgets(page)
        writer.remove_page(page_index, clean=True)
        self._annots.pop(page_index)
        self._page_sources.pop(page_index)
        self._view_page_ids.pop(page_index)
        if labels is not None:
            del labels[page_index]
            self._set_page_labels(labels)
        self._touch()

    def move_page(self, index: int, to_index: int) -> None:
        """Переместить страницу так, чтобы она оказалась на позиции ``to_index``."""
        page_index = self._page_index(index)
        try:
            target = int(to_index)
        except (TypeError, ValueError):
            raise ValueError("Новый индекс страницы должен быть целым числом.") from None
        count = self.page_count
        if not (0 <= target < count):
            raise IndexError("Нельзя переместить страницу на позицию %d: всего страниц %d."
                             % (target, count))
        if target == page_index:
            return
        labels = self._page_labels()
        self._push_undo()
        writer = self._require_open()
        page = writer.pages[page_index]
        writer.remove_page(page_index)
        writer.insert_page(page, target)
        self._annots.insert(target, self._annots.pop(page_index))
        self._page_sources.insert(target, self._page_sources.pop(page_index))
        self._view_page_ids.insert(target, self._view_page_ids.pop(page_index))
        if labels is not None:
            labels.insert(target, labels.pop(page_index))
            self._set_page_labels(labels)
        self._touch()

    def _page_labels(self) -> list[str] | None:
        """Return displayed labels so page moves/deletes keep labels with pages."""
        try:
            return list(self._require_open().page_labels)
        except Exception:
            # A malformed/unreadable number tree is left byte-for-byte attached
            # to the catalog rather than replaced with guessed labels.
            return None

    def _set_page_labels(self, labels: list[str]) -> None:
        """Store exact displayed labels after page positions have changed."""
        writer = self._require_open()
        nums = ArrayObject()
        for index, label in enumerate(labels):
            nums.append(NumberObject(index))
            nums.append(DictionaryObject({NameObject("/P"): TextStringObject(label)}))
        writer._root_object[NameObject("/PageLabels")] = DictionaryObject({
            NameObject("/Nums"): nums,
        })

    def _remove_page_widgets(self, page: Any) -> None:
        """Remove deleted-page widgets from AcroForm fields, preserving siblings."""
        writer = self._require_open()
        raw_annots = page.get("/Annots")
        annots = raw_annots.get_object() if hasattr(raw_annots, "get_object") else raw_annots
        if not isinstance(annots, (ArrayObject, list, tuple)):
            return
        removed = [item for item in annots
                   if str((item.get_object() if hasattr(item, "get_object") else item)
                          .get("/Subtype", "")) == "/Widget"]
        if not removed:
            return
        root = writer._root_object
        acro = root.get("/AcroForm")
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if not isinstance(acro, DictionaryObject):
            return
        raw_fields = acro.get("/Fields")
        fields = raw_fields.get_object() if hasattr(raw_fields, "get_object") else raw_fields
        if not isinstance(fields, ArrayObject):
            return

        def same_ref(left: Any, right: Any) -> bool:
            if isinstance(left, IndirectObject) and isinstance(right, IndirectObject):
                return (left.idnum, left.generation) == (right.idnum, right.generation)
            return left is right

        for widget_ref in removed:
            widget = widget_ref.get_object() if hasattr(widget_ref, "get_object") else widget_ref
            if not isinstance(widget, DictionaryObject):
                continue
            raw_parent = widget.get("/Parent")
            parent = raw_parent.get_object() if hasattr(raw_parent, "get_object") else raw_parent
            field = parent if isinstance(parent, DictionaryObject) else widget
            raw_kids = field.get("/Kids")
            kids = raw_kids.get_object() if hasattr(raw_kids, "get_object") else raw_kids
            if isinstance(kids, ArrayObject):
                kids[:] = [kid for kid in kids if not same_ref(kid, widget_ref)]
                if kids:
                    continue
            field_ref = raw_parent if parent is field and raw_parent is not None else widget_ref
            fields[:] = [entry for entry in fields if not same_ref(entry, field_ref)]

    def _page_has_external_references(self, page: Any) -> bool:
        """Refuse deletion when a catalog/action still navigates to this page."""
        target = getattr(page, "indirect_reference", None)
        if target is None:
            return False
        target_key = (target.idnum, target.generation)
        seen: set[tuple[int, int]] = set()
        visited: set[int] = set()

        def visit(value: Any) -> bool:
            if isinstance(value, IndirectObject):
                key = (value.idnum, value.generation)
                if key == target_key:
                    return True
                if key in seen:
                    return False
                seen.add(key)
                try:
                    return visit(value.get_object())
                except Exception:
                    return False
            if id(value) in visited:
                return False
            if isinstance(value, DictionaryObject):
                visited.add(id(value))
                for raw_key, child in value.items():
                    # These are structural links to the page tree and the
                    # annotation's owning page, not navigation destinations.
                    if str(raw_key) in ("/Kids", "/P"):
                        continue
                    if visit(child):
                        return True
            elif isinstance(value, (ArrayObject, list, tuple)):
                visited.add(id(value))
                return any(visit(child) for child in value)
            return False

        return any(visit(obj) for obj in self._require_open()._objects if obj is not None)

    # -- история ------------------------------------------------------------
    def _new_view_page_ids(self, count: int) -> list[int]:
        identifiers = list(range(self._next_view_page_id, self._next_view_page_id + count))
        self._next_view_page_id += count
        return identifiers

    def page_view_id(self, index: int) -> int:
        """Session-only page identity for view aids; never stored in PDF objects."""
        return self._view_page_ids[self._page_index(index)]

    def _make_snapshot(self) -> _Snapshot:
        data = self._write_bytes()
        return _Snapshot(
            revision=self._revision,
            data=data,
            annots=[[copy.deepcopy(record.data) for record in page] for page in self._annots],
            page_sources=list(self._page_sources),
            view_page_ids=list(self._view_page_ids),
            size=len(data),
            refreshed_form_appearances=set(self._refreshed_form_appearances),
            annotation_metadata=[[copy.deepcopy(record.metadata) for record in page]
                                 for page in self._annots],
            annotation_popup_metadata=[[copy.deepcopy(record.popup_metadata) for record in page]
                                       for page in self._annots],
            annotation_modified=[[record.modified for record in page]
                                 for page in self._annots],
        )

    def _push_undo(self, coalesce: str | None = None, *, permission: str = "edit") -> None:
        self._require_edit_permission(permission)
        writer = self._require_open()
        now = time.monotonic()
        if (coalesce is not None and self._undo and self._last_edit_key == coalesce
                and (now - self._last_edit_time) < _FORM_VALUE_COALESCE_SEC):
            self._last_edit_time = now
            return
        snapshot = self._make_snapshot()
        self._undo.append(snapshot)
        self._undo_bytes += snapshot.size
        while len(self._undo) > _MAX_UNDO_STEPS or (
                self._undo_bytes > _MAX_UNDO_BYTES and len(self._undo) > 1):
            dropped = self._undo.pop(0)
            self._undo_bytes -= dropped.size
        self._redo.clear()
        self._last_edit_key = coalesce
        self._last_edit_time = now

    def _restore(self, snapshot: _Snapshot) -> None:
        self._close_pdfium()
        self._drop_annotation_objects()
        try:
            reader = PdfReader(io.BytesIO(snapshot.data), strict=False)
            writer = PdfWriter(clone_from=reader)
        except Exception as exc:
            raise RuntimeError("Не удалось восстановить предыдущее состояние: %s" % exc) from exc
        self._keepalive.append(reader)
        self._writer = writer
        self._annots = []
        for page_index, page_records in enumerate(snapshot.annots):
            page_metadata = (snapshot.annotation_metadata[page_index]
                             if page_index < len(snapshot.annotation_metadata) else [])
            page_popup_metadata = (snapshot.annotation_popup_metadata[page_index]
                                   if page_index < len(snapshot.annotation_popup_metadata) else [])
            page_modified = (snapshot.annotation_modified[page_index]
                             if page_index < len(snapshot.annotation_modified) else [])
            restored = []
            for position, data in enumerate(page_records):
                metadata = page_metadata[position] if position < len(page_metadata) else {}
                popup_metadata = (page_popup_metadata[position]
                                  if position < len(page_popup_metadata) else {})
                modified = page_modified[position] if position < len(page_modified) else False
                restored.append(_AnnRecord(data=copy.deepcopy(data),
                                           metadata=copy.deepcopy(metadata),
                                           popup_metadata=copy.deepcopy(popup_metadata),
                                           modified=modified))
            self._annots.append(restored)
        self._page_sources = list(snapshot.page_sources)
        self._view_page_ids = (list(snapshot.view_page_ids)
                              if len(snapshot.view_page_ids) == len(writer.pages)
                              else self._new_view_page_ids(len(writer.pages)))
        self._revision = snapshot.revision
        self._refreshed_form_appearances = set(snapshot.refreshed_form_appearances)
        self._doc_revision += 1
        self._serialized = None
        self._field_map = None
        self._field_map_revision = -1
        self._font_refs = {}
        self._font_holders = []
        self._attached = []
        self._layer_cache = {}
        self._layer_restore = []

    def undo(self) -> bool:
        """Отменить последнее изменение. ``True``, если откат выполнен."""
        self._require_open()
        if not self._undo:
            return False
        snapshot = self._undo.pop()
        self._undo_bytes = max(0, self._undo_bytes - snapshot.size)
        try:
            current = self._make_snapshot()
        except RuntimeError:
            self._undo.append(snapshot)
            raise
        self._redo.append(current)
        self._restore(snapshot)
        self._last_edit_key = None
        return True

    def redo(self) -> bool:
        """Повторить отменённое изменение. ``True``, если повтор выполнен."""
        self._require_open()
        if not self._redo:
            return False
        snapshot = self._redo.pop()
        self._undo.append(self._make_snapshot())
        self._undo_bytes += snapshot.size
        self._restore(snapshot)
        self._last_edit_key = None
        return True

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    # -- формы --------------------------------------------------------------
    def _fields(self) -> dict[str, _FieldRef]:
        if self._field_map is not None and self._field_map_revision == self._doc_revision:
            return self._field_map
        writer = self._require_open()
        result: dict[str, _FieldRef] = {}
        for page_index in range(len(writer.pages)):
            page = writer.pages[page_index]
            array = self._page_annots_array(page, create=False)
            if array is None:
                continue
            for item in list(array):
                obj = item.get_object() if hasattr(item, "get_object") else item
                if not isinstance(obj, DictionaryObject):
                    continue
                if str(obj.get("/Subtype", "")) != "/Widget":
                    continue
                field = _field_dict_for_widget(obj)
                if field is None:
                    continue
                qualified = _qualified_field_name(field)
                if not qualified:
                    continue
                source_index = (self._page_sources[page_index]
                                if page_index < len(self._page_sources) else 0)
                key = "f%d:%s" % (source_index, qualified)
                ref = result.get(key)
                if ref is None:
                    kind, ft, ff = self._classify_field(field)
                    ref = _FieldRef(
                        key=key, qualified_name=qualified,
                        partial_name=str(field.get("/T", "")),
                        label=self._field_label(field, qualified),
                        kind=kind, page_index=page_index, source_index=source_index,
                        field=field, ft=ft, ff=ff)
                    result[key] = ref
                elif ref.field is not field and all(item is not field
                                                    for item in ref.extra_fields):
                    # Одноимённые поля-двойники (частый случай в PDF): значение
                    # нужно записывать во все такие словари, иначе часть
                    # виджетов останется пустой.
                    ref.extra_fields.append(field)
                ref.widgets.append((page_index, obj))
        self._field_map = result
        self._field_map_revision = self._doc_revision
        return result

    def _classify_field(self, field: DictionaryObject) -> tuple[str, str, int]:
        ft = str(field.get("/FT", ""))
        try:
            ff = int(field.get("/Ff", 0) or 0)
        except (TypeError, ValueError):
            ff = 0
        if self._has_xfa:
            return "unsupported", ft, ff
        if ft == "/Tx":
            kind = "text"
        elif ft == "/Ch":
            kind = "choice"
        elif ft == "/Btn":
            if ff & (1 << 16) or ff & (1 << 15):  # pushbutton / radio
                kind = "unsupported"
            else:
                kind = "checkbox"
        else:
            kind = "unsupported"
        return kind, ft, ff

    @staticmethod
    def _field_label(field: DictionaryObject, qualified: str) -> str:
        alternate = field.get("/TU")
        if alternate:
            return str(alternate)
        partial = str(field.get("/T", ""))
        return partial or qualified

    @staticmethod
    def _field_flags(field: Any) -> int:
        try:
            return int(field.get("/Ff", 0) or 0)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - битый /Ff
            return 0

    def _widget_kind(self, ref: _FieldRef) -> str:
        """Вид поля для :meth:`form_widgets`.

        Отличается от ``_FieldRef.kind`` только для радиогрупп: в контракте
        :class:`~src.contracts.FormField` такого вида нет (там радиогруппа —
        «unsupported»), а по виджетам её нужно показывать и заполнять.
        """
        if self._has_xfa:
            return "unsupported"
        if ref.ft == "/Tx":
            return "text"
        if ref.ft == "/Ch":
            return "choice"
        if ref.ft == "/Btn":
            if ref.ff & _BTN_PUSHBUTTON:
                return "unsupported"
            if ref.ff & _BTN_RADIO:
                return "radio"
            return "checkbox"
        return "unsupported"

    def _radio_exports(self, ref: _FieldRef) -> list[str]:
        """Экспортное значение каждой кнопки группы (в порядке виджетов).

        По спецификации это элемент ``/Opt``, но многие генераторы пишут туда
        повторяющиеся или неполные списки (например, ``['M', 'M']`` на две
        кнопки). Поэтому значение из ``/Opt`` берётся, только если оно
        непустое и уникальное; иначе используется имя включённого состояния
        (``/0``, ``/1``, …). Итоговый список всегда уникален внутри группы,
        иначе ``set_form_value(key, export_value)`` не смог бы однозначно
        выбрать кнопку.
        """
        widgets = [widget for _, widget in ref.widgets]
        options = ref.options()
        counts: dict[str, int] = {}
        for value in options:
            counts[value] = counts.get(value, 0) + 1
        states = [_widget_on_states(widget) for widget in widgets]
        used: set[str] = set()
        result: list[str] = []
        for position, widget in enumerate(widgets):
            option = options[position] if position < len(options) else ""
            state = states[position][0].lstrip("/") if states[position] else ""
            candidate = option if (option and counts.get(option) == 1) else (state or option)
            value = candidate
            if not value or value in used:
                value = state if state and state not in used else value
            base = value or "radio"
            suffix = 2
            while not value or value in used:
                value = "%s#%d" % (base, suffix)
                suffix += 1
            used.add(value)
            result.append(value)
        return result

    def _radio_value(self, ref: _FieldRef) -> str:
        """Экспортное значение выбранной кнопки группы (``""`` — выбора нет)."""
        raw = ref.field.get("/V")
        if raw is None:
            return ""
        name = str(raw)
        if name in ("", "/Off", "Off"):
            return ""
        widgets = [widget for _, widget in ref.widgets]
        exports = self._radio_exports(ref)
        for position, widget in enumerate(widgets):
            if str(widget.get("/AS", "")) == name:
                return exports[position]
        plain = name.lstrip("/")
        for position, widget in enumerate(widgets):
            if plain in [state.lstrip("/") for state in _widget_on_states(widget)]:
                return exports[position]
        return plain

    def form_widgets(self, index: int | None = None, *,
                     include_appearance: bool = False,
                     include_authoring_metadata: bool = False) -> list[dict]:
        """Виджеты формы: отдельная запись на каждый виджет страницы ``index``.

        Поля AcroForm часто состоят из нескольких виджетов (одно поле — много
        прямоугольников, радиогруппа — много кнопок), поэтому в отличие от
        :meth:`form_fields` здесь возвращается по записи на виджет:

        * ``key`` — ключ поля, который принимает :meth:`set_form_value`;
        * ``rect`` — ``[x, y, w, h]`` в пунктах PDF, начало координат в ЛЕВОМ
          ВЕРХНЕМ углу отображаемой страницы (учтены /CropBox и /Rotate);
        * ``kind`` — ``text`` / ``checkbox`` / ``radio`` / ``choice`` /
          ``unsupported``;
        * ``value`` — строка (text, choice, radio — экспортное значение
          выбранной кнопки) либо ``bool`` (checkbox);
        * ``options`` — варианты для choice, экспортные значения кнопок для
          radio (в порядке кнопок группы), иначе пустой список;
        * ``readonly`` — поле недоступно для правки (бит ReadOnly поля/виджета);
        * ``multiline`` — многострочное текстовое поле;
        * ``export_value`` — для radio: значение, которое нужно передать в
          ``set_form_value(key, export_value)``, чтобы выбрать ИМЕННО эту
          кнопку; для остальных видов — пустая строка.

        Виджеты без корректного ``/Rect`` пропускаются. При ``index=None``
        возвращаются виджеты всех страниц.
        """
        self._require_open()
        if index is None:
            pages = list(range(self.page_count))
        else:
            pages = [self._page_index(index)]
        result: list[dict] = []
        fields = self._fields()
        for page_index in pages:
            geom = self._geometry(page_index)
            tab_positions: dict[int, int] = {}
            if include_authoring_metadata:
                page = self._page(page_index)
                if str(page.get("/Tabs", "")) == "/S":
                    annots = self._page_annots_array(page, create=False) or []
                    sequence = 0
                    for annotation in annots:
                        obj = annotation.get_object() if hasattr(annotation, "get_object") else annotation
                        if isinstance(obj, DictionaryObject) and str(obj.get("/Subtype", "")) == "/Widget":
                            tab_positions[id(obj)] = sequence
                            sequence += 1
            for ref in fields.values():
                kind = self._widget_kind(ref)
                for position, (widget_page, widget) in enumerate(ref.widgets):
                    if widget_page != page_index:
                        continue
                    rect = _rect_tuple(widget.get("/Rect"))
                    if rect is None:  # виджет без геометрии показать нельзя
                        continue
                    entry = self._widget_entry(
                        ref, kind, position, widget, rect, geom,
                        include_appearance=include_appearance,
                        include_authoring_metadata=include_authoring_metadata)
                    if id(widget) in tab_positions:
                        entry["tab_order"] = tab_positions[id(widget)]
                    result.append(entry)
        return result

    def _widget_entry(self, ref: _FieldRef, kind: str, position: int,
                      widget: DictionaryObject,
                      rect: tuple[float, float, float, float], geom: _PageGeom,
                      *, include_appearance: bool = False,
                      include_authoring_metadata: bool = False) -> dict:
        x, y, width, height = _user_rect_to_display(geom, *rect)
        readonly = ref.readonly or bool(self._field_flags(widget) & 1)
        options: list[str] = []
        export_value = ""
        if kind == "checkbox":
            value: str | bool = bool(ref.value())
        elif kind == "radio":
            options = self._radio_exports(ref)
            export_value = options[position]
            value = self._radio_value(ref)
        elif kind == "choice":
            options = ref.options()
            value = str(ref.value())
        elif kind == "text":
            value = str(ref.value())
        else:
            raw = ref.field.get("/V")
            value = str(raw) if isinstance(raw, str) else ""
        entry = {
            "key": ref.key,
            "rect": [x, y, width, height],
            "kind": kind,
            "value": value,
            "options": options,
            "readonly": readonly,
            "multiline": bool(ref.multiline) and kind == "text",
            "export_value": export_value,
        }
        if include_appearance and kind in ("text", "choice"):
            appearance = self._form_widget_appearance(widget)
            if appearance:
                entry.update(appearance)
        if include_authoring_metadata:
            entry["name"] = ref.partial_name
            default = ref.field.get("/DV")
            if kind == "checkbox":
                on_state = ref.on_state()
                default = bool(on_state) and str(default or "/Off") == on_state
            elif kind == "radio":
                default = "" if default is None or str(default) in ("/Off", "Off") else str(default).lstrip("/")
            else:
                default = "" if default is None else str(default)
            entry["default_value"] = default
        return entry

    def _form_widget_appearance(self, widget: DictionaryObject) -> dict[str, Any]:
        """Read effective /DA font metadata without changing legacy records."""
        current: Any = widget
        da = ""
        for _ in range(33):
            if current is None:
                break
            current = current.get_object() if hasattr(current, "get_object") else current
            if not isinstance(current, DictionaryObject):
                break
            da = str(current.get("/DA", "") or "")
            if da:
                break
            parent = current.get("/Parent")
            if parent is None:
                break
            current = parent

        root = self._require_open()._root_object
        acro = root.get("/AcroForm")
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if not da and isinstance(acro, DictionaryObject):
            da = str(acro.get("/DA", "") or "")
        match = _DA_RE.search(da)
        if not match:
            return {}
        resource_name = match.group(1)
        try:
            font_size = max(0.0, float(match.group(2)))
        except ValueError:
            return {}

        # A resource may be an alias such as /F1; resolve its BaseFont when
        # AcroForm /DR provides it. Standard resources already map directly.
        pdf_font_name = resource_name
        if isinstance(acro, DictionaryObject):
            resources = acro.get("/DR")
            resources = resources.get_object() if hasattr(resources, "get_object") else resources
            fonts = resources.get("/Font") if isinstance(resources, DictionaryObject) else None
            fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
            font = fonts.get("/" + resource_name) if isinstance(fonts, DictionaryObject) else None
            font = font.get_object() if hasattr(font, "get_object") else font
            if isinstance(font, DictionaryObject) and font.get("/BaseFont"):
                pdf_font_name = str(font.get("/BaseFont"))
        family = _family_from_font_name(pdf_font_name)
        alignment_value: Any = None
        current = widget
        for _ in range(33):
            current = current.get_object() if hasattr(current, "get_object") else current
            if not isinstance(current, DictionaryObject):
                break
            if "/Q" in current:
                alignment_value = current.get("/Q")
                break
            current = current.get("/Parent")
            if current is None:
                break
        try:
            alignment_code = int(alignment_value) if alignment_value is not None else 0
        except (TypeError, ValueError):
            alignment_code = 0
        alignment = {0: "left", 1: "center", 2: "right"}.get(alignment_code, "left")
        return {"font_size": font_size, "font_family": family,
                "alignment": alignment}

    def form_fields(self, index: int | None = None) -> list[FormField]:
        """Поля формы: для страницы ``index`` либо для всего документа."""
        self._require_open()
        fields = self._fields()
        if index is None:
            return [ref.to_form_field() for ref in fields.values()]
        page_index = self._page_index(index)
        return [ref.to_form_field() for ref in fields.values() if ref.page_index == page_index]

    def set_form_value(self, key: str, value: str | bool) -> None:
        """Заполнить поле формы и обновить его внешний вид."""
        self._require_open()
        self._require_edit_permission("fill")
        if self._has_xfa:
            raise ValueError("Документ содержит XFA-форму: заполнение таких форм не поддерживается.")
        ref = self._fields().get(str(key))
        if ref is None:
            raise ValueError("Поле формы «%s» не найдено." % key)
        widget_kind = self._widget_kind(ref)
        if ref.kind == "unsupported" and widget_kind != "radio":
            reason = _UNSUPPORTED_FT.get(ref.ft, "тип «%s»" % (ref.ft or "неизвестен"))
            raise ValueError("Поле «%s» нельзя заполнить: %s не поддерживается." % (ref.label, reason))
        if ref.readonly:
            raise ValueError("Поле «%s» доступно только для чтения." % ref.label)

        if widget_kind == "radio":
            if isinstance(value, bool) or value is None:
                raise ValueError(
                    "Для группы переключателей «%s» ожидается строка — экспортное значение "
                    "кнопки (см. form_widgets)." % ref.label)
            self._push_undo(coalesce="field:" + ref.key, permission="fill")
            self._apply_radio(ref, str(value))
            self._touch()
            return
        if ref.kind == "checkbox":
            if isinstance(value, bool):
                flag = value
            elif isinstance(value, (int, float)):
                flag = bool(value)
            else:
                text = str(value).strip().lower()
                if text in ("1", "true", "yes", "on", "да", "истина", "вкл"):
                    flag = True
                elif text in ("", "0", "false", "no", "off", "нет", "ложь", "выкл"):
                    flag = False
                else:
                    raise ValueError("Для флажка «%s» ожидается true или false, получено «%s»."
                                     % (ref.label, value))
            self._push_undo(coalesce="field:" + ref.key, permission="fill")
            self._apply_checkbox(ref, flag)
        else:
            if value is None:
                text = ""
            elif isinstance(value, bool):
                raise ValueError("Для поля «%s» ожидается строка, получено логическое значение."
                                 % ref.label)
            else:
                text = str(value)
            if ref.kind == "choice" and not ref.combo and not ref.editable_choice:
                options = ref.options()
                if options and text not in options:
                    raise ValueError(
                        "Значение «%s» отсутствует в списке поля «%s». Допустимо: %s."
                        % (text, ref.label, ", ".join(options[:6])))
            self._push_undo(coalesce="field:" + ref.key, permission="fill")
            self._apply_text_value(ref, text)
        self._touch()

    def _apply_checkbox(self, ref: _FieldRef, flag: bool) -> None:
        writer = self._require_open()
        on_state = ref.on_state() or "/Yes"
        target = NameObject(on_state if flag else "/Off")
        for field in [ref.field] + ref.extra_fields:
            try:
                field[NameObject("/V")] = target
            except Exception:  # pragma: no cover
                pass
        for page_index, widget in ref.widgets:
            if "/AP" not in widget and "/AP" in ref.field:
                widget[NameObject("/AP")] = ref.field["/AP"]
            if flag:
                widget_on = _widget_on_state(widget) or on_state
                widget[NameObject("/AS")] = NameObject(widget_on)
                self._set_button_on_appearance(widget, page_index, "checkbox", widget_on)
            else:
                widget[NameObject("/AS")] = target
        self._refresh_active_button_appearances()
        self._clear_need_appearances_if_ready()

    def _apply_radio(self, ref: _FieldRef, text: str) -> None:
        """Выбрать кнопку радиогруппы по экспортному значению (или снять выбор).

        В ``/V`` и ``/AS`` записывается имя включённого состояния кнопки (так
        делают Adobe и LibreOffice), а экспортное значение из ``/Opt`` остаётся
        представлением для интерфейса.
        """
        writer = self._require_open()
        widgets = [widget for _, widget in ref.widgets]
        exports = self._radio_exports(ref)
        states = [_widget_on_states(widget) for widget in widgets]
        target = text.strip()
        chosen: int | None = None
        if target and target not in ("/Off", "Off"):
            matches = [position for position, value in enumerate(exports) if value == target]
            if not matches:
                plain = target.lstrip("/")
                matches = [position for position, state in enumerate(states)
                           if plain and plain in [name.lstrip("/") for name in state]]
            if not matches:
                plain = target.lstrip("/")
                matches = [position for position, value in enumerate(exports) if value == plain]
            if not matches:
                allowed = ", ".join(value for value in exports if value) or "—"
                raise ValueError("У группы «%s» нет кнопки со значением «%s». Допустимо: %s."
                                 % (ref.label, text, allowed))
            chosen = matches[0]

        if chosen is None:
            for field in [ref.field] + ref.extra_fields:
                field[NameObject("/V")] = NameObject("/Off")
            for widget in widgets:
                widget[NameObject("/AS")] = NameObject("/Off")
        else:
            on_state = states[chosen][0] if states[chosen] else ""
            if not on_state:
                raise ValueError("У кнопки «%s» группы «%s» нет потока внешнего вида (/AP): "
                                 "выбрать её нельзя." % (text, ref.label))
            for field in [ref.field] + ref.extra_fields:
                field[NameObject("/V")] = NameObject(on_state)
            for position, widget in enumerate(widgets):
                page_index = ref.widgets[position][0]
                widget_state = on_state if position == chosen else "/Off"
                widget[NameObject("/AS")] = NameObject(widget_state)
                if position == chosen:
                    self._set_button_on_appearance(widget, page_index, "radio", on_state)
        self._refresh_active_button_appearances()
        self._clear_need_appearances_if_ready()

    def _apply_text_value(self, ref: _FieldRef, text: str) -> None:
        writer = self._require_open()
        for field in [ref.field] + ref.extra_fields:
            try:
                field[NameObject("/V")] = TextStringObject(text)
            except Exception:  # pragma: no cover
                pass
        self._ensure_field_font(ref, text)
        for page_index in sorted({page for page, _ in ref.widgets}):
            try:
                writer.update_page_form_field_values(
                    writer.pages[page_index], {ref.qualified_name: text}, auto_regenerate=True)
            except Exception as exc:
                self._warn("Не удалось перестроить внешний вид поля «%s»: %s" % (ref.label, exc))
        self._refreshed_form_appearances.add(ref.key)
        try:
            writer.set_need_appearances_writer(True)
        except Exception:  # pragma: no cover
            pass
        # pypdf asks viewers to regenerate every appearance after a text edit.
        # That makes some independent renderers ignore even complete button APs.
        # Repair the active marks and clear the global request only after every
        # supported, non-empty field has a concrete appearance stream.
        self._refresh_active_button_appearances()
        self._clear_need_appearances_if_ready()

    def _ensure_acroform(self) -> DictionaryObject:
        writer = self._require_open()
        root = writer._root_object
        acro = root.get("/AcroForm")
        if acro is None:
            acro = DictionaryObject({NameObject("/Fields"): ArrayObject()})
            root[NameObject("/AcroForm")] = acro
            return acro
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if not isinstance(acro, DictionaryObject):  # pragma: no cover - битый файл
            raise RuntimeError("Словарь /AcroForm повреждён: заполнение формы невозможно.")
        if "/Fields" not in acro:
            acro[NameObject("/Fields")] = ArrayObject()
        return acro

    def _ensure_field_font(self, ref: _FieldRef, text: str) -> None:
        """Для текста вне Latin-1 подключаем встроенный Unicode-шрифт."""
        if _PYPDF_FONT_API is False:
            return
        if _is_latin1(text):
            return
        try:
            font, font_ref = self._font_for_family("Arial")
        except (ValueError, RuntimeError) as exc:
            self._warn("Поле «%s»: не удалось встроить Unicode-шрифт (%s). "
                       "Текст сохранён, но может отображаться неверно." % (ref.label, exc))
            return
        acro = self._ensure_acroform()
        dr = acro.get("/DR")
        if dr is None:
            dr = DictionaryObject()
            acro[NameObject("/DR")] = dr
        else:
            dr = dr.get_object() if hasattr(dr, "get_object") else dr
        if not isinstance(dr, DictionaryObject):  # pragma: no cover
            dr = DictionaryObject()
            acro[NameObject("/DR")] = dr
        fonts = dr.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            dr[NameObject("/Font")] = fonts
        else:
            fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
        if not isinstance(fonts, DictionaryObject):  # pragma: no cover
            fonts = DictionaryObject()
            dr[NameObject("/Font")] = fonts
        fonts[NameObject("/" + font.resource_name)] = font_ref

        default_da = str(acro.get("/DA", "") or "")
        targets = [ref.field] + ref.extra_fields + [widget for _, widget in ref.widgets]
        for target in targets:
            current = str(target.get("/DA", "") or default_da)
            target[NameObject("/DA")] = TextStringObject(
                _replace_da_font(current, font.resource_name))

    # -- создание и изменение полей формы ----------------------------------
    @staticmethod
    def _form_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name or any(char in name for char in ".\\"):
            raise ValueError("Имя поля должно быть непустым и не содержать точек или обратных косых черт.")
        return name

    @staticmethod
    def _form_rect(rect: Sequence[float]) -> tuple[float, float, float, float]:
        try:
            x, y, width, height = (float(value) for value in rect)
        except (TypeError, ValueError):
            raise ValueError("Позиция поля должна содержать x, y, ширину и высоту.") from None
        if not all(math.isfinite(value) for value in (x, y, width, height)) or width < 4 or height < 4:
            raise ValueError("Размер поля должен быть не меньше 4 × 4 пункта.")
        return x, y, width, height

    @staticmethod
    def _form_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in ("1", "true", "yes", "on", "да", "истина", "вкл"):
            return True
        if text in ("", "0", "false", "no", "off", "нет", "ложь", "выкл"):
            return False
        raise ValueError("Для флажка ожидается значение true или false.")

    def _form_appearance(self, width: float, height: float, *, state: str | None = None,
                         kind: str = "text") -> StreamObject:
        """Create a compact printable widget appearance for newly authored fields."""
        stream = StreamObject()
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/FormType")] = NumberObject(1)
        stream[NameObject("/BBox")] = ArrayObject([FloatObject(0), FloatObject(0),
                                                    FloatObject(width), FloatObject(height)])
        stream[NameObject("/Resources")] = DictionaryObject()
        inset = 1.0
        ops = ["q", "0.18 0.28 0.32 RG", "0.8 w", f"{inset:g} {inset:g} {max(0.1,width-2*inset):g} {max(0.1,height-2*inset):g} re S"]
        if kind in ("checkbox", "radio") and state and state != "/Off":
            if kind == "checkbox":
                ops.extend(["0.05 0.42 0.48 RG", "1.8 w",
                            f"{width*.2:g} {height*.52:g} m {width*.43:g} {height*.28:g} l {width*.8:g} {height*.75:g} l S"])
            else:
                radius = min(width, height) * .22
                cx, cy = width / 2, height / 2
                k = radius * .55228475
                ops.extend(["0.05 0.42 0.48 rg",
                            f"{cx+radius:g} {cy:g} m {cx+radius:g} {cy+k:g} {cx+k:g} {cy+radius:g} {cx:g} {cy+radius:g} c",
                            f"{cx-k:g} {cy+radius:g} {cx-radius:g} {cy+k:g} {cx-radius:g} {cy:g} c",
                            f"{cx-radius:g} {cy-k:g} {cx-k:g} {cy-radius:g} {cx:g} {cy-radius:g} c",
                            f"{cx+k:g} {cy-radius:g} {cx+radius:g} {cy-k:g} {cx+radius:g} {cy:g} c f"])
        ops.append("Q")
        stream.set_data("\n".join(ops).encode("ascii"))
        return stream

    def _set_button_on_appearance(self, widget: DictionaryObject, page_index: int,
                                  kind: str, state: str) -> None:
        """Replace only a selected button state with a font-free vector mark.

        Existing PDFs commonly draw button marks with a ZapfDingbats glyph.
        Some independent renderers omit that glyph when the font is not
        installed, even though ``/V`` and ``/AS`` are correct. Keeping the
        original ``/Off`` appearance and replacing the selected ``/N`` state
        makes the check or radio dot visible without changing names, geometry,
        or unrelated appearance states. ``page_index`` is retained in the
        helper signature for callers documenting the owning page; the widget's
        /Rect already supplies its local appearance dimensions.
        """
        del page_index  # geometry is local to the annotation appearance stream
        writer = self._require_open()
        state = str(state or "").strip()
        if not state or state in ("/Off", "Off"):
            return
        rect = widget.get("/Rect")
        rect = rect.get_object() if hasattr(rect, "get_object") else rect
        if not isinstance(rect, (ArrayObject, list, tuple)) or len(rect) < 4:
            return
        try:
            x0, y0, x1, y1 = (float(rect[index]) for index in range(4))
        except (TypeError, ValueError, OverflowError):
            return
        width, height = abs(x1 - x0), abs(y1 - y0)
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            return

        appearance = widget.get("/AP")
        appearance = appearance.get_object() if hasattr(appearance, "get_object") else appearance
        if not isinstance(appearance, DictionaryObject):
            appearance = DictionaryObject()
            widget[NameObject("/AP")] = writer._add_object(appearance)

        normal = appearance.get("/N")
        normal = normal.get_object() if hasattr(normal, "get_object") else normal
        if not isinstance(normal, DictionaryObject):
            normal = DictionaryObject()
            appearance[NameObject("/N")] = normal

        generated = self._form_appearance(width, height, state=state, kind=kind)
        existing = normal.get(state)
        existing = existing.get_object() if hasattr(existing, "get_object") else existing
        if (isinstance(existing, StreamObject)
                and existing.get_data() == generated.get_data()
                and list(existing.get("/BBox", ())) == list(generated.get("/BBox", ()))):
            return
        normal[NameObject(state)] = writer._add_object(generated)

    def _refresh_active_button_appearances(self) -> None:
        """Make each selected checkbox/radio mark independent of viewer fonts."""
        for ref in self._fields().values():
            kind = self._widget_kind(ref)
            if kind not in ("checkbox", "radio"):
                continue
            for page_index, widget in ref.widgets:
                state = str(widget.get("/AS", "/Off"))
                if state in ("/Off", "Off", ""):
                    continue
                self._set_button_on_appearance(widget, page_index, kind, state)

    @staticmethod
    def _normal_appearance(widget: DictionaryObject) -> Any:
        appearance = widget.get("/AP")
        appearance = appearance.get_object() if hasattr(appearance, "get_object") else appearance
        if not isinstance(appearance, DictionaryObject):
            return None
        normal = appearance.get("/N")
        return normal.get_object() if hasattr(normal, "get_object") else normal

    def _form_appearances_complete(self) -> bool:
        """Return true only when every populated supported field has saved APs.

        Unknown non-empty widgets deliberately keep NeedAppearances enabled;
        the editor cannot claim completeness for appearance formats it does
        not know how to preserve or rebuild.
        """
        for ref in self._fields().values():
            kind = self._widget_kind(ref)
            value = ref.value()
            populated = str(value or "").strip() not in ("", "/Off", "Off")
            if kind in ("checkbox", "radio"):
                for _, widget in ref.widgets:
                    state = str(widget.get("/AS", "/Off"))
                    normal = self._normal_appearance(widget)
                    if isinstance(normal, DictionaryObject):
                        stream = normal.get(state)
                        stream = stream.get_object() if hasattr(stream, "get_object") else stream
                    else:
                        stream = normal
                    if stream is None or not hasattr(stream, "get_data"):
                        return False
                continue
            if kind in ("text", "choice"):
                if not populated:
                    continue
                if (self._initial_need_appearances
                        and ref.key not in self._refreshed_form_appearances):
                    return False
                if not ref.widgets:
                    return False
                for _, widget in ref.widgets:
                    normal = self._normal_appearance(widget)
                    if normal is None or not isinstance(normal, StreamObject):
                        return False
                    if not hasattr(normal, "get_data") or not normal.get_data():
                        return False
                continue
            if populated:
                return False
        return True

    def _clear_need_appearances_if_ready(self) -> None:
        """Disable viewer-wide regeneration only when all active fields are ready."""
        writer = self._require_open()
        root = writer._root_object
        acro = root.get("/AcroForm")
        acro = acro.get_object() if hasattr(acro, "get_object") else acro
        if not isinstance(acro, DictionaryObject) or not _as_bool(acro.get("/NeedAppearances")):
            return
        if self._form_appearances_complete():
            acro[NameObject("/NeedAppearances")] = BooleanObject(False)

    def _install_widget_appearance(self, widget: DictionaryObject, page_index: int,
                                    width: float, height: float, kind: str,
                                    states: Sequence[str] = ()) -> None:
        writer = self._require_open()
        ap = DictionaryObject()
        normal = DictionaryObject()
        if kind in ("checkbox", "radio"):
            on = states[0] if states else "/Yes"
            normal[NameObject("/Off")] = writer._add_object(
                self._form_appearance(width, height, state="/Off", kind=kind))
            normal[NameObject(on)] = writer._add_object(
                self._form_appearance(width, height, state=on, kind=kind))
        else:
            normal = writer._add_object(self._form_appearance(width, height, kind=kind))
        ap[NameObject("/N")] = normal
        widget[NameObject("/AP")] = writer._add_object(ap)

    def create_form_field(self, kind: str, name: str, page_index: int,
                          rect: Sequence[float] | None = None, *,
                          rects: Sequence[Sequence[float]] | None = None,
                          default_value: str | bool = "", readonly: bool = False,
                          multiline: bool = False, options: Sequence[str] | None = None,
                          tab_order: int | None = None) -> str:
        """Create a native AcroForm field. Rectangles use displayed-page points.

        Radio fields require ``options`` and one rectangle per option. Other kinds
        accept one or more rectangles, creating multiple physical widgets for the
        same logical field. Duplicate names are rejected before the document changes.
        """
        writer = self._require_open()
        if self._has_xfa:
            raise ValueError("В документе XFA-форма: создание полей AcroForm невозможно.")
        kind = str(kind).strip().lower()
        if kind not in ("text", "checkbox", "radio", "choice"):
            raise ValueError("Тип поля должен быть text, checkbox, radio или choice.")
        name = self._form_name(name)
        page = self._page_index(page_index)
        raw_rects = list(rects or ([] if rect is None else [rect]))
        option_values = [str(item) for item in (options or [])]
        if not raw_rects and kind != "radio":
            raise ValueError("Укажите хотя бы один прямоугольник поля.")
        if kind == "radio":
            if len(option_values) < 2:
                raise ValueError("У группы переключателей должно быть не меньше двух вариантов.")
            if len(set(option_values)) != len(option_values):
                raise ValueError("Варианты группы переключателей должны быть уникальными.")
            if len(raw_rects) != len(option_values):
                raise ValueError("Для каждого варианта переключателя нужен отдельный прямоугольник.")
        if kind == "choice" and (len(option_values) < 1 or len(set(option_values)) != len(option_values)):
            raise ValueError("Список должен содержать уникальные варианты.")
        initial: Any = default_value
        if kind == "radio":
            initial = str(default_value or "")
            if initial and initial not in option_values:
                raise ValueError("Начальное значение группы отсутствует среди вариантов.")
        elif kind == "choice":
            initial = str(default_value or "")
            if initial and initial not in option_values:
                raise ValueError("Начальное значение отсутствует в списке вариантов.")
        elif kind == "text":
            initial = str(default_value or "")
        normalized = [self._form_rect(value) for value in raw_rects]
        if any(value[0] < 0 or value[1] < 0 or value[0] + value[2] > self.page_size(page)[0] + .01
               or value[1] + value[3] > self.page_size(page)[1] + .01 for value in normalized):
            raise ValueError("Поле должно полностью находиться внутри страницы.")
        acro = self._ensure_acroform()
        fields = acro.get("/Fields")
        if not isinstance(fields, ArrayObject):
            fields = ArrayObject()
            acro[NameObject("/Fields")] = fields
        if any(_qualified_field_name(field) == name for field in self._acro_fields(acro)):
            raise ValueError("Поле с именем «%s» уже существует." % name)
        self._push_undo()
        ft = {"text": "/Tx", "checkbox": "/Btn", "radio": "/Btn", "choice": "/Ch"}[kind]
        flags = (1 if readonly else 0) | ((1 << 12) if multiline and kind == "text" else 0)
        if kind == "radio":
            flags |= _BTN_RADIO
        if kind == "choice":
            flags |= 1 << 17  # combo box
        field = DictionaryObject({
            NameObject("/FT"): NameObject(ft), NameObject("/T"): TextStringObject(name),
            NameObject("/Ff"): NumberObject(flags),
        })
        if kind == "choice":
            field[NameObject("/Opt")] = ArrayObject([TextStringObject(v) for v in option_values])
        if kind == "checkbox":
            initial = self._form_bool(default_value)
            field[NameObject("/V")] = NameObject("/Yes" if initial else "/Off")
        elif kind == "radio":
            field[NameObject("/V")] = NameObject("/Off" if not initial else "/" + initial)
        elif kind == "choice":
            field[NameObject("/V")] = TextStringObject(initial)
        else:
            field[NameObject("/V")] = TextStringObject(initial)
        if kind != "checkbox":
            field[NameObject("/DV")] = NameObject("/Off") if kind == "radio" and not initial else TextStringObject(str(initial))
        kids = ArrayObject()
        field[NameObject("/Kids")] = kids
        field_ref = writer._add_object(field)
        fields.append(field_ref)
        geom = self._geometry(page)
        widgets: list[DictionaryObject] = []
        for position, (x, y, width, height) in enumerate(normalized):
            ux, uy, uw, uh = _display_rect_to_user(geom, x, y, width, height)
            export = option_values[position] if kind == "radio" else "Yes"
            on_name = "/" + re.sub(r"[^A-Za-z0-9_.-]", "_", export).strip("_")
            if on_name == "/":
                on_name = "/Choice%d" % (position + 1)
            widget = DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Parent"): field_ref,
                NameObject("/Rect"): ArrayObject([FloatObject(ux), FloatObject(uy),
                                                    FloatObject(ux + uw), FloatObject(uy + uh)]),
                NameObject("/F"): NumberObject(4),
                NameObject("/MK"): DictionaryObject({NameObject("/BC"): ArrayObject([FloatObject(.18), FloatObject(.28), FloatObject(.32)]),
                                                       NameObject("/BG"): ArrayObject([FloatObject(1), FloatObject(1), FloatObject(1)])}),
                NameObject("/BS"): DictionaryObject({NameObject("/W"): FloatObject(.8), NameObject("/S"): NameObject("/S")}),
                NameObject("/AS"): NameObject("/Off"),
            })
            states = [on_name] if kind in ("checkbox", "radio") else []
            if kind == "radio" and initial == export:
                widget[NameObject("/AS")] = NameObject(on_name)
            elif kind == "checkbox" and initial:
                widget[NameObject("/AS")] = NameObject(on_name)
            self._install_widget_appearance(widget, page, uw, uh, kind, states)
            widget_ref = writer._add_object(widget)
            kids.append(widget_ref)
            annots = self._page_annots_array(writer.pages[page], create=True)
            annots.append(widget_ref)
            widgets.append(widget)
        if kind in ("checkbox", "radio"):
            if kind == "radio" and initial:
                selected = option_values.index(initial)
                field[NameObject("/V")] = NameObject(_widget_on_state(widgets[selected]))
            elif kind == "checkbox" and initial:
                field[NameObject("/V")] = NameObject(_widget_on_state(widgets[0]))
        if tab_order is not None:
            for position, widget in enumerate(widgets):
                self._move_form_widget_ref(page, widget, int(tab_order) + position)
        try:
            writer.set_need_appearances_writer(True)
        except Exception:
            pass
        self._field_map = None
        self._touch()
        key = "f%d:%s" % (self._page_sources[page] if page < len(self._page_sources) else 0, name)
        if kind in ("text", "choice") and initial:
            ref = self._fields().get(key)
            if ref is not None:
                self._apply_text_value(ref, str(initial))
                self._touch()
        return key

    def _move_form_widget_ref(self, page_index: int, widget: DictionaryObject, order: int) -> None:
        page = self._page(page_index)
        annots = self._page_annots_array(page, create=True)
        hit = next((i for i, ref in enumerate(annots)
                    if (ref.get_object() if hasattr(ref, "get_object") else ref) is widget), None)
        if hit is None:
            return
        entry = annots.pop(hit)
        widget_positions = [i for i, ref in enumerate(annots)
                            if str((ref.get_object() if hasattr(ref, "get_object") else ref).get("/Subtype", "")) == "/Widget"]
        insertion = widget_positions[max(0, min(int(order), len(widget_positions)-1))] if widget_positions else len(annots)
        annots.insert(insertion, entry)
        page[NameObject("/Tabs")] = NameObject("/S")

    def update_form_field(self, key: str, *, name: str | None = None,
                          default_value: str | bool | None = None,
                          readonly: bool | None = None, multiline: bool | None = None,
                          options: Sequence[str] | None = None,
                          tab_order: int | None = None,
                          widget_index: int | None = None) -> str:
        """Update logical-field properties while retaining every widget object."""
        self._require_open()
        ref = self._fields().get(str(key))
        if ref is None:
            raise ValueError("Поле формы не найдено: %s" % key)
        target_name = self._form_name(name) if name is not None else ref.partial_name
        if target_name != ref.partial_name and any(r.key != ref.key and r.qualified_name == target_name for r in self._fields().values()):
            raise ValueError("Поле с именем «%s» уже существует." % target_name)
        option_values = [str(v) for v in options] if options is not None else None
        if option_values is not None and (ref.kind not in ("choice",) and self._widget_kind(ref) != "radio"):
            raise ValueError("Варианты поддерживаются только для списка или группы переключателей.")
        if option_values is not None and (not option_values or len(set(option_values)) != len(option_values)):
            raise ValueError("Варианты должны быть непустыми и уникальными.")
        if option_values is not None and self._widget_kind(ref) == "radio" and len(option_values) != len(ref.widgets):
            raise ValueError("Для каждого виджета группы переключателей нужен вариант.")
        tab_target: tuple[int, DictionaryObject] | None = None
        if tab_order is not None:
            try:
                tab_index = int(widget_index) if widget_index is not None else 0
                if tab_index < 0:
                    raise ValueError
                tab_target = ref.widgets[tab_index]
                tab_order = int(tab_order)
            except (TypeError, ValueError, IndexError):
                raise ValueError("Не удалось определить виджет или порядок перехода.") from None
        self._push_undo()
        if name is not None:
            ref.field[NameObject("/T")] = TextStringObject(target_name)
        flags = self._field_flags(ref.field)
        if readonly is not None:
            flags = flags | 1 if readonly else flags & ~1
        if multiline is not None and ref.ft == "/Tx":
            flags = flags | (1 << 12) if multiline else flags & ~(1 << 12)
        ref.field[NameObject("/Ff")] = NumberObject(flags)
        if option_values is not None:
            ref.field[NameObject("/Opt")] = ArrayObject([TextStringObject(v) for v in option_values])
        if default_value is not None:
            if ref.kind == "checkbox":
                value = self._form_bool(default_value)
                on = ref.on_state() or "/Yes"
                ref.field[NameObject("/DV")] = NameObject(on if value else "/Off")
                self._apply_checkbox(ref, value)
            elif self._widget_kind(ref) == "radio":
                ref.field[NameObject("/DV")] = TextStringObject(str(default_value))
                self._apply_radio(ref, str(default_value))
            else:
                text = str(default_value)
                ref.field[NameObject("/DV")] = TextStringObject(text)
                self._apply_text_value(ref, text)
        if tab_target is not None:
            self._move_form_widget_ref(tab_target[0], tab_target[1], tab_order)
        self._field_map = None
        self._touch()
        return "f%d:%s" % (ref.source_index, target_name)

    def set_form_widget_rect(self, key: str, widget_index: int,
                             rect: Sequence[float]) -> None:
        """Move or resize a physical widget without replacing its PDF object."""
        ref = self._fields().get(str(key))
        if ref is None:
            raise ValueError("Поле формы не найдено: %s" % key)
        try:
            page_index, widget = ref.widgets[int(widget_index)]
        except (IndexError, ValueError, TypeError):
            raise ValueError("Виджет поля не найден.") from None
        x, y, width, height = self._form_rect(rect)
        page_width, page_height = self.page_size(page_index)
        if x < 0 or y < 0 or x + width > page_width + .01 or y + height > page_height + .01:
            raise ValueError("Поле должно полностью находиться внутри страницы.")
        self._push_undo()
        geom = self._geometry(page_index)
        ux, uy, uw, uh = _display_rect_to_user(geom, x, y, width, height)
        widget[NameObject("/Rect")] = ArrayObject([FloatObject(ux), FloatObject(uy), FloatObject(ux+uw), FloatObject(uy+uh)])
        self._install_widget_appearance(widget, page_index, uw, uh, self._widget_kind(ref), _widget_on_states(widget))
        self._field_map = None
        self._touch()

    def set_form_tab_order(self, key: str, widget_index: int, order: int) -> None:
        ref = self._fields().get(str(key))
        if ref is None:
            raise ValueError("Поле формы не найдено: %s" % key)
        page_index, widget = ref.widgets[int(widget_index)]
        self._push_undo()
        self._move_form_widget_ref(page_index, widget, int(order))
        self._touch()

    def add_form_widget(self, key: str, page_index: int, rect: Sequence[float],
                        *, export_value: str = "Yes", tab_order: int | None = None) -> int:
        """Add a physical widget to an existing checkbox or radio field."""
        ref = self._fields().get(str(key))
        if ref is None or self._widget_kind(ref) not in ("checkbox", "radio"):
            raise ValueError("Дополнительные виджеты поддерживаются для флажков и переключателей.")
        page = self._page_index(page_index)
        x, y, width, height = self._form_rect(rect)
        if x < 0 or y < 0 or x+width > self.page_size(page)[0] + .01 or y+height > self.page_size(page)[1] + .01:
            raise ValueError("Поле должно полностью находиться внутри страницы.")
        self._push_undo()
        geom = self._geometry(page)
        ux, uy, uw, uh = _display_rect_to_user(geom, x, y, width, height)
        export = str(export_value or "Yes")
        state = "/" + re.sub(r"[^A-Za-z0-9_.-]", "_", export).strip("_")
        if state == "/":
            state = "/Yes"
        widget = DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
                                   NameObject("/Subtype"): NameObject("/Widget"),
                                   NameObject("/Parent"): self._writer.get_reference(ref.field),
                                   NameObject("/Rect"): ArrayObject([FloatObject(ux), FloatObject(uy), FloatObject(ux+uw), FloatObject(uy+uh)]),
                                   NameObject("/F"): NumberObject(4),
                                   NameObject("/AS"): NameObject("/Off")})
        self._install_widget_appearance(widget, page, uw, uh, self._widget_kind(ref), [state])
        writer = self._require_open()
        ref.field[NameObject("/Kids")].append(writer._add_object(widget))
        annots = self._page_annots_array(writer.pages[page], create=True)
        annots.append(writer.get_reference(widget))
        if self._widget_kind(ref) == "radio":
            old = list(ref.field.get("/Opt", []))
            old.append(TextStringObject(export))
            ref.field[NameObject("/Opt")] = ArrayObject(old)
        if tab_order is not None:
            self._move_form_widget_ref(page, widget, int(tab_order))
        self._field_map = None
        self._touch()
        return len(ref.widgets)

    def delete_form_field(self, key: str, widget_index: int | None = None) -> None:
        """Delete a whole logical field or just one of its physical widgets."""
        writer = self._require_open()
        ref = self._fields().get(str(key))
        if ref is None:
            raise ValueError("Поле формы не найдено: %s" % key)
        if widget_index is not None and len(ref.widgets) > 1:
            try:
                selected = [ref.widgets[int(widget_index)]]
            except (IndexError, ValueError, TypeError):
                raise ValueError("Виджет поля не найден.") from None
        else:
            selected = list(ref.widgets)
        self._push_undo()
        for page_index, widget in selected:
            annots = self._page_annots_array(writer.pages[page_index], create=False)
            if annots is not None:
                for position in range(len(annots)-1, -1, -1):
                    obj = annots[position].get_object() if hasattr(annots[position], "get_object") else annots[position]
                    if obj is widget:
                        del annots[position]
            kids = ref.field.get("/Kids")
            if isinstance(kids, ArrayObject):
                for position in range(len(kids)-1, -1, -1):
                    obj = kids[position].get_object() if hasattr(kids[position], "get_object") else kids[position]
                    if obj is widget:
                        del kids[position]
        if widget_index is None or len(ref.widgets) == len(selected):
            fields = self._ensure_acroform()["/Fields"]
            for position in range(len(fields)-1, -1, -1):
                obj = fields[position].get_object() if hasattr(fields[position], "get_object") else fields[position]
                if obj is ref.field:
                    del fields[position]
        elif self._widget_kind(ref) == "radio":
            options = list(ref.field.get("/Opt", []))
            index = int(widget_index)
            if index < len(options):
                del options[index]
                ref.field[NameObject("/Opt")] = ArrayObject(options)
        self._field_map = None
        self._touch()

    # -- сохранение ---------------------------------------------------------
    def save(self, path: str) -> None:
        """Сохранить документ в НОВЫЙ файл (атомарно, исходники не меняются)."""
        self._require_open()
        target = self._validate_target(path)
        if self.page_count == 0:
            raise ValueError("В документе нет страниц — сохранять нечего.")
        try:
            self._attach_annotations()
            self._attach_text_layer()
            data = self._write_bytes()
        finally:
            self._detach_text_layer()
            self._detach_annotations()
        if not data.startswith(b"%PDF"):
            raise RuntimeError("Внутренняя ошибка: собранный файл не является PDF.")
        data = self._protect_serialized_bytes(data)
        if not data.startswith(b"%PDF"):
            raise RuntimeError("Внутренняя ошибка: защищённый файл не является PDF.")
        folder = os.path.dirname(target) or "."
        handle = None
        temp_path = None
        try:
            descriptor, temp_path = tempfile.mkstemp(prefix=".pdfdesk_", suffix=".tmp", dir=folder)
            handle = os.fdopen(descriptor, "wb")
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temp_path, target)
            temp_path = None
        except PermissionError as exc:
            raise RuntimeError("Нет прав на запись в «%s»: %s" % (target, exc)) from exc
        except OSError as exc:
            raise RuntimeError("Не удалось сохранить файл «%s»: %s" % (target, exc)) from exc
        finally:
            if handle is not None:  # pragma: no cover
                try:
                    handle.close()
                except Exception:
                    pass
            if temp_path and os.path.exists(temp_path):  # pragma: no cover
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        self._saved_revision = self._revision
        self._saved_path = target
        self._doc_revision += 1  # в дерево добавились объекты аннотаций
        self._serialized = None
        self._field_map = None
        self._field_map_revision = -1

    def _validate_target(self, path: Any) -> str:
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Не указан путь для сохранения.")
        target = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(target):
            raise ValueError("Указан каталог, а не файл: %s" % target)
        folder = os.path.dirname(target) or "."
        if not os.path.isdir(folder):
            raise ValueError("Папка для сохранения не найдена: %s" % folder)
        normalized = os.path.normcase(target)
        for source in self._source_paths:
            if os.path.normcase(os.path.abspath(source)) == normalized:
                raise ValueError(
                    "Нельзя перезаписать исходный файл «%s»: сохраните копию под другим именем."
                    % source)
        if os.path.exists(target) and not os.access(target, os.W_OK):
            raise RuntimeError("Файл «%s» защищён от записи." % target)
        return target

    # -- завершение ---------------------------------------------------------
    def close(self) -> None:
        """Закрыть документ и освободить ресурсы PDFium."""
        self._close_pdfium()
        self._writer = None
        self._sources = []
        self._source_paths = []
        self._page_sources = []
        self._view_page_ids = []
        self._annots = []
        self._undo = []
        self._redo = []
        self._undo_bytes = 0
        self._last_edit_key = None
        self._serialized = None
        self._serialized_revision = -1
        self._field_map = None
        self._field_map_revision = -1
        self._font_refs: dict[str, tuple[_EmbeddedFont, IndirectObject]] = {}
        self._font_holders: list[DictionaryObject] = []
        self._warnings = []
        self._has_xfa = False
        self._initial_need_appearances = False
        self._refreshed_form_appearances = set()
        self._attached = []
        self._layer_cache = {}
        self._layer_restore = []
        self._keepalive = []
        self._revision = 0
        self._doc_revision = 0
        self._saved_revision = 0
        self._saved_path = None
        self._security_info = self._plain_security_info()
        self._security_open_password = ""
        self._security_owner_password = ""
        self._security_encryption_required = False
        self._security_source_data = None
        self._security_preserve_encryption = False
