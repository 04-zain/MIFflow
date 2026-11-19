"""Read the selected saved tissue geometry for display, without image or native work."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from mif_pipeline.qupath_runtime.exchange import parse_stage_result_exchange_bytes
from mif_pipeline.qupath_runtime.project_stage import ProjectStageMutationEvidenceRecord
from mif_pipeline.tissue_detection.generations import TissueSectionGeneration
from .project_stage_plan import (
    PreparedProjectStageRunDescriptor, ProjectStageRecordError, ProjectStageRunPlan,
    _read_retained_project_stage_process_records,
)


def tissue_region_review(store, target):
    """Read the selected tissue generation for the linked map and table."""
    terminal = store.load_attempt(target.needs_interaction_terminal_record_sha256)
    if (terminal.stage_result != target.stage_result
            or terminal.authorization_sha256 != target.attempt_authorization_sha256
            or terminal.attempt_id != target.attempt_id or terminal.node_id != target.node_id):
        raise ProjectStageRecordError("Tissue review differs from its saved native attempt")
    request, parsed = read_native_geometry_exchange(store, terminal, project_entry_id=target.project_entry_id)
    if replace(parsed.result, stage=target.stage_result.stage, outputs=None) != target.stage_result:
        raise ProjectStageRecordError("Tissue review native result differs from its saved review")
    selected = [item for item in parsed.generation_manifest["generations"]
                if item["generation_id"] == target.stage_result.generation_id]
    if len(selected) != 1:
        raise ProjectStageRecordError("Tissue review current generation is missing or ambiguous")
    generation = TissueSectionGeneration.from_mapping(selected[0])
    context = target.stage_result.resume_context
    if (generation.slide_id != target.slide_id
            or generation.tissue_resolution_selection.to_mapping() != context["tissue_resolution_selection"]):
        raise ProjectStageRecordError("Tissue review geometry calibration differs")
    changed = set(context.get("changed_object_ids", target.stage_result.affected_object_ids)) if request.get("action") == "edit" else set()
    from .region_review_geometry import project_region_review
    return project_region_review(generation, changed=changed)


def read_native_geometry_exchange(store, terminal, *, project_entry_id):
    """Read one recorded native geometry exchange; never open image pixels.

    The pending review or selected committed artifact supplies its original
    terminal. The existing process records resolve that terminal's exact bytes.
    """
    retained = store._authority_layout
    records = _read_retained_project_stage_process_records(
        retained=retained, workspace_storage_scope_sha256=store.workspace_storage_scope_sha256,
        runtime_evidence_ref=lambda digest: store._batch_project_ref(
            "batch_project_runtime_evidence", evidence_sha256=digest),
    )
    matches = [(record, observation) for _, record in records
        if isinstance(record, ProjectStageMutationEvidenceRecord)
        and record.project_identity_sha256 == terminal.execution_context_binding.project_identity_sha256
        and record.termination_reason == "normal_exit"
        for observation in record.target_observations
        if observation.node_id == terminal.node_id and observation.attempt_id == terminal.attempt_id
        and observation.attempt_authorization_sha256 == terminal.authorization_sha256
        and observation.project_entry_id == project_entry_id]
    if len(matches) != 1:
        raise ProjectStageRecordError("Tissue review native observation is missing or ambiguous")
    mutation, observation = matches[0]
    descriptors = [record for _, record in records if isinstance(record, PreparedProjectStageRunDescriptor)
        and record.active_mutation_run_sha256 == mutation.active_mutation_run_sha256
        and record.mutation_request_sha256 == mutation.mutation_request_sha256
        and record.run_plan_sha256 == mutation.run_plan_sha256]
    if len(descriptors) != 1:
        raise ProjectStageRecordError("Tissue review native plan is missing or ambiguous")

    def read(ref, digest=None, size=None):
        family = retained.layout.family(ref.family)
        bucket = retained.bucket(family.physical_bucket)
        bucket._member_family(ref)
        payload = bucket.read_immutable(ref.physical_name, max_bytes=family.max_member_bytes,
                                        label="tissue review native evidence")
        if (payload is None or (digest is not None and hashlib.sha256(payload).hexdigest() != digest)
                or (size is not None and len(payload) != size)):
            raise ProjectStageRecordError("Tissue review saved geometry or request changed")
        return payload

    plan = ProjectStageRunPlan.from_mapping(json.loads(read(descriptors[0].run_plan_ref)))
    if plan.plan_sha256 != mutation.run_plan_sha256:
        raise ProjectStageRecordError("Tissue review native plan differs")
    targets = [item for item in plan.targets if item.node_id == terminal.node_id
        and item.attempt_id == terminal.attempt_id
        and item.authorized_entry_sha256 == observation.authorized_entry_sha256
        and item.exchange.exchange_sha256 == terminal.execution_exchange_sha256]
    if len(targets) != 1:
        raise ProjectStageRecordError("Tissue review native exchange is missing or ambiguous")
    authorized = targets[0]
    exchange = authorized.exchange
    if (exchange.result_ref != observation.result_ref
            or exchange.generation_manifest_ref != observation.generation_manifest_ref):
        raise ProjectStageRecordError("Tissue review native members differ")
    request = json.loads(read(exchange.request_ref, exchange.request_envelope_sha256,
                             exchange.request_envelope_byte_count))["request"]
    result_payload = read(exchange.result_ref, observation.result_payload_sha256, observation.result_byte_count)
    manifest_payload = read(exchange.generation_manifest_ref,
        observation.generation_manifest_payload_sha256, observation.generation_manifest_byte_count)
    parsed = parse_stage_result_exchange_bytes(result_payload, manifest_payload,
        invocation_id=terminal.attempt_id, run_id=authorized.pipeline_run_id,
        stage=authorized.wire_stage_id, slide_id=terminal.stage_result.slide_id,
        generation_manifest_name=exchange.generation_manifest_ref.physical_name)
    return request, parsed

