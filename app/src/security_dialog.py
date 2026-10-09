"""Password settings for the current PDF, without persisting credentials."""
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)


class PdfSecurityDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Защита PDF')
        self.setMinimumWidth(420)
        outer = QVBoxLayout(self)
        hint = QLabel('Пароль на открытие защищает доступ к документу. '
                      'Пароль на изменение оставляет доступными просмотр и заполнение форм.')
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        self.open_password = self._password_field('OpenPassword')
        self.open_confirmation = self._password_field('OpenPasswordConfirmation')
        self.owner_password = self._password_field('OwnerPassword')
        self.owner_confirmation = self._password_field('OwnerPasswordConfirmation')
        for caption, field in (
            ('На открытие', self.open_password), ('Повторите пароль', self.open_confirmation),
            ('На изменение', self.owner_password), ('Повторите пароль', self.owner_confirmation),
        ):
            form.addRow(caption, field)
        outer.addLayout(form)
        self.print_allowed = QCheckBox('Разрешить печать')
        self.print_allowed.setChecked(True)
        outer.addWidget(self.print_allowed)
        self.error = QLabel()
        self.error.setObjectName('SecurityValidationError')
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _password_field(name):
        field = QLineEdit()
        field.setObjectName(name)
        field.setEchoMode(QLineEdit.EchoMode.Password)
        return field

    def accept(self):
        opening = self.open_password.text()
        owner = self.owner_password.text()
        if not opening and not owner:
            self.error.setText('Укажите хотя бы один пароль.')
            return
        if opening != self.open_confirmation.text() or owner != self.owner_confirmation.text():
            self.error.setText('Повторённые пароли не совпадают.')
            return
        if opening and opening == owner:
            self.error.setText('Для открытия и изменения нужны разные пароли.')
            return
        super().accept()

    def values(self):
        return dict(open_password=self.open_password.text(),
                    owner_password=self.owner_password.text(),
                    allow_print=self.print_allowed.isChecked(), allow_fill_forms=True)
