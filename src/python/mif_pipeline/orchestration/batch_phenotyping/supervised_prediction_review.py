"""Read-only counts and exact image examples from a pending native prediction."""
import math


class PredictionReviewIndex:
    """Index one immutable prediction for exact counts and bounded score pages.

    Per-class orders use descending score and ascending cell ID for ties. A
    range is a contiguous slice in each class, so requesting any visible page
    allocates only that page's dictionaries. The saved prediction rows remain
    unchanged and are never sent wholesale to the interface.
    """

    def __init__(self, rows, class_ids):
        import numpy as np
        self.rows = rows
        self.class_ids = tuple(class_ids)
        self.orders = {}
        self.scores = {}
        for label in self.class_ids:
            order = np.asarray(sorted((i for i, row in enumerate(rows) if row['class_id'] == label),
                key=lambda i: (-rows[i]['confidence'], rows[i]['cell_id'])), dtype=np.int64)
            scores = np.asarray([-rows[i]['confidence'] for i in order], dtype=np.float64)
            order.flags.writeable = scores.flags.writeable = False
            self.orders[label], self.scores[label] = order, scores
        self.all_scores = np.sort(np.fromiter((r['confidence'] for r in rows), dtype=float, count=len(rows)))
        self.all_scores.flags.writeable = False
        self._summary_cache = None

    @staticmethod
    def settings(options):
        """Validate an inclusive score range and the bounded page coordinates."""
        result = dict(options)
        for name, default in (('cutoff', 0.), ('lower', 0.), ('upper', 1.)):
            value = result.get(name, default)
            if isinstance(value, bool):
                raise ValueError('Enter scores between zero and one.')
            value = float(value)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('Enter scores between zero and one.')
            result[name] = value
        if result['lower'] > result['upper']:
            raise ValueError('The first inspection score must not exceed the last.')
        for name, default in (('offset', 0), ('page_size', 100)):
            value = result.get(name, default)
            if type(value) is not int or value < (0 if name == 'offset' else 1):
                raise ValueError('Choose a valid prediction page.')
            result[name] = value
        if result['page_size'] > 200:
            raise ValueError('Prediction pages contain at most 200 cells.')
        return result

    def page(self, options):
        """Return exact scoped totals, coverage and one requested score-sorted page."""
        import numpy as np
        settings = self.settings(options)
        cutoff, lower, upper = (settings[k] for k in ('cutoff', 'lower', 'upper'))
        label = settings.get('class_id')
        if label is not None and label not in self.class_ids:
            raise ValueError('Choose a class from this prediction.')
        counts, spans = [], []
        for name in self.class_ids:
            scores = self.scores[name]
            assigned = int(np.searchsorted(scores, -cutoff, side='right'))
            counts.append(dict(class_id=name, total=len(scores), assigned=assigned,
                               unclassified=len(scores)-assigned))
            if label is None or label == name:
                start = int(np.searchsorted(scores, -upper, side='left'))
                stop = int(np.searchsorted(scores, -lower, side='right'))
                if settings.get('unclassified_only', False):
                    start = max(start, assigned)
                spans.append((name, min(start, stop), stop))
        total = sum(stop-start for _, start, stop in spans)
        skip, remaining = settings['offset'], settings['page_size']
        page = []
        for name, start, stop in spans:
            count = stop-start
            if skip >= count:
                skip -= count
                continue
            begin = start+skip
            for index in self.orders[name][begin:min(stop, begin+remaining)]:
                row = self.rows[int(index)]
                page.append(dict(row, outcome='Unclassified' if row['confidence'] < cutoff else row['class_id']))
            # A page may cross class boundaries; its remaining capacity is exact.
            remaining = settings['page_size']-len(page)
            skip = 0
            if remaining == 0:
                break
        assigned = sum(item['assigned'] for item in counts)
        scoped = next((item for item in counts if item['class_id'] == label),
            dict(total=len(self.rows), assigned=assigned, unclassified=len(self.rows)-assigned))
        key = (cutoff, label)
        if self._summary_cache is not None and self._summary_cache[0] == key:
            summary = self._summary_cache[1]
        else:
            scores = self.all_scores if label is None else -self.scores[label][::-1]
            unique = np.unique(scores)
            points = np.unique(np.append(unique if len(unique) <= 512 else np.linspace(0, 1, 201), [0., cutoff, 1.]))
            coverage = [[float(t), float(1-np.searchsorted(scores, t, side='left')/len(scores))]
                        for t in points] if len(scores) else []
            summary = dict(total=len(self.rows), assigned=assigned, unclassified=len(self.rows)-assigned,
                cutoff=cutoff, classes=counts, coverage=coverage, selected_class=label,
                scope_total=scoped['total'], scope_assigned=scoped['assigned'], scope_unclassified=scoped['unclassified'])
            self._summary_cache = (key, summary)
        return dict(summary=summary, rows=page, matching_total=total, offset=settings['offset'],
                    lower=lower, upper=upper, page_size=settings['page_size'])


def summarize_predictions(rows, class_ids, cutoff, class_id=None, *, unclassified_only=False,
                          lower=0., upper=1., offset=0, page_size=100):
    """Return the current exact summary and a bounded page for image review."""
    result = PredictionReviewIndex(rows, class_ids).page(dict(cutoff=cutoff, class_id=class_id,
        unclassified_only=unclassified_only, lower=lower, upper=upper, offset=offset, page_size=page_size))
    return dict(result['summary'], examples=result['rows'], matching_total=result['matching_total'],
                offset=result['offset'], lower=lower, upper=upper)


def render_prediction_review(snapshot, context, dependencies, options, *, prediction_index=None):
    """The service authenticates the current pending request/head before entry."""
    from ..batch_measurement_artifacts import decode_committed_measurements_result
    from ..batch_training import decode_current_committed_classifier_record_set
    from mif_pipeline.cell_analysis.result_plots import _csv_output

    if options.get('prediction_sha256', context['sha256']) != context['sha256']:
        from .supervised_cohort_review import prediction_owners, open_prediction, SCORE_KINDS
        owner = next((item for item in prediction_owners(snapshot,context,dependencies)
                      if item['sha256'] == options['prediction_sha256']), None)
        if owner is None:
            raise ValueError('Select a cell from a current compatible prediction.')
        prediction = open_prediction(owner,dependencies)
        selected_context = dict(context,sha256=owner['sha256'],byte_count=owner['byte_count'],
                                score_kind=SCORE_KINDS[owner['record'].classifier_engine],class_count=len(prediction.class_ids))
        outputs,resolved = render_prediction_review(snapshot,selected_context,dependencies,
            dict(options,cutoff=owner['cutoff']), prediction_index=(prediction,PredictionReviewIndex(prediction.rows,prediction.class_ids)))
        resolved['prediction_request'] = dict(options)
        return outputs,resolved

    if prediction_index is None:
        stored = dependencies.phenotyping.supervised_prediction_store.reopen(context['sha256'], context['byte_count'])
        prediction = stored.prediction
        index = PredictionReviewIndex(prediction.rows, prediction.class_ids)
    else:
        prediction, index = prediction_index
    checked = index.settings(options)
    authorities = snapshot.committed_artifact_authorities
    authority = next((a for a in authorities if a.artifact_ref.artifact_ref_sha256 ==
                      prediction.measurement_result_ref_sha256), None)
    if authority is None or authority.artifact_ref.artifact_kind != 'measurements_result':
        raise ValueError('The pending prediction’s accepted measurement is no longer current.')
    record = None
    for candidate in authorities:
        if candidate.artifact_ref.artifact_kind == 'classifier_record_set':
            records = decode_current_committed_classifier_record_set(candidate,
                store=dependencies.phenotyping.classifier_record_set_store,
                state_store=dependencies.runtime.state_store)
            record = next((r for r in records.classifier_records
                           if r.classifier_record_sha256 == prediction.classifier_record_sha256), record)
    if record is None:
        raise ValueError('The pending prediction’s classifier is no longer current.')
    cutoff = checked['cutoff']
    if context['score_kind'] == 'native_svm_pairwise_vote_fraction' and context['class_count'] == 2 and cutoff != 0:
        raise ValueError('Binary SVM vote agreement cannot reject uncertain cells.')
    page = index.page(checked)
    summary = dict(page['summary'], examples=page['rows'], matching_total=page['matching_total'])
    summary.update(prediction_sha256=context['sha256'], sample_id=prediction.sample_id,
                   score_kind=context['score_kind'], class_count=len(prediction.class_ids))
    resolved = dict(prediction_review=summary, prediction_request=dict(options))
    fields = ('class_id','total','assigned','unclassified')
    outputs = [_csv_output('supervised-prediction-counts', 'Assignment counts at selected cutoff', fields,
                           [tuple(r[k] for k in fields) for r in summary['classes']]),
               _csv_output('supervised-prediction-coverage','Score cutoff and retained fraction',
                           ('cutoff','assigned_fraction'),summary['coverage']),
               _csv_output('supervised-prediction-examples','Prediction score page',
                           ('cell_id','predicted_class','score','outcome','cutoff'),
                           [(r['cell_id'],r['class_id'],r['confidence'],r['outcome'],cutoff) for r in summary['examples']])]
    if options.get('cell_id') is not None:
        from bisect import bisect_left
        position = bisect_left(prediction.rows, options['cell_id'], key=lambda r: r['cell_id'])
        row = prediction.rows[position] if position < len(prediction.rows) else None
        if (row is None or row['cell_id'] != options['cell_id']
                or not checked['lower'] <= row['confidence'] <= checked['upper']
                or (options.get('class_id') is not None and row['class_id'] != options['class_id'])
                or (options.get('unclassified_only') and row['confidence'] >= cutoff)):
            raise ValueError('Select a cell from the current inspection range.')
        row = dict(row, outcome='Unclassified' if row['confidence'] < cutoff else row['class_id'])
        measurement = decode_committed_measurements_result(authority)
        gallery, appearance = _render_cell(snapshot, authority, measurement, record, row, options)
        outputs = [*gallery, *outputs]
        resolved.update(appearance)
    return tuple(outputs), resolved


def _render_cell(snapshot, authority, measurement, record, row, options):
    from .marker_review_gallery import measurement_cells
    from ..saved_cell_gallery import gallery_resources, load_cell_crops, display_options
    from ..saved_cell_figures import _MeasuredCells
    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery
    image = next((a.artifact_ref for a in snapshot.committed_artifact_authorities
                  if a.artifact_ref.artifact_kind == 'processed_image'
                  and a.artifact_ref.sha256 == measurement.canonical_image_sha256
                  and a.artifact_ref.scope.sample_id == measurement.sample_id), None)
    if image is None:
        raise ValueError('The measured image for this prediction is unavailable.')
    settings = {'channel_display':'grayscale','merged_panel':False,**options,'cell_ids':[row['cell_id']]}
    if 'channels' not in settings:
        names = {feature.measurement_name for feature in record.owner_payload.feature_schema}
        ids = {f.channel_id for f in measurement.measurement_plan.features if f.measurement in names}
        settings['channels'] = [i for i,c in enumerate(measurement.canonical_channels) if c['channel_id'] in ids][:6]
        if not settings['channels']:
            settings['channels'] = list(range(min(3,len(measurement.canonical_channels))))
    label = f"Predicted: {row['class_id']} · score {row['confidence']:.6g}\nAt cutoff: {row['outcome']}"
    with gallery_resources(snapshot, authority):
        with measurement_cells(measurement) as (view, component, scope):
            cells = _MeasuredCells(view, component, measurement, scope)
            columns = ('cell_id','x_level0','y_level0','cell_geometry','nucleus_geometry')
            found = {r['cell_id']:r for batch in cells.lookup_ids([row['cell_id']],columns=columns) for r in batch.to_pylist()}
            if row['cell_id'] not in found:
                raise ValueError('The predicted cell is absent from its accepted measurement geometry.')
            data = load_cell_crops(image.locator,found,(row['cell_id'],),(label,),settings,
                image_id=measurement.canonical_image_id,image_sha256=measurement.canonical_image_sha256,
                pixel_size_um=(scope['x_um_per_px'],scope['y_um_per_px']))
        resolved = display_options(data,settings)
        resolved.update(available_channels=[dict(index=i,label=c['name']) for i,c in enumerate(measurement.canonical_channels)],
            image_label=measurement.sample_id, sample_id=measurement.sample_id, review_caption=label.replace('\n',' · '),
            label_field='prediction',include_pixels=True,compact=True,export_basename='supervised-predicted-cell')
        outputs = render_cell_gallery(data,resolved)
    return tuple(('supervised-prediction-cell/'+identity.split('/',1)[-1],title,fmt,payload)
                 for identity,title,fmt,payload in outputs), resolved
