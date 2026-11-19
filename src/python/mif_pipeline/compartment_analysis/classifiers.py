"""

Records the saved compartment classifiers and checks their use with selected images and reference regions.

We retain each model's ID, version, channel roles, physical training scale
and output classes together with the annotations used for fitting and
evaluation. The saved annotation descriptions include their image IDs,
class labels, outline checksums and accepted parent regions. Cell-derived
references additionally record the selected cells and measurement versions.

Before applying a primary or secondary classifier, we compare its panel,
validation group, physical scale and channel-role mapping with the current
request. Primary output classes must match the selected biological classes.
Secondary classifiers produce proposed structures whose accepted biological
labels are assigned after the corresponding structure checks and review.

The saved training_defaults field retains the model configuration supplied
when the fit was recorded. New-model starting values are selected by the
authoring forms from module JSON settings; loading a saved definition uses
its recorded values.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import re
from types import MappingProxyType
from uuid import UUID

from mif_pipeline.pipeline_core.config import (
    ANALYSIS_DOMAIN_PARENT_ID,
    ClassifierSpec,
    IGNORE_CLASS_ID,
    MAXIMUM_PRIMARY_CLASS_COUNT,
    MINIMUM_PRIMARY_CLASS_COUNT,
)
from mif_pipeline.pipeline_core.models import ROIAnalysisError

# ==============================================================================
#                        Saved inputs and reference descriptions
# ==============================================================================


class ChannelMappingError(ROIAnalysisError):
    """

    Reports an image-channel mapping that cannot supply the saved classifier's required inputs.
    """

class ClassifierCompatibilityError(ROIAnalysisError):
    """

    Reports a mismatch between a saved model, its reference description and the selected analysis inputs.
    """

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRAINING_EVIDENCE_SCHEMA = "training-evidence-snapshot/v2"
_EVIDENCE_SPLITS = ("training", "validation", "test")


def _nonblank_string(value: object, field: str) -> str:
    """

    Checks that a model, reference or channel field contains nonblank text.

    Args:
        value: Text value being decoded.
        field: Field name included in errors.

    Returns:
        Checked text used by subsequent ID or name comparisons.

    Raises:
        ClassifierCompatibilityError: Text is missing or blank.
    """

    if not isinstance(value, str) or not value.strip():
        raise ClassifierCompatibilityError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    """

    Checks the lowercase hexadecimal format of a recorded content checksum.

    Args:
        value: Expected 64-character SHA-256 text.
        field: Checksum-field name included in an error.

    Returns:
        Checked checksum for the later comparison with recorded content.

    Raises:
        ClassifierCompatibilityError: The value has an invalid type, length or character set.
    """

    result = _nonblank_string(value, field)
    if _SHA256.fullmatch(result) is None:
        raise ClassifierCompatibilityError(f"{field} must be a lowercase SHA-256")
    return result


def _canonical_sha256(value: object) -> str:
    """

    Calculates a checksum from a record's consistently ordered JSON fields.

    We first convert retained mappings and sequences to ordinary dictionaries
    and lists. Sorting dictionary keys and using fixed separators makes the
    checksum independent of dictionary insertion order, while preserving list order.

    Args:
        value: Saved model, annotation or feature-description fields.

    Returns:
        SHA-256 of the UTF-8 JSON representation.
    """

    encoded = json.dumps(
        _export_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _export_json(value: object) -> object:
    """

    Copies nested model fields into JSON-compatible dictionaries and lists.

    Args:
        value: Scalar, mapping or non-text sequence to export.

    Returns:
        Corresponding nested dictionaries/lists with scalar values retained.
    """

    if isinstance(value, Mapping):
        return {key: _export_json(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_export_json(nested) for nested in value]
    return value


def _freeze_json(value: object) -> object:
    """

    Copies nested saved feature or software settings into protected mappings and tuples.

    Args:
        value: Checked JSON-compatible model-description value.

    Returns:
        Copied value whose nested dictionaries and sequences cannot be edited
        through ordinary item assignment after being retained with the model.
    """

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    """

    Requires a dictionary-like record with text field names.

    Args:
        value: Model-description mapping to inspect.
        field: Record name included in an error.

    Returns:
        Checked mapping whose specific fields are validated by the calling reader.

    Raises:
        ClassifierCompatibilityError: The value is not a mapping or contains non-text keys.
    """

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ClassifierCompatibilityError(f"{field} must be a string-keyed mapping")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    """

    Requires a collection of values rather than treating a text string as a list of entries.

    Args:
        value: Sequence supplied for classes, features or reference records.
        field: Sequence-field name used in errors.

    Returns:
        Checked sequence for the caller's item and length validation.

    Raises:
        ClassifierCompatibilityError: The value is text or not a sequence.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClassifierCompatibilityError(f"{field} must be a sequence")
    return value


def _positive_number(value: object, field: str) -> float:
    """

    Reads a finite strictly positive value such as physical pixel size.

    Args:
        value: Integer or float supplied for the quantity, excluding booleans.
        field: Quantity name used in an error.

    Returns:
        Finite float greater than zero.

    Raises:
        ClassifierCompatibilityError: The value has an invalid type, sign or finiteness.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClassifierCompatibilityError(f"{field} must be a positive number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ClassifierCompatibilityError(f"{field} must be a positive number")
    return result


def _nonnegative_number(value: object, field: str) -> float:
    """

    Reads a finite nonnegative value such as a tolerance or recorded evaluation score.

    Args:
        value: Numerical value, excluding booleans.
        field: Quantity name used in an error.

    Returns:
        Float greater than or equal to zero. A zero accuracy or F1 remains a
        valid evaluation result that can be retained for review.

    Raises:
        ClassifierCompatibilityError: The value is negative, nonfinite or nonnumerical.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClassifierCompatibilityError(f"{field} must be a non-negative number")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise ClassifierCompatibilityError(f"{field} must be a non-negative number")
    return result


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    """

    Reads a nonempty list of unique class, channel or image identifiers.

    Args:
        value: Sequence of required text values.
        field: List name included in validation errors.

    Returns:
        Tuple retaining the original declared order.

    Raises:
        ClassifierCompatibilityError: The list is empty or contains invalid/duplicate text.
    """

    parsed = tuple(_nonblank_string(item, field) for item in _sequence(value, field))
    if not parsed:
        raise ClassifierCompatibilityError(f"{field} must not be empty")
    if len(set(parsed)) != len(parsed):
        raise ClassifierCompatibilityError(f"{field} must not contain duplicates")
    return parsed


def _sha256_tuple(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    """

    Reads the ordered checksums used to compare reference groups and saved descriptions.

    Args:
        value: Sequence of complete lowercase SHA-256 strings.
        field: Checksum-list name included in an error.
        allow_empty: Whether the corresponding evaluation group may have no records.

    Returns:
        Unique checksums already sorted in the order required by the saved format.

    Raises:
        ClassifierCompatibilityError: Values are invalid, duplicated, unsorted
            or empty when that group is required.
    """

    parsed = tuple(_sha256(item, field) for item in _sequence(value, field))
    if not parsed and not allow_empty:
        raise ClassifierCompatibilityError(f"{field} must not be empty")
    if tuple(sorted(parsed)) != parsed or len(set(parsed)) != len(parsed):
        raise ClassifierCompatibilityError(
            f"{field} must be a unique deterministic ordered inventory"
        )
    return parsed


def _uuid(value: object, field: str) -> str:
    """

    Checks a reference annotation's UUID using its standard hyphenated text representation.

    Args:
        value: Annotation UUID text supplied by QuPath.
        field: Field name included in an error.

    Returns:
        UUID text whose parsed and re-encoded forms agree.

    Raises:
        ClassifierCompatibilityError: The value cannot be parsed or uses another spelling.
    """

    result = _nonblank_string(value, field)
    try:
        parsed = UUID(result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClassifierCompatibilityError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != result:
        raise ClassifierCompatibilityError(f"{field} must be a canonical UUID")
    return result


# ==============================================================================
#            Recording individual training and evaluation references            
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TrainingEvidenceSnapshot:
    """

    Retains the selected annotation, its class and the accepted image region used for one reference.

    The annotation's image ID, UUID and outline checksum identify its spatial
    reference. The parent and analysis-region descriptions identify where that
    reference was permitted to contribute. For cell-derived fitting, the cell
    list and accepted segmentation/measurement versions identify the measured
    observations selected beneath that annotation.

    Attributes:
        schema_version: Current training-evidence-snapshot/v2 format.
        project_entry_id: Image entry containing the reference annotation.
        annotation_uuid: Annotation's QuPath object UUID.
        class_id: Analysis class assigned to that reference.
        path_class_id: QuPath PathClass identity corresponding to its label.
        normalized_roi_sha256: Checksum of the normalized annotation outline.
        material_parent_id: Accepted parent object or analysis-region ID.
        material_parent_type: Analysis domain, primary compartment or secondary structure.
        material_parent_type_id: Specific parent class/structure type, with
            AnalysisDomain used for an analysis-region parent.
        material_parent_revision_sha256: Checksum of the accepted parent version.
        split: Training, validation or test role assigned to this reference.
        analysis_domain_id: Selected analysis-region ID.
        analysis_domain_revision_sha256: Checksum of that region's accepted version.
        domain_set_sha256: Checksum of the selected set of analysis regions.
        cell_ids: Sorted cell IDs selected beneath the annotation, or an empty
            tuple when this reference does not select measured cells.
        cell_evidence_sha256s: One recorded cell-description checksum per selected cell,
            aligned with cell_ids.
        segmentation_generation_id: Accepted cell-detection version, required
            when cells are selected.
        measurement_generation_id: Accepted measurement version for those cells.
        cell_selection_rule: Recorded explicit-hierarchy-descendant-cells/v1 rule.
        cell_selection_sha256: Checksum of the cell list, cell descriptions and
            segmentation/measurement versions.
        evidence_sha256: Checksum of this complete annotation/reference description.
    """

    schema_version: str
    project_entry_id: str
    annotation_uuid: str
    class_id: str
    path_class_id: str
    normalized_roi_sha256: str
    material_parent_id: str
    material_parent_type: str
    material_parent_type_id: str
    material_parent_revision_sha256: str
    split: str
    analysis_domain_id: str
    analysis_domain_revision_sha256: str
    domain_set_sha256: str
    cell_ids: tuple[str, ...]
    cell_evidence_sha256s: tuple[str, ...]
    segmentation_generation_id: str | None
    measurement_generation_id: str | None
    cell_selection_rule: str
    cell_selection_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate reference scope, cell selection and both hashes at initial admission."""
        schema = _nonblank_string(self.schema_version, "training evidence schema_version")
        if schema != _TRAINING_EVIDENCE_SCHEMA:
            raise ClassifierCompatibilityError("training evidence snapshot schema is unsupported")
        split = _nonblank_string(self.split, "training evidence split")
        if split not in _EVIDENCE_SPLITS:
            raise ClassifierCompatibilityError(
                "training evidence split must be training, validation, or test"
            )
        cell_ids = tuple(
            _nonblank_string(item, "training evidence cell ID")
            for item in _sequence(self.cell_ids, "training evidence cell_ids")
        )
        if tuple(sorted(cell_ids)) != cell_ids or len(set(cell_ids)) != len(cell_ids):
            raise ClassifierCompatibilityError(
                "training evidence cell IDs must be a unique deterministic ordered inventory"
            )
        cell_evidence_sha256s = tuple(
            _sha256(item, "training evidence cell evidence SHA-256")
            for item in _sequence(
                self.cell_evidence_sha256s,
                "training evidence cell_evidence_sha256s",
            )
        )
        if len(cell_evidence_sha256s) != len(cell_ids) or len(set(cell_evidence_sha256s)) != len(
            cell_evidence_sha256s
        ):
            raise ClassifierCompatibilityError(
                "training evidence cell evidence must exactly match the stable-cell inventory"
            )
        generation_ids: dict[str, str | None] = {}
        for field in ("segmentation_generation_id", "measurement_generation_id"):
            raw = getattr(self, field)
            generation_ids[field] = (
                None if raw is None else _nonblank_string(raw, f"training evidence {field}")
            )
        if cell_ids and any(value is None for value in generation_ids.values()):
            raise ClassifierCompatibilityError(
                "selected training cells require exact segmentation and measurement generation evidence"
            )
        if not cell_ids and any(value is not None for value in generation_ids.values()):
            raise ClassifierCompatibilityError(
                "empty training-cell selection cannot declare generation evidence"
            )
        material_parent_type = _nonblank_string(
            self.material_parent_type,
            "training evidence material parent type",
        )
        if material_parent_type not in {"analysis_domain", "primary", "secondary"}:
            raise ClassifierCompatibilityError("training evidence material parent type is invalid")
        material_parent_type_id = _nonblank_string(
            self.material_parent_type_id,
            "training evidence material parent type ID",
        )
        if (
            material_parent_type == "analysis_domain"
            and material_parent_type_id != ANALYSIS_DOMAIN_PARENT_ID
        ):
            raise ClassifierCompatibilityError(
                "analysis-domain training parent requires the exact AnalysisDomain type ID"
            )
        selection_rule = _nonblank_string(
            self.cell_selection_rule,
            "training evidence cell selection rule",
        )
        if selection_rule != "explicit-hierarchy-descendant-cells/v1":
            raise ClassifierCompatibilityError(
                "training evidence cell selection rule is unsupported"
            )
        selection_sha256 = _sha256(
            self.cell_selection_sha256,
            "training evidence cell selection SHA-256",
        )
        selection_payload = {
            "cell_selection_rule": selection_rule,
            "cell_ids": list(cell_ids),
            "cell_evidence_sha256s": list(cell_evidence_sha256s),
            **generation_ids,
        }
        if selection_sha256 != _canonical_sha256(selection_payload):
            raise ClassifierCompatibilityError(
                "training evidence cell selection SHA-256 does not match its exact live selection"
            )
        values = dict(
            schema_version=schema,
            project_entry_id=_nonblank_string(
                self.project_entry_id, "training evidence project entry"
            ),
            annotation_uuid=_uuid(
                self.annotation_uuid, "training evidence annotation UUID"
            ),
            class_id=_nonblank_string(self.class_id, "training evidence class ID"),
            path_class_id=_nonblank_string(
                self.path_class_id, "training evidence PathClass ID"
            ),
            normalized_roi_sha256=_sha256(
                self.normalized_roi_sha256,
                "training evidence normalized ROI SHA-256",
            ),
            material_parent_id=_nonblank_string(
                self.material_parent_id,
                "training evidence material parent",
            ),
            material_parent_type=material_parent_type,
            material_parent_type_id=material_parent_type_id,
            material_parent_revision_sha256=_sha256(
                self.material_parent_revision_sha256,
                "training evidence material parent revision SHA-256",
            ),
            split=split,
            analysis_domain_id=_sha256(
                self.analysis_domain_id,
                "training evidence analysis domain ID",
            ),
            analysis_domain_revision_sha256=_sha256(
                self.analysis_domain_revision_sha256,
                "training evidence analysis-domain revision SHA-256",
            ),
            domain_set_sha256=_sha256(
                self.domain_set_sha256,
                "training evidence domain-set SHA-256",
            ),
            cell_ids=cell_ids,
            cell_evidence_sha256s=cell_evidence_sha256s,
            segmentation_generation_id=generation_ids["segmentation_generation_id"],
            measurement_generation_id=generation_ids["measurement_generation_id"],
            cell_selection_rule=selection_rule,
            cell_selection_sha256=selection_sha256,
            evidence_sha256=_sha256(self.evidence_sha256, "training evidence SHA-256"),
        )
        for name, value in values.items():
            object.__setattr__(self, name, value)
        if (
            self.material_parent_type == "analysis_domain"
            and self.material_parent_revision_sha256 != self.analysis_domain_revision_sha256
        ):
            raise ClassifierCompatibilityError(
                "training evidence analysis-domain parent revision is stale"
            )
        if (
            self.material_parent_type != "analysis_domain"
            and self.material_parent_type_id == ANALYSIS_DOMAIN_PARENT_ID
        ):
            raise ClassifierCompatibilityError(
                "non-domain training parent cannot use the AnalysisDomain type ID"
            )
        if self.evidence_sha256 != _canonical_sha256(self.digest_payload()):
            raise ClassifierCompatibilityError(
                "training evidence SHA-256 does not match its exact snapshot row"
            )

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingEvidenceSnapshot":
        """Decode the current fields; the constructor validates and freezes the row."""
        mapping = _mapping(value, "training evidence snapshot")
        if set(mapping) != set(cls.__dataclass_fields__):
            raise ClassifierCompatibilityError("training evidence snapshot fields are invalid")
        return cls(**mapping)

    def digest_payload(self) -> dict[str, object]:
        """

        Collects the reference fields used to calculate evidence_sha256.

        Returns:
            Annotation, parent-region, role and cell-selection values, excluding
            the complete-row checksum itself.
        """

        return {
            "schema_version": self.schema_version,
            "project_entry_id": self.project_entry_id,
            "annotation_uuid": self.annotation_uuid,
            "class_id": self.class_id,
            "path_class_id": self.path_class_id,
            "normalized_roi_sha256": self.normalized_roi_sha256,
            "material_parent_id": self.material_parent_id,
            "material_parent_type": self.material_parent_type,
            "material_parent_type_id": self.material_parent_type_id,
            "material_parent_revision_sha256": self.material_parent_revision_sha256,
            "split": self.split,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "domain_set_sha256": self.domain_set_sha256,
            "cell_ids": list(self.cell_ids),
            "cell_evidence_sha256s": list(self.cell_evidence_sha256s),
            "segmentation_generation_id": self.segmentation_generation_id,
            "measurement_generation_id": self.measurement_generation_id,
            "cell_selection_rule": self.cell_selection_rule,
            "cell_selection_sha256": self.cell_selection_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        """

        Serializes the checked reference row with its complete checksum.

        Returns:
            Dictionary containing digest_payload() fields and evidence_sha256.
        """

        return {**self.digest_payload(), "evidence_sha256": self.evidence_sha256}


def _validate_evidence_splits(
    snapshots: tuple[TrainingEvidenceSnapshot, ...],
    *,
    domain_ids: Mapping[str, tuple[str, ...]],
    evidence_sha256s: Mapping[str, tuple[str, ...]],
) -> None:
    """

    Checks that recorded training, validation and test references remain separated.

    For each role, we compare the annotation rows with its saved analysis-region
    IDs and row checksums. Across roles, we track image-specific annotation IDs,
    normalized-outline checksums, row checksums and selected cell IDs. Reusing
    any of those same references in a different role is rejected.

    Subject allocation and geometric overlap of different reference polygons are
    established by the training workflow. This function checks the recorded
    annotation, full-outline and cell identities; an analysis region may contain
    several distinct labelled reference annotations.

    Args:
        snapshots: Checked annotation/reference rows from the saved model.
        domain_ids: Ordered analysis-region IDs associated with each role.
        evidence_sha256s: Ordered reference-row checksums associated with each role.

    Raises:
        ClassifierCompatibilityError: A role's list differs from its saved rows
            or an identical reference/cell is shared between roles.
    """

    for split in _EVIDENCE_SPLITS:
        rows = tuple(item for item in snapshots if item.split == split)
        if tuple(sorted({item.analysis_domain_id for item in rows})) != domain_ids[split]:
            raise ClassifierCompatibilityError(
                f"classifier {split} domain IDs do not match evidence snapshots"
            )
        if tuple(sorted(item.evidence_sha256 for item in rows)) != evidence_sha256s[split]:
            raise ClassifierCompatibilityError(
                f"classifier {split} evidence SHA-256s do not match snapshots"
            )

    split_by_identity: dict[tuple[str, str, str], str] = {}
    for snapshot in snapshots:
        # Track the particular annotation, complete outline and selected cells
        # within each image. The same reference cannot appear in another role.
        identities = (
            ("annotation", snapshot.project_entry_id, snapshot.annotation_uuid),
            (
                "normalized ROI",
                snapshot.project_entry_id,
                snapshot.normalized_roi_sha256,
            ),
            ("evidence", snapshot.project_entry_id, snapshot.evidence_sha256),
            *(("cell", snapshot.project_entry_id, cell_id) for cell_id in snapshot.cell_ids),
        )
        for identity in identities:
            prior = split_by_identity.setdefault(identity, snapshot.split)
            if prior != snapshot.split:
                raise ClassifierCompatibilityError(
                    f"classifier split leakage across {identity[0]} identity"
                )


def _readonly_strings(value: object, field: str) -> Mapping[str, str]:
    """

    Copies the saved channel-role mapping after checking every role and channel ID.

    Args:
        value: Nonempty mapping of feature-input roles to channel IDs.
        field: Mapping name included in an error.

    Returns:
        Protected mapping of text roles to text channel identifiers.

    Raises:
        ClassifierCompatibilityError: A role, channel ID or mapping is missing/invalid.
    """

    mapping = _mapping(value, field)
    if not mapping:
        raise ClassifierCompatibilityError(f"{field} must not be empty")
    return MappingProxyType(
        {
            _nonblank_string(key, f"{field} role"): _nonblank_string(item, f"{field}[{key!r}]")
            for key, item in mapping.items()
        }
    )


# ==============================================================================
#               Matching saved input channels to the current image              
# ==============================================================================


def resolve_channel_roles(
    saved: Sequence[str] | Mapping[str, str],
    current: Sequence[str] | Mapping[str, str],
    *,
    override: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """

    Maps the saved input channels to the selected image using exact IDs and explicit replacements.

    We first collect the saved and current channel identities and require each
    list to be unique. For each saved channel, use its supplied replacement when
    present, otherwise its original ID. Every resulting ID must exist in the
    current image, and two saved channels cannot map to the same current channel.

    Args:
        saved: Saved channel IDs, supplied directly or as channel-role mapping values.
        current: Available current channel IDs in either supported representation.
        override: Optional explicit saved-ID-to-current-ID replacements.

    Returns:
        Dictionary mapping every saved channel ID to one current input channel.

    Raises:
        ChannelMappingError: IDs repeat, a replacement refers to an unknown
            saved/current channel, or the complete mapping is not one-to-one.
    """

    saved_values = tuple(saved.values()) if isinstance(saved, Mapping) else tuple(saved)
    current_values = tuple(current.values()) if isinstance(current, Mapping) else tuple(current)
    saved_ids = tuple(_nonblank_string(value, "saved channel") for value in saved_values)
    current_ids = tuple(_nonblank_string(value, "current channel") for value in current_values)
    if len(set(saved_ids)) != len(saved_ids) or len(set(current_ids)) != len(current_ids):
        raise ChannelMappingError("channel identities must be unique")
    overrides = (
        {}
        if override is None
        else {
            _nonblank_string(key, "channel override source"): _nonblank_string(
                value, "channel override target"
            )
            for key, value in _mapping(override, "channel override").items()
        }
    )
    unknown_sources = set(overrides) - set(saved_ids)
    unknown_targets = set(overrides.values()) - set(current_ids)
    if unknown_sources or unknown_targets:
        raise ChannelMappingError("explicit override must reference saved and current channels")

    resolved: dict[str, str] = {}
    for saved_id in saved_ids:
        target = overrides.get(saved_id, saved_id)
        if target not in current_ids:
            raise ChannelMappingError(
                f"saved channel {saved_id!r} has no exact current mapping; explicit override is required"
            )
        resolved[saved_id] = target
    if len(set(resolved.values())) != len(resolved):
        raise ChannelMappingError("explicit override must map channels one-to-one")
    return resolved


# ==============================================================================
#                    Reading complete saved model definitions                   
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ClassifierDefinition:
    """

    Records a saved compartment model with its image inputs, reference sets and evaluated settings.

    Construction checks reference allocation, class/feature order and associated
    checksums, then freezes nested settings. The external from_mapping() reader
    decodes annotation rows before construction. Applying the model compares
    these retained values with the required channels, image scale and processing group.

    Attributes:
        classifier_id: Model identifier within the analysis.
        version: Selected version of the saved model.
        kind: Primary compartment classifier or secondary structure classifier.
        channel_roles: Required feature-input roles mapped to saved channel IDs.
        domain_id: Validation-group ID in which this model is permitted to run.
        pixel_scale_um: Physical pixel size used for the classifier inputs, in µm/pixel.
        pixel_scale_tolerance_um: Allowed absolute difference from that physical scale.
        training_domain_ids: Sorted analysis-region IDs represented by training references.
        validation_domain_ids: Sorted analysis-region IDs represented by validation references.
        test_domain_ids: Sorted analysis-region IDs represented by test references.
        training_evidence_sha256s: Sorted checksums of the training-reference rows.
        validation_evidence_sha256s: Sorted checksums of the validation-reference rows.
        test_evidence_sha256s: Sorted checksums of the test-reference rows.
        evidence_snapshots: Complete annotation/reference rows in role and checksum order.
        project_entry_ids: Sorted image-entry IDs contributing these references.
        evidence_snapshot_set_sha256: Checksum of the ordered complete reference-row list.
        runtime_signature: Recorded software/execution settings associated with fitting.
        runtime_signature_sha256: Checksum of that software/execution description.
        feature_signature: Recorded feature calculation and input ordering.
        feature_signature_sha256: Checksum of the complete feature description.
        metrics: Recorded finite nonnegative evaluation scores. A zero value is
            retained; a not_evaluated model has an empty metrics mapping.
        validation_status: Recorded validated, provisional or not_evaluated state.
        training_defaults: Actual fitting configuration supplied when the model
            description was written. An absent optional description becomes
            an empty mapping rather than being populated from current defaults.
        output_classes: Ordered primary or joint-secondary output classes.
        output_class: Proposal class for a separate secondary classifier, Candidate_<Structure>.
        final_class: Structure class for a separate secondary classifier after review.
        unattended_review_override: Whether a provisional or unevaluated model
            was explicitly permitted for processing without another review prompt.
    """

    classifier_id: str
    version: str
    kind: str
    channel_roles: Mapping[str, str]
    domain_id: str
    pixel_scale_um: float
    pixel_scale_tolerance_um: float
    training_domain_ids: tuple[str, ...]
    validation_domain_ids: tuple[str, ...]
    test_domain_ids: tuple[str, ...]
    training_evidence_sha256s: tuple[str, ...]
    validation_evidence_sha256s: tuple[str, ...]
    test_evidence_sha256s: tuple[str, ...]
    evidence_snapshots: tuple[TrainingEvidenceSnapshot, ...]
    project_entry_ids: tuple[str, ...]
    evidence_snapshot_set_sha256: str
    runtime_signature: Mapping[str, object]
    runtime_signature_sha256: str
    feature_signature: Mapping[str, object]
    feature_signature_sha256: str
    metrics: Mapping[str, float]
    validation_status: str
    training_defaults: Mapping[str, object]
    output_classes: tuple[str, ...] = ()
    output_class: str | None = None
    final_class: str | None = None
    unattended_review_override: bool = False

    def to_mapping(self) -> dict[str, object]:
        """

        Collects the complete saved model description for JSON output.

        We export ordered reference rows and class lists together with copied feature/
        software descriptions and the actual recorded fitting settings. The checksums
        remain attached to the corresponding values for later comparison.

        Returns:
            Dictionary containing the model, inputs, references, evaluation and
            primary/secondary output definitions.
        """

        return {
            "classifier_id": self.classifier_id,
            "version": self.version,
            "kind": self.kind,
            "channel_roles": dict(self.channel_roles),
            "domain_id": self.domain_id,
            "pixel_scale_um": self.pixel_scale_um,
            "pixel_scale_tolerance_um": self.pixel_scale_tolerance_um,
            "training_domain_ids": list(self.training_domain_ids),
            "validation_domain_ids": list(self.validation_domain_ids),
            "test_domain_ids": list(self.test_domain_ids),
            "training_evidence_sha256s": list(self.training_evidence_sha256s),
            "validation_evidence_sha256s": list(self.validation_evidence_sha256s),
            "test_evidence_sha256s": list(self.test_evidence_sha256s),
            "evidence_snapshots": [item.to_mapping() for item in self.evidence_snapshots],
            "project_entry_ids": list(self.project_entry_ids),
            "evidence_snapshot_set_sha256": self.evidence_snapshot_set_sha256,
            "runtime_signature": _export_json(self.runtime_signature),
            "runtime_signature_sha256": self.runtime_signature_sha256,
            "feature_signature": _export_json(self.feature_signature),
            "feature_signature_sha256": self.feature_signature_sha256,
            "metrics": dict(self.metrics),
            "validation_status": self.validation_status,
            "training_defaults": _export_json(self.training_defaults),
            "output_classes": list(self.output_classes),
            "output_class": self.output_class,
            "final_class": self.final_class,
            "unattended_review_override": self.unattended_review_override,
        }

    def __post_init__(self) -> None:
        """Validate and freeze the saved settings once when the definition is built.

        The reference rows already own their individual identity/checksum checks.
        Here we check their allocation, ordering and complete-set digest alongside
        the feature/runtime signatures and physical input scale. Copying nested
        settings prevents later caller edits from invalidating those signatures.
        Owner payloads can then retain this definition without decoding it again.

        A not_evaluated model must contain only training references and no
        evaluation metrics. Other states require at least one validation or test
        group with its associated reference rows and finite nonnegative scores.

        Each role's region and reference-checksum lists must agree with its rows.
        Rows are unique, ordered by training/validation/test role and then checksum.
        The contributing image list and complete reference-list checksum must agree,
        and references cannot share annotation, outline or cell identities across
        roles. Channel roles and physical training scale are retained for the
        current-image comparisons performed before model application.

        Raises:
            ClassifierCompatibilityError: Model type/status, scores, reference
                allocation, row ordering, image coverage or checksums are inconsistent.
        """
        kind = _nonblank_string(self.kind, "classifier.kind")
        if kind not in {"primary", "secondary"}:
            raise ClassifierCompatibilityError("classifier.kind must be 'primary' or 'secondary'")
        status = _nonblank_string(self.validation_status, "classifier.validation_status")
        if status not in {"validated", "provisional", "not_evaluated"}:
            raise ClassifierCompatibilityError(
                "classifier.validation_status must be 'validated', 'provisional', or 'not_evaluated'"
            )
        metrics = {
            _nonblank_string(key, "classifier.metrics name"): _nonnegative_number(
                item, f"classifier.metrics[{key!r}]"
            )
            for key, item in _mapping(self.metrics, "classifier.metrics").items()
        }
        if status == "not_evaluated":
            if metrics:
                raise ClassifierCompatibilityError("not_evaluated classifier.metrics must be empty")
        elif not metrics:
            raise ClassifierCompatibilityError("classifier.metrics must not be empty")
        # Retain the actual fitting description recorded by the model writer.
        # New-model defaults are selected by the authoring form before fitting.
        defaults = dict(
            _mapping(self.training_defaults, "classifier.training_defaults")
        )
        output_classes = tuple(
            _nonblank_string(item, "classifier.output_classes")
            for item in _sequence(self.output_classes, "classifier.output_classes")
        )
        output_class = self.output_class
        final_class = self.final_class
        if output_class is not None:
            output_class = _nonblank_string(output_class, "classifier.output_class")
        if final_class is not None:
            final_class = _nonblank_string(final_class, "classifier.final_class")
        review_override = self.unattended_review_override
        if not isinstance(review_override, bool):
            raise ClassifierCompatibilityError(
                "classifier.unattended_review_override must be a boolean"
            )
        split_domain_ids = {
            split: _sha256_tuple(
                getattr(self, f"{split}_domain_ids"),
                f"classifier.{split}_domain_ids",
                allow_empty=split != "training",
            )
            for split in _EVIDENCE_SPLITS
        }
        split_evidence_sha256s = {
            split: _sha256_tuple(
                getattr(self, f"{split}_evidence_sha256s"),
                f"classifier.{split}_evidence_sha256s",
                allow_empty=split != "training",
            )
            for split in _EVIDENCE_SPLITS
        }
        for split in ("validation", "test"):
            if bool(split_domain_ids[split]) != bool(split_evidence_sha256s[split]):
                raise ClassifierCompatibilityError(
                    f"classifier {split} domain and evidence inventories must be coherent"
                )
        if status != "not_evaluated" and not any(
            split_domain_ids[split] for split in ("validation", "test")
        ):
            raise ClassifierCompatibilityError(
                "evaluated classifier requires validation or test evidence"
            )
        snapshots = tuple(_sequence(self.evidence_snapshots, "classifier.evidence_snapshots"))
        if not all(isinstance(item, TrainingEvidenceSnapshot) for item in snapshots):
            raise ClassifierCompatibilityError(
                "classifier.evidence_snapshots must contain TrainingEvidenceSnapshot values"
            )
        if not snapshots:
            raise ClassifierCompatibilityError("classifier.evidence_snapshots must not be empty")
        if status == "not_evaluated" and (
            any(
                split_domain_ids[split] or split_evidence_sha256s[split]
                for split in ("validation", "test")
            )
            or any(item.split != "training" for item in snapshots)
        ):
            raise ClassifierCompatibilityError(
                "not_evaluated classifier must contain training evidence only"
            )
        if len({item.evidence_sha256 for item in snapshots}) != len(snapshots):
            raise ClassifierCompatibilityError("classifier evidence snapshots must be unique")
        if snapshots != tuple(
            sorted(
                snapshots,
                key=lambda item: (_EVIDENCE_SPLITS.index(item.split), item.evidence_sha256),
            )
        ):
            raise ClassifierCompatibilityError(
                "classifier evidence snapshots must use deterministic split and digest order"
            )
        project_entry_ids = _string_tuple(
            self.project_entry_ids, "classifier.project_entry_ids"
        )
        if tuple(sorted(project_entry_ids)) != project_entry_ids:
            raise ClassifierCompatibilityError(
                "classifier.project_entry_ids must use deterministic order"
            )
        if project_entry_ids != tuple(sorted({item.project_entry_id for item in snapshots})):
            raise ClassifierCompatibilityError(
                "classifier project-entry inventory does not match evidence snapshots"
            )
        snapshot_set_sha256 = _sha256(
            self.evidence_snapshot_set_sha256,
            "classifier.evidence_snapshot_set_sha256",
        )
        if snapshot_set_sha256 != _canonical_sha256([item.to_mapping() for item in snapshots]):
            raise ClassifierCompatibilityError(
                "classifier evidence snapshot-set SHA-256 does not match its exact rows"
            )
        signatures: dict[str, tuple[Mapping[str, object], str]] = {}
        for role in ("runtime", "feature"):
            signature = _mapping(
                getattr(self, f"{role}_signature"),
                f"classifier.{role}_signature",
            )
            if not signature:
                raise ClassifierCompatibilityError(f"classifier.{role}_signature must not be empty")
            try:
                actual_signature_sha256 = _canonical_sha256(signature)
            except (TypeError, ValueError) as exc:
                raise ClassifierCompatibilityError(
                    f"classifier.{role}_signature must contain canonical JSON"
                ) from exc
            signature_sha256 = _sha256(
                getattr(self, f"{role}_signature_sha256"),
                f"classifier.{role}_signature_sha256",
            )
            if signature_sha256 != actual_signature_sha256:
                raise ClassifierCompatibilityError(
                    f"classifier {role} signature SHA-256 does not match"
                )
            frozen_signature = _freeze_json(signature)
            assert isinstance(frozen_signature, Mapping)
            signatures[role] = (frozen_signature, signature_sha256)
        _validate_evidence_splits(
            snapshots,
            domain_ids=split_domain_ids,
            evidence_sha256s=split_evidence_sha256s,
        )
        values = dict(
            classifier_id=_nonblank_string(
                self.classifier_id, "classifier.classifier_id"
            ),
            version=_nonblank_string(self.version, "classifier.version"),
            kind=kind,
            channel_roles=_readonly_strings(
                self.channel_roles, "classifier.channel_roles"
            ),
            domain_id=_nonblank_string(self.domain_id, "classifier.domain_id"),
            pixel_scale_um=_positive_number(
                self.pixel_scale_um, "classifier.pixel_scale_um"
            ),
            pixel_scale_tolerance_um=_nonnegative_number(
                self.pixel_scale_tolerance_um, "classifier.pixel_scale_tolerance_um"
            ),
            training_domain_ids=split_domain_ids["training"],
            validation_domain_ids=split_domain_ids["validation"],
            test_domain_ids=split_domain_ids["test"],
            training_evidence_sha256s=split_evidence_sha256s["training"],
            validation_evidence_sha256s=split_evidence_sha256s["validation"],
            test_evidence_sha256s=split_evidence_sha256s["test"],
            evidence_snapshots=snapshots,
            project_entry_ids=project_entry_ids,
            evidence_snapshot_set_sha256=snapshot_set_sha256,
            runtime_signature=signatures["runtime"][0],
            runtime_signature_sha256=signatures["runtime"][1],
            feature_signature=signatures["feature"][0],
            feature_signature_sha256=signatures["feature"][1],
            metrics=MappingProxyType(metrics),
            validation_status=status,
            training_defaults=_freeze_json(defaults),
            output_classes=output_classes,
            output_class=output_class,
            final_class=final_class,
            unattended_review_override=review_override,
        )
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_mapping(cls, value: object) -> "ClassifierDefinition":
        """Decode external fields and reference rows into one checked definition.

        Absent optional fitting settings remain empty; they are never populated
        from current authoring defaults. The constructor owns scientific checks
        and freezes the decoded values for subsequent payload/application use.
        """
        mapping = _mapping(value, "classifier")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(mapping) - allowed)
        if unknown:
            raise ClassifierCompatibilityError(
                "classifier has unknown field(s): " + ", ".join(unknown)
            )
        optional = {
            "training_defaults",
            "output_classes",
            "output_class",
            "final_class",
            "unattended_review_override",
        }
        missing = sorted(allowed - optional - set(mapping))
        if missing:
            raise ClassifierCompatibilityError(
                "classifier is missing field(s): " + ", ".join(missing)
            )
        values = dict(mapping)
        values.setdefault("training_defaults", {})
        values["evidence_snapshots"] = tuple(
            TrainingEvidenceSnapshot.from_mapping(item)
            for item in _sequence(mapping["evidence_snapshots"], "classifier.evidence_snapshots")
        )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ClassifierRegistry:
    """

    Keeps the available saved model definitions indexed by their classifier IDs.

    Attributes:
        classifiers: Mapping from a unique classifier ID to its checked
            ClassifierDefinition. The mapping is retained without subsequent
            item changes through ordinary assignment.
    """

    classifiers: Mapping[str, ClassifierDefinition]

    @classmethod
    def from_mapping(cls, value: object) -> "ClassifierRegistry":
        """

        Checks a list of saved model definitions and indexes them by classifier ID.

        Each model is read through ClassifierDefinition.from_mapping() before it is
        added. Repeated classifier IDs are rejected because a selected ID must resolve
        to one saved definition.

        Args:
            value: Dictionary containing one nonempty classifiers list.

        Returns:
            ClassifierRegistry containing the checked models.

        Raises:
            ClassifierCompatibilityError: Fields are invalid, the list is empty,
                a model cannot be read or classifier IDs repeat.
        """

        mapping = _mapping(value, "classifier registry")
        if set(mapping) != {"classifiers"}:
            raise ClassifierCompatibilityError("classifier registry must contain only classifiers")
        definitions = tuple(
            ClassifierDefinition.from_mapping(item)
            for item in _sequence(mapping["classifiers"], "classifier registry.classifiers")
        )
        if not definitions:
            raise ClassifierCompatibilityError("classifier registry.classifiers must not be empty")
        by_id = {definition.classifier_id: definition for definition in definitions}
        if len(by_id) != len(definitions):
            raise ClassifierCompatibilityError("classifier registry classifier IDs must be unique")
        return cls(classifiers=MappingProxyType(by_id))

    def get(self, classifier_id: str) -> ClassifierDefinition:
        """

        Retrieves the saved definition selected by a classifier ID.

        Args:
            classifier_id: Model identifier selected in the analysis settings.

        Returns:
            Matching ClassifierDefinition.

        Raises:
            ClassifierCompatibilityError: That model is absent from the available definitions.
        """

        try:
            return self.classifiers[classifier_id]
        except KeyError as error:
            raise ClassifierCompatibilityError(
                f"classifier {classifier_id!r} is not registered"
            ) from error




# ==============================================================================
#                Checking primary and secondary model application               
# ==============================================================================


def _validate_compatibility(
    configured: ClassifierSpec,
    definition: ClassifierDefinition,
    *,
    domain_id: str,
    pixel_scale_um: float,
    channel_override: Mapping[str, str] | None,
    unattended: bool,
    classifier_version: str,
) -> None:
    """

    Compares the saved model's input requirements with the current classification request.

    We require the requested model ID/version and processing group to
    match. The current physical input scale must satisfy:

        abs(saved_pixel_scale_um - current_pixel_scale_um) <= pixel_scale_tolerance_um

    The tolerance is an absolute difference in µm/pixel. For each required channel
    role, we resolve its saved ID through any explicit replacement and require it
    to match the current role's channel. A provisional or unevaluated model used
    without an interactive review also requires its explicit saved permission.

    Args:
        configured: ClassifierSpec selected by the analysis.
        definition: Saved ClassifierDefinition for that model ID.
        domain_id: Current validation-group ID.
        pixel_scale_um: Physical sampling scale selected for applying the classifier.
        channel_override: Optional explicit saved-channel-to-current-channel mapping.
        unattended: Whether processing proceeds without another interactive model review.
        classifier_version: Exact saved model version selected for this operation.

    Raises:
        ClassifierCompatibilityError: Model, processing group, physical scale or
            required review permission differs.
        ChannelMappingError: Roles differ or a saved input cannot be mapped to
            the current channel assigned to that role.
    """

    if configured.classifier_id != definition.classifier_id:
        raise ClassifierCompatibilityError(
            "configured classifier ID does not match its registry definition"
        )
    if definition.domain_id != domain_id:
        raise ClassifierCompatibilityError("classifier domain is incompatible")
    if classifier_version is None:
        raise ClassifierCompatibilityError("classifier version is required")
    if definition.version != _nonblank_string(classifier_version, "classifier_version"):
        raise ClassifierCompatibilityError("classifier version is incompatible")
    if (
        abs(definition.pixel_scale_um - _positive_number(pixel_scale_um, "pixel_scale_um"))
        > definition.pixel_scale_tolerance_um
    ):
        raise ClassifierCompatibilityError("classifier pixel scale is incompatible")
    if set(configured.channel_roles) != set(definition.channel_roles):
        raise ChannelMappingError("classifier channel roles must match exactly")
    resolved = resolve_channel_roles(
        definition.channel_roles,
        configured.channel_roles,
        override=channel_override,
    )
    if any(
        resolved[definition.channel_roles[role]] != configured.channel_roles[role]
        for role in definition.channel_roles
    ):
        raise ChannelMappingError("classifier role mapping must match exactly")
    if (
        unattended
        and definition.validation_status == "provisional"
        and not definition.unattended_review_override
    ):
        raise ClassifierCompatibilityError(
            "provisional classifier requires an explicit unattended-review override"
        )
    if (
        unattended
        and definition.validation_status == "not_evaluated"
        and not definition.unattended_review_override
    ):
        raise ClassifierCompatibilityError("Accept the unchecked classifier before unattended use.")


def validate_primary_classifier(
    classifier: ClassifierSpec,
    registry: ClassifierRegistry,
    *,
    primary_class_ids: Sequence[str],
    domain_id: str,
    pixel_scale_um: float,
    classifier_version: str,
    channel_override: Mapping[str, str] | None = None,
    unattended: bool = False,
) -> ClassifierDefinition:
    """

    Checks that the selected primary model supplies the biological classes and image inputs requested.

    We retrieve the selected model, require it to be primary, and compare its
    output class set with the configured biological classes and optional Ignore
    class. When the configuration also supplies an explicit output order, that
    order must match the saved model. We then check processing group, pixel scale,
    channel roles, model version and review permissions.

    Args:
        classifier: Primary ClassifierSpec selected by the analysis.
        registry: Available saved model definitions indexed by classifier ID.
        primary_class_ids: Two to four biological output classes.
        domain_id: Current validation-group ID.
        pixel_scale_um: Physical sampling scale for model application, in µm/pixel.
        classifier_version: Required saved model version.
        channel_override: Optional explicit replacement of saved channel IDs.
        unattended: Whether the model will run without another interactive review.

    Returns:
        Checked primary ClassifierDefinition.

    Raises:
        ClassifierCompatibilityError or ChannelMappingError: The model kind,
            outputs, inputs, physical scale or permissions fail their comparisons.
    """

    definition = registry.get(classifier.classifier_id)
    if definition.kind != "primary":
        raise ClassifierCompatibilityError("primary classifier must be registered as primary")
    declared_outputs = _validate_primary_output_inventory(
        primary_class_ids,
        definition.output_classes,
        require_ignore_training=classifier.require_ignore_training,
    )
    if classifier.output_classes is not None and declared_outputs != classifier.output_classes:
        raise ClassifierCompatibilityError(
            "primary classifier output classes do not match the configured output inventory"
        )
    _validate_compatibility(
        classifier,
        definition,
        domain_id=domain_id,
        pixel_scale_um=pixel_scale_um,
        channel_override=channel_override,
        unattended=unattended,
        classifier_version=classifier_version,
    )
    return definition


def _validate_primary_output_inventory(
    primary_class_ids: Sequence[str],
    output_classes: Sequence[str],
    *,
    require_ignore_training: bool,
) -> tuple[str, ...]:
    """

    Check one to four biological classes, with learned Ignore required for one.

    The biological list must be nonempty and distinct and cannot include Ignore
    as a biological class. The saved output classes must contain exactly those
    names, with Ignore added only as the optional ignored-pixel output. When
    Ignore training is explicitly required, its saved output must be present.
    A single biological class needs annotated Ignore as its second learned output.

    Args:
        primary_class_ids: Configured biological class names.
        output_classes: Unique saved output names in model order.
        require_ignore_training: Whether Ignore-labelled references are required.

    Returns:
        Saved output-class tuple in its original model order.

    Raises:
        ClassifierCompatibilityError: Class count, uniqueness, membership or
            the required Ignore output is invalid.
    """

    if not isinstance(require_ignore_training, bool):
        raise ClassifierCompatibilityError(
            "primary classifier require_ignore_training must be a boolean"
        )
    class_ids = _string_tuple(primary_class_ids, "primary_class_ids")
    minimum = 1 if require_ignore_training else MINIMUM_PRIMARY_CLASS_COUNT
    if (
        not minimum <= len(class_ids) <= MAXIMUM_PRIMARY_CLASS_COUNT
        or IGNORE_CLASS_ID in class_ids
    ):
        raise ClassifierCompatibilityError(
            "primary classifier requires one to four classes; a single class requires learned Ignore"
        )
    declared_outputs = _string_tuple(output_classes, "primary classifier output classes")
    biological_outputs = set(class_ids)
    if set(declared_outputs) not in (biological_outputs, biological_outputs | {IGNORE_CLASS_ID}):
        raise ClassifierCompatibilityError(
            "primary classifier output classes must be the configured biological "
            "classes with optional Ignore"
        )
    if require_ignore_training and IGNORE_CLASS_ID not in declared_outputs:
        raise ClassifierCompatibilityError(
            "primary classifier requires an Ignore output when Ignore training is required"
        )
    return declared_outputs


