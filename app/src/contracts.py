"""Shared data types. Coordinates use PDF points, origin at displayed top-left."""
import math
from dataclasses import dataclass, field
from typing import Any


def _ink_pair(value: Any) -> tuple[float, float]:
    """Одна точка рисунка как пара конечных чисел."""
    try:
        px, py = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise ValueError("Точки рисунка должны быть парами чисел (x, y).") from None
    if not (math.isfinite(px) and math.isfinite(py)):
        raise ValueError("Координаты точек рисунка должны быть числами.")
    return (px, py)


def _stroke_lengths(value: Any, total: int) -> list[int]:
    """Проверить длины штрихов; пустой список — устаревший один штрих."""
    raw = list(value or [])
    if not raw:
        return []
    lengths: list[int] = []
    for item in raw:
        try:
            number = float(item)
        except (TypeError, ValueError):
            raise ValueError("Длины штрихов рисунка должны быть целыми числами.") from None
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError("Длина штриха рисунка должна быть целым числом.")
        count = int(number)
        if count < 2:
            raise ValueError("Штрих рисунка должен содержать не меньше двух точек.")
        lengths.append(count)
    if sum(lengths) != total:
        raise ValueError(
            "Границы штрихов рисунка не совпадают с точками: точек %d, по штрихам %d."
            % (total, sum(lengths)))
    return [] if len(lengths) == 1 else lengths


def normalize_ink_geometry(points: Any,
                           stroke_lengths: Any = None) -> tuple[
                               list[tuple[float, float]], list[int]]:
    """Геометрия рисунка → плоские точки и проверенные длины штрихов.

    ``points`` — плоский список пар (контракт ``Annotation``): все штрихи
    подряд, в порядке рисования. ``stroke_lengths`` — сколько точек в каждом
    штрихе; пустой список означает один непрерывный штрих (прежний формат).
    Порядок штрихов сохраняется, поэтому подъёмы пера не склеиваются.
    """
    flat = [_ink_pair(item) for item in (points or [])]
    return flat, _stroke_lengths(stroke_lengths, len(flat))


def pack_ink_strokes(strokes: Any) -> tuple[list[tuple[float, float]], list[int]]:
    """Список штрихов → плоские точки и длины штрихов.

    Каждый штрих — не меньше двух точек. Один штрих записывается пустыми
    длинами: так его читают прежние вызовы и сохранённые документы.
    """
    flat: list[tuple[float, float]] = []
    lengths: list[int] = []
    for stroke in (strokes or []):
        if isinstance(stroke, (str, bytes)) or not isinstance(stroke, (list, tuple)):
            raise ValueError("Штрих рисунка должен быть списком точек (x, y).")
        pairs = [_ink_pair(item) for item in stroke]
        if len(pairs) < 2:
            raise ValueError("Штрих рисунка должен содержать не меньше двух точек.")
        flat.extend(pairs)
        lengths.append(len(pairs))
    return normalize_ink_geometry(flat, lengths)


def ink_strokes(points: Any,
                stroke_lengths: Any = None) -> list[list[tuple[float, float]]]:
    """Штрихи рисунка для отрисовки: битые данные не роняют холст.

    Несогласованные границы (или их отсутствие) означают прежний один
    непрерывный штрих; чужие координаты не рисуются вовсе.
    """
    flat: list[tuple[float, float]] = []
    for item in (points or []):
        try:
            px, py = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return []
        if not (math.isfinite(px) and math.isfinite(py)):
            return []
        flat.append((px, py))
    if not flat:
        return []
    try:
        lengths = _stroke_lengths(stroke_lengths, len(flat))
    except ValueError:
        lengths = []
    if not lengths:
        return [flat]
    strokes: list[list[tuple[float, float]]] = []
    start = 0
    for length in lengths:
        strokes.append(flat[start:start + length])
        start += length
    return strokes


@dataclass
class Annotation:
    """Одна пометка на странице.

    ``kind``: ``text``, ``highlight``, ``ink``, ``note``, ``rect``, ``ellipse``,
    ``line``, ``arrow``, ``polygon``, ``cloud``, ``image``, ``stamp``.

    ``points`` — вершины в пунктах отображаемой страницы:
    плоская полилиния для ``ink``, две точки для ``line``/``arrow`` и контур
    для ``polygon``/``cloud`` (у облака это «хорда»: зубчатый периметр
    строится из неё модулем :mod:`shape_geometry`). Для остальных типов
    ``points`` пуст, а геометрию задают ``x/y/width/height``.

    У рисунка ``ink`` плоские ``points`` содержат все штрихи подряд, а
    ``stroke_lengths`` — сколько точек в каждом штрихе по порядку. Пустой
    список длин — прежний формат одного непрерывного штриха, поэтому старые
    вызовы и сохранённые документы работают без изменений. Разбиение по
    границам доступно свойством :attr:`strokes`; подъёмы пера не склеиваются.

    Все поля необязательные: ``Annotation()`` и прежние вызовы с именованными
    аргументами продолжают работать без изменений.
    """

    kind: str  # text, highlight, ink, note, rect, ellipse, line, arrow, polygon, cloud, image, stamp
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    text: str = ""
    color: str = "#2457D6"
    font_family: str = "Arial"
    font_size: float = 14.0
    stroke_width: float = 2.0
    opacity: float = 1.0
    points: list[tuple[float, float]] = field(default_factory=list)
    image_data: str = ""  # base64 PNG/JPEG for image/stamp annotations
    bold: bool = False
    alignment: str = "left"
    fill_color: str = ""
    #: Число точек в каждом штрихе рисунка (``ink``); пусто — один штрих.
    stroke_lengths: list[int] = field(default_factory=list)

    @property
    def strokes(self) -> list[list[tuple[float, float]]]:
        """Штрихи рисунка: ``points``, разбитые по ``stroke_lengths``.

        Для не-рисунков возвращается пустой список; для одиночного штриха —
        один список точек (в том числе когда длины пусты по совместимости).
        """
        if str(self.kind or "").strip().lower() != "ink":
            return []
        return ink_strokes(self.points, self.stroke_lengths)

    def set_strokes(self, strokes: Any) -> None:
        """Записать штрихи рисунка в ``points`` и ``stroke_lengths``."""
        flat, lengths = pack_ink_strokes(strokes)
        self.points = flat
        self.stroke_lengths = lengths


@dataclass
class FormField:
    key: str
    label: str
    kind: str  # text, checkbox, choice, unsupported
    value: str | bool = ""
    options: list[str] = field(default_factory=list)
    page_index: int = 0
    readonly: bool = False
    multiline: bool = False
