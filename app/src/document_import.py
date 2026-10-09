"""Convert supported documents and images to PDF bytes for PdfDesk import.

Office formats are converted by the app-local LibreOffice runtime when present,
then by a user-installed LibreOffice. The module only discovers and invokes
the converter; intermediate files and its isolated profile live under app/tmp.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import io
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable
import uuid

from PIL import Image, ImageOps
from pypdf import PdfReader
from reportlab.lib import pagesizes
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas


_LOG = logging.getLogger(__name__)


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
TEXT_EXTENSIONS = frozenset({".txt"})
OFFICE_EXTENSIONS = frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"})
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | OFFICE_EXTENSIONS
DEFAULT_PAGE_SIZE = "A4"
OFFICE_TIMEOUT_SECONDS = 120
_FONT_CANDIDATES = (
    "arial.ttf", "segoeui.ttf", "tahoma.ttf", "calibri.ttf",
    "LiberationSans-Regular.ttf", "DejaVuSans.ttf", "NotoSans-Regular.ttf",
)


class DocumentImportError(RuntimeError):
    """Actionable failure while preparing an importable PDF."""


@dataclass(frozen=True)
class ConverterCapabilities:
    image_formats: tuple[str, ...]
    text_formats: tuple[str, ...]
    office_formats: tuple[str, ...]
    office_available: bool
    office_executable: str | None
    unicode_font: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "images": {"available": True, "formats": list(self.image_formats)},
            "text": {"available": self.unicode_font is not None,
                     "formats": list(self.text_formats), "font": self.unicode_font},
            "office": {"available": self.office_available,
                       "formats": list(self.office_formats),
                       "engine": "LibreOffice" if self.office_available else None,
                       "executable": self.office_executable},
        }


def _find_soffice() -> str | None:
    """Find app-local or user-installed LibreOffice without changing the system."""
    if os.name == "nt":
        # PyInstaller can place resources beside the executable (one-folder
        # bundle) or under its extraction directory. Check both explicitly:
        # __file__ may point into _MEIPASS while the runtime was bundled beside
        # PdfDesk.exe, and the inverse layout is also used by some builds.
        roots = []
        executable = getattr(sys, "executable", None)
        if executable:
            roots.append(Path(executable).resolve().parent / "tools" / "office")
        extraction_root = getattr(sys, "_MEIPASS", None)
        if extraction_root:
            roots.append(Path(extraction_root) / "tools" / "office")
        app_root = Path(__file__).resolve().parents[1]
        configured = os.environ.get("PDFDESK_OFFICE_HOME")
        if configured:
            roots.append(Path(configured).expanduser())
        roots.extend((app_root / "tools" / "office", app_root / "tools" / "libreoffice"))
        for root in roots:
            program = root / "program" if (root / "program").is_dir() else root
            for filename in ("soffice.com", "soffice.exe"):
                candidate = program / filename
                if candidate.is_file():
                    return str(candidate)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for root in roots:
            if root:
                program = Path(root) / "LibreOffice" / "program"
                for filename in ("soffice.com", "soffice.exe"):
                    candidate = program / filename
                    if candidate.is_file():
                        return str(candidate)
    elif sys_platform() == "darwin":
        candidate = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if candidate.is_file():
            return str(candidate)
    return None


def sys_platform() -> str:
    # Kept behind a helper so platform discovery is easy to mock in tests.
    import sys
    return sys.platform


def _font_directories() -> tuple[Path, ...]:
    directories: list[Path] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        directories.extend((Path(windir) / "Fonts", Path.home() / "AppData/Local/Microsoft/Windows/Fonts"))
    elif sys_platform() == "darwin":
        directories.extend((Path("/System/Library/Fonts/Supplemental"),
                            Path("/System/Library/Fonts"), Path.home() / "Library/Fonts"))
    else:
        directories.extend((Path("/usr/share/fonts/truetype/dejavu"),
                            Path("/usr/share/fonts/truetype/liberation"),
                            Path("/usr/share/fonts/truetype/liberation2"),
                            Path.home() / ".local/share/fonts"))
    return tuple(directories)


def _find_unicode_font() -> Path | None:
    for directory in _font_directories():
        for filename in _FONT_CANDIDATES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def _resolve_page_size(value: str | Iterable[float]) -> tuple[float, float]:
    if isinstance(value, str):
        key = value.strip().lower()
        named = {name.lower(): pair for name, pair in vars(pagesizes).items()
                 if isinstance(pair, tuple) and len(pair) == 2}
        if key not in named:
            raise DocumentImportError(
                "Неизвестный размер страницы «%s». Выберите A4, Letter или задайте ширину и высоту в пунктах." % value
            )
        width, height = named[key]
    else:
        try:
            width, height = (float(item) for item in value)
        except (TypeError, ValueError):
            raise DocumentImportError("Размер страницы должен содержать ширину и высоту в пунктах.") from None
    if not (1 <= width <= 14400 and 1 <= height <= 14400):
        raise DocumentImportError("Ширина и высота страницы должны быть от 1 до 14400 пунктов.")
    return float(width), float(height)


def _decode_text(data: bytes, path: Path) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeError as exc:
            raise DocumentImportError("Не удалось прочитать UTF-16 файл «%s»: %s" % (path.name, exc)) from exc
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise DocumentImportError("Файл «%s» не удалось прочитать как UTF-8 или Windows-1251." % path.name)


class DocumentImportService:
    """Convert images, plain text and Office files into standalone PDF bytes."""

    def __init__(self, *, soffice_path: str | None = None,
                 office_timeout: float = OFFICE_TIMEOUT_SECONDS,
                 temp_root: str | Path | None = None) -> None:
        self._soffice_override = soffice_path
        self.office_timeout = max(float(office_timeout), 0.1)
        if temp_root is not None:
            self.temp_root = Path(temp_root)
        else:
            app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
            root = Path(app_data) if app_data else Path(tempfile.gettempdir())
            self.temp_root = root / "PdfDesk" / "tmp"

    @property
    def soffice_path(self) -> str | None:
        return self._soffice_override or _find_soffice()

    def available_converters(self) -> dict[str, Any]:
        """Report formats and the local tools currently available to convert them."""
        font_path = _find_unicode_font()
        soffice = self.soffice_path
        return ConverterCapabilities(
            image_formats=("PNG", "JPEG", "JPG"), text_formats=("TXT",),
            office_formats=("DOCX", "DOC", "XLSX", "XLS", "PPTX", "PPT"),
            office_available=soffice is not None,
            office_executable=soffice,
            unicode_font=str(font_path) if font_path else None,
        ).as_dict()

    def convert_to_pdf(self, source: str | Path, *,
                       page_size: str | Iterable[float] = DEFAULT_PAGE_SIZE,
                       cancel_event: threading.Event | None = None) -> bytes:
        """Return the converted PDF bytes, leaving ``source`` untouched."""
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise DocumentImportError("Файл не найден: «%s»." % path)
        extension = path.suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            return self.images_to_pdf([path], page_size=page_size)
        if extension in TEXT_EXTENSIONS:
            return self.text_to_pdf(path, page_size=page_size)
        if extension in OFFICE_EXTENSIONS:
            return self.office_to_pdf(path, cancel_event=cancel_event)
        supported = ", ".join(sorted(ext.lstrip(".").upper() for ext in SUPPORTED_EXTENSIONS))
        raise DocumentImportError("Формат «%s» не поддержан. Поддерживаются: %s." % (extension or "без расширения", supported))

    def images_to_pdf(self, sources: Iterable[str | Path], *,
                      page_size: str | Iterable[float] = DEFAULT_PAGE_SIZE,
                      cancel_event: threading.Event | None = None) -> bytes:
        """Create one page per PNG/JPEG, centered and fitted without distortion."""
        paths = [Path(item).expanduser().resolve() for item in sources]
        if not paths:
            raise DocumentImportError("Выберите хотя бы одно изображение.")
        size = _resolve_page_size(page_size)
        output = io.BytesIO()
        pdf = pdf_canvas.Canvas(output, pagesize=size, pageCompression=1)
        page_w, page_h = size
        for path in paths:
            if cancel_event is not None and cancel_event.is_set():
                raise DocumentImportError("Импорт изображений отменён.")
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise DocumentImportError("Файл «%s» не найден или не является PNG/JPEG." % path)
            try:
                with Image.open(path) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGBA")
                    source_w, source_h = image.size
                    if source_w <= 0 or source_h <= 0:
                        raise ValueError("пустое изображение")
                    white = Image.new("RGB", image.size, "white")
                    white.paste(image, mask=image.getchannel("A"))
                    image_data = white.copy()
            except Exception as exc:  # noqa: BLE001 - report format and file
                raise DocumentImportError("Не удалось прочитать изображение «%s»: %s" % (path.name, exc)) from exc
            factor = min(page_w / source_w, page_h / source_h)
            draw_w, draw_h = source_w * factor, source_h * factor
            pdf.drawImage(ImageReader(image_data), (page_w - draw_w) / 2,
                          (page_h - draw_h) / 2, width=draw_w, height=draw_h,
                          preserveAspectRatio=True, mask="auto")
            pdf.showPage()
        pdf.save()
        return output.getvalue()

    def image_bytes_to_pdf(self, data: bytes | bytearray | memoryview, *,
                           page_size: str | Iterable[float] = DEFAULT_PAGE_SIZE) -> bytes:
        """Create a one-page PDF directly from image bytes, without a temp file."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise DocumentImportError("Данные изображения должны быть байтовой последовательностью.")
        raw = bytes(data)
        if not raw or len(raw) > 80 * 1024 * 1024:
            raise DocumentImportError("Файл сканирования пуст или превышает 80 МиБ.")
        try:
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.format not in ("PNG", "JPEG"):
                    raise ValueError("формат не PNG/JPEG")
                if opened.width <= 0 or opened.height <= 0 or \
                        opened.width * opened.height > 150_000_000:
                    raise ValueError("слишком большое изображение")
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                source_w, source_h = image.size
                white = Image.new("RGB", image.size, "white")
                white.paste(image, mask=image.getchannel("A"))
                image_data = white.copy()
        except Exception as exc:
            raise DocumentImportError("Не удалось прочитать сканированное изображение: %s" % exc) from exc
        page_w, page_h = _resolve_page_size(page_size)
        factor = min(page_w / source_w, page_h / source_h)
        draw_w, draw_h = source_w * factor, source_h * factor
        output = io.BytesIO()
        pdf = pdf_canvas.Canvas(output, pagesize=(page_w, page_h), pageCompression=1)
        pdf.drawImage(ImageReader(image_data), (page_w - draw_w) / 2,
                      (page_h - draw_h) / 2, width=draw_w, height=draw_h,
                      preserveAspectRatio=True, mask="auto")
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    def text_to_pdf(self, source: str | Path, *,
                    page_size: str | Iterable[float] = DEFAULT_PAGE_SIZE,
                    font_size: float = 11.0) -> bytes:
        """Render UTF-8/UTF-16/Windows-1251 text with an embedded Unicode font."""
        path = Path(source).expanduser().resolve()
        try:
            text = _decode_text(path.read_bytes(), path)
        except OSError as exc:
            raise DocumentImportError("Не удалось прочитать текстовый файл «%s»: %s" % (path.name, exc)) from exc
        size = _resolve_page_size(page_size)
        try:
            font_size = float(font_size)
        except (TypeError, ValueError):
            raise DocumentImportError("Размер текста должен быть числом.") from None
        if not (4 <= font_size <= 72):
            raise DocumentImportError("Размер текста должен быть от 4 до 72 пунктов.")
        font_path = _find_unicode_font()
        if font_path is None:
            raise DocumentImportError(
                "Для импорта TXT не найден системный шрифт с кириллицей. Установите Arial, Segoe UI, DejaVu Sans или Noto Sans."
            )
        font_name = "PdfDeskUnicode_%s" % uuid.uuid4().hex
        try:
            font = TTFont(font_name, str(font_path), validate=1)
            needed = {ord(char) for char in text if not char.isspace()}
            missing = needed.difference(font.face.charWidths)
            if missing:
                raise DocumentImportError(
                    "Шрифт «%s» не содержит символы файла «%s». Установите шрифт с поддержкой этих языков." %
                    (font_path.name, path.name)
                )
            pdfmetrics.registerFont(font)
        except DocumentImportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocumentImportError("Не удалось подготовить Unicode-шрифт «%s»: %s" % (font_path.name, exc)) from exc

        output = io.BytesIO()
        pdf = pdf_canvas.Canvas(output, pagesize=size, pageCompression=1)
        pdf.setFont(font_name, font_size)
        page_w, page_h = size
        margin = min(42.0, page_w * 0.1, page_h * 0.1)
        usable_w = page_w - margin * 2
        line_height = font_size * 1.35
        y = page_h - margin - font_size

        def next_page() -> float:
            pdf.showPage()
            pdf.setFont(font_name, font_size)
            return page_h - margin - font_size

        def wrapped_lines(line: str) -> list[str]:
            words = line.expandtabs(4).split()
            if not words:
                return [""]
            result: list[str] = []
            current = ""
            for word in words:
                if pdfmetrics.stringWidth(word, font_name, font_size) > usable_w:
                    if current:
                        result.append(current)
                        current = ""
                    chunk = ""
                    for char in word:
                        if chunk and pdfmetrics.stringWidth(chunk + char, font_name, font_size) > usable_w:
                            result.append(chunk)
                            chunk = char
                        else:
                            chunk += char
                    word = chunk
                candidate = (current + " " + word).strip()
                if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > usable_w:
                    result.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                result.append(current)
            return result

        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            for visual_line in wrapped_lines(line):
                if y < margin:
                    y = next_page()
                if visual_line:
                    pdf.drawString(margin, y, visual_line)
                y -= line_height
        pdf.save()
        return output.getvalue()

    def office_to_pdf(self, source: str | Path, *,
                      cancel_event: threading.Event | None = None) -> bytes:
        """Convert one Office document using a discovered local LibreOffice."""
        path = Path(source).expanduser().resolve()
        if path.suffix.lower() not in OFFICE_EXTENSIONS:
            raise DocumentImportError("Формат «%s» не входит в список поддерживаемых Office-файлов." % path.suffix)
        soffice = self.soffice_path
        if not soffice:
            raise DocumentImportError(
                "Для импорта %s требуется установленный LibreOffice. Установите LibreOffice и повторите попытку; PdfDesk не устанавливает его автоматически." %
                path.suffix.upper()
            )
        if not path.is_file():
            raise DocumentImportError("Файл не найден: «%s»." % path)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        try:
            with _office_temp_directory(self.temp_root) as work:
                profile = work / "profile"
                profile.mkdir()
                command = [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(work),
                           "-env:UserInstallation=" + profile.as_uri(), str(path)]
                try:
                    process = subprocess.Popen(
                        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        encoding="utf-8", errors="replace",
                    )
                except OSError as exc:
                    raise DocumentImportError("Не удалось запустить LibreOffice «%s»: %s" % (soffice, exc)) from exc
                deadline = time.monotonic() + self.office_timeout
                stdout = stderr = ""
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        process.kill()
                        stdout, stderr = process.communicate()
                        raise DocumentImportError("Импорт «%s» отменён." % path.name)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        stdout, stderr = process.communicate()
                        details = _process_details(stdout, stderr)
                        suffix = " Подробности: %s" % details if details else ""
                        raise DocumentImportError(
                            "LibreOffice не завершил преобразование «%s» за %.1f секунд.%s Закройте зависшие окна LibreOffice и повторите импорт." %
                            (path.name, self.office_timeout, suffix)
                        )
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                produced = work / (path.stem + ".pdf")
                if completed.returncode != 0:
                    details = _process_details(completed.stdout, completed.stderr)
                    raise DocumentImportError(
                        "LibreOffice не смог преобразовать «%s» (код %d). %s" %
                        (path.name, completed.returncode,
                         details or "Проверьте, что файл исправен и не защищён паролем.")
                    )
                if not produced.is_file() or produced.stat().st_size == 0:
                    details = _process_details(completed.stdout, completed.stderr)
                    raise DocumentImportError(
                        "LibreOffice не создал PDF для «%s». %s" %
                        (path.name, details or "Проверьте формат и доступ к файлу.")
                    )
                pdf_bytes = produced.read_bytes()
                if not pdf_bytes.startswith(b"%PDF-"):
                    raise DocumentImportError(
                        "LibreOffice создал файл с неверным содержимым для «%s». Повторите импорт и проверьте, что документ исправен." % path.name
                    )
                try:
                    if not PdfReader(io.BytesIO(pdf_bytes), strict=False).pages:
                        raise ValueError("PDF не содержит страниц")
                except Exception as exc:  # noqa: BLE001
                    raise DocumentImportError(
                        "LibreOffice создал повреждённый PDF для «%s»: %s" % (path.name, exc)
                    ) from exc
                return pdf_bytes
        except DocumentImportError:
            raise
        except OSError as exc:
            raise DocumentImportError("Не удалось подготовить временное место для Office-конвертации: %s" % exc) from exc


def _process_details(stdout: Any, stderr: Any) -> str:
    pieces = []
    for value in (stderr, stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        text = str(value or "").strip()
        if text:
            pieces.append(text[-1500:])
    return " | ".join(pieces)


@contextmanager
def _office_temp_directory(root: Path):
    """Clean up LibreOffice's eagerly-created profile tree with Windows retries."""
    temporary = tempfile.TemporaryDirectory(prefix="pdfdesk-office-", dir=root)
    folder = Path(temporary.name)
    try:
        yield folder
    finally:
        try:
            temporary.cleanup()
        except OSError as first_error:
            # LibreOffice may finish creating its bundled extension registry
            # just as its command process exits. On Windows the first rmtree
            # can race that final write; retry the app-owned temp tree briefly.
            last_error = first_error
            for attempt in range(8):
                time.sleep(0.2 * (attempt + 1))
                try:
                    shutil.rmtree(folder)
                    return
                except FileNotFoundError:
                    return
                except OSError as exc:
                    last_error = exc
            if folder.exists():
                # The PDF has already been produced and read into memory. A
                # profile cleanup race must not turn a successful import into
                # an error; retain it in app/tmp for later cleanup and report
                # the exact path for diagnostics.
                _LOG.warning("Office profile cleanup deferred for %s: %s",
                             folder, last_error)


def available_converters() -> dict[str, Any]:
    """Module-level convenience API for capability checks before showing import UI."""
    return DocumentImportService().available_converters()
