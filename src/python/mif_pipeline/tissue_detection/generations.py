"""Validation contracts for QuPath-owned tissue-section generations.

The `TissueSectionGeneration` record carries `slide_id`, `generation_id`, `status`,
`canonical_image_id`, `canonical_image_sha256` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any

from mif_pipeline.pipeline_core.config import ReviewPolicy, TissueDetectionConfig
from mif_pipeline.pipeline_core.models import ROIAnalysisError, ReviewStatus, TissueSection

from .resolution import NativeLevelReadAttestation, TissueResolutionSelection

# ==============================================================================
#                                  Generations
# ==============================================================================


class GenerationStatus(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value


def _optional_nonblank(value: object, field: str) -> str | None:
    return None if value is None else _nonblank(value, field)


def _sha256(value: object, field: str) -> str:
    result = _nonblank(value, field)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256")
    return result


def _freeze_json(value: object, field: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError(f"{field} keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item, field) for item in value)
    raise ROIAnalysisError(f"{field} must contain only JSON-like values")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ROIAnalysisError(f"{field} must be a string-keyed mapping")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError(f"{field} must be a sequence")
    return value


def _reject_unknown(mapping: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ROIAnalysisError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if set(mapping) != allowed:
        raise ROIAnalysisError(f"{field} requires every field")


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True, slots=True)
class DetectorValidationRecord:
    detector_id: str
    detector_version: str
    settings_hash: str
    reference_channel_id: str | None
    reference_channel_selection_source: str | None
    stage_version: str
    validation_domain_id: str | None
    validation_image_ids: tuple[str, ...]
    validator_identity: str
    validation_date: str
    qc_thresholds: Mapping[str, float]
    status: str

    def __post_init__(self) -> None:
        for name in (
            "detector_id",
            "detector_version",
            "stage_version",
            "validator_identity",
            "validation_date",
        ):
            _nonblank(getattr(self, name), name)
        _sha256(self.settings_hash, "settings_hash")
        _optional_nonblank(self.reference_channel_id, "reference_channel_id")
        _optional_nonblank(self.validation_domain_id, "validation_domain_id")
        if self.reference_channel_id is None:
            if self.reference_channel_selection_source is not None:
                raise ROIAnalysisError(
                    "reference_channel_selection_source requires reference_channel_id"
                )
        elif self.reference_channel_selection_source not in {"canonical_metadata", "user_override"}:
            raise ROIAnalysisError("reference_channel_selection_source is invalid")
        image_ids = tuple(
            _nonblank(item, "validation_image_ids") for item in self.validation_image_ids
        )
        if not image_ids or len(set(image_ids)) != len(image_ids):
            raise ROIAnalysisError("validation_image_ids must contain unique IDs")
        object.__setattr__(self, "validation_image_ids", image_ids)
        if self.status not in {"provisional", "validated"}:
            raise ROIAnalysisError("detector validation status must be provisional or validated")
        thresholds: dict[str, float] = {}
        if not isinstance(self.qc_thresholds, Mapping):
            raise ROIAnalysisError("qc_thresholds must be a mapping")
        required = {"min_tissue_area_fraction", "max_tissue_area_fraction"}
        if set(self.qc_thresholds) != required:
            raise ROIAnalysisError("qc_thresholds must define validated tissue-area bounds")
        for key, value in self.qc_thresholds.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ROIAnalysisError(f"qc_thresholds.{key} must be finite")
            thresholds[key] = float(value)
        if (
            not 0
            <= thresholds["min_tissue_area_fraction"]
            <= thresholds["max_tissue_area_fraction"]
            <= 1
        ):
            raise ROIAnalysisError("validated tissue-area bounds must be within [0, 1]")
        object.__setattr__(self, "qc_thresholds", MappingProxyType(thresholds))

    @classmethod
    def from_mapping(cls, value: object) -> "DetectorValidationRecord":
        mapping = _mapping(value, "detector validation")
        allowed = {
            "detector_id",
            "detector_version",
            "settings_hash",
            "reference_channel_id",
            "reference_channel_selection_source",
            "stage_version",
            "validation_domain_id",
            "validation_image_ids",
            "validator_identity",
            "validation_date",
            "qc_thresholds",
            "status",
        }
        _reject_unknown(mapping, allowed, "detector validation")
        return cls(
            detector_id=mapping["detector_id"],
            detector_version=mapping["detector_version"],
            settings_hash=mapping["settings_hash"],
            reference_channel_id=mapping["reference_channel_id"],
            reference_channel_selection_source=mapping["reference_channel_selection_source"],
            stage_version=mapping["stage_version"],
            validation_domain_id=mapping["validation_domain_id"],
            validation_image_ids=tuple(
                _sequence(mapping["validation_image_ids"], "validation_image_ids")
            ),
            validator_identity=mapping["validator_identity"],
            validation_date=mapping["validation_date"],
            qc_thresholds=_mapping(mapping["qc_thresholds"], "qc_thresholds"),
            status=mapping["status"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "settings_hash": self.settings_hash,
            "reference_channel_id": self.reference_channel_id,
            "reference_channel_selection_source": self.reference_channel_selection_source,
            "stage_version": self.stage_version,
            "validation_domain_id": self.validation_domain_id,
            "validation_image_ids": list(self.validation_image_ids),
            "validator_identity": self.validator_identity,
            "validation_date": self.validation_date,
            "qc_thresholds": dict(self.qc_thresholds),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class TissueDetectionQC:
    geometry_valid: bool
    component_count: int
    tissue_area_fraction: float
    image_bounds_contained: bool
    minimum_area_compliant: bool

    def __post_init__(self) -> None:
        for field in ("geometry_valid", "image_bounds_contained", "minimum_area_compliant"):
            if not isinstance(getattr(self, field), bool):
                raise ROIAnalysisError(f"{field} must be a boolean")
        if (
            isinstance(self.component_count, bool)
            or not isinstance(self.component_count, int)
            or self.component_count < 0
        ):
            raise ROIAnalysisError("component_count must be a non-negative integer")
        if (
            isinstance(self.tissue_area_fraction, bool)
            or not isinstance(self.tissue_area_fraction, (int, float))
            or not 0 <= float(self.tissue_area_fraction) <= 1
        ):
            raise ROIAnalysisError("tissue_area_fraction must be within [0, 1]")

    def passes(self, thresholds: Mapping[str, float]) -> bool:
        return (
            self.geometry_valid
            and self.component_count > 0
            and thresholds["min_tissue_area_fraction"]
            <= self.tissue_area_fraction
            <= thresholds["max_tissue_area_fraction"]
            and self.image_bounds_contained
            and self.minimum_area_compliant
        )

    @classmethod
    def from_mapping(cls, value: object) -> "TissueDetectionQC":
        mapping = _mapping(value, "tissue QC")
        allowed = {
            "geometry_valid",
            "component_count",
            "tissue_area_fraction",
            "image_bounds_contained",
            "minimum_area_compliant",
        }
        _reject_unknown(mapping, allowed, "tissue QC")
        return cls(**mapping)

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_valid": self.geometry_valid,
            "component_count": self.component_count,
            "tissue_area_fraction": self.tissue_area_fraction,
            "image_bounds_contained": self.image_bounds_contained,
            "minimum_area_compliant": self.minimum_area_compliant,
        }


@dataclass(frozen=True, slots=True)
class TissueSectionGeneration:
    slide_id: str
    generation_id: str
    status: GenerationStatus
    canonical_image_id: str
    canonical_image_sha256: str
    detector_id: str
    detector_version: str
    settings_hash: str
    reference_channel_id: str | None
    reference_channel_selection_source: str | None
    stage_version: str
    tissue_resolution_selection: TissueResolutionSelection
    native_level_read_attestation: NativeLevelReadAttestation
    sections: tuple[TissueSection, ...]
    derived_from_generation_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "slide_id",
            "generation_id",
            "canonical_image_id",
            "detector_id",
            "detector_version",
            "stage_version",
        ):
            _nonblank(getattr(self, field), field)
        _sha256(self.canonical_image_sha256, "canonical_image_sha256")
        _sha256(self.settings_hash, "settings_hash")
        _optional_nonblank(self.reference_channel_id, "reference_channel_id")
        _optional_nonblank(self.derived_from_generation_id, "derived_from_generation_id")
        if not isinstance(self.status, GenerationStatus):
            raise ROIAnalysisError("generation status must be a GenerationStatus")
        selection = self.tissue_resolution_selection
        if not isinstance(selection, TissueResolutionSelection):
            selection = TissueResolutionSelection.from_mapping(selection)
        object.__setattr__(self, "tissue_resolution_selection", selection)
        attestation = self.native_level_read_attestation
        if not isinstance(attestation, NativeLevelReadAttestation):
            attestation = NativeLevelReadAttestation.from_mapping(attestation)
        attestation.validate_selection_binding(selection)
        object.__setattr__(self, "native_level_read_attestation", attestation)
        if self.reference_channel_id is None:
            if self.reference_channel_selection_source is not None:
                raise ROIAnalysisError("reference channel selection source requires a channel")
        elif self.reference_channel_selection_source not in {"canonical_metadata", "user_override"}:
            raise ROIAnalysisError("reference channel selection source is invalid")
        object.__setattr__(self, "sections", tuple(self.sections))

    @property
    def acceptance_context(self) -> tuple[object, ...]:
        """Exact material context that scopes acceptance and checkpoint reuse."""
        return (
            self.canonical_image_id,
            self.canonical_image_sha256,
            self.detector_id,
            self.detector_version,
            self.settings_hash,
            self.reference_channel_id,
            self.reference_channel_selection_source,
            self.stage_version,
            self.tissue_resolution_selection.selection_record_sha256,
            self.native_level_read_attestation.native_resolution_index,
            float(self.native_level_read_attestation.requested_downsample),
            self.native_level_read_attestation.returned_width_px,
            self.native_level_read_attestation.returned_height_px,
            self.native_level_read_attestation.resolution_source,
            self.native_level_read_attestation.coordinate_system,
            self.native_level_read_attestation.selection_record_sha256,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "TissueSectionGeneration":
        mapping = _mapping(value, "tissue generation")
        allowed = {
            "slide_id",
            "generation_id",
            "status",
            "canonical_image_id",
            "canonical_image_sha256",
            "detector_id",
            "detector_version",
            "settings_hash",
            "reference_channel_id",
            "reference_channel_selection_source",
            "stage_version",
            "tissue_resolution_selection",
            "native_level_read_attestation",
            "sections",
            "derived_from_generation_id",
        }
        _reject_unknown(mapping, allowed, "tissue generation")
        try:
            status = GenerationStatus(mapping["status"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("tissue generation status is invalid") from exc
        return cls(
            slide_id=mapping["slide_id"],
            generation_id=mapping["generation_id"],
            status=status,
            canonical_image_id=mapping["canonical_image_id"],
            canonical_image_sha256=mapping["canonical_image_sha256"],
            detector_id=mapping["detector_id"],
            detector_version=mapping["detector_version"],
            settings_hash=mapping["settings_hash"],
            reference_channel_id=mapping["reference_channel_id"],
            reference_channel_selection_source=mapping["reference_channel_selection_source"],
            stage_version=mapping["stage_version"],
            tissue_resolution_selection=TissueResolutionSelection.from_mapping(
                mapping["tissue_resolution_selection"]
            ),
            native_level_read_attestation=NativeLevelReadAttestation.from_mapping(
                mapping["native_level_read_attestation"]
            ),
            sections=tuple(
                TissueSection.from_mapping(item)
                for item in _sequence(mapping["sections"], "tissue generation.sections")
            ),
            derived_from_generation_id=mapping["derived_from_generation_id"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "slide_id": self.slide_id,
            "generation_id": self.generation_id,
            "status": self.status.value,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "settings_hash": self.settings_hash,
            "reference_channel_id": self.reference_channel_id,
            "reference_channel_selection_source": self.reference_channel_selection_source,
            "stage_version": self.stage_version,
            "tissue_resolution_selection": self.tissue_resolution_selection.to_mapping(),
            "native_level_read_attestation": (self.native_level_read_attestation.to_mapping()),
            "sections": [section.to_export_mapping() for section in self.sections],
            "derived_from_generation_id": self.derived_from_generation_id,
        }


def _require_unattended_validation(
    config: TissueDetectionConfig,
    record: DetectorValidationRecord | None,
    qc: TissueDetectionQC | None,
    generation: TissueSectionGeneration | None,
    *,
    expected_canonical_image_id: str | None,
    expected_canonical_image_sha256: str | None,
) -> None:
    expected = (
        config.detector_id,
        config.detector_version,
        config.settings_hash,
        config.reference_channel_id,
        config.reference_channel_selection_source,
        config.stage_version,
        config.validation_domain_id,
    )
    actual = (
        None
        if record is None
        else (
            record.detector_id,
            record.detector_version,
            record.settings_hash,
            record.reference_channel_id,
            record.reference_channel_selection_source,
            record.stage_version,
            record.validation_domain_id,
        )
    )
    if record is None or record.status != "validated" or actual != expected:
        raise ROIAnalysisError(
            "unattended tissue detection requires an exact validated detector record"
        )
    if generation is None:
        raise ROIAnalysisError(
            "unattended tissue detection requires an exact candidate or accepted generation"
        )
    if expected_canonical_image_id is None or expected_canonical_image_sha256 is None:
        raise ROIAnalysisError(
            "unattended tissue detection requires authoritative canonical image identity"
        )
    _nonblank(expected_canonical_image_id, "expected_canonical_image_id")
    _sha256(expected_canonical_image_sha256, "expected_canonical_image_sha256")
    generation_context = (
        generation.canonical_image_id,
        generation.canonical_image_sha256,
        generation.detector_id,
        generation.detector_version,
        generation.settings_hash,
        generation.reference_channel_id,
        generation.reference_channel_selection_source,
        generation.stage_version,
    )
    expected_generation_context = (
        expected_canonical_image_id,
        expected_canonical_image_sha256,
        config.detector_id,
        config.detector_version,
        config.settings_hash,
        config.reference_channel_id,
        config.reference_channel_selection_source,
        config.stage_version,
    )
    if generation_context != expected_generation_context:
        raise ROIAnalysisError("unattended tissue detection requires an exact generation context")
    if qc is None or not qc.passes(record.qc_thresholds):
        raise ROIAnalysisError("unattended tissue detection requires every QC check to pass")


def validate_generation_manifest(
    generations: Sequence[TissueSectionGeneration | Mapping[str, object]],
    *,
    tissue_detection: TissueDetectionConfig | None = None,
    detector_validation: DetectorValidationRecord | None = None,
    tissue_qc: TissueDetectionQC | None = None,
    validation_generation_id: str | None = None,
    expected_canonical_image_id: str | None = None,
    expected_canonical_image_sha256: str | None = None,
) -> tuple[TissueSectionGeneration, ...]:
    """Validate identities, contexts, display allocation, lineage, and acceptance.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    items = tuple(
        (
            item
            if isinstance(item, TissueSectionGeneration)
            else TissueSectionGeneration.from_mapping(item)
        )
        for item in generations
    )
    generation_ids: set[str] = set()
    generation_slides: dict[str, str] = {}
    generation_statuses: dict[str, GenerationStatus] = {}
    accepted_slides: set[str] = set()
    roi_displays: dict[tuple[str, str], int] = {}
    roi_sections: dict[tuple[str, str], str] = {}
    roi_revisions: dict[tuple[str, str], int] = {}
    roi_geometries: dict[tuple[str, str], object] = {}
    section_rois: dict[tuple[str, str], str] = {}
    max_display_by_slide: dict[str, int] = {}
    prior_accepted_roi_ids: set[tuple[str, str]] = set()

    for generation in items:
        if not isinstance(generation, TissueSectionGeneration):
            raise ROIAnalysisError(
                "generation manifest entries must be TissueSectionGeneration records"
            )
        if generation.generation_id in generation_ids:
            raise ROIAnalysisError("generation IDs must be unique")
        if generation.derived_from_generation_id is not None:
            if generation.derived_from_generation_id not in generation_ids:
                raise ROIAnalysisError("generation lineage must reference an earlier generation")
            if generation_slides[generation.derived_from_generation_id] != generation.slide_id:
                raise ROIAnalysisError("generation lineage cannot cross slides")
            if generation_statuses[generation.derived_from_generation_id] not in {
                GenerationStatus.ACCEPTED,
                GenerationStatus.SUPERSEDED,
            }:
                raise ROIAnalysisError(
                    "generation lineage must reference a prior accepted generation"
                )
        generation_ids.add(generation.generation_id)
        generation_slides[generation.generation_id] = generation.slide_id
        generation_statuses[generation.generation_id] = generation.status
        if generation.status is GenerationStatus.ACCEPTED:
            if generation.slide_id in accepted_slides:
                raise ROIAnalysisError("only one accepted generation is allowed per slide")
            accepted_slides.add(generation.slide_id)

        roi_ids = [section.roi_id for section in generation.sections]
        section_ids = [section.section_id for section in generation.sections]
        displays = [section.display_number for section in generation.sections]
        if len(set(roi_ids)) != len(roi_ids) or len(set(section_ids)) != len(section_ids):
            raise ROIAnalysisError("generation section and ROI IDs must be unique")
        if len(set(displays)) != len(displays):
            raise ROIAnalysisError("generation display numbers must be unique")

        expected_context = {
            "canonical_image_id": generation.canonical_image_id,
            "canonical_image_sha256": generation.canonical_image_sha256,
            "detector_id": generation.detector_id,
            "detector_version": generation.detector_version,
            "settings_hash": generation.settings_hash,
            "reference_channel_id": generation.reference_channel_id,
            "reference_channel_selection_source": generation.reference_channel_selection_source,
            "stage_version": generation.stage_version,
            "tissue_resolution_selection": (generation.tissue_resolution_selection.to_mapping()),
            "native_level_read_attestation": (
                generation.native_level_read_attestation.to_mapping()
            ),
        }
        for section in sorted(generation.sections, key=lambda item: item.display_number):
            if (
                section.slide_id != generation.slide_id
                or section.generation_id != generation.generation_id
            ):
                raise ROIAnalysisError("section identity must match its generation")
            if dict(section.detector_context) != expected_context:
                raise ROIAnalysisError("section detector context must exactly match its generation")
            if (
                generation.status is GenerationStatus.ACCEPTED
                and section.review_status is not ReviewStatus.ACCEPTED
            ):
                raise ROIAnalysisError(
                    "accepted generation sections must have accepted review status"
                )
            if (
                generation.status not in {GenerationStatus.ACCEPTED, GenerationStatus.SUPERSEDED}
                and section.supersedes
            ):
                raise ROIAnalysisError(
                    f"{generation.status.value} generation sections cannot declare supersedes"
                )
            key = (section.slide_id, section.roi_id)
            section_key = (section.slide_id, section.section_id)
            if section_key in section_rois and section_rois[section_key] != section.roi_id:
                raise ROIAnalysisError("tissue section IDs cannot be recycled for another ROI")
            if key in roi_displays:
                if (
                    roi_displays[key] != section.display_number
                    or roi_sections[key] != section.section_id
                ):
                    raise ROIAnalysisError(
                        "persisted tissue IDs must preserve section and display identity"
                    )
                if section.revision < roi_revisions[key]:
                    raise ROIAnalysisError("persisted tissue revision cannot decrease")
                if (
                    section.geometry != roi_geometries[key]
                    and section.revision <= roi_revisions[key]
                ):
                    raise ROIAnalysisError("changed tissue geometry requires a revision increment")
            else:
                previous_max = max_display_by_slide.get(section.slide_id, 0)
                if section.display_number <= previous_max:
                    raise ROIAnalysisError("new tissue display numbers cannot be recycled")
                roi_displays[key] = section.display_number
                roi_sections[key] = section.section_id
                section_rois[section_key] = section.roi_id
                max_display_by_slide[section.slide_id] = section.display_number
            for ancestor in (*section.derived_from, *section.supersedes):
                if (section.slide_id, ancestor) not in prior_accepted_roi_ids:
                    raise ROIAnalysisError(
                        "tissue section lineage must reference a prior accepted ROI"
                    )
            roi_revisions[key] = section.revision
            roi_geometries[key] = section.geometry

        if generation.status in {GenerationStatus.ACCEPTED, GenerationStatus.SUPERSEDED}:
            prior_accepted_roi_ids.update(
                (section.slide_id, section.roi_id) for section in generation.sections
            )

    if tissue_detection is not None and tissue_detection.review_policy is ReviewPolicy.UNATTENDED:
        target_generation = next(
            (
                item
                for item in items
                if item.generation_id == validation_generation_id
                and item.status in {GenerationStatus.CANDIDATE, GenerationStatus.ACCEPTED}
            ),
            None,
        )
        _require_unattended_validation(
            tissue_detection,
            detector_validation,
            tissue_qc,
            target_generation,
            expected_canonical_image_id=expected_canonical_image_id,
            expected_canonical_image_sha256=expected_canonical_image_sha256,
        )
    return items
