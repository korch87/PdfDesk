"""Чистая геометрия фигур «многоугольник» и «облако».

Модуль намеренно не зависит ни от Qt, ни от PDF-библиотек: одни и те же
функции используют холст (:mod:`editor_canvas`) и запись аннотаций в PDF
(:mod:`pdf_backend`). Благодаря этому живое изображение на странице, поток
внешнего вида ``/AP`` и печать строятся из **одной** геометрии.

Система координат — отображаемые пункты страницы (начало в левом верхнем углу,
ось ``y`` вниз), та же, что у :class:`contracts.Annotation`. Облако задаётся
контуром-«хордой» (``Annotation.points``): каждая сторона заменяется цепочкой
полукруглых «барашков», выступающих наружу. Радиус барашка подбирается так,
чтобы сторона делилась на целое число дуг, — контур детерминирован и потому
одинаков при отрисовке, сохранении и повторном открытии.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

Point = tuple[float, float]
#: Кубическая кривая Безье: начало, две контрольные точки, конец.
Cubic = tuple[Point, Point, Point, Point]

#: Радиус «барашка» облака по умолчанию, пункты.
CLOUD_BUMP_RADIUS = 7.0
#: Разумные границы радиуса: слишком мелкие дуги дают огромный поток /AP.
CLOUD_MIN_BUMP_RADIUS = 0.75
CLOUD_MAX_BUMP_RADIUS = 200.0
#: Предел числа дуг на одну сторону (защита от гигантских контуров).
CLOUD_MAX_BUMPS_PER_EDGE = 60
#: Минимальная длина стороны, которая вообще превращается в дуги.
MIN_EDGE_LENGTH = 1e-6
#: Приближение четверти окружности кубической кривой Безье (4/3·tg(π/8)).
_KAPPA = 0.5522847498307933


def _as_point(point: Sequence[float]) -> Optional[Point]:
    """Привести пару чисел к ``(float, float)``; None для мусора/NaN."""
    try:
        x = float(point[0])
        y = float(point[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def normalize_polygon(points: Iterable[Sequence[float]],
                      min_edge: float = MIN_EDGE_LENGTH) -> list[Point]:
    """Убрать мусор и повторные вершины, замкнуть контур неявно.

    Последняя вершина, совпадающая с первой, отбрасывается: контур всегда
    считается замкнутым. Незамкнутый список из трёх и более точек сохраняется
    как есть (замыкание делает вызывающий код).
    """
    result: list[Point] = []
    for raw in points or ():
        point = _as_point(raw)
        if point is None:
            continue
        if result and math.hypot(point[0] - result[-1][0], point[1] - result[-1][1]) <= min_edge:
            continue
        result.append(point)
    if len(result) > 2:
        first, last = result[0], result[-1]
        if math.hypot(last[0] - first[0], last[1] - first[1]) <= min_edge:
            result.pop()
    return result


def rectangle_points(x: float, y: float, width: float, height: float) -> list[Point]:
    """Четыре вершины прямоугольника по часовой стрелке (для облака протяжкой)."""
    x, y = float(x), float(y)
    width, height = max(float(width), 0.0), max(float(height), 0.0)
    return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]


def polygon_bounds(points: Sequence[Sequence[float]]
                   ) -> Optional[tuple[float, float, float, float]]:
    """Габариты контура ``(x, y, width, height)`` или None для пустого списка."""
    flat = normalize_polygon(points)
    if not flat:
        return None
    xs = [point[0] for point in flat]
    ys = [point[1] for point in flat]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def polygon_area(points: Sequence[Sequence[float]]) -> float:
    """Удвоенная площадь со знаком (знак задаёт ориентацию контура)."""
    flat = normalize_polygon(points)
    if len(flat) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(flat):
        nxt = flat[(index + 1) % len(flat)]
        total += point[0] * nxt[1] - nxt[0] * point[1]
    return total / 2.0


def outward_normal(start: Point, end: Point, area: float) -> Point:
    """Единичная внешняя нормаль стороны ``start → end`` для контура area."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= MIN_EDGE_LENGTH:
        return (0.0, 0.0)
    dx, dy = dx / length, dy / length
    # Ось y направлена вниз: у контура с положительной площадью внешняя
    # нормаль получается поворотом направления стороны на -90°.
    if area >= 0.0:
        return (dy, -dx)
    return (-dy, dx)


def cloud_bump_radii(points: Sequence[Sequence[float]],
                     target_radius: float = CLOUD_BUMP_RADIUS) -> list[float]:
    """Радиус барашка для каждой стороны контура (сторона делится целиком)."""
    flat = normalize_polygon(points)
    if len(flat) < 3:
        return []
    target = _clamp_radius(target_radius)
    radii: list[float] = []
    for index, start in enumerate(flat):
        end = flat[(index + 1) % len(flat)]
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        if length <= MIN_EDGE_LENGTH:
            radii.append(0.0)
            continue
        count = int(round(length / (2.0 * target)))
        count = max(1, min(CLOUD_MAX_BUMPS_PER_EDGE, count))
        radii.append(length / (2.0 * count))
    return radii


def cloud_radius(points: Sequence[Sequence[float]],
                 target_radius: float = CLOUD_BUMP_RADIUS) -> float:
    """Наибольший радиус барашка контура (0.0, если контур не построен)."""
    radii = cloud_bump_radii(points, target_radius)
    return max(radii) if radii else 0.0


def cloud_segments(points: Sequence[Sequence[float]],
                   target_radius: float = CLOUD_BUMP_RADIUS) -> list[Cubic]:
    """Замкнутый «облачный» контур как список стыкующихся кривых Безье.

    Каждая сторона контура заменяется цепочкой полукруглых дуг, выступающих
    наружу. Концы соседних кривых совпадают бит в бит (в том числе на вершинах
    контура), поэтому контур рисуется без разрывов, а все его точки лежат
    внутри габаритов :func:`cloud_bounds`.
    """
    flat = normalize_polygon(points)
    if len(flat) < 3:
        return []
    radii = cloud_bump_radii(flat, target_radius)
    area = polygon_area(flat)
    segments: list[Cubic] = []
    for index, radius in enumerate(radii):
        if radius <= MIN_EDGE_LENGTH:
            continue
        start = flat[index]
        end = flat[(index + 1) % len(flat)]
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        if length <= MIN_EDGE_LENGTH:
            continue
        dx, dy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        nx, ny = outward_normal(start, end, area)
        count = int(round(length / (2.0 * radius)))
        count = max(1, min(CLOUD_MAX_BUMPS_PER_EDGE, count))
        step = length / count
        for index_bump in range(count):
            offset = index_bump * step
            p0 = (start[0] + dx * offset, start[1] + dy * offset)
            if index_bump + 1 == count:
                # Последняя дуга стороны заканчивается ровно в её вершине.
                p1 = (float(end[0]), float(end[1]))
            else:
                next_offset = (index_bump + 1) * step
                p1 = (start[0] + dx * next_offset, start[1] + dy * next_offset)
            middle = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
            bump = math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 2.0
            if bump <= 0.0:  # pragma: no cover - защита от вырожденной стороны
                continue
            k = _KAPPA * bump
            apex = (middle[0] + nx * bump, middle[1] + ny * bump)
            segments.append((
                p0,
                (p0[0] + nx * k, p0[1] + ny * k),
                (apex[0] - dx * k, apex[1] - dy * k),
                apex,
            ))
            segments.append((
                apex,
                (apex[0] + dx * k, apex[1] + dy * k),
                (p1[0] - nx * k, p1[1] - ny * k),
                p1,
            ))
    return segments


def cloud_bounds(points: Sequence[Sequence[float]],
                 target_radius: float = CLOUD_BUMP_RADIUS,
                 pad: float = 0.0) -> Optional[tuple[float, float, float, float]]:
    """Габариты облака вместе с выступающими барашками и запасом ``pad``.

    Границы считаются по точкам уже построенного контура, поэтому любой
    нарисованный барашек гарантированно попадает внутрь (важно для ``/BBox``,
    ``/Rect`` и рамки выделения на холсте).
    """
    inset = max(float(pad), 0.0)
    segments = cloud_segments(points, target_radius)
    if not segments:
        box = polygon_bounds(points)
        if box is None:
            return None
        return (box[0] - inset, box[1] - inset,
                box[2] + 2.0 * inset, box[3] + 2.0 * inset)
    xs = [point[0] for segment in segments for point in segment]
    ys = [point[1] for segment in segments for point in segment]
    return (min(xs) - inset, min(ys) - inset,
            max(xs) - min(xs) + 2.0 * inset, max(ys) - min(ys) + 2.0 * inset)


def _clamp_radius(target_radius: float) -> float:
    try:
        value = float(target_radius)
    except (TypeError, ValueError):
        return CLOUD_BUMP_RADIUS
    if not math.isfinite(value):
        return CLOUD_BUMP_RADIUS
    return min(max(value, CLOUD_MIN_BUMP_RADIUS), CLOUD_MAX_BUMP_RADIUS)
