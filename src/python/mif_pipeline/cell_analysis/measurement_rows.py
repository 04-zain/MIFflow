"""Read selected native measurement components in stable scientific cell-ID order.

Small authenticated references select immutable spatial base data and aligned
numeric components. Projected bounded groups are sorted through a temporary
SQLite file; geometry and prior-generation row files are never copied.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib, json, os, re, sqlite3, struct, tempfile
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator
import numpy as np
from .measurement_plan import CellFeatureError
from .phenotyping_population import require_population_sources, population_includes
from mif_pipeline.datasets.cell_store import CellDataset
from mif_pipeline.io.pinned_files import PinnedFileError, open_pinned_member

MARKER_INTENSITY_ROWS_SCHEMA = "mif-marker-intensity-dataset/v1"
MARKER_INTENSITY_ROWS_REFERENCE_SCHEMA = "mif-marker-intensity-dataset-ref/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DESCRIPTOR_FIELDS = frozenset({"schema_version", "content_schema_version", "path", "sha256", "byte_count", "row_count"})
_HEADER_FIELDS = frozenset({"schema_version", "project_entry_id", "slide_id", "section_id", "image_role", "segmentation_generation_id", "measurement_generation_id", "segmentation_population_id", "analysis_domain_ids", "feature_ids", "measurement_names", "coordinate_system", "population_sources"})
_DOMAIN_BINDING_FIELDS = frozenset({"analysis_domain_id", "analysis_domain_revision_sha256"})

class MeasurementRowStreamError(CellFeatureError):
    """

    Reports an invalid measurement file or disagreement with the selected cells and features.
    """


def _error(message: str) -> MeasurementRowStreamError:
    """

    Creates the measurement-reader exception with the supplied cause.

    Args:
        message: Explanation of the failed file, header or cell-row check.

    Returns:
        MeasurementRowStreamError for the calling operation to raise.
    """

    return MeasurementRowStreamError(message)


def _text(value: object, field: str) -> str:
    """

    Requires nonempty unpadded text for a saved measurement or cell identifier.

    Args:
        value: Field text to check before an exact identity comparison.
        field: Name used to identify the field in an error.

    Returns:
        Checked text.

    Raises:
        MeasurementRowStreamError: The value is not an exact string, is empty
            or has surrounding whitespace.
    """

    if type(value) is not str or not value or value != value.strip():
        raise _error(f"{field} must be nonblank canonical text")
    return value


def _digest(value: object) -> str:
    """

    Calculates the checksum of a compact measurement-summary record.

    We encode dictionary keys in sorted order, retain sequence order and reject
    NaN/infinite values. Equivalent dictionary insertion orders therefore give
    the same checksum for the selected summary values.

    Args:
        value: JSON-compatible cell/domain summary.

    Returns:
        Lowercase SHA-256 of the encoded UTF-8 bytes.

    Raises:
        MeasurementRowStreamError: The value cannot be represented as finite JSON.
    """

    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("row commitment is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _sha256(value: object, field: str) -> str:
    """

    Checks the text format of a saved file or cell-population checksum.

    Args:
        value: Expected 64-character lowercase hexadecimal text.
        field: Checksum name included in an error.

    Returns:
        Checked checksum text for comparison with the later calculation.

    Raises:
        MeasurementRowStreamError: Text type, length or hexadecimal characters are invalid.
    """

    value = _text(value, field)
    if _SHA256.fullmatch(value) is None:
        raise _error(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: object, field: str) -> int:
    """

    Requires a positive integer for a saved byte or cell-row count.

    Args:
        value: Expected Python integer; booleans are rejected.
        field: Count name used in the error message.

    Returns:
        Positive integer value.

    Raises:
        MeasurementRowStreamError: The value is not an exact integer greater than zero.
    """

    if type(value) is not int or value <= 0:
        raise _error(f"{field} must be a positive integer")
    return value


def _exact_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    """

    Checks that a saved measurement record has exactly the fields expected by its reader.

    Args:
        value: Decoded Mapping for a file description, header or cell row.
        fields: Complete permitted field-name set.
        field: Record name used in an error.

    Returns:
        Mapping ready for the specific field-value checks.

    Raises:
        MeasurementRowStreamError: The value is not a string-keyed mapping,
            or required fields are missing or additional fields are present.
    """

    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise _error(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise _error(f"{field} does not have its exact closed fields")
    return value


def _exact_tuple(value: object, field: str) -> tuple[object, ...]:
    """

    Checks the tuple representation used by an ordered in-memory measurement description.

    Args:
        value: Candidate tuple of values.
        field: Collection name used in an error.

    Returns:
        Tuple whose item values are checked by the calling operation.

    Raises:
        MeasurementRowStreamError: The supplied collection is not an exact tuple.
    """

    if type(value) is not tuple:
        raise _error(f"{field} must be an exact tuple")
    return value


def _ordered_unique_texts(value: object, field: str, *, sorted_values: bool) -> tuple[str, ...]:
    """

    Checks complete unique identifiers and their required order.

    We require a nonempty tuple of unpadded text values and compare its length
    with its distinct-value count. For sorted domain IDs, we also require
    lexicographic order; feature IDs retain the explicitly selected column order.

    Args:
        value: Tuple of identifiers or measurement names.
        field: Collection name used in errors.
        sorted_values: Whether lexicographic sorting is required.

    Returns:
        Checked tuple of unique text values.

    Raises:
        MeasurementRowStreamError: Tuple type, text values, uniqueness or the
            requested ordering is invalid.
    """

    items = tuple(_text(item, field) for item in _exact_tuple(value, field))
    if (
        not items
        or len(set(items)) != len(items)
        or (sorted_values and items != tuple(sorted(items)))
    ):
        qualifier = "sorted unique" if sorted_values else "ordered unique"
        raise _error(f"{field} must be nonempty {qualifier} text")
    return items


def _absolute_normalized(path: object, field: str) -> Path:
    """

    Requires an absolute native Path in its normalized filesystem spelling.

    Args:
        path: Analysis-output folder or selected measurement-file Path.
        field: Location name used in an error.

    Returns:
        Checked Path for the subsequent folder and file-opening checks.

    Raises:
        MeasurementRowStreamError: The value is not a Path, is relative or
            differs from its normalized native spelling.
    """

    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path != Path(os.path.normpath(os.fspath(path)))
    ):
        raise _error(f"{field} must be an absolute normalized native path")
    return path


@dataclass(frozen=True, slots=True)
class MarkerIntensityRowsDescriptor:
    """

    Identifies the saved marker-intensity file and the amount of data expected from it.

    Attributes:
        path: Absolute path to the compact dataset selection JSON.
        sha256: Checksum of the complete dataset selection bytes.
        byte_count: Expected positive file size.
        row_count: Expected positive number of accepted component cells.
    """

    path: Path
    sha256: str
    byte_count: int
    row_count: int
    content_schema_version: str = MARKER_INTENSITY_ROWS_SCHEMA

    def __post_init__(self) -> None:
        """

        Checks the measurement-file location, checksum text and expected sizes.

        Raises:
            MeasurementRowStreamError: The path, checksum or positive byte/row counts
                have invalid types or values.
        """

        _absolute_normalized(self.path, "path")
        _sha256(self.sha256, "sha256")
        _positive_int(self.byte_count, "byte_count")
        _positive_int(self.row_count, "row_count")
        if self.content_schema_version != MARKER_INTENSITY_ROWS_SCHEMA:
            raise _error("measurement rows content_schema_version is unsupported")

    @classmethod
    def from_mapping(cls, value: object) -> "MarkerIntensityRowsDescriptor":
        """

        Reads the current measurement-file description and its stated row format.

        We require the complete description fields and current file/row format
        names before converting the saved path to a native Path. Construction then
        checks its normalized spelling, checksum and expected counts.

        Args:
            value: Saved measurement-file mapping with path, SHA-256, byte count
                and cell-row count.

        Returns:
            MarkerIntensityRowsDescriptor for the supported row format.

        Raises:
            MeasurementRowStreamError: Fields, formats, path or counts are invalid.
        """

        value = _exact_mapping(value, _DESCRIPTOR_FIELDS, "measurement rows descriptor")
        if value["schema_version"] != MARKER_INTENSITY_ROWS_REFERENCE_SCHEMA:
            raise _error("measurement rows reference schema_version is unsupported")
        if value["content_schema_version"] != MARKER_INTENSITY_ROWS_SCHEMA:
            raise _error("measurement rows content_schema_version is unsupported")
        if (
            type(value["path"]) is not str
            or not os.path.isabs(value["path"])
            or value["path"] != os.path.normpath(value["path"])
        ):
            raise _error("measurement rows descriptor path must be native text")
        return cls(
            path=Path(value["path"]),
            sha256=value["sha256"],
            byte_count=value["byte_count"],
            row_count=value["row_count"],
            content_schema_version=value["content_schema_version"],
        )

    def to_mapping(self) -> dict[str, object]:
        """

        Records the measurement-file path, counts and checksum in the current format.

        Returns:
            Dictionary containing the file description and its row-content format.
        """

        return {
            "schema_version": MARKER_INTENSITY_ROWS_REFERENCE_SCHEMA,
            "content_schema_version": self.content_schema_version,
            "path": str(self.path),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "row_count": self.row_count,
        }


@dataclass(frozen=True, slots=True)
class MeasurementRowStreamExpectation:
    """

    Retains the selected image, cell population, analysis regions and measurement columns expected by a read.

    These values come from the current analysis request. We compare the file
    header with them before delivering any cell rows. Each domain also retains
    its accepted revision checksum for the per-domain result.

    Attributes:
        project_entry_id: Selected mIF entry in the QuPath project.
        slide_id: Slide containing the measured cells.
        section_id: Accepted tissue section used for this measurement result.
        segmentation_generation_id: Accepted segmentation version.
        measurement_generation_id: Measurement version associated with the values.
        segmentation_population_id: Exact segmented-cell population being read.
        analysis_domain_bindings: Sorted region-ID mappings, each including
            its accepted analysis-domain revision checksum.
        feature_ids: Selected marker-feature IDs in numerical column order.
        measurement_names: Exact QuPath measurement names in the same order.
    """

    project_entry_id: str
    slide_id: str
    section_id: str
    segmentation_generation_id: str
    measurement_generation_id: str
    segmentation_population_id: str
    analysis_domain_bindings: tuple[Mapping[str, object], ...]
    feature_ids: tuple[str, ...]
    measurement_names: tuple[str, ...]
    cell_dataset_sha256: str | None = None

    def __post_init__(self) -> None:
        """

        Checks that the expected image and feature selection is complete before reading a file.

        We require nonempty image/population IDs, unique sorted domain IDs with
        valid revision checksums, and unique feature IDs and measurement names.
        The two feature lists must have equal length so every column has both
        its selected ID and native measurement name.

        Domain dictionaries are copied to read-only mappings before the expectation
        is used for the read and its final domain summaries.

        Raises:
            MeasurementRowStreamError: An identity, revision, domain ordering or
                feature-list relationship is invalid.
        """

        for field in (
            "project_entry_id",
            "slide_id",
            "section_id",
            "segmentation_generation_id",
            "measurement_generation_id",
            "segmentation_population_id",
        ):
            _text(getattr(self, field), field)
        if self.cell_dataset_sha256 is not None:
            _sha256(self.cell_dataset_sha256, "accepted base cell dataset SHA-256")
        bindings = _exact_tuple(self.analysis_domain_bindings, "analysis_domain_bindings")
        normalized: list[Mapping[str, object]] = []
        domain_ids: list[str] = []
        for index, binding in enumerate(bindings):
            binding = _exact_mapping(
                binding, _DOMAIN_BINDING_FIELDS, f"analysis_domain_bindings[{index}]"
            )
            domain_id = _text(binding["analysis_domain_id"], "analysis_domain_id")
            revision = _sha256(
                binding["analysis_domain_revision_sha256"], "analysis_domain_revision_sha256"
            )
            domain_ids.append(domain_id)
            normalized.append(
                MappingProxyType(
                    {
                        "analysis_domain_id": domain_id,
                        "analysis_domain_revision_sha256": revision,
                    }
                )
            )
        if not domain_ids or tuple(domain_ids) != tuple(sorted(set(domain_ids))):
            raise _error("analysis_domain_bindings must be sorted unique complete bindings")
        object.__setattr__(self, "analysis_domain_bindings", tuple(normalized))
        _ordered_unique_texts(self.feature_ids, "feature_ids", sorted_values=False)
        _ordered_unique_texts(self.measurement_names, "measurement_names", sorted_values=False)
        if len(self.feature_ids) != len(self.measurement_names):
            raise _error("feature_ids and measurement_names must have equal length")


@dataclass(frozen=True, slots=True)
class MarkerIntensityRowsHeader:
    """

    Retains the measurement header after it has matched the selected analysis inputs.

    Attributes:
        project_entry_id: Selected QuPath mIF entry.
        slide_id: Slide identified by this table.
        section_id: Accepted tissue section containing the cells.
        image_role: Required value "primary_mif".
        segmentation_generation_id: Segmentation version used for the cells.
        measurement_generation_id: Measurement version used for their values.
        segmentation_population_id: Exact segmented-cell population.
        analysis_domain_ids: Sorted accepted region IDs covered by the rows.
        feature_ids: Ordered marker-feature IDs.
        measurement_names: Exact QuPath names aligned with those feature IDs.
        coordinate_system: Required value "global_level0", associating cells
            with their original full-resolution image coordinates.
    """

    project_entry_id: str
    slide_id: str
    section_id: str
    image_role: str
    segmentation_generation_id: str
    measurement_generation_id: str
    segmentation_population_id: str
    analysis_domain_ids: tuple[str, ...]
    feature_ids: tuple[str, ...]
    measurement_names: tuple[str, ...]
    coordinate_system: str
    population_sources: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MeasurementRowDomainCommitment:
    """

    Records which cells were read for one accepted analysis region.

    Attributes:
        analysis_domain_id: Region ID associated with these cell rows.
        analysis_domain_revision_sha256: Accepted region revision from the read request.
        cell_count: Number of rows assigned to this region.
        cell_ids_sha256: Checksum of its ordered cell-ID list.
    """

    analysis_domain_id: str
    analysis_domain_revision_sha256: str
    cell_count: int
    cell_ids_sha256: str

    def to_mapping(self) -> dict[str, object]:
        """

        Collects the region, accepted revision and observed cell count for saving.

        Returns:
            Dictionary including the checksum of that region's ordered cell IDs.
        """

        return {
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "cell_count": self.cell_count,
            "cell_ids_sha256": self.cell_ids_sha256,
        }


@dataclass(frozen=True, slots=True, init=False)
class ValidatedMeasurementRowStream:
    """

    Summarizes a completed measurement read after its header, rows and complete file pass their checks.

    The result retains the data needed to compare a downstream calculation
    with the same measured cells. Per-region counts are collected while reading,
    and the final file checksum confirms the complete byte sequence supplied
    by QuPath.

    Attributes:
        descriptor: Checked file path, expected size/count and complete-file checksum.
        header: Image, population, analysis regions and feature order read from
            the file and compared with the current request.
        cell_count: Total number of validated cell rows.
        cell_ids_sha256: Checksum of the complete ordered cell-ID list.
        cell_to_domain_membership_sha256: Checksum of the ordered cell-to-region mapping.
        domain_cell_commitments: Per-region counts, revision IDs and ordered-cell checksums.
        domain_cell_commitments_sha256: Checksum of the complete region summaries.
        row_inventory_sha256: Checksum of ordered cell-ID, region-ID and value rows.
        feature_ids: Selected feature IDs in their numerical column order.
        measurement_names: Exact native measurement names in that same order.
    """

    descriptor: MarkerIntensityRowsDescriptor
    header: MarkerIntensityRowsHeader
    cell_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    domain_cell_commitments: tuple[MeasurementRowDomainCommitment, ...]
    domain_cell_commitments_sha256: str
    row_inventory_sha256: str
    feature_ids: tuple[str, ...]
    measurement_names: tuple[str, ...]

    @classmethod
    def _create(cls, **values: object) -> "ValidatedMeasurementRowStream":
        """

        Constructs the completed-read summary from the fields checked by the streaming reader.

        Args:
            values: Validated file/header, total and regional cell counts, ordered
                population/value checksums and feature names.

        Returns:
            Frozen ValidatedMeasurementRowStream. The public reading function calls
            this constructor only after its complete-file checks succeed.
        """

        instance = cls.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


class _CanonicalArrayHasher:
    """

    Calculates a JSON-array checksum as ordered cell IDs or value rows arrive.

    We begin with the opening bracket, add each encoded item with its comma
    separator and append the closing bracket to a copy of the digest when
    requesting a result. This gives the checksum of the complete compact JSON
    array while retaining only the current hash state.
    """

    def __init__(self) -> None:
        """

        Initializes the running digest with the JSON array's opening bracket.
        """

        self._digest = hashlib.sha256(b"[")
        self._first = True

    def add(self, value: object) -> None:
        """

        Appends the next item to the running ordered-array checksum.

        Args:
            value: Current cell ID or row values in validated stream order.
        """

        if not self._first:
            self._digest.update(b",")
        self._digest.update(_canonical_bytes(value))
        self._first = False

    def hexdigest(self) -> str:
        """

        Calculates the checksum of the array accumulated so far, including its closing bracket.

        Returns:
            Lowercase SHA-256. The running digest remains available for later rows
            because the closing bracket is added to a copy.
        """

        result = self._digest.copy()
        result.update(b"]")
        return result.hexdigest()


class _CanonicalMappingHasher:
    """

    Calculates the cell-to-region mapping checksum incrementally in sorted cell-ID order.

    We add each encoded key, colon and region value with the required comma
    separator. The public reader verifies increasing cell IDs before calling
    add(), so the resulting mapping has the same key order as a complete
    sorted-key JSON representation.
    """

    def __init__(self) -> None:
        """

        Initializes the running digest with the JSON object's opening brace.
        """

        self._digest = hashlib.sha256(b"{")
        self._first = True

    def add(self, key: str, value: object) -> None:
        """

        Appends one cell and its assigned analysis region to the mapping checksum.

        Args:
            key: Current cell ID in strictly increasing order.
            value: Analysis-region ID assigned to that cell.
        """

        if not self._first:
            self._digest.update(b",")
        self._digest.update(_canonical_bytes(key))
        self._digest.update(b":")
        self._digest.update(_canonical_bytes(value))
        self._first = False

    def hexdigest(self) -> str:
        """

        Calculates the mapping checksum with a closing brace while retaining the running hash state.

        Returns:
            Lowercase SHA-256 of the cell-to-region entries accumulated so far.
        """

        result = self._digest.copy()
        result.update(b"}")
        return result.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    """

    Encodes one ordered row or summary value for the incremental measurement checksums.

    Args:
        value: Cell ID, region value, row list or JSON-compatible summary record.

    Returns:
        Compact UTF-8 JSON bytes with sorted dictionary keys and sequence order retained.

    Raises:
        MeasurementRowStreamError: The value cannot be serialized or contains
            a nonfinite number or invalid text encoding.
    """

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("row material is not canonical JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """

    Builds one decoded JSON object while rejecting repeated field names.

    Args:
        pairs: Ordered field/value pairs supplied by the JSON decoder.

    Returns:
        Dictionary containing the recorded fields once.

    Raises:
        MeasurementRowStreamError: A repeated key would replace an earlier value.
    """

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _parse_json_line(line: bytes) -> Mapping[str, object]:
    """

    Reads one complete header or cell line as a UTF-8 JSON object.

    We reject duplicate keys before dictionary construction and reject JSON
    constants representing NaN or infinity. The decoded value must be an object;
    its specific field names and values are checked by the header or row reader.

    Args:
        line: Complete line bytes without the trailing newline.

    Returns:
        Decoded field mapping.

    Raises:
        MeasurementRowStreamError: UTF-8, JSON syntax, numerical constants,
            key uniqueness or the top-level object type is invalid.
    """

    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                _error(f"nonfinite JSON value {value!r}")
            ),
        )
    except MeasurementRowStreamError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error("measurement row is invalid JSON") from exc
    if type(value) is not dict:
        raise _error("measurement row must be a JSON object")
    return value


def _population_sources(value: object, expected: MeasurementRowStreamExpectation) -> tuple[Mapping[str, object], ...]:
    """Check accepted classifier sources against the measured analysis revisions."""
    if not isinstance(value, list):
        raise _error("measurement population sources must be an array")
    revisions = {item["analysis_domain_id"]: item["analysis_domain_revision_sha256"]
                 for item in expected.analysis_domain_bindings}
    result = []
    seen = set()
    fields = frozenset({"kind", "class_ids", "analysis_domain_id", "analysis_domain_revision_sha256",
                        "source_result_sha256"})
    for raw in value:
        source = _exact_mapping(raw, fields, "population region source")
        kind = source["kind"]
        if type(kind) is not str or kind not in {"primary_structure", "secondary_structure"}:
            raise _error("population source kind is unsupported")
        domain = source["analysis_domain_id"]
        if type(domain) is not str or domain not in revisions or source["analysis_domain_revision_sha256"] != revisions[domain]:
            raise _error("population source uses a stale analysis region")
        _sha256(source["source_result_sha256"], "population source result")
        classes = source["class_ids"]
        if not isinstance(classes, list) or not classes or any(
            type(item) is not str or not item or item != item.strip() or item == "Ignore" for item in classes
        ) or classes != sorted(set(classes)):
            raise _error("population source classes must be sorted unique identifiers")
        for class_id in classes:
            key = (kind, domain, class_id)
            if key in seen:
                raise _error("population source class is duplicated within an analysis region")
            seen.add(key)
        result.append(MappingProxyType(dict(source)))
    return tuple(result)


def _population_membership(value: object, *, domain_id: str, header: MarkerIntensityRowsHeader) -> Mapping[str, object]:
    """Read native centroid coverage without changing a cell's measurement values."""
    value = _exact_mapping(value, frozenset({"detected_tissue", "primary_structure", "secondary_structure"}), "population membership")
    if type(value["detected_tissue"]) is not bool:
        raise _error("detected-tissue membership must be Boolean")
    normalized = {"detected_tissue": value["detected_tissue"]}
    for kind in ("primary_structure", "secondary_structure"):
        classes = value[kind]
        available = {class_id for source in header.population_sources or ()
                     if source["kind"] == kind and source["analysis_domain_id"] == domain_id
                     for class_id in source["class_ids"]}
        if not isinstance(classes, list) or any(type(item) is not str for item in classes) or classes != sorted(set(classes)) or not set(classes) <= available:
            raise _error("cell membership names an unavailable or duplicate structure class")
        normalized[kind] = tuple(classes)
    return MappingProxyType(normalized)


def _parse_header(
    value: Mapping[str, object], expected: MeasurementRowStreamExpectation
) -> MarkerIntensityRowsHeader:
    """

    Checks that the measurement file describes the selected mIF image, cell population and feature order.

    The first record must use the current header format, image_role="primary_mif"
    and coordinate_system="global_level0". Its image entry, slide, section,
    segmentation and measurement IDs must match the expected analysis values.

    We then compare the sorted analysis-region IDs and the two ordered feature
    lists. Feature IDs and native measurement names are unique and retain their
    selected order, ensuring that value position j has the same meaning in
    every subsequent cell row and downstream matrix.

    Args:
        value: Decoded first record from the file.
        expected: Current image, population, region revisions and selected
            feature IDs/measurement names.

    Returns:
        MarkerIntensityRowsHeader with the checked image and feature descriptions.

    Raises:
        MeasurementRowStreamError: Required fields, fixed values, IDs, uniqueness
            or selected list order differ from the read request.
    """

    value = _exact_mapping(value, _HEADER_FIELDS, "measurement dataset header")
    if value["schema_version"] != MARKER_INTENSITY_ROWS_SCHEMA:
        raise _error("measurement rows header schema is unsupported")
    fixed = {"image_role": "primary_mif", "coordinate_system": "global_level0"}
    if any(value[name] != requirement for name, requirement in fixed.items()):
        raise _error("measurement rows header fixed semantics differ")
    for field in (
        "project_entry_id",
        "slide_id",
        "section_id",
        "segmentation_generation_id",
        "measurement_generation_id",
        "segmentation_population_id",
    ):
        if _text(value[field], field) != getattr(expected, field):
            raise _error(f"measurement rows header {field} differs from expected authority")
    arrays: dict[str, tuple[str, ...]] = {}
    for field, authority, sorted_values in (
        (
            "analysis_domain_ids",
            tuple(item["analysis_domain_id"] for item in expected.analysis_domain_bindings),
            True,
        ),
        ("feature_ids", expected.feature_ids, False),
        ("measurement_names", expected.measurement_names, False),
    ):
        raw = value[field]
        if not isinstance(raw, list):
            raise _error(f"measurement rows header {field} must be an array")
        items = tuple(_text(item, field) for item in raw)
        if (
            not items
            or len(items) != len(set(items))
            or (sorted_values and items != tuple(sorted(items)))
        ):
            raise _error(f"measurement rows header {field} is not canonical")
        if items != authority:
            raise _error(f"measurement rows header {field} differs from expected authority")
        arrays[field] = items
    return MarkerIntensityRowsHeader(
        project_entry_id=expected.project_entry_id,
        slide_id=expected.slide_id,
        section_id=expected.section_id,
        image_role="primary_mif",
        segmentation_generation_id=expected.segmentation_generation_id,
        measurement_generation_id=expected.measurement_generation_id,
        segmentation_population_id=expected.segmentation_population_id,
        analysis_domain_ids=arrays["analysis_domain_ids"],
        feature_ids=arrays["feature_ids"],
        measurement_names=arrays["measurement_names"],
        coordinate_system="global_level0",
        population_sources=_population_sources(value["population_sources"], expected),
    )


def _read_selection(root: Path, path: Path, sha256: str | None = None, byte_count: int | None = None) -> dict:
    """Read bounded selection metadata through our shared retained-file owner."""
    path = _absolute_normalized(path, 'dataset selection path')
    try:
        member = path.relative_to(root).as_posix()
        with open_pinned_member(root, member) as opened:
            if opened.size > 16 * 1024**2 or (byte_count is not None and opened.size != byte_count):
                raise _error('Measurement selection metadata exceeds its allowance or expected size')
            data = b''.join(opened.chunks())
            if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
                raise _error('Measurement dataset selection SHA-256 differs')
            return dict(_parse_json_line(data))
    except (PinnedFileError, ValueError) as exc:
        if isinstance(exc, MeasurementRowStreamError):
            raise
        raise _error(f'Measurement selection cannot retain its admitted file: {exc}') from exc


def read_measurement_dataset_selection(descriptor, *, authenticated_output_root):
    """Read our authenticated current selection for a later dataset owner.

    We retain the selection's file authority and exact descriptor digest, then
    expose its base, result fields and sparse checkpoint references. The caller
    uses these references to select existing data; scientific row validation
    remains the responsibility of ``validate_measurement_row_stream``.
    """
    if type(descriptor) is not MarkerIntensityRowsDescriptor:
        raise _error('Measurement selection requires its checked descriptor')
    root = _absolute_normalized(authenticated_output_root, 'authenticated output root')
    selected = _read_selection(root, descriptor.path, descriptor.sha256, descriptor.byte_count)
    _exact_mapping(selected, _HEADER_FIELDS | {'cell_dataset', 'components', 'accepted_component', 'geometry_revision', 'cell_edits'}, 'measurement dataset selection')
    return selected


def _require_finite_selected_batch(batch, fields, *, slide_id, project_entry_id):
    """Check the existing bounded batch and describe at most five invalid cells.

    Count every invalid selected value in this batch, without reading later batches
    or materializing their rows. Cell IDs are accessed only for the bounded examples.
    """
    invalid_count = 0
    examples = []
    for field in fields:
        values = batch.column(field).to_numpy(zero_copy_only=False)
        invalid = ~np.isfinite(values)
        invalid_count += int(np.count_nonzero(invalid))
        if len(examples) < 5:
            for row in np.flatnonzero(invalid)[:5 - len(examples)]:
                cell_id = batch.column("cell_id")[int(row)].as_py()
                examples.append(f"cell {cell_id!r}, {field.removeprefix('measurement:')!r}: {values[row]}")
    if invalid_count:
        raise _error(
            f"Selected clustering measurements contain {invalid_count:,} non-finite values "
            f"in this batch of {batch.num_rows:,} {'cell' if batch.num_rows == 1 else 'cells'} from slide {slide_id!r} "
            f"(image entry {project_entry_id!r}). Examples: " + "; ".join(examples) + ". "
            "Review these cells and their measurement channels and compartments. "
            "Choose finite measurement columns in Clustering setup, or correct the "
            "underlying cell measurements before fitting again."
        )


def iter_selected_measurement_batches(*, descriptor, authenticated_output_root, expected, measurement_names, population_selection=None):
    """Project our accepted immutable measurement columns for model input.

    The measurement owner has already validated the completed publication. We
    retain that compact selection's authority, match its scientific scope and
    read only requested Float64 values and exact applied cell/domain identities.
    We check finite requested values in bounded batches; this downstream read
    does not replay the owner's all-column logical inventory calculation.
    """
    root = _absolute_normalized(authenticated_output_root, 'authenticated output root')
    selected = read_measurement_dataset_selection(descriptor, authenticated_output_root=root)
    header = _parse_header({key: selected[key] for key in _HEADER_FIELDS}, expected)
    if population_selection is not None:
        require_population_sources(population_selection, header)
    names = tuple(measurement_names)
    if len(set(names)) != len(names) or not set(names) <= set(header.measurement_names):
        raise _error('Selected model measurements differ from the accepted feature schema')
    reference = _exact_mapping(selected['cell_dataset'], frozenset({'path', 'sha256'}), 'measurement base dataset')
    if expected.cell_dataset_sha256 is not None and reference['sha256'] != expected.cell_dataset_sha256:
        raise _error('Measurement base dataset differs from accepted segmentation')
    base_path = _absolute_normalized(Path(_text(reference['path'], 'cell dataset path')), 'cell dataset path')
    # The accepted segmentation base can precede an import. Its recorded path
    # and digest remain input authority; new result columns belong to root.
    base_description = _read_selection(base_path.parent, base_path,
        _sha256(reference['sha256'], 'cell dataset SHA-256'))
    if base_description['scope']['population_id'] != expected.segmentation_population_id:
        raise _error('Selected model measurements belong to a different population')
    accepted = _exact_mapping(selected['accepted_component'], frozenset({'path', 'sha256'}), 'accepted measurement component')
    accepted_path = _absolute_normalized(Path(_text(accepted['path'], 'accepted component path')), 'accepted component path')
    _read_selection(root, accepted_path, _sha256(accepted['sha256'], 'accepted component SHA-256'))
    references = selected['components']
    fields = ['measurement:' + name for name in names]
    if not isinstance(references, dict) or not set(fields) <= set(references) or any(not isinstance(scopes, dict) or len(scopes) != 1 or next(iter(scopes.values())) != str(accepted_path) for scopes in references.values()):
        raise _error('Selected measurements require the exact accepted component')
    base = CellDataset(base_path, max_batch_rows=512)
    edits = selected['cell_edits']
    if edits is not None:
        _exact_mapping(edits, frozenset({'path', 'sha256'}), 'current sparse cell checkpoint')
        _read_selection(root, Path(edits['path']), _sha256(edits['sha256'], 'sparse cell checkpoint SHA-256'))
        base = base.with_edits(edits)
    elif selected['geometry_revision'] != 0:
        raise _error('Edited measurements require their current sparse checkpoint')
    view = base.with_components(references, geometry_revision=selected['geometry_revision'])
    memberships = ['population:detected_tissue', 'population:primary_structure', 'population:secondary_structure'] if population_selection is not None and population_selection['kind'] != 'all_segmented' else []
    try:
        for batch in view.iter_applied_batches(accepted_path, columns=['cell_id', 'analysis_domain_id', *fields, *memberships]):
            checked_batch = batch
            if population_selection is not None:
                import pyarrow as pa
                selected_rows = []
                for row in batch.select(['analysis_domain_id', *memberships]).to_pylist():
                    membership = None
                    if memberships:
                        membership = {'detected_tissue': row[memberships[0]]}
                        for name, kind in zip(memberships[1:], ('primary_structure', 'secondary_structure')):
                            if row[name] is None:
                                raise _error('Current measurement population membership cannot be unavailable')
                            membership[kind] = view.dictionaries[name].decode(row[name])
                        membership = _population_membership(membership, domain_id=row['analysis_domain_id'], header=header)
                    selected_rows.append(population_includes(population_selection, membership))
                mask = pa.array(selected_rows, type=pa.bool_())
                checked_batch = batch.filter(mask)
                batch = batch.select(['cell_id', 'analysis_domain_id', *fields]).append_column('phenotyping_selected', mask)
            _require_finite_selected_batch(
                checked_batch, fields, slide_id=expected.slide_id,
                project_entry_id=expected.project_entry_id,
            )
            yield batch
        _read_selection(root, descriptor.path, descriptor.sha256, descriptor.byte_count)
        _read_selection(base_path.parent, base_path, reference['sha256'])
        _read_selection(root, accepted_path, accepted['sha256'])
    finally:
        if edits is not None:
            view._edit_index.close()


def _validate_current_cell_population(connection, base, edit_index, expected_base_population,
                                      current_population=None):
    """Check the current population and reconstruct immutable commitments.

    Aligned component reads already prove each surviving ID and domain belongs
    to the logical base. Created rows must match the selected checkpoint; their
    live and deleted identities are excluded from the immutable commitments.
    """
    connection.execute('CREATE TABLE deleted_cells(cell_id TEXT PRIMARY KEY, domain_id TEXT)')
    connection.execute('CREATE TABLE created_cells(cell_id TEXT PRIMARY KEY, domain_id TEXT, deleted INTEGER)')
    deleted_count = 0
    created_count = 0
    if edit_index is not None:
        created = edit_index.connection.execute('SELECT cell_id FROM edits WHERE created_group_id IS NOT NULL')
        while items := created.fetchmany(min(512, base.max_batch_rows)):
            rows = edit_index.rows([item[0] for item in items])
            connection.executemany('INSERT INTO created_cells VALUES(?,?,?)',
                ((id, row['metadata']['analysis_domain_id'], row['deleted']) for id, row in rows.items()))
        created_count = connection.execute('SELECT COUNT(*) FROM created_cells WHERE deleted=0').fetchone()[0]
        mismatch = connection.execute('SELECT c.cell_id FROM created_cells c LEFT JOIN rows r ON r.cell_id=c.cell_id '
            'WHERE (c.deleted=0 AND (r.cell_id IS NULL OR r.domain_id!=c.domain_id)) OR (c.deleted=1 AND r.cell_id IS NOT NULL) LIMIT 1').fetchone()
        if mismatch:
            raise _error('Measurement population differs from its saved created cells')
        deleted = edit_index.connection.execute('SELECT cell_id FROM edits WHERE deleted=1 AND created_group_id IS NULL')
        while items := deleted.fetchmany(min(512, base.max_batch_rows)):
            ids = [item[0] for item in items]
            found = 0
            for batch in base.lookup_ids(ids, columns=['cell_id', 'analysis_domain_id']):
                rows = zip(batch.column('cell_id').to_pylist(), batch.column('analysis_domain_id').to_pylist())
                connection.executemany('INSERT INTO deleted_cells VALUES(?,?)', rows)
                found += batch.num_rows
            if found != len(ids):
                raise _error('A saved cell deletion is outside the accepted segmentation')
            deleted_count += found
    overlap = connection.execute('SELECT r.cell_id FROM rows r JOIN deleted_cells d ON r.cell_id=d.cell_id LIMIT 1').fetchone()
    count = connection.execute('SELECT COUNT(*) FROM rows').fetchone()[0]
    if overlap or count + deleted_count - created_count != base.row_count:
        raise _error('Measurement population differs from the saved cell corrections; '
            'created and deleted cells must match the saved checkpoint. Refresh measurements after saving the corrections.')
    if expected_base_population is not None:
        original = current_population
        if deleted_count or created_count or original is None:
            ids_hash, mapping_hash = _CanonicalArrayHasher(), _CanonicalMappingHasher()
            for cell_id, domain_id in connection.execute('SELECT r.cell_id,r.domain_id FROM rows r '
                    'LEFT JOIN created_cells c ON c.cell_id=r.cell_id WHERE c.cell_id IS NULL '
                    'UNION ALL SELECT cell_id,domain_id FROM deleted_cells ORDER BY cell_id'):
                ids_hash.add(cell_id)
                mapping_hash.add(cell_id, domain_id)
            original = (count + deleted_count - created_count, ids_hash.hexdigest(), mapping_hash.hexdigest())
        if original != expected_base_population:
            raise _error('Measurement base cell commitments differ from accepted segmentation')


def validate_measurement_row_stream(*, descriptor: MarkerIntensityRowsDescriptor,
    authenticated_output_root: Path, expected: MeasurementRowStreamExpectation,
    provisional_row_callback: Callable[[str, str, tuple[float, ...]], None] | None = None,
    provisional_population_callback: Callable[[str, str, tuple[float, ...], Mapping[str, object]], None] | None = None,
    population_selection: Mapping[str, object] | None = None,
    expected_population_source_ref_sha256s: frozenset[str] | None = None,
    expected_base_population: tuple[int, str, str] | None = None,
) -> ValidatedMeasurementRowStream:
    """Validate projected current measurements before accepting model input.

    Callbacks are provisional until this operation returns. The temporary
    numeric sort preserves scientific row order without retaining geometry or
    a second persistent population export.
    """
    if type(descriptor) is not MarkerIntensityRowsDescriptor or type(expected) is not MeasurementRowStreamExpectation:
        raise _error('Measurement dataset description and expectation require their checked types')
    if any(value is not None and not callable(value) for value in (provisional_row_callback, provisional_population_callback)):
        raise _error('Provisional measurement callbacks must be callable')
    root = _absolute_normalized(authenticated_output_root, 'authenticated output root')
    selected = _read_selection(root, descriptor.path, descriptor.sha256, descriptor.byte_count)
    _exact_mapping(selected, _HEADER_FIELDS | {'cell_dataset', 'components', 'accepted_component', 'geometry_revision', 'cell_edits'}, 'measurement dataset selection')
    header = _parse_header({key: selected[key] for key in _HEADER_FIELDS}, expected)
    if population_selection is not None:
        require_population_sources(population_selection, header)
    if expected_population_source_ref_sha256s is not None and frozenset(source['source_result_sha256'] for source in header.population_sources) != expected_population_source_ref_sha256s:
        raise _error('Phenotyping region classifiers changed; refresh cell measurements before phenotyping')
    reference = _exact_mapping(selected['cell_dataset'], frozenset({'path', 'sha256'}), 'measurement base dataset')
    if expected.cell_dataset_sha256 is not None and reference['sha256'] != expected.cell_dataset_sha256:
        raise _error('Measurement base dataset differs from accepted segmentation')
    base_path = _absolute_normalized(Path(_text(reference['path'], 'cell dataset path')), 'cell dataset path')
    # Retain the original immutable base independently of this operation's
    # new selection/component publication directory.
    base_description = _read_selection(base_path.parent, base_path,
        _sha256(reference['sha256'], 'cell dataset SHA-256'))
    if base_description['scope']['population_id'] != expected.segmentation_population_id:
        raise _error('Measurement base dataset population differs')
    accepted = _exact_mapping(selected['accepted_component'], frozenset({'path', 'sha256'}), 'accepted measurement component')
    accepted_path = _absolute_normalized(Path(_text(accepted['path'], 'accepted component path')), 'accepted component path')
    _read_selection(root, accepted_path, _sha256(accepted['sha256'], 'accepted component SHA-256'))
    references = selected['components']
    if not isinstance(references, dict):
        raise _error('Measurement components require current field selections')
    # This artifact selects only this stage's result. An unapplied row cannot
    # inherit stale measurements from another accepted section or generation.
    if any(not isinstance(scopes, dict) or len(scopes) != 1 or next(iter(scopes.values())) != str(accepted_path) for scopes in references.values()):
        raise _error('Measurements must select the exact accepted component')
    fields = ['measurement:' + name for name in header.measurement_names]
    memberships = ['population:detected_tissue', 'population:primary_structure', 'population:secondary_structure']
    if not set((*fields, *memberships)) <= set(references):
        raise _error('Measurement component omits selected values or population membership')
    base = CellDataset(base_path, max_batch_rows=512)
    immutable_base = base
    edit_reference = selected['cell_edits']
    if edit_reference is not None:
        edit_reference = _exact_mapping(edit_reference, frozenset({'path', 'sha256'}), 'current sparse cell checkpoint')
        edit_path = _absolute_normalized(Path(_text(edit_reference['path'], 'sparse cell checkpoint path')), 'sparse cell checkpoint path')
        _read_selection(root, edit_path, _sha256(edit_reference['sha256'], 'sparse cell checkpoint SHA-256'))
        base = base.with_edits(edit_reference)
    elif selected['geometry_revision'] != 0:
        raise _error('Edited measurement geometry requires its current sparse checkpoint')
    view = base.with_components(references, geometry_revision=selected['geometry_revision'])
    domains = header.analysis_domain_ids
    domain_hashes = {domain: _CanonicalArrayHasher() for domain in domains}
    domain_counts = dict.fromkeys(domains, 0)
    ids_hash, rows_hash, membership_hash = _CanonicalArrayHasher(), _CanonicalArrayHasher(), _CanonicalMappingHasher()
    count = 0
    numeric_format = '<' + 'd' * len(fields)
    with tempfile.TemporaryDirectory(prefix='mif-measurement-sort-') as temporary:
        connection = sqlite3.connect(Path(temporary) / 'values.sqlite')
        try:
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('CREATE TABLE rows(cell_id TEXT PRIMARY KEY, domain_id TEXT, values_blob BLOB, membership TEXT)')
            for batch in view.iter_applied_batches(accepted_path, columns=['cell_id', 'analysis_domain_id', *fields, *memberships]):
                for row in batch.to_pylist():
                    cell_id, domain_id = _text(row['cell_id'], 'cell_id'), _text(row['analysis_domain_id'], 'analysis_domain_id')
                    if domain_id not in domain_counts:
                        raise _error('Measurement cell belongs to a foreign analysis domain')
                    values = tuple(row[name] for name in fields)
                    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) for value in values):
                        raise _error('Selected measurement values must be finite numbers')
                    population = {'detected_tissue': row[memberships[0]]}
                    for name, kind in zip(memberships[1:], ('primary_structure', 'secondary_structure')):
                        if row[name] is None:
                            raise _error('Current measurement population membership cannot be unavailable')
                        population[kind] = view.dictionaries[name].decode(row[name])
                    checked_population = _population_membership(population, domain_id=domain_id, header=header)
                    try:
                        connection.execute('INSERT INTO rows VALUES(?,?,?,?)', (cell_id, domain_id, struct.pack(numeric_format, *values), json.dumps(dict(checked_population))))
                    except sqlite3.IntegrityError as exc:
                        raise _error('Measurement component repeats a stable cell ID') from exc
                    count += 1
                    if count > descriptor.row_count:
                        raise _error('Measurement component contains too many accepted cells')
            if count != descriptor.row_count:
                raise _error('Measurement component accepted count differs')
            connection.commit()
            for cell_id, domain_id, encoded, population in connection.execute('SELECT cell_id,domain_id,values_blob,membership FROM rows ORDER BY cell_id'):
                values = struct.unpack(numeric_format, encoded)
                checked_population = _population_membership(json.loads(population), domain_id=domain_id, header=header)
                ids_hash.add(cell_id);membership_hash.add(cell_id, domain_id)
                domain_hashes[domain_id].add(cell_id);domain_counts[domain_id] += 1
                rows_hash.add([cell_id, domain_id, list(values)])
                if provisional_row_callback is not None:
                    provisional_row_callback(cell_id, domain_id, values)
                if provisional_population_callback is not None:
                    provisional_population_callback(cell_id, domain_id, values, checked_population)
            _validate_current_cell_population(connection, immutable_base,
                None if edit_reference is None else view._edit_index, expected_base_population,
                (count, ids_hash.hexdigest(), membership_hash.hexdigest()))
        finally:
            connection.close()
            if edit_reference is not None:
                view._edit_index.close()
    _read_selection(root, descriptor.path, descriptor.sha256, descriptor.byte_count)
    _read_selection(base_path.parent, base_path, reference['sha256'])
    _read_selection(root, accepted_path, accepted['sha256'])
    if edit_reference is not None:
        _read_selection(root, edit_path, edit_reference['sha256'])
    binding_by_id = {item['analysis_domain_id']: item for item in expected.analysis_domain_bindings}
    commitments = tuple(MeasurementRowDomainCommitment(domain, binding_by_id[domain]['analysis_domain_revision_sha256'], domain_counts[domain], domain_hashes[domain].hexdigest()) for domain in domains)
    return ValidatedMeasurementRowStream._create(descriptor=descriptor, header=header, cell_count=count,
        cell_ids_sha256=ids_hash.hexdigest(), cell_to_domain_membership_sha256=membership_hash.hexdigest(),
        domain_cell_commitments=commitments, domain_cell_commitments_sha256=_digest([item.to_mapping() for item in commitments]),
        row_inventory_sha256=rows_hash.hexdigest(), feature_ids=header.feature_ids, measurement_names=header.measurement_names)
