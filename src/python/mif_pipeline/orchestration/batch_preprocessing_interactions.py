"""Owner-bound Stage-1 and reference-unmixing batch response validation.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from mif_pipeline.pipeline_core import (
    INTERACTION_ACTION_SCHEMA_VERSION,
    InteractionReason,
)

from .batch_interactions import (
    BatchInteractionError,
    BatchInteractionOwnerBinding,
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from .batch_preprocessing_support import (
    REFERENCE_UNMIXING_START_RESPONSE_VERSION,
    _resolved_sample_mapping,
    artifact_path,
    reference_unmixing_start_response_to_request,
)
from .batch_preprocessing_wire import canonical_bytes
from .batch_preprocessing_reuse import load_reusable_preprocessing_input
from .batch_reference_unmixing_execution import (
    REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION,
    REFERENCE_UNMIXING_INTERACTION_VERSION,
    _load_reference_request,
    _load_reviewed_source_pair,
    _reference_paths,
    _reference_start_stage_result,
    _require_current_source_request,
    reference_unmixing_state_to_stage_result,
)
from .batch_stage1_execution import (
    STAGE1_INTERACTION_VERSION,
    _apply_stage1_response,
    _load_authority,
    _channel_review_workflow,
    stage1_workflow_to_stage_result,
)
from .reference_unmixing import (
    ReferenceUnmixingOperation,
    ReferenceUnmixingState,
    configure_reference_unmixing_action,
)

# ==============================================================================
#                        Batch preprocessing interactions
# ==============================================================================


def _plain_response(response: Mapping[str, object]) -> dict[str, object]:
    return json.loads(canonical_bytes(response))


def _single_input(base, kind: str):
    matches = tuple(item for item in base.input_artifacts if item.artifact_kind == kind)
    if len(matches) != 1:
        raise BatchInteractionError(f"preprocessing interaction requires one {kind} input")
    return matches[0]


def _current_base(state_store, target: BatchInteractionTarget, kind: str):
    head = state_store.load_head()
    base = state_store.reopen_interaction_base_authorization_request(
        target.interaction_request_id, expected_head_sha256=head.sha256
    )
    if base.node.node_id != target.node_id or base.node.kind != kind:
        raise BatchInteractionError("preprocessing interaction base is foreign")
    if (
        target.sample_id not in base.node.participant_ids
        or base.node.validation_domain_id != target.validation_domain_id
        or tuple(base.node.analysis_domain_bindings) != target.analysis_domain_bindings
    ):
        raise BatchInteractionError("preprocessing interaction target scope is foreign")
    return base, head.sha256


def _outcome(target, base, body, *, accepted: bool, validator_id: str, version: str):
    if not accepted:
        return BatchTargetResponseValidation(
            target.target_id,
            target.node_id,
            "rejected",
            None,
            None,
            None,
            None,
            "preprocessing_response_invalid",
        )
    return BatchTargetResponseValidation(
        target.target_id,
        target.node_id,
        "accepted",
        body,
        base,
        validator_id,
        version,
        None,
    )


def _stage1_validator(state_store):
    def validate(target: BatchInteractionTarget, action: str, response: Mapping[str, object]):
        if not isinstance(target, BatchInteractionTarget):
            raise BatchInteractionError("Stage-1 response requires an authenticated target")
        base, _ = _current_base(state_store, target, "channel_review")
        inspection = _single_input(base, "source_inspection")
        context = target.stage_result.resume_context or {}
        owner_context = context.get("service_context")
        if (
            context.get("schema_version") != STAGE1_INTERACTION_VERSION
            or not isinstance(owner_context, Mapping)
            or owner_context.get("inspection_artifact_ref_sha256") != inspection.artifact_ref_sha256
        ):
            raise BatchInteractionError("Stage-1 interaction inspection is foreign")
        authority = _load_authority(inspection)
        sample = None
        if base.node.settled_review_revision is not None:
            snapshot = state_store.reopen_execution_snapshot()
            selected = next(item for item in snapshot.manifest.enabled_samples
                            if item.sample_id == target.sample_id)
            sample = _resolved_sample_mapping(selected, "channel_review")
        workflow, _ = _channel_review_workflow(authority, inspection, base.input_artifacts,
                                               base.node.to_mapping(), sample)
        expected = stage1_workflow_to_stage_result(
            workflow,
            slide_id=target.slide_id,
            inspection_artifact_ref_sha256=inspection.artifact_ref_sha256,
            inspection_authority_sha256=authority.content_sha256,
        )
        if expected.to_mapping() != target.stage_result.to_mapping():
            raise BatchInteractionError("Stage-1 interaction owner context is stale")
        body = _plain_response(response)
        try:
            if action != "accept" or action not in target.stage_result.allowed_actions:
                raise ValueError("Stage-1 action is not offered")
            _apply_stage1_response(workflow, body, authority=authority)
        except (TypeError, ValueError):
            return _outcome(
                target,
                base,
                body,
                accepted=False,
                validator_id="batch-stage1-response",
                version="mif-batch-stage1-response/v1",
            )
        return _outcome(
            target,
            base,
            body,
            accepted=True,
            validator_id="batch-stage1-response",
            version="mif-batch-stage1-response/v1",
        )

    return validate


def _inspection_validator(state_store):
    def validate(target: BatchInteractionTarget, action: str, response: Mapping[str, object]):
        from .batch_acquisition_organization import (load_pending_inspection,
            organization_stage_result, validate_organization_response, RESPONSE_VERSION,
            previous_inspection_artifact, organization_seed)
        if not isinstance(target, BatchInteractionTarget):
            raise BatchInteractionError("Inspection response requires an authenticated target")
        base, _ = _current_base(state_store, target, "inspection")
        snapshot = state_store.reopen_execution_snapshot()
        samples = tuple(item for item in snapshot.manifest.enabled_samples if item.sample_id == target.sample_id)
        if len(samples) != 1 or base.input_artifacts:
            raise BatchInteractionError("Inspection response has foreign source inputs")
        sample = _resolved_sample_mapping(samples[0], "inspection")
        authority, digest = load_pending_inspection(sample, target.node_id)
        previous_ref = previous_inspection_artifact(state_store, base.node, snapshot.head)
        previous = None if previous_ref is None else _load_authority(previous_ref)
        if previous is not None:
            from .batch_stage1_execution import _validate_source_selection
            _validate_source_selection(previous, sample)
        expected = organization_stage_result(authority, slide_id=target.slide_id,
            node_id=target.node_id, pending_sha256=digest, seed=organization_seed(authority, previous))
        if expected.to_mapping() != target.stage_result.to_mapping():
            raise BatchInteractionError("Inspection cycle review no longer matches its original source")
        body = _plain_response(response)
        try:
            if action != "accept" or action not in expected.allowed_actions:
                raise ValueError("Inspection action is not offered")
            validate_organization_response(authority, expected, body)
        except (TypeError, ValueError):
            return _outcome(target, base, body, accepted=False,
                validator_id="batch-acquisition-organization-response", version=RESPONSE_VERSION)
        return _outcome(target, base, body, accepted=True,
            validator_id="batch-acquisition-organization-response", version=RESPONSE_VERSION)
    return validate


def same_reference_request(expected, observed) -> bool:
    """Compare executable request identity independently of derived review display.

    Response authorization still binds the original stored request and its hash.
    The summary is regenerated from checkpoint-bound artifacts and can gain display
    details between releases. Checkpoints, inputs, configuration and operative
    choices remain exact; the retired final-review no-op cannot be submitted.
    """
    left, right = expected.to_mapping(), observed.to_mapping()
    for value in (left, right):
        value.pop("message", None)
        state = (value.get("resume_context") or {}).get("service_context", {}).get(
            "operation_state"
        )
        if not isinstance(state, dict):
            continue
        state.pop("review_summary", None)
        if state.get('checkpoint') in {'positive_reference_rois', 'af_reference_rois',
                                       'negative_reference_rois', 'background_reference_rois'}:
            # A release may add the compound presentation of the same existing
            # phase checkpoint. Legacy responses retain their exact old intent.
            state['offered_actions'] = [item for item in state.get('offered_actions', ())
                                        if item != 'accept_reference_regions']
            state['operation_requirements'] = [item for item in state.get('operation_requirements', ())
                if not isinstance(item, dict) or item.get('option_value') != 'accept_reference_regions']
        for requirement in state.get("operation_requirements", ()):
            if not isinstance(requirement, dict):
                continue
            requirement.pop("label", None)
            requirement.pop("description", None)
            for field in requirement.get("form_fields", ()):
                if isinstance(field, dict):
                    field.pop("label", None)
                    field.pop("help_text", None)
        if state.get("checkpoint") == "final_run_basis":
            # Older saved forms offered a no-op "review again" choice. Its
            # retirement must not invalidate acceptance or changing references.
            if isinstance(state.get("offered_actions"), list):
                state["offered_actions"] = [
                    action for action in state["offered_actions"]
                    if action != "retry_final_run_basis"
                ]
            if isinstance(state.get("operation_requirements"), list):
                state["operation_requirements"] = [
                    requirement for requirement in state["operation_requirements"]
                    if not isinstance(requirement, dict)
                    or requirement.get("option_value") != "retry_final_run_basis"
                ]
    return left == right


def _reference_validator(state_store):
    def validate(target: BatchInteractionTarget, action: str, response: Mapping[str, object]):
        if not isinstance(target, BatchInteractionTarget):
            raise BatchInteractionError("reference response requires an authenticated target")
        base, head_sha256 = _current_base(state_store, target, "reference_unmixing")
        inspection = _single_input(base, "source_inspection")
        review = _single_input(base, "stage1_review")
        if any(
            item.artifact_kind
            not in {"source_inspection", "stage1_review", "spectral_reference_library"}
            for item in base.input_artifacts
        ):
            raise BatchInteractionError("reference interaction has unsupported source input")
        context = target.stage_result.resume_context or {}
        owner_context = context.get("service_context")
        if (
            context.get("schema_version") != REFERENCE_UNMIXING_INTERACTION_VERSION
            or not isinstance(owner_context, Mapping)
            or owner_context.get("source_inspection_artifact_ref_sha256")
            != inspection.artifact_ref_sha256
            or owner_context.get("source_inspection_compatibility_signature_sha256")
            != inspection.compatibility_signature_sha256
            or owner_context.get("stage1_review_artifact_ref_sha256") != review.artifact_ref_sha256
            or owner_context.get("stage1_review_compatibility_signature_sha256")
            != review.compatibility_signature_sha256
        ):
            raise BatchInteractionError("reference interaction source pair is foreign")
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=head_sha256)
        samples = tuple(
            item for item in snapshot.manifest.samples if item.sample_id == target.sample_id
        )
        if len(samples) != 1 or samples[0].slide_id != target.slide_id:
            raise BatchInteractionError("reference interaction sample is foreign")
        sample = _resolved_sample_mapping(samples[0], "reference_unmixing")
        try:
            authority, reviewed_ome, replacement = _load_reviewed_source_pair(
                inspection,
                review,
                sample,
            )
        except (TypeError, ValueError) as exc:
            raise BatchInteractionError("reference interaction source pair is invalid") from exc
        from .batch_reference_unmixing_execution import _reference_revision_node_id, _previous_reference_request
        node = next(item for item in snapshot.plan.nodes if item.node_id == target.node_id).to_mapping()
        run_id, root, request_path, output_path = _reference_paths(sample, inspection, review,
            revision_node_id=_reference_revision_node_id(node))
        library_refs = tuple(
            item
            for item in base.input_artifacts
            if item.artifact_kind == "spectral_reference_library"
        )
        if len(library_refs) > 1:
            raise BatchInteractionError("reference interaction has duplicate library inputs")
        library = library_refs[0] if library_refs else None
        if library is not None:
            try:
                loaded_library = load_reusable_preprocessing_input(library)
            except (TypeError, ValueError) as exc:
                raise BatchInteractionError(
                    "reference interaction library input is invalid"
                ) from exc
        else:
            loaded_library = None
        retained_component_request = None
        if request_path.exists():
            owner_request, payload = _load_reference_request(request_path)
            try:
                _require_current_source_request(
                    owner_request,
                    run_id=run_id,
                    authority=authority,
                    reviewed_ome=reviewed_ome,
                    replacement=replacement,
                    output_path=output_path,
                )
            except (TypeError, ValueError) as exc:
                raise BatchInteractionError("reference interaction request is foreign") from exc
            if (
                owner_request.spectral_state_override in {"unmixed", "isolated"}
                and owner_context.get("interaction_kind") == "configure_reference_unmixing"
                and owner_context.get("operation_status") == "unstarted"
            ):
                # We retain the reviewed copy request while the ordinary image
                # publisher reuses its completed output on the subsequent run.
                retained_component_request = owner_request
                current = None
                previous_request, _ = _previous_reference_request(
                    node, sample, inspection, review, authority, reviewed_ome, replacement)
                expected = _reference_start_stage_result(
                    slide_id=target.slide_id, inspection=inspection, review=review,
                    operation_root=root, request_path=request_path,
                    candidate_library=library, reviewed_ome=reviewed_ome,
                    previous_request=previous_request,
                )
            else:
                current = ReferenceUnmixingOperation.open_existing(root).status(run_id)
                if not isinstance(current, ReferenceUnmixingState):
                    raise BatchInteractionError("completed reference operation has no pending response")
                expected = reference_unmixing_state_to_stage_result(
                    current,
                    slide_id=target.slide_id,
                    inspection_artifact_ref_sha256=inspection.artifact_ref_sha256,
                    inspection_compatibility_signature_sha256=inspection.compatibility_signature_sha256,
                    review_artifact_ref_sha256=review.artifact_ref_sha256,
                    review_compatibility_signature_sha256=review.compatibility_signature_sha256,
                    operation_root=root,
                    request_path=request_path,
                    request_sha256=hashlib.sha256(payload).hexdigest(),
                    request_byte_count=len(payload),
                    candidate_library=library,
                )
        else:
            current = None
            previous_request, _ = _previous_reference_request(
                node, sample, inspection, review, authority, reviewed_ome, replacement)
            expected = _reference_start_stage_result(
                slide_id=target.slide_id,
                inspection=inspection,
                review=review,
                operation_root=root,
                request_path=request_path,
                candidate_library=library,
                reviewed_ome=reviewed_ome,
                previous_request=previous_request,
            )
        if not same_reference_request(expected, target.stage_result):
            raise BatchInteractionError(
                "This review no longer matches the current unmixing settings. "
                "Return to Workflow and reopen Spectral unmixing to load the current review."
            )
        body = _plain_response(response)
        try:
            if action != "accept" or action not in target.stage_result.allowed_actions:
                raise ValueError("reference action is not offered")
            if current is None:
                owner_request = reference_unmixing_start_response_to_request(
                    body,
                    run_id=run_id,
                    authority=authority,
                    reviewed_ome=reviewed_ome,
                    output_path=output_path,
                    accepted_name_choice=replacement,
                )
                if retained_component_request is not None and owner_request != retained_component_request:
                    raise ValueError("Retry uses the saved component-image settings; revise the analysis to change them.")
                if owner_request.replace_channel_names_with_biomarkers is not replacement:
                    raise ValueError("reference configuration conflicts with Stage-one name choice")
                version = body["schema_version"]
            else:
                if (
                    set(body)
                    != {
                        "schema_version",
                        "checkpoint_token",
                        "option_value",
                        "form_values",
                        "injected_configuration",
                    }
                    or body.get("schema_version") != REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION
                    or body.get("checkpoint_token") != current.checkpoint_token
                ):
                    raise ValueError("reference action response is stale or malformed")
                form_values = body["form_values"]
                if not isinstance(form_values, Mapping):
                    raise ValueError("reference action form values are invalid")
                resolved_form_values = dict(form_values)
                if (
                    body["option_value"] in {"import_reference_library", "curated_reference_library"}
                    and loaded_library is not None
                ):
                    pinned_library = artifact_path(
                        sample,
                        "reference-unmixing/reusable/" f"{loaded_library.ref.sha256}.json",
                    )
                    supplied = resolved_form_values.get("library_locator")
                    if supplied is not None and supplied != str(pinned_library):
                        raise ValueError("manifest spectral library locator is adapter-owned")
                    resolved_form_values["library_locator"] = str(pinned_library)
                configure_reference_unmixing_action(
                    current,
                    option_value=body["option_value"],
                    form_values=resolved_form_values,
                    injected_configuration=body["injected_configuration"],
                )
                if current.checkpoint == "basis_source" and body["option_value"] in {
                    "phenoimager_motif_profile",
                    "import_reference_library",
                    "construct_reference_library",
                }:
                    try:
                        from pathlib import Path

                        library_bytes = None
                        if body["option_value"] == "import_reference_library":
                            from mif_pipeline.preprocessing.unmixing.library_exchange import (
                                read_library_payload,
                            )

                            library_bytes = (
                                loaded_library.payload
                                if loaded_library is not None
                                else read_library_payload(resolved_form_values["library_locator"])
                            )
                        ReferenceUnmixingOperation.open_existing(root).validate_basis_choice(
                            run_id,
                            current.checkpoint_token,
                            body["option_value"],
                            library_bytes=library_bytes,
                        )
                    except (ValueError, OSError) as exc:
                        # The pending interaction and earlier results remain intact.
                        raise BatchInteractionError(
                            "Check the spectral references: " + str(exc)
                        ) from exc
                if body["option_value"] == "curated_reference_library":
                    try:
                        ReferenceUnmixingOperation.open_existing(root).validate_supplied_library(
                            run_id,
                            current.checkpoint_token,
                            **{k: v for k, v in resolved_form_values.items() if k != "reviewer_id"},
                        )
                    except (ValueError, OSError) as exc:
                        raise BatchInteractionError(
                            "Check the spectral library: " + str(exc)
                        ) from exc
                version = REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION
        except BatchInteractionError:
            raise
        except (TypeError, ValueError):
            return _outcome(
                target,
                base,
                body,
                accepted=False,
                validator_id="batch-reference-unmixing-response",
                version=body.get("schema_version", "invalid"),
            )
        return _outcome(
            target,
            base,
            body,
            accepted=True,
            validator_id="batch-reference-unmixing-response",
            version=version,
        )

    return validate


def registered_preprocessing_interaction_owner_keys() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (stage, InteractionReason.OTHER_EXPLICIT_REASON.value, INTERACTION_ACTION_SCHEMA_VERSION)
        for stage in ("inspection", "channel_review", "reference_unmixing")
    )


def build_preprocessing_interaction_owner_bindings(*, state_store):
    """Register only the two executable preprocessing interaction families."""

    validators = (_inspection_validator, _stage1_validator, _reference_validator)
    return tuple(
        BatchInteractionOwnerBinding(stage, reason, version, factory(state_store))
        for (stage, reason, version), factory in zip(
            registered_preprocessing_interaction_owner_keys(), validators, strict=True
        )
    )
