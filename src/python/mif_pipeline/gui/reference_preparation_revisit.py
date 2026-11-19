"""Reuse ordinary reference forms for an authority-bound saved phase revisit."""
from dataclasses import replace
from mif_pipeline.gui.interactions import _json_editable_value
from mif_pipeline.pipeline_core import StageResult


def interrupted_preparation_draft_key(form):
    """Match an interrupted live page only while its source evidence is unchanged."""
    import hashlib
    import json
    state = form.context.get('stage_result', {}).get('resume_context', {}).get('service_context', {}).get('operation_state', {})
    phase = state.get('checkpoint')
    evidence_names = {'positive_reference_rois': (),
        'af_reference_rois': ('control_assignment', 'shared_control_assignment', 'saved_positive_regions'),
        'signal_dark_measurements': ('band_review', 'af_control_set_review'),
        'extraction_qc': ('extraction_review',), 'fluorophore_consensus': ('consensus_review',)}
    if phase not in evidence_names or not state.get('fluorophore'):
        return None
    summary = state.get('review_summary', {})
    source = summary.get('control_preparation') or summary.get('shared_control_preparation', {})
    evidence = {key: summary.get(key) for key in evidence_names[phase]}
    evidence['source'] = {key: source.get(key) for key in
        ('input_id', 'preparation_sha256', 'sampled_preparation_sha256')}
    evidence['measurements'] = state.get('measurement_ids')
    evidence['offered_ids'] = state.get('offered_ids')
    if phase == 'positive_reference_rois':
        native = form.context.get('reference_regions_summary') or {}
        if not native.get('snapshot_sha256'):
            return None
        evidence['regions'] = {key: native.get(key) for key in ('snapshot_sha256', 'saved_selection', 'saved_settings')}
    digest = hashlib.sha256(json.dumps(_json_editable_value(evidence), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return ('interrupted-preparation', state.get('run_id'), state['fluorophore'], phase,
            form.context.get('selected_option'), digest)


def component_preparation_form(live_form, inspection):
    from .presenters.reviews import _project_reference
    from .reference_regions import reference_region_form
    original = StageResult.from_mapping(live_form.context['stage_result'])
    owner = original.resume_context['service_context']
    result = replace(original, checkpoint_id=inspection['checkpoint_token'],
        resume_context={**original.resume_context,
            'service_context': {**owner, 'operation_state': inspection['state']}})
    form = _project_reference(result)
    if inspection.get('region_summary') is not None:
        form = reference_region_form(form, inspection['region_summary'])
    values = inspection['values']
    saved = dict(values)
    if 'signal_dark_selections' in values:
        saved['signal_measurement_id'] = next(row['identifier'] for row in values['signal_dark_selections']
            if row['role'] == 'signal_measurement')
        saved['dark_measurement_ids'] = tuple(row['identifier'] for row in values['signal_dark_selections']
            if row['role'] == 'dark_measurement')
    if 'extraction_dispositions' in values:
        for name, role in (('included_roi_ids', 'consensus_included_roi'),
                           ('excluded_roi_ids', 'consensus_excluded_roi')):
            saved[name] = tuple(row['identifier'] for row in values['extraction_dispositions'] if row['role'] == role)
    if 'roi_review' in values:
        saved['consensus_roi_review'] = values['roi_review']
    for field, identity in form.context.get('reference_region_workspace', {}).get('assignment_fields', {}).items():
        saved[field] = values['control_set_assignments'][identity]
    for name, path in form.context.get('reference_settings_paths', {}).items():
        if path[0] not in values:
            continue
        value = values
        for key in path:
            value = value[key]
        saved[name] = value
    constants = dict(form.context.get('reference_settings_constants', {}))
    constants.update({key: values[key] for key in constants if key in values})
    context = {**form.context, 'reference_settings_constants': constants,
        'reference_preparation_revisit': {'component': inspection['component'], 'phase': inspection['phase']},
        'reference_preparation_navigation_options': next((control.choices for control in live_form.controls
            if control.name == 'option_value'), ()),
        'reference_preparation_progress': live_form.context.get('reference_preparation_progress'),
        'reference_component_progress': live_form.context.get('reference_component_progress')}
    if inspection['phase'] == 'control_regions':
        context['reference_control_saved_sets'] = values['control_sets']
    context.pop('back_option', None)
    return replace(form, controls=tuple(replace(control, value=saved.get(control.name, control.value))
        for control in form.controls), context=context)


def component_preparation_values(form, raw_values):
    """Decode by the ordinary phase form, then use the service's phase vocabulary."""
    from .presenters.reviews import _submit_reference
    response = _submit_reference(form, 'accept', raw_values)
    values = {**response['form_values'], **response['injected_configuration']}
    values.pop('reviewer_id', None)
    phase = form.context['reference_preparation_revisit']['phase']
    if phase in {'regions', 'control_regions'}:
        selection = values.pop('reference_region_selection')
        names = ('control_sets', 'background_roi_ids') if phase == 'control_regions' else (
            'positive_roi_ids', 'background_roi_ids')
        values.update({key: selection[key] for key in names})
        if phase == 'control_regions':
            # This page selects sample membership; grouping is edited on the
            # following AF review. Preserve those saved groups when revisiting.
            selected = list(dict.fromkeys(identity for group in values['control_sets'] for identity in group))
            saved = form.context['reference_control_saved_sets']
            groups = [[identity for identity in group if identity in selected] for group in saved]
            known = {identity for group in saved for identity in group}
            added = [identity for identity in selected if identity not in known]
            values['control_sets'] = [group for group in groups if group] + ([added] if added else [])
    elif phase == 'bands':
        values['signal_dark_selections'] = [dict(role='signal_measurement', identifier=values.pop('signal_measurement_id')),
            *(dict(role='dark_measurement', identifier=identity) for identity in values.pop('dark_measurement_ids'))]
    elif phase == 'extraction':
        included, excluded = values.pop('included_roi_ids'), values.pop('excluded_roi_ids')
        order = form.context['stage_result']['resume_context']['service_context']['operation_state']['offered_ids']
        values['extraction_dispositions'] = [dict(role='consensus_included_roi' if identity in included
            else 'consensus_excluded_roi', identifier=identity) for identity in order
            if identity in included or identity in excluded]
    elif phase == 'consensus':
        values['roi_review'] = values.pop('consensus_roi_review')
    return _json_editable_value(values)
