"""Bounded image geometry for the frozen Nimbus-Inference 0.0.5 adapter.

Storage windows never define an inference boundary: normalization, resize
coordinates, augmentation and reflection all refer to the complete source
image. This module imports NumPy only and does not load a model or a slide.
Its sampler implements OpenCV 4's resize coordinates and float32 bilinear
arithmetic, with an explicit IPP/classic coordinate mode. SIMD rounding can
differ by a few float32 ULPs. Model equivalence
must be validated separately with the pinned runtime and checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterator, Protocol, Sequence

import numpy as np


INPUT_SIZE = 1024
VALID_OUTPUT_SIZE = 836
OUTPUT_SIZE = 832
HALO = 96
TRANSFORMS = (0, 1, 2, 3, 4, 5)
# Released 0.0.5 closes over the loop variable in all four rotation lambdas,
# so each observes k=3 at execution. Preserve that actual released inference
# implementation rather than silently replacing it with four distinct angles.
UPSTREAM_TRANSFORMS = (3, 3, 3, 3, 4, 5)


class WindowSource(Protocol):
    """Native, unresized pixels and finalized dense cell labels, zero background."""

    width: int
    height: int
    magnification: float
    channels: tuple[str, ...]

    def read_pixels(self, x: int, y: int, width: int, height: int,
                    channel_names: Sequence[str]) -> np.ndarray: ...

    def read_labels(self, x: int, y: int, width: int, height: int) -> np.ndarray: ...


@dataclass(frozen=True)
class TilePlan:
    """One valid-UNet input and its owned, clipped augmented-image output.

    Bounds use (x, y, width, height). Prediction slices address the raw 836
    output, including Nimbus's two-pixel crop and clipping at the image edge.
    ``restore_output`` maps that region into original model-image coordinates.
    """

    scaled_shape: tuple[int, int]
    transform: int
    input_bounds: tuple[int, int, int, int]
    output_bounds: tuple[int, int, int, int]
    prediction_slices: tuple[slice, slice]


def _shape(shape: Sequence[int]) -> tuple[int, int]:
    if len(shape) != 2 or any(isinstance(v, (bool, np.bool_)) or
                              not isinstance(v, (int, np.integer)) or v < 1 for v in shape):
        raise ValueError("Nimbus image shape must contain two positive integer dimensions")
    return int(shape[0]), int(shape[1])


def _transform(transform: int) -> int:
    if isinstance(transform, (bool, np.bool_)) or transform not in TRANSFORMS:
        raise ValueError("Nimbus transform must be 0..5: rot0/90/180/270, flipY, flipX")
    return int(transform)


def scaled_shape(source: WindowSource) -> tuple[int, int]:
    """Use upstream truncation at the model's fixed 10x magnification."""
    h, w = _shape((source.height, source.width))
    magnification = source.magnification
    if isinstance(magnification, (bool, np.bool_)) or not isfinite(magnification) or magnification <= 0:
        raise ValueError("Nimbus source magnification must be finite and positive")
    return _shape((int(h * (10 / magnification)), int(w * (10 / magnification))))


def transformed_shape(shape: Sequence[int], transform: int) -> tuple[int, int]:
    h, w = _shape(shape)
    return (w, h) if _transform(transform) in (1, 3) else (h, w)


def global_tile_plan(shape: Sequence[int], transform: int = 0) -> Iterator[TilePlan]:
    """Yield upstream's globally centered valid-UNet grid in row-major order.

    Small images require the separate reflect-model path. The released 0.0.5
    empty-stitch bug is rejected rather than silently changing its behavior.
    """
    shape = _shape(shape)
    transform = _transform(transform)
    if max(shape) <= INPUT_SIZE:
        raise ValueError("Nimbus images up to 1024 per axis require the reflect-model path")
    h, w = transformed_shape(shape, transform)
    if h % OUTPUT_SIZE == OUTPUT_SIZE - 1 or w % OUTPUT_SIZE == OUTPUT_SIZE - 1:
        raise ValueError("Nimbus-Inference 0.0.5 cannot stitch a dimension congruent to 831 modulo 832")
    pad_y = (INPUT_SIZE - h % OUTPUT_SIZE + 1) // 2
    pad_x = (INPUT_SIZE - w % OUTPUT_SIZE + 1) // 2
    for row in range(h // OUTPUT_SIZE + 1):
        input_y = row * OUTPUT_SIZE - pad_y
        output_y = input_y + HALO
        y0, y1 = max(output_y, 0), min(output_y + OUTPUT_SIZE, h)
        for col in range(w // OUTPUT_SIZE + 1):
            input_x = col * OUTPUT_SIZE - pad_x
            output_x = input_x + HALO
            x0, x1 = max(output_x, 0), min(output_x + OUTPUT_SIZE, w)
            yield TilePlan(shape, transform, (input_x, input_y, INPUT_SIZE, INPUT_SIZE),
                           (x0, y0, x1 - x0, y1 - y0),
                           (slice(2 + y0 - output_y, 2 + y1 - output_y),
                            slice(2 + x0 - output_x, 2 + x1 - output_x)))


def restored_output_bounds(plan: TilePlan) -> tuple[int, int, int, int]:
    """Locate the owned output in original model coordinates without a prediction."""
    x, y, w, h = plan.output_bounds
    original_h, original_w = plan.scaled_shape
    transform = plan.transform
    if transform == 1:
        bounds = (original_w - y - h, x, h, w)
    elif transform == 2:
        bounds = (original_w - x - w, original_h - y - h, w, h)
    elif transform == 3:
        bounds = (y, original_h - x - w, h, w)
    elif transform == 4:
        bounds = (x, original_h - y - h, w, h)
    elif transform == 5:
        bounds = (original_w - x - w, y, w, h)
    else:
        bounds = (x, y, w, h)
    return bounds


def restore_output(prediction: np.ndarray, plan: TilePlan) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Crop one raw valid-model output, undo TTA, and return original-image bounds."""
    prediction = np.asarray(prediction)
    if prediction.shape != (VALID_OUTPUT_SIZE, VALID_OUTPUT_SIZE):
        raise ValueError("Nimbus valid-model prediction must have shape (836, 836)")
    cropped = prediction[plan.prediction_slices]
    if plan.transform < 4:
        result = np.rot90(cropped, -plan.transform)
    else:
        result = np.flip(cropped, axis=0 if plan.transform == 4 else 1)
    return result, restored_output_bounds(plan)


def native_output_bounds(source: WindowSource, prediction_shape: tuple[int, int],
                         bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Conservatively enclose native pixels receiving one model output rectangle.

    Reduction uses exact nearest-neighbor indices within this enclosing window.
    The extra native pixel on each edge preserves that existing rounding guard;
    occupancy must use the same window so it cannot omit a contributing pixel.
    """
    model_height, model_width = prediction_shape
    x, y, width, height = bounds
    left = max(0, (x * source.width) // model_width - 1)
    right = min(source.width, ((x + width) * source.width + model_width - 1) // model_width + 1)
    top = max(0, (y * source.height) // model_height - 1)
    bottom = min(source.height, ((y + height) * source.height + model_height - 1) // model_height + 1)
    return left, top, right - left, bottom - top


def nearest_indices(destination_indices: np.ndarray, source_length: int,
                    destination_length: int) -> np.ndarray:
    """OpenCV 4 INTER_NEAREST's floor-origin convention, including reciprocal rounding.

    The same helper maps native scoring pixels to model predictions and maps
    model binary-mask pixels to native labels. It is not INTER_NEAREST_EXACT.
    """
    _shape((source_length, destination_length))
    indices = np.asarray(destination_indices)
    if indices.dtype.kind not in "iu" or np.any(indices < 0) or np.any(indices >= destination_length):
        raise ValueError("Nearest-neighbor indices must be inside the destination axis")
    scale = 1.0 / (destination_length / source_length)
    return np.minimum(np.floor(indices.astype(np.float64) * scale).astype(np.int64), source_length - 1)


def _reflect(indices: np.ndarray, length: int) -> np.ndarray:
    if length == 1:
        return np.zeros(indices.shape, dtype=np.int64)
    period = 2 * length - 2
    remainder = indices % period
    return np.minimum(remainder, period - remainder)


def _groups(indices: np.ndarray, size: int):
    blocks = indices // size
    for block in np.unique(blocks):
        positions = np.flatnonzero(blocks == block)
        yield positions, indices[positions]


def _normalized_points(source: WindowSource, channel: str, factor: float,
                       rows: np.ndarray, cols: np.ndarray, read_size: int) -> np.ndarray:
    result = np.empty((len(rows), len(cols)), dtype=np.float32)
    col_groups = list(_groups(cols, read_size))
    for row_positions, ys in _groups(rows, read_size):
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        for col_positions, xs in col_groups:
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            raw = np.asarray(source.read_pixels(x0, y0, x1 - x0, y1 - y0, (channel,)))
            if raw.shape != (1, y1 - y0, x1 - x0) or raw.dtype.kind not in "uif" or not np.isfinite(raw).all():
                raise ValueError("Nimbus native pixels must be finite CHW values matching the requested window")
            normalized = raw[0].astype(np.float32)
            normalized /= factor
            np.clip(normalized, 0, 1, out=normalized)
            if not np.isfinite(normalized).all():
                raise ValueError("Nimbus normalized pixels must be finite float32 values")
            result[np.ix_(row_positions, col_positions)] = normalized[np.ix_(ys - y0, xs - x0)]
    return result


def _binary_points(source: WindowSource, rows: np.ndarray, cols: np.ndarray,
                   read_size: int) -> np.ndarray:
    """Remove four-neighbor inner boundaries using one genuine native pixel of context."""
    result = np.empty((len(rows), len(cols)), dtype=np.float32)
    # Reserve two pixels for the real neighboring labels around every storage window.
    col_groups = list(_groups(cols, read_size - 2))
    for row_positions, ys in _groups(rows, read_size - 2):
        y0, y1 = max(0, int(ys.min()) - 1), min(source.height, int(ys.max()) + 2)
        for col_positions, xs in col_groups:
            x0, x1 = max(0, int(xs.min()) - 1), min(source.width, int(xs.max()) + 2)
            labels = np.asarray(source.read_labels(x0, y0, x1 - x0, y1 - y0))
            if labels.shape != (y1 - y0, x1 - x0) or labels.dtype.kind not in "ui" or \
                    labels.min() < 0 or labels.max() > np.iinfo(np.int32).max:
                raise ValueError("Nimbus native labels must be nonnegative int32-compatible window values")
            local_y, local_x = ys - y0, xs - x0
            center = labels[np.ix_(local_y, local_x)]
            interior = center > 0
            interior &= center == labels[np.ix_(np.maximum(local_y - 1, 0), local_x)]
            interior &= center == labels[np.ix_(np.minimum(local_y + 1, y1 - y0 - 1), local_x)]
            interior &= center == labels[np.ix_(local_y, np.maximum(local_x - 1, 0))]
            interior &= center == labels[np.ix_(local_y, np.minimum(local_x + 1, x1 - x0 - 1))]
            result[np.ix_(row_positions, col_positions)] = interior
    return result


def _linear_axis(indices: np.ndarray, original_length: int, resized_length: int, mode: str):
    # The standard OpenCV kernel rounds the complete coordinate to float32;
    # its default Intel IPP backend retains double precision until subtraction
    # of the integer part. They materially differ on large global coordinates.
    scale = 1.0 / (resized_length / original_length)
    coordinate = (indices.astype(np.float64) + 0.5) * scale - 0.5
    if mode == "opencv4_classic":
        coordinate = coordinate.astype(np.float32)
    lower = np.floor(coordinate).astype(np.int64)
    fraction = (coordinate - lower).astype(np.float32)
    fraction[(lower < 0) | (lower >= original_length - 1)] = 0
    lower = np.clip(lower, 0, original_length - 1)
    return lower, np.minimum(lower + 1, original_length - 1), fraction


def _resized_input(source: WindowSource, channel: str, factor: float, shape: tuple[int, int],
                   rows: np.ndarray, cols: np.ndarray, read_size: int, linear_mode: str) -> np.ndarray:
    h, w = shape
    if (h, w) == (source.height, source.width):
        image = _normalized_points(source, channel, factor, rows, cols, read_size)
    else:
        # OpenCV's IPP linear path falls back to its standard kernel when a
        # source axis has one pixel, even when the global IPP setting is true.
        if min(source.height, source.width) == 1:
            linear_mode = "opencv4_classic"
        y0, y1, fy = _linear_axis(rows, source.height, h, linear_mode)
        x0, x1, fx = _linear_axis(cols, source.width, w, linear_mode)
        unique_y, inverse_y = np.unique(np.concatenate((y0, y1)), return_inverse=True)
        unique_x, inverse_x = np.unique(np.concatenate((x0, x1)), return_inverse=True)
        values = _normalized_points(source, channel, factor, unique_y, unique_x, read_size)
        iy0, iy1 = np.split(inverse_y, 2)
        ix0, ix1 = np.split(inverse_x, 2)
        top = values[np.ix_(iy0, ix0)] * (1 - fx) + values[np.ix_(iy0, ix1)] * fx
        bottom = values[np.ix_(iy1, ix0)] * (1 - fx) + values[np.ix_(iy1, ix1)] * fx
        image = top * (1 - fy[:, None]) + bottom * fy[:, None]
    native_rows = nearest_indices(rows, source.height, h)
    native_cols = nearest_indices(cols, source.width, w)
    binary = _binary_points(source, native_rows, native_cols, read_size)
    return np.stack((image, binary))[None]


def compose_source_tile(source: WindowSource, channel: str, factor: float,
                        shape: Sequence[int], transform: int, tile_bounds: Sequence[int],
                        *, max_read_size: int = 256, linear_mode: str = "opencv4_ipp") -> np.ndarray:
    """Construct one bounded float32 [1, 2, H, W] globally transformed model input.

    The requested rectangle may extend outside the augmented image; only those
    global borders are reflected. Every native read stays inside the source and
    has width and height at most ``max_read_size``, including label context.
    The same function constructs a small reflect-branch input with an inside
    rectangle. No array proportional to complete native-image area is created.
    ``linear_mode`` must match the validated OpenCV backend; default Linux
    OpenCV 4.11 wheels use IPP. Floating arithmetic equality has a tolerance,
    while labels, TTA, reflection and tile coordinates remain exact.
    """
    shape = _shape(shape)
    if shape != scaled_shape(source):
        raise ValueError("Nimbus scaled shape differs from the source magnification")
    transform = _transform(transform)
    if channel not in source.channels:
        raise ValueError("Nimbus marker channel is absent from the native source")
    if linear_mode not in ("opencv4_ipp", "opencv4_classic"):
        raise ValueError("Nimbus linear resize mode must identify the validated OpenCV 4 backend")
    if isinstance(factor, (bool, np.bool_)) or not isfinite(factor) or factor <= 0:
        raise ValueError("Nimbus normalization factor must be finite and positive")
    if isinstance(max_read_size, bool) or not isinstance(max_read_size, int) or not 3 <= max_read_size <= INPUT_SIZE:
        raise ValueError("Nimbus native read size must be an integer from 3 to 1024")
    if len(tile_bounds) != 4 or any(isinstance(v, (bool, np.bool_)) or
                                   not isinstance(v, (int, np.integer)) for v in tile_bounds):
        raise ValueError("Nimbus tile bounds must be integer (x, y, width, height)")
    x, y, width, height = map(int, tile_bounds)
    if not 1 <= width <= INPUT_SIZE or not 1 <= height <= INPUT_SIZE:
        raise ValueError("Nimbus input windows must be at most 1024 pixels per axis")
    transformed_h, transformed_w = transformed_shape(shape, transform)
    rows = _reflect(np.arange(y, y + height, dtype=np.int64), transformed_h)
    cols = _reflect(np.arange(x, x + width, dtype=np.int64), transformed_w)
    h, w = shape
    if transform == 1:
        original_rows, original_cols = cols, w - 1 - rows
    elif transform == 2:
        original_rows, original_cols = h - 1 - rows, w - 1 - cols
    elif transform == 3:
        original_rows, original_cols = h - 1 - cols, rows
    elif transform == 4:
        original_rows, original_cols = h - 1 - rows, cols
    elif transform == 5:
        original_rows, original_cols = rows, w - 1 - cols
    else:
        original_rows, original_cols = rows, cols
    result = _resized_input(source, channel, factor, shape, original_rows, original_cols,
                            max_read_size, linear_mode)
    return result.swapaxes(-2, -1) if transform in (1, 3) else result
