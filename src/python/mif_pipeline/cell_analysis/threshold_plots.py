"""

Draws the marker-threshold review and saves the numerical values used by the figure.

We show the complete selected-cell histogram first, with its manual and
fitted threshold candidates. Additional panels show distributions for each
tissue-section/compartment combination using the same candidate thresholds.
These panels let us inspect how one proposed intensity boundary falls
within the distributions represented across the tissue.

Fitted component curves are scaled for plotting beside cell counts. The
adjacent JSON retains their original probability-density values, displayed
curve values, scale factor, histogram bins, model parameters and selected
cell IDs. Monotone component-score arrays are also saved when provided by the
calculation. The plotting step uses the existing ThresholdReview and does
not change its estimated parameters or threshold values.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .thresholds import Histogram, ThresholdCandidate, ThresholdReview

_COLORS = {
    "manual": "#111111",
    "triangle": "#0072B2",
    "gmm": "#D55E00",
    "gamma": "#009E73",
    "nimbus": "#8B5CF6",
}


# ==============================================================================
#                                Threshold plots
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ThresholdPlotArtifact:
    """

    Identifies the review figure and the numerical description saved beside it.

    Attributes:
        figure_path: Selected PNG, PDF or SVG output filename.
        data_path: JSON filename formed by appending .json to the figure's
            extension, such as marker-review.png.json.
    """

    figure_path: Path
    data_path: Path


def write_threshold_review_artifact(
    review: ThresholdReview,
    destination: str | Path,
    *,
    marker_label: str,
) -> ThresholdPlotArtifact:
    """

    Draws the complete and section-specific intensity distributions with their calculated threshold candidates.

    We arrange the overall histogram and one panel per section/compartment
    combination in rows of at most three panels. Each available threshold is
    drawn at its recorded measurement value. The overall panel also shows the
    candidate's fitted component curves and its positive/negative cell counts.
    An unavailable fit appears in the legend with its recorded reason.

    After saving the figure, we write the corresponding JSON with histogram
    bins/counts, model settings, convergence information, original and displayed
    curves, and the fitting/example cell IDs. The figure and JSON are successive
    writes; an interrupted save can leave a figure without its numerical companion.

    Args:
        review: Completed ThresholdReview containing the full and section-specific
            distributions, candidate calculations and selected cell IDs.
        destination: PNG, PDF or SVG filename. Its parent directory is created
            when needed, and an existing file can be replaced.
        marker_label: Nonblank marker/measurement label for the overall panel.

    Returns:
        ThresholdPlotArtifact containing both output paths.

    Raises:
        ValueError: The label is blank or the figure extension is unsupported.
        OSError: Writing the figure or numerical description fails.
    """

    figure_path = Path(destination)
    if not marker_label.strip():
        raise ValueError("marker_label must be non-empty")
    if figure_path.suffix.lower() not in {".png", ".pdf", ".svg"}:
        raise ValueError("threshold figure must use PNG, PDF, or SVG")
    figure_path.parent.mkdir(parents=True, exist_ok=True)
    data_path = figure_path.with_suffix(figure_path.suffix + ".json")

    panel_count = 1 + len(review.facets)
    columns = min(3, panel_count)
    rows = ceil(panel_count / columns)
    figure, axes = plt.subplots(rows, columns, figsize=(6.2 * columns, 4.2 * rows), squeeze=False)
    flat_axes = tuple(axes.flat)
    _plot_histogram(flat_axes[0], review.histogram, title=f"{marker_label} — all eligible cells")
    for candidate in (review.manual, *review.candidates):
        _plot_candidate(flat_axes[0], candidate)
    flat_axes[0].legend(fontsize="small")

    # Reuse the overall candidate cutoffs in each local distribution.
    # A facet is not fitted again merely because it is shown in another panel.
    for axis, facet in zip(flat_axes[1:], review.facets, strict=False):
        _plot_histogram(
            axis,
            facet.histogram,
            title=f"section={facet.section} | compartment={facet.compartment}",
        )
        for candidate in (review.manual, *review.candidates):
            if candidate.status == "available" and candidate.threshold is not None:
                axis.axvline(candidate.threshold, color=_COLORS[candidate.method], linewidth=1.0)
    for axis in flat_axes[panel_count:]:
        axis.set_visible(False)
    figure.tight_layout()
    figure.savefig(figure_path, dpi=150)
    plt.close(figure)

    payload = {
        "schema_version": "threshold-review-plot/v2",
        "marker_label": marker_label,
        "eligible_count": review.eligible_count,
        "unknown_count": review.unknown_count,
        "histogram": _histogram_mapping(review.histogram),
        "thresholds": [_candidate_mapping(item) for item in (review.manual, *review.candidates)],
        "facets": [
            {
                "section": item.section,
                "compartment": item.compartment,
                "histogram": _histogram_mapping(item.histogram),
            }
            for item in review.facets
        ],
        "fit_cell_ids": list(review.fit_cell_ids),
        "example_cell_ids": list(review.example_cell_ids),
    }
    data_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return ThresholdPlotArtifact(figure_path=figure_path, data_path=data_path)


def _plot_histogram(axis: plt.Axes, histogram: Histogram, *, title: str) -> None:
    """

    Draws the recorded cell counts over their intensity-bin intervals.

    We subtract adjacent bin edges to obtain each bar's width and align the bar
    with its left edge. The horizontal axis retains the selected measurement
    values, and the vertical axis is the eligible-cell count in that interval.

    Args:
        axis: Matplotlib axes receiving the histogram.
        histogram: Recorded bin edges and corresponding cell counts.
        title: Label for the overall or section-specific distribution.
    """

    edges = np.asarray(histogram.bin_edges)
    counts = np.asarray(histogram.counts)
    widths = np.diff(edges)
    axis.bar(edges[:-1], counts, width=widths, align="edge", color="#B8B8B8", alpha=0.65)
    axis.set(title=title, xlabel="measurement value", ylabel="eligible cell count")


def _plot_candidate(axis: plt.Axes, candidate: ThresholdCandidate) -> None:
    """

    Adds a calculated threshold, its cell counts and available component curves to the overall histogram.

    For an available candidate, we draw a vertical line at the recorded boundary
    and include its numerical value and resulting cell proportions in the legend.
    Each fitted component curve is multiplied by _density_count_scale() before
    drawing. The model's monotone component-score arrays are retained in the
    numerical companion; this function draws the component curves on the count axis.

    Args:
        axis: Overall histogram axes.
        candidate: Manual or fitted ThresholdCandidate with its status, threshold,
            model curves and positive/negative counts.
    """

    if candidate.status != "available":
        axis.plot(
            [],
            [],
            color=_COLORS[candidate.method],
            label=f"{candidate.method}: unavailable ({candidate.reason})",
        )
        return
    counts = candidate.counts
    count_label = ""
    if counts is not None:
        count_label = (
            f"; +{counts.positive} ({counts.positive_fraction:.3f}), "
            f"-{counts.negative} ({1.0 - counts.positive_fraction:.3f})"
        )
    if candidate.threshold is None:
        decision_label = {"all_positive": "all finite cells positive",
                          "all_negative": "all finite cells negative",
                          "direct_scores": "direct image scores"}[candidate.outcome]
        axis.plot([], [], color=_COLORS[candidate.method],
                  label=f"{candidate.method}: {decision_label}{count_label}")
    else:
        boundary_label = (
            "Intensity > 0"
            if candidate.method == "gamma" and candidate.threshold == np.nextafter(0., np.inf)
            else f"{candidate.threshold:.8g}"
        )
        axis.axvline(
            candidate.threshold,
            color=_COLORS[candidate.method],
            linewidth=1.8,
            label=f"{candidate.method}: {boundary_label}{count_label}",
        )
    if candidate.component_densities:
        scale = _density_count_scale(candidate)
        for index, density in enumerate(candidate.component_densities, start=1):
            axis.plot(
                candidate.density_x,
                np.asarray(density) * scale,
                color=_COLORS[candidate.method],
                linestyle="--" if index == 1 else ":",
                linewidth=1.1,
                label=f"{candidate.method} component {index}",
            )


def _density_count_scale(candidate: ThresholdCandidate) -> float:
    """

    Calculates the display multiplier for a fitted component curve beside the cell-count histogram.

    Let N be the candidate's eligible-cell count and delta the median spacing
    between consecutive saved curve X positions. The multiplier is:

        display_scale = N * median(diff(density_x))

    Our Gamma model fits the strictly positive domain. Its N excludes the
    recorded zero-intensity cells; those cells remain in the histogram and
    decision counts but contribute no continuous Gamma density.

    Multiplying a density by this value expresses its height as an approximate
    number of cells over one curve-sampling interval. That interval can differ
    from a histogram bin's width, so the displayed curve height is not the exact
    expected count for that bin. We save both the multiplier and the original
    density values with the figure.

    Args:
        candidate: Candidate containing cell counts and its curve-sampling positions.

    Returns:
        Floating multiplier, or 1 when counts are absent or fewer than two
        curve positions were recorded.
    """

    if candidate.counts is None or len(candidate.density_x) < 2:
        return 1.0
    spacing = float(np.median(np.diff(np.asarray(candidate.density_x))))
    population_count = candidate.counts.total
    if candidate.method == "gamma" and candidate.parameters.get("conditional_positive_fit"):
        population_count -= int(candidate.parameters.get("scope_zero_count", 0))
    return population_count * spacing


def _histogram_mapping(histogram: Histogram) -> dict[str, object]:
    """

    Collects the counts and interval edges needed to reproduce a histogram panel.

    Args:
        histogram: Recorded distribution shown in the review.

    Returns:
        Dictionary containing ordered count and bin-edge lists.
    """

    return {"counts": list(histogram.counts), "bin_edges": list(histogram.bin_edges)}


def _candidate_mapping(candidate: ThresholdCandidate) -> dict[str, object]:
    """

    Records the candidate settings and exact numerical arrays associated with its plotted threshold.

    We retain the model status, threshold, convergence information and fitted
    parameters. Component densities are saved before scaling and again as the
    actual plotted Y values. Monotone component scores and their coordinates
    are preserved as supplied by the threshold calculation. These scores do
    not assert calibration against biological labels.

    Args:
        candidate: Manual or fitted threshold candidate displayed in the review.

    Returns:
        JSON-compatible dictionary containing the candidate calculation, display
        multiplier, curves and optional positive/negative cell counts.
    """

    counts = None
    if candidate.counts is not None:
        counts = {
            "negative": candidate.counts.negative,
            "positive": candidate.counts.positive,
            "positive_fraction": candidate.counts.positive_fraction,
        }
    # Retain original and displayed curves so the saved figure can be
    # interpreted without reverse-engineering its density scaling.
    component_density_scale = _density_count_scale(candidate)
    plotted_component_y = [
        list(np.asarray(item) * component_density_scale) for item in candidate.component_densities
    ]
    return {
        "method": candidate.method,
        "status": candidate.status,
        "outcome": candidate.outcome,
        "reason": candidate.reason,
        "threshold": candidate.threshold,
        "iterations": candidate.iterations,
        "converged": candidate.converged,
        "interpretation": candidate.interpretation,
        "parameters": dict(candidate.parameters),
        "density_x": list(candidate.density_x),
        "component_densities": [list(item) for item in candidate.component_densities],
        "component_density_scale": component_density_scale,
        "plotted_component_y": plotted_component_y,
        "posterior_x": list(candidate.posterior_x),
        "positive_posterior": list(candidate.positive_posterior),
        "counts": counts,
    }


__all__ = ["ThresholdPlotArtifact", "write_threshold_review_artifact"]
