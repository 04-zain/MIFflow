"""Read projections and bounded edits of the current marker sequence."""
from collections.abc import Mapping
from mif_pipeline.pipeline_core import StageResult, InteractionReason
from ._common import BatchPhenotypingError, digest


def revision_progress(workflow, marker):
    if 'return_marker_id' in workflow:
        raise BatchPhenotypingError('Finish reviewing the changed marker before changing another confirmed marker.')
    completed = tuple(workflow['completed'])
    ids = [item['marker_rule']['marker_id'] for item in completed]
    if marker not in ids:
        raise BatchPhenotypingError('Choose a confirmed marker in this sequence.')
    index = ids.index(marker)
    return dict(version=workflow['version'], current_marker_id=marker,
        completed=completed[:index], settings_locked=True,
        retained_completed=completed[index+1:], return_marker_id=workflow['current_marker_id'])


def preparation_request(request, marker=None):
    """Project the ordinary setup page; the supplied live request remains authority."""
    source = request.to_mapping()['resume_context']
    if request.interaction_reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
        context = {key:value for key,value in source.items() if key != 'setup_evidence_sha256'}
    elif request.interaction_reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
        workflow = source['marker_workflow']
        settings = workflow['settings']
        offers = source.get('offered_marker_features')
        if offers is None:
            # Older saved requests record exact chosen feature IDs but not alternatives.
            # Expose those identities without inventing additional measurement choices.
            semantics = {review['marker_id']: review['measurement_semantic'] for review in source['marker_reviews']}
            offers = [dict(marker_id=rule['marker_id'],feature_id=rule['feature_id'],
                channel_id=semantics.get(rule['marker_id'],{}).get('channel_id',rule['feature_id']),
                measurement=semantics.get(rule['marker_id'],{}).get('measurement',rule['feature_id']),
                compartment=semantics.get(rule['marker_id'],{}).get('compartment','Saved measurement'))
                for rule in settings['marker_rules']]
        context = dict(schema_version='batch-marker-setup/v1',
            validation_domain_id=source['validation_domain_id'], canonical_marker_ids=source['canonical_marker_ids'],
            measurement_authority_bindings=source['measurement_authority_bindings'],
            offered_marker_features=offers, initial_marker_settings=settings,
            phenotyping_population=settings['phenotyping_population'],
            marker_workflow={key:workflow[key] for key in ('version','current_marker_id','completed',
                'settings_locked','retained_completed','return_marker_id') if key in workflow})
        rules = {rule['marker_id']: rule for rule in settings['marker_rules']}
        context['marker_distributions'] = [dict(marker_id=review['marker_id'],
            feature_id=rules[review['marker_id']]['feature_id'],
            compartment=review['measurement_semantic']['compartment'],
            measurement=review['measurement_semantic']['measurement'],
            pooled={key:review['pooled_review'][key] for key in ('histogram','eligible_count','unknown_count','excluded_count')},
            per_slide=review.get('per_image_distributions', ())) for review in source['marker_reviews']]
    else:
        raise BatchPhenotypingError('This request has no editable marker preparation.')
    workflow = context['marker_workflow']
    selected = marker or workflow['current_marker_id']
    if selected != workflow['current_marker_id']:
        revision_progress(workflow, selected)
    settings = context['initial_marker_settings']
    selected_rules = [rule for rule in settings['marker_rules'] if rule['marker_id'] == selected]
    if len(selected_rules) != 1:
        raise BatchPhenotypingError('The selected marker is absent from this sequence.')
    context['initial_marker_settings'] = {**settings, 'marker_rules': selected_rules}
    context['canonical_marker_ids'] = (selected,)
    context['offered_marker_features'] = tuple(item for item in context['offered_marker_features'] if item['marker_id'] == selected)
    if 'marker_distributions' in context:
        context['marker_distributions'] = tuple(item for item in context['marker_distributions'] if item['marker_id'] == selected)
    # Shared population, scope and marker membership are fixed during a single-marker edit.
    context['marker_workflow'] = dict(version=workflow['version'],current_marker_id=selected,completed=(),settings_locked=True)
    sha = digest(context)
    return StageResult.needs_interaction(stage='marker_gate_review',slide_id=request.slide_id,
        reason=InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED,
        affected_object_ids=request.affected_object_ids,message='Review this marker’s saved preparation.',
        allowed_actions=('define_marker_composite_rules','abort'),
        resume_context={**context,'setup_evidence_sha256':sha},checkpoint_id='marker-setup-'+sha[:48])


def validate_preparation_change(request, settings, marker=None):
    from .marker_settings import validate_marker_setup_response
    projected = preparation_request(request, marker)
    source = request.to_mapping()['resume_context']
    baseline = source.get('initial_marker_settings') or source['marker_workflow']['settings']
    selected = projected.resume_context['marker_workflow']['current_marker_id']
    if not isinstance(settings, Mapping) or set(settings) != set(baseline):
        raise BatchPhenotypingError('Marker preparation settings have invalid fields.')
    if any(digest(settings[key]) != digest(baseline[key]) for key in baseline if key != 'marker_rules'):
        raise BatchPhenotypingError('A marker edit retains the shared population and fitting scope.')
    if len(settings['marker_rules']) != len(baseline['marker_rules']):
        raise BatchPhenotypingError('A marker edit retains the selected marker sequence.')
    for old, new in zip(baseline['marker_rules'], settings['marker_rules'], strict=True):
        if old['marker_id'] != new.get('marker_id') or old['marker_id'] != selected and digest(old) != digest(new):
            raise BatchPhenotypingError('Only the selected marker preparation may change.')
    chosen = [rule for rule in settings['marker_rules'] if rule['marker_id'] == selected]
    normalized = validate_marker_setup_response(projected, {'marker_settings': {**settings,'marker_rules':chosen}})['marker_settings']
    return {**baseline,'marker_rules':[normalized['marker_rules'][0] if rule['marker_id'] == selected else rule for rule in baseline['marker_rules']]}
