"""Authenticated Python-only acquisition of QuPath project pyramid metadata.

No project, ``.qpdata``, image server, image, or network resource is opened. Only one
retained ``.qpproj`` byte sequence is parsed under an explicit profile.

The `ProjectEntryPyramidEvidence` record carries `schema_name`, `schema_version`,
`request_sha256`, `batch_id`, `state_head_sha256` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from math import isfinite
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Iterator, Literal, Mapping, Sequence
from urllib.parse import unquote, urlparse

from mif_pipeline.pipeline_core.win32_file_authority import (
    Win32FileAuthority,
    Win32FileIdentity,
)

PROJECT_PYRAMID_PARSER_PROFILE_ID = "qupath-0.7.0-direct-uri-server-metadata/v1"
_PROJECT_VERSION = "0.7.0"
_MAX_QPPROJ_BYTES = 8 * 1024 * 1024
_MAX_BUILDER_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_ITEMS = 1_000_000
_MAX_PROJECT_ENTRIES = 4096
_MAX_LEVELS = 64
_MAX_CHANNELS = 256
_MAX_TAGS = 128
_MAX_I64 = 2**63 - 1
_BIOFORMATS_PROVIDER = "qupath.lib.images.servers.bioformats.BioFormatsServerBuilder"
_PROCESSED_IMAGE_PROVIDER = "org.mif.image.ProcessedImageServerBuilder"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEX_FLOAT = re.compile(r"-?0x(?:[0-9a-f]+(?:\.[0-9a-f]*)?|\.[0-9a-f]+)p[+-]?[0-9]+\Z")

_QPPROJ_FIELDS = frozenset(
    {"version", "createTimestamp", "modifyTimestamp", "uri", "lastID", "images"}
)
_ENTRY_REQUIRED_FIELDS = frozenset(
    {"serverBuilder", "entryID", "randomizedName", "imageName", "metadata", "tags"}
)
_ENTRY_OPTIONAL_FIELDS = frozenset({"description"})
_BUILDER_FIELDS = frozenset({"builderType", "providerClassName", "uri", "args", "metadata"})
_METADATA_FIELDS = frozenset(
    {
        "name",
        "width",
        "height",
        "sizeZ",
        "sizeT",
        "channelType",
        "isRGB",
        "pixelType",
        "levels",
        "channels",
        "pixelCalibration",
        "preferredTileWidth",
        "preferredTileHeight",
    }
)
_PIXEL_CALIBRATION_FIELDS = frozenset(
    {"pixelWidth", "pixelHeight", "zSpacing", "timeUnit", "timepoints"}
)
_AXIS_FIELDS = frozenset({"value", "unit"})
_SOURCE_LEVEL_FIELDS = frozenset({"downsample", "width", "height"})
_CHANNEL_FIELDS = frozenset({"name", "color"})
_ENTRY_METADATA_FIELDS = frozenset(
    {
        "mif_pipeline.image_role",
        "mif_pipeline.source_image_sha256",
        "mif_pipeline.batch_id",
        "mif_pipeline.sample_id",
        "mif_pipeline.slide_id",
        "mif_pipeline.validation_domain_id",
    }
)
_CALIBRATION_FIELDS = frozenset({"x_um_per_px_hex", "y_um_per_px_hex"})
_LEVEL_FIELDS = frozenset(
    {
        "native_resolution_index",
        "x_downsample_hex",
        "y_downsample_hex",
        "x_um_per_px_hex",
        "y_um_per_px_hex",
    }
)


# ==============================================================================
#                                Project pyramid
# ==============================================================================


class ProjectPyramidContractError(ValueError):
    """The project-entry pyramid input, exchange, or authority is invalid."""


class ProjectPyramidScientificIncompatibility(ProjectPyramidContractError):
    """Well-formed project metadata disagrees with scientific frame authority."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectPyramidContractError("project pyramid material is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProjectPyramidContractError(f"{field} must be nonblank canonical text")
    return value


def _sha(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise ProjectPyramidContractError(f"{field} must be a lowercase SHA-256")
    return digest


def _positive_int(value: object, field: str, *, maximum: int = _MAX_I64) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ProjectPyramidContractError(f"{field} must be a bounded positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_I64:
        raise ProjectPyramidContractError(f"{field} must be a bounded nonnegative integer")
    return value


def _positive_float(value: object, field: str) -> float:
    if type(value) not in {int, float}:
        raise ProjectPyramidContractError(f"{field} must be one finite number")
    number = float(value)
    if not isfinite(number) or number <= 0.0:
        raise ProjectPyramidContractError(f"{field} must be positive and finite")
    return number


def _closed(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ProjectPyramidContractError(f"{field} requires exact fields")
    return value


def _file_uri(value: object, field: str) -> str:
    uri = _text(value, field)
    parsed = urlparse(uri)
    if (
        parsed.scheme.lower() != "file"
        or parsed.netloc not in {"", "localhost"}
        or not parsed.path.startswith("/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectPyramidContractError(f"{field} must be one absolute local file URI")
    return uri


def _local_uri_key(value: object, field: str) -> tuple[str, str]:
    parsed = urlparse(_file_uri(value, field))
    try:
        path = unquote(parsed.path, errors="strict")
    except UnicodeError as exc:
        raise ProjectPyramidContractError(f"{field} contains invalid escaping") from exc
    if os.name == "nt":
        path = path.replace("/", "\\")
        if len(path) >= 3 and path[0] == "\\" and path[2] == ":":
            path = path[1:]
        path = os.path.normcase(os.path.normpath(path))
    else:
        path = os.path.normpath(path)
    return ("file", path)


def _same_file_uri(left: object, right: object, field: str) -> None:
    if _local_uri_key(left, field) != _local_uri_key(right, field):
        raise ProjectPyramidContractError(f"{field} differs from current authority")


def _hex_float(value: object, field: str) -> str:
    encoded = _text(value, field)
    if _HEX_FLOAT.fullmatch(encoded) is None:
        raise ProjectPyramidContractError(f"{field} must be hexadecimal float text")
    try:
        number = float.fromhex(encoded)
    except ValueError as exc:
        raise ProjectPyramidContractError(f"{field} is invalid hexadecimal float text") from exc
    if not isfinite(number) or number <= 0.0:
        raise ProjectPyramidContractError(f"{field} must encode a positive finite value")
    return encoded


def _calibration(value: object) -> Mapping[str, str]:
    mapping = _closed(value, _CALIBRATION_FIELDS, "physical_calibration")
    return MappingProxyType({name: _hex_float(mapping[name], name) for name in sorted(mapping)})


def _levels(value: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProjectPyramidContractError("native pyramid inventory must be an array")
    if not value:
        raise ProjectPyramidContractError("native pyramid inventory must be non-empty")
    if len(value) > _MAX_LEVELS:
        raise ProjectPyramidContractError("native pyramid inventory must contain at most 64 levels")
    rows: list[Mapping[str, object]] = []
    prior = 0.0
    base_x = base_y = 0.0
    for index, item in enumerate(value):
        mapping = _closed(item, _LEVEL_FIELDS, "native pyramid row")
        if (
            type(mapping["native_resolution_index"]) is not int
            or mapping["native_resolution_index"] != index
        ):
            raise ProjectPyramidContractError(
                "native pyramid indices must be contiguous from level 0"
            )
        row: dict[str, object] = {"native_resolution_index": index}
        for name in sorted(_LEVEL_FIELDS - {"native_resolution_index"}):
            row[name] = _hex_float(mapping[name], f"native pyramid {name}")
        downsample = float.fromhex(str(row["x_downsample_hex"]))
        if row["x_downsample_hex"] != row["y_downsample_hex"]:
            raise ProjectPyramidContractError("native pyramid x/y downsamples differ")
        x_um = float.fromhex(str(row["x_um_per_px_hex"]))
        y_um = float.fromhex(str(row["y_um_per_px_hex"]))
        if index == 0:
            if abs(downsample - 1.0) > 1e-12:
                raise ProjectPyramidContractError("native pyramid level 0 must have downsample 1")
            base_x, base_y = x_um, y_um
        if (
            downsample <= prior
            or abs(x_um - base_x * downsample) > max(1e-12, x_um * 1e-12)
            or abs(y_um - base_y * downsample) > max(1e-12, y_um * 1e-12)
        ):
            raise ProjectPyramidContractError(
                "native pyramid calibration or ordering is inconsistent"
            )
        prior = downsample
        rows.append(MappingProxyType(row))
    return tuple(rows)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectPyramidContractError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> object:
    raise ProjectPyramidContractError(f"non-finite JSON number {token!r}")


def _finite_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ProjectPyramidContractError(f"non-finite JSON number {token!r}")
    return value


def _validate_json_bounds(
    value: object, *, depth: int = 0, budget: list[int] | None = None
) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ProjectPyramidContractError("qpproj exceeds the JSON nesting bound")
    remaining = [_MAX_JSON_ITEMS] if budget is None else budget
    remaining[0] -= 1
    if remaining[0] < 0:
        raise ProjectPyramidContractError("qpproj exceeds the JSON item bound")
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_json_bounds(item, depth=depth + 1, budget=remaining)
    elif isinstance(value, list):
        for item in value:
            _validate_json_bounds(item, depth=depth + 1, budget=remaining)


def _parse_qpproj(payload: bytes) -> Mapping[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_QPPROJ_BYTES:
        raise ProjectPyramidContractError("qpproj exceeds its 8 MiB byte bound")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except ProjectPyramidContractError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProjectPyramidContractError("qpproj is invalid UTF-8 JSON") from exc
    _validate_json_bounds(value)
    if not isinstance(value, Mapping):
        raise ProjectPyramidContractError("qpproj JSON must be one object")
    return value


@dataclass(frozen=True, slots=True)
class ProjectEntryPyramidObservation:
    qpproj_snapshot_sha256: str
    parser_profile_id: str
    project_version: str
    server_builder_json: str
    server_builder_sha256: str
    server_builder_metadata_sha256: str
    physical_calibration: Mapping[str, str]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        _sha(self.qpproj_snapshot_sha256, "qpproj_snapshot_sha256")
        if self.parser_profile_id != PROJECT_PYRAMID_PARSER_PROFILE_ID:
            raise ProjectPyramidContractError("project pyramid parser profile is unsupported")
        if self.project_version != _PROJECT_VERSION:
            raise ProjectPyramidContractError("project version is unsupported")
        encoded = _text(self.server_builder_json, "server_builder_json").encode("utf-8")
        if len(encoded) > _MAX_BUILDER_BYTES:
            raise ProjectPyramidContractError("serverBuilder exceeds its byte bound")
        if hashlib.sha256(encoded).hexdigest() != _sha(
            self.server_builder_sha256, "server_builder_sha256"
        ):
            raise ProjectPyramidContractError("serverBuilder checksum is invalid")
        _sha(self.server_builder_metadata_sha256, "server_builder_metadata_sha256")
        object.__setattr__(self, "physical_calibration", _calibration(self.physical_calibration))
        object.__setattr__(self, "native_pyramid_inventory", _levels(self.native_pyramid_inventory))


def _expected_entry_metadata(
    *,
    batch_id: str,
    sample_id: str,
    slide_id: str,
    validation_domain_id: str,
    canonical_image_sha256: str,
) -> dict[str, str]:
    return {
        "mif_pipeline.image_role": "primary_mif",
        "mif_pipeline.source_image_sha256": canonical_image_sha256,
        "mif_pipeline.batch_id": batch_id,
        "mif_pipeline.sample_id": sample_id,
        "mif_pipeline.slide_id": slide_id,
        "mif_pipeline.validation_domain_id": validation_domain_id,
    }


@dataclass(frozen=True, slots=True)
class _ValidatedServerBuilder:
    builder: Mapping[str, object]
    metadata: Mapping[str, object]
    physical_calibration: Mapping[str, str]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]


def _raster_dimension_matches(full_size: int, downsample: float, stored_size: int) -> bool:
    """Allow endpoint raster rounding while retaining QuPath's sampling scale.

    QuPath can retain explicit raster dimensions independently of downsample.
    Pyramid writers may keep or omit a partial boundary pixel. A one-pixel
    terminal level is retained even when the nominal extent is below one.
    """
    extent = full_size / downsample
    if not math.isfinite(extent):
        return False
    nearest = round(extent)
    if abs(extent - nearest) <= 8 * math.ulp(extent):
        extent = float(nearest)
    return stored_size in {max(1, math.floor(extent)), max(1, math.ceil(extent))}


def _validate_server_builder_profile(
    value: object,
    *,
    canonical_image_uri: str,
    level0_size_x_px: int,
    level0_size_y_px: int,
    frame_x_um_per_px: float,
    frame_y_um_per_px: float,
    expected_image_name: str | None,
    image_role: Literal["primary_mif", "moving_reference"] = "primary_mif",
) -> _ValidatedServerBuilder:
    """Validate the builder identity and metadata used to select pyramid levels.

    QuPath may also save metadata such as magnification. We require the fields
    used below and validate their values, without rejecting unrelated metadata.
    """

    builder = _closed(value, _BUILDER_FIELDS, "qpproj serverBuilder")
    if builder["builderType"] != "uri":
        raise ProjectPyramidContractError(
            "qpproj serverBuilder is not the supported direct URI shape"
        )
    if image_role not in {"primary_mif", "moving_reference"}:
        raise ProjectPyramidContractError("serverBuilder image role is unsupported")
    processed = image_role == "primary_mif"
    provider = _PROCESSED_IMAGE_PROVIDER if processed else _BIOFORMATS_PROVIDER
    if builder["providerClassName"] != provider:
        raise ProjectPyramidContractError(
            "qpproj serverBuilder provider is outside the characterized profile"
        )
    _same_file_uri(builder["uri"], canonical_image_uri, "canonical image URI")
    if builder["args"] != ([] if processed else ["--series", "0"]):
        raise ProjectPyramidContractError(
            "qpproj direct URI builder arguments differ from the image role"
        )
    metadata = builder["metadata"]
    if not isinstance(metadata, Mapping) or not _METADATA_FIELDS.issubset(metadata):
        raise ProjectPyramidContractError("qpproj serverBuilder metadata lacks required fields")
    width = _positive_int(metadata["width"], "server metadata width", maximum=2**31 - 1)
    height = _positive_int(metadata["height"], "server metadata height", maximum=2**31 - 1)
    if width != _positive_int(
        level0_size_x_px, "frame level0_size_x_px"
    ) or height != _positive_int(level0_size_y_px, "frame level0_size_y_px"):
        raise ProjectPyramidScientificIncompatibility(
            "server metadata and canonical frame dimensions differ"
        )
    if type(metadata["sizeZ"]) is not int or metadata["sizeZ"] != 1:
        raise ProjectPyramidContractError("server metadata sizeZ must be 1")
    if type(metadata["sizeT"]) is not int or metadata["sizeT"] != 1:
        raise ProjectPyramidContractError("server metadata sizeT must be 1")
    _text(metadata["channelType"], "server metadata channelType")
    if type(metadata["isRGB"]) is not bool:
        raise ProjectPyramidContractError("server metadata isRGB must be boolean")
    _text(metadata["pixelType"], "server metadata pixelType")
    metadata_name = _text(metadata["name"], "server metadata name")
    if processed:
        # Native image metadata names the directory; QuPath names its entry separately.
        expected_image_name = Path(unquote(urlparse(canonical_image_uri).path)).name
    if expected_image_name is not None and metadata_name != expected_image_name:
        raise ProjectPyramidContractError("server metadata name differs from its image source")
    for field in ("preferredTileWidth", "preferredTileHeight"):
        _positive_int(metadata[field], f"server metadata {field}")

    channels = metadata["channels"]
    if not isinstance(channels, list) or not channels or len(channels) > _MAX_CHANNELS:
        raise ProjectPyramidContractError("server metadata channels are invalid")
    names: set[str] = set()
    for raw in channels:
        channel = _closed(raw, _CHANNEL_FIELDS, "server metadata channel")
        name = _text(channel["name"], "server metadata channel name")
        color = channel["color"]
        if name in names or type(color) is not int or not -(2**31) <= color < 2**31:
            raise ProjectPyramidContractError("server metadata channel is invalid")
        names.add(name)

    calibration = _closed(
        metadata["pixelCalibration"],
        _PIXEL_CALIBRATION_FIELDS,
        "server metadata pixelCalibration",
    )
    axis_values: dict[str, float] = {}
    for axis in ("pixelWidth", "pixelHeight"):
        raw = _closed(calibration[axis], _AXIS_FIELDS, f"server metadata {axis}")
        if raw["unit"] != "µm":
            raise ProjectPyramidContractError(
                "server metadata x/y calibration must use micrometres"
            )
        axis_values[axis] = _positive_float(raw["value"], f"server metadata {axis}")
    z_spacing = _closed(calibration["zSpacing"], _AXIS_FIELDS, "server metadata zSpacing")
    _positive_float(z_spacing["value"], "server metadata zSpacing")
    _text(z_spacing["unit"], "server metadata zSpacing unit")
    _text(calibration["timeUnit"], "server metadata timeUnit")
    if not isinstance(calibration["timepoints"], list):
        raise ProjectPyramidContractError("server metadata timepoints must be an array")
    frame_x = _positive_float(frame_x_um_per_px, "frame x calibration")
    frame_y = _positive_float(frame_y_um_per_px, "frame y calibration")
    if axis_values["pixelWidth"] != frame_x or axis_values["pixelHeight"] != frame_y:
        raise ProjectPyramidScientificIncompatibility(
            "server metadata and canonical frame calibration differ"
        )

    raw_levels = metadata["levels"]
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ProjectPyramidScientificIncompatibility("server metadata pyramid must be non-empty")
    if len(raw_levels) > _MAX_LEVELS:
        raise ProjectPyramidScientificIncompatibility(
            "server metadata pyramid must contain at most 64 levels"
        )
    levels: list[Mapping[str, object]] = []
    prior_downsample = 0.0
    prior_width = width + 1
    prior_height = height + 1
    for index, raw in enumerate(raw_levels):
        level = _closed(raw, _SOURCE_LEVEL_FIELDS, "server metadata pyramid level")
        downsample = _positive_float(level["downsample"], "pyramid downsample")
        level_width = _positive_int(level["width"], "pyramid level width", maximum=2**31 - 1)
        level_height = _positive_int(level["height"], "pyramid level height", maximum=2**31 - 1)
        if index == 0 and (downsample != 1.0 or level_width != width or level_height != height):
            raise ProjectPyramidScientificIncompatibility(
                "server metadata pyramid level 0 must match the canonical frame"
            )
        if downsample <= prior_downsample:
            raise ProjectPyramidScientificIncompatibility(
                "server metadata pyramid downsamples must be strictly increasing"
            )
        if not _raster_dimension_matches(
            width, downsample, level_width
        ) or not _raster_dimension_matches(height, downsample, level_height):
            raise ProjectPyramidScientificIncompatibility(
                f"Pyramid level dimensions at index {index}: {level_width} × {level_height} pixels "
                f"with declared downsample {downsample:g} are inconsistent with the full "
                f"image dimensions {width} × {height}."
            )
        if level_width > prior_width or level_height > prior_height:
            raise ProjectPyramidScientificIncompatibility(
                "server metadata pyramid level dimensions are not monotonic"
            )
        prior_downsample = downsample
        prior_width = level_width
        prior_height = level_height
        levels.append(
            MappingProxyType(
                {
                    "native_resolution_index": index,
                    "x_downsample_hex": downsample.hex(),
                    "y_downsample_hex": downsample.hex(),
                    "x_um_per_px_hex": (frame_x * downsample).hex(),
                    "y_um_per_px_hex": (frame_y * downsample).hex(),
                }
            )
        )
    return _ValidatedServerBuilder(
        builder=builder,
        metadata=metadata,
        physical_calibration=MappingProxyType(
            {
                "x_um_per_px_hex": frame_x.hex(),
                "y_um_per_px_hex": frame_y.hex(),
            }
        ),
        native_pyramid_inventory=tuple(levels),
    )


def parse_project_entry_pyramid_payload(
    payload: bytes,
    *,
    project_uri: str,
    project_entry_id: str,
    batch_id: str,
    sample_id: str,
    slide_id: str,
    validation_domain_id: str,
    canonical_image_uri: str,
    canonical_image_sha256: str,
    level0_size_x_px: int,
    level0_size_y_px: int,
    frame_x_um_per_px: float,
    frame_y_um_per_px: float,
) -> ProjectEntryPyramidObservation:
    """Parse only the accepted direct-URI metadata projection."""

    root = _closed(_parse_qpproj(payload), _QPPROJ_FIELDS, "qpproj")
    if root["version"] != _PROJECT_VERSION:
        raise ProjectPyramidContractError(
            "qpproj project version is outside the tested parser profile"
        )
    _nonnegative_int(root["createTimestamp"], "qpproj createTimestamp")
    _nonnegative_int(root["modifyTimestamp"], "qpproj modifyTimestamp")
    _same_file_uri(root["uri"], project_uri, "qpproj project URI")
    last_id = _nonnegative_int(root["lastID"], "qpproj lastID")
    entries = root["images"]
    if not isinstance(entries, list) or len(entries) > _MAX_PROJECT_ENTRIES:
        raise ProjectPyramidContractError("qpproj exceeds its project entry bound")
    if not entries:
        raise ProjectPyramidContractError("qpproj has no project entries")
    try:
        expected_id = int(project_entry_id)
    except (TypeError, ValueError) as exc:
        raise ProjectPyramidContractError(
            "project_entry_id must identify one numeric entryID"
        ) from exc
    if str(expected_id) != project_entry_id or not 0 < expected_id <= _MAX_I64:
        raise ProjectPyramidContractError("project_entry_id must identify one numeric entryID")
    ids: set[int] = set()
    matches: list[Mapping[str, object]] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ProjectPyramidContractError("qpproj entry must be one object")
        fields = set(raw)
        if not (
            _ENTRY_REQUIRED_FIELDS <= fields <= _ENTRY_REQUIRED_FIELDS | _ENTRY_OPTIONAL_FIELDS
        ):
            raise ProjectPyramidContractError("qpproj entry requires exact fields")
        entry_id = _positive_int(raw["entryID"], "qpproj numeric entryID")
        if entry_id in ids:
            raise ProjectPyramidContractError("qpproj duplicates a numeric entryID")
        ids.add(entry_id)
        if entry_id == expected_id:
            matches.append(raw)
    if last_id < max(ids):
        raise ProjectPyramidContractError("qpproj lastID precedes a project entry")
    if len(matches) != 1:
        raise ProjectPyramidContractError("qpproj must contain exactly one target entryID")
    entry = matches[0]
    _text(entry["randomizedName"], "qpproj randomizedName")
    image_name = _text(entry["imageName"], "qpproj imageName")
    if "description" in entry and entry["description"] is not None:
        _text(entry["description"], "qpproj description")
    tags = entry["tags"]
    if (
        not isinstance(tags, list)
        or len(tags) > _MAX_TAGS
        or any(type(tag) is not str or not tag for tag in tags)
        or len(set(tags)) != len(tags)
    ):
        raise ProjectPyramidContractError("qpproj entry tags are invalid")
    entry_metadata = _closed(entry["metadata"], _ENTRY_METADATA_FIELDS, "qpproj entry metadata")
    expected_metadata = _expected_entry_metadata(
        batch_id=batch_id,
        sample_id=sample_id,
        slide_id=slide_id,
        validation_domain_id=validation_domain_id,
        canonical_image_sha256=canonical_image_sha256,
    )
    if dict(entry_metadata) != expected_metadata:
        raise ProjectPyramidContractError(
            "qpproj entry metadata differs from the exact primary_mif projection"
        )

    profile = _validate_server_builder_profile(
        entry["serverBuilder"],
        canonical_image_uri=canonical_image_uri,
        level0_size_x_px=level0_size_x_px,
        level0_size_y_px=level0_size_y_px,
        frame_x_um_per_px=frame_x_um_per_px,
        frame_y_um_per_px=frame_y_um_per_px,
        expected_image_name=image_name,
    )
    builder = profile.builder
    metadata = profile.metadata
    builder_json = _canonical_bytes(builder).decode("utf-8")
    if len(builder_json.encode("utf-8")) > _MAX_BUILDER_BYTES:
        raise ProjectPyramidContractError("serverBuilder exceeds its byte bound")
    return ProjectEntryPyramidObservation(
        qpproj_snapshot_sha256=hashlib.sha256(payload).hexdigest(),
        parser_profile_id=PROJECT_PYRAMID_PARSER_PROFILE_ID,
        project_version=_PROJECT_VERSION,
        server_builder_json=builder_json,
        server_builder_sha256=hashlib.sha256(builder_json.encode("utf-8")).hexdigest(),
        server_builder_metadata_sha256=_digest(metadata),
        physical_calibration=profile.physical_calibration,
        native_pyramid_inventory=profile.native_pyramid_inventory,
    )


@dataclass(frozen=True, slots=True)
class ProjectEntryPyramidRequest:
    schema_name: Literal["mif-qupath-project-entry-pyramid-request"]
    schema_version: Literal[1]
    batch_id: str
    state_head_sha256: str
    active_plan_sha256: str
    execution_authorization_sha256: str
    project_identity_sha256: str
    project_catalogue_sha256: str
    qpproj_snapshot_sha256: str
    project_uri: str
    project_entry_id: str
    entry_projection_sha256: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    canonical_image_uri: str
    canonical_image_sha256: str
    saved_state_attestation_sha256: str
    analysis_frame_ref_sha256: str
    project_catalogue_entry_ref_sha256: str
    spatial_frame_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    frame_x_um_per_px_hex: str
    frame_y_um_per_px_hex: str
    parser_profile_id: str
    project_version: str
    server_builder_json: str
    server_builder_sha256: str
    server_builder_metadata_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectEntryPyramidRequest fields when the record is constructed.

        The checks cover ``project_uri``, ``canonical_image_uri``, ``level0_size_x_px``,
        ``level0_size_y_px``, ``frame_x_um_per_px_hex``, ``frame_y_um_per_px_hex``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (
            self.schema_name != "mif-qupath-project-entry-pyramid-request"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ProjectPyramidContractError("project-entry pyramid request schema is unsupported")
        for name in (
            "batch_id",
            "project_entry_id",
            "sample_id",
            "slide_id",
            "validation_domain_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "state_head_sha256",
            "active_plan_sha256",
            "execution_authorization_sha256",
            "project_identity_sha256",
            "project_catalogue_sha256",
            "qpproj_snapshot_sha256",
            "entry_projection_sha256",
            "canonical_image_sha256",
            "saved_state_attestation_sha256",
            "analysis_frame_ref_sha256",
            "project_catalogue_entry_ref_sha256",
            "spatial_frame_sha256",
            "server_builder_sha256",
            "server_builder_metadata_sha256",
        ):
            _sha(getattr(self, name), name)
        _file_uri(self.project_uri, "project_uri")
        _file_uri(self.canonical_image_uri, "canonical_image_uri")
        _positive_int(self.level0_size_x_px, "level0_size_x_px")
        _positive_int(self.level0_size_y_px, "level0_size_y_px")
        _hex_float(self.frame_x_um_per_px_hex, "frame_x_um_per_px_hex")
        _hex_float(self.frame_y_um_per_px_hex, "frame_y_um_per_px_hex")
        if self.parser_profile_id != PROJECT_PYRAMID_PARSER_PROFILE_ID:
            raise ProjectPyramidContractError("project pyramid parser profile is unsupported")
        if self.project_version != _PROJECT_VERSION:
            raise ProjectPyramidContractError("project version is unsupported")
        encoded = _text(self.server_builder_json, "server_builder_json").encode("utf-8")
        if len(encoded) > _MAX_BUILDER_BYTES:
            raise ProjectPyramidContractError("serverBuilder exceeds its byte bound")
        if hashlib.sha256(encoded).hexdigest() != self.server_builder_sha256:
            raise ProjectPyramidContractError("serverBuilder checksum is invalid")
        try:
            builder = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
                parse_float=_finite_float,
            )
        except (ProjectPyramidContractError, json.JSONDecodeError, ValueError) as exc:
            raise ProjectPyramidContractError("serverBuilder JSON is invalid") from exc
        if _canonical_bytes(builder) != encoded:
            raise ProjectPyramidContractError("serverBuilder JSON is not canonical")
        profile = _validate_server_builder_profile(
            builder,
            canonical_image_uri=self.canonical_image_uri,
            level0_size_x_px=self.level0_size_x_px,
            level0_size_y_px=self.level0_size_y_px,
            frame_x_um_per_px=float.fromhex(self.frame_x_um_per_px_hex),
            frame_y_um_per_px=float.fromhex(self.frame_y_um_per_px_hex),
            expected_image_name=None,
        )
        if _digest(profile.metadata) != self.server_builder_metadata_sha256:
            raise ProjectPyramidContractError("serverBuilder metadata checksum is invalid")
        if _sha(self.request_sha256, "request_sha256") != _digest(self._unsigned_mapping()):
            raise ProjectPyramidContractError("project-entry pyramid request checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "request_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "request_sha256": self.request_sha256}

    @classmethod
    def create(cls, **values: object) -> "ProjectEntryPyramidRequest":
        unsigned = {
            "schema_name": "mif-qupath-project-entry-pyramid-request",
            "schema_version": 1,
            **values,
        }
        return cls(**unsigned, request_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectEntryPyramidRequest":
        mapping = _closed(
            value,
            frozenset(cls.__dataclass_fields__),
            "project-entry pyramid request",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectEntryPyramidEvidence:
    schema_name: Literal["mif-qupath-project-entry-pyramid-evidence"]
    schema_version: Literal[1]
    request_sha256: str
    batch_id: str
    state_head_sha256: str
    active_plan_sha256: str
    execution_authorization_sha256: str
    project_identity_sha256: str
    project_catalogue_sha256: str
    qpproj_snapshot_sha256: str
    project_uri: str
    project_entry_id: str
    entry_projection_sha256: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    canonical_image_uri: str
    canonical_image_sha256: str
    saved_state_attestation_sha256: str
    analysis_frame_ref_sha256: str
    project_catalogue_entry_ref_sha256: str
    spatial_frame_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    frame_x_um_per_px_hex: str
    frame_y_um_per_px_hex: str
    parser_profile_id: str
    project_version: str
    server_builder_json: str
    server_builder_sha256: str
    server_builder_metadata_sha256: str
    physical_calibration: Mapping[str, str]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    native_pyramid_inventory_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectEntryPyramidEvidence fields when the record is constructed.

        The checks cover ``physical_calibration``, ``native_pyramid_inventory``,
        ``schema_name``, ``schema_version``, ``request_sha256``,
        ``native_pyramid_inventory_sha256``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        if (
            self.schema_name != "mif-qupath-project-entry-pyramid-evidence"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ProjectPyramidContractError(
                "project-entry pyramid evidence schema is unsupported"
            )
        request_fields = {
            name: getattr(self, name)
            for name in ProjectEntryPyramidRequest.__dataclass_fields__
            if name not in {"schema_name", "schema_version", "request_sha256"}
        }
        request = ProjectEntryPyramidRequest.create(**request_fields)
        if request.request_sha256 != _sha(self.request_sha256, "request_sha256"):
            raise ProjectPyramidContractError("pyramid evidence request checksum differs")
        calibration = _calibration(self.physical_calibration)
        levels = _levels(self.native_pyramid_inventory)
        profile = _validate_server_builder_profile(
            json.loads(request.server_builder_json),
            canonical_image_uri=request.canonical_image_uri,
            level0_size_x_px=request.level0_size_x_px,
            level0_size_y_px=request.level0_size_y_px,
            frame_x_um_per_px=float.fromhex(request.frame_x_um_per_px_hex),
            frame_y_um_per_px=float.fromhex(request.frame_y_um_per_px_hex),
            expected_image_name=None,
        )
        if dict(calibration) != dict(profile.physical_calibration):
            raise ProjectPyramidContractError(
                "pyramid evidence calibration differs from its request frame"
            )
        if tuple(dict(item) for item in levels) != tuple(
            dict(item) for item in profile.native_pyramid_inventory
        ):
            raise ProjectPyramidContractError(
                "pyramid evidence inventory differs from its request serverBuilder"
            )
        inventory_sha256 = _digest([dict(item) for item in levels])
        if inventory_sha256 != _sha(
            self.native_pyramid_inventory_sha256,
            "native_pyramid_inventory_sha256",
        ):
            raise ProjectPyramidContractError("native pyramid inventory checksum is invalid")
        object.__setattr__(self, "physical_calibration", calibration)
        object.__setattr__(self, "native_pyramid_inventory", levels)
        if _sha(self.evidence_sha256, "evidence_sha256") != _digest(self._unsigned_mapping()):
            raise ProjectPyramidContractError("project-entry pyramid evidence checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for name in self.__dataclass_fields__:
            if name == "evidence_sha256":
                continue
            value = getattr(self, name)
            if name == "physical_calibration":
                value = dict(value)
            elif name == "native_pyramid_inventory":
                value = [dict(item) for item in value]
            result[name] = value
        return result

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def create(
        cls,
        *,
        request: ProjectEntryPyramidRequest,
        physical_calibration: Mapping[str, object],
        native_pyramid_inventory: Sequence[object],
    ) -> "ProjectEntryPyramidEvidence":
        if type(request) is not ProjectEntryPyramidRequest:
            raise ProjectPyramidContractError("pyramid evidence requires one exact request")
        request_values = {
            name: getattr(request, name)
            for name in request.__dataclass_fields__
            if name not in {"schema_name", "schema_version", "request_sha256"}
        }
        calibration = _calibration(physical_calibration)
        levels = _levels(native_pyramid_inventory)
        unsigned = {
            "schema_name": "mif-qupath-project-entry-pyramid-evidence",
            "schema_version": 1,
            "request_sha256": request.request_sha256,
            **request_values,
            "physical_calibration": dict(calibration),
            "native_pyramid_inventory": [dict(item) for item in levels],
            "native_pyramid_inventory_sha256": _digest([dict(item) for item in levels]),
        }
        return cls(**unsigned, evidence_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectEntryPyramidEvidence":
        mapping = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "project-entry pyramid evidence",
            )
        )
        if not isinstance(mapping["native_pyramid_inventory"], list):
            raise ProjectPyramidContractError("native pyramid inventory must be an array")
        mapping["native_pyramid_inventory"] = tuple(mapping["native_pyramid_inventory"])
        return cls(**mapping)  # type: ignore[arg-type]

    def validate_request_binding(self, request: ProjectEntryPyramidRequest) -> None:
        if type(request) is not ProjectEntryPyramidRequest:
            raise ProjectPyramidContractError("pyramid evidence request is invalid")
        request_values = {
            name: getattr(request, name)
            for name in request.__dataclass_fields__
            if name not in {"schema_name", "schema_version", "request_sha256"}
        }
        if self.request_sha256 != request.request_sha256 or any(
            getattr(self, name) != value for name, value in request_values.items()
        ):
            raise ProjectPyramidContractError("pyramid evidence differs from its request")

    def native_levels(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "native_resolution_index": row["native_resolution_index"],
                "x_downsample": float.fromhex(str(row["x_downsample_hex"])),
                "y_downsample": float.fromhex(str(row["y_downsample_hex"])),
                "x_um_per_px": float.fromhex(str(row["x_um_per_px_hex"])),
                "y_um_per_px": float.fromhex(str(row["y_um_per_px_hex"])),
            }
            for row in self.native_pyramid_inventory
        )


def _posix_state(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(stat.S_IFMT(value.st_mode)),
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_nlink),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(getattr(value, "st_file_attributes", 0)),
    )


def _read_descriptor(descriptor: int) -> bytes:
    before = os.fstat(descriptor)
    if before.st_size <= 0 or before.st_size > _MAX_QPPROJ_BYTES:
        raise ProjectPyramidContractError("qpproj exceeds its 8 MiB byte bound")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, min(1024 * 1024, _MAX_QPPROJ_BYTES + 1 - size)):
        size += len(chunk)
        if size > _MAX_QPPROJ_BYTES:
            raise ProjectPyramidContractError("qpproj exceeds its 8 MiB byte bound")
        chunks.append(chunk)
    after = os.fstat(descriptor)
    if _posix_state(before) != _posix_state(after) or size != before.st_size:
        raise ProjectPyramidContractError("project file changed while reading")
    return b"".join(chunks)


def _retain_win32_project_file(
    authority: Win32FileAuthority,
    path: Path,
) -> tuple[object, object, Win32FileIdentity, bytes]:
    """Retain one Windows project reader that denies writes and replacement."""

    parent: object | None = None
    target: object | None = None
    try:
        parent = authority.retain_directory(path.parent.resolve(strict=True), create=False)
        target = authority.retain_read_only_target(parent, path.name)
        identity = authority.read_only_target_identity(target)
        payload = authority.read_read_only_target(
            target,
            maximum_bytes=_MAX_QPPROJ_BYTES,
        )
        if not payload:
            raise ProjectPyramidContractError("qpproj exceeds its 8 MiB byte bound")
        return parent, target, identity, payload
    except BaseException as primary:
        if target is not None:
            try:
                authority.close_read_only_target(target)
            except BaseException as cleanup:  # pragma: no cover
                primary.add_note(f"read-only project target cleanup failed: {cleanup!r}"[:512])
        if parent is not None:
            try:
                authority.close_directory(parent)
            except BaseException as cleanup:  # pragma: no cover
                primary.add_note(f"project parent cleanup failed: {cleanup!r}"[:512])
        raise


class _PinnedProjectFile:
    def __init__(self, path: Path) -> None:
        unresolved = Path(os.path.abspath(os.fspath(path)))
        self.path = unresolved.parent.resolve(strict=True) / unresolved.name
        self.parent_descriptor: int | None = None
        self.parent_identity: tuple[int, int] | None = None
        self.descriptor: int | None = None
        self.opened_state: os.stat_result | None = None
        self.win32: Win32FileAuthority | None = None
        self.win32_parent: object | None = None
        self.win32_target: object | None = None
        self.identity: tuple[int, int] | Win32FileIdentity | None = None
        self.payload = b""
        try:
            if os.name == "nt":
                self.win32 = Win32FileAuthority()
                (
                    self.win32_parent,
                    self.win32_target,
                    self.identity,
                    self.payload,
                ) = _retain_win32_project_file(
                    self.win32,
                    self.path,
                )
            else:
                parent_named = self.path.parent.lstat()
                self.parent_descriptor = os.open(
                    self.path.parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                parent_opened = os.fstat(self.parent_descriptor)
                if (
                    not stat.S_ISDIR(parent_named.st_mode)
                    or not stat.S_ISDIR(parent_opened.st_mode)
                    or (parent_named.st_dev, parent_named.st_ino)
                    != (parent_opened.st_dev, parent_opened.st_ino)
                ):
                    raise ProjectPyramidContractError("project parent identity is unsafe")
                self.parent_identity = (int(parent_opened.st_dev), int(parent_opened.st_ino))
                named = os.stat(
                    self.path.name,
                    dir_fd=self.parent_descriptor,
                    follow_symlinks=False,
                )
                self.descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self.parent_descriptor,
                )
                opened = os.fstat(self.descriptor)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or not stat.S_ISREG(opened.st_mode)
                    or named.st_nlink != 1
                    or opened.st_nlink != 1
                    or _posix_state(named) != _posix_state(opened)
                ):
                    raise ProjectPyramidContractError("project file identity is unsafe")
                self.opened_state = opened
                self.identity = (int(opened.st_dev), int(opened.st_ino))
                self.payload = _read_descriptor(self.descriptor)
            self.sha256 = hashlib.sha256(self.payload).hexdigest()
        except ProjectPyramidContractError:
            self.close()
            raise
        except (OSError, ValueError) as exc:
            self.close()
            raise ProjectPyramidContractError("project file identity cannot be retained") from exc

    def validate_unchanged(self) -> None:
        try:
            if os.name == "nt":
                assert self.win32 is not None
                assert self.win32_parent is not None
                assert self.win32_target is not None
                if (
                    self.win32.read_only_target_identity(self.win32_target) != self.identity
                    or self.win32.read_read_only_target(
                        self.win32_target, maximum_bytes=_MAX_QPPROJ_BYTES
                    )
                    != self.payload
                ):
                    raise ProjectPyramidContractError("project file changed during inspection")
                self.win32.validate_read_only_target_name(
                    self.win32_parent, self.path.name, self.win32_target
                )
                return
            assert self.descriptor is not None and self.opened_state is not None
            assert self.parent_descriptor is not None
            assert self.parent_identity is not None
            parent_named = self.path.parent.lstat()
            parent_opened = os.fstat(self.parent_descriptor)
            named_before = os.stat(
                self.path.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            if (
                (parent_named.st_dev, parent_named.st_ino) != self.parent_identity
                or (parent_opened.st_dev, parent_opened.st_ino) != self.parent_identity
                or _posix_state(os.fstat(self.descriptor)) != _posix_state(self.opened_state)
                or _posix_state(named_before) != _posix_state(self.opened_state)
            ):
                raise ProjectPyramidContractError("project file changed during inspection")
            reopened_payload = _read_descriptor(self.descriptor)
            named_after = os.stat(
                self.path.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            parent_named_after = self.path.parent.lstat()
            parent_opened_after = os.fstat(self.parent_descriptor)
            if (
                reopened_payload != self.payload
                or _posix_state(named_after) != _posix_state(self.opened_state)
                or (parent_named_after.st_dev, parent_named_after.st_ino) != self.parent_identity
                or (parent_opened_after.st_dev, parent_opened_after.st_ino) != self.parent_identity
            ):
                raise ProjectPyramidContractError("project file changed during inspection")
        except ProjectPyramidContractError:
            raise
        except (OSError, ValueError) as exc:
            raise ProjectPyramidContractError("project file changed during inspection") from exc

    def close(self) -> None:
        primary = sys.exc_info()[1]
        failure: BaseException | None = None
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except BaseException as exc:  # pragma: no cover
                failure = exc
            self.descriptor = None
        if self.parent_descriptor is not None:
            try:
                os.close(self.parent_descriptor)
            except BaseException as exc:  # pragma: no cover
                failure = failure or exc
            self.parent_descriptor = None
        if self.win32 is not None and self.win32_target is not None:
            try:
                self.win32.close_read_only_target(self.win32_target)
            except BaseException as exc:  # pragma: no cover
                failure = failure or exc
            self.win32_target = None
        if self.win32 is not None and self.win32_parent is not None:
            try:
                self.win32.close_directory(self.win32_parent)
            except BaseException as exc:  # pragma: no cover
                failure = failure or exc
            self.win32_parent = None
        if failure is not None:
            if primary is not None:
                primary.add_note(
                    f"project pyramid file-authority cleanup failed: {failure!r}"[:512]
                )
            else:
                raise ProjectPyramidContractError(
                    "project pyramid file-authority cleanup failed"
                ) from failure


@contextmanager
def retained_project_file(path: Path) -> Iterator[_PinnedProjectFile]:
    """Retain one exact qpproj identity and the one-time-read bytes."""

    authority = _PinnedProjectFile(path)
    try:
        yield authority
    finally:
        authority.close()


__all__ = [
    "PROJECT_PYRAMID_PARSER_PROFILE_ID",
    "ProjectEntryPyramidEvidence",
    "ProjectEntryPyramidObservation",
    "ProjectEntryPyramidRequest",
    "ProjectPyramidContractError",
    "ProjectPyramidScientificIncompatibility",
    "parse_project_entry_pyramid_payload",
]
