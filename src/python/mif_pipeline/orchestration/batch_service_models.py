"""Immutable authority-free projections returned by the batch service.

The `BatchSummaryRow` record carries analysis and sample identities, status,
progress, and result locations.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .batch_artifacts import BatchArtifactRef
from .batch_workflow import BatchWorkflowNode
from .setup.batch_prerequisites import BatchPrerequisiteItem

_RUN_OUTCOMES = frozenset({"completed", "paused", "needs_interaction", "failed", "incomplete"})
_PROGRESS_KINDS = frozenset({"terminal", "uniform_next", "mixed", "no_ready_work"})
_EXECUTION_STATES = frozenset({"active", "paused"})
_RESUME_DISPOSITIONS = frozenset(
    {"completed", "paused_resumable", "active_current_session", "non_resumable"}
)
_HEX = frozenset("0123456789abcdef")


# ==============================================================================
#                              Batch service models
# ==============================================================================


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _optional_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field)


def _literal(value: object, allowed: frozenset[str], field: str) -> str:
    text = _text(value, field)
    if text not in allowed:
        raise ValueError(f"{field} is unsupported")
    return text


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence of strings")
    parsed = tuple(_text(item, f"{field} item") for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} contains duplicate IDs")
    return parsed


def _string_mapping(value: object, field: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    parsed: dict[str, str] = {}
    for key, item in value.items():
        parsed[_text(key, f"{field} key")] = _text(item, f"{field} value")
    return MappingProxyType(dict(sorted(parsed.items())))


def _tuple_mapping(value: object, field: str) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    parsed: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        parsed[_text(key, f"{field} key")] = _string_tuple(item, f"{field}[{key!r}]")
    return MappingProxyType(dict(sorted(parsed.items())))


def _summary_rows(value: object, batch_id: str) -> tuple["BatchSummaryRow", ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("summary_rows must be a sequence")
    rows = tuple(value)
    if not all(type(row) is BatchSummaryRow for row in rows):
        raise TypeError("summary_rows must contain BatchSummaryRow values")
    if any(row.batch_id != batch_id for row in rows):
        raise ValueError("summary_rows contain a foreign batch")
    identities = tuple((row.sample_id, row.slide_id) for row in rows)
    if len(set(identities)) != len(identities):
        raise ValueError("summary_rows contain duplicate sample/slide identities")
    return rows


def _prerequisites(value: object) -> tuple[BatchPrerequisiteItem, ...]:
    if type(value) is not tuple or not all(type(row) is BatchPrerequisiteItem for row in value):
        raise TypeError("prerequisites must be an immutable tuple of typed rows")
    for row in value:
        replace(row)
    identities = tuple(row.prerequisite_id for row in value)
    if identities != tuple(sorted(set(identities))):
        raise ValueError("prerequisites must have sorted unique IDs")
    return value


@dataclass(frozen=True, slots=True)
class BatchReviewPlot:
    """One complete derived figure, valid for the current provider lifetime."""

    artifact_id: str
    title: str
    path: Path
    sha256: str
    status: Literal["available"]
    display_options: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _text(self.artifact_id, "artifact_id")
        _text(self.title, "title")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("review plot path must be an absolute local path")
        _sha256(self.sha256, "sha256")
        _literal(self.status, frozenset({"available"}), "status")


@dataclass(frozen=True, slots=True)
class BatchSavedStageFigure:
    """One view that can be drawn from this result's saved scientific records."""

    figure_id: str
    title: str
    detail: str
    settings: Mapping[str, object] | None = None
    data_formats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("figure_id", "title", "detail"):
            _text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class BatchSavedStageResult:
    """A currently selected completed stage, independent of final export readiness."""

    artifact_ref_sha256: str
    stage_kind: str
    title: str
    sample_ids: tuple[str, ...]
    path: Path | None
    figures: tuple[BatchSavedStageFigure, ...]
    message: str
    reference_display: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _sha256(self.artifact_ref_sha256, "artifact_ref_sha256")
        for name in ("stage_kind", "title"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "sample_ids", _string_tuple(self.sample_ids, "sample_ids"))
        if self.path is not None and (not isinstance(self.path, Path) or not self.path.is_absolute()):
            raise ValueError("saved result location must be an absolute path")
        if type(self.figures) is not tuple or any(type(item) is not BatchSavedStageFigure for item in self.figures):
            raise TypeError("saved figures must be a tuple of figure choices")
        if len({item.figure_id for item in self.figures}) != len(self.figures):
            raise ValueError("saved figure choices must have unique IDs")
        if self.reference_display is not None:
            object.__setattr__(self, "reference_display", MappingProxyType(dict(self.reference_display)))


@dataclass(frozen=True, slots=True)
class BatchSavedStageResults:
    """Read-only current stage inventory bound to one saved analysis head."""

    batch_id: str
    state_head_sha256: str
    stages: tuple[BatchSavedStageResult, ...]

    def __post_init__(self) -> None:
        _text(self.batch_id, "batch_id")
        _sha256(self.state_head_sha256, "state_head_sha256")
        if type(self.stages) is not tuple or any(type(item) is not BatchSavedStageResult for item in self.stages):
            raise TypeError("saved stages must be a tuple of completed result rows")
        if len({item.artifact_ref_sha256 for item in self.stages}) != len(self.stages):
            raise ValueError("saved stage rows must have unique artifact identities")


@dataclass(frozen=True, slots=True)
class BatchReviewPlots:
    """Presentation files bound to one current review; never approval."""

    target_id: str
    interaction_request_id: str
    state_head_sha256: str
    interaction_context_sha256: str
    plots: tuple[BatchReviewPlot, ...]

    def __post_init__(self) -> None:
        _text(self.target_id, "target_id")
        _text(self.interaction_request_id, "interaction_request_id")
        _sha256(self.state_head_sha256, "state_head_sha256")
        _sha256(self.interaction_context_sha256, "interaction_context_sha256")
        if type(self.plots) is not tuple or any(
            type(plot) is not BatchReviewPlot for plot in self.plots
        ):
            raise TypeError("review plots must be an immutable tuple of typed figures")
        if len({plot.artifact_id for plot in self.plots}) != len(self.plots):
            raise ValueError("review plots contain duplicate figure IDs")


@dataclass(frozen=True, slots=True)
class BatchSummaryRow:
    batch_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    status: str
    current_or_final_stage: str
    progress_kind: Literal["terminal", "uniform_next", "mixed", "no_ready_work"]
    node_status_inventory_sha256: str
    domain_status_inventory_sha256: str
    processed_image_path: Path | None
    qupath_project_entry: str | None
    pending_interaction_reason: str | None
    failure_reason: str | None
    resolved_config_hash: str
    provenance_hash: str | None

    def __post_init__(self) -> None:
        for field in (
            "batch_id",
            "sample_id",
            "slide_id",
            "subject_id",
            "status",
            "current_or_final_stage",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self, "progress_kind", _literal(self.progress_kind, _PROGRESS_KINDS, "progress_kind")
        )
        object.__setattr__(
            self,
            "node_status_inventory_sha256",
            _sha256(self.node_status_inventory_sha256, "node_status_inventory_sha256"),
        )
        object.__setattr__(
            self,
            "domain_status_inventory_sha256",
            _sha256(self.domain_status_inventory_sha256, "domain_status_inventory_sha256"),
        )
        if self.processed_image_path is not None and not isinstance(
            self.processed_image_path, Path
        ):
            raise TypeError("processed_image_path must be a Path or None")
        for field in ("qupath_project_entry", "pending_interaction_reason", "failure_reason"):
            object.__setattr__(self, field, _optional_text(getattr(self, field), field))
        object.__setattr__(
            self, "resolved_config_hash", _sha256(self.resolved_config_hash, "resolved_config_hash")
        )
        object.__setattr__(
            self, "provenance_hash", _optional_sha256(self.provenance_hash, "provenance_hash")
        )


@dataclass(frozen=True, slots=True)
class BatchRunReport:
    batch_id: str
    outcome: Literal["completed", "paused", "needs_interaction", "failed", "incomplete"]
    state_head_sha256: str
    active_plan_sha256: str
    committed_node_ids: tuple[str, ...]
    pending_interaction_ids: tuple[str, ...]
    summary_rows: tuple[BatchSummaryRow, ...]
    prerequisites: tuple[BatchPrerequisiteItem, ...] = ()

    def __post_init__(self) -> None:
        batch_id = _text(self.batch_id, "batch_id")
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "outcome", _literal(self.outcome, _RUN_OUTCOMES, "outcome"))
        object.__setattr__(
            self,
            "state_head_sha256",
            _sha256(self.state_head_sha256, "state_head_sha256"),
        )
        object.__setattr__(
            self,
            "active_plan_sha256",
            _sha256(self.active_plan_sha256, "active_plan_sha256"),
        )
        object.__setattr__(
            self,
            "committed_node_ids",
            _string_tuple(self.committed_node_ids, "committed_node_ids"),
        )
        object.__setattr__(
            self,
            "pending_interaction_ids",
            _string_tuple(self.pending_interaction_ids, "pending_interaction_ids"),
        )
        object.__setattr__(self, "summary_rows", _summary_rows(self.summary_rows, batch_id))
        object.__setattr__(self, "prerequisites", _prerequisites(self.prerequisites))


@dataclass(frozen=True, slots=True)
class BatchStatusSnapshot:
    batch_id: str
    state_head_sha256: str
    active_plan_sha256: str
    execution_state: Literal["active", "paused"]
    resume_disposition: Literal[
        "completed", "paused_resumable", "active_current_session", "non_resumable"
    ]
    quiescent_pause_sha256: str | None
    node_status_by_id: Mapping[str, str]
    domain_status_by_id: Mapping[str, str]
    last_durable_node_ids_by_sample: Mapping[str, tuple[str, ...]]
    next_eligible_node_ids_by_sample: Mapping[str, tuple[str, ...]]
    pending_interaction_ids: tuple[str, ...]
    summary_rows: tuple[BatchSummaryRow, ...]
    prerequisites: tuple[BatchPrerequisiteItem, ...] = ()
    workflow_nodes: tuple[BatchWorkflowNode, ...] = ()
    continuation_available: bool = False
    resource_wait_messages: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the BatchStatusSnapshot fields when the record is constructed.

        The checks cover ``batch_id``, ``workflow_nodes``, ``state_head_sha256``,
        ``active_plan_sha256``, ``execution_state``, ``resume_disposition``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if type(self.workflow_nodes) is not tuple or any(
            type(node) is not BatchWorkflowNode for node in self.workflow_nodes
        ):
            raise TypeError("workflow_nodes must be an immutable tuple of workflow records")
        if len({node.node_id for node in self.workflow_nodes}) != len(self.workflow_nodes):
            raise ValueError("workflow nodes must have unique IDs")
        object.__setattr__(self, "resource_wait_messages", _string_mapping(self.resource_wait_messages, "resource wait messages"))
        if type(self.continuation_available) is not bool or (
            self.continuation_available and (
                self.execution_state != "active"
                or self.resume_disposition != "non_resumable"
            )
        ):
            raise ValueError("saved-work continuation requires an idle active analysis")
        batch_id = _text(self.batch_id, "batch_id")
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(
            self,
            "state_head_sha256",
            _sha256(self.state_head_sha256, "state_head_sha256"),
        )
        object.__setattr__(
            self,
            "active_plan_sha256",
            _sha256(self.active_plan_sha256, "active_plan_sha256"),
        )
        object.__setattr__(
            self,
            "execution_state",
            _literal(self.execution_state, _EXECUTION_STATES, "execution_state"),
        )
        object.__setattr__(
            self,
            "resume_disposition",
            _literal(
                self.resume_disposition,
                _RESUME_DISPOSITIONS,
                "resume_disposition",
            ),
        )
        object.__setattr__(
            self,
            "quiescent_pause_sha256",
            _optional_sha256(self.quiescent_pause_sha256, "quiescent_pause_sha256"),
        )
        object.__setattr__(
            self,
            "node_status_by_id",
            _string_mapping(self.node_status_by_id, "node_status_by_id"),
        )
        object.__setattr__(
            self,
            "domain_status_by_id",
            _string_mapping(self.domain_status_by_id, "domain_status_by_id"),
        )
        object.__setattr__(
            self,
            "last_durable_node_ids_by_sample",
            _tuple_mapping(
                self.last_durable_node_ids_by_sample,
                "last_durable_node_ids_by_sample",
            ),
        )
        object.__setattr__(
            self,
            "next_eligible_node_ids_by_sample",
            _tuple_mapping(
                self.next_eligible_node_ids_by_sample,
                "next_eligible_node_ids_by_sample",
            ),
        )
        object.__setattr__(
            self,
            "pending_interaction_ids",
            _string_tuple(self.pending_interaction_ids, "pending_interaction_ids"),
        )
        object.__setattr__(self, "summary_rows", _summary_rows(self.summary_rows, batch_id))
        object.__setattr__(self, "prerequisites", _prerequisites(self.prerequisites))


@dataclass(frozen=True, slots=True)
class BatchResultsSnapshot:
    """Completed publication evidence; never authority to execute a successor."""

    status: BatchStatusSnapshot
    sample_ids_by_validation_domain: Mapping[str, tuple[str, ...]]
    final_non_spatial_exports: tuple[BatchArtifactRef, ...]
    spatial_handoffs: tuple[BatchArtifactRef, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not BatchStatusSnapshot:
            raise TypeError("results require a typed batch status")
        rows = self.status.summary_rows
        sample_ids = {row.sample_id for row in rows}
        if (
            self.status.resume_disposition != "completed"
            or not rows
            or len(sample_ids) != len(rows)
            or self.status.pending_interaction_ids
            or any(row.status != "completed" or row.provenance_hash is None for row in rows)
        ):
            raise ValueError("results require completed samples with provenance hashes")
        groups = _tuple_mapping(
            self.sample_ids_by_validation_domain, "sample_ids_by_validation_domain"
        )
        members = tuple(sample_id for ids in groups.values() for sample_id in ids)
        if (
            any(not ids for ids in groups.values())
            or len(set(members)) != len(members)
            or set(members) != sample_ids
        ):
            raise ValueError("results validation groups must cover each sample exactly once")
        object.__setattr__(
            self,
            "sample_ids_by_validation_domain",
            MappingProxyType({group: tuple(sorted(ids)) for group, ids in groups.items()}),
        )
        for field in ("final_non_spatial_exports", "spatial_handoffs"):
            artifacts = getattr(self, field)
            if type(artifacts) is not tuple or not all(
                type(ref) is BatchArtifactRef for ref in artifacts
            ):
                raise TypeError(f"{field} must be an immutable BatchArtifactRef tuple")
            if (
                len(artifacts) != len(sample_ids)
                or {ref.scope.sample_id for ref in artifacts} != sample_ids
            ):
                raise ValueError(f"{field} must cover each result sample exactly once")
            object.__setattr__(
                self, field, tuple(sorted(artifacts, key=lambda ref: ref.scope.sample_id or ""))
            )


@dataclass(frozen=True, slots=True)
class BatchRetryChoice:
    """Select a retained failure or resume an unfinished Python calculation."""

    batch_id: str
    state_head_sha256: str
    node_id: str
    terminal_record_sha256: str | None
    sample_id: str
    stage_kind: str
    retry_reason: Literal[
        "timeout", "cancelled", "nonzero_exit", "input_required",
        "exchange_members_missing", "target_codec_rejected", "result_payload_invalid",
        "native_fit_failed", "source_access_failed", "runtime_dependency_unavailable",
        "pending_calculation",
        "request_preparation_failed", "calculation_failed",
    ]
    pending_authorization_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("batch_id", "node_id", "sample_id", "stage_kind"):
            _text(getattr(self, name), name)
        _sha256(self.state_head_sha256, "state_head_sha256")
        pending = self.retry_reason == "pending_calculation"
        if pending != (self.pending_authorization_sha256 is not None) or pending != (self.terminal_record_sha256 is None):
            raise ValueError("Retry must select exactly one retained failure or pending calculation.")
        if pending:
            _sha256(self.pending_authorization_sha256, "pending_authorization_sha256")
        else:
            _sha256(self.terminal_record_sha256, "terminal_record_sha256")
        _literal(
            self.retry_reason,
            frozenset({
                "timeout", "cancelled", "nonzero_exit", "input_required",
                "exchange_members_missing", "target_codec_rejected", "result_payload_invalid",
                "native_fit_failed", "source_access_failed", "runtime_dependency_unavailable",
                "pending_calculation",
                "request_preparation_failed", "calculation_failed",
            }),
            "retry_reason",
        )


@dataclass(frozen=True, slots=True)
class BatchCancellationResult:
    batch_id: str
    accepted: bool
    terminalized_node_ids: tuple[str, ...]
    state_head_sha256: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _text(self.batch_id, "batch_id"))
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a bool")
        object.__setattr__(
            self,
            "terminalized_node_ids",
            _string_tuple(self.terminalized_node_ids, "terminalized_node_ids"),
        )
        object.__setattr__(
            self,
            "state_head_sha256",
            _sha256(self.state_head_sha256, "state_head_sha256"),
        )
        object.__setattr__(self, "message", _text(self.message, "message"))


__all__ = [
    "BatchCancellationResult",
    "BatchResultsSnapshot",
    "BatchSavedStageFigure",
    "BatchSavedStageResult",
    "BatchSavedStageResults",
    "BatchRetryChoice",
    "BatchRunReport",
    "BatchStatusSnapshot",
    "BatchSummaryRow",
]
