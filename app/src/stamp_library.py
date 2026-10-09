# -*- coding: utf-8 -*-
"""PdfDesk — постоянная библиотека пользовательских штампов (изображений).

Модуль не зависит от Qt: файловая система, Pillow и стандартная библиотека.
Интерфейс выбора живёт отдельно — :mod:`src.stamp_library_ui`.

Хранилище (по умолчанию — локальные данные приложения, переопределяется
переменной окружения ``PDFDESK_STAMP_DIR``)::

    <каталог библиотеки>/
        manifest.json           атомарный JSON-манифест (version 1)
        images/<id>.png|.jpg    ТОЛЬКО собственные копии изображений

Правила, заложенные в код:

* импортируются только РЕАЛЬНЫЕ изображения PNG/JPEG (проверяется содержимое
  файла, а не расширение); альфа-канал сохраняется (копия байт-в-байт);
* исходный файл пользователя только читается — библиотека работает со своей
  копией, поэтому изменение/удаление источника штамп не ломает;
* идентификаторы уникальны (``uuid4().hex``, 32 hex-символа), имена очищаются
  от разделителей пути; повторный импорт того же содержимого не создаёт
  дубликатов и не плодит вторые «source path»;
* удаление затрагивает ровно один известный файл внутри ``images/``; выход за
  каталог библиотеки, ссылки и чужие пути отклоняются (path traversal);
* повреждённый манифест не роняет приложение: файл откладывается в
  ``manifest.corrupt-*.json``, а записи восстанавливаются по файлам.

Публичный API (родительское окно)::

    library = StampLibrary()                    # PDFDESK_STAMP_DIR → appdata
    entry = library.import_file(path, name="Печать")
    library.entries() / library.get(id) / library.rename(id, "Новое имя")
    library.delete(id)
    data, name = library.selection(id)          # base64 PNG для set_image_data

Ошибки: :class:`StampLibraryError` (наследник ``ValueError``) с русским текстом.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import uuid
import warnings
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from PIL import Image

__all__ = [
    "STAMP_DIR_ENV",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "IMAGES_DIRNAME",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_IMAGE_SIDE",
    "MAX_NAME_LENGTH",
    "FORMAT_PNG",
    "FORMAT_JPEG",
    "SUPPORTED_FORMATS",
    "StampLibraryError",
    "StampEntry",
    "StampLibrary",
    "default_library_dir",
    "sanitize_stamp_name",
]

# ---------------------------------------------------------------------------
# Константы хранилища
# ---------------------------------------------------------------------------
#: Переменная окружения с каталогом библиотеки (иначе — локальные данные).
STAMP_DIR_ENV = "PDFDESK_STAMP_DIR"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
IMAGES_DIRNAME = "images"
#: Приложение, которому принадлежит манифест (защита от чужих файлов).
MANIFEST_APP = "PdfDesk"

FORMAT_PNG = "PNG"
FORMAT_JPEG = "JPEG"
SUPPORTED_FORMATS: tuple[str, ...] = (FORMAT_PNG, FORMAT_JPEG)
#: Каноническое расширение собственной копии для каждого формата.
FORMAT_EXTENSION: dict[str, str] = {FORMAT_PNG: ".png", FORMAT_JPEG: ".jpg"}
#: Допустимые расширения файлов внутри ``images/``.
EXTENSION_FORMAT: dict[str, str] = {".png": FORMAT_PNG, ".jpg": FORMAT_JPEG,
                                    ".jpeg": FORMAT_JPEG}

#: Лимиты согласованы с backend-ом (``pdf_backend._decode_image_data``):
#: более крупный штамп backend всё равно не сможет встроить в PDF.
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 12_000
#: Максимальная длина имени штампа в интерфейсе.
MAX_NAME_LENGTH = 80

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INVALID_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]+")
_SPACES_RE = re.compile(r"\s+")
FALLBACK_NAME = "Штамп"


class StampLibraryError(ValueError):
    """Ошибка библиотеки штампов (неверные данные, путь или файл)."""


# ---------------------------------------------------------------------------
# Каталог по умолчанию
# ---------------------------------------------------------------------------
def default_library_dir() -> Path:
    """Каталог библиотеки: ``PDFDESK_STAMP_DIR`` или локальные данные системы.

    Windows — ``%LOCALAPPDATA%\\PdfDesk\\Stamps``, macOS — ``Library/Application
    Support``, остальные — ``XDG_DATA_HOME``/``~/.local/share``. Функция только
    вычисляет путь и ничего не создаёт.
    """
    override = os.environ.get(STAMP_DIR_ENV, "").strip()
    if override:
        return Path(os.path.expanduser(override)).absolute()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "PdfDesk" / "Stamps"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PdfDesk" / "Stamps"
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(os.path.expanduser(base)) if base else Path.home() / ".local" / "share"
    return root / "pdfdesk" / "stamps"


def sanitize_stamp_name(value: Any, fallback: str = FALLBACK_NAME) -> str:
    """Очистить имя штампа: без разделителей пути, управляющих символов и «..».

    Имя никогда не используется как путь (файлы называются по id), но
    очищается всегда — так манифест и интерфейс остаются предсказуемыми.
    """
    text = str(value if value is not None else "")
    text = _INVALID_NAME_RE.sub(" ", text)
    text = text.replace("..", " ")
    text = _SPACES_RE.sub(" ", text).strip(" .")
    if len(text) > MAX_NAME_LENGTH:
        text = text[:MAX_NAME_LENGTH].strip()
    return text or fallback


def _has_alpha(image: "Image.Image") -> bool:
    """Есть ли у изображения реальный альфа-канал (или прозрачность палитры)."""
    if image.mode in ("RGBA", "LA", "PA", "RGBa"):
        return True
    if image.mode == "P":
        return "transparency" in image.info
    return False


def _inspect_image(raw: bytes) -> tuple[str, int, int, bool]:
    """Проверить РЕАЛЬНОЕ содержимое картинки: ``(формат, ширина, высота, альфа)``.

    Расширение файла не учитывается: текстовый файл с именем ``.png`` будет
    отклонён, а PNG с именем ``.txt`` — принят. ``Image.verify()`` ловит
    обрезанные и повреждённые файлы.
    """
    if not raw:
        raise StampLibraryError("Файл изображения пуст.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise StampLibraryError(
            "Изображение слишком большое: %d Мбайт (лимит %d Мбайт)."
            % (len(raw) // (1024 * 1024), MAX_IMAGE_BYTES // (1024 * 1024)))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                detected = str(probe.format or "").upper()
                probe.verify()
    except StampLibraryError:
        raise
    except Exception as exc:  # noqa: BLE001 - любые сбои Pillow → понятный текст
        raise StampLibraryError(
            "Не удалось прочитать изображение (нужны PNG, JPEG или JPG): %s" % exc
        ) from exc
    if detected not in SUPPORTED_FORMATS:
        raise StampLibraryError(
            "Формат «%s» не поддерживается: нужны PNG, JPEG или JPG."
            % (detected or "неизвестный"))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = int(image.size[0]), int(image.size[1])
                if width <= 0 or height <= 0:
                    raise StampLibraryError("Изображение имеет нулевой размер.")
                if (width * height > MAX_IMAGE_PIXELS
                        or max(width, height) > MAX_IMAGE_SIDE):
                    raise StampLibraryError(
                        "Изображение слишком большое: %dx%d пикселей (лимит %d Мпикс, "
                        "сторона %d)." % (width, height, MAX_IMAGE_PIXELS // 1_000_000,
                                          MAX_IMAGE_SIDE))
                image.load()
                alpha = _has_alpha(image)
    except StampLibraryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StampLibraryError("Изображение повреждено: %s" % exc) from exc
    return detected, width, height, alpha


def _validate_entry_id(value: Any) -> str:
    """Проверить идентификатор записи; traversal и мусор отклоняются."""
    text = str(value if value is not None else "").strip()
    if not _ID_RE.match(text):
        raise StampLibraryError(
            "Недопустимый идентификатор штампа: %r." % (value,))
    return text


def _validate_filename(entry_id: str, image_format: str, value: Any) -> str:
    """Проверить, что имя файла — ровно собственный файл ``images/<id><ext>``."""
    text = str(value if value is not None else "").replace("\\", "/").strip()
    expected_ext = FORMAT_EXTENSION.get(image_format, "")
    if not expected_ext:
        raise StampLibraryError("Неизвестный формат записи: %r." % (image_format,))
    prefix = IMAGES_DIRNAME + "/"
    if not text.startswith(prefix):
        raise StampLibraryError("Путь записи вне каталога библиотеки: %r." % (value,))
    name = text[len(prefix):]
    if "/" in name or name.startswith("."):
        raise StampLibraryError("Путь записи вне каталога библиотеки: %r." % (value,))
    stem, dot, ext = name.rpartition(".")
    if not dot or stem != entry_id or ("." + ext.lower()) not in EXTENSION_FORMAT:
        raise StampLibraryError(
            "Имя файла записи не совпадает с её идентификатором: %r." % (value,))
    if EXTENSION_FORMAT["." + ext.lower()] != image_format:
        raise StampLibraryError(
            "Расширение файла не соответствует формату записи: %r." % (value,))
    return prefix + name


# ---------------------------------------------------------------------------
# Запись библиотеки
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StampEntry:
    """Один штамп библиотеки (неизменяемое описание записи манифеста)."""

    id: str
    name: str
    filename: str            # "images/<id>.png" — путь относительно каталога
    format: str              # "PNG" | "JPEG"
    width: int
    height: int
    created: str = ""
    source_name: str = ""    # имя исходного файла (для интерфейса/диагностики)
    source_path: str = ""    # нормализованный путь источника (только справка)
    sha256: str = ""
    size_bytes: int = 0
    has_alpha: bool = False

    @property
    def extension(self) -> str:
        return Path(self.filename).suffix.lower()

    @property
    def label(self) -> str:
        """Подпись для интерфейса: имя и размер в пикселях."""
        return "%s · %d×%d" % (self.name, self.width, self.height)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "StampEntry":
        """Разобрать запись манифеста; некорректная запись отклоняется."""
        if not isinstance(payload, dict):
            raise StampLibraryError("Запись манифеста не является объектом.")
        entry_id = _validate_entry_id(payload.get("id"))
        image_format = str(payload.get("format") or "").upper()
        if image_format not in SUPPORTED_FORMATS:
            raise StampLibraryError(
                "Неизвестный формат записи «%s»." % (payload.get("format"),))
        default_filename = "%s/%s%s" % (IMAGES_DIRNAME, entry_id,
                                        FORMAT_EXTENSION[image_format])
        filename = payload.get("filename")
        filename = default_filename if filename in (None, "") else filename
        filename = _validate_filename(entry_id, image_format, filename)
        try:
            width = int(payload.get("width") or 0)
            height = int(payload.get("height") or 0)
            size_bytes = int(payload.get("size_bytes") or 0)
        except (TypeError, ValueError):
            raise StampLibraryError("Размер записи не является числом.") from None
        if width <= 0 or height <= 0:
            raise StampLibraryError("Запись имеет нулевой размер изображения.")
        return cls(
            id=entry_id,
            name=sanitize_stamp_name(payload.get("name"),
                                     fallback="%s %s" % (FALLBACK_NAME, entry_id[:6])),
            filename=filename,
            format=image_format,
            width=width,
            height=height,
            created=str(payload.get("created") or ""),
            source_name=str(payload.get("source_name") or ""),
            source_path=str(payload.get("source_path") or ""),
            sha256=str(payload.get("sha256") or ""),
            size_bytes=size_bytes,
            has_alpha=bool(payload.get("has_alpha")),
        )


# ---------------------------------------------------------------------------
# Библиотека
# ---------------------------------------------------------------------------
class StampLibrary:
    """Постоянная библиотека штампов: импорт, список, переименование, удаление.

    ``root`` можно задать явно (тесты, портативный режим); иначе используется
    :func:`default_library_dir`. Чтение ничего не создаёт: каталоги появляются
    только при первой записи (импорт/переименование/удаление/ремонт манифеста).
    """

    def __init__(self, root: Optional[os.PathLike[str] | str] = None) -> None:
        self.root = Path(root).expanduser().absolute() if root is not None \
            else default_library_dir()
        self.notes: list[str] = []
        self._entries: list[StampEntry] = []
        self._loaded = False
        self._loaded_stamp: Optional[tuple[int, int]] = None
        self._png_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------ хранилище
    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def images_dir(self) -> Path:
        return self.root / IMAGES_DIRNAME

    def exists(self) -> bool:
        """Есть ли уже сохранённая библиотека (манифест или файлы штампов)."""
        return self.manifest_path.is_file() or self.images_dir.is_dir()

    # -------------------------------------------------------------- чтение
    def entries(self) -> list[StampEntry]:
        """Все штампы, отсортированные по имени (авто-перечитывание с диска)."""
        self._ensure_loaded()
        return sorted(self._entries, key=lambda item: (item.name.casefold(),
                                                       item.created, item.id))

    def count(self) -> int:
        return len(self.entries())

    def __len__(self) -> int:
        return self.count()

    def __contains__(self, entry_id: object) -> bool:
        return self.get(entry_id) is not None

    def refresh(self) -> list[StampEntry]:
        """Перечитать манифест с диска (после внешних изменений/перезапуска)."""
        self.notes = []
        reloaded = self._read_manifest()
        self._entries = reloaded
        self._loaded = True
        self._png_cache.clear()
        self._loaded_stamp = self._manifest_stamp()
        return sorted(self._entries, key=lambda item: (item.name.casefold(),
                                                       item.created, item.id))

    def get(self, entry_id: Any) -> Optional[StampEntry]:
        """Запись по идентификатору или ``None`` (мусорный id не бросает ошибку)."""
        try:
            normalized = _validate_entry_id(entry_id)
        except StampLibraryError:
            return None
        self._ensure_loaded()
        return self._by_id(normalized)

    def require(self, entry_id: Any) -> StampEntry:
        """Запись по идентификатору; неизвестный id — понятная ошибка."""
        try:
            normalized = _validate_entry_id(entry_id)
        except StampLibraryError as exc:
            raise StampLibraryError("Штамп не найден: %s" % exc) from exc
        self._ensure_loaded()
        entry = self._by_id(normalized)
        if entry is None:
            raise StampLibraryError("Штамп с идентификатором %s не найден." % normalized)
        return entry

    # -------------------------------------------------------------- импорт
    def import_file(self, path: os.PathLike[str] | str, name: Optional[str] = None,
                    *, reuse: bool = True) -> StampEntry:
        """Импортировать PNG/JPEG/JPG: проверить содержимое и скопировать к себе.

        Исходный файл только читается. При ``reuse=True`` (по умолчанию) то же
        самое изображение (совпадает SHA-256) не копируется повторно — вернётся
        уже существующая запись, поэтому дубликатов путей не появляется.
        """
        source = Path(path).expanduser()
        try:
            if not source.is_file():
                raise StampLibraryError("Файл изображения не найден: %s" % source)
            raw = source.read_bytes()
        except StampLibraryError:
            raise
        except OSError as exc:
            raise StampLibraryError(
                "Не удалось прочитать изображение «%s»: %s" % (source, exc)) from exc
        return self.import_bytes(
            raw,
            name=name if name is not None else source.stem,
            source_name=source.name,
            source_path=os.path.normcase(str(source.resolve())),
            reuse=reuse,
        )

    def import_bytes(self, raw: bytes, name: Optional[str] = None, *,
                     source_name: str = "", source_path: str = "",
                     reuse: bool = True) -> StampEntry:
        """Импортировать изображение из памяти (буфер обмена, тесты, сеть)."""
        data = bytes(raw or b"")
        image_format, width, height, alpha = _inspect_image(data)
        digest = hashlib.sha256(data).hexdigest()
        self._ensure_loaded()
        if reuse:
            for entry in self._entries:
                if entry.sha256 and entry.sha256 == digest:
                    return entry
        entry_id = self._new_id()
        extension = FORMAT_EXTENSION[image_format]
        filename = "%s/%s%s" % (IMAGES_DIRNAME, entry_id, extension)
        target = self.images_dir / ("%s%s" % (entry_id, extension))
        final_name = self._unique_name(
            sanitize_stamp_name(name if name is not None else (source_name or ""),
                                fallback="%s %s" % (FALLBACK_NAME, entry_id[:6])))
        self._ensure_dirs()
        self._atomic_write_bytes(target, data)
        entry = StampEntry(
            id=entry_id,
            name=final_name,
            filename=filename,
            format=image_format,
            width=width,
            height=height,
            created=_dt.datetime.now(_dt.timezone.utc).replace(
                microsecond=0).isoformat(),
            source_name=str(source_name or ""),
            source_path=str(source_path or ""),
            sha256=digest,
            size_bytes=len(data),
            has_alpha=alpha,
        )
        self._entries.append(entry)
        try:
            self._save_manifest()
        except StampLibraryError:
            # Манифест не записался — не оставляем «висячую» запись и файл.
            self._entries = [item for item in self._entries if item.id != entry_id]
            try:
                os.remove(target)
            except OSError:
                pass
            raise
        self.notes.append("Штамп «%s» добавлен в библиотеку." % entry.name)
        return entry

    def import_many(self, paths: Iterable[os.PathLike[str] | str], *,
                    reuse: bool = True) -> tuple[list[StampEntry], list[tuple[str, str]]]:
        """Импортировать несколько файлов: ``(добавленные, [(путь, ошибка)])``."""
        imported: list[StampEntry] = []
        errors: list[tuple[str, str]] = []
        for path in paths:
            try:
                imported.append(self.import_file(path, reuse=reuse))
            except StampLibraryError as exc:
                errors.append((str(path), str(exc)))
        return imported, errors

    # ------------------------------------------------- переименование/удаление
    def rename(self, entry_id: Any, new_name: str) -> StampEntry:
        """Переименовать запись; пустое/«путевое»/занятое имя отклоняется."""
        entry = self.require(entry_id)
        text = str(new_name if new_name is not None else "").strip()
        if not text:
            raise StampLibraryError("Имя штампа не может быть пустым.")
        if _INVALID_NAME_RE.search(text) or ".." in text:
            raise StampLibraryError(
                "Недопустимое имя штампа: нельзя использовать разделители пути "
                "и «..».")
        cleaned = sanitize_stamp_name(text, fallback="")
        if not cleaned:
            raise StampLibraryError("Имя штампа не может быть пустым.")
        if len(text) > MAX_NAME_LENGTH:
            raise StampLibraryError(
                "Имя штампа длиннее %d символов." % MAX_NAME_LENGTH)
        taken = {item.name.casefold() for item in self._entries if item.id != entry.id}
        if cleaned.casefold() in taken:
            raise StampLibraryError("Штамп с именем «%s» уже есть в библиотеке." % cleaned)
        updated = replace(entry, name=cleaned)
        self._entries = [updated if item.id == entry.id else item
                         for item in self._entries]
        try:
            self._save_manifest()
        except StampLibraryError:
            self._entries = [entry if item.id == entry.id else item
                             for item in self._entries]
            raise
        self.notes.append("Штамп переименован: «%s»." % cleaned)
        return updated

    def delete(self, entry_id: Any) -> bool:
        """Удалить запись и ровно её собственный файл внутри ``images/``.

        Возвращает ``False`` для неизвестного (но корректного) идентификатора.
        Мусорный/«путевой» id, ссылка или путь вне библиотеки — ошибка, при
        которой НИЧЕГО не удаляется.
        """
        normalized = _validate_entry_id(entry_id)
        self._ensure_loaded()
        entry = self._by_id(normalized)
        if entry is None:
            return False
        target = self._owned_image_path(entry)
        if target.exists():
            try:
                os.remove(target)
            except OSError as exc:
                raise StampLibraryError(
                    "Не удалось удалить файл штампа «%s»: %s" % (entry.name, exc)) from exc
        self._entries = [item for item in self._entries if item.id != normalized]
        self._png_cache.pop(normalized, None)
        self._save_manifest()
        self.notes.append("Штамп «%s» удалён из библиотеки." % entry.name)
        return True

    def clear(self) -> int:
        """Удалить все штампы библиотеки (только собственные файлы ``images/``)."""
        removed = 0
        for entry in list(self.entries()):
            if self.delete(entry.id):
                removed += 1
        return removed

    # ------------------------------------------------- файлы и данные картинки
    def image_path(self, entry_id: Any) -> Path:
        """Путь к собственной копии изображения (проверяется принадлежность)."""
        entry = self.require(entry_id)
        target = self._owned_image_path(entry)
        if not target.is_file():
            raise StampLibraryError("Файл штампа «%s» не найден в библиотеке." % entry.name)
        return target

    def image_bytes(self, entry_id: Any) -> bytes:
        """Байты сохранённой копии (формат исходного файла, альфа сохранена)."""
        path = self.image_path(entry_id)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StampLibraryError("Не удалось прочитать файл штампа: %s" % exc) from exc

    def png_bytes(self, entry_id: Any) -> bytes:
        """PNG для размещения в PDF: альфа сохраняется, если она есть."""
        entry = self.require(entry_id)
        cached = self._png_cache.get(entry.id)
        if cached is not None:
            return cached
        path = self.image_path(entry.id)
        try:
            with Image.open(path) as image:
                image.load()
                converted = image.convert("RGBA" if _has_alpha(image) else "RGB")
                buffer = io.BytesIO()
                converted.save(buffer, format="PNG")
        except StampLibraryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StampLibraryError(
                "Не удалось подготовить PNG штампа «%s»: %s" % (entry.name, exc)) from exc
        data = buffer.getvalue()
        if len(self._png_cache) >= 64:
            self._png_cache.pop(next(iter(self._png_cache)))
        self._png_cache[entry.id] = data
        return data

    def png_base64(self, entry_id: Any) -> str:
        """base64 PNG — тот же формат, что принимает ``set_image_data``."""
        return base64.b64encode(self.png_bytes(entry_id)).decode("ascii")

    def selection(self, entry_id: Any) -> tuple[str, str]:
        """``(base64 PNG, имя)`` для размещения штампа в документе."""
        entry = self.require(entry_id)
        return self.png_base64(entry.id), entry.name

    def find_by_source(self, path: os.PathLike[str] | str) -> Optional[StampEntry]:
        """Найти запись по исходному пути (нормализованное сравнение)."""
        try:
            key = os.path.normcase(str(Path(path).expanduser().resolve()))
        except OSError:
            return None
        self._ensure_loaded()
        for entry in self._entries:
            if entry.source_path and os.path.normcase(entry.source_path) == key:
                return entry
        return None

    # ------------------------------------------------------- служебное чтение
    def _ensure_loaded(self) -> None:
        if not self._loaded or self._manifest_stamp() != self._loaded_stamp:
            self.refresh()

    def _manifest_stamp(self) -> Optional[tuple[int, int]]:
        try:
            info = self.manifest_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _by_id(self, entry_id: str) -> Optional[StampEntry]:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def _new_id(self) -> str:
        existing = {entry.id for entry in self._entries}
        while True:
            candidate = uuid.uuid4().hex
            if candidate not in existing:
                return candidate

    def _unique_name(self, base: str) -> str:
        taken = {entry.name.casefold() for entry in self._entries}
        if base.casefold() not in taken:
            return base
        index = 2
        while True:
            suffix = " (%d)" % index
            candidate = base[:MAX_NAME_LENGTH - len(suffix)].strip() + suffix
            if candidate.casefold() not in taken:
                return candidate
            index += 1

    def _owned_image_path(self, entry: StampEntry) -> Path:
        """Абсолютный путь ТОЛЬКО собственного файла записи внутри ``images/``."""
        filename = _validate_filename(entry.id, entry.format, entry.filename)
        candidate = self.root / filename
        if candidate.is_symlink():
            raise StampLibraryError(
                "Файл штампа «%s» является ссылкой — операция отменена." % entry.name)
        root = self.images_dir.resolve()
        resolved = candidate.resolve()
        if resolved.parent != root:
            raise StampLibraryError(
                "Путь штампа вне каталога библиотеки — операция отменена.")
        return resolved

    # ------------------------------------------------------- запись на диск
    def _ensure_dirs(self) -> None:
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StampLibraryError(
                "Не удалось создать каталог библиотеки «%s»: %s" % (self.root, exc)) from exc

    def _atomic_write_bytes(self, target: Path, data: bytes) -> None:
        """Записать файл через временный и ``os.replace`` (частичных файлов нет)."""
        self._ensure_dirs()
        handle = None
        temp_path = None
        try:
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".stamp-", suffix=".tmp", dir=str(target.parent))
            handle = os.fdopen(descriptor, "wb")
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temp_path, target)
            temp_path = None
        except OSError as exc:
            raise StampLibraryError(
                "Не удалось сохранить файл штампа «%s»: %s" % (target.name, exc)) from exc
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - защитная уборка
                    pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:  # pragma: no cover - защитная уборка
                    pass

    def _write_manifest(self, entries: Sequence[StampEntry]) -> None:
        payload = {
            "app": MANIFEST_APP,
            "version": MANIFEST_VERSION,
            "saved": _dt.datetime.now(_dt.timezone.utc).replace(
                microsecond=0).isoformat(),
            "stamps": [entry.to_dict() for entry in entries],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write_bytes(self.manifest_path, text.encode("utf-8"))

    def _save_manifest(self) -> None:
        self._write_manifest(self._entries)
        self._loaded = True
        self._loaded_stamp = self._manifest_stamp()

    # ------------------------------------------------------- чтение/ремонт
    def _read_manifest(self) -> list[StampEntry]:
        path = self.manifest_path
        if not path.exists():
            if self.images_dir.is_dir() and any(self.images_dir.iterdir()):
                self.notes.append(
                    "Манифест библиотеки отсутствовал — записи восстановлены по файлам.")
                return self._rebuild_from_files()
            return []
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._quarantine_manifest("файл не читается (%s)" % exc)
            return self._rebuild_from_files()
        if not isinstance(payload, dict) or not isinstance(payload.get("stamps"), list):
            if self._is_foreign_manifest(payload):
                self._quarantine_manifest("манифест принадлежит другому приложению",
                                          kind="foreign")
            else:
                self._quarantine_manifest("неожиданная структура JSON")
            return self._rebuild_from_files()
        try:
            version = int(payload.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version > MANIFEST_VERSION:
            self._quarantine_manifest("версия манифеста %s новее поддерживаемой" % version)
            return self._rebuild_from_files()
        entries: list[StampEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(payload["stamps"]):
            try:
                entry = StampEntry.from_dict(item)
            except StampLibraryError as exc:
                self.notes.append("Запись №%d пропущена: %s" % (index + 1, exc))
                continue
            if entry.id in seen:
                self.notes.append("Запись №%d пропущена: повтор идентификатора." % (index + 1))
                continue
            if not self._entry_file_ok(entry):
                self.notes.append(
                    "Запись «%s» пропущена: файл изображения не найден." % entry.name)
                continue
            seen.add(entry.id)
            entries.append(entry)
        return entries

    def _entry_file_ok(self, entry: StampEntry) -> bool:
        try:
            return self._owned_image_path(entry).is_file()
        except StampLibraryError:
            return False

    @staticmethod
    def _is_foreign_manifest(payload: Any) -> bool:
        """Чужой manifest.json в каталоге библиотеки (не перезаписываем молча)."""
        if not isinstance(payload, dict):
            return False
        app = payload.get("app")
        return isinstance(app, str) and bool(app.strip()) and app != MANIFEST_APP

    def _quarantine_manifest(self, reason: str, *, kind: str = "corrupt") -> None:
        """Убрать неподходящий манифест в сторону, не потеряв данные."""
        target = self.root / ("manifest.%s-%s-%s.json" % (
            kind, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]))
        if kind == "foreign":
            message = ("Файл %s принадлежит другому приложению (%s) — он сохранён "
                       "рядом, записи восстановлены по файлам." % (MANIFEST_NAME, reason))
        else:
            message = ("Манифест библиотеки повреждён (%s) — записи восстановлены "
                       "по файлам." % reason)
        try:
            os.replace(self.manifest_path, target)
            message += " Копия: %s" % target.name
        except OSError as exc:
            message += " Не удалось отложить файл: %s" % exc
        self.notes.append(message)

    def _rebuild_from_files(self) -> list[StampEntry]:
        """Восстановить записи по собственным файлам ``images/`` (файлы не трогаем)."""
        entries: list[StampEntry] = []
        if not self.images_dir.is_dir():
            return entries
        for candidate in sorted(self.images_dir.iterdir(), key=lambda item: item.name):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            entry_id = candidate.stem
            if not _ID_RE.match(entry_id):
                continue
            extension = candidate.suffix.lower()
            expected_format = EXTENSION_FORMAT.get(extension)
            if expected_format is None:
                continue
            try:
                raw = candidate.read_bytes()
                image_format, width, height, alpha = _inspect_image(raw)
            except (OSError, StampLibraryError) as exc:
                self.notes.append("Файл «%s» пропущен при восстановлении: %s"
                                  % (candidate.name, exc))
                continue
            if image_format != expected_format:
                self.notes.append(
                    "Файл «%s» пропущен при восстановлении: расширение не "
                    "соответствует содержимому (%s)." % (candidate.name, image_format))
                continue
            entries.append(StampEntry(
                id=entry_id,
                name="%s %s" % (FALLBACK_NAME, entry_id[:6]),
                filename="%s/%s" % (IMAGES_DIRNAME, candidate.name),
                format=image_format,
                width=width,
                height=height,
                created=_dt.datetime.fromtimestamp(
                    candidate.stat().st_mtime, _dt.timezone.utc).replace(
                        microsecond=0).isoformat(),
                source_name="",
                source_path="",
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                has_alpha=alpha,
            ))
        if entries:
            try:
                self._write_manifest(entries)
            except StampLibraryError as exc:
                self.notes.append("Восстановленный манифест не сохранён: %s" % exc)
        return entries

    # ------------------------------------------------------------- диагностика
    def describe(self) -> dict[str, Any]:
        """Краткая сводка для строки состояния/диагностики."""
        entries = self.entries()
        return {
            "root": str(self.root),
            "count": len(entries),
            "names": [entry.name for entry in entries],
            "notes": list(self.notes),
        }
