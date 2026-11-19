"""Recover a reference form or resume an accepted basis before output publication.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from pathlib import Path
from collections.abc import Mapping
from mif_pipeline.pipeline_core import StageStatus

INPUT_ERRORS = frozenset(
    {
        "preprocessing_attributeerror",
        "preprocessing_keyerror",
        "preprocessing_spectrallibraryerror",
        "preprocessing_referenceartifacterror",
        "preprocessing_filenotfounderror",
        "preprocessing_permissionerror",
        "preprocessing_sourceopenerror",
        "preprocessing_valueerror",
        "preprocessing_isadirectoryerror",
        "preprocessing_unmixingworkflowerror",
        "preprocessing_runtimeerror",
        "preprocessing_oserror",
        "preprocessing_memoryerror",
        "reference_unmixing_aborted",
    }
)

# ==============================================================================
#                            Batch reference recovery
# ==============================================================================


def validate_unchanged_reference_form(failed, prior, current: Mapping, output: Path) -> None:
    """A source-choice failure is recoverable only before the form advances."""
    old = (prior.resume_context or {}).get("service_context", {}).get("operation_state", {})
    if (
        failed.stage != "reference_unmixing"
        or failed.status is not StageStatus.FAILED
        or failed.error_code not in INPUT_ERRORS
        or prior.stage != failed.stage
        or prior.status is not StageStatus.NEEDS_INTERACTION
        or failed.slide_id != prior.slide_id
        or current.get("status") != "awaiting_operation"
        or current.get("checkpoint") != "basis_source"
        or old.get("checkpoint") != "basis_source"
        or current.get("checkpoint_token") != old.get("checkpoint_token")
        or current.get("run_id") != old.get("run_id")
        or current.get("target_id") != old.get("target_id")
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError(
            "The saved spectral-reference step has changed. Open Details to review its current state."
        )


def reopen_reference_input_failure(store, snapshot, node_id: str, terminal_digest: str, pending=None):
    """Return (sample ID, recovery kind) without executing unmixing."""
    from .batch_preprocessing_support import _resolved_sample_mapping
    from .batch_reference_unmixing_execution import (
        _load_reviewed_source_pair,
        _reference_paths,
        _load_reference_request,
        _require_current_source_request,
    )
    from .reference_unmixing import ReferenceUnmixingOperation, ReferenceUnmixingState

    head = snapshot.head
    nodes = tuple(n for n in snapshot.plan.nodes if n.node_id == node_id)
    attempts = head.attempt_ids_by_node.get(node_id, ())
    if (
        len(nodes) != 1
        or nodes[0].kind != "reference_unmixing"
        or not attempts
        or node_id in head.invalidated_node_ids
        or head.pending_authorization_sha256_by_node.get(node_id)
        != (None if pending is None else pending.authorization_sha256)
        or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
        or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != terminal_digest
    ):
        raise ValueError(
            "This spectral-reference correction is no longer current. Refresh the analysis."
        )
    failed = store.load_attempt(terminal_digest)
    auth = store.load_authorization(failed.authorization_sha256)
    if pending is not None and (
        pending.node_id != node_id or pending.node_material_sha256 != nodes[0].material_sha256
        or pending.retry_ordinal != len(attempts) or pending.was_invalidated_at_admission
        or pending.input_artifact_set_sha256 != auth.input_artifact_set_sha256
        or pending.interaction_response_sha256 is not None
        or pending.interaction_predecessor_terminal_record_sha256 is not None
        or pending.failed_retry is not None
    ):
        raise ValueError("The pending spectral-reference retry differs from its recorded failure.")
    predecessor = auth.interaction_predecessor_terminal_record_sha256
    # A calculation retry carries no second response to the accepted final
    # review. If it fails again, retain the original review's predecessor.
    for attempt_id in reversed(attempts[:-1]):
        if predecessor is not None:
            break
        earlier = store.load_attempt(head.terminal_attempt_sha256_by_attempt_id[attempt_id])
        earlier_auth = store.load_authorization(earlier.authorization_sha256)
        if (earlier.node_id != node_id or earlier.stage_result.status is not StageStatus.FAILED
                or earlier.output_artifacts
                or earlier_auth.input_artifact_set_sha256 != auth.input_artifact_set_sha256):
            raise ValueError("The failed calculation no longer follows its accepted spectral review.")
        predecessor = earlier_auth.interaction_predecessor_terminal_record_sha256
    if predecessor is None:
        raise ValueError("This failure has no preceding spectral-reference form to reopen.")
    prior = store.load_attempt(predecessor)
    if prior.node_id != node_id or failed.node_id != node_id or failed.output_artifacts:
        raise ValueError("The failed step does not match the saved reference form.")
    expected = set(failed.execution_context_binding.input_artifact_ref_sha256s)
    inputs = tuple(a for a in snapshot.accepted_artifacts if a.artifact_ref_sha256 in expected)
    if len(inputs) != len(expected):
        raise ValueError("The saved image inspection or channel review is unavailable.")
    inspections = tuple(a for a in inputs if a.artifact_kind == "source_inspection")
    reviews = tuple(a for a in inputs if a.artifact_kind == "stage1_review")
    samples = tuple(s for s in snapshot.manifest.samples if s.sample_id in nodes[0].participant_ids)
    if len(inspections) != 1 or len(reviews) != 1 or len(samples) != 1:
        raise ValueError("The saved spectral-reference inputs are incomplete.")
    sample = _resolved_sample_mapping(samples[0], "reference_unmixing")
    authority, ome, replacement = _load_reviewed_source_pair(inspections[0], reviews[0], sample)
    from .batch_reference_unmixing_execution import _reference_revision_node_id
    run_id, root, request_path, output = _reference_paths(sample, inspections[0], reviews[0],
        revision_node_id=_reference_revision_node_id(nodes[0].to_mapping()))
    request, _payload = _load_reference_request(request_path)
    _require_current_source_request(
        request,
        run_id=run_id,
        authority=authority,
        reviewed_ome=ome,
        replacement=replacement,
        output_path=output,
    )
    from .batch_preprocessing_support import unmixing_source_state
    observed, _source = unmixing_source_state(ome)
    isolated = request.spectral_state_override == "isolated"
    if isolated:
        from mif_pipeline.io.metadata.measurement_band_copy import prepare_measurement_band_copy
        prepare_measurement_band_copy(ome, request.measurement_band_components)
    confirmed_copy = isolated or (observed != "raw" and (
        observed == "unmixed" or request.spectral_state_override == "unmixed"))
    old_form = (prior.stage_result.resume_context or {}).get("service_context", {})
    initial_component_copy = (
        confirmed_copy
        and old_form.get("operation_status") == "unstarted"
        and prior.stage_result.status is StageStatus.NEEDS_INTERACTION
        and prior.stage_result.stage == failed.stage_result.stage
        and prior.stage_result.slide_id == failed.stage_result.slide_id
    )
    if initial_component_copy and not output.exists() and not output.is_symlink():
        from mif_pipeline.preprocessing.unmixing.workflow import _provenance_path
        # The ordinary atomic image publisher owns temporary shards. Reopening
        # the same request is safe before any final image, provenance or copy
        # receipt was published; existing/uncertain publications stay closed.
        for path in (output, _provenance_path(output), root / "confirmed-unmixed-copy.json"):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("Check access to the saved component-image output before retrying.") from exc
            raise ValueError("A component-image publication already exists. Open Details before retrying.")
        return sample["sample_id"], "form"
    # A component-image writer can complete before an older receiver attempts
    # to read its directory as a single file. Reopen that same accepted request
    # through the existing completed-generation reader before offering a retry.
    if failed.stage_result.error_code in {"preprocessing_isadirectoryerror", "preprocessing_unmixingworkflowerror"}:
        from mif_pipeline.preprocessing.unmixing.output_publication import (
            load_unmixing_output_publication,
        )
        from mif_pipeline.preprocessing.unmixing.workflow import _provenance_path

        if (
            not confirmed_copy
            or old_form.get("operation_status") != "unstarted"
            or prior.stage_result.status is not StageStatus.NEEDS_INTERACTION
            or prior.stage_result.stage != "reference_unmixing"
            or prior.stage_result.slide_id != failed.stage_result.slide_id
        ):
            raise ValueError("Select the saved completed component-image request to retry.")
        load_unmixing_output_publication(
            image_path=output, provenance_path=_provenance_path(output)
        )
        return sample["sample_id"], "form"
    current = ReferenceUnmixingOperation.open_existing(root).status(run_id)
    if not isinstance(current, ReferenceUnmixingState):
        raise ValueError("The unmixing output has already been completed.")
    if current.status.value == "ready_for_unmixing":
        previous = old_form.get("operation_state", {})
        if (failed.stage_result.status is not StageStatus.FAILED
                or failed.stage_result.error_code not in INPUT_ERRORS
                or failed.stage_result.stage != "reference_unmixing"
                or prior.stage_result.status is not StageStatus.NEEDS_INTERACTION
                or prior.stage_result.stage != failed.stage_result.stage
                or prior.stage_result.slide_id != failed.stage_result.slide_id
                or previous.get("checkpoint") != "final_run_basis"
                or previous.get("status") != "awaiting_operation"
                or previous.get("run_id") != current.run_id
                or previous.get("target_id") != current.target_id
                or previous.get("start_authority_id") != current.start_authority_id
                or current.configuration_id != request.config_sha256):
            raise ValueError("The failed calculation differs from its accepted spectral basis.")
        from mif_pipeline.preprocessing.unmixing.output_publication import authority_pointer_path
        from mif_pipeline.preprocessing.unmixing.workflow import _provenance_path

        for path in (output, _provenance_path(output), authority_pointer_path(output)):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("Check access to the unmixing output before retrying.") from exc
            raise ValueError("An unmixing publication already exists. Open Details before retrying.")
        return sample["sample_id"], "calculation"
    if (failed.stage_result.error_code in {"preprocessing_attributeerror", "preprocessing_keyerror"}
            and old_form.get("interaction_kind") == "configure_reference_unmixing"
            and old_form.get("operation_status") == "unstarted"):
        # Initial view construction can fail after the operation saves its first
        # checkpoint. Reopen that untouched checkpoint, retaining its settings.
        if (old_form.get("interaction_kind") != "configure_reference_unmixing"
                or old_form.get("operation_status") != "unstarted"
                or prior.stage_result.status is not StageStatus.NEEDS_INTERACTION
                or prior.stage_result.stage != failed.stage_result.stage
                or prior.stage_result.slide_id != failed.stage_result.slide_id
                or current.status.value != "awaiting_operation"
                or current.checkpoint != "basis_source"
                or current.checkpoint_revision != 0
                or current.run_id != run_id
                or output.exists() or output.is_symlink()):
            raise ValueError("The initial spectral-reference checkpoint has changed.")
        return sample["sample_id"], "form"
    if failed.stage_result.error_code == "reference_unmixing_aborted":
        previous = old_form.get("operation_state", {})
        if (current.status.value not in {"aborted", "awaiting_operation"}
                or output.exists() or output.is_symlink()
                or previous.get("run_id") != current.run_id
                or previous.get("target_id") != current.target_id):
            raise ValueError("The stopped spectral review differs from its saved request.")
        ReferenceUnmixingOperation.open_existing(root).reopen_stopped_review(
            run_id, failed.stage_result.error_context["checkpoint_token"],
            previous.get("checkpoint_token"), publish=False)
        return sample["sample_id"], "form"
    validate_unchanged_reference_form(
        failed.stage_result, prior.stage_result, current.to_mapping(), output
    )
    return sample["sample_id"], "form"



def revalidate_pending_reference_retry(store, lease, context) -> None:
    """Resume an interrupted form publication with its exact pending attempt."""
    store.validate_writer_lease(lease)
    current = store.authenticate_current_execution_context(context)
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    attempts = snapshot.head.attempt_ids_by_node.get(context.node_id, ())
    terminal = (None if not attempts else
                snapshot.head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]))
    reopen_reference_input_failure(store, snapshot, context.node_id, terminal, context.authorization)


def commit_reference_form_recovery(store, snapshot, request, terminal_digest, lease):
    """Reissue the verified form as a new attempt without running a compute stage.

    Form correction does not require unrelated classifier or registration tools.
    Their ordinary prerequisites remain enforced when processing is resumed.
    """
    from dataclasses import replace
    from .batch_artifacts import BatchNodeExecutionContext, BatchNodeExecutionResult
    from .batch_state import BatchRawNodeOutcome

    _sample_id, recovery = reopen_reference_input_failure(
        store, snapshot, request.node.node_id, terminal_digest)
    if recovery != "form":
        raise ValueError("Retry the calculation using its already accepted spectral basis.")
    failed = store.load_attempt(terminal_digest)
    old_authorization = store.load_authorization(failed.authorization_sha256)
    prior = store.load_attempt(old_authorization.interaction_predecessor_terminal_record_sha256)
    authorization = store.authorize_attempt(request, snapshot.head.sha256, lease)
    binding = authorization.dispatch_binding
    context = BatchNodeExecutionContext(
        authorization=authorization,
        node_id=request.node.node_id,
        adapter_spec=request.adapter_spec,
        inputs=request.input_artifacts,
        **{
            key: getattr(binding, key)
            for key in (
                "projection_target_sha256",
                "current_image_identity_sha256",
                "script_sha256",
                "project_identity_sha256",
                "project_authority_head_sha256",
                "project_qpproj_snapshot_sha256",
                "project_entry_projection_sha256",
                "compatibility_signature_sha256",
            )
        },
    )
    result = replace(
        prior.stage_result,
        message="Review the spectral references and save your choice. Completed steps have been retained.",
    )
    if failed.stage_result.error_code == "reference_unmixing_aborted":
        from .reference_unmixing import ReferenceUnmixingOperation

        resume = dict(result.resume_context)
        service = dict(resume["service_context"])
        old = service["operation_state"]
        current = ReferenceUnmixingOperation.open_existing(
            Path(service["operation_root_locator"])).reopen_stopped_review(
                old["run_id"], failed.stage_result.error_context["checkpoint_token"],
                old["checkpoint_token"], publish=True)
        service.update(operation_state=current.to_mapping(), operation_status=current.status.value)
        resume["service_context"] = service
        result = replace(result, resume_context=resume, checkpoint_id=current.checkpoint_token)
    elif (failed.stage_result.error_code in {"preprocessing_attributeerror", "preprocessing_keyerror"}
            and result.resume_context["service_context"].get("interaction_kind")
            == "configure_reference_unmixing"):
        from hashlib import sha256
        from .reference_unmixing import ReferenceUnmixingOperation
        from .batch_reference_unmixing_execution import (
            _load_reference_request, reference_unmixing_state_to_stage_result,
        )

        service = result.resume_context["service_context"]
        root = Path(service["operation_root_locator"])
        request_path = Path(service["request_locator"])
        owner_request, payload = _load_reference_request(request_path)
        current = ReferenceUnmixingOperation.open_existing(root).status(owner_request.run_id)
        result = reference_unmixing_state_to_stage_result(
            current, slide_id=result.slide_id,
            inspection_artifact_ref_sha256=service["source_inspection_artifact_ref_sha256"],
            inspection_compatibility_signature_sha256=service["source_inspection_compatibility_signature_sha256"],
            review_artifact_ref_sha256=service["stage1_review_artifact_ref_sha256"],
            review_compatibility_signature_sha256=service["stage1_review_compatibility_signature_sha256"],
            operation_root=root, request_path=request_path,
            request_sha256=sha256(payload).hexdigest(), request_byte_count=len(payload),
            candidate_library=next((item for item in request.input_artifacts
                if item.artifact_kind == "spectral_reference_library"), None),
        )
    outcome = BatchRawNodeOutcome(
        context=context,
        stage_result=result,
        execution_result=BatchNodeExecutionResult(authorization.authorization_sha256, ()),
        execution_exchange_sha256=None,
    )
    return store.terminal_committer((context,), lease).commit_raw((outcome,))
