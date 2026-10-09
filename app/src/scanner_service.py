"""Local scanner discovery and single-page acquisition.

Windows uses the built-in WIA Automation COM interface through a noninteractive
PowerShell worker. The worker writes only to a short-lived file under app/tmp;
the service returns validated image bytes and removes that file. No document
paths are passed to the worker.

macOS delegates to the native ImageCaptureCore adapter in
:mod:`src.mac_scanner` (Swift helper, discovery plus one-page acquisition); the
Windows WIA code below is the only path used on Windows and is unchanged.
"""
from __future__ import annotations

import base64
import io
import json
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .mac_scanner import (MacScannerCancelled, MacScannerDeviceNotFound,
    MacScannerError, MacScannerService, MacScannerTimeout, MacScannerTransferError,
    MacScannerUnavailable)


MIN_DPI = 75
MAX_DPI = 1200
MAX_IMAGE_PIXELS = 150_000_000
MAX_IMAGE_BYTES = 80 * 1024 * 1024
_SCANNER_DEVICE_TYPE = 1
_WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"


class ScannerError(RuntimeError):
    """Base class for discovery, configuration, and transfer failures."""


class ScannerUnavailable(ScannerError):
    """No supported scanner integration is available on this platform."""


class ScannerDeviceNotFound(ScannerError):
    """The requested device is no longer present or is not a scanner."""


class ScannerTransferError(ScannerError):
    """A scanner transfer failed or returned invalid image data."""


@dataclass(frozen=True)
class ScannerDevice:
    device_id: str
    name: str
    manufacturer: str = ""
    description: str = ""


Runner = Callable[[str, dict[str, Any]], Any]


_PS_WORKER = r"""
$ErrorActionPreference = 'Stop'
$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
try {
    $manager = New-Object -ComObject WIA.DeviceManager
    if ($payload.operation -eq 'devices') {
        $devices = @()
        foreach ($info in $manager.DeviceInfos) {
            if ([int]$info.Type -eq 1) {
                $name = ''
                $manufacturer = ''
                $description = ''
                foreach ($prop in $info.Properties) {
                    switch ([int]$prop.PropertyID) {
                        3 { $manufacturer = [string]$prop.Value }
                        4 { $description = [string]$prop.Value }
                        7 { $name = [string]$prop.Value }
                    }
                }
                $devices += [pscustomobject]@{
                    device_id = [string]$info.DeviceID
                    name = $name
                    manufacturer = $manufacturer
                    description = $description
                }
            }
        }
        ConvertTo-Json -InputObject @($devices) -Compress -Depth 4
        exit 0
    }
    if ($payload.operation -ne 'scan') { throw 'Неизвестная операция сканера.' }
    $info = $null
    foreach ($candidate in $manager.DeviceInfos) {
        if ([string]$candidate.DeviceID -ceq [string]$payload.device_id -and [int]$candidate.Type -eq 1) {
            $info = $candidate
            break
        }
    }
    if ($null -eq $info) { throw 'Сканер не найден.' }
    $device = $info.Connect()
    if ($device.Items.Count -lt 1) { throw 'Сканер не предоставил область сканирования.' }
    $item = $device.Items.Item(1)
    foreach ($propertyId in @(6147, 6148)) {
        $property = $item.Properties.Item($propertyId)
        $property.Value = [int]$payload.dpi
    }
    $image = $item.Transfer('{_WIA_FORMAT_JPEG}')
    if ($null -eq $image) { throw 'Сканер не вернул изображение.' }
    $image.SaveFile([string]$payload.output_path)
    '{"ok":true}'
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}
""".replace("{_WIA_FORMAT_JPEG}", _WIA_FORMAT_JPEG)


class ScannerService:
    """Enumerate scanners and acquire a bounded single-page image.

    Windows uses WIA; macOS delegates to the native Image Capture adapter.
    """

    def __init__(self, *, temp_root: str | os.PathLike[str] | None = None,
                 system: str | None = None, runner: Runner | None = None,
                 timeout_seconds: int = 180) -> None:
        self._system = system or platform.system()
        self._runner = runner
        self._timeout = max(5, int(timeout_seconds))
        self._custom_temp_root = temp_root is not None
        app_root = Path(__file__).resolve().parents[1]
        workspace_tmp = (app_root / "tmp").resolve()
        if temp_root:
            candidate = Path(temp_root).resolve()
            if not candidate.is_relative_to(workspace_tmp):
                raise ValueError("Тестовые временные сканы должны храниться в папке app/tmp.")
        else:
            if os.name == "nt":
                app_data = os.environ.get("LOCALAPPDATA") or str(
                    Path.home() / "AppData" / "Local")
            elif self._system == "Darwin":
                app_data = str(Path.home() / "Library" / "Caches")
            else:
                app_data = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
            candidate = Path(app_data) / "PdfDesk" / "scanner-tmp"
        self._temp_root = candidate
        self._mac: MacScannerService | None = None

    @property
    def supported(self) -> bool:
        if self._system == "Windows":
            return True
        if self._system == "Darwin":
            return self._mac_service().supported
        return False

    @property
    def capability_message(self) -> str:
        if self._system == "Windows":
            return "Сканирование доступно через локальные устройства WIA."
        if self._system == "Darwin":
            return self._mac_service().capability_message
        return "Сканирование доступно только в Windows через WIA."

    def devices(self) -> list[ScannerDevice]:
        """Return connected scanner devices without opening an acquisition UI."""
        self._require_supported()
        if self._system == "Darwin":
            return self._mac_devices()
        payload = self._call_worker("devices", {})
        if not isinstance(payload, list):
            raise ScannerError("Служба WIA вернула неверный список устройств.")
        result: list[ScannerDevice] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("device_id") or "").strip()
            if not device_id:
                continue
            result.append(ScannerDevice(device_id=device_id,
                name=str(item.get("name") or "Сканер").strip() or "Сканер",
                manufacturer=str(item.get("manufacturer") or "").strip(),
                description=str(item.get("description") or "").strip()))
        return result

    def scan(self, device_id: str, dpi: int = 300, *,
             cancel_event: Any | None = None) -> bytes:
        """Acquire one page from a selected scanner and return validated image bytes.

        ``cancel_event`` is optional and only observed by the macOS adapter; the
        WIA worker keeps its existing synchronous transfer behaviour.
        """
        self._require_supported()
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 1024:
            raise ValueError("Выберите устройство сканирования.")
        if isinstance(dpi, bool) or not isinstance(dpi, int) or not MIN_DPI <= dpi <= MAX_DPI:
            raise ValueError("Разрешение сканирования должно быть от %d до %d dpi."
                             % (MIN_DPI, MAX_DPI))
        if self._system == "Darwin":
            return self._mac_scan(device_id, dpi, cancel_event=cancel_event)
        target = self._temporary_target()
        try:
            self._call_worker("scan", {"device_id": device_id, "dpi": dpi,
                                         "output_path": str(target)})
            try:
                size = target.stat().st_size
                if not 0 < size <= MAX_IMAGE_BYTES:
                    raise ScannerTransferError("Размер скана превышает допустимый предел.")
                data = target.read_bytes()
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ("JPEG", "PNG"):
                        raise ScannerTransferError("Сканер вернул неподдерживаемый формат.")
                    if image.width <= 0 or image.height <= 0 or \
                            image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ScannerTransferError("Размер изображения сканирования слишком велик.")
                    image.verify()
                return data
            except ScannerTransferError:
                raise
            except Exception as exc:
                raise ScannerTransferError("Сканер не создал корректный файл изображения.") from exc
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    def _require_supported(self) -> None:
        if not self.supported:
            raise ScannerUnavailable(self.capability_message)

    def _mac_service(self) -> MacScannerService:
        service = self._mac
        if service is None:
            config: dict[str, Any] = {"timeout_seconds": self._timeout}
            if self._custom_temp_root:
                config["temp_root"] = self._temp_root
            service = MacScannerService(**config)
            self._mac = service
        return service

    def _mac_devices(self) -> list[ScannerDevice]:
        try:
            found = self._mac_service().devices()
        except MacScannerError as exc:
            raise _translate_mac_error(exc) from exc
        return [ScannerDevice(device_id=device.device_id, name=device.name,
                              manufacturer=device.manufacturer,
                              description=device.description) for device in found]

    def _mac_scan(self, device_id: str, dpi: int, *,
                  cancel_event: Any | None) -> bytes:
        try:
            return self._mac_service().scan(device_id, dpi, cancel_event=cancel_event)
        except MacScannerError as exc:
            raise _translate_mac_error(exc) from exc

    def _temporary_target(self) -> Path:
        root = self._temp_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="pdfdesk_scan_", suffix=".jpg", dir=root)
        os.close(fd)
        target = Path(name).resolve()
        if not target.is_relative_to(root):
            target.unlink(missing_ok=True)
            raise ScannerError("Не удалось создать безопасный временный файл сканирования.")
        return target

    def _call_worker(self, operation: str, payload: dict[str, Any]) -> Any:
        if self._runner is not None:
            return self._runner(operation, payload)
        if os.name != "nt":
            raise ScannerUnavailable(self.capability_message)
        config = dict(payload)
        config["operation"] = operation
        encoded_json = base64.b64encode(json.dumps(config, ensure_ascii=False).encode("utf-8")).decode("ascii")
        script = _PS_WORKER.replace("__PAYLOAD__", encoded_json)
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self._timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScannerUnavailable("Не удалось запустить службу WIA: %s" % exc) from exc
        if completed.returncode != 0:
            raw_detail = (completed.stderr or completed.stdout or "").strip()
            lower_detail = raw_detail.lower()
            if "80070005" in lower_detail or "e_accessdenied" in lower_detail:
                raise ScannerUnavailable("Windows отклонила доступ к службе WIA.")
            detail = raw_detail.split("#< CLIXML", 1)[0].strip()
            if operation == "scan" and "не найден" in detail.lower():
                raise ScannerDeviceNotFound("Выбранный сканер больше не подключён.")
            raise ScannerError("Служба WIA завершилась с ошибкой%s" %
                               (": " + detail if detail else "."))
        try:
            result = json.loads(completed.stdout.strip() or "null")
        except json.JSONDecodeError as exc:
            raise ScannerError("Служба WIA вернула некорректный ответ.") from exc
        if operation == "scan":
            return result
        return result if isinstance(result, list) else []


# The macOS adapter owns its own exception hierarchy so it can be imported by
# this module without a cycle; translate it into the public scanner errors the
# dialog and callers already handle.
_MAC_ERROR_MAP: tuple[tuple[type[MacScannerError], type[ScannerError]], ...] = (
    (MacScannerUnavailable, ScannerUnavailable),
    (MacScannerDeviceNotFound, ScannerDeviceNotFound),
    (MacScannerCancelled, ScannerError),
    (MacScannerTimeout, ScannerTransferError),
    (MacScannerTransferError, ScannerTransferError),
)


def _translate_mac_error(exc: MacScannerError) -> ScannerError:
    for source, target in _MAC_ERROR_MAP:
        if isinstance(exc, source):
            return target(str(exc))
    return ScannerError(str(exc))
