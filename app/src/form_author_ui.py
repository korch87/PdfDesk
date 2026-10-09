"""Small embeddable inspector for the form-authoring controller."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox, QVBoxLayout, QWidget)


class FormAuthorPanel(QWidget):
    """Field list and editable properties; the owner supplies a controller."""

    def __init__(self, controller: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.fields = QListWidget(self)
        self.name = QLineEdit(self)
        self.kind = QComboBox(self)
        self.kind.addItem("Текст", "text")
        self.kind.addItem("Многострочный текст", "multiline")
        self.kind.addItem("Флажок", "checkbox")
        self.kind.addItem("Группа переключателей", "radio")
        self.kind.addItem("Список", "choice")
        self.value = QLineEdit(self)
        self.options = QLineEdit(self)
        self.hint = QLabel("", self)
        self.readonly = QCheckBox("Только чтение", self)
        self.multiline = QCheckBox("Несколько строк", self)
        self.tab_order = QSpinBox(self)
        self.tab_order.setRange(0, 10000)
        self.tab_order.setToolTip("Позиция поля в последовательности Tab на странице")
        create = QPushButton("Создать поле", self)
        apply = QPushButton("Применить свойства", self)
        delete = QPushButton("Удалить поле", self)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Поля формы", self))
        layout.addWidget(self.fields, 1)
        props = QFormLayout()
        props.addRow("Название", self.name)
        props.addRow("Тип", self.kind)
        props.addRow("Значение", self.value)
        props.addRow("Варианты", self.options)
        props.addRow("", self.readonly)
        props.addRow("", self.multiline)
        props.addRow("Порядок Tab", self.tab_order)
        layout.addLayout(props)
        layout.addWidget(self.hint)
        buttons = QHBoxLayout()
        buttons.addWidget(create)
        select = QPushButton("Выбор", self)
        buttons.addWidget(select)
        layout.addLayout(buttons)
        layout.addWidget(delete)
        layout.addWidget(apply)
        self.fields.currentRowChanged.connect(self._select_row)
        create.clicked.connect(self._create)
        select.clicked.connect(lambda: self.controller.set_tool("select"))
        apply.clicked.connect(self._apply)
        delete.clicked.connect(self._delete)
        controller.field_selected.connect(self._select_key)
        controller.fields_changed.connect(self.refresh)
        self.name.textChanged.connect(self._remember_creation_settings)
        self.value.textChanged.connect(self._remember_creation_settings)
        self.options.textChanged.connect(self._remember_creation_settings)
        self.readonly.toggled.connect(self._remember_creation_settings)
        self.multiline.toggled.connect(self._remember_creation_settings)
        self.kind.currentIndexChanged.connect(self._remember_creation_settings)
        self.refresh()

    def refresh(self) -> None:
        selected = self.controller.selected()
        selected_key = selected.get("key") if selected else None
        pending_name = self.controller.field_name
        self.fields.blockSignals(True)
        self.fields.clear()
        for index, item in enumerate(self.controller.widgets):
            x, y, width, height = item["rect"]
            label = "%s · %s · %.0f, %.0f  %.0f×%.0f" % (
                item["kind"], item["key"].split(":", 1)[-1], x, y, width, height)
            self.fields.addItem(label)
            if selected_key == item["key"] and index == self.controller.selected_index:
                self.fields.setCurrentRow(index)
        self.fields.blockSignals(False)
        if not self.name.text():
            self.name.setText(self.controller.field_name)
        if selected is not None:
            self._select_row(self.controller.selected_index)
            if pending_name and pending_name != "Field1":
                self.name.setText(pending_name)

    def _select_row(self, row: int) -> None:
        if 0 <= row < len(self.controller.widgets):
            self.controller.selected_index = row
            item = self.controller.widgets[row]
            self.name.setText(item.get("name", item["key"].split(":", 1)[-1]))
            self.value.setText(str(item.get("default_value", item.get("value", ""))))
            self.readonly.setChecked(bool(item.get("readonly")))
            self.multiline.setChecked(bool(item.get("multiline")))
            kind = "multiline" if item["kind"] == "text" and item.get("multiline") else item["kind"]
            combo_index = self.kind.findData(kind)
            if combo_index >= 0:
                self.kind.setCurrentIndex(combo_index)
            self.options.setText(", ".join(item.get("options", [])))
            self.hint.setText("Нарисуйте прямоугольник для каждого варианта группы." if kind == "radio" else "")
            if item.get("tab_order") is not None:
                self.tab_order.setValue(int(item["tab_order"]))
            ref = self.controller.session._fields().get(item["key"])
            if ref is not None and item.get("tab_order") is None:
                target = None
                from .pdf_backend import _user_rect_to_display
                for position, (page_index, widget) in enumerate(ref.widgets):
                    if page_index != self.controller.page_index:
                        continue
                    raw = widget.get("/Rect")
                    if raw is not None:
                        displayed = _user_rect_to_display(self.controller.session._geometry(page_index),
                            float(raw[0]), float(raw[1]), float(raw[2])-float(raw[0]), float(raw[3])-float(raw[1]))
                        if all(abs(a-b) < .1 for a, b in zip(displayed, item["rect"])):
                            target = widget
                            break
                if target is not None:
                    page = self.controller.session._writer.pages[self.controller.page_index]
                    annots = self.controller.session._page_annots_array(page, create=False) or []
                    order = 0
                    for annotation in annots:
                        obj = annotation.get_object() if hasattr(annotation, "get_object") else annotation
                        if obj is target:
                            break
                        if str(obj.get("/Subtype", "")) == "/Widget":
                            order += 1
                    self.tab_order.setValue(order)
            self.controller.overlay.update()
            self.controller.set_tool("select")

    def _select_key(self, key: str, index: int) -> None:
        self.fields.setCurrentRow(index)
        self._select_row(index)

    def _create(self) -> None:
        tool = self.kind.currentData()
        option_list = [value.strip() for value in self.options.text().split(",") if value.strip()]
        self.controller.configure(name=self.name.text(), kind="text" if tool == "multiline" else tool,
            default_value=self.value.text(), readonly=self.readonly.isChecked(),
            multiline=tool == "multiline" or self.multiline.isChecked(), options=option_list)
        self.controller.set_tool(tool)

    def _remember_creation_settings(self, *_args) -> None:
        tool = self.kind.currentData()
        option_list = [value.strip() for value in self.options.text().split(",") if value.strip()]
        self.controller.configure(name=self.name.text(),
            kind="text" if tool == "multiline" else tool,
            default_value=self.value.text(), readonly=self.readonly.isChecked(),
            multiline=tool == "multiline" or self.multiline.isChecked(), options=option_list)

    def _apply(self) -> None:
        selected = self.controller.selected()
        if selected is None:
            return
        options = [value.strip() for value in self.options.text().split(",") if value.strip()]
        multiline = self.multiline.isChecked()
        widget_index = self.controller.selected_widget_index()
        self.controller.update_selected(name=self.name.text(), default_value=self.value.text(),
            readonly=self.readonly.isChecked(), multiline=multiline,
            options=options if selected["kind"] in ("choice", "radio") else None,
            tab_order=self.tab_order.value(), widget_index=widget_index)

    def _delete(self) -> None:
        if self.controller.delete_selected():
            self.refresh()
