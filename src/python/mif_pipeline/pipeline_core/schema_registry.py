"""

Lists the current file and message formats accepted by this prototype.

Before reading saved analysis data or a processing result, we compare its format
identifier with the version listed for that file type. The corresponding parser
then checks its fields and values. The list also records the supported QuPath,
Warpy and Bio-Formats versions used for the application setup checks.

"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import json
from types import MappingProxyType

_MANIFEST_RESOURCE = "data/record-formats.json"
_RECORD_KINDS = frozenset({"settings", "review_record", "analysis_progress", "process_record", "result", "process_message"})
_SCHEMA_SNAPSHOT = ContextVar("current_record_formats", default=None)


class SchemaCompatibilityError(ValueError):
    """The selected file or message does not use its currently supported format."""


def _mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    """

    Checks the complete field list of a format declaration.

    Args:
        value: Decoded JSON object to inspect.
        fields: Required field names for this declaration.
        label: Description included in a missing or unexpected-field error.

    Returns:
        Mapping containing the required string-keyed fields.
    """

    if not isinstance(value, Mapping) or any(type(key) is not str for key in value) or set(value) != fields:
        raise SchemaCompatibilityError(f"Check the required fields in {label}.")
    return value


def _string(value: object, label: str) -> str:
    """

    Checks a format name or version before it is used in a lookup.

    Args:
        value: Required nonempty string without surrounding whitespace.
        label: Field description used when its text is invalid.

    Returns:
        Validated format name or version string.
    """

    if type(value) is not str or not value or value != value.strip():
        raise SchemaCompatibilityError(f"Provide a nonempty {label} without surrounding spaces.")
    return value


@dataclass(frozen=True, slots=True)
class SchemaFamily:
    """

    Describes the current version of one analysis-file or process-message type.

    Attributes:
        schema_family: Name shared by the writer and corresponding parser.
        record_kind: Settings, review, progress, process, result or message category.
        current_writer: Version identifier currently written and read for this type.
    """

    schema_family: str
    record_kind: str
    current_writer: str

    @classmethod
    def from_mapping(cls, value: object, *, index: int) -> "SchemaFamily":
        """

        Reads one format declaration and checks its category, name and current version.

        Args:
            value: JSON object containing the three declaration fields.
            index: Position used to identify an invalid entry in the format list.

        Returns:
            SchemaFamily describing the current file/message type.
        """

        row = _mapping(value, {"schema_family", "record_kind", "current_writer"}, f"format entry {index}")
        kind = _string(row["record_kind"], "record category")
        if kind not in _RECORD_KINDS:
            raise SchemaCompatibilityError(f"Unknown record category in format entry {index}.")
        return cls(
            _string(row["schema_family"], "format name"), kind,
            _string(row["current_writer"], "format version"),
        )

    def to_mapping(self) -> dict[str, object]:
        """

        Collects the format name, category and supported version for serialization.

        Returns:
            Three-field dictionary used by the current format-list writer.
        """

        return {"schema_family": self.schema_family, "record_kind": self.record_kind, "current_writer": self.current_writer}


@dataclass(frozen=True, slots=True)
class ReleaseCompatibilityManifest:
    """

    Holds the current format list and supported external-software versions.

    Attributes:
        schema_version: Version of this format-list file.
        families: Unique file/message declarations in their recorded order.
        runtime_constraints: Software names and versions used by setup checks.
    """

    schema_version: str
    families: tuple[SchemaFamily, ...]
    runtime_constraints: Mapping[str, str]

    @classmethod
    def load(cls) -> "ReleaseCompatibilityManifest":
        """

        Reads the installed format list, reusing parsed values for identical file contents.

        Returns:
            Current format declarations and software requirements.
        """

        return _manifest_from_material(_validated_manifest_material(_read_manifest_payload()))

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleaseCompatibilityManifest":
        """

        Decodes a UTF-8 format-list file and checks its reproducible JSON representation.

        We reject duplicate object keys before constructing the declarations.
        The bytes are compared with the sorted-key writer output so the same
        declarations have one representation in saved processing records.

        Args:
            payload: Complete encoded format-list file.

        Returns:
            Checked current-format manifest.

        Raises:
            SchemaCompatibilityError: JSON is invalid, duplicates a field or
                declaration, or differs from the format-list writer output.
        """

        if type(payload) is not bytes:
            raise SchemaCompatibilityError("The format list must be supplied as encoded bytes.")

        def unique(items):
            """Builds one JSON object while rejecting a repeated field name."""
            result = {}
            for key, value in items:
                if key in result:
                    raise SchemaCompatibilityError(f"The format list repeats {key!r}.")
                result[key] = value
            return result

        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SchemaCompatibilityError("The format list is not valid UTF-8 JSON.") from error
        result = cls.from_mapping(decoded)
        if result.canonical_bytes() != payload:
            raise SchemaCompatibilityError("Save the format list using its sorted-key JSON writer.")
        return result

    @classmethod
    def from_mapping(cls, value: object) -> "ReleaseCompatibilityManifest":
        """

        Checks all current format declarations and supported software versions.

        Args:
            value: Decoded object containing schema_version, families and
                runtime_constraints.

        Returns:
            Format manifest with unique type names and checked software version text.
        """

        row = _mapping(value, {"schema_version", "families", "runtime_constraints"}, "the format list")
        if row["schema_version"] != "record-formats/v1":
            raise SchemaCompatibilityError("The installed format-list version is unsupported.")
        raw = row["families"]
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not raw:
            raise SchemaCompatibilityError("The format list must contain its file/message declarations.")
        families = tuple(SchemaFamily.from_mapping(item, index=i) for i, item in enumerate(raw))
        if len({item.schema_family for item in families}) != len(families):
            raise SchemaCompatibilityError("The format list contains duplicate type names.")
        constraints = row["runtime_constraints"]
        if not isinstance(constraints, Mapping):
            raise SchemaCompatibilityError("Software versions must be recorded as a name-to-version mapping.")
        software = {_string(key, "software name"): _string(version, "software version") for key, version in constraints.items()}
        return cls("record-formats/v1", families, MappingProxyType(software))

    def family(self, schema_family: str) -> SchemaFamily:
        """

        Finds the current declaration for a requested file or message type.

        Args:
            schema_family: Format name used by its parser or writer.

        Returns:
            Matching SchemaFamily.

        Raises:
            SchemaCompatibilityError: The application has no declaration for that name.
        """

        name = _string(schema_family, "format name")
        for family in self.families:
            if family.schema_family == name:
                return family
        raise SchemaCompatibilityError(f"No current file format is registered as {name!r}.")

    def assert_current_writer(self, schema_family: str, schema_version: str) -> None:
        """

        Requires the version written by the current application for the requested format.

        Args:
            schema_family: Name of the file/message type.
            schema_version: Version declared by the saved data or current writer.

        Raises:
            SchemaCompatibilityError: The type is unknown or its version differs.
        """

        family = self.family(schema_family)
        if schema_version != family.current_writer:
            raise SchemaCompatibilityError(f"{schema_family} requires format {family.current_writer!r}; received {schema_version!r}.")

    def to_mapping(self) -> dict[str, object]:
        """

        Collects the current declarations and software versions for JSON writing.

        Returns:
            Complete current-format-list dictionary.
        """

        return {"schema_version": self.schema_version, "families": [item.to_mapping() for item in self.families], "runtime_constraints": dict(self.runtime_constraints)}

    def canonical_bytes(self) -> bytes:
        """

        Encodes the current format list with sorted keys and a trailing newline.

        Returns:
            UTF-8 bytes used for file comparisons and repeatable record checksums.
        """

        return (json.dumps(self.to_mapping(), allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@lru_cache(maxsize=4)
def _validated_manifest_material(payload: bytes) -> tuple:
    """

    Parses identical format-list bytes once and caches their immutable scalar values.

    Args:
        payload: Encoded format-list file.

    Returns:
        Schema, declaration tuples and software-version tuples used to construct
        independent public manifests.
    """

    result = ReleaseCompatibilityManifest.from_bytes(payload)
    return result.schema_version, tuple((f.schema_family, f.record_kind, f.current_writer) for f in result.families), tuple(result.runtime_constraints.items())


def _manifest_from_material(material: tuple) -> ReleaseCompatibilityManifest:
    """Constructs a manifest from the cached format declarations and software versions."""

    schema, families, software = material
    return ReleaseCompatibilityManifest(schema, tuple(SchemaFamily(*fields) for fields in families), MappingProxyType(dict(software)))


@contextmanager
def schema_validation_snapshot():
    """

    Uses one installed format-list version throughout a group of related file checks.

    Nested checks share the already read bytes. Before leaving the outer group,
    we compare the installed file again so a package update cannot combine two
    format definitions during the same validation operation.
    """

    if _SCHEMA_SNAPSHOT.get() is not None:
        yield
        return
    payload = _read_manifest_payload_fresh()
    _validated_manifest_material(payload)
    token = _SCHEMA_SNAPSHOT.set(payload)
    try:
        yield
        if _read_manifest_payload_fresh() != payload:
            raise SchemaCompatibilityError("The installed file formats changed during validation. Repeat the current operation.")
    finally:
        _SCHEMA_SNAPSHOT.reset(token)


def _read_manifest_payload() -> bytes:
    """Returns the format-list bytes selected for this validation group or reads the installed file."""

    snapshot = _SCHEMA_SNAPSHOT.get()
    return snapshot if snapshot is not None else _read_manifest_payload_fresh()


def _read_manifest_payload_fresh() -> bytes:
    """Reads the packaged current-format list and reports an incomplete installation."""

    try:
        return resources.files("mif_pipeline.pipeline_core").joinpath(_MANIFEST_RESOURCE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise SchemaCompatibilityError("The installed application is missing its current file-format definitions.") from error


def assert_current_writer(schema_family: str, schema_version: str) -> None:
    """

    Checks a parser or writer version against the current declaration for its file type.

    Args:
        schema_family: Registered file/message name.
        schema_version: Declared version to compare with the installed definition.

    Raises:
        SchemaCompatibilityError: The type or version is unsupported.
    """

    schema, families, software = _validated_manifest_material(_read_manifest_payload())
    for fields in families:
        if fields[0] == schema_family:
            _manifest_from_material((schema, (fields,), software)).assert_current_writer(schema_family, schema_version)
            return
    raise SchemaCompatibilityError(f"No current file format is registered as {schema_family!r}.")


__all__ = ["ReleaseCompatibilityManifest", "SchemaCompatibilityError", "SchemaFamily", "assert_current_writer", "schema_validation_snapshot"]
