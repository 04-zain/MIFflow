"""Execute and review one saved analysis through a single workspace-scoped service.

The service validates manifests, prepares plans, admits ready work, records completed
attempts and presents required decisions. Run, pause, resume and retry share the same
state/project owners. Accepted responses can extend the executable plan without
reconstructing or overwriting the history of completed stages."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Event, RLock, Thread
from types import MappingProxyType
from typing import Callable, Literal
from uuid import uuid4

from mif_containment.client import ContainmentCompatibilityExpectationV1
from mif_pipeline.io import selected_offline_bioformats_inventory
from mif_pipeline.io.bioformats_prerequisite import InventoryIncompatible, InventoryMissing
from mif_pipeline.io.bioformats_runtime_lock import require_approved_bioformats_inventory
from mif_pipeline.pipeline_core import InteractionAction, InteractionReason, ROIAnalysisError, StageResult, StageStatus
from mif_pipeline.preprocessing.unmixing.engine import RegionExecutionPolicy
from mif_pipeline.qupath_runtime.runner import LinuxContainmentProfile
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding, qupath_command
from mif_pipeline.qupath_runtime.scripts import (
    QuPathScriptAsset,
    registered_script_asset_sha256,
)

from .batch_artifacts import (
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchPreparedDispatchAuthority,
    DispatchMode,
    StageAdapterRegistry,
    artifact_ref_set_sha256,
)
from .accepted_analysis_domain_records import (
    decode_committed_accepted_analysis_domain,
)
from .batch_dispatch import BatchBoundDispatchPreparation
from .batch_dag import BatchPlan, ResourceKind, expand_batch_plan, plan_batch
from .batch_semantic_profile_import import import_batch_semantic_profiles
from .batch_reference_view import is_reference_handoff, issue_reference_only_view
from .batch_interactions import (
    BatchAggregateInteractionGroup,
    BatchAggregateTargetApplication,
    BatchInteractionApplicationResult,
    BatchInteractionError,
    BatchInteractionGroup,
    BatchInteractionPage,
    BatchInteractionQuery,
    BatchInteractionResponseRequest,
    BatchInteractionTarget,
    BatchTargetApplication,
    apply_group_response,
    group_interactions,
    page_interactions,
    publish_interaction_requests,
    project_batch_interaction_target,
)
from .batch_manifest import (
    BatchManifest,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
    validate_manifest,
)
from .batch_pause import (
    BatchPauseReceipt,
    consume_quiescent_pause,
    paused_response_authority,
    publish_quiescent_pause,
)
from .batch_phenotyping.adapters import build_phenotyping_stage_specs
from .batch_preprocessing_contracts import build_preprocessing_stage_specs
from .setup.batch_prerequisites import BatchPrerequisiteItem
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from .setup.batch_prerequisites import bind_batch_prerequisites
from .setup.batch_prerequisites import collect_batch_prerequisites
from .batch_resources import BatchResourceDemand
from .batch_scheduler import BatchScheduler
from .batch_service_dependencies import (
    BatchRuntimeDependencies,
    build_batch_stage_registry,
    open_batch_runtime_dependencies,
    registered_batch_interaction_owner_inventory,
)
from .batch_service_registry import Task15RuntimeStageRegistry
from .batch_service_models import (
    BatchCancellationResult,
    BatchReviewPlot,
    BatchReviewPlots,
    BatchResultsSnapshot,
    BatchSavedStageResults,
    BatchRetryChoice,
    BatchRunReport,
    BatchStatusSnapshot,
    BatchSummaryRow,
)
from .batch_stage_adapters import build_image_stage_specs
from .batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchExecutionState,
    BatchHeadConflictError,
    BatchMaterialAuthorityUpdate,
    BatchStateHead,
    BatchWorkspaceLayout,
    BatchWorkspaceLease,
    BatchStateError,
    ScheduledNodeResult,
    batch_interaction_request_id,
)
from .batch_phenotyping.review_authority import (
    CLUSTER_REVIEW_RESPONSE_VALIDATOR_ID,
    CLUSTER_REVIEW_RESPONSE_VALIDATOR_VERSION,
)
from .batch_training.stage_adapters import (
    TRAINING_PARENT_NODE_KINDS,
    build_training_stage_adapters,
)
from .batch_failed_retry import (
    PENDING_PYTHON_RETRY_KINDS,
    is_failed_retry_node_kind,
    is_project_request_failure,
    authorize_failed_stage_retry,
    reopen_native_training_failure,
    reopen_project_stage_failure,
    reopen_project_request_failure,
    revalidate_failed_stage_retry,
)

# ==============================================================================
#                                 Batch service
# ==============================================================================


class BatchServiceError(RuntimeError):
    """The requested service operation is foreign, unavailable, or unsafe."""


class BatchReviewPlotError(BatchServiceError):
    """A local derived review figure is unavailable or has changed."""

    code = "batch_review_plot_unavailable"


_REVIEW_PLOT_CACHE_LIMIT_BYTES = 64 * 1024 * 1024
_REVIEW_PLOT_RENDERER_VERSION = "classifier-review-tables/v6"


class BatchPrerequisiteAdmissionError(BatchServiceError):
    """Fresh owner observations do not admit the retained requirements."""

    code: Literal["batch_prerequisite_not_ready"] = "batch_prerequisite_not_ready"
    prerequisites: tuple[BatchPrerequisiteItem, ...]

    def __init__(self, prerequisites: tuple[BatchPrerequisiteItem, ...]) -> None:
        self.prerequisites = prerequisites
        super().__init__("batch prerequisites are non-ready or differ from retained requirements")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise BatchServiceError("batch service canonical mappings require string keys")
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_export_json(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _export_json(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _application_stage_specs(*, include_secondary_definitions: bool = False) -> StageAdapterRegistry:
    from .batch_secondary_definitions import build_secondary_definition_stage_spec
    from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

    with schema_validation_snapshot():
        return StageAdapterRegistry(
            (
                *build_preprocessing_stage_specs(),
                *build_training_stage_adapters(),
                *build_image_stage_specs(),
                *build_phenotyping_stage_specs(),
                *((build_secondary_definition_stage_spec(),) if include_secondary_definitions else ()),
            )
        )


def batch_service_runtime_stage_adapter_kinds() -> tuple[str, ...]:
    """Read the exact stage specifications consumed by runtime composition."""
    return tuple(sorted(spec.node_kind for spec in _application_stage_specs(include_secondary_definitions=True).specs))


def _available_artifacts(snapshot) -> Mapping[str, BatchArtifactRef]:
    return MappingProxyType(
        {item.artifact_ref_sha256: item for item in snapshot.accepted_artifacts}
    )


def _current_saved_result(snapshot, node) -> BatchArtifactRef:
    """Reuse the snapshot's admitted result for this exact current saved producer."""
    from .batch_artifacts import validate_committed_artifact_authority_set
    from .batch_saved_result_retirement import retirement_contracts

    contracts = retirement_contracts()
    candidates = tuple(authority for authority in snapshot.committed_artifact_authorities
        if authority.artifact_ref.producer_node_id == node.node_id
        and authority.artifact_ref.artifact_kind in node.declared_output_kinds
        and authority.artifact_ref.identity_contract == contracts.get(authority.artifact_ref.artifact_kind))
    if len(candidates) != 1:
        raise BatchServiceError("The affected saved result has no unique current producer artifact.")
    authority, = validate_committed_artifact_authority_set(candidates)
    ref = authority.artifact_ref
    if (authority.state_head_sha256 != snapshot.head.sha256
            or authority.active_plan_sha256 != snapshot.plan.plan_sha256
            or authority.producer_node_kind != node.kind
            or (ref.scope.project_entry_id,) != node.project_entry_ids
            or (ref.scope.sample_id,) != node.participant_ids
            or ref.scope.analysis_domain_bindings != node.analysis_domain_bindings
            or (node.section_target is not None
                and ref.owning_identity.get("section_id") != node.section_target.section_id)):
        raise BatchServiceError("The affected saved result differs from its current task and tissue section.")
    return ref


@dataclass(slots=True)
class _ActiveWave:
    batch_id: str
    workspace_lease: BatchWorkspaceLease
    scheduler: BatchScheduler
    post_wave_done: Event = field(default_factory=Event)
    pause_done: Event = field(default_factory=Event)
    post_wave_error: BaseException | None = None


class _ProviderUse:
    def __init__(self, provider: "BatchServiceProvider") -> None:
        self._provider = provider

    def __enter__(self) -> None:
        self._provider._begin_use()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._provider._finish_use()


def _selected_manifest_for_authoring(store, data: bytes, snapshot, parsed: BatchManifest) -> ValidatedBatchManifest:
    """Keep an authenticated historical analysis file as a locator for its current source."""
    canonical = json.dumps(parsed.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if not any(store._load_plan_snapshot(item).normalized_manifest_sha256 == digest
               for item in snapshot.head.plan_history_sha256s):
        raise BatchServiceError("This analysis file is not part of the saved analysis. Open the original analysis folder.")
    retained = store.retained_manifest(digest)
    if data != retained.original_manifest_bytes:
        raise BatchServiceError("The saved analysis file changed. Open the original analysis folder.")
    return snapshot.manifest


class BatchService:
    def __init__(
        self,
        *,
        provider: "BatchServiceProvider",
        workspace_layout: BatchWorkspaceLayout,
        dependencies: BatchRuntimeDependencies,
        manifest_path: Path | None,
    ) -> None:
        self._provider = provider
        self._workspace_layout = workspace_layout
        self._dependencies = dependencies
        self._manifest_path = manifest_path
        self._lock = RLock()
        self._activity_lock = RLock()
        self._active_stage_counts: dict[str, int] = {}
        self._validated_bytes_by_path: dict[Path, bytes] = {}
        self._active_wave: _ActiveWave | None = None
        self._pause_blocks_admission = False
        self._status_stage_specs: StageAdapterRegistry | None = None
        self._tissue_review_summaries: dict[str, Mapping[str, object]] = {}
        self._opened_processed_images = None

    @property
    def active_stage_kinds(self) -> tuple[str, ...]:
        """Read current processing activity without opening saved analysis files."""
        with self._activity_lock:
            return tuple(self._active_stage_counts)

    def _stage_activity(self, kind: str, started: bool) -> None:
        with self._activity_lock:
            count = self._active_stage_counts.get(kind, 0) + (1 if started else -1)
            if count > 0:
                self._active_stage_counts[kind] = count
            else:
                self._active_stage_counts.pop(kind, None)

    def current_manifest(self, batch_id: str, *, expected_head_sha256: str) -> ValidatedBatchManifest:
        """Return the selected retained inputs for an already authenticated desktop status."""
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            head = store.load_head()
            if head.sha256 != expected_head_sha256:
                raise BatchServiceError("The analysis changed. Refresh before opening its saved inputs.")
            return store.retained_manifest(head.normalized_manifest_sha256)

    def current_plan(self, batch_id: str, *, expected_head_sha256: str) -> BatchPlan:
        """Read the plan belonging to the same saved state as the displayed status."""
        with self._provider._use():
            self._require_batch(batch_id)
            return self._dependencies.runtime.state_store.reopen_execution_snapshot(
                expected_head_sha256=expected_head_sha256
            ).plan

    def validate(self, manifest_path: Path) -> ValidatedBatchManifest:
        with self._provider._use():
            return self._validate(manifest_path, remember=True)

    def plan(self, manifest_path: Path) -> BatchPlan:
        with self._provider._use():
            return self._plan(manifest_path)

    def run(self, manifest_path: Path) -> BatchRunReport:
        with self._provider._use():
            validated = self._validate(manifest_path, remember=False)
            return self._managed_worker(lambda: self._run_worker(validated))

    def export_accepted_classifier_record_set(
        self, batch_id: str, *, expected_head_sha256: str,
        classifier_record_set_ref_sha256: str, destination: Path,
    ):
        """Export one accepted native set without changing its retained analysis."""
        from .batch_training.accepted_model_reuse import export_accepted_native_classifier_set

        with self._provider._use():
            self._require_batch(batch_id)
            training = self._dependencies.training
            return export_accepted_native_classifier_set(
                state_store=training.state_store,
                record_store=training.classifier_record_set_store,
                plan_store=training.training_plan_set_store,
                expected_head_sha256=expected_head_sha256,
                record_ref_sha256=classifier_record_set_ref_sha256,
                destination=destination,
            )

    def pause(self, batch_id: str) -> BatchPauseReceipt:
        with self._provider._use():
            self._require_batch(batch_id)
            with self._lock:
                wave = self._active_wave
                if wave is None or wave.batch_id != batch_id:
                    raise BatchServiceError("pause requires an active batch wave")
                if self._pause_blocks_admission:
                    raise BatchServiceError("graceful pause is already in progress")
                self._pause_blocks_admission = True
            try:
                return self._publish_wave_pause(wave, wait_for_post_wave=True)
            except BaseException:
                with self._lock:
                    self._pause_blocks_admission = False
                raise
            finally:
                wave.pause_done.set()

    def _publish_wave_pause(
        self,
        wave: _ActiveWave,
        *,
        wait_for_post_wave: bool,
    ) -> BatchPauseReceipt:
        """Publish the existing quiescent proof while this wave retains its leases."""
        scheduler = wave.scheduler
        receipt = scheduler.quiesce()
        if wait_for_post_wave:
            wave.post_wave_done.wait()
            if wave.post_wave_error is not None:
                raise BatchServiceError("batch wave failed before pause") from wave.post_wave_error
        store = self._dependencies.runtime.state_store
        if store.load_head().sha256 != receipt.state_head_sha256:
            receipt = scheduler.quiesce()
        if not self._dependencies.runtime.wait_for_project_stage_quiescence():
            raise BatchServiceError("Task-9 project stage did not drain")
        with store.current_project_control(
            operation="pause", lease=wave.workspace_lease
        ) as control:
            scheduler.assert_no_admitted_work()
            if not self._dependencies.runtime.wait_for_project_stage_quiescence():
                raise BatchServiceError("Task-9 project stage did not remain drained")
            control.project_processes.assert_all_drained(
                batch_id=control.head.batch_id,
                project_identity_sha256=control.head.project_identity_sha256,
            )
            return publish_quiescent_pause(
                state_store=store,
                batch_id=wave.batch_id,
                pausing_session_id="batch-service-pause-" + uuid4().hex,
                scheduler=scheduler,
                quiescence_receipt=receipt,
                workspace_lease=wave.workspace_lease,
                project_lease=control.project_lease,
                project_processes=control.project_processes,
            )

    def resume(self, batch_id: str) -> BatchRunReport:
        with self._provider._use():
            self._require_batch(batch_id)
            return self._managed_worker(lambda: self._resume_worker(batch_id))

    def continue_run(self, batch_id: str, *, expected_head_sha256: str) -> BatchRunReport:
        """Continue this displayed active revision through ordinary retained-work checks.

        We preserve pending authorizations and reopen their execution records.
        Existing process and project recovery determine whether work can resume;
        this action neither invents a failed terminal nor grants a new retry.
        """
        with self._provider._use():
            self._require_batch(batch_id)
            return self._managed_worker(
                lambda: self._continue_worker(expected_head_sha256)
            )




    def abandon_interrupted_segmentation(self, batch_id: str, *, node_id: str,
                                        expected_head_sha256: str):
        """Explicitly restore accepted material after trusted-local observed absence.

        The abandoned attempt retains an unknown original exit and no outputs.
        Segmentation options then admit a fresh attempt through normal revision.
        """
        from .batch_native_abandon import abandon_interrupted_segmentation
        with self._provider._use():
            self._require_batch(batch_id)
            return self._managed_worker(lambda: abandon_interrupted_segmentation(
                self, node_id=node_id, expected_head_sha256=expected_head_sha256))

    def retry(
        self,
        batch_id: str,
        *,
        node_id: str,
        expected_head_sha256: str,
        selected_failed_terminal_record_sha256: str | None = None,
        selected_pending_authorization_sha256: str | None = None,
    ) -> BatchRunReport:
        """Retry a selected failure or continue one exact pending calculation."""
        with self._provider._use():
            self._require_batch(batch_id)
            if (selected_failed_terminal_record_sha256 is None) == (selected_pending_authorization_sha256 is None):
                raise BatchServiceError("Select one saved failure or pending calculation to retry.")
            if selected_pending_authorization_sha256 is not None:
                return self._managed_worker(lambda: self._retry_pending_python_worker(
                    batch_id, node_id=node_id, expected_head_sha256=expected_head_sha256,
                    selected_authorization_sha256=selected_pending_authorization_sha256))
            return self._managed_worker(
                lambda: self._retry_worker(
                    batch_id,
                    node_id=node_id,
                    expected_head_sha256=expected_head_sha256,
                    selected_failed_terminal_record_sha256=(selected_failed_terminal_record_sha256),
                )
            )

    def stage_options(self, batch_id: str, *, node_id: str,
                             expected_head_sha256: str):
        """Read the current selection for one settled section task or settled review."""
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            return self._stage_options(snapshot, node_id, resolve_saved_settings=True)

    def stage_option_targets(self, batch_id: str, *, node_ids: tuple[str, ...], expected_head_sha256: str):
        """Read scope labels and saved selections for one current chooser."""
        with self._provider._use():
            self._require_batch(batch_id)
            snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            choices = (self._stage_options(snapshot, node_id, resolve_saved_settings=True) for node_id in node_ids)
            return tuple({item.node_id: item for item in choices}.values())

    def _stage_options(self, snapshot, node_id: str, *, resolve_saved_settings: bool = False):
        from .batch_stage_settings import BatchStageOptions
        from .batch_settled_review_revision import SETTLED_REVIEW_KINDS, STAGE_OPTIONS_KINDS, SettledReviewRevision, review_target
        from .batch_segmentation_setup import project_segmentation_setup
        store = self._dependencies.runtime.state_store
        node = next((item for item in snapshot.plan.nodes if item.node_id == node_id), None)
        if node is not None:
            if resolve_saved_settings:
                from .batch_tissue_saved_settings import saved_tissue_view_target
                node = saved_tissue_view_target(snapshot, node)
            node = review_target(snapshot.plan, node)
            node_id = node.node_id
        if (node is None or node.kind not in STAGE_OPTIONS_KINDS
                or (node.kind not in SETTLED_REVIEW_KINDS and node.kind != "inspection" and node.section_target is None)):
            raise BatchServiceError("Select a current task with saved options or a supported completed review.")
        if (snapshot.head.node_status_by_id.get(node_id) not in {StageStatus.COMPLETED, StageStatus.FAILED}
                and not (node.kind == "reference_unmixing" and snapshot.head.node_status_by_id.get(node_id) is StageStatus.NEEDS_INTERACTION)):
            raise BatchServiceError("Options can be revised after the current task finishes or stops.")
        if node_id in snapshot.head.invalidated_node_ids:
            raise BatchServiceError("This task has been replaced. Refresh and select its current revision.")
        if node.kind == "inspection":
            from .batch_source_revision import source_review_revision
            if (snapshot.head.operational_rebinding_sha256 is not None
                    or store.layout.root != snapshot.manifest.manifest.output_root / snapshot.head.batch_id):
                raise BatchServiceError("Correct the image in the original analysis folder; relocated source correction is not available.")
            selected = next(item for item in snapshot.manifest.manifest.samples if (item.sample_id,) == node.participant_ids)
            return BatchStageOptions(snapshot.head.batch_id, snapshot.head.sha256, node_id,
                selected.sample_id, None, node.kind, selected, participant_ids=node.participant_ids,
                inspection_review=(source_review_revision(snapshot, node)
                    if snapshot.head.node_status_by_id[node_id] is StageStatus.COMPLETED else None))
        if node.kind in SETTLED_REVIEW_KINDS:

            attempts = snapshot.head.attempt_ids_by_node.get(node_id, ())
            if not attempts:
                raise BatchServiceError("This review has no retained completed or failed attempt.")
            terminal_sha256 = snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]]
            prior = node.settled_review_revision
            selected = SettledReviewRevision(node_id, 1 if prior is None else prior.revision + 1, terminal_sha256,
                None if prior is None else prior.paired_reference)
            from .batch_tissue_saved_settings import TISSUE_SETTINGS_KINDS, saved_tissue_settings
            saved_tissue = (saved_tissue_settings(snapshot, node)
                            if resolve_saved_settings and node.kind in TISSUE_SETTINGS_KINDS else None)
            saved_unmixing = None
            if resolve_saved_settings and node.kind == "reference_unmixing":
                from .batch_reference_unmixing_execution import saved_unmixing_settings
                saved_unmixing = saved_unmixing_settings(snapshot, node)
            saved_analysis_mode = None
            saved_secondary = None
            if (resolve_saved_settings and node.kind == "secondary_structure_definition"
                    and snapshot.head.node_status_by_id[node_id] is StageStatus.COMPLETED):
                from .batch_secondary_definitions import saved_secondary_definition
                saved_secondary = saved_secondary_definition(snapshot, node, store)
            saved_clustering = None
            if node.kind in {'clustering_per_slide','clustering_pooled'}:
                from .batch_phenotyping.refinement_execution import refit_options
                refit,saved_clustering=refit_options(snapshot,node)
                selected=replace(selected,cluster_refit=refit)
            if node.kind == "analysis_domain_selection":
                saved_analysis_mode = next((
                    decode_committed_accepted_analysis_domain(item).selection_mode
                    for item in snapshot.committed_artifact_authorities
                    if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
                    and item.artifact_ref.producer_node_id == node.node_id
                ), None)
            return BatchStageOptions(snapshot.head.batch_id, snapshot.head.sha256, node_id,
                node.participant_ids[0], None, node.kind, selected,
                participant_ids=node.participant_ids, saved_tissue=saved_tissue,
                saved_unmixing=saved_unmixing, saved_analysis_mode=saved_analysis_mode,
                saved_clustering=saved_clustering, saved_secondary=saved_secondary)
        sample = next(item for item in snapshot.manifest.enabled_samples
                      if (item.sample_id,) == node.participant_ids)
        if node.kind == "segmentation":
            if node.segmentation_settings_revision is not None:
                selected = node.segmentation_settings_revision.settings
            elif sample.config.segmentation is not None:
                selected = sample.config.segmentation
            else:
                attempts = snapshot.head.attempt_ids_by_node.get(node_id, ())
                if not attempts:
                    raise BatchServiceError("This task has no saved segmentation selection.")
                attempt = store.load_attempt(snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]])
                authorization = store.load_authorization(attempt.authorization_sha256)
                from .batch_segmentation_artifacts import _segmentation_primary_routes
                refs = {ref.artifact_ref_sha256: ref for ref in snapshot.accepted_artifacts}
                _, primary_skipped = _segmentation_primary_routes(
                    tuple(refs[key] for key in attempt.execution_context_binding.input_artifact_ref_sha256s),
                    node.analysis_domain_bindings,
                )
                selected = project_segmentation_setup(
                    config=sample.config, section_id=node.section_target.section_id, node_id=node_id,
                    input_artifact_ref_sha256s=attempt.execution_context_binding.input_artifact_ref_sha256s,
                    validated_response=store.load_validated_interaction_response(authorization),
                    primary_classification_skipped=primary_skipped,
                )
                from mif_pipeline.pipeline_core.config import SegmentationConfig
                if type(selected) is not SegmentationConfig:
                    raise BatchServiceError("This task has no accepted segmentation selection.")
        else:
            from .batch_analysis_preflight import resolve_analysis_channels
            from .batch_measurement_setup import project_measurement_setup
            from .batch_measurement_settings import MeasurementSettings, validate_measurement_settings
            from mif_pipeline.cell_analysis.measurement_setup import configured_measurement_feature_requests
            refs = {ref.artifact_id: ref for ref in snapshot.accepted_artifacts}
            frame = next((refs.get(key) for key in node.input_artifact_ids
                          if refs.get(key) is not None and refs[key].artifact_kind == "analysis_frame"), None)
            if frame is None:
                raise BatchServiceError("The accepted image channels are unavailable. Restore this task's inputs first.")
            store.authenticate_available_artifact(frame)
            from .saved_segmentation_cells import segmentation_cells
            segmentation = next((refs.get(key) for key in node.input_artifact_ids
                if refs.get(key) is not None and refs[key].artifact_kind == "segmentation_result"), None)
            if segmentation is None:
                raise BatchServiceError("The accepted segmentation is unavailable. Restore this task's inputs first.")
            cell_boundary_source = segmentation_cells(store.authenticate_available_artifact(segmentation), store).description["scope"].get("cell_boundary_source")
            channels = resolve_analysis_channels(sample.config, frame, sample_id=sample.sample_id,
                validation_domain_id=node.validation_domain_id)
            if node.measurement_settings_revision is not None:
                selected = node.measurement_settings_revision.settings
                validate_measurement_settings(selected, config=sample.config, channels=channels)
            else:
                attempts = snapshot.head.attempt_ids_by_node.get(node_id, ())
                if not attempts:
                    raise BatchServiceError("This task has no saved measurement selection.")
                attempt = store.load_attempt(snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]])
                authorization = store.load_authorization(attempt.authorization_sha256)
                features = project_measurement_setup(config=sample.config, channels=channels,
                    cell_boundary_source=cell_boundary_source,
                    section_id=node.section_target.section_id, node_id=node_id,
                    input_artifact_ref_sha256s=attempt.execution_context_binding.input_artifact_ref_sha256s,
                    validated_response=(None if configured_measurement_feature_requests(sample.config)
                        else store.load_validated_interaction_response(authorization)))
                if isinstance(features, StageResult):
                    raise BatchServiceError("This task stopped before measurement options were accepted. Retry it to choose those inputs.")
                selected = MeasurementSettings(channels, features)
        from mif_pipeline.cell_analysis.measurement_setup import configured_measurement_feature_requests
        required = tuple(item["feature_id"] for item in configured_measurement_feature_requests(sample.config)) if node.kind == "measurements" else ()
        segmentation_channels = ()
        nuclear_channel_id = None
        if node.kind == "segmentation":
            from .batch_analysis_preflight import resolve_analysis_channels
            frame = next(ref for ref in snapshot.accepted_artifacts
                         if ref.artifact_kind == "analysis_frame" and ref.scope.sample_id == sample.sample_id)
            segmentation_channels = resolve_analysis_channels(sample.config, frame,
                sample_id=sample.sample_id, validation_domain_id=frame.scope.validation_domain_id)
            from .channel_settings import saved_nuclear_channel_id
            nuclear_channel_id = saved_nuclear_channel_id(snapshot, sample_id=sample.sample_id,
                frame_ref=frame, channels=segmentation_channels)
        return BatchStageOptions(snapshot.head.batch_id, snapshot.head.sha256,
            node.node_id, sample.sample_id, node.section_target.section_id, node.kind, selected, required,
            segmentation_channels=segmentation_channels,
            segmentation_nuclear_channel_id=nuclear_channel_id,
            cell_boundary_source=cell_boundary_source if node.kind == "measurements" else None)

    def preview_stage_revision(self, batch_id: str, *, node_id: str,
                                      expected_head_sha256: str, settings):
        """Preview the real current dependency closure without changing saved work."""
        with self._provider._use():
            self._require_batch(batch_id)
            snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot(
                expected_head_sha256=expected_head_sha256)
            return self._preview_stage_revision(snapshot, node_id, settings)

    def _preview_stage_revision(self, snapshot, node_id, settings):
        from .batch_settled_review_revision import SETTLED_REVIEW_KINDS, SettledReviewRevision
        from mif_pipeline.pipeline_core.config import SegmentationConfig
        from .batch_measurement_settings import MeasurementSettings, validate_measurement_settings
        from .batch_stage_settings import BatchStageRevisionPreview, stage_dependants
        options = self._stage_options(snapshot, node_id)
        if options.kind == "inspection":
            if type(settings) is SettledReviewRevision:
                if settings != options.inspection_review:
                    raise BatchServiceError("Cycle review must restart from its displayed current completion.")
            else:
                from .batch_source_revision import revised_source_manifest, source_review_revision
                source_node = next(item for item in snapshot.plan.nodes if item.node_id == options.node_id)
                revised_source_manifest(snapshot.manifest, options.sample_id, settings,
                    revision=source_review_revision(snapshot, source_node),
                    source_sha256_by_sample=snapshot.head.source_sha256_by_sample,
                    state_store=self._dependencies.runtime.state_store)
        elif options.kind == "segmentation":
            if type(settings) is not SegmentationConfig:
                raise BatchServiceError("Segmentation settings must pass the stage settings validator.")
            SegmentationConfig.from_mapping(settings.to_mapping(), field="segmentation")
        elif options.kind in SETTLED_REVIEW_KINDS:
            from .batch_settled_review_revision import SettledReviewRevision

            if options.kind == "registration":
                from .batch_settled_review_revision import validate_paired_revision
                validate_paired_revision(settings, options.settings)
            elif options.kind in {'clustering_per_slide','clustering_pooled'}:
                if (type(settings) is not SettledReviewRevision or settings.cluster_refit is None
                        or replace(settings,cluster_refit=options.settings.cluster_refit)!=options.settings):
                    raise BatchServiceError('Refitting must use the displayed current clustering result.')
                refit=settings.cluster_refit
                saved=options.saved_clustering
                if refit.restore_parent:
                    if not saved['can_restore_parent']:
                        raise BatchServiceError('This result has no selected-cluster refit to discard.')
                else:
                    counts=saved['cluster_counts']
                    if not refit.cluster_ids or not set(refit.cluster_ids)<=set(counts):
                        raise BatchServiceError('Select current clusters to refit.')
                    count=sum(counts[key] for key in refit.cluster_ids)
                    if count<2:
                        raise BatchServiceError('Select at least two cells for refitting.')
                    if refit.method=='flowsom' and refit.method_settings['grid_size']**2>count:
                        raise BatchServiceError(f'This FlowSOM grid needs more than the {count:,} selected cells. Choose a smaller grid.')
            elif type(settings) is not SettledReviewRevision or settings != options.settings:
                raise BatchServiceError("Review must restart from its displayed current completion.")
        else:
            if type(settings) is not MeasurementSettings:
                raise BatchServiceError("Measurement settings must pass the stage settings validator.")
            config = next(item.config for item in snapshot.manifest.enabled_samples if item.sample_id == options.sample_id)
            validate_measurement_settings(settings, config=config, channels=options.settings.channels)
            from mif_pipeline.cell_analysis.measurement_setup import validate_segmentation_compartments
            validate_segmentation_compartments(settings.feature_requests, options.cell_boundary_source)
        affected = stage_dependants(snapshot.plan, options.node_id)
        if options.kind == "registration" and settings.paired_reference != options.settings.paired_reference:
            # A replaced preparation is no longer a current input. A plain
            # alignment revisit retains its exact completed preparation.
            affected |= frozenset(item.node_id for item in snapshot.plan.nodes
                if item.kind == "paired_image_preparation"
                and item.participant_ids == options.participant_ids)
        blocked = None
        with self._lock:
            if self._active_wave is not None:
                blocked = "Wait for processing to finish, or pause it before revising options."
        if (snapshot.head.pending_authorization_sha256_by_node if options.kind == "inspection"
                else affected.intersection(snapshot.head.pending_authorization_sha256_by_node)):
            blocked = "Continue the retained attempt or establish its native exit before revising these options."
        if options.kind not in SETTLED_REVIEW_KINDS and any(snapshot.head.node_status_by_id.get(item) is StageStatus.NEEDS_INTERACTION
               for item in affected):
            blocked = "Complete the affected pending review before revising these options."
        from .batch_saved_result_retirement import retirement_kinds
        display_sources = tuple(sorted((
            _current_saved_result(snapshot, item)
            for item in snapshot.plan.nodes
            if item.node_id in affected and item.kind in retirement_kinds(options.kind)
            and snapshot.head.node_status_by_id.get(item.node_id) is StageStatus.COMPLETED
        ), key=lambda ref: ref.artifact_ref_sha256))
        affected_kinds = {item.kind for item in snapshot.plan.nodes if item.node_id in affected}
        affected_samples = {sid for item in snapshot.plan.nodes if item.node_id in affected for sid in item.participant_ids}
        # Deferred tasks have no result to retire yet. Show potential consumers
        # separately from the exact closure rather than inventing concrete IDs.
        deferred = {item.node_kind for item in snapshot.plan.deferred_nodes
                    if set(item.prerequisite_node_kinds).intersection(affected_kinds)
                    and set(item.participant_ids).intersection(affected_samples)}
        changed = True
        while changed:
            consumers = tuple(item for item in snapshot.plan.deferred_nodes
                if set(item.prerequisite_node_kinds).intersection(affected_kinds | deferred)
                and set(item.participant_ids).intersection(affected_samples))
            expanded = {item.node_kind for item in consumers}
            participants = {sid for item in consumers for sid in item.participant_ids}
            changed = not expanded.issubset(deferred) or not participants.issubset(affected_samples)
            deferred.update(expanded)
            affected_samples.update(participants)
        return BatchStageRevisionPreview(options, settings, tuple(sorted(affected)),
            tuple(sorted(item.node_id for item in snapshot.plan.nodes
                         if item.node_id not in affected and snapshot.head.node_status_by_id.get(item.node_id)
                         is StageStatus.COMPLETED)), tuple(sorted(deferred)), blocked, display_sources)

    def revise_stage(self, batch_id: str, *, node_id: str,
                            expected_head_sha256: str, settings) -> BatchStatusSnapshot:
        """Apply a displayed settings change or settled review return through the plan owner."""
        with self._provider._use():
            self._require_batch(batch_id)
            return self._managed_worker(lambda: self._revise_stage_worker(
                node_id, expected_head_sha256, settings))

    def prepare_cell_correction(self, batch_id, **selection):
        """Prepare the selected cell's ordinary native draft before measurements."""
        from .cell_correction_review import prepare_correction
        with self._provider._use():
            return self._managed_worker(lambda: prepare_correction(self, batch_id, **selection))

    def assign_supervised_cutoff(self, batch_id: str, *, interaction_request_id: str | None = None,
                                 artifact_ref_sha256: str | None = None,
                                 expected_state_head_sha256: str, selection) -> BatchStatusSnapshot:
        """Save an explicit image or processing-group assignment through current owners."""
        from .supervised_cutoff_application import assign_cutoff
        with self._provider._use():
            self._require_batch(batch_id)
            return self._managed_worker(lambda: assign_cutoff(self, batch_id,
                interaction_request_id=interaction_request_id, artifact_ref_sha256=artifact_ref_sha256,
                expected_state_head_sha256=expected_state_head_sha256, selection=selection))

    def preview_cell_correction(self, batch_id, **submission):
        """Validate a returned outline without changing accepted science."""
        from .cell_correction_review import prepare_correction_candidate
        with self._provider._use():
            return self._managed_worker(lambda: prepare_correction_candidate(self, batch_id, **submission))

    def accept_cell_correction(self, batch_id, **selection):
        """Select the reviewed correction through the existing native task owner."""
        from .cell_correction_review import accept_correction
        with self._provider._use():
            return self._managed_worker(lambda: accept_correction(self, batch_id, **selection))

    def accept_cell_corrections(self, batch_id, *, selections, expected_head_sha256):
        """Accept the reviewed image corrections in one successor plan."""
        from .cell_correction_review import accept_corrections
        with self._provider._use():
            return self._managed_worker(lambda: accept_corrections(self, batch_id,
                selections=selections, expected_head_sha256=expected_head_sha256))

    def _revise_stage_worker(self, node_id, expected_head_sha256, settings):
        from .batch_settled_review_revision import SETTLED_REVIEW_KINDS, SettledReviewRevision
        from .batch_segmentation_settings import SegmentationSettingsRevision
        store = self._dependencies.runtime.state_store
        with store.acquire_writer_lease("stage-options") as lease:
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            preview = self._preview_stage_revision(snapshot, node_id, settings)
            node_id = preview.options.node_id
            if preview.blocked_reason is not None:
                raise BatchServiceError(preview.blocked_reason)
            supplier = self._provider._prerequisite_inputs
            if supplier is None:
                raise BatchServiceError("The reviewed runtime and input inventory are unavailable.")
            node = next(item for item in snapshot.plan.nodes if item.node_id == node_id)
            selected_manifest = snapshot.manifest
            changing_source = node.kind == "inspection" and type(settings) is not SettledReviewRevision
            if changing_source:
                from .batch_source_revision import revised_source_manifest, source_review_revision
                source_revision = source_review_revision(snapshot, node)
                selected_manifest = revised_source_manifest(snapshot.manifest, preview.options.sample_id, settings,
                    revision=source_revision, source_sha256_by_sample=snapshot.head.source_sha256_by_sample, state_store=store)
                revision = {"settled_review_revision": source_revision}
            elif node.kind == "segmentation":
                prior = node.segmentation_settings_revision
                revision = {"segmentation_settings_revision": SegmentationSettingsRevision(
                    node_id, 1 if prior is None else prior.revision + 1, settings)}
            elif node.kind in SETTLED_REVIEW_KINDS:
                revision = {"settled_review_revision": settings}
            else:
                from .batch_measurement_settings import MeasurementSettingsRevision
                from .batch_settled_review_revision import SettledReviewRevision
                prior = node.measurement_settings_revision
                reference_predecessor = None if prior is None else prior.supervised_reference_predecessor
                candidates = tuple(item for item in snapshot.plan.nodes
                    if item.kind == "supervised_phenotype_training_plan_review"
                    and item.node_id in preview.affected_node_ids
                    and snapshot.head.node_status_by_id.get(item.node_id) is StageStatus.COMPLETED)
                if len(candidates) == 1:
                    candidate = candidates[0]
                    attempts = snapshot.head.attempt_ids_by_node.get(candidate.node_id, ())
                    if attempts:
                        terminal_sha = snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]]
                        terminal = store.load_attempt(terminal_sha)
                        if terminal.node_id == candidate.node_id and any(
                                ref.artifact_kind == "training_plan_set" for ref in terminal.output_artifacts):
                            settled = candidate.settled_review_revision
                            reference_predecessor = SettledReviewRevision(candidate.node_id,
                                1 if settled is None else settled.revision + 1, terminal_sha)
                revision = {"measurement_settings_revision": MeasurementSettingsRevision(
                    node_id, 1 if prior is None else prior.revision + 1, settings, reference_predecessor)}
            from .batch_saved_result_retirement import SavedResultRetirement
            sources_by_entry = {}
            for ref in preview.saved_result_retirement_sources:
                sources_by_entry.setdefault(ref.scope.project_entry_id, []).append(ref)
            retirements = tuple(SavedResultRetirement(node_id, snapshot.plan.plan_sha256, tuple(refs))
                                for _entry, refs in sorted(sources_by_entry.items()))
            affected = frozenset(preview.affected_node_ids)
            retained = tuple(ref for ref in snapshot.accepted_artifacts if ref.producer_node_id not in affected
                and (not changing_source or ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL
                     or not any(binding.sample_id == preview.options.sample_id for binding in ref.configuration_bindings)))
            retained_digests = {ref.artifact_ref_sha256 for ref in retained}
            registry = build_batch_stage_registry(plan=snapshot.plan,
                resolved_samples=selected_manifest.enabled_samples, dependencies=self._dependencies)
            if changing_source and any(ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL
                    and any(binding.sample_id == preview.options.sample_id for binding in ref.configuration_bindings)
                    for ref in snapshot.accepted_artifacts):
                imported = self._initial_plan(selected_manifest, registry.stage_registry).external_inputs
                retained = (*retained, *(ref for ref in imported if any(
                    binding.sample_id == preview.options.sample_id for binding in ref.configuration_bindings)))
            successor = expand_batch_plan(snapshot.plan, selected_manifest, registry.stage_registry,
                retained, accepted_artifact_authorities=tuple(
                    item for item in snapshot.committed_artifact_authorities
                    if item.artifact_ref.artifact_ref_sha256 in retained_digests),
                batch_state_store=store,
                training_plan_set_store=self._dependencies.training.training_plan_set_store,
                saved_result_retirements=retirements, **revision)
            # We check software for work that will run again or is still waiting
            # for inputs. Unchanged completed stages reuse their saved results.
            preserved = frozenset(preview.preserved_completed_node_ids)
            required = frozenset(
                {item.kind for item in successor.nodes if item.node_id not in preserved}
                | {item.node_kind for item in successor.deferred_nodes})
            successor = bind_batch_prerequisites(successor,
                self._collect_prerequisites(selected_manifest, successor,
                    required_node_kinds=required) if required else ())
            self._admit_prerequisites(successor.prerequisites)
            if not self._dependencies.runtime.wait_for_project_stage_quiescence():
                raise BatchServiceError("The native project task has not exited. Wait for its recorded exit before revising options.")
            with store.current_project_control(operation="revise-options", lease=lease) as control:
                control.project_processes.assert_all_drained(batch_id=snapshot.head.batch_id,
                    project_identity_sha256=snapshot.head.project_identity_sha256)
                if snapshot.head.execution_state is BatchExecutionState.PAUSED:
                    scheduler = self._new_scheduler(snapshot, lease, registry)
                    consume_quiescent_pause(state_store=store, batch_id=snapshot.head.batch_id,
                        pause_record_sha256=snapshot.head.quiescent_pause_sha256,
                        scheduler=scheduler, workspace_lease=lease, project_lease=control.project_lease,
                        project_processes=control.project_processes)
                current = store.load_head()
                store.revise_plan(successor, f"reviewed {node.kind} options", current.sha256, lease,
                    manifest=selected_manifest if changing_source else None,
                    source_selection=settings if changing_source else None)
            with self._lock:
                self._pause_blocks_admission = False
            return self._status_from_snapshot(store.reopen_execution_snapshot())

    def retry_choices(
        self, batch_id: str, *, expected_head_sha256: str
    ) -> tuple[BatchRetryChoice, ...]:
        """List supported failures and unfinished calculations without starting work."""
        from .project_stage_plan import _project_stage_failure_can_retry
        with self._provider._use():
            self._require_batch(batch_id)
            with self._lock:
                if self._active_wave is not None:
                    return ()
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("list-retries") as lease:
                with self._lock:
                    if self._active_wave is not None:
                        return ()
                snapshot = store.reopen_execution_snapshot(
                    expected_head_sha256=expected_head_sha256
                )
                head = snapshot.head
                if head.execution_state not in {BatchExecutionState.ACTIVE, BatchExecutionState.PAUSED}:
                    return ()
                with self._lock:
                    if self._pause_blocks_admission and head.execution_state is BatchExecutionState.ACTIVE:
                        return ()
                candidates = tuple(
                    node
                    for node in snapshot.plan.nodes
                    if (is_failed_retry_node_kind(node.kind) or node.kind in {"inspection", "reference_unmixing"})
                    and head.node_status_by_id.get(node.node_id) is StageStatus.FAILED
                    and node.node_id not in head.invalidated_node_ids
                    and node.node_id not in head.pending_authorization_sha256_by_node
                )
                pending = tuple(node for node in snapshot.plan.nodes
                    if node.kind in PENDING_PYTHON_RETRY_KINDS
                    and node.node_id in head.pending_authorization_sha256_by_node
                    and node.node_id not in head.invalidated_node_ids)
                if pending:
                    provider = store._project_mutation_provider
                    if provider is None or provider._unresolved_active_records_for_project(
                            project_uri=Path(head.project_path).as_uri(),
                            project_identity_sha256=head.project_identity_sha256):
                        pending = ()
                if not candidates and not pending:
                    return ()
                registry = build_batch_stage_registry(
                    plan=snapshot.plan,
                    resolved_samples=snapshot.manifest.enabled_samples,
                    dependencies=self._dependencies,
                )
                choices = []
                for node in pending:
                    authorization = self._pending_python_retry_authorization(snapshot, node, lease)
                    choices.append(BatchRetryChoice(batch_id, head.sha256, node.node_id, None,
                        node.participant_ids[0], node.kind, "pending_calculation",
                        authorization.authorization_sha256))
                for node in candidates:
                    attempt_ids = head.attempt_ids_by_node[node.node_id]
                    terminal_sha256 = head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]]
                    terminal = store.load_attempt(terminal_sha256)
                    error = terminal.stage_result
                    details = error.error_context
                    if node.kind == "inspection":
                        from .batch_inspection_recovery import reopen_inspection_failure

                        try:
                            sample_id = reopen_inspection_failure(
                                store, snapshot, node.node_id, terminal_sha256)
                        except ValueError:
                            continue
                        choices.append(BatchRetryChoice(
                            batch_id, head.sha256, node.node_id, terminal_sha256,
                            sample_id, node.kind, ("runtime_dependency_unavailable"
                                if error.error_code == "preprocessing_dependencyversionerror" else "source_access_failed")))
                        continue
                    if node.kind in TRAINING_PARENT_NODE_KINDS:
                        if (error.error_code != "native_classifier_fit_failed"
                            or (details or {}).get("disposition_code") != "drained_failure"
                            or "native_abort_archive_ref" not in (details or {})):
                            continue
                        _archive, _terminal, _authorization = reopen_native_training_failure(
                            dependencies=self._dependencies.training, workspace_lease=lease,
                            node_id=node.node_id, terminal_record_sha256=terminal_sha256)
                        choices.append(BatchRetryChoice(
                            batch_id, head.sha256, node.node_id, terminal_sha256,
                            _authorization.dispatch_binding.participant_ids[0], node.kind,
                            "native_fit_failed",
                        ))
                        continue
                    if node.kind == "reference_unmixing":
                        from .batch_reference_recovery import (
                            INPUT_ERRORS,
                            reopen_reference_input_failure,
                        )

                        if error.error_code not in INPUT_ERRORS:
                            continue
                        try:
                            sample_id, recovery = reopen_reference_input_failure(
                                store, snapshot, node.node_id, terminal_sha256
                            )
                        except ValueError:
                            continue
                        choices.append(
                            BatchRetryChoice(
                                batch_id,
                                head.sha256,
                                node.node_id,
                                terminal_sha256,
                                sample_id,
                                node.kind,
                                "calculation_failed" if recovery == "calculation" else "input_required",
                            )
                        )
                        continue
                    # Omit known unsupported failures. An eligible-looking but
                    # corrupt record must still fail the full owner reopening.
                    if is_project_request_failure(node.kind, error):
                        spec = registry.stage_registry.resolve(node.kind, node.version)
                        _terminal, previous = reopen_project_request_failure(
                            dependencies=self._dependencies.training, workspace_lease=lease,
                            head=head, node_id=node.node_id,
                            terminal=terminal, adapter_spec=spec,
                        )
                        choices.append(BatchRetryChoice(
                            batch_id, head.sha256, node.node_id, terminal_sha256,
                            previous.dispatch_binding.participant_ids[0], node.kind,
                            "request_preparation_failed",
                        ))
                        continue
                    if (
                        not _project_stage_failure_can_retry(node.kind, error)
                    ):
                        continue
                    spec = registry.stage_registry.resolve(node.kind, node.version)
                    reopened = reopen_project_stage_failure(
                        dependencies=self._dependencies.training,
                        workspace_lease=lease,
                        node_id=node.node_id,
                        terminal_record_sha256=terminal_sha256,
                        adapter_spec=spec,
                    )
                    choices.append(
                        BatchRetryChoice(
                            batch_id=batch_id,
                            state_head_sha256=head.sha256,
                            node_id=node.node_id,
                            terminal_record_sha256=terminal_sha256,
                            sample_id=reopened.authorized.sample_id,
                            stage_kind=node.kind,
                            retry_reason=(
                                details["transport_validation_code"]
                                if details["process_termination_code"] == "normal_exit"
                                else details["process_termination_code"]
                            ),
                        )
                    )
                return tuple(
                    sorted(
                        choices, key=lambda item: (item.sample_id, item.stage_kind, item.node_id)
                    )
                )

    def status(self, batch_id: str) -> BatchStatusSnapshot:
        with self._provider._use():
            self._require_batch(batch_id)
            with self._lock:
                snapshot = self._dependencies.runtime.state_store.read_current_progress()
                return self._status_from_snapshot(snapshot)

    def saved_stage_results(
        self, batch_id: str, *, expected_head_sha256: str,
    ) -> BatchSavedStageResults:
        """Read current completed stages even while later decisions remain pending."""
        from .saved_stage_results import project_saved_stages

        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            result = project_saved_stages(snapshot, self._dependencies, self._workspace_layout.summaries)
            if store.load_head().sha256 != snapshot.head.sha256:
                raise BatchHeadConflictError("saved stage results changed while reading")
            return result

    def select_saved_figure_region(self, batch_id: str, *, expected_head_sha256: str,
            artifact_ref_sha256: str, figure_id: str,
            figure_options: Mapping[str, object] | None, runner) -> dict[str, int] | None:
        """Return draft export coordinates for the exact selected prepared-image result."""
        from .saved_reconstruction_figures import reconstruction_record, field_selection, FIELD_FIGURE_IDS
        from .export_region_selection import choose_region
        with self._provider._use():
            self._require_batch(batch_id)
            if figure_id not in FIELD_FIGURE_IDS:
                raise ValueError('This figure does not support choosing an image rectangle.')
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            authority = next((item for item in snapshot.committed_artifact_authorities
                if item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
            if authority is None:
                raise BatchReviewPlotError('The selected saved result is no longer current.')
            record = reconstruction_record(authority, snapshot)
            initial = field_selection(record, dict(figure_options or {}), figure_id=figure_id)
            region = choose_region(runner, record, initial, self._workspace_layout.summaries,
                appearance_path=self._workspace_layout.output_root / 'image-appearance.json')
            if store.load_head().sha256 != expected_head_sha256:
                raise BatchHeadConflictError('The saved result changed while choosing its export region.')
            if region is not None:
                field_selection(record, {**dict(figure_options or {}), **region}, figure_id=figure_id)
            return region

    def measurement_field(self, batch_id: str, *, expected_head_sha256: str,
            artifact_ref_sha256: str, figure_options: Mapping[str, object]) -> dict:
        """Read a bounded tissue view without creating a file for each pan."""
        from .measurement_spatial_review import MeasurementFieldReader, MeasurementImageFieldReader
        from .saved_cell_gallery import gallery_resources
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            authority = next((item for item in snapshot.committed_artifact_authorities
                if item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
            if authority is None:
                raise BatchReviewPlotError('The selected saved result is no longer current.')
            members = None
            if figure_options.get('image_artifact_refs'):
                from urllib.parse import quote
                from .saved_cell_figures import measurement_cohort_members
                members = measurement_cohort_members(authority,
                    'distribution/' + quote(figure_options['measurement'], safe=''), snapshot,
                    figure_options['image_artifact_refs'])
                if len(members) != len(figure_options['image_artifact_refs']):
                    raise BatchReviewPlotError('The selected image sections use different measurement definitions.')
            with self._provider._plot_lock, gallery_resources(snapshot, authority):
                key = (snapshot.head.sha256, artifact_ref_sha256,
                       tuple(source.artifact_ref.artifact_ref_sha256 for source,_measurement,_feature in members)) if members else (
                       snapshot.head.sha256, artifact_ref_sha256)
                previous = self._provider._measurement_field
                if previous is None or previous[0] != key:
                    self._provider._measurement_field = None
                    if previous is not None:
                        previous[1].close()
                    reader = (MeasurementImageFieldReader(members, snapshot, state_store=store) if members
                              else MeasurementFieldReader(authority, snapshot, state_store=store))
                    self._provider._measurement_field = (key, reader)
                else:
                    reader = previous[1]
                result = reader.read(authority, snapshot, dict(figure_options))
                if store.load_head().sha256 != snapshot.head.sha256:
                    raise BatchHeadConflictError('The saved measurements changed while loading the image field.')
                return result

    def segmentation_field(self, batch_id: str, *, expected_head_sha256: str,
            artifact_ref_sha256: str, figure_options: Mapping[str, object]) -> dict:
        """Read accepted staining/outlines before measurements, using the shared reader slot."""
        from .segmentation_spatial_review import SegmentationFieldReader
        from .saved_cell_gallery import gallery_resources
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            authority = next((item for item in snapshot.committed_artifact_authorities
                if item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256
                and item.artifact_ref.artifact_kind == 'segmentation_result'), None)
            if authority is None:
                raise BatchReviewPlotError('The selected segmentation is no longer current.')
            with self._provider._plot_lock, gallery_resources(snapshot, authority,
                    memory_bytes=(768 if figure_options.get('nuclear_coverage') is not None else 640)*1024**2):
                key = ('segmentation', snapshot.head.sha256, artifact_ref_sha256)
                previous = self._provider._measurement_field
                if previous is None or previous[0] != key:
                    self._provider._measurement_field = None
                    if previous is not None:
                        previous[1].close()
                    from .saved_segmentation_cells import segmentation_area_geometry
                    reader = SegmentationFieldReader(authority, snapshot, state_store=store,
                        coverage_scope_loader=lambda: segmentation_area_geometry(authority, snapshot,
                            self._dependencies, self._workspace_layout.summaries))
                    self._provider._measurement_field = (key, reader)
                else:
                    reader = previous[1]
                result = reader.read(authority, snapshot, dict(figure_options))
                if store.load_head().sha256 != snapshot.head.sha256:
                    raise BatchHeadConflictError('The segmentation changed while loading the image field.')
                return result

    def marker_field(self, batch_id: str, *, interaction_request_id: str | None = None,
            artifact_ref_sha256: str | None = None,
            expected_state_head_sha256: str, figure_options: Mapping[str, object]) -> dict:
        """Read an exact marker interval or current pending/saved assignment field."""
        from .batch_phenotyping.marker_spatial_review import (
            MarkerFieldReader, NimbusFieldReader, marker_field_inputs, marker_score_provenance,
        )
        from .saved_cell_gallery import gallery_resources
        if 'phenotype_definition' in figure_options:
            self.preview_marker_combinations(batch_id, interaction_request_id=interaction_request_id,
                expected_state_head_sha256=expected_state_head_sha256,
                phenotype_definition=figure_options['phenotype_definition'])
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_state_head_sha256)
                if figure_options.get('prediction_cohort'):
                    from .batch_phenotyping.supervised_cohort_review import prediction_review_context
                    context, review_identity = prediction_review_context(snapshot, self._dependencies,
                        interaction_request_id, artifact_ref_sha256, cached_index=self._provider._prediction_review_index)
                else:
                    if artifact_ref_sha256 is not None:
                        raise BatchReviewPlotError('This saved result is not a supervised assignment review.')
                    if interaction_request_id not in snapshot.head.pending_interaction_ids:
                        raise BatchReviewPlotError('The marker review is no longer pending.')
                    publication = store.reopen_interaction_request_publication(interaction_request_id,
                        expected_head_sha256=snapshot.head.sha256)
                    target = project_batch_interaction_target(publication)
                if figure_options.get('prediction_cohort'):
                    from .batch_phenotyping.supervised_cohort_review import prediction_owners
                    from .batch_phenotyping.supervised_prediction_review import PredictionReviewIndex
                    from .batch_phenotyping.supervised_prediction_spatial import SupervisedAssignmentFieldReader
                    settings = PredictionReviewIndex.settings(figure_options)
                    if (context['score_kind'] == 'native_svm_pairwise_vote_fraction'
                            and context['class_count'] == 2 and settings['cutoff'] != 0):
                        raise BatchReviewPlotError('Binary SVM vote agreement cannot reject uncertain cells.')
                    cached = self._provider._prediction_review_index
                    owners = (cached[1]['owners'] if cached is not None and cached[0][:3] ==
                        (snapshot.head.sha256,review_identity,'cohort') else
                        prediction_owners(snapshot,context,self._dependencies))
                    image = tuple(settings.get('image_identity',()))
                    selected = [owner for owner in owners if tuple(owner[key] for key in
                                ('sample_id','project_entry_id','slide_id')) == image]
                    if not selected:
                        raise BatchReviewPlotError('Choose an image in the current prediction review.')
                    key = ('supervised-assignments',snapshot.head.sha256,review_identity,
                           tuple(owner['sha256'] for owner in selected),
                           *(settings.get(name) for name in ('cutoff','lower','upper','class_id','unclassified_only','plot_scope','processing_group_id')))
                    authority = selected[0]['measurement_authority']
                    with gallery_resources(snapshot,authority):
                        previous = self._provider._measurement_field
                        if previous is None or previous[0] != key:
                            self._provider._measurement_field = None
                            if previous is not None:
                                previous[1].close()
                            reader = SupervisedAssignmentFieldReader(selected,self._dependencies,snapshot,settings)
                            self._provider._measurement_field = (key,reader)
                        else:
                            reader = previous[1]
                        result = reader.read(authority,snapshot,settings)
                    if store.load_head().sha256 != snapshot.head.sha256:
                        raise BatchHeadConflictError('The prediction review changed while loading its image field.')
                    return result
                if 'phenotype_definition' in figure_options:
                    from .batch_phenotyping.marker_assignment_spatial import assignment_field
                    result = assignment_field(self._provider, snapshot, target, dict(figure_options))
                    if store.load_head().sha256 != snapshot.head.sha256:
                        raise BatchHeadConflictError('The phenotype review changed while loading the image field.')
                    return result
                if target.stage_result.interaction_reason != 'marker_threshold_review_required':
                    raise BatchReviewPlotError('Choose the current marker threshold review.')
                context, options = target.stage_result.resume_context, dict(figure_options)
                marker, authorities, measurements, population = marker_field_inputs(snapshot, context, options)
                provenance = marker_score_provenance(marker, options)
                key = ('marker', snapshot.head.sha256, target.interaction_context_sha256,
                       tuple(a.artifact_ref.artifact_ref_sha256 for a in authorities),
                       marker['marker_id'], options.get('source'), provenance['sha256'] if provenance else None)
                previous = self._provider._measurement_field
                # Score validation indexes the complete authenticated file once;
                # later reads admit only the retained image join and field workspace.
                score_bytes = (getattr(previous[1], 'score_bytes', 0) if previous is not None and previous[0] == key
                    else 5 * Path(provenance['path']).stat().st_size if provenance else 0)
                with gallery_resources(snapshot, authorities[0], memory_bytes=640 * 1024**2 + score_bytes):
                    if previous is None or previous[0] != key:
                        self._provider._measurement_field = None
                        if previous is not None:
                            previous[1].close()
                        reader = (NimbusFieldReader(authorities, measurements, population, snapshot,
                            marker['marker_id'], provenance) if provenance else
                            MarkerFieldReader(authorities, measurements, population, snapshot))
                        self._provider._measurement_field = (key, reader)
                    else:
                        reader = previous[1]
                    options['measurement'] = 'Nimbus score' if provenance else marker['measurement_semantic']['measurement']
                    if not options.get('channels'):
                        channel = next(i for i,c in enumerate(measurements[0].canonical_channels)
                                       if c['channel_id'] == marker['measurement_semantic']['channel_id'])
                        from ome_types import from_xml
                        from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
                        inventory = resolve_component_identity_inventory(
                            from_xml(reader.image.attributes['mif_pipeline']['ome_xml']), 0)
                        nuclear = inventory.nuclear_component_index if inventory is not None else None
                        options['channels'] = list(dict.fromkeys([channel] + ([nuclear] if nuclear is not None else [])))
                    result = reader.read(authorities[0], snapshot, options)
                    result['metadata']['available_channels'] = [dict(index=i,label=c['label'])
                                                               for i,c in enumerate(reader.image.channels)]
                    if store.load_head().sha256 != snapshot.head.sha256:
                        raise BatchHeadConflictError('The marker review changed while loading the image field.')
                    return result

    def saved_stage_figures(
        self, batch_id: str, *, expected_head_sha256: str,
        artifact_ref_sha256: str, figure_id: str, figure_options: Mapping[str, object] | None = None,
    ) -> tuple[BatchReviewPlot, ...]:
        """Draw retained scientific values without reopening a pending interaction."""
        from .saved_stage_results import render_saved_stage

        if figure_id == "umap-current":
            return self._saved_clustering_umap(batch_id, expected_head_sha256,
                artifact_ref_sha256, figure_options)

        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            authority = next((item for item in snapshot.committed_artifact_authorities
                              if item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
            if authority is None:
                raise BatchReviewPlotError("The selected saved result is no longer current.")
            from contextlib import nullcontext
            from .saved_cell_gallery import gallery_resources
            reconstruction_record = None
            reconstruction_selection = None
            from .saved_reconstruction_figures import FIELD_FIGURE_IDS
            if figure_id in FIELD_FIGURE_IDS:
                from .saved_reconstruction_figures import reconstruction_record as resolve_record, field_selection, reconstruction_resources
                reconstruction_record = resolve_record(authority, snapshot)
                reconstruction_selection = field_selection(reconstruction_record, dict(figure_options or {}), figure_id=figure_id)
            resources = (gallery_resources(snapshot, authority) if figure_id == "cell-gallery" else
                         reconstruction_resources(snapshot, authority, reconstruction_record, reconstruction_selection)
                         if reconstruction_record is not None else nullcontext())
            with self._provider._plot_lock, resources:
                resolved_options = None
                gallery = None
                reconstruction = None
                supervised_cell = (figure_options or {}).get('supervised_gallery_settings')
                if supervised_cell is not None:
                    if figure_id != 'classifier-recorded-tables' or authority.artifact_ref.artifact_kind != 'classifier_record_set':
                        raise BatchReviewPlotError('Choose a reference cell from the accepted classifier result.')
                    resolved_options = {'supervised_gallery_settings': dict(supervised_cell)}
                if reconstruction_record is not None:
                    from .saved_reconstruction_figures import load_reconstruction, display_options as reconstruction_options
                    selection_key = (snapshot.head.sha256, artifact_ref_sha256, json.dumps(reconstruction_selection, sort_keys=True))
                    previous = self._provider._reconstruction_field
                    if previous is None or previous[0] != selection_key:
                        self._provider._reconstruction_field = None
                        reconstruction = load_reconstruction(reconstruction_record, reconstruction_selection)
                        self._provider._reconstruction_field = (selection_key, reconstruction)
                    else:
                        reconstruction = previous[1]
                    resolved_options = reconstruction_options(reconstruction, dict(figure_options or {}), figure_id=figure_id)
                if figure_id == "cell-gallery":
                    from .saved_cell_gallery import load_gallery, display_options
                    options = dict(figure_options or {})
                    selection = {key: options.get(key) for key in ("cell_ids", "count", "seed", "phenotype", "phenotypes", "measurement_ranges", "crop_um", "channels", "field_aspect", "pyramid_level")}
                    selection_key = (snapshot.head.sha256, artifact_ref_sha256, json.dumps(selection, sort_keys=True))
                    previous = self._provider._cell_gallery
                    if previous is None or previous[0] != selection_key:
                        self._provider._cell_gallery = None
                        gallery = load_gallery(authority, snapshot, options, state_store=store)
                        self._provider._cell_gallery = (selection_key, gallery)
                    else:
                        gallery = previous[1]
                    resolved_options = display_options(gallery, options)
                if figure_id in {"region-outlines", "secondary-outlines"}:
                    from mif_pipeline.cell_analysis.figure_style import outline_palette
                    palette = (figure_options or {}).get("palette", "teal")
                    outline_palette(palette)
                    resolved_options = {"palette": palette}
                if figure_id.startswith("distribution/"):
                    resolved_options = dict(figure_options or {})
                if figure_id == 'coexpression/selected':
                    from .saved_coexpression_figures import _accepted_gates, _marker_ids, selected_markers
                    markers = _marker_ids(_accepted_gates(authority, snapshot))
                    resolved_options = {'markers': list(selected_markers(markers, figure_id, figure_options))}
                if 'export_palette' in (figure_options or {}):
                    from mif_pipeline.cell_analysis.figure_style import result_export_palette, supports_result_export_palette
                    if not supports_result_export_palette(figure_id):
                        raise ValueError('This figure retains its saved display colors.')
                    result_export_palette(figure_options['export_palette'])
                    resolved_options = {**(resolved_options or {}), 'export_palette': figure_options['export_palette']}
                cache_key = (snapshot.head.sha256, artifact_ref_sha256, "saved:" + figure_id
                             + (json.dumps(resolved_options, sort_keys=True) if resolved_options is not None else ""))
                cached = self._provider._plot_cache.get(cache_key)
                if cached is not None:
                    if store.load_head().sha256 != snapshot.head.sha256:
                        raise BatchHeadConflictError("saved figure result changed while reading")
                    return cached
                if supervised_cell is not None:
                    from .saved_classifier_tables import saved_classifier_cell
                    outputs, resolved_options = saved_classifier_cell(
                        authority, snapshot, self._dependencies, dict(supervised_cell))
                elif reconstruction is not None:
                    from .saved_reconstruction_figures import render_reconstruction
                    outputs = render_reconstruction(reconstruction, resolved_options, figure_id=figure_id)
                elif gallery is not None:
                    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery
                    outputs = render_cell_gallery(gallery, resolved_options, publication=True)
                elif figure_id.startswith("distribution/"):
                    from .saved_cell_figures import measurement_review
                    # Interval edits reuse this measurement's trusted saved histogram;
                    # only the exact interval count and example IDs need another pass.
                    summary = next((items[0].display_options
                                    for key, items in self._provider._plot_cache.items()
                                    if key[:2] == cache_key[:2]
                                    and key[2].startswith("saved:" + figure_id + "{") and items
                                    and (items[0].display_options or {}).get('cohort_artifact_refs') == resolved_options.get('cohort_artifact_refs')
                                    and (items[0].display_options or {}).get('region') == resolved_options.get('region')
                                    and (items[0].display_options or {}).get('phenotype') == resolved_options.get('phenotype')
                                    and (items[0].display_options or {}).get('plot_scope') == resolved_options.get('plot_scope')), None)
                    outputs, resolved_options = measurement_review(
                        authority, figure_id, snapshot, resolved_options, summary=summary, state_store=store,
                        dependencies=self._dependencies)
                else:
                    outputs = render_saved_stage(authority, figure_id, snapshot,
                                                 self._dependencies, self._workspace_layout.summaries,
                                                 figure_options=resolved_options)
                if store.load_head().sha256 != snapshot.head.sha256:
                    raise BatchHeadConflictError("saved figure result changed while rendering")
                added_bytes = sum(len(output[3]) for output in outputs)
                if self._provider._plot_bytes + added_bytes > _REVIEW_PLOT_CACHE_LIMIT_BYTES:
                    raise BatchReviewPlotError("The temporary figure folder is full. Reopen the analysis to clear it.")
                plots = self._provider._write_review_plots(outputs)
                if resolved_options is not None:
                    from dataclasses import replace
                    plots = tuple(replace(plot, display_options=resolved_options) for plot in plots)
                self._provider._plot_bytes += added_bytes
                self._provider._plot_cache[cache_key] = plots
                return plots

    def results(self, batch_id: str, *, expected_head_sha256: str) -> BatchResultsSnapshot:
        """Project completed publication evidence, without authorizing a successor."""
        from .batch_phenotyping.export_execution import (
            final_non_spatial_export_result_identity_contract,
            project_committed_final_export_for_results,
        )
        from .batch_phenotyping.handoff_execution import spatial_cell_handoff_identity_contract

        with self._provider._use():
            self._require_batch(batch_id)
            if (
                type(expected_head_sha256) is not str
                or len(expected_head_sha256) != 64
                or any(char not in "0123456789abcdef" for char in expected_head_sha256)
            ):
                raise BatchServiceError("results require an exact expected head SHA-256")
            with self._lock:
                store = self._dependencies.runtime.state_store
                snapshot = store.reopen_execution_snapshot(
                    expected_head_sha256=expected_head_sha256
                )
                head = snapshot.head
                if head.sha256 != expected_head_sha256:
                    raise BatchHeadConflictError("results execution head changed")
                if self._closed_outcome(snapshot) != "completed":
                    raise BatchServiceError("results require a completed batch")
                samples = {sample.sample_id: sample for sample in snapshot.manifest.enabled_samples}
                final_kind = final_non_spatial_export_result_identity_contract().artifact_kind
                handoff_kind = spatial_cell_handoff_identity_contract().artifact_kind
                owners = {final_kind: {}, handoff_kind: {}, "processed_image": {}}
                for authority in snapshot.committed_artifact_authorities:
                    ref = authority.artifact_ref
                    if ref.artifact_kind not in owners:
                        continue
                    group = owners[ref.artifact_kind]
                    sample_id = ref.scope.sample_id
                    if sample_id not in samples or sample_id in group:
                        raise BatchServiceError(
                            "results contain foreign or duplicate current owners"
                        )
                    if (
                        authority.state_head_sha256 != head.sha256
                        or authority.active_plan_sha256 != snapshot.plan.plan_sha256
                    ):
                        raise BatchServiceError(
                            "results owner differs from the current head or plan"
                        )
                    sample = samples[sample_id]
                    if ref.artifact_kind == "processed_image":
                        if ref.locator is None:
                            raise BatchServiceError("Results require the saved OME-Zarr image location")
                        group[sample_id] = authority
                        continue
                    if (
                        ref.scope.project_entry_id
                        != head.sample_project_entry_id_by_sample.get(sample_id)
                        or sample.resolved_config_sha256
                        != head.resolved_config_sha256_by_sample.get(sample_id)
                        or ref.owning_identity.get("domain_set_sha256")
                        != head.domain_set_sha256_by_sample.get(sample_id)
                    ):
                        raise BatchServiceError(
                            "results owner differs from current sample bindings"
                        )
                    group[sample_id] = authority
                if any(set(group) != set(samples) for group in owners.values()):
                    raise BatchServiceError(
                        "results require one current image, final export and spatial handoff per sample"
                    )
                finals = []
                handoffs = []
                provenance_hashes = {}
                membership = {}
                for sample_id, sample in sorted(samples.items()):
                    final, handoff, provenance_hash = project_committed_final_export_for_results(
                        owners[final_kind][sample_id],
                        spatial_handoff_authority=owners[handoff_kind][sample_id],
                        resolved_sample=sample,
                        state_store=store,
                    )
                    finals.append(final)
                    handoffs.append(handoff)
                    provenance_hashes[sample_id] = provenance_hash
                    membership.setdefault(sample.validation_domain_id, []).append(sample_id)
                status = self._status_from_snapshot(snapshot)
                status = replace(
                    status,
                    summary_rows=tuple(
                        replace(
                            row,
                            processed_image_path=owners["processed_image"][row.sample_id].artifact_ref.locator,
                            provenance_hash=provenance_hashes[row.sample_id],
                        )
                        for row in status.summary_rows
                    ),
                )
                result = BatchResultsSnapshot(status, membership, tuple(finals), tuple(handoffs))
                if store.load_head().sha256 != head.sha256:
                    raise BatchHeadConflictError("results execution head changed before return")
                return result

    def execution_status_if_present(self, batch_id: str) -> BatchStatusSnapshot | None:
        """Return an authenticated status, or None for a pristine unstarted workspace.

        A missing head alone is never evidence that execution has not started.
        Initialization and other retained batch publications make that state
        an integrity failure instead of an empty workspace.
        """
        with self._provider._use():
            self._require_batch(batch_id)
            with self._lock:
                store = self._dependencies.runtime.state_store
                snapshot = store.execution_snapshot_if_present()
                if snapshot is None and self._active_wave is not None:
                    raise BatchServiceError("active batch wave lacks execution state")
                return None if snapshot is None else self._status_from_snapshot(snapshot)

    def align_paired_image(self, batch_id, request, options):
        """Estimate a candidate, then continue through the existing transform/QC review."""
        from .batch_automatic_alignment import prepare_automatic_alignment

        with self._provider._use():
            self._require_batch(batch_id)
            if type(request) is not BatchInteractionResponseRequest:
                raise TypeError("Alignment requires the displayed interaction request")
            with self._lock:
                if self._active_wave is not None:
                    raise ValueError("Wait for the current processing step before alignment")
                store = self._dependencies.runtime.state_store
                with store.acquire_writer_lease("automatic-alignment"):
                    prepare_automatic_alignment(
                        store, self._dependencies.runtime.runner, request, options
                    )
        return self.apply_interaction_response(batch_id, request)

    def saved_project_path(self, batch_id: str, *, expected_head_sha256: str) -> Path:
        """Resolve the current project's location when saved images are opened."""
        with self._provider._use():
            self._require_batch(batch_id)
            head = self._dependencies.runtime.state_store.load_head()
            if head.sha256 != expected_head_sha256:
                raise BatchServiceError("The analysis changed. Refresh before opening its images.")
            project = Path(head.project_path)
            if not project.is_file():
                raise BatchServiceError("The saved QuPath project could not be found. Restore access to the analysis folder, then try again.")
            return project

    def require_current_saved_results(self, batch_id: str, *, expected_head_sha256: str, project_path: Path) -> None:
        """Keep outdated automatic displays closed until their ordinary cleanup task completes."""
        with self._provider._use():
            self._require_batch(batch_id)
            snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot(
                expected_head_sha256=expected_head_sha256)
            if Path(snapshot.head.project_path).resolve() != project_path.resolve():
                return
            pending = tuple(node for node in snapshot.plan.nodes if node.kind == "saved_result_retirement"
                and (snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.COMPLETED
                     or node.node_id in snapshot.head.invalidated_node_ids
                     or node.node_id in snapshot.head.pending_authorization_sha256_by_node))
            if pending:
                samples = ", ".join(sorted({sample for node in pending for sample in node.participant_ids}))
                raise BatchServiceError(f"The revised settings have not yet been applied to the saved QuPath results for {samples}. "
                    "Use Continue saved work to update them. If the update stopped, open Details, "
                    "correct the reported file-access or saving problem, then refresh and use Retry step… when offered.")

    def paired_image_views(
        self, batch_id: str, *, expected_head_sha256: str, project_path: Path
    ) -> tuple[dict, ...]:
        """Project accepted alignment display data; no native model or image read."""
        from .batch_paired_display import registered_pair_displays

        with self._provider._use():
            self._require_batch(batch_id)
            with self._lock:
                store = self._dependencies.runtime.state_store
                snapshot = store.reopen_execution_snapshot(
                    expected_head_sha256=expected_head_sha256
                )
                if Path(snapshot.head.project_path).absolute() != project_path.absolute():
                    return ()
                result = registered_pair_displays(store, snapshot)
                if store.load_head().sha256 != expected_head_sha256:
                    raise BatchHeadConflictError(
                        "The analysis changed before paired-image display opened"
                    )
                return result

    def list_interactions(
        self,
        batch_id: str,
        query: BatchInteractionQuery,
    ) -> BatchInteractionPage:
        with self._provider._use():
            self._require_batch(batch_id)
            if not isinstance(query, BatchInteractionQuery):
                raise BatchInteractionError("interaction query must be typed")
            store = self._dependencies.runtime.state_store
            head = store.load_head()
            with self._lock:
                self._tissue_review_summaries = {
                    key: value for key, value in self._tissue_review_summaries.items()
                    if key in head.pending_interaction_ids
                }
            ordinary: list[BatchInteractionTarget] = []
            aggregate_groups: list[BatchAggregateInteractionGroup] = []
            for request_id in head.pending_interaction_ids:
                try:
                    publication = store.reopen_interaction_request_publication(
                        request_id, expected_head_sha256=head.sha256
                    )
                    ordinary.append(self._present_supervised_setup(store, head,
                        self._present_reference_source(self._present_tissue_review(
                            store, project_batch_interaction_target(publication)))))
                except (BatchInteractionError, BatchStateError) as ordinary_error:
                    try:
                        aggregate_groups.append(
                            self._reopen_aggregate_interaction_group(store, head, request_id)
                        )
                    except (BatchInteractionError, BatchStateError):
                        raise ordinary_error
            if any(is_reference_handoff(target) for target in ordinary):
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                catalogue = store.reopen_current_project_catalogue(expected_head_sha256=head.sha256)
                ordinary = [
                    (
                        replace(
                            target,
                            reference_view=issue_reference_only_view(snapshot, catalogue, target),
                        )
                        if is_reference_handoff(target)
                        else target
                    )
                    for target in ordinary
                ]
            page = page_interactions(
                (*group_interactions(tuple(ordinary)), *aggregate_groups), query
            )
            # The presentation locator comes from the authenticated current head.
            # It grants no classifier or annotation acceptance authority.
            if (
                head.project_catalogue_sha256 is not None
                and head.project_qpproj_snapshot_sha256 is not None
            ):
                project = Path(head.project_path)
                if project.is_absolute() and project.is_file():
                    page = replace(page, qupath_project_path=str(project))
            return page

    def _present_supervised_setup(self, store, head, target):
        """Show the setup explicitly selected by this ordinary review return.

        The predecessor terminal and typed plan are existing retained records.
        Their request supplies editable presentation defaults; accepting the
        current form still validates its current inputs and selection normally.
        """
        if (target.stage_result.stage != "supervised_phenotype_training_plan_review"
                or target.stage_result.interaction_reason is not InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED):
            return target
        plan = store._load_typed_plan(head.active_plan_sha256)
        node = next(item for item in plan.nodes if item.node_id == target.node_id)
        returned = node.settled_review_revision
        if returned is None:
            return target
        previous = store.load_attempt(returned.predecessor_terminal_record_sha256)
        if previous.node_id != returned.predecessor_node_id:
            raise BatchInteractionError("The previous classifier setup belongs to another task.")
        refs = tuple(ref for ref in previous.output_artifacts if ref.artifact_kind == "training_plan_set")
        if len(refs) != 1:
            return target
        identity = refs[0].owning_identity
        saved = self._dependencies.training.training_plan_set_store.reopen(
            identity["store_relative_locator"], sha256=identity["body_sha256"],
            byte_count=identity["body_byte_count"])
        if len(saved.plans) != 1 or saved.plans[0].request.classifier_id != "supervised-phenotype":
            return target
        return replace(target, saved_supervised_setup=saved.plans[0].request.to_mapping())

    def _present_tissue_review(self, store, target):
        """Attach optional display measurements without changing review authority."""
        reason = target.stage_result.interaction_reason
        tissue = reason is InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED
        structure = reason is InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED
        selection = reason is InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED
        if not tissue and reason not in {
            InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
            InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED,
            InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED,
            InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
            InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED,
        }:
            return target
        from .batch_tissue_review_presentation import tissue_region_review
        from .region_review_geometry import pending_domain_review, pending_structure_review, pending_structure_selection_review
        from .project_stage_plan import ProjectStageRecordError
        with self._lock:
            review = self._tissue_review_summaries.get(target.interaction_request_id)
            if review is None:
                try:
                    review = (pending_structure_selection_review(store, target) if selection
                              else pending_structure_review(store, target) if structure
                              else tissue_region_review(store, target) if tissue
                              else pending_domain_review(store, target))
                except (OSError, ROIAnalysisError, ProjectStageRecordError, ValueError, UnicodeError):
                    logging.getLogger(__name__).warning(
                        "Could not read saved geometry for review %s",
                        target.interaction_request_id, exc_info=True,
                    )
                    return replace(target, tissue_section_summary_error=(
                        "Region measurements are unavailable. Review the outlines in QuPath."))
                self._tissue_review_summaries[target.interaction_request_id] = review
        return replace(target, region_review=review)

    def _present_reference_source(self, target):
        """Attach current saved source values without changing the pending request."""
        if target.stage_result.stage != "reference_unmixing":
            return target
        owner = target.stage_result.resume_context.get("service_context", {})
        state = owner.get("operation_state", {})
        if (state.get("checkpoint") != "final_run_basis"
                or "reselect_basis_source" not in state.get("offered_actions", ())):
            return target
        from .reference_unmixing import ReferenceUnmixingOperation, _thaw_json_value
        try:
            operation = ReferenceUnmixingOperation.open_existing(Path(owner["operation_root_locator"]))
            snapshot = operation._service.resume(state["run_id"])
            if snapshot.checkpoint.content_sha256 != state["checkpoint_token"]:
                raise ValueError("The spectral source review changed.")
            canonical = operation._service._require_canonical_target(state["run_id"], snapshot.start_authority_sha256)
            summary = operation._service._saved_spectral_source(
                snapshot.checkpoint, snapshot.start_authority_sha256, canonical)
            source = _thaw_json_value(summary).get("saved_spectral_source")
        except (OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Could not read saved spectral source for review %s",
                target.interaction_request_id, exc_info=True)
            # This optional read must not obstruct the current final-basis decision.
            return target
        return replace(target, saved_spectral_source=source)

    def inspect_reference_component(self, batch_id, *, interaction_request_id,
                                    expected_state_head_sha256, component):
        """Read one accepted spectrum for the exact pending reference review."""
        from .reference_component_inspection import inspect_reference_component
        return inspect_reference_component(self, batch_id,
            interaction_request_id=interaction_request_id,
            expected_state_head_sha256=expected_state_head_sha256, component=component)

    def inspect_reference_component_preparation(self, batch_id, *, interaction_request_id,
                                                expected_state_head_sha256, component, phase):
        """Open a saved preparation page without submitting a change."""
        from .reference_component_inspection import inspect_reference_component_preparation
        return inspect_reference_component_preparation(self, batch_id,
            interaction_request_id=interaction_request_id,
            expected_state_head_sha256=expected_state_head_sha256, component=component, phase=phase)

    def inspect_reference_control_preparation(self, batch_id, *, interaction_request_id,
                                               expected_state_head_sha256, input_id,
                                               expected_preparation_sha256):
        """Read one accepted AF source for the exact pending reference review."""
        from .reference_component_inspection import inspect_reference_control_preparation
        return inspect_reference_control_preparation(self, batch_id,
            interaction_request_id=interaction_request_id,
            expected_state_head_sha256=expected_state_head_sha256, input_id=input_id,
            expected_preparation_sha256=expected_preparation_sha256)

    def reference_region_action(self, batch_id, *, interaction_request_id,
                                expected_state_head_sha256, action, runner=None,
                                snapshot_sha256=None, reviewer_id=None, component=None, preparation_phase=None):
        """Open or review regions for the exact pending spectral-reference request."""
        from .reference_unmixing import ReferenceUnmixingOperation
        from .reference_region_authoring import reference_region_action
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("reference-region-review"):
                head = store.load_head()
                if (head.sha256 != expected_state_head_sha256
                        or interaction_request_id not in head.pending_interaction_ids):
                    raise ValueError("Reopen the current reference-region review before continuing.")
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id, expected_head_sha256=head.sha256)
                target = project_batch_interaction_target(publication)
                if target.stage_result.stage != "reference_unmixing":
                    raise ValueError("This review does not select spectral-reference regions.")
                owner = target.stage_result.resume_context["service_context"]
                if owner.get("interaction_kind") != "reference_unmixing_operation":
                    raise ValueError("Select reference images before drawing regions.")
                state = owner["operation_state"]
                operation = ReferenceUnmixingOperation.open_existing(Path(owner["operation_root_locator"]))
            return reference_region_action(operation, state["run_id"], state["checkpoint_token"],
                action, runner=runner, snapshot_sha256=snapshot_sha256, reviewer_id=reviewer_id,
                component=component, preparation_phase=preparation_phase)

    def export_reference_library(
        self,
        batch_id: str,
        *,
        interaction_request_id: str,
        expected_state_head_sha256: str,
        destination: Path,
    ) -> Path:
        """Export the accepted library of the exact current review; no source reread."""
        from .reference_unmixing import ReferenceUnmixingOperation, ReferenceUnmixingState

        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("export-reference-library"):
                head = store.load_head()
                if (
                    head.sha256 != expected_state_head_sha256
                    or interaction_request_id not in head.pending_interaction_ids
                ):
                    raise ValueError(
                        "The current review has changed; reopen it before exporting the library"
                    )
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id, expected_head_sha256=head.sha256
                )
                target = project_batch_interaction_target(publication)
                if target.stage_result.stage != "reference_unmixing":
                    raise ValueError("The selected review is not a reference-library operation")
                owner = target.stage_result.resume_context["service_context"]
                if owner.get("interaction_kind") != "reference_unmixing_operation":
                    raise ValueError("No accepted library is available at this step yet")
                state = owner["operation_state"]
                if state["review_summary"].get("portable_library_available") is not True:
                    raise ValueError("No accepted portable library is available to export yet")
                operation = ReferenceUnmixingOperation.open_existing(
                    Path(owner["operation_root_locator"])
                )
                current = operation.status(state["run_id"])
                if (
                    not isinstance(current, ReferenceUnmixingState)
                    or current.checkpoint_token != state["checkpoint_token"]
                ):
                    raise ValueError(
                        "The reference-library operation changed; reopen the current review"
                    )
                return operation.export_library(state["run_id"], destination)

    def kronos_cell_preview(self, batch_id: str, *, interaction_request_id: str,
            expected_state_head_sha256: str, settings: Mapping[str, object]) -> dict:
        """Read a bounded tissue field in the current supervised setup."""
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                head = store.load_head()
                if head.sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError('The analysis changed. Reopen classifier setup.')
                if interaction_request_id not in head.pending_interaction_ids:
                    raise BatchReviewPlotError('This classifier setup is no longer pending.')
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id, expected_head_sha256=head.sha256)
                target = project_batch_interaction_target(publication)
                if (target.interaction_request_id != interaction_request_id
                        or target.stage_result.stage != 'supervised_phenotype_training_plan_review'
                        or target.stage_result.interaction_reason != 'training_plan_selection_required'):
                    raise BatchReviewPlotError('Open the current supervised classifier setup to inspect cells.')
                request = store.reopen_interaction_base_authorization_request(
                    interaction_request_id, expected_head_sha256=head.sha256)
                authorities = tuple(store.authenticate_available_artifact(ref) for ref in request.input_artifacts)
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                from .kronos_cell_preview import prepare_kronos_cell_preview
                result = prepare_kronos_cell_preview(snapshot, authorities, dict(settings))
                if store.load_head().sha256 != head.sha256:
                    raise BatchHeadConflictError('The analysis changed while reading the tissue field. Reopen setup.')
                return result

    def secondary_marker_review(self, batch_id: str, *, interaction_request_id: str,
            expected_state_head_sha256: str, options: Mapping[str, object]) -> dict:
        """Review a regional marker threshold without changing cell phenotypes."""
        return BatchService._secondary_marker_read(self, batch_id,
            interaction_request_id=interaction_request_id,
            expected_state_head_sha256=expected_state_head_sha256, options=options, field=False)

    def secondary_marker_field(self, batch_id: str, *, interaction_request_id: str,
            expected_state_head_sha256: str, options: Mapping[str, object]) -> dict:
        """Inspect staining and outlines for the pending regional marker measurement."""
        return BatchService._secondary_marker_read(self, batch_id,
            interaction_request_id=interaction_request_id,
            expected_state_head_sha256=expected_state_head_sha256, options=options, field=True)

    def _secondary_marker_read(self, batch_id, *, interaction_request_id,
            expected_state_head_sha256, options, field):
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                head = store.load_head()
                if head.sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError('The analysis changed. Reopen the regional measurement review.')
                if interaction_request_id not in head.pending_interaction_ids:
                    raise BatchReviewPlotError('The regional measurement review is no longer pending.')
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id, expected_head_sha256=head.sha256)
                target = project_batch_interaction_target(publication)
                if (target.interaction_request_id != interaction_request_id
                        or target.stage_result.stage != 'secondary_structure_definition'):
                    raise BatchReviewPlotError('Open the secondary-structure measurement settings to review this threshold.')
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                from .secondary_marker_review import prepare_secondary_marker_review, prepare_secondary_marker_field
                context = target.stage_result.resume_context
                result = (prepare_secondary_marker_field(self._provider, snapshot, context,
                            dict(options), dependencies=self._dependencies) if field else
                          prepare_secondary_marker_review(snapshot, context, dict(options),
                            dependencies=self._dependencies))
                if store.load_head().sha256 != head.sha256:
                    raise BatchHeadConflictError('The analysis changed while reading the measurements. Reopen the review.')
                return result

    def preview_marker_combinations(
        self,
        batch_id: str,
        *,
        interaction_request_id: str,
        expected_state_head_sha256: str,
        phenotype_definition: Mapping[str, object],
        pattern_page: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Count draft assignments from the exact pending combination request."""
        from .batch_phenotyping.marker_combination_preview import preview_from_snapshot, preview_from_population, preview_pattern_page
        from .batch_phenotyping.marker_combinations import validate_combination_response

        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                head = store.load_head()
                cached = self._provider._marker_combination_population
                if cached is not None and cached[0][0] != head.sha256:
                    self._provider._clear_combination_population()
                if head.sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError("The review changed; reopen phenotype combinations.")
                if interaction_request_id not in head.pending_interaction_ids:
                    raise BatchReviewPlotError("The phenotype combination request is no longer pending.")
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id, expected_head_sha256=head.sha256)
                target = project_batch_interaction_target(publication)
                if target.interaction_request_id != interaction_request_id:
                    raise BatchReviewPlotError("The phenotype combination request has changed.")
                population_key = (head.sha256, interaction_request_id, target.interaction_context_sha256)
                cached = self._provider._marker_combination_population
                if cached is not None and cached[0] != population_key:
                    self._provider._clear_combination_population()
                    cached = None
                if cached is not None:
                    from .batch_phenotyping._common import digest
                    from copy import deepcopy
                    definition = validate_combination_response(target.stage_result,
                        {'phenotype_definition': phenotype_definition})['phenotype_definition']
                    prior = self._provider._marker_gate_examples
                    preview_key = ('marker-combinations', head.sha256, target.interaction_context_sha256, digest(definition))
                    if prior is not None and prior[0] == preview_key:
                        result = (preview_pattern_page(Path(cached[1].name) / 'patterns.sqlite', cached[2],
                            definition, dict(pattern_page)) if pattern_page is not None else deepcopy(prior[1]))
                        if store.load_head().sha256 != head.sha256:
                            raise BatchHeadConflictError('The review changed while reading assignments; reopen it.')
                        return result
                temporary = None
                try:
                    if cached is None:
                        temporary = tempfile.TemporaryDirectory(prefix="mif-combination-population-")
                        path = Path(temporary.name) / "patterns.sqlite"
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        result = preview_from_snapshot(snapshot, target.stage_result, phenotype_definition,
                                                       pattern_path=path)
                    else:
                        _key, directory, markers, total = cached
                        definition = validate_combination_response(target.stage_result,
                            {"phenotype_definition": phenotype_definition})["phenotype_definition"]
                        result = preview_from_population(Path(directory.name) / "patterns.sqlite", markers, total, definition)
                    if store.load_head().sha256 != head.sha256:
                        self._provider._clear_combination_population()
                        raise BatchHeadConflictError("The review changed while counting assignments; reopen it.")
                    if temporary is not None:
                        self._provider._marker_combination_population = (
                            population_key, temporary, tuple(result["observed_patterns"]["markers"]), result["total"])
                        temporary = None
                finally:
                    if temporary is not None:
                        temporary.cleanup()
                from copy import deepcopy
                self._provider._marker_gate_examples = (
                    ("marker-combinations", head.sha256, target.interaction_context_sha256,
                     result["definition_sha256"]), deepcopy(result))
                if pattern_page is not None:
                    cached = self._provider._marker_combination_population
                    definition = validate_combination_response(target.stage_result,
                        {'phenotype_definition': phenotype_definition})['phenotype_definition']
                    result = preview_pattern_page(Path(cached[1].name) / 'patterns.sqlite', cached[2],
                        definition, dict(pattern_page))
                    if store.load_head().sha256 != head.sha256:
                        raise BatchHeadConflictError('The review changed while reading patterns; reopen it.')
                return result

    def saved_supervised_review(self, batch_id: str, *, artifact_ref_sha256: str,
            expected_state_head_sha256: str) -> dict:
        """Read the current saved assignment context for the existing review panel."""
        from .batch_phenotyping.supervised_cohort_review import saved_prediction_context, prediction_owners
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_state_head_sha256)
            context = saved_prediction_context(snapshot, self._dependencies, artifact_ref_sha256)
            prediction_owners(snapshot, context, self._dependencies)
            if store.load_head().sha256 != snapshot.head.sha256:
                raise BatchHeadConflictError('The saved assignments changed while opening their review.')
            return context

    def supervised_prediction_page(self, batch_id: str, *, interaction_request_id: str | None = None,
            artifact_ref_sha256: str | None = None,
            expected_state_head_sha256: str, settings: Mapping[str, object]) -> dict:
        """Read an exact visible score page from a current pending or saved prediction.

        The provider retains one immutable prediction index. Every read checks
        the request and execution head; scrolling creates no plot-cache files.
        """
        from .batch_phenotyping.supervised_prediction_review import PredictionReviewIndex
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                head = store.load_head()
                if head.sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError('The prediction review changed while reading cells.')
                from .batch_phenotyping.supervised_cohort_review import prediction_review_context
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                context, review_identity = prediction_review_context(snapshot, self._dependencies,
                    interaction_request_id, artifact_ref_sha256, cached_index=self._provider._prediction_review_index)
                key = (head.sha256, review_identity)
                cached = self._provider._prediction_review_index
                cohort_key = (*key, 'cohort', json.dumps({name:value for name,value in settings.items()
                    if name not in {'offset','page_size','revision'}}, sort_keys=True))
                from .batch_phenotyping.supervised_cohort_review import (
                    prediction_owners, cohort_prediction_page, cached_prediction_page,
                )
                if cached is not None and cached[0] == cohort_key:
                    result = cached_prediction_page(cached[1]['metadata'], cached[2], dict(settings))
                    result['request'] = dict(settings)
                    if store.load_head().sha256 != head.sha256:
                        raise BatchHeadConflictError('The prediction review changed while reading cells.')
                    return result
                if cached is None or cached[0] != key:
                    if cached is not None and cached[0][:3] == (*key, 'cohort'):
                        owners = cached[1]['owners']
                    else:
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        owners = prediction_owners(snapshot, context, self._dependencies)
                    if owners:
                        result, index = cohort_prediction_page(owners, context, self._dependencies, dict(settings))
                        metadata = {name:result[name] for name in ('summary','cohort_images','active_image','pending_owner','processing_groups')}
                        self._provider._prediction_review_index = (cohort_key, dict(owners=owners,metadata=metadata,context=context), index)
                        result['request'] = dict(settings)
                        if store.load_head().sha256 != head.sha256:
                            raise BatchHeadConflictError('The prediction review changed while reading cells.')
                        return result
                if cached is None or cached[0] != key:
                    stored = self._dependencies.phenotyping.supervised_prediction_store.reopen(
                        context['sha256'], context['byte_count'])
                    prediction = stored.prediction
                    snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                    if not any(a.artifact_ref.artifact_ref_sha256 == prediction.measurement_result_ref_sha256
                               and a.artifact_ref.artifact_kind == 'measurements_result'
                               for a in snapshot.committed_artifact_authorities):
                        raise BatchReviewPlotError('The prediction measurement is no longer current.')
                    index = PredictionReviewIndex(prediction.rows, prediction.class_ids)
                    self._provider._prediction_review_index = (key, prediction, index)
                else:
                    _, prediction, index = cached
                checked = index.settings(settings)
                if (context['score_kind'] == 'native_svm_pairwise_vote_fraction'
                        and context['class_count'] == 2 and checked['cutoff'] != 0):
                    raise BatchReviewPlotError('Binary SVM vote agreement cannot reject uncertain cells.')
                result = index.page(checked)
                result['summary'] = dict(result['summary'], sample_id=prediction.sample_id,
                    project_entry_id=prediction.project_entry_id, prediction_sha256=context['sha256'],
                    score_kind=context['score_kind'], class_count=len(prediction.class_ids))
                result['request'] = dict(settings)
                if store.load_head().sha256 != head.sha256:
                    raise BatchHeadConflictError('The prediction review changed while reading cells.')
                return result

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
        region_cells: Mapping[str, object] | None = None,
    ) -> BatchReviewPlots:
        """Materialize temporary figures for one authenticated current review."""
        from .batch_training.composition import reopen_current_training_fit_review
        from .batch_training.review_plots import render_training_review_plots

        if cluster_gallery_settings is not None:
            return self._cluster_cell_plots(batch_id,interaction_request_id,
                expected_state_head_sha256,cluster_gallery_settings)
        if umap_settings is not None:
            return self._cluster_review_plots(batch_id, interaction_request_id,
                expected_state_head_sha256, umap_settings, attribute)

        with self._provider._use():
            self._require_batch(batch_id)
            if (
                type(interaction_request_id) is not str
                or not interaction_request_id
                or type(expected_state_head_sha256) is not str
                or len(expected_state_head_sha256) != 64
                or any(char not in "0123456789abcdef" for char in expected_state_head_sha256)
            ):
                raise BatchReviewPlotError("review plots require an exact request and head")
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                head = store.load_head()
                if head.sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError("review plot execution head changed")
                if interaction_request_id not in head.pending_interaction_ids:
                    raise BatchReviewPlotError("review plot request is not pending")
                publication = store.reopen_interaction_request_publication(
                    interaction_request_id,
                    expected_head_sha256=head.sha256,
                )
                target = project_batch_interaction_target(publication)
                if target.interaction_request_id != interaction_request_id:
                    raise BatchReviewPlotError(
                        "review plot request differs from current publication"
                    )
                from .region_review_image import REGION_REASONS, render_region_review_image
                region_image = target.stage_result.interaction_reason in REGION_REASONS
                if region_cells is not None:
                    from .secondary_region_cells import render_secondary_region_cells
                    from dataclasses import replace
                    if any(value is not None for value in (marker_gallery_settings,
                            combination_gallery_settings, supervised_gallery_settings, prediction_review_settings)):
                        raise BatchReviewPlotError('Choose one image inspection.')
                    snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                    outputs = render_secondary_region_cells(store, snapshot, target, dict(region_cells))
                    if store.load_head().sha256 != head.sha256:
                        raise BatchHeadConflictError('The region review changed while loading cells.')
                    plots = tuple(replace(plot, display_options={'region_cells': dict(region_cells)})
                                  for plot in self._provider._write_review_plots(outputs))
                    # Retain only the latest field; panning must not build a cache
                    # of geometry files or consume the ordinary figure allowance.
                    previous = getattr(self._provider, '_region_cell_plots', ())
                    self._provider._region_cell_plots = plots
                    for plot in previous:
                        Path(plot.path).unlink(missing_ok=True)
                    return BatchReviewPlots(target_id=target.target_id,
                        interaction_request_id=interaction_request_id, state_head_sha256=head.sha256,
                        interaction_context_sha256=target.interaction_context_sha256, plots=plots)
                marker_image = marker_gallery_settings is not None
                combination_image = combination_gallery_settings is not None
                supervised_image = supervised_gallery_settings is not None
                prediction_review = prediction_review_settings is not None
                if sum((marker_image, combination_image, supervised_image, prediction_review)) > 1:
                    raise BatchReviewPlotError("Choose one cell example review.")
                if prediction_review and (target.stage_result.interaction_reason != 'supervised_classifier_review_required'
                        or target.stage_result.resume_context.get('phase') != 'prediction_review'):
                    raise BatchReviewPlotError('Assignment review requires the current pending supervised predictions.')
                if supervised_image and target.stage_result.stage != 'supervised_phenotype_training_review':
                    raise BatchReviewPlotError('Cell evaluation images require the current supervised fit review.')
                if marker_image and target.stage_result.interaction_reason != "marker_threshold_review_required":
                    raise BatchReviewPlotError("Cell examples require the current pending marker review.")
                if prediction_review:
                    cache_key = (target.interaction_context_sha256,
                        json.dumps([head.sha256,dict(prediction_review_settings)],sort_keys=True),
                        'supervised-prediction-review-v1')
                elif combination_image:
                    from .batch_phenotyping.marker_combinations import validate_combination_response
                    from .batch_phenotyping.marker_combination_gallery import retained_examples
                    definition = validate_combination_response(target.stage_result, {
                        "phenotype_definition": combination_gallery_settings.get("phenotype_definition")
                    })["phenotype_definition"]
                    retained_examples(head, target, definition, combination_gallery_settings, self._provider)
                    cache_key = (target.interaction_context_sha256,
                        json.dumps([head.sha256, dict(combination_gallery_settings)], sort_keys=True),
                        "phenotype-cell-gallery-v1")
                elif marker_image:
                    cache_key = (target.interaction_context_sha256,
                        json.dumps(dict(marker_gallery_settings), sort_keys=True), "marker-gallery-v1")
                elif region_image:
                    target = self._present_tissue_review(store, target)
                    cache_key = (target.interaction_context_sha256, attribute, "region-image-v2")
                else:
                    context, pending, _, review_plans = reopen_current_training_fit_review(
                        target,
                        dependencies=self._dependencies.training,
                        expected_state_head_sha256=head.sha256,
                    )
                    if pending is None:
                        raise BatchReviewPlotError("Classifier plots are available after fitting.")
                    review_digest = context["fit_review_sha256"]
                    cache_key = (
                        review_digest,
                        target.interaction_context_sha256,
                        _REVIEW_PLOT_RENDERER_VERSION,
                    )
                    if supervised_image:
                        cache_key += (json.dumps(dict(supervised_gallery_settings), sort_keys=True),
                                      'supervised-cell-gallery-v1')
                cached = self._provider._plot_cache.get(cache_key)
                if cached is not None:
                    plots = cached
                else:
                    resolved_options = None
                    if prediction_review:
                        from .batch_phenotyping.supervised_prediction_review import render_prediction_review
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        indexed = self._provider._prediction_review_index
                        indexed = indexed[1:] if indexed is not None and indexed[0] == (
                            head.sha256, target.interaction_context_sha256) else None
                        outputs, resolved_options = render_prediction_review(snapshot,
                            target.stage_result.resume_context,self._dependencies,dict(prediction_review_settings),
                            prediction_index=indexed)
                    elif combination_image:
                        from .batch_phenotyping.marker_combination_gallery import render_combination_gallery
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        outputs, resolved_options = render_combination_gallery(
                            snapshot, target, dict(combination_gallery_settings), self._provider)
                    elif marker_image:
                        from .batch_phenotyping.marker_review_gallery import render_marker_gallery
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        outputs, resolved_options = render_marker_gallery(snapshot,
                            target.stage_result.resume_context, dict(marker_gallery_settings), self._provider)
                    elif region_image:
                        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                        outputs = render_region_review_image(store, snapshot, target, attribute)
                    else:
                        from .batch_training.review_tables import load_classifier_review_evidence
                        reports, votes = load_classifier_review_evidence(pending, review_plans, self._dependencies)
                        if supervised_image:
                            from .batch_training.supervised_review_gallery import render_supervised_gallery
                            snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                            outputs, resolved_options = render_supervised_gallery(
                                snapshot, pending, review_plans, votes, dict(supervised_gallery_settings))
                        else:
                            outputs = render_training_review_plots(context, pending, review_plans,
                                                                   selection_reports=reports, reference_votes=votes)
                    if len(
                        {(artifact_id, format_) for artifact_id, _, format_, _ in outputs}
                    ) != len(outputs):
                        raise BatchReviewPlotError("review plots contain duplicate figures")
                    if any(
                        type(artifact_id) is not str
                        or not artifact_id
                        or type(title) is not str
                        or not title
                        or format_ not in {"png", "pdf", "svg", "csv", "npz"}
                        or type(data) is not bytes
                        for artifact_id, title, format_, data in outputs
                    ):
                        raise BatchReviewPlotError("review plot renderer returned invalid bytes")
                    added_bytes = sum(len(data) for _, _, _, data in outputs)
                    if self._provider._plot_bytes + added_bytes > _REVIEW_PLOT_CACHE_LIMIT_BYTES:
                        raise BatchReviewPlotError(
                            "review plot cache is full; reopening the analysis clears temporary figures"
                        )
                    plots = self._provider._write_review_plots(outputs)
                    if resolved_options is not None:
                        from dataclasses import replace
                        plots = tuple(replace(plot, display_options=resolved_options) for plot in plots)
                    self._provider._plot_cache[cache_key] = plots
                    self._provider._plot_bytes += added_bytes
                if store.load_head().sha256 != head.sha256:
                    raise BatchHeadConflictError("review plot execution head changed")
                return BatchReviewPlots(
                    target_id=target.target_id,
                    interaction_request_id=interaction_request_id,
                    state_head_sha256=head.sha256,
                    interaction_context_sha256=target.interaction_context_sha256,
                    plots=plots,
                )

    def cluster_field(self, batch_id: str, *, interaction_request_id: str,
            expected_state_head_sha256: str, figure_options: Mapping[str, object]) -> dict:
        """Read the current fitted group's exact cells in bounded tissue context."""
        from .batch_phenotyping.cluster_spatial_review import cluster_field
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_state_head_sha256)
                if interaction_request_id not in snapshot.head.pending_interaction_ids:
                    raise BatchReviewPlotError('The cluster review is no longer pending.')
                group = self._reopen_aggregate_interaction_group(store, snapshot.head, interaction_request_id)
                bundle = cluster_field(self._provider, snapshot, group.evidence, interaction_request_id, dict(figure_options))
                if store.load_head().sha256 != expected_state_head_sha256:
                    raise BatchHeadConflictError('The cluster review changed while loading its tissue view.')
                return bundle

    def cluster_model_view(self, batch_id: str, *, interaction_request_id: str,
            expected_state_head_sha256: str) -> dict | None:
        """Read learned profiles for the current review without fitting or loading measurements."""
        from .batch_phenotyping.flowsom_review import model_view
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_state_head_sha256)
            if interaction_request_id not in snapshot.head.pending_interaction_ids:
                raise BatchReviewPlotError('The cluster review is no longer pending.')
            group = self._reopen_aggregate_interaction_group(store, snapshot.head, interaction_request_id)
            result = model_view(snapshot, group.evidence)
            if store.load_head().sha256 != expected_state_head_sha256:
                raise BatchHeadConflictError('The cluster review changed while loading its learned profiles.')
            return result

    def _cluster_cell_plots(self,batch_id,request_id,expected_head,settings):
        """Read bounded exact image examples from the current aggregate decision."""
        from .batch_phenotyping.cluster_review_gallery import render_cluster_cells
        with self._provider._use():
            self._require_batch(batch_id)
            store=self._dependencies.runtime.state_store
            with self._provider._plot_lock:
                snapshot=store.reopen_execution_snapshot(expected_head_sha256=expected_head)
                group=self._reopen_aggregate_interaction_group(store,snapshot.head,request_id)
                key=('cluster-cells',expected_head,request_id,json.dumps(dict(settings),sort_keys=True,allow_nan=False))
                plots=self._provider._plot_cache.get(key)
                if plots is None:
                    outputs,appearance=render_cluster_cells(snapshot,group.evidence,dict(settings))
                    if any(fmt not in {'png','pdf','svg','csv','npz'} or type(data) is not bytes for _,_,fmt,data in outputs):
                        raise BatchReviewPlotError('Cluster image review returned invalid figure data.')
                    size=sum(len(data) for _,_,_,data in outputs)
                    if self._provider._plot_bytes+size>_REVIEW_PLOT_CACHE_LIMIT_BYTES:
                        raise BatchReviewPlotError('The temporary figure folder is full. Reopen the analysis to clear it.')
                    plots=tuple(replace(plot,display_options=appearance) for plot in self._provider._write_review_plots(outputs))
                    self._provider._plot_cache[key]=plots;self._provider._plot_bytes+=size
                if store.load_head().sha256!=expected_head:
                    raise BatchHeadConflictError('The cluster review changed while loading cells.')
                return BatchReviewPlots(target_id=group.target.target_id,interaction_request_id=request_id,
                    state_head_sha256=expected_head,interaction_context_sha256=group.target.interaction_context_sha256,plots=plots)

    def _cluster_review_plots(self, batch_id, request_id, expected_head, settings, attribute):
        """Calculate a view for the exact pending review without changing its labels."""
        from mif_pipeline.cell_analysis.embeddings import EmbeddingPolicy

        policy = EmbeddingPolicy(**dict(settings))
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("cluster-review-plot"):
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head)
                group = self._reopen_aggregate_interaction_group(store, snapshot.head, request_id)
                rendered = self._render_cluster_umap(snapshot, group.evidence, group.target.node_id, policy, attribute)
                request={'umap_settings':{name:getattr(policy,name) for name in ('max_cells','neighbors','min_dist','seed')},
                         'attribute':attribute}
                rendered=tuple(replace(plot,display_options={'umap_request':request}) for plot in rendered)
                if store.load_head().sha256 != expected_head:
                    raise BatchHeadConflictError("The review changed while calculating UMAP; reopen the current review.")
                return BatchReviewPlots(
                    target_id=group.target.target_id, interaction_request_id=request_id,
                    state_head_sha256=expected_head,
                    interaction_context_sha256=group.target.interaction_context_sha256,
                    plots=rendered)

    def _saved_clustering_umap(self, batch_id, expected_head, artifact_ref_sha256, options):
        """Bind a derived UMAP view to a current accepted result without reopening its decision."""
        from mif_pipeline.cell_analysis.embeddings import EmbeddingPolicy
        from .batch_phenotyping.review_authority import reopen_cluster_review_evidence_authority

        options = dict(options or {})
        if set(options) - {"umap_settings", "attribute"}:
            raise BatchReviewPlotError("Unsupported UMAP view setting.")
        policy = EmbeddingPolicy(**dict(options.get("umap_settings", {})))
        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("saved-clustering-plot"):
                snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head)
                authority = next((item for item in snapshot.committed_artifact_authorities
                    if item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
                if authority is None or authority.artifact_ref.artifact_kind != "cluster_review_evidence":
                    raise BatchReviewPlotError("The selected clustering result is no longer current.")
                evidence = reopen_cluster_review_evidence_authority(authority)
                rendered = self._render_cluster_umap(snapshot, evidence,
                    authority.artifact_ref.producer_node_id, policy, options.get("attribute", "cluster_id"))
                if store.load_head().sha256 != expected_head:
                    raise BatchHeadConflictError("The clustering result changed while calculating UMAP; reopen Results.")
                return rendered

    def _render_cluster_umap(self, snapshot, evidence, node_id, policy, attribute):
        """Reuse or calculate coordinates under the existing memory and rendering limits.

        Both callers hold the writer lease and supply their checked current evidence.
        The cache uses the clustering result, measurements and UMAP settings; changing
        colour only renders those coordinates again. No scientific record is written.
        """
        from mif_pipeline.pipeline_core.execution_resources import operation_budget
        from .batch_resources import ResourceLeasePool
        from .batch_phenotyping import review_plots as plots

        with self._lock:
            if self._active_wave is not None:
                raise BatchReviewPlotError("Wait for processing to finish, then calculate the UMAP view.")
        count = evidence.cell_count
        feature_count = len(evidence.clusters[0].marker_profiles)
        population_bytes, workspace_bytes = plots.memory_allowances(count, feature_count, policy)
        key = plots.view_key(evidence, policy)
        path = plots.cache_path(self._workspace_layout.summaries, key)
        cached = path.exists()
        memory_bytes = ((128 * 1024**2 + 512 * count + 32 * min(count, policy.max_cells) * feature_count)
                        if cached else population_bytes + workspace_bytes)
        pool = ResourceLeasePool(snapshot.manifest.manifest.execution_config)
        demand = BatchResourceDemand(
            node_id=node_id,
            adapter_spec_sha256=_canonical_sha256({"operation": "umap-review"}),
            input_artifact_set_sha256=key, resource_kind=ResourceKind.SCIENTIFIC_COMPUTE,
            worker_slots=1, future_slots=1, working_set_bytes=memory_bytes, cpu_threads=1)
        def acquire(working_bytes):
            requested = replace(demand, working_set_bytes=working_bytes)
            try:
                reserved = pool.try_acquire(requested)
                if reserved is None:
                    raise BatchReviewPlotError("Memory is in use. Wait for other processing to finish and calculate the view again.")
                return reserved
            except ValueError as error:
                guidance = (f"Preparing all {count:,} cells requires an estimated {population_bytes / 1024**3:.2f} GiB; "
                            f"the sampled UMAP calculation requires {workspace_bytes / 1024**3:.2f} GiB. "
                            "Reducing Maximum cells reduces only the sampled calculation. "
                            "For the complete population, free memory or use a larger cluster allocation. ")
                raise BatchReviewPlotError(guidance + str(error)) from error
        reservation = acquire(memory_bytes)
        try:
            with operation_budget(memory_bytes, 1), self._provider._plot_lock:
                _, result, resolved, features = plots.prepare_view(evidence, snapshot, policy)
                view = plots.read_view(path, key, features)
                if view is None:
                    if cached:
                        reservation.release()
                        reservation = acquire(population_bytes + workspace_bytes)
                    with operation_budget(population_bytes + workspace_bytes, 1):
                        view = plots.calculate_view(result, resolved, features, policy)
                    plots.save_view(path, key, view)
                render_key = ("umap", view.content_hash, attribute)
                rendered = self._provider._plot_cache.get(render_key)
                if rendered is None:
                    outputs = plots.render_view(view, attribute)
                    added_bytes = sum(len(output[3]) for output in outputs)
                    if self._provider._plot_bytes + added_bytes > _REVIEW_PLOT_CACHE_LIMIT_BYTES:
                        raise BatchReviewPlotError("The temporary figure cache is full. Reopen the analysis; saved UMAP coordinates will be reused.")
                    rendered = self._provider._write_review_plots(outputs)
                    self._provider._plot_cache[render_key] = rendered
                    self._provider._plot_bytes += added_bytes
                return rendered
        finally:
            if reservation.is_live:
                reservation.release()

    def apply_interaction_response(
        self,
        batch_id: str,
        request: BatchInteractionResponseRequest,
    ) -> BatchInteractionApplicationResult:
        """Apply a researcher response to the exact current review and selected targets.

        The method validates the request identity, offered action, target selection and
        expected state before delegating scientific fields to their stage owner. An accepted
        response is recorded through the batch state boundary and may enable new work.
        Rejected or stale input leaves the existing review available."""

        with self._provider._use():
            self._require_batch(batch_id)
            store = self._dependencies.runtime.state_store
            with store.acquire_writer_lease("apply-interaction-response") as lease:
                head = store.load_head()
                if head.sha256 != request.expected_state_head_sha256:
                    raise BatchInteractionError("interaction response head changed")
                targets: list[BatchInteractionTarget] = []
                aggregate_groups: list[BatchAggregateInteractionGroup] = []
                for request_id in head.pending_interaction_ids:
                    try:
                        publication = store.reopen_interaction_request_publication(
                            request_id, expected_head_sha256=head.sha256
                        )
                        targets.append(project_batch_interaction_target(publication))
                    except (BatchInteractionError, BatchStateError) as ordinary_error:
                        try:
                            aggregate_groups.append(
                                self._reopen_aggregate_interaction_group(store, head, request_id)
                            )
                        except (BatchInteractionError, BatchStateError):
                            raise ordinary_error
                groups = group_interactions(tuple(targets))
                group = next(
                    (
                        item
                        for item in groups
                        if item.group_id == request.group_id
                        and item.group_sha256 == request.group_sha256
                    ),
                    None,
                )
                aggregate = next(
                    (
                        item
                        for item in aggregate_groups
                        if item.group_id == request.group_id
                        and item.group_sha256 == request.group_sha256
                    ),
                    None,
                )
                if group is not None and aggregate is not None:
                    raise BatchInteractionError("interaction group identity is ambiguous")
                if group is None and aggregate is None:
                    raise BatchInteractionError("interaction group is not current")
                validator = None
                if group is not None:
                    try:
                        validators = tuple(
                            type(self._dependencies.interaction_owners).resolve(
                                self._dependencies.interaction_owners, item
                            )
                            for item in group.targets
                        )
                    except BatchInteractionError:
                        return self._unsupported_interaction_result(
                            group=group,
                            request=request,
                            state_head_sha256=head.sha256,
                        )
                    if not validators or any(item is not validators[0] for item in validators[1:]):
                        raise BatchInteractionError(
                            "bulk interaction targets require one common owner"
                        )
                    validator = validators[0]
                if head.execution_state is BatchExecutionState.PAUSED:
                    with self._lock:
                        if self._active_wave is not None:
                            raise BatchServiceError("paused response requires a retired batch wave")
                    snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                    scheduler = BatchScheduler(
                        snapshot.manifest.manifest.execution_config,
                        store,
                        lease,
                        StageAdapterRegistry(()),
                        lambda node, spec, inputs: self._prepare_dispatch(
                            lease, node, spec, inputs
                        ),
                        self._estimate_current_demand,
                    )
                    if not self._dependencies.runtime.wait_for_project_stage_quiescence():
                        raise BatchServiceError("project stage did not drain")
                    with store.current_project_control(
                        operation="interaction-response", lease=lease
                    ) as control:
                        if not self._dependencies.runtime.wait_for_project_stage_quiescence():
                            raise BatchServiceError("project stage did not remain drained")
                        with paused_response_authority(
                            state_store=store,
                            batch_id=batch_id,
                            session_id="batch-service-response-" + uuid4().hex,
                            scheduler=scheduler,
                            workspace_lease=lease,
                            project_lease=control.project_lease,
                            project_processes=control.project_processes,
                        ) as authority:
                            if aggregate is not None:
                                return self._apply_aggregate_interaction_response(
                                    store=store,
                                    head=head,
                                    group=aggregate,
                                    request=request,
                                    lease=lease,
                                    paused_response_authority=authority,
                                )
                            assert group is not None and validator is not None
                            return apply_group_response(
                                group,
                                request,
                                validator,
                                store,
                                lease,
                                paused_response_authority=authority,
                            )
                if aggregate is not None:
                    return self._apply_aggregate_interaction_response(
                        store=store,
                        head=head,
                        group=aggregate,
                        request=request,
                        lease=lease,
                    )
                assert group is not None and validator is not None
                return apply_group_response(group, request, validator, store, lease)

    def cancel(self, batch_id: str) -> BatchCancellationResult:
        with self._provider._use():
            self._require_batch(batch_id)
            head = self._dependencies.runtime.state_store.load_head()
            return BatchCancellationResult(
                batch_id,
                False,
                (),
                head.sha256,
                "No prototype adapter declares safe application-level cancellation.",
            )

    def _require_batch(self, batch_id: str) -> None:
        if batch_id != self._workspace_layout.batch_id:
            raise BatchServiceError("service lookup names a foreign batch")

    def _validate(self, manifest_path: Path, *, remember: bool) -> ValidatedBatchManifest:
        if not isinstance(manifest_path, Path):
            raise BatchServiceError("manifest_path must be a Path")
        path = manifest_path.expanduser().resolve(strict=True)
        data = path.read_bytes()
        with self._lock:
            previous = self._validated_bytes_by_path.get(path)
            if previous is not None and previous != data:
                raise BatchServiceError("manifest bytes changed after service validation")
        try:
            parsed = BatchManifest.from_json_bytes(data)
            store = self._dependencies.runtime.state_store
            if store.layout.state_head_path.exists():
                snapshot = store.reopen_execution_snapshot()
                validated = _selected_manifest_for_authoring(store, data, snapshot, parsed)
            else:
                validated = validate_manifest(parsed)
        except Exception as exc:
            if isinstance(exc, BatchServiceError):
                raise
            raise BatchServiceError("manifest validation failed") from exc
        manifest = validated.manifest
        if (
            manifest.batch_id != self._workspace_layout.batch_id
            or manifest.output_root.resolve(strict=False) != self._workspace_layout.output_root
        ):
            raise BatchServiceError("manifest names a foreign workspace")
        if self._manifest_path is not None and path != self._manifest_path:
            raise BatchServiceError("service is bound to a different manifest")
        if remember:
            with self._lock:
                self._validated_bytes_by_path[path] = data
        return validated

    def _plan(self, manifest_path: Path) -> BatchPlan:
        validated = self._validate(manifest_path, remember=True)
        store = self._dependencies.runtime.state_store
        if self._workspace_layout.state_head_path.exists():
            snapshot = store.reopen_execution_snapshot()
            plan = snapshot.plan
            kinds = self._remaining_prerequisite_kinds(snapshot)
            requirements = self._collect_prerequisites(
                snapshot.manifest, plan, required_node_kinds=kinds) if kinds else ()
        else:
            registry = _application_stage_specs()
            plan = self._initial_plan(validated, registry)
            requirements = self._collect_prerequisites(validated, plan)
        return bind_batch_prerequisites(plan, requirements)

    def _initial_plan(self, manifest, registry) -> BatchPlan:
        if any(item.config.secondary_structures is None for item in manifest.enabled_samples):
            registry = _application_stage_specs(include_secondary_definitions=True)
        from .batch_training.accepted_model_reuse import import_batch_classifier_record_sets

        profiles = import_batch_semantic_profiles(manifest, registry)
        preliminary = plan_batch(manifest, registry, accepted_artifacts=profiles)
        classifiers = import_batch_classifier_record_sets(manifest)
        if not classifiers:
            return preliminary
        return plan_batch(manifest, registry, accepted_artifacts=(*profiles, *classifiers))

    def _collect_prerequisites(
        self,
        manifest: ValidatedBatchManifest,
        plan: BatchPlan,
        *,
        required_node_kinds: frozenset[str] | None = None,
    ) -> tuple[BatchPrerequisiteItem, ...]:
        supplier = self._provider._prerequisite_inputs
        inputs = (
            BatchPrerequisiteOwnerInputs(None, {})
            if supplier is None
            else supplier(manifest, plan)
        )
        return collect_batch_prerequisites(
            manifest=manifest,
            plan=plan,
            owner_inputs=inputs,
            required_node_kinds=required_node_kinds,
        )

    @staticmethod
    def _admit_prerequisites(
        observations: tuple[BatchPrerequisiteItem, ...],
        retained: tuple[BatchPrerequisiteItem, ...] | None = None,
        *,
        required_node_kinds: frozenset[str] | None = None,
    ) -> None:
        # Its selected-model preflight owns segmentation readiness. Batch
        # forecasts retain it, but unrelated work must not wait for that choice.
        if required_node_kinds is None:
            required_node_kinds = frozenset(
                kind for row in (*observations, *(retained or ()))
                for kind in row.required_by_node_kinds)
        required_node_kinds = required_node_kinds - {"segmentation"}
        observations = tuple(row for row in observations
                             if required_node_kinds.intersection(row.required_by_node_kinds))
        if retained is not None:
            retained = tuple(row for row in retained
                             if required_node_kinds.intersection(row.required_by_node_kinds))

        def requirements(rows: tuple[BatchPrerequisiteItem, ...]):
            return tuple(
                sorted(
                    (
                        row.prerequisite_id,
                        row.prerequisite_kind,
                        row.scope,
                        row.sample_id,
                        row.validation_domain_id,
                        (row.required_by_node_kinds if required_node_kinds is None else
                         tuple(kind for kind in row.required_by_node_kinds if kind in required_node_kinds)),
                    )
                    for row in rows
                    if required_node_kinds is None or required_node_kinds.intersection(row.required_by_node_kinds)
                )
            )

        comparison = observations
        if retained is not None and requirements(observations) != requirements(retained):
            from .setup.batch_prerequisites import equivalent_extension_location

            adjusted = []
            for row in observations:
                matches = tuple(old for old in retained if equivalent_extension_location(row, old))
                adjusted.append(
                    replace(row, prerequisite_id=matches[0].prerequisite_id)
                    if len(matches) == 1
                    else row
                )
            comparison = tuple(adjusted)
        if any(row.state != "ready" for row in observations) or (
            retained is not None and requirements(comparison) != requirements(retained)
        ):
            raise BatchPrerequisiteAdmissionError(observations)

    @staticmethod
    def _remaining_prerequisite_kinds(snapshot) -> frozenset[str]:
        """Identify software requirements from unfinished and deferred stages."""
        return frozenset(
            {node.kind for node in snapshot.plan.nodes
             if snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.COMPLETED}
            | {node.node_kind for node in snapshot.plan.deferred_nodes}
        )

    def _admit_remaining_prerequisites(self, snapshot) -> tuple[BatchPrerequisiteItem, ...]:
        """Check software needed by unfinished or still-deferred work.

        We use the current saved statuses to omit completed stage kinds from
        both live probing and the comparison with retained requirements. A new
        source, model or settings revision creates unfinished work and restores
        its software requirements through the same plan. Deferred stages remain
        included because the continuation can make them executable.
        """
        # Segmentation checks its exact selected model immediately before its
        # scientific invocation. It must not block unrelated saved work.
        kinds = self._remaining_prerequisite_kinds(snapshot) - {"segmentation"}
        if not kinds:
            return ()
        observed = self._collect_prerequisites(
            snapshot.manifest, snapshot.plan, required_node_kinds=kinds)
        self._admit_prerequisites(observed, snapshot.plan.prerequisites,
                                  required_node_kinds=kinds)
        return observed

    @staticmethod
    def _managed_worker(function: Callable[[], object]):
        values: list[object] = []
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                values.append(function())
            except BaseException as exc:
                failures.append(exc)

        worker = Thread(target=invoke, daemon=False)
        worker.start()
        worker.join()
        if failures:
            raise failures[0]
        return values[0]

    def _run_worker(
        self,
        manifest: ValidatedBatchManifest,
    ) -> BatchRunReport:
        store = self._dependencies.runtime.state_store
        committed: list[str] = []
        with store.acquire_writer_lease("run") as lease:
            if not self._workspace_layout.state_head_path.exists():
                registry = _application_stage_specs()
                initial_plan = self._initial_plan(manifest, registry)
                prerequisites = self._collect_prerequisites(manifest, initial_plan)
                self._admit_prerequisites(prerequisites)
                initial_plan = bind_batch_prerequisites(initial_plan, prerequisites)
                with self._lock:
                    head = store.initialize(initial_plan, manifest, lease)
                    snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
            else:
                snapshot = store.reopen_execution_snapshot()
                if (
                    snapshot.manifest.original_manifest_sha256 != manifest.original_manifest_sha256
                    or snapshot.manifest.normalized_manifest_sha256
                    != manifest.normalized_manifest_sha256
                ):
                    raise BatchServiceError("retained state belongs to a different manifest")
                prerequisites = self._admit_remaining_prerequisites(snapshot)
            return self._drive_fixed_point(
                snapshot,
                lease,
                committed,
                initial_scheduler=None,
                prerequisites=prerequisites,
            )

    def _consume_retained_pause(self, snapshot, lease, scheduler):
        """Restore active execution through this revision's exact pause proof.

        Resume and explicit Retry share the same quiescence and project-control
        transition. The admission fence lowers only after the proof is consumed
        and the control lease completes; waiting reviews remain in the head.
        """
        store = self._dependencies.runtime.state_store
        scheduler.assert_no_admitted_work()
        with store.current_project_control(operation="resume", lease=lease) as control:
            consume_quiescent_pause(
                state_store=store,
                batch_id=snapshot.head.batch_id,
                pause_record_sha256=snapshot.head.quiescent_pause_sha256,
                scheduler=scheduler,
                workspace_lease=lease,
                project_lease=control.project_lease,
                project_processes=control.project_processes,
            )
        with self._lock:
            self._pause_blocks_admission = False
        return store.reopen_execution_snapshot()

    def _resume_worker(self, batch_id: str) -> BatchRunReport:
        store = self._dependencies.runtime.state_store
        with store.acquire_writer_lease("resume") as lease:
            try:
                paused = store.load_head()
            except Exception as exc:
                raise BatchServiceError("resume requires retained state") from exc
            if paused.batch_id != batch_id:
                raise BatchServiceError("paused state belongs to another batch")
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=paused.sha256)
            prerequisites = self._admit_remaining_prerequisites(snapshot)
            registry = build_batch_stage_registry(
                plan=snapshot.plan,
                resolved_samples=snapshot.manifest.enabled_samples,
                dependencies=self._dependencies,
            )
            scheduler = self._new_scheduler(snapshot, lease, registry)
            active = self._consume_retained_pause(snapshot, lease, scheduler)
            return self._drive_fixed_point(
                active,
                lease,
                [],
                initial_scheduler=scheduler,
                initial_registry=registry,
                prerequisites=prerequisites,
                reconcile_before_dispatch=True,
            )

    def _continue_worker(self, expected_head_sha256: str) -> BatchRunReport:
        store = self._dependencies.runtime.state_store
        with store.acquire_writer_lease("continue") as lease:
            with self._lock:
                if self._active_wave is not None or self._pause_blocks_admission:
                    raise BatchServiceError("saved-work continuation requires an idle active analysis")
            snapshot = store.reopen_execution_snapshot(
                expected_head_sha256=expected_head_sha256
            )
            if not self._status_from_snapshot(snapshot).continuation_available:
                raise BatchServiceError("No saved work is available to continue at this revision. Refresh the analysis.")
            prerequisites = self._admit_remaining_prerequisites(snapshot)
            return self._drive_fixed_point(
                snapshot, lease, [], initial_scheduler=None, prerequisites=prerequisites,
                reconcile_before_dispatch=True,
            )

    def _pause_recovered_reference_form(self, snapshot, lease, registry):
        """Publish the normal resumable pause after a read-only form recovery."""
        if not snapshot.head.pending_interaction_ids:
            raise BatchServiceError("There is no recovered reference form to pause.")
        store = self._dependencies.runtime.state_store
        for request_id in snapshot.head.pending_interaction_ids:
            result = store.load_interaction_request(request_id)
            service = (result.resume_context or {}).get("service_context", {})
            state = service.get("operation_state", {})
            initial_component_review = (
                service.get("interaction_kind") == "configure_reference_unmixing"
                and service.get("operation_status") == "unstarted"
            )
            if result.stage != "reference_unmixing" or (
                state.get("status") != "awaiting_operation" and not initial_component_review
            ):
                raise BatchServiceError(
                    "The pending review differs from the recovered spectral-reference form."
                )
        scheduler = self._new_scheduler(snapshot, lease, registry)
        scheduler.assert_no_admitted_work()
        receipt = scheduler.quiesce()
        if not self._dependencies.runtime.wait_for_project_stage_quiescence():
            raise BatchServiceError(
                "Wait for the image operation to finish before reopening this form."
            )
        with store.current_project_control(operation="pause", lease=lease) as control:
            control.project_processes.assert_all_drained(
                batch_id=snapshot.head.batch_id,
                project_identity_sha256=control.head.project_identity_sha256,
            )
            pause = publish_quiescent_pause(
                state_store=store,
                batch_id=snapshot.head.batch_id,
                pausing_session_id="reference-form-recovery-" + uuid4().hex,
                scheduler=scheduler,
                quiescence_receipt=receipt,
                workspace_lease=lease,
                project_lease=control.project_lease,
                project_processes=control.project_processes,
            )
        self._pause_blocks_admission = True
        return store.reopen_execution_snapshot(expected_head_sha256=pause.paused_state_head_sha256)

    def _pending_python_retry_authorization(self, snapshot, node, lease):
        """Reopen the exact pending response and inputs through their existing owner."""
        store = self._dependencies.runtime.state_store
        if (node.kind not in PENDING_PYTHON_RETRY_KINDS
                or node.node_id in snapshot.head.invalidated_node_ids
                or snapshot.head.node_status_by_id.get(node.node_id) in {StageStatus.COMPLETED, StageStatus.FAILED}):
            raise BatchServiceError("This task has no supported pending calculation to retry.")
        authorization = store.resolve_pending_authorization(node, snapshot.head.sha256, lease)
        if authorization is None:
            raise BatchServiceError("The pending calculation changed. Refresh the analysis.")
        return authorization

    def _retry_pending_python_worker(self, batch_id, *, node_id, expected_head_sha256,
                                     selected_authorization_sha256):
        """Resume one direct owner without replacing its original attempt."""
        store = self._dependencies.runtime.state_store
        with store.acquire_writer_lease("retry-pending-calculation") as lease:
            with self._lock:
                if self._active_wave is not None:
                    raise BatchServiceError("Wait for processing to finish before retrying this calculation.")
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            if (snapshot.head.batch_id != batch_id
                    or snapshot.head.execution_state not in {BatchExecutionState.ACTIVE, BatchExecutionState.PAUSED}):
                raise BatchServiceError("Open the current saved analysis before retrying this calculation.")
            with self._lock:
                if self._pause_blocks_admission and snapshot.head.execution_state is BatchExecutionState.ACTIVE:
                    raise BatchServiceError("Wait for the pause to finish before retrying this calculation.")
            if store.pending_training_settings_revision(snapshot.head.sha256, lease) is not None:
                raise BatchServiceError("Continue the classifier settings change before retrying another step.")
            node = next((item for item in snapshot.plan.nodes if item.node_id == node_id), None)
            if node is None:
                raise BatchServiceError("The selected task changed. Refresh the analysis.")
            authorization = self._pending_python_retry_authorization(snapshot, node, lease)
            if authorization.authorization_sha256 != selected_authorization_sha256:
                raise BatchServiceError("The pending calculation changed. Refresh its Retry selection.")
            provider = store._project_mutation_provider
            if provider is None or provider._unresolved_active_records_for_project(
                    project_uri=Path(snapshot.head.project_path).as_uri(),
                    project_identity_sha256=snapshot.head.project_identity_sha256):
                raise BatchServiceError("Finish recovering the recorded QuPath task before retrying this calculation.")
            if not self._dependencies.runtime.wait_for_project_stage_quiescence():
                raise BatchServiceError("Wait for the current QuPath task to exit before retrying this calculation.")
            required = frozenset({node.kind})
            prerequisites = self._collect_prerequisites(snapshot.manifest, snapshot.plan,
                required_node_kinds=required)
            self._admit_prerequisites(prerequisites, snapshot.plan.prerequisites,
                required_node_kinds=required)
            registry = build_batch_stage_registry(plan=snapshot.plan,
                resolved_samples=snapshot.manifest.enabled_samples, dependencies=self._dependencies)
            scheduler = self._new_scheduler(snapshot, lease, registry)
            if snapshot.head.execution_state is BatchExecutionState.PAUSED:
                snapshot = self._consume_retained_pause(snapshot, lease, scheduler)
            return self._drive_fixed_point(snapshot, lease, [], initial_scheduler=scheduler,
                initial_registry=registry, prerequisites=prerequisites,
                allowed_node_ids=frozenset({node_id}))

    def _retry_worker(
        self,
        batch_id: str,
        *,
        node_id: str,
        expected_head_sha256: str,
        selected_failed_terminal_record_sha256: str,
    ) -> BatchRunReport:
        store = self._dependencies.runtime.state_store
        with store.acquire_writer_lease("retry") as lease:
            with self._lock:
                if self._active_wave is not None:
                    raise BatchServiceError("retry requires an idle analysis")
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
            head = snapshot.head
            if store.pending_training_settings_revision(head.sha256, lease) is not None:
                raise BatchServiceError(
                    "Continue the classifier settings change before retrying another step."
                )
            if (
                head.batch_id != batch_id
                or head.sha256 != expected_head_sha256
                or head.execution_state not in {BatchExecutionState.ACTIVE, BatchExecutionState.PAUSED}
                or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
                or node_id in head.invalidated_node_ids
                or node_id in head.pending_authorization_sha256_by_node
            ):
                raise BatchServiceError("retry selection is not the current failed node")
            with self._lock:
                if self._pause_blocks_admission and head.execution_state is BatchExecutionState.ACTIVE:
                    raise BatchServiceError("retry requires a completed pause transition")
            attempts = head.attempt_ids_by_node.get(node_id, ())
            if not attempts or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != selected_failed_terminal_record_sha256:
                raise BatchServiceError("retry selection is not the latest failed terminal")
            nodes = tuple(item for item in snapshot.plan.nodes if item.node_id == node_id)
            if len(nodes) != 1:
                raise BatchServiceError("retry target is absent from the current plan")
            node = nodes[0]
            inspection_retry = node.kind == "inspection"
            reference_calculation_retry = False
            if node.kind == "reference_unmixing":
                from .batch_reference_recovery import reopen_reference_input_failure

                _sample_id, recovery = reopen_reference_input_failure(
                    store, snapshot, node_id, selected_failed_terminal_record_sha256)
                reference_calculation_retry = recovery == "calculation"
            if inspection_retry or node.kind == "reference_unmixing":
                required_kinds = frozenset({node.kind})
                prerequisites = self._collect_prerequisites(
                    snapshot.manifest, snapshot.plan, required_node_kinds=required_kinds)
                if inspection_retry or reference_calculation_retry:
                    self._admit_prerequisites(prerequisites, snapshot.plan.prerequisites,
                        required_node_kinds=required_kinds)
            else:
                prerequisites = self._admit_remaining_prerequisites(snapshot)
            registry = build_batch_stage_registry(
                plan=snapshot.plan,
                resolved_samples=snapshot.manifest.enabled_samples,
                dependencies=self._dependencies,
            )
            spec = registry.stage_registry.resolve(node.kind, node.version)
            if spec.spec_sha256 != node.adapter_spec_sha256:
                raise BatchServiceError("retry target adapter differs from current plan")
            scheduler = self._new_scheduler(snapshot, lease, registry)
            if head.execution_state is BatchExecutionState.PAUSED:
                # Reopen the selected failure before consuming the displayed
                # pause. The normal owner rechecks it at current issuance.
                terminal = (store.load_attempt(selected_failed_terminal_record_sha256)
                    if node.kind == "clustering_label_import" else None)
                if inspection_retry:
                    from .batch_inspection_recovery import reopen_inspection_failure
                    reopen_inspection_failure(store, snapshot, node_id, selected_failed_terminal_record_sha256)
                elif node.kind == "reference_unmixing":
                    pass  # Reopened above, under this same writer lease.
                elif node.kind in TRAINING_PARENT_NODE_KINDS:
                    reopen_native_training_failure(dependencies=self._dependencies.training,
                        workspace_lease=lease, node_id=node_id,
                        terminal_record_sha256=selected_failed_terminal_record_sha256)
                elif terminal is not None and is_project_request_failure(
                    node.kind, terminal.stage_result,
                ):
                    reopen_project_request_failure(
                        dependencies=self._dependencies.training, workspace_lease=lease,
                        head=head, node_id=node_id,
                        terminal=terminal,
                        adapter_spec=spec,
                    )
                else:
                    reopen_project_stage_failure(dependencies=self._dependencies.training,
                        workspace_lease=lease, node_id=node_id,
                        terminal_record_sha256=selected_failed_terminal_record_sha256, adapter_spec=spec)
                snapshot = self._consume_retained_pause(snapshot, lease, scheduler)
                head = snapshot.head
            inputs = scheduler.resolve_current_node_inputs(
                snapshot.plan, _available_artifacts(snapshot), node_id
            )
            prepared = self._prepare_dispatch(lease, node, spec, inputs)
            request = BatchAttemptAuthorizationRequest(
                node=node,
                input_artifacts=inputs,
                adapter_spec=spec,
                dispatch_authority=prepared.authority,
                retry_ordinal=len(head.attempt_ids_by_node.get(node_id, ())),
            )
            if reference_calculation_retry:
                _sample_id, recovery = reopen_reference_input_failure(
                    store, snapshot, node_id, selected_failed_terminal_record_sha256)
                if recovery != "calculation":
                    raise BatchServiceError("The accepted unmixing calculation changed. Refresh its Retry selection.")
                # The existing executor resumes this request without submitting
                # another final-basis decision or altering its saved settings.
                store.authorize_attempt(request, snapshot.head.sha256, lease)
            elif node.kind == "reference_unmixing":
                from .batch_reference_recovery import commit_reference_form_recovery

                committed = commit_reference_form_recovery(
                    store, snapshot, request, selected_failed_terminal_record_sha256, lease
                )
                self._publish_ordinary_interaction_requests(
                    store.reopen_execution_snapshot(), committed, lease
                )
                current = self._pause_recovered_reference_form(
                    store.reopen_execution_snapshot(), lease, registry
                )
                return self._report(
                    current,
                    "needs_interaction",
                    tuple(item.node_id for item in committed),
                    prerequisites,
                )
            elif inspection_retry:
                from .batch_inspection_recovery import authorize_inspection_retry

                authorize_inspection_retry(
                    store, snapshot, request, selected_failed_terminal_record_sha256, lease)
            else:
                authorize_failed_stage_retry(
                    dependencies=self._dependencies.training,
                    workspace_lease=lease,
                    request=request,
                    expected_head_sha256=head.sha256,
                    selected_failed_terminal_record_sha256=selected_failed_terminal_record_sha256,
                )
            active = store.reopen_execution_snapshot()
            return self._drive_fixed_point(
                active,
                lease,
                [],
                initial_scheduler=scheduler,
                initial_registry=registry,
                prerequisites=prerequisites,
                allowed_node_ids=(frozenset({node_id})
                    if inspection_retry or reference_calculation_retry else None),
            )

    def _drive_fixed_point(
        self,
        snapshot,
        lease: BatchWorkspaceLease,
        committed: list[str],
        *,
        initial_scheduler: BatchScheduler | None,
        prerequisites: tuple[BatchPrerequisiteItem, ...],
        initial_registry: Task15RuntimeStageRegistry | None = None,
        allowed_node_ids: frozenset[str] | None = None,
        reconcile_before_dispatch: bool = False,
    ) -> BatchRunReport:
        """Advance all currently executable work until the analysis reaches a stable boundary.

        The service synchronizes accepted stage inputs, publishes any required plan
        revision, and admits ready nodes through the scheduler's resource limits. It retires
        completed work and exposes newly required reviews. The loop stops when no further
        accepted work can advance, when pause is requested, or when a failure requires the
        caller's attention. An exact node selection stops after its ordinary
        wave and plan update, retaining new dependants for a later Continue."""

        store = self._dependencies.runtime.state_store
        scheduler = initial_scheduler
        registry = initial_registry
        from .batch_project import recover_completed_project_upsert
        recover_completed_project_upsert(state_store=store, workspace_lease=lease)
        if reconcile_before_dispatch:
            # Continue reopens accepted work before issuing its next operation.
            # A retained native operation must finish before its node can retire.
            with self._lock:
                if self._active_wave is not None:
                    raise BatchServiceError("Wait for processing to finish before continuing saved work.")
            if registry is None:
                registry = build_batch_stage_registry(plan=snapshot.plan,
                    resolved_samples=snapshot.manifest.enabled_samples, dependencies=self._dependencies)
            successor = expand_batch_plan(snapshot.plan, snapshot.manifest,
                registry.stage_registry, snapshot.accepted_artifacts,
                accepted_artifact_authorities=snapshot.committed_artifact_authorities,
                batch_state_store=store,
                training_plan_set_store=self._dependencies.training.training_plan_set_store)
            if successor.plan_sha256 != snapshot.plan.plan_sha256:
                retired_pending = set(snapshot.head.pending_authorization_sha256_by_node) - {
                    node.node_id for node in successor.nodes}
                if retired_pending:
                    provider = store._project_mutation_provider
                    if provider is None or provider._unresolved_active_records_for_project(
                            project_uri=Path(snapshot.head.project_path).as_uri(),
                            project_identity_sha256=snapshot.head.project_identity_sha256):
                        raise BatchServiceError("Finish recovering the recorded QuPath task before continuing the revised work.")
                store.revise_plan(successor, "authenticated saved-work expansion", snapshot.head.sha256, lease)
                snapshot = self._synchronize_accepted_domain_material(store.reopen_execution_snapshot(), lease)
                scheduler, registry = None, None
        while True:
            outcome = self._closed_outcome(snapshot)
            if outcome in {"paused", "completed"}:
                return self._report(snapshot, outcome, tuple(committed), prerequisites)
            revised = self._apply_saved_classifier_selection(snapshot, lease)
            revised = self._apply_secondary_definition_selection(revised, lease)
            revised = self._apply_saved_supervised_cutoffs(revised, lease)
            if revised is not snapshot:
                snapshot, scheduler, registry = revised, None, None
                continue
            revised = self._apply_pending_training_settings_revision(snapshot, lease)
            if revised is not snapshot:
                snapshot = revised
                scheduler = None
                registry = None
                continue
            if registry is None:
                registry = build_batch_stage_registry(
                    plan=snapshot.plan,
                    resolved_samples=snapshot.manifest.enabled_samples,
                    dependencies=self._dependencies,
                )
            if scheduler is None:
                scheduler = self._new_scheduler(snapshot, lease, registry)
            with self._lock:
                if self._pause_blocks_admission:
                    return self._report(snapshot, "paused", tuple(committed), prerequisites)
                wave = _ActiveWave(snapshot.head.batch_id, lease, scheduler)
                self._active_wave = wave
            try:
                results = scheduler.run_ready(
                    snapshot.plan,
                    _available_artifacts(snapshot),
                    self._dispatcher(registry, lease, activity=self._stage_activity),
                    allowed_node_ids=allowed_node_ids,
                )
                committed.extend(item.node_id for item in results)
                reopened = store.reopen_execution_snapshot()
                if self._publish_ordinary_interaction_requests(reopened, results, lease):
                    reopened = store.reopen_execution_snapshot()
                if self._publish_aggregate_cluster_review_requests(reopened, results, lease):
                    reopened = store.reopen_execution_snapshot()
                selected = self._apply_saved_classifier_selection(reopened, lease)
                selected = self._apply_secondary_definition_selection(selected, lease)
                selected = self._apply_saved_supervised_cutoffs(selected, lease)
                selection_changed = selected is not reopened
                if selection_changed:
                    reopened = selected
                    registry = build_batch_stage_registry(plan=reopened.plan,
                        resolved_samples=reopened.manifest.enabled_samples, dependencies=self._dependencies)
                outcome = self._closed_outcome(reopened)
                if outcome in {"paused", "completed"}:
                    report = self._report(reopened, outcome, tuple(committed), prerequisites)
                    return self._retire_wave(wave, committed, report, prerequisites)
                successor = expand_batch_plan(
                    reopened.plan,
                    reopened.manifest,
                    registry.stage_registry,
                    reopened.accepted_artifacts,
                    accepted_artifact_authorities=(reopened.committed_artifact_authorities),
                    batch_state_store=store,
                    training_plan_set_store=(self._dependencies.training.training_plan_set_store),
                )
                if successor.plan_sha256 == reopened.plan.plan_sha256:
                    if selection_changed and allowed_node_ids is None:
                        snapshot, scheduler, registry = reopened, None, None
                        paused_report = self._retire_wave(wave, committed, None, prerequisites)
                        if paused_report is not None:
                            return paused_report
                        continue
                    report = self._report(
                        reopened, outcome or "incomplete", tuple(committed), prerequisites
                    )
                    return self._retire_wave(wave, committed, report, prerequisites)
                store.revise_plan(
                    successor,
                    "authenticated fixed-point expansion",
                    reopened.head.sha256,
                    lease,
                )
                revised = store.reopen_execution_snapshot()
                snapshot = self._synchronize_accepted_domain_material(revised, lease)
                if allowed_node_ids is not None:
                    report = self._report(snapshot, self._closed_outcome(snapshot) or "incomplete",
                        tuple(committed), prerequisites)
                    return self._retire_wave(wave, committed, report, prerequisites)
                scheduler = None
                registry = None
                paused_report = self._retire_wave(wave, committed, None, prerequisites)
                if paused_report is not None:
                    return paused_report
            except BaseException as exc:
                with self._lock:
                    wave.post_wave_error = exc
                    wave.post_wave_done.set()
                    if self._active_wave is wave:
                        self._active_wave = None
                raise

    def _apply_saved_supervised_cutoffs(self, snapshot, lease: BatchWorkspaceLease):
        from .supervised_cutoff_application import apply_saved_cutoffs
        return apply_saved_cutoffs(self, snapshot, lease)

    def _apply_saved_classifier_selection(self, snapshot, lease: BatchWorkspaceLease):
        """Admit the saved model chosen by the existing accepted classifier setup."""
        from .batch_training.runtime import decode_current_committed_training_plan_set
        from .batch_training.accepted_model_reuse import manifest_for_classifier_setup, import_batch_classifier_record_sets
        store = self._dependencies.runtime.state_store
        for authority in snapshot.committed_artifact_authorities:
            if (authority.artifact_ref.artifact_kind != "training_plan_set"
                    or authority.producer_node_kind != "primary_classifier_training_plan_review"):
                continue
            projection = decode_current_committed_training_plan_set(authority,
                store=self._dependencies.training.training_plan_set_store, state_store=store)
            manifest = manifest_for_classifier_setup(snapshot.manifest, projection)
            if manifest.normalized_manifest_sha256 == snapshot.manifest.normalized_manifest_sha256:
                continue
            registry = build_batch_stage_registry(plan=snapshot.plan,
                resolved_samples=manifest.enabled_samples, dependencies=self._dependencies)
            declarations = {item.declaration_sha256 for item in manifest.manifest.reusable_artifacts}
            retained = tuple(ref for ref in snapshot.accepted_artifacts
                if ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL
                or ref.external_declaration_sha256 in declarations)
            imported = import_batch_classifier_record_sets(manifest, retained_artifacts=retained)
            refs = {ref.artifact_ref_sha256: ref for ref in (*retained, *imported)}
            successor = expand_batch_plan(snapshot.plan, manifest, registry.stage_registry,
                tuple(refs.values()), accepted_artifact_authorities=snapshot.committed_artifact_authorities,
                batch_state_store=store, training_plan_set_store=self._dependencies.training.training_plan_set_store)
            preserved = frozenset(store.preview_reusable_completed_nodes(
                successor, expected_head_sha256=snapshot.head.sha256))
            required = frozenset(
                {item.kind for item in successor.nodes if item.node_id not in preserved}
                | {item.node_kind for item in successor.deferred_nodes})
            successor = bind_batch_prerequisites(successor,
                self._collect_prerequisites(manifest, successor, required_node_kinds=required)
                if required else ())
            self._admit_prerequisites(successor.prerequisites)
            store.revise_plan(successor, "accepted saved classifier selection", snapshot.head.sha256, lease,
                              manifest=manifest, classifier_setup_projection=projection)
            return store.reopen_execution_snapshot()
        return snapshot

    def _apply_secondary_definition_selection(self, snapshot, lease: BatchWorkspaceLease):
        """Publish this accepted choice's profile declarations through a successor."""
        from .batch_secondary_definitions import (
            ARTIFACT_KIND,
            manifest_for_secondary_definitions, secondary_definition_selections,
        )
        store = self._dependencies.runtime.state_store
        selections = secondary_definition_selections(snapshot, state_store=store, participant_ids={
            binding.sample_id for item in snapshot.committed_artifact_authorities
            if item.artifact_ref.artifact_kind == ARTIFACT_KIND
            for binding in item.artifact_ref.configuration_bindings
        })
        choices = tuple({item.artifact_ref.artifact_ref_sha256: item
                         for item in selections.values()}.values())
        available_profiles = {(binding.sample_id, ref.sha256)
                              for ref in snapshot.accepted_artifacts if ref.artifact_kind == "semantic_profile"
                              for binding in ref.configuration_bindings}
        for projection in choices:
            manifest = manifest_for_secondary_definitions(snapshot.manifest, projection)
            changed_manifest = manifest.normalized_manifest_sha256 != snapshot.manifest.normalized_manifest_sha256
            expected_profiles = {(sample_id, structure.semantic_profile.content_sha256)
                                 for sample_id in projection.context["participant_ids"]
                                 for structure in projection.structures}
            if not changed_manifest and expected_profiles <= available_profiles:
                continue
            declared = manifest.manifest.reusable_artifacts
            ready_choices = tuple(item for item in choices if all(any(
                declaration.artifact_kind == "semantic_profile"
                and declaration.validation_domain_id in (None, item.context["validation_domain_id"])
                and declaration.locator.resolve(strict=False) == structure.semantic_profile.artifact_path.resolve(strict=False)
                and declaration.sha256 == structure.semantic_profile.content_sha256
                for declaration in declared) for structure in item.structures))
            ready_selections = {key: selections[key] for item in ready_choices
                                for key in item.context["participant_ids"]}
            registry = build_batch_stage_registry(plan=snapshot.plan,
                resolved_samples=manifest.enabled_samples, dependencies=self._dependencies)
            imported = import_batch_semantic_profiles(
                manifest, registry.stage_registry, secondary_selections=ready_selections)
            declaration_digests = {item.declaration_sha256 for item in declared}
            refs = {ref.artifact_ref_sha256: ref for ref in (*snapshot.accepted_artifacts, *imported)
                    if ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL
                    or ref.external_declaration_sha256 in declaration_digests}
            successor = expand_batch_plan(snapshot.plan, manifest, registry.stage_registry,
                tuple(refs.values()), accepted_artifact_authorities=snapshot.committed_artifact_authorities,
                batch_state_store=store, training_plan_set_store=self._dependencies.training.training_plan_set_store)
            preserved = frozenset(store.preview_reusable_completed_nodes(
                successor, expected_head_sha256=snapshot.head.sha256))
            required = frozenset({item.kind for item in successor.nodes if item.node_id not in preserved}
                                 | {item.node_kind for item in successor.deferred_nodes})
            successor = bind_batch_prerequisites(successor,
                self._collect_prerequisites(manifest, successor, required_node_kinds=required) if required else ())
            self._admit_prerequisites(successor.prerequisites, required_node_kinds=required)
            store.revise_plan(successor, "accepted secondary criterion profiles", snapshot.head.sha256, lease,
                              manifest=manifest if changed_manifest else None,
                              secondary_definition_projection=projection if changed_manifest else None)
            return store.reopen_execution_snapshot()
        return snapshot

    def _apply_pending_training_settings_revision(self, snapshot, lease: BatchWorkspaceLease):
        store = self._dependencies.runtime.state_store
        revision = store.pending_training_settings_revision(snapshot.head.sha256, lease)
        if revision is None:
            return snapshot
        registry = build_batch_stage_registry(
            plan=snapshot.plan,
            resolved_samples=snapshot.manifest.enabled_samples,
            dependencies=self._dependencies,
        )
        successor = expand_batch_plan(
            snapshot.plan,
            snapshot.manifest,
            registry.stage_registry,
            snapshot.accepted_artifacts,
            accepted_artifact_authorities=snapshot.committed_artifact_authorities,
            batch_state_store=store,
            training_plan_set_store=self._dependencies.training.training_plan_set_store,
            training_settings_revision=revision,
        )
        if successor.plan_sha256 == snapshot.plan.plan_sha256:
            raise BatchServiceError(
                "The classifier settings change did not create a new training plan."
            )
        store.revise_plan(
            successor, "accepted classifier settings revision", snapshot.head.sha256, lease
        )
        return store.reopen_execution_snapshot()

    def _retire_wave(
        self,
        wave: _ActiveWave,
        committed: list[str],
        report: BatchRunReport | None,
        prerequisites: tuple[BatchPrerequisiteItem, ...],
    ) -> BatchRunReport | None:
        with self._lock:
            pause_requested = self._pause_blocks_admission
            automatic_pause = (
                report is not None and report.outcome in {"needs_interaction", "incomplete"} and not pause_requested
            )
            if automatic_pause:
                self._pause_blocks_admission = True
            else:
                wave.post_wave_done.set()
                if self._active_wave is wave:
                    self._active_wave = None
        if automatic_pause:
            try:
                receipt = self._publish_wave_pause(wave, wait_for_post_wave=False)
                snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot(
                    expected_head_sha256=receipt.paused_state_head_sha256
                )
                outcome = "paused" if report.outcome == "incomplete" else "needs_interaction"
                return self._report(snapshot, outcome, tuple(committed), prerequisites)
            except BaseException:
                with self._lock:
                    self._pause_blocks_admission = False
                raise
            finally:
                with self._lock:
                    wave.post_wave_done.set()
                    if self._active_wave is wave:
                        self._active_wave = None
                wave.pause_done.set()
        if not pause_requested:
            return report
        wave.pause_done.wait()
        snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot()
        if snapshot.head.execution_state is not BatchExecutionState.PAUSED:
            raise BatchServiceError("requested graceful pause did not publish")
        return self._report(snapshot, "paused", tuple(committed), prerequisites)

    def _synchronize_accepted_domain_material(
        self,
        snapshot,
        lease: BatchWorkspaceLease,
    ):
        """Install only decoder-authenticated accepted-domain material."""

        revision_by_id: dict[str, str] = {}
        domain_set_by_sample: dict[str, str] = {}
        for authority in snapshot.committed_artifact_authorities:
            if authority.artifact_ref.artifact_kind != "accepted_analysis_domain":
                continue
            try:
                projection = decode_committed_accepted_analysis_domain(authority)
            except BatchArtifactError as exc:
                raise BatchServiceError("accepted-domain material authority is invalid") from exc
            if projection.sample_id in domain_set_by_sample:
                raise BatchServiceError("accepted-domain material has duplicate sample authority")
            domain_set_by_sample[projection.sample_id] = projection.domain_set.domain_set_sha256
            for domain in projection.domain_authorities:
                if domain.analysis_domain_id in revision_by_id:
                    raise BatchServiceError(
                        "accepted-domain material has duplicate domain authority"
                    )
                revision_by_id[domain.analysis_domain_id] = domain.analysis_domain_revision_sha256

        planned_revision_by_id: dict[str, str] = {}
        planned_domain_set_by_sample: dict[str, str] = {}
        for node in snapshot.plan.nodes:
            for binding in node.analysis_domain_bindings:
                planned_revision = planned_revision_by_id.setdefault(
                    binding.analysis_domain_id,
                    binding.analysis_domain_revision_sha256,
                )
                planned_domain_set = planned_domain_set_by_sample.setdefault(
                    binding.sample_id,
                    binding.domain_set_sha256,
                )
                if (
                    planned_revision != binding.analysis_domain_revision_sha256
                    or planned_domain_set != binding.domain_set_sha256
                ):
                    raise BatchServiceError(
                        "active plan contains conflicting accepted-domain material"
                    )
        if (
            any(revision_by_id.get(key) != value for key, value in planned_revision_by_id.items())
            or any(
                domain_set_by_sample.get(key) != value
                for key, value in planned_domain_set_by_sample.items()
            )
        ):
            raise BatchServiceError("accepted-domain projection differs from active plan material")
        # Shared training may wait for other samples after an area is accepted.
        # Keep its saved owner, but project material only for active-plan bindings.
        revision_by_id = {key: revision_by_id[key] for key in planned_revision_by_id}
        domain_set_by_sample = {
            key: domain_set_by_sample[key] for key in planned_domain_set_by_sample
        }

        head = snapshot.head
        if (
            dict(head.accepted_domain_revision_sha256_by_id) == revision_by_id
            and dict(head.domain_set_sha256_by_sample) == domain_set_by_sample
        ):
            return snapshot
        update = BatchMaterialAuthorityUpdate(
            source_sha256_by_sample=dict(head.source_sha256_by_sample),
            resolved_config_sha256_by_sample=dict(head.resolved_config_sha256_by_sample),
            project_identity_sha256=head.project_identity_sha256,
            project_authority_head_sha256=head.project_authority_head_sha256,
            project_materialization_state=head.project_materialization_state,
            project_qpproj_snapshot_sha256=head.project_qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=(head.project_mutation_attestation_sha256),
            project_catalogue_sha256=head.project_catalogue_sha256,
            sample_project_entry_id_by_sample=dict(head.sample_project_entry_id_by_sample),
            sample_project_entry_projection_sha256_by_sample=dict(
                head.sample_project_entry_projection_sha256_by_sample
            ),
            accepted_domain_revision_sha256_by_id=revision_by_id,
            domain_set_sha256_by_sample=domain_set_by_sample,
            checkpoint_descriptor_sha256_by_node=dict(head.checkpoint_descriptor_sha256_by_node),
            external_artifact_ref_sha256_by_key={
                item.artifact_ref_sha256: item.artifact_ref_sha256
                for item in snapshot.plan.external_inputs
            },
        )
        updated = self._dependencies.runtime.state_store.update_material_authority(
            update,
            head.sha256,
            lease,
        )
        return self._dependencies.runtime.state_store.reopen_execution_snapshot(
            expected_head_sha256=updated.sha256
        )

    def _publish_ordinary_interaction_requests(
        self,
        snapshot,
        results,
        lease: BatchWorkspaceLease,
    ) -> bool:
        """Queue ordinary terminal interactions, including interrupted publications."""

        store = self._dependencies.runtime.state_store
        nodes_by_id = {node.node_id: node for node in snapshot.plan.nodes}
        results = list(results)
        returned_ids = {result.node_id for result in results}
        head = snapshot.head
        # A wave can stop after a sibling's terminal was committed but before
        # its requests were published. Resume must expose that retained review
        # without rerunning its calculation or manufacturing a new decision.
        for node in snapshot.plan.nodes:
            if (node.node_id in returned_ids
                    or node.kind == "cluster_label_review"
                    or node.node_id in head.invalidated_node_ids
                    or node.node_id in head.pending_authorization_sha256_by_node
                    or head.node_status_by_id.get(node.node_id) is not StageStatus.NEEDS_INTERACTION):
                continue
            attempts = head.attempt_ids_by_node.get(node.node_id, ())
            digest = head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) if attempts else None
            if digest is None:
                raise BatchServiceError("retained interaction lacks its latest committed terminal")
            terminal = store.load_attempt(digest)
            if (terminal.node_id != node.node_id or terminal.attempt_id != attempts[-1]
                    or terminal.terminal_record_sha256 != digest
                    or terminal.stage_result.status is not StageStatus.NEEDS_INTERACTION):
                raise BatchServiceError("retained interaction terminal authority is not current")
            results.append(ScheduledNodeResult(node.node_id, terminal.attempt_id,
                terminal.authorization_sha256, terminal.stage_result, digest, head.sha256))
        targets: list[BatchInteractionTarget] = []
        for scheduled in results:
            if scheduled.stage_result.status is not StageStatus.NEEDS_INTERACTION:
                continue
            node = nodes_by_id.get(scheduled.node_id)
            if node is None:
                raise BatchServiceError("terminal interaction node is not current")
            if node.kind == "cluster_label_review":
                # Aggregate cluster review retains its graph target and is
                # deliberately reopened through its aggregate owner.
                continue
            authorization = store.load_authorization(scheduled.attempt_authorization_sha256)
            if (
                authorization.authorization_sha256 != scheduled.attempt_authorization_sha256
                or authorization.attempt_id != scheduled.attempt_id
                or authorization.node_id != node.node_id
                or snapshot.head.node_status_by_id.get(node.node_id)
                is not StageStatus.NEEDS_INTERACTION
                or snapshot.head.terminal_attempt_sha256_by_attempt_id.get(scheduled.attempt_id)
                != scheduled.terminal_attempt_record_sha256
                or node.node_id in snapshot.head.pending_authorization_sha256_by_node
            ):
                raise BatchServiceError("terminal interaction authority is not current")
            samples = tuple(
                sample
                for sample in snapshot.manifest.enabled_samples
                if sample.sample_id in node.participant_ids
                and sample.slide_id == scheduled.stage_result.slide_id
            )
            if len(samples) != 1:
                raise BatchServiceError("ordinary interaction lacks one retained sample target")
            sample = samples[0]
            if sample.validation_domain_id != node.validation_domain_id:
                raise BatchServiceError("ordinary interaction validation-domain authority changed")
            context = scheduled.stage_result.resume_context
            if context is None:
                raise BatchServiceError("terminal interaction lacks resume context")
            interaction_context_sha256 = _canonical_sha256(context)
            request_id = batch_interaction_request_id(
                node_id=node.node_id,
                attempt_id=scheduled.attempt_id,
                attempt_authorization_sha256=(scheduled.attempt_authorization_sha256),
                interaction_context_sha256=interaction_context_sha256,
            )
            if request_id in head.pending_interaction_ids:
                continue
            targets.append(
                BatchInteractionTarget(
                    target_id=request_id,
                    node_id=node.node_id,
                    attempt_id=scheduled.attempt_id,
                    attempt_authorization_sha256=(scheduled.attempt_authorization_sha256),
                    needs_interaction_terminal_record_sha256=(
                        scheduled.terminal_attempt_record_sha256
                    ),
                    interaction_request_id=request_id,
                    interaction_context_sha256=interaction_context_sha256,
                    project_entry_id=(
                        snapshot.head.sample_project_entry_id_by_sample.get(sample.sample_id)
                        if node.project_entry_ids else None
                    ),
                    sample_id=sample.sample_id,
                    slide_id=sample.slide_id,
                    validation_domain_id=sample.validation_domain_id,
                    analysis_domain_bindings=node.analysis_domain_bindings,
                    compatibility_signature_sha256=(
                        authorization.dispatch_binding.compatibility_signature_sha256
                    ),
                    stage_result=scheduled.stage_result,
                )
            )
        if targets:
            publish_interaction_requests(
                targets=tuple(targets),
                expected_state_head_sha256=snapshot.head.sha256,
                state_store=store,
                workspace_lease=lease,
            )
            return True
        return False

    def _publish_aggregate_cluster_review_requests(
        self, snapshot, results, lease: BatchWorkspaceLease
    ) -> bool:
        """Queue state-derived pooled review targets outside ordinary UI scope."""

        nodes_by_id = {node.node_id: node for node in snapshot.plan.nodes}
        node_ids = tuple(
            scheduled.node_id
            for scheduled in results
            if scheduled.stage_result.status is StageStatus.NEEDS_INTERACTION
            and nodes_by_id.get(scheduled.node_id) is not None
            and nodes_by_id[scheduled.node_id].kind == "cluster_label_review"
        )
        if not node_ids:
            return False
        self._dependencies.runtime.state_store.publish_cluster_label_review_requests(
            node_ids,
            snapshot.head.sha256,
            lease,
        )
        return True

    def _new_scheduler(self, snapshot, lease, registry) -> BatchScheduler:
        def validate_pending_failure(context):
            if context.adapter_spec.node_kind == "inspection":
                from .batch_inspection_recovery import revalidate_pending_inspection_retry

                revalidate_pending_inspection_retry(
                    self._dependencies.runtime.state_store, lease, context)
            elif context.adapter_spec.node_kind == "reference_unmixing":
                from .batch_reference_recovery import revalidate_pending_reference_retry

                revalidate_pending_reference_retry(
                    self._dependencies.runtime.state_store, lease, context)
            else:
                revalidate_failed_stage_retry(
                    dependencies=self._dependencies.training,
                    workspace_lease=lease,
                    context=context,
                )

        return BatchScheduler(
            snapshot.manifest.manifest.execution_config,
            self._dependencies.runtime.state_store,
            lease,
            registry.stage_registry,
            lambda node, spec, inputs: self._prepare_dispatch(lease, node, spec, inputs),
            self._estimate_current_demand,
            pending_failure_validator=validate_pending_failure,
        )

    def _prepare_dispatch(self, lease, node, spec, inputs):
        store = self._dependencies.runtime.state_store
        head = store.load_head()
        compatibility_values = tuple(
            sorted({item.compatibility_signature_sha256 for item in inputs})
        )
        compatibility = (
            compatibility_values[0]
            if len(compatibility_values) == 1
            else _canonical_sha256(
                {
                    "compatibility_signature_id": spec.compatibility_signature_id,
                    "input_compatibility_signature_sha256s": list(compatibility_values),
                    "participant_ids": list(node.participant_ids),
                    "project_entry_ids": list(node.project_entry_ids),
                    "validation_domain_id": node.validation_domain_id,
                    "analysis_domain_bindings": [
                        item.to_mapping() for item in node.analysis_domain_bindings
                    ],
                    "configuration_bindings": [
                        item.to_mapping() for item in node.configuration_bindings
                    ],
                }
            )
        )
        is_current_image = spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        is_direct_registration = (
            getattr(node, "kind", None) == "registration"
            and spec.dispatch_mode is DispatchMode.SINGLE
        )
        is_project_entry_bound = is_current_image or is_direct_registration
        is_project = is_project_entry_bound or getattr(node, "kind", None) in {"project_upsert", "paired_image_preparation"}
        current_image = None
        script_sha256 = None
        entry_projection = None
        if is_project_entry_bound:
            if len(node.participant_ids) != 1 or len(node.project_entry_ids) != 1:
                raise BatchServiceError(
                    "project-entry dispatch requires one exact participant and entry"
                )
            sample_id = node.participant_ids[0]
            entry_id = node.project_entry_ids[0]
            # The catalogue fixes image entries. We dispatch against the current
            # verified project save, which advances when native settings are saved.
            catalogue = store.project_catalogue_store(lease).load_current()
            entries = (
                ()
                if catalogue is None
                else tuple(item for item in catalogue.entries if item.project_entry_id == entry_id)
            )
            if (
                catalogue is None
                or len(entries) != 1
                or catalogue.batch_id != head.batch_id
                or catalogue.catalogue_sha256 != head.project_catalogue_sha256
                or catalogue.project_identity_sha256 != head.project_identity_sha256
                or entries[0].sample_id != sample_id
                or head.sample_project_entry_id_by_sample.get(sample_id) != entry_id
                or head.sample_project_entry_projection_sha256_by_sample.get(sample_id)
                != entries[0].entry_projection_sha256
            ):
                raise BatchServiceError(
                    "project-entry dispatch catalogue authority is absent or stale"
                )
            entry_projection = entries[0].entry_projection_sha256
            if is_current_image:
                codec = self._dependencies.project_codecs_by_spec_sha256.get(spec.spec_sha256)
                if codec is None:
                    raise BatchServiceError("current-image dispatch lacks its sealed project codec")
                try:
                    asset = QuPathScriptAsset(codec.binding.target_script_asset_id)
                except ValueError as exc:
                    raise BatchServiceError(
                        "current-image dispatch codec names an unregistered asset"
                    ) from exc
                current_image = entries[0].image_sha256
                script_sha256 = registered_script_asset_sha256(asset)
        authority = BatchPreparedDispatchAuthority(
            compatibility_signature_sha256=compatibility,
            current_image_identity_sha256=current_image,
            script_sha256=script_sha256,
            project_identity_sha256=head.project_identity_sha256 if is_project else None,
            project_authority_head_sha256=(
                head.project_authority_head_sha256 if is_project else None
            ),
            project_qpproj_snapshot_sha256=(
                head.project_qpproj_snapshot_sha256 if is_project else None
            ),
            project_entry_projection_sha256=entry_projection,
        )
        return BatchBoundDispatchPreparation(
            node.node_id,
            spec.spec_sha256,
            artifact_ref_set_sha256(inputs),
            authority,
        )

    def _automatic_nimbus_requested(self, node, head):
        """Read the current marker choice without opening model or image files."""
        store = self._dependencies.runtime.state_store
        pending = head.pending_authorization_sha256_by_node.get(node.node_id)
        if pending is None:
            return False
        response = store.load_validated_interaction_response(store.load_authorization(pending))
        if response is None or response.get("action") not in {"define_marker_composite_rules", "review_marker_thresholds"}:
            return False
        settings = response["response"].get("marker_settings")
        if settings is None:
            return False
        request = store.load_interaction_request(response["interaction_request_id"])
        from .batch_phenotyping.marker_settings import marker_settings_locked
        marker = (request.resume_context["marker_workflow"]["current_marker_id"]
                  if marker_settings_locked(request.resume_context)
                  else settings["marker_rules"][0]["marker_id"])
        marker = response['response'].get('revisit_marker_id', marker)
        rule = next(item for item in settings["marker_rules"]
                    if item["marker_id"] == marker)
        return rule["method"] == "nimbus" and not rule.get("nimbus_score_path")

    def _estimate_current_demand(self, node, spec, inputs):
        """Reserve model resources only for the scientific work being dispatched.

        Settings forms and stored-score imports use the ordinary orchestration
        allowance. Automatic Nimbus scoring includes its image reader and worker
        in one reservation. KRONOS reserves its native coordinator, preparation
        worker and selected patch activations from accepted plans/models. Final
        classifier acceptance keeps the ordinary publication allowance.
        """
        segmentation = None
        if node.kind == "segmentation":
            if node.segmentation_settings_revision is not None:
                segmentation = node.segmentation_settings_revision.settings
            else:
                snapshot = self._dependencies.runtime.state_store.reopen_execution_snapshot()
                sample = next(item for item in snapshot.manifest.enabled_samples if (item.sample_id,) == node.participant_ids)
                if sample.config.segmentation is not None:
                    segmentation = sample.config.segmentation
                else:
                    pending = snapshot.head.pending_authorization_sha256_by_node.get(node.node_id)
                    if pending is not None:
                        response = self._dependencies.runtime.state_store.load_validated_interaction_response(
                            self._dependencies.runtime.state_store.load_authorization(pending))
                        if response is not None and response.get("action") == "configure_segmentation":
                            from mif_pipeline.pipeline_core.config import SegmentationConfig
                            segmentation = SegmentationConfig.from_mapping(
                                response["response"]["response"]["segmentation"], field="segmentation")
            if segmentation is None:
                from .batch_resources import fixed_working_set_allowance

                return BatchResourceDemand(
                    node.node_id,
                    spec.spec_sha256,
                    artifact_ref_set_sha256(inputs),
                    ResourceKind(spec.resource_kind),
                    1,
                    1,
                    fixed_working_set_allowance(None, ResourceKind.SCIENTIFIC_COMPUTE),
                    cpu_threads=1,
                    gpu_sessions=0,
                )
            if segmentation.workers not in (1, 2):
                raise BatchServiceError(
                    "Segmentation supports 1 or 2 simultaneous tiles. Open segmentation "
                    "options, choose one of these values, then continue the analysis."
                )
        automatic_nimbus = False
        encoder_settings = []
        if node.kind == "marker_gate_review":
            automatic_nimbus = self._automatic_nimbus_requested(
                node, self._dependencies.runtime.state_store.load_head())
        elif node.kind in {
            "primary_classifier_training_review", "secondary_classifier_training_review",
            "supervised_phenotype_training_review", "supervised_training_evidence_snapshot",
        }:
            if node.kind in TRAINING_PARENT_NODE_KINDS:
                store = self._dependencies.runtime.state_store
                pending = store.load_head().pending_authorization_sha256_by_node.get(node.node_id)
                response = (None if pending is None else
                            store.load_validated_interaction_response(store.load_authorization(pending)))
                if response is not None and response.get("action") == "accept":
                    request = store.load_interaction_request(response["interaction_request_id"])
                    if (request.stage == node.kind
                            and (request.checkpoint_id or "").startswith("training-fit-review-")
                            and (request.resume_context or {}).get("phase") == "final_results"):
                        # Final acceptance only publishes the retained model.
                        # Accepting freeze_settings can still evaluate test data.
                        from .batch_resources import fixed_working_set_allowance

                        kind = ResourceKind(spec.resource_kind)
                        return BatchResourceDemand(
                            node.node_id, spec.spec_sha256, artifact_ref_set_sha256(inputs),
                            kind, 1, 1, fixed_working_set_allowance(None, kind),
                            cpu_threads=2, gpu_sessions=0,
                        )
            # Accepted plans already bind their configurations. Reopen their
            # compact body for resource planning without repeating current-state
            # admission or loading either model.
            selected = next(item for item in inputs if item.artifact_kind == "training_plan_set")
            identity = selected.owning_identity
            plans = self._dependencies.training.training_plan_set_store.reopen(
                identity["store_relative_locator"], sha256=identity["body_sha256"],
                byte_count=identity["body_byte_count"])
            for plan in plans.plans:
                config = plan.request.model_configuration
                if config.get("configuration_schema") == "qupath-grouped-cross-validation/v1":
                    config = config["candidates"][0]
                if config.get("configuration_schema") in {"qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2"}:
                    encoder_settings.append(config["encoder"])
                elif config.get("feature_source") is not None:
                    encoder_settings.append(config["feature_source"]["encoder"])
        elif node.kind in {
            "primary_classifier_inference", "secondary_classifier_inference",
            "supervised_phenotype_inference",
        }:
            selected = next(item for item in inputs if item.artifact_kind == "classifier_record_set")
            if selected.origin is ArtifactOrigin.MANIFEST_EXTERNAL:
                from .batch_training.accepted_model_reuse import decode_imported_classifier_record_set

                store = self._dependencies.runtime.state_store
                authority = store.authenticate_available_manifest_external_artifact(selected)
                records = decode_imported_classifier_record_set(
                    authority, state_store=store,
                ).record_set
            else:
                records = self._dependencies.training.classifier_record_set_store.reopen(
                    selected.owning_identity["classifier_record_set_sha256"])
            for record in records.classifier_records:
                source = record.feature_requirements.get("feature_source")
                if source is not None:
                    encoder_settings.append(source["encoder"])
                elif record.classifier_engine in {"qupath-kronos-rtrees-classifier", "qupath-kronos-svm-classifier"}:
                    config = record.owner_payload.classifier_definition.training_defaults
                    if config.get("configuration_schema") == "qupath-grouped-cross-validation/v1":
                        config = config["candidates"][0]
                    encoder_settings.append(config["encoder"])
        encoder_memory_bytes = None
        encoder_cpu_threads = None
        if encoder_settings:
            from mif_pipeline.compartment_analysis.kronos_encoder import estimate_activation_bytes

            # The encoder's existing 2 GiB model/process allowance also covers
            # the measured 1.51 GiB CPU text-preparation peak plus the native
            # coordinator. Text is released before encoder attention/export.
            # Fits execute sequentially, so reserve the largest selected panel.
            encoder_memory_bytes = 2 * 1024**3 + max(
                estimate_activation_bytes(len(settings["marker_names"]), settings["patch_size"])
                for settings in encoder_settings)
            encoder_cpu_threads = max(2, *(settings["threads"] for settings in encoder_settings))
        demand = self._estimate_demand(node, spec, inputs, segmentation=segmentation,
            automatic_nimbus=automatic_nimbus,
            encoder_memory_bytes=encoder_memory_bytes, encoder_cpu_threads=encoder_cpu_threads)
        if (node.kind in TRAINING_PARENT_NODE_KINDS or node.kind in {
                "primary_classifier_inference", "secondary_classifier_inference",
                "supervised_phenotype_inference"}) and not encoder_settings:
            # The lease owner grants free RAM atomically with other reservations.
            # Final-results acceptance already returned its ordinary allowance.
            demand = replace(demand, use_available_memory=True)
        return demand

    @staticmethod
    def _estimate_demand(node, spec, inputs, *, segmentation=None,
                         automatic_nimbus=False, encoder_memory_bytes=None, encoder_cpu_threads=None):
        # Array allowances include the largest bounded work unit; reserve
        # runtime/reader/codec space as well. Native models and image servers
        # retain memory even when their selected input is a tiny JSON record.
        kind = ResourceKind(spec.resource_kind)
        model_task = node.kind == "segmentation" or encoder_memory_bytes is not None or automatic_nimbus
        native_task = kind in {
            ResourceKind.QUPATH_IMAGE_READ, ResourceKind.QUPATH_IMAGE_WRITE,
            ResourceKind.PROJECT_CATALOGUE_MUTATION,
            ResourceKind.SHARED_CLASSIFIER_MUTATION,
            ResourceKind.BIOFORMATS_CONVERSION,
        }
        if node.kind == "reference_unmixing":
            # We fit the numerical-array allowance to the currently available
            # process budget. The image engine uses this admission to reduce its
            # region batch and checks the exact minimum allocation for its inputs.
            from .batch_resources import observe_resources, available_working_memory

            observed = observe_resources()
            reserve = 256 * 1024**2
            free = available_working_memory(observed)
            unit = 64 * 1024**2
            arrays = max(unit, ((free - reserve) // unit) * unit)
            arrays = min(RegionExecutionPolicy().working_array_budget_bytes, arrays)
            working_set_bytes = reserve + arrays
        else:
            from .batch_resources import fixed_working_set_allowance
            working_set_bytes = fixed_working_set_allowance(node.kind, kind)
        if encoder_memory_bytes is not None:
            working_set_bytes = max(working_set_bytes, encoder_memory_bytes)
        if automatic_nimbus:
            # Measured model/native overlap is about 3 GiB. One 4 GiB allowance
            # covers both processes and leaves room for buffers and Python state.
            working_set_bytes = 4 * 1024**3
        if segmentation is not None and segmentation.method == "cellpose":
            # One SAM model plus the native image owner and bounded tile buffers.
            working_set_bytes = max(working_set_bytes, 4 * 1024**3)
        if node.kind.startswith("clustering_"):
            # Preparation files are small descriptors. Reserve for their actual
            # cell matrices and method workspaces, not the descriptor byte size.
            from .batch_phenotyping._common import read_bounded_file
            preparation_refs = [item for item in inputs
                                if item.artifact_kind == 'unsupervised_preparation_result']
            preparations = [item.owning_identity for item in preparation_refs]
            descriptions = [json.loads(read_bounded_file(Path(item.locator),
                expected_byte_count=item.byte_count, maximum_byte_count=16*1024**2))
                for item in preparation_refs]
            refit=getattr(getattr(node,'settled_review_revision',None),'cluster_refit',None)
            if refit is not None and not refit.restore_parent:
                descriptions=[{**item,'method':refit.method,'method_settings':dict(refit.method_settings),'k':refit.k}
                              for item in descriptions]
            if descriptions and all(item.get('method', 'snn_leiden') == 'flowsom' for item in descriptions):
                from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings
                nodes = max(validate_method_settings('flowsom', item.get('method_settings', {}))['grid_size']**2
                            for item in descriptions)
                features = max(int(item['measurement_matrix_bytes']) // (8*int(item['cell_count']))
                               for item in preparations)
                # Official SOM fitting retains per-cell labels/distances and a
                # small map; prediction distances are capped at 16 MiB. Include
                # identity/assignment publication and map/linkage workspaces.
                arrays = sum(3*int(item['measurement_matrix_bytes']) + 1024*int(item['cell_count'])
                             for item in preparations) + 16*1024**2 + 32*nodes**2 + 64*nodes*features
            elif descriptions and all(item.get('method') == 'phenograph' for item in descriptions):
                # The official Leiden wrapper retains Python endpoint pairs and
                # igraph copies (about 245 bytes per edge before partitioning).
                # Allow for its retained neighbours, sparse graphs and Leiden
                # workspace; the undirected edge count is at most cells * k.
                cell_count = sum(int(item['cell_count']) for item in preparations)
                arrays = sum(3*int(item['measurement_matrix_bytes'])
                             + int(item['cell_count'])*(512+384*min(int(item['k']), max(0, cell_count-1)))
                             for item in preparations)
            else:
                arrays = sum(3*int(item['measurement_matrix_bytes'])
                             + int(item['cell_count'])*(512+96*int(item['k']))
                             for item in preparations)
            working_set_bytes = max(working_set_bytes, 512*1024**2 + arrays,
                                    sum(item.byte_count or 0 for item in inputs)*8)
        return BatchResourceDemand(
            node.node_id,
            spec.spec_sha256,
            artifact_ref_set_sha256(inputs),
            kind,
            1,
            1,
            working_set_bytes,
            cpu_threads=(segmentation.workers if segmentation is not None else
                         encoder_cpu_threads if encoder_cpu_threads is not None else
                         1 if automatic_nimbus else 2 if native_task or model_task else 1),
            # Automatic device selection may use a GPU in the isolated worker;
            # reserve one permit without loading its tensor runtime to probe it.
            gpu_sessions=int(model_task and (segmentation.device if segmentation is not None
                else os.environ.get("MIF_COMPUTE_DEVICE", "auto")) != "cpu"),
        )

    @staticmethod
    def _dispatcher(registry, lease, *, activity=None):
        def dispatch(contexts, committer):
            first = contexts[0]
            binding = registry.resolve(
                first.adapter_spec.node_kind,
                first.adapter_spec.node_version,
            )
            if any(item.adapter_spec is not binding.spec for item in contexts):
                raise BatchServiceError("dispatch group mixed adapter owners")
            if binding.route == "raw_direct":
                if len(contexts) != 1:
                    raise BatchServiceError("raw direct route requires one context")
                outcome = binding.adapter.execute_raw(first, workspace_lease=lease)
                return committer.commit_raw((outcome,))
            if binding.route == "owner_committed":
                if len(contexts) != 1:
                    raise BatchServiceError("owner committed route requires one context")
                return (
                    binding.adapter.execute_committed(
                        first,
                        workspace_lease=lease,
                        terminal_committer=committer,
                    ),
                )
            if binding.route == "project_current_image":
                return binding.adapter.execute_project_current_image(
                    tuple(contexts),
                    workspace_lease=lease,
                    terminal_committer=committer,
                )
            raise BatchServiceError("runtime route is unsupported")

        if activity is None:
            return dispatch

        def observed_dispatch(contexts, committer):
            kind = contexts[0].adapter_spec.node_kind
            activity(kind, True)
            try:
                return dispatch(contexts, committer)
            finally:
                activity(kind, False)

        return observed_dispatch

    @staticmethod
    def _current_failure_codes(snapshot) -> dict[str, str]:
        """Report failed work that has no current authorized continuation.

        A pending retry retains its predecessor's failed terminal until the new
        attempt completes. We leave that history intact and let deferred work
        reach the existing incomplete-work pause and continuation path.
        """
        head = snapshot.head
        active_node_ids = {node.node_id for node in snapshot.plan.nodes}
        return {
            node_id: code
            for node_id, code in head.failure_codes_by_node.items()
            if node_id in active_node_ids
            and node_id not in head.invalidated_node_ids
            and node_id not in head.pending_authorization_sha256_by_node
            and head.node_status_by_id.get(node_id) is StageStatus.FAILED
        }

    @staticmethod
    def _completed_export_samples(snapshot) -> set[str]:
        if hasattr(snapshot, "completed_export_sample_ids"):
            return set(snapshot.completed_export_sample_ids)
        head = snapshot.head
        published_export_nodes = {
            authority.artifact_ref.producer_node_id
            for authority in snapshot.committed_artifact_authorities
            if authority.artifact_ref.artifact_kind == "final_non_spatial_export_result"
        }
        return {
            node.participant_ids[0]
            for node in snapshot.plan.nodes
            if node.kind == "final_non_spatial_export"
            and len(node.participant_ids) == 1
            and head.node_status_by_id.get(node.node_id) is StageStatus.COMPLETED
            and node.node_id not in head.invalidated_node_ids
            and node.node_id in published_export_nodes
        }

    @staticmethod
    def _closed_outcome(snapshot) -> str | None:
        head = snapshot.head
        if head.execution_state is BatchExecutionState.PAUSED:
            return "paused"
        if head.pending_interaction_ids:
            return "needs_interaction"
        if BatchService._current_failure_codes(snapshot):
            return "failed"
        if (
            not snapshot.plan.deferred_nodes
            and not head.invalidated_node_ids
            and all(
                head.node_status_by_id.get(node.node_id) is StageStatus.COMPLETED
                for node in snapshot.plan.nodes
            )
            and {sample.sample_id for sample in snapshot.manifest.enabled_samples}
            <= BatchService._completed_export_samples(snapshot)
        ):
            return "completed"
        return None

    def _remember_processed_images(self, snapshot) -> Mapping[str, Path]:
        """Retain display locators from an already opened current execution snapshot.

        The exact head limits reuse to this read-only view. Status polling can
        then use its compact progress record without reopening image owners.
        """
        paths = {owner.artifact_ref.scope.sample_id: owner.artifact_ref.locator
                 for owner in snapshot.committed_artifact_authorities
                 if owner.artifact_ref.artifact_kind == "processed_image"
                 and owner.artifact_ref.locator is not None}
        self._opened_processed_images = (snapshot.head.sha256, paths)
        return paths

    def _summary_rows(self, snapshot, outcome: str) -> tuple[BatchSummaryRow, ...]:
        head = snapshot.head
        if hasattr(snapshot, "committed_artifact_authorities"):
            processed_images = self._remember_processed_images(snapshot)
        elif self._opened_processed_images is not None and self._opened_processed_images[0] == head.sha256:
            processed_images = self._opened_processed_images[1]
        else:
            processed_images = head.processed_image_paths_by_sample
        node_digest = _canonical_sha256(
            {
                key: value.value if isinstance(value, StageStatus) else str(value)
                for key, value in head.node_status_by_id.items()
            }
        )
        domain_digest = _canonical_sha256(
            {
                key: value.value if isinstance(value, StageStatus) else str(value)
                for key, value in head.domain_task_status_by_id.items()
            }
        )
        current_failures = self._current_failure_codes(snapshot)
        active_node_ids = {node.node_id for node in snapshot.plan.nodes}
        pending_nodes = {
            node_id
            for node_id, status in head.node_status_by_id.items()
            if status is StageStatus.NEEDS_INTERACTION
            and node_id in active_node_ids
            and node_id not in head.invalidated_node_ids
            and node_id not in head.pending_authorization_sha256_by_node
        }
        export_completed = self._completed_export_samples(snapshot)

        def row_for(sample) -> BatchSummaryRow:
            sample_id = sample.sample_id
            scoped_failures = sorted(
                (node_id, code)
                for node_id, code in current_failures.items()
                if sample_id in head.node_scope_by_id[node_id]["sample_ids"]
            )
            scoped_pending = sorted(
                node_id
                for node_id in pending_nodes
                if sample_id in head.node_scope_by_id[node_id]["sample_ids"]
            )
            if sample_id in export_completed and not scoped_failures:
                status = "completed"
                stage = "export"
                progress_kind = "terminal"
            elif scoped_pending:
                status = "needs_interaction"
                stages = {
                    node.kind for node in snapshot.plan.nodes if node.node_id in scoped_pending
                }
                stage = next(iter(stages)) if len(stages) == 1 else "mixed"
                progress_kind = "uniform_next" if len(stages) == 1 else "mixed"
            else:
                status = (
                    "failed"
                    if scoped_failures
                    else (
                        "paused"
                        if head.execution_state is BatchExecutionState.PAUSED
                        else "incomplete"
                    )
                )
                stage = "none"
                progress_kind = "no_ready_work"
            return BatchSummaryRow(
                batch_id=head.batch_id,
                sample_id=sample_id,
                slide_id=sample.slide_id,
                subject_id=sample.subject_id,
                status=status,
                current_or_final_stage=stage,
                progress_kind=progress_kind,
                node_status_inventory_sha256=node_digest,
                domain_status_inventory_sha256=domain_digest,
                processed_image_path=(
                    Path(processed_images[sample_id])
                    if sample_id in processed_images
                    else None
                ),
                qupath_project_entry=head.sample_project_entry_id_by_sample.get(sample_id),
                pending_interaction_reason=None,
                failure_reason=scoped_failures[0][1] if scoped_failures else None,
                resolved_config_hash=sample.resolved_config_sha256,
                provenance_hash=None,
            )

        return tuple(row_for(sample) for sample in snapshot.manifest.enabled_samples)

    def _report(
        self,
        snapshot,
        outcome: str,
        committed: tuple[str, ...],
        prerequisites: tuple[BatchPrerequisiteItem, ...],
    ) -> BatchRunReport:
        return BatchRunReport(
            snapshot.head.batch_id,
            outcome,
            snapshot.head.sha256,
            snapshot.plan.plan_sha256,
            tuple(dict.fromkeys(committed)),
            snapshot.head.pending_interaction_ids,
            self._summary_rows(snapshot, outcome),
            prerequisites,
        )

    def _status_from_snapshot(self, snapshot) -> BatchStatusSnapshot:
        from .batch_workflow import project_plan_nodes
        from .batch_workflow_readiness import pending_response_node_ids

        outcome = self._closed_outcome(snapshot)
        if outcome == "completed":
            disposition = "completed"
        elif outcome == "paused":
            disposition = "paused_resumable"
        elif self._active_wave is not None:
            disposition = "active_current_session"
        else:
            disposition = "non_resumable"
        head = snapshot.head
        pending_authorization_ids = frozenset(head.pending_authorization_sha256_by_node)
        current_results = dict(getattr(snapshot, "current_results", {}))
        store = self._dependencies.runtime.state_store
        for node in snapshot.plan.nodes:
            if hasattr(snapshot, "current_results"):
                break
            if (
                node.node_id in pending_authorization_ids
                or node.node_id in head.invalidated_node_ids
                or head.node_status_by_id.get(node.node_id)
                not in {StageStatus.FAILED, StageStatus.NEEDS_INTERACTION}
            ):
                continue
            attempts = head.attempt_ids_by_node.get(node.node_id, ())
            digest = (
                head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) if attempts else None
            )
            if digest is not None:
                attempt = store.load_attempt(digest)
                if attempt.node_id != node.node_id:
                    raise BatchServiceError("current workflow diagnostic belongs to another node")
                current_results[node.node_id] = attempt.stage_result
        ready_ids = pending_response_node_ids(snapshot.plan, head)
        wait_messages = {}
        if self._active_wave is None and ready_ids:
            from .batch_resources import observe_resources, resource_memory_wait_reason
            observed = observe_resources()
            # Specifications describe this installed application, not current
            # settings or available RAM. Reuse them while observing both live.
            with self._lock:
                if self._status_stage_specs is None:
                    self._status_stage_specs = _application_stage_specs(
                        include_secondary_definitions=True)
                specifications = self._status_stage_specs
            for node in snapshot.plan.nodes:
                if node.node_id not in ready_ids or node.kind == "reference_unmixing" or node.kind.startswith("clustering_"):
                    continue
                spec = next(item for item in specifications.specs if item.node_kind == node.kind)
                automatic_nimbus = (node.kind == "marker_gate_review"
                    and self._automatic_nimbus_requested(node, head))
                allowance = self._estimate_demand(node, spec, (),
                    automatic_nimbus=automatic_nimbus).working_set_bytes
                reason = resource_memory_wait_reason(allowance, observed)
                if reason is not None:
                    wait_messages[node.node_id] = reason
        workflow = project_plan_nodes(
            snapshot.plan,
            statuses={
                key: value.value if isinstance(value, StageStatus) else str(value)
                for key, value in head.node_status_by_id.items()
            },
            eligible_ids=ready_ids,
            results=current_results,
            invalidated_ids=frozenset(head.invalidated_node_ids),
            pending_authorization_ids=pending_authorization_ids,
            source_correction_samples=(frozenset(sample.sample_id for sample in snapshot.manifest.enabled_samples)
                if head.operational_rebinding_sha256 is None else frozenset()),
        )
        return BatchStatusSnapshot(
            head.batch_id,
            head.sha256,
            snapshot.plan.plan_sha256,
            head.execution_state.value,
            disposition,
            head.quiescent_pause_sha256,
            {
                key: value.value if isinstance(value, StageStatus) else str(value)
                for key, value in head.node_status_by_id.items()
            },
            {
                key: value.value if isinstance(value, StageStatus) else str(value)
                for key, value in head.domain_task_status_by_id.items()
            },
            head.last_durable_node_ids_by_sample,
            head.next_eligible_node_ids_by_sample,
            head.pending_interaction_ids,
            self._summary_rows(snapshot, outcome or "incomplete"),
            snapshot.plan.prerequisites,
            workflow_nodes=workflow,
            resource_wait_messages=wait_messages,
            continuation_available=(
                disposition == "non_resumable"
                and head.execution_state is BatchExecutionState.ACTIVE
                and not self._current_failure_codes(snapshot)
            ),
        )

    @staticmethod
    def _unsupported_interaction_result(
        *,
        group: BatchInteractionGroup,
        request: BatchInteractionResponseRequest,
        state_head_sha256: str,
    ) -> BatchInteractionApplicationResult:
        if request.selection.target_ids:
            selected_ids = set(request.selection.target_ids)
            targets = tuple(item for item in group.targets if item.target_id in selected_ids)
            if {item.target_id for item in targets} != selected_ids:
                raise BatchInteractionError("interaction selection contains a foreign target")
        elif request.selection.validation_domain_ids:
            selected_domains = set(request.selection.validation_domain_ids)
            targets = tuple(
                item for item in group.targets if item.validation_domain_id in selected_domains
            )
        else:
            targets = group.targets
        applications = tuple(
            BatchTargetApplication(
                target_id=target.target_id,
                node_id=target.node_id,
                prior_attempt_id=target.attempt_id,
                prior_attempt_authorization_sha256=(target.attempt_authorization_sha256),
                prior_terminal_record_sha256=(target.needs_interaction_terminal_record_sha256),
                interaction_request_id=target.interaction_request_id,
                interaction_context_sha256=target.interaction_context_sha256,
                project_entry_id=target.project_entry_id,
                sample_id=target.sample_id,
                slide_id=target.slide_id,
                validation_domain_id=target.validation_domain_id,
                analysis_domain_bindings=target.analysis_domain_bindings,
                compatibility_signature_sha256=(target.compatibility_signature_sha256),
                status="rejected",
                accepted_response_sha256=None,
                retry_attempt_id=None,
                retry_authorization_sha256=None,
                rejection_code="unsupported_interaction",
            )
            for target in targets
        )
        return BatchInteractionApplicationResult(
            group_id=group.group_id,
            group_sha256=group.group_sha256,
            applications=applications,
            committed_state_head_sha256=state_head_sha256,
        )

    @staticmethod
    def _apply_aggregate_interaction_response(
        *,
        store,
        head,
        group,
        request,
        lease,
        paused_response_authority=None,
    ) -> BatchInteractionApplicationResult:
        target = group.target
        if (
            request.action != InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value
            or request.selection.target_ids != (target.target_id,)
        ):
            raise BatchInteractionError(
                "aggregate cluster review requires one exact selected target"
            )
        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
        nodes = {item.node_id: item for item in snapshot.plan.nodes}
        node = nodes.get(target.node_id)
        artifacts = {item.artifact_ref_sha256: item for item in snapshot.accepted_artifacts}
        evidence = artifacts.get(target.evidence_artifact_ref_sha256)
        if node is None or node.kind != "cluster_label_review" or evidence is None:
            raise BatchInteractionError("aggregate cluster review authority is not current")
        spec = _application_stage_specs().resolve(node.kind, node.version)
        predecessor = store.load_authorization(target.attempt_authorization_sha256)
        dispatch = predecessor.dispatch_binding
        response_material = BatchService._aggregate_response_material(
            target=target, request=request
        )
        response_sha256 = _canonical_sha256(response_material)
        response = {**response_material, "response_sha256": response_sha256}
        retry_request = BatchAttemptAuthorizationRequest(
            node=node,
            input_artifacts=(evidence,),
            adapter_spec=spec,
            dispatch_authority=BatchPreparedDispatchAuthority(
                compatibility_signature_sha256=(dispatch.compatibility_signature_sha256),
                current_image_identity_sha256=(dispatch.current_image_identity_sha256),
                script_sha256=dispatch.script_sha256,
                project_identity_sha256=dispatch.project_identity_sha256,
                project_authority_head_sha256=(dispatch.project_authority_head_sha256),
                project_qpproj_snapshot_sha256=(dispatch.project_qpproj_snapshot_sha256),
                project_entry_projection_sha256=(dispatch.project_entry_projection_sha256),
            ),
            retry_ordinal=len(head.attempt_ids_by_node.get(node.node_id, ())),
            interaction_response_sha256=response_sha256,
            interaction_predecessor_terminal_record_sha256=(
                target.needs_interaction_terminal_record_sha256
            ),
        )
        retry = store.authorize_cluster_label_review_retry(
            retry_request,
            target=target,
            canonical_response_bytes=_canonical_bytes(response),
            expected_head_sha256=head.sha256,
            lease=lease,
            paused_response_authority=paused_response_authority,
        )
        committed_head = store.load_head()
        return BatchInteractionApplicationResult(
            group_id=target.group_id,
            group_sha256=target.group_sha256,
            applications=(
                BatchAggregateTargetApplication(
                    target=target,
                    status="accepted_for_retry",
                    accepted_response_sha256=response_sha256,
                    retry_attempt_id=retry.attempt_id,
                    retry_authorization_sha256=retry.authorization_sha256,
                    rejection_code=None,
                ),
            ),
            committed_state_head_sha256=committed_head.sha256,
        )

    @staticmethod
    def _aggregate_response_material(*, target, request) -> dict[str, object]:
        if request.action != InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value:
            raise BatchInteractionError(
                "aggregate cluster review requires accept_unlabeled_clusters"
            )
        return {
            "schema_name": "mif-batch-interaction-response",
            "schema_version": 1,
            "target_id": target.target_id,
            "group_id": target.group_id,
            "group_sha256": target.group_sha256,
            "node_id": target.node_id,
            "prior_attempt_id": target.attempt_id,
            "prior_attempt_authorization_sha256": (target.attempt_authorization_sha256),
            "prior_terminal_record_sha256": (target.needs_interaction_terminal_record_sha256),
            "interaction_request_id": target.interaction_request_id,
            "interaction_context_sha256": target.interaction_context_sha256,
            "action": InteractionAction.ACCEPT.value,
            "response": _export_json(request.response),
            "response_validator_id": CLUSTER_REVIEW_RESPONSE_VALIDATOR_ID,
            "response_validator_version": (CLUSTER_REVIEW_RESPONSE_VALIDATOR_VERSION),
        }

    @staticmethod
    def _reopen_aggregate_interaction_group(
        store, head, request_id: str
    ) -> BatchAggregateInteractionGroup:
        snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
        matches: list[BatchAggregateInteractionGroup] = []
        for node in snapshot.plan.nodes:
            if node.kind != "cluster_label_review":
                continue
            try:
                target, evidence = store.reopen_cluster_label_review_presentation(
                    node.node_id, expected_head_sha256=head.sha256
                )
            except BatchStateError:
                continue
            if target.interaction_request_id != request_id:
                continue
            clustering_scope = node.clustering_scope
            if clustering_scope is None:
                raise BatchInteractionError("aggregate interaction lacks validation-domain scope")
            matches.append(
                BatchAggregateInteractionGroup(
                    target=target,
                    evidence=evidence,
                    validation_domain_ids=(clustering_scope.validation_domain_ids),
                )
            )
        if len(matches) != 1:
            raise BatchInteractionError("aggregate interaction request is absent or ambiguous")
        return matches[0]


class BatchServiceProvider:
    def __init__(
        self,
        workspace_layout: BatchWorkspaceLayout,
        dependencies: BatchRuntimeDependencies,
        prerequisite_inputs: (
            Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
        ) = None,
    ) -> None:
        self._workspace_layout = workspace_layout
        self._dependencies = dependencies
        self._prerequisite_inputs = prerequisite_inputs
        self._lock = RLock()
        self._active_uses = 0
        self._closed = False
        self._deferred_manager = None
        self._service_by_manifest: dict[Path, BatchService] = {}
        self._workspace_service: BatchService | None = None
        self._plot_lock = RLock()
        self._plot_directory: tempfile.TemporaryDirectory[str] | None = None
        self._plot_cache: dict[tuple[str, str, str], tuple[BatchReviewPlot, ...]] = {}
        self._plot_bytes = 0
        self._cell_gallery = None
        self._measurement_field = None
        self._prediction_review_index = None
        self._marker_gate_examples = None
        self._marker_combination_population = None
        self._reconstruction_field = None

    def _write_review_plots(
        self,
        outputs: tuple[tuple[str, str, str, bytes], ...],
    ) -> tuple[BatchReviewPlot, ...]:
        """Save rendered figures for the current review under the plot lock.

        We write the supplied image bytes into the provider's temporary folder
        and calculate their descriptor digest in memory. Closing each file
        completes the buffered write; a write or close failure removes the
        files created by this call before returning an error.
        """
        if self._plot_directory is None:
            self._plot_directory = tempfile.TemporaryDirectory(prefix="mif-review-plots-")
        directory = Path(self._plot_directory.name)
        created: list[Path] = []
        plots: list[BatchReviewPlot] = []
        try:
            for artifact_id, title, format_, data in outputs:
                path = directory / f"{uuid4().hex}.{format_}"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created.append(path)
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                plots.append(
                    BatchReviewPlot(
                        artifact_id=artifact_id,
                        title=title,
                        path=path,
                        sha256=hashlib.sha256(data).hexdigest(),
                        status="available",
                    )
                )
        except BaseException as exc:
            for path in created:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    exc.add_note(f"incomplete review plot cleanup failed: {cleanup_error}")
            if isinstance(exc, OSError):
                raise BatchReviewPlotError("review plot file could not be written") from exc
            raise
        return tuple(plots)

    def _clear_combination_population(self) -> None:
        cached = self._marker_combination_population
        self._marker_combination_population = None
        if cached is not None:
            cached[1].cleanup()

    def _cleanup_plots(self, primary: BaseException | None = None) -> None:
        with self._plot_lock:
            field = self._measurement_field
            self._measurement_field = None
            if field is not None:
                field[1].close()
            directory = self._plot_directory
            self._plot_directory = None
            self._plot_cache.clear()
            self._prediction_review_index = None
            self._cell_gallery = None
            self._marker_gate_examples = None
            self._clear_combination_population()
            self._reconstruction_field = None
            self._plot_bytes = 0
            if directory is not None:
                try:
                    directory.cleanup()
                except BaseException as exc:
                    if primary is None:
                        raise
                    primary.add_note(f"review plot cleanup failed: {exc}")

    def _teardown(self, manager, exc_type, exc, traceback) -> None:
        primary = exc
        try:
            manager.__exit__(exc_type, exc, traceback)
        except BaseException as teardown_error:
            if primary is None:
                primary = teardown_error
                raise
            primary.add_note(f"batch dependency teardown failed: {teardown_error}")
        finally:
            self._cleanup_plots(primary)

    def for_manifest(self, manifest_path: Path) -> BatchService:
        if not isinstance(manifest_path, Path):
            raise BatchServiceError("manifest_path must be a Path")
        path = manifest_path.expanduser().resolve(strict=True)
        data = path.read_bytes()
        try:
            parsed = BatchManifest.from_json_bytes(data)
            saved = self._dependencies.runtime.state_store.execution_snapshot_if_present()
            if saved is not None:
                validated = _selected_manifest_for_authoring(self._dependencies.runtime.state_store, data, saved, parsed)
            else:
                validated = validate_manifest(parsed)
        except Exception as exc:
            raise BatchServiceError("manifest validation failed") from exc
        if (
            validated.manifest.batch_id != self._workspace_layout.batch_id
            or validated.manifest.output_root.resolve(strict=False)
            != self._workspace_layout.output_root
        ):
            raise BatchServiceError("manifest names a foreign workspace")
        with self._lock:
            if self._closed:
                raise BatchServiceError("batch service provider is closed")
            service = self._service_by_manifest.get(path)
            if service is None:
                service = BatchService(
                    provider=self,
                    workspace_layout=self._workspace_layout,
                    dependencies=self._dependencies,
                    manifest_path=path,
                )
                self._service_by_manifest[path] = service
            if saved is not None:
                service._remember_processed_images(saved)
            return service

    def for_workspace(self, output_root: Path, batch_id: str) -> BatchService:
        if (
            not isinstance(output_root, Path)
            or output_root.expanduser().resolve(strict=False) != self._workspace_layout.output_root
            or batch_id != self._workspace_layout.batch_id
        ):
            raise BatchServiceError("workspace lookup is foreign")
        with self._lock:
            if self._closed:
                raise BatchServiceError("batch service provider is closed")
            if self._workspace_service is None:
                self._workspace_service = BatchService(
                    provider=self,
                    workspace_layout=self._workspace_layout,
                    dependencies=self._dependencies,
                    manifest_path=None,
                )
            return self._workspace_service

    def _use(self) -> _ProviderUse:
        return _ProviderUse(self)

    def _begin_use(self) -> None:
        with self._lock:
            if self._closed:
                raise BatchServiceError("batch service provider is closed")
            self._active_uses += 1

    def _finish_use(self) -> None:
        manager = None
        with self._lock:
            self._active_uses -= 1
            if self._active_uses == 0 and self._deferred_manager is not None:
                manager = self._deferred_manager
                self._deferred_manager = None
                self._closed = True
        if manager is not None:
            self._teardown(manager, None, None, None)

    def _close_or_defer(self, manager, primary: BaseException | None) -> bool:
        with self._lock:
            if self._active_uses:
                self._deferred_manager = manager
                if primary is not None:
                    primary.add_note("batch provider teardown refused while a live use remains")
                return False
            self._closed = True
            return True


@contextmanager
def open_batch_service_provider(
    *,
    workspace_layout: BatchWorkspaceLayout,
    qupath_executable: Path,
    linux_containment_profile: LinuxContainmentProfile | None = None,
    containment_expectation: ContainmentCompatibilityExpectationV1 | None = None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    cpu_native_root: Path | None = None,
    software_preferences_path: Path | None = None,
    prerequisite_inputs: (
        Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
    ) = None,
) -> Iterator[BatchServiceProvider]:
    qupath_command(qupath_executable, launch_binding=launch_binding)
    provider_inputs = prerequisite_inputs
    if prerequisite_inputs is not None:

        def checked_inputs(
            manifest: ValidatedBatchManifest,
            plan: BatchPlan,
        ) -> BatchPrerequisiteOwnerInputs:
            inputs = prerequisite_inputs(manifest, plan)
            if not isinstance(inputs, BatchPrerequisiteOwnerInputs):
                raise TypeError("owner_inputs must be BatchPrerequisiteOwnerInputs")
            if inputs.bioformats_inventory is not None:
                try:
                    require_approved_bioformats_inventory(inputs.bioformats_inventory)
                except (InventoryMissing, InventoryIncompatible) as exc:
                    raise BatchServiceError(
                        "prerequisite requires approved Bio-Formats inventory"
                    ) from exc
                try:
                    selected = selected_offline_bioformats_inventory()
                except (InventoryMissing, InventoryIncompatible) as exc:
                    raise BatchServiceError(
                        "prerequisite requires selected Bio-Formats payload"
                    ) from exc
                if inputs.bioformats_inventory != selected:
                    raise BatchServiceError(
                        "prerequisite differs from selected Bio-Formats payload"
                    )
            # Bind observations to the exact launcher passed to the runtime.
            # A different executable's successful probe grants no admission.
            if any(
                request.executable != qupath_executable
                for request in inputs.qupath_requests.values()
            ):
                raise BatchServiceError(
                    "prerequisite request must match the selected QuPath executable"
                )
            if any(
                request.launch_binding != launch_binding
                for request in inputs.qupath_requests.values()
            ):
                raise BatchServiceError(
                    "prerequisite request must match the selected QuPath profile"
                )
            return inputs

        provider_inputs = checked_inputs
    manager = open_batch_runtime_dependencies(
        workspace_layout=workspace_layout,
        qupath_executable=qupath_executable,
        linux_containment_profile=linux_containment_profile,
        containment_expectation=containment_expectation,
        launch_binding=launch_binding,
        cpu_native_root=cpu_native_root,
        software_preferences_path=software_preferences_path,
        prerequisite_inputs=provider_inputs,
    )
    dependencies = manager.__enter__()
    provider = BatchServiceProvider(workspace_layout, dependencies, provider_inputs)
    try:
        yield provider
    except BaseException as primary:
        if provider._close_or_defer(manager, primary):
            provider._teardown(manager, type(primary), primary, primary.__traceback__)
        raise
    else:
        if provider._close_or_defer(manager, None):
            provider._teardown(manager, None, None, None)
        else:
            raise BatchServiceError("batch provider teardown refused while a live use remains")


__all__ = [
    "batch_service_runtime_stage_adapter_kinds",
    "registered_batch_interaction_owner_inventory",
    "BatchService",
    "BatchServiceError",
    "BatchPrerequisiteAdmissionError",
    "BatchServiceProvider",
    "open_batch_service_provider",
]
