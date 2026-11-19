"""Consistent embedded typography for scientific figure exports."""
from pathlib import Path


def apply_figure_font(figure):
    """Use bundled Liberation Sans for existing figure labels without changing values.

    We retain each Text artist's size, alignment and formatting, replacing only
    its font file and declared family. Call after assembling the panels and
    legends, before drawing/exporting. Raster/PDF use the bundled font; editable
    SVG declares Liberation Sans with Arial and generic sans-serif fallbacks.
    The caller retains its layout, data, colour mapping and export formats.
    """
    from matplotlib.text import Text

    font_path = Path(__file__).resolve().parent / "data/fonts/LiberationSans-Regular.ttf"
    for text in figure.findobj(Text):
        properties = text.get_fontproperties().copy()
        properties.set_file(font_path)
        properties.set_family(["Liberation Sans", "Arial", "sans-serif"])
        text.set_fontproperties(properties)


# These colours encode outline roles, never marker channels or class identities.
OUTLINE_PALETTES = {
    'teal': ('Teal', '#d4e8e5', '#087f82', '#edb589', '#9f4a14'),
    'blue': ('Blue and orange', '#dce8f5', '#2468a2', '#f3c697', '#b86013'),
    'violet': ('Violet and green', '#e8e0f0', '#7752a1', '#c6e0bd', '#347340'),
    'gray': ('Grayscale', '#d1d1d1', '#3d3d3d', '#999999', '#171717'),
}

# Numerical series and sequential values only; image/class identities keep their colors.
RESULT_EXPORT_PALETTES = {
    'default': ('Default', '#146b78', '#e6a23c', 'viridis'),
    'blue': ('Blue', '#2468a2', '#b86013', 'Blues'),
    'violet': ('Violet', '#7752a1', '#347340', 'Purples'),
    'gray': ('Grayscale', '#3d3d3d', '#999999', 'Greys'),
}


def result_export_palette(name='default'):
    if not isinstance(name, str) or name not in RESULT_EXPORT_PALETTES:
        raise ValueError('Select an available figure export palette.')
    label, series, selection, sequential = RESULT_EXPORT_PALETTES[name]
    return dict(label=label, series=series, selection=selection, sequential=sequential)


def supports_result_export_palette(figure_id):
    return (figure_id.startswith(('distribution/', 'composition/', 'profiles/', 'compartments/')) or
            figure_id.startswith('coexpression/') and not figure_id.startswith('coexpression/rules/'))


def result_sequential_colormap(name='default'):
    """Ordered colours for exported means and percentages, with missing values grey."""
    from matplotlib import colormaps
    from matplotlib.colors import LinearSegmentedColormap

    colors = result_export_palette(name)
    palette = (LinearSegmentedColormap.from_list('result_magnitude', (
        '#f3f7f8', '#cddfe4', '#95bcc7', '#5e96a8', '#2f6d83', '#17475f'))
        if name == 'default' else colormaps[colors['sequential']].copy())
    palette.set_bad('#e5e7eb')
    return palette


def outline_palette(name='teal'):
    """Return named publication colours for exact tissue/region geometry."""
    if name not in OUTLINE_PALETTES:
        raise ValueError('Select an available outline palette.')
    label, fill, edge, overlap_fill, overlap_edge = OUTLINE_PALETTES[name]
    return dict(label=label, tissue_fill='#edf0f0', tissue_edge='#9ca9aa',
                region_fill=fill, region_edge=edge, overlap_fill=overlap_fill,
                overlap_edge=overlap_edge)
