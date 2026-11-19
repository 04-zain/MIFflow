"""Visible phenotyping routes linked to the form's existing choice value."""
from PySide6.QtWidgets import QButtonGroup, QVBoxLayout, QWidget

from .selection_option import SelectionOption


class PhenotypingMethodChoices(QWidget):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.options = {}
        self.group = QButtonGroup(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        methods = (
            ("marker_rules", "Marker rules",
             "Assign phenotypes from combinations of marker-positive and marker-negative states, using intensity thresholds or Nimbus scores."),
            ("supervised_classifier", "Supervised phenotyping",
             "Train a classifier on labelled reference cells, then apply it to the selected cell population."),
            ("unsupervised_clustering", "Unsupervised phenotyping",
             "Group cells by similarity in selected measurements, then review the groups and assign phenotype names."),
        )
        for value, title, description in methods:
            if editor.findData(value) < 0:
                continue
            option = SelectionOption(title, description, self)
            option.button.setObjectName("phenotyping-method-" + value)
            option.button.clicked.connect(
                lambda _checked=False, selected=value:
                editor.setCurrentIndex(editor.findData(selected)))
            self.group.addButton(option.button)
            self.options[value] = option
            layout.addWidget(option)
        editor.currentIndexChanged.connect(self.sync)
        editor.hide()
        if self.options:
            first = next(iter(self.options.values())).button
            self.setFocusProxy(first)
            editor.setFocusProxy(first)
        self.sync()

    def sync(self, *_):
        self.group.setExclusive(False)
        for value, option in self.options.items():
            option.button.setChecked(value == self.editor.currentData())
        self.group.setExclusive(True)
