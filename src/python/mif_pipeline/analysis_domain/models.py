"""Immutable stage-owned analysis-domain aggregates and evidence records.

The `ActiveAnalysisDomainAuthority` record carries `analysis_domain_id`,
`analysis_domain_kind`, `canonical_image_id`, `canonical_image_sha256`, `project_entry_id`
and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import cast

from mif_pipeline.pipeline_core import (
    AnalysisDomainKind,
    AnalysisDomainRef,
    ROIAnalysisError,
    analysis_domain_id,
)

# ==============================================================================
#                                     Models
# ==============================================================================


class AnalysisDomainError(ROIAnalysisError):
    """Raised when analysis-domain identity or geometry evidence is invalid."""


MAX_ANALYSIS_DOMAINS_PER_MANIFEST = 256
MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST = 4096
_MAX_NORMALIZED_WKB_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOWERCASE_HEX = re.compile(r"[0-9a-f]+")


def _closed_mapping(value: object, *, fields: frozenset[str], record: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnalysisDomainError(f"{record} must be a mapping")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields, key=repr)
        raise AnalysisDomainError(
            f"{record} has an invalid closed key set " f"(missing={missing}, unknown={unknown})"
        )
    return value


def _nonblank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisDomainError(f"{field_name} must be a non-empty string")
    return value


def _optional_nonblank(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _nonblank(value, field_name)


def _sha256(value: object, field_name: str) -> str:
    result = _nonblank(value, field_name)
    if _SHA256.fullmatch(result) is None:
        raise AnalysisDomainError(f"{field_name} must be a lowercase SHA-256")
    return result


def _optional_sha256(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field_name)


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AnalysisDomainError(f"{field_name} must be a positive integer")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AnalysisDomainError(f"{field_name} must be a boolean")
    return value


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisDomainError(f"{field_name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise AnalysisDomainError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise AnalysisDomainError(f"{field_name} must be a finite number")
    return result


def _decode_normalized_wkb(value: object, field_name: str) -> bytes | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) % 2 != 0
        or len(value) > 2 * _MAX_NORMALIZED_WKB_BYTES
        or _LOWERCASE_HEX.fullmatch(value) is None
    ):
        raise AnalysisDomainError(f"{field_name} must be bounded nonempty lowercase hexadecimal")
    return bytes.fromhex(value)


def _encode_normalized_wkb(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, bytes) or not value or len(value) > _MAX_NORMALIZED_WKB_BYTES:
        raise AnalysisDomainError(f"{field_name} must be bounded nonempty bytes before projection")
    return value.hex()


def _validate_geometry_digest(
    geometry_sha256: str | None,
    normalized_wkb: bytes | None,
) -> None:
    if (geometry_sha256 is None) != (normalized_wkb is None):
        raise AnalysisDomainError(
            "geometry SHA-256 and normalized WKB must both be present or both be null"
        )
    if normalized_wkb is not None:
        assert geometry_sha256 is not None
        if hashlib.sha256(normalized_wkb).hexdigest() != geometry_sha256:
            raise AnalysisDomainError("geometry SHA-256 does not match normalized WKB bytes")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalysisDomainRevision:
    """One immutable content revision of a stable analysis domain."""

    ref: AnalysisDomainRef
    canonical_image_id: str
    canonical_image_sha256: str
    project_entry_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    source_path_object_id: str
    revision: int
    predecessor_sha256: str | None
    geometry_digest_policy: str
    geometry_sha256: str | None
    normalized_wkb: bytes | None
    geometry_reference_path_object_id: str | None
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "analysis_domain_id": self.ref.analysis_domain_id,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "geometry_digest_policy": self.geometry_digest_policy,
            "geometry_reference_path_object_id": self.geometry_reference_path_object_id,
            "geometry_sha256": self.geometry_sha256,
            "kind": self.ref.kind.value,
            "predecessor_sha256": self.predecessor_sha256,
            "project_entry_id": self.project_entry_id,
            "revision": self.revision,
            "section_id": self.ref.section_id,
            "slide_id": self.ref.slide_id,
            "source_path_object_id": self.source_path_object_id,
            "tissue_generation_id": self.ref.tissue_generation_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "tissue_roi_id": self.tissue_roi_id,
        }
        object.__setattr__(self, "content_sha256", _canonical_sha256(payload))

    @property
    def analysis_domain_id(self) -> str:
        """Return the stable identity shared by every content revision."""
        return self.ref.analysis_domain_id

    def to_mapping(self) -> dict[str, object]:
        """Return the closed owner wire projection for this revision."""
        _positive_integer(self.revision, "revision")
        normalized_wkb_hex = _encode_normalized_wkb(self.normalized_wkb, "normalized_wkb")
        _validate_geometry_digest(self.geometry_sha256, self.normalized_wkb)
        return {
            "analysis_domain_id": self.analysis_domain_id,
            "kind": self.ref.kind.value,
            "slide_id": self.ref.slide_id,
            "section_id": self.ref.section_id,
            "tissue_generation_id": self.ref.tissue_generation_id,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "project_entry_id": self.project_entry_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "source_path_object_id": self.source_path_object_id,
            "revision": self.revision,
            "predecessor_sha256": self.predecessor_sha256,
            "geometry_digest_policy": self.geometry_digest_policy,
            "geometry_sha256": self.geometry_sha256,
            "normalized_wkb_hex": normalized_wkb_hex,
            "geometry_reference_path_object_id": (self.geometry_reference_path_object_id),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> AnalysisDomainRevision:
        """Decode the serialized fields into a validated AnalysisDomainRevision record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""
        fields = frozenset(
            {
                "analysis_domain_id",
                "kind",
                "slide_id",
                "section_id",
                "tissue_generation_id",
                "canonical_image_id",
                "canonical_image_sha256",
                "project_entry_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "source_path_object_id",
                "revision",
                "predecessor_sha256",
                "geometry_digest_policy",
                "geometry_sha256",
                "normalized_wkb_hex",
                "geometry_reference_path_object_id",
                "content_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=fields, record="domain revision")
        kind_value = mapping["kind"]
        if not isinstance(kind_value, str):
            raise AnalysisDomainError("kind must be an analysis-domain kind string")
        try:
            kind = AnalysisDomainKind(kind_value)
        except ValueError as exc:
            raise AnalysisDomainError("kind is unsupported") from exc
        geometry_sha256 = _optional_sha256(mapping["geometry_sha256"], "geometry_sha256")
        normalized_wkb = _decode_normalized_wkb(mapping["normalized_wkb_hex"], "normalized_wkb_hex")
        _validate_geometry_digest(geometry_sha256, normalized_wkb)
        # Native JSON readers can represent an integer token as 1.0. Preserve
        # its integer meaning before checking the original revision digest;
        # fractional values and floats outside the exact integer range fail.
        revision_number = mapping["revision"]
        if (isinstance(revision_number, float) and revision_number.is_integer()
                and 1 <= revision_number < 2**53):
            revision_number = int(revision_number)
        revision = cls(
            ref=AnalysisDomainRef(
                analysis_domain_id=_sha256(mapping["analysis_domain_id"], "analysis_domain_id"),
                kind=kind,
                slide_id=_nonblank(mapping["slide_id"], "slide_id"),
                section_id=_nonblank(mapping["section_id"], "section_id"),
                tissue_generation_id=_nonblank(
                    mapping["tissue_generation_id"], "tissue_generation_id"
                ),
            ),
            canonical_image_id=_nonblank(mapping["canonical_image_id"], "canonical_image_id"),
            canonical_image_sha256=_sha256(
                mapping["canonical_image_sha256"], "canonical_image_sha256"
            ),
            project_entry_id=_nonblank(mapping["project_entry_id"], "project_entry_id"),
            tissue_roi_id=_nonblank(mapping["tissue_roi_id"], "tissue_roi_id"),
            tissue_path_object_id=_nonblank(
                mapping["tissue_path_object_id"], "tissue_path_object_id"
            ),
            source_path_object_id=_nonblank(
                mapping["source_path_object_id"], "source_path_object_id"
            ),
            revision=_positive_integer(revision_number, "revision"),
            predecessor_sha256=_optional_sha256(
                mapping["predecessor_sha256"], "predecessor_sha256"
            ),
            geometry_digest_policy=_nonblank(
                mapping["geometry_digest_policy"], "geometry_digest_policy"
            ),
            geometry_sha256=geometry_sha256,
            normalized_wkb=normalized_wkb,
            geometry_reference_path_object_id=_optional_nonblank(
                mapping["geometry_reference_path_object_id"],
                "geometry_reference_path_object_id",
            ),
        )
        if revision.content_sha256 != _sha256(mapping["content_sha256"], "content_sha256"):
            raise AnalysisDomainError("domain revision content SHA-256 drift")
        return revision


@dataclass(frozen=True, slots=True)
class AnalysisDomainManifest:
    """Bounded immutable revision inventory for one tissue section."""

    schema_version: str
    canonical_image_id: str
    canonical_image_sha256: str
    project_entry_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    revisions: tuple[AnalysisDomainRevision, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "revisions", tuple(self.revisions))
        if len(self.revisions) > MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST:
            raise AnalysisDomainError("analysis-domain manifest exceeds its total revision limit")
        canonical_revisions = sorted(
            self.revisions,
            key=lambda item: (
                item.analysis_domain_id,
                item.revision,
                item.content_sha256,
            ),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _canonical_sha256(
                {
                    "canonical_image_id": self.canonical_image_id,
                    "canonical_image_sha256": self.canonical_image_sha256,
                    "project_entry_id": self.project_entry_id,
                    "revision_sha256s": [item.content_sha256 for item in canonical_revisions],
                    "schema_version": self.schema_version,
                    "section_id": self.section_id,
                    "slide_id": self.slide_id,
                    "tissue_generation_id": self.tissue_generation_id,
                    "tissue_path_object_id": self.tissue_path_object_id,
                    "tissue_roi_id": self.tissue_roi_id,
                }
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed owner wire projection for this manifest."""
        if self.schema_version != "analysis-domain/v1":
            raise AnalysisDomainError("analysis-domain manifest schema version is unsupported")
        if len(self.revisions) > MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST:
            raise AnalysisDomainError("analysis-domain manifest exceeds its total revision limit")
        if len({item.analysis_domain_id for item in self.revisions}) > (
            MAX_ANALYSIS_DOMAINS_PER_MANIFEST
        ):
            raise AnalysisDomainError("analysis-domain manifest exceeds its bounded inventory")
        return {
            "schema_version": self.schema_version,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "project_entry_id": self.project_entry_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "revisions": [item.to_mapping() for item in self.revisions],
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> AnalysisDomainManifest:
        """Decode the serialized fields into a validated AnalysisDomainManifest record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""
        fields = frozenset(
            {
                "schema_version",
                "canonical_image_id",
                "canonical_image_sha256",
                "project_entry_id",
                "slide_id",
                "section_id",
                "tissue_generation_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "revisions",
                "content_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=fields, record="domain manifest")
        schema_version = _nonblank(mapping["schema_version"], "schema_version")
        if schema_version != "analysis-domain/v1":
            raise AnalysisDomainError("analysis-domain manifest schema version is unsupported")
        revision_values = mapping["revisions"]
        if not isinstance(revision_values, list):
            raise AnalysisDomainError("manifest revisions must be a list")
        if len(revision_values) > MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST:
            raise AnalysisDomainError("analysis-domain manifest exceeds its total revision limit")
        revisions = tuple(AnalysisDomainRevision.from_mapping(item) for item in revision_values)
        if len({item.analysis_domain_id for item in revisions}) > (
            MAX_ANALYSIS_DOMAINS_PER_MANIFEST
        ):
            raise AnalysisDomainError("analysis-domain manifest exceeds its bounded inventory")
        manifest = cls(
            schema_version=schema_version,
            canonical_image_id=_nonblank(mapping["canonical_image_id"], "canonical_image_id"),
            canonical_image_sha256=_sha256(
                mapping["canonical_image_sha256"], "canonical_image_sha256"
            ),
            project_entry_id=_nonblank(mapping["project_entry_id"], "project_entry_id"),
            slide_id=_nonblank(mapping["slide_id"], "slide_id"),
            section_id=_nonblank(mapping["section_id"], "section_id"),
            tissue_generation_id=_nonblank(mapping["tissue_generation_id"], "tissue_generation_id"),
            tissue_roi_id=_nonblank(mapping["tissue_roi_id"], "tissue_roi_id"),
            tissue_path_object_id=_nonblank(
                mapping["tissue_path_object_id"], "tissue_path_object_id"
            ),
            revisions=revisions,
        )
        if manifest.content_sha256 != _sha256(mapping["content_sha256"], "content_sha256"):
            raise AnalysisDomainError("domain manifest content SHA-256 drift")
        return manifest


@dataclass(frozen=True, slots=True)
class DomainGeometryEvidence:
    """QuPath/JTS-produced normalized geometry evidence for one candidate."""

    path_object_id: str
    parent_path_object_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    geometry_digest_policy: str
    geometry_sha256: str | None
    normalized_wkb: bytes | None
    is_valid: bool
    is_empty: bool
    is_finite: bool
    area: float
    covered_by_parent: bool

    def to_mapping(self) -> dict[str, object]:
        """Return the closed owner wire projection for geometry evidence."""
        _boolean(self.is_valid, "is_valid")
        _boolean(self.is_empty, "is_empty")
        _boolean(self.is_finite, "is_finite")
        _finite_number(self.area, "area")
        _boolean(self.covered_by_parent, "covered_by_parent")
        normalized_wkb_hex = _encode_normalized_wkb(self.normalized_wkb, "normalized_wkb")
        _validate_geometry_digest(self.geometry_sha256, self.normalized_wkb)
        return {
            "path_object_id": self.path_object_id,
            "parent_path_object_id": self.parent_path_object_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "geometry_digest_policy": self.geometry_digest_policy,
            "geometry_sha256": self.geometry_sha256,
            "normalized_wkb_hex": normalized_wkb_hex,
            "is_valid": self.is_valid,
            "is_empty": self.is_empty,
            "is_finite": self.is_finite,
            "area": self.area,
            "covered_by_parent": self.covered_by_parent,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DomainGeometryEvidence:
        """Decode the serialized fields into a validated DomainGeometryEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""
        fields = frozenset(
            {
                "path_object_id",
                "parent_path_object_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "geometry_digest_policy",
                "geometry_sha256",
                "normalized_wkb_hex",
                "is_valid",
                "is_empty",
                "is_finite",
                "area",
                "covered_by_parent",
            }
        )
        mapping = _closed_mapping(value, fields=fields, record="geometry evidence")
        geometry_sha256 = _optional_sha256(mapping["geometry_sha256"], "geometry_sha256")
        normalized_wkb = _decode_normalized_wkb(mapping["normalized_wkb_hex"], "normalized_wkb_hex")
        _validate_geometry_digest(geometry_sha256, normalized_wkb)
        return cls(
            path_object_id=_nonblank(mapping["path_object_id"], "path_object_id"),
            parent_path_object_id=_nonblank(
                mapping["parent_path_object_id"], "parent_path_object_id"
            ),
            tissue_roi_id=_nonblank(mapping["tissue_roi_id"], "tissue_roi_id"),
            tissue_path_object_id=_nonblank(
                mapping["tissue_path_object_id"], "tissue_path_object_id"
            ),
            geometry_digest_policy=_nonblank(
                mapping["geometry_digest_policy"], "geometry_digest_policy"
            ),
            geometry_sha256=geometry_sha256,
            normalized_wkb=normalized_wkb,
            is_valid=_boolean(mapping["is_valid"], "is_valid"),
            is_empty=_boolean(mapping["is_empty"], "is_empty"),
            is_finite=_boolean(mapping["is_finite"], "is_finite"),
            area=_finite_number(mapping["area"], "area"),
            covered_by_parent=_boolean(mapping["covered_by_parent"], "covered_by_parent"),
        )


@dataclass(frozen=True, slots=True)
class DomainOverlapEvidence:
    """Exact spatial-index-produced overlap evidence for one section."""

    canonical_image_id: str
    canonical_image_sha256: str
    project_entry_id: str
    section_id: str
    tissue_generation_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    geometry_evidence: tuple[DomainGeometryEvidence, ...]
    positive_area_overlaps: tuple[tuple[str, str], ...]
    generated_by_spatial_index: bool
    exact_jts_area_test: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "geometry_evidence", tuple(self.geometry_evidence))
        object.__setattr__(self, "positive_area_overlaps", tuple(self.positive_area_overlaps))

    def to_mapping(self) -> dict[str, object]:
        """Return the closed owner wire projection for overlap evidence."""
        _boolean(self.generated_by_spatial_index, "generated_by_spatial_index")
        _boolean(self.exact_jts_area_test, "exact_jts_area_test")
        if len(self.geometry_evidence) > MAX_ANALYSIS_DOMAINS_PER_MANIFEST:
            raise AnalysisDomainError("geometry evidence exceeds its bounded inventory")
        return {
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "project_entry_id": self.project_entry_id,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "geometry_evidence": [item.to_mapping() for item in self.geometry_evidence],
            "positive_area_overlaps": [list(item) for item in self.positive_area_overlaps],
            "generated_by_spatial_index": self.generated_by_spatial_index,
            "exact_jts_area_test": self.exact_jts_area_test,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DomainOverlapEvidence:
        """Decode the serialized fields into a validated DomainOverlapEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""
        fields = frozenset(
            {
                "canonical_image_id",
                "canonical_image_sha256",
                "project_entry_id",
                "section_id",
                "tissue_generation_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "geometry_evidence",
                "positive_area_overlaps",
                "generated_by_spatial_index",
                "exact_jts_area_test",
            }
        )
        mapping = _closed_mapping(value, fields=fields, record="overlap evidence")
        geometry_values = mapping["geometry_evidence"]
        if not isinstance(geometry_values, list):
            raise AnalysisDomainError("geometry_evidence must be a list")
        if len(geometry_values) > MAX_ANALYSIS_DOMAINS_PER_MANIFEST:
            raise AnalysisDomainError("geometry evidence exceeds its bounded inventory")
        overlap_values = mapping["positive_area_overlaps"]
        if not isinstance(overlap_values, list):
            raise AnalysisDomainError("positive_area_overlaps must be a list")
        overlaps: list[tuple[str, str]] = []
        for item in overlap_values:
            if not isinstance(item, list) or len(item) != 2:
                raise AnalysisDomainError("each positive-area overlap must be a two-item list")
            overlaps.append(
                (
                    _nonblank(item[0], "positive_area_overlap[0]"),
                    _nonblank(item[1], "positive_area_overlap[1]"),
                )
            )
        return cls(
            canonical_image_id=_nonblank(mapping["canonical_image_id"], "canonical_image_id"),
            canonical_image_sha256=_sha256(
                mapping["canonical_image_sha256"], "canonical_image_sha256"
            ),
            project_entry_id=_nonblank(mapping["project_entry_id"], "project_entry_id"),
            section_id=_nonblank(mapping["section_id"], "section_id"),
            tissue_generation_id=_nonblank(mapping["tissue_generation_id"], "tissue_generation_id"),
            tissue_roi_id=_nonblank(mapping["tissue_roi_id"], "tissue_roi_id"),
            tissue_path_object_id=_nonblank(
                mapping["tissue_path_object_id"], "tissue_path_object_id"
            ),
            geometry_evidence=tuple(
                DomainGeometryEvidence.from_mapping(item) for item in geometry_values
            ),
            positive_area_overlaps=tuple(overlaps),
            generated_by_spatial_index=_boolean(
                mapping["generated_by_spatial_index"],
                "generated_by_spatial_index",
            ),
            exact_jts_area_test=_boolean(mapping["exact_jts_area_test"], "exact_jts_area_test"),
        )


@dataclass(frozen=True, slots=True)
class ActiveAnalysisDomainAuthority:
    """Owner projection of one validated current analysis-domain revision."""

    analysis_domain_id: str
    analysis_domain_kind: AnalysisDomainKind
    canonical_image_id: str
    canonical_image_sha256: str
    project_entry_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    source_path_object_id: str
    analysis_domain_revision: int
    analysis_domain_revision_sha256: str
    analysis_domain_manifest_sha256: str
    geometry_digest_policy: str
    geometry_sha256: str | None
    geometry_reference_path_object_id: str | None

    def _validate_invariants(self) -> None:
        """Validate invariants for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        if not isinstance(self.analysis_domain_kind, AnalysisDomainKind):
            raise AnalysisDomainError("analysis_domain_kind must be an AnalysisDomainKind")
        bound_id = _sha256(self.analysis_domain_id, "analysis_domain_id")
        canonical_image_sha256 = _sha256(self.canonical_image_sha256, "canonical_image_sha256")
        _nonblank(self.canonical_image_id, "canonical_image_id")
        _nonblank(self.project_entry_id, "project_entry_id")
        _nonblank(self.slide_id, "slide_id")
        section_id = _nonblank(self.section_id, "section_id")
        tissue_generation_id = _nonblank(self.tissue_generation_id, "tissue_generation_id")
        _nonblank(self.tissue_roi_id, "tissue_roi_id")
        tissue_path_object_id = _nonblank(self.tissue_path_object_id, "tissue_path_object_id")
        source_path_object_id = _nonblank(self.source_path_object_id, "source_path_object_id")
        _positive_integer(self.analysis_domain_revision, "analysis_domain_revision")
        _sha256(
            self.analysis_domain_revision_sha256,
            "analysis_domain_revision_sha256",
        )
        _sha256(
            self.analysis_domain_manifest_sha256,
            "analysis_domain_manifest_sha256",
        )
        geometry_digest_policy = _nonblank(self.geometry_digest_policy, "geometry_digest_policy")
        if geometry_digest_policy != "jts-normalized-wkb-sha256/v1":
            raise AnalysisDomainError("geometry digest policy is unsupported")
        geometry_sha256 = _optional_sha256(self.geometry_sha256, "geometry_sha256")
        geometry_reference_path_object_id = _optional_nonblank(
            self.geometry_reference_path_object_id,
            "geometry_reference_path_object_id",
        )
        if bound_id != analysis_domain_id(
            kind=self.analysis_domain_kind,
            canonical_image_sha256=canonical_image_sha256,
            tissue_generation_id=tissue_generation_id,
            section_id=section_id,
            source_path_object_id=source_path_object_id,
        ):
            raise AnalysisDomainError("analysis_domain_id does not match its bound identity")
        if self.analysis_domain_kind is AnalysisDomainKind.SELECTED_ROI:
            if geometry_sha256 is None or geometry_reference_path_object_id is not None:
                raise AnalysisDomainError(
                    "selected ROI authority must bind geometry without a reference"
                )
        elif (
            source_path_object_id != tissue_path_object_id
            or geometry_sha256 is not None
            or geometry_reference_path_object_id != tissue_path_object_id
        ):
            raise AnalysisDomainError(
                "whole-tissue authority must retain reference-only tissue geometry"
            )

    def to_mapping(self) -> dict[str, object]:
        """Return the exact closed authority projection."""
        self._validate_invariants()
        return {
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_kind": self.analysis_domain_kind.value,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "project_entry_id": self.project_entry_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "source_path_object_id": self.source_path_object_id,
            "analysis_domain_revision": self.analysis_domain_revision,
            "analysis_domain_revision_sha256": (self.analysis_domain_revision_sha256),
            "analysis_domain_manifest_sha256": (self.analysis_domain_manifest_sha256),
            "geometry_digest_policy": self.geometry_digest_policy,
            "geometry_sha256": self.geometry_sha256,
            "geometry_reference_path_object_id": (self.geometry_reference_path_object_id),
        }

    @classmethod
    def from_mapping(cls, value: object) -> ActiveAnalysisDomainAuthority:
        """Decode the serialized fields into a validated ActiveAnalysisDomainAuthority record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""
        fields = frozenset(
            {
                "analysis_domain_id",
                "analysis_domain_kind",
                "canonical_image_id",
                "canonical_image_sha256",
                "project_entry_id",
                "slide_id",
                "section_id",
                "tissue_generation_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "source_path_object_id",
                "analysis_domain_revision",
                "analysis_domain_revision_sha256",
                "analysis_domain_manifest_sha256",
                "geometry_digest_policy",
                "geometry_sha256",
                "geometry_reference_path_object_id",
            }
        )
        mapping = _closed_mapping(value, fields=fields, record="active analysis-domain authority")
        kind_value = mapping["analysis_domain_kind"]
        if not isinstance(kind_value, str):
            raise AnalysisDomainError("analysis_domain_kind must be an analysis-domain kind string")
        try:
            kind = AnalysisDomainKind(kind_value)
        except ValueError as exc:
            raise AnalysisDomainError("analysis_domain_kind is unsupported") from exc
        authority = cls(
            analysis_domain_id=cast(str, mapping["analysis_domain_id"]),
            analysis_domain_kind=kind,
            canonical_image_id=cast(str, mapping["canonical_image_id"]),
            canonical_image_sha256=cast(str, mapping["canonical_image_sha256"]),
            project_entry_id=cast(str, mapping["project_entry_id"]),
            slide_id=cast(str, mapping["slide_id"]),
            section_id=cast(str, mapping["section_id"]),
            tissue_generation_id=cast(str, mapping["tissue_generation_id"]),
            tissue_roi_id=cast(str, mapping["tissue_roi_id"]),
            tissue_path_object_id=cast(str, mapping["tissue_path_object_id"]),
            source_path_object_id=cast(str, mapping["source_path_object_id"]),
            analysis_domain_revision=cast(int, mapping["analysis_domain_revision"]),
            analysis_domain_revision_sha256=cast(str, mapping["analysis_domain_revision_sha256"]),
            analysis_domain_manifest_sha256=cast(str, mapping["analysis_domain_manifest_sha256"]),
            geometry_digest_policy=cast(str, mapping["geometry_digest_policy"]),
            geometry_sha256=cast(str | None, mapping["geometry_sha256"]),
            geometry_reference_path_object_id=cast(
                str | None, mapping["geometry_reference_path_object_id"]
            ),
        )
        authority._validate_invariants()
        return authority


__all__ = [
    "ActiveAnalysisDomainAuthority",
    "AnalysisDomainError",
    "AnalysisDomainManifest",
    "AnalysisDomainRevision",
    "DomainGeometryEvidence",
    "DomainOverlapEvidence",
    "MAX_ANALYSIS_DOMAINS_PER_MANIFEST",
    "MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST",
]
