"""Scaling and representation of supervised cell classifier inputs."""
import math

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWidget


class SupervisedFeaturePreparation(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("supervised-feature-preparation")
        self._last_target = ""
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(20)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.representation = QComboBox(self)
        self.representation.setObjectName("supervised-classifier-inputs")
        self.representation.setAccessibleName("Classifier inputs")
        self.representation.addItem("Individual features", "features")
        self.representation.addItem("Principal components (PCA)", "pca")
        caption = QLabel("Classifier inputs", self)
        caption.setProperty("stageFieldCaption", True)
        caption.setBuddy(self.representation)
        inputs = QVBoxLayout()
        inputs.setContentsMargins(0, 0, 0, 0)
        inputs.setSpacing(10)
        inputs.addLayout(self._field_layout(caption, self.representation))
        self.body.addLayout(inputs)
        self.representation.currentIndexChanged.connect(self._change_projection)

        self.target_group = QWidget(self)
        settings = QVBoxLayout(self.target_group)
        settings.setContentsMargins(12, 2, 0, 0)
        settings.setSpacing(12)
        inputs.addWidget(self.target_group)
        self.target_group.hide()

    @staticmethod
    def _field_layout(caption, field, parent=None):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(caption)
        layout.addWidget(field)
        return layout

    def add_control(self, name, caption, field):
        editor = caption.buddy()
        field.layout().setContentsMargins(0, 0, 0, 0)
        if name.endswith("_normalization"):
            for value, title in (
                ("none", "Keep feature values unchanged"),
                ("mean_variance", "Standardize each feature (z-scoring)"),
                ("min_max", "Scale each feature to 0–1 (min–max)"),
            ):
                index = editor.findData(value)
                if index >= 0:
                    editor.setItemText(index, title)
            self.body.insertLayout(0, self._field_layout(caption, field))
            QWidget.setTabOrder(editor, self.representation)
            return

        if name.endswith("_pca_normalize"):
            caption.setText("Component scaling")
            editor.setAccessibleName(caption.text())
            for value, title in (("no", "Keep component variances"), ("yes", "Whiten to unit variance")):
                editor.setItemText(editor.findData(value), title)
            group = self._field_layout(caption, field)
            help_text = self._help_label()
            group.addWidget(help_text)
            def explain_scaling():
                if editor.currentData() == "yes":
                    help_text.setText(
                        "Whitening divides each component’s scores by its standard deviation "
                        "in the training data. The resulting scores have unit variance."
                    )
                else:
                    help_text.setText(
                        "The classifier uses the original component scores. Their spread "
                        "reflects how much training variance each component explains."
                    )
            editor.currentIndexChanged.connect(explain_scaling)
            explain_scaling()
            self.target_group.layout().addLayout(group)
            return

        self.target_name = name
        self.target = editor
        caption.setText("Target fraction of training variance")
        editor.setAccessibleName(caption.text())
        editor.setAccessibleDescription("Enter a fraction greater than 0 and at most 1.")
        editor.setPlaceholderText("Greater than 0 and up to 1")
        target_layout = self._field_layout(caption, field)
        help_text = self._help_label()
        help_text.setText(
            "PCA adds components in order of decreasing variance until together they explain "
            "this fraction of the total variance in the training data."
        )
        target_layout.addWidget(help_text)
        self.target_group.layout().insertLayout(0, target_layout)
        QWidget.setTabOrder(self.representation, editor)
        self.restore()

    def _change_projection(self):
        mode = self.representation.currentData()
        enabled = mode != "features"
        if enabled:
            if self.target_group.isHidden():
                self.target.setText(self._last_target)
        else:
            self._last_target = self.target.text()
            self.target.setText("0")
        self.target_group.setVisible(enabled)

    def _help_label(self):
        label = QLabel(self.target_group)
        label.setProperty("fieldHelp", True)
        label.setWordWrap(True)
        return label

    def capture(self):
        enabled = self.representation.currentData() != "features"
        return {"pca": enabled, "target": self.target.text() if enabled else self._last_target}

    def restore(self, state=None):
        raw = self.target.text()
        try:
            enabled = float(raw) != 0
        except ValueError:
            enabled = True
        if state is not None:
            enabled = state.get("pca", enabled)
        self._last_target = raw if enabled else (state or {}).get("target", "0.95")
        with QSignalBlocker(self.representation):
            self.representation.setCurrentIndex(self.representation.findData("pca" if enabled else "features"))
        self.target_group.setVisible(enabled)

    def validation_error(self):
        if self.representation.currentData() != "features":
            try:
                value = float(self.target.text())
            except ValueError:
                value = 0
            if not math.isfinite(value) or not 0 < value <= 1:
                return "Enter a variance target greater than 0 and at most 1."
        return None
