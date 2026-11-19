"""Inspect accepted reference evidence behind the current batch review."""
from pathlib import Path


def inspect_reference_component_preparation(service, batch_id, *, interaction_request_id,
                                             expected_state_head_sha256, component, phase):
    from .reference_source_preparation import PREPARATION_PHASES
    if phase not in PREPARATION_PHASES and phase != 'control_regions':
        raise ValueError('Choose a preparation page from its overview.')

    def project(operation, inspection, display_state):
        from .reference_unmixing import _thaw_json_value
        view = inspection['view']
        expected_phase = 'control_regions' if phase == 'control_regions' else PREPARATION_PHASES[phase]
        if inspection['component'] != component or inspection['phase'] != expected_phase:
            raise ValueError('The saved preparation does not match the selected page.')
        try:
            manifest = operation._load_manifest(view.run_id)
        except FileNotFoundError:
            manifest = operation._load_manifest(display_state['run_id'])
        state = operation._state_from_view(manifest, view).to_mapping()
        for key in ('run_id', 'configuration_id', 'target_id', 'start_authority_id',
                    'checkpoint_token', 'checkpoint_revision'):
            state[key] = display_state[key]
        # The saved page describes its own controls; cross-preparation editing
        # remains governed by the current request, not its historical inventory.
        revisions = [item for item in display_state['operation_requirements']
                     if item['option_value'] == 'revise_component_preparation']
        state['operation_requirements'] = [item for item in state['operation_requirements']
            if item['option_value'] != 'revise_component_preparation'] + revisions
        state['offered_actions'] = [item['option_value'] for item in state['operation_requirements']]
        values = dict(inspection['values'])
        result = {'state': state, 'component': component, 'phase': phase,
                  'checkpoint_token': display_state['checkpoint_token']}
        if phase == 'control_regions':
            result['revision_option'] = inspection['revision_option']
            if result['revision_option'] not in display_state['offered_actions']:
                raise ValueError('The current review does not offer changes to this source.')
        if phase in {'regions', 'control_regions'}:
            from .reference_region_authoring import component_preparation_region_summary
            summary = component_preparation_region_summary(operation, inspection,
                checkpoint_token=display_state['checkpoint_token'])
            values.pop('roi_export')
            values['roi_export_locator'] = summary['locator']
            result['region_summary'] = summary
        result['values'] = _thaw_json_value(values)
        return result

    def read(reference_service, run_id, token):
        if phase == 'control_regions':
            from .reference_region_authoring import inspect_control_regions
            return inspect_control_regions(reference_service, run_id, token, input_id=component)
        return reference_service.inspect_component_preparation(
            run_id, token, component=component, phase=PREPARATION_PHASES[phase])

    return _inspect_reference(service, batch_id, interaction_request_id=interaction_request_id,
        expected_state_head_sha256=expected_state_head_sha256,
        read=read, project=project)


def inspect_reference_component(service, batch_id, *, interaction_request_id,
                                expected_state_head_sha256, component):
    from mif_pipeline.preprocessing.unmixing.reference_component_inspection import inspect_accepted_component
    return _inspect_reference(service, batch_id, interaction_request_id=interaction_request_id,
        expected_state_head_sha256=expected_state_head_sha256,
        read=lambda reference_service, run_id, token: inspect_accepted_component(
            reference_service, run_id, token, component))


def inspect_reference_control_preparation(service, batch_id, *, interaction_request_id,
                                           expected_state_head_sha256, input_id,
                                           expected_preparation_sha256):
    from mif_pipeline.preprocessing.unmixing.reference_component_inspection import inspect_accepted_control_preparation
    return _inspect_reference(service, batch_id, interaction_request_id=interaction_request_id,
        expected_state_head_sha256=expected_state_head_sha256,
        read=lambda reference_service, run_id, token: inspect_accepted_control_preparation(
            reference_service, run_id, token, input_id=input_id,
            expected_preparation_sha256=expected_preparation_sha256))


def _inspect_reference(service, batch_id, *, interaction_request_id, expected_state_head_sha256, read,
                       project=None):
    from .batch_interactions import project_batch_interaction_target
    from .reference_unmixing import ReferenceUnmixingOperation

    with service._provider._use():
        service._require_batch(batch_id)
        store = service._dependencies.runtime.state_store
        head = store.load_head()
        if (head.sha256 != expected_state_head_sha256
                or interaction_request_id not in head.pending_interaction_ids):
            raise ValueError('The reference review changed. Reopen its component overview.')
        publication = store.reopen_interaction_request_publication(
            interaction_request_id, expected_head_sha256=head.sha256)
        target = project_batch_interaction_target(publication)
        if target.stage_result.stage != 'reference_unmixing':
            raise ValueError('This review does not contain spectral-reference components.')
        owner = target.stage_result.resume_context['service_context']
        if owner.get('interaction_kind') != 'reference_unmixing_operation':
            raise ValueError('No accepted reference component is available at this step.')
        state = owner['operation_state']
        operation = ReferenceUnmixingOperation.open_existing(Path(owner['operation_root_locator']))
        display_run, display_token = state['run_id'], state['checkpoint_token']
        run_id, checkpoint_token = operation.reference_service_identity(display_run, display_token)
        result = read(operation._service, run_id, checkpoint_token)
        if project is not None:
            result = project(operation, result, state)
        if display_run != run_id:
            operation.reference_service_identity(display_run, display_token)
        if store.load_head().sha256 != expected_state_head_sha256:
            raise ValueError('The reference review changed. Reopen its component overview.')
        return result
