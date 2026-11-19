"""Bounded, presentation-only projection of an immutable batch plan.

The `BatchPlanView` record carries `sample_table`, `domain_table`, `prerequisite_table`,
`node_table`, `edge_table` and its remaining declared fields.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import re

from mif_pipeline.gui.interactions import FormTable, FormTableRow
from mif_pipeline.gui.manifest_draft import ManifestDraft
from mif_pipeline.orchestration.batch_dag import BatchPlan

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PREREQUISITE_LABELS = {
    "java_runtime": "Java runtime",
    "bioformats_runtime": "Bio-Formats runtime",
    "segmentation_method": "Available segmentation models",
    "instanseg_extension": "InstanSeg runtime and CPU libraries",
    "instanseg_model": "InstanSeg model",
    "cellpose_model": "Cellpose-SAM model",
    "stardist_model": "StarDist model",
    "warpy_extension": "Image alignment: Warpy, Elastix and Transformix",
    "classifier_artifact": "Saved classifier",
}

SAMPLE_COLUMNS = (
    "enabled",
    "sample_id",
    "slide_id",
    "subject_id",
    "validation_domain_id",
    "source_mif_path",
    "source_image_index",
    "source_series_index",
    "moving_reference_path",
    "moving_reference_modality",
    "moving_reference_id",
    "moving_reference_source_image_id",
)


# ==============================================================================
#                                      Plan
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchPlanView:
    """The finite public plan evidence rendered by the Plan page."""

    sample_table: FormTable
    domain_table: FormTable
    prerequisite_table: FormTable
    node_table: FormTable
    edge_table: FormTable
    deferred_table: FormTable
    warnings: tuple[str, ...]
    plan_sha256: str
    prerequisites_ready: bool

    def __post_init__(self) -> None:
        tables = (
            self.sample_table,
            self.domain_table,
            self.prerequisite_table,
            self.node_table,
            self.edge_table,
            self.deferred_table,
        )
        if not all(type(table) is FormTable for table in tables):
            raise TypeError("batch plan view tables must be FormTable values")
        if type(self.warnings) is not tuple or not all(
            type(item) is str and item.strip() for item in self.warnings
        ):
            raise TypeError("batch plan view warnings must be an immutable text tuple")
        if type(self.plan_sha256) is not str or _SHA256.fullmatch(self.plan_sha256) is None:
            raise ValueError("batch plan view digest must be a lowercase SHA-256")
        if type(self.prerequisites_ready) is not bool:
            raise TypeError("plan prerequisite readiness must be bool")


def _sample_table(draft: ManifestDraft) -> FormTable:
    return FormTable(
        "samples",
        SAMPLE_COLUMNS,
        tuple(
            FormTableRow(
                f"sample:{sample.sample_id}",
                (
                    sample.enabled,
                    sample.sample_id,
                    sample.slide_id,
                    sample.subject_id,
                    sample.validation_domain_id or draft.validation_domain_id,
                    str(sample.source_mif_path),
                    sample.source_image_index,
                    sample.source_series_index,
                    (
                        str(sample.moving_reference_path)
                        if sample.moving_reference_path is not None
                        else None
                    ),
                    sample.moving_reference_modality,
                    sample.moving_reference_id,
                    sample.moving_reference_source_image_id,
                ),
            )
            for sample in draft.samples
        ),
    )


def _domain_table(plan: BatchPlan) -> FormTable:
    participants_by_domain: dict[str, set[str]] = {}
    for node in plan.nodes:
        participants_by_domain.setdefault(node.validation_domain_id, set()).update(
            node.participant_ids
        )
    return FormTable(
        "domains",
        ("validation_domain_id", "sample_ids"),
        tuple(
            FormTableRow(
                f"domain:{domain_id}",
                (domain_id, tuple(sorted(participants))),
            )
            for domain_id, participants in sorted(participants_by_domain.items())
        ),
    )


def _prerequisite_table(plan: BatchPlan) -> FormTable:
    rows = tuple(
        FormTableRow(
            item.prerequisite_id,
            (
                item.prerequisite_kind,
                item.owner,
                _PREREQUISITE_LABELS[item.prerequisite_kind],
                item.state,
                item.scope,
                item.sample_id,
                item.validation_domain_id,
                item.required_by_node_kinds,
                item.detail,
                item.external_locator,
            ),
        )
        for item in plan.prerequisites
    )
    return FormTable(
        "prerequisites",
        (
            "kind",
            "owner",
            "requirement",
            "state",
            "scope",
            "sample_id",
            "validation_domain_id",
            "required_by_node_kinds",
            "detail",
            "external_locator",
        ),
        rows,
    )


def _node_table(plan: BatchPlan) -> FormTable:
    return FormTable(
        "nodes",
        (
            "kind",
            "scope",
            "participants",
            "validation_domain_id",
            "resource",
            "dispatch_mode",
        ),
        tuple(
            FormTableRow(
                node.node_id,
                (
                    node.kind,
                    node.scope.value,
                    node.participant_ids,
                    node.validation_domain_id,
                    node.resource.value,
                    node.dispatch_mode.value,
                ),
            )
            for node in plan.nodes
        ),
    )


def _edge_table(plan: BatchPlan) -> FormTable:
    return FormTable(
        "edges",
        ("artifact_kind", "producer_node_id", "consumer_node_id", "origin"),
        tuple(
            FormTableRow(
                f"edge:{edge.consumer_node_id}:{edge.artifact_kind}:{edge.artifact_key}",
                (
                    edge.artifact_kind,
                    edge.producer_node_id,
                    edge.consumer_node_id,
                    (None if edge.requirement.origin is None else edge.requirement.origin.value),
                ),
            )
            for edge in plan.edges
        ),
    )


def _deferred_table(plan: BatchPlan) -> FormTable:
    return FormTable(
        "deferred",
        (
            "kind",
            "scope",
            "participants",
            "prerequisites",
            "unresolved_identities",
            "resource",
        ),
        tuple(
            FormTableRow(
                descriptor.descriptor_sha256,
                (
                    descriptor.node_kind,
                    descriptor.scope.value,
                    descriptor.participant_ids,
                    descriptor.prerequisite_node_kinds,
                    descriptor.unresolved_identity_kinds,
                    descriptor.resource.value,
                ),
            )
            for descriptor in plan.deferred_nodes
        ),
    )


def build_batch_plan_view(
    plan: object,
    *,
    draft: ManifestDraft,
    warnings: tuple[str, ...] = (),
) -> BatchPlanView:
    """Project public immutable inputs without querying state or the filesystem."""

    if type(plan) is not BatchPlan:
        raise TypeError("batch plan projection requires a BatchPlan")
    if type(draft) is not ManifestDraft:
        raise TypeError("batch plan projection requires a ManifestDraft")
    if draft.batch_id != plan.batch_id:
        raise ValueError("plan and manifest draft batch IDs differ")
    if type(warnings) is not tuple or not all(type(item) is str for item in warnings):
        raise TypeError("plan warnings must be an immutable text tuple")
    return BatchPlanView(
        sample_table=_sample_table(draft),
        domain_table=_domain_table(plan),
        prerequisite_table=_prerequisite_table(plan),
        node_table=_node_table(plan),
        edge_table=_edge_table(plan),
        deferred_table=_deferred_table(plan),
        warnings=warnings,
        plan_sha256=plan.plan_sha256,
        prerequisites_ready=all(item.state == "ready" for item in plan.prerequisites),
    )


__all__ = ["BatchPlanView", "SAMPLE_COLUMNS", "build_batch_plan_view"]
