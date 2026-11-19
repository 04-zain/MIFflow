"""Publication panels from bounded real image crops and accepted outlines."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from mif_pipeline.orchestration.saved_cell_gallery import GalleryData

import textwrap
import json
import numpy as np


def _publication_gallery_figure(data: GalleryData, options: Mapping[str, Any]):
    """Arrange the existing image fields at print size, without changing their data."""
    from matplotlib import colormaps, patheffects
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, Rectangle
    from matplotlib.textpath import TextToPath
    from pathlib import Path
    from .figure_style import apply_figure_font

    merged = bool(options.get('merged_panel', False))
    coloured = options.get('channel_display', 'grayscale') == 'colour'
    columns = len(data.channels) + int(merged)
    width = 89. if columns <= 3 else 183.
    field_width = data.crop_um * float(data.selection.get('field_aspect', 1.))
    field_height = data.crop_um
    aspect = field_width / field_height
    margin, gap, row_gap = 2., 1.6, 1.8
    font = FontProperties(fname=Path(__file__).parent / 'data/fonts/LiberationSans-Regular.ttf', size=7.5)
    text_metrics = TextToPath()

    def text_width(value):
        # Include a little room for wider fonts when the SVG is edited elsewhere.
        return text_metrics.get_text_width_height_descent(value, font, ismath=False)[0] * 25.4 / 72 * 1.12

    # Keep the row labels close to the images; long names wrap without losing text.
    longest = max((text_width(line) for label in data.labels for line in label.splitlines()), default=0)
    gutter = min(28., max(14., 6.5 + longest))
    panel_width = (width - 2 * margin - gutter - gap * (columns - 1)
                   - (1.2 if merged else 0.)) / columns
    panel_height = panel_width / aspect

    def wrapped(value, available_mm):
        lines = []
        for source in str(value).splitlines():
            current = ''
            for word in source.split():
                if current:
                    candidate = current + ' ' + word
                    if text_width(candidate) <= available_mm:
                        current = candidate
                        continue
                    lines.append(current)
                    current = ''
                for letter in word:
                    candidate = current + letter
                    if current and text_width(candidate) > available_mm:
                        lines.append(current)
                        current = ''
                    current += letter
            lines.append(current)
        return '\n'.join(lines)

    labels = [wrapped(label, gutter - 6) for label in data.labels]
    headings = [wrapped(label, panel_width - 1.5) for label in data.channel_labels]
    if merged:
        headings.append('Merge')
    line_mm = 7.5 * 1.2 / 72 * 25.4
    header = 3.2 + max(label.count('\n') + 1 for label in headings) * line_mm
    if coloured or merged:
        header += 1.2
    row_heights = [max(panel_height, (label.count('\n') + 1) * line_mm + 1.) for label in labels]
    visible_outlines = options.get('show_outlines', True)
    outline_kinds = (
        ('Cell outline', '#22d3ee', options.get('show_cell_outlines', True)),
        ('Nuclear outline', '#ffe052', options.get('show_nuclear_outlines', True)),
    )
    handles = [Line2D([], [], color=color, linewidth=1.3, label=label)
               for kind, (label, color, visible) in enumerate(outline_kinds)
               if visible_outlines and visible and any(row[kind] for row in data.outlines)]
    missing = any(not np.isfinite(crop).all() for crop in data.crops)
    if missing:
        handles.append(Patch(facecolor='#dadde1', edgecolor='none', label='Unavailable pixels'))
    footer = 12. if width == 89 and handles else 7.
    height = header + sum(row_heights) + row_gap * (len(data.cell_ids) - 1) + footer
    figure = Figure(figsize=(width / 25.4, height / 25.4), facecolor='white')
    palette = colormaps['gray'].with_extremes(bad='#dadde1')
    grid_left = margin + gutter
    scale_target = field_width / 4
    magnitude = 10 ** np.floor(np.log10(scale_target))
    scale = max(value * magnitude for value in (1., 2., 5.) if value * magnitude <= scale_target)
    stroke = [patheffects.withStroke(linewidth=2.5, foreground='black')]
    top = height - header

    for row, row_height in enumerate(row_heights):
        bottom = top - (row_height + panel_height) / 2
        figure.text(margin / width, (top - row_height / 2) / height, str(row + 1),
                    fontsize=7.5, color='#66757c', va='center', parse_math=False)
        figure.text((grid_left - 1.8) / width, (top - row_height / 2) / height, labels[row],
                    fontsize=7.5, color='#26343b', ha='right', va='center', parse_math=False)
        rgb_channels = []
        if coloured or merged:
            for col, channel in enumerate(data.channels):
                low, high = options['limits'][str(channel)]
                color = options['channel_colors'][str(channel)].lstrip('#')
                rgb = np.array([int(color[i:i + 2], 16) for i in (0, 2, 4)]) / 255
                rgb_channels.append(np.clip((data.crops[row][col] - low) / (high - low), 0, 1)[..., None] * rgb)
        for col in range(columns):
            is_merged = col == len(data.channels)
            left = grid_left + col * (panel_width + gap) + (1.2 if is_merged else 0.)
            axis = figure.add_axes((left / width, bottom / height, panel_width / width, panel_height / height))
            if is_merged:
                panel = np.clip(np.sum(rgb_channels, axis=0), 0, 1)
                valid = np.all(np.isfinite(data.crops[row]), axis=0)
            else:
                channel = data.channels[col]
                low, high = options['limits'][str(channel)]
                panel = rgb_channels[col].copy() if coloured else data.crops[row][col]
                valid = np.isfinite(data.crops[row][col])
            if is_merged or coloured:
                panel[~valid] = np.array([218, 221, 225]) / 255
                axis.imshow(panel, extent=data.extents[row], interpolation='nearest')
            else:
                axis.imshow(panel, cmap=palette, vmin=low, vmax=high,
                            extent=data.extents[row], interpolation='nearest')
            if visible_outlines:
                for rings, (_label, color, visible) in zip(data.outlines[row], outline_kinds):
                    if visible:
                        for ring in rings:
                            axis.plot(ring[:, 0], ring[:, 1], color=color, linewidth=.65)
            axis.set_xlim(-field_width / 2, field_width / 2)
            axis.set_ylim(field_height / 2, -field_height / 2)
            axis.set_xticks([])
            axis.set_yticks([])
            for spine in axis.spines.values():
                spine.set_color('#8a9499')
                spine.set_linewidth(.35)
            if row == 0:
                figure.text((left + panel_width / 2) / width, (height - 1.5) / height, headings[col],
                            fontsize=7.5, ha='center', va='top', color='#26343b', parse_math=False)
                if not is_merged and (coloured or merged):
                    key_width = min(6., panel_width / 4)
                    figure.add_artist(Rectangle(((left + (panel_width - key_width) / 2) / width,
                                                 (height - header + 1.) / height),
                                                key_width / width, .65 / height,
                                                transform=figure.transFigure, facecolor=options['channel_colors'][str(channel)],
                                                edgecolor='#647178', linewidth=.25))
            if col == columns - 1:
                x = .92 - scale / field_width
                axis.plot([x, .92], [.08, .08], transform=axis.transAxes, color='white',
                          linewidth=1.4, solid_capstyle='butt', path_effects=stroke)
                axis.text((x + .92) / 2, .12, f'{scale:g} µm', transform=axis.transAxes,
                          color='white', fontsize=7, ha='center', va='bottom',
                          bbox={'facecolor': 'black', 'alpha': .65, 'edgecolor': 'none', 'pad': .6})
        top -= row_height + row_gap

    if handles:
        figure.legend(handles=handles, loc='lower left', bbox_to_anchor=(margin / width, 1.5 / height),
                      bbox_transform=figure.transFigure, ncol=len(handles), frameon=False,
                      fontsize=7, borderaxespad=0, handlelength=1.4, handletextpad=.5, columnspacing=1.3)
    field_label = f'{field_width:g} × {field_height:g} µm fields'
    figure.text((width - margin) / width, (7.5 if width == 89 and handles else 2.) / height,
                field_label, fontsize=7, ha='right', va='bottom', color='#52616a', parse_math=False)
    apply_figure_font(figure)
    return figure


def cell_gallery_figure(data: GalleryData, options: Mapping[str, Any], *, compact=False, publication=False):
    """Draw calibrated crop panels and export their existing arrays and display choices.

    Shared channel ranges affect display only; coloured channels add in RGB. PNG/PDF/SVG metadata
    retain full IDs; CSV records selection and display settings, and NPZ stores
    numeric pixels and drawn outlines with their physical coordinates. Rendering
    and export perform no cell selection or image reads.
    """
    if publication:
        return _publication_gallery_figure(data, options)
    from matplotlib.figure import Figure
    from matplotlib import colormaps

    rows = len(data.cell_ids)
    merged = options.get('merged_panel', False)
    coloured = options.get('channel_display', 'grayscale') == 'colour'
    columns = len(data.channels) + int(merged)
    field_width = data.crop_um * float(data.selection.get('field_aspect', 1.))
    field_height = data.crop_um
    suffix = 8
    while len({id[-suffix:] for id in data.cell_ids}) < rows:
        suffix += 1
    legend = (f'{field_width:g} × {field_height:g} µm fields' if field_width != field_height
              else f'{data.crop_um:g} µm crops')
    if options.get('show_outlines', True):
        if options.get('show_cell_outlines', True):
            legend += ' · Cyan: cell outline'
        if options.get('show_nuclear_outlines', True):
            legend += ' · Yellow: nuclear outline'
    if merged and not compact:
        legend += ' · Merge: additive RGB, clipped at display maximum'
    if any(not np.isfinite(crop).all() for crop in data.crops):
        legend += ' · Gray: outside image or unavailable pixels'
    figure = Figure(figsize=(1.8 + 1.55 * columns, 1.05 + 1.55 * rows))
    axes = figure.subplots(rows, columns, squeeze=False)
    figure.subplots_adjust(left=1.7 / figure.get_figwidth(), right=.99,
                           top=1 - .45 / figure.get_figheight(), bottom=.055,
                           hspace=.12, wspace=.07)
    if compact:
        figure.subplots_adjust(left=.02, right=.98, top=.88, bottom=.13, wspace=.07)
    palette = colormaps['gray'].with_extremes(bad='#dadde1')
    for row, id in enumerate(data.cell_ids):
        rgb_channels = []
        if coloured or merged:
            for col, channel in enumerate(data.channels):
                low, high = options['limits'][str(channel)]
                pixels = data.crops[row][col]
                color = options['channel_colors'][str(channel)].lstrip('#')
                rgb = np.array([int(color[i:i+2], 16) for i in (0, 2, 4)]) / 255
                rgb_channels.append(np.clip((pixels - low) / (high - low), 0, 1)[..., None] * rgb)
        for col in range(columns):
            axis = axes[row, col]
            is_merged = col == len(data.channels)
            if is_merged:
                panel = np.clip(np.sum(rgb_channels, axis=0), 0, 1)
                valid = np.all(np.isfinite(data.crops[row]), axis=0)
            else:
                channel = data.channels[col]
                low, high = options['limits'][str(channel)]
                panel = rgb_channels[col].copy() if coloured else data.crops[row][col]
                valid = np.isfinite(data.crops[row][col])
            if is_merged or coloured:
                panel[~valid] = np.array([218, 221, 225]) / 255
                axis.imshow(panel, extent=data.extents[row], interpolation='nearest')
            else:
                axis.imshow(panel, cmap=palette, vmin=low, vmax=high,
                            extent=data.extents[row], interpolation='nearest')
            if options.get('show_outlines', True):
                for rings, color, visible in zip(data.outlines[row], ('#22d3ee', '#ffe052'),
                        (options.get('show_cell_outlines', True), options.get('show_nuclear_outlines', True))):
                    if not visible:
                        continue
                    for ring in rings:
                        axis.plot(ring[:, 0], ring[:, 1], color=color, linewidth=.8)
            half_width, half_height = field_width / 2, field_height / 2
            axis.set_xlim(-half_width, half_width)
            axis.set_ylim(half_height, -half_height)
            axis.set_xticks([]); axis.set_yticks([])
            if row == 0:
                title = ('Merged colour\nSelected channels' if is_merged else
                         f'{data.channel_labels[col]}\nDisplay: {low:.3g}–{high:.3g}')
                if compact:
                    title = 'Merge' if is_merged else data.channel_labels[col]
                axis.set_title(title, fontsize=9, parse_math=False)
                if not is_merged and (coloured or merged):
                    from matplotlib.patches import Rectangle
                    axis.add_patch(Rectangle((0, 1.025), .1, .025, transform=axis.transAxes,
                        facecolor=options['channel_colors'][str(channel)], edgecolor='#374151',
                        linewidth=.5, clip_on=False))
            if col == 0 and not compact:
                label = '\n'.join(textwrap.fill(line, 25) for line in data.labels[row].splitlines())
                if not compact:
                    label += '\nCell ' + id[-suffix:]
                axis.set_ylabel(label, rotation=0, ha='right', va='center', fontsize=8, labelpad=12, parse_math=False)
            if col == columns - 1:
                valid_y, valid_x = np.where(valid)
                extent = data.extents[row]
                if valid_x.size:
                    pixel_x = (extent[1] - extent[0]) / data.crops[row].shape[2]
                    pixel_y = (extent[2] - extent[3]) / data.crops[row].shape[1]
                    left = max(-half_width, extent[0] + valid_x.min() * pixel_x)
                    right = min(half_width, extent[0] + (valid_x.max() + 1) * pixel_x)
                    bottom = min(half_height, extent[3] + (valid_y.max() + 1) * pixel_y)
                    bar = min(field_width / 4, (right - left) * .7)
                    x, y = left + .03 * field_width, bottom - .07 * field_height
                else:
                    continue
                axis.plot([x, x + bar], [y, y], color='black', linewidth=4, solid_capstyle='butt')
                axis.plot([x, x + bar], [y, y], color='white', linewidth=2, solid_capstyle='butt')
                axis.text(x, y - .03 * data.crop_um, f'{bar:g} µm', color='white', fontsize=8,
                          bbox={'facecolor': 'black', 'alpha': .6, 'edgecolor': 'none', 'pad': 1})
    figure.text(.01, .013, legend, fontsize=8)
    return figure


def render_cell_gallery(data: GalleryData, options: Mapping[str, Any], *, data_only=False, publication=False) -> tuple[tuple[str, str, str, bytes], ...]:
    """Export the same calibrated figure used for interactive cell inspection."""
    from .result_plots import _exports, _csv_output
    figure = None if data_only else cell_gallery_figure(data, options, compact=options.get('compact', False), publication=publication)
    merged = options.get('merged_panel', False)
    description = json.dumps({'cell_ids': data.cell_ids, ('phenotypes' if options.get('label_field', 'phenotype') == 'phenotype' else 'review_labels'): data.labels,
        'channels': data.channel_labels, 'display_options': options, 'source_frame': data.frame,
        'pyramid_level': data.pyramid_level, 'pixel_size_um': data.pixel_size_um,
        'selection': 'explicit IDs' if options.get('cell_ids') else 'seeded sample'}, sort_keys=True)
    figure_description = description
    if publication:
        public_metadata = json.loads(description)
        public_metadata['display_options'].pop('image_uri', None)
        figure_description = json.dumps(public_metadata, sort_keys=True)
    figures = () if data_only else _exports(figure, 'cell-gallery', 'Selected cell image gallery', description=figure_description)
    table = _csv_output('cell-gallery', 'Cell gallery selection and display data',
        ('cell_id', options.get('label_field', 'phenotype'), 'channel_index', 'channel_name', 'display_min', 'display_max',
         'crop_width_um', 'crop_height_um', 'center_x_um', 'center_y_um', 'pyramid_level',
         'pixel_size_x_um', 'pixel_size_y_um', 'image_id', 'image_sha256', 'selection_method', 'selection_seed',
         'channel_display', 'merged_panel', 'channel_color', 'matching_population_count'),
        ((id, data.labels[row], channel, data.channel_labels[col], *options['limits'][str(channel)],
          data.crop_um * float(data.selection.get('field_aspect', 1.)), data.crop_um,
          *data.centers_um[row], data.pyramid_level, *data.pixel_size_um,
          data.frame['image_id'], data.frame['image_sha256'],
          'explicit IDs' if options.get('cell_ids') else 'seeded sample',
          None if options.get('cell_ids') else options.get('seed', 0),
          options.get('channel_display', 'grayscale'), merged, options['channel_colors'][str(channel)],
          options.get('selection_count'))
         for row, id in enumerate(data.cell_ids) for col, channel in enumerate(data.channels)))
    if options.get("include_pixels", True) is False:
        return (*figures, table)
    from io import BytesIO
    vertices, offsets, cells, kinds = [], [0], [], []
    for row, outlines in enumerate(data.outlines):
        for kind, rings in zip(('cell', 'nucleus'), outlines):
            for ring in rings:
                vertices.append(ring); offsets.append(offsets[-1] + len(ring)); cells.append(row); kinds.append(kind)
    metadata = json.loads(description)
    metadata.update(pixel_axes='cell,channel,y,x', extents_axes='left,right,bottom,top',
                    crop_coordinates='micrometres relative to each cell centre',
                    center_coordinates='canonical physical image coordinates in micrometres',
                    pixel_values='recorded image values at the selected pyramid level',
                    outline_encoding='drawn rings with offsets, cell indices and cell/nucleus kind',
                    missing_pixels='NaN')
    archive = BytesIO()
    np.savez_compressed(archive, pixels=np.stack(data.crops), cell_ids=np.asarray(data.cell_ids),
        channel_indices=np.asarray(data.channels, dtype=np.int64), channel_names=np.asarray(data.channel_labels),
        centers_um=np.asarray(data.centers_um), extents_um=np.asarray(data.extents),
        outline_vertices_um=np.concatenate(vertices) if vertices else np.empty((0, 2)),
        outline_offsets=np.asarray(offsets, dtype=np.int64), outline_cell_indices=np.asarray(cells, dtype=np.int64),
        outline_kinds=np.asarray(kinds, dtype='U7'), metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)))
    return (*figures,
            table, ('cell-gallery/npz', 'Cell gallery pixels and outlines', 'npz', archive.getvalue()))
