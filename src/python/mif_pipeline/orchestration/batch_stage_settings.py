"""Present section options and settled review redo through one current-plan route."""
from __future__ import annotations

from dataclasses import dataclass
from mif_pipeline.pipeline_core.config import SegmentationConfig
from .batch_manifest import BatchSample
from .batch_source_revision import SourceSelection
from .batch_artifacts import BatchArtifactError, BatchArtifactRef
from .batch_dag import dependent_node_ids
from .batch_measurement_settings import MeasurementSettings
from .batch_settled_review_revision import SettledReviewRevision
from typing import TYPE_CHECKING
from mif_pipeline.pipeline_core import StageResult

if TYPE_CHECKING:
    from .batch_tissue_saved_settings import SavedTissueSettings


@dataclass(frozen=True, slots=True)
class BatchStageOptions:
    batch_id: str
    state_head_sha256: str
    node_id: str
    sample_id: str
    section_id: str | None
    kind: str
    settings: SegmentationConfig | MeasurementSettings | SettledReviewRevision | BatchSample | SourceSelection
    required_feature_ids: tuple[str, ...] = ()
    participant_ids: tuple[str, ...] = ()
    saved_tissue: SavedTissueSettings | None = None
    saved_unmixing: StageResult | None = None
    saved_analysis_mode: str | None = None
    saved_clustering: dict | None = None
    segmentation_channels: tuple = ()
    cell_boundary_source: str | None = None
    segmentation_nuclear_channel_id: str | None = None
    inspection_review: SettledReviewRevision | None = None
    saved_secondary: StageResult | None = None


@dataclass(frozen=True, slots=True)
class BatchStageRevisionPreview:
    options: BatchStageOptions
    settings: SegmentationConfig | MeasurementSettings | SettledReviewRevision | BatchSample | SourceSelection
    affected_node_ids: tuple[str, ...]
    preserved_completed_node_ids: tuple[str, ...]
    deferred_node_kinds: tuple[str, ...]
    blocked_reason: str | None = None
    saved_result_retirement_sources: tuple[BatchArtifactRef, ...] = ()


def stage_dependants(plan, node_id: str) -> frozenset[str]:
    """Follow actual task edges, including consumers pooled across samples."""
    if not any(item.node_id == node_id for item in plan.nodes):
        raise BatchArtifactError("task is absent from the current plan")
    return frozenset(dependent_node_ids(
        ((node.node_id, node.dependency_ids) for node in plan.nodes), (node_id,),
    ))


def inspection_review_target(workflow_nodes, channel_node_id: str) -> str:
    """Navigate one current channel review to its completed upstream inspection."""
    nodes = {node.node_id: node for node in workflow_nodes if not node.deferred}
    review = nodes.get(channel_node_id)
    if review is None or review.kind != 'channel_review' or review.status != 'needs_interaction':
        raise ValueError('The channel review changed. Reopen its current review.')
    pending, seen, inspections = list(review.dependency_ids), set(), []
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        node = nodes.get(identity)
        if node is None:
            continue
        if node.kind == 'inspection':
            inspections.append(node)
        else:
            pending.extend(node.dependency_ids)
    if (len(inspections) != 1 or inspections[0].sample_ids != review.sample_ids
            or inspections[0].status != 'completed'):
        raise ValueError('The current image inspection is unavailable. Refresh the analysis.')
    return inspections[0].node_id
