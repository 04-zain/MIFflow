"""Review section options or return to a completed decision and their shared downstream impact."""
from __future__ import annotations
from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QScrollArea,
    QPushButton, QWidget, QHBoxLayout, QLineEdit, QComboBox,
    QCheckBox, QRadioButton, QAbstractSpinBox, QTextEdit, QPlainTextEdit, QTableWidget, QStackedWidget,
)
from mif_pipeline.gui.segmentation_setup import build_segmentation_options_form
from mif_pipeline.pipeline_core.config import SegmentationConfig
from mif_pipeline.orchestration.batch_settled_review_revision import SETTLED_REVIEW_KINDS, SettledReviewRevision
from mif_pipeline.orchestration.batch_stage_settings import (
    BatchStageOptions, BatchStageRevisionPreview,
)
from .revision_summary import CHANGE_SUBJECTS, stage_choices, source_choices
from .revision_review import RevisionReview
from .forms import FormRenderer
from .measurement_options import MeasurementOptionsEditor
from .error_copy import operation_error_summary
from .run_detail import open_run_diagnostics


class StageOptionsDialog(QDialog):
    """Review saved selections and the later results affected by their replacement."""
    preview_requested = Signal(object)
    apply_requested = Signal(object)
    remove_cells_requested = Signal()

    def __init__(self, options: BatchStageOptions, workflow_nodes, parent=None,
                 *, scope_label: str | None = None, remote=False, mounted_images=None,
                 inspection_action='change_source'):
        """Seed the form from saved settings and keep its impact preview tied to that selection.

        A readable sample/section label identifies the edited scope; exact task
        and section identities remain in the supplied options. We enable Apply
        only after a preview for those settings has been received.
        """
        super().__init__(parent)
        self.options = options
        self._remote = remote
        self._nodes = {node.node_id: node for node in workflow_nodes}
        self._preview = None
        self._preview_pending = False
        self._applying = False
        self._diagnostic = ""
        self.setObjectName("stage-options-dialog")
        self.setWindowTitle("Change source image" if options.kind == "inspection" else "Change " + CHANGE_SUBJECTS[options.kind])
        if options.kind in {'clustering_per_slide','clustering_pooled'}:
            self.setWindowTitle('Recluster selected groups')
        self.resize(1080 if options.kind == "measurements" else 760,
                    650 if options.kind in {"measurements", "segmentation", "registration",'clustering_per_slide','clustering_pooled'} else 340)
        if options.kind in {'clustering_per_slide','clustering_pooled'}:
            self.resize(760,min(820,max(420,self.screen().availableGeometry().height()-80)))
        self._choices_height = self.height()
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        heading = QLabel(self.windowTitle(), self)
        heading.setProperty("dialogHeading", True)
        layout.addWidget(heading)
        scope = QLabel(f"Sample {options.sample_id}" if options.kind == "inspection" else scope_label or f"Sample {options.sample_id} · " + (
            "Original image and series" if options.kind == "inspection"
            else "Accepted tissue sections" if options.kind == "analysis_domain_selection"
            else "Selected image" if options.kind in {"tissue_setup", "registration", "reference_unmixing"} else "Selected tissue section"), self)
        scope.setWordWrap(True)
        scope.setProperty("choiceHelp", True)
        layout.addWidget(scope)
        self._review_only = options.kind in SETTLED_REVIEW_KINDS and options.kind not in {"registration", "inspection",'clustering_per_slide','clustering_pooled'}
        self.steps = QLabel("<b>1 · Choose changes</b>     /     2 · Review impact", self)
        self.steps.setWordWrap(True)
        self.steps.setProperty("choiceHelp", True)
        self.steps.setVisible(not self._review_only)
        layout.addWidget(self.steps)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages, 1)
        self.choices = QWidget(self.pages)
        choices_layout = QVBoxLayout(self.choices)
        choices_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self.choices)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        if options.kind == "inspection":
            from .source_options import SourceOptionsEditor
            self.form = SourceOptionsEditor(options.settings, scroll, mounted_images=mounted_images if remote else None,
                inspection_review=options.inspection_review, inspection_action=inspection_action)
            def inspection_heading():
                title = ('Review cycle assignments' if self.form.action.currentData() == 'review_acquisition_cycles'
                         else 'Change source image')
                self.setWindowTitle(title)
                heading.setText(title)
            self.form.action.currentIndexChanged.connect(inspection_heading)
            inspection_heading()
            scroll.setWidget(self.form)
        elif options.kind == "segmentation":
            self.form = FormRenderer(parent=scroll)
            rendered = build_segmentation_options_form(options.settings, channels=options.segmentation_channels,
                nuclear_channel_id=options.segmentation_nuclear_channel_id)
            if remote:
                from dataclasses import replace
                rendered = replace(rendered, context={**rendered.context,
                    "remote_execution": True, "device": options.settings.device})
            self.form.set_form(rendered)
            self.form.submission_ready.connect(self._request_preview)
            self.form.cancelled.connect(self.reject)
            scroll.setWidget(self.form)
        elif options.kind == "registration":
            from .paired_image_options import PairedImageOptionsEditor
            self.form = PairedImageOptionsEditor(options.settings, options.sample_id, scroll,
                mounted_images=mounted_images if remote else None)
            scroll.setWidget(self.form)
        elif options.kind in {'clustering_per_slide','clustering_pooled'}:
            from .cluster_refit_options import ClusterRefitOptionsEditor
            self.form=ClusterRefitOptionsEditor(options,scroll)
            scroll.setWidget(self.form)
        elif options.kind in SETTLED_REVIEW_KINDS:
            self.form = QWidget(scroll)
            form_layout = QVBoxLayout(self.form)
            notes = {
                "channel_review": "Review the fluorophore, marker and nuclear-stain assignments.",
                "phenotyping_mode_resolution": "Choose marker rules, supervised phenotyping or unsupervised phenotyping for the existing cells in these analysis regions.",
                "unsupervised_preparation": "Change the measurements, Pooled or Per slide fitting, method or its settings. Accepting the revised settings recalculates clusters and replaces their labels and exports.",
                "cluster_label_review": "Change phenotype names and which clusters are included in phenotyping. This updates labels and exports without recalculating clusters.",
                "analysis_domain_selection": "Choose whole tissue or change the regions to analyze. Review region edits before accepting them; affected cells and phenotype results will need recalculation.",
                "marker_combination_selection": "Change phenotype names, marker combinations, assignment order or the unmatched label using the accepted positive/negative marker decisions.",
                "marker_gate_review": "Change marker methods and thresholds for the existing cells. The saved Pooled or Per slide choice is selected initially. Review and confirm each marker's threshold.",
                "tissue_resolution": "Change the image resolution for tissue detection, then review the newly detected tissue.",
                "reference_unmixing": "Review the saved unmixing settings. The previous reference library can be reused when the intensity correction is unchanged.",
                "tissue_setup": "Choose or import a tissue detector, then select its image resolution and review the detected tissue.",
                "tissue_candidate": "Detect tissue again with the current image, detector and resolution, then review the detected sections. Open QuPath to edit their outlines.",
                "primary_classifier_training_plan_review": "Change the primary classifier method, channels, image features and classes, and how subjects are divided for fitting, validation and testing. The revised classifier must be fitted again.",
                "secondary_structure_definition": "Change the structure names and subtypes, detection areas, classifier channels, review measurements and structure-count limits. The saved choices are filled in for editing.",
                "secondary_classifier_training_plan_review": "Change the secondary classifier methods and image features, and how subjects are divided for fitting, validation and testing. The revised classifiers must be fitted again.",
                "supervised_phenotype_training_plan_review": "Change the cell phenotype classifier for the existing cells and measurements, and how subjects are divided for fitting, validation and testing. The revised classifier must be fitted again.",
            }
            note = QLabel(notes[options.kind], self.form)
            self._reopen_description = notes[options.kind]
            note.setWordWrap(True)
            note.setTextFormat(Qt.TextFormat.PlainText)
            form_layout.addWidget(note)
            scroll.setWidget(self.form)
        else:
            self.form = MeasurementOptionsEditor(options.settings, options.required_feature_ids, scroll,
                cell_boundary_source=options.cell_boundary_source)
            scroll.setWidget(self.form)
        choices_layout.addWidget(scroll, 1)
        self.edit_notice = QLabel(self.choices)
        self.edit_notice.setWordWrap(True)
        self.edit_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.edit_notice.hide()
        choices_layout.addWidget(self.edit_notice)
        selected_node = self._nodes.get(options.node_id)
        if options.kind == "measurements" and getattr(selected_node, "status", None) == "failed" and not remote:
            removals = QHBoxLayout()
            self.remove_cells = QPushButton("Remove cells in QuPath", self.choices)
            self.remove_cells.setObjectName("measurement-remove-cells")
            self.remove_cells.clicked.connect(self.remove_cells_requested)
            removals.addWidget(self.remove_cells)
            guidance = QLabel("Select a fragment, delete it and save in QuPath. Close QuPath to return the removals, then review the affected results and recalculate measurements.", self.choices)
            guidance.setWordWrap(True)
            removals.addWidget(guidance, 1)
            choices_layout.addLayout(removals)
        if options.kind == "segmentation":
            actions = self.form.layout().takeAt(self.form.layout().count() - 1).layout()
            for index in range(actions.count()):
                button = actions.itemAt(index).widget()
                if isinstance(button, QPushButton) and button.text() == "Preview":
                    button.setText("Review affected results")
        else:
            actions = QHBoxLayout()
            cancel = QPushButton("Cancel", self.choices)
            cancel.clicked.connect(self.reject)
            actions.addWidget(cancel)
            actions.addStretch(1)
            next_button = QPushButton("Review affected results", self.choices)
            next_button.setProperty("actionRole", "primary")
            next_button.clicked.connect(self._request_preview)
            actions.addWidget(next_button)
        self.edit_details = QPushButton("Error details", self.choices)
        self.edit_details.hide()
        self.edit_details.clicked.connect(self._show_details)
        actions.addWidget(self.edit_details)
        choices_layout.addLayout(actions)
        self._edit_actions = tuple(actions.itemAt(index).widget()
            for index in range(actions.count()) if actions.itemAt(index).widget() is not None)
        self.pages.addWidget(self.choices)
        self.review = RevisionReview(self.pages, review_only=self._review_only)
        self.pages.addWidget(self.review)
        self.summary = self.review.summary
        self.impact = self.review.notice
        self.apply = self.review.apply
        if options.kind == "analysis_domain_selection":
            self.apply.setText("Reopen analysis-area choice")
        self.apply.setObjectName("apply-stage-revision")
        self.details = self.review.details
        self.details.setObjectName("stage-revision-details")
        self.review.back.clicked.connect(self._back_to_choices)
        self.review.retry.clicked.connect(self._request_preview)
        self.review.cancel.clicked.connect(self.reject)
        self.apply.clicked.connect(self._request_apply)
        self.details.clicked.connect(self._show_details)
        self._action_buttons = (*self._edit_actions, self.review.back, self.review.cancel, self.review.retry, self.apply, self.details)
        self._watch_form_edits()
        if self._review_only or (options.kind == 'inspection' and inspection_action == 'review_acquisition_cycles'):
            self.pages.setCurrentWidget(self.review)
            QTimer.singleShot(0, self._request_preview)

    def _back_to_choices(self):
        """Return to the same draft and require its next preview before applying."""
        if self._applying:
            return
        self._invalidate_preview()
        self.edit_notice.hide()
        self.edit_details.hide()
        self.pages.setCurrentWidget(self.choices)
        self.steps.setText("<b>1 · Choose changes</b>     /     2 · Review impact")
        if self.options.kind == "inspection":
            self.resize(self.width(), self._choices_height)

    def _show_review_page(self):
        """Show consequences separately from editable settings, keeping actions visible."""
        self.pages.setCurrentWidget(self.review)
        self.steps.setText("1 · Choose changes     /     <b>2 · Review impact</b>")
        available = self.screen().availableGeometry().height() - 80
        self.resize(self.width(), min(max(420, available), 650))

    def _watch_form_edits(self):
        """Watch value edits, including table controls added since the previous preview."""
        for widget in self.form.findChildren(QWidget):
            if widget.property("revisionPreviewWatched"):
                continue
            signal = (widget.textChanged if isinstance(widget, QLineEdit) else
                      widget.currentIndexChanged if isinstance(widget, QComboBox) else
                      widget.toggled if isinstance(widget, (QCheckBox, QRadioButton)) else
                      widget.valueChanged if isinstance(widget, QAbstractSpinBox) and hasattr(widget, "valueChanged") else
                      widget.textChanged if isinstance(widget, (QTextEdit, QPlainTextEdit)) else
                      widget.cellChanged if isinstance(widget, QTableWidget) else None)
            if signal is not None:
                signal.connect(self._invalidate_preview)
                widget.setProperty("revisionPreviewWatched", True)
            if isinstance(widget, QTableWidget):
                widget.model().rowsInserted.connect(self._invalidate_preview)
                widget.model().rowsRemoved.connect(self._invalidate_preview)

    def _invalidate_preview(self, *args):
        """Clear stale impact as soon as an editable setting changes, without submitting."""
        had_preview = self._preview is not None
        self._preview = None
        self._preview_pending = False
        self.apply.setEnabled(False)
        self.review.clear_preview()
        if had_preview:
            self.review.set_notice("The options changed. Preview their impact again before applying them.")

    def _selected_settings(self):
        if self.options.kind in {"measurements", "inspection", "registration",'clustering_per_slide','clustering_pooled'}:
            return self.form.selected_settings()
        if self.options.kind in SETTLED_REVIEW_KINDS:
            return self.options.settings
        response = self.form.submission_for("preview")
        if response is None:
            return None
        return response["segmentation"] if self._remote else SegmentationConfig.from_mapping(response["segmentation"], field="segmentation")

    def _request_preview(self, *args):
        """Request a fresh impact calculation from the editor's parsed settings."""
        if self._applying:
            return
        self._invalidate_preview()
        self.details.setVisible(False)
        self.edit_details.hide()
        try:
            selected = self._selected_settings()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        if selected is None:
            return
        self.edit_notice.hide()
        self._show_review_page()
        self.review.set_notice("Checking the affected results…")
        self._preview_pending = True
        self.preview_requested.emit(selected)

    def set_preview(self, preview: BatchStageRevisionPreview) -> None:
        """Show affected stages for the saved selection and permit an unblocked revision.

        A response for another selection is rejected. The Apply handler checks
        the current form again so edits cannot reuse a previous impact preview.
        """
        if type(preview) is not BatchStageRevisionPreview or preview.options != self.options:
            raise ValueError("Options preview belongs to another saved selection.")
        # Back, a changed draft or a failed request abandons the pending review.
        # Its late response must not navigate away from the retained choices.
        if not self._preview_pending:
            return
        self._preview_pending = False
        try:
            current_settings = self._selected_settings()
        except ValueError:
            current_settings = None
        if current_settings is None or current_settings != preview.settings:
            self._invalidate_preview()
            self.review.set_notice("The options changed. Preview their impact again before applying them.")
            return
        self._watch_form_edits()
        self._preview = preview
        cycle_review = self.options.kind == 'inspection' and type(preview.settings) is SettledReviewRevision
        if cycle_review:
            current = str(self.options.settings.source_mif_path).replace('\\', '/').rsplit('/', 1)[-1]
            proposed = 'Reopen Image inspection to review cycle assignments for this image.'
        else:
            current, proposed = stage_choices(self.options, preview.settings)
        self.review.show_preview(preview, tuple(self._nodes.values()), current,
                                 self._reopen_description if self._review_only else proposed)
        if self.options.kind == "inspection" and not cycle_review:
            selected = preview.settings if isinstance(preview.settings, dict) else preview.settings.to_mapping()
            if sum(candidate.eligibility in {"eligible", "needs_organization"} for candidate in self.form.inventory.candidates) == 1:
                self.summary.set_choices(*source_choices(self.options.settings, selected, show_series=False))
            self.summary.current.setToolTip(str(self.options.settings.source_mif_path))
            self.summary.proposed.setToolTip(selected["source_mif_path"])
        if self.options.kind == 'inspection':
            self.apply.setText('Reopen Image inspection' if cycle_review else 'Apply changes')
        self._show_review_page()
        self.apply.setEnabled(not self._applying and preview.blocked_reason is None)

    def _request_apply(self):
        """Emit the reviewed revision only while the form still matches its preview."""
        if self._applying:
            return
        try:
            selected = self._selected_settings()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        if selected is None:
            return
        if self._preview is None or selected != self._preview.settings:
            self._invalidate_preview()
            self.review.set_notice("The options changed. Preview their impact again before applying them.")
            return
        self.apply.setEnabled(False)
        self.apply_requested.emit(self._preview)

    def set_applying(self, applying: bool) -> None:
        """Retain the draft while its publication runs, with editing temporarily disabled."""
        self._applying = applying
        self.form.setEnabled(not applying)
        for button in self._action_buttons:
            button.setEnabled(not applying)
        self.apply.setEnabled(False)
        if applying:
            self.details.setVisible(False)
            self.summary.clear_preview()
            self.review.set_notice("Saving revised options…")

    def reject(self) -> None:
        """Keep the pending draft attached to the operation that is saving it."""
        if not self._applying:
            super().reject()

    def show_error(self, message: str, *, diagnostic: str | None = None) -> None:
        """Keep corrected settings and the original diagnosis available for another preview."""
        self._invalidate_preview()
        self._diagnostic = diagnostic or message
        summary = operation_error_summary(message) or message
        message = summary + "\n\nYour selected options are retained."
        self.review.set_notice(message)
        self.edit_notice.setText(message)
        self.edit_notice.setVisible(self.pages.currentWidget() is self.choices)
        self.details.setVisible(True)
        self.edit_details.setVisible(self.pages.currentWidget() is self.choices)
        self.review.retry.setVisible(self._review_only)
        self.apply.setEnabled(False)

    def _show_details(self) -> None:
        if self._diagnostic:
            open_run_diagnostics(self, self._diagnostic).open()
