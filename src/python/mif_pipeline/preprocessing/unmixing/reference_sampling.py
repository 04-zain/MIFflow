"""Bounded Bio-Formats sampling in canonical level-0 image coordinates.

The `Level0ReadAttestation` record carries `source_file_sha256`, `canonical_image_id`,
`series_index`, `native_resolution_index`, `measurement_ids` and its remaining declared
fields.

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
import re
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray
from ome_types import OME

from mif_pipeline.io import (
    BioFormatsSource,
    CanonicalOmeTiffHeader,
    DigitalRangeAuthority,
    OpenedSourceIdentity,
    PlaneIndex,
    Region,
    SourceFactsError,
    SourceImageFacts,
    resolve_effective_digital_range,
    resolve_source_image_facts,
    snapshot_opened_source_identity,
)

from .reference_models import (
    QuantitativeMeasurementTransform,
    ReferenceInputEvidence,
    RejectionCount,
)
from .reference_rois import (
    AcceptedReferenceRoiSelection,
    _geometries_intersect,
    _rings_touch,
    _validated_geometry,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REJECTION_ORDER = (
    "artifact_mask",
    "nonfinite",
    "raw_out_of_range",
    "saturated",
    "near_zero",
)
_MASK_POLICY = "global-level0-pixel-centers-shell-and-hole-boundaries-v1"
_RANK_POLICY = "sha256-seeded-splitmix64-global-pixel-v1"
TARGET_SAMPLING_STRATUM_MINIMUM_POLICY_ID = (
    "minimum-retained-eligible-vectors-per-nonempty-stratum-v1"
)
_UINT64_MASK = (1 << 64) - 1


# ==============================================================================
#                               Reference sampling
# ==============================================================================


class SamplingError(ValueError):
    """Canonical evidence cannot safely support bounded spectral sampling."""


@dataclass(frozen=True, slots=True)
class TargetSamplingStratumMinimumViolation:
    """One exact stratum count below its configured structural minimum."""

    stratum_id: str
    observed_count: int
    minimum_required: int

    def __post_init__(self) -> None:
        _text(self.stratum_id, "stratum_id")
        _nonnegative_integer(self.observed_count, "observed_count")
        minimum = _positive_integer(self.minimum_required, "minimum_required")
        if minimum < 2:
            raise ValueError("minimum_required must be at least 2")
        if self.observed_count >= minimum:
            raise ValueError("minimum violation must be below the required count")


class TargetSamplingStratumMinimumError(SamplingError):
    """Raster or QC-retained stratum support is below policy."""

    def __init__(
        self,
        phase: str,
        violations: tuple[TargetSamplingStratumMinimumViolation, ...],
    ) -> None:
        if phase not in {"raster_preflight", "qc_postflight"}:
            raise ValueError("unsupported target-sampling minimum failure phase")
        if (
            type(violations) is not tuple
            or not violations
            or any(type(item) is not TargetSamplingStratumMinimumViolation for item in violations)
        ):
            raise TypeError("violations must be an exact nonempty violation tuple")
        ids = tuple(item.stratum_id for item in violations)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("minimum violations must use unique canonical stratum order")
        self.phase = phase
        self.violations = violations
        detail = ", ".join(
            f"{item.stratum_id}: count={item.observed_count}, minimum={item.minimum_required}"
            for item in violations
        )
        super().__init__(f"target sampling stratum minimum failed during {phase}: {detail}")


class TargetSamplingCapacityError(SamplingError):
    """The requested cap cannot reserve the minimum for every stratum."""

    def __init__(
        self,
        *,
        maximum_sample_count: int,
        represented_stratum_count: int,
        minimum_per_stratum: int,
    ) -> None:
        self.phase = "raster_preflight"
        self.maximum_sample_count = _positive_integer(maximum_sample_count, "maximum_sample_count")
        self.represented_stratum_count = _positive_integer(
            represented_stratum_count, "represented_stratum_count"
        )
        self.minimum_per_stratum = _positive_integer(minimum_per_stratum, "minimum_per_stratum")
        if self.minimum_per_stratum < 2:
            raise ValueError("minimum_per_stratum must be at least 2")
        self.required_sample_count = self.represented_stratum_count * self.minimum_per_stratum
        if self.maximum_sample_count >= self.required_sample_count:
            raise ValueError("target sampling capacity is not below the required minimum")
        super().__init__(
            "target sampling cap cannot reserve the structural minimum: "
            f"cap={self.maximum_sample_count}, "
            f"represented_strata={self.represented_stratum_count}, "
            f"minimum_per_stratum={self.minimum_per_stratum}, "
            f"required={self.required_sample_count}"
        )


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _stratum_minimum_policy(
    minimum: object,
    policy_id: object,
) -> int:
    if isinstance(minimum, bool) or type(minimum) is not int or minimum < 2:
        raise ValueError(
            "minimum retained eligible vectors per stratum must be an exact " "integer at least 2"
        )
    if policy_id != TARGET_SAMPLING_STRATUM_MINIMUM_POLICY_ID:
        raise ValueError("unsupported target sampling stratum minimum policy")
    return minimum


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in _record_fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported sampling evidence value {type(value).__name__}")


def _record_mapping(record: object) -> dict[str, object]:
    result = _primitive(record)
    if not isinstance(result, dict):
        raise TypeError("record must be a dataclass instance")
    return result


def _input_evidence_digest(input_evidence: ReferenceInputEvidence) -> str:
    return hashlib.sha256(_canonical_bytes(_record_mapping(input_evidence))).hexdigest()


@dataclass(frozen=True, slots=True)
class ProcessingChunkShape:
    """Computational chunk dimensions, independent of TIFF storage tiles."""

    width: int
    height: int

    def __post_init__(self) -> None:
        _positive_integer(self.width, "processing chunk width")
        _positive_integer(self.height, "processing chunk height")


@dataclass(frozen=True, slots=True)
class MeasurementPlane:
    """One exact spectral measurement mapped to its canonical OME channel."""

    measurement_id: str
    channel_id: str
    channel_index: int

    def __post_init__(self) -> None:
        _text(self.measurement_id, "measurement_id")
        _text(self.channel_id, "channel_id")
        _nonnegative_integer(self.channel_index, "channel_index")


@dataclass(frozen=True, slots=True)
class CompleteVectorQcPolicy:
    """Whole-vector QC applied after explicit acquisition normalization."""

    processing_chunk_shape: ProcessingChunkShape
    saturation_fraction: float
    near_zero_l2_threshold: float

    def __post_init__(self) -> None:
        if not isinstance(self.processing_chunk_shape, ProcessingChunkShape):
            raise TypeError("processing_chunk_shape must be ProcessingChunkShape")
        saturation = _finite(self.saturation_fraction, "saturation_fraction")
        if saturation <= 0.0 or saturation > 1.0:
            raise ValueError("saturation_fraction must be in (0, 1]")
        near_zero = _finite(self.near_zero_l2_threshold, "near_zero_l2_threshold")
        if near_zero < 0.0:
            raise ValueError("near_zero_l2_threshold must be nonnegative")


@runtime_checkable
class ArtifactMaskSource(Protocol):
    """Bounded level-0 artifact mask; true pixels are rejected."""

    @property
    def mask_sha256(self) -> str: ...

    def read_region(self, region: Region) -> NDArray[np.bool_]: ...


@dataclass(frozen=True, slots=True)
class SamplingHeaderBinding:
    """Exact IO-owned file/header identity used by one sampling operation."""

    source_path: str
    source_file_sha256: str
    canonical_image_id: str
    canonical_pixels_id: str
    width_px: int
    height_px: int
    channel_count: int
    container_dtype: str
    container_bits: int
    native_pyramid_dimensions: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _text(self.source_path, "source_path")
        _sha256(self.source_file_sha256, "source_file_sha256")
        _text(self.canonical_image_id, "canonical_image_id")
        _text(self.canonical_pixels_id, "canonical_pixels_id")
        _positive_integer(self.width_px, "width_px")
        _positive_integer(self.height_px, "height_px")
        _positive_integer(self.channel_count, "channel_count")
        _text(self.container_dtype, "container_dtype")
        _positive_integer(self.container_bits, "container_bits")
        if type(self.native_pyramid_dimensions) is not tuple or not self.native_pyramid_dimensions:
            raise ValueError("native_pyramid_dimensions must be a nonempty tuple")
        if any(
            type(item) is not tuple
            or len(item) != 2
            or any(type(value) is not int for value in item)
            for item in self.native_pyramid_dimensions
        ):
            raise TypeError("native_pyramid_dimensions must contain exact integer 2-tuples")
        for width, height in self.native_pyramid_dimensions:
            _positive_integer(width, "native pyramid width")
            _positive_integer(height, "native pyramid height")
        if self.native_pyramid_dimensions[0] != (self.width_px, self.height_px):
            raise ValueError("native pyramid level 0 must match header dimensions")

    @classmethod
    def from_source_facts(cls, facts: SourceImageFacts) -> "SamplingHeaderBinding":
        if type(facts) is not SourceImageFacts:
            raise TypeError("facts must be exact SourceImageFacts")
        return cls(
            source_path=str(facts.identity.resolved_path),
            source_file_sha256=facts.identity.file_sha256,
            canonical_image_id=facts.image_id,
            canonical_pixels_id=facts.pixels_id,
            width_px=facts.size_x,
            height_px=facts.size_y,
            channel_count=facts.size_c,
            container_dtype=facts.container_dtype,
            container_bits=facts.container_bits,
            native_pyramid_dimensions=tuple(
                (item.size_x, item.size_y) for item in facts.native_resolutions
            ),
        )


@dataclass(frozen=True, slots=True)
class Level0ReadAttestation:
    """Bounded-read and complete-vector QC evidence for one sampler run."""

    source_file_sha256: str
    canonical_image_id: str
    series_index: int
    native_resolution_index: int
    measurement_ids: tuple[str, ...]
    processing_chunk_width: int
    processing_chunk_height: int
    native_pyramid_level_count: int
    processing_chunk_count: int
    processing_chunk_inventory_sha256: str
    regional_read_count: int
    maximum_regional_read_pixels: int
    candidate_pixel_count: int
    accepted_vector_count: int
    rejected_counts: tuple[RejectionCount, ...]
    artifact_mask_sha256: str | None
    reader_backend: str
    bffile_version: str
    bioformats_maven_coordinate: str
    bioformats_runtime_version: str
    rasterization_policy: str = _MASK_POLICY
    raw_count_qc: str = "applied"

    def __post_init__(self) -> None:
        """Validate the Level0ReadAttestation fields when the record is constructed.

        The checks cover ``source_file_sha256``, ``canonical_image_id``,
        ``processing_chunk_width``, ``processing_chunk_height``,
        ``native_pyramid_level_count``, ``processing_chunk_count``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _sha256(self.source_file_sha256, "source_file_sha256")
        _text(self.canonical_image_id, "canonical_image_id")
        if self.series_index != 0 or self.native_resolution_index != 0:
            raise ValueError("sampling attestation must bind series 0, native resolution 0")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise ValueError("measurement_ids must be a nonempty tuple")
        if any(type(item) is not str or not item for item in self.measurement_ids):
            raise TypeError("measurement_ids must contain exact nonempty strings")
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError("measurement_ids must be unique")
        _positive_integer(self.processing_chunk_width, "processing_chunk_width")
        _positive_integer(self.processing_chunk_height, "processing_chunk_height")
        _positive_integer(self.native_pyramid_level_count, "native_pyramid_level_count")
        _positive_integer(self.processing_chunk_count, "processing_chunk_count")
        _sha256(self.processing_chunk_inventory_sha256, "chunk inventory SHA-256")
        _positive_integer(self.regional_read_count, "regional_read_count")
        _positive_integer(self.maximum_regional_read_pixels, "maximum_regional_read_pixels")
        _positive_integer(self.candidate_pixel_count, "candidate_pixel_count")
        _positive_integer(self.accepted_vector_count, "accepted_vector_count")
        if self.regional_read_count != self.processing_chunk_count * len(self.measurement_ids):
            raise ValueError("regional read count does not match chunks and measurements")
        if self.maximum_regional_read_pixels > (
            self.processing_chunk_width * self.processing_chunk_height
        ):
            raise ValueError("regional read exceeded the processing chunk bound")
        if type(self.rejected_counts) is not tuple:
            raise TypeError("rejected_counts must be a tuple")
        if any(type(item) is not RejectionCount for item in self.rejected_counts):
            raise TypeError("rejected_counts must contain exact RejectionCount records")
        if self.raw_count_qc not in {"applied", "not_applicable_normalized_intensities"}:
            raise ValueError("Unknown raw-count QC applicability")
        if self.raw_count_qc != "applied" and any(item.count for item in self.rejected_counts if item.reason in {"raw_out_of_range", "saturated"}):
            raise ValueError("Unapplied raw-count QC cannot report count-range rejections")
        reasons = tuple(item.reason for item in self.rejected_counts)
        if reasons != _REJECTION_ORDER:
            raise ValueError("rejected_counts must use the complete canonical reason order")
        rejected_total = sum(item.count for item in self.rejected_counts)
        if self.accepted_vector_count + rejected_total != self.candidate_pixel_count:
            raise ValueError("accepted and rejected vector counts do not cover candidates")
        if self.artifact_mask_sha256 is not None:
            _sha256(self.artifact_mask_sha256, "artifact_mask_sha256")
        _text(self.reader_backend, "reader_backend")
        _text(self.bffile_version, "bffile_version")
        _text(self.bioformats_maven_coordinate, "bioformats_maven_coordinate")
        _text(self.bioformats_runtime_version, "bioformats_runtime_version")
        if self.rasterization_policy != _MASK_POLICY:
            raise ValueError("unsupported rasterization policy")


@dataclass(frozen=True, slots=True)
class RoiSamplingResult:
    """Arithmetic complete-vector summary for one accepted Task-4 ROI."""

    roi_id: str
    roi_review_binding_sha256: str
    roi_selection_sha256: str
    reference_roi_export_sha256: str
    input_evidence_sha256: str
    header: SamplingHeaderBinding
    measurement_ids: tuple[str, ...]
    processing_chunk_ids: tuple[str, ...]
    sum_vector: tuple[float, ...]
    mean_vector: tuple[float, ...]
    accepted_vector_count: int
    attestation: Level0ReadAttestation
    sampling_evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the RoiSamplingResult fields when the record is constructed.

        The checks cover ``processing_chunk_ids``, ``roi_id``,
        ``roi_review_binding_sha256``, ``roi_selection_sha256``,
        ``reference_roi_export_sha256``, ``input_evidence_sha256``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _text(self.roi_id, "roi_id")
        _sha256(self.roi_review_binding_sha256, "roi_review_binding_sha256")
        _sha256(self.roi_selection_sha256, "roi_selection_sha256")
        _sha256(self.reference_roi_export_sha256, "reference_roi_export_sha256")
        _sha256(self.input_evidence_sha256, "input_evidence_sha256")
        if not isinstance(self.header, SamplingHeaderBinding):
            raise TypeError("header must be SamplingHeaderBinding")
        if type(self.attestation) is not Level0ReadAttestation:
            raise TypeError("attestation must be an exact Level0ReadAttestation")
        if type(self.measurement_ids) is not tuple or any(
            type(item) is not str for item in self.measurement_ids
        ):
            raise TypeError("measurement_ids must be a tuple of exact strings")
        if self.measurement_ids != self.attestation.measurement_ids:
            raise ValueError("result and attestation measurement identities differ")
        if (
            type(self.processing_chunk_ids) is not tuple
            or len(self.processing_chunk_ids) != self.attestation.processing_chunk_count
        ):
            raise ValueError("ROI processing chunk identities do not match attestation")
        if any(not isinstance(item, str) or not item for item in self.processing_chunk_ids):
            raise ValueError("ROI processing chunk identities must be nonempty text")
        chunk_digest = hashlib.sha256()
        for identity in self.processing_chunk_ids:
            chunk_digest.update(identity.encode("utf-8") + b"\0")
        if chunk_digest.hexdigest() != self.attestation.processing_chunk_inventory_sha256:
            raise ValueError("ROI processing chunk inventory SHA-256 differs")
        if self.header.source_file_sha256 != self.attestation.source_file_sha256:
            raise ValueError("result and attestation source identities differ")
        _positive_integer(self.accepted_vector_count, "accepted_vector_count")
        if self.accepted_vector_count != self.attestation.accepted_vector_count:
            raise ValueError("attestation accepted count differs from ROI result")
        if type(self.sum_vector) is not tuple:
            raise TypeError("sum_vector must be a tuple")
        if type(self.mean_vector) is not tuple:
            raise TypeError("mean_vector must be a tuple")
        if any(type(value) is not float for value in (*self.sum_vector, *self.mean_vector)):
            raise TypeError("ROI summary vectors must contain exact floats")
        if len(self.sum_vector) != len(self.measurement_ids) or len(self.mean_vector) != len(
            self.measurement_ids
        ):
            raise ValueError("ROI summary vectors do not match the measurement axis")
        if any(not math.isfinite(value) for value in (*self.sum_vector, *self.mean_vector)):
            raise ValueError("ROI summary vectors must be finite")
        if any(
            not math.isclose(
                mean,
                total / self.accepted_vector_count,
                rel_tol=1e-15,
                abs_tol=0.0,
            )
            for total, mean in zip(self.sum_vector, self.mean_vector, strict=True)
        ):
            raise ValueError("ROI mean vector must equal arithmetic sum divided by count")
        payload = {
            "roi_id": self.roi_id,
            "roi_review_binding_sha256": self.roi_review_binding_sha256,
            "roi_selection_sha256": self.roi_selection_sha256,
            "reference_roi_export_sha256": self.reference_roi_export_sha256,
            "input_evidence_sha256": self.input_evidence_sha256,
            "header": _record_mapping(self.header),
            "measurement_ids": list(self.measurement_ids),
            "processing_chunk_ids": list(self.processing_chunk_ids),
            "sum_vector": list(self.sum_vector),
            "mean_vector": list(self.mean_vector),
            "accepted_vector_count": self.accepted_vector_count,
            "attestation": _record_mapping(self.attestation),
        }
        object.__setattr__(
            self,
            "sampling_evidence_sha256",
            hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class TargetSamplingStratum:
    """One non-overlapping target-tissue polygon stratum in level-0 pixels."""

    stratum_id: str
    geometry_json: str
    geometry_sha256: str

    def __post_init__(self) -> None:
        _text(self.stratum_id, "stratum_id")
        _sha256(self.geometry_sha256, "geometry_sha256")
        try:
            geometry = json.loads(self.geometry_json)
        except json.JSONDecodeError as exc:
            raise ValueError("stratum geometry_json is invalid") from exc
        validated = _validated_geometry(geometry)
        if self.geometry_json != _canonical_bytes(geometry).decode("utf-8"):
            raise ValueError("stratum geometry_json must use canonical JSON")
        if hashlib.sha256(self.geometry_json.encode()).hexdigest() != self.geometry_sha256:
            raise ValueError("stratum geometry SHA-256 does not match")
        if not validated:
            raise ValueError("stratum geometry must be nonempty")

    @classmethod
    def create(cls, stratum_id: str, geometry_json: str) -> "TargetSamplingStratum":
        return cls(
            stratum_id=stratum_id,
            geometry_json=geometry_json,
            geometry_sha256=hashlib.sha256(geometry_json.encode()).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class TissueMaskBinding:
    """Target tissue-mask identity and its disjoint level-0 sampling strata."""

    source_image_sha256: str
    canonical_image_id: str
    width_px: int
    height_px: int
    source_mask_sha256: str
    strata: tuple[TargetSamplingStratum, ...]
    binding_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TissueMaskBinding fields when the record is constructed.

        The checks cover ``source_image_sha256``, ``canonical_image_id``, ``width_px``,
        ``height_px``, ``source_mask_sha256``, ``strata``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _sha256(self.source_image_sha256, "source_image_sha256")
        _text(self.canonical_image_id, "canonical_image_id")
        _positive_integer(self.width_px, "width_px")
        _positive_integer(self.height_px, "height_px")
        _sha256(self.source_mask_sha256, "source_mask_sha256")
        if type(self.strata) is not tuple or not self.strata:
            raise ValueError("strata must be a nonempty tuple")
        if any(not isinstance(item, TargetSamplingStratum) for item in self.strata):
            raise TypeError("strata must contain TargetSamplingStratum records")
        ids = tuple(item.stratum_id for item in self.strata)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate target sampling stratum")
        if ids != tuple(sorted(ids)):
            raise ValueError("target sampling strata must use canonical stratum order")
        geometries = tuple(_parse_geometry(item.geometry_json) for item in self.strata)
        for index, first in enumerate(geometries):
            for second in geometries[index + 1 :]:
                boundaries_touch = any(
                    _rings_touch(first_ring, second_ring)
                    for first_polygon in first
                    for second_polygon in second
                    for first_ring in first_polygon
                    for second_ring in second_polygon
                )
                if boundaries_touch or _geometries_intersect(first, second):
                    raise ValueError("target sampling strata overlap or touch")
        payload = {
            "source_image_sha256": self.source_image_sha256,
            "canonical_image_id": self.canonical_image_id,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "source_mask_sha256": self.source_mask_sha256,
            "strata": [_record_mapping(item) for item in self.strata],
        }
        object.__setattr__(
            self,
            "binding_sha256",
            hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        )

    @classmethod
    def create(
        cls,
        *,
        header: CanonicalOmeTiffHeader,
        source_mask_sha256: str,
        strata: tuple[TargetSamplingStratum, ...],
    ) -> "TissueMaskBinding":
        if not isinstance(header, CanonicalOmeTiffHeader):
            raise TypeError("header must be CanonicalOmeTiffHeader")
        return cls(
            source_image_sha256=header.sha256,
            canonical_image_id=header.image_id,
            width_px=header.size_x,
            height_px=header.size_y,
            source_mask_sha256=source_mask_sha256,
            strata=strata,
        )


def validate_tissue_mask_binding_authority(binding: TissueMaskBinding) -> None:
    """Rebuild a live tissue binding and every nested geometry authority."""
    if type(binding) is not TissueMaskBinding:
        raise TypeError("binding must be exact TissueMaskBinding")
    if type(binding.strata) is not tuple or any(
        type(item) is not TargetSamplingStratum for item in binding.strata
    ):
        raise TypeError("tissue mask strata must contain exact authority records")
    try:
        rebuilt_strata = tuple(
            TargetSamplingStratum(
                stratum_id=item.stratum_id,
                geometry_json=item.geometry_json,
                geometry_sha256=item.geometry_sha256,
            )
            for item in binding.strata
        )
        rebuilt = TissueMaskBinding(
            source_image_sha256=binding.source_image_sha256,
            canonical_image_id=binding.canonical_image_id,
            width_px=binding.width_px,
            height_px=binding.height_px,
            source_mask_sha256=binding.source_mask_sha256,
            strata=rebuilt_strata,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tissue mask binding geometry authority is stale") from exc
    if rebuilt != binding or rebuilt.binding_sha256 != binding.binding_sha256:
        raise ValueError("tissue mask binding authority differs from its live record")


def rasterize_tissue_strata(
    binding: TissueMaskBinding,
    region: Region,
) -> tuple[tuple[str, NDArray[np.bool_]], ...]:
    """Rasterize exact disjoint tissue strata for one bounded level-0 region."""
    if type(binding) is not TissueMaskBinding:
        raise TypeError("binding must be exact TissueMaskBinding")
    validate_tissue_mask_binding_authority(binding)
    if type(region) is not Region:
        raise TypeError("region must be exact Region")
    if region.x + region.width > binding.width_px or region.y + region.height > binding.height_px:
        raise ValueError("stratum rasterization region exceeds tissue-binding bounds")
    masks: list[tuple[str, NDArray[np.bool_]]] = []
    occupied = np.zeros((region.height, region.width), dtype=np.bool_)
    for stratum in binding.strata:
        mask = np.array(
            _geometry_mask(_parse_geometry(stratum.geometry_json), region),
            dtype=np.bool_,
            copy=True,
            order="C",
        )
        if np.any(occupied & mask):
            raise ValueError("rasterized tissue strata overlap")
        occupied |= mask
        mask.setflags(write=False)
        masks.append((stratum.stratum_id, mask))
    return tuple(masks)


@dataclass(frozen=True, slots=True)
class TargetSamplingPolicy:
    """Versioned deterministic cap for representative target vectors."""

    complete_vector_qc: CompleteVectorQcPolicy
    maximum_sample_count: int
    seed: int
    ranking_policy: str = _RANK_POLICY
    minimum_retained_eligible_vectors_per_stratum: int = 2
    stratum_minimum_policy: str = TARGET_SAMPLING_STRATUM_MINIMUM_POLICY_ID

    def __post_init__(self) -> None:
        if not isinstance(self.complete_vector_qc, CompleteVectorQcPolicy):
            raise TypeError("complete_vector_qc must be CompleteVectorQcPolicy")
        _positive_integer(self.maximum_sample_count, "maximum_sample_count")
        _nonnegative_integer(self.seed, "seed")
        if self.seed > _UINT64_MASK:
            raise ValueError("seed must fit in an unsigned 64-bit integer")
        if self.ranking_policy != _RANK_POLICY:
            raise ValueError("unsupported target sampling ranking policy")
        _stratum_minimum_policy(
            self.minimum_retained_eligible_vectors_per_stratum,
            self.stratum_minimum_policy,
        )


@dataclass(frozen=True, slots=True)
class TargetSamplingResult:
    """Bounded deterministic target vectors with exact spatial provenance."""

    header: SamplingHeaderBinding
    tissue_mask_binding_sha256: str
    input_evidence_sha256: str
    measurement_ids: tuple[str, ...]
    vectors: NDArray[np.float64]
    global_pixel_ids: tuple[int, ...]
    stratum_ids: tuple[str, ...]
    stratum_sample_counts: tuple[tuple[str, int], ...]
    requested_sample_count: int
    retained_sample_count: int
    eligible_vector_count: int
    all_eligible_vectors_used: bool
    seed: int
    ranking_policy: str
    attestation: Level0ReadAttestation
    minimum_retained_eligible_vectors_per_stratum: int = 2
    stratum_minimum_policy: str = TARGET_SAMPLING_STRATUM_MINIMUM_POLICY_ID
    sampling_evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetSamplingResult fields when the record is constructed.

        The checks cover ``tissue_mask_binding_sha256``, ``input_evidence_sha256``,
        ``requested_sample_count``, ``retained_sample_count``, ``eligible_vector_count``,
        ``measurement_ids``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        _sha256(self.tissue_mask_binding_sha256, "tissue_mask_binding_sha256")
        _sha256(self.input_evidence_sha256, "input_evidence_sha256")
        if not isinstance(self.header, SamplingHeaderBinding):
            raise TypeError("header must be SamplingHeaderBinding")
        if type(self.attestation) is not Level0ReadAttestation:
            raise TypeError("attestation must be an exact Level0ReadAttestation")
        if type(self.measurement_ids) is not tuple or any(
            type(item) is not str for item in self.measurement_ids
        ):
            raise TypeError("measurement_ids must be a tuple of exact strings")
        if type(self.global_pixel_ids) is not tuple:
            raise TypeError("global_pixel_ids must be a tuple")
        if type(self.stratum_ids) is not tuple or any(
            type(item) is not str for item in self.stratum_ids
        ):
            raise TypeError("stratum_ids must be a tuple of exact strings")
        if type(self.stratum_sample_counts) is not tuple:
            raise TypeError("stratum_sample_counts must be a tuple")
        if any(type(item) is not tuple or len(item) != 2 for item in self.stratum_sample_counts):
            raise TypeError("stratum_sample_counts must contain exact 2-tuples")
        _positive_integer(self.requested_sample_count, "requested_sample_count")
        _positive_integer(self.retained_sample_count, "retained_sample_count")
        _positive_integer(self.eligible_vector_count, "eligible_vector_count")
        if self.retained_sample_count > self.requested_sample_count:
            raise ValueError("retained target sample exceeds requested cap")
        if self.retained_sample_count > self.eligible_vector_count:
            raise ValueError("retained target sample exceeds eligible vectors")
        if type(self.all_eligible_vectors_used) is not bool or self.all_eligible_vectors_used != (
            self.retained_sample_count == self.eligible_vector_count
        ):
            raise ValueError("all_eligible_vectors_used contradicts retained counts")
        if self.measurement_ids != self.attestation.measurement_ids:
            raise ValueError("result and attestation measurement identities differ")
        if self.header.source_file_sha256 != self.attestation.source_file_sha256:
            raise ValueError("result and attestation source identities differ")
        if self.eligible_vector_count != self.attestation.accepted_vector_count:
            raise ValueError("attestation eligible count differs from target result")
        if (
            len(self.global_pixel_ids) != self.retained_sample_count
            or len(self.stratum_ids) != self.retained_sample_count
        ):
            raise ValueError("target sample identities do not match retained count")
        if len(self.global_pixel_ids) != len(set(self.global_pixel_ids)):
            raise ValueError("target sample contains duplicate global pixels")
        if any(
            isinstance(item, bool)
            or type(item) is not int
            or item < 0
            or item >= self.header.width_px * self.header.height_px
            for item in self.global_pixel_ids
        ):
            raise ValueError("target sample contains an invalid global pixel ID")
        if tuple(item[0] for item in self.stratum_sample_counts) != tuple(
            sorted(item[0] for item in self.stratum_sample_counts)
        ):
            raise ValueError("stratum sample counts must use canonical order")
        count_ids = tuple(item[0] for item in self.stratum_sample_counts)
        if len(count_ids) != len(set(count_ids)):
            raise ValueError("stratum sample count IDs must be unique")
        if any(
            type(identifier) is not str
            or not identifier
            or isinstance(count, bool)
            or type(count) is not int
            or count < 0
            for identifier, count in self.stratum_sample_counts
        ):
            raise ValueError("stratum sample counts require text IDs and nonnegative counts")
        if sum(item[1] for item in self.stratum_sample_counts) != self.retained_sample_count:
            raise ValueError("stratum sample counts do not match retained count")
        if tuple(sorted(self.stratum_ids)) != self.stratum_ids:
            raise ValueError("target sample columns must use canonical stratum order")
        observed_counts = {
            identifier: self.stratum_ids.count(identifier) for identifier in set(self.stratum_ids)
        }
        declared_counts = dict(self.stratum_sample_counts)
        if observed_counts != {
            identifier: count for identifier, count in declared_counts.items() if count > 0
        }:
            raise ValueError("stratum sample counts contradict stratum_ids")
        minimum = _stratum_minimum_policy(
            self.minimum_retained_eligible_vectors_per_stratum,
            self.stratum_minimum_policy,
        )
        minimum_violations = tuple(
            TargetSamplingStratumMinimumViolation(identifier, count, minimum)
            for identifier, count in self.stratum_sample_counts
            if 0 < count < minimum
        )
        if minimum_violations:
            raise TargetSamplingStratumMinimumError("qc_postflight", minimum_violations)
        _nonnegative_integer(self.seed, "seed")
        if self.ranking_policy != _RANK_POLICY:
            raise ValueError("unsupported target sampling ranking policy")
        vectors = np.array(self.vectors, dtype=np.float64, copy=True, order="C")
        if vectors.ndim != 2 or vectors.shape != (
            len(self.measurement_ids),
            self.retained_sample_count,
        ):
            raise ValueError("target vectors must have shape (measurements, samples)")
        if not np.all(np.isfinite(vectors)):
            raise ValueError("target vectors must be finite")
        vectors.setflags(write=False)
        object.__setattr__(self, "vectors", vectors)
        vector_sha = hashlib.sha256(vectors.astype("<f8", copy=False).tobytes()).hexdigest()
        object.__setattr__(
            self,
            "sampling_evidence_sha256",
            hashlib.sha256(
                _canonical_bytes(_target_sampling_authority_payload(self, vector_sha))
            ).hexdigest(),
        )


def _target_sampling_authority_payload(
    result: TargetSamplingResult,
    vectors_sha256: str,
) -> dict[str, object]:
    return {
        "header": _record_mapping(result.header),
        "tissue_mask_binding_sha256": result.tissue_mask_binding_sha256,
        "input_evidence_sha256": result.input_evidence_sha256,
        "measurement_ids": list(result.measurement_ids),
        "vectors_sha256": vectors_sha256,
        "global_pixel_ids": list(result.global_pixel_ids),
        "stratum_ids": list(result.stratum_ids),
        "stratum_sample_counts": [list(item) for item in result.stratum_sample_counts],
        "requested_sample_count": result.requested_sample_count,
        "retained_sample_count": result.retained_sample_count,
        "eligible_vector_count": result.eligible_vector_count,
        "all_eligible_vectors_used": result.all_eligible_vectors_used,
        "seed": result.seed,
        "ranking_policy": result.ranking_policy,
        "minimum_retained_eligible_vectors_per_stratum": (
            result.minimum_retained_eligible_vectors_per_stratum
        ),
        "stratum_minimum_policy": result.stratum_minimum_policy,
        "attestation": _record_mapping(result.attestation),
    }


def _validate_roi_sampling_result_authority(result: RoiSamplingResult) -> None:
    if type(result) is not RoiSamplingResult:
        raise TypeError("result must be an exact RoiSamplingResult")
    rebuilt = RoiSamplingResult(
        **{item.name: getattr(result, item.name) for item in fields(RoiSamplingResult) if item.init}
    )
    if rebuilt.sampling_evidence_sha256 != result.sampling_evidence_sha256:
        raise SamplingError("Task-5 sampling evidence SHA-256 is stale")


def validate_target_sampling_result_authority(result: TargetSamplingResult) -> None:
    """Revalidate live target vectors and all derived evidence."""

    if type(result) is not TargetSamplingResult:
        raise TypeError("result must be an exact TargetSamplingResult")
    if (
        result.vectors.dtype != np.dtype(np.float64)
        or not result.vectors.flags.c_contiguous
        or not result.vectors.flags.owndata
        or result.vectors.flags.writeable
    ):
        raise SamplingError(
            "authoritative target sampling vectors must remain owned C-contiguous "
            "read-only float64 storage"
        )
    rebuilt = TargetSamplingResult(
        **{
            item.name: getattr(result, item.name)
            for item in fields(TargetSamplingResult)
            if item.init
        }
    )
    if rebuilt.sampling_evidence_sha256 != result.sampling_evidence_sha256:
        raise SamplingError("target sampling evidence SHA-256 is stale (authority digest mismatch)")


@dataclass(slots=True)
class _ReadLedger:
    chunk_hash: object = field(default_factory=hashlib.sha256)
    chunk_count: int = 0
    read_count: int = 0
    maximum_read_pixels: int = 0
    chunk_ids: list[str] = field(default_factory=list)

    def add_chunk(self, identity: str, region: Region, measurement_count: int) -> None:
        self.chunk_hash.update(identity.encode("utf-8") + b"\0")
        self.chunk_ids.append(identity)
        self.chunk_count += 1
        self.read_count += measurement_count
        self.maximum_read_pixels = max(
            self.maximum_read_pixels,
            region.width * region.height,
        )


@dataclass(slots=True)
class _Reduction:
    total: NDArray[np.float64]
    candidate_count: int
    accepted_count: int
    rejected: dict[str, int]


@dataclass(slots=True)
class _PriorityReservoir:
    capacity: int
    salt: int
    measurement_count: int
    ranks: NDArray[np.uint64] = field(init=False)
    global_ids: NDArray[np.uint64] = field(init=False)
    vectors: NDArray[np.float64] = field(init=False)

    def __post_init__(self) -> None:
        self.ranks = np.empty(0, dtype=np.uint64)
        self.global_ids = np.empty(0, dtype=np.uint64)
        self.vectors = np.empty((self.measurement_count, 0), dtype=np.float64)

    def consider(
        self,
        global_ids: NDArray[np.uint64],
        vectors: NDArray[np.float64],
    ) -> None:
        if self.capacity == 0 or global_ids.size == 0:
            return
        ranks = _pixel_ranks(self.salt, global_ids)
        combined_ranks = np.concatenate((self.ranks, ranks))
        combined_ids = np.concatenate((self.global_ids, global_ids))
        combined_vectors = np.concatenate((self.vectors, vectors), axis=1)
        if combined_ranks.size > self.capacity:
            partition = np.argpartition(combined_ranks, self.capacity - 1)
            threshold = combined_ranks[partition[self.capacity - 1]]
            lower = np.flatnonzero(combined_ranks < threshold)
            equal = np.flatnonzero(combined_ranks == threshold)
            needed = self.capacity - lower.size
            equal_order = np.argsort(combined_ids[equal], kind="stable")[:needed]
            selected = np.concatenate((lower, equal[equal_order]))
        else:
            selected = np.arange(combined_ranks.size)
        order = np.lexsort((combined_ids[selected], combined_ranks[selected]))
        selected = selected[order]
        self.ranks = combined_ranks[selected]
        self.global_ids = combined_ids[selected]
        self.vectors = combined_vectors[:, selected]


def sample_reference_roi(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    input_evidence: ReferenceInputEvidence,
    measurement_planes: tuple[MeasurementPlane, ...],
    roi_selection: AcceptedReferenceRoiSelection,
    policy: CompleteVectorQcPolicy,
    *,
    artifact_mask: ArtifactMaskSource | None = None,
) -> RoiSamplingResult:
    """Summarize one reviewed polygon through bounded level-0 complete vectors."""
    if type(facts) is not SourceImageFacts:
        raise TypeError("facts must be exact SourceImageFacts")
    if type(roi_selection) is not AcceptedReferenceRoiSelection:
        raise TypeError("roi_selection must be an exact AcceptedReferenceRoiSelection")
    roi = roi_selection.roi
    if not isinstance(input_evidence, ReferenceInputEvidence):
        raise TypeError("input_evidence must be ReferenceInputEvidence")
    if roi.source_file_sha256 != facts.identity.file_sha256:
        raise SamplingError("ROI source-image SHA-256 does not match the reviewed source")
    if roi.canonical_image_id != facts.image_id:
        raise SamplingError("ROI canonical image identity does not match the reviewed source")
    _validate_roi_input_binding(roi_selection, input_evidence, facts)
    transforms, artifact_mask_sha256, metadata_snapshot = _preflight(
        source,
        facts,
        input_evidence,
        measurement_planes,
        policy,
        artifact_mask,
    )
    geometry = _parse_geometry(roi.geometry_json)
    ledger = _ReadLedger()
    reduction = _reduce_geometry(
        source,
        facts,
        measurement_planes,
        transforms,
        geometry,
        policy,
        ledger,
        artifact_mask=artifact_mask,
        chunk_prefix=f"roi:{roi.roi_id}",
    )
    if reduction.accepted_count == 0:
        raise SamplingError("ROI contains no complete vectors accepted by QC")
    _postflight(source, facts, metadata_snapshot, artifact_mask, artifact_mask_sha256)
    mean = reduction.total / reduction.accepted_count
    attestation = _attestation(
        source,
        facts,
        measurement_planes,
        policy,
        ledger,
        reduction,
        artifact_mask_sha256,
        input_evidence.quantitative_signature.normalization_policy,
    )
    return RoiSamplingResult(
        roi_id=roi.roi_id,
        roi_review_binding_sha256=roi.review_binding_sha256,
        roi_selection_sha256=roi_selection.selection_sha256,
        reference_roi_export_sha256=roi_selection.export.content_sha256,
        input_evidence_sha256=_input_evidence_digest(input_evidence),
        header=SamplingHeaderBinding.from_source_facts(facts),
        measurement_ids=tuple(item.measurement_id for item in measurement_planes),
        processing_chunk_ids=tuple(ledger.chunk_ids),
        sum_vector=tuple(float(item) for item in reduction.total),
        mean_vector=tuple(float(item) for item in mean),
        accepted_vector_count=reduction.accepted_count,
        attestation=attestation,
    )


def sample_target_vectors(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    input_evidence: ReferenceInputEvidence,
    measurement_planes: tuple[MeasurementPlane, ...],
    tissue_domain: TissueMaskBinding,
    policy: TargetSamplingPolicy,
    *,
    artifact_mask: ArtifactMaskSource | None = None,
) -> TargetSamplingResult:
    """Return a deterministic bounded representative level-0 target sample."""
    if type(facts) is not SourceImageFacts:
        raise TypeError("facts must be exact SourceImageFacts")
    if not isinstance(tissue_domain, TissueMaskBinding):
        raise TypeError("tissue_domain must be TissueMaskBinding")
    if not isinstance(policy, TargetSamplingPolicy):
        raise TypeError("policy must be TargetSamplingPolicy")
    if (
        tissue_domain.source_image_sha256 != facts.identity.file_sha256
        or tissue_domain.canonical_image_id != facts.image_id
        or tissue_domain.width_px != facts.size_x
        or tissue_domain.height_px != facts.size_y
    ):
        raise SamplingError("tissue mask does not bind the exact reviewed target image")
    if input_evidence.input_role != "target_image":
        raise SamplingError("target sampling requires target_image input evidence")
    qc_policy = policy.complete_vector_qc
    transforms, artifact_mask_sha256, metadata_snapshot = _preflight(
        source,
        facts,
        input_evidence,
        measurement_planes,
        qc_policy,
        artifact_mask,
    )
    geometries = tuple(_parse_geometry(item.geometry_json) for item in tissue_domain.strata)
    raster_counts = tuple(
        _geometry_pixel_count(geometry, facts, qc_policy.processing_chunk_shape)
        for geometry in geometries
    )
    minimum = policy.minimum_retained_eligible_vectors_per_stratum
    raster_violations = tuple(
        TargetSamplingStratumMinimumViolation(
            stratum.stratum_id,
            raster_count,
            minimum,
        )
        for stratum, raster_count in zip(tissue_domain.strata, raster_counts, strict=True)
        if 0 < raster_count < minimum
    )
    if raster_violations:
        raise TargetSamplingStratumMinimumError("raster_preflight", raster_violations)
    quotas = _largest_remainder_quotas(
        raster_counts,
        policy.maximum_sample_count,
        minimum_per_nonempty_stratum=minimum,
    )
    ledger = _ReadLedger()
    aggregate = _Reduction(
        total=np.zeros(len(measurement_planes), dtype=np.float64),
        candidate_count=0,
        accepted_count=0,
        rejected={reason: 0 for reason in _REJECTION_ORDER},
    )
    retained_vectors: list[NDArray[np.float64]] = []
    retained_global_ids: list[NDArray[np.uint64]] = []
    retained_stratum_ids: list[str] = []
    stratum_counts: list[tuple[str, int]] = []
    for stratum, geometry, raster_count, quota in zip(
        tissue_domain.strata, geometries, raster_counts, quotas, strict=True
    ):
        reservoir = _PriorityReservoir(
            capacity=quota,
            salt=_target_rank_salt(
                seed=policy.seed,
                source_file_sha256=facts.identity.file_sha256,
                mask_sha256=tissue_domain.binding_sha256,
                stratum_id=stratum.stratum_id,
            ),
            measurement_count=len(measurement_planes),
        )

        if raster_count == 0:
            retained_vectors.append(reservoir.vectors)
            retained_global_ids.append(reservoir.global_ids)
            stratum_counts.append((stratum.stratum_id, 0))
            continue

        reduction = _reduce_geometry(
            source,
            facts,
            measurement_planes,
            transforms,
            geometry,
            qc_policy,
            ledger,
            artifact_mask=artifact_mask,
            chunk_prefix=f"stratum:{stratum.stratum_id}",
            retain=reservoir.consider,
        )
        aggregate.total += reduction.total
        aggregate.candidate_count += reduction.candidate_count
        aggregate.accepted_count += reduction.accepted_count
        for reason in _REJECTION_ORDER:
            aggregate.rejected[reason] += reduction.rejected[reason]
        retained_vectors.append(reservoir.vectors)
        retained_global_ids.append(reservoir.global_ids)
        retained_stratum_ids.extend([stratum.stratum_id] * reservoir.global_ids.size)
        stratum_counts.append((stratum.stratum_id, int(reservoir.global_ids.size)))
    retained_count = sum(item.size for item in retained_global_ids)
    _postflight(source, facts, metadata_snapshot, artifact_mask, artifact_mask_sha256)
    retained_violations = tuple(
        TargetSamplingStratumMinimumViolation(stratum_id, count, minimum)
        for (stratum_id, count), raster_count in zip(stratum_counts, raster_counts, strict=True)
        if raster_count > 0 and count < minimum
    )
    if retained_violations:
        raise TargetSamplingStratumMinimumError("qc_postflight", retained_violations)
    vectors = np.concatenate(retained_vectors, axis=1)
    global_ids = np.concatenate(retained_global_ids)
    attestation = _attestation(
        source,
        facts,
        measurement_planes,
        qc_policy,
        ledger,
        aggregate,
        artifact_mask_sha256,
        input_evidence.quantitative_signature.normalization_policy,
    )
    return TargetSamplingResult(
        header=SamplingHeaderBinding.from_source_facts(facts),
        tissue_mask_binding_sha256=tissue_domain.binding_sha256,
        input_evidence_sha256=_input_evidence_digest(input_evidence),
        measurement_ids=tuple(item.measurement_id for item in measurement_planes),
        vectors=vectors,
        global_pixel_ids=tuple(int(item) for item in global_ids.tolist()),
        stratum_ids=tuple(retained_stratum_ids),
        stratum_sample_counts=tuple(stratum_counts),
        requested_sample_count=policy.maximum_sample_count,
        retained_sample_count=retained_count,
        eligible_vector_count=aggregate.accepted_count,
        all_eligible_vectors_used=retained_count == aggregate.accepted_count,
        seed=policy.seed,
        ranking_policy=policy.ranking_policy,
        attestation=attestation,
        minimum_retained_eligible_vectors_per_stratum=minimum,
        stratum_minimum_policy=policy.stratum_minimum_policy,
    )


def _preflight(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    input_evidence: ReferenceInputEvidence,
    measurement_planes: tuple[MeasurementPlane, ...],
    policy: CompleteVectorQcPolicy,
    artifact_mask: ArtifactMaskSource | None,
) -> tuple[tuple[QuantitativeMeasurementTransform, ...], str | None, OME]:
    if type(facts) is not SourceImageFacts:
        raise TypeError("facts must be exact SourceImageFacts")
    if not isinstance(input_evidence, ReferenceInputEvidence):
        raise TypeError("input_evidence must be ReferenceInputEvidence")
    if not isinstance(policy, CompleteVectorQcPolicy):
        raise TypeError("policy must be CompleteVectorQcPolicy")
    if type(measurement_planes) is not tuple or not measurement_planes:
        raise TypeError("measurement_planes must be a nonempty tuple")
    if any(not isinstance(item, MeasurementPlane) for item in measurement_planes):
        raise TypeError("measurement_planes must contain MeasurementPlane records")
    artifact_mask_sha256 = None
    if artifact_mask is not None:
        if not isinstance(artifact_mask, ArtifactMaskSource):
            raise TypeError("artifact_mask must implement ArtifactMaskSource")
        artifact_mask_sha256 = _sha256(
            artifact_mask.mask_sha256,
            "artifact mask SHA-256",
        )
    _validate_opened_source_identity(source, facts, phase="before sampling")
    try:
        live_facts = resolve_source_image_facts(source)
    except SourceFactsError as exc:
        raise SamplingError(
            "reviewed source facts could not be revalidated before sampling"
        ) from exc
    if live_facts != facts:
        raise SamplingError("reviewed source facts changed before sampling")
    metadata_snapshot = source.metadata.model_copy(deep=True)
    if facts.size_z != 1 or facts.size_t != 1:
        raise SamplingError("sampling requires SizeZ=1 and SizeT=1")
    channels = metadata_snapshot.images[0].pixels.channels
    channel_ids = tuple(channel.id for channel in channels)
    if (
        len(channel_ids) != facts.size_c
        or any(not channel_id for channel_id in channel_ids)
        or len(set(channel_ids)) != len(channel_ids)
    ):
        raise SamplingError("reviewed source requires a complete unique channel order")
    previous_size = (facts.size_x, facts.size_y)
    for index, level in enumerate(facts.native_resolutions):
        if (
            level.index != index
            or level.size_z != 1
            or level.size_t != 1
            or level.size_c != facts.size_c
            or level.rgb_samples != 1
            or level.dtype != np.dtype(facts.container_dtype)
            or level.dimension_order != facts.dimension_order
            or level.size_x > previous_size[0]
            or level.size_y > previous_size[1]
        ):
            raise SamplingError("reviewed source native resolution topology differs")
        previous_size = (level.size_x, level.size_y)
    if input_evidence.source_image_sha256 != facts.identity.file_sha256:
        raise SamplingError("input evidence source-image SHA-256 does not match reviewed source")
    if not input_evidence.structurally_compatible:
        raise SamplingError("input evidence is not structurally compatible")
    if not input_evidence.quantitatively_transformable:
        raise SamplingError("input evidence is not quantitatively transformable")
    measurement_ids = tuple(item.measurement_id for item in measurement_planes)
    if measurement_ids != input_evidence.axis.measurement_ids:
        raise SamplingError("measurement plane order does not match the spectral axis")
    if len({item.channel_index for item in measurement_planes}) != len(measurement_planes):
        raise SamplingError("measurement planes contain duplicate channel indices")
    if len({item.channel_id for item in measurement_planes}) != len(measurement_planes):
        raise SamplingError("measurement planes contain duplicate channel identities")
    pixels = source.metadata.images[0].pixels
    for plane in measurement_planes:
        if plane.channel_index >= facts.size_c:
            raise SamplingError("measurement channel index exceeds canonical SizeC")
        if pixels.channels[plane.channel_index].id != plane.channel_id:
            raise SamplingError("measurement channel identity changed")
    transforms = input_evidence.quantitative_signature.measurements
    if tuple(item.measurement_id for item in transforms) != measurement_ids:
        raise SamplingError("quantitative transform measurement order changed")
    _validate_transform_ranges(facts, transforms)
    return transforms, artifact_mask_sha256, metadata_snapshot


def _postflight(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    metadata_snapshot: OME,
    artifact_mask: ArtifactMaskSource | None,
    artifact_mask_sha256: str | None,
) -> None:
    _validate_opened_source_identity(source, facts, phase="during sampling")
    try:
        live_facts = resolve_source_image_facts(source)
    except SourceFactsError as exc:
        raise SamplingError("reviewed source changed during sampling") from exc
    if live_facts != facts or source.metadata != metadata_snapshot:
        raise SamplingError("reviewed source changed during sampling")
    if artifact_mask is not None and artifact_mask.mask_sha256 != artifact_mask_sha256:
        raise SamplingError("artifact mask identity changed during sampling")


def _validate_opened_source_identity(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    *,
    phase: str,
) -> None:
    try:
        identity = source.identity
    except Exception as exc:
        raise SamplingError(f"opened source identity is unavailable {phase}") from exc
    if not isinstance(identity, OpenedSourceIdentity):
        raise SamplingError(f"opened source identity is not IO-owned {phase}")
    expected_path = facts.identity.resolved_path
    if identity.resolved_path != expected_path or identity != facts.identity:
        raise SamplingError(f"opened source identity does not match reviewed source facts {phase}")
    try:
        live_identity = snapshot_opened_source_identity(expected_path, retained=identity)
    except Exception as exc:
        raise SamplingError(f"opened source identity could not be revalidated {phase}") from exc
    if live_identity != identity:
        raise SamplingError(f"opened source identity changed {phase}")


def _validate_roi_input_binding(
    selection: AcceptedReferenceRoiSelection,
    input_evidence: ReferenceInputEvidence,
    facts: SourceImageFacts,
) -> None:
    roi = selection.roi
    image = selection.entry.image
    if (
        image.reference_input_id != input_evidence.input_id
        or image.source_file_sha256 != facts.identity.file_sha256
        or image.canonical_image_id != facts.image_id
        or image.canonical_pixels_id != facts.pixels_id
        or image.source_path.resolve() != facts.identity.resolved_path
        or image.width_px != facts.size_x
        or image.height_px != facts.size_y
    ):
        raise SamplingError("Task-4 ROI selection does not match the exact reviewed input")
    if roi.roi_role == "positive":
        accepted = (
            image.source_role == "single_component_reference"
            and input_evidence.input_role == "single_component_reference"
            and image.source_link_id == input_evidence.fluorophore_identity
        )
    elif roi.roi_role == "negative":
        accepted = (
            image.source_role == "negative_control"
            and input_evidence.input_role == "single_stain_negative_control"
            and image.source_link_id == input_evidence.fluorophore_identity
        )
    elif roi.roi_role == "af":
        accepted = (
            image.source_role == "unstained_control"
            and input_evidence.input_role
            in {
                "unstained_control",
                "target_unstained_control",
            }
        ) or (
            image.source_role == "negative_control"
            and input_evidence.input_role == "single_stain_negative_control"
        )
    else:
        accepted = roi.role_link_id == input_evidence.input_id
    if not accepted:
        raise SamplingError("ROI role/link does not match its exact reference input evidence")


def _validate_transform_ranges(
    facts: SourceImageFacts,
    transforms: tuple[QuantitativeMeasurementTransform, ...],
) -> None:
    if transforms and all(item.correction_mode == "as_stored" for item in transforms):
        return  # Stored normalized units have no reconstructible raw-count range.
    if any(item.correction_mode == "as_stored" for item in transforms):
        raise SamplingError("Reference channels must use one correction mode")
    if any(item.intensity_representation == "dark_corrected_counts" and item.offset is None for item in transforms):
        raise SamplingError(
            "Dark-corrected reference saturation range is unavailable: the original "
            "detector offset is needed to reconstruct pre-subtraction counts for reference QC. "
            "Use a reference with that recorded offset or an original count export."
        )
    ordinary = tuple(
        item
        for item in transforms
        if item.digital_range_authority is not DigitalRangeAuthority.EXPLICIT_PROFILE
    )
    if ordinary:
        for item in ordinary:
            storage_authority = (
                item.digital_range_authority
                if item.digital_range_authority
                in {
                    DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE,
                    DigitalRangeAuthority.SOURCE_STORAGE_BITS_PER_SAMPLE,
                }
                else None
            )
            resolved = resolve_effective_digital_range(facts, storage_authority=storage_authority)
            if (
                item.effective_digital_maximum != resolved.maximum
                or item.digital_range_authority is not resolved.authority
                or item.digital_range_meaningful_bits != resolved.meaningful_bits
            ):
                raise SamplingError("quantitative digital range conflicts with reviewed source")
    container_dtype = np.dtype(facts.container_dtype)
    if np.issubdtype(container_dtype, np.unsignedinteger):
        container_max = float((1 << facts.container_bits) - 1)
        if any(item.effective_digital_maximum > container_max for item in transforms):
            raise SamplingError("quantitative digital maximum exceeds the source container")
    with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
        divisors = np.asarray(
            [
                item.exposure_seconds if item.correction_mode == "exposure_only" else
                item.effective_digital_maximum * item.gain * item.binning_area * item.exposure_seconds
                for item in transforms
            ],
            dtype=np.float64,
        )
        responses = np.asarray(
            [item.response_correction_multiplier for item in transforms],
            dtype=np.float64,
        )
        maximum_magnitudes = np.asarray(
            [item.effective_digital_maximum + (abs(item.offset) if item.intensity_representation == "dark_corrected_counts" or item.correction_mode == "full" else 0) for item in transforms],
            dtype=np.float64,
        )
        normalized_bounds = maximum_magnitudes / divisors * responses
    if np.any(~np.isfinite(divisors)) or np.any(divisors <= 0.0):
        raise SamplingError("normalization divisor must remain finite and positive in float64")
    if np.any(~np.isfinite(normalized_bounds)):
        raise SamplingError("normalized measurement range must remain finite in float64")


def _parse_geometry(geometry_json: str):
    try:
        value = json.loads(geometry_json)
    except json.JSONDecodeError as exc:
        raise SamplingError("geometry JSON is invalid") from exc
    return _validated_geometry(value)


def _geometry_bounds(
    geometry,
    facts: SourceImageFacts,
    *,
    allow_empty: bool = False,
) -> tuple[int, int, int, int] | None:
    points = tuple(point for polygon in geometry for ring in polygon for point in ring[:-1])
    min_x = max(0, math.ceil(min(point[0] for point in points) - 0.5))
    min_y = max(0, math.ceil(min(point[1] for point in points) - 0.5))
    max_x = min(facts.size_x, math.floor(max(point[0] for point in points) - 0.5) + 1)
    max_y = min(facts.size_y, math.floor(max(point[1] for point in points) - 0.5) + 1)
    if max_x <= min_x or max_y <= min_y:
        if allow_empty:
            return None
        raise SamplingError("geometry contains no level-0 pixel centers inside the image")
    return min_x, min_y, max_x, max_y


def _regions(bounds: tuple[int, int, int, int], shape: ProcessingChunkShape):
    min_x, min_y, max_x, max_y = bounds
    for y in range(min_y, max_y, shape.height):
        height = min(shape.height, max_y - y)
        for x in range(min_x, max_x, shape.width):
            yield Region(x=x, y=y, width=min(shape.width, max_x - x), height=height)


def _ring_relations(xs: NDArray[np.float64], ys: NDArray[np.float64], ring):
    boundary = np.zeros(xs.shape, dtype=np.bool_)
    inside = np.zeros(xs.shape, dtype=np.bool_)
    for first, second in zip(ring[:-1], ring[1:], strict=True):
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        cross = dx * (ys - first[1]) - dy * (xs - first[0])
        cross_scale = (
            abs(dx) * (np.abs(ys) + abs(first[1])) + abs(dy) * (np.abs(xs) + abs(first[0])) + 1.0
        )
        cross_tolerance = 16.0 * np.finfo(np.float64).eps * cross_scale
        boundary |= (
            (np.abs(cross) <= cross_tolerance)
            & (xs >= min(first[0], second[0]))
            & (xs <= max(first[0], second[0]))
            & (ys >= min(first[1], second[1]))
            & (ys <= max(first[1], second[1]))
        )
        crosses = (first[1] > ys) != (second[1] > ys)
        if dy != 0.0:
            crossing_x = first[0] + (ys - first[1]) * dx / dy
            inside ^= crosses & (crossing_x > xs)
    return inside, boundary


def _geometry_mask(geometry, region: Region) -> NDArray[np.bool_]:
    x_values = np.arange(region.x, region.x + region.width, dtype=np.float64) + 0.5
    y_values = np.arange(region.y, region.y + region.height, dtype=np.float64) + 0.5
    xs, ys = np.meshgrid(x_values, y_values)
    result = np.zeros((region.height, region.width), dtype=np.bool_)
    for polygon in geometry:
        shell_inside, shell_boundary = _ring_relations(xs, ys, polygon[0])
        included = shell_inside | shell_boundary
        for hole in polygon[1:]:
            hole_inside, hole_boundary = _ring_relations(xs, ys, hole)
            included &= ~(hole_inside & ~hole_boundary)
        result |= included
    return result


def _read_vectors(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    measurement_planes: tuple[MeasurementPlane, ...],
    region: Region,
) -> NDArray[np.float64]:
    planes: list[NDArray[np.generic]] = []
    for measurement in measurement_planes:
        plane = np.asarray(
            source.read_region(
                series=0,
                resolution=0,
                plane=PlaneIndex(c=measurement.channel_index),
                region=region,
            )
        )
        if plane.shape != (region.height, region.width):
            raise SamplingError("Bio-Formats regional read returned an unexpected shape")
        if plane.dtype != np.dtype(facts.container_dtype):
            raise SamplingError("Bio-Formats regional read returned an unexpected dtype")
        planes.append(plane)
    return np.asarray(np.stack(planes), dtype=np.float64)


def _normalize(
    raw: NDArray[np.float64],
    transforms: tuple[QuantitativeMeasurementTransform, ...],
) -> NDArray[np.float64]:
    if transforms and all(item.correction_mode == "as_stored" for item in transforms):
        return raw.copy()
    if transforms and all(item.correction_mode == "exposure_only" for item in transforms):
        return raw / np.asarray([item.exposure_seconds for item in transforms], dtype=np.float64)[:, None, None]
    if any(item.correction_mode != "full" for item in transforms):
        raise SamplingError("Reference channels must use one correction mode")
    offsets = np.asarray([item.applied_offset for item in transforms], dtype=np.float64)
    divisors = np.asarray(
        [
            item.effective_digital_maximum * item.gain * item.binning_area * item.exposure_seconds
            for item in transforms
        ],
        dtype=np.float64,
    )
    response = np.asarray(
        [item.response_correction_multiplier for item in transforms],
        dtype=np.float64,
    )
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        normalized = (raw - offsets[:, None, None]) / divisors[:, None, None]
        normalized *= response[:, None, None]
    return normalized


def _classified_vectors(
    raw: NDArray[np.float64],
    normalized: NDArray[np.float64],
    geometry_mask: NDArray[np.bool_],
    artifact_mask: NDArray[np.bool_],
    transforms: tuple[QuantitativeMeasurementTransform, ...],
    policy: CompleteVectorQcPolicy,
) -> tuple[NDArray[np.bool_], dict[str, int]]:
    remaining = geometry_mask.copy()
    rejected: dict[str, int] = {}
    artifact = remaining & artifact_mask
    rejected["artifact_mask"] = int(np.count_nonzero(artifact))
    remaining &= ~artifact
    nonfinite = remaining & ~np.all(np.isfinite(raw) & np.isfinite(normalized), axis=0)
    rejected["nonfinite"] = int(np.count_nonzero(nonfinite))
    remaining &= ~nonfinite
    if transforms and all(item.correction_mode == "as_stored" for item in transforms):
        # These checks concern acquisition counts, not calibrated linear values.
        # Their non-applicability is recorded explicitly in the read attestation.
        rejected["raw_out_of_range"] = 0
        rejected["saturated"] = 0
    else:
        maxima = np.asarray([item.effective_digital_maximum for item in transforms], dtype=np.float64)[
            :, None, None
        ]
        # Range and saturation describe the counts before dark subtraction. Restore
        # the recorded baseline only for this QC comparison; fitting retains negative
        # dark-corrected noise and never subtracts the detector offset a second time.
        if any(item.intensity_representation == "dark_corrected_counts" and item.offset is None for item in transforms):
            raise SamplingError("Dark-corrected reference saturation range needs the original detector offset")
        acquired_counts = raw
        if any(item.intensity_representation == "dark_corrected_counts" for item in transforms):
            qc_offsets = np.asarray([
                item.offset if item.intensity_representation == "dark_corrected_counts" else 0.0
                for item in transforms
            ], dtype=np.float64)[:, None, None]
            acquired_counts = raw + qc_offsets
        out_of_range = remaining & np.any((acquired_counts < 0.0) | (acquired_counts > maxima), axis=0)
        rejected["raw_out_of_range"] = int(np.count_nonzero(out_of_range))
        remaining &= ~out_of_range
        saturated = remaining & np.any(acquired_counts >= maxima * policy.saturation_fraction, axis=0)
        rejected["saturated"] = int(np.count_nonzero(saturated))
        remaining &= ~saturated
    absolute = np.abs(normalized)
    scales = np.max(absolute, axis=0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        scaled = np.divide(
            absolute,
            scales[None, :, :],
            out=np.zeros_like(absolute),
            where=scales[None, :, :] > 0.0,
        )
        norms = scales * np.sqrt(np.sum(scaled * scaled, axis=0, dtype=np.float64))
    near_zero = remaining & (norms <= policy.near_zero_l2_threshold)
    rejected["near_zero"] = int(np.count_nonzero(near_zero))
    remaining &= ~near_zero
    return remaining, rejected


def _reduce_geometry(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    measurement_planes: tuple[MeasurementPlane, ...],
    transforms: tuple[QuantitativeMeasurementTransform, ...],
    geometry,
    policy: CompleteVectorQcPolicy,
    ledger: _ReadLedger,
    *,
    artifact_mask: ArtifactMaskSource | None,
    chunk_prefix: str,
    retain=None,
) -> _Reduction:
    reduction = _Reduction(
        total=np.zeros(len(measurement_planes), dtype=np.float64),
        candidate_count=0,
        accepted_count=0,
        rejected={reason: 0 for reason in _REJECTION_ORDER},
    )
    bounds = _geometry_bounds(geometry, facts)
    for region in _regions(bounds, policy.processing_chunk_shape):
        local_geometry = _geometry_mask(geometry, region)
        candidate_count = int(np.count_nonzero(local_geometry))
        if candidate_count == 0:
            continue
        chunk_identity = (
            f"{chunk_prefix}:x={region.x}:y={region.y}:" f"w={region.width}:h={region.height}"
        )
        raw = _read_vectors(source, facts, measurement_planes, region)
        ledger.add_chunk(chunk_identity, region, len(measurement_planes))
        normalized = _normalize(raw, transforms)
        local_artifact = (
            np.zeros(local_geometry.shape, dtype=np.bool_)
            if artifact_mask is None
            else np.asarray(artifact_mask.read_region(region))
        )
        if local_artifact.shape != local_geometry.shape or local_artifact.dtype != np.bool_:
            raise SamplingError("artifact mask regional read must return a boolean chunk")
        accepted, rejected = _classified_vectors(
            raw,
            normalized,
            local_geometry,
            local_artifact,
            transforms,
            policy,
        )
        selected = normalized[:, accepted]
        reduction.total += np.sum(selected, axis=1, dtype=np.float64)
        accepted_count = selected.shape[1]
        reduction.candidate_count += candidate_count
        reduction.accepted_count += accepted_count
        for reason in _REJECTION_ORDER:
            reduction.rejected[reason] += rejected[reason]
        if retain is not None and accepted_count:
            local_y, local_x = np.nonzero(accepted)
            global_ids = (local_y + region.y) * facts.size_x + (local_x + region.x)
            retain(np.asarray(global_ids, dtype=np.uint64), selected)
    return reduction


def _geometry_pixel_count(
    geometry,
    facts: SourceImageFacts,
    shape: ProcessingChunkShape,
) -> int:
    bounds = _geometry_bounds(geometry, facts, allow_empty=True)
    if bounds is None:
        return 0
    return sum(
        int(np.count_nonzero(_geometry_mask(geometry, region)))
        for region in _regions(bounds, shape)
    )


def _largest_remainder_quotas(
    counts: tuple[int, ...],
    maximum: int,
    *,
    minimum_per_nonempty_stratum: int,
) -> tuple[int, ...]:
    total = sum(counts)
    if total <= 0:
        raise SamplingError("target tissue strata contain no level-0 pixel centers")
    requested = min(total, maximum)
    nonempty = tuple(index for index, count in enumerate(counts) if count > 0)
    required = len(nonempty) * minimum_per_nonempty_stratum
    if maximum < required:
        raise TargetSamplingCapacityError(
            maximum_sample_count=maximum,
            represented_stratum_count=len(nonempty),
            minimum_per_stratum=minimum_per_nonempty_stratum,
        )
    quotas = [0] * len(counts)
    for index in nonempty:
        quotas[index] = minimum_per_nonempty_stratum
    remaining = requested - required
    weights = tuple(max(0, count - quotas[index]) for index, count in enumerate(counts))
    weight_total = sum(weights)
    if remaining == 0 or weight_total == 0:
        return tuple(quotas)
    quotients_and_remainders = tuple(divmod(remaining * weight, weight_total) for weight in weights)
    additions = [item[0] for item in quotients_and_remainders]
    for index, addition in enumerate(additions):
        quotas[index] += addition
    remainder = remaining - sum(additions)
    order = sorted(
        range(len(counts)),
        key=lambda index: (-quotients_and_remainders[index][1], index),
    )
    for index in order[:remainder]:
        quotas[index] += 1
    return tuple(quotas)


def _target_rank_salt(
    *,
    seed: int,
    source_file_sha256: str,
    mask_sha256: str,
    stratum_id: str,
) -> int:
    identity = _canonical_bytes([seed, source_file_sha256, mask_sha256, stratum_id])
    return int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")


def _pixel_ranks(
    salt: int,
    global_pixel_ids: NDArray[np.uint64],
) -> NDArray[np.uint64]:
    values = np.asarray(global_pixel_ids, dtype=np.uint64) ^ np.uint64(salt)
    with np.errstate(over="ignore"):
        values = values + np.uint64(0x9E3779B97F4A7C15)
        values = (values ^ (values >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        values = (values ^ (values >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return values ^ (values >> np.uint64(31))


def _attestation(
    source: BioFormatsSource,
    facts: SourceImageFacts,
    measurement_planes: tuple[MeasurementPlane, ...],
    policy: CompleteVectorQcPolicy,
    ledger: _ReadLedger,
    reduction: _Reduction,
    artifact_mask_sha256: str | None,
    normalization_policy: str,
) -> Level0ReadAttestation:
    return Level0ReadAttestation(
        source_file_sha256=facts.identity.file_sha256,
        canonical_image_id=facts.image_id,
        raw_count_qc=("not_applicable_normalized_intensities" if normalization_policy == "mif_pipeline_as_stored_v1" else "applied"),
        series_index=0,
        native_resolution_index=0,
        measurement_ids=tuple(item.measurement_id for item in measurement_planes),
        processing_chunk_width=policy.processing_chunk_shape.width,
        processing_chunk_height=policy.processing_chunk_shape.height,
        native_pyramid_level_count=len(facts.native_resolutions),
        processing_chunk_count=ledger.chunk_count,
        processing_chunk_inventory_sha256=ledger.chunk_hash.hexdigest(),
        regional_read_count=ledger.read_count,
        maximum_regional_read_pixels=ledger.maximum_read_pixels,
        candidate_pixel_count=reduction.candidate_count,
        accepted_vector_count=reduction.accepted_count,
        rejected_counts=tuple(
            RejectionCount(reason=reason, count=reduction.rejected[reason])
            for reason in _REJECTION_ORDER
        ),
        artifact_mask_sha256=artifact_mask_sha256,
        reader_backend=source.versions.backend,
        bffile_version=source.versions.bffile_version,
        bioformats_maven_coordinate=source.versions.bioformats_maven_coordinate,
        bioformats_runtime_version=source.versions.bioformats_runtime_version,
    )


__all__ = [
    "ArtifactMaskSource",
    "CompleteVectorQcPolicy",
    "Level0ReadAttestation",
    "MeasurementPlane",
    "ProcessingChunkShape",
    "RoiSamplingResult",
    "SamplingError",
    "SamplingHeaderBinding",
    "TARGET_SAMPLING_STRATUM_MINIMUM_POLICY_ID",
    "TargetSamplingCapacityError",
    "TargetSamplingPolicy",
    "TargetSamplingResult",
    "TargetSamplingStratum",
    "TargetSamplingStratumMinimumError",
    "TargetSamplingStratumMinimumViolation",
    "TissueMaskBinding",
    "rasterize_tissue_strata",
    "sample_reference_roi",
    "sample_target_vectors",
    "validate_target_sampling_result_authority",
    "validate_tissue_mask_binding_authority",
]
