"""Explicit administrative restoration of one interrupted trusted-local segmentation.

The old native exit stays unknown. The restored hierarchy is newly validated,
the abandoned attempt produces no artifacts, and ordinary settings revision
admits the next attempt. All old bytes remain in the recovery journal.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from mif_pipeline.pipeline_core import StageResult, StageStatus
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.local_process_absence import observe_local_process_absence

ERROR_CODE = 'segmentation_administratively_abandoned'


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _read(path):
    from .cluster.cluster_annotations import _read as read
    return read(path)


def _sha(path):
    from .cluster.cluster_annotations import _sha as sha
    return sha(path)


def _new(path, value):
    from .cluster.cluster_annotations import _new as new
    new(path, value)


def _source_context(store, snapshot, node, authorization):
    from .batch_artifacts import BatchNodeExecutionContext
    from .batch_service import _application_stage_specs
    spec = next(item for item in _application_stage_specs().specs
                if item.spec_sha256 == node.adapter_spec_sha256)
    selected = set(store._current_input_ref_sha256s(
        store._load_plan_snapshot(snapshot.head.active_plan_sha256), snapshot.head, node.node_id))
    binding = authorization.dispatch_binding
    return BatchNodeExecutionContext(authorization=authorization, node_id=node.node_id,
        adapter_spec=spec, inputs=tuple(ref for ref in snapshot.accepted_artifacts
                                     if ref.artifact_ref_sha256 in selected),
        **{key: getattr(binding, key) for key in (
            'projection_target_sha256', 'current_image_identity_sha256', 'script_sha256',
            'project_identity_sha256', 'project_authority_head_sha256',
            'project_qpproj_snapshot_sha256', 'project_entry_projection_sha256',
            'compatibility_signature_sha256')})


def _pending_target(catalogue, authorization):
    from .project_stage_plan import (_retained_project_stage_process_records,
        PreparedProjectStageRunDescriptor, ProjectStageRunPlan)
    targets = []
    for _ref, record in _retained_project_stage_process_records(catalogue):
        if type(record) is not PreparedProjectStageRunDescriptor:
            continue
        plan = catalogue._reopen_project_stage_runtime_record(record.run_plan_ref,
            record_type=ProjectStageRunPlan, digest_field='plan_sha256',
            expected_digest=record.run_plan_sha256, label='interrupted segmentation plan')
        for target in plan.targets:
            if target.attempt_id == authorization.attempt_id:
                if (target.node_id != authorization.node_id
                        or target.attempt_authorization_sha256 != authorization.authorization_sha256
                        or _digest({'artifact_ref_sha256s':list(target.prerequisite_artifact_ref_sha256s)})
                        != authorization.input_artifact_set_sha256
                        or target.terminal_stage_id != 'segmentation'):
                    raise ValueError('The retained segmentation target differs from its authorization.')
                targets.append((record, target))
    if len(targets) != 1:
        raise ValueError('The interrupted segmentation has no unique retained native target.')
    return targets[0]


def _fixture(store, snapshot, runtime, lease, target, folder):
    from .batch_tissue_artifacts import reopen_committed_accepted_tissue_live_authority
    from .batch_analysis_domain_artifacts import decode_committed_accepted_analysis_domain
    from .batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result
    from mif_pipeline.datasets.roi_inventory import open_roi_inventory, roi_inventory_member, ROI_INVENTORY_SCHEMA
    reopener = runtime.project_stage.committed_exchange_reopener(workspace_lease=lease)
    authorities = [a for a in snapshot.committed_artifact_authorities
                   if a.artifact_ref.scope.sample_id == target.sample_id]
    def one(kind):
        values = [a for a in authorities if a.artifact_ref.artifact_kind == kind]
        if len(values) != 1:
            raise ValueError(f'Restoration requires one accepted {kind} for the selected image.')
        return values[0]
    if any(a.artifact_ref.artifact_kind in {'segmentation_result','measurements_result',
            'secondary_inference_result'} for a in authorities):
        raise ValueError('This bounded recovery requires a first segmentation without secondary or accepted cell results.')
    tissue = reopen_committed_accepted_tissue_live_authority(one('tissue_acceptance_result'), exchange_reopener=reopener)
    domain = decode_committed_accepted_analysis_domain(one('accepted_analysis_domain'))
    primary_authority = one('primary_inference_result')
    primary = decode_committed_primary_classifier_inference_result(primary_authority)
    if (len(tissue.sections) != 1 or len(domain.domain_authorities) != 1
            or domain.selection_mode != 'whole_tissue_direct'
            or primary.pipeline_run_id != snapshot.head.batch_id
            or primary.section_id != tissue.sections[0].section.section_id
            or primary.analysis_domain_bindings != target.analysis_domain_bindings):
        raise ValueError('This bounded recovery requires one unchanged whole-tissue domain and its accepted primary ROIs.')
    root = reopener.classifier_output_dir(primary_authority)
    member = roi_inventory_member(primary.artifact_ref.producer_attempt_id)
    rows = []
    with open_roi_inventory(primary.roi_inventory, admitted_root=root, expected_member=member) as inventory:
        for batch in inventory.iter_batches(columns=ROI_INVENTORY_SCHEMA.names):
            rows.extend(batch.to_pylist())
    captured_table = folder / 'captured' / member
    captured_table.parent.mkdir(parents=True, exist_ok=True)
    from .cluster.cluster_annotations import _copy_checked
    _copy_checked(root / member, captured_table, _sha(root / member))
    kinds = ('analysis_frame','tissue_acceptance_result','accepted_analysis_domain','primary_inference_result')
    return {'sample_id':target.sample_id,'entry_id':target.project_entry_id,
        'accepted_artifacts':{kind:one(kind).artifact_ref.to_mapping() for kind in kinds},
        'tissue_generation':tissue.generation.to_mapping(),
        'roi_class_counts':dict(Counter(row['class_id'] for row in rows))}


def _native_launch(catalogue, descriptor, active):
    from .project_stage_plan import _retained_project_stage_process_records
    from mif_pipeline.qupath_runtime.project_stage import (ProjectStageProcessLaunchRecord,
        ProjectStageProcessAttemptRecord, ProjectStageProcessDrainRecord, ProjectStageMutationEvidenceRecord)
    payload = catalogue._read_ref(descriptor.process_launch_ref, max_bytes=32*1024*1024,
        label='interrupted project-stage launch', optional=False)
    launch = ProjectStageProcessLaunchRecord.from_mapping(json.loads(payload))
    records = [record for _ref,record in _retained_project_stage_process_records(catalogue,
        active_mutation_run_sha256=descriptor.active_mutation_run_sha256)
        if record.active_mutation_run_sha256 == descriptor.active_mutation_run_sha256]
    attempts = [record for record in records if type(record) is ProjectStageProcessAttemptRecord]
    if (canonical_json_bytes(launch.to_mapping()) != payload or len(attempts) != 1
            or any(type(record) in {ProjectStageProcessDrainRecord,ProjectStageMutationEvidenceRecord}
                   for record in records)
            or launch.attempt_sha256 != attempts[0].attempt_sha256
            or any(getattr(attempts[0],key) != getattr(descriptor,key) for key in (
                'workspace_storage_scope_sha256','project_identity_sha256','active_mutation_run_sha256',
                'acquisition_record_sha256','artifact_inventory_sha256','mutation_request_sha256',
                'mutation_authorization_sha256','fencing_token_sha256','run_plan_sha256','launch_nonce_sha256'))
            or launch.process_launch_ref != descriptor.process_launch_ref
            or launch.prepared_descriptor_sha256 != descriptor.prepared_descriptor_sha256
            or any(getattr(launch,key) != getattr(descriptor,key) for key in (
                'workspace_storage_scope_sha256','project_identity_sha256','active_mutation_run_sha256',
                'acquisition_record_sha256','artifact_inventory_sha256','mutation_request_sha256',
                'mutation_authorization_sha256','fencing_token_sha256','run_plan_sha256','launch_nonce_sha256'))
            or any(getattr(active,key) != getattr(descriptor,key) for key in (
                'project_identity_sha256','acquisition_record_sha256','artifact_inventory_sha256',
                'mutation_request_sha256','mutation_authorization_sha256','fencing_token_sha256'))):
        raise ValueError('Administrative recovery requires one exact launch without a retained completion.')
    return launch


def _receipt_path(root, digest):
    if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('The administrative restoration receipt identity is invalid.')
    return root / 'native-recovery' / f'{digest}.json'


def restoration_transitions(store, head):
    """Authenticate only restorations selected by output-free abandoned terminals."""
    transitions = {}
    for digest in head.terminal_attempt_sha256_by_attempt_id.values():
        terminal = store.load_attempt(digest)
        if terminal.stage_result.error_code != ERROR_CODE:
            continue
        selected = terminal.stage_result.error_context.get('administrative_recovery_sha256')
        receipt = _read(_receipt_path(store.layout.root, selected))
        unsigned = {key:value for key,value in receipt.items() if key != 'administrative_recovery_sha256'}
        if (receipt.get('administrative_recovery_sha256') != selected or _digest(unsigned) != selected
                or receipt.get('schema_version') != 'segmentation-administrative-recovery/v1'
                or terminal.stage_result.status is not StageStatus.FAILED or terminal.output_artifacts
                or terminal.execution_exchange_sha256 is not None
                or receipt['source_authorization_sha256'] != terminal.authorization_sha256
                or receipt['source_attempt_id'] != terminal.attempt_id
                or receipt['project_identity_sha256'] != head.project_identity_sha256
                or receipt['original_exit_status'] is not None
                or receipt['original_drain_reconstructed'] is not False):
            raise ValueError('The administrative restoration receipt differs from its abandoned attempt.')
        key = (receipt['project_entry_id'], receipt['accepted_saved_state_before_sha256'])
        after = receipt['restored_saved_state_sha256']
        if key in transitions and transitions[key] != after:
            raise ValueError('Administrative restoration receipts disagree.')
        transitions[key] = after
    return transitions


def abandon_interrupted_segmentation(service, *, node_id, expected_head_sha256):
    """Restore a validated candidate under exclusive locks; never infer old success."""
    from .batch_artifacts import BatchNodeExecutionResult
    from .batch_service import BatchServiceError
    from .cluster.cluster_annotations import _copy_checked, _sync
    from mif_pipeline.qupath_runtime.catalogue import _ProjectFileLock
    runtime = service._dependencies.runtime
    store, provider = runtime.state_store, runtime.project_provider
    with store.acquire_writer_lease('administrative-segmentation-recovery') as lease:
        snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
        head = snapshot.head
        if (head.operational_rebinding_sha256 is not None
                or store.layout.root != snapshot.manifest.manifest.output_root / head.batch_id
                or Path(head.project_path) != store.layout.root/'qupath'/'project.qpproj'):
            raise BatchServiceError('Administrative recovery requires the original local analysis location.')
        node = next((n for n in snapshot.plan.nodes if n.node_id == node_id), None)
        if (node is None or node.kind != 'segmentation'
                or set(head.pending_authorization_sha256_by_node) != {node_id}
                or head.pending_interaction_ids or head.pending_training_fit_set_sha256_by_node):
            raise BatchServiceError('Select the sole interrupted segmentation before administrative recovery.')
        authorization = store.resolve_pending_authorization(node, head.sha256, lease)
        context = _source_context(store, snapshot, node, authorization)
        catalogue = store.project_catalogue_store(lease)
        descriptor, target = _pending_target(catalogue, authorization)
        project = Path(head.project_path)
        journal = store.layout.root / 'native-recovery' / authorization.attempt_id
        journal.mkdir(parents=True, exist_ok=True)
        if (journal / 'intent.json').exists():
            from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseRecord
            intent = _read(journal / 'intent.json')
            receipt = _read(_receipt_path(store.layout.root, intent['administrative_recovery_sha256']))
            active = ProjectMutationLeaseRecord.from_mapping(_read(journal / 'source-lease.json'))
            inventory = store.reopen_project_mutation_artifact_inventory(active.artifact_inventory_sha256, lease=lease).inventory
            launch = _native_launch(catalogue, descriptor, active)
            return _finish_recovery(store, provider, lease, head, context, target, active, launch, receipt)
        candidates = provider._unresolved_active_records_for_project(
            project_uri=project.as_uri(), project_identity_sha256=head.project_identity_sha256)
        if len(candidates) != 1:
            raise BatchServiceError('The original unresolved project lease is unavailable or already disposed.')
        _path, active = candidates[0]
        inventory = store.reopen_project_mutation_artifact_inventory(active.artifact_inventory_sha256, lease=lease).inventory
        provider._validate_process_graph_completeness_for_recovery(active, inventory)
        if provider._exact_owner_liveness(active):
            raise BatchServiceError('The original application process is still live; recovery is unavailable.')
        launch = _native_launch(catalogue, descriptor, active)
        if (launch is None or descriptor.active_mutation_run_sha256 != provider._active_mutation_run_sha256(active)):
            raise BatchServiceError('The interrupted launch does not match its project lease.')
        observe_local_process_absence(launch=launch)
        folder = journal / ('preparation-' + uuid4().hex)
        folder.mkdir()
        original = project.parent / 'data' / target.project_entry_id / 'data.qpdata'
        captured = folder / 'captured' / 'qupath' / 'data' / target.project_entry_id / 'data.qpdata'
        captured.parent.mkdir(parents=True)
        observed_sha = _sha(original)
        _copy_checked(original, captured, observed_sha)
        observed_project_sha = _sha(project)
        captured_project = folder / 'captured' / 'qupath' / 'project.qpproj'
        _copy_checked(project, captured_project, observed_project_sha)
        _new(folder/'source-head.json', head.to_mapping())
        image = _fixture(store, snapshot, runtime, lease, target, folder)
        family = catalogue._authority_layout.layout.family('runtime_exchange')
        result_bytes = catalogue._read_ref(target.exchange.result_ref, max_bytes=family.max_member_bytes,
                                           label='unaccepted interrupted result', optional=False)
        result = json.loads(result_bytes)
        population = result['result']['outputs']['segmentation_population']
        if (population['section_id'] != node.section_target.section_id
                or population['slide_id'] != target.slide_id
                or result.get('invocation_id') != authorization.attempt_id):
            raise BatchServiceError('The retained unaccepted cell population belongs to another attempt.')
        candidate = folder / 'restored.qpdata'
        from mif_pipeline.qupath_runtime.native_image_reader import install_native_reader, READER_NAME
        install_native_reader(folder)
        native_reader = folder / READER_NAME
        request = {'source_head_sha256':head.sha256,
            'source_authorization_sha256':authorization.authorization_sha256,
            'capture_directory':str(folder), 'candidate':str(candidate),
            'images':[image], 'interrupted_population':population,
            'native_reader':{'path':str(native_reader),'sha256':_sha(native_reader)}}
        _new(folder/'request.json',request)
        from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset, _source_script_path
        with ExitStack() as assets:
            source_script = _source_script_path(QuPathScriptAsset.RESTORE_INTERRUPTED_SEGMENTATION, stack=assets)
            if source_script is None:
                raise BatchServiceError('The native recovery script is unavailable.')
            script = folder / 'restore.groovy'
            _copy_checked(source_script, script, _sha(source_script))
        run = runtime.runner.run_evidence_script(project_file=project, script=script,
            request_paths=(folder/'request.json',), log_directory=folder/'logs',
            require_existing_project=False,evidence_path=folder/'native-result.json')
        native = _read(folder/'native-result.json')
        restored_sha = _sha(candidate)
        if (native.get('candidate_sha256') != restored_sha
                or native.get('source_head_sha256') != head.sha256
                or native.get('source_authorization_sha256') != authorization.authorization_sha256
                or native.get('preserved_complete_hierarchy') is not True
                or native.get('old_result_accepted') is not False):
            raise BatchServiceError('The native restoration candidate did not validate.')
        # The ordinary bootstrap and identity locks exclude other project owners.
        with ExitStack() as locks:
            for path in (Path(f'{project}.mif-project-bootstrap.lock'),
                         provider._project_root(head.project_identity_sha256)/'mutation.lock'):
                lock = _ProjectFileLock(path); lock.acquire(); locks.callback(lock.release)
            if (store.load_head().sha256 != head.sha256 or _sha(original) != observed_sha
                    or _sha(project) != observed_project_sha):
                raise BatchServiceError('The saved analysis changed during recovery preparation.')
            current = provider._unresolved_active_records_for_project(
                project_uri=project.as_uri(), project_identity_sha256=head.project_identity_sha256)
            if current != candidates:
                raise BatchServiceError('The original project lease changed during recovery preparation.')
            observation = observe_local_process_absence(launch=launch)
            receipt = {'schema_version':'segmentation-administrative-recovery/v1',
                'source_state_head_sha256':head.sha256,'source_authorization_sha256':authorization.authorization_sha256,
                'source_attempt_id':authorization.attempt_id,'project_identity_sha256':head.project_identity_sha256,
                'project_entry_id':target.project_entry_id,'accepted_saved_state_before_sha256':target.saved_state_before_sha256,
                'observed_interrupted_saved_state_sha256':observed_sha,'restored_saved_state_sha256':restored_sha,
                'observed_project_sha256':observed_project_sha,'project_backup_path':str(captured_project),
                'accepted_artifact_ref_sha256s':list(target.prerequisite_artifact_ref_sha256s),
                'source_lease_record_sha256':active.lease_record_sha256,
                'native_evidence_sha256':_sha(folder/'native-result.json'),
                'native_request_sha256':_sha(folder/'request.json'),'native_script_sha256':run.script_sha256,
                'native_restore_exit_status':run.record.exit_status,
                'observation':observation,'original_exit_status':None,'original_drain_reconstructed':False,
                'backup_path':str(captured),'restored_candidate_path':str(candidate)}
            digest = _digest(receipt); receipt['administrative_recovery_sha256'] = digest
            _new(_receipt_path(store.layout.root,digest),receipt)
            if (journal/'source-lease.json').exists():
                if _read(journal/'source-lease.json') != active.to_mapping():
                    raise BatchServiceError('The administrative recovery source lease changed.')
            else:
                _new(journal/'source-lease.json',active.to_mapping())
            _new(journal/'intent.json',{'administrative_recovery_sha256':digest})
        return _finish_recovery(store, provider, lease, head, context, target, active, launch, receipt)


def _finish_recovery(store, provider, lease, head, context, target, active, launch, receipt):
    from .batch_artifacts import BatchNodeExecutionResult
    from .batch_service import BatchServiceError
    from .cluster.cluster_annotations import _copy_checked, _sync
    from mif_pipeline.qupath_runtime.catalogue import _ProjectFileLock
    digest = receipt['administrative_recovery_sha256']
    unsigned = {key:value for key,value in receipt.items() if key != 'administrative_recovery_sha256'}
    candidate = Path(receipt['restored_candidate_path'])
    captured = Path(receipt['backup_path'])
    restored_sha = receipt['restored_saved_state_sha256']
    observed_sha = receipt['observed_interrupted_saved_state_sha256']
    project = Path(head.project_path)
    original = project.parent/'data'/target.project_entry_id/'data.qpdata'
    authorization = context.authorization
    if (_digest(unsigned) != digest or receipt['source_state_head_sha256'] != head.sha256
            or receipt['source_authorization_sha256'] != authorization.authorization_sha256
            or receipt['source_lease_record_sha256'] != active.lease_record_sha256
            or receipt['observation']['process_launch_sha256'] != launch.process_launch_sha256
            or receipt['project_identity_sha256'] != head.project_identity_sha256
            or receipt['project_entry_id'] != target.project_entry_id
            or receipt['accepted_saved_state_before_sha256'] != target.saved_state_before_sha256
            or receipt['native_restore_exit_status'] != 0
            or _sha(candidate) != restored_sha or _sha(captured) != observed_sha
            or _sha(Path(receipt['project_backup_path'])) != receipt['observed_project_sha256']
            or _sha(candidate.parent/'native-result.json') != receipt['native_evidence_sha256']
            or _sha(candidate.parent/'request.json') != receipt['native_request_sha256']
            or _sha(candidate.parent/'restore.groovy') != receipt['native_script_sha256']):
        raise BatchServiceError('The retained administrative recovery changed; originals remain in its backup.')
    with ExitStack() as locks:
        for path in (Path(f'{project}.mif-project-bootstrap.lock'),
                     provider._project_root(head.project_identity_sha256)/'mutation.lock'):
            lock = _ProjectFileLock(path); lock.acquire(); locks.callback(lock.release)
        if (store.load_head().sha256 != head.sha256 or _sha(original) not in {observed_sha,restored_sha}
                or _sha(project) != receipt['observed_project_sha256']):
            raise BatchServiceError('The saved analysis changed before administrative recovery publication.')
        observation = observe_local_process_absence(launch=launch)
        _new(candidate.parent/('publication-observation-'+observation['observation_sha256']+'.json'), observation)
        result = StageResult.failed(stage='segmentation',slide_id=target.slide_id,
            error_code=ERROR_CODE,
            error_message='The interrupted segmentation was administratively abandoned. Its original exit remains unknown. Accepted tissue and primary ROIs were validated and restored; the unaccepted cell output was preserved separately. Start a new segmentation attempt using Segmentation options.',
            error_context={'administrative_recovery_sha256':digest,'original_exit_status':None,
                           'publication_observation_sha256':observation['observation_sha256'],
                           'original_drain_reconstructed':False})
        prepared = store.prepare_node_result(context,result,
            BatchNodeExecutionResult(authorization.authorization_sha256,()),execution_exchange_sha256=None)
        authority = provider._require_project_authority()
        archived = provider._archived_lease_record(active,'administratively_abandoned',digest)
        payload = canonical_json_bytes(archived.to_mapping())
        authority.publish_json(archived.lease_record_ref,payload)
        authority.publish_json(archived.lease_archive_ref,payload)
        current = provider._read_current_lease(project.as_uri())
        if current is None or current[2] not in (active, archived):
            raise BatchServiceError('The project lease changed before administrative recovery publication.')
        if current[2] == active:
            authority.cas(archived.lease_current_ref,archived.lease_record_ref,
                          expected=canonical_json_bytes(active.to_mapping()))
        temporary = original.with_name('.administrative-restoration.qpdata')
        _copy_checked(candidate,temporary,restored_sha)
        if _sha(original) not in {observed_sha,restored_sha}:
            raise BatchServiceError('The saved image changed before restoration.')
        temporary.replace(original); _sync(original.parent)
        committed = store._commit_terminal_result(context=context,prepared=prepared,lease=lease)
        store.current_project_entry_saved_state_sha256_by_id(
            expected_head_sha256=committed.committed_state_head_sha256,lease=lease)
        return {'state_head_sha256':committed.committed_state_head_sha256,
                'administrative_recovery_sha256':digest,'node_id':context.node_id,
                'original_exit_status':None,'backup_path':str(captured)}
