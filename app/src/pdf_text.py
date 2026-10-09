"""Text extraction and geometry helpers for selectable/searchable PDF text."""
from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from typing import Any, Iterable

try:
    from .pdf_backend import _pdfium, _user_to_display_point
except ImportError:  # pragma: no cover - direct src imports
    from pdf_backend import _pdfium, _user_to_display_point  # type: ignore


@dataclass(frozen=True)
class TextChar:
    value: str
    rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class SearchHit:
    page: int
    text: str
    rects: tuple[tuple[float, float, float, float], ...]


def _rect_from_box(box: Iterable[float], geom: Any) -> tuple[float, float, float, float]:
    left, bottom, right, top = (float(value) for value in box)
    points = (
        _user_to_display_point(geom, left, bottom),
        _user_to_display_point(geom, left, top),
        _user_to_display_point(geom, right, bottom),
        _user_to_display_point(geom, right, top),
    )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def page_chars(session: Any, page_index: int) -> tuple[TextChar, ...]:
    """Return source text characters and tight boxes in displayed page points.

    The independent PDFium document owns all objects opened here. This avoids
    invalidating the session's render cache or interfering with form handling.
    """
    data = session._write_bytes()
    document = _pdfium.PdfDocument(data)
    page = textpage = None
    try:
        page = document[int(page_index)]
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        geom = session._geometry(session._page_index(page_index))
        chars: list[TextChar] = []
        for index, value in enumerate(text):
            try:
                box = textpage.get_charbox(index)
                rect = _rect_from_box(box, geom)
            except Exception:
                rect = (0.0, 0.0, 0.0, 0.0)
            chars.append(TextChar(value, rect))
        return tuple(chars)
    finally:
        if textpage is not None:
            textpage.close()
        if page is not None:
            page.close()
        try:
            document.close()
        except Exception:
            pass


def normalized_with_map(text: str) -> tuple[str, tuple[int, ...]]:
    """Casefold and collapse Unicode whitespace while retaining source indices."""
    normalized: list[str] = []
    source: list[int] = []
    pending_space: int | None = None
    for index, char in enumerate(text):
        if char.isspace():
            if normalized:
                pending_space = index
            continue
        if pending_space is not None:
            normalized.append(" ")
            source.append(pending_space)
            pending_space = None
        folded = unicodedata.normalize("NFC", char).casefold()
        for folded_char in folded:
            normalized.append(folded_char)
            source.append(index)
    return "".join(normalized), tuple(source)


def _merge_rects(chars: Iterable[TextChar]) -> tuple[tuple[float, float, float, float], ...]:
    rects: list[tuple[float, float, float, float]] = []
    for char in chars:
        x1, y1, x2, y2 = char.rect
        if x2 <= x1 or y2 <= y1:
            continue
        if rects:
            a1, b1, a2, b2 = rects[-1]
            same_line = abs(b1 - y1) <= max(2.0, (b2 - b1) * 0.35)
            gap = x1 - a2
            if same_line and -1.0 <= gap <= 3.0:
                rects[-1] = (a1, min(b1, y1), max(a2, x2), max(b2, y2))
                continue
        rects.append(char.rect)
    return tuple(rects)


def search_text(session: Any, query: str) -> list[SearchHit]:
    """Search each page, retaining every occurrence and its source glyph boxes."""
    needle, _ = normalized_with_map(str(query or ""))
    if not needle:
        return []
    hits: list[SearchHit] = []
    count = int(getattr(session, "page_count", 0) or 0)
    for page_index in range(count):
        chars = page_chars(session, page_index)
        source_text = "".join(char.value for char in chars)
        haystack, mapping = normalized_with_map(source_text)
        offset = 0
        while True:
            found = haystack.find(needle, offset)
            if found < 0:
                break
            source_start = mapping[found]
            source_end = mapping[found + len(needle) - 1] + 1
            selected = chars[source_start:source_end]
            hits.append(SearchHit(page_index, "".join(c.value for c in selected),
                                  _merge_rects(selected)))
            offset = found + max(1, len(needle))
    return hits
