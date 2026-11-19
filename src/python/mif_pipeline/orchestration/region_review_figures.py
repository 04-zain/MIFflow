"""Draw saved tissue and analysis-region outlines with their measured areas.

Review and Results use the same calibrated polygons. We preserve interior holes
and export the saved measurements directly, without reading image pixels.
"""
from __future__ import annotations

import numpy as np


def _polygons(geometry):
    return ([geometry['coordinates']] if geometry['type'] == 'Polygon'
            else geometry['coordinates'])


def _label_position(polygon):
    """Place a display label within the widest interior interval near mid-height."""
    exterior = np.asarray(polygon[0], dtype=float)
    # A scan between vertex heights avoids ambiguous horizontal boundary hits.
    heights = np.unique(exterior[:, 1])
    middle = (heights[0] + heights[-1]) / 2
    index = min(range(len(heights) - 1),
                key=lambda i: abs((heights[i] + heights[i + 1]) / 2 - middle))
    y = (heights[index] + heights[index + 1]) / 2
    crossings = []
    for ring in polygon:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
            if (y0 <= y < y1) or (y1 <= y < y0):
                crossings.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    crossings.sort()
    left, right = max(zip(crossings[::2], crossings[1::2]), key=lambda pair: pair[1] - pair[0])
    return (left + right) / 2, y


def _outline_path(polygon, scale):
    """Orient exterior and interior rings oppositely for hole-preserving fills."""
    from matplotlib.path import Path

    vertices, codes = [], []
    for index, ring in enumerate(polygon):
        points = np.asarray(ring, dtype=float) * scale
        signed_area = np.sum(points[:-1, 0] * points[1:, 1] - points[1:, 0] * points[:-1, 1])
        if (signed_area > 0) != (index == 0):
            points = points[::-1]
        vertices.extend(points)
        codes.extend([Path.MOVETO, *([Path.LINETO] * (len(points) - 2)), Path.CLOSEPOLY])
    return Path(vertices, codes)


def region_overview_figure(review, *, palette="teal"):
    """Show numbered regions at their saved physical coordinates, including holes."""
    from matplotlib.figure import Figure
    from matplotlib.patches import PathPatch, Patch
    from mif_pipeline.cell_analysis.figure_style import apply_figure_font, outline_palette

    colors = outline_palette(palette)
    scale = np.asarray([review['x_um_per_px'], review['y_um_per_px']], dtype=float)
    figure = Figure(figsize=(6.3, 4.8), layout='constrained')
    axes = figure.subplots()
    regions = review['regions']
    is_domain = review['kind'] != 'tissue'
    ordered = sorted(regions, key=lambda region: region['layer'] != 'tissue')
    extents = []
    for region in ordered:
        background = is_domain and region['layer'] == 'tissue'
        excluded = region.get('included') is False
        polygons = _polygons(region['geometry'])
        for polygon in polygons:
            path = _outline_path(polygon, scale)
            axes.add_patch(PathPatch(path,
                facecolor=colors['tissue_fill'] if background or excluded else colors['region_fill'],
                edgecolor=colors['tissue_edge'] if background or excluded else colors['region_edge'],
                linestyle='--' if excluded else '-',
                linewidth=.6 if background else .85,
                zorder=1 if background else 2))
            extents.append(np.asarray(polygon[0]) * scale)
        if not background and region.get('display_number') is not None:
            polygon = max(polygons, key=lambda p: abs(np.sum(
                np.asarray(p[0])[:-1, 0] * np.asarray(p[0])[1:, 1] -
                np.asarray(p[0])[1:, 0] * np.asarray(p[0])[:-1, 1])))
            x, y = np.asarray(_label_position(polygon)) * scale
            axes.text(x, y, str(region['display_number']), ha='center', va='center',
                      color=colors['region_edge'], fontsize=8, zorder=4,
                      bbox=dict(facecolor='white', edgecolor='none', alpha=.85, pad=1.2))
    for overlap in review.get('overlaps', ()):
        if overlap.get('geometry'):
            for polygon in _polygons(overlap['geometry']):
                axes.add_patch(PathPatch(_outline_path(polygon, scale),
                    facecolor=colors['overlap_fill'], edgecolor=colors['overlap_edge'], linewidth=.6, alpha=.7, zorder=3))
    if extents:
        points = np.concatenate(extents)
        low, high = points.min(axis=0), points.max(axis=0)
        margin = max(float(np.max(high - low)) * .04, 1.)
        axes.set_xlim(low[0] - margin, high[0] + margin)
        axes.set_ylim(high[1] + margin, low[1] - margin)
    axes.set_aspect('equal', adjustable='box')
    axes.set_xlabel('X (µm)', fontsize=9)
    axes.set_ylabel('Y (µm)', fontsize=9)
    axes.tick_params(labelsize=8, width=.6, length=3, colors='#34484a')
    axes.spines[['top', 'right']].set_visible(False)
    for spine in axes.spines.values():
        spine.set_linewidth(.6)
        spine.set_color('#869697')
    if is_domain:
        axes.legend(handles=[Patch(facecolor=colors['tissue_fill'], edgecolor=colors['tissue_edge'], label='Tissue section'),
                             Patch(facecolor=colors['region_fill'], edgecolor=colors['region_edge'], label='Analysis region')],
                    loc='upper center', bbox_to_anchor=(.5, 1.12), ncol=2, frameon=False, fontsize=8)
    elif any(region.get('included') is False for region in regions):
        axes.legend(handles=[Patch(facecolor=colors['region_fill'], edgecolor=colors['region_edge'], label='Included'),
                             Patch(facecolor=colors['tissue_fill'], edgecolor=colors['tissue_edge'], linestyle='--', label='Excluded')],
                    loc='upper center', bbox_to_anchor=(.5, 1.12), ncol=2, frameon=False, fontsize=8)
    apply_figure_font(figure)
    return figure


def region_review_outputs(review, *, palette="teal"):
    """Export one outline figure and exact identifiers, areas and dimensions."""
    from mif_pipeline.cell_analysis.result_plots import _exports

    figure = region_overview_figure(review, palette=palette)
    title = 'Tissue outlines' if review['kind'] == 'tissue' else 'Analysis-region outlines'
    identity = 'region-outlines'
    outputs = list(_exports(figure, identity, title, description="Outline palette: " + palette))
    outputs.append(region_measurements_csv(review))
    return tuple(outputs)


def region_measurements_csv(review):
    """Return the exact saved area and dimension table used by Review and Results."""
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    columns = ('slide_id', 'layer', 'region_id', 'section_id', 'parent_region_id',
               'display_number', 'display_label', 'area_um2', 'width_um', 'height_um', 'edited')
    if any('included' in region for region in review['regions']):
        columns += ('included',)
    return _csv_output('region-outlines', 'Region areas and dimensions', columns,
        ((review['slide_id'], *(region.get(key) for key in columns[1:])) for region in review['regions']))


def region_image_figure(review, image_path, metadata, *, viewport_um=None, selected_ids=(), show_outlines=True, label_ids=()):
    """Export the loaded overview with the same physical viewport and contour state.

    The PNG is already rendered with the chosen channels, colours and limits.
    Reading it preserves that appearance without another scientific image read.
    """
    from PIL import Image
    from matplotlib.figure import Figure
    from matplotlib.patches import PathPatch
    from mif_pipeline.cell_analysis.figure_style import apply_figure_font

    with Image.open(image_path) as source:
        pixels = np.asarray(source).copy()
    width, height = metadata['width_um'], metadata['height_um']
    viewport = viewport_um or [0., 0., width, height]
    x0, y0, x1, y1 = viewport
    figure = Figure(figsize=(6.3, max(2., 6.3*(y1-y0)/(x1-x0))), layout='constrained')
    axes = figure.subplots()
    axes.set_facecolor('#172329')
    axes.imshow(pixels, extent=(0, width, height, 0), interpolation='nearest',
                cmap='gray' if pixels.ndim == 2 else None, vmin=0, vmax=255)
    scale = np.asarray([review['x_um_per_px'], review['y_um_per_px']], dtype=float)
    if show_outlines:
        for row in review['regions']:
            selected = row['region_id'] in selected_ids
            excluded = row.get('included') is False
            color = '#ffd56a' if selected else '#8d999c' if excluded else '#32d4df' if row['layer'] == review['kind'].replace('analysis_domains', 'analysis') else '#a5afad'
            for polygon in _polygons(row['geometry']):
                axes.add_patch(PathPatch(_outline_path(polygon, scale), facecolor=(1., .835, .416, 18/255) if selected else 'none',
                                         edgecolor=color, linewidth=1.2 if selected else .6,
                                         linestyle='--' if excluded else '-', zorder=2))
            if row['region_id'] in label_ids and row.get('display_number') is not None:
                points = np.concatenate([np.asarray(p[0])*scale for p in _polygons(row['geometry'])])
                x, y = points.min(axis=0)
                axes.text(x, y, str(row['display_number']), color='#32d4df', fontsize=8,
                          ha='left', va='top', zorder=3, clip_on=True)
    axes.set_xlim(x0, x1); axes.set_ylim(y1, y0); axes.set_aspect('equal', adjustable='box')
    axes.set_xlabel('X (µm)'); axes.set_ylabel('Y (µm)')
    apply_figure_font(figure)
    return figure
