"""Shared, content-sized sections for researcher-facing settings dialogs."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QGridLayout,
    QWidget,
    QScrollArea,
    QSizePolicy,
    QLayout,
)

# ==============================================================================
#                                 Form sections
# ==============================================================================


def label(text, parent=None, *, role=None, name=None):
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if role:
        widget.setProperty(role, True)
    if name:
        widget.setObjectName(name)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    return widget


class FormSection(QFrame):
    def __init__(self, title, description="", parent=None):
        super().__init__(parent)
        self.setProperty("workbenchSection", True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 18)
        self.body.setSpacing(12)
        self.body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body.addWidget(label(title, self, role="panelHeading"))
        if description:
            self.body.addWidget(label(description, self, role="choiceHelp"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(14)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        self.body.addLayout(self.grid)
        self._row = 0

    def field(self, key, title, editor, explanation, *, column=0, span=2, prefix="compute"):
        field = QWidget(self)
        field.setProperty("workbenchField", True)
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        box.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        box.setAlignment(Qt.AlignmentFlag.AlignTop)
        caption = label(title, field, role="fieldCaption")
        caption.setBuddy(editor)
        editor.setObjectName(prefix + "-" + key)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        box.addWidget(caption)
        box.addWidget(editor)
        if explanation:
            box.addWidget(
                label(explanation, field, role="choiceHelp", name=prefix + "-" + key + "-help")
            )
        self.grid.addWidget(field, self._row, column, 1, span)
        if span == 2 or column == 1:
            self._row += 1
        return field


def scroll_content(parent=None):
    scroll = QScrollArea(parent)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    content = QWidget(scroll)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(12, 16, 12, 16)
    layout.setSpacing(14)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
    scroll.setWidget(content)
    return scroll, content, layout


class WorkbenchMessage(QFrame):
    """Compact status presentation compatible with the dialog's existing status calls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("workbenchMessage", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.text = label("", self)
        layout.addWidget(self.text)
        self.hide()

    def setPlainText(self, text):
        self.text.setText(str(text))
        self.setVisible(bool(text))

    def toPlainText(self):
        return self.text.text()
