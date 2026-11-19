"""Tissue-section candidate, review, and lineage policy.

The `TissueMatchMetrics` record carries `iou`, `centroid_displacement_um`,
`relative_area_change`."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from mif_pipeline.pipeline_core.config import (
    AnalysisConfig,
    TissueDetectionConfig,
    TissueMatchingConfig,
)
from mif_pipeline.pipeline_core.errors import ProjectPreparationError
from mif_pipeline.pipeline_core.models import ReviewStatus, TissueSection
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult
from mif_pipeline.qupath_runtime.project import ProjectLayout

from .generations import GenerationStatus, TissueSectionGeneration
from .resolution import (
    NativeLevelReadAttestation,
    TissueResolutionSelection,
)

# ==============================================================================
#                                    Project
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TissueCandidate:
    """One QuPath-produced candidate geometry before persistent reconciliation."""

    candidate_key: str
    geometry: Mapping[str, object]
    provenance_pointer: str

    def __post_init__(self) -> None:
        for field in ("candidate_key", "provenance_pointer"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ProjectPreparationError(f"{field} must be a non-empty string")
        if not isinstance(self.geometry, Mapping):
            raise ProjectPreparationError("candidate geometry must be a mapping")
        # TissueSection performs the canonical recursive JSON validation/freezing.
        object.__setattr__(self, "geometry", MappingProxyType(dict(self.geometry)))


@dataclass(frozen=True, slots=True)
class TissueMatchMetrics:
    """QuPath/JTS pairwise geometry measurements used for identity matching."""

    iou: float
    centroid_displacement_um: float
    relative_area_change: float

    def __post_init__(self) -> None:
        for field in ("iou", "centroid_displacement_um", "relative_area_change"):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ProjectPreparationError(f"{field} must be finite")
        if not 0 <= self.iou <= 1:
            raise ProjectPreparationError("iou must be within [0, 1]")
        if self.centroid_displacement_um < 0 or self.relative_area_change < 0:
            raise ProjectPreparationError("match distances and area change must be non-negative")


def resolve_tissue_detection_prerequisites(
    config: AnalysisConfig,
    project: ProjectLayout,
    *,
    available_channel_ids: Sequence[str],
) -> StageResult | None:
    """Return a resumable pre-generation interaction, or ``None`` when ready.

    This checks only deterministic orchestration prerequisites. QuPath remains
    the owner of pixel thresholding and PathObject geometry.
    """
    available = tuple(available_channel_ids)
    if any(not isinstance(value, str) or not value.strip() for value in available) or len(
        set(available)
    ) != len(available):
        raise ProjectPreparationError("available channel IDs must be unique non-empty strings")
    tissue = config.tissue_detection
    if tissue.reference_channel_id is None or tissue.reference_channel_id not in available:
        return StageResult.needs_interaction(
            stage="tissue_detection",
            slide_id=config.slide_id,
            reason=InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED,
            message="Select a channel to use only as the QuPath tissue reference",
            allowed_actions=("select_channel",),
            resume_context={
                "schema_version": "tissue-detection-channel-interaction/v1",
                "checkpoint_id": "cp-tissue-channel",
                "available_channel_ids": list(available),
            },
            checkpoint_id="cp-tissue-channel",
        )
    classifier_path = project.pixel_classifiers_dir / tissue.classifier_name
    classifier_matches = (
        classifier_path.is_file() and _stream_sha256(classifier_path) == tissue.classifier_sha256
    )
    if not classifier_matches:
        return StageResult.needs_interaction(
            stage="tissue_detection",
            slide_id=config.slide_id,
            reason=InteractionReason.OTHER_EXPLICIT_REASON,
            message="Create the configured saved native QuPath tissue thresholder",
            allowed_actions=("create_thresholder",),
            resume_context={
                "schema_version": "tissue-thresholder-interaction/v1",
                "checkpoint_id": "cp-tissue-thresholder",
                "project_path": str(project.project_file),
                "classifier_path": str(classifier_path),
                "reference_channel_id": tissue.reference_channel_id,
            },
            checkpoint_id="cp-tissue-thresholder",
        )
    return None


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reconcile_tissue_candidates(
    *,
    slide_id: str,
    generation_id: str,
    candidates: Sequence[TissueCandidate],
    detector_context: Mapping[str, object],
    matching: TissueMatchingConfig,
    accepted: TissueSectionGeneration | None,
    metrics: Mapping[tuple[str, str], TissueMatchMetrics],
    max_display_number: int,
    identity_factory: Callable[[str, str], str] | None = None,
) -> TissueSectionGeneration:
    """Assign persistent IDs without using candidate order as biological identity.

    Pairwise measurements are produced by QuPath/JTS, which owns the tissue
    geometries. This function executes the deterministic identity policy and is
    deliberately pixel-free.
    """
    if not isinstance(slide_id, str) or not slide_id.strip():
        raise ProjectPreparationError("slide_id must be a non-empty string")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ProjectPreparationError("generation_id must be a non-empty string")
    required_context = {
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
    }
    if not isinstance(detector_context, Mapping) or set(detector_context) != required_context:
        raise ProjectPreparationError("tissue detector context requires every exact material field")
    tissue_resolution_selection = TissueResolutionSelection.from_mapping(
        detector_context["tissue_resolution_selection"]
    )
    native_level_read_attestation = NativeLevelReadAttestation.from_mapping(
        detector_context["native_level_read_attestation"]
    )
    native_level_read_attestation.validate_selection_binding(tissue_resolution_selection)
    candidate_items = tuple(candidates)
    if len({item.candidate_key for item in candidate_items}) != len(candidate_items):
        raise ProjectPreparationError("candidate keys must be unique")
    if (
        isinstance(max_display_number, bool)
        or not isinstance(max_display_number, int)
        or max_display_number < 0
    ):
        raise ProjectPreparationError("max_display_number must be a non-negative integer")
    if accepted is not None:
        if accepted.slide_id != slide_id or accepted.status is not GenerationStatus.ACCEPTED:
            raise ProjectPreparationError(
                "repeat matching requires the accepted generation for this slide"
            )
        if dict(detector_context) != _generation_context(accepted):
            raise ProjectPreparationError(
                "repeat matching requires an exact material detector context"
            )
    factory = identity_factory or (lambda kind, _key: f"{kind}-{uuid4()}")
    old_sections = () if accepted is None else accepted.sections
    old_by_roi = {item.roi_id: item for item in old_sections}
    candidate_keys = {item.candidate_key for item in candidate_items}
    expected_metric_pairs = {
        (old.roi_id, candidate.candidate_key)
        for old in old_sections
        for candidate in candidate_items
    }
    if set(metrics) != expected_metric_pairs:
        raise ProjectPreparationError(
            "repeat matching requires a complete accepted-by-candidate metric matrix"
        )
    for pair, value in metrics.items():
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or pair[0] not in old_by_roi
            or pair[1] not in candidate_keys
            or not isinstance(value, TissueMatchMetrics)
        ):
            raise ProjectPreparationError(
                "match metrics must reference known old ROI and candidate IDs"
            )

    eligible: set[tuple[str, str]] = set()
    for pair, value in metrics.items():
        if (
            value.iou >= matching.min_iou
            and value.centroid_displacement_um <= matching.max_centroid_displacement_um
            and value.relative_area_change <= matching.max_relative_area_change
        ):
            eligible.add(pair)

    def unique_pair(old_roi: str, candidate_key: str) -> bool:
        if (old_roi, candidate_key) not in eligible:
            return False
        old_candidates = [pair for pair in eligible if pair[0] == old_roi]
        candidate_olds = [pair for pair in eligible if pair[1] == candidate_key]
        if len(old_candidates) != 1 or len(candidate_olds) != 1:
            return False
        selected = metrics[(old_roi, candidate_key)].iou
        alternate_for_old = max(
            (
                value.iou
                for (roi, key), value in metrics.items()
                if roi == old_roi and key != candidate_key
            ),
            default=0.0,
        )
        alternate_for_candidate = max(
            (
                value.iou
                for (roi, key), value in metrics.items()
                if key == candidate_key and roi != old_roi
            ),
            default=0.0,
        )
        return (
            selected - alternate_for_old >= matching.min_uniqueness_margin
            and selected - alternate_for_candidate >= matching.min_uniqueness_margin
        )

    inherited_by_candidate: dict[str, TissueSection] = {}
    for old in old_sections:
        for candidate in candidate_items:
            if unique_pair(old.roi_id, candidate.candidate_key):
                inherited_by_candidate[candidate.candidate_key] = old

    next_display = max(
        max_display_number,
        max((item.display_number for item in old_sections), default=0),
    )
    sections: list[TissueSection] = []
    for candidate in candidate_items:
        inherited = inherited_by_candidate.get(candidate.candidate_key)
        if inherited is not None:
            geometry_changed = _json_value(inherited.geometry) != _json_value(candidate.geometry)
            section_id = inherited.section_id
            roi_id = inherited.roi_id
            display_number = inherited.display_number
            revision = inherited.revision + int(geometry_changed)
            derived_from: tuple[str, ...] = ()
        else:
            next_display += 1
            section_id = factory("section", candidate.candidate_key)
            roi_id = factory("roi", candidate.candidate_key)
            display_number = next_display
            revision = 0
            derived_from = tuple(
                item.roi_id
                for item in old_sections
                if (metric := metrics.get((item.roi_id, candidate.candidate_key))) is not None
                and metric.iou > 0
            )
        sections.append(
            TissueSection(
                slide_id=slide_id,
                section_id=section_id,
                roi_id=roi_id,
                path_object_id=candidate.candidate_key,
                display_number=display_number,
                generation_id=generation_id,
                revision=revision,
                review_status=ReviewStatus.NEEDS_REVIEW,
                detector_context=detector_context,
                provenance_pointer=candidate.provenance_pointer,
                geometry=candidate.geometry,
                derived_from=derived_from,
            )
        )
    return TissueSectionGeneration(
        slide_id=slide_id,
        generation_id=generation_id,
        status=GenerationStatus.CANDIDATE,
        canonical_image_id=detector_context["canonical_image_id"],
        canonical_image_sha256=detector_context["canonical_image_sha256"],
        detector_id=detector_context["detector_id"],
        detector_version=detector_context["detector_version"],
        settings_hash=detector_context["settings_hash"],
        reference_channel_id=detector_context["reference_channel_id"],
        reference_channel_selection_source=detector_context["reference_channel_selection_source"],
        stage_version=detector_context["stage_version"],
        tissue_resolution_selection=tissue_resolution_selection,
        native_level_read_attestation=native_level_read_attestation,
        sections=tuple(sections),
        derived_from_generation_id=None if accepted is None else accepted.generation_id,
    )


def edit_tissue_candidate(
    generation: TissueSectionGeneration,
    *,
    roi_id: str,
    geometry: Mapping[str, object],
    provenance_pointer: str,
) -> TissueSectionGeneration:
    """Record a manual correction while preserving biological identity."""
    if generation.status is not GenerationStatus.CANDIDATE:
        raise ProjectPreparationError("only a candidate generation can be edited")
    found = False
    sections: list[TissueSection] = []
    for section in generation.sections:
        if section.roi_id == roi_id:
            found = True
            if _json_value(section.geometry) == _json_value(geometry):
                sections.append(section)
                continue
            sections.append(
                replace(
                    section.with_corrected_geometry(
                        geometry, review_status=ReviewStatus.NEEDS_REVIEW
                    ),
                    provenance_pointer=provenance_pointer,
                )
            )
        else:
            sections.append(section)
    if not found:
        raise ProjectPreparationError(f"candidate ROI does not exist: {roi_id}")
    if all(before is after for before, after in zip(generation.sections, sections)):
        return generation
    return replace(generation, sections=tuple(sections))


def accept_tissue_candidate(
    accepted: TissueSectionGeneration | None,
    candidate: TissueSectionGeneration,
    *,
    expected_context: Mapping[str, object],
    excluded_roi_ids: Sequence[str] = (),
) -> tuple[TissueSectionGeneration, ...]:
    """Promote the retained candidate sections without changing their geometry.

    Optional exclusions identify current candidate ROIs. The original candidate
    remains unchanged, and at least one section must enter the accepted inventory.
    """
    if dict(expected_context) != _generation_context(candidate):
        raise ProjectPreparationError("candidate acceptance requires an exact material context")
    if (isinstance(excluded_roi_ids, (str, bytes)) or not isinstance(excluded_roi_ids, Sequence)
            or any(type(item) is not str or not item for item in excluded_roi_ids)
            or len(set(excluded_roi_ids)) != len(excluded_roi_ids)):
        raise ProjectPreparationError("excluded tissue ROI IDs must be a unique sequence")
    excluded = set(excluded_roi_ids)
    if not excluded <= {section.roi_id for section in candidate.sections}:
        raise ProjectPreparationError("only current candidate tissue sections can be excluded")
    if excluded and not any(section.roi_id not in excluded for section in candidate.sections):
        raise ProjectPreparationError("keep at least one tissue section before accepting")
    if candidate.status is GenerationStatus.ACCEPTED:
        if excluded:
            raise ProjectPreparationError("only a candidate generation can change tissue inclusion")
        if accepted is candidate or (
            accepted is not None
            and accepted.generation_id == candidate.generation_id
            and accepted == candidate
        ):
            return (candidate,)
        raise ProjectPreparationError(
            "an accepted candidate must already be the authoritative generation"
        )
    if candidate.status is not GenerationStatus.CANDIDATE:
        raise ProjectPreparationError("only a candidate generation can be accepted")
    if accepted is not None and (
        accepted.status is not GenerationStatus.ACCEPTED
        or accepted.slide_id != candidate.slide_id
        or candidate.derived_from_generation_id != accepted.generation_id
    ):
        raise ProjectPreparationError("candidate does not derive from the authoritative generation")
    promoted = replace(
        candidate,
        status=GenerationStatus.ACCEPTED,
        sections=tuple(
            replace(
                section,
                review_status=ReviewStatus.ACCEPTED,
                supersedes=section.derived_from,
            )
            for section in candidate.sections
            if section.roi_id not in excluded
        ),
    )
    if accepted is None:
        return (promoted,)
    return (replace(accepted, status=GenerationStatus.SUPERSEDED), promoted)


def reject_tissue_candidate(candidate: TissueSectionGeneration) -> TissueSectionGeneration:
    """Retain a rejected generation for audit without changing accepted state."""
    if candidate.status is GenerationStatus.REJECTED:
        return candidate
    if candidate.status is not GenerationStatus.CANDIDATE:
        raise ProjectPreparationError("only a candidate generation can be rejected")
    return replace(
        candidate,
        status=GenerationStatus.REJECTED,
        sections=tuple(
            replace(section, review_status=ReviewStatus.REJECTED) for section in candidate.sections
        ),
    )


def _generation_context(generation: TissueSectionGeneration) -> dict[str, object]:
    return {
        "canonical_image_id": generation.canonical_image_id,
        "canonical_image_sha256": generation.canonical_image_sha256,
        "detector_id": generation.detector_id,
        "detector_version": generation.detector_version,
        "settings_hash": generation.settings_hash,
        "reference_channel_id": generation.reference_channel_id,
        "reference_channel_selection_source": generation.reference_channel_selection_source,
        "stage_version": generation.stage_version,
        "tissue_resolution_selection": (generation.tissue_resolution_selection.to_mapping()),
        "native_level_read_attestation": (generation.native_level_read_attestation.to_mapping()),
    }


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "NativeLevelReadAttestation",
    "ProjectPreparationError",
    "TissueCandidate",
    "TissueMatchMetrics",
    "accept_tissue_candidate",
    "edit_tissue_candidate",
    "reconcile_tissue_candidates",
    "reject_tissue_candidate",
    "resolve_tissue_detection_prerequisites",
]
