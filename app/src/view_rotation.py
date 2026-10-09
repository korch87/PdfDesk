"""Pure view-only page rotation and geometry transforms.

Coordinates use the page's canonical displayed space: points from the upper-left
corner after the PDF's CropBox and persisted /Rotate have been applied. A view
rotation is clockwise and never mutates the PDF document or its history.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from PIL import Image


def normalize_rotation(degrees: int | float) -> int:
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)):
        raise TypeError("Угол поворота должен быть числом.")
    if not float(degrees).is_integer():
        raise ValueError("Поворот страницы задаётся шагом 90 градусов.")
    normalized = int(degrees) % 360
    if normalized not in (0, 90, 180, 270):
        raise ValueError("Поворот страницы задаётся шагом 90 градусов.")
    return normalized


def rotated_size(width: float, height: float, rotation: int | float) -> tuple[float, float]:
    turn = normalize_rotation(rotation)
    return (height, width) if turn in (90, 270) else (width, height)


def rotate_point(point: Sequence[float], width: float, height: float,
                 rotation: int | float) -> tuple[float, float]:
    """Map a canonical point to the clockwise-rotated visual coordinate space."""
    if len(point) != 2:
        raise ValueError("Точка должна содержать координаты x и y.")
    x, y = float(point[0]), float(point[1])
    w, h = float(width), float(height)
    if w <= 0 or h <= 0:
        raise ValueError("Размер страницы должен быть больше нуля.")
    turn = normalize_rotation(rotation)
    if turn == 0:
        return x, y
    if turn == 90:
        return h - y, x
    if turn == 180:
        return w - x, h - y
    return y, w - x


def unrotate_point(point: Sequence[float], width: float, height: float,
                   rotation: int | float) -> tuple[float, float]:
    """Map a rotated visual point back to the canonical displayed page."""
    turn = normalize_rotation(rotation)
    visual_w, visual_h = rotated_size(width, height, turn)
    return rotate_point(point, visual_w, visual_h, (-turn) % 360)


def rotate_rect(rect: Sequence[float], width: float, height: float,
                rotation: int | float) -> tuple[float, float, float, float]:
    """Map an axis-aligned canonical rectangle to the rotated visual bounds."""
    if len(rect) != 4:
        raise ValueError("Прямоугольник должен содержать x, y, ширину и высоту.")
    x, y, w, h = (float(item) for item in rect)
    corners = (rotate_point((x, y), width, height, rotation),
               rotate_point((x + w, y), width, height, rotation),
               rotate_point((x, y + h), width, height, rotation),
               rotate_point((x + w, y + h), width, height, rotation))
    left = min(point[0] for point in corners)
    top = min(point[1] for point in corners)
    right = max(point[0] for point in corners)
    bottom = max(point[1] for point in corners)
    return left, top, right - left, bottom - top


def unrotate_rect(rect: Sequence[float], width: float, height: float,
                  rotation: int | float) -> tuple[float, float, float, float]:
    turn = normalize_rotation(rotation)
    visual_w, visual_h = rotated_size(width, height, turn)
    return rotate_rect(rect, visual_w, visual_h, (-turn) % 360)


def rotate_glyph_boxes(boxes: Iterable[Sequence[float]], width: float, height: float,
                       rotation: int | float) -> list[tuple[float, float, float, float]]:
    """Transform source-text glyph rectangles for read-only hit testing."""
    return [rotate_rect(box, width, height, rotation) for box in boxes]


def rotate_annotation_geometry(annotation: Any, width: float, height: float,
                               rotation: int | float) -> Any:
    """Return a copied annotation with its rectangle and ink points transformed."""
    result = copy.deepcopy(annotation)
    result.x, result.y, result.width, result.height = rotate_rect(
        (result.x, result.y, result.width, result.height), width, height, rotation)
    if getattr(result, "points", None):
        result.points = [rotate_point(point, width, height, rotation)
                         for point in result.points]
    return result


def rotate_image(image: Image.Image, rotation: int | float) -> Image.Image:
    """Rotate a rendered page clockwise without changing the source PDF."""
    turn = normalize_rotation(rotation)
    transpose = {
        0: None,
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }[turn]
    if transpose is None:
        return image.copy()
    return image.transpose(transpose)


@dataclass
class ViewRotation:
    """Per-view rotation state; cycles without touching document history."""

    degrees: int = 0

    def __post_init__(self) -> None:
        self.degrees = normalize_rotation(self.degrees)

    def rotate_clockwise(self) -> int:
        self.degrees = (self.degrees + 90) % 360
        return self.degrees

    def rotate_counterclockwise(self) -> int:
        self.degrees = (self.degrees - 90) % 360
        return self.degrees

    def reset(self) -> None:
        self.degrees = 0
