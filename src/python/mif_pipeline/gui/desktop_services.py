"""Presentation-neutral service ports for desktop batch workflows.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from collections.abc import Mapping

if TYPE_CHECKING:
    from mif_pipeline.orchestration.batch_dag import BatchPlan
    from mif_pipeline.orchestration.batch_interactions import (
        BatchInteractionApplicationResult,
        BatchInteractionPage,
        BatchInteractionQuery,
        BatchInteractionResponseRequest,
    )
    from mif_pipeline.orchestration.batch_manifest import ValidatedBatchManifest
    from mif_pipeline.orchestration.batch_service_models import (
        BatchResultsSnapshot,
        BatchRetryChoice,
        BatchReviewPlots,
        BatchReviewPlot,
        BatchSavedStageResults,
    )
    from mif_pipeline.orchestration.batch_service import (
        BatchCancellationResult,
        BatchPauseReceipt,
        BatchRunReport,
        BatchStatusSnapshot,
    )


# ==============================================================================
#                                Desktop services
# ==============================================================================


class BatchServicePort(Protocol):
    """Public batch-control boundary consumed by desktop presentation code."""

    def current_plan(self, batch_id: str, *, expected_head_sha256: str) -> BatchPlan: ...

    def current_manifest(self, batch_id: str, *, expected_head_sha256: str) -> ValidatedBatchManifest: ...

    def validate(self, manifest_path: Path) -> ValidatedBatchManifest: ...

    def plan(self, manifest_path: Path) -> BatchPlan: ...

    def run(self, manifest_path: Path) -> BatchRunReport: ...

    def pause(self, batch_id: str) -> BatchPauseReceipt: ...

    def resume(self, batch_id: str) -> BatchRunReport: ...

    def continue_run(self, batch_id: str, *, expected_head_sha256: str) -> BatchRunReport: ...

    def stage_option_targets(self, batch_id: str, *, node_ids: tuple[str, ...], expected_head_sha256: str): ...

    def stage_options(self, batch_id: str, *, node_id: str, expected_head_sha256: str): ...

    def preview_stage_revision(self, batch_id: str, *, node_id: str, expected_head_sha256: str, settings): ...

    def revise_stage(self, batch_id: str, *, node_id: str, expected_head_sha256: str, settings) -> BatchStatusSnapshot: ...

    def retry_choices(
        self, batch_id: str, *, expected_head_sha256: str
    ) -> tuple[BatchRetryChoice, ...]: ...

    def retry(
        self,
        batch_id: str,
        *,
        node_id: str,
        expected_head_sha256: str,
        selected_failed_terminal_record_sha256: str | None = None,
        selected_pending_authorization_sha256: str | None = None,
    ) -> BatchRunReport: ...

    def status(self, batch_id: str) -> BatchStatusSnapshot: ...



    def results(self, batch_id: str, *, expected_head_sha256: str) -> BatchResultsSnapshot: ...

    def saved_stage_results(self, batch_id: str, *, expected_head_sha256: str) -> BatchSavedStageResults: ...

    def measurement_field(self, batch_id: str, *, expected_head_sha256: str,
        artifact_ref_sha256: str, figure_options: Mapping[str, object]) -> dict: ...

    def kronos_cell_preview(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str, settings: Mapping[str, object]) -> dict: ...

    def secondary_marker_review(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str, options: Mapping[str, object]) -> dict: ...

    def secondary_marker_field(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str, options: Mapping[str, object]) -> dict: ...

    def marker_field(self, batch_id: str, *, interaction_request_id: str | None = None,
        artifact_ref_sha256: str | None = None,
        expected_state_head_sha256: str, figure_options: Mapping[str, object]) -> dict: ...

    def preview_marker_combinations(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str, phenotype_definition: Mapping[str, object],
        pattern_page: Mapping[str, object] | None = None) -> dict: ...

    def cluster_model_view(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str) -> dict | None: ...

    def cluster_field(self, batch_id: str, *, interaction_request_id: str,
        expected_state_head_sha256: str, figure_options: Mapping[str, object]) -> dict: ...

    def saved_stage_figures(
        self, batch_id: str, *, expected_head_sha256: str,
        artifact_ref_sha256: str, figure_id: str, figure_options: Mapping[str, object] | None = None,
    ) -> tuple[BatchReviewPlot, ...]: ...

    def select_saved_figure_region(self, batch_id: str, *, expected_head_sha256: str,
        artifact_ref_sha256: str, figure_id: str, figure_options: Mapping[str, object] | None,
        runner: object) -> dict[str, int] | None: ...

    def saved_project_path(self, batch_id: str, *, expected_head_sha256: str) -> Path: ...

    def paired_image_views(
        self, batch_id: str, *, expected_head_sha256: str, project_path: Path
    ) -> tuple[dict, ...]: ...

    def saved_supervised_review(self, batch_id: str, *, artifact_ref_sha256: str,
        expected_state_head_sha256: str) -> dict: ...

    def assign_supervised_cutoff(self, batch_id: str, *, interaction_request_id: str | None = None,
        artifact_ref_sha256: str | None = None, expected_state_head_sha256: str,
        selection: Mapping[str, object]) -> BatchStatusSnapshot: ...

    def supervised_prediction_page(self, batch_id: str, *, interaction_request_id: str | None = None,
        artifact_ref_sha256: str | None = None,
        expected_state_head_sha256: str, settings: Mapping[str, object]) -> dict: ...

    def review_plots(
        self,
        batch_id: str,
        *,
        interaction_request_id: str,
        expected_state_head_sha256: str,
        umap_settings: Mapping[str, object] | None = None,
        attribute: str = "cluster_id",
        marker_gallery_settings: Mapping[str, object] | None = None,
        combination_gallery_settings: Mapping[str, object] | None = None,
        supervised_gallery_settings: Mapping[str, object] | None = None,
        prediction_review_settings: Mapping[str, object] | None = None,
        cluster_gallery_settings: Mapping[str, object] | None = None,
    ) -> BatchReviewPlots: ...

    def execution_status_if_present(self, batch_id: str) -> BatchStatusSnapshot | None: ...

    def list_interactions(
        self,
        batch_id: str,
        query: BatchInteractionQuery,
    ) -> BatchInteractionPage: ...

    def align_paired_image(self, batch_id: str, request, options): ...

    def apply_interaction_response(
        self,
        batch_id: str,
        request: BatchInteractionResponseRequest,
    ) -> BatchInteractionApplicationResult: ...

    def cancel(self, batch_id: str) -> BatchCancellationResult: ...


class BatchServiceProvider(Protocol):
    """Resolves the single service authority for a manifest or workspace."""

    def for_manifest(self, manifest_path: Path) -> BatchServicePort: ...

    def for_workspace(self, output_root: Path, batch_id: str) -> BatchServicePort: ...


class DesktopFileOperations(Protocol):
    """Local presentation requests for persisted desktop artifacts."""

    def open_qupath(self, project_path: Path, *, paired_views: tuple[dict, ...] = ()) -> object: ...

    def run_registration_editor(self, session: object) -> object: ...

    def select_saved_figure_region(self, service: BatchServicePort, batch_id: str,
        expected_head: str, artifact_ref: str, figure_id: str,
        options: Mapping[str, object] | None) -> dict[str, int] | None: ...

    def require_qupath_stopped(self) -> None: ...

    def open_plot(self, plot_path: Path) -> object: ...

    def reveal_path(self, path: Path) -> object: ...


__all__ = ["BatchServicePort", "BatchServiceProvider", "DesktopFileOperations"]
