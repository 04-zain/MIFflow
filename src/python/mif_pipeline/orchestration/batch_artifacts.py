"""Immutable identity-only artifact edges for sample-level batch execution.

This module owns orchestration contracts only. Artifact bodies remain in their owning stores
and scientific execution remains in the owning stage adapters.

The `BatchArtifactRef` record carries `artifact_id`, `artifact_kind`, `schema_name`,
`schema_version`, `origin` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from types import MappingProxyType
from typing import Protocol, TypeAlias, runtime_checkable

from mif_pipeline.pipeline_core import (
    ROIAnalysisError,
    assert_current_writer,
    validate_safe_path_segment,
)
from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2

from .batch_manifest import (
    BatchManifestError,
    ValidatedBatchManifest,
    ValidatedReusableArtifact,
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_SCHEMA_NAME_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
_MAX_IDENTITY_DEPTH = 16
_MAX_IDENTITY_STRING_LENGTH = 65_536
_MAX_RUNTIME_BINDINGS = 128
_MAX_TARGET_BINDINGS = 10_000
_MAX_SIGNED_INTEGER = (1 << 63) - 1
_RESOURCE_KINDS = frozenset(
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
_FORBIDDEN_BODY_KEYS = frozenset(
    {
        "cells",
        "cell_table",
        "ome",
        "objects",
        "path_objects",
        "pathobjects",
        "payload",
        "pixels",
        "roi_table",
        "rois",
        "rows",
    }
)
_BATCH_COMMITTED_ARTIFACT_AUTHORITY_ISSUER = object()
_BATCH_CURRENT_EXECUTION_AUTHORITY_ISSUER = object()

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


# ==============================================================================
#                                Batch artifacts
# ==============================================================================


class BatchArtifactError(ROIAnalysisError):
    """Raised when a batch artifact edge or adapter contract is invalid."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise BatchArtifactError(f"{field} must be non-empty canonical text")
    if len(value) > _MAX_IDENTITY_STRING_LENGTH:
        raise BatchArtifactError(f"{field} exceeds the bounded text limit")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _safe_id(value: object, field: str) -> str:
    try:
        return validate_safe_path_segment(value, field, error_type=BatchArtifactError)
    except ROIAnalysisError as exc:
        raise BatchArtifactError(str(exc)) from exc


def _optional_safe_id(value: object, field: str) -> str | None:
    return None if value is None else _safe_id(value, field)


def _token(value: object, field: str) -> str:
    token = _text(value, field)
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise BatchArtifactError(f"{field} must be a lowercase bounded token")
    return token


def _identity_field_path(value: object, field: str) -> str:
    path = _text(value, field)
    parts = path.split(".")
    if not parts or any(_TOKEN_PATTERN.fullmatch(part) is None for part in parts):
        raise BatchArtifactError(f"{field} must be a dotted lowercase identity path")
    return path


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _optional_sha256(value: object, field: str) -> str | None:
    return None if value is None else _sha256(value, field)


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BatchArtifactError(f"{field} must be a non-negative integer")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return value


def _closed_mapping(
    value: object,
    *,
    fields: frozenset[str],
    field: str,
) -> Mapping[str, object]:
    mapping = _mapping(value, field)
    unknown = sorted(set(mapping) - fields)
    missing = sorted(fields - set(mapping))
    if unknown:
        raise BatchArtifactError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchArtifactError(f"{field} lacks required field(s): {', '.join(missing)}")
    return mapping


def _freeze_json(
    value: object,
    field: str,
    *,
    depth: int = 0,
) -> JsonValue:
    if depth > _MAX_IDENTITY_DEPTH:
        raise BatchArtifactError(f"{field} exceeds the identity nesting limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > _MAX_SIGNED_INTEGER:
            raise BatchArtifactError(f"{field} contains an out-of-range integer")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise BatchArtifactError(f"{field} contains a non-finite number")
        return value
    if type(value) is str:
        if len(value) > _MAX_IDENTITY_STRING_LENGTH:
            raise BatchArtifactError(f"{field} contains an overlong string")
        return value
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise BatchArtifactError(f"{field} mapping keys must be strings")
        frozen: dict[str, JsonValue] = {}
        for key in sorted(value):
            if len(key) > _MAX_IDENTITY_STRING_LENGTH:
                raise BatchArtifactError(f"{field} contains an overlong key")
            if key.casefold() in _FORBIDDEN_BODY_KEYS:
                raise BatchArtifactError(f"{field} must not contain artifact body field {key!r}")
            frozen[key] = _freeze_json(
                value[key], f"{field}.{key}", depth=depth + 1
            )
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item, field, depth=depth + 1) for item in value)
    raise BatchArtifactError(f"{field} must contain only finite JSON values")


def _identity_mapping(value: object, field: str) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, field)
    if not isinstance(frozen, Mapping) or not frozen:
        raise BatchArtifactError(f"{field} must be a non-empty object")
    return frozen


def _identity_leaf_paths(
    value: JsonValue | Mapping[str, JsonValue], prefix: str = ""
) -> frozenset[str]:
    paths: set[str] = set()
    if isinstance(value, Mapping):
        if not value and prefix:
            paths.add(prefix)
        for key, item in value.items():
            path = key if not prefix else f"{prefix}.{key}"
            paths.update(_identity_leaf_paths(item, path))
    elif isinstance(value, tuple):
        if not value:
            paths.add(prefix)
        for item in value:
            paths.update(_identity_leaf_paths(item, prefix))
    else:
        paths.add(prefix)
    return frozenset(paths)


def _project_identity_path(
    value: JsonValue | Mapping[str, JsonValue],
    identity_path: str,
    field: str,
    *,
    allow_null_ancestors: bool = False,
) -> JsonValue:
    """Project a declared path while preserving required identity structure.

    Optional paths may cross a null ancestor in a discriminated record, such as
    an analysis-domain parent whose classifier table is null. Missing keys and
    non-null scalar ancestors remain errors. Required and equivalence paths use
    the strict default.
    """
    parts = tuple(identity_path.split("."))

    def project(
        current: JsonValue | Mapping[str, JsonValue],
        remaining: tuple[str, ...],
    ) -> JsonValue:
        if not remaining:
            return current
        if current is None and allow_null_ancestors:
            return None
        if isinstance(current, Mapping):
            part = remaining[0]
            if part not in current:
                raise BatchArtifactError(f"{field} path is absent from identity")
            return project(current[part], remaining[1:])
        if isinstance(current, tuple):
            return tuple(project(item, remaining) for item in current)
        raise BatchArtifactError(f"{field} path crosses a scalar identity value")

    return project(value, parts)


def _equivalence_paths(
    value: tuple[tuple[str, str], ...], field: str
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple):
        raise BatchArtifactError(f"{field} must be an exact tuple of artifact/path pairs")
    normalized: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise BatchArtifactError(f"{field} must contain exact artifact/path pairs")
        artifact_kind, identity_path = item
        normalized.append(
            (
                _token(artifact_kind, f"{field} artifact kind"),
                _identity_field_path(identity_path, f"{field} identity path"),
            )
        )
    paths = tuple(sorted(normalized))
    if len(set(paths)) != len(paths):
        raise BatchArtifactError(f"{field} must contain unique artifact/path pairs")
    return paths


def _export_json(value: JsonValue | Mapping[str, object]) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("identity must have a finite canonical JSON form") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _runtime_versions(value: object, field: str) -> Mapping[str, str]:
    mapping = _mapping(value, field)
    if len(mapping) > _MAX_RUNTIME_BINDINGS:
        raise BatchArtifactError(f"{field} exceeds the runtime binding limit")
    parsed = {
        _token(key, f"{field} key"): _text(item, f"{field}.{key}") for key, item in mapping.items()
    }
    return MappingProxyType(dict(sorted(parsed.items())))


def _assert_current_schema(name: object, version: object, field: str) -> tuple[str, int]:
    """Validate identity metadata against the current owning artifact schema."""
    schema_name = _text(name, f"{field}.schema_name")
    if _SCHEMA_NAME_PATTERN.fullmatch(schema_name) is None:
        raise BatchArtifactError(f"{field}.schema_name must be a lowercase bounded schema name")
    schema_version = _positive_integer(version, f"{field}.schema_version")
    try:
        assert_current_writer(schema_name, str(schema_version))
    except ValueError as exc:
        raise BatchArtifactError(f"{field} must name a current owning artifact schema") from exc
    return schema_name, schema_version


def _optional_locator(value: object, field: str) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        path = value
    else:
        path = Path(_text(value, field))
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise BatchArtifactError(f"{field} must be a safe absolute non-root locator")
    if any(ord(character) < 32 for character in str(path)):
        raise BatchArtifactError(f"{field} contains unsafe control characters")
    return path


class ArtifactOrigin(str, Enum):
    INTERNAL_NODE = "internal_node"
    MANIFEST_EXTERNAL = "manifest_external"


class ArtifactMultiplicity(str, Enum):
    """Closed cardinality contract for one adapter input artifact kind."""

    SINGLE = "single"
    PER_PARTICIPANT = "per_participant"
    EXACT_SET = "exact_set"


class DispatchMode(str, Enum):
    SINGLE = "single"
    QUPATH_PROJECT_CURRENT_IMAGE = "qupath_project_current_image"


@dataclass(frozen=True, slots=True)
class ArtifactIdentityContract:
    """Owner-controlled allow-list for one artifact identity projection."""

    contract_id: str
    artifact_kind: str
    schema_name: str
    schema_version: int
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    required_runtime_names: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_id", _text(self.contract_id, "identity contract_id"))
        object.__setattr__(
            self, "artifact_kind", _token(self.artifact_kind, "identity artifact_kind")
        )
        name, version = _assert_current_schema(
            self.schema_name, self.schema_version, "identity contract"
        )
        required = tuple(
            sorted(
                _identity_field_path(item, "required identity field")
                for item in self.required_fields
            )
        )
        optional = tuple(
            sorted(
                _identity_field_path(item, "optional identity field")
                for item in self.optional_fields
            )
        )
        runtimes = tuple(
            sorted(_token(item, "required runtime name") for item in self.required_runtime_names)
        )
        if not required or len(set(required)) != len(required):
            raise BatchArtifactError("identity contract requires unique required fields")
        if len(set(optional)) != len(optional) or set(required) & set(optional):
            raise BatchArtifactError(
                "identity contract optional fields must be unique and disjoint"
            )
        if not runtimes or len(set(runtimes)) != len(runtimes):
            raise BatchArtifactError("identity contract requires unique runtime names")
        object.__setattr__(self, "schema_name", name)
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(self, "required_fields", required)
        object.__setattr__(self, "optional_fields", optional)
        object.__setattr__(self, "required_runtime_names", runtimes)

    def validate(
        self,
        *,
        artifact_kind: str,
        schema_name: str,
        schema_version: int,
        owning_identity: Mapping[str, object],
        runtime_versions: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        if artifact_kind != self.artifact_kind:
            raise BatchArtifactError("artifact kind does not match owning identity contract")
        if (schema_name, schema_version) != (self.schema_name, self.schema_version):
            raise BatchArtifactError("artifact schema does not match owning identity contract")
        identity = _identity_mapping(owning_identity, "owning_identity")
        fields = set(_identity_leaf_paths(identity))
        required = set(self.required_fields)
        allowed = required | set(self.optional_fields)
        if not required <= fields or fields - allowed:
            raise BatchArtifactError(
                "owning_identity does not match its owner-specific closed field schema"
            )
        for identity_path in sorted(fields):
            _project_identity_path(
                identity,
                identity_path,
                "owning identity contract",
                allow_null_ancestors=identity_path in self.optional_fields,
            )
        if not set(self.required_runtime_names) <= set(runtime_versions):
            raise BatchArtifactError("artifact lacks an owning contract required runtime binding")
        return identity

    @property
    def contract_sha256(self) -> str:
        return _digest(
            {
                "contract_id": self.contract_id,
                "artifact_kind": self.artifact_kind,
                "schema_name": self.schema_name,
                "schema_version": self.schema_version,
                "required_fields": list(self.required_fields),
                "optional_fields": list(self.optional_fields),
                "required_runtime_names": list(self.required_runtime_names),
            }
        )


@dataclass(frozen=True, slots=True)
class AnalysisDomainBinding:
    project_entry_id: str
    sample_id: str
    slide_id: str
    analysis_domain_id: str
    analysis_domain_revision: int
    analysis_domain_revision_sha256: str
    domain_set_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_entry_id", _text(self.project_entry_id, "project_entry_id")
        )
        object.__setattr__(self, "sample_id", _safe_id(self.sample_id, "sample_id"))
        object.__setattr__(self, "slide_id", _text(self.slide_id, "slide_id"))
        object.__setattr__(
            self, "analysis_domain_id", _sha256(self.analysis_domain_id, "analysis_domain_id")
        )
        object.__setattr__(
            self,
            "analysis_domain_revision",
            _positive_integer(self.analysis_domain_revision, "analysis_domain_revision"),
        )
        object.__setattr__(
            self,
            "analysis_domain_revision_sha256",
            _sha256(self.analysis_domain_revision_sha256, "analysis_domain_revision_sha256"),
        )
        object.__setattr__(
            self, "domain_set_sha256", _sha256(self.domain_set_sha256, "domain_set_sha256")
        )

    @classmethod
    def from_mapping(cls, value: object) -> "AnalysisDomainBinding":
        fields = frozenset(
            {
                "project_entry_id",
                "sample_id",
                "slide_id",
                "analysis_domain_id",
                "analysis_domain_revision",
                "analysis_domain_revision_sha256",
                "domain_set_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="analysis_domain_binding")
        return cls(**{name: mapping[name] for name in fields})  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision": self.analysis_domain_revision,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "domain_set_sha256": self.domain_set_sha256,
        }


def _domain_sort_key(value: AnalysisDomainBinding) -> tuple[str, str, str, int, str]:
    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.project_entry_id,
    )


@dataclass(frozen=True, slots=True)
class ArtifactScope:
    validation_domain_id: str | None
    sample_id: str | None
    slide_id: str | None
    project_entry_id: str | None
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]

    def __post_init__(self) -> None:
        validation_domain_id = _optional_text(self.validation_domain_id, "validation_domain_id")
        sample_id = _optional_safe_id(self.sample_id, "sample_id")
        slide_id = _optional_text(self.slide_id, "slide_id")
        project_entry_id = _optional_text(self.project_entry_id, "project_entry_id")
        if (sample_id is None) != (slide_id is None):
            raise BatchArtifactError("sample_id and slide_id must both be present or both absent")
        bindings = tuple(self.analysis_domain_bindings)
        if not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchArtifactError(
                "analysis_domain_bindings must contain AnalysisDomainBinding values"
            )
        if tuple(sorted(bindings, key=_domain_sort_key)) != bindings:
            raise BatchArtifactError("analysis_domain_bindings must be in canonical sorted order")
        keys = tuple(_domain_sort_key(item) for item in bindings)
        if len(set(keys)) != len(keys):
            raise BatchArtifactError("analysis_domain_bindings must not contain duplicates")
        for binding in bindings:
            if sample_id is not None and (
                binding.sample_id != sample_id or binding.slide_id != slide_id
            ):
                raise BatchArtifactError(
                    "analysis-domain binding conflicts with sample/slide scope"
                )
            if project_entry_id is not None and binding.project_entry_id != project_entry_id:
                raise BatchArtifactError(
                    "analysis-domain binding conflicts with project-entry scope"
                )
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        object.__setattr__(self, "sample_id", sample_id)
        object.__setattr__(self, "slide_id", slide_id)
        object.__setattr__(self, "project_entry_id", project_entry_id)
        object.__setattr__(self, "analysis_domain_bindings", bindings)

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactScope":
        fields = frozenset(
            {
                "validation_domain_id",
                "sample_id",
                "slide_id",
                "project_entry_id",
                "analysis_domain_bindings",
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="artifact_scope")
        bindings = tuple(
            AnalysisDomainBinding.from_mapping(item)
            for item in _sequence(mapping["analysis_domain_bindings"], "analysis_domain_bindings")
        )
        return cls(
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            sample_id=mapping["sample_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            project_entry_id=mapping["project_entry_id"],  # type: ignore[arg-type]
            analysis_domain_bindings=bindings,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "validation_domain_id": self.validation_domain_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "project_entry_id": self.project_entry_id,
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
        }


@dataclass(frozen=True, slots=True)
class ArtifactConfigurationBinding:
    sample_id: str
    resolved_config_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _safe_id(self.sample_id, "sample_id"))
        object.__setattr__(
            self,
            "resolved_config_sha256",
            _sha256(self.resolved_config_sha256, "resolved_config_sha256"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactConfigurationBinding":
        mapping = _closed_mapping(
            value,
            fields=frozenset({"sample_id", "resolved_config_sha256"}),
            field="configuration_binding",
        )
        return cls(mapping["sample_id"], mapping["resolved_config_sha256"])  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "resolved_config_sha256": self.resolved_config_sha256,
        }


def _validate_configuration_bindings(
    values: Sequence[ArtifactConfigurationBinding], *, sorted_required: bool
) -> tuple[ArtifactConfigurationBinding, ...]:
    bindings = tuple(values)
    if len(bindings) > _MAX_TARGET_BINDINGS:
        raise BatchArtifactError("configuration_bindings exceed the target binding limit")
    if not bindings or not all(isinstance(item, ArtifactConfigurationBinding) for item in bindings):
        raise BatchArtifactError("configuration_bindings must contain at least one typed binding")
    ordered = tuple(sorted(bindings, key=lambda item: item.sample_id))
    if sorted_required and ordered != bindings:
        raise BatchArtifactError("configuration_bindings must be in canonical sorted order")
    if len({item.sample_id for item in bindings}) != len(bindings):
        raise BatchArtifactError("configuration_bindings must not repeat a sample")
    return ordered


def _target_material(
    scope: ArtifactScope,
    bindings: tuple[ArtifactConfigurationBinding, ...],
    manifest_sha256: str,
    participant_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "manifest_sha256": manifest_sha256,
        "participant_ids": list(participant_ids),
        "scope": scope.to_mapping(),
        "configuration_bindings": [item.to_mapping() for item in bindings],
    }


@dataclass(frozen=True, slots=True)
class ArtifactProjectionTarget:
    scope: ArtifactScope
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    target_sha256: str
    manifest_sha256: str
    participant_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ArtifactScope):
            raise BatchArtifactError("scope must be an ArtifactScope")
        if self.scope.validation_domain_id is None:
            raise BatchArtifactError(
                "projection target requires one exact non-null validation domain"
            )
        bindings = _validate_configuration_bindings(
            self.configuration_bindings, sorted_required=True
        )
        manifest_sha256 = _sha256(self.manifest_sha256, "manifest_sha256")
        participant_ids = tuple(
            sorted(_safe_id(item, "participant_ids") for item in self.participant_ids)
        )
        if not participant_ids or len(set(participant_ids)) != len(participant_ids):
            raise BatchArtifactError("participant_ids must be non-empty and unique")
        if tuple(item.sample_id for item in bindings) != participant_ids:
            raise BatchArtifactError("participant_ids must exactly match configuration bindings")
        target_sha256 = _sha256(self.target_sha256, "target_sha256")
        if target_sha256 != _digest(
            _target_material(self.scope, bindings, manifest_sha256, participant_ids)
        ):
            raise BatchArtifactError(
                "target_sha256 does not match exact scope/configuration bindings"
            )
        object.__setattr__(self, "configuration_bindings", bindings)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "participant_ids", participant_ids)
        object.__setattr__(self, "target_sha256", target_sha256)

    @classmethod
    def from_manifest(
        cls,
        manifest: ValidatedBatchManifest,
        participant_ids: tuple[str, ...],
        scope: ArtifactScope,
    ) -> "ArtifactProjectionTarget":
        if not isinstance(manifest, ValidatedBatchManifest):
            raise BatchArtifactError("manifest must be a ValidatedBatchManifest")
        if not isinstance(scope, ArtifactScope):
            raise BatchArtifactError("scope must be an ArtifactScope")
        participants = tuple(_safe_id(item, "participant_ids") for item in participant_ids)
        if not participants or len(set(participants)) != len(participants):
            raise BatchArtifactError("participant_ids must be non-empty and unique")
        samples_by_id = {item.sample_id: item for item in manifest.samples}
        unknown = sorted(set(participants) - set(samples_by_id))
        if unknown:
            raise BatchArtifactError(
                f"participant_ids contain unknown sample(s): {', '.join(unknown)}"
            )
        selected = tuple(samples_by_id[item] for item in participants)
        if any(not item.enabled for item in selected):
            raise BatchArtifactError("disabled samples cannot be projection participants")
        participant_set = set(participants)
        if scope.validation_domain_id is None:
            raise BatchArtifactError(
                "projection targets require one exact non-null validation domain"
            )
        if {item.validation_domain_id for item in selected} != {scope.validation_domain_id}:
            raise BatchArtifactError(
                "projection participants must all belong to the exact target validation domain"
            )
        if scope.sample_id is not None:
            if participant_set != {scope.sample_id}:
                raise BatchArtifactError(
                    "sample-scoped projection requires that exact sole participant"
                )
            sample = samples_by_id[scope.sample_id]
            if sample.slide_id != scope.slide_id:
                raise BatchArtifactError(
                    "projection scope slide_id conflicts with manifest participant"
                )
        for binding in scope.analysis_domain_bindings:
            if binding.sample_id not in participant_set:
                raise BatchArtifactError(
                    "analysis-domain binding sample is not a target participant"
                )
            sample = samples_by_id[binding.sample_id]
            if sample.slide_id != binding.slide_id:
                raise BatchArtifactError(
                    "analysis-domain binding slide conflicts with manifest participant"
                )
        bindings = tuple(
            ArtifactConfigurationBinding(item.sample_id, item.resolved_config_sha256)
            for item in sorted(selected, key=lambda item: item.sample_id)
        )
        participant_ids = tuple(item.sample_id for item in bindings)
        material = _target_material(
            scope, bindings, manifest.normalized_manifest_sha256, participant_ids
        )
        return cls(
            scope=scope,
            configuration_bindings=bindings,
            target_sha256=_digest(material),
            manifest_sha256=manifest.normalized_manifest_sha256,
            participant_ids=participant_ids,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactProjectionTarget":
        mapping = _closed_mapping(
            value,
            fields=frozenset(
                {
                    "scope",
                    "configuration_bindings",
                    "target_sha256",
                    "manifest_sha256",
                    "participant_ids",
                }
            ),
            field="artifact_projection_target",
        )
        return cls(
            scope=ArtifactScope.from_mapping(mapping["scope"]),
            configuration_bindings=tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in _sequence(mapping["configuration_bindings"], "configuration_bindings")
            ),
            target_sha256=mapping["target_sha256"],  # type: ignore[arg-type]
            manifest_sha256=mapping["manifest_sha256"],  # type: ignore[arg-type]
            participant_ids=tuple(
                _text(item, "participant_ids")
                for item in _sequence(mapping["participant_ids"], "participant_ids")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **_target_material(
                self.scope,
                self.configuration_bindings,
                self.manifest_sha256,
                self.participant_ids,
            ),
            "target_sha256": self.target_sha256,
        }


@dataclass(frozen=True, slots=True)
class BatchPreparedDispatchAuthority:
    """Executor-owned identities accepted before an attempt is authorized."""

    compatibility_signature_sha256: str
    current_image_identity_sha256: str | None = None
    script_sha256: str | None = None
    project_identity_sha256: str | None = None
    project_authority_head_sha256: str | None = None
    project_qpproj_snapshot_sha256: str | None = None
    project_entry_projection_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(
                self.compatibility_signature_sha256,
                "compatibility_signature_sha256",
            ),
        )
        for field_name in (
            "current_image_identity_sha256",
            "script_sha256",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_qpproj_snapshot_sha256",
            "project_entry_projection_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_sha256(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class BatchDispatchBinding:
    """Closed self-hashed dispatch authority embedded in every attempt grant."""

    node_id: str
    adapter_spec_sha256: str
    dispatch_mode: DispatchMode
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    projection_target_sha256: str
    compatibility_signature_sha256: str
    current_image_identity_sha256: str | None
    script_sha256: str | None
    project_identity_sha256: str | None
    project_authority_head_sha256: str | None
    project_qpproj_snapshot_sha256: str | None
    project_entry_projection_sha256: str | None
    dispatch_binding_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchDispatchBinding fields when the record is constructed.

        The checks cover ``dispatch_binding_sha256``, ``node_id``, ``adapter_spec_sha256``,
        ``dispatch_mode``, ``participant_ids``, ``project_entry_ids``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(self, "node_id", _safe_id(self.node_id, "node_id"))
        object.__setattr__(
            self,
            "adapter_spec_sha256",
            _sha256(self.adapter_spec_sha256, "adapter_spec_sha256"),
        )
        try:
            mode = DispatchMode(self.dispatch_mode)
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("dispatch_mode is invalid") from exc
        participants = tuple(
            sorted(_safe_id(item, "participant_id") for item in self.participant_ids)
        )
        if not participants or len(set(participants)) != len(participants):
            raise BatchArtifactError("dispatch binding participants must be non-empty and unique")
        entries = tuple(sorted(_text(item, "project_entry_id") for item in self.project_entry_ids))
        if len(set(entries)) != len(entries):
            raise BatchArtifactError("dispatch binding project entries must be unique")
        object.__setattr__(self, "dispatch_mode", mode)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        for field_name in (
            "projection_target_sha256",
            "compatibility_signature_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        for field_name in (
            "current_image_identity_sha256",
            "script_sha256",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_qpproj_snapshot_sha256",
            "project_entry_projection_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_sha256(getattr(self, field_name), field_name),
            )
        if mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE and any(
            getattr(self, field_name) is None
            for field_name in (
                "current_image_identity_sha256",
                "script_sha256",
                "project_identity_sha256",
                "project_authority_head_sha256",
                "project_qpproj_snapshot_sha256",
                "project_entry_projection_sha256",
            )
        ):
            raise BatchArtifactError(
                "QuPath dispatch binding requires current-image, script, and project identities"
            )
        digest = _sha256(self.dispatch_binding_sha256, "dispatch_binding_sha256")
        if digest != _digest(self._unsigned_mapping()):
            raise BatchArtifactError("dispatch binding checksum is not canonical")
        object.__setattr__(self, "dispatch_binding_sha256", digest)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "adapter_spec_sha256": self.adapter_spec_sha256,
            "dispatch_mode": self.dispatch_mode.value,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "projection_target_sha256": self.projection_target_sha256,
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
            "current_image_identity_sha256": self.current_image_identity_sha256,
            "script_sha256": self.script_sha256,
            "project_identity_sha256": self.project_identity_sha256,
            "project_authority_head_sha256": self.project_authority_head_sha256,
            "project_qpproj_snapshot_sha256": self.project_qpproj_snapshot_sha256,
            "project_entry_projection_sha256": self.project_entry_projection_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        node_id: str,
        adapter_spec_sha256: str,
        dispatch_mode: DispatchMode,
        participant_ids: tuple[str, ...],
        project_entry_ids: tuple[str, ...],
        projection_target_sha256: str,
        authority: BatchPreparedDispatchAuthority,
    ) -> "BatchDispatchBinding":
        if not isinstance(authority, BatchPreparedDispatchAuthority):
            raise BatchArtifactError("dispatch authority must be typed")
        unsigned = {
            "node_id": node_id,
            "adapter_spec_sha256": adapter_spec_sha256,
            "dispatch_mode": DispatchMode(dispatch_mode).value,
            "participant_ids": list(sorted(participant_ids)),
            "project_entry_ids": list(sorted(project_entry_ids)),
            "projection_target_sha256": projection_target_sha256,
            "compatibility_signature_sha256": authority.compatibility_signature_sha256,
            "current_image_identity_sha256": authority.current_image_identity_sha256,
            "script_sha256": authority.script_sha256,
            "project_identity_sha256": authority.project_identity_sha256,
            "project_authority_head_sha256": authority.project_authority_head_sha256,
            "project_qpproj_snapshot_sha256": authority.project_qpproj_snapshot_sha256,
            "project_entry_projection_sha256": authority.project_entry_projection_sha256,
        }
        return cls(**unsigned, dispatch_binding_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchDispatchBinding":
        """Decode the serialized fields into a validated BatchDispatchBinding record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = frozenset(
            {
                "node_id",
                "adapter_spec_sha256",
                "dispatch_mode",
                "participant_ids",
                "project_entry_ids",
                "projection_target_sha256",
                "compatibility_signature_sha256",
                "current_image_identity_sha256",
                "script_sha256",
                "project_identity_sha256",
                "project_authority_head_sha256",
                "project_qpproj_snapshot_sha256",
                "project_entry_projection_sha256",
                "dispatch_binding_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="dispatch_binding")
        return cls(
            node_id=mapping["node_id"],  # type: ignore[arg-type]
            adapter_spec_sha256=mapping["adapter_spec_sha256"],  # type: ignore[arg-type]
            dispatch_mode=mapping["dispatch_mode"],  # type: ignore[arg-type]
            participant_ids=tuple(_sequence(mapping["participant_ids"], "participant_ids")),  # type: ignore[arg-type]
            project_entry_ids=tuple(_sequence(mapping["project_entry_ids"], "project_entry_ids")),  # type: ignore[arg-type]
            projection_target_sha256=mapping["projection_target_sha256"],  # type: ignore[arg-type]
            compatibility_signature_sha256=mapping["compatibility_signature_sha256"],  # type: ignore[arg-type]
            current_image_identity_sha256=mapping["current_image_identity_sha256"],  # type: ignore[arg-type]
            script_sha256=mapping["script_sha256"],  # type: ignore[arg-type]
            project_identity_sha256=mapping["project_identity_sha256"],  # type: ignore[arg-type]
            project_authority_head_sha256=mapping["project_authority_head_sha256"],  # type: ignore[arg-type]
            project_qpproj_snapshot_sha256=mapping["project_qpproj_snapshot_sha256"],  # type: ignore[arg-type]
            project_entry_projection_sha256=mapping["project_entry_projection_sha256"],  # type: ignore[arg-type]
            dispatch_binding_sha256=mapping["dispatch_binding_sha256"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "dispatch_binding_sha256": self.dispatch_binding_sha256,
        }


@dataclass(frozen=True, slots=True)
class BatchFailedRetry:
    """Bind an explicit retry to its failed attempt and retained native abort.

    Snapshot retries name their failed terminal. Native fits also retain the
    archived lease reference so the original abort remains directly accessible
    after a fresh project owner acquires control.
    """

    terminal_record_sha256: str
    native_abort_archive_ref: AuthorityMemberRefV2 | None = None

    def __post_init__(self) -> None:
        _sha256(self.terminal_record_sha256, "failed retry terminal")
        ref = self.native_abort_archive_ref
        if ref is not None and (
            type(ref) is not AuthorityMemberRefV2 or ref.family != "project_lease_archive"
        ):
            raise BatchArtifactError("failed retry requires an exact native lease archive")

    def to_mapping(self) -> dict[str, object]:
        return {
            "terminal_record_sha256": self.terminal_record_sha256,
            "native_abort_archive_ref": (
                None if self.native_abort_archive_ref is None
                else self.native_abort_archive_ref.to_mapping()
            ),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchFailedRetry":
        mapping = _closed_mapping(value, fields=frozenset({
            "terminal_record_sha256", "native_abort_archive_ref",
        }), field="failed_retry")
        ref = mapping["native_abort_archive_ref"]
        return cls(mapping["terminal_record_sha256"],
                   None if ref is None else AuthorityMemberRefV2.from_mapping(ref))


@dataclass(frozen=True, slots=True)
class BatchAttemptAuthorization:
    schema_name: str
    schema_version: int
    attempt_id: str
    node_id: str
    node_material_sha256: str
    retry_ordinal: int
    was_invalidated_at_admission: bool
    admission_head_sha256: str
    admission_plan_sha256: str
    input_artifact_set_sha256: str
    interaction_response_sha256: str | None
    interaction_predecessor_terminal_record_sha256: str | None
    dispatch_binding: BatchDispatchBinding
    authorization_sha256: str
    failed_retry: BatchFailedRetry | None = None

    def __post_init__(self) -> None:
        """Bind one attempt to its admitted node, inputs and dispatch description.

        We normalize the record identifiers, require a complete response/predecessor
        pair for an interaction retry, and match the dispatch node. The final checksum
        binds those values to the recorded plan and state head. Incomplete retry
        lineage, a foreign dispatch node or changed signed fields raise BatchArtifactError.
        """

        if self.schema_name != "mif-batch-attempt-authorization" or self.schema_version != 2:
            raise BatchArtifactError("attempt authorization must use the current v2 schema")
        object.__setattr__(self, "attempt_id", _safe_id(self.attempt_id, "attempt_id"))
        object.__setattr__(self, "node_id", _safe_id(self.node_id, "node_id"))
        for field in (
            "node_material_sha256",
            "admission_head_sha256",
            "admission_plan_sha256",
            "input_artifact_set_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        object.__setattr__(
            self, "retry_ordinal", _nonnegative_integer(self.retry_ordinal, "retry_ordinal")
        )
        if type(self.was_invalidated_at_admission) is not bool:
            raise BatchArtifactError("was_invalidated_at_admission must be a boolean")
        response = _optional_sha256(self.interaction_response_sha256, "interaction_response_sha256")
        predecessor = _optional_sha256(
            self.interaction_predecessor_terminal_record_sha256,
            "interaction_predecessor_terminal_record_sha256",
        )
        if (response is None) != (predecessor is None):
            raise BatchArtifactError(
                "interaction retry response and predecessor digests must both be absent or present"
            )
        if self.retry_ordinal == 0 and response is not None:
            raise BatchArtifactError("ordinary attempts cannot carry interaction retry digests")
        object.__setattr__(self, "interaction_response_sha256", response)
        object.__setattr__(self, "interaction_predecessor_terminal_record_sha256", predecessor)
        if not isinstance(self.dispatch_binding, BatchDispatchBinding):
            raise BatchArtifactError("attempt authorization requires typed dispatch binding")
        if self.dispatch_binding.node_id != self.node_id:
            raise BatchArtifactError("dispatch binding names a foreign authorization node")
        if self.failed_retry is not None and (
            type(self.failed_retry) is not BatchFailedRetry
            or self.retry_ordinal == 0 or self.was_invalidated_at_admission
            or response is not None
        ):
            raise BatchArtifactError("explicit failed retry provenance is invalid")
        authorization_sha256 = _sha256(self.authorization_sha256, "authorization_sha256")
        if authorization_sha256 != _digest(self._unsigned_mapping()):
            raise BatchArtifactError("authorization_sha256 does not match authorization material")
        object.__setattr__(self, "authorization_sha256", authorization_sha256)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "node_id": self.node_id,
            "node_material_sha256": self.node_material_sha256,
            "retry_ordinal": self.retry_ordinal,
            "was_invalidated_at_admission": self.was_invalidated_at_admission,
            "admission_head_sha256": self.admission_head_sha256,
            "admission_plan_sha256": self.admission_plan_sha256,
            "input_artifact_set_sha256": self.input_artifact_set_sha256,
            "interaction_response_sha256": self.interaction_response_sha256,
            "interaction_predecessor_terminal_record_sha256": self.interaction_predecessor_terminal_record_sha256,
            "dispatch_binding": self.dispatch_binding.to_mapping(),
            **({"failed_retry": self.failed_retry.to_mapping()}
               if self.failed_retry is not None else {}),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchAttemptAuthorization":
        fields = frozenset(
            {
                "schema_name",
                "schema_version",
                "attempt_id",
                "node_id",
                "node_material_sha256",
                "retry_ordinal",
                "was_invalidated_at_admission",
                "admission_head_sha256",
                "admission_plan_sha256",
                "input_artifact_set_sha256",
                "interaction_response_sha256",
                "interaction_predecessor_terminal_record_sha256",
                "dispatch_binding",
                "authorization_sha256",
            }
        )
        if isinstance(value, Mapping) and "failed_retry" in value:
            fields = fields | {"failed_retry"}
        mapping = _closed_mapping(value, fields=fields, field="batch_attempt_authorization")
        values = {name: mapping[name] for name in fields}
        values["dispatch_binding"] = BatchDispatchBinding.from_mapping(mapping["dispatch_binding"])
        if "failed_retry" in mapping:
            values["failed_retry"] = BatchFailedRetry.from_mapping(mapping["failed_retry"])
        return cls(**values)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "authorization_sha256": self.authorization_sha256}


@dataclass(frozen=True, slots=True)
class BatchArtifactRef:
    artifact_id: str
    artifact_kind: str
    schema_name: str
    schema_version: int
    origin: ArtifactOrigin
    producer_node_id: str | None
    producer_attempt_id: str | None
    producer_attempt_authorization_sha256: str | None
    external_declaration_sha256: str | None
    external_validation_receipt_sha256: str | None
    locator: Path | None
    sha256: str | None
    byte_count: int | None
    scope: ArtifactScope
    owning_identity: Mapping[str, object]
    identity_contract: ArtifactIdentityContract
    owning_identity_contract_sha256: str
    producer_version: str
    runtime_versions: Mapping[str, str]
    compatibility_signature_sha256: str
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]

    def __post_init__(self) -> None:
        """Admit one result reference and freeze its metadata for later consumers.

        We require either an internal producer attempt or an external declaration
        with its validation receipt. A physical result supplies its location, byte
        count and checksum together. The owner contract checks the identity fields
        and required runtimes; configuration bindings must cover the selected scope.
        We copy nested identity and runtime values into immutable containers. Invalid
        origin, file metadata, owner fields or scope raise BatchArtifactError.
        """

        object.__setattr__(self, "artifact_id", _safe_id(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "artifact_kind", _token(self.artifact_kind, "artifact_kind"))
        schema_name, schema_version = _assert_current_schema(
            self.schema_name, self.schema_version, "artifact"
        )
        object.__setattr__(self, "schema_name", schema_name)
        object.__setattr__(self, "schema_version", schema_version)
        try:
            origin = ArtifactOrigin(self.origin)
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("origin must be internal_node or manifest_external") from exc
        producer_node_id = _optional_safe_id(self.producer_node_id, "producer_node_id")
        producer_attempt_id = _optional_safe_id(self.producer_attempt_id, "producer_attempt_id")
        authorization = _optional_sha256(
            self.producer_attempt_authorization_sha256,
            "producer_attempt_authorization_sha256",
        )
        declaration = _optional_sha256(
            self.external_declaration_sha256, "external_declaration_sha256"
        )
        receipt = _optional_sha256(
            self.external_validation_receipt_sha256, "external_validation_receipt_sha256"
        )
        internal_fields = (producer_node_id, producer_attempt_id, authorization)
        external_fields = (declaration, receipt)
        if origin is ArtifactOrigin.INTERNAL_NODE:
            if any(item is None for item in internal_fields) or any(
                item is not None for item in external_fields
            ):
                raise BatchArtifactError(
                    "internal_node origin requires exact producer identity and forbids external receipts"
                )
        elif any(item is not None for item in internal_fields) or any(
            item is None for item in external_fields
        ):
            raise BatchArtifactError(
                "manifest_external origin requires both receipts and forbids producer identity"
            )
        locator = _optional_locator(self.locator, "locator")
        sha256 = _optional_sha256(self.sha256, "sha256")
        byte_count = (
            None if self.byte_count is None else _nonnegative_integer(self.byte_count, "byte_count")
        )
        if len({locator is None, sha256 is None, byte_count is None}) != 1:
            raise BatchArtifactError(
                "locator, sha256, and byte_count must all be present or all absent"
            )
        if not isinstance(self.scope, ArtifactScope):
            raise BatchArtifactError("scope must be an ArtifactScope")
        versions = _runtime_versions(self.runtime_versions, "runtime_versions")
        if not isinstance(self.identity_contract, ArtifactIdentityContract):
            raise BatchArtifactError("identity_contract must be an ArtifactIdentityContract")
        contract_sha256 = _sha256(
            self.owning_identity_contract_sha256,
            "owning_identity_contract_sha256",
        )
        if contract_sha256 != self.identity_contract.contract_sha256:
            raise BatchArtifactError("owning identity contract digest is not authoritative")
        identity = self.identity_contract.validate(
            artifact_kind=self.artifact_kind,
            schema_name=self.schema_name,
            schema_version=self.schema_version,
            owning_identity=self.owning_identity,
            runtime_versions=versions,
        )
        bindings = _validate_configuration_bindings(
            self.configuration_bindings, sorted_required=True
        )
        binding_ids = {item.sample_id for item in bindings}
        if self.scope.sample_id is not None and binding_ids != {self.scope.sample_id}:
            raise BatchArtifactError("configuration bindings conflict with sample scope")
        if any(item.sample_id not in binding_ids for item in self.scope.analysis_domain_bindings):
            raise BatchArtifactError("analysis-domain scope lacks a target configuration binding")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "producer_node_id", producer_node_id)
        object.__setattr__(self, "producer_attempt_id", producer_attempt_id)
        object.__setattr__(self, "producer_attempt_authorization_sha256", authorization)
        object.__setattr__(self, "external_declaration_sha256", declaration)
        object.__setattr__(self, "external_validation_receipt_sha256", receipt)
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "byte_count", byte_count)
        object.__setattr__(self, "owning_identity", identity)
        object.__setattr__(self, "owning_identity_contract_sha256", contract_sha256)
        object.__setattr__(
            self, "producer_version", _text(self.producer_version, "producer_version")
        )
        object.__setattr__(self, "runtime_versions", versions)
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(self.compatibility_signature_sha256, "compatibility_signature_sha256"),
        )
        object.__setattr__(self, "configuration_bindings", bindings)

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        identity_contract: ArtifactIdentityContract,
    ) -> "BatchArtifactRef":
        """Decode the serialized fields into a validated BatchArtifactRef record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = frozenset(
            {
                "artifact_id",
                "artifact_kind",
                "schema_name",
                "schema_version",
                "origin",
                "producer_node_id",
                "producer_attempt_id",
                "producer_attempt_authorization_sha256",
                "external_declaration_sha256",
                "external_validation_receipt_sha256",
                "locator",
                "sha256",
                "byte_count",
                "scope",
                "owning_identity",
                "owning_identity_contract_sha256",
                "producer_version",
                "runtime_versions",
                "compatibility_signature_sha256",
                "configuration_bindings",
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="batch_artifact_ref")
        return cls(
            artifact_id=mapping["artifact_id"],  # type: ignore[arg-type]
            artifact_kind=mapping["artifact_kind"],  # type: ignore[arg-type]
            schema_name=mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            origin=mapping["origin"],  # type: ignore[arg-type]
            producer_node_id=mapping["producer_node_id"],  # type: ignore[arg-type]
            producer_attempt_id=mapping["producer_attempt_id"],  # type: ignore[arg-type]
            producer_attempt_authorization_sha256=mapping["producer_attempt_authorization_sha256"],  # type: ignore[arg-type]
            external_declaration_sha256=mapping["external_declaration_sha256"],  # type: ignore[arg-type]
            external_validation_receipt_sha256=mapping["external_validation_receipt_sha256"],  # type: ignore[arg-type]
            locator=mapping["locator"],  # type: ignore[arg-type]
            sha256=mapping["sha256"],  # type: ignore[arg-type]
            byte_count=mapping["byte_count"],  # type: ignore[arg-type]
            scope=ArtifactScope.from_mapping(mapping["scope"]),
            owning_identity=_mapping(mapping["owning_identity"], "owning_identity"),
            identity_contract=identity_contract,
            owning_identity_contract_sha256=mapping["owning_identity_contract_sha256"],  # type: ignore[arg-type]
            producer_version=mapping["producer_version"],  # type: ignore[arg-type]
            runtime_versions=_mapping(mapping["runtime_versions"], "runtime_versions"),  # type: ignore[arg-type]
            compatibility_signature_sha256=mapping["compatibility_signature_sha256"],  # type: ignore[arg-type]
            configuration_bindings=tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in _sequence(mapping["configuration_bindings"], "configuration_bindings")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "origin": self.origin.value,
            "producer_node_id": self.producer_node_id,
            "producer_attempt_id": self.producer_attempt_id,
            "producer_attempt_authorization_sha256": self.producer_attempt_authorization_sha256,
            "external_declaration_sha256": self.external_declaration_sha256,
            "external_validation_receipt_sha256": self.external_validation_receipt_sha256,
            "locator": None if self.locator is None else str(self.locator),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "scope": self.scope.to_mapping(),
            "owning_identity": _export_json(self.owning_identity),
            "owning_identity_contract_sha256": self.owning_identity_contract_sha256,
            "producer_version": self.producer_version,
            "runtime_versions": dict(self.runtime_versions),
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
            "configuration_bindings": [item.to_mapping() for item in self.configuration_bindings],
        }

    @property
    def artifact_ref_sha256(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True, slots=True, init=False)
class BatchCommittedArtifactAuthority:
    """Issuer-only proof that one artifact belongs to the current state head."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    producer_node_kind: str
    terminal_record_sha256: str
    execution_exchange_sha256: str | None
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("BatchCommittedArtifactAuthority is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("BatchCommittedArtifactAuthority is nonserializable")


def _committed_artifact_authority_material(
    *,
    artifact_ref: BatchArtifactRef,
    state_head_sha256: str,
    active_plan_sha256: str,
    producer_node_kind: str,
    terminal_record_sha256: str,
    execution_exchange_sha256: str | None,
) -> dict[str, object]:
    return {
        "artifact_ref_sha256": artifact_ref.artifact_ref_sha256,
        "state_head_sha256": state_head_sha256,
        "active_plan_sha256": active_plan_sha256,
        "producer_node_kind": producer_node_kind,
        "terminal_record_sha256": terminal_record_sha256,
        "execution_exchange_sha256": execution_exchange_sha256,
    }


def _issue_batch_committed_artifact_authority(
    *,
    artifact_ref: BatchArtifactRef,
    state_head_sha256: str,
    active_plan_sha256: str,
    producer_node_kind: str,
    terminal_record_sha256: str,
    execution_exchange_sha256: str | None,
) -> BatchCommittedArtifactAuthority:
    """Issue the private capability after the state owner completes reconstruction."""

    if not isinstance(artifact_ref, BatchArtifactRef):
        raise BatchArtifactError("committed artifact authority requires a BatchArtifactRef")
    public_values = {
        "artifact_ref": artifact_ref,
        "state_head_sha256": _sha256(state_head_sha256, "state_head_sha256"),
        "active_plan_sha256": _sha256(active_plan_sha256, "active_plan_sha256"),
        "producer_node_kind": _token(producer_node_kind, "producer_node_kind"),
        "terminal_record_sha256": _sha256(terminal_record_sha256, "terminal_record_sha256"),
        "execution_exchange_sha256": _optional_sha256(
            execution_exchange_sha256, "execution_exchange_sha256"
        ),
    }
    values = {
        **public_values,
        "_seal_sha256": _digest(_committed_artifact_authority_material(**public_values)),
        "_issuer": _BATCH_COMMITTED_ARTIFACT_AUTHORITY_ISSUER,
    }
    authority = object.__new__(BatchCommittedArtifactAuthority)
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    return authority


def validate_committed_artifact_authority_set(
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
    *,
    expected_active_plan_sha256: str | None = None,
) -> tuple[BatchCommittedArtifactAuthority, ...]:
    """Authenticate and canonically order one atomic set of committed artifacts.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(authorities) is not tuple:
        raise BatchArtifactError("committed artifact authorities must be an exact tuple")
    expected_plan = _optional_sha256(expected_active_plan_sha256, "expected_active_plan_sha256")
    for authority in authorities:
        if (
            type(authority) is not BatchCommittedArtifactAuthority
            or authority._issuer is not _BATCH_COMMITTED_ARTIFACT_AUTHORITY_ISSUER
        ):
            raise BatchArtifactError("committed artifact authority issuer is invalid")
        validate_artifact_ref(authority.artifact_ref)
        _sha256(authority.state_head_sha256, "state_head_sha256")
        _sha256(authority.active_plan_sha256, "active_plan_sha256")
        _token(authority.producer_node_kind, "producer_node_kind")
        _sha256(authority.terminal_record_sha256, "terminal_record_sha256")
        _optional_sha256(
            authority.execution_exchange_sha256,
            "execution_exchange_sha256",
        )
        expected_seal = _digest(
            _committed_artifact_authority_material(
                artifact_ref=authority.artifact_ref,
                state_head_sha256=authority.state_head_sha256,
                active_plan_sha256=authority.active_plan_sha256,
                producer_node_kind=authority.producer_node_kind,
                terminal_record_sha256=authority.terminal_record_sha256,
                execution_exchange_sha256=authority.execution_exchange_sha256,
            )
        )
        if _sha256(authority._seal_sha256, "committed authority seal") != expected_seal:
            raise BatchArtifactError("committed artifact authority seal is invalid")
    digests = tuple(item.artifact_ref.artifact_ref_sha256 for item in authorities)
    if len(set(digests)) != len(digests):
        raise BatchArtifactError("committed artifact authorities contain duplicate references")
    if authorities:
        state_heads = {item.state_head_sha256 for item in authorities}
        active_plans = {item.active_plan_sha256 for item in authorities}
        if len(state_heads) != 1:
            raise BatchArtifactError("committed artifact authorities use mixed state heads")
        if len(active_plans) != 1:
            raise BatchArtifactError("committed artifact authorities use mixed active plans")
        if expected_plan is not None and active_plans != {expected_plan}:
            raise BatchArtifactError("committed artifact authorities use an unexpected active plan")
    return tuple(sorted(authorities, key=lambda item: item.artifact_ref.artifact_ref_sha256))


@dataclass(frozen=True, slots=True)
class StageArtifactRequirement:
    artifact_kind: str
    schema_name: str
    schema_version: int
    scope: ArtifactScope
    owning_identity: Mapping[str, object]
    identity_contract: ArtifactIdentityContract
    compatibility_signature_sha256: str
    producer_version: str
    runtime_versions: Mapping[str, str]
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    multiplicity: ArtifactMultiplicity
    origin: ArtifactOrigin | None = None
    producer_node_id: str | None = None
    producer_attempt_id: str | None = None
    producer_attempt_authorization_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate the StageArtifactRequirement fields when the record is constructed.

        The checks cover ``schema_name``, ``schema_version``, ``runtime_versions``,
        ``origin``, ``producer_node_id``, ``producer_attempt_id``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(self, "artifact_kind", _token(self.artifact_kind, "artifact_kind"))
        name, version = _assert_current_schema(self.schema_name, self.schema_version, "requirement")
        object.__setattr__(self, "schema_name", name)
        object.__setattr__(self, "schema_version", version)
        if not isinstance(self.scope, ArtifactScope):
            raise BatchArtifactError("requirement scope must be an ArtifactScope")
        if not isinstance(self.identity_contract, ArtifactIdentityContract):
            raise BatchArtifactError("identity_contract must be an ArtifactIdentityContract")
        runtime_versions = _runtime_versions(self.runtime_versions, "runtime_versions")
        identity = self.identity_contract.validate(
            artifact_kind=self.artifact_kind,
            schema_name=self.schema_name,
            schema_version=self.schema_version,
            owning_identity=self.owning_identity,
            runtime_versions=runtime_versions,
        )
        object.__setattr__(self, "owning_identity", identity)
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(self.compatibility_signature_sha256, "compatibility_signature_sha256"),
        )
        object.__setattr__(
            self, "producer_version", _text(self.producer_version, "producer_version")
        )
        object.__setattr__(self, "runtime_versions", runtime_versions)
        object.__setattr__(
            self,
            "configuration_bindings",
            _validate_configuration_bindings(self.configuration_bindings, sorted_required=False),
        )
        try:
            multiplicity = ArtifactMultiplicity(self.multiplicity)
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("requirement multiplicity is invalid") from exc
        object.__setattr__(self, "multiplicity", multiplicity)
        if self.origin is None:
            origin = None
        else:
            try:
                origin = ArtifactOrigin(self.origin)
            except (TypeError, ValueError) as exc:
                raise BatchArtifactError("requirement origin is invalid") from exc
        producer_node_id = _optional_safe_id(self.producer_node_id, "producer_node_id")
        producer_attempt_id = _optional_safe_id(self.producer_attempt_id, "producer_attempt_id")
        producer_authorization = _optional_sha256(
            self.producer_attempt_authorization_sha256,
            "producer_attempt_authorization_sha256",
        )
        producer_fields = (producer_node_id, producer_attempt_id, producer_authorization)
        if origin is ArtifactOrigin.INTERNAL_NODE and any(item is None for item in producer_fields):
            raise BatchArtifactError(
                "internal requirements need exact producer node/attempt/authorization"
            )
        if origin is not ArtifactOrigin.INTERNAL_NODE and any(
            item is not None for item in producer_fields
        ):
            raise BatchArtifactError("producer identity is allowed only for internal requirements")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "producer_node_id", producer_node_id)
        object.__setattr__(self, "producer_attempt_id", producer_attempt_id)
        object.__setattr__(self, "producer_attempt_authorization_sha256", producer_authorization)

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "scope": self.scope.to_mapping(),
            "owning_identity": _export_json(self.owning_identity),
            "owning_identity_contract_sha256": self.identity_contract.contract_sha256,
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
            "producer_version": self.producer_version,
            "runtime_versions": dict(self.runtime_versions),
            "configuration_bindings": [item.to_mapping() for item in self.configuration_bindings],
            "multiplicity": self.multiplicity.value,
            "origin": None if self.origin is None else self.origin.value,
            "producer_node_id": self.producer_node_id,
            "producer_attempt_id": self.producer_attempt_id,
            "producer_attempt_authorization_sha256": self.producer_attempt_authorization_sha256,
        }

    @property
    def requirement_sha256(self) -> str:
        return _digest(self.to_mapping())


def validate_artifact_ref(
    artifact: BatchArtifactRef,
    *,
    authorization: BatchAttemptAuthorization | None = None,
) -> BatchArtifactRef:
    """Bind an admitted immutable reference to its optional dispatch authority.

    Construction or external decoding establishes the reference's closed schema,
    recursively frozen identity and scope. We check the producer relationship here
    without rebuilding those established values.
    """
    if not isinstance(artifact, BatchArtifactRef):
        raise BatchArtifactError("artifact must be a BatchArtifactRef")
    if artifact.origin is ArtifactOrigin.INTERNAL_NODE:
        if authorization is not None:
            if not isinstance(authorization, BatchAttemptAuthorization):
                raise BatchArtifactError("authorization must be a BatchAttemptAuthorization")
            if (
                artifact.producer_node_id != authorization.node_id
                or artifact.producer_attempt_id != authorization.attempt_id
                or artifact.producer_attempt_authorization_sha256
                != authorization.authorization_sha256
            ):
                raise BatchArtifactError(
                    "artifact producer identity does not match attempt authorization"
                )
    elif authorization is not None:
        raise BatchArtifactError(
            "manifest_external artifacts cannot bind a batch attempt authorization"
        )
    return artifact


def validate_artifact_edge(
    artifact: BatchArtifactRef,
    requirement: StageArtifactRequirement,
    *,
    authorization: BatchAttemptAuthorization | None = None,
) -> BatchArtifactRef:
    """Require exact agreement across every typed consumer identity dimension."""
    artifact = validate_artifact_ref(artifact, authorization=authorization)
    if not isinstance(requirement, StageArtifactRequirement):
        raise BatchArtifactError("requirement must be a StageArtifactRequirement")
    comparisons = {
        "artifact_kind": artifact.artifact_kind == requirement.artifact_kind,
        "schema": (artifact.schema_name, artifact.schema_version)
        == (requirement.schema_name, requirement.schema_version),
        "scope": artifact.scope == requirement.scope,
        "owning_identity": _canonical_bytes(_export_json(artifact.owning_identity))
        == _canonical_bytes(_export_json(requirement.owning_identity)),
        "owning_identity_contract": artifact.owning_identity_contract_sha256
        == requirement.identity_contract.contract_sha256,
        "compatibility_signature": artifact.compatibility_signature_sha256
        == requirement.compatibility_signature_sha256,
        "producer_version": artifact.producer_version == requirement.producer_version,
        "runtime_versions": dict(artifact.runtime_versions) == dict(requirement.runtime_versions),
        "configuration_bindings": artifact.configuration_bindings
        == requirement.configuration_bindings,
        "origin": requirement.origin is None or artifact.origin is requirement.origin,
        "producer_node_id": requirement.producer_node_id is None
        or artifact.producer_node_id == requirement.producer_node_id,
        "producer_attempt_id": requirement.producer_attempt_id is None
        or artifact.producer_attempt_id == requirement.producer_attempt_id,
        "producer_attempt_authorization": requirement.producer_attempt_authorization_sha256 is None
        or artifact.producer_attempt_authorization_sha256
        == requirement.producer_attempt_authorization_sha256,
    }
    mismatches = tuple(name for name, matches in comparisons.items() if not matches)
    if mismatches:
        raise BatchArtifactError(
            f"artifact edge does not satisfy exact requirement: {', '.join(mismatches)}"
        )
    return artifact


def _participant_requirement(
    requirement: StageArtifactRequirement,
    binding: ArtifactConfigurationBinding,
) -> StageArtifactRequirement:
    """Project one trusted aggregate requirement onto an exact participant."""
    participant_bindings = tuple(
        item
        for item in requirement.scope.analysis_domain_bindings
        if item.sample_id == binding.sample_id
    )
    if requirement.scope.sample_id is not None:
        if requirement.scope.sample_id != binding.sample_id:
            raise BatchArtifactError("per-participant requirement has foreign sample scope")
        scope = requirement.scope
    elif participant_bindings:
        slide_ids = {item.slide_id for item in participant_bindings}
        entry_ids = {item.project_entry_id for item in participant_bindings}
        if len(slide_ids) != 1 or len(entry_ids) != 1:
            raise BatchArtifactError(
                "per-participant requirement lacks one exact slide/project scope"
            )
        scope = ArtifactScope(
            requirement.scope.validation_domain_id,
            binding.sample_id,
            next(iter(slide_ids)),
            next(iter(entry_ids)),
            participant_bindings,
        )
    else:
        scope = requirement.scope
    return replace(
        requirement,
        scope=scope,
        configuration_bindings=(binding,),
        multiplicity=ArtifactMultiplicity.SINGLE,
    )


def external_artifact_ref(
    validated: ValidatedReusableArtifact,
    target: ArtifactProjectionTarget,
    *,
    manifest: ValidatedBatchManifest,
    identity_contract: ArtifactIdentityContract,
) -> BatchArtifactRef:
    """Project an owning-loader-validated manifest artifact onto one exact target."""
    if not isinstance(validated, ValidatedReusableArtifact):
        raise BatchArtifactError("validated must be a ValidatedReusableArtifact")
    if not isinstance(target, ArtifactProjectionTarget):
        raise BatchArtifactError("target must be an ArtifactProjectionTarget")
    if not isinstance(manifest, ValidatedBatchManifest):
        raise BatchArtifactError("manifest must be a ValidatedBatchManifest")
    try:
        validated.validate_authority_sha256()
        validated.validate_manifest_binding(manifest)
    except BatchManifestError as exc:
        raise BatchArtifactError("external artifact owner authority is invalid") from exc
    if validated.declaration not in manifest.manifest.reusable_artifacts:
        raise BatchArtifactError(
            "external artifact authority is not bound to a manifest declaration"
        )
    if validated.manifest_sha256 != manifest.normalized_manifest_sha256:
        raise BatchArtifactError("external artifact authority is bound to a different manifest")
    expected_target = ArtifactProjectionTarget.from_manifest(
        manifest, target.participant_ids, target.scope
    )
    if expected_target != target:
        raise BatchArtifactError(
            "external artifact target is not the exact manifest-derived projection"
        )
    if target.manifest_sha256 != manifest.normalized_manifest_sha256:
        raise BatchArtifactError("external artifact target binds a different manifest")
    declaration = validated.declaration
    if (
        declaration.validation_domain_id is not None
        and target.scope.validation_domain_id != declaration.validation_domain_id
    ):
        raise BatchArtifactError("external artifact cannot cross its validation domain")
    if validated.owning_identity_contract_sha256 != identity_contract.contract_sha256:
        raise BatchArtifactError(
            "external artifact owner identity contract does not match its receipt"
        )
    try:
        schema_version = int(declaration.schema_version)
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "external artifact schema version must be an integer current version"
        ) from exc
    return BatchArtifactRef(
        artifact_id=declaration.artifact_id,
        artifact_kind=declaration.artifact_kind,
        schema_name=declaration.schema_name,
        schema_version=schema_version,
        origin=ArtifactOrigin.MANIFEST_EXTERNAL,
        producer_node_id=None,
        producer_attempt_id=None,
        producer_attempt_authorization_sha256=None,
        external_declaration_sha256=declaration.declaration_sha256,
        external_validation_receipt_sha256=validated.owning_validation_receipt_sha256,
        locator=declaration.locator,
        sha256=declaration.sha256,
        byte_count=declaration.byte_count,
        scope=target.scope,
        owning_identity=declaration.owning_identity,
        identity_contract=identity_contract,
        owning_identity_contract_sha256=validated.owning_identity_contract_sha256,
        producer_version=validated.producer_version,
        runtime_versions=declaration.runtime_versions,
        compatibility_signature_sha256=declaration.compatibility_signature_sha256,
        configuration_bindings=target.configuration_bindings,
    )


@dataclass(frozen=True, slots=True)
class StageAdapterSpec:
    node_kind: str
    node_version: str
    required_inputs: tuple[StageArtifactRequirement, ...]
    optional_inputs: tuple[StageArtifactRequirement, ...]
    declared_outputs: tuple[str, ...]
    output_identity_contracts: tuple[ArtifactIdentityContract, ...]
    compatibility_signature_id: str
    equivalence_contract: ArtifactEquivalenceContract
    resource_kind: str
    dispatch_mode: DispatchMode

    def __post_init__(self) -> None:
        """Validate the StageAdapterSpec fields when the record is constructed.

        The checks cover ``required_inputs``, ``optional_inputs``,
        ``output_identity_contracts``, ``equivalence_contract``, ``resource_kind``,
        ``node_kind``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        object.__setattr__(self, "node_kind", _token(self.node_kind, "node_kind"))
        object.__setattr__(self, "node_version", _text(self.node_version, "node_version"))
        required = tuple(self.required_inputs)
        optional = tuple(self.optional_inputs)
        if not all(isinstance(item, StageArtifactRequirement) for item in required + optional):
            raise BatchArtifactError("adapter inputs must contain StageArtifactRequirement values")
        required = tuple(sorted(required, key=lambda item: item.requirement_sha256))
        optional = tuple(sorted(optional, key=lambda item: item.requirement_sha256))
        required_kinds = tuple(item.artifact_kind for item in required)
        optional_kinds = tuple(item.artifact_kind for item in optional)
        if len(set(required_kinds)) != len(required_kinds) or len(set(optional_kinds)) != len(
            optional_kinds
        ):
            raise BatchArtifactError("adapter input artifact kinds must be unique")
        if set(required_kinds) & set(optional_kinds):
            raise BatchArtifactError("an artifact kind cannot be both required and optional")
        outputs = tuple(_token(item, "declared_outputs") for item in self.declared_outputs)
        if not outputs or len(set(outputs)) != len(outputs):
            raise BatchArtifactError("declared_outputs must be non-empty and unique")
        output_contracts = tuple(self.output_identity_contracts)
        if not all(isinstance(item, ArtifactIdentityContract) for item in output_contracts):
            raise BatchArtifactError("output_identity_contracts must be typed owner contracts")
        output_contract_kinds = tuple(item.artifact_kind for item in output_contracts)
        if len(set(output_contract_kinds)) != len(output_contract_kinds) or set(
            output_contract_kinds
        ) != set(outputs):
            raise BatchArtifactError(
                "every declared output needs exactly one owner identity contract"
            )
        object.__setattr__(self, "required_inputs", required)
        object.__setattr__(self, "optional_inputs", optional)
        object.__setattr__(self, "declared_outputs", tuple(sorted(outputs)))
        object.__setattr__(
            self,
            "output_identity_contracts",
            tuple(sorted(output_contracts, key=lambda item: item.artifact_kind)),
        )
        object.__setattr__(
            self,
            "compatibility_signature_id",
            _text(self.compatibility_signature_id, "compatibility_signature_id"),
        )
        if not isinstance(self.equivalence_contract, ArtifactEquivalenceContract):
            raise BatchArtifactError("adapter spec requires an exact owner equivalence contract")
        output_contracts_by_kind = {
            item.artifact_kind: item for item in self.output_identity_contracts
        }
        for field, paths in self.equivalence_contract.evidence_path_groups:
            for artifact_kind, identity_path in paths:
                identity_contract = output_contracts_by_kind.get(artifact_kind)
                if (
                    identity_contract is None
                    or identity_path not in identity_contract.required_fields
                ):
                    raise BatchArtifactError(
                        f"{field} must name a required leaf in a declared output identity contract"
                    )
        resource_kind = _token(self.resource_kind, "resource_kind")
        if resource_kind not in _RESOURCE_KINDS:
            raise BatchArtifactError("resource_kind is not a closed batch resource class")
        object.__setattr__(self, "resource_kind", resource_kind)
        try:
            dispatch_mode = DispatchMode(self.dispatch_mode)
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError(
                "dispatch_mode must be single or qupath_project_current_image"
            ) from exc
        object.__setattr__(self, "dispatch_mode", dispatch_mode)

    @property
    def spec_sha256(self) -> str:
        return _digest(
            {
                "node_kind": self.node_kind,
                "node_version": self.node_version,
                "required_inputs": [item.to_mapping() for item in self.required_inputs],
                "optional_inputs": [item.to_mapping() for item in self.optional_inputs],
                "declared_outputs": list(self.declared_outputs),
                "output_identity_contract_sha256s": [
                    item.contract_sha256 for item in self.output_identity_contracts
                ],
                "compatibility_signature_id": self.compatibility_signature_id,
                "equivalence_contract_sha256": self.equivalence_contract.contract_sha256,
                "resource_kind": self.resource_kind,
                "dispatch_mode": self.dispatch_mode.value,
            }
        )

    @property
    def equivalence_contract_id(self) -> str:
        return self.equivalence_contract.contract_id


@dataclass(frozen=True, slots=True)
class StageAdapterRegistry:
    specs: tuple[StageAdapterSpec, ...]

    def __post_init__(self) -> None:
        specs = tuple(self.specs)
        if not all(isinstance(item, StageAdapterSpec) for item in specs):
            raise BatchArtifactError("stage registry must contain StageAdapterSpec values")
        keys = tuple((item.node_kind, item.node_version) for item in specs)
        if len(set(keys)) != len(keys):
            raise BatchArtifactError("stage registry has duplicate node-kind/version registrations")
        equivalence_digests: dict[str, str] = {}
        for spec in specs:
            contract = spec.equivalence_contract
            previous = equivalence_digests.setdefault(
                contract.contract_id, contract.contract_sha256
            )
            if previous != contract.contract_sha256:
                raise BatchArtifactError(
                    "one equivalence contract ID cannot name different modes or evidence paths"
                )
        object.__setattr__(
            self,
            "specs",
            tuple(sorted(specs, key=lambda item: (item.node_kind, item.node_version))),
        )

    def resolve(self, node_kind: str, node_version: str) -> StageAdapterSpec:
        key = (_token(node_kind, "node_kind"), _text(node_version, "node_version"))
        for spec in self.specs:
            if (spec.node_kind, spec.node_version) == key:
                return spec
        raise BatchArtifactError(f"no stage adapter specification for {key[0]}/{key[1]}")

    def validate_inputs(
        self, spec: StageAdapterSpec, artifacts: tuple[BatchArtifactRef, ...]
    ) -> tuple[BatchArtifactRef, ...]:
        if spec not in self.specs:
            raise BatchArtifactError("stage adapter specification is not registered")
        values = tuple(artifacts)
        if not all(isinstance(item, BatchArtifactRef) for item in values):
            raise BatchArtifactError("stage inputs must contain BatchArtifactRef values")
        by_kind: dict[str, list[BatchArtifactRef]] = {}
        for artifact in values:
            by_kind.setdefault(artifact.artifact_kind, []).append(artifact)
        allowed = {item.artifact_kind: item for item in spec.required_inputs + spec.optional_inputs}
        missing = sorted({item.artifact_kind for item in spec.required_inputs} - set(by_kind))
        undeclared = sorted(set(by_kind) - set(allowed))
        if missing or undeclared:
            raise BatchArtifactError(
                f"stage inputs have missing {missing!r} or undeclared {undeclared!r} kinds"
            )
        for kind, artifacts in by_kind.items():
            requirement = allowed[kind]
            if requirement.multiplicity is ArtifactMultiplicity.SINGLE:
                if len(artifacts) != 1:
                    raise BatchArtifactError(f"single input {kind} requires exactly one artifact")
                validate_artifact_edge(artifacts[0], requirement)
                continue
            if requirement.multiplicity is ArtifactMultiplicity.EXACT_SET:
                ref_sha256s = tuple(artifact.artifact_ref_sha256 for artifact in artifacts)
                if len(set(ref_sha256s)) != len(ref_sha256s):
                    raise BatchArtifactError(
                        f"exact-set input {kind} contains a duplicate artifact"
                    )
                for artifact in artifacts:
                    validate_artifact_edge(artifact, requirement)
                continue
            expected = {item.sample_id: item for item in requirement.configuration_bindings}
            actual: dict[str, BatchArtifactRef] = {}
            for artifact in artifacts:
                if len(artifact.configuration_bindings) != 1:
                    raise BatchArtifactError(
                        f"per-participant input {kind} must bind one participant"
                    )
                participant = artifact.configuration_bindings[0].sample_id
                if participant in actual:
                    raise BatchArtifactError(
                        f"per-participant input {kind} duplicates {participant}"
                    )
                actual[participant] = artifact
            if set(actual) != set(expected):
                raise BatchArtifactError(
                    f"per-participant input {kind} has missing or foreign participants"
                )
            for participant, artifact in actual.items():
                validate_artifact_edge(
                    artifact,
                    _participant_requirement(requirement, expected[participant]),
                )
        return tuple(
            sorted(
                values,
                key=lambda item: (item.artifact_kind, item.artifact_ref_sha256),
            )
        )

    def validate_declared_inputs(
        self, spec: StageAdapterSpec, artifacts: tuple[BatchArtifactRef, ...]
    ) -> tuple[BatchArtifactRef, ...]:
        """Validate a bound input set against a sample-neutral stage schema.

        Exact scope, configuration, compatibility, runtime, owner values, and
        producer authority belong to the immutable plan edge.  This method is
        used only after that edge validation, so a universal production spec
        need not guess sample identities discovered by an earlier node.
        """

        if spec not in self.specs:
            raise BatchArtifactError("stage adapter specification is not registered")
        values = tuple(artifacts)
        if not all(isinstance(item, BatchArtifactRef) for item in values):
            raise BatchArtifactError("stage inputs must contain BatchArtifactRef values")
        by_kind: dict[str, list[BatchArtifactRef]] = {}
        for artifact in values:
            by_kind.setdefault(artifact.artifact_kind, []).append(artifact)
        requirements = {
            item.artifact_kind: item for item in spec.required_inputs + spec.optional_inputs
        }
        missing = sorted({item.artifact_kind for item in spec.required_inputs} - set(by_kind))
        undeclared = sorted(set(by_kind) - set(requirements))
        if missing or undeclared:
            raise BatchArtifactError(
                f"stage inputs have missing {missing!r} or undeclared {undeclared!r} kinds"
            )
        for kind, artifacts_for_kind in by_kind.items():
            requirement = requirements[kind]
            if (
                requirement.multiplicity is ArtifactMultiplicity.SINGLE
                and len(artifacts_for_kind) != 1
            ):
                raise BatchArtifactError(f"single input {kind} requires exactly one artifact")
            if requirement.multiplicity is ArtifactMultiplicity.EXACT_SET:
                ref_sha256s = tuple(artifact.artifact_ref_sha256 for artifact in artifacts_for_kind)
                if len(set(ref_sha256s)) != len(ref_sha256s):
                    raise BatchArtifactError(
                        f"exact-set input {kind} contains a duplicate artifact"
                    )
            if requirement.multiplicity is ArtifactMultiplicity.PER_PARTICIPANT:
                participants = [
                    artifact.configuration_bindings[0].sample_id
                    for artifact in artifacts_for_kind
                    if len(artifact.configuration_bindings) == 1
                ]
                if len(participants) != len(artifacts_for_kind) or len(set(participants)) != len(
                    participants
                ):
                    raise BatchArtifactError(f"per-participant input {kind} is ambiguous")
            for artifact in artifacts_for_kind:
                validate_artifact_ref(artifact)
                if (artifact.schema_name, artifact.schema_version) != (
                    requirement.schema_name,
                    requirement.schema_version,
                ) or artifact.owning_identity_contract_sha256 != requirement.identity_contract.contract_sha256:
                    raise BatchArtifactError(f"input {kind} violates its declared owner schema")
        return tuple(
            sorted(
                values,
                key=lambda item: (item.artifact_kind, item.artifact_ref_sha256),
            )
        )

    def validate_outputs(
        self,
        spec: StageAdapterSpec,
        artifacts: tuple[BatchArtifactRef, ...],
        *,
        authorization: BatchAttemptAuthorization | None = None,
    ) -> tuple[BatchArtifactRef, ...]:
        if spec not in self.specs:
            raise BatchArtifactError("stage adapter specification is not registered")
        if not isinstance(authorization, BatchAttemptAuthorization):
            raise BatchArtifactError("stage output validation requires exact attempt authorization")
        values = tuple(artifacts)
        if not all(isinstance(item, BatchArtifactRef) for item in values):
            raise BatchArtifactError("stage outputs must contain BatchArtifactRef values")
        kinds = tuple(item.artifact_kind for item in values)
        if len(set(kinds)) != len(kinds) or set(kinds) != set(spec.declared_outputs):
            raise BatchArtifactError("stage outputs must contain every declared kind exactly once")
        for artifact in values:
            if artifact.origin is not ArtifactOrigin.INTERNAL_NODE:
                raise BatchArtifactError("stage outputs must be internal_node artifacts")
            identity_contracts = {
                item.artifact_kind: item for item in spec.output_identity_contracts
            }
            if (
                artifact.owning_identity_contract_sha256
                != identity_contracts[artifact.artifact_kind].contract_sha256
            ):
                raise BatchArtifactError("stage output uses the wrong owner identity contract")
            validate_artifact_ref(artifact, authorization=authorization)
        return tuple(sorted(values, key=lambda item: item.artifact_kind))


def artifact_ref_set_sha256(artifacts: tuple[BatchArtifactRef, ...]) -> str:
    """Hash the exact canonical input-ref set independent of caller ordering."""
    values = tuple(artifacts)
    if not all(isinstance(item, BatchArtifactRef) for item in values):
        raise BatchArtifactError("artifact set must contain BatchArtifactRef values")
    digests = tuple(sorted(item.artifact_ref_sha256 for item in values))
    if len(set(digests)) != len(digests):
        raise BatchArtifactError("artifact set must not contain duplicate references")
    return _digest({"artifact_ref_sha256s": list(digests)})


def batch_node_material_sha256(
    *,
    spec: StageAdapterSpec,
    node_id: str,
    projection_target_sha256: str,
    current_image_identity_sha256: str | None,
    script_sha256: str | None,
    project_identity_sha256: str | None,
    project_authority_head_sha256: str | None,
    project_qpproj_snapshot_sha256: str | None,
    project_entry_projection_sha256: str | None,
    compatibility_signature_sha256: str,
) -> str:
    """Derive dispatch material from the exact closed stage and target identity."""
    if not isinstance(spec, StageAdapterSpec):
        raise BatchArtifactError("spec must be a StageAdapterSpec")
    material = {
        "node_id": _safe_id(node_id, "node_id"),
        "stage_spec_sha256": spec.spec_sha256,
        "projection_target_sha256": _sha256(projection_target_sha256, "projection_target_sha256"),
        "current_image_identity_sha256": _optional_sha256(
            current_image_identity_sha256, "current_image_identity_sha256"
        ),
        "script_sha256": _optional_sha256(script_sha256, "script_sha256"),
        "project_identity_sha256": _optional_sha256(
            project_identity_sha256, "project_identity_sha256"
        ),
        "project_authority_head_sha256": _optional_sha256(
            project_authority_head_sha256, "project_authority_head_sha256"
        ),
        "project_qpproj_snapshot_sha256": _optional_sha256(
            project_qpproj_snapshot_sha256, "project_qpproj_snapshot_sha256"
        ),
        "project_entry_projection_sha256": _optional_sha256(
            project_entry_projection_sha256, "project_entry_projection_sha256"
        ),
        "compatibility_signature_sha256": _sha256(
            compatibility_signature_sha256, "compatibility_signature_sha256"
        ),
    }
    return _digest(material)


@dataclass(frozen=True, slots=True)
class ArtifactEquivalenceReport:
    equivalence_contract_id: str
    byte_deterministic: bool
    exact_bytes_equal: bool | None
    canonical_identities_equal: bool | None
    stable_inventories_equal: bool | None
    scientific_values_equal: bool | None
    qc_evidence_equal: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "equivalence_contract_id",
            _text(self.equivalence_contract_id, "equivalence_contract_id"),
        )
        if type(self.byte_deterministic) is not bool:
            raise BatchArtifactError("byte_deterministic must be boolean")
        values = (
            self.exact_bytes_equal,
            self.canonical_identities_equal,
            self.stable_inventories_equal,
            self.scientific_values_equal,
            self.qc_evidence_equal,
        )
        if any(item is not None and type(item) is not bool for item in values):
            raise BatchArtifactError("equivalence comparison fields must be boolean or null")
        scientific = values[1:]
        if self.byte_deterministic:
            if self.exact_bytes_equal is None or any(item is not None for item in scientific):
                raise BatchArtifactError(
                    "byte-deterministic equivalence requires only exact byte comparison"
                )
        elif self.exact_bytes_equal is not None or any(item is None for item in scientific):
            raise BatchArtifactError(
                "non-byte-deterministic equivalence requires identities, inventories, values, and QC"
            )

    @property
    def equivalent(self) -> bool:
        if self.byte_deterministic:
            return self.exact_bytes_equal is True
        return all(
            item is True
            for item in (
                self.canonical_identities_equal,
                self.stable_inventories_equal,
                self.scientific_values_equal,
                self.qc_evidence_equal,
            )
        )


@dataclass(frozen=True, slots=True)
class ArtifactEquivalenceContract:
    """Digest-bound owner evidence paths for registry-computed equivalence."""

    contract_id: str
    byte_deterministic: bool
    canonical_identity_paths: tuple[tuple[str, str], ...]
    stable_inventory_paths: tuple[tuple[str, str], ...]
    scientific_value_paths: tuple[tuple[str, str], ...]
    qc_evidence_paths: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        """Validate the ArtifactEquivalenceContract fields when the record is constructed.

        The checks cover ``byte_deterministic``, ``contract_id``,
        ``canonical_identity_paths``, ``stable_inventory_paths``,
        ``scientific_value_paths``, ``qc_evidence_paths``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        object.__setattr__(self, "contract_id", _text(self.contract_id, "equivalence contract_id"))
        if type(self.byte_deterministic) is not bool:
            raise BatchArtifactError("equivalence byte_deterministic must be boolean")
        groups = (
            (
                "canonical_identity_paths",
                _equivalence_paths(self.canonical_identity_paths, "canonical_identity_paths"),
            ),
            (
                "stable_inventory_paths",
                _equivalence_paths(self.stable_inventory_paths, "stable_inventory_paths"),
            ),
            (
                "scientific_value_paths",
                _equivalence_paths(self.scientific_value_paths, "scientific_value_paths"),
            ),
            (
                "qc_evidence_paths",
                _equivalence_paths(self.qc_evidence_paths, "qc_evidence_paths"),
            ),
        )
        if self.byte_deterministic and any(paths for _, paths in groups):
            raise BatchArtifactError(
                "byte-deterministic equivalence cannot declare non-byte evidence paths"
            )
        if not self.byte_deterministic and any(not paths for _, paths in groups):
            raise BatchArtifactError(
                "non-byte equivalence requires every exact owner evidence path group"
            )
        for field, paths in groups:
            object.__setattr__(self, field, paths)

    @property
    def evidence_path_groups(
        self,
    ) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
        return (
            ("canonical_identity_paths", self.canonical_identity_paths),
            ("stable_inventory_paths", self.stable_inventory_paths),
            ("scientific_value_paths", self.scientific_value_paths),
            ("qc_evidence_paths", self.qc_evidence_paths),
        )

    @property
    def contract_sha256(self) -> str:
        return _digest(
            {
                "contract_id": self.contract_id,
                "byte_deterministic": self.byte_deterministic,
                "canonical_identity_paths": [list(item) for item in self.canonical_identity_paths],
                "stable_inventory_paths": [list(item) for item in self.stable_inventory_paths],
                "scientific_value_paths": [list(item) for item in self.scientific_value_paths],
                "qc_evidence_paths": [list(item) for item in self.qc_evidence_paths],
            }
        )


def _equivalence_projection(
    outputs: tuple[BatchArtifactRef, ...],
    paths: tuple[tuple[str, str], ...],
) -> bytes:
    by_kind = {item.artifact_kind: item for item in outputs}
    material: list[dict[str, object]] = []
    for artifact_kind, identity_path in paths:
        artifact = by_kind[artifact_kind]
        value = _project_identity_path(
            artifact.owning_identity,
            identity_path,
            "equivalence evidence",
        )
        material.append(
            {
                "artifact_kind": artifact_kind,
                "identity_path": identity_path,
                "value": _export_json(value),
            }
        )
    return _canonical_bytes(material)


@dataclass(frozen=True, slots=True)
class BatchNodeExecutionContext:
    authorization: BatchAttemptAuthorization
    node_id: str
    adapter_spec: StageAdapterSpec
    inputs: tuple[BatchArtifactRef, ...]
    projection_target_sha256: str
    current_image_identity_sha256: str | None
    script_sha256: str | None
    project_identity_sha256: str | None
    project_authority_head_sha256: str | None
    project_qpproj_snapshot_sha256: str | None
    project_entry_projection_sha256: str | None
    compatibility_signature_sha256: str

    def __post_init__(self) -> None:
        """Bind the selected adapter and immutable inputs to their admitted attempt.

        We check each declared input edge and its aggregate identity, then retain
        the image, script and project identities used for dispatch. A foreign node,
        changed input set or incompatible current-image selection raises
        BatchArtifactError before execution begins. Later live-state admission
        remains the state owner's responsibility.
        """

        if not isinstance(self.authorization, BatchAttemptAuthorization):
            raise BatchArtifactError(
                "execution context requires validated BatchAttemptAuthorization"
            )
        if not isinstance(self.adapter_spec, StageAdapterSpec):
            raise BatchArtifactError("execution context requires a StageAdapterSpec")
        node_id = _safe_id(self.node_id, "node_id")
        if self.authorization.node_id != node_id:
            raise BatchArtifactError("execution context node_id does not match authorization")
        inputs = StageAdapterRegistry((self.adapter_spec,)).validate_declared_inputs(
            self.adapter_spec, tuple(self.inputs)
        )
        if artifact_ref_set_sha256(inputs) != self.authorization.input_artifact_set_sha256:
            raise BatchArtifactError("execution inputs do not match authorized input artifact set")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(
            self,
            "projection_target_sha256",
            _sha256(self.projection_target_sha256, "projection_target_sha256"),
        )
        object.__setattr__(
            self,
            "current_image_identity_sha256",
            _optional_sha256(self.current_image_identity_sha256, "current_image_identity_sha256"),
        )
        object.__setattr__(
            self, "script_sha256", _optional_sha256(self.script_sha256, "script_sha256")
        )
        for field_name in (
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_qpproj_snapshot_sha256",
            "project_entry_projection_sha256",
        ):
            object.__setattr__(
                self, field_name, _optional_sha256(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(self.compatibility_signature_sha256, "compatibility_signature_sha256"),
        )
        binding = self.authorization.dispatch_binding
        if (
            binding.node_id != node_id
            or binding.adapter_spec_sha256 != self.adapter_spec.spec_sha256
            or binding.dispatch_mode is not self.adapter_spec.dispatch_mode
            or binding.projection_target_sha256 != self.projection_target_sha256
            or binding.current_image_identity_sha256 != self.current_image_identity_sha256
            or binding.script_sha256 != self.script_sha256
            or binding.project_identity_sha256 != self.project_identity_sha256
            or binding.project_authority_head_sha256 != self.project_authority_head_sha256
            or binding.project_qpproj_snapshot_sha256 != self.project_qpproj_snapshot_sha256
            or binding.project_entry_projection_sha256 != self.project_entry_projection_sha256
            or binding.compatibility_signature_sha256 != self.compatibility_signature_sha256
        ):
            raise BatchArtifactError("execution context does not match authorized dispatch binding")
        if self.adapter_spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE:
            if (
                self.script_sha256 is None
                or self.project_identity_sha256 is None
                or self.project_authority_head_sha256 is None
                or self.project_qpproj_snapshot_sha256 is None
                or self.project_entry_projection_sha256 is None
                or self.current_image_identity_sha256 is None
            ):
                raise BatchArtifactError(
                    "QuPath project dispatch requires exact script, project, and current-image identities"
                )
        # The state store owns the authorization's node-material digest because it
        # binds durable plan, input, source/config/project/domain/checkpoint, and
        # response authority that this runtime-only value object cannot reopen.

    @property
    def coalescing_key(self) -> tuple[str, str, str, str, str]:
        if self.adapter_spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE:
            raise BatchArtifactError("single dispatch contexts cannot be coalesced")
        assert self.script_sha256 is not None and self.project_identity_sha256 is not None
        return (
            self.adapter_spec.node_kind,
            self.adapter_spec.node_version,
            self.script_sha256,
            self.project_identity_sha256,
            self.compatibility_signature_sha256,
        )


def _execution_context_authority_material(
    context: BatchNodeExecutionContext,
) -> dict[str, object]:
    """Project the immutable identities established by context construction.

    We retain these identities for live dispatch binding without reconstructing
    the context, its authorization and every admitted input reference.
    """
    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("current execution authority requires exact context")
    material = {
        "authorization_sha256": context.authorization.authorization_sha256,
        "node_id": context.node_id,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "input_artifact_ref_sha256s": sorted(item.artifact_ref_sha256 for item in context.inputs),
        "projection_target_sha256": context.projection_target_sha256,
        "current_image_identity_sha256": context.current_image_identity_sha256,
        "script_sha256": context.script_sha256,
        "project_identity_sha256": context.project_identity_sha256,
        "project_authority_head_sha256": context.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
    }
    return material


@dataclass(frozen=True, slots=True, init=False)
class BatchCurrentExecutionAuthority:
    """Issuer-only proof of one exact context at the current pending head."""

    execution_context_sha256: str
    state_head_sha256: str
    active_plan_sha256: str
    authorization_sha256: str
    node_id: str
    node_kind: str
    node_scope: str
    validation_domain_id: str | None
    clustering_scope: Mapping[str, object] | None
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    input_artifact_ref_sha256s: tuple[str, ...]
    adapter_spec_sha256: str
    dispatch_binding_sha256: str
    node_material_sha256: str
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("BatchCurrentExecutionAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("BatchCurrentExecutionAuthority cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("BatchCurrentExecutionAuthority is nonserializable")


def _current_execution_authority_material(
    values: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: (
            [item.to_mapping() for item in value]
            if key == "analysis_domain_bindings"
            else (
                [item.to_mapping() for item in value]
                if key == "configuration_bindings"
                else (
                    {
                        "mode": value["mode"],
                        "phenotyping_group_id": value["phenotyping_group_id"],
                        "validation_domain_ids": list(value["validation_domain_ids"]),
                    }
                    if key == "clustering_scope" and value is not None
                    else (
                        list(value)
                        if key
                        in {
                            "participant_ids",
                            "project_entry_ids",
                            "input_artifact_ref_sha256s",
                        }
                        else value
                    )
                )
            )
        )
        for key, value in values.items()
        if not key.startswith("_")
    }


def _issue_batch_current_execution_authority(
    *,
    context: BatchNodeExecutionContext,
    state_head_sha256: str,
    active_plan_sha256: str,
    node_kind: str,
    node_scope: str,
    validation_domain_id: str | None,
    clustering_scope: Mapping[str, object] | None,
    participant_ids: tuple[str, ...],
    project_entry_ids: tuple[str, ...],
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...],
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...],
    input_artifact_ref_sha256s: tuple[str, ...],
) -> BatchCurrentExecutionAuthority:
    context_material = _execution_context_authority_material(context)
    bindings = tuple(analysis_domain_bindings)
    if any(type(item) is not AnalysisDomainBinding for item in bindings):
        raise BatchArtifactError("current execution requires typed analysis-domain bindings")
    if tuple(sorted(bindings, key=_domain_sort_key)) != bindings:
        raise BatchArtifactError("current execution domain bindings are not canonical")
    configurations = _validate_configuration_bindings(configuration_bindings, sorted_required=True)
    inputs = tuple(
        sorted(
            _sha256(item, "current execution input artifact reference")
            for item in input_artifact_ref_sha256s
        )
    )
    if len(set(inputs)) != len(inputs):
        raise BatchArtifactError("current execution input references are duplicated")
    clustering_mode_by_kind = {
        "clustering_per_slide": "per_slide",
        "clustering_pooled": "pooled",
    }
    graph_scoped_kinds = {
        *clustering_mode_by_kind,
        "cluster_review_evidence",
        "cluster_label_review",
    }
    if (node_kind in graph_scoped_kinds) != (clustering_scope is not None):
        raise BatchArtifactError("current execution clustering scope does not match node kind")
    canonical_clustering_scope: Mapping[str, object] | None = None
    if clustering_scope is not None:
        raw_scope = _mapping(clustering_scope, "current execution clustering scope")
        expected_fields = {
            "mode",
            "phenotyping_group_id",
            "validation_domain_ids",
        }
        if set(raw_scope) != expected_fields:
            raise BatchArtifactError("current execution clustering scope is not closed")
        mode = _token(raw_scope["mode"], "current execution clustering mode")
        if mode not in set(clustering_mode_by_kind.values()):
            raise BatchArtifactError("current execution clustering mode is unsupported")
        if node_kind in clustering_mode_by_kind and mode != clustering_mode_by_kind[node_kind]:
            raise BatchArtifactError("current execution clustering mode contradicts node kind")
        group_id = _optional_text(
            raw_scope["phenotyping_group_id"],
            "current execution phenotyping_group_id",
        )
        if group_id is not None:
            raise BatchArtifactError("current execution clustering group is not canonical")
        validation_domains = tuple(
            _text(item, "current execution clustering validation_domain_id")
            for item in _sequence(
                raw_scope["validation_domain_ids"],
                "current execution clustering validation_domain_ids",
            )
        )
        if (
            not validation_domains
            or len(set(validation_domains)) != len(validation_domains)
            or tuple(sorted(validation_domains)) != validation_domains
        ):
            raise BatchArtifactError(
                "current execution clustering validation domains are not canonical"
            )
        canonical_validation_domain = _optional_text(validation_domain_id, "validation_domain_id")
        if mode == "per_slide":
            if (
                node_scope != "sample"
                or len(participant_ids) != 1
                or len(validation_domains) != 1
                or canonical_validation_domain != validation_domains[0]
            ):
                raise BatchArtifactError(
                    "current execution per_slide clustering scope is inconsistent"
                )
        elif (node_scope != "validation_domain"
              or validation_domains != (canonical_validation_domain,)):
            raise BatchArtifactError("pooled clustering requires one Processing group")
        canonical_clustering_scope = MappingProxyType(
            {
                "mode": mode,
                "phenotyping_group_id": group_id,
                "validation_domain_ids": validation_domains,
            }
        )
    public_values: dict[str, object] = {
        "execution_context_sha256": _digest(context_material),
        "state_head_sha256": _sha256(state_head_sha256, "state_head_sha256"),
        "active_plan_sha256": _sha256(active_plan_sha256, "active_plan_sha256"),
        "authorization_sha256": context.authorization.authorization_sha256,
        "node_id": _safe_id(context.node_id, "node_id"),
        "node_kind": _token(node_kind, "node_kind"),
        "node_scope": _token(node_scope, "node_scope"),
        "validation_domain_id": _optional_text(validation_domain_id, "validation_domain_id"),
        "clustering_scope": canonical_clustering_scope,
        "participant_ids": tuple(
            sorted(_safe_id(item, "participant_id") for item in participant_ids)
        ),
        "project_entry_ids": tuple(
            sorted(_text(item, "project_entry_id") for item in project_entry_ids)
        ),
        "analysis_domain_bindings": bindings,
        "configuration_bindings": configurations,
        "input_artifact_ref_sha256s": inputs,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "dispatch_binding_sha256": (context.authorization.dispatch_binding.dispatch_binding_sha256),
        "node_material_sha256": context.authorization.node_material_sha256,
    }
    values = {
        **public_values,
        "_seal_sha256": _digest(_current_execution_authority_material(public_values)),
        "_issuer": _BATCH_CURRENT_EXECUTION_AUTHORITY_ISSUER,
    }
    authority = object.__new__(BatchCurrentExecutionAuthority)
    for name in BatchCurrentExecutionAuthority.__dataclass_fields__:
        object.__setattr__(authority, name, values[name])
    return validate_current_execution_authority(authority, expected_context=context)


def validate_current_execution_authority(
    authority: BatchCurrentExecutionAuthority,
    *,
    expected_context: BatchNodeExecutionContext,
) -> BatchCurrentExecutionAuthority:
    """Validate one state-issued current execution capability and its context.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(authority) is not BatchCurrentExecutionAuthority
        or authority._issuer is not _BATCH_CURRENT_EXECUTION_AUTHORITY_ISSUER
    ):
        raise BatchArtifactError("current execution authority issuer is invalid")
    context_material = _execution_context_authority_material(expected_context)
    values = {
        name: getattr(authority, name)
        for name in BatchCurrentExecutionAuthority.__dataclass_fields__
    }
    expected_seal = _digest(_current_execution_authority_material(values))
    if _sha256(authority._seal_sha256, "current execution authority seal") != expected_seal:
        raise BatchArtifactError("current execution authority seal is invalid")
    if (
        _sha256(authority.execution_context_sha256, "execution_context_sha256")
        != _digest(context_material)
        or authority.authorization_sha256 != expected_context.authorization.authorization_sha256
        or authority.node_id != expected_context.node_id
        or authority.adapter_spec_sha256 != expected_context.adapter_spec.spec_sha256
        or authority.dispatch_binding_sha256
        != expected_context.authorization.dispatch_binding.dispatch_binding_sha256
        or authority.node_material_sha256 != expected_context.authorization.node_material_sha256
        or authority.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in expected_context.inputs))
        or authority.participant_ids
        != expected_context.authorization.dispatch_binding.participant_ids
        or authority.project_entry_ids
        != expected_context.authorization.dispatch_binding.project_entry_ids
    ):
        raise BatchArtifactError("current execution authority context differs")
    return authority


@dataclass(frozen=True, slots=True)
class BatchNodeExecutionResult:
    authorization_sha256: str
    outputs: tuple[BatchArtifactRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorization_sha256",
            _sha256(self.authorization_sha256, "authorization_sha256"),
        )
        outputs = tuple(self.outputs)
        if not all(isinstance(item, BatchArtifactRef) for item in outputs):
            raise BatchArtifactError("execution outputs must contain BatchArtifactRef values")
        object.__setattr__(self, "outputs", outputs)


@runtime_checkable
class RuntimeStageAdapter(Protocol):
    @property
    def spec(self) -> StageAdapterSpec:
        """Return the exact closed stage specification owned by this adapter."""

    def execute(self, context: BatchNodeExecutionContext) -> BatchNodeExecutionResult:
        """Execute one scheduler-authorized context without committing batch state."""


@dataclass(frozen=True, slots=True)
class RuntimeStageAdapterRegistry:
    stage_registry: StageAdapterRegistry
    adapters: tuple[RuntimeStageAdapter, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_registry, StageAdapterRegistry):
            raise BatchArtifactError("stage_registry must be a StageAdapterRegistry")
        adapters = tuple(self.adapters)
        if not all(isinstance(item, RuntimeStageAdapter) for item in adapters):
            raise BatchArtifactError("runtime registry requires real RuntimeStageAdapter values")
        keys = tuple((item.spec.node_kind, item.spec.node_version) for item in adapters)
        if len(set(keys)) != len(keys):
            raise BatchArtifactError(
                "runtime registry has duplicate node-kind/version registrations"
            )
        expected = {(item.node_kind, item.node_version): item for item in self.stage_registry.specs}
        if set(keys) != set(expected):
            raise BatchArtifactError(
                "runtime registry must cover every and only closed stage specification"
            )
        for adapter in adapters:
            if adapter.spec != expected[(adapter.spec.node_kind, adapter.spec.node_version)]:
                raise BatchArtifactError(
                    "runtime adapter specification differs from closed stage registry"
                )
        object.__setattr__(
            self,
            "adapters",
            tuple(sorted(adapters, key=lambda item: (item.spec.node_kind, item.spec.node_version))),
        )

    def resolve(self, node_kind: str, node_version: str) -> RuntimeStageAdapter:
        spec = self.stage_registry.resolve(node_kind, node_version)
        for adapter in self.adapters:
            if adapter.spec == spec:
                return adapter
        raise BatchArtifactError(f"no runtime adapter for {spec.node_kind}/{spec.node_version}")

    def execute(self, context: BatchNodeExecutionContext) -> BatchNodeExecutionResult:
        adapter = self.resolve(context.adapter_spec.node_kind, context.adapter_spec.node_version)
        if adapter.spec != context.adapter_spec:
            raise BatchArtifactError(
                "execution context does not match registered adapter specification"
            )
        result = adapter.execute(context)
        if not isinstance(result, BatchNodeExecutionResult):
            raise BatchArtifactError("runtime adapter returned an invalid execution result")
        if result.authorization_sha256 != context.authorization.authorization_sha256:
            raise BatchArtifactError("execution result does not bind the dispatched authorization")
        canonical_outputs = self.stage_registry.validate_outputs(
            context.adapter_spec, result.outputs, authorization=context.authorization
        )
        return replace(result, outputs=canonical_outputs)

    def compare_one_sample_outputs(
        self,
        node_kind: str,
        node_version: str,
        batch_outputs: tuple[BatchArtifactRef, ...],
        single_sample_outputs: tuple[BatchArtifactRef, ...],
    ) -> ArtifactEquivalenceReport:
        spec = self.stage_registry.resolve(node_kind, node_version)
        contracts = {item.artifact_kind: item for item in spec.output_identity_contracts}
        validated_groups: list[tuple[BatchArtifactRef, ...]] = []
        for label, outputs in (
            ("batch_outputs", batch_outputs),
            ("single_sample_outputs", single_sample_outputs),
        ):
            values = tuple(outputs)
            if (
                not all(isinstance(item, BatchArtifactRef) for item in values)
                or {item.artifact_kind for item in values} != set(spec.declared_outputs)
                or len(values) != len(spec.declared_outputs)
            ):
                raise BatchArtifactError(f"{label} does not match declared stage outputs")
            values = tuple(validate_artifact_ref(item) for item in values)
            if any(
                item.owning_identity_contract_sha256
                != contracts[item.artifact_kind].contract_sha256
                for item in values
            ):
                raise BatchArtifactError(f"{label} uses a wrong owner identity contract")
            validated_groups.append(values)
        equivalence = spec.equivalence_contract
        ordered_batch = tuple(sorted(validated_groups[0], key=lambda item: item.artifact_kind))
        ordered_single = tuple(sorted(validated_groups[1], key=lambda item: item.artifact_kind))
        if equivalence.byte_deterministic:
            batch_bytes = tuple(
                (item.artifact_kind, item.sha256, item.byte_count) for item in ordered_batch
            )
            single_bytes = tuple(
                (item.artifact_kind, item.sha256, item.byte_count) for item in ordered_single
            )
            if any(sha256 is None for _, sha256, _ in batch_bytes + single_bytes):
                raise BatchArtifactError(
                    "byte-deterministic equivalence requires complete-file byte identities"
                )
            return ArtifactEquivalenceReport(
                equivalence_contract_id=equivalence.contract_id,
                byte_deterministic=True,
                exact_bytes_equal=batch_bytes == single_bytes,
                canonical_identities_equal=None,
                stable_inventories_equal=None,
                scientific_values_equal=None,
                qc_evidence_equal=None,
            )
        comparisons = tuple(
            _equivalence_projection(ordered_batch, paths)
            == _equivalence_projection(ordered_single, paths)
            for _, paths in equivalence.evidence_path_groups
        )
        return ArtifactEquivalenceReport(
            equivalence_contract_id=equivalence.contract_id,
            byte_deterministic=False,
            exact_bytes_equal=None,
            canonical_identities_equal=comparisons[0],
            stable_inventories_equal=comparisons[1],
            scientific_values_equal=comparisons[2],
            qc_evidence_equal=comparisons[3],
        )

    @staticmethod
    def validate_coalesced_contexts(
        contexts: tuple[BatchNodeExecutionContext, ...],
    ) -> tuple[BatchNodeExecutionContext, ...]:
        values = tuple(contexts)
        if len(values) < 2 or not all(
            isinstance(item, BatchNodeExecutionContext) for item in values
        ):
            raise BatchArtifactError("coalescing requires at least two typed execution contexts")
        if any(
            item.adapter_spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
            for item in values
        ):
            raise BatchArtifactError("only qupath_project_current_image contexts may coalesce")
        keys = {item.coalescing_key for item in values}
        if len(keys) != 1 or any(
            item.adapter_spec != values[0].adapter_spec for item in values[1:]
        ):
            raise BatchArtifactError(
                "coalesced contexts must share exact stage/script/project/compatibility identity"
            )
        attempts = tuple(item.authorization.attempt_id for item in values)
        nodes = tuple(item.authorization.node_id for item in values)
        authorizations = tuple(item.authorization.authorization_sha256 for item in values)
        current_images = tuple(item.current_image_identity_sha256 for item in values)
        targets = tuple(item.projection_target_sha256 for item in values)
        if (
            len(set(attempts)) != len(values)
            or len(set(nodes)) != len(values)
            or len(set(authorizations)) != len(values)
            or len(set(current_images)) != len(values)
            or len(set(targets)) != len(values)
        ):
            raise BatchArtifactError("coalesced contexts require independent exact authorizations")
        return tuple(sorted(values, key=lambda item: item.authorization.attempt_id))


__all__ = [
    "AnalysisDomainBinding",
    "ArtifactConfigurationBinding",
    "ArtifactEquivalenceContract",
    "ArtifactEquivalenceReport",
    "ArtifactIdentityContract",
    "ArtifactMultiplicity",
    "ArtifactOrigin",
    "ArtifactProjectionTarget",
    "ArtifactScope",
    "BatchArtifactError",
    "BatchArtifactRef",
    "BatchCommittedArtifactAuthority",
    "BatchCurrentExecutionAuthority",
    "BatchAttemptAuthorization",
    "BatchDispatchBinding",
    "BatchPreparedDispatchAuthority",
    "BatchNodeExecutionContext",
    "BatchNodeExecutionResult",
    "DispatchMode",
    "RuntimeStageAdapter",
    "RuntimeStageAdapterRegistry",
    "StageAdapterRegistry",
    "StageAdapterSpec",
    "StageArtifactRequirement",
    "artifact_ref_set_sha256",
    "batch_node_material_sha256",
    "external_artifact_ref",
    "validate_artifact_edge",
    "validate_artifact_ref",
    "validate_committed_artifact_authority_set",
    "validate_current_execution_authority",
]
