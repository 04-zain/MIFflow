"""Explicit SVD, conditioning, and pairwise spectral-shape QC.

The `SpectrumMatrixQcResult` record carries `policy`, `columns`, `measurement_ids`,
`singular_values`, `rank_tolerance` and its remaining declared fields.

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

import numpy as np

from .purification import FluorophoreConsensusCandidate
from .reference_models import PairwiseSpectrumQc
from .typed_checksum import typed_checksum_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================
#                                  Reference qc
# ==============================================================================


class ReferenceMatrixQcError(ValueError):
    """Matrix-QC inputs are malformed or internally contradictory."""


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
    raise TypeError(f"unsupported QC evidence value {type(value).__name__}")


def _digest(value: object, omit: str | None = None) -> str:
    payload = (
        _primitive(value)
        if omit is None
        else {
            item.name: _primitive(getattr(value, item.name))
            for item in _record_fields(value)
            if item.name != omit
        }
    )
    return typed_checksum_sha256(payload)


def _typed_tuple(value: object, item_type: type, label: str, *, empty: bool = False):
    if type(value) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    if not value and not empty:
        raise ValueError(f"{label} must not be empty")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{label} must contain exact {item_type.__name__} records")
    return value


@dataclass(frozen=True, slots=True)
class SpectrumMatrixColumn:
    """One stored unit-L1 column with immutable regional-consensus evidence identity."""

    source_candidate: FluorophoreConsensusCandidate
    component_identity: str = field(init=False)
    fluorophore_identity: str = field(init=False)
    measurement_ids: tuple[str, ...] = field(init=False)
    values: tuple[float, ...] = field(init=False)
    source_evidence_sha256: str = field(init=False)
    column_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.source_candidate) is not FluorophoreConsensusCandidate:
            raise TypeError("source_candidate must be exact FluorophoreConsensusCandidate evidence")
        candidate = self.source_candidate
        object.__setattr__(self, "component_identity", candidate.component_identity)
        object.__setattr__(self, "fluorophore_identity", candidate.fluorophore_identity)
        object.__setattr__(self, "measurement_ids", candidate.measurement_ids)
        total = math.fsum(candidate.values)
        object.__setattr__(self, "values", tuple(value / total for value in candidate.values))
        object.__setattr__(self, "source_evidence_sha256", candidate.candidate_sha256)
        object.__setattr__(self, "column_sha256", _digest(self, "column_sha256"))

    @classmethod
    def from_consensus(cls, candidate: FluorophoreConsensusCandidate) -> "SpectrumMatrixColumn":
        if type(candidate) is not FluorophoreConsensusCandidate:
            raise TypeError("candidate must be exact FluorophoreConsensusCandidate")
        return cls(source_candidate=candidate)


@dataclass(frozen=True, slots=True)
class SpectrumMatrixQcPolicy:
    policy_version: str
    svd_absolute_tolerance: float
    svd_relative_tolerance: float
    cosine_review_threshold: float
    cosine_fail_threshold: float
    condition_review_threshold: float
    condition_fail_threshold: float
    normalization_tolerance: float
    authority_sha256: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "policy_version")
        absolute = _finite(self.svd_absolute_tolerance, "svd_absolute_tolerance")
        relative = _finite(self.svd_relative_tolerance, "svd_relative_tolerance")
        if absolute < 0.0 or relative < 0.0:
            raise ValueError("SVD tolerances must be nonnegative")
        review_cosine = _finite(self.cosine_review_threshold, "cosine_review_threshold")
        fail_cosine = _finite(self.cosine_fail_threshold, "cosine_fail_threshold")
        if not 0.0 <= review_cosine <= fail_cosine <= 1.0:
            raise ValueError("cosine thresholds must satisfy 0 <= review <= fail <= 1")
        review_condition = _finite(self.condition_review_threshold, "condition_review_threshold")
        fail_condition = _finite(self.condition_fail_threshold, "condition_fail_threshold")
        if not 1.0 <= review_condition <= fail_condition:
            raise ValueError("condition thresholds must satisfy 1 <= review <= fail")
        normalization = _finite(self.normalization_tolerance, "normalization_tolerance")
        if normalization < 0.0:
            raise ValueError("normalization_tolerance must be nonnegative")
        _sha(self.authority_sha256, "authority_sha256")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class QcThresholdMetric:
    name: str
    value: float
    direction: str
    review_threshold: float | None
    fail_threshold: float
    disposition: str
    authority_sha256: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.name, "metric name")
        _finite(self.value, "metric value")
        if self.direction not in {"maximum", "minimum"}:
            raise ValueError("unsupported threshold direction")
        if self.review_threshold is not None:
            _finite(self.review_threshold, "review_threshold")
        _finite(self.fail_threshold, "fail_threshold")
        if self.disposition not in {"pass", "review_required", "failed"}:
            raise ValueError("unsupported metric disposition")
        _sha(self.authority_sha256, "authority_sha256")
        if self.direction == "maximum":
            expected = (
                "failed"
                if self.value >= self.fail_threshold
                else (
                    "review_required"
                    if self.review_threshold is not None and self.value >= self.review_threshold
                    else "pass"
                )
            )
        else:
            expected = (
                "failed"
                if self.value < self.fail_threshold
                else (
                    "review_required"
                    if self.review_threshold is not None and self.value < self.review_threshold
                    else "pass"
                )
            )
        if self.disposition != expected:
            raise ValueError("metric disposition contradicts its threshold")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class SpectrumMatrixQcResult:
    policy: SpectrumMatrixQcPolicy
    columns: tuple[SpectrumMatrixColumn, ...]
    measurement_ids: tuple[str, ...]
    singular_values: tuple[float, ...]
    rank_tolerance: float
    rank: int
    condition_number: float | None
    pairwise_spectra: tuple[PairwiseSpectrumQc, ...]
    metrics: tuple[QcThresholdMetric, ...]
    disposition: str
    reason_codes: tuple[str, ...]
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the SpectrumMatrixQcResult fields when the record is constructed.

        The checks cover ``singular_values``, ``columns``, ``pairwise_spectra``,
        ``metrics``, ``disposition``, ``policy``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if type(self.policy) is not SpectrumMatrixQcPolicy:
            raise TypeError("policy must be exact SpectrumMatrixQcPolicy")
        _typed_tuple(self.columns, SpectrumMatrixColumn, "columns")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise TypeError("measurement_ids must be a nonempty tuple")
        if type(self.singular_values) is not tuple or not self.singular_values:
            raise TypeError("singular_values must be a nonempty tuple")
        if any(type(item) is not float for item in self.singular_values):
            raise TypeError("singular_values must contain exact floats")
        for item in self.singular_values:
            if _finite(item, "singular value") < 0.0:
                raise ValueError("singular values must be nonnegative")
        if _finite(self.rank_tolerance, "rank_tolerance") < 0.0:
            raise ValueError("rank_tolerance must be nonnegative")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 0:
            raise ValueError("rank must be a nonnegative integer")
        if (
            self.condition_number is not None
            and _finite(self.condition_number, "condition_number") < 1.0
        ):
            raise ValueError("finite condition_number must be at least one")
        _typed_tuple(self.pairwise_spectra, PairwiseSpectrumQc, "pairwise_spectra", empty=True)
        _typed_tuple(self.metrics, QcThresholdMetric, "metrics")
        if self.disposition not in {"pass", "review_required", "failed"}:
            raise ValueError("unsupported QC disposition")
        if type(self.reason_codes) is not tuple or any(
            type(item) is not str or not item for item in self.reason_codes
        ):
            raise TypeError("reason_codes must be a tuple of exact strings")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")
        expected = _evaluate(self.columns, self.policy)
        actual = (
            self.measurement_ids,
            self.singular_values,
            self.rank_tolerance,
            self.rank,
            self.condition_number,
            self.pairwise_spectra,
            self.metrics,
            self.disposition,
            self.reason_codes,
        )
        if actual != expected:
            raise ReferenceMatrixQcError("matrix-QC result fields contradict derived metrics")
        object.__setattr__(self, "evidence_sha256", _digest(self, "evidence_sha256"))


def _metric_disposition(value: float, review: float, fail: float) -> str:
    if value >= fail:
        return "failed"
    if value >= review:
        return "review_required"
    return "pass"


def _evaluate(
    columns: tuple[SpectrumMatrixColumn, ...],
    policy: SpectrumMatrixQcPolicy,
) -> tuple[
    tuple[str, ...],
    tuple[float, ...],
    float,
    int,
    float | None,
    tuple[PairwiseSpectrumQc, ...],
    tuple[QcThresholdMetric, ...],
    str,
    tuple[str, ...],
]:
    axis = columns[0].measurement_ids
    if any(item.measurement_ids != axis for item in columns):
        raise ReferenceMatrixQcError("matrix-QC column axes differ")
    for item in columns:
        norm = math.fsum(item.values)
        if abs(norm - 1.0) > policy.normalization_tolerance:
            raise ReferenceMatrixQcError("matrix-QC column exceeds policy normalization tolerance")
    identities = tuple(item.component_identity for item in columns)
    fluorophores = tuple(item.fluorophore_identity for item in columns)
    if len(identities) != len(set(identities)) or len(fluorophores) != len(set(fluorophores)):
        raise ReferenceMatrixQcError(
            "matrix-QC component and fluorophore identities must be unique"
        )
    matrix = np.asarray(tuple(item.values for item in columns), dtype=np.float64).T
    singular = np.linalg.svd(matrix, compute_uv=False)
    singular_values = tuple(float(item) for item in singular)
    tau = float(
        max(policy.svd_absolute_tolerance, policy.svd_relative_tolerance * singular_values[0])
    )
    rank = sum(item > tau for item in singular_values)
    full_rank = len(columns) <= len(axis) and rank == len(columns)
    condition = float(singular_values[0] / singular_values[len(columns) - 1]) if full_rank else None
    pairs: list[PairwiseSpectrumQc] = []
    metrics: list[QcThresholdMetric] = []
    reasons: list[str] = []
    count_disposition = "failed" if len(columns) > len(axis) else "pass"
    metrics.append(
        QcThresholdMetric(
            name="component_count",
            value=float(len(columns)),
            direction="maximum",
            review_threshold=None,
            fail_threshold=float(len(axis) + 1),
            disposition=count_disposition,
            authority_sha256=policy.authority_sha256,
        )
    )
    if count_disposition == "failed":
        reasons.append("component_count_exceeds_measurements")
    rank_disposition = "pass" if rank == len(columns) else "failed"
    metrics.append(
        QcThresholdMetric(
            name="column_rank",
            value=float(rank),
            direction="minimum",
            review_threshold=None,
            fail_threshold=float(len(columns)),
            disposition=rank_disposition,
            authority_sha256=policy.authority_sha256,
        )
    )
    if rank_disposition == "failed":
        reasons.append("rank_deficient")
    if condition is not None:
        disposition = _metric_disposition(
            condition, policy.condition_review_threshold, policy.condition_fail_threshold
        )
        metrics.append(
            QcThresholdMetric(
                name="shape_condition_number",
                value=condition,
                direction="maximum",
                review_threshold=policy.condition_review_threshold,
                fail_threshold=policy.condition_fail_threshold,
                disposition=disposition,
                authority_sha256=policy.authority_sha256,
            )
        )
        if disposition != "pass":
            reasons.append(f"condition_number_{disposition}")
    for first_index, first in enumerate(columns):
        for second in columns[first_index + 1 :]:
            cosine = float(
                min(
                    1.0,
                    max(
                        0.0,
                        math.fsum(a * b for a, b in zip(first.values, second.values, strict=True))
                        / (math.hypot(*first.values) * math.hypot(*second.values)),
                    ),
                )
            )
            angle = float(math.degrees(math.acos(cosine)))
            pairs.append(
                PairwiseSpectrumQc(
                    first.component_identity, second.component_identity, cosine, angle
                )
            )
            disposition = _metric_disposition(
                cosine, policy.cosine_review_threshold, policy.cosine_fail_threshold
            )
            metrics.append(
                QcThresholdMetric(
                    name=f"pairwise_cosine:{first.component_identity}:{second.component_identity}",
                    value=cosine,
                    direction="maximum",
                    review_threshold=policy.cosine_review_threshold,
                    fail_threshold=policy.cosine_fail_threshold,
                    disposition=disposition,
                    authority_sha256=policy.authority_sha256,
                )
            )
            if disposition != "pass":
                reasons.append(
                    f"pairwise_similarity_{disposition}:{first.component_identity}:{second.component_identity}"
                )
    if any(item.disposition == "failed" for item in metrics):
        overall = "failed"
    elif any(item.disposition == "review_required" for item in metrics):
        overall = "review_required"
    else:
        overall = "pass"
    return (
        axis,
        singular_values,
        tau,
        rank,
        condition,
        tuple(pairs),
        tuple(metrics),
        overall,
        tuple(reasons),
    )


def evaluate_spectrum_matrix_qc(
    columns: tuple[SpectrumMatrixColumn, ...],
    policy: SpectrumMatrixQcPolicy,
) -> SpectrumMatrixQcResult:
    """Evaluate a reusable spectral-shape matrix under an exact SVD policy."""

    typed = _typed_tuple(columns, SpectrumMatrixColumn, "columns")
    if type(policy) is not SpectrumMatrixQcPolicy:
        raise TypeError("policy must be exact SpectrumMatrixQcPolicy")
    derived = _evaluate(typed, policy)
    return SpectrumMatrixQcResult(
        policy=policy,
        columns=typed,
        measurement_ids=derived[0],
        singular_values=derived[1],
        rank_tolerance=derived[2],
        rank=derived[3],
        condition_number=derived[4],
        pairwise_spectra=derived[5],
        metrics=derived[6],
        disposition=derived[7],
        reason_codes=derived[8],
    )


def evaluate_library_qc(
    columns: tuple[SpectrumMatrixColumn, ...],
    policy: SpectrumMatrixQcPolicy,
) -> SpectrumMatrixQcResult:
    """public name for reusable spectral-library shape QC."""

    return evaluate_spectrum_matrix_qc(columns, policy)


__all__ = [
    "QcThresholdMetric",
    "ReferenceMatrixQcError",
    "SpectrumMatrixColumn",
    "SpectrumMatrixQcPolicy",
    "SpectrumMatrixQcResult",
    "evaluate_library_qc",
    "evaluate_spectrum_matrix_qc",
]
