"""Exact classifier review tables shared by pending review and accepted Results."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from collections.abc import Mapping
from mif_pipeline.cell_analysis.result_plots import _csv_output


def _recipe_fields(value, prefix=""):
    if isinstance(value, Mapping) and value:
        for key, item in value.items():
            yield from _recipe_fields(item, f"{prefix}.{key}" if prefix else key)
    else:
        from ._shared import _export_json
        yield prefix, _export_json(value)


def classifier_metadata_tables(record_set, plan_set, details):
    """Use exact selected fit metadata; counts remain pixels/patches, not regions."""
    plans = {plan.request.classifier_id: plan for plan in plan_set.plans}
    allocations, recipes, classes, images, candidates = [], [], [], [], []
    for record in record_set.classifier_records:
        request = plans[record.classifier_id].request
        row = details.get(record.classifier_id, {})
        classifier_name = (PurePosixPath(record.native_artifact_project_relative_path).stem
                           if record.native_artifact_project_relative_path else '')
        for entry in request.entries:
            allocations.append((record.classifier_id, entry.subject_id, entry.sample_id,
                                entry.project_entry_id, entry.split, classifier_name))
        configuration = request.model_configuration
        selection = row.get('model_selection')
        if selection:
            configuration = configuration['candidates'][selection['selected_candidate_index']]
            for candidate in selection['candidate_scores']:
                for fold, score in enumerate(candidate['fold_scores'], 1):
                    candidates.append((record.classifier_id, candidate['candidate_index'] + 1,
                        candidate['candidate_index'] == selection['selected_candidate_index'],
                        candidate['selection_score'], fold, score))
        recipe = {'model': configuration, 'channels': request.channel_requirements,
                  'features': request.feature_requirements, 'random_seed': request.random_seed}
        if request.classifier_kind.value == 'secondary_structure':
            recipe['eligible_parent_classes'] = request.eligible_parent_classes
        recipes.extend((record.classifier_id, field, json.dumps(value, ensure_ascii=False))
                       for field, value in _recipe_fields(recipe))
        observations = row.get('training_observations') or {}
        unit = ('cells' if request.classifier_kind.value == 'supervised_phenotype' else
                'patches' if 'kronos' in configuration.get('configuration_schema', '') else 'pixels')
        for item in observations.get('classes', ()):
            classes.append((record.classifier_id, item['class_id'], item['name'], item['count'], unit))
        for item in observations.get('images', ()):
            images.append((record.classifier_id, item['entry_id'], item['sample_id'], item['subject_id'], item['count'], unit))
    outputs = [
        _csv_output('classifier-allocation', 'Subject allocation',
                    ('classifier', 'subject_id', 'sample_id', 'project_entry_id', 'split', 'classifier_name'), allocations),
        _csv_output('classifier-recipe', 'Saved model recipe', ('classifier', 'setting', 'value_json'), recipes),
    ]
    for identity, title, columns, values in (
        ('classifier-fitting-classes', 'Fitting contributions by class', ('classifier','class_id','class_name','count','unit'), classes),
        ('classifier-fitting-images', 'Fitting contributions by image', ('classifier','project_entry_id','sample_id','subject_id','count','unit'), images),
        ('classifier-cross-validation', 'Cross-validation candidates', ('classifier','candidate','selected','selection_score','fold','fold_score'), candidates),
    ):
        if values:
            outputs.append(_csv_output(identity, title, columns, values))
    scores = []
    for record in record_set.classifier_records:
        for metric in record.metrics:
            for scope, identity, values in (("pooled", "", metric.pooled),
                    *(("subject", item.scope_id, item.metrics) for item in metric.per_subject),
                    *(("image", item.scope_id, item.metrics) for item in metric.per_project_entry)):
                scores.extend((record.classifier_id, metric.split, scope, identity, name, value)
                              for name, value in values.items())
    if scores:
        outputs.append(_csv_output('classifier-evaluation-scores', 'Recorded evaluation scores',
            ('classifier','split','scope','scope_id','metric','value'), scores))
    return tuple(outputs)


def classifier_location_table(record_set, plan_set, details=None, reference_votes=None):
    """Keep original held-out majority-region predictions and level-0 bounds together."""
    entries = {(plan.request.classifier_id, entry.project_entry_id): entry
               for plan in plan_set.plans for entry in plan.request.entries}
    rows = []
    for record in record_set.classifier_records:
        labels = {item['class_id']: item['name'] for item in
                  ((details or {}).get(record.classifier_id, {}).get('training_observations') or {}).get('classes', ())}
        for metric in record.metrics:
            if metric.pixel_confusion is None:
                continue
            for item in metric.pixel_confusion.observations:
                entry = entries[(record.classifier_id, item.project_entry_id)]
                vote = (reference_votes or {}).get(record.classifier_id, {}).get(
                    (metric.split, *item.identity))
                agreement = ('', '', '', '', '')
                if vote is not None:
                    counts, ignored, unit = vote
                    total = sum(counts.values())
                    agreement = (counts[item.truth_class_id] / total, total, ignored, unit,
                                 json.dumps(counts, sort_keys=True, separators=(',', ':')))
                rows.append((record.classifier_id, metric.split, item.subject_id, entry.sample_id,
                    item.project_entry_id, item.annotation_uuid, item.truth_class_id, item.predicted_class_id,
                    labels.get(item.truth_class_id, item.truth_class_id), labels.get(item.predicted_class_id, item.predicted_class_id),
                    item.normalized_roi_sha256, *(item.roi_bounds[key] for key in ('x','y','width','height','z','t')),
                    *agreement))
    from .supervised_review import supervised_evaluation_tables
    cell_tables = supervised_evaluation_tables(record_set, plan_set, reference_votes or {})
    if not rows:
        return cell_tables
    return (*cell_tables, _csv_output('classifier-evaluation-locations', 'Evaluated reference locations',
        ('classifier','split','subject_id','sample_id','project_entry_id','annotation_uuid','reference_class','predicted_class',
         'reference_name','predicted_name','normalized_roi_sha256','x','y','width','height','z','t',
         'reference_agreement','declared_sample_count','ignored_sample_count','sample_unit',
         'declared_label_counts_json'), rows),)


def load_classifier_reference_votes(record, material, provider):
    """Read the original evaluated votes; never reapply the current model or image."""
    import hashlib
    from mif_pipeline.qupath_runtime._held_out_evaluation_request import _expected_runtime_exchange_ref
    from mif_pipeline.qupath_runtime.held_out_evaluation import (
        decode_qupath_held_out_evaluation_request, decode_qupath_held_out_evaluation_result)

    supervised = record.classifier_kind.value == 'supervised_phenotype'
    metrics = {metric.split: metric for metric in record.metrics
               if supervised or (metric.pixel_confusion is not None and metric.pixel_confusion.observations)}
    if not metrics:
        return {}
    retained = provider.project_authority._retained
    exchange = retained.bucket('exchange')
    votes = {}
    for evidence in record.evaluation_evidence:
        metric = metrics.get(evidence['split'])
        if metric is None:
            continue
        scope = {'workspace_storage_scope_sha256': material.workspace_storage_scope_sha256,
                 'active_mutation_run_sha256': material.active_mutation_run_sha256,
                 'role': 'classifier_held_out_evaluation',
                 'fit_material_sha256': material.fit_material_sha256,
                 'request_identity_sha256': evidence['request_identity_sha256']}
        request_ref = _expected_runtime_exchange_ref(
            authority_layout_sha256=retained.layout.layout_sha256, scope=scope, direction='request')
        result_ref = _expected_runtime_exchange_ref(
            authority_layout_sha256=retained.layout.layout_sha256, scope=scope, direction='result')
        request = decode_qupath_held_out_evaluation_request(exchange.read_member(request_ref))
        result_bytes = exchange.read_member(result_ref)
        if hashlib.sha256(result_bytes).hexdigest() != evidence['result_sha256']:
            raise ValueError('The retained evaluation result differs from this classifier review.')
        result = decode_qupath_held_out_evaluation_result(result_bytes, request=request)
        if supervised:
            from .supervised_review import supervised_reference_rows
            votes.update(supervised_reference_rows(request, result))
            continue
        unit = {'qupath-native-classification-grid/v1': 'pixels',
                'kronos-complete-patch-grid/v1': 'patches'}[request.pixel_sampling_method]
        for prediction in result.predictions:
            identity = (prediction['project_entry_id'], prediction['subject_id'], prediction['annotation_uuid'])
            votes[(metric.split, *identity)] = (dict(prediction['declared_label_counts']),
                                               prediction['ignored_sample_count'], unit)
    return votes


def load_classifier_review_evidence(record_set, plan_set, dependencies):
    """Reopen small retained CV and reference counts once on a review cache miss."""
    from .cross_validation import QuPathGroupedCVConfiguration, compact_selection_summary
    records = {record.classifier_id: record for record in record_set.classifier_records}
    reports, votes = {}, {}
    for plan in plan_set.plans:
        record = records[plan.request.classifier_id]
        grouped_cv = plan.request.model_configuration.get('configuration_schema') == 'qupath-grouped-cross-validation/v1'
        if not grouped_cv and not any(record.classifier_kind.value == 'supervised_phenotype'
                or (metric.pixel_confusion is not None and metric.pixel_confusion.observations)
                for metric in record.metrics):
            continue
        provider = dependencies.training.project_provider
        material = provider.load_native_classifier_fit_material(
            record.native_source_project_identity_sha256, record.fit_material_sha256,
            workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
        votes[record.classifier_id] = load_classifier_reference_votes(record, material, provider)
        if grouped_cv:
            if material.model_selection_json is None:
                raise ValueError('The saved classifier has no cross-validation selection report.')
            report = json.loads(material.model_selection_json)
            search = QuPathGroupedCVConfiguration.from_mapping(plan.request.model_configuration)
            compact_selection_summary(report, plan.split_subject_ids['training'], plan.request.required_classes,
                                      len(search.candidates), search.folds, plan.request.random_seed)
            reports[record.classifier_id] = report
    return reports, votes


def classifier_cv_tables(record_set, plan_set, reports):
    """Expose original out-of-fold counts separately from independent evaluation."""
    from mif_pipeline.compartment_analysis.classifier_qc import classifier_metrics_from_confusion
    plans = {plan.request.classifier_id: plan for plan in plan_set.plans}
    parameters, folds, counts, locations = [], [], [], []
    for record in record_set.classifier_records:
        report = reports.get(record.classifier_id)
        if report is None:
            continue
        supervised = getattr(record.classifier_kind, 'value', record.classifier_kind) == 'supervised_phenotype'
        request = plans[record.classifier_id].request
        selected = report['selected_candidate_index']
        for candidate, configuration in enumerate(request.model_configuration['candidates']):
            parameters.extend((record.classifier_id, candidate + 1, candidate == selected, field,
                               json.dumps(value, ensure_ascii=False))
                              for field, value in _recipe_fields(configuration))
        folds.extend((record.classifier_id, subject, fold + 1)
                     for subject, fold in report['folds_by_subject'].items())
        configuration = request.model_configuration['candidates'][selected]
        unit = 'cells' if supervised else 'patches' if 'kronos' in configuration.get('configuration_schema', '') else 'pixels'
        classes = report['classes']
        matrices = report['candidates'][selected]['subject_confusions']
        references = report.get('reference_observations')
        if references is not None:
            entries = {entry.project_entry_id: entry for entry in request.entries}
            for observation in references['rows']:
                votes = observation['declared_label_counts']
                total = sum(votes.values())
                truth = observation['reference_class']
                locations.append((record.classifier_id, 'cross_validation', selected + 1,
                    observation['fold'] + 1, observation['subject_id'],
                    entries[observation['project_entry_id']].sample_id, observation['project_entry_id'],
                    observation['annotation_uuid'], truth, observation['normalized_roi_sha256'],
                    *(observation['roi_bounds'][key] for key in ('x','y','width','height','z','t')),
                    votes[truth] / total if total else None, total, references['sample_unit'],
                    json.dumps(dict(votes), sort_keys=True, separators=(',', ':')), references['membership'],
                    PurePosixPath(record.native_artifact_project_relative_path).stem
                    if report.get('fold_resources') else ''))
        pooled = tuple(tuple(sum(matrix[i][j] for matrix in matrices.values()) for j in range(len(classes)))
                       for i in range(len(classes)))
        kind = getattr(record.classifier_kind, 'value', record.classifier_kind)
        label = ('Supervised cell classifier' if supervised else
                 'Primary classifier' if kind == 'primary_compartment' else 'Secondary classifier')
        label += f' · candidate {selected + 1}'
        for subject, matrix in (('', pooled), *matrices.items()):
            metrics = classifier_metrics_from_confusion(matrix)
            for reference, values in zip(classes, matrix, strict=True):
                total = sum(values)
                for predicted, count in zip(classes, values, strict=True):
                    counts.append((record.classifier_id, 'cross_validation', len(matrices) if not subject else 1,
                        reference, predicted, count, total, count / total if total else None, subject, unit, label,
                        *(metrics[name] for name in ('accuracy','macro_precision','macro_recall','macro_f1')),
                        selected + 1, report['folds_by_subject'][subject] + 1 if subject else '',
                        'tuning_not_independent_test'))
    if not counts:
        return ()
    location_output = (_csv_output('classifier-cv-reference-locations', 'Out-of-fold reference locations',
        ('classifier','split','candidate','fold','subject_id','sample_id','project_entry_id','annotation_uuid',
         'reference_class','normalized_roi_sha256','x','y','width','height','z','t','reference_agreement',
         'declared_sample_count','sample_unit','declared_label_counts_json','sample_membership','classifier_name'), locations),) if locations else ()
    return (*location_output,
        _csv_output('classifier-cv-parameters', 'Cross-validation candidate settings',
                    ('classifier','candidate','selected','setting','value_json'), parameters),
        _csv_output('classifier-cv-folds', 'Cross-validation subject folds', ('classifier','subject_id','fold'), folds),
        _csv_output('classifier-cv-confusion-data', 'Selected candidate out-of-fold counts',
            ('classifier','split','subject_count','reference_class','predicted_class','count','reference_total',
             'fraction_of_reference_class','subject_id','observation_unit','classifier_label','accuracy',
             'macro_precision','macro_recall','macro_f1','candidate','fold','interpretation'), counts),
    )


def classifier_confusion_table(record_set):
    """Shared exact saved evaluation counts for pending and accepted review."""
    from ._shared import ClassifierKind, EvaluationIntent
    rows = []
    records = record_set.classifier_records
    for index, record in enumerate(records, 1):
        if record.evaluation_intent is EvaluationIntent.TRAINING_ONLY or record.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            continue
        label = 'Primary classifier' if record.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT else 'Secondary classifier'
        if len(records) > 1:
            label += f' {index}'
        for metric in record.metrics:
            confusion = metric.pixel_confusion
            if confusion is None:
                continue
            by_subject = {item.scope_id: item.metrics for item in metric.per_subject}
            for subject, matrix in (('', confusion.pooled), *confusion.per_subject.items()):
                values = metric.pooled if not subject else by_subject[subject]
                for reference, counts in zip(confusion.class_ids, matrix, strict=True):
                    total = sum(counts)
                    for predicted, count in zip(confusion.class_ids, counts, strict=True):
                        rows.append((record.classifier_id, metric.split, len(confusion.per_subject) if not subject else 1,
                            reference, predicted, count, total, count / total if total else None, subject,
                            confusion.observation_unit, label,
                            *(values[name] for name in ('accuracy','macro_precision','macro_recall','macro_f1'))))
    if not rows:
        return ()
    return (_csv_output('classifier-confusion-data', 'Classifier evaluation',
        ('classifier','split','subject_count','reference_class','predicted_class','count','reference_total',
         'fraction_of_reference_class','subject_id','observation_unit','classifier_label','accuracy',
         'macro_precision','macro_recall','macro_f1'), rows),)
