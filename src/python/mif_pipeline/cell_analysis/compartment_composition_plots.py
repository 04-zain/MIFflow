"""Count and display saved phenotype assignments within primary tissue compartments."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import textwrap

import numpy as np

from .result_plots import _csv_output, _exports

PHENOTYPES_PER_PAGE = 16
COMPARTMENTS_PER_PAGE = 4


@dataclass(frozen=True)
class CompartmentComposition:
    """Exact cell counts, with the last row/column reserved for absent assignments."""

    phenotype_labels: tuple[str, ...]
    compartment_ids: tuple[str, ...]
    compartment_labels: tuple[str, ...]
    counts: tuple[tuple[int, ...], ...]
    total: int
    sample_id: str


def compartment_ids(dataset) -> tuple[str, ...]:
    """Return declared nonempty primary classes without reading cell rows."""
    dictionary = dataset.dictionaries.get("primary_compartment")
    return tuple(label for label in dictionary.labels if label) if dictionary else ()


def _codes(values, size: int, label: str) -> np.ndarray:
    valid = values.is_valid().to_numpy(zero_copy_only=False)
    codes = values.fill_null(0).to_numpy(zero_copy_only=False)
    observed = codes[valid]
    if observed.size and (not np.issubdtype(observed.dtype, np.integer)
                          or observed.min() < 0 or observed.max() >= size):
        raise ValueError(f"Saved {label} codes differ from the selected dictionary.")
    return np.where(valid, codes, size).astype(np.int64, copy=False)


def count_compartment_composition(
    dataset, labels: Mapping[str, str], *, sample_id: str,
) -> CompartmentComposition:
    """Count two projected categorical columns in one bounded pass, including nulls."""
    phenotype_dictionary = dataset.dictionaries.get("primary_phenotype")
    phenotypes = phenotype_dictionary.labels if phenotype_dictionary else ()
    compartment_dictionary = dataset.dictionaries.get("primary_compartment")
    declared = compartment_dictionary.labels if compartment_dictionary else ()
    ids = compartment_ids(dataset)
    positions = {class_id: index for index, class_id in enumerate(ids)}
    # Empty and null compartment assignments share the explicit unassigned column.
    remap = np.array([positions.get(class_id, len(ids)) for class_id in declared]
                     + [len(ids)], dtype=np.int64)
    counts = np.zeros((len(phenotypes) + 1, len(ids) + 1), dtype=np.int64)
    total = 0
    for batch in dataset.iter_batches(columns=("primary_compartment", "primary_phenotype")):
        compartments = remap[_codes(batch.column("primary_compartment"), len(declared), "compartment")]
        phenotypes_codes = _codes(batch.column("primary_phenotype"), len(phenotypes), "phenotype")
        counts += np.bincount(phenotypes_codes * counts.shape[1] + compartments,
                              minlength=counts.size).reshape(counts.shape)
        total += batch.num_rows
    if total != dataset.row_count:
        raise ValueError("Saved compartment rows differ from the selected cell count.")
    return CompartmentComposition(
        (*phenotypes, "Unavailable"), (*ids, ""),
        (*(labels.get(class_id, class_id) for class_id in ids), "Unassigned compartment"),
        tuple(tuple(map(int, row)) for row in counts), total, sample_id)


def render_compartment_composition(
    result: CompartmentComposition, *, row_page: int = 0, column_page: int = 0,
    export_palette: str = 'default',
):
    """Render within-compartment percentages and export all exact counts on every page."""
    from matplotlib.figure import Figure
    from matplotlib.colors import Normalize
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextToPath
    from .figure_style import result_sequential_colormap

    counts = np.asarray(result.counts, dtype=np.int64)
    totals = counts.sum(axis=0)
    # Null assignments accompany the final declared-category page when present.
    rows = list(range(len(result.phenotype_labels) - 1))
    columns = list(range(len(result.compartment_ids) - 1))
    if not rows:
        rows = [len(result.phenotype_labels) - 1]
    if not columns:
        columns = [len(result.compartment_ids) - 1]
    row_start = row_page * PHENOTYPES_PER_PAGE
    column_start = column_page * COMPARTMENTS_PER_PAGE
    selected_rows = rows[row_start:row_start + PHENOTYPES_PER_PAGE]
    selected_columns = columns[column_start:column_start + COMPARTMENTS_PER_PAGE]
    if (type(row_page) is not int or type(column_page) is not int
            or row_page < 0 or column_page < 0 or not selected_rows or not selected_columns):
        raise ValueError("This tissue-compartment figure page is unavailable.")
    if (row_start + PHENOTYPES_PER_PAGE >= len(rows) and counts[-1].sum()
            and selected_rows[-1] != counts.shape[0] - 1):
        selected_rows.append(counts.shape[0] - 1)
    if (column_start + COMPARTMENTS_PER_PAGE >= len(columns) and totals[-1]
            and selected_columns[-1] != counts.shape[1] - 1):
        selected_columns.append(counts.shape[1] - 1)
    selected = counts[np.ix_(selected_rows, selected_columns)]
    denominators = totals[selected_columns]
    percentages = np.divide(selected * 100., denominators,
                            out=np.full(selected.shape, np.nan), where=denominators > 0)
    # Keep column pages at one figure width, including their final partial page.
    width_mm = 89 if len(columns) <= 3 and len(selected_columns) <= 3 else 183
    row_labels = [textwrap.fill(result.phenotype_labels[index], 24) for index in selected_rows]
    # Budget the heading width after the row-label gutter. Character counts alone
    # collide when an unassigned fifth column is present, or in narrow figures.
    # DejaVu Sans is the wider editable-SVG fallback used in the export checks.
    font = FontProperties(family="DejaVu Sans", size=7)
    metrics = TextToPath()

    def text_width(value):
        return metrics.get_text_width_height_descent(value, font, ismath=False)[0]

    row_width = max((text_width(line) for label in row_labels for line in label.splitlines()), default=0)

    def available_column_width(width):
        return (width / 25.4 * 72 - row_width - 2 * .16 * 72 - 13) / len(selected_columns) - 4

    column_width = available_column_width(width_mm)
    words = [word for index in selected_columns for word in result.compartment_labels[index].split()]
    words.extend(f"(n={totals[index]:,})" for index in selected_columns)
    if width_mm == 89 and max(map(text_width, words), default=0) > column_width:
        width_mm = 183
        column_width = available_column_width(width_mm)
    column_labels = []
    for index in selected_columns:
        lines, line = [], ""
        for word in result.compartment_labels[index].split():
            if line and text_width(line + " " + word) > column_width:
                lines.append(line)
                line = ""
            # Split a word only when it cannot fit intact at the selected width.
            while len(word) > 1 and text_width(word) > column_width:
                split = 1
                while split < len(word) and text_width(word[:split + 1]) <= column_width:
                    split += 1
                lines.append(word[:split])
                word = word[split:]
            line = line + " " + word if line else word
        if line:
            lines.append(line)
        column_labels.append("\n".join(lines) + f"\n(n={totals[index]:,})")
    row_heights = np.array([max(17, 8.5 * (label.count("\n") + 1) + 4) for label in row_labels])
    edges = np.r_[0, np.cumsum(row_heights)]
    centers = (edges[:-1] + edges[1:]) / 2
    note_height = 12 if np.isnan(percentages).any() else 0
    height_pt = edges[-1] + 46 + 8.5 * max(label.count("\n") + 1 for label in column_labels) + note_height
    figure = Figure(figsize=(width_mm / 25.4, height_pt / 72), layout="constrained")
    figure.get_layout_engine().set(w_pad=.16, h_pad=.045,
                                   rect=(0, note_height / height_pt, 1, 1 - note_height / height_pt))
    grid = figure.add_gridspec(2, 1, height_ratios=(edges[-1], 5))
    axis = figure.add_subplot(grid[0])
    scale_axis = figure.add_subplot(grid[1])
    cmap = result_sequential_colormap(export_palette)
    image = axis.pcolormesh(np.arange(len(selected_columns) + 1), edges,
                           np.ma.masked_invalid(percentages), cmap=cmap,
                           norm=Normalize(0, 100), edgecolors="white", linewidth=.4)
    axis.set_ylim(edges[-1], 0)
    axis.set_xticks(np.arange(len(selected_columns)) + .5, labels=column_labels, fontsize=7)
    axis.set_yticks(centers, labels=row_labels, fontsize=7)
    axis.xaxis.tick_top()
    axis.tick_params(length=0, pad=4)
    axis.tick_params(axis="y", pad=13)
    for label in (*axis.get_xticklabels(), *axis.get_yticklabels()):
        label.set_parse_math(False)
    for i, j in np.ndindex(percentages.shape):
        value = percentages[i, j]
        text = "—" if np.isnan(value) else ("<0.1%" if 0 < value < .1 else f"{value:.1f}%")
        rgb = np.asarray(cmap(value / 100)[:3]) if np.isfinite(value) else np.array([.93] * 3)
        linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
        luminance = float(linear @ [.2126, .7152, .0722])
        axis.text(j + .5, centers[i], text, ha="center", va="center", fontsize=7,
                  color="black" if luminance > .179 else "white")
    axis.spines[:].set_visible(False)
    colorbar = figure.colorbar(image, cax=scale_axis, orientation="horizontal", ticks=(0, 25, 50, 75, 100))
    colorbar.set_label("Cells within compartment (%)", fontsize=7)
    colorbar.ax.tick_params(labelsize=7, length=2, width=.5)
    colorbar.outline.set_linewidth(.5)
    if np.isnan(percentages).any():
        figure.text(.035, .01, "— No exported cells in this compartment", fontsize=6.5, va="bottom")
    rows = ((result.sample_id, result.compartment_ids[column], result.compartment_labels[column],
             column < len(result.compartment_ids) - 1, result.phenotype_labels[row],
             row < len(result.phenotype_labels) - 1, int(counts[row, column]), int(totals[column]),
             int(counts[row, column]) / int(totals[column]) if totals[column] else None,
             result.total)
            for column in range(counts.shape[1]) for row in range(counts.shape[0]))
    data = _csv_output("compartment-composition", "Phenotypes by tissue compartment data",
        ("sample_id", "compartment_id", "compartment", "compartment_assigned", "phenotype",
         "phenotype_assignment_available", "cell_count", "compartment_total_cells",
         "fraction_within_compartment", "total_exported_cells"), rows)
    return (*_exports(figure, "compartment-composition", "Phenotypes by tissue compartment"), data)
