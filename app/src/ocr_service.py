"""Local Tesseract OCR with structured word boxes and a searchable text layer.

No OCR binary or trained data is bundled or downloaded by this module. Missing
engines and language packs are reported as errors instead of returning empty
results that could be mistaken for a successful recognition.
"""
from __future__ import annotations

import csv
import ctypes
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from PIL import Image


LANGUAGES = ("rus", "eng", "ukr", "pol", "kat", "srp", "srp_latn", "mlt")
DEFAULT_TIMEOUT_SECONDS = 60
MAX_IMAGE_PIXELS = 120_000_000


class OCRError(RuntimeError):
    """Base class for explicit OCR service failures."""


class OCREngineUnavailable(OCRError):
    """Tesseract is missing or did not respond as expected."""


class OCRLanguageUnavailable(OCRError):
    """One or more requested trained data packs are not installed."""


class OCRTimeout(OCRError):
    """Recognition exceeded its configured timeout."""


@dataclass(frozen=True)
class OCRWord:
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float


@dataclass(frozen=True)
class OCRResult:
    words: tuple[OCRWord, ...]
    image_size: tuple[int, int]
    languages: tuple[str, ...]
    overlay_pdf: bytes


def _parse_languages(output: str) -> frozenset[str]:
    languages = set()
    lines = output.splitlines()
    if lines:
        for line in lines[1:]:
            value = line.strip()
            if re.fullmatch(r"[A-Za-z0-9_]+", value):
                languages.add(value)
    return frozenset(languages)


def _short_windows_path(path: Path) -> Path:
    """Use an ASCII 8.3 path when available for MinGW Tesseract builds.

    Some Windows Tesseract builds convert command-line paths with the active
    code page. App folders can contain Cyrillic characters, so pass a short
    alias where Windows provides one.
    """
    if os.name != "nt" or path.as_posix().isascii():
        return path
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_short = kernel32.GetShortPathNameW
        get_short.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        get_short.restype = ctypes.c_uint32
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = get_short(str(path), buffer, capacity)
        if 0 < length < capacity and buffer.value.isascii():
            return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        pass
    return path


def _parse_tsv(tsv: str) -> tuple[OCRWord, ...]:
    words = []
    try:
        reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
        required = {"level", "left", "top", "width", "height", "conf", "text"}
        if not required.issubset(reader.fieldnames or ()):
            raise OCRError("Tesseract TSV отсутствует или имеет неверный формат")
        for row in reader:
            text = (row.get("text") or "").strip()
            if row.get("level") != "5" or not text:
                continue
            try:
                left, top = int(row["left"]), int(row["top"])
                width, height = int(row["width"]), int(row["height"])
                confidence = float(row["conf"])
            except (TypeError, ValueError, KeyError):
                continue
            if width <= 0 or height <= 0 or confidence < 0:
                continue
            words.append(OCRWord(text, (left, top, width, height), confidence))
    except csv.Error as exc:
        raise OCRError("Не удалось прочитать TSV от Tesseract") from exc
    return tuple(words)


def create_searchable_overlay_pdf(words: Iterable[OCRWord], image_size: tuple[int, int],
                                  *, dpi: float = 72.0,
                                  font_family: str = "Arial") -> bytes:
    """Create a transparent PDF text layer aligned to the image pixel grid.

    The returned page has dimensions ``image_size * 72 / dpi`` points. Text is
    invisible when rendered (PDF text rendering mode 3) but remains selectable
    and searchable. Callers can merge this page over an existing scan.
    """
    if dpi <= 0:
        raise ValueError("DPI должен быть больше нуля")
    width_px, height_px = map(int, image_size)
    if width_px <= 0 or height_px <= 0:
        raise ValueError("Размер изображения должен быть положительным")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as pdf_canvas
    from .pdf_backend import find_font_file

    font_path = find_font_file(font_family)
    font_name = "PdfDeskOCRLayer"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    scale = 72.0 / float(dpi)
    page_width, page_height = width_px * scale, height_px * scale
    output = io.BytesIO()
    canvas = pdf_canvas.Canvas(output, pagesize=(page_width, page_height),
                               pageCompression=1, invariant=1)
    for word in words:
        left, top, word_width, word_height = word.bbox
        if not word.text or word_width <= 0 or word_height <= 0:
            continue
        size = max(1.0, word_height * scale * 0.82)
        x = left * scale
        y = page_height - (top + word_height) * scale
        text = canvas.beginText(x, y)
        text.setFont(font_name, size)
        text.setTextRenderMode(3)
        text.textOut(word.text)
        canvas.drawText(text)
    canvas.showPage()
    canvas.save()
    return output.getvalue()


class TesseractOCRService:
    """Run a local Tesseract binary without a shell or network access."""

    def __init__(self, executable: str | os.PathLike[str] | None = None,
                 *, temp_root: str | os.PathLike[str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.executable = str(executable) if executable else self._discover_executable()
        self.tessdata_dir = self._discover_tessdata_dir(
            self.executable, include_app_local=executable is None)
        if temp_root is None:
            cache = os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_CACHE_HOME')
            temp_root = (Path(cache) if cache else Path(tempfile.gettempdir())) / 'PdfDesk' / 'tmp' / 'ocr'
        self.temp_root = Path(temp_root)
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("Время ожидания должно быть больше нуля")
        self._languages_cache: frozenset[str] | None = None

    @staticmethod
    def _discover_executable() -> str | None:
        configured = os.environ.get("PDFDESK_TESSERACT") or os.environ.get("TESSERACT_CMD")
        if configured and Path(configured).is_file():
            return str(Path(configured))
        local_home = os.environ.get("PDFDESK_OCR_HOME")
        homes = [Path(local_home)] if local_home else []
        if getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            homes.extend((executable_dir / "tools" / "ocr", executable_dir / "ocr"))
            bundle_dir = getattr(sys, "_MEIPASS", None)
            if bundle_dir:
                homes.append(Path(bundle_dir) / "tools" / "ocr")
        else:
            app_dir = Path(__file__).resolve().parents[1]
            homes.append(app_dir / "tools" / "ocr")
        local_executable = next((home / "tesseract.exe" for home in homes
                                 if (home / "tesseract.exe").is_file()), None)
        if local_executable is not None:
            return str(local_executable)
        found = shutil.which("tesseract")
        if found:
            return found
        if sys.platform == "win32":
            candidates = (
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
                "Tesseract-OCR" / "tesseract.exe",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) /
                "Tesseract-OCR" / "tesseract.exe",
            )
            return next((str(path) for path in candidates if path.is_file()), None)
        if sys.platform == "darwin":
            # GUI applications may not inherit Homebrew's PATH from a shell.
            candidates = (Path("/opt/homebrew/bin/tesseract"),
                          Path("/usr/local/bin/tesseract"))
            return next((str(path) for path in candidates if path.is_file()), None)
        return None

    @staticmethod
    def _discover_tessdata_dir(executable: str | None, *,
                               include_app_local: bool = True) -> Path | None:
        """Find trained data without depending on process-global PATH changes.

        ``PDFDESK_TESSDATA_DIR`` is the direct tessdata folder override.
        ``TESSDATA_PREFIX`` is accepted as either the folder itself or its
        parent (which some Windows setups use).
        """
        candidates: list[Path] = []
        explicit = os.environ.get("PDFDESK_TESSDATA_DIR")
        prefix = os.environ.get("TESSDATA_PREFIX")
        for value in (explicit, prefix):
            if value:
                folder = Path(value).expanduser()
                candidates.extend((folder, folder / "tessdata"))
        if executable:
            executable_dir = Path(executable).resolve().parent
            candidates.extend((executable_dir / "tessdata", executable_dir))
            if sys.platform == "darwin":
                candidates.append(executable_dir.parent / "share" / "tessdata")
        if include_app_local and getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            candidates.extend((executable_dir / "tools" / "ocr" / "tessdata",
                               executable_dir / "ocr" / "tessdata"))
        elif include_app_local:
            candidates.append(Path(__file__).resolve().parents[1] / "tools" / "ocr" / "tessdata")
        for folder in candidates:
            if folder.is_dir() and any(folder.glob("*.traineddata")):
                return _short_windows_path(folder.resolve())
        return None

    def _tessdata_args(self) -> list[str]:
        return ["--tessdata-dir", str(self.tessdata_dir)] if self.tessdata_dir else []

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def _run(self, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if not self.executable:
            raise OCREngineUnavailable(
                "Tesseract не установлен. OCR недоступно; установите Tesseract и нужные языковые пакеты.")
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout or self.timeout,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRTimeout("Tesseract превысил время ожидания") from exc
        except OSError as exc:
            raise OCREngineUnavailable("Не удалось запустить Tesseract: %s" % exc) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise OCREngineUnavailable("Tesseract завершился с ошибкой%s" %
                                        (": " + detail if detail else ""))
        return result

    def installed_languages(self, *, refresh: bool = False) -> frozenset[str]:
        if self._languages_cache is not None and not refresh:
            return self._languages_cache
        result = self._run([*self._tessdata_args(), "--list-langs"])
        languages = _parse_languages(result.stdout + "\n" + result.stderr)
        if not languages:
            raise OCREngineUnavailable("Tesseract не вернул список языковых пакетов")
        self._languages_cache = languages
        return languages

    def language_availability(self) -> dict[str, bool]:
        """Check required language IDs against this engine's actual pack list."""
        try:
            installed = self.installed_languages()
        except OCREngineUnavailable:
            return {language: False for language in LANGUAGES}
        return {language: language in installed for language in LANGUAGES}

    @staticmethod
    def _normalize_languages(languages: str | Iterable[str]) -> tuple[str, ...]:
        values = (languages,) if isinstance(languages, str) else tuple(languages)
        normalized = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        if not normalized:
            raise ValueError("Укажите хотя бы один язык OCR")
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", item) for item in normalized):
            raise ValueError("Код языка содержит недопустимые символы")
        return normalized

    def _validate_languages(self, requested: tuple[str, ...]) -> None:
        installed = self.installed_languages()
        missing = [language for language in requested if language not in installed]
        if missing:
            raise OCRLanguageUnavailable(
                "Не установлены языковые пакеты Tesseract: %s" % ", ".join(missing))

    def recognize(self, image: Image.Image, languages: str | Iterable[str], *,
                  dpi: float = 72.0, timeout: float | None = None) -> OCRResult:
        """Recognize image words and return pixel boxes plus a searchable layer."""
        if not isinstance(image, Image.Image):
            raise TypeError("OCR ожидает изображение Pillow.Image")
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Недопустимый размер изображения для OCR")
        requested = self._normalize_languages(languages)
        self._validate_languages(requested)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=self.temp_root) as temp_dir:
            source = Path(temp_dir) / "page.png"
            image.convert("RGB").save(source, format="PNG", optimize=False)
            input_path = _short_windows_path(source) if Path(self.executable or "").is_file() else source
            result = self._run([str(input_path), "stdout", "-l", "+".join(requested),
                                *self._tessdata_args(),
                                "--dpi", str(round(dpi)), "tsv"], timeout=timeout)
        words = _parse_tsv(result.stdout)
        overlay = create_searchable_overlay_pdf(words, (width, height), dpi=dpi)
        return OCRResult(words, (width, height), requested, overlay)
