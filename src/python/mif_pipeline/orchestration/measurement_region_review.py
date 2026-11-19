"""Filter saved measured cells by their accepted region memberships."""
from collections.abc import Mapping
import json

import pyarrow as pa
import pyarrow.compute as pc

_PRIMARY = 'population:primary_structure'
_IMAGE = ('sample_id', 'project_entry_id', 'slide_id')
_BOUNDS = ('min_x', 'min_y', 'max_x', 'max_y')


class FinalRegionCells:
    """Expose accepted area metadata while retaining the collection's scoped queries."""
    identity_columns = ('sample_id', 'project_entry_id', 'slide_id', 'section_id', 'analysis_domain_id')
    def __init__(self, handoff, identity):
        from .region_review_geometry import geometry_measurements
        from mif_pipeline.cell_analysis.spatial_handoff import FinalizedSecondaryDomainEvidence
        self.dataset = handoff.cell_dataset
        self._identity_values = {key: identity[key] for key in ('sample_id', 'project_entry_id')
                                 if key not in self.dataset.schema.names}
        self.schema = pa.schema([*self.dataset.schema, *(pa.field(key, pa.string()) for key in self._identity_values)])
        scope = self.dataset.description['scope']
        self.scope = {**scope, **{name: identity[name] for name in _IMAGE},
            'x_um_per_px': handoff.spatial_frame.x_um_per_px,
            'y_um_per_px': handoff.spatial_frame.y_um_per_px,
            'domains': {key:value for key,value in scope.get('domains', {}).items()
                        if key in scope.get('active_analysis_domain_ids', ())}}
        active = {item.roi_id for result in handoff.domain_results.values()
            if isinstance(result.finalization, FinalizedSecondaryDomainEvidence) and result.domain_status == 'active'
            for item in result.finalization.dispositions if item.disposition == 'active_included'}
        self.final_secondary_regions = {}
        for number, state in enumerate((item for item in handoff.final_roi_states.values()
                                       if item.pipeline_stage == 'secondary_classifier'), 1):
            if state.record.roi_id in active:
                record = state.record
                self.final_secondary_regions[record.roi_id] = dict(
                    number=number, label=record.semantic_class_id or record.roi_class,
                    bounds=geometry_measurements(record.geometry, 1., 1.)['bounds_level0'])

    def __getattr__(self, name):
        return getattr(self.dataset, name)

    def _batches(self, method, *args, columns, **kwargs):
        native = tuple(key for key in columns if key not in self._identity_values) or ('cell_id',)
        for batch in getattr(self.dataset, method)(*args, columns=native, **kwargs):
            for key, value in self._identity_values.items():
                if key in columns:
                    batch = batch.append_column(key, pa.array([value]*batch.num_rows, type=pa.string()))
            yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns=columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', bounds, columns=columns, predicate=predicate)

    def lookup_ids(self, ids, *, columns):
        yield from self._batches('lookup_ids', ids, columns=columns)


def region_selection(value):
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get('kind') not in {'primary_class', 'analysis_area', 'secondary_class', 'secondary_area'}:
        raise ValueError('Choose an available measurement region.')
    keys = {'kind', 'value'} | ({'image_identity'} if value['kind'] in {'analysis_area', 'secondary_area'} or 'image_identity' in value else set())
    if set(value) != keys or not isinstance(value['value'], str) or not value['value'].strip():
        raise ValueError('The measurement region requires its saved identity.')
    selected = dict(value)
    if 'image_identity' in selected:
        identity = value['image_identity']
        if (not isinstance(identity, (list, tuple)) or len(identity) != 3
                or any(not isinstance(item, str) or not item.strip() for item in identity)):
            raise ValueError('An analysis area requires its complete image identity.')
        selected['image_identity'] = list(identity)
    return selected


def region_key(value):
    return json.dumps(region_selection(value), sort_keys=True)


def saved_primary_labels(authorities, snapshot, dependencies):
    """Resolve display names from the classifier evidence selected by segmentation."""
    from .batch_training.record_publication import decode_current_committed_classifier_record_set
    current = {item.artifact_ref.artifact_ref_sha256:item for item in snapshot.committed_artifact_authorities}
    labels, records, materials, defaults = {}, {}, {}, {}
    for authority in authorities:
        identity = authority.artifact_ref.owning_identity
        image = tuple(identity[name] for name in _IMAGE)
        names = labels.setdefault(image, {})
        sample = next(item for item in snapshot.manifest.enabled_samples if item.sample_id == image[0])
        defaults[image] = {item.class_id:item.display_name for item in sample.config.primary_classes or ()}
        segmentation = current[identity['segmentation_result_ref_sha256']].artifact_ref.owning_identity
        for reference in segmentation['primary_inference_result_ref_sha256s']:
            primary = current[reference].artifact_ref.owning_identity
            record_reference = primary['classifier_record_set_ref_sha256']
            if record_reference not in records:
                records[record_reference] = decode_current_committed_classifier_record_set(current[record_reference],
                    store=dependencies.training.classifier_record_set_store, state_store=dependencies.runtime.state_store)
            record = next(item for item in records[record_reference].classifier_records
                          if item.classifier_record_sha256 == primary['selected_classifier_record_sha256'])
            if record.fit_material_sha256 is None:
                continue
            if record.classifier_record_sha256 not in materials:
                material = dependencies.training.project_provider.load_native_classifier_fit_material(
                    record.native_source_project_identity_sha256, record.fit_material_sha256,
                    workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
                materials[record.classifier_record_sha256] = ({item['class_id']:item['name'] for item in
                    json.loads(material.training_observations_json).get('classes', ())}
                    if material.training_observations_json is not None else {})
            for class_id, label in materials[record.classifier_record_sha256].items():
                if class_id in names and names[class_id] != label:
                    raise ValueError('Accepted primary class names disagree within this image.')
                names[class_id] = label
    for image, names in defaults.items():
        for class_id, label in names.items():
            labels[image].setdefault(class_id, label)
    return labels


def region_choices(datasets, labels_by_image=None):
    """Read only saved dictionaries and area metadata, never cell/image rows."""
    classes, areas, image_order, area_counts, secondary = {}, {}, [], {}, []
    for dataset in datasets:
        scope = dataset.scope
        image = tuple(scope[name] for name in _IMAGE)
        dictionary = dataset.dictionaries.get(_PRIMARY)
        if dictionary is not None and _PRIMARY in dataset.schema.names:
            for identity, label in zip(dictionary.label_ids or dictionary.labels, dictionary.labels, strict=True):
                if identity != 'Ignore':
                    label = (labels_by_image or {}).get(image, {}).get(identity, label)
                    classes.setdefault(identity, {})[image] = label
        if image not in image_order:
            image_order.append(image)
        for domain in sorted(scope.get('domains', {})):
            if (image, domain) not in areas:
                area_counts[image] = area_counts.get(image, 0) + 1
                areas[image, domain] = area_counts[image]
        secondary.extend((image, identity, record) for identity, record in
                         getattr(dataset, 'final_secondary_regions', {}).items())
    choices = [dict(label='All areas', selection=None)]
    for identity, names in sorted(classes.items()):
        if len(set(names.values())) == 1:
            choices.append(dict(label='Primary · ' + next(iter(names.values())),
                                selection=dict(kind='primary_class', value=identity)))
        else:
            # Equal local IDs can name different classes in separate images.
            for image, label in names.items():
                choices.append(dict(label='Primary · ' + label + ' · ' + image[0],
                    selection=dict(kind='primary_class', value=identity, image_identity=list(image))))
    if len(areas) > 1:
        for (image, domain), index in areas.items():
            label = f'Analysis area {index}'
            if len(image_order) > 1:
                label += ' · ' + ' · '.join(dict.fromkeys((image[0], image[2])))
            choices.append(dict(label=label, selection=dict(kind='analysis_area', value=domain,
                                                            image_identity=list(image))))
    for label in sorted({row['label'] for _image,_identity,row in secondary}):
        choices.append(dict(label='Secondary · '+label,
                            selection=dict(kind='secondary_class', value=label)))
    for image, identity, row in secondary:
        label=f"Secondary area {row['number']} · {row['label']}"
        if len(image_order) > 1:
            label += ' · ' + image[0]
        choices.append(dict(label=label, selection=dict(kind='secondary_area', value=identity,
                                                        image_identity=list(image))))
    return choices


class _RegionCells:
    """Filter each projected batch; bounded queries retain the source spatial index."""
    def __init__(self, dataset, selection):
        self.dataset, self.selection = dataset, selection
        self.row_count = None
        self.region_bounds_level0 = None
        self.column = (_PRIMARY if selection['kind'] == 'primary_class' else
                       'secondary_roi_ids' if selection['kind'].startswith('secondary_') else 'analysis_domain_id')
        dictionary = dataset.dictionaries.get(self.column)
        labels = () if dictionary is None else dictionary.label_ids or dictionary.labels
        self.code = labels.index(selection['value']) if selection['value'] in labels else None
        self.image_matches = ('image_identity' not in selection or
            tuple(dataset.scope[name] for name in _IMAGE) == tuple(selection['image_identity']))
        self.secondary_codes = None
        self._fixed_bounds = False
        if self.column == 'secondary_roi_ids':
            regions = getattr(dataset, 'final_secondary_regions', {})
            ids = [key for key, row in regions.items() if
                   (key == selection['value'] if selection['kind'] == 'secondary_area' else row['label'] == selection['value'])]
            if not ids or not self.image_matches:
                raise ValueError('Choose a currently included secondary structure.')
            self.secondary_codes = {index for index, key in enumerate(labels) if key in ids}
            bounds = [regions[key]['bounds'] for key in ids]
            self.region_bounds_level0 = tuple((min(row[index] for row in bounds) if index < 2 else
                                              max(row[index] for row in bounds)) for index in range(4))
            self._fixed_bounds = True

    def __getattr__(self, name):
        return getattr(self.dataset, name)

    def _batches(self, method, columns, *args, **kwargs):
        available = self.column in self.schema.names
        locate = not self._fixed_bounds and method == 'iter_batches' and all(name in columns for name in ('x_level0','y_level0'))
        bounds_columns = _BOUNDS if locate and all(name in self.schema.names for name in _BOUNDS) else ()
        fields = tuple(dict.fromkeys((*columns, *([self.column] if available else []), *bounds_columns)))
        total = selected = 0
        bounds = None
        for batch in getattr(self.dataset, method)(*args, columns=fields, **kwargs):
            total += batch.num_rows
            if not available or not self.image_matches or (self.column == _PRIMARY and self.code is None):
                continue
            if self.column == _PRIMARY:
                mask = pa.array([self.code in (values or ()) for values in batch.column(self.column).to_pylist()])
            elif self.column == 'secondary_roi_ids':
                values = batch.column(self.column).to_pylist()
                if any(any(type(code) is not int or not 0 <= code < len(self.dictionaries[self.column].labels)
                           for code in (row or ())) for row in values):
                    raise ValueError('A final secondary membership has an invalid category code.')
                mask = pa.array([bool(self.secondary_codes.intersection(row or ())) for row in values])
            else:
                mask = pc.equal(batch.column(self.column), self.selection['value']).fill_null(False)
            batch = batch.filter(mask)
            selected += batch.num_rows
            if batch.num_rows:
                if locate:
                    source = bounds_columns or ('x_level0','y_level0','x_level0','y_level0')
                    observed = tuple(pc.min(batch.column(name)).as_py() if index < 2 else
                                     pc.max(batch.column(name)).as_py() for index,name in enumerate(source))
                    if all(value is not None for value in observed):
                        bounds = observed if bounds is None else tuple(
                            min(a,b) if index < 2 else max(a,b)
                            for index,(a,b) in enumerate(zip(bounds,observed,strict=True)))
                yield batch.select(columns)
        if method == 'iter_batches':
            if total != self.dataset.row_count:
                raise ValueError('Saved measurement rows differ from the accepted population.')
            # Existing histogram callers check this after their complete pass.
            # No extra traversal is needed just to count the selected population.
            self.row_count = selected
            if locate:
                self.region_bounds_level0 = bounds

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)

    def lookup_ids(self, ids, *, columns):
        yield from self._batches('lookup_ids', columns, ids)


def filtered_cells(dataset, selection):
    selected = region_selection(selection)
    if selected is None:
        return dataset
    from .measurement_spatial_review import _ImageMeasurementCells
    if isinstance(dataset, _ImageMeasurementCells):
        sections = dataset.sections
        return _ImageMeasurementCells([(identity, _RegionCells(cells, selected)) for identity, cells in sections])
    return _RegionCells(dataset, selected)


def region_cell_bounds(dataset, pixel_size):
    """Reuse bounds accumulated by the existing density pass, without another scan."""
    from .measurement_spatial_review import _ImageMeasurementCells
    candidates = ([getattr(cells, 'region_bounds_level0', None) for _identity,cells in dataset.sections]
                  if isinstance(dataset, _ImageMeasurementCells) else [getattr(dataset, 'region_bounds_level0', None)])
    candidates = [bounds for bounds in candidates if bounds is not None]
    if not candidates:
        return None
    return tuple((min(bounds[index] for bounds in candidates) if index < 2 else
                  max(bounds[index] for bounds in candidates))*pixel_size[index % 2] for index in range(4))


def accepted_area_bounds(authorities, snapshot, selection, state_store):
    """Read the chosen area's accepted bounds in the selected exact image."""
    binding = next((item for authority in authorities
        if tuple(authority.artifact_ref.owning_identity[name] for name in _IMAGE) == tuple(selection['image_identity'])
        for item in authority.artifact_ref.scope.analysis_domain_bindings
        if item.analysis_domain_id == selection['value']), None)
    if binding is None:
        return None
    sources = [item for item in snapshot.committed_artifact_authorities
        if item.artifact_ref.artifact_kind == 'accepted_analysis_domain'
        and binding in item.artifact_ref.scope.analysis_domain_bindings]
    if len(sources) != 1 or state_store is None:
        raise ValueError('The selected analysis area lacks its current accepted outline.')
    from .region_review_geometry import saved_region_review
    review = saved_region_review(state_store, sources[0], snapshot)
    row = next(item for item in review['regions'] if item['layer'] == 'analysis'
               and item['region_id'] == binding.analysis_domain_id)
    scale = (review['x_um_per_px'], review['y_um_per_px'])
    return tuple(value*scale[index % 2] for index,value in enumerate(row['bounds_level0']))
