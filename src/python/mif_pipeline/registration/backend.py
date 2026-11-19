"""Immutable Warpy registration and independent object-transfer QC records.

Warpy and JTS execute inside QuPath. This module does not parse or apply ImgLib2
``RealTransform`` JSON; it validates the durable exchange that the QuPath runtime emits for
exact project entries.

The `TransferQCRecord` record carries `schema_version`, `transfer_qc_id`, `registration_id`,
`registration_revision`, `tissue_generation_id` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.schema_registry import (
    assert_current_writer,
)

WARPY_PROVIDER = "warpy"
WARPY_VERSION = "0.5.0"
REGISTRATION_SCHEMA_VERSION = "warpy-registration/v1"
REGISTRATION_QC_SCHEMA_VERSION = "registration-qc/v1"
TRANSFER_QC_SCHEMA_VERSION = "warpy-transfer-qc/v1"
TRANSFER_ATTEMPT_SCHEMA_VERSION = "warpy-transfer-attempt/v1"


# ==============================================================================
#                                    Backend
# ==============================================================================


@dataclass(frozen=True, slots=True)
class RegistrationResultIdentitySpec:
    """Registration-owned identity schema for accepted batch results."""

    artifact_kind: str
    schema_name: str
    schema_version: int
    required_fields: tuple[str, ...]
    required_runtime_names: tuple[str, ...]


REGISTRATION_RESULT_IDENTITY_SPEC = RegistrationResultIdentitySpec(
    artifact_kind="registration_result",
    schema_name="warpy-registration",
    schema_version=1,
    required_fields=(
        "fixed_project_entry_id",
        "moving_project_entry_id",
        "fixed_image_sha256",
        "moving_image_sha256",
        "direction",
        "transform_qc_review_sha256",
        "original_tissue_pathobject_ids_sha256",
    ),
    required_runtime_names=("mif_pipeline",),
)

_QC_STATUSES = frozenset({"passed", "failed", "uncertain"})
_REVIEW_STATES = frozenset({"pending", "accepted", "rejected"})
_ACCEPTANCE_STATES = frozenset({"pending_review", "accepted", "rejected"})
_DISPOSITIONS = frozenset({"pending_review", "accepted", "rejected", "quarantined"})


def _strict_mapping(value: object, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ROIAnalysisError(f"{name} must be a string-keyed mapping")
    if set(value) != fields:
        raise ROIAnalysisError(f"{name} fields do not match the schema")
    return value


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _string(value, field_name)


def _enum(value: object, choices: frozenset[str], field_name: str) -> str:
    result = _string(value, field_name)
    if result not in choices:
        raise ROIAnalysisError(f"{field_name} must be one of {', '.join(sorted(choices))}")
    return result


def _number(
    value: object,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ROIAnalysisError(f"{field_name} must be a finite number")
    result = float(value)
    if not isfinite(result):
        raise ROIAnalysisError(f"{field_name} must be a finite number")
    if positive and result <= 0:
        raise ROIAnalysisError(f"{field_name} must be positive")
    if nonnegative and result < 0:
        raise ROIAnalysisError(f"{field_name} must be nonnegative")
    return result


def _fraction(value: object, field_name: str) -> float:
    result = _number(value, field_name)
    if not 0 <= result <= 1:
        raise ROIAnalysisError(f"{field_name} must be within [0, 1]")
    return result


def _integer(
    value: object,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ROIAnalysisError(f"{field_name} must be an integer")
    if positive and value <= 0:
        raise ROIAnalysisError(f"{field_name} must be positive")
    if nonnegative and value < 0:
        raise ROIAnalysisError(f"{field_name} must be nonnegative")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ROIAnalysisError(f"{field_name} must be a boolean")
    return value


def _sha256(value: object, field_name: str) -> str:
    result = _string(value, field_name)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ROIAnalysisError(f"{field_name} must be a lowercase SHA-256")
    return result


def _path(value: object, field_name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ROIAnalysisError(f"{field_name} must be a path")
    result = Path(value)
    if not result.is_absolute():
        raise ROIAnalysisError(f"{field_name} must be absolute")
    return result.resolve()


def _strings(value: object, field_name: str, *, require: bool = True) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError(f"{field_name} must be an array")
    result = tuple(_string(item, f"{field_name}[]") for item in value)
    if require and not result:
        raise ROIAnalysisError(f"{field_name} must not be empty")
    if len(set(result)) != len(result):
        raise ROIAnalysisError(f"{field_name} must contain unique values")
    return result


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError(f"{field_name} mapping keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, field_name) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise ROIAnalysisError(f"{field_name} must contain finite JSON-like values")


def _export_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PhysicalCalibration:
    """Level-0 physical calibration in micrometres per pixel."""

    x_um_per_px: float
    y_um_per_px: float

    def __post_init__(self) -> None:
        try:
            x = _number(self.x_um_per_px, "x_um_per_px", positive=True)
            y = _number(self.y_um_per_px, "y_um_per_px", positive=True)
        except ROIAnalysisError as exc:
            raise ROIAnalysisError(
                "physical calibration must contain positive finite X/Y values"
            ) from exc
        object.__setattr__(self, "x_um_per_px", x)
        object.__setattr__(self, "y_um_per_px", y)

    def to_mapping(self) -> dict[str, float]:
        return {
            "x_um_per_px": self.x_um_per_px,
            "y_um_per_px": self.y_um_per_px,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "PhysicalCalibration":
        mapping = _strict_mapping(value, {"x_um_per_px", "y_um_per_px"}, "physical calibration")
        return cls(mapping["x_um_per_px"], mapping["y_um_per_px"])


@dataclass(frozen=True, slots=True)
class CoordinateTransform:
    """An optional coordinate transform explicitly declared by its authority."""

    matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    authority: str
    purpose: str
    source_frame: str
    target_frame: str
    direction: str
    units: str

    def __post_init__(self) -> None:
        """Validate the CoordinateTransform fields when the record is constructed.

        The checks cover ``authority``, ``purpose``, ``source_frame``, ``target_frame``,
        ``direction``, ``units``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (
            not isinstance(self.matrix, tuple)
            or len(self.matrix) != 3
            or any(not isinstance(row, tuple) or len(row) != 3 for row in self.matrix)
        ):
            raise ROIAnalysisError("coordinate transform must be a finite 3-by-3 matrix")
        try:
            normalized = tuple(
                tuple(_number(item, "coordinate transform matrix") for item in row)
                for row in self.matrix
            )
        except ROIAnalysisError as exc:
            raise ROIAnalysisError("coordinate transform must be a finite 3-by-3 matrix") from exc
        if normalized[2] != (0.0, 0.0, 1.0):
            raise ROIAnalysisError("coordinate transform final row must be (0, 0, 1)")
        determinant = normalized[0][0] * normalized[1][1] - normalized[0][1] * normalized[1][0]
        if determinant == 0:
            raise ROIAnalysisError("coordinate transform must be invertible")
        object.__setattr__(self, "matrix", normalized)
        object.__setattr__(
            self, "authority", _string(self.authority, "coordinate transform authority")
        )
        object.__setattr__(self, "purpose", _string(self.purpose, "coordinate transform purpose"))
        object.__setattr__(
            self, "source_frame", _string(self.source_frame, "coordinate transform source_frame")
        )
        object.__setattr__(
            self, "target_frame", _string(self.target_frame, "coordinate transform target_frame")
        )
        object.__setattr__(
            self,
            "direction",
            _enum(
                self.direction,
                frozenset({"source_to_target", "target_to_source"}),
                "coordinate transform direction",
            ),
        )
        object.__setattr__(
            self,
            "units",
            _enum(
                self.units,
                frozenset({"pixel", "micrometre"}),
                "coordinate transform units",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "matrix": [list(row) for row in self.matrix],
            "authority": self.authority,
            "purpose": self.purpose,
            "source_frame": self.source_frame,
            "target_frame": self.target_frame,
            "direction": self.direction,
            "units": self.units,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "CoordinateTransform":
        mapping = _strict_mapping(
            value,
            {
                "matrix",
                "authority",
                "purpose",
                "source_frame",
                "target_frame",
                "direction",
                "units",
            },
            "coordinate transform",
        )
        matrix = mapping["matrix"]
        if isinstance(matrix, (str, bytes)) or not isinstance(matrix, Sequence):
            raise ROIAnalysisError("coordinate transform must be a finite 3-by-3 matrix")
        try:
            rows = tuple(tuple(row) for row in matrix)
        except TypeError as exc:
            raise ROIAnalysisError("coordinate transform must be a finite 3-by-3 matrix") from exc
        return cls(
            rows,
            mapping["authority"],
            mapping["purpose"],
            mapping["source_frame"],
            mapping["target_frame"],
            mapping["direction"],
            mapping["units"],
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LandmarkEvidence:
    available: bool
    count: int | None
    rms_residual_um: float | None
    max_residual_um: float | None
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        available = _boolean(self.available, "landmark available")
        if available:
            count = _integer(self.count, "landmark count", positive=True)
            rms = _number(self.rms_residual_um, "landmark RMS residual", nonnegative=True)
            maximum = _number(self.max_residual_um, "landmark maximum residual", nonnegative=True)
            if maximum < rms:
                raise ROIAnalysisError("landmark maximum residual cannot be below RMS residual")
            if self.unavailable_reason is not None:
                raise ROIAnalysisError(
                    "available landmark evidence cannot carry an unavailable reason"
                )
            object.__setattr__(self, "count", count)
            object.__setattr__(self, "rms_residual_um", rms)
            object.__setattr__(self, "max_residual_um", maximum)
        else:
            if any(
                item is not None
                for item in (self.count, self.rms_residual_um, self.max_residual_um)
            ):
                raise ROIAnalysisError(
                    "unavailable landmark evidence cannot contain fabricated metrics"
                )
            object.__setattr__(
                self,
                "unavailable_reason",
                _string(self.unavailable_reason, "landmark unavailable reason"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "available": self.available,
            "count": self.count,
            "rms_residual_um": self.rms_residual_um,
            "max_residual_um": self.max_residual_um,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "LandmarkEvidence":
        mapping = _strict_mapping(
            value,
            {
                "available",
                "count",
                "rms_residual_um",
                "max_residual_um",
                "unavailable_reason",
            },
            "landmark evidence",
        )
        return cls(**mapping)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DeformationEvidence:
    sample_count: int | None
    jacobian_min: float | None
    jacobian_median: float | None
    jacobian_max: float | None
    negative_jacobian_count: int | None
    available: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        available = _boolean(self.available, "deformation available")
        object.__setattr__(self, "available", available)
        if not available:
            if any(
                item is not None
                for item in (
                    self.sample_count,
                    self.jacobian_min,
                    self.jacobian_median,
                    self.jacobian_max,
                    self.negative_jacobian_count,
                )
            ):
                raise ROIAnalysisError(
                    "unavailable deformation evidence cannot contain fabricated metrics"
                )
            object.__setattr__(
                self,
                "unavailable_reason",
                _string(self.unavailable_reason, "deformation unavailable reason"),
            )
            return
        object.__setattr__(self, "unavailable_reason", None)
        object.__setattr__(
            self,
            "sample_count",
            _integer(self.sample_count, "deformation sample_count", positive=True),
        )
        minimum = _number(self.jacobian_min, "jacobian_min")
        median = _number(self.jacobian_median, "jacobian_median")
        maximum = _number(self.jacobian_max, "jacobian_max")
        if not minimum <= median <= maximum:
            raise ROIAnalysisError("Jacobian summary must be ordered min <= median <= max")
        negative = _integer(
            self.negative_jacobian_count,
            "negative_jacobian_count",
            nonnegative=True,
        )
        if negative > self.sample_count:
            raise ROIAnalysisError("negative Jacobian count cannot exceed sample count")
        object.__setattr__(self, "jacobian_min", minimum)
        object.__setattr__(self, "jacobian_median", median)
        object.__setattr__(self, "jacobian_max", maximum)
        object.__setattr__(self, "negative_jacobian_count", negative)

    def to_mapping(self) -> dict[str, object]:
        return {
            "available": self.available,
            "sample_count": self.sample_count,
            "jacobian_min": self.jacobian_min,
            "jacobian_median": self.jacobian_median,
            "jacobian_max": self.jacobian_max,
            "negative_jacobian_count": self.negative_jacobian_count,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "DeformationEvidence":
        """

        Reads the current sampled-deformation fields and checks their numerical summaries.

        Args:
            value: Availability flag, sample count, ordered Jacobian summaries,
                nonpositive-Jacobian count and any unavailable-data explanation.

        Returns:
            DeformationEvidence with validated counts and signed area-change summaries.
        """

        fields = {"sample_count", "jacobian_min", "jacobian_median", "jacobian_max",
            "negative_jacobian_count", "available", "unavailable_reason"}
        mapping = _strict_mapping(value, fields, "deformation evidence")
        return cls(**mapping)



@dataclass(frozen=True, slots=True)
class RegistrationQCEvidence:
    landmarks: LandmarkEvidence
    deformation: DeformationEvidence
    coverage_fraction: float | None
    out_of_bounds_fraction: float | None
    bounds_finite: bool | None
    available: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.landmarks, LandmarkEvidence):
            raise ROIAnalysisError("registration QC landmarks are invalid")
        if not isinstance(self.deformation, DeformationEvidence):
            raise ROIAnalysisError("registration QC deformation evidence is invalid")
        available = _boolean(self.available, "registration QC available")
        object.__setattr__(self, "available", available)
        if not available:
            if (
                any(
                    item is not None
                    for item in (
                        self.coverage_fraction,
                        self.out_of_bounds_fraction,
                        self.bounds_finite,
                    )
                )
                or self.deformation.available
            ):
                raise ROIAnalysisError(
                    "unavailable registration QC cannot contain fabricated sampling metrics"
                )
            object.__setattr__(
                self,
                "unavailable_reason",
                _string(self.unavailable_reason, "registration QC unavailable reason"),
            )
            return
        object.__setattr__(self, "unavailable_reason", None)
        object.__setattr__(
            self, "coverage_fraction", _fraction(self.coverage_fraction, "coverage_fraction")
        )
        object.__setattr__(
            self,
            "out_of_bounds_fraction",
            _fraction(self.out_of_bounds_fraction, "out_of_bounds_fraction"),
        )
        object.__setattr__(self, "bounds_finite", _boolean(self.bounds_finite, "bounds_finite"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "landmarks": self.landmarks.to_mapping(),
            "deformation": self.deformation.to_mapping(),
            "coverage_fraction": self.coverage_fraction,
            "out_of_bounds_fraction": self.out_of_bounds_fraction,
            "bounds_finite": self.bounds_finite,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationQCEvidence":
        """

        Reads the current coverage, landmark and deformation measurements for one alignment.

        We require the explicit availability fields, then validate the nested
        landmark and Jacobian measurements through their corresponding constructors.
        Coverage and image-bound fractions are checked by this record's constructor.

        Args:
            value: Complete current registration-quality dictionary.

        Returns:
            RegistrationQCEvidence containing the checked numerical measurements.
        """

        assert_current_writer("registration-qc", "1-current")
        fields = {"available", "unavailable_reason", "landmarks", "deformation",
            "coverage_fraction", "out_of_bounds_fraction", "bounds_finite"}
        mapping = _strict_mapping(value, fields, "registration quality measurements")
        return cls(
            landmarks=LandmarkEvidence.from_mapping(mapping["landmarks"]),
            deformation=DeformationEvidence.from_mapping(mapping["deformation"]),
            coverage_fraction=mapping["coverage_fraction"],
            out_of_bounds_fraction=mapping["out_of_bounds_fraction"],
            bounds_finite=mapping["bounds_finite"], available=mapping["available"],
            unavailable_reason=mapping["unavailable_reason"],
        )


@dataclass(frozen=True, slots=True)
class RegistrationQCPolicy:
    min_coverage_fraction: float = 0.8
    max_out_of_bounds_fraction: float = 0.1
    min_jacobian: float = 0.1
    max_jacobian: float = 10.0
    require_landmarks: bool = False
    max_landmark_rms_residual_um: float | None = None

    def __post_init__(self) -> None:
        minimum_coverage = _fraction(self.min_coverage_fraction, "min_coverage_fraction")
        maximum_oob = _fraction(self.max_out_of_bounds_fraction, "max_out_of_bounds_fraction")
        minimum_jacobian = _number(self.min_jacobian, "min_jacobian", positive=True)
        maximum_jacobian = _number(self.max_jacobian, "max_jacobian", positive=True)
        if minimum_jacobian >= maximum_jacobian:
            raise ROIAnalysisError("min_jacobian must be below max_jacobian")
        require_landmarks = _boolean(self.require_landmarks, "require_landmarks")
        residual = self.max_landmark_rms_residual_um
        if residual is not None:
            residual = _number(residual, "max_landmark_rms_residual_um", positive=True)
        if require_landmarks and residual is None:
            raise ROIAnalysisError("required landmark evidence needs a residual threshold")
        object.__setattr__(self, "min_coverage_fraction", minimum_coverage)
        object.__setattr__(self, "max_out_of_bounds_fraction", maximum_oob)
        object.__setattr__(self, "min_jacobian", minimum_jacobian)
        object.__setattr__(self, "max_jacobian", maximum_jacobian)
        object.__setattr__(self, "require_landmarks", require_landmarks)
        object.__setattr__(self, "max_landmark_rms_residual_um", residual)

    def evaluate(
        self, evidence: Mapping[str, object] | RegistrationQCEvidence,
        *, check_coverage: bool = True,
    ) -> tuple[str, tuple[str, ...]]:
        """Require coverage for accepted tissue; image-frame margins are informational."""
        item = (
            evidence
            if isinstance(evidence, RegistrationQCEvidence)
            else RegistrationQCEvidence.from_mapping(evidence)
        )
        if not item.available:
            return "uncertain", ("registration_sampling_unavailable",)
        failures: list[str] = []
        uncertain: list[str] = []
        if not item.bounds_finite:
            failures.append("bounds_nonfinite")
        if check_coverage and item.coverage_fraction < self.min_coverage_fraction:
            failures.append("coverage_insufficient")
        if check_coverage and item.out_of_bounds_fraction > self.max_out_of_bounds_fraction:
            failures.append("out_of_bounds_exceeded")
        if (
            item.deformation.negative_jacobian_count > 0
            or item.deformation.jacobian_min < self.min_jacobian
            or item.deformation.jacobian_max > self.max_jacobian
        ):
            failures.append("jacobian_out_of_policy")
        if self.require_landmarks and not item.landmarks.available:
            uncertain.append("landmark_evidence_unavailable")
        elif (
            item.landmarks.available
            and self.max_landmark_rms_residual_um is not None
            and item.landmarks.rms_residual_um is not None
            and item.landmarks.rms_residual_um > self.max_landmark_rms_residual_um
        ):
            failures.append("landmark_residual_exceeded")
        if failures:
            return "failed", tuple(failures)
        if uncertain:
            return "uncertain", tuple(uncertain)
        return "passed", ()

    def to_mapping(self) -> dict[str, object]:
        return {
            "min_coverage_fraction": self.min_coverage_fraction,
            "max_out_of_bounds_fraction": self.max_out_of_bounds_fraction,
            "min_jacobian": self.min_jacobian,
            "max_jacobian": self.max_jacobian,
            "require_landmarks": self.require_landmarks,
            "max_landmark_rms_residual_um": self.max_landmark_rms_residual_um,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationQCPolicy":
        mapping = _strict_mapping(
            value,
            {
                "min_coverage_fraction",
                "max_out_of_bounds_fraction",
                "min_jacobian",
                "max_jacobian",
                "require_landmarks",
                "max_landmark_rms_residual_um",
            },
            "registration QC policy",
        )
        return cls(**mapping)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SectionRegistrationQCRecord:
    section_id: str
    tissue_roi_id: str
    tissue_path_object_id: str
    pipeline_stage: str
    generation_status: str
    review_status: str
    path_class: str | None
    evidence: RegistrationQCEvidence
    computed_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_id", _string(self.section_id, "section_id"))
        object.__setattr__(self, "tissue_roi_id", _string(self.tissue_roi_id, "tissue_roi_id"))
        object.__setattr__(
            self,
            "tissue_path_object_id",
            _string(self.tissue_path_object_id, "tissue_path_object_id"),
        )
        if self.pipeline_stage != "tissue_detection":
            raise ROIAnalysisError("registration TissueSection pipeline_stage is invalid")
        if self.generation_status != "accepted" or self.review_status != "accepted":
            raise ROIAnalysisError("registration TissueSection acceptance metadata is invalid")
        if self.path_class is not None:
            object.__setattr__(self, "path_class", _string(self.path_class, "path_class"))
        if not isinstance(self.evidence, RegistrationQCEvidence):
            raise ROIAnalysisError("section registration QC evidence is invalid")
        object.__setattr__(
            self,
            "computed_status",
            _enum(self.computed_status, _QC_STATUSES, "section computed_status"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "tissue_roi_id": self.tissue_roi_id,
            "tissue_path_object_id": self.tissue_path_object_id,
            "pipeline_stage": self.pipeline_stage,
            "generation_status": self.generation_status,
            "review_status": self.review_status,
            "path_class": self.path_class,
            "evidence": self.evidence.to_mapping(),
            "computed_status": self.computed_status,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "SectionRegistrationQCRecord":
        mapping = _strict_mapping(
            value,
            {
                "section_id",
                "tissue_roi_id",
                "tissue_path_object_id",
                "evidence",
                "pipeline_stage",
                "generation_status",
                "review_status",
                "path_class",
                "computed_status",
            },
            "section registration QC record",
        )
        return cls(
            section_id=mapping["section_id"],
            tissue_roi_id=mapping["tissue_roi_id"],
            tissue_path_object_id=mapping["tissue_path_object_id"],
            pipeline_stage=mapping["pipeline_stage"],
            generation_status=mapping["generation_status"],
            review_status=mapping["review_status"],
            path_class=mapping["path_class"],
            evidence=RegistrationQCEvidence.from_mapping(mapping["evidence"]),
            computed_status=mapping["computed_status"],
        )


@dataclass(frozen=True, slots=True)
class RegistrationSamplingContext:
    """Exact fixed-frame grid used for Warpy coverage/deformation evidence."""

    target_frame: str
    sample_spacing_px: float
    grid_origin_offset_px: tuple[float, float]
    z_position: float

    def __post_init__(self) -> None:
        if self.target_frame != "fixed_level0_pixels":
            raise ROIAnalysisError("registration sampling target frame is invalid")
        spacing = _number(self.sample_spacing_px, "sample_spacing_px", positive=True)
        offsets = tuple(self.grid_origin_offset_px)
        if len(offsets) != 2:
            raise ROIAnalysisError("registration sampling grid origin requires XY")
        parsed_offsets = tuple(_number(item, "grid_origin_offset_px") for item in offsets)
        if parsed_offsets != (spacing / 2.0, spacing / 2.0):
            raise ROIAnalysisError("registration sampling grid must use half-spacing origin")
        z = _number(self.z_position, "z_position")
        if z != 0.0:
            raise ROIAnalysisError("Warpy registration sampling requires z=0")
        object.__setattr__(self, "sample_spacing_px", spacing)
        object.__setattr__(self, "grid_origin_offset_px", parsed_offsets)
        object.__setattr__(self, "z_position", z)

    def to_mapping(self) -> dict[str, object]:
        return {
            "target_frame": self.target_frame,
            "sample_spacing_px": self.sample_spacing_px,
            "grid_origin_offset_px": list(self.grid_origin_offset_px),
            "z_position": self.z_position,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationSamplingContext":
        mapping = _strict_mapping(
            value,
            {
                "target_frame",
                "sample_spacing_px",
                "grid_origin_offset_px",
                "z_position",
            },
            "registration sampling context",
        )
        offsets = mapping["grid_origin_offset_px"]
        if isinstance(offsets, (str, bytes)) or not isinstance(offsets, Sequence):
            raise ROIAnalysisError("registration sampling grid origin must be an array")
        return cls(
            target_frame=mapping["target_frame"],
            sample_spacing_px=mapping["sample_spacing_px"],
            grid_origin_offset_px=tuple(offsets),
            z_position=mapping["z_position"],
        )


@dataclass(frozen=True, slots=True)
class RegistrationQCRecord:
    schema_version: str
    qc_id: str
    registration_id: str
    tissue_generation_id: str
    sampling_context: RegistrationSamplingContext
    policy: RegistrationQCPolicy
    global_evidence: RegistrationQCEvidence
    section_records: tuple[SectionRegistrationQCRecord, ...]
    computed_status: str
    review_state: str
    reviewed_by: str | None

    def __post_init__(self) -> None:
        """Validate the RegistrationQCRecord fields when the record is constructed.

        The checks cover ``schema_version``, ``section_records``, ``computed_status``,
        ``review_state``, ``reviewed_by``, ``global_evidence``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        if self.schema_version != REGISTRATION_QC_SCHEMA_VERSION:
            raise ROIAnalysisError("registration QC schema version is unsupported")
        object.__setattr__(self, "qc_id", _string(self.qc_id, "registration qc_id"))
        object.__setattr__(
            self, "registration_id", _string(self.registration_id, "registration_id")
        )
        object.__setattr__(
            self, "tissue_generation_id", _string(self.tissue_generation_id, "tissue_generation_id")
        )
        if not isinstance(self.policy, RegistrationQCPolicy):
            raise ROIAnalysisError("registration QC policy is invalid")
        if not isinstance(self.sampling_context, RegistrationSamplingContext):
            raise ROIAnalysisError("registration sampling context is invalid")
        if not isinstance(self.global_evidence, RegistrationQCEvidence):
            raise ROIAnalysisError("global registration QC evidence is invalid")
        records = tuple(self.section_records)
        if not records or any(
            not isinstance(item, SectionRegistrationQCRecord) for item in records
        ):
            raise ROIAnalysisError("registration QC requires section records")
        if len({item.section_id for item in records}) != len(records):
            raise ROIAnalysisError("registration QC section IDs must be unique")
        if len({item.tissue_roi_id for item in records}) != len(records):
            raise ROIAnalysisError("registration QC tissue ROI IDs must be unique")
        if len({item.tissue_path_object_id for item in records}) != len(records):
            raise ROIAnalysisError("registration QC TissueSection PathObject IDs must be unique")
        status = _enum(self.computed_status, _QC_STATUSES, "registration computed_status")
        review = _enum(self.review_state, _REVIEW_STATES, "registration review_state")
        reviewer = _optional_string(self.reviewed_by, "registration reviewed_by")
        if review == "pending" and reviewer is not None:
            raise ROIAnalysisError("pending registration review cannot name a reviewer")
        if review != "pending" and reviewer is None:
            raise ROIAnalysisError("resolved registration review requires reviewed_by")
        global_status, _ = self.policy.evaluate(self.global_evidence, check_coverage=False)
        for item in records:
            section_status, _ = self.policy.evaluate(item.evidence)
            if item.computed_status != section_status:
                raise ROIAnalysisError(
                    "registration section computed status conflicts with QC policy"
                )
        all_statuses = {global_status, *(item.computed_status for item in records)}
        expected_status = (
            "failed"
            if "failed" in all_statuses
            else ("uncertain" if "uncertain" in all_statuses else "passed")
        )
        if status != expected_status:
            raise ROIAnalysisError("registration computed status conflicts with QC policy")
        object.__setattr__(self, "section_records", records)
        object.__setattr__(self, "computed_status", status)
        object.__setattr__(self, "review_state", review)
        object.__setattr__(self, "reviewed_by", reviewer)

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("registration-qc", "1-current")
        return {
            "schema_version": self.schema_version,
            "qc_id": self.qc_id,
            "registration_id": self.registration_id,
            "tissue_generation_id": self.tissue_generation_id,
            "sampling_context": self.sampling_context.to_mapping(),
            "policy": self.policy.to_mapping(),
            "global_evidence": self.global_evidence.to_mapping(),
            "section_records": [item.to_mapping() for item in self.section_records],
            "computed_status": self.computed_status,
            "review_state": self.review_state,
            "reviewed_by": self.reviewed_by,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationQCRecord":
        """Decode the serialized fields into a validated RegistrationQCRecord record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _strict_mapping(
            value,
            {
                "schema_version",
                "qc_id",
                "registration_id",
                "tissue_generation_id",
                "sampling_context",
                "policy",
                "global_evidence",
                "section_records",
                "computed_status",
                "review_state",
                "reviewed_by",
            },
            "registration QC record",
        )
        rows = mapping["section_records"]
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ROIAnalysisError("registration QC section_records must be an array")
        return cls(
            schema_version=mapping["schema_version"],
            qc_id=mapping["qc_id"],
            registration_id=mapping["registration_id"],
            tissue_generation_id=mapping["tissue_generation_id"],
            sampling_context=RegistrationSamplingContext.from_mapping(mapping["sampling_context"]),
            policy=RegistrationQCPolicy.from_mapping(mapping["policy"]),
            global_evidence=RegistrationQCEvidence.from_mapping(mapping["global_evidence"]),
            section_records=tuple(SectionRegistrationQCRecord.from_mapping(item) for item in rows),
            computed_status=mapping["computed_status"],
            review_state=mapping["review_state"],
            reviewed_by=mapping["reviewed_by"],
        )


@dataclass(frozen=True, slots=True)
class RegistrationRecord:
    schema_version: str
    registration_id: str
    provider: str
    provider_version: str
    moving_project_entry_id: str
    fixed_project_entry_id: str
    moving_image_id: str
    fixed_image_id: str
    moving_image_sha256: str
    fixed_image_sha256: str
    moving_image_path: Path
    fixed_image_path: Path
    direction: str
    moving_physical_calibration: PhysicalCalibration
    fixed_physical_calibration: PhysicalCalibration
    coordinate_pre_transform: CoordinateTransform | None
    coordinate_post_transform: CoordinateTransform | None
    transform_file: Path
    transform_file_sha256: str
    runtime_versions: Mapping[str, Any] = field(default_factory=dict)
    moving_modality: str = "other"
    moving_image_metadata: Mapping[str, Any] = field(default_factory=dict)
    tissue_generation_id: str = ""
    tissue_section_ids: tuple[str, ...] = ()
    tissue_roi_ids: tuple[str, ...] = ()
    tissue_path_object_ids: tuple[str, ...] = ()
    revision: int = 1
    supersedes_registration_id: str | None = None
    predecessor_record_sha256: str | None = None
    acceptance_state: str = "pending_review"
    registration_qc: RegistrationQCRecord | None = None

    def __post_init__(self) -> None:
        """Validate the RegistrationRecord fields when the record is constructed.

        The checks cover ``predecessor_record_sha256``, ``schema_version``,
        ``moving_project_entry_id``, ``fixed_project_entry_id``, ``moving_image_sha256``,
        ``fixed_image_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if self.schema_version != REGISTRATION_SCHEMA_VERSION:
            raise ROIAnalysisError("Warpy registration schema version is unsupported")
        object.__setattr__(
            self, "registration_id", _string(self.registration_id, "registration_id")
        )
        if self.provider != WARPY_PROVIDER or self.provider_version != WARPY_VERSION:
            raise ROIAnalysisError("registration provider must be Warpy 0.5.0")
        for name in (
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "moving_modality",
            "tissue_generation_id",
        ):
            object.__setattr__(self, name, _string(getattr(self, name), name))
        if self.moving_project_entry_id == self.fixed_project_entry_id:
            raise ROIAnalysisError("moving and fixed project entries must be distinct")
        object.__setattr__(
            self, "moving_image_sha256", _sha256(self.moving_image_sha256, "moving_image_sha256")
        )
        object.__setattr__(
            self, "fixed_image_sha256", _sha256(self.fixed_image_sha256, "fixed_image_sha256")
        )
        object.__setattr__(
            self, "moving_image_path", _path(self.moving_image_path, "moving_image_path")
        )
        object.__setattr__(
            self, "fixed_image_path", _path(self.fixed_image_path, "fixed_image_path")
        )
        if self.moving_image_sha256 == self.fixed_image_sha256:
            raise ROIAnalysisError("moving and fixed image scopes must be distinct")
        if self.direction != "moving_to_fixed":
            raise ROIAnalysisError("registration direction must be moving_to_fixed")
        if not isinstance(self.moving_physical_calibration, PhysicalCalibration) or not isinstance(
            self.fixed_physical_calibration, PhysicalCalibration
        ):
            raise ROIAnalysisError("registration requires both physical calibrations")
        if self.coordinate_pre_transform is not None and not isinstance(
            self.coordinate_pre_transform, CoordinateTransform
        ):
            raise ROIAnalysisError("coordinate_pre_transform is invalid")
        if self.coordinate_post_transform is not None and not isinstance(
            self.coordinate_post_transform, CoordinateTransform
        ):
            raise ROIAnalysisError("coordinate_post_transform is invalid")
        object.__setattr__(self, "transform_file", _path(self.transform_file, "transform_file"))
        object.__setattr__(
            self,
            "transform_file_sha256",
            _sha256(self.transform_file_sha256, "transform_file_sha256"),
        )
        runtime_versions = _freeze_json(self.runtime_versions, "runtime_versions")
        if (
            not isinstance(runtime_versions, Mapping)
            or not {"qupath", "warpy"}.issubset(runtime_versions)
            or not isinstance(runtime_versions["qupath"], str)
            or not runtime_versions["qupath"].startswith("0.7.")
            or runtime_versions["warpy"] != WARPY_VERSION
        ):
            raise ROIAnalysisError("runtime_versions must identify QuPath 0.7.x and Warpy 0.5.0")
        object.__setattr__(self, "runtime_versions", runtime_versions)
        object.__setattr__(
            self,
            "moving_image_metadata",
            _freeze_json(self.moving_image_metadata, "moving_image_metadata"),
        )
        sections = _strings(self.tissue_section_ids, "tissue_section_ids")
        roi_ids = _strings(self.tissue_roi_ids, "tissue_roi_ids")
        path_object_ids = _strings(self.tissue_path_object_ids, "tissue_path_object_ids")
        if not len(sections) == len(roi_ids) == len(path_object_ids):
            raise ROIAnalysisError("registration tissue identity arrays must have equal length")
        if len(set(roi_ids)) != len(roi_ids) or len(set(path_object_ids)) != len(path_object_ids):
            raise ROIAnalysisError("registration tissue identities must be unique")
        object.__setattr__(self, "tissue_section_ids", sections)
        object.__setattr__(self, "tissue_roi_ids", roi_ids)
        object.__setattr__(self, "tissue_path_object_ids", path_object_ids)
        revision = _integer(self.revision, "registration revision", positive=True)
        supersedes = _optional_string(self.supersedes_registration_id, "supersedes_registration_id")
        predecessor_digest = self.predecessor_record_sha256
        if revision == 1 and (supersedes is not None or predecessor_digest is not None):
            raise ROIAnalysisError("initial registration revision cannot name a predecessor")
        if revision > 1 and (supersedes is None or predecessor_digest is None):
            raise ROIAnalysisError("registration revision lineage requires predecessor identity")
        if predecessor_digest is not None:
            predecessor_digest = _sha256(predecessor_digest, "predecessor_record_sha256")
        acceptance = _enum(
            self.acceptance_state, _ACCEPTANCE_STATES, "registration acceptance_state"
        )
        if not isinstance(self.registration_qc, RegistrationQCRecord):
            raise ROIAnalysisError("registration record requires registration QC")
        if self.registration_qc.registration_id != self.registration_id:
            raise ROIAnalysisError("registration QC registration_id conflicts with its record")
        if self.registration_qc.tissue_generation_id != self.tissue_generation_id:
            raise ROIAnalysisError(
                "registration QC tissue generation scope conflicts with its record"
            )
        qc_sections = tuple(item.section_id for item in self.registration_qc.section_records)
        qc_roi_ids = tuple(item.tissue_roi_id for item in self.registration_qc.section_records)
        qc_path_object_ids = tuple(
            item.tissue_path_object_id for item in self.registration_qc.section_records
        )
        if (
            qc_sections != sections
            or qc_roi_ids != roi_ids
            or qc_path_object_ids != path_object_ids
        ):
            raise ROIAnalysisError("registration QC section scope conflicts with its record")
        if acceptance == "accepted" and (
            self.registration_qc.computed_status != "passed"
            or self.registration_qc.review_state != "accepted"
        ):
            raise ROIAnalysisError(
                "accepted registration requires passing explicitly accepted registration QC"
            )
        if acceptance == "pending_review" and self.registration_qc.review_state != "pending":
            raise ROIAnalysisError("pending registration requires pending QC review")
        if acceptance == "rejected" and self.registration_qc.review_state != "rejected":
            raise ROIAnalysisError("rejected registration requires rejected QC review")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "supersedes_registration_id", supersedes)
        object.__setattr__(self, "predecessor_record_sha256", predecessor_digest)
        object.__setattr__(self, "acceptance_state", acceptance)

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("warpy-registration", "1")
        return {
            "schema_version": self.schema_version,
            "registration_id": self.registration_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "moving_project_entry_id": self.moving_project_entry_id,
            "fixed_project_entry_id": self.fixed_project_entry_id,
            "moving_image_id": self.moving_image_id,
            "fixed_image_id": self.fixed_image_id,
            "moving_image_sha256": self.moving_image_sha256,
            "fixed_image_sha256": self.fixed_image_sha256,
            "moving_image_path": str(self.moving_image_path),
            "fixed_image_path": str(self.fixed_image_path),
            "direction": self.direction,
            "moving_physical_calibration": self.moving_physical_calibration.to_mapping(),
            "fixed_physical_calibration": self.fixed_physical_calibration.to_mapping(),
            "coordinate_pre_transform": (
                None
                if self.coordinate_pre_transform is None
                else self.coordinate_pre_transform.to_mapping()
            ),
            "coordinate_post_transform": (
                None
                if self.coordinate_post_transform is None
                else self.coordinate_post_transform.to_mapping()
            ),
            "transform_file": str(self.transform_file),
            "transform_file_sha256": self.transform_file_sha256,
            "runtime_versions": _export_json(self.runtime_versions),
            "moving_modality": self.moving_modality,
            "moving_image_metadata": _export_json(self.moving_image_metadata),
            "tissue_generation_id": self.tissue_generation_id,
            "tissue_section_ids": list(self.tissue_section_ids),
            "tissue_roi_ids": list(self.tissue_roi_ids),
            "tissue_path_object_ids": list(self.tissue_path_object_ids),
            "revision": self.revision,
            "supersedes_registration_id": self.supersedes_registration_id,
            "predecessor_record_sha256": self.predecessor_record_sha256,
            "acceptance_state": self.acceptance_state,
            "registration_qc": self.registration_qc.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationRecord":
        """Decode the serialized fields into a validated RegistrationRecord record.

        The mapping is checked against the declared field inventory, including
        ``schema_version``, ``registration_id``, ``provider``, ``provider_version``,
        ``moving_project_entry_id``, ``fixed_project_entry_id``. Nested values are decoded
        through the corresponding field validators before the record is constructed."""

        fields = {
            "schema_version",
            "registration_id",
            "provider",
            "provider_version",
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "moving_image_sha256",
            "fixed_image_sha256",
            "moving_image_path",
            "fixed_image_path",
            "direction",
            "moving_physical_calibration",
            "fixed_physical_calibration",
            "coordinate_pre_transform",
            "coordinate_post_transform",
            "transform_file",
            "transform_file_sha256",
            "runtime_versions",
            "moving_modality",
            "moving_image_metadata",
            "tissue_generation_id",
            "tissue_section_ids",
            "tissue_roi_ids",
            "tissue_path_object_ids",
            "revision",
            "supersedes_registration_id",
            "predecessor_record_sha256",
            "acceptance_state",
            "registration_qc",
        }
        mapping = _strict_mapping(value, fields, "registration record")
        pre = mapping["coordinate_pre_transform"]
        post = mapping["coordinate_post_transform"]
        return cls(
            **{
                name: mapping[name]
                for name in fields
                - {
                    "moving_physical_calibration",
                    "fixed_physical_calibration",
                    "coordinate_pre_transform",
                    "coordinate_post_transform",
                    "registration_qc",
                }
            },
            moving_physical_calibration=PhysicalCalibration.from_mapping(
                mapping["moving_physical_calibration"]
            ),
            fixed_physical_calibration=PhysicalCalibration.from_mapping(
                mapping["fixed_physical_calibration"]
            ),
            coordinate_pre_transform=(
                None if pre is None else CoordinateTransform.from_mapping(pre)
            ),
            coordinate_post_transform=(
                None if post is None else CoordinateTransform.from_mapping(post)
            ),
            registration_qc=RegistrationQCRecord.from_mapping(mapping["registration_qc"]),
        )


@dataclass(frozen=True, slots=True)
class RegistrationImportScope:
    moving_project_entry_id: str
    fixed_project_entry_id: str
    moving_image_id: str
    fixed_image_id: str
    moving_image_sha256: str
    fixed_image_sha256: str
    moving_image_path: Path
    fixed_image_path: Path
    transform_file: Path
    transform_file_sha256: str
    tissue_generation_id: str
    tissue_section_ids: tuple[str, ...]
    tissue_roi_ids: tuple[str, ...]
    tissue_path_object_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "tissue_generation_id",
        ):
            object.__setattr__(self, name, _string(getattr(self, name), name))
        object.__setattr__(
            self, "moving_image_sha256", _sha256(self.moving_image_sha256, "moving_image_sha256")
        )
        object.__setattr__(
            self, "fixed_image_sha256", _sha256(self.fixed_image_sha256, "fixed_image_sha256")
        )
        object.__setattr__(
            self, "moving_image_path", _path(self.moving_image_path, "moving_image_path")
        )
        object.__setattr__(
            self, "fixed_image_path", _path(self.fixed_image_path, "fixed_image_path")
        )
        object.__setattr__(self, "transform_file", _path(self.transform_file, "transform_file"))
        object.__setattr__(
            self,
            "transform_file_sha256",
            _sha256(self.transform_file_sha256, "transform_file_sha256"),
        )
        object.__setattr__(
            self, "tissue_section_ids", _strings(self.tissue_section_ids, "tissue_section_ids")
        )
        object.__setattr__(self, "tissue_roi_ids", _strings(self.tissue_roi_ids, "tissue_roi_ids"))
        object.__setattr__(
            self,
            "tissue_path_object_ids",
            _strings(self.tissue_path_object_ids, "tissue_path_object_ids"),
        )
        if not (
            len(self.tissue_section_ids)
            == len(self.tissue_roi_ids)
            == len(self.tissue_path_object_ids)
        ):
            raise ROIAnalysisError("registration import tissue identity arrays must align")


def resolve_registration_review(
    record: RegistrationRecord, *, review_state: str, reviewed_by: object
) -> RegistrationRecord:
    """Resolve one pending review without changing its evidence or lineage."""

    if type(record) is not RegistrationRecord:
        raise ROIAnalysisError("registration review requires a RegistrationRecord")
    if (
        record.acceptance_state != "pending_review"
        or record.registration_qc.review_state != "pending"
    ):
        raise ROIAnalysisError("registration review requires a pending record")
    if review_state not in ("accepted", "rejected"):
        raise ROIAnalysisError("registration review must be accepted or rejected")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ROIAnalysisError("registration review requires reviewed_by")
    if review_state == "accepted" and record.registration_qc.computed_status != "passed":
        raise ROIAnalysisError("registration acceptance requires passing global and per-section QC")
    mapping = record.to_mapping()
    qc = dict(mapping["registration_qc"])
    qc["review_state"] = review_state
    qc["reviewed_by"] = reviewed_by.strip()
    mapping["registration_qc"] = qc
    mapping["acceptance_state"] = review_state
    return RegistrationRecord.from_mapping(mapping)


def registration_record_sha256(record: RegistrationRecord) -> str:
    """Return the canonical digest used to bind a reviewed immutable record."""

    if not isinstance(record, RegistrationRecord):
        raise ROIAnalysisError("registration record digest requires a RegistrationRecord")
    payload = json.dumps(
        record.to_mapping(), allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def import_registration_record(
    value: object,
    *,
    expected_scope: RegistrationImportScope,
    predecessor: RegistrationRecord | None = None,
    predecessor_sha256: str | None = None,
) -> RegistrationRecord:
    """Import a Warpy exchange only when every immutable scope field matches."""

    if not isinstance(expected_scope, RegistrationImportScope):
        raise ROIAnalysisError("expected registration scope is invalid")
    record = RegistrationRecord.from_mapping(value)
    checks = {
        "moving_project_entry_id": expected_scope.moving_project_entry_id,
        "fixed_project_entry_id": expected_scope.fixed_project_entry_id,
        "moving_image_id": expected_scope.moving_image_id,
        "fixed_image_id": expected_scope.fixed_image_id,
        "moving_image_sha256": expected_scope.moving_image_sha256,
        "fixed_image_sha256": expected_scope.fixed_image_sha256,
        "moving_image_path": expected_scope.moving_image_path,
        "fixed_image_path": expected_scope.fixed_image_path,
        "transform_file": expected_scope.transform_file,
        "transform_file_sha256": expected_scope.transform_file_sha256,
        "tissue_generation_id": expected_scope.tissue_generation_id,
        "tissue_section_ids": expected_scope.tissue_section_ids,
        "tissue_roi_ids": expected_scope.tissue_roi_ids,
        "tissue_path_object_ids": expected_scope.tissue_path_object_ids,
    }
    mismatched = tuple(
        name for name, expected in checks.items() if getattr(record, name) != expected
    )
    if mismatched:
        raise ROIAnalysisError("registration import scope mismatch: " + ", ".join(mismatched))
    if not record.transform_file.is_file():
        raise ROIAnalysisError("registration transform file is missing")
    if _stream_sha256(record.transform_file) != record.transform_file_sha256:
        raise ROIAnalysisError("registration transform file scope checksum mismatch")
    if record.revision == 1:
        if predecessor is not None or predecessor_sha256 is not None:
            raise ROIAnalysisError("initial registration cannot declare a predecessor")
        return record
    if not isinstance(predecessor, RegistrationRecord) or predecessor_sha256 is None:
        raise ROIAnalysisError("registration revision requires its exact predecessor record")
    expected_predecessor_sha256 = _sha256(predecessor_sha256, "predecessor_sha256")
    if registration_record_sha256(predecessor) != expected_predecessor_sha256:
        raise ROIAnalysisError("registration predecessor digest mismatch")
    if record.predecessor_record_sha256 != expected_predecessor_sha256:
        raise ROIAnalysisError("registration record predecessor digest mismatch")
    if (
        record.supersedes_registration_id != predecessor.registration_id
        or record.revision != predecessor.revision + 1
    ):
        raise ROIAnalysisError("registration predecessor lineage is not revision + 1")
    continuity_fields = (
        "provider",
        "provider_version",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_sha256",
        "fixed_image_sha256",
        "direction",
        "moving_image_path",
        "fixed_image_path",
        "moving_physical_calibration",
        "fixed_physical_calibration",
        "coordinate_pre_transform",
        "coordinate_post_transform",
        "transform_file",
        "moving_modality",
        "moving_image_metadata",
        "tissue_generation_id",
        "tissue_section_ids",
        "tissue_roi_ids",
        "tissue_path_object_ids",
    )
    changed = tuple(
        field
        for field in continuity_fields
        if getattr(record, field) != getattr(predecessor, field)
    )
    if changed:
        raise ROIAnalysisError(
            "registration predecessor scope continuity mismatch: " + ", ".join(changed)
        )
    return record


def registration_result_identity_spec() -> RegistrationResultIdentitySpec:
    """Return the immutable owner specification for ``registration_result``."""

    return REGISTRATION_RESULT_IDENTITY_SPEC


def _identity_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def project_registration_result_identity(
    record: object,
    *,
    expected_scope: RegistrationImportScope,
    predecessor: RegistrationRecord | None = None,
    predecessor_sha256: str | None = None,
) -> Mapping[str, str]:
    """Project one accepted, scope-validated registration into batch identity.

    A successor record requires the exact predecessor and its canonical digest,
    just as :func:`import_registration_record` does. Initial revisions keep the
    existing no-predecessor call shape.

    ``transform_qc_review_sha256`` is SHA-256 over the UTF-8 canonical JSON
    bytes of ``record.registration_qc.to_mapping()``: sorted keys, compact
    separators, ``ensure_ascii=False``, and no NaN.  The nested QC mapping
    carries the exact computed QC evidence and authenticated review state.
    ``original_tissue_pathobject_ids_sha256`` is SHA-256 over the same canonical
    JSON encoding of the ordered ``list(record.tissue_path_object_ids)``.
    """

    if type(record) is not RegistrationRecord:
        raise ROIAnalysisError("registration result identity requires a current RegistrationRecord")
    if type(expected_scope) is not RegistrationImportScope:
        raise ROIAnalysisError("registration result identity requires a RegistrationImportScope")
    current = import_registration_record(
        record.to_mapping(),
        expected_scope=expected_scope,
        predecessor=predecessor,
        predecessor_sha256=predecessor_sha256,
    )
    if (
        current.acceptance_state != "accepted"
        or current.registration_qc.computed_status != "passed"
        or current.registration_qc.review_state != "accepted"
    ):
        raise ROIAnalysisError(
            "registration result identity requires an explicitly accepted registration"
        )
    return MappingProxyType(
        {
            "fixed_project_entry_id": current.fixed_project_entry_id,
            "moving_project_entry_id": current.moving_project_entry_id,
            "fixed_image_sha256": current.fixed_image_sha256,
            "moving_image_sha256": current.moving_image_sha256,
            "direction": current.direction,
            "transform_qc_review_sha256": _identity_digest(current.registration_qc.to_mapping()),
            "original_tissue_pathobject_ids_sha256": _identity_digest(
                list(current.tissue_path_object_ids)
            ),
        }
    )


def validate_registration_lineage(
    values: Sequence[RegistrationRecord],
) -> tuple[RegistrationRecord, ...]:
    """Validate a complete in-memory revision graph without reading live files.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    records = tuple(values)
    if any(not isinstance(item, RegistrationRecord) for item in records):
        raise ROIAnalysisError("registration lineage contains an invalid record")
    by_id = {item.registration_id: item for item in records}
    if len(by_id) != len(records):
        raise ROIAnalysisError("registration lineage IDs must be unique")
    continuity_fields = (
        "provider",
        "provider_version",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_sha256",
        "fixed_image_sha256",
        "direction",
        "moving_image_path",
        "fixed_image_path",
        "moving_physical_calibration",
        "fixed_physical_calibration",
        "coordinate_pre_transform",
        "coordinate_post_transform",
        "transform_file",
        "moving_modality",
        "moving_image_metadata",
        "tissue_generation_id",
        "tissue_section_ids",
        "tissue_roi_ids",
        "tissue_path_object_ids",
    )
    for record in records:
        if record.revision == 1:
            continue
        predecessor = by_id.get(record.supersedes_registration_id)
        if predecessor is None:
            raise ROIAnalysisError("registration lineage predecessor does not exist")
        if predecessor.revision + 1 != record.revision:
            raise ROIAnalysisError("registration lineage is not revision + 1")
        if registration_record_sha256(predecessor) != record.predecessor_record_sha256:
            raise ROIAnalysisError("registration lineage predecessor digest mismatch")
        if any(
            getattr(record, field) != getattr(predecessor, field) for field in continuity_fields
        ):
            raise ROIAnalysisError("registration lineage scope continuity mismatch")
    return records


@dataclass(frozen=True, slots=True)
class TransferQCPolicy:
    min_target_tissue_intersection_fraction: float = 0.5
    min_area_ratio: float = 0.25
    max_area_ratio: float = 4.0
    max_fragment_count_increase: int = 1
    max_out_of_bounds_fraction: float = 0.05

    def __post_init__(self) -> None:
        minimum_intersection = _fraction(
            self.min_target_tissue_intersection_fraction,
            "min_target_tissue_intersection_fraction",
        )
        minimum_ratio = _number(self.min_area_ratio, "min_area_ratio", positive=True)
        maximum_ratio = _number(self.max_area_ratio, "max_area_ratio", positive=True)
        if minimum_ratio >= maximum_ratio:
            raise ROIAnalysisError("min_area_ratio must be below max_area_ratio")
        fragments = _integer(
            self.max_fragment_count_increase,
            "max_fragment_count_increase",
            nonnegative=True,
        )
        maximum_oob = _fraction(self.max_out_of_bounds_fraction, "max_out_of_bounds_fraction")
        object.__setattr__(self, "min_target_tissue_intersection_fraction", minimum_intersection)
        object.__setattr__(self, "min_area_ratio", minimum_ratio)
        object.__setattr__(self, "max_area_ratio", maximum_ratio)
        object.__setattr__(self, "max_fragment_count_increase", fragments)
        object.__setattr__(self, "max_out_of_bounds_fraction", maximum_oob)

    def to_mapping(self) -> dict[str, object]:
        return {
            "min_target_tissue_intersection_fraction": self.min_target_tissue_intersection_fraction,
            "min_area_ratio": self.min_area_ratio,
            "max_area_ratio": self.max_area_ratio,
            "max_fragment_count_increase": self.max_fragment_count_increase,
            "max_out_of_bounds_fraction": self.max_out_of_bounds_fraction,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TransferQCPolicy":
        mapping = _strict_mapping(
            value,
            {
                "min_target_tissue_intersection_fraction",
                "min_area_ratio",
                "max_area_ratio",
                "max_fragment_count_increase",
                "max_out_of_bounds_fraction",
            },
            "transfer QC policy",
        )
        return cls(**mapping)  # type: ignore[arg-type]

    def validate_evidence(self, value: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
        if not isinstance(value, Mapping):
            raise ROIAnalysisError("transfer QC evidence must be a mapping")
        reasons: list[str] = []
        if not _boolean(value.get("geometry_valid"), "geometry_valid"):
            reasons.append("geometry_invalid")
        if not _boolean(value.get("geometry_nonempty"), "geometry_nonempty"):
            reasons.append("geometry_empty")
        if not _boolean(value.get("coordinates_finite"), "coordinates_finite"):
            reasons.append("coordinates_nonfinite")
        intersection = _fraction(
            value.get("target_tissue_intersection_fraction"),
            "target_tissue_intersection_fraction",
        )
        if intersection < self.min_target_tissue_intersection_fraction:
            reasons.append("target_tissue_intersection_insufficient")
        ratio = _number(value.get("area_ratio"), "area_ratio", positive=True)
        if ratio < self.min_area_ratio or ratio > self.max_area_ratio:
            reasons.append("area_ratio_implausible")
        if _boolean(value.get("self_intersection"), "self_intersection"):
            reasons.append("pathological_self_intersection")
        original_fragments = _integer(
            value.get("original_fragment_count"),
            "original_fragment_count",
            positive=True,
        )
        transformed_fragments = _integer(
            value.get("transformed_fragment_count"),
            "transformed_fragment_count",
            positive=True,
        )
        if transformed_fragments - original_fragments > self.max_fragment_count_increase:
            reasons.append("unexpected_fragmentation")
        candidates = _strings(
            value.get("candidate_target_section_ids"),
            "candidate_target_section_ids",
            require=False,
        )
        if len(candidates) != 1:
            reasons.append("target_section_ambiguous")
        out_of_bounds = _fraction(value.get("out_of_bounds_fraction"), "out_of_bounds_fraction")
        if out_of_bounds > self.max_out_of_bounds_fraction:
            reasons.append("out_of_bounds_exceeded")
        return ("failed", tuple(reasons)) if reasons else ("passed", ())


@dataclass(frozen=True, slots=True)
class TransferQCRecord:
    schema_version: str
    transfer_qc_id: str
    registration_id: str
    registration_revision: int
    tissue_generation_id: str
    moving_project_entry_id: str
    fixed_project_entry_id: str
    moving_image_id: str
    fixed_image_id: str
    moving_image_sha256: str
    fixed_image_sha256: str
    transform_file: Path
    transform_file_sha256: str
    moving_physical_calibration: PhysicalCalibration
    fixed_physical_calibration: PhysicalCalibration
    policy: TransferQCPolicy
    source_object_id: str
    source_object_type: str | None
    source_has_descendants: bool | None
    source_has_nucleus_roi: bool | None
    source_geometry_sha256: str | None
    source_object_revision_sha256: str | None
    target_object_id: str | None
    transformed_geometry_sha256: str | None
    target_section_id: str | None
    geometry_valid: bool | None
    geometry_nonempty: bool | None
    coordinates_finite: bool | None
    target_tissue_intersection_fraction: float | None
    original_area_um2: float | None
    transformed_area_um2: float | None
    area_ratio: float | None
    self_intersection: bool | None
    original_fragment_count: int | None
    transformed_fragment_count: int | None
    candidate_target_section_ids: tuple[str, ...]
    out_of_bounds_fraction: float | None
    computed_status: str
    review_state: str
    disposition: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate the TransferQCRecord fields when the record is constructed.

        The checks cover ``target_tissue_intersection_fraction``, ``original_area_um2``,
        ``transformed_area_um2``, ``area_ratio``, ``original_fragment_count``,
        ``transformed_fragment_count``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if self.schema_version != TRANSFER_QC_SCHEMA_VERSION:
            raise ROIAnalysisError("transfer QC schema version is unsupported")
        for name in (
            "transfer_qc_id",
            "registration_id",
            "tissue_generation_id",
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "source_object_id",
        ):
            object.__setattr__(self, name, _string(getattr(self, name), name))
        object.__setattr__(
            self,
            "source_object_type",
            _optional_string(self.source_object_type, "source_object_type"),
        )
        if not isinstance(self.policy, TransferQCPolicy):
            raise ROIAnalysisError("transfer QC policy is invalid")
        object.__setattr__(
            self, "moving_image_sha256", _sha256(self.moving_image_sha256, "moving_image_sha256")
        )
        object.__setattr__(
            self, "fixed_image_sha256", _sha256(self.fixed_image_sha256, "fixed_image_sha256")
        )
        object.__setattr__(self, "transform_file", _path(self.transform_file, "transform_file"))
        object.__setattr__(
            self,
            "transform_file_sha256",
            _sha256(self.transform_file_sha256, "transform_file_sha256"),
        )
        if not isinstance(self.moving_physical_calibration, PhysicalCalibration) or not isinstance(
            self.fixed_physical_calibration, PhysicalCalibration
        ):
            raise ROIAnalysisError("transfer QC requires both physical calibrations")
        for name in (
            "source_geometry_sha256",
            "source_object_revision_sha256",
            "transformed_geometry_sha256",
        ):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _sha256(value, name))
        for name in ("source_has_descendants", "source_has_nucleus_roi"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _boolean(value, name))
        object.__setattr__(
            self, "target_object_id", _optional_string(self.target_object_id, "target_object_id")
        )
        object.__setattr__(
            self, "target_section_id", _optional_string(self.target_section_id, "target_section_id")
        )
        object.__setattr__(
            self,
            "registration_revision",
            _integer(self.registration_revision, "registration revision", positive=True),
        )
        status = _enum(self.computed_status, _QC_STATUSES, "transfer computed_status")
        for name in (
            "geometry_valid",
            "geometry_nonempty",
            "coordinates_finite",
            "self_intersection",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                None if value is None else _boolean(value, name),
            )
        intersection = self.target_tissue_intersection_fraction
        object.__setattr__(
            self,
            "target_tissue_intersection_fraction",
            (
                None
                if intersection is None
                else _fraction(intersection, "target_tissue_intersection_fraction")
            ),
        )
        original_area = self.original_area_um2
        object.__setattr__(
            self,
            "original_area_um2",
            (
                None
                if original_area is None
                else _number(original_area, "original_area_um2", positive=True)
            ),
        )
        transformed_area = self.transformed_area_um2
        ratio = self.area_ratio
        object.__setattr__(
            self,
            "transformed_area_um2",
            (
                None
                if transformed_area is None
                else _number(transformed_area, "transformed_area_um2", positive=True)
            ),
        )
        object.__setattr__(
            self,
            "area_ratio",
            None if ratio is None else _number(ratio, "area_ratio", positive=True),
        )
        if (
            self.original_area_um2 is not None
            and self.transformed_area_um2 is not None
            and self.area_ratio is not None
        ):
            expected_ratio = self.transformed_area_um2 / self.original_area_um2
            if abs(self.area_ratio - expected_ratio) > max(1e-9, abs(expected_ratio) * 1e-9):
                raise ROIAnalysisError("transfer area_ratio conflicts with physical areas")
        elif self.transformed_area_um2 is not None or self.area_ratio is not None:
            raise ROIAnalysisError(
                "transfer physical area evidence must be complete or unavailable"
            )
        original_fragments = self.original_fragment_count
        object.__setattr__(
            self,
            "original_fragment_count",
            (
                None
                if original_fragments is None
                else _integer(original_fragments, "original_fragment_count", positive=True)
            ),
        )
        transformed_fragments = self.transformed_fragment_count
        object.__setattr__(
            self,
            "transformed_fragment_count",
            (
                None
                if transformed_fragments is None
                else _integer(transformed_fragments, "transformed_fragment_count", positive=True)
            ),
        )
        candidates = _strings(
            self.candidate_target_section_ids,
            "candidate_target_section_ids",
            require=False,
        )
        object.__setattr__(self, "candidate_target_section_ids", candidates)
        out_of_bounds = self.out_of_bounds_fraction
        object.__setattr__(
            self,
            "out_of_bounds_fraction",
            None if out_of_bounds is None else _fraction(out_of_bounds, "out_of_bounds_fraction"),
        )
        review = _enum(self.review_state, _REVIEW_STATES, "transfer review_state")
        disposition = _enum(self.disposition, _DISPOSITIONS, "transfer disposition")
        reasons = _strings(self.reason_codes, "transfer reason_codes", require=False)
        expected_disposition = (
            {
                "pending": "pending_review",
                "accepted": "accepted",
                "rejected": "rejected",
            }[review]
            if status == "passed"
            else {"failed": "rejected", "uncertain": "quarantined"}[status]
        )
        if disposition != expected_disposition:
            raise ROIAnalysisError("transfer disposition conflicts with computed status")
        if status == "passed":
            if (
                self.source_object_type != "PathAnnotationObject"
                or self.source_has_descendants is not False
                or self.source_has_nucleus_roi is not False
            ):
                raise ROIAnalysisError(
                    "passing transfer proposal requires a single-ROI leaf annotation"
                )
            required_evidence = (
                self.geometry_valid,
                self.geometry_nonempty,
                self.coordinates_finite,
                self.source_geometry_sha256,
                self.source_object_revision_sha256,
                self.source_has_descendants,
                self.source_has_nucleus_roi,
                self.transformed_geometry_sha256,
                self.target_tissue_intersection_fraction,
                self.original_area_um2,
                self.transformed_area_um2,
                self.area_ratio,
                self.self_intersection,
                self.original_fragment_count,
                self.transformed_fragment_count,
                self.out_of_bounds_fraction,
            )
            if any(item is None for item in required_evidence):
                raise ROIAnalysisError("passing transfer requires complete QC evidence")
            if review not in {"pending", "accepted", "rejected"}:
                raise ROIAnalysisError(
                    "passing transfer review must be pending, accepted, or rejected"
                )
            if reasons:
                raise ROIAnalysisError("passing transfer cannot carry failure reasons")
            if self.target_object_id is None or self.target_section_id is None:
                raise ROIAnalysisError("passing transfer requires target object and section IDs")
            if candidates != (self.target_section_id,):
                raise ROIAnalysisError("passing transfer requires one exact target section")
        else:
            if status == "failed" and (
                self.source_object_type is None
                or self.source_has_descendants is None
                or self.source_has_nucleus_roi is None
                or self.source_geometry_sha256 is None
                or self.source_object_revision_sha256 is None
            ):
                raise ROIAnalysisError("failed transfer requires resolved source-object identity")
            if status == "failed" and any(
                item is None
                for item in (
                    self.geometry_valid,
                    self.geometry_nonempty,
                    self.coordinates_finite,
                    self.source_geometry_sha256,
                    self.source_object_revision_sha256,
                    self.source_has_descendants,
                    self.transformed_geometry_sha256,
                    self.target_tissue_intersection_fraction,
                    self.original_area_um2,
                    self.transformed_area_um2,
                    self.area_ratio,
                    self.self_intersection,
                    self.original_fragment_count,
                    self.transformed_fragment_count,
                    self.out_of_bounds_fraction,
                )
            ):
                raise ROIAnalysisError("failed transfer requires complete QC evidence")
            if status == "uncertain" and all(
                item is not None
                for item in (
                    self.geometry_valid,
                    self.geometry_nonempty,
                    self.coordinates_finite,
                    self.source_geometry_sha256,
                    self.source_object_revision_sha256,
                    self.source_has_descendants,
                    self.transformed_geometry_sha256,
                    self.target_tissue_intersection_fraction,
                    self.original_area_um2,
                    self.transformed_area_um2,
                    self.area_ratio,
                    self.self_intersection,
                    self.original_fragment_count,
                    self.transformed_fragment_count,
                    self.out_of_bounds_fraction,
                )
            ):
                raise ROIAnalysisError(
                    "uncertain transfer requires explicitly unavailable evidence"
                )
            if not reasons:
                raise ROIAnalysisError("unsafe transfer requires reason codes")
            if review == "accepted":
                raise ROIAnalysisError("unsafe transfer cannot have accepted review")
        if status != "uncertain":
            computed_status, computed_reasons = self.policy.validate_evidence(self.to_mapping())
            if status != computed_status or reasons != computed_reasons:
                raise ROIAnalysisError(
                    "transfer QC policy result conflicts with the immutable record"
                )
        object.__setattr__(self, "computed_status", status)
        object.__setattr__(self, "review_state", review)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "reason_codes", reasons)

    @property
    def authoritative(self) -> bool:
        """Whether this independently validated object may enter mIF hierarchy."""

        return (
            self.computed_status == "passed"
            and self.review_state == "accepted"
            and self.disposition == "accepted"
        )

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("warpy-transfer-qc", "1")
        return {
            "schema_version": self.schema_version,
            "transfer_qc_id": self.transfer_qc_id,
            "registration_id": self.registration_id,
            "registration_revision": self.registration_revision,
            "tissue_generation_id": self.tissue_generation_id,
            "moving_project_entry_id": self.moving_project_entry_id,
            "fixed_project_entry_id": self.fixed_project_entry_id,
            "moving_image_id": self.moving_image_id,
            "fixed_image_id": self.fixed_image_id,
            "moving_image_sha256": self.moving_image_sha256,
            "fixed_image_sha256": self.fixed_image_sha256,
            "transform_file": str(self.transform_file),
            "transform_file_sha256": self.transform_file_sha256,
            "moving_physical_calibration": self.moving_physical_calibration.to_mapping(),
            "fixed_physical_calibration": self.fixed_physical_calibration.to_mapping(),
            "policy": self.policy.to_mapping(),
            "source_object_id": self.source_object_id,
            "source_object_type": self.source_object_type,
            "source_has_descendants": self.source_has_descendants,
            "source_has_nucleus_roi": self.source_has_nucleus_roi,
            "source_geometry_sha256": self.source_geometry_sha256,
            "source_object_revision_sha256": self.source_object_revision_sha256,
            "target_object_id": self.target_object_id,
            "transformed_geometry_sha256": self.transformed_geometry_sha256,
            "target_section_id": self.target_section_id,
            "geometry_valid": self.geometry_valid,
            "geometry_nonempty": self.geometry_nonempty,
            "coordinates_finite": self.coordinates_finite,
            "target_tissue_intersection_fraction": self.target_tissue_intersection_fraction,
            "original_area_um2": self.original_area_um2,
            "transformed_area_um2": self.transformed_area_um2,
            "area_ratio": self.area_ratio,
            "self_intersection": self.self_intersection,
            "original_fragment_count": self.original_fragment_count,
            "transformed_fragment_count": self.transformed_fragment_count,
            "candidate_target_section_ids": list(self.candidate_target_section_ids),
            "out_of_bounds_fraction": self.out_of_bounds_fraction,
            "computed_status": self.computed_status,
            "review_state": self.review_state,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TransferQCRecord":
        """Decode the serialized fields into a validated TransferQCRecord record.

        The mapping is checked against the declared field inventory, including
        ``schema_version``, ``transfer_qc_id``, ``registration_id``,
        ``registration_revision``, ``tissue_generation_id``, ``moving_project_entry_id``.
        Nested values are decoded through the corresponding field validators before the
        record is constructed."""

        fields = {
            "schema_version",
            "transfer_qc_id",
            "registration_id",
            "registration_revision",
            "tissue_generation_id",
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "moving_image_sha256",
            "fixed_image_sha256",
            "transform_file",
            "transform_file_sha256",
            "moving_physical_calibration",
            "fixed_physical_calibration",
            "policy",
            "source_object_id",
            "source_object_type",
            "source_has_descendants",
            "source_has_nucleus_roi",
            "source_geometry_sha256",
            "source_object_revision_sha256",
            "target_object_id",
            "transformed_geometry_sha256",
            "target_section_id",
            "geometry_valid",
            "geometry_nonempty",
            "coordinates_finite",
            "target_tissue_intersection_fraction",
            "original_area_um2",
            "transformed_area_um2",
            "area_ratio",
            "self_intersection",
            "original_fragment_count",
            "transformed_fragment_count",
            "candidate_target_section_ids",
            "out_of_bounds_fraction",
            "computed_status",
            "review_state",
            "disposition",
            "reason_codes",
        }
        mapping = _strict_mapping(value, fields, "transfer QC record")
        return cls(
            **{
                name: mapping[name]
                for name in fields
                - {
                    "policy",
                    "moving_physical_calibration",
                    "fixed_physical_calibration",
                }
            },
            policy=TransferQCPolicy.from_mapping(mapping["policy"]),
            moving_physical_calibration=PhysicalCalibration.from_mapping(
                mapping["moving_physical_calibration"]
            ),
            fixed_physical_calibration=PhysicalCalibration.from_mapping(
                mapping["fixed_physical_calibration"]
            ),
        )  # type: ignore[arg-type]


def import_transfer_qc_records(
    value: object,
    *,
    registration: RegistrationRecord,
    tissue_generation_id: str,
    tissue_section_ids: Sequence[str],
    policy: TransferQCPolicy,
) -> tuple[TransferQCRecord, ...]:
    """Validate a complete Warpy transfer-QC batch against accepted scope."""

    if not isinstance(registration, RegistrationRecord):
        raise ROIAnalysisError("transfer QC registration is invalid")
    if registration.acceptance_state != "accepted":
        raise ROIAnalysisError("object transfer requires an accepted registration")
    if not isinstance(policy, TransferQCPolicy):
        raise ROIAnalysisError("transfer QC policy is invalid")
    generation = _string(tissue_generation_id, "tissue_generation_id")
    sections = _strings(tissue_section_ids, "tissue_section_ids")
    if (
        generation != registration.tissue_generation_id
        or sections != registration.tissue_section_ids
    ):
        raise ROIAnalysisError("transfer QC tissue scope conflicts with registration")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("transfer QC records must be an array")
    records = tuple(TransferQCRecord.from_mapping(item) for item in value)
    if len({item.transfer_qc_id for item in records}) != len(records):
        raise ROIAnalysisError("transfer QC IDs must be unique")
    if len({item.source_object_id for item in records}) != len(records):
        raise ROIAnalysisError("source object IDs must be unique within a transfer batch")
    for record in records:
        if record.policy != policy:
            raise ROIAnalysisError("transfer QC record policy conflicts with requested policy")
        if record.registration_id != registration.registration_id:
            raise ROIAnalysisError("transfer QC registration ID conflicts with registration")
        if record.registration_revision != registration.revision:
            raise ROIAnalysisError("transfer QC registration revision conflicts with registration")
        if record.tissue_generation_id != generation:
            raise ROIAnalysisError("transfer QC tissue generation conflicts with registration")
        if (
            record.moving_project_entry_id != registration.moving_project_entry_id
            or record.fixed_project_entry_id != registration.fixed_project_entry_id
        ):
            raise ROIAnalysisError("transfer QC project-entry scope conflicts with registration")
        direct_scope = (
            (record.moving_image_id, registration.moving_image_id),
            (record.fixed_image_id, registration.fixed_image_id),
            (record.moving_image_sha256, registration.moving_image_sha256),
            (record.fixed_image_sha256, registration.fixed_image_sha256),
            (record.transform_file, registration.transform_file),
            (record.transform_file_sha256, registration.transform_file_sha256),
            (record.moving_physical_calibration, registration.moving_physical_calibration),
            (record.fixed_physical_calibration, registration.fixed_physical_calibration),
        )
        if any(actual != expected for actual, expected in direct_scope):
            raise ROIAnalysisError(
                "transfer QC canonical/transform scope conflicts with registration"
            )
        if record.target_section_id is not None and record.target_section_id not in sections:
            raise ROIAnalysisError("transfer QC target section is outside accepted tissue scope")
        if any(candidate not in sections for candidate in record.candidate_target_section_ids):
            raise ROIAnalysisError("transfer QC candidate section is outside accepted tissue scope")
        if record.computed_status != "uncertain":
            computed_status, computed_reasons = policy.validate_evidence(record.to_mapping())
            if record.computed_status != computed_status or record.reason_codes != computed_reasons:
                raise ROIAnalysisError(
                    "transfer QC policy result conflicts with the immutable record"
                )
    return records


def canonical_transfer_attempt_id(
    registration: RegistrationRecord,
    records: Sequence[TransferQCRecord],
) -> str:
    """Derive one attempt identity from exact registration and ordered QC bytes."""

    if not isinstance(registration, RegistrationRecord):
        raise ROIAnalysisError("canonical transfer attempt requires a registration")
    parsed = tuple(records)
    if not parsed or any(not isinstance(item, TransferQCRecord) for item in parsed):
        raise ROIAnalysisError("canonical transfer attempt requires typed QC records")
    material = {
        "schema_version": TRANSFER_ATTEMPT_SCHEMA_VERSION,
        "registration_id": registration.registration_id,
        "registration_revision": registration.revision,
        "ordered_qc_records": [
            {
                "transfer_qc_id": item.transfer_qc_id,
                "sha256": hashlib.sha256(
                    json.dumps(
                        item.to_mapping(),
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for item in parsed
        ],
    }
    digest = hashlib.sha256(
        json.dumps(material, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"transfer-attempt-{digest}"


@dataclass(frozen=True, slots=True)
class TransferAttemptRecord:
    """Append-only exact registration and ordered QC batch preceding commit."""

    schema_version: str
    attempt_id: str
    registration: RegistrationRecord
    source_object_ids: tuple[str, ...]
    qc_records: tuple[TransferQCRecord, ...]
    qc_records_sha256: str

    def __post_init__(self) -> None:
        """Validate the TransferAttemptRecord fields when the record is constructed.

        The checks cover ``schema_version``, ``source_object_ids``, ``qc_records``,
        ``registration``, ``attempt_id``, ``qc_records_sha256``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        if self.schema_version != TRANSFER_ATTEMPT_SCHEMA_VERSION:
            raise ROIAnalysisError("transfer attempt schema version is unsupported")
        object.__setattr__(self, "attempt_id", _string(self.attempt_id, "attempt_id"))
        if not isinstance(self.registration, RegistrationRecord) or (
            self.registration.acceptance_state != "accepted"
        ):
            raise ROIAnalysisError("transfer attempt requires an accepted registration")
        sources = _strings(self.source_object_ids, "source_object_ids")
        records = tuple(self.qc_records)
        if tuple(item.source_object_id for item in records) != sources:
            raise ROIAnalysisError("transfer attempt QC order differs from source requests")
        if not records:
            raise ROIAnalysisError("transfer attempt requires QC records")
        if any(not isinstance(item, TransferQCRecord) for item in records):
            raise ROIAnalysisError("transfer attempt contains invalid QC records")
        if len({item.transfer_qc_id for item in records}) != len(records):
            raise ROIAnalysisError("transfer attempt QC record IDs must be unique")
        for record in records:
            if (
                record.registration_id != self.registration.registration_id
                or record.registration_revision != self.registration.revision
            ):
                raise ROIAnalysisError("transfer attempt registration scope conflicts")
        canonical_id = canonical_transfer_attempt_id(self.registration, records)
        if self.attempt_id != canonical_id:
            raise ROIAnalysisError("transfer attempt ID is not canonical")
        expected = hashlib.sha256(
            json.dumps(
                [item.to_mapping() for item in records],
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if _sha256(self.qc_records_sha256, "qc_records_sha256") != expected:
            raise ROIAnalysisError("transfer attempt QC digest mismatch")
        object.__setattr__(self, "source_object_ids", sources)
        object.__setattr__(self, "qc_records", records)
        object.__setattr__(self, "qc_records_sha256", expected)

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("warpy-transfer-attempt-record", "1")
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "registration": self.registration.to_mapping(),
            "source_object_ids": list(self.source_object_ids),
            "qc_records": [item.to_mapping() for item in self.qc_records],
            "qc_records_sha256": self.qc_records_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TransferAttemptRecord":
        mapping = _strict_mapping(
            value,
            {
                "schema_version",
                "attempt_id",
                "registration",
                "source_object_ids",
                "qc_records",
                "qc_records_sha256",
            },
            "transfer attempt",
        )
        sources = mapping["source_object_ids"]
        records = mapping["qc_records"]
        if (
            isinstance(sources, (str, bytes))
            or not isinstance(sources, Sequence)
            or isinstance(records, (str, bytes))
            or not isinstance(records, Sequence)
        ):
            raise ROIAnalysisError("transfer attempt arrays are invalid")
        return cls(
            schema_version=mapping["schema_version"],
            attempt_id=mapping["attempt_id"],
            registration=RegistrationRecord.from_mapping(mapping["registration"]),
            source_object_ids=tuple(sources),
            qc_records=tuple(TransferQCRecord.from_mapping(item) for item in records),
            qc_records_sha256=mapping["qc_records_sha256"],
        )


def transfer_attempt_sha256(record: TransferAttemptRecord) -> str:
    if not isinstance(record, TransferAttemptRecord):
        raise ROIAnalysisError("transfer attempt digest requires a typed record")
    return hashlib.sha256(
        json.dumps(
            record.to_mapping(), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CoordinateTransform",
    "DeformationEvidence",
    "LandmarkEvidence",
    "PhysicalCalibration",
    "RegistrationImportScope",
    "RegistrationQCEvidence",
    "RegistrationQCPolicy",
    "RegistrationQCRecord",
    "RegistrationSamplingContext",
    "RegistrationRecord",
    "SectionRegistrationQCRecord",
    "TransferQCPolicy",
    "TransferQCRecord",
    "TransferAttemptRecord",
    "WARPY_PROVIDER",
    "WARPY_VERSION",
    "import_registration_record",
    "import_transfer_qc_records",
    "canonical_transfer_attempt_id",
    "registration_record_sha256",
    "resolve_registration_review",
    "validate_registration_lineage",
    "transfer_attempt_sha256",
]
