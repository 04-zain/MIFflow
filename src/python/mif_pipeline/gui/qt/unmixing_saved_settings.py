"""Edit saved unmixing choices before requesting the existing plan revision."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea, QLineEdit, QComboBox, QCheckBox, QHBoxLayout, QStackedWidget

from ..presenters.reviews import _project_reference, _submit_reference
from .revision_summary import choice_changes
from .revision_review import RevisionReview
from .reviews_training import _BatchFormRenderer


class SavedUnmixingEditor(QWidget):
    """Saved choices are a local draft; only a fresh pending request can submit them."""

    changed_save = Signal(str, object, object)
    preview_requested = Signal(str, object, object)
    finished = Signal()

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = options
        self.saved = None
        self._busy = False
        self._change_references = False
        self._preview_values = None
        self.setObjectName("saved-unmixing-settings")
        outer = QVBoxLayout(self)
        self.steps = QLabel("Step 1 of 2 · Choose changes", self)
        self.steps.setWordWrap(True)
        outer.addWidget(self.steps)
        self.pages = QStackedWidget(self)
        outer.addWidget(self.pages, 1)
        self.choices = QWidget(self.pages)
        layout = QVBoxLayout(self.choices)
        layout.setContentsMargins(0, 0, 0, 0)
        self.pages.addWidget(self.choices)
        scope = QLabel(f"Sample {options.sample_id} · Saved unmixing settings", self)
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        self.form = _BatchFormRenderer(self._save, parent=scroll)
        self.form.set_form(_project_reference(options.saved_unmixing))
        self._baseline = self._values()
        self.form.cancelled.connect(self.finished)
        scroll.setWidget(self.form)
        layout.addWidget(scroll, 1)
        self.references = QPushButton("Change spectral references…", self)
        self.references.clicked.connect(self._references)
        self.references.setVisible(self.form._editors["spectral_state_override"].currentData() == "raw")
        actions = QHBoxLayout()
        actions.addWidget(self.references)
        self.return_button = QPushButton("Return to Workflow", self)
        self.return_button.clicked.connect(self.finished)
        actions.addWidget(self.return_button)
        self.preview_button = QPushButton("Review affected results", self)
        self.preview_button.clicked.connect(self._preview_changes)
        actions.addWidget(self.preview_button)
        self.review = RevisionReview(self.pages)
        self.pages.addWidget(self.review)
        self.summary = self.review.summary
        self.review.back.clicked.connect(self._back_to_choices)
        self.review.cancel.clicked.connect(self.finished)
        self.apply_button = self.review.apply
        self.apply_button.setProperty("actionRole", "primary")
        self.apply_button.clicked.connect(self._apply)
        self.apply_button.setEnabled(False)
        layout.addLayout(actions)
        for field in self.form._editors.values():
            if isinstance(field, QLineEdit):
                field.textChanged.connect(self._clear_impact)
            elif isinstance(field, QComboBox):
                field.currentIndexChanged.connect(self._clear_impact)
            elif isinstance(field, QCheckBox):
                field.toggled.connect(self._clear_impact)

    def _values(self):
        from mif_pipeline.preprocessing.unmixing.validation import UnmixingConfig, SolverKind, SpectralState, AfBehavior

        values = _submit_reference(self.form._form, "accept", self.form._raw_values())
        # Use the scientific configuration owner for numerical bounds before
        # retiring completed results. The new prompt still validates the source.
        UnmixingConfig(solver=SolverKind(values["solver"]),
            spectral_state_override=SpectralState(values["spectral_state_override"]),
            af_behavior=AfBehavior(values["af_behavior"]), chunk_size=tuple(values["chunk_size"]),
            output_dtype=values["output_dtype"], calculation_precision=values["calculation_precision"],
            nnls_tolerance=values["nnls_tolerance"], nnls_max_iterations=values["nnls_max_iterations"],
            overwrite=values["overwrite"], measurement_band_components=tuple(values.get("measurement_band_components", ())))
        return values

    def _back_to_choices(self):
        """Return to the retained settings without accepting the proposed change."""
        if self._busy:
            return
        self._clear_impact()
        self.pages.setCurrentWidget(self.choices)
        self.steps.setText("Step 1 of 2 · Choose changes")

    def _clear_impact(self, *_args):
        self._preview_values = None
        self.summary.clear_preview()
        self.summary.hide()
        self.apply_button.setEnabled(False)
        self.notice.clear()
        raw = self.form._editors["spectral_state_override"].currentData() == "raw"
        self.references.setVisible(raw)
        if not raw:
            self._change_references = False

    def _references(self):
        self._change_references = True
        self._preview_changes()

    def _save(self, action, raw):
        if self._busy:
            return
        try:
            values = self._values()
        except (TypeError, ValueError) as error:
            self.form._present_error(str(error), action)
            return
        if values == self._baseline and not self._change_references:
            self.finished.emit()
            return
        self._preview_changes()

    def _preview_changes(self):
        if self._busy:
            return
        self._clear_impact()
        try:
            values = self._values()
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        if values == self._baseline and not self._change_references:
            self.notice.setText("These settings match the saved unmixing choices.")
            return
        self.preview_requested.emit("reference_unmixing", "accept", dict(self.form._raw_values()))

    def show_impact(self, preview, workflow_nodes):
        from ..terminology import choice_label

        def describe(values):
            result = dict(values)
            result["spectral_state_override"] = choice_label("spectral_state_override", values["spectral_state_override"])
            mapping = values.get("measurement_band_components")
            if mapping is not None:
                context = self.form._form.context
                bands = context["measurement_band_mapping"]["bands"]
                result["measurement_band_components"] = tuple(
                    band["name"] + " → " + context["choice_labels"][f"measurement_band_component_{i}"][str(component)]
                    for i, (band, component) in enumerate(zip(bands, mapping, strict=True)))
            return result

        current, proposed = choice_changes(describe(self._baseline), describe(self._values()), {
            "spectral_state_override": "Image preparation", "measurement_band_components": "Measurement-band assignments",
            "chunk_size": "Processing block (pixels)", "nnls_tolerance": "NNLS tolerance",
            "nnls_max_iterations": "Maximum NNLS iterations", "output_dtype": "Output precision",
            "calculation_precision": "Calculation precision", "af_behavior": "Autofluorescence"})
        self.review.show_preview(preview, workflow_nodes, current or "Saved unmixing settings and spectral references.",
            (proposed + "\n" if proposed else "") + ("Reopen spectral references." if self._change_references else "Use these revised unmixing settings."))
        self.notice.setText(preview.blocked_reason or "")
        self._preview_values = self._values()
        self.apply_button.setEnabled(preview.blocked_reason is None)
        self.review.set_notice(self.notice.text())
        self.pages.setCurrentWidget(self.review)
        self.steps.setText("Step 2 of 2 · Review affected results")

    def _apply(self):
        if self._busy or self._preview_values is None:
            return
        try:
            if self._values() != self._preview_values:
                self._clear_impact()
                return
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        self.set_busy(True)
        self.changed_save.emit("reference_unmixing", "accept", dict(self.form._raw_values()))

    def set_busy(self, busy):
        self._busy = busy
        for widget in (self.form, self.references, self.return_button, self.preview_button, self.apply_button, self.review.back, self.review.cancel):
            widget.setEnabled(not busy)

    def show_error(self, message):
        self.set_busy(False)
        self._preview_values = None
        self.apply_button.setEnabled(False)
        self.notice.setText(str(message))
        self.review.set_notice(str(message))
