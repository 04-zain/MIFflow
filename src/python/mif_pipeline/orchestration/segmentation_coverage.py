"""Native-pixel staining coverage against saved nuclear or cell outlines."""
from math import ceil, floor
from copy import deepcopy

import numpy as np

from mif_pipeline.datasets.geometry import wkb_polygon
from mif_pipeline.io import Region
from skimage.measure import grid_points_in_poly

_TILE = 256
_MAX_FIELDS = 64
_MAX_PIXELS = 64 * 1024**2
_MAX_VIEW_PIXELS = 1024**2
_OVERLAY_AXIS = 1024


def request_values(request, channel_count):
    if not isinstance(request, dict) or set(request) - {'channel', 'threshold', 'field_index', 'field_size_um', 'view_field_um', 'compartment'}:
        raise ValueError('Choose a nuclear channel, threshold and field size.')
    if not isinstance(request.get('compartment', 'nucleus'), str) or request.get('compartment', 'nucleus') not in {'nucleus', 'cell'}:
        raise ValueError('Choose cell or nuclear outlines for coverage.')
    channel, threshold = request.get('channel'), request.get('threshold')
    if type(channel) is not int or not 0 <= channel < channel_count:
        raise ValueError('Choose an available nuclear staining channel.')
    if isinstance(threshold, bool) or not isinstance(threshold, (float, int)) or not np.isfinite(threshold):
        raise ValueError('Enter a finite threshold in the channel’s stored units.')
    size = request.get('field_size_um', (160., 120.))
    if (not isinstance(size, (tuple, list)) or len(size) != 2 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or v <= 0 for v in size)):
        raise ValueError('Field width and height must be positive finite micrometres.')
    index = request.get('field_index')
    if index is not None and (type(index) is not int or index < 0):
        raise ValueError('Choose an available coverage field.')
    field = request.get('view_field_um')
    if field is not None:
        if (index is not None or not isinstance(field, (tuple, list)) or len(field) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) for v in field) or
                field[2] <= field[0] or field[3] <= field[1]):
            raise ValueError('Select a finite image field with positive width and height.')
    return channel, float(threshold), tuple(map(float, size)), index


def polygons(geometry):
    return [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']


def geometry_bounds(geometry):
    points = np.asarray([point for polygon in polygons(geometry) for ring in polygon for point in ring])
    if not points.size:
        raise ValueError('The accepted segmentation area is empty.')
    return (*points.min(axis=0), *points.max(axis=0))


def paint(geometry, region):
    """Rasterize native pixel centres, including shell and hole boundaries.

    The compiled rasterizer uses labels 1 inside, 2 vertex and 3 edge. Its
    boundary treatment matches the existing reference-sampling mask, while
    avoiding full-tile NumPy temporaries for every accepted-domain vertex.
    """
    shape = (region.height, region.width)
    origin = np.asarray([region.x+.5, region.y+.5])
    result = np.zeros(shape, dtype=bool)
    for polygon in polygons(geometry):
        shell = np.asarray(polygon[0], dtype=float)-origin
        if (shell[:,0].max() < 0 or shell[:,0].min() > region.width-1 or
                shell[:,1].max() < 0 or shell[:,1].min() > region.height-1):
            continue
        included = grid_points_in_poly(shape, shell[:,::-1], binarize=False) > 0
        for ring in polygon[1:]:
            hole = np.asarray(ring, dtype=float)-origin
            if (hole[:,0].max() >= 0 and hole[:,0].min() <= region.width-1 and
                    hole[:,1].max() >= 0 and hole[:,1].min() <= region.height-1):
                included &= grid_points_in_poly(shape, hole[:,::-1], binarize=False) != 1
        result |= included
    return result


def nuclear_padding(cells):
    """Establish a complete nuclear query using the existing cell-bounds index.

    Only four maximum overhangs survive this one-time projected geometry scan.
    Nuclear outlines need not lie inside their associated cell envelope.
    """
    padding = np.zeros(4)
    for batch in cells.iter_batches(columns=('nucleus_geometry', 'min_x', 'min_y', 'max_x', 'max_y')):
        for row in batch.to_pylist():
            if row['nucleus_geometry'] is None:
                continue
            x0, y0, x1, y1 = geometry_bounds(wkb_polygon(row['nucleus_geometry']))
            padding = np.maximum(padding, (row['min_x']-x0, row['min_y']-y0, x1-row['max_x'], y1-row['max_y']))
    return tuple(map(float, padding))


class NuclearCoverage:
    """Keep one scalar request and one selected overlay in the existing reader."""
    def __init__(self, image, cells, geometry, pixel_size, extent, *, compartment='nucleus'):
        if not isinstance(compartment, str) or compartment not in {'nucleus', 'cell'}:
            raise ValueError('Choose cell or nuclear outlines for coverage.')
        self.compartment = compartment
        self._geometry_column = 'nucleus_geometry' if compartment == 'nucleus' else 'cell_geometry'
        self.image, self.cells, self.geometry = image, cells, geometry
        self.pixel_size, self.extent = pixel_size, extent
        self._padding = None if compartment == 'nucleus' else (0., 0., 0., 0.)
        self._key = self._summary = self._overlay_key = self._overlay = None
        self._view_key = self._view_result = None

    def _fields(self, size):
        from .segmentation_spatial_review import spatial_fields
        candidates = spatial_fields(self.geometry, self.pixel_size, self.extent, field_size_um=size)
        indices = np.linspace(0, len(candidates)-1, min(_MAX_FIELDS, len(candidates)), dtype=int)
        fields = [candidates[i] for i in indices]
        if not fields:
            raise ValueError('There is no accepted segmentation area to inspect.')
        width, height = self.image.levels[0].shape[2], self.image.levels[0].shape[1]
        px, py = self.pixel_size
        bounds = [(max(0, floor(f[0]/px)), max(0, floor(f[1]/py)),
                   min(width, ceil(f[2]/px)), min(height, ceil(f[3]/py))) for f in fields]
        if sum((b[2]-b[0])*(b[3]-b[1]) for b in bounds) > _MAX_PIXELS:
            raise ValueError('These coverage fields exceed the bounded pixel allowance. Choose a smaller field size.')
        return fields, bounds, len(candidates)

    def _tiles(self, bounds, channel, threshold):
        left, top, right, bottom = bounds
        # A rightward nuclear overhang can enter this tile from a cell to its left.
        pl, pt, pr, pb = self._padding
        for y in range(top, bottom, _TILE):
            for x in range(left, right, _TILE):
                region = Region(x=x, y=y, width=min(_TILE, right-x), height=min(_TILE, bottom-y))
                signal = self.image.read_window(0, x=x, y=y, width=region.width, height=region.height,
                    channels=(channel,), max_output_bytes=_TILE*_TILE*self.image.levels[0].dtype.itemsize)[0]
                domain = paint(self.geometry, region)
                finite = domain & np.isfinite(signal)
                above = finite & (signal > threshold)
                covered = np.zeros(signal.shape, dtype=bool)
                query = (x-pr, y-pb, x+region.width+pl, y+region.height+pt)
                for batch in self.cells.query(query, columns=(self._geometry_column,), predicate='bounds'):
                    for value in batch.column(self._geometry_column).to_pylist():
                        if value is None:
                            continue
                        geometry = wkb_polygon(value)
                        gx0, gy0, gx1, gy1 = geometry_bounds(geometry)
                        # Rasterize a polygon only over its local part of the bounded tile.
                        x0, y0 = max(x, floor(gx0)), max(y, floor(gy0))
                        x1, y1 = min(x+region.width, ceil(gx1)), min(y+region.height, ceil(gy1))
                        if x1 > x0 and y1 > y0:
                            covered[y0-y:y1-y, x0-x:x1-x] |= paint(geometry, Region(x=x0,y=y0,width=x1-x0,height=y1-y0))
                yield region, domain, finite, above, above & ~covered

    def read(self, request):
        channel, threshold, size, index = request_values(request, len(self.image.channels))
        if request.get('compartment', 'nucleus') != self.compartment:
            raise ValueError('Choose the requested outline compartment again.')
        key = (channel, threshold, size)
        summary = self._summary if key == self._key else dict(
                channel=channel, channel_name=self.image.channels[channel]['label'], threshold=threshold,
                compartment=self.compartment,
                field_size_um=list(size), fields=[], candidate_field_count=0,
                sampling='spatial fields independent of staining and detected cells',
                comparison='finite native pixels strictly greater than threshold',
                pixel_size_um=list(self.pixel_size), nuclear_query_padding_level0=list(self._padding or ()))
        # A visible-field calculation does not trigger the optional tissue survey.
        if request.get('view_field_um') is not None:
            if key != self._key:
                self._summary = summary
                self._key, self._overlay_key, self._overlay = key, None, None
                self._view_key = self._view_result = None
            return self._read_view(request['view_field_um'], channel, threshold)
        if not summary['fields']:
            fields, bounds, candidate_count = self._fields(size)
            if self._padding is None:
                self._padding = nuclear_padding(self.cells)
            records = []
            pixel_area = self.pixel_size[0]*self.pixel_size[1]
            for i, (field, box) in enumerate(zip(fields, bounds, strict=True)):
                counts = np.zeros(4, dtype=np.int64)
                for _region, *masks in self._tiles(box, channel, threshold):
                    counts += [np.count_nonzero(mask) for mask in masks]
                domain, finite, above, uncovered = map(int, counts)
                records.append(dict(field_index=i, requested_field_um=list(field), pixel_bounds=list(box),
                    field_um=[box[0]*self.pixel_size[0],box[1]*self.pixel_size[1],box[2]*self.pixel_size[0],box[3]*self.pixel_size[1]],
                    domain_pixels=domain, finite_pixels=finite, above_pixels=above, uncovered_pixels=uncovered,
                    domain_area_um2=domain*pixel_area, finite_area_um2=finite*pixel_area,
                    above_area_um2=above*pixel_area, uncovered_area_um2=uncovered*pixel_area,
                    uncovered_fraction=uncovered/above if above else None))
            # Publish only after every field has completed successfully.
            self._summary = {**summary, 'fields': records, 'candidate_field_count': candidate_count,
                'nuclear_query_padding_level0': list(self._padding)}
            self._key, self._overlay_key, self._overlay = key, None, None
            self._view_key = self._view_result = None
        if index is not None and index >= len(self._summary['fields']):
            raise ValueError('Choose an available coverage field.')
        if index is not None and self._overlay_key != (key, index):
            record = self._summary['fields'][index]
            self._overlay = self._read_overlay(record['pixel_bounds'], channel, threshold)
            self._overlay_key = (key, index)
        # A plot may sort its rows; its display copy cannot reorder cached fields.
        return {**deepcopy(self._summary), 'selected_field_index': index}, self._overlay if index is not None else None

    def _read_overlay(self, bounds, channel, threshold):
        """Sample contributing native pixels into a bounded display array."""
        left, top, right, bottom = bounds
        scale = min(1., _OVERLAY_AXIS/max(right-left, bottom-top))
        width, height = max(1, round((right-left)*scale)), max(1, round((bottom-top)*scale))
        xs = left+np.floor((np.arange(width)+.5)*(right-left)/width).astype(int)
        ys = top+np.floor((np.arange(height)+.5)*(bottom-top)/height).astype(int)
        overlay = np.zeros((len(ys), len(xs)), dtype=np.uint8)
        for region, _domain, _finite, above, uncovered in self._tiles(bounds, channel, threshold):
            xi = np.flatnonzero((xs >= region.x) & (xs < region.x+region.width))
            yi = np.flatnonzero((ys >= region.y) & (ys < region.y+region.height))
            if len(xi) and len(yi):
                positions = np.ix_(ys[yi]-region.y, xs[xi]-region.x)
                overlay[np.ix_(yi, xi)] = above[positions].astype(np.uint8)+uncovered[positions].astype(np.uint8)
        overlay.setflags(write=False)
        return overlay, np.asarray([left*self.pixel_size[0],right*self.pixel_size[0],bottom*self.pixel_size[1],top*self.pixel_size[1]])

    def _read_view(self, field, channel, threshold):
        """Measure one panned field, retaining the fixed overview and latest view only.

        Bounds are clipped to the image before enforcing the native-pixel limit.
        Overview zoom returns a zoom-in status without reading image pixels.
        Counts use the same accepted-domain and nuclear masks as sampled fields.
        """
        width, height = self.image.levels[0].shape[2], self.image.levels[0].shape[1]
        px, py = self.pixel_size
        bounds = (max(0, floor(field[0]/px)), max(0, floor(field[1]/py)),
                  min(width, ceil(field[2]/px)), min(height, ceil(field[3]/py)))
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            raise ValueError('Move the view inside the image.')
        result = {**deepcopy(self._summary), 'selected_field_index': None}
        if (right-left)*(bottom-top) > _MAX_VIEW_PIXELS:
            return {**result, 'current_field': None, 'current_view_status': 'zoom_in'}, None
        if self._padding is None:
            self._padding = nuclear_padding(self.cells)
        self._summary['nuclear_query_padding_level0'] = result['nuclear_query_padding_level0'] = list(self._padding)
        key = (self._key, bounds)
        if key != self._view_key:
            counts = np.zeros(4, dtype=np.int64)
            for _region, *masks in self._tiles(bounds, channel, threshold):
                counts += [np.count_nonzero(mask) for mask in masks]
            domain, finite, above, uncovered = map(int, counts)
            pixel_area = px*py
            record = dict(pixel_bounds=list(bounds), field_um=[left*px, top*py, right*px, bottom*py],
                domain_pixels=domain, finite_pixels=finite, above_pixels=above, uncovered_pixels=uncovered,
                domain_area_um2=domain*pixel_area, finite_area_um2=finite*pixel_area,
                above_area_um2=above*pixel_area, uncovered_area_um2=uncovered*pixel_area,
                uncovered_fraction=uncovered/above if above else None)
            overlay = self._read_overlay(bounds, channel, threshold)
            self._view_key, self._view_result = key, (record, overlay)
        record, overlay = self._view_result
        return {**result, 'current_field': deepcopy(record), 'current_view_status': 'ready'}, overlay
