"""Exact placement in displayed page coordinates; PDF storage remains in points."""
from __future__ import annotations

import dataclasses
import math

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                              QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout)

POINTS_PER_MM = 72 / 25.4


def transform_annotation(annotation, x, y, width, height, *, scale_font=False):
    values = (x, y, width, height)
    if not all(math.isfinite(v) for v in values) or width < 0 or height < 0:
        raise ValueError("Размер и координаты должны быть конечными; размер не отрицательный")
    if annotation.kind == "note":
        width, height = annotation.width, annotation.height
    sx = width / annotation.width if annotation.width else 1.0
    sy = height / annotation.height if annotation.height else 1.0
    if (not annotation.width and width) or (not annotation.height and height):
        raise ValueError("Нельзя растянуть объект с нулевым размером по этой оси")
    points = [(x + (px - annotation.x) * sx,
               y + (py - annotation.y) * sy) for px, py in annotation.points]
    font_size = annotation.font_size
    if scale_font and annotation.kind == "text":
        font_size = max(1.0, min(400.0, font_size * min(sx, sy)))
    return dataclasses.replace(annotation, x=x, y=y, width=width, height=height,
                               points=points, font_size=font_size)


class AnnotationGeometryDialog(QDialog):
    def __init__(self, annotation, parent=None):
        super().__init__(parent)
        self.annotation = annotation
        self.setWindowTitle("Точное размещение")
        self.setObjectName("AnnotationGeometryDialog")
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Миллиметры от левого верхнего угла страницы"))
        form = QFormLayout()
        outer.addLayout(form)
        self.inputs = {}
        for name, label in (("x", "Слева"), ("y", "Сверху"),
                            ("width", "Ширина"), ("height", "Высота")):
            spin = QDoubleSpinBox()
            spin.setObjectName("Geometry_" + name)
            spin.setDecimals(3)
            spin.setRange(-100000 if name in ("x", "y") else 0, 100000)
            spin.setSuffix(" мм")
            spin.setValue(getattr(annotation, name) / POINTS_PER_MM)
            spin.setSingleStep(1)
            spin.setEnabled(name in ("x", "y") or
                            (annotation.kind != "note" and getattr(annotation, name) > 0))
            if name in ("width", "height") and spin.isEnabled():
                spin.setMinimum(0.001)
            self.inputs[name] = spin
            form.addRow(label, spin)
        self.keep_ratio = QCheckBox("Сохранять пропорции")
        self.keep_ratio.setChecked(annotation.kind in ("image", "stamp", "text"))
        self.keep_ratio.setEnabled(annotation.kind != "note" and
                                   annotation.width > 0 and annotation.height > 0)
        outer.addWidget(self.keep_ratio)
        self.scale_font = QCheckBox("Масштабировать шрифт вместе с блоком")
        self.scale_font.setChecked(True)
        self.scale_font.setVisible(annotation.kind == "text")
        outer.addWidget(self.scale_font)
        self.inputs["width"].valueChanged.connect(lambda _: self._link_size("width", "height"))
        self.inputs["height"].valueChanged.connect(lambda _: self._link_size("height", "width"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        # Keep full precision when the dialog is accepted without changing a field.
        self.initial = {key: spin.value() for key, spin in self.inputs.items()}

    def _link_size(self, changed, other):
        if not self.keep_ratio.isEnabled() or not self.keep_ratio.isChecked():
            return
        ratio = getattr(self.annotation, other) / getattr(self.annotation, changed)
        with QSignalBlocker(self.inputs[other]):
            self.inputs[other].setValue(self.inputs[changed].value() * ratio)

    def result_annotation(self):
        values = {key: (getattr(self.annotation, key) if spin.value() == self.initial[key]
                        else spin.value() * POINTS_PER_MM)
                  for key, spin in self.inputs.items()}
        if all(values[key] == getattr(self.annotation, key) for key in values):
            return self.annotation
        return transform_annotation(self.annotation, **values,
                                    scale_font=self.scale_font.isChecked())
