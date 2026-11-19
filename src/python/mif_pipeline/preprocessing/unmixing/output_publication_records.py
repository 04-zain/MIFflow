"""Immutable records and strict canonical wire format for output publication.

The `PublishedUnmixingOutput` record carries `pointer`, `manifest`, `pointer_path`,
`generation_path`, `image_path` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Mapping

MANIFEST_SCHEMA = "mif_pipeline.unmixing_output_generation"
POINTER_SCHEMA = "mif_pipeline.unmixing_output_authority_pointer"
SCHEMA_VERSION = 1
ARTIFACT_NAMES = {
    "image": "image.ome.zarr",
    "provenance": "provenance.json",
    "clipping_evidence": "clipping-evidence.json",
}


# ==============================================================================
#                           Output publication records
# ==============================================================================


def validate_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be an exact lowercase SHA-256")
    return value


def _safe_basename(value: object, label: str) -> str:
    if type(value) is not str or not value or Path(value).name != value:
        raise ValueError(f"{label} must be a safe basename")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must not contain a path separator")
    return value


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _content_hash(value: Mapping[str, object], hash_field: str) -> str:
    content = {key: item for key, item in value.items() if key != hash_field}
    return hashlib.sha256(canonical_json(content)).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate output publication field {key!r}")
        value[key] = item
    return value


def _parse_canonical_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    if canonical_json(value) != raw:
        raise ValueError(f"{label} JSON is not canonical")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")


@dataclass(frozen=True, slots=True)
class OutputPublicationArtifact:
    role: str
    relative_name: str
    alias_name: str
    content_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if type(self.role) is not str:
            raise TypeError("artifact role must be an exact string")
        if self.role not in ARTIFACT_NAMES:
            raise ValueError("unsupported output publication artifact role")
        if self.relative_name != ARTIFACT_NAMES[self.role]:
            raise ValueError("artifact relative name differs from its fixed role")
        _safe_basename(self.alias_name, "artifact alias name")
        validate_sha256(self.content_sha256, "artifact content_sha256")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise ValueError("artifact byte_length must be a nonnegative integer")

    def to_mapping(self) -> dict[str, object]:
        return {
            "role": self.role,
            "relative_name": self.relative_name,
            "alias_name": self.alias_name,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class OutputPublicationManifest:
    schema_name: str
    schema_version: int
    request_sha256: str
    generation_id: str
    predecessor_generation_id: str | None
    artifacts: tuple[OutputPublicationArtifact, ...]
    manifest_content_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_name) is not str
            or type(self.schema_version) is not int
            or (self.schema_name, self.schema_version) != (MANIFEST_SCHEMA, SCHEMA_VERSION)
        ):
            raise ValueError("unsupported output publication manifest schema")
        validate_sha256(self.request_sha256, "manifest request_sha256")
        validate_sha256(self.generation_id, "manifest generation_id")
        if self.generation_id != self.request_sha256:
            raise ValueError("generation identity must equal request identity")
        if self.predecessor_generation_id is not None:
            validate_sha256(
                self.predecessor_generation_id,
                "manifest predecessor_generation_id",
            )
            if self.predecessor_generation_id == self.generation_id:
                raise ValueError("a generation cannot be its own predecessor")
        if type(self.artifacts) is not tuple or any(
            type(item) is not OutputPublicationArtifact for item in self.artifacts
        ):
            raise TypeError("manifest artifacts must be an exact tuple")
        expected_roles = ("image", "provenance")
        roles = tuple(item.role for item in self.artifacts)
        if roles not in {expected_roles, expected_roles + ("clipping_evidence",)}:
            raise ValueError("manifest artifact roles are incomplete or out of order")
        if len({item.alias_name for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("manifest artifact aliases must be unique")
        validate_sha256(self.manifest_content_sha256, "manifest content SHA-256")
        if self.manifest_content_sha256 != _content_hash(
            self.to_mapping(), "manifest_content_sha256"
        ):
            raise ValueError("manifest content checksum mismatch")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "request_sha256": self.request_sha256,
            "generation_id": self.generation_id,
            "predecessor_generation_id": self.predecessor_generation_id,
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "manifest_content_sha256": self.manifest_content_sha256,
        }


@dataclass(frozen=True, slots=True)
class OutputPublicationPointer:
    schema_name: str
    schema_version: int
    request_sha256: str
    generation_id: str
    manifest_relative_path: str
    manifest_sha256: str
    pointer_content_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_name) is not str
            or type(self.schema_version) is not int
            or (self.schema_name, self.schema_version) != (POINTER_SCHEMA, SCHEMA_VERSION)
        ):
            raise ValueError("unsupported output publication pointer schema")
        validate_sha256(self.request_sha256, "pointer request_sha256")
        validate_sha256(self.generation_id, "pointer generation_id")
        validate_sha256(self.manifest_sha256, "pointer manifest_sha256")
        validate_sha256(self.pointer_content_sha256, "pointer content SHA-256")
        if self.request_sha256 != self.generation_id:
            raise ValueError("pointer generation identity differs from request")
        if type(self.manifest_relative_path) is not str:
            raise TypeError("pointer manifest_relative_path must be an exact string")
        if "\\" in self.manifest_relative_path:
            raise ValueError("pointer manifest path cannot contain a backslash")
        relative = PurePosixPath(self.manifest_relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.name != "manifest.json"
            or len(relative.parts) != 3
            or relative.parts[1] != self.generation_id
            or relative.as_posix() != self.manifest_relative_path
        ):
            raise ValueError("pointer manifest path is unsafe")
        if self.pointer_content_sha256 != _content_hash(
            self.to_mapping(), "pointer_content_sha256"
        ):
            raise ValueError("pointer content checksum mismatch")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "request_sha256": self.request_sha256,
            "generation_id": self.generation_id,
            "manifest_relative_path": self.manifest_relative_path,
            "manifest_sha256": self.manifest_sha256,
            "pointer_content_sha256": self.pointer_content_sha256,
        }


@dataclass(frozen=True, slots=True)
class PublishedUnmixingOutput:
    pointer: OutputPublicationPointer
    manifest: OutputPublicationManifest
    pointer_path: Path
    generation_path: Path
    image_path: Path
    provenance_path: Path
    clipping_evidence_path: Path | None
    reused: bool = field(default=False, compare=False)
    warnings: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if type(self.pointer) is not OutputPublicationPointer:
            raise TypeError("pointer must be exact OutputPublicationPointer")
        if type(self.manifest) is not OutputPublicationManifest:
            raise TypeError("manifest must be exact OutputPublicationManifest")
        for value in (
            self.pointer_path,
            self.generation_path,
            self.image_path,
            self.provenance_path,
        ):
            if not isinstance(value, Path):
                raise TypeError("publication paths must be Path values")
        if self.clipping_evidence_path is not None and not isinstance(
            self.clipping_evidence_path, Path
        ):
            raise TypeError("clipping evidence path must be a Path or None")
        if type(self.reused) is not bool:
            raise TypeError("reused must be an exact bool")
        if type(self.warnings) is not tuple or any(
            type(item) is not str or not item for item in self.warnings
        ):
            raise TypeError("warnings must be an exact tuple of nonempty strings")


def build_manifest(
    request_sha256: str,
    artifacts: tuple[OutputPublicationArtifact, ...],
    predecessor_generation_id: str | None,
) -> OutputPublicationManifest:
    content: dict[str, object] = {
        "schema_name": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "request_sha256": request_sha256,
        "generation_id": request_sha256,
        "predecessor_generation_id": predecessor_generation_id,
        "artifacts": [item.to_mapping() for item in artifacts],
    }
    return OutputPublicationManifest(
        schema_name=MANIFEST_SCHEMA,
        schema_version=SCHEMA_VERSION,
        request_sha256=request_sha256,
        generation_id=request_sha256,
        predecessor_generation_id=predecessor_generation_id,
        artifacts=artifacts,
        manifest_content_sha256=hashlib.sha256(canonical_json(content)).hexdigest(),
    )


def build_pointer(
    generation_root_name: str,
    manifest: OutputPublicationManifest,
    manifest_raw: bytes,
) -> OutputPublicationPointer:
    relative = (
        PurePosixPath(generation_root_name) / manifest.generation_id / "manifest.json"
    ).as_posix()
    content: dict[str, object] = {
        "schema_name": POINTER_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "request_sha256": manifest.request_sha256,
        "generation_id": manifest.generation_id,
        "manifest_relative_path": relative,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    }
    return OutputPublicationPointer(
        schema_name=POINTER_SCHEMA,
        schema_version=SCHEMA_VERSION,
        request_sha256=manifest.request_sha256,
        generation_id=manifest.generation_id,
        manifest_relative_path=relative,
        manifest_sha256=str(content["manifest_sha256"]),
        pointer_content_sha256=hashlib.sha256(canonical_json(content)).hexdigest(),
    )


def _artifact_from_mapping(value: object) -> OutputPublicationArtifact:
    if type(value) is not dict:
        raise ValueError("manifest artifact must be an object")
    _exact_fields(
        value,
        {"role", "relative_name", "alias_name", "content_sha256", "byte_length"},
        "manifest artifact",
    )
    return OutputPublicationArtifact(**value)  # type: ignore[arg-type]


def manifest_from_bytes(raw: bytes) -> OutputPublicationManifest:
    value = _parse_canonical_object(raw, "output publication manifest")
    _exact_fields(
        value,
        {
            "schema_name",
            "schema_version",
            "request_sha256",
            "generation_id",
            "predecessor_generation_id",
            "artifacts",
            "manifest_content_sha256",
        },
        "output publication manifest",
    )
    artifacts_value = value["artifacts"]
    if type(artifacts_value) is not list:
        raise ValueError("manifest artifacts must be an array")
    return OutputPublicationManifest(
        schema_name=value["schema_name"],  # type: ignore[arg-type]
        schema_version=value["schema_version"],  # type: ignore[arg-type]
        request_sha256=value["request_sha256"],  # type: ignore[arg-type]
        generation_id=value["generation_id"],  # type: ignore[arg-type]
        predecessor_generation_id=value["predecessor_generation_id"],  # type: ignore[arg-type]
        artifacts=tuple(_artifact_from_mapping(item) for item in artifacts_value),
        manifest_content_sha256=value["manifest_content_sha256"],  # type: ignore[arg-type]
    )


def pointer_from_bytes(raw: bytes) -> OutputPublicationPointer:
    value = _parse_canonical_object(raw, "output publication pointer")
    _exact_fields(
        value,
        {
            "schema_name",
            "schema_version",
            "request_sha256",
            "generation_id",
            "manifest_relative_path",
            "manifest_sha256",
            "pointer_content_sha256",
        },
        "output publication pointer",
    )
    return OutputPublicationPointer(**value)  # type: ignore[arg-type]


__all__ = (
    "OutputPublicationArtifact",
    "OutputPublicationManifest",
    "OutputPublicationPointer",
    "PublishedUnmixingOutput",
)
