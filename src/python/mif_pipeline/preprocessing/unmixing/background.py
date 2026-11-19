"""Explicit additive-background resolution for reviewed spectral ROIs.

The `BackgroundSideEvidence` record carries `source_input_id`, `source_image_sha256`,
`measurement_ids`, `values`, `accepted_vector_count` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .reference_models import _record_fields
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re

from .compatibility import (
    QuantitativeTransformabilityReport,
    StructuralCompatibilityReport,
    compare_quantitative_transformability,
    compare_structural_compatibility,
)
from .reference_models import (
    BackgroundModelEvidence,
    BackgroundSpectrum,
    ReferenceInputEvidence,
)
from .reference_rois import AcceptedReferenceRoiSelection
from .reference_sampling import Level0ReadAttestation, RoiSamplingResult
from .typed_checksum import typed_checksum_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================
#                                   Background
# ==============================================================================


class BackgroundResolutionError(ValueError):
    """Typed evidence is malformed or contradictory."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _finite(value: object, label: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{label} must be an exact float")
    result = value
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _nonnegative(value: object, label: str) -> float:
    result = _finite(value, label)
    if result < 0.0:
        raise ValueError(f"{label} must be nonnegative")
    return result


def _primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in _record_fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported background evidence value {type(value).__name__}")


def _digest(value: object, *, omit: str | None = None) -> str:
    if omit is None:
        payload = _primitive(value)
    else:
        if not is_dataclass(value) or isinstance(value, type):
            raise TypeError("checksummed evidence must be a dataclass record")
        payload = {
            item.name: _primitive(getattr(value, item.name))
            for item in _record_fields(value)
            if item.name != omit
        }
    return typed_checksum_sha256(payload)


def _task5_digest(value: object, *, omit: str | None = None) -> str:
    """Recompute the already-published JSON digest without redefining it."""
    if omit is None:
        payload = _primitive(value)
    else:
        payload = {
            item.name: _primitive(getattr(value, item.name))
            for item in _record_fields(value)
            if item.name != omit
        }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _exact_tuple(value: object, item_type: type, label: str, *, empty: bool = False):
    if type(value) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    if not value and not empty:
        raise ValueError(f"{label} must not be empty")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{label} must contain exact {item_type.__name__} records")
    return value


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise TypeError("reason_codes must be a tuple of nonempty exact strings")
    if len(value) != len(set(value)):
        raise ValueError("reason_codes must be unique")
    return value


@dataclass(frozen=True, slots=True)
class SampledReferenceRoi:
    """Exact Task-4 selection joined to its exact Task-5 sampling evidence."""

    selection: AcceptedReferenceRoiSelection
    input_evidence: ReferenceInputEvidence
    sampling: RoiSamplingResult
    read_attestation_sha256: str = field(init=False)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the SampledReferenceRoi fields when the record is constructed.

        The checks cover ``selection``, ``sampling``, ``input_evidence``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if type(self.selection) is not AcceptedReferenceRoiSelection:
            raise TypeError("selection must be an exact AcceptedReferenceRoiSelection")
        if type(self.input_evidence) is not ReferenceInputEvidence:
            raise TypeError("input_evidence must be exact ReferenceInputEvidence")
        if type(self.sampling) is not RoiSamplingResult:
            raise TypeError("sampling must be an exact RoiSamplingResult")
        if (
            _task5_digest(self.sampling, omit="sampling_evidence_sha256")
            != self.sampling.sampling_evidence_sha256
        ):
            raise BackgroundResolutionError("Task-5 sampling evidence SHA-256 is stale")
        selection = self.selection
        sampling = self.sampling
        entry = selection.entry
        roi = selection.roi
        image = entry.image
        if sampling.roi_id != roi.roi_id:
            raise BackgroundResolutionError("sampling ROI identity differs from selection")
        if sampling.roi_review_binding_sha256 != roi.review_binding_sha256:
            raise BackgroundResolutionError("sampling ROI review binding differs")
        if sampling.roi_selection_sha256 != selection.selection_sha256:
            raise BackgroundResolutionError("sampling selection SHA-256 differs")
        if sampling.reference_roi_export_sha256 != selection.export.content_sha256:
            raise BackgroundResolutionError("sampling export SHA-256 differs")
        if sampling.input_evidence_sha256 != _task5_digest(self.input_evidence):
            raise BackgroundResolutionError("sampling input-evidence SHA-256 differs")
        if image.reference_input_id != self.input_evidence.input_id:
            raise BackgroundResolutionError("selection and input evidence identities differ")
        if image.source_file_sha256 != self.input_evidence.source_image_sha256:
            raise BackgroundResolutionError("selection and input evidence source hashes differ")
        if sampling.header.source_file_sha256 != image.source_file_sha256:
            raise BackgroundResolutionError("sampling and selected source hashes differ")
        if sampling.header.canonical_image_id != image.canonical_image_id:
            raise BackgroundResolutionError("sampling and selected canonical image IDs differ")
        if sampling.header.canonical_pixels_id != image.canonical_pixels_id:
            raise BackgroundResolutionError("sampling and selected Pixels IDs differ")
        if (sampling.header.width_px, sampling.header.height_px) != (
            image.width_px,
            image.height_px,
        ):
            raise BackgroundResolutionError("sampling and selected level-0 dimensions differ")
        if sampling.header.channel_count != len(self.input_evidence.axis.measurements):
            raise BackgroundResolutionError("sampling channel count differs from measurement axis")
        if sampling.header.source_path != str(image.source_path.resolve()):
            raise BackgroundResolutionError("sampling and selected source paths differ")
        if sampling.measurement_ids != self.input_evidence.axis.measurement_ids:
            raise BackgroundResolutionError("sampling and input measurement axes differ")
        expected_input_roles = {
            "single_component_reference": {"single_component_reference"},
            "unstained_control": {"unstained_control", "target_unstained_control"},
            "negative_control": {"single_stain_negative_control"},
        }[image.source_role]
        if self.input_evidence.input_role not in expected_input_roles:
            raise BackgroundResolutionError("Task-4 source role and input-evidence role differ")
        attestation = sampling.attestation
        if (
            attestation.canonical_image_id != sampling.header.canonical_image_id
            or attestation.canonical_image_id != image.canonical_image_id
            or attestation.native_pyramid_level_count
            != len(sampling.header.native_pyramid_dimensions)
        ):
            raise BackgroundResolutionError("Task-5 read attestation and header bindings differ")
        attestation_sha = _digest(sampling.attestation)
        object.__setattr__(self, "read_attestation_sha256", attestation_sha)
        object.__setattr__(self, "evidence_sha256", _digest(self, omit="evidence_sha256"))


@dataclass(frozen=True, slots=True)
class OffsetCorrectedAuthority:
    """Reviewed authority that one normalized input has zero additive offset."""

    source_input_id: str
    source_image_sha256: str
    measurement_ids: tuple[str, ...]
    authority_id: str
    review_binding_sha256: str
    accepted: bool
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.source_input_id, "source_input_id")
        _sha256(self.source_image_sha256, "source_image_sha256")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise TypeError("measurement_ids must be a nonempty tuple")
        if any(type(item) is not str or not item for item in self.measurement_ids):
            raise TypeError("measurement_ids must contain exact nonempty strings")
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError("measurement_ids must be unique")
        if self.authority_id != "validated_offset_corrected_v1":
            raise ValueError("unsupported corrected-zero authority")
        _sha256(self.review_binding_sha256, "review_binding_sha256")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        object.__setattr__(self, "evidence_sha256", _digest(self, omit="evidence_sha256"))


@dataclass(frozen=True, slots=True)
class AcceptedBackgroundCompatibility:
    """Reviewed exact pairwise compatibility reports for shared-background QC."""

    structural_report: StructuralCompatibilityReport
    quantitative_report: QuantitativeTransformabilityReport
    review_binding_sha256: str
    accepted: bool
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.structural_report) is not StructuralCompatibilityReport:
            raise TypeError("structural_report must be exact StructuralCompatibilityReport")
        if type(self.quantitative_report) is not QuantitativeTransformabilityReport:
            raise TypeError("quantitative_report must be exact QuantitativeTransformabilityReport")
        _sha256(self.review_binding_sha256, "review_binding_sha256")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        object.__setattr__(self, "evidence_sha256", _digest(self, omit="evidence_sha256"))


@dataclass(frozen=True, slots=True)
class BackgroundEquivalencePolicy:
    policy_version: str
    per_band_absolute_tolerance: float
    per_band_relative_tolerance: float
    l2_absolute_tolerance: float
    l2_relative_tolerance: float
    scale_floor: float
    maximum_relative_roi_heterogeneity: float
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "policy_version")
        for name in (
            "per_band_absolute_tolerance",
            "per_band_relative_tolerance",
            "l2_absolute_tolerance",
            "l2_relative_tolerance",
            "scale_floor",
            "maximum_relative_roi_heterogeneity",
        ):
            _nonnegative(getattr(self, name), name)
        if self.scale_floor <= 0.0:
            raise ValueError("scale_floor must be strictly positive")
        object.__setattr__(self, "content_sha256", _digest(self, omit="content_sha256"))


@dataclass(frozen=True, slots=True)
class BackgroundSideEvidence:
    source_input_id: str
    source_image_sha256: str
    measurement_ids: tuple[str, ...]
    values: tuple[float, ...]
    accepted_vector_count: int
    roi_sampling_evidence_sha256s: tuple[str, ...]
    roi_read_attestation_sha256s: tuple[str, ...]
    source_rois: tuple[SampledReferenceRoi, ...]
    corrected_zero_authority: OffsetCorrectedAuthority | None
    heterogeneity_scale_floor: float
    maximum_relative_roi_heterogeneity: float
    source_kind: str
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the BackgroundSideEvidence fields when the record is constructed.

        The checks cover ``source_input_id``, ``source_image_sha256``, ``source_rois``,
        ``heterogeneity_scale_floor``, ``source_kind``, ``roi_sampling_evidence_sha256s``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        _text(self.source_input_id, "source_input_id")
        _sha256(self.source_image_sha256, "source_image_sha256")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise TypeError("measurement_ids must be a nonempty tuple")
        if type(self.values) is not tuple or len(self.values) != len(self.measurement_ids):
            raise ValueError("background values must match measurement_ids")
        numeric = tuple(_finite(item, "background value") for item in self.values)
        if any(item < 0.0 for item in numeric):
            raise ValueError("background values must be nonnegative")
        if (
            isinstance(self.accepted_vector_count, bool)
            or not isinstance(self.accepted_vector_count, int)
            or self.accepted_vector_count < 0
        ):
            raise ValueError("accepted_vector_count must be a nonnegative integer")
        for name in ("roi_sampling_evidence_sha256s", "roi_read_attestation_sha256s"):
            value = getattr(self, name)
            if type(value) is not tuple or any(
                _SHA256_RE.fullmatch(item) is None for item in value
            ):
                raise TypeError(f"{name} must be a tuple of lowercase SHA-256 values")
        if len(self.roi_sampling_evidence_sha256s) != len(self.roi_read_attestation_sha256s):
            raise ValueError("sampling and attestation evidence counts differ")
        _exact_tuple(self.source_rois, SampledReferenceRoi, "source_rois", empty=True)
        if (
            self.corrected_zero_authority is not None
            and type(self.corrected_zero_authority) is not OffsetCorrectedAuthority
        ):
            raise TypeError("corrected_zero_authority has the wrong exact type")
        scale_floor = _nonnegative(self.heterogeneity_scale_floor, "heterogeneity_scale_floor")
        if scale_floor <= 0.0:
            raise ValueError("heterogeneity_scale_floor must be positive")
        if self.source_kind not in {"background_rois", "corrected_zero_authority"}:
            raise ValueError("unsupported background source_kind")
        if self.source_kind == "background_rois" and not self.roi_sampling_evidence_sha256s:
            raise ValueError("background ROI estimate requires sampling evidence")
        if self.source_kind == "background_rois" and self.accepted_vector_count <= 0:
            raise ValueError("background ROI estimate requires accepted vectors")
        if self.source_kind == "background_rois" and self.corrected_zero_authority is not None:
            raise ValueError("sampled background evidence cannot carry zero authority")
        if self.source_kind == "corrected_zero_authority" and self.roi_sampling_evidence_sha256s:
            raise ValueError("corrected-zero evidence cannot claim sampled background ROIs")
        if self.source_kind == "corrected_zero_authority" and self.accepted_vector_count != 0:
            raise ValueError("corrected-zero authority has no sampled-vector count")
        if self.source_kind == "corrected_zero_authority" and (
            self.source_rois or self.corrected_zero_authority is None
        ):
            raise ValueError("corrected-zero side requires only its exact authority")
        _nonnegative(self.maximum_relative_roi_heterogeneity, "maximum_relative_roi_heterogeneity")
        if self.source_kind == "background_rois":
            if any(
                item.selection.roi.roi_role != "background"
                or item.input_evidence.input_id != self.source_input_id
                or item.input_evidence.source_image_sha256 != self.source_image_sha256
                or item.sampling.measurement_ids != self.measurement_ids
                for item in self.source_rois
            ):
                raise ValueError("background side contains mismatched sampled ROI evidence")
            count = sum(item.sampling.accepted_vector_count for item in self.source_rois)
            totals = tuple(
                math.fsum(item.sampling.sum_vector[index] for item in self.source_rois)
                for index in range(len(self.measurement_ids))
            )
            expected_values = tuple(float(item / count) for item in totals)
            norm = math.sqrt(math.fsum(item * item for item in expected_values))
            expected_heterogeneity = max(
                math.sqrt(
                    math.fsum(
                        (observed - pooled) ** 2
                        for observed, pooled in zip(
                            roi.sampling.mean_vector, expected_values, strict=True
                        )
                    )
                )
                / max(norm, scale_floor)
                for roi in self.source_rois
            )
            if (
                self.accepted_vector_count != count
                or self.values != expected_values
                or self.roi_sampling_evidence_sha256s
                != tuple(item.sampling.sampling_evidence_sha256 for item in self.source_rois)
                or self.roi_read_attestation_sha256s
                != tuple(item.read_attestation_sha256 for item in self.source_rois)
                or self.maximum_relative_roi_heterogeneity != expected_heterogeneity
            ):
                raise ValueError("background side summary contradicts its Task-5 ROI evidence")
        else:
            authority = self.corrected_zero_authority
            assert authority is not None
            if (
                not authority.accepted
                or authority.source_input_id != self.source_input_id
                or authority.source_image_sha256 != self.source_image_sha256
                or authority.measurement_ids != self.measurement_ids
                or any(self.values)
                or self.maximum_relative_roi_heterogeneity != 0.0
            ):
                raise ValueError("corrected-zero side contradicts its authority")
        object.__setattr__(self, "evidence_sha256", _digest(self, omit="evidence_sha256"))


@dataclass(frozen=True, slots=True)
class BackgroundEquivalenceEvidence:
    policy_version: str
    compatibility_evidence_sha256: str
    per_band_absolute_differences: tuple[float, ...]
    per_band_bounds: tuple[float, ...]
    l2_difference: float
    l2_bound: float
    stained_heterogeneity: float
    control_heterogeneity: float
    passed: bool
    reason_codes: tuple[str, ...]
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "policy_version")
        _sha256(self.compatibility_evidence_sha256, "compatibility_evidence_sha256")
        if (
            type(self.per_band_absolute_differences) is not tuple
            or type(self.per_band_bounds) is not tuple
        ):
            raise TypeError("per-band equivalence values must be tuples")
        if (
            len(self.per_band_absolute_differences) != len(self.per_band_bounds)
            or not self.per_band_bounds
        ):
            raise ValueError("per-band equivalence values must be nonempty and aligned")
        for item in (*self.per_band_absolute_differences, *self.per_band_bounds):
            _nonnegative(item, "per-band equivalence value")
        for name in ("l2_difference", "l2_bound", "stained_heterogeneity", "control_heterogeneity"):
            _nonnegative(getattr(self, name), name)
        if type(self.passed) is not bool:
            raise TypeError("passed must be bool")
        reasons = _reason_codes(self.reason_codes)
        if self.passed == bool(reasons):
            raise ValueError("passed equivalence must have no reasons; failed must have reasons")
        object.__setattr__(self, "evidence_sha256", _digest(self, omit="evidence_sha256"))


def _derive_equivalence(
    stained_side: BackgroundSideEvidence,
    control_side: BackgroundSideEvidence,
    compatibility: AcceptedBackgroundCompatibility,
    policy: BackgroundEquivalencePolicy,
) -> BackgroundEquivalenceEvidence:
    if (
        stained_side.source_kind != "background_rois"
        or control_side.source_kind != "background_rois"
        or not stained_side.source_rois
        or not control_side.source_rois
    ):
        raise BackgroundResolutionError(
            "shared-background equivalence requires sampled background ROIs on both sides"
        )
    stained_input = stained_side.source_rois[0].input_evidence
    control_input = control_side.source_rois[0].input_evidence
    expected_structural = compare_structural_compatibility(
        reference_axis=stained_input.axis,
        candidate_axis=control_input.axis,
        reference_signature=stained_input.structural_signature,
        candidate_signature=control_input.structural_signature,
    )
    expected_quantitative = compare_quantitative_transformability(
        reference_signature=stained_input.quantitative_signature,
        candidate_signature=control_input.quantitative_signature,
    )
    if (
        compatibility.structural_report != expected_structural
        or compatibility.quantitative_report != expected_quantitative
    ):
        raise BackgroundResolutionError(
            "compatibility reports do not describe the retained background inputs"
        )
    differences = tuple(
        float(abs(a - b)) for a, b in zip(stained_side.values, control_side.values, strict=True)
    )
    bounds = tuple(
        float(
            policy.per_band_absolute_tolerance
            + policy.per_band_relative_tolerance * max(abs(a), abs(b), policy.scale_floor)
        )
        for a, b in zip(stained_side.values, control_side.values, strict=True)
    )
    l2_difference = float(math.sqrt(math.fsum(item * item for item in differences)))
    stained_norm = math.sqrt(math.fsum(item * item for item in stained_side.values))
    control_norm = math.sqrt(math.fsum(item * item for item in control_side.values))
    l2_bound = float(
        policy.l2_absolute_tolerance
        + policy.l2_relative_tolerance * max(stained_norm, control_norm, policy.scale_floor)
    )
    reasons: list[str] = []
    if (
        not compatibility.accepted
        or not expected_structural.compatible
        or not expected_quantitative.transformable
    ):
        reasons.append("background_pair_incompatible")
    if any(value > bound for value, bound in zip(differences, bounds, strict=True)):
        reasons.append("background_per_band_equivalence_failed")
    if l2_difference > l2_bound:
        reasons.append("background_l2_equivalence_failed")
    if stained_side.maximum_relative_roi_heterogeneity > policy.maximum_relative_roi_heterogeneity:
        reasons.append("stained_background_heterogeneity_failed")
    if control_side.maximum_relative_roi_heterogeneity > policy.maximum_relative_roi_heterogeneity:
        reasons.append("control_background_heterogeneity_failed")
    return BackgroundEquivalenceEvidence(
        policy_version=policy.policy_version,
        compatibility_evidence_sha256=compatibility.evidence_sha256,
        per_band_absolute_differences=differences,
        per_band_bounds=bounds,
        l2_difference=l2_difference,
        l2_bound=l2_bound,
        stained_heterogeneity=stained_side.maximum_relative_roi_heterogeneity,
        control_heterogeneity=control_side.maximum_relative_roi_heterogeneity,
        passed=not reasons,
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class BackgroundModelResolution:
    status: str
    stained_side: BackgroundSideEvidence | None
    control_side: BackgroundSideEvidence | None
    model: BackgroundModelEvidence | None
    equivalence: BackgroundEquivalenceEvidence | None
    compatibility: AcceptedBackgroundCompatibility | None
    policy: BackgroundEquivalencePolicy
    authority_evidence_sha256s: tuple[str, ...]
    reason_codes: tuple[str, ...]
    resolution_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the BackgroundModelResolution fields when the record is constructed.

        The checks cover ``status``, ``reason_codes``, ``authority_evidence_sha256s``,
        ``equivalence``, ``stained_side``, ``control_side``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.status not in {"resolved", "request_required"}:
            raise ValueError("unsupported background resolution status")
        for value, expected, label in (
            (self.stained_side, BackgroundSideEvidence, "stained_side"),
            (self.control_side, BackgroundSideEvidence, "control_side"),
            (self.model, BackgroundModelEvidence, "model"),
            (self.equivalence, BackgroundEquivalenceEvidence, "equivalence"),
            (self.compatibility, AcceptedBackgroundCompatibility, "compatibility"),
        ):
            if value is not None and type(value) is not expected:
                raise TypeError(f"{label} has the wrong exact record type")
        if type(self.policy) is not BackgroundEquivalencePolicy:
            raise TypeError("policy must be exact BackgroundEquivalencePolicy")
        if type(self.authority_evidence_sha256s) is not tuple or any(
            _SHA256_RE.fullmatch(item) is None for item in self.authority_evidence_sha256s
        ):
            raise TypeError("authority_evidence_sha256s must be a tuple of SHA-256 values")
        _reason_codes(self.reason_codes)
        retained_authorities = tuple(
            dict.fromkeys(
                side.corrected_zero_authority.evidence_sha256
                for side in (self.stained_side, self.control_side)
                if side is not None and side.corrected_zero_authority is not None
            )
        )
        if self.authority_evidence_sha256s != retained_authorities:
            raise ValueError("authority evidence SHA-256 values contradict retained sides")
        if self.equivalence is not None:
            if self.stained_side is None or self.control_side is None or self.compatibility is None:
                raise ValueError("equivalence lacks retained sides or compatibility")
            expected_equivalence = _derive_equivalence(
                self.stained_side, self.control_side, self.compatibility, self.policy
            )
            if self.equivalence != expected_equivalence:
                raise ValueError("equivalence contradicts its policy, sides, or compatibility")
        elif self.compatibility is not None:
            raise ValueError("compatibility cannot be retained without derived equivalence")
        if self.status == "resolved":
            if (
                self.model is None
                or self.stained_side is None
                or self.control_side is None
                or self.reason_codes
            ):
                raise ValueError("resolved background requires both sides/model and no reasons")
            if self.equivalence is not None and (
                self.compatibility is None
                or self.equivalence.compatibility_evidence_sha256
                != self.compatibility.evidence_sha256
            ):
                raise ValueError("equivalence lacks its exact compatibility evidence")
            if (
                self.model.stained_background.source_input_id != self.stained_side.source_input_id
                or self.model.stained_background.source_image_sha256
                != self.stained_side.source_image_sha256
                or self.model.control_background.source_input_id
                != self.control_side.source_input_id
                or self.model.control_background.source_image_sha256
                != self.control_side.source_image_sha256
            ):
                raise ValueError("resolved background model and side identities differ")
            if self.model.policy == "validated_offset_corrected_v1":
                expected_model_sha = _digest(
                    (
                        self.stained_side,
                        self.control_side,
                        self.authority_evidence_sha256s,
                        self.policy,
                    )
                )
                if (
                    self.stained_side.source_kind != "corrected_zero_authority"
                    or self.control_side.source_kind != "corrected_zero_authority"
                    or self.model.stained_background.values != self.stained_side.values
                    or self.model.control_background.values != self.control_side.values
                    or self.equivalence is not None
                    or self.model.evidence_sha256 != expected_model_sha
                    or self.model.shared_offset_equivalence_sha256 is not None
                ):
                    raise ValueError("corrected-zero model contradicts side evidence")
            elif self.model.policy == "validated_shared_offset_v1":
                if self.equivalence is None or not self.equivalence.passed:
                    raise ValueError("shared background requires passing equivalence")
                count = (
                    self.stained_side.accepted_vector_count
                    + self.control_side.accepted_vector_count
                )
                expected = tuple(
                    (
                        a * self.stained_side.accepted_vector_count
                        + b * self.control_side.accepted_vector_count
                    )
                    / count
                    for a, b in zip(self.stained_side.values, self.control_side.values, strict=True)
                )
                if (
                    self.model.stained_background.values != expected
                    or self.model.control_background.values != expected
                ):
                    raise ValueError("shared background is not the pooled-pixel estimate")
                if (
                    self.model.shared_offset_equivalence_sha256 != self.equivalence.evidence_sha256
                    or self.model.evidence_sha256 != self.equivalence.evidence_sha256
                ):
                    raise ValueError("shared background model does not bind exact equivalence")
            elif self.model.policy == "image_specific_offsets_v1":
                expected_model_sha = _digest(
                    (
                        self.stained_side,
                        self.control_side,
                        self.equivalence,
                        self.authority_evidence_sha256s,
                        self.policy,
                    )
                )
                if (
                    self.model.stained_background.values != self.stained_side.values
                    or self.model.control_background.values != self.control_side.values
                    or self.model.shared_offset_equivalence_sha256 is not None
                    or self.model.evidence_sha256 != expected_model_sha
                    or (self.equivalence is not None and self.equivalence.passed)
                ):
                    raise ValueError("image-specific background contradicts retained evidence")
            else:
                raise ValueError("unsupported resolved background-model policy")
        elif self.model is not None or not self.reason_codes:
            raise ValueError("background request must omit model and include reasons")
        object.__setattr__(self, "resolution_sha256", _digest(self, omit="resolution_sha256"))


def _zero_side(
    context: SampledReferenceRoi, authority: OffsetCorrectedAuthority
) -> BackgroundSideEvidence:
    evidence = context.input_evidence
    if not authority.accepted:
        raise BackgroundResolutionError("corrected-zero authority was not accepted")
    if (
        authority.source_input_id != evidence.input_id
        or authority.source_image_sha256 != evidence.source_image_sha256
        or authority.measurement_ids != evidence.axis.measurement_ids
    ):
        raise BackgroundResolutionError("corrected-zero authority identity differs from input")
    return BackgroundSideEvidence(
        source_input_id=evidence.input_id,
        source_image_sha256=evidence.source_image_sha256,
        measurement_ids=evidence.axis.measurement_ids,
        values=tuple(0.0 for _ in evidence.axis.measurement_ids),
        accepted_vector_count=0,
        roi_sampling_evidence_sha256s=(),
        roi_read_attestation_sha256s=(),
        source_rois=(),
        corrected_zero_authority=authority,
        heterogeneity_scale_floor=1.0,
        maximum_relative_roi_heterogeneity=0.0,
        source_kind="corrected_zero_authority",
    )


def _pooled_side(
    context: SampledReferenceRoi,
    rois: tuple[SampledReferenceRoi, ...],
    *,
    scale_floor: float,
) -> BackgroundSideEvidence | None:
    if not rois:
        return None
    axis = context.input_evidence.axis.measurement_ids
    total_count = 0
    totals = [0.0] * len(axis)
    for roi in rois:
        if roi.selection.roi.roi_role != "background":
            raise BackgroundResolutionError("background estimate received a non-background ROI")
        if roi.input_evidence.input_id != context.input_evidence.input_id:
            raise BackgroundResolutionError("background ROI belongs to the wrong input")
        if roi.input_evidence.source_image_sha256 != context.input_evidence.source_image_sha256:
            raise BackgroundResolutionError("background ROI source differs from context")
        if roi.sampling.measurement_ids != axis:
            raise BackgroundResolutionError("background ROI measurement axis differs")
        total_count += roi.sampling.accepted_vector_count
        totals = [
            current + value for current, value in zip(totals, roi.sampling.sum_vector, strict=True)
        ]
    values = tuple(float(value / total_count) for value in totals)
    if any(not math.isfinite(item) or item < 0.0 for item in values):
        return None
    pooled_norm = math.sqrt(math.fsum(item * item for item in values))
    heterogeneity = max(
        math.sqrt(
            math.fsum(
                (observed - pooled) ** 2
                for observed, pooled in zip(roi.sampling.mean_vector, values, strict=True)
            )
        )
        / max(pooled_norm, scale_floor)
        for roi in rois
    )
    return BackgroundSideEvidence(
        source_input_id=context.input_evidence.input_id,
        source_image_sha256=context.input_evidence.source_image_sha256,
        measurement_ids=axis,
        values=values,
        accepted_vector_count=total_count,
        roi_sampling_evidence_sha256s=tuple(roi.sampling.sampling_evidence_sha256 for roi in rois),
        roi_read_attestation_sha256s=tuple(roi.read_attestation_sha256 for roi in rois),
        source_rois=rois,
        corrected_zero_authority=None,
        heterogeneity_scale_floor=scale_floor,
        maximum_relative_roi_heterogeneity=float(heterogeneity),
        source_kind="background_rois",
    )


def _authority_for(
    context: SampledReferenceRoi,
    authorities: tuple[OffsetCorrectedAuthority, ...],
) -> OffsetCorrectedAuthority | None:
    matches = tuple(
        item for item in authorities if item.source_input_id == context.input_evidence.input_id
    )
    if len(matches) > 1:
        raise BackgroundResolutionError("duplicate corrected-zero authority for input")
    return matches[0] if matches and matches[0].accepted else None


def resolve_background_model(
    stained_context: SampledReferenceRoi,
    control_context: SampledReferenceRoi,
    stained_background_rois: tuple[SampledReferenceRoi, ...],
    control_background_rois: tuple[SampledReferenceRoi, ...],
    corrected_zero_authorities: tuple[OffsetCorrectedAuthority, ...],
    compatibility: AcceptedBackgroundCompatibility | None,
    policy: BackgroundEquivalencePolicy,
) -> BackgroundModelResolution:
    """Resolve zero, shared, or image-specific offsets without silent fallback."""

    if (
        type(stained_context) is not SampledReferenceRoi
        or type(control_context) is not SampledReferenceRoi
    ):
        raise TypeError(
            "stained_context and control_context must be exact SampledReferenceRoi records"
        )
    _exact_tuple(
        stained_background_rois, SampledReferenceRoi, "stained_background_rois", empty=True
    )
    _exact_tuple(
        control_background_rois, SampledReferenceRoi, "control_background_rois", empty=True
    )
    _exact_tuple(
        corrected_zero_authorities,
        OffsetCorrectedAuthority,
        "corrected_zero_authorities",
        empty=True,
    )
    if compatibility is not None and type(compatibility) is not AcceptedBackgroundCompatibility:
        raise TypeError("compatibility has the wrong exact record type")
    if type(policy) is not BackgroundEquivalencePolicy:
        raise TypeError("policy must be exact BackgroundEquivalencePolicy")
    stained_axis = stained_context.input_evidence.axis.measurement_ids
    if stained_axis != control_context.input_evidence.axis.measurement_ids:
        raise BackgroundResolutionError("stained and control measurement axes differ")

    stained_authority = _authority_for(stained_context, corrected_zero_authorities)
    control_authority = _authority_for(control_context, corrected_zero_authorities)
    stained_side = (
        _zero_side(stained_context, stained_authority)
        if stained_authority is not None
        else _pooled_side(stained_context, stained_background_rois, scale_floor=policy.scale_floor)
    )
    control_side = (
        _zero_side(control_context, control_authority)
        if control_authority is not None
        else _pooled_side(control_context, control_background_rois, scale_floor=policy.scale_floor)
    )
    authorities = tuple(
        dict.fromkeys(
            item.evidence_sha256
            for item in (stained_authority, control_authority)
            if item is not None
        )
    )
    missing: list[str] = []
    if stained_side is None:
        missing.append("stained_background_model_required")
    if control_side is None:
        missing.append("control_background_model_required")
    if missing:
        return BackgroundModelResolution(
            status="request_required",
            stained_side=stained_side,
            control_side=control_side,
            model=None,
            equivalence=None,
            compatibility=None,
            policy=policy,
            authority_evidence_sha256s=authorities,
            reason_codes=tuple(missing),
        )

    assert stained_side is not None and control_side is not None
    if stained_side.source_kind == control_side.source_kind == "corrected_zero_authority":
        evidence_sha = _digest((stained_side, control_side, authorities, policy))
        model = BackgroundModelEvidence(
            policy="validated_offset_corrected_v1",
            stained_background=BackgroundSpectrum(
                source_input_id=stained_side.source_input_id,
                source_image_sha256=stained_side.source_image_sha256,
                values=stained_side.values,
            ),
            control_background=BackgroundSpectrum(
                source_input_id=control_side.source_input_id,
                source_image_sha256=control_side.source_image_sha256,
                values=control_side.values,
            ),
            shared_offset_equivalence_sha256=None,
            evidence_sha256=evidence_sha,
        )
        return BackgroundModelResolution(
            status="resolved",
            stained_side=stained_side,
            control_side=control_side,
            model=model,
            equivalence=None,
            compatibility=None,
            policy=policy,
            authority_evidence_sha256s=authorities,
            reason_codes=(),
        )

    equivalence: BackgroundEquivalenceEvidence | None = None
    can_consider_shared = (
        stained_side.source_kind == control_side.source_kind == "background_rois"
        and compatibility is not None
    )
    if can_consider_shared:
        assert compatibility is not None
        equivalence = _derive_equivalence(stained_side, control_side, compatibility, policy)

    if equivalence is not None and equivalence.passed:
        combined_count = stained_side.accepted_vector_count + control_side.accepted_vector_count
        shared = tuple(
            float(
                (a * stained_side.accepted_vector_count + b * control_side.accepted_vector_count)
                / combined_count
            )
            for a, b in zip(stained_side.values, control_side.values, strict=True)
        )
        model = BackgroundModelEvidence(
            policy="validated_shared_offset_v1",
            stained_background=BackgroundSpectrum(
                stained_side.source_input_id, stained_side.source_image_sha256, shared
            ),
            control_background=BackgroundSpectrum(
                control_side.source_input_id, control_side.source_image_sha256, shared
            ),
            shared_offset_equivalence_sha256=equivalence.evidence_sha256,
            evidence_sha256=equivalence.evidence_sha256,
        )
    else:
        model_evidence_sha = _digest((stained_side, control_side, equivalence, authorities, policy))
        model = BackgroundModelEvidence(
            policy="image_specific_offsets_v1",
            stained_background=BackgroundSpectrum(
                stained_side.source_input_id, stained_side.source_image_sha256, stained_side.values
            ),
            control_background=BackgroundSpectrum(
                control_side.source_input_id, control_side.source_image_sha256, control_side.values
            ),
            shared_offset_equivalence_sha256=None,
            evidence_sha256=model_evidence_sha,
        )
    return BackgroundModelResolution(
        status="resolved",
        stained_side=stained_side,
        control_side=control_side,
        model=model,
        equivalence=equivalence,
        compatibility=compatibility if equivalence is not None else None,
        policy=policy,
        authority_evidence_sha256s=authorities,
        reason_codes=(),
    )


__all__ = [
    "AcceptedBackgroundCompatibility",
    "BackgroundEquivalenceEvidence",
    "BackgroundEquivalencePolicy",
    "BackgroundModelResolution",
    "BackgroundResolutionError",
    "BackgroundSideEvidence",
    "OffsetCorrectedAuthority",
    "SampledReferenceRoi",
    "resolve_background_model",
]
