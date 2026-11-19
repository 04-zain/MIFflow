"""Read bounded image examples from a pending gate's exact measurement selection."""
from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path

from mif_pipeline.cell_analysis.measurement_rows import MarkerIntensityRowsDescriptor, read_measurement_dataset_selection
from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection, population_includes
from mif_pipeline.datasets.cell_store import CellDataset
from ..saved_cell_gallery import load_cell_crops


@contextmanager
def measurement_cells(measurement):
    """Open the already accepted base, current edits and measured component once."""
    ref = measurement.measurement_rows
    selected = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(ref.path, ref.sha256, ref.byte_count, ref.row_count, ref.content_schema_version),
        authenticated_output_root=ref.path.parent.parent.parent)
    base = CellDataset(selected['cell_dataset']['path'], max_batch_rows=512)
    scope = base.description['scope']
    if (base.summary.sha256 != selected['cell_dataset']['sha256']
            or scope['population_id'] != measurement.segmentation_population_id
            or scope['canonical_image_sha256'] != measurement.canonical_image_sha256
            or selected['section_id'] != measurement.section_id):
        raise ValueError('The cell geometry differs from this marker review’s measurement selection.')
    if selected['cell_edits'] is not None:
        base = base.with_edits(selected['cell_edits'])
    try:
        view = base.with_components(selected['components'], geometry_revision=selected['geometry_revision'])
        component = Path(selected['accepted_component']['path'])
        if next(item for item in view._components if item.path == component).sha256 != selected['accepted_component']['sha256']:
            raise ValueError('The marker review’s measured component has changed.')
        yield view, component, scope
    finally:
        if selected['cell_edits'] is not None:
            base._edit_index.close()


@dataclass
class GateExamples:
    """At most one near-cutoff, lowest and highest representative; no cell-vector retention."""
    cutoff: float
    near: tuple | None = None
    below: tuple | None = None
    above: tuple | None = None
    included: int = 0
    unknown: int = 0

    def add(self, identity, value, payload):
        self.included += 1
        if value is None or not math.isfinite(value):
            self.unknown += 1
            return
        item = (float(value), tuple(identity), payload)
        if self.near is None or (abs(value-self.cutoff), item[1]) < (abs(self.near[0]-self.cutoff), self.near[1]):
            self.near = item
        if value < self.cutoff and (self.below is None or (value, item[1]) < self.below[:2]):
            self.below = item
        if value >= self.cutoff and (self.above is None or (-value, item[1]) < (-self.above[0], self.above[1])):
            self.above = item

    def rows(self):
        used, result = set(), []
        for role, item in (('Near cutoff', self.near), ('Lowest value', self.below), ('Highest value', self.above)):
            if item is not None and item[1] not in used:
                used.add(item[1]); result.append((role, *item))
        return tuple(result)


def select_intensity_examples(measurements, semantic, population_settings, cutoff):
    """Read one measurement plus the selected population membership, never geometry."""
    if not math.isfinite(cutoff):
        raise ValueError('Enter a finite cutoff before inspecting cells.')
    examples = GateExamples(cutoff)
    column = 'measurement:' + semantic['measurement']
    for measurement in measurements:
        if semantic['measurement'] not in measurement.selected_measurement_names:
            raise ValueError('This marker measurement is unavailable in the selected image.')
        selection = resolve_population_selection(population_settings, sample_id=measurement.sample_id,
            project_entry_id=measurement.project_entry_id, slide_id=measurement.slide_id)
        membership_column = None if selection['kind'] == 'all_segmented' else 'population:' + selection['kind']
        with measurement_cells(measurement) as (view, component, _scope):
            columns = ['cell_id', column, *([membership_column] if membership_column else [])]
            for batch in view.iter_applied_batches(component, columns=columns):
                for row in batch.to_pylist():
                    if membership_column:
                        raw = row[membership_column]
                        if selection['kind'] != 'detected_tissue':
                            raw = [view.dictionaries[membership_column].labels[index] for index in raw]
                        if not population_includes(selection, {selection['kind']: raw}):
                            continue
                    identity = (measurement.sample_id, measurement.project_entry_id,
                                measurement.slide_id, measurement.section_id, row['cell_id'])
                    examples.add(identity, row[column], measurement)
    if not examples.rows():
        raise ValueError('No cells with a valid measurement occur in this review population.')
    return examples


def load_example_crops(examples, image_ref, semantic, options):
    """Look up only the selected geometries, then use the shared calibrated crop reader."""
    selected = examples.rows()
    measurements = {item[3].artifact_ref.artifact_ref_sha256: item[3] for item in selected}
    rows, scope = {}, None
    for measurement in measurements.values():
        if image_ref.sha256 != measurement.canonical_image_sha256:
            raise ValueError('The prepared image differs from the marker review’s measured image.')
        ids = tuple(item[2][-1] for item in selected if item[3] is measurement)
        with measurement_cells(measurement) as (view, _component, current_scope):
            scope = current_scope
            for batch in view.lookup_ids(ids, columns=('cell_id','x_level0','y_level0','cell_geometry','nucleus_geometry')):
                for row in batch.to_pylist():
                    if row['cell_id'] in rows:
                        raise ValueError('Cell examples must identify distinct cells in one image.')
                    rows[row['cell_id']] = row
    ids = tuple(item[2][-1] for item in selected)
    if set(ids) != set(rows):
        raise ValueError('A selected example is absent from the current measured cell geometry.')
    measurement = selected[0][3]
    channels = tuple(measurement.canonical_channels)
    channel = next(index for index, value in enumerate(channels) if value['channel_id'] == semantic['channel_id'])
    resolved = {**options, 'channels': options.get('channels', [channel]), 'cell_ids': list(ids)}
    quantity = semantic['measurement']
    unit = 'score' if quantity == 'Nimbus score' else 'intensity'
    labels = tuple(f'{role.replace("value", unit)}\n{quantity}: {value:.6g}'
                   for role,value,_identity,_measurement in selected)
    data = load_cell_crops(image_ref.locator, rows, ids, labels, resolved,
        image_id=measurement.canonical_image_id, image_sha256=measurement.canonical_image_sha256,
        pixel_size_um=(scope['x_um_per_px'],scope['y_um_per_px']))
    data.selection.update(cutoff=examples.cutoff, quantity=quantity,
        cell_identities=[list(item[2]) for item in selected],
        values=[item[1] for item in selected], included_count=examples.included, unknown_count=examples.unknown)
    return data


def select_score_examples(measurements, marker, provenance, population_settings, cutoff):
    """Stream the exact already-reviewed score bytes; retain only three examples.

    The recorded SHA authenticates the prior full score validation. Reuse that
    reader's header, identity and score parsing; do not rebuild its all-cell index.
    Restricted populations require only bounded membership lookups.
    """
    import hashlib
    import json
    from contextlib import ExitStack
    from mif_pipeline.cell_analysis.nimbus import _header_details, _identity, _score, _unique_object, _MAX_LINE_BYTES

    if not math.isfinite(cutoff) or not 0 <= cutoff <= 1:
        raise ValueError('Choose a Nimbus score cutoff between zero and one.')
    by_scope = {(m.sample_id,m.project_entry_id,m.slide_id,m.section_id):m for m in measurements}
    examples = GateExamples(cutoff)
    digest = hashlib.sha256()
    with ExitStack() as stack:
        opened = {}
        for scope, measurement in by_scope.items():
            selection = resolve_population_selection(population_settings, sample_id=scope[0], project_entry_id=scope[1], slide_id=scope[2])
            if selection['kind'] != 'all_segmented':
                view, _component, _scope = stack.enter_context(measurement_cells(measurement))
                opened[scope] = (view, selection)
        pending = []

        def consume():
            for scope in {identity[:4] for identity,value in pending}:
                current = [(identity,value) for identity,value in pending if identity[:4]==scope]
                measurement = by_scope[scope]
                eligible = None
                if scope in opened:
                    view, selection = opened[scope]
                    column = 'population:' + selection['kind']
                    eligible = set()
                    for batch in view.lookup_ids([identity[-1] for identity,value in current], columns=('cell_id',column)):
                        for row in batch.to_pylist():
                            raw = row[column]
                            if selection['kind'] != 'detected_tissue':
                                raw = [view.dictionaries[column].labels[index] for index in raw]
                            if population_includes(selection,{selection['kind']:raw}):
                                eligible.add(row['cell_id'])
                for identity,value in current:
                    if eligible is None or identity[-1] in eligible:
                        examples.add(identity,value,measurement)
            pending.clear()

        with Path(provenance['path']).open('rb') as stream:
            def record():
                line = stream.readline(_MAX_LINE_BYTES+1)
                if len(line)>_MAX_LINE_BYTES:
                    raise ValueError('Nimbus score record exceeds its size allowance.')
                digest.update(line)
                return json.loads(line,object_pairs_hook=_unique_object) if line else None
            scopes,_ = _header_details(record(),marker)
            for scope,m in by_scope.items():
                if scopes.get(scope)!=(m.segmentation_population_id,m.canonical_image_id):
                    raise ValueError('Nimbus scores differ from this review’s image or cell population.')
            while (raw:=record()) is not None:
                identity=_identity(raw['cell_identity'],5)
                if identity[:4] not in by_scope:
                    continue
                pending.append((identity,_score(raw['score'])))
                if len(pending)>=512:consume()
            if pending:consume()
    if digest.hexdigest()!=provenance['sha256']:
        raise ValueError('Nimbus scores changed after this review was prepared. Reopen the marker review.')
    if not examples.rows():
        raise ValueError('No cells with a known Nimbus score occur in this review population.')
    return examples


def render_marker_gallery(snapshot, context, options, provider):
    """Resolve exact current inputs, reuse bounded examples/crops, and draw a view."""
    import json
    from ..batch_measurement_artifacts import decode_committed_measurements_result
    from ..saved_cell_gallery import gallery_resources, display_options
    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery

    marker = next((m for m in context['marker_reviews'] if m['marker_id']==options['marker_id']),None)
    if marker is None:
        raise ValueError('Choose a marker in the current review.')
    image_identity = tuple(options['image_identity'])
    if not any(tuple(row[k] for k in ('sample_id','project_entry_id','slide_id'))==image_identity for row in marker['per_image_distributions']):
        raise ValueError('Choose an image represented in this marker review.')
    bindings = [b for b in context['measurement_authority_bindings']
                if tuple(b[k] for k in ('sample_id','project_entry_id','slide_id'))==image_identity]
    current = {a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    authorities = tuple(current[b['measurements_result_ref_sha256']] for b in bindings)
    measurements = tuple(decode_committed_measurements_result(a) for a in authorities)
    if not measurements:
        raise ValueError('This image has no current measurement selection.')
    images = [a.artifact_ref for a in snapshot.committed_artifact_authorities
              if a.artifact_ref.artifact_kind=='processed_image'
              and a.artifact_ref.scope.sample_id==image_identity[0]
              and a.artifact_ref.sha256==measurements[0].canonical_image_sha256]
    if len(images)!=1:
        raise ValueError('The measured image is no longer available for this review.')
    image=images[0]
    semantic=marker['measurement_semantic']
    settings=context['marker_workflow']['settings']
    population=settings.get('phenotyping_population')
    source=options['source'];cutoff=float(options['cutoff'])
    if marker['fit_scope']=='per_slide':
        review=next(row['review'] for row in marker['per_slide_reviews']
                    if tuple(row[k] for k in ('sample_id','project_entry_id','slide_id'))==image_identity)
    else:review=marker['pooled_review']
    candidate=next((row for row in review['candidates'] if row['method']==source and row['status']=='available'),None)
    if source!='manual' and candidate is None:
        raise ValueError('Choose an available threshold method before inspecting cells.')
    selection_key=(snapshot.head.sha256,context['evidence_sha256'],json.dumps((marker['marker_id'],image_identity,source,cutoff)))
    with gallery_resources(snapshot,authorities[0]):
        previous=provider._marker_gate_examples
        if previous is None or previous[0]!=selection_key:
            provider._marker_gate_examples=None
            examples=(select_score_examples(measurements,marker['marker_id'],candidate['parameters']['score_provenance'],population,cutoff)
                      if source=='nimbus' else select_intensity_examples(measurements,semantic,population,cutoff))
            provider._marker_gate_examples=(selection_key,examples)
        else:examples=previous[1]
        crop_options={key:options[key] for key in ('channels','crop_um') if key in options}
        if 'channels' not in crop_options:
            from ome_types import from_xml
            from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
            metadata=json.loads((image.locator/'zarr.json').read_text())
            inventory=resolve_component_identity_inventory(from_xml(metadata['attributes']['mif_pipeline']['ome_xml']),0)
            channel=next(i for i,c in enumerate(measurements[0].canonical_channels) if c['channel_id']==semantic['channel_id'])
            crop_options['channels']=list(dict.fromkeys((channel,*([inventory.nuclear_component_index] if inventory and inventory.nuclear_component_index is not None else []))))
        crop_key=('marker-gate',selection_key,json.dumps(crop_options,sort_keys=True))
        previous=provider._cell_gallery
        if previous is None or previous[0]!=crop_key:
            provider._cell_gallery=None
            quantity={**semantic,'measurement': 'Nimbus score' if source=='nimbus' else semantic['measurement']}
            gallery=load_example_crops(examples,image,quantity,crop_options)
            provider._cell_gallery=(crop_key,gallery)
        else:gallery=previous[1]
        resolved=display_options(gallery,{**options,**crop_options,'cell_ids':list(gallery.cell_ids)})
        resolved['available_channels']=[{'index':i,'label':c['name']} for i,c in enumerate(measurements[0].canonical_channels)]
        resolved['label_field']='review_label'
        resolved['include_pixels']=True
        resolved['image_label']=' · '.join(dict.fromkeys((image_identity[0],image_identity[2])))
        outputs=render_cell_gallery(gallery,resolved)
        from mif_pipeline.cell_analysis.result_plots import _csv_output
        table=_csv_output('marker-cell-examples','Marker cell values and selection',
            ('sample_id','project_entry_id','slide_id','section_id','cell_id','example','quantity','value','cutoff'),
            ((*identity,role,gallery.selection['quantity'],value,cutoff)
             for role,value,identity,_measurement in examples.rows()))
        outputs=(*outputs,table)
    return outputs,resolved
