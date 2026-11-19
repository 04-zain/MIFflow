"""The shared Pooled and Per slide choices for phenotype fitting."""
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QWidget

from .selection_option import SelectionOption


class FitScopeChoices(QWidget):
    """Bind the two fitting scopes to the caller's existing choice editor.

    Marker fitting and unsupervised phenotyping use the same cards, keyboard
    actions and narrow layout. The editor retains the submitted scope value.
    """

    def __init__(self, editor, parent=None, *, stacked=False, descriptions=None):
        super().__init__(parent)
        self.editor = editor
        self.cards = {}
        self._columns = None
        self._stacked = stacked
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12 if stacked else 8)
        self._group = QButtonGroup(self)
        for key, title, detail in (
            ("pooled", "Pooled", "One shared fit within each Processing group"),
            ("per_slide", "Per slide", "A separate fit for each slide"),
        ):
            detail = (descriptions or {}).get(key, detail)
            card = SelectionOption(title, detail, self, reference_layout=stacked)
            if stacked:
                card.layout().setContentsMargins(16, 15, 16, 15)
                card.layout().setSpacing(8)
            card.button.setObjectName("fit-scope-" + key)
            self._group.addButton(card.button)
            card.button.clicked.connect(lambda _checked=False, value=key:
                editor.setCurrentIndex(editor.findData(value)))
            self.cards[key] = card
        editor.currentIndexChanged.connect(self.sync)
        editor.hide()
        self.setFocusProxy(self.cards["pooled"].button)
        editor.setFocusProxy(self.focusProxy())
        self.sync()
        self._arrange()

    def sync(self, *_args):
        self._group.setExclusive(False)
        for key, card in self.cards.items():
            card.button.setChecked(self.editor.currentData() == key)
        self._group.setExclusive(True)

    def _arrange(self):
        columns = 2 if not self._stacked and self.width() >= 650 else 1
        if columns == self._columns:
            return
        self._columns = columns
        while self._layout.count():
            self._layout.takeAt(0)
        for index, card in enumerate(self.cards.values()):
            self._layout.addWidget(card, index // columns, index % columns)
        self._layout.setColumnStretch(0, 1)
        self._layout.setColumnStretch(1, 1 if columns == 2 else 0)

    def resizeEvent(self, event):
        self._arrange()
        super().resizeEvent(event)
