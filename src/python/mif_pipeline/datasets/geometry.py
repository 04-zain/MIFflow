"""Exact planar polygon WKB and rectangle predicates in image coordinates.

Encoding preserves every supplied double-precision vertex, ring and component.
The existing polygon owner supplies the boundary-inclusive numerical predicates.
"""

from math import isfinite
import struct
from mif_pipeline.pipeline_core.polygon_validation import _point_in_polygon, _segments_intersect


def polygon_wkb(geometry):
    """Encode a Polygon/MultiPolygon and return its complete finite bounds.

    This storage boundary checks the supported planar representation. Native
    finalization remains responsible for the scientific geometry/topology; we
    preserve its vertices without simplification or centroid reconstruction.
    """
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind not in {"Polygon", "MultiPolygon"} or not coordinates:
        raise ValueError("Geometry requires nonempty Polygon or MultiPolygon outlines")
    polygons = [coordinates] if kind == "Polygon" else coordinates
    bounds = [float("inf"), float("inf"), -float("inf"), -float("inf")]

    def encode(polygon):
        result = bytearray(struct.pack("<BI", 1, 3))
        result.extend(struct.pack("<I", len(polygon)))
        if not polygon:
            raise ValueError("A polygon requires an exterior ring")
        for ring in polygon:
            if len(ring) < 4 or tuple(ring[0]) != tuple(ring[-1]):
                raise ValueError("Polygon rings must be closed")
            result.extend(struct.pack("<I", len(ring)))
            for point in ring:
                if len(point) != 2 or not all(not isinstance(value, bool) and isfinite(value) for value in point):
                    raise ValueError("Polygon coordinates must be finite planar pairs")
                x, y = point
                bounds[0] = min(bounds[0], x); bounds[1] = min(bounds[1], y)
                bounds[2] = max(bounds[2], x); bounds[3] = max(bounds[3], y)
                result.extend(struct.pack("<dd", x, y))
        return bytes(result)

    if kind == "Polygon":
        result = encode(coordinates)
    else:
        result = struct.pack("<BII", 1, 6, len(polygons)) + b"".join(encode(polygon) for polygon in polygons)
    return result, tuple(bounds)


def wkb_polygon(value):
    """Decode supported planar WKB while retaining ring and component order."""
    offset = 0

    def read(fmt):
        nonlocal offset
        result = struct.unpack_from(fmt, value, offset)
        offset += struct.calcsize(fmt)
        return result

    def geometry():
        marker, = read("B")
        if marker not in {0, 1}:
            raise ValueError("Invalid WKB byte order")
        endian = "<" if marker else ">"
        kind, = read(endian + "I")
        count, = read(endian + "I")
        if kind == 3:
            rings = []
            for _ in range(count):
                size, = read(endian + "I")
                rings.append([list(read(endian + "dd")) for _ in range(size)])
            return {"type": "Polygon", "coordinates": rings}
        if kind == 6:
            polygons = [geometry() for _ in range(count)]
            if any(polygon["type"] != "Polygon" for polygon in polygons):
                raise ValueError("MultiPolygon WKB requires polygon components")
            return {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in polygons]}
        raise ValueError("Unsupported cell WKB geometry type")

    try:
        result = geometry()
    except struct.error as error:
        raise ValueError("Incomplete cell WKB") from error
    if offset != len(value):
        raise ValueError("Cell WKB has trailing bytes")
    return result


def intersects_rectangle(value, bounds):
    """Test exact polygon intersection, including ring boundaries and holes."""
    geometry = wkb_polygon(value)
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    x0, y0, x1, y1 = bounds
    corners = ((x0,y0),(x1,y0),(x1,y1),(x0,y1))
    edges = tuple(zip(corners, (*corners[1:], corners[0])))
    for polygon in polygons:
        if any(_point_in_polygon(point, polygon) >= 0 for point in corners):
            return True
        for ring in polygon:
            for a, b in zip(ring, ring[1:]):
                if x0 <= a[0] <= x1 and y0 <= a[1] <= y1:
                    return True
                if any(_segments_intersect(a,b,c,d) for c,d in edges):
                    return True
    return False
