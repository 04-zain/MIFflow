"""Fail-closed spectral-reference compatibility and basis-source preflight.

The `BasisSourceResolution` record carries `choice`, `source_choice_accepted`,
`structural_reports`, `quantitative_reports`, `resolved_basis` and its remaining declared
fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component

from .reference_models import _record_fields
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import TypeAlias

from .reference_models import (
    BasisSourceLineage,
    FluorophoreReferenceLibrary,
    QuantitativeTransformSignature,
    ResolvedBasisColumn,
    ResolvedRunBasis,
    ReviewRecord,
    SpectralMeasurementAxis,
    StructuralCompatibilitySignature,
    TargetAutofluorescenceModel,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PINNED_MOTIF_RESOURCE_NAME = "PhenoImager_HT_spectral_library.umxlib"
_PINNED_MOTIF_RESOURCE_SHA256 = "b59e06af09eb934d2cecf29c21ebb418fedd0e4ce301a36caf7f11dea5d94d3f"
_TRUSTED_MOTIF_AUTHORITY_TOKEN = object()


CompatibilityValue: TypeAlias = str | int | float | None | tuple[str | int | float | None, ...]


_STRUCTURAL_FIELD_POLICIES = (
    ("correction_mode", "exact"),
    ("measurement_ids", "exact"),
    ("parent_band_ids", "exact"),
    ("sample_ids", "exact"),
    ("wavelength_values", "exact"),
    ("wavelength_units", "exact"),
    ("instrument_id", "exact"),
    ("optical_path_id", "exact"),
    ("acquisition_profile_id", "authority"),
    ("detector_mode", "exact"),
    ("response_calibration_id", "authority"),
)

_QUANTITATIVE_FIELD_POLICIES = (
    ("measurement_ids", "exact"),
    ("normalization_policies", "exact"),
    ("response_transform_semantics", "exact"),
    ("response_transform_ids", "exact"),
    ("response_transform_authorities", "exact"),
    ("intensity_representations", "transformable"),
    ("effective_digital_maxima", "transformable"),
    ("digital_range_meaningful_bits", "transformable"),
    ("digital_range_authorities", "transformable"),
    ("digital_range_profile_ids", "transformable"),
    ("offsets", "transformable"),
    ("offset_authorities", "transformable"),
    ("offset_profile_identifiers", "transformable"),
    ("exposure_seconds", "transformable"),
    ("gains", "transformable"),
    ("binning_areas", "transformable"),
    ("response_correction_multipliers", "transformable"),
)


# ==============================================================================
#                                 Compatibility
# ==============================================================================


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _report_digest(record: object, omitted_field: str) -> str:
    payload = {
        item.name: _primitive(getattr(record, item.name))
        for item in fields(record)
        if item.name != omitted_field
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in _record_fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported compatibility report value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CompatibilityFieldReport:
    """One immutable comparison without hiding the two exact values."""

    field_name: str
    reference_value: CompatibilityValue
    candidate_value: CompatibilityValue
    accepted: bool
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.field_name, str) or not self.field_name.strip():
            raise ValueError("compatibility field_name must be non-empty text")
        if self.field_name != self.field_name.strip():
            raise ValueError("compatibility field_name must be literal text")
        if type(self.accepted) is not bool:
            raise TypeError("compatibility field accepted must be bool")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("compatibility reason_code must be non-empty text")


@dataclass(frozen=True, slots=True)
class StructuralCompatibilityReport:
    """Exact axis/profile comparisons, separate from numeric transformability."""

    SCHEMA_NAME = "mif_pipeline.structural_compatibility_report"
    SCHEMA_VERSION = 1

    fields: tuple[CompatibilityFieldReport, ...]
    schema_name: str = field(init=False, default=SCHEMA_NAME)
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    compatible: bool = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_report_fields(self.fields, _STRUCTURAL_FIELD_POLICIES)
        object.__setattr__(
            self,
            "compatible",
            all(item.accepted for item in self.fields),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _report_digest(self, "content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class QuantitativeTransformabilityReport:
    """Field evidence that two inputs can reach one processing coordinate system."""

    SCHEMA_NAME = "mif_pipeline.quantitative_transformability_report"
    SCHEMA_VERSION = 1

    fields: tuple[CompatibilityFieldReport, ...]
    schema_name: str = field(init=False, default=SCHEMA_NAME)
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    transformable: bool = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_report_fields(self.fields, _QUANTITATIVE_FIELD_POLICIES)
        object.__setattr__(
            self,
            "transformable",
            all(item.accepted for item in self.fields),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _report_digest(self, "content_sha256"),
        )


CompatibilityReport: TypeAlias = StructuralCompatibilityReport | QuantitativeTransformabilityReport


def dumps_compatibility_report(report: CompatibilityReport) -> str:
    """Render one report as strict deterministic checksummed JSON."""
    if type(report) not in {
        StructuralCompatibilityReport,
        QuantitativeTransformabilityReport,
    }:
        raise TypeError("report has an unsupported compatibility report type")
    return _canonical_json_bytes(_primitive(report)).decode("utf-8") + "\n"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate compatibility report field {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite compatibility report number {value!r}")


def loads_compatibility_report(payload: str | bytes) -> CompatibilityReport:
    """Load one strict report and reject schema drift or content tampering."""
    try:
        root = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid compatibility report JSON") from exc
    if not isinstance(root, dict):
        raise ValueError("compatibility report root must be an object")
    schema_name = root.get("schema_name")
    if schema_name == StructuralCompatibilityReport.SCHEMA_NAME:
        report_type: type[CompatibilityReport] = StructuralCompatibilityReport
        status_field = "compatible"
    elif schema_name == QuantitativeTransformabilityReport.SCHEMA_NAME:
        report_type = QuantitativeTransformabilityReport
        status_field = "transformable"
    else:
        raise ValueError(f"unsupported compatibility report schema {schema_name!r}")
    expected_fields = {
        "fields",
        "schema_name",
        "schema_version",
        status_field,
        "content_sha256",
    }
    if set(root) != expected_fields:
        raise ValueError(
            "compatibility report schema fields differ; "
            f"missing={sorted(expected_fields - set(root))}, "
            f"extra={sorted(set(root) - expected_fields)}"
        )
    if root["schema_version"] != report_type.SCHEMA_VERSION:
        raise ValueError("unsupported compatibility report schema version")
    declared_sha256 = root["content_sha256"]
    _sha256(declared_sha256, "compatibility report content SHA-256")
    hash_payload = dict(root)
    del hash_payload["content_sha256"]
    actual_sha256 = hashlib.sha256(_canonical_json_bytes(hash_payload)).hexdigest()
    if declared_sha256 != actual_sha256:
        raise ValueError("compatibility report content SHA-256 does not match")
    raw_fields = root["fields"]
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("compatibility report fields must be a non-empty array")
    parsed_fields: list[CompatibilityFieldReport] = []
    expected_item_fields = {
        "field_name",
        "reference_value",
        "candidate_value",
        "accepted",
        "reason_code",
    }
    for index, item in enumerate(raw_fields):
        if not isinstance(item, dict) or set(item) != expected_item_fields:
            raise ValueError(f"compatibility report fields[{index}] schema fields differ")
        parsed_fields.append(
            CompatibilityFieldReport(
                field_name=item["field_name"],
                reference_value=_immutable_json_value(item["reference_value"]),
                candidate_value=_immutable_json_value(item["candidate_value"]),
                accepted=item["accepted"],
                reason_code=item["reason_code"],
            )
        )
    report = report_type(fields=tuple(parsed_fields))
    if getattr(report, status_field) != root[status_field]:
        raise ValueError("compatibility report status conflicts with its fields")
    if report.content_sha256 != declared_sha256:
        raise ValueError("compatibility report content SHA-256 does not match")
    return report


def _immutable_json_value(value: object) -> CompatibilityValue:
    if isinstance(value, list):
        return tuple(_immutable_json_value(item) for item in value)  # type: ignore[return-value]
    if value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool)):
        return value
    raise ValueError("compatibility report contains an unsupported field value")


def align_library_measurements(
    library: FluorophoreReferenceLibrary, target_axis: SpectralMeasurementAxis,
) -> tuple[SpectralMeasurementAxis, QuantitativeTransformSignature, tuple[int, ...]]:
    """Align accepted library rows without changing the imported artifact.

    Exact, unique acquired-band labels bind values and their wavelength and
    acquisition records to the target order. The returned positions also reorder
    spectrum values during final-basis assembly. Raw reference-image comparisons
    keep their existing order requirements because their pixels are not moved here.
    """
    from .curated_library import _spectral_label_order

    order = _spectral_label_order(library.axis.parent_band_ids, target_axis.parent_band_ids)
    if order == tuple(range(len(order))):
        return library.axis, library.quantitative_signature, order
    axis = SpectralMeasurementAxis(tuple(library.axis.measurements[i] for i in order))
    signature = replace(library.quantitative_signature,
                        measurements=tuple(library.quantitative_signature.measurements[i] for i in order))
    return axis, signature, order


def _ordered_band_match(reference, candidate):
    """Require one unambiguous acquisition identity per position; never reorder rows."""
    return bool(reference) and reference == candidate and len(set(reference)) == len(reference)


def _axis_comparison_fields(values):
    """Compare local identifiers through ordered bands and known wavelength facts.

    The mapping requires identical unique recorded band identities. Source-local
    IDs remain in the report. Wavelengths are optional per band; two known values
    must describe the same physical length. No wavelengths are inferred from names.
    """
    from ome_types.units import ureg
    by_name = {name: (left, right) for name, left, right in values}
    aligned = _ordered_band_match(*by_name["parent_band_ids"])
    wave_left, wave_right = by_name["wavelength_values"]
    units_left, units_right = by_name["wavelength_units"]
    wavelengths_match = len(wave_left) == len(wave_right)
    if wavelengths_match:
        for left, right, left_unit, right_unit in zip(wave_left, wave_right, units_left, units_right, strict=True):
            if left is None or right is None:
                continue
            if left == right and left_unit == right_unit:
                continue
            try:
                left_nm = float((left * ureg(left_unit)).to(ureg.nanometer).magnitude)
                right_nm = float((right * ureg(right_unit)).to(ureg.nanometer).magnitude)
            except (ValueError, TypeError, AttributeError):
                wavelengths_match = False
                break
            if not math.isclose(left_nm, right_nm, rel_tol=1e-12, abs_tol=0):
                wavelengths_match = False
                break
    result = []
    for name, left, right in values:
        if left == right:
            item = _exact_field(name, left, right)
        elif name in {"measurement_ids", "sample_ids"} and aligned:
            item = CompatibilityFieldReport(name, left, right, True, "same_ordered_acquisition_bands")
        elif name in {"wavelength_values", "wavelength_units"}:
            item = CompatibilityFieldReport(name, left, right, wavelengths_match,
                "compatible_recorded_wavelengths" if wavelengths_match else "recorded_wavelength_mismatch")
        else:
            item = _exact_field(name, left, right)
        result.append(item)
    return tuple(result)


def _validate_report_fields(
    value: object,
    field_policies: tuple[tuple[str, str], ...],
) -> None:
    if type(value) is not tuple or not value:
        raise ValueError("compatibility report fields must be a non-empty tuple")
    if any(type(item) is not CompatibilityFieldReport for item in value):
        raise TypeError("compatibility report fields have the wrong record type")
    names = tuple(item.field_name for item in value)
    expected_names = tuple(name for name, _policy in field_policies)
    if names != expected_names:
        raise ValueError(
            "compatibility report must contain its exact complete field inventory "
            "in canonical order"
        )
    same_settings = next((
        "as_stored" if item.reference_value in {"as_stored", "mif_pipeline_as_stored_v1"}
        else "full" if item.reference_value == "full" else "exposure_only"
        for item in value
        if item.field_name in {"correction_mode", "normalization_policies"}
        and item.reference_value == item.candidate_value
        and item.reference_value in {"full", "exposure_only", "mif_pipeline_exposure_only_v1", "as_stored", "mif_pipeline_as_stored_v1"}
    ), None)
    axis_fields = {}
    if field_policies is _STRUCTURAL_FIELD_POLICIES:
        names = {"measurement_ids", "parent_band_ids", "sample_ids", "wavelength_values", "wavelength_units"}
        axis_fields = {item.field_name: item for item in _axis_comparison_fields(tuple(
            (item.field_name, item.reference_value, item.candidate_value) for item in value if item.field_name in names))}
    for item, (_field_name, policy) in zip(value, field_policies, strict=True):
        expected = axis_fields.get(item.field_name) or _comparison_field(item.field_name, item.reference_value, item.candidate_value, policy, same_settings)
        if item != expected:
            raise ValueError(f"Compatibility field {item.field_name!r} conflicts with its comparison policy")


def _comparison_field(name, reference, candidate, policy, same_settings=False):
    """Compare known settings under the selected intensity correction convention.

    Full correction permits acquisition differences resolved by its applied factors.
    Exposure-only and as-stored fitting permit missing settings under their comparability
    requirement but reject known conflicts. Only as-stored fitting also requires matching
    known exposure times because those stored values receive no intensity correction.
    """
    if same_settings == "full":
        if name in {"acquisition_profile_id", "response_calibration_id"}:
            if reference is not None and reference == candidate:
                return _exact_field(name, reference, candidate)
            return CompatibilityFieldReport(
                name, reference, candidate, True, "profile_identifiers_are_provenance_only"
            )
        if name in {"instrument_id", "optical_path_id", "detector_mode"}:
            if reference is None or candidate is None:
                return CompatibilityFieldReport(
                    name, reference, candidate, True,
                    "unrecorded_optical_settings_require_matching_acquisition_review",
                )
            return _exact_field(name, reference, candidate)
    if same_settings in {"exposure_only", "as_stored"} and name in {"instrument_id", "optical_path_id", "detector_mode", "acquisition_profile_id", "response_calibration_id"}:
        if reference is None or candidate is None or (same_settings == "exposure_only" and name == "response_calibration_id"):
            return CompatibilityFieldReport(name, reference, candidate, True, "declared_identical_acquisition_settings")
    if same_settings in {"exposure_only", "as_stored"} and (name in {"gains", "binning_areas", "offsets", "effective_digital_maxima", "digital_range_meaningful_bits", "intensity_representations"} or (same_settings == "as_stored" and name == "exposure_seconds")):
        compatible = len(reference) == len(candidate) and all(
            left is None or right is None or left == "unknown" or right == "unknown" or left == right
            for left, right in zip(reference, candidate, strict=True)
        )
        return CompatibilityFieldReport(name, reference, candidate, compatible,
            "declared_identical_acquisition_settings" if compatible else "known_acquisition_settings_mismatch")
    if policy == "authority":
        return _authority_field(name, reference, candidate)
    if policy == "transformable":
        return _transformable_difference(name, reference, candidate)
    return _exact_field(name, reference, candidate)


def _exact_field(
    field_name: str,
    reference_value: CompatibilityValue,
    candidate_value: CompatibilityValue,
) -> CompatibilityFieldReport:
    accepted = reference_value == candidate_value
    return CompatibilityFieldReport(
        field_name=field_name,
        reference_value=reference_value,
        candidate_value=candidate_value,
        accepted=accepted,
        reason_code="exact_match" if accepted else "exact_value_mismatch",
    )


def _authority_field(
    field_name: str,
    reference_value: CompatibilityValue,
    candidate_value: CompatibilityValue,
) -> CompatibilityFieldReport:
    if reference_value is None or candidate_value is None:
        return CompatibilityFieldReport(
            field_name=field_name,
            reference_value=reference_value,
            candidate_value=candidate_value,
            accepted=False,
            reason_code="missing_authority",
        )
    return _exact_field(field_name, reference_value, candidate_value)


def compare_structural_compatibility(
    *,
    reference_axis: SpectralMeasurementAxis,
    candidate_axis: SpectralMeasurementAxis,
    reference_signature: StructuralCompatibilitySignature,
    candidate_signature: StructuralCompatibilitySignature,
) -> StructuralCompatibilityReport:
    """Compare exact band axes and known optical facts, retaining profile provenance."""
    if type(reference_axis) is not SpectralMeasurementAxis:
        raise TypeError("reference_axis must be SpectralMeasurementAxis")
    if type(candidate_axis) is not SpectralMeasurementAxis:
        raise TypeError("candidate_axis must be SpectralMeasurementAxis")
    if type(reference_signature) is not StructuralCompatibilitySignature:
        raise TypeError("reference_signature must be StructuralCompatibilitySignature")
    if type(candidate_signature) is not StructuralCompatibilitySignature:
        raise TypeError("candidate_signature must be StructuralCompatibilitySignature")

    reference_measurements = reference_axis.measurements
    candidate_measurements = candidate_axis.measurements
    axis_values: tuple[tuple[str, CompatibilityValue, CompatibilityValue], ...] = (
        (
            "measurement_ids",
            tuple(item.measurement_id for item in reference_measurements),
            tuple(item.measurement_id for item in candidate_measurements),
        ),
        (
            "parent_band_ids",
            tuple(item.parent_band_id for item in reference_measurements),
            tuple(item.parent_band_id for item in candidate_measurements),
        ),
        (
            "sample_ids",
            tuple(item.sample_id for item in reference_measurements),
            tuple(item.sample_id for item in candidate_measurements),
        ),
        (
            "wavelength_values",
            tuple(item.wavelength for item in reference_measurements),
            tuple(item.wavelength for item in candidate_measurements),
        ),
        (
            "wavelength_units",
            tuple(item.wavelength_unit for item in reference_measurements),
            tuple(item.wavelength_unit for item in candidate_measurements),
        ),
    )
    signature_values: tuple[tuple[str, CompatibilityValue, CompatibilityValue], ...] = tuple(
        (
            field_name,
            getattr(reference_signature, field_name),
            getattr(candidate_signature, field_name),
        )
        for field_name in (
            "instrument_id",
            "optical_path_id",
            "acquisition_profile_id",
            "detector_mode",
            "response_calibration_id",
        )
    )
    same_settings = (reference_signature.correction_mode if reference_signature.correction_mode == candidate_signature.correction_mode and reference_signature.correction_mode in {"full", "exposure_only", "as_stored"} else None)
    return StructuralCompatibilityReport(
        fields=(_exact_field("correction_mode", reference_signature.correction_mode, candidate_signature.correction_mode),)
        + _axis_comparison_fields(axis_values)
        + tuple(_comparison_field(name, left, right,
            "authority" if name in {"acquisition_profile_id", "response_calibration_id"} else "exact",
            same_settings) for name, left, right in signature_values)
    )


def _measurement_values(
    signature: QuantitativeTransformSignature,
    attribute: str,
) -> CompatibilityValue:
    values = tuple(getattr(item, attribute) for item in signature.measurements)
    return tuple(item.value if hasattr(item, "value") else item for item in values)


def _transformable_difference(
    field_name: str,
    reference_value: CompatibilityValue,
    candidate_value: CompatibilityValue,
) -> CompatibilityFieldReport:
    return CompatibilityFieldReport(
        field_name=field_name,
        reference_value=reference_value,
        candidate_value=candidate_value,
        accepted=True,
        reason_code=(
            "exact_match" if reference_value == candidate_value else "explicit_per_input_transform"
        ),
    )


def compare_quantitative_transformability(
    *,
    reference_signature: QuantitativeTransformSignature,
    candidate_signature: QuantitativeTransformSignature,
    reference_axis: SpectralMeasurementAxis | None = None,
    candidate_axis: SpectralMeasurementAxis | None = None,
) -> QuantitativeTransformabilityReport:
    """Compare correction rows, optionally binding source-local IDs by recorded bands.

    Supplied axes must match their own transform row IDs. Only identical unique
    ordered band identities permit ID alignment; values and row order are unchanged.
    The report uses the candidate IDs for this explicit comparison coordinate system.
    Without axes, exact measurement-ID comparison remains mandatory.
    """
    if type(reference_signature) is not QuantitativeTransformSignature:
        raise TypeError("reference_signature must be QuantitativeTransformSignature")
    if type(candidate_signature) is not QuantitativeTransformSignature:
        raise TypeError("candidate_signature must be QuantitativeTransformSignature")

    reference_ids = reference_signature.measurement_ids
    if reference_axis is not None or candidate_axis is not None:
        if (type(reference_axis) is not SpectralMeasurementAxis or type(candidate_axis) is not SpectralMeasurementAxis
                or reference_axis.measurement_ids != reference_ids
                or candidate_axis.measurement_ids != candidate_signature.measurement_ids):
            raise ValueError("Quantitative rows must match each input's own measurement axis")
        if _ordered_band_match(reference_axis.parent_band_ids, candidate_axis.parent_band_ids):
            reference_ids = candidate_signature.measurement_ids

    exact_values: tuple[tuple[str, CompatibilityValue, CompatibilityValue], ...] = (
        (
            "measurement_ids",
            reference_ids,
            candidate_signature.measurement_ids,
        ),
        (
            "normalization_policies",
            reference_signature.normalization_policy,
            candidate_signature.normalization_policy,
        ),
        (
            "response_transform_semantics",
            reference_signature.response_transform_semantic,
            candidate_signature.response_transform_semantic,
        ),
        (
            "response_transform_ids",
            reference_signature.response_transform_id,
            candidate_signature.response_transform_id,
        ),
        (
            "response_transform_authorities",
            reference_signature.response_transform_authority,
            candidate_signature.response_transform_authority,
        ),
    )
    same_settings = ({"mif_pipeline_exposure_only_v1": "exposure_only", "mif_pipeline_as_stored_v1": "as_stored"}.get(reference_signature.normalization_policy) if reference_signature.normalization_policy == candidate_signature.normalization_policy else None)
    transformable_values = tuple(
        (
            field_name,
            _measurement_values(reference_signature, attribute),
            _measurement_values(candidate_signature, attribute),
        )
        for field_name, attribute in (
            ("intensity_representations", "intensity_representation"),
            ("effective_digital_maxima", "effective_digital_maximum"),
            ("digital_range_meaningful_bits", "digital_range_meaningful_bits"),
            ("digital_range_authorities", "digital_range_authority"),
            ("digital_range_profile_ids", "digital_range_profile_id"),
            ("offsets", "offset"),
            ("offset_authorities", "offset_authority"),
            ("offset_profile_identifiers", "offset_profile_identifier"),
            ("exposure_seconds", "exposure_seconds"),
            ("gains", "gain"),
            ("binning_areas", "binning_area"),
            (
                "response_correction_multipliers",
                "response_correction_multiplier",
            ),
        )
    )
    return QuantitativeTransformabilityReport(
        fields=tuple(
            _exact_field(field_name, reference_value, candidate_value)
            for field_name, reference_value, candidate_value in exact_values
        )
        + tuple(
            _comparison_field(field_name, reference_value, candidate_value, "transformable", same_settings)
            for field_name, reference_value, candidate_value in transformable_values
        )
    )


class BasisSourceChoice(str, Enum):
    """The three exact reviewed spectral-reference source choices."""

    PHENOIMAGER_MOTIF_PROFILE = "phenoimager_motif_profile"
    IMPORTED_REFERENCE_LIBRARY = "import_reference_library"
    CONSTRUCT_REFERENCE_LIBRARY = "construct_reference_library"
    CURATED_REFERENCE_LIBRARY = "curated_reference_library"


class MotifProfileAuthority(str, Enum):
    """Retained only for the deprecated reviewed-override record."""

    REVIEWED_OVERRIDE = "reviewed_override"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    return value


def _sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class ReviewedCompatibilityOverride:
    """Reviewed provenance for a full explicit MOTIF profile contract."""

    target_image_sha256: str
    profile_id: str
    declared_compatibility_sha256: str
    reviewer_id: str
    reviewed_at: str
    reason: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.target_image_sha256, "override target_image_sha256")
        _required_text(self.profile_id, "override profile_id")
        _sha256(
            self.declared_compatibility_sha256,
            "override declared_compatibility_sha256",
        )
        _required_text(self.reviewer_id, "override reviewer_id")
        timestamp = _required_text(self.reviewed_at, "override reviewed_at")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("override reviewed_at must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("override reviewed_at must include a timezone")
        _required_text(self.reason, "override reason")
        _sha256(self.evidence_sha256, "override evidence_sha256")


def motif_profile_declaration_sha256(
    *,
    profile_id: str,
    library_sha256: str,
    axis: SpectralMeasurementAxis,
    structural_signature: StructuralCompatibilitySignature,
    quantitative_signature: QuantitativeTransformSignature,
    normalization_convention: str,
    abundance_rescaling_factor: float,
) -> str:
    """Bind an override to every declared profile-compatibility value."""
    payload = {
        "profile_id": profile_id,
        "library_sha256": library_sha256,
        "axis": _primitive(axis),
        "structural_signature": _primitive(structural_signature),
        "quantitative_signature": _primitive(quantitative_signature),
        "normalization_convention": normalization_convention,
        "abundance_rescaling_factor": abundance_rescaling_factor,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _load_pinned_motif_spectral_library():
    from .library import load_motif_spectral_library, motif_library_resource

    resource = motif_library_resource()
    if resource.name != _PINNED_MOTIF_RESOURCE_NAME:
        raise ValueError("bundled MOTIF resource identity does not match the pinned profile")
    resource_sha256 = hashlib.sha256(resource.read_bytes()).hexdigest()
    if resource_sha256 != _PINNED_MOTIF_RESOURCE_SHA256:
        raise ValueError("bundled MOTIF resource bytes do not match the pinned profile")
    motif = load_motif_spectral_library()
    if (
        motif.identifier != _PINNED_MOTIF_RESOURCE_NAME
        or motif.sha256 != _PINNED_MOTIF_RESOURCE_SHA256
    ):
        raise ValueError("loaded MOTIF library does not match the pinned resource")
    return motif


def _registered_motif_profile_evidence_sha256(
    *,
    provider: str,
    profile_id: str,
    profile_kind: str,
    resource_name: str,
    resource_sha256: str,
    parent_band_ids: tuple[str, ...],
    component_names: tuple[str, ...],
    fluorophores: tuple[str | None, ...],
    normalization_convention: str,
    abundance_rescaling_factor: float,
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "provider": provider,
                "profile_id": profile_id,
                "profile_kind": profile_kind,
                "resource_name": resource_name,
                "resource_sha256": resource_sha256,
                "parent_band_ids": parent_band_ids,
                "component_names": component_names,
                "fluorophores": fluorophores,
                "normalization_convention": normalization_convention,
                "abundance_rescaling_factor": abundance_rescaling_factor,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RegisteredMotifSyntheticProfile:
    """Pinned Akoya-supplied synthetic profile; never acquisition authority."""

    provider: str
    profile_id: str
    profile_kind: str
    resource_name: str
    resource_sha256: str
    parent_band_ids: tuple[str, ...]
    component_names: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    normalization_convention: str
    abundance_rescaling_factor: float
    evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate the RegisteredMotifSyntheticProfile fields when the record is constructed.

        The checks cover ``provider``, ``profile_id``, ``profile_kind``, ``resource_name``,
        ``resource_sha256``, ``normalization_convention``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.provider != "Akoya Biosciences":
            raise ValueError("MOTIF synthetic-profile provider must be Akoya Biosciences")
        _required_text(self.profile_id, "MOTIF synthetic profile_id")
        _required_text(self.profile_kind, "MOTIF synthetic profile_kind")
        if self.resource_name != _PINNED_MOTIF_RESOURCE_NAME:
            raise ValueError("MOTIF synthetic profile resource name is not pinned")
        if self.resource_sha256 != _PINNED_MOTIF_RESOURCE_SHA256:
            raise ValueError("MOTIF synthetic profile resource SHA-256 is not pinned")
        if (
            type(self.parent_band_ids) is not tuple
            or not self.parent_band_ids
            or any(not isinstance(item, str) or not item for item in self.parent_band_ids)
        ):
            raise ValueError("MOTIF synthetic profile parent bands must be nonempty text")
        if (
            type(self.component_names) is not tuple
            or not self.component_names
            or any(not isinstance(item, str) or not item for item in self.component_names)
        ):
            raise ValueError("MOTIF synthetic profile components must be nonempty text")
        if type(self.fluorophores) is not tuple or len(self.fluorophores) != len(
            self.component_names
        ):
            raise ValueError("MOTIF synthetic profile fluorophore inventory differs")
        if any(
            item is not None and (not isinstance(item, str) or not item)
            for item in self.fluorophores
        ):
            raise ValueError("MOTIF synthetic profile fluorophores must be literal text or null")
        if self.normalization_convention != "motif_profile_exact_values_v1":
            raise ValueError("MOTIF synthetic profile must preserve exact values")
        if self.abundance_rescaling_factor != 1.0:
            raise ValueError("MOTIF synthetic profile abundance rescaling factor must be 1.0")
        _sha256(self.evidence_sha256, "MOTIF synthetic profile evidence_sha256")
        expected = _registered_motif_profile_evidence_sha256(
            provider=self.provider,
            profile_id=self.profile_id,
            profile_kind=self.profile_kind,
            resource_name=self.resource_name,
            resource_sha256=self.resource_sha256,
            parent_band_ids=self.parent_band_ids,
            component_names=self.component_names,
            fluorophores=self.fluorophores,
            normalization_convention=self.normalization_convention,
            abundance_rescaling_factor=self.abundance_rescaling_factor,
        )
        if self.evidence_sha256 != expected:
            raise ValueError("MOTIF synthetic profile evidence SHA-256 does not match")


def _build_registered_motif_synthetic_profile() -> RegisteredMotifSyntheticProfile:
    motif = _load_pinned_motif_spectral_library()
    values = {
        "provider": "Akoya Biosciences",
        "profile_id": "akoya-phenoimager-ht-motif-synthetic-v1",
        "profile_kind": "akoya_synthetic_motif_profile_v1",
        "resource_name": motif.identifier,
        "resource_sha256": motif.sha256,
        "parent_band_ids": motif.band_names,
        "component_names": motif.component_names,
        "fluorophores": motif.fluorophores,
        "normalization_convention": "motif_profile_exact_values_v1",
        "abundance_rescaling_factor": 1.0,
    }
    return RegisteredMotifSyntheticProfile(
        **values,
        evidence_sha256=_registered_motif_profile_evidence_sha256(**values),
    )


_REGISTERED_MOTIF_SYNTHETIC_PROFILE = _build_registered_motif_synthetic_profile()
_REGISTERED_MOTIF_SYNTHETIC_PROFILE_EVIDENCE_SHA256 = (
    _REGISTERED_MOTIF_SYNTHETIC_PROFILE.evidence_sha256
)


def registered_motif_synthetic_profile() -> RegisteredMotifSyntheticProfile:
    """Return the immutable registered profile for inspection, not caller authority."""
    profile = _REGISTERED_MOTIF_SYNTHETIC_PROFILE
    profile.__post_init__()
    if profile.evidence_sha256 != _REGISTERED_MOTIF_SYNTHETIC_PROFILE_EVIDENCE_SHA256:
        raise ValueError("registered MOTIF synthetic profile differs from pinned bytes")
    return profile


@dataclass(frozen=True, slots=True)
class SyntheticMotifProfileCompatibilityEvidence:
    """Target-bound evidence for a synthetic profile, separate from empirical reports."""

    profile: RegisteredMotifSyntheticProfile
    target_measurement_ids: tuple[str, ...]
    target_parent_band_ids: tuple[str, ...]
    target_quantitative_signature: QuantitativeTransformSignature
    target_image_sha256: str | None
    compatible: bool
    reason_codes: tuple[str, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.profile) is not RegisteredMotifSyntheticProfile:
            raise TypeError("synthetic MOTIF evidence profile has the wrong type")
        if type(self.target_measurement_ids) is not tuple or not self.target_measurement_ids:
            raise ValueError("synthetic MOTIF evidence requires target measurement IDs")
        if type(self.target_parent_band_ids) is not tuple or not self.target_parent_band_ids:
            raise ValueError("synthetic MOTIF evidence requires target parent bands")
        if any(
            not isinstance(item, str) or not item
            for item in self.target_measurement_ids + self.target_parent_band_ids
        ):
            raise ValueError("synthetic MOTIF evidence axes require nonempty literal text")
        if type(self.target_quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("synthetic MOTIF evidence requires the target quantitative signature")
        if self.target_quantitative_signature.measurement_ids != self.target_measurement_ids:
            raise ValueError("synthetic MOTIF evidence quantitative axis differs")
        if self.target_image_sha256 is not None:
            _sha256(self.target_image_sha256, "synthetic MOTIF target_image_sha256")
        if type(self.compatible) is not bool or type(self.reason_codes) is not tuple:
            raise TypeError("synthetic MOTIF evidence compatibility fields have wrong types")
        expected_reasons = () if self.compatible else ("motif_parent_band_axis_mismatch",)
        if self.reason_codes != expected_reasons:
            raise ValueError("synthetic MOTIF evidence reasons conflict with compatibility")
        object.__setattr__(self, "content_sha256", _report_digest(self, "content_sha256"))


def _synthetic_motif_compatibility_evidence(
    *,
    profile: RegisteredMotifSyntheticProfile,
    target_axis: SpectralMeasurementAxis,
    target_quantitative_signature: QuantitativeTransformSignature,
    target_image_sha256: str | None,
) -> SyntheticMotifProfileCompatibilityEvidence:
    return SyntheticMotifProfileCompatibilityEvidence(
        profile=profile,
        target_measurement_ids=target_axis.measurement_ids,
        target_parent_band_ids=target_axis.parent_band_ids,
        target_quantitative_signature=target_quantitative_signature,
        target_image_sha256=target_image_sha256,
        compatible=target_axis.parent_band_ids == profile.parent_band_ids,
        reason_codes=(
            ()
            if target_axis.parent_band_ids == profile.parent_band_ids
            else ("motif_parent_band_axis_mismatch",)
        ),
    )


@dataclass(frozen=True, slots=True)
class MotifProfileCompatibilityContract:
    """Externally authoritative MOTIF profile facts not proven by the UMX file."""

    profile_id: str
    library_sha256: str
    axis: SpectralMeasurementAxis
    structural_signature: StructuralCompatibilitySignature
    quantitative_signature: QuantitativeTransformSignature
    normalization_convention: str
    abundance_rescaling_factor: float
    authority: MotifProfileAuthority
    evidence_sha256: str
    reviewed_override: ReviewedCompatibilityOverride | None

    def __post_init__(self) -> None:
        """Validate the MotifProfileCompatibilityContract fields when the record is
        constructed.

        The checks cover ``profile_id``, ``library_sha256``, ``normalization_convention``,
        ``evidence_sha256``, ``authority``, ``axis``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _required_text(self.profile_id, "MOTIF profile_id")
        _sha256(self.library_sha256, "MOTIF library_sha256")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("MOTIF profile axis must be SpectralMeasurementAxis")
        if type(self.structural_signature) is not StructuralCompatibilitySignature:
            raise TypeError("MOTIF structural_signature must be StructuralCompatibilitySignature")
        for name in ("instrument_id", "optical_path_id", "detector_mode"):
            _required_text(getattr(self.structural_signature, name), "MOTIF " + name)
        if self.structural_signature.acquisition_profile_id is None:
            raise ValueError("MOTIF profile contract requires imaging-settings profile authority")
        if self.structural_signature.response_calibration_id is None:
            raise ValueError("MOTIF profile contract requires response calibration authority")
        if type(self.quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("MOTIF quantitative_signature must be QuantitativeTransformSignature")
        if self.quantitative_signature.measurement_ids != self.axis.measurement_ids:
            raise ValueError("MOTIF quantitative measurement axis must match exactly")
        motif = _load_pinned_motif_spectral_library()
        if self.library_sha256 != motif.sha256:
            raise ValueError("MOTIF profile contract does not bind the bundled library bytes")
        if self.axis.parent_band_ids != motif.band_names:
            raise ValueError("MOTIF profile contract does not match the bundled MOTIF band axis")
        _required_text(self.normalization_convention, "MOTIF normalization_convention")
        if self.normalization_convention != "motif_profile_exact_values_v1":
            raise ValueError(
                "MOTIF columns require the motif_profile_exact_values_v1 exact values convention"
            )
        if (
            isinstance(self.abundance_rescaling_factor, bool)
            or not isinstance(self.abundance_rescaling_factor, (int, float))
            or not 0.0 < float(self.abundance_rescaling_factor) < float("inf")
        ):
            raise ValueError("MOTIF abundance_rescaling_factor must be finite and positive")
        if float(self.abundance_rescaling_factor) != 1.0:
            raise ValueError("MOTIF abundance rescaling factor must be 1.0")
        if not isinstance(self.authority, MotifProfileAuthority):
            raise TypeError("MOTIF authority must be MotifProfileAuthority")
        _sha256(self.evidence_sha256, "MOTIF evidence_sha256")
        if self.authority is MotifProfileAuthority.REVIEWED_OVERRIDE:
            if type(self.reviewed_override) is not ReviewedCompatibilityOverride:
                raise ValueError("MOTIF reviewed override provenance is required")
            if self.reviewed_override.profile_id != self.profile_id:
                raise ValueError("MOTIF reviewed override profile_id must match")
            declared_sha256 = motif_profile_declaration_sha256(
                profile_id=self.profile_id,
                library_sha256=self.library_sha256,
                axis=self.axis,
                structural_signature=self.structural_signature,
                quantitative_signature=self.quantitative_signature,
                normalization_convention=self.normalization_convention,
                abundance_rescaling_factor=self.abundance_rescaling_factor,
            )
            if self.reviewed_override.declared_compatibility_sha256 != declared_sha256:
                raise ValueError(
                    "MOTIF reviewed override does not bind the full declared compatibility"
                )
        elif self.reviewed_override is not None:
            raise ValueError(
                "MOTIF reviewed override provenance is valid only for reviewed_override authority"
            )


def _resolved_basis_sha256(basis: ResolvedRunBasis) -> str:
    return hashlib.sha256(_canonical_json_bytes(_primitive(basis))).hexdigest()


def _resolution_evidence_sha256(
    *,
    choice: BasisSourceChoice,
    target_image_sha256: str,
    source_evidence_sha256: str,
    structural_reports: tuple[StructuralCompatibilityReport, ...],
    quantitative_reports: tuple[QuantitativeTransformabilityReport, ...],
    reviewed_override: ReviewedCompatibilityOverride | None,
    resolved_basis_sha256: str,
    synthetic_profile_compatibility: SyntheticMotifProfileCompatibilityEvidence | None = None,
) -> str:
    payload = {
        "choice": choice.value,
        "target_image_sha256": target_image_sha256,
        "source_evidence_sha256": source_evidence_sha256,
        "structural_report_sha256s": tuple(report.content_sha256 for report in structural_reports),
        "quantitative_report_sha256s": tuple(
            report.content_sha256 for report in quantitative_reports
        ),
        "reviewed_override": _primitive(reviewed_override),
        "resolved_basis_sha256": resolved_basis_sha256,
    }
    if synthetic_profile_compatibility is not None:
        payload["synthetic_profile_compatibility_sha256"] = (
            synthetic_profile_compatibility.content_sha256
        )
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class _TrustedPackagedMotifAuthority:
    token: object
    profile: RegisteredMotifSyntheticProfile
    compatibility_evidence: SyntheticMotifProfileCompatibilityEvidence
    channel_fluors: tuple[str, ...]
    run_id: str
    target_axis: SpectralMeasurementAxis
    target_quantitative_signature: QuantitativeTransformSignature
    review: ReviewRecord
    target_image_sha256: str


@dataclass(frozen=True, slots=True)
class BasisSourceResolution:
    """Immutable preflight result; a basis exists only after every gate passes."""

    choice: BasisSourceChoice
    source_choice_accepted: bool
    structural_reports: tuple[StructuralCompatibilityReport, ...]
    quantitative_reports: tuple[QuantitativeTransformabilityReport, ...]
    resolved_basis: ResolvedRunBasis | None
    required_action: str
    reason_codes: tuple[str, ...]
    target_image_sha256: str | None
    source_evidence_sha256: str | None
    reviewed_override: ReviewedCompatibilityOverride | None
    resolved_basis_sha256: str | None
    resolution_evidence_sha256: str | None
    synthetic_profile_compatibility: SyntheticMotifProfileCompatibilityEvidence | None = None
    _authority_proof: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate the BasisSourceResolution fields when the record is constructed.

        The checks cover ``required_action``, ``target_image_sha256``,
        ``source_evidence_sha256``, ``resolved_basis``, ``choice``,
        ``resolved_basis_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if not isinstance(self.choice, BasisSourceChoice):
            raise TypeError("basis choice must be BasisSourceChoice")
        if type(self.source_choice_accepted) is not bool:
            raise TypeError("source_choice_accepted must be bool")
        if type(self.structural_reports) is not tuple or any(
            type(item) is not StructuralCompatibilityReport for item in self.structural_reports
        ):
            raise TypeError("structural_reports must contain structural reports")
        if type(self.quantitative_reports) is not tuple or any(
            type(item) is not QuantitativeTransformabilityReport
            for item in self.quantitative_reports
        ):
            raise TypeError("quantitative_reports must contain quantitative reports")
        _required_text(self.required_action, "basis required_action")
        if type(self.reason_codes) is not tuple or any(
            not isinstance(item, str) or not item for item in self.reason_codes
        ):
            raise TypeError("basis reason_codes must be a tuple of text")
        if self.target_image_sha256 is not None:
            _sha256(self.target_image_sha256, "basis target_image_sha256")
        if self.source_evidence_sha256 is not None:
            _sha256(self.source_evidence_sha256, "basis source_evidence_sha256")
        if (
            self.reviewed_override is not None
            and type(self.reviewed_override) is not ReviewedCompatibilityOverride
        ):
            raise TypeError("basis reviewed_override has the wrong record type")
        all_empirical_compatible = all(item.compatible for item in self.structural_reports) and all(
            item.transformable for item in self.quantitative_reports
        )
        if self.resolved_basis is None:
            if (
                self.resolved_basis_sha256 is not None
                or self.resolution_evidence_sha256 is not None
            ):
                raise ValueError("unresolved basis cannot declare resolved-basis evidence")
            return
        if type(self.resolved_basis) is not ResolvedRunBasis:
            raise TypeError("resolved_basis has the wrong record type")
        if not self.source_choice_accepted or self.required_action != "none":
            raise ValueError(
                "resolved basis requires an accepted source choice and no remaining action"
            )
        if self.choice is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY:
            if (
                len(self.structural_reports) != 2
                or len(self.quantitative_reports) != 2
                or not all_empirical_compatible
                or self.synthetic_profile_compatibility is not None
            ):
                raise ValueError("imported basis requires exact accepted empirical reports")
        elif self.choice is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE:
            evidence = self.synthetic_profile_compatibility
            if (
                self.structural_reports
                or self.quantitative_reports
                or self.reviewed_override is not None
                or type(evidence) is not SyntheticMotifProfileCompatibilityEvidence
                or not evidence.compatible
            ):
                raise ValueError(
                    "ready MOTIF basis requires exactly one accepted synthetic-profile record"
                )
        else:
            raise ValueError("constructed basis cannot be resolved by source preflight")
        if self.target_image_sha256 is None:
            raise ValueError("resolved basis requires a target image SHA-256")
        if self.source_evidence_sha256 is None:
            raise ValueError("resolved basis requires a source evidence SHA-256")
        if self.resolved_basis_sha256 is None:
            raise ValueError("resolved basis requires a resolved basis SHA-256")
        _sha256(self.resolved_basis_sha256, "resolved basis SHA-256")
        actual_basis_sha256 = _resolved_basis_sha256(self.resolved_basis)
        if self.resolved_basis_sha256 != actual_basis_sha256:
            raise ValueError("resolved basis SHA-256 does not match")
        if self.resolution_evidence_sha256 is None:
            raise ValueError("resolved basis requires a resolution evidence SHA-256")
        _sha256(self.resolution_evidence_sha256, "resolution evidence SHA-256")
        actual_resolution_sha256 = _resolution_evidence_sha256(
            choice=self.choice,
            target_image_sha256=self.target_image_sha256,
            source_evidence_sha256=self.source_evidence_sha256,
            structural_reports=self.structural_reports,
            quantitative_reports=self.quantitative_reports,
            reviewed_override=self.reviewed_override,
            resolved_basis_sha256=self.resolved_basis_sha256,
            synthetic_profile_compatibility=self.synthetic_profile_compatibility,
        )
        if self.resolution_evidence_sha256 != actual_resolution_sha256:
            raise ValueError("resolution evidence SHA-256 does not match")
        if self.choice is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE:
            _validate_trusted_packaged_motif_authority(self)
        elif self._authority_proof is not None:
            raise ValueError("MOTIF authority proof is invalid for another source choice")

    @property
    def basis_ready(self) -> bool:
        return self.resolved_basis is not None

    @property
    def authority_path(self) -> str | None:
        if self.basis_ready and self.choice is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE:
            _validate_trusted_packaged_motif_authority(self)
            return "trusted_packaged_motif_profile"
        return None

    @property
    def authoritative(self) -> bool:
        return self.authority_path is not None

    @property
    def target_quantitative_signature(self) -> QuantitativeTransformSignature:
        """Return the recursively validated target transform for a ready MOTIF basis."""
        if self.authority_path != "trusted_packaged_motif_profile":
            raise ValueError(
                "target quantitative signature requires trusted packaged MOTIF authority"
            )
        proof = self._authority_proof
        if type(proof) is not _TrustedPackagedMotifAuthority:
            raise ValueError("trusted packaged MOTIF authority proof is unavailable")
        return proof.target_quantitative_signature


def resolve_channel_fluor_identities(
    channel_fluors: tuple[object, ...],
) -> tuple[str, ...]:
    """Retain known literal fluorophores; raw measurement bands may omit Fluor."""
    if type(channel_fluors) is not tuple or not channel_fluors:
        raise ValueError("raw component selection requires Channel.Fluor identities")
    resolved: list[str] = []
    for value in channel_fluors:
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a supplied Channel.Fluor must be nonempty text")
        if value != value.strip():
            raise ValueError("Channel.Fluor is literal and must not contain whitespace")
        if value not in resolved:
            resolved.append(value)
    return tuple(resolved)


def _incompatible_resolution(
    *,
    choice: BasisSourceChoice,
    action: str,
    reasons: tuple[str, ...],
    structural_reports: tuple[StructuralCompatibilityReport, ...] = (),
    quantitative_reports: tuple[QuantitativeTransformabilityReport, ...] = (),
    target_image_sha256: str | None = None,
    source_evidence_sha256: str | None = None,
    reviewed_override: ReviewedCompatibilityOverride | None = None,
    synthetic_profile_compatibility: SyntheticMotifProfileCompatibilityEvidence | None = None,
) -> BasisSourceResolution:
    return BasisSourceResolution(
        choice=choice,
        source_choice_accepted=True,
        structural_reports=structural_reports,
        quantitative_reports=quantitative_reports,
        resolved_basis=None,
        required_action=action,
        reason_codes=reasons,
        target_image_sha256=target_image_sha256,
        source_evidence_sha256=source_evidence_sha256,
        reviewed_override=reviewed_override,
        resolved_basis_sha256=None,
        resolution_evidence_sha256=None,
        synthetic_profile_compatibility=synthetic_profile_compatibility,
    )


def _ready_basis_resolution(
    *,
    choice: BasisSourceChoice,
    target_image_sha256: str,
    source_evidence_sha256: str,
    structural_reports: tuple[StructuralCompatibilityReport, ...],
    quantitative_reports: tuple[QuantitativeTransformabilityReport, ...],
    resolved_basis: ResolvedRunBasis,
    reviewed_override: ReviewedCompatibilityOverride | None,
    authority_proof: _TrustedPackagedMotifAuthority | None = None,
    synthetic_profile_compatibility: SyntheticMotifProfileCompatibilityEvidence | None = None,
) -> BasisSourceResolution:
    resolved_sha256 = _resolved_basis_sha256(resolved_basis)
    evidence_sha256 = _resolution_evidence_sha256(
        choice=choice,
        target_image_sha256=target_image_sha256,
        source_evidence_sha256=source_evidence_sha256,
        structural_reports=structural_reports,
        quantitative_reports=quantitative_reports,
        reviewed_override=reviewed_override,
        resolved_basis_sha256=resolved_sha256,
        synthetic_profile_compatibility=synthetic_profile_compatibility,
    )
    return BasisSourceResolution(
        choice=choice,
        source_choice_accepted=True,
        structural_reports=structural_reports,
        quantitative_reports=quantitative_reports,
        resolved_basis=resolved_basis,
        required_action="none",
        reason_codes=(),
        target_image_sha256=target_image_sha256,
        source_evidence_sha256=source_evidence_sha256,
        reviewed_override=reviewed_override,
        resolved_basis_sha256=resolved_sha256,
        resolution_evidence_sha256=evidence_sha256,
        synthetic_profile_compatibility=synthetic_profile_compatibility,
        _authority_proof=authority_proof,
    )


def _compatibility_reports(
    *,
    reference_axis: SpectralMeasurementAxis,
    reference_structural: StructuralCompatibilitySignature,
    reference_quantitative: QuantitativeTransformSignature,
    target_axis: SpectralMeasurementAxis,
    target_structural: StructuralCompatibilitySignature,
    target_quantitative: QuantitativeTransformSignature,
) -> tuple[StructuralCompatibilityReport, QuantitativeTransformabilityReport]:
    return (
        compare_structural_compatibility(
            reference_axis=reference_axis,
            candidate_axis=target_axis,
            reference_signature=reference_structural,
            candidate_signature=target_structural,
        ),
        compare_quantitative_transformability(
            reference_axis=reference_axis, candidate_axis=target_axis,
            reference_signature=reference_quantitative,
            candidate_signature=target_quantitative,
        ),
    )


def motif_fluorophore_inventory(identities: tuple[str, ...]) -> tuple[str, ...]:
    """Separate the registered profile AF labels from its stained components.

    This changes only the component-selection inventory. All measurement rows,
    recorded input identities and exact profile matrix values remain unchanged.
    """
    return tuple(value for value in identities if not is_autofluorescence_component(value))


def _motif_selected_indices(motif, identities: tuple[str, ...]) -> tuple[int, ...]:
    fluorophores = motif_fluorophore_inventory(identities)
    motif.select_fluorophores(fluorophores)
    selected_fluorophores = set(fluorophores)
    return tuple(
        index
        for index, fluorophore in enumerate(motif.fluorophores)
        if fluorophore in selected_fluorophores or index in motif.autofluorescence_components
    )


def _assemble_motif_basis(
    *,
    motif,
    selected_indices: tuple[int, ...],
    run_id: str,
    target_axis: SpectralMeasurementAxis,
    review: ReviewRecord,
    profile: RegisteredMotifSyntheticProfile,
) -> ResolvedRunBasis:
    source = BasisSourceLineage(
        source_kind="instrument_profile",
        artifact_id=profile.profile_id,
        content_sha256=motif.sha256,
        run_id=None,
    )
    columns = tuple(
        ResolvedBasisColumn(
            component_identity=motif.component_names[index],
            fluorophore_identity=(
                None if index in motif.autofluorescence_components else motif.fluorophores[index]
            ),
            component_kind=(
                "profile_autofluorescence"
                if index in motif.autofluorescence_components
                else "fluorophore"
            ),
            values=tuple(float(value) for value in motif.matrix[:, index]),
            normalization_convention=profile.normalization_convention,
            abundance_rescaling_factor=profile.abundance_rescaling_factor,
            source=source,
        )
        for index in selected_indices
    )
    return ResolvedRunBasis(
        run_id=run_id,
        axis=target_axis,
        columns=columns,
        review=review,
    )


def _validate_trusted_packaged_motif_authority(
    resolution: BasisSourceResolution,
) -> None:
    proof = resolution._authority_proof
    if (
        type(proof) is not _TrustedPackagedMotifAuthority
        or proof.token is not _TRUSTED_MOTIF_AUTHORITY_TOKEN
    ):
        raise ValueError("ready MOTIF resolution requires trusted packaged MOTIF factory authority")
    profile = proof.profile
    if type(profile) is not RegisteredMotifSyntheticProfile:
        raise ValueError("trusted packaged MOTIF authority has the wrong synthetic profile")
    if profile != registered_motif_synthetic_profile():
        raise ValueError("trusted packaged MOTIF authority has an unregistered synthetic profile")
    evidence = proof.compatibility_evidence
    expected_evidence = _synthetic_motif_compatibility_evidence(
        profile=profile,
        target_axis=proof.target_axis,
        target_quantitative_signature=proof.target_quantitative_signature,
        target_image_sha256=proof.target_image_sha256,
    )
    if evidence != expected_evidence or not evidence.compatible:
        raise ValueError("trusted packaged MOTIF synthetic compatibility evidence does not match")
    identities = resolve_channel_fluor_identities(proof.channel_fluors)
    if identities != proof.channel_fluors:
        raise ValueError("trusted packaged MOTIF authority has noncanonical identities")
    if (
        resolution.target_image_sha256 != proof.target_image_sha256
        or resolution.source_evidence_sha256 != profile.evidence_sha256
        or resolution.reviewed_override is not None
        or resolution.synthetic_profile_compatibility != evidence
    ):
        raise ValueError("trusted packaged MOTIF authority evidence does not match")
    if resolution.structural_reports or resolution.quantitative_reports:
        raise ValueError("trusted packaged MOTIF authority cannot carry empirical reports")
    motif = _load_pinned_motif_spectral_library()
    selected_indices = _motif_selected_indices(motif, identities)
    expected_basis = _assemble_motif_basis(
        motif=motif,
        selected_indices=selected_indices,
        run_id=proof.run_id,
        target_axis=proof.target_axis,
        review=proof.review,
        profile=profile,
    )
    if resolution.resolved_basis != expected_basis:
        raise ValueError(
            "trusted packaged MOTIF basis values, order, or source lineage do not match"
        )


def resolve_basis_source(
    choice: BasisSourceChoice | str,
    *,
    run_id: str,
    channel_fluors: tuple[object, ...],
    target_axis: SpectralMeasurementAxis,
    target_structural_signature: StructuralCompatibilitySignature | None,
    target_quantitative_signature: QuantitativeTransformSignature,
    review: ReviewRecord,
    target_image_sha256: str | None = None,
    imported_library_path: str | Path | None = None,
    target_autofluorescence_model: TargetAutofluorescenceModel | None = None,
    motif_profile_contract: MotifProfileCompatibilityContract | None = None,
) -> BasisSourceResolution:
    """Resolve one exact source choice without prompts, fallbacks, or inferred facts."""
    from .reference_library import load_reference_artifact

    try:
        selected_choice = BasisSourceChoice(choice)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported spectral basis source choice {choice!r}") from exc
    _required_text(run_id, "run_id")
    identities = resolve_channel_fluor_identities(channel_fluors)
    if type(target_axis) is not SpectralMeasurementAxis:
        raise TypeError("target_axis must be SpectralMeasurementAxis")
    if (
        selected_choice is not BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
        and type(target_structural_signature) is not StructuralCompatibilitySignature
    ):
        raise TypeError("target_structural_signature must be StructuralCompatibilitySignature")
    if type(target_quantitative_signature) is not QuantitativeTransformSignature:
        raise TypeError("target_quantitative_signature must be QuantitativeTransformSignature")
    if target_quantitative_signature.measurement_ids != target_axis.measurement_ids:
        raise ValueError("target quantitative measurement axis must match exactly")
    if type(review) is not ReviewRecord:
        raise TypeError("review must be ReviewRecord")
    if target_image_sha256 is not None:
        _sha256(target_image_sha256, "target_image_sha256")

    if selected_choice is BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY:
        return _incompatible_resolution(
            choice=selected_choice,
            action="construct_reference_library",
            reasons=("new_reference_library_construction_required",),
            target_image_sha256=target_image_sha256,
        )

    if selected_choice is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY:
        if imported_library_path is None:
            return _incompatible_resolution(
                choice=selected_choice,
                action="select_imported_reference_library",
                reasons=("imported_reference_library_required",),
                target_image_sha256=target_image_sha256,
            )
        artifact = load_reference_artifact(imported_library_path)
        if type(artifact) is not FluorophoreReferenceLibrary:
            raise ValueError("imported basis source must be a fluorophore reference library")
        if target_autofluorescence_model is None:
            return _incompatible_resolution(
                choice=selected_choice,
                action="provide_target_autofluorescence_model",
                reasons=("target_autofluorescence_model_required",),
                target_image_sha256=target_image_sha256,
                source_evidence_sha256=artifact.content_sha256,
            )
        if type(target_autofluorescence_model) is not TargetAutofluorescenceModel:
            raise TypeError("target_autofluorescence_model has the wrong artifact type")
        if target_image_sha256 is None:
            return _incompatible_resolution(
                choice=selected_choice,
                action="provide_target_image_identity",
                reasons=("target_image_sha256_required",),
                target_image_sha256=target_image_sha256,
                source_evidence_sha256=artifact.content_sha256,
            )
        if target_autofluorescence_model.target_image_sha256 != target_image_sha256:
            return _incompatible_resolution(
                choice=selected_choice,
                action="resolve_incompatibility",
                reasons=("target_autofluorescence_image_mismatch",),
                target_image_sha256=target_image_sha256,
                source_evidence_sha256=artifact.content_sha256,
            )
        library_axis, library_signature, _ = align_library_measurements(artifact, target_axis)
        library_structural, library_quantitative = _compatibility_reports(
            reference_axis=library_axis,
            reference_structural=artifact.structural_signature,
            reference_quantitative=library_signature,
            target_axis=target_axis,
            target_structural=target_structural_signature,
            target_quantitative=target_quantitative_signature,
        )
        af_structural, af_quantitative = _compatibility_reports(
            reference_axis=target_autofluorescence_model.axis,
            reference_structural=target_autofluorescence_model.structural_signature,
            reference_quantitative=target_autofluorescence_model.quantitative_signature,
            target_axis=target_axis,
            target_structural=target_structural_signature,
            target_quantitative=target_quantitative_signature,
        )
        structural_reports = (library_structural, af_structural)
        quantitative_reports = (library_quantitative, af_quantitative)
        if not all(item.compatible for item in structural_reports) or not all(
            item.transformable for item in quantitative_reports
        ):
            return _incompatible_resolution(
                choice=selected_choice,
                action="resolve_incompatibility",
                reasons=("imported_basis_incompatible",),
                structural_reports=structural_reports,
                quantitative_reports=quantitative_reports,
                target_image_sha256=target_image_sha256,
                source_evidence_sha256=artifact.content_sha256,
            )
        available = {item.fluorophore_identity for item in artifact.spectra}
        missing = tuple(identity for identity in identities if identity not in available)
        if missing:
            raise ValueError(
                f"Channel.Fluor identities are absent from imported library: {missing!r}"
            )
        if review.decision != "accepted":
            return _incompatible_resolution(
                choice=selected_choice,
                action="accept_resolved_basis_review",
                reasons=("resolved_basis_review_required",),
                structural_reports=structural_reports,
                quantitative_reports=quantitative_reports,
                target_image_sha256=target_image_sha256,
                source_evidence_sha256=artifact.content_sha256,
            )
        return _incompatible_resolution(
            choice=selected_choice,
            action="assemble_task7_run_basis",
            reasons=("accepted_task7_target_af_artifact_required",),
            target_image_sha256=target_image_sha256,
            source_evidence_sha256=artifact.content_sha256,
            structural_reports=structural_reports,
            quantitative_reports=quantitative_reports,
        )

    if motif_profile_contract is not None:
        raise ValueError(
            "MOTIF uses its registered synthetic profile; caller profile contracts are retired"
        )
    profile = registered_motif_synthetic_profile()
    evidence = _synthetic_motif_compatibility_evidence(
        profile=profile,
        target_axis=target_axis,
        target_quantitative_signature=target_quantitative_signature,
        target_image_sha256=target_image_sha256,
    )
    if not evidence.compatible:
        return _incompatible_resolution(
            choice=selected_choice,
            action="resolve_incompatibility",
            reasons=evidence.reason_codes,
            target_image_sha256=target_image_sha256,
            source_evidence_sha256=profile.evidence_sha256,
            synthetic_profile_compatibility=evidence,
        )
    motif = _load_pinned_motif_spectral_library()
    selected_indices = _motif_selected_indices(motif, identities)
    if review.decision != "accepted":
        return _incompatible_resolution(
            choice=selected_choice,
            action="accept_resolved_basis_review",
            reasons=("resolved_basis_review_required",),
            target_image_sha256=target_image_sha256,
            source_evidence_sha256=profile.evidence_sha256,
            synthetic_profile_compatibility=evidence,
        )
    basis = _assemble_motif_basis(
        motif=motif,
        selected_indices=selected_indices,
        run_id=run_id,
        target_axis=target_axis,
        review=review,
        profile=profile,
    )
    if target_image_sha256 is None:
        return _incompatible_resolution(
            choice=selected_choice,
            action="provide_target_image_identity",
            reasons=("target_image_sha256_required",),
            target_image_sha256=target_image_sha256,
            source_evidence_sha256=profile.evidence_sha256,
            synthetic_profile_compatibility=evidence,
        )
    return _ready_basis_resolution(
        choice=selected_choice,
        target_image_sha256=target_image_sha256,
        source_evidence_sha256=profile.evidence_sha256,
        structural_reports=(),
        quantitative_reports=(),
        resolved_basis=basis,
        reviewed_override=None,
        synthetic_profile_compatibility=evidence,
        authority_proof=_TrustedPackagedMotifAuthority(
            token=_TRUSTED_MOTIF_AUTHORITY_TOKEN,
            profile=profile,
            compatibility_evidence=evidence,
            channel_fluors=identities,
            run_id=run_id,
            target_axis=target_axis,
            target_quantitative_signature=target_quantitative_signature,
            review=review,
            target_image_sha256=target_image_sha256,
        ),
    )


__all__ = [
    "BasisSourceChoice",
    "BasisSourceResolution",
    "CompatibilityFieldReport",
    "CompatibilityReport",
    "MotifProfileAuthority",
    "MotifProfileCompatibilityContract",
    "QuantitativeTransformabilityReport",
    "RegisteredMotifSyntheticProfile",
    "ReviewedCompatibilityOverride",
    "StructuralCompatibilityReport",
    "SyntheticMotifProfileCompatibilityEvidence",
    "compare_quantitative_transformability",
    "compare_structural_compatibility",
    "dumps_compatibility_report",
    "loads_compatibility_report",
    "motif_profile_declaration_sha256",
    "registered_motif_synthetic_profile",
    "resolve_basis_source",
    "resolve_channel_fluor_identities",
]
