"""One compact, wrapping option surface around a native radio button."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QRadioButton, QSizePolicy, QVBoxLayout


class SelectionOption(QFrame):
    """Share appearance and the whole-card target; callers own groups and values."""

    def __init__(self, title, description="", parent=None, *, reference_layout=False):
        super().__init__(parent)
        self.setProperty("selectionOption", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        if reference_layout:
            body = QVBoxLayout(self)
            body.setContentsMargins(14, 12, 14, 12)
            body.setSpacing(5)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            body.addLayout(row)
        else:
            row = QHBoxLayout(self)
            row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(8)
        self.button = QRadioButton(self)
        self.button.setAutoExclusive(False)
        self.button.setAccessibleName(title)
        self.button.setAccessibleDescription(description)
        self.button.setFixedWidth(self.button.sizeHint().width())
        self.button.setFixedHeight(self.fontMetrics().height())
        row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        text.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.title = QLabel(title, self)
        self.title.setProperty("selectionTitle", True)
        self.description = QLabel(description, self)
        self.description.setProperty("selectionHelp", True)
        for label in (self.title, self.description):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if reference_layout:
            row.addWidget(self.title, 1)
            body.addWidget(self.description)
            self.setProperty("referenceChoiceLayout", True)
        else:
            text.addWidget(self.title)
            text.addWidget(self.description)
            row.addLayout(text, 1)
        self.description.setVisible(bool(description))
        self.setFocusProxy(self.button)
        self.button.installEventFilter(self)
        self.button.toggled.connect(lambda selected: self._state("optionSelected", selected))

    def _state(self, name, value):
        if self.property(name) != value:
            self.setProperty(name, value)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._state("optionFocused", event.type() == QEvent.Type.FocusIn)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.button.isEnabled():
            self.button.setFocus(Qt.FocusReason.MouseFocusReason)
            self.button.setDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.button.isDown():
            self.button.setDown(False)
            if self.rect().contains(event.position().toPoint()):
                self.button.click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)
