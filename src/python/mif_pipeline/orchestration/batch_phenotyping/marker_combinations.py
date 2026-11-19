"""One accepted phenotype definition after every marker gate has been reviewed."""
from __future__ import annotations

import json
from collections.abc import Mapping
from mif_pipeline.pipeline_core import StageResult, StageStatus, InteractionReason, InteractionAction
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract, ArtifactOrigin, BatchArtifactRef, BatchNodeExecutionResult,
    validate_committed_artifact_authority_set,
)
from mif_pipeline.orchestration.batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from mif_pipeline.orchestration.batch_interactions import BatchTargetResponseValidation
from ._common import BatchPhenotypingError, canonical_bytes, digest
from .marker_settings import validate_marker_definition

UNMATCHED_PHENOTYPE = 'Unmatched cells'
NODE = 'marker_combination_selection'
SCHEMA = 'mif-batch-marker-combination-selection'
CONTEXT = 'marker-combinations/v1'
VALIDATOR = 'marker-combination-response-validator'
ACTION = InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value


def marker_combination_identity_contract():
    return ArtifactIdentityContract(
        contract_id=SCHEMA+'-identity/v1', artifact_kind='marker_combination',
        schema_name=SCHEMA, schema_version=1,
        required_fields=('canonical_marker_ids', 'definition', 'definition_sha256',
                         'marker_gate_ref_sha256', 'interaction_request_id',
                         'interaction_response_sha256'),
        optional_fields=(), required_runtime_names=('mif_pipeline',))


def validate_combination_context(result):
    context = result.resume_context
    if (result.stage != NODE or result.status is not StageStatus.NEEDS_INTERACTION
            or result.interaction_reason is not InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED
            or result.allowed_actions != (ACTION, InteractionAction.ABORT.value)
            or context.get('schema_version') != CONTEXT
            or set(context) != {'schema_version', 'marker_gate_ref_sha256', 'canonical_marker_ids', 'initial_definition'}):
        raise BatchPhenotypingError('This is not the current phenotype combination request.')
    markers = tuple(context['canonical_marker_ids'])
    if not markers or markers != tuple(sorted(set(markers))) or any(not isinstance(x,str) or not x.strip() for x in markers):
        raise BatchPhenotypingError('Phenotype combinations require the accepted marker inventory.')
    if context['initial_definition'] is not None:
        validate_marker_definition(context['initial_definition'], canonical_marker_ids=markers)
    return context


def validate_combination_response(result, response):
    context = validate_combination_context(result)
    if not isinstance(response, Mapping) or set(response) != {'phenotype_definition'}:
        raise BatchPhenotypingError('Provide the ordered phenotype definition.')
    return {'phenotype_definition': validate_marker_definition(response['phenotype_definition'],
            canonical_marker_ids=context['canonical_marker_ids'])}


def validate_combination_target_response(target, action, response, *, authorization_request):
    try:
        validate_combination_context(target.stage_result)
        if action == InteractionAction.ABORT.value and isinstance(response,Mapping) and not response:
            normalized = {}
        elif action == ACTION:
            normalized = validate_combination_response(target.stage_result,response)
            definition = normalized['phenotype_definition']
            if definition['residual_phenotype_id'] != UNMATCHED_PHENOTYPE or any(
                entry['display_name'] != UNMATCHED_PHENOTYPE
                for entry in definition['phenotype_classes'] if entry['class_id'] == UNMATCHED_PHENOTYPE
            ):
                raise BatchPhenotypingError('Use Unmatched cells for cells that match no rule.')
        else:
            raise BatchPhenotypingError('Choose an offered phenotype combination action.')
    except (TypeError, ValueError):
        return BatchTargetResponseValidation(target.target_id,target.node_id,'rejected',None,None,None,None,'marker_combination_response_invalid')
    return BatchTargetResponseValidation(target.target_id,target.node_id,'accepted',normalized,
        authorization_request,VALIDATOR,'1',None)


def decode_marker_combination(authority, gate):
    validate_committed_artifact_authority_set((authority,))
    ref = authority.artifact_ref
    contract = marker_combination_identity_contract()
    identity = ref.owning_identity
    if (authority.producer_node_kind != NODE or ref.identity_contract != contract
            or ref.artifact_kind != 'marker_combination' or ref.origin is not ArtifactOrigin.INTERNAL_NODE
            or ref.locator is not None or ref.scope != gate.artifact_ref.scope
            or ref.configuration_bindings != gate.artifact_ref.configuration_bindings
            or identity['marker_gate_ref_sha256'] != gate.artifact_ref.artifact_ref_sha256
            or tuple(identity['canonical_marker_ids']) != gate.canonical_marker_ids):
        raise BatchPhenotypingError('Phenotype combinations do not belong to these accepted marker gates.')
    definition = validate_marker_definition(json.loads(identity['definition']), canonical_marker_ids=gate.canonical_marker_ids)
    if canonical_bytes(definition).decode() != identity['definition'] or digest(definition) != identity['definition_sha256']:
        raise BatchPhenotypingError('The accepted phenotype definition has changed.')
    return definition


def execute_marker_combination_selection(context, *, state_store):
    """Use the normal interaction response and publish one gate-bound definition."""
    from .marker_application_execution import decode_committed_marker_gate
    current = state_store.authenticate_current_execution_context(context)
    if context.adapter_spec.node_kind != NODE or len(context.inputs) != 1:
        raise BatchPhenotypingError('Phenotype combination selection requires one accepted gate.')
    gate = decode_committed_marker_gate(state_store.authenticate_available_artifact(context.inputs[0]))
    if (gate.artifact_ref.scope.validation_domain_id != current.validation_domain_id
            or gate.artifact_ref.scope.analysis_domain_bindings != current.analysis_domain_bindings
            or gate.artifact_ref.configuration_bindings != current.configuration_bindings):
        raise BatchPhenotypingError('Phenotype combination selection differs from the accepted gate scope.')
    initial = None
    node = state_store._load_plan_snapshot(current.active_plan_sha256).node_by_id[context.node_id]
    if 'settled_review_revision' in node:
        revision = node['settled_review_revision']
        prior = state_store.load_attempt(revision['predecessor_terminal_record_sha256'])
        if prior.node_id != revision['predecessor_node_id']:
            raise BatchPhenotypingError('The previous phenotype selection belongs to another step.')
        refs = tuple(ref for ref in prior.output_artifacts if ref.artifact_kind == 'marker_combination')
        if refs:
            if len(refs)!=1 or refs[0].identity_contract != marker_combination_identity_contract():
                raise BatchPhenotypingError('The previous phenotype selection is ambiguous.')
            initial = validate_marker_definition(json.loads(refs[0].owning_identity['definition']), canonical_marker_ids=gate.canonical_marker_ids)
    resume = {'schema_version':CONTEXT, 'marker_gate_ref_sha256':gate.artifact_ref.artifact_ref_sha256,
              'canonical_marker_ids':gate.canonical_marker_ids, 'initial_definition':initial}
    request = StageResult.needs_interaction(stage=NODE,
        slide_id=str(gate.measurement_authority_bindings[0]['slide_id']),
        reason=InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED,
        affected_object_ids=tuple(sorted({b.analysis_domain_id for b in current.analysis_domain_bindings})),
        message='Define phenotypes from the reviewed marker states. The first matching rule assigns the cell label.',
        allowed_actions=(ACTION,InteractionAction.ABORT.value),resume_context=resume,
        checkpoint_id='marker-combinations-'+digest(resume)[:48])
    response = state_store.load_validated_interaction_response(context.authorization)
    if response is None:
        return request
    prior_request = state_store.load_interaction_request(response['interaction_request_id'])
    if (prior_request != request or response['node_id'] != context.node_id
            or response['response_validator_id'] != VALIDATOR or response['response_validator_version'] != '1'
            or response['prior_terminal_record_sha256'] != context.authorization.interaction_predecessor_terminal_record_sha256):
        raise BatchPhenotypingError('The phenotype response differs from the current accepted gates.')
    if response['action'] == InteractionAction.ABORT.value:
        return StageResult.failed(stage=NODE,slide_id=request.slide_id,error_code='marker_combinations_stopped',
            error_message='Phenotype combination review was stopped. Use Change options to reopen this step.')
    if response['action'] != ACTION:
        raise BatchPhenotypingError('The phenotype combination response action is invalid.')
    definition = validate_combination_response(request,response['response'])['phenotype_definition']
    identity = {'canonical_marker_ids':gate.canonical_marker_ids,
        'marker_gate_ref_sha256':gate.artifact_ref.artifact_ref_sha256,
        'definition':canonical_bytes(definition).decode(), 'definition_sha256':digest(definition),
        'interaction_request_id':response['interaction_request_id'],
        'interaction_response_sha256':response['response_sha256']}
    contract = marker_combination_identity_contract()
    artifact = BatchArtifactRef(artifact_id='marker-combinations-'+digest(identity)[:48],
        artifact_kind='marker_combination',schema_name=SCHEMA,schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,external_validation_receipt_sha256=None,
        locator=None,sha256=None,byte_count=None,scope=gate.artifact_ref.scope,
        owning_identity=identity,identity_contract=contract,owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=SCHEMA+'/v1',runtime_versions={'mif_pipeline':MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256,(artifact,))
