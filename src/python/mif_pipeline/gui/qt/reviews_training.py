"""Display pending stage reviews and submit their current typed responses.

The page selects a request and target, constructs the applicable form, opens annotation or
inspection actions, and retains entries while background checks run. File observations and
submitted responses are matched to the current review. Failures remain local to the action
so other completed results stay available."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mif_pipeline.gui.interactions import (
    FormModel,
    FormTable,
    FormTableRow,
    PlotArtifact,
    decode_form_values,
    page_roi_inventory_form,
    present_manual_structure_selection,
    selected_content_keys_from_inventory_page,
)
from mif_pipeline.gui.tissue_setup import (
    ObservedTissueClassifierSource,
    observe_tissue_classifier_source,
)
from mif_pipeline.gui.presenters.reviews import (
    BatchInteractionForm,
    UnsupportedBatchInteraction,
    build_batch_interaction_form,
    build_batch_interaction_request,
)
from mif_pipeline.gui.presenters.aggregate_review import (
    AggregateReviewForm,
    build_aggregate_review_form,
    build_aggregate_review_request,
)
from mif_pipeline.orchestration.batch_interactions import (
    BatchAggregateInteractionGroup,
    BatchInteractionGroup,
    BatchInteractionPage,
    BatchInteractionQuery,
    BatchInteractionResponseRequest,
    BatchInteractionTarget,
    ReferenceOnlyQuPathView,
)
from mif_pipeline.orchestration.batch_service_models import BatchReviewPlots

from .forms import FormRenderer, apply_widget_accessibility
from .error_copy import diagnostic_details, operation_error_summary
from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .models import FormTableModel
from .marker_gates import MarkerGatePanel
from .workbench import PageHeading
from .researcher_widgets import ContentSizedButton


def _nimbus_checkpoint_status(explicit_path: str) -> str:
    """Inspect only local checkpoint readiness; numerical imports run at submission."""
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_absolute():
            return "Nimbus model: enter the full file path on this computer."
        if path.is_file():
            return f"Nimbus model file found: {path}. The model is checked when scoring starts."
        return f"Nimbus model file not found: {path}. Install it in Software setup or choose an existing model file."
    import sys
    from mif_pipeline.orchestration.setup.nimbus_setup import NimbusSetupService
    from mif_pipeline.pipeline_core.software_preferences import desktop_preferences_path

    status = NimbusSetupService(
        platform="win-x86_64" if sys.platform == "win32" else "linux-x86_64",
        preferences_path=desktop_preferences_path(),
    ).inspect()
    label = {"installed": "available", "missing": "missing", "incompatible": "needs attention"}[status.state]
    return f"Nimbus model: {label}. {status.detail} Location: {status.location}"

_REVIEW_TITLES = {
    "tissue_detector_setup_required": "Choose a tissue detector",
    "tissue_native_level_required": "Choose tissue-detection resolution",
    "tissue_detection_channel_required": "Choose a tissue channel",
    "tissue_section_review_required": "Review tissue sections",
    "warpy_registration_required": "Choose how to align paired images",
    "registration_review_required": "Review image alignment",
    "transfer_review_required": "Review transferred objects",
    "transfer_commit_recovery_required": "Resolve an interrupted object transfer",
    "training_plan_selection_required": "Choose a training plan",
    "primary_training_annotations_required": "Add primary training regions",
    "primary_classifier_review_required": "Review the primary classifier",
    "segmentation_setup_required": "Set up cell outlines",
    "measurement_setup_required": "Choose cell measurements",
    "secondary_training_annotations_required": "Add secondary training regions",
    "roi_review_required": "Review selected regions",
    "biomarker_threshold_required": "Choose a biomarker threshold",
    "phenotyping_mode_required": "Choose a phenotyping method",
    "phenotype_definition_count_required": "Choose the number of cell types",
    "phenotype_classes_required": "Name cell types",
    "phenotyping_measurements_required": "Choose cell type measurements",
    "marker_threshold_review_required": "Review marker thresholds",
    "marker_composite_rules_required": "Define marker combinations",
    "marker_rules_review_required": "Review marker rules",
    "supervised_phenotype_classes_required": "Name supervised cell types",
    "supervised_training_annotations_required": "Label reference cells",
    "supervised_classifier_review_required": "Review the supervised classifier",
    "clustering_configuration_required": "Set up clustering",
    "cluster_label_review_required": "Review cluster labels",
    "secondary_roi_overlap_policy_required": "Choose how overlapping regions are handled",
    "analysis_domain_choice_required": "Choose analysis area",
    "analysis_domain_rois_required": "Select analysis regions",
    "analysis_domain_review_required": "Review analysis regions",
    "structure_count_policy_acknowledgement_required": "Review structure count limits",
    "primary_structure_count_review_required": "Review region counts",
    "secondary_structure_selection_required": "Select secondary structures",
    "semantic_region_decision_required": "Review region evidence",
    "other_explicit_reason": "Review a required decision",
}


# ==============================================================================
#                                Reviews training
# ==============================================================================


def _review_title(reason: str) -> str:
    return _REVIEW_TITLES.get(reason, reason.replace("_", " ").capitalize())


class _BatchFormRenderer(FormRenderer):
    """Route real button clicks with raw values through the public presenter."""

    def __init__(
        self,
        submit_raw: Callable[[str, Mapping[str, object]], None],
        *,
        parent: QWidget,
    ) -> None:
        super().__init__(parent=parent)
        self._submit_raw = submit_raw

    def raw_values(self) -> dict[str, object]:
        return self._raw_values()

    def _submit(self, action: str, *, continue_after_save: bool = True) -> None:
        if self._advance_af_control_review(action):
            return
        self.last_submission_continuation = continue_after_save
        if self._reference_regions_panel is not None and not self._validate_current_fields(action=action):
            return
        self._submit_raw(action, self.raw_values())


class ReviewsTrainingPage(QWidget):
    """Render bounded interaction pages and emit only controller-owned intents."""

    page_requested = Signal(object)
    workflow_review_requested = Signal(object)
    acquisition_cycle_review_requested = Signal(str, str)
    results_requested = Signal()
    directory_requested = Signal()
    export_library_requested = Signal(str, str, object)
    back_to_workflow_requested = Signal()
    tissue_settings_back_requested = Signal()
    secondary_definition_back_requested = Signal()
    software_setup_requested = Signal()
    response_requested = Signal(object)
    streamed_reference_requested = Signal(object)
    streamed_reference_context_changed = Signal(bool)
    open_qupath_requested = Signal(object)
    feature_inspection_requested = Signal(object)
    reference_regions_requested = Signal(str, str, str, object)
    reference_component_requested = Signal(str, str, str)
    reference_preparation_requested = Signal(str, str, str, str)
    reference_preparation_regions_requested = Signal(str, str, str, str, str, object)
    reference_control_requested = Signal(str, str, str, str)
    open_plot_requested = Signal(object)
    review_plots_requested = Signal(str, str, object)
    prediction_page_requested = Signal(str, str, object)
    cutoff_assignment_requested = Signal(str, str, object)
    marker_combination_preview_requested = Signal(str, str, object, object)
    secondary_marker_requested = Signal(str, str, str, object, object)
    kronos_cell_preview_requested = Signal(str, str, object, object)
    marker_field_requested = Signal(str, str, object)
    cluster_model_requested = Signal(str, str)
    cluster_refit_requested = Signal(str, object)

    def __init__(
        self,
        *,
        segmentation_model_directory: Path | None = None,
        jobs: ManagedJobBridge | None = None,
        response_allowed: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if jobs is not None and not isinstance(jobs, ManagedJobBridge):
            raise TypeError("Reviews requires a managed job bridge")
        if response_allowed is not None and not callable(response_allowed):
            raise TypeError("Reviews response guard must be callable")
        self._response_allowed = response_allowed or (lambda: True)
        self._segmentation_model_directory = segmentation_model_directory
        self.remote_execution = False
        self.streamed_annotations_available = False
        self.secondary_back_caption = "Back"
        self.combination_preview_available = False
        self.remote_segmentation_settings = {}
        self.remote_kronos_setup = {}
        self._nimbus_setup_generation = 0
        self._nimbus_checkpoint_jobs: dict[str, int] = {}
        self._nimbus_check_timer = QTimer(self)
        self._nimbus_check_timer.setSingleShot(True)
        self._nimbus_check_timer.setInterval(180)
        self._nimbus_check_timer.timeout.connect(self._inspect_nimbus_checkpoint)

        self._workflow_title: str | None = None
        self._page: BatchInteractionPage | None = None
        self._groups: dict[str, BatchInteractionGroup | BatchAggregateInteractionGroup] = {}
        self._group_ids: tuple[str, ...] = ()
        self._displayed_group_id: str | None = None
        self._response_group_id: str | None = None
        self._state_head_sha256: str | None = None
        self._current_view: (
            BatchInteractionForm | AggregateReviewForm | UnsupportedBatchInteraction | None
        ) = None
        self._drafts: dict[tuple, dict] = {}
        self._classifier_reference_regions = {}
        self._saved_reference_editor = None
        self._saved_reference_origin = None
        self._saved_reference_pending = None
        self._last_reference_option: dict[tuple, str] = {}
        self._reference_source_selections: dict[tuple, dict[str, str]] = {}
        self._reference_source_additions: dict[tuple, dict[str, str]] = {}
        self._reference_component_entries = set()
        self._reference_components_open = False
        self._reference_component_inspection = None
        self._reference_preparation_read = None
        self._reference_preparation_origin = None
        self._reference_preparation_revisit = None
        self._reference_preparation_history = []
        self._reference_preparation_return_overview = False
        self._reference_control_inspection = None
        self._refresh_required = False
        self._response_pending = False
        self._response_status_received = False
        self._response_page_received = False
        self._qupath_project_path: Path | None = None
        self._marker_panel: MarkerGatePanel | None = None
        self._plot_artifacts: tuple[PlotArtifact, ...] = ()
        self._list_model: FormTableModel | None = None
        self._plot_model: FormTableModel | None = None
        self._inventory_model: FormTableModel | None = None
        self._inventory_form: FormModel | None = None
        self._inventory_selected_keys: tuple[str, ...] = ()
        self._inventory_syncing = False
        self._inventory_job_id: str | None = None
        self._submission_job_id: str | None = None
        self._tissue_observation_job_id: str | None = None
        self._tissue_observed_source: ObservedTissueClassifierSource | None = None
        self._tissue_refresh_values: tuple[str, str, dict[str, object]] | None = None
        self._jobs = jobs if jobs is not None else ManagedJobBridge(self)
        self._jobs.job_succeeded.connect(self._job_succeeded)
        self._jobs.job_failed.connect(self._job_failed)
        self._jobs.active_jobs_changed.connect(self._jobs_changed)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(12)
        self._workflow_back = ContentSizedButton("← Workflow", self)
        self._workflow_back.setObjectName("reviews-back-to-workflow")
        self._workflow_back.clicked.connect(self.back_to_workflow_requested)
        self._workflow_back.hide()
        navigation = QHBoxLayout()
        navigation.setContentsMargins(0, 0, 0, 10)
        navigation.addWidget(self._workflow_back, alignment=Qt.AlignmentFlag.AlignLeft)
        self._tissue_settings_back = ContentSizedButton("Back: tissue settings", self)
        self._tissue_settings_back.setObjectName("reviews-tissue-settings-back")
        self._tissue_settings_back.clicked.connect(self.tissue_settings_back_requested)
        self._tissue_settings_back.hide()
        navigation.addWidget(self._tissue_settings_back, alignment=Qt.AlignmentFlag.AlignLeft)
        navigation.addStretch(1)
        layout.addLayout(navigation)
        layout.addWidget(
            PageHeading(
                "reviews",
                "Reviews and training",
                "Open a pending stage to configure its inputs or review its results.",
                self,
            )
        )
        self._workspace = QStackedWidget(self)
        self._workspace.setObjectName("reviews-workspace")
        layout.addWidget(self._workspace, 1)
        inbox = QWidget(self._workspace)
        inbox.setObjectName("reviews-inbox-pane")
        self._inbox_pane = inbox
        inbox_layout = QVBoxLayout(inbox)
        inbox_layout.setContentsMargins(0, 8, 0, 0)
        inbox_layout.setSpacing(12)
        inbox_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        inbox_heading = QLabel("Review inbox", inbox)
        inbox_heading.setProperty("panelHeading", True)
        inbox_heading.setAccessibleName("Review inbox")
        inbox_heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        inbox_layout.addWidget(inbox_heading)
        self._empty = QLabel("No reviews are waiting on this page.", inbox)
        self._empty.setObjectName("reviews-empty")
        self._empty.setWordWrap(True)
        self._empty.setMaximumHeight(96)
        self._empty.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        inbox_layout.addWidget(self._empty)

        self._table = QTableView(inbox)
        self._table.setObjectName("reviews-list")
        self._table.clicked.connect(self._select_index)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().hide()
        self._table.horizontalHeader().sectionResized.connect(
            lambda *_args: self._table.resizeRowsToContents()
        )
        self._table.verticalHeader().hide()
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.hide()
        inbox_layout.addWidget(self._table)
        directory_actions = QHBoxLayout()
        self._directory_open = ContentSizedButton("Open step", inbox)
        self._directory_open.setObjectName("reviews-open-step")
        self._directory_open.setProperty("actionRole", "primary")
        self._directory_open.clicked.connect(self._open_directory_step)
        self._directory_results = ContentSizedButton("View results", inbox)
        self._directory_results.setObjectName("reviews-view-results")
        self._directory_results.clicked.connect(self.results_requested)
        directory_actions.addWidget(self._directory_open)
        directory_actions.addWidget(self._directory_results)
        directory_actions.addStretch(1)
        inbox_layout.addLayout(directory_actions)
        self._table.doubleClicked.connect(lambda _index: self._open_directory_step())

        paging = QHBoxLayout()
        self._previous = ContentSizedButton("Previous", inbox)
        self._previous.setObjectName("reviews-previous")
        self._previous.clicked.connect(self._request_previous)
        paging.addWidget(self._previous)
        self._next = ContentSizedButton("Next", inbox)
        self._next.setObjectName("reviews-next")
        self._next.clicked.connect(self._request_next)
        paging.addWidget(self._next)
        layout.addLayout(paging)

        detail_pane = QWidget(self._workspace)
        detail_pane.setObjectName("reviews-detail-pane")
        self._detail_pane = detail_pane
        detail_layout = QVBoxLayout(detail_pane)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        detail_layout.setSpacing(10)
        detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        detail_heading = QLabel("Review details", detail_pane)
        self._detail_heading = detail_heading
        detail_heading.setProperty("panelHeading", True)
        detail_heading.setAccessibleName("Review details")
        detail_heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        detail_layout.addWidget(detail_heading)
        stage_navigation = QHBoxLayout()
        self._count_navigation = False
        self._preferred_count_node = None
        self._review_group = QComboBox(detail_pane)
        self._review_group.setObjectName("reviews-stage-selector")
        self._review_group.setAccessibleName("Review in this step")
        self._review_group.currentIndexChanged.connect(self._select_stage_group)
        self._review_group.hide()
        stage_navigation.addWidget(self._review_group, 1)
        self._all_reviews = ContentSizedButton("All reviews", detail_pane)
        self._all_reviews.setObjectName("reviews-all-reviews")
        self._all_reviews.clicked.connect(self.directory_requested)
        navigation.addWidget(self._all_reviews, alignment=Qt.AlignmentFlag.AlignRight)
        self._reference_components_link = ContentSizedButton("Preparation overview", self)
        self._reference_components_link.setObjectName("reviews-reference-components")
        self._reference_components_link.setProperty('actionRole', 'secondary')
        self._reference_components_link.clicked.connect(self._open_reference_components)
        self._reference_components_link.hide()
        self._reference_components_layout = None
        detail_layout.addLayout(stage_navigation)
        from .primary_count_review import CountRegionSelection
        self._count_regions = CountRegionSelection(detail_pane)
        self._count_regions.inspected.connect(lambda group, ident: self.show_group(group, target_id=ident))
        self._count_regions.decided.connect(lambda action: self._submit_raw_response(action, {}))
        self._count_regions.open_requested.connect(self._emit_open_qupath)
        self._count_regions.hide()
        detail_layout.addWidget(self._count_regions)
        self._workspace.addWidget(inbox)
        self._workspace.addWidget(detail_pane)
        from .reference_components import ReferenceComponentsOverview, AcceptedReferenceSpectrum, AcceptedSharedControl
        self._reference_components = ReferenceComponentsOverview(self._workspace)
        self._reference_components.resumed.connect(self._resume_reference_component)
        self._reference_components.region_requested.connect(self._resume_reference_component)
        self._reference_components.inspection_requested.connect(self._inspect_reference_component)
        self._reference_components.control_inspection_requested.connect(self._inspect_shared_control)
        self._reference_components.source_requested.connect(self._open_reference_source)
        self._reference_components.sources_accepted.connect(self._accept_reference_sources)
        self._reference_components.option_requested.connect(self._navigate_reference_preparation)
        self._reference_components.preparation_requested.connect(self._open_reference_preparation)
        self._workspace.addWidget(self._reference_components)
        self._accepted_reference_spectrum = AcceptedReferenceSpectrum(self._workspace)
        self._workspace.addWidget(self._accepted_reference_spectrum)
        self._accepted_shared_control = AcceptedSharedControl(self._workspace)
        self._workspace.addWidget(self._accepted_shared_control)

        self._detail = QLabel("", self)
        self._detail.setObjectName("reviews-detail")
        self._detail.setAccessibleName("Review details")
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._target_label = QLabel("Review target", self)
        self._target_label.setObjectName("reviews-target-label")
        self._target_label.setVisible(False)
        self._target = QComboBox(self)
        self._target.setObjectName("reviews-target")
        self._target.setAccessibleName("Review target")
        self._target_label.setBuddy(self._target)
        self._target.currentIndexChanged.connect(self._target_changed)
        self._target.setVisible(False)

        self._reference_guidance_label = QLabel("QuPath view", self)
        self._reference_guidance_label.setObjectName("reviews-reference-guidance-label")
        self._reference_guidance_label.setVisible(False)
        self._reference_guidance = QComboBox(self)
        self._reference_guidance.setObjectName("reviews-reference-guidance")
        self._reference_guidance.setAccessibleName("QuPath view")
        self._reference_guidance_label.setBuddy(self._reference_guidance)
        self._reference_guidance.addItem("No paired reference", "primary-only")
        self._reference_guidance.addItem("Side-by-side reference", "paired-reference-only")
        self._reference_guidance.currentIndexChanged.connect(self._reference_guidance_changed)
        self._reference_guidance.setVisible(False)
        self._reference_guidance_help = QLabel(
            "The paired image is for visual reference only. Keep annotations on the primary mIF image.",
            self,
        )
        self._reference_guidance_help.setObjectName("reviews-reference-guidance-help")
        self._reference_guidance_help.setWordWrap(True)
        self._reference_guidance_help.setVisible(False)
        self._reference_detail = QLabel("", self)
        self._reference_detail.setObjectName("reviews-reference-detail")
        self._reference_detail.setWordWrap(True)
        self._reference_detail.setVisible(False)

        self._form_scroll = QScrollArea(detail_pane)
        self._form_scroll.setObjectName("reviews-form-scroll")
        self._form_scroll.setWidgetResizable(True)
        self._form_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._form_host = QWidget(self._form_scroll)
        self._form_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._form_layout = QVBoxLayout(self._form_host)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._form_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        for widget in (
            self._target_label,
            self._target,
            self._reference_guidance_label,
            self._reference_guidance,
            self._reference_guidance_help,
            self._reference_detail,
        ):
            self._form_layout.addWidget(widget)
        self._nimbus_setup_message = QLabel(self._form_host)
        self._nimbus_setup_message.setObjectName("reviews-nimbus-setup-message")
        self._nimbus_setup_message.setWordWrap(True)
        self._nimbus_setup_message.setTextFormat(Qt.TextFormat.PlainText)
        self._nimbus_checkpoint_status = QLabel(self._form_host)
        self._nimbus_checkpoint_status.setObjectName("reviews-nimbus-checkpoint-status")
        self._nimbus_checkpoint_status.setWordWrap(True)
        self._nimbus_checkpoint_status.setTextFormat(Qt.TextFormat.PlainText)
        self._nimbus_setup_button = ContentSizedButton("Software setup", self._form_host)
        self._nimbus_setup_button.setObjectName("reviews-nimbus-software-setup")
        self._nimbus_setup_button.setProperty("actionRole", "secondary")
        self._nimbus_setup_button.clicked.connect(self._request_nimbus_setup)
        for widget in (self._nimbus_setup_message, self._nimbus_checkpoint_status, self._nimbus_setup_button):
            self._form_layout.addWidget(widget)
            widget.hide()
        self._form_scroll.setWidget(self._form_host)
        self._form_scroll.hide()
        detail_layout.addWidget(self._form_scroll, 1)
        self._renderer = self._new_renderer()
        self._form_layout.addWidget(self._detail)
        self._detail.hide()
        self._actions = QWidget(detail_pane)
        self._actions.setObjectName("reviews-actions")
        self._actions_layout = QVBoxLayout(self._actions)
        self._actions_layout.setContentsMargins(16, 8, 16, 8)
        self._actions_layout.setSpacing(0)
        self._actions.setStyleSheet(
            "QWidget#reviews-actions { background: #ffffff; border-top: 1px solid #dce4e4; }"
        )
        self._actions_widget: QWidget | None = None
        self._actions.hide()
        detail_layout.addWidget(self._actions)
        self._continue_choice = QCheckBox("Continue automatically after saving", detail_pane)
        self._continue_choice.setObjectName("reviews-continue-after-save")
        self._continue_choice.setChecked(True)
        self._continue_choice.setToolTip(
            "Automatic work runs until another decision is needed. Clear this option to save and remain paused."
        )
        self._continue_choice.hide()
        detail_layout.addWidget(self._continue_choice)
        self._submitted_continuation = None

        self._inventory_panel = QWidget(self._form_host)
        self._inventory_panel.setObjectName("reviews-inventory-panel")
        inventory_layout = QVBoxLayout(self._inventory_panel)
        inventory_layout.setContentsMargins(16, 14, 16, 0)
        inventory_layout.setSpacing(10)
        self._inventory_prompt = QLabel(self._inventory_panel)
        self._inventory_prompt.setObjectName("reviews-inventory-prompt")
        self._inventory_prompt.setWordWrap(True)
        self._inventory_prompt.setTextFormat(Qt.TextFormat.PlainText)
        self._inventory_prompt.setProperty("stageDescription", True)
        inventory_layout.addWidget(self._inventory_prompt)
        inventory_heading = QLabel("Accepted structures", self._inventory_panel)
        inventory_heading.setProperty("panelHeading", True)
        inventory_layout.addWidget(inventory_heading)
        self._inventory = QTableView(self._inventory_panel)
        self._inventory.setObjectName("reviews-inventory")
        self._inventory.setAccessibleName("Choose accepted structures to include")
        self._inventory.setProperty("scientificTable", True)
        self._inventory.setAlternatingRowColors(True)
        self._inventory.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._inventory.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._inventory.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._inventory.verticalHeader().hide()
        self._inventory.verticalHeader().setDefaultSectionSize(38)
        self._inventory.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._inventory.setVisible(False)
        inventory_layout.addWidget(self._inventory)
        self._inventory_selection_status = QLabel(self._inventory_panel)
        self._inventory_selection_status.setObjectName("reviews-inventory-selection-status")
        self._inventory_selection_status.setWordWrap(True)
        self._inventory_selection_status.setProperty("fieldHelp", True)
        inventory_layout.addWidget(self._inventory_selection_status)
        inventory_paging = QHBoxLayout()
        self._inventory_previous = ContentSizedButton("Previous page", self._inventory_panel)
        self._inventory_previous.setObjectName("reviews-inventory-previous")
        self._inventory_previous.clicked.connect(self._request_inventory_previous)
        inventory_paging.addWidget(self._inventory_previous)
        self._inventory_next = ContentSizedButton("Next page", self._inventory_panel)
        self._inventory_next.setObjectName("reviews-inventory-next")
        self._inventory_next.clicked.connect(self._request_inventory_next)
        inventory_paging.addWidget(self._inventory_next)
        inventory_layout.addLayout(inventory_paging)
        self._inventory_panel.hide()
        self._form_layout.addWidget(self._inventory_panel)

        self._plots = QTableView(self)
        self._plots.setObjectName("reviews-plots")
        self._plots.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._plots.setVisible(False)
        self._form_layout.addWidget(self._plots)
        self._plot_selection = QComboBox(self)
        self._plot_selection.setObjectName("reviews-plot-selection")
        self._plot_selection.currentIndexChanged.connect(self._update_plot_open_enabled)
        self._plot_selection.setVisible(False)
        self._form_layout.addWidget(self._plot_selection)
        from .umap_review import UmapReviewControls
        self._umap = UmapReviewControls(self, collapsible=True)
        self._umap.requested.connect(self._request_umap)
        self._umap.cell_selected.connect(self._select_umap_cell)
        self._umap.export_view.clicked.connect(self._export_plot)
        self._form_layout.addWidget(self._umap)
        self._plot_status = QLabel("", self)
        self._plot_status.setObjectName("reviews-plot-status")
        self._plot_status.setTextFormat(Qt.TextFormat.PlainText)
        self._plot_status.setWordWrap(True)
        self._plot_status.hide()
        self._form_layout.addWidget(self._plot_status)

        self._annotation_image_label = QLabel("Reference image", self)
        self._annotation_image = QComboBox(self)
        self._annotation_image.setObjectName("reference-annotation-image")
        self._annotation_image.currentIndexChanged.connect(self._annotation_image_changed)
        self._annotation_image_label.hide()
        self._annotation_image.hide()
        self._form_layout.addWidget(self._annotation_image_label)
        self._form_layout.addWidget(self._annotation_image)
        artifacts = QHBoxLayout()
        self._artifact_actions = artifacts
        artifacts.setContentsMargins(16, 6, 16, 8)
        self._open_qupath = ContentSizedButton("Open QuPath", self)
        self._open_qupath.setObjectName("reviews-open-qupath")
        self._open_qupath.clicked.connect(self._emit_open_qupath)
        qupath_actions = QVBoxLayout()
        qupath_actions.setSpacing(8)
        qupath_actions.addWidget(self._open_qupath, alignment=Qt.AlignmentFlag.AlignLeft)
        self._qupath_save_help = QLabel("", self)
        self._qupath_save_help.setWordWrap(True)
        self._qupath_save_help.setObjectName("reviews-qupath-save-help")
        qupath_actions.addWidget(self._qupath_save_help)
        artifacts.addLayout(qupath_actions)
        self._open_plot = ContentSizedButton("Open plot", self)
        self._open_plot.setObjectName("reviews-open-plot")
        self._open_plot.clicked.connect(self._emit_open_plot)
        artifacts.addWidget(self._open_plot)
        self._export_plot_button = ContentSizedButton("Export figure…", self)
        self._export_plot_button.setObjectName("reviews-export-figure")
        self._export_plot_button.hide()
        self._export_plot_button.clicked.connect(self._export_plot)
        artifacts.addWidget(self._export_plot_button)
        self._export_library = ContentSizedButton("Export reference library…", self)
        self._export_library.setObjectName("reviews-export-library")
        self._export_library.hide()
        self._export_library.clicked.connect(self._choose_library_export)
        artifacts.addWidget(self._export_library)
        self._reference_region_actions = QWidget(self)
        region_layout = QHBoxLayout(self._reference_region_actions)
        region_layout.setContentsMargins(0, 0, 0, 0)
        self._reference_region_buttons = {}
        for action, label in (("edit", "Edit regions in QuPath"), ("load", "Load saved regions"),
                              ("accept", "Accept displayed regions")):
            button = ContentSizedButton(label, self)
            button.setObjectName("reference-regions-" + action)
            button.clicked.connect(lambda _checked=False, choice=action: self._request_reference_regions(choice))
            self._reference_region_buttons[action] = button
            region_layout.addWidget(button)
        region_layout.addStretch(1)
        self._reference_region_actions.hide()
        self._form_layout.addWidget(self._reference_region_actions)
        self._form_layout.addLayout(artifacts)
        self._form_layout.addStretch(1)

        self._status = QLabel("", self)
        self._status.setObjectName("reviews-status")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.hide()
        layout.addWidget(self._status)
        self._error_details_toggle = QToolButton(self)
        self._error_details_toggle.setObjectName("reviews-error-details-toggle")
        self._error_details_toggle.setText("Details")
        self._error_details_toggle.setCheckable(True)
        self._error_details_toggle.hide()
        layout.addWidget(self._error_details_toggle)
        self._error_details = QLabel("", self)
        self._error_details.setObjectName("reviews-error-details")
        self._error_details.setWordWrap(True)
        self._error_details.setTextFormat(Qt.TextFormat.PlainText)
        self._error_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._error_details.hide()
        layout.addWidget(self._error_details)
        self._error_details_toggle.toggled.connect(self._error_details.setVisible)
        self._set_empty_controls()
        self.set_workflow_context(None)
        apply_widget_accessibility(self, "reviews")

    def _jobs_changed(self, active):
        """Refresh saved reference choices after shared work finishes."""
        if not active:
            QTimer.singleShot(0, self, self._bind_saved_reference_choices)
            QTimer.singleShot(0, self, self._autoload_reference_regions)

    def _open_region_appearance(self, metadata):
        """Use the shared appearance editor for the image currently being reviewed."""
        from .image_appearance_editor import ImageAppearanceDialog
        from .region_review import RegionDecisionReview
        if not metadata or not metadata.get('image_uri'):
            return
        region = self._renderer.findChild(RegionDecisionReview)
        dialog = ImageAppearanceDialog(
            [(region.projection.get('slide_id', 'Current image'), metadata['image_uri'])],
            getattr(self, 'image_appearance_path', None), self._jobs, self)
        dialog.saved.connect(region.refresh_appearance)
        dialog.show()

    def release_jobs(self):
        """Detach a closed analysis review from the shared job bridge."""
        self._nimbus_check_timer.stop()
        self._jobs.job_succeeded.disconnect(self._job_succeeded)
        self._jobs.job_failed.disconnect(self._job_failed)
        self._jobs.active_jobs_changed.disconnect(self._jobs_changed)

    def set_verified_segmentation_model_directory(self, directory: Path) -> None:
        """Update the default for future forms without replacing current edits."""
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("verified model directory must be absolute")
        self._segmentation_model_directory = directory

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._size_inbox_rows()

    def _size_inbox_rows(self) -> None:
        if self._list_model is None:
            return
        metrics = self._table.fontMetrics()
        width = max(120, self._table.viewport().width() - 16)
        for row, group_id in enumerate(self._group_ids):
            summary = self._inbox_summary(self._groups[group_id])
            wrapped_height = metrics.boundingRect(
                QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, summary
            ).height()
            self._table.setRowHeight(row, max(3 * metrics.lineSpacing(), wrapped_height) + 12)
        content_height = sum(self._table.rowHeight(row) for row in range(len(self._group_ids))) + 14
        self._table.setFixedHeight(min(max(70, content_height), max(200, self.height() - 260)))

    def _new_renderer(self) -> _BatchFormRenderer:
        renderer = _BatchFormRenderer(self._submit_raw_response, parent=self._form_host)
        renderer.tissue_authoring_requested.connect(self._open_tissue_authoring)
        renderer.feature_inspection_requested.connect(self._request_feature_inspection)
        renderer.secondary_marker_requested.connect(self._request_secondary_marker)
        renderer.secondary_definition_back_requested.connect(self.secondary_definition_back_requested)
        renderer.kronos_cell_preview_requested.connect(self._request_kronos_cell_preview)
        renderer.tissue_source_selected.connect(self._observe_tissue_source)
        renderer.tissue_source_invalidated.connect(self._invalidate_tissue_source)
        renderer.page_changed.connect(
            lambda: self._form_scroll.verticalScrollBar().setValue(0)
        )
        renderer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        renderer.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
        renderer.page_changed.connect(self._update_action_visibility)
        renderer.page_changed.connect(self._sync_reference_review_heading)
        renderer.reference_image_selected.connect(self._select_annotation_image)
        renderer.cancelled.connect(self._leave_form)
        renderer.reference_back_requested.connect(self._reference_back)
        renderer.acquisition_cycle_review_requested.connect(self._review_acquisition_cycles)
        renderer.setVisible(False)
        self._form_layout.insertWidget(
            self._form_layout.indexOf(self._nimbus_setup_button) + 1, renderer
        )
        return renderer

    def _sync_reference_review_heading(self):
        """Show the current task title for scoped reference and secondary classifier pages."""
        if self._workflow_title is None:
            return
        title = self._workflow_title
        from .secondary_navigation import SavedSecondaryEditor
        saved_definition = self._workspace.currentWidget()
        if isinstance(saved_definition, SavedSecondaryEditor):
            title = saved_definition.definitions.page_title()
        elif self._count_navigation:
            title = 'Review region counts'
        elif self._reference_components_open:
            title = 'Prepare the reference library'
        elif self._renderer._form is not None and (
                self._renderer._form.context.get("adapter") == "secondary_definition" or
                self._renderer._form.context.get("adapter") == "training_plan_selection"
                and self._renderer._form.context.get("secondary")):
            title = self._renderer._heading.text()
        elif (getattr(self._renderer, '_af_control_review_panel', None) is not None
                or self._renderer._form is not None
                and (self._renderer._form.context.get('reference_source_step')
                     or self._renderer._form.context.get('reference_region_workspace')
                     or self._renderer._form.context.get('reference_preparation_revisit')
                     or self._renderer._form.context.get('selected_option') == 'revise_shared_control')):
            title = self._renderer._heading.text()
        self.findChild(QLabel, 'reviews-title').setText(title)
        if (not self._reference_components_open and not self._reference_components_link.isHidden()
                and self._workspace.currentWidget() is self._detail_pane):
            self._mount_reference_components_link(self._renderer)

    def _review_acquisition_cycles(self):
        """Retain the channel draft and request Inspection for this exact review."""
        view = self._current_view
        if (self._refresh_required or self._response_pending or self._submission_job_id is not None
                or not self._response_allowed() or type(view) is not BatchInteractionForm
                or view.expected_state_head_sha256 != self._state_head_sha256
                or view.form.context.get('adapter') != 'batch_stage1'
                or not view.form.context.get('acquisition_cycle_review_available')):
            return
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return
        target = next((item for item in group.targets if item.target_id == self._target.currentData()), None)
        if target is None or target.target_id != view.representative_target_id:
            return
        self._capture_draft()
        self.acquisition_cycle_review_requested.emit(target.node_id, view.expected_state_head_sha256)

    def _replace_renderer(self) -> None:
        self._detach_reference_components_link()
        self._detach_inventory_panel()
        self._clear_actions()
        self._cluster_accept_actions=None
        old = self._renderer
        self._form_layout.removeWidget(old)
        old.setParent(None)
        old.deleteLater()
        self._renderer = self._new_renderer()

    def _clear_actions(self) -> None:
        self._reference_footer_overview_actions = None
        if self._actions_widget is not None:
            self._actions_layout.removeWidget(self._actions_widget)
            self._actions_widget.setParent(None)
            self._actions_widget.deleteLater()
            self._actions_widget = None
        self._actions.setMinimumHeight(0)
        self._actions.hide()

    def _detach_inventory_panel(self) -> None:
        """Keep the managed selection table alive when its form workspace is replaced."""
        if self._inventory_panel.parentWidget() is not self._form_host:
            self._inventory_panel.setParent(self._form_host)
            self._form_layout.addWidget(self._inventory_panel)

    def _set_rendered_form(self, form: FormModel) -> None:
        """Keep stage navigation and save actions below the scrolling content.

        We render the existing typed form, then move its navigation and final
        action layout into one footer. The renderer retains editor values and
        submission ownership. Replacing the form disposes the previous footer
        with its navigation, so actions cannot refer to an earlier review.
        """
        self._detach_inventory_panel()
        self._detach_reference_components_link()
        self._clear_actions()
        self._cluster_accept_actions=None
        form = present_manual_structure_selection(form)
        has_inventory = any(table.table_id.startswith("inventory_page:") for table in form.tables)
        rendered = (
            replace(
                form,
                controls=tuple(
                    control for control in form.controls if control.name != "selected_content_keys"
                ),
                message="",
                tables=tuple(table for table in form.tables
                             if table.table_id not in {"v2_context", "inventory_descriptor"}
                             and not table.table_id.startswith("inventory_page:")),
            )
            if has_inventory
            else form
        )
        if has_inventory and rendered.context.get("region_review") is not None:
            inventory = next(table for table in form.tables if table.table_id.startswith("inventory_page:"))
            region_ids = {row.values[1] for row in inventory.rows}
            projection = rendered.context["region_review"]
            rendered = replace(rendered, context={**rendered.context, "region_review": {
                **projection, "regions": tuple(row for row in projection["regions"]
                                               if row["region_id"] in region_ids)}})
        if any(control.name=='cluster_decisions' for control in rendered.controls) and not rendered.context.get('refit_summary'):
            rendered=replace(rendered,message='')
        if (rendered.form_id == 'training_fit_review' and rendered.context['stage_result']['stage'] in {
                'primary_classifier_training_review', 'secondary_classifier_training_review'}):
            # The workspace owns the image action and evaluation-scope heading.
            # Keep acceptance/test sequencing and revision messages from the presenter.
            import re
            message = rendered.message.replace(
                'Open mIF → Review fitted classifier in QuPath to inspect its predictions before accepting the model.', '')
            message = re.sub(r'No subjects were kept separate for validation or independent testing[^.]*\.', '', message)
            rendered = replace(rendered, message=message.strip())
        self._renderer.native_feature_inspection_available = not self.remote_execution
        self._renderer.secondary_back_caption = self.secondary_back_caption
        self._umap.setParent(self)
        self._umap.hide()
        self._detach_reference_components_link()
        if self._count_navigation and rendered.context.get("reason") == "primary_structure_count_review_required":
            rendered = replace(rendered, message="", tables=())
        self._renderer.set_form(rendered)
        self._sync_reference_review_heading()
        reference_plan = self._renderer._reference_plan
        if reference_plan is not None:
            if self.remote_execution:
                reference_plan.annotation_requested.connect(
                    lambda: self.streamed_reference_requested.emit(reference_plan.image.currentData()))
            else:
                reference_plan.annotation_requested.connect(self._open_qupath.click)
        cell_references = self._renderer._supervised_reference_panel
        if cell_references is not None:
            if self.remote_execution:
                cell_references.annotation_requested.connect(
                    lambda: self.streamed_reference_requested.emit(cell_references.image.currentData()))
            else:
                cell_references.annotation_requested.connect(self._open_qupath.click)
            cell_references.reference_selected.connect(self._select_cell_annotation)
        from .supervised_prediction_review import SupervisedPredictionReview
        prediction_review = self._renderer.findChild(SupervisedPredictionReview)
        if prediction_review is not None:
            prediction_review.scoped_assignment_available = bool(
                getattr(self, 'scoped_cutoff_available', lambda: False)())
            prediction_review.image_requested.connect(self._request_region_image)
            prediction_review.page_requested.connect(self._request_prediction_page)
            prediction_review.assign_requested.connect(self._request_cutoff_assignment)
            prediction_review.field.requested.connect(self._request_marker_field)
        from .cluster_review import ClusterReview
        cluster_review=self._renderer.findChild(ClusterReview)
        if cluster_review is not None:
            for widget in (self._open_qupath,self._qupath_save_help,self._plots,
                           self._plot_selection,self._open_plot,self._export_plot_button):
                widget.hide()
            from .flowsom_map import FlowSomMap
            cluster_review.attach_model_view(FlowSomMap(cluster_review))
            cluster_review.image_requested.connect(self._request_region_image)
            cluster_review.field.requested.connect(self._request_marker_field)
            cluster_review.field.image_appearance_path = getattr(self, 'image_appearance_path', None)
            cluster_review.page_changed.connect(lambda:self._form_scroll.verticalScrollBar().setValue(0))
            cluster_review.page_changed.connect(self._update_action_visibility)
            cluster_review.refit_requested.connect(self._request_cluster_refit)
            help_label=self._renderer.findChild(QLabel,'form-help-cluster_decisions')
            if help_label is not None:help_label.hide()
            cluster_review.cluster_selected.connect(lambda _cluster:cluster_review._highlight_selection())
        if rendered.context.get('adapter') == 'marker_combinations':
            feedback = self._renderer._marker_combinations.feedback
            feedback.setVisible(self.combination_preview_available)
            feedback.update_requested.connect(self._request_combination_preview)
            self._renderer._marker_combinations.review.requested.connect(self._request_marker_field)
            self._renderer._marker_combinations.review.image_appearance_path = getattr(self, 'image_appearance_path', None)
            if self.combination_preview_available:
                feedback.enable_live_updates()
        from .training_fit_summary import TrainingFitSummary
        fit_workspace = self._renderer.findChild(TrainingFitSummary)
        if fit_workspace is not None:
            fit_workspace.inspect_predictions_requested.connect(self._emit_open_qupath)
            from .supervised_fit_review import SupervisedCellEvaluation
            if isinstance(fit_workspace.evaluation_panel, SupervisedCellEvaluation):
                fit_workspace.evaluation_panel.image_requested.connect(self._request_supervised_cell_image)
        region_review = bool(rendered.context.get("reference_regions_available"))
        region_workspace = self._renderer._reference_regions_panel
        self._reference_region_actions.setVisible(region_review and region_workspace is None)
        if region_workspace is not None:
            region_workspace.native_action_requested.connect(self._request_reference_regions)
            if hasattr(region_workspace, "revision_requested"):
                region_workspace.revision_requested.connect(self._navigate_reference_preparation)
        if region_review:
            summary = rendered.context.get("reference_regions_summary")
            self._reference_region_buttons["edit"].setVisible(
                rendered.context["reference_regions_checkpoint"] in {"shared_control_preparation", "positive_reference_rois"})
            self._reference_region_buttons["accept"].setEnabled(bool(
                summary and summary["rows"] and not summary["accepted"]))

        scoped_reference = self._workflow_title is not None and rendered.context.get("adapter") in {
            "batch_reference_start", "batch_reference_operation"
        }
        pages = self._renderer._stage_pages
        classifier_groups = pages is not None and pages.classifier_overview is not None
        self._actions.setStyleSheet(
            "QWidget#reviews-actions { background: transparent; border: none; }"
            if scoped_reference else
            "QWidget#reviews-actions { background: #ffffff; border-top: 1px solid #dce4e4; }")
        compact_inset = scoped_reference or (self._workflow_title is not None and classifier_groups)
        self._renderer.layout().setContentsMargins(*((6, 0, 6, 12) if compact_inset else (16, 14, 16, 18)))
        if self._workflow_title is not None:
            self._renderer._heading.hide()
        if has_inventory:
            from .region_review import RegionDecisionReview
            region = self._renderer.findChild(RegionDecisionReview)
            if region is not None:
                # The existing bounded image view stays inside the renderer so
                # ordinary image requests, appearance and error handling still apply.
                layout = self._renderer.layout()
                layout.removeWidget(region)
                self._form_layout.removeWidget(self._inventory_panel)
                workspace = QWidget(self._renderer)
                workspace.setObjectName("structure-selection-workspace")
                columns = QHBoxLayout(workspace)
                columns.setContentsMargins(0, 0, 0, 0)
                columns.setSpacing(16)
                columns.addWidget(self._inventory_panel, 2)
                columns.addWidget(region, 3)
                columns.setAlignment(self._inventory_panel, Qt.AlignmentFlag.AlignTop)
                self._inventory_panel.layout().setContentsMargins(0, 0, 0, 0)
                self._inventory_prompt.hide()
                layout.removeWidget(self._renderer._message)
                self._renderer._message.setText(form.message)
                self._renderer._message.show()
                layout.insertWidget(0, self._renderer._message)
                layout.insertWidget(1, workspace)
                region.view.inspected.connect(self._select_inventory_regions)
            else:
                self._form_layout.removeWidget(self._inventory_panel)
                self._form_layout.insertWidget(
                    self._form_layout.indexOf(self._renderer), self._inventory_panel)
                self._inventory_panel.layout().setContentsMargins(16, 14, 16, 0)
                self._inventory_prompt.show()
            self._renderer._heading.setText("Selection details")
            self._renderer._heading.show()
            action = self._renderer.findChild(
                QPushButton, f"form-{form.form_id}-action-accept_secondary_structure_selection")
            if action is not None:
                action.setText("Save selection")
        else:
            self._inventory_panel.hide()
        if rendered.context.get("adapter") == "marker_setup":
            self._renderer._marker_selector.currentIndexChanged.connect(self._update_nimbus_setup)
            for name, editor in self._renderer._editors.items():
                if name.endswith(("_method", "_nimbus_source")):
                    editor.currentIndexChanged.connect(self._update_nimbus_setup)
                elif name.endswith("_nimbus_checkpoint_path"):
                    editor.textChanged.connect(self._update_nimbus_setup)
            requirement = rendered.context["stage_result"]["resume_context"].get("software_requirement")
            if requirement:
                self._renderer._set_marker_step(1)
        self._update_nimbus_setup()
        if self.remote_execution and rendered.context.get("adapter") == "alignment_setup":
            choice = self._renderer._editors["alignment_method"]
            empty = choice.findData(None)
            if empty >= 0:
                choice.removeItem(empty)
        if (self._workflow_title is not None
                and rendered.context.get("adapter") not in {"batch_reference_start", "batch_reference_operation"}):
            # A recently replaced form may still await deferred deletion.
            for leave in self._renderer.findChildren(QPushButton, f"form-{form.form_id}-cancel"):
                leave.hide()
        if (rendered.context.get("reason") == "analysis_domain_choice_required"
                or rendered.context.get("adapter") in {"secondary_definition", "tissue_setup"}
                or classifier_groups
                or (rendered.context.get("reason") == "semantic_region_decision_required"
                    and (rendered.context.get("region_review") or {}).get("kind") == "secondary")):
            # These forms keep their workflow actions beside their content.
            self._actions.hide()
            self._continue_choice.hide()
            self._form_scroll.verticalScrollBar().setValue(0)
            return
        layout = self._renderer.layout()
        actions = self._renderer._reference_navigation_layout
        if actions is not None:
            owner = actions.parent()
            if isinstance(owner, QLayout):
                owner.removeItem(actions)
            actions.setParent(None)
        else:
            item = layout.takeAt(layout.count() - 1)
            actions = item.layout()
        assert actions is not None
        if self._renderer._reference_band_panel is not None:
            # Its navigation now lives in the persistent page footer.
            self._renderer._reference_band_panel._review_actions.hide()
        host = QWidget(self._actions)
        # Wrapping navigation has a zero-width size hint, which represents
        # one button per row. Let height-for-width determine the actual rows
        # instead of promoting that hint to a permanent minimum height.
        host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        footer = QHBoxLayout(host)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        navigation = cluster_review.navigation if cluster_review is not None else self._renderer.navigation_widget
        if navigation is not None:
            (cluster_review.layout() if cluster_review is not None else layout).removeWidget(navigation)
            footer.addWidget(navigation, 1)
        actions.setContentsMargins(0, 8, 0, 8)
        if self._renderer._reference_extraction_panel is not None:
            # The former wrapping row had no internal vertical padding.
            # Keep its height when aligning the primary action to the right.
            actions.setContentsMargins(0, 0, 0, 0)
        actions.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        if cluster_review is not None:
            self._cluster_accept_actions=QWidget(host)
            self._cluster_accept_actions.setLayout(actions)
            footer.addWidget(self._cluster_accept_actions)
        else:
            footer.addLayout(actions)
        if rendered.context.get('adapter') in {'batch_reference_start', 'batch_reference_operation'}:
            self._reference_footer_overview_actions = footer
        self._actions_layout.addWidget(host)
        self._actions_widget = host
        self._renderer._stage_action_footer = host
        self._actions.setMinimumHeight(0)
        self._actions_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._actions.setEnabled(True)
        self._update_action_visibility()
        self._continue_choice.hide()
        self._form_scroll.verticalScrollBar().setValue(0)

    def _update_action_visibility(self) -> None:
        form = self._renderer._form
        if self._count_navigation and form is not None and form.context.get("reason") == "primary_structure_count_review_required":
            self._actions.hide()
            return
        if (form is not None and form.context.get("reference_region_workspace")
                and not form.allowed_actions and not form.context.get("back_option")):
            self._actions.hide()
            return
        cluster=self._renderer._cluster_review
        if cluster is not None:
            self._actions.show()
            if getattr(self,'_cluster_accept_actions',None) is not None:
                self._cluster_accept_actions.setVisible(cluster.pages.currentIndex()==1)
            return
        if form is not None and (
                form.context.get("reason") == "analysis_domain_choice_required"
                or form.context.get("adapter") in {"secondary_definition", "tissue_setup"}
                or (self._renderer._stage_pages is not None
                    and self._renderer._stage_pages.classifier_overview is not None)
                or (form.context.get("reason") == "semantic_region_decision_required"
                    and (form.context.get("region_review") or {}).get("kind") == "secondary")):
            self._actions.hide()
            self._update_training_annotation_visibility()
            return
        initial_reference = form is not None and form.context.get("adapter") == "batch_reference_start"
        self._actions.setVisible(not initial_reference or any(
            not button.isHidden() for button in self._renderer._unmixing_buttons.values()
        ))
        self._update_training_annotation_visibility()

    def _select_annotation_image(self, entry_id: str) -> None:
        index = self._annotation_image.findData(entry_id)
        if index >= 0:
            self._annotation_image.setCurrentIndex(index)

    def _select_cell_annotation(self, identity) -> None:
        index = next((index for index in range(self._annotation_image.count())
                      if tuple(self._annotation_image.itemData(index)) == tuple(identity)), -1)
        if index >= 0:
            self._annotation_image.setCurrentIndex(index)

    def _update_training_annotation_visibility(self) -> None:
        """Associate the existing native annotation action with its selected image."""
        form = self._renderer._form
        available = self.streamed_annotations_available if self.remote_execution else self._qupath_project_path is not None
        if self.remote_execution:
            guidance = ("QuPath opens the selected image on this computer. Save the annotations before closing QuPath to return them to the cluster."
                        if available else "Set up mounted image access and local QuPath to annotate this image.")
        else:
            guidance = "" if available else "The QuPath project is not available for annotation."
        cell_panel = getattr(self._renderer, "_supervised_reference_panel", None)
        if form is not None and form.context.get("adapter") == "supervised_references" and cell_panel is not None:
            self._open_qupath.hide()
            self._qupath_save_help.hide()
            self._annotation_image.hide()
            self._annotation_image_label.hide()
            cell_panel.select_reference(self._annotation_image.currentData())
            cell_panel.set_annotation_available(available, guidance)
            return
        if form is None or not (form.context.get("adapter") == "training_plan_selection" and (form.context.get("primary") or form.context.get("secondary"))):
            return
        pages = self._renderer._stage_pages
        panel = getattr(self._renderer, "_reference_plan", None)
        visible = pages is not None and pages.current_key == "references"
        native = visible and available
        self._open_qupath.setVisible(native and panel is None)
        self._qupath_save_help.setVisible(native and panel is None)
        # The reference workspace owns selection and presentation for this form.
        self._annotation_image.setVisible(visible and panel is None and self._annotation_image.count() > 1)
        self._annotation_image_label.setVisible(not self._annotation_image.isHidden())
        if panel is not None:
            self._open_qupath.setEnabled(native and panel._selected is not None and bool(panel._entries))
            panel.set_annotation_available(native, guidance)

    def offer_classifier_reference_regions(self, result) -> None:
        """Attach informational counts only to the current successful annotation return."""
        summary = result.get("reference_regions") if isinstance(result, dict) else None
        view = self._current_view
        if (not isinstance(summary, dict) or result.get("status") != "annotations_saved"
                or type(view) is not BatchInteractionForm or not (view.form.context.get("primary") or view.form.context.get("secondary"))
                or result.get("state_head_sha256") != self._state_head_sha256
                or summary.get("saved_annotations_sha256") != result.get("saved_annotations_sha256")):
            return
        group = self._groups.get(view.group_id)
        target = next((item for item in getattr(group, "targets", ())
                       if item.target_id == self._target.currentData()), None)
        if (target is None or summary.get("request_id") != target.interaction_request_id
                or summary.get("interaction_context_sha256") != target.interaction_context_sha256):
            return
        panel = getattr(self._renderer, "_reference_plan", None)
        if panel is None or summary.get("project_entry_id") not in {
                entry for allocation in panel.allocations.values() for entries in allocation.subjects.values() for entry in entries}:
            return
        key = (summary["request_id"], summary["interaction_context_sha256"], summary["project_entry_id"])
        self._classifier_reference_regions[key] = summary
        self._refresh_classifier_reference_regions()

    def _refresh_classifier_reference_regions(self) -> None:
        panel = getattr(self._renderer, "_reference_plan", None)
        view = self._current_view
        if panel is None or type(view) is not BatchInteractionForm:
            return
        group = self._groups.get(view.group_id)
        target = next((item for item in getattr(group, "targets", ())
                       if item.target_id == self._target.currentData()), None)
        if target is None:
            return
        prefix = (target.interaction_request_id, target.interaction_context_sha256)
        panel.set_saved_regions({key[2]: summary for key, summary in self._classifier_reference_regions.items() if key[:2] == prefix})

    def _automatic_nimbus_input(self):
        form = self._renderer._form
        if form is None or form.context.get("adapter") != "marker_setup":
            return None
        marker = self._renderer._marker_selector.currentData()
        prefix = f"marker_{marker}_"
        editors = self._renderer._editors
        if (editors[prefix + "method"].currentData() != "nimbus"
                or editors[prefix + "nimbus_source"].currentData() != "automatic"):
            return None
        return marker, editors[prefix + "nimbus_checkpoint_path"].text().strip()

    def _update_nimbus_setup(self, *_args) -> None:
        """Offer setup on selection, with host-specific guidance and no downloads."""
        self._nimbus_check_timer.stop()
        self._nimbus_setup_generation += 1
        selected = self._automatic_nimbus_input()
        for widget in (self._nimbus_setup_message, self._nimbus_checkpoint_status, self._nimbus_setup_button):
            widget.hide()
        if selected is None:
            return
        context = self._renderer._form.context["stage_result"]["resume_context"]
        requirement = context.get("software_requirement") or {}
        problem = str(requirement.get("message", ""))
        if self.remote_execution:
            guidance = (
                "Install or select the Nimbus model on the cluster. "
                "If entering a model path here, use its full path on the cluster."
            )
        else:
            guidance = (
                "Install or select the Nimbus model in Software setup → Cell phenotyping."
            )
            self._nimbus_setup_button.show()
            self._nimbus_setup_button.setEnabled(self._renderer.isEnabled())
            self._nimbus_checkpoint_status.setText("Checking the Nimbus model…")
            self._nimbus_checkpoint_status.show()
            self._nimbus_check_timer.start()
        self._nimbus_setup_message.setText((problem + "\n" if problem else "") + guidance)
        self._nimbus_setup_message.show()

    def _inspect_nimbus_checkpoint(self) -> None:
        selected = self._automatic_nimbus_input()
        if selected is None or self.remote_execution:
            return
        path = selected[1]
        try:
            job_id = self._jobs.submit("inspect-nimbus-checkpoint", lambda: _nimbus_checkpoint_status(path), access=JobAccess.READ)
        except (TypeError, ValueError, RuntimeError) as error:
            self._nimbus_checkpoint_status.setText(f"Nimbus model could not be checked: {error}")
            return
        self._nimbus_checkpoint_jobs[job_id] = self._nimbus_setup_generation

    def _request_nimbus_setup(self) -> None:
        if (self.remote_execution or self._automatic_nimbus_input() is None
                or not self._renderer.isEnabled() or not self._response_allowed()):
            return
        self._capture_draft()
        self.software_setup_requested.emit()

    @property
    def continue_after_save(self) -> bool:
        return self._continue_choice.isChecked()

    def take_alignment_options(self, request):
        submitted = getattr(self, "_alignment_submission", None)
        self._alignment_submission = None
        return submitted[1] if submitted is not None and submitted[0] is request else None

    def take_continuation_intent(self, request: BatchInteractionResponseRequest) -> bool:
        submitted, self._submitted_continuation = self._submitted_continuation, None
        return bool(submitted is not None and submitted[0] is request and submitted[1])

    def select_pending_count_node(self, node_id: str) -> None:
        self._preferred_count_node = node_id

    def set_workflow_context(self, title: str | None) -> None:
        self._preferred_count_node = None
        self._workflow_title = title
        self._tissue_settings_back.hide()
        self._detail_heading.setVisible(title is None)
        self.findChild(QLabel, "reviews-information").setVisible(title is None)
        self._workflow_back.setVisible(title is not None)
        self._all_reviews.setVisible(title is not None)
        self._workspace.setCurrentWidget(
            self._inbox_pane if title is None else self._detail_pane
        )
        self._directory_open.setEnabled(False)
        self.findChild(QLabel, "reviews-title").setText(title or "Reviews and training")
        self.findChild(QLabel, "reviews-information").setText(
            "Complete the required inputs for this workflow step."
            if title
            else "Open a pending workflow step or review the available results."
        )
        self._refresh_required = True
        self._clear_detail()
        self._set_empty_controls()
        self._table.setEnabled(False)

    def set_page(
        self,
        page: BatchInteractionPage,
        *,
        expected_state_head_sha256: str,
    ) -> None:
        if type(page) is not BatchInteractionPage:
            raise TypeError("Reviews page requires a BatchInteractionPage")
        self._inventory_job_id = None
        awaiting_response = self._response_pending
        processing = getattr(self, "_processing_message", None)
        if (processing is not None and self._status.text() == processing
                and expected_state_head_sha256 != self._state_head_sha256):
            self._set_status_message("")
        previous_group_id = self._displayed_group_id
        previous_target = self._target.currentData() if self._count_navigation and previous_group_id else None
        if awaiting_response and previous_group_id is not None:
            self._response_group_id = previous_group_id
        self._page = page
        self._state_head_sha256 = expected_state_head_sha256
        self._groups = {group.group_id: group for group in page.groups}
        self._group_ids = tuple(group.group_id for group in page.groups)
        scoped = self._workflow_title is not None
        self._workspace.setCurrentWidget(
            self._detail_pane if scoped and page.groups else self._inbox_pane
        )
        self._directory_open.setVisible(not scoped)
        self._directory_results.setVisible(not scoped)
        self._directory_open.setEnabled(bool(page.groups) and not awaiting_response)
        self._review_group.blockSignals(True)
        self._review_group.clear()
        self._count_navigation = scoped and bool(page.groups) and all(
            group.interaction_reason == "primary_structure_count_review_required" for group in page.groups
        )
        if scoped:
            self.findChild(QLabel, "reviews-title").setText(
                "Review region counts" if self._count_navigation else self._workflow_title
            )
        if self._count_navigation:
            from .primary_count_review import pending_count_choices
            if page.offset:
                self._review_group.addItem("Previous reviews…", "previous-page")
            for label, group_id, target_id in pending_count_choices(page.groups):
                self._review_group.addItem(label, group_id)
                self._review_group.setItemData(self._review_group.count() - 1, target_id, Qt.ItemDataRole.UserRole + 1)
            if page.next_offset is not None:
                self._review_group.addItem("More reviews…", "next-page")
        else:
            for group in page.groups:
                self._review_group.addItem(self._inbox_summary(group).replace("\n", " · "), group.group_id)
        self._review_group.setAccessibleName("Analysis regions above a count limit" if self._count_navigation else "Review in this step")
        self._review_group.blockSignals(False)
        self._count_regions.setVisible(self._count_navigation)
        if self._count_navigation:
            self._count_regions.set_entries(page.groups)
        self._review_group.setVisible(scoped and not self._count_navigation and self._review_group.count() > 1)
        self._all_reviews.setVisible(scoped)
        empty_message = (
            "This step has no remaining forms. Return to the workflow to continue."
            if self._workflow_title is not None
            else "No reviews are waiting on this page."
        )
        self._empty.setText("" if page.groups else empty_message)
        self._empty.setVisible(not page.groups)
        self._table.setVisible(not scoped and bool(page.groups))
        self._form_scroll.setVisible(scoped and bool(page.groups))
        if awaiting_response:
            self._response_page_received = True
        else:
            self._set_status_message("")
        table = FormTable(
            "batch_interaction_groups",
            ("Review",),
            tuple(
                FormTableRow(
                    group.group_id,
                    (self._inbox_summary(group),),
                )
                for group in page.groups
            ),
        )
        prior = self._list_model
        self._list_model = FormTableModel(table, self._table)
        self._table.setModel(self._list_model)
        self._table.setAccessibleDescription(
            "\n".join(self._inbox_summary(group) for group in page.groups)
        )
        self._table.selectionModel().currentChanged.connect(self._select_index)
        self._size_inbox_rows()
        if prior is not None:
            prior.deleteLater()
        self._clear_detail()
        if awaiting_response:
            self._table.setEnabled(False)
            self._previous.setEnabled(False)
            self._next.setEnabled(False)
            self._complete_response_refresh_if_ready()
        else:
            self._enable_current_page(
                select_first=(previous_group_id is None or previous_group_id in self._groups),
                group_id=previous_group_id, target_id=previous_target,
            )

    @staticmethod
    def _target_summary(target: BatchInteractionTarget, number: int) -> str:
        parts = (
            f"Sample {target.sample_id}",
            f"Slide {target.slide_id}",
            f"Processing group {target.validation_domain_id}",
        )
        if target.project_entry_id is not None:
            parts += (f"Project entry {target.project_entry_id}",)
        return f"Review {number}: " + " · ".join(parts)

    @staticmethod
    def _inbox_summary(
        group: BatchInteractionGroup | BatchAggregateInteractionGroup,
    ) -> str:
        if type(group) is BatchAggregateInteractionGroup:
            summary = (
                f"{_review_title(group.interaction_reason)}\n"
                f"Processing groups: {', '.join(group.validation_domain_ids)}"
            )
            return summary
        samples = tuple(dict.fromkeys(target.sample_id for target in group.targets))
        scope = ", ".join(samples[:3])
        if len(samples) > 3:
            scope += f" · {len(samples)} samples"
        title = {
            "mif-batch-stage1-interaction/v1": "Confirm channels and markers",
            "mif-batch-reference-unmixing-interaction/v2": "Review the spectral unmixing inputs",
        }.get(group.context_version, _review_title(group.interaction_reason))
        return f"{title}\n{scope}"

    def _open_directory_step(self) -> None:
        if self._refresh_required or self._response_pending or self._submission_job_id is not None:
            return
        group = self._groups.get(self._displayed_group_id)
        if group is None:
            return
        nodes = (
            (group.target.node_id,)
            if type(group) is BatchAggregateInteractionGroup
            else tuple(sorted({target.node_id for target in group.targets}))
        )
        self.workflow_review_requested.emit(nodes)

    def _select_stage_group(self, _index: int = 0) -> None:
        group_id = self._review_group.currentData()
        if group_id == "previous-page":
            self._request_previous()
        elif group_id == "next-page":
            self._request_next()
        elif group_id in self._groups:
            self.show_group(group_id, target_id=self._review_group.currentData(Qt.ItemDataRole.UserRole + 1))

    def _group_detail(
        self,
        group: BatchInteractionGroup | BatchAggregateInteractionGroup,
    ) -> str:
        lines = [
            f"Group: {group.group_id}",
            f"Group SHA-256: {group.group_sha256}",
            f"State head SHA-256: {self._state_head_sha256}",
            f"Stage: {group.stage}",
            f"Reason: {group.interaction_reason}",
            f"Action schema: {group.action_schema_version}",
            f"Context schema: {group.context_version}",
        ]
        if type(group) is BatchAggregateInteractionGroup:
            lines.extend(
                (
                    f"Aggregate target: {group.target.target_id}",
                    f"Processing groups: {', '.join(group.validation_domain_ids)}",
                )
            )
        else:
            lines.append(f"Targets: {', '.join(target.target_id for target in group.targets)}")
            for target in group.targets:
                identity = (
                    f"{target.target_id}: sample {target.sample_id}; "
                    f"slide {target.slide_id}; processing group {target.validation_domain_id}"
                )
                if target.project_entry_id is not None:
                    identity += f"; project entry {target.project_entry_id}"
                lines.append(identity)
        return "\n".join(lines)

    def _enable_current_page(
        self,
        *,
        select_first: bool = True,
        group_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        page = self._page
        if page is None:
            return
        self._refresh_required = False
        self._table.setEnabled(True)
        self._previous.setEnabled(page.offset > 0)
        self._next.setEnabled(page.next_offset is not None)
        show_paging = page.offset > 0 or page.next_offset is not None
        self._previous.setVisible(show_paging)
        self._next.setVisible(show_paging)
        if self._count_navigation:
            node_id = self._preferred_count_node
            for group in page.groups:
                for target in group.targets:
                    if target.node_id == node_id or (node_id is None and target.target_id == target_id):
                        self._preferred_count_node = None
                        self.show_group(group.group_id, target_id=target.target_id)
                        return
            if node_id and page.next_offset is not None:
                self._request_next()
                return
            self._preferred_count_node = None
        if page.groups and (select_first or self._count_navigation):
            row = self._group_ids.index(group_id) if group_id in self._groups else 0
            self._table.setCurrentIndex(self._list_model.index(row, 0))

    def show_group(self, group_id: str, *, target_id: str | None = None) -> None:
        if self._refresh_required:
            return
        group = self._groups.get(group_id)
        if group is None or self._state_head_sha256 is None:
            raise ValueError("selected interaction group is not on the current page")
        self._inventory_job_id = None
        self._invalidate_tissue_source()
        self._clear_detail()
        self._umap.hide()
        self._displayed_group_id = group_id
        self._directory_open.setEnabled(True)
        if self._workflow_title is None:
            return
        self._review_group.blockSignals(True)
        index = self._review_group.findData(group_id)
        if self._count_navigation:
            target_id = target_id or self._review_group.itemData(index, Qt.ItemDataRole.UserRole + 1)
            index = self._review_group.findData(target_id, Qt.ItemDataRole.UserRole + 1)
            self._count_regions.focus_target(target_id)
        self._review_group.setCurrentIndex(index)
        self._review_group.blockSignals(False)
        self._detail.setText(self._group_detail(group))
        if type(group) is BatchAggregateInteractionGroup:
            view = build_aggregate_review_form(
                group,
                expected_state_head_sha256=self._state_head_sha256,
            )
            self._current_view = view
            self._set_rendered_form(view.form)
            self._restore_draft()
            self._renderer.setVisible(True)
            for widget in (self._open_qupath, self._qupath_save_help, self._plots,
                           self._plot_selection, self._open_plot, self._export_plot_button):
                widget.hide()
            self._umap.configure(group)
            self._renderer._cluster_review.attach_umap(self._umap)
            self._form_layout.insertWidget(1, self._plot_status)
            identity = self.current_review_plot_identity()
            self.cluster_model_requested.emit(identity[0], identity[1])
            return
        if group.context_version == "secondary-structure-selection/v1":
            self._detail.setText(self._group_detail(group) + "\nLoading available regions…")
            state_head = self._state_head_sha256
            job_id = self._jobs.submit(
                "project_secondary_inventory",
                lambda: build_batch_interaction_form(
                    group,
                    expected_state_head_sha256=state_head,
                ),
                access=JobAccess.READ,
            )
            self._inventory_job_id = job_id
            return
        view = build_batch_interaction_form(
            group,
            expected_state_head_sha256=self._state_head_sha256,
            segmentation_model_directory=self._segmentation_model_directory,
            target_id=target_id,
            reference_option=self._last_reference_option.get(
                (group.group_sha256, group.targets[0].target_id)
            ),
        )
        self._render_view(view)

    def _render_view(
        self,
        view: BatchInteractionForm | UnsupportedBatchInteraction,
    ) -> None:
        origin = self._reference_preparation_origin
        if (origin is not None and (type(view) is not BatchInteractionForm
                or view.expected_state_head_sha256 != origin.expected_state_head_sha256)):
            self._reference_preparation_read = None
            self._reference_preparation_revisit = None
            self._reference_preparation_origin = None
            self._reference_preparation_history.clear()
        if self.remote_execution and type(view) is BatchInteractionForm:
            view = replace(view, form=replace(view.form,
                context={**view.form.context, "kronos_setup": self.remote_kronos_setup}))
        if (
            self.remote_execution
            and type(view) is BatchInteractionForm
            and view.form.context.get("adapter") == "segmentation_setup"
        ):
            view = replace(
                view,
                form=replace(
                    view.form,
                    context={
                        **view.form.context,
                        **self.remote_segmentation_settings,
                        "remote_execution": True,
                    },
                ),
            )
        if (
            self.remote_execution
            and type(view) is BatchInteractionForm
            and view.form.context.get("adapter") == "alignment_setup"
        ):
            from mif_pipeline.gui.alignment_setup import build_alignment_form
            from mif_pipeline.pipeline_core.stage_result import StageResult

            view = replace(view, form=build_alignment_form(
                StageResult.from_mapping(view.form.context["stage_result"]), remote_execution=True))
            controls = tuple(
                (
                    replace(
                        control,
                        choices=("automatic", "manual"),
                        value="automatic",
                        help_text="Automatic registration fits affine and optional B-spline alignment on the cluster. Manual registration uses paired landmarks in local QuPath, with global alignment before local refinement.",
                    )
                    if control.name == "alignment_method"
                    else control
                )
                for control in view.form.controls
            )
            context = dict(view.form.context)
            labels = {key: dict(value) for key, value in context.get("choice_labels", {}).items()}
            labels["alignment_method"] = {
                "automatic": "Automatic registration",
                "manual": "Manual registration",
            }
            context["choice_labels"] = labels
            view = replace(
                view,
                form=replace(
                    view.form,
                    controls=controls,
                    context=context,
                    message="Choose manual or automatic registration. Both routes establish global affine alignment before the selected local refinement.",
                ),
            )
        self._current_view = view
        self._tissue_settings_back.setVisible(
            type(view) is BatchInteractionForm and view.form.context.get("reason")
            in {"tissue_native_level_required", "tissue_section_review_required"})
        if type(view) is UnsupportedBatchInteraction:
            self._detail.show()
            self._detail.setAccessibleName("Unsupported interaction")
            self._detail.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            summary = (
                "The app cannot display this review with the current app version. "
                "Keep the analysis folder unchanged and reopen it after the app is updated."
            )
            self._detail.setText(
                self._detail.text() + "\n" + diagnostic_details(view.message, summary)
            )
            self._detail.setFocus()
            return
        assert type(view) is BatchInteractionForm
        group = self._groups[view.group_id]
        self._detail.setText(self._group_detail(group))
        selected_target = self._target.currentData()
        self._target.blockSignals(True)
        self._target.clear()
        for index, target_id in enumerate(view.target_ids, 1):
            target = next(item for item in group.targets if item.target_id == target_id)
            duplicate_sample = sum(item.sample_id == target.sample_id for item in group.targets) > 1
            label = (
                (f"{target.sample_id} · image {index}" if duplicate_sample else target.sample_id)
                if self._workflow_title is not None
                else self._target_summary(target, index)
            )
            self._target.addItem(label, target_id)
        retained_target = (
            selected_target
            if not self._count_navigation and type(selected_target) is str and selected_target in view.target_ids
            else view.representative_target_id
        )
        self._target.setCurrentIndex(self._target.findData(retained_target))
        self._target.blockSignals(False)
        show_target = self._workflow_title is None or (len(view.target_ids) > 1 and not self._count_navigation)
        self._target_label.setVisible(show_target)
        self._target.setVisible(show_target)
        self._set_rendered_form(view.form)
        self._restore_draft()
        if view.form.context.get("reference_source_overview"):
            key = self._reference_source_key(view)
            selected = {**self._reference_source_selections.get(key, {}),
                        **self._reference_source_additions.get(key, {})}
            valid = {control.name: selected[control.name] for control in view.form.controls
                     if selected.get(control.name) in control.choices}
            self._renderer.restore_values(valid)
            additions = self._reference_source_additions.get(key, {})
            for name in tuple(additions):
                if valid.get(name) == additions[name]:
                    additions.pop(name)
        if view.form.context.get("adapter") == "tissue_setup" and self._tissue_refresh_values:
            target_id, domain_id, values = self._tissue_refresh_values
            if (
                target_id == view.representative_target_id
                and domain_id == view.form.context["seed"]["validation_domain_id"]
            ):
                self._renderer.restore_tissue_values(values)
            self._tissue_refresh_values = None
        if view.form.context.get("adapter") == "tissue_setup":
            restored = self._renderer.raw_values()
            if restored.get("detector_source") == "import" and restored.get("source_path"):
                self._observe_tissue_source(restored["source_path"])
        self._renderer.setVisible(True)
        if self._marker_panel is not None:
            self._form_layout.removeWidget(self._marker_panel)
            self._marker_panel.setParent(None)
            self._marker_panel.deleteLater()
            self._marker_panel = None
        if view.form.context.get("adapter") == "marker_gates":
            self._marker_panel = MarkerGatePanel(view.form, self._renderer, self._form_host)
            self._renderer._marker_review_panel_widget = self._marker_panel
            self._form_layout.insertWidget(0, self._marker_panel)
            visit = getattr(self._renderer, '_marker_preparation_visit', None)
            if visit is not None:
                self._marker_panel.hide()
                visit.hidden.append(self._marker_panel)
            self._marker_panel.image_requested.connect(self._request_region_image)
            self._marker_panel.field_requested.connect(self._request_marker_field)
            self.setTabOrder(self._target, self._marker_panel.marker)
        if view.form.context.get("adapter") == "batch_reference_operation":
            selected = view.form.context.get("selected_option")
            if selected is not None and not view.form.context.get('reference_preparation_revisit'):
                if view.form.context.get("reference_source_step"):
                    offered = next(control.choices for control in view.form.controls if control.name == "option_value")
                    selected = next((value for value in (
                        "accept_reference_control_files_with_source_preparation", "accept_reference_control_files")
                        if value in offered), selected)
                self._last_reference_option[(view.group_sha256, view.representative_target_id)] = (
                    selected
                )
            option = self._renderer.findChild(QComboBox, "control-option_value")
            assert option is not None
            option.currentIndexChanged.connect(self._reference_option_changed)
            from .reference_choices import SpectralSourceChoices

            choices = self._renderer.findChild(SpectralSourceChoices)
            if choices is not None:
                choices.choice_activated.connect(self._reference_source_activated)
            from .reference_sources import ReferenceSourceOverview
            sources = self._renderer.findChild(ReferenceSourceOverview)
            if sources is not None:
                sources.source_requested.connect(
                    lambda option, locator, origin=self._renderer:
                    self._reference_source_file_requested(option, locator, origin))
                sources.refresh()
        final_af = self._renderer._target_af_groups_panel
        if final_af is not None:
            final_af.library_export_requested.connect(self._choose_library_export)
        self._set_inventory_form(view.form)
        self._set_artifacts(view.form)
        from .training_fit_summary import TrainingFitSummary
        from .region_review import RegionDecisionReview
        region_review = self._renderer.findChild(RegionDecisionReview)
        if region_review is not None:
            region_review.set_appearance_store(getattr(self, 'image_appearance_path', None))
            region_review.image_requested.connect(self._request_region_image)
            region_review.appearance_requested.connect(self._open_region_appearance)
        self._open_plot.setVisible(
            self._renderer.findChild(TrainingFitSummary) is None
            and region_review is None
            and (self._workflow_title is None or bool(self._plot_artifacts)))
        self._set_plot_status("")
        identity = self.current_review_plot_identity()
        if identity is not None and self._marker_panel is None and view.form.context.get('adapter') != 'marker_combinations':
            request_id, head, _target_id, _context_sha256 = identity
            from .supervised_prediction_review import SupervisedPredictionReview
            prediction = self._renderer.findChild(SupervisedPredictionReview)
            if prediction is not None:
                self._set_plot_status("")
                prediction._request()
            else:
                self._set_plot_status("" if region_review is not None else "Preparing figures…")
                self.review_plots_requested.emit(request_id, head, self.review_plot_options())

        self._bind_saved_reference_choices()

        self._sync_reference_components(view.form)
        QTimer.singleShot(0, self, self._autoload_reference_regions)

    def _sync_reference_components(self, form):
        from .reference_components import preparation_progress
        # Inspection remains available while the mounted scientific form is incomplete.
        progress = preparation_progress(form, self._renderer.capture_draft_values())
        available = bool(progress and progress.get("components") and (
            progress.get("source_selection")
            or (progress.get("shared_control") or {}).get("current")
            or progress.get("current_component")
            or any(row["status"] == "accepted" for row in progress["components"])))
        self._mount_reference_components_link(self._renderer)
        self._reference_components_link.setVisible(available)
        if not available:
            self._reference_components_open = False
            self._reference_component_inspection = None
            self._reference_control_inspection = None
            return
        state = form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
        control = progress.get("control_source") or progress.get("shared_control") or {}
        entry = (state["run_id"], "source-images" if progress.get("source_selection") else
                 ("control-source", control["input_id"]) if control.get("current") else progress["current_component"])
        first_entry = entry not in self._reference_component_entries
        self._reference_component_entries.add(entry)
        self._reference_components.set_progress(progress)
        if form.context.get('reference_preparation_revisit'):
            return
        if first_entry or self._reference_components_open:
            self._open_reference_components()

    def _detach_reference_components_link(self):
        """Keep this reusable jump alive when a form or inspection is replaced."""
        if self._reference_components_layout is not None:
            self._reference_components_layout.removeWidget(self._reference_components_link)
            self._reference_components_layout = None
        self._reference_components_link.setParent(self)
        self._reference_components_link.hide()

    def _mount_reference_components_link(self, target):
        """Keep overview navigation at the lower left of each preparation page."""
        self._detach_reference_components_link()
        row = (getattr(self, '_reference_footer_overview_actions', None)
               if target is self._renderer else None)
        if row is None:
            # Read-only spectrum inspections have their own page body, outside
            # the live form. Give those pages the same lower-left return link.
            body = target.widget() if isinstance(target, QScrollArea) else target
            row = getattr(body, '_reference_overview_actions', None)
            if row is None:
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.addStretch(1)
                body.layout().addLayout(row)
                body._reference_overview_actions = row
        row.insertWidget(0, self._reference_components_link)
        self._reference_components_layout = row
        self._reference_components_link.show()

    def _open_reference_components(self):
        if self._reference_components_link.isHidden():
            return
        if type(self._current_view) is BatchInteractionForm:
            from .reference_components import preparation_progress
            progress = preparation_progress(self._current_view.form, self._renderer.capture_draft_values())
            if progress is not None:
                self._reference_components.set_progress(progress)
        self._reference_components_open = True
        self._reference_component_inspection = None
        self._reference_control_inspection = None
        self._reference_components_link.hide()
        self._workspace.setCurrentWidget(self._reference_components)
        self._sync_reference_review_heading()

    def _resume_reference_component(self, entry_page=None):
        # Resume the live task; alternate offered forms retain separate drafts.
        # An AF-set revisit is another offered form at the same checkpoint.
        # The stained row still returns to that checkpoint's primary task;
        # its own draft is restored by the existing option-change binding.
        view = self._current_view
        if type(view) is BatchInteractionForm and view.form.context.get('reference_preparation_revisit'):
            self._return_reference_preparation()
            return
        if (type(view) is BatchInteractionForm
                and view.form.context.get('selected_option') == 'review_control_sets'):
            editor = self._renderer._editors.get('option_value')
            if editor is not None and editor.count() and editor.itemData(0) != 'review_control_sets':
                editor.setCurrentIndex(0)
        regions = self._renderer._reference_regions_panel
        if entry_page in {"control", "stained"} and regions is not None:
            regions.show_page(entry_page)
        self._reference_components_open = False
        self._reference_component_inspection = None
        self._reference_control_inspection = None
        self._mount_reference_components_link(self._renderer)
        self._workspace.setCurrentWidget(self._detail_pane)
        self._sync_reference_review_heading()

    def _open_reference_source(self, field):
        """Open the original source chooser; its selectors remain the draft owners."""
        view = self._current_view
        if type(view) is not BatchInteractionForm or not view.form.context.get("reference_source_overview"):
            return
        fields = {row["control"] for group in view.form.context["reference_source_overview"]
                  for row in group["rows"]}
        if field not in fields:
            return
        self._resume_reference_component()
        selector = self._renderer._editors[field]
        self._form_scroll.ensureWidgetVisible(selector, 16, 24)
        selector.setFocus(Qt.FocusReason.OtherFocusReason)

    def _accept_reference_sources(self):
        """Submit only the mounted binding form, with its live reviewed-file choices."""
        view = self._current_view
        if (type(view) is not BatchInteractionForm or not view.form.context.get("reference_source_overview")
                or self._refresh_required or self._response_pending or self._jobs.has_active_jobs
                or self._submission_job_id is not None or not self._response_allowed()):
            return
        from .reference_components import preparation_progress
        progress = preparation_progress(view.form, self._renderer.capture_draft_values())
        if progress["sources_ready"] != progress["source_count"]:
            self._reference_components.set_progress(progress)
            return
        self._resume_reference_component()
        self._renderer._submit("accept")

    def _navigate_reference_preparation(self, option):
        """Use only revision routes offered by the still-current scientific request."""
        view = self._current_view
        if (type(view) is not BatchInteractionForm or self._refresh_required or self._response_pending
                or self._jobs.has_active_jobs or self._submission_job_id is not None
                or not self._response_allowed()):
            return
        from .reference_components import preparation_progress
        progress = preparation_progress(view.form, self._renderer.capture_draft_values())
        if progress is None or option not in progress.get("navigation_options", ()):
            return
        if option in {'retry_shared_control_preparation', 'revise_shared_control'}:
            controls = [progress.get('control_source') or progress.get('shared_control') or {}]
            controls.extend(row.get('control') or {} for row in progress['components'])
            source = next((row for row in controls if row.get('input_id') and
                (row.get('can_revise') if option == 'revise_shared_control' else
                 row.get('current') and row.get('proposal_available'))), None)
            if source is not None:
                self._open_reference_preparation(source['input_id'], 'control_regions')
                return
            self._set_status_message('Reopen the source preparation from its current overview.')
            return
        phase = {'reselect_reference_rois': 'regions', 'revise_signal_dark_measurements': 'bands'}.get(option)
        if (phase is not None
                and progress.get('current_component') in progress.get('revisitable_components', ())):
            self._open_reference_preparation(progress['current_component'], phase)
            return
        self._resume_reference_component()
        view = self._current_view
        if option == 'review_control_sets':
            # Selecting an offered form is local navigation. Only its explicit
            # Save applies regrouping and the service's dependent-work revision.
            editor = self._renderer._editors.get('option_value')
            if editor is not None:
                index = editor.findData(option)
                if index >= 0:
                    editor.setCurrentIndex(index)
        elif option in view.form.context.get("reference_navigation_options", ()):
            self._reference_back(option)
        elif option == "reselect_reference_rois":
            state = view.form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
            if state.get("review_summary", {}).get("saved_positive_regions"):
                self._open_saved_positive_regions()
            else:
                self._submit_reference_reselection(option)
        elif option in {"retry_shared_control_preparation", "revise_signal_dark_measurements"}:
            self._submit_reference_reselection(option)
        elif option == "revise_shared_control":
            # Keep the offered revision's impact visible even when the current
            # extraction checkpoint normally supplies its own review message.
            self._capture_draft()
            state = view.form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
            requirement = next(item for item in state["operation_requirements"] if item["option_value"] == option)
            updated = build_batch_interaction_form(self._groups[view.group_id],
                expected_state_head_sha256=view.expected_state_head_sha256,
                target_id=self._target.currentData(), reference_option=option)
            updated = replace(updated, form=replace(updated.form, title="Edit shared AF preparation",
                message=requirement["description"]))
            self._replace_renderer()
            self._render_view(updated)

    def _inspect_reference_component(self, component):
        identity = self._reference_review_identity()
        if identity is None or self._jobs.has_active_jobs or self._response_pending:
            return
        if not any(row["name"] == component and row["status"] == "accepted"
                   for row in self._reference_components.progress["components"]):
            return
        self._reference_component_inspection = (*identity, component)
        self._reference_control_inspection = None
        self._detach_reference_components_link()
        self._accepted_reference_spectrum.prepare(component)
        self._mount_reference_components_link(self._accepted_reference_spectrum)
        self._workspace.setCurrentWidget(self._accepted_reference_spectrum)
        self.reference_component_requested.emit(*identity, component)

    def _open_reference_preparation(self, component, phase, *, remember=True):
        """Inspect a completed phase; opening never changes workflow authority."""
        view = self._reference_preparation_origin or self._current_view
        identity = self._reference_review_identity()
        if (identity is None or type(view) is not BatchInteractionForm or self._refresh_required
                or self._response_pending or self._jobs.has_active_jobs or not self._response_allowed()):
            return
        from .reference_components import preparation_progress
        progress = preparation_progress(view.form, {})
        if not progress:
            return
        if phase == 'control_regions':
            controls = [progress.get('control_source') or progress.get('shared_control') or {}]
            controls.extend(row.get('control') or {} for row in progress['components'])
            source = next((row for row in controls if row.get('input_id') == component), None)
            option = ('retry_shared_control_preparation' if source and source.get('current')
                and source.get('proposal_available') else 'revise_shared_control')
            if not source or option not in progress.get('navigation_options', ()) or (
                    option == 'revise_shared_control' and not source.get('can_revise')):
                return
        elif (component not in progress.get('revisitable_components', ())
                or phase not in {'regions', 'assignments', 'bands', 'extraction', 'consensus'}):
            return
        self._capture_draft()
        if self._reference_preparation_origin is None:
            self._reference_preparation_return_overview = self._workspace.currentWidget() is self._reference_components
        elif remember and self._reference_preparation_revisit is not None:
            previous = self._reference_preparation_revisit['identity'][2:]
            if previous != (component, phase):
                self._reference_preparation_history.append(previous)
        self._reference_preparation_origin = view
        self._reference_preparation_read = (*identity, component, phase)
        self._set_status_message('Opening saved preparation…')
        self.reference_preparation_requested.emit(*identity, component, phase)

    def offer_reference_preparation(self, identity, result=None, error=None):
        if (self._reference_preparation_read != identity
                or self._reference_review_identity() != identity[:2]):
            return
        self._reference_preparation_read = None
        if error is not None:
            self._set_status_message(str(error))
            return
        origin = self._reference_preparation_origin
        state = origin.form.context['stage_result']['resume_context']['service_context']['operation_state']
        if (result.get('component') != identity[2] or result.get('phase') != identity[3]
                or result.get('checkpoint_token') != state['checkpoint_token']):
            self._set_status_message('The preparation changed. Reopen it from the overview.')
            return
        from mif_pipeline.gui.reference_preparation_revisit import component_preparation_form
        form = component_preparation_form(origin.form, result)
        self._reference_preparation_revisit = {'identity': identity, 'inspection': result}
        self._reference_components_open = False
        self._replace_renderer()
        self._render_view(replace(origin, form=form))
        self._workspace.setCurrentWidget(self._detail_pane)
        self._sync_reference_review_heading()
        self._set_status_message('')

    def _return_reference_preparation(self):
        origin = self._reference_preparation_origin
        if origin is None:
            return
        self._capture_draft()
        self._reference_preparation_read = None
        self._reference_preparation_revisit = None
        self._reference_preparation_origin = None
        self._reference_preparation_history.clear()
        self._reference_components_open = False
        self._replace_renderer()
        self._render_view(origin)
        self._workspace.setCurrentWidget(self._detail_pane)
        self._sync_reference_review_heading()

    def _back_reference_preparation(self):
        if self._reference_preparation_history:
            component, phase = self._reference_preparation_history.pop()
            self._open_reference_preparation(component, phase, remember=False)
        else:
            overview = self._reference_preparation_return_overview
            self._return_reference_preparation()
            if overview:
                self._open_reference_components()

    def _continue_reference_preparation(self):
        revisit = self._reference_preparation_revisit
        component, phase = revisit['identity'][2:]
        if phase == 'control_regions':
            self._return_reference_preparation()
            from .reference_components import preparation_progress
            progress = preparation_progress(self._current_view.form, {})
            if 'review_control_sets' in progress.get('navigation_options', ()):
                self._navigate_reference_preparation('review_control_sets')
            elif progress.get('current_checkpoint') == 'shared_control_preparation':
                self._resume_reference_component()
            else:
                self._inspect_shared_control(component)
            return
        phases = ('regions', 'assignments', 'bands', 'extraction', 'consensus')
        index = phases.index(phase)
        from .reference_components import preparation_progress
        progress = preparation_progress(self._reference_preparation_origin.form, {})
        row = next(item for item in progress['components'] if item['name'] == component)
        if index + 1 < len(phases):
            next_phase = phases[index + 1]
            current = {'positive_reference_rois': 'regions', 'af_reference_rois': 'assignments',
                'signal_dark_measurements': 'bands', 'extraction_qc': 'extraction',
                'fluorophore_consensus': 'consensus'}.get(progress.get('current_checkpoint'))
            if row['status'] == 'accepted' or current in phases and index + 1 < phases.index(current):
                self._open_reference_preparation(component, next_phase)
                return
            if row['status'] == 'current' and next_phase == current:
                self._return_reference_preparation()
                self._resume_reference_component()
                return
        self._return_reference_preparation()
        self._open_reference_components()

    def _reference_preparation_submission(self, raw_values):
        from mif_pipeline.gui.interactions import _json_editable_value
        from mif_pipeline.gui.reference_preparation_revisit import component_preparation_values
        from mif_pipeline.gui.presenters.reviews import _project_reference
        from mif_pipeline.pipeline_core import StageResult
        revisit = self._reference_preparation_revisit
        if revisit is None or self._reference_review_identity() != revisit['identity'][:2]:
            raise ValueError('The preparation changed. Reopen its overview before saving.')
        values = component_preparation_values(self._current_view.form, raw_values)
        baseline = _json_editable_value(revisit['inspection']['values'])
        comparable = lambda mapping: {key: value for key, value in mapping.items() if key != 'qc_policy'}
        if comparable(values) == comparable(baseline):
            self._continue_reference_preparation()
            return None
        origin = self._reference_preparation_origin
        if revisit['identity'][3] == 'control_regions':
            option = revisit['inspection']['revision_option']
            state = origin.form.context['stage_result']['resume_context']['service_context']['operation_state']
            if option not in state['offered_actions']:
                raise ValueError('The source preparation changed. Reopen its overview.')
            summary = self._current_view.form.context['reference_regions_summary']
            submitted = dict(raw_values)
            submitted['reference_region_selection'] = {**submitted['reference_region_selection'],
                'control_sets': values['control_sets']}
            self._saved_reference_pending = {'run_id': state['run_id'], 'checkpoint_token': state['checkpoint_token'],
                'control_input_id': revisit['identity'][2], 'values': submitted, 'summary': dict(summary),
                'option': option, 'loading': False, 'expected_revision': state['checkpoint_revision'] + 1}
            self._saved_reference_origin_view = self._current_view
            form = _project_reference(StageResult.from_mapping(origin.form.context['stage_result']), option_value=option)
            return replace(origin, form=form), {'option_value': option}
        from mif_pipeline.gui.reference_preparation_revisit import interrupted_preparation_draft_key
        key = interrupted_preparation_draft_key(origin.form)
        origin_key = (origin.group_sha256, origin.representative_target_id,
                      origin.form.context.get('selected_option'))
        if key is not None and origin_key in self._drafts:
            self._drafts[key] = self._drafts[origin_key]
        form = _project_reference(StageResult.from_mapping(origin.form.context['stage_result']),
            option_value='revise_component_preparation')
        return replace(origin, form=form), {'option_value': 'revise_component_preparation',
            'component': revisit['identity'][2], 'phase': revisit['identity'][3], 'values': values}

    def _inspect_shared_control(self, input_id):
        """Show this exact source, loading earlier accepted evidence on a read worker."""
        view = self._current_view
        identity = self._reference_review_identity()
        if (identity is None or type(view) is not BatchInteractionForm
                or self._jobs.has_active_jobs or self._response_pending or self._refresh_required):
            return
        from .reference_components import preparation_progress
        progress = preparation_progress(view.form, self._renderer.capture_draft_values())
        if not progress:
            return
        controls = [progress.get("shared_control") or {}]
        controls.extend(row.get("control") or {} for row in progress["components"])
        row = next((row for row in controls if row.get("input_id") == input_id and row.get("can_inspect")), None)
        if row is None:
            return
        state = view.form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
        summary = state["review_summary"]
        review = summary.get("control_preparation") or summary.get("shared_control_preparation") or {}
        expected = row.get("preparation_sha256")
        self._reference_component_inspection = None
        self._reference_control_inspection = None
        self._detach_reference_components_link()
        self._workspace.setCurrentWidget(self._accepted_shared_control)
        if (review.get("input_id") == input_id
                and (expected is None or review.get("preparation_sha256") == expected)):
            self._accepted_shared_control.show_preparation(review)
        elif expected:
            self._reference_control_inspection = (*identity, input_id, expected)
            self._accepted_shared_control.prepare(row)
            self.reference_control_requested.emit(*identity, input_id, expected)
        self._mount_reference_components_link(self._accepted_shared_control)

    def offer_reference_control(self, identity, result=None, error=None):
        if (self._reference_control_inspection != identity
                or self._reference_review_identity() != identity[:2]):
            return
        if error is not None:
            self._accepted_shared_control.status.setText(error)
            self._reference_control_inspection = None
        elif (result.get("input_id") == identity[2]
                and result.get("preparation_sha256") == identity[3]):
            self._detach_reference_components_link()
            self._accepted_shared_control.show_preparation(result)
            self._mount_reference_components_link(self._accepted_shared_control)
            self._reference_control_inspection = None

    def offer_reference_component(self, identity, result=None, error=None):
        if (self._reference_component_inspection != identity
                or self._reference_review_identity() != identity[:2]):
            return
        if error is not None:
            self._accepted_reference_spectrum.status.setText(error)
        elif result.get("component") == identity[2]:
            self._accepted_reference_spectrum.show_spectrum(result)

    def _request_prediction_page(self, options):
        """Forward a bounded score-page intent with the current review identity."""
        identity = self.current_review_plot_identity()
        if identity is not None:
            self.prediction_page_requested.emit(identity[0], identity[1], options)

    def _request_cutoff_assignment(self, selection):
        identity = self.current_review_plot_identity()
        if identity is not None and self._response_allowed() and not self._refresh_required:
            if getattr(self, 'scoped_cutoff_available', lambda: False)():
                self.cutoff_assignment_requested.emit(identity[0], identity[1], selection)
            else:
                self._renderer._submit('review_supervised_classifier')

    def prediction_panel(self):
        from .supervised_prediction_review import SupervisedPredictionReview
        return self._renderer.findChild(SupervisedPredictionReview)

    def review_plot_options(self):
        from .supervised_prediction_review import SupervisedPredictionReview
        prediction = self._renderer.findChild(SupervisedPredictionReview)
        if prediction is not None:
            try:
                return prediction.image_options()
            except ValueError:
                return None
        if self._marker_panel is not None:
            return self._marker_panel.image_options()
        if type(self._current_view) is BatchInteractionForm and self._current_view.form.context.get('adapter') == 'marker_combinations':
            return self._renderer._marker_combinations.feedback.image_options()
        if type(self._current_view) is AggregateReviewForm:
            panel=self._renderer._cluster_review
            return (panel.image_options() if panel is not None else None) or self._umap.options()
        from .region_review import RegionDecisionReview
        region = self._renderer.findChild(RegionDecisionReview)
        return region.image_options() if region is not None else None

    def kronos_cell_preview_context(self):
        view = self._current_view
        if type(view) is not BatchInteractionForm or not view.form.context.get('supervised'):
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next((item for item in group.targets if item.target_id == view.representative_target_id), None)
        return (target.interaction_request_id, view.expected_state_head_sha256) if target is not None else None

    def _request_kronos_cell_preview(self, settings, receiver):
        current = self.kronos_cell_preview_context()
        if current is not None:
            self.kronos_cell_preview_requested.emit(*current, settings, receiver)
        else:
            receiver.set_error('Reopen this setup request to inspect an example cell.')

    def secondary_marker_context(self):
        view = self._current_view
        if type(view) is not BatchInteractionForm or view.form.context.get('adapter') != 'secondary_definition':
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next((item for item in group.targets if item.target_id == view.representative_target_id), None)
        return (target.interaction_request_id, view.expected_state_head_sha256) if target is not None else None

    def _request_secondary_marker(self, kind, options, receiver):
        current = self.secondary_marker_context()
        if current is not None and kind in {'review', 'field'}:
            self.secondary_marker_requested.emit(*current, kind, options, receiver)

    def combination_preview_context(self):
        """Resolve the current request and validate its unsaved phenotype rules."""
        view = self._current_view
        if type(view) is not BatchInteractionForm or view.form.context.get('adapter') != 'marker_combinations':
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next((target for target in group.targets if target.target_id == view.representative_target_id), None)
        if target is None:
            return None
        from mif_pipeline.gui.marker_combinations import submit_marker_combination_form
        definition = submit_marker_combination_form(view.form, 'define_marker_composite_rules',
            self._renderer.raw_values())['phenotype_definition']
        return target.interaction_request_id, view.expected_state_head_sha256, definition, self._renderer._marker_combinations

    def _request_combination_preview(self):
        try:
            current = self.combination_preview_context()
        except (TypeError, ValueError):
            self._renderer._marker_combinations.feedback.set_incomplete()
            return
        if current is not None:
            current[3].set_preview_busy()
            self.marker_combination_preview_requested.emit(*current[:3], current[3].pattern_page_options())

    def combination_preview_finished(self, editor):
        # A discarded old draft must still release the one in-flight preview.
        if self._renderer._marker_combinations is editor:
            editor.feedback.request_finished()

    def _request_region_image(self):
        identity = self.current_review_plot_identity()
        if identity is not None:
            self.review_plots_requested.emit(identity[0], identity[1], self.review_plot_options())

    def _request_marker_field(self):
        identity = self.current_review_plot_identity()
        field = self.current_marker_field()
        if identity is not None and field is not None and not field.isHidden():
            options = field.options()
            if options is not None:
                self.marker_field_requested.emit(identity[0], identity[1], options)

    def current_marker_field(self):
        prediction = self.prediction_panel()
        if prediction is not None and not prediction.field.isHidden():
            return prediction.field
        cluster = self._renderer._cluster_review
        if cluster is not None and cluster.pages.currentWidget() is cluster.cell_page:
            return cluster.field
        editor = self._renderer._marker_combinations
        if editor is not None and editor.reviewing:
            return editor.review
        return self._marker_panel._field_review if self._marker_panel is not None else None

    def _request_supervised_cell_image(self):
        from .supervised_fit_review import SupervisedCellEvaluation
        panel = self._renderer.findChild(SupervisedCellEvaluation)
        identity = self.current_review_plot_identity()
        if panel is not None and identity is not None and panel.image_options() is not None:
            self.review_plots_requested.emit(identity[0], identity[1], panel.image_options())

    def _request_umap(self):
        identity = self.current_review_plot_identity()
        if identity is not None:
            panel=self._renderer._cluster_review
            if panel is not None:
                panel.cancel_image_request()
            self._set_plot_status("Preparing the UMAP view…")
            self.review_plots_requested.emit(identity[0], identity[1], self._umap.options())

    def _select_umap_cell(self,row):
        panel=self._renderer._cluster_review
        if type(self._current_view) is AggregateReviewForm and panel is not None:
            panel.select_embedding_cell(row)

    def _request_cluster_refit(self, selected_ids):
        view=self._current_view
        group=self._groups.get(view.group_id) if type(view) is AggregateReviewForm else None
        if type(group) is BatchAggregateInteractionGroup:
            self.cluster_refit_requested.emit(group.target.node_id,tuple(selected_ids))

    def current_review_plot_identity(self) -> tuple[str, str, str, str] | None:
        view = self._current_view
        if type(view) is AggregateReviewForm:
            group = self._groups.get(view.group_id)
            if type(group) is BatchAggregateInteractionGroup:
                return (group.target.interaction_request_id, view.expected_state_head_sha256,
                        group.target.target_id, group.target.interaction_context_sha256)
            return None
        if (
            type(view) is not BatchInteractionForm
            or not (
                view.form.context.get("adapter") in {"marker_gates", "marker_combinations"}
                or view.form.context.get("region_review") is not None
                or view.form.form_id == "training_fit_review"
                or (view.form.context.get("reason") == "supervised_classifier_review_required"
                    and view.form.context.get("resume_context", {}).get("phase") == "prediction_review")
            )
        ):
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next(
            (item for item in group.targets if item.target_id == view.representative_target_id),
            None,
        )
        if target is None:
            return None
        return (
            target.interaction_request_id,
            view.expected_state_head_sha256,
            target.target_id,
            target.interaction_context_sha256,
        )

    def _set_plot_status(self, message: str) -> None:
        self._plot_status.setText(message)
        self._plot_status.setVisible(bool(message))

    def set_review_plots(self, result: BatchReviewPlots) -> bool:
        identity = self.current_review_plot_identity()
        if (
            type(result) is not BatchReviewPlots
            or identity is None
            or identity
            != (
                result.interaction_request_id,
                result.state_head_sha256,
                result.target_id,
                result.interaction_context_sha256,
            )
        ):
            return False
        if result.plots and result.plots[0].artifact_id == 'region-cells':
            from .region_review import RegionDecisionReview
            region = self._renderer.findChild(RegionDecisionReview)
            return region.set_cell_artifact(result.plots[0]) if region is not None else False
        if result.plots and 'prediction_review' in (result.plots[0].display_options or {}):
            from .supervised_prediction_review import SupervisedPredictionReview
            panel = self._renderer.findChild(SupervisedPredictionReview)
            if panel is not None:
                panel.set_plots(result.plots)
            return True
        if result.plots and 'cluster_request' in (result.plots[0].display_options or {}):
            panel=self._renderer._cluster_review
            return panel.set_image_plots(result.plots) if panel is not None else False
        if result.plots and result.plots[0].artifact_id.startswith('supervised-cell-gallery/'):
            from .supervised_fit_review import SupervisedCellEvaluation
            panel = self._renderer.findChild(SupervisedCellEvaluation)
            if panel is not None:
                panel.set_image_plots(result.plots)
            return True
        if self._marker_panel is not None:
            self._marker_panel.set_image_plots(result.plots)
            return True
        if type(self._current_view) is BatchInteractionForm and self._current_view.form.context.get('adapter') == 'marker_combinations':
            self._renderer._marker_combinations.feedback.set_image_plots(result.plots)
            return True
        view = self._current_view
        assert type(view) in (BatchInteractionForm, AggregateReviewForm)
        if type(view) is AggregateReviewForm:
            try:
                if not self._umap.set_plots(result.plots):
                    return False
            except (ValueError,OSError) as error:
                self._set_plot_status(str(error))
                return False
            if self._renderer._cluster_review is not None:
                self._renderer._cluster_review._highlight_selection()
        form = replace(
            view.form,
            plot_artifacts=tuple(
                PlotArtifact(plot.artifact_id, plot.title, str(plot.path), plot.sha256, plot.status)
                for plot in result.plots
            ),
        )
        self._current_view = replace(view, form=form)
        self._set_artifacts(form)
        if type(view) is AggregateReviewForm:
            self._open_qupath.hide()
            self._qupath_save_help.hide()
            self._plot_selection.setCurrentIndex(1)
            self._plots.hide()
            self._plot_selection.hide()
            self._open_plot.hide()
            self._export_plot_button.hide()
        self._set_plot_status("")
        return True

    def set_review_plots_failure(self, request_id: str, head: str, message: str = "") -> bool:
        identity = self.current_review_plot_identity()
        if identity is None or identity[:2] != (request_id, head):
            return False
        if type(self._current_view) is AggregateReviewForm and self._renderer._cluster_review is not None:
            panel=self._renderer._cluster_review
            if panel.image_options() is not None:
                panel.set_image_error(message or 'Could not load these cluster cells. Select Inspect cells to retry.')
                return True
        from .supervised_prediction_review import SupervisedPredictionReview
        prediction = self._renderer.findChild(SupervisedPredictionReview)
        if prediction is not None:
            prediction.set_error(message or 'Could not load recorded predictions. Select Refresh review to retry.')
            return True
        from .supervised_fit_review import SupervisedCellEvaluation
        panel = self._renderer.findChild(SupervisedCellEvaluation)
        if panel is not None and panel.image_options() is not None:
            panel.set_image_error(message or 'Could not load the evaluated cell image. Select Refresh cells to retry.')
            return True
        if self._marker_panel is not None:
            self._marker_panel.set_image_error(message or 'Could not load cell images. Select Refresh cells to retry.')
            return True
        if type(self._current_view) is BatchInteractionForm and self._current_view.form.context.get('adapter') == 'marker_combinations':
            self._renderer._marker_combinations.feedback.set_image_error(message or 'Could not load cell images. Select Refresh cells to retry.')
            return True
        from .region_review import RegionDecisionReview
        region = self._renderer.findChild(RegionDecisionReview)
        if region is not None:
            region.set_image_error(message or 'Could not load the image overview. Reopen this review to try again, or inspect the outlines in QuPath.')
            self._set_plot_status("")
            return True
        self._set_plot_status(message or (
            "The view could not be calculated. You can still review cluster labels; select Calculate UMAP view to retry."
            if type(self._current_view) is AggregateReviewForm else
            "Could not prepare figures. You can still review the saved results; select this review again to retry."
        ))
        return True

    def _target_changed(self, _index: int) -> None:
        view = self._current_view
        target_id = self._target.currentData()
        if type(view) is BatchInteractionForm:
            self._set_artifacts(view.form)
        if (
            type(view) is not BatchInteractionForm
            or type(target_id) is not str
            or target_id == view.representative_target_id
            or view.form.context.get("adapter")
            not in {
                "batch_stage1",
                "batch_reference_start",
                "batch_reference_operation",
                "marker_gates",
                "tissue_setup",
                "segmentation_setup",
                "measurement_setup",
                "training_fit_review",
                "roi",
            }
        ):
            return
        group = self._groups[view.group_id]
        self._invalidate_tissue_source()
        updated = build_batch_interaction_form(
            group,
            expected_state_head_sha256=view.expected_state_head_sha256,
            target_id=target_id,
            segmentation_model_directory=self._segmentation_model_directory,
        )
        self._replace_renderer()
        self._render_view(updated)

    def _reference_option_changed(self, _index: int) -> None:
        view = self._current_view
        source = self.sender()
        if (
            type(view) is not BatchInteractionForm
            or view.form.context.get("adapter") != "batch_reference_operation"
            or not isinstance(source, QComboBox)
        ):
            return
        option_value = source.currentData()
        if option_value == view.form.context.get("selected_option"):
            return
        self._capture_draft()
        group = self._groups[view.group_id]
        updated = build_batch_interaction_form(
            group,
            expected_state_head_sha256=view.expected_state_head_sha256,
            target_id=self._target.currentData(),
            reference_option=option_value,
        )
        self._replace_renderer()
        self._render_view(updated)

    def _reference_source_activated(self, option_value: str) -> None:
        """Apply the field-free MOTiF choice only after an explicit source click."""
        view = self._current_view
        if (option_value == "phenoimager_motif_profile"
                and type(view) is BatchInteractionForm
                and view.form.context.get("selected_option") == option_value
                and tuple(control.name for control in view.form.controls) == ("option_value",)
                and self._submission_job_id is None and not self._response_pending
                and self._response_allowed()):
            self._renderer._submit("accept")

    def _reference_source_file_requested(self, option_value: str, locator: str, origin) -> None:
        """Register a row's file through its exact offered role, then review metadata."""
        from uuid import uuid4

        view = self._current_view
        if (origin is not self._renderer or type(view) is not BatchInteractionForm
                or not view.form.context.get("reference_source_overview")
                or self._submission_job_id is not None or self._refresh_required
                or not self._response_allowed()):
            return
        offered = {row["option"] for group in view.form.context["reference_source_overview"]
                   for row in group["rows"]}
        if option_value not in offered or not locator:
            return
        self._capture_draft()
        input_id = "reference-" + uuid4().hex
        row = next(row for group in view.form.context["reference_source_overview"]
                   for row in group["rows"] if row["option"] == option_value)
        self._reference_source_additions.setdefault(self._reference_source_key(view), {})[row["control"]] = input_id
        updated = build_batch_interaction_form(self._groups[view.group_id],
            expected_state_head_sha256=view.expected_state_head_sha256,
            target_id=self._target.currentData(), reference_option=option_value)
        self._replace_renderer()
        self._render_view(updated)
        self._renderer.restore_values({"locator": locator, "input_id": input_id})
        self._renderer._submit("accept")

    @staticmethod
    def _reference_source_key(view):
        state = view.form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
        # Published target IDs change after a response; the source inventory
        # belongs to the same scientific reference run across those refreshes.
        return (state.get("run_id") or view.representative_target_id,)

    def _reference_back(self, option_value: str) -> None:
        """Open accepted choices locally; only changed Save submits reselection."""
        if option_value == 'reference_preparation_revisit':
            self._back_reference_preparation()
            return
        view = self._current_view
        if (type(view) is not BatchInteractionForm
                or (view.form.context.get("back_option") != option_value
                    and option_value not in view.form.context.get("reference_navigation_options", ()))
                or not self._response_allowed()):
            return
        if option_value in {'retry_shared_control_preparation', 'revise_shared_control'}:
            self._navigate_reference_preparation(option_value)
            return
        phase = {'reselect_reference_rois': 'regions', 'revise_signal_dark_measurements': 'bands'}.get(option_value)
        if phase is not None:
            from .reference_components import preparation_progress
            progress = preparation_progress(view.form, {})
            if progress and progress.get('current_component') in progress.get('revisitable_components', ()):
                self._open_reference_preparation(progress['current_component'], phase)
                return
        if option_value == "reselect_reference_rois" and view.form.context.get("reference_regions_available"):
            self._open_saved_positive_regions()
            return
        if option_value == "reselect_basis_source":
            self._open_saved_reference("source")
            return
        self._submit_reference_reselection(option_value)

    def _submit_reference_reselection(self, option_value):
        view = self._current_view
        self._capture_draft()
        updated = build_batch_interaction_form(
            self._groups[view.group_id],
            expected_state_head_sha256=view.expected_state_head_sha256,
            target_id=self._target.currentData(), reference_option=option_value)
        self._replace_renderer()
        self._render_view(updated)
        self._renderer._submit("accept")

    def _open_saved_positive_regions(self):
        self._open_saved_reference("regions")

    def _open_saved_reference(self, kind):
        from .reference_saved_regions import SavedReferenceRegionsEditor
        from .reference_saved_source import SavedSpectralSourceEditor
        view = self._current_view
        state = view.form.context["stage_result"]["resume_context"]["service_context"]["operation_state"]
        summary = state.get("review_summary", {})
        if kind == "source":
            baseline = view.form.context.get("saved_spectral_source")
            editor_type = SavedSpectralSourceEditor
        else:
            baseline = summary.get("saved_positive_regions")
            if baseline is None:
                baseline = (view.form.context.get("reference_regions_summary") or {}).get("saved_positive_regions")
            editor_type = SavedReferenceRegionsEditor
        if baseline is None:
            self._set_status_message("The saved spectral source could not be read. The current spectral review remains available." if kind == "source"
                                     else "Load the saved regions first, then reopen their positive-region choices.")
            return
        origin = (self._reference_review_identity(), state["run_id"], state["checkpoint_token"], state["fluorophore"])
        self._capture_draft()
        if self._saved_reference_origin != origin or self._saved_reference_editor is None:
            if self._saved_reference_editor is not None:
                self._saved_reference_editor.deleteLater()
            self._saved_reference_editor = editor_type(baseline, self._workspace)
            self._workspace.addWidget(self._saved_reference_editor)
            self._saved_reference_editor.returned.connect(self._return_saved_reference)
            self._saved_reference_editor.changed.connect(self._save_reference_choices)
            self._saved_reference_origin = origin
            self._saved_reference_origin_view = view
            self._saved_reference_kind = kind
        self._saved_reference_return_widget = self._workspace.currentWidget()
        self._workspace.setCurrentWidget(self._saved_reference_editor)

    def _return_saved_reference(self):
        self._workspace.setCurrentWidget(self._saved_reference_return_widget)

    def _save_reference_choices(self, values, edit=False):
        editor = self._saved_reference_editor
        origin = self._saved_reference_origin
        if (origin is None or self._reference_review_identity() != origin[0]
                or not self._response_allowed() or self._jobs.has_active_jobs
                or self._response_pending):
            editor.error.setText("The review changed or another operation is running. Your entries are retained; reopen the current review before saving.")
            return
        source = self._saved_reference_kind == "source"
        option = "reselect_basis_source" if source else "reselect_reference_rois"
        self._saved_reference_pending = {"run_id": origin[1], "checkpoint_token": origin[2],
            "fluorophore": origin[3], "values": values, "edit": edit, "option": option,
            "export_sha256": editor.baseline.get("export_sha256"), "loading": False}
        self._return_saved_reference()
        try:
            self._submit_reference_reselection(option)
        except (RuntimeError, ValueError) as error:
            self._restore_saved_reference_after_failure(str(error))

    def _restore_saved_reference_after_failure(self, message):
        pending = self._saved_reference_pending
        if pending is None or pending["loading"]:
            return
        self._saved_reference_pending = None
        if pending.get('control_input_id'):
            self._set_status_message(message)
            return
        # Only the still-current original review may be rendered again. The
        # editor itself retains the draft even if an external change made it stale.
        original = self._saved_reference_origin_view
        if original.expected_state_head_sha256 == self._state_head_sha256:
            self._replace_renderer()
            self._render_view(original)
        if self._saved_reference_editor is not None:
            self._saved_reference_editor.error.setText(message)
            self._workspace.setCurrentWidget(self._saved_reference_editor)
        else:
            self._set_status_message(message)

    def _bind_saved_reference_choices(self):
        pending = self._saved_reference_pending
        view = self._current_view
        if pending is None or type(view) is not BatchInteractionForm:
            return
        state = view.form.context.get("stage_result", {}).get("resume_context", {}).get("service_context", {}).get("operation_state", {})
        if pending.get('control_input_id'):
            self._bind_control_region_choices(pending, view, state)
            return
        if (state.get("run_id") != pending["run_id"] or state.get("fluorophore") != pending["fluorophore"]):
            self._saved_reference_pending = None
            return
        if state.get("checkpoint_token") == pending["checkpoint_token"]:
            return
        source = pending["option"] == "reselect_basis_source"
        if state.get("checkpoint") != ("basis_source" if source else "positive_reference_rois"):
            self._saved_reference_pending = None
            return
        if self._jobs.has_active_jobs or self._response_pending or not self._response_allowed():
            return
        if source:
            if not pending["loading"]:
                pending["loading"] = True
                updated = build_batch_interaction_form(
                    self._groups[view.group_id], expected_state_head_sha256=view.expected_state_head_sha256,
                    target_id=self._target.currentData(), reference_option=pending["values"]["option_value"])
                self._replace_renderer()
                self._render_view(updated)
                self._renderer.restore_values(pending["values"])
                self._capture_draft()
                identity = self._reference_review_identity()
                def submit_bound():
                    if self._saved_reference_pending is not pending:
                        return
                    self._saved_reference_pending = None
                    if (self._reference_review_identity() != identity or self._jobs.has_active_jobs
                            or self._response_pending or not self._response_allowed()):
                        self._set_status_message("Your spectral-source entries are retained. Review the current step before saving.")
                        return
                    self._renderer._submit("accept")
                QTimer.singleShot(0, self, submit_bound)
            return
        if pending.get("ready") == self.reference_region_identity():
            self._saved_reference_pending = None
            self._renderer._submit("accept")
            return
        if not pending["loading"]:
            pending["loading"] = True
            self._renderer.restore_values(pending["values"])
            identity = self.reference_region_identity()
            def request_bound():
                if self._saved_reference_pending is not pending:
                    return
                if (self.reference_region_identity() != identity or self._jobs.has_active_jobs
                        or not self._response_allowed()):
                    pending["loading"] = False
                    return
                self._request_reference_regions("edit" if pending["edit"] else "load")
            QTimer.singleShot(0, self, request_bound)

    def _bind_control_region_choices(self, pending, view, state):
        """Finish the explicitly requested source change on its exact new live page."""
        if pending['loading'] or state.get('checkpoint_token') == pending['checkpoint_token']:
            return
        progress = state.get('review_summary', {}).get('reference_preparation_progress', {})
        source = progress.get('control_source') or progress.get('shared_control') or next((
            row.get('control') for row in progress.get('components', ())
            if row.get('name') == progress.get('current_component')), {}) or {}
        if (state.get('run_id') != pending['run_id'] or state.get('checkpoint') != 'shared_control_preparation'
                or state.get('checkpoint_revision') != pending['expected_revision']
                or source.get('input_id') != pending['control_input_id']
                or view.form.context.get('selected_option') != 'prepare_shared_control'):
            self._saved_reference_pending = None
            self._set_status_message('The source review changed. Reopen its preparation before continuing.')
            return
        if self._jobs.has_active_jobs or self._response_pending or not self._response_allowed():
            return
        from mif_pipeline.gui.reference_regions import reference_region_form
        pending['loading'] = True
        summary = {**pending['summary'], 'checkpoint_token': state['checkpoint_token']}
        updated = replace(view, form=reference_region_form(view.form, summary))
        self._replace_renderer()
        self._render_view(updated)
        self._renderer.restore_values(pending['values'])
        self._capture_draft()
        self._reference_components_open = False
        self._workspace.setCurrentWidget(self._detail_pane)
        identity = self._reference_review_identity()
        def submit_bound():
            if self._saved_reference_pending is not pending:
                return
            self._saved_reference_pending = None
            if (self._reference_review_identity() != identity or self._jobs.has_active_jobs
                    or self._response_pending or not self._response_allowed()):
                self._set_status_message('Your source choices are retained. Review this page before continuing.')
                return
            self._submit_raw_response('accept', pending['values'])
        QTimer.singleShot(0, self, submit_bound)

    def _draft_key(self):
        view = self._current_view
        if type(view) is AggregateReviewForm:
            return (view.group_sha256 + view.expected_state_head_sha256, view.target_id, None)
        if type(view) is not BatchInteractionForm:
            return None
        revisit = view.form.context.get('reference_preparation_revisit')
        if revisit:
            return (view.group_sha256, view.representative_target_id,
                    ('preparation', revisit['component'], revisit['phase']))
        return (
            view.group_sha256,
            view.representative_target_id,
            view.form.context.get("selected_option"),
        )

    def _capture_draft(self):
        key = self._draft_key()
        if key is not None and self._renderer is not None:
            values = self._renderer.capture_draft_values()
            if type(self._current_view) is BatchInteractionForm and self._current_view.form.context.get("reference_source_overview"):
                source_key = self._reference_source_key(self._current_view)
                self._reference_source_selections[source_key] = {
                    name: value for name, value in values.items()
                    if name.startswith("reference_file_") and value is not None}
            # The changed selector already contains the next option; retain the
            # old form's exact choice with its own values instead.
            if "option_value" in values:
                values["option_value"] = (self._current_view.form.context.get('selected_option')
                    if self._current_view.form.context.get('reference_preparation_revisit') else key[2])
            self._drafts[key] = values
            while len(self._drafts) > 128:
                self._drafts.pop(next(iter(self._drafts)))

    def _restore_draft(self):
        values = self._drafts.get(self._draft_key())
        if not values and type(self._current_view) is BatchInteractionForm:
            from .marker_preparation import interrupted_marker_draft_key
            key = interrupted_marker_draft_key(self._current_view.form)
            if key is not None:
                values = self._drafts.pop(key, None)
        if not values and type(self._current_view) is BatchInteractionForm:
            from mif_pipeline.gui.reference_preparation_revisit import interrupted_preparation_draft_key
            key = interrupted_preparation_draft_key(self._current_view.form)
            if key is not None:
                values = self._drafts.pop(key, None)
        if values is not None:
            self._renderer.restore_values(values)

    @property
    def selected_inventory_content_keys(self) -> tuple[str, ...]:
        self._capture_inventory_selection()
        return self._inventory_selected_keys

    def _set_inventory_form(self, form: FormModel) -> None:
        table = next(
            (item for item in form.tables if item.table_id.startswith("inventory_page:")),
            None,
        )
        self._inventory_form = form if table is not None else None
        if table is None:
            self._inventory_panel.hide()
            self._inventory.setVisible(False)
            self._inventory_previous.setVisible(False)
            self._inventory_next.setVisible(False)
            return
        selected = next(
            (control.value for control in form.controls if control.name == "selected_content_keys"),
            (),
        )
        if not self._inventory_selected_keys:
            self._inventory_selected_keys = tuple(selected)
        offset = int(form.context["inventory_page_offset"])
        limit = int(form.context["inventory_page_limit"])
        count = self._inventory_record_count(form)
        # Row order remains the canonical inventory order. Display measurements
        # from the same authenticated projection as the linked outlines.
        regions = {row["region_id"]: row for row in form.context.get("region_review", {}).get("regions", ())}
        rows = []
        for index, row in enumerate(table.rows):
            region = regions.get(row.values[1], {})
            area = region.get("area_um2")
            cells = region.get("cell_count")
            rows.append(replace(row, values=(
                region.get("display_number", offset + index + 1),
                f"{area / 1_000_000:.4g}" if area is not None else "Unavailable",
                f"{cells:,}" if cells is not None else "—")))
        displayed = replace(table, columns=("Structure", "Area (mm²)", "Cells"), rows=tuple(rows))
        prior = self._inventory_model
        self._inventory_model = FormTableModel(displayed, self._inventory)
        self._inventory.setModel(self._inventory_model)
        if prior is not None:
            prior.deleteLater()
        self._inventory.setVisible(True)
        self._inventory_panel.show()
        self._inventory_prompt.setText(present_manual_structure_selection(form).message)
        self._inventory.setFixedHeight(
            self._inventory.horizontalHeader().height()
            + self._inventory.verticalHeader().defaultSectionSize() * max(1, min(8, len(table.rows)))
            + 12)
        self._inventory_previous.setVisible(count > limit)
        self._inventory_next.setVisible(count > limit)
        self._inventory_previous.setEnabled(offset > 0)
        self._inventory_next.setEnabled(offset + len(table.rows) < count)
        selected_keys = set(self._inventory_selected_keys)
        selection_model = self._inventory.selectionModel()
        for row, item in enumerate(table.rows):
            if item.values[0] in selected_keys and selection_model is not None:
                selection_model.select(
                    self._inventory_model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._update_inventory_selection_status)
        self._update_inventory_selection_status()

    def _update_inventory_selection_status(self, *_args) -> None:
        if self._inventory_form is None:
            return
        self._capture_inventory_selection()
        maximum = self._inventory_form.context["resume_context"]["maximum"]
        text = f"{len(self._inventory_selected_keys)} of {maximum} selected"
        if maximum > 1:
            text += " · Use Ctrl (⌘ on Mac) or Shift to select multiple rows."
        self._inventory_selection_status.setText(text)
        if not self._inventory_syncing:
            from .region_review import RegionDecisionReview
            region = self._renderer.findChild(RegionDecisionReview)
            if region is not None:
                table = next(table for table in self._inventory_form.tables
                             if table.table_id.startswith("inventory_page:"))
                ids = tuple(row.values[1] for row in table.rows
                            if row.values[0] in self._inventory_selected_keys)
                self._inventory_syncing = True
                try:
                    region.select_region_ids(ids)
                finally:
                    self._inventory_syncing = False

    def _select_inventory_regions(self, region_ids) -> None:
        """Use the same row selection for a clicked outline and the count response."""
        if self._inventory_syncing or self._inventory_form is None:
            return
        table = next(table for table in self._inventory_form.tables
                     if table.table_id.startswith("inventory_page:"))
        wanted = set(region_ids)
        selection = self._inventory.selectionModel()
        self._inventory_syncing = True
        try:
            selection.clearSelection()
            for index, row in enumerate(table.rows):
                if row.values[1] in wanted:
                    selection.select(self._inventory_model.index(index, 0),
                                     QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._inventory_syncing = False
        self._update_inventory_selection_status()

    @staticmethod
    def _inventory_record_count(form: FormModel) -> int:
        descriptor = next(
            table for table in form.tables if table.table_id == "inventory_descriptor"
        )
        value = descriptor.rows[0].values[-1]
        if type(value) is not int:
            raise ValueError("inventory descriptor count is invalid")
        return value

    def _capture_inventory_selection(self) -> None:
        form = self._inventory_form
        model = self._inventory_model
        selection = self._inventory.selectionModel()
        if form is None or model is None or selection is None:
            return
        table = next(item for item in form.tables if item.table_id.startswith("inventory_page:"))
        selected_rows = tuple(table.rows[index.row()].row_id for index in selection.selectedRows())
        self._inventory_selected_keys = selected_content_keys_from_inventory_page(
            form,
            selected_row_ids=selected_rows,
            current_content_keys=self._inventory_selected_keys,
        )

    def _set_artifacts(self, form: FormModel) -> None:
        if self.remote_execution:
            self.streamed_reference_context_changed.emit(
                form.context.get("adapter") in {"supervised_references", "training_plan_selection"})
        descriptor = self._current_reference_view()
        self._reference_guidance.clear()
        self._reference_guidance.addItem("No paired reference", "primary-only")
        self._reference_guidance.addItem("Side-by-side reference", "paired-reference-only")
        if descriptor is not None and descriptor.registration_mode == "warpy":
            self._reference_guidance.addItem("Registered overlay", "registered-overlay")
        self._reference_guidance.setCurrentIndex(0)
        has_reference = descriptor is not None and form.context.get("adapter") != "alignment_setup"
        self._reference_guidance_label.setVisible(has_reference)
        self._reference_guidance.setVisible(has_reference)
        self._reference_guidance_help.setVisible(has_reference)
        self._reference_guidance_changed()
        library_available = self._current_library_export_identity() is not None
        library_enabled = not self._refresh_required and not self._response_pending
        final_af = self._renderer._target_af_groups_panel
        if final_af is not None:
            final_af.configure_library_export(library_available, library_enabled)
        self._export_library.setVisible(library_available and final_af is None
            and not form.context.get('reference_preparation_revisit'))
        self._export_library.setEnabled(library_enabled)
        project_path = (
            descriptor.project_path
            if descriptor
            else (form.open_qupath_path or (self._page.qupath_project_path if self._page else None))
        )
        qupath = Path(project_path) if project_path else None
        if qupath is not None and not qupath.is_absolute():
            raise ValueError("QuPath project path must be absolute")
        self._qupath_project_path = qupath
        self._open_qupath.setEnabled(qupath is not None)
        options_decision = (
            form.context.get("reason") == "tissue_native_level_required"
            or form.context.get("adapter") in {"segmentation_setup", "measurement_setup"}
            or (form.context.get("adapter") == "training_plan_selection"
                and form.context.get("supervised"))
        )
        self._open_qupath.setVisible(
            (self._workflow_title is None or qupath is not None)
            and form.context.get("adapter") != "tissue_setup"
            and not options_decision
        )
        self._populate_annotation_images()
        annotation = self.annotation_review_identity()
        drawing_regions = form.context.get("reason") == "analysis_domain_rois_required"
        self._form_layout.removeItem(self._artifact_actions)
        action_position = (
            self._form_layout.indexOf(self._renderer)
            if drawing_regions
            else self._form_layout.indexOf(self._renderer) + 1
            if form.context.get("adapter") == "alignment_setup"
            else self._form_layout.indexOf(self._reference_region_actions) + 1
        )
        self._form_layout.insertLayout(action_position, self._artifact_actions)
        self._open_qupath.setProperty("actionRole", "primary" if annotation or drawing_regions or form.context.get("adapter") == "alignment_setup" else "secondary")
        self._open_qupath.style().unpolish(self._open_qupath)
        self._open_qupath.style().polish(self._open_qupath)
        marker_inspection = form.context.get("adapter") in {"marker_setup", "marker_gates"}
        self._open_qupath.setText(
            (
                "Annotate reference cells" if form.context.get("adapter") == "supervised_references"
                else "Annotate reference image"
                if form.context.get("reference_first")
                else "Review tissue outlines in QuPath"
                if form.context.get("reason") == "tissue_section_review_required"
                else "Annotate in QuPath"
            )
            if annotation
            else "Open registration editor" if form.context.get("adapter") == "alignment_setup"
            else "Inspect alignment and checks" if form.context.get("reason") == "registration_review_required"
            else "Inspect in QuPath" if marker_inspection else "Open QuPath"
        )
        self._qupath_save_help.setText(
            (
                "If you edit the detected sections, save your annotations in QuPath, then close it to return them to this analysis."
                if form.context.get("reason") == "tissue_section_review_required"
                else "Save your annotations in QuPath, then close it to return them to this analysis."
            )
            if annotation
            else "Inspect the saved alignment in the application editor, then return here to accept or reject it."
            if form.context.get("reason") == "registration_review_required" and not self.remote_execution
            else (
                "Inspect marker staining and cell outlines in a temporary copy of the project."
                if marker_inspection
                else "QuPath opens a temporary project for inspection."
            )
        )
        self._qupath_save_help.setVisible(
            qupath is not None
            and form.context.get("adapter") not in {"tissue_setup", "alignment_setup"}
            and not options_decision
        )
        if form.context.get("reason") == "primary_structure_count_review_required":
            counts = self._renderer.findChild(QTableView, f"form-{form.form_id}-primary_region_counts-table")
            if counts is not None:
                def describe_inspection(*_args):
                    selected = counts.currentIndex().siblingAtColumn(0)
                    if selected.row() <= 0:
                        label = "Inspect all classes in QuPath"
                    else:
                        label = f"Inspect {selected.data()} in QuPath"
                    self._open_qupath.setText(label)
                counts.selectionModel().currentRowChanged.connect(describe_inspection)
                describe_inspection()
            if self._count_navigation:
                item = self._count_regions.table.currentItem()
                key = self._count_regions.inspection_class()
                label = self._count_regions.table.horizontalHeaderItem(item.column()).text() if key and key != "primary_classifier" else "all classes"
                self._count_regions.inspect.setText("Inspect outlines in QuPath")
                self._count_regions.inspect.setToolTip(f"Open the selected analysis region and show outlines for {label}.")
                self._count_regions.inspect.setVisible(qupath is not None)
                self._count_regions.inspect.setEnabled(qupath is not None and (item is None or item.column() == 0 or key is not None))
                self._open_qupath.hide()
            self._qupath_save_help.hide()
        from .region_review import native_region_action
        native_region = native_region_action(form.context.get("reason"))
        if native_region is not None:
            visible, label, instructions = native_region
            self._open_qupath.setVisible(visible and qupath is not None)
            self._open_qupath.setText(label)
            self._qupath_save_help.setText(instructions)
            self._qupath_save_help.setVisible(visible and qupath is not None)
        self._update_training_annotation_visibility()
        self._plot_artifacts = form.plot_artifacts
        from .region_review import RegionDecisionReview
        region = self._renderer.findChild(RegionDecisionReview)
        if region is not None:
            region.set_image_artifacts(form.plot_artifacts)
            for widget in (self._plots, self._plot_selection, self._open_plot, self._export_plot_button):
                widget.hide()
            return
        from .training_fit_summary import TrainingFitSummary
        fit_summary = self._renderer.findChild(TrainingFitSummary)
        if fit_summary is not None:
            fit_summary.set_inspection_available(qupath is not None)
            self._open_qupath.hide()
            self._qupath_save_help.hide()
            fit_summary.set_evaluation_artifacts(form.plot_artifacts)
            for widget in (self._plots, self._plot_selection, self._open_plot, self._export_plot_button):
                widget.hide()
            return
        self._open_plot.setVisible(any(item.status == "available" for item in form.plot_artifacts))
        self._export_plot_button.setVisible(bool(form.plot_artifacts))
        table = FormTable(
            "plot_artifacts",
            ("title", "status", "path"),
            tuple(
                FormTableRow(
                    artifact.artifact_id,
                    (
                        artifact.title,
                        artifact.status,
                        artifact.path,
                    ),
                )
                for artifact in form.plot_artifacts
            ),
        )
        prior = self._plot_model
        self._plot_model = FormTableModel(table, self._plots)
        self._plots.setModel(self._plot_model)
        if prior is not None:
            prior.deleteLater()
        self._plots.setVisible(bool(table.rows))
        self._plot_selection.clear()
        self._plot_selection.addItem("", None)
        for artifact in form.plot_artifacts:
            if artifact.status == "available":
                self._plot_selection.addItem(artifact.title, artifact)
        self._plot_selection.setVisible(bool(table.rows))
        self._update_plot_open_enabled()

    def reference_region_identity(self):
        """Return the current pending review identity for native region actions."""
        view = self._current_view
        if type(view) is not BatchInteractionForm or not view.form.context.get("reference_regions_available"):
            return None
        return self._reference_review_identity()

    def _reference_review_identity(self):
        view = self._current_view
        if type(view) is not BatchInteractionForm:
            return None
        group = self._groups.get(view.group_id)
        target = next((item for item in getattr(group, "targets", ())
                       if item.target_id == self._target.currentData()), None)
        return None if target is None else (target.interaction_request_id, self._state_head_sha256)

    def _autoload_reference_regions(self):
        """Load a source's saved export once, under the existing review identity."""
        identity = self.reference_region_identity()
        if (identity is None or self._saved_reference_pending is not None
                or self._jobs.has_active_jobs or self._response_pending or not self._response_allowed()):
            return
        context = self._current_view.form.context
        loaded = getattr(self, '_reference_region_autoloaded', None)
        if (not context.get('reference_region_workspace', {}).get('unified')
                or context.get('reference_regions_summary') is not None
                or (loaded is not None and loaded[0] == identity and loaded[1] is self._current_view)):
            return
        self._reference_region_autoloaded = (identity, self._current_view)
        self._request_reference_regions('load')

    def _request_reference_regions(self, action):
        """Request one native edit, saved reload or explicit acceptance from this page."""
        revisit = self._reference_preparation_revisit
        if revisit is not None and self._current_view.form.context.get('reference_preparation_revisit'):
            if (revisit['identity'][:2] != self._reference_review_identity() or self._refresh_required
                    or self._response_pending or self._jobs.has_active_jobs or not self._response_allowed()):
                return
            summary = self._current_view.form.context.get('reference_regions_summary') or {}
            self.reference_preparation_regions_requested.emit(*revisit['identity'], action,
                summary.get('snapshot_sha256'))
            return
        identity = self.reference_region_identity()
        if identity is None or self._refresh_required or self._response_pending or not self._response_allowed():
            return
        summary = self._current_view.form.context.get("reference_regions_summary")
        digest = summary.get("snapshot_sha256") if summary else None
        form = self._current_view.form
        workspace = form.context.get('reference_region_workspace', {})
        if (action == 'edit' and workspace.get('unified')
                and workspace['checkpoint'] not in {'shared_control_preparation', 'positive_reference_rois'}):
            state = form.context['stage_result']['resume_context']['service_context']['operation_state']
            self._saved_reference_pending = {'run_id': state['run_id'],
                'checkpoint_token': state['checkpoint_token'], 'fluorophore': state['fluorophore'],
                'values': self._renderer.capture_draft_values(), 'edit': True,
                'option': 'reselect_reference_rois', 'export_sha256': digest, 'loading': False}
            self._saved_reference_origin_view = self._current_view
            self._submit_reference_reselection('reselect_reference_rois')
            return
        self.reference_regions_requested.emit(*identity, action, digest)

    def offer_reference_preparation_regions(self, identity, summary):
        revisit = self._reference_preparation_revisit
        if (revisit is None or revisit['identity'] != identity
                or self._reference_review_identity() != identity[:2]):
            return
        from mif_pipeline.gui.reference_regions import reference_region_form
        before = self._current_view.form.context.get('reference_regions_summary') or {}
        draft = self._renderer.capture_draft_values()
        form = reference_region_form(self._current_view.form, summary)
        same_export = before.get('snapshot_sha256') == summary.get('snapshot_sha256')
        self._drafts[self._draft_key()] = {key: value for key, value in draft.items()
            if key.startswith('reference_setting_') or same_export and key == 'reference_region_selection'}
        view = replace(self._current_view, form=form)
        self._replace_renderer()
        self._render_view(view)
        self._workspace.setCurrentWidget(self._detail_pane)
        self._set_status_message('')

    def offer_reference_regions(self, identity, summary):
        """Show real returned regions without replacing the current scientific request."""
        if self.reference_region_identity() != identity:
            return
        from mif_pipeline.gui.reference_regions import reference_region_form
        source_page = None
        if self._current_view.form.context.get('reference_region_workspace', {}).get('unified'):
            panel = self._renderer._reference_regions_panel
            source_page = (panel.page, panel.image_selector.currentData())
        previous_summary = self._current_view.form.context.get('reference_regions_summary') or {}
        same_export = (bool(summary.get('snapshot_sha256'))
                       and previous_summary.get('snapshot_sha256') == summary['snapshot_sha256'])
        updated = replace(self._current_view,
                          form=reference_region_form(self._current_view.form, summary))
        # Keep current numerical choices, while replacing region IDs and pairings
        # with the returned snapshot rather than restoring stale geometry choices.
        key = self._draft_key()
        if key is not None:
            initial = {control.name: control.value for control in self._current_view.form.controls}
            self._drafts[key] = {
                name: value for name, value in self._renderer.capture_draft_values().items()
                if (name.startswith("reference_setting_") and (previous_summary
                    or (value not in (None, '') and str(value) != str(initial.get(name)))))
                or (same_export and name == 'reference_region_selection')
            }
        self._replace_renderer()
        self._render_view(updated)
        if source_page and updated.form.context.get('reference_region_workspace', {}).get('unified'):
            panel = self._renderer._reference_regions_panel
            if source_page[1] is not None and panel.image_selector.findData(source_page[1]) >= 0:
                panel.show_page(*source_page)
        preparation = updated.form.context.get('reference_region_workspace', {}).get('preparation_role')
        self._set_status_message('' if summary['accepted'] or preparation in {'control', 'stained'} else
            'Review the saved regions before accepting them.' if summary['rows'] else '')
        pending = self._saved_reference_pending
        if pending is not None and pending["loading"]:
            same_geometry = summary["snapshot_sha256"] == pending["export_sha256"] and summary["accepted"]
            self._renderer.restore_values({name: value for name, value in pending["values"].items()
                                           if name.startswith("reference_setting_")
                                           or (same_geometry and not pending["edit"])})
            if pending["edit"]:
                self._saved_reference_pending = None
            else:
                if not same_geometry:
                    self._saved_reference_pending = None
                    self._set_status_message("The saved outlines changed. Review and accept the current regions before continuing; your pixel-selection settings are retained.")
                    return
                pending["ready"] = self.reference_region_identity()
                QTimer.singleShot(0, self, self._bind_saved_reference_choices)

    def _current_library_export_identity(self) -> tuple[str, str] | None:
        view = self._current_view
        if type(view) is not BatchInteractionForm or self._workflow_title is None:
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next(
            (item for item in group.targets if item.target_id == self._target.currentData()), None
        )
        if target is None or target.stage_result.stage != "reference_unmixing":
            return None
        owner = target.stage_result.resume_context.get("service_context", {})
        state = owner.get("operation_state", {})
        if state.get("review_summary", {}).get("portable_library_available") is not True:
            return None
        return target.interaction_request_id, self._state_head_sha256

    def _choose_library_export(self) -> None:
        identity = self._current_library_export_identity()
        if identity is None or self._refresh_required or self._response_pending:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export reference library",
            "reference-library.miflib.zip",
            "Reviewed spectral library (*.miflib.zip)",
        )
        if not path:
            return
        if (self._refresh_required or self._response_pending
                or self._current_library_export_identity() != identity):
            return
        destination = Path(path).expanduser().absolute()
        if not str(destination).endswith(".miflib.zip"):
            destination = Path(str(destination) + ".miflib.zip")
        self.export_library_requested.emit(*identity, destination)

    def _current_reference_view(self) -> ReferenceOnlyQuPathView | None:
        view = self._current_view
        if type(view) is not BatchInteractionForm:
            return None
        group = self._groups.get(view.group_id)
        if type(group) is not BatchInteractionGroup:
            return None
        target = next(
            (item for item in group.targets if item.target_id == self._target.currentData()), None
        )
        descriptor = target.reference_view if target is not None else None
        if descriptor is None or descriptor.state_head_sha256 != self._state_head_sha256:
            return None
        if descriptor.target_id != target.target_id:
            return None
        return descriptor

    def _open_tissue_authoring(self, method):
        view = self._current_view
        if (
            type(view) is not BatchInteractionForm
            or view.form.context.get("adapter") != "tissue_setup"
            or method not in {"threshold", "rf", "svm"}
        ):
            return
        group = self._groups.get(view.group_id)
        target = next(
            (item for item in group.targets if item.target_id == self._target.currentData()), None
        )
        if target is None or not target.project_entry_id:
            self._set_status_message("Select the image for tissue detection before opening QuPath.")
            return
        self._pending_tissue_authoring = {"entry_id": target.project_entry_id, "method": method}
        if method in {"rf", "svm"}:
            try:
                self._pending_tissue_authoring["setup"] = self._renderer.tissue_authoring_setup()
            except ValueError as error:
                self._pending_tissue_authoring = None
                self._renderer._present_error(str(error), "select_tissue_detector")
                return
        self._active_tissue_authoring = (view.group_id, target.target_id, self._state_head_sha256)
        self.open_qupath_requested.emit(Path(view.form.open_qupath_path))

    def tissue_authoring_finished(self, *, success=True, result=None):
        identity = getattr(self, "_active_tissue_authoring", None)
        self._active_tissue_authoring = None
        if not success or identity is None or result is None:
            return
        view = self._current_view
        if (
            type(view) is BatchInteractionForm
            and view.form.context.get("adapter") == "tissue_setup"
            and (view.group_id, self._target.currentData(), self._state_head_sha256) == identity
        ):
            self._renderer.offer_tissue_authoring_result(result)
            self._set_status_message(
                "Detector saved. Finish the tissue settings, then continue to use it in this analysis."
            )

    def _populate_annotation_images(self):
        view = self._current_view
        self._annotation_image.blockSignals(True)
        selected = self._annotation_image.currentData()
        self._annotation_image.clear()
        active = bool(
            not self.remote_execution
            and type(view) is BatchInteractionForm
            and view.form.context.get("reference_first")
        )
        if active:
            group = self._groups.get(view.group_id)
            target = next(
                (
                    item
                    for item in getattr(group, "targets", ())
                    if item.target_id == self._target.currentData()
                ),
                None,
            )
            if target is not None:
                from mif_pipeline.orchestration.cluster.cluster_annotations import annotation_entry_ids

                context = view.form.context["resume_context"]
                if view.form.context.get("adapter") == "supervised_references":
                    for row in context["references"]:
                        role_label = {"training": "Training", "validation": "Validation", "test": "Independent test"}[row["split"]]
                        self._annotation_image.addItem(
                            f"{row['sample_id']} · {role_label} · {', '.join(row['class_ids'])}",
                            (row["project_entry_id"], {
                                "training_plan_id": row["training_plan_id"],
                                "split": row["split"],
                            }),
                        )
                else:
                    participants = context["candidate_participants"]
                    for entry in annotation_entry_ids(target):
                        row = participants.get(entry, {})
                        title = str(row.get("sample_id") or row.get("slide_id") or entry)
                        self._annotation_image.addItem(title, entry)
                index = self._annotation_image.findData(selected)
                if index >= 0:
                    self._annotation_image.setCurrentIndex(index)
        self._annotation_image.blockSignals(False)
        visible = active and self._annotation_image.count() > 1
        self._annotation_image.setVisible(visible)
        self._annotation_image_label.setVisible(visible)
        panel = getattr(self._renderer, "_reference_plan", None)
        if panel is not None and panel._selected is not None:
            self._select_annotation_image(panel._selected)
        self._update_training_annotation_visibility()
        self._refresh_classifier_reference_regions()

    def _annotation_image_changed(self, *_args):
        if not hasattr(self, "_open_qupath"):
            return
        cell_panel = getattr(self._renderer, "_supervised_reference_panel", None)
        if cell_panel is not None:
            cell_panel.select_reference(self._annotation_image.currentData())
        identity = self.annotation_review_identity()
        if identity is not None:
            self._open_qupath.setEnabled(self._qupath_project_path is not None)

    def _request_feature_inspection(self, index):
        view = self._current_view
        if type(view) is not BatchInteractionForm:
            return
        if self.remote_execution:
            self._set_status_message("Inspect image features on the computer containing this project.")
            return
        try:
            entry_id = self._annotation_image.currentData()
            if index == -1:
                group = self._groups[view.group_id]
                target = next(item for item in group.targets if item.target_id == self._target.currentData())
                entry_id = target.project_entry_id
            request = self._renderer.feature_inspection_values(index, entry_id)
        except ValueError as error:
            self._set_status_message(str(error))
            return
        self._feature_inspection_origin = (view.form, index)
        self.feature_inspection_requested.emit(request)

    def feature_inspection_finished(self, result):
        """Apply native edits only to the form that opened the feature window."""
        origin = getattr(self, "_feature_inspection_origin", None)
        self._feature_inspection_origin = None
        if result.get("status") != "feature_settings_saved" or origin is None:
            return
        view = self._current_view
        if (type(view) is not BatchInteractionForm or view.form is not origin[0]
                or result.get("classifier_index") != origin[1]):
            return
        self._renderer.apply_pixel_features(origin[1], result["pixel_features"])
        self._capture_draft()

    def annotation_preview_inputs(self):
        view = self._current_view
        if (type(view) is not BatchInteractionForm or not view.form.context.get("reference_first")
                or view.form.context.get("adapter") == "supervised_references"):
            return None
        values = self._renderer._raw_values()
        return (view.form, values) if values.get("allow_annotation_preview") is True else None

    def present_annotation_preview_error(self, inputs, message):
        """Recover the unchanged field that failed this form's preview validation."""
        form, submitted = inputs
        view = self._current_view
        if type(view) is not BatchInteractionForm or view.form is not form:
            return None
        control = next((control for control in form.controls
                        if message.startswith((control.name + " ", control.name + ":"))), None)
        if control is None:
            return None
        if self._renderer._raw_values(draft=True).get(control.name) != submitted.get(control.name):
            return None
        self._renderer._present_error(message, "select_training_plan")
        return self._renderer._form_error_summary.text() or None

    def annotation_reference_role(self):
        """Pass the displayed accepted plan/role choice to the ordinary ticket owner."""
        view = self._current_view
        if (type(view) is BatchInteractionForm
                and view.form.context.get("adapter") == "supervised_references"):
            selected = self._annotation_image.currentData()
            return selected[1] if selected is not None else None
        return None

    def annotation_reference_inputs(self):
        """Snapshot pixel settings for validation in the annotation launch job."""
        view = self._current_view
        if (type(view) is BatchInteractionForm
                and view.form.context.get("adapter") == "training_plan_selection"
                and (view.form.context.get("primary") or view.form.context.get("secondary"))):
            return view.form, self._renderer._raw_values()
        return None

    def annotation_review_identity(self):
        if self.remote_execution:
            return None
        view = self._current_view
        if (type(view) is BatchInteractionForm
                and view.form.context.get("reason") in {"secondary_structure_selection_required", "primary_structure_count_review_required"}):
            return None
        if type(view) is not BatchInteractionForm or view.form.context.get("adapter") in {
            "alignment_setup",
            "tissue_setup",
            "training_fit_review",
            "segmentation_setup",
            "measurement_setup",
            "marker_setup",
            "marker_combinations",
            "marker_gates",
        }:
            return None
        if (view.form.context.get("adapter") == "training_plan_selection"
                and view.form.context.get("supervised")):
            return None
        if (self.reference_display_request() is not None
                or view.form.context.get("reason") == "tissue_native_level_required"):
            return None
        group = self._groups.get(view.group_id)
        target = next(
            (
                item
                for item in getattr(group, "targets", ())
                if item.target_id == self._target.currentData()
            ),
            None,
        )
        if target is None or target.stage_result.stage == "registration":
            return None
        from mif_pipeline.orchestration.cluster.cluster_annotations import annotation_entry_ids

        entries = annotation_entry_ids(target)
        selected = self._annotation_image.currentData()
        if view.form.context.get("adapter") == "supervised_references":
            selected = selected[0] if selected is not None else None
        if len(entries) == 1:
            selected = entries[0]
        elif selected not in entries:
            return None
        return target.interaction_request_id, self._state_head_sha256, selected

    def take_tissue_authoring_request(self):
        value = getattr(self, "_pending_tissue_authoring", None)
        self._pending_tissue_authoring = None
        return value

    def reference_display_request(self):
        view = self._current_view
        if (
            type(view) is BatchInteractionForm
            and view.form.context.get("adapter") == "alignment_setup"
        ):
            context = view.form.context.get("resume_context", {})
            fixed, moving = context.get("fixed_project_entry_id"), context.get(
                "moving_project_entry_id"
            )
            if fixed and moving:
                method = self._renderer.raw_values().get("alignment_method", "manual")
                if method not in {"manual", "automatic"}:
                    raise ValueError("Select manual or automatic registration.")
                return {"mode": "alignment", "fixed_entry_id": fixed, "moving_entry_id": moving,
                        "method": method}
        if type(view) is BatchInteractionForm:
            pending = view.form.context.get("resume_context", {}).get("pending_registration")
            if isinstance(pending, Mapping):
                return {
                    "mode": "candidate_overlay",
                    "fixed_entry_id": pending["fixed_project_entry_id"],
                    "moving_entry_id": pending["moving_project_entry_id"],
                    "candidate_transform_sha256": pending["transform_file_sha256"],
                    "review_policy": dict(pending.get("registration_qc", {}).get("policy", {})),
                }
        descriptor = self._current_reference_view()
        mode = self._reference_guidance.currentData()
        if descriptor is None or mode == "primary-only":
            return None
        if mode == "registered-overlay" and descriptor.registration_mode != "warpy":
            return None
        return {
            "mode": "overlay" if mode == "registered-overlay" else "side_by_side",
            "fixed_entry_id": descriptor.primary_project_entry_id,
            "moving_entry_id": descriptor.moving_project_entry_id,
        }

    def _reference_guidance_changed(self, _index: int = 0) -> None:
        descriptor = self._current_reference_view()
        paired = descriptor is not None and self._reference_guidance.currentData() in {
            "paired-reference-only",
            "registered-overlay",
        }
        self._reference_detail.setVisible(paired)
        self._reference_detail.setText(
            (
                "The accepted paired image will be shown over the native mIF image with adjustable opacity. "
                if self._reference_guidance.currentData() == "registered-overlay"
                else f"Open the primary mIF and paired {descriptor.moving_reference_modality} images in a two-pane view. "
            )
            + "Draw training annotations on the primary mIF image; the paired image provides tissue context."
            if paired
            else ""
        )

    def _set_status_message(self, message: str) -> None:
        self._processing_message = None
        self._status.setText(message)
        self._status.setVisible(bool(message))
        if not message:
            self._clear_failure_details()

    def _clear_failure_details(self) -> None:
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.hide()
        self._error_details.clear()

    def _show_failure_details(self, detail: str, explanation: str) -> None:
        expanded = diagnostic_details(detail, explanation)
        if expanded.startswith(explanation):
            expanded = expanded[len(explanation) :].lstrip()
        self._error_details.setText(expanded)
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.setVisible(bool(expanded))

    def _worker_failure_text(self, error: object, fallback: str) -> str:
        if type(error) is not DesktopJobError:
            return fallback
        summary = operation_error_summary(error.message) or fallback
        self._show_failure_details(error.diagnostic, summary)
        return summary

    def set_processing(self, message: str) -> None:
        self._set_submission_validation_enabled(False)
        self._set_status_message(message)
        self._processing_message = message

    def set_response_status(self, status: str, *, rejection_codes: tuple[str, ...] = ()) -> None:
        """

        Displays whether the processing step saved the submitted choices.

        After a failed save, we retain the explanation and available failure codes
        while the pending-step list refreshes. Otherwise the refresh could immediately
        replace the failure with the generic message that input is still needed.
        The restored form keeps its entered values available for correction or retry.

        Args:
            status: "accepted_for_retry" after saving or "rejected" after a processing check.
            rejection_codes: Returned failure identifiers included in the Details panel.

        Raises:
            ValueError: The status is outside the supported save outcomes.
        """

        if status not in {"accepted_for_retry", "rejected"}:
            raise ValueError("interaction response status is invalid")
        self._clear_failure_details()
        if status == "accepted_for_retry":
            self._set_status_message("Decision saved.")
        elif status == "rejected":
            self._restore_saved_reference_after_failure("The change was not saved. Your entries are retained; reopen the current review if it changed.")
            message = (
                "The application could not save these settings. Your entries are retained. "
                "Open Details to inspect the processing check."
            )
            if "unsupported_interaction" in rejection_codes:
                message = (
                    "This application version cannot save the selected step. "
                    "Keep the analysis folder and reopen it with the updated application."
                )
            self._set_status_message(message)
            if rejection_codes:
                self._show_failure_details(
                    "Processing checks:\n" + "\n".join(dict.fromkeys(rejection_codes)), message
                )
            if not self._response_pending:
                self._refresh_required = False
                self._set_submission_validation_enabled(True)
        if self._response_pending:
            self._response_status_received = True
            self._complete_response_refresh_if_ready()

    def set_response_failure(self, message: str) -> None:
        """Show a failed service mutation while awaiting fresh owner state."""
        if type(message) is not str or not message.strip():
            raise ValueError("interaction response failure requires a message")
        self._restore_saved_reference_after_failure(message)
        summary = operation_error_summary(message)
        self._set_status_message(
            summary
            or "The choice could not be saved. Your entries are retained. Open Details for the cause, then try again."
        )
        self._show_failure_details(message, self._status.text())
        if self._response_pending:
            self._response_status_received = True
            self._complete_response_refresh_if_ready()

    def _complete_response_refresh_if_ready(self) -> None:
        if not (
            self._response_pending
            and self._response_status_received
            and self._response_page_received
        ):
            return
        self._response_pending = False
        self._response_status_received = False
        self._response_page_received = False
        group_id = self._response_group_id
        self._response_group_id = None
        self._enable_current_page(
            select_first=group_id is None or group_id in self._groups,
            group_id=group_id,
        )
        self._bind_saved_reference_choices()

    def _select_index(self, index: QModelIndex) -> None:
        if self._list_model is None or not index.isValid():
            return
        if 0 <= index.row() < len(self._group_ids):
            group_id = self._group_ids[index.row()]
            if group_id != self._displayed_group_id:
                self.show_group(group_id)

    def _invalidate_tissue_source(self) -> None:
        self._tissue_observed_source = None
        self._tissue_observation_job_id = None

    def _observe_tissue_source(self, path: str) -> None:
        view = self._current_view
        if (
            type(view) is not BatchInteractionForm
            or view.form.context.get("adapter") != "tissue_setup"
        ):
            return
        self._tissue_observed_source = None
        self._renderer.begin_tissue_source_check()
        self._tissue_observation_job_id = self._jobs.submit(
            "observe_tissue_classifier_source",
            lambda: observe_tissue_classifier_source(path),
            access=JobAccess.READ,
        )

    def submit_optional_setup_skip(self, node_id: str, head: str) -> None:
        """Submit the ordinary optional-setup choice for one freshly listed node."""
        if self._state_head_sha256 != head or self._refresh_required or self._response_pending:
            raise ValueError("This setup changed. Reopen its current review before skipping it.")
        targets = [(group, target) for group in self._groups.values()
                   if type(group) is BatchInteractionGroup for target in group.targets
                   if target.node_id == node_id]
        if len(targets) != 1:
            raise ValueError("The optional setup is no longer waiting for this choice.")
        group, target = targets[0]
        if self._displayed_group_id != group.group_id:
            self.show_group(group.group_id)
        if self._target.currentData() != target.target_id:
            self._target.setCurrentIndex(self._target.findData(target.target_id))
        view = self._current_view
        if type(view) is not BatchInteractionForm or view.representative_target_id != target.target_id:
            raise ValueError("Open the current setup form before skipping this step.")
        if (group.stage == "primary_classifier_training_plan_review"
                and view.form.context.get("adapter") == "training_plan_selection"
                and view.form.context.get("primary") is True):
            action, values = "select_training_plan", {"classifier_source": "skip"}
        elif (group.stage == "secondary_structure_definition"
                and view.form.context.get("adapter") == "secondary_definition"):
            action, values = "accept", {"definitions": {"selection": "none", "structures": []}}
        else:
            raise ValueError("This review does not offer an optional setup skip.")
        self._renderer.restore_values(values)
        self._renderer.last_submission_continuation = True
        self._submit_raw_response(action, values)

    def _submit_raw_response(
        self,
        action: str,
        raw_values: Mapping[str, object],
    ) -> None:
        if (
            not self._response_allowed()
            or self._submission_job_id is not None
            or self._refresh_required
            or self._tissue_observation_job_id is not None
        ):
            return
        self._submission_continuation = getattr(
            self._renderer, "last_submission_continuation", True
        )
        view = self._current_view
        if type(view) is AggregateReviewForm:
            try:
                values = decode_form_values(view.form, action, raw_values)
                from ..presenters.aggregate_review import cluster_decisions_from_values
                labels, excluded = cluster_decisions_from_values(view, values)
                request = build_aggregate_review_request(
                    view,
                    cluster_labels=labels,
                    excluded_cluster_ids=excluded,
                )
            except (TypeError, ValueError) as exc:
                detail = str(exc)
                summary = (
                    "A cluster in the response is not in this review. "
                    "Check the cluster labels shown here, then try again."
                    if "unknown cluster ID" in detail
                    else "The review response could not be accepted. Check the entered values, "
                    "then try again. Open Details for the exact validation rule."
                )
                self._set_status_message(summary)
                self._show_failure_details(detail, summary)
                return
            self._emit_validated_response(request)
            return
        target_id = self._target.currentData()
        if type(view) is not BatchInteractionForm or type(target_id) is not str:
            return
        if view.form.context.get('reference_preparation_revisit'):
            try:
                prepared = self._reference_preparation_submission(raw_values)
            except (TypeError, ValueError) as exc:
                self._set_status_message(str(exc))
                return
            if prepared is None:
                return
            view, submitted = prepared
        else:
            submitted = dict(raw_values)
        if view.form.context.get('adapter') == 'marker_setup' and 'revisit_marker_id' in submitted and 'marker_settings' in submitted:
            from .marker_preparation import interrupted_marker_draft_key
            key = interrupted_marker_draft_key(view.form)
            if key is not None:
                prefix = 'marker_' + key[2] + '_'
                self._drafts[key] = {name: value for name, value in self._renderer.capture_draft_values().items()
                                     if name.startswith(prefix)}
        if view.form.context.get("adapter") == "tissue_setup":
            if action == "refresh_tissue_detectors":
                self._tissue_refresh_values = (
                    target_id,
                    view.form.context["seed"]["validation_domain_id"],
                    submitted.copy(),
                )
            if self._tissue_observed_source is not None:
                submitted["__observed_source"] = self._tissue_observed_source.to_mapping()
        if self._inventory_form is not None:
            self._capture_inventory_selection()
            submitted["selected_content_keys"] = self._inventory_selected_keys
        count_ids = self._count_regions.selected_ids() if self._count_navigation else None
        self._set_submission_validation_enabled(False)
        job_id = self._jobs.submit(
            "validate_batch_interaction_response",
            lambda: build_batch_interaction_request(
                view,
                target_id=target_id,
                action=action,
                raw_values=submitted,
                count_target_ids=count_ids,
            ),
            access=JobAccess.READ,
        )
        self._submission_job_id = job_id

    def _emit_validated_response(
        self,
        request: BatchInteractionResponseRequest,
    ) -> None:
        # The controller owns close intent. A joined validation result may
        # arrive while close is waiting for this same bridge to drain.
        if not self._response_allowed():
            return
        self._refresh_required = True
        self._response_pending = True
        self._response_status_received = False
        self._response_page_received = False
        self._set_status_message("")
        self._table.setEnabled(False)
        self._previous.setEnabled(False)
        self._next.setEnabled(False)
        continue_after = self.continue_after_save and getattr(
            self, "_submission_continuation", True
        )
        self._capture_draft()
        self._set_submission_validation_enabled(False)
        self._submitted_continuation = (request, continue_after)
        self._alignment_submission = None
        if (
            type(self._current_view) is BatchInteractionForm
            and self._current_view.form.context.get("adapter") == "alignment_setup"
            and request.action == "import_warpy_transform"
        ):
            from mif_pipeline.gui.alignment_setup import alignment_options

            options = alignment_options(self._current_view.form, self._renderer.raw_values())
            if self.remote_execution and options is None:
                options = {"method": "landmarks"}
            self._alignment_submission = (request, options)
        self.response_requested.emit(request)

    def _set_submission_validation_enabled(self, enabled: bool) -> None:
        page = self._page
        self._table.setEnabled(enabled)
        self._renderer.setEnabled(enabled)
        self._nimbus_setup_button.setEnabled(enabled)
        self._actions.setEnabled(enabled)
        if not enabled:
            self._open_qupath.setEnabled(False)
            self._open_plot.setEnabled(False)
            self._export_plot_button.setEnabled(False)
            self._umap._invalidate_view()
        self._target.setEnabled(enabled)
        self._review_group.setEnabled(enabled)
        self._count_regions.setEnabled(enabled)
        self._all_reviews.setEnabled(enabled)
        self._directory_open.setEnabled(enabled and bool(self._displayed_group_id))
        self._previous.setEnabled(enabled and page is not None and page.offset > 0)
        self._next.setEnabled(enabled and page is not None and page.next_offset is not None)
        if enabled and self._inventory_form is not None:
            offset = int(self._inventory_form.context["inventory_page_offset"])
            inventory_page = next(
                table
                for table in self._inventory_form.tables
                if table.table_id.startswith("inventory_page:")
            )
            self._inventory_previous.setEnabled(offset > 0)
            self._inventory_next.setEnabled(
                offset + len(inventory_page.rows)
                < self._inventory_record_count(self._inventory_form)
            )
        else:
            self._inventory_previous.setEnabled(False)
            self._inventory_next.setEnabled(False)

    def _request_inventory_previous(self) -> None:
        form = self._inventory_form
        if form is None or self._inventory_job_id is not None:
            return
        offset = int(form.context["inventory_page_offset"])
        limit = int(form.context["inventory_page_limit"])
        if offset <= 0:
            return
        self._request_inventory_page(max(0, offset - limit))

    def _request_inventory_next(self) -> None:
        form = self._inventory_form
        if form is None or self._inventory_job_id is not None:
            return
        offset = int(form.context["inventory_page_offset"])
        limit = int(form.context["inventory_page_limit"])
        page = next(item for item in form.tables if item.table_id.startswith("inventory_page:"))
        if offset + len(page.rows) >= self._inventory_record_count(form):
            return
        self._request_inventory_page(offset + limit)

    def _request_inventory_page(self, offset: int) -> None:
        form = self._inventory_form
        view = self._current_view
        if form is None or type(view) is not BatchInteractionForm:
            return
        self._capture_inventory_selection()
        control_values = self._renderer.raw_values()
        control_values["selected_content_keys"] = self._inventory_selected_keys
        self._inventory_previous.setEnabled(False)
        self._inventory_next.setEnabled(False)
        job_id = self._jobs.submit(
            "page_secondary_inventory",
            lambda: page_roi_inventory_form(
                form,
                offset=offset,
                control_values=control_values,
            ),
            access=JobAccess.READ,
        )
        self._inventory_job_id = job_id

    def _job_succeeded(self, job_id: str, value: object) -> None:
        generation = self._nimbus_checkpoint_jobs.pop(job_id, None)
        if generation is not None:
            if generation == self._nimbus_setup_generation:
                self._nimbus_checkpoint_status.setText(str(value))
            return
        if job_id == self._tissue_observation_job_id:
            self._tissue_observation_job_id = None
            if (
                type(value) is ObservedTissueClassifierSource
                and type(self._current_view) is BatchInteractionForm
                and self._current_view.form.context.get("adapter") == "tissue_setup"
            ):
                from mif_pipeline.gui.tissue_setup import normalized_tissue_source_path

                try:
                    selected_path = normalized_tissue_source_path(
                        self._renderer.raw_values().get("source_path")
                    )
                except ValueError:
                    self._renderer.set_tissue_observation_result(
                        success=False, message="Select a detector file to continue."
                    )
                    return
                if selected_path != value.path:
                    self._renderer.set_tissue_observation_result(
                        success=False,
                        message="The detector path changed during its check. Select the file again.",
                    )
                    return
                self._tissue_observed_source = value
                self._renderer.set_tissue_observation_result(
                    success=True,
                )
            return
        if job_id == self._submission_job_id:
            self._submission_job_id = None
            if type(value) is BatchInteractionResponseRequest:
                self._emit_validated_response(value)
            else:
                self._set_submission_validation_enabled(True)
            return
        if job_id != self._inventory_job_id:
            return
        self._inventory_job_id = None
        if type(value) is BatchInteractionForm:
            self._render_view(value)
            return
        if type(value) is FormModel and type(self._current_view) is BatchInteractionForm:
            self._current_view = replace(self._current_view, form=value)
            self._render_view(self._current_view)

    def _job_failed(self, job_id: str, error: object) -> None:
        generation = self._nimbus_checkpoint_jobs.pop(job_id, None)
        if generation is not None:
            if generation == self._nimbus_setup_generation:
                self._nimbus_checkpoint_status.setText(
                    "The Nimbus model could not be checked. Open Software setup or check the model file path."
                )
            return
        if job_id == self._tissue_observation_job_id:
            self._tissue_observation_job_id = None
            self._tissue_observed_source = None
            message = self._worker_failure_text(
                error,
                "Could not check the classifier file. Open Details "
                "before selecting another file.",
            )
            self._renderer.set_tissue_observation_result(success=False, message=message)
            return
        if job_id == self._submission_job_id:
            self._submission_job_id = None
            self._tissue_refresh_values = None
            self._set_submission_validation_enabled(True)
            message = self._worker_failure_text(
                error,
                "Check the entered values and save again. Your entries have been retained. Open Details for the cause.",
            )
            if type(error) is DesktopJobError and type(self._current_view) is BatchInteractionForm:
                # Worker validation uses the same field/page recovery as direct forms.
                # This includes finite-number and bounds errors in collapsed sections.
                self._renderer._present_error(error.message, "")
                if self._renderer._form_error_summary.text():
                    message = self._renderer._form_error_summary.text()
            if (
                type(error) is DesktopJobError
                and type(self._current_view) is BatchInteractionForm
                and self._current_view.form.context.get("adapter") == "marker_setup"
                and error.message == "composite marker states are empty"
            ):
                message = (
                    "Select at least one positive or negative marker for each named cell type."
                )
            self._set_status_message(message)
            self._restore_saved_reference_after_failure(message)
            return
        if job_id != self._inventory_job_id:
            return
        self._inventory_job_id = None
        self._displayed_group_id = None
        message = self._worker_failure_text(
            error,
            "Could not load the available regions. Open Details " "before changing this review.",
        )
        self._set_status_message(message)

    def _request_previous(self) -> None:
        if self._page is None or self._refresh_required or self._page.offset <= 0:
            return
        self.page_requested.emit(
            BatchInteractionQuery(
                offset=max(0, self._page.offset - self._page.limit),
                limit=self._page.limit,
            )
        )

    def _request_next(self) -> None:
        if self._page is None or self._refresh_required or self._page.next_offset is None:
            return
        self.page_requested.emit(
            BatchInteractionQuery(
                offset=self._page.next_offset,
                limit=self._page.limit,
            )
        )

    def classifier_error_review_request(self):
        from .training_fit_summary import TrainingFitSummary
        workspace = self._renderer.findChild(TrainingFitSummary)
        return workspace.take_error_review_request() if workspace is not None else None

    def inspection_scope(self):
        view = self._current_view
        if type(view) is BatchInteractionForm and view.form.context.get("reason") == "primary_structure_count_review_required":
            context = view.form.context
            scope = {"slide_id": context["stage_result"]["slide_id"],
                     "domain_id": context["resume_context"]["analysis_domain_id"]}
            if self._count_navigation:
                key = self._count_regions.inspection_class()
                if key and key != "primary_classifier":
                    scope["class_id"] = key
                return scope
            table = next(t for t in view.form.tables if t.table_id == "primary_region_counts")
            counts = self._renderer.findChild(QTableView, f"form-{view.form.form_id}-{table.table_id}-table")
            if counts is not None and counts.currentIndex().isValid():
                row_id = table.rows[counts.currentIndex().row()].row_id
                if row_id != "primary_classifier":
                    scope["class_id"] = row_id
            return scope
        return {}

    def classifier_image_review_request(self):
        from .training_fit_summary import TrainingFitSummary
        workspace = self._renderer.findChild(TrainingFitSummary)
        return workspace.selected_image_review() if workspace is not None else None

    def show_registration_editor(self, editor) -> None:
        """Keep the current decision form while its paired-image editor is open."""
        self._registration_return_page = self._workspace.currentWidget()
        self._workspace.addWidget(editor)
        self._workspace.setCurrentWidget(editor)

    def finish_registration_editor(self, editor) -> None:
        """Restore the decision after the editor's native and local jobs drain."""
        previous = getattr(self, '_registration_return_page', None)
        if previous is not None and self._workspace.indexOf(previous) >= 0:
            self._workspace.setCurrentWidget(previous)
        self._workspace.removeWidget(editor)
        self._registration_return_page = None
        editor.deleteLater()

    def _emit_open_qupath(self) -> None:
        if self._qupath_project_path is not None:
            self.open_qupath_requested.emit(self._qupath_project_path)

    def _emit_open_plot(self) -> None:
        artifact = self._plot_selection.currentData()
        if type(artifact) is PlotArtifact and artifact.status == "available":
            self.open_plot_requested.emit(Path(artifact.path))

    def _update_plot_open_enabled(self) -> None:
        artifact = self._plot_selection.currentData()
        self._open_plot.setEnabled(
            type(artifact) is PlotArtifact and artifact.status == "available"
        )
        self._export_plot_button.setEnabled(self._open_plot.isEnabled())

    def _export_plot(self) -> None:
        """Copy a prepared rendering; this action never requests UMAP or fitting."""
        from .figure_export import export_plot_files
        artifact = self._plot_selection.currentData()
        if type(artifact) is not PlotArtifact or artifact.status != "available":
            return
        identity = artifact.artifact_id.rsplit("/", 1)[0]
        paths = {Path(item.path).suffix.lower().lstrip("."): Path(item.path)
                 for item in self._plot_artifacts if item.status == "available"
                 and item.artifact_id.rsplit("/", 1)[0] == identity}
        name = "umap.svg" if type(self._current_view) is AggregateReviewForm else "classifier-confusion.svg"
        export_plot_files(self, paths, name)

    def _leave_form(self) -> None:
        self._capture_draft()
        if self._workflow_title is not None:
            self.back_to_workflow_requested.emit()
        else:
            self.directory_requested.emit()

    def _clear_detail(self) -> None:
        self._detach_reference_components_link()
        # The coordinate widget is shared across aggregate reviews; detach it
        # before disposing a renderer that currently owns its inspection page.
        self._detach_inventory_panel()
        self._umap.setParent(self)
        self._umap.hide()
        self._reference_region_actions.hide()
        self._reference_components_link.hide()
        self._reference_component_inspection = None
        self._reference_control_inspection = None
        self._capture_draft()
        self._reference_preparation_read = None
        self._reference_preparation_revisit = None
        self._reference_preparation_origin = None
        self._reference_preparation_history.clear()
        self._continue_choice.hide()
        self._clear_actions()
        if self._marker_panel is not None:
            self._form_layout.removeWidget(self._marker_panel)
            self._marker_panel.setParent(None)
            self._marker_panel.deleteLater()
            self._marker_panel = None
        self._displayed_group_id = None
        self._current_view = None
        self._nimbus_check_timer.stop()
        self._nimbus_setup_generation += 1
        for widget in (self._nimbus_setup_message, self._nimbus_checkpoint_status, self._nimbus_setup_button):
            widget.hide()
        self._reference_guidance.setCurrentIndex(0)
        self._reference_guidance_label.setVisible(False)
        self._reference_guidance.setVisible(False)
        self._reference_guidance_help.setVisible(False)
        self._reference_detail.clear()
        self._reference_detail.setVisible(False)
        self._detail.clear()
        self._detail.hide()
        self._detail.setAccessibleName("Review details")
        self._target.clear()
        self._target_label.setVisible(False)
        self._target.setVisible(False)
        old = self._renderer
        self._form_layout.removeWidget(old)
        old.setParent(None)
        old.deleteLater()
        self._renderer = self._new_renderer()
        self._qupath_project_path = None
        self._plot_artifacts = ()
        self._export_plot_button.hide()
        self._set_plot_status("")
        self._plot_selection.clear()
        self._plot_selection.setVisible(False)
        self._plots.setModel(None)
        self._plots.setVisible(False)
        self._inventory_form = None
        self._inventory_selected_keys = ()
        self._inventory.setModel(None)
        self._inventory.setVisible(False)
        self._inventory_panel.hide()
        self._inventory_previous.setVisible(False)
        self._inventory_next.setVisible(False)
        self._open_qupath.setEnabled(False)
        self._open_plot.setEnabled(False)

    def _set_empty_controls(self) -> None:
        self._previous.setEnabled(False)
        self._next.setEnabled(False)
        self._previous.hide()
        self._next.hide()
        self._open_qupath.setEnabled(False)
        self._open_plot.setEnabled(False)
        self._inventory_previous.setVisible(False)
        self._inventory_next.setVisible(False)


__all__ = ["ReviewsTrainingPage"]
