"""Return to a settled decision through the existing successor-plan owner."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import re
from typing import TYPE_CHECKING

from .batch_artifacts import BatchArtifactError, _closed_mapping, _digest

if TYPE_CHECKING:
    from .batch_project import PairedMovingReferenceAuthority
    from .batch_phenotyping.refinement_settings import ClusterRefitSettings


TRAINING_REVIEW_TARGETS = {
    "primary_classifier_training_review": "primary_classifier_training_plan_review",
    "secondary_classifier_training_review": "secondary_classifier_training_plan_review",
    "supervised_phenotype_training_review": "supervised_phenotype_training_plan_review",
}
REVIEW_TARGETS = {**TRAINING_REVIEW_TARGETS,
                  "clustering_label_import": "cluster_label_review",
                  "marker_rule_application": "marker_combination_selection"}
REGION_SCOPED_REVIEW_KINDS = frozenset({
    "phenotyping_mode_resolution", "unsupervised_preparation", "cluster_label_review",
    "clustering_per_slide", "clustering_pooled", "supervised_phenotype_inference",
})
SETTLED_REVIEW_KINDS = frozenset({
    "secondary_structure_definition", "marker_combination_selection",
    "inspection", "channel_review", "reference_unmixing", "marker_gate_review", *REGION_SCOPED_REVIEW_KINDS, "tissue_setup", "tissue_resolution", "tissue_candidate", "analysis_domain_selection", "registration",
    *TRAINING_REVIEW_TARGETS.values(),
})
from .batch_source_revision import SOURCE_CORRECTION_KINDS
STAGE_OPTIONS_KINDS = (SETTLED_REVIEW_KINDS - {"supervised_phenotype_inference"}) | set(REVIEW_TARGETS) | {"segmentation", "measurements"} | SOURCE_CORRECTION_KINDS


def requires_paired_alignment(sample, plan):
    """Include the Warpy owner for a configured or reopened image-pair choice."""
    if sample.config.registration.mode == "warpy":
        return True
    return any(
        node.kind == "registration"
        and node.participant_ids == (sample.sample_id,)
        and node.settled_review_revision is not None
        and (node.settled_review_revision.paired_reference is not None
             or sample.reference is not None)
        for node in plan.nodes
    )


def review_target(plan, node):
    """Return an applied result to its unique existing upstream review."""
    if node.kind in SOURCE_CORRECTION_KINDS and node.kind != "reference_unmixing":
        targets = [item for item in plan.nodes if item.kind == "inspection" and item.participant_ids == node.participant_ids]
        if len(targets) != 1:
            raise BatchArtifactError("This source has no unique current inspection task to correct.")
        return targets[0]
    kind = REVIEW_TARGETS.get(node.kind)
    if kind is None:
        return node
    nodes = {item.node_id: item for item in plan.nodes}
    pending, seen, targets = list(node.dependency_ids), set(), []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        item = nodes[key]
        if item.kind == kind:
            targets.append(item)
        else:
            pending.extend(item.dependency_ids)
    if len(targets) != 1:
        raise BatchArtifactError("This result has no unique current upstream review to revise.")
    return targets[0]


ReviewKey = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def review_scope_key(kind, validation_domain_id, participant_ids, bindings=(), section_target=None) -> ReviewKey:
    """Keep each phenotyping choice attached to its exact analysis regions."""
    regions = tuple(_digest(item if isinstance(item, Mapping) else item.to_mapping())
                    for item in bindings) if kind in REGION_SCOPED_REVIEW_KINDS else ()
    if kind == "supervised_phenotype_inference":
        if section_target is None:
            raise BatchArtifactError("Supervised assignment review requires its exact physical section.")
        regions = (*regions, _digest(section_target if isinstance(section_target, Mapping)
                                     else section_target.to_mapping()))
    return kind, validation_domain_id, tuple(participant_ids), regions


def review_key(node) -> ReviewKey:
    """Identify the same review scope while upstream task identities change."""
    if isinstance(node, Mapping):
        return review_scope_key(node["kind"], node["validation_domain_id"],
                                node["participant_ids"], node["analysis_domain_bindings"], node.get("section_target"))
    return review_scope_key(node.kind, node.validation_domain_id,
                            node.participant_ids, node.analysis_domain_bindings, getattr(node, "section_target", None))


def revision_root(plan, node_id):
    """Find the closest admitted return controlling this unchanged review scope."""
    target = plan.node_by_id[node_id]
    if "settled_review_revision" in target:
        return target
    pending = list(target["dependency_ids"])
    seen, roots = set(), []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        candidate = plan.node_by_id[key]
        if ("settled_review_revision" in candidate
                and candidate["validation_domain_id"] == target["validation_domain_id"]
                and tuple(candidate["participant_ids"]) == tuple(target["participant_ids"])):
            roots.append(candidate)
        else:
            pending.extend(candidate["dependency_ids"])
    # A later return can depend on an earlier returned decision through one
    # input and expose it again through another. Keep its nearest controller;
    # unrelated competing roots remain an error.
    ancestors = set()
    for root in roots:
        pending, seen = list(root["dependency_ids"]), set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            ancestors.add(key)
            pending.extend(plan.node_by_id[key]["dependency_ids"])
    roots = [root for root in roots if root["node_id"] not in ancestors]
    if len(roots) > 1:
        raise BatchArtifactError("settled review predecessor is ambiguous for this task")
    return roots[0] if roots else None


@dataclass(frozen=True, slots=True)
class SettledReviewRevision:
    predecessor_node_id: str
    revision: int
    predecessor_terminal_record_sha256: str
    paired_reference: PairedMovingReferenceAuthority | None = None
    cluster_refit: ClusterRefitSettings | None = None

    def __post_init__(self) -> None:
        if type(self.predecessor_node_id) is not str or re.fullmatch(
                r"node-[0-9a-f]{48}", self.predecessor_node_id) is None:
            raise BatchArtifactError("settled review revision requires its exact predecessor task")
        if type(self.revision) is not int or self.revision < 1:
            raise BatchArtifactError("settled review revision number must be positive")
        if type(self.predecessor_terminal_record_sha256) is not str or re.fullmatch(
                r"[0-9a-f]{64}", self.predecessor_terminal_record_sha256) is None:
            raise BatchArtifactError("settled review revision requires its retained terminal record")
        if self.paired_reference is not None:
            from .batch_project import PairedMovingReferenceAuthority

            if type(self.paired_reference) is not PairedMovingReferenceAuthority:
                raise BatchArtifactError("Paired-image revision requires its inspected source identity.")
            if self.paired_reference.canonical_uri != self.paired_reference.source_path.as_uri():
                raise BatchArtifactError("Paired-image source path differs from its inspected location.")
        if self.cluster_refit is not None:
            from .batch_phenotyping.refinement_settings import ClusterRefitSettings
            if type(self.cluster_refit) is not ClusterRefitSettings or self.paired_reference is not None:
                raise BatchArtifactError('A cluster refit requires its own typed selection.')

    def to_mapping(self) -> dict[str, object]:
        result = {"predecessor_node_id": self.predecessor_node_id, "revision": self.revision,
                  "predecessor_terminal_record_sha256": self.predecessor_terminal_record_sha256}
        if self.paired_reference is not None:
            result["paired_reference"] = self.paired_reference.to_mapping()
        if self.cluster_refit is not None:
            result['cluster_refit']=self.cluster_refit.to_mapping()
        return result

    @classmethod
    def from_mapping(cls, value: object) -> "SettledReviewRevision":
        paired_fields = {key for key in ('paired_reference','cluster_refit') if isinstance(value,Mapping) and key in value}
        mapping = _closed_mapping(value, fields=frozenset({
            "predecessor_node_id", "revision", "predecessor_terminal_record_sha256"
        } | paired_fields), field="settled review revision")
        values = dict(mapping)
        if 'paired_reference' in paired_fields:
            from .batch_project import PairedMovingReferenceAuthority

            values["paired_reference"] = PairedMovingReferenceAuthority.from_mapping(values["paired_reference"])
        if 'cluster_refit' in paired_fields:
            from .batch_phenotyping.refinement_settings import ClusterRefitSettings
            values['cluster_refit']=ClusterRefitSettings.from_mapping(values['cluster_refit'])
        return cls(**values)



def paired_reference_from_inventory(inventory, *, reference_id, modality, candidate_id=None):
    """Bind one inspected paired image, including the native reader series."""
    from dataclasses import asdict
    from mif_pipeline.io import SourceSeriesInventory, bind_source_series_selection
    from .batch_project import PairedMovingReferenceAuthority

    if type(inventory) is not SourceSeriesInventory:
        raise BatchArtifactError("Paired image selection requires its current IO inspection.")
    eligible = tuple(item for item in inventory.candidates if item.eligibility == "eligible")
    if candidate_id is None:
        if len(eligible) != 1:
            raise BatchArtifactError("Choose one of the inspected paired images.")
        candidate_id = eligible[0].candidate_id
    selection = bind_source_series_selection(inventory, candidate_id=candidate_id)
    candidate = next(item for item in eligible if item.candidate_id == candidate_id)
    if modality not in {"he", "brightfield", "fluorescence", "other"}:
        raise BatchArtifactError("Choose the paired image type.")
    source = inventory.source_identity
    metadata = {"source_series_selection": {
        **asdict(selection), "source_file_sha256": source.file_sha256,
        "size_x": candidate.size_x, "size_y": candidate.size_y, "size_c": candidate.size_c,
    }}
    return PairedMovingReferenceAuthority.create(reference_id=reference_id,
        source_path=source.resolved_path, canonical_uri=source.resolved_path.as_uri(),
        source_sha256=source.file_sha256, modality=modality,
        source_image_id=selection.image_id, source_metadata=metadata)


def validate_paired_revision(selection, displayed):
    """Reinspect a changed pair on the processing host before preview/publication."""
    from dataclasses import replace
    from mif_pipeline.io import inspect_source_series_inventory

    if type(selection) is not SettledReviewRevision or replace(selection, paired_reference=displayed.paired_reference) != displayed:
        raise BatchArtifactError("Alignment must restart from its displayed current completion.")
    if selection.paired_reference == displayed.paired_reference:
        return
    paired = selection.paired_reference
    if paired is None:
        raise BatchArtifactError("Keep the current pair here; choose mIF only in the alignment review.")
    inventory = inspect_source_series_inventory(paired.source_path, require_planar_channels=False)
    candidates = tuple(item for item in inventory.candidates
                       if item.image_id == paired.source_image_id and item.eligibility == "eligible")
    if len(candidates) != 1:
        raise BatchArtifactError("The selected paired image is no longer available. Inspect it again.")
    observed = paired_reference_from_inventory(inventory,
        reference_id=paired.reference_id, modality=paired.modality,
        candidate_id=candidates[0].candidate_id)
    if "source_series_selection" not in paired.source_metadata:
        # Retain legacy default-image authority without inventing a new selection.
        if len(tuple(item for item in inventory.candidates if item.eligibility == "eligible")) != 1 or candidates[0].source_series_index != 0:
            raise BatchArtifactError("Inspect and choose the paired image again before applying this revision.")
        observed = type(observed).create(reference_id=observed.reference_id,
            source_path=observed.source_path, canonical_uri=observed.canonical_uri,
            source_sha256=observed.source_sha256, modality=observed.modality,
            source_image_id=observed.source_image_id, source_metadata=paired.source_metadata)
    if observed != paired:
        raise BatchArtifactError("The paired image changed. Inspect it again before applying this revision.")
