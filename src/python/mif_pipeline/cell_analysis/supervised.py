"""

Associates supervised reference labels and predicted phenotypes with the measured cells.

We fit the cell classifier in QuPath using selected measurement columns. This
module checks which cells supplied its reference labels and which cells receive
its predictions. QuPath binds a coordinate-only reference to a nearby eligible cell once and
saves that association in its annotation metadata. References and predictions
are subsequently joined to the current cells by stable cell ID.

The saved model description records the ordered measurements and output
classes, the model-file checksum and the reference-cell lists. Before applying
reference-associated predictions, we compare that description with the current
QuPath export and verify the recorded point-to-cell assignments. For a new
image, we use the saved model description with the new image's eligible cells.

We retain a predicted phenotype when its confidence is greater than or equal
to the selected confidence threshold. Otherwise its displayed phenotype becomes
Unclassified, while the confidence and rejection flag remain available for
evaluation. The class returned by the native model, the threshold decision
and the reference class therefore have separate roles in the analysis.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from math import hypot, isfinite
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from mif_pipeline.pipeline_core.models import CellRecord, ROIAnalysisError
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

SUPERVISED_TRAINING_STAGE = "supervised_phenotyping"
SUPERVISED_TRAINING_SCRIPT_ASSET = QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES


# ==============================================================================
#                        Model and reference-cell descriptions
# ==============================================================================


class SupervisedPhenotypingError(ROIAnalysisError):
    """

    Reports an inconsistent reference assignment, classifier description or returned cell prediction.
    """

def _nonblank(value: object, field: str) -> str:
    """

    Checks that a required ID, measurement name or class label contains text.

    Args:
        value: Text supplied for the field.
        field: Field name included in the error.

    Returns:
        Checked text used by the subsequent exact-name comparison.

    Raises:
        SupervisedPhenotypingError: The value is not a string or contains only whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise SupervisedPhenotypingError(f"{field} must be a non-empty string")
    return value


def _ordered_strings(value: object, field: str, *, sort: bool = False) -> tuple[str, ...]:
    """

    Checks a sequence of distinct IDs and optionally sorts them for a saved comparison.

    We retain class and feature order because each position determines a model
    output or input column. Cell-ID sets are sorted by callers when their order
    is needed only for repeatable saving and comparison.

    Args:
        value: Nonempty sequence of text values.
        field: Sequence name used to identify an invalid entry.
        sort: Whether to sort the checked IDs alphabetically.

    Returns:
        Tuple of unique IDs in the selected order.

    Raises:
        SupervisedPhenotypingError: The sequence is empty, contains invalid text
            or repeats an ID.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SupervisedPhenotypingError(f"{field} must be a sequence of strings")
    values = tuple(_nonblank(item, field) for item in value)
    try:
        unique_count = len(set(values))
    except TypeError as error:
        raise SupervisedPhenotypingError(f"{field} must contain valid strings") from error
    if not values or unique_count != len(values):
        raise SupervisedPhenotypingError(f"{field} must contain unique non-empty strings")
    return tuple(sorted(values)) if sort else values


def _exact_mapping(value: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    """

    Checks the complete field list of a saved model, point or prediction row.

    Args:
        value: Dictionary-like record to inspect.
        fields: Expected field names, including fields whose values may be None.
        name: Description identifying the record in an error.

    Returns:
        Checked mapping for the operation that decodes its individual values.

    Raises:
        SupervisedPhenotypingError: Keys are not text, required fields are missing
            or additional fields are present.
    """

    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SupervisedPhenotypingError(f"{name} must be a string-keyed mapping")
    unknown = sorted(set(value).difference(fields))
    if unknown:
        raise SupervisedPhenotypingError(f"{name} has unknown field(s): {", ".join(unknown)}")
    missing = sorted(fields.difference(value))
    if missing:
        raise SupervisedPhenotypingError(f"{name} is missing field(s): {", ".join(missing)}")
    return value


def _finite_number(value: object, field: str) -> float:
    """

    Reads a numerical coordinate, distance or confidence and requires a finite value.

    Args:
        value: Integer or floating-point value; boolean flags are excluded.
        field: Quantity name used in an error.

    Returns:
        Float to which the caller applies its required sign or numerical interval.

    Raises:
        SupervisedPhenotypingError: The value is not numerical or is NaN/infinite.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupervisedPhenotypingError(f"{field} must be numeric")
    try:
        number = float(value)
    except OverflowError as error:
        raise SupervisedPhenotypingError(f"{field} must be finite") from error
    if not isfinite(number):
        raise SupervisedPhenotypingError(f"{field} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class SupervisedFeatureDefinition:
    """

    Identifies one measurement column used by the cell classifier.

    We retain the exact QuPath measurement key and its declared role. For example,
    a mean nuclear marker intensity and a cell-area measurement occupy different
    positions and have different meanings even when their numerical values coincide.

    Attributes:
        measurement_name: Exact key read from each cell's QuPath measurements.
        semantic_role: Recorded feature description, such as marker intensity
            or morphology, associated with that column.
    """

    measurement_name: str
    semantic_role: str

    def __post_init__(self) -> None:
        """

        Requires the measurement key and its declared role to contain text.

        Raises:
            SupervisedPhenotypingError: Either field is missing or blank.
        """

        object.__setattr__(
            self, "measurement_name", _nonblank(self.measurement_name, "measurement_name")
        )
        object.__setattr__(self, "semantic_role", _nonblank(self.semantic_role, "semantic_role"))

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedFeatureDefinition":
        """

        Reads a saved measurement key and its role into a feature definition.

        Args:
            value: Mapping containing measurement_name and semantic_role.

        Returns:
            SupervisedFeatureDefinition ready to occupy its recorded input-column position.

        Raises:
            SupervisedPhenotypingError: Fields are missing, extra or blank.
        """

        item = _exact_mapping(
            value, frozenset({"measurement_name", "semantic_role"}), "feature schema"
        )
        return cls(item["measurement_name"], item["semantic_role"])

    def to_mapping(self) -> dict[str, str]:
        """

        Records the measurement key and its role for the native classifier request.

        Returns:
            Dictionary containing measurement_name and semantic_role.
        """

        return {"measurement_name": self.measurement_name, "semantic_role": self.semantic_role}


@dataclass(frozen=True, slots=True)
class SupervisedClassifierArtifact:
    """

    Associates the saved cell model with its input measurements and original reference cells.

    We record class and feature lists in model order. The training and held-out
    cell lists are stored separately and sorted by ID, allowing later reference
    exports to be compared with the cells used for this model's original review.
    Both sets are required by this reference-associated record.

    Attributes:
        classifier_id: Model name used within the analysis.
        classifier_sha256: SHA-256 of the complete saved classifier file.
        qupath_version: QuPath version recorded with the model.
        class_ids: Unique class names in the native model's output order.
        feature_schema: Ordered measurement keys and their declared roles.
        training_cell_ids: Sorted, unique cells supplying training labels.
        holdout_cell_ids: Sorted, unique reference cells reserved from that fit.
        validation_domain_id: Analysis validation group in which this model
            and the selected image measurements are permitted to be used together.
    """

    classifier_id: str
    classifier_sha256: str
    qupath_version: str
    class_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedFeatureDefinition, ...]
    training_cell_ids: tuple[str, ...]
    holdout_cell_ids: tuple[str, ...]
    validation_domain_id: str

    def __post_init__(self) -> None:
        """

        Checks the saved model's feature order and separation of its two reference-cell sets.

        We require unique measurement keys and output classes, then sort each cell-ID
        list and reject any cell appearing in both training and held-out references.
        The model-file checksum must be complete lowercase SHA-256 text.

        Raises:
            SupervisedPhenotypingError: A model field, feature list or reference-cell
                set is incomplete, duplicated or inconsistent.
        """

        for field in ("classifier_id", "qupath_version", "validation_domain_id"):
            object.__setattr__(self, field, _nonblank(getattr(self, field), field))
        digest = self.classifier_sha256
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest.lower() != digest
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SupervisedPhenotypingError(
                "classifier_sha256 must be a lowercase complete-file SHA-256"
            )
        object.__setattr__(self, "class_ids", _ordered_strings(self.class_ids, "class_ids"))
        schema = tuple(self.feature_schema)
        if not schema or any(not isinstance(item, SupervisedFeatureDefinition) for item in schema):
            raise SupervisedPhenotypingError("feature_schema must contain feature definitions")
        names = tuple(item.measurement_name for item in schema)
        if len(set(names)) != len(names):
            raise SupervisedPhenotypingError(
                "feature_schema measurement_name values must be unique"
            )
        object.__setattr__(self, "feature_schema", schema)
        training = _ordered_strings(self.training_cell_ids, "training_cell_ids", sort=True)
        holdout = _ordered_strings(self.holdout_cell_ids, "holdout_cell_ids", sort=True)
        if set(training).intersection(holdout):
            raise SupervisedPhenotypingError("training and holdout cell IDs must be disjoint")
        object.__setattr__(self, "training_cell_ids", training)
        object.__setattr__(self, "holdout_cell_ids", holdout)

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedClassifierArtifact":
        """

        Reconstructs the saved classifier description and its ordered measurement definitions.

        Args:
            value: Mapping with model ID/checksum, QuPath version, classes, features,
                training and held-out cell IDs, and the validation-group ID.

        Returns:
            SupervisedClassifierArtifact checked through the same rules used at construction.

        Raises:
            SupervisedPhenotypingError: A required field or model/reference relationship fails.
        """

        fields = frozenset(
            {
                "classifier_id",
                "classifier_sha256",
                "qupath_version",
                "class_ids",
                "feature_schema",
                "training_cell_ids",
                "holdout_cell_ids",
                "validation_domain_id",
            }
        )
        item = _exact_mapping(value, fields, "supervised classifier artifact")
        return cls(
            classifier_id=item["classifier_id"],
            classifier_sha256=item["classifier_sha256"],
            qupath_version=item["qupath_version"],
            class_ids=item["class_ids"],
            feature_schema=tuple(
                SupervisedFeatureDefinition.from_mapping(raw) for raw in item["feature_schema"]
            ),
            training_cell_ids=item["training_cell_ids"],
            holdout_cell_ids=item["holdout_cell_ids"],
            validation_domain_id=item["validation_domain_id"],
        )

    def validate_classifier_bytes(self, content: bytes | bytearray | memoryview) -> None:
        """

        Calculates the saved model file's checksum and compares it with this classifier description.

        Args:
            content: Complete classifier-file bytes, bytearray or memoryview.

        Raises:
            SupervisedPhenotypingError: The input is not bytes-like or its SHA-256
                differs from the model used for the recorded review.
        """

        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise SupervisedPhenotypingError("classifier bytes must be bytes-like")
        actual = hashlib.sha256(bytes(content)).hexdigest()
        if actual != self.classifier_sha256:
            raise SupervisedPhenotypingError(
                "classifier complete-file digest does not match the accepted artifact"
            )

    def to_mapping(self) -> dict[str, object]:
        """

        Collects the model, feature and reference-cell fields for saving or native execution.

        Returns:
            Dictionary with ordered class/feature lists and the sorted training
            and held-out cell lists.
        """

        return {
            "classifier_id": self.classifier_id,
            "classifier_sha256": self.classifier_sha256,
            "qupath_version": self.qupath_version,
            "class_ids": list(self.class_ids),
            "feature_schema": [item.to_mapping() for item in self.feature_schema],
            "training_cell_ids": list(self.training_cell_ids),
            "holdout_cell_ids": list(self.holdout_cell_ids),
            "validation_domain_id": self.validation_domain_id,
        }

    @property
    def feature_ids(self) -> tuple[str, ...]:
        """

        Retrieves the measurement keys in the exact order supplied to the model.

        Returns:
            Tuple of measurement_name values from feature_schema.
        """

        return tuple(item.measurement_name for item in self.feature_schema)


@dataclass(frozen=True, slots=True)
class SupervisedDeploymentClassifierIdentity:
    """

    Records the saved model information needed to apply it to another eligible cell population.

    We retain the model checksum and ordered inputs/classes when moving to a new
    image. The new image supplies its own expected cells, which are checked against
    the returned predictions by reconcile_deployment_predictions().

    Attributes:
        classifier_id: Model ID recorded with the accepted classifier.
        classifier_sha256: Checksum of its saved model file.
        qupath_version: QuPath version associated with the model.
        class_ids: Output classes in their recorded order.
        feature_schema: Ordered measurement keys and their roles.
        validation_domain_id: Validation group required for the current application.
    """

    classifier_id: str
    classifier_sha256: str
    qupath_version: str
    class_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedFeatureDefinition, ...]
    validation_domain_id: str

    def __post_init__(self) -> None:
        """

        Checks the saved model identity and complete input-measurement list before application.

        We require a valid model checksum, distinct class names and distinct measurement
        keys. Feature definitions remain in their recorded order so each new cell's
        measurement vector is interpreted using the fitted model's input positions.

        Raises:
            SupervisedPhenotypingError: A model identifier, class or feature definition is invalid.
        """

        try:
            for field in ("classifier_id", "qupath_version", "validation_domain_id"):
                object.__setattr__(self, field, _nonblank(getattr(self, field), field))
            digest = self.classifier_sha256
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or digest.lower() != digest
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SupervisedPhenotypingError(
                    "classifier_sha256 must be a lowercase complete-file SHA-256"
                )
            object.__setattr__(self, "class_ids", _ordered_strings(self.class_ids, "class_ids"))
            schema = tuple(self.feature_schema)
            if not schema or any(
                not isinstance(item, SupervisedFeatureDefinition) for item in schema
            ):
                raise SupervisedPhenotypingError("feature_schema must contain feature definitions")
            names = tuple(item.measurement_name for item in schema)
            if len(set(names)) != len(names):
                raise SupervisedPhenotypingError(
                    "feature_schema measurement_name values must be unique"
                )
            object.__setattr__(self, "feature_schema", schema)
        except SupervisedPhenotypingError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError) as error:
            raise SupervisedPhenotypingError(
                "supervised deployment classifier identity is malformed"
            ) from error


@dataclass(frozen=True, slots=True)
class SupervisedDeploymentCellIdentity:
    """

    Identifies a cell whose phenotype will be updated from a native prediction.

    Attributes:
        cell_id: Saved cell ID used to join the prediction to the original object.
        slide_id: Image containing that cell.
        section_id: Accepted tissue section containing that cell.
    """

    cell_id: str
    slide_id: str
    section_id: str

    def __post_init__(self) -> None:
        """

        Requires the cell, image and tissue-section IDs before matching prediction rows.

        Raises:
            SupervisedPhenotypingError: An ID is missing or blank.
        """

        for field in ("cell_id", "slide_id", "section_id"):
            object.__setattr__(self, field, _nonblank(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class SupervisedReferenceCell:
    """Our saved reference identity and current centroid projected from Arrow."""

    cell_id: str
    slide_id: str
    section_id: str
    analysis_domain_id: str
    x_level0: float
    y_level0: float

    def __post_init__(self):
        """Retain our exact reference scope and finite level-zero position."""
        for name in ('cell_id', 'slide_id', 'section_id', 'analysis_domain_id'):
            object.__setattr__(self, name, _nonblank(getattr(self, name), name))
        for name in ('x_level0', 'y_level0'):
            object.__setattr__(self, name, _finite_number(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class MappedPointEvidence:
    """

    Records the cell selected for one labelled reference point.

    The point and cell-centroid coordinates are retained together with their
    calculated distance. We can therefore inspect which measured object received
    the point's reference class and compare that assignment on reopening.

    Attributes:
        point_id: Original reference-point ID.
        cell_id: ID of the selected nearest eligible cell.
        slide_id: Image shared by the point and cell.
        section_id: Tissue section shared by the point and cell.
        x_level0: Point X position in full-resolution image pixels.
        y_level0: Point Y position in full-resolution image pixels.
        cell_x_level0: Selected cell-centroid X position in those same pixels.
        cell_y_level0: Selected cell-centroid Y position.
        distance_level0: Euclidean distance between the point and selected centroid,
            measured in full-resolution pixels.
        class_id: Reference class attached to the point.
        evidence_set: Training or holdout assignment attached to the reference.
        spatial_region_id: ID of its labelled training or held-out region.
    """

    point_id: str
    cell_id: str
    slide_id: str
    section_id: str
    x_level0: float
    y_level0: float
    cell_x_level0: float
    cell_y_level0: float
    distance_level0: float
    class_id: str
    evidence_set: str
    spatial_region_id: str

    def __post_init__(self) -> None:
        """Retain immutable reference identities and finite mapped coordinates."""
        for name in (
            "point_id", "cell_id", "slide_id", "section_id", "class_id", "spatial_region_id"
        ):
            _nonblank(getattr(self, name), name)
        for name in (
            "x_level0", "y_level0", "cell_x_level0", "cell_y_level0", "distance_level0"
        ):
            _finite_number(getattr(self, name), name)
        if self.distance_level0 < 0:
            raise SupervisedPhenotypingError("distance_level0 must be non-negative")
        if self.evidence_set not in ("training", "holdout"):
            raise SupervisedPhenotypingError("evidence_set must be training or holdout")


@dataclass(frozen=True, slots=True)
class TrainingHoldoutEvidence:
    """

    Collects the checked training and held-out references after points have been matched to cells.

    We retain separate cell and region-ID sets for the two roles. The point records
    supply the coordinates behind each assignment. Subject assignment and any
    geometric relationship between the labelled regions are established earlier
    in the training workflow; this result records their supplied identities.

    Attributes:
        training_cell_ids: Sorted, unique cells supplying training labels.
        holdout_cell_ids: Sorted, unique held-out cells, excluding training cells.
        training_region_ids: Sorted IDs of regions contributing training points.
        holdout_region_ids: Sorted held-out region IDs, excluding training region IDs.
        evidence: Point-to-cell records in their original supplied order.
    """

    training_cell_ids: tuple[str, ...]
    holdout_cell_ids: tuple[str, ...]
    training_region_ids: tuple[str, ...]
    holdout_region_ids: tuple[str, ...]
    evidence: tuple[MappedPointEvidence, ...]

    def __post_init__(self) -> None:
        """

        Stores the reference-cell, region and point sequences as tuples.

        We construct this record through validate_training_holdout(), which checks
        that both roles are present and their cell/region IDs are distinct before
        retaining them here.
        """

        object.__setattr__(self, "training_cell_ids", tuple(self.training_cell_ids))
        object.__setattr__(self, "holdout_cell_ids", tuple(self.holdout_cell_ids))
        object.__setattr__(self, "training_region_ids", tuple(self.training_region_ids))
        object.__setattr__(self, "holdout_region_ids", tuple(self.holdout_region_ids))
        object.__setattr__(self, "evidence", tuple(self.evidence))














_POINT_FIELDS = frozenset(
    {
        "point_id",
        "cell_id",
        "slide_id",
        "section_id",
        "x_level0",
        "y_level0",
        "class_id",
        "evidence_set",
        "spatial_region_id",
    }
)


# ==============================================================================
#                 Joining native references to the measured cells               
# ==============================================================================


def map_point_evidence(
    points: Iterable[Mapping[str, object]],
    cells: Iterable[CellRecord],
    *,
    class_ids: Sequence[str],
    radius_level0: float,
) -> tuple[MappedPointEvidence, ...]:
    """Join native reference associations to the current cells by stable ID.

    QuPath resolves coordinate-only annotations once and saves their cell,
    image-entry and segmentation-population identity. This consuming boundary
    requires that association and its current image/section scope. Coordinates
    and distance remain informative; they do not select a different cell when
    an annotation moves. The radius applies to the native coordinate-only import binding.

    Args:
        points: Native point records carrying their saved cell IDs and roles.
        cells: Current eligible cells in the supervised reference snapshot.
        class_ids: Ordered classes allowed for these reference labels.
        radius_level0: Positive radius used by native coordinate-only annotation binding.

    Returns:
        Reference records joined in their input order, retaining coordinates.

    Raises:
        SupervisedPhenotypingError: An identity, scope, class or role is invalid.
    """
    radius = _finite_number(radius_level0, "radius_level0")
    if radius <= 0:
        raise SupervisedPhenotypingError("radius_level0 must be positive")
    class_set = frozenset(_ordered_strings(class_ids, "class_ids"))
    cell_records = tuple(cells)
    if any(not isinstance(cell, (CellRecord, SupervisedReferenceCell)) for cell in cell_records):
        raise SupervisedPhenotypingError("cells must contain CellRecord values")
    cells_by_id = {cell.cell_id: cell for cell in cell_records}
    if len(cells_by_id) != len(cell_records):
        raise SupervisedPhenotypingError("eligible cells require unique stable cell IDs")
    point_ids: set[str] = set()
    mapped: list[MappedPointEvidence] = []
    for raw in points:
        point = _exact_mapping(raw, _POINT_FIELDS, "training point")
        point_id = _nonblank(point["point_id"], "point_id")
        if point_id in point_ids:
            raise SupervisedPhenotypingError("training point IDs must be unique")
        point_ids.add(point_id)
        cell_id = _nonblank(point["cell_id"], "cell_id")
        slide_id = _nonblank(point["slide_id"], "slide_id")
        section_id = _nonblank(point["section_id"], "section_id")
        cell = cells_by_id.get(cell_id)
        if cell is None or (cell.slide_id, cell.section_id) != (slide_id, section_id):
            raise SupervisedPhenotypingError("saved reference cell is absent from its exact scope")
        class_id = _nonblank(point["class_id"], "class_id")
        if class_id not in class_set:
            raise SupervisedPhenotypingError(f"point class {class_id!r} is not declared")
        evidence_set = _nonblank(point["evidence_set"], "evidence_set")
        if evidence_set not in {"training", "holdout"}:
            raise SupervisedPhenotypingError("evidence_set must be training or holdout")
        region_id = _nonblank(point["spatial_region_id"], "spatial_region_id")
        x = _finite_number(point["x_level0"], "x_level0")
        y = _finite_number(point["y_level0"], "y_level0")
        mapped.append(MappedPointEvidence(
            point_id, cell_id, slide_id, section_id, x, y,
            float(cell.x_level0), float(cell.y_level0),
            hypot(x-cell.x_level0, y-cell.y_level0),
            class_id, evidence_set, region_id,
        ))
    return tuple(mapped)


def validate_training_holdout(
    evidence: Iterable[MappedPointEvidence],
) -> TrainingHoldoutEvidence:
    """

    Requires separate, nonempty cell and region-ID sets for training and held-out references.

    We collect mapped points by their training or holdout role, then sort the
    corresponding cell IDs. A repeated cell within one set or shared by both
    sets would give one measured object more than one reference assignment and
    is rejected. Region IDs are collected in the same way and must also be
    separate between roles.

    These checks use the supplied point and region identities. The training
    workflow establishes subject assignment and any required geometric separation
    of the annotated regions before this function receives their mapped points.

    Args:
        evidence: MappedPointEvidence records returned by point-to-cell matching.

    Returns:
        TrainingHoldoutEvidence containing sorted cell/region IDs and the
        original ordered point records.

    Raises:
        SupervisedPhenotypingError: Either role is absent, another role is
            supplied, or a cell or region ID is shared between the required sets.
    """

    records = tuple(evidence)
    if any(not isinstance(item, MappedPointEvidence) for item in records):
        raise SupervisedPhenotypingError("evidence must contain mapped point evidence")
    training = tuple(item for item in records if item.evidence_set == "training")
    holdout = tuple(item for item in records if item.evidence_set == "holdout")
    if not training or not holdout or len(training) + len(holdout) != len(records):
        raise SupervisedPhenotypingError("training and holdout evidence are both required")
    training_ids = tuple(sorted(item.cell_id for item in training))
    holdout_ids = tuple(sorted(item.cell_id for item in holdout))
    if len(set(training_ids)) != len(training_ids) or len(set(holdout_ids)) != len(holdout_ids):
        raise SupervisedPhenotypingError("each stable cell may occur once per evidence set")
    if set(training_ids).intersection(holdout_ids):
        raise SupervisedPhenotypingError("training and holdout stable cell IDs must be disjoint")
    training_regions = tuple(sorted({item.spatial_region_id for item in training}))
    holdout_regions = tuple(sorted({item.spatial_region_id for item in holdout}))
    if set(training_regions).intersection(holdout_regions):
        raise SupervisedPhenotypingError(
            "training and holdout spatial region metadata must be explicitly separated"
        )
    return TrainingHoldoutEvidence(
        training_cell_ids=training_ids,
        holdout_cell_ids=holdout_ids,
        training_region_ids=training_regions,
        holdout_region_ids=holdout_regions,
        evidence=records,
    )


# ==============================================================================
#              Checking the model and references before prediction              
# ==============================================================================




@dataclass(frozen=True, slots=True)
class SupervisedDeploymentPredictionResult:
    """

    Retains checked model predictions for one image and tissue section.

    We construct this result through reconcile_deployment_predictions(), which
    compares every returned row with the expected cells from the current image.
    Those expected IDs remain separate from the model's original reference cells.

    Attributes:
        classifier: Saved model identity, classes and ordered input measurements.
        confidence_threshold: Selected threshold in [0, 1].
        rejected_count: Number of cells below that threshold.
        import_rows: Cell-ID-sorted assignments covering the current eligible population.
    """

    classifier: SupervisedDeploymentClassifierIdentity
    confidence_threshold: float
    rejected_count: int
    import_rows: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        """

        Requires construction through the function that checks the expected image and cell IDs.

        Raises:
            SupervisedPhenotypingError: Direct construction is rejected. We use
                reconcile_deployment_predictions() to establish complete cell
                coverage before creating the result.
        """

        raise SupervisedPhenotypingError(
            "SupervisedDeploymentPredictionResult is owner-issued; "
            "use reconcile_deployment_predictions as the factory"
        )


_PREDICTION_FIELDS = frozenset({"cell_id", "class_id", "confidence"})


# ==============================================================================
#                 Preparing complete per-cell phenotype updates                 
# ==============================================================================


def reconcile_deployment_predictions(
    cells: Iterable[CellRecord | SupervisedDeploymentCellIdentity],
    predictions: Iterable[Mapping[str, object]],
    *,
    classifier: SupervisedDeploymentClassifierIdentity,
    confidence_threshold: object,
    included_cell_ids: frozenset[str] | None = None,
) -> SupervisedDeploymentPredictionResult:
    """Join our current native rows once and retain immutable threshold decisions.

    i) We admit the model and one image/section population, then validate the
    exact native ID/class/confidence rows. Their order never establishes cell
    association; complete unique stable IDs do.
    ii) We construct sorted immutable assignments directly from those checked
    values. Confidence equal to the cutoff retains its class; lower values
    become Unclassified while preserving their confidence and rejection flag.

    Args:
        cells: Measured cells or their immutable application identities.
        predictions: Native cell_id, class_id and confidence rows.
        classifier: Accepted model identity and its ordered class/feature schema.
        confidence_threshold: Finite cutoff in [0, 1].
        included_cell_ids: Exact selected population; omitted means all cells.
            Other measured cells retain a Not phenotyped assignment without a
            prediction or confidence value.

    Returns:
        Owner-issued result for exactly one complete image/section population.

    Raises:
        SupervisedPhenotypingError: Scope, coverage, classes or numerical values
            are invalid. Direct result construction remains unavailable.
    """

    if not isinstance(classifier, SupervisedDeploymentClassifierIdentity):
        raise SupervisedPhenotypingError(
            "classifier must be a SupervisedDeploymentClassifierIdentity"
        )
    threshold = _finite_number(confidence_threshold, "confidence_threshold")
    if threshold < 0 or threshold > 1:
        raise SupervisedPhenotypingError("confidence_threshold must be between zero and one")
    cell_records = tuple(cells)
    if any(
        not isinstance(cell, (CellRecord, SupervisedDeploymentCellIdentity))
        for cell in cell_records
    ):
        raise SupervisedPhenotypingError("cells must contain deployment cell identity values")
    scopes = {(cell.slide_id, cell.section_id) for cell in cell_records}
    if len(scopes) != 1:
        raise SupervisedPhenotypingError(
            "deployment cells must belong to exactly one slide/section scope"
        )
    expected_by_id = {cell.cell_id: cell for cell in cell_records}
    if len(expected_by_id) != len(cell_records):
        raise SupervisedPhenotypingError("expected cells require unique stable cell IDs")
    rows_by_id: dict[str, tuple[str, float]] = {}
    for raw in predictions:
        row = _exact_mapping(raw, _PREDICTION_FIELDS, "supervised prediction")
        cell_id = _nonblank(row["cell_id"], "cell_id")
        if cell_id in rows_by_id:
            raise SupervisedPhenotypingError("prediction stable cell IDs must be unique")
        class_id = _nonblank(row["class_id"], "class_id")
        if class_id not in classifier.class_ids:
            raise SupervisedPhenotypingError(f"prediction class {class_id!r} is not declared")
        confidence = _finite_number(row["confidence"], "confidence")
        if confidence < 0 or confidence > 1:
            raise SupervisedPhenotypingError("prediction confidence must be between zero and one")
        rows_by_id[cell_id] = (class_id, confidence)
    # Join the complete prediction population by cell ID before constructing
    # any phenotype update or counting a confidence rejection.
    selected = set(expected_by_id) if included_cell_ids is None else set(included_cell_ids)
    if not selected <= set(expected_by_id) or set(rows_by_id) != selected:
        raise SupervisedPhenotypingError(
            "supervised predictions must completely cover every expected stable cell exactly once"
        )
    import_rows: list[Mapping[str, object]] = []
    rejected = 0
    for cell_id in sorted(expected_by_id):
        cell = expected_by_id[cell_id]
        if cell_id not in selected:
            from .phenotyping_population import NOT_PHENOTYPED
            import_rows.append(MappingProxyType({
                "cell_id": cell_id, "slide_id": cell.slide_id, "section_id": cell.section_id,
                "primary_phenotype": NOT_PHENOTYPED, "phenotype_source": "population_excluded",
                "classifier_id": classifier.classifier_id, "classifier_sha256": classifier.classifier_sha256,
                "confidence": None, "confidence_rejected": False, "selected_display_view": "supervised",
            }))
            continue
        class_id, confidence = rows_by_id[cell_id]
        # Equality remains accepted. Keep the confidence and rejection flag
        # even when the displayed phenotype becomes Unclassified.
        was_rejected = confidence < threshold
        rejected += int(was_rejected)
        if class_id == "Unclassified" and not was_rejected:
            raise SupervisedPhenotypingError("Unclassified requires confidence rejection")
        import_rows.append(
            MappingProxyType({
                "cell_id": cell_id,
                "slide_id": cell.slide_id,
                "section_id": cell.section_id,
                "primary_phenotype": "Unclassified" if was_rejected else class_id,
                "phenotype_source": "supervised_qupath",
                "classifier_id": classifier.classifier_id,
                "classifier_sha256": classifier.classifier_sha256,
                "confidence": confidence,
                "confidence_rejected": was_rejected,
                "selected_display_view": "supervised",
            })
        )
    result = object.__new__(SupervisedDeploymentPredictionResult)
    object.__setattr__(result, "classifier", classifier)
    object.__setattr__(result, "confidence_threshold", threshold)
    object.__setattr__(result, "rejected_count", rejected)
    object.__setattr__(result, "import_rows", tuple(import_rows))
    return result


__all__ = [
    "MappedPointEvidence",
    "SupervisedFeatureDefinition",
    "SupervisedClassifierArtifact",
    "SupervisedDeploymentCellIdentity",
    "SupervisedDeploymentClassifierIdentity",
    "SupervisedDeploymentPredictionResult",
    "SupervisedPhenotypingError",
    "SUPERVISED_TRAINING_SCRIPT_ASSET",
    "SUPERVISED_TRAINING_STAGE",
    "TrainingHoldoutEvidence",
    "map_point_evidence",
    "reconcile_deployment_predictions",
    "validate_training_holdout",
]
