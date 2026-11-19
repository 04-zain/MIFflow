"""Count and display saved binary marker calls without deriving new gates."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from io import BytesIO
import json

from .phenotyping_population import NOT_PHENOTYPED

MAX_MARKERS = 8
MAX_DISPLAY_COMBINATIONS = 20


@dataclass(frozen=True)
class MarkerCombinationCounts:
    markers: tuple[str, ...]
    counts: tuple[tuple[int, int], ...]
    total: int
    unknown: int
    not_phenotyped: int

    @property
    def complete(self):
        return sum(count for _mask, count in self.counts)


def count_marker_combinations(dataset, markers):
    """Stream two selected columns; at most 256 binary counters are retained."""
    markers = tuple(markers)
    if not 2 <= len(markers) <= MAX_MARKERS or len(set(markers)) != len(markers):
        raise ValueError("Co-expression figures require two to eight distinct selected markers.")
    dictionary = dataset.dictionaries.get("primary_phenotype")
    unphenotyped_codes = set() if dictionary is None else {
        index for index, label in enumerate(dictionary.labels) if label == NOT_PHENOTYPED}
    counts = Counter()
    total = unknown = not_phenotyped = 0
    for batch in dataset.iter_batches(columns=("marker_states", "primary_phenotype")):
        for raw, phenotype in zip(batch.column("marker_states").to_pylist(),
                                  batch.column("primary_phenotype").to_pylist(), strict=True):
            total += 1
            if phenotype in unphenotyped_codes:
                not_phenotyped += 1
                continue
            states = {} if raw is None else json.loads(raw)
            if not isinstance(states, dict):
                raise ValueError("Saved marker calls must be a marker-to-state mapping.")
            selected = tuple(states.get(marker, "unknown") for marker in markers)
            if any(state not in {"positive", "negative", "unknown"} for state in selected):
                raise ValueError("Saved marker calls contain an unsupported state.")
            if "unknown" in selected:
                unknown += 1
                continue
            mask = sum(1 << index for index, state in enumerate(selected) if state == "positive")
            counts[mask] += 1
    if total != dataset.row_count:
        raise ValueError("Saved marker-call rows differ from the selected cell count.")
    return MarkerCombinationCounts(markers, tuple(sorted(counts.items())), total, unknown, not_phenotyped)


def displayed_combinations(result):
    """Keep all-negative visible and explicitly aggregate every remaining cell."""
    ranked = sorted(result.counts, key=lambda item: (-item[1], item[0]))
    shown = ranked[:MAX_DISPLAY_COMBINATIONS]
    negative = next((item for item in ranked if item[0] == 0), None)
    if negative is not None and negative not in shown:
        shown[-1] = negative
        shown.sort(key=lambda item: (-item[1], item[0]))
    selected = {mask for mask, _count in shown}
    remaining = [(mask, count) for mask, count in ranked if mask not in selected]
    return tuple(shown), sum(count for _mask, count in remaining), len(remaining)


def render_marker_combinations(result, *, export_palette='default'):
    """Draw saved calls and export every counted combination before display grouping."""
    from itertools import chain
    from .result_plots import _csv_output
    import numpy as np
    from matplotlib import rc_context
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from .figure_style import result_export_palette
    colors = result_export_palette(export_palette)

    if not result.complete:
        raise ValueError("No cells have known calls for all selected markers.")
    shown, other_count, other_combinations = displayed_combinations(result)
    rows = (*shown, *(((None, other_count),) if other_count else ()))
    figure = Figure(figsize=(183 / 25.4, (14 * len(rows) + 78) / 72), layout="constrained")
    # Keep edge labels inside the canvas when an editable SVG uses fallback fonts.
    figure.get_layout_engine().set(w_pad=.16, h_pad=.045)
    try:
        matrix, bars = figure.subplots(1, 2, sharey=True, gridspec_kw={"width_ratios": (1.3, 1)})
        positions = np.arange(len(rows))
        largest_count = max(count for _mask, count in rows)
        for y, (mask, count) in enumerate(rows):
            if y % 2 == 0:
                matrix.axhspan(y - .5, y + .5, color="#f0f4f5", zorder=0)
                bars.axhspan(y - .5, y + .5, color="#f0f4f5", zorder=0)
            if mask is not None:
                positive = [index for index in range(len(result.markers)) if mask & (1 << index)]
                matrix.scatter(range(len(result.markers)), [y] * len(result.markers),
                               s=13, facecolors="white", edgecolors="#8c9ba1", linewidths=.6, zorder=2)
                if positive:
                    matrix.scatter(positive, [y] * len(positive), s=14, color=colors['series'], zorder=3)
            bars.barh(y, count, height=.65, color=colors['series'] if mask is not None else "#869399")
            fraction = "<0.1%" if 0 < count / result.complete < .001 else f"{count / result.complete:.1%}"
            bars.text(count + largest_count * .025, y,
                      f"{count:,} ({fraction})", va="center", fontsize=7)
        labels = ["All negative" if mask == 0 else f"Other {other_combinations} combinations"
                  if mask is None else "" for mask, _count in rows]
        matrix.set_yticks(positions, labels=labels, fontsize=7)
        matrix.set_xticks(range(len(result.markers)), labels=result.markers, rotation=35, ha="right")
        for label in matrix.get_xticklabels():
            label.set_parse_math(False)
        matrix.set_xlim(-.6, len(result.markers) - .4)
        matrix.set_ylim(len(rows) - .5, -.5)
        bars.set_xlim(0, largest_count * 1.42)
        bars.set_xlabel("Cells", fontsize=7)
        from matplotlib.ticker import MaxNLocator
        bars.xaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        bars.tick_params(axis="y", left=False, labelleft=False)
        matrix.tick_params(axis="y", length=0)
        for axis in (matrix, bars):
            axis.tick_params(labelsize=7, length=2, width=.5)
            axis.spines["bottom"].set_linewidth(.5)
            axis.spines[["top", "right", "left"]].set_visible(False)
        matrix.tick_params(axis="y", length=0)
        matrix.legend(handles=[
            Line2D([], [], marker="o", markersize=4, linestyle="", color=colors['series'], label="Positive"),
            Line2D([], [], marker="o", markersize=4, linestyle="", markerfacecolor="white", markeredgecolor="#8c9ba1", label="Negative")],
            loc="lower left", bbox_to_anchor=(0, 1.01), frameon=False, ncol=2, fontsize=7)
        bars.legend(handles=[Line2D([], [], linestyle="", label=
                    f"Percentages of {result.complete:,} complete calls")],
                    loc="lower left", bbox_to_anchor=(0, 1.01), frameon=False,
                    fontsize=7, handlelength=0, handletextpad=0)
        figure.supxlabel(
            f"Complete calls: {result.complete:,} / {result.total:,} cells   ·   "
            f"Cells with unknown calls: {result.unknown:,}   ·   Not phenotyped: {result.not_phenotyped:,}", fontsize=7)
        outputs = []
        from .figure_style import apply_figure_font
        apply_figure_font(figure)
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            for fmt in ("png", "pdf", "svg"):
                output = BytesIO()
                figure.savefig(output, format=fmt, dpi=300, facecolor="white")
                outputs.append(("marker-combinations/" + fmt, "Saved marker co-expression", fmt, output.getvalue()))
        combinations = (("combination", *("positive" if mask & (1 << index) else "negative"
                          for index in range(len(result.markers))), count,
                         count / result.complete, result.complete, result.total,
                         result.unknown, result.not_phenotyped)
                        for mask, count in result.counts)
        exclusions = ((kind, *(None for _ in result.markers), count, None,
                       result.complete, result.total, result.unknown, result.not_phenotyped)
                      for kind, count in (("unknown_calls", result.unknown),
                                          ("not_phenotyped", result.not_phenotyped)))
        outputs.append(_csv_output("marker-combinations", "Marker combination data (all counted combinations)",
            ("row_type", *("marker:" + marker for marker in result.markers), "cell_count",
             "fraction_of_complete_calls", "complete_calls", "total_cells",
             "unknown_calls", "not_phenotyped"), chain(combinations, exclusions)))
        return tuple(outputs)
    finally:
        figure.clear()
