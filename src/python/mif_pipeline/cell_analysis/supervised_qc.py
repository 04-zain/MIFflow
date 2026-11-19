"""

Calculates supervised cell-classification results from the held-out reference labels.

We match the reference and predicted class for every held-out cell using its
saved cell ID. The confusion matrix counts reference classes in rows and
predicted classes in columns. A supplied Unclassified prediction is counted
separately from those class-to-class entries. The evaluation uses the given
labels and does not infer a confidence threshold from them.

Every held-out cell still contributes to its reference class's denominator
for recall and to the denominator for overall accuracy. Excluding rejected
predictions from these denominators would make rejecting difficult cells
appear to improve performance. Precision uses the number of predictions
actually assigned to the class. Macro precision, recall and F1 average over
all declared classes, with zero assigned when a class ratio is undefined.

These are cell-count summaries of the supplied evaluation population.
Subject assignment and subject-balanced cross-validation scores are established
by the training workflow before this result is constructed.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np

from .supervised import (
    SupervisedDeploymentClassifierIdentity,
    SupervisedPhenotypingError,
)

# ==============================================================================
#                        Held-out cell evaluation records
# ==============================================================================


def _nonblank(value: object, field: str) -> str:
    """

    Checks that an evaluation ID or class name contains nonblank text.

    Args:
        value: Supplied text value.
        field: Field description included in an error.

    Returns:
        Checked text used to match model, measurement or cell IDs.

    Raises:
        SupervisedPhenotypingError: Text is missing or blank.
    """

    if not isinstance(value, str) or not value.strip():
        raise SupervisedPhenotypingError(f"{field} must be a non-empty string")
    return value


def _ordered_strings(value: object, field: str) -> tuple[str, ...]:
    """

    Checks the distinct class, feature or cell IDs used by an evaluation.

    Args:
        value: Nonempty sequence of text IDs.
        field: Sequence name included in a validation error.

    Returns:
        Tuple of checked IDs in their declared order.

    Raises:
        SupervisedPhenotypingError: The sequence is empty, invalid or contains duplicates.
    """

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SupervisedPhenotypingError(f"{field} must be a sequence of strings")
    values = tuple(_nonblank(item, field) for item in value)
    if not values or len(set(values)) != len(values):
        raise SupervisedPhenotypingError(f"{field} must contain unique non-empty strings")
    return values


def _bounded_metric(value: object, field: str) -> float:
    """

    Requires a finite precision, recall, F1, accuracy or confidence threshold in [0, 1].

    Args:
        value: Integer or float supplied for the metric; boolean flags are excluded.
        field: Quantity name included in an error.

    Returns:
        Float within the closed unit interval.

    Raises:
        SupervisedPhenotypingError: Type, finiteness or interval checks fail.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupervisedPhenotypingError(f"{field} must be numeric")
    try:
        output = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise SupervisedPhenotypingError(f"{field} must be a finite numeric value") from error
    if not isfinite(output) or output < 0.0 or output > 1.0:
        raise SupervisedPhenotypingError(f"{field} must be finite and between zero and one")
    return output


def _freeze_json(value: object, field: str) -> object:
    """

    Copies evaluation settings into protected dictionaries and tuple sequences.

    We recursively copy mapping values and sequence items, requiring text keys
    and finite numerical values. The resulting settings can be retained with
    the result while the original caller continues editing its own dictionaries.

    Args:
        value: JSON-compatible scalar or nested setting.
        field: Field path identifying an unsupported nested value.

    Returns:
        Copied mappings and tuples containing the checked settings.

    Raises:
        SupervisedPhenotypingError: A key, number or nested value is unsupported.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise SupervisedPhenotypingError(f"{field} must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        try:
            items = tuple(value.items())
        except (TypeError, ValueError) as error:
            raise SupervisedPhenotypingError(f"{field} must be a valid mapping") from error
        if any(not isinstance(key, str) for key, _item in items):
            raise SupervisedPhenotypingError(f"{field} mapping keys must be strings")
        return MappingProxyType({key: _freeze_json(item, f"{field}.{key}") for key, item in items})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, field) for item in value)
    raise SupervisedPhenotypingError(f"{field} must contain only JSON-compatible values")


def _export_json(value: object) -> object:
    """

    Converts retained evaluation settings to dictionaries and lists for JSON output.

    Args:
        value: Nested saved mappings, tuples or scalar values.

    Returns:
        JSON-compatible copy with the original values and sequence order.
    """

    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FrozenConfusionMatrix:
    """

    Stores the observed reference/prediction counts as a fixed collection of rows.

    Attributes:
        rows: Tuple of rows containing nonnegative integer counts. The enclosing
            evaluation requires a square matrix with one row and column per
            declared class.
    """

    rows: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        """

        Checks equal row lengths and nonnegative integer confusion counts.

        We convert integer values to Python integers and retain nested tuples.
        Boolean values are rejected because an evaluation flag must not become a
        one-cell count.

        Raises:
            SupervisedPhenotypingError: A row has an unsupported shape or count type/value.
        """

        if isinstance(self.rows, (str, bytes, bytearray)) or not isinstance(self.rows, Sequence):
            raise SupervisedPhenotypingError("confusion matrix must contain rows")
        frozen_rows: list[tuple[int, ...]] = []
        try:
            for row in self.rows:
                if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
                    raise SupervisedPhenotypingError("confusion matrix must contain rows")
                if any(
                    isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 0
                    for value in row
                ):
                    raise SupervisedPhenotypingError(
                        "confusion matrix values must be non-negative integers"
                    )
                frozen_rows.append(tuple(int(value) for value in row))
        except SupervisedPhenotypingError:
            raise
        except (TypeError, ValueError, OverflowError) as error:
            raise SupervisedPhenotypingError("confusion matrix values are malformed") from error
        frozen = tuple(frozen_rows)
        if frozen and any(len(row) != len(frozen[0]) for row in frozen):
            raise SupervisedPhenotypingError("confusion matrix rows must have equal length")
        object.__setattr__(self, "rows", frozen)

    @property
    def shape(self) -> tuple[int, int]:
        """

        Returns the number of reference-class rows and predicted-class columns.

        Returns:
            Pair of dimensions, or (0, 0) for an empty matrix.
        """

        return (len(self.rows), len(self.rows[0]) if self.rows else 0)

    def __getitem__(self, index: tuple[int, int]) -> int:
        """

        Reads the count at one reference/prediction class pair.

        Args:
            index: Row and column positions in the saved class order.

        Returns:
            Number of cells in that confusion-matrix entry.
        """

        row, column = index
        return self.rows[row][column]

    def to_list(self) -> list[list[int]]:
        """

        Converts the retained confusion counts to lists for plotting and JSON saving.

        Returns:
            Copy of the matrix as a list of row lists.
        """

        return [list(row) for row in self.rows]


@dataclass(frozen=True, slots=True)
class PerClassMetrics:
    """

    Retains precision, recall, F1 and the reference-cell count for one class.

    Attributes:
        precision: Correct predictions of this class divided by all predictions
            assigned to this class, with zero when none were assigned.
        recall: Correct predictions of this class divided by all reference cells
            of this class, including cells whose prediction was rejected.
        f1: 2 * precision * recall / (precision + recall), with zero for a zero sum.
        support: Total reference cells in this class before confidence rejection.
    """

    precision: float
    recall: float
    f1: float
    support: int

    def __post_init__(self) -> None:
        """

        Checks the three unit-interval metrics and nonnegative reference-cell count.

        Raises:
            SupervisedPhenotypingError: A metric or support count is invalid.
        """

        object.__setattr__(self, "precision", _bounded_metric(self.precision, "precision"))
        object.__setattr__(self, "recall", _bounded_metric(self.recall, "recall"))
        object.__setattr__(self, "f1", _bounded_metric(self.f1, "f1"))
        if (
            isinstance(self.support, bool)
            or not isinstance(self.support, (int, np.integer))
            or self.support < 0
        ):
            raise SupervisedPhenotypingError("support must be a non-negative integer")
        object.__setattr__(self, "support", int(self.support))


@dataclass(frozen=True, slots=True)
class SupervisedQCFeature:
    """

    Identifies one measured input used when producing the evaluated cell predictions.

    Attributes:
        feature_id: Feature ID retained in the evaluation record.
        exact_measurement_name: QuPath measurement key used for that input column.
        semantic_role: Recorded description of the feature, such as marker
            intensity or morphology.
    """

    feature_id: str
    exact_measurement_name: str
    semantic_role: str

    def __post_init__(self) -> None:
        """

        Requires text for the feature ID, measurement key and feature description.

        Raises:
            SupervisedPhenotypingError: A required feature field is absent or blank.
        """

        object.__setattr__(self, "feature_id", _nonblank(self.feature_id, "feature_id"))
        object.__setattr__(
            self,
            "exact_measurement_name",
            _nonblank(self.exact_measurement_name, "exact_measurement_name"),
        )
        object.__setattr__(self, "semantic_role", _nonblank(self.semantic_role, "semantic_role"))

    def to_mapping(self) -> dict[str, str]:
        """

        Records the selected feature's ID, measurement key and description.

        Returns:
            Dictionary used in the saved evaluation's ordered feature list.
        """

        return {
            "feature_id": self.feature_id,
            "exact_measurement_name": self.exact_measurement_name,
            "semantic_role": self.semantic_role,
        }


@dataclass(frozen=True, slots=True)
class SupervisedQC:
    """

    Retains the complete held-out evaluation with the model and measurement choices that produced it.

    We keep the class-by-class counts and rejected-cell IDs together. A square
    matrix therefore describes the retained class assignments, while support
    and accuracy refer to all held-out cells. The supplied-label policy records
    that these class assignments are the evaluation inputs.

    Attributes:
        artifact_sha256: Complete file checksum of the evaluated classifier.
        classifier_id: Model ID within the analysis.
        validation_domain_id: Validation group associated with this model.
        class_ids: Class order shared by matrix axes and per-class results.
        feature_ids: Ordered measurement keys used to obtain predictions.
        feature_schema: Exact measurement names and their recorded descriptions.
        holdout_cell_ids: Complete held-out cell population in its saved order.
        confusion_matrix: Retained class assignments, with references in rows
            and predictions in columns.
        per_class: Metrics for every declared class in the same class order.
        accuracy: Correct predictions divided by all held-out cells.
        macro_precision: Arithmetic mean of per-class precision.
        macro_recall: Arithmetic mean of per-class recall.
        macro_f1: Arithmetic mean of per-class F1, including absent-class zeros.
        rejected_count: Number of predictions reported as Unclassified.
        rejected_cell_ids: IDs of the rejected cells, drawn from holdout_cell_ids.
        rejection_policy: Supplied-label policy with no inferred confidence threshold.
        zero_denominator_policy: Saved value "zero", identifying how undefined
            per-class ratios enter the reported metrics.
    """

    artifact_sha256: str
    classifier_id: str
    validation_domain_id: str
    class_ids: tuple[str, ...]
    feature_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedQCFeature, ...]
    holdout_cell_ids: tuple[str, ...]
    confusion_matrix: FrozenConfusionMatrix
    per_class: Mapping[str, PerClassMetrics]
    accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    rejected_count: int
    rejected_cell_ids: tuple[str, ...] = ()
    rejection_policy: Mapping[str, object] = MappingProxyType(
        {"version": "supplied-labels/v1", "comparator": "supplied_label", "threshold": None}
    )
    zero_denominator_policy: str = "zero"

    def __post_init__(self) -> None:
        """

        Checks that the model, feature list, class matrix and rejected cells describe one evaluation.

        We require a square confusion matrix in the declared class order and one
        result per class. The feature IDs must agree with the exact measurement
        names in the saved feature definitions. Rejected IDs must be unique members
        of the held-out population, and their count must equal rejected_count.

        The supplied-label policy records that a confidence threshold cannot be
        inferred from these labels. The numerical metric values are checked for
        valid intervals; evaluate_predictions() constructs them from the actual
        reference/prediction pairs before this record is created.

        Raises:
            SupervisedPhenotypingError: Model identity, input/class order, matrix
                shape, counts, metric values or confidence settings are inconsistent.
        """

        try:
            digest = self.artifact_sha256
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or digest.lower() != digest
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SupervisedPhenotypingError("artifact_sha256 must be a lowercase SHA-256")
            object.__setattr__(
                self, "classifier_id", _nonblank(self.classifier_id, "classifier_id")
            )
            object.__setattr__(
                self,
                "validation_domain_id",
                _nonblank(self.validation_domain_id, "validation_domain_id"),
            )
            class_ids = _ordered_strings(self.class_ids, "class_ids")
            feature_ids = _ordered_strings(self.feature_ids, "feature_ids")
            holdout_ids = _ordered_strings(self.holdout_cell_ids, "holdout_cell_ids")
            schema = tuple(self.feature_schema)
            if not schema or any(not isinstance(item, SupervisedQCFeature) for item in schema):
                raise SupervisedPhenotypingError(
                    "feature_schema must contain immutable SupervisedQCFeature values"
                )
            if tuple(item.feature_id for item in schema) != feature_ids:
                raise SupervisedPhenotypingError(
                    "feature_ids must exactly match the ordered feature schema"
                )
            if tuple(item.exact_measurement_name for item in schema) != feature_ids:
                raise SupervisedPhenotypingError(
                    "feature schema measurement names must exactly match feature IDs"
                )
            if not isinstance(self.confusion_matrix, FrozenConfusionMatrix):
                raise SupervisedPhenotypingError("confusion_matrix must be a FrozenConfusionMatrix")
            if self.confusion_matrix.shape != (len(class_ids), len(class_ids)):
                raise SupervisedPhenotypingError(
                    "confusion_matrix shape must match the declared class order"
                )
            if not isinstance(self.per_class, Mapping):
                raise SupervisedPhenotypingError(
                    "per_class must map class IDs to immutable PerClassMetrics"
                )
            per_class = dict(self.per_class)
            if tuple(per_class) != class_ids or any(
                not isinstance(value, PerClassMetrics) for value in per_class.values()
            ):
                raise SupervisedPhenotypingError(
                    "per_class must exactly follow the class order with immutable metrics"
                )
            rejected_ids = tuple(self.rejected_cell_ids)
            if any(not isinstance(item, str) or not item.strip() for item in rejected_ids):
                raise SupervisedPhenotypingError("rejected_cell_ids must contain non-empty strings")
            if len(set(rejected_ids)) != len(rejected_ids) or not set(rejected_ids).issubset(
                holdout_ids
            ):
                raise SupervisedPhenotypingError(
                    "rejected_cell_ids must be unique held-out stable IDs"
                )
            if isinstance(self.rejected_count, bool) or not isinstance(
                self.rejected_count, (int, np.integer)
            ):
                raise SupervisedPhenotypingError("rejected_count must be a non-negative integer")
            if self.rejected_count < 0 or int(self.rejected_count) != len(rejected_ids):
                raise SupervisedPhenotypingError(
                    "rejected_count must exactly match rejected_cell_ids"
                )
            policy = _freeze_json(self.rejection_policy, "rejection_policy")
            if not isinstance(policy, Mapping):
                raise SupervisedPhenotypingError("rejection_policy must be a mapping")
            if dict(policy) != {
                "version": "supplied-labels/v1",
                "comparator": "supplied_label",
                "threshold": None,
            }:
                raise SupervisedPhenotypingError(
                    "evaluation requires the supplied-label policy with no confidence threshold"
                )
            object.__setattr__(self, "class_ids", class_ids)
            object.__setattr__(self, "feature_ids", feature_ids)
            object.__setattr__(self, "feature_schema", schema)
            object.__setattr__(self, "holdout_cell_ids", holdout_ids)
            object.__setattr__(self, "per_class", MappingProxyType(per_class))
            object.__setattr__(self, "accuracy", _bounded_metric(self.accuracy, "accuracy"))
            object.__setattr__(
                self, "macro_precision", _bounded_metric(self.macro_precision, "macro_precision")
            )
            object.__setattr__(
                self, "macro_recall", _bounded_metric(self.macro_recall, "macro_recall")
            )
            object.__setattr__(self, "macro_f1", _bounded_metric(self.macro_f1, "macro_f1"))
            object.__setattr__(self, "rejected_count", int(self.rejected_count))
            object.__setattr__(self, "rejected_cell_ids", rejected_ids)
            object.__setattr__(self, "rejection_policy", policy)
            if self.zero_denominator_policy != "zero":
                raise SupervisedPhenotypingError("zero_denominator_policy must be zero")
        except SupervisedPhenotypingError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError) as error:
            raise SupervisedPhenotypingError("supervised QC values are malformed") from error

    def to_plot_data(self) -> dict[str, object]:
        """

        Collects the evaluation counts, metrics and matching cell/model IDs for plotting or saving.

        We include rejected IDs and the supplied-label policy alongside the matrix so
        a plot or exported summary can retain the complete held-out denominator.

        Returns:
            Current supervised-qc-review/v1 dictionary containing the model, ordered
            features/classes, cell IDs, confusion matrix, metrics and rejection details.
        """

        policy = _export_json(self.rejection_policy)
        assert isinstance(policy, dict)
        return {
            "schema_version": "supervised-qc-review/v1",
            "classifier_id": self.classifier_id,
            "artifact_sha256": self.artifact_sha256,
            "validation_domain_id": self.validation_domain_id,
            "class_ids": list(self.class_ids),
            "feature_ids": list(self.feature_ids),
            "feature_schema": [item.to_mapping() for item in self.feature_schema],
            "holdout_cell_ids": list(self.holdout_cell_ids),
            "matrix": self.confusion_matrix.to_list(),
            "per_class": {
                class_id: {
                    "precision": metric.precision,
                    "recall": metric.recall,
                    "f1": metric.f1,
                    "support": metric.support,
                }
                for class_id, metric in self.per_class.items()
            },
            "accuracy": self.accuracy,
            "macro_precision": self.macro_precision,
            "macro_recall": self.macro_recall,
            "macro_f1": self.macro_f1,
            "rejected_count": self.rejected_count,
            "rejected_cell_ids": list(self.rejected_cell_ids),
            "rejection_policy": policy,
            "zero_denominator_policy": self.zero_denominator_policy,
        }


def _stable_label_mapping(value: object, field: str) -> dict[str, str]:
    """

    Reads reference or predicted labels keyed by their original cell IDs.

    Args:
        value: Mapping from cell ID to class name.
        field: "truth" or "predicted", identifying the side being checked.

    Returns:
        Dictionary whose keys and class names all contain nonblank text.

    Raises:
        SupervisedPhenotypingError: A mapping, cell ID or class label is invalid.
    """

    if not isinstance(value, Mapping):
        raise SupervisedPhenotypingError(f"{field} must be a stable-ID mapping")
    output: dict[str, str] = {}
    try:
        items = tuple(value.items())
    except (TypeError, ValueError) as error:
        raise SupervisedPhenotypingError(f"{field} must be a valid stable-ID mapping") from error
    for cell_id, class_id in items:
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise SupervisedPhenotypingError(f"{field} stable cell IDs must be non-empty strings")
        if not isinstance(class_id, str) or not class_id.strip():
            raise SupervisedPhenotypingError(f"{field} class IDs must be non-empty strings")
        output[cell_id] = class_id
    return output


def _safe_divide(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """

    Calculates a class ratio while assigning zero when its denominator is zero.

    We allocate a zero-filled float64 array, then divide only positions with
    nonzero denominators using NumPy's where argument. Classes without predictions
    or reference cells therefore receive the declared zero metric directly.

    Args:
        numerator: Per-class correct counts or products used by the metric.
        denominator: Corresponding predicted totals, reference totals or
            precision-plus-recall values.

    Returns:
        Float64 ratio array with zeros at undefined positions.
    """

    result = np.zeros_like(numerator, dtype=np.float64)
    np.divide(numerator, denominator, out=result, where=denominator != 0)
    return result


def evaluate_predictions(
    truth: Mapping[str, str],
    predicted: Mapping[str, str],
    *,
    classifier_identity: SupervisedDeploymentClassifierIdentity,
    holdout_cell_ids: Sequence[str],
) -> SupervisedQC:
    """

    Calculates held-out classification statistics after joining labels by cell ID.

    We obtain the held-out IDs from the explicit application-model and cell
    list. Reference and predicted mappings must cover that exact cell set. We
    require declared classes for all reference labels; a predicted label may
    also be Unclassified after confidence rejection.

    For class k, the diagonal count C[k,k] is the number correctly predicted.
    The column total counts predictions assigned to that class. Reference
    support counts every held-out cell of that class, including rejected cells:

        precision[k] = C[k,k] / sum(C[:,k])
        recall[k] = C[k,k] / reference_support[k]
        F1[k] = 2 * precision[k] * recall[k] / (precision[k] + recall[k])
        accuracy = sum(diagonal(C)) / number_of_heldout_cells

    For example, if a class has ten reference cells, six correct predictions,
    two predictions assigned to another class and two rejected predictions,
    its recall is 6/10. A confidence rejection does not remove those cells
    from the reference denominator. Precision additionally depends on how many
    cells from other reference classes were assigned to this class.

    We use zero when a ratio denominator is zero. Each macro metric is the
    arithmetic mean across all declared classes. The returned aggregate counts
    weight cells equally; the training workflow separately calculates the
    subject-balanced score used to select hyperparameters.

    Args:
        truth: Reference class for every held-out cell ID.
        predicted: Cell-ID-to-class mapping, including Unclassified
            where a saved prediction was rejected.
        classifier_identity: Saved model identity, ordered measurements and classes.
        holdout_cell_ids: Complete evaluation population in its selected order.

    Returns:
        SupervisedQC containing the matrix, full support, per-class and macro
        scores, model/measurement IDs and rejected-cell details.

    Raises:
        SupervisedPhenotypingError: The model identity is invalid, required cell
            sets differ, or a reference/predicted class is undeclared.
    """

    if not isinstance(classifier_identity, SupervisedDeploymentClassifierIdentity):
        raise SupervisedPhenotypingError(
            "classifier_identity must be a SupervisedDeploymentClassifierIdentity"
        )
    stable_ids = _ordered_strings(holdout_cell_ids, "holdout_cell_ids")
    truth_by_id = _stable_label_mapping(truth, "truth")
    predicted_by_id = _stable_label_mapping(predicted, "predicted")
    if set(truth_by_id) != set(predicted_by_id):
        raise SupervisedPhenotypingError("truth and predicted must use the same stable cell IDs")
    if set(truth_by_id) != set(stable_ids):
        raise SupervisedPhenotypingError(
            "QC evidence must exactly cover the declared holdout stable IDs"
        )
    class_index = {class_id: index for index, class_id in enumerate(classifier_identity.class_ids)}
    unknown_truth = sorted({value for value in truth_by_id.values() if value not in class_index})
    unknown_predicted = sorted(
        {
            value
            for value in predicted_by_id.values()
            if value not in class_index and value != "Unclassified"
        }
    )
    if unknown_truth or unknown_predicted:
        raise SupervisedPhenotypingError("QC class labels must use exact declared class IDs")
    truth_indices = np.fromiter(
        (class_index[truth_by_id[cell_id]] for cell_id in stable_ids),
        dtype=np.int64,
        count=len(stable_ids),
    )
    predicted_values = tuple(predicted_by_id[cell_id] for cell_id in stable_ids)
    accepted_mask = np.fromiter(
        (value != "Unclassified" for value in predicted_values),
        dtype=np.bool_,
        count=len(stable_ids),
    )
    rejected_ids = tuple(
        cell_id
        for cell_id, value in zip(stable_ids, predicted_values, strict=True)
        if value == "Unclassified"
    )
    predicted_indices = np.fromiter(
        (class_index.get(value, 0) for value in predicted_values),
        dtype=np.int64,
        count=len(stable_ids),
    )
    size = len(classifier_identity.class_ids)
    # Count retained predictions by reference/predicted class. A rejected
    # prediction receives no predicted-class column, but retains its true class.
    matrix = np.zeros((size, size), dtype=np.int64)
    np.add.at(matrix, (truth_indices[accepted_mask], predicted_indices[accepted_mask]), 1)
    true_positive = np.diag(matrix).astype(np.float64)
    predicted_total = matrix.sum(axis=0, dtype=np.int64)
    # Reference support includes both classified and rejected cells,
    # preserving the denominator used to evaluate recall and accuracy.
    support = np.bincount(truth_indices, minlength=size)
    precision = _safe_divide(true_positive, predicted_total)
    recall = _safe_divide(true_positive, support)
    f1 = _safe_divide(2.0 * precision * recall, precision + recall)
    per_class = {
        class_id: PerClassMetrics(
            precision=float(precision[index]),
            recall=float(recall[index]),
            f1=float(f1[index]),
            support=int(support[index]),
        )
        for index, class_id in enumerate(classifier_identity.class_ids)
    }
    total = len(stable_ids)
    return SupervisedQC(
        artifact_sha256=classifier_identity.classifier_sha256,
        classifier_id=classifier_identity.classifier_id,
        validation_domain_id=classifier_identity.validation_domain_id,
        class_ids=classifier_identity.class_ids,
        feature_ids=tuple(
            definition.measurement_name for definition in classifier_identity.feature_schema
        ),
        feature_schema=tuple(
            SupervisedQCFeature(
                feature_id=definition.measurement_name,
                exact_measurement_name=definition.measurement_name,
                semantic_role=definition.semantic_role,
            )
            for definition in classifier_identity.feature_schema
        ),
        holdout_cell_ids=stable_ids,
        confusion_matrix=FrozenConfusionMatrix(tuple(map(tuple, matrix.tolist()))),
        per_class=per_class,
        accuracy=float(true_positive.sum() / total) if total else 0.0,
        macro_precision=float(precision.mean()) if size else 0.0,
        macro_recall=float(recall.mean()) if size else 0.0,
        macro_f1=float(f1.mean()) if size else 0.0,
        rejected_count=int(np.count_nonzero(~accepted_mask)),
        rejected_cell_ids=rejected_ids,
    )


__all__ = [
    "FrozenConfusionMatrix",
    "PerClassMetrics",
    "SupervisedQC",
    "SupervisedQCFeature",
    "evaluate_predictions",
]
