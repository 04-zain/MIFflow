"""

Draws the recorded classifier counts and percentages within each reference class.

We use the same reference-by-prediction matrix for two displays. The first
shows the number of evaluated observations in each class pair. The second divides
each row by its reference-class total, showing how that class's evaluated
observations were distributed among predicted classes. An empty reference row
is displayed as unavailable because no observations supplied that percentage.

The byte-rendering function creates, saves and clears one Matplotlib Figure
while holding a rendering lock. This allows UI requests to obtain independent
images without overlapping the font and drawing operations.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from io import BytesIO
from threading import Lock
from textwrap import fill

from matplotlib import rc_context
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Patch
from matplotlib.figure import Figure
import numpy as np

from .classifier_qc import classifier_metrics_from_confusion
from mif_pipeline.pipeline_core.config import MAXIMUM_PRIMARY_CLASS_COUNT

_RENDER_LOCK = Lock()


# ==============================================================================
#                                Evaluation plots
# ==============================================================================


def build_classifier_confusion_figure(
    *,
    classifier_label: str,
    split: str,
    patient_count: int,
    class_ids: tuple[str, ...],
    confusion_matrix: tuple[tuple[int, ...], ...],
    observation_unit: str = "regions",
) -> Figure:
    """

    Builds the confusion-count and within-reference-class percentage views for a saved evaluation.

    For row i and predicted class j, we calculate:

        percentage[i,j] = 100 * C[i,j] / sum(C[i,:])

    A row with no evaluated observations receives NaN and is drawn with an em dash
    and neutral background. A supported row can contain an actual zero, which
    is displayed as 0%. These cases show whether a class was absent from the
    selected references or was observed but never predicted in that column.

    Counts use a colour scale from zero to the largest entry, while percentages
    use zero to 100. Class names are wrapped and treated as literal text. We
    convert each background colour to linear RGB before calculating luminance
    and selecting black or white annotation text for contrast.

    Args:
        classifier_label: Model display name retained by the review context.
        split: Validation/test evaluation or grouped cross-validation tuning evidence.
        patient_count: Number of contributing subjects, at least one and no
            greater than the number of evaluated observations. The figure uses
            "subject" so it describes human and animal analyses consistently.
        class_ids: Unique output names in matrix-axis order, including optional Ignore.
            Two outputs are required; tissue figures support up to four named classes plus Ignore.
        observation_unit: Regions, pixels, patches or cells counted in the supplied matrix.
        confusion_matrix: Nonnegative integer counts with references in rows,
            predictions in columns and at least one observation overall.

    Returns:
        Matplotlib Figure showing both matrices with labeled count and percentage scales.

    Raises:
        ValueError: Labels, split or subject count are invalid for this figure.
        ClassifierCompatibilityError: Confusion counts have an incompatible
            shape, type or total.
    """

    classifier_metrics_from_confusion(confusion_matrix)
    if (
        type(classifier_label) is not str
        or not classifier_label.strip()
        or split not in ("validation", "test", "cross_validation")
        or observation_unit not in ("regions", "pixels", "patches", "cells")
        or type(patient_count) is not int
        or patient_count < 1
        or type(class_ids) is not tuple
        or len(class_ids) < 2
        or (observation_unit != "cells" and len(class_ids) > MAXIMUM_PRIMARY_CLASS_COUNT + 1)
        or any(type(label) is not str or not label.strip() for label in class_ids)
        or len(set(class_ids)) != len(class_ids)
        or len(class_ids) != len(confusion_matrix)
    ):
        raise ValueError(
            "A classifier figure needs class names, recorded counts and a subject count."
        )
    total = sum(sum(row) for row in confusion_matrix)
    if patient_count > total:
        raise ValueError("The subject count exceeds the number of reviewed observations.")

    counts = np.asarray(confusion_matrix, dtype=np.float64)
    support = counts.sum(axis=1, keepdims=True)
    # Normalize each reference class by its own evaluated observation count.
    # Keep an empty reference row unavailable rather than divide by zero.
    percentages = np.divide(
        counts * 100,
        support,
        out=np.full_like(counts, np.nan),
        where=support != 0,
    )
    labels = tuple(fill(label, width=18) for label in class_ids)
    vertical_labels = max(map(len, class_ids)) > 16
    # Context belongs in the review/caption; the figure retains only scientific labels.
    figure = Figure(figsize=(11.5, 5.2), facecolor="white", layout="constrained")
    axes = figure.subplots(1, 2)
    cmap = LinearSegmentedColormap.from_list(
        "classifier_teal",
        ("#F0F8F7", "#0B7777"),
    ).with_extremes(bad="#E6EBED")
    for axis, values, maximum, panel_title in zip(
        axes,
        (counts, percentages),
        (float(counts.max()), 100.0),
        ("Reviewed " + observation_unit, "Within each true class (%)"),
        strict=True,
    ):
        artist = axis.imshow(values, cmap=cmap, vmin=0, vmax=maximum, interpolation="nearest")
        colorbar = figure.colorbar(artist, ax=axis, shrink=.8)
        colorbar.set_label(panel_title, fontsize=13)
        colorbar.ax.tick_params(labelsize=13)
        axis.set_xticks(
            range(len(labels)),
            labels=tuple(fill(label, width=18 if vertical_labels else 12) for label in class_ids),
            fontsize=13,
        )
        for tick in axis.get_xticklabels():
            tick.set_rotation(90 if vertical_labels else 40)
            tick.set_ha("right")
            tick.set_rotation_mode("anchor")
        axis.set_yticks(range(len(labels)), labels=labels, fontsize=13)
        axis.set_xlabel("Predicted class", fontsize=14, labelpad=12)
        axis.set_ylabel("True class", fontsize=14, labelpad=12)

        axis.tick_params(length=0, pad=8)
        for spine in axis.spines.values():
            spine.set_visible(False)
        for text in (
            *axis.get_xticklabels(),
            *axis.get_yticklabels(),
            axis.xaxis.label,
            axis.yaxis.label,
            axis.title,
        ):
            text.set_fontfamily("DejaVu Sans")
            text.set_parse_math(False)
        for row in range(len(labels)):
            for column in range(len(labels)):
                value = values[row, column]
                text = (
                    "—"
                    if not np.isfinite(value)
                    else str(confusion_matrix[row][column]) if values is counts else f"{value:.0f}%"
                )
                rgb = np.asarray(cmap(value / maximum)[:3]) if np.isfinite(value) else np.ones(3)
                # Convert display RGB to linear light before estimating
                # luminance and choosing dark or light annotation text.
                linear_rgb = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
                luminance = float(linear_rgb @ (0.2126, 0.7152, 0.0722))
                axis.text(
                    column,
                    row,
                    text,
                    ha="center",
                    va="center",
                    fontsize=14,
                    fontfamily="DejaVu Sans",
                    color="white" if luminance < 0.179 else "black",
                )
    if np.isnan(percentages).any():
        axes[1].legend(handles=[Patch(facecolor="#E6EBED", label="No reference " + observation_unit)],
                       loc="lower center", bbox_to_anchor=(.5, 1.02), frameon=False, fontsize=13)
    from mif_pipeline.cell_analysis.figure_style import apply_figure_font
    apply_figure_font(figure)
    return figure


def render_classifier_confusion_figure(*, image_format: str = "png", **values: object) -> bytes:
    """

    Encodes the classifier evaluation figure for display or file export.

    We build and save the figure under one rendering lock, then clear it even
    when rendering fails. PNG uses 300 dpi; the PDF and SVG encoders retain
    supported vector elements. The returned bytes are supplied to the viewer
    or written to its chosen output location by the caller.

    Args:
        image_format: PNG, PDF or SVG, supplied as lowercase text.
        values: Arguments for build_classifier_confusion_figure(), containing
            the model label, split, subject count, classes and confusion counts.

    Returns:
        Complete encoded figure bytes.

    Raises:
        ValueError: The format or selected figure inputs are invalid. Errors
            from the image encoder propagate after the figure is cleared.
    """

    if image_format not in ("png", "pdf", "svg"):
        raise ValueError("Choose PNG, PDF or SVG for the classifier figure.")
    with _RENDER_LOCK:
        figure = build_classifier_confusion_figure(**values)
        try:
            output = BytesIO()
            with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
                figure.savefig(output, format=image_format, dpi=300, facecolor="white")
            return output.getvalue()
        finally:
            figure.clear()
