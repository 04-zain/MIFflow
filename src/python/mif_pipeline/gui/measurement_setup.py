"""

Collects the processed channels, cellular compartments and intensity statistics selected for cell measurement.

We present four choices for each processed channel: whether to include it,
its measurement role, one cellular compartment and the intensity measurements.
Protein-marker channels retain their reviewed biomarker assignment. DNA and
autofluorescence channels use their own roles and remain separate from
antibody-target measurements.

The compartment choices are nucleus, cytoplasm, membrane and whole cell.
The supported statistics are Mean, Median, Min, Max, Std.Dev. and Variance.
We combine the selected compartment, processed-channel display name and
statistic into the exact QuPath measurement key, such as "Cell: CD8: Mean".
The native measurement step later calculates those values from the accepted
segmented cells and reports any selected combination that is unavailable.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping

from mif_pipeline.cell_analysis.measurement_setup import (
    validate_measurement_feature_requests, segmentation_compartments,
    validate_segmentation_compartments,
)
from mif_pipeline.cell_analysis.measurement_plan import shape_measurement_choices
from mif_pipeline.gui.interactions import (
    ControlKind,
    FormControl,
    FormModel,
    FormTable,
    FormTableRow,
)
from mif_pipeline.pipeline_core import InteractionReason, StageResult, StageStatus
from mif_pipeline.pipeline_core.interactions import (
    validate_interaction_response,
    validate_measurement_setup_context,
)

_ROLES = ("expression", "dapi", "autofluorescence")
_COMPARTMENTS = ("nucleus", "cytoplasm", "membrane", "cell")
_STATISTICS = ("Mean", "Median", "Min", "Max", "Std.Dev.", "Variance")


def compartment_choices(boundary_source=None):
    labels = {"nucleus": "Nucleus", "cytoplasm": "Cytoplasm", "membrane": "Membrane", "cell": "Whole cell"}
    if boundary_source == "estimated_nuclear_expansion":
        labels.update(cell="Estimated cell region", cytoplasm="Estimated cytoplasm", membrane="Estimated cell boundary")
    elif boundary_source == "learned_cell":
        labels["membrane"] = "Cell boundary"
    return tuple((key, labels[key]) for key in segmentation_compartments(boundary_source))


def available_shape_choices(boundary_source=None):
    choices = []
    for key, label, request in shape_measurement_choices():
        try:
            validate_segmentation_compartments((request,), boundary_source)
        except ValueError:
            continue
        if boundary_source == "estimated_nuclear_expansion":
            label = label.replace("Cell ", "Estimated cell ").replace("Nucleus-to-cell", "Nucleus-to-estimated-cell")
        choices.append((key, label, request))
    return tuple(choices)


def segmentation_measurement_note(boundary_source=None):
    return {
        "learned_cell": "Cellpose supplies cell boundaries without separate nuclei. Nuclear and cytoplasmic measurements are unavailable.",
        "nuclear_outline": "StarDist supplies nuclear outlines. Only nuclear measurements are available; no whole-cell boundary was estimated.",
        "estimated_nuclear_expansion": "StarDist supplies nuclei and an expanded, estimated cell region. Cell, cytoplasm and boundary measurements refer to that estimate.",
    }.get(boundary_source, "Select the image channels and cell measurements to calculate.")


def _initial_channel_role(channel, nuclear_channel_id):
    """Suggest editable roles from the accepted stain or exact marker names."""
    if channel.channel_id == nuclear_channel_id:
        return "dapi"
    names = {str(value).strip().casefold() for value in
             (channel.name, channel.component, channel.fluorophore, channel.biomarker)
             if value}
    if names & {"dapi", "dna", "hoechst", "hoechst 33258", "hoechst 33342", "draq5"}:
        return "dapi"
    if names & {"af", "sample af", "tissue af", "autofluorescence", "autofluorescence signal"}:
        return "autofluorescence"
    return "expression"


def intensity_measurement_choices() -> tuple[tuple[str, str], ...]:
    """Name the intensity calculations consistently in setup and later revisions."""
    return tuple(zip(_STATISTICS, (
        "Mean intensity", "Median intensity", "Minimum intensity", "Maximum intensity",
        "Intensity standard deviation", "Intensity variance",
    )))


def build_measurement_setup_form(result: StageResult) -> FormModel:
    """

    Creates the measurement controls from the processed channels associated with the pending step.

    We first require the current measurement-setup request and named channels.
    Each channel receives an inclusion checkbox, a protein/DNA/autofluorescence
    role selector, one cellular-compartment selector and a selection of the six intensity
    measurements, with Mean selected initially. Its original component, fluorophore and biomarker descriptions
    are retained with the form so its submitted choices can be checked against
    the same image.

    The renderer presents channel inclusion and role first, followed by cellular
    compartment and intensity measurements. Changing pages retains these choices.
    The submission function subsequently requires a complete role, compartment
    and at least one intensity measurement for each included channel.

    Args:
        result: Current measurements StageResult offering configure_measurements,
            with the processed image's reviewed channel definitions.

    Returns:
        FormModel containing the channel controls, recorded image-channel
        descriptions and the request used to validate the completed selection.

    Raises:
        ValueError: The result is not the offered measurement step or a processed
            channel lacks its display name.
    """

    if (
        type(result) is not StageResult
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage != "measurements"
        or result.interaction_reason is not InteractionReason.MEASUREMENT_SETUP_REQUIRED
        or result.allowed_actions != ("configure_measurements",)
    ):
        raise ValueError("measurement setup requires its stage interaction")
    channels = validate_measurement_setup_context(result)
    boundary_source = result.resume_context.get("cell_boundary_source")
    if any(channel.name is None or not channel.name.strip() for channel in channels):
        raise ValueError("measurement setup requires named processed channels")
    controls: list[FormControl] = []
    labels: dict[str, dict[str, str]] = {}
    for index, channel in enumerate(channels):
        prefix = f"channel_{index}"
        display = f"{channel.name} · {channel.biomarker or 'Unknown biomarker'}"
        controls.extend(
            (
                FormControl(
                    f"{prefix}_selected",
                    f"Measure {display}",
                    ControlKind.CHECKBOX,
                    value=False,
                    help_text="Include this channel in cell measurements.",
                ),
                FormControl(
                    f"{prefix}_role",
                    f"{channel.name}: channel role",
                    ControlKind.CHOICE,
                    choices=_ROLES,
                    value=_initial_channel_role(channel, result.resume_context.get("nuclear_channel_id")),
                    help_text="Choose what this channel measures.",
                ),
                FormControl(
                    f"{prefix}_compartment",
                    f"{channel.name}: cell region",
                    ControlKind.CHOICE,
                    choices=tuple(key for key, _ in compartment_choices(boundary_source)),
                    help_text=None,
                ),
                FormControl(
                    f"{prefix}_statistics",
                    f"{channel.name}: intensity measurements",
                    ControlKind.OBJECT_SEQUENCE,
                    choices=_STATISTICS,
                    value=("Mean",),
                    help_text="If performing phenotyping by marker-threshold or unsupervised methods, include Mean or Median.",
                ),
            )
        )
        labels[f"{prefix}_role"] = {
            "expression": "Protein marker",
            "dapi": "DNA",
            "autofluorescence": "Autofluorescence",
        }
        labels[f"{prefix}_compartment"] = dict(compartment_choices(boundary_source))
        labels[f"{prefix}_statistics"] = dict(intensity_measurement_choices())
    controls.extend(FormControl(key, label, ControlKind.CHECKBOX, value=False)
                    for key, label, _request in available_shape_choices(boundary_source))
    return FormModel(
        form_id="measurement_setup",
        title="Cell measurement setup",
        message=segmentation_measurement_note(boundary_source),
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=(
            FormTable(
                "processed_channels",
                ("Channel", "Component", "Fluorophore", "Biomarker"),
                tuple(
                    FormTableRow(
                        channel.channel_id,
                        (
                            channel.name,
                            channel.component or "—",
                            channel.fluorophore or "—",
                            channel.biomarker or "Unknown",
                        ),
                    )
                    for channel in channels
                ),
            ),
        ),
        context={
            "adapter": "measurement_setup",
            "stage_result": result.to_mapping(),
            "choice_labels": labels,
        },
    )


def submit_measurement_setup_form(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """

    Converts the selected channel choices into exact native measurement requests.

    We retrieve the channel definitions recorded with the form and require the
    submitted field names to match its controls. An unchecked channel contributes
    no measurement request. For each checked channel, its role, compartment and
    intensity measurements must be selected from the offered values.

    The measurement name is constructed as:

        compartment.capitalize() + ": " + channel.name + ": " + statistic

    The feature ID combines the UTF-8 hexadecimal channel ID with the compartment
    and statistic, preserving the same selection when channel display names are
    read or shown elsewhere. We then check the complete feature requests against
    the processed channels and their explicit biomarker assignments.

    The returned dictionary contains the entered feature_requests. The response
    validator is called here to check them, while the surrounding workflow adds
    its request and saved-review fields when it accepts that same dictionary.
    Returning those saved-review fields as another set of entered values would
    fail the subsequent submission check.

    Args:
        form: Current measurement FormModel and its recorded StageResult.
        action: Offered configure_measurements action selected by the form.
        raw_values: Inclusion, role, compartment and intensity selections for every
            displayed channel. Unchecked channels may retain empty selections.

    Returns:
        Dictionary containing the ordered feature_requests for the included
        channels, ready for the next workflow response check.

    Raises:
        ValueError: The action or submitted field set is invalid, an included
            channel lacks a complete selection, or the resulting measurement
            role, exact name or biomarker conflicts with the processed image.
    """

    if (
        type(form) is not FormModel
        or form.context.get("adapter") != "measurement_setup"
        or action not in form.allowed_actions
    ):
        raise ValueError("measurement setup action was not offered")
    if not isinstance(raw_values, Mapping) or set(raw_values) != {c.name for c in form.controls}:
        raise ValueError("measurement setup requires exactly its displayed fields")
    result = StageResult.from_mapping(form.context["stage_result"])
    channels = validate_measurement_setup_context(result)
    boundary_source = result.resume_context.get("cell_boundary_source")
    rows: list[dict[str, object]] = []
    for index, channel in enumerate(channels):
        prefix = f"channel_{index}"
        selected = raw_values[f"{prefix}_selected"]
        if type(selected) is not bool:
            raise ValueError(f"{prefix}_selected must be a choice")
        # Only included channels require a role, region and intensity selections.
        # Empty controls for excluded channels remain outside the request.
        if not selected:
            continue
        role = raw_values[f"{prefix}_role"]
        compartment = raw_values[f"{prefix}_compartment"]
        statistics = raw_values[f"{prefix}_statistics"]
        if role not in _ROLES:
            raise ValueError(f"{prefix}_role: Choose a channel role.")
        if compartment not in segmentation_compartments(boundary_source):
            raise ValueError(f"{prefix}_compartment: Choose a cell region.")
        if (not isinstance(statistics, (list, tuple)) or not statistics
                or any(type(value) is not str or value not in _STATISTICS for value in statistics)
                or len(set(statistics)) != len(statistics)):
            raise ValueError(f"{prefix}_statistics: Select at least one intensity measurement.")
        for statistic in _STATISTICS:
            if statistic not in statistics:
                continue
            rows.append(
                {
                    "feature_id": f"intensity-{channel.channel_id.encode('utf-8').hex()}-{compartment}-{statistic}",
                    "measurement_name": f"{compartment.capitalize()}: {channel.name}: {statistic}",
                    "kind": role,
                    "channel_id": channel.channel_id,
                    "biomarker": channel.biomarker if role == "expression" else None,
                    "compartment": compartment,
                    "units": None,
                    "opt_in": False,
                }
            )
    for key, _label, request in available_shape_choices(boundary_source):
        if type(raw_values[key]) is not bool:
            raise ValueError("Select or clear each shape measurement")
        if raw_values[key]:
            rows.append(request)
    validated = validate_measurement_feature_requests(rows, channels=channels)
    validate_segmentation_compartments(validated, boundary_source)
    response = {"feature_requests": [dict(row) for row in validated]}
    # Check these entered values here and return the same inner settings.
    # The surrounding workflow constructs its saved response from them once.
    validate_interaction_response(result, action, response)
    return response
