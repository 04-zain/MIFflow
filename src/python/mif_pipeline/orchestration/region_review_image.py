"""Bounded prepared-image context for the existing region-review plot transport."""
from io import BytesIO
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image, PngImagePlugin

from mif_pipeline.io.processed_image import ProcessedImage
from .saved_cell_gallery import channel_color

REGION_REASONS = frozenset({
    'tissue_section_review_required', 'analysis_domain_choice_required',
    'analysis_domain_rois_required', 'analysis_domain_review_required',
    'semantic_region_decision_required', 'secondary_structure_selection_required',
})
MAX_SIDE = 1024
CHUNK_CACHE_BYTES = 16 * 1024**2
MAX_DECODE_BYTES = 64 * 1024**2


def _selected_image(store, snapshot, target):
    """Use the pending review's authenticated image identity in the current snapshot."""
    from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
    from mif_pipeline.pipeline_core.stage_result import InteractionReason

    terminal = store.load_attempt(target.needs_interaction_terminal_record_sha256)
    identity = terminal.execution_context_binding.current_image_identity_sha256
    if target.stage_result.interaction_reason in {
            InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
            InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED}:
        context = resolve_resume_envelope(target.stage_result.resume_context).service_context
        raw_inputs = [a.artifact_ref for a in snapshot.committed_artifact_authorities
                      if a.artifact_ref.artifact_kind == 'semantic_raw_inventory'
                      and a.artifact_ref.artifact_ref_sha256
                      in terminal.execution_context_binding.input_artifact_ref_sha256s
                      and (target.stage_result.interaction_reason is not InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED
                           or a.artifact_ref.artifact_ref_sha256 == context['semantic_count_state_descriptor']['raw_inventory_ref_sha256'])
                      and a.artifact_ref.scope.sample_id == target.sample_id
                      and a.artifact_ref.scope.project_entry_id == target.project_entry_id
                      and any(binding.analysis_domain_id == context['analysis_domain_id']
                              for binding in a.artifact_ref.scope.analysis_domain_bindings)]
        if len(raw_inputs) != 1:
            raise ValueError('The prepared image for these regions is unavailable.')
        identity = raw_inputs[0].owning_identity['canonical_image_sha256']
    matches = [a.artifact_ref for a in snapshot.committed_artifact_authorities
               if a.artifact_ref.artifact_kind == 'processed_image'
               and a.artifact_ref.scope.sample_id == target.sample_id
               and a.artifact_ref.sha256 == identity]
    if len(matches) != 1 or not matches[0].locator:
        raise ValueError('The prepared image for these regions is unavailable.')
    reference_channel = None
    from .batch_tissue_setup_artifacts import decode_committed_tissue_setup_result
    setups = [a for a in snapshot.committed_artifact_authorities
              if a.artifact_ref.artifact_kind == 'tissue_setup_result'
              and a.artifact_ref.scope.sample_id == target.sample_id
              and a.artifact_ref.scope.project_entry_id == target.project_entry_id]
    if len(setups) == 1:
        setup = decode_committed_tissue_setup_result(setups[0])
        if setup.canonical_image_sha256 == identity:
            reference_channel = setup.tissue_detection.reference_channel_id
    return matches[0], reference_channel


def _rgb_channels(reader):
    channels = reader.channels
    return (len(channels) == 3 and reader.levels[0].dtype == np.dtype('uint8')
            and [c.get('sample_index') for c in channels] == [0, 1, 2]
            and len({c.get('source_channel_id') for c in channels}) == 1
            and channels[0].get('source_channel_id') is not None)


def _channel_index(reader, attribute, reference_channel):
    if attribute.startswith('region-channel:'):
        value = attribute.removeprefix('region-channel:')
        if not re.fullmatch(r'-?\d+', value):
            raise ValueError('Choose an available image channel.')
        index = int(value)
        if index == -1 and _rgb_channels(reader):
            return index
        if not 0 <= index < len(reader.channels):
            raise ValueError('Choose an available image channel.')
        return index
    if reference_channel is not None:
        selected = [i for i, c in enumerate(reader.channels)
                    if c.get('component_id', c.get('source_channel_id')) == reference_channel]
        if len(selected) == 1:
            return selected[0]
    if _rgb_channels(reader):
        return -1
    return next((i for i, c in enumerate(reader.channels)
                 if any(marker in c.get('label', '').upper()
                        for marker in ('DAPI', 'HOECHST', 'DNA'))), 0)


def _preview_extent(reader, crop_level0=None):
    base = reader.levels[0]
    bounds = (0., 0., float(base.shape[2]), float(base.shape[1])) if crop_level0 is None else tuple(crop_level0)
    if (len(bounds) != 4 or not np.isfinite(bounds).all()
            or not 0 <= bounds[0] < bounds[2] <= base.shape[2]
            or not 0 <= bounds[1] < bounds[3] <= base.shape[1]):
        raise ValueError('The region preview must lie inside the image.')
    x0, y0, x1, y1 = bounds
    factor = min(1., MAX_SIDE / max(x1-x0, y1-y0))
    width, height = max(1, round((x1-x0)*factor)), max(1, round((y1-y0)*factor))
    level = next((i for i, item in enumerate(reader.levels)
                  if max((x1-x0)*base.x_um_per_px/item.x_um_per_px,
                         (y1-y0)*base.y_um_per_px/item.y_um_per_px) <= MAX_SIDE), len(reader.levels)-1)
    return bounds, width, height, level


def overview_pixels(reader, channels, *, crop_level0=None):
    """Sample coarse chunks at full-frame physical pixel centres.

    This nearest-neighbour overview retains actual prepared pixels. Output and
    borrowed chunk memory are bounded independently of whole-slide dimensions.
    The canonical level-0 extent sets the coordinates, including anisotropy.
    """
    base = reader.levels[0]
    (x0, y0, x1, y1), width, height, level = _preview_extent(reader, crop_level0)
    description = reader.levels[level]
    chunk_bytes = description.shape[0]*min(256, description.shape[1])*min(256, description.shape[2])*description.dtype.itemsize
    if chunk_bytes > MAX_DECODE_BYTES:
        raise ValueError('This image has too many channels for the overview memory allowance. Inspect it in QuPath.')
    xs = np.clip(((x0+(np.arange(width)+.5)*(x1-x0)/width)*base.x_um_per_px/description.x_um_per_px).astype(np.int64), 0, description.shape[2]-1)
    ys = np.clip(((y0+(np.arange(height)+.5)*(y1-y0)/height)*base.y_um_per_px/description.y_um_per_px).astype(np.int64), 0, description.shape[1]-1)
    output = np.empty((len(channels), height, width), dtype=description.dtype)
    for cy in np.unique(ys//256):
        oy = np.flatnonzero(ys//256 == cy)
        for cx in np.unique(xs//256):
            ox = np.flatnonzero(xs//256 == cx)
            block = reader.chunk(level, int(cy), int(cx))
            output[:, oy[:, None], ox] = block[np.ix_(channels, ys[oy]-cy*256, xs[ox]-cx*256)]
    return output, level


def _appearance(reader, attribute, reference_channel):
    """Resolve channel selection and validate only display settings, never pixels."""
    if attribute.startswith('region-display:'):
        try:
            requested = json.loads(attribute.removeprefix('region-display:'))
        except (ValueError, TypeError) as error:
            raise ValueError('Choose valid image appearance settings.') from error
        if not isinstance(requested, dict):
            raise ValueError('Choose valid image appearance settings.')
        mode, channels = requested.get('mode'), requested.get('channels')
    else:
        index = _channel_index(reader, attribute, reference_channel)
        mode, channels = ('rgb', [0, 1, 2]) if index == -1 else ('grayscale', [index])
        requested = {}
    if mode not in {'rgb', 'grayscale', 'colour', 'composite'}:
        raise ValueError('Choose Grayscale, Colour or Composite.')
    if (not isinstance(channels, list) or
            any(type(c) is not int or not 0 <= c < len(reader.channels) for c in channels) or
            len(set(channels)) != len(channels)):
        raise ValueError('Select distinct available image channels.')
    if mode in {'grayscale', 'colour'} and len(channels) > 1:
        raise ValueError('Select one channel, or choose Composite to combine channels.')
    if mode == 'rgb' and (not _rgb_channels(reader) or channels not in ([], [0, 1, 2])):
        raise ValueError('Native colour requires an RGB image.')
    colors, limits = {}, {}
    supplied_colors, supplied_limits = requested.get('colors', {}), requested.get('limits', {})
    if not isinstance(supplied_colors, dict) or not isinstance(supplied_limits, dict):
        raise ValueError('Enter valid channel colours and display limits.')
    for channel in channels:
        key = str(channel)
        color = supplied_colors.get(key, channel_color(reader.channels[channel], channel))
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise ValueError('Choose a channel colour with six hexadecimal RGB digits.')
        colors[key] = color.lower()
        if key in supplied_limits:
            raw = supplied_limits[key]
            if (not isinstance(raw, (list, tuple)) or len(raw) != 2 or
                    any(type(v) not in (int, float) for v in raw)):
                raise ValueError('Enter both channel display limits as numbers.')
            low, high = map(float, raw)
            if not np.isfinite([low, high]).all() or high <= low:
                raise ValueError('Each display maximum must exceed its minimum.')
            limits[key] = [low, high]
    return dict(mode=mode, channels=channels, colors=colors, limits=limits)


def render_prepared_overview(reader, *, attribute='cluster_id', reference_channel=None, image_uri=None, crop_level0=None):
    """Render the chosen appearance with bounded channel buffers and exact metadata.

    Channels are sampled one at a time, so composite memory does not grow with
    channel count. Automatic limits are calculated only for missing entries;
    callers retain the resolved limits when changing the view or channel mode.
    """
    display = _appearance(reader, attribute, reference_channel)
    (x0, y0, x1, y1), width, height, level = _preview_extent(reader, crop_level0)
    mode, channels = display['mode'], display['channels']
    if not channels:
        rendered = Image.new('L' if mode == 'grayscale' else 'RGB', (width, height), 0)
    elif mode == 'rgb':
        pixels, level = overview_pixels(reader, (0, 1, 2), crop_level0=crop_level0)
        rendered = Image.fromarray(np.moveaxis(pixels, 0, -1), mode='RGB')
        display['limits'] = {str(c): [0., 255.] for c in channels}
    else:
        accumulated = None
        for channel in channels:
            pixels, level = overview_pixels(reader, (channel,), crop_level0=crop_level0)
            values = pixels[0].astype(np.float32)
            key = str(channel)
            if key not in display['limits']:
                finite = values[np.isfinite(values)]
                low, high = np.percentile(finite, (1., 99.5)) if finite.size else (0., 1.)
                if high <= low:
                    high = low + 1.
                display['limits'][key] = [float(low), float(high)]
            low, high = display['limits'][key]
            scaled = np.nan_to_num((values-low)/(high-low), nan=0., posinf=1., neginf=0.)
            np.clip(scaled, 0., 1., out=scaled)
            if mode == 'grayscale':
                rendered = Image.fromarray((scaled*255).astype(np.uint8), mode='L')
            else:
                if accumulated is None:
                    accumulated = np.zeros((*scaled.shape, 3), dtype=np.float32)
                color = display['colors'][key][1:]
                for component in range(3):
                    accumulated[..., component] += scaled * (int(color[2*component:2*component+2], 16)/255.)
        if accumulated is not None:
            np.clip(accumulated, 0., 1., out=accumulated)
            rendered = Image.fromarray((accumulated*255).astype(np.uint8), mode='RGB')
    base = reader.levels[0]
    index = -1 if mode == 'rgb' or not channels else channels[0]
    metadata = dict(width_um=(x1-x0)*base.x_um_per_px, height_um=(y1-y0)*base.y_um_per_px,
                    origin_x_um=x0*base.x_um_per_px, origin_y_um=y0*base.y_um_per_px,
                    size_x_px=base.shape[2], size_y_px=base.shape[1],
                    x_um_per_px=base.x_um_per_px, y_um_per_px=base.y_um_per_px,
                    channels=[dict(index=i, id=c.get('component_id') or c.get('source_channel_id'),
                                   label=c.get('label', f'Channel {i+1}'), color=channel_color(c, i))
                              for i, c in enumerate(reader.channels)],
                    selected_index=index, display_mode=mode, level=level,
                    display_limits=display['limits'][str(channels[0])] if channels else [], display=display,
                    native_rgb=_rgb_channels(reader), image_uri=image_uri)
    info = PngImagePlugin.PngInfo()
    info.add_text('mif-region-image', json.dumps(metadata, separators=(',', ':')))
    buffer = BytesIO()
    rendered.save(buffer, format='PNG', pnginfo=info)
    return buffer.getvalue()


def render_region_review_image(store, snapshot, target, attribute):
    image, reference_channel = _selected_image(store, snapshot, target)
    projection = target.region_review or {}
    with ProcessedImage(image.locator, cache_bytes=CHUNK_CACHE_BYTES) as reader:
        data = render_prepared_overview(reader, attribute=attribute, reference_channel=reference_channel,
                                       image_uri=Path(image.locator).resolve().as_uri().rstrip('/'),
                                       crop_level0=projection.get('crop_level0'))
    return (('region-image', 'Prepared image', 'png', data),)
