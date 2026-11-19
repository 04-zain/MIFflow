"""

Calculates compartment-classifier results for the complete evaluation, each image and each subject.

We receive one reference/predicted class pair for each expected observation.
Its image-entry, subject and object IDs identify where that observation came
from. Before calculating counts, we compare those complete IDs with the
expected evaluation set so omitted or repeated predictions cannot change
its denominator.

Confusion matrices place reference classes in rows and predicted classes in
columns. We calculate accuracy, precision, recall and F1 from the complete
matrix and from matrices for each image and subject. Per-subject matrices
are retained for the training workflow's subject-balanced comparison; the
pooled matrix here counts every supplied observation once.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from .classifiers import ClassifierCompatibilityError

_METRIC_NAMES = ("accuracy", "macro_precision", "macro_recall", "macro_f1")


# ==============================================================================
#                  Counts for pooled, image and subject review
# ==============================================================================


def _nonblank(value: object, field: str) -> str:
    """

    Checks that an image, subject, object or class ID contains text.

    Args:
        value: Required ID value.
        field: Field description included in an error.

    Returns:
        Checked text used for subsequent ID comparisons.

    Raises:
        ClassifierCompatibilityError: The value is not text or is blank.
    """

    if not isinstance(value, str) or not value.strip():
        raise ClassifierCompatibilityError(f"{field} must be a non-empty string")
    return value


def _class_ids(value: object) -> tuple[str, ...]:
    """

    Checks the class order shared by every evaluation matrix.

    We require distinct nonempty class names and retain their order. A particular
    image or subject can have no reference observations for a declared class;
    its matrix still includes that class at the same position.

    Args:
        value: Nonempty sequence of class names.

    Returns:
        Tuple defining the reference-row and predicted-column order.

    Raises:
        ClassifierCompatibilityError: The list is empty or includes a blank,
            invalid or repeated class.
    """

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ClassifierCompatibilityError("class_ids must be a non-empty sequence")
    result = tuple(_nonblank(item, "class_ids") for item in value)
    if not result or len(set(result)) != len(result):
        raise ClassifierCompatibilityError("class_ids must be unique non-empty strings")
    return result


def classifier_metrics_from_confusion(
    confusion_matrix: Sequence[Sequence[int]],
) -> Mapping[str, float]:
    """

    Calculates accuracy and macro precision, recall and F1 from reference-by-prediction counts.

    For class k, C[k,k] counts correct predictions, the sum of row k counts its
    reference observations and the sum of column k counts predictions assigned
    to it. We calculate:

        precision[k] = C[k,k] / sum(C[:,k])
        recall[k] = C[k,k] / sum(C[k,:])
        F1[k] = 2 * precision[k] * recall[k] / (precision[k] + recall[k])
        accuracy = sum(diagonal(C)) / sum(C)

    Each macro score averages the corresponding per-class values across all
    matrix classes. A class with no references or no assigned predictions receives
    zero for the undefined ratio. For a three-class model evaluated on an image
    containing only one class, those absent classes still participate in its
    macro scores; accuracy uses all observations represented in the matrix.

    Args:
        confusion_matrix: Nonempty square count matrix with reference classes
            in rows, predictions in columns and at least one observation overall.

    Returns:
        Mapping containing accuracy, macro_precision, macro_recall and macro_f1.

    Raises:
        ClassifierCompatibilityError: The matrix is nonsquare, a count is
            negative/non-integer, or the complete matrix has zero observations.
    """

    if isinstance(confusion_matrix, (str, bytes, bytearray)) or not isinstance(
        confusion_matrix, Sequence
    ):
        raise ClassifierCompatibilityError("confusion matrix must be a non-empty square matrix")
    rows = tuple(confusion_matrix)
    if not rows or any(
        isinstance(row, (str, bytes, bytearray))
        or not isinstance(row, Sequence)
        or len(row) != len(rows)
        for row in rows
    ):
        raise ClassifierCompatibilityError("confusion matrix must be a non-empty square matrix")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for row in rows
        for count in row
    ):
        raise ClassifierCompatibilityError("confusion matrix values must be non-negative integers")
    total = sum(sum(row) for row in rows)
    if total == 0:
        raise ClassifierCompatibilityError("confusion matrix must contain observations")
    # Diagonal counts are correct predictions. Row sums describe reference
    # support; column sums describe how often each class was predicted.
    true_positive = tuple(rows[position][position] for position in range(len(rows)))
    predicted_total = tuple(
        sum(rows[row][column] for row in range(len(rows))) for column in range(len(rows))
    )
    support = tuple(sum(row) for row in rows)
    precision = tuple(
        tp / denominator if denominator else 0.0
        for tp, denominator in zip(true_positive, predicted_total, strict=True)
    )
    recall = tuple(
        tp / denominator if denominator else 0.0
        for tp, denominator in zip(true_positive, support, strict=True)
    )
    f1 = tuple(
        2.0 * p * r / (p + r) if p + r else 0.0 for p, r in zip(precision, recall, strict=True)
    )
    return MappingProxyType(
        {
            "accuracy": sum(true_positive) / total,
            "macro_precision": sum(precision) / len(rows),
            "macro_recall": sum(recall) / len(rows),
            "macro_f1": sum(f1) / len(rows),
        }
    )


@dataclass(frozen=True, slots=True, order=True)
class ClassifierObservationIdentity:
    """

    Identifies the image, subject and object contributing one reference/prediction pair.

    The object ID is interpreted together with its image-entry ID. Two images
    can therefore use the same object-ID text without being mistaken for the
    same evaluated annotation.

    Attributes:
        project_entry_id: QuPath image-entry ID containing the evaluated object.
        subject_id: Subject from which that image was obtained.
        object_id: Evaluated annotation or object ID within that image.
    """

    project_entry_id: str
    subject_id: str
    object_id: str

    def __post_init__(self) -> None:
        """

        Checks that the image-entry, subject and object IDs are present.

        Raises:
            ClassifierCompatibilityError: An ID is missing or blank.
        """

        object.__setattr__(
            self, "project_entry_id", _nonblank(self.project_entry_id, "project_entry_id")
        )
        object.__setattr__(self, "subject_id", _nonblank(self.subject_id, "subject_id"))
        object.__setattr__(self, "object_id", _nonblank(self.object_id, "object_id"))


@dataclass(frozen=True, slots=True)
class ClassifierPredictionObservation:
    """

    Associates the reference and predicted class with one evaluated object.

    Attributes:
        identity: Image-entry, subject and object IDs used to join the result
            to its expected reference observation.
        truth_class_id: Class recorded by the reference annotation.
        predicted_class_id: Class returned by the fitted classifier evaluation.
    """

    identity: ClassifierObservationIdentity
    truth_class_id: str
    predicted_class_id: str

    def __post_init__(self) -> None:
        """

        Checks that the observation has its image/object ID and both class labels.

        The complete class list is supplied when observations are combined, so class
        membership is checked by reduce_classifier_confusion() before counting.

        Raises:
            ClassifierCompatibilityError: The ID record has the wrong type or either
                class label is missing.
        """

        if not isinstance(self.identity, ClassifierObservationIdentity):
            raise ClassifierCompatibilityError("identity must be a ClassifierObservationIdentity")
        object.__setattr__(self, "truth_class_id", _nonblank(self.truth_class_id, "truth_class_id"))
        object.__setattr__(
            self, "predicted_class_id", _nonblank(self.predicted_class_id, "predicted_class_id")
        )


@dataclass(frozen=True, slots=True)
class ClassifierConfusionSummary:
    """

    Retains classification counts and scores for one selected set of observations.

    The sorted object descriptions identify which reference/prediction pairs
    contributed. Their count must equal the sum of the confusion matrix, allowing
    the later grouped summaries to be compared with this same population.

    Attributes:
        observation_identities: Unique evaluated objects, sorted by image-entry,
            subject and object ID.
        confusion_matrix: Integer counts with reference rows and predicted columns.
        metrics: Accuracy and the three macro metrics calculated from those counts.
        zero_denominator_policy: Saved value "zero", used for per-class ratios
            without a reference or predicted denominator.
    """

    observation_identities: tuple[ClassifierObservationIdentity, ...]
    confusion_matrix: tuple[tuple[int, ...], ...]
    metrics: Mapping[str, float]
    zero_denominator_policy: str = "zero"

    def __post_init__(self) -> None:
        """

        Checks that the matrix counts cover the recorded observations and all metric values are valid.

        We require sorted unique observations, a square nonnegative integer matrix
        and equality between the observation count and total matrix count. The
        metric map must provide the four supported names with finite values in
        [0, 1]. The _summary() function calculates those values before constructing
        this record.

        Raises:
            ClassifierCompatibilityError: IDs, matrix shape/counts, metric fields
                or the zero-denominator setting are inconsistent.
        """

        identities = tuple(self.observation_identities)
        if not identities or any(
            not isinstance(item, ClassifierObservationIdentity) for item in identities
        ):
            raise ClassifierCompatibilityError(
                "summary must retain non-empty exact observation identities"
            )
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ClassifierCompatibilityError(
                "summary observation identities must be unique and canonical"
            )
        rows = tuple(tuple(row) for row in self.confusion_matrix)
        if not rows or any(len(row) != len(rows) for row in rows):
            raise ClassifierCompatibilityError("confusion matrix must be a non-empty square matrix")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for row in rows
            for item in row
        ):
            raise ClassifierCompatibilityError(
                "confusion matrix values must be non-negative integers"
            )
        if sum(sum(row) for row in rows) != len(identities):
            raise ClassifierCompatibilityError("confusion matrix must exactly cover observations")
        if not isinstance(self.metrics, Mapping) or set(self.metrics) != set(_METRIC_NAMES):
            raise ClassifierCompatibilityError(
                "metrics must contain the canonical classifier metric names"
            )
        metrics: dict[str, float] = {}
        for name in _METRIC_NAMES:
            value = self.metrics[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ClassifierCompatibilityError("metrics must contain bounded numeric values")
            metrics[name] = float(value)
        if self.zero_denominator_policy != "zero":
            raise ClassifierCompatibilityError("zero_denominator_policy must be zero")
        object.__setattr__(self, "observation_identities", identities)
        object.__setattr__(self, "confusion_matrix", rows)
        object.__setattr__(self, "metrics", MappingProxyType(metrics))


@dataclass(frozen=True, slots=True)
class ClassifierConfusionReduction:
    """

    Keeps pooled, image-specific and subject-specific results in one shared class order.

    Every evaluated observation appears in the pooled matrix, exactly one image
    summary and exactly one subject summary. A subject with several image entries
    receives one combined matrix containing all of its evaluated objects.

    Attributes:
        class_ids: Row/column class order used by every matrix.
        pooled: Summary of the complete expected evaluation population.
        per_project_entry: Image-entry IDs mapped to their observation summaries.
        per_subject: Subject IDs mapped to their combined image observations.
    """

    class_ids: tuple[str, ...]
    pooled: ClassifierConfusionSummary
    per_project_entry: Mapping[str, ClassifierConfusionSummary]
    per_subject: Mapping[str, ClassifierConfusionSummary]

    def __post_init__(self) -> None:
        """

        Checks that both grouped result sets cover exactly the pooled observation list.

        We flatten the image-specific observation lists, sort them and compare them
        with the pooled list; we repeat that comparison for subjects. Each object's
        own image and subject IDs must agree with the group containing it. This
        identifies missing, duplicated or incorrectly grouped observations before
        a review displays their counts.

        Raises:
            ClassifierCompatibilityError: A group has the wrong type or matrix size,
                omits/repeats observations, or contains an observation from another group.
        """

        class_ids = _class_ids(self.class_ids)
        if not isinstance(self.pooled, ClassifierConfusionSummary):
            raise ClassifierCompatibilityError("pooled must be a ClassifierConfusionSummary")
        entries = _freeze_scope_summaries(self.per_project_entry, "per_project_entry")
        subjects = _freeze_scope_summaries(self.per_subject, "per_subject")
        expected_width = len(class_ids)
        summaries = (self.pooled, *entries.values(), *subjects.values())
        if any(
            summary.confusion_matrix and len(summary.confusion_matrix) != expected_width
            for summary in summaries
        ):
            raise ClassifierCompatibilityError("confusion matrices must match class_ids")
        pooled_identities = self.pooled.observation_identities
        if (
            tuple(
                sorted(
                    identity
                    for summary in entries.values()
                    for identity in summary.observation_identities
                )
            )
            != pooled_identities
        ):
            raise ClassifierCompatibilityError(
                "per-project-entry summaries must exactly partition pooled observations"
            )
        if (
            tuple(
                sorted(
                    identity
                    for summary in subjects.values()
                    for identity in summary.observation_identities
                )
            )
            != pooled_identities
        ):
            raise ClassifierCompatibilityError(
                "per-subject summaries must exactly partition pooled observations"
            )
        if any(
            any(identity.project_entry_id != scope for identity in summary.observation_identities)
            for scope, summary in entries.items()
        ):
            raise ClassifierCompatibilityError(
                "per-project-entry summary scope must match exact identities"
            )
        if any(
            any(identity.subject_id != scope for identity in summary.observation_identities)
            for scope, summary in subjects.items()
        ):
            raise ClassifierCompatibilityError(
                "per-subject summary scope must match exact identities"
            )
        object.__setattr__(self, "class_ids", class_ids)
        object.__setattr__(self, "per_project_entry", entries)
        object.__setattr__(self, "per_subject", subjects)


def _freeze_scope_summaries(value: object, field: str) -> Mapping[str, ClassifierConfusionSummary]:
    """

    Checks and orders summaries indexed by image or subject ID.

    Args:
        value: Nonempty mapping of group IDs to ClassifierConfusionSummary values.
        field: Name identifying the image or subject grouping in an error.

    Returns:
        Protected mapping sorted by group ID for repeatable review ordering.

    Raises:
        ClassifierCompatibilityError: The grouping is empty, an ID is blank,
            or a value is not a classification summary.
    """

    if not isinstance(value, Mapping) or not value:
        raise ClassifierCompatibilityError(f"{field} must be a non-empty scope mapping")
    result: dict[str, ClassifierConfusionSummary] = {}
    for scope, summary in value.items():
        scope_id = _nonblank(scope, field)
        if not isinstance(summary, ClassifierConfusionSummary):
            raise ClassifierCompatibilityError(
                f"{field} must contain ClassifierConfusionSummary values"
            )
        result[scope_id] = summary
    return MappingProxyType(dict(sorted(result.items())))


def _summary(
    class_ids: tuple[str, ...], observations: Sequence[ClassifierPredictionObservation]
) -> ClassifierConfusionSummary:
    """

    Counts one selected observation group and calculates its classification scores.

    We sort observations by their complete IDs, map reference and predicted class
    names to the shared matrix positions, and increment the appropriate entry
    once per observation. The resulting matrix is passed to the common metric
    calculation, so pooled, image and subject summaries use identical formulas.

    Args:
        class_ids: Checked class names in the required matrix-axis order.
        observations: Nonempty reference/prediction pairs already checked for
            unique IDs and membership in class_ids.

    Returns:
        ClassifierConfusionSummary with the contributing objects, count matrix
        and its calculated metrics.
    """

    ordered = tuple(sorted(observations, key=lambda item: item.identity))
    index = {class_id: position for position, class_id in enumerate(class_ids)}
    matrix = [[0 for _ in class_ids] for _ in class_ids]
    for observation in ordered:
        matrix[index[observation.truth_class_id]][index[observation.predicted_class_id]] += 1
    return ClassifierConfusionSummary(
        observation_identities=tuple(item.identity for item in ordered),
        confusion_matrix=tuple(tuple(row) for row in matrix),
        metrics=classifier_metrics_from_confusion(matrix),
    )


def reduce_classifier_confusion(
    *,
    class_ids: Sequence[str],
    expected_identities: Sequence[ClassifierObservationIdentity],
    observations: Sequence[ClassifierPredictionObservation],
) -> ClassifierConfusionReduction:
    """

    Builds pooled, image and subject summaries from one complete reference/prediction set.

    We compare the returned observation IDs with the expected reference IDs
    before counting. IDs include image-entry, subject and object information.
    Missing, additional or repeated observations are rejected, as are class
    labels outside the model's declared class order.

    We then calculate the pooled matrix and group those same observations by
    image and subject. Each subject's images contribute to one subject matrix.
    The training workflow can use these subject matrices to give subjects equal
    weight when comparing parameter settings; the pooled result retains its
    original observation counts.

    Args:
        class_ids: Ordered classes shared by all result matrices.
        expected_identities: Complete nonempty list of reference observations
            selected for this evaluation.
        observations: One returned reference/predicted class pair per expected ID.

    Returns:
        ClassifierConfusionReduction with the checked population and all three
        reporting levels.

    Raises:
        ClassifierCompatibilityError: Observation IDs or class labels fail their
            type, uniqueness, coverage or membership checks.
    """

    classes = _class_ids(class_ids)
    expected = tuple(expected_identities)
    if not expected or any(
        not isinstance(item, ClassifierObservationIdentity) for item in expected
    ):
        raise ClassifierCompatibilityError(
            "expected_identities must contain exact observation identities"
        )
    if len(set(expected)) != len(expected):
        raise ClassifierCompatibilityError("expected_identities must be unique")
    supplied = tuple(observations)
    if any(not isinstance(item, ClassifierPredictionObservation) for item in supplied):
        raise ClassifierCompatibilityError(
            "observations must contain complete truth-prediction observations"
        )
    identities = tuple(item.identity for item in supplied)
    if len(set(identities)) != len(identities):
        raise ClassifierCompatibilityError("observations must not duplicate exact identities")
    # Keep the expected evaluation population fixed before calculating
    # any count, proportion or class-average score.
    if set(identities) != set(expected):
        raise ClassifierCompatibilityError("observations must exactly cover expected identities")
    unknown = {
        class_id
        for observation in supplied
        for class_id in (observation.truth_class_id, observation.predicted_class_id)
        if class_id not in classes
    }
    if unknown:
        raise ClassifierCompatibilityError(
            "truth and predicted class IDs must use declared classes"
        )
    # Use the same checked reference/prediction pairs at all three
    # reporting levels, grouping images together when they share a subject.
    pooled = _summary(classes, supplied)
    by_entry: dict[str, list[ClassifierPredictionObservation]] = {}
    by_subject: dict[str, list[ClassifierPredictionObservation]] = {}
    for observation in supplied:
        by_entry.setdefault(observation.identity.project_entry_id, []).append(observation)
        by_subject.setdefault(observation.identity.subject_id, []).append(observation)
    return ClassifierConfusionReduction(
        class_ids=classes,
        pooled=pooled,
        per_project_entry={scope: _summary(classes, values) for scope, values in by_entry.items()},
        per_subject={scope: _summary(classes, values) for scope, values in by_subject.items()},
    )


__all__ = [
    "ClassifierConfusionReduction",
    "ClassifierConfusionSummary",
    "ClassifierObservationIdentity",
    "ClassifierPredictionObservation",
    "classifier_metrics_from_confusion",
    "reduce_classifier_confusion",
]
