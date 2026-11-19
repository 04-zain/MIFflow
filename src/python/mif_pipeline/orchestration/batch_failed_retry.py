"""Admit supported failed stages while retaining their accepted upstream work.

We authenticate each failed attempt before issuing a fresh authorization. Project-stage
retries bind the recorded native process and current saved image; native fitting retries
bind the original abort archive and unchanged training inputs."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2

from mif_pipeline.pipeline_core import StageResult, StageStatus
from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectMutationLease,
    ProjectMutationLeaseRecord,
    QuPathProjectSnapshot,
)

from .batch_artifacts import (
    BatchAttemptAuthorization,
    BatchFailedRetry,
    BatchNodeExecutionContext,
    StageAdapterSpec,
    artifact_ref_set_sha256,
)
from .batch_project import deterministic_project_entry_name
from .batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchExecutionState,
    BatchHeadConflictError,
    BatchStateHead,
    BatchStateStore,
    BatchNodeAttempt,
    BatchWorkspaceLease,
)
from .project_stage_plan import _ReopenedProjectStageFailure
from .batch_training._shared import BatchTrainingError
from .batch_training.composition import TrainingRuntimeDependencies
from .batch_training.stage_adapters import TRAINING_EVIDENCE_NODE_KINDS, TRAINING_PARENT_NODE_KINDS

def is_failed_retry_node_kind(node_kind: str) -> bool:
    """Use the registered project-stage routes and existing native-fit routes."""
    from .batch_stage_adapters import PROJECT_CURRENT_IMAGE_ASSET_BY_NODE

    return node_kind in (
        *TRAINING_EVIDENCE_NODE_KINDS, *TRAINING_PARENT_NODE_KINDS,
    ) or node_kind in PROJECT_CURRENT_IMAGE_ASSET_BY_NODE


# These direct owners publish immutable results or atomically replace files for
# the exact input set. Resuming keeps the original response and producer identity.
PENDING_PYTHON_RETRY_KINDS = frozenset({
    "phenotyping_mode_resolution", "marker_gate_review", "marker_combination_selection", "unsupervised_preparation",
    "clustering_per_slide", "clustering_pooled",
    "cluster_review_evidence", "cluster_label_review", "final_non_spatial_export",
    "phenotype_hierarchy_attestation", "no_secondary_completion",
})

_FAILED_RETRY_ISSUER = object()


# ==============================================================================
#                                     Retry
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class _FailedRetryAdmission:
    """Invocation-only proof for one existing authorization transaction."""

    state_store: BatchStateStore
    workspace_lease: BatchWorkspaceLease
    project_lease: ProjectMutationLease
    request: BatchAttemptAuthorizationRequest
    head_sha256: str
    terminal_record_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("failed-stage retry admissions are owner-issued")

    def __reduce__(self) -> object:
        raise TypeError("failed-stage retry admissions are nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("failed-stage retry admissions are nonserializable")

    def __copy__(self) -> object:
        raise TypeError("failed-stage retry admissions are noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("failed-stage retry admissions are noncopyable")

    def _validate_for_authorize(
        self,
        *,
        state_store: BatchStateStore,
        workspace_lease: BatchWorkspaceLease,
        request: BatchAttemptAuthorizationRequest,
        head: BatchStateHead,
    ) -> None:
        if (
            type(self) is not _FailedRetryAdmission
            or self._issuer is not _FAILED_RETRY_ISSUER
            or self.state_store is not state_store
            or self.workspace_lease is not workspace_lease
            or self.request is not request
            or self.head_sha256 != head.sha256
            or head.execution_state is not BatchExecutionState.ACTIVE
            or head.node_status_by_id.get(request.node.node_id) is not StageStatus.FAILED
            or request.node.node_id in head.invalidated_node_ids
            or not is_failed_retry_node_kind(request.node.kind)
            or request.interaction_response_sha256 is not None
            or request.interaction_predecessor_terminal_record_sha256 is not None
            or request.failed_retry is None
            or request.failed_retry.terminal_record_sha256 != self.terminal_record_sha256
            or not isinstance(self.project_lease, ProjectMutationLease)
            or self.project_lease._provider is not state_store._project_mutation_provider
            or not self.project_lease.is_live
            or not workspace_lease.is_live
        ):
            raise BatchTrainingError("failed-stage retry admission is stale or foreign")
        attempts = head.attempt_ids_by_node.get(request.node.node_id, ())
        if (
            not attempts
            or request.retry_ordinal != len(attempts)
            or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
            != self.terminal_record_sha256
            or request.node.node_id in head.pending_authorization_sha256_by_node
        ):
            raise BatchTrainingError("failed-stage retry predecessor is not latest")


def reopen_project_stage_failure(
    *,
    dependencies: TrainingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    node_id: str,
    terminal_record_sha256: str,
    adapter_spec: StageAdapterSpec,
) -> _ReopenedProjectStageFailure:
    """Reopen retained failed evidence without inspecting live image data."""

    if (
        type(dependencies) is not TrainingRuntimeDependencies
        or type(adapter_spec) is not StageAdapterSpec
        or dependencies.state_store._project_mutation_provider is not dependencies.project_provider
        or dependencies.project_stage._state_store is not dependencies.state_store
    ):
        raise BatchTrainingError("project-stage reopen dependencies are invalid")
    dependencies.state_store._assert_lease(workspace_lease)
    bindings = tuple(
        codec.binding
        for codec in dependencies.project_stage._codec_registry.application_codecs()
        if codec.binding.adapter_spec_sha256 == adapter_spec.spec_sha256
        and codec.binding.terminal_stage_id == adapter_spec.node_kind
    )
    if len(bindings) != 1:
        raise BatchTrainingError("failed project stage lacks its exact registered codec")
    binding = bindings[0]
    reopener = dependencies.project_stage.committed_exchange_reopener(
        workspace_lease=workspace_lease
    )
    return reopener.reopen_failed_target_for_owner(
        node_id=node_id,
        terminal_record_sha256=terminal_record_sha256,
        expected_binding=(
            binding.adapter_spec_sha256,
            binding.target_script_asset_id,
            binding.wire_stage_id,
            binding.terminal_stage_id,
            binding.request_codec_version,
        ),
    )


def is_project_request_failure(node_kind: str, result: StageResult) -> bool:
    """Recognize the one failure issued before project request publication."""
    return (
        node_kind == "clustering_label_import"
        and result.stage == node_kind
        and result.status is StageStatus.FAILED
        and result.error_code == "cluster_label_request_failed"
    )


def _project_request_failure_predecessor(
    *,
    state: BatchStateStore,
    head: BatchStateHead,
    node_id: str,
    terminal: BatchNodeAttempt,
    adapter_spec: StageAdapterSpec,
) -> tuple[BatchNodeAttempt, BatchAttemptAuthorization]:
    """Read the exact unlaunched terminal without inventing native evidence.

    Initial Retry adds current input and project admission below. A pending
    Retry uses this retained association after its own native run may have
    started, so this reader must not require an empty native journal.
    """
    attempts = head.attempt_ids_by_node.get(node_id, ())
    previous = state.load_authorization(terminal.authorization_sha256)
    dispatch = previous.dispatch_binding
    if (
        not attempts
        or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != terminal.terminal_record_sha256
        or terminal.attempt_id != attempts[-1]
        or terminal.node_id != node_id
        or not is_project_request_failure(adapter_spec.node_kind, terminal.stage_result)
        or terminal.output_artifacts
        or terminal.execution_exchange_sha256 is not None
        or previous.node_id != node_id
        or previous.attempt_id != terminal.attempt_id
        or previous.retry_ordinal != len(attempts) - 1
        or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
        or node_id in head.invalidated_node_ids
        or dispatch.adapter_spec_sha256 != adapter_spec.spec_sha256
        or len(dispatch.participant_ids) != 1
        or len(dispatch.project_entry_ids) != 1
    ):
        raise BatchTrainingError("project request retry requires its latest unchanged failed attempt")
    return terminal, previous


def reopen_project_request_failure(
    *,
    dependencies: TrainingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    head: BatchStateHead,
    node_id: str,
    terminal: BatchNodeAttempt,
    adapter_spec: StageAdapterSpec,
) -> tuple[BatchNodeAttempt, BatchAttemptAuthorization]:
    """Admit the recorded request failure only while the same image is unchanged.

    The known issuer ran before publication and refused unresolved journals.
    No exchange exists to reopen. We retain strict current task/input binding
    and reuse the existing unrelated-entry history check for project advances.
    Physical image-data inspection remains at the live Retry lease boundary.
    """
    state = dependencies.state_store
    provider = dependencies.project_provider
    state._assert_lease(workspace_lease)
    terminal, previous = _project_request_failure_predecessor(
        state=state, head=head, node_id=node_id,
        terminal=terminal, adapter_spec=adapter_spec,
    )
    if (state._project_mutation_provider is not provider
            or node_id in head.pending_authorization_sha256_by_node
            or provider._unresolved_active_records_for_project(
                project_uri=Path(head.project_path).as_uri(),
                project_identity_sha256=head.project_identity_sha256)):
        raise BatchTrainingError("new project request retry requires a drained project")
    # Full head recovery already authenticates the terminal/dispatch fields.
    # Reuse the existing unchanged-task admission instead of repeating them.
    state._plan_material_is_compatible(head=head, authorization=previous)
    dispatch = previous.dispatch_binding
    entry_id = dispatch.project_entry_ids[0]
    sample_id = dispatch.participant_ids[0]
    catalogue = state.project_catalogue_store(workspace_lease).load_current()
    entry = catalogue.resolve(entry_id)
    exact_project = (
        dispatch.project_identity_sha256 == head.project_identity_sha256
        and dispatch.project_authority_head_sha256 == head.project_authority_head_sha256
        and dispatch.project_qpproj_snapshot_sha256 == head.project_qpproj_snapshot_sha256
    )
    if (
        catalogue.catalogue_sha256 != head.project_catalogue_sha256
        or catalogue.project_identity_sha256 != head.project_identity_sha256
        or entry.sample_id != sample_id
        or entry.image_role != "primary_mif"
        or entry.image_sha256 != dispatch.current_image_identity_sha256
        or entry.entry_projection_sha256 != dispatch.project_entry_projection_sha256
        or head.sample_project_entry_id_by_sample.get(sample_id) != entry_id
        or head.sample_project_entry_projection_sha256_by_sample.get(sample_id)
        != entry.entry_projection_sha256
        or not (exact_project or state._authenticated_project_stage_predecessor_binding(
            authorization=previous, binding=dispatch, head=head,
            require_unaffected_project_entry_id=entry_id,
        ))
    ):
        raise BatchTrainingError("project request retry inputs or image changed")
    return terminal, previous


def _check_live_target(
    *,
    state_store: BatchStateStore,
    head: BatchStateHead,
    reopened: _ReopenedProjectStageFailure,
    snapshot: QuPathProjectSnapshot,
    workspace_lease: BatchWorkspaceLease,
) -> None:
    """Check the exact native target selected by the failed exchange opener."""
    target = reopened.authorized
    _check_live_project_entry(
        state_store=state_store, head=head, project_entry_id=target.project_entry_id,
        sample_id=target.sample_id, slide_id=target.slide_id,
        entry_projection_sha256=reopened.context.project_entry_projection_sha256,
        snapshot=snapshot, workspace_lease=workspace_lease,
    )


def _check_live_project_entry(
    *,
    state_store: BatchStateStore,
    head: BatchStateHead,
    project_entry_id: str,
    sample_id: str,
    slide_id: str,
    entry_projection_sha256: str,
    snapshot: QuPathProjectSnapshot,
    workspace_lease: BatchWorkspaceLease,
) -> None:
    """Compare the live entry with its authenticated saved-state projection.

    Native failures also name their observed saved state. A request failure
    has no native observation; its initial admission already proved that the
    entry did not change, so the current saved-state projection supplies it.
    """
    catalogue = state_store.project_catalogue_store(workspace_lease).load_current()
    entry = catalogue.resolve(project_entry_id)
    current_saved = state_store.current_project_entry_saved_state_sha256_by_id(
        expected_head_sha256=head.sha256,
        lease=workspace_lease,
    )
    matches = tuple(
        item for item in snapshot.entries if item.project_entry_id == project_entry_id
    )
    required_metadata = {
        "mif_pipeline.batch_id": head.batch_id,
        "mif_pipeline.sample_id": entry.sample_id,
        "mif_pipeline.slide_id": entry.slide_id,
        "mif_pipeline.validation_domain_id": entry.validation_domain_id,
        "mif_pipeline.image_role": entry.image_role,
        "mif_pipeline.source_image_sha256": entry.image_sha256,
    }
    if entry.reference_modality is not None:
        required_metadata["mif_pipeline.reference_modality"] = entry.reference_modality
    if (
        catalogue.catalogue_sha256 != head.project_catalogue_sha256
        or snapshot.project_uri != catalogue.project_uri
        or not same_project_snapshot(
            Path(head.project_path), head.project_qpproj_snapshot_sha256,
            snapshot.qpproj_sha256,
        )
        or snapshot.identity_record is None
        or snapshot.identity_record.project_identity_sha256 != head.project_identity_sha256
        or len(matches) != 1
        or entry.image_role != "primary_mif"
        or entry.sample_id != sample_id
        or entry.slide_id != slide_id
        or entry.entry_projection_sha256 != entry_projection_sha256
    ):
        raise BatchTrainingError("project-stage current project differs")
    live = matches[0]
    if (
        live.entry_name
        != deterministic_project_entry_name(entry.sample_id, entry.slide_id, entry.image_role)
        or entry.canonical_uri not in live.source_uris
        or sum(entry.canonical_uri in item.source_uris for item in snapshot.entries) != 1
        or sum(
            all(
                item.metadata.get(key) == value
                for key, value in required_metadata.items()
                if key != "mif_pipeline.source_image_sha256"
            )
            for item in snapshot.entries
        )
        != 1
        or any(live.metadata.get(key) != value for key, value in required_metadata.items())
        or live.saved_state_attestation_sha256 != current_saved[project_entry_id]
    ):
        raise BatchTrainingError("project-stage live image data differs")


def _native_training_retry_predecessor(
    *,
    state: BatchStateStore,
    head: BatchStateHead,
    node_id: str,
    terminal_record_sha256: str,
    archive_ref: AuthorityMemberRefV2 | None = None,
) -> tuple[
    AuthorityMemberRefV2, BatchNodeAttempt, BatchAttemptAuthorization,
]:
    """Bind a retry's retained provenance to its latest failed predecessor.

    We read the original terminal and authorization, require the supported
    drained failure, and compare its archive reference with the admitted retry
    provenance. This association remains valid when the pending retry later
    saves a fit; current scientific state belongs to the native-session opener.
    """
    attempts = head.attempt_ids_by_node.get(node_id, ())
    terminal = state.load_attempt(terminal_record_sha256)
    authorization = state.load_authorization(terminal.authorization_sha256)
    failure_context = dict(terminal.stage_result.error_context or {})
    recorded_archive = failure_context.pop("native_abort_archive_ref", None)
    if recorded_archive is None:
        raise BatchTrainingError("native fit retry terminal lacks its retained abort archive")
    recorded_archive = AuthorityMemberRefV2.from_mapping(recorded_archive)
    if archive_ref is not None and archive_ref != recorded_archive:
        raise BatchTrainingError("native fit retry archive differs from the failed terminal")
    archive_ref = recorded_archive
    if (
        not attempts
        or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != terminal_record_sha256
        or terminal.attempt_id != attempts[-1]
        or authorization.attempt_id != terminal.attempt_id
        or authorization.node_id != node_id
        or terminal.node_id != node_id
        or terminal.stage_result.stage not in TRAINING_PARENT_NODE_KINDS
        or terminal.stage_result.status is not StageStatus.FAILED
        or terminal.stage_result.error_code != "native_classifier_fit_failed"
        or set(failure_context) != {"disposition_code", "failure_code"}
        or failure_context["disposition_code"] != "drained_failure"
        or terminal.output_artifacts
        or terminal.execution_exchange_sha256 is not None
        or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
        or node_id in head.invalidated_node_ids
        or authorization.admission_plan_sha256 != head.active_plan_sha256
    ):
        raise BatchTrainingError(
            "native fit retry requires its latest drained failed attempt"
        )
    return archive_ref, terminal, authorization


def reopen_native_training_failure(
    *,
    dependencies: TrainingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    node_id: str,
    terminal_record_sha256: str,
    archive_ref: AuthorityMemberRefV2 | None = None,
) -> tuple[
    ProjectMutationLeaseRecord, BatchNodeAttempt, BatchAttemptAuthorization,
]:
    """Prove that one failed native attempt can receive a new retry authorization.

    Initial admission requires no batch outputs, an abort proving no published
    classifier, and no selected fit checkpoint. These facts cover separate
    publication stores. We also bind the abort to the current project before
    authorizing new work; pending continuation uses its already admitted
    provenance and the ordinary scientific opener instead.
    """
    from mif_pipeline.qupath_runtime.catalogue import ProjectMutationArtifactInventory

    state = dependencies.state_store
    state._assert_lease(workspace_lease)
    snapshot = state.reopen_execution_snapshot()
    head = snapshot.head
    archive_ref, terminal, authorization = _native_training_retry_predecessor(
        state=state,
        head=head,
        node_id=node_id,
        terminal_record_sha256=terminal_record_sha256,
        archive_ref=archive_ref,
    )
    if node_id in head.pending_training_fit_set_sha256_by_node:
        raise BatchTrainingError("new native fit retry cannot replace a selected fit checkpoint")
    binding = terminal.execution_context_binding
    provider = dependencies.project_provider
    archived = provider.reopen_native_training_abort_archive(
        Path(head.project_path).resolve().as_uri(), archive_ref=archive_ref,
    )
    inventory_ref = state._batch_project_ref(
        "batch_project_inventory", inventory_sha256=archived.artifact_inventory_sha256,
    )
    inventory = ProjectMutationArtifactInventory.from_mapping(
        state._read_batch_project_ref(inventory_ref, label="failed native fit artifact inventory")
    )
    acquisition = state.reopen_project_mutation_acquisition_record(
        archived.acquisition_record_sha256, lease=workspace_lease,
    )
    abort = provider.reopen_native_training_failure(archived, inventory)
    qpproj_targets = [target for target in inventory.static_targets if target.target_kind == "qpproj"]
    if (
        archived.project_identity_sha256 != head.project_identity_sha256
        or len(qpproj_targets) != 1
        or qpproj_targets[0].predecessor_sha256 != head.project_qpproj_snapshot_sha256
        or abort.observed_project_authority_head_sha256 != head.project_authority_head_sha256
        or acquisition.expected_authority_head_sha256 != head.project_authority_head_sha256
        or acquisition.expected_project_identity_sha256 != head.project_identity_sha256
        or acquisition.mutation_request_sha256 != archived.mutation_request_sha256
        or acquisition.mutation_authorization_sha256 != archived.mutation_authorization_sha256
        or acquisition.artifact_inventory_sha256 != inventory.artifact_inventory_sha256
    ):
        raise BatchTrainingError("native fit abort does not bind the failed batch transition")
    refs = {ref.artifact_ref_sha256: ref for ref in snapshot.accepted_artifacts}
    if not set(binding.input_artifact_ref_sha256s) <= refs.keys():
        raise BatchTrainingError("native fit retry inputs are no longer current")
    return archived, terminal, authorization


def authorize_failed_stage_retry(
    *,
    dependencies: TrainingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    request: BatchAttemptAuthorizationRequest,
    expected_head_sha256: str,
    selected_failed_terminal_record_sha256: str,
) -> BatchAttemptAuthorization:
    """Authorize one explicit failed-stage retry while current project control is held."""

    if (
        type(dependencies) is not TrainingRuntimeDependencies
        or type(request) is not BatchAttemptAuthorizationRequest
        or dependencies.state_store._project_mutation_provider is not dependencies.project_provider
    ):
        raise BatchTrainingError("failed-stage retry dependencies are invalid")
    state = dependencies.state_store
    state._assert_lease(workspace_lease)
    head = state.load_head()
    if head.sha256 != expected_head_sha256:
        raise BatchHeadConflictError("failed-stage retry selection is stale")
    if (
        head.execution_state is not BatchExecutionState.ACTIVE
        or head.node_status_by_id.get(request.node.node_id) is not StageStatus.FAILED
        or request.node.node_id in head.invalidated_node_ids
        or request.node.node_id in head.pending_authorization_sha256_by_node
        or request.retry_ordinal != len(head.attempt_ids_by_node.get(request.node.node_id, ()))
        or request.interaction_response_sha256 is not None
        or request.interaction_predecessor_terminal_record_sha256 is not None
    ):
        raise BatchTrainingError("failed-stage retry selection is ineligible")
    if not is_failed_retry_node_kind(request.node.kind):
        raise BatchTrainingError("retry target is not a supported failed stage")
    native = None
    request_failure = None
    if request.node.kind in TRAINING_PARENT_NODE_KINDS:
        native = reopen_native_training_failure(
            dependencies=dependencies, workspace_lease=workspace_lease, node_id=request.node.node_id,
            terminal_record_sha256=selected_failed_terminal_record_sha256)
    elif request.node.kind == "clustering_label_import":
        terminal = state.load_attempt(selected_failed_terminal_record_sha256)
        if is_project_request_failure(request.node.kind, terminal.stage_result):
            request_failure = reopen_project_request_failure(
                dependencies=dependencies, workspace_lease=workspace_lease, head=head,
                node_id=request.node.node_id,
                terminal=terminal,
                adapter_spec=request.adapter_spec,
            )
    request = replace(request, failed_retry=BatchFailedRetry(
        selected_failed_terminal_record_sha256,
        None if native is None else native[0].lease_archive_ref))
    with state.current_project_control(operation="retry", lease=workspace_lease) as control:
        if control.head.sha256 != expected_head_sha256:
            raise BatchHeadConflictError("failed-stage retry control head changed")
        control.project_processes.assert_all_drained(
            batch_id=head.batch_id,
            project_identity_sha256=head.project_identity_sha256,
        )
        if native is not None:
            archived, terminal, failed_authorization = native
            if (failed_authorization.retry_ordinal + 1 != request.retry_ordinal
                or failed_authorization.input_artifact_set_sha256 != artifact_ref_set_sha256(request.input_artifacts)
                or terminal.execution_context_binding.adapter_spec_sha256 != request.adapter_spec.spec_sha256
                or archived.predecessor_authority_head_sha256 != control.head.project_authority_head_sha256):
                raise BatchTrainingError("native fit retry request differs from the failed attempt")
        elif request_failure is not None:
            terminal, previous = request_failure
            binding = previous.dispatch_binding
            if (
                previous.retry_ordinal + 1 != request.retry_ordinal
                or previous.input_artifact_set_sha256 != artifact_ref_set_sha256(request.input_artifacts)
                or binding.current_image_identity_sha256
                != request.dispatch_authority.current_image_identity_sha256
                or binding.project_entry_projection_sha256
                != request.dispatch_authority.project_entry_projection_sha256
            ):
                raise BatchTrainingError("project request retry differs from the failed attempt")
            snapshot = dependencies.project_provider.inspect_live_project(control.project_lease)
            _check_live_project_entry(
                state_store=state, head=head,
                project_entry_id=binding.project_entry_ids[0],
                sample_id=binding.participant_ids[0],
                slide_id=terminal.stage_result.slide_id,
                entry_projection_sha256=binding.project_entry_projection_sha256,
                snapshot=snapshot, workspace_lease=workspace_lease,
            )
        else:
            reopened = reopen_project_stage_failure(
                dependencies=dependencies,
                workspace_lease=workspace_lease,
                node_id=request.node.node_id,
                terminal_record_sha256=selected_failed_terminal_record_sha256,
                adapter_spec=request.adapter_spec,
            )
            if (
                reopened.state_head_sha256 != head.sha256
                or reopened.context.adapter_spec != request.adapter_spec
                or reopened.context.authorization.retry_ordinal + 1 != request.retry_ordinal
                or reopened.context.node_id != request.node.node_id
                or reopened.context.authorization.input_artifact_set_sha256
                != artifact_ref_set_sha256(request.input_artifacts)
                or reopened.context.current_image_identity_sha256
                != request.dispatch_authority.current_image_identity_sha256
                or reopened.context.project_entry_projection_sha256
                != request.dispatch_authority.project_entry_projection_sha256
            ):
                raise BatchTrainingError("failed-stage retry request differs from failure")
            snapshot = dependencies.project_provider.inspect_live_project(control.project_lease)
            _check_live_target(
                state_store=state,
                head=head,
                reopened=reopened,
                snapshot=snapshot,
                workspace_lease=workspace_lease,
            )
        admission = object.__new__(_FailedRetryAdmission)
        for name, value in {
            "state_store": state,
            "workspace_lease": workspace_lease,
            "project_lease": control.project_lease,
            "request": request,
            "head_sha256": head.sha256,
            "terminal_record_sha256": selected_failed_terminal_record_sha256,
            "_issuer": _FAILED_RETRY_ISSUER,
        }.items():
            object.__setattr__(admission, name, value)
        return state.authorize_attempts(
            (request,),
            expected_head_sha256,
            workspace_lease,
            failed_retry_admissions=(admission,),
        )[0]


def revalidate_failed_stage_retry(
    *,
    dependencies: TrainingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    context: BatchNodeExecutionContext,
) -> None:
    """Check admitted retry provenance before continuing its pending execution.

    We authenticate the current authorization and its exact failed predecessor
    without repeating initial native failure admission after a fit has been
    saved. The native-session owner reopens selected checkpoints and scientific
    files; physical saved-image inspection remains the stage runtime's check.
    Independent region and marker retries keep their terminal association here
    and reopen the full native exchange once in their execution owner.
    """

    if (
        type(dependencies) is not TrainingRuntimeDependencies
        or type(context) is not BatchNodeExecutionContext
        or dependencies.state_store._project_mutation_provider is not dependencies.project_provider
    ):
        raise BatchTrainingError("failed-stage retry dependencies are invalid")
    state = dependencies.state_store
    state._assert_lease(workspace_lease)
    current = state.authenticate_current_execution_context(context)
    head = state.load_head()
    authorization = context.authorization
    attempt_ids = head.attempt_ids_by_node.get(context.node_id, ())
    if (
        current.state_head_sha256 != head.sha256
        or head.execution_state is not BatchExecutionState.ACTIVE
        or head.node_status_by_id.get(context.node_id) is not StageStatus.FAILED
        or context.node_id in head.invalidated_node_ids
        or not is_failed_retry_node_kind(context.adapter_spec.node_kind)
        or authorization.retry_ordinal != len(attempt_ids)
        or authorization.was_invalidated_at_admission
        or not attempt_ids
        or authorization.interaction_response_sha256 is not None
        or authorization.interaction_predecessor_terminal_record_sha256 is not None
    ):
        raise BatchTrainingError("pending failed-stage retry is ineligible")
    terminal_sha256 = head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
    if terminal_sha256 is None:
        raise BatchTrainingError("pending failed-stage retry lacks predecessor")
    proof = authorization.failed_retry
    if proof is None or proof.terminal_record_sha256 != terminal_sha256:
        raise BatchTrainingError("pending failed-stage retry lacks its exact failed predecessor")
    if context.adapter_spec.node_kind in TRAINING_PARENT_NODE_KINDS:
        if proof.native_abort_archive_ref is None:
            raise BatchTrainingError("native fit retry lacks its original abort archive")
        _archive, terminal, previous = _native_training_retry_predecessor(
            state=state, head=head, node_id=context.node_id,
            terminal_record_sha256=terminal_sha256, archive_ref=proof.native_abort_archive_ref)
        if (previous.retry_ordinal + 1 != authorization.retry_ordinal
            or previous.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
            or terminal.execution_context_binding.adapter_spec_sha256 != context.adapter_spec.spec_sha256):
            raise BatchTrainingError("pending native fit retry differs from its failed attempt")
        return
    if proof.native_abort_archive_ref is not None:
        raise BatchTrainingError("project-stage retry carries a native fit archive")
    if context.adapter_spec.node_kind == "clustering_label_import":
        terminal = state.load_attempt(terminal_sha256)
        if is_project_request_failure(context.adapter_spec.node_kind, terminal.stage_result):
            _terminal, previous = _project_request_failure_predecessor(
                state=state, head=head, node_id=context.node_id,
                terminal=terminal, adapter_spec=context.adapter_spec,
            )
            if (
                previous.retry_ordinal + 1 != authorization.retry_ordinal
                or previous.node_material_sha256 != authorization.node_material_sha256
                or previous.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
                or previous.dispatch_binding.current_image_identity_sha256
                != context.current_image_identity_sha256
                or previous.dispatch_binding.project_entry_projection_sha256
                != context.project_entry_projection_sha256
            ):
                raise BatchTrainingError("pending project request retry differs from failure")
            # This new attempt follows the ordinary project-stage owner. Its
            # own started or interrupted journal is handled there, not reset.
            return
    if context.adapter_spec.node_kind in {"analysis_domain_selection", "marker_rule_application"}:
        terminal = state.load_attempt(terminal_sha256)
        previous = state.load_authorization(terminal.authorization_sha256)
        binding = terminal.execution_context_binding
        if (
            terminal.node_id != context.node_id
            or terminal.attempt_id != attempt_ids[-1]
            or terminal.stage_result.status is not StageStatus.FAILED
            or terminal.output_artifacts
            or previous.node_id != context.node_id
            or previous.attempt_id != terminal.attempt_id
            or previous.retry_ordinal + 1 != authorization.retry_ordinal
            or previous.node_material_sha256 != authorization.node_material_sha256
            or previous.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
            or binding.adapter_spec_sha256 != context.adapter_spec.spec_sha256
            or binding.project_entry_projection_sha256 != context.project_entry_projection_sha256
            or binding.current_image_identity_sha256 != context.current_image_identity_sha256
        ):
            raise BatchTrainingError("pending retained-result retry differs from failure")
        return
    reopened = reopen_project_stage_failure(
        dependencies=dependencies,
        workspace_lease=workspace_lease,
        node_id=context.node_id,
        terminal_record_sha256=terminal_sha256,
        adapter_spec=context.adapter_spec,
    )
    if (
        reopened.state_head_sha256 != head.sha256
        or reopened.context.adapter_spec != context.adapter_spec
        or reopened.context.authorization.retry_ordinal + 1 != authorization.retry_ordinal
        or reopened.context.authorization.input_artifact_set_sha256
        != authorization.input_artifact_set_sha256
        or reopened.context.project_entry_projection_sha256
        != context.project_entry_projection_sha256
    ):
        raise BatchTrainingError("pending failed-stage retry differs from failure")


__all__ = [
    "authorize_failed_stage_retry",
    "reopen_project_stage_failure",
    "revalidate_failed_stage_retry",
]
