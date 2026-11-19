"""Pure UI response binding for batch preprocessing owner prompts.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from mif_pipeline.pipeline_core import (
    InteractionReason,
    StageResult,
    StageStatus,
)

from .batch_preprocessing_wire import canonical_bytes, canonical_digest
from .batch_reference_unmixing_execution import (
    REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION,
    REFERENCE_UNMIXING_INTERACTION_VERSION,
)
from .batch_preprocessing_support import REFERENCE_UNMIXING_START_RESPONSE_VERSION
from .batch_stage1_execution import STAGE1_INTERACTION_VERSION

_STAGE1_RESPONSE_VERSION = "mif-batch-stage1-response/v1"


# ==============================================================================
#                      Batch preprocessing response binding
# ==============================================================================


def _plain_mapping(value: Mapping[str, object], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise ValueError(f"{field} must be a string-keyed object")
    return json.loads(canonical_bytes(value))


def _owner_context(
    result: StageResult,
    action: str,
    *,
    stage: str,
    version: str,
) -> Mapping[str, object]:
    if (
        not isinstance(result, StageResult)
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.stage != stage
        or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
        or action != "accept"
        or action not in result.allowed_actions
    ):
        raise ValueError("preprocessing response names a foreign or unoffered action")
    context = result.resume_context
    if not isinstance(context, Mapping) or context.get("schema_version") != version:
        raise ValueError("preprocessing interaction context version is foreign")
    owner = context.get("service_context")
    if not isinstance(owner, Mapping) or owner.get("schema_version") != version:
        raise ValueError("preprocessing owner context version is foreign")
    return owner


def _declared_names(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} contains a malformed row")
        name = item.get("name")
        if type(name) is not str or not name:
            raise ValueError(f"{field} contains an unnamed row")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError(f"{field} contains duplicate names")
    return tuple(names)


def bind_stage1_response(
    result: StageResult,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """Bind displayed Stage-1 rows and an explicit name choice to one response."""

    owner = _owner_context(
        result,
        action,
        stage="channel_review",
        version=STAGE1_INTERACTION_VERSION,
    )
    if owner.get(
        "response_schema_version"
    ) != _STAGE1_RESPONSE_VERSION or result.checkpoint_id != canonical_digest(owner):
        raise ValueError("Stage-1 response context is stale")
    raw = _plain_mapping(raw_values, "Stage-1 values")
    status = owner.get("workflow_status")
    content = raw.get("channel_content")
    if content not in {"raw", "unmixed"}:
        raise ValueError("Choose raw measurement bands or separated components")
    if owner.get("source_spectral_state") not in {"unknown", content}:
        raise ValueError("The channel content conflicts with the inspected source")
    replacement = raw.get("replace_channel_names_with_biomarkers")
    if type(replacement) is not bool:
        raise ValueError("Stage-1 requires an explicit display-name choice")
    if status in {"needs_biomarkers", "needs_channel_name_replacement"}:
        if set(raw) - {"channel_cycle_values"} != {
            "biomarker_values",
            "component_values",
            "replace_channel_names_with_biomarkers",
            "channel_content",
            "nuclear_stain",
        }:
            raise ValueError("Stage-1 biomarker response fields differ")
        rows = owner.get("channel_review_rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("Stage-1 channel rows are malformed")
        row_ids = tuple(row.get("row_id") if isinstance(row, Mapping) else None for row in rows)
        if (
            not row_ids
            or any(type(row_id) is not str or not row_id for row_id in row_ids)
            or len(set(row_ids)) != len(row_ids)
        ):
            raise ValueError("Stage-1 channel row identities are malformed")
        values = raw["biomarker_values"]
        if not isinstance(values, Mapping) or (content == "unmixed" and set(values) != set(row_ids)):
            raise ValueError("Stage-1 biomarker values do not cover displayed rows")
        components = raw["component_values"]
        if not isinstance(components, list) or len(components) > 512:
            raise ValueError("Stage-1 component values must be a bounded list")
        if content == "raw" and (not components or values):
            raise ValueError("Raw review requires component assignments and no source-band assignments")
        if content == "unmixed" and components:
            raise ValueError("Separated input uses its existing channel assignments")
        for row in (components if content == "raw" else values.values()):
            if (
                not isinstance(row, Mapping)
                or not {"assignment", "biomarker"}.issubset(row)
                or set(row) - {"assignment", "biomarker", "component_name"}
            ):
                raise ValueError("Stage-1 biomarker row fields differ")
        cycle_values = raw.get("channel_cycle_values", [])
        if not isinstance(cycle_values, list):
            raise ValueError("Stage-1 acquired-channel cycles must be a list")
        if cycle_values:
            from mif_pipeline.io.metadata.cycles import validate_channel_cycle_values
            validate_channel_cycle_values(tuple(row["channel_id"] for row in rows), cycle_values)
    else:
        raise ValueError("Stage-1 workflow status is not interactive")
    response = {
        "schema_version": _STAGE1_RESPONSE_VERSION,
        "workflow_status": status,
        "biomarker_values": values,
        "component_values": components,
        "replace_channel_names_with_biomarkers": replacement,
        "channel_content": content,
        "nuclear_stain": raw["nuclear_stain"],
    }
    if "channel_cycle_values" in raw:
        response["channel_cycle_values"] = cycle_values
    return response


def bind_reference_unmixing_response(
    result: StageResult,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """Bind one displayed reference start or operation form to its checkpoint."""

    owner = _owner_context(
        result,
        action,
        stage="reference_unmixing",
        version=REFERENCE_UNMIXING_INTERACTION_VERSION,
    )
    raw = _plain_mapping(raw_values, "reference-unmixing values")
    kind = owner.get("interaction_kind")
    if kind == "configure_reference_unmixing":
        if (
            owner.get("response_schema_version")
            not in {
                REFERENCE_UNMIXING_START_RESPONSE_VERSION,
                "mif-batch-reference-unmixing-start/v4",
            }
            or owner.get("operation_status") != "unstarted"
            or owner.get("request_present") is not False
            or result.checkpoint_id != canonical_digest(owner)
        ):
            raise ValueError("reference start context is stale")
        names = _declared_names(owner.get("configuration_fields"), "reference configuration fields")
        expected_names = set(names)
        if raw.get("spectral_state_override") == "isolated":
            if "measurement_band_mapping" not in owner:
                raise ValueError("The measurement-band mapping was not offered for review")
            expected_names.add("measurement_band_components")
        if owner.get("response_schema_version") == "mif-batch-reference-unmixing-start/v4":
            if "calibration_request" not in owner or set(raw) != expected_names | {"calibration"}:
                raise ValueError("Missing-calibration values differ from the displayed request")
            return {"schema_version": "mif-batch-reference-unmixing-start/v4", **raw}
        if set(raw) == expected_names:
            return {"schema_version": REFERENCE_UNMIXING_START_RESPONSE_VERSION, **raw}
        raise ValueError("reference start values differ from displayed fields")
    if kind != "reference_unmixing_operation":
        raise ValueError("reference operation kind is foreign")
    state = owner.get("operation_state")
    if (
        owner.get("response_schema_version") != REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION
        or owner.get("request_present") is not True
        or not isinstance(state, Mapping)
        or state.get("schema_name") != "mif_pipeline.reference_unmixing_state"
        or state.get("schema_version") != 3
        or result.checkpoint_id != state.get("checkpoint_token")
        or result.generation_id != state.get("run_id")
    ):
        raise ValueError("reference operation context is stale")
    if set(raw) != {"option_value", "form_values", "injected_configuration"}:
        raise ValueError("reference operation response fields differ")
    option = raw["option_value"]
    offered = state.get("offered_actions")
    requirements = state.get("operation_requirements")
    if (
        type(option) is not str
        or not isinstance(offered, Sequence)
        or isinstance(offered, (str, bytes))
        or option not in offered
        or not isinstance(requirements, Sequence)
        or isinstance(requirements, (str, bytes))
    ):
        raise ValueError("reference operation option is not offered")
    matches = tuple(
        item
        for item in requirements
        if isinstance(item, Mapping) and item.get("option_value") == option
    )
    if len(matches) != 1:
        raise ValueError("reference operation requirement is ambiguous")
    form_values = raw["form_values"]
    injected = raw["injected_configuration"]
    if not isinstance(form_values, Mapping) or not isinstance(injected, Mapping):
        raise ValueError("reference operation values must be objects")
    names = set(_declared_names(matches[0].get("form_fields"), "reference form fields"))
    candidate_library = all(
        type(owner.get(field)) is str and bool(owner[field])
        for field in (
            "candidate_spectral_library_artifact_id",
            "candidate_spectral_library_artifact_ref_sha256",
            "candidate_spectral_library_compatibility_signature_sha256",
        )
    )
    optional = (
        {"library_locator"} if option == "import_reference_library" and candidate_library else set()
    )
    if not names - optional <= set(form_values) <= names:
        raise ValueError("reference operation values differ from displayed form fields")
    return {
        "schema_version": REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION,
        "checkpoint_token": state["checkpoint_token"],
        "option_value": option,
        "form_values": form_values,
        "injected_configuration": injected,
    }


__all__ = ["bind_stage1_response", "bind_reference_unmixing_response"]
