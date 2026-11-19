"""Parse and validate the complete recorded analysis configuration.

Typed settings describe image inputs, tissue detection, analysis domains, classifiers,
segmentation, measurements, phenotyping and semantic criteria. Construction checks required
fields, units and cross-field compatibility before those values can enter a plan. Newly
authored presets are supplied separately; reopening an accepted configuration uses its
stored values."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import Enum
import hashlib
import json
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unicodedata import category as unicode_category

from .models import ROIAnalysisError
from .schema_registry import assert_current_writer

SCHEMA_VERSION = "6"
IGNORE_CLASS_ID = "Ignore"
ANALYSIS_DOMAIN_PARENT_ID = "AnalysisDomain"
MINIMUM_PRIMARY_CLASS_COUNT = 2
MAXIMUM_PRIMARY_CLASS_COUNT = 4
MAXIMUM_SECONDARY_STRUCTURE_COUNT = 8
from mif_pipeline._defaults import default_section

_REGISTRATION_QC_POLICY_DEFAULTS = default_section("registration.quality")
_TRANSFER_QC_POLICY_DEFAULTS = default_section("registration.transfer_quality")


# ==============================================================================
#                                     Config
# ==============================================================================


class ReviewPolicy(str, Enum):
    ALWAYS = "always"
    IF_UNVALIDATED = "if_unvalidated"
    IF_QC_FAILED = "if_qc_failed"
    UNATTENDED = "unattended"


class ClassifierMode(str, Enum):
    SAVED = "saved"
    TRAIN_NEW = "train_new"


class SecondaryExcessPolicy(str, Enum):
    MANUAL_SELECT = "manual_select"
    DETERMINISTIC_HASH_SAMPLE = "deterministic_hash_sample"


class NativeLevelSelectionMode(str, Enum):
    """Authority used to select the native QuPath tissue level."""

    PROMPT = "prompt"
    CONFIGURED_INDEX = "configured_index"
    AUTOMATIC_TARGET = "automatic_target"


PHENOTYPING_MODE_POLICY_SCHEMA_VERSION = "phenotyping-mode-policy/v1"


class PhenotypingMode(str, Enum):
    """Explicit scientific method selected for a phenotyping generation."""

    MARKER_RULES = "marker_rules"
    SUPERVISED_CLASSIFIER = "supervised_classifier"
    UNSUPERVISED_CLUSTERING = "unsupervised_clustering"


class ClusteringMode(str, Enum):
    """Explicit participant scope for unsupervised clustering."""

    PER_SLIDE = "per_slide"
    POOLED = "pooled"


@dataclass(frozen=True, slots=True)
class PhenotypeClassSpec:
    """One user-declared phenotype class in deterministic precedence order."""

    class_id: str
    display_name: str
    precedence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "class_id", _string(self.class_id, "phenotype class_id"))
        object.__setattr__(
            self, "display_name", _string(self.display_name, "phenotype display_name")
        )
        _integer(self.precedence, "phenotype precedence", nonnegative=True)

    def to_mapping(self) -> dict[str, object]:
        return {
            "class_id": self.class_id,
            "display_name": self.display_name,
            "precedence": self.precedence,
        }


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ROIAnalysisError(f"{field} must be a string-keyed mapping")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError(f"{field} must be a sequence")
    return value


def _string(value: object, field: str, *, preserve: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value if preserve else value.strip()


def validate_tissue_classifier_name(value: object) -> str:
    """Return one portable project classifier basename without changing identity."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.endswith(".json")
        or not value[:-5]
    ):
        raise ROIAnalysisError("classifier_name must be a nonempty .json basename")
    if any(char in '<>:"/\\|?*' or unicode_category(char) == "Cc" for char in value):
        raise ROIAnalysisError("classifier_name contains an unsafe character")
    first_stem = value[:-5].split(".", 1)[0].rstrip(" ").upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    if first_stem in reserved:
        raise ROIAnalysisError("classifier_name uses a reserved Windows basename")
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _path(value: object, field: str) -> Path:
    return Path(_string(value, field)).expanduser().resolve(strict=False)


def _optional_path(value: object, field: str) -> Path | None:
    return None if value is None else _path(value, field)


def _optional_artifact_path(value: object, field: str) -> Path | None:
    """Preserve project-relative classifier paths for output-dir resolution."""
    if value is None:
        return None
    return Path(_string(value, field)).expanduser()


def _number(
    value: object, field: str, *, positive: bool = False, nonnegative: bool = False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ROIAnalysisError(f"{field} must be a number")
    result = float(value)
    if not isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ROIAnalysisError(f"{field} must be a {qualifier} number")
    return result


def _integer(
    value: object, field: str, *, positive: bool = False, nonnegative: bool = False
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ROIAnalysisError(f"{field} must be an integer")
    if (positive and value <= 0) or (nonnegative and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ROIAnalysisError(f"{field} must be a {qualifier} integer")
    return value


def _enum(enum_type: type[Enum], value: object, field: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ROIAnalysisError(f"{field} must be one of: {allowed}") from exc


def _reject_unknown(mapping: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ROIAnalysisError(f"{field} has unknown field(s): {', '.join(unknown)}")


def _freeze_json(value: object, field: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError(f"{field} mapping keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item, field) for item in value)
    raise ROIAnalysisError(f"{field} must contain only JSON-safe values")


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _readonly_strings(
    value: Mapping[str, object], field: str, *, require: bool = True
) -> Mapping[str, str]:
    if require and not value:
        raise ROIAnalysisError(f"{field} must not be empty")
    return MappingProxyType(
        {
            _string(key, f"{field} role", preserve=True): _string(
                item, f"{field}[{key!r}]", preserve=True
            )
            for key, item in value.items()
        }
    )


def _sha256(value: object, field: str) -> str:
    digest = _string(value, field, preserve=True)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256")
    return digest


@dataclass(frozen=True, slots=True)
class StructureCountBounds:
    minimum: int = 0
    maximum: int | None = None
    excess_policy: SecondaryExcessPolicy | None = None
    sampling_seed: int | None = None

    def __post_init__(self) -> None:
        minimum = _integer(self.minimum, "count bounds minimum", nonnegative=True)
        maximum = (
            None
            if self.maximum is None
            else _integer(self.maximum, "count bounds maximum", positive=True)
        )
        if maximum is not None and maximum < minimum:
            raise ROIAnalysisError("count bounds maximum must be greater than or equal to minimum")
        policy = self.excess_policy
        if isinstance(policy, str):
            policy = _enum(SecondaryExcessPolicy, policy, "excess_structure_policy")
        if policy is not None and not isinstance(policy, SecondaryExcessPolicy):
            raise ROIAnalysisError("excess_structure_policy is invalid")
        seed = self.sampling_seed
        if seed is not None:
            seed = _integer(seed, "sampling_seed")
        if maximum is None and (policy is not None or seed is not None):
            raise ROIAnalysisError(
                "a null maximum requires null excess_structure_policy and sampling_seed"
            )
        if maximum is not None and policy is None and seed is not None:
            raise ROIAnalysisError("sampling_seed requires excess_structure_policy")
        if policy is SecondaryExcessPolicy.DETERMINISTIC_HASH_SAMPLE and seed is None:
            raise ROIAnalysisError("deterministic_hash_sample requires sampling_seed")
        if policy is SecondaryExcessPolicy.MANUAL_SELECT and seed is not None:
            raise ROIAnalysisError("manual_select requires sampling_seed=null")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "excess_policy", policy)
        object.__setattr__(self, "sampling_seed", seed)

    @classmethod
    def from_mapping(cls, value: object, *, field: str, secondary: bool) -> "StructureCountBounds":
        mapping = _mapping(value, field)
        allowed = {"minimum", "maximum"}
        if secondary:
            allowed |= {"excess_structure_policy", "sampling_seed"}
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every count-policy field")
        maximum = mapping["maximum"]
        if not secondary:
            minimum = _integer(mapping["minimum"], f"{field}.minimum", nonnegative=True)
            parsed_maximum = (
                None if maximum is None else _integer(maximum, f"{field}.maximum", positive=True)
            )
            if parsed_maximum is not None and parsed_maximum < minimum:
                raise ROIAnalysisError(f"{field}.maximum must be greater than or equal to minimum")
            return cls(minimum=minimum, maximum=parsed_maximum)
        parsed = cls(
            minimum=mapping["minimum"],
            maximum=maximum,
            excess_policy=mapping["excess_structure_policy"],
            sampling_seed=mapping["sampling_seed"],
        )
        if parsed.maximum is not None and parsed.excess_policy is None:
            raise ROIAnalysisError("a finite secondary maximum requires excess_structure_policy")
        return parsed

    def to_mapping(self, *, secondary: bool) -> dict[str, object]:
        result: dict[str, object] = {
            "minimum": self.minimum,
            "maximum": self.maximum,
        }
        if secondary:
            result.update(
                {
                    "excess_structure_policy": (
                        None if self.excess_policy is None else self.excess_policy.value
                    ),
                    "sampling_seed": self.sampling_seed,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class SemanticProfileReference:
    profile_id: str
    profile_version: str
    content_sha256: str
    artifact_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _string(self.profile_id, "semantic profile_id"))
        object.__setattr__(
            self, "profile_version", _string(self.profile_version, "semantic profile_version")
        )
        if self.profile_version not in {"3", "4"}:
            raise ROIAnalysisError("semantic profile_version must equal '3' or '4'")
        object.__setattr__(
            self, "content_sha256", _sha256(self.content_sha256, "semantic content_sha256")
        )
        if not isinstance(self.artifact_path, Path):
            object.__setattr__(
                self,
                "artifact_path",
                _optional_artifact_path(self.artifact_path, "semantic artifact_path"),
            )
        if self.artifact_path is None:
            raise ROIAnalysisError("semantic artifact_path must be explicit")

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "SemanticProfileReference":
        mapping = _mapping(value, field)
        allowed = {"profile_id", "profile_version", "content_sha256", "artifact_path"}
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every field")
        path = _optional_artifact_path(mapping["artifact_path"], f"{field}.artifact_path")
        if path is None:
            raise ROIAnalysisError(f"{field}.artifact_path must be explicit")
        return cls(
            profile_id=_string(mapping["profile_id"], f"{field}.profile_id"),
            profile_version=_string(mapping["profile_version"], f"{field}.profile_version"),
            content_sha256=_sha256(mapping["content_sha256"], f"{field}.content_sha256"),
            artifact_path=path,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "content_sha256": self.content_sha256,
            "artifact_path": str(self.artifact_path),
        }


@dataclass(frozen=True, slots=True)
class AnalysisDomainConfig:
    selection_policy: str
    stage_version: str
    review_policy: ReviewPolicy
    saved_selection_path: Path | None

    def __post_init__(self) -> None:
        policy = _string(self.selection_policy, "analysis_domain.selection_policy")
        if policy not in {"whole_tissue", "prompt", "selected_rois"}:
            raise ROIAnalysisError(
                "analysis_domain.selection_policy must be whole_tissue, prompt, or selected_rois"
            )
        stage_version = _string(self.stage_version, "analysis_domain.stage_version")
        if not isinstance(self.review_policy, ReviewPolicy):
            raise ROIAnalysisError("analysis_domain.review_policy must be a ReviewPolicy")
        saved = self.saved_selection_path
        if saved is not None and not isinstance(saved, Path):
            saved = _optional_artifact_path(saved, "analysis_domain.saved_selection_path")
        if (policy == "selected_rois") != (saved is not None):
            raise ROIAnalysisError(
                "analysis_domain.saved_selection_path is required only for selected_rois"
            )
        object.__setattr__(self, "selection_policy", policy)
        object.__setattr__(self, "stage_version", stage_version)
        object.__setattr__(self, "saved_selection_path", saved)

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "AnalysisDomainConfig":
        mapping = _mapping(value, field)
        allowed = {"selection_policy", "stage_version", "review_policy", "saved_selection_path"}
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every field")
        policy = _string(mapping["selection_policy"], f"{field}.selection_policy")
        if policy not in {"whole_tissue", "prompt", "selected_rois"}:
            raise ROIAnalysisError(
                f"{field}.selection_policy must be whole_tissue, prompt, or selected_rois"
            )
        saved = _optional_artifact_path(
            mapping["saved_selection_path"], f"{field}.saved_selection_path"
        )
        if (policy == "selected_rois") != (saved is not None):
            raise ROIAnalysisError(
                f"{field}.saved_selection_path is required only for selected_rois"
            )
        return cls(
            selection_policy=policy,
            stage_version=_string(mapping["stage_version"], f"{field}.stage_version"),
            review_policy=_enum(ReviewPolicy, mapping["review_policy"], f"{field}.review_policy"),
            saved_selection_path=saved,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "selection_policy": self.selection_policy,
            "stage_version": self.stage_version,
            "review_policy": self.review_policy.value,
            "saved_selection_path": (
                None if self.saved_selection_path is None else str(self.saved_selection_path)
            ),
        }


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    channel_id: str
    name: str | None
    component: str | None
    fluorophore: str | None
    biomarker: str | None

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "ChannelSpec":
        mapping = _mapping(value, field)
        _reject_unknown(
            mapping, {"channel_id", "name", "component", "fluorophore", "biomarker"}, field
        )
        return cls(
            channel_id=_string(mapping.get("channel_id"), f"{field}.channel_id", preserve=True),
            name=_optional_string(mapping.get("name"), f"{field}.name"),
            component=_optional_string(mapping.get("component"), f"{field}.component"),
            fluorophore=_optional_string(mapping.get("fluorophore"), f"{field}.fluorophore"),
            biomarker=_optional_string(mapping.get("biomarker"), f"{field}.biomarker"),
        )

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "component": self.component,
            "fluorophore": self.fluorophore,
            "biomarker": self.biomarker,
        }


@dataclass(frozen=True, slots=True)
class PrimaryClassSpec:
    class_id: str
    display_name: str

    @classmethod
    def from_value(cls, value: object, *, field: str) -> "PrimaryClassSpec":
        if isinstance(value, str):
            class_id = _string(value, field)
            return cls(class_id, class_id)
        mapping = _mapping(value, field)
        _reject_unknown(mapping, {"class_id", "display_name"}, field)
        return cls(
            _string(mapping.get("class_id"), f"{field}.class_id"),
            _string(mapping.get("display_name"), f"{field}.display_name"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"class_id": self.class_id, "display_name": self.display_name}


@dataclass(frozen=True, slots=True)
class ClassifierSpec:
    """Explicit choice to train a QuPath classifier or apply a saved model."""

    classifier_id: str
    channel_roles: Mapping[str, str]
    mode: ClassifierMode
    artifact_path: Path | None = None
    classifier_version: str | None = None
    validation_domain_id: str | None = None
    classifier_sha256: str | None = None
    validation_status: str | None = None
    validation_record_id: str | None = None
    qc_passed: bool | None = None
    qc_record_id: str | None = None
    review_policy: ReviewPolicy = ReviewPolicy.ALWAYS
    training_regions_per_label: int = 5
    require_ignore_training: bool = False
    output_classes: tuple[str, ...] | None = None
    count_bounds: StructureCountBounds = field(default_factory=StructureCountBounds)
    class_count_bounds: Mapping[str, StructureCountBounds] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the ClassifierSpec fields when the record is constructed.

        The checks cover ``classifier_id``, ``mode``, ``review_policy``,
        ``training_regions_per_label``, ``output_classes``, ``artifact_path``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _string(self.classifier_id, "classifier_id", preserve=True)
        object.__setattr__(
            self,
            "channel_roles",
            _readonly_strings(_mapping(self.channel_roles, "channel_roles"), "channel_roles", require=False),
        )
        object.__setattr__(self, "mode", _enum(ClassifierMode, self.mode, "classifier.mode"))
        if isinstance(self.review_policy, str):
            object.__setattr__(
                self,
                "review_policy",
                _enum(ReviewPolicy, self.review_policy, "classifier.review_policy"),
            )
        if self.mode is ClassifierMode.SAVED:
            if not self.channel_roles:
                raise ROIAnalysisError("saved classifier requires selected channel roles")
            if self.artifact_path is None:
                raise ROIAnalysisError("saved classifier requires artifact_path")
            if self.classifier_version is None:
                raise ROIAnalysisError("saved classifier requires classifier_version")
        elif self.mode is ClassifierMode.TRAIN_NEW:
            if self.validation_domain_id is None:
                raise ROIAnalysisError("train_new classifier requires validation_domain_id")
        _integer(
            self.training_regions_per_label,
            "classifier.training_regions_per_label",
            positive=True,
        )
        if not isinstance(self.require_ignore_training, bool):
            raise ROIAnalysisError("classifier.require_ignore_training must be a boolean")
        if self.output_classes is not None:
            output_classes = tuple(
                _string(item, "classifier.output_classes")
                for item in _sequence(self.output_classes, "classifier.output_classes")
            )
            if not output_classes or len(set(output_classes)) != len(output_classes):
                raise ROIAnalysisError(
                    "classifier.output_classes must be a non-empty unique inventory"
                )
            object.__setattr__(self, "output_classes", output_classes)
        if not isinstance(self.count_bounds, StructureCountBounds):
            raise ROIAnalysisError("classifier.count_bounds is invalid")
        parsed_class_bounds: dict[str, StructureCountBounds] = {}
        for class_id, bounds in _mapping(
            self.class_count_bounds, "classifier.class_count_bounds"
        ).items():
            normalized_id = _string(class_id, "classifier class_count_bounds key")
            if not isinstance(bounds, StructureCountBounds):
                raise ROIAnalysisError("classifier.class_count_bounds values are invalid")
            parsed_class_bounds[normalized_id] = bounds
        object.__setattr__(self, "class_count_bounds", MappingProxyType(parsed_class_bounds))
        if self.classifier_sha256 is not None and (
            len(self.classifier_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.classifier_sha256)
        ):
            raise ROIAnalysisError("classifier.classifier_sha256 must be a lowercase SHA-256")
        if self.validation_status is not None and self.validation_status not in {
            "validated",
            "provisional",
        }:
            raise ROIAnalysisError("classifier.validation_status must be validated or provisional")
        if self.qc_passed is not None and not isinstance(self.qc_passed, bool):
            raise ROIAnalysisError("classifier.qc_passed must be a boolean or null")
        if self.review_policy is ReviewPolicy.UNATTENDED:
            if (
                self.artifact_path is None
                or self.classifier_version is None
                or self.validation_domain_id is None
                or self.classifier_sha256 is None
                or self.validation_status != "validated"
                or self.validation_record_id is None
                or self.qc_passed is not True
                or self.qc_record_id is None
            ):
                raise ROIAnalysisError(
                    "unattended classifier requires pinned artifact, validation, and QC evidence"
                )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        field: str,
        include_count_policy: bool = False,
        include_primary_output_inventory: bool = False,
    ) -> "ClassifierSpec":
        """Decode the serialized fields into a validated ClassifierSpec record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _mapping(value, field)
        allowed = {
            "classifier_id",
            "mode",
            "channel_roles",
            "artifact_path",
            "classifier_version",
            "validation_domain_id",
            "review_policy",
            "training_regions_per_label",
            "require_ignore_training",
            "classifier_sha256",
            "validation_status",
            "validation_record_id",
            "qc_passed",
            "qc_record_id",
        }
        if include_count_policy:
            allowed |= {"count_bounds", "class_count_bounds"}
        if include_primary_output_inventory:
            allowed.add("output_classes")
        _reject_unknown(mapping, allowed, field)
        optional = {
            "training_regions_per_label",
            "require_ignore_training",
            "classifier_sha256",
            "validation_status",
            "validation_record_id",
            "qc_passed",
            "qc_record_id",
        }
        if include_primary_output_inventory:
            optional.add("output_classes")
        missing = (allowed - optional) - set(mapping)
        if missing:
            raise ROIAnalysisError(
                f"{field} requires explicit field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            classifier_id=_string(
                mapping.get("classifier_id"), f"{field}.classifier_id", preserve=True
            ),
            channel_roles=_readonly_strings(
                _mapping(mapping.get("channel_roles"), f"{field}.channel_roles"),
                f"{field}.channel_roles", require=False,
            ),
            mode=_enum(ClassifierMode, mapping.get("mode"), f"{field}.mode"),
            artifact_path=_optional_artifact_path(
                mapping.get("artifact_path"), f"{field}.artifact_path"
            ),
            classifier_version=_optional_string(
                mapping.get("classifier_version"), f"{field}.classifier_version"
            ),
            validation_domain_id=_optional_string(
                mapping.get("validation_domain_id"), f"{field}.validation_domain_id"
            ),
            classifier_sha256=_optional_string(
                mapping.get("classifier_sha256"), f"{field}.classifier_sha256"
            ),
            validation_status=_optional_string(
                mapping.get("validation_status"), f"{field}.validation_status"
            ),
            validation_record_id=_optional_string(
                mapping.get("validation_record_id"), f"{field}.validation_record_id"
            ),
            qc_passed=mapping.get("qc_passed"),
            qc_record_id=_optional_string(mapping.get("qc_record_id"), f"{field}.qc_record_id"),
            review_policy=_enum(
                ReviewPolicy, mapping.get("review_policy"), f"{field}.review_policy"
            ),
            training_regions_per_label=_integer(
                mapping.get("training_regions_per_label", 5),
                f"{field}.training_regions_per_label",
                positive=True,
            ),
            require_ignore_training=mapping.get("require_ignore_training", False),
            output_classes=(
                None
                if mapping.get("output_classes") is None
                else tuple(
                    _string(item, f"{field}.output_classes")
                    for item in _sequence(
                        mapping.get("output_classes"),
                        f"{field}.output_classes",
                    )
                )
            ),
            count_bounds=(
                StructureCountBounds.from_mapping(
                    mapping["count_bounds"], field=f"{field}.count_bounds", secondary=False
                )
                if include_count_policy
                else StructureCountBounds()
            ),
            class_count_bounds=(
                MappingProxyType(
                    {
                        _string(
                            class_id, f"{field}.class_count_bounds key"
                        ): StructureCountBounds.from_mapping(
                            bounds,
                            field=f"{field}.class_count_bounds[{class_id!r}]",
                            secondary=False,
                        )
                        for class_id, bounds in _mapping(
                            mapping["class_count_bounds"], f"{field}.class_count_bounds"
                        ).items()
                    }
                )
                if include_count_policy
                else MappingProxyType({})
            ),
        )

    def to_mapping(
        self,
        *,
        include_count_policy: bool = False,
        include_primary_output_inventory: bool = False,
    ) -> dict[str, object]:
        result = {
            "classifier_id": self.classifier_id,
            "mode": self.mode.value,
            "channel_roles": dict(self.channel_roles),
            "artifact_path": None if self.artifact_path is None else str(self.artifact_path),
            "classifier_version": self.classifier_version,
            "validation_domain_id": self.validation_domain_id,
            "classifier_sha256": self.classifier_sha256,
            "validation_status": self.validation_status,
            "validation_record_id": self.validation_record_id,
            "qc_passed": self.qc_passed,
            "qc_record_id": self.qc_record_id,
            "review_policy": self.review_policy.value,
            "training_regions_per_label": self.training_regions_per_label,
            "require_ignore_training": self.require_ignore_training,
        }
        if include_primary_output_inventory:
            result["output_classes"] = (
                None if self.output_classes is None else list(self.output_classes)
            )
        if include_count_policy:
            result.update(
                {
                    "count_bounds": self.count_bounds.to_mapping(secondary=False),
                    "class_count_bounds": {
                        class_id: bounds.to_mapping(secondary=False)
                        for class_id, bounds in self.class_count_bounds.items()
                    },
                }
            )
        return result


@dataclass(frozen=True, slots=True, init=False)
class SecondaryStructureSpec:
    structure_id: str
    display_name: str
    eligible_parent_ids: tuple[str, ...]
    classifier: ClassifierSpec
    mutually_exclusive: bool
    count_bounds: StructureCountBounds
    semantic_profile: SemanticProfileReference
    minimum_candidate_area_um2: float

    def __init__(
        self,
        structure_id: str,
        display_name: str,
        eligible_parent_ids: Sequence[str] | None = None,
        classifier: ClassifierSpec | None = None,
        mutually_exclusive: bool = False,
        count_bounds: StructureCountBounds | None = None,
        semantic_profile: SemanticProfileReference | None = None,
        *,
        eligible_primary_class_ids: Sequence[str] | None = None,
        minimum_candidate_area_um2: float = 0.0,
    ) -> None:
        parents = (
            eligible_parent_ids if eligible_parent_ids is not None else eligible_primary_class_ids
        )
        if parents is None:
            raise ROIAnalysisError("eligible_parent_ids must be explicit")
        if classifier is None:
            raise ROIAnalysisError("classifier must be explicit")
        object.__setattr__(self, "structure_id", _string(structure_id, "structure_id"))
        object.__setattr__(self, "display_name", _string(display_name, "display_name"))
        object.__setattr__(
            self,
            "eligible_parent_ids",
            tuple(_string(item, "eligible_parent_ids") for item in parents),
        )
        object.__setattr__(self, "classifier", classifier)
        object.__setattr__(self, "mutually_exclusive", mutually_exclusive)
        if count_bounds is None:
            raise ROIAnalysisError("secondary count_bounds must be explicit")
        if semantic_profile is None:
            raise ROIAnalysisError("secondary semantic_profile must be explicit")
        object.__setattr__(self, "count_bounds", count_bounds)
        object.__setattr__(self, "semantic_profile", semantic_profile)
        object.__setattr__(self, "minimum_candidate_area_um2", _number(
            minimum_candidate_area_um2, "minimum_candidate_area_um2", nonnegative=True))
        if not self.eligible_parent_ids:
            raise ROIAnalysisError("eligible_parent_ids must not be empty")
        if not isinstance(mutually_exclusive, bool):
            raise ROIAnalysisError("mutually_exclusive must be a boolean")

    @property
    def eligible_primary_class_ids(self) -> tuple[str, ...]:
        """Compatibility view for classifier validation callers."""
        return tuple(
            parent for parent in self.eligible_parent_ids if parent != ANALYSIS_DOMAIN_PARENT_ID
        )

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "SecondaryStructureSpec":
        mapping = _mapping(value, field)
        _reject_unknown(
            mapping,
            {
                "structure_id",
                "display_name",
                "eligible_parent_ids",
                "classifier",
                "mutually_exclusive",
                "count_bounds",
                "semantic_profile",
                "minimum_candidate_area_um2",
            },
            field,
        )
        return cls(
            structure_id=_string(mapping.get("structure_id"), f"{field}.structure_id"),
            display_name=_string(mapping.get("display_name"), f"{field}.display_name"),
            eligible_parent_ids=tuple(
                _string(item, f"{field}.eligible_parent_ids")
                for item in _sequence(
                    mapping.get("eligible_parent_ids"), f"{field}.eligible_parent_ids"
                )
            ),
            classifier=ClassifierSpec.from_mapping(
                mapping.get("classifier"), field=f"{field}.classifier", include_primary_output_inventory=True
            ),
            mutually_exclusive=mapping.get("mutually_exclusive", False),
            count_bounds=StructureCountBounds.from_mapping(
                mapping.get("count_bounds"), field=f"{field}.count_bounds", secondary=True
            ),
            semantic_profile=SemanticProfileReference.from_mapping(
                mapping.get("semantic_profile"), field=f"{field}.semantic_profile"
            ),
            minimum_candidate_area_um2=mapping.get("minimum_candidate_area_um2", 0.0),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "structure_id": self.structure_id,
            "display_name": self.display_name,
            "eligible_parent_ids": list(self.eligible_parent_ids),
            "classifier": self.classifier.to_mapping(include_primary_output_inventory=True),
            "mutually_exclusive": self.mutually_exclusive,
            "count_bounds": self.count_bounds.to_mapping(secondary=True),
            "semantic_profile": self.semantic_profile.to_mapping(),
            **({"minimum_candidate_area_um2": self.minimum_candidate_area_um2}
               if self.minimum_candidate_area_um2 else {}),
        }


def secondary_classifier_output_classes(structure_ids: Sequence[str], *, joint: bool) -> tuple[str, ...]:
    """Canonical annotation/model labels for one independent or joint detector."""
    ids = tuple(_string(item, "secondary structure ID") for item in structure_ids)
    if not ids or len(set(ids)) != len(ids) or (not joint and len(ids) != 1):
        raise ROIAnalysisError("Choose distinct structures for the secondary classifier.")
    return tuple(f"Candidate_{item}" for item in ids) + (
        ("Other_tissue",) if joint else (f"Other_{ids[0]}",))


def secondary_classifier_groups(structures: Sequence[SecondaryStructureSpec]) -> dict[str, tuple[SecondaryStructureSpec, ...]]:
    """Validate shared model settings while retaining each structure's own review/count settings."""
    groups: dict[str, list[SecondaryStructureSpec]] = {}
    for structure in structures:
        groups.setdefault(structure.classifier.classifier_id, []).append(structure)
    result = {}
    for classifier_id, members in groups.items():
        first = members[0]
        if any(item.classifier != first.classifier
               or set(item.eligible_parent_ids) != set(first.eligible_parent_ids) for item in members):
            raise ROIAnalysisError("Structures trained together require the same classifier settings and detection area.")
        required = {f"Candidate_{item.structure_id}" for item in members}
        outputs = first.classifier.output_classes
        if outputs is None:
            if len(members) > 1:
                raise ROIAnalysisError("A shared secondary classifier requires its target output labels.")
        elif not required <= set(outputs) or {item for item in outputs if item.startswith("Candidate_")} != required:
            raise ROIAnalysisError("Secondary classifier output classes must include every selected structure.")
        result[classifier_id] = tuple(members)
    return result


@dataclass(frozen=True, slots=True)
class TissueMatchingConfig:
    min_iou: float
    max_centroid_displacement_um: float
    max_relative_area_change: float
    min_uniqueness_margin: float

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "TissueMatchingConfig":
        mapping = _mapping(value, field)
        allowed = {
            "min_iou",
            "max_centroid_displacement_um",
            "max_relative_area_change",
            "min_uniqueness_margin",
        }
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every matching threshold")
        min_iou = _number(mapping["min_iou"], f"{field}.min_iou")
        margin = _number(mapping["min_uniqueness_margin"], f"{field}.min_uniqueness_margin")
        if not 0 <= min_iou <= 1 or not 0 <= margin <= 1:
            raise ROIAnalysisError(f"{field} overlap thresholds must be within [0, 1]")
        return cls(
            min_iou,
            _number(
                mapping["max_centroid_displacement_um"],
                f"{field}.max_centroid_displacement_um",
                nonnegative=True,
            ),
            _number(
                mapping["max_relative_area_change"],
                f"{field}.max_relative_area_change",
                nonnegative=True,
            ),
            margin,
        )

    def to_mapping(self) -> dict[str, float]:
        return {
            "min_iou": self.min_iou,
            "max_centroid_displacement_um": self.max_centroid_displacement_um,
            "max_relative_area_change": self.max_relative_area_change,
            "min_uniqueness_margin": self.min_uniqueness_margin,
        }


@dataclass(frozen=True, slots=True)
class TissueDetectionParameters:
    """Explicit unresolved authoring parameters, never execution authority."""

    detector_id: str
    detector_version: str
    classifier_name: str
    reference_channel_id: str | None
    reference_channel_selection_source: str | None
    native_level_selection_mode: NativeLevelSelectionMode
    native_resolution_index: int | None
    resolution_um_per_px: float
    native_level_match_tolerance_fraction: float
    allow_level0_selection: bool
    minimum_object_area_um2: float
    review_policy: ReviewPolicy
    validation_domain_id: str | None
    stage_version: str
    matching: TissueMatchingConfig

    def __post_init__(self) -> None:
        """Validate the TissueDetectionParameters fields when the record is constructed.

        The checks cover ``classifier_name``, ``resolution_um_per_px``,
        ``native_level_match_tolerance_fraction``, ``minimum_object_area_um2``,
        ``reference_channel_id``, ``native_level_selection_mode``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        validate_tissue_classifier_name(self.classifier_name)
        _number(
            self.resolution_um_per_px,
            "tissue_detection.resolution_um_per_px",
            positive=True,
        )
        _number(
            self.native_level_match_tolerance_fraction,
            "tissue_detection.native_level_match_tolerance_fraction",
            nonnegative=True,
        )
        _number(
            self.minimum_object_area_um2,
            "tissue_detection.minimum_object_area_um2",
            positive=True,
        )
        sources = {"canonical_metadata", "user_override"}
        if self.reference_channel_id is None:
            if self.reference_channel_selection_source is not None:
                raise ROIAnalysisError(
                    "reference_channel_selection_source requires reference_channel_id"
                )
        elif self.reference_channel_selection_source not in sources:
            raise ROIAnalysisError(
                "reference_channel_selection_source must be canonical_metadata or user_override"
            )
        if not 0 <= self.native_level_match_tolerance_fraction < 1:
            raise ROIAnalysisError(
                "tissue_detection.native_level_match_tolerance_fraction must be within [0, 1)"
            )
        if not isinstance(self.native_level_selection_mode, NativeLevelSelectionMode):
            raise ROIAnalysisError("tissue_detection.native_level_selection_mode is invalid")
        if self.native_level_selection_mode is NativeLevelSelectionMode.CONFIGURED_INDEX:
            _integer(
                self.native_resolution_index,
                "tissue_detection.native_resolution_index",
                nonnegative=True,
            )
        elif self.native_resolution_index is not None:
            raise ROIAnalysisError(
                "tissue_detection.native_resolution_index is permitted only for configured_index"
            )
        if not isinstance(self.allow_level0_selection, bool):
            raise ROIAnalysisError("tissue_detection.allow_level0_selection must be a boolean")

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "TissueDetectionParameters":
        """Decode the serialized fields into a validated TissueDetectionParameters record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _mapping(value, field)
        allowed = {
            "detector_id",
            "detector_version",
            "classifier_name",
            "reference_channel_id",
            "reference_channel_selection_source",
            "native_level_selection_mode",
            "native_resolution_index",
            "resolution_um_per_px",
            "native_level_match_tolerance_fraction",
            "allow_level0_selection",
            "minimum_object_area_um2",
            "review_policy",
            "validation_domain_id",
            "stage_version",
            "matching",
        }
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every field")
        return cls(
            detector_id=_string(mapping["detector_id"], f"{field}.detector_id"),
            detector_version=_string(mapping["detector_version"], f"{field}.detector_version"),
            classifier_name=validate_tissue_classifier_name(mapping["classifier_name"]),
            reference_channel_id=_optional_string(
                mapping["reference_channel_id"], f"{field}.reference_channel_id"
            ),
            reference_channel_selection_source=_optional_string(
                mapping["reference_channel_selection_source"],
                f"{field}.reference_channel_selection_source",
            ),
            native_level_selection_mode=_enum(
                NativeLevelSelectionMode,
                mapping["native_level_selection_mode"],
                f"{field}.native_level_selection_mode",
            ),
            native_resolution_index=(
                None
                if mapping["native_resolution_index"] is None
                else _integer(
                    mapping["native_resolution_index"],
                    f"{field}.native_resolution_index",
                    nonnegative=True,
                )
            ),
            resolution_um_per_px=_number(
                mapping["resolution_um_per_px"], f"{field}.resolution_um_per_px", positive=True
            ),
            native_level_match_tolerance_fraction=_number(
                mapping["native_level_match_tolerance_fraction"],
                f"{field}.native_level_match_tolerance_fraction",
                nonnegative=True,
            ),
            allow_level0_selection=mapping["allow_level0_selection"],
            minimum_object_area_um2=_number(
                mapping["minimum_object_area_um2"],
                f"{field}.minimum_object_area_um2",
                positive=True,
            ),
            review_policy=_enum(ReviewPolicy, mapping["review_policy"], f"{field}.review_policy"),
            validation_domain_id=_optional_string(
                mapping["validation_domain_id"], f"{field}.validation_domain_id"
            ),
            stage_version=_string(mapping["stage_version"], f"{field}.stage_version"),
            matching=TissueMatchingConfig.from_mapping(
                mapping["matching"], field=f"{field}.matching"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "classifier_name": self.classifier_name,
            "reference_channel_id": self.reference_channel_id,
            "reference_channel_selection_source": self.reference_channel_selection_source,
            "native_level_selection_mode": self.native_level_selection_mode.value,
            "native_resolution_index": self.native_resolution_index,
            "resolution_um_per_px": self.resolution_um_per_px,
            "native_level_match_tolerance_fraction": self.native_level_match_tolerance_fraction,
            "allow_level0_selection": self.allow_level0_selection,
            "minimum_object_area_um2": self.minimum_object_area_um2,
            "review_policy": self.review_policy.value,
            "validation_domain_id": self.validation_domain_id,
            "stage_version": self.stage_version,
            "matching": self.matching.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class TissueDetectionConfig(TissueDetectionParameters):
    classifier_sha256: str

    def __post_init__(self) -> None:
        TissueDetectionParameters.__post_init__(self)
        if (
            type(self.classifier_sha256) is not str
            or len(self.classifier_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.classifier_sha256)
        ):
            raise ROIAnalysisError("tissue_detection.classifier_sha256 must be a lowercase SHA-256")

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "TissueDetectionConfig":
        mapping = _mapping(value, field)
        if "classifier_sha256" not in mapping:
            raise ROIAnalysisError(f"{field} requires every field")
        parameters = TissueDetectionParameters.from_mapping(
            {key: item for key, item in mapping.items() if key != "classifier_sha256"},
            field=field,
        )
        return cls(
            **{
                item.name: getattr(parameters, item.name)
                for item in fields(TissueDetectionParameters)
            },
            classifier_sha256=_string(mapping["classifier_sha256"], f"{field}.classifier_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **TissueDetectionParameters.to_mapping(self),
            "classifier_sha256": self.classifier_sha256,
        }

    @property
    def settings_hash(self) -> str:
        serialized = json.dumps(
            self.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

def _numeric_mapping(value: object, field: str) -> Mapping[str, float]:
    mapping = _mapping(value, field)
    return MappingProxyType(
        {
            _string(key, f"{field} key"): _number(item, f"{field}.{key}", nonnegative=True)
            for key, item in mapping.items()
        }
    )


def _registration_qc_policy(value: object, field: str) -> Mapping[str, object]:
    mapping = _mapping(value, field)
    expected = set(_REGISTRATION_QC_POLICY_DEFAULTS)
    _reject_unknown(mapping, expected, field)
    if set(mapping) != expected:
        raise ROIAnalysisError(f"{field} requires every policy field")
    coverage = _number(mapping["min_coverage_fraction"], f"{field}.min_coverage_fraction")
    out_of_bounds = _number(
        mapping["max_out_of_bounds_fraction"], f"{field}.max_out_of_bounds_fraction"
    )
    if not 0 <= coverage <= 1 or not 0 <= out_of_bounds <= 1:
        raise ROIAnalysisError(f"{field} fractions must be within [0, 1]")
    minimum_jacobian = _number(mapping["min_jacobian"], f"{field}.min_jacobian", positive=True)
    maximum_jacobian = _number(mapping["max_jacobian"], f"{field}.max_jacobian", positive=True)
    if minimum_jacobian >= maximum_jacobian:
        raise ROIAnalysisError(f"{field}.min_jacobian must be below max_jacobian")
    require_landmarks = mapping["require_landmarks"]
    if not isinstance(require_landmarks, bool):
        raise ROIAnalysisError(f"{field}.require_landmarks must be a boolean")
    residual = mapping["max_landmark_rms_residual_um"]
    if residual is not None:
        residual = _number(residual, f"{field}.max_landmark_rms_residual_um", positive=True)
    if require_landmarks and residual is None:
        raise ROIAnalysisError(f"{field} requires a landmark residual threshold")
    return MappingProxyType(
        {
            "min_coverage_fraction": coverage,
            "max_out_of_bounds_fraction": out_of_bounds,
            "min_jacobian": minimum_jacobian,
            "max_jacobian": maximum_jacobian,
            "require_landmarks": require_landmarks,
            "max_landmark_rms_residual_um": residual,
        }
    )


def _transfer_qc_policy(value: object, field: str) -> Mapping[str, object]:
    mapping = _mapping(value, field)
    expected = set(_TRANSFER_QC_POLICY_DEFAULTS)
    _reject_unknown(mapping, expected, field)
    if set(mapping) != expected:
        raise ROIAnalysisError(f"{field} requires every policy field")
    intersection = _number(
        mapping["min_target_tissue_intersection_fraction"],
        f"{field}.min_target_tissue_intersection_fraction",
    )
    out_of_bounds = _number(
        mapping["max_out_of_bounds_fraction"],
        f"{field}.max_out_of_bounds_fraction",
    )
    if not 0 <= intersection <= 1 or not 0 <= out_of_bounds <= 1:
        raise ROIAnalysisError(f"{field} fractions must be within [0, 1]")
    minimum_ratio = _number(mapping["min_area_ratio"], f"{field}.min_area_ratio", positive=True)
    maximum_ratio = _number(mapping["max_area_ratio"], f"{field}.max_area_ratio", positive=True)
    if minimum_ratio >= maximum_ratio:
        raise ROIAnalysisError(f"{field}.min_area_ratio must be below max_area_ratio")
    return MappingProxyType(
        {
            "min_target_tissue_intersection_fraction": intersection,
            "min_area_ratio": minimum_ratio,
            "max_area_ratio": maximum_ratio,
            "max_fragment_count_increase": _integer(
                mapping["max_fragment_count_increase"],
                f"{field}.max_fragment_count_increase",
                nonnegative=True,
            ),
            "max_out_of_bounds_fraction": out_of_bounds,
        }
    )


@dataclass(frozen=True, slots=True)
class MovingAssistanceScope:
    """One explicit decision to use accepted moving-modality evidence."""

    decision_id: str
    decision_source: str
    stage: str
    analysis_domain_id: str
    structure_id: str | None
    eligible_parent_ids: tuple[str, ...]
    registration_id: str
    decided_by: str

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "MovingAssistanceScope":
        """Decode the serialized fields into a validated MovingAssistanceScope record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _mapping(value, field)
        allowed = {
            "decision_id",
            "decision_source",
            "stage",
            "analysis_domain_id",
            "structure_id",
            "eligible_parent_ids",
            "registration_id",
            "decided_by",
        }
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every moving-assistance field")
        stage = _string(mapping["stage"], f"{field}.stage")
        if stage not in {"primary_classifier", "secondary_classifier"}:
            raise ROIAnalysisError(f"{field}.stage must be a classifier stage")
        source = _string(mapping["decision_source"], f"{field}.decision_source")
        if source not in {"user", "config", "training"}:
            raise ROIAnalysisError(f"{field}.decision_source must be user, config, or training")
        structure_id = _optional_string(mapping["structure_id"], f"{field}.structure_id")
        parent_ids = tuple(
            _string(item, f"{field}.eligible_parent_ids")
            for item in _sequence(mapping["eligible_parent_ids"], f"{field}.eligible_parent_ids")
        )
        if len(set(parent_ids)) != len(parent_ids):
            raise ROIAnalysisError(f"{field}.eligible_parent_ids must be unique")
        if stage == "primary_classifier" and (structure_id is not None or parent_ids):
            raise ROIAnalysisError(
                f"{field} primary assistance cannot name a structure or eligible parent"
            )
        if stage == "secondary_classifier" and (structure_id is None or not parent_ids):
            raise ROIAnalysisError(
                f"{field} secondary assistance requires structure and eligible parent IDs"
            )
        return cls(
            decision_id=_string(mapping["decision_id"], f"{field}.decision_id"),
            decision_source=source,
            stage=stage,
            analysis_domain_id=_sha256(
                mapping["analysis_domain_id"], f"{field}.analysis_domain_id"
            ),
            structure_id=structure_id,
            eligible_parent_ids=parent_ids,
            registration_id=_string(mapping["registration_id"], f"{field}.registration_id"),
            decided_by=_string(mapping["decided_by"], f"{field}.decided_by"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "decision_source": self.decision_source,
            "stage": self.stage,
            "analysis_domain_id": self.analysis_domain_id,
            "structure_id": self.structure_id,
            "eligible_parent_ids": list(self.eligible_parent_ids),
            "registration_id": self.registration_id,
            "decided_by": self.decided_by,
        }


@dataclass(frozen=True, slots=True)
class RegistrationConfig:
    mode: str = "prompt"
    moving_image: Path | None = None
    moving_image_id: str | None = None
    transform_file: Path | None = None
    moving_modality: str = "other"
    moving_image_metadata: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    registration_qc_policy: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType(dict(_REGISTRATION_QC_POLICY_DEFAULTS))
    )
    transfer_qc_policy: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType(dict(_TRANSFER_QC_POLICY_DEFAULTS))
    )
    moving_assistance_policy: str = "prompt"
    moving_assistance_scopes: tuple[MovingAssistanceScope, ...] = ()

    def __post_init__(self) -> None:
        """Validate the RegistrationConfig fields when the record is constructed.

        The checks cover ``mode``, ``moving_image_id``, ``moving_assistance_policy``,
        ``moving_assistance_scopes``, ``moving_modality``, ``registration_qc_policy``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if self.mode not in {"prompt", "none", "warpy", "side_by_side", "skip"}:
            raise ROIAnalysisError(
                "registration.mode must be prompt, none, warpy, side_by_side, or skip"
            )
        if self.mode in {"warpy", "side_by_side"} and (
            self.moving_image is None or self.moving_image_id is None
        ):
            raise ROIAnalysisError(
                "registration.moving_image and moving_image_id are required for "
                f"{self.mode} mode"
            )
        if self.moving_image_id is not None:
            object.__setattr__(
                self,
                "moving_image_id",
                _string(self.moving_image_id, "registration.moving_image_id"),
            )
        object.__setattr__(
            self, "moving_modality", _string(self.moving_modality, "registration.moving_modality")
        )
        object.__setattr__(
            self,
            "moving_image_metadata",
            _freeze_json(
                _mapping(self.moving_image_metadata, "registration.moving_image_metadata"),
                "registration.moving_image_metadata",
            ),
        )
        object.__setattr__(
            self,
            "registration_qc_policy",
            _registration_qc_policy(
                self.registration_qc_policy, "registration.registration_qc_policy"
            ),
        )
        object.__setattr__(
            self,
            "transfer_qc_policy",
            _transfer_qc_policy(self.transfer_qc_policy, "registration.transfer_qc_policy"),
        )
        if self.moving_assistance_policy not in {"none", "configured", "prompt"}:
            raise ROIAnalysisError(
                "registration.moving_assistance_policy must be none, configured, or prompt"
            )
        scopes = tuple(self.moving_assistance_scopes)
        if any(not isinstance(item, MovingAssistanceScope) for item in scopes):
            raise ROIAnalysisError("registration moving-assistance scopes are invalid")
        if len({item.decision_id for item in scopes}) != len(scopes):
            raise ROIAnalysisError("registration moving-assistance decision IDs must be unique")
        if self.moving_assistance_policy == "configured" and not scopes:
            raise ROIAnalysisError("configured moving assistance requires at least one scope")
        if self.moving_assistance_policy != "configured" and scopes:
            raise ROIAnalysisError("moving-assistance scopes require configured policy")
        if self.moving_assistance_policy == "configured" and self.mode != "warpy":
            raise ROIAnalysisError(
                "configured moving assistance requires an explicit Warpy registration mode"
            )
        object.__setattr__(self, "moving_assistance_scopes", scopes)

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "RegistrationConfig":
        mapping = _mapping(value, field)
        allowed = {
            "mode",
            "moving_image",
            "moving_image_id",
            "transform_file",
            "moving_modality",
            "moving_image_metadata",
            "registration_qc_policy",
            "transfer_qc_policy",
            "moving_assistance_policy",
            "moving_assistance_scopes",
        }
        _reject_unknown(mapping, allowed, field)
        mode = _string(mapping.get("mode", "prompt"), f"{field}.mode")
        default_assistance = "prompt" if mode in {"prompt", "warpy"} else "none"
        return cls(
            mode=mode,
            moving_image=_optional_path(mapping.get("moving_image"), f"{field}.moving_image"),
            moving_image_id=(
                None
                if mapping.get("moving_image_id") is None
                else _string(mapping.get("moving_image_id"), f"{field}.moving_image_id")
            ),
            transform_file=_optional_path(mapping.get("transform_file"), f"{field}.transform_file"),
            moving_modality=_string(
                mapping.get("moving_modality", "other"), f"{field}.moving_modality"
            ),
            moving_image_metadata=_mapping(
                mapping.get("moving_image_metadata", {}), f"{field}.moving_image_metadata"
            ),
            registration_qc_policy=_mapping(
                mapping.get("registration_qc_policy", _REGISTRATION_QC_POLICY_DEFAULTS),
                f"{field}.registration_qc_policy",
            ),
            transfer_qc_policy=_mapping(
                mapping.get("transfer_qc_policy", _TRANSFER_QC_POLICY_DEFAULTS),
                f"{field}.transfer_qc_policy",
            ),
            moving_assistance_policy=_string(
                mapping.get("moving_assistance_policy", default_assistance),
                f"{field}.moving_assistance_policy",
            ),
            moving_assistance_scopes=tuple(
                MovingAssistanceScope.from_mapping(
                    item, field=f"{field}.moving_assistance_scopes[{index}]"
                )
                for index, item in enumerate(
                    _sequence(
                        mapping.get("moving_assistance_scopes", ()),
                        f"{field}.moving_assistance_scopes",
                    )
                )
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "moving_image": _export_json(self.moving_image),
            "moving_image_id": self.moving_image_id,
            "transform_file": _export_json(self.transform_file),
            "moving_modality": self.moving_modality,
            "moving_image_metadata": _export_json(self.moving_image_metadata),
            "registration_qc_policy": _export_json(self.registration_qc_policy),
            "transfer_qc_policy": _export_json(self.transfer_qc_policy),
            "moving_assistance_policy": self.moving_assistance_policy,
            "moving_assistance_scopes": [
                item.to_mapping() for item in self.moving_assistance_scopes
            ],
        }


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    model_path: Path
    model_version: str
    device: str
    tile_size_px: int
    padding_px: int
    workers: int
    scope: str
    method: str = "instanseg"
    parameters: Mapping[str, object] | None = None

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "SegmentationConfig":
        mapping = _mapping(value, field)
        allowed = {
            "model_path",
            "model_version",
            "device",
            "tile_size_px",
            "padding_px",
            "workers",
            "scope",
        }
        method = mapping.get("method", "instanseg")
        if method not in {"instanseg", "cellpose", "stardist"}:
            raise ROIAnalysisError(f"{field}.method must be instanseg, cellpose or stardist")
        if method != "instanseg":
            allowed |= {"method", "parameters"}
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every field")
        scope = _string(mapping["scope"], f"{field}.scope")
        if scope not in {"accepted_tissue", "configured_union"}:
            raise ROIAnalysisError(f"{field}.scope must be accepted_tissue or configured_union")
        parameters = None
        if method != "instanseg":
            parameters = dict(_mapping(mapping["parameters"], f"{field}.parameters"))
            if mapping["device"] not in ({"cpu"} if method == "stardist" else {"cpu", "gpu", "auto"}):
                raise ROIAnalysisError("StarDist uses CPU; Cellpose supports CPU, NVIDIA GPU or automatic device selection")
            expected = {"channels", "pixel_size_um"} | (
                {"flow_threshold", "cellprob_threshold"} if method == "cellpose" else
                {"probability_threshold", "cell_expansion_um"})
            if method == "stardist" and "cell_constrain_scale" in parameters:
                expected.add("cell_constrain_scale")
            if set(parameters) != expected:
                raise ROIAnalysisError(f"{field}.parameters requires exactly the {method} controls")
            channels = parameters["channels"]
            if (not isinstance(channels, (list, tuple)) or
                    not 1 <= len(channels) <= (3 if method == "cellpose" else 1) or
                    any(type(channel) is not int or channel < 0 for channel in channels) or
                    len(set(channels)) != len(channels)):
                raise ROIAnalysisError(f"{field}.parameters.channels requires explicit distinct zero-based channel indices")
            parameters["channels"] = list(channels)
            for name in expected - {"channels"}:
                parameters[name] = _number(parameters[name], f"{field}.parameters.{name}")
            if not 0 < parameters["pixel_size_um"] <= 10:
                raise ROIAnalysisError(f"{field}.parameters.pixel_size_um must be positive and at most 10")
            if method == "stardist":
                if not 0 < parameters["probability_threshold"] < 1:
                    raise ROIAnalysisError("StarDist probability threshold must be between 0 and 1")
                if not 0 <= parameters["cell_expansion_um"] <= 25:
                    raise ROIAnalysisError("StarDist estimated cell expansion must be between 0 and 25 µm")
                if "cell_constrain_scale" in parameters:
                    if parameters["cell_constrain_scale"] <= 1:
                        raise ROIAnalysisError("StarDist expansion scale factor must be greater than 1")
                    if parameters["cell_expansion_um"] <= 0:
                        raise ROIAnalysisError("StarDist expansion scale factor requires positive estimated cell expansion")
            elif not 0 < parameters["flow_threshold"] <= 3:
                raise ROIAnalysisError("Cellpose flow threshold must be positive and at most 3")
            tile_size = _integer(mapping["tile_size_px"], f"{field}.tile_size_px", positive=True)
            padding = _integer(mapping["padding_px"], f"{field}.padding_px", nonnegative=True)
            if tile_size < 128 or tile_size > 2048 or padding * 2 >= tile_size:
                raise ROIAnalysisError("Alternative segmentation requires tile size 128–2048 with padding smaller than half the tile")
            if mapping["workers"] != 1:
                raise ROIAnalysisError("Cellpose and StarDist use one model worker; select 1 simultaneous tile")
        return cls(
            _path(mapping["model_path"], f"{field}.model_path"),
            _string(mapping["model_version"], f"{field}.model_version"),
            _string(mapping["device"], f"{field}.device"),
            _integer(mapping["tile_size_px"], f"{field}.tile_size_px", positive=True),
            _integer(mapping["padding_px"], f"{field}.padding_px", nonnegative=True),
            _integer(mapping["workers"], f"{field}.workers", positive=True),
            scope,
            method,
            _freeze_json(parameters, f"{field}.parameters") if parameters is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "model_path": str(self.model_path),
            "model_version": self.model_version,
            "device": self.device,
            "tile_size_px": self.tile_size_px,
            "padding_px": self.padding_px,
            "workers": self.workers,
            "scope": self.scope,
            **({"method": self.method, "parameters": _export_json(self.parameters or {})}
               if self.method != "instanseg" else {}),
        }


@dataclass(frozen=True, slots=True)
class PhenotypingConfig:
    known_marker_rules: Mapping[str, object]
    composite_rules: tuple[object, ...]
    mode: PhenotypingMode | None = None
    mode_policy_version: str = PHENOTYPING_MODE_POLICY_SCHEMA_VERSION
    phenotype_classes: tuple[PhenotypeClassSpec, ...] = ()
    population: Mapping[str, object] | None = None

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "PhenotypingConfig":
        """Decode the serialized fields into a validated PhenotypingConfig record.

        The mapping is checked against the declared field inventory, including ``mode``.
        Nested values are decoded through the corresponding field validators before the
        record is constructed."""

        mapping = _mapping(value, field)
        canonical_fields = {
            "mode_policy",
            "phenotype_classes",
            "known_marker_rules",
            "composite_rules",
        }
        _reject_unknown(mapping, canonical_fields | {"population"}, field)
        if set(mapping) - {"population"} != canonical_fields:
            raise ROIAnalysisError(f"{field} requires every field")
        mode_policy = _mapping(mapping["mode_policy"], f"{field}.mode_policy")
        _reject_unknown(mode_policy, {"schema_version", "mode"}, f"{field}.mode_policy")
        if set(mode_policy) != {"schema_version", "mode"}:
            raise ROIAnalysisError(f"{field}.mode_policy requires every field")
        version = _string(mode_policy["schema_version"], f"{field}.mode_policy.schema_version")
        if version != PHENOTYPING_MODE_POLICY_SCHEMA_VERSION:
            raise ROIAnalysisError(f"{field}.mode_policy.schema_version is invalid")
        raw_mode = mode_policy["mode"]
        mode = (
            None
            if raw_mode is None
            else _enum(PhenotypingMode, raw_mode, f"{field}.mode_policy.mode")
        )
        classes: list[PhenotypeClassSpec] = []
        class_ids: set[str] = set()
        display_names: set[str] = set()
        for index, raw in enumerate(
            _sequence(mapping["phenotype_classes"], f"{field}.phenotype_classes")
        ):
            row = _mapping(raw, f"{field}.phenotype_classes[{index}]")
            _reject_unknown(
                row,
                {"class_id", "display_name", "precedence"},
                f"{field}.phenotype_classes[{index}]",
            )
            required = {"class_id", "display_name"}
            if not required <= set(row):
                raise ROIAnalysisError(
                    f"{field}.phenotype_classes[{index}] requires class_id and display_name"
                )
            if (
                "precedence" in row
                and _integer(
                    row["precedence"],
                    f"{field}.phenotype_classes[{index}].precedence",
                    nonnegative=True,
                )
                != index
            ):
                raise ROIAnalysisError("phenotype class precedence must match declared order")
            item = PhenotypeClassSpec(
                _string(row["class_id"], f"{field}.phenotype_classes[{index}].class_id"),
                _string(row["display_name"], f"{field}.phenotype_classes[{index}].display_name"),
                index,
            )
            if item.class_id in class_ids or item.display_name.casefold() in display_names:
                raise ROIAnalysisError("phenotype class IDs and display names must be unique")
            class_ids.add(item.class_id)
            display_names.add(item.display_name.casefold())
            classes.append(item)
        rules: dict[str, object] = {}
        for marker, raw in _mapping(
            mapping["known_marker_rules"], f"{field}.known_marker_rules"
        ).items():
            rule = _mapping(raw, f"{field}.known_marker_rules.{marker}")
            marker_mode = _string(rule.get("mode"), f"{field}.known_marker_rules.{marker}.mode")
            required = {"mode"}
            allowed_rule = {"mode"}
            if marker_mode == "threshold":
                required |= {
                    "channel_id",
                    "measurement",
                    "compartment",
                    "polarity",
                    "operator",
                    "threshold",
                    "confirmed",
                }
                allowed_rule |= required
                _string(
                    rule.get("channel_id"),
                    f"{field}.known_marker_rules.{marker}.channel_id",
                )
                _string(
                    rule.get("measurement"),
                    f"{field}.known_marker_rules.{marker}.measurement",
                )
                compartment = _string(
                    rule.get("compartment"),
                    f"{field}.known_marker_rules.{marker}.compartment",
                )
                if compartment not in {"nucleus", "cytoplasm", "membrane", "cell"}:
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker}.compartment must be "
                        "nucleus, cytoplasm, membrane, or cell"
                    )
                polarity = _string(
                    rule.get("polarity"),
                    f"{field}.known_marker_rules.{marker}.polarity",
                ).casefold()
                if polarity not in {"positive", "above"}:
                    raise ROIAnalysisError("threshold marker polarity is invalid")
                operator = rule.get("operator")
                if operator not in {">", ">="}:
                    raise ROIAnalysisError("A positive marker requires an upper-tail intensity threshold")
                _number(rule.get("threshold"), f"{field}.known_marker_rules.{marker}.threshold")
                if not isinstance(rule.get("confirmed"), bool):
                    raise ROIAnalysisError("threshold marker confirmed must be a boolean")
            elif marker_mode == "object_classifier":
                required |= {
                    "channel_id",
                    "channel_role",
                    "positive_classification",
                    "negative_classification",
                    "artifact_path",
                    "classifier_id",
                    "classifier_version",
                    "classifier_sha256",
                    "validation_domain_id",
                    "validation_status",
                    "validation_record_id",
                    "qc_passed",
                    "review_status",
                    "review_record_id",
                }
                allowed_rule |= required
                for name in (
                    "channel_id",
                    "channel_role",
                    "positive_classification",
                    "negative_classification",
                    "artifact_path",
                    "classifier_id",
                    "classifier_version",
                    "validation_domain_id",
                    "validation_record_id",
                    "review_record_id",
                ):
                    _string(
                        rule.get(name),
                        f"{field}.known_marker_rules.{marker}.{name}",
                    )
                if rule.get("positive_classification") == rule.get("negative_classification"):
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker} object-classifier "
                        "positive and negative classifications must differ"
                    )
                digest = _string(
                    rule.get("classifier_sha256"),
                    f"{field}.known_marker_rules.{marker}.classifier_sha256",
                )
                if len(digest) != 64 or any(
                    character not in "0123456789abcdef" for character in digest
                ):
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker}.classifier_sha256 "
                        "must be a lowercase SHA-256"
                    )
                if rule.get("validation_status") != "validated":
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker}.validation_status "
                        "must be validated"
                    )
                if rule.get("qc_passed") is not True:
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker}.qc_passed must be true"
                    )
                if rule.get("review_status") != "accepted":
                    raise ROIAnalysisError(
                        f"{field}.known_marker_rules.{marker}.review_status " "must be accepted"
                    )
            elif marker_mode != "unused":
                raise ROIAnalysisError(
                    "marker rule mode must be threshold, object_classifier, or unused"
                )
            _reject_unknown(rule, allowed_rule, f"{field}.known_marker_rules.{marker}")
            if not required <= set(rule):
                raise ROIAnalysisError(
                    f"{field}.known_marker_rules.{marker} lacks mode-required fields"
                )
            rules[_string(marker, f"{field} marker", preserve=True)] = _freeze_json(
                rule, f"{field}.known_marker_rules.{marker}"
            )
        composites: list[object] = []
        for index, raw in enumerate(
            _sequence(mapping["composite_rules"], f"{field}.composite_rules")
        ):
            rule = _mapping(raw, f"{field}.composite_rules[{index}]")
            _reject_unknown(
                rule,
                {"phenotype", "all_of", "any_of", "none_of"},
                f"{field}.composite_rules[{index}]",
            )
            _string(rule.get("phenotype"), f"{field}.composite_rules[{index}].phenotype")
            if not any(name in rule for name in ("all_of", "any_of", "none_of")):
                raise ROIAnalysisError("composite rule requires explicit evidence")
            composites.append(_freeze_json(rule, f"{field}.composite_rules[{index}]"))
        population = None
        if "population" in mapping:
            from ..cell_analysis.phenotyping_population import validate_population_settings
            try:
                population = _freeze_json(validate_population_settings(mapping["population"]), f"{field}.population")
            except ValueError as exc:
                raise ROIAnalysisError(str(exc)) from exc
        return cls(
            MappingProxyType(rules),
            tuple(composites),
            mode,
            version,
            tuple(classes),
            population,
        )

    def to_mapping(self) -> dict[str, object]:
        result = {
            "mode_policy": {
                "schema_version": self.mode_policy_version,
                "mode": None if self.mode is None else self.mode.value,
            },
            "phenotype_classes": [item.to_mapping() for item in self.phenotype_classes],
            "known_marker_rules": _export_json(self.known_marker_rules),
            "composite_rules": _export_json(self.composite_rules),
        }
        if self.population is not None:
            result["population"] = _export_json(self.population)
        return result


_CLUSTERING_EXCLUDED_CHANNEL_ROLES = (
    "nuclear",
    "autofluorescence",
    "dapi",
)


def _clustering_feature_specs(value: object, *, field: str) -> tuple[object, ...]:
    features: list[object] = []
    for index, raw in enumerate(_sequence(value, field)):
        item_field = f"{field}[{index}]"
        spec = _mapping(raw, item_field)
        _reject_unknown(
            spec,
            {
                "feature_id",
                "kind",
                "source",
                "transform",
                "weight",
                "channel_id",
                "biomarker",
                "compartment",
                "units",
                "opt_in",
                "producer",
                "producer_version",
            },
            item_field,
        )
        required = {"feature_id", "kind", "source", "channel_id", "biomarker"}
        missing = sorted(required - set(spec))
        if missing:
            raise ROIAnalysisError(f"{item_field} requires explicit " + ", ".join(missing))
        for name in ("feature_id", "kind", "source", "channel_id"):
            _string(spec.get(name), f"{item_field}.{name}")
        normalized = dict(spec)
        kind = _string(spec["kind"], f"{item_field}.kind")
        if kind != "expression":
            raise ROIAnalysisError(
                f"{item_field}.kind must be expression "
                "(the wire token for mIF protein-marker intensity)"
            )
        transform = _string(spec.get("transform", "robust_scale"), f"{item_field}.transform")
        if transform != "robust_scale":
            raise ROIAnalysisError(f"{item_field}.transform must be robust_scale")
        normalized["transform"] = transform
        normalized["weight"] = _number(
            spec.get("weight", 1.0), f"{item_field}.weight", positive=True
        )
        for name in (
            "channel_id",
            "biomarker",
            "compartment",
            "units",
            "producer",
            "producer_version",
        ):
            if name in normalized and normalized[name] is not None:
                normalized[name] = _string(normalized[name], f"{item_field}.{name}")
        if "opt_in" in normalized and not isinstance(normalized["opt_in"], bool):
            raise ROIAnalysisError(f"{item_field}.opt_in must be a boolean")
        features.append(_freeze_json(normalized, item_field))
    return tuple(features)


@dataclass(frozen=True, slots=True)
class ClusteringConfig:
    enabled: bool
    mode: ClusteringMode | None
    feature_specs: tuple[object, ...]
    k: int | None
    resolution: float | None
    seed: int | None
    exclude_channel_roles: tuple[str, ...] = ("nuclear", "autofluorescence", "dapi")
    method: str = 'snn_leiden'
    method_settings: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the ClusteringConfig fields when the record is constructed.

        The checks cover ``feature_specs``, ``mode``, ``enabled``, ``k``, ``resolution``,
        ``seed``. Invalid field types, values or combinations raise the declared validation
        error before this instance can be used by its caller."""

        from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings
        object.__setattr__(self, 'method_settings', validate_method_settings(self.method, self.method_settings or {}))
        if not isinstance(self.enabled, bool):
            raise ROIAnalysisError("clustering.enabled must be a boolean")
        features = _clustering_feature_specs(self.feature_specs, field="clustering.feature_specs")
        if self.mode is None:
            if (
                self.enabled
                or features
                or (self.k, self.resolution, self.seed) != (None, None, None)
            ):
                raise ROIAnalysisError(
                    "unresolved clustering.mode requires an inactive featureless block with null parameters"
                )
        elif not isinstance(self.mode, ClusteringMode):
            raise ROIAnalysisError("clustering.mode must be a ClusteringMode or None")
        if (
            not self.enabled
            and not features
            and (self.k, self.resolution, self.seed) == (None, None, None)
        ):
            k = resolution = seed = None
        else:
            k = _integer(self.k, "clustering.k", positive=True)
            if k > 64:
                raise ROIAnalysisError("clustering.k must not exceed 64")
            resolution = _number(self.resolution, "clustering.resolution", positive=True)
            seed = _integer(self.seed, "clustering.seed")
            from mif_pipeline.cell_analysis.clustering_methods import validate_method_seed
            validate_method_seed(self.method, seed)
        exclusions = tuple(
            _string(item, "clustering.exclude_channel_roles")
            for item in _sequence(self.exclude_channel_roles, "clustering.exclude_channel_roles")
        )
        if exclusions != _CLUSTERING_EXCLUDED_CHANNEL_ROLES:
            raise ROIAnalysisError(
                "clustering.exclude_channel_roles must be exactly "
                "nuclear, autofluorescence, dapi in canonical order"
            )
        object.__setattr__(self, "feature_specs", features)
        object.__setattr__(self, "k", k)
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "seed", seed)
        object.__setattr__(self, "exclude_channel_roles", exclusions)

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "ClusteringConfig":
        mapping = _mapping(value, field)
        allowed = {
            "enabled",
            "mode",
            "feature_specs",
            "k",
            "resolution",
            "seed",
            "exclude_channel_roles",
            "method",
            "method_settings",
        }
        _reject_unknown(mapping, allowed, field)
        required = allowed - {"exclude_channel_roles", "method", "method_settings"}
        if not required <= set(mapping):
            raise ROIAnalysisError(
                f"{field} requires enabled, mode, feature_specs, k, " "resolution, and seed"
            )
        return cls(
            enabled=mapping["enabled"],
            method=mapping.get('method', 'snn_leiden'),
            method_settings=mapping.get('method_settings', {}),
            mode=(
                None
                if mapping["mode"] is None
                else _enum(ClusteringMode, mapping["mode"], f"{field}.mode")
            ),
            feature_specs=mapping["feature_specs"],  # type: ignore[arg-type]
            k=mapping["k"],
            resolution=mapping["resolution"],
            seed=mapping["seed"],
            exclude_channel_roles=mapping.get(
                "exclude_channel_roles", _CLUSTERING_EXCLUDED_CHANNEL_ROLES
            ),  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode.value if self.mode is not None else None,
            "feature_specs": _export_json(self.feature_specs),
            **({'method': self.method, 'method_settings': dict(self.method_settings)} if self.method != 'snn_leiden' else {}),
            "k": self.k,
            "resolution": self.resolution,
            "seed": self.seed,
            "exclude_channel_roles": list(self.exclude_channel_roles),
        }


@dataclass(frozen=True, slots=True)
class SemanticConfig:
    profile_references: tuple[SemanticProfileReference, ...]
    roi_review_policy: ReviewPolicy

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "SemanticConfig":
        mapping = _mapping(value, field)
        allowed = {"profile_references", "roi_review_policy"}
        _reject_unknown(mapping, allowed, field)
        if set(mapping) != allowed:
            raise ROIAnalysisError(f"{field} requires every field")
        profiles = tuple(
            SemanticProfileReference.from_mapping(
                item, field=f"{field}.profile_references[{index}]"
            )
            for index, item in enumerate(
                _sequence(mapping["profile_references"], f"{field}.profile_references")
            )
        )
        if len({item.profile_id for item in profiles}) != len(profiles):
            raise ROIAnalysisError(f"{field}.profile_references profile IDs must be unique")
        if len({item.content_sha256 for item in profiles}) != len(profiles):
            raise ROIAnalysisError(f"{field}.profile_references content digests must be unique")
        return cls(
            profiles,
            _enum(ReviewPolicy, mapping["roi_review_policy"], f"{field}.roi_review_policy"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_references": [item.to_mapping() for item in self.profile_references],
            "roi_review_policy": self.roi_review_policy.value,
        }


def _primary_classes(
    values: Sequence[PrimaryClassSpec], *, require_ignore_training: bool,
) -> tuple[PrimaryClassSpec, ...]:
    minimum = 1 if require_ignore_training else MINIMUM_PRIMARY_CLASS_COUNT
    if not minimum <= len(values) <= MAXIMUM_PRIMARY_CLASS_COUNT:
        raise ROIAnalysisError(
            "Primary analysis requires one to four classes; a single class requires learned Ignore"
        )
    if len({item.class_id for item in values}) != len(values):
        raise ROIAnalysisError("primary class IDs must be unique")
    if any(item.class_id == IGNORE_CLASS_ID for item in values):
        raise ROIAnalysisError("primary class IDs must be distinct from Ignore")
    display_names = tuple(item.display_name.strip().casefold() for item in values)
    if len(set(display_names)) != len(values):
        raise ROIAnalysisError(
            "primary class display names must be case-insensitively unique QuPath labels"
        )
    if IGNORE_CLASS_ID.casefold() in display_names:
        raise ROIAnalysisError(
            "primary class display names must be distinct from reserved QuPath label Ignore"
        )
    return tuple(values)


def _validate_analysis_identifier(identifier: str) -> None:
    if (
        identifier in {"TissueSection", ANALYSIS_DOMAIN_PARENT_ID, IGNORE_CLASS_ID}
        or identifier.startswith("Candidate_")
        or identifier.startswith("Other_")
    ):
        raise ROIAnalysisError(f"identifier {identifier!r} is reserved by the analysis protocol")


@dataclass(frozen=True, slots=True)
class PrimaryClassifierSettings:
    """Concrete primary choices, without admission or execution authority."""

    primary_classes: tuple[PrimaryClassSpec, ...]
    primary_classifier: ClassifierSpec

    def __post_init__(self) -> None:
        if (
            not isinstance(self.primary_classes, tuple)
            or not all(isinstance(item, PrimaryClassSpec) for item in self.primary_classes)
            or not isinstance(self.primary_classifier, ClassifierSpec)
        ):
            raise ROIAnalysisError("primary settings require typed concrete classes and classifier")
        if not self.primary_classifier.channel_roles:
            raise ROIAnalysisError("Choose at least one input channel for the primary classifier.")
        classes = _primary_classes(
            tuple(
                PrimaryClassSpec.from_value(item.to_mapping(), field=f"primary_classes[{index}]")
                for index, item in enumerate(self.primary_classes)
            ),
            require_ignore_training=self.primary_classifier.require_ignore_training,
        )
        object.__setattr__(self, "primary_classes", classes)
        class_ids = {item.class_id for item in classes}
        for identifier in class_ids:
            _validate_analysis_identifier(identifier)
        classifier = self.primary_classifier
        if set(classifier.class_count_bounds) - class_ids:
            raise ROIAnalysisError(
                "primary classifier class_count_bounds references unknown classes"
            )
        outputs = classifier.output_classes
        if outputs is None:
            raise ROIAnalysisError("primary classifier requires explicit output_classes")
        if set(outputs) not in (class_ids, class_ids | {IGNORE_CLASS_ID}):
            raise ROIAnalysisError(
                "primary classifier output_classes must be the configured "
                "biological classes with optional Ignore"
            )
        if classifier.require_ignore_training and IGNORE_CLASS_ID not in outputs:
            raise ROIAnalysisError(
                "primary classifier requires Ignore output when Ignore training is required"
            )

    @classmethod
    def from_mapping(cls, value: object) -> "PrimaryClassifierSettings":
        mapping = _mapping(value, "primary settings")
        allowed = {"primary_classes", "primary_classifier"}
        _reject_unknown(mapping, allowed, "primary settings")
        if set(mapping) != allowed or any(mapping[key] is None for key in allowed):
            raise ROIAnalysisError(
                "primary settings require concrete primary_classes and primary_classifier"
            )
        return cls(
            primary_classes=tuple(
                PrimaryClassSpec.from_value(item, field=f"primary_classes[{index}]")
                for index, item in enumerate(
                    _sequence(mapping["primary_classes"], "primary_classes")
                )
            ),
            primary_classifier=ClassifierSpec.from_mapping(
                mapping["primary_classifier"],
                field="primary_classifier",
                include_count_policy=True,
                include_primary_output_inventory=True,
            ),
        )

    def validate_channels(self, channels: Sequence[ChannelSpec]) -> None:
        if isinstance(channels, (str, bytes)) or not isinstance(channels, Sequence):
            raise ROIAnalysisError("primary settings channels must be a sequence of ChannelSpec")
        if not all(isinstance(item, ChannelSpec) for item in channels):
            raise ROIAnalysisError("primary settings channels must contain ChannelSpec records")
        channel_ids = {item.channel_id for item in channels}
        if not channels or len(channel_ids) != len(channels):
            raise ROIAnalysisError("channels must contain unique canonical channel IDs")
        classifier = self.primary_classifier
        for channel_id in classifier.channel_roles.values():
            if channel_id not in channel_ids:
                raise ROIAnalysisError(
                    f"classifier {classifier.classifier_id!r} references unknown channel {channel_id!r}"
                )

    def to_mapping(self) -> dict[str, object]:
        return {
            "primary_classes": [item.to_mapping() for item in self.primary_classes],
            "primary_classifier": self.primary_classifier.to_mapping(
                include_count_policy=True,
                include_primary_output_inventory=True,
            ),
        }


def require_primary_settings_compatible(
    admitted: PrimaryClassifierSettings,
    accepted: PrimaryClassifierSettings,
) -> None:
    """Keep admitted primary settings while applying an accepted Ignore choice.

    We permit a training-plan choice to change the optional output inventory
    and whether actual Ignore reference annotations are required. Biological
    classes, classifier identity, channels and every other setting remain exact.
    The caller owns the accepted plan or settings-revision authority; this
    comparison does not grant permission to change a running analysis.
    """
    if type(admitted) is not PrimaryClassifierSettings or type(accepted) is not PrimaryClassifierSettings:
        raise ROIAnalysisError("primary settings comparison requires complete settings")
    if admitted == accepted:
        return
    if (admitted.primary_classifier.mode is not ClassifierMode.TRAIN_NEW
            or accepted.primary_classifier.mode is not ClassifierMode.TRAIN_NEW):
        raise ROIAnalysisError("only train_new settings permit an accepted Ignore choice")
    original = admitted.to_mapping()
    selected = accepted.to_mapping()
    for name in ("output_classes", "require_ignore_training"):
        original["primary_classifier"].pop(name)
        selected["primary_classifier"].pop(name)
    if original != selected:
        raise ROIAnalysisError("selected primary settings change fields outside the Ignore choice")


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    schema_version: str
    slide_id: str
    input_image: Path
    provenance: Path
    output_dir: Path
    channels: tuple[ChannelSpec, ...] | None
    primary_classes: tuple[PrimaryClassSpec, ...] | None
    primary_classifier: ClassifierSpec | None
    secondary_structures: tuple[SecondaryStructureSpec, ...] | None
    tissue_detection: TissueDetectionConfig | None
    analysis_domain: AnalysisDomainConfig
    registration: RegistrationConfig
    segmentation: SegmentationConfig | None
    phenotyping: PhenotypingConfig
    clustering: ClusteringConfig
    semantic: SemanticConfig

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ROIAnalysisError(
                f"analysis config schema_version must equal {SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        assert_current_writer("analysis-config", self.schema_version)
        if (self.primary_classes is None) != (self.primary_classifier is None):
            raise ROIAnalysisError(
                "primary_classes and primary_classifier must both be concrete or both null"
            )
        if self.channels is None:
            if self.primary_classes is not None or self.primary_classifier is not None:
                raise ROIAnalysisError(
                    "unresolved channels require deferred primary classifier settings"
                )
            if any(item.classifier.channel_roles for item in (self.secondary_structures or ())):
                raise ROIAnalysisError("unresolved channels cannot supply secondary channel roles")
            if (
                self.tissue_detection is not None
                and self.tissue_detection.reference_channel_id is not None
            ):
                raise ROIAnalysisError(
                    "unresolved channels cannot supply a tissue reference channel"
                )
            if any(
                isinstance(rule, Mapping) and rule.get("mode") != "unused"
                for rule in self.phenotyping.known_marker_rules.values()
            ):
                raise ROIAnalysisError("unresolved channels cannot supply active marker rules")
            if self.clustering.feature_specs:
                raise ROIAnalysisError("unresolved channels cannot supply clustering features")
        if (
            self.phenotyping.mode is PhenotypingMode.UNSUPERVISED_CLUSTERING
            and not self.clustering.enabled
        ):
            raise ROIAnalysisError(
                "phenotyping mode unsupervised_clustering requires " "clustering.enabled=true"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "AnalysisConfig":
        """Decode the serialized fields into a validated AnalysisConfig record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _mapping(value, "config")
        schema_version = _string(mapping.get("schema_version"), "schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ROIAnalysisError(
                f"analysis config schema_version must equal {SCHEMA_VERSION!r}, got {schema_version!r}"
            )
        assert_current_writer("analysis-config", schema_version)
        allowed = {
            "schema_version",
            "slide_id",
            "input_image",
            "provenance",
            "output_dir",
            "channels",
            "primary_classes",
            "primary_classifier",
            "secondary_structures",
            "tissue_detection",
            "analysis_domain",
            "registration",
            "segmentation",
            "phenotyping",
            "clustering",
            "semantic",
        }
        _reject_unknown(mapping, allowed, "config")
        if set(mapping) != allowed:
            raise ROIAnalysisError("schema v6 requires every top-level configuration block")
        channels = (
            None
            if mapping["channels"] is None
            else tuple(
                ChannelSpec.from_mapping(item, field=f"channels[{index}]")
                for index, item in enumerate(_sequence(mapping["channels"], "channels"))
            )
        )
        if channels is not None and (
            not channels or len({item.channel_id for item in channels}) != len(channels)
        ):
            raise ROIAnalysisError("channels must contain unique canonical channel IDs")
        if (mapping["primary_classes"] is None) != (mapping["primary_classifier"] is None):
            raise ROIAnalysisError(
                "primary_classes and primary_classifier must both be concrete or both null"
            )
        primary_settings = (
            None
            if mapping["primary_classes"] is None
            else PrimaryClassifierSettings.from_mapping(
                {
                    "primary_classes": mapping["primary_classes"],
                    "primary_classifier": mapping["primary_classifier"],
                }
            )
        )
        secondary = (
            None if mapping["secondary_structures"] is None else tuple(
                SecondaryStructureSpec.from_mapping(item, field=f"secondary_structures[{index}]")
                for index, item in enumerate(
                    _sequence(mapping["secondary_structures"], "secondary_structures")
                )
            )
        )
        if secondary is not None and len(secondary) > MAXIMUM_SECONDARY_STRUCTURE_COUNT:
            raise ROIAnalysisError(
                "ROI analysis supports at most eight active secondary structures"
            )
        config = cls(
            schema_version=schema_version,
            slide_id=_string(mapping["slide_id"], "slide_id", preserve=True),
            input_image=_path(mapping["input_image"], "input_image"),
            provenance=_path(mapping["provenance"], "provenance"),
            output_dir=_path(mapping["output_dir"], "output_dir"),
            channels=channels,
            primary_classes=None if primary_settings is None else primary_settings.primary_classes,
            primary_classifier=(
                None if primary_settings is None else primary_settings.primary_classifier
            ),
            secondary_structures=secondary,
            tissue_detection=(
                None
                if mapping["tissue_detection"] is None
                else TissueDetectionConfig.from_mapping(
                    mapping["tissue_detection"], field="tissue_detection"
                )
            ),
            analysis_domain=AnalysisDomainConfig.from_mapping(
                mapping["analysis_domain"], field="analysis_domain"
            ),
            registration=RegistrationConfig.from_mapping(
                mapping["registration"], field="registration"
            ),
            segmentation=(
                None
                if mapping["segmentation"] is None
                else SegmentationConfig.from_mapping(mapping["segmentation"], field="segmentation")
            ),
            phenotyping=PhenotypingConfig.from_mapping(mapping["phenotyping"], field="phenotyping"),
            clustering=ClusteringConfig.from_mapping(mapping["clustering"], field="clustering"),
            semantic=SemanticConfig.from_mapping(mapping["semantic"], field="semantic"),
        )
        config._validate_cross_references()
        return config

    def _validate_cross_references(self) -> None:
        # A pending stage choice has no supplied definitions or profile references.
        # The empty tuple remains an explicit decision to use no secondary structures.
        secondary_structures = self.secondary_structures or ()
        channels_by_id = {
            item.channel_id: item for item in (() if self.channels is None else self.channels)
        }
        channel_ids = set(channels_by_id)
        primary_class_ids = (
            set()
            if self.primary_classes is None
            else {item.class_id for item in self.primary_classes}
        )
        if self.primary_classifier is not None:
            PrimaryClassifierSettings(
                self.primary_classes, self.primary_classifier
            ).validate_channels(self.channels)
        for identifier in (
            *(item.structure_id for item in secondary_structures),
            *(item.profile_id for item in self.semantic.profile_references),
            *(item.class_id for item in self.phenotyping.phenotype_classes),
        ):
            _validate_analysis_identifier(identifier)
        secondary_classifier_groups(secondary_structures)
        classifiers = tuple(item.classifier for item in secondary_structures)
        for classifier in classifiers:
            for channel_id in classifier.channel_roles.values():
                if channel_id not in channel_ids:
                    raise ROIAnalysisError(
                        f"classifier {classifier.classifier_id!r} references unknown channel {channel_id!r}"
                    )
        if (
            self.tissue_detection is not None
            and self.tissue_detection.reference_channel_id is not None
            and self.tissue_detection.reference_channel_id not in channel_ids
        ):
            raise ROIAnalysisError("tissue_detection references unknown channel")
        for biomarker, raw_rule in self.phenotyping.known_marker_rules.items():
            if not isinstance(raw_rule, Mapping) or raw_rule.get("mode") == "unused":
                continue
            channel_id = raw_rule.get("channel_id")
            if channel_id not in channels_by_id:
                raise ROIAnalysisError(
                    f"phenotyping marker {biomarker!r} references unknown channel"
                )
            if channels_by_id[channel_id].biomarker != biomarker:
                raise ROIAnalysisError(
                    f"phenotyping marker {biomarker!r} does not match the explicit "
                    "canonical biomarker assigned to its channel"
                )
            if raw_rule.get("mode") == "threshold":
                channel_name = channels_by_id[channel_id].name
                if channel_name is None:
                    raise ROIAnalysisError(
                        f"phenotyping threshold marker {biomarker!r} requires an "
                        "explicit canonical channel name for QuPath measurements"
                    )
                compartment = raw_rule.get("compartment")
                display_compartment = {
                    "nucleus": "Nucleus",
                    "cytoplasm": "Cytoplasm",
                    "membrane": "Membrane",
                    "cell": "Cell",
                }[compartment]
                expected = {
                    f"{display_compartment}: {channel_name}: {statistic}"
                    for statistic in (
                        "Mean",
                        "Median",
                        "Min",
                        "Max",
                        "Std.Dev.",
                        "Variance",
                    )
                }
                measurement = raw_rule.get("measurement")
                if measurement not in expected:
                    raise ROIAnalysisError(
                        f"phenotyping threshold measurement {measurement!r} does "
                        f"not agree with compartment {compartment!r} and canonical "
                        f"channel {channel_name!r}"
                    )
        for raw_feature in self.clustering.feature_specs:
            assert isinstance(raw_feature, Mapping)
            channel_id = raw_feature["channel_id"]
            if channel_id not in channels_by_id:
                raise ROIAnalysisError(
                    f"clustering feature {raw_feature['feature_id']!r} references "
                    f"unknown channel {channel_id!r}"
                )
            if channels_by_id[channel_id].biomarker != raw_feature["biomarker"]:
                raise ROIAnalysisError(
                    f"clustering feature {raw_feature['feature_id']!r} does not "
                    "match the explicit canonical biomarker assigned to its channel"
                )
        allowed_parents = {ANALYSIS_DOMAIN_PARENT_ID, *primary_class_ids}
        seen: set[str] = set()
        for structure in secondary_structures:
            if structure.structure_id in seen:
                raise ROIAnalysisError("secondary structure IDs must be unique")
            invalid = set(structure.eligible_parent_ids) - allowed_parents
            if structure.structure_id in structure.eligible_parent_ids or invalid:
                raise ROIAnalysisError(
                    f"Secondary structure {structure.structure_id!r} must use the analysis region or a primary compartment as its parent."
                )
            seen.add(structure.structure_id)
        configured_profiles = {item.profile_id: item for item in self.semantic.profile_references}
        referenced_profile_ids: set[str] = set()
        for structure in secondary_structures:
            profile = configured_profiles.get(structure.semantic_profile.profile_id)
            if profile != structure.semantic_profile:
                raise ROIAnalysisError(
                    f"secondary structure {structure.structure_id!r} semantic profile reference is unresolved"
                )
            if profile.profile_id in referenced_profile_ids:
                raise ROIAnalysisError("secondary semantic profile references must be unique")
            referenced_profile_ids.add(profile.profile_id)
        if referenced_profile_ids != set(configured_profiles):
            raise ROIAnalysisError("semantic profile references must be used exactly once")
        structures = {item.structure_id: item for item in secondary_structures}
        for scope in self.registration.moving_assistance_scopes:
            if scope.stage == "secondary_classifier":
                structure = structures.get(scope.structure_id)
                if structure is None or scope.eligible_parent_ids != structure.eligible_parent_ids:
                    raise ROIAnalysisError(
                        "moving-assistance scope must carry the complete ordered "
                        "secondary classifier parent set"
                    )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "slide_id": self.slide_id,
            "input_image": str(self.input_image),
            "provenance": str(self.provenance),
            "output_dir": str(self.output_dir),
            "channels": (
                None if self.channels is None else [item.to_mapping() for item in self.channels]
            ),
            "primary_classes": (
                None
                if self.primary_classes is None
                else [item.to_mapping() for item in self.primary_classes]
            ),
            "primary_classifier": (
                None
                if self.primary_classifier is None
                else self.primary_classifier.to_mapping(
                    include_count_policy=True,
                    include_primary_output_inventory=True,
                )
            ),
            "secondary_structures": (
                None if self.secondary_structures is None
                else [item.to_mapping() for item in self.secondary_structures]
            ),
            "tissue_detection": (
                None if self.tissue_detection is None else self.tissue_detection.to_mapping()
            ),
            "analysis_domain": self.analysis_domain.to_mapping(),
            "registration": self.registration.to_mapping(),
            "segmentation": None if self.segmentation is None else self.segmentation.to_mapping(),
            "phenotyping": self.phenotyping.to_mapping(),
            "clustering": self.clustering.to_mapping(),
            "semantic": self.semantic.to_mapping(),
        }
