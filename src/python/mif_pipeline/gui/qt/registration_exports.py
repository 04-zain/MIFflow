"""Export already displayed alignment pixels and existing sampled checks."""
import csv
import json
import math
from copy import deepcopy
from io import BytesIO, StringIO

import numpy as np

from .registration_review_geometry import grid_sample


def field_data(frame, bounds_um, pixel_size):
    """Crop the retained native grid without interpolating its channel values."""
    px, py = (frame['downsample'] * value for value in pixel_size)
    origin_x, origin_y = frame['x'] * pixel_size[0], frame['y'] * pixel_size[1]
    height, width = frame['arrays'][0].shape[1:]
    left, top, right, bottom = bounds_um
    x0, y0 = max(0, math.floor((left-origin_x)/px)), max(0, math.floor((top-origin_y)/py))
    x1, y1 = min(width, math.ceil((right-origin_x)/px)), min(height, math.ceil((bottom-origin_y)/py))
    if x1 <= x0 or y1 <= y0:
        raise ValueError('The current field has no sampled pixels to export.')
    result = dict(x_um=origin_x+(np.arange(x0, x1)+.5)*px,
                  y_um=origin_y+(np.arange(y0, y1)+.5)*py)
    for image, values in zip(frame['images'], frame['arrays']):
        result[image['source']+'_pixels'] = values[:, y0:y1, x0:x1].copy()
        result[image['source']+'_channel_indices'] = np.asarray(image['channels'], dtype=np.int32)
    result['metadata'] = dict(
        coordinate_space='fixed mIF image, micrometres', revision=frame['revision'],
        view_bounds_um=list(bounds_um),
        sampled_bounds_um=[origin_x+x0*px, origin_y+y0*py, origin_x+x1*px, origin_y+y1*py],
        pixel_size_um=[px, py], downsample=frame['downsample'],
        array_axes='channel, y, x; x_um/y_um give sample centres',
        paired_resampling='Nearest neighbour (Warpy interpolation mode 0); inverse accepted transform into the current mIF sampling grid.',
        data_scope='Bounded sampled viewport only. Source pyramid/resolution is chosen by the native image reader. '
                   'Paired values are already resampled, not original paired-image pixels. '
                   'The viewport may include positions outside the paired image. Separate sampled checks describe coverage at their grid centres; they are not a per-pixel validity mask.',
        images=[{key: value for key, value in image.items() if key != 'file'} for image in frame['images']])
    return result


def field_npz(bundle):
    stream = BytesIO()
    np.savez_compressed(stream, **{key: value for key, value in bundle.items() if key != 'metadata'},
                        metadata_json=np.asarray(json.dumps(bundle['metadata'], sort_keys=True)))
    return stream.getvalue()


def sampled_checks_csv(scope, pixel_size):
    """Write every existing grid sample in the selected review scope."""
    stream = StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('scope', 'row', 'column', 'x_mif_um', 'y_mif_um', 'state', 'state_label', 'area_ratio'))
    states = ('outside_review_region', 'outside_paired_image', 'inside_paired_image', 'invalid_mapping')
    grid = scope['grid']
    for row in range(grid['rows']):
        for column in range(grid['columns']):
            point = grid_sample(grid, column, row, pixel_size)
            writer.writerow((scope['name'], row, column, *point['point_um'], point['state'],
                             states[point['state']], point['area_ratio']))
    return stream.getvalue().encode('utf-8')


def check_summary_csv(scope):
    """Preserve unrounded saved evidence; unavailable numeric values stay absent."""
    stream = StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('scope', 'metric', 'value', 'unit'))
    def rows(mapping, prefix=''):
        for key, value in mapping.items():
            name = prefix+key
            if isinstance(value, dict):
                rows(value, name+'.')
            elif value is not None:
                unit = ('µm' if key.endswith('_um') else 'fraction' if key.endswith('_fraction')
                        else 'ratio' if key.startswith('jacobian_') else 'positions' if key in
                        {'sample_count', 'negative_jacobian_count'} else '')
                writer.writerow((scope['name'], name, value, unit))
    rows(scope['evidence'])
    return stream.getvalue().encode('utf-8')


def capture_field(editor):
    """Capture the settled physical scene, excluding viewport controls."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    frame = editor._buffers[('fixed', False)]
    view = editor.view
    visible = view.mapToScene(view.viewport().rect()).boundingRect()
    bounds = visible.intersected(view.sceneRect())
    if bounds.isEmpty():
        raise ValueError('Move the view over the image before exporting.')
    bundle = field_data(frame, (bounds.left(), bounds.top(), bounds.right(), bounds.bottom()),
                        (editor._meta['fixed']['x_um'], editor._meta['fixed']['y_um']))
    width = max(1, round(view.viewport().width()*bounds.width()/visible.width()))
    height = max(1, round(view.viewport().height()*bounds.height()/visible.height()))
    canvas = QImage(width, height, QImage.Format_RGBA8888)
    canvas.fill(QColor('#172329'))
    painter = QPainter(canvas)
    painter.setRenderHints(view.renderHints())
    view.scene().render(painter, QRectF(0, 0, width, height), bounds, Qt.IgnoreAspectRatio)
    painter.end()
    bundle['rendered_rgba'] = np.frombuffer(canvas.bits(), dtype=np.uint8).reshape(height, canvas.bytesPerLine())[:, :width*4].reshape(height, width, 4).copy()
    index = editor.scope.currentIndex()
    scope = editor._scopes[index] if 0 <= index < len(editor._scopes) else None
    bundle['metadata'].update(
        display_source=editor._display_source(), paired_label=editor._paired_label,
        paired_opacity=editor.opacity.value()/100, appearances=deepcopy(editor._appearances),
        source_metadata=deepcopy({name: editor._meta[name] for name in ('fixed', 'moving')}),
        tissue_outlines_visible=editor.tissue_visible.isChecked(),
        check_map_legend=editor.key.text() if editor.key.isVisible() else '',
        selected_check=(editor.checks.item(editor.checks.currentRow(), 0).text()
                        if editor.checks.currentRow() >= 0 else None),
        selected_check_row=editor.checks.currentRow(),
        selected_scope=deepcopy(scope), aligned_image_outline=editor._state.get('outline', []),
        tissue_regions=deepcopy(editor._meta.get('tissue', [])),
        outline_coordinate_space='fixed mIF image pixels; multiply x/y by source_metadata.fixed.x_um/y_um',
        rendered_scope='RGB scene rendering at the current display resolution; includes visible contours/check map, excludes interface controls.')
    return bundle


def field_figure(bundle):
    """Lay out the captured image, calibrated scale and compact display legend."""
    import textwrap
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    meta = bundle['metadata']
    left, top, right, bottom = meta['view_bounds_um']
    width, height = right-left, bottom-top
    handles = []
    if meta['display_source'] != 'moving':
        appearance = meta['appearances']['fixed']
        channels = meta['source_metadata']['fixed']['channels']
        for index in appearance['channels']:
            label = channels[index]['label'] if isinstance(channels[index], dict) else channels[index]
            color = '#6c7474' if appearance['mode'] in {'rgb', 'grayscale'} else appearance['colors'][str(index)]
            handles.append(Patch(facecolor=color, edgecolor='none', label=label))
    if meta['display_source'] != 'fixed':
        label = meta['paired_label']
        if meta['display_source'] == 'overlay':
            label += f" · {meta['paired_opacity']:.0%} opacity"
        handles.append(Patch(facecolor='#ae7895', edgecolor='none', label=label))
    if 'aligned_image_outline' in meta:
        selected_tissue = meta.get('tissue_outlines_visible') and bool((meta.get('selected_scope') or {}).get('rings'))
        handles.append(Line2D([], [], color='#23bcb8', linewidth=2,
                              label='mIF bounds / selected tissue' if selected_tissue else 'mIF bounds'))
        if meta['aligned_image_outline']:
            handles.append(Line2D([], [], color='#f3b746', linewidth=2, label='Aligned image bounds'))
        if meta.get('tissue_outlines_visible') and len(meta.get('tissue_regions', [])) > int(selected_tissue):
            handles.append(Line2D([], [], color='#d8e6dc', linewidth=3, label='Tissue outlines'))
    note = '' if meta.get('selected_check_row') == 1 else meta.get('check_map_legend', '')
    note = '\n'.join(textwrap.wrap(note, 95))
    image_height = min(8., max(3., 8*height/width))
    legend_height = .36*max(1, math.ceil(len(handles)/3)) + (.25*len(note.splitlines()) if note else 0)
    figure = Figure(figsize=(8, image_height+legend_height))
    axis = figure.add_axes((.01, legend_height/(image_height+legend_height), .98, image_height/(image_height+legend_height)-.01))
    axis.imshow(bundle['rendered_rgba'], extent=(left, right, bottom, top), interpolation='nearest')
    axis.set(xlim=(left, right), ylim=(bottom, top))
    axis.set_axis_off()
    desired = width/6
    decade = 10**math.floor(math.log10(desired))
    length = max(value*decade for value in (1, 2, 5, 10) if value*decade <= desired)
    x, y = left+.025*width, bottom-.045*height
    axis.plot((x, x+length), (y, y), color='black', linewidth=5)
    axis.plot((x, x+length), (y, y), color='white', linewidth=2.5)
    axis.text(x, y-.025*height, f'{length:g} µm', color='white', fontsize=10,
              bbox=dict(facecolor='black', alpha=.7, edgecolor='none', pad=2))
    if handles:
        figure.legend(handles=handles, loc='lower center', ncol=min(3, len(handles)),
                      bbox_to_anchor=(.5, (.25*len(note.splitlines()) if note else 0)/(image_height+legend_height)),
                      frameon=False, fontsize=10)
    if note:
        figure.text(.5, .01, note, ha='center', va='bottom', fontsize=9)
    return figure
