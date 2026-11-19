"""Retry recorded source-access or runtime failures before inspection publication.

Saved attempt records retain the selected source, configuration and retry number.
We establish the narrower recovery condition both when offering a retry and
before dispatching its pending authorization. Existing or uncertain output is
never overwritten by this route.
"""
from __future__ import annotations

from pathlib import Path

from mif_pipeline.pipeline_core import StageStatus

_SOURCE_ACCESS_ERRORS = frozenset({"preprocessing_filenotfounderror", "preprocessing_permissionerror",
    "preprocessing_dependencyversionerror", "preprocessing_sourceopenerror", "preprocessing_pixelreaderror"})


def _inspection_failure(store, snapshot, node_id, terminal_digest, pending=None):
    """Reopen a correctable inspection failure with current inputs and absent output."""
    from .batch_preprocessing_support import _resolved_sample_mapping
    from .batch_state import BatchExecutionState

    head = snapshot.head
    nodes = tuple(node for node in snapshot.plan.nodes if node.node_id == node_id)
    attempts = head.attempt_ids_by_node.get(node_id, ())
    pending_digest = head.pending_authorization_sha256_by_node.get(node_id)
    if (
        head.execution_state not in {BatchExecutionState.ACTIVE, BatchExecutionState.PAUSED}
        or (pending is not None and head.execution_state is not BatchExecutionState.ACTIVE)
        or len(nodes) != 1 or nodes[0].kind != "inspection" or not attempts
        or node_id in head.invalidated_node_ids
        or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
        or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != terminal_digest
        or pending_digest != (None if pending is None else pending.authorization_sha256)
    ):
        raise ValueError("This inspection failure is no longer current. Refresh the analysis.")
    node = nodes[0]
    failed = store.load_attempt(terminal_digest)
    previous = store.load_authorization(failed.authorization_sha256)
    samples = tuple(item for item in snapshot.manifest.enabled_samples
                    if (item.sample_id,) == node.participant_ids)
    if len(samples) != 1:
        raise ValueError("The failed inspection has no exact current source selection.")
    sample = _resolved_sample_mapping(samples[0], "inspection")
    result = failed.stage_result
    if (
        failed.node_id != node_id or failed.attempt_id != attempts[-1]
        or previous.node_id != node_id or previous.node_material_sha256 != node.material_sha256
        or previous.retry_ordinal + 1 != len(attempts)
        or failed.execution_context_binding.adapter_spec_sha256 != node.adapter_spec_sha256
        or previous.dispatch_binding.participant_ids != node.participant_ids
        or node.input_artifact_ids or failed.execution_context_binding.input_artifact_ref_sha256s
        or failed.output_artifacts
        or any(item.producer_node_id == node_id for item in snapshot.accepted_artifacts)
        or result.stage != "inspection" or result.status is not StageStatus.FAILED
        or result.slide_id != sample["slide_id"]
        or result.error_code not in _SOURCE_ACCESS_ERRORS
    ):
        raise ValueError("Retry requires an unchanged source or runtime failure before inspection publication.")
    if pending is not None and (
        pending.node_id != node_id or pending.node_material_sha256 != node.material_sha256
        or pending.retry_ordinal != len(attempts) or pending.was_invalidated_at_admission
        or pending.input_artifact_set_sha256 != previous.input_artifact_set_sha256
        or pending.interaction_response_sha256 is not None
        or pending.interaction_predecessor_terminal_record_sha256 is not None
        or pending.failed_retry is not None
    ):
        raise ValueError("The pending inspection retry differs from its recorded failure.")
    # Do not resolve the final component: even a broken symlink is an existing
    # publication conflict. Permission errors also refuse admission.
    relative = (f"preprocessing/inspection/revisions/{node_id}/source-inspection.json"
        if node.settled_review_revision is not None else "preprocessing/inspection/source-inspection.json")
    output = Path(sample["sample_output_root"]) / relative
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError("The inspection output location cannot be checked before retrying.") from exc
    else:
        raise ValueError("An inspection output already exists. Review the retained result before retrying.")
    return sample["sample_id"]


def reopen_inspection_failure(store, snapshot, node_id: str, terminal_digest: str) -> str:
    """Return the selected sample only for a current unadmitted inspection failure."""
    return _inspection_failure(store, snapshot, node_id, terminal_digest)


def authorize_inspection_retry(store, snapshot, request, terminal_digest, lease):
    """Validate the failure, then issue a fresh ordinary attempt under the writer lease."""
    reopen_inspection_failure(store, snapshot, request.node.node_id, terminal_digest)
    return store.authorize_attempt(request, snapshot.head.sha256, lease)


def revalidate_pending_inspection_retry(store, lease, context) -> None:
    """Refuse stale or partially published retries immediately before normal dispatch."""
    store.validate_writer_lease(lease)
    current = store.authenticate_current_execution_context(context)
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    attempts = snapshot.head.attempt_ids_by_node.get(context.node_id, ())
    terminal = (None if not attempts else
                snapshot.head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]))
    _inspection_failure(store, snapshot, context.node_id, terminal, context.authorization)


def load_inspection_retry_response(store, context):
    """Retain an authenticated cycle decision across unchanged access failures.

    Ordinary first attempts and first-time access failures have no decision.
    Repeated retries traverse only this node's failed attempts; the original
    interaction publication remains the sole authority for the accepted choice.
    """
    import json
    from .batch_acquisition_organization import (load_pending_inspection,
        organization_stage_result, validate_organization_response, RESPONSE_VERSION,
        previous_inspection_artifact, organization_seed)
    from .batch_preprocessing_support import _resolved_sample_mapping
    from .batch_preprocessing_wire import canonical_bytes

    if context.adapter_spec.node_kind != "inspection":
        raise ValueError("Inspection response recovery requires its exact stage")
    authorization = context.authorization
    if authorization.retry_ordinal == 0:
        return None
    current = store.authenticate_current_execution_context(context)
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    attempts = snapshot.head.attempt_ids_by_node.get(context.node_id, ())
    terminal = None if not attempts else snapshot.head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
    sample_id = _inspection_failure(store, snapshot, context.node_id, terminal, authorization)
    previous = authorization
    for attempt_id in reversed(attempts):
        failed = store.load_attempt(snapshot.head.terminal_attempt_sha256_by_attempt_id[attempt_id])
        original = store.load_authorization(failed.authorization_sha256)
        if (failed.node_id != context.node_id or failed.attempt_id != attempt_id
                or failed.stage_result.stage != "inspection" or failed.stage_result.status is not StageStatus.FAILED
                or failed.stage_result.error_code not in _SOURCE_ACCESS_ERRORS or failed.output_artifacts
                or original.node_id != context.node_id or original.attempt_id != attempt_id
                or original.node_material_sha256 != previous.node_material_sha256
                or original.input_artifact_set_sha256 != previous.input_artifact_set_sha256
                or original.retry_ordinal + 1 != previous.retry_ordinal):
            raise ValueError("Inspection retry no longer follows its unchanged source-access failure")
        accepted = store.load_validated_interaction_response(original)
        if accepted is not None:
            if (accepted.get("action") != "accept"
                    or accepted.get("response_validator_id") != "batch-acquisition-organization-response"
                    or accepted.get("response_validator_version") != RESPONSE_VERSION):
                raise ValueError("Inspection retry has a foreign accepted decision")
            selected = next(item for item in snapshot.manifest.enabled_samples if item.sample_id == sample_id)
            sample = _resolved_sample_mapping(selected, "inspection")
            authority, digest = load_pending_inspection(sample, context.node_id)
            node = next(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
            previous_ref = previous_inspection_artifact(store, node, snapshot.head)
            from .batch_stage1_execution import _load_authority, _validate_source_selection
            prior = None if previous_ref is None else _load_authority(previous_ref)
            if prior is not None:
                _validate_source_selection(prior, sample)
            prompt = organization_stage_result(authority, slide_id=sample["slide_id"],
                node_id=context.node_id, pending_sha256=digest, seed=organization_seed(authority, prior))
            validate_organization_response(authority, prompt, json.loads(canonical_bytes(accepted["response"])))
            return accepted
        previous = original
    return None
