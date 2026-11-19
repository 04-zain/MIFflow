"""Explicit marker phenotype setup on the committed measurement inventory.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from math import isfinite

from mif_pipeline.gui.interactions import ControlKind, FormControl, FormModel, decode_control_value, decode_form_values
from mif_pipeline.orchestration.batch_phenotyping.marker_settings import (
    validate_marker_setup_context,
    validate_marker_setup_response,
)
from mif_pipeline.pipeline_core import StageResult
from .terminology import cell_measurement_label

# ==============================================================================
#                                  Marker setup
# ==============================================================================


def build_marker_setup_form(result: StageResult) -> FormModel:
    """Offer scope and per-marker models from current measurements.

    Existing names and rules can prefill the form. We leave the fitting scope and
    method unselected so the researcher records those choices before calculation.
    """
    context = validate_marker_setup_context(result)
    markers = tuple(context["canonical_marker_ids"])
    initial = context.get("initial_marker_settings") or {}
    initial_rules = {rule["marker_id"]: rule for rule in initial.get("marker_rules", ())}
    offers = tuple(context["offered_marker_features"])
    workflow = context.get("marker_workflow")
    current = workflow.get("current_marker_id") if workflow else None
    sequential_initial = initial.get("schema_version") == "marker-phenotype-settings/v4"
    controls: list[FormControl] = [
        FormControl(
            "marker_fit_scope", "Fit scope", ControlKind.CHOICE,
            choices=("pooled", "per_slide"),
            value=initial.get("fit_scope") if sequential_initial else None,
            help_text="Fit each marker using cells pooled across images or fit it separately on each slide.",
        )
    ]
    labels: dict[str, dict[str, str]] = {
        "marker_fit_scope": {"pooled": "Pooled", "per_slide": "Per slide"}
    }
    for marker in markers:
        choices = tuple(item["feature_id"] for item in offers if item["marker_id"] == marker)
        controls.extend(
            (
                FormControl(
                    f"marker_{marker}_feature",
                    f"{marker}: cell measurement",
                    ControlKind.CHOICE,
                    choices=choices, required=False,
                    value=initial_rules.get(marker, {}).get("feature_id") or next(
                        (item["feature_id"] for item in offers
                         if item["marker_id"] == marker and str(item["measurement"]).rsplit(":", 1)[-1].strip().lower() == "mean"),
                        choices[0] if choices else None),
                    help_text="Choose which cell measurement defines this marker's signal.",
                ),
                FormControl(
                    f"marker_{marker}_method", "Threshold method", ControlKind.CHOICE,
                    choices=("gmm", "gmm_separate", "gamma", "triangle", "nimbus", "manual"),
                    required=False,
                    value=initial_rules.get(marker, {}).get("method") if sequential_initial else None,
                ),
                *(FormControl(
                    f"marker_{marker}_{component}_{bound}",
                    f"{caption}-mean component: {bound_caption} mean or peak", ControlKind.TEXT, required=False,
                ) for component, caption in (("lower", "Lower"), ("upper", "Higher"))
                  for bound, bound_caption in (("min", "minimum"), ("max", "maximum"))),
                FormControl(
                    f"marker_{marker}_nimbus_source", "Nimbus input", ControlKind.CHOICE,
                    choices=("automatic", "scores"), value="automatic", required=False,
                ),
                FormControl(
                    f"marker_{marker}_nimbus_checkpoint_path", "Nimbus model checkpoint", ControlKind.LOCAL_FILE,
                    required=False, help_text="Leave blank to use Software setup on the processing computer. Otherwise enter the checkpoint's absolute path there. For cluster streaming, enter a cluster path or Browse within the configured mounted cluster folder. Images and segmented cells are supplied automatically.",
                ),
                FormControl(
                    f"marker_{marker}_nimbus_score_path", "Nimbus scores file", ControlKind.LOCAL_FILE,
                    required=False, help_text="Enter the saved scores path on the processing computer, with the image, cell and model identities for this marker. For cluster streaming, enter a cluster path or Browse within the configured mounted cluster folder.",
                ),
                FormControl(
                    f"marker_{marker}_nimbus_magnification", "Objective magnification if missing", ControlKind.NUMBER,
                    required=False, help_text="Enter the objective magnification used to scan images whose metadata does not record it, for example 20 for 20×. The entered value applies to those images.",
                ),
                FormControl(
                    f"marker_{marker}_nimbus_cutoff", "Nimbus score cutoff", ControlKind.NUMBER,
                    value=0.5, required=False,
                ),
            )
        )
        labels[f"marker_{marker}_nimbus_source"] = {"automatic": "Run on selected images", "scores": "Advanced: load saved scores"}
        labels[f"marker_{marker}_method"] = {
            "gmm": "Gaussian mixture (shared variance)", "gmm_separate": "Gaussian mixture (separate variances)",
            "gamma": "Two-component Gamma mixture",
            "triangle": "Triangle", "nimbus": "Nimbus", "manual": "Manual",
        }
        labels[f"marker_{marker}_feature"] = {
            item[
                "feature_id"
            ]: cell_measurement_label(item["compartment"], item["measurement"].rsplit(":", 1)[-1].strip()) + (
                f" · {item['channel_id']}" if len({offer["channel_id"] for offer in offers
                    if offer["marker_id"] == marker}) > 1 else "")
            for item in offers
            if item["marker_id"] == marker
        }
    if sequential_initial:
        restored = []
        for control in controls:
            value = control.value
            for marker, rule in initial_rules.items():
                prefix = f"marker_{marker}_"
                if not control.name.startswith(prefix):
                    continue
                suffix = control.name.removeprefix(prefix)
                if suffix == "method" and value == "gmm" and rule.get("gaussian_variance") == "unshared":
                    value = "gmm_separate"
                elif suffix in {"nimbus_score_path", "nimbus_cutoff", "nimbus_checkpoint_path", "nimbus_magnification"}:
                    value = rule.get(suffix, value)
                elif suffix == "nimbus_source":
                    value = "scores" if rule.get("nimbus_score_path") else "automatic"
                constraints = rule.get("component_location_constraints", {}).get(rule.get("method"), {})
                bounds = constraints.get("mean_bounds", constraints.get("mode_bounds", [[None, None], [None, None]]))
                if suffix in {"lower_min", "lower_max", "upper_min", "upper_max"}:
                    endpoint = bounds[0 if suffix.startswith("lower") else 1][0 if suffix.endswith("min") else 1]
                    value = "Q" + format(endpoint["quantile"] * 100, ".12g") if isinstance(endpoint, Mapping) else endpoint
            restored.append(replace(control, value=value))
        controls = restored
    controls.append(FormControl(
        "selected_marker_ids", "Markers and measurements", ControlKind.OBJECT_SEQUENCE,
        choices=markers, value=list(initial_rules) if initial_rules else list(markers),
    ))
    return FormModel(
        form_id="marker_setup",
        title="Fit marker gates",
        message="Select markers and their measurements, then fit and review each marker.",
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        context={
            "adapter": "marker_setup",
            "stage_result": result.to_mapping(),
            "choice_labels": labels,
        },
    )


def submit_marker_setup_form(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object] | None:
    """Submit the selected scope and models for marker fitting.

    We omit inapplicable component constraints and retain Nimbus input choices.
    The settings owner checks the complete response against the offered features
    before any fitting or inference is requested.
    """
    if action == "abort":
        if action not in form.allowed_actions:
            raise ValueError("Marker action was not offered")
        return {}
    if "revisit_marker_id" in raw_values:
        if action != "define_marker_composite_rules":
            raise ValueError("Marker revisit requires the current setup action")
        return validate_marker_setup_response(
            StageResult.from_mapping(form.context["stage_result"]), raw_values)
    if set(raw_values) - {control.name for control in form.controls} - {"phenotyping_population", "preview_population"}:
        raise ValueError("Marker setup contains fields that were not offered")
    result = StageResult.from_mapping(form.context["stage_result"])
    context = validate_marker_setup_context(result)
    available_markers = tuple(context["canonical_marker_ids"])
    selection_control = next(control for control in form.controls if control.name == "selected_marker_ids")
    selected = decode_control_value(selection_control, raw_values.get("selected_marker_ids", list(available_markers)))
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(marker, str) or marker not in available_markers for marker in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("selected_marker_ids: Select at least one offered marker.")
    markers = tuple(marker for marker in available_markers if marker in selected)
    preview = raw_values.get("preview_population") is True
    applicable = {key: value for key, value in raw_values.items() if key not in {"phenotyping_population", "preview_population"}}
    applicable["selected_marker_ids"] = selected
    for control in form.controls:
        marker = next((item for item in available_markers if control.name.startswith(f"marker_{item}_")), None)
        if marker is not None and (marker not in markers or (preview and control.name != f"marker_{marker}_feature")):
            applicable.pop(control.name, None)
            continue
        if control.name.endswith("_nimbus_cutoff"):
            prefix = control.name.removesuffix("nimbus_cutoff")
            if applicable.get(prefix + "method") != "nimbus":
                applicable[control.name] = None
        if control.name.endswith("_nimbus_magnification"):
            prefix = control.name.removesuffix("nimbus_magnification")
            if applicable.get(prefix + "method") != "nimbus" or applicable.get(prefix + "nimbus_source") != "automatic":
                applicable[control.name] = None
    values = decode_form_values(form, action, applicable)
    settings = {
        "schema_version": "marker-phenotype-settings/v4",
        "fit_scope": values.get("marker_fit_scope"),
        "marker_rules": [
            {
                "marker_id": marker,
                "feature_id": values.get(f"marker_{marker}_feature"),
                **_marker_model_settings(marker, values),
            }
            for marker in markers
        ],
    }
    settings["phenotyping_population"] = raw_values.get("phenotyping_population", context.get("phenotyping_population", {"default": {"kind": "all_segmented", "class_ids": []}, "slide_overrides": {}}))
    try:
        response = {"marker_settings": settings}
        if preview:
            response["preview_population"] = True
        return validate_marker_setup_response(result, response)
    except ValueError as error:
        # Keep the backend's validation while identifying the missing choice
        # for the renderer's existing field navigation and error message.
        suffix = {
            "feature_id must be canonical nonblank text": "feature",
            "marker method is unsupported": "method",
            "choose the current marker's feature, direction, and method before fitting": "method",
        }.get(str(error))
        workflow = context.get("marker_workflow")
        required_markers = (next((marker for marker in markers
            if marker == workflow.get("current_marker_id")), markers[0]),) if workflow else markers
        for marker in required_markers if suffix else ():
            name = f"marker_{marker}_{suffix}"
            if values.get(name) in (None, ""):
                raise ValueError(f"{name} requires a choice") from error
        raise


def _marker_model_settings(marker: str, values: Mapping[str, object]) -> dict[str, object]:
    """Translate visible method controls into the owner's pre-fit settings.

    We retain only constraints for the selected mixture family. Percentile entries
    refer to the complete fitting population; the owner resolves them before sampling.
    Nonfinite endpoints and percentiles outside 0–100 cannot define location bounds.
    """
    prefix = f"marker_{marker}_"
    selected = values.get(prefix + "method")
    method = "gmm" if selected == "gmm_separate" else selected
    constraints = {}
    if method in {"gmm", "gamma"}:
        def endpoint(name):
            """Parse one named bound and retain its control in validation errors."""
            raw = values.get(name)
            if raw is None or raw == "":
                return None
            text = str(raw).strip()
            quantile = text.lower().startswith("q")
            try:
                value = float(text[1:] if quantile else text)
            except ValueError as exc:
                raise ValueError(name + ": Use a finite measurement value or a percentile such as Q05.") from exc
            if not isfinite(value) or quantile and not 0 <= value <= 100:
                raise ValueError(name + ": Enter a finite measurement value or a percentile from Q0 to Q100.")
            return {"quantile": value / 100} if quantile else value

        bounds = [[endpoint(prefix + component + "_" + bound)
                   for bound in ("min", "max")] for component in ("lower", "upper")]
        if any(value is not None for pair in bounds for value in pair):
            constraints = {method: {"mean_bounds" if method == "gmm" else "mode_bounds": bounds}}
    result = {
        "method": method,
        "polarity": "above" if method is not None else None,
        "gaussian_variance": "unshared" if selected == "gmm_separate" else "shared",
        "component_location_constraints": constraints,
    }
    if method == "nimbus":
        result.update(nimbus_cutoff=values.get(prefix + "nimbus_cutoff"), nimbus_distill=False)
        if values.get(prefix + "nimbus_source") == "automatic":
            result.update(nimbus_checkpoint_path=values.get(prefix + "nimbus_checkpoint_path"))
            if values.get(prefix + "nimbus_magnification") is not None:
                result["nimbus_magnification"] = values[prefix + "nimbus_magnification"]
        else:
            result["nimbus_score_path"] = values.get(prefix + "nimbus_score_path")
    return result
