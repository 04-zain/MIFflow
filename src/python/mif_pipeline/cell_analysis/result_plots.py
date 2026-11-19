"""Bounded counts and publication figures from selected saved cell columns."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import textwrap

import numpy as np

COMPOSITION_PAGE_SIZE = 24
PROFILE_PAGE_SIZE = 16
PROFILE_MARKERS_PER_FIGURE = 8


@dataclass(frozen=True)
class MeasurementDistribution:
    measurement: str
    units: str | None
    edges: tuple[float, ...]
    counts: tuple[int, ...]
    total: int
    missing: int
    nonfinite: int

    @property
    def finite(self):
        return sum(self.counts)


@dataclass(frozen=True)
class PhenotypeComposition:
    labels: tuple[str, ...]
    counts: tuple[int, ...]
    total: int


@dataclass(frozen=True)
class MeasurementInterval:
    lower: float
    upper: float
    count: int
    cell_ids: tuple[str, ...]
    values: tuple[float, ...]


def select_measurement_interval(dataset, measurement, lower, upper):
    """Count a closed finite interval; retain eight repeatable IDs and saved values."""
    lower, upper = float(lower), float(upper)
    if not np.isfinite([lower, upper]).all() or lower > upper:
        raise ValueError("Enter finite interval endpoints with minimum no greater than maximum.")
    column = "measurement:" + measurement
    presence = "present:" + column
    columns = ("cell_id", column, presence) if presence in dataset.schema.names else ("cell_id", column)
    count = 0
    keys, ids = np.array([], dtype=float), np.array([], dtype=object)
    examples = np.array([], dtype=float)
    random = np.random.default_rng(0)
    for batch in dataset.iter_batches(columns=columns):
        values = batch.column(column)
        numeric = values.to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
        selected = values.is_valid().to_numpy(zero_copy_only=False) & np.isfinite(numeric)
        if presence in columns:
            selected &= batch.column(presence).fill_null(False).to_numpy(zero_copy_only=False)
        selected &= (numeric >= lower) & (numeric <= upper)
        incoming = batch.column("cell_id").to_numpy(zero_copy_only=False)[selected]
        count += len(incoming)
        keys = np.concatenate((keys, random.random(len(incoming))))
        ids = np.concatenate((ids, incoming))
        examples = np.concatenate((examples, numeric[selected]))
        if len(keys) > 8:
            keep = np.argpartition(keys, 7)[:8]
            keys, ids, examples = keys[keep], ids[keep], examples[keep]
    order = np.argsort(keys)
    return MeasurementInterval(lower, upper, count, tuple(str(i) for i in ids[order]),
                               tuple(float(value) for value in examples[order]))


def _measurement_batches(dataset, measurement):
    column = "measurement:" + measurement
    presence = "present:" + column
    columns = (column, presence) if presence in dataset.schema.names else (column,)
    for batch in dataset.iter_batches(columns=columns):
        values = batch.column(column)
        available = values.is_valid().to_numpy(zero_copy_only=False)
        if presence in columns:
            available &= batch.column(presence).fill_null(False).to_numpy(zero_copy_only=False)
        numeric = values.to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
        finite = np.isfinite(numeric)
        yield numeric[available & finite], batch.num_rows, int((~available).sum()), int((available & ~finite).sum())


def count_measurement_distribution(dataset, measurement, units, *, bins=256):
    """Use two projected passes and fixed counters; preserve zero and negative values."""
    if type(bins) is not int or not 1 <= bins <= 512:
        raise ValueError("Select between one and 512 histogram bins.")
    total = missing = nonfinite = 0
    low, high = np.inf, -np.inf
    for values, rows, absent, invalid in _measurement_batches(dataset, measurement):
        total += rows
        missing += absent
        nonfinite += invalid
        if values.size:
            low = min(low, float(values.min()))
            high = max(high, float(values.max()))
    if total != dataset.row_count:
        raise ValueError("Saved measurement rows differ from the selected cell count.")
    if total == missing + nonfinite:
        return MeasurementDistribution(measurement, units, (), (), total, missing, nonfinite)
    if low == high:
        # A finite interval places a constant measurement at its centre.
        padding = max(abs(low) * .01, .5)
        low = max(-np.finfo(float).max, low - padding)
        high = min(np.finfo(float).max, high + padding)
    # Weighted endpoints avoid overflow in high-low for extreme signed values.
    positions = np.linspace(0., 1., bins + 1)
    edges = np.unique((1 - positions) * low + positions * high)
    counts = np.zeros(len(edges) - 1, dtype=np.int64)
    for values, _rows, _absent, _invalid in _measurement_batches(dataset, measurement):
        counts += np.histogram(values, bins=edges)[0]
    if int(counts.sum()) != total - missing - nonfinite:
        raise ValueError("Saved values changed between histogram passes.")
    return MeasurementDistribution(measurement, units, tuple(map(float, edges)),
                                   tuple(map(int, counts)), total, missing, nonfinite)


def count_measurement_cohort(sources, measurement, units, *, lower=None, upper=None, bins=256, plot_indices=None):
    """Two bounded projected passes with common edges and exact inclusive counts.

    Each source opens one accepted dataset. Only counters survive closing it;
    no sampled cell identities determine an image's membership or navigation.
    """
    if type(bins) is not int or not 1 <= bins <= 512:
        raise ValueError('Select between one and 512 histogram bins.')
    selected = set(range(len(sources))) if plot_indices is None else set(plot_indices)
    if not selected or any(type(index) is not int or not 0 <= index < len(sources) for index in selected):
        raise ValueError('Select an available image for the measurement plot.')
    rows = []
    low, high = np.inf, -np.inf
    for source in sources:
        total = missing = nonfinite = 0
        with source() as dataset:
            for values, count, absent, invalid in _measurement_batches(dataset, measurement):
                total += count
                missing += absent
                nonfinite += invalid
                if values.size:
                    low, high = min(low, float(values.min())), max(high, float(values.max()))
            if total != dataset.row_count:
                raise ValueError('Saved measurement rows differ from the selected cell count.')
        rows.append(dict(included_cells=total-missing-nonfinite, matching_cells=0,
                         missing_cells=missing, nonfinite_cells=nonfinite))
    usable = sum(row['included_cells'] for row in rows)
    plot_usable = sum(rows[index]['included_cells'] for index in selected)
    missing = sum(rows[index]['missing_cells'] for index in selected)
    nonfinite = sum(rows[index]['nonfinite_cells'] for index in selected)
    if not usable:
        return MeasurementDistribution(measurement, units, (), (), missing+nonfinite, missing, nonfinite), None, rows
    if low == high:
        padding = max(abs(low)*.01, .5)
        low, high = max(-np.finfo(float).max, low-padding), min(np.finfo(float).max, high+padding)
    positions = np.linspace(0., 1., bins+1)
    edges = np.unique((1-positions)*low + positions*high)
    lower, upper = float(edges[0] if lower is None else lower), float(edges[-1] if upper is None else upper)
    if not np.isfinite([lower, upper]).all() or lower > upper:
        raise ValueError('Enter finite interval endpoints with minimum no greater than maximum.')
    counts = np.zeros(len(edges)-1, dtype=np.int64)
    for index,(source, row) in enumerate(zip(sources, rows, strict=True)):
        observed = 0
        with source() as dataset:
            for values, _total, _missing, _nonfinite in _measurement_batches(dataset, measurement):
                observed += len(values)
                if index in selected:
                    counts += np.histogram(values, bins=edges)[0]
                row['matching_cells'] += int(np.count_nonzero((values >= lower) & (values <= upper)))
        if observed != row['included_cells']:
            raise ValueError('Saved values changed between histogram passes.')
    if int(counts.sum()) != plot_usable:
        raise ValueError('Saved values changed between histogram passes.')
    distribution = MeasurementDistribution(measurement, units, tuple(map(float, edges)),
        tuple(map(int, counts)), plot_usable+missing+nonfinite, missing, nonfinite)
    interval = MeasurementInterval(lower, upper, sum(rows[index]['matching_cells'] for index in selected), (), ())
    return distribution, interval, rows


def count_phenotype_composition(dataset):
    """Count every selected row, preserving declared zero categories and nulls."""
    dictionary = dataset.dictionaries.get("primary_phenotype")
    labels = () if dictionary is None else dictionary.labels
    counts = np.zeros(len(labels) + 1, dtype=np.int64)
    total = 0
    for batch in dataset.iter_batches(columns=("primary_phenotype",)):
        values = batch.column("primary_phenotype")
        total += batch.num_rows
        counts[-1] += values.null_count
        codes = values.drop_null().to_numpy(zero_copy_only=False)
        if codes.size:
            if (not np.issubdtype(codes.dtype, np.integer)
                    or codes.min() < 0 or codes.max() >= len(labels)):
                raise ValueError("Saved phenotype codes differ from the selected dictionary.")
            counts[:-1] += np.bincount(codes.astype(np.int64, copy=False), minlength=len(labels))
    if total != dataset.row_count:
        raise ValueError("Saved phenotype rows differ from the selected cell count.")
    return PhenotypeComposition((*labels, "Unavailable"), tuple(map(int, counts)), total)


def _exports(figure, identity, label, *, description=None):
    from matplotlib import rc_context
    from .figure_style import apply_figure_font

    apply_figure_font(figure)

    try:
        outputs = []
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            for fmt in ("png", "pdf", "svg"):
                output = BytesIO()
                metadata = ({"Subject" if fmt == "pdf" else "Description": description}
                            if description is not None else None)
                figure.savefig(output, format=fmt, dpi=300, facecolor="white", metadata=metadata)
                outputs.append((identity + "/" + fmt, label, fmt, output.getvalue()))
        return tuple(outputs)
    finally:
        figure.clear()


def _csv_output(identity, label, columns, rows):
    """Encode exact calculated figure values as one ordinary UTF-8 table."""
    import csv
    from io import StringIO
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return (identity + "/csv", label, "csv", stream.getvalue().encode("utf-8"))


def render_measurement_distribution(result, *, interval=None, region=None, phenotype=None, export_palette='default'):
    """Plot complete finite measurements with explicit availability counts and units."""
    from matplotlib.figure import Figure
    from .figure_style import result_export_palette
    colors = result_export_palette(export_palette)

    figure = Figure(figsize=(89 / 25.4, 68 / 25.4), layout="constrained")
    region_columns, region_values = (), ()
    measured_label = f"Measured: {result.finite:,} of {result.total:,} cells"
    if phenotype is not None:
        measured_label = textwrap.fill(phenotype + ' · ' + measured_label, 48)
        region_columns, region_values = ('phenotype',), (phenotype,)
    if region is not None:
        import json
        measured_label = textwrap.fill(region['label'] + ' · ' + measured_label, 48)
        region_columns += ('region_label', 'region_selection')
        region_values += (region['label'], json.dumps(region['selection'], sort_keys=True))
    axis = figure.subplots()
    if result.finite:
        axis.stairs(result.counts, result.edges, fill=True, color=colors['series'], alpha=.85,
                    label=measured_label)
    else:
        axis.plot([], [], color=colors['series'], label=measured_label)
        axis.set_xticks([])
        axis.set_yticks([0])
        axis.set_ylim(0, 1)
    unavailable = [text for count, text in (
        (result.missing, f"Missing: {result.missing:,}"),
        (result.nonfinite, f"Invalid: {result.nonfinite:,}")) if count]
    if unavailable:
        axis.plot([], [], linestyle="", label="; ".join(unavailable))
    if interval is not None:
        axis.axvspan(interval.lower, interval.upper, color=colors['selection'], alpha=.2,
                     label=f"Selected interval: {interval.count:,} cells")
    label = result.measurement + (f" ({result.units})" if result.units else "")
    axis.set_xlabel(textwrap.fill(label, 48), fontsize=7, parse_math=False)
    axis.set_ylabel("Cells", fontsize=7)
    axis.tick_params(labelsize=7, length=2, width=.5)
    axis.spines[["bottom", "left"]].set_linewidth(.5)
    axis.set_ylim(bottom=0)
    axis.spines[["top", "right"]].set_visible(False)
    # Leave room for editable SVG text when a viewer uses a wider fallback font.
    legend = axis.legend(loc="lower left", bbox_to_anchor=(0, 1.02), borderaxespad=0,
                         frameon=False, fontsize=7)
    for text in legend.get_texts():
        text.set_parse_math(False)
    rows = [("bin", result.measurement, result.units, left, right, count,
             result.total, result.missing, result.nonfinite)
            for left, right, count in zip(result.edges[:-1], result.edges[1:], result.counts)]
    if not rows:
        rows = [("summary", result.measurement, result.units, None, None, 0,
                 result.total, result.missing, result.nonfinite)]
    if interval is not None:
        rows.append(("closed_interval", result.measurement, result.units,
                     interval.lower, interval.upper, interval.count,
                     result.total, result.missing, result.nonfinite))
    data = _csv_output("measurement-distribution", "Measurement distribution data",
        ("row_type", "measurement", "units", "lower_edge", "upper_edge", "cell_count",
         "total_cells", "missing_cells", "invalid_cells", *region_columns),
        ((*row, *region_values) for row in rows))
    examples = ()
    if interval is not None and interval.cell_ids:
        examples = (_csv_output("measurement-examples", "Selected cell measurements",
            ("cell_id", "measurement", "units", "value", *region_columns),
            ((cell_id, result.measurement, result.units, value, *region_values)
             for cell_id, value in zip(interval.cell_ids, interval.values))),)
    return (*_exports(figure, "measurement-distribution", result.measurement), data, *examples)


def render_phenotype_composition(result, *, page=0, export_palette='default'):
    """Keep category pages readable while retaining a common complete-cell denominator."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
    from .figure_style import result_export_palette
    colors = result_export_palette(export_palette)

    declared_labels, declared_counts = result.labels[:-1], result.counts[:-1]
    if not declared_labels:
        declared_labels, declared_counts = result.labels, result.counts
    start = page * COMPOSITION_PAGE_SIZE
    stop = min(start + COMPOSITION_PAGE_SIZE, len(declared_labels))
    if type(page) is not int or page < 0 or start >= stop:
        raise ValueError("This phenotype-composition page is unavailable.")
    labels = declared_labels[start:stop]
    counts = declared_counts[start:stop]
    wrapped = [textwrap.fill(label, 28) for label in labels]
    height_pt = max(108, 17 * sum(label.count('\n') + 1 for label in wrapped) + 50)
    figure = Figure(figsize=(183 / 25.4, height_pt / 72), layout="constrained")
    # Editable SVG can use a wider sans-serif fallback than the embedded font.
    figure.get_layout_engine().set(w_pad=.16, h_pad=.045)
    axis = figure.subplots()
    positions = np.arange(len(labels))
    axis.barh(positions, counts, height=.65, color=colors['series'])
    largest = max(result.counts, default=0)
    for position, count in enumerate(counts):
        fraction = ("<0.1%" if 0 < count / result.total < .001 else f"{count / result.total:.1%}") if result.total else "undefined"
        axis.text(count + max(1, largest) * .02, position, f"{count:,} ({fraction})", va="center", fontsize=7)
    axis.set_yticks(positions, labels=wrapped, fontsize=7)
    for label in axis.get_yticklabels():
        label.set_parse_math(False)
    axis.invert_yaxis()
    axis.set_xlim(0, max(1, largest) * 1.38)
    axis.xaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    axis.tick_params(axis="x", labelsize=7)
    axis.set_xlabel("Cells", fontsize=7)
    denominator = (f"Percentages of all {result.total:,} exported cells" if result.total
                   else "Percentages unavailable: no exported cells")
    axis.plot([], [], linestyle="", label=denominator)
    if result.counts[-1] and result.labels[:-1]:
        axis.plot([], [], linestyle="", label=f"Unavailable assignments: {result.counts[-1]:,}")
    axis.legend(loc="lower left", bbox_to_anchor=(0, 1.01), frameon=False,
                fontsize=7, handlelength=0, handletextpad=0)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_linewidth(.5)
    axis.tick_params(axis="x", length=2, width=.5)
    axis.tick_params(axis="y", length=0, pad=9)
    data = _csv_output("phenotype-composition", "Phenotype composition data",
        ("label", "assignment_available", "cell_count", "total_cells", "fraction"),
        ((label, index < len(result.labels) - 1, count, result.total,
          count / result.total if result.total else None)
         for index, (label, count) in enumerate(zip(result.labels, result.counts))))
    return (*_exports(figure, "phenotype-composition", "Saved phenotype composition"), data)


@dataclass(frozen=True)
class MarkerProfiles:
    measurements: tuple[str, ...]
    units: str | None
    labels: tuple[str, ...]
    cell_counts: tuple[int, ...]
    usable_counts: tuple[tuple[int, ...], ...]
    means: tuple[tuple[float | None, ...], ...]


def count_marker_profiles(dataset, measurements, units):
    """Aggregate selected scalar columns in one bounded pass on their recorded scale."""
    measurements = tuple(measurements)
    if not measurements or len(set(measurements)) != len(measurements):
        raise ValueError("Select distinct saved measurements for the marker profile.")
    dictionary = dataset.dictionaries.get("primary_phenotype")
    labels = () if dictionary is None else dictionary.labels
    size = len(labels) + 1
    cells = np.zeros(size, dtype=np.int64)
    counts = np.zeros((size, len(measurements)), dtype=np.int64)
    sums = np.zeros(counts.shape, dtype=np.float64)
    columns = ["primary_phenotype"]
    for measurement in measurements:
        column = "measurement:" + measurement
        columns.append(column)
        if "present:" + column in dataset.schema.names:
            columns.append("present:" + column)
    for batch in dataset.iter_batches(columns=tuple(columns)):
        phenotype = batch.column("primary_phenotype")
        valid_codes = phenotype.drop_null().to_numpy(zero_copy_only=False)
        if valid_codes.size and (not np.issubdtype(valid_codes.dtype, np.integer)
                                 or valid_codes.min() < 0 or valid_codes.max() >= len(labels)):
            raise ValueError("Saved phenotype codes differ from the selected dictionary.")
        codes = np.full(batch.num_rows, len(labels), dtype=np.int64)
        codes[phenotype.is_valid().to_numpy(zero_copy_only=False)] = valid_codes
        cells += np.bincount(codes, minlength=size)
        for index, measurement in enumerate(measurements):
            column = "measurement:" + measurement
            values = batch.column(column)
            available = values.is_valid().to_numpy(zero_copy_only=False)
            if "present:" + column in columns:
                available &= batch.column("present:" + column).fill_null(False).to_numpy(zero_copy_only=False)
            numeric = values.to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
            available &= np.isfinite(numeric)
            counts[:, index] += np.bincount(codes[available], minlength=size)
            sums[:, index] += np.bincount(codes[available], weights=numeric[available], minlength=size)
    if int(cells.sum()) != dataset.row_count:
        raise ValueError("Saved profile rows differ from the selected cell count.")
    if not np.isfinite(sums).all():
        raise ValueError("Saved measurement totals exceed the supported numeric range.")
    means = tuple(tuple(float(sums[row, col] / counts[row, col]) if counts[row, col] else None
                        for col in range(len(measurements))) for row in range(size))
    return MarkerProfiles(measurements, units, (*labels, "Unavailable"), tuple(map(int, cells)),
                          tuple(tuple(map(int, row)) for row in counts), means)


def render_marker_profiles(result, *, page=0, export_palette='default'):
    """Display recorded arithmetic means at a deliberate 89 or 183 mm width."""
    from matplotlib.figure import Figure
    from matplotlib.colors import Normalize
    from matplotlib.ticker import MaxNLocator
    from .figure_style import result_sequential_colormap

    declared = len(result.labels) - 1
    pages = max(1, (declared + PROFILE_PAGE_SIZE - 1) // PROFILE_PAGE_SIZE)
    if type(page) is not int or not 0 <= page < pages:
        raise ValueError("This marker-profile page is unavailable.")
    rows = list(range(page * PROFILE_PAGE_SIZE, min((page + 1) * PROFILE_PAGE_SIZE, declared)))
    if page == pages - 1 and (result.cell_counts[-1] or not rows):
        rows.append(declared)
    matrix = np.array([[np.nan if value is None else value for value in row] for row in result.means])
    usable = matrix[np.isfinite(matrix)]
    low, high = (float(usable.min()), float(usable.max())) if usable.size else (0., 1.)
    if low == high:
        padding = max(abs(low) * .01, .5)
        low, high = low - padding, high + padding
    norm = Normalize(vmin=low, vmax=high)
    # Raw intensities have an ordered magnitude, with no meaningful midpoint.
    # A light-to-dark blue-teal scale keeps low values quiet and high means clear.
    palette = result_sequential_colormap(export_palette)
    width_mm = 89 if len(result.measurements) <= 3 else 183
    labels = [textwrap.fill(result.labels[row], 24) + f"\n(n={result.cell_counts[row]:,})"
              for row in rows]
    # Factor repeated measurement context out of the column headings only when
    # every name has the same known compartment and statistic. CSV names stay exact.
    parts = [name.split(": ") for name in result.measurements]
    compartments = {"Cell": "whole-cell", "Nucleus": "nuclear",
                    "Cytoplasm": "cytoplasmic", "Membrane": "membrane"}
    common = (all(len(part) == 3 and part[0] == parts[0][0] and part[2] == "Mean"
                  for part in parts) and parts[0][0] in compartments)
    column_labels = [textwrap.fill(part[1] if common else name, 14)
                     for part, name in zip(parts, result.measurements)]
    scale_label = ("Mean " + compartments[parts[0][0]] + " intensity" if common else "Mean")
    scale_label += f" ({result.units})" if result.units else " (recorded scale)"
    notes = []
    if any(n < result.cell_counts[row] for row in rows for n in result.usable_counts[row]):
        notes.append("Entry n: cells with usable measurements; row n: all cells.")
    if np.isnan(matrix[rows]).any():
        notes.append("Gray / —: no usable measurements.")
    note = "\n".join(textwrap.fill(line, 56 if width_mm == 89 else 115) for line in notes)
    note_height = 8 * (note.count("\n") + 1) + 4 if note else 0
    # Variable row heights accommodate wrapped names without shrinking the type.
    row_heights = np.array([8.5 * (label.count("\n") + 1) + 4 for label in labels])
    edges = np.r_[0, np.cumsum(row_heights)]
    centers = (edges[:-1] + edges[1:]) / 2
    height_pt = edges[-1] + 46 + 8.5 * max(label.count("\n") + 1 for label in column_labels) + note_height
    figure = Figure(figsize=(width_mm / 25.4, height_pt / 72), layout="constrained")
    # Spare margin protects editable SVG text in common wider fallback fonts.
    figure.get_layout_engine().set(w_pad=.16, h_pad=.045,
                                   rect=(0, note_height / height_pt, 1, 1 - note_height / height_pt))
    grid = figure.add_gridspec(2, 1, height_ratios=(edges[-1], 5))
    axis = figure.add_subplot(grid[0])
    scale_axis = figure.add_subplot(grid[1])
    heatmap = axis.pcolormesh(np.arange(len(result.measurements) + 1), edges,
                             np.ma.masked_invalid(matrix[rows]), cmap=palette, norm=norm,
                             edgecolors="white", linewidth=.4)
    axis.set_ylim(edges[-1], 0)
    axis.set_yticks(centers, labels=labels, fontsize=7)
    axis.set_xticks(np.arange(len(result.measurements)) + .5, labels=column_labels, fontsize=7)
    axis.xaxis.tick_top()
    for label in (*axis.get_xticklabels(), *axis.get_yticklabels()):
        label.set_parse_math(False)
    axis.tick_params(length=0, pad=4)
    axis.tick_params(axis="y", pad=13)
    axis.spines[:].set_visible(False)
    for plotted_row, row in enumerate(rows):
        for col, value in enumerate(matrix[row]):
            text = f"{value:.3g}" if np.isfinite(value) else "—"
            if np.isfinite(value) and 1000 <= abs(value) < 1_000_000:
                text = f"{float(text):,.0f}"
            n = result.usable_counts[row][col]
            if n < result.cell_counts[row]:
                text += f"\nn={n:,}"
            rgba = palette(norm(value)) if np.isfinite(value) else (.9, .9, .9, 1.)
            rgb = np.asarray(rgba[:3])
            linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
            luminance = float(linear @ [.2126, .7152, .0722])
            axis.text(col + .5, centers[plotted_row], text, ha="center", va="center", fontsize=7,
                      color="black" if luminance > .179 else "white")
    colorbar = figure.colorbar(heatmap, cax=scale_axis, orientation="horizontal")
    colorbar.set_label(textwrap.fill(scale_label, 55 if width_mm == 89 else 110), fontsize=7)
    colorbar.locator = MaxNLocator(nbins=4)
    colorbar.update_ticks()
    colorbar.ax.tick_params(labelsize=7, length=2, width=.5)
    colorbar.ax.xaxis.get_offset_text().set_fontsize(7)
    colorbar.outline.set_linewidth(.5)
    if note:
        figure.text(.035, .01, note, fontsize=6.5, va="bottom")
    data = _csv_output("marker-profiles", "Marker profile data (all categories)",
        ("label", "assignment_available", "total_cells", "measurement", "units", "usable_cells", "mean"),
        ((label, row < len(result.labels) - 1, result.cell_counts[row], measurement, result.units,
          result.usable_counts[row][col], result.means[row][col])
         for row, label in enumerate(result.labels) for col, measurement in enumerate(result.measurements)))
    return (*_exports(figure, "marker-profiles", "Saved marker profiles"), data)
