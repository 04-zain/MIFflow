"""Immutable input, request, result, and entry-projection records.

The `UnsupervisedPreparationInput` record carries `sample_id`, `subject_id`,
`project_entry_id`, `slide_id`, `section_id` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.cell_analysis.phenotyping_population import validate_population_settings
from mif_pipeline.cell_analysis.clustering import ClusteringEvidence, FeatureSpec
from mif_pipeline.pipeline_core.config import ClusteringMode

from ._common import (
    DEFAULT_MAXIMUM_INPUT_BYTES,
    INPUT_SCHEMA,
    RESULT_SCHEMA,
    BatchPhenotypingError,
    canonical_ids,
    compact_cell_inventory_sha256,
    closed_mapping,
    digest,
    exact_tuple,
    feature_from_mapping,
    feature_mapping,
    feature_schema_sha256,
    integer,
    mapping,
    number,
    optional_text,
    sequence,
    sha256,
    text,
    validated_features,
)

INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "subject_id",
        "project_entry_id",
        "slide_id",
        "section_id",
        "image_role",
        "phenotyping_group_id",
        "resolved_config_sha256",
        "panel_sha256",
        "validation_domain_ids",
        "analysis_domain_ids",
        "segmentation_generation_id",
        "measurement_generation_id",
        "measurement_plan_sha256",
        "measurement_result_ref_sha256",
        "measurement_rows_reference_sha256",
        "measurement_rows_sha256",
        "measurement_rows_byte_count",
        "measurement_matrix_bytes",
        "row_inventory_sha256",
        "cell_count",
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
        "domain_cell_commitments_sha256",
        "measurement_saved_state_after_sha256",
        "measurement_execution_exchange_sha256",
        "measurement_feature_schema_sha256",
        "population_selection",
        "clustering_mode",
        "k",
        "resolution",
        "seed",
        "feature_specs",
    }
)


# ==============================================================================
#                                    Records
# ==============================================================================


@dataclass(frozen=True, slots=True)
class UnsupervisedPreparationInput:
    """Compact authenticated reference to one complete section-local row table."""

    sample_id: str
    subject_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    image_role: str
    phenotyping_group_id: str | None
    resolved_config_sha256: str
    panel_sha256: str
    validation_domain_ids: tuple[str, ...]
    analysis_domain_ids: tuple[str, ...]
    segmentation_generation_id: str
    measurement_generation_id: str
    measurement_plan_sha256: str
    measurement_result_ref_sha256: str
    measurement_rows_reference_sha256: str
    measurement_rows_sha256: str
    measurement_rows_byte_count: int
    measurement_matrix_bytes: int
    row_inventory_sha256: str
    cell_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    domain_cell_commitments_sha256: str
    measurement_saved_state_after_sha256: str
    measurement_execution_exchange_sha256: str
    measurement_feature_schema_sha256: str
    population_selection: Mapping[str, object]
    clustering_mode: ClusteringMode
    k: int
    resolution: float
    seed: int
    feature_specs: tuple[FeatureSpec, ...]
    method: str = 'snn_leiden'
    method_settings: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the UnsupervisedPreparationInput fields when the record is constructed.

        The checks cover ``image_role``, ``k``, ``phenotyping_group_id``,
        ``validation_domain_ids``, ``analysis_domain_ids``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings
        object.__setattr__(self, 'method_settings', validate_method_settings(self.method, self.method_settings or {}))
        for name in (
            "sample_id",
            "subject_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "segmentation_generation_id",
            "measurement_generation_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        if self.image_role != "primary_mif":
            raise BatchPhenotypingError(
                "unsupervised phenotype preparation requires a primary_mif image"
            )
        if self.phenotyping_group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        for name in (
            "resolved_config_sha256",
            "panel_sha256",
            "measurement_plan_sha256",
            "measurement_result_ref_sha256",
            "measurement_rows_reference_sha256",
            "measurement_rows_sha256",
            "row_inventory_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "domain_cell_commitments_sha256",
            "measurement_saved_state_after_sha256",
            "measurement_execution_exchange_sha256",
            "measurement_feature_schema_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "validation_domain_ids",
            canonical_ids(self.validation_domain_ids, "validation_domain_ids"),
        )
        object.__setattr__(
            self,
            "analysis_domain_ids",
            canonical_ids(self.analysis_domain_ids, "analysis_domain_ids"),
        )
        try:
            mode = ClusteringMode(self.clustering_mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("clustering_mode is invalid") from exc
        population = validate_population_settings({"default": self.population_selection, "slide_overrides": {}})["default"]
        object.__setattr__(self, "population_selection", MappingProxyType({"kind": population["kind"], "class_ids": tuple(population["class_ids"])}))
        k = integer(self.k, "k", positive=True)
        if k > 64:
            raise BatchPhenotypingError("k must not exceed 64")
        if type(self.seed) is not int:
            raise BatchPhenotypingError("seed must be an integer")
        object.__setattr__(
            self,
            "measurement_rows_byte_count",
            integer(self.measurement_rows_byte_count, "measurement_rows_byte_count", positive=True),
        )
        object.__setattr__(
            self, "cell_count", integer(self.cell_count, "cell_count", positive=True)
        )
        object.__setattr__(self, "measurement_matrix_bytes", integer(self.measurement_matrix_bytes, "measurement_matrix_bytes", positive=True))
        object.__setattr__(self, "clustering_mode", mode)
        object.__setattr__(self, "k", k)
        object.__setattr__(self, "resolution", number(self.resolution, "resolution", positive=True))
        object.__setattr__(self, "feature_specs", validated_features(self.feature_specs))
        if self.measurement_matrix_bytes % 8 or self.measurement_matrix_bytes < self.cell_count * len(self.feature_specs) * 8:
            raise BatchPhenotypingError("measurement matrix allowance must cover the selected Float64 features")

    @property
    def feature_schema_sha256(self) -> str:
        return feature_schema_sha256(self.feature_specs)

    @property
    def cell_inventory_sha256(self) -> str:
        return digest({"population_selection": {"kind": self.population_selection["kind"], "class_ids": list(self.population_selection["class_ids"])},
                       "measurement_inventory_sha256": compact_cell_inventory_sha256(
            sample_id=self.sample_id,
            project_entry_id=self.project_entry_id,
            section_id=self.section_id,
            measurement_rows_reference_sha256=self.measurement_rows_reference_sha256,
            row_inventory_sha256=self.row_inventory_sha256,
            cell_count=self.cell_count,
            cell_ids_sha256=self.cell_ids_sha256,
            cell_to_domain_membership_sha256=self.cell_to_domain_membership_sha256,
            domain_cell_commitments_sha256=self.domain_cell_commitments_sha256,
            feature_schema_sha256_value=self.feature_schema_sha256,
        )})

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": INPUT_SCHEMA,
            "sample_id": self.sample_id,
            "subject_id": self.subject_id,
            "project_entry_id": self.project_entry_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "image_role": self.image_role,
            "phenotyping_group_id": self.phenotyping_group_id,
            "resolved_config_sha256": self.resolved_config_sha256,
            "panel_sha256": self.panel_sha256,
            "validation_domain_ids": list(self.validation_domain_ids),
            "analysis_domain_ids": list(self.analysis_domain_ids),
            "segmentation_generation_id": self.segmentation_generation_id,
            "measurement_generation_id": self.measurement_generation_id,
            "measurement_plan_sha256": self.measurement_plan_sha256,
            "measurement_result_ref_sha256": self.measurement_result_ref_sha256,
            "measurement_rows_reference_sha256": self.measurement_rows_reference_sha256,
            "measurement_rows_sha256": self.measurement_rows_sha256,
            "measurement_rows_byte_count": self.measurement_rows_byte_count,
            "measurement_matrix_bytes": self.measurement_matrix_bytes,
            "row_inventory_sha256": self.row_inventory_sha256,
            "cell_count": self.cell_count,
            "cell_ids_sha256": self.cell_ids_sha256,
            "cell_to_domain_membership_sha256": self.cell_to_domain_membership_sha256,
            "domain_cell_commitments_sha256": self.domain_cell_commitments_sha256,
            "measurement_saved_state_after_sha256": self.measurement_saved_state_after_sha256,
            "measurement_execution_exchange_sha256": self.measurement_execution_exchange_sha256,
            "measurement_feature_schema_sha256": self.measurement_feature_schema_sha256,
            "population_selection": {"kind": self.population_selection["kind"], "class_ids": list(self.population_selection["class_ids"])},
            "clustering_mode": self.clustering_mode.value,
            **({'method': self.method, 'method_settings': dict(self.method_settings)} if self.method != 'snn_leiden' else {}),
            "k": self.k,
            "resolution": self.resolution,
            "seed": self.seed,
            "feature_specs": [feature_mapping(item) for item in self.feature_specs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "UnsupervisedPreparationInput":
        method_fields = frozenset({'method', 'method_settings'}) if isinstance(value, Mapping) and 'method' in value else frozenset()
        value = closed_mapping(value, INPUT_FIELDS | method_fields, "unsupervised preparation")
        if value["schema_version"] != INPUT_SCHEMA:
            raise BatchPhenotypingError("unsupervised preparation schema is unsupported")
        try:
            mode = ClusteringMode(value["clustering_mode"])
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("unsupervised preparation content is invalid") from exc
        values = {
            name: (
                tuple(text(item, name) for item in sequence(value[name], name))
                if name in {"validation_domain_ids", "analysis_domain_ids"}
                else value[name]
            )
            for name in INPUT_FIELDS
            if name not in {"schema_version", "feature_specs", "clustering_mode"}
        }
        return cls(
            **values,
            method=value.get('method', 'snn_leiden'),
            method_settings=value.get('method_settings', {}),
            clustering_mode=mode,
            feature_specs=tuple(
                feature_from_mapping(item)
                for item in sequence(value["feature_specs"], "feature_specs")
            ),
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchClusteringRequest:
    """Exact graph scope and explicit resource envelope from the active plan."""

    active_plan_sha256: str
    mode: ClusteringMode
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    phenotyping_group_id: str | None
    feature_specs: tuple[FeatureSpec, ...]
    k: int
    resolution: float
    seed: int
    maximum_cell_count: int
    maximum_input_bytes: int = DEFAULT_MAXIMUM_INPUT_BYTES
    method: str = 'snn_leiden'
    method_settings: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the BatchClusteringRequest fields when the record is constructed.

        The checks cover ``participant_ids``, ``project_entry_ids``,
        ``phenotyping_group_id``, ``k``, ``active_plan_sha256``, ``mode``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings
        object.__setattr__(self, 'method_settings', validate_method_settings(self.method, self.method_settings or {}))
        object.__setattr__(self, "active_plan_sha256", sha256(self.active_plan_sha256, "active_plan_sha256"))
        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("mode is invalid") from exc
        participants = canonical_ids(self.participant_ids, "participant_ids")
        entries = canonical_ids(self.project_entry_ids, "project_entry_ids")
        group_id = optional_text(self.phenotyping_group_id, "phenotyping_group_id")
        if mode is ClusteringMode.PER_SLIDE and (len(participants) != 1 or len(entries) != 1):
            raise BatchPhenotypingError("per_slide requires one participant and one entry")
        if group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        k = integer(self.k, "k", positive=True)
        if k > 64:
            raise BatchPhenotypingError("k must not exceed 64")
        if type(self.seed) is not int:
            raise BatchPhenotypingError("seed must be an integer")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "feature_specs", validated_features(self.feature_specs))
        object.__setattr__(self, "k", k)
        object.__setattr__(self, "resolution", number(self.resolution, "resolution", positive=True))
        object.__setattr__(
            self,
            "maximum_cell_count",
            integer(self.maximum_cell_count, "maximum_cell_count", positive=True),
        )
        object.__setattr__(
            self,
            "maximum_input_bytes",
            integer(self.maximum_input_bytes, "maximum_input_bytes", positive=True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "active_plan_sha256": self.active_plan_sha256,
            "mode": self.mode.value,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "phenotyping_group_id": self.phenotyping_group_id,
            "feature_specs": [feature_mapping(item) for item in self.feature_specs],
            "k": self.k,
            "resolution": self.resolution,
            "seed": self.seed,
            "maximum_cell_count": self.maximum_cell_count,
            "maximum_input_bytes": self.maximum_input_bytes,
            **({'method': self.method, 'method_settings': dict(self.method_settings)} if self.method != 'snn_leiden' else {}),
        }

    @property
    def request_sha256(self) -> str:
        return digest(self.to_mapping())


@dataclass(frozen=True, order=True, slots=True)
class BatchClusterAssignment:
    sample_id: str
    project_entry_id: str
    cell_id: str
    cluster_id: str
    slide_id: str
    section_id: str
    analysis_domain_id: str
    parent_cluster_id: str | None = None
    original_cluster_id: str | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            validator = optional_text if name in {'parent_cluster_id','original_cluster_id'} else text
            object.__setattr__(self, name, validator(getattr(self, name), name))
        if (self.parent_cluster_id is None) != (self.original_cluster_id is None):
            raise BatchPhenotypingError('Refined assignments require both parent and original cluster IDs.')

    def to_mapping(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if getattr(self,name) is not None}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterAssignment":
        optional = {'parent_cluster_id','original_cluster_id'}
        supplied = set(value) if isinstance(value,Mapping) else set()
        value = closed_mapping(value, (frozenset(cls.__dataclass_fields__)-optional)|(supplied&optional), "cluster assignment")
        return cls(**{name: text(raw,name) for name,raw in value.items()})


@dataclass(frozen=True, order=True, slots=True)
class BatchClusteringEntryComposition:
    sample_id: str
    subject_id: str
    project_entry_id: str
    slide_id: str
    phenotyping_group_id: str | None
    resolved_config_sha256: str
    panel_sha256: str
    feature_schema_sha256: str
    section_ids: tuple[str, ...]
    validation_domain_ids: tuple[str, ...]
    analysis_domain_ids: tuple[str, ...]
    segmentation_generation_ids: tuple[str, ...]
    measurement_generation_ids: tuple[str, ...]
    measurement_plan_sha256s: tuple[str, ...]
    preparation_ref_sha256s: tuple[str, ...]
    measurement_ref_sha256s: tuple[str, ...]
    cell_count: int
    cell_inventory_sha256: str

    def __post_init__(self) -> None:
        for name in ("sample_id", "subject_id", "project_entry_id", "slide_id"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        if self.phenotyping_group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        for name in (
            "resolved_config_sha256",
            "panel_sha256",
            "feature_schema_sha256",
            "cell_inventory_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        for name in (
            "section_ids",
            "validation_domain_ids",
            "analysis_domain_ids",
            "segmentation_generation_ids",
            "measurement_generation_ids",
            "measurement_plan_sha256s",
            "preparation_ref_sha256s",
            "measurement_ref_sha256s",
        ):
            object.__setattr__(
                self,
                name,
                canonical_ids(getattr(self, name), name),
            )
        for name in (
            "measurement_plan_sha256s",
            "preparation_ref_sha256s",
            "measurement_ref_sha256s",
        ):
            for value in getattr(self, name):
                sha256(value, name)
        object.__setattr__(self, "cell_count", integer(self.cell_count, "cell_count"))

    def to_mapping(self) -> dict[str, object]:
        return {
            name: list(value) if isinstance(value, tuple) else value
            for name in self.__dataclass_fields__
            for value in (getattr(self, name),)
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusteringEntryComposition":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "entry composition")
        tuple_fields = {
            "section_ids",
            "validation_domain_ids",
            "analysis_domain_ids",
            "segmentation_generation_ids",
            "measurement_generation_ids",
            "measurement_plan_sha256s",
            "preparation_ref_sha256s",
            "measurement_ref_sha256s",
        }
        values: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            raw = value[name]
            if name in tuple_fields:
                values[name] = tuple(text(item, name) for item in sequence(raw, name))
            elif name == "phenotyping_group_id":
                values[name] = optional_text(raw, name)
            elif name == "cell_count":
                values[name] = integer(raw, name)
            else:
                values[name] = text(raw, name)
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, order=True, slots=True)
class BatchClusteringLabelProjection:
    sample_id: str
    project_entry_id: str
    assignments: tuple[BatchClusterAssignment, ...]
    source_cell_inventory_sha256: str
    assignment_inventory_sha256: str
    clustering_generation_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", text(self.sample_id, "sample_id"))
        object.__setattr__(
            self, "project_entry_id", text(self.project_entry_id, "project_entry_id")
        )
        assignments = exact_tuple(self.assignments, "assignments")
        if not all(type(item) is BatchClusterAssignment for item in assignments):
            raise BatchPhenotypingError("label assignments must be exact composite records")
        normalized: tuple[BatchClusterAssignment, ...] = assignments  # type: ignore[assignment]
        if (
            len({(item.sample_id, item.project_entry_id, item.cell_id) for item in normalized})
            != len(normalized)
            or normalized != tuple(sorted(normalized))
            or any(
                item.sample_id != self.sample_id or item.project_entry_id != self.project_entry_id
                for item in normalized
            )
        ):
            raise BatchPhenotypingError("label assignments must be sorted and unique")
        object.__setattr__(self, "assignments", normalized)
        for name in (
            "source_cell_inventory_sha256",
            "assignment_inventory_sha256",
            "clustering_generation_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        if self.assignment_inventory_sha256 != digest([item.to_mapping() for item in normalized]):
            raise BatchPhenotypingError("label assignment inventory digest is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "project_entry_id": self.project_entry_id,
            "assignments": [item.to_mapping() for item in self.assignments],
            "source_cell_inventory_sha256": self.source_cell_inventory_sha256,
            "assignment_inventory_sha256": self.assignment_inventory_sha256,
            "clustering_generation_sha256": self.clustering_generation_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusteringLabelProjection":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "label projection")
        assignments = tuple(
            BatchClusterAssignment.from_mapping(item)
            for item in sequence(value["assignments"], "assignments")
        )
        return cls(
            sample_id=value["sample_id"],
            project_entry_id=value["project_entry_id"],
            assignments=assignments,
            source_cell_inventory_sha256=value["source_cell_inventory_sha256"],
            assignment_inventory_sha256=value["assignment_inventory_sha256"],
            clustering_generation_sha256=value["clustering_generation_sha256"],
        )  # type: ignore[arg-type]


def evidence_mapping(value: ClusteringEvidence) -> dict[str, object]:
    return {
        item.name: list(raw) if isinstance(raw, tuple) else raw
        for item in fields(ClusteringEvidence)
        for raw in (getattr(value, item.name),)
        if item.name not in {'method_settings', 'model_codebook', 'model_clusters', 'model_sha256', 'model_group_labels'} or raw
    }


def validate_evidence(value: object) -> ClusteringEvidence:
    """Validate the full scientific/runtime evidence record, not only its digests.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(value) is not ClusteringEvidence:
        raise BatchPhenotypingError("clustering evidence must be an exact owner record")
    expected = {
        "graph_algorithm": "exact_snn_jaccard_leiden/v1",
        "neighbor_mode": "exact",
        "distance_metric": "euclidean",
        "symmetrization": "directed_weight_average",
        "affinity": "jaccard_directed_neighbor_sets",
        "zero_edge_policy": "remove",
        "pruning_policy": "none",
    }
    if value.graph_algorithm not in {'exact_snn_jaccard_leiden/v1', 'phenograph_leiden/v1', 'flowsom/v1'}:
        raise BatchPhenotypingError('The saved clustering method is unsupported.')
    graph_method = value.graph_algorithm != 'flowsom/v1'
    if value.model_group_labels and graph_method:
        raise BatchPhenotypingError('Only a FlowSOM result can link learned profiles to cell groups.')
    if graph_method and any(getattr(value, name) != required for name, required in expected.items() if name != 'graph_algorithm'):
        raise BatchPhenotypingError("clustering evidence algorithm contract differs")
    for name in ("backend", "partitioner_backend", "partition_quality_function"):
        text(getattr(value, name), name)
    nonnegative = (
        "pairwise_python_calls",
        "tree_query_calls",
        "requested_k",
        "effective_k",
        "node_count",
        "edge_count",
        "component_count",
        "isolate_count",
        "retained_candidate_slots",
        "maximum_candidate_working_set",
        "exact_distance_evaluations",
        "fallback_group_count",
    )
    if any(
        type(getattr(value, name)) is not int or getattr(value, name) < 0 for name in nonnegative
    ):
        raise BatchPhenotypingError("clustering evidence counters are invalid")
    if (
        type(value.seed) is not int
        or isinstance(value.resolution, bool)
        or not isinstance(value.resolution, (int, float))
        or not isfinite(float(value.resolution))
        or value.resolution <= 0
        or (graph_method and value.requested_k < 1)
    ):
        raise BatchPhenotypingError("clustering evidence configuration is invalid")
    if (
        value.effective_k > value.requested_k
        or value.effective_k > max(0, value.node_count - 1)
        or value.component_count > value.node_count
        or value.isolate_count > value.node_count
        or value.edge_count > value.node_count * max(0, value.node_count - 1) // 2
    ):
        raise BatchPhenotypingError("clustering evidence graph counts are inconsistent")
    if any(
        type(items) is not tuple
        for items in (
            value.feature_measurements,
            value.canonical_biomarkers,
            value.robust_centers,
            value.robust_scales,
            value.feature_weights,
        )
    ):
        raise BatchPhenotypingError("clustering evidence feature vectors are invalid")
    feature_count = len(value.feature_measurements)
    if feature_count == 0 or any(
        len(items) != feature_count
        for items in (
            value.canonical_biomarkers,
            value.robust_centers,
            value.robust_scales,
            value.feature_weights,
        )
    ):
        raise BatchPhenotypingError("clustering evidence feature vectors differ")
    if any(type(item) is not str or not item for item in value.feature_measurements):
        raise BatchPhenotypingError("clustering evidence feature names are invalid")
    if any(
        item is not None and (type(item) is not str or not item)
        for item in value.canonical_biomarkers
    ):
        raise BatchPhenotypingError("clustering evidence biomarker identities are invalid")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not isfinite(float(item))
        for items in (value.robust_centers, value.robust_scales, value.feature_weights)
        for item in items
    ):
        raise BatchPhenotypingError("clustering evidence feature values are invalid")
    if any(float(item) <= 0 for item in (*value.robust_scales, *value.feature_weights)):
        raise BatchPhenotypingError("clustering evidence scales or weights are invalid")
    weights = (value.minimum_edge_weight, value.maximum_edge_weight, value.mean_edge_weight)
    if value.edge_count == 0:
        if any(item is not None for item in weights):
            raise BatchPhenotypingError("edgeless clustering evidence has edge weights")
    elif any(
        item is None
        or isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not isfinite(float(item))
        or not 0 < float(item) <= 1
        for item in weights
    ):
        raise BatchPhenotypingError("clustering evidence edge weights are invalid")
    if value.edge_count and not (
        value.minimum_edge_weight <= value.mean_edge_weight <= value.maximum_edge_weight  # type: ignore[operator]
    ):
        raise BatchPhenotypingError("clustering evidence edge-weight summary differs")
    for name in ("feature_transform_sha256", "node_identity_sha256", *(('graph_sha256',) if graph_method else ('model_sha256',))):
        sha256(getattr(value, name), name)
    if not graph_method:
        from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings
        settings = validate_method_settings('flowsom', dict(value.method_settings))
        nodes = settings['grid_size']**2
        if (len(value.model_codebook) != nodes or len(value.model_clusters) != nodes
                or any(len(row) != feature_count or any(not isfinite(x) for x in row) for row in value.model_codebook)
                or any(type(x) is not int or not 0 <= x < settings['n_clusters'] for x in value.model_clusters)
                or value.requested_k or value.effective_k or value.edge_count):
            raise BatchPhenotypingError('The saved FlowSOM map or metacluster assignments are incomplete.')
        mapping = value.model_group_labels
        if mapping:
            if (type(mapping) is not tuple
                    or any(type(row) is not tuple or len(row) != 2
                           or type(row[0]) is not int or type(row[1]) is not str for row in mapping)):
                raise BatchPhenotypingError('The FlowSOM cell-group mapping is invalid.')
            groups, labels = zip(*mapping)
            if (tuple(sorted(set(groups))) != groups or not set(groups) <= set(value.model_clusters)
                    or set(labels) != {f'Cluster_{i:03d}' for i in range(1, len(mapping) + 1)}):
                raise BatchPhenotypingError('The FlowSOM cell-group mapping is inconsistent.')
    return value


@dataclass(frozen=True, slots=True)
class BatchClusterRefinement:
    """Keep a subset fit distinct from the retained whole-population model."""
    parent_terminal_record_sha256: str
    parent_result_ref_sha256: str
    parent_result_sha256: str
    selected_cluster_ids: tuple[str, ...]
    evidence: ClusteringEvidence
    child_labels: Mapping[str,str]
    runtime_versions: Mapping[str,str]

    def __post_init__(self):
        for name in ('parent_terminal_record_sha256','parent_result_ref_sha256','parent_result_sha256'):
            object.__setattr__(self,name,sha256(getattr(self,name),name))
        selected=canonical_ids(self.selected_cluster_ids,'selected_cluster_ids')
        evidence=validate_evidence(self.evidence)
        if evidence.node_count<2 or evidence.graph_algorithm not in {'phenograph_leiden/v1','flowsom/v1'}:
            raise BatchPhenotypingError('A cluster refit requires at least two selected cells and a supported method.')
        labels=dict(mapping(self.child_labels,'child_labels'))
        if (not labels or set(labels)!={f'Cluster_{i:03d}' for i in range(1,len(labels)+1)}
                or len(set(labels.values()))!=len(labels)
                or any(type(value) is not str or not value.strip() for value in labels.values())):
            raise BatchPhenotypingError('Refit child labels must map distinct local groups to current cluster IDs.')
        if evidence.model_group_labels and {label for _,label in evidence.model_group_labels}!=set(labels):
            raise BatchPhenotypingError('The refit learned groups differ from its child labels.')
        runtime=dict(mapping(self.runtime_versions,'runtime_versions'))
        if not runtime or any(type(key) is not str or type(value) is not str or not value for key,value in runtime.items()):
            raise BatchPhenotypingError('Refit runtime versions are invalid.')
        object.__setattr__(self,'selected_cluster_ids',selected)
        object.__setattr__(self,'child_labels',MappingProxyType(dict(sorted(labels.items()))))
        object.__setattr__(self,'runtime_versions',MappingProxyType(dict(sorted(runtime.items()))))

    def to_mapping(self):
        return dict(parent_terminal_record_sha256=self.parent_terminal_record_sha256,
            parent_result_ref_sha256=self.parent_result_ref_sha256,parent_result_sha256=self.parent_result_sha256,
            selected_cluster_ids=list(self.selected_cluster_ids),evidence=evidence_mapping(self.evidence),
            child_labels=dict(self.child_labels),runtime_versions=dict(self.runtime_versions))

    @classmethod
    def from_mapping(cls,value):
        from .codec import _evidence_from_mapping
        value=closed_mapping(value,frozenset(cls.__dataclass_fields__),'cluster refinement')
        return cls(**{**value,'selected_cluster_ids':tuple(sequence(value['selected_cluster_ids'],'selected_cluster_ids')),
                      'evidence':_evidence_from_mapping(value['evidence'])})


def generation_sha256(
    *,
    request_sha256: str,
    source_authority_sha256s: tuple[str, ...],
    entry_composition: tuple[BatchClusteringEntryComposition, ...],
    assignments: tuple[BatchClusterAssignment, ...],
    evidence: ClusteringEvidence,
    runtime_versions: Mapping[str, str],
    refinement: BatchClusterRefinement | None = None,
) -> str:
    return digest(
        {
            "request_sha256": request_sha256,
            "source_authority_sha256s": list(source_authority_sha256s),
            "entry_composition": [item.to_mapping() for item in entry_composition],
            "assignments": [item.to_mapping() for item in assignments],
            "evidence": evidence_mapping(evidence),
            "runtime_versions": dict(runtime_versions),
            **({'refinement':refinement.to_mapping()} if refinement is not None else {}),
        }
    )


def label_projections(
    assignments: tuple[BatchClusterAssignment, ...],
    composition: tuple[BatchClusteringEntryComposition, ...],
    generation: str,
) -> tuple[BatchClusteringLabelProjection, ...]:
    return tuple(
        BatchClusteringLabelProjection(
            sample_id=item.sample_id,
            project_entry_id=item.project_entry_id,
            assignments=tuple(
                assignment
                for assignment in assignments
                if assignment.project_entry_id == item.project_entry_id
            ),
            source_cell_inventory_sha256=item.cell_inventory_sha256,
            assignment_inventory_sha256=digest(
                [
                    assignment.to_mapping()
                    for assignment in assignments
                    if assignment.project_entry_id == item.project_entry_id
                ]
            ),
            clustering_generation_sha256=generation,
        )
        for item in composition
    )


@dataclass(frozen=True, slots=True)
class BatchClusteringExecutionResult:
    """Neutral graph result; expert-label acceptance is a later adapter boundary."""

    mode: ClusteringMode
    phenotyping_group_id: str | None
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    request_sha256: str
    source_authority_sha256s: tuple[str, ...]
    source_state_head_sha256: str
    active_plan_sha256: str
    entry_composition: tuple[BatchClusteringEntryComposition, ...]
    assignments: tuple[BatchClusterAssignment, ...]
    entry_projections: tuple[BatchClusteringLabelProjection, ...]
    label_review_status: str
    input_population_sha256: str
    output_assignment_sha256: str
    evidence: ClusteringEvidence
    runtime_versions: Mapping[str, str]
    clustering_generation_sha256: str
    refinement: BatchClusterRefinement | None = None

    def __post_init__(self) -> None:
        """Validate the BatchClusteringExecutionResult fields when the record is constructed.

        The checks cover ``participant_ids``, ``project_entry_ids``,
        ``phenotyping_group_id``, ``label_review_status``, ``source_authority_sha256s``,
        ``entry_composition``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("clustering result mode is invalid") from exc
        participants = canonical_ids(self.participant_ids, "participant_ids")
        entries = canonical_ids(self.project_entry_ids, "project_entry_ids")
        group_id = optional_text(self.phenotyping_group_id, "phenotyping_group_id")
        if mode is ClusteringMode.PER_SLIDE and (len(participants) != 1 or len(entries) != 1):
            raise BatchPhenotypingError("per_slide result scope is invalid")
        if group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        if self.label_review_status != "pending_optional_expert_review":
            raise BatchPhenotypingError("neutral clustering label review status is invalid")
        for name in (
            "request_sha256",
            "source_state_head_sha256",
            "active_plan_sha256",
            "input_population_sha256",
            "output_assignment_sha256",
            "clustering_generation_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        sources = canonical_ids(self.source_authority_sha256s, "source_authority_sha256s")
        for value in sources:
            sha256(value, "source_authority_sha256s")
        composition = exact_tuple(self.entry_composition, "entry_composition")
        assignments = exact_tuple(self.assignments, "assignments")
        projections = exact_tuple(self.entry_projections, "entry_projections")
        if (
            not all(type(item) is BatchClusteringEntryComposition for item in composition)
            or not all(type(item) is BatchClusterAssignment for item in assignments)
            or not all(type(item) is BatchClusteringLabelProjection for item in projections)
        ):
            raise BatchPhenotypingError("clustering result contains invalid records")
        typed_composition: tuple[BatchClusteringEntryComposition, ...] = composition  # type: ignore[assignment]
        typed_assignments: tuple[BatchClusterAssignment, ...] = assignments  # type: ignore[assignment]
        typed_projections: tuple[BatchClusteringLabelProjection, ...] = projections  # type: ignore[assignment]
        if (
            typed_composition
            != tuple(sorted(typed_composition, key=lambda item: item.project_entry_id))
            or tuple(item.project_entry_id for item in typed_composition) != entries
        ):
            raise BatchPhenotypingError("entry composition is not an exact cover")
        if tuple(sorted({item.sample_id for item in typed_composition})) != participants:
            raise BatchPhenotypingError("entry composition participants differ")
        if len(
            {(item.sample_id, item.project_entry_id, item.cell_id) for item in typed_assignments}
        ) != len(typed_assignments) or typed_assignments != tuple(sorted(typed_assignments)):
            raise BatchPhenotypingError("clustering assignments are not canonical and unique")
        if any(
            item.sample_id not in participants or item.project_entry_id not in entries
            for item in typed_assignments
        ) or sum(item.cell_count for item in typed_composition) != len(typed_assignments):
            raise BatchPhenotypingError("clustering assignments do not cover the scope")
        composition_by_entry = {item.project_entry_id: item for item in typed_composition}
        for entry_id, item in composition_by_entry.items():
            entry_assignments = tuple(
                assignment
                for assignment in typed_assignments
                if assignment.project_entry_id == entry_id
            )
            if len(entry_assignments) != item.cell_count or any(
                assignment.sample_id != item.sample_id
                or assignment.slide_id != item.slide_id
                or assignment.section_id not in item.section_ids
                or assignment.analysis_domain_id not in item.analysis_domain_ids
                for assignment in entry_assignments
            ):
                raise BatchPhenotypingError("clustering assignments differ from entry composition")
        evidence = validate_evidence(self.evidence)
        if evidence.node_count != len(typed_assignments):
            raise BatchPhenotypingError("clustering evidence population differs")
        if self.refinement is not None:
            refinement=self.refinement
            if type(refinement) is not BatchClusterRefinement:
                raise BatchPhenotypingError('Cluster refinement must be an exact owner record.')
            transform=('feature_measurements','canonical_biomarkers','robust_centers','robust_scales',
                       'feature_weights','feature_transform_sha256')
            if any(getattr(refinement.evidence,name)!=getattr(evidence,name) for name in transform):
                raise BatchPhenotypingError('Refit preprocessing differs from the retained parent transform.')
            children=set(refinement.child_labels.values())
            subset=tuple(row for row in typed_assignments if row.cluster_id in children)
            if (len(subset)!=refinement.evidence.node_count
                    or {row.parent_cluster_id for row in subset}!=set(refinement.selected_cluster_ids)
                    or {row.cluster_id for row in subset}!=children
                    or any(row.original_cluster_id is None for row in subset)):
                raise BatchPhenotypingError('Refit child membership differs from its subset evidence or parent lineage.')
        runtime = mapping(self.runtime_versions, "runtime_versions")
        if not {"python", "numpy", "scipy", "igraph", "leidenalg"} <= set(runtime) or any(
            type(key) is not str or type(value) is not str or not value
            for key, value in runtime.items()
        ):
            raise BatchPhenotypingError("clustering runtime versions are invalid")
        runtime = MappingProxyType(dict(sorted(runtime.items())))
        composition_sources = tuple(
            sorted(
                ref
                for item in typed_composition
                for ref in (*item.preparation_ref_sha256s, *item.measurement_ref_sha256s)
            )
        )
        input_digest = digest(
            [
                [item.sample_id, item.project_entry_id, item.cell_inventory_sha256]
                for item in typed_composition
            ]
        )
        output_digest = digest([item.to_mapping() for item in typed_assignments])
        generation = generation_sha256(
            request_sha256=self.request_sha256,
            source_authority_sha256s=sources,
            entry_composition=typed_composition,
            assignments=typed_assignments,
            evidence=self.evidence,
            runtime_versions=runtime,
            refinement=self.refinement,
        )
        if (
            composition_sources != sources
            or input_digest != self.input_population_sha256
            or output_digest != self.output_assignment_sha256
            or generation != self.clustering_generation_sha256
            or typed_projections
            != label_projections(typed_assignments, typed_composition, generation)
        ):
            raise BatchPhenotypingError("clustering result relationships differ")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "source_authority_sha256s", sources)
        object.__setattr__(self, "entry_composition", typed_composition)
        object.__setattr__(self, "assignments", typed_assignments)
        object.__setattr__(self, "entry_projections", typed_projections)
        object.__setattr__(self, "runtime_versions", runtime)

    def material_mapping(self) -> dict[str, object]:
        return {
            "schema_version": RESULT_SCHEMA,
            "mode": self.mode.value,
            "phenotyping_group_id": self.phenotyping_group_id,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "request_sha256": self.request_sha256,
            "source_authority_sha256s": list(self.source_authority_sha256s),
            "source_state_head_sha256": self.source_state_head_sha256,
            "active_plan_sha256": self.active_plan_sha256,
            "entry_composition": [item.to_mapping() for item in self.entry_composition],
            "assignments": [item.to_mapping() for item in self.assignments],
            "entry_projections": [item.to_mapping() for item in self.entry_projections],
            "label_review_status": self.label_review_status,
            "input_population_sha256": self.input_population_sha256,
            "output_assignment_sha256": self.output_assignment_sha256,
            "evidence": evidence_mapping(self.evidence),
            "runtime_versions": dict(self.runtime_versions),
            "clustering_generation_sha256": self.clustering_generation_sha256,
            **({'refinement':self.refinement.to_mapping()} if self.refinement is not None else {}),
        }

    @property
    def result_sha256(self) -> str:
        return digest(self.material_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {**self.material_mapping(), "result_sha256": self.result_sha256}


@dataclass(frozen=True, slots=True)
class PersistedBatchClusteringResult:
    locator: Path
    sha256: str
    byte_count: int
    result_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.locator, Path) or not self.locator.is_absolute():
            raise BatchPhenotypingError("persisted clustering locator is invalid")
        object.__setattr__(self, "sha256", sha256(self.sha256, "sha256"))
        object.__setattr__(self, "byte_count", integer(self.byte_count, "byte_count"))
        object.__setattr__(self, "result_sha256", sha256(self.result_sha256, "result_sha256"))


@dataclass(frozen=True, slots=True)
class PersistedUnsupervisedPreparation:
    locator: Path
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.locator, Path) or not self.locator.is_absolute():
            raise BatchPhenotypingError("persisted preparation locator is invalid")
        object.__setattr__(self, "sha256", sha256(self.sha256, "sha256"))
        object.__setattr__(
            self, "byte_count", integer(self.byte_count, "byte_count", positive=True)
        )
