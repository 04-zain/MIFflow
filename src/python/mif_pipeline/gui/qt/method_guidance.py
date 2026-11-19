"""Compact method descriptions beside their owning scientific controls."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

from ..method_guidance import GuidancePoints


class MethodGuidanceCard(QFrame):
    """Show the selected calculation as one wrapping paragraph.

    The selection control already names the method. This component supplies its
    explanation without repeating a title or dividing short prose into columns.
    The content layout remains available for closely related controls.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("methodGuidance", True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet('''
            QFrame[methodGuidance="true"] { background: transparent; border: none; }
            QLabel[guidanceBody="true"] { color: #48616a; background: transparent; border: none; }
        ''')
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(0, 2, 0, 4)
        self.content.setSpacing(8)
        self.body = QLabel(self)
        self.body.setProperty("guidanceBody", True)
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.content.addWidget(self.body)
        self._text = ""

    def set_guidance(self, title: str, points: GuidancePoints | None):
        """Update the explanation and omit it when no method is selected."""
        self._text = points.text() if points else ""
        self.body.setText(self._text)
        self.setAccessibleName(title or "Method description")
        self.setAccessibleDescription(self._text)
        self.setVisible(points is not None)

    def text(self):
        """Return the displayed explanation as plain text."""
        return self._text
