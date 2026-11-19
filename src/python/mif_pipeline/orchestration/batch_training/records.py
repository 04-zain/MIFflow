"""Accepted classifier records and immutable record sets.

The `ClassifierRecord` record carries `schema_name`, `schema_version`, `classifier_id`,
`classifier_version`, `classifier_kind` and its remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import ClassVar
from uuid import UUID

from mif_pipeline.compartment_analysis.classifiers import (
    ClassifierCompatibilityError,
    ClassifierDefinition,
    TrainingEvidenceSnapshot,
)
from mif_pipeline.compartment_analysis.classifier_qc import (
    classifier_metrics_from_confusion,
)
from mif_pipeline.cell_analysis.supervised import (
    SupervisedFeatureDefinition,
    SupervisedPhenotypingError,
)
from mif_pipeline.pipeline_core import SecondaryStructureSpec
from mif_pipeline.pipeline_core.config import IGNORE_CLASS_ID
from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectDesignation,
    canonical_project_uri,
)

from ..batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)

from ._shared import (
    BatchTrainingError,
    ClassifierArtifactKind,
    ClassifierKind,
    EvaluationIntent,
    _SPLITS,
    _SPLIT_RANK,
    _array,
    _classifier_training_producer_kind,
    _closed_mapping,
    _digest,
    _enum,
    _export_json,
    _integer,
    _json_object,
    _normalized_native_locator,
    _read_exact_artifact_bytes,
    _sha256,
    _string_tuple,
    _text,
    _version_inventory,
)
from .evidence import (
    MultiImageSupervisedTrainingAggregate,
    MultiImageTrainingEvidence,
    _validate_evidence_plan,
    _validate_supervised_plan_coverage,
)
from .planning import TrainingPlan, TrainingPlanSet

# ==============================================================================
#                                    Records
# ==============================================================================


def _compact_settings_revision(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    mapping = _closed_mapping(
        value,
        fields=frozenset({"predecessor_review_sha256", "adjusted_after_validation"}),
        field="classifier record settings revision",
    )
    if type(mapping["adjusted_after_validation"]) is not bool:
        raise BatchTrainingError("record adjusted_after_validation must be boolean")
    return MappingProxyType(
        {
            "predecessor_review_sha256": _sha256(
                mapping["predecessor_review_sha256"], "predecessor_review_sha256"
            ),
            "adjusted_after_validation": mapping["adjusted_after_validation"],
        }
    )


def _plan_set_settings_revision(plan_set: TrainingPlanSet) -> Mapping[str, object] | None:
    revision = plan_set.settings_revision
    if revision is None:
        return None
    return _compact_settings_revision(
        {
            "predecessor_review_sha256": revision.predecessor_review_sha256,
            "adjusted_after_validation": revision.adjusted_after_validation,
        }
    )


def _finite_metric_mapping(value: object, field: str) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise BatchTrainingError(f"{field} must be a nonempty metric mapping")
    metrics: dict[str, float] = {}
    for raw_name, raw_value in value.items():
        name = _text(raw_name, f"{field} metric name")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise BatchTrainingError(f"{field} must contain finite numeric values")
        number = float(raw_value)
        if not math.isfinite(number):
            raise BatchTrainingError(f"{field} must contain finite numeric values")
        metrics[name] = number
    return MappingProxyType(dict(sorted(metrics.items())))


@dataclass(frozen=True, slots=True)
class ScopedMetricSet:
    scope_id: str
    metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _text(self.scope_id, "scope_id"))
        object.__setattr__(self, "metrics", _finite_metric_mapping(self.metrics, "metrics"))

    @classmethod
    def from_mapping(cls, value: object) -> "ScopedMetricSet":
        mapping = _closed_mapping(
            value,
            fields=frozenset({"scope_id", "metrics"}),
            field="scoped_metric_set",
        )
        return cls(
            scope_id=mapping["scope_id"],  # type: ignore[arg-type]
            metrics=mapping["metrics"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {"scope_id": self.scope_id, "metrics": dict(self.metrics)}


def _metric_scope_tuple(value: object, field: str) -> tuple[ScopedMetricSet, ...]:
    result = tuple(value) if isinstance(value, (list, tuple)) else ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, ScopedMetricSet) for item in result
    ):
        raise BatchTrainingError(f"{field} must contain ScopedMetricSet values")
    if result != tuple(sorted(result, key=lambda item: item.scope_id)):
        raise BatchTrainingError(f"{field} must use canonical scope order")
    if len({item.scope_id for item in result}) != len(result):
        raise BatchTrainingError(f"{field} must contain unique scope IDs")
    return result


def _confusion_matrix(value: object, width: int, field: str) -> tuple[tuple[int, ...], ...]:
    if not isinstance(value, (list, tuple)) or len(value) != width:
        raise BatchTrainingError(f"{field} must be a square class-width matrix")
    if any(not isinstance(row, (list, tuple)) or len(row) != width for row in value):
        raise BatchTrainingError(f"{field} must be a square class-width matrix")
    matrix = tuple(tuple(row) for row in value)
    if any(type(count) is not int or count < 0 for row in matrix for count in row):
        raise BatchTrainingError(f"{field} must contain nonnegative integer counts")
    if sum(map(sum, matrix)) == 0:
        raise BatchTrainingError(f"{field} must contain observations")
    return matrix


def _confusion_scopes(
    value: object, width: int, field: str
) -> Mapping[str, tuple[tuple[int, ...], ...]]:
    if not isinstance(value, Mapping) or not value:
        raise BatchTrainingError(f"{field} must be a nonempty scope mapping")
    scopes = {
        _text(key, f"{field} scope ID"): _confusion_matrix(matrix, width, field)
        for key, matrix in value.items()
    }
    if tuple(scopes) != tuple(sorted(scopes)):
        raise BatchTrainingError(f"{field} must use canonical scope order")
    return MappingProxyType(scopes)


@dataclass(frozen=True, slots=True)
class _PixelAnnotationObservation:
    """One held-out annotation's majority prediction and exact level-0 pixel bounds.

    The ROI digest identifies the geometry evaluated by the original held-out
    model. Bounds support navigation; they do not replace that geometry.
    """

    project_entry_id: str
    subject_id: str
    annotation_uuid: str
    truth_class_id: str
    predicted_class_id: str
    normalized_roi_sha256: str
    roi_bounds: Mapping[str, float | int]

    def __post_init__(self) -> None:
        for name in ("project_entry_id", "subject_id", "annotation_uuid", "truth_class_id", "predicted_class_id"):
            _text(getattr(self, name), name)
        try:
            if str(UUID(self.annotation_uuid)) != self.annotation_uuid:
                raise ValueError
        except ValueError as exc:
            raise BatchTrainingError("evaluation annotation UUID is invalid") from exc
        _sha256(self.normalized_roi_sha256, "evaluation ROI digest")
        bounds = _closed_mapping(self.roi_bounds,
            fields=frozenset({"x", "y", "width", "height", "z", "t"}), field="evaluation ROI bounds")
        for name in ("x", "y", "width", "height"):
            value = bounds[name]
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise BatchTrainingError("evaluation ROI bounds must be finite")
            if name in {"width", "height"} and value <= 0:
                raise BatchTrainingError("evaluation ROI width and height must be positive")
        for name in ("z", "t"):
            if type(bounds[name]) is not int or not 0 <= bounds[name] <= 2147483647:
                raise BatchTrainingError("evaluation ROI plane must use nonnegative integers")
        object.__setattr__(self, "roi_bounds", MappingProxyType(dict(bounds)))

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.project_entry_id, self.subject_id, self.annotation_uuid

    @classmethod
    def from_mapping(cls, value: object) -> "_PixelAnnotationObservation":
        row = _closed_mapping(value, fields=frozenset(cls.__dataclass_fields__),
                              field="evaluation annotation observation")
        return cls(**row)

    def to_mapping(self) -> dict[str, object]:
        return {name: dict(self.roi_bounds) if name == "roi_bounds" else getattr(self, name)
                for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class _PixelConfusionEvidence:
    class_ids: tuple[str, ...]
    observation_unit: str
    zero_denominator_policy: str
    pooled: tuple[tuple[int, ...], ...]
    per_project_entry: Mapping[str, tuple[tuple[int, ...], ...]]
    per_subject: Mapping[str, tuple[tuple[int, ...], ...]]
    observations: tuple[_PixelAnnotationObservation, ...] = ()

    def __post_init__(self) -> None:
        classes = _string_tuple(self.class_ids, "class_ids", nonempty=True, canonical=False)
        if len(set(classes)) != len(classes):
            raise BatchTrainingError("pixel confusion class_ids must be unique")
        if self.observation_unit != "annotation_majority":
            raise BatchTrainingError("pixel confusion observation_unit must be annotation_majority")
        if self.zero_denominator_policy != "zero":
            raise BatchTrainingError("pixel confusion zero_denominator_policy must be zero")
        pooled = _confusion_matrix(self.pooled, len(classes), "pooled confusion")
        entries = _confusion_scopes(
            self.per_project_entry, len(classes), "per_project_entry confusion"
        )
        subjects = _confusion_scopes(self.per_subject, len(classes), "per_subject confusion")
        for scopes in (entries, subjects):
            if any(
                pooled[row][column] != sum(matrix[row][column] for matrix in scopes.values())
                for row in range(len(classes))
                for column in range(len(classes))
            ):
                raise BatchTrainingError("pixel confusion scoped counts must sum to pooled counts")
        object.__setattr__(self, "class_ids", classes)
        object.__setattr__(self, "pooled", pooled)
        object.__setattr__(self, "per_project_entry", entries)
        object.__setattr__(self, "per_subject", subjects)
        observations = self.observations
        if type(observations) is not tuple or any(type(item) is not _PixelAnnotationObservation for item in observations):
            raise BatchTrainingError("pixel confusion observations must contain typed annotation rows")
        if observations:
            identities = tuple(item.identity for item in observations)
            if identities != tuple(sorted(set(identities))):
                raise BatchTrainingError("pixel confusion observations must use unique canonical identities")
            positions = {value: index for index, value in enumerate(classes)}
            observed = [[0] * len(classes) for _ in classes]
            by_entry, by_subject = {}, {}
            for item in observations:
                if item.truth_class_id not in positions or item.predicted_class_id not in positions:
                    raise BatchTrainingError("pixel confusion observation uses an undeclared class")
                row, column = positions[item.truth_class_id], positions[item.predicted_class_id]
                observed[row][column] += 1
                for scopes, scope in ((by_entry, item.project_entry_id), (by_subject, item.subject_id)):
                    scopes.setdefault(scope, [[0] * len(classes) for _ in classes])[row][column] += 1
            matrices = lambda rows: {scope: tuple(tuple(row) for row in matrix) for scope, matrix in rows.items()}
            if (tuple(tuple(row) for row in observed) != pooled
                    or matrices(by_entry) != entries or matrices(by_subject) != subjects):
                raise BatchTrainingError("pixel confusion observations differ from pooled or scoped counts")

    @classmethod
    def from_mapping(cls, value: object) -> "_PixelConfusionEvidence":
        mapping = _closed_mapping(
            value,
            fields=frozenset(
                {
                    "class_ids",
                    "observation_unit",
                    "zero_denominator_policy",
                    "pooled",
                    "per_project_entry",
                    "per_subject",
                }
            ) | ({"observations"} if isinstance(value, Mapping) and "observations" in value else set()),
            field="pixel_confusion",
        )
        observations = _array(mapping.get("observations", []), "evaluation observations")
        if "observations" in mapping and not observations:
            raise BatchTrainingError("recorded evaluation observations must not be empty")
        return cls(
            class_ids=tuple(_array(mapping["class_ids"], "class_ids")),
            observation_unit=mapping["observation_unit"],  # type: ignore[arg-type]
            zero_denominator_policy=mapping["zero_denominator_policy"],  # type: ignore[arg-type]
            pooled=mapping["pooled"],  # type: ignore[arg-type]
            per_project_entry=mapping["per_project_entry"],  # type: ignore[arg-type]
            per_subject=mapping["per_subject"],  # type: ignore[arg-type]
            observations=tuple(_PixelAnnotationObservation.from_mapping(item)
                               for item in observations),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "class_ids": list(self.class_ids),
            "observation_unit": self.observation_unit,
            "zero_denominator_policy": self.zero_denominator_policy,
            "pooled": [list(row) for row in self.pooled],
            "per_project_entry": {
                scope: [list(row) for row in matrix]
                for scope, matrix in self.per_project_entry.items()
            },
            "per_subject": {
                scope: [list(row) for row in matrix] for scope, matrix in self.per_subject.items()
            },
            **({"observations": [item.to_mapping() for item in self.observations]} if self.observations else {}),
        }


def _check_confusion_metrics(
    metrics: Mapping[str, float], matrix: tuple[tuple[int, ...], ...]
) -> None:
    expected = classifier_metrics_from_confusion(matrix)
    if set(metrics) != set(expected) or any(
        not math.isclose(metrics[name], value, rel_tol=1e-12, abs_tol=1e-12)
        for name, value in expected.items()
    ):
        raise BatchTrainingError("pixel confusion counts differ from scalar metrics")


@dataclass(frozen=True, slots=True)
class ClassifierEvaluationMetrics:
    split: str
    pooled: Mapping[str, float]
    per_project_entry: tuple[ScopedMetricSet, ...]
    per_subject: tuple[ScopedMetricSet, ...]
    pixel_confusion: _PixelConfusionEvidence | None = None
    pixel_scores: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.split not in _SPLITS:
            raise BatchTrainingError("metric split must be training, validation, or test")
        object.__setattr__(self, "pooled", _finite_metric_mapping(self.pooled, "pooled"))
        object.__setattr__(
            self,
            "per_project_entry",
            _metric_scope_tuple(self.per_project_entry, "per_project_entry"),
        )
        object.__setattr__(
            self, "per_subject", _metric_scope_tuple(self.per_subject, "per_subject")
        )
        metric_names = set(self.pooled)
        if any(
            set(item.metrics) != metric_names
            for item in (*self.per_project_entry, *self.per_subject)
        ):
            raise BatchTrainingError("pooled and scoped metric names must match exactly")
        confusion = self.pixel_confusion
        if confusion is not None:
            if not isinstance(confusion, _PixelConfusionEvidence):
                raise BatchTrainingError("pixel_confusion must be typed evidence or null")
            _check_confusion_metrics(self.pooled, confusion.pooled)
            for scalar, matrices in (
                (self.per_project_entry, confusion.per_project_entry),
                (self.per_subject, confusion.per_subject),
            ):
                if tuple(item.scope_id for item in scalar) != tuple(matrices):
                    raise BatchTrainingError(
                        "pixel confusion scope coverage differs from scalar metrics"
                    )
                for item in scalar:
                    _check_confusion_metrics(item.metrics, matrices[item.scope_id])
        if self.pixel_scores is not None:
            from mif_pipeline.compartment_analysis.precision_recall import validate_pixel_score_evidence
            scores = validate_pixel_score_evidence(self.pixel_scores)
            if (confusion is None or scores["split"] != self.split
                    or scores["class_ids"] != list(confusion.class_ids)
                    or {r["project_entry_id"] for r in scores["entries"]} != set(confusion.per_project_entry)
                    or {r["subject_id"] for r in scores["entries"]} != set(confusion.per_subject)):
                raise BatchTrainingError("Held-out pixel scores differ from the evaluated split, classes or subjects")
            object.__setattr__(self, "pixel_scores", _json_object(scores, "pixel_scores"))

    @classmethod
    def create(
        cls,
        *,
        split: str,
        pooled: Mapping[str, float],
        per_project_entry: tuple[ScopedMetricSet, ...],
        per_subject: tuple[ScopedMetricSet, ...],
        pixel_confusion: _PixelConfusionEvidence | None = None,
        pixel_scores: Mapping[str, object] | None = None,
    ) -> "ClassifierEvaluationMetrics":
        return cls(
            split=split,
            pooled=pooled,
            per_project_entry=tuple(sorted(per_project_entry, key=lambda item: item.scope_id)),
            per_subject=tuple(sorted(per_subject, key=lambda item: item.scope_id)),
            pixel_confusion=pixel_confusion,
            pixel_scores=pixel_scores,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ClassifierEvaluationMetrics":
        mapping = _closed_mapping(
            value,
            fields=frozenset(
                {"split", "pooled", "per_project_entry", "per_subject", "pixel_confusion"}
                | ({"pixel_scores"} if isinstance(value, Mapping) and "pixel_scores" in value else set())
            ),
            field="classifier_evaluation_metrics",
        )
        return cls(
            split=mapping["split"],  # type: ignore[arg-type]
            pooled=mapping["pooled"],  # type: ignore[arg-type]
            per_project_entry=tuple(
                ScopedMetricSet.from_mapping(item)
                for item in _array(mapping["per_project_entry"], "per_project_entry")
            ),
            per_subject=tuple(
                ScopedMetricSet.from_mapping(item)
                for item in _array(mapping["per_subject"], "per_subject")
            ),
            pixel_confusion=(
                None
                if mapping["pixel_confusion"] is None
                else _PixelConfusionEvidence.from_mapping(mapping["pixel_confusion"])
            ),
            pixel_scores=mapping.get("pixel_scores"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "split": self.split,
            "pooled": dict(self.pooled),
            "per_project_entry": [item.to_mapping() for item in self.per_project_entry],
            "per_subject": [item.to_mapping() for item in self.per_subject],
            "pixel_confusion": (
                None if self.pixel_confusion is None else self.pixel_confusion.to_mapping()
            ),
            **({"pixel_scores": _export_json(self.pixel_scores)} if self.pixel_scores is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class PixelClassifierOwnerPayload:
    classifier_definition: ClassifierDefinition
    classifier_definition_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.classifier_definition, ClassifierDefinition):
            raise BatchTrainingError("classifier_definition must be a ClassifierDefinition")
        # ClassifierDefinition construction owns scientific validation and freezes
        # its nested settings. This payload only binds that value to its digest.
        definition = self.classifier_definition
        digest = _sha256(self.classifier_definition_sha256, "classifier_definition_sha256")
        if digest != _digest(definition.to_mapping()):
            raise BatchTrainingError("classifier_definition_sha256 is not canonical")
        object.__setattr__(self, "classifier_definition_sha256", digest)

    @classmethod
    def create(cls, definition: ClassifierDefinition) -> "PixelClassifierOwnerPayload":
        if not isinstance(definition, ClassifierDefinition):
            raise BatchTrainingError("classifier_definition must be a ClassifierDefinition")
        return cls(definition, _digest(definition.to_mapping()))

    @classmethod
    def from_mapping(cls, value: object) -> "PixelClassifierOwnerPayload":
        mapping = _closed_mapping(
            value,
            fields=frozenset({"classifier_definition", "classifier_definition_sha256"}),
            field="pixel_classifier_owner_payload",
        )
        try:
            definition = ClassifierDefinition.from_mapping(mapping["classifier_definition"])
        except ClassifierCompatibilityError as exc:
            raise BatchTrainingError("classifier_definition is invalid") from exc
        return cls(
            definition,
            mapping["classifier_definition_sha256"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "classifier_definition": self.classifier_definition.to_mapping(),
            "classifier_definition_sha256": self.classifier_definition_sha256,
        }


@dataclass(frozen=True, slots=True)
class EntryScopedCellIdentity:
    project_entry_id: str
    cell_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_entry_id", _text(self.project_entry_id, "project_entry_id")
        )
        object.__setattr__(self, "cell_id", _text(self.cell_id, "cell_id"))

    @classmethod
    def from_mapping(cls, value: object) -> "EntryScopedCellIdentity":
        mapping = _closed_mapping(
            value,
            fields=frozenset({"project_entry_id", "cell_id"}),
            field="entry_scoped_cell_identity",
        )
        return cls(mapping["project_entry_id"], mapping["cell_id"])  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, str]:
        return {"project_entry_id": self.project_entry_id, "cell_id": self.cell_id}


@dataclass(frozen=True, slots=True)
class EntryScopedRegionIdentity:
    project_entry_id: str
    spatial_region_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_entry_id", _text(self.project_entry_id, "project_entry_id")
        )
        object.__setattr__(
            self,
            "spatial_region_id",
            _text(self.spatial_region_id, "spatial_region_id"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "EntryScopedRegionIdentity":
        mapping = _closed_mapping(
            value,
            fields=frozenset({"project_entry_id", "spatial_region_id"}),
            field="entry_scoped_region_identity",
        )
        return cls(mapping["project_entry_id"], mapping["spatial_region_id"])  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, str]:
        return {
            "project_entry_id": self.project_entry_id,
            "spatial_region_id": self.spatial_region_id,
        }


def _scoped_identity_tuple(
    value: object,
    field: str,
    expected_type: type[EntryScopedCellIdentity] | type[EntryScopedRegionIdentity],
) -> tuple[EntryScopedCellIdentity, ...] | tuple[EntryScopedRegionIdentity, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, expected_type) for item in value
    ):
        raise BatchTrainingError(f"{field} must contain typed entry-scoped identities")
    result = tuple(value)
    key = (
        (lambda item: (item.project_entry_id, item.cell_id))
        if expected_type is EntryScopedCellIdentity
        else (lambda item: (item.project_entry_id, item.spatial_region_id))
    )
    if result != tuple(sorted(result, key=key)) or len({key(item) for item in result}) != len(
        result
    ):
        raise BatchTrainingError(f"{field} must be canonical and unique")
    return result


@dataclass(frozen=True, slots=True)
class SupervisedClassifierOwnerPayload:
    classifier_id: str
    classifier_sha256: str
    qupath_version: str
    class_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedFeatureDefinition, ...]
    validation_domain_id: str
    training_cells: tuple[EntryScopedCellIdentity, ...]
    validation_cells: tuple[EntryScopedCellIdentity, ...]
    test_cells: tuple[EntryScopedCellIdentity, ...]
    training_regions: tuple[EntryScopedRegionIdentity, ...]
    validation_regions: tuple[EntryScopedRegionIdentity, ...]
    test_regions: tuple[EntryScopedRegionIdentity, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        """Validate the SupervisedClassifierOwnerPayload fields when the record is constructed.

        The checks cover ``class_ids``, ``feature_schema``, ``payload_sha256``,
        ``classifier_id``, ``classifier_sha256``, ``qupath_version``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(self, "classifier_id", _text(self.classifier_id, "classifier_id"))
        object.__setattr__(
            self, "classifier_sha256", _sha256(self.classifier_sha256, "classifier_sha256")
        )
        object.__setattr__(self, "qupath_version", _text(self.qupath_version, "qupath_version"))
        classes = _string_tuple(self.class_ids, "class_ids", nonempty=True, canonical=False)
        features = tuple(self.feature_schema)
        if not features or not all(
            isinstance(item, SupervisedFeatureDefinition) for item in features
        ):
            raise BatchTrainingError("feature_schema must contain owner feature definitions")
        if len({item.measurement_name for item in features}) != len(features):
            raise BatchTrainingError("feature_schema measurement names must be unique")
        object.__setattr__(self, "class_ids", classes)
        object.__setattr__(self, "feature_schema", features)
        object.__setattr__(
            self,
            "validation_domain_id",
            _text(self.validation_domain_id, "validation_domain_id"),
        )
        cell_fields = ("training_cells", "validation_cells", "test_cells")
        region_fields = ("training_regions", "validation_regions", "test_regions")
        for field in cell_fields:
            object.__setattr__(
                self,
                field,
                _scoped_identity_tuple(getattr(self, field), field, EntryScopedCellIdentity),
            )
        for field in region_fields:
            object.__setattr__(
                self,
                field,
                _scoped_identity_tuple(getattr(self, field), field, EntryScopedRegionIdentity),
            )
        if not self.training_cells or not self.training_regions:
            raise BatchTrainingError("training cell and region inventories must not be empty")
        cell_sets = [
            {(item.project_entry_id, item.cell_id) for item in getattr(self, field)}
            for field in cell_fields
        ]
        region_sets = [
            {(item.project_entry_id, item.spatial_region_id) for item in getattr(self, field)}
            for field in region_fields
        ]
        if any(cell_sets[left] & cell_sets[right] for left, right in ((0, 1), (0, 2), (1, 2))):
            raise BatchTrainingError("entry-scoped supervised cells must be split-disjoint")
        if any(region_sets[left] & region_sets[right] for left, right in ((0, 1), (0, 2), (1, 2))):
            raise BatchTrainingError("entry-scoped supervised regions must be split-disjoint")
        for cell_field, region_field in zip(cell_fields, region_fields, strict=True):
            cell_entries = {item.project_entry_id for item in getattr(self, cell_field)}
            region_entries = {item.project_entry_id for item in getattr(self, region_field)}
            if cell_entries != region_entries:
                raise BatchTrainingError(f"{region_field} must cover exact split entries")
        payload_sha = _sha256(self.payload_sha256, "payload_sha256")
        if payload_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("payload_sha256 is not canonical")
        object.__setattr__(self, "payload_sha256", payload_sha)

    @classmethod
    def create(cls, **values: object) -> "SupervisedClassifierOwnerPayload":
        cell_names = ("training_cells", "validation_cells", "test_cells")
        region_names = ("training_regions", "validation_regions", "test_regions")
        normalized = dict(values)
        for field in cell_names:
            normalized[field] = tuple(
                sorted(
                    normalized[field],  # type: ignore[arg-type]
                    key=lambda item: (item.project_entry_id, item.cell_id),
                )
            )
        for field in region_names:
            normalized[field] = tuple(
                sorted(
                    normalized[field],  # type: ignore[arg-type]
                    key=lambda item: (item.project_entry_id, item.spatial_region_id),
                )
            )
        prototype = object.__new__(cls)
        for name, item in normalized.items():
            object.__setattr__(prototype, name, item)
        return cls(**normalized, payload_sha256=_digest(prototype._unsigned_mapping()))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedClassifierOwnerPayload":
        fields = frozenset(cls.__dataclass_fields__)
        mapping = _closed_mapping(value, fields=fields, field="supervised_classifier_owner_payload")
        try:
            feature_schema = tuple(
                SupervisedFeatureDefinition.from_mapping(item)
                for item in _array(mapping["feature_schema"], "feature_schema")
            )
        except SupervisedPhenotypingError as exc:
            raise BatchTrainingError("supervised owner contains invalid owner material") from exc
        values: dict[str, object] = {
            "classifier_id": mapping["classifier_id"],
            "classifier_sha256": mapping["classifier_sha256"],
            "qupath_version": mapping["qupath_version"],
            "class_ids": tuple(_array(mapping["class_ids"], "class_ids")),
            "feature_schema": feature_schema,
            "validation_domain_id": mapping["validation_domain_id"],
            "payload_sha256": mapping["payload_sha256"],
        }
        for field in ("training_cells", "validation_cells", "test_cells"):
            values[field] = tuple(
                EntryScopedCellIdentity.from_mapping(item) for item in _array(mapping[field], field)
            )
        for field in ("training_regions", "validation_regions", "test_regions"):
            values[field] = tuple(
                EntryScopedRegionIdentity.from_mapping(item)
                for item in _array(mapping[field], field)
            )
        return cls(**values)  # type: ignore[arg-type]

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "classifier_id": self.classifier_id,
            "classifier_sha256": self.classifier_sha256,
            "qupath_version": self.qupath_version,
            "class_ids": list(self.class_ids),
            "feature_schema": [item.to_mapping() for item in self.feature_schema],
            "validation_domain_id": self.validation_domain_id,
            "training_cells": [item.to_mapping() for item in self.training_cells],
            "validation_cells": [item.to_mapping() for item in self.validation_cells],
            "test_cells": [item.to_mapping() for item in self.test_cells],
            "training_regions": [item.to_mapping() for item in self.training_regions],
            "validation_regions": [item.to_mapping() for item in self.validation_regions],
            "test_regions": [item.to_mapping() for item in self.test_regions],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "payload_sha256": self.payload_sha256}


_PYTHON_MODEL_OWNER_FIELDS = frozenset(
    {
        "classifier_id",
        "classifier_version",
        "training_plan_id",
        "accepted_training_evidence_sha256",
        "normalized_manifest_sha256",
        "model_configuration_sha256",
    }
)


def _validate_python_model_artifact_binding(
    artifact: BatchArtifactRef,
    *,
    classifier_id: str,
    classifier_version: str,
    training_plan_id: str,
    accepted_training_evidence_sha256: str,
    validation_domain_id: str,
    normalized_manifest_sha256: str,
    model_configuration_sha256: str,
    expected_sample_ids: tuple[str, ...],
) -> BatchArtifactRef:
    """Validate python model artifact binding for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if not isinstance(artifact, BatchArtifactRef):
        raise BatchTrainingError("Python model artifact must be a typed reference")
    try:
        validate_artifact_ref(artifact)
    except BatchArtifactError as exc:
        raise BatchTrainingError("Python model artifact reference is invalid") from exc
    if artifact.origin is not ArtifactOrigin.INTERNAL_NODE:
        raise BatchTrainingError("Python model artifact must be repository-owned")
    contract = artifact.identity_contract
    if (
        frozenset(contract.required_fields) != _PYTHON_MODEL_OWNER_FIELDS
        or contract.optional_fields
    ):
        raise BatchTrainingError("Python model owner identity contract is not exact")
    identity = dict(artifact.owning_identity)
    if frozenset(identity) != _PYTHON_MODEL_OWNER_FIELDS:
        raise BatchTrainingError("Python model owner identity is not complete")
    expected_identity = {
        "classifier_id": _text(classifier_id, "classifier_id"),
        "classifier_version": _text(classifier_version, "classifier_version"),
        "training_plan_id": _sha256(training_plan_id, "training_plan_id"),
        "accepted_training_evidence_sha256": _sha256(
            accepted_training_evidence_sha256,
            "accepted_training_evidence_sha256",
        ),
    }
    if any(identity[name] != value for name, value in expected_identity.items()):
        raise BatchTrainingError(
            "Python model owner identity differs from exact record plan context"
        )
    for field, expected in (
        ("normalized_manifest_sha256", normalized_manifest_sha256),
        ("model_configuration_sha256", model_configuration_sha256),
    ):
        observed = _sha256(identity[field], f"Python model owner {field}")
        if observed != _sha256(expected, field):
            raise BatchTrainingError(
                "Python model owner identity differs from exact record plan context"
            )
    scope = artifact.scope
    if (
        scope.validation_domain_id != _text(validation_domain_id, "validation_domain_id")
        or any(
            item is not None for item in (scope.sample_id, scope.slide_id, scope.project_entry_id)
        )
        or scope.analysis_domain_bindings
    ):
        raise BatchTrainingError("Python model owner scope differs from exact record plan context")
    observed_sample_ids = tuple(item.sample_id for item in artifact.configuration_bindings)
    if observed_sample_ids != expected_sample_ids:
        raise BatchTrainingError("Python model configuration bindings differ from plan samples")
    if artifact.locator is None or artifact.sha256 is None or artifact.byte_count is None:
        raise BatchTrainingError("Python model artifact must be materialized")
    return artifact


def _validate_record_metrics(
    plan: TrainingPlan, metrics: tuple[ClassifierEvaluationMetrics, ...], *, frozen: bool
) -> None:
    if plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
        if metrics:
            raise BatchTrainingError("training-only records must not contain metrics")
        return
    by_split = {item.split: item for item in metrics}
    expected_splits = {
        split for split in ("validation", "test") if plan.split_project_entry_ids[split]
    }
    if not frozen:
        expected_splits.discard("test")
    if set(by_split) != expected_splits:
        raise BatchTrainingError("classifier acceptance requires exact held-out metric splits")
    for metric in metrics:
        if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            if metric.pixel_confusion is not None:
                raise BatchTrainingError("supervised metrics must have null pixel confusion")
        elif (
            metric.pixel_confusion is None
            or metric.pixel_confusion.class_ids != plan.request.required_classes
        ):
            raise BatchTrainingError(
                "pixel metrics require confusion counts in planned class order"
            )
        expected_entries = plan.split_project_entry_ids[metric.split]
        actual_entries = tuple(item.scope_id for item in metric.per_project_entry)
        if actual_entries != expected_entries:
            raise BatchTrainingError(
                f"{metric.split} metrics require exact per-project-entry coverage"
            )
        expected_subjects = plan.split_subject_ids[metric.split]
        actual_subjects = tuple(item.scope_id for item in metric.per_subject)
        if actual_subjects != expected_subjects:
            raise BatchTrainingError(f"{metric.split} metrics have incomplete per-subject coverage")
    if frozen and plan.request.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE:
        test = by_split.get("test")
        if test is None:
            raise BatchTrainingError("independent performance requires complete test metrics")
        if tuple(item.scope_id for item in test.per_subject) != plan.split_subject_ids["test"]:
            raise BatchTrainingError(
                "independent performance requires complete per-subject test metrics"
            )


def _qupath_executable_version_label(semantic_version: object) -> str:
    """Project a native version into the executable probe's exact label format.

    Native material and owner payloads retain GeneralTools.getVersion() values
    such as 0.7.0. Frame-derived runtime inventories retain the canonical
    QuPathRunner executable label, QuPath v0.7.0. This one-way projection joins
    those contracts without accepting alternative spellings or rewriting them.
    """
    return f"QuPath v{_text(semantic_version, 'qupath_version')}"


def _validate_record_owner_plan(
    plan: TrainingPlan,
    owner: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload,
    *,
    classifier_version: str,
    runtime_versions: tuple[tuple[str, str], ...],
) -> None:
    planned_entries = {split: set(plan.split_project_entry_ids[split]) for split in _SPLITS}
    if isinstance(owner, PixelClassifierOwnerPayload):
        definition = owner.classifier_definition
        if plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
            if definition.validation_status != "not_evaluated":
                raise BatchTrainingError("training-only pixel owner must be not_evaluated")
        elif definition.validation_status == "not_evaluated":
            raise BatchTrainingError("evaluated pixel owner cannot be not_evaluated")
        planned_channels = plan.request.channel_requirements
        if (
            not isinstance(planned_channels, Mapping)
            or set(planned_channels) != {"channel_roles"}
            or _export_json(planned_channels["channel_roles"])
            != _export_json(definition.channel_roles)
        ):
            raise BatchTrainingError("pixel owner channel roles differ from plan")
        runtime_map = dict(_version_inventory(runtime_versions, "runtime_versions", nonempty=True))
        if runtime_map.get("qupath") != _qupath_executable_version_label(
            definition.runtime_signature.get("qupath_version")
        ) or runtime_map.get("script_bundle_sha256") != definition.runtime_signature.get(
            "script_bundle_sha256"
        ):
            raise BatchTrainingError("pixel owner runtime signature differs from record runtime")
        owner_entries = {item.project_entry_id for item in definition.evidence_snapshots}
        if owner_entries != set().union(*planned_entries.values()):
            raise BatchTrainingError("pixel owner project-entry inventory differs from plan")
        owner_roles = {
            (item.project_entry_id, item.split) for item in definition.evidence_snapshots
        }
        expected_roles = {
            (entry_id, split) for split, entries in planned_entries.items() for entry_id in entries
        }
        if owner_roles != expected_roles:
            raise BatchTrainingError("pixel owner split inventory differs from plan")
        owner_domains = {item.analysis_domain_id for item in definition.evidence_snapshots}
        plan_domains = {item.analysis_domain_id for item in plan.request.analysis_domain_bindings}
        if owner_domains != plan_domains:
            raise BatchTrainingError("pixel owner analysis-domain inventory differs from plan")
    else:
        split_cells = {
            "training": owner.training_cells,
            "validation": owner.validation_cells,
            "test": owner.test_cells,
        }
        for split, cells in split_cells.items():
            if {item.project_entry_id for item in cells} != planned_entries[split]:
                raise BatchTrainingError(
                    f"supervised owner {split} entry inventory differs from plan"
                )
        if owner.class_ids != plan.request.required_classes:
            raise BatchTrainingError("supervised owner class inventory differs from plan")
        required_features = plan.request.feature_requirements
        if not isinstance(required_features, Mapping) or set(required_features) != {
            "feature_schema",
            "point_selection_radius_level0",
        }:
            raise BatchTrainingError("supervised plan lacks exact evidence feature requirements")
        if _export_json(required_features["feature_schema"]) != [
            item.to_mapping() for item in owner.feature_schema
        ]:
            raise BatchTrainingError("supervised owner feature schema differs from plan")
        radius = required_features["point_selection_radius_level0"]
        if (
            isinstance(radius, bool)
            or not isinstance(radius, (int, float))
            or not math.isfinite(float(radius))
            or radius <= 0
        ):
            raise BatchTrainingError(
                "supervised plan point-selection radius must be positive and finite"
            )
        runtime_map = dict(_version_inventory(runtime_versions, "runtime_versions", nonempty=True))
        if runtime_map.get("qupath") != _qupath_executable_version_label(owner.qupath_version):
            raise BatchTrainingError("supervised owner QuPath version differs from runtime")
    if isinstance(owner, PixelClassifierOwnerPayload):
        if owner.classifier_definition.version != _text(classifier_version, "classifier_version"):
            raise BatchTrainingError("pixel owner version differs from plan acceptance")


def _validate_record_training_evidence(
    plan: TrainingPlan,
    owner: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload | None,
    complete: MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate,
) -> None:
    """Join immutable, constructor-validated evidence to this plan and classifier owner."""

    if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        if type(complete) is not MultiImageSupervisedTrainingAggregate or (
            owner is not None and type(owner) is not SupervisedClassifierOwnerPayload
        ):
            raise BatchTrainingError(
                "supervised record acceptance requires the exact complete aggregate"
            )
        if (
            complete.training_plan_id != plan.training_plan_id
            or complete.normalized_manifest_sha256 != plan.normalized_manifest_sha256
        ):
            raise BatchTrainingError("complete supervised evidence differs from its plan")
        _validate_supervised_plan_coverage(complete.entries, plan, include_test=True)
        if owner is not None and (
            owner.class_ids != complete.class_ids
            or owner.feature_schema != complete.feature_schema
            or owner.training_cells != complete.training_cells
            or owner.validation_cells != complete.validation_cells
            or owner.test_cells != complete.test_cells
            or owner.training_regions != complete.training_regions
            or owner.validation_regions != complete.validation_regions
            or owner.test_regions != complete.test_regions
        ):
            raise BatchTrainingError("supervised owner differs from the exact complete aggregate")
        return
    if type(complete) is not MultiImageTrainingEvidence or (
        owner is not None and type(owner) is not PixelClassifierOwnerPayload
    ):
        raise BatchTrainingError(
            "pixel record acceptance requires exact MultiImageTrainingEvidence"
        )
    _validate_evidence_plan(complete, plan)
    if owner is not None and not _same_pixel_evidence_rows(
        tuple(owner.classifier_definition.evidence_snapshots), complete.rows
    ):
        raise BatchTrainingError("pixel owner evidence rows differ from accepted training evidence")


def _same_pixel_evidence_rows(
    owner_rows: tuple[TrainingEvidenceSnapshot, ...],
    accepted_rows: tuple[TrainingEvidenceSnapshot, ...],
) -> bool:
    """Compare identical authenticated rows across their two canonical orders."""

    by_evidence = lambda item: item.evidence_sha256
    return tuple(sorted(owner_rows, key=by_evidence)) == tuple(
        sorted(accepted_rows, key=by_evidence)
    )


@dataclass(frozen=True, slots=True)
class _PreparedClassifierRecordAcceptance:
    record: "ClassifierRecord"
    held_out_evaluations: tuple[object, ...]

    def consume(self) -> "ClassifierRecord":
        if self.held_out_evaluations:
            from .evaluation import _consume_held_out_classifier_evaluations

            authenticated = _consume_held_out_classifier_evaluations(self.held_out_evaluations)
            by_split = {item.split: item for item in self.record.metrics}
            for supplied, (issued, metrics) in zip(
                self.held_out_evaluations, authenticated, strict=True
            ):
                if supplied is not issued or tuple(metrics) != (by_split[issued.evaluation_split],):
                    raise BatchTrainingError(
                        "held-out classifier evaluation authentication changed identity"
                    )
        return self.record


@dataclass(frozen=True, slots=True)
class ClassifierRecord:
    schema_name: ClassVar[str] = "mif-classifier-record"
    schema_version: ClassVar[int] = 6

    classifier_id: str
    classifier_version: str
    classifier_kind: ClassifierKind
    classifier_engine: str
    evaluation_intent: EvaluationIntent
    artifact_kind: ClassifierArtifactKind
    native_artifact_project_relative_path: str | None
    native_artifact_sha256: str | None
    native_artifact_byte_count: int | None
    native_source_project_identity_sha256: str | None
    python_model_artifact: BatchArtifactRef | None
    owner_payload: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload | None
    training_plan_id: str
    accepted_training_evidence_sha256: str
    fit_plan_sha256: str | None
    fit_execution_sha256: str | None
    checkpoint_entry_sha256: str | None
    fit_material_sha256: str | None
    training_material_evidence_sha256: str | None
    channel_requirements: Mapping[str, object]
    channel_requirements_sha256: str
    feature_requirements: Mapping[str, object]
    feature_requirements_sha256: str
    runtime_versions: tuple[tuple[str, str], ...]
    extension_versions: tuple[tuple[str, str], ...]
    model_versions: tuple[tuple[str, str], ...]
    metrics: tuple[ClassifierEvaluationMetrics, ...]
    evaluation_evidence: tuple[Mapping[str, str], ...]
    freeze_review_sha256: str | None
    review_decision: str
    validation_domain_id: str
    settings_revision: Mapping[str, object] | None
    classifier_record_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClassifierRecord fields when the record is constructed.

        The checks cover ``owner_payload``, ``artifact_kind``, ``runtime_versions``,
        ``metrics``, ``evaluation_intent``, ``evaluation_evidence``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(self, "classifier_id", _text(self.classifier_id, "classifier_id"))
        object.__setattr__(
            self, "classifier_version", _text(self.classifier_version, "classifier_version")
        )
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        if not isinstance(self.evaluation_intent, EvaluationIntent):
            raise BatchTrainingError("evaluation_intent must be an EvaluationIntent")
        object.__setattr__(
            self, "classifier_engine", _text(self.classifier_engine, "classifier_engine")
        )
        if not isinstance(self.artifact_kind, ClassifierArtifactKind):
            raise BatchTrainingError("artifact_kind must be a ClassifierArtifactKind")
        if self.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
            if self.python_model_artifact is not None:
                raise BatchTrainingError("native and Python artifact representations are exclusive")
            if None in (
                self.native_artifact_project_relative_path,
                self.native_artifact_sha256,
                self.native_artifact_byte_count,
                self.native_source_project_identity_sha256,
            ):
                raise BatchTrainingError("native artifact representation is incomplete")
            object.__setattr__(
                self,
                "native_artifact_project_relative_path",
                _normalized_native_locator(
                    self.native_artifact_project_relative_path, require_canonical=True
                ),
            )
            object.__setattr__(
                self,
                "native_artifact_sha256",
                _sha256(self.native_artifact_sha256, "native_artifact_sha256"),
            )
            count = _integer(self.native_artifact_byte_count, "native_artifact_byte_count")
            if count == 0:
                raise BatchTrainingError("native_artifact_byte_count must be positive")
            object.__setattr__(self, "native_artifact_byte_count", count)
            object.__setattr__(
                self,
                "native_source_project_identity_sha256",
                _sha256(
                    self.native_source_project_identity_sha256,
                    "native_source_project_identity_sha256",
                ),
            )
        else:
            if any(
                item is not None
                for item in (
                    self.native_artifact_project_relative_path,
                    self.native_artifact_sha256,
                    self.native_artifact_byte_count,
                    self.native_source_project_identity_sha256,
                )
            ):
                raise BatchTrainingError("native and Python artifact representations are exclusive")
            if not isinstance(self.python_model_artifact, BatchArtifactRef):
                raise BatchTrainingError("python-model records require a typed Python artifact")
            try:
                validate_artifact_ref(self.python_model_artifact)
            except BatchArtifactError as exc:
                raise BatchTrainingError("python model artifact is invalid") from exc
            if self.python_model_artifact.origin is not ArtifactOrigin.INTERNAL_NODE:
                raise BatchTrainingError("Python model artifact must be repository-owned")
            if any(
                item is None
                for item in (
                    self.python_model_artifact.locator,
                    self.python_model_artifact.sha256,
                    self.python_model_artifact.byte_count,
                )
            ):
                raise BatchTrainingError("Python model artifact must be materialized")
        owner = self.owner_payload
        if owner is None:
            if self.freeze_review_sha256 is not None or self.review_decision != "pending":
                raise BatchTrainingError(
                    "pre-freeze record requires pending decision and null freeze"
                )
        elif self.classifier_kind in (
            ClassifierKind.PRIMARY_COMPARTMENT,
            ClassifierKind.SECONDARY_STRUCTURE,
        ):
            if not isinstance(owner, PixelClassifierOwnerPayload):
                raise BatchTrainingError("pixel classifier kind requires pixel owner payload")
            definition = owner.classifier_definition
            expected_kind = (
                "primary"
                if self.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT
                else "secondary"
            )
            if (
                definition.classifier_id != self.classifier_id
                or definition.version != self.classifier_version
                or definition.kind != expected_kind
                or definition.domain_id != self.validation_domain_id
            ):
                raise BatchTrainingError("pixel owner definition differs from classifier record")
            if self.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
                if definition.validation_status != "not_evaluated":
                    raise BatchTrainingError(
                        "training-only pixel record requires not_evaluated owner"
                    )
            elif definition.validation_status == "not_evaluated":
                raise BatchTrainingError("evaluated pixel record cannot use not_evaluated owner")
        else:
            if not isinstance(owner, SupervisedClassifierOwnerPayload):
                raise BatchTrainingError(
                    "supervised classifier kind requires supervised owner payload"
                )
            if (
                owner.classifier_id != self.classifier_id
                or owner.validation_domain_id != self.validation_domain_id
            ):
                raise BatchTrainingError("supervised owner payload differs from classifier record")
            artifact_sha = (
                self.native_artifact_sha256
                if self.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE
                else self.python_model_artifact.sha256  # type: ignore[union-attr]
            )
            if owner.classifier_sha256 != artifact_sha:
                raise BatchTrainingError(
                    "supervised owner classifier SHA-256 differs from artifact"
                )
        object.__setattr__(self, "owner_payload", owner)
        object.__setattr__(
            self, "settings_revision", _compact_settings_revision(self.settings_revision)
        )
        object.__setattr__(
            self, "training_plan_id", _sha256(self.training_plan_id, "training_plan_id")
        )
        object.__setattr__(
            self,
            "accepted_training_evidence_sha256",
            _sha256(
                self.accepted_training_evidence_sha256,
                "accepted_training_evidence_sha256",
            ),
        )
        fit_fields = (
            "fit_plan_sha256",
            "fit_execution_sha256",
            "checkpoint_entry_sha256",
            "fit_material_sha256",
            "training_material_evidence_sha256",
        )
        if self.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
            if any(getattr(self, field) is None for field in fit_fields):
                raise BatchTrainingError("native classifier record fit provenance is incomplete")
            for field in fit_fields:
                object.__setattr__(self, field, _sha256(getattr(self, field), field))
        elif any(getattr(self, field) is not None for field in fit_fields):
            raise BatchTrainingError(
                "Python-model classifier record cannot carry native fit provenance"
            )
        for field in ("channel_requirements", "feature_requirements"):
            frozen = _json_object(getattr(self, field), field)
            object.__setattr__(self, field, frozen)
            digest_field = f"{field}_sha256"
            digest = _sha256(getattr(self, digest_field), digest_field)
            if digest != _digest(frozen):
                raise BatchTrainingError(f"{digest_field} is not canonical")
            object.__setattr__(self, digest_field, digest)
        for field in ("runtime_versions", "extension_versions", "model_versions"):
            object.__setattr__(
                self,
                field,
                _version_inventory(
                    getattr(self, field), field, nonempty=field == "runtime_versions"
                ),
            )
        runtime_map = dict(self.runtime_versions)
        if isinstance(owner, PixelClassifierOwnerPayload):
            definition = owner.classifier_definition
            if set(self.channel_requirements) != {"channel_roles"} or _export_json(
                self.channel_requirements["channel_roles"]
            ) != _export_json(definition.channel_roles):
                raise BatchTrainingError("pixel owner channel roles differ from classifier record")
            if _export_json(self.feature_requirements) != _export_json(
                definition.feature_signature
            ):
                raise BatchTrainingError(
                    "pixel owner feature signature differs from classifier record"
                )
            from .qupath_feature_settings import PixelImageFeatures, configured_image_features
            recipe = PixelImageFeatures.from_mapping(self.feature_requirements.get("image_features"))
            if recipe != configured_image_features(definition.training_defaults):
                raise BatchTrainingError("pixel image_features differ from the saved training settings")
            if runtime_map.get("qupath") != _qupath_executable_version_label(
                definition.runtime_signature.get("qupath_version")
            ) or runtime_map.get("script_bundle_sha256") != definition.runtime_signature.get(
                "script_bundle_sha256"
            ):
                raise BatchTrainingError(
                    "pixel owner runtime signature differs from classifier record"
                )
        elif isinstance(owner, SupervisedClassifierOwnerPayload):
            expected_features = {
                "feature_schema": [item.to_mapping() for item in owner.feature_schema]
            }
            if "feature_source" in self.feature_requirements:
                from .qupath_kronos_cell import KronosCellFeatureSource
                from .supervised_features import require_kronos_cell_schema
                source = KronosCellFeatureSource.from_mapping(self.feature_requirements["feature_source"])
                require_kronos_cell_schema(source, expected_features["feature_schema"])
                expected_features["feature_source"] = source.to_mapping()
            if _export_json(self.feature_requirements) != expected_features:
                raise BatchTrainingError(
                    "supervised owner feature schema differs from classifier record"
                )
            if runtime_map.get("qupath") != _qupath_executable_version_label(owner.qupath_version):
                raise BatchTrainingError("supervised owner runtime differs from classifier record")
        metrics = tuple(self.metrics)
        if not all(isinstance(item, ClassifierEvaluationMetrics) for item in metrics):
            raise BatchTrainingError("metrics must contain classifier evaluation metrics")
        metric_splits = {item.split for item in metrics}
        if owner is None:
            if metric_splits not in (set(), {"validation"}):
                raise BatchTrainingError("pre-freeze records permit validation metrics only")
        else:
            object.__setattr__(
                self,
                "freeze_review_sha256",
                _sha256(self.freeze_review_sha256, "freeze_review_sha256"),
            )
        if self.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
            if metrics:
                raise BatchTrainingError("training-only classifier record must not contain metrics")
            if (
                self.review_decision == "pending"
                and isinstance(owner, PixelClassifierOwnerPayload)
                and owner.classifier_definition.unattended_review_override
            ):
                raise BatchTrainingError(
                    "pending training-only pixel record cannot carry unattended-review override"
                )
            if isinstance(owner, SupervisedClassifierOwnerPayload) and any(
                (
                    owner.validation_cells,
                    owner.validation_regions,
                    owner.test_cells,
                    owner.test_regions,
                )
            ):
                raise BatchTrainingError("training-only supervised record has held-out owner data")
        elif owner is not None and (not metrics or not metric_splits & {"validation", "test"}):
            raise BatchTrainingError("evaluated classifier record requires held-out metrics")
        elif (
            owner is not None
            and self.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE
            and "test" not in metric_splits
        ):
            raise BatchTrainingError("independent classifier record requires test metrics")
        elif (
            owner is not None
            and self.evaluation_intent is EvaluationIntent.EXPLORATORY
            and "validation" not in metric_splits
        ):
            raise BatchTrainingError("exploratory classifier record requires validation metrics")
        if self.evaluation_intent is not EvaluationIntent.TRAINING_ONLY and isinstance(
            owner, PixelClassifierOwnerPayload
        ):
            owner_splits = {
                item.split
                for item in owner.classifier_definition.evidence_snapshots
                if item.split in {"validation", "test"}
            }
            if metric_splits != owner_splits:
                raise BatchTrainingError("pixel owner held-out splits differ from metrics")
        if self.evaluation_intent is not EvaluationIntent.TRAINING_ONLY and isinstance(
            owner, SupervisedClassifierOwnerPayload
        ):
            for split in ("validation", "test"):
                cells = getattr(owner, f"{split}_cells")
                regions = getattr(owner, f"{split}_regions")
                if bool(cells) != (split in metric_splits) or bool(regions) != (
                    split in metric_splits
                ):
                    raise BatchTrainingError(
                        f"evaluated supervised record requires exact {split} owner data"
                    )
        if metrics != tuple(sorted(metrics, key=lambda item: _SPLIT_RANK[item.split])):
            raise BatchTrainingError("metrics must use canonical split order")
        if len({item.split for item in metrics}) != len(metrics):
            raise BatchTrainingError("metrics must contain unique splits")
        evidence_rows: list[Mapping[str, str]] = []
        for index, raw in enumerate(self.evaluation_evidence):
            row = _closed_mapping(
                raw,
                fields=frozenset({"split", "request_identity_sha256", "result_sha256"}),
                field=f"evaluation_evidence[{index}]",
            )
            split = row["split"]
            if split not in ("validation", "test"):
                raise BatchTrainingError("evaluation evidence requires validation or test split")
            evidence_rows.append(
                MappingProxyType(
                    {
                        "split": split,
                        "request_identity_sha256": _sha256(
                            row["request_identity_sha256"], "request_identity_sha256"
                        ),
                        "result_sha256": _sha256(row["result_sha256"], "result_sha256"),
                    }
                )
            )
        if tuple(row["split"] for row in evidence_rows) != tuple(item.split for item in metrics):
            raise BatchTrainingError(
                "evaluation evidence must cover metrics in canonical split order"
            )
        object.__setattr__(self, "evaluation_evidence", tuple(evidence_rows))
        for item in metrics:
            if item.pixel_scores is not None and (
                    item.pixel_scores["fit_execution_sha256"] != self.fit_execution_sha256
                    or any(row["model_sha256"] != self.native_artifact_sha256
                           for row in item.pixel_scores["entries"])):
                raise BatchTrainingError("Held-out score evidence belongs to a different fitted classifier")
        if self.evaluation_intent is not EvaluationIntent.TRAINING_ONLY:
            if self.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
                owner_classes = (
                    tuple(
                        class_id
                        for class_id in owner.classifier_definition.output_classes
                        if class_id != IGNORE_CLASS_ID
                    )
                    if isinstance(owner, PixelClassifierOwnerPayload)
                    else None
                )
                if any(
                    item.pixel_confusion is None
                    or (
                        owner_classes is not None
                        and item.pixel_confusion.class_ids != owner_classes
                    )
                    for item in metrics
                ):
                    raise BatchTrainingError(
                        "evaluated pixel metrics require confusion counts in owner class order"
                    )
            elif any(item.pixel_confusion is not None for item in metrics):
                raise BatchTrainingError("supervised metrics must have null pixel confusion")
        object.__setattr__(self, "metrics", metrics)
        if self.review_decision not in ("pending", "accepted"):
            raise BatchTrainingError("review_decision must be pending or accepted")
        object.__setattr__(
            self,
            "validation_domain_id",
            _text(self.validation_domain_id, "validation_domain_id"),
        )
        record_sha = _sha256(self.classifier_record_sha256, "classifier_record_sha256")
        if record_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("classifier_record_sha256 is not canonical")
        object.__setattr__(self, "classifier_record_sha256", record_sha)

    @classmethod
    def create(
        cls,
        *,
        plan: TrainingPlan,
        plan_set: TrainingPlanSet,
        classifier_version: str,
        artifact_kind: ClassifierArtifactKind,
        native_artifact_project_relative_path: str | None,
        native_source_project: ProjectDesignation | None,
        python_model_authority: BatchCommittedArtifactAuthority | None,
        python_model_identity_contract: ArtifactIdentityContract | None,
        owner_payload: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload | None,
        complete_training_evidence: (
            MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate
        ),
        runtime_versions: tuple[tuple[str, str], ...],
        extension_versions: tuple[tuple[str, str], ...],
        model_versions: tuple[tuple[str, str], ...],
        feature_requirements: Mapping[str, object],
        held_out_evaluations: tuple[HeldOutClassifierEvaluation, ...],
        retained_record: ClassifierRecord | None,
        freeze_review_sha256: str | None,
        fit_plan_sha256: str | None,
        fit_execution_sha256: str | None,
        checkpoint_entry_sha256: str | None,
        fit_material_sha256: str | None,
        training_material_evidence_sha256: str | None,
        review_decision: str,
    ) -> "ClassifierRecord":
        return cls._prepare_record_data(
            plan=plan,
            plan_set=plan_set,
            classifier_version=classifier_version,
            artifact_kind=artifact_kind,
            native_artifact_project_relative_path=(native_artifact_project_relative_path),
            native_source_project=native_source_project,
            python_model_authority=python_model_authority,
            python_model_identity_contract=python_model_identity_contract,
            owner_payload=owner_payload,
            complete_training_evidence=complete_training_evidence,
            runtime_versions=runtime_versions,
            extension_versions=extension_versions,
            model_versions=model_versions,
            feature_requirements=feature_requirements,
            held_out_evaluations=held_out_evaluations,
            retained_record=retained_record,
            freeze_review_sha256=freeze_review_sha256,
            fit_plan_sha256=fit_plan_sha256,
            fit_execution_sha256=fit_execution_sha256,
            checkpoint_entry_sha256=checkpoint_entry_sha256,
            fit_material_sha256=fit_material_sha256,
            training_material_evidence_sha256=(training_material_evidence_sha256),
            review_decision=review_decision,
        ).consume()

    @classmethod
    def _prepare_record_data(
        cls,
        *,
        plan: TrainingPlan,
        plan_set: TrainingPlanSet,
        classifier_version: str,
        artifact_kind: ClassifierArtifactKind,
        native_artifact_project_relative_path: str | None,
        native_source_project: ProjectDesignation | None,
        python_model_authority: BatchCommittedArtifactAuthority | None,
        python_model_identity_contract: ArtifactIdentityContract | None,
        owner_payload: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload | None,
        complete_training_evidence: (
            MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate
        ),
        runtime_versions: tuple[tuple[str, str], ...],
        extension_versions: tuple[tuple[str, str], ...],
        model_versions: tuple[tuple[str, str], ...],
        feature_requirements: Mapping[str, object],
        held_out_evaluations: tuple[HeldOutClassifierEvaluation, ...],
        retained_record: ClassifierRecord | None,
        freeze_review_sha256: str | None,
        fit_plan_sha256: str | None,
        fit_execution_sha256: str | None,
        checkpoint_entry_sha256: str | None,
        fit_material_sha256: str | None,
        training_material_evidence_sha256: str | None,
        review_decision: str,
    ) -> _PreparedClassifierRecordAcceptance:
        if not isinstance(plan, TrainingPlan) or not isinstance(plan_set, TrainingPlanSet):
            raise BatchTrainingError("record acceptance requires exact plan and plan set")
        if plan not in plan_set.plans:
            raise BatchTrainingError("training plan is absent from the accepted plan set")
        if (
            plan_set.normalized_manifest_sha256 != plan.normalized_manifest_sha256
            or plan_set.validation_domain_id != plan.request.validation_domain_id
            or plan_set.classifier_kind is not plan.request.classifier_kind
        ):
            raise BatchTrainingError("training plan set differs from the selected plan")
        frozen = freeze_review_sha256 is not None
        if frozen:
            _sha256(freeze_review_sha256, "freeze_review_sha256")
            if not isinstance(
                owner_payload, (PixelClassifierOwnerPayload, SupervisedClassifierOwnerPayload)
            ):
                raise BatchTrainingError("frozen record requires a typed owner payload")
            if (
                type(retained_record) is not ClassifierRecord
                or retained_record.owner_payload is not None
                or retained_record.freeze_review_sha256 is not None
                or retained_record.review_decision != "pending"
            ):
                raise BatchTrainingError("frozen record requires a retained pre-freeze record")
        elif (
            owner_payload is not None or retained_record is not None or review_decision != "pending"
        ):
            raise BatchTrainingError("pre-freeze record requires pending decision and null owner")
        runtime_versions = _version_inventory(
            runtime_versions,
            "runtime_versions",
            nonempty=True,
            require_canonical=False,
        )
        extension_versions = _version_inventory(
            extension_versions,
            "extension_versions",
            require_canonical=False,
        )
        model_versions = _version_inventory(
            model_versions,
            "model_versions",
            require_canonical=False,
        )
        if owner_payload is not None:
            _validate_record_owner_plan(
                plan,
                owner_payload,
                classifier_version=classifier_version,
                runtime_versions=runtime_versions,
            )
        _validate_record_training_evidence(
            plan,
            owner_payload,
            complete_training_evidence,
        )
        if not isinstance(artifact_kind, ClassifierArtifactKind):
            raise BatchTrainingError("artifact_kind must be a ClassifierArtifactKind")
        native_path = None
        native_sha = None
        native_count = None
        native_project_identity = None
        python_model_artifact = None
        if artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
            if not isinstance(native_source_project, ProjectDesignation):
                raise BatchTrainingError("native acceptance requires retained project authority")
            native_path = _normalized_native_locator(
                native_artifact_project_relative_path, require_canonical=False
            )
            try:
                project_path = native_source_project.project_path.resolve(strict=True)
                project_payload = project_path.read_bytes()
                if (
                    canonical_project_uri(project_path) != native_source_project.project_uri
                    or not same_project_snapshot(
                        project_path,
                        native_source_project.authority_head.qpproj_snapshot_sha256,
                        hashlib.sha256(project_payload).hexdigest(),
                        observed_payload=project_payload,
                    )
                ):
                    raise BatchTrainingError("native source project authority is not current")
                project_root = project_path.parent
                classifier_root = (project_root / "classifiers").resolve(strict=True)
                resolved = (project_root / Path(*PurePosixPath(native_path).parts)).resolve(
                    strict=True
                )
                resolved.relative_to(classifier_root)
            except (OSError, ValueError) as exc:
                raise BatchTrainingError(
                    "native artifact locator escapes retained project authority"
                ) from exc
            native_sha, native_count = _read_exact_artifact_bytes(
                resolved,
                None,
                None,
                field="native artifact",
            )
            native_project_identity = native_source_project.identity.project_identity_sha256
            if python_model_authority is not None or python_model_identity_contract is not None:
                raise BatchTrainingError("native and Python artifact representations are exclusive")
        else:
            if (
                native_source_project is not None
                or native_artifact_project_relative_path is not None
            ):
                raise BatchTrainingError("native and Python artifact representations are exclusive")
            if not isinstance(python_model_identity_contract, ArtifactIdentityContract):
                raise BatchTrainingError("Python model acceptance requires exact identity contract")
            if not isinstance(python_model_authority, BatchCommittedArtifactAuthority):
                raise BatchTrainingError(
                    "Python model acceptance requires committed model authority"
                )
            try:
                (validated_model_authority,) = validate_committed_artifact_authority_set(
                    (python_model_authority,)
                )
            except (BatchArtifactError, TypeError, ValueError) as exc:
                raise BatchTrainingError("Python model committed authority is invalid") from exc
            expected_producer_kind = _classifier_training_producer_kind(
                plan.request.classifier_kind
            )
            if validated_model_authority.producer_node_kind != expected_producer_kind:
                raise BatchTrainingError("Python model committed authority has a foreign producer")
            python_model_artifact = validated_model_authority.artifact_ref
            if python_model_artifact.identity_contract != python_model_identity_contract:
                raise BatchTrainingError("Python model artifact has a different identity contract")
            expected_samples = tuple(sorted({item.sample_id for item in plan.request.entries}))
            python_model_artifact = _validate_python_model_artifact_binding(
                python_model_artifact,
                classifier_id=plan.request.classifier_id,
                classifier_version=classifier_version,
                training_plan_id=plan.training_plan_id,
                accepted_training_evidence_sha256=(
                    complete_training_evidence.material_evidence_sha256
                ),
                validation_domain_id=plan.request.validation_domain_id,
                normalized_manifest_sha256=plan.normalized_manifest_sha256,
                model_configuration_sha256=_digest(plan.request.model_configuration),
                expected_sample_ids=expected_samples,
            )
            _read_exact_artifact_bytes(
                python_model_artifact.locator,
                python_model_artifact.sha256,
                python_model_artifact.byte_count,
                field="Python model artifact",
            )
        if type(held_out_evaluations) is not tuple:
            raise BatchTrainingError("held-out evaluations must be a tuple")
        from .evaluation import HeldOutClassifierEvaluation

        expected_new_splits = (
            ("test",)
            if frozen and plan.split_project_entry_ids["test"]
            else () if frozen or not plan.split_project_entry_ids["validation"] else ("validation",)
        )
        if tuple(
            getattr(item, "evaluation_split", None) for item in held_out_evaluations
        ) != expected_new_splits or any(
            type(item) is not HeldOutClassifierEvaluation for item in held_out_evaluations
        ):
            raise BatchTrainingError("record phase requires exact held-out evaluation splits")
        expected_artifact_sha256 = (
            native_sha
            if artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE
            else python_model_artifact.sha256
        )
        for proof in held_out_evaluations:
            if (
                proof.training_plan_id != plan.training_plan_id
                or proof.classifier_id != plan.request.classifier_id
                or proof.classifier_kind is not plan.request.classifier_kind
                or proof.validation_domain_id != plan.request.validation_domain_id
                or proof.native_classifier_sha256 != expected_artifact_sha256
                or proof.freeze_review_sha256 != (freeze_review_sha256 if frozen else None)
                or tuple(item.split for item in proof.metrics) != (proof.evaluation_split,)
            ):
                raise BatchTrainingError("held-out classifier evaluation differs from record phase")
            if artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE and (
                fit_plan_sha256,
                fit_execution_sha256,
                checkpoint_entry_sha256,
                fit_material_sha256,
            ) != (
                proof.fit_plan_sha256,
                proof.fit_execution_sha256,
                proof.checkpoint_entry_sha256,
                proof.fit_material_sha256,
            ):
                raise BatchTrainingError("native fit provenance differs from held-out evaluation")
        if artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
            _sha256(training_material_evidence_sha256, "training_material_evidence_sha256")
        elif any(
            item is not None
            for item in (
                fit_plan_sha256,
                fit_execution_sha256,
                checkpoint_entry_sha256,
                fit_material_sha256,
                training_material_evidence_sha256,
            )
        ):
            raise BatchTrainingError("Python-model acceptance cannot carry native fit provenance")
        if review_decision not in ("pending", "accepted"):
            raise BatchTrainingError("review_decision must be pending or accepted")
        retained_metrics = () if retained_record is None else retained_record.metrics
        retained_evidence = () if retained_record is None else retained_record.evaluation_evidence
        metric_tuple = retained_metrics + tuple(
            metric for proof in held_out_evaluations for metric in proof.metrics
        )
        evidence_tuple = retained_evidence + tuple(
            {
                "split": proof.evaluation_split,
                "request_identity_sha256": proof.request_identity_sha256,
                "result_sha256": proof.result_sha256,
            }
            for proof in held_out_evaluations
        )
        _validate_record_metrics(plan, metric_tuple, frozen=frozen)
        channel_requirements = plan.request.channel_requirements or {}
        feature_requirements = _json_object(feature_requirements, "feature_requirements")
        if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            from .supervised_features import supervised_snapshot_source
            schema = list((plan.request.feature_requirements or {})["feature_schema"])
            expected_features = {
                "feature_schema": schema,
                **supervised_snapshot_source(plan.request.model_configuration, schema),
            }
            if _export_json(feature_requirements) != expected_features:
                raise BatchTrainingError("supervised feature requirements differ from plan")
        elif (
            set(feature_requirements) != {"feature_policy", "ordered_channel_ids", "image_features"}
            or feature_requirements["feature_policy"] != "pixel-channels/v1"
            or not isinstance(feature_requirements["ordered_channel_ids"], (tuple, list))
            or not feature_requirements["ordered_channel_ids"]
            or any(
                not isinstance(item, str) or not item.strip()
                for item in feature_requirements["ordered_channel_ids"]
            )
            or len(set(feature_requirements["ordered_channel_ids"]))
            != len(feature_requirements["ordered_channel_ids"])
            or not set(channel_requirements["channel_roles"].values()).issubset(
                set(feature_requirements["ordered_channel_ids"])
            )
        ):
            raise BatchTrainingError("pixel feature requirements differ from planned channels")
        if plan.request.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
            from .qupath_feature_settings import configured_image_features, PixelImageFeatures
            if PixelImageFeatures.from_mapping(feature_requirements["image_features"]) != configured_image_features(plan.request.model_configuration):
                raise BatchTrainingError("pixel image_features differ from planned image preparation")
        if retained_record is not None and _export_json(
            retained_record.feature_requirements
        ) != _export_json(feature_requirements):
            raise BatchTrainingError("frozen feature requirements differ from reviewed fit")
        settings_revision = _plan_set_settings_revision(plan_set)
        if retained_record is not None and retained_record.settings_revision != settings_revision:
            raise BatchTrainingError("frozen settings revision differs from reviewed fit")
        if isinstance(owner_payload, PixelClassifierOwnerPayload) and _export_json(
            feature_requirements
        ) != _export_json(owner_payload.classifier_definition.feature_signature):
            raise BatchTrainingError("pixel owner feature signature differs from reviewed fit")
        unsigned = {
            "classifier_id": plan.request.classifier_id,
            "classifier_version": classifier_version,
            "classifier_kind": plan.request.classifier_kind,
            "classifier_engine": plan.classifier_engine,
            "evaluation_intent": plan.request.evaluation_intent,
            "artifact_kind": artifact_kind,
            "native_artifact_project_relative_path": native_path,
            "native_artifact_sha256": native_sha,
            "native_artifact_byte_count": native_count,
            "native_source_project_identity_sha256": native_project_identity,
            "python_model_artifact": python_model_artifact,
            "owner_payload": owner_payload,
            "training_plan_id": plan.training_plan_id,
            "accepted_training_evidence_sha256": (
                complete_training_evidence.material_evidence_sha256
            ),
            "fit_plan_sha256": fit_plan_sha256,
            "fit_execution_sha256": fit_execution_sha256,
            "checkpoint_entry_sha256": checkpoint_entry_sha256,
            "fit_material_sha256": fit_material_sha256,
            "training_material_evidence_sha256": (training_material_evidence_sha256),
            "channel_requirements": channel_requirements,
            "channel_requirements_sha256": _digest(channel_requirements),
            "feature_requirements": feature_requirements,
            "feature_requirements_sha256": _digest(feature_requirements),
            "runtime_versions": runtime_versions,
            "extension_versions": extension_versions,
            "model_versions": model_versions,
            "metrics": metric_tuple,
            "evaluation_evidence": evidence_tuple,
            "freeze_review_sha256": freeze_review_sha256,
            "review_decision": review_decision,
            "validation_domain_id": plan.request.validation_domain_id,
            "settings_revision": settings_revision,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        record = cls(
            **unsigned,
            classifier_record_sha256=_digest(prototype._unsigned_mapping()),
        )
        if retained_record is not None:
            unchanged = (
                "classifier_id",
                "classifier_version",
                "classifier_kind",
                "classifier_engine",
                "evaluation_intent",
                "artifact_kind",
                "native_artifact_project_relative_path",
                "native_artifact_sha256",
                "native_artifact_byte_count",
                "native_source_project_identity_sha256",
                "python_model_artifact",
                "training_plan_id",
                "accepted_training_evidence_sha256",
                "fit_plan_sha256",
                "fit_execution_sha256",
                "checkpoint_entry_sha256",
                "fit_material_sha256",
                "training_material_evidence_sha256",
                "channel_requirements_sha256",
                "feature_requirements_sha256",
                "runtime_versions",
                "extension_versions",
                "model_versions",
                "validation_domain_id",
                "settings_revision",
            )
            if any(
                getattr(record, field) != getattr(retained_record, field) for field in unchanged
            ):
                raise BatchTrainingError("frozen record differs from retained reviewed fit")
        return _PreparedClassifierRecordAcceptance(record, held_out_evaluations)

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        python_model_identity_contract: ArtifactIdentityContract | None = None,
    ) -> "ClassifierRecord":
        """Decode the serialized fields into a validated ClassifierRecord record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        if not isinstance(value, Mapping) or (
            value.get("schema_name"),
            value.get("schema_version"),
        ) != (cls.schema_name, cls.schema_version):
            raise BatchTrainingError("classifier record schema is unsupported")
        fields = frozenset({"schema_name", "schema_version", *cls.__dataclass_fields__})
        mapping = _closed_mapping(value, fields=fields, field="classifier_record")
        if (
            mapping["schema_name"] != cls.schema_name
            or mapping["schema_version"] != cls.schema_version
        ):
            raise BatchTrainingError("classifier record schema is unsupported")
        kind = _enum(mapping["classifier_kind"], ClassifierKind, "classifier_kind")
        artifact_kind = _enum(mapping["artifact_kind"], ClassifierArtifactKind, "artifact_kind")
        if mapping["owner_payload"] is None:
            owner = None
        elif kind in (ClassifierKind.PRIMARY_COMPARTMENT, ClassifierKind.SECONDARY_STRUCTURE):
            owner = PixelClassifierOwnerPayload.from_mapping(mapping["owner_payload"])
        else:
            owner = SupervisedClassifierOwnerPayload.from_mapping(mapping["owner_payload"])
        raw_python = mapping["python_model_artifact"]
        if raw_python is None:
            python_artifact = None
        else:
            if python_model_identity_contract is None:
                raise BatchTrainingError(
                    "historical Python model parsing requires its named identity contract"
                )
            try:
                python_artifact = BatchArtifactRef.from_mapping(
                    raw_python, identity_contract=python_model_identity_contract
                )
            except BatchArtifactError as exc:
                raise BatchTrainingError("Python model artifact is invalid") from exc
        return cls(
            classifier_id=mapping["classifier_id"],  # type: ignore[arg-type]
            classifier_version=mapping["classifier_version"],  # type: ignore[arg-type]
            classifier_kind=kind,
            classifier_engine=mapping["classifier_engine"],  # type: ignore[arg-type]
            evaluation_intent=_enum(
                mapping["evaluation_intent"], EvaluationIntent, "evaluation_intent"
            ),
            artifact_kind=artifact_kind,
            native_artifact_project_relative_path=mapping["native_artifact_project_relative_path"],  # type: ignore[arg-type]
            native_artifact_sha256=mapping["native_artifact_sha256"],  # type: ignore[arg-type]
            native_artifact_byte_count=mapping["native_artifact_byte_count"],  # type: ignore[arg-type]
            native_source_project_identity_sha256=mapping["native_source_project_identity_sha256"],  # type: ignore[arg-type]
            python_model_artifact=python_artifact,
            owner_payload=owner,
            training_plan_id=mapping["training_plan_id"],  # type: ignore[arg-type]
            accepted_training_evidence_sha256=mapping["accepted_training_evidence_sha256"],  # type: ignore[arg-type]
            fit_plan_sha256=mapping["fit_plan_sha256"],  # type: ignore[arg-type]
            fit_execution_sha256=mapping["fit_execution_sha256"],  # type: ignore[arg-type]
            checkpoint_entry_sha256=mapping["checkpoint_entry_sha256"],  # type: ignore[arg-type]
            fit_material_sha256=mapping["fit_material_sha256"],  # type: ignore[arg-type]
            training_material_evidence_sha256=mapping["training_material_evidence_sha256"],  # type: ignore[arg-type]
            channel_requirements=mapping["channel_requirements"],  # type: ignore[arg-type]
            channel_requirements_sha256=mapping["channel_requirements_sha256"],  # type: ignore[arg-type]
            feature_requirements=mapping["feature_requirements"],  # type: ignore[arg-type]
            feature_requirements_sha256=mapping["feature_requirements_sha256"],  # type: ignore[arg-type]
            runtime_versions=_version_inventory(
                mapping["runtime_versions"], "runtime_versions", nonempty=True
            ),
            extension_versions=_version_inventory(
                mapping["extension_versions"], "extension_versions"
            ),
            model_versions=_version_inventory(mapping["model_versions"], "model_versions"),
            metrics=tuple(
                ClassifierEvaluationMetrics.from_mapping(item)
                for item in _array(mapping["metrics"], "metrics")
            ),
            evaluation_evidence=tuple(
                _closed_mapping(
                    item,
                    fields=frozenset({"split", "request_identity_sha256", "result_sha256"}),
                    field="evaluation_evidence",
                )
                for item in _array(mapping["evaluation_evidence"], "evaluation_evidence")
            ),
            freeze_review_sha256=mapping["freeze_review_sha256"],  # type: ignore[arg-type]
            review_decision=mapping["review_decision"],  # type: ignore[arg-type]
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            settings_revision=mapping["settings_revision"],  # type: ignore[arg-type]
            classifier_record_sha256=mapping["classifier_record_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
            "classifier_kind": self.classifier_kind.value,
            "classifier_engine": self.classifier_engine,
            "evaluation_intent": self.evaluation_intent.value,
            "artifact_kind": self.artifact_kind.value,
            "native_artifact_project_relative_path": self.native_artifact_project_relative_path,
            "native_artifact_sha256": self.native_artifact_sha256,
            "native_artifact_byte_count": self.native_artifact_byte_count,
            "native_source_project_identity_sha256": self.native_source_project_identity_sha256,
            "python_model_artifact": (
                None
                if self.python_model_artifact is None
                else self.python_model_artifact.to_mapping()
            ),
            "owner_payload": (
                None if self.owner_payload is None else self.owner_payload.to_mapping()
            ),
            "training_plan_id": self.training_plan_id,
            "accepted_training_evidence_sha256": self.accepted_training_evidence_sha256,
            "fit_plan_sha256": self.fit_plan_sha256,
            "fit_execution_sha256": self.fit_execution_sha256,
            "checkpoint_entry_sha256": self.checkpoint_entry_sha256,
            "fit_material_sha256": self.fit_material_sha256,
            "training_material_evidence_sha256": (self.training_material_evidence_sha256),
            "channel_requirements": _export_json(self.channel_requirements),
            "channel_requirements_sha256": self.channel_requirements_sha256,
            "feature_requirements": _export_json(self.feature_requirements),
            "feature_requirements_sha256": self.feature_requirements_sha256,
            "runtime_versions": [list(item) for item in self.runtime_versions],
            "extension_versions": [list(item) for item in self.extension_versions],
            "model_versions": [list(item) for item in self.model_versions],
            "metrics": [item.to_mapping() for item in self.metrics],
            "evaluation_evidence": [dict(item) for item in self.evaluation_evidence],
            "freeze_review_sha256": self.freeze_review_sha256,
            "review_decision": self.review_decision,
            "validation_domain_id": self.validation_domain_id,
            "settings_revision": (
                None if self.settings_revision is None else dict(self.settings_revision)
            ),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "classifier_record_sha256": self.classifier_record_sha256,
        }


@dataclass(frozen=True, slots=True)
class ClassifierRecordSet:
    schema_name: ClassVar[str] = "mif-classifier-record-set"
    schema_version: ClassVar[int] = 1

    classifier_records: tuple[ClassifierRecord, ...]
    classifier_kind: ClassifierKind
    validation_domain_id: str
    source_training_plan_set_sha256: str
    output_inventory: tuple[tuple[str, str], ...]
    classifier_record_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClassifierRecordSet fields when the record is constructed.

        The checks cover ``classifier_records``, ``validation_domain_id``,
        ``classifier_record_set_sha256``, ``classifier_kind``, ``output_inventory``,
        ``source_training_plan_set_sha256``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        records = tuple(self.classifier_records)
        if not records or not all(isinstance(item, ClassifierRecord) for item in records):
            raise BatchTrainingError("classifier_records must contain typed records")
        if records != tuple(
            sorted(records, key=lambda item: (item.classifier_id, item.classifier_record_sha256))
        ):
            raise BatchTrainingError("classifier_records must use canonical order")
        if len({item.classifier_record_sha256 for item in records}) != len(records):
            raise BatchTrainingError("classifier_records must be unique")
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        validation_domain_id = _text(self.validation_domain_id, "validation_domain_id")
        if any(
            item.classifier_kind is not self.classifier_kind
            or item.validation_domain_id != validation_domain_id
            for item in records
        ):
            raise BatchTrainingError("classifier records differ from their record-set owner")
        if self.classifier_kind is not ClassifierKind.SECONDARY_STRUCTURE and len(records) != 1:
            raise BatchTrainingError(
                "primary and supervised record sets require exactly one record"
            )
        inventory: list[tuple[str, str]] = []
        for index, pair in enumerate(_array(self.output_inventory, "output_inventory")):
            values = _array(pair, f"output_inventory[{index}]")
            if len(values) != 2:
                raise BatchTrainingError("output_inventory entries must be output/digest pairs")
            inventory.append(
                (
                    _text(values[0], f"output_inventory[{index}].output_id"),
                    _sha256(values[1], f"output_inventory[{index}].classifier_record_sha256"),
                )
            )
        parsed_inventory = tuple(inventory)
        if parsed_inventory != tuple(sorted(parsed_inventory)) or len(
            {item[0] for item in parsed_inventory}
        ) != len(parsed_inventory):
            raise BatchTrainingError("output_inventory must be canonical with unique output IDs")
        if len(parsed_inventory) != len(records) or {item[1] for item in parsed_inventory} != {
            item.classifier_record_sha256 for item in records
        }:
            raise BatchTrainingError("output_inventory must cover records bijectively")
        object.__setattr__(self, "classifier_records", records)
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        object.__setattr__(
            self,
            "source_training_plan_set_sha256",
            _sha256(
                self.source_training_plan_set_sha256,
                "source_training_plan_set_sha256",
            ),
        )
        object.__setattr__(self, "output_inventory", parsed_inventory)
        set_sha = _sha256(self.classifier_record_set_sha256, "classifier_record_set_sha256")
        if set_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("classifier_record_set_sha256 is not canonical")
        object.__setattr__(self, "classifier_record_set_sha256", set_sha)

    @classmethod
    def create(
        cls,
        plan_set: TrainingPlanSet,
        output_records: tuple[tuple[str, ClassifierRecord], ...],
        *,
        secondary_structure_specs: tuple[SecondaryStructureSpec, ...] = (),
    ) -> "ClassifierRecordSet":
        if not isinstance(plan_set, TrainingPlanSet):
            raise BatchTrainingError("source plan set must be a TrainingPlanSet")
        pairs = tuple(sorted(output_records, key=lambda item: item[0]))
        if not pairs or not all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], ClassifierRecord)
            for item in pairs
        ):
            raise BatchTrainingError("output_records must contain output/record pairs")
        plans = {item.request.classifier_id: item for item in plan_set.plans}
        if plan_set.classifier_kind is ClassifierKind.SECONDARY_STRUCTURE:
            specs = tuple(secondary_structure_specs)
            if not specs or not all(isinstance(item, SecondaryStructureSpec) for item in specs):
                raise BatchTrainingError(
                    "secondary record sets require configured SecondaryStructureSpec values"
                )
            from mif_pipeline.pipeline_core.config import secondary_classifier_groups
            groups = secondary_classifier_groups(specs)
            if set(groups) != set(plans):
                raise BatchTrainingError("secondary classifiers differ from exact training plan set outputs")
        elif secondary_structure_specs:
            raise BatchTrainingError("secondary structure specs are foreign to this record set")
        if {item[0] for item in pairs} != set(plans):
            raise BatchTrainingError("output_records differ from exact training plan set outputs")
        plan_by_output = plans
        for output_id, record in pairs:
            plan = plan_by_output[output_id]
            if (
                record.training_plan_id != plan.training_plan_id
                or record.classifier_id != plan.request.classifier_id
                or record.classifier_kind is not plan_set.classifier_kind
                or record.evaluation_intent is not plan.request.evaluation_intent
                or record.validation_domain_id != plan_set.validation_domain_id
            ):
                raise BatchTrainingError("classifier record differs from its exact training plan")
            if record.settings_revision != _plan_set_settings_revision(plan_set):
                raise BatchTrainingError(
                    "classifier record settings revision differs from plan set"
                )
        records = tuple(
            sorted(
                (item[1] for item in pairs),
                key=lambda item: (item.classifier_id, item.classifier_record_sha256),
            )
        )
        inventory = tuple(
            (output_id, record.classifier_record_sha256) for output_id, record in pairs
        )
        unsigned = {
            "classifier_records": records,
            "classifier_kind": plan_set.classifier_kind,
            "validation_domain_id": plan_set.validation_domain_id,
            "source_training_plan_set_sha256": plan_set.training_plan_set_sha256,
            "output_inventory": inventory,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, classifier_record_set_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        python_model_identity_contract: ArtifactIdentityContract | None = None,
    ) -> "ClassifierRecordSet":
        fields = frozenset({"schema_name", "schema_version", *cls.__dataclass_fields__})
        mapping = _closed_mapping(value, fields=fields, field="classifier_record_set")
        if (
            mapping["schema_name"] != cls.schema_name
            or mapping["schema_version"] != cls.schema_version
        ):
            raise BatchTrainingError("classifier record-set schema is unsupported")
        return cls(
            classifier_records=tuple(
                ClassifierRecord.from_mapping(
                    item,
                    python_model_identity_contract=python_model_identity_contract,
                )
                for item in _array(mapping["classifier_records"], "classifier_records")
            ),
            classifier_kind=_enum(mapping["classifier_kind"], ClassifierKind, "classifier_kind"),
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            source_training_plan_set_sha256=mapping["source_training_plan_set_sha256"],  # type: ignore[arg-type]
            output_inventory=tuple(
                tuple(_array(item, "output_inventory item"))
                for item in _array(mapping["output_inventory"], "output_inventory")
            ),  # type: ignore[arg-type]
            classifier_record_set_sha256=mapping["classifier_record_set_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "classifier_records": [item.to_mapping() for item in self.classifier_records],
            "classifier_kind": self.classifier_kind.value,
            "validation_domain_id": self.validation_domain_id,
            "source_training_plan_set_sha256": self.source_training_plan_set_sha256,
            "output_inventory": [list(item) for item in self.output_inventory],
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "classifier_record_set_sha256": self.classifier_record_set_sha256,
        }


def _require_accepted_classifier_record_set(record_set: ClassifierRecordSet) -> None:
    """Require every record in a completed or application-bound set to be accepted."""
    if any(record.review_decision != "accepted" for record in record_set.classifier_records):
        raise BatchTrainingError(
            "These classifiers have not been accepted. Open Reviews to check the current results."
        )
