"""Bounded tissue pixels and exact accepted cells for pending KRONOS setup.

The pending request owns source authority. Display reads never change accepted
cells, measurements, population membership, sampling, or encoder input pixels.
"""
from __future__ import annotations

from contextlib import closing
from copy import copy
from dataclasses import replace
import math
from pathlib import Path

import numpy as np

from mif_pipeline.datasets.geometry import wkb_polygon
from .batch_measurement_artifacts import decode_committed_measurements_result
from .batch_phenotyping.marker_review_gallery import measurement_cells
from .saved_cell_gallery import channel_color, gallery_resources, image_authority
from .secondary_region_cells import MAX_CELLS, MAX_VERTICES, MAX_FIELD_UM

MAX_NATIVE_GROUPS = 64
MAX_GEOMETRY_BYTES = 32 * 1024**2
MAX_PIXEL_BYTES = 64 * 1024**2


class _NoEligibleExamples(ValueError):
    """A bounded section search found no usable accepted initial location."""


def nucleus_centroid(raw):
    """Area centroid of accepted polygon components, subtracting their holes.

    Translate before the shoelace sum to retain precision at WSI coordinates.
    Ring orientation does not determine whether an accepted ring is a hole.
    """
    if raw is None:
        raise ValueError('This cell has no accepted nucleus outline. Choose another example.')
    shape = wkb_polygon(raw)
    polygons = [shape['coordinates']] if shape['type'] == 'Polygon' else shape['coordinates']
    origin = np.asarray(polygons[0][0][0], dtype=float)
    weighted, total = np.zeros(2), 0.
    for polygon in polygons:
        for index, ring in enumerate(polygon):
            points = np.asarray(ring, dtype=float) - origin
            if not np.isfinite(points).all():
                raise ValueError('The accepted nucleus outline has invalid coordinates.')
            cross = points[:-1, 0] * points[1:, 1] - points[1:, 0] * points[:-1, 1]
            twice_area = math.fsum(cross)
            if twice_area == 0:
                continue
            center = np.asarray([math.fsum((points[:-1, axis] + points[1:, axis]) * cross)
                                 for axis in range(2)]) / (3 * twice_area)
            weight = abs(twice_area) * (.5 if index == 0 else -.5)
            weighted += center * weight
            total += weight
    if not math.isfinite(total) or total <= 0:
        raise ValueError('The accepted nucleus outline has no positive area. Choose another example.')
    center = origin + weighted / total
    if not np.isfinite(center).all():
        raise ValueError('The accepted nucleus center is unavailable. Choose another example.')
    return tuple(map(float, center))


def _population_for_measurement(input_authorities, authority, measured):
    from .batch_phenotyping_mode_artifacts import decode_committed_phenotyping_mode
    modes = [decode_committed_phenotyping_mode(item) for item in input_authorities
             if item.artifact_ref.artifact_kind == 'phenotyping_mode']
    modes = [item for item in modes if item.analysis_domain_binding in measured.analysis_domain_bindings]
    if (not modes or len(modes) != len(measured.analysis_domain_bindings)
            or {item.analysis_domain_binding for item in modes} != set(measured.analysis_domain_bindings)
            or any(item.selected_mode != 'supervised_classifier'
                or (item.project_entry_id, item.sample_id, item.slide_id)
                   != (measured.project_entry_id, measured.sample_id, measured.slide_id)
                or item.measurements_result_ref_sha256 != authority.artifact_ref.artifact_ref_sha256
                or item.population_selection != modes[0].population_selection for item in modes)):
        raise ValueError('The supervised population does not match these accepted measurements. Reopen setup.')
    return dict(modes[0].population_selection)


def _accepted_groups(view, component, groups, columns):
    """Use the native applied/revision mask within only these physical groups."""
    group_ids = {view.layout['group_id'][int(index)].as_py() for index in groups}
    selected = replace(component, groups={group: items for group, items in component.groups.items() if group in group_ids})
    bounded = copy(view)
    bounded._components = [selected if item is component else item for item in view._components]
    yield from bounded.iter_applied_batches(component.path, columns=columns)


def _cell_bounds(row, center, calibration):
    shape = wkb_polygon(row['cell_geometry'])
    polygons = [shape['coordinates']] if shape['type'] == 'Polygon' else shape['coordinates']
    points = np.concatenate([np.asarray(ring, dtype=float) for polygon in polygons for ring in polygon])
    points = (points - center) * calibration
    if not np.isfinite(points).all():
        raise ValueError('The accepted cell outline has invalid coordinates.')
    return np.r_[points.min(axis=0), points.max(axis=0)]


def _cell_admission(view, measured, population):
    from mif_pipeline.cell_analysis.phenotyping_population import population_includes
    population = population or {'kind': 'all_segmented', 'class_ids': []}
    presence = 'present:measurement:' + measured.measurement_plan.features[0].measurement
    membership = None if population['kind'] == 'all_segmented' else 'population:' + population['kind']
    columns = ('cell_id', 'section_id', 'x_level0', 'y_level0', 'cell_geometry', 'nucleus_geometry', presence,
               *((membership,) if membership else ()))

    def included(row):
        if not row[presence] or row['section_id'] != measured.section_id:
            return False
        if membership is None:
            return True
        value = row[membership]
        if value is None:
            raise ValueError('The accepted cell population membership is unavailable. Refresh measurements.')
        if population['kind'] != 'detected_tissue':
            value = view.dictionaries[membership].decode(value)
        return population_includes(population, {population['kind']: value})
    return columns, included


def _example_row(view, component, measured, cell_id=None, population=None, *, max_groups=4):
    """Locate an initial tissue field within at most four native groups."""
    columns, included = _cell_admission(view, measured, population)
    selected = next(item for item in view._components if Path(item.path) == Path(component))
    if cell_id is not None:
        if not isinstance(cell_id, str) or not cell_id:
            raise ValueError('Choose an accepted cell ID.')
        accepted = copy(view)
        accepted._groups = lambda groups, columns: _accepted_groups(view, selected, groups, columns)
        rows = [row for batch in accepted.lookup_ids((cell_id,), columns=columns) for row in batch.to_pylist()]
        if len(rows) != 1 or not included(rows[0]):
            raise ValueError('The cell is outside this accepted phenotyping population.')
        nucleus_centroid(rows[0]['nucleus_geometry'])
        return rows[0]
    group_ids = view.layout['group_id'].to_pylist()
    candidates = [index for index, group in enumerate(group_ids) if group in selected.groups]
    if candidates:
        bounds = view.bounds[candidates]
        middle = (bounds[:, :2].min(axis=0) + bounds[:, 2:].max(axis=0)) / 2
        centers = (bounds[:, :2] + bounds[:, 2:]) / 2
        candidates = [candidates[index] for index in np.argsort(np.sum((centers-middle)**2, axis=1), kind='stable')]
    for index in candidates[:max_groups]:
        with closing(_accepted_groups(view, selected, (index,), columns)) as batches:
            batch = next(batches, None)
            if batch is None:
                continue
            group_center = (view.bounds[index, :2] + view.bounds[index, 2:]) / 2
            rows = sorted(batch.to_pylist(), key=lambda row:
                (row['x_level0'] - group_center[0])**2 + (row['y_level0'] - group_center[1])**2)
            for row in rows:
                if included(row) and row['nucleus_geometry'] is not None:
                    try:
                        center = nucleus_centroid(row['nucleus_geometry'])
                        _cell_bounds(row, center, 1.)
                    except ValueError:
                        continue
                    return row
    raise _NoEligibleExamples('No eligible example with an accepted nucleus was found in the inspected part of this image.')


def _empty_cells(message='Zoom in to see cells.'):
    return dict(cells=[], outline_detail_available=False, message=message,
        outline_vertices_um=np.empty((0, 2)), outline_offsets=np.asarray([0], dtype=np.int64),
        outline_cell_indices=np.empty(0, dtype=np.int64), outline_kinds=np.asarray([], dtype='U7'))


def _field_cells(view, component, measured, field_um, calibration, population, *, budget=None):
    """Use the native spatial index, gated by the exact applied/revision mask."""
    field = np.asarray(field_um, dtype=float)
    if max(field[2]-field[0], field[3]-field[1]) > MAX_FIELD_UM:
        return _empty_cells()
    budget = budget if budget is not None else dict(groups=0, cells=0, bytes=0, vertices=0)
    columns, included = _cell_admission(view, measured, population)
    selected = next(item for item in view._components if Path(item.path) == Path(component))
    bounds = field / np.tile(calibration, 2)
    intersects = ((view.bounds[:, 0] <= bounds[2]) & (view.bounds[:, 2] >= bounds[0]) &
                  (view.bounds[:, 1] <= bounds[3]) & (view.bounds[:, 3] >= bounds[1]))
    group_ids = view.layout['group_id'].to_pylist()
    groups = [int(index) for index in np.flatnonzero(intersects) if group_ids[int(index)] in selected.groups]
    budget['groups'] += len(groups)
    if budget['groups'] > MAX_NATIVE_GROUPS:
        return _empty_cells()
    accepted = copy(view)
    permitted = set(groups)
    accepted._groups = lambda indices, columns: _accepted_groups(
        view, selected, [index for index in indices if int(index) in permitted], columns)
    rows = []
    with closing(accepted.query(bounds.tolist(), columns=columns, predicate='intersects')) as batches:
        for batch in batches:
            budget['bytes'] += batch.nbytes
            if budget['bytes'] > MAX_GEOMETRY_BYTES:
                return _empty_cells()
            for row in batch.to_pylist():
                if included(row):
                    rows.append(row)
                    budget['cells'] += 1
                    if budget['cells'] > MAX_CELLS:
                        return _empty_cells()
    vertices, offsets, indices, kinds, cells = [], [0], [], [], []
    for index, row in enumerate(rows):
        try:
            center = (np.asarray(nucleus_centroid(row['nucleus_geometry'])) * calibration).tolist()
        except ValueError:
            center = None
        cell_bounds = _cell_bounds(row, np.zeros(2), calibration).tolist()
        cells.append(dict(cell_id=row['cell_id'], section_id=measured.section_id,
                          nucleus_center_um=center, cell_bounds_um=cell_bounds))
        for name, kind in (('cell_geometry', 'cell'), ('nucleus_geometry', 'nucleus')):
            shape = wkb_polygon(row[name]) if row[name] is not None else None
            polygons = [] if shape is None else [shape['coordinates']] if shape['type'] == 'Polygon' else shape['coordinates']
            for polygon in polygons:
                for ring in polygon:
                    points = np.asarray(ring, dtype=float) * calibration
                    if not np.isfinite(points).all():
                        raise ValueError('The accepted cell outline has invalid coordinates.')
                    budget['vertices'] += len(points)
                    if budget['vertices'] > MAX_VERTICES:
                        return _empty_cells()
                    vertices.extend(points); offsets.append(len(vertices))
                    indices.append(index); kinds.append(kind)
    return dict(cells=cells, outline_detail_available=True, message='',
        outline_vertices_um=np.asarray(vertices).reshape(-1, 2), outline_offsets=np.asarray(offsets, dtype=np.int64),
        outline_cell_indices=np.asarray(indices, dtype=np.int64), outline_kinds=np.asarray(kinds, dtype='U7'))


def _validated_field(field, extent):
    field = np.asarray(field, dtype=float)
    if (field.shape != (4,) or not np.isfinite(field).all()
            or not extent[0] <= field[0] < field[2] <= extent[2]
            or not extent[1] <= field[1] < field[3] <= extent[3]):
        raise ValueError('Choose a field inside the prepared image.')
    return field


def _settings(settings):
    entry, patch_size, sampling = settings.get('project_entry_id'), settings.get('patch_size'), settings.get('pixel_size_um')
    channels = settings.get('channel_ids', [settings.get('channel_id')])
    if not isinstance(entry, str) or not entry:
        raise ValueError('Choose a participating image to inspect.')
    if type(patch_size) is not int or patch_size not in (32, 64, 128, 256):
        raise ValueError('Choose a KRONOS image size of 32, 64, 128 or 256 samples.')
    if (not isinstance(sampling, (list, tuple)) or len(sampling) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or value <= 0 for value in sampling)):
        raise ValueError('Set positive finite X and Y sampling sizes in micrometres.')
    if (not isinstance(channels, (list, tuple)) or not 1 <= len(channels) <= 64
            or any(not isinstance(item, str) or not item for item in channels)
            or len(set(channels)) != len(channels)):
        raise ValueError('Choose up to 64 distinct image channels to display.')
    offered = settings.get('available_channel_ids', channels)
    if (not isinstance(offered, (list, tuple)) or not 1 <= len(offered) <= 256
            or any(not isinstance(item, str) or not item for item in offered)
            or len(set(offered)) != len(offered) or not set(channels).issubset(offered)):
        raise ValueError('Choose channels from the current image inputs.')
    if settings.get('request_key') is not None and (type(settings['request_key']) is not int or settings['request_key'] < 0):
        raise ValueError('Choose a current image field.')
    return entry, patch_size, np.asarray(sampling, dtype=float), tuple(channels)


def prepare_kronos_cell_preview(snapshot, input_authorities, settings):
    """Read one physical image field from this setup's exact measurement inputs."""
    entry, *_ = _settings(settings)
    matches = []
    for authority in input_authorities:
        if authority.artifact_ref.artifact_kind == 'measurements_result':
            measured = decode_committed_measurements_result(authority)
            if measured.project_entry_id == entry:
                matches.append((authority, measured))
    if settings.get('section_id') is not None:
        matches = [item for item in matches if item[1].section_id == settings['section_id']]
    if not matches:
        raise ValueError('This image has no accepted measurement source in the pending setup.')
    matches.sort(key=lambda item: item[1].section_id)
    if len({item[1].section_id for item in matches}) != len(matches):
        raise ValueError('The pending setup has ambiguous measurement sections. Reopen setup.')
    candidates = matches[:1] if settings.get('field_um') is not None else matches[:4]
    max_groups = max(1, 4 // len(candidates))
    for authority, measured in candidates:
        try:
            return _prepare_measurement_preview(snapshot, input_authorities, settings, authority, measured,
                max_groups=max_groups, additional_measurements=[item for item in matches if item[0] is not authority])
        except _NoEligibleExamples:
            if settings.get('section_id') is not None:
                raise
    raise _NoEligibleExamples('No eligible example with an accepted nucleus was found in the inspected sections of this image.')


def _prepare_measurement_preview(snapshot, input_authorities, settings, authority, measured, *, max_groups,
                                 additional_measurements=()):
    from mif_pipeline.io.processed_image import ProcessedImage
    from .region_review_image import overview_pixels, _preview_extent, CHUNK_CACHE_BYTES

    entry, patch_size, sampling, channel_ids = _settings(settings)
    population = _population_for_measurement(input_authorities, authority, measured)
    image = image_authority(authority, snapshot, measured)
    if image is None:
        raise ValueError('The accepted cells’ canonical image is unavailable.')
    by_channel = {item['channel_id']: item for item in measured.canonical_channels}
    offered_ids = settings.get('available_channel_ids', channel_ids)
    if any(channel not in by_channel for channel in offered_ids):
        raise ValueError('A display channel is absent from the accepted measurement image.')
    with gallery_resources(snapshot, authority, memory_bytes=256 * 1024**2):
        with ProcessedImage(image.locator, cache_bytes=CHUNK_CACHE_BYTES) as reader:
            base = reader.levels[0]
            calibration = np.asarray([base.x_um_per_px, base.y_um_per_px])
            extent = np.asarray([0., 0., base.shape[2]*calibration[0], base.shape[1]*calibration[1]])
            labels = [item['label'] for item in reader.channels]
            names = [by_channel[channel]['name'] for channel in channel_ids]
            if any(labels.count(name) != 1 for name in names):
                raise ValueError('Display channel names do not match the accepted canonical image.')
            channel_indices = [labels.index(name) for name in names]
            offered_names = [by_channel[channel]['name'] for channel in offered_ids]
            if any(labels.count(name) != 1 for name in offered_names):
                raise ValueError('The selected channel names do not match the accepted image.')
            available = [dict(index=labels.index(name), label=name, canonical_id=identity,
                color=channel_color(reader.channels[labels.index(name)], labels.index(name)))
                for identity, name in zip(offered_ids, offered_names)]
            budget = dict(groups=0, cells=0, bytes=0, vertices=0)
            with measurement_cells(measured) as (view, component, scope):
                if not np.allclose(calibration, [scope['x_um_per_px'], scope['y_um_per_px']], rtol=1e-10, atol=0):
                    raise ValueError('The accepted cells and prepared image use different coordinates.')
                if settings.get('field_um') is not None:
                    field = _validated_field(settings['field_um'], extent)
                else:
                    row = _example_row(view, component, measured, settings.get('cell_id'), population, max_groups=max_groups)
                    center = np.asarray(nucleus_centroid(row['nucleus_geometry'])) * calibration
                    viewport = settings.get('viewport_size', [720, 360])
                    if (not isinstance(viewport, (tuple, list)) or len(viewport) != 2 or
                            any(type(value) is not int or not 0 < value <= 16384 for value in viewport)):
                        raise ValueError('Choose a finite image viewport.')
                    aspect = min(3., max(.5, viewport[0]/viewport[1]))
                    height = max(128., min(192., 1.2*patch_size*float(sampling.max())))
                    size = np.minimum(extent[2:], [height*aspect, height])
                    start = np.maximum(0., np.minimum(center-size/2, extent[2:]-size))
                    field = np.r_[start, start+size]
                overlay = _field_cells(view, component, measured, field, calibration, population, budget=budget)
            source_refs = [authority.artifact_ref.artifact_ref_sha256]
            for other_authority, other in additional_measurements:
                if not overlay['outline_detail_available']:
                    break
                other_population = _population_for_measurement(input_authorities, other_authority, other)
                if (other.canonical_image_sha256 != measured.canonical_image_sha256 or
                        (other.sample_id, other.project_entry_id) != (measured.sample_id, measured.project_entry_id)):
                    raise ValueError('The accepted sections belong to different prepared images.')
                with measurement_cells(other) as (view, component, scope):
                    if not np.allclose(calibration, [scope['x_um_per_px'], scope['y_um_per_px']], rtol=1e-10, atol=0):
                        raise ValueError('The accepted sections use different image coordinates.')
                    following = _field_cells(view, component, other, field, calibration, other_population, budget=budget)
                source_refs.append(other_authority.artifact_ref.artifact_ref_sha256)
                if not following['outline_detail_available']:
                    overlay = following
                    break
                offset, index = len(overlay['outline_vertices_um']), len(overlay['cells'])
                overlay['outline_vertices_um'] = np.concatenate((overlay['outline_vertices_um'], following['outline_vertices_um']))
                overlay['outline_offsets'] = np.r_[overlay['outline_offsets'], following['outline_offsets'][1:]+offset]
                overlay['outline_cell_indices'] = np.r_[overlay['outline_cell_indices'], following['outline_cell_indices']+index]
                overlay['outline_kinds'] = np.r_[overlay['outline_kinds'], following['outline_kinds']]
                overlay['cells'].extend(following['cells'])
            crop = field / np.tile(calibration, 2)
            _, width, height, _ = _preview_extent(reader, crop)
            if len(channel_indices)*width*height*base.dtype.itemsize > MAX_PIXEL_BYTES:
                raise ValueError('Choose fewer display channels for this field.')
            pixels, level = overview_pixels(reader, channel_indices, crop_level0=crop)
            colors = {str(index): channel_color(reader.channels[index], index) for index in channel_indices}
            limits = {}
            for index, values in zip(channel_indices, pixels):
                finite = values[np.isfinite(values)]
                low, high = np.percentile(finite, [1, 99.5]) if finite.size else (0., 1.)
                limits[str(index)] = [float(low), float(high if high > low else low+1)]
    cells = overlay.pop('cells')
    detail, message = overlay.pop('outline_detail_available'), overlay.pop('message')
    return dict(bundle=dict(pixels=pixels[None], channel_indices=np.asarray(channel_indices),
            channel_names=np.asarray(names), extents_um=np.asarray([[field[0], field[2], field[3], field[1]]]), **overlay),
        cells=cells, outline_detail_available=detail, message=message,
        display_options=dict(channels=channel_indices, channel_colors=colors, limits=limits, available_channels=available,
            channel_display='colour', merged_panel=len(channel_indices)>1,
            image_uri=Path(image.locator).resolve().as_uri().rstrip('/')),
        project_entry_id=entry, section_id=measured.section_id, image_label=measured.slide_id,
        channel_ids=list(channel_ids), field_um=field.tolist(), image_extent_um=extent.tolist(),
        image_sha256=measured.canonical_image_sha256, source_ref_sha256=source_refs[0], source_ref_sha256s=source_refs,
        population_selection=population, request_key=settings.get('request_key'), level=level)
