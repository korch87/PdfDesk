"""Asynchronous user workflow for adding searchable OCR layers to scans.

The dialog does not mutate the open PDF. It returns page-indexed invisible-text
overlay PDFs for the owning window/session to review and apply as one operation.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import sys
from typing import Any, Callable, Sequence

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from . import ocr_service
from .pdf_backend import PdfSession


OCR_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("Русский", "rus"),
    ("Английский", "eng"),
    ("Украинский", "ukr"),
    ("Польский", "pol"),
    ("Грузинский", "kat"),
    ("Сербский (кириллица)", "srp"),
    ("Сербский (латиница)", "srp_latn"),
    ("Мальтийский", "mlt"),
)
OCR_RASTER_SCALE = 1.5
OCR_DPI = 72.0 * OCR_RASTER_SCALE


@dataclass(frozen=True)
class OCRPageOutput:
    """One page's recognition and searchable text overlay in display coordinates."""

    page_index: int
    result: ocr_service.OCRResult
    display_size_points: tuple[float, float]


@dataclass(frozen=True)
class OCRDialogResult:
    pages: tuple[OCRPageOutput, ...]
    languages: tuple[str, ...]


class _OCRWorker(QThread):
    progress = Signal(int, int)

    def __init__(self, pdf_bytes: bytes, title: str, pages: Sequence[int],
                 page_sizes: Sequence[tuple[float, float]], languages: Sequence[str],
                 service: Any, cancel_event: threading.Event,
                 session_factory: Callable[[], Any]) -> None:
        super().__init__()
        self.pdf_bytes = bytes(pdf_bytes)
        self.title = str(title)
        self.pages = tuple(int(page) for page in pages)
        self.page_sizes = tuple((float(width), float(height))
                                for width, height in page_sizes)
        self.languages = tuple(str(language) for language in languages)
        self.service = service
        self.cancel_event = cancel_event
        self.session_factory = session_factory
        self.outcome: OCRDialogResult | None = None
        self.error: str | None = None
        self.cancelled = False

    def run(self) -> None:
        outputs: list[OCRPageOutput] = []
        session = None
        try:
            session = self.session_factory()
            session.open_bytes(self.pdf_bytes, title=self.title)
            for position, page_index in enumerate(self.pages):
                if self.cancel_event.is_set():
                    self.cancelled = True
                    break
                if page_index >= len(self.page_sizes):
                    raise RuntimeError("Не удалось определить размер страницы %d" %
                                       (page_index + 1))
                image = session.render(page_index, OCR_RASTER_SCALE,
                                       include_annotations=False)
                if image is None or getattr(image, "width", 0) <= 0:
                    raise RuntimeError("Не удалось подготовить страницу %d для OCR" %
                                       (page_index + 1))
                result = self.service.recognize(image, self.languages, dpi=OCR_DPI)
                if self.cancel_event.is_set():
                    self.cancelled = True
                    break
                outputs.append(OCRPageOutput(
                    page_index=page_index,
                    result=result,
                    display_size_points=self.page_sizes[page_index],
                ))
                self.progress.emit(position + 1, len(self.pages))
            if self.cancel_event.is_set():
                self.cancelled = True
            if not self.cancelled:
                self.outcome = OCRDialogResult(tuple(outputs), self.languages)
        except Exception as exc:  # noqa: BLE001 - show service failures in the dialog
            self.error = str(exc) or "Неизвестная ошибка OCR"
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - cleanup must not hide OCR result
                    pass


class OCRDialog(QDialog):
    """Choose languages/page scope, run OCR, and return overlays on success.

    Cancellation is responsive between pages. A running Tesseract call has its
    own service timeout and is allowed to return; its result is then discarded.
    """

    def __init__(self, session: Any, current_page: int = 0, *, service: Any = None,
                 parent: QWidget | None = None,
                 session_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Распознавание текста (OCR)")
        self.setObjectName("OCRDialog")
        self.setMinimumWidth(430)
        self.session = session
        self.current_page = max(0, int(current_page))
        self.service = service or ocr_service.TesseractOCRService()
        self.session_factory = session_factory or PdfSession
        self.result: OCRDialogResult | None = None
        self._worker: _OCRWorker | None = None
        self._cancel_event: threading.Event | None = None
        self._cancel_requested = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("Создать поисковый слой для скана", self)
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        note = QLabel(
            "Распознанные слова сохраняются как невидимый текст поверх страницы. "
            "Исходный PDF не изменяется до применения результата.", self
        )
        note.setWordWrap(True)
        note.setProperty("role", "sub")
        layout.addWidget(note)

        self.languages = QListWidget(self)
        self.languages.setObjectName("OCRLanguageList")
        self.languages.setAccessibleName("Языки распознавания")
        self.languages.setMaximumHeight(190)
        availability = self._language_availability()
        missing_languages: list[str] = []
        for label, code in OCR_LANGUAGES:
            is_available = availability.get(code, True)
            item = QListWidgetItem("%s (%s)%s" % (
                label, code, " — модель не найдена" if not is_available else ""))
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if code == "rus"
                               else Qt.CheckState.Unchecked)
            if not is_available:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            if not is_available:
                item.setToolTip("Не установлена модель %s.traineddata" % code)
                missing_languages.append(code)
            self.languages.addItem(item)
        layout.addWidget(self.languages)

        self.current_page_option = QRadioButton(
            "Текущая страница (%d)" % (self.current_page + 1), self
        )
        self.current_page_option.setObjectName("OCRCurrentPage")
        self.all_pages_option = QRadioButton("Все страницы документа", self)
        self.all_pages_option.setObjectName("OCRAllPages")
        self.current_page_option.setChecked(True)
        scope = QVBoxLayout()
        scope.addWidget(self.current_page_option)
        scope.addWidget(self.all_pages_option)
        layout.addLayout(scope)

        self.status = QLabel("", self)
        self.status.setObjectName("OCRStatus")
        self.status.setWordWrap(True)
        self.status.setProperty("role", "hint")
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(self)
        self.run_button = buttons.addButton("Распознать", QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_button.setObjectName("RunOCR")
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("CancelOCR")
        buttons.accepted.connect(self.start_recognition)
        buttons.rejected.connect(self.request_cancel)
        layout.addWidget(buttons)

        page_count = int(getattr(session, "page_count", 0) or 0)
        self.current_page_option.setEnabled(page_count > 0)
        self.all_pages_option.setEnabled(page_count > 0)
        if page_count <= 0:
            self.status.setText("В документе нет страниц для распознавания.")
            self.run_button.setEnabled(False)
        elif not bool(getattr(self.service, "available", True)):
            self.status.setText(self._engine_missing_message())
            self.run_button.setEnabled(False)
        elif missing_languages:
            self.status.setText(self._missing_language_message(missing_languages))

    @staticmethod
    def _engine_missing_message() -> str:
        if sys.platform == "darwin":
            return (
                "Tesseract не найден. Установите Tesseract OCR и языковые модели "
                "вручную либо укажите путь через PDFDESK_TESSERACT и папку моделей "
                "через TESSDATA_PREFIX или PDFDESK_TESSDATA_DIR. Затем повторите OCR."
            )
        return ("Tesseract не найден. Запустите scripts/setup-ocr.ps1 в папке "
                "проекта, чтобы установить локальный движок и языковые пакеты, либо укажите "
                "PDFDESK_TESSERACT и TESSDATA_PREFIX. После установки повторите "
                "распознавание.")

    @staticmethod
    def _missing_language_message(missing: list[str]) -> str:
        languages = ", ".join(missing)
        if sys.platform == "darwin":
            return ("Не найдены модели: %s. Добавьте нужные .traineddata в папку "
                    "tessdata и задайте её через TESSDATA_PREFIX или "
                    "PDFDESK_TESSDATA_DIR. Можно выбрать доступные языки для "
                    "смешанного текста на странице." % languages)
        return ("Не найдены модели: %s. Установите все или нужные языки через "
                "scripts/setup-ocr.ps1; выберите доступные языки для смешанного "
                "текста на странице." % languages)

    def _language_availability(self) -> dict[str, bool]:
        checker = getattr(self.service, "language_availability", None)
        if not callable(checker):
            return {code: True for _, code in OCR_LANGUAGES}
        try:
            values = checker()
            return {code: bool(values.get(code, False)) for _, code in OCR_LANGUAGES}
        except Exception:  # noqa: BLE001 - diagnostic UI should remain available
            return {code: False for _, code in OCR_LANGUAGES}

    @property
    def outputs(self) -> tuple[OCRPageOutput, ...]:
        return self.result.pages if self.result is not None else ()

    def selected_languages(self) -> tuple[str, ...]:
        values = []
        for index in range(self.languages.count()):
            item = self.languages.item(index)
            if (item.flags() & Qt.ItemFlag.ItemIsEnabled
                    and item.checkState() == Qt.CheckState.Checked):
                values.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return tuple(values)

    def selected_pages(self) -> tuple[int, ...]:
        page_count = int(getattr(self.session, "page_count", 0) or 0)
        if self.all_pages_option.isChecked():
            return tuple(range(page_count))
        if self.current_page < page_count:
            return (self.current_page,)
        return ()

    def start_recognition(self) -> bool:
        if self._worker is not None and self._worker.isRunning():
            return False
        languages = self.selected_languages()
        pages = self.selected_pages()
        if not languages:
            self.status.setText("Выберите хотя бы один язык OCR.")
            return False
        if not pages:
            self.status.setText("Нет страниц для распознавания.")
            return False
        try:
            # Snapshot writer state and display geometry while still on the UI
            # thread. PDFium work is then isolated in a fresh worker-owned session.
            snapshot = self.session._make_snapshot()
            page_count = int(getattr(self.session, "page_count", 0) or 0)
            page_sizes = tuple(self.session.page_size(page) for page in range(page_count))
            title = str(getattr(self.session, "title", "Документ.pdf") or "Документ.pdf")
            pdf_bytes = bytes(snapshot.data)
        except Exception as exc:  # noqa: BLE001 - reject before starting a worker
            self.status.setText("Не удалось подготовить PDF для OCR: %s" % exc)
            return False
        self.result = None
        self._cancel_requested = False
        self._cancel_event = threading.Event()
        worker = _OCRWorker(pdf_bytes, title, pages, page_sizes, languages, self.service,
                            self._cancel_event, self.session_factory)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(lambda worker=worker: self._on_worker_finished(worker))
        self._worker = worker
        self._set_running(True)
        self.status.setText("Подготовка страниц для распознавания…")
        worker.start()
        return True

    def request_cancel(self) -> None:
        worker = self._worker
        if worker is None or not worker.isRunning():
            super().reject()
            return
        self._cancel_requested = True
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.status.setText(
            "Остановка после текущей страницы… Результат этого запуска будет отменён."
        )

    def reject(self) -> None:
        """Keep the dialog alive until its worker stops touching the session."""
        if self._worker is not None and self._worker.isRunning():
            self.request_cancel()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._worker is not None and self._worker.isRunning():
            self.request_cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _on_progress(self, completed: int, total: int) -> None:
        if self._cancel_requested:
            return
        self.status.setText("Распознано страниц: %d из %d" % (completed, total))

    def _set_running(self, running: bool) -> None:
        self.languages.setEnabled(not running)
        self.current_page_option.setEnabled(not running)
        self.all_pages_option.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Остановить" if running else "Отмена")

    def _on_worker_finished(self, worker: _OCRWorker) -> None:
        if self._worker is not worker:
            return
        self._worker = None
        worker.deleteLater()
        self._set_running(False)
        if self._cancel_requested or worker.cancelled:
            self.result = None
            self.status.setText("Распознавание отменено; результат не применён.")
            super().reject()
            return
        if worker.error:
            self.result = None
            self.status.setText(self._actionable_error(worker.error))
            return
        self.result = worker.outcome
        if self.result is None or not self.result.pages:
            self.status.setText("Текст не распознан на выбранных страницах.")
            return
        super().accept()

    @staticmethod
    def _actionable_error(message: str) -> str:
        detail = str(message or "Неизвестная ошибка OCR")
        if "языков" in detail.lower() or "language" in detail.lower():
            return ("Не хватает языковых пакетов Tesseract: %s. Установите "
                    "указанные пакеты и повторите распознавание." % detail)
        if "tesseract" in detail.lower() or "не установлен" in detail.lower():
            return "%s Установите Tesseract OCR и повторите распознавание." % detail
        return "Не удалось распознать документ: %s" % detail
