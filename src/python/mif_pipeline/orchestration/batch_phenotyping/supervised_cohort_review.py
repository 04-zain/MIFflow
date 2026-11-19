"""Read current deployment predictions without changing models, cutoffs or evaluations."""
import numpy as np
from mif_pipeline.pipeline_core import StageStatus

from .supervised_prediction_review import PredictionReviewIndex

SCORE_KINDS = {
    'qupath-object-classifier': 'native_tree_vote_fraction',
    'qupath-object-svm-classifier': 'native_svm_pairwise_vote_fraction',
    'qupath-object-logistic-regression-classifier': 'native_logistic_ovr_normalized_score',
}
IDENTITY_FIELDS = ('sample_id', 'project_entry_id', 'slide_id', 'section_id')


def saved_prediction_context(snapshot, dependencies, artifact_ref_sha256):
    """Reopen one exact current accepted assignment without creating an interaction."""
    from ..batch_training import decode_current_committed_classifier_record_set
    authority = next((a for a in snapshot.committed_artifact_authorities
        if a.artifact_ref.artifact_kind == 'supervised_phenotype_result'
        and a.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
    if (authority is None or snapshot.head.node_status_by_id.get(authority.artifact_ref.producer_node_id)
            is not StageStatus.COMPLETED):
        raise ValueError('The saved cell assignment is no longer current.')
    identity = authority.artifact_ref.owning_identity
    prediction = dependencies.phenotyping.supervised_prediction_store.reopen(
        identity['prediction_sha256'], identity['prediction_byte_count']).prediction
    if (prediction.classifier_record_sha256 != identity['classifier_record_sha256']
            or any(getattr(prediction, key) != identity[key] for key in IDENTITY_FIELDS)):
        raise ValueError('The saved assignment differs from its retained prediction.')
    records = []
    for owner in snapshot.committed_artifact_authorities:
        if owner.artifact_ref.artifact_kind == 'classifier_record_set':
            records.extend(decode_current_committed_classifier_record_set(owner,
                store=dependencies.phenotyping.classifier_record_set_store,
                state_store=dependencies.runtime.state_store).classifier_records)
    model = next((r for r in records if r.classifier_record_sha256 == prediction.classifier_record_sha256), None)
    if (model is None or model.classifier_engine not in SCORE_KINDS
            or tuple(model.owner_payload.class_ids) != prediction.class_ids):
        raise ValueError('The saved prediction does not match a current fitted classifier.')
    sample = next(s for s in snapshot.manifest.enabled_samples if s.sample_id == prediction.sample_id)
    return dict(phase='prediction_review', sha256=identity['prediction_sha256'],
        byte_count=identity['prediction_byte_count'], score_kind=SCORE_KINDS[model.classifier_engine],
        class_count=len(prediction.class_ids), confidence_threshold=identity['confidence_threshold'],
        image_identity=[prediction.sample_id, prediction.project_entry_id, prediction.slide_id],
        processing_group_id=sample.validation_domain_id, artifact_ref_sha256=artifact_ref_sha256)


def prediction_review_context(snapshot, dependencies, interaction_request_id=None, artifact_ref_sha256=None, *, cached_index=None):
    """Choose exactly one existing pending or accepted prediction owner."""
    if bool(interaction_request_id) == bool(artifact_ref_sha256):
        raise ValueError('Choose one current assignment review or saved assignment result.')
    if artifact_ref_sha256:
        if (cached_index is not None and cached_index[0][:3] ==
                (snapshot.head.sha256, artifact_ref_sha256, 'cohort') and 'context' in cached_index[1]):
            if not any(a.artifact_ref.artifact_kind == 'supervised_phenotype_result'
                    and a.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256
                    and snapshot.head.node_status_by_id.get(a.artifact_ref.producer_node_id) is StageStatus.COMPLETED
                    for a in snapshot.committed_artifact_authorities):
                raise ValueError('The saved cell assignment is no longer current.')
            return cached_index[1]['context'], artifact_ref_sha256
        return saved_prediction_context(snapshot, dependencies, artifact_ref_sha256), artifact_ref_sha256
    from ..batch_interactions import project_batch_interaction_target
    if interaction_request_id not in snapshot.head.pending_interaction_ids:
        raise ValueError('The assignment review is no longer pending.')
    publication = dependencies.runtime.state_store.reopen_interaction_request_publication(
        interaction_request_id, expected_head_sha256=snapshot.head.sha256)
    target = project_batch_interaction_target(publication)
    context = target.stage_result.resume_context
    if (target.interaction_request_id != interaction_request_id
            or target.stage_result.interaction_reason != 'supervised_classifier_review_required'
            or context.get('phase') != 'prediction_review'):
        raise ValueError('Choose the current supervised assignment review.')
    return context, target.interaction_context_sha256


def prediction_owners(snapshot, context, dependencies):
    """Inventory current predictions with matching score semantics and measurements."""
    from ..batch_training import decode_current_committed_classifier_record_set
    current = {a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    records = {}
    for authority in current.values():
        if authority.artifact_ref.artifact_kind == 'classifier_record_set':
            record_set = decode_current_committed_classifier_record_set(authority,
                store=dependencies.phenotyping.classifier_record_set_store,
                state_store=dependencies.runtime.state_store)
            records.update((r.classifier_record_sha256,r) for r in record_set.classifier_records
                           if r.classifier_engine in SCORE_KINDS)
    active = dependencies.phenotyping.supervised_prediction_store.reopen(context['sha256'], context['byte_count']).prediction
    record = records.get(active.classifier_record_sha256)
    if (record is None or SCORE_KINDS[record.classifier_engine] != context['score_kind']
            or tuple(record.owner_payload.class_ids) != active.class_ids
            or len(active.class_ids) != context['class_count']):
        raise ValueError('The pending prediction does not match a current classifier.')
    signature = (SCORE_KINDS[record.classifier_engine], tuple(record.owner_payload.class_ids),
                 record.owner_payload.feature_schema)
    accepted_active = next((a.artifact_ref.owning_identity for a in current.values()
        if a.artifact_ref.artifact_kind == 'supervised_phenotype_result'
        and a.artifact_ref.owning_identity['prediction_sha256'] == context['sha256']), None)
    candidates = [dict(sha256=context['sha256'], byte_count=context['byte_count'], pending=accepted_active is None,
        cutoff=None if accepted_active is None else accepted_active['confidence_threshold'], classifier_record_sha256=active.classifier_record_sha256,
        measurement_ref=active.measurement_result_ref_sha256,
        _prediction=active,
        **{key:getattr(active,key) for key in IDENTITY_FIELDS})]
    sample_groups = {sample.sample_id:sample.validation_domain_id for sample in snapshot.manifest.enabled_samples}
    group_models = {sample_groups[active.sample_id]:{active.classifier_record_sha256}}
    for authority in current.values():
        if authority.artifact_ref.artifact_kind != 'supervised_phenotype_result':
            continue
        identity = authority.artifact_ref.owning_identity
        group_models.setdefault(sample_groups[identity['sample_id']],set()).add(identity['classifier_record_sha256'])
        if (identity['prediction_sha256'] == context['sha256']
                or all(identity[key] == getattr(active,key) for key in IDENTITY_FIELDS)):
            continue
        model = records.get(identity['classifier_record_sha256'])
        if model is None or (SCORE_KINDS[model.classifier_engine], tuple(model.owner_payload.class_ids),
                             model.owner_payload.feature_schema) != signature:
            continue
        measurements = [current[ref] for ref in identity['source_ref_sha256s'] if ref in current
                        and current[ref].artifact_ref.artifact_kind == 'measurements_result'
                        and all(current[ref].artifact_ref.owning_identity[key] == identity[key] for key in IDENTITY_FIELDS)]
        if len(measurements) != 1:
            continue
        candidates.append(dict(sha256=identity['prediction_sha256'], byte_count=identity['prediction_byte_count'],
            pending=False, cutoff=identity['confidence_threshold'], classifier_record_sha256=identity['classifier_record_sha256'],
            measurement_ref=measurements[0].artifact_ref.artifact_ref_sha256,
            **{key:identity[key] for key in IDENTITY_FIELDS}))
    for node in snapshot.plan.nodes:
        if (node.kind != 'supervised_phenotype_inference'
                or snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.NEEDS_INTERACTION):
            continue
        attempts = snapshot.head.attempt_ids_by_node.get(node.node_id, ())
        if not attempts:
            continue
        terminal = dependencies.runtime.state_store.load_attempt(
            snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]])
        pending = terminal.stage_result.resume_context
        if (terminal.stage_result.interaction_reason != 'supervised_classifier_review_required'
                or pending.get('phase') != 'prediction_review' or pending['sha256'] == context['sha256']):
            continue
        prediction = dependencies.phenotyping.supervised_prediction_store.reopen(pending['sha256'],pending['byte_count']).prediction
        group_models.setdefault(sample_groups[prediction.sample_id],set()).add(prediction.classifier_record_sha256)
        model = records.get(prediction.classifier_record_sha256)
        if model is None or (SCORE_KINDS[model.classifier_engine],tuple(model.owner_payload.class_ids),
                            model.owner_payload.feature_schema) != signature:
            continue
        candidates.append(dict(sha256=pending['sha256'],byte_count=pending['byte_count'],pending=True,
            cutoff=None,classifier_record_sha256=prediction.classifier_record_sha256,
            measurement_ref=prediction.measurement_result_ref_sha256,_prediction=prediction,
            **{key:getattr(prediction,key) for key in IDENTITY_FIELDS}))
    for owner in candidates:
        measurement = current.get(owner['measurement_ref'])
        if measurement is None or measurement.artifact_ref.artifact_kind != 'measurements_result':
            raise ValueError('A prediction measurement is no longer current.')
        if any(measurement.artifact_ref.owning_identity[key] != owner[key] for key in IDENTITY_FIELDS):
            raise ValueError('The prediction and measurement image identities differ.')
        owner['measurement_authority'] = measurement
        owner['record'] = records[owner['classifier_record_sha256']]
        owner['processing_group_id'] = sample_groups[owner['sample_id']]
        owner['group_assignment_available'] = len(group_models[owner['processing_group_id']]) == 1
    sample_order = {sample.sample_id:i for i,sample in enumerate(snapshot.manifest.enabled_samples)}
    node_order = {node.node_id:i for i,node in enumerate(snapshot.plan.nodes)}
    return sorted(candidates, key=lambda owner:(sample_order[owner['sample_id']],
        node_order.get(owner['measurement_authority'].artifact_ref.producer_node_id, len(node_order))))


def open_prediction(owner, dependencies):
    prediction = owner.pop('_prediction', None)
    if prediction is None:
        prediction = dependencies.phenotyping.supervised_prediction_store.reopen(owner['sha256'], owner['byte_count']).prediction
    if (prediction.measurement_result_ref_sha256 != owner['measurement_ref']
            or prediction.classifier_record_sha256 != owner['classifier_record_sha256']
            or prediction.class_ids != tuple(owner['record'].owner_payload.class_ids)
            or any(getattr(prediction,key) != owner[key] for key in IDENTITY_FIELDS)):
        raise ValueError('The saved prediction differs from its current image and model owners.')
    return prediction


def reference_splits(owner):
    payload = owner['record'].owner_payload
    return {item.cell_id:split for split in ('training','validation','test')
            for item in getattr(payload, split+'_cells') if item.project_entry_id == owner['project_entry_id']}


def owner_metadata(owner, cutoff):
    record = owner['record']
    return dict(prediction_sha256=owner['sha256'], measurement_ref=owner['measurement_ref'],
        classifier_record_sha256=owner['classifier_record_sha256'], classifier_id=record.classifier_id,
        validation_domain_id=record.validation_domain_id, evaluation_intent=record.evaluation_intent.value,
        processing_group_id=owner['processing_group_id'],
        score_kind=SCORE_KINDS[record.classifier_engine], cutoff=cutoff, pending=owner['pending'],
        **{key:owner[key] for key in IDENTITY_FIELDS})


def preview_cutoff(owner, options):
    scope = options.get('plot_scope', 'image')
    selected = (scope == 'group' and owner['processing_group_id'] == options.get('processing_group_id')
        or scope == 'image' and tuple(owner[key] for key in IDENTITY_FIELDS[:3]) == tuple(options.get('image_identity', ())))
    return options['cutoff'] if selected or owner['pending'] else owner['cutoff']


def cohort_prediction_page(owners, context, dependencies, options):
    """Exact assignment coverage and selected-image rows; no evaluation metrics."""
    checked = PredictionReviewIndex.settings(options)
    scope = options.get('plot_scope','image')
    if scope not in {'all','image','group'}:
        raise ValueError('Choose All images, a processing group or a selected image.')
    groups = tuple(dict.fromkeys(owner['processing_group_id'] for owner in owners))
    if scope == 'group' and options.get('processing_group_id') not in groups:
        raise ValueError('Choose a processing group in the current prediction review.')
    visible = [owner for owner in owners if scope != 'group' or owner['processing_group_id'] == options['processing_group_id']]
    if scope == 'group' and (any(not owner.get('group_assignment_available', True) for owner in visible)
            or len({owner['classifier_record_sha256'] for owner in visible}) != 1):
        raise ValueError('This processing group uses more than one fitted classifier. Review its images separately.')
    if context['score_kind'] == 'native_svm_pairwise_vote_fraction' and context['class_count'] == 2 and checked['cutoff'] != 0:
        raise ValueError('Binary SVM vote agreement cannot reject uncertain cells.')
    keys = list(dict.fromkeys(tuple(owner[key] for key in IDENTITY_FIELDS[:3]) for owner in visible))
    active = tuple(options.get('image_identity', keys[0]))
    if active not in keys and scope == 'group':
        active = keys[0]
    if active not in keys:
        raise ValueError('Choose an image in the current prediction review.')
    checked['image_identity'] = list(active)
    checked['plot_scope'] = scope
    class_ids = tuple(owners[0]['record'].owner_payload.class_ids)
    images = {key:dict(key=list(key),label=' · '.join(dict.fromkeys((key[0],key[2]))),
        included_cells=0,matching_cells=0,owners=[]) for key in keys}
    points = np.unique(np.append(np.linspace(0.,1.,201),
        [preview_cutoff(owner, checked) for owner in visible]))
    coverage_counts = np.zeros(len(points), dtype=np.int64)
    classes = {label:dict(class_id=label,total=0,assigned=0,unclassified=0) for label in class_ids}
    active_rows = []
    selected_class = checked.get('class_id')
    for owner in visible:
        prediction = open_prediction(owner, dependencies)
        image = tuple(owner[key] for key in IDENTITY_FIELDS[:3])
        cutoff = preview_cutoff(owner, checked)
        index = PredictionReviewIndex(prediction.rows, class_ids)
        part = index.page(dict(checked,cutoff=cutoff,offset=0,page_size=1))
        images[image]['included_cells'] += len(prediction.rows)
        images[image]['matching_cells'] += part['matching_total']
        images[image]['owners'].append(owner_metadata(owner,cutoff))
        if scope in {'all','group'} or image == active:
            for row in part['summary']['classes']:
                for field in ('total','assigned','unclassified'):
                    classes[row['class_id']][field] += row[field]
            scores = index.all_scores if selected_class is None else -index.scores[selected_class][::-1]
            coverage_counts += len(scores)-np.searchsorted(scores,points,side='left')
        if image == active:
            splits = reference_splits(owner)
            for row in prediction.rows:
                rejected = row['confidence'] < cutoff
                if checked.get('unclassified_only') and not rejected:
                    continue
                active_rows.append(dict(row, **{key:owner[key] for key in IDENTITY_FIELDS},
                    prediction_sha256=owner['sha256'], reference_split=splits.get(row['cell_id'],'deployment'),
                    resolved_outcome='Unclassified' if rejected else row['class_id'], cutoff=cutoff))
    index = PredictionReviewIndex(tuple(active_rows),class_ids)
    page = index.page(dict(checked,cutoff=0.,unclassified_only=False))
    for row in page['rows']:
        row['outcome'] = row.pop('resolved_outcome')
    total = sum(row['total'] for row in classes.values())
    assigned = sum(row['assigned'] for row in classes.values())
    scoped = classes[selected_class] if selected_class is not None else dict(total=total,assigned=assigned,unclassified=total-assigned)
    summary = dict(total=total,assigned=assigned,unclassified=total-assigned,classes=list(classes.values()),
        scope_total=scoped['total'],scope_assigned=scoped['assigned'],scope_unclassified=scoped['unclassified'],
        selected_class=selected_class,cutoff=checked['cutoff'],score_kind=context['score_kind'],class_count=len(class_ids),
        coverage=[[float(score),float(count/scoped['total'])] for score,count in zip(points,coverage_counts)] if scoped['total'] else [],
        sample_id='All images' if scope == 'all' else 'Processing group · '+options['processing_group_id'] if scope == 'group' else images[active]['label'],
        population='deployment_predictions', interpretation='Assignment score coverage; not an evaluation metric.')
    page.update(summary=summary,cohort_images=list(images.values()),active_image=list(active),
        processing_groups=[dict(id=group,label=group,assignment_available=all(owner.get('group_assignment_available',True)
            for owner in owners if owner['processing_group_id']==group) and len({owner['classifier_record_sha256']
            for owner in owners if owner['processing_group_id']==group})==1) for group in groups],
        pending_owner=next(owner_metadata(owner,checked['cutoff']) for owner in owners if owner['sha256']==context['sha256']))
    return page,index


def cached_prediction_page(metadata, index, options):
    page = index.page(dict(options,cutoff=0.,unclassified_only=False))
    for row in page['rows']:
        row['outcome'] = row.pop('resolved_outcome')
    return dict(page, **metadata)
