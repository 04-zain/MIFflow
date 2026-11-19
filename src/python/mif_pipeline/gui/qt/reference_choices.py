"""Visible, mutually exclusive spectral-reference source choices."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QFrame,
    QRadioButton,
    QLabel,
    QButtonGroup,
    QSizePolicy,
)
from ..reference_copy import SOURCE_CHOICES

# ==============================================================================
#                               Reference choices
# ==============================================================================


class SpectralSourceChoices(QWidget):
    choice_activated = Signal(str)

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self.setObjectName("spectral-source-choices")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        group = QButtonGroup(self)
        group.idClicked.connect(lambda index: self.choice_activated.emit(combo.itemData(index)))
        for index in range(1, combo.count()):
            value = combo.itemData(index)
            title, description = SOURCE_CHOICES.get(value, (combo.itemText(index), ""))
            card = QFrame(self)
            card.setProperty("sourceChoice", True)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            body = QVBoxLayout(card)
            body.setContentsMargins(14, 12, 14, 12)
            body.setSpacing(5)
            body.setAlignment(Qt.AlignmentFlag.AlignTop)
            button = QRadioButton(title, card)
            button.setObjectName("spectral-source-" + str(value))
            button.setAccessibleName(title)
            group.addButton(button, index)
            button.setChecked(combo.currentIndex() == index)
            button.toggled.connect(
                lambda checked, i=index: combo.setCurrentIndex(i) if checked else None
            )
            body.addWidget(button)
            if description:
                help = QLabel(description, card)
                help.setTextFormat(Qt.TextFormat.PlainText)
                help.setWordWrap(True)
                help.setProperty("choiceHelp", True)
                body.addWidget(help)
            layout.addWidget(card)
