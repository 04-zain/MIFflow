"""Researcher-facing stage page and exact, copyable diagnostic records.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from ..presenters.workflow import STATUS_LABELS, WorkflowStageView, failure_guidance, optional_setup_skip_label
from ..terminology import WAITING_FOR_INPUTS, PROCESSING
from ..method_guidance import stage_guidance
from mif_pipeline.orchestration.batch_settled_review_revision import STAGE_OPTIONS_KINDS, TRAINING_REVIEW_TARGETS
from .researcher_widgets import HelpDisclosure, SampleScopeStrip, ContentSizedButton

# ==============================================================================
#                                   Run detail
# ==============================================================================


class RunStepPage(QWidget):
    back_requested = Signal()
    review_requested = Signal()
    continue_requested = Signal()
    options_requested = Signal()
    source_options_requested = Signal()
    skip_setup_requested = Signal(object)
    measurement_review_requested = Signal()
    segmentation_review_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("researcher-stage-page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setObjectName("run-step-scroll")
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        content = QWidget(scroll)
        content.setMaximumWidth(860)
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 8, 24, 24)
        body.setSpacing(20)
        back = ContentSizedButton("← Workflow", self)
        back.setObjectName("run-step-back")
        back.setProperty("actionRole", "navigation")
        back.clicked.connect(self.back_requested)
        body.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        introduction = QVBoxLayout()
        introduction.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(18)
        self.title = QLabel(self)
        self.title.setWordWrap(True)
        self.title.setObjectName("run-step-title")
        self.title.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.status = QLabel(self)
        self.status.setObjectName("run-step-status")
        self.status.setProperty("statusBadge", True)
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(240)
        heading.addWidget(self.title, 1)
        heading.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignTop)
        introduction.addLayout(heading)
        self.description = QLabel(self)
        self.description.setWordWrap(True)
        self.description.setProperty("stageDescription", True)
        introduction.addWidget(self.description)
        self.samples = SampleScopeStrip(self)
        self.samples.layout().setContentsMargins(0, 4, 0, 0)
        introduction.addWidget(self.samples)
        body.addLayout(introduction)
        next_panel = QFrame(self)
        next_panel.setObjectName("stage-next-action")
        next_layout = QVBoxLayout(next_panel)
        next_layout.setContentsMargins(18, 18, 18, 18)
        next_layout.setSpacing(10)
        self.guidance_heading = QLabel(self)
        self.guidance_heading.setProperty("panelHeading", True)
        next_layout.addWidget(self.guidance_heading)
        self.guidance = QLabel(self)
        self.guidance.setWordWrap(True)
        self.guidance.setTextFormat(Qt.TextFormat.PlainText)
        next_layout.addWidget(self.guidance)
        body.addWidget(next_panel)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        from .action_bar import WorkflowActionBar

        self._actions = WorkflowActionBar(self)
        self._actions.setObjectName("stage-current-actions")
        self.review = ContentSizedButton("Open review", self)
        self.review.setObjectName("run-step-review")
        self.review.setProperty("actionRole", "primary")
        self.review.clicked.connect(self.review_requested)
        self.skip = ContentSizedButton("Skip classification", self)
        self.skip.setObjectName("run-step-skip-optional")
        self.skip.setProperty("actionRole", "secondary")
        self.skip.clicked.connect(self._request_skip)
        self._skip_nodes = ()
        self.skip.hide()
        self.proceed = ContentSizedButton("Continue available work", self)
        self.proceed.setObjectName("run-step-continue")
        self.proceed.setProperty("actionRole", "primary")
        self.proceed.clicked.connect(self.continue_requested)
        self.options = ContentSizedButton("Change settings…", self)
        self.options.setObjectName("run-step-options")
        self.options.setProperty("actionRole", "secondary")
        self.options.clicked.connect(self.options_requested)
        self._actions.add_primary(self.review)
        self._actions.add_primary(self.skip)
        self._actions.add_primary(self.proceed)
        self.measurement_review = ContentSizedButton('Review cell measurements', self)
        self.measurement_review.setObjectName('run-step-measurement-review')
        self.measurement_review.setProperty('actionRole', 'primary')
        self.measurement_review.clicked.connect(self.measurement_review_requested)
        self.measurement_review.hide()
        self._actions.add_primary(self.measurement_review)
        self.segmentation_review = ContentSizedButton('Review cell and nucleus outlines', self)
        self.segmentation_review.setObjectName('run-step-segmentation-review')
        self.segmentation_review.setProperty('actionRole', 'primary')
        self.segmentation_review.clicked.connect(self.segmentation_review_requested)
        self.segmentation_review.hide()
        self._actions.add_primary(self.segmentation_review)
        self._actions._outer.setContentsMargins(0, 8, 0, 0)
        next_layout.addWidget(self._actions)
        self._revision_panel = QFrame(self)
        self._revision_panel.setObjectName("stage-revision-actions")
        revision_layout = QVBoxLayout(self._revision_panel)
        revision_layout.setContentsMargins(18, 18, 18, 18)
        revision_layout.setSpacing(10)
        revision_heading = QLabel("Revise this step", self._revision_panel)
        revision_heading.setProperty("panelHeading", True)
        revision_layout.addWidget(revision_heading)
        revision_layout.addWidget(self.options, alignment=Qt.AlignmentFlag.AlignLeft)
        self.source_options = ContentSizedButton("Change source image…", self)
        self.source_options.setObjectName("run-step-source-options")
        self.source_options.setProperty("actionRole", "secondary")
        self.source_options.clicked.connect(self.source_options_requested)
        revision_layout.addWidget(self.source_options, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self._revision_panel)
        self.help = HelpDisclosure("About the methods", parent=self)
        body.addWidget(self.help)
        body.addStretch(1)

    def _request_skip(self):
        if self.skip.isEnabled() and len(self._skip_nodes) == 1:
            self.skip_setup_requested.emit((self._skip_nodes[0].node_id,))

    def _set_skip_choices(self, stage, review_allowed):
        """Keep one direct action, with an image-group menu only when needed."""
        old = self.skip.menu()
        self.skip.setMenu(None)
        if old is not None:
            old.deleteLater()
        self._skip_nodes = tuple(node for node in stage.nodes if optional_setup_skip_label(node))
        self.skip.setVisible(bool(self._skip_nodes))
        self.skip.setEnabled(bool(self._skip_nodes) and review_allowed)
        if not self._skip_nodes:
            return
        self.skip.setText(optional_setup_skip_label(self._skip_nodes[0]))
        if len(self._skip_nodes) > 1:
            menu = QMenu(self.skip)
            for node in self._skip_nodes:
                action = menu.addAction(", ".join(node.sample_ids))
                action.triggered.connect(lambda _checked=False, identity=node.node_id:
                    self.skip_setup_requested.emit((identity,)))
            self.skip.setMenu(menu)

    def set_stage(self, stage: WorkflowStageView, *, review_allowed: bool, continue_allowed: bool, options_allowed: bool = False, resource_wait_message: str | None = None, analysis_running: bool = False):
        self.title.setText(stage.definition.title)
        # The run flag identifies analysis activity, not this particular node.
        active_display = (analysis_running and stage.status in {"ready", "partial"}
                          and resource_wait_message is None)
        self.status.setText("Analysis running" if active_display else STATUS_LABELS.get(stage.status, "Not yet selected"))
        self.status.setProperty("stageState", "running" if active_display else stage.status)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setMinimumWidth(min(240, self.status.fontMetrics().horizontalAdvance(self.status.text()) + 28))
        self.description.setText(stage.definition.description)
        heading = "Next step"
        failed = next((n for n in stage.nodes if n.failure_message), None)
        if failed:
            heading = "Step stopped"
            message = failure_guidance(failed.failure_code, failed.failure_message, stage_kind=failed.kind, source_correction_available=failed.source_correction_available)
        elif stage.review_node_ids:
            from .reviews_training import _review_title

            pending = [node for node in stage.nodes if node.node_id in stage.review_node_ids]
            def review_title(node):
                return ("Choose structures" if node.kind == "secondary_structure_definition"
                        else _review_title(node.interaction_reason or "other_explicit_reason"))
            titles = tuple(dict.fromkeys(review_title(node) for node in pending))
            heading = titles[0] if len(titles) == 1 else "Required reviews"
            samples = tuple(sorted({sample for node in pending for sample in node.sample_ids}))
            message = ("" if samples == stage.sample_ids else ", ".join(samples)) if len(titles) == 1 else "\n".join(
                review_title(node) +
                (" · " + ", ".join(node.sample_ids) if node.sample_ids else "")
                for node in pending
            )
        elif stage.status == "completed":
            heading = "Saved results"
            message = ("Inspect measurement distributions and locate selected cells in the tissue."
                       if stage.key == 'measurements' else
                       "Inspect cell and nuclear outlines against the staining before choosing measurements."
                       if stage.key == 'segmentation' else "View this step’s outputs in Results.")
        elif stage.status == "failed":
            heading = "Step stopped"
            message = "Return to Workflow and open Details to inspect the recorded cause."
        elif stage.status in {"planned", "waiting"}:
            heading = "Waiting for inputs"
            message = WAITING_FOR_INPUTS
        elif stage.status == "running":
            heading = "Processing"
            message = PROCESSING
        elif active_display:
            heading = "Processing"
            message = "Waiting for the current work to finish."
        elif continue_allowed:
            heading = "Ready to continue"
            message = "Continue to process the available steps. The workflow will pause when your input is needed."
        else:
            message = WAITING_FOR_INPUTS
        if resource_wait_message is not None and stage.status in {"ready", "waiting", "partial"}:
            heading = "Waiting for resources"
            message = resource_wait_message
        self.guidance.setText(message)
        self.guidance.setVisible(bool(message))
        self.guidance_heading.setText(heading)
        self.samples.set_samples(stage.sample_ids)
        self.help.set_text(stage_guidance(stage.key))
        self.review.setVisible(bool(stage.review_node_ids))
        self.review.setEnabled(review_allowed)
        self.measurement_review.setVisible(stage.key == 'measurements' and stage.status == 'completed')
        self.measurement_review.setEnabled(options_allowed and not analysis_running)
        self.segmentation_review.setVisible(stage.key == 'segmentation' and stage.status == 'completed')
        self.segmentation_review.setEnabled(options_allowed and not analysis_running)
        self._set_skip_choices(stage, review_allowed)
        self.proceed.setVisible(
            not stage.review_node_ids and stage.status not in {"completed", "failed", "planned"}
        )
        self.proceed.setEnabled(continue_allowed)
        from mif_pipeline.orchestration.batch_source_revision import SOURCE_CORRECTION_KINDS
        source_correction = any(node.source_correction_available and node.kind in SOURCE_CORRECTION_KINDS and not node.deferred
            and (node.status in {"completed", "failed"}
                 or (node.kind == "reference_unmixing" and node.status == "needs_interaction")) for node in stage.nodes)
        marker_redo = any(node.kind in {"marker_gate_review", "phenotyping_mode_resolution"} and not node.deferred
                         and node.status in {"completed", "failed"} for node in stage.nodes)
        tissue_redo = any(node.kind in {"tissue_setup", "tissue_resolution", "tissue_candidate"} and not node.deferred
                          and node.status in {"completed", "failed"} for node in stage.nodes)
        region_redo = any(node.kind == "analysis_domain_selection" and not node.deferred
                          and node.status in {"completed", "failed"} for node in stage.nodes)
        classifier_redo = any(node.kind in {*TRAINING_REVIEW_TARGETS, *TRAINING_REVIEW_TARGETS.values()} and not node.deferred
                              and node.status in {"completed", "failed"} for node in stage.nodes)
        alignment_redo = any(node.kind == "registration" and not node.deferred
                             and node.status in {"completed", "failed"} for node in stage.nodes)
        unmixing_redo = any(node.kind == "reference_unmixing" and not node.deferred
                           and node.status in {"completed", "failed", "needs_interaction"} for node in stage.nodes)
        self.source_options.setVisible(source_correction and unmixing_redo)
        self.source_options.setEnabled(options_allowed)
        self.options.setText(
            "Review unmixing settings…" if unmixing_redo else
            "Change channels and markers…" if stage.key == "channels" else
            "Change source image…" if source_correction else
            "Change phenotyping choices…" if marker_redo else
            "Review tissue settings…" if tissue_redo else
            "Change analysis regions…" if region_redo else
            "Change classifier setup…" if classifier_redo else
            "Change paired image or alignment…" if alignment_redo else
            "Change segmentation settings…" if stage.key == "segmentation" else
            "Change cell measurements…" if stage.key == "measurements" else
            "Change settings…"
        )
        revisable = unmixing_redo or any(node.kind in STAGE_OPTIONS_KINDS
                        and (node.kind not in SOURCE_CORRECTION_KINDS or node.source_correction_available) and not node.deferred
                        and node.status in {"completed", "failed"} for node in stage.nodes)
        self.options.setVisible(revisable)
        self.options.setEnabled(revisable and options_allowed)
        self._revision_panel.setVisible(revisable or (source_correction and unmixing_redo))
        self._actions.setVisible(any(not button.isHidden() for button in
                                    (self.review, self.skip, self.proceed, self.measurement_review, self.segmentation_review)))


def open_run_diagnostics(parent: QWidget, text: str) -> QDialog:
    dialog = QDialog(parent)
    dialog.setObjectName("run-diagnostic-dialog")
    dialog.setWindowTitle("Analysis details")
    dialog.resize(820, 520)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    layout = QVBoxLayout(dialog)
    heading = QLabel("Recorded cause and processing context", dialog)
    heading.setProperty("panelHeading", True)
    layout.addWidget(heading)
    body = QPlainTextEdit(dialog)
    body.setObjectName("run-diagnostic-text")
    body.setReadOnly(True)
    body.setPlainText(text)
    body.setAccessibleName("Analysis diagnostic details")
    layout.addWidget(body, 1)
    actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
    copy = actions.addButton("Copy details", QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(body.toPlainText()))
    actions.rejected.connect(dialog.reject)
    layout.addWidget(actions)
    dialog.open()
    return dialog
