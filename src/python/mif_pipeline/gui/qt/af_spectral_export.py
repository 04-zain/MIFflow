"""Export the visible AF curves with a real legend, separate from the live Qt view."""
from math import ceil
from textwrap import fill

import numpy as np
from matplotlib.figure import Figure
from PySide6.QtWidgets import QMessageBox

from mif_pipeline.cell_analysis.figure_style import apply_figure_font
from mif_pipeline.preprocessing.unmixing.spectral_plots import label_spectral_axis, spectral_axis
from .figure_export import export_figure


def build_af_export_figure(source_axes, *, measurement_names, wavelengths=None,
                           wavelength_unit=None, optical_settings=None, region_indices=None):
    """Copy visible display arrays and styling; never recompute or mutate evidence."""
    supplied = [line for line in source_axes.lines if line.get_visible()]
    if not supplied:
        raise ValueError('Choose at least one visible AF curve to export.')
    labels = [line.get_label() for line in supplied]
    columns = min(len(supplied), 3 if max(len(label) for label in labels) <= 35 else 2)
    captions = [fill(label, width=38 if columns == 2 else 32,
                     break_long_words=False, break_on_hyphens=False) for label in labels]
    legend_rows = ceil(len(supplied) / columns)
    legend_height = .28 * legend_rows * max(caption.count('\n') + 1 for caption in captions) + .1
    figure = Figure(figsize=(10, 4.4 + legend_height), layout='constrained', facecolor='white')
    grid = figure.add_gridspec(2, 1, height_ratios=(4.4, legend_height))
    axes = figure.add_subplot(grid[0], facecolor='white')
    legend_axes = figure.add_subplot(grid[1], facecolor='white')
    legend_axes.set_axis_off()
    lines = []
    region_colours = ('#0072b2', '#d55e00', '#009e73', '#cc79a7', '#e69f00', '#56b4e9', '#333333')
    region_dashes = ('--', '-.', ':')
    for original, label in zip(supplied, labels, strict=True):
        style = dict(color=original.get_color(), linestyle=original.get_linestyle(),
                     linewidth=original.get_linewidth(), alpha=original.get_alpha())
        index = (region_indices or {}).get(label)
        if index is not None:
            style.update(color=region_colours[index % len(region_colours)],
                linestyle=region_dashes[(index // len(region_colours)) % len(region_dashes)],
                linewidth=1.5, alpha=.9)
        line, = axes.plot(np.asarray(original.get_xdata()).copy(), np.asarray(original.get_ydata()).copy(),
            marker=original.get_marker(), markersize=original.get_markersize(),
            zorder=original.get_zorder(), label=label, **style)
        lines.append(line)
    x, groups, unit = spectral_axis(measurement_names, wavelengths=wavelengths,
                                   wavelength_unit=wavelength_unit, optical_settings=optical_settings)
    label_spectral_axis(axes, measurement_names, x, groups, unit if groups else None, ordinal=True)
    axes.set_xlabel(source_axes.get_xlabel())
    axes.set_ylabel(source_axes.get_ylabel())
    axes.set_xlim(source_axes.get_xlim())
    axes.set_ylim(source_axes.get_ylim())
    axes.spines[['top', 'right']].set_visible(False)
    axes.spines[['left', 'bottom']].set_color('#a7b8c3')
    axes.tick_params(color='#a7b8c3', labelcolor='#344957')
    axes.set_axisbelow(True)
    axes.grid(axis='y', color='#d5dfe5', linewidth=.7, linestyle=(0, (3, 4)))
    legend_axes.legend(lines, captions, frameon=False, ncol=columns, loc='center',
                       handlelength=3, columnspacing=2, labelspacing=.9)
    apply_figure_font(figure)
    return figure


def export_af_spectrum(parent, source_axes, *, review, default_name, description=None, region_indices=None):
    """Choose an output using an isolated figure; the review remains untouched."""
    try:
        figure = build_af_export_figure(source_axes, measurement_names=review['measurement_names'],
            wavelengths=review.get('measurement_wavelengths'), wavelength_unit=review.get('wavelength_unit'),
            optical_settings=review.get('measurement_optical_settings'), region_indices=region_indices)
    except ValueError as exc:
        QMessageBox.information(parent, 'AF spectrum export', str(exc))
        return None
    try:
        return export_figure(parent, figure, default_name, description=description)
    finally:
        figure.clear()
