"""Диалог точного управления направляющими на отображаемой странице."""
from __future__ import annotations

import math
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


MM_PER_POINT = 25.4 / 72.0


class GuideDialog(QDialog):
    """Draft editor: закрытие по «Отмена» не затрагивает состояние холста.

    Args:
        page_size: displayed-page width and height in PDF points. These already
            include the page's effective rotation and crop box.
        guides: ``('v'|'h', position_points)`` pairs measured from the displayed
            page's upper-left corner.
        visible: whether the guide overlay is currently shown.
    """

    def __init__(
        self,
        page_size: tuple[float, float],
        guides: Iterable[tuple[str, float]],
        visible: bool = True,
        *,
        selected_index: int = -1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("GuideDialog")
        self.setWindowTitle("Направляющие")
        self.setModal(True)
        self.setMinimumWidth(390)
        self._page_mm = (page_size[0] * MM_PER_POINT, page_size[1] * MM_PER_POINT)
        self._guides = [(str(axis), float(position)) for axis, position in guides]
        self._selection_initial: tuple[str, float, float] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        description = QLabel(
            f"Положение от верхнего или левого края страницы · "
            f"{self._page_mm[0]:.2f} × {self._page_mm[1]:.2f} мм",
            self,
        )
        description.setWordWrap(True)
        description.setObjectName("guidePageSize")
        root.addWidget(description)

        self.list = QListWidget(self)
        self.list.setObjectName("guideList")
        self.list.setMinimumHeight(128)
        root.addWidget(self.list)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.axis = QComboBox(self)
        self.axis.setObjectName("guideAxis")
        self.axis.addItem("Вертикальная · X", "v")
        self.axis.addItem("Горизонтальная · Y", "h")
        self.position = QDoubleSpinBox(self)
        self.position.setObjectName("guidePositionMm")
        self.position.setDecimals(3)
        self.position.setSingleStep(1.0)
        self.position.setSuffix(" мм")
        self.position.setMinimum(0.0)
        self.position.setMaximum(0.0)
        form.addRow("Направление", self.axis)
        form.addRow("Положение", self.position)
        root.addLayout(form)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить", self)
        self.add_button.setObjectName("addGuide")
        self.edit_button = QPushButton("Изменить", self)
        self.edit_button.setObjectName("editGuide")
        self.remove_button = QPushButton("Удалить", self)
        self.remove_button.setObjectName("removeGuide")
        self.remove_all_button = QPushButton("Удалить все", self)
        self.remove_all_button.setObjectName("removeAllGuides")
        for button in (self.add_button, self.edit_button,
                       self.remove_button, self.remove_all_button):
            actions.addWidget(button)
        root.addLayout(actions)

        self.visible_box = QCheckBox("Показывать направляющие", self)
        self.visible_box.setObjectName("showGuides")
        self.visible_box.setChecked(bool(visible))
        root.addWidget(self.visible_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.setObjectName("guideDialogButtons")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.add_button.clicked.connect(self._add)
        self.edit_button.clicked.connect(self._edit)
        self.remove_button.clicked.connect(self._remove)
        self.remove_all_button.clicked.connect(self._remove_all)
        self.list.currentRowChanged.connect(self._selection_changed)
        self.axis.currentIndexChanged.connect(self._sync_limit)
        self._refresh_list(selected_index)
        if selected_index < 0:
            self._sync_limit()

    def _limit_mm(self, axis: str) -> float:
        return self._page_mm[0 if axis == "v" else 1]

    def _sync_limit(self, *_args) -> None:
        axis = str(self.axis.currentData())
        limit = self._limit_mm(axis)
        self.position.setMaximum(max(0.0, limit))

    def _selection_changed(self, row: int) -> None:
        if not 0 <= row < len(self._guides):
            self._selection_initial = None
            self.edit_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            return
        axis, points = self._guides[row]
        self.edit_button.setEnabled(True)
        self.remove_button.setEnabled(True)
        self.axis.setCurrentIndex(0 if axis == "v" else 1)
        self._sync_limit()
        self.position.setValue(points * MM_PER_POINT)
        self._selection_initial = (axis, points, self.position.value())

    def _refresh_list(self, selected: int = -1) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for axis, points in self._guides:
            label = "Вертикальная" if axis == "v" else "Горизонтальная"
            item = QListWidgetItem(f"{label} · {points * MM_PER_POINT:.3f} мм")
            item.setData(Qt.ItemDataRole.UserRole, (axis, points))
            self.list.addItem(item)
        self.list.setCurrentRow(selected if 0 <= selected < len(self._guides) else -1)
        self.list.blockSignals(False)
        self.remove_all_button.setEnabled(bool(self._guides))
        self._selection_changed(self.list.currentRow())

    def _add(self) -> None:
        axis = str(self.axis.currentData())
        position_mm = float(self.position.value())
        if not math.isfinite(position_mm) or not 0.0 <= position_mm <= self._limit_mm(axis):
            return
        self._guides.append((axis, position_mm / MM_PER_POINT))
        self.visible_box.setChecked(True)
        self._refresh_list(len(self._guides) - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self._guides):
            return
        axis = str(self.axis.currentData())
        position_mm = float(self.position.value())
        if not math.isfinite(position_mm) or not 0.0 <= position_mm <= self._limit_mm(axis):
            return
        initial = self._selection_initial
        if initial is not None and axis == initial[0] and position_mm == initial[2]:
            # QDoubleSpinBox displays three decimals. An explicit click on
            # «Изменить» with the untouched displayed value must retain the
            # original full-precision PDF point coordinate.
            points = initial[1]
        else:
            points = position_mm / MM_PER_POINT
        self._guides[row] = (axis, points)
        self._refresh_list(row)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._guides):
            del self._guides[row]
            self._refresh_list(min(row, len(self._guides) - 1))

    def _remove_all(self) -> None:
        self._guides.clear()
        self._refresh_list()

    def guides_points(self) -> list[tuple[str, float]]:
        return list(self._guides)

    def guides_visible(self) -> bool:
        return self.visible_box.isChecked()
