"""macOS Image Capture (ImageCaptureCore) scanner adapter.

Discovery and acquisition happen in a small Swift helper
(``app/native/MacScanner.swift``, build notes in
``app/native/BUILD-mac-scanner.md``) that talks to ImageCaptureCore directly.
The helper is built only on macOS, prints exactly one structured JSON object on
stdout and writes the acquired page only into the app-owned temporary file this
adapter hands to it, so no PyObjC bridge is required and no document path ever
reaches the helper.

The process launcher is injectable (``process_factory``) so the protocol,
cancellation, timeout, error mapping and image validation can be exercised
without macOS hardware. Nothing in this module reports a successful macOS scan
unless a helper really produced a validated image.
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image


MIN_DPI = 75
MAX_DPI = 1200
MAX_IMAGE_PIXELS = 150_000_000
MAX_IMAGE_BYTES = 80 * 1024 * 1024
MAX_DEVICES = 64
MAX_LABEL_LENGTH = 200
MAX_DEVICE_ID_LENGTH = 1024
HELPER_NAME = "MacScanner"
SOURCE_NAME = "MacScanner.swift"
HELPER_STDOUT_LIMIT = 1_000_000
POLL_SECONDS = 0.2
TERMINATE_GRACE_SECONDS = 3.0
BUILD_TIMEOUT_SECONDS = 600
TIMEOUT_FLOOR_SECONDS = 5
HELPER_TIMEOUT_MARGIN_SECONDS = 5
_MACHO_MAGICS = frozenset({
    b"\xcf\xfa\xed\xfe",  # Mach-O 64 little endian (x86_64, arm64)
    b"\xce\xfa\xed\xfe",  # Mach-O 32 little endian
    b"\xfe\xed\xfa\xce",  # Mach-O 32 big endian
    b"\xfe\xed\xfa\xcf",  # Mach-O 64 big endian
    b"\xca\xfe\xba\xbe",  # universal binary
    b"\xbe\xba\xfe\xca",  # universal binary, byte swapped
    b"\xca\xfe\xba\xbf",  # universal binary 64
})


class MacScannerError(RuntimeError):
    """Base class for macOS Image Capture discovery and transfer failures."""


class MacScannerUnavailable(MacScannerError):
    """No usable Image Capture helper exists on this machine."""


class MacScannerDeviceNotFound(MacScannerError):
    """The selected scanner is no longer connected."""


class MacScannerCancelled(MacScannerError):
    """The user cancelled the acquisition."""


class MacScannerTransferError(MacScannerError):
    """The helper failed to acquire or write a valid image."""


class MacScannerTimeout(MacScannerError):
    """The helper exceeded the configured deadline."""


@dataclass(frozen=True)
class MacScannerDevice:
    device_id: str
    name: str
    manufacturer: str = ""
    description: str = ""


ProcessFactory = Callable[[list[str]], Any]

_ERROR_MESSAGES = {
    "device_not_found": "Выбранный сканер больше не подключён.",
    "timeout": "Сканирование не завершилось за отведённое время.",
    "cancelled": "Сканирование отменено.",
    "session_failed": "Не удалось открыть сеанс сканера через Image Capture.",
    "no_functional_unit": "Сканер не предоставил доступный модуль сканирования.",
    "transfer_failed": "Сканер не смог передать изображение.",
    "invalid_image": "Сканер вернул некорректное изображение.",
    "write_failed": "Не удалось сохранить результат сканирования.",
    "device_error": "Сканер сообщил об ошибке.",
    "permission_denied": "macOS отклонила доступ к сканеру через Image Capture.",
    "unavailable": "Служба Image Capture недоступна.",
    "usage": "Внутренняя ошибка параметров помощника сканирования.",
    "internal": "Внутренняя ошибка помощника сканирования.",
}

_ERROR_TYPES: dict[str, type[MacScannerError]] = {
    "device_not_found": MacScannerDeviceNotFound,
    "timeout": MacScannerTimeout,
    "cancelled": MacScannerCancelled,
    "session_failed": MacScannerTransferError,
    "no_functional_unit": MacScannerTransferError,
    "transfer_failed": MacScannerTransferError,
    "invalid_image": MacScannerTransferError,
    "write_failed": MacScannerTransferError,
    "device_error": MacScannerTransferError,
    "permission_denied": MacScannerUnavailable,
    "unavailable": MacScannerUnavailable,
    "usage": MacScannerError,
    "internal": MacScannerError,
}

_RETURNCODE_CODES = {
    2: "usage",
    3: "device_not_found",
    4: "timeout",
    5: "cancelled",
    7: "unavailable",
}


class MacScannerService:
    """Discover and acquire one page from a macOS scanner via ImageCaptureCore."""

    def __init__(self, *, temp_root: str | os.PathLike[str] | None = None,
                 timeout_seconds: int = 180, host: str | None = None,
                 helper_path: str | os.PathLike[str] | None = None,
                 process_factory: ProcessFactory | None = None,
                 allow_build: bool | None = None,
                 build_root: str | os.PathLike[str] | None = None,
                 source_path: str | os.PathLike[str] | None = None) -> None:
        self._host = host or platform.system()
        self._timeout = max(TIMEOUT_FLOOR_SECONDS, int(timeout_seconds))
        app_root = Path(__file__).resolve().parents[1]
        workspace_tmp = (app_root / "tmp").resolve()
        if temp_root is not None:
            candidate = Path(temp_root).resolve()
            if not candidate.is_relative_to(workspace_tmp):
                raise ValueError("Тестовые временные сканы должны храниться в папке app/tmp.")
        else:
            candidate = Path.home() / "Library" / "Caches" / "PdfDesk" / "scanner-tmp"
        self._temp_root = candidate
        self._app_root = app_root
        self._workspace_tmp = workspace_tmp
        self._helper_override = (Path(helper_path).resolve()
                                 if helper_path is not None else None)
        self._process_factory = process_factory
        self._allow_build = (not bool(getattr(sys, "frozen", False))
                             if allow_build is None else bool(allow_build))
        self._build_root = (Path(build_root).resolve() if build_root is not None
                            else self._default_build_root())
        self._source_path = (Path(source_path).resolve() if source_path is not None
                             else (app_root / "native" / SOURCE_NAME))
        # Instance cache only: no module level state, so two services never share
        # a resolved or built helper.
        self._resolved_helper: Path | None = None
        self._build_error = ""

    # ------------------------------------------------------------------ state

    @property
    def supported(self) -> bool:
        """True when a helper exists (or can be built) on a real macOS host."""
        return self._availability()[0]

    @property
    def capability_message(self) -> str:
        return self._availability()[1]

    @property
    def temp_root(self) -> Path:
        return self._temp_root

    def helper_path(self) -> Path | None:
        """Return the discovered helper without building anything."""
        return self._find_helper()

    def _on_macos(self) -> bool:
        return self._host == "Darwin"

    def _availability(self) -> tuple[bool, str]:
        if not self._on_macos():
            return False, ("Сканирование на macOS доступно через Image Capture "
                           "(ImageCaptureCore) только на самой macOS; текущая система — %s."
                           % (self._host or "неизвестна"))
        if self._find_helper() is not None:
            return True, "Сканирование доступно через Image Capture (ImageCaptureCore)."
        if self._can_build():
            return True, ("Помощник MacScanner будет собран из исходника при первом "
                          "обращении (Image Capture, swiftc).")
        return False, ("Сканирование через Image Capture недоступно: %s"
                       % self._build_limitation())

    def _can_build(self) -> bool:
        """True when the development build path could still produce the helper."""
        if not self._on_macos() or not self._allow_build or self._helper_override is not None:
            return False
        if self._build_error or not self._source_path.is_file():
            return False
        return self._compiler_path() is not None

    def _build_limitation(self) -> str:
        if self._helper_override is not None:
            return ("помощник MacScanner по указанному пути не найден или не является "
                    "исполняемым файлом Mach-O (%s)." % self._helper_override)
        if not self._allow_build:
            return "помощник MacScanner не входит в сборку приложения."
        if not self._source_path.is_file():
            return "исходный файл помощника не найден (%s)." % self._source_path
        if self._compiler_path() is None:
            return ("не найден компилятор Swift (swiftc). Установите Xcode Command Line "
                    "Tools: xcode-select --install.")
        if self._build_error:
            return "не удалось собрать помощник MacScanner: %s" % self._build_error
        return "помощник MacScanner не найден и не может быть собран."

    # ------------------------------------------------------------ helper file

    def _helper_candidates(self) -> list[Path]:
        if self._helper_override is not None:
            return [self._helper_override]
        candidates: list[Path] = []
        executable = Path(sys.executable).resolve()
        contents = executable.parent.parent
        bundles = (contents / "Resources", contents / "Frameworks", executable.parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "native" / HELPER_NAME)
        for bundle in bundles:
            candidates.append(bundle / "native" / HELPER_NAME)
            candidates.append(bundle / HELPER_NAME)
        candidates.append(self._app_root / "native" / "build" / HELPER_NAME)
        candidates.append(self._app_root / "native" / HELPER_NAME)
        unique: list[Path] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        return unique

    def _helper_ok(self, path: Path) -> bool:
        try:
            if not path.is_file():
                return False
            if path.stat().st_size <= 0:
                return False
            with path.open("rb") as handle:
                magic = handle.read(4)
        except OSError:
            return False
        if magic not in _MACHO_MAGICS:
            return False
        if self._on_macos() and not os.access(path, os.X_OK):
            return False
        return True

    def _find_helper(self) -> Path | None:
        cached = self._resolved_helper
        if cached is not None and self._helper_ok(cached):
            return cached
        for candidate in self._helper_candidates():
            if self._helper_ok(candidate):
                self._resolved_helper = candidate
                return candidate
        return None

    def _ensure_helper(self) -> Path:
        # A helper binary can only run on macOS; never start one elsewhere, even
        # when an injected path happens to point at a Mach-O file.
        if not self._on_macos():
            raise MacScannerUnavailable(self.capability_message)
        helper = self._find_helper()
        if helper is not None:
            return helper
        built = self._build_helper()
        if built is None:
            raise MacScannerUnavailable(self.capability_message)
        return built

    def _compiler_path(self) -> str | None:
        if not self._on_macos():
            return None
        return shutil.which("swiftc")

    def _default_build_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return (Path.home() / "Library" / "Caches" / "PdfDesk" / "native").resolve()
        return (self._app_root / "tmp" / "mac-scanner-build").resolve()

    def _build_helper(self) -> Path | None:
        """Compile the helper on macOS only; returns the built path or None."""
        if not self._on_macos() or not self._allow_build:
            return None
        compiler = self._compiler_path()
        source = self._source_path
        if compiler is None or not source.is_file():
            return None
        build_root = self._build_root
        try:
            build_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._build_error = str(exc)[:200]
            return None
        output = build_root / HELPER_NAME
        staging = build_root / (HELPER_NAME + ".building")
        try:
            staging.unlink(missing_ok=True)
        except OSError as exc:
            self._build_error = str(exc)[:200]
            return None
        machine = platform.machine()
        command = [compiler, "-O", "-swift-version", "5"]
        if machine in ("arm64", "x86_64"):
            command += ["-target", "%s-apple-macos12.0" % machine]
        command += ["-framework", "ImageCaptureCore", "-framework", "ImageIO",
                    "-framework", "CoreGraphics", "-o", str(staging), str(source)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace",
                                       timeout=BUILD_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._build_error = str(exc)[:200]
            self._discard(staging)
            return None
        if completed.returncode != 0 or not self._helper_ok(staging):
            detail = (completed.stderr or completed.stdout or "").strip()
            self._build_error = (detail[:200] if detail
                                 else "компилятор завершился с кодом %s" % completed.returncode)
            self._discard(staging)
            return None
        try:
            os.chmod(staging, 0o755)
            os.replace(staging, output)
        except OSError as exc:
            self._build_error = str(exc)[:200]
            self._discard(staging)
            return None
        if not self._helper_ok(output):
            self._build_error = "собранный файл не является исполняемым Mach-O"
            return None
        self._resolved_helper = output
        return output

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # --------------------------------------------------------------- protocol

    def devices(self) -> list[MacScannerDevice]:
        """Return connected scanners without opening any acquisition UI."""
        helper = self._ensure_helper()
        payload = self._run_helper(
            [str(helper), "list", "--timeout", str(self._helper_timeout())])
        raw = payload.get("devices")
        if not isinstance(raw, list):
            raise MacScannerError("Помощник MacScanner вернул неверный список устройств.")
        result: list[MacScannerDevice] = []
        for item in raw:
            if len(result) >= MAX_DEVICES:
                break
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("id") or "").strip()
            if not device_id or len(device_id) > MAX_DEVICE_ID_LENGTH:
                continue
            result.append(MacScannerDevice(
                device_id=device_id,
                name=self._clean_label(item.get("name"), "Сканер"),
                manufacturer=self._clean_label(item.get("manufacturer"), ""),
                description=self._clean_label(item.get("description"), "")))
        return result

    def scan(self, device_id: str, dpi: int = 300, *,
             cancel_event: Any | None = None) -> bytes:
        """Acquire one page and return validated JPEG bytes."""
        if not isinstance(device_id, str) or not device_id.strip() \
                or len(device_id) > MAX_DEVICE_ID_LENGTH:
            raise ValueError("Выберите устройство сканирования.")
        if isinstance(dpi, bool) or not isinstance(dpi, int) or not MIN_DPI <= dpi <= MAX_DPI:
            raise ValueError("Разрешение сканирования должно быть от %d до %d dpi."
                             % (MIN_DPI, MAX_DPI))
        helper = self._ensure_helper()
        target = self._temporary_target()
        try:
            payload = self._run_helper(
                [str(helper), "scan", "--device-id", device_id, "--dpi", str(dpi),
                 "--output", str(target), "--timeout", str(self._helper_timeout())],
                cancel_event=cancel_event)
            reported = payload.get("bytes")
            if isinstance(reported, int) and reported > MAX_IMAGE_BYTES:
                raise MacScannerTransferError("Размер скана превышает допустимый предел.")
            return self._validate_image(target)
        finally:
            self._discard(target)

    def _helper_timeout(self) -> int:
        return max(TIMEOUT_FLOOR_SECONDS, self._timeout - HELPER_TIMEOUT_MARGIN_SECONDS)

    def _spawn(self, argv: list[str]) -> Any:
        if self._process_factory is not None:
            return self._process_factory(argv)
        kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                  "stdin": subprocess.DEVNULL, "text": True,
                                  "encoding": "utf-8", "errors": "replace"}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        return subprocess.Popen(argv, **kwargs)

    def _run_helper(self, argv: list[str], *,
                    cancel_event: Any | None = None) -> dict[str, Any]:
        process = self._spawn(argv)
        stdout = ""
        stderr = ""
        try:
            deadline = time.monotonic() + self._timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise MacScannerCancelled("Сканирование отменено.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MacScannerTimeout(
                        "Операция сканирования не завершилась за %d с." % self._timeout)
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(POLL_SECONDS, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            returncode = process.returncode
        finally:
            self._release(process)
        return self._interpret(returncode, stdout, stderr)

    def _release(self, process: Any) -> None:
        try:
            if process.poll() is None:
                self._terminate(process)
        finally:
            for stream in (getattr(process, "stdout", None),
                           getattr(process, "stderr", None)):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass

    def _terminate(self, process: Any) -> None:
        try:
            process.terminate()
        except OSError:
            pass
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.05)
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _interpret(self, returncode: Any, stdout: str, stderr: str) -> dict[str, Any]:
        payload = self._parse_payload(stdout)
        if payload is not None:
            if payload.get("ok") is True:
                return payload
            code = ""
            detail = ""
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "").strip()
                detail = str(error.get("message") or "").strip()
            raise self._error_for(code, detail, returncode)
        if returncode == 0:
            raise MacScannerError("Помощник MacScanner вернул некорректный ответ.")
        detail = (stderr or stdout or "").strip()
        if len(detail) > 500:
            detail = detail[:500]
        raise self._error_for("", detail, returncode)

    def _error_for(self, code: str, detail: str, returncode: Any) -> MacScannerError:
        if not code and isinstance(returncode, int):
            code = _RETURNCODE_CODES.get(returncode, "")
        message = _ERROR_MESSAGES.get(code, "")
        if not message:
            message = ("Помощник MacScanner завершился с ошибкой (код %s)."
                       % (returncode if returncode is not None else "неизвестен"))
        if detail and detail not in message:
            message = "%s (%s)" % (message, detail)
        error_type = _ERROR_TYPES.get(code)
        if error_type is None:
            error_type = MacScannerError if returncode == 0 else MacScannerTransferError
        return error_type(message)

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        text = (stdout or "").strip()
        if not text or len(text) > HELPER_STDOUT_LIMIT:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = candidate
                break
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------ acquisition

    def _temporary_target(self) -> Path:
        root = self._temp_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix="pdfdesk_scan_", suffix=".jpg", dir=root)
        os.close(handle)
        target = Path(name).resolve()
        if not target.is_relative_to(root):
            target.unlink(missing_ok=True)
            raise MacScannerError("Не удалось создать безопасный временный файл сканирования.")
        return target

    @staticmethod
    def _validate_image(target: Path) -> bytes:
        try:
            size = target.stat().st_size
            if size <= 0:
                raise MacScannerTransferError("Сканер не создал файл изображения.")
            if size > MAX_IMAGE_BYTES:
                raise MacScannerTransferError("Размер скана превышает допустимый предел.")
            data = target.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise MacScannerTransferError("Сканер вернул неподдерживаемый формат.")
                if image.width <= 0 or image.height <= 0 or \
                        image.width * image.height > MAX_IMAGE_PIXELS:
                    raise MacScannerTransferError("Размер изображения сканирования слишком велик.")
                image.verify()
            return data
        except MacScannerTransferError:
            raise
        except Exception as exc:
            raise MacScannerTransferError(
                "Сканер не создал корректный файл изображения.") from exc

    @staticmethod
    def _clean_label(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        return text[:MAX_LABEL_LENGTH]
