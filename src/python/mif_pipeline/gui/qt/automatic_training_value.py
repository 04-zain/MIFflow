"""Present the training schema's automatic integer choice in researcher-facing terms."""
from PySide6.QtCore import QRegularExpression, QSignalBlocker
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QComboBox, QSizePolicy


class AutomaticTrainingValue(QComboBox):
    def __init__(self, editor, *, folds=False, grid=False):
        super().__init__(editor.parentWidget())
        self.editor = editor
        self.folds = folds
        self.grid = grid
        self._feature_default = None
        self._pending_label = 'Default'
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumContentsLength(10)
        self.setAccessibleName('Validation folds' if folds else 'Features considered per split')
        if folds:
            self._default_folds = 5
            for n in range(2, 11):
                self.addItem(str(n), str(n))
            self.currentIndexChanged.connect(self._choose_folds)
            # A user can pin the displayed default by choosing that same number.
            self.activated.connect(self._choose_folds)
        else:
            self.addItem('Default', '0')
            self.setEditable(True)
            self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            token = r'(?:Default|[1-9][0-9]*)'
            pattern = token + (r'(?:\s*,\s*' + token + r')*' if grid else '')
            self.setValidator(QRegularExpressionValidator(QRegularExpression(pattern), self))
            self.setCompleter(None)
            self.lineEdit().textEdited.connect(self._edited)
            self.activated.connect(lambda _index: self.editor.setText("0"))
        editor.textChanged.connect(self.sync)
        editor.setFocusProxy(self)
        editor.hide()
        self.sync()

    def _edited(self, text):
        canonical = ', '.join('0' if part.strip() == 'Default' else part.strip() for part in text.split(','))
        self.editor.setText(canonical)

    def _choose_folds(self, index):
        if index >= 0:
            self.editor.setText(self.itemData(index))

    def sync(self):
        raw = self.editor.text()
        with QSignalBlocker(self):
            if self.folds:
                display = str(self._default_folds or '') if raw.strip() == '0' else raw.strip()
                self.setPlaceholderText(display)
                self.setCurrentIndex(self.findData(display))
            else:
                display = ', '.join('Default' if part.strip() == '0' else part.strip() for part in raw.split(','))
                if raw.strip() == '0':
                    display = str(self._feature_default) if self._feature_default is not None else self._pending_label
                current = self.currentText()
                if [p.strip() for p in current.split(',')] != [p.strip() for p in display.split(',')]:
                    self.setEditText(display)

    def set_feature_count(self, count, pending_label="Default"):
        value = max(1, round(count ** 0.5)) if count and count > 0 else None
        if (value, pending_label) != (self._feature_default, self._pending_label):
            self._feature_default = value
            self._pending_label = pending_label
            self.sync()

    def set_automatic_folds(self, count):
        self._default_folds = min(5, count) if count >= 2 else None
        self.sync()
