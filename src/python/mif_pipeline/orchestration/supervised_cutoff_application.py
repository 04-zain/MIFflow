"""Apply an explicit assignment cutoff through existing plan and response owners."""
from collections.abc import Mapping
import getpass

from mif_pipeline.pipeline_core import StageStatus
from .supervised_cutoff_selection import SupervisedCutoffSelection, merge_selection, selected_cutoff


def _latest_terminal(snapshot, node, store):
    attempts = snapshot.head.attempt_ids_by_node.get(node.node_id, ())
    if not attempts:
        raise ValueError('The selected prediction has no retained terminal attempt.')
    digest = snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]]
    terminal = store.load_attempt(digest)
    if terminal.node_id != node.node_id:
        raise ValueError('The prediction predecessor belongs to a different task.')
    return digest, terminal


def assignment_owners(snapshot, dependencies):
    """Inventory every current prediction, including idle pending sections."""
    store = dependencies.runtime.state_store
    refs = {a.artifact_ref.producer_node_id:a.artifact_ref for a in snapshot.committed_artifact_authorities
            if a.artifact_ref.artifact_kind == 'supervised_phenotype_result'}
    measurements = {a.artifact_ref.artifact_ref_sha256:a.artifact_ref for a in snapshot.committed_artifact_authorities
                    if a.artifact_ref.artifact_kind == 'measurements_result'}
    groups = {sample.sample_id:sample.validation_domain_id for sample in snapshot.manifest.enabled_samples}
    owners=[]
    for node in snapshot.plan.nodes:
        status = snapshot.head.node_status_by_id.get(node.node_id)
        if node.kind != 'supervised_phenotype_inference' or status not in {StageStatus.COMPLETED, StageStatus.NEEDS_INTERACTION}:
            continue
        terminal_sha, terminal = _latest_terminal(snapshot, node, store)
        if node.node_id in snapshot.head.pending_authorization_sha256_by_node:
            raise ValueError('Wait for the selected prediction task to finish.')
        if status is StageStatus.COMPLETED:
            ref=refs.get(node.node_id)
            if ref is None:
                raise ValueError('The completed prediction has no current accepted result.')
            identity=ref.owning_identity
            sha,size=identity['prediction_sha256'],identity['prediction_byte_count']
            cutoff=identity['confidence_threshold']
        else:
            context=terminal.stage_result.resume_context
            if (terminal.stage_result.interaction_reason != 'supervised_classifier_review_required'
                    or not isinstance(context, Mapping) or context.get('phase') != 'prediction_review'):
                raise ValueError('The selected task is not an idle prediction review.')
            sha,size=context['sha256'],context['byte_count'];cutoff=None
        prediction=dependencies.phenotyping.supervised_prediction_store.reopen(sha,size).prediction
        measurement=measurements.get(prediction.measurement_result_ref_sha256)
        image=(prediction.sample_id,prediction.project_entry_id,prediction.slide_id)
        if (measurement is None or node.participant_ids != (prediction.sample_id,)
                or node.project_entry_ids != (prediction.project_entry_id,)
                or node.validation_domain_id != groups[prediction.sample_id]
                or node.section_target is None or any(getattr(node.section_target,key) != getattr(prediction,key)
                    for key in ('section_id','project_entry_id','tissue_generation_id'))
                or any(measurement.owning_identity[key] != getattr(prediction,key)
                       for key in ('sample_id','project_entry_id','slide_id','section_id'))):
            raise ValueError('The prediction does not match its current image and measurement owner.')
        if status is StageStatus.COMPLETED and (identity['classifier_record_sha256'] != prediction.classifier_record_sha256
                or any(identity[key] != getattr(prediction,key) for key in ('sample_id','project_entry_id','slide_id','section_id'))):
            raise ValueError('The accepted assignment differs from its retained prediction.')
        owners.append(dict(node=node,terminal_sha256=terminal_sha,prediction_sha256=sha,byte_count=size,
            image_identity=image,section_id=prediction.section_id,processing_group_id=groups[prediction.sample_id],
            classifier_record_sha256=prediction.classifier_record_sha256,cutoff=cutoff,class_ids=prediction.class_ids))
    return tuple(owners)


def resolve_selection(owners, raw, *, reviewer_id):
    """Resolve the scope-specific UI intent against exact current prediction owners."""
    if not isinstance(raw, Mapping) or raw.get('scope') not in {'image','group'}:
        raise ValueError('Assign an explicit cutoff to the selected image or processing group.')
    required = {'scope','cutoff','image_identity' if raw['scope']=='image' else 'processing_group_id'}
    if set(raw) not in (required, {'scope','cutoff','image_identity','processing_group_id'}):
        raise ValueError('Assign an explicit cutoff to the selected image or processing group.')
    image = raw.get('image_identity')
    if image is not None:
        if not isinstance(image,(tuple,list)) or len(image)!=3 or any(not isinstance(v,str) or not v for v in image):
            raise ValueError('Select the exact image whose assignments are being reviewed.')
        image = tuple(image)
        anchor = tuple(owner for owner in owners if owner['image_identity']==image)
        groups = {owner['processing_group_id'] for owner in anchor}
        if len(groups)!=1 or raw.get('processing_group_id',next(iter(groups))) not in groups:
            raise ValueError('The selected image is not in this processing group.')
        group = next(iter(groups))
    elif raw['scope']=='image':
        raise ValueError('Select the exact image whose assignments are being reviewed.')
    else:
        group = raw['processing_group_id']
    chosen = tuple(owner for owner in owners if owner['processing_group_id']==group
                   and (raw['scope']=='group' or owner['image_identity']==image))
    if not chosen:
        raise ValueError('The selected processing group has no current predictions.')
    models = {owner['classifier_record_sha256'] for owner in chosen}
    if len(models)!=1:
        raise ValueError('This selection uses more than one fitted classifier. Assign each compatible image separately.')
    selection = SupervisedCutoffSelection(raw['scope'],group,
        image if raw['scope']=='image' else None,next(iter(models)),raw['cutoff'],reviewer_id)
    return selection,chosen


def _validate_model(snapshot, dependencies, selection):
    from .batch_training.record_publication import decode_current_committed_classifier_record_set
    records=[]
    for authority in snapshot.committed_artifact_authorities:
        if authority.artifact_ref.artifact_kind=='classifier_record_set':
            saved=decode_current_committed_classifier_record_set(authority,
                store=dependencies.phenotyping.classifier_record_set_store,state_store=dependencies.runtime.state_store)
            records.extend(r for r in saved.classifier_records if r.classifier_record_sha256==selection.classifier_record_sha256)
    if not records:
        raise ValueError('The selected fitted classifier is no longer current.')
    record=records[0]
    if record.classifier_engine=='qupath-object-svm-classifier' and len(record.owner_payload.class_ids)==2 and selection.cutoff!=0:
        raise ValueError('Binary SVM vote agreement supports assignment at cutoff zero only.')


def assign_cutoff(service, batch_id, *, interaction_request_id=None, artifact_ref_sha256=None, expected_state_head_sha256, selection):
    from .batch_service import BatchServiceError, build_batch_stage_registry, expand_batch_plan, bind_batch_prerequisites
    from .batch_interactions import project_batch_interaction_target
    from .batch_settled_review_revision import SettledReviewRevision
    from .batch_saved_result_retirement import SavedResultRetirement, retirement_kinds
    from .batch_stage_settings import stage_dependants
    from .batch_state import BatchExecutionState
    store=service._dependencies.runtime.state_store
    with store.acquire_writer_lease('supervised-assignment-cutoff') as lease:
        snapshot=store.reopen_execution_snapshot(expected_head_sha256=expected_state_head_sha256)
        with service._lock:
            if service._active_wave is not None:
                raise BatchServiceError('Wait for processing to finish, or pause before assigning these cells.')
        if bool(interaction_request_id) == bool(artifact_ref_sha256):
            raise BatchServiceError('Choose one current assignment review or saved assignment result.')
        if artifact_ref_sha256:
            from .batch_phenotyping.supervised_cohort_review import saved_prediction_context
            saved_prediction_context(snapshot, service._dependencies, artifact_ref_sha256)
        else:
            if interaction_request_id not in snapshot.head.pending_interaction_ids:
                raise BatchServiceError('The assignment review is no longer pending.')
            publication=store.reopen_interaction_request_publication(interaction_request_id,expected_head_sha256=snapshot.head.sha256)
            target=project_batch_interaction_target(publication)
            if (target.stage_result.interaction_reason!='supervised_classifier_review_required'
                    or not isinstance(target.stage_result.resume_context, Mapping)
                    or target.stage_result.resume_context.get('phase')!='prediction_review'):
                raise BatchServiceError('Choose the current supervised assignment review.')
        if service._provider._prerequisite_inputs is None:
            raise BatchServiceError('The reviewed runtime and input inventory are unavailable.')
        owners=assignment_owners(snapshot,service._dependencies)
        chosen,targets=resolve_selection(owners,selection,reviewer_id='local-account:'+getpass.getuser())
        _validate_model(snapshot,service._dependencies,chosen)
        selections=merge_selection(snapshot.plan.supervised_cutoff_selections,chosen)
        if selections==snapshot.plan.supervised_cutoff_selections:
            return service._status_from_snapshot(snapshot)
        revisions=[];affected=set();retirements=[]
        for owner in targets:
            node=owner['node'];prior=node.settled_review_revision
            revisions.append(SettledReviewRevision(node.node_id,1 if prior is None else prior.revision+1,owner['terminal_sha256']))
            closure=set(stage_dependants(snapshot.plan,node.node_id));affected.update(closure)
            sources=tuple(sorted((a.artifact_ref for a in snapshot.committed_artifact_authorities
                if a.artifact_ref.producer_node_id in closure and a.producer_node_kind in retirement_kinds(node.kind)),
                key=lambda ref:ref.artifact_ref_sha256))
            if sources:retirements.append(SavedResultRetirement(node.node_id,snapshot.plan.plan_sha256,sources))
        if affected.intersection(snapshot.head.pending_authorization_sha256_by_node):
            raise BatchServiceError('Finish retained work before changing these assignments.')
        retained=tuple(ref for ref in snapshot.accepted_artifacts if ref.producer_node_id not in affected)
        retained_ids={ref.artifact_ref_sha256 for ref in retained}
        registry=build_batch_stage_registry(plan=snapshot.plan,resolved_samples=snapshot.manifest.enabled_samples,
            dependencies=service._dependencies)
        successor=expand_batch_plan(snapshot.plan,snapshot.manifest,registry.stage_registry,retained,
            accepted_artifact_authorities=tuple(a for a in snapshot.committed_artifact_authorities if a.artifact_ref.artifact_ref_sha256 in retained_ids),
            batch_state_store=store,training_plan_set_store=service._dependencies.training.training_plan_set_store,
            settled_review_revisions=tuple(revisions),supervised_cutoff_selections=selections,saved_result_retirements=tuple(retirements))
        preserved={node.node_id for node in snapshot.plan.nodes if node.node_id not in affected
                   and snapshot.head.node_status_by_id.get(node.node_id) is StageStatus.COMPLETED}
        required=frozenset({node.kind for node in successor.nodes if node.node_id not in preserved}
            |{node.node_kind for node in successor.deferred_nodes})
        prerequisites=service._collect_prerequisites(snapshot.manifest,successor,required_node_kinds=required) if required else ()
        successor=bind_batch_prerequisites(successor,prerequisites);service._admit_prerequisites(prerequisites)
        if not service._dependencies.runtime.wait_for_project_stage_quiescence():
            raise BatchServiceError('Wait for the current image task to exit before assigning cells.')
        with store.current_project_control(operation='revise-options',lease=lease) as control:
            control.project_processes.assert_all_drained(batch_id=batch_id,project_identity_sha256=snapshot.head.project_identity_sha256)
            if snapshot.head.execution_state is BatchExecutionState.PAUSED:
                from .batch_pause import consume_quiescent_pause
                consume_quiescent_pause(state_store=store,batch_id=batch_id,pause_record_sha256=snapshot.head.quiescent_pause_sha256,
                    scheduler=service._new_scheduler(snapshot,lease,registry),workspace_lease=lease,
                    project_lease=control.project_lease,project_processes=control.project_processes)
            head=store.load_head()
            store.revise_plan(successor,'accepted supervised '+chosen.scope+' assignment cutoff',head.sha256,lease)
        with service._lock:service._pause_blocks_admission=False
        return service._status_from_snapshot(store.reopen_execution_snapshot())


def apply_saved_cutoffs(service, snapshot, lease):
    """Use an explicit saved choice for a later ordinary prediction review."""
    if not snapshot.plan.supervised_cutoff_selections:
        return snapshot
    from .batch_interactions import (project_batch_interaction_target,group_interactions,BatchInteractionResponseRequest,
        BatchTargetSelection,apply_group_response,BatchInteractionError)
    from .batch_state import BatchStateError
    store=service._dependencies.runtime.state_store
    samples={sample.sample_id:sample for sample in snapshot.manifest.enabled_samples}
    for request_id in snapshot.head.pending_interaction_ids:
        try:
            target=project_batch_interaction_target(store.reopen_interaction_request_publication(request_id,expected_head_sha256=snapshot.head.sha256))
        except (BatchInteractionError, BatchStateError):
            continue
        context=target.stage_result.resume_context
        if target.stage_result.interaction_reason!='supervised_classifier_review_required' or not isinstance(context,Mapping) or context.get('phase')!='prediction_review':
            continue
        prediction=service._dependencies.phenotyping.supervised_prediction_store.reopen(context['sha256'],context['byte_count']).prediction
        node = next(item for item in snapshot.plan.nodes if item.node_id == target.node_id)
        if (node.kind != 'supervised_phenotype_inference'
                or (prediction.sample_id,prediction.project_entry_id,prediction.slide_id)
                != (target.sample_id,target.project_entry_id,target.slide_id)
                or node.section_target is None or node.section_target.section_id != prediction.section_id
                or not any(a.artifact_ref.artifact_ref_sha256 == prediction.measurement_result_ref_sha256
                    and a.artifact_ref.artifact_kind == 'measurements_result'
                    for a in snapshot.committed_artifact_authorities)):
            raise ValueError('The saved cutoff does not match the current prediction owner.')
        image=(prediction.sample_id,prediction.project_entry_id,prediction.slide_id)
        selection=selected_cutoff(snapshot.plan.supervised_cutoff_selections,samples[prediction.sample_id].validation_domain_id,
            image,prediction.classifier_record_sha256)
        if selection is None:
            continue
        group=group_interactions((target,))[0]
        request=BatchInteractionResponseRequest(group.group_id,group.group_sha256,
            BatchTargetSelection(target_ids=(target.target_id,)),'review_supervised_classifier',
            dict(prediction_sha256=context['sha256'],confidence_threshold=selection.cutoff,reviewer_id=selection.reviewer_id),snapshot.head.sha256)
        validator=type(service._dependencies.interaction_owners).resolve(service._dependencies.interaction_owners,target)
        result=apply_group_response(group,request,validator,store,lease)
        if len(result.applications)!=1 or result.applications[0].status!='accepted_for_retry':
            raise ValueError('The saved assignment cutoff could not be applied to its current prediction.')
        return store.reopen_execution_snapshot()
    return snapshot
