"""Immutable aggregate evidence for pooled unsupervised cluster review.

The `BatchClusterReviewEvidence` record carries `mode`, `phenotyping_group_id`,
`participant_ids`, `project_entry_ids`, `analysis_domain_bindings` and its remaining
declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType

from mif_pipeline.orchestration.batch_artifacts import AnalysisDomainBinding
from mif_pipeline.pipeline_core.config import ClusteringMode

from ._common import (
    BatchPhenotypingError,
    canonical_ids,
    closed_mapping,
    digest,
    exact_tuple,
    integer,
    number,
    optional_text,
    sequence,
    sha256,
    text,
)

REVIEW_EVIDENCE_SCHEMA = "mif-batch-cluster-review-evidence/v1"


# ==============================================================================
#                                 Review records
# ==============================================================================


def _binding_key(
    value: AnalysisDomainBinding,
) -> tuple[str, str, str, int, str]:
    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.project_entry_id,
    )


@dataclass(frozen=True, slots=True)
class BatchClusterReviewMarkerProfile:
    """One raw protein-marker profile; no fitted or transformed values."""

    feature_id: str
    measurement: str
    biomarker: str | None
    compartment: str | None
    units: str | None
    cell_count: int
    raw_mean: float
    raw_median: float

    def __post_init__(self) -> None:
        for name in ("feature_id", "measurement"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in ("biomarker", "compartment", "units"):
            object.__setattr__(self, name, optional_text(getattr(self, name), name))
        object.__setattr__(
            self, "cell_count", integer(self.cell_count, "cell_count", positive=True)
        )
        object.__setattr__(self, "raw_mean", number(self.raw_mean, "raw_mean"))
        object.__setattr__(self, "raw_median", number(self.raw_median, "raw_median"))

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterReviewMarkerProfile":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "cluster marker profile")
        return cls(  # type: ignore[arg-type]
            **{name: value[name] for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True, order=True, slots=True)
class BatchClusterReviewEntryCount:
    sample_id: str
    project_entry_id: str
    cell_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", text(self.sample_id, "sample_id"))
        object.__setattr__(
            self, "project_entry_id", text(self.project_entry_id, "project_entry_id")
        )
        object.__setattr__(self, "cell_count", integer(self.cell_count, "cell_count"))

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterReviewEntryCount":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "cluster entry count")
        return cls(  # type: ignore[arg-type]
            **{name: value[name] for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True, order=True, slots=True)
class BatchClusterReviewSectionCount:
    sample_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    cell_count: int

    def __post_init__(self) -> None:
        for name in ("sample_id", "project_entry_id", "slide_id", "section_id"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        object.__setattr__(self, "cell_count", integer(self.cell_count, "cell_count"))

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterReviewSectionCount":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "cluster section count")
        return cls(  # type: ignore[arg-type]
            **{name: value[name] for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True, slots=True)
class BatchClusterReviewSummary:
    cluster_id: str
    cell_count: int
    marker_profiles: tuple[BatchClusterReviewMarkerProfile, ...]
    entry_counts: tuple[BatchClusterReviewEntryCount, ...]
    section_counts: tuple[BatchClusterReviewSectionCount, ...]

    def __post_init__(self) -> None:
        """Validate the BatchClusterReviewSummary fields when the record is constructed.

        The checks cover ``marker_profiles``, ``entry_counts``, ``section_counts``,
        ``cluster_id``, ``cell_count``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        object.__setattr__(self, "cluster_id", text(self.cluster_id, "cluster_id"))
        object.__setattr__(
            self, "cell_count", integer(self.cell_count, "cell_count", positive=True)
        )
        profiles = exact_tuple(self.marker_profiles, "marker_profiles")
        entries = exact_tuple(self.entry_counts, "entry_counts")
        sections = exact_tuple(self.section_counts, "section_counts")
        if (
            not profiles
            or not all(type(item) is BatchClusterReviewMarkerProfile for item in profiles)
            or not all(type(item) is BatchClusterReviewEntryCount for item in entries)
            or not all(type(item) is BatchClusterReviewSectionCount for item in sections)
        ):
            raise BatchPhenotypingError("cluster review summary contains invalid records")
        typed_profiles: tuple[BatchClusterReviewMarkerProfile, ...] = (  # type: ignore[assignment]
            profiles
        )
        typed_entries: tuple[BatchClusterReviewEntryCount, ...] = (  # type: ignore[assignment]
            entries
        )
        typed_sections: tuple[BatchClusterReviewSectionCount, ...] = (  # type: ignore[assignment]
            sections
        )
        if (
            tuple(item.feature_id for item in typed_profiles)
            != tuple(sorted({item.feature_id for item in typed_profiles}))
            or typed_entries != tuple(sorted(typed_entries))
            or typed_sections != tuple(sorted(typed_sections))
            or len({(item.sample_id, item.project_entry_id) for item in typed_entries})
            != len(typed_entries)
            or len(
                {
                    (item.sample_id, item.project_entry_id, item.slide_id, item.section_id)
                    for item in typed_sections
                }
            )
            != len(typed_sections)
            or any(item.cell_count != self.cell_count for item in typed_profiles)
            or sum(item.cell_count for item in typed_entries) != self.cell_count
            or sum(item.cell_count for item in typed_sections) != self.cell_count
        ):
            raise BatchPhenotypingError("cluster review summary relationships differ")
        object.__setattr__(self, "marker_profiles", typed_profiles)
        object.__setattr__(self, "entry_counts", typed_entries)
        object.__setattr__(self, "section_counts", typed_sections)

    @property
    def feature_identity(
        self,
    ) -> tuple[tuple[str, str, str | None, str | None, str | None], ...]:
        return tuple(
            (
                item.feature_id,
                item.measurement,
                item.biomarker,
                item.compartment,
                item.units,
            )
            for item in self.marker_profiles
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "cell_count": self.cell_count,
            "marker_profiles": [item.to_mapping() for item in self.marker_profiles],
            "entry_counts": [item.to_mapping() for item in self.entry_counts],
            "section_counts": [item.to_mapping() for item in self.section_counts],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterReviewSummary":
        value = closed_mapping(value, frozenset(cls.__dataclass_fields__), "cluster review summary")
        return cls(
            cluster_id=value["cluster_id"],
            cell_count=value["cell_count"],
            marker_profiles=tuple(
                BatchClusterReviewMarkerProfile.from_mapping(item)
                for item in sequence(value["marker_profiles"], "marker_profiles")
            ),
            entry_counts=tuple(
                BatchClusterReviewEntryCount.from_mapping(item)
                for item in sequence(value["entry_counts"], "entry_counts")
            ),
            section_counts=tuple(
                BatchClusterReviewSectionCount.from_mapping(item)
                for item in sequence(value["section_counts"], "section_counts")
            ),
        )


_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "phenotyping_group_id",
        "participant_ids",
        "project_entry_ids",
        "analysis_domain_bindings",
        "active_plan_sha256",
        "source_state_head_sha256",
        "source_authority_sha256s",
        "clustering_result_ref_sha256",
        "clustering_result_sha256",
        "clustering_request_sha256",
        "clustering_generation_sha256",
        "input_population_sha256",
        "assignment_inventory_sha256",
        "feature_schema_sha256",
        "cluster_inventory_sha256",
        "cell_count",
        "cluster_count",
        "clusters",
        "evidence_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class BatchClusterReviewEvidence:
    """Profile-first aggregate review evidence without per-cell material."""

    mode: ClusteringMode
    phenotyping_group_id: str | None
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    active_plan_sha256: str
    source_state_head_sha256: str
    source_authority_sha256s: tuple[str, ...]
    clustering_result_ref_sha256: str
    clustering_result_sha256: str
    clustering_request_sha256: str
    clustering_generation_sha256: str
    input_population_sha256: str
    assignment_inventory_sha256: str
    feature_schema_sha256: str
    cluster_inventory_sha256: str
    cell_count: int
    cluster_count: int
    clusters: tuple[BatchClusterReviewSummary, ...]
    refit_summary: Mapping | None = None

    def __post_init__(self) -> None:
        """Validate the BatchClusterReviewEvidence fields when the record is constructed.

        The checks cover ``phenotyping_group_id``, ``participant_ids``,
        ``project_entry_ids``, ``source_authority_sha256s``, ``analysis_domain_bindings``,
        ``clusters``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("cluster review mode is invalid") from exc
        group_id = optional_text(self.phenotyping_group_id, "phenotyping_group_id")
        if group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        participants = canonical_ids(self.participant_ids, "participant_ids")
        entries = canonical_ids(self.project_entry_ids, "project_entry_ids")
        sources = canonical_ids(self.source_authority_sha256s, "source_authority_sha256s")
        for value in sources:
            sha256(value, "source_authority_sha256s")
        for name in (
            "active_plan_sha256",
            "source_state_head_sha256",
            "clustering_result_ref_sha256",
            "clustering_result_sha256",
            "clustering_request_sha256",
            "clustering_generation_sha256",
            "input_population_sha256",
            "assignment_inventory_sha256",
            "feature_schema_sha256",
            "cluster_inventory_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        bindings = exact_tuple(self.analysis_domain_bindings, "analysis_domain_bindings")
        clusters = exact_tuple(self.clusters, "clusters")
        binding_keys = tuple(
            _binding_key(item) for item in bindings if type(item) is AnalysisDomainBinding
        )
        if (
            not bindings
            or not all(type(item) is AnalysisDomainBinding for item in bindings)
            or binding_keys != tuple(sorted(binding_keys))
            or len(set(binding_keys)) != len(binding_keys)
            or not clusters
            or not all(type(item) is BatchClusterReviewSummary for item in clusters)
        ):
            raise BatchPhenotypingError("cluster review evidence records are invalid")
        typed_bindings: tuple[AnalysisDomainBinding, ...] = bindings  # type: ignore[assignment]
        typed_clusters: tuple[BatchClusterReviewSummary, ...] = clusters  # type: ignore[assignment]
        if (
            tuple(item.cluster_id for item in typed_clusters)
            != tuple(sorted({item.cluster_id for item in typed_clusters}))
            or tuple(sorted({item.sample_id for item in typed_bindings})) != participants
            or tuple(sorted({item.project_entry_id for item in typed_bindings})) != entries
            or mode is ClusteringMode.PER_SLIDE
            and (len(participants) != 1 or len(entries) != 1)
        ):
            raise BatchPhenotypingError("cluster review evidence scope is invalid")
        binding_scope: dict[str, tuple[str, str]] = {}
        for item in typed_bindings:
            scope = (item.sample_id, item.slide_id)
            if binding_scope.setdefault(item.project_entry_id, scope) != scope:
                raise BatchPhenotypingError("cluster review bindings have ambiguous entry scope")
        if len(binding_scope) != len(entries):
            raise BatchPhenotypingError("cluster review bindings have ambiguous entry scope")
        feature_identity = typed_clusters[0].feature_identity
        entry_totals: dict[tuple[str, str], int] = {}
        expected_entry_scope = {
            (sample_id, entry_id) for entry_id, (sample_id, _) in binding_scope.items()
        }
        expected_section_scope: set[tuple[str, str, str, str]] | None = None
        for cluster in typed_clusters:
            if cluster.feature_identity != feature_identity:
                raise BatchPhenotypingError("cluster review feature profiles differ")
            actual_entry_scope = {
                (item.sample_id, item.project_entry_id) for item in cluster.entry_counts
            }
            actual_section_scope = {
                (item.sample_id, item.project_entry_id, item.slide_id, item.section_id)
                for item in cluster.section_counts
            }
            if actual_entry_scope != expected_entry_scope:
                raise BatchPhenotypingError("cluster review entry counts are incomplete")
            if expected_section_scope is None:
                expected_section_scope = actual_section_scope
            elif actual_section_scope != expected_section_scope:
                raise BatchPhenotypingError("cluster review section counts are incomplete")
            for item in cluster.entry_counts:
                expected_sample_and_slide = binding_scope.get(item.project_entry_id)
                if (
                    expected_sample_and_slide is None
                    or expected_sample_and_slide[0] != item.sample_id
                ):
                    raise BatchPhenotypingError("cluster review entry count scope differs")
                key = (item.sample_id, item.project_entry_id)
                entry_totals[key] = entry_totals.get(key, 0) + item.cell_count
            for item in cluster.section_counts:
                if binding_scope.get(item.project_entry_id) != (
                    item.sample_id,
                    item.slide_id,
                ):
                    raise BatchPhenotypingError("cluster review section count scope differs")
            section_totals: dict[tuple[str, str], int] = {}
            for item in cluster.section_counts:
                key = (item.sample_id, item.project_entry_id)
                section_totals[key] = section_totals.get(key, 0) + item.cell_count
            if section_totals != {
                (item.sample_id, item.project_entry_id): item.cell_count
                for item in cluster.entry_counts
            }:
                raise BatchPhenotypingError("cluster review entry and section counts differ")
        if set(entry_totals) != expected_entry_scope:
            raise BatchPhenotypingError("cluster review counts do not cover every entry")
        cell_count = integer(self.cell_count, "cell_count", positive=True)
        cluster_count = integer(self.cluster_count, "cluster_count", positive=True)
        if (
            cell_count != sum(item.cell_count for item in typed_clusters)
            or cluster_count != len(typed_clusters)
            or self.cluster_inventory_sha256
            != digest([item.to_mapping() for item in typed_clusters])
        ):
            raise BatchPhenotypingError("cluster review evidence inventory differs")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "source_authority_sha256s", sources)
        object.__setattr__(self, "analysis_domain_bindings", typed_bindings)
        object.__setattr__(self, "clusters", typed_clusters)
        object.__setattr__(self, "cell_count", cell_count)
        object.__setattr__(self, "cluster_count", cluster_count)
        if self.refit_summary is not None:
            summary=dict(closed_mapping(self.refit_summary,frozenset({
                'parent_cluster_ids','child_cluster_ids','cell_count','method','parent_result_sha256'}),'refit summary'))
            for key in ('parent_cluster_ids','child_cluster_ids'):
                summary[key]=canonical_ids(tuple(sequence(summary[key],key)),key)
            summary['cell_count']=integer(summary['cell_count'],'refit cell_count',positive=True)
            sha256(summary['parent_result_sha256'],'parent_result_sha256')
            if (summary['method'] not in {'phenograph','flowsom'}
                    or not set(summary['child_cluster_ids'])<={item.cluster_id for item in clusters}
                    or summary['cell_count']!=sum(item.cell_count for item in clusters
                        if item.cluster_id in summary['child_cluster_ids'])):
                raise BatchPhenotypingError('The refit summary differs from the reviewed clusters.')
            object.__setattr__(self,'refit_summary',MappingProxyType(summary))

    def material_mapping(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_EVIDENCE_SCHEMA,
            "mode": self.mode.value,
            "phenotyping_group_id": self.phenotyping_group_id,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "active_plan_sha256": self.active_plan_sha256,
            "source_state_head_sha256": self.source_state_head_sha256,
            "source_authority_sha256s": list(self.source_authority_sha256s),
            "clustering_result_ref_sha256": self.clustering_result_ref_sha256,
            "clustering_result_sha256": self.clustering_result_sha256,
            "clustering_request_sha256": self.clustering_request_sha256,
            "clustering_generation_sha256": self.clustering_generation_sha256,
            "input_population_sha256": self.input_population_sha256,
            "assignment_inventory_sha256": self.assignment_inventory_sha256,
            "feature_schema_sha256": self.feature_schema_sha256,
            "cluster_inventory_sha256": self.cluster_inventory_sha256,
            "cell_count": self.cell_count,
            "cluster_count": self.cluster_count,
            "clusters": [item.to_mapping() for item in self.clusters],
            **({'refit_summary':dict(self.refit_summary)} if self.refit_summary is not None else {}),
        }

    @property
    def evidence_sha256(self) -> str:
        return digest(self.material_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {**self.material_mapping(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchClusterReviewEvidence":
        """Decode the serialized fields into a validated BatchClusterReviewEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        names=_EVIDENCE_FIELDS|({'refit_summary'} if isinstance(value,Mapping) and 'refit_summary' in value else set())
        value = closed_mapping(value, names, "cluster review evidence")
        if value["schema_version"] != REVIEW_EVIDENCE_SCHEMA:
            raise BatchPhenotypingError("cluster review evidence schema is unsupported")
        try:
            mode = ClusteringMode(value["mode"])
            bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in sequence(value["analysis_domain_bindings"], "analysis_domain_bindings")
            )
            clusters = tuple(
                BatchClusterReviewSummary.from_mapping(item)
                for item in sequence(value["clusters"], "clusters")
            )
            result = cls(
                mode=mode,
                phenotyping_group_id=value["phenotyping_group_id"],
                participant_ids=tuple(
                    value_item
                    for value_item in sequence(value["participant_ids"], "participant_ids")
                ),
                project_entry_ids=tuple(
                    value_item
                    for value_item in sequence(value["project_entry_ids"], "project_entry_ids")
                ),
                analysis_domain_bindings=bindings,
                active_plan_sha256=value["active_plan_sha256"],
                source_state_head_sha256=value["source_state_head_sha256"],
                source_authority_sha256s=tuple(
                    value_item
                    for value_item in sequence(
                        value["source_authority_sha256s"], "source_authority_sha256s"
                    )
                ),
                clustering_result_ref_sha256=value["clustering_result_ref_sha256"],
                clustering_result_sha256=value["clustering_result_sha256"],
                clustering_request_sha256=value["clustering_request_sha256"],
                clustering_generation_sha256=value["clustering_generation_sha256"],
                input_population_sha256=value["input_population_sha256"],
                assignment_inventory_sha256=value["assignment_inventory_sha256"],
                feature_schema_sha256=value["feature_schema_sha256"],
                cluster_inventory_sha256=value["cluster_inventory_sha256"],
                cell_count=value["cell_count"],
                cluster_count=value["cluster_count"],
                clusters=clusters,
                refit_summary=value.get('refit_summary'),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError("cluster review evidence content is invalid") from exc
        if result.evidence_sha256 != value["evidence_sha256"]:
            raise BatchPhenotypingError("cluster review evidence digest differs")
        return result


__all__ = [
    "REVIEW_EVIDENCE_SCHEMA",
    "BatchClusterReviewEntryCount",
    "BatchClusterReviewEvidence",
    "BatchClusterReviewMarkerProfile",
    "BatchClusterReviewSectionCount",
    "BatchClusterReviewSummary",
]
