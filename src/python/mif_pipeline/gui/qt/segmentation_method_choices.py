"""Segmentation method choices bound to the existing form value."""
from PySide6.QtWidgets import QButtonGroup, QVBoxLayout, QWidget

from .selection_option import SelectionOption


class SegmentationMethodChoices(QWidget):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.cards = {}
        self.group = QButtonGroup(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for key, title, description in (
            ("instanseg", "InstanSeg · nuclei and cells",
             "Predicts nuclear and whole-cell outlines from all prepared fluorescence channels."),
            ("cellpose", "Cellpose-SAM · cells",
             "Predicts whole-cell outlines from up to three selected channels showing nuclear, membrane or cytoplasmic staining."),
            ("stardist", "StarDist · nuclei",
             "Predicts nuclear outlines from the nuclear stain. Optional expansion estimates the surrounding cell regions."),
        ):
            card = SelectionOption(title, description, self)
            choice = card.button
            choice.setObjectName("segmentation-method-" + key)
            choice.setAccessibleDescription(description)
            self.group.addButton(choice)
            choice.clicked.connect(lambda _checked=False, value=key: editor.setCurrentIndex(editor.findData(value)))
            layout.addWidget(card)
            self.cards[key] = (card, choice)
        editor.hide()
        editor.currentIndexChanged.connect(self.sync)
        self.setFocusProxy(self.cards["instanseg"][1])
        editor.setFocusProxy(self.focusProxy())
        self.sync()

    def sync(self, *_args):
        self.group.setExclusive(False)
        for key, (card, choice) in self.cards.items():
            selected = key == self.editor.currentData()
            choice.setChecked(selected)
        self.group.setExclusive(True)
