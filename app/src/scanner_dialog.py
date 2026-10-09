"""Russian Qt dialog for asynchronous scanner discovery and import."""
from __future__ import annotations

import inspect
import threading
from typing import Any

from PySide6.QtCore import QRunnable, QThreadPool, QTimer, Signal, QObject
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout)

from .document_import import DocumentImportService
from .scanner_service import ScannerService


class _WorkerSignals(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)


class _ScannerJob(QRunnable):
    def __init__(self, operation: str, service: Any, importer: Any,
                 device_id: str = "", dpi: int = 300) -> None:
        super().__init__()
        self.operation = operation
        self.service = service
        self.importer = importer
        self.device_id = device_id
        self.dpi = dpi
        self.cancel_event = threading.Event()
        self.signals = _WorkerSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            if self.operation == "devices":
                result = self.service.devices()
            else:
                if self.cancel_event.is_set():
                    return
                image = self._scan_once()
                if self.cancel_event.is_set():
                    return
                result = self.importer.image_bytes_to_pdf(image)
            if self.cancel_event.is_set():
                return
            self.signals.succeeded.emit(self.operation, result)
        except Exception as exc:  # noqa: BLE001 - surface device/driver errors to the user
            self.signals.failed.emit(self.operation, str(exc))

    def _scan_once(self) -> bytes:
        """Pass cancellation when supported, without retrying hardware calls.

        Some tests and external integrations inject older ``scan`` callables
        that accept only ``(device_id, dpi)``. Inspect the callable before
        starting a transfer; a TypeError raised by the actual scan is reported
        as-is and can never trigger a second physical acquisition.
        """
        scan = self.service.scan
        try:
            parameters = inspect.signature(scan).parameters
        except (TypeError, ValueError):
            parameters = {}
        cancel_parameter = parameters.get("cancel_event")
        supports_cancel = (cancel_parameter is not None
                           or any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                                  for parameter in parameters.values())
                           or bool(getattr(self.service,
                                           "supports_scan_cancellation", False)))
        if not supports_cancel:
            return scan(self.device_id, dpi=self.dpi)
        if (cancel_parameter is not None
                and cancel_parameter.kind is inspect.Parameter.POSITIONAL_ONLY):
            return scan(self.device_id, self.dpi, self.cancel_event)
        return scan(self.device_id, dpi=self.dpi, cancel_event=self.cancel_event)


class ScannerDialog(QDialog):
    """Discover WIA scanners asynchronously and return an imported PDF byte stream."""

    pdf_ready = Signal(bytes)

    def __init__(self, parent=None, *, service: Any | None = None,
                 importer: Any | None = None,
                 thread_pool: QThreadPool | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Сканирование")
        self.setModal(True)
        self._service = service or ScannerService()
        self._importer = importer or DocumentImportService()
        self._pool = thread_pool or QThreadPool.globalInstance()
        self._jobs: list[_ScannerJob] = []
        self._busy = False
        self._closing = False
        self._pdf_bytes: bytes | None = None

        self.status_label = QLabel("Поиск сканеров…")
        self.status_label.setWordWrap(True)
        self.device_combo = QComboBox()
        self.device_combo.setObjectName("scannerDeviceCombo")
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setObjectName("scannerDpiSpin")
        self.dpi_spin.setRange(75, 1200)
        self.dpi_spin.setSingleStep(75)
        self.dpi_spin.setValue(300)
        self.dpi_spin.setSuffix(" dpi")
        self.refresh_button = QPushButton("Обновить")
        self.scan_button = QPushButton("Сканировать")
        self.cancel_button = QPushButton("Отмена")
        self.scan_button.setDefault(True)

        form = QFormLayout()
        form.addRow("Устройство", self.device_combo)
        form.addRow("Разрешение", self.dpi_spin)
        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.scan_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addLayout(form)
        layout.addLayout(buttons)

        self.refresh_button.clicked.connect(self.refresh_devices)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.reject)
        self.device_combo.currentIndexChanged.connect(self._update_buttons)
        if not bool(getattr(self._service, "supported", True)):
            self.status_label.setText(str(getattr(self._service, "capability_message",
                "Сканирование на этой платформе недоступно.")))
            self.refresh_button.setEnabled(False)
            self.scan_button.setEnabled(False)
        else:
            QTimer.singleShot(0, self.refresh_devices)
        self._update_buttons()

    @property
    def pdf_bytes(self) -> bytes | None:
        """PDF created from the scan after successful acceptance."""
        return self._pdf_bytes

    def refresh_devices(self) -> None:
        if self._busy or self._closing:
            return
        if not bool(getattr(self._service, "supported", True)):
            self.status_label.setText(str(getattr(self._service, "capability_message",
                "Сканирование на этой платформе недоступно.")))
            return
        self._start_job(_ScannerJob("devices", self._service, self._importer))
        self.status_label.setText("Поиск подключённых сканеров…")

    def start_scan(self) -> None:
        if self._busy or self._closing:
            return
        device_id = self.device_combo.currentData()
        if not device_id:
            self.status_label.setText("Выберите подключённый сканер.")
            return
        job = _ScannerJob("scan", self._service, self._importer,
                          str(device_id), self.dpi_spin.value())
        self._start_job(job)
        self.status_label.setText("Сканирование страницы…")

    def _start_job(self, job: _ScannerJob) -> None:
        self._busy = True
        self._jobs.append(job)
        job.signals.succeeded.connect(self._on_success)
        job.signals.failed.connect(self._on_error)
        self._update_buttons()
        self._pool.start(job)

    def _job_finished(self, operation: str) -> None:
        self._busy = False
        self._jobs = [job for job in self._jobs if job.operation != operation]
        self._update_buttons()

    def _on_success(self, operation: str, result: object) -> None:
        if self._closing:
            return
        self._job_finished(operation)
        if operation == "devices":
            self.device_combo.clear()
            for device in result or []:
                device_id = str(getattr(device, "device_id", "") or "")
                name = str(getattr(device, "name", "Сканер") or "Сканер")
                if device_id:
                    self.device_combo.addItem(name, device_id)
            if self.device_combo.count():
                self.status_label.setText("Выберите устройство и разрешение сканирования.")
            else:
                self.status_label.setText(self._no_devices_message())
            self._update_buttons()
            return
        if not isinstance(result, (bytes, bytearray)) or not result.startswith(b"%PDF-"):
            self.status_label.setText("Не удалось подготовить PDF из изображения сканера.")
            return
        self._pdf_bytes = bytes(result)
        self.pdf_ready.emit(self._pdf_bytes)
        self.accept()

    def _no_devices_message(self) -> str:
        capability = str(getattr(self._service, "capability_message", ""))
        if "image capture" in capability.lower():
            return ("Сканеры не найдены. Проверьте подключение и разрешения "
                    "для Image Capture.")
        return "Сканеры не найдены. Проверьте подключение и службу WIA."

    def _on_error(self, operation: str, message: str) -> None:
        if self._closing:
            return
        self._job_finished(operation)
        self.status_label.setText(message or "Операция сканирования завершилась с ошибкой.")

    def _update_buttons(self, *_args: object) -> None:
        enabled = not self._busy and not self._closing
        self.refresh_button.setEnabled(enabled and bool(getattr(self._service, "supported", True)))
        self.device_combo.setEnabled(enabled and self.device_combo.count() > 0)
        self.dpi_spin.setEnabled(enabled)
        self.scan_button.setEnabled(enabled and self.device_combo.count() > 0)
        self.cancel_button.setEnabled(not self._closing)

    def reject(self) -> None:
        # WIA Transfer is synchronous inside the worker and drivers do not expose
        # a portable cancellation primitive; dismissing discards any late result.
        self._closing = True
        for job in self._jobs:
            job.cancel()
        super().reject()
