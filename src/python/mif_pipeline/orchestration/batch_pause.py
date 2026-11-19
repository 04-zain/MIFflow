"""Authenticated durable graceful-pause proof for batch execution.

The `BatchQuiescentPauseRecord` record carries `schema_name`, `schema_version`, `batch_id`,
`pause_id`, `pausing_session_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
import hashlib
import json
import re
from typing import Iterator, Protocol
from uuid import uuid4

from mif_pipeline.orchestration.batch_scheduler import (
    BatchScheduler,
    BatchSchedulerError,
    QuiescenceReceipt,
)
from mif_pipeline.orchestration.batch_state import (
    BatchExecutionState,
    BatchHeadConflictError,
    BatchStateError,
    BatchStateHead,
    BatchStateStore,
    BatchWorkspaceLease,
    _batch_request_id,
)
from mif_pipeline.pipeline_core import assert_current_writer
from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease

PAUSE_SCHEMA_NAME = "mif-batch-quiescent-pause"
PAUSE_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z")


# ==============================================================================
#                                  Batch pause
# ==============================================================================


class BatchPauseError(RuntimeError):
    """Raised when a durable graceful-pause proof cannot be authenticated."""


class ProjectProcessRegistryView(Protocol):
    """Owner-issued proof that no project child process remains live."""

    def assert_all_drained(self, *, batch_id: str, project_identity_sha256: str) -> None: ...


_PAUSE_STATE_AUTHORITY_ISSUER = object()


class _PauseStateTransitionAuthority:
    """Private non-serializable, one-use bridge into batch state mutation."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchPauseError("pause state authority is owner-issued only")

    def __copy__(self) -> "_PauseStateTransitionAuthority":
        raise BatchPauseError("pause state authority may not be copied")

    def __deepcopy__(self, memo: object) -> "_PauseStateTransitionAuthority":
        del memo
        raise BatchPauseError("pause state authority may not be copied")

    def __reduce__(self) -> object:
        raise BatchPauseError("pause state authority may not be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise BatchPauseError("pause state authority may not be serialized")

    @classmethod
    def _issue(
        cls,
        *,
        state_store: BatchStateStore,
        workspace_lease: BatchWorkspaceLease,
        operation: str,
        pause_record_sha256: str,
        expected_head_sha256: str,
        scheduler_receipt_capability: object | None,
        candidate_head_sha256: str | None = None,
    ) -> "_PauseStateTransitionAuthority":
        if operation not in {"publish", "pause", "consume", "response"}:
            raise BatchPauseError("pause state operation is invalid")
        authority = object.__new__(cls)
        authority._issuer = _PAUSE_STATE_AUTHORITY_ISSUER
        authority._state_store = state_store
        authority._workspace_lease = workspace_lease
        authority._operation = operation
        authority._pause_record_sha256 = _sha256(pause_record_sha256, "pause_record_sha256")
        authority._expected_head_sha256 = _sha256(expected_head_sha256, "expected_head_sha256")
        authority._scheduler_receipt_capability = scheduler_receipt_capability
        authority._candidate_head_sha256 = candidate_head_sha256
        authority._used = False
        return authority


def _issue_state_authority(
    *,
    state_store: BatchStateStore,
    workspace_lease: BatchWorkspaceLease,
    operation: str,
    pause_record_sha256: str,
    expected_head_sha256: str,
    scheduler_receipt_capability: object | None = None,
    candidate_head_sha256: str | None = None,
) -> _PauseStateTransitionAuthority:
    return _PauseStateTransitionAuthority._issue(
        state_store=state_store,
        workspace_lease=workspace_lease,
        operation=operation,
        pause_record_sha256=pause_record_sha256,
        expected_head_sha256=expected_head_sha256,
        scheduler_receipt_capability=scheduler_receipt_capability,
        candidate_head_sha256=candidate_head_sha256,
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise BatchPauseError(f"{field_name} must be a SHA-256 digest")
    return value


def _segment(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        raise BatchPauseError(f"{field_name} is invalid")
    return value


def _terminal_inventory_sha256(head: BatchStateHead) -> str:
    return _digest(
        {
            "terminal_attempts": [
                [attempt_id, terminal_sha256]
                for attempt_id, terminal_sha256 in sorted(
                    head.terminal_attempt_sha256_by_attempt_id.items()
                )
            ],
            "pending_training_fit_sets": [
                [node_id, checkpoint_sha256]
                for node_id, checkpoint_sha256 in sorted(
                    head.pending_training_fit_set_sha256_by_node.items()
                )
            ],
        }
    )


def _pending_interaction_inventory_sha256(head: BatchStateHead) -> str:
    return _digest(list(head.pending_interaction_ids))


@dataclass(frozen=True, slots=True)
class BatchQuiescentPauseRecord:
    schema_name: str
    schema_version: int
    batch_id: str
    pause_id: str
    pausing_session_id: str
    predecessor_state_head_sha256: str
    active_plan_sha256: str
    project_authority_head_sha256: str
    terminal_attempt_inventory_sha256: str
    pending_interaction_inventory_sha256: str
    scheduler_quiescence_state_head_sha256: str
    all_project_processes_drained: bool
    sha256: str

    def __post_init__(self) -> None:
        if (self.schema_name, self.schema_version) != (
            PAUSE_SCHEMA_NAME,
            PAUSE_SCHEMA_VERSION,
        ):
            raise BatchPauseError("pause record must use mif-batch-quiescent-pause/v1")
        try:
            assert_current_writer(PAUSE_SCHEMA_NAME, str(PAUSE_SCHEMA_VERSION))
        except ValueError as exc:
            raise BatchPauseError("pause record schema is not current") from exc
        for field_name in ("batch_id", "pause_id", "pausing_session_id"):
            object.__setattr__(self, field_name, _segment(getattr(self, field_name), field_name))
        for field_name in (
            "predecessor_state_head_sha256",
            "active_plan_sha256",
            "project_authority_head_sha256",
            "terminal_attempt_inventory_sha256",
            "pending_interaction_inventory_sha256",
            "scheduler_quiescence_state_head_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        if self.all_project_processes_drained is not True:
            raise BatchPauseError("pause record must prove all project processes drained")
        if self.predecessor_state_head_sha256 != self.scheduler_quiescence_state_head_sha256:
            raise BatchPauseError("pause record scheduler receipt differs from predecessor")
        if self.sha256 != _digest(self._unsigned_mapping()):
            raise BatchPauseError("pause record checksum does not match canonical bytes")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "pause_id": self.pause_id,
            "pausing_session_id": self.pausing_session_id,
            "predecessor_state_head_sha256": self.predecessor_state_head_sha256,
            "active_plan_sha256": self.active_plan_sha256,
            "project_authority_head_sha256": self.project_authority_head_sha256,
            "terminal_attempt_inventory_sha256": self.terminal_attempt_inventory_sha256,
            "pending_interaction_inventory_sha256": self.pending_interaction_inventory_sha256,
            "scheduler_quiescence_state_head_sha256": self.scheduler_quiescence_state_head_sha256,
            "all_project_processes_drained": self.all_project_processes_drained,
        }

    @classmethod
    def create(
        cls,
        *,
        batch_id: str,
        pausing_session_id: str,
        head: BatchStateHead,
        scheduler_quiescence_state_head_sha256: str,
    ) -> "BatchQuiescentPauseRecord":
        values: dict[str, object] = {
            "schema_name": PAUSE_SCHEMA_NAME,
            "schema_version": PAUSE_SCHEMA_VERSION,
            "batch_id": batch_id,
            "pause_id": "pause-" + uuid4().hex,
            "pausing_session_id": pausing_session_id,
            "predecessor_state_head_sha256": head.sha256,
            "active_plan_sha256": head.active_plan_sha256,
            "project_authority_head_sha256": head.project_authority_head_sha256,
            "terminal_attempt_inventory_sha256": _terminal_inventory_sha256(head),
            "pending_interaction_inventory_sha256": _pending_interaction_inventory_sha256(head),
            "scheduler_quiescence_state_head_sha256": scheduler_quiescence_state_head_sha256,
            "all_project_processes_drained": True,
        }
        values["sha256"] = _digest(values)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def create_response_successor(
        cls,
        *,
        predecessor: BatchStateHead,
        successor: BatchStateHead,
        pausing_session_id: str,
        scheduler_quiescence_state_head_sha256: str,
    ) -> "BatchQuiescentPauseRecord":
        values: dict[str, object] = {
            "schema_name": PAUSE_SCHEMA_NAME,
            "schema_version": PAUSE_SCHEMA_VERSION,
            "batch_id": predecessor.batch_id,
            "pause_id": "pause-" + uuid4().hex,
            "pausing_session_id": pausing_session_id,
            "predecessor_state_head_sha256": predecessor.sha256,
            "active_plan_sha256": successor.active_plan_sha256,
            "project_authority_head_sha256": successor.project_authority_head_sha256,
            "terminal_attempt_inventory_sha256": _terminal_inventory_sha256(successor),
            "pending_interaction_inventory_sha256": _pending_interaction_inventory_sha256(
                successor
            ),
            "scheduler_quiescence_state_head_sha256": scheduler_quiescence_state_head_sha256,
            "all_project_processes_drained": True,
        }
        values["sha256"] = _digest(values)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchQuiescentPauseRecord":
        if not isinstance(value, Mapping):
            raise BatchPauseError("pause record must be a mapping")
        expected = frozenset(item.name for item in fields(cls))
        if set(value) != expected:
            raise BatchPauseError("pause record fields are not closed")
        return cls(**{name: value[name] for name in expected})  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "sha256": self.sha256}


def _validate_imported_pause(record: BatchQuiescentPauseRecord, head: BatchStateHead) -> None:
    """Authenticate the selected source pause before changing its workspace."""
    if (
        head.execution_state is not BatchExecutionState.PAUSED
        or head.quiescent_pause_sha256 != record.sha256
        or record.batch_id != head.batch_id
        or record.predecessor_state_head_sha256 != head.predecessor_head_sha256
        or record.scheduler_quiescence_state_head_sha256 != head.predecessor_head_sha256
        or record.active_plan_sha256 != head.active_plan_sha256
        or record.project_authority_head_sha256 != head.project_authority_head_sha256
        or record.terminal_attempt_inventory_sha256 != _terminal_inventory_sha256(head)
        or record.pending_interaction_inventory_sha256 != _pending_interaction_inventory_sha256(head)
    ):
        raise BatchPauseError("imported pause does not match its selected source head")


def _import_pause_successor(
    record: BatchQuiescentPauseRecord,
    *,
    predecessor: BatchStateHead,
    successor: BatchStateHead,
) -> BatchQuiescentPauseRecord:
    """Describe continued quiescence during an operational import transition.

    These are planned bytes, not authority to publish a pause. Import publication
    additionally requires its live lease and verified no-process drain. The source
    pause proves the scheduler was already stopped; unchanged task and review
    inventories ensure this transition did not process a scientific decision.
    """
    _validate_imported_pause(record, predecessor)
    if (
        successor.batch_id != predecessor.batch_id
        or successor.execution_state is not BatchExecutionState.PAUSED
        or successor.predecessor_head_sha256 != predecessor.sha256
        or _terminal_inventory_sha256(successor) != _terminal_inventory_sha256(predecessor)
        or _pending_interaction_inventory_sha256(successor)
        != _pending_interaction_inventory_sha256(predecessor)
    ):
        raise BatchPauseError("paused import changed the saved task or review inventory")
    values = dict(record._unsigned_mapping())
    values.update(
        predecessor_state_head_sha256=predecessor.sha256,
        scheduler_quiescence_state_head_sha256=predecessor.sha256,
        active_plan_sha256=successor.active_plan_sha256,
        project_authority_head_sha256=successor.project_authority_head_sha256,
    )
    values["pause_id"] = "pause-" + _digest({
        "source_pause_sha256": record.sha256,
        "predecessor_sha256": predecessor.sha256,
        "active_plan_sha256": successor.active_plan_sha256,
        "project_authority_head_sha256": successor.project_authority_head_sha256,
    })
    values["sha256"] = _digest(values)
    return BatchQuiescentPauseRecord(**values)


@dataclass(frozen=True, slots=True)
class PublishedBatchQuiescentPauseRecord:
    record: BatchQuiescentPauseRecord

    @property
    def pause_record_sha256(self) -> str:
        return self.record.sha256


@dataclass(frozen=True, slots=True)
class BatchPauseReceipt:
    batch_id: str
    pause_id: str
    paused_state_head_sha256: str
    pause_record_sha256: str
    pending_interaction_ids: tuple[str, ...]
    safe_to_exit: bool

    def __post_init__(self) -> None:
        _segment(self.batch_id, "batch_id")
        _segment(self.pause_id, "pause_id")
        _sha256(self.paused_state_head_sha256, "paused_state_head_sha256")
        _sha256(self.pause_record_sha256, "pause_record_sha256")
        if type(self.pending_interaction_ids) is not tuple:
            raise BatchPauseError("pause receipt pending interactions must be a tuple")
        try:
            pending = tuple(_batch_request_id(item) for item in self.pending_interaction_ids)
        except BatchStateError as exc:
            raise BatchPauseError("pause receipt pending interaction ID is not portable") from exc
        if len(set(pending)) != len(pending):
            raise BatchPauseError("pause receipt pending interactions must be unique")
        object.__setattr__(self, "pending_interaction_ids", pending)
        if self.safe_to_exit is not True:
            raise BatchPauseError("pause receipt may only report safe_to_exit=True")


def _validate_project_authority(
    *, state_store: BatchStateStore, project_lease: ProjectMutationLease, head: BatchStateHead
) -> None:
    provider = state_store._project_mutation_provider
    if provider is None or project_lease._provider is not provider:
        raise BatchPauseError("pause project lease provider differs from state authority")
    require_project = head.project_materialization_state == "materialized"
    try:
        active = provider._validate_live_lease_authority(
            project_lease,
            require_project=require_project,
        )
    except Exception as exc:
        raise BatchPauseError("pause project lease authority is stale") from exc
    designation = project_lease.designation
    if (
        active.project_identity_sha256 != head.project_identity_sha256
        or designation.identity.project_identity_sha256 != head.project_identity_sha256
        or project_lease.current_authority_head.authority_head_sha256
        != head.project_authority_head_sha256
        or project_lease.current_authority_head.materialization_state
        != head.project_materialization_state
    ):
        raise BatchPauseError("pause project lease differs from current state head")


def _validate_common_inputs(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    scheduler: BatchScheduler,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> BatchStateHead:
    if not isinstance(state_store, BatchStateStore):
        raise BatchPauseError("pause requires an exact BatchStateStore")
    if not isinstance(scheduler, BatchScheduler):
        raise BatchPauseError("pause requires an exact BatchScheduler")
    if not isinstance(workspace_lease, BatchWorkspaceLease):
        raise BatchPauseError("pause requires an exact workspace lease")
    if not isinstance(project_lease, ProjectMutationLease):
        raise BatchPauseError("pause requires an exact project mutation lease")
    if not callable(getattr(project_processes, "assert_all_drained", None)):
        raise BatchPauseError("pause requires a project process registry view")
    try:
        scheduler.validate_pause_authority(state_store=state_store, workspace_lease=workspace_lease)
        state_store.validate_writer_lease(workspace_lease)
        head = state_store.load_head()
    except (BatchSchedulerError, BatchStateError) as exc:
        raise BatchPauseError("pause workspace or scheduler authority is stale") from exc
    if head.batch_id != _segment(batch_id, "batch_id"):
        raise BatchPauseError("pause batch differs from current state head")
    _validate_project_authority(state_store=state_store, project_lease=project_lease, head=head)
    return head


def _assert_project_drain_and_reauthorize(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    scheduler: BatchScheduler,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
    expected_head: BatchStateHead,
) -> BatchStateHead:
    try:
        project_processes.assert_all_drained(
            batch_id=expected_head.batch_id,
            project_identity_sha256=expected_head.project_identity_sha256,
        )
    except Exception as exc:
        raise BatchPauseError("project process drain is incomplete") from exc
    head = _validate_common_inputs(
        state_store=state_store,
        batch_id=batch_id,
        scheduler=scheduler,
        workspace_lease=workspace_lease,
        project_lease=project_lease,
        project_processes=project_processes,
    )
    if head != expected_head:
        raise BatchPauseError("pause state changed during project drain validation")
    return head


_PAUSED_RESPONSE_ISSUER = object()


class _PausedResponseAuthority:
    """One scoped permission to replace an exact paused head with a response head."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchPauseError("paused response authority is owner-issued only")

    def __copy__(self) -> "_PausedResponseAuthority":
        raise BatchPauseError("paused response authority may not be copied")

    def __deepcopy__(self, memo: object) -> "_PausedResponseAuthority":
        del memo
        raise BatchPauseError("paused response authority may not be copied")

    def __reduce__(self) -> object:
        raise BatchPauseError("paused response authority may not be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise BatchPauseError("paused response authority may not be serialized")

    @classmethod
    def _issue(
        cls,
        *,
        state_store: BatchStateStore,
        batch_id: str,
        session_id: str,
        scheduler: BatchScheduler,
        receipt: QuiescenceReceipt,
        workspace_lease: BatchWorkspaceLease,
        project_lease: ProjectMutationLease,
        project_processes: ProjectProcessRegistryView,
        head: BatchStateHead,
    ) -> "_PausedResponseAuthority":
        authority = object.__new__(cls)
        authority._issuer = _PAUSED_RESPONSE_ISSUER
        authority._state_store = state_store
        authority._batch_id = batch_id
        authority._session_id = session_id
        authority._scheduler = scheduler
        authority._receipt = receipt
        authority._workspace_lease = workspace_lease
        authority._project_lease = project_lease
        authority._project_processes = project_processes
        authority._head = head
        authority._open = True
        authority._used = False
        return authority

    def _validate_for_authorize(
        self,
        *,
        state_store: BatchStateStore,
        lease: BatchWorkspaceLease,
        head: BatchStateHead,
    ) -> None:
        if (
            type(self) is not _PausedResponseAuthority
            or self._issuer is not _PAUSED_RESPONSE_ISSUER
            or not self._open
            or self._used
            or self._state_store is not state_store
            or self._workspace_lease is not lease
            or self._head != head
            or head.execution_state is not BatchExecutionState.PAUSED
        ):
            raise BatchPauseError("paused response authority is stale or foreign")
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=self._batch_id,
            scheduler=self._scheduler,
            workspace_lease=lease,
            project_lease=self._project_lease,
            project_processes=self._project_processes,
            expected_head=head,
        )
        try:
            self._scheduler.assert_no_admitted_work()
            self._scheduler.validate_quiescence_receipt(self._receipt)
        except (BatchSchedulerError, BatchStateError) as exc:
            raise BatchPauseError("paused response receipt is stale or invalid") from exc

    def _publish_candidate(self, candidate: BatchStateHead) -> None:
        self._validate_for_authorize(
            state_store=self._state_store,
            lease=self._workspace_lease,
            head=self._head,
        )
        if (
            candidate.execution_state is not BatchExecutionState.PAUSED
            or candidate.predecessor_head_sha256 != self._head.sha256
            or candidate.batch_id != self._head.batch_id
            or candidate.active_plan_sha256 != self._head.active_plan_sha256
            or candidate.project_authority_head_sha256 != self._head.project_authority_head_sha256
            or _terminal_inventory_sha256(candidate) != _terminal_inventory_sha256(self._head)
            or self._receipt.state_head_sha256 != self._head.sha256
        ):
            raise BatchPauseError("paused response successor changes retained authority")
        record = BatchQuiescentPauseRecord.create_response_successor(
            predecessor=self._head,
            successor=candidate,
            pausing_session_id=self._session_id,
            scheduler_quiescence_state_head_sha256=self._receipt.state_head_sha256,
        )
        if (
            record.predecessor_state_head_sha256 != self._head.sha256
            or record.scheduler_quiescence_state_head_sha256 != self._receipt.state_head_sha256
            or record.pending_interaction_inventory_sha256
            != _pending_interaction_inventory_sha256(candidate)
        ):
            raise BatchPauseError("paused response record differs from successor")
        store = self._state_store
        lease = self._workspace_lease
        published = store._publish_quiescent_pause_record(
            record,
            lease,
            _issue_state_authority(
                state_store=store,
                workspace_lease=lease,
                operation="publish",
                pause_record_sha256=record.sha256,
                expected_head_sha256=self._head.sha256,
            ),
        )
        if published is not record:
            raise BatchPauseError("state store substituted pause record publication")
        successor = self._head.evolve(
            pending_authorization_sha256_by_node=(candidate.pending_authorization_sha256_by_node),
            pending_interaction_ids=candidate.pending_interaction_ids,
            invalidated_node_ids=candidate.invalidated_node_ids,
            quiescent_pause_sha256=record.sha256,
        )
        self._validate_for_authorize(
            state_store=store,
            lease=lease,
            head=self._head,
        )
        try:
            receipt_capability = self._scheduler._consume_quiescence_receipt_for_pause(
                self._receipt
            )
        except (BatchSchedulerError, BatchStateError) as exc:
            raise BatchPauseError("paused response receipt is stale or invalid") from exc
        self._used = True
        store._replace_paused_response_head_cas(
            successor,
            self._head.sha256,
            lease,
            authority=_issue_state_authority(
                state_store=store,
                workspace_lease=lease,
                operation="response",
                pause_record_sha256=record.sha256,
                expected_head_sha256=self._head.sha256,
                scheduler_receipt_capability=receipt_capability,
                candidate_head_sha256=successor.sha256,
            ),
        )


@contextmanager
def paused_response_authority(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    session_id: str,
    scheduler: BatchScheduler,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> Iterator[_PausedResponseAuthority]:
    """Retain the project lifecycle lock while one paused response is staged."""

    if type(project_lease) is not ProjectMutationLease:
        raise BatchPauseError("paused response requires an exact project lease")
    with project_lease._lifecycle_lock:
        head = _validate_common_inputs(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
        )
        if head.execution_state is not BatchExecutionState.PAUSED:
            raise BatchPauseError("paused response requires a paused state head")
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            expected_head=head,
        )
        try:
            scheduler.assert_no_admitted_work()
            receipt = scheduler.quiesce()
            scheduler.validate_quiescence_receipt(receipt)
        except (BatchSchedulerError, BatchStateError) as exc:
            raise BatchPauseError("paused response cannot quiesce scheduler") from exc
        if receipt.state_head_sha256 != head.sha256:
            raise BatchPauseError("paused response receipt differs from current head")
        authority = _PausedResponseAuthority._issue(
            state_store=state_store,
            batch_id=batch_id,
            session_id=_segment(session_id, "session_id"),
            scheduler=scheduler,
            receipt=receipt,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            head=head,
        )
        try:
            yield authority
        finally:
            authority._open = False


def _validate_record_for_head(record: BatchQuiescentPauseRecord, head: BatchStateHead) -> None:
    if (
        record.batch_id != head.batch_id
        or record.predecessor_state_head_sha256 != head.sha256
        or record.active_plan_sha256 != head.active_plan_sha256
        or record.project_authority_head_sha256 != head.project_authority_head_sha256
        or record.terminal_attempt_inventory_sha256 != _terminal_inventory_sha256(head)
        or record.pending_interaction_inventory_sha256
        != _pending_interaction_inventory_sha256(head)
    ):
        raise BatchPauseError("pause record does not bind the exact current head")


def publish_quiescent_pause(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    pausing_session_id: str,
    scheduler: BatchScheduler,
    quiescence_receipt: QuiescenceReceipt,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> BatchPauseReceipt:
    """Retain exact project authority through durable pause publication."""

    if type(project_lease) is not ProjectMutationLease:
        raise BatchPauseError("pause requires an exact project mutation lease")
    with project_lease._lifecycle_lock:
        return _publish_quiescent_pause_locked(
            state_store=state_store,
            batch_id=batch_id,
            pausing_session_id=pausing_session_id,
            scheduler=scheduler,
            quiescence_receipt=quiescence_receipt,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
        )


def _publish_quiescent_pause_locked(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    pausing_session_id: str,
    scheduler: BatchScheduler,
    quiescence_receipt: QuiescenceReceipt,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> BatchPauseReceipt:
    """Persist an authenticated quiescent proof and atomically pause its head."""

    head = _validate_common_inputs(
        state_store=state_store,
        batch_id=batch_id,
        scheduler=scheduler,
        workspace_lease=workspace_lease,
        project_lease=project_lease,
        project_processes=project_processes,
    )
    try:
        scheduler.validate_quiescence_receipt(quiescence_receipt)
    except (BatchSchedulerError, BatchStateError) as exc:
        raise BatchPauseError("pause quiescence proof is stale or incomplete") from exc
    if head.execution_state is not BatchExecutionState.ACTIVE:
        raise BatchPauseError("only an active batch head may be paused")
    record = BatchQuiescentPauseRecord.create(
        batch_id=head.batch_id,
        pausing_session_id=pausing_session_id,
        head=head,
        scheduler_quiescence_state_head_sha256=quiescence_receipt.state_head_sha256,
    )
    _validate_record_for_head(record, head)
    try:
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            expected_head=head,
        )
        published = state_store._publish_quiescent_pause_record(
            record,
            workspace_lease,
            _issue_state_authority(
                state_store=state_store,
                workspace_lease=workspace_lease,
                operation="publish",
                pause_record_sha256=record.sha256,
                expected_head_sha256=head.sha256,
            ),
        )
        if published is not record:
            raise BatchPauseError("state store substituted pause record publication")
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            expected_head=head,
        )
        receipt_capability = scheduler._consume_quiescence_receipt_for_pause(quiescence_receipt)
        paused = state_store._transition_to_quiescent_pause(
            pause_record_sha256=record.sha256,
            expected_head_sha256=head.sha256,
            lease=workspace_lease,
            authority=_issue_state_authority(
                state_store=state_store,
                workspace_lease=workspace_lease,
                operation="pause",
                pause_record_sha256=record.sha256,
                expected_head_sha256=head.sha256,
                scheduler_receipt_capability=receipt_capability,
            ),
        )
        reopened = state_store.load_head()
        reopened_record = state_store.load_quiescent_pause_record(record.sha256)
    except (BatchHeadConflictError, BatchStateError) as exc:
        raise BatchPauseError("pause state head changed before publication") from exc
    if (
        reopened != paused
        or reopened.execution_state is not BatchExecutionState.PAUSED
        or reopened.quiescent_pause_sha256 != record.sha256
        or reopened_record != record
    ):
        raise BatchPauseError("persisted pause proof did not reopen exactly")
    _validate_project_authority(state_store=state_store, project_lease=project_lease, head=reopened)
    return BatchPauseReceipt(
        batch_id=head.batch_id,
        pause_id=record.pause_id,
        paused_state_head_sha256=reopened.sha256,
        pause_record_sha256=record.sha256,
        pending_interaction_ids=reopened.pending_interaction_ids,
        safe_to_exit=True,
    )


def consume_quiescent_pause(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    pause_record_sha256: str,
    scheduler: BatchScheduler,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> BatchStateHead:
    """Retain exact project authority through durable pause consumption."""

    if type(project_lease) is not ProjectMutationLease:
        raise BatchPauseError("pause requires an exact project mutation lease")
    with project_lease._lifecycle_lock:
        return _consume_quiescent_pause_locked(
            state_store=state_store,
            batch_id=batch_id,
            pause_record_sha256=pause_record_sha256,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
        )


def _consume_quiescent_pause_locked(
    *,
    state_store: BatchStateStore,
    batch_id: str,
    pause_record_sha256: str,
    scheduler: BatchScheduler,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
    project_processes: ProjectProcessRegistryView,
) -> BatchStateHead:
    """Consume one exact pause proof before a fresh scheduler can dispatch."""

    head = _validate_common_inputs(
        state_store=state_store,
        batch_id=batch_id,
        scheduler=scheduler,
        workspace_lease=workspace_lease,
        project_lease=project_lease,
        project_processes=project_processes,
    )
    digest = _sha256(pause_record_sha256, "pause_record_sha256")
    if (
        head.execution_state is not BatchExecutionState.PAUSED
        or head.quiescent_pause_sha256 != digest
    ):
        raise BatchPauseError("current state does not name the supplied pause proof")
    try:
        record = state_store.load_quiescent_pause_record(digest)
        scheduler.assert_no_admitted_work()
    except (BatchSchedulerError, BatchStateError) as exc:
        raise BatchPauseError("pause proof cannot be safely consumed") from exc
    if (
        record.batch_id != head.batch_id
        or record.predecessor_state_head_sha256 != head.predecessor_head_sha256
        or record.scheduler_quiescence_state_head_sha256 != head.predecessor_head_sha256
        or record.active_plan_sha256 != head.active_plan_sha256
        or record.project_authority_head_sha256 != head.project_authority_head_sha256
        or record.terminal_attempt_inventory_sha256 != _terminal_inventory_sha256(head)
        or record.pending_interaction_inventory_sha256
        != _pending_interaction_inventory_sha256(head)
    ):
        raise BatchPauseError("pause record predecessor differs from paused head")
    try:
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            expected_head=head,
        )
        scheduler.assert_no_admitted_work()
        _assert_project_drain_and_reauthorize(
            state_store=state_store,
            batch_id=batch_id,
            scheduler=scheduler,
            workspace_lease=workspace_lease,
            project_lease=project_lease,
            project_processes=project_processes,
            expected_head=head,
        )
        scheduler.assert_no_admitted_work()
        active = state_store._consume_quiescent_pause(
            pause_record_sha256=digest,
            expected_head_sha256=head.sha256,
            lease=workspace_lease,
            authority=_issue_state_authority(
                state_store=state_store,
                workspace_lease=workspace_lease,
                operation="consume",
                pause_record_sha256=digest,
                expected_head_sha256=head.sha256,
            ),
        )
    except (BatchHeadConflictError, BatchStateError) as exc:
        raise BatchPauseError("pause proof is stale or already consumed") from exc
    if (
        active.execution_state is not BatchExecutionState.ACTIVE
        or active.quiescent_pause_sha256 is not None
    ):
        raise BatchPauseError("pause consumption did not restore an active head")
    _validate_project_authority(state_store=state_store, project_lease=project_lease, head=active)
    return active


__all__ = [
    "BatchExecutionState",
    "BatchPauseError",
    "BatchPauseReceipt",
    "BatchQuiescentPauseRecord",
    "ProjectProcessRegistryView",
    "PublishedBatchQuiescentPauseRecord",
    "consume_quiescent_pause",
    "paused_response_authority",
    "publish_quiescent_pause",
]
