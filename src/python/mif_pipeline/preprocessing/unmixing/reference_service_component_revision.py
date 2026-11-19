"""Revisit saved component preparation and commit only changed phase inputs."""
from dataclasses import replace, fields
import json

from .interactions import ReferenceCheckpointKind, SelectedIdentifier
from .reference_service_records import ReferenceServiceAdvance, ReferenceServiceAdvanceStatus, ReferenceServiceSnapshot
from .reference_service_storage import ReferenceCheckpointConflictError
from .reference_workflow import ReferenceTransitionKind, revise_component_preparation_checkpoint
from .typed_checksum import typed_checksum_sha256

PHASES = {'positive_reference_rois', 'af_reference_rois', 'signal_dark_measurements',
          'extraction_qc', 'fluorophore_consensus'}
ALIASES = {'regions': 'positive_reference_rois', 'assignments': 'af_reference_rois',
           'bands': 'signal_dark_measurements', 'extraction': 'extraction_qc',
           'spectrum': 'fluorophore_consensus'}


def _decision(current, component, phase):
    superseded = {identity for transition in current.transition_log for identity in transition.superseded_request_ids}
    decisions = current.active_decisions
    for index in range(len(decisions) - 1, -1, -1):
        item = decisions[index]
        source_revision = item.response.option_value == 'revise_shared_control' and (
            current.control_strategy == 'shared_unstained_control' or item.request.fluorophore in {None, component})
        own_revision = item.request.fluorophore == component and (
            item.response.option_value == 'reselect_reference_rois' or
            item.response.option_value == 'revise_signal_dark_measurements' and phase in {
                'signal_dark_measurements', 'extraction_qc', 'fluorophore_consensus'})
        if source_revision or own_revision:
            decisions = decisions[index + 1:]
            break
    matches = [item for item in decisions if item.request.fluorophore == component
        and item.request.checkpoint.value == phase and item.response.option_value == 'accept_' + phase
        and item.request.request_id not in superseded]
    if len(matches) != 1:
        raise ValueError('This component does not have a completed preparation for this page')
    return matches[0]


def _policy(policy):
    return {item.name: getattr(policy, item.name) for item in fields(policy)
            if item.init and item.name != 'policy_version'}


def _sampling(policy):
    return {'processing_chunk_shape': [policy.processing_chunk_shape.width, policy.processing_chunk_shape.height],
        'saturation_fraction': policy.saturation_fraction, 'near_zero_l2_threshold': policy.near_zero_l2_threshold}


def _saved(service, current, start, component, phase):
    decision = _decision(current, component, phase)
    source, _, authority = service._store._load_service_decision_authority(current.run_id,
        source_checkpoint_sha256=decision.source_checkpoint_sha256,
        decision_record_sha256=decision.content_sha256)
    if authority != start:
        raise ReferenceCheckpointConflictError('Saved component preparation authority differs')
    live = lambda kind, stage: service._live_roi_phase_artifact(current, start, kind, component, stage).record
    if phase == 'signal_dark_measurements':
        values = {'signal_dark_selections': [item.to_mapping() for item in decision.response.selections],
            'purification_policy': dict(service._historical_purification_policy(source, start, decision))}
    elif phase == 'extraction_qc':
        values = {'extraction_dispositions': [item.to_mapping() for item in decision.response.selections]}
    elif phase == 'fluorophore_consensus':
        candidate = service._artifacts.load_pointer(run_id=current.run_id, start_authority_sha256=start,
            kind='consensus_candidate', subject_id=component, pointer=decision.response.artifacts[0]).record
        values = {'consensus_policy': _policy(candidate.policy), 'roi_review': {
            item.roi_id: ('exclude' if not item.included else 'retain' if
                item.reason == 'Retained after explicitly reviewing the spectral difference' else 'include')
            for item in candidate.inclusion_decisions}}
    elif phase == 'af_reference_rois':
        from .reference_service_control_preparation import accepted_preparation, _source_set_id
        prepared = accepted_preparation(service, source, start)
        if prepared is None:
            raise ValueError('AF-set assignments require an independently prepared control source')
        plan = live('roi_selection_plan', phase)
        by_positive = {}
        for pair in plan.pairings:
            by_positive.setdefault(pair.positive_roi_id, []).append(pair.control_roi_id)
        values = {'control_set_assignments': {positive: _source_set_id(prepared, tuple(controls))
            for positive, controls in by_positive.items()}}
    else:
        positive = live('roi_selection_plan', 'positive_reference_rois')
        # Independent stain geometry/background choices are recorded together
        # before the later assignment decision, including an unfinished assignment.
        choice = service._artifact_at_authority_or_none(run_id=current.run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=positive.source_checkpoint_sha256,
            kind='stained_region_choice', subject_id=component)
        if choice is None:
            background = live('roi_selection_plan', 'background_reference_rois')
            background_policy = live('background_operation', 'background_reference_rois').policy
        else:
            background, background_policy = choice.record.background_plan, choice.record.background_policy
        if positive.export != background.export:
            raise ValueError('Reopen the existing separate-image region review for this preparation')
        values = {'roi_export': positive.export,
            'positive_roi_ids': [item.roi_id for item in positive.selections],
            'background_roi_ids': [item.roi_id for item in background.selections],
            'positive_sampling_policy': _sampling(positive.sampling_policy),
            'background_sampling_policy': _sampling(background.sampling_policy),
            'background_policy': _policy(background_policy)}
    return source, values


def inspect_component_preparation(service, run_id, token, *, component, phase):
    requested_phase, phase = phase, ALIASES.get(phase, phase)
    if phase not in PHASES:
        raise ValueError('Unsupported component preparation page')
    current, start = service._store._load_service_checkpoint(run_id)
    if current.content_sha256 != token:
        raise ReferenceCheckpointConflictError('The preparation changed. Reopen its overview')
    source, values = _saved(service, current, start, component, phase)
    view = service._bounded_view(source, start)
    # The saved page is a projection. Every submission remains bound to the
    # actual live request; its historical token never becomes UI authority.
    view = replace(view, checkpoint_token=token, revision=current.revision)
    if service.resume(run_id).checkpoint.content_sha256 != token:
        raise ReferenceCheckpointConflictError('The preparation changed while opening this page')
    return {'view': view, 'checkpoint': source, 'values': values,
        'component': component, 'phase': requested_phase, 'canonical_phase': phase, 'checkpoint_token': token}


def _binding(values, *, component, phase, reviewer_id):
    serial = dict(values)
    if 'roi_export' in serial:
        serial['roi_export'] = serial['roi_export'].content_sha256
    return ('component_preparation_revision_v1', typed_checksum_sha256({
        'component': component, 'phase': phase, 'reviewer_id': reviewer_id, 'values': serial}))


def _validate(service, current, start, component, phase, values):
    from .reference_service import (_purification_policy_from_mapping,
        _purification_sampling_policy_from_mapping, _background_policy_from_mapping)
    if type(values) is not dict:
        raise ValueError('Preparation values must be a mapping')
    required = {
        'signal_dark_measurements': {'signal_dark_selections', 'purification_policy'},
        'extraction_qc': {'extraction_dispositions'},
        'fluorophore_consensus': {'consensus_policy', 'roi_review', 'qc_policy'},
        'af_reference_rois': {'control_set_assignments'},
        'positive_reference_rois': {'roi_export', 'positive_roi_ids', 'background_roi_ids',
            'positive_sampling_policy', 'background_sampling_policy', 'background_policy'}}[phase]
    if set(values) != required:
        raise ValueError('Preparation values differ from this page')
    if phase == 'signal_dark_measurements':
        _purification_policy_from_mapping(values['purification_policy'])
        choices = tuple(SelectedIdentifier.from_mapping(item) for item in values['signal_dark_selections'])
        signal = [item.identifier for item in choices if item.role == 'signal_measurement']
        dark = [item.identifier for item in choices if item.role == 'dark_measurement']
        if (len(signal) != 1 or not dark or len(signal) + len(dark) != len(choices)
                or len({item.identifier for item in choices}) != len(choices)
                or any(item.identifier not in current.measurement_ids for item in choices)):
            raise ValueError('Choose one signal band and distinct dark bands')
    elif phase == 'positive_reference_rois':
        from .reference_rois import AcceptedReferenceRoiSelection
        from .reference_service_control_preparation import accepted_preparation
        binding = service._one_artifact(current.run_id, start, 'reference_control_files', 'reference-control-files').record
        input_id = binding.positive_reference_input_ids[current.fluorophores.index(component)]
        for name, role in (('positive_roi_ids', 'positive'), ('background_roi_ids', 'background')):
            identities = values[name]
            if type(identities) is not list or not identities or len(set(identities)) != len(identities):
                raise ValueError('Choose distinct tissue and background regions')
            for identity in identities:
                selected = AcceptedReferenceRoiSelection(values['roi_export'], identity)
                if selected.roi.roi_role != role or selected.entry.image.reference_input_id != input_id:
                    raise ValueError('Regions must belong to the selected component image')
        for name in ('positive_sampling_policy', 'background_sampling_policy'):
            _purification_sampling_policy_from_mapping(values[name])
        background = _background_policy_from_mapping(values['background_policy'])
        source, _ = _saved(service, current, start, component, phase)
        prepared = accepted_preparation(service, source, start)
        if prepared is None or prepared.record.plan.background_policy != background:
            raise ValueError('AF background settings require a source preparation revision')
    elif phase == 'extraction_qc':
        choices = tuple(SelectedIdentifier.from_mapping(item) for item in values['extraction_dispositions'])
        results = service._live_roi_phase_artifact(current, start, 'purifications', component,
            'signal_dark_measurements').record
        expected = {item.positive.selection.roi_id for item in results}
        if (len(choices) != len(expected) or {item.identifier for item in choices} != expected
                or any(item.role not in {'consensus_included_roi', 'consensus_excluded_roi'} for item in choices)):
            raise ValueError('Review each saved extraction region exactly once')
    elif phase == 'fluorophore_consensus':
        from .purification import ConsensusPolicy, build_fluorophore_consensus
        from .reference_qc import SpectrumMatrixQcPolicy
        from .reference_service import _CONSENSUS_POLICY_VERSION, _MATRIX_QC_POLICY_VERSION
        from .reference_service_result_operations import reviewed_consensus_inclusions, validate_consensus_retention
        policy = ConsensusPolicy(policy_version=_CONSENSUS_POLICY_VERSION, **values['consensus_policy'])
        SpectrumMatrixQcPolicy(policy_version=_MATRIX_QC_POLICY_VERSION,
            authority_sha256=typed_checksum_sha256(values['qc_policy']), **values['qc_policy'])
        purifications, decisions = service._result_operations._reviewed_extraction_for_consensus(
            current.run_id, current, start, component)
        inclusions = reviewed_consensus_inclusions(decisions, values['roi_review'],
            checkpoint_sha256=current.content_sha256, reviewer_id='preparation validation')
        candidate = build_fluorophore_consensus(component, component, purifications, inclusions, policy)
        validate_consensus_retention(candidate, values['roi_review'])
    elif phase == 'af_reference_rois':
        from .reference_service_control_preparation import accepted_preparation, _source_set_id, control_set_id
        source, _ = _saved(service, current, start, component, phase)
        prepared = accepted_preparation(service, source, start)
        allowed = {_source_set_id(prepared, group) for group in prepared.record.control_sets}
        allowed.update(control_set_id(prepared.content_sha256, index) for index in range(len(prepared.record.control_sets)))
        positive = service._live_roi_phase_artifact(current, start, 'roi_selection_plan', component,
            'positive_reference_rois').record
        choices = values['control_set_assignments']
        if (type(choices) is not dict or set(choices) != {item.roi_id for item in positive.selections}
                or any(value not in allowed for value in choices.values())):
            raise ValueError('Assign an accepted AF set to each stained region')


def _apply(service, run_id, token, phase, values, reviewer_id):
    if phase == 'positive_reference_rois':
        return service.prepare_stained_reference(run_id, token, reviewer_id=reviewer_id,
            **{**values, 'positive_roi_ids': tuple(values['positive_roi_ids']),
               'background_roi_ids': tuple(values['background_roi_ids'])})
    if phase == 'af_reference_rois':
        return service.assign_shared_control_sets(run_id, token, reviewer_id=reviewer_id,
            assignments=values['control_set_assignments'])
    if phase == 'fluorophore_consensus':
        return service.accept_live_fluorophore_consensus(run_id, token, reviewer_id=reviewer_id, **values)
    key = 'signal_dark_selections' if phase == 'signal_dark_measurements' else 'extraction_dispositions'
    return service.advance_reference_purification_from_configuration(run_id, token, reviewer_id=reviewer_id,
        **{**values, key: tuple(SelectedIdentifier.from_mapping(item) for item in values[key])})


def revise_component_preparation(service, run_id, token, *, component, phase, reviewer_id, values):
    phase = ALIASES.get(phase, phase)
    if phase not in PHASES:
        raise ValueError('Unsupported component preparation page')
    if type(reviewer_id) is not str or not reviewer_id or reviewer_id != reviewer_id.strip():
        raise ValueError('Reviewer must be nonempty literal text')
    current, start = service._store._load_service_checkpoint(run_id)
    binding = _binding(values, component=component, phase=phase, reviewer_id=reviewer_id)
    if current.content_sha256 != token:
        revisions = [(index, item) for index, item in enumerate(current.transition_log)
            if item.kind is ReferenceTransitionKind.PREPARATION_REVISION
            and item.source_checkpoint_sha256 == token and item.affected_fluorophore == component]
        if len(revisions) != 1:
            raise ReferenceCheckpointConflictError('This preparation submission is stale')
        index, revision = revisions[0]
        saved = service._artifacts.load(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=token, kind='component_preparation_revision', subject_id=component).record
        if saved != binding:
            raise ReferenceCheckpointConflictError('Preparation retry choices differ')
        following = current.transition_log[index + 1:]
        accepted_options = {'accept_' + phase}
        if phase == 'af_reference_rois':
            accepted_options.add('accept_background_reference_rois')
        decisions = {item.content_sha256: item for item in current.decisions}
        for item in following:
            if (item.kind not in {ReferenceTransitionKind.PROPOSAL, ReferenceTransitionKind.DECISION,
                                 ReferenceTransitionKind.PUBLICATION}
                    or json.loads(item.source_state_json)['current_fluorophore'] != component
                    or item.kind is ReferenceTransitionKind.DECISION
                    and decisions[item.decision_record_sha256].response.option_value not in accepted_options
                    or item.kind is ReferenceTransitionKind.PUBLICATION and phase != 'fluorophore_consensus'):
                raise ReferenceCheckpointConflictError('The preparation revision was superseded')
        target_token = current.content_sha256 if not following else following[0].source_checkpoint_sha256
        return _apply(service, run_id, target_token, phase, values, reviewer_id)
    source, previous = _saved(service, current, start, component, phase)
    _validate(service, current, start, component, phase, values)
    editable = {key: value for key, value in values.items() if key != 'qc_policy'}
    if editable == previous:
        return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(current, start))
    revised = revise_component_preparation_checkpoint(current, fluorophore=component,
        checkpoint=ReferenceCheckpointKind(phase), evidence=source.request.evidence)
    service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
        authority_checkpoint_sha256=token, kind='component_preparation_revision', subject_id=component, record=binding)
    revised, _ = service._store._compare_and_swap_service(token, revised, start)
    return _apply(service, run_id, revised.content_sha256, phase, values, reviewer_id)
