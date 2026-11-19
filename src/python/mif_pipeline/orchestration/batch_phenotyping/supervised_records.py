"""Immutable supervised prediction and completion records.

The `SupervisedNativePrediction` record carries `sample_id`, `project_entry_id`, `slide_id`,
`section_id`, `tissue_generation_id` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType

from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactIdentityContract,
)

from ._common import BatchPhenotypingError, canonical_bytes, digest

PREDICTION_SCHEMA = "mif-batch-supervised-native-prediction/v1"
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                               Supervised records
# ==============================================================================


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchPhenotypingError(f"{field} must be non-empty canonical text")
    return value


def _sha(value: object, field: str) -> str:
    result = _text(value, field)
    if _SHA_RE.fullmatch(result) is None:
        raise BatchPhenotypingError(f"{field} must be a lowercase SHA-256")
    return result


def _binding_key(value: AnalysisDomainBinding) -> tuple[object, ...]:
    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.analysis_domain_revision_sha256,
        value.domain_set_sha256,
        value.project_entry_id,
    )


def _canonical_rows(
    value: Sequence[Mapping[str, object]],
    *,
    class_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "cell_id",
            "class_id",
            "confidence",
        }:
            raise BatchPhenotypingError("supervised prediction row is invalid")
        cell_id = _text(raw["cell_id"], "prediction cell_id")
        class_id = _text(raw["class_id"], "prediction class_id")
        confidence = raw["confidence"]
        if (
            class_id not in class_ids
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise BatchPhenotypingError("supervised prediction class or confidence is invalid")
        rows.append(
            MappingProxyType(
                {
                    "cell_id": cell_id,
                    "class_id": class_id,
                    "confidence": float(confidence),
                }
            )
        )
    result = tuple(sorted(rows, key=lambda item: str(item["cell_id"])))
    if len({item["cell_id"] for item in result}) != len(result):
        raise BatchPhenotypingError("supervised prediction cells must be unique")
    return result


@dataclass(frozen=True, slots=True)
class SupervisedNativePrediction:
    sample_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    classifier_record_sha256: str
    classifier_id: str
    class_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    measurement_result_ref_sha256: str
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    rows: tuple[Mapping[str, object], ...]
    rows_sha256: str
    prediction_sha256: str

    def __post_init__(self) -> None:
        """Validate the SupervisedNativePrediction fields when the record is constructed.

        The checks cover ``analysis_domain_bindings``, ``rows``, ``rows_sha256``,
        ``prediction_sha256``, ``class_ids``, ``sample_id``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        for field in (
            "sample_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
            "classifier_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "classifier_record_sha256",
            "measurement_result_ref_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
        ):
            object.__setattr__(self, field, _sha(getattr(self, field), field))
        classes = tuple(_text(item, "class_id") for item in self.class_ids)
        if not classes or len(set(classes)) != len(classes):
            raise BatchPhenotypingError("prediction class_ids must be unique")
        bindings = tuple(self.analysis_domain_bindings)
        if (
            not bindings
            or not all(isinstance(item, AnalysisDomainBinding) for item in bindings)
            or bindings != tuple(sorted(bindings, key=_binding_key))
            or len({_binding_key(item) for item in bindings}) != len(bindings)
            or any(
                (item.sample_id, item.slide_id, item.project_entry_id)
                != (self.sample_id, self.slide_id, self.project_entry_id)
                for item in bindings
            )
        ):
            raise BatchPhenotypingError("prediction analysis-domain bindings are invalid")
        rows = _canonical_rows(self.rows, class_ids=classes)
        rows_sha256 = digest([dict(item) for item in rows])
        if _sha(self.rows_sha256, "rows_sha256") != rows_sha256:
            raise BatchPhenotypingError("prediction rows_sha256 is not canonical")
        object.__setattr__(self, "class_ids", classes)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "rows", rows)
        expected = digest(self._unsigned_mapping())
        if _sha(self.prediction_sha256, "prediction_sha256") != expected:
            raise BatchPhenotypingError("prediction_sha256 is not canonical")

    @classmethod
    def create(
        cls,
        *,
        sample_id: str,
        project_entry_id: str,
        slide_id: str,
        section_id: str,
        tissue_generation_id: str,
        classifier_record_sha256: str,
        classifier_id: str,
        class_ids: Sequence[str],
        analysis_domain_bindings: Sequence[AnalysisDomainBinding],
        measurement_result_ref_sha256: str,
        cell_ids_sha256: str,
        cell_to_domain_membership_sha256: str,
        rows: Sequence[Mapping[str, object]],
    ) -> "SupervisedNativePrediction":
        classes = tuple(class_ids)
        canonical_rows = _canonical_rows(rows, class_ids=classes)
        values = {
            "sample_id": sample_id,
            "project_entry_id": project_entry_id,
            "slide_id": slide_id,
            "section_id": section_id,
            "tissue_generation_id": tissue_generation_id,
            "classifier_record_sha256": classifier_record_sha256,
            "classifier_id": classifier_id,
            "class_ids": classes,
            "analysis_domain_bindings": tuple(analysis_domain_bindings),
            "measurement_result_ref_sha256": measurement_result_ref_sha256,
            "cell_ids_sha256": cell_ids_sha256,
            "cell_to_domain_membership_sha256": cell_to_domain_membership_sha256,
            "rows": canonical_rows,
            "rows_sha256": digest([dict(item) for item in canonical_rows]),
        }
        candidate = object.__new__(cls)
        for field, value in values.items():
            object.__setattr__(candidate, field, value)
        object.__setattr__(candidate, "prediction_sha256", "0" * 64)
        unsigned = candidate._unsigned_mapping()
        return cls(**values, prediction_sha256=digest(unsigned))

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PREDICTION_SCHEMA,
            "sample_id": self.sample_id,
            "project_entry_id": self.project_entry_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "tissue_generation_id": self.tissue_generation_id,
            "classifier_record_sha256": self.classifier_record_sha256,
            "classifier_id": self.classifier_id,
            "class_ids": list(self.class_ids),
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "measurement_result_ref_sha256": self.measurement_result_ref_sha256,
            "cell_ids_sha256": self.cell_ids_sha256,
            "cell_to_domain_membership_sha256": (self.cell_to_domain_membership_sha256),
            "cell_count": len(self.rows),
            "rows": [dict(item) for item in self.rows],
            "rows_sha256": self.rows_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._unsigned_mapping()

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedNativePrediction":
        """Decode the serialized fields into a validated SupervisedNativePrediction record.

        The mapping is checked against the declared field inventory, including
        ``schema_version``, ``sample_id``, ``project_entry_id``, ``slide_id``,
        ``section_id``, ``tissue_generation_id``. Nested values are decoded through the
        corresponding field validators before the record is constructed."""

        if not isinstance(value, Mapping):
            raise BatchPhenotypingError("prediction payload must be a mapping")
        expected = {
            "schema_version",
            "sample_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
            "classifier_record_sha256",
            "classifier_id",
            "class_ids",
            "analysis_domain_bindings",
            "measurement_result_ref_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "cell_count",
            "rows",
            "rows_sha256",
        }
        if set(value) != expected or value.get("schema_version") != PREDICTION_SCHEMA:
            raise BatchPhenotypingError("prediction payload schema is invalid")
        try:
            result = cls(
                sample_id=value["sample_id"],
                project_entry_id=value["project_entry_id"],
                slide_id=value["slide_id"],
                section_id=value["section_id"],
                tissue_generation_id=value["tissue_generation_id"],
                classifier_record_sha256=value["classifier_record_sha256"],
                classifier_id=value["classifier_id"],
                class_ids=tuple(value["class_ids"]),
                analysis_domain_bindings=tuple(
                    AnalysisDomainBinding.from_mapping(item)
                    for item in value["analysis_domain_bindings"]
                ),
                measurement_result_ref_sha256=value["measurement_result_ref_sha256"],
                cell_ids_sha256=value["cell_ids_sha256"],
                cell_to_domain_membership_sha256=value["cell_to_domain_membership_sha256"],
                rows=tuple(value["rows"]),
                rows_sha256=value["rows_sha256"],
                prediction_sha256=digest(dict(value)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError("prediction payload is invalid") from exc
        if value["cell_count"] != len(result.rows):
            raise BatchPhenotypingError("prediction cell_count differs")
        return result


@dataclass(frozen=True, slots=True)
class StoredSupervisedNativePrediction:
    prediction: SupervisedNativePrediction
    locator: Path
    sha256: str
    byte_count: int


class SupervisedNativePredictionStore:
    """Write and reopen immutable prediction bytes by their exact digest."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=False)

    def publish(self, prediction: SupervisedNativePrediction) -> StoredSupervisedNativePrediction:
        if not isinstance(prediction, SupervisedNativePrediction):
            raise BatchPhenotypingError("prediction store requires a typed payload")
        payload = canonical_bytes(prediction.to_mapping())
        sha256 = hashlib.sha256(payload).hexdigest()
        if sha256 != prediction.prediction_sha256:
            raise BatchPhenotypingError(
                "prediction object address is not its canonical byte digest"
            )
        locator = self.root / "objects" / f"{sha256}.json"
        locator.parent.mkdir(parents=True, exist_ok=True)
        if locator.exists():
            if locator.read_bytes() != payload:
                raise BatchPhenotypingError("prediction object digest collision")
        else:
            temporary = locator.with_name(f".{locator.name}.{os.getpid()}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, locator)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return StoredSupervisedNativePrediction(prediction, locator, sha256, len(payload))

    def reopen(self, sha256: str, byte_count: int) -> StoredSupervisedNativePrediction:
        sha256 = _sha(sha256, "prediction object sha256")
        if type(byte_count) is not int or byte_count <= 0:
            raise BatchPhenotypingError("prediction byte_count must be positive")
        locator = self.root / "objects" / f"{sha256}.json"
        try:
            payload = locator.read_bytes()
        except OSError as exc:
            raise BatchPhenotypingError("prediction object is unavailable") from exc
        if len(payload) != byte_count:
            raise BatchPhenotypingError("prediction object bytes differ")
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise BatchPhenotypingError("prediction object digest differs")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchPhenotypingError("prediction object JSON is invalid") from exc
        prediction = SupervisedNativePrediction.from_mapping(decoded)
        if (
            prediction.prediction_sha256 != sha256
            or canonical_bytes(prediction.to_mapping()) != payload
        ):
            raise BatchPhenotypingError("prediction object is not canonical")
        return StoredSupervisedNativePrediction(prediction, locator, sha256, byte_count)


@dataclass(frozen=True, slots=True)
class AcceptedSupervisedPredictionReview:
    prediction_sha256: str
    confidence_threshold: float
    reviewer_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "prediction_sha256",
            _sha(self.prediction_sha256, "prediction_sha256"),
        )
        value = self.confidence_threshold
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise BatchPhenotypingError("confidence_threshold must be finite and in [0, 1]")
        object.__setattr__(self, "confidence_threshold", float(value))
        object.__setattr__(self, "reviewer_id", _text(self.reviewer_id, "reviewer_id"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "prediction_sha256": self.prediction_sha256,
            "confidence_threshold": self.confidence_threshold,
            "reviewer_id": self.reviewer_id,
        }


def supervised_phenotype_result_identity_contract() -> ArtifactIdentityContract:
    fields = {
        "component_descriptor.path",
        "component_descriptor.sha256",
        "sample_id",
        "project_entry_id",
        "slide_id",
        "section_id",
        "tissue_generation_id",
        "classifier_record_sha256",
        "application_request_sha256",
        "prediction_sha256",
        "prediction_rows_sha256",
        "prediction_byte_count",
        "confidence_threshold",
        "reviewer_id",
        "phenotype_generation_id",
        "phenotype_view_artifact_sha256",
        "phenotype_view_lineage_sha256",
        "phenotype_view_inventory_sha256",
        "cell_count",
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
        "source_ref_sha256s",
        "source_ref_set_sha256",
        "saved_state_before_sha256",
        "saved_state_after_sha256",
        "saved_state_disposition",
        "execution_exchange_sha256",
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "request_codec_id",
        "request_codec_version",
    }
    return ArtifactIdentityContract(
        contract_id="mif-batch-supervised-native-prediction-identity/v1",
        artifact_kind="supervised_phenotype_result",
        schema_name="mif-batch-supervised-native-prediction",
        schema_version=1,
        required_fields=tuple(sorted(fields)),
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


__all__ = [
    "AcceptedSupervisedPredictionReview",
    "PREDICTION_SCHEMA",
    "StoredSupervisedNativePrediction",
    "SupervisedNativePrediction",
    "SupervisedNativePredictionStore",
    "supervised_phenotype_result_identity_contract",
]
