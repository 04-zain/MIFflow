"""Present retained held-out cell predictions without reapplying the classifier."""

from collections import Counter

from mif_pipeline.cell_analysis.result_plots import _csv_output
from ._shared import ClassifierKind, EvaluationIntent


def supervised_reference_rows(request, result):
    """Join the already decoded exchange by image and stable cell identity."""
    truth = {(entry['project_entry_id'], cell['cell_id']): (entry, cell)
             for entry in request.evaluation_entries for cell in entry['evaluation_cells']}
    rows = {}
    for prediction in result.predictions:
        entry, cell = truth[prediction['project_entry_id'], prediction['cell_id']]
        geometry = cell['cell_geometry']
        point = geometry['coordinates'] if geometry['type'] == 'Point' else ('', '')
        rows[entry['split'], entry['project_entry_id'], cell['cell_id']] = {
            'split': entry['split'], 'subject_id': entry['subject_id'],
            'project_entry_id': entry['project_entry_id'], 'cell_id': cell['cell_id'],
            'reference_class': cell['expected_class_id'],
            'predicted_class': prediction['predicted_class_id'], 'x': point[0], 'y': point[1],
            'cell_geometry_sha256': cell['cell_geometry_sha256'],
            **{key: entry[key] for key in ('source_snapshot_sha256',
                'segmentation_generation_id', 'measurement_generation_id')},
        }
    return rows


def supervised_evaluation_tables(record_set, plan_set, evidence):
    """Export one reference-by-prediction cell matrix per saved evaluation scope."""
    plans = {plan.request.classifier_id: plan for plan in plan_set.plans}
    counts, cells = [], []
    cell_fields = ('split', 'subject_id', 'project_entry_id', 'cell_id',
                   'reference_class', 'predicted_class', 'x', 'y', 'cell_geometry_sha256',
                   'source_snapshot_sha256', 'segmentation_generation_id', 'measurement_generation_id')
    for record in record_set.classifier_records:
        if (record.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE
                or record.evaluation_intent is EvaluationIntent.TRAINING_ONLY):
            continue
        rows = tuple(evidence.get(record.classifier_id, {}).values())
        if not rows:
            continue
        request = plans[record.classifier_id].request
        entries = {entry.project_entry_id: entry for entry in request.entries}
        classes = request.required_classes
        for metric in record.metrics:
            selected = tuple(row for row in rows if row['split'] == metric.split)
            if not selected:
                continue
            subjects = tuple(item.scope_id for item in metric.per_subject)
            for subject, metrics in (('', metric.pooled), *((item.scope_id, item.metrics)
                                                          for item in metric.per_subject)):
                scoped = tuple(row for row in selected if not subject or row['subject_id'] == subject)
                matrix = Counter((row['reference_class'], row['predicted_class']) for row in scoped)
                for reference in classes:
                    total = sum(matrix[reference, predicted] for predicted in classes)
                    for predicted in classes:
                        count = matrix[reference, predicted]
                        counts.append((record.classifier_id, metric.split, len(subjects) if not subject else 1,
                            reference, predicted, count, total, count / total if total else None,
                            subject, 'cells', 'Supervised cell classifier',
                            *(metrics[name] for name in ('accuracy', 'macro_precision', 'macro_recall', 'macro_f1'))))
            cells.extend((record.classifier_id, entries[row['project_entry_id']].sample_id,
                          *(row[key] for key in cell_fields)) for row in selected)
    if not counts:
        return ()
    return (
        _csv_output('classifier-confusion-data', 'Cell classifier evaluation',
            ('classifier', 'split', 'subject_count', 'reference_class', 'predicted_class', 'count',
             'reference_total', 'fraction_of_reference_class', 'subject_id', 'observation_unit',
             'classifier_label', 'accuracy', 'macro_precision', 'macro_recall', 'macro_f1'), counts),
        _csv_output('classifier-evaluation-cells', 'Evaluated reference cells',
                    ('classifier', 'sample_id', *cell_fields), cells),
    )
