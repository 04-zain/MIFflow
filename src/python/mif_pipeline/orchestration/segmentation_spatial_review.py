"""Plain staining and saved outlines from the accepted segmentation, before measurements."""
import numpy as np

from .measurement_spatial_review import MeasurementFieldReader
from .saved_cell_gallery import channel_color


def spatial_fields(geometry, pixel_size, extent, *, field_size_um=(160., 120.)):
    """Spread fixed physical fields across accepted tissue, independently of cells."""
    from mif_pipeline.pipeline_core.polygon_validation import _point_in_polygon
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    width, height = min(field_size_um[0], extent[1]), min(field_size_um[1], extent[2])
    positions = {}
    for polygon in polygons:
        shell = np.asarray(polygon[0], dtype=float) * pixel_size
        low, high = shell.min(axis=0), shell.max(axis=0)
        centres = [(x,y) for y in np.linspace(low[1],high[1],5)[1:-1]
                   for x in np.linspace(low[0],high[0],5)[1:-1]
                   if _point_in_polygon((x/pixel_size[0],y/pixel_size[1]),polygon)>=0]
        if not centres:
            centres = [shell[0]]
        for x,y in centres:
            x,y = np.clip(x,width/2,extent[1]-width/2),np.clip(y,height/2,extent[2]-height/2)
            field = [float(x-width/2),float(y-height/2),float(x+width/2),float(y+height/2)]
            positions.setdefault(tuple(field),field)
    fields = list(positions.values())
    return fields[::2] + fields[1::2]



class SegmentationFieldReader(MeasurementFieldReader):
    """Reuse bounded pixels and geometry; no scalar distribution or count grid."""
    def __init__(self, authority, snapshot, *, state_store=None, coverage_scope_loader=None):
        self._missing_nuclei = None
        self._coverage = None
        self._coverage_scope_loader = coverage_scope_loader
        self._coverage_excluded = bool(authority.artifact_ref.owning_identity.get('segmentation_skipped_excluded_domains'))
        super().__init__(authority, snapshot, state_store=state_store)
        try:
            from .saved_segmentation_cells import segmentation_tissue_geometry
            from ome_types import from_xml
            from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
            self.tissue_geometry = segmentation_tissue_geometry(authority, snapshot, state_store)
            self.spatial_fields = spatial_fields(self.tissue_geometry, self.pixel_size, self.extent)
            inventory = resolve_component_identity_inventory(
                from_xml(self.image.attributes['mif_pipeline']['ome_xml']), 0)
            self.nuclear_channel = inventory.nuclear_component_index if inventory is not None else None
            self._overview = None
        except Exception:
            self.close()
            raise

    def close(self):
        if self._missing_nuclei is not None:
            self._missing_nuclei[0].cleanup()
            self._missing_nuclei = None
        self._coverage = None
        super().close()

    def _missing_nucleus_location(self, index):
        """Index confirmed null nuclei once; retain locations on disk, not all shapes."""
        import sqlite3
        import tempfile
        import pyarrow.compute as pc
        from contextlib import closing
        from pathlib import Path
        if type(index) is not int or index < 0:
            raise ValueError('Choose an available cell without a nuclear outline.')
        if self._missing_nuclei is None:
            directory = tempfile.TemporaryDirectory(prefix='mif-nuclear-outline-review-')
            path = Path(directory.name) / 'locations.sqlite'
            try:
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute('PRAGMA cache_size=-8192')
                    connection.execute('CREATE TABLE locations(cell_id TEXT,x REAL,y REAL,min_x REAL,min_y REAL,max_x REAL,max_y REAL)')
                    presence = 'present:measurement:Nucleus: Area µm^2'
                    cells = self._segmentation_cells
                    for batch in cells.iter_batches(columns=('cell_id', presence)):
                        ids = batch.filter(pc.invert(batch.column(presence).fill_null(False))).column('cell_id').to_pylist()
                        if not ids:
                            continue
                        for found in cells.lookup_ids(ids, columns=('cell_id', 'nucleus_geometry',
                                'x_level0', 'y_level0', 'min_x', 'min_y', 'max_x', 'max_y')):
                            connection.executemany('INSERT INTO locations VALUES(?,?,?,?,?,?,?)',
                                [(row['cell_id'], row['x_level0'], row['y_level0'], row['min_x'],
                                  row['min_y'], row['max_x'], row['max_y'])
                                 for row in found.to_pylist() if row['nucleus_geometry'] is None])
                    count = connection.execute('SELECT COUNT(*) FROM locations').fetchone()[0]
                    connection.commit()
                self._missing_nuclei = (directory, path, count)
            except BaseException:
                directory.cleanup()
                raise
        _, path, count = self._missing_nuclei
        if not count and index == 0:
            return 0, None, None
        if index >= count:
            raise ValueError('Choose an available cell without a nuclear outline.')
        with closing(sqlite3.connect(path)) as connection:
            identity, x, y, left, top, right, bottom = connection.execute(
                'SELECT * FROM locations WHERE rowid=?', (index + 1,)).fetchone()
        px, py = self.pixel_size
        width, height = min(self.extent[1], max(40., 2*(right-left)*px)), min(self.extent[2], max(30., 2*(bottom-top)*py))
        cx = float(np.clip((left+right)*px/2, width/2, self.extent[1]-width/2))
        cy = float(np.clip((top+bottom)*py/2, height/2, self.extent[2]-height/2))
        return count, dict(cell_id=identity, x_um=x*px, y_um=y*py), [cx-width/2, cy-height/2, cx+width/2, cy+height/2]

    def read(self, authority, snapshot, options):
        # The retained native InstanSeg adapter supplies every canonical channel;
        # it does not configure an input-channel subset. Saved visibility can
        # select which of those inputs to display without changing this review set.
        channels = tuple(options.get('channels') or ([self.nuclear_channel] if self.nuclear_channel is not None else [0]))
        if (not channels or any(type(c) is not int or not 0 <= c < len(self.image.channels) for c in channels)
                or len(set(channels)) != len(channels)):
            raise ValueError('Select available image channels.')
        scope = self._segmentation_cells.description['scope']
        identity = {key: scope[key] for key in ('sample_id', 'project_entry_id', 'slide_id', 'section_id')}
        coverage_reason = ('This segmentation supplies cell outlines without nuclear outlines.'
            if scope.get('nucleus_boundary_source') == 'not_segmented' or scope.get('segmentation_method') == 'cellpose'
            else 'No selected area was segmented.' if self._coverage_excluded else None)
        cell_coverage_reason = ('This segmentation supplies nuclear outlines only.'
            if scope.get('cell_boundary_source') == 'nuclear_outline'
            else 'No selected area was segmented.' if self._coverage_excluded else None)
        coverage = overlay = None
        coverage_request = options.get('nuclear_coverage')
        if coverage_request is not None:
            from .segmentation_coverage import NuclearCoverage, request_values
            request_values(coverage_request, len(self.image.channels))
            compartment = coverage_request.get('compartment', 'nucleus')
            reason = coverage_reason if compartment == 'nucleus' else cell_coverage_reason
            if reason is not None:
                raise ValueError(reason)
            if self._coverage is None or self._coverage.compartment != compartment:
                if self._coverage_scope_loader is None:
                    raise ValueError('The accepted segmentation area is unavailable for coverage inspection.')
                geometry = self._coverage.geometry if self._coverage is not None else self._coverage_scope_loader()
                self._coverage = NuclearCoverage(self.image, self._segmentation_cells,
                    geometry, self.pixel_size, self.extent, compartment=compartment)
            coverage, overlay = self._coverage.read(coverage_request)
            coverage = {**coverage, 'source_frame': self.frame, **identity,
                        'segmentation_population_id': scope['population_id']}
        navigation_available = scope.get('segmentation_method', 'instanseg') == 'instanseg'
        mode = options.get('inspection_mode', 'across_tissue')
        if mode not in {'across_tissue', 'without_nuclear_outline'} or (mode != 'across_tissue' and not navigation_available):
            raise ValueError('Choose an inspection mode available for this segmentation method.')
        selected, focus, count = None, None, None
        if mode == 'without_nuclear_outline':
            count, selected, focus = self._missing_nucleus_location(options.get('inspection_index', 0))
            if selected is not None:
                selected = {**selected, **identity}
        field = options.get('field_um', focus or self.spatial_fields[0])
        if coverage is not None and coverage['selected_field_index'] is not None:
            field = coverage['fields'][coverage['selected_field_index']]['field_um']
        if len(field) != 4 or not np.isfinite(field).all():
            raise ValueError('Select a finite image field.')
        left, top, right, bottom = map(float, field)
        left, top, right, bottom = max(0., left), max(0., top), min(self.extent[1], right), min(self.extent[2], bottom)
        if right <= left or bottom <= top:
            raise ValueError('Move the view inside the image.')
        viewport = options.get('viewport_size', [900, 600])
        if len(viewport) != 2 or any(type(v) is not int or not 1 <= v <= 1024 for v in viewport):
            raise ValueError('The image viewport must be between one and 1024 pixels on each axis.')
        browse_index = options.get('browse_field_index')
        if browse_index is not None and coverage is None and mode == 'across_tissue':
            if type(browse_index) is not int or not 0 <= browse_index < len(self.spatial_fields):
                raise ValueError('Choose an available tissue field.')
            left, top, right, bottom = self.spatial_fields[browse_index]
            # Keep the entire preset field and load the adjoining tissue visible at this zoom.
            aspect = viewport[0] / viewport[1]
            width = min(self.extent[1], max(right - left, (bottom - top) * aspect))
            height = min(self.extent[2], max(bottom - top, (right - left) / aspect))
            left = max(0., min((left + right - width) / 2, self.extent[1] - width))
            top = max(0., min((top + bottom - height) / 2, self.extent[2] - height))
            right, bottom = left + width, top + height
        level, pixels, extent, sampling = self._pixels((left, right, bottom, top), viewport, channels)
        bounds = (extent[0]/self.pixel_size[0], extent[3]/self.pixel_size[1], extent[1]/self.pixel_size[0], extent[2]/self.pixel_size[1])
        outlines = self._outlines(self._segmentation_cells, None, None, None, bounds)
        rows = [{**row, **identity} for row in outlines.pop('visible_cells')]
        available = outlines.pop('outline_detail_available')
        # One retained overview per loaded channel set; changes replace it.
        if self._overview is None or self._overview[0] != channels:
            axis = min(256,max(1,int(np.sqrt(48*1024**2/(len(channels)*self.image.levels[0].dtype.itemsize)))))
            target = max(self.extent[1:3])/axis
            overview_level = max((i for i,size in enumerate(self.image.levels) if max(size.x_um_per_px,size.y_um_per_px)<=target),default=0)
            size = self.image.levels[overview_level]
            overview = self.image.read_display_window(overview_level, x=0, y=0,
                width=size.shape[2], height=size.shape[1],
                output_width=min(size.shape[2], max(1, round(axis*self.extent[1]/max(self.extent[1:3])))),
                output_height=min(size.shape[1], max(1, round(axis*self.extent[2]/max(self.extent[1:3])))),
                channels=channels, max_output_bytes=48*1024**2)
            self._overview = (channels, overview)
        colors, limits = {}, {}
        for i, channel in enumerate(channels):
            key = str(channel)
            if key not in self._limits:
                values = self._overview[1][i]; values = values[np.isfinite(values)]
                low, high = np.percentile(values, [1, 99]) if values.size else (0., 1.)
                self._limits[key] = [float(low), float(high if high > low else low+1)]
            low, high = map(float, options.get('limits', {}).get(key, self._limits[key]))
            if not np.isfinite([low, high]).all() or high <= low:
                raise ValueError('Each display maximum must be greater than its minimum.')
            limits[key] = [low, high]
            supplied = options.get('channel_colors', {}).get(key)
            colors[key] = supplied or channel_color(self.image.channels[channel], channel)
        display = {**options, 'channels': list(channels), 'limits': limits, 'channel_colors': colors,
                   'channel_display': options.get('channel_display', 'colour' if len(channels)>1 else 'grayscale'),
                   'image_uri': self.image.path.as_uri()}
        display.pop("browse_field_index", None)
        review_channels=list(range(len(self.image.channels)))
        if self.nuclear_channel is not None:
            review_channels.remove(self.nuclear_channel);review_channels.insert(0,self.nuclear_channel)
        from mif_pipeline.pipeline_core import StageStatus
        section_nodes = [node for node in snapshot.plan.nodes if node.section_target is not None
                         and node.section_target.section_id == identity['section_id']
                         and node.participant_ids == (identity['sample_id'],)]
        correction_available = not snapshot.head.pending_authorization_sha256_by_node and not any(
            (node.kind == 'measurements' and snapshot.head.node_status_by_id.get(node.node_id) is StageStatus.FAILED)
            or (node.kind == 'segmentation_correction' and snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.COMPLETED)
            for node in section_nodes)
        metadata = dict(review_kind='segmentation', source_frame=self.frame, image_uri=self.image.path.as_uri(), **identity,
            cell_correction_available=bool(correction_available),
            coverage_available=coverage_reason is None, coverage_unavailable_reason=coverage_reason,
            cell_coverage_available=cell_coverage_reason is None, cell_coverage_unavailable_reason=cell_coverage_reason,
            missing_nucleus_navigation_available=navigation_available,
            inspection_mode=mode, inspection_index=options.get('inspection_index', 0),
            missing_nucleus_count=count, inspection_cell=selected,
            cell_boundary_source=scope.get('cell_boundary_source'),
            nucleus_boundary_source=scope.get('nucleus_boundary_source'),
            available_channels=[dict(index=i,label=c['label'],color=channel_color(c,i)) for i,c in enumerate(self.image.channels)],
            nuclear_channel=self.nuclear_channel, review_channels=review_channels, field_um=[left,top,right,bottom], display_options=display,
            visible_cells=rows, outline_detail_available=available, field_cell_count=len(rows) if available else None,
            spatial_fields=self.spatial_fields, tissue_geometry=self.tissue_geometry, pyramid_level=level,
            pixel_size_um=(self.image.levels[level].x_um_per_px,self.image.levels[level].y_um_per_px), **sampling)
        result = dict(pixels=pixels, channel_indices=np.asarray(channels),
            channel_names=np.asarray([self.image.channels[c]['label'] for c in channels]),
            extent_um=np.asarray(extent), grid_extent_um=np.asarray(self.extent),
            overview_pixels=self._overview[1], metadata=metadata, **outlines)
        if coverage is not None:
            metadata['nuclear_coverage'] = coverage
        if overlay is not None:
            result.update(coverage_overlay=overlay[0], coverage_extent_um=overlay[1])
        for value in result.values():
            if isinstance(value, np.ndarray): value.setflags(write=False)
        return result
