"""Assemble the accepted cell and region generations into a domain-scoped export.

The handoff checks canonical image coordinates, segmentation cell identities,
measurement/phenotype views and finalized secondary membership. It retains the complete
domain inventory, including zero-cell and excluded regions, while the active groups identify
the cells supplied for downstream analysis."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any

from mif_pipeline.pipeline_core.domain import AnalysisDomainKind
from mif_pipeline.datasets.collection import CellDatasetCollection
from mif_pipeline.cell_analysis.measurement_rows import MARKER_INTENSITY_ROWS_SCHEMA
from mif_pipeline.pipeline_core.models import (
    CellRecord,
    ROIAnalysisError,
    ROIRecord,
    ReviewStatus,
    SemanticStatus,
    TissueSection,
)

# ==============================================================================
#                                Spatial handoff
# ==============================================================================


class SpatialHandoffError(ROIAnalysisError):
    """The immutable cell population is not ready for spatial consumption."""


def canonical_json_sha256(value: object) -> str:
    """Hash any JSON-compatible value with the canonical JSON policy."""
    encoded = json.dumps(
        _plain_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def path_free_identity_sha256(value: object) -> str:
    """Hash identity evidence after removing serialized locator ``path`` fields."""
    return canonical_json_sha256(_without_locator_paths(value))


def semantic_parent_content_key(*, analysis_domain_id: str, parent_roi_id: str) -> str:
    """Return the shared parent identity used by semantic/count/final evidence."""
    _nonblank(analysis_domain_id, "semantic analysis_domain_id")
    _nonblank(parent_roi_id, "semantic parent_roi_id")
    return canonical_json_sha256(
        {
            "schema_version": "semantic-parent-content/v1",
            "analysis_domain_id": analysis_domain_id,
            "parent_roi_id": parent_roi_id,
        }
    )


def semantic_secondary_content_key(
    *,
    analysis_domain_id: str,
    roi_id: str,
    path_object_id: str,
    structure_id: str,
    parent_content_key: str,
    geometry_sha256: str,
) -> str:
    """Return the one source identity shared through semantic finalization."""
    for value, field in (
        (analysis_domain_id, "semantic analysis_domain_id"),
        (roi_id, "semantic roi_id"),
        (path_object_id, "semantic path_object_id"),
        (structure_id, "semantic structure_id"),
    ):
        _nonblank(value, field)
    _sha256(parent_content_key, "semantic parent_content_key")
    _sha256(geometry_sha256, "semantic geometry SHA-256")
    return canonical_json_sha256(
        {
            "schema_version": "semantic-secondary-content/v1",
            "analysis_domain_id": analysis_domain_id,
            "roi_id": roi_id,
            "path_object_id": path_object_id,
            "structure_id": structure_id,
            "parent_content_key": parent_content_key,
            "geometry_sha256": geometry_sha256,
        }
    )


def stable_id_inventory_sha256(stable_ids: Sequence[str]) -> str:
    """Hash one exact ordered stable-ID inventory using an explicit JSON list."""
    return hashlib.sha256(json.dumps(list(stable_ids), ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DomainSetMemberEvidence:
    """The accepted current revision of one stable analysis domain."""

    analysis_domain_id: str
    analysis_domain_kind: AnalysisDomainKind
    slide_id: str
    section_id: str
    tissue_generation_id: str
    tissue_roi_id: str
    analysis_domain_revision: int
    analysis_domain_revision_sha256: str
    analysis_domain_manifest_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "analysis_domain_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
            "tissue_roi_id",
        ):
            _nonblank(getattr(self, field), field)
        if not isinstance(self.analysis_domain_kind, AnalysisDomainKind):
            raise SpatialHandoffError("analysis_domain_kind is invalid")
        _positive_int(self.analysis_domain_revision, "analysis_domain_revision")
        _sha256(
            self.analysis_domain_revision_sha256,
            "analysis-domain revision SHA-256",
        )
        _sha256(
            self.analysis_domain_manifest_sha256,
            "analysis-domain manifest SHA-256",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_kind": self.analysis_domain_kind.value,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "tissue_roi_id": self.tissue_roi_id,
            "analysis_domain_revision": self.analysis_domain_revision,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "analysis_domain_manifest_sha256": self.analysis_domain_manifest_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "DomainSetMemberEvidence":
        """Decode the serialized fields into a validated DomainSetMemberEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            {
                "analysis_domain_id",
                "analysis_domain_kind",
                "slide_id",
                "section_id",
                "tissue_generation_id",
                "tissue_roi_id",
                "analysis_domain_revision",
                "analysis_domain_revision_sha256",
                "analysis_domain_manifest_sha256",
            },
            "domain-set member",
        )
        try:
            return cls(
                analysis_domain_id=mapping["analysis_domain_id"],
                analysis_domain_kind=AnalysisDomainKind(mapping["analysis_domain_kind"]),
                slide_id=mapping["slide_id"],
                section_id=mapping["section_id"],
                tissue_generation_id=mapping["tissue_generation_id"],
                tissue_roi_id=mapping["tissue_roi_id"],
                analysis_domain_revision=mapping["analysis_domain_revision"],
                analysis_domain_revision_sha256=mapping["analysis_domain_revision_sha256"],
                analysis_domain_manifest_sha256=mapping["analysis_domain_manifest_sha256"],
            )
        except (TypeError, ValueError) as exc:
            raise SpatialHandoffError("domain-set member is invalid") from exc


@dataclass(frozen=True, slots=True)
class DomainSetEvidence:
    """Complete accepted analysis-domain set and its existing authority digest."""

    members: tuple[DomainSetMemberEvidence, ...]
    domain_set_sha256: str
    schema_version: str = "analysis-domain-set/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "analysis-domain-set/v1":
            raise SpatialHandoffError("analysis-domain set schema_version is invalid")
        members = tuple(self.members)
        if any(not isinstance(item, DomainSetMemberEvidence) for item in members):
            raise SpatialHandoffError("analysis-domain set requires typed member evidence")
        domain_ids = tuple(item.analysis_domain_id for item in members)
        if domain_ids != tuple(sorted(domain_ids)) or len(set(domain_ids)) != len(domain_ids):
            raise SpatialHandoffError("analysis-domain set members must be sorted and unique")
        manifests = tuple(item.analysis_domain_manifest_sha256 for item in members)
        if len(set(manifests)) != len(manifests):
            raise SpatialHandoffError("analysis-domain manifest digests must be unique")
        expected = canonical_json_sha256(
            {
                "schema_version": "analysis-domain-set/v1",
                "analysis_domain_manifest_sha256s": sorted(manifests),
            }
        )
        if self.domain_set_sha256 != expected:
            raise SpatialHandoffError("analysis-domain-set SHA-256 does not match")
        object.__setattr__(self, "members", members)

    @classmethod
    def create(cls, members: Iterable[DomainSetMemberEvidence]) -> "DomainSetEvidence":
        ordered = tuple(sorted(tuple(members), key=lambda item: item.analysis_domain_id))
        digest = canonical_json_sha256(
            {
                "schema_version": "analysis-domain-set/v1",
                "analysis_domain_manifest_sha256s": sorted(
                    item.analysis_domain_manifest_sha256 for item in ordered
                ),
            }
        )
        return cls(members=ordered, domain_set_sha256=digest)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "domain_set_sha256": self.domain_set_sha256,
            "members": [item.to_mapping() for item in self.members],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "DomainSetEvidence":
        mapping = _closed_mapping(
            value,
            {"schema_version", "domain_set_sha256", "members"},
            "analysis-domain set",
        )
        members = _sequence(mapping["members"], "analysis-domain set members")
        return cls(
            schema_version=mapping["schema_version"],
            domain_set_sha256=mapping["domain_set_sha256"],
            members=tuple(DomainSetMemberEvidence.from_mapping(v) for v in members),
        )


@dataclass(frozen=True, slots=True)
class CanonicalSpatialFrameEvidence:
    """Path-free identity of the exact calibrated canonical level-0 frame."""

    canonical_image_id: str
    canonical_image_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    x_um_per_px: float
    y_um_per_px: float
    spatial_frame_sha256: str
    schema_version: str = "canonical-spatial-frame/v1"
    series_index: int = 0
    coordinate_system: str = "global_level0_pixels"
    calibration_status: str = "calibrated"
    physical_size_unit: str = "micrometer"

    def __post_init__(self) -> None:
        if self.schema_version != "canonical-spatial-frame/v1":
            raise SpatialHandoffError("canonical spatial-frame schema_version is invalid")
        _nonblank(self.canonical_image_id, "canonical_image_id")
        _sha256(self.canonical_image_sha256, "canonical image SHA-256")
        if self.series_index != 0 or type(self.series_index) is not int:
            raise SpatialHandoffError("canonical spatial-frame series_index must be 0")
        if self.coordinate_system != "global_level0_pixels":
            raise SpatialHandoffError("canonical spatial-frame coordinate_system is invalid")
        _positive_int(self.level0_size_x_px, "level0_size_x_px")
        _positive_int(self.level0_size_y_px, "level0_size_y_px")
        if self.calibration_status != "calibrated":
            raise SpatialHandoffError("canonical spatial-frame calibration_status is invalid")
        if self.physical_size_unit != "micrometer":
            raise SpatialHandoffError("canonical spatial-frame physical_size_unit is invalid")
        object.__setattr__(
            self,
            "x_um_per_px",
            _positive_finite(self.x_um_per_px, "x_um_per_px"),
        )
        object.__setattr__(
            self,
            "y_um_per_px",
            _positive_finite(self.y_um_per_px, "y_um_per_px"),
        )
        if self.spatial_frame_sha256 != canonical_json_sha256(self.identity_mapping()):
            raise SpatialHandoffError("canonical spatial-frame SHA-256 does not match")

    @classmethod
    def create(
        cls,
        *,
        canonical_image_id: str,
        canonical_image_sha256: str,
        level0_size_x_px: int,
        level0_size_y_px: int,
        x_um_per_px: float,
        y_um_per_px: float,
    ) -> "CanonicalSpatialFrameEvidence":
        identity = {
            "schema_version": "canonical-spatial-frame/v1",
            "canonical_image_id": canonical_image_id,
            "canonical_image_sha256": canonical_image_sha256,
            "series_index": 0,
            "coordinate_system": "global_level0_pixels",
            "level0_size_x_px": level0_size_x_px,
            "level0_size_y_px": level0_size_y_px,
            "calibration_status": "calibrated",
            "physical_size_unit": "micrometer",
            "x_um_per_px": float(x_um_per_px),
            "y_um_per_px": float(y_um_per_px),
        }
        return cls(
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            level0_size_x_px=level0_size_x_px,
            level0_size_y_px=level0_size_y_px,
            x_um_per_px=float(x_um_per_px),
            y_um_per_px=float(y_um_per_px),
            spatial_frame_sha256=canonical_json_sha256(identity),
        )

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "series_index": self.series_index,
            "coordinate_system": self.coordinate_system,
            "level0_size_x_px": self.level0_size_x_px,
            "level0_size_y_px": self.level0_size_y_px,
            "calibration_status": self.calibration_status,
            "physical_size_unit": self.physical_size_unit,
            "x_um_per_px": self.x_um_per_px,
            "y_um_per_px": self.y_um_per_px,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_mapping(),
            "spatial_frame_sha256": self.spatial_frame_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "CanonicalSpatialFrameEvidence":
        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "canonical_image_id",
                "canonical_image_sha256",
                "series_index",
                "coordinate_system",
                "level0_size_x_px",
                "level0_size_y_px",
                "calibration_status",
                "physical_size_unit",
                "x_um_per_px",
                "y_um_per_px",
                "spatial_frame_sha256",
            },
            "canonical spatial frame",
        )
        try:
            return cls(**mapping)
        except TypeError as exc:
            raise SpatialHandoffError("canonical spatial frame is invalid") from exc


@dataclass(frozen=True, slots=True)
class DomainCellInventoryEvidence:
    """One independent, domain-scoped stable-cell inventory attestation."""

    stage: str
    analysis_domain_id: str
    analysis_domain_revision_sha256: str
    cell_index_scope: Mapping[str, str]
    count: int
    cell_ids_sha256: str
    source_schema_version: str
    source_evidence_sha256: str
    attestation_sha256: str
    schema_version: str = "domain-cell-inventory/v2"

    def __post_init__(self) -> None:
        if self.schema_version != "domain-cell-inventory/v2":
            raise SpatialHandoffError("domain cell inventory schema_version is invalid")
        if self.stage not in {"segmentation", "phenotyping", "export"}:
            raise SpatialHandoffError("domain cell inventory stage is invalid")
        _nonblank(self.analysis_domain_id, "analysis_domain_id")
        _sha256(
            self.analysis_domain_revision_sha256,
            "analysis-domain revision SHA-256",
        )
        if type(self.count) is not int or self.count < 0:
            raise SpatialHandoffError("domain cell inventory count is invalid")
        _sha256(self.cell_ids_sha256, "domain cell inventory stable-ID SHA-256")
        expected_scope = {"dataset": "selected-cell-dataset", "field": "analysis_domain_id", "value": self.analysis_domain_id}
        if self.cell_index_scope != expected_scope:
            raise SpatialHandoffError("domain cell inventory index scope is invalid")
        object.__setattr__(self, "cell_index_scope", MappingProxyType(expected_scope))
        _nonblank(self.source_schema_version, "source_schema_version")
        _sha256(self.source_evidence_sha256, "source evidence SHA-256")
        if self.attestation_sha256 != canonical_json_sha256(self.identity_mapping()):
            raise SpatialHandoffError("domain cell inventory attestation SHA-256 does not match")

    @classmethod
    def create(
        cls,
        *,
        stage: str,
        analysis_domain_id: str,
        analysis_domain_revision_sha256: str,
        cell_ids: Sequence[str],
        source_schema_version: str,
        source_evidence_sha256: str,
    ) -> "DomainCellInventoryEvidence":
        ids = _sorted_unique_ids(tuple(sorted(cell_ids)), "domain cell inventory IDs")
        return cls.from_summary(
            stage=stage, analysis_domain_id=analysis_domain_id,
            analysis_domain_revision_sha256=analysis_domain_revision_sha256,
            count=len(ids), cell_ids_sha256=stable_id_inventory_sha256(ids),
            source_schema_version=source_schema_version,
            source_evidence_sha256=source_evidence_sha256,
        )

    @classmethod
    def from_summary(
        cls, *, stage: str, analysis_domain_id: str,
        analysis_domain_revision_sha256: str, count: int, cell_ids_sha256: str,
        source_schema_version: str, source_evidence_sha256: str,
    ) -> "DomainCellInventoryEvidence":
        """Use the completed dataset's compact inventory without loading its IDs."""
        identity = {
            "schema_version": "domain-cell-inventory/v2",
            "stage": stage,
            "analysis_domain_id": analysis_domain_id,
            "analysis_domain_revision_sha256": analysis_domain_revision_sha256,
            "cell_index_scope": {"dataset": "selected-cell-dataset", "field": "analysis_domain_id", "value": analysis_domain_id},
            "count": count,
            "cell_ids_sha256": cell_ids_sha256,
            "source_schema_version": source_schema_version,
            "source_evidence_sha256": source_evidence_sha256,
        }
        return cls(
            stage=stage,
            analysis_domain_id=analysis_domain_id,
            analysis_domain_revision_sha256=analysis_domain_revision_sha256,
            cell_index_scope=identity["cell_index_scope"],
            count=count,
            cell_ids_sha256=identity["cell_ids_sha256"],
            source_schema_version=source_schema_version,
            source_evidence_sha256=source_evidence_sha256,
            attestation_sha256=canonical_json_sha256(identity),
        )

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "cell_index_scope": dict(self.cell_index_scope),
            "count": self.count,
            "cell_ids_sha256": self.cell_ids_sha256,
            "source_schema_version": self.source_schema_version,
            "source_evidence_sha256": self.source_evidence_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_mapping(),
            "attestation_sha256": self.attestation_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "DomainCellInventoryEvidence":
        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "stage",
                "analysis_domain_id",
                "analysis_domain_revision_sha256",
                "cell_index_scope",
                "count",
                "cell_ids_sha256",
                "source_schema_version",
                "source_evidence_sha256",
                "attestation_sha256",
            },
            "domain cell inventory",
        )
        return cls(**mapping)

    def matches_ids(self, cell_ids: Sequence[str]) -> bool:
        """Compare required dataset membership with the authenticated summary."""
        return len(cell_ids) == self.count and stable_id_inventory_sha256(cell_ids) == self.cell_ids_sha256


@dataclass(frozen=True, slots=True)
class NoSecondaryDomainCompletionEvidence:
    """Completion for a domain without configured secondary structures.

    Secondary validation is not applicable. The accepted primary count outcome
    still determines whether this domain remains active in the final analysis.
    """

    analysis_domain_id: str
    analysis_domain_revision_sha256: str
    domain_set_sha256: str
    spatial_frame_sha256: str
    section_id: str
    tissue_generation_id: str
    pipeline_run_id: str
    analysis_config_sha256: str
    raw_input_sha256: str
    configured_secondary_structure_ids: tuple[str, ...]
    configured_secondary_structure_ids_sha256: str
    candidate_secondary_roi_ids: tuple[str, ...]
    candidate_secondary_roi_ids_sha256: str
    completion_sha256: str
    schema_version: str = "semantic-domain-completion/v1"
    kind: str = "no_secondary_configured"
    stage_status: str = "completed"
    applicability_status: str = "not_applicable"
    domain_status: str = "active"
    reason: str = "no_configured_secondary_structures"
    raw_input_schema_version: str = "qupath-raw-objects/v1"
    count_decision_status: str = "not_required"
    qupath_finalizer_mutation_status: str = "not_performed"

    def __post_init__(self) -> None:
        """Validate the NoSecondaryDomainCompletionEvidence fields when the record is
        constructed.

        The checks cover ``configured_secondary_structure_ids``,
        ``candidate_secondary_roi_ids``, ``completion_sha256``,
        ``configured_secondary_structure_ids_sha256``,
        ``candidate_secondary_roi_ids_sha256``, ``identity_mapping``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        fixed = {
            "schema_version": "semantic-domain-completion/v1",
            "kind": "no_secondary_configured",
            "stage_status": "completed",
            "applicability_status": "not_applicable",
            "reason": "no_configured_secondary_structures",
            "raw_input_schema_version": "qupath-raw-objects/v1",
            "count_decision_status": "not_required",
            "qupath_finalizer_mutation_status": "not_performed",
        }
        if any(getattr(self, key) != expected for key, expected in fixed.items()):
            raise SpatialHandoffError("no-secondary completion literals are invalid")
        if self.domain_status not in {"active", "excluded_below_minimum", "excluded_by_primary_count_review"}:
            raise SpatialHandoffError("no-secondary primary domain disposition is invalid")
        for field in (
            "analysis_domain_id",
            "section_id",
            "tissue_generation_id",
            "pipeline_run_id",
        ):
            _nonblank(getattr(self, field), field)
        for field in (
            "analysis_domain_revision_sha256",
            "domain_set_sha256",
            "spatial_frame_sha256",
            "analysis_config_sha256",
            "raw_input_sha256",
        ):
            _sha256(getattr(self, field), field)
        configured = _sorted_unique_ids(
            self.configured_secondary_structure_ids,
            "configured secondary structure IDs",
        )
        candidates = _sorted_unique_ids(
            self.candidate_secondary_roi_ids,
            "candidate secondary ROI IDs",
        )
        if configured or candidates:
            raise SpatialHandoffError(
                "no-secondary completion requires empty configured/candidate inventories"
            )
        empty_digest = stable_id_inventory_sha256(())
        if (
            self.configured_secondary_structure_ids_sha256 != empty_digest
            or self.candidate_secondary_roi_ids_sha256 != empty_digest
        ):
            raise SpatialHandoffError("no-secondary empty inventory SHA-256 does not match")
        object.__setattr__(self, "configured_secondary_structure_ids", configured)
        object.__setattr__(self, "candidate_secondary_roi_ids", candidates)
        if self.completion_sha256 != canonical_json_sha256(self.identity_mapping()):
            raise SpatialHandoffError("no-secondary completion SHA-256 does not match")

    @classmethod
    def create(
        cls,
        *,
        analysis_domain_id: str,
        analysis_domain_revision_sha256: str,
        domain_set_sha256: str,
        spatial_frame_sha256: str,
        section_id: str,
        tissue_generation_id: str,
        pipeline_run_id: str,
        analysis_config_sha256: str,
        raw_input_sha256: str,
        domain_status: str,
    ) -> "NoSecondaryDomainCompletionEvidence":
        empty_digest = stable_id_inventory_sha256(())
        identity = {
            "schema_version": "semantic-domain-completion/v1",
            "kind": "no_secondary_configured",
            "stage_status": "completed",
            "applicability_status": "not_applicable",
            "domain_status": domain_status,
            "reason": "no_configured_secondary_structures",
            "analysis_domain_id": analysis_domain_id,
            "analysis_domain_revision_sha256": analysis_domain_revision_sha256,
            "domain_set_sha256": domain_set_sha256,
            "spatial_frame_sha256": spatial_frame_sha256,
            "section_id": section_id,
            "tissue_generation_id": tissue_generation_id,
            "pipeline_run_id": pipeline_run_id,
            "analysis_config_sha256": analysis_config_sha256,
            "raw_input_schema_version": "qupath-raw-objects/v1",
            "raw_input_sha256": raw_input_sha256,
            "configured_secondary_structure_ids": [],
            "configured_secondary_structure_ids_sha256": empty_digest,
            "candidate_secondary_roi_ids": [],
            "candidate_secondary_roi_ids_sha256": empty_digest,
            "count_decision_status": "not_required",
            "qupath_finalizer_mutation_status": "not_performed",
        }
        return cls(
            analysis_domain_id=analysis_domain_id,
            analysis_domain_revision_sha256=analysis_domain_revision_sha256,
            domain_set_sha256=domain_set_sha256,
            spatial_frame_sha256=spatial_frame_sha256,
            section_id=section_id,
            tissue_generation_id=tissue_generation_id,
            pipeline_run_id=pipeline_run_id,
            analysis_config_sha256=analysis_config_sha256,
            raw_input_sha256=raw_input_sha256,
            domain_status=domain_status,
            configured_secondary_structure_ids=(),
            configured_secondary_structure_ids_sha256=empty_digest,
            candidate_secondary_roi_ids=(),
            candidate_secondary_roi_ids_sha256=empty_digest,
            completion_sha256=canonical_json_sha256(identity),
        )

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "stage_status": self.stage_status,
            "applicability_status": self.applicability_status,
            "domain_status": self.domain_status,
            "reason": self.reason,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "domain_set_sha256": self.domain_set_sha256,
            "spatial_frame_sha256": self.spatial_frame_sha256,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "pipeline_run_id": self.pipeline_run_id,
            "analysis_config_sha256": self.analysis_config_sha256,
            "raw_input_schema_version": self.raw_input_schema_version,
            "raw_input_sha256": self.raw_input_sha256,
            "configured_secondary_structure_ids": list(self.configured_secondary_structure_ids),
            "configured_secondary_structure_ids_sha256": (
                self.configured_secondary_structure_ids_sha256
            ),
            "candidate_secondary_roi_ids": list(self.candidate_secondary_roi_ids),
            "candidate_secondary_roi_ids_sha256": (self.candidate_secondary_roi_ids_sha256),
            "count_decision_status": self.count_decision_status,
            "qupath_finalizer_mutation_status": (self.qupath_finalizer_mutation_status),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_mapping(),
            "completion_sha256": self.completion_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "NoSecondaryDomainCompletionEvidence":
        """Decode the serialized fields into a validated NoSecondaryDomainCompletionEvidence
        record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "kind",
                "stage_status",
                "applicability_status",
                "domain_status",
                "reason",
                "analysis_domain_id",
                "analysis_domain_revision_sha256",
                "domain_set_sha256",
                "spatial_frame_sha256",
                "section_id",
                "tissue_generation_id",
                "pipeline_run_id",
                "analysis_config_sha256",
                "raw_input_schema_version",
                "raw_input_sha256",
                "configured_secondary_structure_ids",
                "configured_secondary_structure_ids_sha256",
                "candidate_secondary_roi_ids",
                "candidate_secondary_roi_ids_sha256",
                "count_decision_status",
                "qupath_finalizer_mutation_status",
                "completion_sha256",
            },
            "no-secondary completion",
        )
        configured = _sequence(
            mapping["configured_secondary_structure_ids"],
            "configured secondary structure IDs",
        )
        candidates = _sequence(
            mapping["candidate_secondary_roi_ids"], "candidate secondary ROI IDs"
        )
        return cls(
            **{
                **mapping,
                "configured_secondary_structure_ids": tuple(configured),
                "candidate_secondary_roi_ids": tuple(candidates),
            }
        )


@dataclass(frozen=True, slots=True)
class SemanticResultArtifactEvidence:
    """Closed identity of one immutable semantic-result-set artifact."""

    schema_version: str
    path: str
    sha256: str
    record_count: int
    semantic_result_set_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "semantic-result-set/v1":
            raise SpatialHandoffError("semantic result artifact schema is invalid")
        _nonblank(self.path, "semantic result artifact path")
        _sha256(self.sha256, "semantic result artifact SHA-256")
        _nonnegative_int(self.record_count, "semantic result record_count")
        _sha256(
            self.semantic_result_set_sha256,
            "semantic result-set SHA-256",
        )

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "record_count": self.record_count,
            "semantic_result_set_sha256": self.semantic_result_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self.identity_mapping(), "path": self.path}

    @classmethod
    def from_mapping(cls, value: object) -> "SemanticResultArtifactEvidence":
        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "path",
                "sha256",
                "record_count",
                "semantic_result_set_sha256",
            },
            "semantic result artifact",
        )
        return cls(**mapping)


@dataclass(frozen=True, slots=True)
class ManualSelectionEvidence:
    """Accepted manual inventory descriptor and its exact selection lineage."""

    structure_id: str
    inventory_descriptor: Mapping[str, object]
    selection_lineage: Mapping[str, object]
    lineage_sha256: str
    schema_version: str = "manual-selection-evidence/v1"

    def __post_init__(self) -> None:
        """Validate the ManualSelectionEvidence fields when the record is constructed.

        The checks cover ``schema_version``, ``structure_id``, ``inventory_descriptor``,
        ``selection_lineage``, ``lineage_sha256``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.schema_version != "manual-selection-evidence/v1":
            raise SpatialHandoffError("manual selection evidence schema is invalid")
        _nonblank(self.structure_id, "manual selection structure_id")
        descriptor = _closed_mapping(
            self.inventory_descriptor,
            {"artifact_id", "schema_version", "path", "sha256", "record_count"},
            "manual selection inventory descriptor",
        )
        if descriptor.get("schema_version") != "structure-count-inventory/v1":
            raise SpatialHandoffError("manual selection inventory schema is invalid")
        _nonblank(descriptor.get("artifact_id"), "manual selection artifact_id")
        _nonblank(descriptor.get("path"), "manual selection artifact path")
        _sha256(descriptor.get("sha256"), "manual selection artifact SHA-256")
        _nonnegative_int(descriptor.get("record_count"), "manual selection record_count")
        lineage = _closed_mapping(
            self.selection_lineage,
            {
                "analysis_domain_sha256",
                "config_sha256",
                "result_sha256",
                "inventory_sha256",
                "structure_id",
                "algorithm",
                "selected_content_keys",
                "selection_criterion",
                "reviewer_id",
                "rationale",
            },
            "manual selection lineage",
        )
        for field in (
            "analysis_domain_sha256",
            "config_sha256",
            "result_sha256",
            "inventory_sha256",
        ):
            _sha256(lineage.get(field), f"manual selection {field}")
        if (
            lineage.get("structure_id") != self.structure_id
            or lineage.get("algorithm") != "manual-select/v1"
        ):
            raise SpatialHandoffError("manual selection lineage identity is invalid")
        selected = _sorted_unique_ids(
            _sequence(
                lineage.get("selected_content_keys"),
                "manual selected content keys",
            ),
            "manual selected content keys",
        )
        if not selected:
            raise SpatialHandoffError("manual selection requires selected content keys")
        for field in ("selection_criterion", "reviewer_id", "rationale"):
            _nonblank(lineage.get(field), f"manual selection {field}")
        frozen_descriptor = _freeze_json(descriptor)
        frozen_lineage = _freeze_json(lineage)
        assert isinstance(frozen_descriptor, Mapping)
        assert isinstance(frozen_lineage, Mapping)
        object.__setattr__(self, "inventory_descriptor", frozen_descriptor)
        object.__setattr__(self, "selection_lineage", frozen_lineage)
        if self.lineage_sha256 != canonical_json_sha256(frozen_lineage):
            raise SpatialHandoffError("manual selection lineage SHA-256 does not match")

    @classmethod
    def create(
        cls,
        *,
        structure_id: str,
        inventory_descriptor: Mapping[str, object],
        selection_lineage: Mapping[str, object],
    ) -> "ManualSelectionEvidence":
        return cls(
            structure_id=structure_id,
            inventory_descriptor=inventory_descriptor,
            selection_lineage=selection_lineage,
            lineage_sha256=canonical_json_sha256(selection_lineage),
        )

    def identity_mapping(self) -> dict[str, object]:
        descriptor = dict(_plain_json(self.inventory_descriptor))
        descriptor.pop("path", None)
        return {
            "schema_version": self.schema_version,
            "structure_id": self.structure_id,
            "inventory_descriptor": descriptor,
            "selection_lineage": _plain_json(self.selection_lineage),
            "lineage_sha256": self.lineage_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "structure_id": self.structure_id,
            "inventory_descriptor": _plain_json(self.inventory_descriptor),
            "selection_lineage": _plain_json(self.selection_lineage),
            "lineage_sha256": self.lineage_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ManualSelectionEvidence":
        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "structure_id",
                "inventory_descriptor",
                "selection_lineage",
                "lineage_sha256",
            },
            "manual selection evidence",
        )
        return cls(**mapping)


@dataclass(frozen=True, slots=True)
class FinalObjectDispositionEvidence:
    """Checked bridge from count content identity to final QuPath ROI identity."""

    content_key: str
    roi_id: str
    path_object_id: str
    structure_id: str
    parent_content_key: str
    geometry_sha256: str
    disposition: str
    selection_lineage_sha256: str | None
    disposition_sha256: str
    schema_version: str = "final-object-disposition/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "final-object-disposition/v1":
            raise SpatialHandoffError("final object disposition schema is invalid")
        _sha256(self.content_key, "disposition content_key")
        _nonblank(self.roi_id, "disposition roi_id")
        _nonblank(self.path_object_id, "disposition path_object_id")
        _nonblank(self.structure_id, "disposition structure_id")
        _sha256(self.parent_content_key, "disposition parent_content_key")
        _sha256(self.geometry_sha256, "disposition geometry SHA-256")
        if self.disposition not in {
            "active_included",
            "valid_not_selected_by_cap",
            "audit_excluded_below_minimum",
        }:
            raise SpatialHandoffError("final object disposition is invalid")
        if self.selection_lineage_sha256 is not None:
            _sha256(
                self.selection_lineage_sha256,
                "disposition selection lineage SHA-256",
            )
        if self.disposition_sha256 != canonical_json_sha256(self.identity_mapping()):
            raise SpatialHandoffError("final object disposition SHA-256 does not match")

    @classmethod
    def create(cls, **values: object) -> "FinalObjectDispositionEvidence":
        identity = {"schema_version": "final-object-disposition/v1", **values}
        return cls(**values, disposition_sha256=canonical_json_sha256(identity))

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "content_key": self.content_key,
            "roi_id": self.roi_id,
            "path_object_id": self.path_object_id,
            "structure_id": self.structure_id,
            "parent_content_key": self.parent_content_key,
            "geometry_sha256": self.geometry_sha256,
            "disposition": self.disposition,
            "selection_lineage_sha256": self.selection_lineage_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_mapping(),
            "disposition_sha256": self.disposition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "FinalObjectDispositionEvidence":
        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "content_key",
                "roi_id",
                "path_object_id",
                "structure_id",
                "parent_content_key",
                "geometry_sha256",
                "disposition",
                "selection_lineage_sha256",
                "disposition_sha256",
            },
            "final object disposition",
        )
        return cls(**mapping)


@dataclass(frozen=True, slots=True)
class FinalizedSecondaryDomainEvidence:
    """Applicable secondary semantic/count/finalization evidence for one domain."""

    analysis_domain_id: str
    analysis_domain_revision_sha256: str
    domain_set_sha256: str
    spatial_frame_sha256: str
    semantic_result_artifact: SemanticResultArtifactEvidence
    count_decision: Mapping[str, object]
    decision_sha256: str
    dispositions: tuple[FinalObjectDispositionEvidence, ...]
    manual_selections: tuple[ManualSelectionEvidence, ...]
    finalization_receipt: Mapping[str, object]
    finalization_sha256: str
    domain_status: str
    schema_version: str = "domain-finalization-evidence/v1"
    kind: str = "secondary_finalized"
    stage_status: str = "completed"
    applicability_status: str = "applicable"

    def __post_init__(self) -> None:
        """Validate the FinalizedSecondaryDomainEvidence fields when the record is constructed.

        The checks cover ``analysis_domain_id``, ``domain_status``, ``count_decision``,
        ``dispositions``, ``manual_selections``, ``finalization_receipt``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (
            self.schema_version != "domain-finalization-evidence/v1"
            or self.kind != "secondary_finalized"
            or self.stage_status != "completed"
            or self.applicability_status != "applicable"
        ):
            raise SpatialHandoffError("secondary-finalized discriminator is invalid")
        _nonblank(self.analysis_domain_id, "finalization analysis_domain_id")
        for field in (
            "analysis_domain_revision_sha256",
            "domain_set_sha256",
            "spatial_frame_sha256",
            "decision_sha256",
        ):
            _sha256(getattr(self, field), field)
        if self.domain_status not in {"active", "excluded_below_minimum"}:
            raise SpatialHandoffError("finalized domain status is invalid")
        if not isinstance(self.semantic_result_artifact, SemanticResultArtifactEvidence):
            raise SpatialHandoffError("semantic result artifact evidence is required")
        decision = _validate_count_decision(
            self.count_decision,
            expected_domain_id=self.analysis_domain_id,
            expected_revision_sha256=self.analysis_domain_revision_sha256,
            expected_result_sha256=(self.semantic_result_artifact.semantic_result_set_sha256),
            expected_domain_status=self.domain_status,
        )
        if decision.get(
            "decision_sha256"
        ) != self.decision_sha256 or self.decision_sha256 != canonical_json_sha256(
            {key: value for key, value in decision.items() if key != "decision_sha256"}
        ):
            raise SpatialHandoffError("count decision SHA-256 does not match")
        dispositions = tuple(self.dispositions)
        if any(not isinstance(item, FinalObjectDispositionEvidence) for item in dispositions):
            raise SpatialHandoffError("final dispositions require typed evidence")
        if tuple(item.content_key for item in dispositions) != tuple(
            sorted(item.content_key for item in dispositions)
        ) or len({item.content_key for item in dispositions}) != len(dispositions):
            raise SpatialHandoffError("final dispositions must be sorted and unique")
        if len({item.roi_id for item in dispositions}) != len(dispositions) or len(
            {item.path_object_id for item in dispositions}
        ) != len(dispositions):
            raise SpatialHandoffError("final disposition source ROI/path-object IDs must be unique")
        _validate_dispositions_against_decision(dispositions, decision)
        manual = tuple(self.manual_selections)
        if any(not isinstance(item, ManualSelectionEvidence) for item in manual):
            raise SpatialHandoffError("manual selection evidence is invalid")
        if tuple(item.structure_id for item in manual) != tuple(
            sorted(item.structure_id for item in manual)
        ) or len({item.structure_id for item in manual}) != len(manual):
            raise SpatialHandoffError("manual selection evidence is duplicated or unsorted")
        _validate_manual_evidence_against_decision(manual, decision)
        receipt = _validate_finalization_receipt(
            self.finalization_receipt,
            domain_id=self.analysis_domain_id,
            artifact=self.semantic_result_artifact,
            dispositions=dispositions,
        )
        object.__setattr__(self, "count_decision", _freeze_json(decision))
        object.__setattr__(self, "dispositions", dispositions)
        object.__setattr__(self, "manual_selections", manual)
        object.__setattr__(self, "finalization_receipt", _freeze_json(receipt))
        if self.finalization_sha256 != canonical_json_sha256(_without_locator_paths(receipt)):
            raise SpatialHandoffError("semantic finalization SHA-256 does not match")

    @classmethod
    def create(cls, **values: object) -> "FinalizedSecondaryDomainEvidence":
        receipt = values.get("finalization_receipt")
        if not isinstance(receipt, Mapping):
            raise SpatialHandoffError("semantic finalization receipt is invalid")
        return cls(
            **values,
            finalization_sha256=canonical_json_sha256(_without_locator_paths(receipt)),
        )

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "stage_status": self.stage_status,
            "applicability_status": self.applicability_status,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "domain_set_sha256": self.domain_set_sha256,
            "spatial_frame_sha256": self.spatial_frame_sha256,
            "semantic_result_artifact": self.semantic_result_artifact.identity_mapping(),
            "count_decision": _plain_json(self.count_decision),
            "decision_sha256": self.decision_sha256,
            "dispositions": [item.to_mapping() for item in self.dispositions],
            "manual_selections": [item.identity_mapping() for item in self.manual_selections],
            "finalization_receipt": _without_locator_paths(self.finalization_receipt),
            "finalization_sha256": self.finalization_sha256,
            "domain_status": self.domain_status,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "stage_status": self.stage_status,
            "applicability_status": self.applicability_status,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "domain_set_sha256": self.domain_set_sha256,
            "spatial_frame_sha256": self.spatial_frame_sha256,
            "semantic_result_artifact": self.semantic_result_artifact.to_mapping(),
            "count_decision": _plain_json(self.count_decision),
            "decision_sha256": self.decision_sha256,
            "dispositions": [item.to_mapping() for item in self.dispositions],
            "manual_selections": [item.to_mapping() for item in self.manual_selections],
            "finalization_receipt": _plain_json(self.finalization_receipt),
            "finalization_sha256": self.finalization_sha256,
            "domain_status": self.domain_status,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "FinalizedSecondaryDomainEvidence":
        """Decode the serialized fields into a validated FinalizedSecondaryDomainEvidence
        record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "kind",
                "stage_status",
                "applicability_status",
                "analysis_domain_id",
                "analysis_domain_revision_sha256",
                "domain_set_sha256",
                "spatial_frame_sha256",
                "semantic_result_artifact",
                "count_decision",
                "decision_sha256",
                "dispositions",
                "manual_selections",
                "finalization_receipt",
                "finalization_sha256",
                "domain_status",
            },
            "finalized secondary-domain evidence",
        )
        dispositions = _sequence(mapping["dispositions"], "final object dispositions")
        manual = _sequence(mapping["manual_selections"], "manual selections")
        return cls(
            **{
                **mapping,
                "semantic_result_artifact": SemanticResultArtifactEvidence.from_mapping(
                    mapping["semantic_result_artifact"]
                ),
                "dispositions": tuple(
                    FinalObjectDispositionEvidence.from_mapping(item) for item in dispositions
                ),
                "manual_selections": tuple(
                    ManualSelectionEvidence.from_mapping(item) for item in manual
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class PrimaryExcludedDomainCompletionEvidence:
    """Retain primary exclusion when a complete section bypasses cell analysis.

    The final handoff owner joins these two committed result references to the
    accepted domain and the empty segmentation base. No phenotype, measurement
    or semantic-finalization receipt is manufactured for the skipped work.
    """

    primary_result_ref_sha256: str
    segmentation_result_ref_sha256: str
    pipeline_run_id: str
    primary_count_decision: Mapping[str, object]
    domain_status: str
    kind: str = "primary_excluded_section"
    schema_version: str = "primary-excluded-domain-completion/v1"

    def __post_init__(self) -> None:
        from mif_pipeline.compartment_analysis import PrimaryCountDecision

        if (self.kind != "primary_excluded_section"
                or self.schema_version != "primary-excluded-domain-completion/v1"
                or self.domain_status not in {
                    "excluded_below_minimum", "excluded_by_primary_count_review"
                }):
            raise SpatialHandoffError("primary exclusion completion discriminator is invalid")
        _sha256(self.primary_result_ref_sha256, "excluded primary result")
        _sha256(self.segmentation_result_ref_sha256, "excluded segmentation result")
        _nonblank(self.pipeline_run_id, "excluded pipeline run")
        decision = PrimaryCountDecision.from_mapping(self.primary_count_decision)
        if ((decision.domain_exclusion is not None)
                != (self.domain_status == "excluded_below_minimum")):
            raise SpatialHandoffError("primary exclusion differs from its count decision")
        if (self.domain_status == "excluded_by_primary_count_review"
                and not any(item.status.value == "primary_review_required"
                            for item in decision.outputs)):
            raise SpatialHandoffError("primary review exclusion lacks a maximum review")
        object.__setattr__(self, "primary_count_decision", _freeze_json(decision.to_mapping()))

    def identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "kind": self.kind,
            "primary_result_ref_sha256": self.primary_result_ref_sha256,
            "segmentation_result_ref_sha256": self.segmentation_result_ref_sha256,
            "pipeline_run_id": self.pipeline_run_id,
            "primary_count_decision": _plain_json(self.primary_count_decision),
            "domain_status": self.domain_status,
        }

    def to_mapping(self) -> dict[str, object]:
        return self.identity_mapping()

    @classmethod
    def from_mapping(cls, value: object) -> "PrimaryExcludedDomainCompletionEvidence":
        return cls(**_closed_mapping(value, {
            "schema_version", "kind", "primary_result_ref_sha256",
            "segmentation_result_ref_sha256", "pipeline_run_id",
            "primary_count_decision", "domain_status",
        }, "primary exclusion completion"))


DomainFinalizationEvidence = (FinalizedSecondaryDomainEvidence
    | NoSecondaryDomainCompletionEvidence | PrimaryExcludedDomainCompletionEvidence)


@dataclass(frozen=True, slots=True)
class DomainHandoffEvidence:
    """Complete lineage and effective disposition for one accepted domain.

    Primary exclusion takes precedence over the secondary result and requires
    empty active cell inventories. The secondary finalization retains its own
    complete count, subset and selection-lineage evidence without alteration.

    ``phenotype_source_run_id`` identifies the accepted phenotype application
    attempt. A recovered application can use a view prepared by an earlier
    attempt; that original ID remains in the content-hashed view metadata.

    The original segmentation remains recorded independently of the measured
    population. ``measurement_rows_sha256`` binds the current cell inventory,
    including saved deletions, to the measurements used for phenotyping.
    """

    member: DomainSetMemberEvidence
    spatial_frame_sha256: str
    segmentation_population_id: str
    segmentation_canonical_image_id: str
    segmentation_canonical_image_sha256: str
    segmentation_domain_set_sha256: str
    segmentation_section_population_sha256: str
    measurement_rows_sha256: str | None
    segmentation_domain_sha256: str
    segmentation_domain_cell_mapping_sha256: str
    selected_phenotype_view: str | None
    phenotype_generation_id: str | None
    phenotype_inventory_sha256: str | None
    phenotype_section_artifact_sha256: str | None
    phenotype_section_lineage_sha256: str | None
    phenotype_source_run_id: str | None
    analysis_config_sha256: str
    semantic_raw_input_sha256: str | None
    segmented_inventory: DomainCellInventoryEvidence
    phenotyped_inventory: DomainCellInventoryEvidence | None
    exported_inventory: DomainCellInventoryEvidence
    finalization: DomainFinalizationEvidence
    primary_domain_status: str
    domain_status: str
    domain_result_sha256: str
    schema_version: str = "domain-handoff-evidence/v1"

    def __post_init__(self) -> None:
        """Validate the DomainHandoffEvidence fields when the record is constructed.

        The checks cover ``schema_version``, ``segmented_inventory``,
        ``phenotyped_inventory``, ``exported_inventory``, ``domain_status``,
        ``segmentation_domain_set_sha256``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        if self.schema_version != "domain-handoff-evidence/v1":
            raise SpatialHandoffError("domain handoff evidence schema_version is invalid")
        if not isinstance(self.member, DomainSetMemberEvidence):
            raise SpatialHandoffError("domain handoff requires typed domain-set member")
        excluded_section = isinstance(self.finalization, PrimaryExcludedDomainCompletionEvidence)
        absent_fields = ("selected_phenotype_view", "phenotype_generation_id",
            "phenotype_inventory_sha256", "phenotype_section_artifact_sha256",
            "phenotype_section_lineage_sha256", "phenotype_source_run_id",
            "semantic_raw_input_sha256", "phenotyped_inventory", "measurement_rows_sha256")
        if excluded_section and any(getattr(self, name) is not None for name in absent_fields):
            raise SpatialHandoffError("excluded section cannot claim performed cell analysis")
        for field in (
            "segmentation_population_id",
            "segmentation_canonical_image_id",
            "selected_phenotype_view",
            "phenotype_generation_id",
            "phenotype_source_run_id",
        ):
            if not excluded_section or field not in absent_fields:
                _nonblank(getattr(self, field), field)
        for field in (
            "spatial_frame_sha256",
            "segmentation_canonical_image_sha256",
            "segmentation_domain_set_sha256",
            "segmentation_section_population_sha256",
            "measurement_rows_sha256",
            "segmentation_domain_sha256",
            "segmentation_domain_cell_mapping_sha256",
            "phenotype_inventory_sha256",
            "phenotype_section_artifact_sha256",
            "phenotype_section_lineage_sha256",
            "analysis_config_sha256",
            "semantic_raw_input_sha256",
        ):
            if not excluded_section or field not in absent_fields:
                _sha256(getattr(self, field), field)
        inventories = ((self.segmented_inventory, self.exported_inventory)
            if excluded_section else
            (self.segmented_inventory, self.phenotyped_inventory, self.exported_inventory))
        if any(not isinstance(item, DomainCellInventoryEvidence) for item in inventories):
            raise SpatialHandoffError("domain handoff cell attestations are incomplete")
        expected_stages = ("segmentation", "export") if excluded_section else (
            "segmentation", "phenotyping", "export")
        if tuple(item.stage for item in inventories) != expected_stages:
            raise SpatialHandoffError("domain handoff cell attestation stages are invalid")
        if any(
            item.analysis_domain_id != self.member.analysis_domain_id
            or item.analysis_domain_revision_sha256 != self.member.analysis_domain_revision_sha256
            for item in inventories
        ):
            raise SpatialHandoffError("domain cell attestation scope is stale")
        if len({(item.count, item.cell_ids_sha256) for item in inventories}) != 1:
            raise SpatialHandoffError(
                "domain segmented, phenotyped, and exported stable-cell IDs differ"
            )
        expected_sources = (
            (
                self.segmented_inventory,
                "segmentation-population/v2" if excluded_section else MARKER_INTENSITY_ROWS_SCHEMA,
                self.segmentation_section_population_sha256 if excluded_section else self.measurement_rows_sha256,
            ),
            (
                self.phenotyped_inventory,
                "phenotype-view-artifact/v1",
                self.phenotype_section_artifact_sha256,
            ),
        )
        for evidence, schema, digest in expected_sources:
            if evidence is None and excluded_section:
                continue
            if (
                evidence.source_schema_version != schema
                or evidence.source_evidence_sha256 != digest
            ):
                raise SpatialHandoffError(f"domain {evidence.stage} source evidence is stale")
        if self.exported_inventory.source_schema_version != "qupath-raw-objects/v1":
            raise SpatialHandoffError("domain export source schema is invalid")
        if not isinstance(
            self.finalization,
            (FinalizedSecondaryDomainEvidence, NoSecondaryDomainCompletionEvidence, PrimaryExcludedDomainCompletionEvidence),
        ):
            raise SpatialHandoffError("domain finalization discriminator is invalid")
        if self.primary_domain_status not in {
            "active", "excluded_below_minimum", "excluded_by_primary_count_review"
        }:
            raise SpatialHandoffError("primary domain disposition is invalid")
        # Primary exclusion controls the complete domain. Secondary decisions
        # retain their own count/subset semantics and cannot reactivate it.
        expected_status = (
            self.primary_domain_status
            if self.primary_domain_status != "active"
            else self.finalization.domain_status
        )
        if self.domain_status != expected_status:
            raise SpatialHandoffError("domain handoff lost its primary or secondary disposition")
        if self.primary_domain_status != "active" and any(
                item.count or item.cell_ids_sha256 != stable_id_inventory_sha256(())
                for item in inventories):
            raise SpatialHandoffError("primary-excluded domain contains active cell evidence")
        expected_scope = {
            "analysis_domain_id": self.member.analysis_domain_id,
            "analysis_domain_revision_sha256": self.member.analysis_domain_revision_sha256,
            "domain_set_sha256": self.segmentation_domain_set_sha256,
            "spatial_frame_sha256": self.spatial_frame_sha256,
            "section_id": self.member.section_id,
            "tissue_generation_id": self.member.tissue_generation_id,
        }
        if any(
            getattr(self.finalization, key) != expected
            for key, expected in expected_scope.items()
            if hasattr(self.finalization, key)
        ):
            raise SpatialHandoffError("domain finalization scope is stale")
        if excluded_section:
            decision_domain = _plain_json(self.finalization.primary_count_decision["domain"])
            expected_domain = {"analysis_domain_id": self.member.analysis_domain_id,
                "kind": self.member.analysis_domain_kind.value, "slide_id": self.member.slide_id,
                "section_id": self.member.section_id,
                "tissue_generation_id": self.member.tissue_generation_id}
            if decision_domain != expected_domain or self.finalization.domain_status != self.primary_domain_status:
                raise SpatialHandoffError("primary exclusion completion has stale domain scope")
        elif isinstance(self.finalization, NoSecondaryDomainCompletionEvidence):
            if self.finalization.domain_status != self.primary_domain_status:
                raise SpatialHandoffError("no-secondary completion lost its primary disposition")
            if self.finalization.raw_input_sha256 != self.semantic_raw_input_sha256:
                raise SpatialHandoffError(
                    "domain no-secondary semantic raw input identity is stale"
                )
            if self.finalization.analysis_config_sha256 != self.analysis_config_sha256:
                raise SpatialHandoffError("domain no-secondary analysis config identity is stale")
        else:
            receipt = self.finalization.finalization_receipt
            decision_domain = self.finalization.count_decision.get("domain")
            expected_decision_domain = {
                "analysis_domain_id": self.member.analysis_domain_id,
                "kind": self.member.analysis_domain_kind.value,
                "slide_id": self.member.slide_id,
                "section_id": self.member.section_id,
                "tissue_generation_id": self.member.tissue_generation_id,
            }
            if _plain_json(decision_domain) != expected_decision_domain:
                raise SpatialHandoffError(
                    "count decision domain conflicts with domain-set evidence"
                )
            receipt_scope = {
                "analysis_domain_id": self.member.analysis_domain_id,
                "section_id": self.member.section_id,
                "tissue_generation_id": self.member.tissue_generation_id,
            }
            if any(receipt.get(key) != value for key, value in receipt_scope.items()):
                raise SpatialHandoffError("semantic finalization receipt scope is stale")
            if (
                receipt.get("reconciled_cell_ids_sha256") != self.exported_inventory.cell_ids_sha256
                or receipt.get("membership_cell_count") != self.exported_inventory.count
            ):
                raise SpatialHandoffError(
                    "semantic finalization reconciled cell IDs differ from domain inventories"
                )
            lineage_configs = {
                str(lineage["config_sha256"])
                for output in self.finalization.count_decision["outputs"]
                for lineage in (
                    output.get("selection_lineage"),
                    *(item.get("selection_lineage") for item in output["inventory"]),
                )
                if isinstance(lineage, Mapping)
            }
            if lineage_configs and lineage_configs != {self.analysis_config_sha256}:
                raise SpatialHandoffError("count/manual selection config lineage is stale")
        if self.domain_result_sha256 != path_free_identity_sha256(self.identity_mapping()):
            raise SpatialHandoffError("domain result SHA-256 does not match")

    @classmethod
    def create(cls, **values: object) -> "DomainHandoffEvidence":
        identity = _domain_handoff_identity(values)
        return cls(
            **values,
            domain_result_sha256=path_free_identity_sha256(identity),
        )

    def identity_mapping(self) -> dict[str, object]:
        return _domain_handoff_identity(
            {
                "member": self.member,
                "spatial_frame_sha256": self.spatial_frame_sha256,
                "segmentation_population_id": self.segmentation_population_id,
                "segmentation_canonical_image_id": self.segmentation_canonical_image_id,
                "segmentation_canonical_image_sha256": self.segmentation_canonical_image_sha256,
                "segmentation_domain_set_sha256": self.segmentation_domain_set_sha256,
                "segmentation_section_population_sha256": self.segmentation_section_population_sha256,
                "measurement_rows_sha256": self.measurement_rows_sha256,
                "segmentation_domain_sha256": self.segmentation_domain_sha256,
                "segmentation_domain_cell_mapping_sha256": self.segmentation_domain_cell_mapping_sha256,
                "selected_phenotype_view": self.selected_phenotype_view,
                "phenotype_generation_id": self.phenotype_generation_id,
                "phenotype_inventory_sha256": self.phenotype_inventory_sha256,
                "phenotype_section_artifact_sha256": self.phenotype_section_artifact_sha256,
                "phenotype_section_lineage_sha256": self.phenotype_section_lineage_sha256,
                "phenotype_source_run_id": self.phenotype_source_run_id,
                "analysis_config_sha256": self.analysis_config_sha256,
                "semantic_raw_input_sha256": self.semantic_raw_input_sha256,
                "segmented_inventory": self.segmented_inventory,
                "phenotyped_inventory": self.phenotyped_inventory,
                "exported_inventory": self.exported_inventory,
                "finalization": self.finalization,
                "primary_domain_status": self.primary_domain_status,
                "domain_status": self.domain_status,
            }
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_mapping(),
            "finalization": self.finalization.to_mapping(),
            "domain_result_sha256": self.domain_result_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "DomainHandoffEvidence":
        """Decode the serialized fields into a validated DomainHandoffEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            {
                "schema_version",
                "member",
                "spatial_frame_sha256",
                "segmentation_population_id",
                "segmentation_canonical_image_id",
                "segmentation_canonical_image_sha256",
                "segmentation_domain_set_sha256",
                "segmentation_section_population_sha256",
                "measurement_rows_sha256",
                "segmentation_domain_sha256",
                "segmentation_domain_cell_mapping_sha256",
                "selected_phenotype_view",
                "phenotype_generation_id",
                "phenotype_inventory_sha256",
                "phenotype_section_artifact_sha256",
                "phenotype_section_lineage_sha256",
                "phenotype_source_run_id",
                "analysis_config_sha256",
                "semantic_raw_input_sha256",
                "segmented_inventory",
                "phenotyped_inventory",
                "exported_inventory",
                "finalization",
                "primary_domain_status",
                "domain_status",
                "domain_result_sha256",
            },
            "domain handoff evidence",
        )
        finalization_mapping = mapping["finalization"]
        if not isinstance(finalization_mapping, Mapping):
            raise SpatialHandoffError("domain finalization evidence is invalid")
        kind = finalization_mapping.get("kind")
        if kind == "secondary_finalized":
            finalization: DomainFinalizationEvidence = (
                FinalizedSecondaryDomainEvidence.from_mapping(finalization_mapping)
            )
        elif kind == "primary_excluded_section":
            finalization = PrimaryExcludedDomainCompletionEvidence.from_mapping(finalization_mapping)
        elif kind == "no_secondary_configured":
            finalization = NoSecondaryDomainCompletionEvidence.from_mapping(finalization_mapping)
        else:
            raise SpatialHandoffError("domain finalization discriminator is invalid")
        return cls(
            **{
                **mapping,
                "member": DomainSetMemberEvidence.from_mapping(mapping["member"]),
                "segmented_inventory": DomainCellInventoryEvidence.from_mapping(
                    mapping["segmented_inventory"]
                ),
                "phenotyped_inventory": (None if mapping["phenotyped_inventory"] is None
                    else DomainCellInventoryEvidence.from_mapping(mapping["phenotyped_inventory"])),
                "exported_inventory": DomainCellInventoryEvidence.from_mapping(
                    mapping["exported_inventory"]
                ),
                "finalization": finalization,
            }
        )


def _domain_handoff_identity(values: Mapping[str, object]) -> dict[str, object]:
    member = values["member"]
    segmented = values["segmented_inventory"]
    phenotyped = values["phenotyped_inventory"]
    exported = values["exported_inventory"]
    finalization = values["finalization"]
    if (
        not isinstance(member, DomainSetMemberEvidence)
        or not all(
            isinstance(item, DomainCellInventoryEvidence)
            for item in (segmented, exported)
        )
        or (phenotyped is not None and not isinstance(phenotyped, DomainCellInventoryEvidence))
        or not isinstance(
            finalization,
            (FinalizedSecondaryDomainEvidence, NoSecondaryDomainCompletionEvidence, PrimaryExcludedDomainCompletionEvidence),
        )
    ):
        raise SpatialHandoffError("domain handoff identity contains invalid typed evidence")
    return {
        "schema_version": "domain-handoff-evidence/v1",
        "member": member.to_mapping(),
        "spatial_frame_sha256": values["spatial_frame_sha256"],
        "segmentation_population_id": values["segmentation_population_id"],
        "segmentation_canonical_image_id": values["segmentation_canonical_image_id"],
        "segmentation_canonical_image_sha256": values["segmentation_canonical_image_sha256"],
        "segmentation_domain_set_sha256": values["segmentation_domain_set_sha256"],
        "segmentation_section_population_sha256": values["segmentation_section_population_sha256"],
        "measurement_rows_sha256": values["measurement_rows_sha256"],
        "segmentation_domain_sha256": values["segmentation_domain_sha256"],
        "segmentation_domain_cell_mapping_sha256": values[
            "segmentation_domain_cell_mapping_sha256"
        ],
        "selected_phenotype_view": values["selected_phenotype_view"],
        "phenotype_generation_id": values["phenotype_generation_id"],
        "phenotype_inventory_sha256": values["phenotype_inventory_sha256"],
        "phenotype_section_artifact_sha256": values["phenotype_section_artifact_sha256"],
        "phenotype_section_lineage_sha256": values["phenotype_section_lineage_sha256"],
        "phenotype_source_run_id": values["phenotype_source_run_id"],
        "analysis_config_sha256": values["analysis_config_sha256"],
        "semantic_raw_input_sha256": values["semantic_raw_input_sha256"],
        "segmented_inventory": segmented.to_mapping(),
        "phenotyped_inventory": None if phenotyped is None else phenotyped.to_mapping(),
        "exported_inventory": exported.to_mapping(),
        "finalization": finalization.identity_mapping(),
        "primary_domain_status": values["primary_domain_status"],
        "domain_status": values["domain_status"],
    }


@dataclass(frozen=True, slots=True)
class FinalROIState:
    """Checksummed current-state evidence for one biological ROI."""

    record: ROIRecord
    generation_id: str
    pipeline_run_id: str
    pipeline_stage: str
    classifier_status: str | None = None
    candidate_status: str | None = None
    path_object_id: str | None = None
    structure_id: str | None = None
    geometry_sha256: str | None = None
    content_key: str | None = None

    def __post_init__(self) -> None:
        """Validate the FinalROIState fields when the record is constructed.

        The checks cover ``generation_id``, ``pipeline_run_id``, ``pipeline_stage``,
        ``path_object_id``, ``geometry_sha256``, ``content_key``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if not isinstance(self.record, ROIRecord):
            raise SpatialHandoffError("final ROI evidence requires an ROIRecord")
        _nonblank(self.generation_id, "ROI generation_id")
        _nonblank(self.pipeline_run_id, "ROI pipeline_run_id")
        if self.pipeline_stage not in {"primary_classifier", "secondary_classifier"}:
            raise SpatialHandoffError("final ROI pipeline_stage is invalid")
        expected = "primary" if self.pipeline_stage == "primary_classifier" else "secondary"
        if self.record.roi_level != expected:
            raise SpatialHandoffError("final ROI stage and level conflict")
        if self.pipeline_stage == "secondary_classifier" and self.candidate_status not in {
            "candidate",
            "other",
        }:
            raise SpatialHandoffError(
                "final secondary ROI candidate_status must be candidate or other"
            )
        if self.path_object_id is not None:
            _nonblank(self.path_object_id, "ROI path_object_id")
        if self.geometry_sha256 is not None:
            _sha256(self.geometry_sha256, "ROI geometry SHA-256")
        if self.content_key is not None:
            _sha256(self.content_key, "ROI content_key")
        if self.structure_id is not None:
            _nonblank(self.structure_id, "ROI structure_id")
        if self.pipeline_stage == "secondary_classifier" and any(
            value is None
            for value in (
                self.path_object_id,
                self.structure_id,
                self.geometry_sha256,
                self.content_key,
            )
        ):
            raise SpatialHandoffError(
                "final secondary ROI identity requires path, structure, geometry, and content"
            )

    @property
    def is_final_accepted(self) -> bool:
        if self.record.review_status is not ReviewStatus.ACCEPTED:
            return False
        if self.pipeline_stage == "primary_classifier":
            return self.classifier_status == "accepted"
        return (
            self.classifier_status == "accepted"
            and self.candidate_status == "candidate"
            and self.record.semantic_status is SemanticStatus.ACCEPTED
        )


@dataclass(frozen=True, slots=True)
class TissueCellGroup:
    """Our accepted tissue navigation scope over existing cell datasets."""

    tissue_roi_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    tissue_review_status: str
    cell_dataset: CellDatasetCollection

    def __post_init__(self):
        """Admit our tissue identity and a compact selected-domain reader."""
        for field in ('tissue_roi_id','slide_id','section_id','tissue_generation_id'):
            _nonblank(getattr(self,field),field)
        if self.tissue_review_status!='accepted' or not isinstance(self.cell_dataset,CellDatasetCollection):
            raise SpatialHandoffError('Tissue navigation requires an accepted current dataset scope')

    @property
    def cell_count(self):
        return self.cell_dataset.row_count


@dataclass(frozen=True, slots=True)
class AnalysisDomainCellGroup:
    """Our authoritative domain scope without a second population index."""

    analysis_domain_id: str
    domain_result_sha256: str
    cell_dataset: CellDatasetCollection

    def __post_init__(self):
        """Bind our exact active-domain reader to its completed result."""
        _nonblank(self.analysis_domain_id,'analysis_domain_id')
        _sha256(self.domain_result_sha256,'domain result SHA-256')
        if not isinstance(self.cell_dataset,CellDatasetCollection) or self.cell_dataset.active_domains!=[self.analysis_domain_id]:
            raise SpatialHandoffError('A domain group requires its exact current dataset scope')

    @property
    def cell_count(self):
        return self.cell_dataset.row_count


@dataclass(frozen=True, slots=True)
class SpatialCellHandoff:
    """Our complete accepted metadata and lazy canonical cell selection."""

    spatial_frame: CanonicalSpatialFrameEvidence
    domain_set: DomainSetEvidence
    domain_results: Mapping[str, DomainHandoffEvidence]
    by_domain: Mapping[str, AnalysisDomainCellGroup]
    by_tissue: Mapping[str, TissueCellGroup]
    cell_dataset: CellDatasetCollection
    final_roi_states: Mapping[str, FinalROIState]
    selected_phenotype_view: str | None
    phenotype_generation_id: str | None
    phenotype_inventory_sha256: str | None
    cell_selection_sha256: str | None = None

    def __post_init__(self):
        """Bind our completed dataset selection to scientific scope evidence.

        i) We check frame, complete-domain lineage and chosen phenotype metadata.
        ii) Exact source-domain counts and sorted-ID digests agree with the
        authenticated export inventories. iii) Active groups share the selected
        immutable bases/components; ROI state receives its required geometric
        checks once. Population geometry and numerical values are read only by
        the downstream operation that requests them.
        """
        if not isinstance(self.spatial_frame,CanonicalSpatialFrameEvidence) or not isinstance(self.domain_set,DomainSetEvidence) or not isinstance(self.cell_dataset,CellDatasetCollection):
            raise SpatialHandoffError('A spatial handoff requires current frame/domain/dataset evidence')
        selection_digest=self.cell_dataset.selection_sha256
        if self.cell_selection_sha256 is not None and self.cell_selection_sha256!=selection_digest:
            raise SpatialHandoffError('Completed cell selection identity differs from its handoff')
        object.__setattr__(self,'cell_selection_sha256',selection_digest)
        results=_typed_mapping(self.domain_results,DomainHandoffEvidence,'domain_results')
        section_routes = {}
        for result in results.values():
            excluded = isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence)
            prior = section_routes.setdefault(result.member.section_id, excluded)
            if prior != excluded:
                raise SpatialHandoffError('A section cannot mix performed and bypassed cell analysis')
        analyzed = [result for result in results.values()
                    if not isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence)]
        if analyzed:
            _nonblank(self.selected_phenotype_view, 'selected_phenotype_view')
            _nonblank(self.phenotype_generation_id, 'phenotype_generation_id')
            _sha256(self.phenotype_inventory_sha256, 'phenotype inventory SHA-256')
        elif any(value is not None for value in (self.selected_phenotype_view,
                self.phenotype_generation_id, self.phenotype_inventory_sha256)):
            raise SpatialHandoffError('Excluded sections cannot select a phenotype generation')
        members={item.analysis_domain_id:item for item in self.domain_set.members}
        if set(results)!=set(members):
            raise SpatialHandoffError('Domain results do not cover the complete domain set')
        inventories=self.cell_dataset.source_summary['domain_inventories']
        if set(inventories)!=set(members):
            raise SpatialHandoffError('Source summaries do not cover the complete domain inventory')
        for id,result in results.items():
            if (id!=result.member.analysis_domain_id or result.member!=members[id] or result.spatial_frame_sha256!=self.spatial_frame.spatial_frame_sha256 or result.segmentation_canonical_image_id!=self.spatial_frame.canonical_image_id or result.segmentation_canonical_image_sha256!=self.spatial_frame.canonical_image_sha256 or result.segmentation_domain_set_sha256!=self.domain_set.domain_set_sha256):
                raise SpatialHandoffError('A domain result has stale spatial/domain-set lineage')
            if result in analyzed and (result.selected_phenotype_view!=self.selected_phenotype_view or result.phenotype_generation_id!=self.phenotype_generation_id or result.phenotype_inventory_sha256!=self.phenotype_inventory_sha256):
                raise SpatialHandoffError('A domain result has stale selected phenotype lineage')
            inventory=inventories[id]
            if (inventory['cell_count']!=result.exported_inventory.count or inventory['cell_ids_sha256']!=result.exported_inventory.cell_ids_sha256):
                raise SpatialHandoffError('Source domain membership differs from its authenticated export inventory')
        if len({result.exported_inventory.source_evidence_sha256 for result in results.values()})!=1:
            raise SpatialHandoffError('Domain export inventories require one current scientific snapshot')
        active=sorted(id for id,result in results.items() if result.domain_status=='active')
        if self.cell_dataset.active_domains!=active:
            raise SpatialHandoffError('Canonical cell selection differs from active domains')
        groups=_typed_mapping(self.by_domain,AnalysisDomainCellGroup,'by_domain')
        if set(groups)!=set(active):
            raise SpatialHandoffError('Domain navigation differs from active domains')
        for id,group in groups.items():
            if group.analysis_domain_id!=id or group.domain_result_sha256!=results[id].domain_result_sha256 or group.cell_count!=results[id].exported_inventory.count:
                raise SpatialHandoffError('A domain navigation scope differs from its completed result')
        tissues=_typed_mapping(self.by_tissue,TissueCellGroup,'by_tissue')
        if sum(group.cell_count for group in tissues.values())!=self.cell_count:
            raise SpatialHandoffError('Accepted tissue counts differ from the active-domain union')
        for id,group in tissues.items():
            domain_ids=sorted(domain for domain in active if results[domain].member.tissue_roi_id==id)
            if id!=group.tissue_roi_id or group.cell_dataset.active_domains!=domain_ids:
                raise SpatialHandoffError('A tissue navigation scope differs from accepted domains')
            if any((results[domain].member.slide_id,results[domain].member.section_id,results[domain].member.tissue_generation_id)!=(group.slide_id,group.section_id,group.tissue_generation_id) for domain in domain_ids):
                raise SpatialHandoffError('Accepted tissue navigation has stale source lineage')
        states=_typed_mapping(self.final_roi_states,FinalROIState,'final_roi_states')
        if any(id!=state.record.roi_id for id,state in states.items()):
            raise SpatialHandoffError('Final ROI keys differ from their stable identities')
        _validate_roi_spatial_content(states=states,frame=self.spatial_frame,results=results)
        for field,value in [('domain_results',results),('by_domain',groups),('by_tissue',tissues),('final_roi_states',states)]:
            object.__setattr__(self,field,MappingProxyType(dict(sorted(value.items()))))

    @property
    def cell_count(self):
        return self.cell_dataset.row_count

    def iter_cell_records(self):
        """Decode our full cells only for an operation requesting full geometry.

        We validate each requested record against current domain, phenotype,
        calibration and finalized membership evidence, then release it. The
        completed traversal must match its admitted count; partial failed output
        remains the consuming owner's private unpublished work.
        """
        count=0
        for cell in self.cell_dataset.iter_cell_records():
            _validate_cell_spatial_content(cell=cell,states=self.final_roi_states,frame=self.spatial_frame,results=self.domain_results)
            count+=1
            yield cell
        if count!=self.cell_count:
            raise SpatialHandoffError('Complete scientific traversal differs from its admitted cell count')

    @property
    def secondary_roi_classes(self):
        return MappingProxyType({id:state.record.roi_class for id,state in self.final_roi_states.items() if state.pipeline_stage=='secondary_classifier' and state.is_final_accepted})


def build_spatial_handoff(tissues,cell_dataset,*,spatial_frame,domain_set,domain_results,selected_phenotype_view,phenotype_generation_id,phenotype_inventory_sha256,final_roi_states):
    """Select our accepted domains from completed existing datasets.

    We retain small tissue/ROI/domain evidence and their scoped readers. Exact
    domain membership uses producer count/ID commitments and immutable selected
    descriptor identities; constructing the handoff performs no population read.
    Actual scientific consumers request narrow numerical batches or full cells.
    """
    if not isinstance(cell_dataset,CellDatasetCollection):
        raise SpatialHandoffError('Handoff construction requires a current dataset selection')
    tissues=tuple(tissues);states=tuple(final_roi_states);results=tuple(domain_results)
    _require_instances(tissues,TissueSection,'tissues');_require_instances(states,FinalROIState,'final_roi_states');_require_instances(results,DomainHandoffEvidence,'domain_results')
    result_map={result.member.analysis_domain_id:result for result in results}
    if len(result_map)!=len(results):
        raise SpatialHandoffError('A domain result is duplicated')
    active=sorted(id for id,result in result_map.items() if result.domain_status=='active')
    selected=cell_dataset.select_domains(active)
    groups={id:AnalysisDomainCellGroup(id,result_map[id].domain_result_sha256,selected.select_domains([id])) for id in active}
    accepted={tissue.roi_id:tissue for tissue in tissues if tissue.review_status is ReviewStatus.ACCEPTED}
    if len(accepted)!=sum(tissue.review_status is ReviewStatus.ACCEPTED for tissue in tissues):
        raise SpatialHandoffError('An accepted tissue is duplicated')
    tissue_groups={id:TissueCellGroup(id,tissue.slide_id,tissue.section_id,tissue.generation_id,'accepted',selected.select_domains([domain for domain in active if result_map[domain].member.tissue_roi_id==id])) for id,tissue in accepted.items()}
    state_map={state.record.roi_id:state for state in states}
    if len(state_map)!=len(states):
        raise SpatialHandoffError('A final ROI state is duplicated')
    return SpatialCellHandoff(spatial_frame,domain_set,result_map,groups,tissue_groups,selected,state_map,selected_phenotype_view,phenotype_generation_id,phenotype_inventory_sha256)


def _validate_cell_spatial_content(*,cell,states,frame,results):
    """Check our requested full cell against its exact scientific scope."""
    result = results.get(cell.analysis_domain_id)
    if result is None:
        raise SpatialHandoffError(f"cell {cell.cell_id} has no authenticated domain")
    member = result.member
    if (
        cell.analysis_domain_kind is not member.analysis_domain_kind
        or cell.slide_id != member.slide_id
        or cell.section_id != member.section_id
        or cell.tissue_roi_id != member.tissue_roi_id
        or cell.tissue_generation_id != member.tissue_generation_id
        or cell.phenotyping_generation_id != result.phenotype_generation_id
        or not _is_nonblank(cell.primary_phenotype)
        or not _is_nonblank(cell.phenotype_source)
    ):
        raise SpatialHandoffError(
            f"canonical cell {cell.cell_id} domain/phenotype scope is stale"
        )
    x = _finite(cell.x_level0, f"cell {cell.cell_id} x_level0")
    y = _finite(cell.y_level0, f"cell {cell.cell_id} y_level0")
    if not (0.0 <= x < frame.level0_size_x_px and 0.0 <= y < frame.level0_size_y_px):
        raise SpatialHandoffError(f"cell {cell.cell_id} centroid is outside level 0")
    if (cell.x_um is None) != (cell.y_um is None):
        raise SpatialHandoffError(f"cell {cell.cell_id} physical coordinates are incomplete")
    if cell.x_um is not None and cell.y_um is not None:
        x_um = _finite(cell.x_um, f"cell {cell.cell_id} x_um")
        y_um = _finite(cell.y_um, f"cell {cell.cell_id} y_um")
        if not math.isclose(
            x_um,
            x * frame.x_um_per_px,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ) or not math.isclose(
            y_um,
            y * frame.y_um_per_px,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise SpatialHandoffError(
                f"cell {cell.cell_id} physical coordinates conflict with calibration"
            )
    # We retain a missing native nucleus as unavailable geometry. Any present
    # nucleus still receives the same exact polygon and level-zero bounds checks.
    if cell.nucleus_geometry:
        _validate_geometry_bounds(
            cell.nucleus_geometry,
            frame,
            f"cell {cell.cell_id} nucleus geometry",
        )
    _validate_geometry_bounds(
        cell.cell_geometry, frame, f"cell {cell.cell_id} geometry",
    )
    if cell.secondary_roi_ids != tuple(sorted(set(cell.secondary_roi_ids))):
        raise SpatialHandoffError(f"cell {cell.cell_id} secondary memberships are invalid")
    if isinstance(result.finalization, NoSecondaryDomainCompletionEvidence):
        if cell.secondary_roi_ids:
            raise SpatialHandoffError(
                f"cell {cell.cell_id} conflicts with no-secondary completion"
            )
        if cell.membership_run_id is not None or cell.membership_generation_id is not None:
            raise SpatialHandoffError(
                f"cell {cell.cell_id} no-secondary membership must remain unfinalized"
            )
    else:
        receipt = result.finalization.finalization_receipt
        if (
            cell.membership_run_id != receipt["pipeline_run_id"]
            or cell.membership_generation_id != receipt["tissue_generation_id"]
        ):
            raise SpatialHandoffError(f"cell {cell.cell_id} semantic membership scope is stale")
        active_roi_ids = {
            item.roi_id
            for item in result.finalization.dispositions
            if item.disposition == "active_included"
        }
        if not set(cell.secondary_roi_ids) <= active_roi_ids:
            raise SpatialHandoffError(
                f"cell {cell.cell_id} includes capped or audit-only membership"
            )
        for roi_id in cell.secondary_roi_ids:
            if not _valid_secondary_parent_chain(
                cell=cell,
                roi_id=roi_id,
                analysis_domain_id=member.analysis_domain_id,
                active_secondary_roi_ids=active_roi_ids,
                states=states,
            ):
                raise SpatialHandoffError(
                    f"cell {cell.cell_id} secondary ROI membership is stale"
                )


def _validate_roi_spatial_content(*,states,frame,results):
    """Check our final ROI geometry and accepted dispositions once per handoff."""
    for state in states.values():
        _validate_geometry_bounds(
            state.record.geometry,
            frame,
            f"final ROI {state.record.roi_id} geometry",
        )
        if state.record.original_predicted_geometry is not None:
            _validate_geometry_bounds(
                state.record.original_predicted_geometry,
                frame,
                f"final ROI {state.record.roi_id} original geometry",
            )
        result = results.get(state.record.analysis_domain_id)
        if result is None:
            raise SpatialHandoffError("final ROI state references an unknown domain")
        expected_run_id = (
            result.finalization.pipeline_run_id
            if isinstance(result.finalization, (NoSecondaryDomainCompletionEvidence,
                                                PrimaryExcludedDomainCompletionEvidence))
            else result.finalization.finalization_receipt["pipeline_run_id"]
        )
        if (
            state.record.analysis_domain_kind is not result.member.analysis_domain_kind
            or state.record.slide_id != result.member.slide_id
            or state.record.section_id != result.member.section_id
            or state.generation_id != result.member.tissue_generation_id
            or state.pipeline_run_id != expected_run_id
        ):
            raise SpatialHandoffError("final ROI state has stale domain/generation/run scope")
        if (
            isinstance(result.finalization, (NoSecondaryDomainCompletionEvidence,
                                             PrimaryExcludedDomainCompletionEvidence))
            and state.pipeline_stage == "secondary_classifier"
        ):
            raise SpatialHandoffError(
                "no-secondary domain cannot contain a final secondary ROI state"
            )
        disposition: FinalObjectDispositionEvidence | None = None
        if (
            isinstance(result.finalization, FinalizedSecondaryDomainEvidence)
            and state.pipeline_stage == "secondary_classifier"
        ):
            assert state.path_object_id is not None
            assert state.structure_id is not None
            assert state.geometry_sha256 is not None
            assert state.content_key is not None
            parent_content_key = semantic_parent_content_key(
                analysis_domain_id=result.member.analysis_domain_id,
                parent_roi_id=state.record.parent_roi_id or "",
            )
            expected_content_key = semantic_secondary_content_key(
                analysis_domain_id=result.member.analysis_domain_id,
                roi_id=state.record.roi_id,
                path_object_id=state.path_object_id,
                structure_id=state.structure_id,
                parent_content_key=parent_content_key,
                geometry_sha256=state.geometry_sha256,
            )
            if state.content_key != expected_content_key:
                raise SpatialHandoffError("final secondary ROI content identity is stale")
            disposition = next(
                (
                    item
                    for item in result.finalization.dispositions
                    if item.roi_id == state.record.roi_id
                ),
                None,
            )
            accepted_for_count = (
                state.record.semantic_status is SemanticStatus.ACCEPTED
                and state.record.review_status is ReviewStatus.ACCEPTED
            )
            if disposition is None and accepted_for_count:
                raise SpatialHandoffError(
                    "accepted final secondary ROI state has no disposition evidence"
                )
            if disposition is not None and (
                state.path_object_id != disposition.path_object_id
                or parent_content_key != disposition.parent_content_key
            ):
                raise SpatialHandoffError(
                    "final secondary ROI source identity conflicts with disposition"
                )
        if not _valid_roi_parent_chain(
            state=state,
            analysis_domain_id=result.member.analysis_domain_id,
            states=states,
            require_final_accepted_ancestors=(
                state.pipeline_stage == "secondary_classifier"
                and (state.is_final_accepted or disposition is not None)
            ),
        ):
            raise SpatialHandoffError(f"final ROI {state.record.roi_id} parent chain is stale")
    secondary_path_ids = tuple(
        state.path_object_id
        for state in states.values()
        if state.pipeline_stage == "secondary_classifier"
    )
    if len(set(secondary_path_ids)) != len(secondary_path_ids):
        raise SpatialHandoffError("final secondary ROI path-object IDs must be unique")
    for result in results.values():
        if not isinstance(result.finalization, FinalizedSecondaryDomainEvidence):
            continue
        dispositions_by_roi = {item.roi_id: item for item in result.finalization.dispositions}
        for roi_id, disposition in dispositions_by_roi.items():
            state = states.get(roi_id)
            if state is None:
                raise SpatialHandoffError("final disposition has no current final ROI state")
            if (
                state.pipeline_stage != "secondary_classifier"
                or state.record.analysis_domain_id != result.member.analysis_domain_id
                or state.record.analysis_domain_kind is not result.member.analysis_domain_kind
                or state.record.slide_id != result.member.slide_id
                or state.record.section_id != result.member.section_id
                or state.generation_id != result.member.tissue_generation_id
                or state.pipeline_run_id
                != result.finalization.finalization_receipt["pipeline_run_id"]
                or state.content_key != disposition.content_key
                or state.path_object_id != disposition.path_object_id
                or state.structure_id != disposition.structure_id
                or state.geometry_sha256 != disposition.geometry_sha256
            ):
                raise SpatialHandoffError("final ROI state conflicts with its disposition evidence")
            if not state.is_final_accepted:
                raise SpatialHandoffError("count disposition requires accepted final ROI state")


def _valid_roi_parent_chain(
    *,
    state: FinalROIState,
    analysis_domain_id: str,
    states: Mapping[str, FinalROIState],
    require_final_accepted_ancestors: bool = False,
) -> bool:
    """Validate stored ancestry and, when required, accepted parent state."""
    seen: set[str] = set()
    cursor = state
    while True:
        roi_id = cursor.record.roi_id
        if roi_id in seen:
            return False
        seen.add(roi_id)
        parent_id = cursor.record.parent_roi_id
        if not _is_nonblank(parent_id):
            return False
        if parent_id == analysis_domain_id:
            return True
        if cursor.pipeline_stage == "primary_classifier":
            return False
        parent = states.get(parent_id)
        if parent is None:
            return False
        if (
            parent.record.analysis_domain_id != analysis_domain_id
            or parent.record.analysis_domain_kind is not cursor.record.analysis_domain_kind
            or parent.record.slide_id != cursor.record.slide_id
            or parent.record.section_id != cursor.record.section_id
            or parent.generation_id != cursor.generation_id
            or parent.pipeline_run_id != cursor.pipeline_run_id
            or (require_final_accepted_ancestors and not parent.is_final_accepted)
        ):
            return False
        cursor = parent


def _valid_secondary_parent_chain(
    *,
    cell: CellRecord,
    roi_id: str,
    analysis_domain_id: str,
    active_secondary_roi_ids: set[str],
    states: Mapping[str, FinalROIState],
) -> bool:
    """Validate accepted ROI ancestry to the authenticated domain root."""
    seen: set[str] = set()
    cursor = roi_id
    first = True
    while cursor != analysis_domain_id:
        if cursor in seen:
            return False
        seen.add(cursor)
        state = states.get(cursor)
        if state is None or not state.is_final_accepted:
            return False
        record = state.record
        if (
            record.analysis_domain_id != cell.analysis_domain_id
            or record.analysis_domain_kind is not cell.analysis_domain_kind
            or record.slide_id != cell.slide_id
            or record.section_id != cell.section_id
            or state.generation_id != cell.membership_generation_id
            or state.pipeline_run_id != cell.membership_run_id
        ):
            return False
        if first and state.pipeline_stage != "secondary_classifier":
            return False
        if (
            state.pipeline_stage == "secondary_classifier"
            and cursor not in active_secondary_roi_ids
        ):
            return False
        if not first and state.pipeline_stage == "secondary_classifier":
            # Nested secondary structures are permitted; their accepted parent
            # remains part of the same checked chain.
            pass
        parent_id = record.parent_roi_id
        if not _is_nonblank(parent_id):
            return False
        cursor = parent_id
        first = False
    return True


def _validate_geometry_bounds(
    geometry: Mapping[str, Any],
    frame: CanonicalSpatialFrameEvidence,
    field: str,
) -> None:
    """Check finite coordinates and bounds against the accepted image frame.

    Native cell finalization and ROI export validate the current geometries.
    We retain their polygon semantics, including valid point contacts, and
    check their placement in this image. Historical predicted outlines receive
    the same coordinate checks without changing their saved shape.
    """
    if not isinstance(geometry, Mapping) or set(geometry) != {
        "type",
        "coordinates",
    }:
        raise SpatialHandoffError(f"{field} is not a closed polygonal geometry")
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise SpatialHandoffError(f"{field} must be Polygon or MultiPolygon")
    points: list[tuple[float, float]] = []

    def visit(value: object) -> None:
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        ):
            points.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item)
            return
        raise SpatialHandoffError(f"{field} coordinates are invalid")

    visit(geometry["coordinates"])
    if not points:
        raise SpatialHandoffError(f"{field} has no coordinates")
    if any(
        not math.isfinite(x)
        or not math.isfinite(y)
        or not (0.0 <= x <= frame.level0_size_x_px)
        or not (0.0 <= y <= frame.level0_size_y_px)
        for x, y in points
    ):
        raise SpatialHandoffError(f"{field} is outside the canonical level-0 frame")


def _cell_matches_tissue(cell: CellRecord, tissue: TissueSection) -> bool:
    return (
        cell.slide_id == tissue.slide_id
        and cell.section_id == tissue.section_id
        and cell.tissue_roi_id == tissue.roi_id
        and cell.tissue_generation_id == tissue.generation_id
    )


def _validate_count_decision(
    value: object,
    *,
    expected_domain_id: str,
    expected_revision_sha256: str,
    expected_result_sha256: str,
    expected_domain_status: str,
) -> dict[str, object]:
    """Validate the complete public count-decision wire contract structurally.

    The required mapping shape includes ``analysis_domain_sha256``, ``config_sha256``,
    ``result_sha256``, ``inventory_sha256``, ``structure_id``, ``algorithm``. Field checks
    are applied before the validated value is returned or passed to its next constructor.

    The handoff is a downstream evidence consumer, so it validates the durable mapping
    rather than importing the compartment algorithm package that owns construction of the
    decision."""
    mapping = _closed_mapping(
        value,
        {
            "schema_version",
            "domain",
            "outputs",
            "domain_exclusion",
            "component_selection_action",
            "decision_sha256",
        },
        "structure count decision",
    )
    if mapping.get("schema_version") != "structure-count-decision/v1":
        raise SpatialHandoffError("structure count decision schema is invalid")
    decision_sha256 = mapping.get("decision_sha256")
    _sha256(decision_sha256, "count decision SHA-256")
    unsigned = {key: _plain_json(item) for key, item in mapping.items() if key != "decision_sha256"}
    if decision_sha256 != canonical_json_sha256(unsigned):
        raise SpatialHandoffError("count decision SHA-256 does not match")

    domain_mapping = _closed_mapping(
        mapping.get("domain"),
        {
            "analysis_domain_id",
            "kind",
            "slide_id",
            "section_id",
            "tissue_generation_id",
        },
        "count decision domain",
    )
    for field in (
        "analysis_domain_id",
        "slide_id",
        "section_id",
        "tissue_generation_id",
    ):
        _nonblank(domain_mapping.get(field), f"count decision domain {field}")
    try:
        AnalysisDomainKind(domain_mapping.get("kind"))
    except (TypeError, ValueError) as exc:
        raise SpatialHandoffError("count decision domain kind is invalid") from exc
    if domain_mapping["analysis_domain_id"] != expected_domain_id:
        raise SpatialHandoffError("count decision domain is stale")

    # The count owner applies domain-wide minimum exclusion before evaluating
    # maxima.  Every output therefore carries audit-only inclusion semantics
    # whenever this aggregate state is present, including non-failing outputs.
    has_domain_exclusion = mapping.get("domain_exclusion") is not None

    lineage_by_structure: dict[str, Mapping[str, object]] = {}

    def parse_lineage(raw: object, *, label: str) -> Mapping[str, object] | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise SpatialHandoffError(f"{label} selection lineage is invalid")
        algorithm = raw.get("algorithm")
        if algorithm == "sha256-rank/v1":
            fields = {
                "analysis_domain_sha256",
                "config_sha256",
                "result_sha256",
                "inventory_sha256",
                "structure_id",
                "algorithm",
                "sampling_seed",
            }
        elif algorithm == "manual-select/v1":
            fields = {
                "analysis_domain_sha256",
                "config_sha256",
                "result_sha256",
                "inventory_sha256",
                "structure_id",
                "algorithm",
                "selected_content_keys",
                "selection_criterion",
                "reviewer_id",
                "rationale",
            }
        else:
            raise SpatialHandoffError(f"{label} selection algorithm is invalid")
        parsed = dict(_closed_mapping(raw, fields, f"{label} selection lineage"))
        for field in (
            "analysis_domain_sha256",
            "config_sha256",
            "result_sha256",
            "inventory_sha256",
        ):
            _sha256(parsed.get(field), f"{label} {field}")
        _nonblank(parsed.get("structure_id"), f"{label} structure_id")
        if algorithm == "sha256-rank/v1":
            if type(parsed.get("sampling_seed")) is not int:
                raise SpatialHandoffError(f"{label} deterministic sampling_seed is invalid")
        else:
            selected = tuple(
                _sequence(
                    parsed.get("selected_content_keys"),
                    f"{label} selected content keys",
                )
            )
            if not selected or len(set(selected)) != len(selected):
                raise SpatialHandoffError(f"{label} manual selected content keys are invalid")
            for key in selected:
                _sha256(key, f"{label} manual selected content key")
            for field in ("selection_criterion", "reviewer_id", "rationale"):
                _nonblank(parsed.get(field), f"{label} {field}")
        if (
            parsed["analysis_domain_sha256"] != expected_revision_sha256
            or parsed["result_sha256"] != expected_result_sha256
        ):
            raise SpatialHandoffError(f"{label} selection lineage is stale")
        return parsed

    output_values = _sequence(mapping.get("outputs"), "count decision outputs")
    if not output_values:
        raise SpatialHandoffError("applicable count decision has no outputs")
    outputs: list[Mapping[str, object]] = []
    all_content_keys: set[str] = set()
    for raw_output in output_values:
        output_mapping = _closed_mapping(
            raw_output,
            {
                "output_id",
                "status",
                "observed_count",
                "minimum",
                "maximum",
                "inventory_sha256",
                "inventory",
                "included_content_keys",
                "not_selected_content_keys",
                "inclusion_fraction",
                "inclusion_probability_status",
                "inclusion_probability",
                "selection_lineage",
            },
            "count decision output",
        )
        output_lineage = parse_lineage(output_mapping["selection_lineage"], label="output")
        inventory_values = _sequence(output_mapping["inventory"], "count decision inventory")
        output_id = _nonblank(output_mapping.get("output_id"), "count decision output_id")
        status = output_mapping.get("status")
        if status not in {
            "within_bounds",
            "below_minimum",
            "capped",
        }:
            raise SpatialHandoffError("count decision output status is invalid")
        observed_count = _nonnegative_int(
            output_mapping.get("observed_count"), "count decision observed_count"
        )
        minimum = _nonnegative_int(output_mapping.get("minimum"), "count decision minimum")
        maximum = output_mapping.get("maximum")
        if maximum is not None:
            maximum = _positive_int(maximum, "count decision maximum")
            if maximum < minimum:
                raise SpatialHandoffError("count decision maximum is below its minimum")
        _sha256(
            output_mapping.get("inventory_sha256"),
            "count decision inventory SHA-256",
        )
        inventory: list[Mapping[str, object]] = []
        for raw_item in inventory_values:
            item_mapping = _closed_mapping(
                raw_item,
                {
                    "content_key",
                    "structure_id",
                    "parent_content_key",
                    "geometry_sha256",
                    "disposition",
                    "observed_count",
                    "inclusion_fraction",
                    "inventory_sha256",
                    "inclusion_probability_status",
                    "inclusion_probability",
                    "selection_lineage",
                },
                "count decision inventory item",
            )
            item_lineage = parse_lineage(item_mapping["selection_lineage"], label="inventory item")
            content_key = item_mapping.get("content_key")
            _sha256(content_key, "count decision content_key")
            _nonblank(item_mapping.get("structure_id"), "count decision structure_id")
            _sha256(
                item_mapping.get("parent_content_key"),
                "count decision parent_content_key",
            )
            _sha256(
                item_mapping.get("geometry_sha256"),
                "count decision geometry SHA-256",
            )
            disposition = item_mapping.get("disposition")
            if disposition not in {
                "active_included",
                "valid_not_selected_by_cap",
                "audit_excluded_below_minimum",
            }:
                raise SpatialHandoffError("count decision inventory disposition is invalid")
            if content_key in all_content_keys:
                raise SpatialHandoffError("count decision content keys must be globally unique")
            all_content_keys.add(str(content_key))
            item = dict(item_mapping)
            item["selection_lineage"] = item_lineage
            inventory.append(item)
        inventory_keys = tuple(str(item["content_key"]) for item in inventory)
        if inventory_keys != tuple(sorted(inventory_keys)):
            raise SpatialHandoffError(
                "count decision inventory must follow canonical content-key order"
            )
        if len(inventory) != observed_count or any(
            item["structure_id"] != output_id for item in inventory
        ):
            raise SpatialHandoffError("count decision observed inventory identity is inconsistent")
        expected_inventory_sha256 = canonical_json_sha256(
            {
                "items": [
                    {
                        "content_key": item["content_key"],
                        "geometry_sha256": item["geometry_sha256"],
                        "parent_content_key": item["parent_content_key"],
                        "structure_id": item["structure_id"],
                    }
                    for item in inventory
                ],
                "schema_version": "object-inventory/v1",
            }
        )
        if output_mapping["inventory_sha256"] != expected_inventory_sha256:
            raise SpatialHandoffError("count decision inventory SHA-256 does not match")
        included = _content_keys(output_mapping["included_content_keys"], "included content keys")
        not_selected = _content_keys(
            output_mapping["not_selected_content_keys"],
            "not-selected content keys",
        )
        if set(included) & set(not_selected):
            raise SpatialHandoffError("included and capped-out content keys overlap")
        fraction = _fraction(output_mapping.get("inclusion_fraction"), "inclusion_fraction")
        probability_status = output_mapping.get("inclusion_probability_status")
        if probability_status not in {"known", "unknown"}:
            raise SpatialHandoffError("inclusion probability status is invalid")
        probability_raw = output_mapping.get("inclusion_probability")
        probability = (
            None if probability_raw is None else _fraction(probability_raw, "inclusion_probability")
        )
        if (probability_status == "known") != (probability is not None):
            raise SpatialHandoffError("inclusion probability is not closed")

        _validate_count_output_state(
            output_id=output_id,
            status=str(status),
            observed_count=observed_count,
            minimum=minimum,
            maximum=maximum,
            inventory_sha256=str(output_mapping["inventory_sha256"]),
            inventory=tuple(inventory),
            included=included,
            not_selected=not_selected,
            fraction=fraction,
            probability_status=str(probability_status),
            probability=probability,
            lineage=output_lineage,
            excluded_domain=has_domain_exclusion,
        )
        if output_lineage is not None:
            previous = lineage_by_structure.setdefault(output_id, output_lineage)
            if _plain_json(previous) != _plain_json(output_lineage):
                raise SpatialHandoffError("count decision selection lineage is inconsistent")
        outputs.append(dict(output_mapping))

    exclusion_mapping = mapping.get("domain_exclusion")
    exclusion: Mapping[str, object] | None = None
    if exclusion_mapping is not None:
        raw_exclusion = _closed_mapping(
            exclusion_mapping,
            {"domain", "status", "failing_output_ids", "reason"},
            "domain exclusion",
        )
        exclusion_domain = _closed_mapping(
            raw_exclusion["domain"], set(domain_mapping), "domain exclusion scope"
        )
        if _plain_json(exclusion_domain) != _plain_json(domain_mapping):
            raise SpatialHandoffError("domain exclusion scope is stale")
        failing_ids = tuple(
            _sequence(
                raw_exclusion["failing_output_ids"],
                "domain exclusion failing outputs",
            )
        )
        if (
            raw_exclusion["status"] != "excluded_below_minimum"
            or not failing_ids
            or len(set(failing_ids)) != len(failing_ids)
            or any(not _is_nonblank(item) for item in failing_ids)
            or raw_exclusion["reason"] != "one_or_more_outputs_below_configured_minimum"
        ):
            raise SpatialHandoffError("domain exclusion is invalid")
        exclusion = raw_exclusion
    if mapping["component_selection_action"] is not None:
        raise SpatialHandoffError("count decision component selection action is forbidden")
    output_ids = tuple(str(output["output_id"]) for output in outputs)
    if len(set(output_ids)) != len(output_ids):
        raise SpatialHandoffError("count decision output IDs must be unique")
    failing = tuple(
        str(output["output_id"]) for output in outputs if output["status"] == "below_minimum"
    )
    if exclusion is None:
        if failing:
            raise SpatialHandoffError("below-minimum outputs require a domain exclusion")
    else:
        if tuple(exclusion["failing_output_ids"]) != failing:
            raise SpatialHandoffError("domain exclusion failing outputs do not match decision")
        if any(output["status"] not in {"below_minimum", "within_bounds"} for output in outputs):
            raise SpatialHandoffError("excluded domain has an invalid output status")
    if (expected_domain_status == "active" and exclusion is not None) or (
        expected_domain_status == "excluded_below_minimum" and exclusion is None
    ):
        raise SpatialHandoffError("count decision domain exclusion is stale")
    plain = _plain_json(mapping)
    assert isinstance(plain, dict)
    return plain


def _content_keys(value: object, field: str) -> tuple[str, ...]:
    keys = tuple(_sequence(value, field))
    if len(set(keys)) != len(keys):
        raise SpatialHandoffError(f"{field} must contain unique keys")
    for key in keys:
        _sha256(key, field)
    return keys


def _fraction(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 <= result <= 1.0:
        raise SpatialHandoffError(f"{field} must be between zero and one")
    return result


def _validate_count_output_state(
    *,
    output_id: str,
    status: str,
    observed_count: int,
    minimum: int,
    maximum: int | None,
    inventory_sha256: str,
    inventory: tuple[Mapping[str, object], ...],
    included: tuple[str, ...],
    not_selected: tuple[str, ...],
    fraction: float,
    probability_status: str,
    probability: float | None,
    lineage: Mapping[str, object] | None,
    excluded_domain: bool,
) -> None:
    """Validate the closed count-output state carried into the handoff."""
    inventory_keys = tuple(str(item["content_key"]) for item in inventory)
    if excluded_domain:
        expected_status = "below_minimum" if observed_count < minimum else "within_bounds"
        if (
            status != expected_status
            or lineage is not None
            or included
            or not_selected
            or fraction != 0.0
            or probability_status != "known"
            or probability != 0.0
            or any(
                item["disposition"] != "audit_excluded_below_minimum"
                or any(
                    item[field] is not None
                    for field in (
                        "observed_count",
                        "inclusion_fraction",
                        "inventory_sha256",
                        "inclusion_probability_status",
                        "inclusion_probability",
                        "selection_lineage",
                    )
                )
                for item in inventory
            )
        ):
            raise SpatialHandoffError("excluded-domain count output state is invalid")
        return
    if status == "capped":
        if maximum is None or observed_count <= maximum or lineage is None:
            raise SpatialHandoffError("capped output state is invalid")
        if (
            len(included) != maximum
            or set(included) | set(not_selected) != set(inventory_keys)
            or included != tuple(key for key in inventory_keys if key in set(included))
            or not_selected != tuple(key for key in inventory_keys if key in set(not_selected))
            or fraction != maximum / observed_count
            or lineage["inventory_sha256"] != inventory_sha256
            or lineage["structure_id"] != output_id
        ):
            raise SpatialHandoffError("capped output partition is inconsistent")
        manual = lineage["algorithm"] == "manual-select/v1"
        if manual:
            if (
                included != tuple(lineage["selected_content_keys"])
                or probability_status != "unknown"
                or probability is not None
            ):
                raise SpatialHandoffError("manual capped output probability is inconsistent")
        elif probability_status != "known" or probability != fraction:
            raise SpatialHandoffError("deterministic capped output probability is inconsistent")
        for item in inventory:
            expected_disposition = (
                "active_included"
                if item["content_key"] in set(included)
                else "valid_not_selected_by_cap"
            )
            if (
                item["disposition"] != expected_disposition
                or _plain_json(item["selection_lineage"]) != _plain_json(lineage)
                or item["observed_count"] != observed_count
                or item["inclusion_fraction"] != fraction
                or item["inventory_sha256"] != inventory_sha256
                or item["inclusion_probability_status"] != probability_status
                or item["inclusion_probability"] != probability
            ):
                raise SpatialHandoffError("inventory cap metadata does not match output")
        return

    if lineage is not None or not_selected:
        raise SpatialHandoffError("uncapped output carries cap state")
    if status == "below_minimum":
        if (
            observed_count >= minimum
            or included
            or fraction != 0.0
            or probability_status != "known"
            or probability != 0.0
            or any(
                item["disposition"] != "audit_excluded_below_minimum"
                or any(
                    item[field] is not None
                    for field in (
                        "observed_count",
                        "inclusion_fraction",
                        "inventory_sha256",
                        "inclusion_probability_status",
                        "inclusion_probability",
                        "selection_lineage",
                    )
                )
                for item in inventory
            )
        ):
            raise SpatialHandoffError("below-minimum output state is invalid")
        return
    if observed_count < minimum:
        raise SpatialHandoffError("non-failing output is below its minimum")
    if (
        included != inventory_keys
        or fraction != 1.0
        or probability_status != "known"
        or probability != 1.0
        or any(
            item["disposition"] != "active_included"
            or any(
                item[field] is not None
                for field in (
                    "observed_count",
                    "inclusion_fraction",
                    "inventory_sha256",
                    "inclusion_probability_status",
                    "inclusion_probability",
                    "selection_lineage",
                )
            )
            for item in inventory
        )
    ):
        raise SpatialHandoffError("uncapped active output state is invalid")
    if status == "within_bounds" and maximum is not None and observed_count > maximum:
        raise SpatialHandoffError("within-bounds output exceeds maximum")


def _validate_dispositions_against_decision(
    dispositions: tuple[FinalObjectDispositionEvidence, ...],
    decision: Mapping[str, object],
) -> None:
    """Validate dispositions against decision for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    decision_items: dict[str, Mapping[str, object]] = {}
    for raw_output in _sequence(decision["outputs"], "count decision outputs"):
        output = _closed_mapping(
            raw_output,
            {
                "output_id",
                "status",
                "observed_count",
                "minimum",
                "maximum",
                "inventory_sha256",
                "inventory",
                "included_content_keys",
                "not_selected_content_keys",
                "inclusion_fraction",
                "inclusion_probability_status",
                "inclusion_probability",
                "selection_lineage",
            },
            "count decision output",
        )
        for raw_item in _sequence(output["inventory"], "count decision inventory"):
            item = _closed_mapping(
                raw_item,
                {
                    "content_key",
                    "structure_id",
                    "parent_content_key",
                    "geometry_sha256",
                    "disposition",
                    "observed_count",
                    "inclusion_fraction",
                    "inventory_sha256",
                    "inclusion_probability_status",
                    "inclusion_probability",
                    "selection_lineage",
                },
                "count decision inventory item",
            )
            decision_items[str(item["content_key"])] = item
    if {item.content_key for item in dispositions} != set(decision_items):
        raise SpatialHandoffError(
            "final dispositions are not a content-key bijection with count inventory"
        )
    if len({item.roi_id for item in dispositions}) != len(dispositions):
        raise SpatialHandoffError("final disposition ROI IDs must be unique")
    for disposition in dispositions:
        source = decision_items[disposition.content_key]
        lineage = source["selection_lineage"]
        expected_lineage_sha256 = None if lineage is None else canonical_json_sha256(lineage)
        if (
            disposition.structure_id != source["structure_id"]
            or disposition.parent_content_key != source["parent_content_key"]
            or disposition.geometry_sha256 != source["geometry_sha256"]
            or disposition.disposition != source["disposition"]
            or disposition.selection_lineage_sha256 != expected_lineage_sha256
        ):
            raise SpatialHandoffError("final disposition conflicts with count inventory")


def _validate_manual_evidence_against_decision(
    manual: tuple[ManualSelectionEvidence, ...],
    decision: Mapping[str, object],
) -> None:
    expected: dict[str, tuple[Mapping[str, object], int]] = {}
    for raw_output in _sequence(decision["outputs"], "count decision outputs"):
        output = raw_output
        assert isinstance(output, Mapping)
        lineage = output.get("selection_lineage")
        if isinstance(lineage, Mapping) and lineage.get("algorithm") == "manual-select/v1":
            expected[str(output["output_id"])] = (
                lineage,
                int(output["observed_count"]),
            )
    if {item.structure_id for item in manual} != set(expected):
        raise SpatialHandoffError("manual selection evidence does not match the count decision")
    for evidence in manual:
        lineage, observed_count = expected[evidence.structure_id]
        if (
            _plain_json(evidence.selection_lineage) != _plain_json(lineage)
            or evidence.inventory_descriptor["record_count"] != observed_count
        ):
            raise SpatialHandoffError("manual selection evidence conflicts with count lineage")


def _validate_finalization_receipt(
    value: object,
    *,
    domain_id: str,
    artifact: SemanticResultArtifactEvidence,
    dispositions: tuple[FinalObjectDispositionEvidence, ...],
) -> dict[str, object]:
    receipt = _closed_mapping(
        value,
        {
            "schema_version",
            "analysis_domain_id",
            "section_id",
            "tissue_generation_id",
            "pipeline_run_id",
            "semantic_result_set_sha256",
            "semantic_result_artifact",
            "active_secondary_roi_ids",
            "accepted_secondary_roi_ids",
            "accepted_secondary_count",
            "reconciled_cell_ids_sha256",
            "membership_cell_count",
        },
        "semantic finalization receipt",
    )
    for field in (
        "analysis_domain_id",
        "section_id",
        "tissue_generation_id",
        "pipeline_run_id",
    ):
        _nonblank(receipt.get(field), f"semantic finalization {field}")
    active = _sorted_unique_ids(
        _sequence(
            receipt.get("active_secondary_roi_ids"),
            "active secondary ROI IDs",
        ),
        "active secondary ROI IDs",
    )
    accepted = _sorted_unique_ids(
        _sequence(
            receipt.get("accepted_secondary_roi_ids"),
            "accepted secondary ROI IDs",
        ),
        "accepted secondary ROI IDs",
    )
    _sha256(receipt.get("reconciled_cell_ids_sha256"), "reconciled cell IDs SHA-256")
    membership_count = receipt.get("membership_cell_count")
    if type(membership_count) is not int or membership_count < 0:
        raise SpatialHandoffError("semantic membership count is invalid")
    expected_active = tuple(
        sorted(item.roi_id for item in dispositions if item.disposition == "active_included")
    )
    if (
        receipt.get("schema_version") != "semantic-finalization/v2"
        or receipt.get("analysis_domain_id") != domain_id
        or receipt.get("semantic_result_set_sha256") != artifact.semantic_result_set_sha256
        or _plain_json(receipt.get("semantic_result_artifact")) != artifact.to_mapping()
        or active != expected_active
        or accepted != expected_active
        or receipt.get("accepted_secondary_count") != len(expected_active)
    ):
        raise SpatialHandoffError("semantic finalization receipt identity is invalid")
    result = dict(_plain_json(receipt))
    result["active_secondary_roi_ids"] = list(active)
    result["accepted_secondary_roi_ids"] = list(accepted)
    return result


def _typed_mapping(value: object, expected: type[Any], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpatialHandoffError(f"{field} must be a mapping")
    result = dict(value)
    if any(
        not isinstance(key, str) or not isinstance(item, expected) for key, item in result.items()
    ):
        raise SpatialHandoffError(f"{field} has invalid keys or values")
    return result


def _closed_mapping(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SpatialHandoffError(f"{label} fields do not match the closed schema")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SpatialHandoffError(f"{field} must be an array")
    return value


def _sorted_unique_ids(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SpatialHandoffError(f"{field} must be an array")
    result = tuple(values)
    if any(not _is_nonblank(item) for item in result) or result != tuple(sorted(set(result))):
        raise SpatialHandoffError(f"{field} must be sorted, unique non-empty strings")
    return result


def _require_instances(values: tuple[object, ...], expected: type[object], field: str) -> None:
    if any(not isinstance(value, expected) for value in values):
        raise SpatialHandoffError(f"{field} must contain {expected.__name__} instances")


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise SpatialHandoffError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SpatialHandoffError(f"{field} must be a non-negative integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpatialHandoffError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise SpatialHandoffError(f"{field} must be finite")
    return result


def _positive_finite(value: object, field: str) -> float:
    result = _finite(value, field)
    if result <= 0.0:
        raise SpatialHandoffError(f"{field} must be positive")
    return result


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SpatialHandoffError(f"{field} must be a lowercase SHA-256")
    return value


def _is_nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonblank(value: object, field: str) -> str:
    if not _is_nonblank(value):
        raise SpatialHandoffError(f"{field} must be a non-empty string")
    assert isinstance(value, str)
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain_json(item) for item in value]
    if isinstance(value, AnalysisDomainKind):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SpatialHandoffError("handoff evidence is not canonical JSON data")


def _freeze_json(value: object) -> object:
    """Recursively freeze already validated JSON evidence."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SpatialHandoffError("handoff evidence is not canonical JSON data")


def _without_locator_paths(value: object) -> object:
    """Return canonical JSON evidence with locator-only ``path`` keys removed."""
    if isinstance(value, Mapping):
        return {
            str(key): _without_locator_paths(item) for key, item in value.items() if key != "path"
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_without_locator_paths(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SpatialHandoffError("handoff evidence is not canonical JSON data")


__all__ = [
    "AnalysisDomainCellGroup",
    "CanonicalSpatialFrameEvidence",
    "DomainCellInventoryEvidence",
    "DomainFinalizationEvidence",
    "DomainHandoffEvidence",
    "DomainSetEvidence",
    "DomainSetMemberEvidence",
    "FinalObjectDispositionEvidence",
    "FinalizedSecondaryDomainEvidence",
    "FinalROIState",
    "ManualSelectionEvidence",
    "NoSecondaryDomainCompletionEvidence",
    "PrimaryExcludedDomainCompletionEvidence",
    "SemanticResultArtifactEvidence",
    "SpatialCellHandoff",
    "SpatialHandoffError",
    "TissueCellGroup",
    "build_spatial_handoff",
    "canonical_json_sha256",
    "path_free_identity_sha256",
    "semantic_parent_content_key",
    "semantic_secondary_content_key",
    "stable_id_inventory_sha256",
]
