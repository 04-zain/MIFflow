"""Attach exact prediction membership to the accepted bounded image field."""
from contextlib import contextmanager, ExitStack
import pyarrow as pa

from ..measurement_spatial_review import MeasurementFieldReader, _ImageMeasurementCells
from ..saved_cell_figures import _selected_cells
from ..batch_measurement_artifacts import decode_committed_measurements_result
from .supervised_cohort_review import open_prediction, reference_splits, IDENTITY_FIELDS, preview_cutoff

MEASUREMENT = 'Predicted assignment'
COLUMN = 'measurement:' + MEASUREMENT


class PredictionMembershipCells:
    def __init__(self, dataset, rows):
        self.dataset, self.rows = dataset, rows
        self.scope = dataset.scope
        self.schema = dataset.schema.append(pa.field(COLUMN,pa.float64()))

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys(('cell_id',*(name for name in columns if name != COLUMN))))
        for batch in getattr(self.dataset,method)(*args,columns=fields,**kwargs):
            ids = batch.column('cell_id').to_pylist()
            batch = batch.filter(pa.array([identity in self.rows for identity in ids]))
            if not batch.num_rows:
                continue
            ids = batch.column('cell_id').to_pylist()
            batch = batch.append_column(COLUMN,pa.array([float(self.rows[identity]['selected']) for identity in ids]))
            yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches',columns)

    def query(self,bounds,*,columns,predicate='intersects'):
        yield from self._batches('query',columns,bounds,predicate=predicate)


class SupervisedAssignmentFieldReader(MeasurementFieldReader):
    def __init__(self,owners,dependencies,snapshot,settings):
        self.owners = owners
        self.membership = {}
        self.measurements = [decode_committed_measurements_result(owner['measurement_authority']) for owner in owners]
        first = self.measurements[0]
        frame = (first.sample_id,first.project_entry_id,first.slide_id,first.canonical_image_id,first.canonical_image_sha256)
        if any((m.sample_id,m.project_entry_id,m.slide_id,m.canonical_image_id,m.canonical_image_sha256) != frame for m in self.measurements):
            raise ValueError('Predictions must share the exact selected image frame.')
        for owner in owners:
            prediction = open_prediction(owner,dependencies)
            cutoff = preview_cutoff(owner, settings)
            splits = reference_splits(owner)
            rows = {}
            for row in prediction.rows:
                outcome = 'Unclassified' if row['confidence'] < cutoff else row['class_id']
                selected = (settings['lower'] <= row['confidence'] <= settings['upper']
                    and (settings.get('class_id') is None or row['class_id'] == settings['class_id'])
                    and (not settings.get('unclassified_only') or outcome == 'Unclassified'))
                rows[row['cell_id']] = dict(row,selected=selected,outcome=outcome,
                    reference_split=splits.get(row['cell_id'],'deployment'),prediction_sha256=owner['sha256'])
            self.membership[owner['sha256']] = rows
        self.expected_count = sum(len(rows) for rows in self.membership.values())
        super().__init__(owners[0]['measurement_authority'],snapshot)
        self.features = {MEASUREMENT:None}

    @contextmanager
    def _selected_cells(self,authority):
        with ExitStack() as stack:
            sections = []
            for owner in self.owners:
                dataset = stack.enter_context(_selected_cells(owner['measurement_authority']))
                cells = PredictionMembershipCells(dataset,self.membership[owner['sha256']])
                sections.append((tuple(owner[key] for key in IDENTITY_FIELDS),cells))
            yield _ImageMeasurementCells(sections)

    def _outlines(self,dataset,name,lower,upper,bounds):
        result = super()._outlines(dataset,name,lower,upper,bounds)
        owners = {tuple(owner[key] for key in IDENTITY_FIELDS):owner['sha256'] for owner in self.owners}
        for row in result['visible_cells']:
            owner = owners[tuple(row[key] for key in IDENTITY_FIELDS)]
            row.update(self.membership[owner][row['cell_id']])
        return result

    def read(self,authority,snapshot,options):
        display = dict(options,measurement=MEASUREMENT,lower=1.,upper=1.)
        if not display.get('channels'):
            names = {feature.measurement_name for owner in self.owners for feature in owner['record'].owner_payload.feature_schema}
            ids = {feature.channel_id for measurement in self.measurements
                   for feature in measurement.measurement_plan.features if feature.measurement in names}
            display['channels'] = [i for i,channel in enumerate(self.measurements[0].canonical_channels) if channel['channel_id'] in ids]
            if not display['channels']:
                display['channels'] = list(range(min(3,len(self.image.channels))))
        focus = display.pop('focus_cell',None)
        if focus:
            owner = next((item for item in self.owners if item['sha256'] == focus['prediction_sha256']),None)
            row = self.membership.get(focus['prediction_sha256'],{}).get(focus['cell_id'])
            if owner is None or row is None or not row['selected']:
                raise ValueError('Choose a cell in this image and inspection range.')
            with _selected_cells(owner['measurement_authority']) as cells:
                positions = [r for batch in cells.lookup_ids([row['cell_id']],columns=('cell_id','x_level0','y_level0')) for r in batch.to_pylist()]
            if len(positions) != 1:
                raise ValueError('The predicted cell is absent from its accepted image geometry.')
            x,y = positions[0]['x_level0']*self.pixel_size[0],positions[0]['y_level0']*self.pixel_size[1]
            display['field_um'] = [max(0.,x-100),max(0.,y-100),min(self.extent[1],x+100),min(self.extent[2],y+100)]
        bundle = super().read(authority,snapshot,display)
        if bundle['metadata']['total_measured'] != self.expected_count:
            raise ValueError('The predicted cells do not match their accepted measurement geometry.')
        bundle['metadata'].update(population_label='predicted cells',image_uri=self.image.path.as_uri(),
            available_channels=[dict(index=i,label=channel['label']) for i,channel in enumerate(self.image.channels)])
        return bundle
