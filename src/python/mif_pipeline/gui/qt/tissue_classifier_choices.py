"""Choose the tissue feature source and classifier using the existing model enum."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class TissueClassifierChoices(QWidget):
    def __init__(self, editor, parent=None, *, cells=False):
        super().__init__(parent)
        self.setObjectName("tissue-classifier-choices")
        self.editor = editor
        self.cells = cells
        self.method = QComboBox(self)
        self.method.setObjectName(editor.objectName() + "-method")
        self.final_classifier = QComboBox(self)
        self.final_classifier.setObjectName(editor.objectName() + "-final-classifier")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._procedure = None
        self._row = row = QGridLayout()
        row.setHorizontalSpacing(22)
        row.setVerticalSpacing(6)
        row.setColumnStretch(0, 1)
        layout.addLayout(row)
        for index, (title, combo) in enumerate((("Method", self.method), ("Final classifier", self.final_classifier))):
            caption = QLabel(title, self)
            caption.setProperty("stageFieldCaption", True)
            caption.setBuddy(combo)
            combo.setAccessibleName(title)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row.addWidget(caption, index * 3, 0)
            row.addWidget(combo, index * 3 + 1, 0, Qt.AlignmentFlag.AlignTop)
            if combo is self.final_classifier:
                self.final_caption = caption
        for index in range(editor.count()):
            key = editor.itemData(index)
            if key in ("rtrees", "svm", "logistic_regression"):
                self.method.addItem(editor.itemText(index), key)
            elif key in ("kronos_rtrees", "kronos_svm", "kronos_logistic_regression"):
                head = key.removeprefix("kronos_")
                self.final_classifier.addItem(editor.itemText(editor.findData(head)), head)
        if self.final_classifier.count():
            self.method.addItem("KRONOS", "kronos")
        self.inputs = QLabel(self)
        self.inputs.setWordWrap(True)
        self.inputs.setProperty("fieldHelp", True)
        row.addWidget(self.inputs, 2, 0)
        self.parameter_mode = QLabel(self)
        self.parameter_mode.setObjectName(editor.objectName() + "-parameter-mode")
        self.parameter_mode.setWordWrap(True)
        self.parameter_mode.setProperty("panelHeading", True)
        self.parameter_mode.setContentsMargins(0, 6, 0, 0)
        self.parameter_mode.hide()
        layout.addWidget(self.parameter_mode)
        self.method.currentIndexChanged.connect(self._choose)
        self.final_classifier.currentIndexChanged.connect(self._choose)
        editor.currentIndexChanged.connect(self.sync)
        editor.hide()
        self.setFocusProxy(self.method)
        editor.setFocusProxy(self.method)
        self.sync()

    def set_procedure(self, caption, field):
        editor = caption.buddy()
        # The schema offers four procedures and supplies a default. The form's
        # generic blank placeholder is not an offered procedure.
        if editor.itemData(0) is None and editor.currentData() is not None:
            editor.removeItem(0)
        field.layout().setContentsMargins(0, 0, 0, 0)
        self._procedure = caption, field
        self._row.setRowMinimumHeight(5, 12)
        self._row.addWidget(caption, 6, 0)
        self._row.addWidget(field, 7, 0)

    def _choose(self):
        value = self.method.currentData()
        if value == "kronos":
            value = "kronos_" + self.final_classifier.currentData()
        self.editor.setCurrentIndex(self.editor.findData(value))

    def sync(self):
        value = self.editor.currentData()
        kronos = str(value).startswith("kronos_")
        with QSignalBlocker(self.method), QSignalBlocker(self.final_classifier):
            self.method.setCurrentIndex(self.method.findData("kronos" if kronos else value))
            if kronos:
                self.final_classifier.setCurrentIndex(self.final_classifier.findData(value.removeprefix("kronos_")))
        self.final_caption.setVisible(kronos)
        self.final_classifier.setVisible(kronos)
        self.method.setMinimumWidth(self.method.fontMetrics().horizontalAdvance(self.method.currentText()) + 86)
        text = ("KRONOS encodes the staining inside each cell outline for the selected classifier." if self.cells else
                "KRONOS encodes tissue patches from the selected markers. A graph neural network combines neighbouring patches before the final classifier learns the tissue classes."
                ) if self.method.currentData() == "kronos" else ""
        self.inputs.setText(text)
        self.inputs.setVisible(bool(text))

    def set_parameter_mode(self, cross_validation):
        self.parameter_mode.show()
        self.parameter_mode.setText("Candidate values" if cross_validation else "Fixed parameters")
