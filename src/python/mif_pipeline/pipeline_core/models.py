"""Immutable, source-format-independent records for ROI analysis outputs.

The `CellRecord` record carries `cell_id`, `slide_id`, `section_id`, `x_level0`, `y_level0`
and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .errors import ROIAnalysisError, validate_safe_path_segment
from .domain import AnalysisDomainKind

# ==============================================================================
#                                     Models
# ==============================================================================


class SourceKind(str, Enum):
    """Origin of an analysis object or annotation."""

    MIF_MANUAL = "mif_manual"
    REGISTERED_REFERENCE_ASSISTED = "registered_reference_assisted"
    CLASSIFIER = "classifier"
    IMPORTED = "imported"


class ReviewStatus(str, Enum):
    """Human-review state retained independently from semantic validation."""

    NOT_REVIEWED = "not_reviewed"
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SemanticStatus(str, Enum):
    """Outcome of semantic ROI validation."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"
    NEEDS_REVIEW = "needs_review"


def _freeze_json(value: Any, *, field: str) -> Any:
    """Deep-copy immutable JSON-like analysis payloads for audit records."""
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ROIAnalysisError(f"{field} mapping keys must be strings")
            frozen[key] = _freeze_json(item, field=field)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field=field) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ROIAnalysisError(f"{field} must contain only JSON-like values")


def _freeze_mapping(value: Mapping[str, Any], *, field: str) -> Mapping[str, Any]:
    frozen = _freeze_json(value, field=field)
    assert isinstance(frozen, Mapping)
    return frozen


def _export_json(value: Any) -> Any:
    """Return a mutable JSON-serializable copy of a frozen record value."""
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class TissueSection:
    """One versioned QuPath tissue PathObject in the level-0 frame."""

    slide_id: str
    section_id: str
    roi_id: str
    path_object_id: str
    display_number: int
    generation_id: str
    revision: int
    review_status: ReviewStatus
    detector_context: Mapping[str, Any]
    provenance_pointer: str
    geometry: Mapping[str, Any]
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "slide_id",
            "section_id",
            "roi_id",
            "path_object_id",
            "generation_id",
            "provenance_pointer",
        ):
            _non_empty(getattr(self, field), field)
        if (
            isinstance(self.display_number, bool)
            or not isinstance(self.display_number, int)
            or self.display_number <= 0
        ):
            raise ROIAnalysisError("display_number must be a positive integer")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ROIAnalysisError("revision must be a non-negative integer")
        if not isinstance(self.review_status, ReviewStatus):
            raise ROIAnalysisError("review_status must be a ReviewStatus")
        if not isinstance(self.detector_context, Mapping):
            raise ROIAnalysisError("detector_context must be a mapping")
        if not isinstance(self.geometry, Mapping):
            raise ROIAnalysisError("geometry must be a mapping")
        object.__setattr__(
            self,
            "detector_context",
            _freeze_mapping(self.detector_context, field="detector_context"),
        )
        object.__setattr__(self, "geometry", _freeze_mapping(self.geometry, field="geometry"))
        for field in ("derived_from", "supersedes"):
            values = tuple(_non_empty(item, field) for item in getattr(self, field))
            if len(set(values)) != len(values) or self.roi_id in values:
                raise ROIAnalysisError(f"{field} must contain unique non-self ROI IDs")
            object.__setattr__(self, field, values)

    def with_corrected_geometry(
        self,
        geometry: Mapping[str, Any],
        *,
        review_status: ReviewStatus,
    ) -> "TissueSection":
        """Create the next audited revision without changing object identity."""
        return replace(
            self,
            geometry=geometry,
            revision=self.revision + 1,
            review_status=review_status,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "TissueSection":
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError("tissue section must be a string-keyed mapping")
        allowed = {
            "slide_id",
            "section_id",
            "roi_id",
            "path_object_id",
            "display_number",
            "generation_id",
            "revision",
            "review_status",
            "detector_context",
            "provenance_pointer",
            "geometry",
            "derived_from",
            "supersedes",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ROIAnalysisError("tissue section has unknown field(s): " + ", ".join(unknown))
        if set(value) != allowed:
            raise ROIAnalysisError("tissue section mapping requires every field")
        try:
            review_status = ReviewStatus(value["review_status"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("tissue section review_status is invalid") from exc
        derived = value["derived_from"]
        supersedes = value["supersedes"]
        if isinstance(derived, (str, bytes)) or not isinstance(derived, Sequence):
            raise ROIAnalysisError("derived_from must be a sequence")
        if isinstance(supersedes, (str, bytes)) or not isinstance(supersedes, Sequence):
            raise ROIAnalysisError("supersedes must be a sequence")
        return cls(
            slide_id=value["slide_id"],
            section_id=value["section_id"],
            roi_id=value["roi_id"],
            path_object_id=value["path_object_id"],
            display_number=value["display_number"],
            generation_id=value["generation_id"],
            revision=value["revision"],
            review_status=review_status,
            detector_context=value["detector_context"],
            provenance_pointer=value["provenance_pointer"],
            geometry=value["geometry"],
            derived_from=tuple(derived),
            supersedes=tuple(supersedes),
        )

    def to_export_mapping(self) -> dict[str, Any]:
        return {
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "roi_id": self.roi_id,
            "path_object_id": self.path_object_id,
            "display_number": self.display_number,
            "generation_id": self.generation_id,
            "revision": self.revision,
            "review_status": self.review_status.value,
            "detector_context": _export_json(self.detector_context),
            "provenance_pointer": self.provenance_pointer,
            "geometry": _export_json(self.geometry),
            "derived_from": list(self.derived_from),
            "supersedes": list(self.supersedes),
        }


@dataclass(frozen=True, slots=True)
class ROIRecord:
    """Canonical, auditable derived ROI export record."""

    roi_id: str
    slide_id: str
    section_id: str
    roi_class: str
    roi_level: str
    geometry: Mapping[str, Any]
    source: SourceKind
    classifier_id: str | None = None
    classifier_version: str | None = None
    classifier_confidence: float | None = None
    parent_roi_id: str | None = None
    semantic_profile: str | None = None
    semantic_status: SemanticStatus = SemanticStatus.CANDIDATE
    semantic_rule_results: Mapping[str, Any] | None = None
    review_status: ReviewStatus = ReviewStatus.NOT_REVIEWED
    original_predicted_geometry: Mapping[str, Any] | None = None
    registration_id: str | None = None
    registration_qc: Mapping[str, Any] | None = None
    analysis_domain_id: str = field(kw_only=True)
    analysis_domain_kind: AnalysisDomainKind = field(kw_only=True)
    semantic_classification_status: str | None = field(default=None, kw_only=True)
    semantic_class_id: str | None = field(default=None, kw_only=True)
    review_rationale: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Validate the ROIRecord fields when the record is constructed.

        The checks cover ``semantic_rule_results``, ``original_predicted_geometry``,
        ``registration_id``, ``registration_qc``, ``source``, ``analysis_domain_kind``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        for field in (
            "roi_id",
            "slide_id",
            "section_id",
            "roi_class",
            "roi_level",
            "analysis_domain_id",
        ):
            _non_empty(getattr(self, field), field)
        if not isinstance(self.analysis_domain_kind, AnalysisDomainKind):
            raise ROIAnalysisError("analysis_domain_kind must be an AnalysisDomainKind")
        if not isinstance(self.geometry, Mapping):
            raise ROIAnalysisError("geometry must be a mapping")
        object.__setattr__(self, "geometry", _freeze_mapping(self.geometry, field="geometry"))
        for name in ("semantic_classification_status", "semantic_class_id", "review_rationale"):
            value = getattr(self, name)
            if value is not None:
                _non_empty(value, name)
        if self.semantic_rule_results is not None:
            if not isinstance(self.semantic_rule_results, Mapping):
                raise ROIAnalysisError("semantic_rule_results must be a mapping")
            object.__setattr__(
                self,
                "semantic_rule_results",
                _freeze_mapping(
                    self.semantic_rule_results,
                    field="semantic_rule_results",
                ),
            )
        if self.original_predicted_geometry is not None:
            if not isinstance(self.original_predicted_geometry, Mapping):
                raise ROIAnalysisError("original_predicted_geometry must be a mapping")
            object.__setattr__(
                self,
                "original_predicted_geometry",
                _freeze_mapping(
                    self.original_predicted_geometry,
                    field="original_predicted_geometry",
                ),
            )
        if self.registration_id is not None:
            _non_empty(self.registration_id, "registration_id")
        if self.registration_qc is not None:
            if not isinstance(self.registration_qc, Mapping):
                raise ROIAnalysisError("registration_qc must be a mapping")
            object.__setattr__(
                self,
                "registration_qc",
                _freeze_mapping(self.registration_qc, field="registration_qc"),
            )
        if self.source is SourceKind.REGISTERED_REFERENCE_ASSISTED:
            if self.registration_id is None or self.registration_qc is None:
                raise ROIAnalysisError(
                    "registered-reference-assisted ROI records require registration_id and registration_qc"
                )
            if self.registration_qc.get("status") != "passed":
                raise ROIAnalysisError(
                    "registered-reference-assisted ROI records require passing registration_qc"
                )

    def to_export_mapping(self) -> dict[str, Any]:
        """Serialize the public ROI schema, using ``class`` at its boundary."""
        return {
            "roi_id": self.roi_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "class": self.roi_class,
            "roi_level": self.roi_level,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_kind": self.analysis_domain_kind.value,
            "parent_roi_id": self.parent_roi_id,
            "geometry": _export_json(self.geometry),
            "source": _export_json(self.source),
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
            "classifier_confidence": self.classifier_confidence,
            "semantic_profile": self.semantic_profile,
            "semantic_status": _export_json(self.semantic_status),
            "semantic_rule_results": _export_json(self.semantic_rule_results),
            "semantic_classification_status": self.semantic_classification_status,
            "semantic_class_id": self.semantic_class_id,
            "review_rationale": self.review_rationale,
            "review_status": _export_json(self.review_status),
            "original_predicted_geometry": _export_json(self.original_predicted_geometry),
            "registration_id": self.registration_id,
            "registration_qc": _export_json(self.registration_qc),
        }


@dataclass(frozen=True, slots=True)
class CellRecord:
    """Canonical cell export record, located in one tissue section at level 0."""

    cell_id: str
    slide_id: str
    section_id: str
    x_level0: float
    y_level0: float
    x_um: float | None
    y_um: float | None
    nucleus_geometry: Mapping[str, Any]
    cell_geometry: Mapping[str, Any]
    primary_compartment: str | None
    secondary_roi_ids: tuple[str, ...]
    marker_measurements: Mapping[str, Any]
    marker_states: Mapping[str, Any]
    primary_phenotype: str | None = None
    phenotype_components: tuple[str, ...] = ()
    phenotype_source: str | None = None
    cluster_id: str | None = None
    tissue_roi_id: str | None = None
    tissue_generation_id: str | None = None
    phenotyping_generation_id: str | None = None
    phenotype_confidence: float | None = None
    cluster_label: str | None = None
    pipeline_run_id: str | None = None
    membership_run_id: str | None = None
    membership_generation_id: str | None = None
    analysis_domain_id: str = field(kw_only=True)
    analysis_domain_kind: AnalysisDomainKind = field(kw_only=True)

    def __post_init__(self) -> None:
        """Validate the CellRecord fields when the record is constructed.

        Scalar fields retain their declared types, coordinates are finite, and nested
        mappings and sequences are copied into immutable values. Empty optional labels
        remain valid for unassigned cells."""

        for field in ("cell_id", "slide_id", "section_id", "analysis_domain_id"):
            _non_empty(getattr(self, field), field)
        if not isinstance(self.analysis_domain_kind, AnalysisDomainKind):
            raise ROIAnalysisError("analysis_domain_kind must be an AnalysisDomainKind")
        for field in ("nucleus_geometry", "cell_geometry", "marker_measurements", "marker_states"):
            value = getattr(self, field)
            if not isinstance(value, Mapping):
                raise ROIAnalysisError(f"{field} must be a mapping")
            object.__setattr__(self, field, _freeze_mapping(value, field=field))
        for field in ("secondary_roi_ids", "phenotype_components"):
            value = getattr(self, field)
            if not isinstance(value, (list, tuple)):
                raise ROIAnalysisError(f"{field} must be a sequence of strings")
            items = tuple(value)
            if not all(isinstance(item, str) for item in items):
                raise ROIAnalysisError(f"{field} must contain strings")
            object.__setattr__(self, field, items)
        for field in ("x_level0", "y_level0", "x_um", "y_um"):
            value = getattr(self, field)
            if value is None and field in ("x_um", "y_um"):
                continue
            try:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                )
            except OverflowError:
                valid = False
            if not valid:
                raise ROIAnalysisError(f"{field} must be a finite number")
        for field in (
            "primary_compartment",
            "primary_phenotype",
            "phenotype_source",
            "cluster_id",
        ):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise ROIAnalysisError(f"{field} must be a string or None")
        for field in (
            "tissue_roi_id",
            "tissue_generation_id",
            "phenotyping_generation_id",
            "cluster_label",
            "pipeline_run_id",
            "membership_run_id",
            "membership_generation_id",
        ):
            value = getattr(self, field)
            if value is not None:
                _non_empty(value, field)
        if self.phenotype_confidence is not None:
            value = self.phenotype_confidence
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= value <= 1.0
                or not math.isfinite(value)
            ):
                raise ROIAnalysisError("phenotype_confidence must be a finite value in [0, 1]")
            object.__setattr__(self, "phenotype_confidence", float(value))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CellRecord":
        """Parse the strict v2 canonical cell wire mapping."""
        if not isinstance(value, Mapping):
            raise ROIAnalysisError("CellRecord mapping must be a mapping")
        fields = {
            "cell_id",
            "slide_id",
            "section_id",
            "analysis_domain_id",
            "analysis_domain_kind",
            "x_level0",
            "y_level0",
            "x_um",
            "y_um",
            "nucleus_geometry",
            "cell_geometry",
            "primary_compartment",
            "secondary_roi_ids",
            "marker_measurements",
            "marker_states",
            "primary_phenotype",
            "phenotype_components",
            "phenotype_source",
            "cluster_id",
            "tissue_roi_id",
            "tissue_generation_id",
            "phenotyping_generation_id",
            "phenotype_confidence",
            "cluster_label",
            "pipeline_run_id",
            "membership_run_id",
            "membership_generation_id",
        }
        unknown = set(value) - fields
        if unknown:
            raise ROIAnalysisError(
                f"CellRecord mapping contains unknown fields: {sorted(unknown)!r}"
            )
        required = fields - {
            "primary_phenotype",
            "phenotype_components",
            "phenotype_source",
            "cluster_id",
            "tissue_roi_id",
            "tissue_generation_id",
            "phenotyping_generation_id",
            "phenotype_confidence",
            "cluster_label",
            "pipeline_run_id",
            "membership_run_id",
            "membership_generation_id",
        }
        missing = required - set(value)
        if missing:
            raise ROIAnalysisError(f"CellRecord mapping is missing fields: {sorted(missing)!r}")
        payload = dict(value)
        try:
            payload["analysis_domain_kind"] = AnalysisDomainKind(payload["analysis_domain_kind"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ROIAnalysisError("CellRecord analysis_domain_kind is invalid") from exc
        return cls(**payload)

    def to_export_mapping(self) -> dict[str, Any]:
        """Serialize the public cell schema without exposing frozen internals."""
        result = {
            "cell_id": self.cell_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_kind": self.analysis_domain_kind.value,
            "x_level0": self.x_level0,
            "y_level0": self.y_level0,
            "x_um": self.x_um,
            "y_um": self.y_um,
            "nucleus_geometry": _export_json(self.nucleus_geometry),
            "cell_geometry": _export_json(self.cell_geometry),
            "primary_compartment": self.primary_compartment,
            "secondary_roi_ids": _export_json(self.secondary_roi_ids),
            "marker_measurements": _export_json(self.marker_measurements),
            "marker_states": _export_json(self.marker_states),
            "primary_phenotype": self.primary_phenotype,
            "phenotype_components": _export_json(self.phenotype_components),
            "phenotype_source": self.phenotype_source,
            "cluster_id": self.cluster_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_generation_id": self.tissue_generation_id,
            "phenotyping_generation_id": self.phenotyping_generation_id,
            "phenotype_confidence": self.phenotype_confidence,
            "cluster_label": self.cluster_label,
        }
        if any(
            value is not None
            for value in (
                self.pipeline_run_id,
                self.membership_run_id,
                self.membership_generation_id,
            )
        ):
            result.update(
                pipeline_run_id=self.pipeline_run_id,
                membership_run_id=self.membership_run_id,
                membership_generation_id=self.membership_generation_id,
            )
        return result


__all__ = [
    "CellRecord",
    "ROIAnalysisError",
    "ROIRecord",
    "ReviewStatus",
    "SemanticStatus",
    "SourceKind",
    "TissueSection",
    "validate_safe_path_segment",
]
