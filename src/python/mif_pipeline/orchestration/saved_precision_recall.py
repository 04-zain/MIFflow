"""Separate saved tissue RF precision–recall figures and exact score data."""
from __future__ import annotations

import json
from threading import Lock
from textwrap import fill
from urllib.parse import quote

from mif_pipeline.compartment_analysis.precision_recall import (
    precision_recall_curves, validate_cv_pixel_scores, validate_pixel_score_evidence,
)

_RENDER_LOCK = Lock()


def saved_precision_recall_sources(record_set, dependencies):
    """Yield only retained scores; old hard-count records never trigger inference."""
    for record in record_set.classifier_records:
        if record.classifier_kind.value == "supervised_phenotype":
            continue
        material, names = None, {}
        if record.fit_material_sha256 is not None:
            material = dependencies.training.project_provider.load_native_classifier_fit_material(
                record.native_source_project_identity_sha256, record.fit_material_sha256,
                workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
            if material.training_observations_json is not None:
                names = {r["class_id"]: r["name"] for r in
                         json.loads(material.training_observations_json).get("classes", ())}
        datasets = [metric.pixel_scores for metric in record.metrics if metric.pixel_scores is not None]
        if material is not None and material.model_selection_json is not None:
            report = json.loads(material.model_selection_json)
            if "pixel_scores" in report:
                scores = validate_cv_pixel_scores(report["pixel_scores"], report)
                if scores["fit_execution_sha256"] != record.fit_execution_sha256:
                    raise ValueError("The saved PR fold scores belong to a different classifier fit.")
                datasets.append(scores)
        for dataset in datasets:
            data = validate_pixel_score_evidence(dataset)
            identity = f"precision-recall/{quote(record.classifier_id, safe='')}/{data['split']}"
            yield identity, record, data, None, names
            for subject in sorted({r["subject_id"] for r in data["entries"]}):
                yield f"{identity}/subject/{quote(subject, safe='')}", record, data, subject, names


def precision_recall_choices(record_set, dependencies):
    from .batch_service_models import BatchSavedStageFigure
    choices = []
    for identity, record, data, subject, _ in saved_precision_recall_sources(record_set, dependencies):
        split = "Cross-validation tuning" if data["split"] == "cross_validation" else data["split"].title()
        title = f"{record.classifier_id} · {split} precision–recall · " + (
            "All subjects" if subject is None else f"Subject {subject}")
        choices.append(BatchSavedStageFigure(identity, title,
            "One-versus-rest curves from retained native RF scores on held-out reference pixels. "
            "Exact tied-score data and noninterpolated average precision; pooled curves weight pixels equally.",
            data_formats=("csv",)))
    return tuple(choices)


def render_saved_precision_recall(record_set, dependencies, figure_id):
    selected = next((r for r in saved_precision_recall_sources(record_set, dependencies) if r[0] == figure_id), None)
    if selected is None:
        raise ValueError("This classifier has no retained held-out pixel scores for the selected PR export.")
    _, record, data, subject, names = selected
    return render_precision_recall(data, figure_id=figure_id, classifier_label=record.classifier_id,
                                   subject_id=subject, class_names=names)


def render_precision_recall(value, *, figure_id, classifier_label, subject_id=None, class_names=None):
    """Return figure bytes, all curve thresholds, exact score bins and provenance."""
    from matplotlib.figure import Figure
    from matplotlib import colormaps, rc_context
    from mif_pipeline.cell_analysis.result_plots import _exports, _csv_output

    data = validate_pixel_score_evidence(value)
    curves = precision_recall_curves(data, subject_id=subject_id)
    selected = [r for r in data["entries"] if subject_id is None or r["subject_id"] == subject_id]
    names = class_names or {}
    subjects = len({r["subject_id"] for r in selected})
    count = sum(sum(r["reference_class_counts"]) for r in selected)
    split = "Subject-grouped CV · tuning" if data["split"] == "cross_validation" else data["split"].title()
    label = f"{classifier_label} · {split} precision–recall"
    scope = "All subjects" if subject_id is None else f"Subject {subject_id}"
    description = (f"{scope}; {count} held-out reference pixels; {subjects} subjects. "
        "One-versus-rest native RF vote scores, uncalibrated. Equal score ties enter together. "
        "AP is the noninterpolated recall-increment sum; pooling gives each pixel equal weight. "
        "Cross-validation curves describe tuning evidence, not independent test performance."
        if data["split"] == "cross_validation" else
        f"{scope}; {count} held-out reference pixels; {subjects} subjects. "
        "One-versus-rest native RF vote scores, uncalibrated. Equal score ties enter together. "
        "AP is the noninterpolated recall-increment sum; pooling gives each pixel equal weight.")
    with _RENDER_LOCK, rc_context({"font.size": 7, "axes.labelsize": 7.5,
                                 "xtick.labelsize": 7, "ytick.labelsize": 7}):
        height_mm = max(100, 18 * len(curves) + 20)
        figure = Figure(figsize=(183 / 25.4, height_mm / 25.4), constrained_layout=False)
        ax = figure.add_axes((.095, .16, .57, .80))
        colors = ("#146b78", "#bd641a", "#79549a", "#46843c", "#4c78a8", "#b45475")
        for i, curve in enumerate(curves):
            name = names.get(curve["class_id"], curve["class_id"])
            color = (colors[i] if len(curves) <= len(colors) else
                     colormaps["tab20"](i % 20) if len(curves) <= 20 else
                     colormaps["turbo"](i / max(1, len(curves) - 1)))
            if curve["status"] != "available":
                ax.plot([], [], color=color, label=fill(f"{name}: no positive references", 25))
                continue
            text = f"{name}\nAP {curve['average_precision']:.3f}; prevalence {curve['prevalence']:.1%}"
            ax.step([p["recall"] for p in curve["points"]],
                    [p["precision"] for p in curve["points"]], where="pre", linewidth=1.2,
                    color=color, label=fill(name, 25) + text[len(name):])
            ax.axhline(curve["prevalence"], color=color, linestyle=":", linewidth=.8, alpha=.5)
        ax.set(xlabel="Recall", ylabel="Precision", xlim=(0, 1.01), ylim=(0, 1.02))
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(color="#dce2e5", linewidth=.45, alpha=.65)
        ax.tick_params(width=.6, length=3)
        for spine in ax.spines.values():
            spine.set_linewidth(.6)
        ax.legend(loc="upper left", bbox_to_anchor=(1.03, 1), borderaxespad=0,
                  frameon=False, fontsize=7, labelspacing=1.3, handlelength=2.2)
        figure.text(.095, .025, "Dotted lines: class prevalence; AP: average precision",
                    fontsize=7, va="bottom", color="#4b5563")
        figures = _exports(figure, figure_id, label, description=description)
    common = (classifier_label, data["split"], scope, subjects, "pixels")
    curve_rows = []
    for curve in curves:
        base = (*common, curve["class_id"], names.get(curve["class_id"], curve["class_id"]),
                curve["status"], curve["positive_count"], curve["negative_count"],
                curve["prevalence"], curve["average_precision"])
        points = curve["points"] or [dict.fromkeys(("threshold", "precision", "recall", "true_positive", "false_positive"))]
        curve_rows.extend((*base, *(p[k] for k in ("threshold", "precision", "recall", "true_positive", "false_positive"))) for p in points)
    curves_csv = _csv_output(figure_id + "/curves", label + " · Curves",
        ("classifier", "split", "scope", "subject_count", "sample_unit", "class_id", "class_name",
         "status", "positive_count", "negative_count", "prevalence", "average_precision",
         "threshold", "precision", "recall", "true_positive", "false_positive"), curve_rows)
    bins_csv = _csv_output(figure_id + "/scores", label + " · Exact score counts",
        ("classifier", "split", "project_entry_id", "subject_id", "fold", "model_sha256",
         "fit_execution_sha256", "sample_unit", "score_kind", "class_id", "score", "positive_count", "negative_count"),
        ((classifier_label, data["split"], r["project_entry_id"], r["subject_id"], r["fold"], r["model_sha256"],
          data["fit_execution_sha256"], "pixels", data["score_kind"], class_id, score, pos, neg)
         for r in selected for class_id, bins in zip(data["class_ids"], r["histograms"], strict=True)
         for score, pos, neg in bins))
    metadata = _csv_output(figure_id + "/metadata", label + " · Evaluation meaning",
        ("field", "value"), (("description", description),
        ("threshold_rule", "score >= threshold; blank threshold selects none"),
        ("average_precision", "sum of recall increments times precision; no interpolation"),
        ("score_kind", data["score_kind"]), ("sampling", data["sampling"]),
        ("reference_membership", "class-union geometry covers pixel centre; polygon boundaries included"),
        ("pixel_centre_coordinates", "native tile region origin + (pixel index + 0.5) * downsample"),
        ("fit_execution_sha256", data["fit_execution_sha256"])))
    return (*figures, curves_csv, bins_csv, metadata)
