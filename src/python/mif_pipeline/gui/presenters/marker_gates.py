"""Presentation of owner-provided marker evidence; no threshold fitting.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping
import getpass
import json
from math import isfinite
from uuid import uuid4

from mif_pipeline.gui.interactions import (
    ControlKind,
    FormControl,
    FormModel,
)
from mif_pipeline.orchestration.batch_phenotyping.marker_codec import (
    decode_marker_gate_response,
    encode_marker_gate_response,
)
from mif_pipeline.orchestration.batch_phenotyping.marker_records import (
    MarkerGateResponse,
    MarkerSelection,
    SlideOverride,
)
from mif_pipeline.pipeline_core import StageResult, StageStatus, InteractionReason

# ==============================================================================
#                                  Marker gates
# ==============================================================================


def marker_gate_choices(review: Mapping) -> tuple[str, ...]:
    return tuple(
        item["method"]
        for item in review["candidates"]
        if item["status"] == "available" and (
            item["threshold"] is not None or item.get("outcome") in {"all_positive", "all_negative", "direct_scores"}
        )
    ) + (() if any(item["method"] == "nimbus" for item in review["candidates"]) else ("manual",))


def marker_candidate_guidance(candidate: Mapping, *, can_revise: bool, can_use_manual: bool) -> str:
    """Explain the saved candidate reason and only offer actions present in this review."""
    reason = str(candidate.get("reason") or "No specific reason was recorded")
    descriptions = {
        "gamma_requires_nonnegative_intensities": "Negative measurements are present; a Gamma mixture requires nonnegative intensities.",
        "constant_intensity": "The fitting measurements are constant, so two intensity populations cannot be separated.",
        "insufficient_positive_intensity_variation": "The positive measurements have too little variation to fit two Gamma components.",
        "degenerate_histogram": "The histogram lacks the distinct occupied bins needed for a Triangle threshold.",
        "dominant_peak_tail_geometry_absent": "The histogram has no usable peak-to-tail geometry for a Triangle threshold.",
        "maximum_iterations_reached": "The fit did not converge within the configured iteration limit.",
        "component_locations_coincide": "The fitted component locations coincide, so they do not define separate intensity populations.",
        "component_means_coincide": "The fitted component means coincide, so they do not define separate intensity populations.",
        "no_finite_measurements": "No finite measurements are available in this population.",
        "no_finite_nimbus_scores": "No finite Nimbus scores are available in this population.",
        "No finite Nimbus scores in this scope": "No finite Nimbus scores are available in this population.",
        "nimbus_scores_not_provided": "Nimbus scores were not supplied for this population.",
    }
    if candidate.get("status") == "available":
        return reason.replace("_", " ")
    text = descriptions.get(reason, "The fit failed: " + reason.replace("_", " ") + ".")
    missing_values = reason in {
        "no_finite_measurements", "no_finite_nimbus_scores",
        "No finite Nimbus scores in this scope", "nimbus_scores_not_provided",
    }
    if missing_values:
        text += " Review the selected measurement or score source and the included cells."
    elif can_use_manual:
        text += " Choose Manual if controls or reference cells support an intensity boundary."
    if can_revise:
        text += " Use Back: marker method to change the measurement, method or fitting bounds."
    return text


def project_marker_gate(result: StageResult) -> FormModel:
    """Present the saved candidate for each marker and its chosen fitting scope.

    We retain a saved decision for every image in a per-slide fit. A
    direct Nimbus score review can request a separate intensity review, while
    constant mixture decisions remain selectable without inventing a cutoff.
    """
    context = result.resume_context or {}
    if (
        result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage != "marker_gate_review"
        or result.interaction_reason is not InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED
        or context.get("schema_version") != "marker-gates/v1"
        or result.allowed_actions != ("review_marker_thresholds", "abort")
    ):
        raise ValueError("not an owner marker-gate request")
    controls = []

    if context.get("marker_workflow"):
        controls.append(FormControl("refit_current_marker", "Revise current marker fit", ControlKind.CHECKBOX, value=False, required=False))

    def selection(prefix, label, review):
        choices = marker_gate_choices(review)
        initial = "nimbus_distilled" if "nimbus_distilled" in choices else next(iter(choices), None)
        return [
            FormControl(
                prefix + "_source",
                label + " threshold source",
                ControlKind.CHOICE,
                choices=choices,
                value=initial,
                required=False,
            ),
            FormControl(
                prefix + "_manual",
                label + " manual threshold",
                ControlKind.TEXT,
                required=False,
                help_text="Drag the gate or enter an intensity threshold.",
            ),
            FormControl(prefix + "_adjusted_threshold", "Final intensity gate", ControlKind.TEXT, required=False),
            FormControl(prefix + "_adjusted_score_cutoff", "Final Nimbus score cutoff", ControlKind.TEXT, required=False),
            FormControl(prefix + "_adjustment_source", "Adjustment input", ControlKind.TEXT, required=False),

        ]

    for index, marker in enumerate(context["marker_reviews"]):
        prefix, label = f"m{index}", marker["marker_id"]
        if marker.get("fit_scope") not in {"pooled", "per_slide"}:
            raise ValueError("marker review requires its current fitting scope")
        if marker["fit_scope"] == "per_slide":
            for slide_index, slide in enumerate(marker["per_slide_reviews"]):
                slide_prefix = f"{prefix}_s{slide_index}"
                slide_label = f"{label} / {slide['sample_id']} / {slide['slide_id']}"
                controls.extend(selection(slide_prefix, slide_label, slide["review"]))
                controls.append(FormControl(slide_prefix + "_confirm", "Gate reviewed for this image", ControlKind.CHECKBOX, value=False))
        else:
            controls.extend(selection(prefix, label, marker["pooled_review"]))
            controls.append(FormControl(prefix + "_confirm", "Gate reviewed for this marker", ControlKind.CHECKBOX, value=False))
        if marker.get("configured_method") == "nimbus":
            reviews = [marker["pooled_review"], *(slide["review"] for slide in marker.get("per_slide_reviews", ()))]
            if not any(candidate["method"] == "nimbus_distilled" for review in reviews for candidate in review["candidates"]):
                controls.append(FormControl(
                    prefix + "_nimbus_distill", "How to classify cells for this marker", ControlKind.CHOICE,
                    choices=("score_cutoff", "intensity_review"), value="score_cutoff",
                    help_text="Classify cells using the reviewed Nimbus score cutoff."))
    return FormModel(
        form_id="marker_gate_review",
        title="Review marker thresholds",
        message="Review the distribution and save the selected marker threshold.",
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=(),
        context={
            "adapter": "marker_gates",
            "resume_context": context,
            "stage_result": result.to_mapping(),
            "choice_labels": {control.name: ({
                "gmm": "Two-component Gaussian mixture", "gamma": "Two-component Gamma mixture",
                "triangle": "Triangle", "nimbus": "Nimbus scores", "nimbus_distilled": "Marker intensity",
                "manual": "Manual",
            } if control.name.endswith("_source") else {
                "score_cutoff": "Use the Nimbus score cutoff",
                "intensity_review": "Compare with an intensity threshold",
            }) for control in controls if control.name.endswith(("_source", "_nimbus_distill"))},
        },
    )


def submit_marker_gate(form: FormModel, action: str, values: Mapping[str, object]) -> dict | None:
    """Bind saved decisions to the retained review without fitting new candidates.

    Manual values must be finite and each required image must be saved. We
    preserve the supplied image identities and encode the same typed response used
    by the batch service. We record the local application account and generate
    one review ID for the submitted decisions. These identify the local review;
    they do not establish an authenticated researcher identity. The evidence digest binds the selected fit;
    mixture positivity follows the higher-intensity component without an
    additional acknowledgement or a claim of image inspection.
    """
    if action not in form.allowed_actions:
        raise ValueError("marker action was not offered")
    if action == "abort":
        return {}
    if set(values) == {'revised_marker_settings'}:
        from mif_pipeline.orchestration.batch_phenotyping.marker_preparation import validate_preparation_change
        settings = validate_preparation_change(StageResult.from_mapping(form.context['stage_result']), values['revised_marker_settings'])
        context = form.context['resume_context']
        return json.loads(encode_marker_gate_response(MarkerGateResponse(
            context['validation_domain_id'], context['evidence_sha256'], (), True, settings)))
    if set(values) - {control.name for control in form.controls}:
        raise ValueError("marker response contains fields that were not offered")
    context = form.context["resume_context"]
    if values.get("refit_current_marker") is True:
        if not context.get("marker_workflow"):
            raise ValueError("Refitting requires the current sequential marker setup")
        return json.loads(encode_marker_gate_response(MarkerGateResponse(
            context["validation_domain_id"], context["evidence_sha256"], (), refit_current_marker=True)))

    reviewer = "local-account:" + getpass.getuser()
    record = "marker-review:" + uuid4().hex

    def selected(prefix, review):
        source = values.get(prefix + "_source")
        if source not in marker_gate_choices(review):
            raise ValueError(prefix + "_source must be an available candidate or manual")
        raw = values.get(prefix + "_manual")
        if source == "manual":
            if type(raw) not in {str, int, float} or raw == "":
                raise ValueError(prefix + "_manual must be a finite number")
            try:
                threshold = float(raw)
            except ValueError as exc:
                raise ValueError(prefix + "_manual must be a finite number") from exc
        else:
            if raw not in (None, ""):
                raise ValueError(prefix + "_manual must be empty for a fitted candidate")
            threshold = None
        adjustment = {}
        for suffix in ("adjusted_threshold", "adjusted_score_cutoff"):
            value = values.get(prefix + "_" + suffix)
            if value not in (None, ""):
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(prefix + "_" + suffix + " must be finite") from exc
                if not isfinite(number):
                    raise ValueError(prefix + "_" + suffix + " must be finite")
                adjustment[suffix] = number
        if adjustment:
            adjustment["adjustment_source"] = values.get(prefix + "_adjustment_source") or "numeric"
        return source, threshold, adjustment

    selections = []
    for index, marker in enumerate(context["marker_reviews"]):
        prefix = f"m{index}"
        route = values.get(prefix + "_nimbus_distill", False)
        if type(route) is not bool and route not in ("score_cutoff", "intensity_review"):
            raise ValueError("Choose the Nimbus score cutoff or an intensity threshold review")
        distill = route is True or route == "intensity_review"
        if marker["fit_scope"] == "per_slide":
            slides = []
            for slide_index, slide in enumerate(marker["per_slide_reviews"]):
                slide_prefix = f"{prefix}_s{slide_index}"
                if values.get(slide_prefix + "_confirm") is not True:
                    raise ValueError(f"Save the threshold for each image: {slide['slide_id']} has not been saved")
                slide_source, slide_threshold, adjustment = selected(slide_prefix, slide["review"])
                slides.append(SlideOverride(slide["image_identity"], slide_source, slide_threshold,
                    "Per-slide review", reviewer, record, **adjustment))
            if not slides:
                raise ValueError("Per-slide review requires the represented images")
            source = marker["configured_method"]
            selections.append(MarkerSelection(marker["marker_id"], source, None, reviewer, record,
                slide_overrides=tuple(sorted(slides, key=lambda item: item.image_identity)),
                nimbus_distill=distill))
            continue
        if values.get(prefix + "_confirm") is not True:
            raise ValueError("Save the displayed marker threshold before continuing")
        source, threshold, adjustment = selected(prefix, marker["pooled_review"])
        selections.append(
            MarkerSelection(
                marker["marker_id"],
                source,
                threshold,
                reviewer,
                record,
                nimbus_distill=distill,
                **adjustment,
            )
        )
    response = MarkerGateResponse(
        context["validation_domain_id"], context["evidence_sha256"], tuple(selections)
    )
    checked = decode_marker_gate_response(
        encode_marker_gate_response(response),
        expected_validation_domain_id=context["validation_domain_id"],
        expected_evidence_sha256=context["evidence_sha256"],
        expected_marker_ids=tuple(marker["marker_id"] for marker in context["marker_reviews"]) if context.get("marker_workflow") else context["canonical_marker_ids"],
        allowed_slide_ids=tuple(sorted({slide["image_identity"] for marker in context["marker_reviews"] for slide in marker.get("per_slide_reviews", ())})),
        allowed_section_ids=tuple(
            sorted(
                {
                    section["section_identity"]
                    for marker in context["marker_reviews"]
                    for section in marker["per_section_reviews"]
                }
            )
        ),
    )
    return json.loads(encode_marker_gate_response(checked))
