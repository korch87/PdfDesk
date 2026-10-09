"""Validated metadata access for correcting PdfDesk-owned OCR layers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable

from pypdf.generic import NameObject

from .ocr_service import LANGUAGES, OCRResult, OCRWord, create_searchable_overlay_pdf
from .ocr_apply import _marked_xobjects, apply_ocr_results


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_WORDS = 50_000
MAX_WORD_TEXT = 256


class OCRCorrectionError(ValueError):
    """The selected page has no safely editable PdfDesk OCR data."""


@dataclass(frozen=True)
class EditableOCRPage:
    page_index: int
    result: OCRResult


def _validate_word(word: Any, image_size: tuple[int, int]) -> dict[str, Any]:
    text = str(getattr(word, "text", ""))
    if (not text or len(text) > MAX_WORD_TEXT or text.strip() != text
            or any(ord(ch) < 32 for ch in text)):
        raise OCRCorrectionError("OCR содержит слово недопустимой длины или формата.")
    try:
        raw_box = tuple(getattr(word, "bbox"))
        if len(raw_box) != 4 or any(isinstance(value, bool) for value in raw_box):
            raise ValueError
        left, top, width, height = (int(value) for value in raw_box)
        if any(float(value) != int(value) for value in raw_box):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise OCRCorrectionError("OCR содержит неверную рамку слова.") from None
    image_width, image_height = image_size
    if (left < 0 or top < 0 or width <= 0 or height <= 0
            or left + width > image_width or top + height > image_height):
        raise OCRCorrectionError("Рамка слова OCR выходит за границы страницы.")
    try:
        confidence = float(getattr(word, "confidence"))
    except (TypeError, ValueError, OverflowError):
        raise OCRCorrectionError("OCR содержит неверную уверенность распознавания.") from None
    if not math.isfinite(confidence) or not 0 <= confidence <= 100:
        raise OCRCorrectionError("OCR содержит неверную уверенность распознавания.")
    return {"text": text, "bbox": [left, top, width, height],
            "confidence": confidence}


def encode_ocr_metadata(result: Any, image_size: tuple[int, int]) -> str:
    """Produce strict, bounded JSON to persist on the marked form itself."""
    if len(image_size) != 2:
        raise OCRCorrectionError("OCR не содержит размеров изображения.")
    width, height = image_size
    if (isinstance(width, bool) or isinstance(height, bool)
            or int(width) != width or int(height) != height
            or width <= 0 or height <= 0):
        raise OCRCorrectionError("OCR содержит неверный размер изображения.")
    words = tuple(getattr(result, "words", ()) or ())
    if not words or len(words) > MAX_WORDS:
        raise OCRCorrectionError("Число слов OCR пусто или превышает допустимый предел.")
    languages = tuple(dict.fromkeys(str(value) for value in
                                    (getattr(result, "languages", ()) or ())))
    if not languages or any(language not in LANGUAGES for language in languages):
        raise OCRCorrectionError("OCR содержит неизвестные языковые коды.")
    payload = {"version": 1, "image_size": [int(width), int(height)],
               "languages": list(languages),
               "words": [_validate_word(word, (int(width), int(height))) for word in words]}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OCRCorrectionError("Не удалось сохранить геометрию OCR: %s" % exc) from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise OCRCorrectionError("Метаданные OCR превышают допустимый размер.")
    return encoded


def _decode_metadata(value: Any) -> tuple[tuple[int, int], tuple[str, ...], tuple[OCRWord, ...]]:
    if value is None:
        raise OCRCorrectionError(
            "У этого слоя OCR нет данных о словах. Старый слой нельзя исправить безопасно; "
            "выполните распознавание страницы заново.")
    raw = str(value)
    if len(raw.encode("utf-8", errors="strict")) > MAX_METADATA_BYTES:
        raise OCRCorrectionError("Метаданные OCR превышают допустимый размер.")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
                "version", "image_size", "languages", "words"} or payload["version"] != 1:
            raise ValueError
        size = payload["image_size"]
        if (not isinstance(size, list) or len(size) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                       for value in size)):
            raise ValueError
        langs = payload["languages"]
        if (not isinstance(langs, list) or not langs
                or any(not isinstance(code, str) or code not in LANGUAGES for code in langs)
                or len(set(langs)) != len(langs)):
            raise ValueError
        entries = payload["words"]
        if not isinstance(entries, list) or not entries or len(entries) > MAX_WORDS:
            raise ValueError
        words = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"text", "bbox", "confidence"}:
                raise ValueError
            bbox = entry["bbox"]
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           for value in bbox)):
                raise ValueError
            word = OCRWord(entry["text"], tuple(bbox), entry["confidence"])
            _validate_word(word, (size[0], size[1]))
            words.append(word)
        return (size[0], size[1]), tuple(langs), tuple(words)
    except OCRCorrectionError:
        raise
    except (UnicodeError, TypeError, ValueError, KeyError, OverflowError):
        raise OCRCorrectionError("Метаданные слоя OCR повреждены или имеют неизвестный формат.") from None


def read_page_ocr_result(session: Any, page_index: int) -> EditableOCRPage:
    """Read only one unique, explicitly marked PdfDesk OCR Form XObject."""
    session._require_open()
    security = session.security_info
    if (security.get("encrypted")
            and security.get("authenticated_as") != "owner"
            and not bool(security.get("permissions", {}).get("extract", True))):
        raise OCRCorrectionError("Права PDF запрещают извлекать распознанный текст.")
    try:
        index = session._page_index(int(page_index))
        page = session._writer.pages[index]
    except Exception as exc:
        raise OCRCorrectionError("Страница OCR не найдена: %s" % exc) from exc
    marked = _marked_xobjects(page)
    if not marked:
        raise OCRCorrectionError("На странице нет слоя OCR PdfDesk для исправления.")
    if len(marked) != 1:
        raise OCRCorrectionError("На странице несколько слоёв OCR; безопасное исправление невозможно.")
    form = next(iter(marked.values()))
    metadata = form.get("/PdfDeskOCRMetadata")
    image_size, languages, words = _decode_metadata(metadata)
    overlay = create_searchable_overlay_pdf(words, image_size, dpi=72.0)
    return EditableOCRPage(index, OCRResult(words, image_size, languages, overlay))


def corrected_result(original: OCRResult, words: Iterable[OCRWord]) -> OCRResult:
    """Rebuild a corrected invisible overlay, retaining all other OCR data."""
    corrected_words = tuple(words)
    if len(corrected_words) != len(original.words):
        raise OCRCorrectionError("Исправление не должно добавлять или удалять слова.")
    # Validate the whole correction before handing it to the PDF writer.
    payload = OCRResult(corrected_words, original.image_size, original.languages, b"")
    encode_ocr_metadata(payload, original.image_size)
    overlay = create_searchable_overlay_pdf(corrected_words, original.image_size, dpi=72.0)
    return OCRResult(corrected_words, original.image_size, original.languages, overlay)


def apply_corrected_words(session: Any, page_index: int,
                          original: OCRResult,
                          words: Iterable[OCRWord]) -> None:
    """Atomically replace the page's PdfDesk-owned invisible OCR layer."""
    result = corrected_result(original, words)
    apply_ocr_results(session, {int(page_index): result})
