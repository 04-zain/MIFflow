"""Inspect immutable cluster assignments through the bounded tissue reader."""
from contextlib import contextmanager, ExitStack

import pyarrow as pa

from ..measurement_spatial_review import MeasurementFieldReader, _ImageMeasurementCells
from ..saved_cell_figures import _MeasuredCells
from .marker_review_gallery import measurement_cells


class ClusterCells:
    """Join spatially queried cells to one image's exact saved assignments."""

    def __init__(self, dataset, assignments, cluster, identity):
        self.dataset, self.assignments, self.cluster, self.identity = dataset, assignments, cluster, identity
        self.column = 'measurement:' + cluster
        self.schema = dataset.schema.append(pa.field(self.column, pa.float64()))
        self.scope = dataset.scope

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys(('cell_id', *(c for c in columns if c != self.column))))
        for batch in getattr(self.dataset, method)(*args, columns=fields, **kwargs):
            records = [self.assignments.get(identity) for identity in batch.column('cell_id').to_pylist()]
            labels = [record[1] if record is not None and record[0] == self.identity else None for record in records]
            batch = batch.append_column(self.column, pa.array([float(label == self.cluster) for label in labels]))
            batch = batch.filter(pa.array([label is not None for label in labels]))
            if batch.num_rows:
                yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


class ClusterFieldReader(MeasurementFieldReader):
    def __init__(self, authority, measurements, assignments, clusters, snapshot):
        self.measurements, self.assignments = measurements, assignments
        self.cluster = clusters[0]
        super().__init__(authority, snapshot)
        self.features = dict.fromkeys(clusters)

    def close(self):
        super().close()
        self.assignments.clear()
        self.measurements = ()

    @contextmanager
    def _selected_cells(self, authority):
        with ExitStack() as stack:
            datasets = []
            for measurement in self.measurements:
                view, component, scope = stack.enter_context(measurement_cells(measurement))
                identity = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id, measurement.section_id)
                dataset = _MeasuredCells(view, component, measurement, scope)
                datasets.append((identity, ClusterCells(dataset, self.assignments, self.cluster, identity)))
            yield _ImageMeasurementCells(datasets)

    def _outlines(self, dataset, name, lower, upper, bounds):
        result = super()._outlines(dataset, name, lower, upper, bounds)
        for row in result['visible_cells']:
            identity, row['cluster_id'] = self.assignments[row['cell_id']]
            row.update(zip(('sample_id','project_entry_id','slide_id','section_id'),identity,strict=True))
        return result

    def focus_bounds(self, identity):
        """Resolve a selected embedding cell against its accepted image geometry."""
        for measurement in self.measurements:
            scope = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id, measurement.section_id)
            if self.assignments.get(identity) != (scope, self.cluster):
                continue
            with measurement_cells(measurement) as (view, component, metadata):
                cells = _MeasuredCells(view, component, measurement, metadata)
                for batch in cells.lookup_ids((identity,), columns=('cell_id', 'x_level0', 'y_level0')):
                    for row in batch.to_pylist():
                        if row['cell_id'] == identity:
                            x, y = row['x_level0']*self.pixel_size[0], row['y_level0']*self.pixel_size[1]
                            return [max(0.,x-40), max(0.,y-40), min(self.extent[1],x+40), min(self.extent[2],y+40)]
        raise ValueError('The selected cell is not in this cluster and image.')


def cluster_field(provider, snapshot, evidence, request_id, options):
    from .review_execution import _validate_sources
    from ..saved_cell_gallery import gallery_resources

    cluster = options.get('cluster_id')
    clusters = tuple(row.cluster_id for row in evidence.clusters)
    if cluster not in clusters:
        raise ValueError('Choose a cluster in the current review.')
    image = tuple(options.get('image_identity', ()))
    images = {(row.sample_id,row.project_entry_id,row.slide_id) for group in evidence.clusters for row in group.section_counts}
    if image not in images:
        raise ValueError('Choose an image represented in the clustered population.')
    authorities = {a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    clustering = authorities[evidence.clustering_result_ref_sha256]
    sources = tuple(authorities[key] for key in evidence.source_authority_sha256s)
    key = ('cluster-field', snapshot.head.sha256, request_id, evidence.clustering_result_sha256, image)
    previous = provider._measurement_field
    cached = previous is not None and previous[0] == key
    if not cached:
        provider._measurement_field = None
        if previous is not None:previous[1].close()
    # Reserve the standard decoder's JSON/object workspace once. Subsequent
    # reads account for only the retained image index and bounded field buffers.
    json_bytes = sum(a.artifact_ref.byte_count or 0 for a in (clustering,*sources))
    memory_bytes = 640 * 1024**2 + (previous[1].membership_bytes if cached else 5 * json_bytes)
    with gallery_resources(snapshot, clustering, memory_bytes=memory_bytes):
        if not cached:
            result, resolved, _ = _validate_sources(clustering_authority=clustering,
                source_authorities=sources, maximum_clustering_result_byte_count=max(a.artifact_ref.byte_count or 0 for a in (clustering,*sources)))
            if result.result_sha256 != evidence.clustering_result_sha256:
                raise ValueError('The saved assignments differ from the current review.')
            selected = [(p,m) for p,m in resolved if (m.sample_id,m.project_entry_id,m.slide_id) == image]
            measurements = tuple(m for _,m in selected)
            identities = {(m.sample_id,m.project_entry_id,m.slide_id,m.section_id):None for m in measurements}
            identities = {identity:identity for identity in identities}
            assignments = {}
            labels = {identity:identity for identity in clusters}
            for row in result.assignments:
                if (row.sample_id,row.project_entry_id,row.slide_id) == image:
                    scope = identities[(row.sample_id,row.project_entry_id,row.slide_id,row.section_id)]
                    assignments[row.cell_id] = (scope,labels[row.cluster_id])
            del result
            authority = authorities[selected[0][0].measurement_result_ref_sha256]
            reader = ClusterFieldReader(authority, measurements, assignments, clusters, snapshot)
            import sys
            reader.membership_bytes = sys.getsizeof(assignments) + sum(sys.getsizeof(cell)+sys.getsizeof(record)+sys.getsizeof(record[0]) for cell,record in assignments.items())
            reader.authority = authority
            provider._measurement_field = (key, reader)
        else:
            reader = previous[1]
        reader.cluster = cluster
        display = {**options, 'measurement': cluster, 'lower': 1., 'upper': 1.}
        focus = display.pop('focus_cell_id', None)
        if focus is not None:
            display['field_um'] = reader.focus_bounds(focus)
        if not display.get('channels'):
            from ome_types import from_xml
            from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
            inventory = resolve_component_identity_inventory(from_xml(reader.image.attributes['mif_pipeline']['ome_xml']), 0)
            nuclear = inventory.nuclear_component_index if inventory is not None else None
            first = reader.measurements[0]
            feature = next((f for f in first.measurement_plan.features if f.measurement == options.get('profile_measurement')), None)
            channel = next((i for i,c in enumerate(first.canonical_channels) if feature is not None and c['channel_id'] == feature.channel_id), None)
            display['channels'] = list(dict.fromkeys(([nuclear] if nuclear is not None else []) +
                ([channel] if channel is not None else list(range(min(3,len(first.canonical_channels)))))))[:6]
        bundle = reader.read(reader.authority, snapshot, display)
        bundle['metadata'].update(available_channels=[dict(index=i,label=c['label']) for i,c in enumerate(reader.image.channels)],
            image_uri=reader.image.path.as_uri(), population_label='clustered cells', cluster_id=cluster, focus_cell_id=focus)
        return bundle
