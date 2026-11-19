"""Dependency-neutral validation for closed GeoJSON polygonal geometry.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from math import isfinite

GeometryRing = tuple[tuple[float, float], ...]
GeometryPolygon = tuple[GeometryRing, ...]


# ==============================================================================
#                               Polygon validation
# ==============================================================================


class PolygonGeometryError(ValueError):
    """Raised when a Polygon/MultiPolygon is malformed or topologically invalid."""


def validate_polygonal_geometry(geometry: object) -> None:
    """Validate finite, simple, positive-area Polygon/MultiPolygon geometry."""
    if not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"}:
        raise PolygonGeometryError("geometry must contain exactly type and coordinates")
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise PolygonGeometryError("geometry type must be Polygon or MultiPolygon")
    polygons = (
        [geometry.get("coordinates")]
        if geometry["type"] == "Polygon"
        else geometry.get("coordinates")
    )
    if not isinstance(polygons, (list, tuple)) or not polygons:
        raise PolygonGeometryError("polygon coordinates must be non-empty")
    validated: list[GeometryPolygon] = []
    for polygon in polygons:
        if not isinstance(polygon, (list, tuple)) or not polygon:
            raise PolygonGeometryError("polygon must contain an exterior ring")
        rings: list[GeometryRing] = []
        for ring in polygon:
            if not isinstance(ring, (list, tuple)) or len(ring) < 4 or ring[0] != ring[-1]:
                raise PolygonGeometryError("polygon rings must be closed")
            points: list[tuple[float, float]] = []
            for point in ring:
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not isfinite(value)
                        for value in point
                    )
                ):
                    raise PolygonGeometryError("polygon coordinates must be finite x/y pairs")
                points.append((float(point[0]), float(point[1])))
            if len(set(points[:-1])) < 3:
                raise PolygonGeometryError("polygon rings must contain three unique vertices")
            if abs(_ring_area2(tuple(points))) <= 1e-12:
                raise PolygonGeometryError("polygon rings must have non-zero area")
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
                        raise PolygonGeometryError(
                            "polygon ring has pathological self-intersection"
                        )
            rings.append(tuple(points))
        shell, holes = rings[0], rings[1:]
        for hole in holes:
            if any(_point_relation(point, shell) != 1 for point in hole[:-1]) or _rings_touch(
                shell, hole
            ):
                raise PolygonGeometryError(
                    "polygon topology requires every hole strictly inside its shell"
                )
        for index, first in enumerate(holes):
            for second in holes[index + 1 :]:
                if (
                    _rings_touch(first, second)
                    or _point_relation(first[0], second) >= 0
                    or _point_relation(second[0], first) >= 0
                ):
                    raise PolygonGeometryError(
                        "polygon topology forbids overlapping or touching holes"
                    )
        shell_area = abs(_ring_area2(shell)) / 2.0
        hole_area = sum(abs(_ring_area2(hole)) / 2.0 for hole in holes)
        if shell_area - hole_area <= 1e-12:
            raise PolygonGeometryError("polygon topology must have positive resulting area")
        validated.append(tuple(rings))
    for index, first in enumerate(validated):
        for second in validated[index + 1 :]:
            if any(_rings_touch(a, b) for a in first for b in second) or (
                _point_in_polygon(first[0][0], second) == 1
                or _point_in_polygon(second[0][0], first) == 1
            ):
                raise PolygonGeometryError(
                    "multipolygon topology requires disjoint non-touching components"
                )


def _ring_area2(ring: GeometryRing) -> float:
    return sum(
        ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
        for index in range(len(ring) - 1)
    )


def _point_relation(point: tuple[float, float], ring: GeometryRing) -> int:
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


__all__ = ["PolygonGeometryError", "validate_polygonal_geometry"]
