"""Inspect an intensity or Nimbus gate in its accepted phenotyping population."""
from contextlib import contextmanager, ExitStack
import math
import sys

import pyarrow as pa

from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection
from ..measurement_spatial_review import MeasurementFieldReader, _ImageMeasurementCells
from ..saved_cell_figures import _MeasuredCells
from .marker_review_gallery import measurement_cells


class MarkerPopulationCells:
    """Project accepted sections and filter membership before counting or drawing."""

    def __init__(self, datasets, selection):
        self.datasets, self.selection = datasets, selection
        self.schema = datasets[0].schema
        self.scope = getattr(datasets[0], 'scope', None)
        self.identity_columns = getattr(datasets[0], 'identity_columns', ())
        self.column = None if selection['kind'] == 'all_segmented' else 'population:' + selection['kind']

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys((*columns, *([self.column] if self.column else []))))
        for dataset in self.datasets:
            for batch in getattr(dataset, method)(*args, columns=fields, **kwargs):
                if self.column:
                    membership = batch.column(self.column)
                    if self.selection['kind'] == 'detected_tissue':
                        mask = membership.fill_null(False)
                    else:
                        labels = dataset.dictionaries[self.column].labels
                        wanted = {i for i, label in enumerate(labels) if label in self.selection['class_ids']}
                        mask = pa.array([bool(wanted.intersection(values or ())) for values in membership.to_pylist()])
                    batch = batch.filter(mask)
                if batch.num_rows:
                    yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


class MarkerFieldReader(MeasurementFieldReader):
    """Reuse bounded pixels, density counts and outlines for accepted marker inputs."""

    def __init__(self, authorities, measurements, population, snapshot):
        self.measurements = measurements
        first = measurements[0]
        self.selection = resolve_population_selection(population, sample_id=first.sample_id,
            project_entry_id=first.project_entry_id, slide_id=first.slide_id)
        super().__init__(authorities[0], snapshot)

    @contextmanager
    def _section_cells(self):
        with ExitStack() as stack:
            datasets = []
            for measurement in self.measurements:
                view, component, scope = stack.enter_context(measurement_cells(measurement))
                datasets.append((measurement, _MeasuredCells(view, component, measurement, scope)))
            yield datasets

    @contextmanager
    def _selected_cells(self, authority):
        with self._section_cells() as sections:
            yield _ImageMeasurementCells([
                (tuple(getattr(measurement, key) for key in _ImageMeasurementCells.identity_columns),
                 MarkerPopulationCells([dataset], self.selection))
                for measurement, dataset in sections])


class NimbusScoreCells:
    """Join one section's saved scores without conflating repeated cell IDs."""
    identity_columns = ('sample_id', 'project_entry_id', 'slide_id', 'section_id')
    column = 'measurement:Nimbus score'

    def __init__(self, dataset, identity, scores):
        self.dataset, self.identity, self.scores = dataset, identity, scores
        self.scope = getattr(dataset, 'scope', None)
        self.dictionaries = getattr(dataset, 'dictionaries', {})
        self.schema = pa.schema([*dataset.schema, pa.field(self.column, pa.float64()),
                                 *(pa.field(name, pa.string()) for name in self.identity_columns)])

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys(('cell_id', *(name for name in columns
            if name != self.column and name not in self.identity_columns))))
        for batch in getattr(self.dataset, method)(*args, columns=fields, **kwargs):
            if self.column in columns:
                values = [self.scores.get(identity, float('nan')) for identity in batch.column('cell_id').to_pylist()]
                batch = batch.append_column(self.column, pa.array(values, type=pa.float64()))
            for name, value in zip(self.identity_columns, self.identity, strict=True):
                if name in columns:
                    batch = batch.append_column(name, pa.array([value] * batch.num_rows, type=pa.string()))
            yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


class NimbusFieldReader(MarkerFieldReader):
    """Authenticate scores once and retain only the selected image's value join."""
    def __init__(self, authorities, measurements, population, snapshot, marker, provenance):
        from mif_pipeline.cell_analysis.nimbus import load_nimbus_scores
        self._scores = None
        super().__init__(authorities, measurements, population, snapshot)
        identities, populations, images = [], {}, {}
        try:
            with self._section_cells() as sections:
                for measurement, dataset in sections:
                    scope = tuple(getattr(measurement, key) for key in NimbusScoreCells.identity_columns)
                    populations[scope], images[scope] = measurement.segmentation_population_id, measurement.canonical_image_id
                    selected = MarkerPopulationCells([dataset], self.selection)
                    for batch in selected.iter_batches(columns=('cell_id',)):
                        identities.extend((*scope, identity) for identity in batch.column('cell_id').to_pylist())
            loaded = load_nimbus_scores(provenance['path'], expected_identities=identities,
                expected_population_by_scope=populations, expected_image_by_scope=images,
                marker=marker, allow_superset=True, expected_sha256=provenance['sha256'])
        except BaseException:
            self.close()
            raise
        self._scores = {scope: {} for scope in populations}
        for identity, value in zip(identities, loaded.scores, strict=True):
            self._scores[identity[:4]][identity[4]] = float(value)
        self.included_count = len(identities)
        self.known_count = sum(math.isfinite(value) for scores in self._scores.values() for value in scores.values())
        self.score_bytes = sum(sys.getsizeof(scores) + sum(sys.getsizeof(key) + sys.getsizeof(value)
            for key,value in scores.items()) for scores in self._scores.values())
        self.provenance = dict(loaded.provenance)
        self.features = {'Nimbus score': None}

    @contextmanager
    def _selected_cells(self, authority):
        if self._scores is None:
            with super()._selected_cells(authority) as cells:
                yield cells
        else:
            with self._section_cells() as sections:
                datasets = []
                for measurement,dataset in sections:
                    scope = tuple(getattr(measurement, key) for key in NimbusScoreCells.identity_columns)
                    scored = NimbusScoreCells(dataset, scope, self._scores[scope])
                    datasets.append((scope, MarkerPopulationCells([scored], self.selection)))
                yield _ImageMeasurementCells(datasets)

    def read(self, authority, snapshot, options):
        result = super().read(authority, snapshot, options)
        result['metadata'].update(population_label='scored cells', quantity_label='Nimbus score',
            included_cells=self.included_count, scored_cells=self.known_count,
            unknown_scores=self.included_count-self.known_count, score_provenance=self.provenance)
        return result

    def close(self):
        super().close()
        self._scores = None


def marker_score_provenance(marker, options):
    if options.get('source') != 'nimbus':
        return None
    image = tuple(options.get('image_identity', ()))
    review = (next(row['review'] for row in marker['per_slide_reviews']
                   if tuple(row[key] for key in ('sample_id','project_entry_id','slide_id')) == image)
              if marker['fit_scope'] == 'per_slide' else marker['pooled_review'])
    candidate = next((row for row in review['candidates']
                      if row['method'] == 'nimbus' and row['status'] == 'available'), None)
    if candidate is None:
        raise ValueError('Choose an available Nimbus decision before inspecting scores.')
    cutoff = float(options['cutoff'])
    if not math.isfinite(cutoff) or not 0 <= cutoff <= 1:
        raise ValueError('Choose a Nimbus score cutoff between zero and one.')
    return candidate['parameters']['score_provenance']


def marker_field_inputs(snapshot, context, options):
    """Resolve only measurements represented by the current pending marker review."""
    from ..batch_measurement_artifacts import decode_committed_measurements_result
    marker = next((row for row in context['marker_reviews'] if row['marker_id'] == options.get('marker_id')), None)
    if marker is None:
        raise ValueError('Choose a marker in the current review.')
    image = tuple(options.get('image_identity', ()))
    if not any(tuple(row[k] for k in ('sample_id','project_entry_id','slide_id')) == image
               for row in marker['per_image_distributions']):
        raise ValueError('Choose an image represented in this marker review.')
    current = {a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    bindings = [b for b in context['measurement_authority_bindings']
                if tuple(b[k] for k in ('sample_id','project_entry_id','slide_id')) == image]
    authorities = tuple(current[b['measurements_result_ref_sha256']] for b in bindings)
    measurements = tuple(decode_committed_measurements_result(a) for a in authorities)
    if not measurements:
        raise ValueError('This image has no current measurement selection.')
    name = marker['measurement_semantic']['measurement']
    if any(name not in m.selected_measurement_names for m in measurements):
        raise ValueError('This marker measurement is unavailable in the selected image.')
    if len({(m.canonical_image_sha256, m.canonical_image_id) for m in measurements}) != 1:
        raise ValueError('The selected measured sections refer to different images.')
    population = context['marker_workflow']['settings'].get('phenotyping_population')
    return marker, authorities, measurements, population
