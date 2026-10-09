"""Small, geometry-driven text selection model shared by the page canvas."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

#: Прямоугольник в пунктах отображаемой страницы: ``(x1, y1, x2, y2)``.
Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class SelectableChar:
    value: str
    rect: tuple[float, float, float, float]


def hit_test(chars: Iterable[SelectableChar], x: float, y: float,
             tolerance: float = 5.0) -> int:
    """Find a glyph under/near a point, preferring actual glyph area."""
    items = tuple(chars)
    nearest = -1
    nearest_distance = float("inf")
    for index, char in enumerate(items):
        left, top, right, bottom = char.rect
        if right <= left or bottom <= top:
            continue
        dx = max(left - x, 0.0, x - right)
        dy = max(top - y, 0.0, y - bottom)
        distance = dx * dx + dy * dy
        if distance == 0:
            return index
        if distance < nearest_distance:
            nearest, nearest_distance = index, distance
    return nearest if nearest_distance <= tolerance * tolerance else -1


def selected_range(anchor: int, current: int, count: int) -> tuple[int, int]:
    """Return a half-open character range, inclusive of both drag endpoints."""
    if count <= 0 or anchor < 0 or current < 0:
        return (0, 0)
    return max(0, min(anchor, current)), min(count, max(anchor, current) + 1)


def selected_text(chars: Iterable[SelectableChar], start: int, end: int) -> str:
    return "".join(char.value for char in tuple(chars)[start:end])


# ---------------------------------------------------------------------------
# Геометрия выделенного текста
# ---------------------------------------------------------------------------
def _box_of(char: Any) -> Rect | None:
    """Достать прямоугольник глифа из ``SelectableChar``, кортежа или заглушки."""
    rect = getattr(char, "rect", char)
    try:
        values = tuple(float(value) for value in rect)
    except (TypeError, ValueError):
        return None
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        return None
    x1, y1, x2, y2 = values
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 <= 0.0 or y2 - y1 <= 0.0:
        # Пробелы, переводы строк и битые боксы не рисуют и не подсвечивают.
        return None
    return (x1, y1, x2, y2)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _vertical_run(boxes: list[Rect]) -> bool:
    """Текст идёт сверху вниз? Решает большинство шагов между соседними глифами.

    На повёрнутой странице (``/Rotate 90/270``) исходная горизонтальная строка
    отображается вертикальным набором глифов, поэтому направление набора берётся
    из геометрии, а не из предположения о письменности.
    """
    down = 0
    right = 0
    for first, second in zip(boxes, boxes[1:]):
        dx = abs(((first[0] + first[2]) - (second[0] + second[2])) / 2.0)
        dy = abs(((first[1] + first[3]) - (second[1] + second[3])) / 2.0)
        if dy > dx:
            down += 1
        elif dx > dy:
            right += 1
    return down > right


def _cross_overlap(first: Rect, second: Rect, vertical: bool) -> float:
    """Доля перекрытия по поперечной оси относительно меньшего бокса."""
    if vertical:
        low = max(first[0], second[0])
        high = min(first[2], second[2])
        smallest = min(first[2] - first[0], second[2] - second[0])
    else:
        low = max(first[1], second[1])
        high = min(first[3], second[3])
        smallest = min(first[3] - first[1], second[3] - second[1])
    if smallest <= 0.0:
        return 0.0
    return (high - low) / smallest


def _group_lines(boxes: list[Rect], vertical: bool) -> list[list[Rect]]:
    """Разложить глифы по строкам (поперёк направления набора)."""
    if vertical:
        ordered = sorted(boxes, key=lambda box: (box[0], box[1]))
    else:
        ordered = sorted(boxes, key=lambda box: (box[1], box[0]))
    lines: list[list[Rect]] = []
    for box in ordered:
        for line in lines:
            if any(_cross_overlap(item, box, vertical) >= 0.5 for item in line):
                line.append(box)
                break
        else:
            lines.append([box])
    return lines


def _union(first: Rect, second: Rect) -> Rect:
    return (min(first[0], second[0]), min(first[1], second[1]),
            max(first[2], second[2]), max(first[3], second[3]))


def _split_runs(line: list[Rect], vertical: bool, gap: float) -> list[Rect]:
    """Склеить соседние глифы строки и разорвать её на больших промежутках.

    Так одна строка таблицы с колонками даёт отдельные прямоугольники, а
    пропущенный пробел внутри слова — нет.
    """
    main = (lambda box: box[1]) if vertical else (lambda box: box[0])
    tail = (lambda box: box[3]) if vertical else (lambda box: box[2])
    ordered = sorted(line, key=main)
    runs: list[Rect] = []
    for box in ordered:
        if runs and main(box) - tail(runs[-1]) <= gap:
            runs[-1] = _union(runs[-1], box)
        else:
            runs.append(box)
    return runs


def get_selected_text_rects(chars: Iterable[SelectableChar], start: int, end: int,
                            *, gap_ratio: float = 0.6) -> tuple[Rect, ...]:
    """Прямоугольники подсветки для выделенного диапазона глифов.

    Возвращает по одному прямоугольнику на визуальную строку (несколько строк
    выделения дают несколько прямоугольников) в порядке чтения. Координаты — в
    пунктах отображаемой страницы (``(x1, y1, x2, y2)``, начало сверху-слева),
    то есть ровно в той системе, в которой холст рисует глифы и аннотации;
    обрезка страницы (``/CropBox``) и поворот (``/Rotate``) уже учтены
    источником глифов, поэтому подсветка ложится на видимый текст.

    Поддерживаются и горизонтальный, и вертикальный набор глифов.
    """
    items = tuple(chars)
    try:
        start = max(int(start), 0)
        end = min(int(end), len(items))
    except (TypeError, ValueError):
        return ()
    if end <= start:
        return ()
    boxes: list[Rect] = []
    for char in items[start:end]:
        box = _box_of(char)
        if box is not None:
            boxes.append(box)
    if not boxes:
        return ()
    if len(boxes) == 1:
        return (boxes[0],)

    vertical = _vertical_run(boxes)
    # Допустимый разрыв внутри строки: пропущенный пробел склеивается, а
    # межколоночный промежуток (~1em) — уже нет.
    thickness = _median([(box[2] - box[0]) if vertical else (box[3] - box[1])
                         for box in boxes])
    tolerance = max(0.75, float(gap_ratio) * thickness)

    rects: list[Rect] = []
    for line in _group_lines(boxes, vertical):
        rects.extend(_split_runs(line, vertical, tolerance))
    rects.sort(key=(lambda box: (box[0], box[1])) if vertical
               else (lambda box: (box[1], box[0])))
    return tuple(rects)
