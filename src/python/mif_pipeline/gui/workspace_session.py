"""Non-serialized presentation state for one desktop batch workspace.

The `WorkspaceSession` record carries `manifest_path`, `output_root`, `batch_id`, `service`,
`last_plan` and its remaining declared fields.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Protocol

from .desktop_services import BatchServicePort

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESUME_DISPOSITIONS = {
    "completed",
    "paused_resumable",
    "active_current_session",
    "non_resumable",
}


# ==============================================================================
#                               Workspace session
# ==============================================================================


class BatchPauseReceiptView(Protocol):
    batch_id: str
    pause_id: str
    paused_state_head_sha256: str
    pause_record_sha256: str
    pending_interaction_ids: tuple[str, ...]
    safe_to_exit: bool


class BatchStatusSnapshotView(Protocol):
    batch_id: str
    state_head_sha256: str
    resume_disposition: str
    quiescent_pause_sha256: str | None


class SessionExitState(str, Enum):
    """Close authority derived only from current typed service evidence."""

    MUTABLE = "mutable"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETED = "completed"
    DIAGNOSTIC_ONLY = "diagnostic_only"


def _absolute_path(value: Path, *, label: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    return value.expanduser().resolve(strict=False)


def _nonempty_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _canonical_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256")
    return value


def _attribute(value: object, name: str, *, label: str) -> object:
    try:
        return getattr(value, name)
    except (AttributeError, TypeError) as exc:
        raise TypeError(f"{label} lacks required field {name}") from exc


def _interaction_ids(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError("pause pending_interaction_ids must be a tuple")
    items = tuple(_nonempty_text(item, label="pending interaction ID") for item in value)
    if len(set(items)) != len(items):
        raise ValueError("pause pending_interaction_ids must be unique")
    return items


@dataclass(slots=True)
class WorkspaceSession:
    """Own one provider-issued service and ephemeral desktop authority state."""

    manifest_path: Path | None
    output_root: Path
    batch_id: str
    service: BatchServicePort
    last_plan: object | None = None
    last_status: BatchStatusSnapshotView | None = None
    last_interactions: object | None = None
    last_pause_receipt: BatchPauseReceiptView | None = None
    current_state_head_sha256: str | None = None
    pending_pause_operation_id: str | None = None
    pending_status_operation_id: str | None = None
    exit_state: SessionExitState = SessionExitState.MUTABLE
    _close_authorized: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.manifest_path is not None:
            self.manifest_path = _absolute_path(
                self.manifest_path,
                label="manifest_path",
            )
        self.output_root = _absolute_path(self.output_root, label="output_root")
        self.batch_id = _nonempty_text(self.batch_id, label="batch_id")
        if self.service is None:
            raise TypeError("service must be a provider-issued BatchServicePort")
        if type(self.exit_state) is not SessionExitState:
            raise TypeError("exit_state must be a SessionExitState")

    def begin_pause(self, operation_id: str) -> None:
        """Register the sole pause result that may next authorize closing."""

        operation = _nonempty_text(operation_id, label="pause operation ID")
        self.pending_pause_operation_id = operation
        self.pending_status_operation_id = None
        self.last_pause_receipt = None
        self._close_authorized = False
        self.exit_state = SessionExitState.PAUSING

    def record_pause(
        self,
        operation_id: str,
        receipt: BatchPauseReceiptView,
    ) -> None:
        """Accept only the current exact quiescent-pause receipt."""

        operation = _nonempty_text(operation_id, label="pause operation ID")
        if operation != self.pending_pause_operation_id:
            raise ValueError("pause result does not match the pending operation")
        batch_id = _nonempty_text(
            _attribute(receipt, "batch_id", label="pause receipt"),
            label="pause receipt batch_id",
        )
        if batch_id != self.batch_id:
            raise ValueError("pause receipt belongs to another batch")
        _nonempty_text(
            _attribute(receipt, "pause_id", label="pause receipt"),
            label="pause receipt pause_id",
        )
        head = _canonical_sha256(
            _attribute(receipt, "paused_state_head_sha256", label="pause receipt"),
            label="paused state head",
        )
        _canonical_sha256(
            _attribute(receipt, "pause_record_sha256", label="pause receipt"),
            label="pause record",
        )
        _interaction_ids(_attribute(receipt, "pending_interaction_ids", label="pause receipt"))
        if _attribute(receipt, "safe_to_exit", label="pause receipt") is not True:
            raise ValueError("pause receipt does not explicitly permit exit")
        self.last_pause_receipt = receipt
        self.current_state_head_sha256 = head
        self.pending_pause_operation_id = None
        self.exit_state = SessionExitState.PAUSED
        self._close_authorized = True

    def begin_status(self, operation_id: str) -> None:
        """Register the sole status result that may next update current state."""

        self.pending_status_operation_id = _nonempty_text(
            operation_id,
            label="status operation ID",
        )

    def record_status(
        self,
        operation_id: str,
        snapshot: BatchStatusSnapshotView,
    ) -> None:
        """Accept one current typed status without inferring from summary rows."""

        operation = _nonempty_text(operation_id, label="status operation ID")
        if operation != self.pending_status_operation_id:
            raise ValueError("status result does not match the pending operation")
        batch_id = _nonempty_text(
            _attribute(snapshot, "batch_id", label="status snapshot"),
            label="status batch_id",
        )
        if batch_id != self.batch_id:
            raise ValueError("status snapshot belongs to another batch")
        head = _canonical_sha256(
            _attribute(snapshot, "state_head_sha256", label="status snapshot"),
            label="status state head",
        )
        disposition = _attribute(
            snapshot,
            "resume_disposition",
            label="status snapshot",
        )
        if type(disposition) is not str or disposition not in _RESUME_DISPOSITIONS:
            raise ValueError("status resume_disposition is invalid")
        pause_digest = _attribute(
            snapshot,
            "quiescent_pause_sha256",
            label="status snapshot",
        )
        if pause_digest is not None:
            _canonical_sha256(pause_digest, label="status quiescent pause")
        if disposition == "paused_resumable" and pause_digest is None:
            raise ValueError("paused status requires a quiescent pause digest")

        self.last_status = snapshot
        self.current_state_head_sha256 = head
        self.pending_status_operation_id = None
        if disposition == "completed":
            self.exit_state = SessionExitState.COMPLETED
            self._close_authorized = True
        elif disposition == "paused_resumable":
            self.exit_state = SessionExitState.PAUSED
            self._close_authorized = True
        else:
            self.exit_state = SessionExitState.MUTABLE
            self._close_authorized = False

    def begin_mutation(self) -> None:
        """Invalidate every close authorization before submitting mutation."""

        self.last_pause_receipt = None
        self.last_status = None
        self.pending_pause_operation_id = None
        self.pending_status_operation_id = None
        self.exit_state = SessionExitState.MUTABLE
        self._close_authorized = False

    def mark_diagnostic_only(self) -> None:
        """Mark an explicitly non-mutable workspace safe to leave."""

        self.last_pause_receipt = None
        self.pending_pause_operation_id = None
        self.pending_status_operation_id = None
        self.exit_state = SessionExitState.DIAGNOSTIC_ONLY
        self._close_authorized = True

    def may_close_immediately(self) -> bool:
        """Return close authority without inspecting service-private state."""

        return self._close_authorized and self.exit_state in {
            SessionExitState.PAUSED,
            SessionExitState.COMPLETED,
            SessionExitState.DIAGNOSTIC_ONLY,
        }


__all__ = [
    "BatchPauseReceiptView",
    "BatchStatusSnapshotView",
    "SessionExitState",
    "WorkspaceSession",
]
