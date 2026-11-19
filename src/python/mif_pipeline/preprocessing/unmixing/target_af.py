"""Reviewed compatible-control target AF and fixed-basis assembly.

The `RunBasisCandidate` record carries `source_choice`, `channel_fluors`,
`fluorophore_library`, `target_af_artifact`, `qc_policy` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .reference_models import _record_fields
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import NoReturn

import numpy as np

from .compatibility import (
    QuantitativeTransformabilityReport,
    align_library_measurements,
    StructuralCompatibilityReport,
    compare_quantitative_transformability,
    compare_structural_compatibility,
    resolve_channel_fluor_identities,
)
from .interactions import TargetControlCompatibilityEvidence
from .background import BackgroundModelResolution, SampledReferenceRoi
from .reference_sampling import _validate_roi_sampling_result_authority
from .reference_models import (
    AcceptedSpectrum,
    ArtifactLineage,
    BasisSourceLineage,
    FluorophoreReferenceLibrary,
    LibraryQcEvidence,
    PairwiseSpectrumQc,
    QcMetric,
    ReferenceInputEvidence,
    ResolvedBasisColumn,
    ResolvedRunBasis,
    ReviewRecord,
    RoiEvidence,
    RoiBackgroundEvidence,
    SpectralMeasurementAxis,
    TargetAutofluorescenceModel,
    VersionRecord,
)
from .typed_checksum import typed_checksum_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================
#                                   Target af
# ==============================================================================


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return text


def _nonnegative_float(value: object, label: str, *, positive: bool = False) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise TypeError(f"{label} must be an exact finite float")
    if value < 0.0 or (positive and value <= 0.0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return value


class TargetAfError(ValueError):
    """Target-AF evidence is incomplete, incompatible, or contradictory."""


@dataclass(frozen=True, slots=True)
class FixedBasisQcPolicy:
    policy_version: str
    svd_absolute_tolerance: float
    svd_relative_tolerance: float
    cosine_review_threshold: float
    cosine_fail_threshold: float
    condition_review_threshold: float
    condition_fail_threshold: float
    authority_sha256: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FixedBasisQcPolicy fields when the record is constructed.

        The checks cover ``policy_version``, ``cosine_review_threshold``,
        ``cosine_fail_threshold``, ``condition_review_threshold``,
        ``condition_fail_threshold``, ``authority_sha256``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _text(self.policy_version, "fixed-basis QC policy_version")
        for name in ("svd_absolute_tolerance", "svd_relative_tolerance"):
            _nonnegative_float(getattr(self, name), name)
        for name in ("cosine_review_threshold", "cosine_fail_threshold"):
            value = _nonnegative_float(getattr(self, name), name)
            if value > 1.0:
                raise ValueError(f"{name} must not exceed one")
        if self.cosine_review_threshold > self.cosine_fail_threshold:
            raise ValueError("cosine review threshold must not exceed fail threshold")
        _nonnegative_float(
            self.condition_review_threshold,
            "condition_review_threshold",
            positive=True,
        )
        _nonnegative_float(
            self.condition_fail_threshold,
            "condition_fail_threshold",
            positive=True,
        )
        if self.condition_review_threshold > self.condition_fail_threshold:
            raise ValueError("condition review threshold must not exceed fail threshold")
        _sha256(self.authority_sha256, "authority_sha256")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "policy_version": self.policy_version,
                    "svd_absolute_tolerance": self.svd_absolute_tolerance,
                    "svd_relative_tolerance": self.svd_relative_tolerance,
                    "cosine_review_threshold": self.cosine_review_threshold,
                    "cosine_fail_threshold": self.cosine_fail_threshold,
                    "condition_review_threshold": self.condition_review_threshold,
                    "condition_fail_threshold": self.condition_fail_threshold,
                    "authority_sha256": self.authority_sha256,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class FixedBasisColumnEvidence:
    component_identity: str
    component_kind: str
    measurement_ids: tuple[str, ...]
    values: tuple[float, ...]
    normalization_convention: str
    abundance_rescaling_factor: float
    source_evidence_sha256: str
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FixedBasisColumnEvidence fields when the record is constructed.

        The checks cover ``component_identity``, ``component_kind``,
        ``normalization_convention``, ``abundance_rescaling_factor``,
        ``source_evidence_sha256``, ``measurement_ids``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _text(self.component_identity, "basis-QC component identity")
        _text(self.component_kind, "basis-QC component kind")
        if (
            type(self.measurement_ids) is not tuple
            or not self.measurement_ids
            or any(type(item) is not str or not item for item in self.measurement_ids)
        ):
            raise TypeError("measurement_ids must be a nonempty tuple of exact strings")
        if type(self.values) is not tuple or len(self.values) != len(self.measurement_ids):
            raise ValueError("basis-QC values must match measurement_ids")
        if any(
            type(item) is not float or not math.isfinite(item) or item < 0.0 for item in self.values
        ):
            raise ValueError("basis-QC values must be exact finite nonnegative floats")
        if not any(item > 0.0 for item in self.values):
            raise ValueError("basis-QC column must be nonzero")
        _text(self.normalization_convention, "normalization_convention")
        _nonnegative_float(
            self.abundance_rescaling_factor,
            "abundance_rescaling_factor",
            positive=True,
        )
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "component_identity": self.component_identity,
                    "component_kind": self.component_kind,
                    "measurement_ids": list(self.measurement_ids),
                    "values": list(self.values),
                    "normalization_convention": self.normalization_convention,
                    "abundance_rescaling_factor": self.abundance_rescaling_factor,
                    "source_evidence_sha256": self.source_evidence_sha256,
                }
            ),
        )

    @classmethod
    def from_target_candidate(
        cls,
        candidate: "TargetAfEndmemberCandidate",
    ) -> "FixedBasisColumnEvidence":
        if type(candidate) not in {TargetAfEndmemberCandidate, TargetAfComponentEstimate} or candidate.values is None:
            raise TargetAfError("basis QC requires a passing target-AF candidate")
        return cls(
            component_identity=candidate.component_identity,
            component_kind="target_autofluorescence",
            measurement_ids=candidate.source_roi.sampling.measurement_ids,
            values=candidate.values,
            normalization_convention="unit_l1",
            abundance_rescaling_factor=1.0,
            source_evidence_sha256=candidate.evidence_sha256,
        )

    @classmethod
    def from_resolved_column(
        cls,
        column: ResolvedBasisColumn,
        measurement_ids: tuple[str, ...],
    ) -> "FixedBasisColumnEvidence":
        if type(column) is not ResolvedBasisColumn:
            raise TypeError("column must be exact ResolvedBasisColumn")
        return cls(
            component_identity=column.component_identity,
            component_kind=column.component_kind,
            measurement_ids=measurement_ids,
            values=column.values,
            normalization_convention=column.normalization_convention,
            abundance_rescaling_factor=float(column.abundance_rescaling_factor),
            source_evidence_sha256=column.spectrum_sha256,
        )


def _fixed_basis_outputs(
    columns: tuple[FixedBasisColumnEvidence, ...],
    policy: FixedBasisQcPolicy,
):
    if not columns:
        raise TargetAfError("fixed-basis QC requires at least one column")
    axis = columns[0].measurement_ids
    if any(item.measurement_ids != axis for item in columns):
        raise TargetAfError("fixed-basis QC column axes differ")
    identities = tuple(item.component_identity for item in columns)
    if len(identities) != len(set(identities)):
        raise TargetAfError("duplicate fixed-basis component identity")
    matrix = np.asarray(tuple(item.values for item in columns), dtype=np.float64).T
    singular = tuple(float(item) for item in np.linalg.svd(matrix, compute_uv=False))
    tau = float(max(policy.svd_absolute_tolerance, policy.svd_relative_tolerance * singular[0]))
    rank = sum(item > tau for item in singular)
    full_rank = len(columns) <= len(axis) and rank == len(columns)
    condition = float(singular[0] / singular[len(columns) - 1]) if full_rank else None
    pairs: list[PairwiseSpectrumQc] = []
    reasons: list[str] = []
    failed = len(columns) > len(axis) or not full_rank
    review = False
    if len(columns) > len(axis):
        reasons.append("component_count_exceeds_measurements")
    if not full_rank:
        reasons.append("rank_deficient")
    if condition is not None:
        if condition >= policy.condition_fail_threshold:
            failed = True
            reasons.append("condition_number_failed")
        elif condition >= policy.condition_review_threshold:
            review = True
            reasons.append("condition_number_review_required")
    for index, first in enumerate(columns):
        first_norm = math.sqrt(math.fsum(item * item for item in first.values))
        for second in columns[index + 1 :]:
            second_norm = math.sqrt(math.fsum(item * item for item in second.values))
            cosine = float(
                min(
                    1.0,
                    max(
                        0.0,
                        math.fsum(
                            left * right
                            for left, right in zip(first.values, second.values, strict=True)
                        )
                        / (first_norm * second_norm),
                    ),
                )
            )
            pairs.append(
                PairwiseSpectrumQc(
                    first.component_identity,
                    second.component_identity,
                    cosine,
                    float(math.degrees(math.acos(cosine))),
                )
            )
            suffix = f":{first.component_identity}:{second.component_identity}"
            if cosine >= policy.cosine_fail_threshold:
                failed = True
                reasons.append("pairwise_similarity_failed" + suffix)
            elif cosine >= policy.cosine_review_threshold:
                review = True
                reasons.append("pairwise_similarity_review_required" + suffix)
    disposition = "failed" if failed else "review_required" if review else "pass"
    return axis, singular, tau, rank, condition, tuple(pairs), disposition, tuple(reasons)


@dataclass(frozen=True, slots=True)
class FixedBasisQcResult:
    policy: FixedBasisQcPolicy
    columns: tuple[FixedBasisColumnEvidence, ...]
    measurement_ids: tuple[str, ...]
    singular_values: tuple[float, ...]
    rank_tolerance: float
    rank: int
    condition_number: float | None
    pairwise_spectra: tuple[PairwiseSpectrumQc, ...]
    disposition: str
    reason_codes: tuple[str, ...]
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FixedBasisQcResult fields when the record is constructed.

        The checks cover ``columns``, ``policy``, ``measurement_ids``, ``singular_values``,
        ``rank_tolerance``, ``rank``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if type(self.policy) is not FixedBasisQcPolicy:
            raise TypeError("policy must be FixedBasisQcPolicy")
        if type(self.columns) is not tuple or any(
            type(item) is not FixedBasisColumnEvidence for item in self.columns
        ):
            raise TypeError("columns must contain exact FixedBasisColumnEvidence")
        expected = _fixed_basis_outputs(self.columns, self.policy)
        actual = (
            self.measurement_ids,
            self.singular_values,
            self.rank_tolerance,
            self.rank,
            self.condition_number,
            self.pairwise_spectra,
            self.disposition,
            self.reason_codes,
        )
        if actual != expected:
            raise TargetAfError("fixed-basis QC result fields contradict derived evidence")
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "policy_sha256": self.policy.content_sha256,
                    "column_sha256s": [item.evidence_sha256 for item in self.columns],
                    "measurement_ids": list(self.measurement_ids),
                    "singular_values": list(self.singular_values),
                    "rank_tolerance": self.rank_tolerance,
                    "rank": self.rank,
                    "condition_number": self.condition_number,
                    "pairwise_spectra": [
                        {
                            "first": item.first_component_identity,
                            "second": item.second_component_identity,
                            "cosine": item.cosine_similarity,
                            "angle": item.spectral_angle_degrees,
                        }
                        for item in self.pairwise_spectra
                    ],
                    "disposition": self.disposition,
                    "reason_codes": list(self.reason_codes),
                }
            ),
        )


def _evaluate_fixed_basis(
    columns: tuple[FixedBasisColumnEvidence, ...],
    policy: FixedBasisQcPolicy,
) -> FixedBasisQcResult:
    if type(columns) is not tuple or any(
        type(item) is not FixedBasisColumnEvidence for item in columns
    ):
        raise TypeError("columns must contain exact FixedBasisColumnEvidence")
    if type(policy) is not FixedBasisQcPolicy:
        raise TypeError("policy must be FixedBasisQcPolicy")
    derived = _fixed_basis_outputs(columns, policy)
    return FixedBasisQcResult(
        policy=policy,
        columns=columns,
        measurement_ids=derived[0],
        singular_values=derived[1],
        rank_tolerance=derived[2],
        rank=derived[3],
        condition_number=derived[4],
        pairwise_spectra=derived[5],
        disposition=derived[6],
        reason_codes=derived[7],
    )


def evaluate_target_af_qc(
    candidates: tuple["TargetAfEndmemberCandidate", ...],
    policy: FixedBasisQcPolicy,
) -> FixedBasisQcResult:
    """Evaluate explicitly selected target-AF columns under versioned gates."""

    if (
        type(candidates) is not tuple
        or not candidates
        or any(type(item) not in {TargetAfEndmemberCandidate, TargetAfComponentEstimate} for item in candidates)
    ):
        raise TypeError("candidates must contain exact target-AF candidates")
    return _evaluate_fixed_basis(
        tuple(FixedBasisColumnEvidence.from_target_candidate(item) for item in candidates),
        policy,
    )


@dataclass(frozen=True, slots=True)
class TargetAfEstimationPolicy:
    policy_version: str
    minimum_l2_norm: float
    authority_sha256: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "target-AF policy_version")
        _nonnegative_float(self.minimum_l2_norm, "minimum_l2_norm", positive=True)
        _sha256(self.authority_sha256, "authority_sha256")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "policy_version": self.policy_version,
                    "minimum_l2_norm": self.minimum_l2_norm,
                    "authority_sha256": self.authority_sha256,
                }
            ),
        )


def _target_background_values(
    source: SampledReferenceRoi,
    background: BackgroundModelResolution,
) -> tuple[float, ...]:
    if type(background) is not BackgroundModelResolution or (
        background.status != "resolved" or background.model is None
    ):
        raise TargetAfError("target-AF background is not resolved")
    input_evidence = source.input_evidence
    matches = tuple(
        item.values
        for item in (
            background.model.stained_background,
            background.model.control_background,
        )
        if item.source_input_id == input_evidence.input_id
        and item.source_image_sha256 == input_evidence.source_image_sha256
    )
    unique = tuple(dict.fromkeys(matches))
    if len(unique) != 1:
        raise TargetAfError("target-AF background does not bind one exact control input")
    return unique[0]


def _artifact_roi_evidence(
    source: SampledReferenceRoi,
    *,
    inclusion_reason: str,
) -> RoiEvidence:
    sampling = source.sampling
    rejected = sum(item.count for item in sampling.attestation.rejected_counts)
    candidate_count = sampling.attestation.candidate_pixel_count
    return RoiEvidence(
        roi_id=source.selection.roi.roi_id,
        roi_role=source.selection.roi.roi_role,
        source_input_id=source.input_evidence.input_id,
        source_image_sha256=source.input_evidence.source_image_sha256,
        geometry_artifact_sha256=source.selection.roi.geometry_sha256,
        complete_vector_estimator_version="complete_vector_arithmetic_mean_v1",
        sampling_policy=sampling.attestation.rasterization_policy,
        sampling_seed=None,
        requested_pixel_count=None,
        total_pixel_count=candidate_count,
        accepted_pixel_count=sampling.accepted_vector_count,
        rejected_pixel_count=rejected,
        rejection_counts=sampling.attestation.rejected_counts,
        roi_coverage_fraction=float(sampling.accepted_vector_count / candidate_count),
        sampled_chunk_ids=sampling.processing_chunk_ids,
        signal_measurement_id=None,
        dark_measurement_ids=(),
        matched_af_roi_ids=(),
        af_subtraction_factor=None,
        purified_spectrum=None,
        consensus_inclusion="not_applicable",
        inclusion_reason=inclusion_reason,
        qc_metrics=(),
        accepted=True,
        reason_codes=(),
    )


def _validate_sampled_roi_authority(source: SampledReferenceRoi) -> None:
    try:
        _validate_roi_sampling_result_authority(source.sampling)
        rebuilt = SampledReferenceRoi(
            source.selection,
            source.input_evidence,
            source.sampling,
        )
    except (TypeError, ValueError) as exc:
        raise TargetAfError("Task-5 sampling evidence is stale or invalid") from exc
    if (
        rebuilt.read_attestation_sha256 != source.read_attestation_sha256
        or rebuilt.evidence_sha256 != source.evidence_sha256
    ):
        raise TargetAfError("Task-5 sampling evidence is stale or invalid")


def _validate_target_af_sampling_authority(
    proposal: "TargetAutofluorescenceProposal",
) -> None:
    retained: list[SampledReferenceRoi] = [
        item.proposal.source_roi for item in proposal.estimation.selections
    ]
    for candidate in proposal.estimation.candidates:
        for side in (candidate.background.stained_side, candidate.background.control_side):
            if side is not None:
                retained.extend(side.source_rois)
    seen: set[int] = set()
    for source in retained:
        if id(source) in seen:
            continue
        seen.add(id(source))
        _validate_sampled_roi_authority(source)


def _target_background_roi_evidence(
    candidate: "TargetAfEndmemberCandidate",
) -> tuple[RoiEvidence, ...]:
    model = candidate.background.model
    assert model is not None
    if model.policy == "validated_offset_corrected_v1":
        return ()
    retained: list[SampledReferenceRoi] = []
    for side in (candidate.background.stained_side, candidate.background.control_side):
        if side is None:
            continue
        for source in side.source_rois:
            if source.evidence_sha256 not in {item.evidence_sha256 for item in retained}:
                retained.append(source)
    return tuple(
        _artifact_roi_evidence(
            source,
            inclusion_reason="target_control_background",
        )
        for source in retained
    )


def evaluate_run_basis_qc(
    columns: tuple[ResolvedBasisColumn, ...],
    axis: SpectralMeasurementAxis,
    policy: FixedBasisQcPolicy,
) -> FixedBasisQcResult:
    """Evaluate exact stored run-basis columns without changing their scaling."""

    if type(axis) is not SpectralMeasurementAxis:
        raise TypeError("axis must be SpectralMeasurementAxis")
    if (
        type(columns) is not tuple
        or not columns
        or any(type(item) is not ResolvedBasisColumn for item in columns)
    ):
        raise TypeError("columns must contain exact ResolvedBasisColumn records")
    return _evaluate_fixed_basis(
        tuple(
            FixedBasisColumnEvidence.from_resolved_column(
                item,
                axis.measurement_ids,
            )
            for item in columns
        ),
        policy,
    )


def _json_primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_primitive(getattr(value, item.name))
            for item in _record_fields(value)
            if hasattr(value, item.name)
        }
    if isinstance(value, tuple):
        return [_json_primitive(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported run-basis artifact value {type(value).__name__}")


def _reject_constant(value: str) -> NoReturn:
    raise TargetAfError(f"non-finite JSON number {value!r} is not permitted")


def write_resolved_run_basis_artifact(
    artifact: ResolvedRunBasisArtifact,
    path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically publish strict run-basis JSON without silent replacement."""

    if type(artifact) is not ResolvedRunBasisArtifact:
        raise TypeError("artifact must be ResolvedRunBasisArtifact")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"resolved run-basis artifact exists: {destination}")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(dumps_resolved_run_basis_artifact(artifact))
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, destination)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                raise FileExistsError(
                    f"resolved run-basis artifact exists: {destination}"
                ) from None
            temporary_path.unlink()
            temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


@dataclass(frozen=True, slots=True)
class TargetControlBinding:
    """Exact reviewed target/control compatibility with recomputed reports."""

    selection_kind: str
    target_input: ReferenceInputEvidence
    control_input: ReferenceInputEvidence
    compatibility: TargetControlCompatibilityEvidence
    structural_report: StructuralCompatibilityReport
    quantitative_report: QuantitativeTransformabilityReport
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetControlBinding fields when the record is constructed.

        The checks cover ``selection_kind``, ``compatibility``, ``structural_report``,
        ``quantitative_report``, ``target_input``, ``control_input``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if self.selection_kind not in {
            "reuse_reference_unstained_control",
            "select_target_unstained_control",
            "review_reference_control_groups",
        }:
            raise TargetAfError("unsupported target-control selection")
        if type(self.target_input) is not ReferenceInputEvidence or (
            self.target_input.input_role != "target_image"
        ):
            raise TargetAfError("target input must be exact target_image evidence")
        if type(self.control_input) is not ReferenceInputEvidence:
            raise TargetAfError("control input must be exact reviewed reference input evidence")
        expected_control_role = {
            "reuse_reference_unstained_control": "unstained_control",
            "select_target_unstained_control": "target_unstained_control",
            "review_reference_control_groups": self.control_input.input_role,
        }[self.selection_kind]
        expected_artifact_role = {
            "reuse_reference_unstained_control": "unstained_reference_image",
            "select_target_unstained_control": "target_unstained_control_image",
            "review_reference_control_groups": {
                "unstained_control": "unstained_reference_image",
                "target_unstained_control": "target_unstained_control_image",
                "single_stain_negative_control": "negative_reference_image",
            }.get(self.control_input.input_role),
        }[self.selection_kind]
        if type(self.control_input) is not ReferenceInputEvidence or (
            self.control_input.input_role != expected_control_role
            or self.control_input.input_role not in {
                "unstained_control", "target_unstained_control", "single_stain_negative_control"}
        ):
            raise TargetAfError("control input role does not match its selection")
        if type(self.compatibility) is not TargetControlCompatibilityEvidence:
            raise TypeError("compatibility must be TargetControlCompatibilityEvidence")
        pointer = self.compatibility.control_artifact
        if (
            pointer.role != expected_artifact_role
            or pointer.artifact_sha256 != self.control_input.source_image_sha256
            or self.compatibility.target_image_sha256 != self.target_input.source_image_sha256
        ):
            raise TargetAfError("target/control artifact bytes or target identity differ")
        if not self.compatibility.accepted:
            raise TargetAfError("target-control compatibility review is not accepted")
        if type(self.structural_report) is not StructuralCompatibilityReport:
            raise TypeError("structural_report must be StructuralCompatibilityReport")
        if type(self.quantitative_report) is not QuantitativeTransformabilityReport:
            raise TypeError("quantitative_report must be QuantitativeTransformabilityReport")
        expected_structural = compare_structural_compatibility(
            reference_axis=self.control_input.axis,
            candidate_axis=self.target_input.axis,
            reference_signature=self.control_input.structural_signature,
            candidate_signature=self.target_input.structural_signature,
        )
        if (
            self.structural_report != expected_structural
            or not expected_structural.compatible
            or self.compatibility.structural_report_sha256 != expected_structural.content_sha256
        ):
            raise TargetAfError("structural report does not bind compatible target/control inputs")
        expected_quantitative = compare_quantitative_transformability(
            reference_signature=self.control_input.quantitative_signature,
            candidate_signature=self.target_input.quantitative_signature,
        )
        if (
            self.quantitative_report != expected_quantitative
            or not expected_quantitative.transformable
            or self.compatibility.quantitative_report_sha256 != expected_quantitative.content_sha256
        ):
            raise TargetAfError(
                "quantitative report does not bind transformable target/control inputs"
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "selection_kind": self.selection_kind,
                    "target_input": _json_primitive(self.target_input),
                    "control_input": _json_primitive(self.control_input),
                    "compatibility": self.compatibility.to_mapping(),
                    "structural_report_sha256": expected_structural.content_sha256,
                    "quantitative_report_sha256": expected_quantitative.content_sha256,
                }
            ),
        )


def validate_target_control_binding(
    *,
    selection_kind: str,
    target_input: ReferenceInputEvidence,
    control_input: ReferenceInputEvidence,
    compatibility: TargetControlCompatibilityEvidence,
    structural_report: StructuralCompatibilityReport,
    quantitative_report: QuantitativeTransformabilityReport,
) -> TargetControlBinding:
    """Recompute target/control compatibility and bind exact reviewed bytes."""

    return TargetControlBinding(
        selection_kind=selection_kind,
        target_input=target_input,
        control_input=control_input,
        compatibility=compatibility,
        structural_report=structural_report,
        quantitative_report=quantitative_report,
    )


# publication contracts deliberately wrap the legacy accepted model.
# The wrapper is the authoritative persisted form because it retains the exact
# proposal and decision evidence from which the model is rederived.


@dataclass(frozen=True, slots=True)
class ProposalReviewDecision:
    proposal_sha256: str
    decision: str
    reviewer_id: str
    reviewed_at: str
    revision: int
    decision_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _sha256(self.proposal_sha256, "proposal SHA-256")
        if self.decision not in {"accepted", "rejected"}:
            raise TargetAfError("proposal decision must be accepted or rejected")
        _text(self.reviewer_id, "proposal reviewer ID")
        _timestamp(self.reviewed_at, "proposal reviewed_at")
        if type(self.revision) is not int or self.revision < 1:
            raise TargetAfError("proposal review revision must be a positive integer")
        object.__setattr__(
            self,
            "decision_sha256",
            typed_checksum_sha256(
                {
                    "proposal_sha256": self.proposal_sha256,
                    "decision": self.decision,
                    "reviewer_id": self.reviewer_id,
                    "reviewed_at": self.reviewed_at,
                    "revision": self.revision,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetAfRoiProposal:
    source_roi: SampledReferenceRoi
    component_identity: str
    proposal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.source_roi) is not SampledReferenceRoi:
            raise TypeError("source_roi must be exact SampledReferenceRoi")
        _validate_sampled_roi_authority(self.source_roi)
        role = self.source_roi.selection.roi.roi_role
        source_role = self.source_roi.input_evidence.input_role
        if not ((role == "af" and source_role in {"unstained_control", "target_unstained_control", "single_stain_negative_control"})
                or (role == "negative" and source_role == "single_stain_negative_control")):
            raise TargetAfError("target-AF ROI proposal requires an original reviewed control ROI")
        _text(self.component_identity, "target-AF component identity")
        object.__setattr__(
            self,
            "proposal_sha256",
            typed_checksum_sha256(
                {
                    "source_roi": _json_primitive(self.source_roi),
                    "component_identity": self.component_identity,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewedTargetAfRoiSelection:
    proposal: TargetAfRoiProposal
    decision: ProposalReviewDecision
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.proposal) is not TargetAfRoiProposal:
            raise TypeError("proposal must be exact TargetAfRoiProposal")
        if type(self.decision) is not ProposalReviewDecision:
            raise TypeError("decision must be exact ProposalReviewDecision")
        if self.decision.proposal_sha256 != self.proposal.proposal_sha256:
            raise TargetAfError("ROI review decision does not bind the exact proposal")
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "proposal_sha256": self.proposal.proposal_sha256,
                    "decision_sha256": self.decision.decision_sha256,
                }
            ),
        )

    @property
    def included(self) -> bool:
        return self.decision.decision == "accepted"

    @property
    def roi_id(self) -> str:
        return self.proposal.source_roi.selection.roi.roi_id

    @property
    def component_identity(self) -> str:
        return self.proposal.component_identity


@dataclass(frozen=True, slots=True)
class TargetAfEndmemberCandidate:
    selection: ReviewedTargetAfRoiSelection
    background: BackgroundModelResolution
    policy: TargetAfEstimationPolicy
    corrected_values: tuple[float, ...] = field(init=False)
    values: tuple[float, ...] | None = field(init=False)
    disposition: str = field(init=False)
    reason_codes: tuple[str, ...] = field(init=False)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAfEndmemberCandidate fields when the record is constructed.

        The checks cover ``source_roi``, ``background``, ``selection``, ``policy``,
        ``disposition``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        if type(self.selection) is not ReviewedTargetAfRoiSelection:
            raise TypeError("selection must be exact ReviewedTargetAfRoiSelection")
        if not self.selection.included:
            raise TargetAfError("excluded target-AF ROI cannot become a candidate")
        if type(self.policy) is not TargetAfEstimationPolicy:
            raise TypeError("policy must be exact TargetAfEstimationPolicy")
        source = self.source_roi
        background_values = _target_background_values(source, self.background)
        mean = source.sampling.mean_vector
        if len(background_values) != len(mean):
            raise TargetAfError("target-AF background axis differs from sampled ROI")
        corrected: list[float] = []
        reasons: list[str] = []
        for observed, offset in zip(mean, background_values, strict=True):
            residual = float(observed - offset)
            if not math.isfinite(residual):
                raise TargetAfError("target-AF background subtraction produced a nonfinite value")
            corrected.append(max(residual, 0.0))
        norm = math.hypot(*corrected)
        if not math.isfinite(norm):
            reasons.append("target_af_norm_nonfinite")
        if norm <= self.policy.minimum_l2_norm:
            reasons.append("insufficient_target_af_norm")
        unique_reasons = tuple(dict.fromkeys(reasons))
        values = None
        if not unique_reasons:
            total = math.fsum(corrected)
            values = tuple(float(value / total) for value in corrected)
        object.__setattr__(self, "corrected_values", tuple(corrected))
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "disposition", "failed" if unique_reasons else "pass")
        object.__setattr__(self, "reason_codes", unique_reasons)
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "selection_sha256": self.selection.evidence_sha256,
                    "background": _json_primitive(self.background),
                    "policy_sha256": self.policy.content_sha256,
                    "corrected_values": list(corrected),
                    "values": None if values is None else list(values),
                    "disposition": self.disposition,
                    "reason_codes": list(unique_reasons),
                }
            ),
        )

    @property
    def signed_background_difference(self) -> tuple[float, ...]:
        """Retain the measured subtraction for review before zero clipping."""
        return tuple(
            float(observed - offset)
            for observed, offset in zip(
                self.source_roi.sampling.mean_vector,
                _target_background_values(self.source_roi, self.background),
                strict=True,
            )
        )

    @property
    def source_roi(self) -> SampledReferenceRoi:
        return self.selection.proposal.source_roi

    @property
    def component_identity(self) -> str:
        return self.selection.component_identity


@dataclass(frozen=True, slots=True)
class TargetAfComponentEstimate:
    """One explicitly named AF shape from compatible regional observations."""

    members: tuple[TargetAfEndmemberCandidate, ...]
    corrected_values: tuple[float, ...] = field(init=False)
    values: tuple[float, ...] = field(init=False)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        from .purification import combine_af_regional_means
        if (type(self.members) is not tuple or len(self.members) < 2
                or any(type(item) is not TargetAfEndmemberCandidate for item in self.members)):
            raise TypeError("AF component requires multiple exact regional candidates")
        if len(set(self.roi_ids)) != len(self.members):
            raise TargetAfError("AF component has duplicate regional evidence")
        first = self.members[0]
        for item in self.members:
            if (item.component_identity != first.component_identity or item.policy != first.policy
                    or item.source_roi.input_evidence.axis != first.source_roi.input_evidence.axis
                    or not compare_structural_compatibility(
                        reference_axis=first.source_roi.input_evidence.axis,
                        candidate_axis=item.source_roi.input_evidence.axis,
                        reference_signature=first.source_roi.input_evidence.structural_signature,
                        candidate_signature=item.source_roi.input_evidence.structural_signature).compatible
                    or not compare_quantitative_transformability(
                        reference_signature=first.source_roi.input_evidence.quantitative_signature,
                        candidate_signature=item.source_roi.input_evidence.quantitative_signature).transformable):
                raise TargetAfError("AF component members differ in identity or measurement compatibility")
            if item.disposition != "pass" or item.values is None:
                raise TargetAfError("Failed AF regions must be excluded before combining a component")
        combined = combine_af_regional_means(tuple(item.signed_background_difference for item in self.members))
        if math.hypot(*combined.corrected_mean) <= first.policy.minimum_l2_norm:
            raise TargetAfError("Combined target AF spectrum has insufficient signal")
        total = math.fsum(combined.corrected_mean)
        values = tuple(value / total for value in combined.corrected_mean)
        object.__setattr__(self, "corrected_values", combined.corrected_mean)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "evidence_sha256", typed_checksum_sha256({
            "semantic": "equal_roi_signed_mean_target_af_v1",
            "member_evidence_sha256s": [item.evidence_sha256 for item in self.members],
            "roi_weights": list(combined.roi_weights), "signed_mean": list(combined.signed_mean),
            "values": list(values)}))

    @property
    def component_identity(self):
        return self.members[0].component_identity

    @property
    def roi_ids(self):
        return tuple(item.source_roi.selection.roi.roi_id for item in self.members)

    @property
    def roi_weights(self):
        return tuple(1.0 / len(self.members) for _ in self.members)

    @property
    def signed_background_difference(self):
        from .purification import combine_af_regional_means
        return combine_af_regional_means(tuple(item.signed_background_difference for item in self.members)).signed_mean

    @property
    def source_roi(self):
        return self.members[0].source_roi

    @property
    def background(self):
        return self.members[0].background

    @property
    def disposition(self):
        return "pass"


def target_af_component_members(component):
    return component.members if type(component) is TargetAfComponentEstimate else (component,)


@dataclass(frozen=True, slots=True)
class TargetAfEstimationResult:
    selections: tuple[ReviewedTargetAfRoiSelection, ...]
    candidates: tuple[TargetAfEndmemberCandidate, ...]
    background: BackgroundModelResolution
    policy: TargetAfEstimationPolicy
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAfEstimationResult fields when the record is constructed.

        The checks cover ``background``, ``selections``, ``candidates``, ``policy``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (
            type(self.selections) is not tuple
            or not self.selections
            or any(type(item) is not ReviewedTargetAfRoiSelection for item in self.selections)
        ):
            raise TypeError("selections must contain exact reviewed ROI selections")
        if type(self.candidates) is not tuple or any(
            type(item) is not TargetAfEndmemberCandidate for item in self.candidates
        ):
            raise TypeError("candidates must contain exact target-AF candidates")
        included = tuple(item for item in self.selections if item.included)
        if not included or tuple(item.selection for item in self.candidates) != included:
            raise TargetAfError("candidates must equal the exact included ROI decisions")
        roi_ids = tuple(item.roi_id for item in self.selections)
        task4_selection_hashes = tuple(
            item.proposal.source_roi.selection.selection_sha256 for item in self.selections
        )
        sampled_evidence_hashes = tuple(
            item.proposal.source_roi.evidence_sha256 for item in self.selections
        )
        reviewed_selection_hashes = tuple(item.evidence_sha256 for item in self.selections)
        if len(roi_ids) != len(set(roi_ids)):
            raise TargetAfError("one biological ROI may have at most one target-AF decision")
        if len(task4_selection_hashes) != len(set(task4_selection_hashes)):
            raise TargetAfError("duplicate Task-4 ROI selection evidence")
        if len(sampled_evidence_hashes) != len(set(sampled_evidence_hashes)):
            raise TargetAfError("duplicate sampled-ROI evidence")
        if len(reviewed_selection_hashes) != len(set(reviewed_selection_hashes)):
            raise TargetAfError("duplicate reviewed target-AF selection evidence")
        if type(self.background) is not BackgroundModelResolution:
            raise TypeError("background must be exact BackgroundModelResolution")
        if any(item.policy != self.policy for item in self.candidates):
            raise TargetAfError("candidate policy substitution detected")
        if self.candidates[0].background != self.background:
            raise TargetAfError("primary candidate background substitution detected")
        # Repeated reviewed names are replicate observations of one component.
        # Force derivation now so failed/zero members cannot be hidden by aggregation.
        self.components
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "selection_sha256s": [item.evidence_sha256 for item in self.selections],
                    "candidate_sha256s": [item.evidence_sha256 for item in self.candidates],
                    "background_sha256": self.background.resolution_sha256,
                    "policy_sha256": self.policy.content_sha256,
                }
            ),
        )

    @property
    def components(self):
        grouped = {}
        for candidate in self.candidates:
            grouped.setdefault(candidate.component_identity, []).append(candidate)
        return tuple(items[0] if len(items) == 1 else TargetAfComponentEstimate(tuple(items))
                     for items in grouped.values())

    @property
    def sampled_af_rois(self) -> tuple[SampledReferenceRoi, ...]:
        return tuple(item.proposal.source_roi for item in self.selections)


def estimate_target_af_candidates(
    *,
    selections: tuple[ReviewedTargetAfRoiSelection, ...],
    background: BackgroundModelResolution,
    policy: TargetAfEstimationPolicy,
    backgrounds: tuple[BackgroundModelResolution, ...] | None = None,
) -> TargetAfEstimationResult:
    if type(selections) is not tuple:
        raise TypeError("selections must be an exact tuple")
    selected_backgrounds = (
        tuple(background for _ in selections) if backgrounds is None else backgrounds
    )
    if (
        type(selected_backgrounds) is not tuple
        or len(selected_backgrounds) != len(selections)
        or any(type(item) is not BackgroundModelResolution for item in selected_backgrounds)
    ):
        raise TypeError("backgrounds must align exact resolutions with reviewed selections")
    candidates = tuple(
        TargetAfEndmemberCandidate(item, candidate_background, policy)
        for item, candidate_background in zip(selections, selected_backgrounds, strict=True)
        if item.included
    )
    primary_background = candidates[0].background if candidates else background
    return TargetAfEstimationResult(selections, candidates, primary_background, policy)


@dataclass(frozen=True, slots=True)
class TargetAutofluorescenceProposal:
    artifact_id: str
    created_at: str
    run_id: str
    control_binding: TargetControlBinding
    estimation: TargetAfEstimationResult
    qc: FixedBasisQcResult
    software_versions: tuple[VersionRecord, ...]
    policy_versions: tuple[VersionRecord, ...]
    additional_control_bindings: tuple[TargetControlBinding, ...] = field(
        default=(), metadata={"omit_empty": True})
    proposal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAutofluorescenceProposal fields when the record is constructed.

        The checks cover ``artifact_id``, ``created_at``, ``run_id``, ``control_binding``,
        ``estimation``, ``software_versions``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        _text(self.artifact_id, "target-AF artifact ID")
        _timestamp(self.created_at, "target-AF created_at")
        _text(self.run_id, "target-AF run ID")
        if type(self.control_binding) is not TargetControlBinding:
            raise TypeError("control_binding must be exact TargetControlBinding")
        if type(self.estimation) is not TargetAfEstimationResult:
            raise TypeError("estimation must be exact TargetAfEstimationResult")
        _validate_target_af_sampling_authority(self)
        if type(self.qc) is not FixedBasisQcResult or self.qc.disposition == "failed":
            raise TargetAfError("target-AF proposal requires nonfailed QC")
        expected_sources = tuple(item.evidence_sha256 for item in self.estimation.components)
        if tuple(item.source_evidence_sha256 for item in self.qc.columns) != expected_sources:
            raise TargetAfError("target-AF QC does not bind exact candidates")
        if type(self.additional_control_bindings) is not tuple or any(
                type(item) is not TargetControlBinding for item in self.additional_control_bindings):
            raise TypeError("additional control bindings must be an exact tuple")
        bindings = self.control_bindings
        by_id = {item.control_input.input_id: item for item in bindings}
        if len(by_id) != len(bindings) or any(
                item.target_input != self.control_binding.target_input for item in bindings):
            raise TargetAfError("target-AF bindings require unique sources and the same target")
        for selection in self.estimation.selections:
            if not selection.included:
                continue
            source = selection.proposal.source_roi.input_evidence
            binding = by_id.get(source.input_id)
            if binding is None or binding.control_input != source:
                raise TargetAfError("every included target-AF selection must bind its reviewed control")
            if source.input_role == "single_stain_negative_control" and (
                    binding.selection_kind != "review_reference_control_groups"):
                raise TargetAfError("negative-origin AF requires explicit target-use review")
        if any(item.disposition != "pass" for item in self.estimation.candidates):
            raise TargetAfError("failed target-AF candidates cannot be proposed")
        if type(self.software_versions) is not tuple or not self.software_versions:
            raise TypeError("software_versions must be a nonempty tuple")
        if type(self.policy_versions) is not tuple or not self.policy_versions:
            raise TypeError("policy_versions must be a nonempty tuple")
        object.__setattr__(
            self,
            "proposal_sha256",
            typed_checksum_sha256(
                {
                    "artifact_id": self.artifact_id,
                    "created_at": self.created_at,
                    "run_id": self.run_id,
                    "control_binding": _json_primitive(self.control_binding),
                    "estimation": _json_primitive(self.estimation),
                    "qc": _json_primitive(self.qc),
                    "software_versions": _json_primitive(self.software_versions),
                    "policy_versions": _json_primitive(self.policy_versions),
                    **({"additional_control_bindings": _json_primitive(self.additional_control_bindings)}
                       if self.additional_control_bindings else {}),
                }
            ),
        )

    @property
    def control_bindings(self) -> tuple[TargetControlBinding, ...]:
        return (self.control_binding, *self.additional_control_bindings)


def propose_target_autofluorescence_model(**kwargs: object) -> TargetAutofluorescenceProposal:
    return TargetAutofluorescenceProposal(**kwargs)  # type: ignore[arg-type]


def _model_from_target_proposal(
    proposal: TargetAutofluorescenceProposal,
    decision: ProposalReviewDecision,
) -> TargetAutofluorescenceModel:
    review = ReviewRecord("accepted", decision.reviewer_id, decision.reviewed_at)
    binding = proposal.control_binding
    source_inputs = []
    source_by_id = {}

    def include_source(source: ReferenceInputEvidence) -> None:
        previous = source_by_id.get(source.input_id)
        if previous is not None and previous != source:
            raise ValueError("target-AF source input identity is ambiguous")
        if previous is None:
            source_by_id[source.input_id] = source
            source_inputs.append(source)

    include_source(binding.target_input)
    for source_binding in proposal.control_bindings:
        include_source(source_binding.control_input)
    for candidate in proposal.estimation.candidates:
        include_source(candidate.source_roi.input_evidence)
        assert candidate.background.model is not None
        for side in (
            candidate.background.stained_side,
            candidate.background.control_side,
        ):
            assert side is not None
            for source_roi in side.source_rois:
                include_source(source_roi.input_evidence)
    endmembers = []
    for component in proposal.estimation.components:
        members = target_af_component_members(component)
        evidence = []
        seen = set()
        for member in members:
            for item in (_artifact_roi_evidence(member.source_roi,
                    inclusion_reason=("reviewed_target_negative_control_af"
                        if member.source_roi.input_evidence.input_role == "single_stain_negative_control"
                        else "reviewed_target_control_af")), *_target_background_roi_evidence(member)):
                if item.roi_id not in seen:
                    evidence.append(item)
                    seen.add(item.roi_id)
        endmembers.append(AcceptedSpectrum(
            component_identity=component.component_identity, fluorophore_identity=None,
            component_kind="target_autofluorescence", normalization_convention="unit_l1",
            abundance_rescaling_factor=1.0, values=component.values, roi_evidence=tuple(evidence),
            background_model=members[0].background.model, review=review,
            regional_backgrounds=tuple(RoiBackgroundEvidence(member.source_roi.selection.roi.roi_id,
                None, member.background.model) for member in members) if len(members) > 1 else ()))
    endmembers = tuple(endmembers)
    qc = proposal.qc
    assert qc.condition_number is not None
    artifact_qc = LibraryQcEvidence(
        policy_version=qc.policy.policy_version,
        rank=qc.rank,
        condition_number=qc.condition_number,
        pairwise_spectra=qc.pairwise_spectra,
        metrics=(
            QcMetric(
                f"target_af_fixed_basis_qc:{qc.evidence_sha256}",
                0.0 if qc.disposition == "pass" else 1.0,
                True,
            ),
        ),
        passed=True,
    )
    lineage = ArtifactLineage(
        None,
        None,
        tuple(dict.fromkeys(item.source_image_sha256 for item in source_inputs))
        + (
            *(item.evidence_sha256 for item in proposal.control_bindings),
            proposal.estimation.evidence_sha256,
            qc.evidence_sha256,
        ),
        "accepted_typed_target_af_proposal",
    )
    return TargetAutofluorescenceModel(
        artifact_id=proposal.artifact_id,
        created_at=proposal.created_at,
        run_id=proposal.run_id,
        target_image_sha256=binding.target_input.source_image_sha256,
        axis=binding.target_input.axis,
        structural_signature=binding.target_input.structural_signature,
        quantitative_signature=binding.target_input.quantitative_signature,
        source_inputs=tuple(source_inputs),
        normalization_convention="unit_l1",
        estimation_method=("reviewed_reference_control_equal_roi_mean_v1"
            if any(item.selection_kind == "review_reference_control_groups" for item in proposal.control_bindings)
            else "compatible_unstained_control_equal_roi_mean_v1"
            if any(type(item) is TargetAfComponentEstimate for item in proposal.estimation.components)
            else "compatible_unstained_control"),
        endmembers=endmembers,
        qc=artifact_qc,
        software_versions=proposal.software_versions,
        policy_versions=proposal.policy_versions,
        review=review,
        lineage=lineage,
    )


@dataclass(frozen=True, slots=True)
class TargetAutofluorescenceArtifact:
    proposal: TargetAutofluorescenceProposal
    decision: ProposalReviewDecision
    model: TargetAutofluorescenceModel = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.proposal) is not TargetAutofluorescenceProposal:
            raise TypeError("proposal must be exact TargetAutofluorescenceProposal")
        if type(self.decision) is not ProposalReviewDecision:
            raise TypeError("decision must be exact ProposalReviewDecision")
        _validate_target_af_sampling_authority(self.proposal)
        if (
            self.decision.proposal_sha256 != self.proposal.proposal_sha256
            or self.decision.decision != "accepted"
        ):
            raise TargetAfError("target-AF publication requires its exact accepted proposal")
        model = _model_from_target_proposal(self.proposal, self.decision)
        object.__setattr__(self, "model", model)
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "proposal_sha256": self.proposal.proposal_sha256,
                    "decision_sha256": self.decision.decision_sha256,
                    "model_sha256": model.content_sha256,
                }
            ),
        )


def build_target_autofluorescence_model(
    *,
    proposal: TargetAutofluorescenceProposal,
    decision: ProposalReviewDecision,
) -> TargetAutofluorescenceArtifact:
    return TargetAutofluorescenceArtifact(proposal, decision)


@dataclass(frozen=True, slots=True)
class RunBasisCompatibilityEvidence:
    structural_report: StructuralCompatibilityReport
    quantitative_report: QuantitativeTransformabilityReport
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.structural_report) is not StructuralCompatibilityReport:
            raise TypeError("structural_report must be exact typed evidence")
        if type(self.quantitative_report) is not QuantitativeTransformabilityReport:
            raise TypeError("quantitative_report must be exact typed evidence")
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "structural_report_sha256": self.structural_report.content_sha256,
                    "quantitative_report_sha256": self.quantitative_report.content_sha256,
                    "structurally_compatible": self.structural_report.compatible,
                    "quantitatively_transformable": self.quantitative_report.transformable,
                }
            ),
        )


def _control_run_basis_columns(*, channel_fluors, library, target, af_columns):
    """Assemble the same numerical columns for pending and accepted control AF."""
    identities = resolve_channel_fluor_identities(channel_fluors)
    axis, signature, row_order = align_library_measurements(library, target.axis)
    compatibility = RunBasisCompatibilityEvidence(
        structural_report=compare_structural_compatibility(
            reference_axis=axis, candidate_axis=target.axis,
            reference_signature=library.structural_signature,
            candidate_signature=target.structural_signature),
        quantitative_report=compare_quantitative_transformability(
            reference_axis=axis, candidate_axis=target.axis,
            reference_signature=signature, candidate_signature=target.quantitative_signature),
    )
    if not compatibility.structural_report.compatible:
        raise TargetAfError("run-basis structural compatibility failed")
    if not compatibility.quantitative_report.transformable:
        raise TargetAfError("run-basis quantitative compatibility failed")
    by_fluor = {item.fluorophore_identity: item for item in library.spectra}
    missing = tuple(item for item in identities if item not in by_fluor)
    if missing:
        raise TargetAfError(f"Channel.Fluor identities are absent from library: {missing!r}")
    library_source = BasisSourceLineage(
        "fluorophore_reference_library", library.artifact_id, library.content_sha256, None)
    columns = tuple(
        ResolvedBasisColumn(
            item.component_identity, item.fluorophore_identity, item.component_kind,
            tuple(item.values[i] for i in row_order), item.normalization_convention,
            item.abundance_rescaling_factor, library_source,
        )
        for item in (by_fluor[identity] for identity in identities)
    ) + af_columns
    return columns, compatibility


def preview_control_run_basis(*, channel_fluors, fluorophore_library, target_af_proposal, qc_policy):
    """Calculate review values from pending AF without granting accepted authority."""
    if type(target_af_proposal) is not TargetAutofluorescenceProposal:
        raise TypeError("target_af_proposal must be exact pending proposal")
    if type(fluorophore_library) is not FluorophoreReferenceLibrary:
        raise TypeError("fluorophore_library must be exact accepted library")
    proposal = target_af_proposal
    source = BasisSourceLineage("target_autofluorescence_model", proposal.artifact_id,
                              proposal.proposal_sha256, proposal.run_id)
    columns, compatibility = _control_run_basis_columns(
        channel_fluors=channel_fluors, library=fluorophore_library,
        target=proposal.control_binding.target_input,
        af_columns=tuple(ResolvedBasisColumn(component.component_identity, None,
            "target_autofluorescence", component.values, "unit_l1", 1.0, source)
            for component in proposal.estimation.components),
    )
    return columns, evaluate_run_basis_qc(columns, proposal.control_binding.target_input.axis,
                                          qc_policy), compatibility


@dataclass(frozen=True, slots=True)
class RunBasisCandidate:
    source_choice: str
    channel_fluors: tuple[str, ...]
    fluorophore_library: FluorophoreReferenceLibrary
    target_af_artifact: TargetAutofluorescenceArtifact
    qc_policy: FixedBasisQcPolicy
    run_id: str = field(init=False)
    target_image_sha256: str = field(init=False)
    axis: SpectralMeasurementAxis = field(init=False)
    columns: tuple[ResolvedBasisColumn, ...] = field(init=False)
    qc: FixedBasisQcResult = field(init=False)
    compatibility: RunBasisCompatibilityEvidence = field(init=False)
    source_artifact_sha256s: tuple[str, ...] = field(init=False)
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the RunBasisCandidate fields when the record is constructed.

        The checks cover ``source_choice``, ``target_af_artifact``, ``channel_fluors``,
        ``fluorophore_library``, ``qc_policy``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        if self.source_choice not in {"import_reference_library", "construct_reference_library"}:
            raise TargetAfError("run-basis source choice is unsupported")
        if type(self.fluorophore_library) is not FluorophoreReferenceLibrary:
            raise TypeError("fluorophore_library must be exact accepted artifact")
        if type(self.target_af_artifact) is not TargetAutofluorescenceArtifact:
            raise TypeError("target_af_artifact must be exact authoritative artifact")
        validate_task7_artifact_authority(self.target_af_artifact)
        identities = resolve_channel_fluor_identities(self.channel_fluors)
        model = self.target_af_artifact.model
        target_source = BasisSourceLineage(
            "target_autofluorescence_model",
            model.artifact_id,
            self.target_af_artifact.content_sha256,
            model.run_id,
        )
        columns, compatibility = _control_run_basis_columns(
            channel_fluors=self.channel_fluors, library=self.fluorophore_library, target=model,
            af_columns=tuple(
                ResolvedBasisColumn(
                    item.component_identity,
                    None,
                    item.component_kind,
                    item.values,
                    item.normalization_convention,
                    item.abundance_rescaling_factor,
                    target_source,
                )
                for item in model.endmembers
            ),
        )
        qc = evaluate_run_basis_qc(columns, model.axis, self.qc_policy)
        sources = (
            self.fluorophore_library.content_sha256,
            self.target_af_artifact.content_sha256,
        )
        object.__setattr__(self, "run_id", model.run_id)
        object.__setattr__(self, "target_image_sha256", model.target_image_sha256)
        object.__setattr__(self, "axis", model.axis)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "qc", qc)
        object.__setattr__(self, "compatibility", compatibility)
        object.__setattr__(self, "source_artifact_sha256s", sources)
        object.__setattr__(
            self,
            "candidate_sha256",
            typed_checksum_sha256(
                {
                    "source_choice": self.source_choice,
                    "channel_fluors": list(identities),
                    "library_sha256": self.fluorophore_library.content_sha256,
                    "target_af_sha256": self.target_af_artifact.content_sha256,
                    "target_image_sha256": model.target_image_sha256,
                    "column_sha256s": [item.spectrum_sha256 for item in columns],
                    "qc_sha256": qc.evidence_sha256,
                    "compatibility_sha256": compatibility.evidence_sha256,
                }
            ),
        )

    @property
    def component_order(self) -> tuple[str, ...]:
        return tuple(item.component_identity for item in self.columns)


def assemble_fixed_run_basis_candidate(
    *,
    source_choice: str,
    channel_fluors: tuple[str, ...],
    fluorophore_library: FluorophoreReferenceLibrary,
    target_af_artifact: TargetAutofluorescenceArtifact,
    qc_policy: FixedBasisQcPolicy,
) -> RunBasisCandidate:
    return RunBasisCandidate(
        source_choice,
        channel_fluors,
        fluorophore_library,
        target_af_artifact,
        qc_policy,
    )


@dataclass(frozen=True, slots=True)
class ResolvedRunBasisArtifact:
    candidate: RunBasisCandidate
    decision: ProposalReviewDecision
    artifact_id: str
    created_at: str
    basis: ResolvedRunBasis = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the ResolvedRunBasisArtifact fields when the record is constructed.

        The checks cover ``candidate``, ``artifact_id``, ``created_at``, ``decision``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if type(self.candidate) is not RunBasisCandidate:
            raise TypeError("candidate must be exact RunBasisCandidate")
        validate_task7_artifact_authority(self.candidate)
        if type(self.decision) is not ProposalReviewDecision:
            raise TypeError("decision must be exact ProposalReviewDecision")
        if (
            self.decision.proposal_sha256 != self.candidate.candidate_sha256
            or self.decision.decision != "accepted"
        ):
            raise TargetAfError("resolved basis decision does not bind exact candidate")
        if self.candidate.qc.disposition == "failed":
            raise TargetAfError("failed run-basis QC cannot be accepted")
        _text(self.artifact_id, "run-basis artifact ID")
        _timestamp(self.created_at, "run-basis created_at")
        review = ReviewRecord("accepted", self.decision.reviewer_id, self.decision.reviewed_at)
        basis = ResolvedRunBasis(
            self.candidate.run_id,
            self.candidate.axis,
            self.candidate.columns,
            review,
        )
        object.__setattr__(self, "basis", basis)
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "artifact_id": self.artifact_id,
                    "created_at": self.created_at,
                    "candidate_sha256": self.candidate.candidate_sha256,
                    "decision_sha256": self.decision.decision_sha256,
                    "basis": _json_primitive(basis),
                }
            ),
        )

    @property
    def source_choice(self) -> str:
        return self.candidate.source_choice

    @property
    def target_image_sha256(self) -> str:
        return self.candidate.target_image_sha256

    @property
    def qc(self) -> FixedBasisQcResult:
        return self.candidate.qc

    @property
    def source_artifact_sha256s(self) -> tuple[str, ...]:
        return self.candidate.source_artifact_sha256s


def validate_task7_artifact_authority(
    artifact: TargetAutofluorescenceArtifact | RunBasisCandidate | ResolvedRunBasisArtifact,
) -> None:
    """Recursively reconstruct all retained evidence and reject stale live state."""

    allowed = {
        TargetAutofluorescenceArtifact,
        RunBasisCandidate,
        ResolvedRunBasisArtifact,
    }
    if type(artifact) not in allowed:
        raise TypeError("artifact must be an exact Task-7 authority record")
    from . import reference_evidence_wire

    try:
        rebuilt = reference_evidence_wire._decode(
            reference_evidence_wire._encode(artifact),
            reference_evidence_wire._registry(),
        )
    except Exception as exc:
        raise TargetAfError(
            "Task-7 authority validation failed for stale or invalid evidence"
        ) from exc
    if type(rebuilt) is not type(artifact) or rebuilt != artifact:
        raise TargetAfError("Task-7 authority validation failed for stale or invalid evidence")


def resolved_run_basis_handoff(artifact: ResolvedRunBasisArtifact) -> ResolvedRunBasis:
    """Return the exact basis only after recursive authoritative revalidation."""

    if type(artifact) is not ResolvedRunBasisArtifact:
        raise TypeError("artifact must be exact ResolvedRunBasisArtifact")
    validate_task7_artifact_authority(artifact)
    return artifact.basis


def accept_resolved_run_basis(
    *,
    artifact_id: str,
    created_at: str,
    candidate: RunBasisCandidate,
    decision: ProposalReviewDecision,
) -> ResolvedRunBasisArtifact:
    artifact = ResolvedRunBasisArtifact(candidate, decision, artifact_id, created_at)
    validate_task7_artifact_authority(artifact)
    return artifact


def _dumps_task7_artifact(record: object, *, schema_name: str) -> str:
    from . import reference_evidence_wire

    if type(record) not in {
        TargetAutofluorescenceArtifact,
        ResolvedRunBasisArtifact,
    }:
        raise TypeError("record must be an exact persisted Task-7 artifact")
    validate_task7_artifact_authority(record)
    encoded = reference_evidence_wire._encode(record)
    payload = {"schema_name": schema_name, "schema_version": 3, "record": encoded}
    envelope = {**payload, "content_sha256": typed_checksum_sha256(payload)}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _loads_task7_artifact(source: str | bytes, *, schema_name: str, record_type: type):
    from . import reference_evidence_wire

    try:
        root = json.loads(
            source,
            object_pairs_hook=reference_evidence_wire._reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        envelope = reference_evidence_wire._mapping(
            root,
            frozenset({"schema_name", "schema_version", "record", "content_sha256"}),
            "Task-7 envelope",
        )
        if (
            envelope["schema_name"] == schema_name
            and type(envelope["schema_version"]) is int
            and envelope["schema_version"] in (1, 2)
        ):
            raise TargetAfError("Task-7 artifact versions 1 and 2 require source-backed migration")
        if envelope["schema_name"] != schema_name or envelope["schema_version"] != 3:
            raise TargetAfError("unsupported Task-7 artifact schema")
        payload = {
            "schema_name": envelope["schema_name"],
            "schema_version": envelope["schema_version"],
            "record": envelope["record"],
        }
        if typed_checksum_sha256(payload) != envelope["content_sha256"]:
            raise TargetAfError("Task-7 artifact content SHA-256 does not match")
        result = reference_evidence_wire._decode(
            envelope["record"], reference_evidence_wire._registry()
        )
        if type(result) is not record_type:
            raise TargetAfError("Task-7 artifact root type differs")
        return result
    except TargetAfError:
        raise
    except Exception as exc:
        raise TargetAfError("invalid strict Task-7 artifact") from exc


def dumps_target_autofluorescence_artifact(artifact: TargetAutofluorescenceArtifact) -> str:
    if type(artifact) is not TargetAutofluorescenceArtifact:
        raise TypeError("artifact must be exact TargetAutofluorescenceArtifact")
    return _dumps_task7_artifact(artifact, schema_name="mif_pipeline.target_af_artifact")


def loads_target_autofluorescence_artifact(source: str | bytes) -> TargetAutofluorescenceArtifact:
    return _loads_task7_artifact(
        source,
        schema_name="mif_pipeline.target_af_artifact",
        record_type=TargetAutofluorescenceArtifact,
    )


def write_target_autofluorescence_artifact(
    artifact: TargetAutofluorescenceArtifact,
    path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    if type(artifact) is not TargetAutofluorescenceArtifact:
        raise TypeError("artifact must be exact TargetAutofluorescenceArtifact")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"target-AF artifact exists: {destination}")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(dumps_target_autofluorescence_artifact(artifact))
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, destination)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                raise FileExistsError(f"target-AF artifact exists: {destination}") from None
            temporary_path.unlink()
            temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


def dumps_resolved_run_basis_artifact(artifact: ResolvedRunBasisArtifact) -> str:
    if type(artifact) is not ResolvedRunBasisArtifact:
        raise TypeError("artifact must be exact ResolvedRunBasisArtifact")
    return _dumps_task7_artifact(artifact, schema_name="mif_pipeline.resolved_run_basis")


def loads_resolved_run_basis_artifact(source: str | bytes) -> ResolvedRunBasisArtifact:
    return _loads_task7_artifact(
        source,
        schema_name="mif_pipeline.resolved_run_basis",
        record_type=ResolvedRunBasisArtifact,
    )


__all__ = [
    "FixedBasisColumnEvidence",
    "FixedBasisQcPolicy",
    "FixedBasisQcResult",
    "ProposalReviewDecision",
    "ReviewedTargetAfRoiSelection",
    "ResolvedRunBasisArtifact",
    "RunBasisCandidate",
    "RunBasisCompatibilityEvidence",
    "TargetAfError",
    "TargetAfEndmemberCandidate",
    "TargetAfComponentEstimate",
    "TargetAfEstimationPolicy",
    "TargetAfEstimationResult",
    "TargetAfRoiProposal",
    "TargetAutofluorescenceArtifact",
    "TargetAutofluorescenceProposal",
    "TargetControlBinding",
    "accept_resolved_run_basis",
    "assemble_fixed_run_basis_candidate",
    "build_target_autofluorescence_model",
    "dumps_resolved_run_basis_artifact",
    "dumps_target_autofluorescence_artifact",
    "estimate_target_af_candidates",
    "evaluate_run_basis_qc",
    "evaluate_target_af_qc",
    "loads_resolved_run_basis_artifact",
    "loads_target_autofluorescence_artifact",
    "propose_target_autofluorescence_model",
    "resolved_run_basis_handoff",
    "validate_target_control_binding",
    "validate_task7_artifact_authority",
    "write_resolved_run_basis_artifact",
    "write_target_autofluorescence_artifact",
]
