"""Accepted rigid cycle geometry and bounded, single-pass linear sampling.

Transforms map moving to reference in micrometres. Integer image coordinates
are pixel centres, matching PairedGeometryOps and the Elastix intensity views.
Registration views never replace these original quantitative pixel sources.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math
import numpy as np
from .backends import Region

_EPS = 1e-7


@dataclass(frozen=True, slots=True)
class CycleGrid:
    width: int
    height: int
    x_um: float
    y_um: float

    def __post_init__(self):
        if any(type(v) is not int or v < 2 for v in (self.width, self.height)):
            raise ValueError('Cycle registration requires planar images at least two pixels wide and high')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (self.x_um, self.y_um)):
            raise ValueError('Cycle registration requires known positive XY pixel calibration')

    def to_mapping(self): return asdict(self)


@dataclass(frozen=True, slots=True)
class CycleRigidTransform:
    """Row-major 2×3 moving-to-reference physical matrix, with no scale/shear."""
    matrix: tuple[float, ...]

    def __post_init__(self):
        if (type(self.matrix) is not tuple or len(self.matrix) != 6 or
                any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.floating))
                    or not math.isfinite(v) for v in self.matrix)):
            raise ValueError('Cycle transforms require six finite physical matrix values')
        linear = np.asarray(self.matrix, dtype=np.float64).reshape(2, 3)[:, :2]
        if not np.allclose(linear.T @ linear, np.eye(2), rtol=0, atol=1e-6) or not math.isclose(np.linalg.det(linear), 1., abs_tol=1e-6):
            raise ValueError('Cycle registration requires a physical rigid transform without scale, shear or reflection')
        object.__setattr__(self, 'matrix', tuple(float(v) for v in self.matrix))

    @classmethod
    def identity(cls): return cls((1., 0., 0., 0., 1., 0.))

    def inverse_pixels(self, source: CycleGrid, reference: CycleGrid):
        homogeneous = np.eye(3)
        homogeneous[:2] = np.asarray(self.matrix).reshape(2, 3)
        return (np.diag((1/source.x_um, 1/source.y_um, 1.)) @ np.linalg.inv(homogeneous)
                @ np.diag((reference.x_um, reference.y_um, 1.)))[:2]


def _halfplanes(grids, transforms, reference_index):
    reference = grids[reference_index]
    result = []
    for grid, transform in zip(grids, transforms, strict=True):
        inverse = transform.inverse_pixels(grid, reference)
        for row, size in zip(inverse, (grid.width, grid.height), strict=True):
            result.extend(((row[0], row[1], size-1-row[2]), (-row[0], -row[1], row[2])))
    return result


def validate_crop(grids, transforms, reference_index, crop):
    if (type(crop) is not tuple or len(crop) != 4 or any(type(v) is not int for v in crop)
            or min(crop[:2]) < 0 or min(crop[2:]) <= 0):
        raise ValueError('Invalid common cycle crop')
    x, y, width, height = crop
    for a, b, limit in _halfplanes(grids, transforms, reference_index):
        if any(a*xx+b*yy > limit+_EPS for xx in (x, x+width-1) for yy in (y, y+height-1)):
            raise ValueError('The common crop includes pixels without complete interpolation support')
    return crop


def common_crop(grids, transforms, reference_index):
    """Return a conservative integer rectangle within every original footprint.

    Clip convex footprints without allocating an image-sized validity mask, then
    inscribe a rectangle about their common polygon's interior. It need not be
    the maximum-area rectangle; every retained interpolation footprint is valid.
    """
    if len(grids) != len(transforms) or not grids or not 0 <= reference_index < len(grids):
        raise ValueError('Every cycle needs one grid and one common-reference transform')
    reference = grids[reference_index]
    polygon = [(0., 0.), (reference.width-1., 0.),
               (reference.width-1., reference.height-1.), (0., reference.height-1.)]
    planes = _halfplanes(grids, transforms, reference_index)
    for a, b, limit in planes:
        clipped = []
        for first, second in zip(polygon, polygon[1:]+polygon[:1]):
            f = a*first[0]+b*first[1]-limit
            s = a*second[0]+b*second[1]-limit
            if f <= _EPS: clipped.append(first)
            if (f <= _EPS) != (s <= _EPS):
                t = f/(f-s)
                clipped.append((first[0]+t*(second[0]-first[0]), first[1]+t*(second[1]-first[1])))
        polygon = clipped
        if not polygon: raise ValueError('The cycles have no common image overlap')
    points = np.asarray(polygon)
    centre = points.mean(axis=0)
    half = (points.max(axis=0)-points.min(axis=0))/2
    if min(half) <= 0: raise ValueError('The cycles have no usable common image area')
    scale = 1.
    for a, b, limit in planes:
        reach = abs(a)*half[0]+abs(b)*half[1]
        if reach > 0: scale = min(scale, max(0., (limit-a*centre[0]-b*centre[1])/reach))
    low = np.ceil(centre-scale*half-_EPS).astype(np.int64)
    high = np.floor(centre+scale*half+_EPS).astype(np.int64)
    crop = tuple(int(v) for v in (*low, *(high-low+1)))
    if min(crop[2:]) <= 0: raise ValueError('The cycles have no usable common image crop')
    return validate_crop(grids, transforms, reference_index, crop)


def resample_region(read, source, reference, transform, crop, region, *, max_read_pixels=1048576):
    """Read only original rectangles and return float32 samples on the fixed grid.

    Output tiles are at most 256² pixels; source windows are recursively divided
    when calibration or rotation would exceed the read bound. A failed read
    cannot produce a partly successful region or substitute zero-valued signal.
    """
    if not isinstance(region, Region) or region.x+region.width > crop[2] or region.y+region.height > crop[3]:
        raise ValueError('Requested region lies outside the common crop')
    if type(max_read_pixels) is not int or max_read_pixels < 4:
        raise ValueError('The source read bound must allow a linear interpolation footprint')
    inverse = transform.inverse_pixels(source, reference)
    output = np.empty((region.height, region.width), dtype=np.float32)

    def tile(ox, oy, width, height):
        xx = np.arange(width, dtype=np.float64)[None, :]+crop[0]+region.x+ox
        yy = np.arange(height, dtype=np.float64)[:, None]+crop[1]+region.y+oy
        sx = inverse[0, 0]*xx + inverse[0, 1]*yy + inverse[0, 2]
        sy = inverse[1, 0]*xx + inverse[1, 1]*yy + inverse[1, 2]
        sx = np.where(abs(sx-np.rint(sx)) < _EPS, np.rint(sx), sx)
        sy = np.where(abs(sy-np.rint(sy)) < _EPS, np.rint(sy), sy)
        if sx.min() < 0 or sy.min() < 0 or sx.max() > source.width-1 or sy.max() > source.height-1:
            raise ValueError('The common crop includes pixels without complete interpolation support')
        x0, y0 = np.floor(sx).astype(np.int64), np.floor(sy).astype(np.int64)
        x1, y1 = np.ceil(sx).astype(np.int64), np.ceil(sy).astype(np.int64)
        left, top, right, bottom = int(x0.min()), int(y0.min()), int(x1.max()), int(y1.max())
        if (right-left+1)*(bottom-top+1) > max_read_pixels:
            if width >= height and width > 1:
                split = width//2
                tile(ox, oy, split, height); tile(ox+split, oy, width-split, height)
            elif height > 1:
                split = height//2
                tile(ox, oy, width, split); tile(ox, oy+split, width, height-split)
            else: raise ValueError('A source interpolation footprint exceeds the read bound')
            return
        window = Region(left, top, right-left+1, bottom-top+1)
        values = np.asarray(read(window))
        if values.shape != (window.height, window.width):
            raise ValueError('The cycle source returned an unexpected region shape')
        # Promote before arithmetic so signed/unsigned source differences cannot wrap.
        values = values.astype(np.float64, copy=False)
        wx, wy = sx-x0, sy-y0
        lo = values[y0-top, x0-left]*(1-wx)+values[y0-top, x1-left]*wx
        hi = values[y1-top, x0-left]*(1-wx)+values[y1-top, x1-left]*wx
        output[oy:oy+height, ox:ox+width] = lo*(1-wy)+hi*wy

    for oy in range(0, region.height, 256):
        for ox in range(0, region.width, 256):
            tile(ox, oy, min(256, region.width-ox), min(256, region.height-oy))
    return output


@dataclass(frozen=True, slots=True)
class AcceptedCycleRegistration:
    """Reviewed shared-stain alignment; every matrix addresses the same reference."""
    reference_member: int
    registration_channels: tuple[int, ...]
    shared_stain: str
    grids: tuple[CycleGrid, ...]
    transforms: tuple[CycleRigidTransform, ...]
    crop: tuple[int, int, int, int]

    def __post_init__(self):
        count = len(self.grids)
        if (type(self.grids) is not tuple or not 2 <= count <= 256
                or any(type(g) is not CycleGrid for g in self.grids)
                or type(self.transforms) is not tuple or len(self.transforms) != count
                or any(type(t) is not CycleRigidTransform for t in self.transforms)
                or type(self.reference_member) is not int or not 0 <= self.reference_member < count):
            raise ValueError('Each registered cycle needs its original grid and common-reference transform')
        if self.transforms[self.reference_member] != CycleRigidTransform.identity():
            raise ValueError('The fixed reference cycle must retain an identity transform')
        if (type(self.registration_channels) is not tuple or len(self.registration_channels) != count
                or any(type(c) is not int or c < 0 for c in self.registration_channels)):
            raise ValueError('Select the actual corresponding stain channel in every cycle')
        if (type(self.shared_stain) is not str or not self.shared_stain or self.shared_stain != self.shared_stain.strip()
                or len(self.shared_stain) > 200 or not self.shared_stain.isprintable()):
            raise ValueError('Confirm the actual shared stain; a reused dye is not a corresponding marker')
        validate_crop(self.grids, self.transforms, self.reference_member, self.crop)

    def to_mapping(self):
        reference = self.grids[self.reference_member]
        origin = [self.crop[0]*reference.x_um, self.crop[1]*reference.y_um]
        output_transforms = [[a,b,tx-origin[0],c,d,ty-origin[1]]
            for a,b,tx,c,d,ty in (t.matrix for t in self.transforms)]
        return dict(schema_name='mif-accepted-cycle-registration',schema_version=1,
            reference_member=self.reference_member,registration_channels=list(self.registration_channels),
            shared_stain=self.shared_stain,grids=[g.to_mapping() for g in self.grids],
            transforms=[list(t.matrix) for t in self.transforms],crop=list(self.crop),
            coordinate_convention='integer_pixel_centres',transform_direction='moving_to_reference_um',
            interpolation='linear',output_dtype='float32',reviewed=True,
            output_coordinates='crop_local_zero_origin',output_origin_reference_um=origin,
            moving_to_output_um=output_transforms)

    @classmethod
    def from_mapping(cls, value):
        constants=dict(schema_name='mif-accepted-cycle-registration',schema_version=1,
            coordinate_convention='integer_pixel_centres',transform_direction='moving_to_reference_um',
            interpolation='linear',output_dtype='float32',reviewed=True,output_coordinates='crop_local_zero_origin')
        fields={'reference_member','registration_channels','shared_stain','grids','transforms','crop',
                'output_origin_reference_um','moving_to_output_um'}
        if (type(value) is not dict or set(value) != set(constants)|fields
                or any(type(value[k]) is not type(v) or value[k] != v for k,v in constants.items())
                or any(type(value[k]) is not list for k in ('registration_channels','grids','transforms','crop'))):
            raise ValueError('Unsupported accepted cycle registration record')
        if any(type(g) is not dict or set(g) != {'width','height','x_um','y_um'} for g in value['grids']):
            raise ValueError('Invalid original cycle grid')
        if any(type(t) is not list or len(t) != 6 for t in value['transforms']):
            raise ValueError('Invalid physical cycle matrix')
        result = cls(value['reference_member'],tuple(value['registration_channels']),value['shared_stain'],
            tuple(CycleGrid(**g) for g in value['grids']),
            tuple(CycleRigidTransform(tuple(t)) for t in value['transforms']),tuple(value['crop']))
        if result.to_mapping() != value:
            raise ValueError('The output origin or cycle transforms differ from the reviewed common crop')
        return result
