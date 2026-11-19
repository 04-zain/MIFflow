"""Restricted model access stays with the user's browser and local filesystem."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QLayout, QVBoxLayout, QWidget, QSizePolicy
from .researcher_widgets import ContentSizedButton

KRONOS_ACCESS_URL = "https://huggingface.co/MahmoodLab/KRONOS2"

# ==============================================================================
#                                  Model access
# ==============================================================================


class KronosAccessGuide(QWidget):
    def __init__(self, field_name: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 4, 0, 5)
        layout.setSpacing(7)
        text = QLabel(
            "Request KRONOS2 access on Hugging Face using your institutional email. "
            "After approval, download the complete model folder and select it in Software setup.",
            self,
        )
        self._description = text
        text.setObjectName("kronos-package-guidance-" + field_name)
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setProperty("choiceHelp", True)
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        button = ContentSizedButton("Open KRONOS access page", self)
        button.setObjectName("kronos-access-" + field_name)
        button.setProperty("actionRole", "secondary")
        status = QLabel(self)
        status.setWordWrap(True)
        status.hide()

        def open_access():
            if not QDesktopServices.openUrl(QUrl(KRONOS_ACCESS_URL)):
                status.setText("Open this model page in your browser: " + KRONOS_ACCESS_URL)
                status.show()

        button.clicked.connect(open_access)
        layout.addWidget(text)
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(status)

    def resizeEvent(self, event):
        """Retain the full wrapped guidance when the surrounding form narrows.

        The nested form grid can allocate this guide from its minimum size before
        applying height-for-width. We update the label's minimum from its actual
        available width, then let the parent layout include every line.
        """
        super().resizeEvent(event)
        height = self._description.heightForWidth(self._description.width())
        if height > 0 and height != self._description.minimumHeight():
            self._description.setMinimumHeight(height)
