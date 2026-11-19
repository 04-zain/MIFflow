"""Read an analysis manifest and resolve its complete per-sample execution inputs.

The parser checks the declared schema, unique sample identities, subject/group assignments,
source paths and referenced settings. Validation retains both the original manifest and
normalized configuration identities. The resolved samples then provide the inputs from which
the dependency graph is constructed."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from types import MappingProxyType
from typing import TypeAlias

from mif_pipeline.pipeline_core import (
    AnalysisConfig,
    ROIAnalysisError,
    assert_current_writer,
    validate_safe_path_segment,
)
from mif_pipeline.pipeline_core.config import SCHEMA_VERSION as ANALYSIS_CONFIG_SCHEMA_VERSION

SCHEMA_NAME = "mif-batch-manifest"
SCHEMA_VERSION = "1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_NESTING_DEPTH = 32
MAX_MODALITY_TOKEN_LENGTH = 32
MAX_SAMPLES = 10_000
MAX_REUSABLE_ARTIFACTS = 10_000

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_-]*\Z")
_ARTIFACT_KINDS = frozenset(
    {
        "reviewed_panel_mapping",
        "spectral_reference_library",
        "classifier_record_set",
        "semantic_profile",
    }
)
_RESOURCE_CAPACITY_KEYS = frozenset(
    {
        "bioformats_conversion",
        "spectral_preprocessing",
        "scientific_compute",
        "qupath_image_read",
        "qupath_image_write",
        "project_catalogue_mutation",
        "shared_classifier_mutation",
    }
)
_OVERRIDE_KEYS = frozenset(
    {
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
)

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


# ==============================================================================
#                                 Batch manifest
# ==============================================================================


class BatchManifestError(ROIAnalysisError):
    """Raised when a batch authoring declaration is malformed or unsafe."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise BatchManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _safe_id(value: object, field: str) -> str:
    try:
        return validate_safe_path_segment(value, field, error_type=BatchManifestError)
    except ROIAnalysisError as exc:
        raise BatchManifestError(str(exc)) from exc


def _token(value: object, field: str) -> str:
    token = _text(value, field)
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise BatchManifestError(f"{field} must be a lowercase bounded token")
    return token


def _modality(value: object, field: str) -> str:
    modality = _token(value, field)
    if len(modality) > MAX_MODALITY_TOKEN_LENGTH:
        raise BatchManifestError(f"{field} must not exceed {MAX_MODALITY_TOKEN_LENGTH} characters")
    return modality


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise BatchManifestError(f"{field} must be a lowercase SHA-256")
    return digest


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchManifestError(f"{field} must be a positive integer")
    return value


def _path(value: object, field: str) -> Path:
    if isinstance(value, Path):
        return value.expanduser().resolve(strict=False)
    return Path(_text(value, field)).expanduser().resolve(strict=False)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchManifestError(f"{field} must be a string-keyed object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchManifestError(f"{field} must be an array")
    return value


def _closed_mapping(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str],
    field: str,
) -> Mapping[str, object]:
    mapping = _mapping(value, field)
    unknown = sorted(set(mapping) - required - optional)
    missing = sorted(required - set(mapping))
    if unknown:
        raise BatchManifestError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchManifestError(f"{field} lacks required field(s): {', '.join(missing)}")
    return mapping


def _freeze_json(value: object, field: str) -> JsonValue:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise BatchManifestError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise BatchManifestError(f"{field} mapping keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item, field) for item in value)
    raise BatchManifestError(f"{field} must contain only JSON-safe values")


def _export_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest_mapping(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _assert_json_nesting_limit(text: str) -> None:
    """Reject excessive structural nesting before recursive JSON decoding."""
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise BatchManifestError(
                    f"manifest nesting depth must not exceed {MAX_NESTING_DEPTH}"
                )
        elif character in "]}":
            depth -= 1


def _parse_json(data: bytes) -> Mapping[str, object]:
    if len(data) > MAX_MANIFEST_BYTES:
        raise BatchManifestError("manifest must not exceed 16 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchManifestError("manifest must be UTF-8") from exc

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BatchManifestError(f"manifest has duplicate key {key!r}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise BatchManifestError(f"manifest contains a non-finite number {value!r}")

    _assert_json_nesting_limit(text)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonfinite,
        )
    except BatchManifestError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
        raise BatchManifestError("manifest must contain one JSON object") from exc
    _freeze_json(parsed, "manifest")
    return _mapping(parsed, "manifest")


@dataclass(frozen=True, slots=True)
class BatchExecutionConfig:
    """Finite scheduler capacities captured as part of immutable batch input."""

    max_workers: int
    max_in_flight: int
    resource_capacities: Mapping[str, int]
    # Preserve recorded input identity when reopening an existing analysis.
    # New definitions omit this value; processing never uses it as a cap.
    max_working_set_bytes: int | None = None

    def __post_init__(self) -> None:
        maximum_workers = _positive_integer(self.max_workers, "execution_config.max_workers")
        maximum_in_flight = _positive_integer(self.max_in_flight, "execution_config.max_in_flight")
        if maximum_in_flight < maximum_workers:
            raise BatchManifestError("execution_config.max_in_flight must be at least max_workers")
        if self.max_working_set_bytes is not None:
            _positive_integer(self.max_working_set_bytes, "execution_config.max_working_set_bytes")
        capacities = _mapping(self.resource_capacities, "execution_config.resource_capacities")
        if set(capacities) != _RESOURCE_CAPACITY_KEYS:
            raise BatchManifestError(
                "execution_config.resource_capacities must declare every resource capacity"
            )
        parsed_capacities = {
            _token(name, "execution_config.resource_capacities key"): _positive_integer(
                capacity, f"execution_config.resource_capacities.{name}"
            )
            for name, capacity in capacities.items()
        }
        object.__setattr__(self, "max_workers", maximum_workers)
        object.__setattr__(self, "max_in_flight", maximum_in_flight)
        object.__setattr__(
            self, "resource_capacities", MappingProxyType(dict(sorted(parsed_capacities.items())))
        )

    @classmethod
    def from_mapping(cls, value: object) -> "BatchExecutionConfig":
        mapping = _closed_mapping(
            value,
            required=frozenset(
                {"max_workers", "max_in_flight", "resource_capacities"}
            ),
            optional=frozenset({"max_working_set_bytes"}),
            field="execution_config",
        )
        capacities = _mapping(
            mapping["resource_capacities"], "execution_config.resource_capacities"
        )
        if set(capacities) != _RESOURCE_CAPACITY_KEYS:
            raise BatchManifestError(
                "execution_config.resource_capacities must declare every resource capacity"
            )
        parsed = {
            _token(name, "execution_config.resource_capacities key"): _positive_integer(
                capacity, f"execution_config.resource_capacities.{name}"
            )
            for name, capacity in capacities.items()
        }
        maximum_workers = _positive_integer(mapping["max_workers"], "execution_config.max_workers")
        maximum_in_flight = _positive_integer(
            mapping["max_in_flight"], "execution_config.max_in_flight"
        )
        if maximum_in_flight < maximum_workers:
            raise BatchManifestError("execution_config.max_in_flight must be at least max_workers")
        return cls(
            max_workers=maximum_workers,
            max_in_flight=maximum_in_flight,
            resource_capacities=MappingProxyType(dict(sorted(parsed.items()))),
            max_working_set_bytes=(_positive_integer(mapping["max_working_set_bytes"],
                "execution_config.max_working_set_bytes") if "max_working_set_bytes" in mapping else None),
        )

    def to_mapping(self) -> dict[str, object]:
        value = {
            "max_workers": self.max_workers,
            "max_in_flight": self.max_in_flight,
            "resource_capacities": dict(self.resource_capacities),
        }
        if self.max_working_set_bytes is not None:
            value["max_working_set_bytes"] = self.max_working_set_bytes
        return value


@dataclass(frozen=True, slots=True)
class PairedReferenceImage:
    """One reviewed moving reference without modality-specific assumptions."""

    reference_id: str
    source_path: Path
    modality: str
    source_image_id: str | None
    source_metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        metadata = _freeze_json(self.source_metadata, "paired_reference_image.source_metadata")
        if not isinstance(metadata, Mapping):
            raise BatchManifestError("paired_reference_image.source_metadata must be an object")
        object.__setattr__(
            self, "reference_id", _safe_id(self.reference_id, "paired_reference_image.reference_id")
        )
        object.__setattr__(
            self, "source_path", _path(self.source_path, "paired_reference_image.source_path")
        )
        object.__setattr__(
            self, "modality", _modality(self.modality, "paired_reference_image.modality")
        )
        object.__setattr__(
            self,
            "source_image_id",
            _optional_text(self.source_image_id, "paired_reference_image.source_image_id"),
        )
        object.__setattr__(self, "source_metadata", metadata)

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> "PairedReferenceImage":
        mapping = _closed_mapping(
            value,
            required=frozenset({"reference_id", "source_path", "modality"}),
            optional=frozenset({"source_image_id", "source_metadata"}),
            field=field,
        )
        metadata_value = mapping.get("source_metadata", {})
        metadata = _freeze_json(metadata_value, f"{field}.source_metadata")
        if not isinstance(metadata, Mapping):
            raise BatchManifestError(f"{field}.source_metadata must be an object")
        return cls(
            reference_id=_safe_id(mapping["reference_id"], f"{field}.reference_id"),
            source_path=_path(mapping["source_path"], f"{field}.source_path"),
            modality=_modality(mapping["modality"], f"{field}.modality"),
            source_image_id=(
                None
                if mapping.get("source_image_id") is None
                else _text(mapping["source_image_id"], f"{field}.source_image_id")
            ),
            source_metadata=metadata,
        )

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "reference_id": self.reference_id,
            "source_path": str(self.source_path),
            "modality": self.modality,
        }
        if self.source_image_id is not None:
            value["source_image_id"] = self.source_image_id
        if self.source_metadata:
            value["source_metadata"] = _export_json(self.source_metadata)
        return value


@dataclass(frozen=True, slots=True)
class BatchSample:
    """Strict authoring declaration for one sample-local branch."""

    sample_id: str
    slide_id: str
    subject_id: str
    source_mif_path: Path
    reference: PairedReferenceImage | None
    validation_domain_id: str | None
    overrides: Mapping[str, JsonValue]
    enabled: bool
    phenotyping_group_id: str | None = None
    source_image_index: int | None = None
    source_series_index: int | None = None
    source_file_sha256: str | None = None
    processing_root: Path | None = None

    def __post_init__(self) -> None:
        if self.phenotyping_group_id is not None:
            raise BatchManifestError("phenotyping_group_id is a reserved null field")
        _validate_source_selection_pair(self.source_image_index, self.source_series_index)
        _validate_source_file_hash(self.source_file_sha256, self.source_image_index)
        if self.processing_root is not None:
            root = _path(self.processing_root, "sample.processing_root")
            if (len(root.parts) < 6 or root.parts[-4:-1] != ("samples", self.sample_id, "sources")
                    or re.fullmatch(r"node-[0-9a-f]{48}-[1-9][0-9]*", root.name) is None):
                raise BatchManifestError("processing_root must name its source-inspection revision in the sample's owned namespace")
            object.__setattr__(self, "processing_root", root)
        overrides = _freeze_json(self.overrides, "sample.overrides")
        if not isinstance(overrides, Mapping):
            raise BatchManifestError("sample.overrides must be an object")
        unknown_overrides = sorted(set(overrides) - _OVERRIDE_KEYS)
        if unknown_overrides:
            raise BatchManifestError(
                f"sample.overrides contains not permitted config v{ANALYSIS_CONFIG_SCHEMA_VERSION} field(s): "
                + ", ".join(unknown_overrides)
            )
        if self.reference is not None and not isinstance(self.reference, PairedReferenceImage):
            raise BatchManifestError("sample.reference must be a PairedReferenceImage")
        if type(self.enabled) is not bool:
            raise BatchManifestError("sample.enabled must be a boolean")
        object.__setattr__(self, "sample_id", _safe_id(self.sample_id, "sample.sample_id"))
        object.__setattr__(self, "slide_id", _text(self.slide_id, "sample.slide_id"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "sample.subject_id"))
        object.__setattr__(
            self, "source_mif_path", _path(self.source_mif_path, "sample.source_mif_path")
        )
        for name in (
            "validation_domain_id",
            "phenotyping_group_id",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), f"sample.{name}"))
        object.__setattr__(self, "overrides", overrides)

    @classmethod
    def from_mapping(cls, value: object, *, index: int) -> "BatchSample":
        """Decode the serialized fields into a validated BatchSample record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        field = f"samples[{index}]"
        mapping = _closed_mapping(
            value,
            required=frozenset(
                {"sample_id", "slide_id", "subject_id", "source_mif_path", "enabled"}
            ),
            optional=frozenset(
                {
                    "paired_reference_image",
                    "validation_domain_id",
                    "phenotyping_group_id",
                    "source_image_index",
                    "source_series_index",
                    "source_file_sha256",
                    "processing_root",
                    "overrides",
                }
            ),
            field=field,
        )
        overrides = _freeze_json(mapping.get("overrides", {}), f"{field}.overrides")
        if not isinstance(overrides, Mapping):
            raise BatchManifestError(f"{field}.overrides must be an object")
        unknown_overrides = sorted(set(overrides) - _OVERRIDE_KEYS)
        if unknown_overrides:
            raise BatchManifestError(
                f"{field}.overrides contains not permitted config v{ANALYSIS_CONFIG_SCHEMA_VERSION} field(s): {', '.join(unknown_overrides)}"
            )
        if type(mapping["enabled"]) is not bool:
            raise BatchManifestError(f"{field}.enabled must be a boolean")
        return cls(
            sample_id=_safe_id(mapping["sample_id"], f"{field}.sample_id"),
            slide_id=_text(mapping["slide_id"], f"{field}.slide_id"),
            subject_id=_text(mapping["subject_id"], f"{field}.subject_id"),
            source_mif_path=_path(mapping["source_mif_path"], f"{field}.source_mif_path"),
            reference=(
                None
                if mapping.get("paired_reference_image") is None
                else PairedReferenceImage.from_mapping(
                    mapping["paired_reference_image"], field=f"{field}.paired_reference_image"
                )
            ),
            validation_domain_id=(
                None
                if mapping.get("validation_domain_id") is None
                else _text(mapping["validation_domain_id"], f"{field}.validation_domain_id")
            ),
            overrides=overrides,
            enabled=mapping["enabled"],
            source_image_index=mapping.get("source_image_index"),
            source_series_index=mapping.get("source_series_index"),
            source_file_sha256=mapping.get("source_file_sha256"),
            processing_root=None if mapping.get("processing_root") is None else _path(mapping["processing_root"], f"{field}.processing_root"),
            phenotyping_group_id=_optional_text(
                mapping.get("phenotyping_group_id"),
                f"{field}.phenotyping_group_id",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "source_mif_path": str(self.source_mif_path),
            "enabled": self.enabled,
        }
        optional_values = {
            "validation_domain_id": self.validation_domain_id,
            "phenotyping_group_id": self.phenotyping_group_id,
            "source_image_index": self.source_image_index,
            "source_series_index": self.source_series_index,
            "source_file_sha256": self.source_file_sha256,
            "processing_root": None if self.processing_root is None else str(self.processing_root),
        }
        value.update({name: item for name, item in optional_values.items() if item is not None})
        if self.reference is not None:
            value["paired_reference_image"] = self.reference.to_mapping()
        if self.overrides:
            value["overrides"] = _export_json(self.overrides)
        return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _validate_source_selection_pair(image_index: object, series_index: object) -> None:
    if image_index is None and series_index is None:
        return
    if any(type(value) is not int or value < 0 for value in (image_index, series_index)):
        raise BatchManifestError(
            "source_image_index and source_series_index must both be absent "
            "or both be nonnegative integers"
        )


def _validate_source_file_hash(value: object, image_index: int | None) -> None:
    if value is None:
        return
    if image_index is None:
        raise BatchManifestError("source_file_sha256 requires source image and series indices")
    _sha256(value, "source_file_sha256")


@dataclass(frozen=True, slots=True)
class ReusableArtifactDeclaration:
    """Checksum-validated declaration awaiting owning-loader acceptance."""

    artifact_id: str
    artifact_kind: str
    schema_name: str
    schema_version: str
    locator: Path
    sha256: str
    byte_count: int
    owning_identity: Mapping[str, JsonValue]
    runtime_versions: Mapping[str, str]
    validation_domain_id: str | None
    compatibility_signature_sha256: str
    target_sample_ids: tuple[str, ...] = ()
    primary_settings: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        """Validate the ReusableArtifactDeclaration fields when the record is constructed.

        The checks cover ``artifact_kind``, ``owning_identity``, ``runtime_versions``,
        ``artifact_id``, ``schema_name``, ``schema_version``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        targets = tuple(self.target_sample_ids)
        if targets != tuple(sorted(set(targets))) or any(
                _safe_id(item, "saved classifier target") != item for item in targets):
            raise BatchManifestError("Saved classifier targets must be unique sample IDs in canonical order")
        if bool(targets) != (self.primary_settings is not None):
            raise BatchManifestError("Saved classifier selection requires targets and deployment settings together")
        if self.primary_settings is not None:
            from mif_pipeline.pipeline_core.config import PrimaryClassifierSettings, ClassifierMode
            settings = PrimaryClassifierSettings.from_mapping(self.primary_settings)
            if (self.artifact_kind != "classifier_record_set"
                    or settings.primary_classifier.mode is not ClassifierMode.SAVED):
                raise BatchManifestError("Only a saved primary classifier may carry deployment settings")
            object.__setattr__(self, "primary_settings", _freeze_json(settings.to_mapping(), "primary_settings"))
        object.__setattr__(self, "target_sample_ids", targets)
        artifact_kind = _token(self.artifact_kind, "reusable_artifact.artifact_kind")
        if artifact_kind not in _ARTIFACT_KINDS:
            raise BatchManifestError("reusable_artifact.artifact_kind is not supported")
        identity = _freeze_json(self.owning_identity, "reusable_artifact.owning_identity")
        if not isinstance(identity, Mapping) or not identity:
            raise BatchManifestError("reusable_artifact.owning_identity must be a non-empty object")
        runtime_values = _mapping(self.runtime_versions, "reusable_artifact.runtime_versions")
        if not runtime_values:
            raise BatchManifestError(
                "reusable_artifact.runtime_versions must be a non-empty object"
            )
        runtime_versions = {
            _token(key, "reusable_artifact.runtime_versions key"): _text(
                value, f"reusable_artifact.runtime_versions.{key}"
            )
            for key, value in runtime_values.items()
        }
        object.__setattr__(
            self, "artifact_id", _safe_id(self.artifact_id, "reusable_artifact.artifact_id")
        )
        object.__setattr__(self, "artifact_kind", artifact_kind)
        object.__setattr__(
            self, "schema_name", _text(self.schema_name, "reusable_artifact.schema_name")
        )
        object.__setattr__(
            self, "schema_version", _text(self.schema_version, "reusable_artifact.schema_version")
        )
        object.__setattr__(self, "locator", _path(self.locator, "reusable_artifact.locator"))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "reusable_artifact.sha256"))
        object.__setattr__(
            self, "byte_count", _positive_integer(self.byte_count, "reusable_artifact.byte_count")
        )
        object.__setattr__(self, "owning_identity", identity)
        object.__setattr__(
            self, "runtime_versions", MappingProxyType(dict(sorted(runtime_versions.items())))
        )
        object.__setattr__(
            self,
            "validation_domain_id",
            _optional_text(self.validation_domain_id, "reusable_artifact.validation_domain_id"),
        )
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(
                self.compatibility_signature_sha256,
                "reusable_artifact.compatibility_signature_sha256",
            ),
        )

    @classmethod
    def from_mapping(cls, value: object, *, index: int) -> "ReusableArtifactDeclaration":
        """Decode the serialized fields into a validated ReusableArtifactDeclaration record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        field = f"reusable_artifacts[{index}]"
        mapping = _closed_mapping(
            value,
            required=frozenset(
                {
                    "artifact_id",
                    "artifact_kind",
                    "schema_name",
                    "schema_version",
                    "locator",
                    "sha256",
                    "byte_count",
                    "owning_identity",
                    "runtime_versions",
                    "compatibility_signature_sha256",
                }
            ),
            optional=frozenset({"validation_domain_id", "target_sample_ids", "primary_settings"}),
            field=field,
        )
        artifact_kind = _token(mapping["artifact_kind"], f"{field}.artifact_kind")
        if artifact_kind not in _ARTIFACT_KINDS:
            raise BatchManifestError(f"{field}.artifact_kind is not supported")
        identity = _freeze_json(mapping["owning_identity"], f"{field}.owning_identity")
        if not isinstance(identity, Mapping) or not identity:
            raise BatchManifestError(f"{field}.owning_identity must be a non-empty object")
        runtime_values = _mapping(mapping["runtime_versions"], f"{field}.runtime_versions")
        if not runtime_values:
            raise BatchManifestError(f"{field}.runtime_versions must be a non-empty object")
        runtime_versions = {
            _token(key, f"{field}.runtime_versions key"): _text(
                runtime_value, f"{field}.runtime_versions.{key}"
            )
            for key, runtime_value in runtime_values.items()
        }
        return cls(
            target_sample_ids=tuple(_sequence(mapping.get("target_sample_ids", ()), "target_sample_ids")),
            primary_settings=mapping.get("primary_settings"),
            artifact_id=_safe_id(mapping["artifact_id"], f"{field}.artifact_id"),
            artifact_kind=artifact_kind,
            schema_name=_text(mapping["schema_name"], f"{field}.schema_name"),
            schema_version=_text(mapping["schema_version"], f"{field}.schema_version"),
            locator=_path(mapping["locator"], f"{field}.locator"),
            sha256=_sha256(mapping["sha256"], f"{field}.sha256"),
            byte_count=_positive_integer(mapping["byte_count"], f"{field}.byte_count"),
            owning_identity=identity,
            runtime_versions=MappingProxyType(dict(sorted(runtime_versions.items()))),
            validation_domain_id=(
                None
                if mapping.get("validation_domain_id") is None
                else _text(mapping["validation_domain_id"], f"{field}.validation_domain_id")
            ),
            compatibility_signature_sha256=_sha256(
                mapping["compatibility_signature_sha256"], f"{field}.compatibility_signature_sha256"
            ),
        )

    @property
    def declaration_sha256(self) -> str:
        return _digest_mapping(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "locator": str(self.locator),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "owning_identity": _export_json(self.owning_identity),
            "runtime_versions": dict(self.runtime_versions),
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
        }
        if self.target_sample_ids:
            value["target_sample_ids"] = list(self.target_sample_ids)
            value["primary_settings"] = _export_json(self.primary_settings)
        if self.validation_domain_id is not None:
            value["validation_domain_id"] = self.validation_domain_id
        return value


_VALIDATED_REUSABLE_ARTIFACT_CAPABILITY = object()


def _reusable_artifact_authority_material(
    *,
    declaration: ReusableArtifactDeclaration,
    manifest_sha256: str,
    owning_validation_receipt_sha256: str,
    producer_version: str,
    owning_identity_contract_sha256: str,
    manifest_projection_sha256: str,
) -> dict[str, object]:
    return {
        "manifest_sha256": manifest_sha256,
        "declaration_sha256": declaration.declaration_sha256,
        "owning_validation_receipt_sha256": owning_validation_receipt_sha256,
        "producer_version": producer_version,
        "owning_identity_contract_sha256": owning_identity_contract_sha256,
        "manifest_projection_sha256": manifest_projection_sha256,
        "runtime_versions": dict(declaration.runtime_versions),
        "compatibility_signature_sha256": declaration.compatibility_signature_sha256,
    }


@dataclass(frozen=True, slots=True, init=False)
class ValidatedReusableArtifact:
    """Owning-loader receipt for a reusable declaration after scientific validation."""

    declaration: ReusableArtifactDeclaration
    owning_validation_receipt_sha256: str
    producer_version: str
    owning_identity_contract_sha256: str
    manifest_sha256: str
    manifest_projection_sha256: str
    authority_sha256: str

    def __init__(
        self,
        *,
        _capability: object,
        declaration: ReusableArtifactDeclaration,
        owning_validation_receipt_sha256: str,
        producer_version: str,
        owning_identity_contract_sha256: str,
        manifest_sha256: str,
        manifest_projection_sha256: str,
    ) -> None:
        if _capability is not _VALIDATED_REUSABLE_ARTIFACT_CAPABILITY:
            raise BatchManifestError(
                "validated reusable artifacts require the owning-loader issuance hook"
            )
        if not isinstance(declaration, ReusableArtifactDeclaration):
            raise BatchManifestError("declaration must be a ReusableArtifactDeclaration")
        object.__setattr__(self, "declaration", declaration)
        object.__setattr__(
            self,
            "owning_validation_receipt_sha256",
            _sha256(owning_validation_receipt_sha256, "owning_validation_receipt_sha256"),
        )
        object.__setattr__(
            self,
            "producer_version",
            _text(producer_version, "validated_reusable_artifact.producer_version"),
        )
        object.__setattr__(
            self,
            "owning_identity_contract_sha256",
            _sha256(
                owning_identity_contract_sha256,
                "validated_reusable_artifact.owning_identity_contract_sha256",
            ),
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _sha256(
                manifest_sha256,
                "validated_reusable_artifact.manifest_sha256",
            ),
        )
        object.__setattr__(
            self,
            "manifest_projection_sha256",
            _sha256(
                manifest_projection_sha256,
                "validated_reusable_artifact.manifest_projection_sha256",
            ),
        )
        object.__setattr__(
            self,
            "authority_sha256",
            hashlib.sha256(_canonical_bytes(self.authority_material())).hexdigest(),
        )

    def authority_material(self) -> dict[str, object]:
        return _reusable_artifact_authority_material(
            declaration=self.declaration,
            manifest_sha256=self.manifest_sha256,
            owning_validation_receipt_sha256=self.owning_validation_receipt_sha256,
            producer_version=self.producer_version,
            owning_identity_contract_sha256=self.owning_identity_contract_sha256,
            manifest_projection_sha256=self.manifest_projection_sha256,
        )

    def validate_authority_sha256(self) -> None:
        expected = hashlib.sha256(_canonical_bytes(self.authority_material())).hexdigest()
        if self.authority_sha256 != expected:
            raise BatchManifestError("validated reusable artifact authority digest is invalid")

    def validate_manifest_binding(self, manifest: "ValidatedBatchManifest") -> None:
        if not isinstance(manifest, ValidatedBatchManifest):
            raise BatchManifestError("authority manifest must be a ValidatedBatchManifest")
        if (
            self.manifest_sha256 != manifest.normalized_manifest_sha256
            or self.manifest_projection_sha256 != _manifest_projection_sha256(manifest.samples)
        ):
            raise BatchManifestError(
                "validated reusable artifact authority binds different manifest participants"
            )


@dataclass(frozen=True, slots=True)
class BatchManifest:
    """Immutable parsed authoring input with retained exact parser evidence."""

    schema_name: str
    schema_version: str
    batch_id: str
    validation_domain_id: str
    validation_domains: tuple[str, ...]
    output_root: Path
    project_path: Path | None
    create_project_path: Path | None
    shared_analysis_config: Mapping[str, JsonValue]
    execution_config: BatchExecutionConfig
    reusable_artifacts: tuple[ReusableArtifactDeclaration, ...]
    samples: tuple[BatchSample, ...]
    original_manifest_bytes: bytes

    def __post_init__(self) -> None:
        """Validate the BatchManifest fields when the record is constructed.

        The checks cover ``schema_name``, ``schema_version``, ``validation_domain_id``,
        ``shared_analysis_config``, ``execution_config``, ``reusable_artifacts``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        schema_name = _text(self.schema_name, "schema_name")
        schema_version = _text(self.schema_version, "schema_version")
        if schema_name != SCHEMA_NAME:
            raise BatchManifestError(f"schema_name must equal {SCHEMA_NAME!r}")
        if schema_version != SCHEMA_VERSION:
            raise BatchManifestError(f"schema_version must equal {SCHEMA_VERSION!r}")
        try:
            assert_current_writer("mif-batch-manifest", schema_version)
        except ValueError as exc:
            raise BatchManifestError(
                "batch manifest schema is not the current release writer"
            ) from exc
        default_domain = _text(self.validation_domain_id, "validation_domain_id")
        domains = tuple(
            _text(item, f"validation_domains[{index}]")
            for index, item in enumerate(_sequence(self.validation_domains, "validation_domains"))
        )
        if len(set(domains)) != len(domains):
            raise BatchManifestError("validation_domains must not contain duplicates")
        shared_config = _freeze_json(self.shared_analysis_config, "shared_analysis_config")
        if not isinstance(shared_config, Mapping):
            raise BatchManifestError("shared_analysis_config must be an object")
        if not isinstance(self.execution_config, BatchExecutionConfig):
            raise BatchManifestError("execution_config must be a BatchExecutionConfig")
        artifacts = tuple(_sequence(self.reusable_artifacts, "reusable_artifacts"))
        if len(artifacts) > MAX_REUSABLE_ARTIFACTS:
            raise BatchManifestError(f"reusable_artifacts must not exceed {MAX_REUSABLE_ARTIFACTS}")
        if not all(isinstance(item, ReusableArtifactDeclaration) for item in artifacts):
            raise BatchManifestError(
                "reusable_artifacts must contain ReusableArtifactDeclaration values"
            )
        samples = tuple(_sequence(self.samples, "samples"))
        if not samples:
            raise BatchManifestError("samples must not be empty")
        if len(samples) > MAX_SAMPLES:
            raise BatchManifestError(f"samples must not exceed {MAX_SAMPLES}")
        if not all(isinstance(item, BatchSample) for item in samples):
            raise BatchManifestError("samples must contain BatchSample values")
        if type(self.original_manifest_bytes) is not bytes:
            raise BatchManifestError("original_manifest_bytes must be bytes")
        object.__setattr__(self, "schema_name", schema_name)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "batch_id", _safe_id(self.batch_id, "batch_id"))
        object.__setattr__(self, "validation_domain_id", default_domain)
        object.__setattr__(self, "validation_domains", tuple(sorted({default_domain, *domains})))
        object.__setattr__(self, "output_root", _path(self.output_root, "output_root"))
        object.__setattr__(
            self,
            "project_path",
            None if self.project_path is None else _path(self.project_path, "project_path"),
        )
        object.__setattr__(
            self,
            "create_project_path",
            (
                None
                if self.create_project_path is None
                else _path(self.create_project_path, "create_project_path")
            ),
        )
        object.__setattr__(self, "shared_analysis_config", shared_config)
        object.__setattr__(self, "reusable_artifacts", artifacts)
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "original_manifest_bytes", bytes(self.original_manifest_bytes))

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "BatchManifest":
        if not isinstance(data, bytes):
            raise BatchManifestError("manifest input must be bytes")
        mapping = _closed_mapping(
            _parse_json(bytes(data)),
            required=frozenset(
                {
                    "schema_name",
                    "schema_version",
                    "batch_id",
                    "validation_domain_id",
                    "output_root",
                    "shared_analysis_config",
                    "execution_config",
                    "samples",
                }
            ),
            optional=frozenset(
                {"validation_domains", "project_path", "create_project_path", "reusable_artifacts"}
            ),
            field="manifest",
        )
        schema_name = _text(mapping["schema_name"], "schema_name")
        schema_version = _text(mapping["schema_version"], "schema_version")
        if schema_name != SCHEMA_NAME:
            raise BatchManifestError(f"schema_name must equal {SCHEMA_NAME!r}")
        if schema_version != SCHEMA_VERSION:
            raise BatchManifestError(f"schema_version must equal {SCHEMA_VERSION!r}")
        try:
            assert_current_writer("mif-batch-manifest", schema_version)
        except ValueError as exc:
            raise BatchManifestError(
                "batch manifest schema is not the current release writer"
            ) from exc
        domains = tuple(
            _text(item, f"validation_domains[{index}]")
            for index, item in enumerate(
                _sequence(mapping.get("validation_domains", ()), "validation_domains")
            )
        )
        default_domain = _text(mapping["validation_domain_id"], "validation_domain_id")
        if len(set(domains)) != len(domains):
            raise BatchManifestError("validation_domains must not contain duplicates")
        all_domains = tuple(sorted({default_domain, *domains}))
        shared_config = _freeze_json(mapping["shared_analysis_config"], "shared_analysis_config")
        if not isinstance(shared_config, Mapping):
            raise BatchManifestError("shared_analysis_config must be an object")
        samples = tuple(
            BatchSample.from_mapping(item, index=index)
            for index, item in enumerate(_sequence(mapping["samples"], "samples"))
        )
        if not samples:
            raise BatchManifestError("samples must not be empty")
        if len(samples) > MAX_SAMPLES:
            raise BatchManifestError(f"samples must not exceed {MAX_SAMPLES}")
        artifacts = tuple(
            ReusableArtifactDeclaration.from_mapping(item, index=index)
            for index, item in enumerate(
                _sequence(mapping.get("reusable_artifacts", ()), "reusable_artifacts")
            )
        )
        if len(artifacts) > MAX_REUSABLE_ARTIFACTS:
            raise BatchManifestError(f"reusable_artifacts must not exceed {MAX_REUSABLE_ARTIFACTS}")
        return cls(
            schema_name=schema_name,
            schema_version=schema_version,
            batch_id=_safe_id(mapping["batch_id"], "batch_id"),
            validation_domain_id=default_domain,
            validation_domains=all_domains,
            output_root=_path(mapping["output_root"], "output_root"),
            project_path=(
                None
                if mapping.get("project_path") is None
                else _path(mapping["project_path"], "project_path")
            ),
            create_project_path=(
                None
                if mapping.get("create_project_path") is None
                else _path(mapping["create_project_path"], "create_project_path")
            ),
            shared_analysis_config=shared_config,
            execution_config=BatchExecutionConfig.from_mapping(mapping["execution_config"]),
            reusable_artifacts=artifacts,
            samples=samples,
            original_manifest_bytes=bytes(data),
        )

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "validation_domain_id": self.validation_domain_id,
            "output_root": str(self.output_root),
            "shared_analysis_config": _export_json(self.shared_analysis_config),
            "execution_config": self.execution_config.to_mapping(),
            "samples": [
                item.to_mapping() for item in sorted(self.samples, key=lambda item: item.sample_id)
            ],
        }
        nondefault_domains = [
            item for item in self.validation_domains if item != self.validation_domain_id
        ]
        if nondefault_domains:
            value["validation_domains"] = nondefault_domains
        if self.project_path is not None:
            value["project_path"] = str(self.project_path)
        if self.create_project_path is not None:
            value["create_project_path"] = str(self.create_project_path)
        if self.reusable_artifacts:
            value["reusable_artifacts"] = [
                item.to_mapping()
                for item in sorted(self.reusable_artifacts, key=lambda item: item.artifact_id)
            ]
        return value


@dataclass(frozen=True, slots=True)
class ResolvedBatchSample:
    """One sample after batch-owned fields and strict current configuration resolution."""

    sample_id: str
    slide_id: str
    subject_id: str
    source_mif_path: Path
    reference: PairedReferenceImage | None
    validation_domain_id: str
    enabled: bool
    sample_output_root: Path
    config: AnalysisConfig
    resolved_config_sha256: str
    phenotyping_group_id: str | None = None
    source_image_index: int | None = None
    source_series_index: int | None = None
    source_file_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.phenotyping_group_id is not None:
            raise BatchManifestError("phenotyping_group_id is a reserved null field")
        _validate_source_selection_pair(self.source_image_index, self.source_series_index)
        _validate_source_file_hash(self.source_file_sha256, self.source_image_index)


def _manifest_projection_sha256(samples: tuple[ResolvedBatchSample, ...]) -> str:
    if not isinstance(samples, tuple) or not all(
        isinstance(item, ResolvedBatchSample) for item in samples
    ):
        raise BatchManifestError(
            "validated manifest projection inventory requires resolved samples"
        )
    if len({item.sample_id for item in samples}) != len(samples):
        raise BatchManifestError("validated manifest projection inventory has duplicate sample IDs")
    material = [
        {
            "sample_id": item.sample_id,
            "slide_id": item.slide_id,
            "validation_domain_id": item.validation_domain_id,
            "enabled": item.enabled,
            "resolved_config_sha256": item.resolved_config_sha256,
        }
        for item in sorted(samples, key=lambda item: item.sample_id)
    ]
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class _PathInventoryItem:
    role: str
    path: Path


@dataclass(frozen=True, slots=True)
class ValidatedBatchManifest:
    """Static validation result retaining both manifest representations and hashes."""

    manifest: BatchManifest
    original_manifest_bytes: bytes
    original_manifest_sha256: str
    normalized_manifest_bytes: bytes
    normalized_manifest_sha256: str
    samples: tuple[ResolvedBatchSample, ...]
    reusable_artifacts: tuple[ValidatedReusableArtifact, ...]

    @property
    def enabled_samples(self) -> tuple[ResolvedBatchSample, ...]:
        return tuple(item for item in self.samples if item.enabled)


def issue_validated_reusable_artifact(
    *,
    manifest: ValidatedBatchManifest,
    declaration: ReusableArtifactDeclaration,
    owning_validation_receipt_sha256: str,
    producer_version: str,
    owning_identity_contract_sha256: str,
) -> ValidatedReusableArtifact:
    """Bind a loader's acceptance to exact retained manifest selections.

    The owning loader validates the selected artifact's current bytes before
    calling this function. Reconstructing its manifest binding does not reopen
    unrelated external inputs or repeat fresh workspace admission.
    """
    if not isinstance(manifest, ValidatedBatchManifest):
        raise BatchManifestError("manifest must be a ValidatedBatchManifest")
    authoritative = revalidate_retained_manifest(
        manifest.original_manifest_bytes, manifest.normalized_manifest_bytes
    )
    normalized = authoritative.normalized_manifest_bytes
    if (
        manifest.manifest != authoritative.manifest
        or manifest.normalized_manifest_bytes != normalized
        or manifest.normalized_manifest_sha256 != authoritative.normalized_manifest_sha256
        or manifest.original_manifest_bytes != authoritative.original_manifest_bytes
        or manifest.original_manifest_sha256 != authoritative.original_manifest_sha256
        or manifest.samples != authoritative.samples
    ):
        raise BatchManifestError(
            "owning loader requires manifest evidence derived from retained original bytes"
        )
    if not isinstance(declaration, ReusableArtifactDeclaration):
        raise BatchManifestError("declaration must be a ReusableArtifactDeclaration")
    if declaration not in manifest.manifest.reusable_artifacts:
        raise BatchManifestError("owning loader may issue only an exact manifest declaration")
    return ValidatedReusableArtifact(
        _capability=_VALIDATED_REUSABLE_ARTIFACT_CAPABILITY,
        declaration=declaration,
        owning_validation_receipt_sha256=owning_validation_receipt_sha256,
        producer_version=producer_version,
        owning_identity_contract_sha256=owning_identity_contract_sha256,
        manifest_sha256=manifest.normalized_manifest_sha256,
        manifest_projection_sha256=_manifest_projection_sha256(manifest.samples),
    )


def _assert_existing_file(path: Path, field: str) -> None:
    if not path.is_file():
        raise BatchManifestError(f"{field} does not exist as a file: {path}")


def _assert_owned_path(output_root: Path, path: Path, field: str) -> Path:
    root = output_root.resolve(strict=False)
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BatchManifestError(f"{field} must remain within output_root") from exc
    return candidate


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise BatchManifestError(f"reusable artifact cannot be read: {path}") from exc
    return digest.hexdigest()


def _validate_schema(manifest: BatchManifest) -> None:
    if manifest.schema_name != SCHEMA_NAME or manifest.schema_version != SCHEMA_VERSION:
        raise BatchManifestError("manifest does not use the current batch schema")


def _validate_unique_ids(samples: tuple[BatchSample, ...]) -> None:
    sample_ids = [item.sample_id for item in samples]
    slide_ids = [item.slide_id for item in samples]
    if len(set(sample_ids)) != len(sample_ids):
        raise BatchManifestError("duplicate sample_id values are not allowed")
    if len(set(slide_ids)) != len(slide_ids):
        raise BatchManifestError("duplicate slide_id values are not allowed")
    if len({item.sample_id.casefold() for item in samples}) != len(samples):
        raise BatchManifestError("sample output paths would collide")


def _validate_project_choice(manifest: BatchManifest) -> None:
    """Check the saved project selection and containment of a create target."""
    if (manifest.project_path is None) == (manifest.create_project_path is None):
        raise BatchManifestError("exactly one of project_path or create_project_path is required")
    if manifest.create_project_path is not None:
        _assert_owned_path(
            manifest.output_root, manifest.create_project_path, "create_project_path"
        )


def _validate_project_admission(
    manifest: BatchManifest, *, allow_materialized_create_target: bool = False
) -> None:
    """Check the selected project's availability when admitting a manifest."""
    if manifest.project_path is not None:
        _assert_existing_file(manifest.project_path, "project_path")
    if manifest.create_project_path is not None:
        if manifest.create_project_path.exists() and not allow_materialized_create_target:
            raise BatchManifestError("create_project_path must not already exist")


def _sample_output_root(manifest: BatchManifest, sample: BatchSample) -> Path:
    """Retain independent processing files for each admitted source selection."""
    if sample.processing_root is not None:
        return sample.processing_root
    selection = {
        "source_mif_path": str(sample.source_mif_path),
        "source_image_index": sample.source_image_index,
        "source_series_index": sample.source_series_index,
        "source_file_sha256": sample.source_file_sha256,
    }
    generation = hashlib.sha256(_canonical_bytes(selection)).hexdigest()
    return (manifest.output_root / manifest.batch_id / "samples" / sample.sample_id
            / "sources" / generation)


def operational_sample_output_root(sample: ResolvedBatchSample, workspace_root: Path) -> Path:
    """Place new files in this workspace without changing the retained sample.

    Retained samples own either a sample directory or a source revision below
    it. Imported analyses keep that exact suffix and their original scientific
    configuration, while new publications use the chosen current workspace.
    Publication owners create and retain the actual directory ancestry.
    """
    if type(sample) is not ResolvedBatchSample or not isinstance(workspace_root, Path):
        raise BatchManifestError("operational output requires a resolved sample and workspace")
    saved_root = sample.sample_output_root
    if saved_root.parts[-2:] == ("samples", sample.sample_id):
        suffix = Path(*saved_root.parts[-2:])
    elif saved_root.parts[-4:-1] == ("samples", sample.sample_id, "sources"):
        suffix = Path(*saved_root.parts[-4:])
    else:
        raise BatchManifestError("operational output differs from the sample's owned namespace")
    if (
        not workspace_root.is_absolute()
        or workspace_root != workspace_root.resolve(strict=False)
        or not saved_root.is_absolute()
        or len(saved_root.parts) <= len(suffix.parts)
        or saved_root.parents[len(suffix.parts) - 1].name != workspace_root.name
    ):
        raise BatchManifestError("operational output differs from the sample's owned namespace")
    return _assert_owned_path(workspace_root, workspace_root / suffix, "operational sample output")


def _validate_sample_source_files(sample: BatchSample) -> None:
    """Require selected image files when admitting a newly authored manifest."""
    _assert_existing_file(sample.source_mif_path, f"sample {sample.sample_id} source_mif_path")
    from mif_pipeline.io.aligned_collection import is_aligned_source_collection, load_aligned_source_collection
    if is_aligned_source_collection(sample.source_mif_path):
        from mif_pipeline.io.backends import snapshot_opened_source_identity
        try:
            for member in load_aligned_source_collection(sample.source_mif_path).members:
                snapshot_opened_source_identity(member.path, retained=member.identity)
        except (OSError, ValueError) as error:
            raise BatchManifestError(f"sample {sample.sample_id} aligned source member is unavailable or changed: {error}") from error
    if sample.reference is not None:
        _assert_existing_file(
            sample.reference.source_path, f"sample {sample.sample_id} reference source_path"
        )


def _resolve_sample(manifest: BatchManifest, sample: BatchSample) -> ResolvedBatchSample:
    """Resolve saved sample configuration and owned paths without opening its sources.

    Retained failures must remain readable when a source is unavailable. Fresh
    admission checks file presence separately; image readers check access and
    identity again when executing the selected operation.
    """
    domain = sample.validation_domain_id or manifest.validation_domain_id
    if domain not in manifest.validation_domains:
        raise BatchManifestError(f"sample {sample.sample_id} validation domain is not declared")
    owner_root = manifest.output_root if sample.processing_root is None else sample.processing_root.parents[4]
    sample_root = _assert_owned_path(
        owner_root,
        _sample_output_root(manifest, sample),
        f"sample {sample.sample_id} output root",
    )
    config_mapping = _export_json(manifest.shared_analysis_config)
    if not isinstance(config_mapping, Mapping):
        raise BatchManifestError("shared_analysis_config must be an object")
    if (
        "schema_version" in config_mapping
        and config_mapping["schema_version"] != ANALYSIS_CONFIG_SCHEMA_VERSION
    ):
        raise BatchManifestError(
            f"sample {sample.sample_id} shared analysis config schema_version must equal "
            f"{ANALYSIS_CONFIG_SCHEMA_VERSION!r}, got {config_mapping['schema_version']!r}"
        )
    resolved_mapping = dict(config_mapping)
    resolved_mapping.update(_export_json(sample.overrides))
    processed_root = sample_root / "preprocessing"
    resolved_mapping.update(
        {
            "schema_version": ANALYSIS_CONFIG_SCHEMA_VERSION,
            "slide_id": sample.slide_id,
            "input_image": str(processed_root / "analysis-ready.ome.zarr"),
            "provenance": str(processed_root / "analysis-ready.provenance.json"),
            "output_dir": str(sample_root / "analysis"),
        }
    )
    try:
        config = AnalysisConfig.from_mapping(resolved_mapping)
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchManifestError(
            f"sample {sample.sample_id} resolved config v{ANALYSIS_CONFIG_SCHEMA_VERSION} is invalid: {exc}"
        ) from exc
    for field, path in (
        ("input_image", config.input_image),
        ("provenance", config.provenance),
        ("output_dir", config.output_dir),
    ):
        _assert_owned_path(owner_root, path, f"sample {sample.sample_id} {field}")
    return ResolvedBatchSample(
        sample_id=sample.sample_id,
        slide_id=sample.slide_id,
        subject_id=sample.subject_id,
        source_mif_path=sample.source_mif_path,
        reference=sample.reference,
        validation_domain_id=domain,
        enabled=sample.enabled,
        sample_output_root=sample_root,
        config=config,
        resolved_config_sha256=_digest_mapping(config.to_mapping()),
        phenotyping_group_id=sample.phenotyping_group_id,
        source_image_index=sample.source_image_index,
        source_series_index=sample.source_series_index,
        source_file_sha256=sample.source_file_sha256,
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False


def _assert_target_directory_ancestry(item: _PathInventoryItem) -> None:
    ancestor = item.path.parent
    while ancestor != ancestor.parent:
        if ancestor.exists() and not ancestor.is_dir():
            raise BatchManifestError(
                f"{item.role} has invalid file/directory ancestry at {ancestor}"
            )
        ancestor = ancestor.parent


def _validate_path_inventory(
    manifest: BatchManifest, samples: tuple[ResolvedBatchSample, ...]
) -> None:
    """Reject all aliases between generated targets and external input locators."""
    targets: list[_PathInventoryItem] = []
    if manifest.create_project_path is not None:
        targets.append(
            _PathInventoryItem(
                "create_project_path", manifest.create_project_path.resolve(strict=False)
            )
        )
    inputs: list[_PathInventoryItem] = []
    if manifest.project_path is not None:
        inputs.append(
            _PathInventoryItem("project_path", manifest.project_path.resolve(strict=False))
        )
    for sample in samples:
        targets.extend(
            (
                _PathInventoryItem(
                    f"sample {sample.sample_id} input_image",
                    sample.config.input_image.resolve(strict=False),
                ),
                _PathInventoryItem(
                    f"sample {sample.sample_id} provenance",
                    sample.config.provenance.resolve(strict=False),
                ),
                _PathInventoryItem(
                    f"sample {sample.sample_id} output_dir",
                    sample.config.output_dir.resolve(strict=False),
                ),
            )
        )
        inputs.append(
            _PathInventoryItem(
                f"sample {sample.sample_id} source_mif_path",
                sample.source_mif_path.resolve(strict=False),
            )
        )
        from mif_pipeline.io.aligned_collection import is_aligned_source_collection, load_aligned_source_collection
        if is_aligned_source_collection(sample.source_mif_path) and sample.source_mif_path.is_file():
            inputs.extend(_PathInventoryItem(f"sample {sample.sample_id} aligned member {index}",
                member.path.resolve(strict=False)) for index, member in enumerate(
                    load_aligned_source_collection(sample.source_mif_path).members))
        if sample.reference is not None:
            inputs.append(
                _PathInventoryItem(
                    f"sample {sample.sample_id} reference source_path",
                    sample.reference.source_path.resolve(strict=False),
                )
            )
    inputs.extend(
        _PathInventoryItem(
            f"reusable artifact {declaration.artifact_id} locator",
            declaration.locator.resolve(strict=False),
        )
        for declaration in manifest.reusable_artifacts
    )
    for target in targets:
        _assert_owned_path(manifest.output_root, target.path, target.role)
        _assert_target_directory_ancestry(target)
    for index, first in enumerate(targets):
        for second in targets[index + 1 :]:
            if _paths_overlap(first.path, second.path):
                raise BatchManifestError(
                    f"path inventory target-target alias: {first.role} and {second.role}"
                )
        for input_item in inputs:
            if _paths_overlap(first.path, input_item.path):
                raise BatchManifestError(
                    f"path inventory target-input alias: {first.role} and {input_item.role}"
                )


def _validate_reusable_declarations(
    declarations: tuple[ReusableArtifactDeclaration, ...], validation_domains: tuple[str, ...]
) -> None:
    """Check declaration identities and domains without opening external files."""
    artifact_ids = [item.artifact_id for item in declarations]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise BatchManifestError("reusable artifact IDs must be unique")
    for declaration in declarations:
        if (
            declaration.validation_domain_id is not None
            and declaration.validation_domain_id not in validation_domains
        ):
            raise BatchManifestError(
                f"reusable artifact {declaration.artifact_id} validation domain is not declared"
            )


def _validate_reusable_artifact_files(
    declarations: tuple[ReusableArtifactDeclaration, ...],
) -> None:
    """Check complete declared file bytes during fresh manifest admission."""
    for declaration in declarations:
        _assert_existing_file(
            declaration.locator, f"reusable artifact {declaration.artifact_id} locator"
        )
        if declaration.locator.stat().st_size != declaration.byte_count:
            raise BatchManifestError(
                f"reusable artifact {declaration.artifact_id} byte_count does not match the file"
            )
        if _stream_sha256(declaration.locator) != declaration.sha256:
            raise BatchManifestError(
                f"reusable artifact {declaration.artifact_id} sha256 does not match the complete file"
            )


def validate_manifest(
    manifest: BatchManifest, *, allow_materialized_create_target: bool = False
) -> ValidatedBatchManifest:
    """Resolve metadata; saved-workspace callers authenticate retained state separately.

    Fresh planning and execution retain the default absent-project requirement.
    The opt-in flag permits read-only examination of a previously created project.
    """
    if type(allow_materialized_create_target) is not bool:
        raise TypeError("The saved-project selection must be a bool")
    if not isinstance(manifest, BatchManifest):
        raise BatchManifestError("manifest must be a BatchManifest")
    _validate_schema(manifest)
    _validate_unique_ids(manifest.samples)
    _validate_project_choice(manifest)
    _validate_project_admission(
        manifest, allow_materialized_create_target=allow_materialized_create_target
    )
    for sample in manifest.samples:
        _validate_sample_source_files(sample)
    resolved = tuple(_resolve_sample(manifest, sample) for sample in manifest.samples)
    _validate_reusable_declarations(manifest.reusable_artifacts, manifest.validation_domains)
    _validate_reusable_artifact_files(manifest.reusable_artifacts)
    _validate_path_inventory(manifest, resolved)
    normalized = _canonical_bytes(manifest.to_mapping())
    return ValidatedBatchManifest(
        manifest=manifest,
        original_manifest_bytes=manifest.original_manifest_bytes,
        original_manifest_sha256=hashlib.sha256(manifest.original_manifest_bytes).hexdigest(),
        normalized_manifest_bytes=normalized,
        normalized_manifest_sha256=hashlib.sha256(normalized).hexdigest(),
        samples=resolved,
        # A checksum declaration is not an owning-loader validation receipt.
        reusable_artifacts=(),
    )


def revalidate_retained_manifest(
    original_manifest_bytes: bytes,
    normalized_manifest_bytes: bytes,
) -> ValidatedBatchManifest:
    """Reconstruct retained selections even when external inputs are unavailable.

    Schema, configuration, path ownership and exact normalized bytes remain
    checked. Fresh admission checks external files separately; the selected
    image, reusable-artifact and project consumers validate current material
    before use. This reconstruction does not authorize their execution.
    """

    manifest = BatchManifest.from_json_bytes(original_manifest_bytes)
    _validate_schema(manifest)
    _validate_unique_ids(manifest.samples)
    _validate_project_choice(manifest)
    resolved = tuple(_resolve_sample(manifest, sample) for sample in manifest.samples)
    _validate_reusable_declarations(manifest.reusable_artifacts, manifest.validation_domains)
    _validate_path_inventory(manifest, resolved)
    normalized = _canonical_bytes(manifest.to_mapping())
    if normalized != normalized_manifest_bytes:
        raise BatchManifestError("retained normalized manifest bytes changed")
    return ValidatedBatchManifest(
        manifest=manifest,
        original_manifest_bytes=original_manifest_bytes,
        original_manifest_sha256=hashlib.sha256(original_manifest_bytes).hexdigest(),
        normalized_manifest_bytes=normalized,
        normalized_manifest_sha256=hashlib.sha256(normalized).hexdigest(),
        samples=resolved,
        reusable_artifacts=(),
    )


def manifest_generation_paths(directory: Path, normalized_sha256: str) -> tuple[Path, Path]:
    """Select immutable original/normalized files using their existing manifest digest."""
    if not isinstance(directory, Path) or not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise BatchManifestError("retained manifest selection is invalid")
    generation = directory / normalized_sha256
    if generation.is_symlink():
        raise BatchManifestError("retained manifest generation is linked")
    return generation / "original.json", generation / "normalized.json"


def read_retained_manifest(directory: Path, normalized_sha256: str) -> ValidatedBatchManifest:
    """Reopen one head, journal or admission-plan manifest without changing its selection."""
    original, normalized = manifest_generation_paths(directory, normalized_sha256)
    if original.is_symlink() or normalized.is_symlink():
        raise BatchManifestError("retained manifest files are linked")
    manifest = revalidate_retained_manifest(original.read_bytes(), normalized.read_bytes())
    if manifest.normalized_manifest_sha256 != normalized_sha256:
        raise BatchManifestError("retained manifest generation and content differ")
    return manifest






def is_memory_limit_change(previous: ValidatedBatchManifest, current: ValidatedBatchManifest) -> bool:
    """Recognize an existing memory-only revision without authorizing new ones."""
    before, after = previous.manifest.to_mapping(), current.manifest.to_mapping()
    old = before["execution_config"].pop("max_working_set_bytes", None)
    new = after["execution_config"].pop("max_working_set_bytes", None)
    return old != new and before == after



__all__ = [
    "BatchExecutionConfig",
    "BatchManifest",
    "BatchManifestError",
    "BatchSample",
    "MAX_MANIFEST_BYTES",
    "PairedReferenceImage",
    "ReusableArtifactDeclaration",
    "ResolvedBatchSample",
    "operational_sample_output_root",
    "ValidatedBatchManifest",
    "ValidatedReusableArtifact",
    "issue_validated_reusable_artifact",
    "revalidate_retained_manifest",
    "manifest_generation_paths",
    "read_retained_manifest",
    "validate_manifest",
]
