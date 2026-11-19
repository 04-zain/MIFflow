"""Draw reference spectra with explicit acquisition groups and separate legends."""

from matplotlib.figure import Figure
from matplotlib.collections import LineCollection
from matplotlib.transforms import ScaledTranslation
from math import ceil
import re
import numpy as np


class _SpectralBracket(LineCollection):
    """Keep short endcaps the same physical height after layout and resizing."""

    def draw(self, renderer):
        cap = renderer.points_to_pixels(3) / max(self.axes.bbox.height, 1)
        self.set_segments([
            [(span[0][0], cap), (span[1][0], 0),
             (span[2][0], 0), (span[3][0], cap)]
            for span in self.get_segments()
        ])
        super().draw(renderer)


def spectral_axis(measurement_names, *, wavelengths=None, wavelength_unit=None, optical_settings=None):
    """Return recorded wavelengths and optical-setting groups, or band indices.

    Structured wavelengths and setting identities take precedence over display labels.
    Labels supply contiguous optical-setting groups only when their wavelengths
    agree with the metadata, or supply missing wavelengths when all known values
    agree. Native IM3 labels round to six significant digits. Unknown or invalid
    coordinates use acquired-band indices; marker names never supply wavelengths.
    """
    names = tuple(measurement_names)
    ordinal = np.arange(1, len(names) + 1, dtype=float), (), None
    matches = [re.fullmatch(r"(.+?):\s*((?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\s*nm", name)
               if isinstance(name, str) else None for name in names]
    labelled = None
    if names and all(match and match.group(1).strip() for match in matches):
        candidate = np.asarray([float(match.group(2)) for match in matches])
        if np.all(np.isfinite(candidate) & (candidate > 0)):
            labelled = candidate
    x, unit = labelled, "nm"
    if wavelengths is not None:
        try:
            recorded = np.asarray(wavelengths, dtype=float)
        except (TypeError, ValueError, OverflowError):
            return ordinal
        if recorded.shape != (len(names),):
            return ordinal
        known = np.asarray([value is not None for value in wavelengths], dtype=bool)
        if np.any(known):
            if (not isinstance(wavelength_unit, str) or not wavelength_unit.strip()
                    or not np.all(np.isfinite(recorded[known]) & (recorded[known] > 0))):
                return ordinal
            agrees = (labelled is not None and wavelength_unit == "nm"
                      and all(label in (value, float(f"{value:g}"))
                              for label, value in zip(labelled[known], recorded[known])))
            if np.all(known):
                x, unit = recorded, wavelength_unit
                if not agrees:
                    labelled = None
            elif not agrees:
                return ordinal
            else:
                x = labelled.copy()
                x[known] = recorded[known]
    if x is None:
        return ordinal
    if optical_settings is not None:
        # Service summaries use mappings; saved axes use typed records. IDs, not
        # captions, distinguish paths. Unknown rows never disappear from curves.
        try:
            if len(optical_settings) != len(names):
                return x, (), unit
            identities = []
            for index, setting in enumerate(optical_settings):
                if setting is None:
                    identities.append((None, "Unrecorded setting"))
                    continue
                if hasattr(setting, "setting_id"):
                    ident, label = setting.setting_id, setting.label
                else:
                    setting = dict(setting)
                    ident, label = setting["setting_id"], setting["label"]
                if not all(isinstance(v, str) and v.strip() for v in (ident, label)):
                    return x, (), unit
                identities.append((ident, label))
        except (TypeError, ValueError, KeyError):
            return x, (), unit
        if not any(ident is not None for ident, _label in identities):
            return x, (), unit
        groups, previous = [], None
        for index, (ident, label) in enumerate(identities):
            if not groups or ident is None or ident != previous:
                groups.append((label, [], []))
            groups[-1][1].append(index)
            groups[-1][2].append(float(x[index]))
            previous = ident
        return x, tuple(groups), unit
    if labelled is None:
        return x, (), unit
    groups = []
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        if not groups or groups[-1][0] != name:
            groups.append((name, [], []))
        groups[-1][1].append(index)
        groups[-1][2].append(float(x[index]))
    return x, tuple(groups), unit


def plot_spectral_profile(axes, x, values, groups, *, ordinal=False, **style):
    """Connect original values within each recorded setting, using one legend item.

    With ordinal=True, connect all values on one-based acquired-band positions.
    Otherwise NaN separators break the display line between optical settings. The source
    arrays and their order are unchanged. Without setting labels, non-increasing
    wavelengths are shown as individual points rather than guessing connections.
    """
    if ordinal:
        return axes.plot(np.arange(1, len(values) + 1), values, **style)[0]
    if groups:
        positions, plotted = [], []
        for _name, indices, _wavelengths in groups:
            if positions:
                positions.append(np.nan); plotted.append(np.nan)
            positions.extend(x[index] for index in indices)
            plotted.extend(values[index] for index in indices)
        return axes.plot(positions, plotted, **style)[0]
    if len(x) > 1 and np.any(np.diff(x) <= 0):
        style = {**style, "linestyle": "None", "marker": style.get("marker", "o")}
    return axes.plot(x, values, **style)[0]


def label_spectral_axis(axes, names, x, groups, wavelength_unit=None, *, ordinal=False):
    """Label wavelength positions and lanes spanning each setting's recorded values.

    ordinal=True uses one row of brackets under the acquired-band spans instead
    of wavelength lanes. Both show recorded minima/maxima, not filter passband
    widths. They may overlap horizontally. On zoom, only the visible part remains;
    setting names follow that part. Unknown wavelengths retain acquired-band ticks.
    """
    if ordinal:
        ticks = np.unique(np.linspace(1, len(names), min(8, len(names)), dtype=int))
        axes.set_xticks(ticks, [str(value) for value in ticks])
        axes.set_xlim(.5, len(names) + .5)
        axes.set_xlabel("Acquired band", labelpad=8)
        if groups:
            transform = axes.get_xaxis_transform() + ScaledTranslation(
                0, -47 / 72, axes.figure.dpi_scale_trans)
            brackets = []
            for name, indices, wavelengths in groups:
                left, right = indices[0] + .6, indices[-1] + 1.4
                bracket = _SpectralBracket([], colors="#8b9da9", linewidths=.8,
                    transform=transform, clip_on=False)
                axes.add_collection(bracket, autolim=False)
                caption = axes.text((left + right) / 2, 0,
                    f"{name}\n{min(wavelengths):g}–{max(wavelengths):g} {wavelength_unit}",
                    transform=transform + ScaledTranslation(0, -5 / 72, axes.figure.dpi_scale_trans),
                    ha="center", va="top", fontsize=9, color="#344957", clip_on=False)
                brackets.append((bracket, caption, left, right))

            def update_brackets(current):
                lower, upper = sorted(current.get_xlim())
                for bracket, caption, first, last in brackets:
                    left, right = max(lower, first), min(upper, last)
                    visible = left <= right
                    bracket.set_visible(visible); caption.set_visible(visible)
                    if visible:
                        bracket.set_segments([[(left, 0), (left, 0), (right, 0), (right, 0)]])
                        caption.set_x((left + right) / 2)

            axes.callbacks.connect("xlim_changed", update_brackets)
            update_brackets(axes)
        elif wavelength_unit:
            axes.text(.5, 0,
                f"Recorded detection wavelengths: {min(x):g}–{max(x):g} {wavelength_unit}",
                transform=axes.transAxes + ScaledTranslation(0, -47 / 72, axes.figure.dpi_scale_trans),
                ha="center", va="top", fontsize=9, color="#344957", clip_on=False)
        return
    if wavelength_unit:
        lower, upper = float(min(x)), float(max(x))
        margin = max((upper - lower) * .025, 1)
        axes.set_xlim(lower - margin, upper + margin)
        axes.set_xlabel(f"Detection wavelength ({wavelength_unit})", labelpad=10)
    else:
        ticks = np.unique(np.linspace(1, len(names), min(8, len(names)), dtype=int))
        axes.set_xticks(ticks, [str(value) for value in ticks])
        axes.set_xlim(.5, len(names) + .5)
        axes.set_xlabel("Acquired band", labelpad=10)
    if not groups:
        return
    axes.text(0, 0, "Recorded wavelengths by optical setting",
        transform=axes.transAxes + ScaledTranslation(0, -43 / 72, axes.figure.dpi_scale_trans),
        ha="left", va="center", fontsize=9, color="#526971", clip_on=False)
    lanes = []
    for ordinal, (name, indices, _wavelengths) in enumerate(groups):
        first, last = min(float(x[index]) for index in indices), max(float(x[index]) for index in indices)
        transform = axes.get_xaxis_transform() + ScaledTranslation(
            0, -(55 + 13 * ordinal) / 72, axes.figure.dpi_scale_trans)
        lane = LineCollection([[(first, 0), (last, 0)]], colors="#8b9da9",
                              linewidths=1.1, transform=transform, clip_on=False)
        axes.add_collection(lane, autolim=False)
        caption = axes.text((first + last) / 2, 0, name, transform=transform,
            ha="center", va="center", fontsize=9, color="#344957", clip_on=False,
            bbox={"facecolor": axes.figure.get_facecolor(), "edgecolor": "none", "pad": 1.5})
        lanes.append((lane, caption, first, last))

    def update_lanes(current):
        lower, upper = sorted(current.get_xlim())
        for lane, caption, first, last in lanes:
            left, right = max(lower, first), min(upper, last)
            visible = left <= right
            lane.set_visible(visible); caption.set_visible(visible)
            if visible:
                lane.set_segments([[(left, 0), (right, 0)]])
                caption.set_x((left + right) / 2)

    axes.callbacks.connect("xlim_changed", update_lanes)
    update_lanes(axes)


def build_spectral_figure(profiles, *, measurement_names, wavelength_unit=None, optical_settings=None,
                          xlim=None, ylim=None):
    """Draw supplied full-reference fractions and reserve a lower row for the legend.

    Each profile supplies band X/Y values, component name and optional colour.
    Display positions use recorded detection wavelengths when available, with
    separate line segments for each optical setting and recorded-range lanes.
    Values are never normalized per setting. Limits retain the current review view. The independent legend row cannot overlap the data axes.
    """
    profiles = tuple(profiles)
    if not profiles:
        raise ValueError("Select at least one reference spectrum to export.")
    x, groups, unit = spectral_axis(measurement_names,
        wavelengths=profiles[0][0] if wavelength_unit else None, wavelength_unit=wavelength_unit,
        optical_settings=optical_settings)
    columns = min(4, len(profiles))
    rows = ceil(len(profiles) / columns)
    figure = Figure(figsize=(9, (5.4 if groups else 4.3) + .28 * rows), layout="constrained", facecolor="white")
    grid = figure.add_gridspec(2, 1, height_ratios=(4, .35 * rows))
    axes = figure.add_subplot(grid[0]); legend_axes = figure.add_subplot(grid[1]); legend_axes.axis("off")
    lines = []
    for supplied_x, y, name, colour in profiles:
        lines.append(plot_spectral_profile(axes, x, y, groups,
            color=colour, marker="o", markersize=4, linewidth=1.7, label=name))
    label_spectral_axis(axes, measurement_names, x, groups, unit)
    axes.set_ylabel("Fraction of total reference signal", fontsize=11)
    axes.tick_params(axis="y", labelsize=10)
    if xlim is not None:
        axes.set_xlim(xlim)
    axes.set_ylim(ylim if ylim is not None else (0, None))
    axes.spines[["top", "right"]].set_visible(False)
    axes.set_axisbelow(True)
    axes.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
    legend_axes.legend(lines, [line.get_label() for line in lines], frameon=False,
        ncol=columns, fontsize=10, loc="center", handlelength=2.2, columnspacing=2, labelspacing=.9)
    from mif_pipeline.cell_analysis.figure_style import apply_figure_font
    apply_figure_font(figure)
    return figure
