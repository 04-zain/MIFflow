"""Figures from accepted gates and their exact retained review predecessors."""
from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
from urllib.parse import quote

from .batch_service_models import BatchSavedStageFigure


@dataclass(frozen=True)
class SavedMarkerFigure:
    descriptor: BatchSavedStageFigure
    histogram: object
    candidate: object
    cutoff: float | None
    outcome: str
    score: bool
    axis_label: str
    fitted: bool = False


def marker_figures(authority, snapshot, state_store):
    from .batch_phenotyping.marker_application_execution import decode_committed_marker_gate

    decode_committed_marker_gate(authority)
    return retained_marker_figures(authority.artifact_ref.owning_identity, snapshot.head, state_store)


def retained_marker_figures(identity, head, state_store):
    """Follow recorded predecessor links, never infer associations from filenames."""
    from .batch_phenotyping._common import digest

    accepted = {item["marker_id"]: item for item in identity["accepted_thresholds"]}
    predecessor = identity["interaction_predecessor_terminal_record_sha256"]
    allowed = set(head.terminal_attempt_sha256_by_attempt_id.values())
    contexts = {}
    wanted = {}
    seen = set()
    node_id = None
    while predecessor is not None and predecessor not in seen:
        if predecessor not in allowed:
            raise ValueError("The accepted marker review is no longer retained.")
        seen.add(predecessor)
        attempt = state_store.load_attempt(predecessor)
        node_id = attempt.node_id if node_id is None else node_id
        if attempt.node_id != node_id:
            raise ValueError("The saved marker review belongs to a different stage.")
        context = attempt.stage_result.resume_context or {}
        evidence = context.get("evidence_sha256")
        if evidence is not None and "marker_reviews" in context:
            if (evidence != digest({key: value for key, value in context.items()
                                   if key not in {"schema_version", "evidence_sha256"}})
                    or digest(context["measurement_authority_bindings"]) !=
                    digest(identity["measurement_authority_bindings"])):
                raise ValueError("The saved marker review does not match its accepted measurements.")
            if not wanted:
                if evidence != identity["evidence_sha256"]:
                    raise ValueError("The accepted marker review association is inconsistent.")
                wanted.update((item["marker_id"], evidence) for item in context["marker_reviews"])
                for item in (context.get("marker_workflow") or {}).get("completed", ()):
                    marker = item["accepted_threshold"]["marker_id"]
                    if digest(item["accepted_threshold"]) != digest(accepted[marker]):
                        raise ValueError("The saved marker decision differs from the accepted gate.")
                    wanted[marker] = item["review_evidence_sha256"]
            for review in context["marker_reviews"]:
                if wanted.get(review["marker_id"]) == evidence:
                    contexts[review["marker_id"]] = review
            if set(contexts) == set(accepted):
                break
        authorization = state_store.load_authorization(attempt.authorization_sha256)
        predecessor = authorization.interaction_predecessor_terminal_record_sha256
    figures = []
    for marker, decision in accepted.items():
        review = contexts.get(marker)
        if review is None:
            continue
        if decision["fit_scope"] == "per_slide":
            decisions = {item["image_id"]: item for item in decision["slide_thresholds"]}
            views = [(item["image_identity"], item["sample_id"] + " · " + item["slide_id"],
                      item["review"], decisions[item["image_identity"]])
                     for item in review["per_slide_reviews"] if item["image_identity"] in decisions]
        else:
            views = [("pooled", "Pooled", review["pooled_review"], decision)]
        for scope, label, values, selected in views:
            score = selected["selection_source"] == "nimbus"
            candidate = next((item for item in values["candidates"]
                              if item["method"] == selected["selection_source"]), None)
            histogram = (None if candidate is None else candidate["parameters"].get("score_histogram")) if score else values["histogram"]
            if histogram is None:
                continue
            detail = "Retained review distribution with the accepted " + ("Nimbus score cutoff." if score else "intensity gate.")
            if decision["section_overrides"]:
                detail += " Section-specific overrides are not shown in this overall view."
            descriptor = BatchSavedStageFigure(
                f"marker/{quote(marker, safe='')}/{quote(scope, safe='')}",
                f"{marker} · {label}" + (" · Nimbus scores" if score else " · Intensity"), detail,
                data_formats=("csv",))
            saved = SavedMarkerFigure(descriptor, histogram, candidate,
                selected["score_cutoff"] if score else selected["threshold"], selected["outcome"],
                score, "Nimbus expression confidence" if score else review["measurement_semantic"]["measurement"])
            figures.append(saved)
            if candidate is not None and not score and candidate["component_densities"]:
                figures.append(replace(saved, fitted=True, descriptor=BatchSavedStageFigure(
                    descriptor.figure_id + "/fit", f"{marker} · {label} · Fitted distribution",
                    "Fitted component curves with their means, variances and weights.", data_formats=("csv",))))
    return tuple(figures)


def render_marker_figure(saved):
    """Render the retained histogram and export its exact saved values and decision."""
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    import numpy as np
    from matplotlib import rc_context
    from matplotlib.figure import Figure

    edges = np.asarray(saved.histogram["bin_edges"], dtype=float)
    counts = np.asarray(saved.histogram["counts"], dtype=float)
    if (edges.size != counts.size + 1 or not np.all(np.isfinite(edges))
            or not np.all(np.isfinite(counts)) or np.any(counts < 0) or np.any(np.diff(edges) <= 0)):
        raise ValueError("Saved marker histogram is invalid.")
    candidate = saved.candidate
    has_density = saved.fitted and candidate is not None and not saved.score and bool(candidate["component_densities"])
    font_size = 10
    figure = Figure(figsize=(7.5, 5.4) if has_density else (7, 4.5), layout="constrained")
    try:
        if has_density:
            axis, table_axis = figure.subplots(2, 1, gridspec_kw={"height_ratios": (4, 1)})
            from mif_pipeline.cell_analysis.marker_fit_figure import add_component_parameters
            add_component_parameters(table_axis, candidate["parameters"])
        else:
            axis = figure.subplots()
        heights = counts / (counts.sum() * np.diff(edges)) if has_density and counts.sum() else counts
        axis.bar(edges[:-1], heights, width=np.diff(edges), align="edge", color="#91bdb6",
                 label=f"Plotted: {int(counts.sum()):,} cells")
        axis.set(ylabel="Weighted density (1 / measurement unit)" if has_density else "Cells", ylim=(0, None))
        if saved.cutoff is not None:
            axis.axvline(saved.cutoff, color="#163a46", linestyle="-", linewidth=1.6,
                         label=f"Accepted cutoff: {saved.cutoff:.6g}")
        else:
            axis.text(.98, .95, "Accepted outcome: " + saved.outcome.replace("_", " "),
                      ha="right", va="top", transform=axis.transAxes, fontsize=font_size)
        if has_density:
            parameters = candidate["parameters"]
            total = candidate.get("counts", {}).get("total", 0)
            positive_fraction = ((total - parameters.get("scope_zero_count", 0)) / total
                if total and parameters.get("conditional_positive_fit") else 1.)
            for density, color, style, label in zip(candidate["component_densities"],
                    ("#357e9c", "#a25a35"), ("-", "--"),
                    ("Lower-mean component", "Higher-mean component"), strict=True):
                axis.plot(candidate["density_x"], np.asarray(density) * positive_fraction,
                    color=color, linestyle=style, linewidth=1.8, label=label)
        legend_position = {} if saved.cutoff is not None else dict(loc="upper right", bbox_to_anchor=(1, .86))
        axis.legend(frameon=False, fontsize=font_size, **legend_position)
        axis.set_xlabel(saved.axis_label if saved.score else saved.axis_label + " (measurement units)", fontsize=font_size)
        axis.yaxis.label.set_fontsize(font_size)
        axis.tick_params(labelsize=font_size)
        axis.spines[["top", "right"]].set_visible(False)
        outputs = []
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        apply_figure_font(figure)
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            for fmt in ("png", "pdf", "svg"):
                buffer = BytesIO()
                figure.savefig(buffer, format=fmt, dpi=300)
                outputs.append((saved.descriptor.figure_id + "/" + fmt, saved.descriptor.title, fmt, buffer.getvalue()))
        outputs.append(_csv_output(saved.descriptor.figure_id + "/histogram",
            saved.descriptor.title + " · Histogram data",
            ("axis", "value_kind", "lower_edge", "upper_edge", "cell_count", "accepted_cutoff", "accepted_outcome"),
            ((saved.axis_label, "Nimbus confidence" if saved.score else "saved measurement",
              left, right, count, saved.cutoff, saved.outcome)
             for left, right, count in zip(saved.histogram["bin_edges"][:-1],
                saved.histogram["bin_edges"][1:], saved.histogram["counts"], strict=True))))
        if has_density:
            outputs.append(_csv_output(saved.descriptor.figure_id + "/density",
                saved.descriptor.title + " · Component density data",
                ("axis", "measurement_value", "lower_mean_weighted_density", "higher_mean_weighted_density"),
                ((saved.axis_label, x, low, high) for x, low, high in zip(candidate["density_x"],
                    *(np.asarray(curve) * positive_fraction for curve in candidate["component_densities"]), strict=True))))
            from mif_pipeline.cell_analysis.marker_fit_figure import component_parameters
            outputs.append(_csv_output(saved.descriptor.figure_id + "/parameters",
                saved.descriptor.title + " · Fitted component parameters",
                ("component", "mean", "variance", "weight", "weight_population"),
                ((*row, "positive values" if candidate["parameters"].get("conditional_positive_fit") else "all fitted values")
                 for row in component_parameters(candidate["parameters"]))))
        return tuple(outputs)
    finally:
        figure.clear()
