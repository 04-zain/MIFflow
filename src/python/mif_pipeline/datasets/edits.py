"""Sparse current native edits and explicitly created correction identities.

Only edited records occupy the private disk index. Clean geometry remains in
the published base and projected joins retain immutable physical row offsets.
"""

import hashlib
from itertools import groupby
import json
import math
from pathlib import Path
import pickle
import sqlite3
import tempfile
from uuid import UUID

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .categories import CategoryDictionary
from .cell_store import CellDataset, GROUP_BYTES, MAX_GROUP_ROWS, _BOUNDS, _LAYOUT_SCHEMA, _regular_snapshot
from .geometry import wkb_polygon, polygon_wkb, intersects_rectangle

EDIT_SCHEMA_VERSION = "mif-cell-edits/v1"


class EditIndex:
    """Bounded admission and disk lookup of one completed sparse checkpoint."""

    def __init__(self, base, reference):
        """Admit our exact completed delta and index its stable IDs on disk.

        i) We authenticate the descriptor against the immutable base and bound
        each Parquet read. ii) New identities require exact creation provenance;
        unsupported planes, repeated IDs and inconsistent counts are rejected.
        """
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise ValueError("A sparse edit selection requires its exact descriptor identity")
        self.path = Path(reference['path'])
        snapshot = _regular_snapshot(self.path)
        if self.path.stat().st_size > 16 * 1024**2:
            raise ValueError("Sparse edit metadata exceeds its allowance")
        payload = self.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != reference['sha256'] or _regular_snapshot(self.path) != snapshot:
            raise ValueError("The selected sparse edit descriptor changed")
        self.description = json.loads(payload)
        d = self.description
        if (d.get('schema_version') != EDIT_SCHEMA_VERSION or d.get('complete') is not True or
                d.get('population_id') != base.description['scope']['population_id'] or
                d.get('layout_version') != base.description['layout_version'] or
                d.get('base_sha256') != base.summary.sha256 or
                isinstance(d.get('geometry_revision'), bool) or not isinstance(d.get('geometry_revision'), int) or d['geometry_revision'] < 0):
            raise ValueError("Sparse edits do not belong to the selected immutable base")
        self.temporary = tempfile.TemporaryDirectory(prefix='mif-cell-edits-')
        self.connection = sqlite3.connect(Path(self.temporary.name) / 'edits.sqlite')
        self.connection.execute('PRAGMA cache_size=-8192')
        self.connection.execute('CREATE TABLE edits(cell_id TEXT PRIMARY KEY, revision INTEGER, deleted INTEGER, min_x REAL,min_y REAL,max_x REAL,max_y REAL,data BLOB,created_group_id INTEGER UNIQUE)')
        last_group = pc.max(base.layout['group_id']).as_py()
        self.minimum_created_group = 0 if last_group is None else last_group + 1
        count = 0
        self.labels = set()
        vocabulary_bytes = 0
        try:
            for part in d['parts']:
                relative = Path(part['file'])
                if relative.is_absolute() or len(relative.parts) != 1 or relative.suffix != '.parquet':
                    raise ValueError("Sparse edits require local completed Parquet parts")
                path = self.path.parent / relative
                original = _regular_snapshot(path)
                if path.stat().st_size != part['bytes']:
                    raise ValueError("Sparse edit part size changed")
                part_count = 0
                with pq.ParquetFile(path) as parquet:
                    for group in range(parquet.num_row_groups):
                        footer = parquet.metadata.row_group(group)
                        if footer.num_rows > MAX_GROUP_ROWS or sum(footer.column(i).total_uncompressed_size for i in range(footer.num_columns)) > GROUP_BYTES:
                            raise ValueError("Sparse edit group exceeds its bounded allowance")
                    for batch in parquet.iter_batches(batch_size=min(512, base.max_batch_rows)):
                        if batch.nbytes > GROUP_BYTES:
                            raise ValueError("Sparse edit batch exceeds its allowance")
                        ids = batch.column('cell_id').to_pylist()
                        actual = {value for found in base.lookup_ids(ids, columns=['cell_id']) for value in found.column('cell_id').to_pylist()}
                        for row in batch.to_pylist():
                            if (row['c'], row['z'], row['t']) != (-1, 0, 0):
                                raise ValueError("Sparse edits require the accepted image plane")
                            revision = row['geometry_revision']
                            if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= d['geometry_revision'] or not isinstance(row['deleted'], bool):
                                raise ValueError("Sparse edit revision or deletion state is invalid")
                            if not all(math.isfinite(row[name]) for name in ('x_level0', 'y_level0')):
                                raise ValueError("Sparse edited centroids must remain finite")
                            _, bounds = polygon_wkb(wkb_polygon(row['cell_geometry']))
                            row['metadata'] = dict(row['metadata'])
                            created_group = self._created_group(base, row, row['cell_id'] in actual)
                            if row['primary_phenotype'] is not None and row['primary_phenotype'] not in self.labels:
                                self.labels.add(row['primary_phenotype'])
                                vocabulary_bytes += len(row['primary_phenotype'].encode('utf-8')) + 128
                                if vocabulary_bytes > 64 * 1024**2:
                                    raise ValueError("Sparse manual class vocabulary exceeds its allowance")
                            try:
                                self.connection.execute('INSERT INTO edits VALUES(?,?,?,?,?,?,?,?,?)', (row['cell_id'], revision, row['deleted'], *bounds, pickle.dumps(row, protocol=5), created_group))
                            except sqlite3.IntegrityError as error:
                                raise ValueError('Sparse edits repeat a cell identity or created group') from error
                            part_count += 1
                if part_count != part['row_count'] or _regular_snapshot(path) != original:
                    raise ValueError("Sparse edit part changed or its row count differs")
                count += part_count
            if count != d['row_count']:
                raise ValueError("Sparse edit rows differ from their completed descriptor")
            self._validate_sources(base)
            self.connection.commit()
        except Exception:
            self.close()
            raise

    def _created_group(self, base, row, original):
        """Authenticate new IDs and their stable logical row-group identities."""
        metadata = row['metadata']
        value = metadata.get('mif.created_group_id')
        if value is None:
            if not original:
                raise ValueError('A novel sparse identity requires creation provenance')
            return None
        if original:
            raise ValueError('A created cell identity collides with the immutable base')
        try:
            group = int(value)
            valid_id = str(UUID(row['cell_id'])) == row['cell_id']
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError('A created cell requires a canonical UUID and group') from error
        if not valid_id or str(group) != value or not self.minimum_created_group <= group < 2**63 or row['geometry_revision'] <= 0:
            raise ValueError('A created cell requires a canonical UUID and unused nonnegative group')
        scope = base.description['scope']
        if (any(metadata.get(name) != row['cell_id'] for name in ('cell_id', 'mif.cell_id')) or
                any(metadata.get(name) != scope['population_id'] for name in ('mif.population_id', 'segmentation_population_id')) or
                metadata.get('cell_evidence_sha256') != base.summary.sha256 or
                metadata.get('section_id') != scope.get('section_id') or
                metadata.get('analysis_domain_id') not in scope.get('domains', {})):
            raise ValueError('A created cell belongs to a different accepted identity or domain')
        domain = scope['domains'][metadata['analysis_domain_id']]
        if metadata.get('generation_id') != domain.get('generation_id'):
            raise ValueError('A created cell belongs to a different accepted domain generation')
        for name in ('analysis_domain_kind', 'primary_compartment'):
            if name == 'primary_compartment' and metadata.get(name) is None:
                continue
            if metadata.get(name) not in base.dictionaries[name].labels:
                raise ValueError('A created cell changes its accepted scope categories')
        sources = json.loads(metadata.get('mif.correction_source_ids', 'null'))
        operation = metadata.get('mif.correction_operation')
        if (not isinstance(sources, list) or any(not isinstance(id, str) or not id for id in sources) or
                sources != sorted(set(sources)) or json.dumps(sources, separators=(',', ':'), ensure_ascii=False) != metadata['mif.correction_source_ids'] or
                not (operation == 'add_cell' and not sources or operation == 'split' and len(sources) == 1 or operation == 'merge' and len(sources) >= 2)):
            raise ValueError('A created cell requires exact correction source provenance')
        return group

    def _validate_sources(self, base):
        """Resolve correction ancestry in bounded batches without reading clean geometry."""
        cursor = self.connection.execute('SELECT created_group_id,data FROM edits WHERE created_group_id IS NOT NULL ORDER BY created_group_id')
        for group, payload in cursor:
            metadata = pickle.loads(payload)['metadata']
            sources = json.loads(metadata['mif.correction_source_ids'])
            for start in range(0, len(sources), base.max_batch_rows):
                ids = sources[start:start + base.max_batch_rows]
                original = {row['cell_id']: row['analysis_domain_id'] for batch in base.lookup_ids(ids, columns=['cell_id', 'analysis_domain_id']) for row in batch.to_pylist()}
                for id in ids:
                    if id in original:
                        domain = original[id]
                    else:
                        source = self.connection.execute('SELECT created_group_id,data FROM edits WHERE cell_id=?', (id,)).fetchone()
                        if source is None or source[0] is None or source[0] >= group:
                            raise ValueError('A correction source must exist before its created cell')
                        domain = pickle.loads(source[1])['metadata']['analysis_domain_id']
                    if domain != metadata['analysis_domain_id']:
                        raise ValueError('A correction source belongs to another analysis domain')

    def rows(self, ids):
        """Return edited values for one bounded group of immutable identities."""
        if not ids:
            return {}
        placeholders = ','.join('?' for _ in ids)
        return {id: pickle.loads(payload) for id, payload in self.connection.execute('SELECT cell_id,data FROM edits WHERE cell_id IN (' + placeholders + ')', tuple(ids))}

    def close(self):
        """Release our private lookup database and remove its temporary files."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
            self.temporary.cleanup()

    def __del__(self):
        """Release abandoned read scopes when their owning view is collected."""
        if hasattr(self, 'connection'):
            self.close()


class _CreatedCellDataset(CellDataset):
    """Unfiltered logical base: immutable rows followed by created one-row groups.

    Tombstones retain their alignment slot. Only the outer edited view filters
    deletions, so result components can keep exact row offsets across rounds.
    """

    def __init__(self, base, index):
        self.__dict__.update(base.__dict__)
        self._immutable = base
        self._created_index = index
        count = index.connection.execute('SELECT COUNT(*) FROM edits WHERE created_group_id IS NOT NULL').fetchone()[0]
        if base.layout.nbytes + count * 128 > getattr(base, 'max_index_bytes', 64 * 1024**2):
            raise ValueError('Created cell layout exceeds the selected index allowance')
        tables = [base.layout]
        cursor = index.connection.execute('SELECT created_group_id,min_x,min_y,max_x,max_y FROM edits WHERE created_group_id IS NOT NULL ORDER BY created_group_id')
        while rows := cursor.fetchmany(512):
            tables.append(pa.Table.from_pylist([dict(group_id=group, file='', row_group=0, row_count=1,
                decoded_bytes=0, **dict(zip(_BOUNDS, bounds))) for group, *bounds in rows], schema=_LAYOUT_SCHEMA))
        self.layout = pa.concat_tables(tables)
        self._created_layout_start = base.layout.num_rows
        self._created_group_ids = self.layout['group_id'].slice(base.layout.num_rows).to_numpy()
        self.bounds = np.column_stack([self.layout[name].to_numpy() for name in _BOUNDS])
        self.row_count = base.row_count + count

    def _groups(self, groups, columns):
        if not columns or any(name not in self.schema.names for name in columns):
            raise ValueError('Select existing cell dataset fields')
        for original, selected in groupby(groups, key=lambda group: group < self._immutable.layout.num_rows):
            if original:
                yield from self._immutable._groups(selected, columns)
            else:
                yield from self._created_groups(selected, columns)

    def _created_groups(self, groups, columns):
        for group in groups:
            group_id = self.layout['group_id'][int(group)].as_py()
            payload, = self._created_index.connection.execute('SELECT data FROM edits WHERE created_group_id=?', (group_id,)).fetchone()
            row = pickle.loads(payload)
            metadata = row['metadata']
            values = {name: None for name in columns}
            for name in columns:
                if name.startswith('present:'):
                    values[name] = False
                elif name in ('cell_id', 'cell_geometry', 'nucleus_geometry', 'x_level0', 'y_level0', 'x_um', 'y_um'):
                    values[name] = row[name]
                elif name in ('slide_id', 'section_id', 'analysis_domain_id', 'tissue_roi_id'):
                    values[name] = metadata.get(name)
                elif name == 'tissue_generation_id':
                    values[name] = metadata.get('generation_id')
                elif name == 'pipeline_run_id':
                    values[name] = metadata.get(name) or metadata.get('run_id')
                elif name in ('analysis_domain_kind', 'primary_compartment'):
                    values[name] = self.dictionaries[name].labels.index(metadata[name]) if metadata.get(name) is not None else None
            if any(name in columns for name in _BOUNDS):
                _, bounds = polygon_wkb(wkb_polygon(row['cell_geometry']))
                values.update((name, value) for name, value in zip(_BOUNDS, bounds) if name in columns)
            yield pa.RecordBatch.from_arrays([pa.array([values[name]], type=self.schema.field(name).type) for name in columns], names=columns)


def _with_created(base, index):
    if isinstance(base, _CreatedCellDataset) or hasattr(base, '_components'):
        return base
    if index.connection.execute('SELECT 1 FROM edits WHERE created_group_id IS NOT NULL LIMIT 1').fetchone() is None:
        return base
    return _CreatedCellDataset(base, index)


class EditedCellDataset(CellDataset):
    """Current sparse geometry and manual labels over a projected result view."""

    def __init__(self, base, index):
        """Select our sparse checkpoint and extend only the manual label map."""
        base = _with_created(base, index)
        self.__dict__.update(base.__dict__)
        self._view = base
        self._edit_index = index
        self.dictionaries = dict(base.dictionaries)
        prior = self.dictionaries['primary_phenotype']
        additions = sorted(index.labels - set(prior.labels))
        if additions:
            self.dictionaries['primary_phenotype'] = CategoryDictionary(prior.field, prior.dictionary_id + ':edits', (*prior.labels, *additions), (*(prior.label_ids or prior.labels), *additions), (*(prior.colors or (None,) * len(prior.labels)), *(None,) * len(additions)))
        self.row_count = base.row_count - index.connection.execute('SELECT COUNT(*) FROM edits WHERE deleted=1').fetchone()[0]

    def with_components(self, references, *, geometry_revision=0):
        """Join our selected numeric results before applying geometry/deletions.

        Immutable row alignment is retained during the result join. Older
        results remain applicable to clean cells; edited cells accept results
        calculated at or after their own shape revision.
        """
        if geometry_revision != self._edit_index.description['geometry_revision']:
            raise ValueError("Selected result geometry revision differs from its sparse checkpoint")
        from .components import ComponentCellDataset
        return EditedCellDataset(ComponentCellDataset(getattr(self._view, '_base', self._view), references, geometry_revision, edit_index=self._edit_index), self._edit_index)

    def _overlay(self, batch, columns):
        """Return our projected current shapes, labels and surviving rows.

        We replace only sparse edited positions. A manual label survives the
        result source against which it was entered; selecting a different
        primary result intentionally changes that cell's displayed class.
        """
        edits = self._edit_index.rows(batch.column('cell_id').to_pylist())
        if not edits:
            return batch.select(columns)
        values = {name: batch.column(name) for name in batch.schema.names}
        keep = np.ones(batch.num_rows, dtype=bool)
        for offset, id in enumerate(batch.column('cell_id').to_pylist()):
            row = edits.get(id)
            if row is None:
                continue
            if row['deleted']:
                keep[offset] = False
                continue
            _, bounds = polygon_wkb(wkb_polygon(row['cell_geometry']))
            overrides = {name: row[name] for name in ('cell_geometry', 'nucleus_geometry', 'x_level0', 'y_level0', 'x_um', 'y_um')}
            overrides.update(zip(_BOUNDS, bounds))
            if row['geometry_revision'] > 0 and not hasattr(self._view, '_components'):
                for name in values:
                    if name.startswith(('measurement:', 'state:')) or name in {'secondary_roi_ids', 'membership_generation_id', 'phenotyping_generation_id'}:
                        overrides[name] = None
                    elif name.startswith('present:'):
                        overrides[name] = False
            metadata = row['metadata']
            source = batch.column('mif.primary_class_source')[offset].as_py() if 'mif.primary_class_source' in batch.schema.names else None
            if metadata.get('mif.manual_class_override') == 'true' and (source is None or source == metadata.get('mif.manual_class_source')):
                label = row['primary_phenotype']
                overrides['primary_phenotype'] = None if label is None else self.dictionaries['primary_phenotype'].labels.index(label)
            for name, value in overrides.items():
                if name in values:
                    prior = values[name]
                    values[name] = pa.concat_arrays([prior.slice(0, offset), pa.array([value], type=prior.type), prior.slice(offset + 1)])
        return pa.RecordBatch.from_arrays([values[name] for name in columns], names=columns).filter(pa.array(keep))

    def _groups(self, groups, columns):
        """Read our selected base groups and release each overlaid batch."""
        required = list(dict.fromkeys(('cell_id', *columns, *(('mif.primary_class_source',) if 'primary_phenotype' in columns and 'mif.primary_class_source' in self._view.schema.names else ()))))
        for batch in self._view._groups(groups, required):
            result = self._overlay(batch, columns)
            if result.num_rows:
                yield result

    def lookup_ids(self, ids, *, columns):
        """Resolve immutable IDs strictly and created IDs through their disk index."""
        ids = tuple(dict.fromkeys(ids))
        if len(ids) > self.max_batch_rows:
            raise ValueError('Known-ID requests must fit the selected batch allowance')
        yield from CellDataset.lookup_ids(self, ids, columns=columns)
        if not ids:
            return
        placeholders = ','.join('?' for _ in ids)
        for group, in self._edit_index.connection.execute(
                'SELECT created_group_id FROM edits WHERE created_group_id IS NOT NULL AND cell_id IN (' + placeholders + ') ORDER BY created_group_id', ids):
            position = self._created_layout_start + int(np.searchsorted(self._created_group_ids, group))
            yield from self._groups((position,), columns)

    def iter_applied_batches(self, component_path, *, columns):
        """Return current edited rows accepted by the named scientific result."""
        required = list(dict.fromkeys(('cell_id', *columns)))
        for batch in self._view.iter_applied_batches(component_path, columns=required):
            result = self._overlay(batch, columns)
            if result.num_rows:
                yield result

    def query(self, bounds, *, columns, predicate='intersects'):
        """Return our exact spatial scope including independently moved edits.

        i) We read clean candidate groups through the retained base index.
        ii) We query the sparse bounds index and resolve only matching edited
        IDs, preserving exact outline, bounds or centroid inclusion semantics.
        """
        # Clean candidates use the immutable spatial index; sparse moved shapes
        # are queried separately so their old home group cannot hide them.
        edited_ids = self._edit_index.connection
        for batch in self._view.query(bounds, columns=list(dict.fromkeys(('cell_id', *columns))), predicate=predicate):
            ids = batch.column('cell_id').to_pylist()
            edits = self._edit_index.rows(ids)
            result = batch.filter(pa.array([id not in edits for id in ids])).select(columns)
            if result.num_rows:
                yield result
        cursor = edited_ids.execute('SELECT cell_id,data FROM edits WHERE deleted=0 AND min_x<=? AND max_x>=? AND min_y<=? AND max_y>=?', (bounds[2], bounds[0], bounds[3], bounds[1]))
        while rows := cursor.fetchmany(self.max_batch_rows):
            selected = []
            for id, payload in rows:
                row = pickle.loads(payload)
                if predicate == 'intersects' and not intersects_rectangle(row['cell_geometry'], bounds):
                    continue
                if predicate == 'centroid' and not (bounds[0] <= row['x_level0'] <= bounds[2] and bounds[1] <= row['y_level0'] <= bounds[3]):
                    continue
                selected.append(id)
            yield from self.lookup_ids(selected, columns=columns)
