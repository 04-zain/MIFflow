"""Immutable multi-image QuPath exchange for spectral-reference ROIs.

The `ReferenceRoiRecord` record carries `roi_id`, `path_object_id`, `project_entry_id`,
`canonical_image_id`, `source_file_sha256` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import NoReturn, TYPE_CHECKING

if TYPE_CHECKING:
    from .canonical_reference_input import CanonicalReferenceInput

from mif_pipeline.io import CanonicalOmeTiffHeader
from .typed_checksum import typed_checksum_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SOURCE_ROLES = frozenset({"single_component_reference", "unstained_control", "negative_control"})
_ROI_ROLES = frozenset({"positive", "af", "negative", "background"})
_SECTION_KINDS = frozenset({"accepted_tissue_section", "declared_whole_image"})
_REVIEW_STATES = frozenset({"pending", "accepted", "rejected"})


# ==============================================================================
#                                 Reference rois
# ==============================================================================


class ReferenceRoiError(ValueError):
    """A spectral-reference project or ROI artifact is invalid."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be non-empty text without surrounding whitespace")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return text


def _absolute_path(value: object, label: str) -> Path:
    path = Path(value) if isinstance(value, (str, Path)) else None
    if path is None or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceRoiError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ReferenceRoiError(f"non-finite JSON number {value!r} is not permitted")


def _schema(value: object, record_type: type, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReferenceRoiError(f"{label} must be a JSON object")
    expected = {item.name for item in fields(record_type)}
    actual = set(value)
    if actual != expected:
        raise ReferenceRoiError(
            f"{label} schema fields differ; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ReferenceProjectImage:
    """Canonical image identity and level-0 geometry used by one reference input."""

    reference_input_id: str
    canonical_image_id: str
    canonical_pixels_id: str
    source_file_sha256: str
    source_path: Path
    width_px: int
    height_px: int
    x_um_per_px: float
    y_um_per_px: float
    source_role: str
    source_link_id: str

    def __post_init__(self) -> None:
        _text(self.reference_input_id, "reference_input_id")
        _text(self.canonical_image_id, "canonical_image_id")
        _text(self.canonical_pixels_id, "canonical_pixels_id")
        _sha256(self.source_file_sha256, "source_file_sha256")
        _absolute_path(self.source_path, "source_path")
        _positive_integer(self.width_px, "width_px")
        _positive_integer(self.height_px, "height_px")
        _positive_number(self.x_um_per_px, "x_um_per_px")
        _positive_number(self.y_um_per_px, "y_um_per_px")
        if self.source_role not in _SOURCE_ROLES:
            raise ValueError(f"unsupported source_role {self.source_role!r}")
        _text(self.source_link_id, "source_link_id")

    @property
    def canonical_identity(self) -> tuple[str, str]:
        """Composite semantic/content identity; paths are location only."""
        return self.canonical_image_id, self.source_file_sha256

    @classmethod
    def from_canonical_header(
        cls,
        header: CanonicalOmeTiffHeader,
        *,
        reference_input_id: str,
        source_role: str,
        source_link_id: str,
    ) -> "ReferenceProjectImage":
        """Bind one public IO-owned canonical header without reopening TIFF."""
        if not isinstance(header, CanonicalOmeTiffHeader):
            raise TypeError("header must be a CanonicalOmeTiffHeader")
        if header.physical_size_x_um is None or header.physical_size_y_um is None:
            raise ValueError("reference image requires physical X/Y calibration")
        return cls(
            reference_input_id=reference_input_id,
            canonical_image_id=header.image_id,
            canonical_pixels_id=header.pixels_id,
            source_file_sha256=header.sha256,
            source_path=header.path.resolve(),
            width_px=header.size_x,
            height_px=header.size_y,
            x_um_per_px=header.physical_size_x_um,
            y_um_per_px=header.physical_size_y_um,
            source_role=source_role,
            source_link_id=source_link_id,
        )


def reference_project_imported_metadata(
    image: ReferenceProjectImage, canonical_input: CanonicalReferenceInput,
) -> dict[str, object]:
    """Bind native viewing metadata to one already validated reference input.

    The retained source header supplies geometry and the complete measurement axis
    supplies ordered channel labels. No image pixels or native metadata are read.
    """
    from mif_pipeline.io.canonical_ome_tiff import _physical_calibration
    from .canonical_reference_input import CanonicalReferenceInput

    if not isinstance(canonical_input, CanonicalReferenceInput):
        raise TypeError("canonical_inputs must contain validated CanonicalReferenceInput records")
    value = canonical_input
    header = value.header
    x = _physical_calibration(header.physical_size_x, header.physical_size_x_unit, "X")[2]
    y = _physical_calibration(header.physical_size_y, header.physical_size_y_unit, "Y")[2]
    if (
        value.evidence.input_id != image.reference_input_id
        or value.evidence.input_role != ("single_stain_negative_control"
            if image.source_role == "negative_control" else image.source_role)
        or (image.source_role in {"single_component_reference", "negative_control"}
            and value.evidence.fluorophore_identity != image.source_link_id)
        or header.source_file_sha256 != image.source_file_sha256
        or Path(value.locator_binding.source_locator) != image.source_path
        or header.ome_image_id != image.canonical_image_id
        or header.ome_pixels_id != image.canonical_pixels_id
        or (header.size_x, header.size_y) != (image.width_px, image.height_px)
        or x is None or y is None
        or abs(x - image.x_um_per_px) > 1e-9 or abs(y - image.y_um_per_px) > 1e-9
    ):
        raise ReferenceRoiError("imported canonical reference identity or calibration differs")
    if header.size_z != 1 or header.size_t != 1:
        raise ReferenceRoiError("native reference annotation requires one Z plane and one time point")
    names = [measurement.parent_band_id for measurement in value.evidence.axis.measurements]
    if len(names) != header.size_c or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ReferenceRoiError("imported reference channel names do not cover the stored channel axis")
    identity = value.locator_binding.source_identity
    return {"channel_names": names, "source_observations": {
        "size_bytes": identity.size_bytes, "modified_time_ns": identity.modified_time_ns,
        "device_id": identity.device_id, "inode_id": identity.inode_id,
    }}


def validate_reference_project_imported_metadata(
    images: tuple[ReferenceProjectImage, ...], metadata: object,
) -> None:
    """Check the compact native-view projection carried by the existing request."""
    if not isinstance(metadata, list) or len(metadata) != len(images):
        raise ReferenceRoiError("imported metadata must match every reference project image")
    for item in metadata:
        if not isinstance(item, dict) or set(item) != {"channel_names", "source_observations"}:
            raise ReferenceRoiError("imported reference metadata fields differ")
        names = item["channel_names"]
        if not isinstance(names, list) or not names or any(
            not isinstance(name, str) or not name.strip() for name in names
        ):
            raise ReferenceRoiError("imported reference channel names are invalid")
        observations = item["source_observations"]
        if not isinstance(observations, dict) or set(observations) != {
            "size_bytes", "modified_time_ns", "device_id", "inode_id"
        } or any(type(value) is not int or value < 0 for value in observations.values()):
            raise ReferenceRoiError("imported reference file observations are invalid")


@dataclass(frozen=True, slots=True)
class ReferenceSectionScope:
    """One unambiguous accepted tissue section or declared whole-image scope."""

    section_id: str
    scope_kind: str
    tissue_path_object_id: str | None
    section_geometry_json: str | None
    section_geometry_sha256: str | None
    review_state: str

    def __post_init__(self) -> None:
        _text(self.section_id, "section_id")
        if self.scope_kind not in _SECTION_KINDS:
            raise ValueError(f"unsupported section scope_kind {self.scope_kind!r}")
        if self.review_state != "accepted":
            raise ValueError("reference section scope must be accepted")
        if self.scope_kind == "accepted_tissue_section":
            object_id = _text(self.tissue_path_object_id, "tissue_path_object_id")
            if _UUID_RE.fullmatch(object_id) is None:
                raise ValueError("tissue_path_object_id must be a lowercase UUID")
            geometry_json = _text(self.section_geometry_json, "section_geometry_json")
            try:
                geometry = json.loads(
                    geometry_json,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constant,
                )
            except (json.JSONDecodeError, ReferenceRoiError) as exc:
                raise ValueError("section_geometry_json must contain finite valid JSON") from exc
            if not isinstance(geometry, dict):
                raise ValueError("section geometry must be a JSON object")
            _validated_geometry(geometry)
            if _canonical_bytes(geometry).decode("utf-8") != geometry_json:
                raise ValueError("section_geometry_json must use canonical JSON encoding")
            declared = _sha256(self.section_geometry_sha256, "section_geometry_sha256")
            if hashlib.sha256(geometry_json.encode("utf-8")).hexdigest() != declared:
                raise ValueError("section geometry SHA-256 does not match")
        elif (
            self.tissue_path_object_id is not None
            or self.section_geometry_json is not None
            or self.section_geometry_sha256 is not None
        ):
            raise ValueError("declared whole-image section cannot carry tissue geometry")


@dataclass(frozen=True, slots=True)
class ReferenceProjectEntry:
    """Exact QuPath project entry bound to one canonical reference image."""

    project_entry_id: str
    project_entry_path: Path
    image: ReferenceProjectImage
    section_candidates: tuple[ReferenceSectionScope, ...]

    def __post_init__(self) -> None:
        _text(self.project_entry_id, "project_entry_id")
        _absolute_path(self.project_entry_path, "project_entry_path")
        if not isinstance(self.image, ReferenceProjectImage):
            raise TypeError("image must be a ReferenceProjectImage")
        if type(self.section_candidates) is not tuple or not self.section_candidates:
            raise ValueError("section_candidates must be a nonempty tuple")
        if any(not isinstance(item, ReferenceSectionScope) for item in self.section_candidates):
            raise TypeError("section_candidates must contain ReferenceSectionScope records")
        ids = tuple(item.section_id for item in self.section_candidates)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate section_id in project entry")
        whole_image_count = sum(
            item.scope_kind == "declared_whole_image" for item in self.section_candidates
        )
        if whole_image_count and len(self.section_candidates) != 1:
            raise ValueError("whole-image section scope is ambiguous with other candidates")


def _validate_role_source(
    roi_role: str,
    role_link_id: str,
    image: ReferenceProjectImage,
) -> None:
    if roi_role in {"positive", "negative"}:
        expected_role = "single_component_reference" if roi_role == "positive" else "negative_control"
        if image.source_role != expected_role:
            raise ValueError(f"{roi_role} ROI requires a matching {expected_role} input")
        if role_link_id != image.source_link_id:
            raise ValueError(f"{roi_role} ROI link must equal the exact source fluorophore")
    elif roi_role == "af":
        if image.source_role not in {"unstained_control", "negative_control"}:
            raise ValueError("af ROI requires a matching unstained/control input")
        if role_link_id != image.source_link_id:
            raise ValueError("af ROI link must equal the exact control/AF class")
    elif roi_role == "background":
        if role_link_id != image.reference_input_id:
            raise ValueError("background ROI link must equal the exact reference input")


GeometryRing = tuple[tuple[float, float], ...]
GeometryPolygon = tuple[GeometryRing, ...]
ValidatedGeometry = tuple[GeometryPolygon, ...]


def _validated_geometry(geometry: dict[str, object]) -> ValidatedGeometry:
    if set(geometry) != {"type", "coordinates"}:
        raise ValueError("geometry schema must contain exactly type and coordinates")
    geometry_type = geometry["type"]
    raw = geometry["coordinates"]
    if geometry_type == "Polygon":
        polygons = [raw]
    elif geometry_type == "MultiPolygon":
        polygons = raw
    else:
        raise ValueError("geometry type must be Polygon or MultiPolygon")
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("geometry must be nonempty")
    result: list[GeometryPolygon] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError("geometry polygon must be nonempty")
        rings: list[GeometryRing] = []
        for raw_ring in polygon:
            if not isinstance(raw_ring, list) or len(raw_ring) < 4:
                raise ValueError("geometry ring requires at least four coordinates")
            points: list[tuple[float, float]] = []
            for point in raw_ring:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("geometry coordinates must be [x,y] pairs")
                x, y = point
                if (
                    isinstance(x, bool)
                    or isinstance(y, bool)
                    or not isinstance(x, (int, float))
                    or not isinstance(y, (int, float))
                    or not math.isfinite(float(x))
                    or not math.isfinite(float(y))
                ):
                    raise ValueError("geometry coordinates must be finite")
                points.append((float(x), float(y)))
            if points[0] != points[-1]:
                raise ValueError("geometry ring must be closed")
            area2 = sum(
                points[index][0] * points[index + 1][1] - points[index + 1][0] * points[index][1]
                for index in range(len(points) - 1)
            )
            if abs(area2) <= 1e-12:
                raise ValueError("geometry ring must have nonzero area")
            segment_count = len(points) - 1
            for first_index in range(segment_count):
                for second_index in range(first_index + 1, segment_count):
                    if second_index == first_index + 1 or (
                        first_index == 0 and second_index == segment_count - 1
                    ):
                        continue
                    if _segments_intersect(
                        points[first_index],
                        points[first_index + 1],
                        points[second_index],
                        points[second_index + 1],
                    ):
                        raise ValueError("geometry ring has pathological self-intersection")
            rings.append(tuple(points))
        shell = rings[0]
        holes = rings[1:]
        for hole in holes:
            if any(_point_relation(point, shell) != 1 for point in hole[:-1]) or _rings_touch(
                shell, hole
            ):
                raise ValueError("geometry topology requires every hole strictly inside its shell")
        for index, first in enumerate(holes):
            for second in holes[index + 1 :]:
                if (
                    _rings_touch(first, second)
                    or _point_relation(first[0], second) >= 0
                    or _point_relation(second[0], first) >= 0
                ):
                    raise ValueError("geometry topology forbids overlapping or touching holes")
        shell_area = abs(_ring_area2(shell)) / 2.0
        hole_area = sum(abs(_ring_area2(hole)) / 2.0 for hole in holes)
        if shell_area - hole_area <= 1e-12:
            raise ValueError("geometry topology must have positive resulting area")
        result.append(tuple(rings))
    for index, first in enumerate(result):
        for second in result[index + 1 :]:
            if any(_rings_touch(a, b) for a in first for b in second) or (
                _point_in_polygon(first[0][0], second) == 1
                or _point_in_polygon(second[0][0], first) == 1
            ):
                raise ValueError("geometry topology requires disjoint non-touching polygons")
    return tuple(result)


def _geometry_coordinates(geometry: dict[str, object]) -> tuple[GeometryRing, ...]:
    return tuple(ring for polygon in _validated_geometry(geometry) for ring in polygon)


def _ring_area2(ring: GeometryRing) -> float:
    return sum(
        ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
        for index in range(len(ring) - 1)
    )


def _point_relation(point: tuple[float, float], ring: GeometryRing) -> int:
    """Return 1 inside, 0 on boundary, and -1 outside a simple ring."""
    x, y = point
    inside = False
    for index in range(len(ring) - 1):
        a, b = ring[index], ring[index + 1]
        if abs((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])) <= 1e-12 and (
            min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1])
        ):
            return 0
        if (a[1] > y) != (b[1] > y):
            crossing_x = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if crossing_x > x:
                inside = not inside
    return 1 if inside else -1


def _point_in_polygon(point: tuple[float, float], polygon: GeometryPolygon) -> int:
    shell_relation = _point_relation(point, polygon[0])
    if shell_relation <= 0:
        return shell_relation
    for hole in polygon[1:]:
        relation = _point_relation(point, hole)
        if relation == 0:
            return 0
        if relation == 1:
            return -1
    return 1


def _rings_touch(first: GeometryRing, second: GeometryRing) -> bool:
    return any(
        _segments_intersect(first[i], first[i + 1], second[j], second[j + 1])
        for i in range(len(first) - 1)
        for j in range(len(second) - 1)
    )


def _geometries_intersect(first: ValidatedGeometry, second: ValidatedGeometry) -> bool:
    for first_polygon in first:
        for second_polygon in second:
            if any(
                _segments_properly_intersect(a[i], a[i + 1], b[j], b[j + 1])
                for a in first_polygon
                for b in second_polygon
                for i in range(len(a) - 1)
                for j in range(len(b) - 1)
            ):
                return True
            if any(
                _point_in_polygon(point, second_polygon) == 1 for point in first_polygon[0][:-1]
            ):
                return True
            if any(
                _point_in_polygon(point, first_polygon) == 1 for point in second_polygon[0][:-1]
            ):
                return True
            if first_polygon == second_polygon:
                return True
    return False


def _segments_properly_intersect(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> bool:
    def orientation(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return (
        orientation(a, b, c) * orientation(a, b, d) < 0
        and orientation(c, d, a) * orientation(c, d, b) < 0
    )


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orientation(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(
            p[1], r[1]
        )

    values = (
        orientation(a, b, c),
        orientation(a, b, d),
        orientation(c, d, a),
        orientation(c, d, b),
    )
    if values[0] * values[1] < 0 and values[2] * values[3] < 0:
        return True
    return any(
        abs(value) <= 1e-12 and on_segment(start, point, end)
        for value, start, point, end in (
            (values[0], a, c, b),
            (values[1], a, d, b),
            (values[2], c, a, d),
            (values[3], c, b, d),
        )
    )


@dataclass(frozen=True, slots=True)
class ReferenceRoiRecord:
    """One exact QuPath annotation snapshot in global level-0 coordinates."""

    roi_id: str
    path_object_id: str
    project_entry_id: str
    canonical_image_id: str
    source_file_sha256: str
    roi_role: str
    role_link_id: str
    section_id: str
    coordinate_system: str
    geometry_json: str
    geometry_sha256: str
    review_state: str
    reviewer_id: str | None
    reviewed_at: str | None

    def __post_init__(self) -> None:
        """Validate the ReferenceRoiRecord fields when the record is constructed.

        The checks cover ``roi_id``, ``path_object_id``, ``project_entry_id``,
        ``canonical_image_id``, ``source_file_sha256``, ``roi_role``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _text(self.roi_id, "roi_id")
        path_object_id = _text(self.path_object_id, "path_object_id")
        if _UUID_RE.fullmatch(path_object_id) is None:
            raise ValueError("path_object_id must be a lowercase UUID")
        _text(self.project_entry_id, "project_entry_id")
        _text(self.canonical_image_id, "canonical_image_id")
        _sha256(self.source_file_sha256, "source_file_sha256")
        if self.roi_role not in _ROI_ROLES:
            raise ValueError(f"unsupported roi_role {self.roi_role!r}")
        _text(self.role_link_id, "role_link_id")
        _text(self.section_id, "section_id")
        if self.coordinate_system != "global_level0_pixels":
            raise ValueError("coordinate_system must be global_level0_pixels")
        _text(self.geometry_json, "geometry_json")
        try:
            geometry = json.loads(
                self.geometry_json,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, ReferenceRoiError) as exc:
            raise ValueError("geometry_json must contain finite valid JSON") from exc
        if not isinstance(geometry, dict):
            raise ValueError("geometry must be a JSON object")
        _geometry_coordinates(geometry)
        if _canonical_bytes(geometry).decode("utf-8") != self.geometry_json:
            raise ValueError("geometry_json must use canonical JSON encoding")
        expected = hashlib.sha256(self.geometry_json.encode("utf-8")).hexdigest()
        if _sha256(self.geometry_sha256, "geometry_sha256") != expected:
            raise ValueError("geometry SHA-256 does not match")
        if self.review_state not in _REVIEW_STATES:
            raise ValueError(f"unsupported review_state {self.review_state!r}")
        if self.review_state == "pending":
            if self.reviewer_id is not None or self.reviewed_at is not None:
                raise ValueError("pending ROI cannot carry review authority")
        else:
            _text(self.reviewer_id, "reviewer_id")
            _timestamp(self.reviewed_at, "reviewed_at")

    @property
    def review_binding_sha256(self) -> str:
        """Bind every scientific/spatial identity reviewed for this ROI."""
        payload = {
            name: getattr(self, name)
            for name in (
                "roi_id",
                "path_object_id",
                "project_entry_id",
                "canonical_image_id",
                "source_file_sha256",
                "roi_role",
                "role_link_id",
                "section_id",
                "coordinate_system",
                "geometry_json",
                "geometry_sha256",
            )
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _image_mapping(image: ReferenceProjectImage) -> dict[str, object]:
    return {
        "reference_input_id": image.reference_input_id,
        "canonical_image_id": image.canonical_image_id,
        "canonical_pixels_id": image.canonical_pixels_id,
        "source_file_sha256": image.source_file_sha256,
        "source_path": str(image.source_path),
        "width_px": image.width_px,
        "height_px": image.height_px,
        "x_um_per_px": image.x_um_per_px,
        "y_um_per_px": image.y_um_per_px,
        "source_role": image.source_role,
        "source_link_id": image.source_link_id,
    }


def _scope_mapping(scope: ReferenceSectionScope) -> dict[str, object]:
    return {
        "section_id": scope.section_id,
        "scope_kind": scope.scope_kind,
        "tissue_path_object_id": scope.tissue_path_object_id,
        "section_geometry_json": scope.section_geometry_json,
        "section_geometry_sha256": scope.section_geometry_sha256,
        "review_state": scope.review_state,
    }


def _entry_mapping(entry: ReferenceProjectEntry) -> dict[str, object]:
    return {
        "project_entry_id": entry.project_entry_id,
        "project_entry_path": str(entry.project_entry_path),
        "image": _image_mapping(entry.image),
        "section_candidates": [_scope_mapping(item) for item in entry.section_candidates],
    }


def _roi_mapping(roi: ReferenceRoiRecord) -> dict[str, object]:
    return {item.name: getattr(roi, item.name) for item in fields(ReferenceRoiRecord)}


@dataclass(frozen=True, slots=True)
class ReferenceRoleRequirement:
    """Explicit required ROI cardinality for one source and scientific link."""

    roi_role: str
    role_link_id: str
    reference_input_id: str
    minimum_count: int

    def __post_init__(self) -> None:
        if self.roi_role not in _ROI_ROLES:
            raise ValueError(f"unsupported roi_role {self.roi_role!r}")
        _text(self.role_link_id, "role_link_id")
        _text(self.reference_input_id, "reference_input_id")
        _positive_integer(self.minimum_count, "minimum_count")


def _requirement_mapping(requirement: ReferenceRoleRequirement) -> dict[str, object]:
    return {item.name: getattr(requirement, item.name) for item in fields(ReferenceRoleRequirement)}


@dataclass(frozen=True, slots=True)
class ReferenceRoiExport:
    """Checksummed snapshot/review revision for one reference QuPath project."""

    schema_name: str
    schema_version: str
    project_id: str
    project_file: Path
    project_review_state: str
    entries: tuple[ReferenceProjectEntry, ...]
    rois: tuple[ReferenceRoiRecord, ...]
    role_requirements: tuple[ReferenceRoleRequirement, ...]
    created_at: str
    predecessor_content_sha256: str | None
    content_sha256: str

    SCHEMA_NAME = "mif-spectral-reference-roi-export"
    SCHEMA_VERSION = "1"

    def __post_init__(self) -> None:
        """Validate the ReferenceRoiExport fields when the record is constructed.

        The checks cover ``role_requirements``, ``rois``, ``project_id``, ``project_file``,
        ``project_review_state``, ``created_at``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.schema_name != self.SCHEMA_NAME or self.schema_version != self.SCHEMA_VERSION:
            raise ValueError("unsupported reference ROI export schema")
        _text(self.project_id, "project_id")
        _absolute_path(self.project_file, "project_file")
        if self.project_review_state not in _REVIEW_STATES:
            raise ValueError("unsupported project_review_state")
        if type(self.entries) is not tuple or not self.entries:
            raise ValueError("entries must be a nonempty tuple")
        if any(not isinstance(item, ReferenceProjectEntry) for item in self.entries):
            raise TypeError("entries must contain ReferenceProjectEntry records")
        if type(self.rois) is not tuple:
            raise TypeError("rois must be a tuple")
        if any(not isinstance(item, ReferenceRoiRecord) for item in self.rois):
            raise TypeError("rois must contain ReferenceRoiRecord records")
        if type(self.role_requirements) is not tuple or not self.role_requirements:
            raise ValueError("role_requirements must be a nonempty tuple")
        if any(not isinstance(item, ReferenceRoleRequirement) for item in self.role_requirements):
            raise TypeError("role_requirements must contain ReferenceRoleRequirement records")
        entry_ids = tuple(item.project_entry_id for item in self.entries)
        input_ids = tuple(item.image.reference_input_id for item in self.entries)
        identities = tuple(item.image.canonical_identity for item in self.entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("duplicate project_entry_id")
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("duplicate reference_input_id")
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate canonical image identity")
        roi_ids = tuple(item.roi_id for item in self.rois)
        object_ids = tuple(item.path_object_id for item in self.rois)
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("duplicate roi_id")
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("duplicate path_object_id")
        by_entry = {item.project_entry_id: item for item in self.entries}
        by_input = {item.image.reference_input_id: item for item in self.entries}
        requirement_keys = tuple(
            (item.roi_role, item.role_link_id, item.reference_input_id)
            for item in self.role_requirements
        )
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("duplicate reference role requirement")
        for requirement in self.role_requirements:
            entry = by_input.get(requirement.reference_input_id)
            if entry is None:
                raise ValueError("reference role requirement names unknown input")
            _validate_role_source(
                requirement.roi_role,
                requirement.role_link_id,
                entry.image,
            )
        for roi in self.rois:
            entry = by_entry.get(roi.project_entry_id)
            if entry is None:
                raise ValueError(f"ROI {roi.roi_id!r} references unknown project entry")
            if (
                roi.canonical_image_id != entry.image.canonical_image_id
                or roi.source_file_sha256 != entry.image.source_file_sha256
            ):
                raise ValueError(f"ROI {roi.roi_id!r} canonical image identity changed")
            scopes = tuple(
                item for item in entry.section_candidates if item.section_id == roi.section_id
            )
            if len(scopes) != 1:
                raise ValueError(f"ROI {roi.roi_id!r} section is missing or ambiguous")
            _validate_role_source(roi.roi_role, roi.role_link_id, entry.image)
            if (
                roi.roi_role,
                roi.role_link_id,
                entry.image.reference_input_id,
            ) not in set(requirement_keys):
                raise ValueError(f"ROI {roi.roi_id!r} has no explicit role requirement")
            if scopes[0].scope_kind == "accepted_tissue_section":
                roi_geometry = _validated_geometry(json.loads(roi.geometry_json))
                spatial_matches = tuple(
                    candidate
                    for candidate in entry.section_candidates
                    if candidate.scope_kind == "accepted_tissue_section"
                    and _geometries_intersect(
                        roi_geometry,
                        _validated_geometry(json.loads(candidate.section_geometry_json)),
                    )
                )
                if not spatial_matches:
                    raise ValueError(
                        f"ROI {roi.roi_id!r} does not intersect its exact accepted section"
                    )
                if len(spatial_matches) != 1 or spatial_matches[0] != scopes[0]:
                    raise ValueError(f"ROI {roi.roi_id!r} resolves to multiple accepted sections")
            for ring in _geometry_coordinates(json.loads(roi.geometry_json)):
                if any(
                    x < 0 or y < 0 or x > entry.image.width_px or y > entry.image.height_px
                    for x, y in ring
                ):
                    raise ValueError(f"ROI {roi.roi_id!r} geometry is outside image bounds")
        if self.project_review_state == "accepted" and not self.rois:
            raise ValueError("accepted reference project must contain at least one ROI")
        for requirement in self.role_requirements:
            count = sum(
                roi.roi_role == requirement.roi_role
                and roi.role_link_id == requirement.role_link_id
                and by_entry[roi.project_entry_id].image.reference_input_id
                == requirement.reference_input_id
                for roi in self.rois
            )
            if count < requirement.minimum_count:
                raise ValueError(
                    "reference ROI inventory does not meet explicit required cardinality"
                )
        _timestamp(self.created_at, "created_at")
        if self.predecessor_content_sha256 is not None:
            _sha256(self.predecessor_content_sha256, "predecessor_content_sha256")
        declared = _sha256(self.content_sha256, "content_sha256")
        if declared != _export_digest(self):
            raise ValueError("reference ROI export content SHA-256 does not match")

    @property
    def scientific_content_sha256(self) -> str:
        """Path-free scientific snapshot identity; content_sha256 still fences bytes."""

        return hashlib.sha256(_canonical_bytes(_scientific_export_mapping(self))).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        project_file: Path,
        project_review_state: str,
        entries: tuple[ReferenceProjectEntry, ...],
        rois: tuple[ReferenceRoiRecord, ...],
        role_requirements: tuple[ReferenceRoleRequirement, ...],
        created_at: str,
        predecessor_content_sha256: str | None,
    ) -> "ReferenceRoiExport":
        payload = {
            "schema_name": cls.SCHEMA_NAME,
            "schema_version": cls.SCHEMA_VERSION,
            "project_id": project_id,
            "project_file": str(project_file),
            "project_review_state": project_review_state,
            "entries": [_entry_mapping(item) for item in entries],
            "rois": [_roi_mapping(item) for item in rois],
            "role_requirements": [_requirement_mapping(item) for item in role_requirements],
            "created_at": created_at,
            "predecessor_content_sha256": predecessor_content_sha256,
        }
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        return cls(
            schema_name=cls.SCHEMA_NAME,
            schema_version=cls.SCHEMA_VERSION,
            project_id=project_id,
            project_file=project_file,
            project_review_state=project_review_state,
            entries=entries,
            rois=rois,
            role_requirements=role_requirements,
            created_at=created_at,
            predecessor_content_sha256=predecessor_content_sha256,
            content_sha256=digest,
        )


def _export_mapping(export: ReferenceRoiExport, *, include_hash: bool = True) -> dict[str, object]:
    value = {
        "schema_name": export.schema_name,
        "schema_version": export.schema_version,
        "project_id": export.project_id,
        "project_file": str(export.project_file),
        "project_review_state": export.project_review_state,
        "entries": [_entry_mapping(item) for item in export.entries],
        "rois": [_roi_mapping(item) for item in export.rois],
        "role_requirements": [_requirement_mapping(item) for item in export.role_requirements],
        "created_at": export.created_at,
        "predecessor_content_sha256": export.predecessor_content_sha256,
    }
    if include_hash:
        value["content_sha256"] = export.content_sha256
    return value


def _export_digest(export: ReferenceRoiExport) -> str:
    return hashlib.sha256(_canonical_bytes(_export_mapping(export, include_hash=False))).hexdigest()


def _scientific_export_mapping(export: ReferenceRoiExport) -> dict[str, object]:
    """Return reviewed content without mutable project ownership or locators."""

    entries: list[dict[str, object]] = []
    input_by_entry = {
        entry.project_entry_id: entry.image.reference_input_id for entry in export.entries
    }
    for entry in export.entries:
        image = _image_mapping(entry.image)
        del image["source_path"]
        scopes: list[dict[str, object]] = []
        for scope in entry.section_candidates:
            value = _scope_mapping(scope)
            del value["tissue_path_object_id"]
            scopes.append(value)
        entries.append(
            {
                "image": image,
                "section_candidates": scopes,
            }
        )
    rois: list[dict[str, object]] = []
    for roi in export.rois:
        value = _roi_mapping(roi)
        del value["path_object_id"]
        del value["project_entry_id"]
        value["reference_input_id"] = input_by_entry[roi.project_entry_id]
        rois.append(value)
    return {
        "schema_name": export.schema_name,
        "schema_version": export.schema_version,
        "project_review_state": export.project_review_state,
        "entries": entries,
        "rois": rois,
        "role_requirements": [_requirement_mapping(item) for item in export.role_requirements],
    }


def dumps_reference_roi_export(export: ReferenceRoiExport) -> str:
    if not isinstance(export, ReferenceRoiExport):
        raise TypeError("export must be a ReferenceRoiExport")
    return (
        json.dumps(
            _export_mapping(export),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _parse_image(value: object) -> ReferenceProjectImage:
    item = _schema(value, ReferenceProjectImage, "image")
    return ReferenceProjectImage(
        reference_input_id=item["reference_input_id"],
        canonical_image_id=item["canonical_image_id"],
        canonical_pixels_id=item["canonical_pixels_id"],
        source_file_sha256=item["source_file_sha256"],
        source_path=Path(item["source_path"]),
        width_px=item["width_px"],
        height_px=item["height_px"],
        x_um_per_px=item["x_um_per_px"],
        y_um_per_px=item["y_um_per_px"],
        source_role=item["source_role"],
        source_link_id=item["source_link_id"],
    )


def _parse_scope(value: object) -> ReferenceSectionScope:
    item = _schema(value, ReferenceSectionScope, "section scope")
    return ReferenceSectionScope(**item)


def _parse_entry(value: object) -> ReferenceProjectEntry:
    item = _schema(value, ReferenceProjectEntry, "project entry")
    candidates = item["section_candidates"]
    if not isinstance(candidates, list):
        raise ReferenceRoiError("section_candidates must be a JSON array")
    return ReferenceProjectEntry(
        project_entry_id=item["project_entry_id"],
        project_entry_path=Path(item["project_entry_path"]),
        image=_parse_image(item["image"]),
        section_candidates=tuple(_parse_scope(scope) for scope in candidates),
    )


def _parse_roi(value: object) -> ReferenceRoiRecord:
    return ReferenceRoiRecord(**_schema(value, ReferenceRoiRecord, "ROI record"))


def _parse_requirement(value: object) -> ReferenceRoleRequirement:
    return ReferenceRoleRequirement(
        **_schema(value, ReferenceRoleRequirement, "reference role requirement")
    )


def loads_reference_roi_export(source: str | bytes) -> ReferenceRoiExport:
    try:
        root = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReferenceRoiError("reference ROI export is not valid JSON") from exc
    root = _schema(root, ReferenceRoiExport, "reference ROI export")
    entries = root["entries"]
    rois = root["rois"]
    requirements = root["role_requirements"]
    if (
        not isinstance(entries, list)
        or not isinstance(rois, list)
        or not isinstance(requirements, list)
    ):
        raise ReferenceRoiError("entries, rois, and role_requirements must be JSON arrays")
    try:
        return ReferenceRoiExport(
            schema_name=root["schema_name"],
            schema_version=root["schema_version"],
            project_id=root["project_id"],
            project_file=Path(root["project_file"]),
            project_review_state=root["project_review_state"],
            entries=tuple(_parse_entry(item) for item in entries),
            rois=tuple(_parse_roi(item) for item in rois),
            role_requirements=tuple(_parse_requirement(item) for item in requirements),
            created_at=root["created_at"],
            predecessor_content_sha256=root["predecessor_content_sha256"],
            content_sha256=root["content_sha256"],
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ReferenceRoiError):
            raise
        raise ReferenceRoiError(str(exc)) from exc


def write_reference_roi_export(path: str | Path, export: ReferenceRoiExport) -> None:
    """Publish only pending snapshots; reviewed children require project lineage."""
    if not isinstance(export, ReferenceRoiExport):
        raise TypeError("export must be a ReferenceRoiExport")
    if export.predecessor_content_sha256 is not None or any(
        item.review_state != "pending" for item in export.rois
    ):
        raise ReferenceRoiError("reviewed ROI revisions require the authoritative review publisher")
    destination = Path(path)
    if destination.exists():
        raise ReferenceRoiError(f"reference ROI export already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(dumps_reference_roi_export(export))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if destination.exists():
            raise ReferenceRoiError(f"reference ROI export already exists: {destination}")
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise ReferenceRoiError(f"reference ROI export already exists: {destination}") from exc
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ReferenceRoiReviewDecision:
    roi_id: str
    expected_roi_sha256: str
    decision: str

    def __post_init__(self) -> None:
        _text(self.roi_id, "roi_id")
        _sha256(self.expected_roi_sha256, "expected_roi_sha256")
        if self.decision not in {"accepted", "rejected"}:
            raise ValueError("ROI review decision must be accepted or rejected")


@dataclass(frozen=True, slots=True)
class ReferenceRoiReviewResult:
    export: ReferenceRoiExport
    accepted_roi_ids: tuple[str, ...]
    rejected_roi_ids: tuple[str, ...]
    pending_roi_ids: tuple[str, ...]
    stale_roi_ids: tuple[str, ...]


def accept_reference_roi_snapshot(
    snapshot: ReferenceRoiExport,
    *,
    decisions: tuple[ReferenceRoiReviewDecision, ...],
    reviewer_id: str,
    reviewed_at: str,
) -> ReferenceRoiReviewResult:
    """Build an in-memory review revision; this does not authoritatively publish it."""
    if not isinstance(snapshot, ReferenceRoiExport):
        raise TypeError("snapshot must be a ReferenceRoiExport")
    if type(decisions) is not tuple or any(
        not isinstance(item, ReferenceRoiReviewDecision) for item in decisions
    ):
        raise TypeError("decisions must be a tuple of ReferenceRoiReviewDecision records")
    _text(reviewer_id, "reviewer_id")
    _timestamp(reviewed_at, "reviewed_at")
    decision_ids = tuple(item.roi_id for item in decisions)
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("duplicate ROI review decision")
    by_id = {item.roi_id: item for item in snapshot.rois}
    unknown = tuple(item for item in decision_ids if item not in by_id)
    if unknown:
        raise ValueError(f"ROI review decision references unknown ROI {unknown[0]!r}")
    already = tuple(item for item in decision_ids if by_id[item].review_state != "pending")
    if already:
        raise ValueError(f"ROI {already[0]!r} is already reviewed")
    decisions_by_id = {item.roi_id: item for item in decisions}
    updated: list[ReferenceRoiRecord] = []
    stale: list[str] = []
    for roi in snapshot.rois:
        decision = decisions_by_id.get(roi.roi_id)
        if decision is None:
            updated.append(roi)
        elif decision.expected_roi_sha256 != roi.review_binding_sha256:
            stale.append(roi.roi_id)
            updated.append(roi)
        else:
            updated.append(
                replace(
                    roi,
                    review_state=decision.decision,
                    reviewer_id=reviewer_id,
                    reviewed_at=reviewed_at,
                )
            )
    revision = ReferenceRoiExport.create(
        project_id=snapshot.project_id,
        project_file=snapshot.project_file,
        project_review_state=snapshot.project_review_state,
        entries=snapshot.entries,
        rois=tuple(updated),
        role_requirements=snapshot.role_requirements,
        created_at=reviewed_at,
        predecessor_content_sha256=snapshot.content_sha256,
    )
    return ReferenceRoiReviewResult(
        export=revision,
        accepted_roi_ids=tuple(item.roi_id for item in updated if item.review_state == "accepted"),
        rejected_roi_ids=tuple(item.roi_id for item in updated if item.review_state == "rejected"),
        pending_roi_ids=tuple(item.roi_id for item in updated if item.review_state == "pending"),
        stale_roi_ids=tuple(stale),
    )


def accepted_reference_rois(export: ReferenceRoiExport) -> tuple[ReferenceRoiRecord, ...]:
    """Return only individually accepted current geometries for numerical sampling."""
    if not isinstance(export, ReferenceRoiExport):
        raise TypeError("export must be a ReferenceRoiExport")
    if export.project_review_state != "accepted":
        raise ReferenceRoiError("reference project must be accepted before ROI sampling")
    accepted = tuple(item for item in export.rois if item.review_state == "accepted")
    entry_by_id = {item.project_entry_id: item for item in export.entries}
    for requirement in export.role_requirements:
        count = sum(
            item.roi_role == requirement.roi_role
            and item.role_link_id == requirement.role_link_id
            and entry_by_id[item.project_entry_id].image.reference_input_id
            == requirement.reference_input_id
            for item in accepted
        )
        if count < requirement.minimum_count:
            raise ReferenceRoiError("accepted ROI set does not meet explicit required cardinality")
    return accepted


@dataclass(frozen=True, slots=True)
class AcceptedReferenceRoiSelection:
    """One accepted ROI with its exact accepted Task-4 export and project entry."""

    export: ReferenceRoiExport
    roi_id: str
    roi: ReferenceRoiRecord = field(init=False)
    entry: ReferenceProjectEntry = field(init=False)
    selection_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the AcceptedReferenceRoiSelection fields when the record is constructed.

        The checks cover ``roi_id``, ``export``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        if type(self.export) is not ReferenceRoiExport:
            raise TypeError("export must be an exact ReferenceRoiExport")
        roi_id = _text(self.roi_id, "roi_id")
        matches = tuple(
            item for item in accepted_reference_rois(self.export) if item.roi_id == roi_id
        )
        if len(matches) != 1:
            raise ReferenceRoiError("ROI is not one exact accepted member of the Task-4 export")
        roi = matches[0]
        entries = tuple(
            item for item in self.export.entries if item.project_entry_id == roi.project_entry_id
        )
        if len(entries) != 1:
            raise ReferenceRoiError("accepted ROI project entry is missing or ambiguous")
        entry = entries[0]
        payload = {
            "export_content_sha256": self.export.content_sha256,
            "project_id": self.export.project_id,
            "project_entry_id": entry.project_entry_id,
            "reference_input_id": entry.image.reference_input_id,
            "roi_id": roi.roi_id,
            "roi_review_binding_sha256": roi.review_binding_sha256,
        }
        object.__setattr__(self, "roi", roi)
        object.__setattr__(self, "entry", entry)
        object.__setattr__(
            self,
            "selection_sha256",
            hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        )


def select_accepted_reference_roi(
    export: ReferenceRoiExport,
    roi_id: str,
) -> AcceptedReferenceRoiSelection:
    """Resolve one immutable numerical input from a validated accepted export."""

    return AcceptedReferenceRoiSelection(export=export, roi_id=roi_id)


def loads_reference_project_evidence(
    source: str | bytes,
    *,
    request_source: str | bytes,
    expected_request_sha256: str,
    expected_project_id: str,
    expected_project_file: Path,
    expected_images: tuple[ReferenceProjectImage, ...],
    section_scopes: tuple[tuple[str, tuple[ReferenceSectionScope, ...]], ...],
) -> tuple[ReferenceProjectEntry, ...]:
    """Validate QuPath prepare evidence and attach explicit section declarations."""
    request_bytes = (
        request_source.encode("utf-8") if isinstance(request_source, str) else request_source
    )
    if hashlib.sha256(request_bytes).hexdigest() != expected_request_sha256:
        raise ReferenceRoiError("reference project request SHA-256 changed")
    try:
        request = json.loads(
            request_bytes,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReferenceRoiError("reference project request is not valid JSON") from exc
    if not isinstance(request, dict) or set(request) != {
        "schema_version",
        "project_id",
        "project_file",
        "images",
        "imported_metadata",
    }:
        raise ReferenceRoiError("reference project request schema fields differ")
    if (
        request["schema_version"] != "spectral-reference-project-request/v1"
        or request["project_id"] != expected_project_id
        or Path(request["project_file"]) != Path(expected_project_file)
        or not isinstance(request["images"], list)
    ):
        raise ReferenceRoiError("reference project request identity changed")
    request_images = tuple(_parse_image(item) for item in request["images"])
    validate_reference_project_imported_metadata(request_images, request["imported_metadata"])
    if request_images != expected_images:
        raise ReferenceRoiError(
            "expected reference images differ from the immutable prepare request"
        )
    try:
        value = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReferenceRoiError("reference project evidence is not valid JSON") from exc
    root = value
    if not isinstance(root, dict) or set(root) != {
        "schema_version",
        "project_id",
        "project_file",
        "request_sha256",
        "entries",
        "content_sha256",
    }:
        raise ReferenceRoiError("reference project evidence schema fields differ")
    declared_checksum = root["content_sha256"]
    if (
        not isinstance(declared_checksum, str)
        or _SHA256_RE.fullmatch(declared_checksum) is None
        or typed_checksum_sha256(
            {key: item for key, item in root.items() if key != "content_sha256"}
        )
        != declared_checksum
    ):
        raise ReferenceRoiError("reference project evidence content SHA-256 changed")
    if (
        root["schema_version"] != "spectral-reference-project-evidence/v2"
        or root["project_id"] != expected_project_id
        or Path(root["project_file"]) != Path(expected_project_file)
        or root["request_sha256"] != expected_request_sha256
    ):
        raise ReferenceRoiError("reference project evidence identity changed")
    raw_entries = root["entries"]
    if not isinstance(raw_entries, list):
        raise ReferenceRoiError("reference project evidence entries must be an array")
    if type(expected_images) is not tuple or any(
        not isinstance(item, ReferenceProjectImage) for item in expected_images
    ):
        raise TypeError("expected_images must contain ReferenceProjectImage records")
    if type(section_scopes) is not tuple:
        raise TypeError("section_scopes must be a tuple")
    scopes_by_input: dict[str, tuple[ReferenceSectionScope, ...]] = {}
    for item in section_scopes:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("section_scopes items must be (reference_input_id, scopes) tuples")
        input_id, scopes = item
        input_id = _text(input_id, "section scope reference_input_id")
        if input_id in scopes_by_input:
            raise ValueError("duplicate section scope reference_input_id")
        if (
            type(scopes) is not tuple
            or not scopes
            or any(not isinstance(scope, ReferenceSectionScope) for scope in scopes)
        ):
            raise TypeError("section scope values must be nonempty tuples")
        scopes_by_input[input_id] = scopes
    expected_by_input = {item.reference_input_id: item for item in expected_images}
    if set(scopes_by_input) != set(expected_by_input):
        raise ValueError("section scopes must cover every exact reference input")
    entries: list[ReferenceProjectEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {
            "reference_input_id",
            "project_entry_id",
            "project_entry_path",
            "source_uri",
            "canonical_image_id",
            "canonical_pixels_id",
            "width_px",
            "height_px",
            "x_um_per_px",
            "y_um_per_px",
        }:
            raise ReferenceRoiError("reference project entry evidence schema fields differ")
        reference_input_id = raw["reference_input_id"]
        expected = expected_by_input.get(reference_input_id)
        if expected is None:
            raise ReferenceRoiError("canonical image evidence changed")
        for field_name in ("width_px", "height_px"):
            if isinstance(raw[field_name], bool) or not isinstance(raw[field_name], int):
                raise ReferenceRoiError(
                    f"reference project evidence {field_name} must be an integer"
                )
        for field_name in ("x_um_per_px", "y_um_per_px"):
            if (
                isinstance(raw[field_name], bool)
                or not isinstance(raw[field_name], (int, float))
                or not math.isfinite(float(raw[field_name]))
            ):
                raise ReferenceRoiError(f"reference project evidence {field_name} must be finite")
        if (
            raw["source_uri"] != expected.source_path.as_uri()
            or raw["canonical_image_id"] != expected.canonical_image_id
            or raw["canonical_pixels_id"] != expected.canonical_pixels_id
            or raw["width_px"] != expected.width_px
            or raw["height_px"] != expected.height_px
            or abs(float(raw["x_um_per_px"]) - expected.x_um_per_px) > 1e-9
            or abs(float(raw["y_um_per_px"]) - expected.y_um_per_px) > 1e-9
        ):
            raise ReferenceRoiError("canonical image evidence changed")
        entries.append(
            ReferenceProjectEntry(
                project_entry_id=raw["project_entry_id"],
                project_entry_path=Path(raw["project_entry_path"]),
                image=expected,
                section_candidates=scopes_by_input[reference_input_id],
            )
        )
    if len(entries) != len(expected_images) or {
        item.image.reference_input_id for item in entries
    } != set(expected_by_input):
        raise ReferenceRoiError("reference project evidence image inventory changed")
    return tuple(entries)


__all__ = [
    "AcceptedReferenceRoiSelection",
    "ReferenceProjectEntry",
    "ReferenceProjectImage",
    "ReferenceRoiError",
    "ReferenceRoiExport",
    "ReferenceRoiRecord",
    "ReferenceRoiReviewDecision",
    "ReferenceRoiReviewResult",
    "ReferenceRoleRequirement",
    "ReferenceSectionScope",
    "accept_reference_roi_snapshot",
    "accepted_reference_rois",
    "select_accepted_reference_roi",
    "dumps_reference_roi_export",
    "loads_reference_roi_export",
    "loads_reference_project_evidence",
    "write_reference_roi_export",
]
