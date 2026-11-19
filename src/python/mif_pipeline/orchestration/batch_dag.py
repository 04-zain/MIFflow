"""Construct the dependency graph for the current sample and accepted-region inventory.

Node identities include the stage, target and applicable configuration. Edges bind accepted
upstream results. Shared operations name all declared participants, while tissue/domain
discovery can produce a successor plan with new concrete identities. Planning itself leaves
execution and result publication to the service."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .batch_project import PairedMovingReferenceAuthority

from mif_pipeline.pipeline_core import (
    ClusteringMode,
    PhenotypingMode,
    ROIAnalysisError,
    StageStatus,
    assert_current_writer,
)
from mif_pipeline.roi_validation import load_semantic_profile_v3

from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactProjectionTarget,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    DispatchMode,
    StageAdapterRegistry,
    StageAdapterSpec,
    StageArtifactRequirement,
    validate_artifact_edge,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from .batch_manifest import (
    ValidatedBatchManifest,
    revalidate_retained_manifest,
    validate_manifest,
)
from .setup.batch_prerequisites import BatchPrerequisiteItem
from .batch_training_settings import TrainingSettingsRevision
from .batch_segmentation_settings import SegmentationSettingsRevision
from .batch_measurement_settings import MeasurementSettingsRevision
from .batch_settled_review_revision import SettledReviewRevision, SETTLED_REVIEW_KINDS, ReviewKey, review_key, review_scope_key
from .batch_saved_result_retirement import SavedResultRetirement, KIND as SAVED_RESULT_RETIREMENT_KIND, revision_predecessor
from .batch_cell_corrections import CellCorrectionRequest, KIND as CELL_CORRECTION_KIND
from .supervised_cutoff_selection import SupervisedCutoffSelection

SCHEMA_NAME = "mif-batch-plan"
SCHEMA_VERSION = 2
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


# ==============================================================================
#                                   Batch dag
# ==============================================================================


def _node_version(kind: str) -> str:
    return "2" if kind in {"reference_unmixing", "segmentation_correction"} or kind in _TRAINING_PLAN_REVIEW_KINDS else "1"


_TRAINING_PLAN_REVIEW_KINDS = frozenset(
    {
        "primary_classifier_training_plan_review",
        "secondary_classifier_training_plan_review",
        "supervised_phenotype_training_plan_review",
    }
)


class BatchPlanError(ROIAnalysisError):
    """Invalid or non-authoritative graph material."""


class NodeScope(str, Enum):
    SAMPLE = "sample"
    VALIDATION_DOMAIN = "validation_domain"
    SHARED_PROJECT = "shared_project"


class ResourceKind(str, Enum):
    BIOFORMATS_CONVERSION = "bioformats_conversion"
    SPECTRAL_PREPROCESSING = "spectral_preprocessing"
    SCIENTIFIC_COMPUTE = "scientific_compute"
    QUPATH_IMAGE_READ = "qupath_image_read"
    QUPATH_IMAGE_WRITE = "qupath_image_write"
    PROJECT_CATALOGUE_MUTATION = "project_catalogue_mutation"
    SHARED_CLASSIFIER_MUTATION = "shared_classifier_mutation"


SINGLE_SAMPLE_STAGE_ADAPTATION: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "preflight": ("analysis_preflight",),
        "project": ("project_upsert", "paired_image_preparation", "tissue_setup"),
        "tissue_resolution": ("tissue_resolution",),
        "tissue_channel": ("tissue_channel",),
        "tissue_candidate": ("tissue_candidate",),
        "tissue_acceptance": ("tissue_acceptance",),
        "registration": ("registration",),
        "analysis_domain_selection": ("analysis_domain_selection",),
        "primary_classifier": (
            "primary_classifier_training_plan_review",
            "primary_training_evidence_snapshot",
            "primary_classifier_training_review",
            "primary_classifier_inference",
        ),
        "secondary_classifier": (
            "secondary_structure_definition",
            "secondary_classifier_training_plan_review",
            "secondary_training_evidence_snapshot",
            "secondary_classifier_training_review",
            "secondary_classifier_inference",
        ),
        "segmentation": ("segmentation",),
        "measurements": ("measurements",),
        "phenotyping": (
            "phenotyping_mode_resolution",
            "marker_gate_review",
            "marker_combination_selection",
            "marker_rule_application",
            "supervised_phenotype_training_plan_review",
            "supervised_training_evidence_snapshot",
            "supervised_phenotype_training_review",
            "supervised_phenotype_inference",
            "unsupervised_preparation",
        ),
        "clustering": (
            "clustering_per_slide",
            "clustering_pooled",
            "cluster_review_evidence",
            "cluster_label_review",
        ),
        "semantic_validation": (
            "semantic_raw_inventory_snapshot",
            "no_secondary_completion",
            "semantic_evidence",
            "semantic_evaluation",
            "final_count_cap",
            "semantic_finalization",
        ),
        "phenotype_handoff": ("phenotype_handoff",),
        "export": ("final_non_spatial_export",),
    }
)


_EARLY = (
    "inspection",
    "channel_review",
    "reference_unmixing",
    "analysis_preflight",
    "project_upsert",
)
_PROJECT = (
    "paired_image_preparation",
    "tissue_setup",
    "tissue_resolution",
    "tissue_channel",
    "tissue_candidate",
    "tissue_acceptance",
    "registration",
    "analysis_domain_selection",
)
_CLUSTERING = (
    "clustering_per_slide",
    "clustering_pooled",
)
_CLUSTER_REVIEW = ("cluster_review_evidence", "cluster_label_review")
_DOMAIN = (
    "primary_classifier_training_plan_review",
    "primary_training_evidence_snapshot",
    "primary_classifier_training_review",
    "primary_classifier_inference",
    "secondary_structure_definition",
    "secondary_classifier_training_plan_review",
    "secondary_training_evidence_snapshot",
    "secondary_classifier_training_review",
    "secondary_classifier_inference",
    "segmentation",
    "measurements",
    "phenotyping_mode_resolution",
    "marker_gate_review",
    "marker_combination_selection",
    "marker_rule_application",
    "supervised_phenotype_training_plan_review",
    "supervised_training_evidence_snapshot",
    "supervised_phenotype_training_review",
    "supervised_phenotype_inference",
    "unsupervised_preparation",
    "clustering_label_import",
    "semantic_raw_inventory_snapshot",
    "phenotype_hierarchy_attestation",
)
_BRANCH = (
    "no_secondary_completion",
    "semantic_evidence",
    "semantic_evaluation",
    "final_count_cap",
    "semantic_finalization",
    "phenotype_handoff",
    "final_non_spatial_export",
)
_ALL_KINDS = tuple(
    dict.fromkeys((*_EARLY, *_PROJECT, *_DOMAIN, *_CLUSTERING, *_CLUSTER_REVIEW, *_BRANCH, SAVED_RESULT_RETIREMENT_KIND, CELL_CORRECTION_KIND))
)


def registered_batch_node_kinds() -> tuple[str, ...]:
    """Return the planner's closed node vocabulary independently of adapters."""
    return tuple(sorted(_ALL_KINDS))


_EXPECTED_OUTPUT_KIND: Mapping[str, str] = MappingProxyType(
    {
        "inspection": "source_inspection",
        "channel_review": "stage1_review",
        "reference_unmixing": "processed_image",
        "analysis_preflight": "analysis_frame",
        "project_upsert": "project_catalogue_entry",
        "paired_image_preparation": "project_catalogue_entry",
        "tissue_setup": "tissue_setup_result",
        "tissue_resolution": "tissue_resolution_result",
        "tissue_channel": "tissue_channel_result",
        "tissue_candidate": "tissue_candidate_result",
        "tissue_acceptance": "tissue_acceptance_result",
        "registration": "registration_result",
        "analysis_domain_selection": "accepted_analysis_domain",
        "primary_classifier_training_plan_review": "training_plan_set",
        "primary_training_evidence_snapshot": ("primary_training_evidence_snapshot"),
        "secondary_classifier_training_plan_review": "training_plan_set",
        "secondary_structure_definition": "secondary_definition_selection",
        "secondary_training_evidence_snapshot": ("secondary_training_evidence_snapshot"),
        "primary_classifier_training_review": "classifier_record_set",
        "primary_classifier_inference": "primary_inference_result",
        "secondary_classifier_training_review": "classifier_record_set",
        "secondary_classifier_inference": "secondary_inference_result",
        "segmentation": "segmentation_result",
        "measurements": "measurements_result",
        "phenotyping_mode_resolution": "phenotyping_mode",
        "marker_gate_review": "marker_gate",
        "marker_combination_selection": "marker_combination",
        "marker_rule_application": "marker_phenotype_result",
        "supervised_phenotype_training_plan_review": "training_plan_set",
        "supervised_training_evidence_snapshot": ("supervised_training_evidence_snapshot"),
        "supervised_phenotype_training_review": "classifier_record_set",
        "supervised_phenotype_inference": "supervised_phenotype_result",
        "unsupervised_preparation": "unsupervised_preparation_result",
        "clustering_per_slide": "clustered_phenotype_result",
        "clustering_pooled": "clustered_phenotype_result",
        "cluster_review_evidence": "cluster_review_evidence",
        "cluster_label_review": "accepted_cluster_label_review",
        "clustering_label_import": "cluster_label_import_result",
        "semantic_raw_inventory_snapshot": "semantic_raw_inventory",
        "phenotype_hierarchy_attestation": "phenotype_view_attestation",
        "no_secondary_completion": "no_secondary_completion_evidence",
        "semantic_evidence": "semantic_evidence_result",
        "semantic_evaluation": "semantic_evaluation_result",
        "final_count_cap": "final_count_cap_result",
        "semantic_finalization": "semantic_finalization_result",
        "phenotype_handoff": "spatial_cell_handoff",
        "final_non_spatial_export": "final_non_spatial_export_result",
    }
)

_EXPECTED_OUTPUT_SCHEMA: Mapping[str, tuple[str, int]] = MappingProxyType(
    {
        "inspection": ("mif-batch-source-inspection", 1),
        "channel_review": ("mif-batch-stage1-review", 1),
        "reference_unmixing": (
            "mif-batch-reference-unmixing-completion",
            2,
        ),
        "analysis_preflight": ("mif-batch-analysis-preflight", 2),
        "project_upsert": ("mif-batch-project-catalogue", 1),
        "paired_image_preparation": ("mif-batch-project-catalogue", 1),
        "tissue_setup": ("tissue-detector-setup", 1),
        "tissue_resolution": ("coarse-tissue-resolution-selection", 1),
        "tissue_channel": ("tissue-detection-channel-selection", 1),
        "tissue_candidate": ("roi-generation-manifest", 1),
        "tissue_acceptance": ("roi-generation-manifest", 1),
        "registration": ("mif-batch-registration-result", 1),
        "analysis_domain_selection": ("mif-batch-accepted-analysis-domain", 1),
        "primary_classifier_inference": ("roi-generation-manifest", 1),
        "secondary_classifier_inference": ("roi-generation-manifest", 1),
        "secondary_structure_definition": ("mif-secondary-definition-selection", 1),
        "primary_classifier_training_plan_review": (
            "mif-training-plan-set",
            2,
        ),
        "primary_training_evidence_snapshot": (
            "roi-generation-manifest",
            1,
        ),
        "secondary_classifier_training_plan_review": (
            "mif-training-plan-set",
            2,
        ),
        "secondary_training_evidence_snapshot": (
            "roi-generation-manifest",
            1,
        ),
        "secondary_classifier_training_review": (
            "mif-classifier-record-set",
            1,
        ),
        "primary_classifier_training_review": (
            "mif-classifier-record-set",
            1,
        ),
        "segmentation": ("roi-generation-manifest", 1),
        "measurements": ("roi-generation-manifest", 1),
        "phenotyping_mode_resolution": (
            "mif-batch-phenotyping-mode-selection",
            2,
        ),
        "marker_gate_review": (
            "mif-batch-marker-gate-domain-authority",
            6,
        ),
        "marker_combination_selection": ("mif-batch-marker-combination-selection", 1),
        "marker_rule_application": (
            "mif-batch-marker-gate-section-application",
            1,
        ),
        "supervised_phenotype_inference": (
            "mif-batch-supervised-native-prediction",
            1,
        ),
        "supervised_phenotype_training_plan_review": (
            "mif-training-plan-set",
            2,
        ),
        "supervised_training_evidence_snapshot": (
            "roi-generation-manifest",
            1,
        ),
        "supervised_phenotype_training_review": (
            "mif-classifier-record-set",
            1,
        ),
        "unsupervised_preparation": ("mif-batch-unsupervised-input", 2),
        "clustering_per_slide": ("mif-batch-clustering-result", 1),
        "clustering_pooled": ("mif-batch-clustering-result", 1),
        "cluster_review_evidence": (
            "mif-batch-cluster-review-evidence",
            1,
        ),
        "cluster_label_review": (
            "mif-batch-accepted-cluster-label-review",
            1,
        ),
        "clustering_label_import": ("roi-generation-manifest", 1),
        "semantic_raw_inventory_snapshot": ("qupath-raw-objects", 1),
        "phenotype_hierarchy_attestation": (
            "mif-batch-phenotype-view-attestation",
            1,
        ),
        "no_secondary_completion": ("semantic-domain-completion", 1),
        "semantic_evidence": (
            "mif-batch-roi-semantic-evidence-reference",
            1,
        ),
        "semantic_evaluation": (
            "mif-batch-roi-semantic-result-reference",
            1,
        ),
        "final_count_cap": ("structure-count-decision", 1),
        "semantic_finalization": ("domain-finalization-evidence", 1),
        "phenotype_handoff": ("spatial-cell-handoff", 3),
        "final_non_spatial_export": ("roi-analysis-provenance", 2),
    }
)


def _bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BatchPlanError("plan material must be finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchPlanError(f"{field} must be a non-empty string")
    return value


def _token(value: object, field: str) -> str:
    value = _text(value, field)
    if _TOKEN_RE.fullmatch(value) is None:
        raise BatchPlanError(f"{field} must be a lowercase token")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise BatchPlanError(f"{field} must be a lowercase SHA-256")
    return value


def _node_id(value: object, field: str = "node_id") -> str:
    value = _text(value, field)
    if not value.startswith("node-") or len(value) != 53:
        raise BatchPlanError(f"{field} is not a deterministic node ID")
    int(value[5:], 16)
    return value


def _binding_key(value: AnalysisDomainBinding) -> tuple[object, ...]:
    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.analysis_domain_revision_sha256,
        value.domain_set_sha256,
        value.project_entry_id,
    )


def _marker_application_section_key(
    binding: AnalysisDomainBinding,
    section_target: PhysicalSectionTarget | None,
) -> tuple[str, str, str, str, str]:
    """Key marker applications by physical section, never analysis domain."""

    if section_target is None:
        raise BatchPlanError("marker application measurement lacks its physical section")
    if binding.project_entry_id != section_target.project_entry_id:
        raise BatchPlanError("marker application binding belongs to another project entry")
    return (
        binding.sample_id,
        section_target.project_entry_id,
        section_target.tissue_generation_id,
        section_target.section_id,
        section_target.tissue_path_object_id,
    )


def _supervised_application_sections(
    items: Sequence[tuple[object, object, object, object, object]],
) -> tuple[
    tuple[
        tuple[str, str, str, str, str],
        tuple[tuple[object, object, object, object, object], ...],
        object,
    ],
    ...,
]:
    """Close supervised application over one exact physical-section cover."""

    grouped: dict[
        tuple[str, str, str, str, str],
        list[tuple[object, object, object, object, object]],
    ] = {}
    for item in items:
        binding, measurements, _mode, _decision, mode_name = item
        if not isinstance(binding, AnalysisDomainBinding):
            raise BatchPlanError("supervised application binding is not typed")
        if mode_name != "supervised_classifier":
            raise BatchPlanError("supervised application requires one supervised mode decision")
        node = getattr(measurements, "node", None)
        grouped.setdefault(
            _marker_application_section_key(binding, getattr(node, "section_target", None)),
            [],
        ).append(item)
    result: list[
        tuple[
            tuple[str, str, str, str, str],
            tuple[tuple[object, object, object, object, object], ...],
            object,
        ]
    ] = []
    for section_key, raw_items in sorted(grouped.items()):
        section_items = tuple(sorted(raw_items, key=lambda item: _binding_key(item[0])))
        measurements_by_node = {item[1].node.node_id: item[1] for item in section_items}
        if len(measurements_by_node) != 1:
            raise BatchPlanError("supervised application section lacks one measurement authority")
        measurement = next(iter(measurements_by_node.values()))
        actual_bindings = tuple(item[0] for item in section_items)
        expected_bindings = tuple(
            sorted(measurement.node.analysis_domain_bindings, key=_binding_key)
        )
        if actual_bindings != expected_bindings:
            raise BatchPlanError("supervised application lacks exact physical-section domain cover")
        if len({item[2].node.node_id for item in section_items}) != len(section_items):
            raise BatchPlanError(
                "supervised application lacks one supervised mode decision per domain"
            )
        result.append((section_key, section_items, measurement))
    return tuple(result)


def _binding_tuple(values: Sequence[AnalysisDomainBinding]) -> tuple[AnalysisDomainBinding, ...]:
    result = tuple(values)
    if not all(isinstance(item, AnalysisDomainBinding) for item in result):
        raise BatchPlanError("analysis-domain bindings must be typed")
    if result != tuple(sorted(result, key=_binding_key)):
        raise BatchPlanError("analysis-domain bindings must be canonically sorted")
    if len({_binding_key(item) for item in result}) != len(result):
        raise BatchPlanError("analysis-domain bindings must be unique")
    return result


@dataclass(frozen=True, slots=True, order=True)
class PhysicalSectionTarget:
    """Exact owner-derived identity of one physical tissue section."""

    project_entry_id: str
    tissue_generation_id: str
    section_id: str
    tissue_path_object_id: str

    def __post_init__(self) -> None:
        for field in (
            "project_entry_id",
            "tissue_generation_id",
            "section_id",
            "tissue_path_object_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))

    def to_mapping(self) -> dict[str, str]:
        return {
            "project_entry_id": self.project_entry_id,
            "tissue_generation_id": self.tissue_generation_id,
            "section_id": self.section_id,
            "tissue_path_object_id": self.tissue_path_object_id,
        }


@dataclass(frozen=True, slots=True)
class ClusteringGraphScope:
    """Exact biological graph scope retained in authenticated node material."""

    mode: ClusteringMode
    phenotyping_group_id: str | None
    validation_domain_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPlanError("clustering graph mode is invalid") from exc
        group_id = (
            None
            if self.phenotyping_group_id is None
            else _text(self.phenotyping_group_id, "phenotyping_group_id")
        )
        domains = tuple(
            sorted(
                _text(item, "clustering validation_domain_id")
                for item in self.validation_domain_ids
            )
        )
        if not domains or len(set(domains)) != len(domains):
            raise BatchPlanError("clustering graph validation domains must be non-empty and unique")
        if group_id is not None:
            raise BatchPlanError("phenotyping_group_id is a reserved null field")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "validation_domain_ids", domains)

    def to_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "phenotyping_group_id": self.phenotyping_group_id,
            "validation_domain_ids": list(self.validation_domain_ids),
        }


@dataclass(frozen=True, slots=True)
class BatchNode:
    node_id: str
    kind: str
    version: str
    batch_id: str
    scope: NodeScope
    validation_domain_id: str | None
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    dependency_ids: tuple[str, ...]
    input_artifact_ids: tuple[str, ...]
    input_edge_material_sha256s: tuple[str, ...]
    declared_output_kinds: tuple[str, ...]
    resource: ResourceKind
    adapter_spec_sha256: str
    dispatch_mode: DispatchMode
    material_sha256: str
    structure_id: str | None = None
    classifier_id: str | None = None
    eligible_parent_ids: tuple[str, ...] = ()
    section_target: PhysicalSectionTarget | None = None
    clustering_scope: ClusteringGraphScope | None = None
    training_settings_revision: TrainingSettingsRevision | None = None
    segmentation_settings_revision: SegmentationSettingsRevision | None = None
    measurement_settings_revision: MeasurementSettingsRevision | None = None
    settled_review_revision: SettledReviewRevision | None = None
    paired_reference: PairedMovingReferenceAuthority | None = None
    saved_result_retirement: SavedResultRetirement | None = None
    cell_correction: CellCorrectionRequest | None = None

    def __post_init__(self) -> None:
        """Validate the BatchNode fields when the record is constructed.

        The checks cover ``structure_id``, ``classifier_id``, ``section_target``,
        ``clustering_scope``, ``analysis_domain_bindings``, ``configuration_bindings``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "kind", _token(self.kind, "node kind"))
        object.__setattr__(self, "version", _text(self.version, "node version"))
        object.__setattr__(self, "batch_id", _text(self.batch_id, "node batch_id"))
        validation_domain_id = (
            None
            if self.validation_domain_id is None
            else _text(self.validation_domain_id, "node validation domain")
        )
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        try:
            scope, resource, dispatch_mode = (
                NodeScope(self.scope),
                ResourceKind(self.resource),
                DispatchMode(self.dispatch_mode),
            )
        except (TypeError, ValueError) as exc:
            raise BatchPlanError("node uses a non-closed enum") from exc
        bindings = _binding_tuple(self.analysis_domain_bindings)
        participants = tuple(sorted(_text(item, "participant_id") for item in self.participant_ids))
        entries = tuple(sorted(_text(item, "project_entry_id") for item in self.project_entry_ids))
        entries_by_sample: dict[str, str] = {}
        for binding in bindings:
            prior_entry = entries_by_sample.setdefault(binding.sample_id, binding.project_entry_id)
            if prior_entry != binding.project_entry_id:
                raise BatchPlanError("one participant cannot bind multiple project entries")
        if len(participants) == 1 and len(entries) == 1 and not entries_by_sample:
            entries_by_sample[participants[0]] = entries[0]
        if entries and (
            set(entries_by_sample) != set(participants)
            or set(entries_by_sample.values()) != set(entries)
        ):
            raise BatchPlanError("project entries require exact participant associations")
        configurations = tuple(self.configuration_bindings)
        if not all(isinstance(item, ArtifactConfigurationBinding) for item in configurations):
            raise BatchPlanError("node configuration bindings must be typed")
        configurations = tuple(sorted(configurations, key=lambda item: item.sample_id))
        dependencies = tuple(
            sorted(_node_id(item, "dependency_id") for item in self.dependency_ids)
        )
        inputs = tuple(sorted(_text(item, "input artifact ID") for item in self.input_artifact_ids))
        edge_material = tuple(
            sorted(
                _sha(item, "input edge material digest")
                for item in self.input_edge_material_sha256s
            )
        )
        outputs = tuple(sorted(_token(item, "output kind") for item in self.declared_output_kinds))
        structure_id = self.structure_id
        classifier_id = self.classifier_id
        parents = tuple(self.eligible_parent_ids)
        section_target = self.section_target
        clustering_scope = self.clustering_scope
        if self.kind in _TRAINING_PLAN_REVIEW_KINDS:
            if self.version != "2":
                raise BatchPlanError("training plan review version is unsupported")
            if (
                self.training_settings_revision is not None
                and type(self.training_settings_revision) is not TrainingSettingsRevision
            ):
                raise BatchPlanError("training settings revision must be typed")
        elif self.training_settings_revision is not None:
            raise BatchPlanError("only a training plan review may bind settings revision")
        if self.segmentation_settings_revision is not None and (
            self.kind != "segmentation"
            or type(self.segmentation_settings_revision) is not SegmentationSettingsRevision
        ):
            raise BatchPlanError("only segmentation may bind its typed settings revision")
        if self.measurement_settings_revision is not None and (
            self.kind != "measurements"
            or type(self.measurement_settings_revision) is not MeasurementSettingsRevision
        ):
            raise BatchPlanError("only measurements may bind measurement settings revisions")
        if self.settled_review_revision is not None and (
            self.kind not in SETTLED_REVIEW_KINDS or type(self.settled_review_revision) is not SettledReviewRevision
        ):
            raise BatchPlanError("only a supported settled review may bind its typed revision")
        if (self.settled_review_revision is not None
                and self.settled_review_revision.paired_reference is not None
                and self.kind != "registration"):
            raise BatchPlanError("Only Alignment may select a revised paired image.")
        if (self.settled_review_revision is not None
                and self.settled_review_revision.cluster_refit is not None
                and self.kind not in {'clustering_per_slide','clustering_pooled'}):
            raise BatchPlanError('Only a completed clustering calculation may bind a selected-cluster refit.')
        if (self.kind == "paired_image_preparation") != (self.paired_reference is not None):
            raise BatchPlanError("Paired-image preparation requires its selected image identity.")
        if self.paired_reference is not None:
            from .batch_project import PairedMovingReferenceAuthority

            if type(self.paired_reference) is not PairedMovingReferenceAuthority:
                raise BatchPlanError("Paired-image preparation requires a typed source identity.")
        if (self.kind == SAVED_RESULT_RETIREMENT_KIND) != (self.saved_result_retirement is not None):
            raise BatchPlanError("saved result retirement task requires its exact binding")
        if self.saved_result_retirement is not None:
            binding = self.saved_result_retirement
            if (type(binding) is not SavedResultRetirement or len(entries) != 1
                    or {ref.scope.project_entry_id for ref in binding.sources} != set(entries)
                    or {ref.scope.sample_id for ref in binding.sources} != set(participants)):
                raise BatchPlanError("saved result retirement binding conflicts with its entry")
        if (self.kind == CELL_CORRECTION_KIND) != (self.cell_correction is not None):
            raise BatchPlanError("cell correction task requires its reviewed draft")
        if self.cell_correction is not None and type(self.cell_correction) is not CellCorrectionRequest:
            raise BatchPlanError("cell correction must be a typed known-cell request")
        if self.kind == "secondary_classifier_inference":
            structure_id = _text(structure_id, "secondary target structure_id")
            classifier_id = _text(classifier_id, "secondary target classifier_id")
            parents = tuple(_text(item, "secondary target eligible_parent_id") for item in parents)
            if not parents or len(set(parents)) != len(parents):
                raise BatchPlanError(
                    "secondary target eligible parents must be non-empty and unique"
                )
        elif structure_id is not None or classifier_id is not None or parents:
            raise BatchPlanError("only secondary classifier inference may bind a structure target")
        if self.kind in {
            "segmentation",
            CELL_CORRECTION_KIND,
            "measurements",
            "marker_rule_application",
            "supervised_phenotype_inference",
            "clustering_label_import",
            "phenotype_hierarchy_attestation",
        }:
            if not isinstance(section_target, PhysicalSectionTarget):
                raise BatchPlanError("section-level nodes require an exact physical section target")
            if (
                len(entries) != 1
                or entries[0] != section_target.project_entry_id
                or not bindings
                or {item.project_entry_id for item in bindings} != {section_target.project_entry_id}
            ):
                raise BatchPlanError("physical section target conflicts with node entry or domains")
        elif section_target is not None:
            raise BatchPlanError("only exact section stages may bind a physical section target")
        clustering_kind_by_mode = {
            ClusteringMode.PER_SLIDE: "clustering_per_slide",
            ClusteringMode.POOLED: "clustering_pooled",
        }
        graph_scoped_kinds = {
            *clustering_kind_by_mode.values(),
            "cluster_review_evidence",
            "cluster_label_review",
        }
        if self.kind in graph_scoped_kinds:
            if (
                not isinstance(clustering_scope, ClusteringGraphScope)
                or (
                    self.kind in set(clustering_kind_by_mode.values())
                    and clustering_kind_by_mode[clustering_scope.mode] != self.kind
                )
                or resource is not ResourceKind.SCIENTIFIC_COMPUTE
            ):
                raise BatchPlanError("clustering graph node lacks its exact scientific scope")
            if clustering_scope.mode is ClusteringMode.PER_SLIDE:
                if (
                    scope is not NodeScope.SAMPLE
                    or len(clustering_scope.validation_domain_ids) != 1
                    or validation_domain_id != clustering_scope.validation_domain_ids[0]
                ):
                    raise BatchPlanError("per_slide clustering has inconsistent graph scope")
            elif (scope is not NodeScope.VALIDATION_DOMAIN
                  or clustering_scope.validation_domain_ids != (validation_domain_id,)):
                raise BatchPlanError("pooled clustering requires one Processing group")
        elif clustering_scope is not None:
            raise BatchPlanError("clustering graph scope is allowed only on clustering nodes")
        elif validation_domain_id is None:
            raise BatchPlanError("non-pooled nodes require one validation domain")
        if not participants or len(set(participants)) != len(participants):
            raise BatchPlanError("node participants must be non-empty and unique")
        pooled_clustering = self.kind in {
            "clustering_pooled",
            "cluster_review_evidence",
            "cluster_label_review",
        }
        if scope is NodeScope.SAMPLE and len(participants) != 1:
            raise BatchPlanError("sample nodes require one participant")
        if scope is NodeScope.SHARED_PROJECT and len(participants) != 1 and not pooled_clustering:
            raise BatchPlanError(
                "only pooled clustering project nodes may bind multiple participants"
            )
        if {item.sample_id for item in bindings} - set(participants):
            raise BatchPlanError("domain binding is outside node participants")
        if (
            len(set(dependencies)) != len(dependencies)
            or len(set(entries)) != len(entries)
            or len(set(edge_material)) != len(edge_material)
        ):
            raise BatchPlanError("node dependencies and inputs must be unique")
        if len(inputs) != len(edge_material):
            raise BatchPlanError("node input identities lack exact edge material")
        if tuple(item.sample_id for item in configurations) != participants:
            raise BatchPlanError("node configuration bindings must exactly cover participants")
        if not outputs or len(set(outputs)) != len(outputs):
            raise BatchPlanError("node outputs must be non-empty and unique")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "dispatch_mode", dispatch_mode)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "configuration_bindings", configurations)
        object.__setattr__(self, "dependency_ids", dependencies)
        object.__setattr__(self, "input_artifact_ids", inputs)
        object.__setattr__(self, "input_edge_material_sha256s", edge_material)
        object.__setattr__(self, "declared_output_kinds", outputs)
        object.__setattr__(self, "structure_id", structure_id)
        object.__setattr__(self, "classifier_id", classifier_id)
        object.__setattr__(self, "eligible_parent_ids", parents)
        object.__setattr__(self, "section_target", section_target)
        object.__setattr__(self, "clustering_scope", clustering_scope)
        object.__setattr__(
            self,
            "adapter_spec_sha256",
            _sha(self.adapter_spec_sha256, "adapter spec digest"),
        )
        self.validate_material()

    def _material(self) -> dict[str, object]:
        result = {
            "batch_id": self.batch_id,
            "kind": self.kind,
            "version": self.version,
            "scope": self.scope.value,
            "validation_domain_id": self.validation_domain_id,
            "participant_ids": list(self.participant_ids),
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "project_entry_ids": list(self.project_entry_ids),
            "configuration_bindings": [item.to_mapping() for item in self.configuration_bindings],
            "dependency_ids": list(self.dependency_ids),
            "input_artifact_ids": list(self.input_artifact_ids),
            "input_edge_material_sha256s": list(self.input_edge_material_sha256s),
            "declared_output_kinds": list(self.declared_output_kinds),
            "resource": self.resource.value,
            "adapter_spec_sha256": self.adapter_spec_sha256,
            "dispatch_mode": self.dispatch_mode.value,
        }
        if self.structure_id is not None:
            result["secondary_target"] = {
                "structure_id": self.structure_id,
                "classifier_id": self.classifier_id,
                "eligible_parent_ids": list(self.eligible_parent_ids),
            }
        if self.section_target is not None:
            result["section_target"] = self.section_target.to_mapping()
        if self.clustering_scope is not None:
            result["clustering_scope"] = self.clustering_scope.to_mapping()
        if self.segmentation_settings_revision is not None:
            result["segmentation_settings_revision"] = self.segmentation_settings_revision.to_mapping()
        if self.measurement_settings_revision is not None:
            result["measurement_settings_revision"] = self.measurement_settings_revision.to_mapping()
        if self.settled_review_revision is not None:
            result["settled_review_revision"] = self.settled_review_revision.to_mapping()
        if self.paired_reference is not None:
            result["paired_reference"] = self.paired_reference.to_mapping()
        if self.saved_result_retirement is not None:
            result["saved_result_retirement"] = self.saved_result_retirement.to_mapping()
        if self.cell_correction is not None:
            result["cell_correction"] = self.cell_correction.to_mapping()
        if self.kind in _TRAINING_PLAN_REVIEW_KINDS:
            result["training_settings_revision"] = (
                None
                if self.training_settings_revision is None
                else self.training_settings_revision.to_mapping()
            )
        return result

    def validate_material(self) -> None:
        material_sha = _sha(self.material_sha256, "material_sha256")
        expected = _digest(self._material())
        if material_sha != expected:
            raise BatchPlanError("node material_sha256 is not canonical")
        if self.node_id != "node-" + expected[:48]:
            raise BatchPlanError("node ID does not match canonical material")

    def to_mapping(self) -> dict[str, object]:
        result = {
            "node_id": self.node_id,
            "kind": self.kind,
            "version": self.version,
            "batch_id": self.batch_id,
            "scope": self.scope.value,
            "validation_domain_id": self.validation_domain_id,
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "configuration_bindings": [item.to_mapping() for item in self.configuration_bindings],
            "dependency_ids": list(self.dependency_ids),
            "input_artifact_ids": list(self.input_artifact_ids),
            "input_edge_material_sha256s": list(self.input_edge_material_sha256s),
            "declared_output_kinds": list(self.declared_output_kinds),
            "resource": self.resource.value,
            "adapter_spec_sha256": self.adapter_spec_sha256,
            "dispatch_mode": self.dispatch_mode.value,
            "material_sha256": self.material_sha256,
        }
        if self.structure_id is not None:
            result["secondary_target"] = {
                "structure_id": self.structure_id,
                "classifier_id": self.classifier_id,
                "eligible_parent_ids": list(self.eligible_parent_ids),
            }
        if self.section_target is not None:
            result["section_target"] = self.section_target.to_mapping()
        if self.clustering_scope is not None:
            result["clustering_scope"] = self.clustering_scope.to_mapping()
        if self.segmentation_settings_revision is not None:
            result["segmentation_settings_revision"] = self.segmentation_settings_revision.to_mapping()
        if self.measurement_settings_revision is not None:
            result["measurement_settings_revision"] = self.measurement_settings_revision.to_mapping()
        if self.settled_review_revision is not None:
            result["settled_review_revision"] = self.settled_review_revision.to_mapping()
        if self.paired_reference is not None:
            result["paired_reference"] = self.paired_reference.to_mapping()
        if self.saved_result_retirement is not None:
            result["saved_result_retirement"] = self.saved_result_retirement.to_mapping()
        if self.cell_correction is not None:
            result["cell_correction"] = self.cell_correction.to_mapping()
        if self.kind in _TRAINING_PLAN_REVIEW_KINDS:
            result["training_settings_revision"] = (
                None
                if self.training_settings_revision is None
                else self.training_settings_revision.to_mapping()
            )
        return result


@dataclass(frozen=True, slots=True)
class DeferredNodeDescriptor:
    node_kind: str
    node_version: str
    scope: NodeScope
    participant_ids: tuple[str, ...]
    prerequisite_node_kinds: tuple[str, ...]
    unresolved_identity_kinds: tuple[str, ...]
    resource: ResourceKind
    descriptor_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_kind", _token(self.node_kind, "deferred node kind"))
        object.__setattr__(self, "node_version", _text(self.node_version, "deferred version"))
        try:
            scope, resource = NodeScope(self.scope), ResourceKind(self.resource)
        except (TypeError, ValueError) as exc:
            raise BatchPlanError("deferred descriptor uses a non-closed enum") from exc
        participants = tuple(sorted(_text(item, "participant") for item in self.participant_ids))
        prerequisites = tuple(
            sorted(_token(item, "prerequisite") for item in self.prerequisite_node_kinds)
        )
        unresolved = tuple(
            sorted(_token(item, "unresolved identity") for item in self.unresolved_identity_kinds)
        )
        if not participants or not unresolved:
            raise BatchPlanError("deferred descriptor requires participants and missing identities")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "prerequisite_node_kinds", prerequisites)
        object.__setattr__(self, "unresolved_identity_kinds", unresolved)
        digest = _sha(self.descriptor_sha256, "descriptor_sha256")
        if digest != _digest(self._unsigned()):
            raise BatchPlanError("deferred descriptor digest is not canonical")

    def _unsigned(self) -> dict[str, object]:
        return {
            "node_kind": self.node_kind,
            "node_version": self.node_version,
            "scope": self.scope.value,
            "participant_ids": list(self.participant_ids),
            "prerequisite_node_kinds": list(self.prerequisite_node_kinds),
            "unresolved_identity_kinds": list(self.unresolved_identity_kinds),
            "resource": self.resource.value,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned(), "descriptor_sha256": self.descriptor_sha256}


@dataclass(frozen=True, slots=True)
class BatchArtifactEdge:
    artifact_id: str
    artifact_kind: str
    producer_node_id: str | None
    consumer_node_id: str
    requirement: StageArtifactRequirement
    accepted_artifact_ref_sha256: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, "edge artifact ID"))
        object.__setattr__(self, "artifact_kind", _token(self.artifact_kind, "edge artifact kind"))
        if self.producer_node_id is not None:
            object.__setattr__(
                self,
                "producer_node_id",
                _node_id(self.producer_node_id, "producer node"),
            )
        object.__setattr__(
            self, "consumer_node_id", _node_id(self.consumer_node_id, "consumer node")
        )
        if not isinstance(self.requirement, StageArtifactRequirement):
            raise BatchPlanError("edge requirement must be typed")
        if self.requirement.artifact_kind != self.artifact_kind:
            raise BatchPlanError("edge kind disagrees with its requirement")
        if self.accepted_artifact_ref_sha256 is not None:
            object.__setattr__(
                self,
                "accepted_artifact_ref_sha256",
                _sha(self.accepted_artifact_ref_sha256, "accepted ref"),
            )
        if self.producer_node_id is None:
            if (
                self.accepted_artifact_ref_sha256 is None
                or self.requirement.origin is not ArtifactOrigin.MANIFEST_EXTERNAL
            ):
                raise BatchPlanError("external edge requires exact manifest input authority")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "producer_node_id": self.producer_node_id,
            "consumer_node_id": self.consumer_node_id,
            "requirement": self.requirement.to_mapping(),
            "accepted_artifact_ref_sha256": self.accepted_artifact_ref_sha256,
        }

    @property
    def artifact_key(self) -> str:
        """Exact availability key; projections use their complete ref digest."""
        return self.accepted_artifact_ref_sha256 or self.artifact_id


@dataclass(frozen=True, slots=True)
class BatchPlan:
    schema_name: str
    schema_version: int
    batch_id: str
    normalized_manifest_sha256: str
    revision: int
    predecessor_plan_sha256: str | None
    workspace_rebinding_sha256: str | None
    nodes: tuple[BatchNode, ...]
    edges: tuple[BatchArtifactEdge, ...]
    external_inputs: tuple[BatchArtifactRef, ...]
    deferred_nodes: tuple[DeferredNodeDescriptor, ...]
    prerequisites: tuple[BatchPrerequisiteItem, ...]
    adapter_spec_set_sha256: str
    plan_sha256: str
    supervised_cutoff_selections: tuple[SupervisedCutoffSelection, ...] = ()

    def __post_init__(self) -> None:
        """Validate the BatchPlan fields when the record is constructed.

        The checks cover ``prerequisites``, ``predecessor_plan_sha256``,
        ``workspace_rebinding_sha256``, ``batch_id``, ``normalized_manifest_sha256``,
        ``adapter_spec_set_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (self.schema_name, self.schema_version) != (SCHEMA_NAME, SCHEMA_VERSION):
            raise BatchPlanError("batch plan must use mif-batch-plan/v2; v1 requires regeneration")
        try:
            assert_current_writer(SCHEMA_NAME, str(SCHEMA_VERSION))
        except ValueError as exc:
            raise BatchPlanError("batch plan schema is not current") from exc
        if type(self.revision) is not int or self.revision < 1:
            raise BatchPlanError("plan revision must be positive")
        if (self.revision == 1) != (self.predecessor_plan_sha256 is None):
            raise BatchPlanError("only revision one may omit a predecessor")
        if self.predecessor_plan_sha256 is not None:
            _sha(self.predecessor_plan_sha256, "predecessor plan")
        if self.workspace_rebinding_sha256 is not None:
            _sha(self.workspace_rebinding_sha256, "workspace rebinding")
        _text(self.batch_id, "batch_id")
        _sha(self.normalized_manifest_sha256, "manifest digest")
        _sha(self.adapter_spec_set_sha256, "adapter set digest")
        selections = self.supervised_cutoff_selections
        if (type(selections) is not tuple
                or any(type(item) is not SupervisedCutoffSelection for item in selections)
                or selections != tuple(sorted(selections, key=lambda item: item.sort_key))
                or len({item.sort_key for item in selections}) != len(selections)):
            raise BatchPlanError("Supervised cutoff selections must be unique and canonical.")
        nodes, edges = tuple(self.nodes), tuple(self.edges)
        deferred = tuple(self.deferred_nodes)
        if type(self.prerequisites) is not tuple:
            raise BatchPlanError("plan prerequisites must be an immutable tuple")
        prerequisites = self.prerequisites
        if not all(isinstance(item, BatchPrerequisiteItem) for item in prerequisites):
            raise BatchPlanError("plan prerequisites must be typed")
        for item in prerequisites:
            replace(item)
        if prerequisites != tuple(sorted(prerequisites, key=lambda item: item.prerequisite_id)):
            raise BatchPlanError("plan prerequisites must be sorted")
        if len({item.prerequisite_id for item in prerequisites}) != len(prerequisites):
            raise BatchPlanError("plan prerequisite IDs must be unique")
        if not all(isinstance(item, BatchNode) for item in nodes):
            raise BatchPlanError("plan nodes must be typed")
        if not all(isinstance(item, BatchArtifactEdge) for item in edges):
            raise BatchPlanError("plan edges must be typed")
        if not all(isinstance(item, DeferredNodeDescriptor) for item in deferred):
            raise BatchPlanError("deferred nodes must be typed")
        present_kinds = {item.kind for item in nodes} | {item.node_kind for item in deferred}
        if any(not set(item.required_by_node_kinds) <= present_kinds for item in prerequisites):
            raise BatchPlanError("plan prerequisite references foreign node kind")
        for edge in edges:
            replace(edge)
        for descriptor in deferred:
            replace(descriptor)
        external = tuple(self.external_inputs)
        if not all(isinstance(item, BatchArtifactRef) for item in external):
            raise BatchPlanError("external inputs must be typed artifact references")
        try:
            external = tuple(validate_artifact_ref(item) for item in external)
        except BatchArtifactError as exc:
            raise BatchPlanError("external input reference is invalid") from exc
        if any(item.origin is not ArtifactOrigin.MANIFEST_EXTERNAL for item in external):
            raise BatchPlanError("plan external inputs must have manifest_external origin")
        if external != tuple(sorted(external, key=lambda item: item.artifact_ref_sha256)) or len(
            {item.artifact_ref_sha256 for item in external}
        ) != len(external):
            raise BatchPlanError("plan external inputs must be canonical and unique")
        if nodes != tuple(sorted(nodes, key=lambda item: item.node_id)):
            raise BatchPlanError("plan nodes are not canonical")
        edge_key = lambda item: (
            item.consumer_node_id,
            item.artifact_kind,
            item.artifact_key,
        )
        if edges != tuple(sorted(edges, key=edge_key)):
            raise BatchPlanError("plan edges are not canonical")
        if len({item.node_id for item in nodes}) != len(nodes):
            raise BatchPlanError("plan has a node-ID collision")
        if deferred != tuple(sorted(deferred, key=lambda item: item.descriptor_sha256)) or len(
            {item.descriptor_sha256 for item in deferred}
        ) != len(deferred):
            raise BatchPlanError("deferred nodes must be canonical and unique")
        _validate_graph(nodes, edges, external)
        for node in nodes:
            node.validate_material()
            if (
                node.batch_id != self.batch_id
            ):
                raise BatchPlanError("node authority differs from enclosing plan")
        if _sha(self.plan_sha256, "plan_sha256") != _digest(self._unsigned()):
            raise BatchPlanError("plan_sha256 is not canonical")

    def _unsigned(self) -> dict[str, object]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "revision": self.revision,
            "predecessor_plan_sha256": self.predecessor_plan_sha256,
            "workspace_rebinding_sha256": self.workspace_rebinding_sha256,
            "nodes": [item.to_mapping() for item in self.nodes],
            "edges": [item.to_mapping() for item in self.edges],
            "external_inputs": [item.to_mapping() for item in self.external_inputs],
            "deferred_nodes": [item.to_mapping() for item in self.deferred_nodes],
            "prerequisites": [item.to_mapping() for item in self.prerequisites],
            "adapter_spec_set_sha256": self.adapter_spec_set_sha256,
        }
        if self.supervised_cutoff_selections:
            result["supervised_cutoff_selections"] = [item.to_mapping() for item in self.supervised_cutoff_selections]
        return result

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned(), "plan_sha256": self.plan_sha256}


def _validate_graph(
    nodes: tuple[BatchNode, ...],
    edges: tuple[BatchArtifactEdge, ...],
    external_inputs: tuple[BatchArtifactRef, ...] = (),
) -> None:
    """Validate graph for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    by_id = {item.node_id: item for item in nodes}
    pair_preparations = {item.node_id: item for item in nodes if item.kind == "paired_image_preparation"}
    referenced_pairs = set()
    for node in nodes:
        if node.kind != "registration":
            continue
        selected = None if node.settled_review_revision is None else node.settled_review_revision.paired_reference
        predecessors = [pair_preparations[key] for key in node.dependency_ids if key in pair_preparations]
        if selected is None:
            if predecessors:
                raise BatchPlanError("Alignment has no selected paired image for its preparation.")
            continue
        if (len(predecessors) != 1 or predecessors[0].paired_reference != selected
                or predecessors[0].participant_ids != node.participant_ids
                or predecessors[0].project_entry_ids != node.project_entry_ids):
            raise BatchPlanError("Alignment must use its exact selected paired-image preparation.")
        referenced_pairs.add(predecessors[0].node_id)
    if referenced_pairs != set(pair_preparations):
        raise BatchPlanError("Paired-image preparation must belong to the current Alignment selection.")
    for node in nodes:
        for dependency in node.dependency_ids:
            if dependency not in by_id:
                raise BatchPlanError(f"node has missing dependency {dependency}")
            if dependency == node.node_id:
                raise BatchPlanError("batch plan contains a cycle")
    active: set[str] = set()
    done: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            raise BatchPlanError("batch plan contains a cycle")
        if node_id in done:
            return
        active.add(node_id)
        for dependency in by_id[node_id].dependency_ids:
            visit(dependency)
        active.remove(node_id)
        done.add(node_id)

    for node_id in sorted(by_id):
        visit(node_id)
    by_consumer: dict[str, list[BatchArtifactEdge]] = {}
    for edge in edges:
        if edge.consumer_node_id not in by_id:
            raise BatchPlanError("edge has a missing consumer")
        consumer = by_id[edge.consumer_node_id]
        if edge.producer_node_id is None:
            matches = [
                item
                for item in external_inputs
                if item.artifact_id == edge.artifact_id
                and item.artifact_ref_sha256 == edge.accepted_artifact_ref_sha256
            ]
            if len(matches) != 1:
                raise BatchPlanError("external edge lacks exact plan input authority")
            try:
                validate_artifact_edge(matches[0], edge.requirement)
            except BatchArtifactError as exc:
                raise BatchPlanError("external edge requirement is invalid") from exc
        else:
            if edge.producer_node_id not in by_id:
                raise BatchPlanError("edge has a missing producer")
            producer = by_id[edge.producer_node_id]
            if edge.artifact_kind not in producer.declared_output_kinds:
                raise BatchPlanError("edge kind is not a declared producer output")
            if producer.node_id not in consumer.dependency_ids:
                raise BatchPlanError("edge producer is not a consumer dependency")
        by_consumer.setdefault(consumer.node_id, []).append(edge)
    for node in nodes:
        consumer_edges = by_consumer.get(node.node_id, ())
        actual = tuple(sorted(item.artifact_id for item in consumer_edges))
        if actual != node.input_artifact_ids:
            raise BatchPlanError("node inputs do not match typed edges")
        edge_material = tuple(
            sorted(
                _digest(
                    _input_edge_material(
                        artifact_id=item.artifact_id,
                        artifact_kind=item.artifact_kind,
                        requirement=item.requirement,
                        accepted_artifact_ref_sha256=(item.accepted_artifact_ref_sha256),
                    )
                )
                for item in consumer_edges
            )
        )
        if edge_material != node.input_edge_material_sha256s:
            raise BatchPlanError("node material does not match typed edges")


@dataclass(frozen=True, slots=True)
class _Context:
    node: BatchNode
    validation_domain_id: str | None
    project_entry_ids: tuple[str, ...]


def _input_edge_material(
    *,
    artifact_id: str,
    artifact_kind: str,
    requirement: StageArtifactRequirement,
    accepted_artifact_ref_sha256: str | None,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "artifact_kind": artifact_kind,
        "requirement_sha256": requirement.requirement_sha256,
        "accepted_artifact_ref_sha256": accepted_artifact_ref_sha256,
    }


class _Builder:
    def __init__(
        self,
        manifest: ValidatedBatchManifest,
        registry: StageAdapterRegistry,
        accepted_by_producer_kind: Mapping[tuple[str, str], BatchArtifactRef],
        training_settings_revisions: (
            Mapping[tuple[str, str], TrainingSettingsRevision] | None
        ) = None,
        segmentation_settings_revisions: Mapping[tuple[str, str], SegmentationSettingsRevision] | None = None,
        measurement_settings_revisions: Mapping[tuple[str, str], MeasurementSettingsRevision] | None = None,
        settled_review_revisions: Mapping[ReviewKey, SettledReviewRevision] | None = None,
        cell_corrections: tuple[CellCorrectionRequest, ...] = (),
        supervised_cutoff_selections: tuple[SupervisedCutoffSelection, ...] = (),
    ) -> None:
        self.manifest, self.registry = manifest, registry
        self.accepted_by_producer_kind = accepted_by_producer_kind
        self.training_settings_revisions = dict(training_settings_revisions or {})
        self.segmentation_settings_revisions = dict(segmentation_settings_revisions or {})
        self.measurement_settings_revisions = dict(measurement_settings_revisions or {})
        self.settled_review_revisions = dict(settled_review_revisions or {})
        self.cell_corrections = cell_corrections
        self.supervised_cutoff_selections = supervised_cutoff_selections
        self.saved_result_retirement_barriers: dict[str, list[_Context]] = {}
        self.secondary_definition_selections: dict[str, object] = {}
        self.secondary_definition_contexts: dict[str, _Context] = {}
        self.samples = {item.sample_id: item for item in manifest.enabled_samples}
        self.nodes: list[BatchNode] = []
        self.edges: list[BatchArtifactEdge] = []
        self.deferred: list[DeferredNodeDescriptor] = []

    def spec(self, kind: str) -> StageAdapterSpec:
        try:
            return self.registry.resolve(kind, _node_version(kind))
        except BatchArtifactError as exc:
            raise BatchPlanError(f"missing adapter for {kind}/{_node_version(kind)}") from exc

    def configs(self, participants: tuple[str, ...]) -> tuple[ArtifactConfigurationBinding, ...]:
        return tuple(
            ArtifactConfigurationBinding(item, self.samples[item].resolved_config_sha256)
            for item in sorted(participants)
        )

    def secondary_structures(self, sample_id: str):
        from .batch_secondary_definitions import resolve_secondary_structures

        return resolve_secondary_structures(
            self.samples[sample_id], self.secondary_definition_selections.get(sample_id),
        )

    def output_scope(self, context: _Context) -> ArtifactScope:
        node = context.node
        pooled_clustering = node.kind in {
            "clustering_pooled",
            "cluster_review_evidence",
            "cluster_label_review",
        }
        if node.scope is NodeScope.SAMPLE or (
            node.scope is NodeScope.SHARED_PROJECT
            and len(node.participant_ids) == 1
            and not pooled_clustering
        ):
            sample = self.samples[node.participant_ids[0]]
            entry = context.project_entry_ids[0] if len(context.project_entry_ids) == 1 else None
            return ArtifactScope(
                context.validation_domain_id,
                sample.sample_id,
                sample.slide_id,
                entry,
                node.analysis_domain_bindings,
            )
        return ArtifactScope(
            context.validation_domain_id,
            None,
            None,
            None,
            node.analysis_domain_bindings,
        )

    def output_contract(self, context: _Context, kind: str):
        matches = [
            item
            for item in self.spec(context.node.kind).output_identity_contracts
            if item.artifact_kind == kind
        ]
        if len(matches) != 1:
            raise BatchPlanError(f"{context.node.kind} lacks output contract for {kind}")
        return matches[0]

    def template(self, consumer_kind: str, artifact_kind: str) -> StageArtifactRequirement:
        spec = self.spec(consumer_kind)
        matches = [
            item
            for item in (*spec.required_inputs, *spec.optional_inputs)
            if item.artifact_kind == artifact_kind
        ]
        if len(matches) != 1:
            raise BatchPlanError(f"{consumer_kind} lacks one typed input for {artifact_kind}")
        return matches[0]

    def effective_accepted_output(
        self,
        producer: _Context,
        artifact_kind: str,
        accepted: BatchArtifactRef | None,
    ) -> BatchArtifactRef | None:
        indexed = self.accepted_by_producer_kind.get((producer.node.node_id, artifact_kind))
        if accepted is None:
            return indexed
        if indexed is not None and accepted != indexed:
            raise BatchPlanError("accepted artifact differs from indexed producer owner authority")
        return accepted

    def artifact_id(self, producer: _Context, kind: str) -> str:
        return "artifact-" + _digest({"node": producer.node.node_id, "kind": kind})[:48]

    def external_requirement(
        self,
        consumer_kind: str,
        artifact_kind: str,
        accepted: BatchArtifactRef,
    ) -> StageArtifactRequirement:
        if accepted.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
            raise BatchPlanError("external edge requires manifest_external authority")
        template = self.template(consumer_kind, artifact_kind)
        declarations = {
            item.declaration_sha256: item for item in self.manifest.manifest.reusable_artifacts
        }
        declaration = declarations.get(accepted.external_declaration_sha256 or "")
        if declaration is None:
            raise BatchPlanError("external edge lacks manifest declaration authority")
        if (
            accepted.schema_name,
            accepted.schema_version,
            accepted.owning_identity_contract_sha256,
        ) != (
            template.schema_name,
            template.schema_version,
            template.identity_contract.contract_sha256,
        ):
            raise BatchPlanError(f"external {artifact_kind} differs from its owning stage contract")
        participant_ids = tuple(binding.sample_id for binding in accepted.configuration_bindings)
        requirement = replace(
            template,
            schema_name=accepted.schema_name,
            schema_version=accepted.schema_version,
            scope=accepted.scope,
            owning_identity=accepted.owning_identity,
            identity_contract=accepted.identity_contract,
            compatibility_signature_sha256=(accepted.compatibility_signature_sha256),
            producer_version=accepted.producer_version,
            runtime_versions=accepted.runtime_versions,
            configuration_bindings=self.configs(participant_ids),
            origin=ArtifactOrigin.MANIFEST_EXTERNAL,
            producer_node_id=None,
            producer_attempt_id=None,
            producer_attempt_authorization_sha256=None,
        )
        try:
            validate_artifact_edge(accepted, requirement)
        except BatchArtifactError as exc:
            raise BatchPlanError(
                f"external {artifact_kind} is incompatible with {consumer_kind}"
            ) from exc
        return requirement

    def requirement(
        self,
        consumer_kind: str,
        producer: _Context,
        artifact_kind: str,
        artifact_id: str,
        accepted: BatchArtifactRef | None,
    ) -> StageArtifactRequirement:
        template = self.template(consumer_kind, artifact_kind)
        accepted = self.effective_accepted_output(producer, artifact_kind, accepted)
        contract = self.output_contract(producer, artifact_kind)
        if (
            template.schema_name,
            template.schema_version,
            template.identity_contract.contract_sha256,
        ) != (contract.schema_name, contract.schema_version, contract.contract_sha256):
            raise BatchPlanError(
                f"typed edge {producer.node.kind}->{consumer_kind} is incompatible"
            )
        scope = self.output_scope(producer)
        owner_defined_accepted = artifact_kind in {
            "source_inspection",
            "stage1_review",
            "processed_image",
            "analysis_frame",
            "project_catalogue_entry",
            "accepted_analysis_domain",
            "primary_inference_result",
            "secondary_inference_result",
            "segmentation_result",
            "segmentation_correction_result",
            "measurements_result",
            "phenotyping_mode",
            "training_plan_set",
            "primary_training_evidence_snapshot",
            "secondary_training_evidence_snapshot",
            "supervised_training_evidence_snapshot",
            "classifier_record_set",
            "clustered_phenotype_result",
            "cluster_review_evidence",
            "accepted_cluster_label_review",
            "cluster_label_import_result",
            "semantic_raw_inventory",
            "phenotype_view_attestation",
        }
        if accepted is not None:
            owning_identity = dict(accepted.owning_identity)
        elif owner_defined_accepted:
            raise BatchPlanError(f"{artifact_kind} consumer requires accepted owner identity")
        elif artifact_kind in {
            "no_secondary_completion_evidence",
            "semantic_evidence_result",
            "semantic_evaluation_result",
            "final_count_cap_result",
            "semantic_finalization_result",
            "spatial_cell_handoff",
        }:
            # Imported lazily because the shared template builder's registry
            # depends on ``ResourceKind`` from this module.
            from .batch_preprocessing_contracts import nested_identity

            owning_identity = nested_identity(contract.required_fields)
        else:
            owning_identity = (
                {"material_id": artifact_id}
                if contract.required_fields == ("material_id",)
                else dict(template.owning_identity)
            )
        if producer.node.kind == "phenotyping_mode_resolution":
            configured = self.samples[producer.node.participant_ids[0]].config.phenotyping.mode
            raw_mode = (
                accepted.owning_identity.get("selected_mode")
                if accepted is not None
                else None if configured is None else configured.value
            )
            try:
                selected_mode = PhenotypingMode(raw_mode)
            except (TypeError, ValueError) as exc:
                raise BatchPlanError(
                    "phenotyping mode output lacks one closed selected mode"
                ) from exc
            owning_identity["selected_mode"] = selected_mode.value
        if accepted is not None:
            candidate_scope = accepted.scope
            if producer.node.kind == "project_upsert":
                if (
                    candidate_scope.validation_domain_id != scope.validation_domain_id
                    or candidate_scope.sample_id != scope.sample_id
                    or candidate_scope.slide_id != scope.slide_id
                    or candidate_scope.project_entry_id is None
                    or candidate_scope.analysis_domain_bindings
                ):
                    raise BatchPlanError("accepted project output has stale scope")
                scope = candidate_scope
            elif producer.node.kind == "analysis_domain_selection":
                if (
                    candidate_scope.validation_domain_id != scope.validation_domain_id
                    or candidate_scope.sample_id != scope.sample_id
                    or candidate_scope.slide_id != scope.slide_id
                    or candidate_scope.project_entry_id != scope.project_entry_id
                    or not candidate_scope.analysis_domain_bindings
                ):
                    raise BatchPlanError("accepted domain output has stale scope")
                scope = candidate_scope
            elif producer.node.kind == "semantic_raw_inventory_snapshot":
                if candidate_scope != scope:
                    raise BatchPlanError("accepted semantic inventory has stale scope")
                owning_identity.update(
                    {
                        "configured_secondary_ids": _inventory(
                            accepted.owning_identity.get("configured_secondary_ids"),
                            "configured secondary inventory",
                        ),
                        "candidate_secondary_roi_ids": _inventory(
                            accepted.owning_identity.get("candidate_secondary_roi_ids"),
                            "candidate secondary inventory",
                        ),
                    }
                )
            elif artifact_kind in {
                "training_plan_set",
                "primary_training_evidence_snapshot",
                "secondary_training_evidence_snapshot",
                "supervised_training_evidence_snapshot",
                "classifier_record_set",
            }:
                if candidate_scope != scope:
                    raise BatchPlanError("accepted classifier record set has stale scope")
                owning_identity = dict(accepted.owning_identity)
            elif candidate_scope != scope:
                raise BatchPlanError(f"accepted {artifact_kind} has stale scope")
        requirement = replace(
            template,
            scope=self.output_scope(producer),
            owning_identity=owning_identity,
            identity_contract=contract,
            configuration_bindings=self.configs(producer.node.participant_ids),
            origin=None,
            producer_node_id=None,
            producer_attempt_id=None,
            producer_attempt_authorization_sha256=None,
        )
        if accepted is not None:
            requirement = replace(
                requirement,
                owning_identity=accepted.owning_identity,
                compatibility_signature_sha256=(accepted.compatibility_signature_sha256),
                producer_version=accepted.producer_version,
                runtime_versions=accepted.runtime_versions,
            )
        if scope != requirement.scope:
            requirement = replace(requirement, scope=scope)
        if accepted is None:
            return requirement
        try:
            validate_artifact_edge(accepted, requirement)
        except BatchArtifactError as exc:
            raise BatchPlanError(
                f"accepted {artifact_kind} is incompatible with {consumer_kind}"
            ) from exc
        return replace(
            requirement,
            origin=ArtifactOrigin.INTERNAL_NODE,
            producer_node_id=accepted.producer_node_id,
            producer_attempt_id=accepted.producer_attempt_id,
            producer_attempt_authorization_sha256=(accepted.producer_attempt_authorization_sha256),
        )

    def _validate_primary_skip_inputs(self, matching, selected_bindings, validation_domain):
        """Project a shared explicit Skip onto exactly the selected analysis regions."""
        from .batch_training.planning import validate_primary_skip_artifact_ref

        coverage = {binding: 0 for binding in selected_bindings}
        for producer, _kind, _artifact_id, requirement, ref_sha in matching:
            if producer is None or producer.node.kind != "primary_classifier_training_plan_review":
                raise BatchPlanError("Primary Skip requires its accepted primary setup owner")
            ref = self.accepted_by_producer_kind.get((producer.node.node_id, "training_plan_set"))
            if (ref is None or ref_sha != ref.artifact_ref_sha256
                    or requirement.scope.validation_domain_id != validation_domain
                    or producer.node.validation_domain_id != validation_domain
                    or tuple(requirement.configuration_bindings) != self.configs(producer.node.participant_ids)
                    or tuple(ref.configuration_bindings) != tuple(requirement.configuration_bindings)):
                raise BatchPlanError("Primary Skip has stale or foreign participant authority")
            covered = tuple(binding for binding in requirement.scope.analysis_domain_bindings
                            if binding in coverage)
            if not covered:
                raise BatchPlanError("Primary Skip does not cover a selected analysis region")
            validate_primary_skip_artifact_ref(ref, domain_bindings=covered)
            for binding in covered:
                coverage[binding] += 1
        if not coverage or any(count != 1 for count in coverage.values()):
            raise BatchPlanError("Primary Skip requires exact selected analysis-region coverage")

    def add(
        self,
        kind: str,
        *,
        scope: NodeScope,
        participants: tuple[str, ...],
        validation_domain: str | None,
        bindings: tuple[AnalysisDomainBinding, ...] = (),
        entries: tuple[str, ...] = (),
        structure_id: str | None = None,
        classifier_id: str | None = None,
        eligible_parent_ids: tuple[str, ...] = (),
        section_target: PhysicalSectionTarget | None = None,
        clustering_scope: ClusteringGraphScope | None = None,
        inputs: tuple[tuple[_Context | None, str, BatchArtifactRef | None], ...] = (),
        extra: tuple[_Context, ...] = (),
        saved_result_retirement: SavedResultRetirement | None = None,
        cell_correction: CellCorrectionRequest | None = None,
        paired_reference: PairedMovingReferenceAuthority | None = None,
        training_plan_projection: object | None = None,
    ) -> _Context:
        spec = self.spec(kind)
        participants = tuple(sorted(participants))
        bindings = tuple(sorted(bindings, key=_binding_key))
        entry_ids = tuple(sorted(entries))
        edges: list[
            tuple[
                _Context | None,
                str,
                str,
                StageArtifactRequirement,
                str | None,
            ]
        ] = []
        input_material: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        artifact_ids: list[str] = []
        actual_kinds: list[str] = []
        for producer, artifact_kind, accepted in inputs:
            if producer is None:
                if accepted is None:
                    raise BatchPlanError("external input lacks an accepted reference")
                artifact_id = accepted.artifact_id
                requirement = self.external_requirement(kind, artifact_kind, accepted)
            else:
                effective_accepted = self.effective_accepted_output(
                    producer, artifact_kind, accepted
                )
                artifact_id = (
                    effective_accepted.artifact_id
                    if effective_accepted is not None
                    else self.artifact_id(producer, artifact_kind)
                )
                requirement = self.requirement(
                    kind,
                    producer,
                    artifact_kind,
                    artifact_id,
                    effective_accepted,
                )
                accepted = effective_accepted
            ref_sha = accepted.artifact_ref_sha256 if accepted is not None else None
            artifact_key = ref_sha or artifact_id
            if artifact_key in seen_keys:
                raise BatchPlanError(f"{kind} has a duplicate input artifact")
            seen_keys.add(artifact_key)
            artifact_ids.append(artifact_id)
            actual_kinds.append(artifact_kind)
            edges.append((producer, artifact_kind, artifact_id, requirement, ref_sha))
            input_material.append(
                _input_edge_material(
                    artifact_id=artifact_id,
                    artifact_kind=artifact_kind,
                    requirement=requirement,
                    accepted_artifact_ref_sha256=ref_sha,
                )
            )
        required_kinds = {item.artifact_kind for item in spec.required_inputs}
        allowed_kinds = required_kinds | {item.artifact_kind for item in spec.optional_inputs}
        actual_kind_set = set(actual_kinds)
        missing = sorted(required_kinds - actual_kind_set)
        extra_kinds = sorted(actual_kind_set - allowed_kinds)
        if missing or extra_kinds:
            raise BatchPlanError(
                f"{kind} typed inputs have missing required input(s) {missing!r} "
                f"or undeclared input(s) {extra_kinds!r}"
            )
        spec_requirements = {
            item.artifact_kind: item for item in (*spec.required_inputs, *spec.optional_inputs)
        }
        secondary_requirement = spec_requirements.get("secondary_inference_result")
        expected_secondary_pairs = set()
        if secondary_requirement is not None and secondary_requirement.multiplicity is ArtifactMultiplicity.EXACT_SET:
            from .batch_classifier_inference_artifacts import needed_secondary_structure_ids
            for binding in bindings:
                configured = self.secondary_structures(binding.sample_id)
                ids = (
                    needed_secondary_structure_ids(configured, structure_id)
                    if kind == "secondary_classifier_inference"
                    else tuple(item.structure_id for item in configured)
                )
                expected_secondary_pairs.update((binding, selected_id) for selected_id in ids)
            if expected_secondary_pairs and "secondary_inference_result" not in actual_kind_set:
                raise BatchPlanError(f"{kind} requires its exact secondary structure coverage")
        late_cell_kinds = {
            "measurements_result": "measurements",
            "marker_phenotype_result": "marker_rule_application",
            "supervised_phenotype_result": "supervised_phenotype_inference",
            "cluster_label_import_result": "clustering_label_import",
        }
        if kind == "secondary_structure_definition":
            # Phenotype modes may differ between sections in one group. Their
            # combined cover, rather than each mode alone, must be complete.
            expected_bindings = set(bindings)
            for kinds in ({"measurements_result"}, set(late_cell_kinds) - {"measurements_result"}):
                coverage = {binding: 0 for binding in bindings}
                for producer, artifact_kind, _artifact_id, requirement, ref_sha in edges:
                    if artifact_kind not in kinds:
                        continue
                    covered = set(requirement.scope.analysis_domain_bindings)
                    if (producer is None or producer.node.kind != late_cell_kinds[artifact_kind]
                            or ref_sha is None or not covered or not covered <= expected_bindings
                            or not set(requirement.configuration_bindings) <= set(self.configs(participants))):
                        raise BatchPlanError("secondary definition requires current section cell results")
                    for binding in covered:
                        coverage[binding] += 1
                if any(count != 1 for count in coverage.values()):
                    raise BatchPlanError("secondary definition requires one measurement and phenotype result per region")
        for artifact_kind in sorted(actual_kind_set):
            multiplicity = spec_requirements[artifact_kind].multiplicity
            matching = [edge for edge in edges if edge[1] == artifact_kind]
            if kind == "secondary_structure_definition" and artifact_kind in late_cell_kinds:
                continue
            if artifact_kind == "training_plan_set" and kind in {
                "segmentation", "secondary_structure_definition",
                "secondary_classifier_inference", "semantic_raw_inventory_snapshot",
            }:
                classified = {binding for edge in edges if edge[1] == "primary_inference_result"
                              for binding in edge[3].scope.analysis_domain_bindings}
                self._validate_primary_skip_inputs(matching, set(bindings) - classified, validation_domain)
                continue
            if (
                kind in {"segmentation", "supervised_phenotype_inference"}
                and artifact_kind == "accepted_analysis_domain"
            ):
                expected_bindings = set(bindings)
                coverage = {binding: 0 for binding in expected_bindings}
                for (
                    producer,
                    _kind,
                    _artifact_id,
                    requirement,
                    ref_sha,
                ) in matching:
                    covered = set(requirement.scope.analysis_domain_bindings) & expected_bindings
                    if (
                        producer is None
                        or producer.node.kind != "analysis_domain_selection"
                        or ref_sha is None
                        or not covered
                    ):
                        raise BatchPlanError(
                            f"{kind} requires authenticated accepted-domain " "section coverage"
                        )
                    for binding in covered:
                        coverage[binding] += 1
                if any(count != 1 for count in coverage.values()):
                    raise BatchPlanError(f"{kind} requires exact accepted-domain section coverage")
                continue
            if kind == "segmentation" and artifact_kind == "primary_inference_result":
                expected_bindings = set(bindings)
                actual_bindings = {
                    producer.node.analysis_domain_bindings[0]
                    for producer, _kind, _artifact_id, _requirement, _ref_sha in matching
                    if producer is not None and len(producer.node.analysis_domain_bindings) == 1
                }
                if len(actual_bindings) != len(matching) or actual_bindings != expected_bindings:
                    raise BatchPlanError("segmentation requires exact primary domain coverage")
                continue
            if (
                multiplicity is ArtifactMultiplicity.EXACT_SET
                and artifact_kind == "secondary_inference_result"
            ):
                expected_pairs = expected_secondary_pairs
                actual_pairs = set()
                for (
                    producer,
                    _kind,
                    _artifact_id,
                    _requirement,
                    _ref_sha,
                ) in matching:
                    if (
                        producer is None
                        or producer.node.kind != "secondary_classifier_inference"
                        or len(producer.node.analysis_domain_bindings) != 1
                        or producer.node.structure_id is None
                    ):
                        raise BatchPlanError(f"{kind} requires exact secondary structure coverage")
                    actual_pairs.add(
                        (
                            producer.node.analysis_domain_bindings[0],
                            producer.node.structure_id,
                        )
                    )
                if len(actual_pairs) != len(matching) or actual_pairs != expected_pairs:
                    raise BatchPlanError(f"{kind} requires exact secondary structure coverage")
                continue
            if (
                kind == "secondary_training_evidence_snapshot"
                and artifact_kind == "primary_inference_result"
            ):
                expected_bindings = set(bindings)
                actual_bindings = {
                    producer.node.analysis_domain_bindings[0]
                    for producer, _kind, _artifact_id, _requirement, _ref_sha in matching
                    if producer is not None and len(producer.node.analysis_domain_bindings) == 1
                }
                if len(actual_bindings) != len(matching) or actual_bindings != expected_bindings:
                    raise BatchPlanError(
                        "secondary evidence requires exact primary binding coverage"
                    )
                continue
            if kind in {
                "supervised_phenotype_training_plan_review",
                "supervised_training_evidence_snapshot",
            } and artifact_kind in {"measurements_result", "phenotyping_mode"}:
                expected_bindings = set(bindings)
                coverage = {binding: 0 for binding in expected_bindings}
                seen_producers: set[str] = set()
                expected_producer_kind = {
                    "measurements_result": "measurements",
                    "phenotyping_mode": "phenotyping_mode_resolution",
                }[artifact_kind]
                for (
                    producer,
                    _kind,
                    _artifact_id,
                    requirement,
                    _ref_sha,
                ) in matching:
                    covered = set(requirement.scope.analysis_domain_bindings)
                    if (
                        producer is None
                        or producer.node.kind != expected_producer_kind
                        or producer.node.node_id in seen_producers
                        or not covered
                        or not covered <= expected_bindings
                    ):
                        raise BatchPlanError(
                            "supervised training requires unique binding-scoped "
                            f"{artifact_kind} authorities"
                        )
                    seen_producers.add(producer.node.node_id)
                    for binding in covered:
                        coverage[binding] += 1
                if any(count != 1 for count in coverage.values()):
                    raise BatchPlanError(
                        "supervised training requires exact per-binding "
                        f"{artifact_kind} coverage"
                    )
                continue
            if kind in {
                "clustering_per_slide",
                "clustering_pooled",
                "cluster_review_evidence",
            } and artifact_kind in {"measurements_result", "unsupervised_preparation_result"}:
                expected_bindings = set(bindings)
                coverage = {binding: 0 for binding in expected_bindings}
                seen_producers: set[str] = set()
                expected_producer_kind = {
                    "measurements_result": "measurements",
                    "unsupervised_preparation_result": ("unsupervised_preparation"),
                }[artifact_kind]
                for producer, _kind, _artifact_id, _requirement, _ref_sha in matching:
                    if (
                        producer is None
                        or producer.node.kind != expected_producer_kind
                        or producer.node.node_id in seen_producers
                    ):
                        raise BatchPlanError(f"{kind} requires unique {artifact_kind} authorities")
                    seen_producers.add(producer.node.node_id)
                    covered = set(producer.node.analysis_domain_bindings) & expected_bindings
                    if not covered:
                        raise BatchPlanError(
                            f"{kind} {artifact_kind} authority is outside graph scope"
                        )
                    for binding in covered:
                        coverage[binding] += 1
                if any(count != 1 for count in coverage.values()):
                    raise BatchPlanError(f"{kind} requires exact {artifact_kind} binding coverage")
                continue
            if (
                kind
                in {
                    "unsupervised_preparation",
                    "supervised_phenotype_inference",
                }
                and artifact_kind == "phenotyping_mode"
            ) or (
                kind
                in {
                    "clustering_per_slide",
                    "clustering_pooled",
                }
                and artifact_kind == "phenotyping_mode"
            ):
                expected_bindings = set(bindings)
                actual_bindings = {
                    producer.node.analysis_domain_bindings[0]
                    for producer, _kind, _artifact_id, _requirement, _ref_sha in matching
                    if producer is not None and len(producer.node.analysis_domain_bindings) == 1
                }
                if len(actual_bindings) != len(matching) or actual_bindings != expected_bindings:
                    raise BatchPlanError(f"{kind} requires exact {artifact_kind} binding coverage")
                continue
            if multiplicity is ArtifactMultiplicity.SINGLE:
                if len(matching) != 1:
                    raise BatchPlanError(f"single input {artifact_kind} requires exactly one edge")
                continue
            if multiplicity is ArtifactMultiplicity.EXACT_SET:
                expected_configurations = set(self.configs(participants))
                if training_plan_projection is not None and artifact_kind in {
                    "primary_training_evidence_snapshot",
                    "secondary_training_evidence_snapshot",
                    "supervised_training_evidence_snapshot",
                }:
                    # The parent retains its application cohort; reference
                    # evidence covers only entries selected in its accepted plans.
                    reference_entries = {
                        entry.project_entry_id: entry.sample_id
                        for plan in training_plan_projection.plan_set.plans
                        for entry in plan.request.entries
                    }
                    observed_entries = [item[3].scope.project_entry_id for item in matching]
                    if (len(observed_entries) != len(reference_entries)
                            or set(observed_entries) != set(reference_entries)):
                        raise BatchPlanError("training evidence differs from selected plan entries")
                    expected_configurations = set(self.configs(tuple(set(reference_entries.values()))))
                covered_configurations: set[ArtifactConfigurationBinding] = set()
                for (
                    _producer,
                    _kind,
                    _artifact_id,
                    requirement,
                    _ref_sha,
                ) in matching:
                    actual_configurations = set(requirement.configuration_bindings)
                    if (
                        not actual_configurations
                        or not actual_configurations <= expected_configurations
                    ):
                        raise BatchPlanError(
                            f"exact-set input {artifact_kind} has stale or foreign "
                            "participant configuration"
                        )
                    covered_configurations.update(actual_configurations)
                if covered_configurations != expected_configurations:
                    raise BatchPlanError(
                        f"exact-set input {artifact_kind} has missing or foreign participants"
                    )
                continue
            participant_edges: dict[str, StageArtifactRequirement] = {}
            for _producer, _kind, _artifact_id, requirement, _ref_sha in matching:
                if len(requirement.configuration_bindings) != 1:
                    raise BatchPlanError(
                        f"per-participant input {artifact_kind} must bind one participant"
                    )
                sample_id = requirement.configuration_bindings[0].sample_id
                if sample_id in participant_edges:
                    raise BatchPlanError(
                        f"per-participant input {artifact_kind} duplicates {sample_id}"
                    )
                participant_edges[sample_id] = requirement
            if set(participant_edges) != set(participants):
                raise BatchPlanError(
                    f"per-participant input {artifact_kind} has missing or foreign participants"
                )
            for sample_id, requirement in participant_edges.items():
                expected_bindings = tuple(item for item in bindings if item.sample_id == sample_id)
                if (
                    requirement.configuration_bindings != self.configs((sample_id,))
                    or requirement.scope.validation_domain_id != validation_domain
                    or (
                        artifact_kind
                        in {
                            "accepted_analysis_domain",
                            "primary_inference_result",
                            "secondary_training_evidence_snapshot",
                        }
                        and expected_bindings
                        and requirement.scope.analysis_domain_bindings != expected_bindings
                    )
                ):
                    raise BatchPlanError(
                        f"per-participant input {artifact_kind} has stale domain/config scope"
                    )
        if kind == "supervised_phenotype_training_plan_review":
            # Measurement tasks remain concrete while later phenotype choices
            # are deferred. Carry their one explicit accepted reference owner
            # into the rebuilt setup; competing owners never select history.
            key = review_scope_key(kind, validation_domain, participants, bindings)
            candidates = {item.supervised_reference_predecessor
                for (sample_id, _section_id), item in self.measurement_settings_revisions.items()
                if sample_id in participants and item.supervised_reference_predecessor is not None}
            current = self.settled_review_revisions.get(key)
            if current is not None:
                candidates.add(current)
            if candidates:
                latest = max(item.revision for item in candidates)
                selected = tuple(item for item in candidates if item.revision == latest)
                if len(selected) == 1:
                    self.settled_review_revisions[key] = selected[0]
        revision = (self.segmentation_settings_revisions.get((participants[0], section_target.section_id))
                    if kind == "segmentation" and section_target is not None else
                    self.measurement_settings_revisions.get((participants[0], section_target.section_id))
                    if kind == "measurements" and section_target is not None else
                    self.settled_review_revisions.get(review_scope_key(kind, validation_domain, participants, bindings, section_target)) if kind in SETTLED_REVIEW_KINDS else None)
        if revision is not None:
            extra = (*extra, *self.saved_result_retirement_barriers.get(revision.predecessor_node_id, ()))
        if cell_correction is not None:
            extra = (*extra, *self.saved_result_retirement_barriers.get(cell_correction.predecessor_node_id, ()))
        if kind != "secondary_structure_definition":
            extra = (*extra, *(self.secondary_definition_contexts[sample_id]
                              for sample_id in participants
                              if sample_id in self.secondary_definition_contexts))
        dependencies = tuple(
            sorted(
                {
                    item.node.node_id
                    for item in (
                        *(edge[0] for edge in edges if edge[0] is not None),
                        *extra,
                    )
                }
            )
        )
        configurations = self.configs(participants)
        input_edge_material_sha256s = tuple(sorted(_digest(item) for item in input_material))
        resource = ResourceKind(spec.resource_kind)
        training_revision = self.training_settings_revisions.get((kind, validation_domain))
        segmentation_revision = (
            self.segmentation_settings_revisions.get((participants[0], section_target.section_id))
            if kind == "segmentation" and section_target is not None else None
        )
        measurement_revision = (
            self.measurement_settings_revisions.get((participants[0], section_target.section_id))
            if kind == "measurements" and section_target is not None else None
        )
        review_revision = self.settled_review_revisions.get(review_scope_key(kind, validation_domain, participants, bindings, section_target)) if kind in SETTLED_REVIEW_KINDS else None
        material = {
            "batch_id": self.manifest.manifest.batch_id,
            "kind": kind,
            "version": _node_version(kind),
            "scope": scope.value,
            "validation_domain_id": validation_domain,
            "participant_ids": list(participants),
            "analysis_domain_bindings": [item.to_mapping() for item in bindings],
            "project_entry_ids": list(entry_ids),
            "configuration_bindings": [item.to_mapping() for item in configurations],
            "dependency_ids": list(dependencies),
            "input_artifact_ids": list(sorted(artifact_ids)),
            "input_edge_material_sha256s": list(input_edge_material_sha256s),
            "declared_output_kinds": list(spec.declared_outputs),
            "resource": resource.value,
            "adapter_spec_sha256": spec.spec_sha256,
            "dispatch_mode": spec.dispatch_mode.value,
        }
        if structure_id is not None:
            material["secondary_target"] = {
                "structure_id": structure_id,
                "classifier_id": classifier_id,
                "eligible_parent_ids": list(eligible_parent_ids),
            }
        if section_target is not None:
            material["section_target"] = section_target.to_mapping()
        if clustering_scope is not None:
            material["clustering_scope"] = clustering_scope.to_mapping()
        if segmentation_revision is not None:
            material["segmentation_settings_revision"] = segmentation_revision.to_mapping()
        if measurement_revision is not None:
            material["measurement_settings_revision"] = measurement_revision.to_mapping()
        if review_revision is not None:
            material["settled_review_revision"] = review_revision.to_mapping()
        if paired_reference is not None:
            material["paired_reference"] = paired_reference.to_mapping()
        if saved_result_retirement is not None:
            material["saved_result_retirement"] = saved_result_retirement.to_mapping()
        if cell_correction is not None:
            material["cell_correction"] = cell_correction.to_mapping()
        if kind in _TRAINING_PLAN_REVIEW_KINDS:
            material["training_settings_revision"] = (
                None if training_revision is None else training_revision.to_mapping()
            )
        material_sha = _digest(material)
        node = BatchNode(
            node_id="node-" + material_sha[:48],
            kind=kind,
            version=_node_version(kind),
            batch_id=self.manifest.manifest.batch_id,
            scope=scope,
            validation_domain_id=validation_domain,
            analysis_domain_bindings=bindings,
            participant_ids=participants,
            project_entry_ids=entry_ids,
            configuration_bindings=configurations,
            dependency_ids=dependencies,
            input_artifact_ids=tuple(sorted(artifact_ids)),
            input_edge_material_sha256s=input_edge_material_sha256s,
            declared_output_kinds=spec.declared_outputs,
            resource=resource,
            adapter_spec_sha256=spec.spec_sha256,
            dispatch_mode=spec.dispatch_mode,
            material_sha256=material_sha,
            structure_id=structure_id,
            classifier_id=classifier_id,
            eligible_parent_ids=eligible_parent_ids,
            section_target=section_target,
            clustering_scope=clustering_scope,
            training_settings_revision=training_revision,
            segmentation_settings_revision=segmentation_revision,
            measurement_settings_revision=measurement_revision,
            settled_review_revision=review_revision,
            paired_reference=paired_reference,
            saved_result_retirement=saved_result_retirement,
            cell_correction=cell_correction,
        )
        context = _Context(node, validation_domain, entry_ids)
        self.nodes.append(node)
        self.edges.extend(
            BatchArtifactEdge(
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                producer_node_id=(None if producer is None else producer.node.node_id),
                consumer_node_id=node.node_id,
                requirement=requirement,
                accepted_artifact_ref_sha256=ref_sha,
            )
            for producer, artifact_kind, artifact_id, requirement, ref_sha in edges
        )
        return context

    def defer(
        self,
        kind: str,
        participant_ids: str | Sequence[str],
        unresolved: str,
        prerequisite: str,
        *,
        scope_override: NodeScope | None = None,
    ) -> None:
        spec = self.spec(kind)
        participants = (
            (participant_ids,)
            if isinstance(participant_ids, str)
            else tuple(sorted(participant_ids))
        )
        if scope_override is not None:
            scope = NodeScope(scope_override)
        elif kind in {
            "primary_classifier_training_plan_review",
            "primary_classifier_training_review",
            "secondary_classifier_training_plan_review",
            "secondary_classifier_training_review",
            "secondary_structure_definition",
            "marker_gate_review",
            "marker_combination_selection",
            "clustering_pooled",
            "supervised_phenotype_training_plan_review",
            "supervised_phenotype_training_review",
        }:
            scope = NodeScope.VALIDATION_DOMAIN
        elif kind in {
            "project_upsert",
            "registration",
        }:
            scope = NodeScope.SHARED_PROJECT
        else:
            scope = NodeScope.SAMPLE
        unsigned = {
            "node_kind": kind,
            "node_version": _node_version(kind),
            "scope": scope.value,
            "participant_ids": list(participants),
            "prerequisite_node_kinds": [prerequisite],
            "unresolved_identity_kinds": [unresolved],
            "resource": spec.resource_kind,
        }
        descriptor = DeferredNodeDescriptor(
            kind,
            _node_version(kind),
            scope,
            participants,
            (prerequisite,),
            (unresolved,),
            ResourceKind(spec.resource_kind),
            _digest(unsigned),
        )
        if descriptor.descriptor_sha256 not in {item.descriptor_sha256 for item in self.deferred}:
            self.deferred.append(descriptor)


def _validate_manifest(manifest: ValidatedBatchManifest) -> ValidatedBatchManifest:
    if not isinstance(manifest, ValidatedBatchManifest):
        raise BatchPlanError("manifest must be a ValidatedBatchManifest")
    try:
        authoritative = revalidate_retained_manifest(
            manifest.original_manifest_bytes,
            manifest.normalized_manifest_bytes,
        )
    except Exception as exc:
        raise BatchPlanError("manifest authority cannot be reconstructed") from exc
    if (
        manifest.manifest != authoritative.manifest
        or manifest.original_manifest_sha256 != authoritative.original_manifest_sha256
        or manifest.normalized_manifest_sha256 != authoritative.normalized_manifest_sha256
        or manifest.normalized_manifest_bytes != authoritative.normalized_manifest_bytes
        or manifest.samples != authoritative.samples
    ):
        raise BatchPlanError("manifest differs from its retained authoring bytes")
    return authoritative


def _validate_registry(registry: StageAdapterRegistry) -> None:
    from .batch_classifier_inference_contracts import (
        primary_classifier_inference_result_identity_contract,
        secondary_classifier_inference_result_identity_contract,
    )
    from .batch_phenotyping import (
        accepted_cluster_label_review_identity_contract,
        cluster_label_import_result_identity_contract,
        phenotype_hierarchy_attestation_identity_contract,
        cluster_review_evidence_identity_contract,
        clustering_result_identity_contract,
        marker_section_application_identity_contract,
        no_secondary_completion_identity_contract,
        final_count_cap_result_identity_contract,
        semantic_evaluation_result_identity_contract,
        semantic_evidence_result_identity_contract,
        semantic_finalization_result_identity_contract,
        semantic_raw_inventory_identity_contract,
        supervised_phenotype_result_identity_contract,
        spatial_cell_handoff_identity_contract,
        final_non_spatial_export_result_identity_contract,
    )
    from .accepted_analysis_domain_records import (
        accepted_analysis_domain_identity_contract,
    )
    from .batch_measurement_artifacts import measurements_result_identity_contract
    from .batch_analysis_preflight import analysis_preflight_identity_contract
    from .batch_phenotyping_mode_artifacts import phenotyping_mode_identity_contract
    from .batch_segmentation_artifacts import segmentation_result_identity_contract
    from .batch_tissue_artifacts import tissue_acceptance_result_identity_contract
    from .batch_tissue_setup_artifacts import tissue_setup_result_identity_contract
    from .batch_training import classifier_record_set_identity_contract
    from .batch_training.planning import training_plan_set_identity_contract
    from .batch_project import project_catalogue_entry_identity_contract

    if not isinstance(registry, StageAdapterRegistry):
        raise BatchPlanError("adapter_specs must be a StageAdapterRegistry")
    for kind in _ALL_KINDS:
        if (kind == "secondary_structure_definition"
                and not any(item.node_kind == kind for item in registry.specs)):
            continue
        try:
            spec = registry.resolve(kind, _node_version(kind))
            ResourceKind(spec.resource_kind)
        except (BatchArtifactError, ValueError) as exc:
            raise BatchPlanError(
                f"missing or invalid adapter for {kind}/{_node_version(kind)}"
            ) from exc
        if kind == SAVED_RESULT_RETIREMENT_KIND:
            from .batch_saved_result_retirement import build_saved_result_retirement_spec
            if spec != build_saved_result_retirement_spec():
                raise BatchPlanError("saved result retirement adapter differs from its owner")
            continue
        if kind == CELL_CORRECTION_KIND:
            from .batch_cell_corrections import build_cell_correction_spec
            if spec != build_cell_correction_spec():
                raise BatchPlanError("cell correction adapter differs from its owner")
            continue
        if kind == "secondary_structure_definition":
            from .batch_secondary_definitions import build_secondary_definition_stage_spec
            if spec != build_secondary_definition_stage_spec():
                raise BatchPlanError("secondary definition adapter differs from its owner")
            continue
        expected_output = _EXPECTED_OUTPUT_KIND[kind]
        expected_schema = _EXPECTED_OUTPUT_SCHEMA[kind]
        if spec.declared_outputs != (expected_output,):
            raise BatchPlanError(f"{kind} has the wrong declared output set")
        contract = spec.output_identity_contracts[0]
        if (
            contract.artifact_kind != expected_output
            or (contract.schema_name, contract.schema_version) != expected_schema
        ):
            raise BatchPlanError(f"{kind} has the wrong output contract")
        expected_public_output_contract = (
            tissue_setup_result_identity_contract()
            if kind == "tissue_setup"
            else (
                clustering_result_identity_contract()
                if kind in _CLUSTERING
                else (
                    cluster_review_evidence_identity_contract()
                    if kind == "cluster_review_evidence"
                    else (
                        accepted_cluster_label_review_identity_contract()
                        if kind == "cluster_label_review"
                        else (
                            cluster_label_import_result_identity_contract()
                            if kind == "clustering_label_import"
                            else (
                                semantic_raw_inventory_identity_contract()
                                if kind == "semantic_raw_inventory_snapshot"
                                else (
                                    phenotype_hierarchy_attestation_identity_contract()
                                    if kind == "phenotype_hierarchy_attestation"
                                    else (
                                        no_secondary_completion_identity_contract()
                                        if kind == "no_secondary_completion"
                                        else (
                                            semantic_evidence_result_identity_contract()
                                            if kind == "semantic_evidence"
                                            else (
                                                semantic_evaluation_result_identity_contract()
                                                if kind == "semantic_evaluation"
                                                else (
                                                    final_count_cap_result_identity_contract()
                                                    if kind == "final_count_cap"
                                                    else (
                                                        semantic_finalization_result_identity_contract()
                                                        if kind == "semantic_finalization"
                                                        else (
                                                            primary_classifier_inference_result_identity_contract()
                                                            if kind
                                                            == "primary_classifier_inference"
                                                            else (
                                                                secondary_classifier_inference_result_identity_contract()
                                                                if kind
                                                                == "secondary_classifier_inference"
                                                                else (
                                                                    supervised_phenotype_result_identity_contract()
                                                                    if kind
                                                                    == "supervised_phenotype_inference"
                                                                    else (
                                                                        spatial_cell_handoff_identity_contract()
                                                                        if kind
                                                                        == "phenotype_handoff"
                                                                        else (
                                                                            final_non_spatial_export_result_identity_contract()
                                                                            if kind
                                                                            == "final_non_spatial_export"
                                                                            else None
                                                                        )
                                                                    )
                                                                )
                                                            )
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        if (
            expected_public_output_contract is not None
            and contract != expected_public_output_contract
        ):
            raise BatchPlanError(f"{kind} has the wrong public identity contract")
        expected_public_input_contracts = (
            {
                "accepted_analysis_domain": (accepted_analysis_domain_identity_contract()),
                "analysis_frame": analysis_preflight_identity_contract(),
                "project_catalogue_entry": (project_catalogue_entry_identity_contract()),
                "measurements_result": measurements_result_identity_contract(),
                "phenotyping_mode": phenotyping_mode_identity_contract(),
                "classifier_record_set": classifier_record_set_identity_contract(),
            }
            if kind == "supervised_phenotype_inference"
            else (
                {"clustered_phenotype_result": clustering_result_identity_contract()}
                if kind == "cluster_review_evidence"
                else (
                    {"cluster_review_evidence": cluster_review_evidence_identity_contract()}
                    if kind == "cluster_label_review"
                    else (
                        {
                            "accepted_analysis_domain": (
                                accepted_analysis_domain_identity_contract()
                            ),
                            "accepted_cluster_label_review": (
                                accepted_cluster_label_review_identity_contract()
                            ),
                            "clustered_phenotype_result": clustering_result_identity_contract(),
                            "measurements_result": measurements_result_identity_contract(),
                            "phenotyping_mode": phenotyping_mode_identity_contract(),
                            "segmentation_result": segmentation_result_identity_contract(),
                        }
                        if kind == "clustering_label_import"
                        else (
                            {
                                "analysis_frame": analysis_preflight_identity_contract(),
                                "tissue_acceptance_result": (
                                    tissue_acceptance_result_identity_contract()
                                ),
                                "accepted_analysis_domain": (
                                    accepted_analysis_domain_identity_contract()
                                ),
                                "segmentation_result": segmentation_result_identity_contract(),
                                "measurements_result": measurements_result_identity_contract(),
                            }
                            if kind == "semantic_raw_inventory_snapshot"
                            else (
                                {
                                    "cluster_label_import_result": (
                                        cluster_label_import_result_identity_contract()
                                    ),
                                    "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                }
                                if kind == "phenotype_hierarchy_attestation"
                                else (
                                    {
                                        "analysis_frame": analysis_preflight_identity_contract(),
                                        "accepted_analysis_domain": (
                                            accepted_analysis_domain_identity_contract()
                                        ),
                                        "measurements_result": measurements_result_identity_contract(),
                                        "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                    }
                                    if kind == "no_secondary_completion"
                                    else (
                                        {
                                            "analysis_frame": analysis_preflight_identity_contract(),
                                            "accepted_analysis_domain": (
                                                accepted_analysis_domain_identity_contract()
                                            ),
                                            "measurements_result": measurements_result_identity_contract(),
                                            "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                        }
                                        if kind == "semantic_evidence"
                                        else (
                                            {
                                                "semantic_evidence_result": semantic_evidence_result_identity_contract(),
                                                "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                            }
                                            if kind == "semantic_evaluation"
                                            else (
                                                {
                                                    "semantic_evaluation_result": semantic_evaluation_result_identity_contract(),
                                                    "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                                    "accepted_analysis_domain": accepted_analysis_domain_identity_contract(),
                                                }
                                                if kind == "final_count_cap"
                                                else (
                                                    {
                                                        "semantic_evaluation_result": semantic_evaluation_result_identity_contract(),
                                                        "final_count_cap_result": final_count_cap_result_identity_contract(),
                                                        "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
                                                        "analysis_frame": analysis_preflight_identity_contract(),
                                                        "accepted_analysis_domain": accepted_analysis_domain_identity_contract(),
                                                        "measurements_result": measurements_result_identity_contract(),
                                                    }
                                                    if kind == "semantic_finalization"
                                                    else (
                                                        {
                                                            "analysis_frame": analysis_preflight_identity_contract(),
                                                            "tissue_acceptance_result": (
                                                                tissue_acceptance_result_identity_contract()
                                                            ),
                                                            "accepted_analysis_domain": (
                                                                accepted_analysis_domain_identity_contract()
                                                            ),
                                                            "segmentation_result": segmentation_result_identity_contract(),


                                                        }
                                                        if kind == "phenotype_handoff"
                                                        else (
                                                            {
                                                                "analysis_frame": analysis_preflight_identity_contract(),
                                                                "tissue_acceptance_result": (
                                                                    tissue_acceptance_result_identity_contract()
                                                                ),
                                                                "accepted_analysis_domain": (
                                                                    accepted_analysis_domain_identity_contract()
                                                                ),


                                                                "spatial_cell_handoff": (
                                                                    spatial_cell_handoff_identity_contract()
                                                                ),
                                                            }
                                                            if kind == "final_non_spatial_export"
                                                            else {}
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        expected_public_optional_input_contracts = (
            {
                "primary_inference_result": primary_classifier_inference_result_identity_contract(),
                "training_plan_set": training_plan_set_identity_contract(),
                "secondary_inference_result": (
                    secondary_classifier_inference_result_identity_contract()
                ),
                "cluster_label_import_result": (cluster_label_import_result_identity_contract()),
                "marker_phenotype_result": (marker_section_application_identity_contract()),
                "supervised_phenotype_result": (supervised_phenotype_result_identity_contract()),
            }
            if kind == "semantic_raw_inventory_snapshot"
            else (
                {
                    "marker_phenotype_result": (marker_section_application_identity_contract()),
                    "cluster_label_import_result": (
                        cluster_label_import_result_identity_contract()
                    ),
                    "phenotype_view_attestation": (
                        phenotype_hierarchy_attestation_identity_contract()
                    ),
                    "supervised_phenotype_result": (
                        supervised_phenotype_result_identity_contract()
                    ),
                }
                if kind == "semantic_evidence"
                else (
                    {
                        "marker_phenotype_result": (marker_section_application_identity_contract()),
                        "cluster_label_import_result": (
                            cluster_label_import_result_identity_contract()
                        ),
                        "phenotype_view_attestation": (
                            phenotype_hierarchy_attestation_identity_contract()
                        ),
                        "supervised_phenotype_result": (
                            supervised_phenotype_result_identity_contract()
                        ),
                    }
                    if kind == "no_secondary_completion"
                    else (
                        {
                            "marker_phenotype_result": (
                                marker_section_application_identity_contract()
                            ),
                            "supervised_phenotype_result": (
                                supervised_phenotype_result_identity_contract()
                            ),
                            "cluster_label_import_result": (
                                cluster_label_import_result_identity_contract()
                            ),
                            "phenotype_view_attestation": (
                                phenotype_hierarchy_attestation_identity_contract()
                            ),
                            "no_secondary_completion_evidence": (
                                no_secondary_completion_identity_contract()
                            ),
                            "semantic_finalization_result": (
                                semantic_finalization_result_identity_contract()
                            ),
                        }
                        if kind == "phenotype_handoff"
                        else (
                            {
                                "marker_phenotype_result": (
                                    marker_section_application_identity_contract()
                                ),
                                "supervised_phenotype_result": (
                                    supervised_phenotype_result_identity_contract()
                                ),
                                "cluster_label_import_result": (
                                    cluster_label_import_result_identity_contract()
                                ),
                                "phenotype_view_attestation": (
                                    phenotype_hierarchy_attestation_identity_contract()
                                ),
                                "semantic_evidence_result": (
                                    semantic_evidence_result_identity_contract()
                                ),
                                "semantic_evaluation_result": (
                                    semantic_evaluation_result_identity_contract()
                                ),
                                "no_secondary_completion_evidence": (
                                    no_secondary_completion_identity_contract()
                                ),
                                "semantic_finalization_result": (
                                    semantic_finalization_result_identity_contract()
                                ),
                            }
                            if kind == "final_non_spatial_export"
                            else {}
                        )
                    )
                )
            )
        )
        if kind in {"phenotype_handoff", "final_non_spatial_export"}:
            expected_public_optional_input_contracts.update({
                "measurements_result": measurements_result_identity_contract(),
                "semantic_raw_inventory": semantic_raw_inventory_identity_contract(),
            })
            if kind == "phenotype_handoff":
                expected_public_optional_input_contracts["primary_inference_result"] = primary_classifier_inference_result_identity_contract()
        requirements_by_kind = {
            requirement.artifact_kind: requirement for requirement in spec.required_inputs
        }
        if any(
            requirements_by_kind.get(artifact_kind) is None
            or requirements_by_kind[artifact_kind].identity_contract != expected_contract
            for artifact_kind, expected_contract in expected_public_input_contracts.items()
        ):
            raise BatchPlanError(f"{kind} has the wrong public input identity contract")
        optional_requirements_by_kind = {
            requirement.artifact_kind: requirement for requirement in spec.optional_inputs
        }
        if kind == "secondary_classifier_inference":
            expected_public_optional_input_contracts = {
                "primary_inference_result": primary_classifier_inference_result_identity_contract(),
                "training_plan_set": training_plan_set_identity_contract(),
                "secondary_inference_result": secondary_classifier_inference_result_identity_contract(),
            }
            if (set(optional_requirements_by_kind) != set(expected_public_optional_input_contracts)
                    or optional_requirements_by_kind["secondary_inference_result"].multiplicity is not ArtifactMultiplicity.EXACT_SET
                    or any(optional_requirements_by_kind[key].multiplicity is not ArtifactMultiplicity.SINGLE
                           for key in ("primary_inference_result", "training_plan_set"))):
                raise BatchPlanError("secondary inference requires its accepted primary choice and exact predecessor inputs")
        if any(
            optional_requirements_by_kind.get(artifact_kind) is None
            or optional_requirements_by_kind[artifact_kind].identity_contract != expected_contract
            for artifact_kind, expected_contract in expected_public_optional_input_contracts.items()
        ):
            raise BatchPlanError(f"{kind} has the wrong public optional-input identity contract")
        exact_set_inputs = (
            {"phenotyping_mode"}
            if kind == "unsupervised_preparation"
            else (
                {
                    "measurements_result",
                    "phenotyping_mode",
                    "unsupervised_preparation_result",
                }
                if kind in _CLUSTERING
                else (
                    {
                        "measurements_result",
                        "unsupervised_preparation_result",
                    }
                    if kind == "cluster_review_evidence"
                    else (
                        {"accepted_analysis_domain", "phenotyping_mode"}
                        if kind
                        in {
                            "clustering_label_import",
                            "supervised_phenotype_inference",
                        }
                        else (
                            {"semantic_profile"}
                            if kind in {"semantic_evidence", "semantic_evaluation"}
                            else (
                                {
                                    "accepted_analysis_domain",
                                    "segmentation_result",
                                }
                                if kind == "phenotype_handoff"
                                else (
                                    {
                                        "accepted_analysis_domain",
                                    }
                                    if kind == "final_non_spatial_export"
                                    else set()
                                )
                            )
                        )
                    )
                )
            )
        )
        multiplicity_by_kind = {
            requirement.artifact_kind: requirement.multiplicity
            for requirement in spec.required_inputs
        }
        if any(
            multiplicity_by_kind.get(artifact_kind) is not ArtifactMultiplicity.EXACT_SET
            for artifact_kind in exact_set_inputs
        ):
            raise BatchPlanError(f"{kind} requires EXACT_SET scientific fan-in inputs")
        single_inputs = (
            {
                "analysis_frame",
                "project_catalogue_entry",
                "measurements_result",
                "classifier_record_set",
            }
            if kind == "supervised_phenotype_inference"
            else (
                {
                    "accepted_cluster_label_review",
                    "clustered_phenotype_result",
                    "measurements_result",
                    "segmentation_result",
                }
                if kind == "clustering_label_import"
                else (
                    {
                        "cluster_label_import_result",
                        "semantic_raw_inventory",
                    }
                    if kind == "phenotype_hierarchy_attestation"
                    else (
                        {
                            "analysis_frame",
                            "tissue_acceptance_result",
                            "accepted_analysis_domain",
                            "segmentation_result",
                            "measurements_result",
                        }
                        if kind == "semantic_raw_inventory_snapshot"
                        else (
                            {
                                "analysis_frame",
                                "accepted_analysis_domain",
                                "measurements_result",
                                "semantic_raw_inventory",
                            }
                            if kind == "no_secondary_completion"
                            else (
                                {
                                    "analysis_frame",
                                    "accepted_analysis_domain",
                                    "measurements_result",
                                    "semantic_raw_inventory",
                                }
                                if kind == "semantic_evidence"
                                else (
                                    {"semantic_evidence_result", "semantic_raw_inventory"}
                                    if kind == "semantic_evaluation"
                                    else (
                                        {
                                            "semantic_evaluation_result",
                                            "semantic_raw_inventory",
                                            "accepted_analysis_domain",
                                        }
                                        if kind == "final_count_cap"
                                        else (
                                            {
                                                "semantic_evaluation_result",
                                                "final_count_cap_result",
                                                "semantic_raw_inventory",
                                                "analysis_frame",
                                                "accepted_analysis_domain",
                                                "measurements_result",
                                            }
                                            if kind == "semantic_finalization"
                                            else (
                                                {"analysis_frame", "tissue_acceptance_result"}
                                                if kind == "phenotype_handoff"
                                                else (
                                                    {
                                                        "analysis_frame",
                                                        "tissue_acceptance_result",
                                                        "spatial_cell_handoff",
                                                    }
                                                    if kind == "final_non_spatial_export"
                                                    else set()
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        if any(
            multiplicity_by_kind.get(artifact_kind) is not ArtifactMultiplicity.SINGLE
            for artifact_kind in single_inputs
        ):
            raise BatchPlanError(f"{kind} requires SINGLE scientific owner inputs")
        optional_multiplicity_by_kind = {
            requirement.artifact_kind: requirement.multiplicity
            for requirement in spec.optional_inputs
        }
        if kind == "semantic_evidence" and any(
            optional_multiplicity_by_kind.get(artifact_kind) is not ArtifactMultiplicity.SINGLE
            for artifact_kind in {
                "marker_phenotype_result",
                "cluster_label_import_result",
                "phenotype_view_attestation",
            }
        ):
            raise BatchPlanError("semantic_evidence requires SINGLE phenotype source inputs")
        if kind == "semantic_raw_inventory_snapshot" and (
            set(requirements_by_kind)
            != {
                "analysis_frame",
                "tissue_acceptance_result",
                "accepted_analysis_domain",
                "segmentation_result",
                "measurements_result",
            }
            or set(optional_requirements_by_kind)
            != {
                "primary_inference_result",
                "training_plan_set",
                "secondary_inference_result",
                "cluster_label_import_result",
                "marker_phenotype_result",
                "supervised_phenotype_result",
            }
            or optional_multiplicity_by_kind.get("secondary_inference_result")
            is not ArtifactMultiplicity.EXACT_SET
            or any(
                optional_multiplicity_by_kind.get(artifact_kind) is not ArtifactMultiplicity.SINGLE
                for artifact_kind in {
                    "primary_inference_result",
                    "training_plan_set",
                    "cluster_label_import_result",
                    "marker_phenotype_result",
                    "supervised_phenotype_result",
                }
            )
        ):
            raise BatchPlanError("semantic_raw_inventory_snapshot has the wrong exact owner inputs")
        if kind == "no_secondary_completion" and (
            set(requirements_by_kind)
            != {
                "analysis_frame",
                "accepted_analysis_domain",
                "measurements_result",
                "semantic_raw_inventory",
            }
            or set(optional_requirements_by_kind)
            != {
                "marker_phenotype_result",
                "cluster_label_import_result",
                "supervised_phenotype_result",
                "phenotype_view_attestation",
            }
            or any(
                optional_multiplicity_by_kind.get(artifact_kind) is not ArtifactMultiplicity.SINGLE
                for artifact_kind in optional_requirements_by_kind
            )
        ):
            raise BatchPlanError("no_secondary_completion has the wrong exact owner inputs")
        if kind == "phenotype_handoff" and (
            set(requirements_by_kind)
            != {
                "analysis_frame",
                "tissue_acceptance_result",
                "accepted_analysis_domain",
                "segmentation_result",
            }
            or set(optional_requirements_by_kind)
            != {
                "primary_inference_result",
                "measurements_result",
                "semantic_raw_inventory",
                "marker_phenotype_result",
                "supervised_phenotype_result",
                "cluster_label_import_result",
                "phenotype_view_attestation",
                "no_secondary_completion_evidence",
                "semantic_finalization_result",
            }
            or any(
                optional_multiplicity_by_kind.get(artifact_kind)
                is not ArtifactMultiplicity.EXACT_SET
                for artifact_kind in optional_requirements_by_kind
            )
        ):
            raise BatchPlanError("phenotype_handoff has the wrong exact all-domain inputs")
        if kind == "final_non_spatial_export" and (
            set(requirements_by_kind)
            != {
                "analysis_frame",
                "tissue_acceptance_result",
                "accepted_analysis_domain",
                "spatial_cell_handoff",
            }
            or set(optional_requirements_by_kind)
            != {
                "measurements_result",
                "semantic_raw_inventory",
                "marker_phenotype_result",
                "supervised_phenotype_result",
                "cluster_label_import_result",
                "phenotype_view_attestation",
                "semantic_profile",
                "semantic_evidence_result",
                "semantic_evaluation_result",
                "no_secondary_completion_evidence",
                "semantic_finalization_result",
            }
            or any(
                optional_multiplicity_by_kind.get(artifact_kind)
                is not ArtifactMultiplicity.EXACT_SET
                for artifact_kind in optional_requirements_by_kind
            )
        ):
            raise BatchPlanError("final_non_spatial_export has the wrong exact all-domain inputs")


def _accepted(
    values: tuple[BatchArtifactRef, ...],
    manifest: ValidatedBatchManifest,
    accepted_artifact_authorities: tuple[BatchCommittedArtifactAuthority, ...] = (),
    *,
    expected_active_plan_sha256: str | None = None,
    batch_state_store: object | None = None,
    training_plan_set_store: object | None = None,
) -> tuple[
    tuple[BatchArtifactRef, ...],
    tuple[BatchArtifactRef, ...],
    Mapping[tuple[str, str], BatchArtifactRef],
    Mapping[str, object],
]:
    if not isinstance(values, tuple) or not all(
        isinstance(item, BatchArtifactRef) for item in values
    ):
        raise BatchPlanError("accepted_artifacts must be an exact typed tuple")
    try:
        values = tuple(validate_artifact_ref(item) for item in values)
    except BatchArtifactError as exc:
        raise BatchPlanError("accepted artifact reference is invalid") from exc
    if len({item.artifact_ref_sha256 for item in values}) != len(values):
        raise BatchPlanError("accepted artifact references must be unique")
    for artifact in values:
        try:
            if artifact.artifact_kind == "analysis_frame":
                from .batch_analysis_preflight import (
                    validate_analysis_frame_artifact_ref,
                )

                validate_analysis_frame_artifact_ref(artifact)
            elif artifact.artifact_kind == "project_catalogue_entry":
                from .batch_project import reopen_project_catalogue_entry_artifact

                _catalogue, entry = reopen_project_catalogue_entry_artifact(artifact)
                if entry.image_role != "primary_mif":
                    raise BatchPlanError("project catalogue consumer requires primary_mif entry")
        except BatchPlanError:
            raise
        except ValueError as exc:
            raise BatchPlanError(
                f"accepted {artifact.artifact_kind} lacks public owner authority"
            ) from exc
    external = tuple(
        sorted(
            (item for item in values if item.origin is ArtifactOrigin.MANIFEST_EXTERNAL),
            key=lambda item: item.artifact_ref_sha256,
        )
    )
    internal = tuple(
        sorted(
            (item for item in values if item.origin is ArtifactOrigin.INTERNAL_NODE),
            key=lambda item: item.artifact_ref_sha256,
        )
    )
    declarations = {item.declaration_sha256: item for item in manifest.manifest.reusable_artifacts}
    samples = {item.sample_id: item for item in manifest.enabled_samples}
    for artifact in external:
        declaration = declarations.get(artifact.external_declaration_sha256 or "")
        if declaration is None:
            raise BatchPlanError("external artifact lacks exact manifest owner authority")
        if (
            artifact.artifact_id != declaration.artifact_id
            or artifact.artifact_kind != declaration.artifact_kind
            or artifact.schema_name != declaration.schema_name
            or str(artifact.schema_version) != declaration.schema_version
            or artifact.locator != declaration.locator
            or artifact.sha256 != declaration.sha256
            or artifact.byte_count != declaration.byte_count
            or dict(artifact.owning_identity) != dict(declaration.owning_identity)
            or dict(artifact.runtime_versions) != dict(declaration.runtime_versions)
            or artifact.compatibility_signature_sha256 != declaration.compatibility_signature_sha256
        ):
            raise BatchPlanError("external artifact differs from its manifest declaration")
        for binding in artifact.configuration_bindings:
            sample = samples.get(binding.sample_id)
            if (
                sample is None
                or binding.resolved_config_sha256 != sample.resolved_config_sha256
                or artifact.scope.validation_domain_id != sample.validation_domain_id
            ):
                raise BatchPlanError("external artifact has foreign target authority")
        participant_ids = tuple(item.sample_id for item in artifact.configuration_bindings)
        try:
            target = ArtifactProjectionTarget.from_manifest(
                manifest, participant_ids, artifact.scope
            )
        except BatchArtifactError as exc:
            raise BatchPlanError("external artifact target is not manifest-derived") from exc
        if target.configuration_bindings != artifact.configuration_bindings:
            raise BatchPlanError("external artifact has stale target configuration")
    try:
        authorities = validate_committed_artifact_authority_set(
            accepted_artifact_authorities,
            expected_active_plan_sha256=expected_active_plan_sha256,
        )
    except BatchArtifactError as exc:
        raise BatchPlanError("accepted artifact authorities are invalid") from exc
    task11_kinds = {
        "training_plan_set",
        "primary_training_evidence_snapshot",
        "secondary_training_evidence_snapshot",
        "supervised_training_evidence_snapshot",
        "classifier_record_set",
    }
    cluster_review_kinds = {
        "cluster_review_evidence",
        "accepted_cluster_label_review",
    }
    state_authoritative_refs = {
        item.artifact_ref_sha256
        for item in internal
        if item.artifact_kind
        in {
            "accepted_analysis_domain",
            "primary_inference_result",
            "secondary_inference_result",
            "segmentation_result",
            "measurements_result",
            "phenotyping_mode",
            "secondary_definition_selection",
            "unsupervised_preparation_result",
            *task11_kinds,
            *cluster_review_kinds,
            "cluster_label_import_result",
            "semantic_raw_inventory",
            "phenotype_view_attestation",
        }
    }
    authority_by_ref = {item.artifact_ref.artifact_ref_sha256: item for item in authorities}
    projections: dict[str, object] = {}
    accepted_internal_refs = {item.artifact_ref_sha256 for item in internal}
    if not set(authority_by_ref) <= accepted_internal_refs:
        raise BatchPlanError("committed artifact authority names a nonaccepted internal artifact")
    if not state_authoritative_refs <= set(authority_by_ref):
        raise BatchPlanError(
            "state-authoritative artifacts and authorities must have identical references"
        )
    for ref_sha256 in sorted(state_authoritative_refs):
        authority = authority_by_ref[ref_sha256]
        try:
            if authority.artifact_ref.artifact_kind == "accepted_analysis_domain":
                from .batch_analysis_domain_artifacts import (
                    decode_committed_accepted_analysis_domain,
                )

                projection = decode_committed_accepted_analysis_domain(authority)
            elif authority.artifact_ref.artifact_kind in {
                "primary_inference_result",
                "secondary_inference_result",
            }:
                from .batch_classifier_inference_artifacts import (
                    decode_committed_primary_classifier_inference_result,
                    decode_committed_secondary_classifier_inference_result,
                )

                decoder = (
                    decode_committed_primary_classifier_inference_result
                    if authority.artifact_ref.artifact_kind == "primary_inference_result"
                    else decode_committed_secondary_classifier_inference_result
                )
                projection = decoder(authority)
            elif authority.artifact_ref.artifact_kind == "segmentation_result":
                from .batch_segmentation_artifacts import (
                    decode_committed_segmentation_result,
                )

                projection = decode_committed_segmentation_result(authority)
            elif authority.artifact_ref.artifact_kind == "measurements_result":
                from .batch_measurement_artifacts import (
                    decode_committed_measurements_result,
                )

                projection = decode_committed_measurements_result(authority)
            elif authority.artifact_ref.artifact_kind == "unsupervised_preparation_result":
                from .batch_phenotyping.authority import decode_committed_preparation

                decode_committed_preparation(
                    authority,
                    maximum_byte_count=authority.artifact_ref.byte_count,
                )
                projection = None
            elif authority.artifact_ref.artifact_kind == "secondary_definition_selection":
                from .batch_secondary_definitions import decode_committed_secondary_definition_selection
                projection = decode_committed_secondary_definition_selection(authority, state_store=batch_state_store)
            elif authority.artifact_ref.artifact_kind == "phenotyping_mode":
                from .batch_phenotyping_mode_artifacts import (
                    decode_committed_phenotyping_mode,
                )

                projection = decode_committed_phenotyping_mode(authority)
            elif authority.artifact_ref.artifact_kind in task11_kinds:
                from .batch_state import BatchStateError, BatchStateStore
                from .batch_training import (
                    BatchTrainingError,
                    TrainingPlanSetStore,
                    build_training_stage_adapters,
                    decode_current_committed_training_plan_set,
                )

                try:
                    if type(batch_state_store) is not BatchStateStore:
                        raise BatchArtifactError(
                            "Task 11 artifact requires its exact current state owner"
                        )
                    production = {spec.node_kind: spec for spec in build_training_stage_adapters()}
                    artifact_kind = authority.artifact_ref.artifact_kind
                    if artifact_kind == "training_plan_set":
                        if type(training_plan_set_store) is not TrainingPlanSetStore:
                            raise BatchArtifactError(
                                "training plan set requires its exact durable owner"
                            )
                        projection = decode_current_committed_training_plan_set(
                            authority,
                            store=training_plan_set_store,
                            state_store=batch_state_store,
                        )
                    else:
                        producer_kind = authority.producer_node_kind
                        expected_spec = production.get(producer_kind)
                        if (
                            expected_spec is None
                            or artifact_kind not in expected_spec.declared_outputs
                        ):
                            raise BatchArtifactError(
                                "Task 11 artifact producer kind is not production"
                            )
                        current = batch_state_store.authenticate_available_artifact_producer_spec(
                            authority.artifact_ref,
                            expected_adapter_spec=expected_spec,
                        )
                        if current != authority:
                            raise BatchArtifactError(
                                "Task 11 artifact authority changed while authenticating"
                            )
                        projection = None
                except (BatchStateError, BatchTrainingError) as exc:
                    raise BatchArtifactError(
                        "Task 11 artifact lacks current production authority"
                    ) from exc
            elif authority.artifact_ref.artifact_kind in cluster_review_kinds:
                from .batch_state import BatchStateError, BatchStateStore

                try:
                    if type(batch_state_store) is not BatchStateStore:
                        raise BatchArtifactError(
                            "cluster review artifact requires its exact state owner"
                        )
                    current = batch_state_store.authenticate_available_artifact(
                        authority.artifact_ref
                    )
                    if current != authority:
                        raise BatchArtifactError(
                            "cluster review artifact authority changed while authenticating"
                        )
                    projection = None
                except BatchStateError as exc:
                    raise BatchArtifactError(
                        "cluster review artifact lacks current state authority"
                    ) from exc
            elif authority.artifact_ref.artifact_kind in {
                "cluster_label_import_result",
                "semantic_raw_inventory",
                "phenotype_view_attestation",
            }:
                from .batch_state import BatchStateError, BatchStateStore
                from .batch_phenotyping import (
                    BatchPhenotypingError,
                    decode_committed_cluster_label_import_result,
                    decode_committed_phenotype_hierarchy_attestation,
                    decode_committed_semantic_raw_inventory,
                )

                try:
                    if type(batch_state_store) is not BatchStateStore:
                        raise BatchArtifactError(
                            "Task 13B artifact requires its exact current state owner"
                        )
                    current = batch_state_store.authenticate_available_artifact(
                        authority.artifact_ref
                    )
                    if current != authority:
                        raise BatchArtifactError(
                            "Task 13B artifact authority changed while authenticating"
                        )
                    decoder = {
                        "cluster_label_import_result": (
                            decode_committed_cluster_label_import_result
                        ),
                        "semantic_raw_inventory": (decode_committed_semantic_raw_inventory),
                        "phenotype_view_attestation": (
                            decode_committed_phenotype_hierarchy_attestation
                        ),
                    }[authority.artifact_ref.artifact_kind]
                    projection = decoder(authority)
                except (BatchStateError, BatchPhenotypingError) as exc:
                    raise BatchArtifactError(
                        "Task 13B artifact lacks current state authority"
                    ) from exc
            else:  # pragma: no cover - set construction closes this branch
                raise BatchArtifactError("unsupported committed owner artifact")
        except (
            BatchArtifactError,
            BatchPlanError,
            TypeError,
            ValueError,
        ) as exc:
            raise BatchPlanError(
                f"accepted {authority.artifact_ref.artifact_kind} authority is invalid"
            ) from exc
        if projection is not None and projection.artifact_ref.artifact_ref_sha256 != ref_sha256:
            raise BatchPlanError("committed owner authority does not match its artifact")
        if projection is not None:
            projections[ref_sha256] = projection
    accepted_by_producer_kind: dict[tuple[str, str], BatchArtifactRef] = {}
    for artifact in internal:
        producer_node_id = artifact.producer_node_id
        if producer_node_id is None:
            raise BatchPlanError("accepted internal output lacks producer authority")
        key = (producer_node_id, artifact.artifact_kind)
        if key in accepted_by_producer_kind:
            raise BatchPlanError("accepted internal outputs duplicate a producer artifact kind")
        accepted_by_producer_kind[key] = artifact
    return (
        internal,
        external,
        MappingProxyType(accepted_by_producer_kind),
        MappingProxyType(projections),
    )


def _triggers(
    artifacts: tuple[BatchArtifactRef, ...],
    kind: str,
) -> tuple[BatchArtifactRef, ...]:
    return tuple(item for item in artifacts if item.artifact_kind == kind)


def _one_per_sample(
    artifacts: tuple[BatchArtifactRef, ...],
    kind: str,
) -> dict[str, BatchArtifactRef]:
    result: dict[str, BatchArtifactRef] = {}
    for artifact in _triggers(artifacts, kind):
        sample_id = artifact.scope.sample_id
        if sample_id is None or sample_id in result:
            raise BatchPlanError(f"{kind} requires one exact sample-scoped authority")
        result[sample_id] = artifact
    return result


def _external_one_per_sample(
    artifacts: tuple[BatchArtifactRef, ...],
    kind: str,
) -> dict[str, BatchArtifactRef]:
    result: dict[str, BatchArtifactRef] = {}
    for artifact in _triggers(artifacts, kind):
        if artifact.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
            raise BatchPlanError(f"external {kind} lacks manifest authority")
        for binding in artifact.configuration_bindings:
            if binding.sample_id in result:
                raise BatchPlanError(f"sample has multiple accepted external {kind} inputs")
            result[binding.sample_id] = artifact
    return result


def _external_set_per_sample(
    artifacts: tuple[BatchArtifactRef, ...],
    kind: str,
) -> dict[str, tuple[BatchArtifactRef, ...]]:
    grouped: dict[str, list[BatchArtifactRef]] = {}
    for artifact in _triggers(artifacts, kind):
        if artifact.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
            raise BatchPlanError(f"external {kind} lacks manifest authority")
        for binding in artifact.configuration_bindings:
            grouped.setdefault(binding.sample_id, []).append(artifact)
    result: dict[str, tuple[BatchArtifactRef, ...]] = {}
    for sample_id, values in grouped.items():
        if len({item.artifact_ref_sha256 for item in values}) != len(values):
            raise BatchPlanError(f"sample has duplicate external {kind} inputs")
        result[sample_id] = tuple(sorted(values, key=lambda item: item.artifact_ref_sha256))
    return result


def _semantic_profile_set(
    artifacts: tuple[BatchArtifactRef, ...],
    sample: object,
    secondary_selection=None,
) -> tuple[BatchArtifactRef, ...]:
    from .batch_secondary_definitions import resolve_secondary_structures
    structures = {item.structure_id: item for item in resolve_secondary_structures(sample, secondary_selection)}
    references = {
        (item.profile_id, item.profile_version): item
        for item in (structure.semantic_profile for structure in structures.values())
    }
    by_structure: dict[str, BatchArtifactRef] = {}
    for artifact in artifacts:
        if artifact.locator is None or artifact.sha256 is None:
            raise BatchPlanError("semantic profile lacks its external byte authority")
        try:
            profile = load_semantic_profile_v3(Path(artifact.locator))
        except (OSError, TypeError, ValueError, ROIAnalysisError) as exc:
            raise BatchPlanError("semantic profile bytes are invalid") from exc
        reference = references.get((profile.profile_id, profile.profile_version))
        structure = structures.get(profile.structure_id)
        configured_path = None if reference is None else reference.artifact_path
        if configured_path is not None and not configured_path.is_absolute():
            configured_path = sample.config.output_dir / configured_path
        if (
            reference is None
            or structure is None
            or structure.semantic_profile != reference
            or configured_path is None
            or configured_path.resolve(strict=False) != Path(artifact.locator).resolve(strict=False)
            or artifact.configuration_bindings
            != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
            or artifact.scope.validation_domain_id != sample.validation_domain_id
            or artifact.scope.sample_id not in {None, sample.sample_id}
            or artifact.scope.slide_id not in {None, sample.slide_id}
            or profile.structure_id in by_structure
        ):
            raise BatchPlanError("semantic profile set differs from configuration")
        by_structure[profile.structure_id] = artifact
    if set(by_structure) != set(structures):
        raise BatchPlanError("semantic profile set lacks exact structure coverage")
    return tuple(by_structure[key] for key in sorted(by_structure))


def _secondary_record_set_targets(artifact: BatchArtifactRef) -> tuple[str, ...]:
    """Check only the compact set inventory; the inference owner loads models.

    Classifier IDs and eligible parents are not in a compact artifact reference.
    They are authenticated against the loaded classifier records at application.
    """
    identity = artifact.owning_identity
    if identity.get("classifier_kind") != "secondary_structure":
        raise BatchPlanError("secondary classifier record set has the wrong classifier kind")
    inventory = identity.get("output_inventory")
    if not isinstance(inventory, tuple) or not inventory:
        raise BatchPlanError("secondary classifier record set lacks its complete output inventory")
    structures = set()
    records = set()
    for item in inventory:
        if not isinstance(item, tuple) or len(item) != 2:
            raise BatchPlanError("secondary classifier record set output inventory is invalid")
        structure = _text(item[0], "secondary structure_id")
        digest = _sha(item[1], "secondary classifier record digest")
        if structure in structures or digest in records:
            raise BatchPlanError(
                "secondary classifier record set output inventory is not bijective"
            )
        structures.add(structure)
        records.add(digest)
    return tuple(sorted(structures))


def _configured_secondary_targets(
    builder: _Builder,
    participant_ids: tuple[str, ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    from mif_pipeline.pipeline_core.config import secondary_classifier_groups

    by_structure: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for sample_id in participant_ids:
        structures = builder.secondary_structures(sample_id)
        secondary_classifier_groups(structures)
        for structure in structures:
            target = (
                structure.structure_id,
                structure.classifier.classifier_id,
                tuple(structure.eligible_parent_ids),
            )
            prior_target = by_structure.setdefault(structure.structure_id, target)
            if prior_target != target:
                raise BatchPlanError(
                    "secondary training participants have conflicting shared " "secondary identity"
                )
    return tuple(by_structure[key] for key in sorted(by_structure))


def _validate_secondary_record_set_coverage(
    artifact: BatchArtifactRef,
    builder: _Builder,
    participant_ids: tuple[str, ...],
) -> None:
    _configured_secondary_targets(builder, participant_ids)
    output_ids = {
        structure.classifier.classifier_id
        for sample_id in participant_ids
        for structure in builder.secondary_structures(sample_id)
    }
    if _secondary_record_set_targets(artifact) != tuple(sorted(output_ids)):
        raise BatchPlanError(
            "secondary classifier record set does not exactly cover configured structures"
        )


def _external_classifiers(
    artifacts: tuple[BatchArtifactRef, ...],
    builder: _Builder,
) -> dict[tuple[str, str], BatchArtifactRef]:
    result: dict[tuple[str, str], BatchArtifactRef] = {}
    for artifact in _triggers(artifacts, "classifier_record_set"):
        if artifact.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
            continue
        role = {"primary_compartment": "primary", "secondary_structure": "secondary"}.get(
            artifact.owning_identity.get("classifier_kind")
        )
        if role is None:
            raise BatchPlanError("external pixel classifier set role is unsupported")
        for binding in artifact.configuration_bindings:
            key = (role, binding.sample_id)
            if key in result:
                raise BatchPlanError("sample has multiple external classifiers for one role")
            result[key] = artifact
    return result


def _validate_output(
    artifact: BatchArtifactRef,
    producer: _Context,
    builder: _Builder,
    consumer_kind: str,
) -> None:
    _validate_output_authority(artifact, producer, builder)
    builder.requirement(
        consumer_kind,
        producer,
        artifact.artifact_kind,
        artifact.artifact_id,
        artifact,
    )


def _validate_output_authority(
    artifact: BatchArtifactRef,
    producer: _Context,
    builder: _Builder,
) -> None:
    """Validate output authority for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if artifact.origin is not ArtifactOrigin.INTERNAL_NODE:
        raise BatchPlanError("dynamic artifact must have internal_node origin")
    if artifact.producer_node_id != producer.node.node_id:
        raise BatchPlanError(f"accepted {artifact.artifact_kind} has the wrong producer")
    if artifact.artifact_kind not in producer.node.declared_output_kinds:
        raise BatchPlanError("accepted artifact is not a declared output")
    if (
        artifact.owning_identity_contract_sha256
        != builder.output_contract(producer, artifact.artifact_kind).contract_sha256
    ):
        raise BatchPlanError("accepted artifact has the wrong owner contract")
    if artifact.scope.validation_domain_id != producer.validation_domain_id:
        raise BatchPlanError("accepted artifact crosses validation domains")
    if artifact.configuration_bindings != builder.configs(producer.node.participant_ids):
        raise BatchPlanError("accepted artifact has stale participant configuration")
    expected_scope = builder.output_scope(producer)
    if artifact.artifact_kind == "analysis_frame":
        from .batch_analysis_preflight import validate_analysis_frame_artifact_ref

        evidence = validate_analysis_frame_artifact_ref(artifact)
        processed_requirements = tuple(
            edge.requirement
            for edge in builder.edges
            if edge.consumer_node_id == producer.node.node_id
            and edge.artifact_kind == "processed_image"
        )
        if len(processed_requirements) != 1:
            raise BatchPlanError("analysis-frame producer lacks one processed image input")
        try:
            expected_image_sha256 = processed_requirements[0].owning_identity["publication"][
                "output_sha256"
            ]
        except KeyError as exc:
            raise BatchPlanError(
                "processed image requirement lacks its published output identity"
            ) from exc
        if (
            evidence.spatial_frame.canonical_image_sha256 != expected_image_sha256
            or artifact.scope.sample_id != expected_scope.sample_id
            or artifact.scope.slide_id != expected_scope.slide_id
            or artifact.scope.project_entry_id is not None
        ):
            raise BatchPlanError(
                "accepted analysis frame differs from decoded image/sample/slide authority"
            )
    elif artifact.artifact_kind == "project_catalogue_entry":
        from .batch_analysis_preflight import BatchAnalysisPreflightEvidence
        from .batch_project import reopen_project_catalogue_entry_artifact

        _catalogue, entry = reopen_project_catalogue_entry_artifact(artifact)
        frame_requirements = tuple(
            edge.requirement
            for edge in builder.edges
            if edge.consumer_node_id == producer.node.node_id
            and edge.artifact_kind == "analysis_frame"
        )
        if len(frame_requirements) != 1:
            raise BatchPlanError("project producer lacks one analysis-frame input")
        frame = BatchAnalysisPreflightEvidence.from_mapping(frame_requirements[0].owning_identity)
        if (
            entry.image_sha256 != frame.spatial_frame.canonical_image_sha256
            or entry.sample_id != expected_scope.sample_id
            or entry.slide_id != expected_scope.slide_id
            or artifact.scope.project_entry_id != entry.project_entry_id
        ):
            raise BatchPlanError(
                "accepted project differs from decoded image/sample/slide/project authority"
            )


def _early_graph(
    builder: _Builder,
    artifacts: tuple[BatchArtifactRef, ...],
    external_artifacts: tuple[BatchArtifactRef, ...],
) -> dict[str, dict[str, _Context]]:
    contexts: dict[str, dict[str, _Context]] = {}
    inspections = _one_per_sample(artifacts, "source_inspection")
    stage1_reviews = _one_per_sample(artifacts, "stage1_review")
    processed_images = _one_per_sample(artifacts, "processed_image")
    analysis_frames = _one_per_sample(artifacts, "analysis_frame")
    external_panel_mappings = _external_one_per_sample(external_artifacts, "reviewed_panel_mapping")
    external_libraries = _external_one_per_sample(external_artifacts, "spectral_reference_library")
    for sample in sorted(builder.manifest.enabled_samples, key=lambda item: item.sample_id):
        common = {
            "scope": NodeScope.SAMPLE,
            "participants": (sample.sample_id,),
            "validation_domain": sample.validation_domain_id,
        }
        inspection = builder.add("inspection", **common)
        sample_contexts = {"inspection": inspection}
        contexts[sample.sample_id] = sample_contexts
        inspected = inspections.get(sample.sample_id)
        if inspected is None:
            continue
        _validate_output(inspected, inspection, builder, "channel_review")
        panel_mapping = external_panel_mappings.get(sample.sample_id)
        channel = builder.add(
            "channel_review",
            **common,
            inputs=((inspection, "source_inspection", inspected),)
            + (
                ((None, "reviewed_panel_mapping", panel_mapping),)
                if panel_mapping is not None
                else ()
            ),
        )
        sample_contexts["channel_review"] = channel
        reviewed = stage1_reviews.get(sample.sample_id)
        if reviewed is None:
            continue
        _validate_output(reviewed, channel, builder, "reference_unmixing")
        external_library = external_libraries.get(sample.sample_id)
        reference_inputs = (
            (inspection, "source_inspection", inspected),
            (channel, "stage1_review", reviewed),
        )
        if external_library is not None:
            reference_inputs += ((None, "spectral_reference_library", external_library),)
        unmixing = builder.add(
            "reference_unmixing",
            **common,
            inputs=reference_inputs,
        )
        sample_contexts["reference_unmixing"] = unmixing
        processed = processed_images.get(sample.sample_id)
        if processed is None:
            continue
        _validate_output(processed, unmixing, builder, "analysis_preflight")
        preflight = builder.add(
            "analysis_preflight",
            **common,
            inputs=((unmixing, "processed_image", processed),),
        )
        sample_contexts["analysis_preflight"] = preflight
        frame = analysis_frames.get(sample.sample_id)
        if frame is None:
            continue
        _validate_output(frame, preflight, builder, "project_upsert")
        upsert = builder.add(
            "project_upsert",
            scope=NodeScope.SHARED_PROJECT,
            participants=(sample.sample_id,),
            validation_domain=sample.validation_domain_id,
            inputs=(
                (preflight, "analysis_frame", frame),
                (unmixing, "processed_image", processed),
            ),
        )
        sample_contexts["project_upsert"] = upsert
    return contexts


def _project_graph(
    builder: _Builder,
    contexts: dict[str, dict[str, _Context]],
    artifacts: tuple[BatchArtifactRef, ...],
    external_artifacts: tuple[BatchArtifactRef, ...],
) -> dict[str, BatchArtifactRef]:
    projects = _primary_project_inputs(artifacts, contexts)
    del external_artifacts
    for sample_id, artifact in sorted(projects.items()):
        if sample_id not in contexts:
            raise BatchPlanError("project catalogue names an unknown sample")
        producer = contexts[sample_id]["project_upsert"]
        _validate_output(artifact, producer, builder, "tissue_setup")
        sample = builder.samples[sample_id]
        from .batch_stage_adapters import (
            ImageStageAdapterRegistryError,
            _require_supported_batch_image_modes,
        )

        try:
            _require_supported_batch_image_modes(sample.config)
        except ImageStageAdapterRegistryError as exc:
            raise BatchPlanError(str(exc)) from exc
        entry = artifact.scope.project_entry_id
        if (
            entry is None
            or artifact.scope.analysis_domain_bindings
            or artifact.owning_identity.get("project_entry_id") != entry
            or (artifact.scope.sample_id, artifact.scope.slide_id)
            != (sample.sample_id, sample.slide_id)
        ):
            raise BatchPlanError("project catalogue lacks exact project-entry authority")
        frame = contexts[sample_id]["analysis_preflight"]
        common = {
            "participants": (sample_id,),
            "validation_domain": sample.validation_domain_id,
            "entries": (entry,),
        }
        setup = builder.add(
            "tissue_setup",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (producer, "project_catalogue_entry", artifact),
            ),
        )
        resolution = builder.add(
            "tissue_resolution",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (producer, "project_catalogue_entry", artifact),
                (setup, "tissue_setup_result", None),
            ),
        )
        channel = builder.add(
            "tissue_channel",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (resolution, "tissue_resolution_result", None),
                (setup, "tissue_setup_result", None),
            ),
        )
        candidate = builder.add(
            "tissue_candidate",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (channel, "tissue_channel_result", None),
                (setup, "tissue_setup_result", None),
            ),
        )
        acceptance = builder.add(
            "tissue_acceptance",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (candidate, "tissue_candidate_result", None),
                (setup, "tissue_setup_result", None),
            ),
        )
        registration_inputs = (
            (frame, "analysis_frame", None),
            (acceptance, "tissue_acceptance_result", None),
        )
        registration_dependencies = ()
        revision = builder.settled_review_revisions.get(
            review_scope_key("registration", sample.validation_domain_id, (sample_id,)))
        if revision is not None and revision.paired_reference is not None:
            # Preparing a different moving image never writes primary qpdata.
            # It and retirement serialize through the project lease, and both
            # precede registration. Keeping retirement/review counters out of
            # this node lets a later landmark review reuse the accepted pair.
            preparation = builder.add(
                "paired_image_preparation", scope=NodeScope.SHARED_PROJECT,
                **common, paired_reference=revision.paired_reference,
                inputs=((frame, "analysis_frame", None),
                        (contexts[sample_id]["reference_unmixing"], "processed_image", None),
                        (producer, "project_catalogue_entry", artifact)),
            )
            contexts[sample_id]["paired_image_preparation"] = preparation
            registration_dependencies = (preparation,)
            prepared_pair = builder.effective_accepted_output(preparation, "project_catalogue_entry", None)
            if prepared_pair is not None:
                registration_inputs += ((preparation, "project_catalogue_entry", prepared_pair),)
        registration = builder.add(
            "registration",
            scope=NodeScope.SHARED_PROJECT,
            **common,
            inputs=registration_inputs,
            extra=registration_dependencies,
        )
        selection = builder.add(
            "analysis_domain_selection",
            scope=NodeScope.SAMPLE,
            **common,
            inputs=(
                (frame, "analysis_frame", None),
                (registration, "registration_result", None),
                (acceptance, "tissue_acceptance_result", None),
            ),
        )
        contexts[sample_id].update(
            {
                item.node.kind: item
                for item in (
                    setup,
                    resolution,
                    channel,
                    candidate,
                    acceptance,
                    registration,
                    selection,
                )
            }
        )
    return projects


def _primary_project_inputs(artifacts, contexts):
    """Keep primary preparation separate from later paired-image publications.

    Both results use the existing catalogue record. Tissue and primary-image
    consumers retain the original producer; registration alone consumes the
    current paired-image preparation.
    """
    original_producers = {items["project_upsert"].node.node_id for items in contexts.values()
                          if "project_upsert" in items}
    return _one_per_sample(tuple(item for item in artifacts
        if item.producer_node_id in original_producers), "project_catalogue_entry")


def _processing_group_members(samples):
    """Use the existing Processing group for every shared phenotyping fit."""
    groups: dict[str, set[str]] = {}
    for sample in samples:
        groups.setdefault(sample.validation_domain_id, set()).add(sample.sample_id)
    return groups


def _domain_authorities(
    builder: _Builder,
    contexts: dict[str, dict[str, _Context]],
    artifacts: tuple[BatchArtifactRef, ...],
    accepted_domain_projections: Mapping[str, object],
) -> tuple[
    dict[tuple[object, ...], BatchArtifactRef],
    dict[tuple[object, ...], _Context],
    dict[str, list[AnalysisDomainBinding]],
    dict[tuple[object, ...], PhysicalSectionTarget],
]:
    refs: dict[tuple[object, ...], BatchArtifactRef] = {}
    selections: dict[tuple[object, ...], _Context] = {}
    groups: dict[str, list[AnalysisDomainBinding]] = {}
    section_targets: dict[tuple[object, ...], PhysicalSectionTarget] = {}
    for artifact in _triggers(artifacts, "accepted_analysis_domain"):
        sample_id = artifact.scope.sample_id
        selection = (
            None
            if sample_id is None
            else contexts.get(sample_id, {}).get("analysis_domain_selection")
        )
        if selection is None:
            raise BatchPlanError("analysis domain exists before project-entry authority")
        _validate_output(
            artifact,
            selection,
            builder,
            "primary_classifier_training_plan_review",
        )
        if (
            not artifact.scope.analysis_domain_bindings
            or artifact.scope.project_entry_id != selection.project_entry_ids[0]
        ):
            raise BatchPlanError("analysis domain lacks exact accepted bindings")
        projection = accepted_domain_projections.get(artifact.artifact_ref_sha256)
        if projection is None:
            raise BatchPlanError("accepted analysis-domain artifact lacks decoded authority")
        projected_domains = {
            (
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
                item.project_entry_id,
                item.slide_id,
            ): item
            for item in projection.domain_authorities
        }
        if len(projected_domains) != len(projection.domain_authorities):
            raise BatchPlanError("accepted analysis-domain owner identities are duplicated")
        sample = builder.samples[sample_id]
        for binding in artifact.scope.analysis_domain_bindings:
            owner_key = (
                binding.analysis_domain_id,
                binding.analysis_domain_revision,
                binding.analysis_domain_revision_sha256,
                binding.project_entry_id,
                binding.slide_id,
            )
            owner = projected_domains.get(owner_key)
            if owner is None:
                raise BatchPlanError(
                    "analysis-domain binding differs from decoded aggregate authority"
                )
            if binding.domain_set_sha256 != projection.domain_set.domain_set_sha256:
                raise BatchPlanError(
                    "analysis-domain binding differs from decoded domain-set authority"
                )
            if (
                binding.sample_id != sample_id
                or binding.slide_id != sample.slide_id
                or binding.project_entry_id != selection.project_entry_ids[0]
            ):
                raise BatchPlanError("analysis-domain binding conflicts with project/sample scope")
            key = _binding_key(binding)
            if key in refs:
                raise BatchPlanError("analysis-domain binding is duplicated")
            refs[key], selections[key] = artifact, selection
            section_targets[key] = PhysicalSectionTarget(
                project_entry_id=owner.project_entry_id,
                tissue_generation_id=owner.tissue_generation_id,
                section_id=owner.section_id,
                tissue_path_object_id=owner.tissue_path_object_id,
            )
            groups.setdefault(sample.validation_domain_id, []).append(binding)
    return refs, selections, groups, section_targets


@dataclass(frozen=True, slots=True)
class _UnsupervisedWork:
    bindings: tuple[AnalysisDomainBinding, ...]
    segmentation: _Context
    measurements: _Context
    mode_inputs: tuple[tuple[_Context, BatchArtifactRef | None], ...]
    accepted_domain_inputs: tuple[tuple[_Context, str, BatchArtifactRef], ...]
    section_target: PhysicalSectionTarget
    preparation: _Context


def _validate_clustering_graph_compatibility(
    builder: _Builder,
    work: tuple[_UnsupervisedWork, ...],
) -> None:
    """Validate clustering graph compatibility for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from .batch_measurement_artifacts import (
        validate_measurements_result_artifact_ref,
    )

    # The graph request compares the accepted preparation settings. Original
    # manifest defaults may have been replaced through a reviewed revision.
    panel_and_channels: set[str] = set()
    for item in work:
        if not item.bindings:
            raise BatchPlanError("pooled clustering work lacks a measurement domain")
        sample_id = item.bindings[0].sample_id
        sample = builder.samples[sample_id]
        measurement_context = item.measurements
        measurement_ref = builder.effective_accepted_output(
            measurement_context, "measurements_result", None
        )
        if measurement_ref is None:
            raise BatchPlanError("pooled clustering requires accepted measurement authority")
        try:
            measurement = validate_measurements_result_artifact_ref(measurement_ref)
        except BatchArtifactError as exc:
            raise BatchPlanError("pooled clustering measurement authority is invalid") from exc
        if (
            measurement.artifact_ref != measurement_ref
            or measurement.sample_id != sample_id
            or measurement.slide_id != sample.slide_id
            or measurement.project_entry_id != item.bindings[0].project_entry_id
            or measurement.analysis_domain_bindings
            != measurement_context.node.analysis_domain_bindings
            or any(binding not in measurement.analysis_domain_bindings for binding in item.bindings)
            or any(
                binding.sample_id != sample_id
                or binding.slide_id != sample.slide_id
                or binding.project_entry_id != measurement.project_entry_id
                for binding in item.bindings
            )
            or measurement_context.node.participant_ids != (sample_id,)
            or measurement_context.project_entry_ids != (measurement.project_entry_id,)
            or measurement_context.validation_domain_id != sample.validation_domain_id
            or measurement_ref.scope.validation_domain_id != sample.validation_domain_id
            or measurement_ref.scope.sample_id != sample_id
            or measurement_ref.scope.slide_id != sample.slide_id
            or measurement_ref.scope.project_entry_id != measurement.project_entry_id
            or measurement_ref.scope.analysis_domain_bindings
            != measurement.analysis_domain_bindings
            or measurement_ref.configuration_bindings
            != (ArtifactConfigurationBinding(sample_id, sample.resolved_config_sha256),)
            or measurement.canonical_image_id
            != measurement_ref.owning_identity["canonical_image_id"]
            or measurement.canonical_image_sha256
            != measurement_ref.owning_identity["canonical_image_sha256"]
        ):
            raise BatchPlanError("pooled clustering measurement scope or image authority differs")
        panel_and_channels.add(
            _digest(
                {
                    "canonical_channels": [
                        dict(channel) for channel in measurement.canonical_channels
                    ],
                }
            )
        )
    if len(panel_and_channels) != 1:
        raise BatchPlanError(
            "pooled clustering participants have incompatible panel/channel semantics"
        )


def _effective_clustering_scopes(
    builder: _Builder,
    work: Sequence[_UnsupervisedWork],
) -> dict[str, ClusteringMode | None]:
    """Resolve late scope only from accepted preparations of every known section."""
    scopes = {sample_id: sample.config.clustering.mode
              for sample_id, sample in builder.samples.items()}
    for item in work:
        if item.preparation.node.settled_review_revision is not None:
            # An explicit return may change the scope. Wait for its reviewed
            # replacement before assembling a shared graph's participants.
            scopes[item.bindings[0].sample_id] = None
    selected: dict[str, set[ClusteringMode]] = {}
    incomplete: set[str] = set()
    for item in work:
        sample_id = item.bindings[0].sample_id
        artifact = builder.effective_accepted_output(
            item.preparation, "unsupervised_preparation_result", None
        )
        if artifact is None:
            incomplete.add(sample_id)
            continue
        _validate_output_authority(artifact, item.preparation, builder)
        try:
            mode = ClusteringMode(artifact.owning_identity.get("clustering_mode"))
        except (TypeError, ValueError) as exc:
            raise BatchPlanError("accepted preparation has invalid clustering scope") from exc
        configured = scopes[sample_id]
        if configured is not None and configured is not mode:
            raise BatchPlanError("accepted clustering scope differs from configured scope")
        choices = selected.setdefault(sample_id, set())
        choices.add(mode)
        if len(choices) != 1:
            raise BatchPlanError("sample sections disagree on clustering scope")
    for sample_id, choices in selected.items():
        if sample_id not in incomplete:
            scopes[sample_id] = next(iter(choices))
    return scopes


def _materialize_unsupervised_graphs(
    builder: _Builder,
    contexts: Mapping[str, Mapping[str, _Context]],
    result: dict[tuple[object, ...], dict[str, _Context]],
    raw_work: Sequence[_UnsupervisedWork],
    accepted_bindings: Sequence[AnalysisDomainBinding],
    phenotyping_modes: Mapping[tuple[object, ...], str | None],
) -> None:
    work = tuple(sorted(raw_work, key=lambda item: _binding_key(item.bindings[0])))
    work_by_binding = {_binding_key(binding): item for item in work for binding in item.bindings}
    accepted_by_sample: dict[str, list[AnalysisDomainBinding]] = {}
    excluded_samples: set[str] = set()
    for binding in accepted_bindings:
        if "excluded_section" in result.get(_binding_key(binding), {}):
            excluded_samples.add(binding.sample_id)
            continue
        accepted_by_sample.setdefault(binding.sample_id, []).append(binding)
        key = _binding_key(binding)
        if (
            phenotyping_modes.get(key) == PhenotypingMode.UNSUPERVISED_CLUSTERING.value
            and key not in work_by_binding
        ):
            raise BatchPlanError(
                "unsupervised clustering selection lacks its preparation authority"
            )

    potential_samples: set[str] = set()
    known_unsupervised_samples: set[str] = set()
    complete_samples: set[str] = set()
    unresolved_unknown_samples: set[str] = set()
    for sample_id, sample in sorted(builder.samples.items()):
        configured_mode = sample.config.phenotyping.mode
        if configured_mode not in {
            None,
            PhenotypingMode.UNSUPERVISED_CLUSTERING,
        }:
            continue
        sample_bindings = tuple(accepted_by_sample.get(sample_id, ()))
        if not sample_bindings and sample_id in excluded_samples:
            continue
        resolved = tuple(phenotyping_modes.get(_binding_key(item)) for item in sample_bindings)
        unresolved = not sample_bindings or any(item is None for item in resolved)
        selected_unsupervised = tuple(
            item
            for item, mode in zip(sample_bindings, resolved, strict=True)
            if mode == PhenotypingMode.UNSUPERVISED_CLUSTERING.value
        )
        if configured_mode is PhenotypingMode.UNSUPERVISED_CLUSTERING:
            potential_samples.add(sample_id)
            known_unsupervised_samples.add(sample_id)
            expected = sample_bindings
        elif selected_unsupervised:
            potential_samples.add(sample_id)
            known_unsupervised_samples.add(sample_id)
            expected = selected_unsupervised
        elif unresolved:
            potential_samples.add(sample_id)
            unresolved_unknown_samples.add(sample_id)
            expected = ()
        else:
            continue
        if (
            not unresolved
            and expected
            and all(_binding_key(item) in work_by_binding for item in expected)
        ):
            complete_samples.add(sample_id)

    scope_by_sample = _effective_clustering_scopes(builder, work)
    def defer_graph(kind: str, participants: Sequence[str]) -> None:
        participant_ids = tuple(sorted(set(participants)))
        if not participant_ids:
            return
        builder.defer(
            kind,
            participant_ids,
            "clustering_graph_participants",
            "unsupervised_preparation",
        )
        graph_scope = (
            NodeScope.SAMPLE if kind == "clustering_per_slide" else NodeScope.VALIDATION_DOMAIN
        )
        builder.defer(
            "cluster_review_evidence",
            participant_ids,
            "clustering_graph_result",
            kind,
            scope_override=graph_scope,
        )
        builder.defer(
            "cluster_label_review",
            participant_ids,
            "cluster_review_evidence",
            "cluster_review_evidence",
            scope_override=graph_scope,
        )
        for sample_id in participant_ids:
            builder.defer(
                "clustering_label_import",
                sample_id,
                "accepted_cluster_label_review",
                "cluster_label_review",
            )
            builder.defer(
                "semantic_raw_inventory_snapshot",
                sample_id,
                "cluster_label_import_result",
                "clustering_label_import",
            )

    # Use the same existing Processing group identity as marker fitting.
    # An unfinished group must not block unrelated groups.
    potential_by_domain = _processing_group_members(
        builder.samples[sample_id] for sample_id in potential_samples
    )
    graph_groups = []
    for domain, candidates in sorted(potential_by_domain.items()):
        undecided = {sample_id for sample_id in candidates
                     if scope_by_sample[sample_id] is None}
        pooled = {sample_id for sample_id in candidates
                  if scope_by_sample[sample_id] is ClusteringMode.POOLED}
        if undecided:
            for sample_id in sorted(undecided):
                builder.defer("clustering_label_import", sample_id,
                              "clustering_scope", "unsupervised_preparation")
        if pooled:
            if undecided or not pooled <= complete_samples:
                defer_graph("clustering_pooled", tuple(pooled))
            else:
                selected = tuple(item for item in work
                                 if item.bindings[0].sample_id in pooled)
                graph_groups.append(("clustering_pooled", NodeScope.VALIDATION_DOMAIN,
                                     domain, selected))
        per_slide: dict[tuple[str, str, str], list[_UnsupervisedWork]] = {}
        for item in work:
            first = item.bindings[0]
            if (first.sample_id in candidates and
                    scope_by_sample[first.sample_id] is ClusteringMode.PER_SLIDE):
                per_slide.setdefault((first.sample_id, first.project_entry_id,
                                      first.slide_id), []).append(item)
        for (sample_id, _entry_id, _slide_id), selected in sorted(per_slide.items()):
            if sample_id not in complete_samples:
                defer_graph("clustering_per_slide", (sample_id,))
            else:
                graph_groups.append(("clustering_per_slide", NodeScope.SAMPLE,
                                     domain, tuple(selected)))

    clustered: dict[tuple[object, ...], _Context] = {}
    reviewed: dict[tuple[object, ...], tuple[_Context, _Context | None]] = {}
    for kind, scope, validation_domain, selected in graph_groups:
        _validate_clustering_graph_compatibility(builder, selected)
        bindings = tuple(
            sorted(
                (binding for item in selected for binding in item.bindings),
                key=_binding_key,
            )
        )
        graph_scope = ClusteringGraphScope(
            mode={
                "clustering_per_slide": ClusteringMode.PER_SLIDE,
                "clustering_pooled": ClusteringMode.POOLED,
            }[kind],
            phenotyping_group_id=None,
            validation_domain_ids=tuple(
                sorted(
                    {
                        builder.samples[binding.sample_id].validation_domain_id
                        for binding in bindings
                    }
                )
            ),
        )
        graph = builder.add(
            kind,
            scope=scope,
            participants=tuple(sorted({binding.sample_id for binding in bindings})),
            validation_domain=validation_domain,
            bindings=bindings,
            entries=tuple(sorted({binding.project_entry_id for binding in bindings})),
            clustering_scope=graph_scope,
            inputs=tuple(
                (mode, "phenotyping_mode", decision)
                for item in selected
                for mode, decision in item.mode_inputs
            )
            + tuple(
                (measurement, "measurements_result", None)
                for measurement in (item.measurements for item in selected)
            )
            + tuple(
                (
                    item.preparation,
                    "unsupervised_preparation_result",
                    None,
                )
                for item in selected
            ),
        )
        clustered.update(
            {_binding_key(binding): graph for item in selected for binding in item.bindings}
        )
        accepted_graph = builder.accepted_by_producer_kind.get(
            (graph.node.node_id, "clustered_phenotype_result")
        )
        if accepted_graph is None:
            builder.defer(
                "cluster_review_evidence",
                graph.node.participant_ids,
                "clustering_graph_result",
                kind,
                scope_override=scope,
            )
            builder.defer(
                "cluster_label_review",
                graph.node.participant_ids,
                "cluster_review_evidence",
                "cluster_review_evidence",
                scope_override=scope,
            )
            continue
        evidence = builder.add(
            "cluster_review_evidence",
            scope=scope,
            participants=graph.node.participant_ids,
            validation_domain=validation_domain,
            bindings=bindings,
            entries=graph.node.project_entry_ids,
            clustering_scope=graph_scope,
            inputs=((graph, "clustered_phenotype_result", accepted_graph),)
            + tuple((item.measurements, "measurements_result", None) for item in selected)
            + tuple(
                (
                    item.preparation,
                    "unsupervised_preparation_result",
                    None,
                )
                for item in selected
            ),
        )
        accepted_evidence = builder.accepted_by_producer_kind.get(
            (evidence.node.node_id, "cluster_review_evidence")
        )
        review = (
            None
            if accepted_evidence is None
            else builder.add(
                "cluster_label_review",
                scope=scope,
                participants=graph.node.participant_ids,
                validation_domain=validation_domain,
                bindings=bindings,
                entries=graph.node.project_entry_ids,
                clustering_scope=graph_scope,
                inputs=(
                    (
                        evidence,
                        "cluster_review_evidence",
                        accepted_evidence,
                    ),
                ),
            )
        )
        reviewed.update(
            {
                _binding_key(binding): (evidence, review)
                for item in selected
                for binding in item.bindings
            }
        )

    for item in work:
        first = item.bindings[0]
        sample_id = first.sample_id
        graph = clustered.get(_binding_key(first))
        review_pair = reviewed.get(_binding_key(first))
        if graph is None:
            continue
        for binding in item.bindings:
            result[_binding_key(binding)][graph.node.kind] = graph
        if review_pair is None:
            continue
        evidence, review = review_pair
        for binding in item.bindings:
            result[_binding_key(binding)][evidence.node.kind] = evidence
        if review is None:
            continue
        for binding in item.bindings:
            result[_binding_key(binding)][review.node.kind] = review
        accepted_review = builder.accepted_by_producer_kind.get(
            (review.node.node_id, "accepted_cluster_label_review")
        )
        accepted_graph = builder.accepted_by_producer_kind.get(
            (graph.node.node_id, "clustered_phenotype_result")
        )
        if accepted_review is None or accepted_graph is None:
            continue
        label_import = builder.add(
            "clustering_label_import",
            scope=NodeScope.SAMPLE,
            participants=(sample_id,),
            validation_domain=builder.samples[sample_id].validation_domain_id,
            bindings=item.bindings,
            entries=(first.project_entry_id,),
            section_target=item.section_target,
            inputs=(
                (item.segmentation, "segmentation_result", None),
                (item.measurements, "measurements_result", None),
                (graph, "clustered_phenotype_result", accepted_graph),
                (
                    review,
                    "accepted_cluster_label_review",
                    accepted_review,
                ),
            )
            + item.accepted_domain_inputs
            + tuple((mode, "phenotyping_mode", decision) for mode, decision in item.mode_inputs),
        )
        for binding in item.bindings:
            result[_binding_key(binding)]["clustering_label_import"] = label_import


def _domain_graph(
    builder: _Builder,
    contexts: dict[str, dict[str, _Context]],
    refs: dict[tuple[object, ...], BatchArtifactRef],
    selections: dict[tuple[object, ...], _Context],
    groups: dict[str, list[AnalysisDomainBinding]],
    section_targets: dict[tuple[object, ...], PhysicalSectionTarget],
    owner_projections: Mapping[str, object],
    artifacts: tuple[BatchArtifactRef, ...],
    external_artifacts: tuple[BatchArtifactRef, ...],
) -> dict[tuple[object, ...], dict[str, _Context]]:
    result: dict[tuple[object, ...], dict[str, _Context]] = {}
    # These scopes are consumed after all validation-domain groups are built.
    # Resetting them inside each group loses earlier images during phenotype/
    # semantic materialization and can join the wrong final group.
    primaries: dict[tuple[object, ...], _Context] = {}
    skipped_primaries: dict[tuple[object, ...], tuple[_Context, BatchArtifactRef]] = {}
    def classification_input(binding):
        key = _binding_key(binding)
        if key in skipped_primaries:
            owner, accepted = skipped_primaries[key]
            return owner, "training_plan_set", accepted
        owner = primaries[key]
        return owner, "primary_inference_result", builder.accepted_by_producer_kind.get(
            (owner.node.node_id, "primary_inference_result")
        )

    secondary_nodes_by_binding: dict[tuple[object, ...], tuple[_Context, ...]] = {}
    external_classifiers = _external_classifiers(external_artifacts, builder)
    analysis_frames = _one_per_sample(artifacts, "analysis_frame")
    project_entries = _primary_project_inputs(artifacts, contexts)
    measurements_by_domain: dict[str, list[tuple[AnalysisDomainBinding, _Context, _Context]]] = {}
    excluded_cell_domains: set[tuple[object, ...]] = set()
    unsupervised_work: list[_UnsupervisedWork] = []
    unsupervised_candidates: list[
        tuple[
            AnalysisDomainBinding,
            _Context,
            _Context,
            BatchArtifactRef | None,
        ]
    ] = []
    phenotyping_modes: dict[tuple[object, ...], str | None] = {}

    def accepted_domain_inputs(
        selected_bindings: tuple[AnalysisDomainBinding, ...],
    ) -> tuple[tuple[_Context, str, BatchArtifactRef], ...]:
        result: list[tuple[_Context, str, BatchArtifactRef]] = []
        seen_refs: set[str] = set()
        for binding in selected_bindings:
            key = _binding_key(binding)
            artifact = refs[key]
            if artifact.artifact_ref_sha256 in seen_refs:
                continue
            seen_refs.add(artifact.artifact_ref_sha256)
            result.append((selections[key], "accepted_analysis_domain", artifact))
        return tuple(result)

    def accepted_training_plan(
        producer: _Context,
        *,
        classifier_kind: str,
        participant_ids: tuple[str, ...],
        selected_bindings: tuple[AnalysisDomainBinding, ...],
        project_entry_ids: tuple[str, ...],
        consumer_kind: str,
    ) -> tuple[BatchArtifactRef, object] | None:
        accepted = builder.accepted_by_producer_kind.get(
            (producer.node.node_id, "training_plan_set")
        )
        if accepted is None:
            return None
        _validate_output(accepted, producer, builder, consumer_kind)
        projection = owner_projections.get(accepted.artifact_ref_sha256)
        from .batch_training import (
            ClassifierKind,
            ValidatedTrainingPlanSetProjection,
        )

        if type(projection) is not ValidatedTrainingPlanSetProjection:
            raise BatchPlanError("training plan set lacks decoded committed owner authority")
        if (
            projection.artifact_ref != accepted
            or projection.batch_id != builder.manifest.manifest.batch_id
            or projection.validation_domain_id != producer.validation_domain_id
            or projection.classifier_kind is not ClassifierKind(classifier_kind)
            or projection.participant_ids != participant_ids
            or projection.project_entry_ids != project_entry_ids
            or projection.analysis_domain_bindings != selected_bindings
            or projection.configuration_bindings != builder.configs(participant_ids)
        ):
            raise BatchPlanError("training plan set differs from its exact planned family scope")
        return accepted, projection

    def accepted_training_outputs(
        producers: tuple[_Context, ...],
        artifact_kind: str,
        consumer_kind: str,
    ) -> tuple[BatchArtifactRef, ...] | None:
        selected: list[BatchArtifactRef] = []
        for producer in producers:
            accepted = builder.accepted_by_producer_kind.get((producer.node.node_id, artifact_kind))
            if accepted is None:
                return None
            _validate_output(accepted, producer, builder, consumer_kind)
            selected.append(accepted)
        return tuple(selected)

    def retain_training_contexts(
        selected_bindings: tuple[AnalysisDomainBinding, ...],
        *contexts_to_retain: _Context,
    ) -> None:
        for binding in selected_bindings:
            retained = result.setdefault(_binding_key(binding), {})
            for context in contexts_to_retain:
                retained[context.node.kind] = context

    def unique_scientific_inputs(
        values: tuple[tuple[_Context, str, BatchArtifactRef | None], ...],
    ) -> tuple[tuple[_Context, str, BatchArtifactRef | None], ...]:
        unique: dict[
            tuple[str, str, str | None],
            tuple[_Context, str, BatchArtifactRef | None],
        ] = {}
        for producer, artifact_kind, accepted in values:
            key = (
                producer.node.node_id,
                artifact_kind,
                None if accepted is None else accepted.artifact_ref_sha256,
            )
            unique.setdefault(key, (producer, artifact_kind, accepted))
        return tuple(unique[key] for key in sorted(unique))

    for validation_domain, raw in sorted(groups.items()):
        bindings = tuple(sorted(raw, key=_binding_key))
        participants = tuple(sorted({item.sample_id for item in bindings}))
        entries = tuple(sorted({item.project_entry_id for item in bindings}))
        def build_primary_scope(bindings):
            """Keep a saved model's application targets stable as other images become ready."""
            primary_training_bindings = bindings
            primary_training_plan = None
            primary_evidence_children: tuple[_Context, ...] = ()
            primary_training = None
            selected_primary_record = None
            if primary_training_bindings:
                primary_training_participants = tuple(
                    sorted({item.sample_id for item in primary_training_bindings})
                )
                primary_training_entries = tuple(
                    sorted({item.project_entry_id for item in primary_training_bindings})
                )
                primary_training_plan = builder.add(
                    "primary_classifier_training_plan_review",
                    scope=NodeScope.VALIDATION_DOMAIN,
                    participants=primary_training_participants,
                    validation_domain=validation_domain,
                    bindings=primary_training_bindings,
                    entries=primary_training_entries,
                    inputs=accepted_domain_inputs(primary_training_bindings)
                    + tuple(
                        (
                            contexts[sample_id]["analysis_preflight"],
                            "analysis_frame",
                            analysis_frames[sample_id],
                        )
                        for sample_id in primary_training_participants
                    )
                    + tuple(
                        (
                            contexts[sample_id]["project_upsert"],
                            "project_catalogue_entry",
                            project_entries[sample_id],
                        )
                        for sample_id in primary_training_participants
                    ),
                )
                primary_plan_material = accepted_training_plan(
                    primary_training_plan,
                    classifier_kind="primary_compartment",
                    participant_ids=primary_training_participants,
                    selected_bindings=primary_training_bindings,
                    project_entry_ids=primary_training_entries,
                    consumer_kind="primary_training_evidence_snapshot",
                )
                if primary_plan_material is None:
                    retain_training_contexts(bindings, primary_training_plan)
                    for kind in (
                        "primary_training_evidence_snapshot",
                        "primary_classifier_training_review",
                        "primary_classifier_inference",
                    ):
                        builder.defer(
                            kind,
                            primary_training_participants,
                            "training_plan_set",
                            "primary_classifier_training_plan_review",
                        )
                    return
                accepted_primary_plan, _primary_plan_projection = primary_plan_material
                if _primary_plan_projection.plan_set.skipped_primary is not None:
                    for binding in bindings:
                        skipped_primaries[_binding_key(binding)] = (primary_training_plan, accepted_primary_plan)
                    retain_training_contexts(bindings, primary_training_plan)
                    return
                saved_primary_selection = _primary_plan_projection.plan_set.saved_selection
                if saved_primary_selection is not None:
                    expected = saved_primary_selection.declaration.declaration_sha256
                    if any(("primary", sid) not in external_classifiers or
                            external_classifiers[("primary", sid)].external_declaration_sha256 != expected
                            for sid in primary_training_participants):
                        retain_training_contexts(bindings, primary_training_plan)
                        return
                else:
                    primary_reference_entries = {
                        entry.project_entry_id
                        for plan in _primary_plan_projection.plan_set.plans
                        for entry in plan.request.entries
                    }
                    primary_by_entry: dict[str, list[AnalysisDomainBinding]] = {}
                    for item in primary_training_bindings:
                        if item.project_entry_id in primary_reference_entries:
                            primary_by_entry.setdefault(item.project_entry_id, []).append(item)
                    primary_children: list[_Context] = []
                    for entry_id, raw_entry_bindings in sorted(primary_by_entry.items()):
                        entry_bindings = tuple(sorted(raw_entry_bindings, key=_binding_key))
                        sample_ids = {item.sample_id for item in entry_bindings}
                        if len(sample_ids) != 1:
                            raise BatchPlanError("primary evidence entry crosses sample identity")
                        sample_id = next(iter(sample_ids))
                        primary_children.append(
                            builder.add(
                                "primary_training_evidence_snapshot",
                                scope=NodeScope.SAMPLE,
                                participants=(sample_id,),
                                validation_domain=validation_domain,
                                bindings=entry_bindings,
                                entries=(entry_id,),
                                inputs=(
                                    (
                                        primary_training_plan,
                                        "training_plan_set",
                                        accepted_primary_plan,
                                    ),
                                )
                                + accepted_domain_inputs(entry_bindings),
                            )
                        )
                    primary_evidence_children = tuple(primary_children)
                    accepted_primary_children = accepted_training_outputs(
                        primary_evidence_children,
                        "primary_training_evidence_snapshot",
                        "primary_classifier_training_review",
                    )
                    if accepted_primary_children is None:
                        retain_training_contexts(
                            bindings,
                            primary_training_plan,
                            *primary_evidence_children,
                        )
                        builder.defer(
                            "primary_classifier_training_review",
                            primary_training_participants,
                            "primary_training_evidence_snapshot",
                            "primary_training_evidence_snapshot",
                        )
                        builder.defer(
                            "primary_classifier_inference",
                            primary_training_participants,
                            "classifier_record_set",
                            "primary_classifier_training_review",
                        )
                        return
                    primary_training = builder.add(
                        "primary_classifier_training_review",
                        training_plan_projection=_primary_plan_projection,
                        scope=NodeScope.VALIDATION_DOMAIN,
                        participants=primary_training_participants,
                        validation_domain=validation_domain,
                        bindings=primary_training_bindings,
                        entries=primary_training_entries,
                        inputs=(
                            (
                                primary_training_plan,
                                "training_plan_set",
                                accepted_primary_plan,
                            ),
                        )
                        + tuple(
                            (
                                child,
                                "primary_training_evidence_snapshot",
                                accepted_child,
                            )
                            for child, accepted_child in zip(
                                primary_evidence_children,
                                accepted_primary_children,
                                strict=True,
                            )
                        ),
                    )
                    matching_records = tuple(
                        artifact
                        for artifact in _triggers(artifacts, "classifier_record_set")
                        if artifact.producer_node_id == primary_training.node.node_id
                    )
                    if len(matching_records) > 1:
                        raise BatchPlanError("primary classifier record set authority is duplicated")
                    if not matching_records:
                        retain_training_contexts(
                            bindings,
                            primary_training_plan,
                            *primary_evidence_children,
                            primary_training,
                        )
                        builder.defer(
                            "primary_classifier_inference",
                            primary_training_participants,
                            "classifier_record_set",
                            "primary_classifier_training_review",
                        )
                        return
                    selected_primary_record = matching_records[0]
                    _validate_output(
                        selected_primary_record,
                        primary_training,
                        builder,
                        "primary_classifier_inference",
                    )
            for binding in bindings:
                key, sample_id = _binding_key(binding), binding.sample_id
                frame = contexts[sample_id]["analysis_preflight"]
                external_primary = (external_classifiers.get(("primary", sample_id))
                                    if _primary_plan_projection.plan_set.saved_selection is not None else None)
                classifier_input: tuple[_Context | None, str, BatchArtifactRef | None] = (
                    (None, "classifier_record_set", external_primary)
                    if external_primary is not None
                    else (
                        primary_training,
                        "classifier_record_set",
                        selected_primary_record,
                    )
                )
                if classifier_input[0] is None and classifier_input[2] is None:
                    raise BatchPlanError("primary classifier lacks exact authority")
                primaries[key] = builder.add(
                    "primary_classifier_inference",
                    extra=(primary_training_plan,),
                    scope=NodeScope.SAMPLE,
                    participants=(sample_id,),
                    validation_domain=validation_domain,
                    bindings=(binding,),
                    entries=(binding.project_entry_id,),
                    inputs=(
                        (selections[key], "accepted_analysis_domain", refs[key]),
                        (frame, "analysis_frame", None),
                        (
                            contexts[sample_id]["project_upsert"],
                            "project_catalogue_entry",
                            project_entries[sample_id],
                        ),
                        classifier_input,
                    ),
                )
                result.setdefault(key, {})["primary_classifier_inference"] = primaries[key]
                if primary_training is not None:
                    result[key]["primary_classifier_training_plan_review"] = primary_training_plan
                    result[key]["primary_classifier_training_review"] = primary_training

        primary_scopes: dict[str, list[AnalysisDomainBinding]] = {}
        for binding in bindings:
            saved = external_classifiers.get(("primary", binding.sample_id))
            scope = "" if saved is None else saved.external_declaration_sha256
            primary_scopes.setdefault(scope, []).append(binding)
        for _scope, scope_bindings in sorted(primary_scopes.items()):
            if not _scope:
                expected = {sample.sample_id for sample in builder.samples.values()
                            if sample.validation_domain_id == validation_domain
                            and ("primary", sample.sample_id) not in external_classifiers}
                if not expected <= {item.sample_id for item in scope_bindings}:
                    # New shared training must not open for a temporary subset
                    # while another image is still choosing its analysis area.
                    for kind in ("primary_classifier_training_plan_review",
                                 "primary_training_evidence_snapshot",
                                 "primary_classifier_training_review",
                                 "primary_classifier_inference"):
                        builder.defer(kind, tuple(sorted(expected)),
                                      "analysis_domain_bindings", "analysis_domain_selection")
                    continue
            build_primary_scope(tuple(scope_bindings))
        bindings = tuple(item for item in bindings
                         if _binding_key(item) in primaries or _binding_key(item) in skipped_primaries)
        if not bindings:
            continue

        section_groups: dict[PhysicalSectionTarget, list[AnalysisDomainBinding]] = {}
        for binding in bindings:
            key = _binding_key(binding)
            section_target = section_targets.get(key)
            if section_target is None:
                raise BatchPlanError("analysis domain lacks exact physical section authority")
            section_groups.setdefault(section_target, []).append(binding)

        for section_target, raw_section_bindings in sorted(
            section_groups.items(), key=lambda item: item[0]
        ):
            section_bindings = tuple(sorted(raw_section_bindings, key=_binding_key))
            sample_ids = {item.sample_id for item in section_bindings}
            entry_ids = {item.project_entry_id for item in section_bindings}
            if len(sample_ids) != 1 or entry_ids != {section_target.project_entry_id}:
                raise BatchPlanError(
                    "physical section group crosses sample or project-entry authority"
                )
            sample_id = next(iter(sample_ids))
            frame = contexts[sample_id]["analysis_preflight"]
            section_common = {
                "scope": NodeScope.SAMPLE,
                "participants": (sample_id,),
                "validation_domain": validation_domain,
                "bindings": section_bindings,
                "entries": (section_target.project_entry_id,),
                "section_target": section_target,
            }
            section_domain_inputs = accepted_domain_inputs(section_bindings)
            classification_sources = tuple(classification_input(binding) for binding in section_bindings)
            accepted_primary_inputs = tuple(source[2] for source in classification_sources)
            if any(item is None for item in accepted_primary_inputs):
                builder.defer(
                    "segmentation",
                    sample_id,
                    "primary_inference_result",
                    "primary_classifier_inference",
                )
                continue
            segmentation = builder.add(
                "segmentation",
                **section_common,
                inputs=section_domain_inputs
                + ((frame, "analysis_frame", None),)
                + unique_scientific_inputs(classification_sources)
            )
            accepted_segmentation = builder.accepted_by_producer_kind.get(
                (segmentation.node.node_id, "segmentation_result")
            )
            if accepted_segmentation is None:
                for binding in section_bindings:
                    result[_binding_key(binding)]["segmentation"] = segmentation
                builder.defer(
                    "measurements",
                    sample_id,
                    "segmentation_result",
                    "segmentation",
                )
                continue
            from .batch_segmentation_artifacts import (
                ValidatedSegmentationResultProjection,
            )

            segmentation_projection = owner_projections.get(
                accepted_segmentation.artifact_ref_sha256
            )
            if type(segmentation_projection) is not ValidatedSegmentationResultProjection:
                raise BatchPlanError("segmentation result lacks decoded committed owner authority")
            _validate_output(
                accepted_segmentation,
                segmentation,
                builder,
                "measurements",
            )
            expected_domain_ref_sha256s = tuple(
                sorted(
                    {
                        domain_ref.artifact_ref_sha256
                        for _selection, _kind, domain_ref in section_domain_inputs
                    }
                )
            )
            binding_keys = {
                (
                    item.analysis_domain_id,
                    item.analysis_domain_revision,
                    item.analysis_domain_revision_sha256,
                    item.project_entry_id,
                    item.slide_id,
                )
                for item in section_bindings
            }
            domain_owner_by_key: dict[tuple[object, ...], object] = {}
            for _selection, _kind, domain_ref in section_domain_inputs:
                domain_projection = owner_projections.get(domain_ref.artifact_ref_sha256)
                if domain_projection is None:
                    raise BatchPlanError(
                        "segmentation join lacks decoded accepted-domain authority"
                    )
                for owner in domain_projection.domain_authorities:
                    owner_key = (
                        owner.analysis_domain_id,
                        owner.analysis_domain_revision,
                        owner.analysis_domain_revision_sha256,
                        owner.project_entry_id,
                        owner.slide_id,
                    )
                    if owner_key not in binding_keys:
                        continue
                    if owner_key in domain_owner_by_key:
                        raise BatchPlanError(
                            "segmentation join duplicates accepted-domain authority"
                        )
                    domain_owner_by_key[owner_key] = owner
            if set(domain_owner_by_key) != binding_keys:
                raise BatchPlanError("segmentation join does not exactly cover section domains")
            section_owners = tuple(domain_owner_by_key[key] for key in sorted(binding_keys))
            if (
                segmentation_projection.artifact_ref != accepted_segmentation
                or segmentation_projection.sample_id != sample_id
                or segmentation_projection.slide_id != section_bindings[0].slide_id
                or segmentation_projection.project_entry_id != section_target.project_entry_id
                or segmentation_projection.section_id != section_target.section_id
                or segmentation_projection.tissue_generation_id
                != section_target.tissue_generation_id
                or segmentation_projection.tissue_path_object_id
                != section_target.tissue_path_object_id
                or segmentation_projection.analysis_domain_bindings != section_bindings
                or segmentation_projection.accepted_analysis_domain_ref_sha256s
                != expected_domain_ref_sha256s
                or segmentation_projection.domain_set_sha256
                != section_bindings[0].domain_set_sha256
                or any(
                    owner.section_id != section_target.section_id
                    or owner.tissue_generation_id != segmentation_projection.tissue_generation_id
                    or owner.tissue_path_object_id != segmentation_projection.tissue_path_object_id
                    or owner.tissue_roi_id != segmentation_projection.tissue_roi_id
                    or owner.canonical_image_id != segmentation_projection.canonical_image_id
                    or owner.canonical_image_sha256
                    != segmentation_projection.canonical_image_sha256
                    for owner in section_owners
                )
            ):
                raise BatchPlanError("segmentation result differs from its exact section authority")
            primary_statuses = {}
            for binding, primary_ref in zip(section_bindings, accepted_primary_inputs, strict=True):
                if primary_ref.artifact_kind == "training_plan_set":
                    # The accepted setup already binds this explicit choice to the domain.
                    primary_statuses[binding.analysis_domain_id] = "active"
                    continue
                primary = owner_projections.get(primary_ref.artifact_ref_sha256)
                if (primary is None or primary.artifact_ref != primary_ref
                        or primary.analysis_domain_bindings != (binding,)):
                    raise BatchPlanError("section route lacks exact committed primary authority")
                primary_statuses[binding.analysis_domain_id] = primary.domain_status
            from .batch_segmentation_artifacts import section_cell_analysis_route
            route = section_cell_analysis_route(
                primary_domain_statuses=primary_statuses,
                cell_count=segmentation_projection.cell_count,
                segmentation_skipped_excluded_domains=segmentation_projection.segmentation_skipped_excluded_domains)
            if segmentation_projection.primary_inference_result_ref_sha256s != tuple(sorted(
                    {item.artifact_ref_sha256 for item in accepted_primary_inputs
                     if item.artifact_kind == "primary_inference_result"})):
                raise BatchPlanError("section route primary references differ from segmentation inputs")
            if route == "excluded":
                for binding in section_bindings:
                    excluded_cell_domains.add(_binding_key(binding))
                    result[_binding_key(binding)].update({
                        "segmentation": segmentation, "excluded_section": segmentation})
                continue
            if route == "review_zero_cells":
                raise BatchPlanError("active section with zero cells requires segmentation review")
            from .batch_cell_corrections import append_cell_corrections, OUTPUT as CORRECTION_OUTPUT
            correction, accepted_correction = append_cell_corrections(
                builder, segmentation, accepted_segmentation, section_common)
            if correction is not None and accepted_correction is None:
                for binding in section_bindings:
                    result[_binding_key(binding)]["segmentation"] = segmentation
                builder.defer("measurements", sample_id, CORRECTION_OUTPUT, CELL_CORRECTION_KIND)
                continue
            correction_inputs = (() if correction is None else
                                 ((correction, CORRECTION_OUTPUT, accepted_correction),))
            measurements = builder.add(
                "measurements",
                **section_common,
                inputs=(
                    (frame, "analysis_frame", None),
                    (
                        segmentation,
                        "segmentation_result",
                        accepted_segmentation,
                    ),
                ) + correction_inputs,
            )
            for binding in section_bindings:
                result[_binding_key(binding)].update(
                    {
                        "segmentation": segmentation,
                        "measurements": measurements,
                    }
                )
            accepted_measurements = builder.accepted_by_producer_kind.get(
                (measurements.node.node_id, "measurements_result")
            )
            if accepted_measurements is None:
                builder.defer(
                    "phenotyping_mode_resolution",
                    sample_id,
                    "measurements_result",
                    "measurements",
                )
                continue
            _validate_output(
                accepted_measurements,
                measurements,
                builder,
                "phenotyping_mode_resolution",
            )
            for binding in section_bindings:
                key = _binding_key(binding)
                domain_common = {
                    "scope": NodeScope.SAMPLE,
                    "participants": (sample_id,),
                    "validation_domain": validation_domain,
                    "bindings": (binding,),
                    "entries": (section_target.project_entry_id,),
                }
                mode = builder.add(
                    "phenotyping_mode_resolution",
                    **domain_common,
                    inputs=(
                        (
                            measurements,
                            "measurements_result",
                            accepted_measurements,
                        ),
                    ),
                )
                result[key].update(
                    {
                        "phenotyping_mode_resolution": mode,
                    }
                )
                measurements_by_domain.setdefault(validation_domain, []).append(
                    (binding, measurements, mode)
                )

    decisions_by_producer: dict[str, BatchArtifactRef] = {}
    for artifact in _triggers(artifacts, "phenotyping_mode"):
        producer_id = artifact.producer_node_id
        if producer_id is None or producer_id in decisions_by_producer:
            raise BatchPlanError("phenotyping mode decisions require one exact producer")
        decisions_by_producer[producer_id] = artifact

    def defer_mode_dependents(sample_id: str) -> None:
        for deferred_kind in (
            "marker_gate_review",
            "marker_combination_selection",
            "marker_rule_application",
            "supervised_phenotype_training_plan_review",
            "supervised_training_evidence_snapshot",
            "supervised_phenotype_training_review",
            "supervised_phenotype_inference",
            "unsupervised_preparation",
            "clustering_label_import",
            "semantic_raw_inventory_snapshot",
            "phenotype_hierarchy_attestation",
        ):
            builder.defer(
                deferred_kind,
                sample_id,
                "phenotyping_mode",
                "phenotyping_mode_resolution",
            )
        configured_scope = builder.samples[sample_id].config.clustering.mode
        if configured_scope is not None:
            builder.defer(
                {
                    ClusteringMode.PER_SLIDE: "clustering_per_slide",
                    ClusteringMode.POOLED: "clustering_pooled",
                }[configured_scope],
                sample_id,
                "phenotyping_mode",
                "phenotyping_mode_resolution",
            )

    resolved_domain_samples = {ref.scope.sample_id for ref in _triggers(artifacts, "accepted_analysis_domain")}
    for validation_domain, raw_items in sorted(measurements_by_domain.items()):
        items: list[
            tuple[
                AnalysisDomainBinding,
                _Context,
                _Context,
                BatchArtifactRef | None,
                str | None,
            ]
        ] = []
        for binding, measurements, mode in raw_items:
            sample_id = binding.sample_id
            configured_mode = builder.samples[sample_id].config.phenotyping.mode
            triggered = decisions_by_producer.pop(mode.node.node_id, None)
            decision = builder.accepted_by_producer_kind.get(
                (mode.node.node_id, "phenotyping_mode")
            )
            if decision is None:
                if triggered is not None:
                    raise BatchPlanError("phenotyping mode trigger lacks accepted owner authority")
                defer_mode_dependents(sample_id)
                continue
            if (
                triggered is not None
                and triggered.artifact_ref_sha256 != decision.artifact_ref_sha256
            ):
                raise BatchPlanError(
                    "phenotyping mode trigger differs from accepted owner authority"
                )
            raw_mode = decision.owning_identity.get("selected_mode")
            try:
                selected = PhenotypingMode(raw_mode)
            except (TypeError, ValueError) as exc:
                raise BatchPlanError(
                    "accepted mode decision is not a closed phenotyping mode"
                ) from exc
            if configured_mode is not None and selected is not configured_mode:
                raise BatchPlanError("accepted mode decision differs from resolved config")
            mode_name = selected.value
            consumer_kind = {
                PhenotypingMode.MARKER_RULES: "marker_rule_application",
                PhenotypingMode.SUPERVISED_CLASSIFIER: ("supervised_phenotype_inference"),
                PhenotypingMode.UNSUPERVISED_CLUSTERING: ("unsupervised_preparation"),
            }[selected]
            _validate_output(decision, mode, builder, consumer_kind)
            items.append((binding, measurements, mode, decision, mode_name))
            phenotyping_modes[_binding_key(binding)] = mode_name
        # Shared phenotyping setup must cover the final group. Images may reach
        # measurements at different times; opening it early would change the
        # cell population while the researcher is still reviewing that setup.
        expected_samples = _processing_group_members(builder.samples.values())[validation_domain]
        resolved_domains = {_binding_key(item[0]) for item in items} | excluded_cell_domains
        shared_membership_complete = (
            expected_samples <= resolved_domain_samples
            and all(_binding_key(binding) in resolved_domains for binding in groups[validation_domain])
        )
        retained_families = {}
        if not shared_membership_complete:
            # An unrelated section may be awaiting replacement measurements.
            # Keep a group's accepted decision only for its exact existing
            # members; new groups still wait for all selections. Below we also
            # require the reconstructed producer, including its inputs, to match.
            for mode_name, artifact_kind in (
                ("marker_rules", "marker_gate"),
                ("supervised_classifier", "training_plan_set"),
            ):
                family_bindings = tuple(sorted(
                    (item[0] for item in items if item[4] == mode_name), key=_binding_key))
                matches = [artifact for artifact in artifacts
                           if family_bindings and artifact.artifact_kind == artifact_kind
                           and artifact.scope.validation_domain_id == validation_domain
                           and artifact.scope.analysis_domain_bindings == family_bindings
                           and (mode_name != "supervised_classifier"
                                or artifact.owning_identity.get("classifier_kind") == "supervised_phenotype")]
                if len(matches) > 1:
                    raise BatchPlanError("phenotyping group has more than one accepted decision")
                if matches:
                    retained_families[mode_name] = matches[0]
            shared_items = [item for item in items
                            if item[4] in {"marker_rules", "supervised_classifier"}
                            and item[4] not in retained_families]
            for binding, *_rest in shared_items:
                defer_mode_dependents(binding.sample_id)
            items = [item for item in items
                     if item[4] not in {"marker_rules", "supervised_classifier"}
                     or item[4] in retained_families]
        marker_items = [item for item in items if item[4] == "marker_rules"]
        marker_gate = None
        marker_combinations = None
        if marker_items:
            bindings = tuple(
                sorted(
                    (item[0] for item in marker_items),
                    key=_binding_key,
                )
            )
            marker_gate = builder.add(
                "marker_gate_review",
                scope=NodeScope.VALIDATION_DOMAIN,
                participants=tuple(sorted({item.sample_id for item in bindings})),
                validation_domain=validation_domain,
                bindings=bindings,
                entries=tuple(sorted({item.project_entry_id for item in bindings})),
                inputs=tuple(
                    (measurement, "measurements_result", None)
                    for _, measurement, _, _, _ in marker_items
                )
                + tuple(
                    (mode, "phenotyping_mode", decision)
                    for _, _, mode, decision, _ in marker_items
                ),
            )
            marker_combinations = builder.add(
                "marker_combination_selection", scope=NodeScope.VALIDATION_DOMAIN,
                participants=tuple(sorted({item.sample_id for item in bindings})),
                validation_domain=validation_domain, bindings=bindings,
                entries=tuple(sorted({item.project_entry_id for item in bindings})),
                inputs=((marker_gate, "marker_gate", None),),
            )
            retained_gate = retained_families.get("marker_rules")
            if retained_gate is not None and retained_gate.producer_node_id != marker_gate.node.node_id:
                raise BatchPlanError("retained marker group inputs changed while another section is incomplete")
        supervised_items = [item for item in items if item[4] == "supervised_classifier"]
        supervised_training_plan = None
        supervised_evidence_children: tuple[_Context, ...] = ()
        supervised_training = None
        selected_supervised_record = None
        supervised_training_items = tuple(
            item
            for item in supervised_items
            if ("supervised", item[0].sample_id) not in external_classifiers
        )
        if supervised_training_items:
            bindings = tuple(
                sorted(
                    (item[0] for item in supervised_training_items),
                    key=_binding_key,
                )
            )
            supervised_participants = tuple(sorted({item.sample_id for item in bindings}))
            supervised_entries = tuple(sorted({item.project_entry_id for item in bindings}))
            supervised_training_plan = builder.add(
                "supervised_phenotype_training_plan_review",
                scope=NodeScope.VALIDATION_DOMAIN,
                participants=supervised_participants,
                validation_domain=validation_domain,
                bindings=bindings,
                entries=supervised_entries,
                inputs=accepted_domain_inputs(bindings)
                + tuple(
                    (
                        contexts[sample_id]["analysis_preflight"],
                        "analysis_frame",
                        analysis_frames[sample_id],
                    )
                    for sample_id in supervised_participants
                )
                + tuple(
                    (
                        contexts[sample_id]["project_upsert"],
                        "project_catalogue_entry",
                        project_entries[sample_id],
                    )
                    for sample_id in supervised_participants
                )
                + unique_scientific_inputs(
                    tuple(
                        (measurement, "measurements_result", None)
                        for _, measurement, _, _, _ in supervised_training_items
                    )
                )
                + unique_scientific_inputs(
                    tuple(
                        (mode, "phenotyping_mode", decision)
                        for _, _, mode, decision, _ in supervised_training_items
                    )
                ),
            )
            supervised_plan_material = accepted_training_plan(
                supervised_training_plan,
                classifier_kind="supervised_phenotype",
                participant_ids=supervised_participants,
                selected_bindings=bindings,
                project_entry_ids=supervised_entries,
                consumer_kind="supervised_training_evidence_snapshot",
            )
            retained_plan = retained_families.get("supervised_classifier")
            if retained_plan is not None and retained_plan.producer_node_id != supervised_training_plan.node.node_id:
                raise BatchPlanError("retained supervised group inputs changed while another section is incomplete")
            if supervised_plan_material is not None:
                accepted_supervised_plan, _supervised_plan_projection = supervised_plan_material
                supervised_reference_entries = {
                    entry.project_entry_id
                    for plan in _supervised_plan_projection.plan_set.plans
                    for entry in plan.request.entries
                }
            else:
                accepted_supervised_plan = None
                supervised_reference_entries = set()
            supervised_by_entry: dict[
                str,
                list[
                    tuple[
                        AnalysisDomainBinding,
                        _Context,
                        _Context,
                        BatchArtifactRef | None,
                        str | None,
                    ]
                ],
            ] = {}
            for item in supervised_training_items:
                if item[0].project_entry_id in supervised_reference_entries:
                    supervised_by_entry.setdefault(item[0].project_entry_id, []).append(item)
            supervised_children: list[_Context] = []
            for entry_id, entry_items in sorted(supervised_by_entry.items()):
                if accepted_supervised_plan is None:
                    break
                entry_bindings = tuple(sorted((item[0] for item in entry_items), key=_binding_key))
                sample_ids = {item.sample_id for item in entry_bindings}
                if len(sample_ids) != 1:
                    raise BatchPlanError("supervised evidence entry crosses sample identity")
                sample_id = next(iter(sample_ids))
                supervised_children.append(
                    builder.add(
                        "supervised_training_evidence_snapshot",
                        scope=NodeScope.SAMPLE,
                        participants=(sample_id,),
                        validation_domain=validation_domain,
                        bindings=entry_bindings,
                        entries=(entry_id,),
                        inputs=(
                            (
                                supervised_training_plan,
                                "training_plan_set",
                                accepted_supervised_plan,
                            ),
                        )
                        + accepted_domain_inputs(entry_bindings)
                        + unique_scientific_inputs(
                            tuple((item[1], "measurements_result", None) for item in entry_items)
                        )
                        + unique_scientific_inputs(
                            tuple((item[2], "phenotyping_mode", item[3]) for item in entry_items)
                        ),
                    )
                )
            supervised_evidence_children = tuple(supervised_children)
            accepted_supervised_children = (
                None
                if accepted_supervised_plan is None
                else accepted_training_outputs(
                    supervised_evidence_children,
                    "supervised_training_evidence_snapshot",
                    "supervised_phenotype_training_review",
                )
            )
            if accepted_supervised_children is not None:
                supervised_training = builder.add(
                    "supervised_phenotype_training_review",
                    training_plan_projection=_supervised_plan_projection,
                    scope=NodeScope.VALIDATION_DOMAIN,
                    participants=supervised_participants,
                    validation_domain=validation_domain,
                    bindings=bindings,
                    entries=supervised_entries,
                    inputs=(
                        (
                            supervised_training_plan,
                            "training_plan_set",
                            accepted_supervised_plan,
                        ),
                    )
                    + tuple(
                        (
                            child,
                            "supervised_training_evidence_snapshot",
                            accepted_child,
                        )
                        for child, accepted_child in zip(
                            supervised_evidence_children,
                            accepted_supervised_children,
                            strict=True,
                        )
                    ),
                )
                matching_records = tuple(
                    artifact
                    for artifact in _triggers(artifacts, "classifier_record_set")
                    if artifact.producer_node_id == supervised_training.node.node_id
                )
                if len(matching_records) > 1:
                    raise BatchPlanError("supervised classifier record set authority is duplicated")
                if matching_records:
                    selected_supervised_record = matching_records[0]
                    _validate_output(
                        selected_supervised_record,
                        supervised_training,
                        builder,
                        "supervised_phenotype_inference",
                    )
            retain_training_contexts(
                bindings,
                supervised_training_plan,
                *supervised_evidence_children,
                *((supervised_training,) if supervised_training is not None else ()),
            )
        marker_application_by_key: dict[tuple[object, ...], _Context] = {}
        marker_sections: dict[
            tuple[str, str, str, str, str],
            list[
                tuple[
                    AnalysisDomainBinding,
                    _Context,
                    _Context,
                    BatchArtifactRef | None,
                    str | None,
                ]
            ],
        ] = {}
        for item in marker_items:
            binding, measurements, _mode, _decision, _mode_name = item
            marker_sections.setdefault(
                _marker_application_section_key(binding, measurements.node.section_target),
                [],
            ).append(item)
        for section_key, section_items_list in sorted(marker_sections.items()):
            section_items = tuple(section_items_list)
            sample_id = section_key[0]
            bindings = tuple(sorted((item[0] for item in section_items), key=_binding_key))
            measurement_contexts = {item[1].node.node_id: item[1] for item in section_items}
            if len(measurement_contexts) != 1:
                raise BatchPlanError("marker application section lacks one measurement authority")
            if marker_gate is None:
                raise BatchPlanError("marker mode lacks its domain gate")
            phenotype = builder.add(
                "marker_rule_application",
                scope=NodeScope.SAMPLE,
                participants=(sample_id,),
                validation_domain=validation_domain,
                bindings=bindings,
                entries=(section_key[1],),
                section_target=next(iter(measurement_contexts.values())).node.section_target,
                inputs=((marker_gate, "marker_gate", None), (marker_combinations, "marker_combination", None))
                + tuple(
                    (context, "measurements_result", None)
                    for context in measurement_contexts.values()
                )
                + tuple(
                    (mode, "phenotyping_mode", decision)
                    for _binding, _measurements, mode, decision, _name in section_items
                ),
            )
            marker_application_by_key.update(
                {_binding_key(binding): phenotype for binding in bindings}
            )

        supervised_application_by_key: dict[tuple[object, ...], _Context] = {}
        if supervised_training is not None and selected_supervised_record is not None:
            for section_key, section_items, measurements in _supervised_application_sections(
                supervised_training_items
            ):
                sample_id = section_key[0]
                bindings = tuple(item[0] for item in section_items)
                frame = contexts[sample_id]["analysis_preflight"]
                project = contexts[sample_id]["project_upsert"]
                phenotype = builder.add(
                    "supervised_phenotype_inference",
                    scope=NodeScope.SAMPLE,
                    participants=(sample_id,),
                    validation_domain=validation_domain,
                    bindings=bindings,
                    entries=(section_key[1],),
                    section_target=measurements.node.section_target,
                    inputs=(
                        (frame, "analysis_frame", None),
                        (project, "project_catalogue_entry", None),
                        (measurements, "measurements_result", None),
                        (
                            supervised_training,
                            "classifier_record_set",
                            selected_supervised_record,
                        ),
                    )
                    + accepted_domain_inputs(bindings)
                    + tuple(
                        (mode, "phenotyping_mode", decision)
                        for _binding, _measurements, mode, decision, _name in section_items
                    ),
                )
                supervised_application_by_key.update(
                    {_binding_key(binding): phenotype for binding in bindings}
                )

        for binding, measurements, mode, decision, mode_name in items:
            key, sample_id = _binding_key(binding), binding.sample_id
            common = {
                "scope": NodeScope.SAMPLE,
                "participants": (sample_id,),
                "validation_domain": validation_domain,
                "bindings": (binding,),
                "entries": (binding.project_entry_id,),
            }
            if mode_name is None:
                defer_mode_dependents(sample_id)
                continue
            if mode_name == "marker_rules":
                phenotype = marker_application_by_key[key]
                phenotype_kind = "marker_phenotype_result"
            elif mode_name == "supervised_classifier":
                phenotype = supervised_application_by_key.get(key)
                if phenotype is None:
                    continue
                phenotype_kind = "supervised_phenotype_result"
            else:
                unsupervised_candidates.append((binding, measurements, mode, decision))
                continue
            result[key][phenotype.node.kind] = phenotype
    candidates_by_measurement: dict[
        str,
        list[
            tuple[
                AnalysisDomainBinding,
                _Context,
                _Context,
                BatchArtifactRef | None,
            ]
        ],
    ] = {}
    for item in unsupervised_candidates:
        candidates_by_measurement.setdefault(item[1].node.node_id, []).append(item)
    for _measurement_node_id, raw_candidates in sorted(candidates_by_measurement.items()):
        candidates = tuple(sorted(raw_candidates, key=lambda item: _binding_key(item[0])))
        bindings = tuple(item[0] for item in candidates)
        measurements = candidates[0][1]
        sample_ids = {item.sample_id for item in bindings}
        entry_ids = {item.project_entry_id for item in bindings}
        if len(sample_ids) != 1 or len(entry_ids) != 1:
            raise BatchPlanError(
                "one unsupervised section preparation crosses sample or entry scope"
            )
        sample_id = next(iter(sample_ids))
        preparation = builder.add(
            "unsupervised_preparation",
            scope=NodeScope.SAMPLE,
            participants=(sample_id,),
            validation_domain=builder.samples[sample_id].validation_domain_id,
            bindings=bindings,
            entries=(next(iter(entry_ids)),),
            inputs=((measurements, "measurements_result", None),)
            + tuple(
                (mode, "phenotyping_mode", decision)
                for _binding, _measurements, mode, decision in candidates
            ),
        )
        work = _UnsupervisedWork(
            bindings=bindings,
            segmentation=result[_binding_key(bindings[0])]["segmentation"],
            measurements=measurements,
            mode_inputs=tuple(
                (mode, decision) for _binding, _measurements, mode, decision in candidates
            ),
            accepted_domain_inputs=accepted_domain_inputs(bindings),
            section_target=section_targets[_binding_key(bindings[0])],
            preparation=preparation,
        )
        unsupervised_work.append(work)
        for binding in bindings:
            result[_binding_key(binding)][preparation.node.kind] = preparation
    _materialize_unsupervised_graphs(
        builder,
        contexts,
        result,
        unsupervised_work,
        tuple(
            sorted(
                (binding for raw_bindings in groups.values() for binding in raw_bindings),
                key=_binding_key,
            )
        ),
        phenotyping_modes,
    )
    # Cells and their selected phenotype result precede all secondary setup.
    # One late join serves marker rules, supervised labels and reviewed clusters.
    phenotype_kinds = {
        "marker_rule_application": "marker_phenotype_result",
        "supervised_phenotype_inference": "supervised_phenotype_result",
        "clustering_label_import": "cluster_label_import_result",
    }
    cell_inputs = {}
    for raw_bindings in groups.values():
        for binding in raw_bindings:
            key = _binding_key(binding)
            values = result.get(key, {})
            measurements = values.get("measurements")
            selected = [(values[kind], output) for kind, output in phenotype_kinds.items() if kind in values]
            if measurements is None or not selected:
                continue
            if len(selected) != 1:
                raise BatchPlanError("late secondary setup requires one selected phenotype result")
            phenotype, output = selected[0]
            measured = builder.accepted_by_producer_kind.get((measurements.node.node_id, "measurements_result"))
            phenotyped = builder.accepted_by_producer_kind.get((phenotype.node.node_id, output))
            if measured is None or phenotyped is None:
                continue
            _validate_output(measured, measurements, builder, "semantic_raw_inventory_snapshot")
            _validate_output(phenotyped, phenotype, builder, "semantic_raw_inventory_snapshot")
            cell_inputs[key] = ((measurements, "measurements_result", measured), (phenotype, output, phenotyped))

    for validation_domain, raw_bindings in sorted(groups.items()):
        bindings = tuple(sorted((binding for binding in raw_bindings
            if "excluded_section" not in result.get(_binding_key(binding), {})), key=_binding_key))
        if not bindings or any(_binding_key(binding) not in cell_inputs for binding in bindings):
            continue
        pending_definitions = tuple(
            item for item in bindings
            if builder.samples[item.sample_id].config.secondary_structures is None
        )
        if pending_definitions:
            if any(_binding_key(item) not in primaries and _binding_key(item) not in skipped_primaries
                   for item in pending_definitions):
                continue
            classified_bindings = tuple(item for item in pending_definitions if _binding_key(item) in primaries)
            accepted_primaries = accepted_training_outputs(
                tuple(primaries[_binding_key(item)] for item in classified_bindings),
                "primary_inference_result", "secondary_structure_definition",
            )
            if accepted_primaries is None:
                continue
            definition_participants = tuple(sorted({item.sample_id for item in pending_definitions}))
            definition = builder.add(
                "secondary_structure_definition", scope=NodeScope.VALIDATION_DOMAIN,
                participants=definition_participants, validation_domain=validation_domain,
                bindings=pending_definitions,
                entries=tuple(sorted({item.project_entry_id for item in pending_definitions})),
                inputs=accepted_domain_inputs(pending_definitions)
                + tuple((contexts[sample_id]["analysis_preflight"], "analysis_frame", analysis_frames[sample_id])
                        for sample_id in definition_participants)
                + tuple((contexts[sample_id]["project_upsert"], "project_catalogue_entry", project_entries[sample_id])
                        for sample_id in definition_participants)
                + tuple((primaries[_binding_key(binding)], "primary_inference_result", accepted)
                        for binding, accepted in zip(classified_bindings, accepted_primaries, strict=True))
                + unique_scientific_inputs(tuple(
                    item for binding in pending_definitions for item in cell_inputs[_binding_key(binding)]))
                + unique_scientific_inputs(tuple(
                    (skipped_primaries[_binding_key(binding)][0], "training_plan_set",
                     skipped_primaries[_binding_key(binding)][1])
                    for binding in pending_definitions if _binding_key(binding) in skipped_primaries)),
            )
            retain_training_contexts(pending_definitions, definition)
            accepted_definition = builder.accepted_by_producer_kind.get(
                (definition.node.node_id, "secondary_definition_selection"))
            if accepted_definition is None:
                continue
            _validate_output_authority(accepted_definition, definition, builder)
            from .batch_secondary_definitions import ValidatedSecondaryDefinitionSelection

            projection = owner_projections.get(accepted_definition.artifact_ref_sha256)
            if (type(projection) is not ValidatedSecondaryDefinitionSelection
                    or projection.artifact_ref != accepted_definition
                    or projection.context["batch_id"] != builder.manifest.manifest.batch_id
                    or projection.context["validation_domain_id"] != validation_domain
                    or tuple(projection.context["participant_ids"]) != definition_participants
                    or accepted_definition.configuration_bindings != builder.configs(definition_participants)):
                raise BatchPlanError("secondary definition selection differs from its current group")
            for sample_id in definition_participants:
                builder.secondary_definition_selections[sample_id] = projection
                builder.secondary_definition_contexts[sample_id] = definition
        secondary_bindings = tuple(
            item for item in bindings if builder.secondary_structures(item.sample_id)
        )
        # Secondary fitting retains its full validation-domain cohort; a saved
        # primary choice does not split that separate training population.
        if any(_binding_key(item) not in primaries and _binding_key(item) not in skipped_primaries
               for item in secondary_bindings):
            continue
        bindings = tuple(item for item in bindings
                         if _binding_key(item) in primaries or _binding_key(item) in skipped_primaries)
        if not bindings:
            continue
        secondary_training_bindings = tuple(
            item
            for item in secondary_bindings
            if ("secondary", item.sample_id) not in external_classifiers
        )
        secondary_training_plan = None
        secondary_evidence_children: tuple[_Context, ...] = ()
        secondary_training = None
        selected_secondary = None
        if secondary_training_bindings:
            secondary_training_participants = tuple(
                sorted({item.sample_id for item in secondary_training_bindings})
            )
            _configured_secondary_targets(builder, secondary_training_participants)
            secondary_training_plan = builder.add(
                "secondary_classifier_training_plan_review",
                extra=tuple(cell_inputs[_binding_key(binding)][1][0] for binding in secondary_training_bindings),
                scope=NodeScope.VALIDATION_DOMAIN,
                participants=secondary_training_participants,
                validation_domain=validation_domain,
                bindings=secondary_training_bindings,
                entries=tuple(
                    sorted({item.project_entry_id for item in secondary_training_bindings})
                ),
                inputs=accepted_domain_inputs(secondary_training_bindings)
                + tuple(
                    (
                        contexts[sample_id]["analysis_preflight"],
                        "analysis_frame",
                        analysis_frames[sample_id],
                    )
                    for sample_id in secondary_training_participants
                )
                + tuple(
                    (
                        contexts[sample_id]["project_upsert"],
                        "project_catalogue_entry",
                        project_entries[sample_id],
                    )
                    for sample_id in secondary_training_participants
                ),
            )
            secondary_training_entries = tuple(
                sorted({item.project_entry_id for item in secondary_training_bindings})
            )
            secondary_plan_material = accepted_training_plan(
                secondary_training_plan,
                classifier_kind="secondary_structure",
                participant_ids=secondary_training_participants,
                selected_bindings=secondary_training_bindings,
                project_entry_ids=secondary_training_entries,
                consumer_kind="secondary_training_evidence_snapshot",
            )
            if secondary_plan_material is None:
                retain_training_contexts(secondary_training_bindings, secondary_training_plan)
                continue
            accepted_secondary_plan, _secondary_plan_projection = secondary_plan_material
            classification_sources = {
                binding: classification_input(binding) for binding in secondary_training_bindings
            }
            if any(source[2] is None for source in classification_sources.values()):
                retain_training_contexts(secondary_training_bindings, secondary_training_plan)
                continue
            children: list[_Context] = []
            secondary_reference_entries = {
                entry.project_entry_id
                for plan in _secondary_plan_projection.plan_set.plans
                for entry in plan.request.entries
            }
            training_by_entry: dict[str, list[AnalysisDomainBinding]] = {}
            for item in secondary_training_bindings:
                if item.project_entry_id in secondary_reference_entries:
                    training_by_entry.setdefault(item.project_entry_id, []).append(item)
            for entry_id, raw_entry_bindings in sorted(training_by_entry.items()):
                entry_bindings = tuple(sorted(raw_entry_bindings, key=_binding_key))
                sample_ids = {item.sample_id for item in entry_bindings}
                if len(sample_ids) != 1:
                    raise BatchPlanError("secondary evidence entry crosses sample identity")
                sample_id = next(iter(sample_ids))
                children.append(
                    builder.add(
                        "secondary_training_evidence_snapshot",
                        scope=NodeScope.SAMPLE,
                        participants=(sample_id,),
                        validation_domain=validation_domain,
                        bindings=entry_bindings,
                        entries=(entry_id,),
                        inputs=(
                            (
                                secondary_training_plan,
                                "training_plan_set",
                                accepted_secondary_plan,
                            ),
                        )
                        + accepted_domain_inputs(entry_bindings)
                        + unique_scientific_inputs(
                            classification_sources[item] for item in entry_bindings
                        ),
                    )
                )
            secondary_evidence_children = tuple(children)
            accepted_secondary_children = accepted_training_outputs(
                secondary_evidence_children,
                "secondary_training_evidence_snapshot",
                "secondary_classifier_training_review",
            )
            if accepted_secondary_children is None:
                retain_training_contexts(
                    secondary_training_bindings,
                    secondary_training_plan,
                    *secondary_evidence_children,
                )
                continue
            secondary_training = builder.add(
                "secondary_classifier_training_review",
                training_plan_projection=_secondary_plan_projection,
                scope=NodeScope.VALIDATION_DOMAIN,
                participants=secondary_training_participants,
                validation_domain=validation_domain,
                bindings=secondary_training_bindings,
                entries=tuple(
                    sorted({item.project_entry_id for item in secondary_training_bindings})
                ),
                inputs=(
                    (
                        secondary_training_plan,
                        "training_plan_set",
                        accepted_secondary_plan,
                    ),
                )
                + tuple(
                    (
                        child,
                        "secondary_training_evidence_snapshot",
                        accepted_child,
                    )
                    for child, accepted_child in zip(
                        secondary_evidence_children,
                        accepted_secondary_children,
                        strict=True,
                    )
                ),
            )
            matching_sets = tuple(
                artifact
                for artifact in _triggers(artifacts, "classifier_record_set")
                if artifact.producer_node_id == secondary_training.node.node_id
            )
            if len(matching_sets) > 1:
                raise BatchPlanError("secondary classifier record set authority is duplicated")
            if not matching_sets:
                retain_training_contexts(
                    secondary_training_bindings,
                    secondary_training_plan,
                    *secondary_evidence_children,
                    secondary_training,
                )
                continue
            selected_secondary = matching_sets[0]
            _validate_output(
                selected_secondary,
                secondary_training,
                builder,
                "secondary_classifier_inference",
            )
            _validate_secondary_record_set_coverage(
                selected_secondary,
                builder,
                secondary_training.node.participant_ids,
            )
        for binding in bindings:
            key, sample_id = _binding_key(binding), binding.sample_id
            frame = contexts[sample_id]["analysis_preflight"]
            common = {
                "scope": NodeScope.SAMPLE,
                "participants": (sample_id,),
                "validation_domain": validation_domain,
                "bindings": (binding,),
                "entries": (binding.project_entry_id,),
            }
            structures = builder.secondary_structures(sample_id)
            external_secondary = external_classifiers.get(("secondary", sample_id))
            secondary_nodes: list[_Context] = []
            classification_source = classification_input(binding)
            accepted_primary = classification_source[2]
            if structures and accepted_primary is None:
                secondary_nodes_by_binding[key] = ()
                if key in primaries:
                    result[key]["primary_classifier_inference"] = primaries[key]
                builder.defer(
                    "secondary_classifier_inference",
                    sample_id,
                    "primary_inference_result",
                    "primary_classifier_inference",
                )
                continue
            from .batch_classifier_inference_artifacts import needed_secondary_structure_ids
            secondary_by_id: dict[str, _Context] = {}
            for structure in structures:
                secondary_classifier_input: tuple[_Context | None, str, BatchArtifactRef | None] = (
                    (None, "classifier_record_set", external_secondary)
                    if external_secondary is not None
                    else (
                        secondary_training,
                        "classifier_record_set",
                        selected_secondary,
                    )
                )
                if secondary_classifier_input[0] is None and secondary_classifier_input[2] is None:
                    raise BatchPlanError("secondary classifier lacks exact record-set authority")
                needed = needed_secondary_structure_ids(structures, structure.structure_id)
                source_inputs = tuple(
                    (secondary_by_id[source_id], "secondary_inference_result",
                     builder.accepted_by_producer_kind.get((secondary_by_id[source_id].node.node_id, "secondary_inference_result")))
                    for source_id in needed
                )
                if any(accepted is None for _, _, accepted in source_inputs):
                    builder.defer(
                        "secondary_classifier_inference",
                        sample_id,
                        "secondary_inference_result",
                        "secondary_classifier_inference",
                    )
                    break
                secondary_node = builder.add(
                        "secondary_classifier_inference",
                        extra=(cell_inputs[key][1][0],),
                        **common,
                        structure_id=structure.structure_id,
                        classifier_id=structure.classifier.classifier_id,
                        eligible_parent_ids=tuple(structure.eligible_parent_ids),
                        inputs=(
                            (
                                selections[key],
                                "accepted_analysis_domain",
                                refs[key],
                            ),
                            (frame, "analysis_frame", None),
                            (
                                contexts[sample_id]["project_upsert"],
                                "project_catalogue_entry",
                                project_entries[sample_id],
                            ),
                            classification_source,
                            secondary_classifier_input,
                        ) + source_inputs,
                    )
                secondary_nodes.append(secondary_node)
                secondary_by_id[structure.structure_id] = secondary_node
            secondary_nodes_by_binding[key] = tuple(secondary_nodes)
            if key in primaries:
                result[key]["primary_classifier_inference"] = primaries[key]
            if len(secondary_nodes) == 1:
                result[key]["secondary_classifier_inference"] = secondary_nodes[0]
            if secondary_training is not None:
                result[key]["secondary_classifier_training_plan_review"] = secondary_training_plan
                result[key]["secondary_classifier_training_review"] = secondary_training


    for raw_bindings in groups.values():
        for binding in raw_bindings:
            key, sample_id = _binding_key(binding), binding.sample_id
            if key not in cell_inputs or key not in secondary_nodes_by_binding:
                continue
            secondaries = secondary_nodes_by_binding[key]
            if len(secondaries) != len(builder.secondary_structures(sample_id)):
                continue
            secondary_inputs = tuple((secondary, "secondary_inference_result",
                builder.accepted_by_producer_kind.get((secondary.node.node_id, "secondary_inference_result")))
                for secondary in secondaries)
            if any(accepted is None for _owner, _kind, accepted in secondary_inputs):
                continue
            measurements_input, phenotype_input = cell_inputs[key]
            common = dict(scope=NodeScope.SAMPLE, participants=(sample_id,),
                validation_domain=builder.samples[sample_id].validation_domain_id,
                bindings=(binding,), entries=(binding.project_entry_id,))
            snapshot = builder.add(
                "semantic_raw_inventory_snapshot", **common,
                inputs=(
                    (contexts[sample_id]["analysis_preflight"], "analysis_frame", None),
                    (contexts[sample_id]["tissue_acceptance"], "tissue_acceptance_result", None),
                    (selections[key], "accepted_analysis_domain", refs[key]),
                    classification_input(binding),
                    *secondary_inputs,
                    (result[key]["segmentation"], "segmentation_result",
                     builder.accepted_by_producer_kind.get((result[key]["segmentation"].node.node_id, "segmentation_result"))),
                    measurements_input, phenotype_input,
                ),
            )
            result[key]["semantic_raw_inventory_snapshot"] = snapshot
            if phenotype_input[1] != "cluster_label_import_result":
                continue
            accepted_snapshot = builder.accepted_by_producer_kind.get((snapshot.node.node_id, "semantic_raw_inventory"))
            if accepted_snapshot is None:
                builder.defer("phenotype_hierarchy_attestation", sample_id,
                    "semantic_raw_inventory", "semantic_raw_inventory_snapshot")
                continue
            result[key]["phenotype_hierarchy_attestation"] = builder.add(
                "phenotype_hierarchy_attestation", **common, section_target=section_targets[key],
                inputs=(phenotype_input, (snapshot, "semantic_raw_inventory", accepted_snapshot)),
            )

    if decisions_by_producer:
        raise BatchPlanError("accepted mode decision does not bind one materialized mode boundary")
    return result


def _inventory(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise BatchPlanError(f"{field} must be an exact string tuple")
    if len(set(value)) != len(value):
        raise BatchPlanError(f"{field} must be unique")
    return value


def _semantic_graph(
    builder: _Builder,
    contexts: dict[str, dict[str, _Context]],
    domain_contexts: dict[tuple[object, ...], dict[str, _Context]],
    artifacts: tuple[BatchArtifactRef, ...],
    external_artifacts: tuple[BatchArtifactRef, ...],
) -> set[str]:
    materialized_samples: set[str] = set()
    semantic_profiles = _external_set_per_sample(external_artifacts, "semantic_profile")
    artifacts_by_ref_sha256 = {item.artifact_ref_sha256: item for item in artifacts}
    seen_bindings: set[tuple[object, ...]] = set()
    completions_by_sample: dict[str, list[tuple[AnalysisDomainBinding, _Context, str]]] = {}
    sources_by_sample: dict[str, dict[str, _Context]] = {}
    raw_by_binding: dict[AnalysisDomainBinding, BatchArtifactRef] = {}
    applicable_by_binding: dict[
        AnalysisDomainBinding,
        tuple[tuple[BatchArtifactRef, ...], _Context, _Context],
    ] = {}
    accepted_domain_by_binding: dict[AnalysisDomainBinding, BatchArtifactRef] = {}
    for artifact in artifacts:
        if artifact.artifact_kind != "accepted_analysis_domain":
            continue
        for binding in artifact.scope.analysis_domain_bindings:
            prior = accepted_domain_by_binding.setdefault(binding, artifact)
            if prior != artifact:
                raise BatchPlanError("accepted analysis-domain authority is duplicated")
    accepted_binding_by_key = {
        _binding_key(binding): binding for binding in accepted_domain_by_binding
    }

    def binding_for(values: Mapping[str, _Context]) -> AnalysisDomainBinding:
        snapshot = values.get("semantic_raw_inventory_snapshot")
        if "excluded_section" in values:
            snapshot = values.get("primary_classifier_inference")
        if snapshot is None or len(snapshot.node.analysis_domain_bindings) != 1:
            raise BatchPlanError("semantic domain context lacks one accepted binding")
        return snapshot.node.analysis_domain_bindings[0]

    def accepted_domain_input(
        binding: AnalysisDomainBinding,
    ) -> tuple[_Context, str, BatchArtifactRef]:
        artifact = accepted_domain_by_binding.get(binding)
        selection = contexts[binding.sample_id].get("analysis_domain_selection")
        if artifact is None or selection is None:
            raise BatchPlanError("accepted analysis-domain authority is unavailable")
        if binding not in artifact.scope.analysis_domain_bindings:
            raise BatchPlanError("accepted analysis-domain authority has stale scope")
        return selection, "accepted_analysis_domain", artifact

    def _expected_sample_domain_bindings(
        sample_id: str,
        domain_contexts: Mapping[tuple[object, ...], dict[str, _Context]],
    ) -> tuple[AnalysisDomainBinding, ...]:
        del domain_contexts
        expected = tuple(
            sorted(
                (
                    binding
                    for binding in accepted_domain_by_binding
                    if binding.sample_id == sample_id
                ),
                key=_binding_key,
            )
        )
        if not expected:
            raise BatchPlanError("sample has no accepted analysis-domain bindings")
        if len(set(expected)) != len(expected):
            raise BatchPlanError("accepted analysis-domain ownership is duplicated")
        return expected

    def _single_project_entry(
        bindings: tuple[AnalysisDomainBinding, ...],
    ) -> tuple[str, ...]:
        entries = tuple(sorted({item.project_entry_id for item in bindings}))
        if len(entries) != 1:
            raise BatchPlanError("sample terminal requires one exact project entry")
        return entries

    def _contexts_for_expected(
        sample_id: str, expected: tuple[AnalysisDomainBinding, ...]
    ) -> dict[AnalysisDomainBinding, dict[str, _Context]]:
        rows: dict[AnalysisDomainBinding, dict[str, _Context]] = {}
        for key, values in domain_contexts.items():
            indexed_binding = accepted_binding_by_key.get(key)
            if indexed_binding is None:
                raise BatchPlanError("domain context lacks accepted-domain authority")
            if indexed_binding.sample_id != sample_id:
                continue
            binding = binding_for(values)
            if binding != indexed_binding:
                raise BatchPlanError("semantic domain context has stale binding ownership")
            if binding in expected:
                if binding in rows:
                    raise BatchPlanError("accepted analysis-domain ownership is duplicated")
                rows[binding] = values
        if tuple(sorted(rows, key=_binding_key)) != expected:
            raise BatchPlanError("terminal input coverage differs from accepted domains")
        if any(binding.sample_id != sample_id for binding in rows):
            raise BatchPlanError("terminal input crosses sample identity")
        return rows

    def _deduplicate_contexts(values: Sequence[_Context], *, field: str) -> tuple[_Context, ...]:
        result: dict[str, _Context] = {}
        for value in values:
            prior = result.setdefault(value.node.node_id, value)
            if prior is not value and prior.node != value.node:
                raise BatchPlanError(f"{field} authority is duplicated")
        return tuple(result[key] for key in sorted(result))

    def _exact_handoff_inputs(
        sample_id: str,
        expected: tuple[AnalysisDomainBinding, ...],
        completion_rows: Sequence[tuple[AnalysisDomainBinding, _Context, str]],
    ) -> tuple[tuple[_Context | None, str, BatchArtifactRef | None], ...]:
        def input_for(
            context: _Context, artifact_kind: str
        ) -> tuple[_Context, str, BatchArtifactRef | None]:
            return (
                context,
                artifact_kind,
                builder.accepted_by_producer_kind.get((context.node.node_id, artifact_kind)),
            )

        values_by_binding = _contexts_for_expected(sample_id, expected)
        analyzed = tuple(binding for binding in expected
                         if "excluded_section" not in values_by_binding[binding])
        completion_by_binding = {item[0]: item for item in completion_rows}
        if len(completion_by_binding) != len(completion_rows):
            raise BatchPlanError("completion union has overlap or duplicated domain ownership")
        if tuple(sorted(completion_by_binding, key=_binding_key)) != expected:
            raise BatchPlanError("completion union differs from accepted domains")
        source_contexts = _deduplicate_contexts(
            tuple(
                sources_by_sample[sample_id][
                    values_by_binding[binding]["semantic_raw_inventory_snapshot"].node.node_id
                ]
                for binding in analyzed
            ),
            field="selected phenotype",
        )
        return (
            (
                input_for(contexts[sample_id]["analysis_preflight"], "analysis_frame"),
                input_for(
                    contexts[sample_id]["tissue_acceptance"],
                    "tissue_acceptance_result",
                ),
            )
            + tuple(
                {
                    artifact.artifact_ref_sha256: (selection, kind, artifact)
                    for binding in expected
                    for selection, kind, artifact in (accepted_domain_input(binding),)
                }[artifact_ref_sha256]
                for artifact_ref_sha256 in sorted(
                    {accepted_domain_input(binding)[2].artifact_ref_sha256 for binding in expected}
                )
            )
            + tuple(
                input_for(value, "segmentation_result")
                for value in _deduplicate_contexts(
                    tuple(values_by_binding[binding]["segmentation"] for binding in expected),
                    field="segmentation",
                )
            )
            + tuple(
                input_for(value, "measurements_result")
                for value in _deduplicate_contexts(
                    tuple(values_by_binding[binding]["measurements"] for binding in analyzed),
                    field="measurements",
                )
            )
            + tuple(
                input_for(value, value.node.declared_output_kinds[0]) for value in source_contexts
            )
            + tuple(
                input_for(value, "phenotype_view_attestation")
                for value in _deduplicate_contexts(
                    tuple(values_by_binding[binding]["phenotype_hierarchy_attestation"]
                          for binding in analyzed
                          if "phenotype_hierarchy_attestation" in values_by_binding[binding]),
                    field="phenotype hierarchy attestation",
                )
            )
            + tuple(
                (
                    values_by_binding[binding]["semantic_raw_inventory_snapshot"],
                    "semantic_raw_inventory",
                    raw_by_binding[binding],
                )
                for binding in analyzed
            )
            + tuple(
                input_for(
                    completion_by_binding[binding][1],
                    completion_by_binding[binding][2],
                )
                for binding in expected
            )
        )

    def _exact_final_export_inputs(
        sample_id: str,
        expected: tuple[AnalysisDomainBinding, ...],
        completion_rows: Sequence[tuple[AnalysisDomainBinding, _Context, str]],
        handoff: _Context,
    ) -> tuple[tuple[_Context | None, str, BatchArtifactRef | None], ...]:
        handoff_inputs = _exact_handoff_inputs(sample_id, expected, completion_rows)
        base = handoff_inputs[:2] + tuple(
            item for item in handoff_inputs[2:] if item[1] not in {"segmentation_result", "primary_inference_result"}
        )
        analyzed_rows = tuple(item for item in completion_rows if item[2] != "primary_inference_result")
        completion_kinds = {item[2] for item in analyzed_rows}
        if not completion_kinds or completion_kinds == {"no_secondary_completion_evidence"}:
            return base + ((handoff, "spatial_cell_handoff", None),)
        if completion_kinds != {"semantic_finalization_result"}:
            raise BatchPlanError("completion union is configuration-inconsistent")
        applicable = tuple(applicable_by_binding.get(binding) for binding, _context, _kind in analyzed_rows)
        if any(item is None for item in applicable):
            raise BatchPlanError("configured-secondary terminal lacks semantic authority")
        typed = tuple(item for item in applicable if item is not None)
        profiles = {
            profile.artifact_ref_sha256: profile
            for profile_set, _evidence, _evaluation in typed
            for profile in profile_set
        }
        return (
            base
            + tuple((None, "semantic_profile", profiles[key]) for key in sorted(profiles))
            + tuple(
                (evidence, "semantic_evidence_result", None)
                for _profiles, evidence, _evaluation in typed
            )
            + tuple(
                (evaluation, "semantic_evaluation_result", None)
                for _profiles, _evidence, evaluation in typed
            )
            + ((handoff, "spatial_cell_handoff", None),)
        )

    for key, values in domain_contexts.items():
        if "excluded_section" not in values:
            continue
        binding = accepted_binding_by_key[key]
        primary = values["primary_classifier_inference"]
        completions_by_sample.setdefault(binding.sample_id, []).append(
            (binding, primary, "primary_inference_result"))
        materialized_samples.add(binding.sample_id)

    for artifact in _triggers(artifacts, "semantic_raw_inventory"):
        matches = [
            (key, values)
            for key, values in domain_contexts.items()
            if "semantic_raw_inventory_snapshot" in values
            and artifact.scope.analysis_domain_bindings
            == values["semantic_raw_inventory_snapshot"].node.analysis_domain_bindings
        ]
        if len(matches) != 1:
            raise BatchPlanError("semantic inventory does not bind one materialized domain")
        key, values = matches[0]
        if key in seen_bindings:
            raise BatchPlanError("semantic inventory authority is duplicated")
        seen_bindings.add(key)
        snapshot = values["semantic_raw_inventory_snapshot"]
        _validate_output(artifact, snapshot, builder, "no_secondary_completion")
        if artifact.scope != builder.output_scope(snapshot):
            raise BatchPlanError("semantic inventory has stale domain scope")
        phenotype_source_ref_sha256 = artifact.owning_identity.get(
            "phenotype_source_artifact_ref_sha256"
        )
        phenotype_source = artifacts_by_ref_sha256.get(phenotype_source_ref_sha256)
        if phenotype_source is None:
            raise BatchPlanError("semantic inventory phenotype source is unavailable")
        source_edges = tuple(
            edge
            for edge in builder.edges
            if edge.consumer_node_id == snapshot.node.node_id
            and edge.artifact_kind
            in {
                "cluster_label_import_result",
                "marker_phenotype_result",
                "supervised_phenotype_result",
            }
        )
        if len(source_edges) != 1:
            raise BatchPlanError("semantic snapshot lacks one phenotype source input")
        source_edge = source_edges[0]
        if (
            phenotype_source.artifact_kind != source_edge.artifact_kind
            or (
                source_edge.accepted_artifact_ref_sha256 is not None
                and phenotype_source_ref_sha256 != source_edge.accepted_artifact_ref_sha256
            )
            or (
                source_edge.accepted_artifact_ref_sha256 is None
                and phenotype_source.producer_node_id != source_edge.producer_node_id
            )
            or phenotype_source.scope != source_edge.requirement.scope
            or (
                phenotype_source.configuration_bindings
                != source_edge.requirement.configuration_bindings
            )
            or (
                phenotype_source.compatibility_signature_sha256
                != source_edge.requirement.compatibility_signature_sha256
            )
        ):
            raise BatchPlanError("semantic inventory phenotype source differs from snapshot input")
        source_contexts = tuple(
            item for item in values.values() if item.node.node_id == source_edge.producer_node_id
        )
        if len(source_contexts) != 1:
            raise BatchPlanError("semantic inventory phenotype source context is unavailable")
        source_context = source_contexts[0]
        hierarchy_input: tuple[_Context, str, BatchArtifactRef] | None = None
        if phenotype_source.artifact_kind == "cluster_label_import_result":
            attestation_context = values.get("phenotype_hierarchy_attestation")
            matching_attestations = tuple(
                item
                for item in artifacts
                if item.artifact_kind == "phenotype_view_attestation"
                and item.scope == artifact.scope
                and item.owning_identity.get("cluster_label_import_result_ref_sha256")
                == phenotype_source_ref_sha256
            )
            if len(matching_attestations) > 1:
                raise BatchPlanError("semantic hierarchy attestation is ambiguous")
            if attestation_context is None or not matching_attestations:
                continue
            hierarchy_input = (
                attestation_context,
                "phenotype_view_attestation",
                matching_attestations[0],
            )
        elif phenotype_source.artifact_kind not in {
            "marker_phenotype_result",
            "supervised_phenotype_result",
        }:
            raise BatchPlanError("semantic inventory phenotype source kind is invalid")
        configured = _inventory(
            artifact.owning_identity.get("configured_secondary_ids"),
            "configured secondary inventory",
        )
        candidates = _inventory(
            artifact.owning_identity.get("candidate_secondary_roi_ids"),
            "candidate secondary inventory",
        )
        binding = snapshot.node.analysis_domain_bindings[0]
        sample_id = binding.sample_id
        common = {
            "scope": NodeScope.SAMPLE,
            "participants": (sample_id,),
            "validation_domain": builder.samples[sample_id].validation_domain_id,
            "bindings": (binding,),
            "entries": (binding.project_entry_id,),
        }
        materialized_samples.add(sample_id)
        sources_by_sample.setdefault(sample_id, {})[snapshot.node.node_id] = source_context
        raw_by_binding[binding] = artifact
        if not configured and not candidates:
            owner_contexts = (
                (contexts[sample_id]["analysis_preflight"], "analysis_frame"),
                accepted_domain_input(binding)[:2],
                (values["measurements"], "measurements_result"),
            )
            owner_inputs = []
            for owner_context, owner_kind in owner_contexts:
                owner = builder.accepted_by_producer_kind.get(
                    (owner_context.node.node_id, owner_kind)
                )
                if owner is None:
                    matches = tuple(
                        item
                        for item in artifacts
                        if item.artifact_kind == owner_kind
                        and item.producer_node_id == owner_context.node.node_id
                    )
                    if len(matches) != 1:
                        raise BatchPlanError("no-secondary completion owner is unavailable")
                    owner = matches[0]
                owner_inputs.append((owner_context, owner_kind, owner))
            completion = builder.add(
                "no_secondary_completion",
                **common,
                inputs=tuple(owner_inputs)
                + ((snapshot, "semantic_raw_inventory", artifact),)
                + ((source_context, phenotype_source.artifact_kind, phenotype_source),)
                + (() if hierarchy_input is None else (hierarchy_input,)),
            )
            completion_kind = "no_secondary_completion_evidence"
        else:
            if sample_id not in semantic_profiles:
                raise BatchPlanError("applicable semantic branch lacks its profile exact set")
            semantic_profile_set = _semantic_profile_set(
                semantic_profiles[sample_id], builder.samples[sample_id],
                builder.secondary_definition_selections.get(sample_id),
            )
            owner_inputs: list[tuple[_Context, str, BatchArtifactRef]] = []
            for owner_context, owner_kind in (
                (contexts[sample_id]["analysis_preflight"], "analysis_frame"),
                accepted_domain_input(binding)[:2],
                (values["measurements"], "measurements_result"),
            ):
                owner = builder.accepted_by_producer_kind.get(
                    (owner_context.node.node_id, owner_kind)
                )
                if owner is None:
                    matches = tuple(
                        item
                        for item in artifacts
                        if item.artifact_kind == owner_kind
                        and item.producer_node_id == owner_context.node.node_id
                    )
                    if len(matches) != 1:
                        raise BatchPlanError("applicable semantic owner is unavailable")
                    owner = matches[0]
                owner_inputs.append((owner_context, owner_kind, owner))
            frame_input, domain_input, measurements_input = owner_inputs
            profile_inputs = tuple(
                (None, "semantic_profile", item) for item in semantic_profile_set
            )
            evidence = builder.add(
                "semantic_evidence",
                **common,
                inputs=((snapshot, "semantic_raw_inventory", artifact),)
                + (frame_input, domain_input, measurements_input)
                + profile_inputs
                + ((source_context, phenotype_source.artifact_kind, phenotype_source),)
                + (() if hierarchy_input is None else (hierarchy_input,)),
            )
            evaluation = builder.add(
                "semantic_evaluation",
                **common,
                inputs=((evidence, "semantic_evidence_result", None),)
                + ((snapshot, "semantic_raw_inventory", artifact),)
                + profile_inputs,
            )
            count_cap = builder.add(
                "final_count_cap",
                **common,
                inputs=((evaluation, "semantic_evaluation_result", None),)
                + ((snapshot, "semantic_raw_inventory", artifact), domain_input),
            )
            completion = builder.add(
                "semantic_finalization",
                **common,
                inputs=(
                    (evaluation, "semantic_evaluation_result", None),
                    (count_cap, "final_count_cap_result", None),
                    (snapshot, "semantic_raw_inventory", artifact),
                    frame_input,
                    domain_input,
                    measurements_input,
                ),
            )
            completion_kind = "semantic_finalization_result"
            applicable_by_binding[binding] = (semantic_profile_set, evidence, evaluation)
        completions_by_sample.setdefault(sample_id, []).append(
            (binding, completion, completion_kind)
        )
    for sample_id, completion_rows in sorted(completions_by_sample.items()):
        expected = _expected_sample_domain_bindings(sample_id, domain_contexts)
        actual = tuple(sorted((item[0] for item in completion_rows), key=_binding_key))
        if len(set(actual)) != len(actual):
            raise BatchPlanError("completion union has overlap or duplicated domain ownership")
        if actual != expected:
            continue
        completion_kinds = {item[2] for item in completion_rows} - {"primary_inference_result"}
        if completion_kinds not in (
            set(),
            {"semantic_finalization_result"},
            {"no_secondary_completion_evidence"},
        ):
            raise BatchPlanError(
                "completion union is configuration-inconsistent across accepted domains"
            )
        entries = _single_project_entry(expected)
        common = {
            "scope": NodeScope.SAMPLE,
            "participants": (sample_id,),
            "validation_domain": builder.samples[sample_id].validation_domain_id,
            "bindings": expected,
            "entries": entries,
        }
        handoff = builder.add(
            "phenotype_handoff",
            **common,
            inputs=_exact_handoff_inputs(sample_id, expected, completion_rows),
        )
        builder.add(
            "final_non_spatial_export",
            **common,
            inputs=_exact_final_export_inputs(sample_id, expected, completion_rows, handoff),
        )
    return materialized_samples


def _build(
    manifest: ValidatedBatchManifest,
    registry: StageAdapterRegistry,
    artifacts: tuple[BatchArtifactRef, ...],
    external_artifacts: tuple[BatchArtifactRef, ...],
    accepted_by_producer_kind: Mapping[tuple[str, str], BatchArtifactRef],
    owner_projections: Mapping[str, object],
    training_settings_revisions: Mapping[tuple[str, str], TrainingSettingsRevision] | None = None,
    segmentation_settings_revisions: Mapping[tuple[str, str], SegmentationSettingsRevision] | None = None,
    measurement_settings_revisions: Mapping[tuple[str, str], MeasurementSettingsRevision] | None = None,
    settled_review_revisions: Mapping[ReviewKey, SettledReviewRevision] | None = None,
    saved_result_retirements: tuple[SavedResultRetirement, ...] = (),
    cell_corrections: tuple[CellCorrectionRequest, ...] = (),
    supervised_cutoff_selections: tuple[SupervisedCutoffSelection, ...] = (),
) -> _Builder:
    builder = _Builder(manifest, registry, accepted_by_producer_kind, training_settings_revisions,
                       segmentation_settings_revisions, measurement_settings_revisions, settled_review_revisions,
                       cell_corrections, supervised_cutoff_selections)
    source_returns = {revision.predecessor_node_id
        for (kind, _domain, _participants, _regions), revision in (settled_review_revisions or {}).items()
        if kind == "inspection"}
    # The old entry is authenticated by each saved result. A source return must
    # retire it before inspection, before any replacement upsert exists.
    for retirement in saved_result_retirements:
        source = retirement.sources[0]
        sample_id, entry_id = source.scope.sample_id, source.scope.project_entry_id
        bindings = tuple(sorted({item for ref in retirement.sources for item in ref.scope.analysis_domain_bindings}, key=_binding_key))
        retired = builder.add(SAVED_RESULT_RETIREMENT_KIND, scope=NodeScope.SAMPLE, participants=(sample_id,),
            validation_domain=source.scope.validation_domain_id, bindings=bindings, entries=(entry_id,),
            saved_result_retirement=retirement)
        builder.saved_result_retirement_barriers.setdefault(retirement.predecessor_node_id, []).append(retired)
    contexts = _early_graph(builder, artifacts, external_artifacts)
    projects = _project_graph(builder, contexts, artifacts, external_artifacts)
    if any(source.scope.sample_id not in projects
            or projects[source.scope.sample_id].scope.project_entry_id != source.scope.project_entry_id
            for retirement in saved_result_retirements if retirement.predecessor_node_id not in source_returns
            for source in retirement.sources):
        raise BatchPlanError("saved result retirement differs from its retained project entry")
    refs, selections, groups, section_targets = _domain_authorities(
        builder, contexts, artifacts, owner_projections
    )
    domain_contexts = _domain_graph(
        builder,
        contexts,
        refs,
        selections,
        groups,
        section_targets,
        owner_projections,
        artifacts,
        external_artifacts,
    )
    inventory_samples = _semantic_graph(
        builder,
        contexts,
        domain_contexts,
        artifacts,
        external_artifacts,
    )
    domain_samples = {binding.sample_id for values in groups.values() for binding in values}
    saved_classifiers = _external_classifiers(external_artifacts, builder)
    for sample in sorted(manifest.enabled_samples, key=lambda item: item.sample_id):
        sample_contexts = contexts[sample.sample_id]
        early_forecasts = (
            ("channel_review", "source_inspection", "inspection"),
            ("reference_unmixing", "stage1_review", "channel_review"),
            ("analysis_preflight", "processed_image", "reference_unmixing"),
            ("project_upsert", "analysis_frame", "analysis_preflight"),
        )
        for kind, unresolved, prerequisite in early_forecasts:
            if kind not in sample_contexts:
                builder.defer(kind, sample.sample_id, unresolved, prerequisite)
        if sample.sample_id not in projects:
            for kind in _PROJECT:
                if kind == "paired_image_preparation":
                    continue
                builder.defer(kind, sample.sample_id, "project_entry_id", "project_upsert")
        if sample.sample_id not in domain_samples:
            for kind in _DOMAIN:
                if kind == "secondary_structure_definition":
                    continue
                if kind.startswith("secondary_") and sample.config.secondary_structures == ():
                    continue
                if (kind in {"primary_classifier_training_plan_review", "primary_training_evidence_snapshot",
                             "primary_classifier_training_review"}
                        and ("primary", sample.sample_id) in saved_classifiers):
                    continue
                if (kind in {"secondary_classifier_training_plan_review", "secondary_training_evidence_snapshot",
                             "secondary_classifier_training_review"}
                        and ("secondary", sample.sample_id) in saved_classifiers):
                    continue
                builder.defer(
                    kind, sample.sample_id, "analysis_domain_bindings", "analysis_domain_selection"
                )
        elif not any(
            node.kind == "segmentation" and sample.sample_id in node.participant_ids
            for node in builder.nodes
        ):
            # Classifier-plan review can suspend the domain graph before cell
            # work is materialized. Keep its required software and visible steps
            # associated with the pending inference results during that interval.
            for kind, unresolved, prerequisite in (
                ("segmentation", "classifier_inference", "primary_classifier_inference"),
                ("measurements", "segmentation_result", "segmentation"),
                ("phenotyping_mode_resolution", "measurements_result", "measurements"),
            ):
                builder.defer(kind, sample.sample_id, unresolved, prerequisite)
        if (sample.config.secondary_structures is None and not any(
                node.kind == "secondary_structure_definition" and sample.sample_id in node.participant_ids
                for node in builder.nodes)):
            builder.defer("secondary_structure_definition", sample.sample_id,
                          "selected_phenotype_result", "phenotyping_mode_resolution")
        if sample.sample_id not in inventory_samples:
            for kind in _BRANCH:
                builder.defer(
                    kind, sample.sample_id, "semantic_inventory", "semantic_raw_inventory_snapshot"
                )
    return builder


def _adapter_digest(registry: StageAdapterRegistry) -> str:
    return _digest({"stage_adapter_spec_sha256s": [item.spec_sha256 for item in registry.specs]})


def _make_plan(
    manifest: ValidatedBatchManifest,
    registry: StageAdapterRegistry,
    builder: _Builder,
    external: tuple[BatchArtifactRef, ...],
    revision: int,
    predecessor: str | None,
    workspace_rebinding_sha256: str | None,
    prerequisites: tuple[BatchPrerequisiteItem, ...] = (),
) -> BatchPlan:
    nodes = tuple(sorted(builder.nodes, key=lambda item: item.node_id))
    edges = tuple(
        sorted(
            builder.edges,
            key=lambda item: (
                item.consumer_node_id,
                item.artifact_kind,
                item.artifact_key,
            ),
        )
    )
    deferred = tuple(sorted(builder.deferred, key=lambda item: item.descriptor_sha256))
    adapter_digest = _adapter_digest(registry)
    unsigned = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "batch_id": manifest.manifest.batch_id,
        "normalized_manifest_sha256": manifest.normalized_manifest_sha256,
        "revision": revision,
        "predecessor_plan_sha256": predecessor,
        "workspace_rebinding_sha256": workspace_rebinding_sha256,
        "nodes": [item.to_mapping() for item in nodes],
        "edges": [item.to_mapping() for item in edges],
        "external_inputs": [item.to_mapping() for item in external],
        "deferred_nodes": [item.to_mapping() for item in deferred],
        "prerequisites": [item.to_mapping() for item in prerequisites],
        "adapter_spec_set_sha256": adapter_digest,
    }
    if builder.supervised_cutoff_selections:
        unsigned["supervised_cutoff_selections"] = [item.to_mapping() for item in builder.supervised_cutoff_selections]
    return BatchPlan(
        SCHEMA_NAME,
        SCHEMA_VERSION,
        manifest.manifest.batch_id,
        manifest.normalized_manifest_sha256,
        revision,
        predecessor,
        workspace_rebinding_sha256,
        nodes,
        edges,
        external,
        deferred,
        prerequisites,
        adapter_digest,
        _digest(unsigned),
        builder.supervised_cutoff_selections,
    )


def _validate_plan_instance(plan: BatchPlan, field: str) -> BatchPlan:
    if not isinstance(plan, BatchPlan):
        raise BatchPlanError(f"{field} must be a BatchPlan")
    try:
        replace(plan)
    except BatchPlanError:
        raise
    except (TypeError, ValueError) as exc:
        raise BatchPlanError(f"{field} is not internally canonical") from exc
    return plan


def plan_batch(
    manifest: ValidatedBatchManifest,
    adapter_specs: StageAdapterRegistry,
    accepted_artifacts: tuple[BatchArtifactRef, ...] = (),
) -> BatchPlan:
    """Create an initial immutable plan without guessing future identities."""
    manifest = _validate_manifest(manifest)
    _validate_registry(adapter_specs)
    internal, external, accepted_by_producer_kind, accepted_domain_projections = _accepted(
        accepted_artifacts, manifest
    )
    if internal:
        raise BatchPlanError("initial plan accepts only validated manifest_external inputs")
    return _make_plan(
        manifest,
        adapter_specs,
        _build(
            manifest,
            adapter_specs,
            (),
            external,
            accepted_by_producer_kind,
            accepted_domain_projections,
        ),
        external,
        1,
        None,
        None,
    )


def expand_batch_plan(
    previous: BatchPlan,
    manifest: ValidatedBatchManifest,
    adapter_specs: StageAdapterRegistry,
    accepted_artifacts: tuple[BatchArtifactRef, ...],
    *,
    accepted_artifact_authorities: tuple[BatchCommittedArtifactAuthority, ...] = (),
    batch_state_store: object | None = None,
    training_plan_set_store: object | None = None,
    training_settings_revision: TrainingSettingsRevision | None = None,
    segmentation_settings_revision: SegmentationSettingsRevision | None = None,
    measurement_settings_revision: MeasurementSettingsRevision | None = None,
    settled_review_revision: SettledReviewRevision | None = None,
    settled_review_revisions: tuple[SettledReviewRevision, ...] = (),
    supervised_cutoff_selections: tuple[SupervisedCutoffSelection, ...] | None = None,
    saved_result_retirements: tuple[SavedResultRetirement, ...] = (),
    cell_correction: CellCorrectionRequest | None = None,
    cell_corrections: tuple[CellCorrectionRequest, ...] = (),
) -> BatchPlan:
    """Create a predecessor-linked successor from exact accepted artifacts."""
    previous = _validate_plan_instance(previous, "previous")
    manifest = _validate_manifest(manifest)
    _validate_registry(adapter_specs)
    if (
        previous.batch_id != manifest.manifest.batch_id
        or previous.adapter_spec_set_sha256 != _adapter_digest(adapter_specs)
    ):
        raise BatchPlanError("successor authority differs from its predecessor")
    source_sample_id = None
    if previous.normalized_manifest_sha256 != manifest.normalized_manifest_sha256:
        from .batch_source_revision import source_revision_sample
        if batch_state_store is None:
            raise BatchPlanError("source correction requires its retained manifest owner")
        old_manifest = batch_state_store.retained_manifest(previous.normalized_manifest_sha256)
        if old_manifest.manifest.to_mapping().get("reusable_artifacts", []) != manifest.manifest.to_mapping().get("reusable_artifacts", []):
            before = old_manifest.manifest.reusable_artifacts
            after = manifest.manifest.reusable_artifacts
            changed = tuple(item for item in before if item not in after) + tuple(item for item in after if item not in before)
            if {item.artifact_kind for item in changed} == {"semantic_profile"}:
                from .batch_secondary_definitions import validate_secondary_profile_manifest_transition
                validate_secondary_profile_manifest_transition(old_manifest, manifest)
            else:
                from .batch_training.accepted_model_reuse import validate_classifier_manifest_transition
                validate_classifier_manifest_transition(old_manifest, manifest)
        else:
            source_sample_id = source_revision_sample(old_manifest, manifest)
    internal, external, accepted_by_producer_kind, accepted_domain_projections = _accepted(
        accepted_artifacts,
        manifest,
        accepted_artifact_authorities,
        expected_active_plan_sha256=previous.plan_sha256,
        batch_state_store=batch_state_store,
        training_plan_set_store=training_plan_set_store,
    )
    previous_nodes = {item.node_id: item for item in previous.nodes}
    corrections = tuple(item.cell_correction for item in previous.nodes if item.cell_correction is not None)
    introduced_corrections = ((cell_correction,) if cell_correction is not None else ()) + tuple(cell_corrections)
    introduced_bases = set()
    for cell_correction in introduced_corrections:
        if type(cell_correction) is not CellCorrectionRequest:
            raise BatchPlanError("cell correction requires its exact reviewed draft")
        if cell_correction.segmentation_result_ref_sha256 in introduced_bases:
            raise BatchPlanError("A section's correction can be introduced only once per successor.")
        introduced_bases.add(cell_correction.segmentation_result_ref_sha256)
        predecessor = previous_nodes.get(cell_correction.predecessor_node_id)
        accepted_by_ref = {item.artifact_ref_sha256: item for item in internal}
        base = accepted_by_ref.get(cell_correction.segmentation_result_ref_sha256)
        prior = accepted_by_ref.get(cell_correction.previous_correction_ref_sha256)
        if (predecessor is None or predecessor.kind not in {"segmentation", "measurements", CELL_CORRECTION_KIND}
                or base is None or base.artifact_kind != "segmentation_result"
                or predecessor.section_target is None
                or predecessor.section_target.section_id != base.owning_identity["section_id"]
                or predecessor.project_entry_ids != (base.scope.project_entry_id,)
                or predecessor.analysis_domain_bindings != base.scope.analysis_domain_bindings):
            raise BatchPlanError("cell correction differs from its current accepted section")
        if cell_correction.previous_correction_ref_sha256 is not None and (
                prior is None or prior.artifact_kind != "segmentation_correction_result"
                or prior.owning_identity["segmentation_result_ref_sha256"] != base.artifact_ref_sha256):
            raise BatchPlanError("cell correction lacks its selected accepted predecessor")
        corrections = (*corrections, cell_correction)
    revisions = {
        (item.kind, item.validation_domain_id): item.training_settings_revision
        for item in previous.nodes
        if item.training_settings_revision is not None
    }
    if training_settings_revision is not None:
        if type(training_settings_revision) is not TrainingSettingsRevision:
            raise BatchPlanError("training settings revision must be typed")
        predecessor = previous_nodes.get(training_settings_revision.predecessor_plan_review_node_id)
        if predecessor is None or predecessor.kind not in _TRAINING_PLAN_REVIEW_KINDS:
            raise BatchPlanError("training settings revision lacks its current predecessor")
        revisions[(predecessor.kind, predecessor.validation_domain_id)] = training_settings_revision
    segmentation_revisions = {
        (item.participant_ids[0], item.section_target.section_id): item.segmentation_settings_revision
        for item in previous.nodes if item.segmentation_settings_revision is not None
    }
    if segmentation_settings_revision is not None:
        predecessor = previous_nodes.get(segmentation_settings_revision.predecessor_node_id)
        if predecessor is None or predecessor.kind != "segmentation" or predecessor.section_target is None:
            raise BatchPlanError("segmentation revision lacks its current predecessor")
        prior = predecessor.segmentation_settings_revision
        if segmentation_settings_revision.revision != (1 if prior is None else prior.revision + 1):
            raise BatchPlanError("segmentation revision is not the exact next selection")
        segmentation_revisions[(predecessor.participant_ids[0], predecessor.section_target.section_id)] = segmentation_settings_revision
    measurement_revisions = {
        (item.participant_ids[0], item.section_target.section_id): item.measurement_settings_revision
        for item in previous.nodes if item.measurement_settings_revision is not None
    }
    # A correction defers measurements until its native checkpoint is accepted.
    # Restore the bound completed choices when that successor becomes concrete.
    if batch_state_store is not None:
        from .batch_cell_corrections import read_correction_submission, measurement_revision_for_correction
        accepted_bases = {ref.artifact_ref_sha256:ref for ref in internal if ref.artifact_kind == 'segmentation_result'}
        accepted_corrections = {ref.producer_node_id for ref in internal if ref.artifact_kind == 'segmentation_correction_result'}
        for node in previous.nodes:
            if node.cell_correction is None or node.node_id not in accepted_corrections:
                continue
            selected = node.cell_correction
            revision = measurement_revision_for_correction(batch_state_store, selected,
                read_correction_submission(batch_state_store, selected), accepted_bases[selected.segmentation_result_ref_sha256])
            key = (node.participant_ids[0], node.section_target.section_id)
            if revision is not None and (key not in measurement_revisions or measurement_revisions[key].revision < revision.revision):
                measurement_revisions[key] = revision
    if measurement_settings_revision is not None:
        if type(measurement_settings_revision) is not MeasurementSettingsRevision:
            raise BatchPlanError("measurement settings revision must be typed")
        predecessor = previous_nodes.get(measurement_settings_revision.predecessor_node_id)
        if predecessor is None or predecessor.kind != "measurements" or predecessor.section_target is None:
            raise BatchPlanError("measurement revision lacks its current predecessor")
        prior = predecessor.measurement_settings_revision
        if measurement_settings_revision.revision != (1 if prior is None else prior.revision + 1):
            raise BatchPlanError("measurement revision is not the exact next selection")
        measurement_revisions[(predecessor.participant_ids[0], predecessor.section_target.section_id)] = measurement_settings_revision
    review_revisions = {review_key(item): item.settled_review_revision
        for item in previous.nodes if item.settled_review_revision is not None
        and (source_sample_id is None or source_sample_id not in item.participant_ids)}
    if type(settled_review_revisions) is not tuple:
        raise BatchPlanError("Settled review revisions must be an immutable tuple.")
    introduced_reviews = ((settled_review_revision,) if settled_review_revision is not None else ()) + settled_review_revisions
    introduced_keys = set()
    for selected_revision in introduced_reviews:
        if type(selected_revision) is not SettledReviewRevision:
            raise BatchPlanError("settled review revision must be typed")
        predecessor = previous_nodes.get(selected_revision.predecessor_node_id)
        if predecessor is None or predecessor.kind not in SETTLED_REVIEW_KINDS:
            raise BatchPlanError("settled review revision lacks its current predecessor")
        prior = predecessor.settled_review_revision
        if selected_revision.revision != (1 if prior is None else prior.revision + 1):
            raise BatchPlanError("settled review revision is not the exact next selection")
        key = review_key(predecessor)
        if key in introduced_keys:
            raise BatchPlanError("A settled review scope can be revised only once per successor.")
        introduced_keys.add(key)
        if predecessor.kind in {"channel_review", "tissue_setup", "tissue_resolution", "tissue_candidate"}:
            affected = dependent_node_ids(
                ((item.node_id, item.dependency_ids) for item in previous.nodes),
                {predecessor.node_id},
            ) - {predecessor.node_id}
            # The new upstream decision controls rebuilt reviews. Their
            # former return links remain in the predecessor plan, not on the
            # new tasks where they would override this return's lineage.
            reset_kinds = ({"reference_unmixing"} if predecessor.kind == "channel_review"
                           else {"tissue_setup", "tissue_resolution", "tissue_candidate"})
            for item in previous.nodes:
                if item.node_id in affected and item.kind in reset_kinds:
                    review_revisions.pop(review_key(item), None)
        review_revisions[key] = selected_revision
    carried_retirements = tuple(item.saved_result_retirement for item in previous.nodes if item.saved_result_retirement is not None)
    revised_predecessors = {item.predecessor_node_id for item in (segmentation_settings_revision, measurement_settings_revision, *introduced_reviews, *introduced_corrections) if item is not None}
    superseded = {revision_predecessor(previous_nodes[item]) for item in revised_predecessors}
    carried_retirements = tuple(item for item in carried_retirements if item.predecessor_node_id not in superseded
        and (source_sample_id is None or not any(ref.scope.sample_id == source_sample_id for ref in item.sources)))
    if any(type(item) is not SavedResultRetirement or item.predecessor_node_id not in revised_predecessors
           or item.predecessor_plan_sha256 != previous.plan_sha256 for item in saved_result_retirements):
        raise BatchPlanError("saved result retirement requires its introduced current revision")
    saved_result_retirements = (*carried_retirements, *saved_result_retirements)
    for artifact in internal:
        producer = previous_nodes.get(artifact.producer_node_id or "")
        if producer is None or artifact.artifact_kind not in producer.declared_output_kinds:
            raise BatchPlanError(
                "accepted internal output lacks exact previous plan producer authority"
            )
    configurations = {item.sample_id: item.resolved_config_sha256 for item in manifest.enabled_samples}
    current_declarations = {item.declaration_sha256 for item in manifest.manifest.reusable_artifacts}
    external_by_ref = {item.artifact_ref_sha256: item for item in previous.external_inputs
        if (item.origin is not ArtifactOrigin.MANIFEST_EXTERNAL or item.external_declaration_sha256 in current_declarations)
        and all(configurations.get(binding.sample_id) == binding.resolved_config_sha256
               for binding in item.configuration_bindings)}
    external_by_ref.update({item.artifact_ref_sha256: item for item in external})
    external = tuple(sorted(external_by_ref.values(), key=lambda item: item.artifact_ref_sha256))
    workspace_rebinding = previous.workspace_rebinding_sha256

    candidate = _make_plan(
        manifest,
        adapter_specs,
        _build(
            manifest,
            adapter_specs,
            internal,
            external,
            accepted_by_producer_kind,
            accepted_domain_projections,
            revisions,
            segmentation_revisions,
            measurement_revisions,
            review_revisions,
            saved_result_retirements,
            corrections,
            previous.supervised_cutoff_selections if supervised_cutoff_selections is None else supervised_cutoff_selections,
        ),
        external,
        previous.revision + 1,
        previous.plan_sha256,
        workspace_rebinding,
        previous.prerequisites,
    )
    if (
        candidate.nodes,
        candidate.edges,
        candidate.external_inputs,
        candidate.deferred_nodes,
        candidate.supervised_cutoff_selections,
    ) == (
        previous.nodes,
        previous.edges,
        previous.external_inputs,
        previous.deferred_nodes,
        previous.supervised_cutoff_selections,
    ):
        return previous
    return candidate




def plan_with_memory_limit(previous: BatchPlan, manifest: ValidatedBatchManifest) -> BatchPlan:
    """Reconstruct the expected graph for a recorded memory-only revision.

    Used only to validate saved history; new work uses live resource observations.
    """
    if previous.batch_id != manifest.manifest.batch_id:
        raise BatchPlanError("Memory-limit change belongs to another analysis")
    value = previous._unsigned()
    value.update(normalized_manifest_sha256=manifest.normalized_manifest_sha256,
                 revision=previous.revision + 1, predecessor_plan_sha256=previous.plan_sha256)
    return replace(previous, normalized_manifest_sha256=manifest.normalized_manifest_sha256,
        revision=previous.revision + 1, predecessor_plan_sha256=previous.plan_sha256,
        plan_sha256=_digest(value))



def dependent_node_ids(
    dependencies: Iterable[tuple[str, Iterable[str]]], roots: Iterable[str],
) -> set[str]:
    """Return the changed tasks and every task that depends on them.

    We index each prerequisite's direct consumers, then visit those consumers
    once. This follows pooled tasks across samples without repeated scans of
    the whole plan. Roots stay in the result even when a revision has removed
    them from the supplied graph. Callers validate the plan and decide which
    roots changed; this calculation does not read results or update state.
    """
    consumers: dict[str, list[str]] = {}
    for node_id, requirements in dependencies:
        for requirement in requirements:
            consumers.setdefault(requirement, []).append(node_id)
    affected = set(roots)
    pending = list(affected)
    while pending:
        for node_id in consumers.get(pending.pop(), ()):
            if node_id not in affected:
                affected.add(node_id)
                pending.append(node_id)
    return affected


def ready_node_ids(
    plan: BatchPlan,
    statuses: Mapping[str, StageStatus],
    pending_authorization_sha256_by_node: Mapping[str, str],
    available_artifacts: Mapping[str, BatchArtifactRef],
    available_resources: Mapping[ResourceKind, int],
) -> tuple[str, ...]:
    """Calculate readiness from exact current status/artifact/resource evidence."""
    plan = _validate_plan_instance(plan, "plan")
    if not all(
        isinstance(key, str) and isinstance(value, StageStatus) for key, value in statuses.items()
    ):
        raise BatchPlanError("statuses must be node-keyed StageStatus values")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in pending_authorization_sha256_by_node.items()
    ):
        raise BatchPlanError("pending authorizations must be node-keyed digests")
    if not all(
        isinstance(key, str) and isinstance(value, BatchArtifactRef)
        for key, value in available_artifacts.items()
    ):
        raise BatchPlanError("available artifacts must be typed")
    if not all(
        isinstance(key, ResourceKind) and type(value) is int and value >= 0
        for key, value in available_resources.items()
    ):
        raise BatchPlanError("available resources must use closed nonnegative capacities")
    edges: dict[str, list[BatchArtifactEdge]] = {}
    for edge in plan.edges:
        edges.setdefault(edge.consumer_node_id, []).append(edge)
    ready: list[str] = []
    admitted_by_resource: dict[ResourceKind, int] = {resource: 0 for resource in ResourceKind}
    for node in plan.nodes:
        status = statuses.get(node.node_id)
        if status in {StageStatus.COMPLETED, StageStatus.FAILED}:
            continue
        if status is StageStatus.NEEDS_INTERACTION:
            digest = pending_authorization_sha256_by_node.get(node.node_id)
            if digest is None or _SHA_RE.fullmatch(digest) is None:
                continue
        if any(statuses.get(item) is not StageStatus.COMPLETED for item in node.dependency_ids):
            continue
        if admitted_by_resource[node.resource] >= available_resources.get(node.resource, 0):
            continue
        valid = True
        for edge in edges.get(node.node_id, ()):
            artifact = available_artifacts.get(edge.artifact_key)
            expected_origin = (
                ArtifactOrigin.MANIFEST_EXTERNAL
                if edge.producer_node_id is None
                else ArtifactOrigin.INTERNAL_NODE
            )
            if (
                artifact is None
                or artifact.artifact_id != edge.artifact_id
                or artifact.origin is not expected_origin
                or artifact.producer_node_id != edge.producer_node_id
                or (
                    edge.accepted_artifact_ref_sha256 is not None
                    and artifact.artifact_ref_sha256 != edge.accepted_artifact_ref_sha256
                )
            ):
                valid = False
                break
            try:
                validate_artifact_edge(artifact, edge.requirement)
            except BatchArtifactError:
                valid = False
                break
        if valid:
            ready.append(node.node_id)
            admitted_by_resource[node.resource] += 1
    return tuple(sorted(ready))


__all__ = [
    "BatchArtifactEdge",
    "BatchNode",
    "BatchPlan",
    "BatchPlanError",
    "ClusteringGraphScope",
    "DeferredNodeDescriptor",
    "NodeScope",
    "PhysicalSectionTarget",
    "ResourceKind",
    "SINGLE_SAMPLE_STAGE_ADAPTATION",
    "expand_batch_plan",
    "dependent_node_ids",
    "plan_batch",
    "ready_node_ids",
]
