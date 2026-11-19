"""Run one native classifier fit and retain its completed model publication.

The session opens the declared project/model destination, prepares the validated request and
delegates numerical fitting to QuPath. It checks the returned model and saved state before
completing the fit record. Reopening a completed fit retains its exact model rather than
running the training operation again."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping
from weakref import ReferenceType, ref

from mif_pipeline.pipeline_core.stage_result import StageResult
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectMutationLeaseProvider,
)
from mif_pipeline.qupath_runtime.runner import (
    QuPathNativeTrainingProcessCompletion,
    QuPathRunner,
    format_qupath_process_diagnostics,
)

from ..batch_artifacts import BatchNodeExecutionContext, BatchNodeExecutionResult
from ..batch_state import (
    BatchNativeFitCheckpointWriter,
    BatchRawNodeOutcome,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    CurrentNativeFitSetAuthority,
    ScheduledNodeResult,
)
from ._shared import BatchTrainingError, ClassifierKind, _digest
from .engines import (
    NativeFitRegistryCommitAuthority,
    TrainingEngineExecutionReservation,
    TrainingEngineProviderRegistry,
    TrainingEngineReadinessAuthority,
    TrainingEngineReadinessDisposition,
    TrainingEngineReadinessResult,
    TrainingFitPlanProjection,
)
from .evidence import (
    PixelFitEvidenceAuthority,
    SupervisedFitEvidenceAuthority,
)
from .native_projection import QuPathPixelEntryChannelFeatures
from .parent_execution import (
    CurrentTrainingParentExecution,
    open_current_training_parent_execution,
    validate_current_training_parent_execution,
)
from .runtime import TrainingPlanSetStore

# ==============================================================================
#                                 Native session
# ==============================================================================


def _bounded_note(primary: BaseException, cleanup: BaseException, context: str) -> None:
    try:
        primary.add_note(f"{context}: {type(cleanup).__name__}: {str(cleanup)[:240]}"[:512])
    except BaseException:
        pass


@dataclass(slots=True)
class _SessionRecord:
    session_ref: ReferenceType["QuPathTrainingMutationSession"]
    snapshot: tuple[object, ...]
    state: str = "open"
    finalizable_fit_identity: int | None = None


def _session_snapshot(
    session: "QuPathTrainingMutationSession",
) -> tuple[object, ...]:
    return (
        id(session._state_store),
        id(session._context),
        id(session._current_parent),
        id(session._workspace_lease),
        id(session._checkpoint_writer),
        id(session._terminal_committer),
        id(session._project_leases),
        id(session._engine_registry),
        id(session._fit_authority),
        id(session._plan_set_store),
        id(session._runner),
        tuple(
            (
                item.project_entry_id,
                item.channel_features.to_mapping(),
            )
            for item in session._pixel_entry_channel_features
        ),
    )


class _SessionIssuer:
    """Retain lifecycle state outside session-mutatable fields."""

    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._sessions: dict[int, _SessionRecord] = {}

    def issue(self, session: "QuPathTrainingMutationSession") -> "QuPathTrainingMutationSession":
        with self._lock:
            key = id(session)

            def release(
                expired: ReferenceType[QuPathTrainingMutationSession],
                *,
                session_key: int = key,
            ) -> None:
                with self._lock:
                    current = self._sessions.get(session_key)
                    if current is not None and current.session_ref is expired:
                        self._sessions.pop(session_key, None)

            self._sessions[key] = _SessionRecord(
                session_ref=ref(session, release),
                snapshot=_session_snapshot(session),
            )
        return session

    def validate(
        self,
        session: object,
        *,
        required_state: str = "open",
    ) -> "QuPathTrainingMutationSession":
        if type(session) is not QuPathTrainingMutationSession:
            raise BatchTrainingError("QuPath training mutation session is closed or foreign")
        with self._lock:
            current = self._sessions.get(id(session))
            if current is None or current.session_ref() is not session:
                raise BatchTrainingError("QuPath training mutation session is closed or foreign")
            if current.state == "closed":
                raise BatchTrainingError("QuPath training mutation session is closed")
            if current.snapshot != _session_snapshot(session):
                current.state = "closed"
                raise BatchTrainingError("QuPath training mutation session changed")
            if current.state != required_state:
                if required_state == "finalizable":
                    raise BatchTrainingError("QuPath training mutation session is not finalizable")
                raise BatchTrainingError("QuPath training mutation session is already consumed")
        return session

    def consume(self, session: object) -> "QuPathTrainingMutationSession":
        checked = self.validate(session)
        with self._lock:
            current = self._sessions[id(checked)]
            if current.state != "open":
                raise BatchTrainingError("QuPath training mutation session is already consumed")
            current.state = "consumed"
        return checked

    def mark_finalizable(
        self,
        session: object,
        current_fit_set: CurrentNativeFitSetAuthority,
    ) -> None:
        if type(current_fit_set) is not CurrentNativeFitSetAuthority:
            raise BatchTrainingError("QuPath training mutation session lacks an exact current fit")
        checked = self.validate(session, required_state="consumed")
        with self._lock:
            current = self._sessions[id(checked)]
            if current.state != "consumed":
                raise BatchTrainingError("QuPath training mutation session is not finalizable")
            current.finalizable_fit_identity = id(current_fit_set)
            current.state = "finalizable"

    def claim_finalization(
        self,
        session: object,
        current_fit_set: object,
    ) -> "QuPathTrainingMutationSession":
        checked = self.validate(session, required_state="finalizable")
        with self._lock:
            current = self._sessions[id(checked)]
            current.state = "closed"
            if type(
                current_fit_set
            ) is not CurrentNativeFitSetAuthority or current.finalizable_fit_identity != id(
                current_fit_set
            ):
                raise BatchTrainingError(
                    "finalization requires the exact reopened current fit authority"
                )
        return checked

    def close(self, session: object) -> None:
        if type(session) is not QuPathTrainingMutationSession:
            return
        with self._lock:
            current = self._sessions.get(id(session))
            if current is not None and current.session_ref() is session:
                current.state = "closed"

    def close_unless_finalizable(self, session: object) -> None:
        if type(session) is not QuPathTrainingMutationSession:
            return
        with self._lock:
            current = self._sessions.get(id(session))
            if (
                current is not None
                and current.session_ref() is session
                and current.state != "finalizable"
            ):
                current.state = "closed"


_SESSION_ISSUER = _SessionIssuer()


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
)
class QuPathTrainingMutationSession:
    """Issuer-only one-use capability for one current aggregate-parent fit."""

    _state_store: BatchStateStore
    _context: BatchNodeExecutionContext
    _current_parent: CurrentTrainingParentExecution
    _workspace_lease: BatchWorkspaceLease
    _checkpoint_writer: BatchNativeFitCheckpointWriter
    _terminal_committer: BatchTerminalCommitter
    _project_leases: ProjectMutationLeaseProvider
    _engine_registry: TrainingEngineProviderRegistry
    _fit_authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority
    _pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...]
    _plan_set_store: TrainingPlanSetStore
    _runner: QuPathRunner

    def _reopen_unlaunched_acquisition(self, active: object) -> object:
        """Reopen the recorded reservation solely to settle its unused project lease."""
        _validate_session(self, required_state="consumed")
        store, workspace_lease = self._state_store, self._workspace_lease
        head = store.load_head()
        acquisition = store.reopen_project_mutation_acquisition_record(
            active.acquisition_record_sha256, lease=workspace_lease,
        )
        if (acquisition.operation != "annotation_classifier"
                or acquisition.expected_project_identity_sha256 != head.project_identity_sha256
                or acquisition.expected_authority_head_sha256 != head.project_authority_head_sha256):
            raise BatchTrainingError("unused native acquisition differs from the current project")
        _request, capability = store.project_catalogue_store(workspace_lease)._reopen_annotation_classifier_request(
            acquisition.mutation_request_sha256, workspace_lease=workspace_lease,
            state_store_issuer=store._project_durability_capability,
        )
        inventory = store.reopen_project_mutation_artifact_inventory(
            acquisition.artifact_inventory_sha256, lease=workspace_lease,
        )
        return self._project_leases.issue_acquisition_session(
            acquisition, request=capability, inventory=inventory,
            durability_authority=store.project_mutation_durability_authority(workspace_lease),
        )

    def _authenticate_unlaunched_abort(self, project_lease: object) -> object:
        """Bind preparation cleanup to this live current training session."""
        _validate_session(self, required_state="consumed")
        self._project_leases._validate_session(project_lease._acquisition_session)
        head = self._state_store.load_head()
        record = project_lease._acquisition_session.record
        if (record.operation != "annotation_classifier"
                or record.expected_project_identity_sha256 != head.project_identity_sha256
                or record.expected_authority_head_sha256 != head.project_authority_head_sha256
                or project_lease.designation.project_path.resolve().as_uri() != record.project_uri):
            raise BatchTrainingError("native preparation acquisition is not current")
        return head

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("QuPath training mutation sessions are issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("QuPath training mutation sessions cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("QuPath training mutation sessions are noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("QuPath training mutation sessions are noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("QuPath training mutation sessions are nonserializable")


def _validate_session(
    session: QuPathTrainingMutationSession,
    *,
    required_state: str = "open",
) -> None:
    _SESSION_ISSUER.validate(session, required_state=required_state)
    if (
        type(session) is not QuPathTrainingMutationSession
        or type(session._state_store) is not BatchStateStore
        or type(session._context) is not BatchNodeExecutionContext
        or type(session._current_parent) is not CurrentTrainingParentExecution
        or type(session._workspace_lease) is not BatchWorkspaceLease
        or type(session._checkpoint_writer) is not BatchNativeFitCheckpointWriter
        or type(session._terminal_committer) is not BatchTerminalCommitter
        or type(session._project_leases) is not ProjectMutationLeaseProvider
        or type(session._engine_registry) is not TrainingEngineProviderRegistry
        or type(session._plan_set_store) is not TrainingPlanSetStore
        or type(session._runner) is not QuPathRunner
        or not all(
            type(item) is QuPathPixelEntryChannelFeatures
            for item in session._pixel_entry_channel_features
        )
    ):
        raise BatchTrainingError("QuPath training mutation session is invalid")
    try:
        session._state_store.validate_writer_lease(session._workspace_lease)
        session._state_store.validate_native_fit_checkpoint_writer(
            session._checkpoint_writer,
            context=session._context,
            lease=session._workspace_lease,
        )
        session._state_store.validate_terminal_committer(
            session._terminal_committer,
            contexts=(session._context,),
            lease=session._workspace_lease,
        )
        validate_current_training_parent_execution(
            session._current_parent,
            expected_context=session._context,
            state_store=session._state_store,
        )
        session._engine_registry.project_native_fit_authority(
            session._fit_authority,
            current_parent=session._current_parent,
        )
    except Exception as exc:
        _SESSION_ISSUER.close(session)
        raise BatchTrainingError("QuPath training mutation session authority is stale") from exc


def _require_finalizable_training_session(session: object) -> QuPathTrainingMutationSession:
    """Reject expired sessions before resolving decisions; leave the claim unspent."""
    return _SESSION_ISSUER.validate(session, required_state="finalizable")


def _claim_finalizable_training_session(
    session: object,
    current_fit_set: object,
) -> QuPathTrainingMutationSession:
    """Burn and return one exact fresh-dispatch terminal finalization claim."""

    _validate_session(session, required_state="finalizable")
    return _SESSION_ISSUER.claim_finalization(session, current_fit_set)


@contextmanager
def open_qupath_training_mutation_session(
    *,
    state_store: BatchStateStore,
    context: BatchNodeExecutionContext,
    current_parent: CurrentTrainingParentExecution,
    workspace_lease: BatchWorkspaceLease,
    checkpoint_writer: BatchNativeFitCheckpointWriter,
    terminal_committer: BatchTerminalCommitter,
    project_leases: ProjectMutationLeaseProvider,
    engine_registry: TrainingEngineProviderRegistry,
    fit_authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority,
    plan_set_store: TrainingPlanSetStore,
    runner: QuPathRunner,
    pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...] = (),
) -> Iterator[QuPathTrainingMutationSession]:
    """Open one exact session while its caller retains the writer lease."""

    values = {
        "_state_store": state_store,
        "_context": context,
        "_current_parent": current_parent,
        "_workspace_lease": workspace_lease,
        "_checkpoint_writer": checkpoint_writer,
        "_terminal_committer": terminal_committer,
        "_project_leases": project_leases,
        "_engine_registry": engine_registry,
        "_fit_authority": fit_authority,
        "_pixel_entry_channel_features": tuple(pixel_entry_channel_features),
        "_plan_set_store": plan_set_store,
        "_runner": runner,
    }
    session = object.__new__(QuPathTrainingMutationSession)
    for name, value in values.items():
        object.__setattr__(session, name, value)
    _SESSION_ISSUER.issue(session)
    _validate_session(session)
    try:
        yield session
    finally:
        _SESSION_ISSUER.close(session)


def _training_process_failure_message(completion, process_authority) -> str:
    """Expose the runner's already-retained excerpts without another log read."""
    logs = process_authority._authority_layout.bucket("logs")
    return "QuPath-native classifier fitting failed.\n\n" + format_qupath_process_diagnostics(
        termination_reason=completion.termination_reason,
        exit_status=completion.exit_status,
        log_paths=(logs.member_path(process_authority._stdout_ref), logs.member_path(process_authority._stderr_ref)),
        excerpts=(completion.stdout_excerpt, completion.stderr_excerpt),
        truncated=(completion.stdout_truncated, completion.stderr_truncated),
    )


def _failed_outcome(
    context: BatchNodeExecutionContext,
    terminal_committer: BatchTerminalCommitter,
    *,
    fit_plan: TrainingFitPlanProjection,
    error_code: str,
    error_message: str,
    error_context: Mapping[str, object] | None,
) -> ScheduledNodeResult:
    slide_id = fit_plan.training_entries[0].slide_id
    results = terminal_committer.commit_raw(
        (
            BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.failed(
                    stage=context.adapter_spec.node_kind,
                    slide_id=slide_id,
                    error_code=error_code,
                    error_message=error_message,
                    error_context=error_context,
                ),
                execution_result=BatchNodeExecutionResult(
                    authorization_sha256=(context.authorization.authorization_sha256),
                    outputs=(),
                ),
                execution_exchange_sha256=None,
            ),
        )
    )
    return results[0]


def _interaction_outcome(
    context: BatchNodeExecutionContext,
    terminal_committer: BatchTerminalCommitter,
    *,
    fit_plan: TrainingFitPlanProjection,
    readiness: TrainingEngineReadinessResult,
) -> ScheduledNodeResult:
    interaction = readiness.interaction
    if interaction is None:
        raise BatchTrainingError("native training interaction readiness is invalid")
    results = terminal_committer.commit_raw(
        (
            BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.needs_interaction(
                    stage=context.adapter_spec.node_kind,
                    slide_id=fit_plan.training_entries[0].slide_id,
                    reason=interaction.interaction_reason,
                    message=interaction.message,
                    allowed_actions=interaction.allowed_actions,
                    resume_context={
                        "schema_version": "native-training-readiness/v1",
                        "fit_plan_sha256": interaction.fit_plan_sha256,
                        "provider_interaction_envelope": (interaction.resume_context),
                    },
                    checkpoint_id=interaction.checkpoint_id,
                ),
                execution_result=BatchNodeExecutionResult(
                    authorization_sha256=(context.authorization.authorization_sha256),
                    outputs=(),
                ),
                execution_exchange_sha256=None,
            ),
        )
    )
    return results[0]


def _reopen_selected_before_reservation(
    state_store: BatchStateStore,
    context: BatchNodeExecutionContext,
    current_parent: CurrentTrainingParentExecution,
    workspace_lease: BatchWorkspaceLease,
    engine_registry: TrainingEngineProviderRegistry,
    *,
    fit_plan: TrainingFitPlanProjection,
    plan_set_store: TrainingPlanSetStore,
) -> tuple[
    CurrentNativeFitSetAuthority | None,
    CurrentNativeFitSetAuthority | None,
    CurrentTrainingParentExecution,
]:
    """Reuse a selected fit after checking its scientific and durability owners.

    We reopen model bytes once at selection. If its lease was already archived
    and the selected state stays unchanged, that same checked fit is returned.
    Recording newly completed durability work retains the ordinary checkpoint
    and parent reopening path before any fit can be reused.
    """

    current = state_store.reopen_current_native_fit_set(
        context,
        current_parent=current_parent,
        engine_registry=engine_registry,
    )
    if current is None:
        return None, None, current_parent
    try:
        cleanup = state_store.complete_selected_native_fit_durability(
            context=context,
            current_parent=current_parent,
            current_fit_set=current,
            lease=workspace_lease,
        )
        if cleanup.disposition not in {"completed_now", "already_completed"}:
            raise BatchTrainingError("selected native fit cleanup returned an invalid outcome")
        completion_recorded = set(
            current.checkpoint_set.completed_lease_archive_ref_by_fit_material_sha256
        ) == {
            entry.runtime_fit_material_sha256 for entry in current.checkpoint_set.entries
        }
        if not completion_recorded:
            current = state_store.record_selected_native_fit_completion(
                context=context,
                current_parent=current_parent,
                current_fit_set=current,
                lease=workspace_lease,
            )
            current_parent = open_current_training_parent_execution(
                context,
                state_store=state_store,
                plan_set_store=plan_set_store,
            )
    except Exception as exc:
        raise BatchTrainingError("selected native fit cleanup requires manual review") from exc
    if cleanup.disposition == "already_completed" and completion_recorded:
        if state_store.load_head().sha256 != current.state_head_sha256:
            raise BatchTrainingError("selected native fit changed during cleanup")
        reopened = current
    else:
        reopened = state_store.reopen_current_native_fit_set(
            context,
            current_parent=current_parent,
            engine_registry=engine_registry,
        )
    if reopened is None or reopened.checkpoint_set != current.checkpoint_set:
        raise BatchTrainingError("selected native fit changed during cleanup")
    matches = tuple(
        entry
        for entry in reopened.checkpoint_set.entries
        if entry.classifier_id == fit_plan.classifier_id
        and entry.fit_plan_sha256 == fit_plan.fit_plan_sha256
    )
    return (reopened if len(matches) == 1 else None), reopened, current_parent


def _require_canonical_next_missing_fit(
    *,
    current_parent: CurrentTrainingParentExecution,
    current_fit_set: CurrentNativeFitSetAuthority | None,
    fit_plan: TrainingFitPlanProjection,
) -> None:
    selected = (
        frozenset()
        if current_fit_set is None
        else frozenset(entry.classifier_id for entry in current_fit_set.checkpoint_set.entries)
    )
    missing = tuple(
        plan
        for plan in current_parent.training_plan_set.plan_set.plans
        if plan.request.classifier_id not in selected
    )
    if (
        not missing
        or missing[0].request.classifier_id != fit_plan.classifier_id
        or TrainingFitPlanProjection.create(missing[0]).fit_plan_sha256 != fit_plan.fit_plan_sha256
    ):
        raise BatchTrainingError(
            "native training must reserve the canonical next missing classifier"
        )


def _execute_qupath_training_mutation_session(
    session: QuPathTrainingMutationSession,
    *,
    cancellation_event: object | None = None,
) -> CurrentNativeFitSetAuthority | ScheduledNodeResult:
    """Reopen an exact fit or run and checkpoint one contained native fit."""

    _validate_session(session)
    session = _SESSION_ISSUER.consume(session)
    state_store = session._state_store
    context = session._context
    current_parent = session._current_parent
    workspace_lease = session._workspace_lease
    checkpoint_writer = session._checkpoint_writer
    terminal_committer = session._terminal_committer
    project_leases = session._project_leases
    engine_registry = session._engine_registry
    fit_authority = session._fit_authority
    pixel_entry_channel_features = session._pixel_entry_channel_features
    plan_set_store = session._plan_set_store
    runner = session._runner
    fit_plan = engine_registry.project_native_fit_authority(
        fit_authority,
        current_parent=current_parent,
    )
    selected, current_fit_set, current_parent = _reopen_selected_before_reservation(
        state_store,
        context,
        current_parent,
        workspace_lease,
        engine_registry,
        fit_plan=fit_plan,
        plan_set_store=plan_set_store,
    )
    if selected is not None:
        _SESSION_ISSUER.mark_finalizable(session, selected)
        return selected
    _require_canonical_next_missing_fit(
        current_parent=current_parent,
        current_fit_set=current_fit_set,
        fit_plan=fit_plan,
    )

    reservation = engine_registry.reserve_native(
        fit_authority,
        requested_launch_mode="headless",
        pixel_entry_channel_features=pixel_entry_channel_features,
    )
    try:
        readiness = engine_registry.assess_readiness(reservation)
    except BaseException as primary:
        try:
            reservation.abandon()
        except BaseException as cleanup:
            _bounded_note(
                primary,
                cleanup,
                "native fit reservation cleanup also failed",
            )
        raise
    if type(readiness) is TrainingEngineReadinessResult:
        if readiness.disposition is TrainingEngineReadinessDisposition.NEEDS_INTERACTION:
            return _interaction_outcome(
                context,
                terminal_committer,
                fit_plan=fit_plan,
                readiness=readiness,
            )
        return _failed_outcome(
            context,
            terminal_committer,
            fit_plan=fit_plan,
            error_code=readiness.error_code or "native_fit_readiness_rejected",
            error_message=readiness.error_message
            or "QuPath-native training readiness was rejected.",
            error_context={
                "fit_plan_sha256": readiness.fit_plan_sha256,
                "readiness_disposition_code": readiness.disposition.value,
            },
        )
    if type(readiness) is not TrainingEngineReadinessAuthority:
        failure = BatchTrainingError("native training readiness requires manual review")
        try:
            reservation.abandon()
        except BaseException as cleanup:
            _bounded_note(
                failure,
                cleanup,
                "native fit reservation cleanup also failed",
            )
        raise failure

    ready_claim_live = True
    primary: BaseException | None = None
    try:
        dispatch_authority = engine_registry.prepare_native_training_dispatch(
            reservation=reservation, readiness=readiness, runner=runner,
        )
        with state_store.annotation_classifier_project_acquisition(
            reservation,
            context,
            lease=workspace_lease,
            project_leases=project_leases,
        ) as acquisition_session:
            project_leases.settle_unlaunched_native_training(acquisition_session, runtime=session)
            with project_leases.acquire(acquisition_session) as project_lease:
                try:
                    process_authority = runner.issue_native_training_process_authority(
                        dispatch_authority=dispatch_authority,
                        project_lease=project_lease,
                    )
                except BaseException as exc:
                    try:
                        project_leases._abort_unlaunched_native_training(project_lease, runtime=session)
                    except BaseException as cleanup:
                        _bounded_note(exc, cleanup, "native preparation cleanup also failed")
                    raise
                try:
                    completion = runner.run_training_process(
                        process_authority,
                        cancellation_event=cancellation_event,
                    )
                except BaseException as exc:
                    failure = BatchTrainingError(
                        "native training process requires manual review; no refit was attempted"
                    )
                    failure.__cause__ = exc
                    raise failure
                if type(completion) is not QuPathNativeTrainingProcessCompletion:
                    raise BatchTrainingError("native training completion requires manual review")
                if completion.disposition == "completed":
                    try:
                        project_commit = project_lease.finalize_native_classifier_fit(completion)
                        registry_authority: NativeFitRegistryCommitAuthority = (
                            engine_registry.authenticate_native_fit_commit(
                                reservation=reservation,
                                readiness=readiness,
                                completion=completion,
                                project_commit=project_commit,
                            )
                        )
                        ready_claim_live = False
                        successor = state_store.prepare_native_fit_checkpoint(
                            context=context,
                            current_parent=current_parent,
                            registry_authority=registry_authority,
                            project_commit=project_commit,
                            lease=workspace_lease,
                        )
                        current, receipt = checkpoint_writer.commit(successor)
                        project_lease.complete(receipt)
                        refreshed_parent = open_current_training_parent_execution(
                            context,
                            state_store=state_store,
                            plan_set_store=plan_set_store,
                        )
                        current = state_store.record_selected_native_fit_completion(
                            context=context,
                            current_parent=refreshed_parent,
                            current_fit_set=current,
                            lease=workspace_lease,
                        )
                        refreshed_parent = open_current_training_parent_execution(
                            context,
                            state_store=state_store,
                            plan_set_store=plan_set_store,
                        )
                        reopened = state_store.reopen_current_native_fit_set(
                            context,
                            current_parent=refreshed_parent,
                            engine_registry=engine_registry,
                        )
                        if reopened is None or reopened.checkpoint_set != current.checkpoint_set:
                            raise BatchTrainingError("native fit changed after lease completion")
                    except BaseException as exc:
                        failure = BatchTrainingError(
                            "native fit checkpoint requires manual review; automatic refit is forbidden"
                        )
                        failure.__cause__ = exc
                        raise failure
                    return reopened

                observed_head = state_store.load_head().sha256
                try:
                    abort_evidence, abort_archive_ref = project_lease.issue_native_training_abort_evidence(
                        completion,
                        observed_batch_state_head_sha256=observed_head,
                    )
                except BaseException as exc:
                    failure = BatchTrainingError(
                        "native training failure requires manual review; no false terminal was committed"
                    )
                    failure.__cause__ = exc
                    raise failure
                scheduled = _failed_outcome(
                    context,
                    terminal_committer,
                    fit_plan=fit_plan,
                    error_code="native_classifier_fit_failed",
                    error_message=_training_process_failure_message(completion, process_authority),
                    error_context={
                        "disposition_code": completion.disposition,
                        "failure_code": completion.failure_kind,
                        "native_abort_archive_ref": abort_archive_ref.to_mapping(),
                    },
                )
                try:
                    project_lease.abort(abort_evidence)
                except BaseException as exc:
                    failure = BatchTrainingError(
                        "native fit failed terminal requires manual review during lease cleanup"
                    )
                    _bounded_note(failure, exc, "project lease abort also failed")
                    raise failure from exc
                return scheduled
    except BatchTrainingError as exc:
        primary = exc
        raise
    except BaseException as exc:
        failure = BatchTrainingError(
            "native training acquisition requires manual review; automatic refit is forbidden"
        )
        failure.__cause__ = exc
        primary = failure
        raise failure
    finally:
        if ready_claim_live:
            try:
                reservation.abandon()
            except BaseException as cleanup:
                if primary is None:
                    raise BatchTrainingError(
                        "native fit reservation cleanup requires manual review"
                    ) from cleanup
                _bounded_note(
                    primary,
                    cleanup,
                    "native fit reservation cleanup also failed",
                )


def execute_qupath_training_mutation_session(
    session: QuPathTrainingMutationSession,
    *,
    cancellation_event: object | None = None,
) -> CurrentNativeFitSetAuthority | ScheduledNodeResult:
    """Execute once, retaining only an exact no-op reopen for finalization."""

    try:
        result = _execute_qupath_training_mutation_session(
            session,
            cancellation_event=cancellation_event,
        )
    except BaseException:
        _SESSION_ISSUER.close(session)
        raise
    _SESSION_ISSUER.close_unless_finalizable(session)
    return result


__all__ = [
    "NativeFitRegistryCommitAuthority",
    "QuPathTrainingMutationSession",
    "execute_qupath_training_mutation_session",
    "open_qupath_training_mutation_session",
]
