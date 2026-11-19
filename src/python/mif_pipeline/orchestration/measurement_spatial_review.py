"""A retained bounded image reader for measurement-to-tissue inspection."""
from __future__ import annotations

from contextlib import contextmanager
import numpy as np

from mif_pipeline.cell_analysis.measurement_spatial import (
    count_measurement_grid, count_measurement_field, measurement_columns,
)
from .saved_cell_figures import _selected_cells, _measurements, cell_frame
from .saved_cell_gallery import image_authority, channel_color


class _ImageMeasurementCells:
    """Project one image's sections while retaining full outline identities."""
    identity_columns = ('sample_id', 'project_entry_id', 'slide_id', 'section_id')

    def __init__(self, sections):
        import pyarrow as pa
        self.sections = sections
        self.boundary_sources = {identity: dataset.scope.get('cell_boundary_source') for identity, dataset in sections}
        self.scope = dict(sections[0][1].scope)
        if len({dataset.scope.get('cell_boundary_source') for _identity, dataset in sections}) > 1:
            self.scope['cell_boundary_source'] = 'mixed'
        self.schema = pa.schema([*(f for f in sections[0][1].schema if f.name not in self.identity_columns),
                                 *(pa.field(name, pa.string()) for name in self.identity_columns)])
        for _identity,dataset in sections:
            if any(dataset.scope[key] != self.scope[key] for key in ('x_um_per_px','y_um_per_px')):
                raise ValueError('The selected sections disagree on their image calibration.')

    def _batches(self, method, columns, *args, **kwargs):
        import pyarrow as pa
        fields = tuple(name for name in columns if name not in self.identity_columns)
        for identity,dataset in self.sections:
            for batch in getattr(dataset, method)(*args, columns=fields, **kwargs):
                for name,value in zip(self.identity_columns, identity, strict=True):
                    if name in columns:
                        batch = batch.append_column(name, pa.array([value]*batch.num_rows, type=pa.string()))
                yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


class MeasurementFieldReader:
    """Keep one image cache and one selected-range grid, never a history of pans."""
    def __init__(self, authority, snapshot, *, state_store=None):
        from mif_pipeline.io.processed_image import ProcessedImage
        self._state_store = state_store
        self._segmentation_cells = None
        if authority.artifact_ref.artifact_kind == 'segmentation_result':
            from .saved_segmentation_cells import segmentation_cells
            self._segmentation_cells = segmentation_cells(authority, state_store, snapshot=snapshot)
        self.features = {item.measurement: item for item in _measurements(authority, snapshot,
            dataset=self._segmentation_cells)}
        frame = cell_frame(authority)
        source = image_authority(authority, snapshot, frame)
        if source is None:
            raise ValueError('The image associated with these measurements is unavailable.')
        self.image = ProcessedImage(source.locator, cache_bytes=32 * 1024**2)
        base = self.image.levels[0]
        self.pixel_size = (base.x_um_per_px, base.y_um_per_px)
        with self._selected_cells(authority) as cells:
            scope = (cells.description['scope'] if self._segmentation_cells is not None else
                     cells.scope if authority.artifact_ref.artifact_kind == 'measurements_result' else None)
            calibration = (scope['x_um_per_px'], scope['y_um_per_px']) if scope is not None else (frame.x_um_per_px, frame.y_um_per_px)
            self._cell_boundary_source = scope.get('cell_boundary_source') if scope is not None else None
        if not np.allclose(self.pixel_size, calibration, rtol=1e-10, atol=0):
            self.image.close()
            raise ValueError('The image pixel size differs from the saved cell coordinates.')
        self.extent = (0., base.shape[2]*base.x_um_per_px, base.shape[1]*base.y_um_per_px, 0.)
        self.frame = dict(image_id=frame.canonical_image_id, image_sha256=frame.canonical_image_sha256,
                          level0_pixel_size_x_um=self.pixel_size[0], level0_pixel_size_y_um=self.pixel_size[1])
        self._grid_key = self._grid = None
        self._region_bounds_key = self._region_bounds = None
        self._limits = {}

    def close(self):
        self.image.close()
        self._grid = None
        if self._segmentation_cells is not None and hasattr(self._segmentation_cells, '_edit_index'):
            self._segmentation_cells._edit_index.close()
        self._segmentation_cells = None

    @contextmanager
    def _selected_cells(self, authority):
        if self._segmentation_cells is not None:
            yield self._segmentation_cells
        else:
            with _selected_cells(authority) as cells:
                yield cells

    def read(self, authority, snapshot, options):
        phenotype = options.get('phenotype')
        if phenotype is not None and authority.artifact_ref.artifact_kind != 'spatial_cell_handoff':
            raise ValueError('Phenotype selection requires the accepted cell results.')
        name = options.get('measurement')
        if name not in self.features:
            raise ValueError('Select an available saved measurement.')
        lower, upper = float(options['lower']), float(options['upper'])
        if not np.isfinite([lower, upper]).all() or lower > upper:
            raise ValueError('Select a finite measurement range with its lower limit first.')
        channels = tuple(int(c) for c in options.get('channels', ()))
        if (not channels or len(set(channels)) != len(channels)
                or min(channels) < 0 or max(channels) >= len(self.image.channels)):
            raise ValueError('Select distinct available image channels.')
        field = options.get('field_um')
        if field is None:
            field = (self.extent[0], self.extent[3], self.extent[1], self.extent[2])
        if len(field) != 4 or not np.isfinite(field).all():
            raise ValueError('Select a finite image field.')
        left, top, right, bottom = map(float, field)
        left, top = max(0., left), max(0., top)
        right, bottom = min(self.extent[1], right), min(self.extent[2], bottom)
        if right <= left or bottom <= top:
            raise ValueError('Move the view inside the image.')
        viewport = options.get('viewport_size', (900, 600))
        if len(viewport) != 2 or any(type(v) is not int or not 1 <= v <= 1024 for v in viewport):
            raise ValueError('The image viewport must be between one and 1024 pixels on each axis.')
        extent = (left, right, bottom, top)
        bounds = (left/self.pixel_size[0], top/self.pixel_size[1],
                  right/self.pixel_size[0], bottom/self.pixel_size[1])
        from .measurement_region_review import filtered_cells, region_key, region_cell_bounds, accepted_area_bounds
        from .measurement_phenotype_review import filter_phenotype
        with self._selected_cells(authority) as dataset:
            dataset = filtered_cells(dataset, options.get('region'))
            dataset = filter_phenotype(dataset, phenotype)
            key = (name, lower, upper, phenotype, region_key(options.get('region')))
            if key != self._grid_key:
                self._grid = count_measurement_grid(dataset, name, lower, upper, self.extent, self.pixel_size)
                self._grid_key = key
                if options.get('region') is not None and self._region_bounds_key != key[-1]:
                    selection = options['region']
                    authorities = [row[0] for row in getattr(self, 'members', ())] or [authority]
                    self._region_bounds = (accepted_area_bounds(authorities, snapshot, selection, self._state_store)
                        if selection['kind'] == 'analysis_area' else region_cell_bounds(dataset, self.pixel_size))
                    self._region_bounds_key = key[-1]
            grid = self._grid
            counts = (dict(field_selected=int(grid['grid_selected'].sum()),
                           field_measured=int(grid['grid_measured'].sum())) if extent == self.extent
                      else count_measurement_field(dataset, name, lower, upper, bounds))
            outlines = self._outlines(dataset, name, lower, upper, bounds) if counts['field_measured'] <= 2000 else self._empty_outlines()
        level, pixels, actual_extent, sampling = self._pixels(extent, viewport, channels)
        colors, limits = {}, {}
        for index, channel in enumerate(channels):
            identity = str(channel)
            if identity not in self._limits:
                values = pixels[index][np.isfinite(pixels[index])]
                low, high = np.percentile(values, [1, 99]) if values.size else (0., 1.)
                self._limits[identity] = (float(low), float(high if high > low else low + max(1., abs(low)*.01)))
            low, high = map(float, options.get('limits', {}).get(identity, self._limits[identity]))
            if not np.isfinite([low, high]).all() or high <= low:
                raise ValueError('Each display maximum must be greater than its minimum.')
            limits[identity] = [low, high]
            supplied = options.get('channel_colors', {}).get(identity)
            colors[identity] = channel_color({'color': supplied.lstrip('#')} if supplied else self.image.channels[channel], channel)
        display = {**options, 'image_uri':self.image.path.as_uri(), 'channels': list(channels), 'channel_colors': colors, 'limits': limits}
        size = self.image.levels[level]
        metadata = {'image_uri':self.image.path.as_uri(),
                    'cell_boundary_source': self._cell_boundary_source,
                    'region_bounds_um': self._region_bounds if options.get('region') is not None else None,
                    'region_image_identity': [authority.artifact_ref.owning_identity.get(key)
                        for key in ('sample_id','project_entry_id','slide_id')],
                    **counts, **{key: value for key, value in grid.items() if not isinstance(value, np.ndarray)},
                    'measurement': name, 'lower': lower, 'upper': upper,
                    **({'phenotype': phenotype} if phenotype is not None else {}),
                    'source_frame': self.frame, 'pyramid_level': level,
                    'pixel_size_um': (size.x_um_per_px, size.y_um_per_px),
                    **sampling,
                    'field_um': (left, top, right, bottom), 'display_options': display,
                    'visible_cells': outlines.pop('visible_cells'),
                    'outline_detail_available': outlines.pop('outline_detail_available')}
        output = dict(pixels=pixels, channel_indices=np.asarray(channels),
                      channel_names=np.asarray([self.image.channels[c]['label'] for c in channels]),
                      extent_um=np.asarray(actual_extent), grid_extent_um=np.asarray(self.extent),
                      grid_selected=grid['grid_selected'], grid_measured=grid['grid_measured'],
                      metadata=metadata, **outlines)
        for value in output.values():
            if isinstance(value, np.ndarray):
                value.setflags(write=False)
        return output

    def _pixels(self, extent, viewport, channels):
        left, right, bottom, top = extent
        target = ((right-left)/viewport[0], (bottom-top)/viewport[1])
        # Choose the finest useful source level. Never interpolate cell detail.
        level = 0
        for index, size in enumerate(self.image.levels):
            if size.x_um_per_px <= target[0] and size.y_um_per_px <= target[1]:
                level = index
        for level in range(level, len(self.image.levels)):
            size = self.image.levels[level]
            x, y = int(np.floor(left/size.x_um_per_px)), int(np.floor(top/size.y_um_per_px))
            end_x = min(size.shape[2], int(np.ceil(right/size.x_um_per_px)))
            end_y = min(size.shape[1], int(np.ceil(bottom/size.y_um_per_px)))
            width, height = max(1, end_x-x), max(1, end_y-y)
            if max(width, height) <= 2048 and width*height*len(channels)*size.dtype.itemsize <= 48*1024**2:
                break
        else:
            budget=48*1024**2
            scale = max(width/viewport[0],height/viewport[1],1.,
                        np.sqrt(width*height*len(channels)*size.dtype.itemsize/budget))
            output_width, output_height = max(1,int(np.ceil(width/scale))), max(1,int(np.ceil(height/scale)))
            while output_width*output_height*len(channels)*size.dtype.itemsize>budget:
                scale*=1.01
                output_width,output_height=max(1,int(np.ceil(width/scale))),max(1,int(np.ceil(height/scale)))
            pixels = self.image.read_display_window(level,x=x,y=y,width=width,height=height,
                output_width=output_width,output_height=output_height,channels=channels,max_output_bytes=48*1024**2)
            return level, pixels, (x*size.x_um_per_px,end_x*size.x_um_per_px,end_y*size.y_um_per_px,y*size.y_um_per_px), {
                'display_sampling':'nearest', 'source_window_pixels':(x,y,width,height),
                'source_pixel_size_um':(size.x_um_per_px,size.y_um_per_px),
                'pixel_size_um':(width*size.x_um_per_px/output_width,height*size.y_um_per_px/output_height)}
        pixels = self.image.read_window(level, x=x, y=y, width=width, height=height,
                                       channels=channels, max_output_bytes=48*1024**2)
        return level, pixels, (x*size.x_um_per_px, end_x*size.x_um_per_px,
                               end_y*size.y_um_per_px, y*size.y_um_per_px), {}

    @staticmethod
    def _empty_outlines():
        return dict(visible_cells=[], outline_detail_available=False,
                    outline_vertices_um=np.empty((0, 2)), outline_offsets=np.asarray([0], dtype=np.int64),
                    outline_cell_indices=np.empty(0, dtype=np.int64), outline_kinds=np.asarray([], dtype=str))

    def _outlines(self, dataset, name, lower, upper, bounds):
        from mif_pipeline.datasets.geometry import wkb_polygon
        column, presence, fields = measurement_columns(dataset, name) if name is not None else (None, None, ())
        identity_columns = getattr(dataset, 'identity_columns', ())
        columns = ('cell_id', 'x_level0', 'y_level0', 'cell_geometry', 'nucleus_geometry', *fields, *identity_columns)
        rows = []
        for batch in dataset.query(bounds, columns=columns, predicate='intersects'):
            if len(rows)+batch.num_rows > 2000:
                return self._empty_outlines()
            rows.extend(batch.to_pylist())
        cells, vertices, offsets, indices, kinds = [], [], [0], [], []
        for index, row in enumerate(rows):
            boundary = (dataset.boundary_sources[tuple(row[key] for key in identity_columns)]
                if isinstance(dataset, _ImageMeasurementCells) else self._cell_boundary_source)
            value = row[column] if column is not None else None
            valid = value is not None and np.isfinite(value) and row.get(presence, True)
            scalar = dict(value=float(value) if valid else None, selected=bool(valid and lower <= value <= upper)) if column is not None else {}
            cells.append(dict(cell_id=row['cell_id'], **scalar,
                              x_um=row['x_level0']*self.pixel_size[0], y_um=row['y_level0']*self.pixel_size[1],
                              **{key: row[key] for key in identity_columns}))
            for field, kind in (('cell_geometry', 'cell'), ('nucleus_geometry', 'nucleus')):
                if kind == 'cell' and boundary == 'nuclear_outline':
                    continue
                shape = wkb_polygon(row[field]) if row[field] is not None else None
                polygons = ([] if shape is None else [shape['coordinates']] if shape['type']=='Polygon' else shape['coordinates'])
                for polygon in polygons:
                    for ring in polygon:
                        points = np.asarray(ring, dtype=float)*self.pixel_size
                        if offsets[-1]+len(points) > 300_000:
                            return self._empty_outlines()
                        vertices.extend(points); offsets.append(offsets[-1]+len(points))
                        indices.append(index); kinds.append(kind)
        return dict(visible_cells=cells, outline_detail_available=True,
                    outline_vertices_um=np.asarray(vertices).reshape(-1, 2), outline_offsets=np.asarray(offsets),
                    outline_cell_indices=np.asarray(indices, dtype=np.int64), outline_kinds=np.asarray(kinds))


class MeasurementImageFieldReader(MeasurementFieldReader):
    """Reuse the bounded field renderer for compatible sections of one exact image."""
    def __init__(self, members, snapshot, *, state_store=None):
        self.members = members
        first = members[0][1]
        identity = (first.sample_id, first.project_entry_id, first.slide_id,
                    first.canonical_image_id, first.canonical_image_sha256, first.canonical_channels)
        if any((measurement.sample_id, measurement.project_entry_id, measurement.slide_id,
                measurement.canonical_image_id, measurement.canonical_image_sha256,
                measurement.canonical_channels) != identity for _source,measurement,_feature in members):
            raise ValueError('The selected measurement sections do not share one exact image frame.')
        super().__init__(members[0][0], snapshot, state_store=state_store)

    @contextmanager
    def _selected_cells(self, authority):
        from contextlib import ExitStack
        with ExitStack() as stack:
            sections = []
            for source,measurement,_feature in self.members:
                dataset = stack.enter_context(_selected_cells(source))
                identity = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id, measurement.section_id)
                sections.append((identity, dataset))
            yield _ImageMeasurementCells(sections)
