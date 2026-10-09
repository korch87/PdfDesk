# -*- coding: utf-8 -*-
"""Автоматическое временное восстановление PdfDesk (temporary snapshots).

Модуль хранит снимки открытого документа в ОТДЕЛЬНОЙ служебной папке
(``QStandardPaths::AppLocalDataLocation/recovery``) и никогда не пишет в
исходные PDF-файлы: источник только читается. Каждый документ получает свой
ключ (по первому исходнику), поэтому снимки разных документов не смешиваются,
а внутри своего каталога модуль не сканирует и не удаляет ничего лишнего.

Оценка serialize-методов backend (``app/src/pdf_backend.py`` — только чтение):

* ``PdfSession.save(path)`` пишет файл атомарно, но помечает документ
  сохранённым (``_saved_revision``, ``_saved_path``), увеличивает
  ``_doc_revision`` и сбрасывает кэши. Для временного снимка это недопустимые
  побочные эффекты: пользователь ничего не сохранял, документ обязан остаться
  «грязным», а сохранённый путь — прежним.
* ``PdfSession._serialize()`` / ``_write_bytes()`` только собирают байты;
  заметки и невидимый текстовый слой попадают в файл лишь после
  ``_attach_annotations()`` / ``_attach_text_layer()``.
* Поэтому снимок повторяет внутренний порядок ``save()`` вручную:
  ``attach`` → ``write`` → ``finally detach``, а изменённые атрибуты состояния
  восстанавливаются (:func:`preserved_session_state`) — «mark clean» не
  выполняется.

Сеть не используется. Все операции с файлами ограничены каталогом
восстановления; запись атомарная (temp + ``os.replace``), поэтому предыдущая
исправная копия не теряется.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

#: Имя каталога восстановления внутри AppLocalDataLocation.
RECOVERY_DIR_NAME = "recovery"
#: Имя env-переменной, которой можно переопределить каталог (удобно для проверок).
RECOVERY_DIR_ENV = "PDFDESK_RECOVERY_DIR"
#: Период авто-снимков по умолчанию — 3 минуты.
DEFAULT_INTERVAL_MS = 3 * 60 * 1000

DOCUMENT_NAME = "document.pdf"
METADATA_NAME = "metadata.json"
FORMAT_VERSION = 1
#: Потолок хеширования исходника (большие файлы только измеряются).
MAX_HASH_BYTES = 64 * 1024 * 1024

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_MISSING = object()

#: Атрибуты сессии, которые меняются при сохранении; снимок их восстанавливает.
_STATE_ATTRS = (
    "_revision",
    "_doc_revision",
    "_saved_revision",
    "_saved_path",
    "_serialized",
    "_serialized_revision",
    "_field_map",
    "_field_map_revision",
)

#: Методы backend, без которых снимок невозможен (чужие/тестовые сессии).
_REQUIRED_METHODS = (
    "_attach_annotations",
    "_attach_text_layer",
    "_write_bytes",
    "_detach_text_layer",
    "_detach_annotations",
)


class RecoveryError(RuntimeError):
    """Снимок восстановления создать не удалось."""


class RecoveryUnsupported(RecoveryError):
    """Сессия не умеет отдавать байты PDF (например, тестовая заглушка)."""


# ---------------------------------------------------------------------------
# Каталог восстановления
# ---------------------------------------------------------------------------


def default_recovery_root() -> Path:
    """Каталог восстановления по умолчанию (отдельно от исходных файлов)."""
    override = os.environ.get(RECOVERY_DIR_ENV, "").strip()
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    base = ""
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
    except Exception:  # pragma: no cover - Qt недоступен только в экзотике
        base = ""
    if not base:
        base = os.path.join(tempfile.gettempdir(), "PdfDesk")
    return Path(base) / RECOVERY_DIR_NAME


def is_inside_root(root: Path | str, path: Path | str) -> bool:
    """True, если путь лежит внутри каталога восстановления (или совпадает с ним)."""
    root_abs = os.path.normcase(os.path.abspath(str(root)))
    path_abs = os.path.normcase(os.path.abspath(str(path)))
    if path_abs == root_abs:
        return True
    return path_abs.startswith(root_abs.rstrip("\\/") + os.sep)


# ---------------------------------------------------------------------------
# Ключ документа
# ---------------------------------------------------------------------------


def _as_paths(source_paths: Any) -> list[str]:
    if source_paths is None:
        return []
    if isinstance(source_paths, (str, os.PathLike)):
        candidates: Sequence[Any] = [source_paths]
    elif hasattr(source_paths, "source_paths"):  # сессия PdfSession
        candidates = list(getattr(source_paths, "source_paths") or [])
    else:
        candidates = list(source_paths)
    result: list[str] = []
    for item in candidates:
        if isinstance(item, os.PathLike):
            item = os.fspath(item)
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def document_key(source_paths: Any) -> str:
    """Ключ документа: по первому исходнику и его «отпечатку» на диске.

    Ключ устойчив при добавлении страниц (первый исходник не меняется) и
    меняется, если по тому же пути лежит уже другой файл.
    """
    paths = _as_paths(source_paths)
    if not paths:
        raise RecoveryError("Не удалось определить документ: нет исходных путей.")
    first = os.path.normcase(os.path.abspath(paths[0]))
    identity = ""
    try:
        stat = os.stat(paths[0])
        identity = "%d:%d" % (stat.st_size, stat.st_mtime_ns)
    except OSError:
        identity = ""
    digest = hashlib.sha256(("%s|%s" % (first, identity)).encode("utf-8", "surrogatepass"))
    return digest.hexdigest()[:32]


def file_sha256(path: Path | str, limit: int = MAX_HASH_BYTES) -> Optional[str]:
    """SHA-256 исходного файла (None, если файл недоступен или слишком велик)."""
    try:
        if os.path.getsize(str(path)) > limit:
            return None
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Снимок: сериализация без побочных эффектов
# ---------------------------------------------------------------------------


@contextmanager
def preserved_session_state(session: Any) -> Iterator[None]:
    """Сохраняет и возвращает атрибуты сессии, которые меняет save().

    Нужен, чтобы временный снимок не помечал документ сохранённым и не менял
    ``_saved_path``: пользователь ничего не сохранял.
    """
    saved: dict[str, Any] = {}
    for name in _STATE_ATTRS:
        saved[name] = getattr(session, name, _MISSING)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                continue
            try:
                setattr(session, name, value)
            except Exception:  # pragma: no cover - защита от нестандартных сессий
                pass


def serialize_session(session: Any) -> bytes:
    """Байты PDF со всеми поддерживаемыми заметками и формами.

    Повторяет внутренний порядок ``PdfSession.save`` (attach → write →
    finally detach), но НЕ помечает документ сохранённым.
    """
    for name in _REQUIRED_METHODS:
        if not callable(getattr(session, name, None)):
            raise RecoveryUnsupported(
                "Сессия не поддерживает временные снимки (нет метода %s)." % name
            )
    with preserved_session_state(session):
        try:
            try:
                session._attach_annotations()
                session._attach_text_layer()
                data = session._write_bytes()
            finally:
                try:
                    session._detach_text_layer()
                finally:
                    session._detach_annotations()
        except Exception as exc:
            raise RecoveryError(
                "Не удалось собрать PDF для временного снимка: %s" % exc
            ) from exc
    if not data or not data.startswith(b"%PDF"):
        raise RecoveryError("Собранный снимок не является PDF.")
    # Keep protected documents protected in recovery. The live session applies
    # the same save policy, while credentials stay only in its memory and never
    # enter recovery metadata or a serialized history snapshot.
    protect = getattr(session, "_protect_serialized_bytes", None)
    if callable(protect):
        try:
            data = protect(data)
        except Exception as exc:
            raise RecoveryError("Не удалось сохранить защиту временного снимка: %s" % exc) from exc
    return data


def _model_counts(session: Any) -> tuple[Optional[int], Optional[int]]:
    """Количество заметок и полей формы в модели (только чтение)."""
    try:
        pages = int(getattr(session, "page_count", 0) or 0)
    except Exception:
        return None, None
    annotations: Optional[int] = 0
    try:
        for index in range(pages):
            annotations += len(session.get_annotations(index) or [])
    except Exception:
        annotations = None
    try:
        forms: Optional[int] = len(session.form_fields(None) or [])
    except Exception:
        forms = None
    return annotations, forms


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Атомарно заменить файл: сначала temp рядом, затем os.replace."""
    folder = str(path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=folder
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            temp.unlink()
        except OSError:  # pragma: no cover - temp мог не создаться
            pass
        raise


def snapshot(
    session: Any,
    *,
    root: Path | str,
    key: Optional[str] = None,
    source_paths: Any = None,
    saved_path: Optional[str] = None,
    copy_path: Optional[str] = None,
    revision: Optional[int] = None,
    now: Optional[float] = None,
    extra: Optional[dict] = None,
) -> "RecoveryRecord":
    """Записать временный снимок документа и метаданные (атомарно).

    Предыдущая исправная копия не теряется: байты сначала собираются в памяти,
    затем пишутся во временные файлы и подменяются через ``os.replace``.
    """
    root_path = Path(root)
    paths = _as_paths(source_paths) or _as_paths(session)
    document_key_value = str(key or "") or document_key(paths)

    # 1. Сборка PDF в памяти: при ошибке на диске ничего не меняется.
    data = serialize_session(session)

    created = float(now if now is not None else time.time())
    annotations, forms = _model_counts(session)
    metadata: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "app": "PdfDesk",
        "key": document_key_value,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)),
        "created_ts": created,
        "source_paths": paths,
        "source_sha256": {path: file_sha256(path) for path in paths},
        "saved_path": saved_path,
        "copy_path": copy_path,
        "page_count": int(getattr(session, "page_count", 0) or 0),
        "revision": revision,
        "annotations": annotations,
        "forms": forms,
    }
    if extra:
        metadata.update(extra)

    # 2. Каталог документа внутри своего recovery-корня.
    directory = root_path / document_key_value
    document_path = directory / DOCUMENT_NAME
    metadata_path = directory / METADATA_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecoveryError("Не удалось создать папку восстановления: %s" % exc) from exc

    payload = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)

    # 3. Сначала новая копия PDF, потом метаданные (обе — атомарно).
    try:
        _atomic_write_bytes(document_path, data)
        _atomic_write_bytes(metadata_path, payload.encode("utf-8"))
    except OSError as exc:
        raise RecoveryError("Не удалось записать временный снимок: %s" % exc) from exc

    return RecoveryRecord(
        key=document_key_value,
        directory=directory,
        document_path=document_path,
        metadata_path=metadata_path,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Чтение и удаление снимков
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryRecord:
    """Найденный снимок восстановления."""

    key: str
    directory: Path
    document_path: Path
    metadata_path: Path
    metadata: dict = field(default_factory=dict)

    @property
    def source_paths(self) -> list[str]:
        return _as_paths(self.metadata.get("source_paths"))

    @property
    def created_ts(self) -> float:
        try:
            return float(self.metadata.get("created_ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def page_count(self) -> int:
        try:
            return int(self.metadata.get("page_count") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def source_label(self) -> str:
        paths = self.source_paths
        if not paths:
            return "неизвестный документ"
        return os.path.basename(paths[0]) or paths[0]


def load_records(root: Path | str) -> list[RecoveryRecord]:
    """Снимки внутри СВОЕГО каталога (чужие файлы игнорируются), новые — первыми."""
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    try:
        entries = list(root_path.iterdir())
    except OSError:
        return []
    records: list[RecoveryRecord] = []
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not _KEY_RE.match(entry.name):
            continue
        document_path = entry / DOCUMENT_NAME
        metadata_path = entry / METADATA_NAME
        if not document_path.is_file() or not metadata_path.is_file():
            continue
        try:
            raw = metadata_path.read_text(encoding="utf-8")
            metadata = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("key") or entry.name) != entry.name:
            continue
        records.append(
            RecoveryRecord(
                key=entry.name,
                directory=entry,
                document_path=document_path,
                metadata_path=metadata_path,
                metadata=metadata,
            )
        )
    records.sort(key=lambda item: item.created_ts, reverse=True)
    return records


def remove_record(root: Path | str, key: str) -> bool:
    """Удалить снимок своего документа. Вне своего recovery-каталога не работает."""
    text = str(key or "")
    if not _KEY_RE.match(text):
        return False
    root_path = Path(root)
    directory = root_path / text
    if not root_path.is_dir() or not directory.is_dir() or directory.is_symlink():
        return False
    if os.path.normcase(os.path.abspath(str(directory.parent))) != os.path.normcase(
        os.path.abspath(str(root_path))
    ):
        return False
    try:
        shutil.rmtree(str(directory))
    except OSError:
        return False
    return True
