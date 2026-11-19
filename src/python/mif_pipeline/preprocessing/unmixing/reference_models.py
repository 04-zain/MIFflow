"""Immutable spectral-reference, target-AF, and resolved-basis records.

The `RoiEvidence` record carries `roi_id`, `roi_role`, `source_input_id`,
`source_image_sha256`, `geometry_artifact_sha256` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import ClassVar

import numpy as np

from mif_pipeline.io import (
    DigitalRangeAuthority,
    MetadataConflictError,
    OffsetAuthority,
    validate_offset_specification,
)

from mif_pipeline.io.metadata.canonical import applied_detector_offset

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_NORMALIZATIONS = frozenset({"unit_l1", "unit_l2_spectral_shape_v1"})
_SPECTRUM_KINDS = frozenset({"fluorophore", "target_autofluorescence"})
_RESOLVED_KINDS = frozenset({"fluorophore", "target_autofluorescence", "profile_autofluorescence"})
_SOURCE_KINDS = frozenset(
    {
        "fluorophore_reference_library",
        "target_autofluorescence_model",
        "instrument_profile",
        "curated_spectral_library",
        "accepted_cycle_basis",
    }
)
_REFERENCE_INPUT_ROLES = frozenset(
    {
        "single_component_reference",
        "unstained_control",
        "single_stain_negative_control",
        "target_image",
        "target_unstained_control",
    }
)
_ROI_ROLES = frozenset({"positive", "af", "negative", "background"})
_BACKGROUND_POLICIES = frozenset(
    {
        "validated_offset_corrected_v1",
        "validated_shared_offset_v1",
        "image_specific_offsets_v1",
    }
)


# ==============================================================================
#                                Reference models
# ==============================================================================


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _timestamp(value: object, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return text


def _tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    if not value and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    return value


def _typed_tuple(
    value: object,
    item_type: type,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[object, ...]:
    result = _tuple(value, label, allow_empty=allow_empty)
    if any(type(item) is not item_type for item in result):
        raise TypeError(f"{label} must contain only {item_type.__name__} records")
    return result


def _text_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    result = _tuple(value, label, allow_empty=allow_empty)
    texts = tuple(_required_text(item, f"{label} item") for item in result)
    if len(texts) != len(set(texts)):
        raise ValueError(f"duplicate identity in {label}")
    return texts


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _positive(value: object, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ValueError(f"{label} must be strictly positive")
    return number


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    result = _nonnegative_integer(value, label)
    if result == 0:
        raise ValueError(f"{label} must be strictly positive")
    return result


def _record_fields(value):
    """Retain the historical wire/hash form when optional display evidence is absent."""
    return tuple(item for item in fields(value) if not (item.metadata.get("omit_empty") and not getattr(value, item.name)) and not (
        isinstance(value, SpectralMeasurement) and item.name == "optical_setting"
        and value.optical_setting is None))


def _primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in _record_fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported artifact value type: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _record_digest(record: object, omitted_field: str) -> str:
    if not is_dataclass(record) or isinstance(record, type):
        raise TypeError("checksummed records must serialize as objects")
    payload = {
        item.name: _primitive(getattr(record, item.name))
        for item in _record_fields(record)
        if item.name != omitted_field
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _artifact_mapping(record: object) -> dict[str, object]:
    payload = _primitive(record)
    if not isinstance(payload, dict):
        raise TypeError("reference artifacts must serialize as objects")
    return payload


def _basis_rank_condition(
    columns: tuple[tuple[float, ...], ...],
) -> tuple[int, float]:
    matrix = np.asarray(columns, dtype=np.float64).T
    rank = int(np.linalg.matrix_rank(matrix))
    condition_number = float(np.linalg.cond(matrix))
    return rank, condition_number


@dataclass(frozen=True, slots=True)
class SpectralOpticalSetting:
    """Recorded display grouping within one source image, not cross-file compatibility.

    The ID distinguishes references with identical display names. Provenance
    distinguishes a used OME light path from a declared (potentially incomplete)
    filter set and the supported native IM3 acquisition record.
    """

    setting_id: str
    label: str
    provenance: str

    def __post_init__(self):
        _required_text(self.setting_id, "setting_id")
        _required_text(self.label, "label")
        if self.provenance not in {"ome_light_path", "ome_filter_set", "im3_acquisition"}:
            raise ValueError("unsupported optical setting provenance")


@dataclass(frozen=True, slots=True)
class SpectralMeasurement:
    """One retained spectral sample and its parent acquisition band."""

    measurement_id: str
    parent_band_id: str
    sample_id: str
    wavelength: float | None
    wavelength_unit: str | None
    optical_setting: SpectralOpticalSetting | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _required_text(self.measurement_id, "measurement_id")
        _required_text(self.parent_band_id, "parent_band_id")
        _required_text(self.sample_id, "sample_id")
        if self.optical_setting is not None and type(self.optical_setting) is not SpectralOpticalSetting:
            raise TypeError("optical_setting must be SpectralOpticalSetting")
        if (self.wavelength is None) != (self.wavelength_unit is None):
            raise ValueError("wavelength and wavelength_unit must be supplied together")
        if self.wavelength is not None:
            _positive(self.wavelength, "wavelength")
            _required_text(self.wavelength_unit, "wavelength_unit")


@dataclass(frozen=True, slots=True)
class SpectralMeasurementAxis:
    """Immutable ordered spectral-measurement axis."""

    measurements: tuple[SpectralMeasurement, ...]

    def __post_init__(self) -> None:
        records = _typed_tuple(self.measurements, SpectralMeasurement, "measurements")
        measurement_ids = tuple(item.measurement_id for item in records)
        sample_ids = tuple((item.parent_band_id, item.sample_id) for item in records)
        if len(measurement_ids) != len(set(measurement_ids)):
            raise ValueError("duplicate measurement identity in spectral axis")
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("duplicate sample identity within a parent band")
        represented = tuple(item.wavelength is not None for item in records)
        if any(represented):
            units = {item.wavelength_unit for item in records if item.wavelength is not None}
            if len(units) != 1:
                raise ValueError("spectral axis must use one wavelength unit")

    @property
    def measurement_ids(self) -> tuple[str, ...]:
        return tuple(item.measurement_id for item in self.measurements)

    @property
    def parent_band_ids(self) -> tuple[str, ...]:
        return tuple(item.parent_band_id for item in self.measurements)


@dataclass(frozen=True, slots=True)
class StructuralCompatibilitySignature:
    """Recorded acquisition identities and the selected correction policy.

    Exposure-only and as-stored records retain absent identities as None under the
    same-settings requirement. Known instrument and optical-path conflicts remain
    incompatible. Unrecorded identities require matching-acquisition review;
    opaque profile names do not establish a response calibration.
    """

    instrument_id: str | None
    optical_path_id: str | None
    detector_mode: str | None
    acquisition_profile_id: str | None
    response_calibration_id: str | None
    correction_mode: str = "full"

    def __post_init__(self) -> None:
        if self.correction_mode not in {"full", "exposure_only", "as_stored"}:
            raise ValueError("Unsupported intensity correction mode")
        check = _optional_text
        check(self.instrument_id, "instrument_id")
        check(self.optical_path_id, "optical_path_id")
        check(self.detector_mode, "detector_mode")
        _optional_text(self.acquisition_profile_id, "acquisition_profile_id")
        _optional_text(self.response_calibration_id, "response_calibration_id")


@dataclass(frozen=True, slots=True)
class QuantitativeMeasurementTransform:
    """Recorded acquisition facts and selected correction for one measurement.

    Full correction requires each applied factor; metadata-known dark-corrected
    values need no further offset subtraction. Exposure-only applies actual exposure
    alone. As-stored fitting retains known linear calibrated values unchanged.
    Unused imaging settings remain absent when they were not recorded.
    """

    measurement_id: str
    effective_digital_maximum: float | None
    digital_range_meaningful_bits: int | None
    digital_range_authority: DigitalRangeAuthority | None
    digital_range_profile_id: str | None
    offset: float | None
    offset_authority: OffsetAuthority
    offset_profile_identifier: str | None
    exposure_seconds: float | None
    gain: float | None
    binning_area: int | None
    response_correction_multiplier: float
    correction_mode: str = "full"
    intensity_representation: str = "unknown"

    @property
    def applied_offset(self) -> float | None:
        """Offset used by full correction; ``offset`` retains the acquisition fact."""
        return applied_detector_offset(self.offset, self.intensity_representation)

    def __post_init__(self) -> None:
        _required_text(self.measurement_id, "measurement_id")
        if self.correction_mode == "as_stored":
            if self.intensity_representation != "normalized_counts":
                raise ValueError("As-stored references require independently known normalized units")
            if any(value is not None for value in (self.effective_digital_maximum, self.digital_range_meaningful_bits, self.digital_range_authority, self.digital_range_profile_id)):
                raise ValueError("As-stored references do not apply a raw-count range")
        else:
            _positive(self.effective_digital_maximum, "effective_digital_maximum")
            _positive_integer(self.digital_range_meaningful_bits, "digital_range_meaningful_bits")
            if not isinstance(self.digital_range_authority, DigitalRangeAuthority):
                raise TypeError("digital_range_authority must be DigitalRangeAuthority")
        if self.digital_range_authority is DigitalRangeAuthority.EXPLICIT_PROFILE:
            _required_text(self.digital_range_profile_id, "digital range profile identifier")
        elif self.digital_range_profile_id is not None:
            raise ValueError(
                "digital range profile identifier is permitted only for explicit profiles"
            )
        if self.correction_mode not in {"full", "exposure_only", "as_stored"}:
            raise ValueError("Unsupported measurement correction mode")
        if self.intensity_representation not in {"unknown", "raw_counts", "dark_corrected_counts", "normalized_counts"}:
            raise ValueError("Unsupported stored intensity representation")
        if self.intensity_representation == "normalized_counts" and self.correction_mode != "as_stored":
            raise ValueError("Already-normalized references cannot receive intensity correction again")
        if self.correction_mode == "full" and (
            self.gain is None or self.binning_area is None
            or (self.offset is None and self.intensity_representation != "dark_corrected_counts")
        ):
            raise ValueError("Full reference correction requires applied offset, gain and binning")
        if self.offset is not None:
            _finite(self.offset, "offset")
        if not isinstance(self.offset_authority, OffsetAuthority):
            raise TypeError("offset_authority must be OffsetAuthority")
        try:
            validate_offset_specification(
                () if self.offset is None else (self.offset,),
                self.offset_authority,
                self.offset_profile_identifier,
            )
        except MetadataConflictError as exc:
            raise ValueError(f"invalid offset authority: {exc}") from exc
        if self.exposure_seconds is not None or self.correction_mode != "as_stored":
            _positive(self.exposure_seconds, "exposure_seconds")
        if self.gain is not None:
            _positive(self.gain, "gain")
        if self.binning_area is not None:
            _positive_integer(self.binning_area, "binning_area")
        if self.correction_mode in {"exposure_only", "as_stored"} and self.response_correction_multiplier != 1.0:
            raise ValueError("Exposure-only and as-stored measurements do not apply response calibration")
        _positive(
            self.response_correction_multiplier,
            "response_correction_multiplier",
        )


@dataclass(frozen=True, slots=True)
class QuantitativeTransformSignature:
    """Ordered quantitative transforms into the processing coordinate system."""

    normalization_policy: str
    response_transform_id: str
    response_transform_semantic: str
    measurements: tuple[QuantitativeMeasurementTransform, ...]
    response_transform_authority: str

    def __post_init__(self) -> None:
        _required_text(self.normalization_policy, "normalization_policy")
        if (
            not isinstance(self.response_transform_id, str)
            or not self.response_transform_id.strip()
        ):
            raise ValueError("an explicit response transform ID is required")
        _required_text(self.response_transform_id, "response_transform_id")
        if self.response_transform_semantic != "multiply_normalized_measurement":
            raise ValueError(
                "response transform requires the multiplication semantic "
                "multiply_normalized_measurement"
            )
        if (
            not isinstance(self.response_transform_authority, str)
            or not self.response_transform_authority.strip()
        ):
            raise ValueError("an explicit response transform authority is required")
        _required_text(
            self.response_transform_authority,
            "response_transform_authority",
        )
        records = _typed_tuple(
            self.measurements,
            QuantitativeMeasurementTransform,
            "quantitative measurements",
        )
        expected_mode = {
            "mif_pipeline_offset_exposure_v1": "full",
            "mif_pipeline_exposure_only_v1": "exposure_only",
            "mif_pipeline_as_stored_v1": "as_stored",
        }.get(self.normalization_policy)
        if expected_mode is None or any(item.correction_mode != expected_mode for item in records):
            raise ValueError("Reference normalization policy and measurement correction modes differ")
        identities = tuple(item.measurement_id for item in records)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate quantitative measurement identity")

    @property
    def measurement_ids(self) -> tuple[str, ...]:
        return tuple(item.measurement_id for item in self.measurements)


@dataclass(frozen=True, slots=True)
class ReferenceInputEvidence:
    """Independently resolved compatibility and transform facts for one input."""

    input_id: str
    input_role: str
    fluorophore_identity: str | None
    source_image_sha256: str
    axis: SpectralMeasurementAxis
    structural_signature: StructuralCompatibilitySignature | None
    quantitative_signature: QuantitativeTransformSignature
    structurally_compatible: bool
    quantitatively_transformable: bool
    structural_compatibility_evidence_sha256: str | None
    quantitative_transform_evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate the ReferenceInputEvidence fields when the record is constructed.

        The checks cover ``input_id``, ``input_role``, ``source_image_sha256``,
        ``structural_compatibility_evidence_sha256``,
        ``quantitative_transform_evidence_sha256``, ``fluorophore_identity``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _required_text(self.input_id, "reference input_id")
        if self.input_role not in _REFERENCE_INPUT_ROLES:
            raise ValueError("reference input_role is not supported")
        if self.input_role in {
            "single_component_reference",
            "single_stain_negative_control",
        }:
            _required_text(
                self.fluorophore_identity,
                "reference input fluorophore_identity",
            )
        elif self.fluorophore_identity is not None:
            raise ValueError("unstained and target inputs must not claim fluorophore identity")
        _sha256(self.source_image_sha256, "reference input source_image_sha256")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("reference input axis must be SpectralMeasurementAxis")
        if self.structural_signature is None and self.input_role != "target_image":
            raise TypeError("only target images may lack a structural signature")
        if self.structural_signature is None and (
            self.structurally_compatible is not False
            or self.structural_compatibility_evidence_sha256 is not None
        ):
            raise ValueError("target without structural facts must retain absent compatibility")
        if (
            self.structural_signature is not None
            and type(self.structural_signature) is not StructuralCompatibilitySignature
        ):
            raise TypeError("reference input structural_signature has the wrong record type")
        if (
            self.input_role == "target_image"
            and self.structurally_compatible is False
            and self.structural_compatibility_evidence_sha256 is not None
        ):
            raise ValueError("target without structural comparison must retain absent report hash")
        if self.structural_compatibility_evidence_sha256 is None and (
            self.input_role != "target_image" or self.structurally_compatible is not False
        ):
            raise ValueError(
                "absent structural report hash is permitted only for an un-compared target"
            )
        if type(self.quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("reference input quantitative_signature has the wrong record type")
        if self.quantitative_signature.measurement_ids != self.axis.measurement_ids:
            raise ValueError("reference input quantitative measurement axis must match exactly")
        if self.structural_signature is not None and any(
            item.correction_mode != self.structural_signature.correction_mode
            for item in self.quantitative_signature.measurements
        ):
            raise ValueError("Structural and quantitative intensity correction policies differ")
        if type(self.structurally_compatible) is not bool:
            raise TypeError("structurally_compatible must be bool")
        if type(self.quantitatively_transformable) is not bool:
            raise TypeError("quantitatively_transformable must be bool")
        if self.structural_compatibility_evidence_sha256 is not None:
            _sha256(
                self.structural_compatibility_evidence_sha256,
                "structural_compatibility_evidence_sha256",
            )
        _sha256(
            self.quantitative_transform_evidence_sha256,
            "quantitative_transform_evidence_sha256",
        )


@dataclass(frozen=True, slots=True)
class RejectionCount:
    reason: str
    count: int

    def __post_init__(self) -> None:
        _required_text(self.reason, "rejection reason")
        _nonnegative_integer(self.count, "rejection count")


@dataclass(frozen=True, slots=True)
class QcMetric:
    name: str
    value: float
    passed: bool

    def __post_init__(self) -> None:
        _required_text(self.name, "QC metric name")
        _finite(self.value, "QC metric value")
        if type(self.passed) is not bool:
            raise TypeError("QC metric passed must be bool")


@dataclass(frozen=True, slots=True)
class BackgroundSpectrum:
    """One explicit additive-background vector bound to its canonical input."""

    source_input_id: str
    source_image_sha256: str
    values: tuple[float, ...]
    spectrum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _required_text(self.source_input_id, "background source_input_id")
        _sha256(self.source_image_sha256, "background source_image_sha256")
        values = _tuple(self.values, "background spectrum values")
        numeric = tuple(_finite(value, "background spectrum value") for value in values)
        if any(value < 0 for value in numeric):
            raise ValueError("background spectrum values must be nonnegative")
        object.__setattr__(
            self,
            "spectrum_sha256",
            _record_digest(self, "spectrum_sha256"),
        )


@dataclass(frozen=True, slots=True)
class BackgroundModelEvidence:
    """Explicit shared, image-specific, or validated-zero background model."""

    policy: str
    stained_background: BackgroundSpectrum
    control_background: BackgroundSpectrum
    shared_offset_equivalence_sha256: str | None
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.policy not in _BACKGROUND_POLICIES:
            raise ValueError("background policy is not supported")
        if type(self.stained_background) is not BackgroundSpectrum:
            raise TypeError("stained_background must be BackgroundSpectrum")
        if type(self.control_background) is not BackgroundSpectrum:
            raise TypeError("control_background must be BackgroundSpectrum")
        if len(self.stained_background.values) != len(self.control_background.values):
            raise ValueError("background spectra must have the same length")
        if self.shared_offset_equivalence_sha256 is not None:
            _sha256(
                self.shared_offset_equivalence_sha256,
                "shared_offset_equivalence_sha256",
            )
        _sha256(self.evidence_sha256, "background model evidence_sha256")
        if self.policy == "validated_shared_offset_v1":
            if self.shared_offset_equivalence_sha256 is None:
                raise ValueError(
                    "validated shared background requires shared-offset equivalence evidence"
                )
            if self.stained_background.values != self.control_background.values:
                raise ValueError("validated shared background spectra must be identical")
        elif self.shared_offset_equivalence_sha256 is not None:
            raise ValueError(
                "shared-offset equivalence evidence is only valid for shared background"
            )
        if self.policy == "validated_offset_corrected_v1" and any(
            value != 0.0
            for value in (
                *self.stained_background.values,
                *self.control_background.values,
            )
        ):
            raise ValueError("validated offset-corrected background spectra must be zero")


@dataclass(frozen=True, slots=True)
class RoiEvidence:
    """Complete-vector sampling, purification, background, and QC evidence."""

    roi_id: str
    roi_role: str
    source_input_id: str
    source_image_sha256: str
    geometry_artifact_sha256: str
    complete_vector_estimator_version: str
    sampling_policy: str
    sampling_seed: int | None
    requested_pixel_count: int | None
    total_pixel_count: int
    accepted_pixel_count: int
    rejected_pixel_count: int
    rejection_counts: tuple[RejectionCount, ...]
    roi_coverage_fraction: float
    sampled_chunk_ids: tuple[str, ...]
    signal_measurement_id: str | None
    dark_measurement_ids: tuple[str, ...]
    matched_af_roi_ids: tuple[str, ...]
    af_subtraction_factor: float | None
    purified_spectrum: tuple[float, ...] | None
    consensus_inclusion: str
    inclusion_reason: str
    qc_metrics: tuple[QcMetric, ...]
    accepted: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate the RoiEvidence fields when the record is constructed.

        The checks cover ``roi_id``, ``roi_role``, ``source_input_id``,
        ``source_image_sha256``, ``geometry_artifact_sha256``,
        ``complete_vector_estimator_version``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        _required_text(self.roi_id, "roi_id")
        if self.roi_role not in _ROI_ROLES:
            raise ValueError("ROI role is not supported")
        _required_text(self.source_input_id, "source_input_id")
        _sha256(self.source_image_sha256, "source_image_sha256")
        _sha256(self.geometry_artifact_sha256, "geometry_artifact_sha256")
        _required_text(
            self.complete_vector_estimator_version,
            "complete_vector_estimator_version",
        )
        _required_text(self.sampling_policy, "sampling_policy")
        if self.sampling_seed is not None:
            _nonnegative_integer(self.sampling_seed, "sampling_seed")
        if self.requested_pixel_count is not None:
            _positive_integer(self.requested_pixel_count, "requested_pixel_count")
        _nonnegative_integer(self.total_pixel_count, "total_pixel_count")
        _nonnegative_integer(self.accepted_pixel_count, "accepted_pixel_count")
        _nonnegative_integer(self.rejected_pixel_count, "rejected_pixel_count")
        if self.accepted_pixel_count + self.rejected_pixel_count != self.total_pixel_count:
            raise ValueError("accepted and rejected pixel counts must equal total")
        rejection_counts = _typed_tuple(
            self.rejection_counts,
            RejectionCount,
            "rejection_counts",
            allow_empty=True,
        )
        rejection_reasons = tuple(item.reason for item in rejection_counts)
        if len(rejection_reasons) != len(set(rejection_reasons)):
            raise ValueError("duplicate rejection reason")
        if sum(item.count for item in rejection_counts) != self.rejected_pixel_count:
            raise ValueError("rejection counts must equal rejected_pixel_count")
        coverage = _finite(self.roi_coverage_fraction, "roi_coverage_fraction")
        if not 0.0 <= coverage <= 1.0:
            raise ValueError("roi_coverage_fraction must be between zero and one")
        _text_tuple(self.sampled_chunk_ids, "sampled_chunk_ids", allow_empty=True)
        _optional_text(self.signal_measurement_id, "signal_measurement_id")
        _text_tuple(self.dark_measurement_ids, "dark_measurement_ids", allow_empty=True)
        _text_tuple(self.matched_af_roi_ids, "matched_af_roi_ids", allow_empty=True)
        if self.af_subtraction_factor is not None:
            factor = _finite(self.af_subtraction_factor, "af_subtraction_factor")
            if factor < 0:
                raise ValueError("af_subtraction_factor must be nonnegative")
        if self.purified_spectrum is not None:
            purified = _tuple(self.purified_spectrum, "purified_spectrum")
            numeric = tuple(_finite(value, "purified spectrum value") for value in purified)
            if any(value < 0 for value in numeric) or not any(value > 0 for value in numeric):
                raise ValueError("purified_spectrum must be nonnegative and nonzero")
        if self.roi_role == "positive":
            if self.purified_spectrum is None:
                raise ValueError("positive ROI requires its purified spectrum")
            if self.consensus_inclusion not in {"included", "excluded"}:
                raise ValueError("positive ROI requires an inclusion/exclusion decision")
        else:
            if self.purified_spectrum is not None:
                raise ValueError("only positive ROI may carry a purified spectrum")
            if self.consensus_inclusion != "not_applicable":
                raise ValueError("non-positive ROI consensus inclusion is not applicable")
        _required_text(self.inclusion_reason, "inclusion_reason")
        metrics = _typed_tuple(
            self.qc_metrics,
            QcMetric,
            "qc_metrics",
            allow_empty=True,
        )
        metric_names = tuple(item.name for item in metrics)
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("duplicate ROI QC metric")
        if type(self.accepted) is not bool:
            raise TypeError("ROI accepted must be bool")
        if self.accepted and self.accepted_pixel_count <= 0:
            raise ValueError("accepted ROI requires positive pixels")
        _text_tuple(self.reason_codes, "reason_codes", allow_empty=True)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    decision: str
    reviewer_id: str | None
    reviewed_at: str

    def __post_init__(self) -> None:
        if self.decision not in {"accepted", "pending", "rejected"}:
            raise ValueError("review decision is not supported")
        _optional_text(self.reviewer_id, "reviewer_id")
        _timestamp(self.reviewed_at, "reviewed_at")


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    predecessor_artifact_id: str | None
    predecessor_content_sha256: str | None
    source_artifact_sha256s: tuple[str, ...]
    creation_reason: str

    def __post_init__(self) -> None:
        _optional_text(self.predecessor_artifact_id, "predecessor_artifact_id")
        if self.predecessor_content_sha256 is not None:
            _sha256(
                self.predecessor_content_sha256,
                "predecessor_content_sha256",
            )
        if (self.predecessor_artifact_id is None) != (self.predecessor_content_sha256 is None):
            raise ValueError("predecessor artifact identity and hash must be supplied together")
        hashes = _tuple(self.source_artifact_sha256s, "source_artifact_sha256s")
        validated = tuple(_sha256(item, "source_artifact_sha256") for item in hashes)
        if len(validated) != len(set(validated)):
            raise ValueError("duplicate source artifact SHA-256")
        _required_text(self.creation_reason, "creation_reason")


@dataclass(frozen=True, slots=True)
class VersionRecord:
    name: str
    version: str

    def __post_init__(self) -> None:
        _required_text(self.name, "version name")
        _required_text(self.version, "version")


@dataclass(frozen=True, slots=True)
class RoiBackgroundEvidence:
    """Map a measured region to its real correction, without inventing an aggregate."""
    roi_id: str
    matched_control_roi_id: str | None
    background_model: BackgroundModelEvidence

    def __post_init__(self) -> None:
        _required_text(self.roi_id, "background ROI mapping")
        _optional_text(self.matched_control_roi_id, "matched control ROI mapping")
        if type(self.background_model) is not BackgroundModelEvidence:
            raise TypeError("regional background mapping must retain exact model evidence")


@dataclass(frozen=True, slots=True)
class AcceptedSpectrum:
    """One reviewed spectrum with exact identity, values, scaling, and evidence."""

    component_identity: str
    fluorophore_identity: str | None
    component_kind: str
    normalization_convention: str
    abundance_rescaling_factor: float
    values: tuple[float, ...]
    roi_evidence: tuple[RoiEvidence, ...]
    background_model: BackgroundModelEvidence
    review: ReviewRecord
    regional_backgrounds: tuple[RoiBackgroundEvidence, ...] = field(default=(), metadata={"omit_empty": True})
    spectrum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the AcceptedSpectrum fields when the record is constructed.

        The checks cover ``component_identity``, ``component_kind``,
        ``normalization_convention``, ``abundance_rescaling_factor``, ``values``,
        ``roi_evidence``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        _required_text(self.component_identity, "component_identity")
        if self.component_kind not in _SPECTRUM_KINDS:
            raise ValueError("accepted spectrum component_kind is not supported")
        if self.component_kind == "fluorophore":
            _required_text(self.fluorophore_identity, "fluorophore_identity")
        elif self.fluorophore_identity is not None:
            raise ValueError("target autofluorescence must not claim fluorophore identity")
        if self.normalization_convention not in _PORTABLE_NORMALIZATIONS:
            raise ValueError("accepted spectrum requires declared unit-L1 or unit-L2 normalization")
        _positive(self.abundance_rescaling_factor, "abundance_rescaling_factor")
        values = _tuple(self.values, "spectrum values")
        numeric = tuple(_finite(value, "spectrum value") for value in values)
        if any(value < 0 for value in numeric):
            raise ValueError("spectrum values must be nonnegative")
        if not any(value > 0 for value in numeric):
            raise ValueError("spectrum values must define a nonzero spectrum")
        norm = (math.fsum(numeric) if self.normalization_convention == "unit_l1"
                else math.sqrt(math.fsum(value * value for value in numeric)))
        if not math.isclose(norm, 1.0, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("spectrum values do not match their declared normalization")
        evidence = _typed_tuple(self.roi_evidence, RoiEvidence, "roi_evidence")
        roi_ids = tuple(item.roi_id for item in evidence)
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("duplicate ROI identity in accepted spectrum")
        if not any(item.accepted for item in evidence):
            raise ValueError("accepted spectrum requires accepted ROI evidence")
        if type(self.background_model) is not BackgroundModelEvidence:
            raise TypeError("background_model must be BackgroundModelEvidence")
        if any(
            len(item.values) != len(numeric)
            for item in (
                self.background_model.stained_background,
                self.background_model.control_background,
            )
        ):
            raise ValueError("background spectrum length must match accepted spectrum")
        accepted_by_role = {
            role: tuple(item for item in evidence if item.accepted and item.roi_role == role)
            for role in _ROI_ROLES
        }
        if self.component_kind == "fluorophore":
            positive = tuple(
                item
                for item in accepted_by_role["positive"]
                if item.consensus_inclusion == "included"
            )
            if not positive:
                raise ValueError("fluorophore spectrum requires an accepted positive ROI")
            control_ids = {
                item.roi_id for role in ("af", "negative") for item in accepted_by_role[role]
            }
            if not control_ids:
                raise ValueError("fluorophore spectrum requires an accepted AF or negative ROI")
            if any(len(item.purified_spectrum or ()) != len(numeric) for item in positive):
                raise ValueError("purified ROI spectrum length must match accepted spectrum")
            if any(
                not item.matched_af_roi_ids
                or not set(item.matched_af_roi_ids).issubset(control_ids)
                for item in positive
            ):
                raise ValueError("positive ROI must link accepted AF/negative evidence")
        elif not (accepted_by_role["af"] or any(
                item.inclusion_reason == "reviewed_target_negative_control_af"
                for item in accepted_by_role["negative"])):
            raise ValueError("target AF spectrum requires accepted AF ROI evidence")
        if self.background_model.policy != "validated_offset_corrected_v1":
            background_input_ids = {item.source_input_id for item in accepted_by_role["background"]}
            required_background_inputs = {
                self.background_model.stained_background.source_input_id,
                self.background_model.control_background.source_input_id,
            }
            if not required_background_inputs.issubset(background_input_ids):
                raise ValueError("accepted spectrum requires background ROI evidence")
        _typed_tuple(self.regional_backgrounds, RoiBackgroundEvidence, "regional_backgrounds", allow_empty=True)
        keys = tuple((item.roi_id, item.matched_control_roi_id) for item in self.regional_backgrounds)
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate regional background mapping")
        by_roi = {item.roi_id: item for item in evidence}
        for item in self.regional_backgrounds:
            roi = by_roi.get(item.roi_id)
            control = by_roi.get(item.matched_control_roi_id) if item.matched_control_roi_id else roi
            if roi is None or control is None:
                raise ValueError("regional background mapping requires retained ROI evidence")
            model = item.background_model
            side = model.stained_background if roi.roi_role == "positive" else model.control_background
            if (side.source_input_id != roi.source_input_id or side.source_image_sha256 != roi.source_image_sha256
                    or model.control_background.source_input_id != control.source_input_id
                    or model.control_background.source_image_sha256 != control.source_image_sha256):
                raise ValueError("regional background mapping contradicts ROI sources")
            if any(len(side.values) != len(numeric) for side in (model.stained_background, model.control_background)):
                raise ValueError("regional background axis differs from accepted spectrum")
            if model.policy != "validated_offset_corrected_v1":
                backgrounds = {r.source_input_id for r in accepted_by_role["background"]}
                if not {model.stained_background.source_input_id, model.control_background.source_input_id}.issubset(backgrounds):
                    raise ValueError("regional background requires all sampled background evidence")
        if self.regional_backgrounds and self.regional_backgrounds[0].background_model != self.background_model:
            raise ValueError("primary background must be the first exact regional model")
        if type(self.review) is not ReviewRecord:
            raise TypeError("spectrum review must be a ReviewRecord")
        if self.review.decision != "accepted":
            raise ValueError("accepted spectrum review must be accepted")
        object.__setattr__(self, "spectrum_sha256", _record_digest(self, "spectrum_sha256"))


    @property
    def background_models(self) -> tuple[BackgroundModelEvidence, ...]:
        return tuple(dict.fromkeys((self.background_model, *(item.background_model for item in self.regional_backgrounds))))


@dataclass(frozen=True, slots=True)
class PairwiseSpectrumQc:
    first_component_identity: str
    second_component_identity: str
    cosine_similarity: float
    spectral_angle_degrees: float

    def __post_init__(self) -> None:
        first = _required_text(
            self.first_component_identity,
            "first_component_identity",
        )
        second = _required_text(
            self.second_component_identity,
            "second_component_identity",
        )
        if first == second:
            raise ValueError("pairwise QC requires two distinct components")
        cosine = _finite(self.cosine_similarity, "cosine_similarity")
        if not 0.0 <= cosine <= 1.0:
            raise ValueError("cosine_similarity must be between zero and one")
        angle = _finite(self.spectral_angle_degrees, "spectral_angle_degrees")
        if not 0.0 <= angle <= 90.0:
            raise ValueError("spectral_angle_degrees must be between zero and 90")


@dataclass(frozen=True, slots=True)
class LibraryQcEvidence:
    policy_version: str
    rank: int
    condition_number: float
    pairwise_spectra: tuple[PairwiseSpectrumQc, ...]
    metrics: tuple[QcMetric, ...]
    passed: bool

    def __post_init__(self) -> None:
        _required_text(self.policy_version, "library QC policy_version")
        _positive_integer(self.rank, "library QC rank")
        _positive(self.condition_number, "library QC condition_number")
        pairs = _typed_tuple(
            self.pairwise_spectra,
            PairwiseSpectrumQc,
            "pairwise_spectra",
            allow_empty=True,
        )
        identities = tuple(
            frozenset((item.first_component_identity, item.second_component_identity))
            for item in pairs
        )
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate pairwise spectrum QC identity")
        metrics = _typed_tuple(
            self.metrics,
            QcMetric,
            "library QC metrics",
            allow_empty=True,
        )
        names = tuple(item.name for item in metrics)
        if len(names) != len(set(names)):
            raise ValueError("duplicate library QC metric")
        if type(self.passed) is not bool:
            raise TypeError("library QC passed must be bool")


def _validate_versions(value: object, label: str) -> tuple[VersionRecord, ...]:
    records = _typed_tuple(value, VersionRecord, label)
    names = tuple(item.name for item in records)
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate version identity in {label}")
    return records  # type: ignore[return-value]


def _validate_artifact_spectra(
    *,
    axis: SpectralMeasurementAxis,
    quantitative_signature: QuantitativeTransformSignature,
    normalization_convention: str,
    source_inputs: object,
    spectra: object,
    expected_kind: str,
    qc: LibraryQcEvidence,
) -> tuple[AcceptedSpectrum, ...]:
    if quantitative_signature.measurement_ids != axis.measurement_ids:
        raise ValueError("quantitative measurement axis must match spectral axis exactly")
    records = _typed_tuple(spectra, AcceptedSpectrum, "spectra")
    inputs = _typed_tuple(source_inputs, ReferenceInputEvidence, "source_inputs")
    input_ids = tuple(item.input_id for item in inputs)
    if len(input_ids) != len(set(input_ids)):
        raise ValueError("duplicate source input identity")
    input_index = {item.input_id: item for item in inputs}
    for source_input in inputs:
        if source_input.axis != axis:
            raise ValueError("source input spectral axis must match artifact exactly")
        if not source_input.structurally_compatible and not (
            expected_kind == "target_autofluorescence" and source_input.input_role == "target_image"
        ):
            raise ValueError("accepted artifact source input is structurally incompatible")
        if not source_input.quantitatively_transformable:
            raise ValueError("accepted artifact source input is not quantitatively transformable")
    components = tuple(item.component_identity for item in records)
    if len(components) != len(set(components)):
        raise ValueError("duplicate component identity in artifact")
    fluorophores = tuple(
        item.fluorophore_identity for item in records if item.fluorophore_identity is not None
    )
    if len(fluorophores) != len(set(fluorophores)):
        raise ValueError("duplicate fluorophore identity in artifact")
    if len(records) > len(axis.measurements):
        raise ValueError("artifact has more spectra than spectral measurements")
    axis_ids = set(axis.measurement_ids)
    for spectrum in records:
        if spectrum.component_kind != expected_kind:
            if expected_kind == "fluorophore":
                raise ValueError("portable library is fluorophore-only; target AF is separate")
            raise ValueError("target AF model contains a non-AF spectrum")
        if spectrum.normalization_convention != normalization_convention:
            raise ValueError("spectrum normalization convention does not match artifact")
        if len(spectrum.values) != len(axis.measurements):
            raise ValueError("spectrum length must match spectral measurement axis")
        for background in tuple(side for model in spectrum.background_models
                for side in (model.stained_background, model.control_background)):
            source_input = input_index.get(background.source_input_id)
            if (
                source_input is None
                or source_input.source_image_sha256 != background.source_image_sha256
            ):
                raise ValueError(
                    "background source input identity/hash does not match source input evidence"
                )
        for evidence in spectrum.roi_evidence:
            source_input = input_index.get(evidence.source_input_id)
            if (
                source_input is None
                or source_input.source_image_sha256 != evidence.source_image_sha256
            ):
                raise ValueError(
                    "ROI source input identity/hash does not match source input evidence"
                )
            referenced_ids = (
                () if evidence.signal_measurement_id is None else (evidence.signal_measurement_id,)
            ) + evidence.dark_measurement_ids
            if not set(referenced_ids).issubset(axis_ids):
                raise ValueError("ROI evidence references an unknown spectral measurement")
        if expected_kind == "fluorophore":
            positive_inputs = tuple(
                input_index[item.source_input_id]
                for item in spectrum.roi_evidence
                if item.roi_role == "positive"
            )
            if any(
                item.input_role != "single_component_reference"
                or item.fluorophore_identity != spectrum.fluorophore_identity
                for item in positive_inputs
            ):
                raise ValueError("positive ROI source must bind the exact fluorophore identity")
            for evidence in spectrum.roi_evidence:
                source_input = input_index[evidence.source_input_id]
                if evidence.roi_role == "af" and not (
                    source_input.input_role == "unstained_control"
                    or source_input.input_role == "single_stain_negative_control"
                    and source_input.fluorophore_identity == spectrum.fluorophore_identity
                ):
                    raise ValueError("AF ROI source must be unstained or the exact stain's prepared control")
                if evidence.roi_role == "negative" and (
                    source_input.input_role
                    not in {
                        "single_component_reference",
                        "single_stain_negative_control",
                    }
                    or source_input.fluorophore_identity != spectrum.fluorophore_identity
                ):
                    raise ValueError(
                        "negative ROI source role must match the exact single-component reference"
                    )
            positive_source_ids = {
                item.source_input_id
                for item in spectrum.roi_evidence
                if item.roi_role == "positive"
            }
            control_source_ids = {
                item.source_input_id
                for item in spectrum.roi_evidence
                if item.roi_role in {"af", "negative"}
            }
            if (
                spectrum.background_model.stained_background.source_input_id
                not in positive_source_ids
            ):
                raise ValueError("stained background source must match a positive reference source")
            if (
                spectrum.background_model.control_background.source_input_id
                not in control_source_ids
            ):
                raise ValueError("control background source must match an AF or negative source")
        else:
            af_source_ids: set[str] = set()
            for evidence in spectrum.roi_evidence:
                source_input = input_index[evidence.source_input_id]
                if evidence.roi_role == "af":
                    if source_input.input_role not in {
                        "target_unstained_control",
                        "unstained_control",
                    } and not (source_input.input_role == "single_stain_negative_control"
                               and evidence.inclusion_reason == "reviewed_target_negative_control_af"):
                        raise ValueError(
                            "target AF ROI source role must be a compatible unstained control"
                        )
                    af_source_ids.add(evidence.source_input_id)
                elif evidence.roi_role == "negative" and (
                        source_input.input_role == "single_stain_negative_control"
                        and evidence.inclusion_reason == "reviewed_target_negative_control_af"):
                    af_source_ids.add(evidence.source_input_id)
                elif evidence.roi_role in {"positive", "negative"}:
                    raise ValueError(
                        "target AF model must not contain positive or negative ROI evidence"
                    )
            background_source_ids = {
                spectrum.background_model.stained_background.source_input_id,
                spectrum.background_model.control_background.source_input_id,
            }
            reviewed_background_source_ids = {
                evidence.source_input_id
                for evidence in spectrum.roi_evidence
                if evidence.roi_role == "background"
            }
            if not background_source_ids.issubset(af_source_ids | reviewed_background_source_ids):
                raise ValueError(
                    "target background source must match target AF or reviewed background ROI evidence"
                )
        allowed_background_source_ids = {side.source_input_id for model in spectrum.background_models
            for side in (model.stained_background, model.control_background)}
        if any(
            item.roi_role == "background"
            and item.source_input_id not in allowed_background_source_ids
            for item in spectrum.roi_evidence
        ):
            raise ValueError("background ROI source must match the explicit background model")
    if qc.rank != len(records):
        raise ValueError("accepted artifact QC rank must equal accepted spectrum count")
    computed_rank, computed_condition_number = _basis_rank_condition(
        tuple(tuple(float(value) for value in item.values) for item in records)
    )
    if computed_rank != len(records):
        raise ValueError("accepted artifact spectra must have full column rank")
    if qc.rank != computed_rank:
        raise ValueError("accepted artifact QC rank does not match spectrum matrix")
    if not math.isclose(
        qc.condition_number,
        computed_condition_number,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise ValueError("accepted artifact QC condition number does not match spectrum matrix")
    if not qc.passed:
        raise ValueError("accepted artifact QC must have passed")
    component_set = set(components)
    for pair in qc.pairwise_spectra:
        if {
            pair.first_component_identity,
            pair.second_component_identity,
        } - component_set:
            raise ValueError("pairwise QC identities must name artifact components")
    expected_pairs = {
        frozenset((components[first], components[second]))
        for first in range(len(components))
        for second in range(first + 1, len(components))
    }
    observed_pairs = {
        frozenset((pair.first_component_identity, pair.second_component_identity))
        for pair in qc.pairwise_spectra
    }
    if observed_pairs != expected_pairs:
        raise ValueError("accepted artifact requires complete pairwise QC coverage")
    spectra_by_identity = {
        item.component_identity: tuple(float(value) for value in item.values) for item in records
    }
    for pair in qc.pairwise_spectra:
        first = spectra_by_identity[pair.first_component_identity]
        second = spectra_by_identity[pair.second_component_identity]
        dot_product = math.fsum(left * right for left, right in zip(first, second))
        first_norm = math.sqrt(math.fsum(value * value for value in first))
        second_norm = math.sqrt(math.fsum(value * value for value in second))
        cosine_similarity = min(
            1.0,
            max(0.0, dot_product / (first_norm * second_norm)),
        )
        spectral_angle_degrees = math.degrees(math.acos(cosine_similarity))
        if not math.isclose(
            pair.cosine_similarity,
            cosine_similarity,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("pairwise QC cosine similarity does not match component vectors")
        if not math.isclose(
            pair.spectral_angle_degrees,
            spectral_angle_degrees,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("pairwise QC spectral angle does not match component vectors")
    return records  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FluorophoreReferenceLibrary:
    """Portable reviewed fluorophore-only artifact."""

    SCHEMA_NAME: ClassVar[str] = "mif_pipeline.fluorophore_reference_library"
    SCHEMA_VERSION: ClassVar[int] = 2

    artifact_id: str
    created_at: str
    axis: SpectralMeasurementAxis
    structural_signature: StructuralCompatibilitySignature
    quantitative_signature: QuantitativeTransformSignature
    source_inputs: tuple[ReferenceInputEvidence, ...]
    normalization_convention: str
    spectra: tuple[AcceptedSpectrum, ...]
    qc: LibraryQcEvidence
    software_versions: tuple[VersionRecord, ...]
    policy_versions: tuple[VersionRecord, ...]
    review: ReviewRecord
    lineage: ArtifactLineage
    schema_name: str = field(init=False, default=SCHEMA_NAME)
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FluorophoreReferenceLibrary fields when the record is constructed.

        The checks cover ``artifact_id``, ``created_at``, ``normalization_convention``,
        ``software_versions``, ``policy_versions``, ``axis``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _required_text(self.artifact_id, "artifact_id")
        _timestamp(self.created_at, "created_at")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("axis must be a SpectralMeasurementAxis")
        if type(self.structural_signature) is not StructuralCompatibilitySignature:
            raise TypeError("structural_signature has the wrong record type")
        if type(self.quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("quantitative_signature has the wrong record type")
        if self.normalization_convention not in _PORTABLE_NORMALIZATIONS:
            raise ValueError("portable library normalization convention must be unit-L1 or unit-L2")
        if type(self.qc) is not LibraryQcEvidence:
            raise TypeError("qc must be LibraryQcEvidence")
        if any(
            item.input_role in {"target_image", "target_unstained_control"}
            for item in self.source_inputs
        ):
            raise ValueError("portable library must not contain target source inputs")
        _validate_artifact_spectra(
            axis=self.axis,
            quantitative_signature=self.quantitative_signature,
            normalization_convention=self.normalization_convention,
            source_inputs=self.source_inputs,
            spectra=self.spectra,
            expected_kind="fluorophore",
            qc=self.qc,
        )
        _validate_versions(self.software_versions, "software_versions")
        _validate_versions(self.policy_versions, "policy_versions")
        if type(self.review) is not ReviewRecord:
            raise TypeError("review must be ReviewRecord")
        if self.review.decision != "accepted":
            raise ValueError("portable library review must be accepted")
        if type(self.lineage) is not ArtifactLineage:
            raise TypeError("lineage must be ArtifactLineage")
        object.__setattr__(self, "content_sha256", _record_digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class TargetAutofluorescenceModel:
    """Non-authoritative compatibility view derived from a typed Task-7 artifact."""

    SCHEMA_NAME: ClassVar[str] = "mif_pipeline.target_autofluorescence_model"
    SCHEMA_VERSION: ClassVar[int] = 2

    artifact_id: str
    created_at: str
    run_id: str
    target_image_sha256: str
    axis: SpectralMeasurementAxis
    structural_signature: StructuralCompatibilitySignature
    quantitative_signature: QuantitativeTransformSignature
    source_inputs: tuple[ReferenceInputEvidence, ...]
    normalization_convention: str
    estimation_method: str
    endmembers: tuple[AcceptedSpectrum, ...]
    qc: LibraryQcEvidence
    software_versions: tuple[VersionRecord, ...]
    policy_versions: tuple[VersionRecord, ...]
    review: ReviewRecord
    lineage: ArtifactLineage
    schema_name: str = field(init=False, default=SCHEMA_NAME)
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAutofluorescenceModel fields when the record is constructed.

        The checks cover ``endmembers``, ``artifact_id``, ``created_at``, ``run_id``,
        ``target_image_sha256``, ``normalization_convention``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        _required_text(self.artifact_id, "artifact_id")
        _timestamp(self.created_at, "created_at")
        _required_text(self.run_id, "run_id")
        _sha256(self.target_image_sha256, "target_image_sha256")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("axis must be a SpectralMeasurementAxis")
        if type(self.structural_signature) is not StructuralCompatibilitySignature:
            raise TypeError("structural_signature has the wrong record type")
        if type(self.quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("quantitative_signature has the wrong record type")
        if self.normalization_convention not in _PORTABLE_NORMALIZATIONS:
            raise ValueError("target AF model requires unit-L1 or unit-L2 normalization")
        if self.estimation_method not in {"compatible_unstained_control", "compatible_unstained_control_equal_roi_mean_v1",
                                        "reviewed_reference_control_equal_roi_mean_v1"}:
            raise ValueError("target AF model supports only compatible unstained control")
        if type(self.qc) is not LibraryQcEvidence:
            raise TypeError("qc must be LibraryQcEvidence")
        target_inputs = tuple(
            item for item in self.source_inputs if item.input_role == "target_image"
        )
        if (
            len(target_inputs) != 1
            or target_inputs[0].source_image_sha256 != self.target_image_sha256
        ):
            raise ValueError("target image SHA-256 must match exactly one target input")
        _validate_artifact_spectra(
            axis=self.axis,
            quantitative_signature=self.quantitative_signature,
            normalization_convention=self.normalization_convention,
            source_inputs=self.source_inputs,
            spectra=self.endmembers,
            expected_kind="target_autofluorescence",
            qc=self.qc,
        )
        accepted_af_roi_ids: list[str] = []
        for endmember in self.endmembers:
            af_evidence = tuple(
                item for item in endmember.roi_evidence if item.accepted and item.roi_role in {"af", "negative"}
            )
            if self.estimation_method == "compatible_unstained_control":
                if len(af_evidence) != 1:
                    raise ValueError("each legacy target-AF endmember requires exactly one biological AF ROI")
            elif len(af_evidence) > 1 and tuple(item.roi_id for item in endmember.regional_backgrounds) != tuple(item.roi_id for item in af_evidence):
                raise ValueError("grouped target AF requires every exact regional background mapping")
            accepted_af_roi_ids.extend(item.roi_id for item in af_evidence)
        if len(accepted_af_roi_ids) != len(set(accepted_af_roi_ids)):
            raise ValueError("one biological AF ROI cannot support multiple target-AF endmembers")
        _validate_versions(self.software_versions, "software_versions")
        _validate_versions(self.policy_versions, "policy_versions")
        if type(self.review) is not ReviewRecord:
            raise TypeError("review must be ReviewRecord")
        if self.review.decision != "accepted":
            raise ValueError("target AF review must be accepted")
        if type(self.lineage) is not ArtifactLineage:
            raise TypeError("lineage must be ArtifactLineage")
        expected_source_prefix = tuple(dict.fromkeys(
            item.source_image_sha256 for item in self.source_inputs
        ))
        if (
            self.lineage.source_artifact_sha256s[: len(expected_source_prefix)]
            != expected_source_prefix
        ):
            raise ValueError(
                "legacy target-AF source lineage must begin with every exact source input"
            )
        object.__setattr__(self, "content_sha256", _record_digest(self, "content_sha256"))

    @property
    def authoritative(self) -> bool:
        """Legacy model bytes are a derived view; only the wrapper is authority."""

        return False


@dataclass(frozen=True, slots=True)
class BasisSourceLineage:
    source_kind: str
    artifact_id: str
    content_sha256: str
    run_id: str | None

    def __post_init__(self) -> None:
        if self.source_kind not in _SOURCE_KINDS:
            raise ValueError("basis source_kind is not supported")
        _required_text(self.artifact_id, "basis source artifact_id")
        _sha256(self.content_sha256, "basis source content_sha256")
        _optional_text(self.run_id, "basis source run_id")
        if (
            self.source_kind
            in {
                "target_autofluorescence_model",
                "accepted_cycle_basis",
            }
            and self.run_id is None
        ):
            raise ValueError("target-specific basis source requires run_id")
        if (
            self.source_kind
            not in {
                "target_autofluorescence_model",
                "accepted_cycle_basis",
            }
            and self.run_id is not None
        ):
            raise ValueError("only target-specific basis sources may carry run_id")


@dataclass(frozen=True, slots=True)
class ResolvedBasisColumn:
    """Exact accepted basis column plus source, scaling, and rescaling lineage."""

    component_identity: str
    fluorophore_identity: str | None
    component_kind: str
    values: tuple[float, ...]
    normalization_convention: str
    abundance_rescaling_factor: float
    source: BasisSourceLineage
    spectrum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the ResolvedBasisColumn fields when the record is constructed.

        The checks cover ``component_identity``, ``component_kind``, ``values``,
        ``normalization_convention``, ``abundance_rescaling_factor``,
        ``fluorophore_identity``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        _required_text(self.component_identity, "resolved component_identity")
        if self.component_kind not in _RESOLVED_KINDS:
            raise ValueError("resolved basis component_kind is not supported")
        if self.component_kind == "fluorophore":
            _required_text(self.fluorophore_identity, "resolved fluorophore_identity")
        elif self.fluorophore_identity is not None:
            raise ValueError("resolved AF column must not claim fluorophore identity")
        values = _tuple(self.values, "resolved basis values")
        numeric = tuple(_finite(value, "resolved basis value") for value in values)
        if any(value < 0 for value in numeric):
            raise ValueError("resolved basis values must be nonnegative")
        if not any(value > 0 for value in numeric):
            raise ValueError("resolved basis column must be nonzero")
        _required_text(self.normalization_convention, "normalization_convention")
        _positive(self.abundance_rescaling_factor, "abundance_rescaling_factor")
        if type(self.source) is not BasisSourceLineage:
            raise TypeError("resolved basis source must be BasisSourceLineage")
        if (
            self.source.source_kind == "fluorophore_reference_library"
            and self.component_kind != "fluorophore"
        ):
            raise ValueError("library source may provide only a fluorophore column")
        if (
            self.source.source_kind
            in {
                "target_autofluorescence_model",
            }
            and self.component_kind != "target_autofluorescence"
        ):
            raise ValueError("target AF source may provide only target autofluorescence")
        if self.source.source_kind == "instrument_profile" and self.component_kind not in {
            "fluorophore",
            "profile_autofluorescence",
        }:
            raise ValueError("instrument profile source may provide only profile fluorophore or AF")
        object.__setattr__(self, "spectrum_sha256", _record_digest(self, "spectrum_sha256"))


@dataclass(frozen=True, slots=True)
class ResolvedRunBasis:
    """Ordered exact run basis, with no implicit normalization or source fallback."""

    run_id: str
    axis: SpectralMeasurementAxis
    columns: tuple[ResolvedBasisColumn, ...]
    review: ReviewRecord
    rank: int = field(init=False)
    condition_number: float = field(init=False)

    def __post_init__(self) -> None:
        """Validate the ResolvedRunBasis fields when the record is constructed.

        The checks cover ``run_id``, ``columns``, ``axis``, ``review``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _required_text(self.run_id, "run_id")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("axis must be SpectralMeasurementAxis")
        columns = _typed_tuple(self.columns, ResolvedBasisColumn, "resolved basis columns")
        identities = tuple(item.component_identity for item in columns)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate resolved basis component identity")
        if len(columns) > len(self.axis.measurements):
            raise ValueError("resolved basis has more columns than measurements")
        if any(len(item.values) != len(self.axis.measurements) for item in columns):
            raise ValueError("resolved basis column length must match spectral axis")
        if not any(item.component_kind == "fluorophore" for item in columns):
            raise ValueError("resolved basis requires at least one fluorophore")
        rank, condition_number = _basis_rank_condition(
            tuple(tuple(float(value) for value in item.values) for item in columns)
        )
        if rank != len(columns):
            raise ValueError("resolved basis columns must have full column rank")
        if not math.isfinite(condition_number):
            raise ValueError("resolved basis condition number must be finite")
        target_sources = {
            item.source.run_id
            for item in columns
            if item.source.source_kind
            in {
                "target_autofluorescence_model",
            }
        }
        if target_sources and target_sources != {self.run_id}:
            raise ValueError("resolved target AF lineage must match run_id")
        if type(self.review) is not ReviewRecord:
            raise TypeError("resolved basis review must be ReviewRecord")
        if self.review.decision != "accepted":
            raise ValueError("resolved basis review must be accepted")
        object.__setattr__(self, "rank", rank)
        object.__setattr__(self, "condition_number", condition_number)

    @property
    def component_order(self) -> tuple[str, ...]:
        return tuple(item.component_identity for item in self.columns)

    @classmethod
    def from_artifacts(
        cls,
        *,
        run_id: str,
        fluorophore_library: FluorophoreReferenceLibrary,
        fluorophore_order: tuple[str, ...],
        target_autofluorescence_model: TargetAutofluorescenceModel,
        review: ReviewRecord,
    ) -> ResolvedRunBasis:
        """Reject the retired weaker factory; owns authoritative assembly."""

        _required_text(run_id, "run_id")
        if type(fluorophore_library) is not FluorophoreReferenceLibrary:
            raise TypeError("fluorophore_library has the wrong artifact type")
        if type(target_autofluorescence_model) is not TargetAutofluorescenceModel:
            raise TypeError("target_autofluorescence_model has the wrong artifact type")
        _text_tuple(fluorophore_order, "fluorophore_order")
        if type(review) is not ReviewRecord:
            raise TypeError("review has the wrong record type")
        raise ValueError(
            "ResolvedRunBasis.from_artifacts is a non-authoritative legacy factory; "
            "use the exact Task-7 candidate, compatibility evidence, and acceptance artifact"
        )


ReferenceArtifact = FluorophoreReferenceLibrary | TargetAutofluorescenceModel


__all__ = [
    "AcceptedSpectrum",
    "ArtifactLineage",
    "BackgroundModelEvidence",
    "BackgroundSpectrum",
    "BasisSourceLineage",
    "FluorophoreReferenceLibrary",
    "LibraryQcEvidence",
    "PairwiseSpectrumQc",
    "QcMetric",
    "QuantitativeMeasurementTransform",
    "QuantitativeTransformSignature",
    "ReferenceArtifact",
    "ReferenceInputEvidence",
    "RejectionCount",
    "ResolvedBasisColumn",
    "ResolvedRunBasis",
    "ReviewRecord",
    "RoiEvidence",
    "RoiBackgroundEvidence",
    "SpectralMeasurement",
    "SpectralOpticalSetting",
    "SpectralMeasurementAxis",
    "StructuralCompatibilitySignature",
    "TargetAutofluorescenceModel",
    "VersionRecord",
]
