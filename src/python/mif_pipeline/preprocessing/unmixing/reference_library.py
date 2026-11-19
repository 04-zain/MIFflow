"""Strict deterministic persistence for spectral-reference artifacts.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import NoReturn

from mif_pipeline.io import DigitalRangeAuthority, OffsetAuthority

from .reference_models import (
    AcceptedSpectrum,
    ArtifactLineage,
    BackgroundModelEvidence,
    BackgroundSpectrum,
    FluorophoreReferenceLibrary,
    LibraryQcEvidence,
    PairwiseSpectrumQc,
    QcMetric,
    QuantitativeMeasurementTransform,
    QuantitativeTransformSignature,
    ReferenceArtifact,
    ReferenceInputEvidence,
    RejectionCount,
    ReviewRecord,
    RoiEvidence,
    RoiBackgroundEvidence,
    SpectralMeasurement,
    SpectralOpticalSetting,
    SpectralMeasurementAxis,
    StructuralCompatibilitySignature,
    TargetAutofluorescenceModel,
    VersionRecord,
    _artifact_mapping,
    _canonical_json_bytes,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================
#                               Reference library
# ==============================================================================


class ReferenceArtifactError(ValueError):
    """A persisted spectral-reference artifact is invalid or was modified."""


def _schema_fields(record_type: type) -> frozenset[str]:
    return frozenset(item.name for item in fields(record_type))


def _object(value: object, record_type: type, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReferenceArtifactError(f"{label} must be a JSON object")
    expected = _schema_fields(record_type)
    actual = frozenset(value)
    expected -= {item.name for item in fields(record_type)
                 if item.metadata.get("omit_empty") and item.name not in actual}
    if record_type is SpectralMeasurement and "optical_setting" not in actual:
        expected -= {"optical_setting"}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReferenceArtifactError(
            f"{label} schema fields differ; missing={missing}, extra={extra}"
        )
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReferenceArtifactError(f"{label} must be a JSON array")
    return value


def _validate_review_schema(value: object, label: str) -> None:
    _object(value, ReviewRecord, label)


def _validate_roi_schema(value: object, label: str) -> None:
    roi = _object(value, RoiEvidence, label)
    for index, item in enumerate(_array(roi["rejection_counts"], f"{label}.rejection_counts")):
        _object(item, RejectionCount, f"{label}.rejection_counts[{index}]")
    for index, item in enumerate(_array(roi["qc_metrics"], f"{label}.qc_metrics")):
        _object(item, QcMetric, f"{label}.qc_metrics[{index}]")


def _validate_spectrum_schema(value: object, label: str) -> None:
    spectrum = _object(value, AcceptedSpectrum, label)
    for index, item in enumerate(_array(spectrum["roi_evidence"], f"{label}.roi_evidence")):
        _validate_roi_schema(item, f"{label}.roi_evidence[{index}]")
    background = _object(
        spectrum["background_model"],
        BackgroundModelEvidence,
        f"{label}.background_model",
    )
    _object(
        background["stained_background"],
        BackgroundSpectrum,
        f"{label}.background_model.stained_background",
    )
    _object(
        background["control_background"],
        BackgroundSpectrum,
        f"{label}.background_model.control_background",
    )
    for index, value in enumerate(_array(spectrum.get("regional_backgrounds", []), f"{label}.regional_backgrounds")):
        mapping = _object(value, RoiBackgroundEvidence, f"{label}.regional_backgrounds[{index}]")
        model = _object(mapping["background_model"], BackgroundModelEvidence, "regional background model")
        for side in ("stained_background", "control_background"):
            _object(model[side], BackgroundSpectrum, "regional background spectrum")
    _validate_review_schema(spectrum["review"], f"{label}.review")


def _validate_qc_schema(value: object, label: str) -> None:
    qc = _object(value, LibraryQcEvidence, label)
    for index, item in enumerate(_array(qc["pairwise_spectra"], f"{label}.pairwise_spectra")):
        _object(item, PairwiseSpectrumQc, f"{label}.pairwise_spectra[{index}]")
    for index, item in enumerate(_array(qc["metrics"], f"{label}.metrics")):
        _object(item, QcMetric, f"{label}.metrics[{index}]")


def _validate_shared_schema(
    root: dict[str, object],
    *,
    spectra_field: str,
) -> None:
    axis = _object(root["axis"], SpectralMeasurementAxis, "axis")
    for index, item in enumerate(_array(axis["measurements"], "axis.measurements")):
        _object(item, SpectralMeasurement, f"axis.measurements[{index}]")
    _object(
        root["structural_signature"],
        StructuralCompatibilitySignature,
        "structural_signature",
    )
    quantitative = _object(
        root["quantitative_signature"],
        QuantitativeTransformSignature,
        "quantitative_signature",
    )
    for index, item in enumerate(
        _array(quantitative["measurements"], "quantitative_signature.measurements")
    ):
        _object(
            item,
            QuantitativeMeasurementTransform,
            f"quantitative_signature.measurements[{index}]",
        )
    for index, item in enumerate(_array(root["source_inputs"], "source_inputs")):
        source_input = _object(
            item,
            ReferenceInputEvidence,
            f"source_inputs[{index}]",
        )
        input_axis = _object(
            source_input["axis"],
            SpectralMeasurementAxis,
            f"source_inputs[{index}].axis",
        )
        for measurement_index, measurement in enumerate(
            _array(
                input_axis["measurements"],
                f"source_inputs[{index}].axis.measurements",
            )
        ):
            _object(
                measurement,
                SpectralMeasurement,
                f"source_inputs[{index}].axis.measurements[{measurement_index}]",
            )
        _object(
            source_input["structural_signature"],
            StructuralCompatibilitySignature,
            f"source_inputs[{index}].structural_signature",
        )
        input_quantitative = _object(
            source_input["quantitative_signature"],
            QuantitativeTransformSignature,
            f"source_inputs[{index}].quantitative_signature",
        )
        for measurement_index, measurement in enumerate(
            _array(
                input_quantitative["measurements"],
                f"source_inputs[{index}].quantitative_signature.measurements",
            )
        ):
            _object(
                measurement,
                QuantitativeMeasurementTransform,
                f"source_inputs[{index}].quantitative_signature.measurements[{measurement_index}]",
            )
    for index, item in enumerate(_array(root[spectra_field], spectra_field)):
        _validate_spectrum_schema(item, f"{spectra_field}[{index}]")
    _validate_qc_schema(root["qc"], "qc")
    for field_name in ("software_versions", "policy_versions"):
        for index, item in enumerate(_array(root[field_name], field_name)):
            _object(item, VersionRecord, f"{field_name}[{index}]")
    _validate_review_schema(root["review"], "review")
    _object(root["lineage"], ArtifactLineage, "lineage")


def _validated_root(payload: object) -> tuple[dict[str, object], type[ReferenceArtifact]]:
    if not isinstance(payload, dict):
        raise ReferenceArtifactError("reference artifact root must be a JSON object")
    schema_name = payload.get("schema_name")
    if schema_name == FluorophoreReferenceLibrary.SCHEMA_NAME:
        artifact_type: type[ReferenceArtifact] = FluorophoreReferenceLibrary
        root = _object(payload, artifact_type, "fluorophore reference library")
        spectra_field = "spectra"
    elif schema_name == TargetAutofluorescenceModel.SCHEMA_NAME:
        artifact_type = TargetAutofluorescenceModel
        root = _object(payload, artifact_type, "target autofluorescence model")
        spectra_field = "endmembers"
    else:
        raise ReferenceArtifactError(f"unsupported reference artifact schema {schema_name!r}")
    if type(root["schema_version"]) is int and root["schema_version"] == 1:
        raise ReferenceArtifactError(f"{schema_name} version 1 requires source-backed migration")
    if root["schema_version"] != artifact_type.SCHEMA_VERSION:
        raise ReferenceArtifactError(
            f"unsupported {schema_name} schema version {root['schema_version']!r}"
        )
    _validate_shared_schema(root, spectra_field=spectra_field)
    return root, artifact_type


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceArtifactError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ReferenceArtifactError(f"non-finite JSON number {value!r} is not permitted")


def _verify_content_hash(root: dict[str, object]) -> None:
    declared = root["content_sha256"]
    if not isinstance(declared, str) or _SHA256_RE.fullmatch(declared) is None:
        raise ReferenceArtifactError("content_sha256 must be a lowercase SHA-256")
    content = dict(root)
    del content["content_sha256"]
    actual = hashlib.sha256(_canonical_json_bytes(content)).hexdigest()
    if declared != actual:
        raise ReferenceArtifactError("reference artifact content SHA-256 does not match")


def _parse_measurement(value: object) -> SpectralMeasurement:
    item = value  # schema validation has already proved the mapping shape
    assert isinstance(item, dict)
    setting = item.get("optical_setting")
    if setting is not None:
        setting = SpectralOpticalSetting(**_object(setting, SpectralOpticalSetting, "optical_setting"))
    return SpectralMeasurement(
        measurement_id=item["measurement_id"],
        parent_band_id=item["parent_band_id"],
        sample_id=item["sample_id"],
        wavelength=item["wavelength"],
        wavelength_unit=item["wavelength_unit"],
        optical_setting=setting,
    )


def _parse_axis(value: object) -> SpectralMeasurementAxis:
    item = value
    assert isinstance(item, dict)
    measurements = item["measurements"]
    assert isinstance(measurements, list)
    return SpectralMeasurementAxis(
        measurements=tuple(_parse_measurement(entry) for entry in measurements)
    )


def _parse_structural(value: object) -> StructuralCompatibilitySignature:
    item = value
    assert isinstance(item, dict)
    return StructuralCompatibilitySignature(
        instrument_id=item["instrument_id"],
        optical_path_id=item["optical_path_id"],
        detector_mode=item["detector_mode"],
        acquisition_profile_id=item["acquisition_profile_id"],
        response_calibration_id=item["response_calibration_id"],
        correction_mode=item["correction_mode"],
    )


def _parse_quantitative_measurement(value: object) -> QuantitativeMeasurementTransform:
    item = value
    assert isinstance(item, dict)
    return QuantitativeMeasurementTransform(
        measurement_id=item["measurement_id"],
        effective_digital_maximum=item["effective_digital_maximum"],
        digital_range_meaningful_bits=item["digital_range_meaningful_bits"],
        digital_range_authority=(DigitalRangeAuthority(item["digital_range_authority"]) if item["digital_range_authority"] is not None else None),
        digital_range_profile_id=item["digital_range_profile_id"],
        offset=item["offset"],
        offset_authority=OffsetAuthority(item["offset_authority"]),
        offset_profile_identifier=item["offset_profile_identifier"],
        exposure_seconds=item["exposure_seconds"],
        gain=item["gain"],
        binning_area=item["binning_area"],
        response_correction_multiplier=item["response_correction_multiplier"],
        correction_mode=item["correction_mode"],
        intensity_representation=item["intensity_representation"],
    )


def _parse_quantitative(value: object) -> QuantitativeTransformSignature:
    item = value
    assert isinstance(item, dict)
    measurements = item["measurements"]
    assert isinstance(measurements, list)
    return QuantitativeTransformSignature(
        normalization_policy=item["normalization_policy"],
        response_transform_id=item["response_transform_id"],
        response_transform_semantic=item["response_transform_semantic"],
        measurements=tuple(_parse_quantitative_measurement(entry) for entry in measurements),
        response_transform_authority=item["response_transform_authority"],
    )


def _parse_source_input(value: object) -> ReferenceInputEvidence:
    item = value
    assert isinstance(item, dict)
    return ReferenceInputEvidence(
        input_id=item["input_id"],
        input_role=item["input_role"],
        fluorophore_identity=item["fluorophore_identity"],
        source_image_sha256=item["source_image_sha256"],
        axis=_parse_axis(item["axis"]),
        structural_signature=_parse_structural(item["structural_signature"]),
        quantitative_signature=_parse_quantitative(item["quantitative_signature"]),
        structurally_compatible=item["structurally_compatible"],
        quantitatively_transformable=item["quantitatively_transformable"],
        structural_compatibility_evidence_sha256=item["structural_compatibility_evidence_sha256"],
        quantitative_transform_evidence_sha256=item["quantitative_transform_evidence_sha256"],
    )


def _parse_rejection(value: object) -> RejectionCount:
    item = value
    assert isinstance(item, dict)
    return RejectionCount(reason=item["reason"], count=item["count"])


def _parse_metric(value: object) -> QcMetric:
    item = value
    assert isinstance(item, dict)
    return QcMetric(name=item["name"], value=item["value"], passed=item["passed"])


def _parse_review(value: object) -> ReviewRecord:
    item = value
    assert isinstance(item, dict)
    return ReviewRecord(
        decision=item["decision"],
        reviewer_id=item["reviewer_id"],
        reviewed_at=item["reviewed_at"],
    )


def _parse_roi(value: object) -> RoiEvidence:
    item = value
    assert isinstance(item, dict)
    rejection_counts = item["rejection_counts"]
    qc_metrics = item["qc_metrics"]
    assert isinstance(rejection_counts, list)
    assert isinstance(qc_metrics, list)
    return RoiEvidence(
        roi_id=item["roi_id"],
        roi_role=item["roi_role"],
        source_input_id=item["source_input_id"],
        source_image_sha256=item["source_image_sha256"],
        geometry_artifact_sha256=item["geometry_artifact_sha256"],
        complete_vector_estimator_version=item["complete_vector_estimator_version"],
        sampling_policy=item["sampling_policy"],
        sampling_seed=item["sampling_seed"],
        requested_pixel_count=item["requested_pixel_count"],
        total_pixel_count=item["total_pixel_count"],
        accepted_pixel_count=item["accepted_pixel_count"],
        rejected_pixel_count=item["rejected_pixel_count"],
        rejection_counts=tuple(_parse_rejection(entry) for entry in rejection_counts),
        roi_coverage_fraction=item["roi_coverage_fraction"],
        sampled_chunk_ids=tuple(item["sampled_chunk_ids"]),
        signal_measurement_id=item["signal_measurement_id"],
        dark_measurement_ids=tuple(item["dark_measurement_ids"]),
        matched_af_roi_ids=tuple(item["matched_af_roi_ids"]),
        af_subtraction_factor=item["af_subtraction_factor"],
        purified_spectrum=(
            None if item["purified_spectrum"] is None else tuple(item["purified_spectrum"])
        ),
        consensus_inclusion=item["consensus_inclusion"],
        inclusion_reason=item["inclusion_reason"],
        qc_metrics=tuple(_parse_metric(entry) for entry in qc_metrics),
        accepted=item["accepted"],
        reason_codes=tuple(item["reason_codes"]),
    )


def _parse_background_spectrum(value: object) -> BackgroundSpectrum:
    item = value
    assert isinstance(item, dict)
    result = BackgroundSpectrum(
        source_input_id=item["source_input_id"],
        source_image_sha256=item["source_image_sha256"],
        values=tuple(item["values"]),
    )
    declared = item["spectrum_sha256"]
    if not isinstance(declared, str) or _SHA256_RE.fullmatch(declared) is None:
        raise ReferenceArtifactError("background spectrum_sha256 must be a lowercase SHA-256")
    if result.spectrum_sha256 != declared:
        raise ReferenceArtifactError("background spectrum SHA-256 does not match")
    return result


def _parse_background_model(value: object) -> BackgroundModelEvidence:
    item = value
    assert isinstance(item, dict)
    return BackgroundModelEvidence(
        policy=item["policy"],
        stained_background=_parse_background_spectrum(item["stained_background"]),
        control_background=_parse_background_spectrum(item["control_background"]),
        shared_offset_equivalence_sha256=item["shared_offset_equivalence_sha256"],
        evidence_sha256=item["evidence_sha256"],
    )


def _parse_spectrum(value: object) -> AcceptedSpectrum:
    item = value
    assert isinstance(item, dict)
    roi_evidence = item["roi_evidence"]
    assert isinstance(roi_evidence, list)
    result = AcceptedSpectrum(
        component_identity=item["component_identity"],
        fluorophore_identity=item["fluorophore_identity"],
        component_kind=item["component_kind"],
        normalization_convention=item["normalization_convention"],
        abundance_rescaling_factor=item["abundance_rescaling_factor"],
        values=tuple(item["values"]),
        roi_evidence=tuple(_parse_roi(entry) for entry in roi_evidence),
        background_model=_parse_background_model(item["background_model"]),
        regional_backgrounds=tuple(RoiBackgroundEvidence(entry["roi_id"], entry["matched_control_roi_id"],
            _parse_background_model(entry["background_model"])) for entry in item.get("regional_backgrounds", [])),
        review=_parse_review(item["review"]),
    )
    declared = item["spectrum_sha256"]
    if not isinstance(declared, str) or _SHA256_RE.fullmatch(declared) is None:
        raise ReferenceArtifactError("spectrum_sha256 must be a lowercase SHA-256")
    if result.spectrum_sha256 != declared:
        raise ReferenceArtifactError("accepted spectrum SHA-256 does not match")
    return result


def _parse_pairwise(value: object) -> PairwiseSpectrumQc:
    item = value
    assert isinstance(item, dict)
    return PairwiseSpectrumQc(
        first_component_identity=item["first_component_identity"],
        second_component_identity=item["second_component_identity"],
        cosine_similarity=item["cosine_similarity"],
        spectral_angle_degrees=item["spectral_angle_degrees"],
    )


def _parse_qc(value: object) -> LibraryQcEvidence:
    item = value
    assert isinstance(item, dict)
    pairs = item["pairwise_spectra"]
    metrics = item["metrics"]
    assert isinstance(pairs, list)
    assert isinstance(metrics, list)
    return LibraryQcEvidence(
        policy_version=item["policy_version"],
        rank=item["rank"],
        condition_number=item["condition_number"],
        pairwise_spectra=tuple(_parse_pairwise(entry) for entry in pairs),
        metrics=tuple(_parse_metric(entry) for entry in metrics),
        passed=item["passed"],
    )


def _parse_version(value: object) -> VersionRecord:
    item = value
    assert isinstance(item, dict)
    return VersionRecord(name=item["name"], version=item["version"])


def _parse_lineage(value: object) -> ArtifactLineage:
    item = value
    assert isinstance(item, dict)
    return ArtifactLineage(
        predecessor_artifact_id=item["predecessor_artifact_id"],
        predecessor_content_sha256=item["predecessor_content_sha256"],
        source_artifact_sha256s=tuple(item["source_artifact_sha256s"]),
        creation_reason=item["creation_reason"],
    )


def _parse_artifact(
    root: dict[str, object],
    artifact_type: type[ReferenceArtifact],
) -> ReferenceArtifact:
    common = {
        "artifact_id": root["artifact_id"],
        "created_at": root["created_at"],
        "axis": _parse_axis(root["axis"]),
        "structural_signature": _parse_structural(root["structural_signature"]),
        "quantitative_signature": _parse_quantitative(root["quantitative_signature"]),
        "source_inputs": tuple(_parse_source_input(entry) for entry in root["source_inputs"]),
        "normalization_convention": root["normalization_convention"],
        "qc": _parse_qc(root["qc"]),
        "software_versions": tuple(_parse_version(entry) for entry in root["software_versions"]),
        "policy_versions": tuple(_parse_version(entry) for entry in root["policy_versions"]),
        "review": _parse_review(root["review"]),
        "lineage": _parse_lineage(root["lineage"]),
    }
    try:
        if artifact_type is FluorophoreReferenceLibrary:
            spectra = root["spectra"]
            assert isinstance(spectra, list)
            result: ReferenceArtifact = FluorophoreReferenceLibrary(
                **common,
                spectra=tuple(_parse_spectrum(entry) for entry in spectra),
            )
        else:
            endmembers = root["endmembers"]
            assert isinstance(endmembers, list)
            result = TargetAutofluorescenceModel(
                **common,
                run_id=root["run_id"],
                target_image_sha256=root["target_image_sha256"],
                estimation_method=root["estimation_method"],
                endmembers=tuple(_parse_spectrum(entry) for entry in endmembers),
            )
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReferenceArtifactError):
            raise
        raise ReferenceArtifactError(f"invalid reference artifact: {exc}") from exc
    if result.content_sha256 != root["content_sha256"]:
        raise ReferenceArtifactError("reference artifact content SHA-256 does not match")
    return result


def dumps_reference_artifact(artifact: ReferenceArtifact) -> str:
    """Render one artifact as deterministic strict JSON."""
    if type(artifact) not in {
        FluorophoreReferenceLibrary,
        TargetAutofluorescenceModel,
    }:
        raise TypeError("artifact has an unsupported reference artifact type")
    payload = _artifact_mapping(artifact)
    return _canonical_json_bytes(payload).decode("utf-8") + "\n"


def loads_reference_artifact(source: str | bytes) -> ReferenceArtifact:
    """Parse, schema-check, and verify one checksummed reference artifact."""
    try:
        payload = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ReferenceArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReferenceArtifactError("invalid reference artifact JSON") from exc
    root, artifact_type = _validated_root(payload)
    _verify_content_hash(root)
    try:
        return _parse_artifact(root, artifact_type)
    except ReferenceArtifactError:
        raise
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise ReferenceArtifactError(f"invalid reference artifact: {exc}") from exc


def load_reference_artifact(path: str | Path) -> ReferenceArtifact:
    """Load a checksummed artifact from disk."""
    source = Path(path)
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise ReferenceArtifactError(f"could not read reference artifact: {source}") from exc
    return loads_reference_artifact(content)


def write_reference_artifact(
    artifact: ReferenceArtifact,
    path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically publish deterministic JSON through a sibling temporary file."""
    destination = Path(path)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"reference artifact already exists: {destination}")
    content = dumps_reference_artifact(artifact)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            os.replace(temporary_path, destination)
        else:
            try:
                os.link(temporary_path, destination)
            except FileExistsError as exc:
                raise FileExistsError(f"reference artifact already exists: {destination}") from exc
            temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return destination


__all__ = [
    "ReferenceArtifactError",
    "dumps_reference_artifact",
    "load_reference_artifact",
    "loads_reference_artifact",
    "write_reference_artifact",
]
