"""Bounded, presentation-only projection of immutable batch status.

The `BatchRunView` record carries `batch_id`, `state_head_sha256`, `resume_disposition`,
`summary_table`, `domain_table` and its remaining declared fields.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from mif_pipeline.orchestration.batch_workflow import BatchWorkflowNode
import re
from typing import Literal, TypeAlias, cast

from mif_pipeline.gui.interactions import FormTable, FormTableRow
from mif_pipeline.orchestration.batch_service_models import (
    BatchStatusSnapshot,
    BatchSummaryRow,
)

MAX_VISIBLE_LOG_LINES = 500

ResumeDisposition: TypeAlias = Literal[
    "completed",
    "paused_resumable",
    "active_current_session",
    "non_resumable",
]

_RESUME_DISPOSITIONS = frozenset(
    {
        "completed",
        "paused_resumable",
        "active_current_session",
        "non_resumable",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

SUMMARY_COLUMNS = (
    "batch_id",
    "sample_id",
    "slide_id",
    "subject_id",
    "status",
    "current_or_final_stage",
    "progress_kind",
    "node_status_inventory_sha256",
    "domain_status_inventory_sha256",
    "processed_image_path",
    "qupath_project_entry",
    "pending_interaction_reason",
    "failure_reason",
    "resolved_config_hash",
    "provenance_hash",
)
NODE_STATUS_COLUMNS = (
    "node_id",
    "status",
)
DOMAIN_TASK_STATUS_COLUMNS = (
    "domain_task_id",
    "status",
)


# ==============================================================================
#                                      Run
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchRunView:
    """Finite public status evidence rendered by the Run page."""

    batch_id: str
    state_head_sha256: str
    resume_disposition: ResumeDisposition
    summary_table: FormTable
    domain_table: FormTable
    node_table: FormTable
    completed_count: int
    interaction_count: int
    failure_count: int
    active_operation: str | None
    log_lines: tuple[str, ...]
    workflow_nodes: tuple[BatchWorkflowNode, ...] = ()
    continuation_available: bool = False
    resource_wait_messages: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.workflow_nodes) is not tuple or any(
            type(n) is not BatchWorkflowNode for n in self.workflow_nodes
        ):
            raise TypeError("run workflow must contain immutable workflow records")
        if not isinstance(self.resource_wait_messages, Mapping) or not all(
            type(key) is str and type(message) is str for key, message in self.resource_wait_messages.items()
        ):
            raise TypeError("run memory wait messages must be keyed by their task")
        object.__setattr__(self, "resource_wait_messages", MappingProxyType(dict(self.resource_wait_messages)))
        if type(self.continuation_available) is not bool:
            raise TypeError("saved-work continuation must be a bool")
        _required_text(self.batch_id, label="batch run view batch_id")
        _sha256(self.state_head_sha256, label="batch run view state head")
        if (
            type(self.resume_disposition) is not str
            or self.resume_disposition not in _RESUME_DISPOSITIONS
        ):
            raise ValueError("batch run view resume disposition is invalid")
        if not all(
            type(table) is FormTable
            for table in (self.summary_table, self.domain_table, self.node_table)
        ):
            raise TypeError("batch run view tables must be FormTable values")
        for label, count in (
            ("completed", self.completed_count),
            ("interaction", self.interaction_count),
            ("failure", self.failure_count),
        ):
            if type(count) is not int or count < 0:
                raise ValueError(f"{label} count must be a non-negative integer")
        _optional_text(self.active_operation, label="active operation")
        if type(self.log_lines) is not tuple or not all(
            type(line) is str for line in self.log_lines
        ):
            raise TypeError("batch run logs must be an immutable text tuple")
        if len(self.log_lines) > MAX_VISIBLE_LOG_LINES:
            raise ValueError("batch run logs exceed the visible bound")


def _required_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label=label)


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256")
    return value


def _text_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    result = tuple(_required_text(item, label=label) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _summary_table(rows: tuple[BatchSummaryRow, ...], *, batch_id: str) -> FormTable:
    projected: list[FormTableRow] = []
    for row in rows:
        if type(row) is not BatchSummaryRow:
            raise TypeError("batch summary row must be a BatchSummaryRow")
        if row.batch_id != batch_id:
            raise ValueError("batch summary row belongs to another batch")
        projected.append(
            FormTableRow(
                f"sample:{row.sample_id}",
                tuple(
                    (
                        str(row.processed_image_path)
                        if name == "processed_image_path"
                        and row.processed_image_path is not None
                        else getattr(row, name)
                    )
                    for name in SUMMARY_COLUMNS
                ),
            )
        )
    return FormTable("summary", SUMMARY_COLUMNS, tuple(projected))


def _status_table(
    statuses: Mapping[str, str],
    *,
    table_id: str,
    columns: tuple[str, ...],
) -> FormTable:
    return FormTable(
        table_id,
        columns,
        tuple(
            FormTableRow(f"{table_id}:{key}", (key, value))
            for key, value in sorted(statuses.items())
        ),
    )


def build_batch_run_view(
    status: BatchStatusSnapshot,
    *,
    active_operation: str | None,
    log_lines: Sequence[str],
) -> BatchRunView:
    """Project supplied public state without querying state, logs, or services.

    Current workflow states take precedence over historical attempt statuses in
    the node table. Rows without a workflow projection retain their supplied
    status; sample failure counts come from the current summary rows.
    """

    if type(status) is not BatchStatusSnapshot:
        raise TypeError("batch status must be a BatchStatusSnapshot")
    batch_id = _required_text(status.batch_id, label="status batch_id")
    state_head = _sha256(status.state_head_sha256, label="status state head")
    disposition = status.resume_disposition
    if type(disposition) is not str or disposition not in _RESUME_DISPOSITIONS:
        raise ValueError("status resume disposition is invalid")
    pause_digest = status.quiescent_pause_sha256
    if pause_digest is not None:
        _sha256(pause_digest, label="status quiescent pause")

    summary_rows = status.summary_rows
    pending_ids = _text_tuple(
        status.pending_interaction_ids,
        label="pending interaction IDs",
    )
    active = _optional_text(active_operation, label="active operation")
    if isinstance(log_lines, (str, bytes)) or not isinstance(log_lines, Sequence):
        raise TypeError("run log lines must be a text sequence")
    if not all(type(line) is str for line in log_lines):
        raise TypeError("run log lines must contain only text")
    bounded_logs = tuple(log_lines[-MAX_VISIBLE_LOG_LINES:])

    summary_table = _summary_table(summary_rows, batch_id=batch_id)
    node_statuses = dict(status.node_status_by_id)
    node_statuses.update(
        (node.node_id, node.status) for node in status.workflow_nodes if not node.deferred
    )
    return BatchRunView(
        batch_id=batch_id,
        state_head_sha256=state_head,
        resume_disposition=cast(ResumeDisposition, disposition),
        summary_table=summary_table,
        domain_table=_status_table(
            status.domain_status_by_id,
            table_id="domains",
            columns=DOMAIN_TASK_STATUS_COLUMNS,
        ),
        node_table=_status_table(
            node_statuses,
            table_id="nodes",
            columns=NODE_STATUS_COLUMNS,
        ),
        completed_count=sum(row.status == "completed" for row in summary_rows),
        interaction_count=len(pending_ids),
        failure_count=sum(row.failure_reason is not None for row in summary_rows),
        active_operation=active,
        log_lines=bounded_logs,
        workflow_nodes=status.workflow_nodes,
        continuation_available=status.continuation_available,
        resource_wait_messages=status.resource_wait_messages,
    )


__all__ = [
    "BatchRunView",
    "DOMAIN_TASK_STATUS_COLUMNS",
    "MAX_VISIBLE_LOG_LINES",
    "NODE_STATUS_COLUMNS",
    "ResumeDisposition",
    "SUMMARY_COLUMNS",
    "build_batch_run_view",
]
