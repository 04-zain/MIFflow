"""Coordinate desktop pages with the active analysis service and background jobs.

The controller opens or closes workspace sessions, submits lifecycle and review operations,
and routes completion/error signals back to the appropriate Qt page. It retains the active
request identity across asynchronous work and coordinates safe shutdown before replacing or
closing the workspace."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtWidgets import QLabel, QDialog, QVBoxLayout, QComboBox, QDialogButtonBox

from ..desktop_services import (
    BatchServicePort,
    BatchServiceProvider,
    DesktopFileOperations,
)
from ..analysis_setup import (
    AnalysisSetupDocument,
    NewAnalysisRequest,
    create_analysis_setup,
    load_analysis_setup,
    save_analysis_setup,
    prepare_analysis_manifest,
)
from ..analysis_location import resolve_analysis_folder
from ..manifest_draft import (
    ManifestDraft,
    load_manifest_draft,
    manifest_draft_from_manifest,
    validate_manifest_draft,
    write_manifest_draft,
)
from ..source_series import SourceSeriesInspectionPort
from ..workspace_session import SessionExitState, WorkspaceSession
from ..presenters.plan import build_batch_plan_view
from ..presenters.run import build_batch_run_view
from ..presenters.results import build_results_view
from ..successor_services import (
    BoundSuccessorRoutes,
    SuccessorServiceRegistry,
    bind_successor_routes,
)
from mif_pipeline.io import SourceSeriesInventory
from mif_pipeline.orchestration.batch_interactions import (
    BatchInteractionPage,
    BatchInteractionQuery,
    BatchInteractionResponseRequest,
    BatchInteractionApplicationResult,
)
from mif_pipeline.orchestration.batch_service_models import (
    BatchCancellationResult,
    BatchRetryChoice,
    BatchReviewPlots,
)
from .home import HomePage, OpenWorkspaceIntent
from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .plan import PlanPage
from .results import ResultsPage
from .reviews_training import ReviewsTrainingPage
from .run import RunPage
from .samples import SamplesPage
from .shell import WorkspaceShell

# ==============================================================================
#                                   Controller
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _OpenedWorkspace:
    output_root: Path
    batch_id: str
    service: BatchServicePort
    status: object
    draft: ManifestDraft | None
    plan: object | None = None


@dataclass(frozen=True, slots=True)
class _PreparedWorkspace:
    manifest_path: Path
    draft: ManifestDraft
    service: BatchServicePort
    validation: object
    plan: object
    execution_status: object | None


@dataclass(frozen=True, slots=True)
class _CompletedResults:
    status_head: str
    routes: BoundSuccessorRoutes
    saved_images_available: bool
    session: WorkspaceSession
    epoch: int


@dataclass(frozen=True, slots=True)
class SetupDiscardCloseRequest:
    request_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class _SetupDiscardOffer:
    request: SetupDiscardCloseRequest
    setup: AnalysisSetupDocument
    page: SamplesPage
    draft: ManifestDraft


_QUPATH_OPEN_GUIDANCE = "QuPath is open. Close its window to return to the workflow."


_ACTIVITY_TEXT = {
    "validate_manifest_draft": "Validating samples…",
    "inspect_source_series": "Inspecting image…",
    "open_workspace": "Opening analysis…",
    "load_manifest_draft": "Opening analysis…",
    "load_analysis_setup": "Opening analysis…",
    "create_analysis_setup": "Creating analysis…",
    "save_analysis_setup": "Saving analysis…",
    "prepare_analysis_manifest": "Preparing analysis…",
    "plan_analysis": "Preparing plan…",
    "run": "Running analysis…",
    "resume": "Running analysis…",
    "retry": "Retrying step…",
    "retry_choices": "Checking available retries…",
    "review_plots": "Preparing figures…",
    "marker_combination_preview": "Calculating phenotype assignments…",
    "saved_stage_results": "Loading saved results…",
    "saved_stage_figures": "Preparing the saved figure…",
    "open_qupath": _QUPATH_OPEN_GUIDANCE,
    "segmentation_review_correction_preview": "Preparing cell correction…",
    "correct_cell": "Saving cell correction…",
    "prepare_registration_editor": "Opening the paired images…",
    "registration_editor": "Paired-image registration is open",
    "close_registration_editor": "Closing the paired images…",
    "check_cell_alignment": "Checking accepted paired images…",
    "prepare_cell_alignment": "Opening aligned H&E…",
    "cell_alignment_reader": "Aligned H&E is available",
    "open_cell_alignment": "Opening aligned H&E…",
    "read_cell_alignment": "Reading the aligned cell field…",
    "close_cell_alignment": "Closing aligned H&E…",
}

_STAGE_ACTIVITY_TEXT = {
    "inspection": "Inspecting images…",
    "reference_unmixing": "Spectral unmixing…",
    "analysis_preflight": "Checking prepared images…",
    "project_upsert": "Adding images to the project…",
    "tissue_setup": "Preparing the tissue detector…",
    "tissue_candidate": "Detecting tissue…",
    "tissue_acceptance": "Saving tissue sections…",
    "analysis_domain_selection": "Preparing analysis regions…",
    "paired_image_preparation": "Preparing paired images…",
    "registration": "Aligning images…",
    "primary_training_evidence_snapshot": "Reading tissue reference annotations…",
    "primary_classifier_training_review": "Fitting the tissue classifier…",
    "primary_classifier_inference": "Classifying tissue…",
    "secondary_training_evidence_snapshot": "Reading structure reference annotations…",
    "secondary_classifier_training_review": "Fitting structure classifiers…",
    "secondary_classifier_inference": "Detecting secondary structures…",
    "segmentation": "Segmenting cells…",
    "segmentation_correction": "Saving cell correction…",
    "measurements": "Calculating cell measurements…",
    "unsupervised_preparation": "Preparing cell measurements for clustering…",
    "clustering_per_slide": "Clustering cells…",
    "clustering_pooled": "Clustering cells…",
    "supervised_training_evidence_snapshot": "Reading labelled reference cells…",
    "supervised_phenotype_training_review": "Fitting the cell classifier…",
    "supervised_phenotype_inference": "Classifying cells…",
    "marker_gate_review": "Calculating marker thresholds…",
    "marker_rule_application": "Assigning cell phenotypes…",
    "final_non_spatial_export": "Exporting results…",
}


class QtDesktopController(QObject):
    """Manage workspace opening and close authority through the public port."""

    workspace_opened = Signal(object)
    operation_failed = Signal(str)
    discard_setup_close_requested = Signal(object)

    def __init__(
        self,
        *,
        shell: WorkspaceShell,
        jobs: ManagedJobBridge,
        batch_services: BatchServiceProvider,
        samples_page: SamplesPage | None = None,
        source_series_inspection: SourceSeriesInspectionPort | None = None,
        file_operations: DesktopFileOperations | None = None,
        successors: SuccessorServiceRegistry = SuccessorServiceRegistry(),
        segmentation_model_directory: Path | None = None,
    ) -> None:
        super().__init__(shell)
        self.shell = shell
        self.jobs = jobs
        self.batch_services = batch_services
        if samples_page is not None and source_series_inspection is None:
            raise ValueError("source-series page requires an inspection port")
        self.samples_page: SamplesPage | None = None
        self.source_series_inspection = source_series_inspection
        self.file_operations = file_operations
        if file_operations is not None:
            stopped_guard = file_operations.require_qupath_stopped

            def require_qupath_stopped_for_mutation() -> None:
                try:
                    stopped_guard()
                except Exception as exc:
                    raise RuntimeError(_QUPATH_OPEN_GUIDANCE) from exc

            jobs.bind_mutation_guard(require_qupath_stopped_for_mutation)
        self.successors = successors
        self._segmentation_model_directory = segmentation_model_directory

        self._initialize_workspace_state()
        shell.close_requested.connect(self.request_close)
        shell.route_changed.connect(self._route_changed)
        shell.sample_selected.connect(self._select_project_sample)
        home = shell.findChild(HomePage)
        if home is None:
            raise ValueError("workspace shell requires a Home page")
        self._home = home
        home.new_workspace_requested.connect(self._new_analysis_request)
        home.open_workspace_requested.connect(self._open_manifest_intent)
        home.open_folder_requested.connect(self._open_folder)
        home.software_setup_return_requested.connect(self._return_from_software_setup)
        if samples_page is not None:
            self.attach_samples_page(samples_page)
        jobs.job_succeeded.connect(self._job_succeeded)
        jobs.job_failed.connect(self._job_failed)
        jobs.active_jobs_changed.connect(self._jobs_changed)
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(250)
        self._activity_timer.timeout.connect(self._update_activity)
        self._jobs_changed(jobs.active_job_ids)

    def open_image_appearance(self):
        """Edit existing prepared-image display settings without a stage revision."""
        from .image_appearance_editor import ImageAppearanceDialog
        from .region_review import RegionDecisionReview
        session = self.session
        if session is None or session.last_status is None:
            return
        images = [(row.sample_id, row.processed_image_path)
                  for row in session.last_status.summary_rows if row.processed_image_path]
        if not images:
            return
        dialog = ImageAppearanceDialog(images, Path(session.output_root) / 'image-appearance.json',
                                       self.jobs, self.run_page)
        region = self.reviews_page.findChild(RegionDecisionReview)
        if region is not None:
            dialog.saved.connect(region.refresh_appearance)
        dialog.show()

    def _initialize_workspace_state(self) -> None:
        """Initialize the current analysis fields while retaining application services."""
        self.samples_page = None
        self._open_after_close: OpenWorkspaceIntent | None = None
        self._source_series_jobs: dict[str, tuple[str, Path]] = {}
        self._paired_image_jobs: dict[str, tuple[SamplesPage, str, Path]] = {}
        self._manifest_load_jobs: dict[str, Path] = {}
        self._folder_load_jobs: set[str] = set()
        self._setup_load_jobs: dict[str, Path] = {}
        self._latest_manifest_load_job_id: str | None = None
        self._setup_create_job_id: str | None = None
        self._setup_save_job_id: str | None = None
        self._setup_manifest_job_id: str | None = None
        self._prepared_manifest_path: Path | None = None
        self._setup: AnalysisSetupDocument | None = None
        self._setup_dirty = False
        self._setup_save_document: AnalysisSetupDocument | None = None
        self._promote_after_save = False
        self._pending_loaded_manifest: ManifestDraft | None = None
        self.session: WorkspaceSession | None = None
        self._prepared_without_execution = False
        self._open_job_id: str | None = None
        self._new_job_id: str | None = None
        self._validation_jobs: dict[str, ManifestDraft] = {}
        self._run_job_id: str | None = None
        self._status_job_id: str | None = None
        self._terminal_status_job_id: str | None = None
        self._pending_run_failure: str | None = None
        self._close_pause_failure: str | None = None
        self._status_epoch = 0
        self._retry_generation = 0
        self._retry_status_key: tuple[int, int, int, str] | None = None
        self._retry_cache_key: tuple[int, int, int, str] | None = None
        self._retry_choices: tuple[BatchRetryChoice, ...] = ()
        self._retry_choice_jobs: dict[str, tuple[int, int, int, str]] = {}
        self._stage_option_jobs: dict[str, tuple] = {}
        self._cluster_refit_option_drafts: dict[str, tuple[str,...]] = {}
        self._stage_revision_jobs: dict[str, tuple[int, object]] = {}
        self._stage_options_dialog = None
        from .tissue_navigation import TissueNavigation
        self._tissue_navigation = TissueNavigation(self)
        from .measurement_stage_review import MeasurementReviewNavigation
        self._measurement_review = MeasurementReviewNavigation(self)
        self._segmentation_review = MeasurementReviewNavigation(self, stage_kind="segmentation")
        from .saved_supervised_review import SavedSupervisedReviewNavigation
        self._saved_supervised_review = SavedSupervisedReviewNavigation(self)
        self._cutoff_saved_page = None
        from .registration_navigation import RegistrationNavigation
        self._registration_navigation = RegistrationNavigation(self)
        from .cell_alignment_navigation import CellAlignmentNavigation
        self._cell_alignment = CellAlignmentNavigation(self)
        self._interaction_jobs: dict[str, tuple[BatchInteractionQuery, str, int]] = {}
        self._workflow_review_node_ids: tuple[str, ...] = ()
        self._workflow_skip_pending = None
        self._software_setup_review = None
        self._review_plot_jobs: dict[str, tuple[WorkspaceSession, int, str, str, object]] = {}
        self._prediction_page_jobs: dict[str, tuple] = {}
        self._combination_preview_jobs: dict[str, tuple] = {}
        self._secondary_marker_jobs: dict[str, tuple] = {}
        self._kronos_cell_preview_jobs: dict[str, tuple] = {}
        self._response_job_id: str | None = None
        self._cutoff_assignment_job_id: str | None = None
        self._response_refresh_pending = False
        self._continue_session: WorkspaceSession | None = None
        self._continue_accepted = False
        self._continue_origin_head: str | None = None
        self._results_job_id: str | None = None
        self._saved_stage_jobs: dict[str, tuple] = {}
        self._saved_figure_jobs: dict[str, tuple] = {}
        self._measurement_field_jobs: dict[str, tuple] = {}
        self._measurement_field_pending = None
        self._marker_field_jobs = {}
        self._marker_field_pending = None
        self._cluster_model_jobs = {}
        self._saved_region_jobs: dict[str, tuple] = {}
        self._saved_results_key = None
        self._file_jobs: set[str] = set()
        self._annotation_preview_jobs: dict[str, tuple] = {}
        self._reference_region_jobs = {}
        self._reference_component_jobs = {}
        self._reference_preparation_jobs = {}
        self._reference_control_jobs = {}
        self._qupath_job_id: str | None = None
        self._successor_jobs: dict[str, str] = {}
        self._spatial_results: tuple[object, ...] = ()
        self._pause_job_id: str | None = None
        self._cancel_job_id: str | None = None
        self._close_pending = False
        self._close_queued = False
        self._setup_discard_offer: _SetupDiscardOffer | None = None
        self._discard_setup_on_close: _SetupDiscardOffer | None = None

    def _clear_workspace(self) -> None:
        """Release drained analysis pages after the existing close checks succeed."""
        self._tissue_navigation.reset()
        for name in ("_stage_options_dialog", "_compute_dialog"):
            dialog = getattr(self, name, None)
            if dialog is not None:
                dialog.reject()
                dialog.deleteLater()
        location = getattr(self, "execution_location", None)
        if location is not None:
            location.dispose()
        reviews = getattr(self, "reviews_page", None)
        if reviews is not None:
            reviews.release_jobs()
        for name in ("execution_location", "plan_page", "run_page", "reviews_page", "results_page", "_compute_dialog"):
            if hasattr(self, name):
                delattr(self, name)
        self._initialize_workspace_state()
        self.shell.clear_workspace_pages()
        self._home.set_software_setup_return(False)
        self._home.set_draft(None)

    def set_verified_segmentation_model_directory(self, directory: Path) -> None:
        """Apply a verified default only to forms built after this call."""
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("verified model directory must be absolute")
        self._segmentation_model_directory = directory
        reviews = getattr(self, "reviews_page", None)
        if reviews is not None:
            reviews.set_verified_segmentation_model_directory(directory)

    def attach_samples_page(self, page: SamplesPage) -> None:
        """Connect the installed page to this controller's existing job bridge."""
        if not isinstance(page, SamplesPage):
            raise TypeError("source-series page must be a SamplesPage")
        if self.samples_page is not None:
            raise RuntimeError("a Samples page is already attached")
        if self.source_series_inspection is None:
            raise ValueError("source-series page requires an inspection port")
        self.samples_page = page
        page.source_series_inspection_requested.connect(self._request_source_series_inspection)
        page.paired_image_inspection_requested.connect(self._request_paired_image_inspection)
        page.draft_changed.connect(self._draft_changed)
        self._project_sample_list(page.draft)

    def _new_analysis_request(self, request: NewAnalysisRequest) -> None:
        if not self.shell.is_admitted:
            return
        if type(request) is not NewAnalysisRequest:
            self.operation_failed.emit("new analysis requires a typed request")
            return
        if (
            self.samples_page is not None
            or self.session is not None
            or self._setup_create_job_id is not None
        ):
            self.operation_failed.emit("a workspace is already open")
            return
        if self.source_series_inspection is None:
            self.operation_failed.emit("source-series inspection port is unavailable")
            return
        self._setup_create_job_id = self.jobs.submit(
            "create_analysis_setup",
            lambda: create_analysis_setup(request),
            access=JobAccess.MUTATION,
        )

    def _project_sample_list(self, draft: ManifestDraft) -> None:
        self.shell.set_project_samples(
            tuple(
                (sample.sample_id, sample.source_mif_path.name)
                for sample in draft.samples
                if sample.sample_id
            )
        )

    def _draft_changed(self, draft: ManifestDraft) -> None:
        self._project_sample_list(draft)
        if self._setup is not None:
            self._setup_dirty = self._setup_dirty or draft != self._setup.draft

    def _select_project_sample(self, sample_id: str) -> None:
        if self.samples_page is not None and self.samples_page.select_sample(sample_id):
            self.shell.show_route("samples")

    @property
    def setup_document(self) -> AnalysisSetupDocument | None:
        return self._setup

    def _route_changed(self, route_id: str) -> None:
        if (route_id in {"plan", "run"} and self.samples_page is not None
                and self.samples_page.source_selection_pending):
            self.shell.show_route("samples")
            return
        if route_id != "reviews":
            self._workflow_skip_pending = None
            self._cell_alignment.close()
        location = getattr(self, "execution_location", None)
        if location is not None and location.handle_route(route_id):
            return
        saved_review = self._software_setup_review
        if saved_review is not None:
            page, view = saved_review
            unchanged = getattr(self, "reviews_page", None) is page and page._current_view is view
            if route_id == "home" and unchanged:
                return
            self._software_setup_review = None
            self._home.set_software_setup_return(False)
            if route_id == "reviews" and unchanged:
                page._update_nimbus_setup()
                return
        if route_id != "reviews" and hasattr(self, "reviews_page"):
            self._tissue_navigation.leave_secondary()
        if route_id != "reviews" and getattr(self, "_workflow_review_node_ids", ()):
            self._workflow_review_node_ids = ()
            if hasattr(self, "reviews_page"):
                self.reviews_page.set_workflow_context(None)
        if (
            route_id in {"plan", "run"}
            and self._setup is not None
            and (
                self.session is None
                or (
                    route_id == "plan"
                    and self._prepared_without_execution
                    and self.session.last_plan is not None
                    and any(item.state != "ready" for item in self.session.last_plan.prerequisites)
                )
            )
        ):
            # A software check may have changed since this unstarted plan was
            # prepared. Recheck it before displaying Start analysis again.
            if self._new_job_id is None:
                self._promote_setup()
            return
        if route_id == "results" and self.session is not None:
            self._project_saved_results()
            return
        if route_id == "reviews" and (self._tissue_navigation.visible or self._registration_navigation.pending):
            return
        if route_id != "reviews" or self.session is None:
            return
        if any(
            job_id is not None
            for job_id in (
                self._response_job_id,
                self._pause_job_id,
                self._cancel_job_id,
            )
        ):
            return
        if self.session.last_status is not None:
            self.request_interactions(BatchInteractionQuery())
        elif self._run_job_id is None:
            self.refresh_status()

    def _open_folder(self, folder: Path) -> None:
        if not self.shell.is_admitted:
            return
        if not isinstance(folder, Path) or not folder.is_absolute():
            self.operation_failed.emit("Select an absolute analysis folder")
            return
        if (
            self._close_pending
            or self._folder_load_jobs
            or self._open_job_id is not None
        ):
            return
        job_id = self.jobs.submit(
            "resolve_analysis_folder",
            lambda: resolve_analysis_folder(folder),
            access=JobAccess.READ,
        )
        self._folder_load_jobs.add(job_id)

    def _open_manifest_intent(self, intent: OpenWorkspaceIntent) -> None:
        if not self.shell.is_admitted:
            return
        if not isinstance(intent, OpenWorkspaceIntent):
            self.operation_failed.emit("open workspace requires a manifest path intent")
            return
        if (
            self.samples_page is not None
            or self.session is not None
            or self._open_job_id is not None
            or self._setup_create_job_id is not None
        ):
            self.operation_failed.emit("a workspace is already open or opening")
            return
        if self.source_series_inspection is None:
            self.operation_failed.emit("source-series inspection port is unavailable")
            return
        path = intent.manifest_path
        is_setup = path.name == "analysis.setup.json"
        job_id = self.jobs.submit(
            "load_analysis_setup" if is_setup else "load_manifest_draft",
            (
                (lambda: load_analysis_setup(path))
                if is_setup
                else (lambda: load_manifest_draft(path, existing_analysis=True))
            ),
            access=JobAccess.READ,
            coalesce_key=f"manifest:{path}",
        )
        (self._setup_load_jobs if is_setup else self._manifest_load_jobs)[job_id] = path
        self._latest_manifest_load_job_id = job_id

    def _install_loaded_manifest(
        self, draft: ManifestDraft, *, setup_path: Path | None = None
    ) -> None:
        self._tissue_navigation.reset()
        self._measurement_review.reset()
        self._segmentation_review.reset()
        self._saved_supervised_review.reset()
        samples = SamplesPage(
            draft=draft,
            managed_setup_path=setup_path,
            native_operations=self.file_operations,
        )
        plan = PlanPage()
        run = RunPage()
        reviews = ReviewsTrainingPage(
            segmentation_model_directory=self._segmentation_model_directory,
            jobs=self.jobs,
            response_allowed=lambda: not self._close_pending,
        )
        results = ResultsPage()
        from .execution_flow import ExecutionFlow

        self.execution_location = ExecutionFlow(
            self, draft, setup_path, plan, run, reviews, results
        )
        self.shell.install_route_pages(
            {
                "computing": self.execution_location.computing,
                "samples": samples,
                **self.execution_location.routes,
            }
        )
        self.attach_samples_page(samples)
        if self.execution_location.prepared:
            samples.lock_for_plan()
        elif setup_path is not None:
            samples.inspect_unbound_sources()
        self.plan_page = plan
        self.run_page = run
        self.reviews_page = reviews
        self._workflow_review_node_ids = ()
        self.results_page = results
        samples.validate_requested.connect(self._validate_draft)
        samples.save_requested.connect(
            self._save_setup_request if setup_path is not None else self.new_analysis
        )
        plan.start_requested.connect(lambda _intent: self.start_run())
        plan.open_home_setup_requested.connect(self._open_software_setup)
        run.start_requested.connect(self.start_run)
        run.continue_saved_requested.connect(self.continue_saved_work)
        run.stage_options_requested.connect(self.open_stage_options)
        run.review_nodes_requested.connect(self.open_workflow_review)
        run.skip_setup_requested.connect(self.skip_optional_setup)
        run.measurement_review_requested.connect(self._measurement_review.open)
        run.segmentation_review_requested.connect(self._segmentation_review.open)
        run.results_requested.connect(lambda: self.shell.show_route("results"))
        run.software_setup_requested.connect(self._open_software_setup)
        run.image_appearance_requested.connect(self.open_image_appearance)
        reviews.back_to_workflow_requested.connect(self.return_to_workflow)
        reviews.tissue_settings_back_requested.connect(self._tissue_navigation.back)
        reviews.secondary_definition_back_requested.connect(self._tissue_navigation.back_to_definition)
        reviews.software_setup_requested.connect(self._open_review_software_setup)
        reviews.workflow_review_requested.connect(self.open_workflow_review)
        reviews.acquisition_cycle_review_requested.connect(self.open_acquisition_cycle_options)
        reviews.directory_requested.connect(self.open_review_directory)
        reviews.export_library_requested.connect(self.export_reference_library)
        reviews.results_requested.connect(lambda: self.shell.show_route("results"))
        from .compute_workbench import restore_compute_preference

        restore_compute_preference()
        if self.execution_location.valid and self.execution_location.settings_path.exists():
            from os import environ

            environ["MIF_COMPUTE_DEVICE"] = self.execution_location.settings["device"]
            if "precision" in self.execution_location.settings:
                environ["MIF_COMPUTE_PRECISION"] = self.execution_location.settings["precision"]
            else:
                environ.pop("MIF_COMPUTE_PRECISION", None)
            environ["MIF_SPECTRAL_PRECISION"] = "device_selected"
        plan.compute_requested.connect(self.open_compute_workbench)
        run.refresh_requested.connect(lambda: self.refresh_status(refresh_retry_choices=True))
        run.pause_requested.connect(self.request_pause)
        run.cancel_requested.connect(self.request_cancel)
        run.resume_requested.connect(self.resume)
        run.retry_requested.connect(self.retry_step)
        run.segmentation_recovery_requested.connect(self.recover_interrupted_segmentation)
        reviews.page_requested.connect(self.request_interactions)
        reviews.response_requested.connect(self.apply_review_response)
        reviews.review_plots_requested.connect(self.request_review_plots)
        reviews.prediction_page_requested.connect(self.request_prediction_page)
        reviews.cutoff_assignment_requested.connect(self.assign_supervised_cutoff)
        reviews.scoped_cutoff_available = lambda: (self.session is not None and
            callable(getattr(self.session.service, 'assign_supervised_cutoff', None)))
        reviews.combination_preview_available = True
        reviews.marker_combination_preview_requested.connect(self.request_combination_preview)
        reviews.secondary_marker_requested.connect(self.request_secondary_marker)
        reviews.kronos_cell_preview_requested.connect(self.request_kronos_cell_preview)
        reviews.marker_field_requested.connect(self.request_marker_field)
        reviews.cluster_model_requested.connect(self.request_cluster_model)
        reviews.cluster_refit_requested.connect(self.open_cluster_refit_options)
        reviews.open_qupath_requested.connect(self.open_qupath)
        reviews.feature_inspection_requested.connect(self.inspect_image_features)
        reviews.reference_regions_requested.connect(self.reference_region_action)
        reviews.reference_component_requested.connect(self.inspect_reference_component)
        reviews.reference_preparation_requested.connect(self.inspect_reference_component_preparation)
        reviews.reference_preparation_regions_requested.connect(self.reference_preparation_region_action)
        reviews.reference_control_requested.connect(self.inspect_reference_control_preparation)
        for page in (run, results):
            page.open_qupath_requested.connect(self.open_saved_qupath)
        reviews.open_plot_requested.connect(self.open_plot)
        results.open_plot_requested.connect(self.open_plot)
        results.saved_figure_requested.connect(self.request_saved_figure)
        results.measurement_field_requested.connect(self.request_measurement_field)
        results.saved_region_requested.connect(self.select_saved_region)
        results.supervised_review_requested.connect(self._saved_supervised_review.open)
        results.segmentation_review_requested.connect(self._segmentation_review.open_saved)
        results.reveal_path_requested.connect(self.reveal_path)
        results.spatial_requested.connect(self.run_spatial)
        results.patient_requested.connect(self.run_patient)
        self._home.set_draft(draft)
        self.shell.set_workspace_context(draft.batch_id, None)

    def _open_software_setup(self) -> None:
        self._software_setup_review = None
        self.shell.show_route("home")
        self._home.show_software_setup()

    def _open_review_software_setup(self) -> None:
        page = getattr(self, "reviews_page", None)
        if (page is None or page.remote_execution or page._current_view is None
                or self._close_pending or self._response_job_id is not None):
            return
        self._software_setup_review = (page, page._current_view)
        self.shell.show_route("home")
        self._home.show_software_setup(return_to_review=True, component="nimbus")

    def _return_from_software_setup(self) -> None:
        if self._software_setup_review is not None:
            self.shell.show_route("reviews")

    def _install_setup(self, setup: AnalysisSetupDocument) -> None:
        self._setup = setup
        self._setup_dirty = False
        self._prepared_manifest_path = None
        self._install_loaded_manifest(setup.draft, setup_path=setup.path)
        self.shell.show_route("computing")

    def _save_setup_request(self, draft: ManifestDraft, path: Path) -> None:
        setup = self._setup
        if setup is None or path != setup.path:
            self.operation_failed.emit("analysis setup location is unavailable")
            return
        if self._setup_save_job_id is not None:
            return
        if self._prepared_manifest_path is not None:
            self.operation_failed.emit("analysis setup is locked after manifest publication")
            return
        try:
            self._submit_setup_save()
        except RuntimeError as exc:
            self.operation_failed.emit(str(exc))

    def _submit_setup_save(self) -> None:
        setup = self._setup
        page = self.samples_page
        if setup is None or page is None:
            return
        document = replace(setup, draft=page.draft)
        self._setup_save_document = document
        self._setup_save_job_id = self.jobs.submit(
            "save_analysis_setup",
            lambda: save_analysis_setup(document),
            access=JobAccess.MUTATION,
        )

    def _promote_setup(self) -> None:
        if self.samples_page is None or not self.samples_page.commit_pending() or self.samples_page.source_selection_pending:
            self.shell.show_route("samples")
            return
        if self._prepared_manifest_path is not None:
            if self._setup_dirty:
                self.operation_failed.emit("analysis setup changed after manifest publication")
                self.shell.show_route("samples")
                return
            try:
                self._submit_setup_service_plan(self._prepared_manifest_path)
            except RuntimeError as exc:
                self.operation_failed.emit(str(exc))
            return
        if self._new_job_id is not None or self._setup_manifest_job_id is not None:
            return
        if self._setup_save_job_id is not None:
            self._promote_after_save = True
            return
        self._promote_after_save = True
        try:
            self._submit_setup_save()
        except RuntimeError as exc:
            self._promote_after_save = False
            self.shell.show_route("samples")
            self.operation_failed.emit(str(exc))

    def _submit_setup_promotion(self) -> None:
        setup = self._setup
        if setup is None:
            return
        if self._prepared_manifest_path is not None:
            self._submit_setup_service_plan(self._prepared_manifest_path)
            return

        def publish_or_reuse() -> Path:
            try:
                return prepare_analysis_manifest(setup)
            except FileExistsError:
                validation = validate_manifest_draft(
                    setup.draft,
                    analysis_config=setup.analysis_settings,
                )
                if not validation.is_valid or validation.validated_manifest is None:
                    raise ValueError(
                        "existing run manifest cannot be authenticated against this setup"
                    )
                path = setup.path.parent / "analysis.manifest.json"
                if (
                    path.is_symlink()
                    or path.read_bytes() != validation.validated_manifest.normalized_manifest_bytes
                ):
                    raise ValueError("existing run manifest differs from this setup")
                return path

        self._setup_manifest_job_id = self.jobs.submit(
            "prepare_analysis_manifest",
            publish_or_reuse,
            access=JobAccess.MUTATION,
        )

    def _submit_setup_service_plan(self, manifest_path: Path) -> None:
        setup = self._setup
        assert setup is not None

        def plan() -> _PreparedWorkspace:
            service = self.batch_services.for_manifest(manifest_path)
            validation = service.validate(manifest_path)
            prepared = service.plan(manifest_path)
            status = service.execution_status_if_present(setup.draft.batch_id)
            return _PreparedWorkspace(
                manifest_path,
                setup.draft,
                service,
                validation,
                prepared,
                status,
            )

        self._new_job_id = self.jobs.submit(
            "plan_analysis",
            plan,
            access=JobAccess.MUTATION,
        )

    def _display_run_context(self, batch_id: str) -> None:
        """Mirror the Run page's already-rendered public status text."""

        status = self.run_page.findChild(QLabel, "run-state")
        assert status is not None
        self.shell.set_workspace_context(batch_id, status.text().splitlines()[0])

    def _validate_draft(self, draft: ManifestDraft) -> None:
        if type(draft) is not ManifestDraft:
            raise TypeError("validation requires a ManifestDraft")
        setup = self._setup
        if setup is not None and setup.analysis_settings is None:
            self.operation_failed.emit("analysis settings are incomplete")
            return
        job_id = self.jobs.submit(
            "validate_manifest_draft",
            lambda: (
                validate_manifest_draft(
                    draft,
                    analysis_config=setup.analysis_settings,
                )
                if setup is not None
                else validate_manifest_draft(draft)
            ),
            access=JobAccess.READ,
        )
        self._validation_jobs[job_id] = draft

    def new_analysis(self, draft: ManifestDraft, manifest_path: Path) -> None:
        """Persist a new manifest and use one provider-issued service for it."""
        if not self.shell.is_admitted:
            raise RuntimeError("Complete software setup and restart the app if requested.")
        if self.samples_page is not None and self.samples_page.source_selection_pending:
            self.shell.show_route("samples")
            return
        if type(draft) is not ManifestDraft or not isinstance(manifest_path, Path):
            raise TypeError("new analysis requires a draft and manifest Path")
        if (
            self.session is not None
            or self._open_job_id is not None
            or self._new_job_id is not None
        ):
            raise RuntimeError("a workspace is already open or opening")
        path = manifest_path.expanduser().resolve(strict=False)
        location = getattr(self, "execution_location", None)
        if location is not None and location.mode != "local":
            location.save_workspace_draft(draft, path)
            return

        def prepare() -> _PreparedWorkspace:
            written = write_manifest_draft(draft, path)
            service = self.batch_services.for_manifest(written)
            validation = service.validate(written)
            plan = service.plan(written)
            status = service.execution_status_if_present(draft.batch_id)
            return _PreparedWorkspace(written, draft, service, validation, plan, status)

        self._new_job_id = self.jobs.submit("new_analysis", prepare, access=JobAccess.MUTATION)

    def _source_sample_is_current(self, sample_id: str, source_path: Path) -> bool:
        page = self.samples_page
        return page is not None and any(
            sample.sample_id == sample_id and sample.source_mif_path == source_path
            for sample in page.draft.samples
        )

    def _request_source_series_inspection(self, sample_id: str, source_path: Path) -> None:
        port = self.source_series_inspection
        if port is None or not self._source_sample_is_current(sample_id, source_path):
            return
        key = "source-series:" + json.dumps([sample_id, str(source_path)], separators=(",", ":"))
        job_id = self.jobs.submit(
            "inspect_source_series",
            lambda: port.inspect_source_series(source_path),
            access=JobAccess.READ,
            coalesce_key=key,
        )
        self._source_series_jobs[job_id] = (sample_id, source_path)

    def _request_paired_image_inspection(self, request: str, source_path: Path) -> None:
        port, page = self.source_series_inspection, self.samples_page
        if port is None or page is None or not page.paired_inspection_is_current(request, source_path):
            return
        job_id = self.jobs.submit(
            "inspect_source_series",
            lambda: port.inspect_source_series(source_path, require_planar_channels=False),
            access=JobAccess.READ,
            coalesce_key=f"paired-image:{id(page)}:{request}",
        )
        self._paired_image_jobs[job_id] = (page, request, source_path)

    def open_workspace(
        self, output_root: Path, batch_id: str, *, manifest_path: Path | None = None
    ) -> None:
        """Resolve one service and read its typed status on a managed worker."""

        if not self.shell.is_admitted:
            raise RuntimeError("Complete software setup and restart the app if requested.")
        if not isinstance(output_root, Path):
            raise TypeError("output_root must be a Path")
        if type(batch_id) is not str or not batch_id.strip():
            raise ValueError("batch_id must be non-empty text")
        if self.session is not None or self._open_job_id is not None:
            raise RuntimeError("a workspace is already open or opening")

        def resolve_and_read() -> _OpenedWorkspace | _PreparedWorkspace:
            root = output_root.expanduser().resolve(strict=False)
            service = (
                self.batch_services.for_manifest(manifest_path)
                if manifest_path is not None
                else self.batch_services.for_workspace(root, batch_id)
            )
            status = service.execution_status_if_present(batch_id)
            if status is None and manifest_path is not None:
                draft = load_manifest_draft(manifest_path)
                validation = service.validate(manifest_path)
                plan = service.plan(manifest_path)
                status = service.execution_status_if_present(batch_id)
                return _PreparedWorkspace(manifest_path, draft, service, validation, plan, status)
            draft = None if status is None else manifest_draft_from_manifest(
                service.current_manifest(batch_id, expected_head_sha256=status.state_head_sha256),
                manifest_path or root / batch_id / "analysis.manifest.json")
            plan = None if status is None else service.current_plan(
                batch_id, expected_head_sha256=status.state_head_sha256)
            return _OpenedWorkspace(root, batch_id, service, status, draft, plan)

        self._open_job_id = self.jobs.submit(
            "open_workspace",
            resolve_and_read,
            access=JobAccess.READ,
        )

    def request_close(self, *, open_after_close: OpenWorkspaceIntent | None = None) -> None:
        """Save and pause this analysis, then close the window or open a selected analysis."""
        if self._close_pending:
            return
        location = getattr(self, "execution_location", None)
        if location is not None and not location.may_close():
            return
        self._continue_session = None
        if self._close_pending:
            return
        self._open_after_close = open_after_close
        self._setup_discard_offer = None
        self._discard_setup_on_close = None
        if self._setup is not None and self.samples_page is not None:
            if not self.samples_page.commit_pending():
                self._offer_setup_discard("The current sample edit could not be saved.")
                return
        self._close_pending = True
        self._cell_alignment.close()
        self._registration_navigation.close()
        self.shell.statusBar().showMessage(
            "Closing after active work reaches a safe stopping point…"
            if self.jobs.has_active_jobs
            or (self.session is not None and not self._prepared_without_execution)
            else "Closing…"
        )
        self._advance_close()

    def _offer_setup_discard(self, reason: str) -> None:
        setup, page = self._setup, self.samples_page
        if setup is None or page is None:
            self.operation_failed.emit(reason)
            return
        self._close_pending = False
        self._discard_setup_on_close = None
        self.shell.statusBar().clearMessage()
        request = SetupDiscardCloseRequest(uuid4().hex, reason)
        self._setup_discard_offer = _SetupDiscardOffer(request, setup, page, page.draft)
        self.discard_setup_close_requested.emit(request)

    def _setup_discard_is_current(self, offer: _SetupDiscardOffer) -> bool:
        return (
            self._setup is offer.setup
            and self.samples_page is offer.page
            and offer.page.draft is offer.draft
        )

    def cancel_discard_setup_close(self, request_id: str) -> None:
        offer = self._setup_discard_offer
        if offer is not None and offer.request.request_id == request_id:
            self._setup_discard_offer = None
            self._open_after_close = None

    def confirm_discard_setup_close(self, request_id: str) -> None:
        """Discard one exact setup revision after explicit operator choice."""
        offer = self._setup_discard_offer
        if offer is None or offer.request.request_id != request_id:
            return
        self._setup_discard_offer = None
        if not self._setup_discard_is_current(offer):
            self.operation_failed.emit("The setup changed. Review it before closing again.")
            return
        if self.jobs.has_active_jobs:
            self.operation_failed.emit(
                "Current work is still finishing. Close again after it ends."
            )
            return
        self._discard_setup_on_close = offer
        self._close_pending = True
        self.shell.statusBar().showMessage(
            "Closing after active work reaches a safe stopping point…"
        )
        self._advance_close()

    def start_run(self) -> None:
        """Start the manifest through the workspace's single service."""
        location = getattr(self, "execution_location", None)
        if location is not None and location.mode != "local":
            location.start_cluster()
            return
        session = self._mutable_session()
        if session.manifest_path is None:
            raise RuntimeError("running requires a saved manifest")
        if self._run_job_id is not None:
            raise RuntimeError("a workspace run is already active")
        manifest_path = session.manifest_path
        self._begin_mutation(session)
        self._terminal_status_job_id = None
        self._pending_run_failure = None
        self._status_epoch += 1
        self._run_job_id = self.jobs.submit(
            "run", lambda: session.service.run(manifest_path), access=JobAccess.RUN
        )
        self._prepared_without_execution = False
        self.refresh_status()

    def resume(self) -> None:
        """Resume the current workspace after explicit operator intent."""
        session = self._mutable_session()
        if (
            session.last_status is None
            or session.last_status.resume_disposition != "paused_resumable"
        ):
            raise RuntimeError("resume requires a current resumable status")
        self._begin_mutation(session)
        self._terminal_status_job_id = None
        self._pending_run_failure = None
        self._status_epoch += 1
        self._run_job_id = self.jobs.submit(
            "resume",
            lambda: session.service.resume(session.batch_id),
            access=JobAccess.RUN,
        )
        self.refresh_status()



    def open_stage_options(self, node_ids: tuple[str, ...]) -> None:
        session = self._require_session()
        if session is None or session.last_status is None or self.jobs.has_active_jobs or self._close_pending:
            return
        head = session.last_status.state_head_sha256
        service = session.service
        job_id = self.jobs.submit("stage_options",
            lambda: service.stage_option_targets(session.batch_id, node_ids=node_ids, expected_head_sha256=head), access=JobAccess.READ)
        self._stage_option_jobs[job_id] = ("options", id(session), head, None)

    def open_acquisition_cycle_options(self, review_node_id: str, expected_head: str) -> None:
        """Return the displayed channel review to its own current Inspection options."""
        session = self._require_session()
        if session.last_status is None or self.jobs.has_active_jobs or self._close_pending:
            return
        if session.last_status.state_head_sha256 != expected_head:
            self.operation_failed.emit('The analysis changed. Reopen its current channel review.')
            return
        from mif_pipeline.orchestration.batch_stage_settings import inspection_review_target
        try:
            node_id = inspection_review_target(session.last_status.workflow_nodes, review_node_id)
        except ValueError as error:
            self.operation_failed.emit(str(error))
            return
        job_id = self.jobs.submit('stage_options', lambda: session.service.stage_option_targets(
            session.batch_id, node_ids=(node_id,), expected_head_sha256=expected_head), access=JobAccess.READ)
        self._stage_option_jobs[job_id] = ('inspection-cycles', id(session), expected_head, None)

    def open_cluster_refit_options(self, review_node_id, selected_ids):
        """Open the exact current review's existing clustering revision editor."""
        session=self._require_session()
        if session is None or session.last_status is None or self.jobs.has_active_jobs or self._close_pending:return
        nodes={node.node_id:node for node in session.last_status.workflow_nodes}
        current=nodes.get(review_node_id)
        if current is None or current.kind!='cluster_label_review':
            self.operation_failed.emit('The cluster review changed. Reopen its current review.')
            return
        pending=list(current.dependency_ids);seen=set();fits=[]
        while pending:
            identity=pending.pop()
            if identity in seen:continue
            seen.add(identity);node=nodes.get(identity)
            if node is None:continue
            if node.kind in {'clustering_per_slide','clustering_pooled'}:
                if node.status=='completed':fits.append(node.node_id)
                continue
            pending.extend(node.dependency_ids)
        if len(fits)!=1:
            self.operation_failed.emit('The current fitted memberships are unavailable. Refresh the analysis.')
            return
        head=session.last_status.state_head_sha256
        job_id=self.jobs.submit('stage_options',lambda:session.service.stage_option_targets(
            session.batch_id,node_ids=(fits[0],),expected_head_sha256=head),access=JobAccess.READ)
        self._stage_option_jobs[job_id]=('options',id(session),head,None)
        self._cluster_refit_option_drafts[job_id]=tuple(selected_ids)

    def _inspect_source_options(self, dialog, path) -> None:
        """Reuse the current source-inspection port for one retained correction form."""
        session = self._require_session()
        if session is None or self.source_series_inspection is None:
            dialog.show_error("Image inspection is unavailable. Complete Software setup and reopen this form.")
            return
        if self.jobs.has_active_jobs or dialog is not self._stage_options_dialog:
            dialog.show_error("Wait for the current operation to finish, then inspect the image again.")
            return
        job_id = self.jobs.submit("inspect_source_series",
            lambda: self.source_series_inspection.inspect_source_series(path,
                require_planar_channels=dialog.options.kind != "registration"), access=JobAccess.READ)
        self._stage_option_jobs[job_id] = ("source-inspection", id(session), dialog.options.state_head_sha256, dialog)

    def _preview_stage_options(self, dialog, settings) -> None:
        session = self._require_session()
        if session is None or session.last_status is None or self.jobs.has_active_jobs:
            dialog.show_error("Wait for the current check to finish, then preview these options again.")
            return
        options = dialog.options
        if session.last_status.state_head_sha256 != options.state_head_sha256:
            dialog.show_error("The analysis changed. Close these options and reopen the current task.")
            return
        service = session.service
        job_id = self.jobs.submit("preview_stage_revision",
            lambda: service.preview_stage_revision(session.batch_id, node_id=options.node_id,
                expected_head_sha256=options.state_head_sha256, settings=settings), access=JobAccess.READ)
        self._stage_option_jobs[job_id] = ("preview", id(session), options.state_head_sha256, dialog)

    def _remove_measurement_cells(self, dialog) -> None:
        """Return explicit deletions through the stopped task's existing native draft."""
        session = self._require_session()
        if (session is None or self.file_operations is None or self.jobs.has_active_jobs
                or self._close_pending or dialog is not self._stage_options_dialog):
            return
        options = dialog.options
        if session.current_state_head_sha256 != options.state_head_sha256:
            dialog.show_error("The analysis changed. Reopen the stopped task's options.")
            return
        def invoke():
            project = session.service.saved_project_path(session.batch_id,
                expected_head_sha256=options.state_head_sha256)
            return self.file_operations.edit_local_annotations(session.service, session.batch_id,
                None, options.state_head_sha256, None, project.parent,
                cell_removal_node_id=options.node_id,
                image_appearance_path=Path(session.output_root) / "image-appearance.json")
        job = self.jobs.submit("open_qupath", invoke, access=JobAccess.MUTATION)
        self._stage_option_jobs[job] = ("cell-removals", id(session), options.state_head_sha256, dialog)
        dialog.setEnabled(False)

    def _apply_stage_options(self, dialog, preview) -> None:
        session = self._require_session()
        if session is None or session.last_status is None or self.jobs.has_active_jobs or self._close_pending:
            dialog.show_error("Wait for the current operation to finish before applying options.")
            return
        options = preview.options
        if session.last_status.state_head_sha256 != options.state_head_sha256:
            dialog.show_error("The analysis changed. Reopen its current options before applying a revision.")
            return
        service = session.service
        try:
            job_id = self.jobs.submit("revise_stage",
                lambda: service.revise_stage(session.batch_id, node_id=options.node_id,
                    expected_head_sha256=options.state_head_sha256, settings=preview.settings), access=JobAccess.MUTATION)
        except RuntimeError as exc:
            dialog.show_error(str(exc))
            return
        self._begin_mutation(session)
        self._status_epoch += 1
        self._run_job_id = job_id
        self._stage_revision_jobs[self._run_job_id] = (id(session), dialog)
        dialog.set_applying(True)
        self.refresh_status()

    def recover_interrupted_segmentation(self, selected) -> None:
        session = self._require_session()
        if (selected != self.run_page.interrupted_segmentation_selection()
                or selected is None or self.jobs.has_active_jobs or self._close_pending
                or session.last_status is None or selected[1] != session.last_status.state_head_sha256):
            self.operation_failed.emit("Refresh the analysis before restoring its accepted regions.")
            return
        self._begin_mutation(session)
        self._terminal_status_job_id = None
        self._pending_run_failure = None
        self._status_epoch += 1
        self._run_job_id = self.jobs.submit(
            "restore_interrupted_segmentation",
            lambda: session.service.abandon_interrupted_segmentation(
                session.batch_id, node_id=selected[0], expected_head_sha256=selected[1]),
            access=JobAccess.RUN)
        self.refresh_status()

    def continue_saved_work(self) -> None:
        """Continue the displayed retained attempt without issuing a failed retry."""
        session = self._require_session()
        status = session.last_status
        if (status is None or not status.continuation_available
                or self.jobs.has_active_jobs or self._close_pending):
            self.operation_failed.emit("Refresh the analysis before continuing its saved work.")
            return
        selected_head = status.state_head_sha256
        self._begin_mutation(session)
        self._terminal_status_job_id = None
        self._pending_run_failure = None
        self._status_epoch += 1
        self._run_job_id = self.jobs.submit(
            "continue_run",
            lambda: session.service.continue_run(
                session.batch_id, expected_head_sha256=selected_head,
            ),
            access=JobAccess.RUN,
        )
        self._prepared_without_execution = False
        self.refresh_status()

    def refresh_status(self, *, refresh_retry_choices: bool = False) -> None:
        """Read current status without altering durable workspace state."""
        session = self._require_session()
        if refresh_retry_choices:
            self._invalidate_retry_choices()
        self._retry_status_key = None
        key = "status:" + json.dumps(
            [str(session.output_root), session.batch_id, self._status_epoch], separators=(",", ":")
        )
        job_id = self.jobs.submit(
            "status",
            lambda: session.service.execution_status_if_present(session.batch_id),
            access=JobAccess.READ,
            coalesce_key=key,
        )
        self._status_job_id = job_id
        if self._terminal_status_job_id is not None:
            self._terminal_status_job_id = job_id
        session.begin_status(job_id)

    def _require_session(self) -> WorkspaceSession:
        if self.session is None:
            raise RuntimeError("no workspace is open")
        return self.session

    def _begin_mutation(self, session: WorkspaceSession) -> None:
        self._invalidate_retry_choices()
        session.begin_mutation()
        if hasattr(self, "run_page"):
            self.run_page.invalidate_actions()
        self._saved_results_key = None
        self._bound_routes = None
        if hasattr(self, "results_page"):
            self.results_page.clear_results("Saved results will refresh when the current operation finishes.")

    def _invalidate_retry_choices(self) -> None:
        self._retry_generation += 1
        self._retry_status_key = self._retry_cache_key = None
        self._retry_choices = ()
        if hasattr(self, "run_page"):
            self.run_page.set_retry_choices(())

    def _retry_context(self) -> tuple[int, int, int, str] | None:
        session = self.session
        if session is None or session.last_status is None:
            return None
        return (
            id(session),
            self._status_epoch,
            self._retry_generation,
            session.last_status.state_head_sha256,
        )

    def _refresh_retry_choices(self) -> None:
        """Cache an authority-free listing only after an accepted idle status."""
        if not hasattr(self, "run_page"):
            return
        session = self.session
        key = self._retry_context()
        if (
            self.jobs.has_active_jobs
            or self._close_pending
            or session is None
            or key is None
            or key != self._retry_status_key
            or any(
                job is not None
                for job in (
                    self._open_job_id,
                    self._new_job_id,
                    self._run_job_id,
                    self._status_job_id,
                    self._pause_job_id,
                    self._cancel_job_id,
                    self._response_job_id,
                )
            )
        ):
            self.run_page.set_retry_choices(())
            return
        status = session.last_status
        if status.execution_state not in {"active", "paused"} or "failed" not in status.node_status_by_id.values():
            self.run_page.set_retry_choices(())
            return
        if self._retry_cache_key == key:
            self.run_page.set_retry_choices(self._retry_choices)
        elif not self._retry_choice_jobs:
            self._retry_choices = ()
            self.run_page.set_retry_choices(())
            job_id = self.jobs.submit(
                "retry_choices",
                lambda: session.service.retry_choices(
                    session.batch_id,
                    expected_head_sha256=key[3],
                ),
                access=JobAccess.READ,
            )
            self._retry_choice_jobs[job_id] = key

    def retry_step(self, choice: BatchRetryChoice) -> None:
        """Submit this exact current offer, including from a diagnostic view."""
        key = self._retry_context()
        if (
            type(choice) is not BatchRetryChoice
            or key is None
            or key != self._retry_status_key
            or key != self._retry_cache_key
            or choice not in self._retry_choices
            or self.jobs.has_active_jobs
            or self._close_pending
        ):
            self.operation_failed.emit(
                "The retry selection is no longer current. Refresh and choose the step again."
            )
            return
        session = self._require_session()
        self._begin_mutation(session)
        if choice.retry_reason == "input_required":
            self._continue_session = session
            self._continue_accepted = True
            self._continue_origin_head = choice.state_head_sha256
        self._terminal_status_job_id = None
        self._pending_run_failure = None
        self._status_epoch += 1
        self._run_job_id = self.jobs.submit(
            "retry",
            lambda: session.service.retry(
                session.batch_id,
                node_id=choice.node_id,
                expected_head_sha256=choice.state_head_sha256,
                selected_failed_terminal_record_sha256=choice.terminal_record_sha256,
                selected_pending_authorization_sha256=choice.pending_authorization_sha256,
            ),
            access=JobAccess.RUN,
        )
        self._prepared_without_execution = False
        self.refresh_status()

    def _mutable_session(self) -> WorkspaceSession:
        session = self._require_session()
        if session.exit_state.name == "DIAGNOSTIC_ONLY":
            raise RuntimeError("workspace is diagnostic only")
        return session

    def request_pause(self) -> None:
        """Request one durable pause on the exact active workspace service."""
        self._continue_session = None
        session = self._mutable_session()
        if self._pause_job_id is not None:
            return
        self._invalidate_retry_choices()
        if self._run_job_id is not None:
            job_id = self.jobs.submit_control(
                self._run_job_id,
                "pause",
                lambda: session.service.pause(session.batch_id),
            )
        else:
            job_id = self.jobs.submit(
                "pause",
                lambda: session.service.pause(session.batch_id),
                access=JobAccess.MUTATION,
            )
        self._pause_job_id = job_id
        session.begin_pause(job_id)

    def request_cancel(self) -> None:
        """Delegate cancellation only to the exact active run worker."""
        self._continue_session = None
        session = self._mutable_session()
        if self._run_job_id is None:
            raise RuntimeError("cancellation requires an active workspace run")
        if self._cancel_job_id is not None:
            return
        job_id = self.jobs.submit_control(
            self._run_job_id,
            "cancel",
            lambda: session.service.cancel(session.batch_id),
        )
        self._cancel_job_id = job_id
        self._begin_mutation(session)
        self._status_epoch += 1

    def skip_optional_setup(self, node_ids: tuple[str, ...]) -> None:
        """Open the current setup authority, then submit its existing skip response."""
        from ..presenters.workflow import optional_setup_skip_label
        session = self.session
        status = session.last_status if session is not None else None
        if (self._close_pending or status is None or type(node_ids) is not tuple
                or len(node_ids) != 1 or status.resume_disposition != "paused_resumable"
                or any(job is not None for job in (self._run_job_id, self._response_job_id,
                                                  self._pause_job_id, self._cancel_job_id))):
            return
        node = next((node for node in status.workflow_nodes if node.node_id == node_ids[0]), None)
        if node is None or optional_setup_skip_label(node) is None:
            return
        self.open_workflow_review(node_ids)
        if self._workflow_review_node_ids == node_ids:
            self._workflow_skip_pending = (session, status.state_head_sha256, self._status_epoch, node_ids[0])

    def open_workflow_review(self, node_ids: tuple[str, ...]) -> None:
        self._workflow_skip_pending = None
        session = self.session
        status = session.last_status if session is not None else None
        if (
            type(node_ids) is not tuple
            or not node_ids
            or status is None
            or status.resume_disposition != "paused_resumable"
            or self._run_job_id is not None
            or self._response_job_id is not None
        ):
            return
        current = {
            node.node_id: node
            for node in status.workflow_nodes
            if node.status == "needs_interaction" and not node.deferred
        }
        if any(node_id not in current for node_id in node_ids):
            self.operation_failed.emit(
                "This step changed. Refresh the workflow before opening its form."
            )
            return
        from ..presenters.workflow import BY_KIND

        definitions = {BY_KIND.get(current[node_id].kind) for node_id in node_ids}
        if len(definitions) != 1 or None in definitions:
            self.operation_failed.emit("Select one workflow step to open its required form.")
            return
        first_count_node = None
        if all(current[key].interaction_reason == "primary_structure_count_review_required" for key in node_ids):
            first_count_node = node_ids[0]
            node_ids = tuple(node.node_id for node in current.values()
                             if node.interaction_reason == "primary_structure_count_review_required"
                             and node.kind == current[node_ids[0]].kind)
        self._workflow_review_node_ids = node_ids
        self.reviews_page.set_workflow_context(next(iter(definitions)).title)
        if first_count_node is not None:
            self.reviews_page.select_pending_count_node(first_count_node)
        was_reviews = self.shell.current_route_id == "reviews"
        self.shell.show_route("reviews")
        if was_reviews:
            self.request_interactions(BatchInteractionQuery())

    def open_review_directory(self) -> None:
        self._workflow_review_node_ids = ()
        self.reviews_page.set_workflow_context(None)
        was_reviews = self.shell.current_route_id == "reviews"
        self.shell.show_route("reviews")
        if was_reviews:
            self.request_interactions(BatchInteractionQuery())

    def return_to_workflow(self) -> None:
        # An already queued completion callback must respect explicit navigation.
        # Saved responses and retained editor drafts remain available to continue.
        self._continue_session = None
        self._tissue_navigation.leave_secondary(discard=True)
        self._workflow_review_node_ids = ()
        self.reviews_page.set_workflow_context(None)
        self.shell.show_route("run")

    def request_interactions(self, query: BatchInteractionQuery) -> None:
        if self._tissue_navigation.visible and self._tissue_navigation.pending_node_id is None:
            return
        session = self._require_session()
        if type(query) is not BatchInteractionQuery:
            raise TypeError("interaction query must be typed")
        if self._workflow_review_node_ids:
            query = replace(query, node_ids=self._workflow_review_node_ids)
        head = session.current_state_head_sha256
        if head is None:
            raise RuntimeError("interaction query requires a current status")
        job_id = self.jobs.submit(
            "list_interactions",
            lambda: session.service.list_interactions(session.batch_id, query),
            access=JobAccess.READ,
            coalesce_key="interactions:"
            + json.dumps(
                [
                    str(session.output_root),
                    session.batch_id,
                    head,
                    query.offset,
                    query.limit,
                    query.validation_domain_ids,
                    query.interaction_reasons,
                    query.node_ids,
                ],
                separators=(",", ":"),
            ),
        )
        self._interaction_jobs[job_id] = (query, head, self._status_epoch)

    def request_review_plots(self, request_id: str, head: str, options=None) -> None:
        session = self.session
        reviews = getattr(self, "reviews_page", None)
        if (
            session is None
            or reviews is None
            or self._close_pending
            or session.current_state_head_sha256 != head
        ):
            return
        identity = reviews.current_review_plot_identity()
        if identity is None or identity[:2] != (request_id, head):
            return
        epoch = self._status_epoch
        job_id = self.jobs.submit(
            "review_plots",
            lambda: session.service.review_plots(
                session.batch_id,
                interaction_request_id=request_id,
                expected_state_head_sha256=head,
                **(options or {}),
            ),
            access=JobAccess.RUN if options and options.get("umap_settings") else JobAccess.READ,
            coalesce_key="review_plots:"
            + json.dumps(
                [id(session), str(session.output_root), session.batch_id, epoch, head, request_id, options],
                separators=(",", ":"),
            ),
        )
        self._review_plot_jobs[job_id] = (session, epoch, request_id, head, options)

    def request_prediction_page(self, request_id, head, settings):
        """Read one visible score page; the panel serializes and bounds requests."""
        session, page = self.session, self.reviews_page
        panel = page.prediction_panel()
        identity = page.current_review_plot_identity()
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or panel is None or identity is None or identity[:2] != (request_id, head)):
            return
        job = self.jobs.submit('supervised_prediction_page',
            lambda: session.service.supervised_prediction_page(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head, settings=settings),
            access=JobAccess.READ)
        self._prediction_page_jobs[job] = (session, self._status_epoch, identity, panel, settings)

    def request_cluster_model(self, request_id, head):
        session, page = self.session, self.reviews_page
        panel = page._renderer._cluster_review
        identity = page.current_review_plot_identity()
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or panel is None or panel.model_view is None
                or identity is None or identity[:2] != (request_id, head)):
            return
        job = self.jobs.submit('cluster_model_view',
            lambda: session.service.cluster_model_view(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head),
            access=JobAccess.READ)
        self._cluster_model_jobs[job] = (session, self._status_epoch, identity, panel)

    def _current_cluster_model(self, request):
        session, epoch, identity, panel = request
        return (self.session is session and epoch == self._status_epoch
                and session.current_state_head_sha256 == identity[1]
                and self.reviews_page.current_review_plot_identity() == identity
                and self.reviews_page._renderer._cluster_review is panel)

    def request_marker_field(self, request_id, head, options):
        session, page = self.session, self.reviews_page
        field = page.current_marker_field()
        identity = page.current_review_plot_identity()
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or field is None or field.isHidden() or identity is None or identity[:2] != (request_id, head)):
            return
        request = (session, self._status_epoch, identity, field, field.revision)
        if self._marker_field_jobs:
            self._marker_field_pending = (request, options)
            return
        read_field = session.service.cluster_field if 'cluster_id' in options else session.service.marker_field
        job = self.jobs.submit('marker_field',
            lambda: read_field(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head, figure_options=options),
            access=JobAccess.READ)
        self._marker_field_jobs[job] = request

    def _current_marker_field(self, request):
        session, epoch, identity, field, revision = request
        return (self.session is session and epoch == self._status_epoch
                and session.current_state_head_sha256 == identity[1]
                and self.reviews_page.current_review_plot_identity() == identity
                and self.reviews_page.current_marker_field() is field
                and not field.isHidden() and field.revision == revision)

    def _next_marker_field(self):
        pending = self._marker_field_pending
        self._marker_field_pending = None
        if pending is not None and self._current_marker_field(pending[0]):
            identity = pending[0][2]
            self.request_marker_field(identity[0], identity[1], pending[1])

    def _current_prediction_page(self, request):
        session, epoch, identity, panel, _settings = request
        return (self.session is session and epoch == self._status_epoch
                and session.current_state_head_sha256 == identity[1]
                and self.reviews_page.current_review_plot_identity() == identity
                and self.reviews_page.prediction_panel() is panel)

    def _bind_cell_alignment(self):
        """Attach the accepted H&E reader to the loaded cell or cluster field."""
        from .supervised_prediction_review import SupervisedPredictionReview
        from .cluster_field_review import ClusterFieldReview
        page, session = self.reviews_page, self.session
        field = page.current_marker_field()
        panel = page._renderer.findChild(SupervisedPredictionReview)
        gallery = field if isinstance(field, ClusterFieldReview) else panel._viewer if panel is not None else None
        if (session is None or gallery is None or gallery.isHidden()
                or getattr(page, "remote_execution", False)
                or not callable(getattr(self.file_operations, "run_registration_editor", None))):
            self._cell_alignment.close()
            return
        head, identity = session.current_state_head_sha256, page.current_review_plot_identity()
        def current():
            return (self.session is session and session.current_state_head_sha256 == head
                    and self.reviews_page is page and page.current_review_plot_identity() == identity
                    and self.shell.current_route_id == "reviews" and not gallery.isHidden()
                    and (page.current_marker_field() is gallery if isinstance(gallery, ClusterFieldReview)
                         else panel._viewer is gallery))
        self._cell_alignment.bind(gallery.cell_view, (session, head, identity), current)

    def request_kronos_cell_preview(self, request_id, head, settings, receiver):
        session = self.session
        if session is None or self._close_pending:
            receiver.set_error('Open the current project to inspect an example cell.')
            return
        request = (session, request_id, head, receiver, receiver.revision)
        if not self._current_kronos_cell_preview(request):
            receiver.set_error('This setup request has changed. Reopen it to inspect an example cell.')
            return
        from copy import deepcopy
        settings = deepcopy(settings)
        job = self.jobs.submit('kronos_cell_preview',
            lambda: session.service.kronos_cell_preview(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head, settings=settings),
            access=JobAccess.READ)
        self._kronos_cell_preview_jobs[job] = request

    def _current_kronos_cell_preview(self, request):
        from shiboken6 import isValid
        session, request_id, head, receiver, revision = request
        return (self.session is session and session.current_state_head_sha256 == head
            and isValid(receiver) and receiver.revision == revision
            and self.reviews_page.kronos_cell_preview_context() == (request_id, head)
            and self.reviews_page._renderer.isAncestorOf(receiver))

    def request_secondary_marker(self, request_id, head, kind, options, receiver):
        session = self.session
        if session is None or self._close_pending or kind not in {'review', 'field'}:
            return
        from copy import deepcopy
        options = deepcopy(options)
        request = (session, request_id, head, kind, options, receiver, receiver.revision)
        if not self._current_secondary_marker_request(request):
            return
        service = session.service.secondary_marker_field if kind == 'field' else session.service.secondary_marker_review
        job = self.jobs.submit('secondary_marker_' + kind,
            lambda: service(session.batch_id, interaction_request_id=request_id,
                expected_state_head_sha256=head, options=options), access=JobAccess.READ)
        self._secondary_marker_jobs[job] = request

    def _current_secondary_marker_request(self, request):
        from shiboken6 import isValid
        session, request_id, head, _kind, _options, receiver, revision = request
        return (self.session is session and session.current_state_head_sha256 == head
            and isValid(receiver) and receiver.revision == revision
            and self.reviews_page.secondary_marker_context() == (request_id, head)
            and self.reviews_page._renderer.isAncestorOf(receiver))

    def request_combination_preview(self, request_id, head, definition, pattern_page=None):
        """Calculate a draft's assignments without accepting or refitting it."""
        session = self.session
        if session is None or self._close_pending or session.current_state_head_sha256 != head:
            return
        current = self.reviews_page.combination_preview_context()
        if (current is None or current[:3] != (request_id, head, definition)
                or current[3].pattern_page_options() != pattern_page):
            return
        job_id = self.jobs.submit('marker_combination_preview',
            lambda: session.service.preview_marker_combinations(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head,
                phenotype_definition=definition, pattern_page=pattern_page), access=JobAccess.READ)
        self._combination_preview_jobs[job_id] = (session, *current[:3], pattern_page, current[3])

    def _current_combination_preview(self, request):
        session, request_id, head, definition, pattern_page, editor = request
        if self.session is not session or session.current_state_head_sha256 != head:
            return False
        try:
            current = self.reviews_page.combination_preview_context()
        except (TypeError, ValueError):
            return False
        return (current is not None and current[:3] == (request_id, head, definition)
                and current[3] is editor and editor.pattern_page_options() == pattern_page)

    def _current_review_plot_request(
        self,
        request: tuple[WorkspaceSession, int, str, str, object],
    ) -> bool:
        session, epoch, request_id, head, options = request
        reviews = getattr(self, "reviews_page", None)
        identity = reviews.current_review_plot_identity() if reviews is not None else None
        return (
            self.session is session
            and epoch == self._status_epoch
            and session.current_state_head_sha256 == head
            and identity is not None
            and identity[:2] == (request_id, head)
            and reviews.review_plot_options() == options
        )

    def apply_review_response(self, request: BatchInteractionResponseRequest) -> None:
        """Consume this form's explicit save/continue choice without changing its data."""
        session = self.session
        identity = self.reviews_page.current_review_plot_identity()
        def current():
            return (self.session is session and session is not None
                    and session.current_state_head_sha256 == request.expected_state_head_sha256
                    and self.reviews_page.current_review_plot_identity() == identity)
        if self._cell_alignment.finish_then(lambda: self.apply_review_response(request), current=current):
            return
        continuation = self.reviews_page.take_continuation_intent(request)
        self._continue_session = self.session if continuation else None
        self._continue_accepted = False
        self._continue_origin_head = request.expected_state_head_sha256
        try:
            alignment = self.reviews_page.take_alignment_options(request)
            if alignment is None:
                self.apply_interaction_response(request)
            else:
                self.apply_interaction_response(request, alignment_options=alignment)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._continue_session = None
            self.reviews_page.set_response_failure(str(exc))
            session = self.session
            if session is not None and session.last_interactions is not None:
                # Admission failed before mutation. Restore this unchanged page,
                # including its draft, through the normal response lifecycle.
                self.reviews_page.set_page(
                    session.last_interactions,
                    expected_state_head_sha256=session.current_state_head_sha256,
                )
            self.operation_failed.emit(str(exc))
        except BaseException:
            self._continue_session = None
            raise

    def assign_supervised_cutoff(self, request_id, head, selection, *, artifact_ref_sha256=None):
        """Apply the displayed image/group decision through the saved review owner."""
        session = self.session
        identity = self.reviews_page.current_review_plot_identity() if artifact_ref_sha256 is None else None
        def current():
            if artifact_ref_sha256 is not None:
                return self._saved_supervised_review.current(artifact_ref_sha256, head)
            return (session is not None and self.session is session
                    and session.current_state_head_sha256 == head
                    and self.reviews_page.current_review_plot_identity() == identity
                    and identity is not None and identity[:2] == (request_id, head))
        if not current() or self._close_pending or self.jobs.has_active_jobs:
            return
        if self._cell_alignment.finish_then(
                lambda: self.assign_supervised_cutoff(request_id, head, selection, artifact_ref_sha256=artifact_ref_sha256), current=current):
            return
        panel = self._saved_supervised_review.panel if artifact_ref_sha256 is not None else self.reviews_page.prediction_panel()
        if panel is None:
            return
        owner = ({'artifact_ref_sha256': artifact_ref_sha256} if artifact_ref_sha256 is not None
                 else {'interaction_request_id': request_id})
        job_id = self.jobs.submit('assign_supervised_cutoff',
            lambda: session.service.assign_supervised_cutoff(session.batch_id,
                **owner, expected_state_head_sha256=head,
                selection=dict(selection)), access=JobAccess.MUTATION)
        self._response_job_id = self._cutoff_assignment_job_id = job_id
        self._continue_session = session
        self._continue_accepted = False
        self._continue_origin_head = head
        self._cutoff_saved_page = self._saved_supervised_review.page if artifact_ref_sha256 is not None else None
        if panel is not None:
            panel.assign.setEnabled(False)
            panel.status.setText('Applying the cutoff…')
            if artifact_ref_sha256 is not None:
                panel._timer.stop()
                panel.field._timer.stop()
                panel.setEnabled(False)
        self._begin_mutation(session)
        self._status_epoch += 1
        session.last_interactions = None

    def _advance_guided_workflow(self) -> None:
        session = self.session
        if self._continue_session is None:
            return
        if session is not self._continue_session or self._close_pending:
            self._continue_session = None
            return
        if (
            not self._continue_accepted
            or self.jobs.has_active_jobs
            or any(
                job is not None
                for job in (
                    self._response_job_id,
                    self._status_job_id,
                    self._run_job_id,
                    self._pause_job_id,
                    self._cancel_job_id,
                )
            )
        ):
            return
        status = session.last_status
        if status is None or status.state_head_sha256 == self._continue_origin_head:
            return
        if status.resume_disposition == "completed":
            self._continue_session = None
            self.shell.show_route("results")
            return
        if self._tissue_navigation.pending_node_id is not None and status.continuation_available:
            self._continue_origin_head = status.state_head_sha256
            self.continue_saved_work()
            return
        failed = next((n for n in status.workflow_nodes if n.status == "failed"), None)
        if status.resume_disposition != "paused_resumable" or failed is not None:
            self._continue_session = None
            if failed is not None:
                from ..presenters.workflow import BY_KIND

                definition = BY_KIND.get(failed.kind)
                if definition is not None:
                    self.run_page.open_stage(definition.key)
            self.shell.show_route("run")
            return
        if any(n.eligible and not n.deferred for n in status.workflow_nodes):
            self._continue_origin_head = status.state_head_sha256
            self.reviews_page.set_processing("Applying your saved choices…")
            self.shell.show_route("reviews")
            try:
                self.resume()
            except (RuntimeError, TypeError, ValueError) as exc:
                self._continue_session = None
                self.operation_failed.emit(str(exc))
            return
        pending = tuple(
            n.node_id
            for n in status.workflow_nodes
            if n.status == "needs_interaction" and not n.deferred
        )
        self._continue_session = None
        if pending:
            # Continue the selected step's next checkpoint before opening an
            # unrelated pending stage. Preserve owner order when it completes.
            preferred = self._tissue_navigation.pending_node_id
            current = ((preferred,) if preferred in pending else tuple(
                node_id for node_id in self._workflow_review_node_ids if node_id in pending))
            self.open_workflow_review(current or (pending[0],))
        else:
            self.shell.show_route("run")

    def apply_interaction_response(
        self, request: BatchInteractionResponseRequest, *, alignment_options=None
    ) -> None:
        session = self._mutable_session()
        if type(request) is not BatchInteractionResponseRequest:
            raise TypeError("interaction response must be typed")
        job_id = self.jobs.submit(
            "apply_interaction_response",
            (
                (lambda: session.service.apply_interaction_response(session.batch_id, request))
                if alignment_options is None
                else (
                    lambda: session.service.align_paired_image(
                        session.batch_id, request, alignment_options
                    )
                )
            ),
            access=JobAccess.MUTATION,
        )
        # A rejected submission never invalidates the saved status or Run actions.
        self._response_job_id = job_id
        self._begin_mutation(session)
        self._status_epoch += 1
        session.last_interactions = None

    def inspect_reference_component(self, request_id, head, component):
        """Read one accepted spectrum without submitting a workflow response."""
        session = self.session
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or self.jobs.has_active_jobs):
            return
        job = self.jobs.submit('inspect_reference_component',
            lambda: session.service.inspect_reference_component(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head,
                component=component), access=JobAccess.READ)
        self._reference_component_jobs[job] = (session, (request_id, head, component))

    def inspect_reference_component_preparation(self, request_id, head, component, phase):
        """Read the requested saved phase under the current live review fence."""
        session = self.session
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or self.jobs.has_active_jobs):
            return
        job = self.jobs.submit('inspect_reference_component_preparation',
            lambda: session.service.inspect_reference_component_preparation(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head,
                component=component, phase=phase), access=JobAccess.READ)
        self._reference_preparation_jobs[job] = (session, (request_id, head, component, phase))

    def inspect_reference_control_preparation(self, request_id, head, input_id, preparation_sha256):
        """Read one retained AF source without submitting a workflow response."""
        session = self.session
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or self.jobs.has_active_jobs):
            return
        job = self.jobs.submit('inspect_reference_control_preparation',
            lambda: session.service.inspect_reference_control_preparation(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head,
                input_id=input_id, expected_preparation_sha256=preparation_sha256), access=JobAccess.READ)
        self._reference_control_jobs[job] = (session, (request_id, head, input_id, preparation_sha256))

    def _finish_reference_control_inspection(self, job_id, result=None, *, error=None):
        request = self._reference_control_jobs.pop(job_id, None)
        if request is None:
            return False
        origin, identity = request
        if (not self._close_pending and self.session is origin
                and origin.current_state_head_sha256 == identity[1]):
            self.reviews_page.offer_reference_control(identity, result, error=error)
        self._advance_close()
        return True

    def reference_preparation_region_action(self, request_id, head, component, phase, action, snapshot_sha256):
        self.reference_region_action(request_id, head, action, snapshot_sha256, component=component,
            preparation_phase=phase)

    def reference_region_action(self, request_id, head, action, snapshot_sha256, *, component=None,
                                preparation_phase=None):
        """Run the current native-reference action through the shared job and process gates."""
        session = self.session
        if (session is None or self._close_pending or session.current_state_head_sha256 != head
                or self.jobs.has_active_jobs):
            return
        def invoke():
            if action == "edit":
                if self.file_operations is None:
                    raise ValueError("Native QuPath file operations are unavailable.")
                return self.file_operations.edit_spectral_reference_regions(
                    session.service, session.batch_id, request_id, head,
                    **({'component': component, 'preparation_phase': preparation_phase} if component is not None else {}))
            import getpass
            return session.service.reference_region_action(session.batch_id,
                interaction_request_id=request_id, expected_state_head_sha256=head,
                action=action, snapshot_sha256=snapshot_sha256,
                reviewer_id="local-account:" + getpass.getuser(),
                **({'component': component, 'preparation_phase': preparation_phase} if component is not None else {}))
        job = self.jobs.submit("open_qupath" if action == "edit" else "reference_regions",
                               invoke, access=JobAccess.MUTATION)
        self._reference_region_jobs[job] = (session, (request_id, head) if component is None
            else (request_id, head, component, preparation_phase))
        if action == "edit":
            self._qupath_job_id = job

    def _submit_file_operation(
        self, name: str, path: Path | None, *, review_context: bool = True, feature_inspection=None
    ) -> None:
        operations = self.file_operations
        if operations is None:
            self.operation_failed.emit("desktop file operations are unavailable")
            return
        if name == "open_qupath" and self._close_pending:
            self.operation_failed.emit("Close is pending; wait before opening QuPath again.")
            return
        if not (name == "open_qupath" and not review_context and path is None) and (
            not isinstance(path, Path) or not path.is_absolute()
        ):
            self.operation_failed.emit("file operation requires an absolute local path")
            return
        function = getattr(operations, name)
        session = self.session
        expected = session.current_state_head_sha256 if session is not None else None
        reviews = getattr(self, "reviews_page", None) if review_context else None
        display = (
            reviews.reference_display_request()
            if name == "open_qupath" and reviews is not None
            else None
        )
        tissue = (
            reviews.take_tissue_authoring_request()
            if name == "open_qupath" and reviews is not None
            else None
        )
        annotation = (
            reviews.annotation_review_identity()
            if name == "open_qupath" and reviews is not None and tissue is None
            else None
        )
        preview_inputs = reviews.annotation_preview_inputs() if annotation is not None else None
        reference_role = reviews.annotation_reference_role() if annotation is not None else None
        reference_inputs = reviews.annotation_reference_inputs() if annotation is not None else None

        classifier_review = None
        saved_alignment = None
        if name == "open_qupath" and reviews is not None:
            classifier_review = (reviews.classifier_error_review_request()
                                 or reviews.classifier_image_review_request())
        elif name == "open_qupath" and not review_context:
            results = getattr(self, "results_page", None)
            if results is not None:
                classifier_review = results.classifier_error_review_request()
                saved_alignment = results.take_saved_alignment_review_request()
                if saved_alignment is not None:
                    if saved_alignment[0] != expected:
                        self.operation_failed.emit("Refresh Results before inspecting this alignment.")
                        return
                    display = dict(saved_alignment[2])
        if classifier_review is not None:
            # Open the selected fitting image; paired-image menus remain available.
            display = None
        registration_editor = (name == "open_qupath" and display is not None
                               and (display.get("mode") in {"alignment", "candidate_overlay"}
                                    or (display.get("mode") == "overlay" and saved_alignment is not None))
                               and feature_inspection is None and tissue is None)
        if registration_editor and not callable(getattr(operations, "run_registration_editor", None)):
            self.operation_failed.emit("The embedded registration editor is unavailable. Reopen this analysis in the desktop application.")
            return
        if registration_editor:
            annotation = preview_inputs = reference_inputs = None
        appearance_path = (Path(session.output_root) / "image-appearance.json"
                           if name == "open_qupath" and session is not None else None)

        inspection = (reviews.inspection_scope() if name == "open_qupath" and reviews is not None
                      and annotation is None and tissue is None and not registration_editor else {})

        def invoke():
            nonlocal path
            if path is None:
                if session is None or expected is None:
                    raise ValueError("Open an analysis before viewing its saved images.")
                path = session.service.saved_project_path(
                    session.batch_id, expected_head_sha256=expected)
            if feature_inspection is not None:
                return function(path, feature_inspection=feature_inspection, image_appearance_path=appearance_path)
            if annotation is not None and session is not None and expected is not None:
                if annotation[1] != expected:
                    raise ValueError("Refresh this review before opening its annotations.")
                role = reference_role
                if reference_inputs is not None:
                    from mif_pipeline.gui.presenters.reviews import build_annotation_reference_role

                    role = build_annotation_reference_role(*reference_inputs)
                if preview_inputs is not None:
                    from mif_pipeline.gui.presenters.reviews import build_annotation_preview_request

                    preview = build_annotation_preview_request(*preview_inputs, annotation[2])
                    return operations.edit_local_annotations(
                        session.service,
                        session.batch_id,
                        *annotation,
                        path.parent,
                        preview_selection=preview,
                        reference_role=role,
                        image_appearance_path=appearance_path,
                    )
                return operations.edit_local_annotations(
                    session.service, session.batch_id, *annotation, path.parent,
                    reference_role=role,
                    image_appearance_path=appearance_path,
                )
            if tissue is not None:
                return function(path, tissue_authoring=tissue, image_appearance_path=appearance_path)
            if name == "open_qupath" and session is not None and expected is not None:
                if display is None or saved_alignment is not None:
                    session.service.require_current_saved_results(
                        session.batch_id, expected_head_sha256=expected, project_path=path)
                views = session.service.paired_image_views(
                    session.batch_id, expected_head_sha256=expected, project_path=path
                )
                if display is not None:
                    if saved_alignment is not None:
                        if (session.current_state_head_sha256 != expected
                                or results.saved_alignment_review_identity() != saved_alignment[:2]):
                            raise ValueError("The selected result changed. Open the alignment from Results again.")
                        if not any(view["fixed_entry_id"] == display["fixed_entry_id"]
                                   and view["moving_entry_id"] == display["moving_entry_id"] for view in views):
                            raise ValueError("This alignment is no longer an approved paired-image view.")
                    selected_display = dict(display)
                    if selected_display["mode"] == "alignment":
                        from mif_pipeline.registration.automatic import find_elastix_directory

                        try:
                            selected_display["tools_directory"] = str(find_elastix_directory())
                        except (OSError, ValueError):
                            pass
                    if registration_editor:
                        from mif_pipeline.qupath_runtime.registration_editor import create_registration_session
                        return create_registration_session(path, selected_display, paired_views=views,
                                                           appearance_path=appearance_path)
                    return function(path, paired_views=views, reference_display=selected_display, image_appearance_path=appearance_path, classifier_review=classifier_review, inspection=inspection)
                if views:
                    return function(path, paired_views=views, image_appearance_path=appearance_path, classifier_review=classifier_review, inspection=inspection)
            return function(path, image_appearance_path=appearance_path, classifier_review=classifier_review, inspection=inspection) if name == "open_qupath" else function(path)

        try:
            if registration_editor:
                def current_registration():
                    if self.session is not session or session.current_state_head_sha256 != expected:
                        return False
                    if saved_alignment is not None:
                        return results.saved_alignment_review_identity() == saved_alignment[:2]
                    return reviews is not None and reviews.reference_display_request() == display
                self._registration_navigation.prepare(invoke, current_registration)
                return
            job_id = self.jobs.submit(
                name,
                invoke,
                access=JobAccess.MUTATION if name == "open_qupath" else JobAccess.READ,
            )
        except RuntimeError as exc:
            self.operation_failed.emit(str(exc))
            return
        self._file_jobs.add(job_id)
        if feature_inspection is None and preview_inputs is not None and session is not None and expected is not None:
            self._annotation_preview_jobs[job_id] = (session, expected, reviews, preview_inputs)
        if name == "open_qupath":
            self._qupath_job_id = job_id

    def export_reference_library(self, request_id: str, head: str, destination: Path) -> None:
        session = self.session
        if session is None or self._close_pending or session.current_state_head_sha256 != head:
            return
        if self.reviews_page._current_library_export_identity() != (request_id, head):
            return
        job = self.jobs.submit(
            "export_reference_library",
            lambda: session.service.export_reference_library(
                session.batch_id,
                interaction_request_id=request_id,
                expected_state_head_sha256=head,
                destination=destination,
            ),
            access=JobAccess.MUTATION,
        )
        self._file_jobs.add(job)

    def open_qupath(self, path: Path) -> None:
        self._submit_file_operation("open_qupath", path)

    def inspect_image_features(self, request: object) -> None:
        """Edit filters in the shared native preview and return them to the current form."""
        self._submit_file_operation("open_qupath", None, review_context=False,
                                    feature_inspection=request)

    def open_saved_qupath(self) -> None:
        """Open saved images and cells without starting the last selected review."""
        self._submit_file_operation("open_qupath", None, review_context=False)

    def open_compute_workbench(self):
        if hasattr(self, "execution_location"):
            self.execution_location.open_settings()
            return
        if self._close_pending or self.jobs.has_active_jobs:
            return
        from .compute_workbench import ComputeWorkbench

        folder = Path(self.session.output_root) if self.session is not None else None
        setup = getattr(self, "_setup", None)
        manifest = (setup.path.parent / "analysis.manifest.json") if setup is not None else None
        if folder is None and setup is not None:
            folder = setup.path.parent
        if self.samples_page is not None and not self.samples_page.commit_pending():
            self.shell.show_route("samples")
            return
        draft = self.samples_page.draft if self.samples_page is not None else None
        dialog = ComputeWorkbench(
            self.jobs,
            self.shell,
            workspace=folder,
            manifest_path=manifest,
            file_operations=self.file_operations,
            sample_draft=draft,
        )
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._compute_dialog = dialog
        dialog.show()

    def open_plot(self, path: Path) -> None:
        if isinstance(path, Path) and path.suffix.lower() == ".png":
            from .figure_preview import FigurePreview

            try:
                dialog = FigurePreview(path, self.shell)
            except (OSError, ValueError) as exc:
                self.operation_failed.emit(str(exc))
                return
            dialog.show()
            return
        self._submit_file_operation("open_plot", path)

    def reveal_path(self, path: Path) -> None:
        self._submit_file_operation("reveal_path", path)

    def run_spatial(self, handoffs: tuple[object, ...]) -> None:
        routes = getattr(self, "_bound_routes", None)
        if (
            routes is None
            or not routes.spatial_route.run_available
            or handoffs != routes.spatial_handoffs
        ):
            raise ValueError("spatial request is not the displayed authorized route")
        job_id = self.jobs.submit(
            "run_spatial",
            lambda: self.successors.run_spatial(handoffs),
            access=JobAccess.MUTATION,
        )
        self._successor_jobs[job_id] = "spatial"

    def run_patient(self, spatial_results: tuple[object, ...], subject_ids: object) -> None:
        routes = getattr(self, "_bound_routes", None)
        if (
            routes is None
            or not routes.patient_route.run_available
            or spatial_results != routes.spatial_results
            or subject_ids != routes.subject_id_by_sample
        ):
            raise ValueError("patient request is not the displayed authorized route")
        job_id = self.jobs.submit(
            "run_patient",
            lambda: self.successors.run_patient(spatial_results, subject_ids),
            access=JobAccess.MUTATION,
        )
        self._successor_jobs[job_id] = "patient"

    def _saved_request_current(self, request) -> bool:
        session, epoch, head = request[:3]
        return (self.session is session and self._status_epoch == epoch
                and session.current_state_head_sha256 == head and not self._close_pending)

    def _project_saved_results(self) -> None:
        session = self.session
        if session is None or session.current_state_head_sha256 is None or self._close_pending:
            return
        head = session.current_state_head_sha256
        key = (session, self._status_epoch, head)
        if self._saved_results_key == key:
            return
        self._saved_results_key = key
        self._bound_routes = None
        self.results_page.clear_results()
        self.results_page.image_appearance_path = Path(session.output_root) / "image-appearance.json"
        job = self.jobs.submit(
            "saved_stage_results",
            lambda: session.service.saved_stage_results(session.batch_id, expected_head_sha256=head),
            access=JobAccess.READ,
            coalesce_key=f"saved-results:{id(session)}:{head}:{self._status_epoch}",
        )
        self._saved_stage_jobs[job] = key

    def select_saved_region(self) -> None:
        """Choose coordinates in QuPath without accepting any analysis-region changes."""
        session = self.session
        if session is None or self._close_pending or self.jobs.has_active_jobs or self.file_operations is None:
            return
        identity = self.results_page.saved_figure_identity()
        head = session.current_state_head_sha256
        if identity is None or identity[0] != head:
            return
        try:
            options = self.results_page.saved_figure_options()
            job = self.jobs.submit('open_qupath',
                lambda: self.file_operations.select_saved_figure_region(session.service, session.batch_id,
                    head, identity[1], identity[2], options), access=JobAccess.MUTATION)
        except (ValueError, RuntimeError) as error:
            self.operation_failed.emit(str(error))
            return
        self._saved_region_jobs[job] = (session, identity)
        self._qupath_job_id = job

    def request_measurement_field(self, artifact_ref: str, options: object) -> None:
        session = self.session
        if session is None or self._close_pending:
            return
        head = session.current_state_head_sha256
        identity = self.results_page.measurement_field_identity()
        if head is None or identity is None or identity[:2] != (head, artifact_ref):
            return
        if self._measurement_field_jobs:
            self._measurement_field_pending = (artifact_ref, options, identity)
            return
        job = self.jobs.submit('measurement_field',
            lambda: session.service.measurement_field(session.batch_id,
                expected_head_sha256=head, artifact_ref_sha256=artifact_ref, figure_options=options),
            access=JobAccess.READ,
            coalesce_key=f'measurement-field:{id(session)}:{identity}')
        self._measurement_field_jobs[job] = (session, self._status_epoch, head, identity)

    def _next_measurement_field(self) -> None:
        pending = self._measurement_field_pending
        self._measurement_field_pending = None
        if pending is not None and pending[2] == self.results_page.measurement_field_identity():
            self.request_measurement_field(pending[0], pending[1])

    def request_saved_figure(self, artifact_ref: str, figure_id: str, action: str) -> None:
        session = self.session
        if session is None or self._close_pending or action not in {"open", "export", "data"}:
            return
        head = session.current_state_head_sha256
        identity = self.results_page.saved_figure_identity()
        if head is None or identity is None or identity[:2] != (head, artifact_ref):
            return
        if identity[2] != figure_id:
            return
        options = self.results_page.saved_figure_options(action=action)
        job = self.jobs.submit(
            "saved_stage_figures",
            lambda: session.service.saved_stage_figures(session.batch_id,
                expected_head_sha256=head, artifact_ref_sha256=artifact_ref, figure_id=figure_id, figure_options=options),
            access=JobAccess.READ,
            coalesce_key=f"saved-figure:{id(session)}:{head}:{artifact_ref}:{figure_id}:{identity[3:]}:{action}:{json.dumps(options, sort_keys=True)}",
        )
        self._saved_figure_jobs[job] = (session, self._status_epoch, head, identity, action)

    def _project_results(self) -> None:
        session = self._require_session()
        head = session.current_state_head_sha256
        if head is None:
            return
        spatial_results = self._spatial_results
        epoch = self._status_epoch

        def project() -> _CompletedResults:
            snapshot = session.service.results(session.batch_id, expected_head_sha256=head)
            if (
                snapshot.status.batch_id != session.batch_id
                or snapshot.status.state_head_sha256 != head
            ):
                raise ValueError("results differ from the requested batch or state head")
            view = build_results_view(snapshot)
            routes = bind_successor_routes(
                view,
                spatial_handoffs=snapshot.spatial_handoffs,
                spatial_results=spatial_results,
                registry=self.successors,
            )
            return _CompletedResults(head, routes, any(
                row.qupath_project_entry for row in snapshot.status.summary_rows), session, epoch)

        self._results_job_id = self.jobs.submit(
            "project_results",
            project,
            access=JobAccess.READ,
            coalesce_key=f"results:{id(session)}:{head}:{epoch}",
        )

    def _advance_close(self) -> None:
        if not self._close_pending:
            return
        if self._registration_navigation.pending or self._cell_alignment.pending:
            return
        if self._discard_setup_on_close is not None and not self._setup_discard_is_current(
            self._discard_setup_on_close
        ):
            self._close_pending = False
            self._discard_setup_on_close = None
            self.shell.statusBar().clearMessage()
            self.operation_failed.emit("The setup changed. Review it before closing again.")
            return
        if (
            self._open_job_id is not None
            or self._new_job_id is not None
            or self._pause_job_id is not None
            or self._setup_create_job_id is not None
            or self._setup_save_job_id is not None
            or self._setup_manifest_job_id is not None
        ):
            return
        if (
            self._setup is not None
            and self._setup_dirty
            and self._discard_setup_on_close is None
            and (
                self.session is None
                or self._prepared_without_execution
                or self.session.may_close_immediately()
            )
        ):
            self.shell.statusBar().showMessage("Saving your setup before closing…")
            try:
                self._submit_setup_save()
            except RuntimeError as exc:
                if not self.jobs.has_active_jobs:
                    self._offer_setup_discard(f"The setup could not be saved: {exc}")
            return
        session = self.session
        if self._terminal_status_job_id is not None:
            # A run outcome or failed pause needs fresh owner status before
            # deciding whether close is already authorized.
            return
        if session is None or self._prepared_without_execution or session.may_close_immediately():
            if self.jobs.has_active_jobs or self._close_queued:
                return
            self._close_queued = True
            QTimer.singleShot(0, self._finish_close)
            return
        try:
            self.request_pause()
        except RuntimeError as exc:
            if self.jobs.has_active_jobs:
                # Another mutation may drain before this pending close advances.
                return
            self._close_pending = False
            self.shell.statusBar().clearMessage()
            self.operation_failed.emit(str(exc))
            return
        except ValueError as exc:
            if self._run_job_id is not None:
                # The exact run may have finished before control submission.
                # Its joined outcome will advance close to ordinary pause.
                return
            self._close_pending = False
            self.operation_failed.emit(str(exc))
            return

    def _finish_close(self) -> None:
        self._close_queued = False
        if not self._close_pending:
            return
        if self._registration_navigation.pending or self._cell_alignment.pending:
            return
        if self._discard_setup_on_close is not None and not self._setup_discard_is_current(
            self._discard_setup_on_close
        ):
            self._close_pending = False
            self._discard_setup_on_close = None
            self.shell.statusBar().clearMessage()
            self.operation_failed.emit("The setup changed. Review it before closing again.")
            return
        if (
            self._open_job_id is not None
            or self._new_job_id is not None
            or self._pause_job_id is not None
            or self._setup_create_job_id is not None
            or self._setup_save_job_id is not None
            or self._setup_manifest_job_id is not None
        ):
            return
        session = self.session
        if self.jobs.has_active_jobs:
            return
        if (
            session is not None
            and not self._prepared_without_execution
            and not session.may_close_immediately()
        ):
            self._advance_close()
            return
        if self.file_operations is not None:
            try:
                self.file_operations.require_qupath_stopped()
            except Exception:
                self._close_pending = False
                self.shell.statusBar().showMessage(_QUPATH_OPEN_GUIDANCE)
                self.operation_failed.emit(_QUPATH_OPEN_GUIDANCE)
                return
        location = getattr(self, "execution_location", None)
        if location is not None and not location.may_close():
            self._close_pending = False
            self.shell.statusBar().clearMessage()
            return
        self._close_pending = False
        self._discard_setup_on_close = None
        intent = self._open_after_close
        if intent is not None:
            self._clear_workspace()
            self.shell.statusBar().clearMessage()
            self._open_manifest_intent(intent)
            return
        self.shell.allow_close()
        self.shell.close()

    def _jobs_changed(self, active_job_ids: tuple[str, ...]) -> None:
        active_operations = self.jobs.active_operations
        reviews = getattr(self, "reviews_page", None)
        if reviews is not None:
            # Parent enablement preserves each control's validation/pending state.
            reviews.setEnabled("open_qupath" not in active_operations)
        self._update_activity()
        if active_job_ids:
            self._activity_timer.start()
        else:
            self._activity_timer.stop()
        if not active_job_ids and self._close_pending:
            self._advance_close()
        if getattr(self, "results_page", None) is not None:
            self.results_page.set_region_selection_busy(bool(active_job_ids))
        self._refresh_retry_choices()
        if not active_job_ids and self._continue_session is not None:
            QTimer.singleShot(0, self._advance_guided_workflow)

    def _update_activity(self) -> None:
        """Show active processing stages using the service's in-memory activity."""
        operations = self.jobs.active_operations
        session = getattr(self, "session", None)
        kinds = getattr(session.service, "active_stage_kinds", ()) if session is not None else ()
        if "open_qupath" in operations:
            message = _ACTIVITY_TEXT["open_qupath"]
        elif operations and kinds:
            from mif_pipeline.gui.presenters.workflow import BY_KIND
            labels = []
            for kind in kinds:
                label = _STAGE_ACTIVITY_TEXT.get(kind)
                if label is None and kind in BY_KIND:
                    label = BY_KIND[kind].title + "…"
                if label is not None and label not in labels:
                    labels.append(label)
            message = " · ".join(labels) or "Running analysis…"
        else:
            message = _ACTIVITY_TEXT.get(operations[0], "Working…") if operations else None
        if message != getattr(self, "_last_activity_text", None):
            self._last_activity_text = message
            self.shell.set_activity(message)

    def _job_succeeded(self, job_id: str, value: object) -> None:
        preview = self._kronos_cell_preview_jobs.pop(job_id, None)
        if preview is not None:
            if self._current_kronos_cell_preview(preview):
                preview[3].set_result(value)
            return
        secondary = self._secondary_marker_jobs.pop(job_id, None)
        if secondary is not None:
            if self._current_secondary_marker_request(secondary):
                secondary[5].set_result(secondary[3], secondary[4], value)
            return
        if self._finish_reference_control_inspection(job_id, value):
            return
        model_request = self._cluster_model_jobs.pop(job_id, None)
        if model_request is not None:
            if self._current_cluster_model(model_request):
                model_request[3].model_view.set_model(value)
            return
        marker_request = self._marker_field_jobs.pop(job_id, None)
        if marker_request is not None:
            if self._current_marker_field(marker_request):
                marker_request[3].set_bundle(value)
                self._bind_cell_alignment()
            self._next_marker_field()
            return
        if self._saved_supervised_review.succeeded(job_id, value):
            return
        if self._segmentation_review.succeeded(job_id, value):
            return
        if self._measurement_review.succeeded(job_id, value):
            return
        if self._cell_alignment.succeeded(job_id, value):
            return
        field_request = self._measurement_field_jobs.pop(job_id, None)
        if field_request is not None:
            if self._saved_request_current(field_request):
                self.results_page.set_measurement_field(field_request[3], value)
            self._next_measurement_field()
            return
        if self._registration_navigation.succeeded(job_id, value):
            return
        if self._tissue_navigation.succeeded(job_id, value):
            return
        saved_request = self._saved_stage_jobs.pop(job_id, None)
        if saved_request is not None:
            if self._saved_request_current(saved_request):
                from mif_pipeline.orchestration.batch_service_models import BatchSavedStageResults
                if (type(value) is BatchSavedStageResults
                        and value.batch_id == saved_request[0].batch_id
                        and value.state_head_sha256 == saved_request[2]):
                    self.results_page.set_saved_stages(value)
                else:
                    self._saved_results_key = None
                    self.results_page.saved_results_failed("Saved results could not be read. Reopen Results & export to retry.")
            return
        figure_request = self._saved_figure_jobs.pop(job_id, None)
        if figure_request is not None:
            if self._saved_request_current(figure_request):
                self.results_page.set_saved_figures(figure_request[3], value, figure_request[4])
            return
        revision_job = self._stage_revision_jobs.pop(job_id, None)
        if revision_job is not None:
            session_id, dialog = revision_job
            if self.session is not None and id(self.session) == session_id and dialog is self._stage_options_dialog:
                from mif_pipeline.orchestration.batch_source_revision import SourceSelection
                if (dialog.options.kind == "inspection" and self.samples_page is not None
                        and type(dialog._preview.settings) is SourceSelection):
                    self.samples_page.show_corrected_source(dialog.options.sample_id, dialog._preview.settings)
                    self._project_sample_list(self.samples_page.draft)
                dialog.set_applying(False)
                dialog.accept()
        options_job = self._stage_option_jobs.pop(job_id, None)
        if options_job is not None:
            selected_cluster_ids=self._cluster_refit_option_drafts.pop(job_id,None)
            operation, session_id, head, dialog = options_job
            session = self.session
            if session is None or id(session) != session_id or session.last_status is None or session.last_status.state_head_sha256 != head:
                return
            if operation in {"options", "inspection-cycles"}:
                from mif_pipeline.orchestration.batch_stage_settings import BatchStageOptions
                from .stage_options import StageOptionsDialog
                if type(value) is not tuple or not value or any(type(item) is not BatchStageOptions or item.state_head_sha256 != head for item in value):
                    self.operation_failed.emit("The saved stage options changed. Refresh the analysis.")
                    return
                selected = value[0]
                if operation == 'inspection-cycles' and (len(value) != 1 or selected.kind != 'inspection'
                        or selected.inspection_review is None):
                    self.operation_failed.emit('The image inspection changed. Reopen its current review.')
                    return
                if selected_cluster_ids is not None:
                    if (len(value)!=1 or selected.kind not in {'clustering_per_slide','clustering_pooled'}
                            or not set(selected_cluster_ids)<=set(selected.saved_clustering['cluster_counts'])):
                        self.operation_failed.emit('The selected clusters changed. Reopen the current review.')
                        return
                    selected=replace(selected,settings=replace(selected.settings,cluster_refit=replace(
                        selected.settings.cluster_refit,cluster_ids=tuple(sorted(selected_cluster_ids)))))
                def option_scope(item, number=None):
                    if item.kind == "inspection":
                        return f"Sample {item.sample_id} · Original image and series"
                    if item.kind == "phenotyping_mode_resolution":
                        return f"Sample {item.sample_id} · Cell phenotyping method · Analysis region {number or 1}"
                    if item.kind == "unsupervised_preparation":
                        return f"Sample {item.sample_id} · Clustering settings"
                    if item.kind == "cluster_label_review":
                        return "Samples " + ", ".join(item.participant_ids) + " · Cluster labels"
                    if item.kind in {'clustering_per_slide','clustering_pooled'}:
                        return "Samples " + ", ".join(item.participant_ids)
                    if item.kind == "marker_gate_review":
                        return "Samples " + ", ".join(item.participant_ids) + " · Shared marker review"
                    if item.kind == "tissue_setup":
                        return f"Sample {item.sample_id} · Tissue-detector selection"
                    if item.kind == "tissue_resolution":
                        return f"Sample {item.sample_id} · Tissue-detection resolution"
                    if item.kind == "tissue_candidate":
                        return f"Sample {item.sample_id} · Tissue detection and review"
                    if item.kind == "analysis_domain_selection":
                        return f"Sample {item.sample_id} · Analysis regions"
                    if item.kind == "registration":
                        return f"Sample {item.sample_id} · Paired-image alignment"
                    if item.kind.endswith("classifier_training_plan_review") or item.kind == "supervised_phenotype_training_plan_review":
                        return "Samples " + ", ".join(item.participant_ids) + " · Classifier setup"
                    return f"Sample {item.sample_id} · Tissue section {number or 1}"
                scope_label = option_scope(selected)
                if len(value) > 1:
                    chooser = QDialog(self.run_page)
                    chooser.setWindowTitle("Choose a saved task")
                    layout = QVBoxLayout(chooser)
                    selector = QComboBox(chooser)
                    section_numbers: dict[tuple[str, str], int] = {}
                    for item in value:
                        scope_key = (item.sample_id, item.kind)
                        section_numbers[scope_key] = section_numbers.get(scope_key, 0) + 1
                        selector.addItem(option_scope(item, section_numbers[scope_key]), item.node_id)
                    layout.addWidget(selector)
                    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, chooser)
                    buttons.accepted.connect(chooser.accept)
                    buttons.rejected.connect(chooser.reject)
                    layout.addWidget(buttons)
                    if chooser.exec() != QDialog.DialogCode.Accepted:
                        return
                    selected = next(item for item in value if item.node_id == selector.currentData())
                    scope_label = selector.currentText()
                    if session.last_status is None or session.last_status.state_head_sha256 != head:
                        self.operation_failed.emit("The analysis changed. Reopen its current options.")
                        return
                if self._tissue_navigation.open(selected):
                    return
                if self._stage_options_dialog is not None:
                    self._stage_options_dialog.reject()
                dialog = StageOptionsDialog(
                    selected,
                    session.last_status.workflow_nodes,
                    self.run_page,
                    scope_label=scope_label,
                    inspection_action='review_acquisition_cycles' if operation == 'inspection-cycles' else 'change_source',
                )
                self._stage_options_dialog = dialog
                if selected.kind in {"inspection", "registration"}:
                    dialog.form.inspection_requested.connect(lambda path: self._inspect_source_options(dialog, path))
                dialog.preview_requested.connect(lambda settings: self._preview_stage_options(dialog, settings))
                dialog.apply_requested.connect(lambda preview: self._apply_stage_options(dialog, preview))
                dialog.remove_cells_requested.connect(lambda: self._remove_measurement_cells(dialog))
                dialog.finished.connect(lambda _result: setattr(self, "_stage_options_dialog", None))
                dialog.open()
            elif dialog is self._stage_options_dialog:
                if operation == "cell-removals":
                    dialog.setEnabled(True)
                    dialog._invalidate_preview()
                    count = value["changes"]["removed_cells"]
                    dialog.edit_notice.setText(f"Returned {count} cell removal(s). Review the affected results to recalculate measurements.")
                    dialog.edit_notice.show()
                elif operation == "source-inspection":
                    try:
                        dialog.form.set_inventory(value)
                    except ValueError as exc:
                        dialog.show_error(str(exc))
                else:
                    dialog.set_preview(value)
            return
        retry_key = self._retry_choice_jobs.pop(job_id, None)
        if retry_key is not None:
            if retry_key == self._retry_context() == self._retry_status_key:
                self._retry_cache_key = retry_key
                if type(value) is tuple and all(
                    type(item) is BatchRetryChoice
                    and item.batch_id == self.session.batch_id
                    and item.state_head_sha256 == retry_key[3]
                    for item in value
                ):
                    self._retry_choices = value
                else:
                    self._retry_choices = ()
                    self.operation_failed.emit(
                        "The interrupted-step listing is invalid. Refresh to check again."
                    )
            self._refresh_retry_choices()
            return
        combination_request = self._combination_preview_jobs.pop(job_id, None)
        if combination_request is not None:
            self.reviews_page.combination_preview_finished(combination_request[-1])
            if self._current_combination_preview(combination_request):
                combination_request[-1].set_preview_result(value)
            return
        prediction_request = self._prediction_page_jobs.pop(job_id, None)
        if prediction_request is not None:
            if self._current_prediction_page(prediction_request):
                prediction_request[3].set_page(value)
            return
        plot_request = self._review_plot_jobs.pop(job_id, None)
        if plot_request is not None:
            if self._current_review_plot_request(plot_request):
                if type(value) is not BatchReviewPlots or not self.reviews_page.set_review_plots(
                    value
                ):
                    self.reviews_page.set_review_plots_failure(plot_request[2], plot_request[3])
                else:
                    self._bind_cell_alignment()
            return
        if job_id == self._open_job_id and isinstance(value, _PreparedWorkspace):
            self._open_job_id = None
            self._pending_loaded_manifest = None
            self._new_job_id = job_id
        if job_id in self._folder_load_jobs:
            self._folder_load_jobs.remove(job_id)
            if not self._close_pending:
                intent = OpenWorkspaceIntent(value)
                if self._setup is not None and self._setup.path == value:
                    return
                session = self.session
                if session is not None and value.parent.resolve() == (
                    session.output_root / session.batch_id
                ).resolve():
                    return
                if self.session is not None or self.samples_page is not None:
                    self.request_close(open_after_close=intent)
                else:
                    self._open_manifest_intent(intent)
            return
        if job_id == self._setup_manifest_job_id:
            self._setup_manifest_job_id = None
            if (
                not isinstance(value, Path)
                or self._setup is None
                or value != self._setup.path.parent / "analysis.manifest.json"
            ):
                self.operation_failed.emit("manifest preparation returned an invalid path")
                return
            self._prepared_manifest_path = value
            if self.samples_page is not None:
                self.samples_page.lock_for_plan(prepared=False)
            if not self._close_pending:
                try:
                    self._submit_setup_service_plan(value)
                except RuntimeError as exc:
                    self.operation_failed.emit(str(exc))
            self._advance_close()
            return
        if job_id == self._setup_create_job_id:
            self._setup_create_job_id = None
            if type(value) is not AnalysisSetupDocument:
                self.operation_failed.emit("analysis setup creation returned an invalid document")
            elif not self._close_pending:
                self._install_setup(value)
            self._advance_close()
            return
        setup_path = self._setup_load_jobs.pop(job_id, None)
        if setup_path is not None:
            if job_id != self._latest_manifest_load_job_id:
                return
            self._latest_manifest_load_job_id = None
            if not self._close_pending:
                if type(value) is AnalysisSetupDocument:
                    self._install_setup(value)
                else:
                    self.operation_failed.emit("analysis setup load returned an invalid document")
            self._advance_close()
            return
        if job_id == self._setup_save_job_id:
            self._setup_save_job_id = None
            saved_input = self._setup_save_document
            self._setup_save_document = None
            if type(value) is not AnalysisSetupDocument or saved_input is None:
                self._promote_after_save = False
                if self._close_pending:
                    self._offer_setup_discard("The setup save returned an invalid result.")
                else:
                    self.operation_failed.emit("analysis setup save returned an invalid document")
                return
            current = self._setup
            self._setup = (
                replace(value, analysis_settings=current.analysis_settings)
                if current is not None
                else value
            )
            page = self.samples_page
            self._setup_dirty = (page is not None and page.draft != value.draft) or (
                current is not None and (current.analysis_settings != value.analysis_settings)
            )
            if self._close_pending:
                self._promote_after_save = False
                self._advance_close()
            elif self._promote_after_save:
                self._promote_after_save = False
                if self._setup_dirty:
                    self._promote_setup()
                else:
                    try:
                        self._submit_setup_promotion()
                    except RuntimeError as exc:
                        self.shell.show_route("samples")
                        self.operation_failed.emit(str(exc))
            return
        saved_region_job = self._saved_region_jobs.pop(job_id, None)
        if saved_region_job is not None:
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
            origin, identity = saved_region_job
            if not self._close_pending and self.session is origin and origin.current_state_head_sha256 == identity[0]:
                try:
                    self.results_page.apply_saved_region(identity, value)
                except ValueError as error:
                    self.operation_failed.emit(str(error))
            self._advance_close()
            return
        preparation_job = self._reference_preparation_jobs.pop(job_id, None)
        if preparation_job is not None:
            origin, identity = preparation_job
            if (not self._close_pending and self.session is origin
                    and origin.current_state_head_sha256 == identity[1]):
                self.reviews_page.offer_reference_preparation(identity, value)
            self._advance_close()
            return
        component_job = self._reference_component_jobs.pop(job_id, None)
        if component_job is not None:
            origin, identity = component_job
            if (not self._close_pending and self.session is origin
                    and origin.current_state_head_sha256 == identity[1]):
                self.reviews_page.offer_reference_component(identity, value)
            self._advance_close()
            return
        reference_region_job = self._reference_region_jobs.pop(job_id, None)
        if reference_region_job is not None:
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
            origin, identity = reference_region_job
            if self.session is origin and origin.current_state_head_sha256 == identity[1]:
                try:
                    if len(identity) == 4:
                        self.reviews_page.offer_reference_preparation_regions(identity, value)
                    else:
                        self.reviews_page.offer_reference_regions(identity, value)
                except (ValueError, OSError) as error:
                    self.operation_failed.emit(str(error))
            self._advance_close()
            return
        if job_id in self._file_jobs:
            self._file_jobs.remove(job_id)
            self._annotation_preview_jobs.pop(job_id, None)
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
                reviews = getattr(self, "reviews_page", None)
                if reviews is not None:
                    try:
                        from .region_review import RegionDecisionReview
                        region = reviews.findChild(RegionDecisionReview)
                        if region is not None:
                            region.refresh_appearance()
                        if isinstance(value, dict) and value.get("status") in (
                            "feature_settings_saved", "feature_settings_cancelled"
                        ):
                            if not self._tissue_navigation.feature_inspection_finished(value):
                                reviews.feature_inspection_finished(value)
                        elif isinstance(value, dict) and value.get("status") == "annotations_saved":
                            reviews.offer_classifier_reference_regions(value)
                            reviews._set_status_message(
                                "Annotations saved. Complete the current form to continue the analysis."
                            )
                        else:
                            if not self._tissue_navigation.authoring_finished(result=value):
                                reviews.tissue_authoring_finished(result=value)
                    except (ValueError, OSError) as error:
                        self.operation_failed.emit(str(error))
            if isinstance(value, Path) and str(value).endswith(".miflib.zip"):
                self.shell.statusBar().showMessage(
                    "Reference library exported to " + str(value), 15000
                )
            self._advance_close()
            return
        successor_kind = self._successor_jobs.pop(job_id, None)
        if successor_kind is not None:
            if successor_kind == "spatial":
                self._spatial_results = (value,)
                session = self.session
                if session is not None and session.last_status is not None:
                    self._project_results()
            return
        draft = self._validation_jobs.pop(job_id, None)
        if draft is not None:
            if self.samples_page is not None and self.samples_page.draft == draft:
                try:
                    self.samples_page.set_validation(value)
                except (TypeError, ValueError) as exc:
                    self.operation_failed.emit(str(exc))
            return
        manifest_path = self._manifest_load_jobs.pop(job_id, None)
        if manifest_path is not None:
            if job_id != self._latest_manifest_load_job_id:
                return
            self._latest_manifest_load_job_id = None
            if self._close_pending:
                return
            if type(value) is not ManifestDraft:
                self.operation_failed.emit("manifest load returned an invalid draft")
                return
            try:
                from ..execution_location import load_location

                location = load_location(
                    value.output_root / value.batch_id / "analysis.computing.json"
                )
                if location["mode"] != "local":
                    self._install_loaded_manifest(value)
                    self.shell.show_route("computing")
                    return
                self._pending_loaded_manifest = value
                self.open_workspace(value.output_root, value.batch_id, manifest_path=manifest_path)
            except (RuntimeError, TypeError, ValueError) as exc:
                self._pending_loaded_manifest = None
                self.operation_failed.emit(str(exc))
            return
        paired_request = self._paired_image_jobs.pop(job_id, None)
        if paired_request is not None:
            page, request, source_path = paired_request
            if page is not self.samples_page or not page.paired_inspection_is_current(request, source_path):
                return
            try:
                page.set_paired_image_inventory(request, source_path, value)
            except (AttributeError, TypeError, ValueError) as exc:
                page.paired_image_inspection_failed(request, source_path, str(exc))
            return
        source_request = self._source_series_jobs.pop(job_id, None)
        if source_request is not None:
            sample_id, source_path = source_request
            if not self._source_sample_is_current(sample_id, source_path):
                return
            if not isinstance(value, SourceSeriesInventory):
                self.samples_page.source_series_inspection_failed(sample_id, source_path, "Image inspection returned an invalid result.")
                self.operation_failed.emit("source-series inspection returned an invalid result")
                return
            page = self.samples_page
            assert page is not None
            try:
                page.set_source_series_inventory(sample_id, source_path, value)
            except (AttributeError, TypeError, ValueError) as exc:
                page.source_series_inspection_failed(sample_id, source_path, str(exc))
                self.operation_failed.emit(str(exc))
            return
        interaction_request = self._interaction_jobs.pop(job_id, None)
        if interaction_request is not None:
            _query, head, epoch = interaction_request
            session = self.session
            if (
                session is None
                or session.current_state_head_sha256 != head
                or epoch != self._status_epoch
                or _query.node_ids != self._workflow_review_node_ids
            ):
                return
            if type(value) is not BatchInteractionPage:
                self.operation_failed.emit("interaction listing returned an invalid page")
                return
            session.last_interactions = value
            if self._tissue_navigation.visible and self._tissue_navigation.pending_node_id is None:
                return
            if hasattr(self, "reviews_page"):
                self.reviews_page.image_appearance_path = Path(session.output_root) / "image-appearance.json"
                self.reviews_page.set_page(value, expected_state_head_sha256=head)
                self._tissue_navigation.bind_pending(value, head)
                pending = self._workflow_skip_pending
                self._workflow_skip_pending = None
                if (pending is not None and pending[:3] == (session, head, epoch)
                        and not self._close_pending):
                    try:
                        self.reviews_page.submit_optional_setup_skip(pending[3], head)
                    except (TypeError, ValueError) as error:
                        self.operation_failed.emit(str(error))
            return
        if job_id == self._open_job_id:
            self._open_job_id = None
            if not isinstance(value, _OpenedWorkspace):
                self._pending_loaded_manifest = None
                self.operation_failed.emit("workspace open returned an invalid result")
                return
            try:
                session = WorkspaceSession(
                    manifest_path=None,
                    output_root=value.output_root,
                    batch_id=value.batch_id,
                    service=value.service,
                )
                if value.status is not None:
                    session.begin_status(job_id)
                    session.record_status(job_id, value.status)
                    if value.status.resume_disposition == "non_resumable":
                        session.mark_diagnostic_only()
                pending = value.draft
                if pending is not None:
                    self._install_loaded_manifest(pending)
                    self.samples_page.lock_for_plan()
                    if value.plan is not None:
                        session.last_plan = value.plan
                        self.plan_page.set_plan(build_batch_plan_view(value.plan, draft=pending), started=True)
            except (RuntimeError, TypeError, ValueError) as exc:
                self._pending_loaded_manifest = None
                self.operation_failed.emit(str(exc))
                return
            self._pending_loaded_manifest = None
            self.session = session
            self._prepared_without_execution = value.status is None
            if value.status is not None and hasattr(self, "run_page"):
                try:
                    view = build_batch_run_view(value.status, active_operation=None, log_lines=())
                    self.run_page.set_view(
                        view,
                        cancel_offered=False,
                        exit_state=session.exit_state,
                        close_authorized=session.may_close_immediately(),
                        saved_images_available=any(
                            row.qupath_project_entry for row in value.status.summary_rows),
                    )
                    self._display_run_context(view.batch_id)
                except (TypeError, ValueError) as exc:
                    self.operation_failed.emit(str(exc))
                else:
                    self._project_saved_results()
                    if value.status.resume_disposition == "completed":
                        self._project_results()
            self.workspace_opened.emit(session)
            if value.status is not None and not self._close_pending:
                self.shell.show_route("run")
            self._retry_status_key = self._retry_context()
            self._refresh_retry_choices()
            self._advance_close()
        elif job_id == self._new_job_id:
            self._new_job_id = None
            if not isinstance(value, _PreparedWorkspace):
                self.operation_failed.emit("new analysis returned an invalid result")
                return
            try:
                plan_view = build_batch_plan_view(value.plan, draft=value.draft)
                session = WorkspaceSession(
                    manifest_path=value.manifest_path,
                    output_root=value.draft.output_root,
                    batch_id=value.draft.batch_id,
                    service=value.service,
                    last_plan=value.plan,
                )
                if value.execution_status is not None:
                    session.begin_status(job_id)
                    session.record_status(job_id, value.execution_status)
                    if value.execution_status.resume_disposition == "non_resumable":
                        session.mark_diagnostic_only()
                if self.samples_page is None:
                    self._install_loaded_manifest(value.draft)
                self.plan_page.set_plan(plan_view, started=value.execution_status is not None)
                from mif_pipeline.orchestration.batch_workflow import project_plan_nodes

                self.run_page.set_plan_preview(
                    project_plan_nodes(value.plan), can_start=plan_view.prerequisites_ready
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                self.operation_failed.emit(str(exc))
                return
            self.session = session
            self._prepared_without_execution = value.execution_status is None
            assert self.samples_page is not None
            self.samples_page.lock_for_plan()
            self.workspace_opened.emit(session)
            self._advance_close()
        elif job_id == self._status_job_id:
            self._status_job_id = None
            terminal_status = job_id == self._terminal_status_job_id
            pending_failure = self._pending_run_failure if terminal_status else None
            if terminal_status:
                self._terminal_status_job_id = None
                self._pending_run_failure = None
                if hasattr(self, "run_page"):
                    self.run_page.invalidate_actions()
            session = self.session
            if session is None or session.pending_status_operation_id != job_id:
                if (
                    terminal_status
                    and self._close_pending
                    and (session is None or not session.may_close_immediately())
                ):
                    self._close_pending = False
                    reason = self._close_pause_failure
                    self._close_pause_failure = None
                    self.shell.statusBar().clearMessage()
                    self.operation_failed.emit(
                        "current workspace status is unavailable"
                        if reason is None
                        else f"{reason}; current workspace status is unavailable"
                    )
                self._advance_close()
                if pending_failure is not None:
                    self.operation_failed.emit(pending_failure)
                return
            if any(
                active is not None
                for active in (
                    self._response_job_id,
                    self._pause_job_id,
                    self._cancel_job_id,
                )
            ):
                session.pending_status_operation_id = None
                if pending_failure is not None:
                    self.operation_failed.emit(pending_failure)
                return
            if value is None:
                # Only the owner can authenticate pristine unstarted state.
                # A read racing a run cannot grant close authority.
                session.pending_status_operation_id = None
                if self._run_job_id is None:
                    self._prepared_without_execution = True
                    self._close_pause_failure = None
                    self._advance_close()
                if pending_failure is not None:
                    self.operation_failed.emit(pending_failure)
                return
            if hasattr(self, "plan_page"):
                self.plan_page.mark_started()
            if self._run_job_id is not None and not (
                terminal_status and self._close_pause_failure is not None
            ):
                # A read concurrent with a run may describe its predecessor.
                # It can update the display but cannot grant close authority.
                session.pending_status_operation_id = None
                if hasattr(self, "run_page"):
                    try:
                        view = build_batch_run_view(value, active_operation="run", log_lines=())
                        self.run_page.set_view(
                            view,
                            cancel_offered=False,
                            exit_state=SessionExitState.MUTABLE,
                            close_authorized=False,
                            saved_images_available=False,
                        )
                        self._display_run_context(view.batch_id)
                    except (TypeError, ValueError) as exc:
                        self.operation_failed.emit(str(exc))
                if pending_failure is not None:
                    self.operation_failed.emit(pending_failure)
                return
            try:
                session.record_status(job_id, value)
                self._prepared_without_execution = False
                if value.resume_disposition == "non_resumable":
                    session.mark_diagnostic_only()
                if hasattr(self, "run_page"):
                    view = build_batch_run_view(value, active_operation=None, log_lines=())
                    self.run_page.set_view(
                        view,
                        cancel_offered=False,
                        exit_state=session.exit_state,
                        close_authorized=session.may_close_immediately(),
                        saved_images_available=any(
                            row.qupath_project_entry for row in value.summary_rows),
                    )
                    self._display_run_context(view.batch_id)
            except (TypeError, ValueError) as exc:
                if self._close_pending:
                    self._close_pending = False
                    self.shell.statusBar().clearMessage()
                reason = self._close_pause_failure
                self._close_pause_failure = None
                self.operation_failed.emit(str(exc) if reason is None else f"{reason}; {exc}")
                if pending_failure is not None:
                    self.operation_failed.emit(pending_failure)
                return
            if pending_failure is not None:
                # Publish the final accepted status before choosing recovery guidance.
                self.operation_failed.emit(pending_failure)
                pending_failure = None
            reason = self._close_pause_failure if terminal_status else None
            if reason is not None:
                self._close_pause_failure = None
                if not session.may_close_immediately():
                    self._close_pending = False
                    self.shell.statusBar().clearMessage()
                    self.operation_failed.emit(reason)
                    return
            if self._response_refresh_pending:
                self._response_refresh_pending = False
                if self._continue_session is None:
                    self.request_interactions(BatchInteractionQuery())
            elif self.shell.current_route_id == "reviews" and self._continue_session is None:
                self.request_interactions(BatchInteractionQuery())
            if hasattr(self, "results_page"):
                self._project_saved_results()
                if value.resume_disposition == "completed":
                    self._project_results()
            self._retry_status_key = self._retry_context()
            self._refresh_retry_choices()
            self._advance_close()
        elif job_id == self._run_job_id:
            self._run_job_id = None
            if (
                self.session is not None
                and self._pause_job_id is None
                and self._terminal_status_job_id is None
                and not (self._close_pending and self.session.may_close_immediately())
            ):
                self._status_epoch += 1
                try:
                    self.refresh_status()
                    self._terminal_status_job_id = self._status_job_id
                except RuntimeError as exc:
                    if self._close_pending:
                        self._close_pending = False
                    self.operation_failed.emit(str(exc))
            self._advance_close()
        elif job_id == self._pause_job_id:
            self._pause_job_id = None
            session = self.session
            if session is None:
                return
            try:
                session.record_pause(job_id, value)
            except (TypeError, ValueError) as exc:
                self._begin_mutation(session)
                self._close_pending = False
                self.operation_failed.emit(str(exc))
                return
            if not self._close_pending:
                self._status_epoch += 1
                self.refresh_status()
            self._advance_close()
        elif job_id == self._cancel_job_id:
            self._cancel_job_id = None
            if type(value) is not BatchCancellationResult:
                self.operation_failed.emit("cancellation returned an invalid result")
                self._advance_close()
                return
            self._status_epoch += 1
            self.refresh_status()
            self._advance_close()
        elif job_id == self._response_job_id:
            self._response_job_id = None
            if job_id == self._cutoff_assignment_job_id:
                from mif_pipeline.orchestration.batch_service_models import BatchStatusSnapshot
                self._cutoff_assignment_job_id = None
                if type(value) is not BatchStatusSnapshot or self.session is None or value.batch_id != self.session.batch_id:
                    self._continue_session = None
                    self.operation_failed.emit('The cutoff could not be saved. Refresh the analysis and reopen cell assignment review.')
                else:
                    self._continue_accepted = True
                    if self._cutoff_saved_page is None:
                        self.reviews_page.set_response_status('accepted_for_retry')
                    else:
                        unchanged = value.state_head_sha256 == self._continue_origin_head
                        if unchanged:
                            self._continue_session = None
                        self._saved_supervised_review.assignment_finished(self._cutoff_saved_page,
                            'This cutoff is already saved.' if unchanged else 'Applying the saved cutoff…')
                self._cutoff_saved_page = None
                self._response_refresh_pending = True
                self._status_epoch += 1
                self.refresh_status()
                return
            if type(value) is not BatchInteractionApplicationResult:
                self.operation_failed.emit("interaction response returned an invalid result")
                return
            accepted = bool(value.applications) and all(
                item.status == "accepted_for_retry" for item in value.applications
            )
            self._continue_accepted = accepted
            if not accepted:
                self._continue_session = None
            if hasattr(self, "reviews_page"):
                if accepted:
                    self.reviews_page.set_response_status("accepted_for_retry")
                else:
                    self.reviews_page.set_response_status(
                        "rejected",
                        rejection_codes=tuple(
                            item.rejection_code for item in value.applications if item.rejection_code
                        ),
                    )
            self._response_refresh_pending = True
            self._status_epoch += 1
            self.refresh_status()
        elif job_id == self._results_job_id:
            self._results_job_id = None
            session = self.session
            if (
                type(value) is not _CompletedResults
                or session is None
                or value.session is not session
                or value.epoch != self._status_epoch
                or session.current_state_head_sha256 != value.status_head
            ):
                return
            self._bound_routes = value.routes
            self.results_page.set_view(
                value.routes,
                saved_images_available=value.saved_images_available,
                plot_path=None,
                reveal_path=None,
            )

    def _job_failed(self, job_id: str, error: DesktopJobError) -> None:
        preview = self._kronos_cell_preview_jobs.pop(job_id, None)
        if preview is not None:
            if self._current_kronos_cell_preview(preview):
                preview[3].set_error(error.message)
            return
        secondary = self._secondary_marker_jobs.pop(job_id, None)
        if secondary is not None:
            if self._current_secondary_marker_request(secondary):
                secondary[5].set_error(secondary[3], secondary[4], error.message)
            return
        # operation_failed remains a text signal for existing consumers. The
        # directly connected dialog can also retain this worker's full traceback.
        self.current_job_error = error
        try:
            self._present_job_failure(job_id, error)
        finally:
            self.current_job_error = None

    def _present_job_failure(self, job_id: str, error: DesktopJobError) -> None:
        if self._finish_reference_control_inspection(job_id, error=error.message):
            return
        model_request = self._cluster_model_jobs.pop(job_id, None)
        if model_request is not None:
            if self._current_cluster_model(model_request):
                self.reviews_page._set_plot_status('Could not load the learned profiles: ' + error.message)
            return
        marker_request = self._marker_field_jobs.pop(job_id, None)
        if marker_request is not None:
            if self._current_marker_field(marker_request):
                marker_request[3].set_error(error.message)
            self._next_marker_field()
            return
        if self._saved_supervised_review.failed(job_id, error):
            return
        if self._segmentation_review.failed(job_id, error):
            return
        if self._measurement_review.failed(job_id, error):
            return
        if self._cell_alignment.failed(job_id, error):
            return
        field_request = self._measurement_field_jobs.pop(job_id, None)
        if field_request is not None:
            if self._saved_request_current(field_request):
                self.results_page.measurement_field_failed(field_request[3], error.message)
            self._next_measurement_field()
            return
        if self._registration_navigation.failed(job_id, error):
            return
        if self._tissue_navigation.failed(job_id, error):
            return
        saved_request = self._saved_stage_jobs.pop(job_id, None)
        if saved_request is not None:
            if self._saved_request_current(saved_request):
                self._saved_results_key = None
                self.results_page.saved_results_failed(error.message)
            return
        figure_request = self._saved_figure_jobs.pop(job_id, None)
        if figure_request is not None:
            if self._saved_request_current(figure_request):
                self.results_page.saved_figure_failed(figure_request[3], error.message)
            return
        revision_job = self._stage_revision_jobs.pop(job_id, None)
        revision_error_displayed = False
        if revision_job is not None:
            session_id, dialog = revision_job
            if self.session is not None and id(self.session) == session_id and dialog is self._stage_options_dialog:
                dialog.set_applying(False)
                dialog.show_error(error.message, diagnostic=error.diagnostic)
                revision_error_displayed = True
        options_job = self._stage_option_jobs.pop(job_id, None)
        if options_job is not None:
            self._cluster_refit_option_drafts.pop(job_id,None)
            dialog = options_job[3]
            if dialog is not None and dialog is self._stage_options_dialog:
                dialog.setEnabled(True)
                dialog.show_error(error.message, diagnostic=error.diagnostic)
            else:
                self.operation_failed.emit(error.message)
            return
        if job_id in {self._response_job_id, self._run_job_id, self._status_job_id}:
            self._continue_session = None
        retry_key = self._retry_choice_jobs.pop(job_id, None)
        if retry_key is not None:
            if retry_key == self._retry_context() == self._retry_status_key:
                self._retry_cache_key = retry_key
                self._retry_choices = ()
                self.operation_failed.emit(error.message)
            self._refresh_retry_choices()
            return
        combination_request = self._combination_preview_jobs.pop(job_id, None)
        if combination_request is not None:
            self.reviews_page.combination_preview_finished(combination_request[-1])
            if self._current_combination_preview(combination_request):
                combination_request[-1].set_preview_error(error.message)
            return
        prediction_request = self._prediction_page_jobs.pop(job_id, None)
        if prediction_request is not None:
            if self._current_prediction_page(prediction_request):
                prediction_request[3].page_failed(prediction_request[4], error.message)
            return
        plot_request = self._review_plot_jobs.pop(job_id, None)
        if plot_request is not None:
            if self._current_review_plot_request(plot_request):
                self.reviews_page.set_review_plots_failure(plot_request[2], plot_request[3], error.message)
            return
        if job_id in self._folder_load_jobs:
            self._folder_load_jobs.remove(job_id)
            self.operation_failed.emit(error.message)
            return
        if job_id == self._setup_manifest_job_id:
            self._setup_manifest_job_id = None
            self._promote_after_save = False
            self._close_pending = False
            self.operation_failed.emit(error.message)
            if self._setup is not None:
                self.shell.show_route("samples")
            return
        if job_id == self._setup_create_job_id:
            self._setup_create_job_id = None
            self._close_pending = False
            self.operation_failed.emit(error.message)
            return
        if self._setup_load_jobs.pop(job_id, None) is not None:
            if job_id == self._latest_manifest_load_job_id:
                self._latest_manifest_load_job_id = None
                if not self._close_pending:
                    self.operation_failed.emit(error.message)
            self._advance_close()
            return
        if job_id == self._setup_save_job_id:
            self._setup_save_job_id = None
            self._setup_save_document = None
            self._promote_after_save = False
            if self._close_pending:
                self._offer_setup_discard(f"The setup could not be saved: {error.message}")
            else:
                self.operation_failed.emit(error.message)
            if self._setup is not None:
                self.shell.show_route("samples")
            return
        if self._saved_region_jobs.pop(job_id, None) is not None:
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
            self.operation_failed.emit(error.message)
            self._advance_close()
            return
        preparation_job = self._reference_preparation_jobs.pop(job_id, None)
        if preparation_job is not None:
            origin, identity = preparation_job
            if (not self._close_pending and self.session is origin
                    and origin.current_state_head_sha256 == identity[1]):
                self.reviews_page.offer_reference_preparation(identity, error=error.message)
            self._advance_close()
            return
        component_job = self._reference_component_jobs.pop(job_id, None)
        if component_job is not None:
            origin, identity = component_job
            if (not self._close_pending and self.session is origin
                    and origin.current_state_head_sha256 == identity[1]):
                self.reviews_page.offer_reference_component(identity, error=error.message)
            self._advance_close()
            return
        reference_region_job = self._reference_region_jobs.pop(job_id, None)
        if reference_region_job is not None:
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
            self.operation_failed.emit(error.message)
            self._advance_close()
            return
        if job_id in self._file_jobs:
            self._file_jobs.remove(job_id)
            preview_job = self._annotation_preview_jobs.pop(job_id, None)
            message = error.message
            if preview_job is not None:
                origin, head, reviews, inputs = preview_job
                if self.session is origin and origin.current_state_head_sha256 == head:
                    message = reviews.present_annotation_preview_error(inputs, message) or message
            if job_id == self._qupath_job_id:
                self._qupath_job_id = None
                reviews = getattr(self, "reviews_page", None)
                if reviews is not None:
                    self._tissue_navigation.feature_inspection_finished(
                        {"status": "feature_settings_cancelled", "classifier_index": -1}
                    )
                    if not self._tissue_navigation.authoring_finished(success=False):
                        reviews.tissue_authoring_finished(success=False)
                try:
                    self.file_operations.require_qupath_stopped()
                except Exception:
                    self.shell.statusBar().showMessage(_QUPATH_OPEN_GUIDANCE)
                    self.operation_failed.emit(_QUPATH_OPEN_GUIDANCE)
                    self._advance_close()
                    return
            self.operation_failed.emit(message)
            self._advance_close()
            return
        if self._successor_jobs.pop(job_id, None) is not None:
            self.operation_failed.emit(error.message)
            return
        draft = self._validation_jobs.pop(job_id, None)
        if draft is not None:
            self.operation_failed.emit(error.message)
            return
        manifest_path = self._manifest_load_jobs.pop(job_id, None)
        if manifest_path is not None:
            if job_id == self._latest_manifest_load_job_id:
                self._latest_manifest_load_job_id = None
                if not self._close_pending:
                    self.operation_failed.emit(error.message)
            return
        paired_request = self._paired_image_jobs.pop(job_id, None)
        if paired_request is not None:
            page, request, source_path = paired_request
            if page is self.samples_page:
                page.paired_image_inspection_failed(request, source_path, error.message)
            return
        source_request = self._source_series_jobs.pop(job_id, None)
        if source_request is not None:
            if self._source_sample_is_current(*source_request):
                self.samples_page.source_series_inspection_failed(*source_request, error.message)
                self.operation_failed.emit(error.message)
            return
        if self._interaction_jobs.pop(job_id, None) is not None:
            self._workflow_skip_pending = None
            self.operation_failed.emit(error.message)
            return
        if job_id == self._open_job_id:
            self._open_job_id = None
            self._pending_loaded_manifest = None
            self.operation_failed.emit(error.message)
            self._advance_close()
        elif job_id == self._new_job_id:
            self._new_job_id = None
            self.operation_failed.emit(error.message)
            if self._setup is not None:
                self.shell.show_route("samples")
            self._advance_close()
        elif job_id == self._status_job_id:
            self._status_job_id = None
            terminal_status = job_id == self._terminal_status_job_id
            if self._run_job_id is not None and not terminal_status:
                # Initialization may still be publishing its first progress
                # record. The read after Run finishes owns the final diagnosis.
                if self.session is not None and self.session.pending_status_operation_id == job_id:
                    self.session.pending_status_operation_id = None
                return
            if terminal_status:
                self._terminal_status_job_id = None
                if hasattr(self, "run_page"):
                    self.run_page.invalidate_actions()
            reason = self._close_pause_failure if terminal_status else None
            detail = error.message
            if terminal_status:
                self._close_pause_failure = None
                if self._pending_run_failure is not None:
                    reason = self._pending_run_failure
                    self._pending_run_failure = None
                    detail = "Progress check also failed:\n" + detail
            if self.session is not None and self.session.pending_status_operation_id == job_id:
                self.session.pending_status_operation_id = None
                self.operation_failed.emit(detail if reason is None else f"{reason}\n\n{detail}")
            elif terminal_status:
                self.operation_failed.emit(detail if reason is None else f"{reason}\n\n{detail}")
            if terminal_status and self._close_pending:
                self._close_pending = False
                self.shell.statusBar().clearMessage()
        elif job_id == self._run_job_id:
            self._run_job_id = None
            failure = ("Revised options could not be applied.\n" if revision_job is not None
                       else "Analysis could not start or continue.\n") + error.message
            if hasattr(self, "run_page"):
                self.run_page.set_operation_error(failure, error.diagnostic)
            if (
                self.session is not None
                and self._pause_job_id is None
                and self._terminal_status_job_id is None
                and not self.session.may_close_immediately()
            ):
                self._status_epoch += 1
                try:
                    self.refresh_status()
                    self._terminal_status_job_id = self._status_job_id
                    self._pending_run_failure = None if revision_error_displayed else failure
                except RuntimeError as exc:
                    self._close_pending = False
                    self.operation_failed.emit(f"{failure}\n\nProgress check also failed:\n{exc}")
            else:
                if not revision_error_displayed:
                    self.operation_failed.emit(failure)
            self._advance_close()
        elif job_id == self._pause_job_id:
            self._pause_job_id = None
            if self.session is not None:
                self._begin_mutation(self.session)
            if self._close_pending and self.session is not None:
                self._close_pause_failure = error.message
                self._status_epoch += 1
                try:
                    self.refresh_status()
                    self._terminal_status_job_id = self._status_job_id
                except RuntimeError as exc:
                    self._close_pending = False
                    self._close_pause_failure = None
                    self.shell.statusBar().clearMessage()
                    self.operation_failed.emit(f"{error.message}; {exc}")
            else:
                self._close_pending = False
                self.operation_failed.emit(error.message)
        elif job_id == self._cancel_job_id:
            self._cancel_job_id = None
            self.operation_failed.emit(error.message)
            self._advance_close()
        elif job_id == self._response_job_id:
            self._response_job_id = None
            self._cutoff_assignment_job_id = None
            self._continue_session = None
            if self._cutoff_saved_page is not None:
                self._saved_supervised_review.assignment_finished(self._cutoff_saved_page, error.message)
                self._cutoff_saved_page = None
            elif hasattr(self, "reviews_page"):
                self.reviews_page.set_response_failure(error.message)
            self._response_refresh_pending = True
            self.operation_failed.emit(error.message)
            self._status_epoch += 1
            self.refresh_status()
        elif job_id == self._results_job_id:
            self._results_job_id = None
            self.operation_failed.emit(error.message)


__all__ = ["QtDesktopController", "SetupDiscardCloseRequest"]
