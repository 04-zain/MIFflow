"""Physical locations for inspecting accepted contours and native review samples."""
import math
import numpy as np


def contour_locations(rings, pixel_size, count=12):
    """A bounded set of locations spaced over the combined physical contour length."""
    outlines = []
    for number, ring in enumerate(rings):
        points = np.asarray(ring, dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
            continue
        points = points * np.asarray(pixel_size)
        points = np.concatenate((points, points[:1]))
        area = np.sum(points[:-1, 0]*points[1:, 1] - points[1:, 0]*points[:-1, 1])
        if area == 0:
            continue
        vectors = np.diff(points, axis=0)
        lengths = np.linalg.norm(vectors, axis=1)
        valid = lengths > 0
        starts, vectors, lengths = points[:-1][valid], vectors[valid], lengths[valid]
        if len(lengths) < 3:
            continue
        outlines.append((number, starts, vectors, lengths, np.cumsum(lengths)))
    if not outlines:
        return []
    ring_ends = np.cumsum([outline[-1][-1] for outline in outlines])
    locations = []
    for position, distance in enumerate(np.arange(count) * ring_ends[-1] / count):
        ring = int(np.searchsorted(ring_ends, distance, side='right'))
        number, starts, vectors, lengths, ends = outlines[ring]
        distance -= ring_ends[ring-1] if ring else 0.
        segment = int(np.searchsorted(ends, distance, side='right'))
        fraction = (distance - (ends[segment-1] if segment else 0.)) / lengths[segment]
        point = starts[segment] + fraction * vectors[segment]
        locations.append(dict(point_um=tuple(float(v) for v in point), ring=number, position=position))
    return locations


def grid_sample(grid, column, row, pixel_size):
    """Return the original grid-centre value; no interpolation or new sampling."""
    if not 0 <= column < grid['columns'] or not 0 <= row < grid['rows']:
        return None
    index = row * grid['columns'] + column
    ratio = grid['area_ratios'][index]
    return dict(state=grid['states'][index],
        point_um=((grid['x'] + (column+.5)*grid['spacing']) * pixel_size[0],
                  (grid['y'] + (row+.5)*grid['spacing']) * pixel_size[1]),
        area_ratio=float(ratio) if ratio is not None and math.isfinite(ratio) else None)
