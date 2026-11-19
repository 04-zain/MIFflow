"""Submit one complete region choice through the existing ordered ROI phases.

The batch response retains the whole submitted choice. Existing immutable phase
plans retain completed work; no preview or duplicate sampling cache is created.
"""
from __future__ import annotations

import json

from mif_pipeline.preprocessing.unmixing.reference_rois import AcceptedReferenceRoiSelection
from mif_pipeline.preprocessing.unmixing.reference_service import (
    _purification_sampling_policy_from_mapping, _background_policy_from_mapping,
)
from mif_pipeline.preprocessing.unmixing.reference_service_roi_purification import ReferenceRoiPairingSelection
from mif_pipeline.preprocessing.unmixing.typed_checksum import typed_checksum_sha256

REGION_CHECKPOINTS = frozenset({'positive_reference_rois', 'af_reference_rois',
                              'negative_reference_rois', 'background_reference_rois'})
REGION_OPTION = 'accept_reference_regions'
REGION_ACTION = 'select_reference_regions'
POLICY_FIELDS = frozenset({'positive_sampling_policy', 'control_sampling_policy',
                           'background_sampling_policy', 'background_policy'})
FORM_FIELDS = frozenset({'reviewer_id', 'roi_export_locator', 'reference_region_selection'})


def region_requirement():
    from .reference_unmixing import (ReferenceUnmixingOperationRequirement,
        ReferenceUnmixingFormValueKind, _form_field)
    return ReferenceUnmixingOperationRequirement(
        option_value=REGION_OPTION, label='Use reference regions',
        description='Use the selected stained regions, matched controls and image backgrounds.',
        operation_action=REGION_ACTION, minimum_selected_ids=0, minimum_artifacts=0,
        artifact_requirements=(), form_fields=(
            _form_field('reviewer_id', 'Reviewer', 'Reviewer of reference regions.',
                        ReferenceUnmixingFormValueKind.TEXT),
            _form_field('roi_export_locator', 'Reviewed regions', 'Use the accepted native region export.',
                        ReferenceUnmixingFormValueKind.LOCATOR),
            _form_field('reference_region_selection', 'Reference regions',
                        'Choose stained tissue, controls and image background together.',
                        ReferenceUnmixingFormValueKind.PRIMITIVE_MAPPING)))


def saved_region_choices(service, current, start, export):
    """Project committed selections/settings for reopening a legacy partial step."""
    control_phase = ('negative_reference_rois' if current.control_strategy == 'per_stain_negative_controls'
                     else 'af_reference_rois')
    phases = ('positive_reference_rois', control_phase, 'background_reference_rois')
    selection, settings = {}, {}
    for phase, role in zip(phases, ('positive', 'control', 'background'), strict=True):
        if phase == current.current_checkpoint.value:
            break
        plan = service._live_roi_phase_artifact(current, start, 'roi_selection_plan',
                                                current.current_fluorophore, phase).record
        policy = plan.sampling_policy
        settings[role + '_sampling_policy'] = {
            'processing_chunk_shape': [policy.processing_chunk_shape.width, policy.processing_chunk_shape.height],
            'saturation_fraction': policy.saturation_fraction,
            'near_zero_l2_threshold': policy.near_zero_l2_threshold}
        if export is None or plan.export.content_sha256 != export.content_sha256:
            continue
        if role == 'positive':
            selection['positive_roi_ids'] = [item.roi_id for item in plan.selections]
        elif role == 'control':
            mapping = {}
            for pair in plan.pairings:
                mapping.setdefault(pair.positive_roi_id, []).append(pair.control_roi_id)
            selection['pairings'] = mapping
            sets = []
            for members in mapping.values():
                if members not in sets:
                    sets.append(members)
            selection['control_sets'] = sets
    return selection, settings


def configured_region_response(body):
    """Rebuild an already retained response, including a partially applied one."""
    from .reference_unmixing import ReferenceUnmixingConfiguredAction
    if (body.get('option_value') != REGION_OPTION or set(body['form_values']) != FORM_FIELDS
            or set(body['injected_configuration']) != POLICY_FIELDS):
        raise ValueError('Reference-region response fields differ')
    return ReferenceUnmixingConfiguredAction.from_mapping(
        checkpoint_token=body['checkpoint_token'], action=REGION_ACTION,
        configuration={**body['form_values'], **body['injected_configuration']})


def _ordered_ids(value, eligible, label, *, allow_empty=False):
    if (type(value) is not list or (not value and not allow_empty)
            or any(type(item) is not str for item in value)
            or len(value) != len(set(value)) or set(value) - set(eligible)):
        raise ValueError(f'Choose valid {label} from the displayed reference regions')
    return tuple(item for item in eligible if item in value)


def _selection(service, current, start, export, configuration):
    """Validate every future phase before the first sample or invalidation."""
    if set(configuration) != FORM_FIELDS | POLICY_FIELDS:
        raise ValueError('Reference-region configuration fields differ')
    reviewer = configuration['reviewer_id']
    if type(reviewer) is not str or not reviewer.strip():
        raise ValueError('Reference-region selection requires a reviewer')
    selection = configuration['reference_region_selection']
    required = {'positive_roi_ids', 'pairings', 'background_roi_ids', 'control_sets'}
    if (type(selection) is not dict or not required <= set(selection)
            or set(selection) - required - {'control_set_assignments'}):
        raise ValueError('Reference-region selection fields differ')
    binding = service._one_artifact(current.run_id, start, 'reference_control_files',
                                    'reference-control-files').record
    index = current.fluorophores.index(current.current_fluorophore)
    positive_input = binding.positive_reference_input_ids[index]
    negative = current.control_strategy == 'per_stain_negative_controls'
    control_input = (binding.negative_control_input_ids[index] if negative
                     else binding.shared_unstained_input_id)
    entries = {entry.project_entry_id: entry.image.reference_input_id for entry in export.entries}
    eligible = lambda role, inputs: tuple(roi.roi_id for roi in export.rois
        if roi.roi_role == role and entries[roi.project_entry_id] in inputs)
    positive_ids = eligible('positive', {positive_input})
    control_ids = eligible('negative' if negative else 'af', {control_input})
    background_ids = eligible('background', {positive_input, control_input})
    positives = _ordered_ids(selection['positive_roi_ids'], positive_ids, 'stained regions')
    backgrounds = _ordered_ids(selection['background_roi_ids'], background_ids, 'background regions')
    mapping = selection['pairings']
    if type(mapping) is not dict or set(mapping) - set(positive_ids):
        raise ValueError('Control assignments contain foreign stained regions')
    memberships = {key: _ordered_ids(value, control_ids, 'control regions', allow_empty=key not in positives)
                   for key, value in mapping.items()}
    if set(positives) - set(memberships) or any(not memberships[key] for key in positives):
        raise ValueError('Choose a control for every selected stained region')
    if negative and any(len(memberships[key]) != 1 for key in positives):
        raise ValueError('Choose one matched negative control for each stained region')
    sets = selection['control_sets']
    if type(sets) is not list:
        raise ValueError('AF control sets must be a list')
    for members in sets:
        _ordered_ids(members, control_ids, 'control-set members', allow_empty=True)
    assignments = selection.get('control_set_assignments', {})
    if type(assignments) is not dict or set(assignments) - set(positive_ids):
        raise ValueError('Control-set assignments contain foreign stained regions')
    for key, index in assignments.items():
        if index is not None and (type(index) is not int or not 0 <= index < len(sets)
                or set(sets[index]) != set(mapping.get(key, ()))):
            raise ValueError('Control-set assignment differs from its displayed members')
    used = {member for key in positives for member in memberships[key]}
    controls = tuple(identity for identity in control_ids if identity in used)
    samples = {roi.roi_id: AcceptedReferenceRoiSelection(export, roi.roi_id)
               for roi in export.rois if roi.roi_id in set(positives + controls + backgrounds)}
    if {samples[key].entry.image.reference_input_id for key in backgrounds} != {positive_input, control_input}:
        raise ValueError('Select image background for both the stained and control image')
    if any(samples[key].roi.role_link_id != current.current_fluorophore for key in positives):
        raise ValueError('Stained region identity differs from this fluorophore')
    expected_link = current.current_fluorophore if negative else 'shared'
    if any(samples[key].roi.role_link_id != expected_link for key in controls):
        raise ValueError('Control region identity differs from this reference')
    policies = {role: _purification_sampling_policy_from_mapping(configuration[role + '_sampling_policy'])
                for role in ('positive', 'control', 'background')}
    background_policy = _background_policy_from_mapping(configuration['background_policy'])
    phases = ('positive_reference_rois', 'negative_reference_rois' if negative else 'af_reference_rois',
              'background_reference_rois')
    pairings = tuple(ReferenceRoiPairingSelection(key, member) for key in positives
                     for member in control_ids if member in memberships[key])
    choices = dict(zip(phases, (positives, controls, backgrounds), strict=True))
    return phases, {phase: {'roi_export': export,
        'selections': tuple(samples[key] for key in choices[phase]),
        'pairings': pairings if phase == phases[1] else (),
        'sampling_policy': policies[role],
        'background_policy': background_policy if role == 'background' else None,
        'reviewer_id': reviewer} for phase, role in zip(phases, ('positive', 'control', 'background'), strict=True)}


def _origin(current, token, wrap):
    if wrap(current.content_sha256) == token:
        if current.current_checkpoint.value not in REGION_CHECKPOINTS:
            raise ValueError('Open the current reference-region review before saving')
        return current.content_sha256, current.current_checkpoint.value, ()
    matches = [(index, transition) for index, transition in enumerate(current.transition_log)
               if wrap(transition.source_checkpoint_sha256) == token]
    if len(matches) != 1:
        raise ValueError('The reference-region submission is stale')
    index, transition = matches[0]
    source = json.loads(transition.source_state_json)
    if (source['checkpoint'] not in REGION_CHECKPOINTS
            or source['current_fluorophore'] != current.current_fluorophore):
        raise ValueError('The reference-region submission belongs to another review')
    suffix = current.transition_log[index:]
    decisions = {item.content_sha256: item for item in current.decisions}
    for position, item in enumerate(suffix):
        if json.loads(item.source_state_json)['current_fluorophore'] != current.current_fluorophore:
            raise ValueError('The reference-region lineage changed')
        if item.kind.value == 'proposal':
            if json.loads(item.source_state_json)['checkpoint'] not in REGION_CHECKPOINTS:
                raise ValueError('The reference-region lineage changed')
        elif item.kind.value == 'decision':
            decision = decisions[item.decision_record_sha256]
            option = decision.response.option_value
            if option == 'reselect_reference_rois':
                if position != 0 or source['checkpoint'] == 'positive_reference_rois':
                    raise ValueError('A later reselection invalidated the reference-region submission')
            elif option not in {f'accept_{phase}' for phase in REGION_CHECKPOINTS}:
                raise ValueError('The reference-region submission is stale after another decision')
        else:
            raise ValueError('The reference-region lineage changed')
    return transition.source_checkpoint_sha256, source['checkpoint'], suffix


def _plan_matches(plan, values):
    return (plan.export == values['roi_export'] and plan.selections == values['selections']
            and plan.pairings == values['pairings'] and plan.sampling_policy == values['sampling_policy'])


def _committed_choices_match(service, current, start, phases, inputs, suffix):
    phase = current.current_checkpoint.value
    completed = phases[:phases.index(phase)] if phase in phases else phases
    new_authorities = {item.source_checkpoint_sha256 for item in suffix}
    for phase in completed:
        plan = service._live_roi_phase_artifact(current, start, 'roi_selection_plan',
                                                current.current_fluorophore, phase).record
        if not _plan_matches(plan, inputs[phase]):
            return False
        if plan.source_checkpoint_sha256 in new_authorities and plan.reviewer_id != inputs[phase]['reviewer_id']:
            return False
    if phases[-1] in completed:
        background = service._live_roi_phase_artifact(current, start, 'background_operation',
            current.current_fluorophore, phases[-1]).record
        if background.policy != inputs[phases[-1]]['background_policy']:
            return False
    return True


def advance_reference_regions(operation, request, action):
    """Run/recover one retained all-role response without changing phase semantics."""
    manifest = operation._load_manifest(request.run_id)
    operation._require_request(manifest, request)
    service = operation._service
    parent, parent_start = service._store._load_service_checkpoint(request.run_id)
    if (parent_start != manifest.start_authority_sha256
            or parent.target_image_sha256 != manifest.target_image_sha256):
        raise ValueError('Reference unmixing target authority changed')
    coordinator = operation._shared_coordinator()
    child = coordinator.persisted_child(request.run_id)
    run_id = request.run_id if child is None else child.run_id
    wrap = lambda token: token
    if child is not None:
        session = coordinator._session(request.run_id, parent_start)
        wrap = lambda token: typed_checksum_sha256({'shared_session': session.content_sha256,
            'child_run': run_id, 'child_checkpoint': token})
    current, start = service._store._load_service_checkpoint(run_id)
    _origin_token, _origin_phase, suffix = _origin(current, action.checkpoint_token, wrap)
    configuration = action.configuration_mapping()
    export = operation._resources.resolve_reference_roi_export(configuration['roi_export_locator'])
    phases, inputs = _selection(service, current, start, export, configuration)
    # A new edit at a legacy partial checkpoint may revise earlier selections.
    # Only that live submission can invalidate; stale retries must match exactly.
    if not _committed_choices_match(service, current, start, phases, inputs, suffix):
        if suffix:
            raise ValueError('Reference-region retry choices differ from saved sampling')
        service.respond_with_offered_ids(run_id, current.content_sha256,
            option='reselect_reference_rois', reviewer_id=configuration['reviewer_id'])
        current, start = service._store._load_service_checkpoint(run_id)
    for phase in phases:
        current, start = service._store._load_service_checkpoint(run_id)
        _, _, suffix = _origin(current, action.checkpoint_token, wrap)
        if not _committed_choices_match(service, current, start, phases, inputs, suffix):
            raise ValueError('Reference-region choices changed while sampling')
        if current.current_checkpoint.value != phase:
            continue
        # A phase can fail between proposal and decision CAS. Its original plan
        # authority is the proposal's parent; the existing service verifies and
        # finishes exactly that persisted plan without resampling.
        token = (current.prior_checkpoint_sha256 if current.request.proposed_artifacts
                 else current.content_sha256)
        operation._advance_snapshot(service.advance_reference_purification(run_id, token, **inputs[phase]))
    current, start = service._store._load_service_checkpoint(run_id)
    _, _, suffix = _origin(current, action.checkpoint_token, wrap)
    if not _committed_choices_match(service, current, start, phases, inputs, suffix):
        raise ValueError('Reference-region choices changed while sampling')
    return operation.status(request.run_id)
