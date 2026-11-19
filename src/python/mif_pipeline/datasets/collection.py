"""Current scientific collections selecting existing section bases and results."""

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .categories import CategoryDictionary
from .cell_store import CellDataset, CellDatasetDescription, _regular_snapshot

CELL_COLLECTION_SCHEMA_VERSION = 'mif-cell-dataset-collection/v1'
_SECTION_FIELDS = {'section_id', 'population_id', 'cell_dataset', 'components', 'geometry_revision', 'cell_edits', 'phenotype_view'}


class SelectedPhenotypeDataset(CellDataset):
    """Our explicit scientific phenotype view projected onto canonical fields."""

    def __init__(self, base, selection):
        """Resolve our selected mode/generation without changing display state.

        We map that result's seven typed namespaced fields to the canonical
        scientific fields. Its dictionaries retain their result identities;
        marker-state JSON is decoded only when a complete cell is requested.
        """
        if not isinstance(selection, dict) or set(selection) != {'mode', 'phenotype_generation_id'} or any(not isinstance(value, str) or not value for value in selection.values()):
            raise ValueError('Select an exact current phenotype mode and generation')
        self.__dict__.update(base.__dict__)
        self._result_view = base
        prefix = 'phenotype_view/' + selection['mode'] + '/' + selection['phenotype_generation_id'] + '/'
        names = ('primary_phenotype', 'phenotype_source', 'phenotype_components', 'marker_states', 'phenotype_confidence', 'cluster_id', 'cluster_label')
        self._aliases = {name: prefix + name for name in names}
        self._aliases['mif.primary_class_source'] = prefix + 'class_source'
        self._generation_id = selection['phenotype_generation_id']
        if any(source not in base.schema.names for source in self._aliases.values()):
            raise ValueError('Selected phenotype component omits its canonical result fields')
        fields = {field.name: field for field in base.schema}
        self.dictionaries = dict(base.dictionaries)
        for name, source in self._aliases.items():
            fields[name] = pa.field(name, base.schema.field(source).type)
            if source in base.dictionaries:
                dictionary = base.dictionaries[source]
                self.dictionaries[name] = CategoryDictionary(name, dictionary.dictionary_id, dictionary.labels, dictionary.label_ids, dictionary.colors)
        self.schema = pa.schema(list(fields.values()))

    def _groups(self, groups, columns):
        """Read our selected typed fields under canonical scientific names."""
        required = list(dict.fromkeys(self._aliases.get(name, name) for name in columns))
        for batch in self._result_view._groups(groups, required):
            yield pa.RecordBatch.from_arrays([pa.array([self._generation_id] * batch.num_rows, type=pa.string()) if name == 'phenotyping_generation_id' else batch.column(self._aliases.get(name, name)) for name in columns], names=columns)


def _descriptor(reference):
    """Read our exact immutable descriptor under its selected SHA identity."""
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('A collection reference requires its path and SHA-256')
    path = Path(reference['path'])
    if not path.is_absolute() or path.stat().st_size > 64 * 1024**2:
        raise ValueError('A collection descriptor requires bounded absolute metadata')
    snapshot = _regular_snapshot(path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != reference['sha256'] or _regular_snapshot(path) != snapshot:
        raise ValueError('A collection reference changed from its selected identity')
    return path, json.loads(payload)


def _section(selector):
    """Resolve our selected base, results and current sparse edits for a section.

    We authenticate the base identity before opening its layout. Result scopes
    must belong to that population and revision; a nonzero shape revision must
    retain its sparse checkpoint so projected Python values use current shapes.
    """
    if not isinstance(selector, dict) or set(selector) != _SECTION_FIELDS:
        raise ValueError('A collection section requires its exact current selection')
    if any(not isinstance(selector[name], str) or not selector[name] for name in ('section_id', 'population_id')):
        raise ValueError('A collection section requires its stable scope identity')
    path, description = _descriptor(selector['cell_dataset'])
    if description['scope']['population_id'] != selector['population_id']:
        raise ValueError('A collection selects a different segmentation population')
    section = description['scope'].get('section_id')
    if section is not None and section != selector['section_id']:
        raise ValueError('A collection base belongs to a different section')
    base = CellDataset(path)
    edit_index = None
    if selector['cell_edits'] is not None:
        _descriptor(selector['cell_edits'])
        edit_index = base.with_edits(selector['cell_edits'])._edit_index
    elif selector['geometry_revision'] != 0:
        raise ValueError('A collection requires current sparse shape edits')
    from .components import ComponentCellDataset
    try:
        view = ComponentCellDataset(base, selector['components'], selector['geometry_revision'], edit_index=edit_index)
        if selector['phenotype_view'] is not None:
            view = SelectedPhenotypeDataset(view, selector['phenotype_view'])
        if edit_index is not None:
            from .edits import EditedCellDataset
            view = EditedCellDataset(view, edit_index)
        return view
    except Exception:
        if edit_index is not None:
            edit_index.close()
        raise


def _validate_source_summary(summary):
    """Admit our producing owner's compact complete-population commitments.

    i) We require exact count, sorted-ID, domain-mapping and domain-inventory
    fields. ii) Per-domain counts sum to the source count and each digest has
    its canonical SHA-256 representation. This checks bounded metadata only;
    actual scientific operations read the selected columns they need.
    """
    fields = {'row_count', 'cell_ids_sha256', 'domain_cell_mapping_sha256', 'domain_inventories'}
    if not isinstance(summary, dict) or set(summary) != fields:
        raise ValueError('A current source requires exact compact population summaries')
    count = summary['row_count']
    inventories = summary['domain_inventories']
    def digest(value):
        return isinstance(value, str) and len(value) == 64 and all(character in '0123456789abcdef' for character in value)
    if type(count) is not int or count < 0 or any(not digest(summary[name]) for name in ('cell_ids_sha256', 'domain_cell_mapping_sha256')):
        raise ValueError('Source population count or identity digest is invalid')
    if not isinstance(inventories, dict) or any(not isinstance(id, str) or not id or not isinstance(item, dict) or set(item) != {'cell_count', 'cell_ids_sha256'} or type(item['cell_count']) is not int or item['cell_count'] < 0 or not digest(item['cell_ids_sha256']) for id, item in inventories.items()) or sum(item['cell_count'] for item in inventories.values()) != count:
        raise ValueError('Source domain summaries disagree with their complete population')


class CellDatasetCollection(CellDataset):
    """Projected scientific access across our selected section populations."""

    def __init__(self, path, *, max_index_bytes=64 * 1024**2):
        """Resolve our compact section selection and unify field vocabularies.

        i) We open each selected section's bounded layout and result metadata.
        ii) We freeze a collection dictionary for each category field and retain
        primitive remapping arrays. Geometry and numerical population arrays
        remain in their original files until a caller requests selected columns.
        """
        self.path = Path(path)
        self.root = self.path.parent
        if self.path.stat().st_size > max_index_bytes:
            raise ValueError('Cell collection metadata exceeds its allowance')
        original = _regular_snapshot(self.path)
        payload = self.path.read_bytes()
        self.description = json.loads(payload)
        if _regular_snapshot(self.path) != original or self.description.get('schema_version') != CELL_COLLECTION_SCHEMA_VERSION or self.description.get('complete') is not True:
            raise ValueError('A cell collection requires its completed current descriptor')
        try:
            self._initialize(max_index_bytes=max_index_bytes, descriptor_sha256=hashlib.sha256(payload).hexdigest())
        except Exception:
            self.close()
            raise

    @classmethod
    def from_selection(cls, selectors, scope, summary, *, max_index_bytes=64 * 1024**2):
        """Open our authenticated in-memory selection without publishing a copy.

        i) We admit completed base, component and sparse-edit metadata using the
        same current reader. ii) We retain the producing owner's compact count,
        sorted-ID and domain summaries. No cell rows, geometry or numeric values
        are scanned merely to construct this selection. Each scientific caller
        requests the columns used by its own operation.
        """
        fields = {'row_count','cell_ids_sha256','domain_cell_mapping_sha256','domain_inventories'}
        if not isinstance(summary,dict) or set(summary) != fields or not isinstance(scope,dict):
            raise ValueError('A current selection requires exact compact population summaries')
        description = {'schema_version':CELL_COLLECTION_SCHEMA_VERSION,'sections':list(selectors),'scope':scope,'complete':True,'source_summary':summary,**summary}
        payload = json.dumps(description,sort_keys=True,separators=(',',':')).encode()
        if len(payload)>max_index_bytes:
            raise ValueError('In-memory cell selection exceeds its metadata allowance')
        value = cls.__new__(cls)
        value.path = value.root = None
        value.description = json.loads(payload)
        try:
            value._initialize(max_index_bytes=max_index_bytes,descriptor_sha256=None)
        except Exception:
            value.close()
            raise
        return value

    def _initialize(self, *, max_index_bytes, descriptor_sha256):
        """Resolve our compact metadata and field-local category dictionaries.

        We validate declared counts and scientific schema relationships once,
        retain bounded layout/index metadata, and defer population reads until
        an operation requests its actual numeric or geometric inputs.
        """
        selectors = self.description['sections']
        if not isinstance(selectors, list) or len({item['section_id'] for item in selectors}) != len(selectors):
            raise ValueError('A cell collection requires distinct selected sections')
        self.sections = []
        for item in selectors:
            self.sections.append(_section(item))
        if sum(section.layout.nbytes + section.path.stat().st_size for section in self.sections) > max_index_bytes:
            raise ValueError('Selected section layouts exceed the collection index allowance')
        available_count = sum(section.row_count for section in self.sections)
        self.row_count = self.description['row_count']
        self.active_domains = self.description['scope'].get('active_analysis_domain_ids')
        if self.active_domains is not None and (not isinstance(self.active_domains, list) or any(not isinstance(id, str) for id in self.active_domains)):
            raise ValueError('A collection requires explicit active domain identities')
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool) or not 0 <= self.row_count <= available_count or self.active_domains is None and self.row_count != available_count:
            raise ValueError('Cell collection count differs from its selected sections')
        self.summary = CellDatasetDescription(self.path,self.row_count,CELL_COLLECTION_SCHEMA_VERSION,descriptor_sha256) if self.path is not None else None
        self.source_summary = self.description['source_summary']
        _validate_source_summary(self.source_summary)
        if self.source_summary['row_count'] > available_count:
            raise ValueError('Source population count exceeds the admitted sections')
        inventories = self.description['domain_inventories']
        if not isinstance(inventories,dict) or any(not isinstance(id,str) or not isinstance(item,dict) or set(item)!={'cell_count','cell_ids_sha256'} or not isinstance(item['cell_count'],int) or isinstance(item['cell_count'],bool) or item['cell_count']<0 or not isinstance(item['cell_ids_sha256'],str) or len(item['cell_ids_sha256'])!=64 for id,item in inventories.items()) or sum(item['cell_count'] for item in inventories.values())!=self.row_count:
            raise ValueError('Selected domain summaries disagree with the declared population count')
        active = self.active_domains if self.active_domains is not None else list(inventories)
        if len(set(active)) != len(active) or any(id not in inventories or inventories[id] != self.source_summary['domain_inventories'].get(id) for id in active) or sum(inventories[id]['cell_count'] for id in active) != self.row_count:
            raise ValueError('Selected active domains disagree with the authenticated source summary')
        fields = {}
        for section in self.sections:
            for field in section.schema:
                if field.name in fields and fields[field.name].type != field.type:
                    raise ValueError('Selected sections disagree on a scientific field type')
                fields.setdefault(field.name, field)
        self.schema = pa.schema(list(fields.values()))
        self.dictionaries = {}
        self._remaps = {}
        vocabulary_bytes = 0
        for name in {name for section in self.sections for name in section.dictionaries}:
            entries = []
            positions = {}
            meanings = {}
            for section in self.sections:
                dictionary = section.dictionaries.get(name)
                if dictionary is None:
                    continue
                codes = []
                for code, label in enumerate(dictionary.labels):
                    id = dictionary.label_ids[code] if dictionary.label_ids else label
                    color = dictionary.colors[code] if dictionary.colors else None
                    if id in meanings and meanings[id] != (label, color):
                        raise ValueError('Collection category label identities disagree')
                    if id not in positions:
                        vocabulary_bytes += len(id.encode()) + len(label.encode()) + 128
                        if vocabulary_bytes > max_index_bytes:
                            raise ValueError('Collection dictionaries exceed their allowance')
                        positions[id] = len(entries)
                        entries.append((id, label, color))
                        meanings[id] = (label, color)
                    codes.append(positions[id])
                self._remaps[(section.path, name)] = pa.array(codes, type=pa.int32())
            digest = hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()
            self.dictionaries[name] = CategoryDictionary(name, digest, tuple(entry[1] for entry in entries), tuple(entry[0] for entry in entries), tuple(entry[2] for entry in entries))

    def select_domains(self, domain_ids):
        """Select our active domains while retaining exact source commitments.

        We derive the selected count from the owner's per-domain summaries and
        filter only requested batches. The selection identity binds these exact
        domain commitments; the complete source summaries remain available for
        complete-domain checks and metadata-only publication.
        """
        domain_ids = sorted(set(domain_ids))
        inventories = self.source_summary['domain_inventories']
        if any(id not in inventories for id in domain_ids):
            raise ValueError('An active selection contains an unauthenticated domain')
        value = self.__class__.__new__(self.__class__)
        value.__dict__.update(self.__dict__)
        value.active_domains = domain_ids
        value.row_count = sum(inventories[id]['cell_count'] for id in domain_ids)
        value.description = {**self.description,'scope':{**self.description['scope'],'active_analysis_domain_ids':domain_ids},'row_count':value.row_count,'domain_inventories':{id:inventories[id] for id in domain_ids}}
        value.description.pop('cell_ids_sha256',None)
        value.description.pop('domain_cell_mapping_sha256',None)
        value.summary = None
        return value

    @property
    def selection_sha256(self):
        """Bind our scientific read selection to completed immutable metadata.

        Base and sparse-edit SHA identities are retained directly. Each distinct
        component descriptor is hashed once during its admission; all selected
        fields/scopes refer to that identity. Paths are excluded from this digest.
        Active-domain filtering and exact selected domain summaries complete the
        identity, so publication and read-only selection share the same meaning.
        """
        sections = []
        for selector,section in zip(self.description['sections'],self.sections):
            identities = {str(component.path):component.sha256 for component in section._components}
            sections.append({'section_id':selector['section_id'],'population_id':selector['population_id'],'cell_dataset_sha256':selector['cell_dataset']['sha256'],'components':{field:{scope:identities[str(Path(path))] for scope,path in selected.items()} for field,selected in selector['components'].items()},'geometry_revision':selector['geometry_revision'],'cell_edits_sha256':selector['cell_edits']['sha256'] if selector['cell_edits'] is not None else None,'phenotype_view':selector['phenotype_view']})
        active = sorted(self.active_domains) if self.active_domains is not None else sorted(self.description['domain_inventories'])
        material = {'sections':sorted(sections,key=lambda item:item['section_id']),'active_analysis_domain_ids':active,'domain_inventories':{id:self.description['domain_inventories'][id] for id in active},'row_count':self.row_count}
        return hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('utf-8')).hexdigest()

    def _project(self, section, batch, columns):
        """Return our common primitive field codes and projected null validity."""
        arrays = []
        for name in columns:
            if name not in batch.schema.names:
                values = pa.array(np.zeros(batch.num_rows, dtype=bool)) if name.startswith('present:') else pa.nulls(batch.num_rows, type=self.schema.field(name).type)
            else:
                values = batch.column(name)
                mapping = self._remaps.get((section.path, name))
                if mapping is not None:
                    codes = pc.list_flatten(values) if pa.types.is_list(values.type) else values
                    if pc.any(pc.or_(pc.less(codes, 0), pc.greater_equal(codes, len(mapping)))).as_py():
                        raise ValueError('A selected section contains an undeclared category code')
                    resolved = pc.take(mapping, codes)
                    if pa.types.is_list(values.type):
                        offsets = pc.subtract(values.offsets, values.offsets[0]).cast(pa.int32())
                        values = pa.ListArray.from_arrays(offsets, resolved, mask=values.is_null())
                    else:
                        values = resolved
            arrays.append(values)
        return pa.RecordBatch.from_arrays(arrays, names=columns)

    def _read(self, operation, *, columns, **arguments):
        """Release each requested section batch after its collection projection."""
        if not columns or any(name not in self.schema.names for name in columns):
            raise ValueError('Select existing collection fields')
        for section in self.sections:
            available = [name for name in columns if name in section.schema.names]
            if self.active_domains is not None and 'analysis_domain_id' not in available:
                available.append('analysis_domain_id')
            if not available:
                available = ['cell_id']
            for batch in getattr(section, operation)(columns=available, **arguments):
                if self.active_domains is not None:
                    batch = batch.filter(pc.is_in(batch.column('analysis_domain_id'), value_set=pa.array(self.active_domains, type=pa.string())))
                    if not batch.num_rows:
                        continue
                yield self._project(section, batch, columns)

    def iter_batches(self, *, columns):
        """Stream our complete selected scientific population by requested field."""
        yield from self._read('iter_batches', columns=columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        """Read our exact scientific rectangle in each current section frame."""
        yield from self._read('query', columns=columns, bounds=bounds, predicate=predicate)

    def lookup_ids(self, ids, *, columns):
        """Resolve our bounded known-ID request across selected section indices."""
        yield from self._read('lookup_ids', columns=columns, ids=tuple(ids))

    def close(self):
        """Release private sparse-edit indexes owned by this collection read."""
        for section in getattr(self, 'sections', ()):
            if getattr(section, '_edit_index', None) is not None:
                section._edit_index.close()


def open_cell_dataset(path):
    """Open our current base or collection data kind by its declared descriptor.

    Both kinds are current: a base owns geometry, while a collection selects
    existing section bases and narrow result components. Retired wire formats
    cannot enter this reader.
    """
    path = Path(path)
    if path.is_dir():
        path = path / 'dataset.json'
    if path.stat().st_size > 64 * 1024**2:
        raise ValueError('Cell metadata exceeds its allowance')
    description = json.loads(path.read_bytes())
    if description['schema_version'] == CELL_COLLECTION_SCHEMA_VERSION:
        return CellDatasetCollection(path)
    return CellDataset(path)


def write_cell_dataset_collection(
    destination: str | Path, collection: CellDatasetCollection, *, scope: dict[str, object],
) -> CellDatasetDescription:
    """Publish our admitted selection using its retained population commitments.

    We preserve the selected sections, domains and completed source summary.
    Scope may add descriptive lineage while keeping the admitted population
    unchanged. We synchronize this compact descriptor; the handoff owner then
    reopens it and atomically selects the completed generation.
    """
    if not isinstance(collection, CellDatasetCollection):
        raise ValueError('Collection publication requires its admitted cell selection')
    active = sorted(collection.active_domains if collection.active_domains is not None
                    else collection.description['domain_inventories'])
    if (scope.get('active_analysis_domain_ids') != active
            or any(scope.get(name) != value for name, value in collection.description['scope'].items())):
        raise ValueError('Publication scope changes the admitted cell selection')
    description = {
        'schema_version': CELL_COLLECTION_SCHEMA_VERSION, 'scope': scope,
        'source_sha256': collection.selection_sha256,
        'sections': sorted(collection.description['sections'], key=lambda item: item['section_id']),
        'row_count': collection.row_count, 'complete': True,
        'domain_inventories': collection.description['domain_inventories'],
        'source_summary': collection.source_summary,
    }
    payload = json.dumps(description, sort_keys=True, separators=(',', ':')).encode()
    if len(payload) > 64 * 1024**2:
        raise ValueError('Collection metadata exceeds its allowance')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / 'dataset.json'
    with path.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return CellDatasetDescription(path, collection.row_count, CELL_COLLECTION_SCHEMA_VERSION,
                                  hashlib.sha256(payload).hexdigest())
