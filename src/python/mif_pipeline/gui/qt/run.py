"""Guided workflow landing page; execution remains with the batch service.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from ..presenters.run import BatchRunView
from ..presenters.workflow import build_workflow_graph, failure_guidance, BY_KIND
from ..workspace_session import SessionExitState
from mif_pipeline.orchestration.batch_workflow import BatchWorkflowNode
from mif_pipeline.orchestration.batch_service_models import BatchRetryChoice
from mif_pipeline.orchestration.batch_settled_review_revision import STAGE_OPTIONS_KINDS
from .forms import apply_widget_accessibility
from .workbench import PageHeading
from .workflow_graph import WorkflowGraph
from .run_detail import RunStepPage, open_run_diagnostics
from .error_copy import operation_error_summary
from .researcher_widgets import ContentSizedButton, SampleScopeStrip

_CLOSE_STATES = frozenset(
    {SessionExitState.PAUSED, SessionExitState.COMPLETED, SessionExitState.DIAGNOSTIC_ONLY}
)
_RETRY_REASONS = {
    "request_preparation_failed": "The step stopped before QuPath started. Retry with the saved inputs and settings.",
    "pending_calculation": "Continue this calculation using its saved inputs and settings. Check the reported error if it stops again.",
    "source_access_failed": "Read the same source image again after restoring file access.",
    "runtime_dependency_unavailable": "Check Java and Bio-Formats in Software setup, correct the reported cause, then inspect the same source image again.",
    "input_required": "Reopen the saved form; completed work is retained.",
    "calculation_failed": "Retry unmixing with the accepted spectral basis and saved settings.",
    "timeout": "QuPath took too long to finish.",
    "cancelled": "The QuPath step was cancelled.",
    "nonzero_exit": "QuPath stopped with an error. Open Details for the recorded cause before retrying with the saved inputs and settings.",
    "exchange_members_missing": "QuPath finished without saving the step result.",
    "native_fit_failed": "Classifier fitting stopped before saving a model. The training inputs are retained.",
    "target_codec_rejected": "The saved result could not be accepted. Retry using the saved inputs and settings.",
    "result_payload_invalid": "The saved result is invalid. Open Details for the recorded cause before retrying with the saved inputs and settings.",
}


# ==============================================================================
#                                      Run
# ==============================================================================


class RunPage(QWidget):
    """Render supplied stage evidence; never dispatch a node from a graph click."""

    refresh_requested = Signal()
    start_requested = Signal()
    continue_saved_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    retry_requested = Signal(object)
    segmentation_recovery_requested = Signal(object)
    cancel_requested = Signal()
    open_qupath_requested = Signal()
    review_nodes_requested = Signal(object)
    skip_setup_requested = Signal(object)
    software_setup_requested = Signal()
    image_appearance_requested = Signal()
    stage_options_requested = Signal(object)
    measurement_review_requested = Signal()
    segmentation_review_requested = Signal()
    results_requested = Signal()

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_view: BatchRunView | None = None
        self._exit_state: SessionExitState | None = None
        self._close_authorized = False
        self._pause_pending = False
        self._authority_key = None
        self._retry_choices: tuple[BatchRetryChoice, ...] = ()
        self._retry_dialog: QDialog | None = None
        self._diagnostic_dialog: QDialog | None = None
        self._operation_diagnostic = ""
        self._plan_nodes: tuple[BatchWorkflowNode, ...] = ()
        self._plan_start_allowed = False
        self._selected_stage: str | None = None
        self._stage_views = {}
        self._actions_valid = False
        self._measurement_review_due = False
        self._segmentation_review_due = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 8)
        outer.setSpacing(10)
        self._heading = PageHeading(
                "run",
                "Analysis workflow",
                "Follow your samples from image preparation to reviewed cell results.",
                self,
            )
        heading_row = QHBoxLayout()
        heading_row.addWidget(self._heading, 1)
        self._appearance = QPushButton('Image appearance…', self)
        self._appearance.setEnabled(False)
        self._appearance.clicked.connect(self.image_appearance_requested.emit)
        heading_row.addWidget(self._appearance, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading_row)
        self._status_card = QFrame(self)
        self._status_card.setObjectName("run-status-card")
        status_layout = QVBoxLayout(self._status_card)
        status_layout.setContentsMargins(18, 14, 18, 12)
        status_layout.setSpacing(9)
        status_top = QHBoxLayout()
        caption = QLabel("ANALYSIS STATUS", self._status_card)
        caption.setObjectName("run-status-caption")
        self._progress_summary = QLabel(self._status_card)
        self._progress_summary.setObjectName("run-progress-summary")
        status_top.addWidget(caption)
        status_top.addStretch(1)
        status_top.addWidget(self._progress_summary)
        status_layout.addLayout(status_top)
        self._state = QLabel("Prepare an analysis to see its workflow.", self)
        self._state.setObjectName("run-state")
        self._state.setWordWrap(True)
        state_row = QHBoxLayout()
        state_row.setSpacing(16)
        state_row.addWidget(self._state, 1)
        self._suggested_stage = None
        self._open_current = ContentSizedButton("Open review", self)
        self._open_current.setObjectName("run-open-current-step")
        self._open_current.setProperty("actionRole", "primary")
        self._open_current.clicked.connect(lambda: self.open_stage(self._suggested_stage))
        self._open_current.hide()
        state_row.addWidget(self._open_current, alignment=Qt.AlignmentFlag.AlignVCenter)
        status_layout.addLayout(state_row)
        self._stage_progress = QProgressBar(self._status_card)
        self._stage_progress.setObjectName("run-stage-progress")
        self._stage_progress.setTextVisible(False)
        self._stage_progress.setFixedHeight(6)
        self._stage_progress.setAccessibleName("Completed scientific stages")
        self._stage_progress.hide()
        status_layout.addWidget(self._stage_progress)
        self._scope = SampleScopeStrip(self._status_card)
        self._scope.layout().setContentsMargins(0, 2, 0, 0)
        self._scope.hide()
        status_layout.addWidget(self._scope)
        outer.addWidget(self._status_card)
        self._pages = QStackedWidget(self)
        self._graph = WorkflowGraph(self._pages)
        self._graph.stage_requested.connect(self.open_stage)
        self._step = RunStepPage(self._pages)
        self._step.options_requested.connect(self._select_stage_options)
        self._step.source_options_requested.connect(self._select_source_options)
        self._step.back_requested.connect(self.show_overview)
        self._step.review_requested.connect(self._emit_review)
        self._step.measurement_review_requested.connect(self.measurement_review_requested)
        self._step.segmentation_review_requested.connect(self.segmentation_review_requested)
        self._step.skip_setup_requested.connect(self._emit_optional_skip)
        self._step.continue_requested.connect(self._continue_workflow)
        self._pages.addWidget(self._graph)
        self._pages.addWidget(self._step)
        outer.addWidget(self._pages, 1)

        from .action_bar import WorkflowActionBar

        action_bar = WorkflowActionBar(self)
        self._footer_actions = action_bar
        action_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        action_bar._outer.setContentsMargins(12, 0, 12, 0)
        self._start = self._button("Start analysis", "start-run", self._emit_start)
        self._start.setProperty("actionRole", "primary")
        self._refresh = self._button("Refresh", "refresh-run", self._emit_refresh)
        self._pause = self._button("Pause after current work", "pause-run", self._emit_pause)
        self._resume = self._button("Continue analysis", "resume-run", self._emit_resume)
        self._resume.setProperty("actionRole", "primary")
        self._continue_saved = self._button("Continue saved work", "continue-saved-run", self._emit_continue_saved)
        self._continue_saved.setProperty("actionRole", "primary")
        self._continue_saved.setToolTip("Resume the interrupted analysis from its saved progress.")
        self._retry = self._button("Retry step…", "retry-run", self._select_retry)
        self._recover_segmentation = self._button(
            "Restore accepted regions", "recover-interrupted-segmentation", self._recover_interrupted_segmentation)
        self._recover_segmentation.setToolTip(
            "Back up the interrupted state, verify the old local process is absent, and restore accepted tissue and primary regions. "
            "The old segmentation remains unaccepted and its exit stays unknown. Then choose Segmentation options to start a new attempt.")
        self._cancel = self._button("Cancel", "cancel-run", self._emit_cancel)
        self._open_qupath = self._button("Open QuPath", "open-qupath", self._emit_open_qupath)
        self._diagnostics = self._button("Details", "run-diagnostics", self._show_diagnostics)
        self._setup = self._button(
            "Software setup", "run-software-setup", self.software_setup_requested.emit
        )
        for button in (
            self._start,
            self._resume,
            self._continue_saved,
            self._pause,
            self._retry,
            self._recover_segmentation,
            self._cancel,
            self._setup,
        ):
            action_bar.add_primary(button)
            button.hide()
        for button in (self._open_qupath, self._refresh, self._diagnostics):
            action_bar.add_utility(button)
        self._open_qupath.hide()
        footer = QHBoxLayout()
        footer.setSpacing(16)
        self._graph.layout().removeWidget(self._graph.legend)
        footer.addWidget(self._graph.legend, 1)
        footer.addWidget(action_bar)
        outer.addLayout(footer)
        self._pause_help = QLabel("Finishing current work before pausing…", self)
        self._pause_help.setObjectName("run-pause-help")
        self._pause_help.setWordWrap(True)
        self._pause_help.hide()
        outer.addWidget(self._pause_help)
        self._disable_controls()
        self._start.setEnabled(False)
        self._diagnostics.setEnabled(False)
        apply_widget_accessibility(self, "run")

    def set_plan_preview(self, nodes: tuple[BatchWorkflowNode, ...], *, can_start: bool) -> None:
        if type(can_start) is not bool:
            raise TypeError("plan start availability must be bool")
        graph = build_workflow_graph(nodes)
        changed_plan = nodes != self._plan_nodes
        self._plan_nodes = nodes
        if self._current_view is not None:
            return
        if changed_plan:
            self._operation_diagnostic = ""
            self._update_diagnostic_dialog()
        self._plan_start_allowed = can_start
        self._actions_valid = True
        self._install_graph(graph)
        self._state.setText(
            "Ready to begin. Automatic steps run until your input is needed."
            if can_start
            else "Complete the setup checks before starting this analysis."
        )
        self._start.show()
        self._start.setEnabled(can_start)
        self._diagnostics.setEnabled(bool(self._operation_diagnostic))

    def _install_graph(self, graph) -> None:
        self._stage_views = {stage.key: stage for stage in graph.stages}
        self._graph.set_graph(graph)
        included = tuple(stage for stage in graph.stages if stage.status != "planned")
        completed = sum(stage.status == "completed" for stage in included)
        self._progress_summary.setText(f"{completed} of {len(included)} stages complete" if included else "")
        self._stage_progress.setRange(0, max(1, len(included)))
        self._stage_progress.setValue(completed)
        self._stage_progress.setVisible(bool(included))
        self._stage_progress.setToolTip("Completed stages; this is not an estimate of processing time.")
        samples = tuple(sorted({sample for stage in included for sample in stage.sample_ids}))
        self._scope.set_samples(samples)
        self._scope.setVisible(bool(samples))
        self._suggested_stage = None
        for status, label in (("failed", "Open stopped step"), ("needs_interaction", "Open review"), ("running", "View processing"), ("ready", "Open next step")):
            stage = next((stage for stage in graph.stages if stage.status == status), None)
            if stage is not None:
                self._suggested_stage = stage.key
                self._open_current.setText(label)
                self._open_current.setToolTip(stage.definition.title)
                break
        self._open_current.setVisible(self._suggested_stage is not None)
        if self._selected_stage in self._stage_views:
            self._refresh_step()
        elif self._selected_stage is not None:
            self.show_overview()

    def open_stage(self, key: str) -> None:
        if key not in self._stage_views:
            return
        if key == 'results' and self._stage_views[key].status == 'completed':
            self.results_requested.emit()
            return
        self._selected_stage = key
        self._appearance.hide()
        self._footer_actions.show()
        self._heading.hide()
        self._status_card.hide()
        self._refresh_step()
        self._pages.setCurrentWidget(self._step)
        self._graph.legend.hide()
        self._step.title.setFocus()

    def show_overview(self) -> None:
        self._selected_stage = None
        self._appearance.show()
        self._footer_actions.show()
        self._heading.show()
        self._status_card.show()
        self._pages.setCurrentWidget(self._graph)
        self._graph.legend.show()

    def _refresh_step(self) -> None:
        stage = self._stage_views.get(self._selected_stage)
        if stage is None:
            return
        view = self._current_view
        idle = self._actions_valid and (view is None or view.active_operation is None)
        resume_allowed = (
            view is not None
            and view.resume_disposition == "paused_resumable"
            and self._exit_state is SessionExitState.PAUSED
            and (
                not view.workflow_nodes
                or not view.interaction_count
                or any(node.eligible for node in view.workflow_nodes if not node.deferred)
            )
        )
        self._step.set_stage(
            stage,
            analysis_running=bool(view is not None and view.active_operation == "run"),
            review_allowed=bool(
                idle
                and stage.review_node_ids
                and view is not None
                and view.resume_disposition == "paused_resumable"
            ),
            options_allowed=bool(idle and view is not None),
            resource_wait_message=("\n".join(dict.fromkeys(
                view.resource_wait_messages[node.node_id] for node in stage.nodes
                if node.node_id in view.resource_wait_messages)) or None if view is not None else None),
            continue_allowed=bool(idle and (
                self._plan_start_allowed or resume_allowed
                or (view is not None and view.continuation_available)
            )),
        )

    def _select_stage_options(self) -> None:
        stage = self._stage_views.get(self._selected_stage)
        if stage is None or not self._actions_valid or not self._step.options.isEnabled():
            return
        from mif_pipeline.orchestration.batch_source_revision import SOURCE_CORRECTION_KINDS
        reference = tuple(node for node in stage.nodes if node.kind == "reference_unmixing"
                          and not node.deferred and node.status in {"completed", "failed", "needs_interaction"})
        tissue = tuple(node for node in stage.nodes if node.kind == "tissue_setup"
                       and not node.deferred and node.status == "completed")
        nodes = tissue or reference or tuple(node for node in stage.nodes if node.kind in STAGE_OPTIONS_KINDS
                      and (node.kind not in SOURCE_CORRECTION_KINDS or node.source_correction_available)
                      and not node.deferred and node.status in {"completed", "failed"})
        self.stage_options_requested.emit(tuple(node.node_id for node in nodes))

    def _select_source_options(self) -> None:
        stage = self._stage_views.get(self._selected_stage)
        if stage is None or not self._actions_valid or not self._step.source_options.isEnabled():
            return
        sample_ids = set(stage.sample_ids)
        nodes = tuple(node for view in self._stage_views.values() for node in view.nodes
                      if node.kind == "inspection" and node.source_correction_available
                      and sample_ids.intersection(node.sample_ids)
                      and not node.deferred and node.status in {"completed", "failed"})
        self.stage_options_requested.emit(tuple(node.node_id for node in nodes))

    def _emit_review(self) -> None:
        stage = self._stage_views.get(self._selected_stage)
        if stage is not None and self._step.review.isEnabled() and self._actions_valid:
            self.review_nodes_requested.emit(stage.review_node_ids)

    def _emit_optional_skip(self, node_ids) -> None:
        from ..presenters.workflow import optional_setup_skip_label
        stage = self._stage_views.get(self._selected_stage)
        if (stage is None or not self._actions_valid or not self._step.skip.isEnabled()
                or type(node_ids) is not tuple or len(node_ids) != 1):
            return
        if any(node.node_id == node_ids[0] and optional_setup_skip_label(node) for node in stage.nodes):
            self.skip_setup_requested.emit(node_ids)

    def _emit_start(self) -> None:
        if self._current_view is None and self._plan_start_allowed and self._start.isEnabled():
            self._start.setEnabled(False)
            self._plan_start_allowed = False
            self._operation_diagnostic = ""
            self._actions_valid = False
            self._state.setText("Starting image inspection…")
            self._refresh_step()
            self.start_requested.emit()

    def _continue_workflow(self) -> None:
        if not self._actions_valid:
            return
        if self._current_view is None:
            self._emit_start()
        elif self._current_view.continuation_available:
            self._emit_continue_saved()
        else:
            self._emit_resume()

    def can_continue_saved_work(self) -> bool:
        """Use the same current action authority for guidance and the Continue button."""
        return (
            self._actions_valid
            and self._current_view is not None
            and self._current_view.continuation_available
            and self._continue_saved.isEnabled()
        )

    def _emit_continue_saved(self) -> None:
        if self.can_continue_saved_work():
            self.continue_saved_requested.emit()

    def has_stopped_step(self) -> bool:
        return self._actions_valid and any(stage.status == "failed" for stage in self._stage_views.values())

    def open_stopped_step(self) -> None:
        """Open the current failed stage without changing or resubmitting its data."""
        if self.has_stopped_step():
            stage = next(stage for stage in self._stage_views.values() if stage.status == "failed")
            self.open_stage(stage.key)

    def operation_diagnostic(self) -> str:
        """Return the retained worker traceback and current recorded failure context."""
        return self._diagnostic_text()

    def retry_action_label(self) -> str | None:
        """Expose only the retry choices already accepted for this current view."""
        return self._retry.text() if self._actions_valid and self._retry_choices and self._retry.isEnabled() else None

    def open_retry_selection(self) -> None:
        if self.retry_action_label() is not None:
            self._select_retry()

    def set_operation_error(self, summary: str, diagnostic: str) -> None:
        if type(summary) is not str or type(diagnostic) is not str:
            raise TypeError("operation errors require text")
        self._operation_diagnostic = diagnostic or summary
        self._state.setText(operation_error_summary(summary) or summary)
        self._status_card.show()
        self._diagnostics.setEnabled(True)
        self._update_diagnostic_dialog()
        self._update_segmentation_recovery()

    def interrupted_segmentation_selection(self):
        view = self._current_view
        if (not self._actions_valid or view is None or view.active_operation is not None
                or 'The saved QuPath run has no process-completion record.' not in self._operation_diagnostic):
            return None
        choices = [node for node in view.workflow_nodes
                   if node.kind == 'segmentation' and node.status in {'ready','waiting'} and not node.deferred]
        return (choices[0].node_id, view.state_head_sha256) if len(choices) == 1 else None

    def _update_segmentation_recovery(self):
        available = self.interrupted_segmentation_selection() is not None
        self._recover_segmentation.setVisible(available)
        self._recover_segmentation.setEnabled(available)

    def _recover_interrupted_segmentation(self):
        selected = self.interrupted_segmentation_selection()
        if selected is not None:
            self.segmentation_recovery_requested.emit(selected)

    def _diagnostic_text(self) -> str:
        lines = []
        view = self._current_view
        if view is not None:
            lines.extend(
                (
                    "Analysis: " + view.batch_id,
                )
            )
            for node in view.workflow_nodes:
                if not node.failure_message:
                    continue
                definition = BY_KIND.get(node.kind)
                lines.extend(
                    (
                        "",
                        definition.title if definition else node.kind,
                        "Samples: " + ", ".join(node.sample_ids),
                        "",
                        failure_guidance(
                            node.failure_code, node.failure_message,
                            stage_kind=node.kind,
                            source_correction_available=node.source_correction_available,
                        ),
                        "",
                        "Technical details",
                        "Stage: " + node.kind,
                        "Code: " + (node.failure_code or "not recorded"),
                        "Cause: " + node.failure_message,
                        "Node: " + node.node_id,
                    )
                )
            lines.extend(("", "Saved state: " + view.state_head_sha256,
                          "Resume state: " + view.resume_disposition))
            if view.log_lines:
                lines.extend(("", "Recent activity", *view.log_lines))
            if not any(n.failure_message for n in view.workflow_nodes) and view.failure_count:
                lines.append(
                    "The saved status reports a failure, but its error message is unavailable."
                )
        if self._operation_diagnostic:
            explanation = operation_error_summary(self._operation_diagnostic)
            if explanation is not None:
                lines.extend(("", explanation))
            lines.extend(("", "Technical details of the failed action", self._operation_diagnostic))
        return "\n".join(lines) or "No diagnostic record is available yet."

    def _show_diagnostics(self) -> None:
        if self._diagnostic_dialog is not None:
            self._diagnostic_dialog.raise_()
            self._diagnostic_dialog.activateWindow()
            return
        self._diagnostic_dialog = open_run_diagnostics(self, self._diagnostic_text())
        self._diagnostic_dialog.finished.connect(
            lambda _: setattr(self, "_diagnostic_dialog", None)
        )

    def _update_diagnostic_dialog(self) -> None:
        if self._diagnostic_dialog is not None:
            self._diagnostic_dialog.findChild(QPlainTextEdit, "run-diagnostic-text").setPlainText(
                self._diagnostic_text()
            )

    @staticmethod
    def _state_text(view: BatchRunView) -> str:
        failure = next((n for n in view.workflow_nodes if n.failure_message), None)
        if failure is not None:
            definition = BY_KIND.get(failure.kind)
            title = definition.title if definition else failure.kind.replace("_", " ").capitalize()
            return (
                title
                + " stopped. "
                + failure_guidance(failure.failure_code, failure.failure_message, stage_kind=failure.kind, source_correction_available=failure.source_correction_available)
            )
        if view.failure_count:
            return (
                "Processing stopped. Open Details to inspect the available diagnostic information."
            )
        if view.active_operation is not None:
            return "Processing available steps. You will be prompted when a decision is needed."
        if view.resource_wait_messages:
            return "\n".join(dict.fromkeys(view.resource_wait_messages.values()))
        if view.continuation_available:
            return "Select Continue saved work to resume processing."
        if view.interaction_count:
            return "Your input is needed. Open the highlighted step to complete its required form."
        return {
            "completed": "Analysis completed. Open Results to review the results.",
            "paused_resumable": "Progress saved. Continue when you are ready.",
            "active_current_session": "Processing available workflow steps.",
            "non_resumable": "Processing stopped. Open Details for the cause and available recovery actions.",
        }[view.resume_disposition]

    def set_view(
        self,
        view: BatchRunView,
        *,
        cancel_offered: bool,
        exit_state: SessionExitState,
        close_authorized: bool,
        saved_images_available: bool,
    ) -> None:
        """Render only explicit presentation values and supplied authorities."""

        if type(view) is not BatchRunView:
            raise TypeError("Run page requires a BatchRunView")
        image_column = view.summary_table.columns.index('processed_image_path')
        self._appearance.setEnabled(any(row.values[image_column] for row in view.summary_table.rows))
        if type(cancel_offered) is not bool:
            raise TypeError("cancel_offered must be a bool")
        if type(exit_state) is not SessionExitState:
            raise TypeError("exit_state must be a SessionExitState")
        if type(close_authorized) is not bool:
            raise TypeError("close_authorized must be a bool")
        if close_authorized and exit_state not in _CLOSE_STATES:
            raise ValueError("close authority requires an explicitly safe exit state")
        if close_authorized and view.resume_disposition == "active_current_session":
            raise ValueError("an active current session cannot be authorized to close")
        authority_key = (
            view.state_head_sha256,
            view.resume_disposition,
            exit_state,
            close_authorized,
        )
        if authority_key != self._authority_key:
            self._pause_pending = exit_state is SessionExitState.PAUSING
        self._authority_key = authority_key
        prior_view = self._current_view
        if self._current_view is not None and self._current_view.batch_id != view.batch_id:
            self._operation_diagnostic = ""
        prior_measurements = self._stage_views.get('measurements')
        prior_segmentation = self._stage_views.get('segmentation')
        self._current_view = view
        if view.active_operation is not None or any(
            choice.batch_id != view.batch_id or choice.state_head_sha256 != view.state_head_sha256
            for choice in self._retry_choices
        ):
            self.set_retry_choices(())
        self._exit_state = exit_state
        self._close_authorized = close_authorized

        self._start.hide()
        self._plan_start_allowed = False
        self._actions_valid = True
        self._continue_saved.setVisible(view.continuation_available)
        self._continue_saved.setEnabled(view.continuation_available and view.active_operation is None)
        self._install_graph(build_workflow_graph(view.workflow_nodes))
        measurements = self._stage_views.get('measurements')
        if (prior_measurements is not None and prior_measurements.status != 'completed'
                and measurements is not None and measurements.status == 'completed'):
            self._measurement_review_due = True
        segmentation = self._stage_views.get('segmentation')
        if (prior_segmentation is not None and prior_segmentation.status != 'completed'
                and segmentation is not None and segmentation.status == 'completed'):
            self._segmentation_review_due = True
        if view.failure_count:
            # Keep recovery on the stopped task instead of opening an earlier result.
            self._segmentation_review_due = self._measurement_review_due = False
        if self._segmentation_review_due and view.active_operation is None:
            self._segmentation_review_due = False
            if not self._measurement_review_due:
                QTimer.singleShot(0, lambda head=view.state_head_sha256:
                    self._open_completed_review("segmentation", head))
        if self._measurement_review_due and view.active_operation is None:
            self._measurement_review_due = False
            QTimer.singleShot(0, lambda head=view.state_head_sha256:
                self._open_completed_review("measurements", head))
        self._diagnostics.setEnabled(True)
        self._setup.setVisible(
            any(
                node.failure_code == "preprocessing_dependencyversionerror"
                or (node.failure_code or "").lower().startswith(
                    ("bioformats", "bffile", "payload_absent", "dependencyversion", "java_")
                )
                for node in view.workflow_nodes
            )
        )
        if view.active_operation is not None or (view.resume_disposition == "completed" and (
            prior_view is None or prior_view.state_head_sha256 != view.state_head_sha256
        )):
            self._operation_diagnostic = ""
        self._update_diagnostic_dialog()
        explanation = (operation_error_summary(self._operation_diagnostic)
                       if view.resume_disposition == "non_resumable" else None)
        self._state.setText(explanation or self._state_text(view))
        self._refresh.setEnabled(True)
        self._pause.setEnabled(
            view.resume_disposition == "active_current_session"
            and exit_state is SessionExitState.MUTABLE
            and not self._pause_pending
        )
        self._resume.setEnabled(
            view.resume_disposition == "paused_resumable"
            and exit_state is SessionExitState.PAUSED
            and view.active_operation is None
        )
        self._cancel.setEnabled(cancel_offered)
        self._open_qupath.setEnabled(saved_images_available)
        self._pause.setVisible(view.resume_disposition == "active_current_session")
        available_work = (
            not view.workflow_nodes
            or not view.interaction_count
            or any(node.eligible for node in view.workflow_nodes if not node.deferred)
        )
        self._resume.setVisible(view.resume_disposition == "paused_resumable" and available_work)
        self._resume.setEnabled(self._resume.isEnabled() and available_work)
        self._cancel.setVisible(cancel_offered)
        self._open_qupath.setVisible(saved_images_available)
        self._pause_help.setVisible(
            view.resume_disposition == "active_current_session" and self._pause_pending
        )
        self._refresh_step()
        self._update_segmentation_recovery()

    def _open_completed_review(self, stage, expected_head):
        """Admit a queued review only while its idle, successful status is current."""
        view = self._current_view
        current = self._stage_views.get(stage)
        if (not self._actions_valid or view is None or view.state_head_sha256 != expected_head
                or view.active_operation is not None or view.failure_count
                or current is None or current.status != "completed"):
            return
        (self.measurement_review_requested if stage == "measurements"
         else self.segmentation_review_requested).emit()

    def invalidate_actions(self) -> None:
        """Retire displayed action/close authority while owner state changes."""
        self.set_retry_choices(())
        self._authority_key = None
        self._close_authorized = False
        self._exit_state = SessionExitState.MUTABLE
        self._pause_pending = True
        for button in (self._pause, self._resume, self._continue_saved, self._cancel, self._recover_segmentation):
            button.setEnabled(False)
        self._state.setText("Updating saved state…")
        self._actions_valid = False
        self._plan_start_allowed = False
        self._start.setEnabled(False)
        self._step.review.setEnabled(False)
        self._step.skip.setEnabled(False)
        self._step.measurement_review.setEnabled(False)
        self._step.segmentation_review.setEnabled(False)
        self._step.proceed.setEnabled(False)

    def _button(self, text: str, name: str, callback: object) -> QPushButton:
        button = ContentSizedButton(text, self)
        button.setObjectName(name)
        button.clicked.connect(callback)  # type: ignore[arg-type]
        return button

    def set_retry_choices(self, choices: tuple[BatchRetryChoice, ...]) -> None:
        """Display only exact choices supplied for the current idle view."""
        if type(choices) is not tuple or any(
            type(item) is not BatchRetryChoice for item in choices
        ):
            raise TypeError("retry choices must be a tuple of BatchRetryChoice records")
        view = self._current_view
        if choices and (
            view is None
            or view.active_operation is not None
            or any(
                item.batch_id != view.batch_id
                or item.state_head_sha256 != view.state_head_sha256
                for item in choices
            )
        ):
            raise ValueError("retry choices do not match the current idle run view")
        if choices != self._retry_choices and self._retry_dialog is not None:
            self._retry_dialog.reject()
        self._retry_choices = choices
        self._retry.setText(
            "Edit spectral references…"
            if choices and all(c.retry_reason == "input_required" for c in choices)
            else "Retry step…"
        )
        self._retry.setVisible(bool(choices))
        self._retry.setEnabled(bool(choices))

    def _select_retry(self) -> None:
        choices = self._retry_choices
        if not choices or self._retry_dialog is not None:
            return
        dialog = QDialog(self)
        dialog.setObjectName("retry-step-dialog")
        edit_form = all(choice.retry_reason == "input_required" for choice in choices)
        dialog.setWindowTitle("Edit spectral references" if edit_form else "Retry step")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Choose the saved form to reopen." if edit_form else "Choose the step to retry.", dialog))
        selector = QComboBox(dialog)
        selector.setAccessibleName("Step to retry")
        selector.setPlaceholderText("Choose a step…")
        selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        selector.setMinimumContentsLength(24)
        selector.view().setTextElideMode(Qt.TextElideMode.ElideRight)
        for choice in choices:
            selector.addItem(
                f"{choice.sample_id} — {(BY_KIND[choice.stage_kind].title if choice.stage_kind in BY_KIND else choice.stage_kind.replace("_", " "))}"
            )
            selector.setItemData(selector.count() - 1, selector.itemText(selector.count() - 1), Qt.ItemDataRole.ToolTipRole)
        selector.setCurrentIndex(-1)
        layout.addWidget(selector)
        reason = QLabel(dialog)
        reason.setObjectName("retry-step-reason")
        reason.setTextFormat(Qt.TextFormat.PlainText)
        reason.setWordWrap(True)
        reason.hide()
        layout.addWidget(reason)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        retry = buttons.button(QDialogButtonBox.StandardButton.Ok)
        retry.setText("Open saved form" if edit_form else "Retry")
        retry.setProperty("actionRole", "primary")
        retry.setEnabled(False)
        def select_choice(index: int) -> None:
            retry.setEnabled(index >= 0)
            selector.setToolTip(selector.itemText(index) if index >= 0 else "")
            reason.setText(_RETRY_REASONS[choices[index].retry_reason] if index >= 0 else "")
            reason.setVisible(index >= 0)
            layout.activate()
            dialog.resize(dialog.width(), max(dialog.sizeHint().height(), layout.totalHeightForWidth(dialog.width())))

        selector.currentIndexChanged.connect(select_choice)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def finished(result: int) -> None:
            self._retry_dialog = None
            index = selector.currentIndex()
            if (
                result == QDialog.DialogCode.Accepted
                and choices == self._retry_choices
                and index >= 0
            ):
                self.retry_requested.emit(choices[index])
            dialog.deleteLater()

        dialog.finished.connect(finished)
        self._retry_dialog = dialog
        dialog.resize(max(360, min(620, self.width() - 48, self.screen().availableGeometry().width() - 48)), dialog.sizeHint().height())
        dialog.open()

    def can_close_immediately(self) -> bool:
        return (
            self._close_authorized
            and self._exit_state in _CLOSE_STATES
            and self._current_view is not None
            and self._current_view.resume_disposition != "active_current_session"
        )

    def request_close(self) -> bool:
        """Return supplied close authority or request one graceful pause."""

        if self.can_close_immediately():
            return True
        if (
            self._current_view is not None
            and self._current_view.resume_disposition == "active_current_session"
            and self._exit_state is SessionExitState.MUTABLE
        ):
            self._emit_pause()
        return False

    def _emit_refresh(self) -> None:
        if self._current_view is not None:
            self.refresh_requested.emit()

    def _emit_pause(self) -> None:
        if (
            self._current_view is None
            or self._current_view.resume_disposition != "active_current_session"
            or self._exit_state is not SessionExitState.MUTABLE
            or self._pause_pending
        ):
            return
        self._pause_pending = True
        self._pause.setEnabled(False)
        self._pause_help.show()
        self.pause_requested.emit()

    def _emit_resume(self) -> None:
        if (
            self._current_view is None
            or self._current_view.resume_disposition != "paused_resumable"
            or not self._resume.isEnabled()
        ):
            return
        self._resume.setEnabled(False)
        self.resume_requested.emit()

    def _emit_cancel(self) -> None:
        if not self._cancel.isEnabled():
            return
        self._cancel.setEnabled(False)
        self.cancel_requested.emit()

    def _emit_open_qupath(self) -> None:
        if self._open_qupath.isEnabled():
            self.open_qupath_requested.emit()

    def _disable_controls(self) -> None:
        for button in (
            self._refresh,
            self._pause,
            self._resume,
            self._continue_saved,
            self._recover_segmentation,
            self._cancel,
            self._open_qupath,
        ):
            button.setEnabled(False)


__all__ = ["RunPage"]
