"""Dialog for editing one word in a PdfDesk-generated searchable OCR layer."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from PIL import Image, ImageDraw
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QSplitter, QVBoxLayout, QWidget,
)

from .ocr_correction import (
    OCRCorrectionError, apply_corrected_words, read_page_ocr_result,
)
from .ocr_service import OCRWord


class OCRCorrectionDialog(QDialog):
    """Preview and correct a word; applying changes only the invisible OCR layer."""

    def __init__(self, session: Any, page_index: int = 0, *,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Исправить распознанный текст")
        self.setObjectName("OCRCorrectionDialog")
        self.setMinimumSize(680, 440)
        self.session = session
        self.page_index = int(page_index)
        self.page = read_page_ocr_result(session, self.page_index)
        self.words = list(self.page.result.words)
        self._selected_index = -1
        self._syncing = False

        layout = QVBoxLayout(self)
        note = QLabel(
            "Изменяется только поисковый текст. Изображение скана останется прежним.",
            self)
        note.setWordWrap(True)
        note.setProperty("role", "sub")
        layout.addWidget(note)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.word_list = QListWidget(splitter)
        self.word_list.setObjectName("OCRCorrectionWords")
        self.word_list.setAccessibleName("Распознанные слова")
        for word in self.words:
            self.word_list.addItem(word.text)
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.preview = QLabel(right)
        self.preview.setObjectName("OCRCorrectionPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 260)
        self.preview.setStyleSheet("background: #e9e9e9;")
        right_layout.addWidget(self.preview, 1)
        form = QFormLayout()
        self.text_edit = QLineEdit(right)
        self.text_edit.setObjectName("OCRCorrectionText")
        self.text_edit.setAccessibleName("Исправление слова")
        form.addRow("Текст слова", self.text_edit)
        self.box_label = QLabel("", right)
        self.box_label.setWordWrap(True)
        form.addRow("Область на странице", self.box_label)
        right_layout.addLayout(form)
        splitter.addWidget(self.word_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = QLabel("", self)
        self.status.setObjectName("OCRCorrectionStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(self)
        self.apply_button = buttons.addButton(
            "Применить исправление", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setObjectName("ApplyOCRCorrection")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.apply_correction)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.word_list.currentRowChanged.connect(self._select_word)
        self.text_edit.textChanged.connect(self._edit_text)
        self.word_list.setCurrentRow(0)

    def _render_preview(self, selected: int) -> None:
        result = self.page.result
        width, height = result.image_size
        scale = min(900 / width, 900 / height, 1.0)
        image = self.session.render(self.page_index, scale=scale,
                                    include_annotations=False).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")
        if selected >= 0:
            left, top, box_width, box_height = self.words[selected].bbox
            x1, y1 = left * image.width / width, top * image.height / height
            x2 = (left + box_width) * image.width / width
            y2 = (top + box_height) * image.height / height
            draw.rectangle((x1, y1, x2, y2), fill=(255, 214, 0, 72),
                           outline=(214, 80, 0, 230), width=max(2, round(scale * 2)))
        image = image.convert("RGB")
        qimage = QImage(image.tobytes(), image.width, image.height, image.width * 3,
                        QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimage)
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._selected_index >= 0:
            self._render_preview(self._selected_index)

    def _select_word(self, index: int) -> None:
        if not 0 <= index < len(self.words):
            return
        self._selected_index = index
        word = self.words[index]
        self._syncing = True
        self.text_edit.setText(word.text)
        self._syncing = False
        self.box_label.setText("x=%d, y=%d, ширина=%d, высота=%d пикс." % word.bbox)
        self._render_preview(index)
        self.status.clear()

    def _edit_text(self, text: str) -> None:
        if self._syncing or not 0 <= self._selected_index < len(self.words):
            return
        old = self.words[self._selected_index]
        self.words[self._selected_index] = replace(old, text=text)
        item = self.word_list.item(self._selected_index)
        item.setText(text or "(пусто)")

    def apply_correction(self) -> bool:
        try:
            apply_corrected_words(self.session, self.page_index, self.page.result,
                                  tuple(self.words))
        except Exception as exc:  # noqa: BLE001 - keep dialog open on atomic failure
            self.status.setText(str(exc) or "Не удалось применить исправление OCR.")
            return False
        self.accept()
        return True
