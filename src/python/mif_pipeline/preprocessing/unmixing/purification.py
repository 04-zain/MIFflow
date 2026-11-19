"""Reviewed dark-band AF subtraction and equal-ROI fluorophore consensus.

The `PurifiedReferenceRoi` record carries `positive`, `control`, `background_resolution`,
`match_review`, `negative_fallback_review` and its remaining declared fields.

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
import math
from pathlib import Path
import re

from .background import BackgroundModelResolution, SampledReferenceRoi
from .compatibility import (
    QuantitativeTransformabilityReport,
    StructuralCompatibilityReport,
    compare_quantitative_transformability,
    compare_structural_compatibility,
)
from .typed_checksum import typed_checksum_sha256
from .reference_workflow import ReferenceWorkflowCheckpoint

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================
#                                  Purification
# ==============================================================================


class ReferencePurificationError(ValueError):
    """Purification inputs are malformed or scientifically contradictory."""


class AfControlSignalError(ReferencePurificationError):
    """Valid sampled evidence cannot supply a nonzero AF control shape."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha(value: object, label: str) -> str:
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
    raise TypeError(f"unsupported purification evidence value {type(value).__name__}")


def _digest(value: object, omit: str | None = None) -> str:
    if omit is None:
        payload = _primitive(value)
    else:
        payload = {
            item.name: _primitive(getattr(value, item.name))
            for item in _record_fields(value)
            if item.name != omit
        }
    return typed_checksum_sha256(payload)


def _typed_tuple(value: object, item_type: type, label: str, *, empty: bool = False):
    if type(value) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    if not value and not empty:
        raise ValueError(f"{label} must not be empty")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{label} must contain exact {item_type.__name__} records")
    return value


def _reasons(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise TypeError("reason_codes must be a tuple of exact nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError("reason_codes must be unique")
    return value


def _numeric_tuple(value: object, label: str, length: int) -> tuple[float, ...]:
    if type(value) is not tuple or len(value) != length:
        raise ValueError(f"{label} must be a tuple matching the measurement axis")
    if any(type(item) is not float for item in value):
        raise TypeError(f"{label} must contain exact floats")
    for item in value:
        _finite(item, label)
    return value


def _safe_fsum(values) -> float | None:
    try:
        result = math.fsum(values)
    except (OverflowError, ValueError):
        return None
    return float(result) if math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class AfRegionalMean:
    """Equal ROI mean in corrected intensity units, with one final rectification."""

    signed_vectors: tuple[tuple[float, ...], ...]
    roi_weights: tuple[float, ...] = field(init=False)
    signed_mean: tuple[float, ...] = field(init=False)
    corrected_mean: tuple[float, ...] = field(init=False)
    normalized_shape: tuple[float, ...] = field(init=False)

    def __post_init__(self) -> None:
        vectors = self.signed_vectors
        if type(vectors) is not tuple or not vectors:
            raise ValueError("AF regional means require a nonempty tuple")
        length = len(vectors[0])
        if not length:
            raise ValueError("AF regional means require measurements")
        for vector in vectors:
            _numeric_tuple(vector, "signed regional AF mean", length)
            norm = math.hypot(*(max(value, 0.0) for value in vector))
            if not math.isfinite(norm) or norm <= 0.0:
                raise AfControlSignalError("Every AF region must have a finite nonzero corrected spectrum")
        weights = tuple(1.0 / len(vectors) for _ in vectors)
        # Divide before summation so finite large regional means do not overflow.
        signed = tuple(float(math.fsum(vector[index] / len(vectors) for vector in vectors))
                       for index in range(length))
        corrected = tuple(max(value, 0.0) for value in signed)
        norm = math.hypot(*corrected)
        if not math.isfinite(norm) or norm <= 0.0:
            raise AfControlSignalError("The combined AF mean must have a finite nonzero spectrum")
        object.__setattr__(self, "roi_weights", weights)
        object.__setattr__(self, "signed_mean", signed)
        object.__setattr__(self, "corrected_mean", corrected)
        object.__setattr__(self, "normalized_shape", tuple(value / norm for value in corrected))


def combine_af_regional_means(signed_vectors: tuple[tuple[float, ...], ...]) -> AfRegionalMean:
    """Average signed, image-background-corrected ROI means with equal ROI weights."""
    return AfRegionalMean(signed_vectors)


@dataclass(frozen=True, slots=True)
class MatchedControlReviewEvidence:
    positive_roi_id: str
    control_roi_id: str
    match_kind: str
    positive_sampling_evidence_sha256: str
    control_sampling_evidence_sha256: str
    structural_report: StructuralCompatibilityReport
    quantitative_report: QuantitativeTransformabilityReport
    review_binding_sha256: str
    accepted: bool
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.positive_roi_id, "positive_roi_id")
        _text(self.control_roi_id, "control_roi_id")
        if self.match_kind not in {"dedicated_af", "negative_fallback"}:
            raise ValueError("unsupported control match_kind")
        _sha(self.positive_sampling_evidence_sha256, "positive_sampling_evidence_sha256")
        _sha(self.control_sampling_evidence_sha256, "control_sampling_evidence_sha256")
        if type(self.structural_report) is not StructuralCompatibilityReport:
            raise TypeError("structural_report must be exact StructuralCompatibilityReport")
        if type(self.quantitative_report) is not QuantitativeTransformabilityReport:
            raise TypeError("quantitative_report must be exact QuantitativeTransformabilityReport")
        _sha(self.review_binding_sha256, "review_binding_sha256")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


@dataclass(frozen=True, slots=True)
class AfControlSetMember:
    """One real region, its own image correction and exact positive/control review."""

    control: SampledReferenceRoi
    background_resolution: BackgroundModelResolution
    match_review: MatchedControlReviewEvidence

    def __post_init__(self) -> None:
        if type(self.control) is not SampledReferenceRoi or self.control.selection.roi.roi_role != "af":
            raise ValueError("AF control sets require sampled AF regions")
        if type(self.background_resolution) is not BackgroundModelResolution:
            raise TypeError("AF member background must be exact")
        model = self.background_resolution.model
        if self.background_resolution.status != "resolved" or model is None:
            raise ValueError("AF control sets require resolved image backgrounds")
        if (model.control_background.source_input_id != self.control.input_evidence.input_id
                or model.control_background.source_image_sha256 != self.control.input_evidence.source_image_sha256):
            raise ValueError("AF member background does not bind its source")
        if type(self.match_review) is not MatchedControlReviewEvidence:
            raise TypeError("AF member match review must be exact")
        if (not self.match_review.accepted or self.match_review.match_kind != "dedicated_af"
                or self.match_review.control_roi_id != self.control.selection.roi.roi_id
                or self.match_review.control_sampling_evidence_sha256 != self.control.sampling.sampling_evidence_sha256):
            raise ValueError("AF member match review differs from the sampled control")

    @property
    def signed_values(self) -> tuple[float, ...]:
        return tuple(float(value - background) for value, background in zip(
            self.control.sampling.mean_vector,
            self.background_resolution.model.control_background.values, strict=True))


@dataclass(frozen=True, slots=True)
class AfControlSetEvidence:
    """Reviewed membership, equal ROI weights and signed-before-clipping estimate."""

    positive: SampledReferenceRoi
    members: tuple[AfControlSetMember, ...]
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.positive) is not SampledReferenceRoi or self.positive.selection.roi.roi_role != "positive":
            raise ValueError("AF control set requires one sampled positive region")
        _typed_tuple(self.members, AfControlSetMember, "AF control set members")
        if len(self.roi_ids) != len(set(self.roi_ids)):
            raise ValueError("AF control set contains duplicate regions")
        stained_backgrounds = []
        for member in self.members:
            review, control = member.match_review, member.control
            if (review.positive_roi_id != self.positive.selection.roi.roi_id
                    or review.positive_sampling_evidence_sha256 != self.positive.sampling.sampling_evidence_sha256
                    or control.sampling.measurement_ids != self.positive.sampling.measurement_ids):
                raise ValueError("AF member does not bind this positive region and measurement axis")
            structural = compare_structural_compatibility(
                reference_axis=self.positive.input_evidence.axis, candidate_axis=control.input_evidence.axis,
                reference_signature=self.positive.input_evidence.structural_signature,
                candidate_signature=control.input_evidence.structural_signature)
            quantitative = compare_quantitative_transformability(
                reference_signature=self.positive.input_evidence.quantitative_signature,
                candidate_signature=control.input_evidence.quantitative_signature)
            if (review.structural_report != structural or review.quantitative_report != quantitative
                    or not structural.compatible or not quantitative.transformable):
                raise ValueError("AF control set contains incompatible measurements")
            stained = member.background_resolution.model.stained_background
            if (stained.source_input_id != self.positive.input_evidence.input_id
                    or stained.source_image_sha256 != self.positive.input_evidence.source_image_sha256):
                raise ValueError("AF control set positive background does not bind its source")
            stained_backgrounds.append(stained)
        if len(set(stained_backgrounds)) != 1:
            raise ValueError("One positive region requires one exact image background")
        combined = combine_af_regional_means(self.regional_signed_values)
        object.__setattr__(self, "evidence_sha256", typed_checksum_sha256({
            "semantic": "equal_roi_signed_mean_control_set_v1",
            "positive_evidence_sha256": self.positive.evidence_sha256,
            "members": _primitive(self.members), "roi_weights": list(combined.roi_weights),
            "signed_corrected_mean": list(combined.signed_mean),
            "corrected_mean": list(combined.corrected_mean),
            "normalized_shape": list(combined.normalized_shape)}))

    @property
    def roi_ids(self):
        return tuple(item.control.selection.roi.roi_id for item in self.members)

    @property
    def regional_signed_values(self):
        return tuple(item.signed_values for item in self.members)

    @property
    def regional_normalized_shapes(self):
        return tuple(combine_af_regional_means((values,)).normalized_shape for values in self.regional_signed_values)

    @property
    def roi_weights(self):
        return tuple(1.0 / len(self.members) for _ in self.members)

    @property
    def signed_corrected_mean(self):
        return combine_af_regional_means(self.regional_signed_values).signed_mean

    @property
    def corrected_mean(self):
        return combine_af_regional_means(self.regional_signed_values).corrected_mean

    @property
    def normalized_shape(self):
        return combine_af_regional_means(self.regional_signed_values).normalized_shape


@dataclass(frozen=True, slots=True)
class NegativeFallbackReviewEvidence:
    negative_roi_id: str
    positive_roi_id: str
    signal_measurement_ids: tuple[str, ...]
    fluorophore_identity: str
    positive_input_id: str
    control_input_id: str
    positive_sampling_evidence_sha256: str
    control_sampling_evidence_sha256: str
    positive_source_image_sha256: str
    control_source_image_sha256: str
    fluorophore_negativity_ratio: float
    negativity_scale_floor: float
    anatomically_matched: bool
    negativity_authority_sha256: str
    anatomical_match_authority_sha256: str
    accepted: bool
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.negative_roi_id, "negative_roi_id")
        _text(self.positive_roi_id, "positive_roi_id")
        if type(self.signal_measurement_ids) is not tuple or not self.signal_measurement_ids:
            raise TypeError("signal_measurement_ids must be a nonempty tuple")
        if any(type(item) is not str or not item for item in self.signal_measurement_ids):
            raise TypeError("signal_measurement_ids must contain exact strings")
        if len(self.signal_measurement_ids) != len(set(self.signal_measurement_ids)):
            raise ValueError("signal_measurement_ids must be unique")
        _text(self.fluorophore_identity, "fluorophore_identity")
        _text(self.positive_input_id, "positive_input_id")
        _text(self.control_input_id, "control_input_id")
        for name in (
            "positive_sampling_evidence_sha256",
            "control_sampling_evidence_sha256",
            "positive_source_image_sha256",
            "control_source_image_sha256",
            "negativity_authority_sha256",
            "anatomical_match_authority_sha256",
        ):
            _sha(getattr(self, name), name)
        _nonnegative(self.fluorophore_negativity_ratio, "fluorophore_negativity_ratio")
        if _nonnegative(self.negativity_scale_floor, "negativity_scale_floor") <= 0.0:
            raise ValueError("negativity_scale_floor must be positive")
        if type(self.anatomically_matched) is not bool or type(self.accepted) is not bool:
            raise TypeError("fallback review decisions must be bool")
        if self.accepted and not self.anatomically_matched:
            raise ValueError("accepted negative fallback requires anatomical review")
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


@dataclass(frozen=True, slots=True)
class ReviewedDarkMeasurement:
    measurement_id: str
    review_evidence_sha256: str
    reliability_weight: float = field(init=False, default=1.0)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.measurement_id, "measurement_id")
        _sha(self.review_evidence_sha256, "review_evidence_sha256")
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


@dataclass(frozen=True, slots=True)
class SignalDarkMeasurementReviewEvidence:
    signal_measurement_id: str
    dark_measurement_ids: tuple[str, ...]
    excluded_measurement_ids: tuple[str, ...]
    fluorophore_identity: str
    positive_roi_id: str
    control_roi_id: str
    positive_input_id: str
    control_input_id: str
    positive_sampling_evidence_sha256: str
    control_sampling_evidence_sha256: str
    positive_source_image_sha256: str
    control_source_image_sha256: str
    reviewer_id: str
    source_checkpoint_sha256: str
    result_checkpoint_sha256: str
    decision_record_sha256: str
    accepted: bool
    authority_sha256: str = field(init=False)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the SignalDarkMeasurementReviewEvidence fields when the record is
        constructed.

        The checks cover ``signal_measurement_id``, ``dark_measurement_ids``,
        ``excluded_measurement_ids``, ``accepted``, ``source_checkpoint_sha256``,
        ``result_checkpoint_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        _text(self.signal_measurement_id, "signal_measurement_id")
        if type(self.dark_measurement_ids) is not tuple:
            raise TypeError("dark_measurement_ids must be a tuple")
        if not self.dark_measurement_ids:
            raise ValueError("dark_measurement_ids must not be empty")
        if any(type(item) is not str or not item for item in self.dark_measurement_ids):
            raise TypeError("dark_measurement_ids must contain exact nonempty strings")
        if len(self.dark_measurement_ids) != len(set(self.dark_measurement_ids)):
            raise ValueError("dark_measurement_ids must be unique")
        if self.signal_measurement_id in self.dark_measurement_ids:
            raise ValueError("signal measurement cannot also be dark")
        if type(self.excluded_measurement_ids) is not tuple:
            raise TypeError("excluded_measurement_ids must be a tuple")
        if any(type(item) is not str or not item for item in self.excluded_measurement_ids):
            raise TypeError("excluded_measurement_ids must contain exact nonempty strings")
        if len(self.excluded_measurement_ids) != len(set(self.excluded_measurement_ids)):
            raise ValueError("excluded_measurement_ids must be unique")
        if self.signal_measurement_id in self.excluded_measurement_ids or set(
            self.dark_measurement_ids
        ) & set(self.excluded_measurement_ids):
            raise ValueError("signal, dark, and excluded measurements must be disjoint")
        for name in (
            "fluorophore_identity",
            "positive_roi_id",
            "control_roi_id",
            "positive_input_id",
            "control_input_id",
            "reviewer_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "positive_sampling_evidence_sha256",
            "control_sampling_evidence_sha256",
            "positive_source_image_sha256",
            "control_source_image_sha256",
            "source_checkpoint_sha256",
            "result_checkpoint_sha256",
            "decision_record_sha256",
        ):
            _sha(getattr(self, name), name)
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        object.__setattr__(
            self,
            "authority_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "signal_dark_measurement_review_binding_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "result_checkpoint_sha256": self.result_checkpoint_sha256,
                    "decision_sha256": self.decision_record_sha256,
                    "positive_sampling_evidence_sha256": (self.positive_sampling_evidence_sha256),
                    "control_sampling_evidence_sha256": (self.control_sampling_evidence_sha256),
                    "signal_measurement_id": self.signal_measurement_id,
                    "dark_measurement_ids": list(self.dark_measurement_ids),
                    "excluded_measurement_ids": list(self.excluded_measurement_ids),
                }
            ),
        )
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


@dataclass(frozen=True, slots=True)
class PurificationPolicy:
    policy_version: str
    denominator_tolerance: float
    af_minimum_l2_norm: float
    scale_floor: float
    purified_minimum_l2_norm: float
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "policy_version")
        for name in (
            "denominator_tolerance",
            "af_minimum_l2_norm",
            "scale_floor",
            "purified_minimum_l2_norm",
        ):
            _nonnegative(getattr(self, name), name)
        if self.scale_floor <= 0.0:
            raise ValueError("scale_floor must be positive")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class NegativeResidualClamp:
    measurement_id: str
    raw_value: float
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.measurement_id, "measurement_id")
        if _finite(self.raw_value, "raw_value") >= 0.0:
            raise ValueError("clamp raw_value must be negative")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class DarkBandSensitivity:
    omitted_measurement_id: str
    status: str
    leave_one_out_denominator: float | None
    af_subtraction_factor_without_band: float | None
    absolute_factor_change: float | None
    leave_one_out_omitted_component: float | None
    leave_one_out_signal_component: float | None
    diagnostic_numerator: float | None
    diagnostic_denominator: float | None
    diagnostic_ratio: float | None
    content_sha256: str = field(init=False)

    @property
    def denominator_sufficient(self) -> bool:
        """Compatibility projection; availability is not a scientific pass decision."""

        return self.status == "available"

    def __post_init__(self) -> None:
        _text(self.omitted_measurement_id, "omitted_measurement_id")
        if self.status not in {"available", "unavailable", "not_applicable"}:
            raise ValueError("unsupported dark-band diagnostic status")
        if self.leave_one_out_denominator is not None:
            _nonnegative(self.leave_one_out_denominator, "leave_one_out_denominator")
        optional = (
            self.af_subtraction_factor_without_band,
            self.absolute_factor_change,
            self.leave_one_out_omitted_component,
            self.leave_one_out_signal_component,
            self.diagnostic_numerator,
            self.diagnostic_denominator,
            self.diagnostic_ratio,
        )
        if self.status == "available":
            if self.leave_one_out_denominator is None or any(item is None for item in optional):
                raise ValueError("available dark-band diagnostic requires every component")
            _nonnegative(
                self.af_subtraction_factor_without_band, "af_subtraction_factor_without_band"
            )
            _nonnegative(self.absolute_factor_change, "absolute_factor_change")
            _finite(self.leave_one_out_omitted_component, "leave_one_out_omitted_component")
            _finite(self.leave_one_out_signal_component, "leave_one_out_signal_component")
            _nonnegative(self.diagnostic_numerator, "diagnostic_numerator")
            if _nonnegative(self.diagnostic_denominator, "diagnostic_denominator") <= 0.0:
                raise ValueError("diagnostic_denominator must be positive")
            _nonnegative(self.diagnostic_ratio, "diagnostic_ratio")
        elif any(item is not None for item in optional):
            raise ValueError("unavailable dark-band diagnostic cannot carry components")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class _PurificationDerivation:
    background_corrected_control: tuple[float, ...]
    af_subtraction_factor: float | None
    raw_residual: tuple[float, ...] | None
    negative_residual_clamps: tuple[NegativeResidualClamp, ...]
    dark_band_sensitivity: tuple[DarkBandSensitivity, ...]
    purified_spectrum: tuple[float, ...] | None
    disposition: str
    reason_codes: tuple[str, ...]


def _derive_purification(
    *,
    z: tuple[float, ...],
    raw_a: tuple[float, ...],
    measurement_ids: tuple[str, ...],
    signal_measurement_id: str,
    dark_measurements: tuple[ReviewedDarkMeasurement, ...],
    policy: PurificationPolicy,
) -> _PurificationDerivation:
    # Background is removed from regional means before imposing nonnegativity.
    a = tuple(max(_finite(value, "control mean minus background"), 0.0) for value in raw_a)
    a_norm = math.hypot(*a)
    if not math.isfinite(a_norm):
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("background_corrected_af_norm_nonfinite",),
        )
    if a_norm <= policy.af_minimum_l2_norm:
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("background_corrected_af_near_zero",),
        )
    index = {item: position for position, item in enumerate(measurement_ids)}
    numerator = _safe_fsum(
        a[index[item.measurement_id]] * z[index[item.measurement_id]] for item in dark_measurements
    )
    denominator = _safe_fsum(a[index[item.measurement_id]] ** 2 for item in dark_measurements)
    if denominator is None:
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("dark_band_denominator_nonfinite",),
        )
    if numerator is None:
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("dark_band_numerator_nonfinite",),
        )
    if denominator <= policy.denominator_tolerance:
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("dark_band_denominator_below_tolerance",),
        )
    alpha_ratio = numerator / denominator
    if not math.isfinite(alpha_ratio):
        return _PurificationDerivation(
            a,
            None,
            None,
            (),
            (),
            None,
            "failed",
            ("af_subtraction_factor_nonfinite",),
        )
    alpha = max(0.0, alpha_ratio)
    sensitivities: list[DarkBandSensitivity] = []
    for omitted in dark_measurements:
        retained = tuple(
            item for item in dark_measurements if item.measurement_id != omitted.measurement_id
        )
        leave_denominator = _safe_fsum(a[index[item.measurement_id]] ** 2 for item in retained)
        if len(dark_measurements) == 1:
            sensitivities.append(
                DarkBandSensitivity(
                    omitted.measurement_id,
                    "not_applicable",
                    leave_denominator,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
        elif leave_denominator is None or (leave_denominator <= policy.denominator_tolerance):
            sensitivities.append(
                DarkBandSensitivity(
                    omitted.measurement_id,
                    "unavailable",
                    leave_denominator,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
        else:
            leave_numerator = _safe_fsum(
                a[index[item.measurement_id]] * z[index[item.measurement_id]] for item in retained
            )
            leave_ratio = None if leave_numerator is None else leave_numerator / leave_denominator
            leave_alpha = (
                None
                if leave_ratio is None or not math.isfinite(leave_ratio)
                else max(0.0, leave_ratio)
            )
            omitted_position = index[omitted.measurement_id]
            signal_position = index[signal_measurement_id]
            omitted_component = (
                None
                if leave_alpha is None
                else z[omitted_position] - leave_alpha * a[omitted_position]
            )
            signal_component = (
                None
                if leave_alpha is None
                else z[signal_position] - leave_alpha * a[signal_position]
            )
            factor_change = None if leave_alpha is None else abs(leave_alpha - alpha)
            if (
                leave_alpha is None
                or factor_change is None
                or omitted_component is None
                or signal_component is None
                or not all(
                    math.isfinite(item)
                    for item in (
                        leave_alpha,
                        factor_change,
                        omitted_component,
                        signal_component,
                    )
                )
            ):
                sensitivities.append(
                    DarkBandSensitivity(
                        omitted.measurement_id,
                        "unavailable",
                        leave_denominator,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                )
                continue
            diagnostic_numerator = max(0.0, omitted_component)
            diagnostic_denominator = max(signal_component, policy.scale_floor)
            diagnostic_ratio = diagnostic_numerator / diagnostic_denominator
            if not math.isfinite(diagnostic_ratio):
                sensitivities.append(
                    DarkBandSensitivity(
                        omitted.measurement_id,
                        "unavailable",
                        leave_denominator,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                )
                continue
            sensitivities.append(
                DarkBandSensitivity(
                    omitted.measurement_id,
                    "available",
                    leave_denominator,
                    float(leave_alpha),
                    float(factor_change),
                    float(omitted_component),
                    float(signal_component),
                    float(diagnostic_numerator),
                    float(diagnostic_denominator),
                    float(diagnostic_ratio),
                )
            )
    residual_values: list[float] = []
    for value, af_value in zip(z, a, strict=True):
        subtraction = alpha * af_value
        residual_value = value - subtraction
        if not math.isfinite(subtraction) or not math.isfinite(residual_value):
            return _PurificationDerivation(
                a,
                float(alpha),
                None,
                (),
                tuple(sensitivities),
                None,
                "failed",
                ("purified_residual_nonfinite",),
            )
        residual_values.append(float(residual_value))
    residual = tuple(residual_values)
    purified = list(residual)
    clamps: list[NegativeResidualClamp] = []
    # The least-squares AF fit can leave signed noise in fluorophore-dark
    # bands. Retain that residual for review, then impose the nonnegative
    # reference spectrum without a relative test against a near-zero band.
    for position, value in enumerate(residual):
        if value < 0.0:
            clamps.append(NegativeResidualClamp(measurement_ids[position], value))
            purified[position] = 0.0
    norm_squared = _safe_fsum(item * item for item in purified)
    if norm_squared is None:
        return _PurificationDerivation(
            a,
            float(alpha),
            residual,
            tuple(clamps),
            tuple(sensitivities),
            None,
            "failed",
            ("purified_spectrum_norm_nonfinite",),
        )
    norm = math.sqrt(norm_squared)
    if norm <= policy.purified_minimum_l2_norm:
        return _PurificationDerivation(
            a,
            float(alpha),
            residual,
            tuple(clamps),
            tuple(sensitivities),
            None,
            "failed",
            ("purified_spectrum_near_zero",),
        )
    return _PurificationDerivation(
        a,
        float(alpha),
        residual,
        tuple(clamps),
        tuple(sensitivities),
        tuple(float(item / norm) for item in purified),
        "pass",
        (),
    )


def _validate_review_lineage(
    *,
    positive: SampledReferenceRoi,
    control: SampledReferenceRoi,
    signal_dark_review: SignalDarkMeasurementReviewEvidence,
    negative_fallback_review: NegativeFallbackReviewEvidence | None,
    background_corrected_positive: tuple[float, ...],
    background_corrected_control: tuple[float, ...],
) -> None:
    """Validate review lineage for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if signal_dark_review.evidence_sha256 != _digest(signal_dark_review, "evidence_sha256"):
        raise ReferencePurificationError("signal/dark review authority is stale")
    fluorophore = positive.input_evidence.fluorophore_identity
    expected_dark_binding = (
        fluorophore,
        positive.selection.roi.roi_id,
        control.selection.roi.roi_id,
        positive.input_evidence.input_id,
        control.input_evidence.input_id,
        positive.sampling.sampling_evidence_sha256,
        control.sampling.sampling_evidence_sha256,
        positive.input_evidence.source_image_sha256,
        control.input_evidence.source_image_sha256,
    )
    actual = (
        signal_dark_review.fluorophore_identity,
        signal_dark_review.positive_roi_id,
        signal_dark_review.control_roi_id,
        signal_dark_review.positive_input_id,
        signal_dark_review.control_input_id,
        signal_dark_review.positive_sampling_evidence_sha256,
        signal_dark_review.control_sampling_evidence_sha256,
        signal_dark_review.positive_source_image_sha256,
        signal_dark_review.control_source_image_sha256,
    )
    if actual != expected_dark_binding:
        raise ReferencePurificationError(
            "signal/dark review does not bind the exact sampled positive/control pair"
        )

    fallback = negative_fallback_review
    if fallback is None:
        return
    expected_fallback_binding = (
        control.selection.roi.roi_id,
        positive.selection.roi.roi_id,
        fluorophore,
        positive.input_evidence.input_id,
        control.input_evidence.input_id,
        positive.sampling.sampling_evidence_sha256,
        control.sampling.sampling_evidence_sha256,
        positive.input_evidence.source_image_sha256,
        control.input_evidence.source_image_sha256,
    )
    actual_fallback_binding = (
        fallback.negative_roi_id,
        fallback.positive_roi_id,
        fallback.fluorophore_identity,
        fallback.positive_input_id,
        fallback.control_input_id,
        fallback.positive_sampling_evidence_sha256,
        fallback.control_sampling_evidence_sha256,
        fallback.positive_source_image_sha256,
        fallback.control_source_image_sha256,
    )
    if actual_fallback_binding != expected_fallback_binding:
        raise ReferencePurificationError(
            "negative-fallback review does not bind the exact sampled positive/control pair"
        )
    if (control.input_evidence.input_role != "single_stain_negative_control"
            or positive.input_evidence.fluorophore_identity != control.input_evidence.fluorophore_identity):
        raise ReferencePurificationError("Negative control does not match the stained fluorophore")
    index = {item: position for position, item in enumerate(positive.sampling.measurement_ids)}
    if any(item not in index for item in fallback.signal_measurement_ids):
        raise ReferencePurificationError("negative-fallback signal measurement is absent")
    control_norm = math.sqrt(
        math.fsum(
            background_corrected_control[index[item]] ** 2
            for item in fallback.signal_measurement_ids
        )
    )
    positive_norm = math.sqrt(
        math.fsum(
            background_corrected_positive[index[item]] ** 2
            for item in fallback.signal_measurement_ids
        )
    )
    expected_ratio = float(control_norm / max(positive_norm, fallback.negativity_scale_floor))
    if fallback.fluorophore_negativity_ratio != expected_ratio:
        raise ReferencePurificationError(
            "negative-fallback ratio contradicts the bound Task-5 vectors"
        )


@dataclass(frozen=True, slots=True)
class PurifiedReferenceRoi:
    positive: SampledReferenceRoi
    control: SampledReferenceRoi
    background_resolution: BackgroundModelResolution
    match_review: MatchedControlReviewEvidence
    negative_fallback_review: NegativeFallbackReviewEvidence | None
    signal_dark_review: SignalDarkMeasurementReviewEvidence
    dark_measurements: tuple[ReviewedDarkMeasurement, ...]
    policy: PurificationPolicy
    measurement_ids: tuple[str, ...]
    measured_positive: tuple[float, ...]
    measured_control: tuple[float, ...]
    background_corrected_positive: tuple[float, ...]
    background_corrected_control: tuple[float, ...]
    af_subtraction_factor: float | None
    raw_residual: tuple[float, ...] | None
    negative_residual_clamps: tuple[NegativeResidualClamp, ...]
    dark_band_sensitivity: tuple[DarkBandSensitivity, ...]
    purified_spectrum: tuple[float, ...] | None
    disposition: str
    reason_codes: tuple[str, ...]
    control_set: AfControlSetEvidence | None = field(default=None, metadata={"omit_empty": True})
    control_signal_dark_reviews: tuple[SignalDarkMeasurementReviewEvidence, ...] = field(default=(), metadata={"omit_empty": True})
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the PurifiedReferenceRoi fields when the record is constructed.

        The checks cover ``dark_measurements``, ``measurement_ids``,
        ``af_subtraction_factor``, ``raw_residual``, ``negative_residual_clamps``,
        ``dark_band_sensitivity``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (
            type(self.positive) is not SampledReferenceRoi
            or type(self.control) is not SampledReferenceRoi
        ):
            raise TypeError("positive/control must be exact SampledReferenceRoi records")
        if type(self.background_resolution) is not BackgroundModelResolution:
            raise TypeError("background_resolution has the wrong exact type")
        if type(self.match_review) is not MatchedControlReviewEvidence:
            raise TypeError("match_review has the wrong exact type")
        if (
            self.negative_fallback_review is not None
            and type(self.negative_fallback_review) is not NegativeFallbackReviewEvidence
        ):
            raise TypeError("negative_fallback_review has the wrong exact type")
        if type(self.signal_dark_review) is not SignalDarkMeasurementReviewEvidence:
            raise TypeError("signal_dark_review has the wrong exact type")
        _typed_tuple(self.dark_measurements, ReviewedDarkMeasurement, "dark_measurements")
        if type(self.policy) is not PurificationPolicy:
            raise TypeError("policy must be exact PurificationPolicy")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise TypeError("measurement_ids must be a nonempty tuple")
        length = len(self.measurement_ids)
        for name in (
            "measured_positive",
            "measured_control",
            "background_corrected_positive",
            "background_corrected_control",
        ):
            _numeric_tuple(getattr(self, name), name, length)
        if self.af_subtraction_factor is not None:
            _nonnegative(self.af_subtraction_factor, "af_subtraction_factor")
        if self.raw_residual is not None:
            _numeric_tuple(self.raw_residual, "raw_residual", length)
        _typed_tuple(
            self.negative_residual_clamps,
            NegativeResidualClamp,
            "negative_residual_clamps",
            empty=True,
        )
        _typed_tuple(
            self.dark_band_sensitivity, DarkBandSensitivity, "dark_band_sensitivity", empty=True
        )
        if self.purified_spectrum is not None:
            spectrum = _numeric_tuple(self.purified_spectrum, "purified_spectrum", length)
            if any(item < 0.0 for item in spectrum):
                raise ValueError("purified_spectrum must be nonnegative")
            norm = math.sqrt(math.fsum(item * item for item in spectrum))
            if not math.isclose(norm, 1.0, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("purified_spectrum must have unit L2 norm")
        if self.disposition not in {"pass", "failed"}:
            raise ValueError("unsupported purification disposition")
        reasons = _reasons(self.reason_codes)
        if self.disposition == "pass" and (reasons or self.purified_spectrum is None):
            raise ValueError("passing purification requires spectrum and no reasons")
        if self.disposition == "failed" and (not reasons or self.purified_spectrum is not None):
            raise ValueError("failed purification requires reasons and no spectrum")
        if (
            self.positive.selection.roi.roi_role != "positive"
            or self.control.selection.roi.roi_role not in {"af", "negative"}
        ):
            raise ValueError("purification ROI roles contradict the evidence")
        control_role = self.control.selection.roi.roi_role
        expected_kind = "negative_fallback" if control_role == "negative" else "dedicated_af"
        if self.match_review.match_kind != expected_kind:
            raise ValueError("control match kind contradicts the retained ROI role")
        expected_structural = compare_structural_compatibility(
            reference_axis=self.positive.input_evidence.axis,
            candidate_axis=self.control.input_evidence.axis,
            reference_signature=self.positive.input_evidence.structural_signature,
            candidate_signature=self.control.input_evidence.structural_signature,
        )
        expected_quantitative = compare_quantitative_transformability(
            reference_signature=self.positive.input_evidence.quantitative_signature,
            candidate_signature=self.control.input_evidence.quantitative_signature,
        )
        if (
            self.match_review.structural_report != expected_structural
            or self.match_review.quantitative_report != expected_quantitative
            or not expected_structural.compatible
            or not expected_quantitative.transformable
        ):
            raise ValueError("control match reports contradict retained input evidence")
        if control_role == "negative":
            if (
                self.negative_fallback_review is None
                or not self.negative_fallback_review.accepted
                or self.control.input_evidence.input_role != "single_stain_negative_control"
                or self.positive.input_evidence.fluorophore_identity != self.control.input_evidence.fluorophore_identity
                or self.positive.selection.roi.role_link_id
                != self.control.selection.roi.role_link_id
            ):
                raise ValueError("negative fallback lacks exact accepted anatomical evidence")
        elif self.negative_fallback_review is not None:
            raise ValueError("dedicated AF purification cannot retain negative-fallback evidence")
        if (
            self.measurement_ids != self.positive.sampling.measurement_ids
            or self.measurement_ids != self.control.sampling.measurement_ids
        ):
            raise ValueError("purification measurement axes differ")
        if (
            self.measured_positive != self.positive.sampling.mean_vector
            or self.measured_control != self.control.sampling.mean_vector
        ):
            raise ValueError("purification measured vectors differ from Task-5 summaries")
        if (
            self.background_resolution.status != "resolved"
            or self.background_resolution.model is None
        ):
            raise ValueError("purification requires resolved background evidence")
        model = self.background_resolution.model
        if (
            model.stained_background.source_input_id != self.positive.input_evidence.input_id
            or model.stained_background.source_image_sha256
            != self.positive.input_evidence.source_image_sha256
            or model.control_background.source_input_id != self.control.input_evidence.input_id
            or model.control_background.source_image_sha256
            != self.control.input_evidence.source_image_sha256
        ):
            raise ValueError("background model does not bind retained sampled inputs")
        dark_ids = tuple(item.measurement_id for item in self.dark_measurements)
        if (
            not self.signal_dark_review.accepted
            or self.signal_dark_review.signal_measurement_id not in self.measurement_ids
            or dark_ids != self.signal_dark_review.dark_measurement_ids
            or dark_ids != tuple(item for item in self.measurement_ids if item in set(dark_ids))
            or self.signal_dark_review.excluded_measurement_ids
            != tuple(
                item
                for item in self.measurement_ids
                if item != self.signal_dark_review.signal_measurement_id
                and item not in set(dark_ids)
            )
            or any(
                item.review_evidence_sha256 != self.signal_dark_review.evidence_sha256
                or item.reliability_weight != 1.0
                or ReviewedDarkMeasurement(item.measurement_id, item.review_evidence_sha256) != item
                for item in self.dark_measurements
            )
        ):
            raise ValueError("signal/dark evidence is invalid for retained purification")
        expected_z = tuple(
            max(_finite(float(value - background), "positive mean minus background"), 0.0)
            for value, background in zip(
                self.measured_positive, model.stained_background.values, strict=True
            )
        )
        expected_a = tuple(
            max(_finite(float(value - background), "control mean minus background"), 0.0)
            for value, background in zip(
                self.measured_control, model.control_background.values, strict=True
            )
        )
        if (
            self.background_corrected_positive != expected_z
            or self.background_corrected_control != (expected_a if self.control_set is None else self.control_set.corrected_mean)
        ):
            raise ValueError("background-corrected vectors contradict sampled means and offsets")
        if (
            self.match_review.positive_roi_id != self.positive.selection.roi.roi_id
            or self.match_review.control_roi_id != self.control.selection.roi.roi_id
            or self.match_review.positive_sampling_evidence_sha256
            != self.positive.sampling.sampling_evidence_sha256
            or self.match_review.control_sampling_evidence_sha256
            != self.control.sampling.sampling_evidence_sha256
            or not self.match_review.accepted
        ):
            raise ValueError("match review contradicts purification inputs")
        raw_a = tuple(
            float(value - background)
            for value, background in zip(
                self.measured_control,
                model.control_background.values,
                strict=True,
            )
        )
        _validate_review_lineage(
            positive=self.positive,
            control=self.control,
            signal_dark_review=self.signal_dark_review,
            negative_fallback_review=self.negative_fallback_review,
            background_corrected_positive=expected_z,
            background_corrected_control=expected_a,
        )
        if self.control_set is not None:
            if type(self.control_set) is not AfControlSetEvidence or self.control_set.positive != self.positive:
                raise ValueError("purification control set does not bind the positive ROI")
            members = self.control_set.members
            if (members[0] != AfControlSetMember(self.control, self.background_resolution, self.match_review)
                    or len(self.control_signal_dark_reviews) != len(members)
                    or self.control_signal_dark_reviews[0] != self.signal_dark_review):
                raise ValueError("purification control-set member authority differs")
            for member, review in zip(members, self.control_signal_dark_reviews, strict=True):
                if (review.signal_measurement_id != self.signal_dark_review.signal_measurement_id
                        or review.dark_measurement_ids != self.signal_dark_review.dark_measurement_ids
                        or review.source_checkpoint_sha256 != self.signal_dark_review.source_checkpoint_sha256
                        or review.decision_record_sha256 != self.signal_dark_review.decision_record_sha256):
                    raise ValueError("AF members require the same reviewed dark-band decision")
                individual = _purify_reference_roi_with_review(self.positive, member.control,
                    member.background_resolution, member.match_review, review, self.policy)
                if math.hypot(*individual.background_corrected_control) <= self.policy.af_minimum_l2_norm:
                    raise ValueError("An AF control member is uninformative under the purification policy")
            raw_a = self.control_set.signed_corrected_mean
        elif self.control_signal_dark_reviews:
            raise ValueError("additional signal reviews require a control set")
        derived = _derive_purification(
            z=expected_z,
            raw_a=raw_a,
            measurement_ids=self.measurement_ids,
            signal_measurement_id=self.signal_dark_review.signal_measurement_id,
            dark_measurements=self.dark_measurements,
            policy=self.policy,
        )
        actual = (
            self.background_corrected_control,
            self.af_subtraction_factor,
            self.raw_residual,
            self.negative_residual_clamps,
            self.dark_band_sensitivity,
            self.purified_spectrum,
            self.disposition,
            self.reason_codes,
        )
        expected = (
            derived.background_corrected_control,
            derived.af_subtraction_factor,
            derived.raw_residual,
            derived.negative_residual_clamps,
            derived.dark_band_sensitivity,
            derived.purified_spectrum,
            derived.disposition,
            derived.reason_codes,
        )
        if actual != expected:
            raise ValueError("purification fields contradict the retained dark-band derivation")
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


def _purify_reference_roi_with_review(
    positive: SampledReferenceRoi,
    control: SampledReferenceRoi,
    background: BackgroundModelResolution,
    match_review: MatchedControlReviewEvidence,
    signal_dark_review: SignalDarkMeasurementReviewEvidence,
    policy: PurificationPolicy,
    *,
    negative_fallback_review: NegativeFallbackReviewEvidence | None = None,
) -> PurifiedReferenceRoi:
    """Trusted arithmetic path for already replay-validated review evidence."""

    if type(positive) is not SampledReferenceRoi or type(control) is not SampledReferenceRoi:
        raise TypeError("positive/control must be exact SampledReferenceRoi records")
    if (
        type(background) is not BackgroundModelResolution
        or background.status != "resolved"
        or background.model is None
    ):
        raise ReferencePurificationError("purification requires a resolved background model")
    if type(match_review) is not MatchedControlReviewEvidence:
        raise TypeError("match_review has the wrong exact type")
    if type(signal_dark_review) is not SignalDarkMeasurementReviewEvidence:
        raise TypeError("signal_dark_review has the wrong exact type")
    if type(policy) is not PurificationPolicy:
        raise TypeError("policy has the wrong exact type")
    if positive.selection.roi.roi_role != "positive":
        raise ReferencePurificationError("positive input is not a positive Task-4 ROI")
    control_role = control.selection.roi.roi_role
    if control_role not in {"af", "negative"}:
        raise ReferencePurificationError("control input must be an AF or negative ROI")
    if positive.sampling.measurement_ids != control.sampling.measurement_ids:
        raise ReferencePurificationError("positive and control axes differ")
    axis = positive.sampling.measurement_ids
    if (
        match_review.positive_roi_id != positive.selection.roi.roi_id
        or match_review.control_roi_id != control.selection.roi.roi_id
        or match_review.positive_sampling_evidence_sha256
        != positive.sampling.sampling_evidence_sha256
        or match_review.control_sampling_evidence_sha256
        != control.sampling.sampling_evidence_sha256
        or not match_review.accepted
    ):
        raise ReferencePurificationError(
            "control match review does not bind these accepted samples"
        )
    expected_structural = compare_structural_compatibility(
        reference_axis=positive.input_evidence.axis,
        candidate_axis=control.input_evidence.axis,
        reference_signature=positive.input_evidence.structural_signature,
        candidate_signature=control.input_evidence.structural_signature,
    )
    expected_quantitative = compare_quantitative_transformability(
        reference_signature=positive.input_evidence.quantitative_signature,
        candidate_signature=control.input_evidence.quantitative_signature,
    )
    if (
        match_review.structural_report != expected_structural
        or match_review.quantitative_report != expected_quantitative
        or not expected_structural.compatible
        or not expected_quantitative.transformable
        or not positive.input_evidence.structurally_compatible
        or not positive.input_evidence.quantitatively_transformable
        or not control.input_evidence.structurally_compatible
        or not control.input_evidence.quantitatively_transformable
    ):
        raise ReferencePurificationError("control inputs lack accepted exact compatibility")
    expected_kind = "negative_fallback" if control_role == "negative" else "dedicated_af"
    if match_review.match_kind != expected_kind:
        raise ReferencePurificationError("control match kind contradicts the Task-4 ROI role")
    if control_role == "negative":
        if type(negative_fallback_review) is not NegativeFallbackReviewEvidence:
            raise ReferencePurificationError("negative fallback requires explicit review evidence")
        if (
            negative_fallback_review.negative_roi_id != control.selection.roi.roi_id
            or negative_fallback_review.positive_roi_id != positive.selection.roi.roi_id
            or not negative_fallback_review.accepted
            or any(item not in axis for item in negative_fallback_review.signal_measurement_ids)
            or control.input_evidence.input_role != "single_stain_negative_control"
            or positive.input_evidence.fluorophore_identity != control.input_evidence.fluorophore_identity
            or positive.selection.roi.role_link_id != control.selection.roi.role_link_id
        ):
            raise ReferencePurificationError("negative fallback is not accepted and matched")
    elif negative_fallback_review is not None:
        raise ReferencePurificationError(
            "dedicated AF control cannot carry negative-fallback evidence"
        )
    model = background.model
    if (
        model.stained_background.source_input_id != positive.input_evidence.input_id
        or model.stained_background.source_image_sha256
        != positive.input_evidence.source_image_sha256
        or model.control_background.source_input_id != control.input_evidence.input_id
        or model.control_background.source_image_sha256
        != control.input_evidence.source_image_sha256
    ):
        raise ReferencePurificationError("background model does not bind the sampled inputs")
    if len(model.stained_background.values) != len(axis):
        raise ReferencePurificationError("background model axis length differs")
    if not signal_dark_review.accepted:
        raise ReferencePurificationError("signal/dark measurement review is not accepted")
    if signal_dark_review.signal_measurement_id not in axis:
        raise ReferencePurificationError("signal measurement is absent from the exact axis")
    dark_ids = signal_dark_review.dark_measurement_ids
    if any(item not in axis for item in dark_ids):
        raise ReferencePurificationError("dark measurement is absent from the exact axis")
    if dark_ids != tuple(item for item in axis if item in set(dark_ids)):
        raise ReferencePurificationError("dark measurements are not in canonical axis order")
    expected_excluded = tuple(
        item
        for item in axis
        if item != signal_dark_review.signal_measurement_id and item not in set(dark_ids)
    )
    if signal_dark_review.excluded_measurement_ids != expected_excluded:
        raise ReferencePurificationError(
            "excluded measurements do not complete the exact signal/dark axis partition"
        )
    dark_measurements = tuple(
        ReviewedDarkMeasurement(item, signal_dark_review.evidence_sha256) for item in dark_ids
    )

    z = tuple(
        max(_finite(float(m - b), "positive mean minus background"), 0.0)
        for m, b in zip(positive.sampling.mean_vector, model.stained_background.values, strict=True)
    )
    raw_a = tuple(
        float(u - b)
        for u, b in zip(control.sampling.mean_vector, model.control_background.values, strict=True)
    )
    preliminary_a = tuple(max(_finite(item, "control mean minus background"), 0.0) for item in raw_a)
    _validate_review_lineage(
        positive=positive,
        control=control,
        signal_dark_review=signal_dark_review,
        negative_fallback_review=negative_fallback_review,
        background_corrected_positive=z,
        background_corrected_control=preliminary_a,
    )
    derived = _derive_purification(
        z=z,
        raw_a=raw_a,
        measurement_ids=axis,
        signal_measurement_id=signal_dark_review.signal_measurement_id,
        dark_measurements=dark_measurements,
        policy=policy,
    )
    return PurifiedReferenceRoi(
        positive=positive,
        control=control,
        background_resolution=background,
        match_review=match_review,
        negative_fallback_review=negative_fallback_review,
        signal_dark_review=signal_dark_review,
        dark_measurements=dark_measurements,
        policy=policy,
        measurement_ids=axis,
        measured_positive=positive.sampling.mean_vector,
        measured_control=control.sampling.mean_vector,
        background_corrected_positive=z,
        background_corrected_control=derived.background_corrected_control,
        af_subtraction_factor=derived.af_subtraction_factor,
        raw_residual=derived.raw_residual,
        negative_residual_clamps=derived.negative_residual_clamps,
        dark_band_sensitivity=derived.dark_band_sensitivity,
        purified_spectrum=derived.purified_spectrum,
        disposition=derived.disposition,
        reason_codes=derived.reason_codes,
    )


def purify_reference_roi(
    positive: SampledReferenceRoi,
    control: SampledReferenceRoi,
    background: BackgroundModelResolution,
    match_review: MatchedControlReviewEvidence,
    signal_dark_review: SignalDarkMeasurementReviewEvidence,
    policy: PurificationPolicy,
    *,
    signal_dark_source: ReferenceWorkflowCheckpoint | None = None,
    signal_dark_result: ReferenceWorkflowCheckpoint | None = None,
    negative_fallback_review: NegativeFallbackReviewEvidence | None = None,
) -> PurifiedReferenceRoi:
    """Purify only after replaying the exact signal/dark workflow authority."""

    if (
        type(signal_dark_source) is not ReferenceWorkflowCheckpoint
        or type(signal_dark_result) is not ReferenceWorkflowCheckpoint
    ):
        raise ReferencePurificationError(
            "purification requires exact decision-bound signal/dark authority"
        )
    from .reference_review_bindings import bind_signal_dark_measurement_review

    rebound = bind_signal_dark_measurement_review(
        positive,
        control,
        signal_dark_source,
        signal_dark_result,
    )
    if type(signal_dark_review) is not SignalDarkMeasurementReviewEvidence or (
        rebound != signal_dark_review
    ):
        raise ReferencePurificationError(
            "signal/dark review differs from replayed decision authority"
        )
    return _purify_reference_roi_with_review(
        positive,
        control,
        background,
        match_review,
        signal_dark_review,
        policy,
        negative_fallback_review=negative_fallback_review,
    )


def purify_reference_control_set(
    control_set: AfControlSetEvidence,
    signal_dark_reviews: tuple[SignalDarkMeasurementReviewEvidence, ...],
    policy: PurificationPolicy,
    *, signal_dark_source: ReferenceWorkflowCheckpoint,
    signal_dark_result: ReferenceWorkflowCheckpoint,
) -> PurifiedReferenceRoi:
    """Fit one amplitude per positive ROI against its explicitly reviewed AF set."""
    from dataclasses import replace
    if type(control_set) is not AfControlSetEvidence:
        raise TypeError("control_set must be exact AfControlSetEvidence")
    if type(signal_dark_reviews) is not tuple or len(signal_dark_reviews) != len(control_set.members):
        raise ValueError("control signal reviews must match every AF member")
    individual = tuple(purify_reference_roi(control_set.positive, member.control,
        member.background_resolution, member.match_review, review, policy,
        signal_dark_source=signal_dark_source, signal_dark_result=signal_dark_result)
        for member, review in zip(control_set.members, signal_dark_reviews, strict=True))
    first = individual[0]
    derived = _derive_purification(z=first.background_corrected_positive,
        raw_a=control_set.signed_corrected_mean, measurement_ids=first.measurement_ids,
        signal_measurement_id=first.signal_dark_review.signal_measurement_id,
        dark_measurements=first.dark_measurements, policy=policy)
    return replace(first, control_set=control_set, control_signal_dark_reviews=signal_dark_reviews,
        **{item.name: getattr(derived, item.name) for item in fields(derived)})


@dataclass(frozen=True, slots=True)
class ConsensusInclusionDecision:
    roi_id: str
    purification_evidence_sha256: str
    included: bool
    reason: str
    review_binding_sha256: str
    accepted: bool
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.roi_id, "roi_id")
        _sha(self.purification_evidence_sha256, "purification_evidence_sha256")
        if type(self.included) is not bool or type(self.accepted) is not bool:
            raise TypeError("consensus decisions must be bool")
        _text(self.reason, "reason")
        _sha(self.review_binding_sha256, "review_binding_sha256")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class ConsensusPolicy:
    policy_version: str
    outlier_review_angle_degrees: float
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "policy_version")
        angle = _finite(self.outlier_review_angle_degrees, "outlier_review_angle_degrees")
        if not 0.0 <= angle <= 90.0:
            raise ValueError("outlier review angle must be in [0, 90]")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class ConsensusRoiQc:
    roi_id: str
    purification_evidence_sha256: str
    included: bool
    spectral_angle_degrees: float
    euclidean_distance: float
    outlier_review_required: bool
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.roi_id, "roi_id")
        _sha(self.purification_evidence_sha256, "purification_evidence_sha256")
        if type(self.included) is not bool or type(self.outlier_review_required) is not bool:
            raise TypeError("consensus ROI decisions must be bool")
        angle = _finite(self.spectral_angle_degrees, "spectral_angle_degrees")
        if not 0.0 <= angle <= 90.0:
            raise ValueError("spectral angle must be in [0, 90]")
        _nonnegative(self.euclidean_distance, "euclidean_distance")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


def compare_consensus_spectra(
    spectra: tuple[tuple[float, ...], ...], included: tuple[bool, ...],
) -> tuple[tuple[float, ...], tuple[tuple[float, float], ...]]:
    """Compare cached unit-L2 regional shapes with their equal-region consensus.

    Shared by candidate construction and its interactive review. No threshold or
    acceptance decision is implicit in this calculation.
    """
    if not spectra or len(spectra) != len(included) or not any(included):
        raise ReferencePurificationError("consensus requires at least one included ROI")
    width = len(spectra[0])
    if not width or any(len(row) != width for row in spectra):
        raise ReferencePurificationError("regional spectra must share the same band axis")
    if any(type(value) is not bool for value in included):
        raise ReferencePurificationError("regional inclusion decisions must be boolean")
    if any(not math.isfinite(value) or value < 0 for row in spectra for value in row):
        raise ReferencePurificationError("regional spectra must be finite and nonnegative")
    if any(not math.isclose(math.sqrt(math.fsum(v * v for v in row)), 1.0,
                            rel_tol=1e-12, abs_tol=1e-12) for row in spectra):
        raise ReferencePurificationError("regional spectra must retain unit-L2 normalization")
    selected = tuple(row for row, keep in zip(spectra, included, strict=True) if keep)
    mean = tuple(math.fsum(row[position] for row in selected) / len(selected)
                 for position in range(width))
    norm = math.sqrt(math.fsum(item * item for item in mean))
    if norm <= 0.0:
        raise ReferencePurificationError("equal ROI mean is zero")
    consensus = tuple(float(item / norm) for item in mean)
    comparisons = []
    for spectrum in spectra:
        cosine = min(1.0, max(0.0, math.fsum(
            a * b for a, b in zip(spectrum, consensus, strict=True))))
        angle = math.degrees(math.acos(cosine))
        distance = math.sqrt(math.fsum(
            (a - b) ** 2 for a, b in zip(spectrum, consensus, strict=True)))
        comparisons.append((float(angle), float(distance)))
    return consensus, tuple(comparisons)


def _derive_consensus_outputs(
    purifications: tuple[PurifiedReferenceRoi, ...],
    decisions: tuple[ConsensusInclusionDecision, ...],
    policy: ConsensusPolicy,
) -> tuple[
    tuple[float, ...],
    tuple[ConsensusRoiQc, ...],
    str,
    tuple[str, ...],
]:
    consensus, comparisons = compare_consensus_spectra(
        tuple(item.purified_spectrum for item in purifications),
        tuple(decision.included for decision in decisions),
    )
    qc: list[ConsensusRoiQc] = []
    outlier_ids: list[str] = []
    for item, decision, (angle, distance) in zip(purifications, decisions, comparisons, strict=True):
        outlier = bool(decision.included and angle > policy.outlier_review_angle_degrees)
        if outlier:
            outlier_ids.append(item.positive.selection.roi.roi_id)
        qc.append(
            ConsensusRoiQc(
                roi_id=item.positive.selection.roi.roi_id,
                purification_evidence_sha256=item.evidence_sha256,
                included=decision.included,
                spectral_angle_degrees=float(angle),
                euclidean_distance=float(distance),
                outlier_review_required=outlier,
            )
        )
    return (
        consensus,
        tuple(qc),
        "review_required" if outlier_ids else "pass",
        tuple(f"consensus_outlier:{item}" for item in outlier_ids),
    )


@dataclass(frozen=True, slots=True)
class FluorophoreConsensusCandidate:
    component_identity: str
    fluorophore_identity: str
    policy: ConsensusPolicy
    measurement_ids: tuple[str, ...]
    values: tuple[float, ...]
    roi_purifications: tuple[PurifiedReferenceRoi, ...]
    inclusion_decisions: tuple[ConsensusInclusionDecision, ...]
    roi_qc: tuple[ConsensusRoiQc, ...]
    disposition: str
    reason_codes: tuple[str, ...]
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FluorophoreConsensusCandidate fields when the record is constructed.

        The checks cover ``component_identity``, ``fluorophore_identity``, ``values``,
        ``roi_purifications``, ``inclusion_decisions``, ``roi_qc``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _text(self.component_identity, "component_identity")
        _text(self.fluorophore_identity, "fluorophore_identity")
        if type(self.policy) is not ConsensusPolicy:
            raise TypeError("policy must be exact ConsensusPolicy")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise TypeError("measurement_ids must be a nonempty tuple")
        values = _numeric_tuple(self.values, "values", len(self.measurement_ids))
        if any(item < 0.0 for item in values):
            raise ValueError("consensus values must be nonnegative")
        if not math.isclose(
            math.sqrt(math.fsum(item * item for item in values)), 1.0, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError("consensus values must be unit L2")
        purifications = _typed_tuple(
            self.roi_purifications, PurifiedReferenceRoi, "roi_purifications"
        )
        decisions = _typed_tuple(
            self.inclusion_decisions, ConsensusInclusionDecision, "inclusion_decisions"
        )
        qc = _typed_tuple(self.roi_qc, ConsensusRoiQc, "roi_qc")
        ids = tuple(item.positive.selection.roi.roi_id for item in purifications)
        if (
            len(ids) != len(set(ids))
            or tuple(item.roi_id for item in decisions) != ids
            or tuple(item.roi_id for item in qc) != ids
        ):
            raise ValueError("consensus nested ROI identities must be unique and aligned")
        if self.disposition not in {"pass", "review_required"}:
            raise ValueError("unsupported consensus disposition")
        reasons = _reasons(self.reason_codes)
        if self.disposition == "pass" and reasons:
            raise ValueError("passing consensus cannot have reason codes")
        if self.disposition == "review_required" and not reasons:
            raise ValueError("review-required consensus needs reason codes")
        for item, decision in zip(purifications, decisions, strict=True):
            if (
                item.disposition != "pass"
                or item.purified_spectrum is None
                or item.measurement_ids != self.measurement_ids
                or item.positive.input_evidence.fluorophore_identity != self.fluorophore_identity
                or not decision.accepted
                or decision.purification_evidence_sha256 != item.evidence_sha256
            ):
                raise ValueError("consensus nested purification/decision evidence is invalid")
        expected_values, expected_qc, expected_disposition, expected_reasons = (
            _derive_consensus_outputs(purifications, decisions, self.policy)
        )
        if (
            self.values != expected_values
            or self.roi_qc != expected_qc
            or self.disposition != expected_disposition
            or self.reason_codes != expected_reasons
        ):
            raise ValueError("consensus fields contradict normalized equal-ROI derivation")
        object.__setattr__(self, "candidate_sha256", _digest(self, "candidate_sha256"))


def build_fluorophore_consensus(
    component_identity: str,
    fluorophore_identity: str,
    roi_purifications: tuple[PurifiedReferenceRoi, ...],
    inclusion_decisions: tuple[ConsensusInclusionDecision, ...],
    policy: ConsensusPolicy,
) -> FluorophoreConsensusCandidate:
    """Build the normalized equal mean of explicitly included ROI spectra."""

    _text(component_identity, "component_identity")
    _text(fluorophore_identity, "fluorophore_identity")
    purifications = _typed_tuple(roi_purifications, PurifiedReferenceRoi, "roi_purifications")
    decisions = _typed_tuple(inclusion_decisions, ConsensusInclusionDecision, "inclusion_decisions")
    if type(policy) is not ConsensusPolicy:
        raise TypeError("policy has the wrong exact type")
    ids = tuple(item.positive.selection.roi.roi_id for item in purifications)
    if len(ids) != len(set(ids)):
        raise ReferencePurificationError("duplicate positive ROI identity in consensus")
    if len(decisions) != len(purifications):
        raise ReferencePurificationError("every ROI requires one explicit inclusion decision")
    decision_by_id = {item.roi_id: item for item in decisions}
    if set(decision_by_id) != set(ids):
        raise ReferencePurificationError("consensus decisions and ROI identities differ")
    axis = purifications[0].measurement_ids
    ordered_decisions: list[ConsensusInclusionDecision] = []
    for item in purifications:
        decision = decision_by_id[item.positive.selection.roi.roi_id]
        if (
            not decision.accepted
            or decision.purification_evidence_sha256 != item.evidence_sha256
            or item.disposition != "pass"
            or item.purified_spectrum is None
            or item.measurement_ids != axis
            or item.positive.input_evidence.fluorophore_identity != fluorophore_identity
        ):
            raise ReferencePurificationError("consensus input or accepted decision is invalid")
        ordered_decisions.append(decision)
    consensus, qc, disposition, reasons = _derive_consensus_outputs(
        purifications, tuple(ordered_decisions), policy
    )
    return FluorophoreConsensusCandidate(
        component_identity=component_identity,
        fluorophore_identity=fluorophore_identity,
        policy=policy,
        measurement_ids=axis,
        values=consensus,
        roi_purifications=purifications,
        inclusion_decisions=tuple(ordered_decisions),
        roi_qc=qc,
        disposition=disposition,
        reason_codes=reasons,
    )


__all__ = [
    "AfRegionalMean", "AfControlSetMember", "AfControlSetEvidence", "AfControlSignalError",
    "combine_af_regional_means", "purify_reference_control_set",
    "ConsensusInclusionDecision",
    "ConsensusPolicy",
    "ConsensusRoiQc",
    "DarkBandSensitivity",
    "FluorophoreConsensusCandidate",
    "MatchedControlReviewEvidence",
    "NegativeFallbackReviewEvidence",
    "NegativeResidualClamp",
    "PurificationPolicy",
    "PurifiedReferenceRoi",
    "ReferencePurificationError",
    "ReviewedDarkMeasurement",
    "SignalDarkMeasurementReviewEvidence",
    "build_fluorophore_consensus",
    "purify_reference_roi",
]
