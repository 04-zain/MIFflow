"""Project retained shared-AF evidence and exact stained-region assignments."""
from dataclasses import replace
from mif_pipeline.gui.interactions import ControlKind, FormControl


def reference_control_preparation_form(form):
    """Reuse the native form binding without submitting scientific arrays."""
    context = dict(form.context)
    state = context['stage_result']['resume_context']['service_context']['operation_state']
    summary = state['review_summary']
    option = context['selected_option']
    context.update(reference_regions_available=False, reference_region_values={})
    context.pop('back_option', None)
    context.pop('back_label', None)
    if option in {'accept_shared_control_preparation', 'review_control_sets'}:
        review = summary.get('control_preparation') or summary['shared_control_preparation']
        revision = next((name for name in ('retry_shared_control_preparation', 'revise_shared_control')
            if name in state.get('offered_actions', ())), None)
        if option == 'review_control_sets':
            primary = next((control.choices[0] for control in form.controls
                if control.name == 'option_value' and control.choices), None)
            if primary and primary != option:
                context['reference_local_return_option'] = primary
                revision = None
        members = [identity for group in review['sets'] for identity in group['roi_ids']]
        overlapping = len(members) != len(set(members))
        editable = 'review_control_sets' in state.get('offered_actions', ()) and not overlapping
        context['reference_region_workspace'] = {'source_review': True, 'review': review,
            'revision_option': revision, 'composition_enabled': editable,
            'composition_note': ('Saved sets share regions. Edit regions to prepare new, disjoint sets.'
                                 if overlapping else '')}
        controls = tuple(control for control in form.controls if control.name != 'control_sets')
        if editable:
            controls += (FormControl('control_sets', 'AF control sets', ControlKind.OBJECT_SEQUENCE,
                value=[list(row['roi_ids']) for row in review['sets']]),)
        return replace(form, title='Review and combine AF spectra', message='', controls=controls,
            allowed_actions=form.allowed_actions if (editable or review['can_accept']) and
                not (overlapping and option == 'review_control_sets') else (), context=context)

    assignment = summary.get('control_assignment') or summary['shared_control_assignment']
    fields = {f'reference_mapping_{field_index}_{index}_value': identity
        for requirement in state['operation_requirements'] if requirement['option_value'] == option
        for field_index, field in enumerate(requirement['form_fields'])
        if field['name'] == 'control_set_assignments' and field['value_kind'] == 'enum_mapping'
        for index, identity in enumerate(field['mapping_key_choices'])}
    positives = {row['roi_id']: row for row in assignment['positive_regions']}
    source = summary.get('control_preparation') or summary.get('shared_control_preparation', {})
    progress = summary.get('reference_preparation_progress', {})
    stained_filename = next((row['label'] for row in progress.get('components', ())
        if row['name'] == state.get('fluorophore')), '')
    review = dict(assignment, assignment=True, assignment_fields=fields,
        positive_regions=tuple(positives[identity] for identity in fields.values()),
        stained_filename=stained_filename,
        edit_regions_option='reselect_reference_rois' if not progress and 'reselect_reference_rois' in state.get('offered_actions', ()) else None,
        review_sets_option='review_control_sets' if not progress and 'review_control_sets' in state.get('offered_actions', ()) else None)
    # Wavelength metadata describes the same retained source arrays. Never
    # infer it from a stained spectrum or another acquisition.
    if tuple(source.get('measurement_names', ())) == tuple(assignment['measurement_names']):
        review.update({key: source[key] for key in
            ('measurement_wavelengths', 'wavelength_unit', 'measurement_optical_settings') if key in source})
    context['reference_region_workspace'] = review
    labels = dict(context.get('choice_labels', {}))
    set_labels = {row['set_id']: row['label'] for row in assignment['sets']}
    controls = []
    for control in form.controls:
        if control.name in fields:
            identity = fields[control.name]
            labels[control.name] = set_labels
            control = replace(control, label=f"AF control for {positives[identity]['label']}",
                help_text=None, value=assignment.get('default_assignments', {}).get(identity)
                if control.value is None else control.value)
        controls.append(control)
    context['choice_labels'] = labels
    title = 'Assign AF controls' + (f" · {state['fluorophore']}" if state.get('fluorophore') else '')
    return replace(form, title=title,
        message='Assign the AF set used to subtract autofluorescence from each stained region.',
        controls=tuple(controls), context=context)
