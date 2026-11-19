"""Translate a stage review into explicit form fields, tables and response data.

The presenter uses the request's current subjects, channels, regions and model settings to
build its controls. Response construction resolves the selected training procedure and exact
candidate grid, or delegates the other stage's values to its validator. It contains
presentation decisions rather than a parallel scientific implementation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.gui.reference_copy import SPECTRAL_CHOICE_LABELS

from mif_pipeline.gui.interactions import (
    ControlKind,
    FormControl,
    FormModel,
    FormTable,
    FormTableRow,
    build_roi_interaction_form,
    decode_control_value,
    decode_form_values,
    submit_form_values,
    _decode_json_value,
)
from mif_pipeline.gui.tissue_setup import build_tissue_setup_form, submit_tissue_setup_form
from mif_pipeline.gui.segmentation_setup import (
    build_segmentation_setup_form,
    submit_segmentation_setup_form,
)
from mif_pipeline.gui.measurement_setup import (
    build_measurement_setup_form,
    submit_measurement_setup_form,
)
from mif_pipeline.gui.supervised_setup import supervised_controls, supervised_selection
from mif_pipeline.gui.secondary_setup import build_secondary_setup_form, submit_secondary_setup_form
from mif_pipeline.orchestration import (
    bind_reference_unmixing_response,
    bind_stage1_response,
)
from mif_pipeline.orchestration.batch_training import (
    BatchTrainingError,
    EvaluationIntent,
    TRAINING_PARENT_NODE_KINDS,
    TRAINING_PLAN_REVIEW_NODE_KINDS,
    validate_training_plan_selection_response,
)
from mif_pipeline.orchestration.batch_training.plan_selection import (
    validate_training_plan_selection_request,
    TRAINING_PLAN_SELECTION_SCHEMA_VERSION, TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION,
)
from mif_pipeline.orchestration.batch_training.qupath_rtrees import (
    QuPathPixelRTreesConfiguration,
    QuPathRTreesParameters,
)
from mif_pipeline.orchestration.batch_training.qupath_svm import (
    QuPathPixelSvmConfiguration,
    QuPathSvmParameters,
)
from mif_pipeline.orchestration.batch_interactions import (
    BatchInteractionGroup,
    BatchInteractionResponseRequest,
    BatchTargetSelection,
)
from mif_pipeline.pipeline_core import InteractionReason, StageResult, StageStatus
from mif_pipeline.pipeline_core.config import ChannelSpec, PrimaryClassifierSettings
from .marker_gates import project_marker_gate, submit_marker_gate
from mif_pipeline.gui.marker_setup import build_marker_setup_form, submit_marker_setup_form
from mif_pipeline.gui.marker_combinations import build_marker_combination_form, submit_marker_combination_form

_UNSUPPORTED_MESSAGE = "unsupported context version; refresh/update required"


# ==============================================================================
#                                    Reviews
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchInteractionForm:
    group_id: str
    group_sha256: str
    expected_state_head_sha256: str
    representative_target_id: str
    target_ids: tuple[str, ...]
    form: FormModel


@dataclass(frozen=True, slots=True)
class UnsupportedBatchInteraction:
    stage: str
    reason: str
    action_schema_version: str
    context_schema_version: str
    target_ids: tuple[str, ...]
    message: str = _UNSUPPORTED_MESSAGE


@dataclass(frozen=True, slots=True)
class _Projector:
    project: Callable[..., FormModel]
    submit: Callable[[FormModel, str, Mapping[str, object]], dict[str, object] | None]


from mif_pipeline.gui.interactions import submit_batch_form_values

_ROI_PROJECTOR = _Projector(build_roi_interaction_form, submit_batch_form_values)
_TARGET_SPECIFIC_SCHEMAS = frozenset(
    {
        "primary-count-review/v1",
        "batch-marker-setup/v1",
        "training-fit-review/v4",
        "training-fit-review/v5",
        "tissue-detector-setup-interaction/v1",
        "segmentation-setup-interaction/v1",
        "measurement-setup-interaction/v1",
        "marker-gates/v1",
        "warpy-registration-interaction/v1",
        "mif-batch-stage1-interaction/v1",
        "mif-batch-reference-unmixing-interaction/v2",
    }
)


def _owner_service_context(result: StageResult, *, stage: str, schema: str) -> Mapping[str, object]:
    context = result.resume_context or {}
    owner = context.get("service_context")
    if (
        result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage != stage
        or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
        or result.allowed_actions != ("accept",)
        or context.get("schema_version") != schema
        or not isinstance(owner, Mapping)
        or owner.get("schema_version") != schema
    ):
        raise ValueError("preprocessing form has a foreign owner context")
    return owner


def _project_organization(result: StageResult) -> FormModel:
    owner = _owner_service_context(result, stage="inspection", schema="mif-batch-acquisition-organization/v1")
    return FormModel(form_id="source_plane_organization", title="Imaging cycles",
        message="Confirm how the acquired planes are grouped into cycles. Fluorophores and markers are assigned in Channels and markers.",
        allowed_actions=result.allowed_actions,
        controls=(FormControl("acquisition_organization", "Source-plane organization", ControlKind.OBJECT_MAPPING,
            value=dict(layout=owner["layout"], registered=owner.get("registered", False), channel_cycle_values=owner["channel_cycle_values"])),),
        context={"adapter": "batch_acquisition_organization", "stage_result": result.to_mapping(),
                 "organization_owner": owner})


def _submit_organization(form: FormModel, action: str, raw_values: Mapping[str, object]) -> dict[str, object]:
    from mif_pipeline.orchestration.batch_acquisition_organization import bind_organization_response
    values = decode_form_values(form, action, raw_values)
    return bind_organization_response(StageResult.from_mapping(form.context["stage_result"]),
        action, values["acquisition_organization"])


def _project_stage1(result: StageResult) -> FormModel:
    owner = _owner_service_context(
        result,
        stage="channel_review",
        schema="mif-batch-stage1-interaction/v1",
    )
    status = owner.get("workflow_status")
    rows = owner.get("channel_review_rows")
    if status not in {"needs_biomarkers", "needs_channel_name_replacement"} or not isinstance(
        rows, Sequence
    ):
        raise ValueError("Stage-1 form lacks interactive owner rows")
    table_rows = tuple(
        FormTableRow(
            str(row["row_id"]),
            (
                row["channel_id"],
                row["channel_name"],
                row["fluorophore"],
                row["component_name"],
                row["assignment"],
                row["biomarker"],
            ),
        )
        for row in rows
    )
    observed = owner["source_spectral_state"]
    controls: list[FormControl] = [FormControl(
        "channel_content", "Channel content", ControlKind.CHOICE,
        choices=("raw", "unmixed") if observed == "unknown" else (observed,),
        value=None if observed == "unknown" else observed,
        help_text=None,
    )]
    if observed != "raw":
        for index, row in enumerate(rows):
            label = f"{row['channel_id']} ({row['channel_name'] or 'unnamed'})"
            controls.extend(
                (
                    FormControl(
                        f"stage1_{index}_assignment",
                        f"{label}: assignment",
                        ControlKind.CHOICE,
                        required=False,
                        choices=("assigned", "none"),
                        value=(
                            row["assignment"] if row["assignment"] in {"assigned", "none"} else None
                        ),
                    ),
                    FormControl(
                        f"stage1_{index}_biomarker",
                        f"{label}: biomarker",
                        ControlKind.TEXT,
                        required=False,
                        value=row["biomarker"],
                        help_text="Leave blank only when the assignment is none.",
                    ),
                    FormControl(
                        f"stage1_{index}_component_name",
                        f"{label}: channel label",
                        ControlKind.TEXT,
                        required=False,
                        value=row["component_name"],
                        help_text="A label identifies the channel; recorded dye metadata is retained separately.",
                    ),
                )
            )
    if observed != "unmixed":
        controls.append(FormControl(
            "component_values", "Output components and markers", ControlKind.OBJECT_SEQUENCE,
            required=False, value=owner["component_review_rows"],
            help_text=None,
        ))
    controls.append(FormControl("nuclear_stain", "Nuclear stain", ControlKind.TEXT,
        required=False, value=owner.get("nuclear_stain"),
        help_text="Select the component containing the DNA stain."))
    controls.append(
        FormControl(
            "replace_channel_names_with_biomarkers",
            "Use marker names in the processed image",
            ControlKind.CHECKBOX,
            value=owner["replace_channel_names_with_biomarkers"] if owner["replace_channel_names_with_biomarkers"] is not None else True,
            help_text=None,
        )
    )
    return FormModel(
        form_id=f"batch_stage1_{status}",
        title="Channels and markers",
        message=(
            "Review channel labels and marker assignments. Specify fluorescence components for raw spectral bands. "
            "Values recorded in the image metadata are filled in automatically."
            + (f" The source has {len(rows)} acquired measurement bands." if observed == "raw" else "")
        ),
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=(
            ((FormTable("stage1_component_rows", ("component_name", "assignment", "biomarker"),
                tuple(FormTableRow(str(i), (row["component_name"], row["assignment"], row["biomarker"]))
                      for i, row in enumerate(owner["component_review_rows"])), title="Output components"),)
             if observed != "unmixed" else ())
            + ((FormTable("stage1_channel_rows", ("channel_id", "channel_name", "fluorophore", "component_name", "assignment", "biomarker"), table_rows),)
               if observed != "raw" else ())
        ),
        context={"adapter": "batch_stage1", "stage_result": result.to_mapping(),
                 "acquisition_cycle_review_available": (not owner.get("channel_cycle_values")
                    or any(row.get("cycle") is None for row in owner["channel_cycle_values"])),
                 "component_af_retained": owner["component_af_retained"],
                 "component_cycles": owner.get("component_cycles", ()),
                 "accepted_channel_cycles": owner.get("channel_cycle_values", ()),
                 "source_channel_cycles": owner.get("source_channel_cycles"),
                 "nuclear_stain_reviewed": owner.get("nuclear_stain_reviewed", False),
                 "stage1_channel_rows": rows,
                 "choice_labels": {"channel_content": {"raw": "Raw spectral bands", "unmixed": "Separated components"}}},
    )


def _submit_stage1(
    form: FormModel, action: str, raw_values: Mapping[str, object]
) -> dict[str, object]:
    result = StageResult.from_mapping(form.context["stage_result"])
    content = raw_values.get("channel_content")
    values = dict(raw_values)
    if content == "raw":
        values = {name: value for name, value in values.items() if not name.startswith("stage1_")}
    elif content == "unmixed":
        values.pop("component_values", None)
    decoded = decode_form_values(form, action, values)
    owner = result.resume_context["service_context"]
    biomarker_values = {}
    if content == "unmixed":
        for index, row in enumerate(owner["channel_review_rows"]):
            biomarker = decoded.get(f"stage1_{index}_biomarker")
            biomarker_values[row["row_id"]] = {
                "assignment": decoded.get(f"stage1_{index}_assignment"),
                "biomarker": biomarker if biomarker else None,
                "component_name": decoded.get(f"stage1_{index}_component_name"),
            }
    decoded = {
        "biomarker_values": biomarker_values,
        "component_values": decoded.get("component_values", []) if content == "raw" else [],
        "replace_channel_names_with_biomarkers": decoded["replace_channel_names_with_biomarkers"],
        "channel_content": content,
        "nuclear_stain": decoded.get("nuclear_stain"),
    }
    return bind_stage1_response(result, action, decoded)


def _reference_start_control(field: Mapping[str, object]) -> FormControl:
    from mif_pipeline.orchestration.batch_preprocessing_support import unmixing_start_defaults
    from mif_pipeline.gui.terminology import field_label
    from mif_pipeline.gui.control_guidance import control_help

    name, kind = field["name"], field["kind"]
    defaults = unmixing_start_defaults()
    control_kind = {
        "enum": ControlKind.CHOICE,
        "nullable_enum": ControlKind.CHOICE,
        "positive_integer_pair": ControlKind.OBJECT_SEQUENCE,
        "positive_number": ControlKind.NUMBER,
        "positive_integer": ControlKind.INTEGER,
        "boolean": ControlKind.CHECKBOX,
    }.get(kind)
    if control_kind is None:
        raise ValueError("reference start field kind is unsupported")
    choices = ("raw", "unmixed", "isolated") if name == "spectral_state_override" else tuple(field["choices"])
    return FormControl(
        name,
        field_label(name),
        control_kind,
        required=True,
        choices=choices,
        value=defaults.get(name),
        help_text=control_help(name),
    )


def _reference_start_controls(field: Mapping[str, object]) -> tuple[FormControl, ...]:
    from mif_pipeline.orchestration.batch_preprocessing_support import unmixing_start_defaults
    from mif_pipeline.gui.terminology import field_label
    from mif_pipeline.gui.control_guidance import control_help

    if field["name"] == "replace_channel_names_with_biomarkers":
        return ()
    if field["name"] == "af_behavior":
        return ()
    if field["name"] == "chunk_size" and field["kind"] == "positive_integer_pair":
        width, height = unmixing_start_defaults()["chunk_size"]
        return tuple(
            FormControl(
                name,
                field_label(name),
                ControlKind.INTEGER,
                value=value,
                help_text=control_help(name),
            )
            for name, value in (("chunk_width", width), ("chunk_height", height))
        )
    return (_reference_start_control(field),)


def _reference_operation_control(field: Mapping[str, object]) -> FormControl:
    from mif_pipeline.gui.reference_copy import FIELD_COPY

    label, help_text = FIELD_COPY.get(field["name"], (field["label"], field["help_text"]))
    kind = field.get("value_kind")
    control_kind = {
        "text": ControlKind.TEXT,
        "enum": ControlKind.CHOICE,
        "locator": ControlKind.LOCAL_FILE,
        "identifier": ControlKind.CHOICE,
        "ordered_identifier_sequence": ControlKind.OBJECT_SEQUENCE,
        "enum_mapping": ControlKind.OBJECT_MAPPING,
        "reason_code_sequence": ControlKind.OBJECT_SEQUENCE,
        "primitive_mapping": ControlKind.JSON_OBJECT,
        "primitive_list": ControlKind.OBJECT_SEQUENCE,
    }.get(kind)
    if control_kind is None:
        raise ValueError("reference operation field kind is unsupported")
    if kind == "identifier" and not field.get("identifier_choices"):
        control_kind = ControlKind.TEXT
    choices = tuple(
        field.get("mapping_key_choices") or ()
        if kind == "enum_mapping"
        else field.get("identifier_choices")
        or field.get("value_choices")
        or field.get("mapping_key_choices")
        or field.get("mapping_value_choices")
        or ()
    )
    value: object = None
    if control_kind is ControlKind.OBJECT_SEQUENCE:
        value = ()
    elif control_kind is ControlKind.OBJECT_MAPPING:
        value = {key: None for key in choices}
    elif control_kind is ControlKind.JSON_OBJECT:
        value = {}
    return FormControl(
        field["name"],
        label,
        control_kind,
        required=field["required"],
        choices=choices,
        value=value,
        help_text=help_text,
    )


def _reference_operation_controls(
    field: Mapping[str, object],
    field_index: int,
) -> tuple[FormControl, ...]:
    kind = field.get("value_kind")
    if kind == "enum_mapping":
        keys = tuple(field.get("mapping_key_choices") or ())
        choices = tuple(field.get("value_choices") or ())
        if not keys or not choices:
            raise ValueError("reference enum mapping lacks offered keys or values")
        return tuple(
            FormControl(
                f"reference_mapping_{field_index}_{index}_value",
                f"{field['label']}: {key.replace('_', ' ').title()}",
                ControlKind.CHOICE,
                choices=choices,
                help_text=field["help_text"],
            )
            for index, key in enumerate(keys)
        )
    if kind in {"identifier_mapping", "identifier_multimapping"}:
        keys = tuple(field.get("mapping_key_choices") or ())
        if not keys:
            raise ValueError("reference mapping lacks offered key identifiers")
        choices = tuple(field.get("mapping_value_choices") or ())
        source = field.get("mapping_value_source_field")
        return tuple(
            FormControl(
                f"reference_mapping_{field_index}_{index}_value",
                f"{field['label']}: {key}",
                (ControlKind.OBJECT_SEQUENCE if kind == "identifier_multimapping"
                 else ControlKind.CHOICE if choices else ControlKind.TEXT),
                required=True,
                choices=choices,
                help_text=(
                    field["help_text"] + (f" Select a value from {source}." if source else "")
                ),
            )
            for index, key in enumerate(keys)
        )
    if kind == "identifier_disposition_mapping":
        identifiers = tuple(field.get("identifier_choices") or ())
        if not identifiers:
            raise ValueError("reference disposition lacks offered identifiers")
        choices = tuple(field.get("value_choices") or ())
        controls: list[FormControl] = []
        for index, identifier in enumerate(identifiers):
            controls.extend(
                (
                    FormControl(
                        f"reference_mapping_{field_index}_{index}_disposition",
                        f"{field['label']}: {identifier}",
                        ControlKind.CHOICE,
                        required=False,
                        choices=choices,
                        help_text="Leave blank when this identifier is not selected.",
                    ),
                    FormControl(
                        f"reference_mapping_{field_index}_{index}_component",
                        f"{identifier}: AF spectrum" if field['name'] == 'roi_reviews' else f"{identifier}: component identity",
                        ControlKind.TEXT,
                        required=False,
                        value='AF' if field['name'] == 'roi_reviews' else None,
                        help_text=('Regions with the same name form one AF spectrum. Use different names only for deliberately distinct AF shapes.'
                                   if field['name'] == 'roi_reviews' else ''),
                    ),
                )
            )
        return tuple(controls)
    return (_reference_operation_control(field),)


def _has_manifest_library_candidate(owner: Mapping[str, object]) -> bool:
    return all(
        type(owner.get(field)) is str and bool(owner[field])
        for field in (
            "candidate_spectral_library_artifact_id",
            "candidate_spectral_library_artifact_ref_sha256",
            "candidate_spectral_library_compatibility_signature_sha256",
        )
    )


def _project_reference(result: StageResult, *, option_value: str | None = None,
                       saved_spectral_source: Mapping[str, object] | None = None) -> FormModel:
    owner = _owner_service_context(
        result,
        stage="reference_unmixing",
        schema="mif-batch-reference-unmixing-interaction/v2",
    )
    kind = owner.get("interaction_kind")
    if kind == "configure_reference_unmixing":
        if option_value is not None:
            raise ValueError("reference start has no operation option")
        fields = owner.get("configuration_fields")
        if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
            raise ValueError("reference start lacks declared configuration fields")
        controls = [control for field in fields for control in _reference_start_controls(field)]
        controls.sort(key=lambda c: c.name != "spectral_state_override")
        state = owner.get("source_spectral_state", "unknown")
        controls = [
            (
                replace(c, value=state if state in {"raw", "unmixed"} else None)
                if c.name == "spectral_state_override"
                else c
            )
            for c in controls
        ]
        from mif_pipeline.gui.calibration_entry import CORRECTION_CHOICES

        calibration = owner.get("calibration_request")
        if calibration is not None:
            from mif_pipeline.gui.calibration_entry import calibration_controls

            controls.extend(calibration_controls(calibration))
        initial = owner.get("initial_configuration", {})
        mapping = owner.get("measurement_band_mapping", {"bands": [], "components": []})
        component_choices = tuple(str(row["index"]) for row in mapping["components"])
        cyclic_mapping = len({band["cycle"] for band in mapping["bands"] if band["cycle"] is not None}) > 1
        component_labels = {str(row["index"]): row["name"] + (
            " · " + row["marker"] if row["marker"] else " · No marker") + (
            f" · Cycle {row['cycle'] + 1}" if cyclic_mapping and row.get("cycle") is not None
            and " [cycle " not in row["name"] else "")
            for row in mapping["components"]}
        saved_mapping = initial.get("measurement_band_components", ())
        for i, band in enumerate(mapping["bands"]):
            selected = saved_mapping[i] if i < len(saved_mapping) else band["component_index"]
            controls.append(FormControl(f"measurement_band_component_{i}", band["name"],
                ControlKind.CHOICE, required=True, choices=component_choices,
                value=None if selected is None else str(selected)))
        if initial:
            chunk = initial.get("chunk_size", ())
            values = dict(initial)
            if len(chunk) == 2:
                values.update(chunk_width=chunk[0], chunk_height=chunk[1])
            controls = [replace(control, value=values[control.name])
                        if control.name in values else control for control in controls]
        source = owner.get("spectral_state_source", "not recorded")
        state_message = {
            "raw": "Recorded image content: raw measurement bands. Confirm whether the fluorophore signals are mixed or isolated by those bands.",
            "unmixed": "Recorded image status: separated channels.",
            "unknown": "Image status is not recorded. Confirm whether unmixing is needed.",
        }[state]
        copy_state_message = state_message
        if calibration is not None and calibration.get("correction_mode") == "as_stored":
            state_message += (
                " The image metadata records calibrated intensities. "
                "Reference spectra and batch images must use the same imaging settings "
                "and correction procedure, including identical exposure times for each channel."
            )
        return FormModel(
            form_id="batch_reference_start",
            title="Spectral unmixing",
            message=state_message,
            allowed_actions=result.allowed_actions,
            controls=tuple(controls),
            context={
                "adapter": "batch_reference_start",
                "stage_result": result.to_mapping(),
                "source_spectral_state": state,
                "spectral_state_source": source,
                "copy_state_message": copy_state_message,
                "calibration_request": calibration,
                "measurement_band_mapping": mapping,
                "choice_labels": {
                    "calibration_correction_mode": dict(CORRECTION_CHOICES),
                    **{f"measurement_band_component_{i}": component_labels for i in range(len(mapping["bands"]))},
                },
            },
        )
    if kind != "reference_unmixing_operation":
        raise ValueError("reference operation kind is foreign")
    state = owner.get("operation_state")
    if not isinstance(state, Mapping):
        raise ValueError("reference operation lacks bounded owner state")
    from mif_pipeline.orchestration.reference_region_selection import REGION_CHECKPOINTS, REGION_OPTION, region_requirement
    source_preparation_options = {'prepare_shared_control', 'accept_shared_control_preparation',
                                  'prepare_stained_reference', 'assign_shared_control_sets'}
    if (state.get('checkpoint') in REGION_CHECKPOINTS
            and REGION_OPTION not in state.get('offered_actions', ())
            and not source_preparation_options.intersection(state.get('offered_actions', ()))):
        # Add the current compound presentation to a persisted legacy request.
        # The unchanged checkpoint is still validated against the live service.
        state = {**state, 'offered_actions': [REGION_OPTION, *state['offered_actions']],
                 'operation_requirements': [region_requirement().to_mapping(), *state['operation_requirements']]}
        owner = {**owner, 'operation_state': state}
        result = replace(result, resume_context={**result.resume_context, 'service_context': owner})
    offered = state.get("offered_actions")
    requirements = state.get("operation_requirements")
    if (
        not isinstance(offered, Sequence)
        or isinstance(offered, (str, bytes))
        or not isinstance(requirements, Sequence)
        or isinstance(requirements, (str, bytes))
    ):
        raise ValueError("reference operation options are malformed")
    # Leaving this review preserves its pending checkpoint. A terminal owner
    # abort is not a pause and must not be offered as a navigation choice.
    offered = tuple(value for value in offered
                    if value not in {"abort", "abort_workflow", "retry_final_run_basis"}
                    and (value != 'revise_component_preparation' or option_value == value))
    if state.get("checkpoint") == "basis_source" and "curated_reference_library" in offered:
        # One visible importer; the file's typed format selects its scientific path.
        offered = tuple(value for value in offered if value != "import_reference_library")
        if option_value == "import_reference_library":
            option_value = "curated_reference_library"
    requirements = tuple(item for item in requirements
                         if item.get("option_value") in offered)
    pending = state.get("review_summary", {}).get("pending_reference_input")
    if option_value is None and state.get("checkpoint") == "basis_source" and owner.get("revision_library_locator"):
        option_value = "curated_reference_library"
    if pending is not None:
        option_value = "complete_reference_input:" + pending["pending_id"]
    if option_value is None and state.get("checkpoint") == "reference_control_files":
        option_value = next((value for value in (
            'accept_reference_control_files_with_source_preparation', 'accept_reference_control_files')
            if value in offered), None)
    if (option_value is None and state.get("checkpoint") == "fluorophore_consensus"
            and "accept_fluorophore_consensus" in offered):
        option_value = "accept_fluorophore_consensus"
    if option_value is None:
        default_review = {"signal_dark_measurements": "accept_signal_dark_measurements",
                          "extraction_qc": "accept_extraction_qc"}.get(state.get("checkpoint"))
        if default_review in offered:
            option_value = default_review
    if (option_value is None and state.get("checkpoint") == "final_run_basis"
            and "accept_final_run_basis" in offered):
        option_value = "accept_final_run_basis"
    if (option_value is None and state.get("checkpoint") == "target_af_control_review"
            and state.get("review_summary", {}).get("spectral_review") is not None
            and "accept_control_target_af" in offered):
        option_value = "accept_control_target_af"
    if (option_value is None and state.get("checkpoint") == "target_af"
            and state.get("review_summary", {}).get("target_af_groups") is not None
            and "review_reference_control_groups" in offered):
        option_value = "review_reference_control_groups"
    from mif_pipeline.gui.reference_regions import REGION_CHECKPOINTS, reference_region_form, reference_file_form
    if option_value is None and state.get("checkpoint") in REGION_CHECKPOINTS:
        option_value = next((value for value in ('accept_shared_control_preparation',
            'assign_shared_control_sets', 'prepare_shared_control', 'prepare_stained_reference',
            'accept_reference_regions') if value in offered), None)
    matches = tuple(
        requirement
        for requirement in requirements
        if isinstance(requirement, Mapping) and requirement.get("option_value") == option_value
    )
    if option_value is not None and (option_value not in offered or len(matches) != 1):
        raise ValueError("reference operation option is not offered")
    controls: list[FormControl] = [
        FormControl(
            "option_value",
            (
                "Spectral references"
                if state.get("checkpoint") == "basis_source"
                else "Reference controls"
                if state.get("checkpoint") == "control_strategy"
                else "Choose the next step"
            ),
            ControlKind.CHOICE,
            choices=tuple(offered),
            value=option_value,
        )
    ]
    settings_paths, settings_groups, settings_constants = {}, {}, {}
    if option_value is not None:
        fields = matches[0].get("form_fields")
        if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
            raise ValueError("reference operation fields are malformed")
        for field_index, field in enumerate(fields):
            if (
                option_value in {"import_reference_library", "curated_reference_library"}
                and _has_manifest_library_candidate(owner)
                and field["name"] == "library_locator"
            ):
                continue
            if matches[0]["operation_action"] == "complete_reference_input" and field["name"] == "calibration":
                from mif_pipeline.gui.calibration_entry import calibration_controls
                controls.extend(calibration_controls(pending["calibration_inventory"], fixed_correction_mode=pending["correction_mode"]))
                continue
            if field["name"] != "reviewer_id":
                field_controls = _reference_operation_controls(field, field_index)
                if option_value in {"import_reference_library", "curated_reference_library"} and field["name"] == "library_locator" and owner.get("revision_library_locator"):
                    field_controls = tuple(replace(c, value=owner["revision_library_locator"]) for c in field_controls)
                controls.extend(field_controls)
        from mif_pipeline.gui.reference_settings import reference_settings

        setting_controls, settings_paths, settings_groups, settings_constants = reference_settings(
            matches[0]["operation_action"],
            state.get("checkpoint"),
            len(state.get("measurement_ids", ())),
        )
        controls.extend(setting_controls)
    plain_text_sequences = tuple(
        field["name"]
        for field in (matches[0]["form_fields"] if option_value is not None else ())
        if field["value_kind"] == "reason_code_sequence"
        or (
            field["value_kind"] == "ordered_identifier_sequence"
            and not field.get("identifier_choices")
        )
    )
    from mif_pipeline.gui.reference_copy import checkpoint_copy, option_copy

    saved_bands = state.get("review_summary", {}).get("band_review")
    if saved_bands and saved_bands.get("signal_measurement_id") is not None:
        controls = [replace(control, value=saved_bands[control.name])
                    if control.name in {"signal_measurement_id", "dark_measurement_ids"} else control
                    for control in controls]

    saved_policy = saved_bands.get("purification_policy") if saved_bands else None
    if saved_policy is not None and "purification_policy" in settings_constants:
        settings_constants["purification_policy"] = dict(saved_policy)
        controls = [replace(control, value=saved_policy[settings_paths[control.name][-1]])
                    if control.name in settings_paths
                    and settings_paths[control.name][0] == "purification_policy" else control
                    for control in controls]

    af_groups = (state.get("review_summary", {}).get("target_af_groups")
                 if option_value == "review_reference_control_groups" else None)
    if af_groups is not None:
        controls = [replace(control, value=af_groups["selection"])
                    if control.name == "target_af_groups" else control for control in controls]
    title, message = checkpoint_copy(state.get("checkpoint"))
    failures = tuple(
        {field["name"]: field["value"] for field in evidence["summary"]}
        for evidence in state.get("review_summary", {}).get("evidence", ())
        if evidence["kind"] == "reference_extraction_failure"
    )
    if state.get("checkpoint") == "basis_source" and option_value == "curated_reference_library":
        title = "Import or enter a spectral library"
        message = "Import reference values or enter them in the table, then review their normalization and imaging settings."
    if option_value is not None and state.get("checkpoint") not in {"basis_source", "control_strategy"}:
        message = option_copy(matches[0])[1]
    if option_value is not None and matches[0]["operation_action"] == "complete_reference_input":
        title = "Review reference imaging settings"
        role = pending["fluorophore_id"] or "unstained control"
        correction_label = {
            "full": "Full intensity correction",
            "exposure_only": "Exposure-only correction",
            "as_stored": "Use recorded calibrated intensities",
        }[pending["correction_mode"]]
        message = (f"{Path(pending['locator']).name} — {role}. {correction_label}. "
                   + pending["acquisition_condition"])
    if state.get("checkpoint") == "basis_source" and owner.get("revision_library_locator"):
        message += " Your previously accepted library is selected."
    if state.get("checkpoint") == "extraction_qc":
        message = "Review each reference spectrum, then choose the regions to include."
    if af_groups is not None:
        title = "Final autofluorescence reference"
        message = ""
    if failures:
        explanations = " ".join(dict.fromkeys(item["reason"] for item in failures))
        message = explanations + " Reselect the affected regions before continuing."
    form = FormModel(
        form_id="batch_reference_operation",
        title=title,
        message=message,
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=(FormTable(
            "reference_extraction_failures", ("Region", "Result"),
            tuple(FormTableRow(str(index), (item["roi_id"], "Failed extraction"))
                  for index, item in enumerate(failures)),
        ),) if failures else (),
        context={
            "adapter": "batch_reference_operation",
            "back_option": ("reselect_basis_source" if "reselect_basis_source" in offered
                            else "reselect_target_af_source" if "reselect_target_af_source" in offered else None),
            "back_label": ("Edit AF groups" if "reselect_target_af_source" in offered and state.get("review_summary", {}).get("target_af_review") is not None
                           else "Back: autofluorescence source" if "reselect_target_af_source" in offered
                           else "Back: spectral references"),
            "saved_spectral_source": saved_spectral_source,
            "spectral_review": state.get("review_summary", {}).get("spectral_review"),
            "reference_component_progress": state.get("review_summary", {}).get("reference_component_progress"),
            "reference_preparation_progress": state.get("review_summary", {}).get("reference_preparation_progress"),
            "consensus_review": state.get("review_summary", {}).get("consensus_review"),
            "extraction_review": state.get("review_summary", {}).get("extraction_review"),
            "target_af_review": state.get("review_summary", {}).get("target_af_review"),
            "target_af_groups": af_groups,
            "target_af_combined_review": (state.get("checkpoint") == "target_af_control_review"
                and state.get("review_summary", {}).get("spectral_review") is not None),
            "af_control_set_review": (
                None if (state.get("review_summary", {}).get("control_preparation") or
                         state.get("review_summary", {}).get("shared_control_preparation", {})).get("accepted")
                else state.get("review_summary", {}).get("af_control_set_review")
            ),
            "band_review": state.get("review_summary", {}).get("band_review"),
            "reference_workspace": option_value in {"accept_signal_dark_measurements", "accept_extraction_qc",
                                                     "retry_extraction", "accept_fluorophore_consensus"},
            "reference_navigation_options": tuple(value for value in offered if value in {
                "reselect_reference_rois", "revise_signal_dark_measurements"})
                if option_value in {"accept_signal_dark_measurements", "accept_extraction_qc",
                                    "retry_extraction", "accept_fluorophore_consensus"} else (),
            "spectral_input": state.get("review_summary", {}).get("spectral_input"),
            "manifest_library_selected": _has_manifest_library_candidate(owner),
            "pending_reference_input": pending,
            "reference_settings_paths": settings_paths,
            "reference_settings_groups": settings_groups,
            "reference_settings_constants": settings_constants,
            "stage_result": result.to_mapping(),
            "selected_option": option_value,
            "starts_unmixing": (option_value in {"accept_final_run_basis", "accept_control_target_af"}
                and state.get("review_summary", {}).get("spectral_review") is not None
                and not state.get("review_summary", {}).get("shared_reference")),
            "choice_labels": {
                "option_value": {r["option_value"]: option_copy(r)[0] for r in requirements},
                "signal_measurement_id": dict(state.get("review_summary", {}).get("measurement_labels", {})),
                "dark_measurement_ids": dict(state.get("review_summary", {}).get("measurement_labels", {})),
                "acquisition_confirmation": {"confirmed": "Imaging settings match"},
                "spectral_state": {"raw": "Original acquired spectral bands (not unmixed components)"},
                **SPECTRAL_CHOICE_LABELS,
            },
            "plain_text_sequences": plain_text_sequences,
            "plain_text_sequence_placeholders": {
                "selected_roi_ids": "Enter ROI ID",
                "reason_codes": "Enter reason code",
            },
        },
    )
    from mif_pipeline.gui.reference_sources import reference_source_form
    form = reference_region_form(reference_source_form(reference_file_form(form)))
    shared = state.get("review_summary", {}).get("shared_reference")
    if shared and shared.get("cycle") is not None:
        form = replace(form, title=f"{form.title} · Cycle {shared['cycle']}")
    return form


def _submit_reference(
    form: FormModel, action: str, raw_values: Mapping[str, object]
) -> dict[str, object]:
    result = StageResult.from_mapping(form.context["stage_result"])
    source_workspace = form.context.get('reference_region_workspace', {})
    if (source_workspace.get('source_review') and source_workspace.get('composition_enabled')
            and form.context.get('selected_option') == 'accept_shared_control_preparation'):
        original = [list(row['roi_ids']) for row in source_workspace['review']['sets']]
        if raw_values.get('control_sets', original) != original:
            # Re-project the real offered action so its own fields and decoder
            # bind the edit. Acceptance never carries unsaved scientific data.
            form = _project_reference(result, option_value='review_control_sets')
            raw_values = {**raw_values, 'option_value': 'review_control_sets'}
    if form.context["adapter"] == "batch_reference_start":
        from mif_pipeline.orchestration.batch_preprocessing_support import unmixing_start_defaults

        defaults = unmixing_start_defaults()
        prepared = {
            c.name: (
                str(c.value)
                if c.kind in {ControlKind.NUMBER, ControlKind.INTEGER, ControlKind.TEXT}
                and c.value is not None
                else c.value
            )
            for c in form.controls
        }
        prepared.update(raw_values)
        state = prepared.get("spectral_state_override")
        copying = state in {"unmixed", "isolated"}
        if copying:
            for name in prepared:
                if name == "spectral_state_override" or name.startswith("measurement_band_component_"):
                    continue
                key = "chunk_size" if name in {"chunk_width", "chunk_height"} else name
                value = defaults.get(key)
                if name in {"chunk_width", "chunk_height"}:
                    value = value[0 if name == "chunk_width" else 1]
                control = next(c for c in form.controls if c.name == name)
                prepared[name] = (
                    str(value)
                    if control.kind in {ControlKind.NUMBER, ControlKind.INTEGER, ControlKind.TEXT}
                    else value
                )
        if prepared.get("solver") == "clipped_ls":
            prepared["nnls_tolerance"] = str(defaults["nnls_tolerance"])
            prepared["nnls_max_iterations"] = str(defaults["nnls_max_iterations"])
        observed = form.context.get("source_spectral_state", "unknown")
        if observed != "unknown" and state not in {None, observed, "isolated"}:
            raise ValueError(
                "Your choice conflicts with the spectral state recorded in this image. Verify the source export before proceeding."
            )
        calibration = form.context.get("calibration_request")
        # A complete, metadata-selected policy needs no manual calibration response.
        if calibration is not None and not calibration["missing"]:
            calibration = None
        mode = prepared.get("calibration_correction_mode")
        selected_rows = [] if calibration is None else [
            (i, row) for i, row in enumerate(calibration["missing"])
            if mode == "full" or (mode == "exposure_only" and row["field"] == "exposure_seconds")
        ]
        active_calibration = {f"calibration_{i}" for i, _ in selected_rows}
        if not copying and calibration is not None:
            if mode not in {"full", "exposure_only"}:
                raise ValueError("Choose how to correct the acquired intensities")
            active_calibration.add("calibration_correction_mode")
            if selected_rows:
                active_calibration.add("calibration_source")
        decode_form = replace(form, controls=tuple(
            control for control in form.controls
            if (not control.name.startswith("calibration_") or
               (not copying and control.name in active_calibration))
            and (not control.name.startswith("measurement_band_component_") or state == "isolated")
        ))
        values = decode_form_values(decode_form, action, prepared)
        if calibration is not None:
            supplied = None
            if not copying:
                supplied = {
                    "correction_mode": values.pop("calibration_correction_mode"),
                    "values": {row["key"]: values.pop(f"calibration_{i}", None) for i, row in selected_rows},
                    "source_note": values.pop("calibration_source", ""),
                }
            values["calibration"] = supplied
        if state == "isolated":
            bands = form.context["measurement_band_mapping"]["bands"]
            mapping = [int(values.pop(f"measurement_band_component_{i}")) for i in range(len(bands))]
            if len(mapping) != len(form.context["measurement_band_mapping"]["components"]) or len(set(mapping)) != len(mapping):
                raise ValueError("Assign every measurement band to a different reviewed component")
            values["measurement_band_components"] = mapping
        values["chunk_size"] = [values.pop("chunk_width"), values.pop("chunk_height")]
        values["af_behavior"] = result.resume_context["service_context"].get(
            "source_af_behavior", defaults["af_behavior"]
        )
    else:
        values = decode_form_values(form, action, raw_values)
        values.update(form.context.get("reference_region_values", {}))
        from mif_pipeline.gui.reference_regions import restore_reference_file_values
        restore_reference_file_values(values, form.context.get("reference_file_bindings", {}))
    if form.context["adapter"] == "batch_reference_operation":
        option_value = values.pop("option_value", None)
        from mif_pipeline.gui.reference_settings import assemble_settings

        injected = assemble_settings(
            values,
            form.context.get("reference_settings_paths", {}),
            form.context.get("reference_settings_constants", {}),
        )
        if option_value != form.context["selected_option"] or injected is None:
            raise ValueError("reference operation option is not selected")
        owner = result.resume_context["service_context"]
        requirements = owner["operation_state"]["operation_requirements"]
        requirement = next(item for item in requirements if item["option_value"] == option_value)
        if requirement["operation_action"] == "propose_final_basis":
            from mif_pipeline.preprocessing.unmixing.reference_defaults import (
                fixed_basis_review_defaults,
            )

            injected = {"qc_policy": fixed_basis_review_defaults()}
        form_values: dict[str, object] = {}
        if any(field["name"] == "reviewer_id" for field in requirement["form_fields"]):
            import getpass

            values["reviewer_id"] = "local-account:" + getpass.getuser()
        for field_index, field in enumerate(requirement["form_fields"]):
            name = field["name"]
            kind = field["value_kind"]
            if (
                option_value in {"import_reference_library", "curated_reference_library"}
                and _has_manifest_library_candidate(owner)
                and name == "library_locator"
            ):
                continue
            if requirement["operation_action"] == "complete_reference_input" and name == "calibration":
                pending = form.context["pending_reference_input"]
                mode = pending["correction_mode"]
                form_values[name] = {
                    "values": {row["key"]: values[f"calibration_{i}"]
                               for i, row in enumerate(pending["calibration_inventory"]["missing"])
                               if mode == "full" or (mode == "exposure_only" and row["field"] == "exposure_seconds")},
                    "source_note": values["calibration_source"],
                }
            elif kind == "identifier_mapping":
                form_values[name] = [
                    [key, values.get(f"reference_mapping_{field_index}_{index}_value")]
                    for index, key in enumerate(field["mapping_key_choices"])
                ]
            elif kind == "identifier_multimapping":
                form_values[name] = [
                    [key, control]
                    for index, key in enumerate(field["mapping_key_choices"])
                    for control in (values.get(f"reference_mapping_{field_index}_{index}_value") or ())
                ]
            elif kind == "enum_mapping":
                choices = tuple(field["value_choices"])
                form_values[name] = {}
                for index, key in enumerate(field["mapping_key_choices"]):
                    value = values.get(f"reference_mapping_{field_index}_{index}_value")
                    if value not in choices:
                        raise ValueError("reference mapping value is not offered")
                    form_values[name][key] = value
            elif kind == "identifier_disposition_mapping":
                decisions = []
                for index, identifier in enumerate(field["identifier_choices"]):
                    disposition = values.get(f"reference_mapping_{field_index}_{index}_disposition")
                    if disposition is None:
                        continue
                    if disposition not in field["value_choices"]:
                        raise ValueError("reference disposition is not offered")
                    decisions.append(
                        {
                            "roi_id": identifier,
                            "component_identity": values.get(
                                f"reference_mapping_{field_index}_{index}_component"
                            ),
                            "included": disposition == "included",
                        }
                    )
                form_values[name] = decisions
            else:
                form_values[name] = values.get(name)
        values = {
            "option_value": option_value,
            "form_values": form_values,
            "injected_configuration": injected,
        }
    return bind_reference_unmixing_response(result, action, values)


def _training_plan_candidates(context: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    raw = context.get("candidate_entry_ids")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("training plan interaction lacks owner candidates")
    candidates: dict[str, tuple[str, ...]] = {}
    for candidate_id, entry_ids in raw.items():
        if (
            type(candidate_id) is not str
            or not candidate_id
            or isinstance(entry_ids, (str, bytes))
            or not isinstance(entry_ids, Sequence)
            or not entry_ids
            or any(type(item) is not str or not item for item in entry_ids)
        ):
            raise ValueError("training plan interaction has invalid owner candidates")
        candidates[candidate_id] = tuple(entry_ids)
    return candidates


def _ignore_policy_controls(
    prefix: str, *, value: str | None, available: tuple[str, ...] = ("none", "annotated"),
) -> tuple[tuple[FormControl, ...], dict[str, dict[str, str]]]:
    """Offer the tissue classes and any explicitly annotated Ignore examples."""
    name = f"{prefix}_ignore_policy"
    choices = tuple(option for option in available if option in {"none", "annotated"})
    if not choices:
        raise ValueError("Reopen classifier setup to choose the tissue classes and annotate training examples.")
    if len(choices) == 1:
        value = choices[0]
    elif value not in choices:
        value = None
    control = FormControl(name, "Classes to fit", ControlKind.CHOICE, choices=choices, value=value)
    return (control,), {name: {
        "none": "Selected class labels only",
        "annotated": "Learn Ignore from annotated regions",
    }}


def _primary_plan_controls(
    context: Mapping[str, object],
    candidate_index: int,
    candidate_id: str,
) -> tuple[list[FormControl], dict[str, dict[str, str]]]:
    seeds = context.get("primary_settings_by_candidate_id")
    participants = context.get("application_participants")
    if not isinstance(seeds, Mapping) or not isinstance(participants, Sequence):
        raise ValueError("primary training plan lacks owner settings and participant inventory")
    seed = seeds.get(candidate_id)
    settings = None if seed is None else PrimaryClassifierSettings.from_mapping(seed)
    classifier = None if settings is None else settings.primary_classifier
    classes = [] if settings is None else list(settings.primary_classes)
    roles = [] if classifier is None else list(classifier.channel_roles.items())
    channels: dict[str, ChannelSpec] = {}
    for participant in participants:
        if not isinstance(participant, Mapping):
            raise ValueError("primary training participant must be an owner mapping")
        for raw_channel in participant.get("channels", ()):
            channel = ChannelSpec.from_mapping(raw_channel, field="participant.channels")
            channels.setdefault(channel.channel_id, channel)
    if not channels:
        raise ValueError("primary training plan lacks canonical channels")
    from mif_pipeline._defaults import default_section

    defaults = default_section("classifier.training")
    prefix = f"primary_{candidate_index}"
    controls: list[FormControl] = [FormControl(
        f"{prefix}_class_count", "Number of classes", ControlKind.CHOICE,
        choices=("1", "2", "3", "4"), value=str(len(classes) or 2),
    )]
    labels: dict[str, dict[str, str]] = {}
    for row in range(4):
        item = classes[row] if row < len(classes) else None
        controls.extend(
            (
                FormControl(
                    f"{prefix}_class_{row}_name",
                    f"Class {row + 1}",
                    ControlKind.TEXT,
                    required=False,
                    value=None if item is None else item.display_name,
                ),
                FormControl(
                    f"{prefix}_class_{row}_id",
                    f"Class {row + 1} ID",
                    ControlKind.TEXT,
                    required=False,
                    value=None if item is None else item.class_id,
                    help_text="Identifier retained with this class across saved models. New classes receive a generated identifier.",
                ),
            )
        )
    from mif_pipeline.gui.feature_channels import default_feature_channels, channel_choice_labels

    channel_choices = tuple(channels)
    selected_channels = [channel for _, channel in roles] if classifier is not None else [
        channel.channel_id for channel in default_feature_channels(channels.values())
    ]
    controls.append(FormControl(
        f"{prefix}_channel_ids", "Channels to use", ControlKind.OBJECT_SEQUENCE,
        choices=channel_choices, value=selected_channels,
    ))
    labels[f"{prefix}_channel_ids"] = channel_choice_labels(channels.values())
    controls.extend(
        (
            FormControl(
                f"{prefix}_classifier_id",
                "Classifier ID",
                ControlKind.TEXT,
                value=candidate_id if classifier is None else classifier.classifier_id,
                help_text="Name used to identify this classifier in saved settings and results.",
            ),
            FormControl(
                f"{prefix}_training_regions",
                "Regions per training label",
                ControlKind.INTEGER,
                value=(
                    defaults["regions_per_label"]
                    if classifier is None
                    else classifier.training_regions_per_label
                ),
                help_text="Required reference regions per class. Include regions representing the tissue variation within each label.",
            ),
            FormControl(
                f"{prefix}_count_minimum",
                "Minimum total regions",
                ControlKind.INTEGER,
                value=(
                    defaults["minimum_structure_count"]
                    if classifier is None
                    else classifier.count_bounds.minimum
                ),
                help_text="Use 0 for no minimum.",
            ),
            FormControl(
                f"{prefix}_count_maximum",
                "Maximum total regions",
                ControlKind.INTEGER,
                required=False,
                value=None if classifier is None else classifier.count_bounds.maximum,
                help_text="Leave empty for no maximum.",
            ),
            FormControl(
                f"{prefix}_class_bounds",
                "Regions in each class",
                ControlKind.JSON_OBJECT,
                value=(
                    {}
                    if classifier is None
                    else {
                        key: value.to_mapping(secondary=False)
                        for key, value in classifier.class_count_bounds.items()
                    }
                ),
                help_text="Ignore regions are excluded from all counts.",
            ),
        )
    )
    policy = (
        defaults["ignore_policy"] if classifier is None
        else "none" if "Ignore" not in classifier.output_classes
        else "annotated" if classifier.require_ignore_training else None
    )
    policy_controls, policy_labels = _ignore_policy_controls(prefix, value=policy)
    controls.extend(policy_controls)
    labels.update(policy_labels)
    if classifier is not None:
        controls.append(FormControl(
            f"{prefix}_review_policy", "Review policy", ControlKind.CHOICE,
            choices=("always", "if_unvalidated", "if_qc_failed", "unattended"),
            value=classifier.review_policy.value,
            help_text="Choose when this classifier’s results require review before downstream processing.",
        ))
        labels[f"{prefix}_review_policy"] = {
            "always": "Always review",
            "if_unvalidated": "Review unless validated",
            "if_qc_failed": "Review unless quality checks pass",
            "unattended": "Unattended",
        }
    return controls, labels


def _primary_settings_from_values(
    form: FormModel,
    decoded: Mapping[str, object],
    index: int,
    candidate_id: str,
) -> dict[str, object]:
    context = form.context["resume_context"]
    seed = context["primary_settings_by_candidate_id"][candidate_id]
    prefix = f"primary_{index}"
    controls = [item for item in form.controls if item.name.startswith(prefix + "_")]
    changed = any(item.name in decoded and decoded[item.name] != item.value for item in controls)
    if seed is not None and not changed:
        settings = PrimaryClassifierSettings.from_mapping(seed)
    else:
        available = {item.name: item for item in controls}

        count_name = f"{prefix}_class_count"
        count_value = decoded.get(count_name)
        if count_value not in available[count_name].choices:
            raise ValueError(f"{count_name}: select the number of classes")
        class_count = int(count_value)
        if class_count == 1 and decoded.get(f"{prefix}_ignore_policy") != "annotated":
            raise ValueError(f"{prefix}_ignore_policy: with one class, learn Ignore from annotated regions")
        classes = []
        for row in range(class_count):
            name = f"{prefix}_class_{row}_name"
            label = str(decoded.get(name) or "").strip()
            identifier = str(decoded.get(f"{prefix}_class_{row}_id") or "").strip()
            if not label:
                raise ValueError(f"{name}: enter a label for class {row + 1}")
            classes.append({"class_id": identifier or f"class-{row + 1}", "display_name": label})
        channel_name = f"{prefix}_channel_ids"
        selected = decoded.get(channel_name)
        if (isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence)
                or not selected or any(channel not in available[channel_name].choices for channel in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError(f"{channel_name}: select at least one offered image channel, once each")
        retained = {} if seed is None else PrimaryClassifierSettings.from_mapping(seed).primary_classifier.channel_roles
        roles = {role: channel for role, channel in retained.items() if channel in selected}
        # Existing names remain part of saved classifier and selector contracts.
        # New bindings use canonical IDs rather than researcher-entered aliases.
        for channel in available[channel_name].choices:
            if channel not in selected or channel in roles.values():
                continue
            role = channel
            suffix = 2
            while role in retained or role in roles:
                role = f"{channel}#{suffix}"
                suffix += 1
            roles[role] = channel
        base = (
            dict(PrimaryClassifierSettings.from_mapping(seed).to_mapping()["primary_classifier"])
            if seed is not None
            else {
                "mode": "train_new",
                "artifact_path": None,
                "classifier_version": None,
                "validation_domain_id": context["validation_domain_id"],
                "classifier_sha256": None,
                "validation_status": None,
                "validation_record_id": None,
                "qc_passed": None,
                "qc_record_id": None,
                "review_policy": "always",
            }
        )
        class_ids = [item["class_id"] for item in classes]
        base.update(
            {
                "classifier_id": decoded.get(f"{prefix}_classifier_id"),
                "channel_roles": roles,
                "training_regions_per_label": decoded.get(f"{prefix}_training_regions"),
                "require_ignore_training": decoded.get(f"{prefix}_ignore_policy") == "annotated",
                "count_bounds": {
                    "minimum": decoded.get(f"{prefix}_count_minimum"),
                    "maximum": decoded.get(f"{prefix}_count_maximum"),
                },
                "class_count_bounds": decoded.get(f"{prefix}_class_bounds"),
            }
        )
        output_changed = seed is None or any(
            decoded.get(item.name) != item.value
            for item in controls
            if item.name in {f"{prefix}_ignore_policy", f"{prefix}_class_count"}
            or ("_class_" in item.name and item.name.endswith("_id"))
        )
        if output_changed:
            base["output_classes"] = class_ids + (
                ["Ignore"] if decoded.get(f"{prefix}_ignore_policy") == "annotated" else []
            )
        if f"{prefix}_review_policy" in available:
            base["review_policy"] = decoded.get(f"{prefix}_review_policy")
        settings = PrimaryClassifierSettings.from_mapping(
            {
                "primary_classes": classes,
                "primary_classifier": base,
            }
        )
    for participant in context["application_participants"]:
        settings.validate_channels(
            tuple(
                ChannelSpec.from_mapping(item, field="participant.channels")
                for item in participant["channels"]
            )
        )
    return settings.to_mapping()


def _pixel_model_controls(
    index: int,
    *,
    channels: Sequence[Mapping[str, object]] = (),
    nuclear_channel_id=None, intensity_divisor=None,
) -> list[FormControl]:
    prefix = f"training_{index}"
    controls = [
        FormControl(
            f"{prefix}_model_method",
            "Training method",
            ControlKind.CHOICE,
            choices=(
                ("rtrees", "svm", "kronos_rtrees", "kronos_svm") if channels else ("rtrees", "svm")
            ),
            value="rtrees",
            help_text=None,
        ),
        FormControl(
            f"{prefix}_resolution",
            "Pixel resolution (µm/pixel)",
            ControlKind.NUMBER,
            help_text="Pixel size at which the classifier samples the tissue and calculates its image features.",
        ),
        FormControl(
            f"{prefix}_maximum_samples",
            "Maximum training samples",
            ControlKind.INTEGER,
            help_text="Use 0 to fit all eligible labelled pixels. A positive limit fits a random sample of up to that many pixels.",
        ),
        FormControl(
            f"{prefix}_reweight",
            "Reweight training samples",
            ControlKind.CHOICE,
            choices=("yes", "no"),
            help_text="Give each class the same total weight by assigning more weight to pixels from less frequent classes.",
        ),
        FormControl(
            f"{prefix}_normalization",
            "Feature normalization",
            ControlKind.CHOICE,
            choices=("none", "mean_variance", "min_max"),
            help_text="Choose the feature normalization method.",
        ),
        FormControl(
            f"{prefix}_pca_variance",
            "PCA variance target",
            ControlKind.NUMBER,
            help_text="The target sets how much variation in the training data the retained components should capture.",
        ),
        FormControl(
            f"{prefix}_pca_normalize",
            "PCA whitening",
            ControlKind.CHOICE,
            choices=("yes", "no"),
            help_text="Scale each retained component by its training standard deviation.",
        ),
        FormControl(
            f"{prefix}_boundary_strategy",
            "Boundary strategy",
            ControlKind.CHOICE,
            choices=("skip", "classify"),
            help_text="Keep reference-region labels or assign a separate class along their edges.",
        ),
        FormControl(
            f"{prefix}_boundary_thickness",
            "Boundary thickness (pixels)",
            ControlKind.NUMBER,
            required=False,
            help_text="Width of the labelled strip centred on the annotation edge, in training pixels.",
        ),
        FormControl(
            f"{prefix}_boundary_label",
            "Boundary QuPath label number",
            ControlKind.INTEGER,
            required=False,
            help_text="QuPath class number assigned to pixels in the boundary strip.",
        ),
    ]
    from mif_pipeline.gui.pixel_image_features import image_feature_controls
    controls[2:2] = image_feature_controls(prefix)
    defaults = QuPathRTreesParameters.authoring_defaults().to_mapping()
    from mif_pipeline.gui.control_guidance import RF_PARAMETER_HELP

    for field, label, kind in (
        ("max_depth", "Maximum tree depth", ControlKind.INTEGER),
        ("min_sample_count", "Minimum samples per node", ControlKind.INTEGER),
        ("active_var_count", "Features considered per split", ControlKind.INTEGER),
        ("max_trees", "Maximum trees", ControlKind.INTEGER),
        ("termination_epsilon", "Out-of-bag error threshold", ControlKind.NUMBER),
        ("calculate_variable_importance", "Calculate variable importance", ControlKind.CHECKBOX),
    ):
        controls.append(
            FormControl(
                f"{prefix}_rtrees_{field}",
                label,
                kind,
                value=defaults[field],
                help_text=RF_PARAMETER_HELP[field],
            )
        )
    svm_defaults = QuPathSvmParameters.authoring_defaults().to_mapping()
    controls.extend(
        (
            FormControl(
                f"{prefix}_svm_kernel",
                "SVM kernel",
                ControlKind.CHOICE,
                choices=("LINEAR", "RBF"),
                value=svm_defaults["kernel"],
                help_text="Linear separates with a straight boundary; RBF allows curved boundaries.",
            ),
            FormControl(
                f"{prefix}_svm_c",
                "SVM C",
                ControlKind.NUMBER,
                value=svm_defaults["c"],
                help_text="Higher C penalizes training observations inside the margin or on the wrong side of the boundary more strongly.",
            ),
            FormControl(
                f"{prefix}_svm_gamma",
                "RBF gamma",
                ControlKind.NUMBER,
                required=False,
                help_text="Larger values restrict the influence of each training observation to a smaller part of feature space.",
            ),
            FormControl(
                f"{prefix}_svm_max_iterations",
                "Maximum SVM iterations",
                ControlKind.INTEGER,
                value=svm_defaults["max_iterations"],
                help_text="Fitting stops at this limit even if the SVM has yet to reach its tolerance.",
            ),
            FormControl(
                f"{prefix}_svm_termination_epsilon",
                "SVM termination tolerance",
                ControlKind.NUMBER,
                value=svm_defaults["termination_epsilon"],
                help_text="Optimizer stopping tolerance. Smaller values can require additional fitting iterations.",
            ),
        )
    )
    if channels:
        from mif_pipeline.gui.kronos_setup import kronos_controls

        controls.extend(kronos_controls(index, channels, nuclear_channel_id=nuclear_channel_id, intensity_divisor=intensity_divisor))
    return controls


def _pixel_model_from_values(
    decoded: Mapping[str, object],
    index: int,
    output_ids: Sequence[str],
    class_indices: Mapping[str, int],
) -> dict[str, object]:
    prefix = f"training_{index}"
    labels = [
        {
            "canonical_class_id": class_id,
            "qupath_label": (
                max(class_indices.values(), default=-1) + 1
                if class_id == "Ignore"
                else class_indices[class_id]
            ),
        }
        for class_id in output_ids
    ]

    def choice_boolean(name: str) -> bool:
        choice = decoded.get(f"{prefix}_{name}")
        if choice not in {"yes", "no"}:
            raise ValueError(f"{prefix}_{name} requires an explicit choice")
        return choice == "yes"

    strategy = decoded.get(f"{prefix}_boundary_strategy")
    if strategy not in {"skip", "classify"}:
        raise ValueError("boundary strategy requires an explicit choice")
    from mif_pipeline.gui.pixel_image_features import image_features_from_values
    mapping = {
        "image_features": image_features_from_values(decoded, prefix),
        "resolution_um_per_pixel": decoded.get(f"{prefix}_resolution"),
        "maximum_training_samples": decoded.get(f"{prefix}_maximum_samples"),
        "reweight_samples": choice_boolean("reweight"),
        "normalization": decoded.get(f"{prefix}_normalization"),
        "pca_retained_variance": decoded.get(f"{prefix}_pca_variance"),
        "pca_normalize": choice_boolean("pca_normalize"),
        "boundary_strategy": strategy,
        "boundary_thickness_px": (
            0.0 if strategy == "skip" else decoded.get(f"{prefix}_boundary_thickness")
        ),
        "output_labels": {
            "schema": "qupath-output-labels/v1",
            "ordered_labels": labels,
        },
    }
    if strategy == "classify":
        mapping["boundary_class_id"] = decoded.get(f"{prefix}_boundary_label")
    method = decoded.get(f"{prefix}_model_method", "rtrees").removeprefix("kronos_")
    if method == "rtrees":
        native_defaults = QuPathRTreesParameters.authoring_defaults().to_mapping()
        rtrees = {
            field: decoded.get(f"{prefix}_rtrees_{field}", default)
            for field, default in native_defaults.items()
            if field not in {"cv_folds", "use_surrogates"}
        }
        rtrees.update({"cv_folds": 0, "use_surrogates": False})
        mapping.update({"configuration_schema": "qupath-pixel-rtrees/v1", "rtrees": rtrees})
        return QuPathPixelRTreesConfiguration.from_mapping(mapping).to_mapping()
    if method == "svm":
        defaults = QuPathSvmParameters.authoring_defaults().to_mapping()
        kernel = decoded.get(f"{prefix}_svm_kernel", defaults["kernel"])
        mapping.update(
            {
                "configuration_schema": "qupath-pixel-svm/v1",
                "svm": {
                    "model_type": "C_SVC",
                    "kernel": kernel,
                    "c": decoded.get(f"{prefix}_svm_c", defaults["c"]),
                    "gamma": decoded.get(f"{prefix}_svm_gamma") if kernel == "RBF" else None,
                    "max_iterations": decoded.get(
                        f"{prefix}_svm_max_iterations", defaults["max_iterations"]
                    ),
                    "termination_epsilon": decoded.get(
                        f"{prefix}_svm_termination_epsilon", defaults["termination_epsilon"]
                    ),
                },
            }
        )
        return QuPathPixelSvmConfiguration.from_mapping(mapping).to_mapping()
    raise ValueError("Select a supported training method")


def _primary_model_from_values(
    decoded: Mapping[str, object],
    index: int,
    settings: Mapping[str, object],
) -> dict[str, object]:
    from mif_pipeline.orchestration.batch_training.plan_selection import apply_primary_ignore_policy

    output_ids = list(settings["primary_classifier"]["output_classes"])
    class_indices = {item["class_id"]: row for row, item in enumerate(settings["primary_classes"])}
    base_values = {**decoded, f"training_{index}_boundary_strategy": "skip"}
    model = _pixel_model_from_values(base_values, index, output_ids, class_indices)
    policy = decoded.get(f"primary_{index}_ignore_policy")
    _, model = apply_primary_ignore_policy(
        PrimaryClassifierSettings.from_mapping(settings), model, policy=policy,
        boundary_thickness_px=0.0,
    )
    return model


def _secondary_channel_roles_from_values(form, decoded, index, candidate_id):
    name = f"secondary_{index}_channel_ids"
    control = next(item for item in form.controls if item.name == name)
    selected = decoded.get(name)
    if (isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence)
            or not selected or len(set(selected)) != len(selected)
            or any(channel not in control.choices for channel in selected)):
        raise ValueError(f"{name}: select at least one offered image channel, once each")
    retained = form.context["resume_context"]["secondary_output_classes_by_candidate_id"][candidate_id]["channel_roles"]
    roles = {role: channel for role, channel in retained.items() if channel in selected}
    for channel in control.choices:
        if channel not in selected or channel in roles.values():
            continue
        role, suffix = channel, 2
        while role in retained or role in roles:
            role, suffix = f"{channel}#{suffix}", suffix + 1
        roles[role] = channel
    return roles


def _secondary_model_from_values(
    decoded: Mapping[str, object],
    index: int,
    record: Mapping[str, object],
) -> dict[str, object]:
    output_ids = tuple(item["canonical_class_id"] for item in record["output_classes"])
    try:
        return _pixel_model_from_values(
            decoded,
            index,
            output_ids,
            {class_id: row for row, class_id in enumerate(output_ids)},
        )
    except BatchTrainingError as exc:
        raise BatchTrainingError(f"training_{index}: {exc}") from exc


def _candidate_patient_entries(
    entry_ids: Sequence[str],
    participants: Mapping[str, Mapping[str, str]],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for entry_id in sorted(entry_ids):
        grouped.setdefault(participants[entry_id]["subject_id"], []).append(entry_id)
    return {subject_id: tuple(grouped[subject_id]) for subject_id in sorted(grouped)}


def _project_supervised_references(result: StageResult) -> FormModel:
    """Show each accepted image/role without asking for backend identity fields."""
    from mif_pipeline.orchestration.batch_training.supervised_references import validate_reference_review_context

    context = validate_reference_review_context(result.resume_context)
    if (result.stage != "supervised_phenotype_training_plan_review"
            or result.interaction_reason is not InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED
            or result.allowed_actions != ("annotate_supervised_training",)):
        raise ValueError("Reopen the current supervised reference review.")
    rows = tuple(FormTableRow(
        row_id=str(index), values=(
            row["sample_id"], row["subject_id"],
            {"training": "Training references", "validation": "Validation references",
             "test": "Independent-test references"}[row["split"]],
            ", ".join(row["class_ids"]),
        ),
    ) for index, row in enumerate(context["references"]))
    return FormModel(
        form_id="supervised_references", title="Label reference cells",
        message="Choose an image and reference role, then annotate cells in QuPath with the point tool and Set class. Each point selects a nearby cell. Save and close QuPath to return the labels before fitting.",
        tables=(FormTable(table_id="reference_images",
                          columns=("Image", "Subject", "Reference role", "Cell types"), rows=rows),),
        allowed_actions=("annotate_supervised_training",),
        context={"adapter": "supervised_references", "reason": result.interaction_reason.value,
                 "resume_context": dict(context), "reference_first": True,
                 "action_labels": {"annotate_supervised_training": "Freeze references and fit"}},
    )


def _submit_supervised_references(form, action, raw_values):
    """Submit the displayed plan identity with the explicit completion choice."""
    from mif_pipeline.orchestration.batch_training.supervised_references import validate_reference_completion

    if action != "annotate_supervised_training":
        raise ValueError("Choose Freeze references and fit after saving the reference points.")
    decode_form_values(form, action, raw_values)
    context = form.context["resume_context"]
    return validate_reference_completion({
        "training_plan_set_sha256": context["training_plan_set_sha256"],
    }, context)


def _project_training_plan(result: StageResult, *, saved_supervised_setup=None) -> FormModel:
    """Build the current classifier form from its subjects, classes and image inputs.

    Each primary or secondary classifier starts with one training procedure.
    That procedure determines scalar parameters, an editable cross-validation
    grid and any independent test allocation. Reference completion and optional
    annotation feedback belong to the same form. Native class codes are assigned
    from the declared class order when the response is constructed.
    """
    context = result.resume_context or {}
    if (
        result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage not in TRAINING_PLAN_REVIEW_NODE_KINDS
        or result.interaction_reason is not InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED
        or result.allowed_actions != ("select_training_plan",)
        or context.get("schema_version") != TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION
    ):
        raise ValueError("The classifier request does not match this prototype release.")
    context = validate_training_plan_selection_request(context)
    from mif_pipeline._defaults import default_section
    from mif_pipeline.gui.cv_authoring import apply_current_tissue_defaults, search_controls
    from mif_pipeline.gui.training_procedure import LABELS

    defaults = default_section("classifier.training")
    candidates = _training_plan_candidates(context)
    participants = context["candidate_participants"]
    panels = dict(context["available_channels_by_candidate_id"])
    primary = result.stage == "primary_classifier_training_plan_review"
    supervised = result.stage == "supervised_phenotype_training_plan_review"
    secondary = result.stage == "secondary_classifier_training_plan_review"
    controls: list[FormControl] = []
    labels: dict[str, dict[str, str]] = {}
    if primary:
        controls.extend((
            FormControl("classifier_source", "Classifier source", ControlKind.CHOICE,
                        choices=("train", "saved", "skip"), value="train"),
            FormControl("saved_classifier_export", "Saved classifier", ControlKind.LOCAL_FILE,
                        required=False, help_text="Choose export.json from an accepted classifier package."),
        ))
        labels["classifier_source"] = {"train": "Train a classifier", "saved": "Use a saved classifier", "skip": "Skip primary classification"}
    options = context.get("supervised_options")
    if supervised:
        if not isinstance(options, Mapping):
            raise ValueError("The supervised phenotype settings are missing.")
        fields, names = supervised_controls(0, options,
            intensity_divisor=context.get("kronos_intensity_divisor_by_candidate_id", {}).get("supervised-phenotype"))
        controls.extend(fields)
        labels.update(names)

    for index, (candidate_id, entries) in enumerate(sorted(candidates.items())):
        prefix = f"training_{index}"
        subjects = _candidate_patient_entries(entries, participants)
        controls.append(
            FormControl(
                prefix + "_procedure",
                "Training procedure",
                ControlKind.CHOICE,
                choices=tuple(LABELS),
                value="training_validation" if len(subjects) >= 2 else "training_only",
                help_text="Cross-validation compares settings by holding out training subjects in turn. Independent testing evaluates the fitted model on subjects reserved before training.",
            )
        )
        labels[prefix + "_procedure"] = LABELS
        controls.append(FormControl(
            prefix + "_test_subjects",
            "Test subjects for automatic assignment",
            ControlKind.INTEGER, required=False,
            value=defaults["reserved_test_subjects"] if len(subjects) >= 3 else 0,
            help_text="Choose how many subjects to reserve for testing, then apply the assignment to update the groups.",
        ))
        for subject_index, (subject, subject_entries) in enumerate(subjects.items()):
            name = f"{prefix}_patient_{subject_index}_role"
            controls.append(
                FormControl(
                    name,
                    (f"{candidate_id}: " if len(candidates) > 1 else "") + f"Subject {subject}",
                    ControlKind.CHOICE,
                    choices=("training", "test", "not_selected"),
                    value="training",
                    help_text=None,
                )
            )
            labels[name] = {
                "training": "Development",
                "test": "Independent test",
                "not_selected": "Leave out",
            }
        controls.append(
            FormControl(
                prefix + "_seed",
                "Random seed",
                ControlKind.INTEGER,
                value=defaults["random_seed"],
                help_text="Seed used for reproducible subject allocation and classifier fitting.",
            )
        )
        if not supervised:
            controls.extend(
                apply_current_tissue_defaults(
                    [control for control in _pixel_model_controls(
                        index, channels=panels.get(candidate_id, ()),
                        nuclear_channel_id=context.get("nuclear_channel_by_candidate_id", {}).get(candidate_id),
                        intensity_divisor=context.get("kronos_intensity_divisor_by_candidate_id", {}).get(candidate_id),
                    ) if not (primary and control.name.endswith((
                        "_boundary_strategy", "_boundary_thickness", "_boundary_label"
                    )))]
                )
            )
            labels[prefix + "_model_method"] = {
                "rtrees": "Random forest (RF)",
                "svm": "Support vector machine (SVM)",
            }
            if panels.get(candidate_id):
                labels[prefix + "_model_method"].update(
                    {"kronos_rtrees": "KRONOS–GNN–RF", "kronos_svm": "KRONOS–GNN–SVM"}
                )
        search = search_controls(index, len(subjects))
        controls.extend(
            replace(control, help_text="Each fold holds out whole subjects. Every training fold must include all classes.")
            if (primary or secondary) and control.name.endswith("_cv_folds") else control
            for control in search
        )
        if secondary:
            from mif_pipeline.gui.feature_channels import channel_choice_labels
            record = context["secondary_output_classes_by_candidate_id"][candidate_id]
            channel_name = f"secondary_{index}_channel_ids"
            controls.append(FormControl(channel_name, "Channels to use", ControlKind.OBJECT_SEQUENCE,
                choices=tuple(row["channel_id"] for row in panels[candidate_id]),
                value=list(dict.fromkeys(record["channel_roles"].values()))))
            labels[channel_name] = channel_choice_labels(panels[candidate_id])
        if primary:
            fields, names = _primary_plan_controls(context, index, candidate_id)
            controls.extend(fields)
            labels.update(names)

    if not supervised:
        if len(candidates) > 1:
            controls.append(
                FormControl(
                    "annotation_preview_candidate",
                    "Classifier to preview",
                    ControlKind.CHOICE,
                    choices=tuple(candidates),
                    value=next(iter(candidates)),
                    required=False,
                )
            )
            if secondary:
                labels["annotation_preview_candidate"] = {
                    candidate_id: ", ".join(row["display_name"] for row in context["secondary_output_classes_by_candidate_id"][candidate_id]["structures"])
                    for candidate_id in candidates
                }
        controls.extend(
            (
                FormControl(
                    "allow_annotation_preview",
                    "Show predictions while annotating",
                    ControlKind.CHECKBOX,
                    value=False,
                    help_text="Fits a temporary classifier from this image’s annotations. Its predictions may influence the labels you draw.",
                ),
            )
        )
    saved_cell_extraction = None
    saved_kronos_encoder = None
    if supervised and saved_supervised_setup is not None:
        from mif_pipeline.gui.supervised_setup import prefill_supervised_controls
        from mif_pipeline.cell_analysis.kronos_features import validate_cell_extraction

        controls = prefill_supervised_controls(controls, context, saved_supervised_setup,
            subjects=_candidate_patient_entries(candidates["supervised-phenotype"], participants))
        configuration = saved_supervised_setup["model_configuration"]
        model = (configuration["candidates"][0]
                 if configuration["configuration_schema"] == "qupath-grouped-cross-validation/v1"
                 else configuration)
        source = model.get("feature_source")
        if source is not None:
            saved_cell_extraction = validate_cell_extraction(source["extraction"])
            saved_kronos_encoder = source["encoder"]
    return FormModel(
        form_id="training_plan_selection",
        title=("Supervised cell classification" if supervised else "Primary tissue classification" if primary else "Secondary structure classification"),
        message=(
            "Assign phenotypes to individual cells using a classifier trained on labelled reference cells. The classifier can use cell measurements or staining patterns in the selected channels."
            if supervised
            else "Identify tissue compartments within the analysis regions from staining in the selected image channels. Train a classifier on reference annotations or apply a saved classifier."
            if primary else (
                "Choose the shared channels, image features and training settings."
                if len(context["candidate_entry_ids"]) == 1 and len(next(iter(context["secondary_output_classes_by_candidate_id"].values()))["structures"]) > 1
                else "Choose the channels, image features and training settings for each structure."
            )
        ),
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=(),
        context={
            "adapter": "training_plan_selection",
            "resume_context": dict(context),
            "primary": primary,
            "supervised": supervised,
            "secondary": secondary,
            "kronos_panels": panels,
            # A reopened accepted setup retains its sampling semantics; this is
            # source metadata, not an editable algorithm control.
            "saved_cell_extraction": saved_cell_extraction,
            "saved_kronos_encoder": saved_kronos_encoder,
            "development_cv_policy": True,
            "reference_first": not supervised,
            "choice_labels": labels,
            "plain_text_sequences": ("supervised_0_class_ids",) if supervised else (),
            "locked_sequences": (
                ("supervised_0_class_ids",)
                if supervised and options["configured_class_ids"] is not None
                else ()
            ),
        },
    )


def _submit_training_plan(
    form: FormModel, action: str, raw_values: Mapping[str, object], *, for_preview: bool = False
) -> dict[str, object]:
    if form.context.get("primary") and raw_values.get("classifier_source") == "skip":
        if for_preview:
            raise ValueError("Primary classification is skipped; no classifier preview is available.")
        return {"schema_version": TRAINING_PLAN_SELECTION_SCHEMA_VERSION, "skip_primary": True}
    if form.context.get("primary") and raw_values.get("classifier_source") == "saved":
        if for_preview:
            raise ValueError("A saved classifier uses its recorded input channels and classes.")
        from pathlib import Path
        from mif_pipeline.orchestration.batch_training.accepted_model_reuse import read_native_classifier_export
        from mif_pipeline.orchestration.batch_training.plan_selection import TrainingPlanSelectionResponse
        path = raw_values.get("saved_classifier_export")
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError("Choose export.json from the saved classifier package.")
        material = read_native_classifier_export(Path(path))
        if material.primary_settings is None:
            raise ValueError("Choose an accepted primary tissue classifier.")
        owner = validate_training_plan_selection_request(form.context["resume_context"])
        for row in owner["application_participants"]:
            material.primary_settings.validate_channels(tuple(ChannelSpec.from_mapping(item, field="application channel")
                for item in row["channels"]))
        return TrainingPlanSelectionResponse(TRAINING_PLAN_SELECTION_SCHEMA_VERSION, (),
            {"export_path": str(material.path), "export_sha256": material.sha256}).to_mapping()
    owner_context = validate_training_plan_selection_request(form.context["resume_context"])
    candidates = _training_plan_candidates(owner_context)
    indexed_candidates = list(enumerate(sorted(candidates.items())))
    active_values = dict(raw_values)
    if for_preview and form.context.get("secondary") and len(candidates) > 1:
        selected = raw_values.get("annotation_preview_candidate")
        if not isinstance(selected, str) or selected not in candidates:
            raise ValueError("Select a structure classifier before opening References.")
        index = sorted(candidates).index(selected)
        indexed_candidates = [indexed_candidates[index]]
        candidates = {selected: candidates[selected]}
        # Reuse the real candidate's control names and defaults. Other drafts
        # may be incomplete while the user collects this classifier's labels.
        prefixes = (f"training_{index}_", f"secondary_{index}_")
        form = replace(form, controls=tuple(c for c in form.controls if c.name.startswith(prefixes)))
        active_values = {key: value for key, value in active_values.items() if key.startswith(prefixes)}
    if form.context.get("development_cv_policy"):
        from mif_pipeline.gui.training_procedure import selected_procedure

        for control in form.controls:
            if control.name.endswith("_procedure") and control.name not in active_values:
                active_values[control.name] = control.value
    if form.context["primary"] or form.context["secondary"]:
        for candidate_index, _ in indexed_candidates:
            prefix = f"training_{candidate_index}"
            if form.context.get("development_cv_policy"):
                from mif_pipeline.gui.training_procedure import selected_procedure

                if selected_procedure(active_values, candidate_index).cross_validation:
                    from mif_pipeline.gui.cv_authoring import seed_grid_baseline

                    seed_grid_baseline(active_values, candidate_index)
            method = active_values.get(f"{prefix}_model_method", "rtrees")
            if method in {"kronos_rtrees", "kronos_svm"}:
                active_values.update(
                    {
                        f"{prefix}_maximum_samples": 0,
                        f"{prefix}_reweight": "no",
                        f"{prefix}_pca_variance": 0.0,
                        f"{prefix}_pca_normalize": "no",
                        f"{prefix}_boundary_strategy": "skip",
                    }
                )
            if form.context.get("primary") or form.context.get("secondary"):
                try:
                    pca_disabled = float(active_values.get(f"{prefix}_pca_variance")) == 0
                except (TypeError, ValueError):
                    pca_disabled = False
                if pca_disabled:
                    active_values[f"{prefix}_pca_normalize"] = "no"
            inactive = (
                "_rtrees_"
                if method in {"svm", "kronos_svm"}
                else "_svm_" if method in {"rtrees", "kronos_rtrees"} else None
            )
            if inactive is not None:
                for name in tuple(active_values):
                    if name.startswith(f"{prefix}{inactive}"):
                        active_values.pop(name)
            if (
                method in {"svm", "kronos_svm"}
                and active_values.get(f"{prefix}_svm_kernel", "LINEAR") == "LINEAR"
            ):
                active_values.pop(f"{prefix}_svm_gamma", None)
    if form.context.get("development_cv_policy"):
        for i, _ in indexed_candidates:
            prefix = f"training_{i}"
            from mif_pipeline.gui.training_procedure import selected_procedure

            procedure = selected_procedure(active_values, i)
            if not (procedure.independent_test and active_values.get(prefix + "_assignment_mode", "manual") == "automatic"):
                active_values.pop(prefix + "_test_subjects", None)
            if not procedure.cross_validation:
                active_values.pop(prefix + "_cv_folds", None)
                active_values.pop(prefix + "_cv_graph_widths", None)
            if active_values.get(prefix + "_model_method", "rtrees") not in {
                "kronos_rtrees",
                "kronos_svm",
            }:
                active_values.pop(prefix + "_cv_graph_widths", None)
    if form.context.get("supervised"):
        from mif_pipeline.gui.cv_authoring import seed_grid_baseline
        for i, _ in indexed_candidates:
            prefix = f"supervised_{i}"
            selected_method = active_values.get(prefix + "_model_method", "rtrees")
            backend = "kronos" if selected_method.startswith("kronos_") else "measurements"
            method = selected_method.removeprefix("kronos_")
            for name in tuple(active_values):
                if not name.startswith(prefix + "_"):
                    continue
                inactive_family = any(
                    name.startswith(prefix + "_" + family + "_") and method != family
                    for family in ("rtrees", "svm", "logistic_regression")
                )
                if inactive_family or ("_kronos_" in name and backend != "kronos") or (name == prefix + "_feature_ids" and backend == "kronos"):
                    active_values.pop(name)
            if backend == "kronos":
                channels = form.context["resume_context"]["supervised_options"].get("available_channels", ())
                selected_ids = active_values.get(prefix + "_kronos_channel_ids", ())
                for channel_index, channel in enumerate(channels):
                    if channel["channel_id"] not in selected_ids:
                        for suffix in ("marker", "divisor"):
                            active_values.pop(f"{prefix}_kronos_panel_{channel_index}_{suffix}", None)
            if method == "svm" and active_values.get(prefix + "_svm_kernel", "LINEAR") == "LINEAR":
                active_values.pop(prefix + "_svm_gamma", None)
            if selected_procedure(active_values, i).cross_validation:
                seed_grid_baseline(active_values, i)
    decoded = decode_form_values(form, action, active_values)
    for control in form.controls:
        if control.name not in decoded and control.value is not None:
            decoded[control.name] = control.value
    participants = owner_context["candidate_participants"]
    kronos_setup = form.context.get("kronos_setup")
    selections = []
    for candidate_index, (candidate_id, entry_ids) in indexed_candidates:
        prefix = f"training_{candidate_index}"
        assignment = decoded.get(prefix + "_assignment_mode", "manual")
        roles = []
        intent = (
            selected_procedure(decoded, candidate_index).evaluation_intent
            if form.context.get("development_cv_policy")
            else decoded.get(prefix + "_intent")
        )
        if assignment == "automatic":
            from mif_pipeline.orchestration.batch_training.split_policy import assign_subject_roles

            validation = 0
            test = (
                decoded.get(prefix + "_test_subjects", 0)
                if intent == "independent_performance"
                else 0
            )
            assignments = assign_subject_roles(
                {entry: participants[entry]["subject_id"] for entry in entry_ids},
                validation_subjects=validation,
                test_subjects=test,
                seed=decoded.get(prefix + "_seed", 0),
                evaluation_intent=intent,
            )
            roles = [{"project_entry_id": entry, "split": split} for entry, split in assignments]
        elif assignment == "manual":
            for patient_index, patient_entry_ids in enumerate(
                _candidate_patient_entries(entry_ids, participants).values()
            ):
                name = f"training_{candidate_index}_patient_{patient_index}_role"
                split = decoded.get(name)
                if not isinstance(split, str) or split not in {
                    "training",
                    "validation",
                    "test",
                    "not_selected",
                }:
                    raise ValueError(f"{name}: select a role for this patient")
                if split != "not_selected":
                    roles.extend(
                        {"project_entry_id": entry_id, "split": split}
                        for entry_id in patient_entry_ids
                    )
        else:
            raise ValueError("Choose automatic or manual subject assignment")
        primary_settings = (
            _primary_settings_from_values(form, decoded, candidate_index, candidate_id)
            if form.context["primary"]
            else None
        )
        secondary_roles = (_secondary_channel_roles_from_values(form, decoded, candidate_index, candidate_id)
                           if form.context.get("secondary") else None)
        if kronos_setup is None and (
            decoded.get(f"training_{candidate_index}_model_method") in {"kronos_rtrees", "kronos_svm"}
            or (form.context["supervised"]
                and str(decoded.get(f"supervised_{candidate_index}_model_method", "")).startswith("kronos_"))
        ):
            from mif_pipeline.orchestration.setup.kronos_setup import model_setup_snapshot

            kronos_setup = model_setup_snapshot()
        supervised_classes = supervised_features = None
        supervised_model = None
        if form.context["supervised"]:
            supervised_classes, supervised_features, supervised_model = supervised_selection(
                decoded,
                candidate_index,
                form.context["resume_context"]["supervised_options"],
                kronos_setup=kronos_setup,
                kronos_extraction=form.context.get("saved_cell_extraction"),
                kronos_saved_encoder=form.context.get("saved_kronos_encoder"),
            )
        model_configuration = (
            _primary_model_from_values(decoded, candidate_index, primary_settings)
            if primary_settings is not None
            else (
                supervised_model
                if supervised_model is not None
                else _secondary_model_from_values(
                    decoded,
                    candidate_index,
                    form.context["resume_context"]["secondary_output_classes_by_candidate_id"][
                        candidate_id
                    ],
                )
            )
        )
        if decoded.get(f"training_{candidate_index}_model_method") in {
            "kronos_rtrees",
            "kronos_svm",
        }:
            from mif_pipeline.gui.kronos_setup import build_kronos_configuration

            channels = form.context["kronos_panels"][candidate_id]
            selected_ids = (
                primary_settings["primary_classifier"]["channel_roles"].values()
                if primary_settings is not None
                else secondary_roles.values() if secondary_roles is not None
                else [row["channel_id"] for row in channels]
            )
            model_configuration = build_kronos_configuration(
                model_configuration, decoded, candidate_index, channels, selected_ids,
                setup=kronos_setup,
                nuclear_channel_id=owner_context.get("nuclear_channel_by_candidate_id", {}).get(candidate_id),
            )
        if form.context.get("development_cv_policy"):
            from mif_pipeline.gui.cv_authoring import configure_search

            development = [
                participants[r["project_entry_id"]]["subject_id"]
                for r in roles
                if r["split"] == "training"
            ]
            radius = model_configuration.pop("point_selection_radius_level0", None)
            model_configuration = configure_search(
                model_configuration, decoded, candidate_index, development
            )
            if radius is not None:
                model_configuration["point_selection_radius_level0"] = radius
        selections.append(
            {
                "candidate_id": candidate_id,
                "roles": sorted(roles, key=lambda row: (row["project_entry_id"], row["split"])),
                "evaluation_intent": intent,
                "model_configuration": model_configuration,
                "random_seed": decoded.get(f"training_{candidate_index}_seed"),
                "primary_settings": primary_settings,
                "secondary_channel_roles": secondary_roles,
                "supervised_class_ids": supervised_classes,
                "supervised_feature_ids": supervised_features,
            }
        )
    checked = validate_training_plan_selection_response(
        {
            "schema_version": TRAINING_PLAN_SELECTION_SCHEMA_VERSION,
            "selections": selections,
        },
        candidate_entry_ids=candidates,
        supervised=form.context["supervised"],
    )
    return checked.to_mapping()


def build_annotation_reference_role(form, raw_values):
    """Carry the displayed classifier choice into its ordinary annotation ticket."""
    selection = _submit_training_plan(form, "select_training_plan", raw_values, for_preview=True)
    candidate = raw_values.get("annotation_preview_candidate") or next(
        iter(form.context["resume_context"]["candidate_entry_ids"])
    )
    return {"candidate_id": candidate, "selection": selection}


def build_annotation_preview_request(form, raw_values, entry_id):
    if (
        not form.context.get("reference_first")
        or raw_values.get("allow_annotation_preview") is not True
    ):
        return None
    from mif_pipeline.orchestration.annotation_preview import validate_preview_selection

    request = {
        "schema": "annotation-preview-selection/v1",
        **build_annotation_reference_role(form, raw_values),
        "acknowledged_bias": True,
    }
    validate_preview_selection(request, form.context["resume_context"], entry_id)
    from mif_pipeline.orchestration.annotation_preview import plain

    return plain(request)


_FIT_RTREES_FIELDS = (
    ("max_depth", "Maximum tree depth", ControlKind.INTEGER),
    ("min_sample_count", "Minimum samples per node", ControlKind.INTEGER),
    ("active_var_count", "Features considered per split", ControlKind.INTEGER),
    ("max_trees", "Maximum trees", ControlKind.INTEGER),
    ("termination_epsilon", "Out-of-bag error threshold", ControlKind.NUMBER),
    ("calculate_variable_importance", "Calculate variable importance", ControlKind.CHECKBOX),
)
_FIT_SVM_FIELDS = (
    ("kernel", "SVM kernel", ControlKind.CHOICE),
    ("c", "SVM C", ControlKind.NUMBER),
    ("gamma", "RBF gamma", ControlKind.NUMBER),
    ("max_iterations", "Maximum SVM iterations", ControlKind.INTEGER),
    ("termination_epsilon", "SVM termination tolerance", ControlKind.NUMBER),
)
_FIT_LOGISTIC_REGRESSION_FIELDS = (
    ("regularization", "Logistic regression regularization", ControlKind.CHOICE),
    ("learning_rate", "Logistic regression learning rate", ControlKind.NUMBER),
    ("iterations", "Logistic regression iterations", ControlKind.INTEGER),
)


def _fit_family(settings):
    schema = settings["configuration_schema"]
    if "logistic-regression" in schema:
        return "logistic_regression"
    return "svm" if "svm" in schema else "rtrees"


def _fit_fields(family):
    return {"rtrees": _FIT_RTREES_FIELDS, "svm": _FIT_SVM_FIELDS,
            "logistic_regression": _FIT_LOGISTIC_REGRESSION_FIELDS}[family]


_FIT_CONTROL_HELP = {
    ("rtrees", "termination_epsilon"): "Stop adding trees when the out-of-bag error falls below this value. Set 0 to fit the maximum number of trees.",
    ("rtrees", "max_depth"): "Limits how deep each tree can grow. Enter a positive integer.",
    ("rtrees", "max_trees"): "Maximum number of trees to fit.",
    ("svm", "kernel"): "Linear uses a straight boundary; RBF allows curved boundaries.",
    (
        "svm",
        "c",
    ): "Higher C penalizes training observations inside the margin or on the wrong side of the boundary more strongly.",
    (
        "svm",
        "gamma",
    ): "For RBF only; controls how locally each training example influences the boundary.",
}


def _training_classifier_labels(stage: str, count: int) -> tuple[str, ...]:
    """Name fitted classifier roles without exposing their internal record IDs."""
    title = {
        "primary_classifier_training_review": "Primary compartment classifier",
        "secondary_classifier_training_review": "Secondary structure classifier",
        "supervised_phenotype_training_review": "Cell phenotype classifier",
    }[stage]
    return tuple(title if count == 1 else f"{title} {index + 1}" for index in range(count))


def _fit_review_controls(
    classifiers: Sequence[Mapping[str, object]], *, classifier_labels: Sequence[str]
) -> tuple[FormControl, ...]:
    controls = []
    for index, row in enumerate(classifiers):
        settings = row["estimator_settings"]
        schema = settings["configuration_schema"]
        family = _fit_family(settings)
        parameters = settings[family]
        prefix = f"settings_{index}"
        controls.append(
            FormControl(
                f"{prefix}_selected",
                f"Change settings for {classifier_labels[index]}",
                ControlKind.CHECKBOX,
                value=False,
                help_text="Select this classifier to adjust its training method settings.",
            )
        )
        for field, label, kind in _fit_fields(family):
            controls.append(
                FormControl(
                    f"{prefix}_{family}_{field}",
                    label,
                    kind,
                    choices=("LINEAR", "RBF") if field == "kernel" else ("L2", "DISABLE") if field == "regularization" else (),
                    value=parameters[field],
                    required=field != "gamma",
                    help_text=_FIT_CONTROL_HELP.get((family, field)),
                )
            )
    return tuple(controls)


def _project_training_fit_review(result: StageResult) -> FormModel:
    from mif_pipeline.orchestration.batch_training.record_publication import (
        validate_training_fit_review_context,
    )

    if (
        result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage not in TRAINING_PARENT_NODE_KINDS
        or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
    ):
        raise ValueError(
            "This classifier review is no longer available. Reopen Reviews and select the current result."
        )
    context = validate_training_fit_review_context(result.resume_context)
    if context["phase"] == "configure_outputs":
        if result.allowed_actions != ("retrain_classifier",):
            raise ValueError("The output review has changed. Open the current review from Run to continue.")
        row = context["classifiers"][0]
        controls, labels = _ignore_policy_controls(
            "output", value="none", available=tuple(row["available_policies"]),
        )
        return FormModel(
            form_id="training_output_review", title="Review classifier outputs",
            message=("Choose whether to fit only the selected classes or include Ignore "
                     "using the available source of training examples."),
            allowed_actions=result.allowed_actions, controls=controls, tables=(),
            context={"adapter": "training_output_review", "stage_result": result.to_mapping(),
                     "choice_labels": labels},
        )
    actions = (
        ("accept", "retrain_classifier")
        if context["phase"] == "freeze_settings"
        and not any(row.get("model_selection") for row in context["classifiers"])
        else ("accept",)
    )
    if result.allowed_actions != actions:
        raise ValueError(
            "This classifier review is no longer available. Reopen Reviews and select the current result."
        )
    classifiers = context["classifiers"]
    has_cv = any(row.get("model_selection") for row in classifiers)
    classifier_labels = _training_classifier_labels(result.stage, len(classifiers))
    labels_by_id = {row["classifier_id"]: label for row, label in zip(classifiers, classifier_labels, strict=True)}
    if context["phase"] == "freeze_settings":
        title = "Review fitted classifier" if result.stage in {"primary_classifier_training_review", "secondary_classifier_training_review"} else "Confirm classifier settings"
        messages = [
            (
                "Review the validation results."
                if any(row["validation_patient_count"] for row in classifiers)
                else "Check the subject counts."
            )
        ]
        messages.append(
            "Accept the classifier to keep these settings and run the independent test."
            if any(row["test_patient_count"] for row in classifiers)
            else "Accept the classifier to continue with this model."
        )
    else:
        title = "Review classifier test results"
        messages = ["Accept the classifier to continue with this model and its test results."]
    if has_cv:
        messages.insert(
            0,
            "Review the cross-validation scores and fitted parameters.",
        )
    if result.stage in {
        "primary_classifier_training_review", "secondary_classifier_training_review"
    }:
        messages.insert(0, "Open mIF → Review fitted classifier in QuPath to inspect its predictions before accepting the model.")
    if not has_cv and any(row["evaluation_intent"] == "training_only" for row in classifiers):
        training_only = [
            labels_by_id[row["classifier_id"]]
            for row in classifiers if row["evaluation_intent"] == "training_only"
        ]
        scope = " for " + ", ".join(training_only) if len(classifiers) > 1 else ""
        messages.append(
            "No subjects were kept separate for validation or independent testing" + scope + "."
        )
    if any(row["evaluation_intent"] == "exploratory" for row in classifiers):
        messages.append("These scores summarize the selected validation subjects.")
    adjusted = [labels_by_id[row["classifier_id"]] for row in classifiers if row["adjusted_after_validation"]]
    if adjusted:
        messages.append("Adjusted after validation: " + ", ".join(adjusted) + ".")
    if result.stage in {"primary_classifier_training_review", "secondary_classifier_training_review"}:
        messages = [message for message in messages if message not in {
            "Check the subject counts.", "Review the validation results.",
            "Review the cross-validation scores and fitted parameters.",
            "Accept the classifier to continue with this model.",
            "Accept the classifier to continue with this model and its test results.",
        }]
    intent_labels = {
        "training_only": "Training only",
        "exploratory": "Exploratory",
        "independent_performance": "Independent test",
    }
    tables = [
        FormTable(
            "training_fit_classifiers",
            (
                ("Classifier", "Review", "Development subjects", "Test subjects")
                if has_cv
                else (
                    "Classifier",
                    "Review",
                    "Training subjects",
                    "Validation subjects",
                    "Test subjects",
                )
            ),
            tuple(
                FormTableRow(
                    row["classifier_id"],
                    (
                        labels_by_id[row["classifier_id"]],
                        (
                            "Grouped CV and refit"
                            if row.get("model_selection")
                            else intent_labels[row["evaluation_intent"]]
                        ),
                        row["training_patient_count"],
                        *((row["validation_patient_count"],) if not has_cv else ()),
                        row["test_patient_count"],
                    ),
                )
                for row in classifiers
            ),
            title="Subjects and evaluation",
        )
    ]
    if result.stage in {"primary_classifier_training_review", "secondary_classifier_training_review"}:
        for index, row in enumerate(classifiers):
            observations = row.get("training_observations")
            label = labels_by_id[row["classifier_id"]]
            unit = ("Patches used for fitting" if "kronos" in row["estimator_settings"]["configuration_schema"]
                    else "Pixels used for fitting")
            if observations is None:
                messages.append(f"Fitting counts are unavailable for {label}.")
                continue
            title_unit = "patches" if "kronos" in row["estimator_settings"]["configuration_schema"] else "pixels"
            suffix = f": {label}" if len(classifiers) > 1 else ""
            tables.extend((
                FormTable(
                    f"training_classes_{index}", ("Class", unit),
                    tuple(FormTableRow(item["class_id"], (item["name"], item["count"]))
                          for item in observations["classes"]),
                    title=f"Training {title_unit} by class{suffix}",
                ),
                FormTable(
                    f"training_images_{index}", ("Image", "Subject", unit),
                    tuple(FormTableRow(item["entry_id"], (item["sample_id"], item["subject_id"], item["count"]))
                          for item in observations["images"]),
                    title=f"Training {title_unit} by image{suffix}",
                ),
            ))
    for index, row in enumerate(classifiers):
        cv = row.get("model_selection")
        if cv:
            tables.append(
                FormTable(
                    f"cv_candidates_{index}",
                    ("Candidate", "Subject-weighted macro F1", "Fold scores", "Selected"),
                    tuple(
                        FormTableRow(
                            str(item["candidate_index"]),
                            (
                                item["candidate_index"] + 1,
                                item["selection_score"],
                                ", ".join(f"{score:.4f}" for score in item["fold_scores"]),
                                (
                                    "Yes"
                                    if item["candidate_index"] == cv["selected_candidate_index"]
                                    else ""
                                ),
                            ),
                        )
                        for item in cv["candidate_scores"]
                    ),
                    title=f"Parameter comparison: {labels_by_id[row['classifier_id']]}",
                )
            )
    for index, row in enumerate(classifiers):
        settings = row["estimator_settings"]
        family = _fit_family(settings)
        labels = (
            {
                "kernel": "Kernel",
                "c": "C",
                "gamma": "RBF gamma",
                "max_iterations": "Maximum iterations",
                "termination_epsilon": "SVM termination tolerance",
            }
            if family == "svm"
            else {"regularization": "Regularization", "learning_rate": "Learning rate", "iterations": "Batch iterations"}
            if family == "logistic_regression"
            else {
                "max_depth": "Maximum tree depth",
                "min_sample_count": "Minimum samples per node",
                "active_var_count": "Features considered per split",
                "max_trees": "Maximum tree count",
                "termination_epsilon": "Out-of-bag error threshold",
                "calculate_variable_importance": "Calculate variable importance",
            }
        )
        parameters = tuple(
            FormTableRow(key, (label, settings[family][key]))
            for key, label in labels.items()
            if settings[family].get(key) is not None
            and not (family == "svm" and key == "gamma" and settings[family].get("kernel") != "RBF")
        )
        tables.append(
            FormTable(
                f"selected_parameters_{index}", ("Fitted parameter", "Value"), parameters,
                title=f"Fitted parameters: {labels_by_id[row['classifier_id']]}",
            )
        )
    for split in ("validation", "test"):
        rows = tuple(
            FormTableRow(
                row["classifier_id"],
                (
                    labels_by_id[row["classifier_id"]],
                    metrics["patient_count"],
                    metrics["accuracy"],
                    metrics["macro_precision"],
                    metrics["macro_recall"],
                    metrics["macro_f1"],
                ),
            )
            for row in classifiers
            for metrics in row["split_metrics"]
            if metrics["split"] == split
        )
        if rows:
            tables.append(
                FormTable(
                    f"{split}_results",
                    (
                        "Classifier",
                        "Subjects",
                        "Accuracy",
                        "Macro precision",
                        "Macro recall",
                        "Macro F1",
                    ),
                    rows,
                )
            )
    return FormModel(
        form_id="training_fit_review",
        title=title,
        message=" ".join(messages),
        allowed_actions=actions,
        controls=(
            _fit_review_controls(classifiers, classifier_labels=classifier_labels)
            if context["phase"] == "freeze_settings" and "retrain_classifier" in actions
            else ()
        ),
        tables=tuple(tables),
        context={"adapter": "training_fit_review", "stage_result": result.to_mapping()},
    )


def _submit_training_fit_review(
    form: FormModel, action: str, raw_values: Mapping[str, object]
) -> dict[str, object]:
    from mif_pipeline.orchestration.batch_training.record_publication import (
        validate_training_fit_review_context,
    )
    from mif_pipeline.orchestration.batch_training.plan_selection import (
        validate_training_settings_change_response,
    )

    if form.form_id == "training_output_review":
        from mif_pipeline.orchestration.batch_training.output_review import validate_output_policy_change

        result = StageResult.from_mapping(form.context["stage_result"])
        review = validate_training_fit_review_context(result.resume_context)
        if action != "retrain_classifier" or review["phase"] != "configure_outputs":
            raise ValueError("Save the selected output policy before fitting.")
        active_values = dict(raw_values)
        decoded = decode_form_values(form, action, active_values)
        policy = decoded.get("output_ignore_policy")
        return validate_output_policy_change({
            "schema_version": "classifier-settings-change/v1",
            "fit_review_sha256": review["fit_review_sha256"],
            "output_policy_by_classifier_id": {
                review["classifiers"][0]["classifier_id"]: {
                    "policy": policy,
                    "boundary_thickness_px": 0.0,
                },
            },
        }, review=review)
    if (
        form.form_id != "training_fit_review"
        or not isinstance(raw_values, Mapping)
        or set(form.context) != {"adapter", "stage_result"}
        or form.context["adapter"] != "training_fit_review"
    ):
        raise ValueError(
            "This response does not match the classifier review. Reopen Reviews and accept the current result."
        )
    try:
        result = StageResult.from_mapping(form.context["stage_result"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "The saved classifier review could not be read. Reopen Reviews and select the current result."
        ) from exc
    if (
        result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage not in TRAINING_PARENT_NODE_KINDS
        or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
    ):
        raise ValueError(
            "This classifier review is no longer available. Reopen Reviews and select the current result."
        )
    context = validate_training_fit_review_context(result.resume_context)
    actions = (
        ("accept", "retrain_classifier")
        if context["phase"] == "freeze_settings"
        and not any(row.get("model_selection") for row in context["classifiers"])
        else ("accept",)
    )
    if (
        form.allowed_actions != actions
        or result.allowed_actions != actions
        or action not in actions
    ):
        raise ValueError(
            "This classifier review is no longer available. Reopen Reviews and select the current result."
        )
    controls = (
        _fit_review_controls(
            context["classifiers"],
            classifier_labels=_training_classifier_labels(result.stage, len(context["classifiers"])),
        )
        if context["phase"] == "freeze_settings" and "retrain_classifier" in actions
        else ()
    )
    if form.controls != controls or set(raw_values) - {control.name for control in controls}:
        raise ValueError(
            "This response does not match the classifier review. Reopen Reviews and select the current result."
        )
    if action == "accept":
        return {"fit_review_sha256": context["fit_review_sha256"]}
    by_name = {control.name: control for control in controls}
    revisions: dict[str, dict[str, object]] = {}
    for index, row in enumerate(context["classifiers"]):
        prefix = f"settings_{index}"
        selected = raw_values.get(f"{prefix}_selected", False)
        if type(selected) is not bool:
            raise ValueError("Select the classifiers whose settings should change.")
        if not selected:
            continue
        settings = row["estimator_settings"]
        family = _fit_family(settings)
        parameters = dict(settings[family])
        for field, _label, _kind in _fit_fields(family):
            name = f"{prefix}_{family}_{field}"
            if family == "svm" and field == "gamma" and parameters["kernel"] == "LINEAR":
                parameters["gamma"] = None
                continue
            if name not in raw_values:
                raise ValueError(f"{by_name[name].label} is required")
            parameters[field] = decode_control_value(by_name[name], raw_values[name])
        if family == "svm" and parameters["kernel"] == "LINEAR":
            parameters["gamma"] = None
        revisions[row["classifier_id"]] = parameters
    return validate_training_settings_change_response(
        {
            "schema_version": "classifier-settings-change/v1",
            "fit_review_sha256": context["fit_review_sha256"],
            "parameters_by_classifier_id": revisions,
        },
        expected_review_sha256=context["fit_review_sha256"],
        model_configurations={
            row["classifier_id"]: row["estimator_settings"] for row in context["classifiers"]
        },
    )


def _register_projectors(
    registrations: tuple[tuple[str, _Projector], ...],
) -> Mapping[str, _Projector]:
    contexts = tuple(context for context, _projector in registrations)
    if len(contexts) != len(set(contexts)):
        raise ValueError("duplicate interaction context projector")
    return MappingProxyType(dict(registrations))


from mif_pipeline.gui.alignment_setup import build_alignment_form, submit_alignment_form

_PROJECTORS = _register_projectors(
    (
        (
            "warpy-registration-interaction/v1",
            _Projector(build_alignment_form, submit_alignment_form),
        ),
        (
            "tissue-detector-setup-interaction/v1",
            _Projector(
                build_tissue_setup_form,
                submit_tissue_setup_form,
            ),
        ),
        (
            "segmentation-setup-interaction/v1",
            _Projector(
                build_segmentation_setup_form,
                submit_segmentation_setup_form,
            ),
        ),
        (
            "measurement-setup-interaction/v1",
            _Projector(
                build_measurement_setup_form,
                submit_measurement_setup_form,
            ),
        ),
        ("secondary-definition-review/v1", _Projector(build_secondary_setup_form, submit_secondary_setup_form)),
        ("marker-gates/v1", _Projector(project_marker_gate, submit_marker_gate)),
        ("marker-combinations/v1", _Projector(build_marker_combination_form, submit_marker_combination_form)),
        (
            "batch-marker-setup/v1",
            _Projector(
                build_marker_setup_form,
                submit_marker_setup_form,
            ),
        ),
        *(
            (schema, _ROI_PROJECTOR)
            for schema in (
                "tissue-native-resolution-interaction/v1",
                "tissue-detection-channel-interaction/v1",
                "tissue-thresholder-interaction/v1",
                "tissue-section-review-interaction/v1",
                "analysis-domain-choice-interaction/v1",
                "analysis-domain-rois-interaction/v1",
                "analysis-domain-review-interaction/v1",
                "roi-moving-reference-availability/v1",
                "roi-moving-reference-usage/v1",
                "registration-review-interaction/v1",
                "transfer-commit-recovery-interaction/v1",
                "moving-assistance-scope-interaction/v1",
                "classifier-reuse-interaction/v1",
                "primary-classifier-training-interaction/v1",
                "primary-classifier-review-interaction/v1",
                "secondary-classifier-training-interaction/v1",
                "secondary-classifier-review-interaction/v1",
                "primary-count-review/v1",
                "secondary-structure-selection/v1",
                "semantic-roi-review-interaction/v1",
                "semantic-region-interaction/v1",
                "semantic-region-interaction/v2",
                "phenotyping-interaction/v1",
                "phenotyping-interaction/v2",
                "supervised-phenotyping-interaction/v1",
                "batch-clustering-setup/v2",
            )
        ),
        ("mif-batch-acquisition-organization/v1", _Projector(_project_organization, _submit_organization)),
        ("mif-batch-stage1-interaction/v1", _Projector(_project_stage1, _submit_stage1)),
        (
            "mif-batch-reference-unmixing-interaction/v2",
            _Projector(_project_reference, _submit_reference),
        ),
        (
            TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION,
            _Projector(
                _project_training_plan,
                _submit_training_plan,
            ),
        ),
        ("supervised-reference-review/v1",
         _Projector(_project_supervised_references, _submit_supervised_references)),
        (
            "training-fit-review/v4",
            _Projector(_project_training_fit_review, _submit_training_fit_review),
        ),
        (
            "training-fit-review/v5",
            _Projector(_project_training_fit_review, _submit_training_fit_review),
        ),
    )
)


def supported_interaction_context_versions() -> tuple[str, ...]:
    """Return the exact registered form-projector context versions."""
    return tuple(sorted(_PROJECTORS))


def build_batch_interaction_form(
    group: BatchInteractionGroup,
    *,
    expected_state_head_sha256: str,
    target_id: str | None = None,
    reference_option: str | None = None,
    segmentation_model_directory: Path | None = None,
) -> BatchInteractionForm | UnsupportedBatchInteraction:
    """Project one exact group without inventing an owner response schema."""

    if type(group) is not BatchInteractionGroup:
        raise TypeError("batch interaction form requires a BatchInteractionGroup")
    if target_id is None:
        representative = group.targets[0]
    else:
        matches = tuple(item for item in group.targets if item.target_id == target_id)
        if len(matches) != 1:
            raise ValueError("selected target does not belong to the interaction group")
        representative = matches[0]
    context = representative.stage_result.resume_context or {}
    context_schema_version = context.get("schema_version")
    if type(context_schema_version) is not str or not context_schema_version:
        raise ValueError("representative interaction lacks a context schema version")
    target_ids = tuple(target.target_id for target in group.targets)
    projector = _PROJECTORS.get(context_schema_version)
    if projector is None:
        return UnsupportedBatchInteraction(
            stage=group.stage,
            reason=group.interaction_reason,
            action_schema_version=group.action_schema_version,
            context_schema_version=context_schema_version,
            target_ids=target_ids,
        )
    if context_schema_version == "mif-batch-reference-unmixing-interaction/v2":
        form = projector.project(representative.stage_result, option_value=reference_option,
                                 saved_spectral_source=representative.saved_spectral_source)
    elif context_schema_version == "tissue-detector-setup-interaction/v1":
        form = projector.project(
            representative.stage_result,
            validation_domain_id=representative.validation_domain_id,
        )
    elif context_schema_version == "tissue-section-review-interaction/v1":
        form = projector.project(
            representative.stage_result,
            region_review=representative.region_review,
            tissue_section_summary_error=representative.tissue_section_summary_error,
        )
    elif context_schema_version in {
        "analysis-domain-choice-interaction/v1",
        "analysis-domain-rois-interaction/v1",
        "analysis-domain-review-interaction/v1",
        "semantic-region-interaction/v2",
        "secondary-structure-selection/v1",
    }:
        form = projector.project(
            representative.stage_result,
            region_review=representative.region_review,
        )
    elif context_schema_version == "segmentation-setup-interaction/v1":
        form = projector.project(
            representative.stage_result, model_directory=segmentation_model_directory
        )
    elif context_schema_version == TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION:
        form = projector.project(representative.stage_result,
                                 saved_supervised_setup=representative.saved_supervised_setup)
    else:
        form = projector.project(representative.stage_result)
    return BatchInteractionForm(
        group_id=group.group_id,
        group_sha256=group.group_sha256,
        expected_state_head_sha256=expected_state_head_sha256,
        representative_target_id=representative.target_id,
        target_ids=target_ids,
        form=form,
    )


def build_batch_interaction_request(
    view: BatchInteractionForm,
    *,
    target_id: str,
    action: str | None,
    raw_values: Mapping[str, object],
    count_target_ids: tuple[str, ...] | None = None,
) -> BatchInteractionResponseRequest | None:
    """Validate and bind one selected target; cancellation creates no request."""

    if type(view) is not BatchInteractionForm:
        raise TypeError("batch interaction requests require a BatchInteractionForm")
    if action is None:
        return None
    if type(target_id) is not str or target_id not in view.target_ids:
        raise ValueError("selected target does not belong to the interaction group")
    if (
        view.form.context.get("stage_result", {}).get("resume_context", {}).get("schema_version")
        in _TARGET_SPECIFIC_SCHEMAS
        and target_id != view.representative_target_id
    ):
        raise ValueError("selected target differs from the projected owner context")
    projector = _PROJECTORS.get(
        view.form.context.get("resume_context", {}).get("schema_version")
        or view.form.context.get("stage_result", {}).get("resume_context", {}).get("schema_version")
    )
    if projector is None:
        raise ValueError("batch interaction form has no registered response binder")
    response = projector.submit(view.form, action, raw_values)
    if response is None:
        return None
    selected = (target_id,)
    if count_target_ids is not None:
        if (view.form.context.get("reason") != "primary_structure_count_review_required"
                or action not in {"exclude_analysis_domain", "accept_all_primary_structures"}
                or response or not count_target_ids or not set(count_target_ids).issubset(view.target_ids)):
            raise ValueError("Select pending analysis regions from this count review.")
        selected = count_target_ids
    return BatchInteractionResponseRequest(
        group_id=view.group_id,
        group_sha256=view.group_sha256,
        selection=BatchTargetSelection(target_ids=selected),
        action=action,
        response=response,
        expected_state_head_sha256=view.expected_state_head_sha256,
    )


__all__ = [
    "supported_interaction_context_versions",
    "BatchInteractionForm",
    "UnsupportedBatchInteraction",
    "build_batch_interaction_form",
    "build_batch_interaction_request",
]
