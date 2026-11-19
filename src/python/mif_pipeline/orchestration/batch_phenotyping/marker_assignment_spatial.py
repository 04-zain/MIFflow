"""Read draft phenotype membership through the shared bounded tissue viewer."""
from contextlib import contextmanager, ExitStack
import sqlite3

import pyarrow as pa

from ._common import canonical_bytes, digest
from .marker_combination_preview import resolve_preview_inputs, preview_from_population, assignment_image_counts
from .marker_combinations import validate_combination_response
from .marker_spatial_review import MarkerFieldReader, MarkerPopulationCells
from .marker_review_gallery import measurement_cells
from ..saved_cell_figures import _MeasuredCells
from ..measurement_spatial_review import _ImageMeasurementCells

_MEMBERSHIP = 'Assigned phenotype'
_COLUMN = 'measurement:' + _MEMBERSHIP


class AssignmentCells:
    """Attach exact draft membership to bounded accepted cell batches."""
    identity_columns = ('sample_id', 'project_entry_id', 'slide_id', 'section_id')

    def __init__(self, dataset, connection, scope, selection):
        self.dataset, self.connection = dataset, connection
        self.identity = scope
        self._scope_key, self.selection = canonical_bytes(scope).decode(), selection
        self.scope = dataset.scope
        self.schema = pa.schema([*(field for field in dataset.schema if field.name not in self.identity_columns),
                                 pa.field(_COLUMN, pa.float64()),
                                 *(pa.field(name, pa.string()) for name in self.identity_columns)])

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys(('cell_id', *(c for c in columns
            if c != _COLUMN and c not in self.identity_columns))))
        for batch in getattr(self.dataset, method)(*args, columns=fields, **kwargs):
            ids = batch.column('cell_id').to_pylist()
            membership = {}
            for offset in range(0, len(ids), 400):
                chunk = ids[offset:offset + 400]
                marks = ','.join('?' for _ in chunk)
                rows = self.connection.execute(
                    f'SELECT c.cell_id,c.pattern,p.phenotype FROM cells c JOIN patterns p ON p.pattern=c.pattern '
                    f'WHERE c.scope=? AND c.cell_id IN ({marks})', (self._scope_key, *chunk))
                membership.update((identity, (pattern, phenotype)) for identity,pattern,phenotype in rows)
            if len(membership) != len(ids):
                raise ValueError('The assignment preview differs from the included measured cells. Refresh the review.')
            kind, value = self.selection
            values = [float(kind == 'all' or membership[identity][0 if kind == 'pattern' else 1] == value)
                      for identity in ids]
            batch = batch.append_column(_COLUMN, pa.array(values))
            for name, value in zip(self.identity_columns, self.identity, strict=True):
                if name in columns:
                    batch = batch.append_column(name, pa.array([value] * batch.num_rows, type=pa.string()))
            yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


class MarkerAssignmentFieldReader(MarkerFieldReader):
    def __init__(self, authorities, measurements, population, snapshot, path, selection):
        self.path, self.assignment_selection = path, selection
        self.image_counts = assignment_image_counts(path, selection)
        super().__init__(authorities, measurements, population, snapshot)
        self.features = {_MEMBERSHIP: None}

    @contextmanager
    def _selected_cells(self, authority):
        with ExitStack() as stack:
            connection = sqlite3.connect(self.path)
            stack.callback(connection.close)
            datasets = []
            for measurement in self.measurements:
                view, component, scope = stack.enter_context(measurement_cells(measurement))
                cells = MarkerPopulationCells([_MeasuredCells(view, component, measurement, scope)], self.selection)
                identity = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id, measurement.section_id)
                datasets.append((identity, AssignmentCells(cells, connection, identity, self.assignment_selection)))
            yield _ImageMeasurementCells(datasets)

    def _outlines(self, dataset, name, lower, upper, bounds):
        result = super()._outlines(dataset, name, lower, upper, bounds)
        ids = [row['cell_id'] for row in result['visible_cells']]
        labels = {}
        with sqlite3.connect(self.path) as connection:
            for measurement in self.measurements:
                scope = canonical_bytes((measurement.sample_id, measurement.project_entry_id,
                                         measurement.slide_id, measurement.section_id)).decode()
                for offset in range(0, len(ids), 400):
                    chunk = ids[offset:offset + 400]
                    marks = ','.join('?' for _ in chunk)
                    rows = connection.execute(f'SELECT c.cell_id,c.pattern,p.phenotype FROM cells c '
                        f'JOIN patterns p ON p.pattern=c.pattern WHERE c.scope=? AND c.cell_id IN ({marks})', (scope, *chunk))
                    identity_prefix = (measurement.sample_id, measurement.project_entry_id,
                                       measurement.slide_id, measurement.section_id)
                    labels.update(((*identity_prefix, identity), (pattern, phenotype))
                                  for identity, pattern, phenotype in rows)
        for row in result['visible_cells']:
            identity = (*(row[key] for key in AssignmentCells.identity_columns), row['cell_id'])
            row['pattern_id'], row['phenotype'] = labels[identity]
        return result


def assignment_field(provider, snapshot, target, options):
    """Called under the existing read lock after preparing the exact preview."""
    from pathlib import Path
    from ..saved_cell_gallery import gallery_resources
    definition = validate_combination_response(target.stage_result,
        {'phenotype_definition': options['phenotype_definition']})['phenotype_definition']
    cached = provider._marker_combination_population
    key = (snapshot.head.sha256, target.interaction_request_id, target.interaction_context_sha256)
    if cached is None or cached[0] != key:
        raise ValueError('Refresh the phenotype assignment preview before inspecting tissue.')
    _, directory, markers, total = cached
    path = Path(directory.name) / 'patterns.sqlite'
    prior = provider._marker_gate_examples
    preview_key = ('marker-combinations', snapshot.head.sha256, target.interaction_context_sha256, digest(definition))
    preview = (prior[1] if prior is not None and prior[0] == preview_key
               else preview_from_population(path, markers, total, definition))
    image = tuple(options.get('image_identity', ()))
    if image not in {tuple(row[k] for k in ('sample_id','project_entry_id','slide_id')) for row in preview['images']}:
        raise ValueError('Choose an image in the included phenotype population.')
    kind, value = options.get('selection_kind'), options.get('selection_value')
    if kind == 'phenotype':
        if value not in preview['observed_patterns']['assigned_by_phenotype']:
            raise ValueError('Choose a phenotype in the current ordered rules.')
    elif kind == 'pattern':
        with sqlite3.connect(path) as connection:
            if connection.execute('SELECT 1 FROM patterns WHERE pattern=?', (value,)).fetchone() is None:
                raise ValueError('Choose an observed marker pattern.')
    elif kind != 'all':
        raise ValueError('Choose a phenotype or observed marker pattern.')
    gate, measurements, authorities = resolve_preview_inputs(snapshot, target.stage_result.resume_context)
    selected = [(m,a) for m,a in zip(measurements, authorities, strict=True)
                if (m.sample_id,m.project_entry_id,m.slide_id) == image]
    measurements, authorities = tuple(zip(*selected))
    reader_key = ('marker-assignments', *key, image, digest(definition), kind, value)
    with gallery_resources(snapshot, authorities[0]):
        previous = provider._measurement_field
        if previous is None or previous[0] != reader_key:
            provider._measurement_field = None
            if previous is not None:
                previous[1].close()
            reader = MarkerAssignmentFieldReader(authorities, measurements,
                gate.marker_settings['phenotyping_population'], snapshot, path, (kind, value))
            provider._measurement_field = (reader_key, reader)
        else:
            reader = previous[1]
        display = {**options, 'measurement': _MEMBERSHIP, 'lower': 1., 'upper': 1.}
        if not display.get('channels'):
            from ome_types import from_xml
            from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
            from .marker_settings import marker_feature_candidates
            inventory = resolve_component_identity_inventory(from_xml(reader.image.attributes['mif_pipeline']['ome_xml']), 0)
            nuclear = inventory.nuclear_component_index if inventory is not None else None
            rule = next((r for r in definition['composite_rules'] if r['phenotype_id'] == value), None)
            wanted = list(rule['marker_states']) if kind == 'phenotype' and rule else list(markers[:2])
            if kind == 'pattern':
                wanted = [marker for marker,state in zip(markers,value,strict=True) if state == '1'] or list(markers[:2])
            _, offered = marker_feature_candidates(measurements)
            channels = {feature['marker_id']: next(i for i,c in enumerate(measurements[0].canonical_channels)
                        if c['channel_id'] == feature['channel_id']) for feature in offered}
            display['channels'] = list(dict.fromkeys(([nuclear] if nuclear is not None else []) +
                                                     [channels[marker] for marker in wanted]))
        bundle = reader.read(authorities[0], snapshot, display)
        bundle['metadata'].update(available_channels=[dict(index=i,label=c['label']) for i,c in enumerate(reader.image.channels)],
                                  marker_ids=list(markers), selection_kind=kind, selection_value=value,
                                  image_uri=reader.image.path.as_uri(), population_label='included cells',
                                  cohort_images=reader.image_counts)
        return bundle
