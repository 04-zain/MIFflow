"""Bounded image examples from accepted cell IDs and their canonical image."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .batch_artifacts import BatchCommittedArtifactAuthority
    from .batch_state import BatchExecutionSnapshot
    from mif_pipeline.datasets.collection import CellDatasetCollection
import json
from pathlib import Path
import numpy as np
from mif_pipeline.io.channel_colors import channel_color

MAX_CELLS = 8
MAX_CHANNELS = 6
MAX_CROP_BYTES = 32 * 1024**2


def image_authority(authority, snapshot, frame):
    return next((item.artifact_ref for item in snapshot.committed_artifact_authorities
                 if item.artifact_ref.artifact_kind == 'processed_image'
                 and item.artifact_ref.scope.sample_id == authority.artifact_ref.scope.sample_id
                 and item.artifact_ref.sha256 == frame.canonical_image_sha256), None)


def gallery_choice(authority, snapshot, *, state_store=None):
    from .saved_cell_figures import _measurements, cell_frame
    measurements=[dict(measurement=item.measurement,units=item.units) for item in _measurements(authority,snapshot,state_store=state_store)]
    if authority.artifact_ref.artifact_kind in {'segmentation_result', 'measurements_result'}:
        return _gallery_choice(image_authority(authority, snapshot, cell_frame(authority)), (),measurements)
    from .exports import load_spatial_handoff
    handoff = load_spatial_handoff(authority.artifact_ref.locator)
    try:
        image = image_authority(authority, snapshot, handoff.spatial_frame)
        dictionary = handoff.cell_dataset.dictionaries.get('primary_phenotype')
        return _gallery_choice(image, dictionary.labels if dictionary else (),measurements)
    finally:
        handoff.cell_dataset.close()


def _gallery_choice(image, phenotypes, measurements=()):
    from .batch_service_models import BatchSavedStageFigure
    if image is None or not (Path(image.locator) / 'zarr.json').is_file():
        return ()
    metadata = json.loads((Path(image.locator) / 'zarr.json').read_bytes())
    channels = metadata['attributes']['ome']['omero']['channels']
    settings = {'channels': [dict(index=i, label=c['label']) for i, c in enumerate(channels)],
                'phenotypes': list(phenotypes), 'measurements':list(measurements),
                'image_uri': Path(image.locator).as_uri()}
    return (BatchSavedStageFigure('cell-gallery', 'Cell gallery',
            'Image crops around selected accepted cells. Choose cell IDs or a repeatable sample, '
            'crop size and channels. Display limits are shared across all cells.',
            settings=settings, data_formats=('csv', 'npz')),)


def select_ids(dataset: CellDatasetCollection, options: Mapping[str, Any], *, return_count=False):
    """Count the filtered population and retain a bounded repeatable sample.

    Sampling assigns reproducible random priorities in stable accepted row order
    and retains only the requested smallest priorities. Chosen phenotypes form
    an OR selection, intersected with every inclusive measurement range. Reads
    project only IDs, required labels, measurements and their presence flags.
    Explicit IDs replace these filters; later lookup authenticates membership.
    """
    explicit = tuple(options.get('cell_ids', ()))
    if explicit:
        if len(explicit) > MAX_CELLS or len(set(explicit)) != len(explicit):
            raise ValueError('Enter up to eight distinct cell IDs.')
        return (explicit,len(explicit)) if return_count else explicit
    count = int(options.get('count', 4))
    if not 1 <= count <= MAX_CELLS:
        raise ValueError('Choose between one and eight cell examples.')
    seed = int(options.get('seed', 0))
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError('Sample seed must be between 0 and 4294967295.')
    phenotypes=tuple(options.get('phenotypes') or ([options['phenotype']] if options.get('phenotype') is not None else ()))
    labels = dataset.dictionaries.get('primary_phenotype')
    if phenotypes and (labels is None or any(name not in labels.labels for name in phenotypes)):
        raise ValueError('Choose available phenotypes.')
    codes=[labels.labels.index(name) for name in phenotypes]
    columns=['cell_id']+(['primary_phenotype'] if codes else [])
    ranges=options.get('measurement_ranges') or ()
    if not isinstance(ranges,(list,tuple)) or len(ranges)>8:
        raise ValueError('Choose up to eight measurement ranges.')
    filters=[]
    for item in ranges:
        if not isinstance(item,Mapping) or not isinstance(item.get('measurement'),str):
            raise ValueError('Choose a saved measurement for each range.')
        column='measurement:'+item['measurement'];presence='present:'+column
        if column not in dataset.schema.names:
            raise ValueError('A selected measurement is unavailable in this result.')
        lower=float(item['lower']) if item.get('lower') is not None else -np.inf
        upper=float(item['upper']) if item.get('upper') is not None else np.inf
        if np.isnan(lower) or np.isnan(upper) or lower>upper or (item.get('lower') is not None and not np.isfinite(lower)) or (item.get('upper') is not None and not np.isfinite(upper)):
            raise ValueError('Measurement limits must be finite and ordered.')
        columns.append(column)
        if presence in dataset.schema.names:columns.append(presence)
        filters.append((column,presence,lower,upper))
    columns=tuple(dict.fromkeys(columns))
    random = np.random.default_rng(seed)
    keys, ids = np.array([], dtype=float), np.array([], dtype=object)
    matched=0
    for batch in dataset.iter_batches(columns=columns):
        selected=np.ones(batch.num_rows,dtype=bool)
        if codes:
            selected&=np.isin(batch.column('primary_phenotype').to_numpy(zero_copy_only=False),codes)
        for column,presence,lower,upper in filters:
            values=batch.column(column);numeric=values.to_numpy(zero_copy_only=False)
            selected&=values.is_valid().to_numpy(zero_copy_only=False)&np.isfinite(numeric)&(numeric>=lower)&(numeric<=upper)
            if presence in columns:
                selected&=batch.column(presence).fill_null(False).to_numpy(zero_copy_only=False)
        batch=batch.filter(selected)
        incoming = batch.column('cell_id').to_numpy(zero_copy_only=False)
        matched+=len(incoming)
        keys = np.concatenate((keys, random.random(len(incoming))))
        ids = np.concatenate((ids, incoming))
        if len(keys) > count:
            keep = np.argpartition(keys, count - 1)[:count]
            keys, ids = keys[keep], ids[keep]
    if not len(ids):
        raise ValueError('No accepted cells match this selection.')
    result=tuple(str(value) for value in ids[np.argsort(keys)])
    return (result,matched) if return_count else result


@dataclass
class GalleryData:
    crops: tuple
    channels: tuple
    channel_labels: tuple
    cell_ids: tuple
    labels: tuple
    outlines: tuple
    extents: tuple
    crop_um: float
    limits: tuple
    selection: dict
    frame: dict
    pyramid_level: int
    pixel_size_um: tuple
    centers_um: tuple
    channel_colors: tuple = ()


def load_gallery(authority: BatchCommittedArtifactAuthority, snapshot: BatchExecutionSnapshot,
                 options: Mapping[str, Any], *, state_store=None) -> GalleryData:
    """Read selected cells and physical image crops in their accepted canonical frame.

    Resolve IDs before retrieving their geometry. Choose one saved pyramid level
    for every crop, preserve edge padding as NaN, and convert outlines to physical
    coordinates relative to each cell centre. Returned arrays own their pixels;
    the image reader closes before display or export. Initial per-channel limits
    use pooled crop percentiles and remain separate from the stored pixel values.
    """
    from .saved_cell_figures import _selected_cells, cell_frame
    segmented = authority.artifact_ref.artifact_kind == 'segmentation_result'
    measured = authority.artifact_ref.artifact_kind == 'measurements_result'
    frame = cell_frame(authority)
    image = image_authority(authority, snapshot, frame)
    if image is None:
        raise ValueError('The accepted cells’ canonical image is unavailable.')
    with _selected_cells(authority, state_store=state_store, snapshot=snapshot) as dataset:
        ids,matched = select_ids(dataset, options,return_count=True)
        columns = ('cell_id', 'x_level0', 'y_level0', 'cell_geometry', 'nucleus_geometry', 'primary_phenotype')
        rows = {row['cell_id']: row for batch in dataset.lookup_ids(ids, columns=columns) for row in batch.to_pylist()}
        if set(rows) != set(ids):
            raise ValueError('Some requested cell IDs are absent from this accepted result.')
        dictionary = None if measured or segmented else dataset.dictionaries.get('primary_phenotype')
        labels = tuple(dictionary.labels[rows[id]['primary_phenotype']]
                       if dictionary is not None and rows[id]['primary_phenotype'] is not None
                       else 'Segmented cell' if segmented else 'Measured cell' if measured else 'Unavailable' for id in ids)
        scope = dataset.description['scope'] if segmented else dataset.scope if measured else None
        pixel_size = ((scope['x_um_per_px'], scope['y_um_per_px']) if scope is not None
                      else (frame.x_um_per_px, frame.y_um_per_px))
    return load_cell_crops(image.locator, rows, ids, labels, {**options,'selection_count':matched},
        image_id=frame.canonical_image_id, image_sha256=frame.canonical_image_sha256,
        pixel_size_um=pixel_size,
        level0_size=None if measured or segmented else (frame.level0_size_x_px, frame.level0_size_y_px))


def load_cell_crops(image_path, rows, ids, labels, options, *, image_id,
                    image_sha256, pixel_size_um, level0_size=None):
    """Read bounded calibrated crops for already-resolved cell identities and geometry."""
    from mif_pipeline.io.processed_image import ProcessedImage
    from mif_pipeline.datasets.geometry import wkb_polygon

    crop_um = float(options.get('crop_um', 40.))
    if not np.isfinite(crop_um) or crop_um <= 0:
        raise ValueError('Crop width must be a positive number of micrometres.')
    aspect = float(options.get('field_aspect', 1.))
    if not np.isfinite(aspect) or not .25 <= aspect <= 4:
        raise ValueError('The image field aspect ratio must be between 0.25 and 4.')
    field_width, field_height = crop_um * aspect, crop_um
    channels = tuple(int(c) for c in options.get('channels', ()))
    if not 1 <= len(channels) <= MAX_CHANNELS or len(set(channels)) != len(channels):
        raise ValueError('Choose between one and six distinct image channels.')
    if not 1 <= len(ids) <= MAX_CELLS or len(set(ids)) != len(ids):
        raise ValueError('Choose between one and eight distinct cells in one image.')
    with ProcessedImage(image_path, cache_bytes=16 * 1024**2) as reader:
        if min(channels) < 0 or max(channels) >= len(reader.channels):
            raise ValueError('A selected image channel is unavailable.')
        base = reader.levels[0]
        if (level0_size is not None and (base.shape[2], base.shape[1]) != level0_size) or not np.allclose(
                [base.x_um_per_px, base.y_um_per_px], pixel_size_um, rtol=1e-10, atol=0):
            raise ValueError('Image dimensions or calibration differ from the accepted cell frame.')
        level = options.get('pyramid_level')
        if level is None:
            level = next((i for i, item in enumerate(reader.levels)
                          if max(field_width / item.x_um_per_px, field_height / item.y_um_per_px) <= 256), len(reader.levels) - 1)
        elif isinstance(level, bool) or not isinstance(level, int) or not 0 <= level < len(reader.levels):
            raise ValueError('Choose an available source image pyramid level.')
        size = reader.levels[level]
        width, height = int(np.ceil(field_width / size.x_um_per_px)), int(np.ceil(field_height / size.y_um_per_px))
        output_bytes = len(ids) * len(channels) * width * height * 8
        if output_bytes > MAX_CROP_BYTES:
            raise ValueError('These crops require more than 32 MiB of pixels. Reduce crop width, cells or channels.')
        crops, outlines, extents, centers = [], [], [], []
        for id in ids:
            row = rows[id]
            cx, cy = row['x_level0'] * pixel_size_um[0], row['y_level0'] * pixel_size_um[1]
            centers.append((cx, cy))
            x, y = int(np.floor(cx / size.x_um_per_px - width / 2)), int(np.floor(cy / size.y_um_per_px - height / 2))
            left, top, right, bottom = max(0, x), max(0, y), min(size.shape[2], x + width), min(size.shape[1], y + height)
            crop = np.full((len(channels), height, width), np.nan)
            if right > left and bottom > top:
                crop[:, top-y:bottom-y, left-x:right-x] = reader.read_window(level, x=left, y=top,
                    width=right-left, height=bottom-top, channels=channels, max_output_bytes=MAX_CROP_BYTES)
            crop.setflags(write=False)
            crops.append(crop)
            extents.append((x * size.x_um_per_px - cx, (x+width) * size.x_um_per_px - cx,
                            (y+height) * size.y_um_per_px - cy, y * size.y_um_per_px - cy))
            rings = []
            for name in ('cell_geometry', 'nucleus_geometry'):
                geometry = wkb_polygon(row[name]) if row[name] is not None else None
                polygons = ([] if geometry is None else [geometry['coordinates']]
                            if geometry['type'] == 'Polygon' else geometry['coordinates'])
                rings.append(tuple(np.asarray(ring) * pixel_size_um - [cx, cy]
                                   for polygon in polygons for ring in polygon))
            outlines.append(tuple(rings))
        limits = []
        for col in range(len(channels)):
            values = np.concatenate([crop[col][np.isfinite(crop[col])] for crop in crops])
            low, high = np.percentile(values, [1, 99]) if values.size else (0., 1.)
            if low == high:
                high = low + max(abs(low) * .01, 1.)
            limits.append((float(low), float(high)))
        return GalleryData(tuple(crops), channels, tuple(reader.channels[c]['label'] for c in channels),
                           ids, labels, tuple(outlines), tuple(extents), crop_um, tuple(limits), {**options,'image_uri':reader.path.as_uri()},
                           {'image_id': image_id, 'image_sha256': image_sha256,
                            'level0_pixel_size_x_um': pixel_size_um[0], 'level0_pixel_size_y_um': pixel_size_um[1]},
                           level, (size.x_um_per_px, size.y_um_per_px), tuple(centers),
                           tuple(channel_color(reader.channels[c], c) for c in channels))


def display_options(data: GalleryData, options: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve editable shared display ranges and attach the exact selected IDs.

    Blank ranges use the crop bundle's initial limits. Explicit ranges must be
    finite and increasing. This operation reuses loaded values without reading
    image pixels or changing the accepted measurements.
    """
    mode = options.get('channel_display', 'grayscale')
    merged = options.get('merged_panel', False)
    if mode not in ('grayscale', 'colour'):
        raise ValueError('Choose Grayscale or Colour for channel display.')
    if not isinstance(merged, bool):
        raise ValueError('Choose whether to show a merged colour panel.')
    colors = data.channel_colors or tuple(channel_color({}, c) for c in data.channels)
    supplied_colors = options.get('channel_colors') or {}
    colors = tuple(channel_color({'color': supplied_colors[str(c)].lstrip('#')}, c)
                   if str(c) in supplied_colors else color for c, color in zip(data.channels, colors))
    supplied = options.get('limits') or {}
    limits = []
    for channel, defaults in zip(data.channels, data.limits):
        values = supplied.get(str(channel), defaults)
        low, high = map(float, values)
        if not np.isfinite([low, high]).all() or high <= low:
            raise ValueError('Each display maximum must be greater than its minimum.')
        limits.append((low, high))
    return {**options, **({'image_uri':data.selection['image_uri']} if 'image_uri' in data.selection else {}), 'channel_display': mode, 'merged_panel': merged,
            'channel_colors': dict(zip(map(str, data.channels), colors)), 'limits': {str(c): list(v) for c, v in zip(data.channels, limits)},
            'selected_cell_ids': list(data.cell_ids), 'selected_labels': list(data.labels),
            **({'selection_count':data.selection['selection_count']} if 'selection_count' in data.selection else {})}


from contextlib import contextmanager


@contextmanager
def gallery_resources(snapshot, authority, *, memory_bytes=640 * 1024**2):
    """Admit crop buffers, bounded decode cache and three-format rendering together."""
    from .batch_resources import BatchResourceDemand, ResourceLeasePool
    from .batch_dag import ResourceKind
    from .batch_phenotyping._common import digest
    from mif_pipeline.pipeline_core.execution_resources import operation_budget

    # Eight rows × six channel panels plus a merged panel at 300 dpi,
    # renderer temporaries, bounded crops, NPZ staging and the decode cache.
    demand = BatchResourceDemand(node_id=authority.artifact_ref.producer_node_id,
        adapter_spec_sha256=digest({'operation': 'saved-cell-gallery'}),
        input_artifact_set_sha256=authority.artifact_ref.artifact_ref_sha256,
        resource_kind=ResourceKind.SCIENTIFIC_COMPUTE, worker_slots=1, future_slots=1,
        working_set_bytes=memory_bytes, cpu_threads=1)
    reservation = ResourceLeasePool(snapshot.manifest.manifest.execution_config).try_acquire(demand)
    if reservation is None:
        raise ValueError('Memory is in use. Wait for processing to finish and open the gallery again.')
    try:
        with operation_budget(memory_bytes, 1):
            yield
    finally:
        reservation.release()
