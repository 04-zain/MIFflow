"""Exact selected-cell counts in a bounded spatial grid and image field."""
from __future__ import annotations

import numpy as np


def neighborhood_counts(grid, radius_y, radius_x):
    """Count selected cells around each grid location using integer window sums."""
    padded = np.pad(np.asarray(grid, dtype=np.int64),
                    ((radius_y, radius_y), (radius_x, radius_x)))
    integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)
    height, width = 2 * radius_y + 1, 2 * radius_x + 1
    return (integral[height:, width:] - integral[:-height, width:]
            - integral[height:, :-width] + integral[:-height, :-width])


def measurement_columns(dataset, measurement):
    column = 'measurement:' + measurement
    presence = 'present:' + column
    return column, presence, tuple(name for name in (column, presence)
                                   if name in dataset.schema.names)


def measured_values(batch, column, presence):
    values = batch.column(column).to_numpy(zero_copy_only=False).astype(float, copy=False)
    valid = np.isfinite(values) & batch.column(column).is_valid().to_numpy(zero_copy_only=False)
    if presence in batch.schema.names:
        valid &= batch.column(presence).fill_null(False).to_numpy(zero_copy_only=False)
    return values, valid


def count_measurement_grid(dataset, measurement, lower, upper, extent_um, pixel_size_um, *, bins=256):
    """Stream saved centroids and values; retain two fixed-size count arrays.

    Extent order is left, right, bottom, top. A cell at the outer right/bottom
    boundary belongs to the final bin. Missing measurements are not included in
    the measured denominator. Unlocated cells remain in the total counts.
    """
    left, right, bottom, top = map(float, extent_um)
    if (not np.isfinite([lower, upper, left, right, bottom, top]).all()
            or lower > upper or right <= left or bottom <= top
            or type(bins) is not int or not 1 <= bins <= 512):
        raise ValueError('Choose a finite measurement range and positive image extent.')
    column, presence, fields = measurement_columns(dataset, measurement)
    if column not in fields:
        raise ValueError('Select an available saved measurement.')
    measured = np.zeros((bins, bins), dtype=np.int64)
    selected = np.zeros_like(measured)
    total_measured = total_selected = 0
    for batch in dataset.iter_batches(columns=('x_level0', 'y_level0', *fields)):
        values, valid = measured_values(batch, column, presence)
        matches = valid & (values >= lower) & (values <= upper)
        total_measured += int(valid.sum())
        total_selected += int(matches.sum())
        x = batch.column('x_level0').to_numpy(zero_copy_only=False) * pixel_size_um[0]
        y = batch.column('y_level0').to_numpy(zero_copy_only=False) * pixel_size_um[1]
        located = np.isfinite(x) & np.isfinite(y) & (x >= left) & (x <= right) & (y >= top) & (y <= bottom)
        indices = np.flatnonzero(located & valid)
        ix = np.minimum(bins - 1, ((x[indices] - left) / (right - left) * bins).astype(int))
        iy = np.minimum(bins - 1, ((y[indices] - top) / (bottom - top) * bins).astype(int))
        flat = iy * bins + ix
        measured += np.bincount(flat, minlength=bins*bins).reshape(bins, bins)
        selected += np.bincount(flat[matches[indices]], minlength=bins*bins).reshape(bins, bins)
    for array in (measured, selected):
        array.setflags(write=False)
    return dict(grid_measured=measured, grid_selected=selected, grid_extent_um=tuple(extent_um),
                total_measured=total_measured, total_selected=total_selected,
                unlocated_selected=total_selected-int(selected.sum()),
                unlocated_measured=total_measured-int(measured.sum()))


def count_measurement_field(dataset, measurement, lower, upper, bounds_level0):
    """Count the visible field by centroid using the existing spatial index."""
    column, presence, fields = measurement_columns(dataset, measurement)
    measured = selected = 0
    for batch in dataset.query(bounds_level0, columns=fields, predicate='centroid'):
        values, valid = measured_values(batch, column, presence)
        measured += int(valid.sum())
        selected += int((valid & (values >= lower) & (values <= upper)).sum())
    return dict(field_measured=measured, field_selected=selected)
