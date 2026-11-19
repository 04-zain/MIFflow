"""Bounded projected joins for selected native cell result components."""

from collections import defaultdict
from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .categories import CategoryDictionary
from .cell_store import CellDataset, GROUP_BYTES, MAX_GROUP_ROWS, _regular_snapshot

COMPONENT_SCHEMA_VERSION = "mif-cell-component/v1"
_TYPES = {"float64": pa.float64(), "int32": pa.int32(), "bool": pa.bool_(), "string": pa.string(), "list_int32": pa.list_(pa.int32())}
_PROTECTED = {"cell_id", "applied", "cell_geometry", "nucleus_geometry", "min_x", "min_y", "max_x", "max_y", "x_level0", "y_level0", "x_um", "y_um"}


@dataclass(frozen=True)
class _Component:
    path: Path
    description: dict
    sha256: str
    fields: tuple[str, ...]
    sources: dict[str, str]
    constants: dict[str, str]
    dictionaries: dict[str, CategoryDictionary]
    groups: dict[int, tuple[dict, ...]]



def _selected_fields(description, fields):
    """Resolve our persisted phenotype namespace into selected logical fields.

    i) We admit directly stored columns unchanged. ii) Known display aliases
    refer to the same seven typed prediction columns and their dictionaries;
    selected lineage comes from compact owner attributes. No result values or
    repeated constant columns are copied into storage.
    """
    columns = description['columns']
    view = description.get('attributes', {}).get('phenotype_view', {})
    prefix = f"phenotype_view/{view.get('mode')}/{view.get('phenotype_generation_id')}/"
    aliases = {'primary_phenotype','phenotype_source','phenotype_components','marker_states','phenotype_confidence','cluster_id','cluster_label'}
    lineage = {'selected_phenotype_view':'mode','phenotyping_generation_id':'phenotype_generation_id','phenotyping_run_id':'run_id'}
    sources, constants = {}, {}
    for field in fields:
        if field in columns:
            sources[field] = field
        elif field in aliases and isinstance(view.get('mode'), str) and isinstance(view.get('phenotype_generation_id'), str) and prefix + field in columns:
            sources[field] = prefix + field
        elif field in lineage and isinstance(view.get(lineage[field]), str) and view[lineage[field]]:
            constants[field] = view[lineage[field]]
        else:
            raise ValueError("The component does not define its selected typed result fields")
    if any(columns[source] not in _TYPES for source in sources.values()):
        raise ValueError("The component does not define its selected typed result fields")
    return sources, constants

def _integer(value, minimum=0):
    """Admit our integer row/group metadata without accepting Boolean values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("A cell component requires nonnegative integer group metadata")
    return value


def _resolve(base, references, revision, edit_index=None):
    """Admit our selected scopes and their bounded physical alignment metadata.

    We confirm current population, immutable layout, typed fields and complete
    base-group coverage before reading any result rows. A sparse checkpoint
    permits older results for clean cells; per-cell shape revisions control
    their scientific applicability during the projected join.
    """
    if not isinstance(references, dict):
        raise ValueError("Select cell component fields by their exact current scope")
    _integer(revision)
    paths = defaultdict(set)
    scopes = defaultdict(set)
    for field, selected in references.items():
        if not isinstance(field, str) or not field or field in _PROTECTED or not isinstance(selected, dict):
            raise ValueError("Cell components require selected scalar result fields")
        for scope, path in selected.items():
            if not isinstance(scope, str) or not scope:
                raise ValueError("Cell components require an exact selected scope")
            descriptor = Path(path)
            paths[descriptor].add(field)
            scopes[descriptor].add(scope)
    admitted = []
    metadata_bytes = 0
    group_rows = {base.layout['group_id'][i].as_py(): base.layout['row_count'][i].as_py() for i in range(base.layout.num_rows)}
    for path, fields in paths.items():
        metadata_bytes += path.stat().st_size
        if metadata_bytes > 64 * 1024**2:
            raise ValueError("Selected cell component metadata exceeds its allowance")
        original = _regular_snapshot(path)
        payload = path.read_bytes()
        description = json.loads(payload)
        if _regular_snapshot(path) != original:
            raise ValueError("A selected component descriptor changed during admission")
        if (description.get('schema_version') != COMPONENT_SCHEMA_VERSION or description.get('complete') is not True or
                description.get('population_id') != base.description['scope']['population_id'] or
                description.get('layout_version') != base.description['layout_version'] or
                (description.get('geometry_revision') != revision if edit_index is None else
                 not isinstance(description.get('geometry_revision'), int) or isinstance(description.get('geometry_revision'), bool) or not 0 <= description['geometry_revision'] <= revision) or
                scopes[path] != {description.get('scope_key')}):
            raise ValueError("A selected cell component belongs to a different population, layout, scope or geometry revision")
        columns = description['columns']
        sources, constants = _selected_fields(description, fields)
        dictionaries = {}
        for field, source in sources.items():
            if source in description['dictionaries']:
                dictionary = CategoryDictionary.from_mapping(description['dictionaries'][source])
                if dictionary.field != source or columns[source] not in {'int32','list_int32'}:
                    raise ValueError("A result category dictionary does not match its primitive field")
                dictionaries[field] = CategoryDictionary(field, dictionary.dictionary_id, dictionary.labels, dictionary.label_ids, dictionary.colors)
        if any(field in base.dictionaries and field not in dictionaries for field in sources):
            raise ValueError("A category result must retain its own frozen field dictionary")
        groups = defaultdict(list)
        total = 0
        for item in description['groups']:
            group = _integer(item['group_id'])
            start = _integer(item['base_row_offset'])
            rows = _integer(item['row_count'], 1)
            _integer(item['row_group'])
            relative = Path(item['file'])
            if relative.is_absolute() or len(relative.parts) != 1 or relative.suffix != '.parquet':
                raise ValueError("A component group references an invalid local part")
            if group not in group_rows or rows > MAX_GROUP_ROWS or start + rows > group_rows[group]:
                raise ValueError("A component group exceeds its immutable base group")
            groups[group].append(item)
            total += rows
        if total != _integer(description['row_count']):
            raise ValueError("Component groups disagree with the completed result count")
        for items in groups.values():
            items.sort(key=lambda item: item['base_row_offset'])
            previous_end = 0
            for item in items:
                if item['base_row_offset'] != previous_end:
                    raise ValueError("A completed component must cover its aligned base group without gaps")
                previous_end = item['base_row_offset'] + item['row_count']
            if previous_end != group_rows[items[0]['group_id']]:
                raise ValueError("A completed component does not cover its immutable base group")
        admitted.append(_Component(path, description, hashlib.sha256(payload).hexdigest(), tuple(sorted(fields)), sources, constants, dictionaries, {group: tuple(items) for group, items in groups.items()}))
    return admitted


class ComponentCellDataset(CellDataset):
    """One current result selection joined while releasing each base batch."""

    def __init__(self, base, references, geometry_revision, *, edit_index=None):
        """Resolve our result schema and freeze field-local primitive remappings.

        We retain compact component metadata and result dictionaries. No base
        geometry or whole-population result array is decoded by this operation.
        Conflicting scientific types or retained label identities stop admission.
        """
        if edit_index is not None:
            from .edits import _with_created
            base = _with_created(base, edit_index)
        self.__dict__.update(base.__dict__)
        self._base = base
        self._edit_index = edit_index
        self._components = _resolve(base, references, geometry_revision, edit_index)
        fields = {field.name: field for field in base.schema}
        self.dictionaries = dict(base.dictionaries)
        self._remaps = {}
        for component in self._components:
            for name in component.fields:
                dtype = pa.string() if name in component.constants else _TYPES[component.description['columns'][component.sources[name]]]
                if name in fields and fields[name].type != dtype:
                    raise ValueError("A component changes an existing cell field's scientific type")
                fields.setdefault(name, pa.field(name, dtype))
                if name.startswith(('measurement:', 'state:')):
                    fields.setdefault('present:' + name, pa.field('present:' + name, pa.bool_()))
        for component in self._components:
            for name in component.fields:
                if name == 'primary_phenotype' or name.endswith('/primary_phenotype'):
                    source_field = 'mif.primary_class_source' if name == 'primary_phenotype' else name.rsplit('/', 1)[0] + '/class_source'
                    fields.setdefault(source_field, pa.field(source_field, pa.string()))
        self.schema = pa.schema(list(fields.values()), metadata=base.schema.metadata)
        for name in {name for component in self._components for name in component.dictionaries}:
            dictionaries = [*( [base.dictionaries[name]] if name in base.dictionaries else []), *(component.dictionaries[name] for component in self._components if name in component.dictionaries)]
            entries = []
            positions = {}
            meanings = {}
            if name == 'primary_phenotype' and not any('Unclassified' in dictionary.labels for dictionary in dictionaries):
                entries.append(('Unclassified', 'Unclassified', None));positions['Unclassified'] = 0;meanings['Unclassified'] = ('Unclassified', None)
            for dictionary in dictionaries:
                for index, label in enumerate(dictionary.labels):
                    label_id = dictionary.label_ids[index] if dictionary.label_ids else label
                    color = dictionary.colors[index] if dictionary.colors else None
                    meaning = (label, color)
                    if label_id in meanings and meanings[label_id] != meaning:
                        raise ValueError("Selected category results disagree on a retained label identity")
                    if label_id not in positions:
                        positions[label_id] = len(entries);entries.append((label_id, label, color));meanings[label_id] = meaning
            if len(entries) > 2**31:
                raise ValueError("Selected category mappings exceed Int32")
            identity = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.dictionaries[name] = CategoryDictionary(name, identity, tuple(item[1] for item in entries), tuple(item[0] for item in entries), tuple(item[2] for item in entries))
            for component in self._components:
                if name in component.dictionaries:
                    dictionary = component.dictionaries[name]
                    self._remaps[(component.path, name)] = pa.array([positions[dictionary.label_ids[i] if dictionary.label_ids else label] for i, label in enumerate(dictionary.labels)], type=pa.int32())
        self.description = {**base.description, 'arrow_schema': base64.b64encode(self.schema.serialize()).decode(), 'dictionaries': {name: dictionary.to_mapping() for name, dictionary in self.dictionaries.items()}, 'selected_components': references}
        self.last_component_reads = {"groups": 0, "last": None}

    def _read(self, component, group, fields):
        """Read our one projected aligned result group and verify its stability."""
        path = component.path.parent / group['file']
        original = _regular_snapshot(path)
        parquet = pq.ParquetFile(path)
        number = group['row_group']
        if number >= parquet.num_row_groups or parquet.metadata.row_group(number).num_rows != group['row_count']:
            raise ValueError("A component part disagrees with its aligned group count")
        physical = list(dict.fromkeys(component.sources[field] for field in fields if field in component.sources))
        selected = ['cell_id', 'applied', *physical]
        footer = parquet.metadata.row_group(number)
        if sum(footer.column(i).total_uncompressed_size for i in range(footer.num_columns) if footer.column(i).path_in_schema.split('.')[0] in selected) > GROUP_BYTES:
            raise ValueError("A projected component group exceeds the row-group byte allowance")
        table = parquet.read_row_group(number, columns=selected).combine_chunks()
        parquet.close()
        if _regular_snapshot(path) != original:
            raise ValueError("A cell component part changed while its projected group was read")
        if table.nbytes > GROUP_BYTES or table['applied'].null_count or table['cell_id'].null_count:
            raise ValueError("A cell component exceeds its group allowance or lacks alignment keys")
        table = pa.table({'cell_id':table['cell_id'], 'applied':table['applied'], **{field:pa.array([component.constants[field]] * table.num_rows, type=pa.string()) if field in component.constants else table[component.sources[field]] for field in fields}})
        for field in fields:
            observed = table[field].type
            expected = self.schema.field(field).type
            if observed != expected:
                if pa.types.is_list(observed) and pa.types.is_list(expected) and observed.value_type == expected.value_type:
                    if pc.list_flatten(table[field]).null_count:
                        raise ValueError("A category component contains unavailable elements inside a declared label list")
                    table = table.set_column(table.schema.get_field_index(field), field, table[field].cast(expected))
                else:
                    raise ValueError("A component part changes its declared result field type")
        self.last_component_reads['groups'] += 1
        self.last_component_reads['last'] = {'file': str(path), 'row_group': number, 'columns': selected}
        return table

    def iter_applied_batches(self, component_path, *, columns):
        """Project only cells accepted by one current scientific calculation.

        An unapplied aligned row can represent a deleted cell or a cell outside
        this calculation. It must not inherit an older value into model input.
        """
        path = Path(component_path)
        components = [item for item in self._components if item.path == path]
        if len(components) != 1:
            raise ValueError("Select an admitted component's accepted cell scope")
        component = components[0]
        required = list(dict.fromkeys(('cell_id', *columns)))
        for index in range(self.layout.num_rows):
            group_id = self.layout['group_id'][index].as_py()
            items = component.groups.get(group_id, ())
            if not items:
                continue
            offset = 0
            current = None
            mask = None
            for batch in self._groups((index,), required):
                for item in items:
                    start = max(offset, item['base_row_offset'])
                    stop = min(offset + batch.num_rows, item['base_row_offset'] + item['row_count'])
                    if stop <= start:
                        continue
                    if current is not item:
                        mask = self._read(component, item, [])
                        current = item
                    incoming = mask.slice(start - item['base_row_offset'], stop - start)
                    selected = batch.slice(start - offset, stop - start)
                    if pc.all(pc.equal(selected.column('cell_id'), incoming['cell_id'])).as_py() is not True:
                        raise ValueError("An accepted result mask disagrees with its immutable cell IDs")
                    applied = incoming['applied'].chunk(0)
                    if self._edit_index is not None:
                        ids = selected.column('cell_id').to_pylist()
                        edits = self._edit_index.rows(ids)
                        current_revision = pa.array([edits.get(id, {}).get('geometry_revision', 0) <= component.description['geometry_revision'] for id in ids])
                        applied = pc.and_(applied, current_revision)
                    filtered = selected.filter(applied)
                    if filtered.num_rows:
                        yield filtered.select(columns)
                offset += batch.num_rows

    def _remap(self, component, name, values):
        """Resolve our local scalar/list codes into the selected field dictionary."""
        mapping = self._remaps.get((component.path, name))
        if mapping is None:
            return values
        codes = pc.list_flatten(values) if pa.types.is_list(values.type) else values
        invalid = pc.or_(pc.less(codes, 0), pc.greater_equal(codes, len(mapping)))
        if pc.any(invalid).as_py():
            raise ValueError("A component contains an undeclared category code")
        resolved = pc.take(mapping, codes)
        if pa.types.is_list(values.type):
            offsets = pc.subtract(values.offsets, values.offsets[0]).cast(pa.int32())
            return pa.ListArray.from_arrays(offsets, resolved, mask=values.is_null())
        return resolved

    def _groups(self, groups, columns):
        """Join our requested results within immutable physical base offsets.

        i) We project base fields and clear shape-dependent stale values for
        edited cells. ii) We admit one aligned result group at a time and apply
        only current-revision rows. iii) We yield primitive projected batches
        while releasing completed group buffers and preserving null validity.
        """
        if not columns or any(name not in self.schema.names for name in columns):
            raise ValueError("Select existing cell dataset fields")
        required = list(dict.fromkeys(('cell_id', *(name for name in columns if name in self._base.schema.names))))
        self.last_component_reads = {"groups": 0, "last": None}
        for group in groups:
            group_id = self.layout['group_id'][int(group)].as_py()
            selected = []
            for component in self._components:
                fields = [name for name in component.fields if name in columns or 'present:' + name in columns or (name == 'primary_phenotype' and 'mif.primary_class_source' in columns) or (name.endswith('/primary_phenotype') and name.rsplit('/',1)[0]+'/class_source' in columns)]
                if fields:
                    selected.extend((component, item, fields) for item in component.groups.get(group_id, ()))
            offset = 0
            cache = {}
            for base_batch in self._base._groups((group,), required):
                values = {name: base_batch.column(name) if name in base_batch.schema.names else pa.array(np.zeros(base_batch.num_rows, dtype=bool)) if name.startswith('present:') else pa.nulls(base_batch.num_rows, type=self.schema.field(name).type) for name in columns}
                edits = self._edit_index.rows(base_batch.column('cell_id').to_pylist()) if self._edit_index is not None else {}
                revisions = np.array([edits.get(id, {}).get('geometry_revision', 0) for id in base_batch.column('cell_id').to_pylist()], dtype=np.int64)
                changed = pa.array(revisions > 0)
                derived = {'secondary_roi_ids', 'selected_phenotype_view', 'phenotyping_generation_id', 'membership_generation_id', *(name for component in self._components for name in component.fields)}
                for name, prior in tuple(values.items()):
                    if name.startswith(('measurement:', 'state:', 'present:')) or name in derived:
                        replacement = pa.array(np.zeros(base_batch.num_rows, dtype=bool)) if name.startswith('present:') else pa.nulls(base_batch.num_rows, type=prior.type)
                        values[name] = pc.if_else(changed, replacement, prior)
                for component, item, fields in selected:
                    start = max(offset, item['base_row_offset'])
                    stop = min(offset + base_batch.num_rows, item['base_row_offset'] + item['row_count'])
                    if stop <= start:
                        continue
                    key = (component.path, item['file'], item['row_group'])
                    if key not in cache:
                        # At most one admitted result group remains resident;
                        # selecting many scopes cannot multiply the read cache.
                        cache.clear()
                        cache[key] = self._read(component, item, [name for name in fields if name in columns])
                    table = cache[key]
                    begin = start - item['base_row_offset']
                    length = stop - start
                    incoming = table.slice(begin, length)
                    original_ids = base_batch.column('cell_id').slice(start - offset, length)
                    if pc.all(pc.equal(original_ids, incoming['cell_id'])).as_py() is not True:
                        raise ValueError("A component's stable IDs disagree with the immutable base row order")
                    applied = pc.and_(incoming['applied'].chunk(0), pa.array(revisions[start-offset:stop-offset] <= component.description['geometry_revision']))
                    for name in fields:
                        if name in values:
                            prior = values[name]
                            replacement = self._remap(component, name, incoming[name].chunk(0))
                            joined = pc.if_else(applied, replacement, prior.slice(start - offset, length))
                            values[name] = pa.concat_arrays([prior.slice(0, start - offset), joined, prior.slice(stop - offset)])
                        source_field = 'mif.primary_class_source' if name == 'primary_phenotype' else name.rsplit('/', 1)[0] + '/class_source' if name.endswith('/primary_phenotype') else None
                        if source_field in values:
                            prior = values[source_field]
                            source = str(component.path.parent) + ':' + component.description['request_sha256']
                            joined = pc.if_else(applied, pa.array([source] * length), prior.slice(start-offset, length))
                            values[source_field] = pa.concat_arrays([prior.slice(0,start-offset), joined, prior.slice(stop-offset)])
                        presence = 'present:' + name
                        if name.startswith(('measurement:', 'state:')) and presence in values:
                            prior = values[presence]
                            joined = pc.or_(applied, prior.slice(start - offset, length))
                            values[presence] = pa.concat_arrays([prior.slice(0, start - offset), joined, prior.slice(stop - offset)])
                    if stop == item['base_row_offset'] + item['row_count']:
                        cache.pop(key, None)
                yield pa.RecordBatch.from_arrays([values[name] for name in columns], names=columns)
                offset += base_batch.num_rows
