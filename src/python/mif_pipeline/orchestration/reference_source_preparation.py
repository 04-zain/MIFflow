"""Primitive operation boundary for source preparation and later stain assignment."""
from __future__ import annotations

from mif_pipeline.preprocessing.unmixing.typed_checksum import typed_checksum_sha256

PREPARATION_PHASES = {'regions': 'positive_reference_rois', 'assignments': 'af_reference_rois',
    'bands': 'signal_dark_measurements', 'extraction': 'extraction_qc',
    'consensus': 'fluorophore_consensus'}

ACTIONS = frozenset({'prepare_shared_control', 'accept_shared_control',
                     'prepare_stained_reference', 'assign_shared_control_sets', 'review_control_sets',
                     'revise_component_preparation'})
RESPONSE_ACTIONS = {action: action for action in ACTIONS if action != 'accept_shared_control'}
RESPONSE_ACTIONS['accept_shared_control_preparation'] = 'accept_shared_control'
REGION_FIELDS = frozenset({'reviewer_id', 'roi_export_locator', 'reference_region_selection'})
POLICIES = {
    'prepare_shared_control': frozenset({'control_sampling_policy', 'background_sampling_policy', 'background_policy'}),
    'prepare_stained_reference': frozenset({'positive_sampling_policy', 'background_sampling_policy', 'background_policy'}),
    'accept_shared_control': frozenset(), 'assign_shared_control_sets': frozenset(), 'review_control_sets': frozenset(),
    'revise_component_preparation': frozenset(),
}


def form_fields(action):
    if action == 'revise_component_preparation':
        return frozenset({'reviewer_id', 'component', 'phase', 'values'})
    return (REGION_FIELDS if action in {'prepare_shared_control', 'prepare_stained_reference'}
            else frozenset({'reviewer_id', 'control_set_assignments'}) if action == 'assign_shared_control_sets'
            else frozenset({'reviewer_id', 'control_sets'}) if action == 'review_control_sets'
            else frozenset({'reviewer_id'}))


def component_revision_requirement(view):
    """Declare the saved-preparation action; the service checks phase ownership."""
    from .reference_unmixing import (
        ReferenceUnmixingOperationRequirement, ReferenceUnmixingFormValueKind as Kind,
        _form_field, _thaw_json_value,
    )
    progress = _thaw_json_value(view.review_summary).get('reference_preparation_progress', {})
    components = tuple(row['name'] for row in progress.get('components', ())
        if row.get('status') == 'accepted' or row.get('status') == 'current'
        and view.checkpoint in {'af_reference_rois', 'signal_dark_measurements',
                               'extraction_qc', 'fluorophore_consensus'})
    if not components or view.checkpoint is None:
        return None
    return ReferenceUnmixingOperationRequirement(
        option_value='revise_component_preparation', label='Apply preparation changes',
        description='Apply the changes to the selected fluorophore preparation.',
        operation_action='revise_component_preparation', minimum_selected_ids=0,
        minimum_artifacts=0, artifact_requirements=(), form_fields=(
            _form_field('reviewer_id', 'Reviewer', 'Reviewer of the preparation.', Kind.TEXT),
            _form_field('component', 'Fluorophore', 'Preparation to change.', Kind.ENUM,
                        value_choices=components),
            _form_field('phase', 'Preparation page', 'Page being changed.', Kind.ENUM,
                        value_choices=tuple(PREPARATION_PHASES)),
            _form_field('values', 'Changes', 'Selections and settings on that page.', Kind.PRIMITIVE_MAPPING),
        ))


def configured_source_response(body):
    """Recover the same saved response after its exact service write was committed."""
    from .reference_unmixing import ReferenceUnmixingConfiguredAction
    action = RESPONSE_ACTIONS.get(body.get('option_value'))
    if (action is None or set(body['form_values']) != form_fields(action)
            or set(body['injected_configuration']) != POLICIES[action]):
        raise ValueError('Source preparation response fields differ')
    return ReferenceUnmixingConfiguredAction.from_mapping(checkpoint_token=body['checkpoint_token'],
        action=action, configuration={**body['form_values'], **body['injected_configuration']})


def preparation_requirements(view, existing):
    """Keep legacy routes intact; expose only the new route's actual next action."""
    from .reference_unmixing import (
        ReferenceUnmixingOperationRequirement, ReferenceUnmixingFormValueKind as Kind,
        _form_field, _thaw_json_value,
    )
    summary = _thaw_json_value(view.review_summary)
    shared = summary.get('reference_preparation_progress', {}).get('control_source') or summary.get('reference_preparation_progress', {}).get('shared_control') or {}
    if shared.get('preparation_mode') != 'independent':
        return existing, False
    phase = view.checkpoint
    reviewer = _form_field('reviewer_id', 'Reviewer', 'Reviewer of the current preparation.', Kind.TEXT)
    set_review = ReferenceUnmixingOperationRequirement(option_value='review_control_sets',
        label='Apply AF set changes', description='Combine retained source regions without reading image pixels.',
        operation_action='review_control_sets', minimum_selected_ids=0, minimum_artifacts=0,
        artifact_requirements=(), form_fields=(reviewer, _form_field('control_sets', 'AF sets',
            'Ordered sets of sampled source region IDs; omitted regions remain excluded.', Kind.PRIMITIVE_LIST)))
    if phase not in {'shared_control_preparation', 'positive_reference_rois', 'af_reference_rois'}:
        return [*existing, *([set_review] if shared.get('accepted') and shared.get('can_revise') else [])], True
    regions = (reviewer,
        _form_field('roi_export_locator', 'Reviewed regions', 'Use the accepted native region export.', Kind.LOCATOR),
        _form_field('reference_region_selection', 'Reference regions', 'Choose tissue and its image background.', Kind.PRIMITIVE_MAPPING))
    if phase == 'shared_control_preparation':
        if shared.get('proposal_available'):
            option, action, title, fields = 'accept_shared_control_preparation', 'accept_shared_control', 'Use AF preparation', (reviewer,)
        else:
            option = action = 'prepare_shared_control'
            title, fields = 'Review and combine AF spectra', regions
    elif phase == 'positive_reference_rois':
        option = action = 'prepare_stained_reference'
        title, fields = 'Use stained regions', regions
    else:
        option = action = 'assign_shared_control_sets'
        title = 'Use AF assignments'
        assignment = summary['shared_control_assignment']
        fields = (reviewer, _form_field('control_set_assignments', 'AF sets for stained regions',
            'Assign an accepted source set to each stained region before extraction.', Kind.ENUM_MAPPING,
            mapping_key_choices=tuple(item['roi_id'] for item in assignment['positive_regions']),
            value_choices=tuple(item['set_id'] for item in assignment['sets'])))
    requirement = ReferenceUnmixingOperationRequirement(option_value=option, label=title,
        description=title, operation_action=action, minimum_selected_ids=0, minimum_artifacts=0,
        artifact_requirements=(), form_fields=fields)
    retained = [item for item in existing if item.operation_action == 'decide']
    if shared.get('proposal_available') or shared.get('accepted') and shared.get('can_revise'):
        retained.insert(0, set_review)
    return [requirement, *retained], True


def advance_source_preparation(operation, request, action):
    """Retain parent/child token fencing and let each service operation recover its own exact write."""
    manifest = operation._load_manifest(request.run_id)
    operation._require_request(manifest, request)
    service = operation._service
    parent, start = service._store._load_service_checkpoint(request.run_id)
    if start != manifest.start_authority_sha256 or parent.target_image_sha256 != manifest.target_image_sha256:
        raise ValueError('Reference unmixing target authority changed')
    child = operation._shared_coordinator().persisted_child(request.run_id)
    run_id = request.run_id if child is None else child.run_id
    current, _ = service._store._load_service_checkpoint(run_id)
    wrap = lambda token: token
    if child is not None:
        session = operation._shared_coordinator()._session(request.run_id, start)
        wrap = lambda token: typed_checksum_sha256({'shared_session': session.content_sha256,
            'child_run': run_id, 'child_checkpoint': token})
    identities = {current.content_sha256, *(item.source_checkpoint_sha256 for item in current.transition_log)}
    matching = tuple(token for token in identities if wrap(token) == action.checkpoint_token)
    if len(matching) != 1:
        raise ValueError('The source preparation belongs to an earlier reference task')
    token = matching[0]
    configuration = action.configuration_mapping()
    if set(configuration) != form_fields(action.action) | POLICIES[action.action]:
        raise ValueError('Source preparation configuration fields differ')
    reviewer = configuration['reviewer_id']
    if action.action == 'revise_component_preparation':
        if (type(configuration['values']) is not dict
                or configuration['phase'] not in PREPARATION_PHASES
                or type(configuration['component']) is not str or not configuration['component']):
            raise ValueError('Select a saved preparation and provide its page settings.')
        values = dict(configuration['values'])
        if 'roi_export_locator' in values:
            values['roi_export'] = operation._resources.resolve_reference_roi_export(
                values.pop('roi_export_locator'))
        result = service.revise_component_preparation(run_id, token,
            component=configuration['component'], phase=PREPARATION_PHASES[configuration['phase']],
            reviewer_id=reviewer, values=values)
    elif action.action in {'prepare_shared_control', 'prepare_stained_reference'}:
        export = operation._resources.resolve_reference_roi_export(configuration['roi_export_locator'])
        values = configuration['reference_region_selection']
        required = {'positive_roi_ids', 'background_roi_ids', 'pairings', 'control_sets'}
        if (type(values) is not dict or not required <= set(values)
                or set(values) - required - {'control_set_assignments'}
                or values['pairings'] or values.get('control_set_assignments')):
            raise ValueError('Choose source regions now; assign AF sets during extraction setup')
        if any(type(values[key]) is not list for key in ('positive_roi_ids', 'background_roi_ids', 'control_sets')):
            raise ValueError('Region choices must be ordered lists')
        policies = {key: configuration[key] for key in POLICIES[action.action]}
        if action.action == 'prepare_shared_control':
            if values['positive_roi_ids'] or any(type(group) is not list for group in values['control_sets']):
                raise ValueError('Control preparation accepts AF sets and its own image background')
            members = {identity for group in values['control_sets'] for identity in group}
            controls = tuple(roi.roi_id for roi in export.rois if roi.roi_id in members)
            if set(controls) != members:
                raise ValueError('AF sets contain unknown regions')
            result = service.prepare_shared_control(run_id, token, roi_export=export,
                control_roi_ids=controls, background_roi_ids=tuple(values['background_roi_ids']),
                control_sets=tuple(tuple(group) for group in values['control_sets']), reviewer_id=reviewer, **policies)
        else:
            if values['control_sets']:
                raise ValueError('Stain annotation does not redefine prepared AF sets')
            result = service.prepare_stained_reference(run_id, token, roi_export=export,
                positive_roi_ids=tuple(values['positive_roi_ids']), background_roi_ids=tuple(values['background_roi_ids']),
                reviewer_id=reviewer, **policies)
    elif action.action == 'review_control_sets':
        groups = configuration['control_sets']
        if type(groups) is not list or any(type(group) is not list for group in groups):
            raise ValueError('AF set choices must be ordered region lists')
        result = service.review_control_sets(run_id, token,
            control_sets=tuple(tuple(group) for group in groups), reviewer_id=reviewer)
    elif action.action == 'accept_shared_control':
        result = service.accept_shared_control(run_id, token, reviewer_id=reviewer)
    else:
        result = service.assign_shared_control_sets(run_id, token,
            assignments=configuration['control_set_assignments'], reviewer_id=reviewer)
    operation._advance_snapshot(result)
    return operation.status(request.run_id)
