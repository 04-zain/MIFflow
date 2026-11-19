"""Authority-free workflow evidence for desktop navigation and diagnostics.

These records describe the real plan; they never authorize execution or review."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .batch_dag import BatchPlan
    from mif_pipeline.pipeline_core import StageResult


# ==============================================================================
#                                 Batch workflow
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchWorkflowNode:
    node_id: str
    kind: str
    sample_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...] = ()
    status: str = "waiting"
    eligible: bool = False
    deferred: bool = False
    prerequisite_kinds: tuple[str, ...] = ()
    interaction_reason: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    source_correction_available: bool = False

    def __post_init__(self) -> None:
        for field in ("node_id", "kind", "status"):
            value = getattr(self, field)
            if type(value) is not str or not value.strip():
                raise ValueError(f"workflow {field} must be non-empty text")
        for field in ("sample_ids", "dependency_ids", "prerequisite_kinds"):
            values = getattr(self, field)
            if type(values) is not tuple or any(type(v) is not str or not v for v in values):
                raise TypeError(f"workflow {field} must be a text tuple")
            if len(set(values)) != len(values):
                raise ValueError(f"workflow {field} must be unique")
        if type(self.source_correction_available) is not bool:
            raise TypeError("source correction availability must be bool")
        if type(self.eligible) is not bool or type(self.deferred) is not bool:
            raise TypeError("workflow eligibility and deferred state must be bool")
        if self.deferred and self.eligible:
            raise ValueError("deferred workflow steps cannot be eligible")
        for field in ("interaction_reason", "failure_code", "failure_message"):
            value = getattr(self, field)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"workflow {field} must be non-empty text or None")


def project_plan_nodes(
    plan: BatchPlan,
    *,
    statuses: Mapping[str, str] | None = None,
    eligible_ids: frozenset[str] = frozenset(),
    results: Mapping[str, StageResult] | None = None,
    invalidated_ids: frozenset[str] = frozenset(),
    pending_authorization_ids: frozenset[str] = frozenset(),
    source_correction_samples: frozenset[str] = frozenset(),
) -> tuple[BatchWorkflowNode, ...]:
    """Describe the current work without treating its preceding attempt as current.

    A pending authorization identifies work awaiting completion. We display it
    as ready when its current prerequisites permit dispatch, otherwise waiting,
    and omit the preceding attempt's diagnostic. Invalidated work stays waiting.
    These display states do not authorize execution or alter retained history.
    """
    from .batch_dag import BatchPlan

    from .batch_progress import CurrentProgressPlan

    if not isinstance(plan, (BatchPlan, CurrentProgressPlan)):
        raise TypeError("workflow projection requires a BatchPlan")
    from .batch_source_revision import SOURCE_CORRECTION_KINDS
    statuses, results = statuses or {}, results or {}
    projected = []
    for node in plan.nodes:
        invalidated = node.node_id in invalidated_ids
        pending = node.node_id in pending_authorization_ids
        result = None if invalidated or pending else results.get(node.node_id)
        status = "waiting" if invalidated or pending else statuses.get(node.node_id, "waiting")
        eligible = node.node_id in eligible_ids and not invalidated
        if status == "waiting" and eligible:
            status = "ready"
        projected.append(
            BatchWorkflowNode(
                node.node_id,
                node.kind,
                node.participant_ids,
                node.dependency_ids,
                status,
                eligible,
                False,
                (),
                result.interaction_reason.value if result and result.interaction_reason else None,
                result.error_code if result else None,
                result.error_message if result else None,
                node.kind in SOURCE_CORRECTION_KINDS and set(node.participant_ids) <= source_correction_samples,
            )
        )
    projected.extend(
        BatchWorkflowNode(
            "planned-" + node.descriptor_sha256,
            node.node_kind,
            node.participant_ids,
            status="planned",
            deferred=True,
            prerequisite_kinds=node.prerequisite_node_kinds,
        )
        for node in plan.deferred_nodes
    )
    return tuple(projected)
