"""Presentation of one owner-issued aggregate graph review.

The `AggregateReviewForm` record carries `group_id`, `group_sha256`, `target_id`,
`cluster_ids`, `expected_state_head_sha256` and its remaining declared fields.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping
from dataclasses import dataclass
import json

from mif_pipeline.gui.interactions import (
    ControlKind,
    FormControl,
    FormModel,
    FormTable,
    FormTableRow,
)
from mif_pipeline.orchestration.batch_interactions import (
    BatchAggregateInteractionGroup,
    BatchInteractionResponseRequest,
    BatchTargetSelection,
)
from mif_pipeline.pipeline_core import InteractionAction

# ==============================================================================
#                                Aggregate review
# ==============================================================================


@dataclass(frozen=True, slots=True)
class AggregateReviewForm:
    group_id: str
    group_sha256: str
    target_id: str
    cluster_ids: tuple[str, ...]
    expected_state_head_sha256: str
    form: FormModel


def build_aggregate_review_form(
    group: BatchAggregateInteractionGroup,
    *,
    expected_state_head_sha256: str,
) -> AggregateReviewForm:
    if type(group) is not BatchAggregateInteractionGroup:
        raise TypeError("aggregate review requires an owner-issued aggregate group")
    target, evidence = group.target, group.evidence
    tables = (
        FormTable(
            "marker_profiles",
            ("cluster_id", "measurement", "units", "raw_mean", "raw_median"),
            tuple(
                FormTableRow(
                    json.dumps((cluster.cluster_id, item.feature_id)),
                    (cluster.cluster_id, item.measurement, item.units,
                     item.raw_mean, item.raw_median),
                )
                for cluster in evidence.clusters
                for item in cluster.marker_profiles
            ),
        ),
        FormTable(
            "entry_counts",
            (
                "cluster_id",
                "sample_id",
                "cell_count",
            ),
            tuple(
                FormTableRow(
                    json.dumps((cluster.cluster_id, item.sample_id, item.project_entry_id)),
                    (cluster.cluster_id, item.sample_id, item.cell_count),
                )
                for cluster in evidence.clusters
                for item in cluster.entry_counts
            ),
        ),
        FormTable(
            "section_counts",
            (
                "cluster_id",
                "sample_id",
                "slide_id",
                "section_id",
                "cell_count",
            ),
            tuple(
                FormTableRow(
                    json.dumps((cluster.cluster_id, item.sample_id, item.project_entry_id,
                                item.slide_id, item.section_id)),
                    (cluster.cluster_id, item.sample_id, item.slide_id,
                     item.section_id, item.cell_count),
                )
                for cluster in evidence.clusters
                for item in cluster.section_counts
            ),
        ),
    )
    return AggregateReviewForm(
        group.group_id,
        group.group_sha256,
        target.target_id,
        target.cluster_ids,
        expected_state_head_sha256,
        FormModel(
            form_id=group.group_id,
            title="Review cell clusters",
            message=(f"Reclustering result: {evidence.refit_summary['cell_count']:,} cells from "
                f"{len(evidence.refit_summary['parent_cluster_ids']):,} selected groups were reclustered with "
                f"{'FlowSOM' if evidence.refit_summary['method']=='flowsom' else 'PhenoGraph'} using the original feature scaling. "
                f"{evidence.cell_count-evidence.refit_summary['cell_count']:,} cells kept their memberships. "
                "Review the new groups before applying names. Restore previous groups is available in Recluster selected groups."
                if evidence.refit_summary is not None else
                "Review marker measurements and cells in the image, then choose which clusters to include and name their phenotypes."),
            allowed_actions=(InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value,),
            controls=(
                FormControl(
                    "cluster_decisions",
                    "Cluster interpretation",
                    ControlKind.OBJECT_MAPPING,
                    required=False,
                    value={},
                    choices=target.cluster_ids,
                    help_text="Give clusters the same phenotype name to combine them in phenotype summaries. Leave a name blank to keep its numbered label. Clear Include in phenotyping to leave a cluster unphenotyped.",
                ),
            ),
            tables=tables,
            context={"cluster_counts": {item.cluster_id: item.cell_count for item in evidence.clusters},
                     **({'refit_summary':dict(evidence.refit_summary)} if evidence.refit_summary is not None else {})},
        ),
    )


def build_aggregate_review_request(
    view: AggregateReviewForm,
    *,
    cluster_labels: Mapping[str, str],
    excluded_cluster_ids: tuple[str, ...] = (),
) -> BatchInteractionResponseRequest:
    if type(view) is not AggregateReviewForm:
        raise TypeError("aggregate response requires an AggregateReviewForm")
    if not isinstance(cluster_labels, Mapping):
        raise ValueError("cluster labels must be a mapping")
    labels = dict(cluster_labels)
    for cluster_id, label in labels.items():
        if cluster_id not in view.cluster_ids:
            raise ValueError("cluster label refers to an unknown cluster ID")
        if type(label) is not str or not label.strip():
            raise ValueError("cluster labels must be nonblank text")
        if label.strip() == "Not phenotyped":
            raise ValueError("Clear Include in phenotyping to exclude a cluster")
    if (type(excluded_cluster_ids) is not tuple
            or any(type(item) is not str for item in excluded_cluster_ids)
            or len(set(excluded_cluster_ids)) != len(excluded_cluster_ids)
            or not set(excluded_cluster_ids) <= set(view.cluster_ids)
            or set(excluded_cluster_ids).intersection(labels)):
        raise ValueError("Excluded clusters must be current clusters without phenotype names")
    return BatchInteractionResponseRequest(
        group_id=view.group_id,
        group_sha256=view.group_sha256,
        selection=BatchTargetSelection(target_ids=(view.target_id,)),
        action=InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value,
        response={"cluster_labels": labels,
                  **({"excluded_cluster_ids": sorted(excluded_cluster_ids)}
                     if excluded_cluster_ids else {})},
        expected_state_head_sha256=view.expected_state_head_sha256,
    )


def cluster_decisions_from_values(view: AggregateReviewForm, values: Mapping[str, object]):
    """Translate the editable table into names and explicit excluded cluster IDs."""
    decisions = values.get("cluster_decisions")
    if not isinstance(decisions, Mapping) or set(decisions) != set(view.cluster_ids):
        raise ValueError("Review every cluster in the current table")
    labels = {}
    excluded = []
    for cluster_id, decision in decisions.items():
        if (not isinstance(decision, Mapping) or set(decision) != {"include", "name"}
                or type(decision["include"]) is not bool or type(decision["name"]) is not str):
            raise ValueError("Each cluster needs an inclusion choice and a phenotype name field")
        if not decision["include"]:
            excluded.append(cluster_id)
        elif decision["name"].strip():
            labels[cluster_id] = decision["name"].strip()
    return labels, tuple(sorted(excluded))
