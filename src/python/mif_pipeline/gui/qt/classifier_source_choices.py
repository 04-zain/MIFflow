"""Visible classifier-source choices backed by the existing form editor."""

from PySide6.QtWidgets import QButtonGroup, QVBoxLayout, QWidget

from .selection_option import SelectionOption


class ClassifierSourceChoices(QWidget):
    def __init__(self, editor, parent=None, *, allow_skip=False):
        super().__init__(parent)
        self.setObjectName("classifier-source-choices")
        self.editor = editor
        self.cards = {}
        self.group = QButtonGroup(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        descriptions = {
            "train": "Learn tissue classes from your reference annotations.",
            "saved": "Apply an accepted classifier with its recorded classes and image channels.",
            "skip": "Continue without primary tissue classification.",
        }
        for index in range(editor.count()):
            value = editor.itemData(index)
            if value is None or (value == "skip" and not allow_skip and editor.currentData() != "skip"):
                continue
            card = SelectionOption(editor.itemText(index), descriptions.get(value, ""), self)
            card.button.setObjectName("classifier-source-" + value)
            self.group.addButton(card.button)
            card.button.clicked.connect(
                lambda _checked=False, value=value: editor.setCurrentIndex(editor.findData(value))
            )
            self.cards[value] = card
            layout.addWidget(card)
        editor.hide()
        editor.currentIndexChanged.connect(self.sync)
        if self.cards:
            self.setFocusProxy(next(iter(self.cards.values())).button)
            editor.setFocusProxy(self.focusProxy())
        self.sync()

    def sync(self, *_args):
        self.group.setExclusive(False)
        for value, card in self.cards.items():
            card.button.setChecked(value == self.editor.currentData())
        self.group.setExclusive(True)
