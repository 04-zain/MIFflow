"""Spatially ordered cell datasets with bounded Parquet groups and ID lookup.

We preserve finalized cell IDs and geometry while an on-disk sort establishes
Morton locality. Readers resolve the compact layout once, then project only
the groups and fields required by the requested scientific or display scope.
"""

from dataclasses import dataclass
import base64
import hashlib
import json
import math
import os
import sys
from pathlib import Path
import pickle
import shutil
import stat
import sqlite3
import struct
import tempfile
from uuid import uuid4

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from mif_pipeline.pipeline_core.models import CellRecord
from .categories import CategoryDictionary
from .geometry import polygon_wkb, wkb_polygon, intersects_rectangle
from .identity import SortedIdDigest, SortedIdMappingDigest

CELL_DATASET_SCHEMA_VERSION = "mif-cell-dataset/v1"
HOME_BLOCK = 4096
MAX_GROUP_ROWS = 8192
GROUP_BYTES = 8 * 1024**2
PART_BYTES = 64 * 1024**2
_CATEGORIES = ("analysis_domain_kind", "primary_compartment", "primary_phenotype", "phenotype_source", "cluster_id", "cluster_label")
_LIST_CATEGORIES = ("phenotype_components", "secondary_roi_ids")
_FLOATS = ("x_level0", "y_level0", "x_um", "y_um", "phenotype_confidence")
_IDENTIFIERS = ("cell_id", "slide_id", "section_id", "analysis_domain_id", "tissue_roi_id", "tissue_generation_id", "phenotyping_generation_id", "pipeline_run_id", "membership_run_id", "membership_generation_id")
_BOUNDS = ("min_x", "min_y", "max_x", "max_y")
_LAYOUT_SCHEMA = pa.schema([
    ("group_id", pa.int64()), ("file", pa.string()), ("row_group", pa.int32()),
    ("row_count", pa.int32()), ("decoded_bytes", pa.int64()),
    *((field, pa.float64()) for field in _BOUNDS),
])
_ID_SCHEMA = pa.schema([("cell_id", pa.string()), ("group_id", pa.int64()), ("row_offset", pa.int32())])


def _regular_snapshot(path):
    """Reject linked parts and detect ordinary mutation during a bounded read."""
    path = Path(path)
    parent = path.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or getattr(parent, 'st_file_attributes', 0) & 0x400:
        raise ValueError("A cell dataset part requires its real published parent")
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or getattr(details, 'st_file_attributes', 0) & 0x400:
        raise ValueError("A cell dataset part must be one regular file")
    return (parent.st_dev, parent.st_ino, details.st_dev, details.st_ino, details.st_mode, details.st_size, details.st_mtime_ns, details.st_ctime_ns)


@dataclass(frozen=True)
class CellDatasetDescription:
    """Location and compact publication summary of one completed population."""
    path: Path
    row_count: int
    schema_version: str
    sha256: str


def _spread(value):
    value &= 0xffffffff
    value = (value | value << 16) & 0x0000ffff0000ffff
    value = (value | value << 8) & 0x00ff00ff00ff00ff
    value = (value | value << 4) & 0x0f0f0f0f0f0f0f0f
    value = (value | value << 2) & 0x3333333333333333
    return (value | value << 1) & 0x5555555555555555


def _morton(x, y):
    return struct.pack(">Q", _spread(x) | _spread(y) << 1)


def _prepared_row(record):
    """Separate exact geometry, measurements and categories from a final cell."""
    if not isinstance(record, CellRecord):
        raise ValueError("Cell datasets require finalized CellRecord inputs")
    row = record.to_export_mapping()
    x, y = row["x_level0"], row["y_level0"]
    if not all(math.isfinite(value) and 0 <= value < 2**44 for value in (x,y)):
        raise ValueError("Cell centroids must be finite nonnegative level-zero coordinates")
    cell_wkb, bounds = polygon_wkb(row["cell_geometry"])
    row["cell_geometry"] = cell_wkb
    row["nucleus_geometry"] = polygon_wkb(row["nucleus_geometry"])[0] if row["nucleus_geometry"] else None
    row.update(zip(_BOUNDS, bounds))
    measurements = row.pop("marker_measurements")
    states = row.pop("marker_states")
    for name,value in measurements.items():
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))):
            raise ValueError(f"Measurement {name} requires a numeric scalar or null")
        row["measurement:" + name] = value
        row["present:measurement:" + name] = True
    for name,value in states.items():
        if value is not None and not isinstance(value,str):
            raise ValueError(f"Marker state {name} requires a declared string label or null")
        row["state:" + name] = value
        row["present:state:" + name] = True
    bx, by = math.floor(x/HOME_BLOCK), math.floor(y/HOME_BLOCK)
    return row, _morton(bx,by), _morton(math.floor(x-bx*HOME_BLOCK), math.floor(y-by*HOME_BLOCK))


def _schema(measurements, categories, dictionary_version):
    fields = [pa.field(name,pa.string()) for name in _IDENTIFIERS]
    fields += [pa.field(name,pa.float64()) for name in _FLOATS]
    fields += [pa.field(name,pa.int32()) for name in categories]
    fields += [pa.field("present:measurement:"+name,pa.bool_()) for name in sorted(measurements)]
    fields += [pa.field("present:"+name,pa.bool_()) for name in categories if name.startswith("state:")]
    fields += [pa.field(name,pa.list_(pa.field("element",pa.int32()))) for name in _LIST_CATEGORIES]
    fields += [pa.field("cell_geometry",pa.binary(),nullable=False),pa.field("nucleus_geometry",pa.binary())]
    fields += [pa.field(name,pa.float64(),nullable=False) for name in _BOUNDS]
    fields += [pa.field("measurement:"+name,pa.float64()) for name in sorted(measurements)]
    geo = {"version":"1.1.0","primary_column":"cell_geometry","columns":{name:{"encoding":"WKB","geometry_types":["Polygon","MultiPolygon"],"crs":None,"edges":"planar"} for name in ("cell_geometry","nucleus_geometry")}}
    return pa.schema(fields,metadata={b"geo":json.dumps(geo).encode(),b"coordinate_system":b"global_level0_pixels",b"schema_version":CELL_DATASET_SCHEMA_VERSION.encode(),b"dictionary_version":dictionary_version.encode()})


def _close_part(writer, path, schema, count):
    """Close, synchronize and inspect bounded metadata once before publication."""
    writer.close()
    # Windows needs a writable handle to flush this completed output file.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
    footer = pq.ParquetFile(path)
    if footer.metadata.num_rows != count or not footer.schema_arrow.equals(schema,check_metadata=True):
        raise ValueError("A completed cell part has an unexpected schema or row count")
    return path.stat().st_size


def _sync_directory(path):
    """Persist directory entries on platforms exposing directory fsync."""
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_index(path, schema, count):
    """Check the completed narrow index once before publication."""
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
    footer = pq.ParquetFile(path)
    if not footer.schema_arrow.equals(schema, check_metadata=True) or footer.metadata.num_rows != count:
        raise ValueError("A completed cell index has an invalid schema or row count")


def write_cell_dataset(destination, records, *, scope, source_sha256=None,
                       max_rows=MAX_GROUP_ROWS, max_group_bytes=GROUP_BYTES,
                       target_part_bytes=PART_BYTES, max_record_bytes=PART_BYTES,
                       max_index_bytes=64*1024**2):
    """Publish an immutable spatial dataset from a possibly unordered cell stream.

    i) We retain accepted values in a private SQLite spill file, gather field
    definitions and freeze each field dictionary. SQLite's disk-backed sort uses
    a bounded cache; it never requires the whole cell population in Python.
    ii) We stream Morton-ordered rows into bounded Arrow groups and packed files,
    recording full-object bounds and known-ID locations. An oversized valid
    object is isolated within the explicit max_record_bytes allowance.
    iii) Completed parts, layout, sorted ID index and description are published
    together by moving the finished sibling directory. A failed write leaves
    the preceding selected dataset untouched and removes only its own staging.

    Existing destinations are reused only for the same recorded source digest
    and scope. Duplicate finalized IDs, unsupported measurement values and work
    units exceeding their byte allowance fail before a new result is published.
    """
    destination = Path(destination)
    if not isinstance(scope,dict) or not scope.get("population_id"):
        raise ValueError("A cell dataset requires its segmentation population identity")
    if not 0 < max_rows <= MAX_GROUP_ROWS or min(max_group_bytes,target_part_bytes,max_record_bytes,max_index_bytes) <= 0:
        raise ValueError("Cell dataset limits must be positive and groups contain at most 8192 rows")
    if source_sha256 is not None and (len(source_sha256)!=64 or any(c not in '0123456789abcdef' for c in source_sha256)):
        raise ValueError("The cell source identity must be a SHA-256 digest")
    if destination.exists():
        existing = CellDataset(destination)
        if source_sha256 is not None and existing.description.get("source_sha256") == source_sha256 and existing.description["scope"] == scope:
            return existing.summary
        raise FileExistsError("A different cell dataset already occupies this destination")
    destination.parent.mkdir(parents=True,exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cells-",dir=destination.parent))
    connection = None
    writer = None
    try:
        connection = sqlite3.connect(staging/"sort.sqlite")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("CREATE TABLE cells(block BLOB,local BLOB,cell_id TEXT,data BLOB)")
        connection.execute("CREATE TABLE locations(cell_id TEXT,group_id INTEGER,row_offset INTEGER,domain_id TEXT)")
        measurements = set()
        category_labels = {name:set() for name in (*_CATEGORIES,*_LIST_CATEGORIES)}
        count = 0
        vocabulary_bytes = 0
        for record in records:
            row,block,local = _prepared_row(record)
            serialized = pickle.dumps(row,protocol=5)
            if len(serialized) > max_record_bytes:
                raise ValueError("One cell exceeds the allocated geometry/record bytes")
            measurements.update(name.removeprefix("measurement:") for name in row if name.startswith("measurement:"))
            for name in row:
                if name.startswith("state:"):
                    category_labels.setdefault(name,set())
            for name,labels in category_labels.items():
                value=row.get(name)
                if value is not None:
                    for label in (value if name in _LIST_CATEGORIES else (value,)):
                        if label not in labels:
                            vocabulary_bytes += len(label.encode("utf-8")) + 128
                            if vocabulary_bytes > max_index_bytes:
                                raise ValueError("Category vocabularies exceed their index allowance")
                            labels.add(label)
            connection.execute("INSERT INTO cells VALUES(?,?,?,?)",(block,local,row["cell_id"],serialized))
            count += 1
        connection.commit()
        duplicate = connection.execute("SELECT cell_id FROM cells GROUP BY cell_id HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if duplicate:
            raise ValueError(f"Duplicate finalized cell ID: {duplicate[0]}")
        dictionary_version = uuid4().hex
        dictionaries = {name:CategoryDictionary.from_labels(name,dictionary_version+":"+name,sorted(labels),zero_label="Unclassified" if name=="primary_phenotype" else None) for name,labels in category_labels.items()}
        category_fields = [name for name in dictionaries if name not in _LIST_CATEGORIES]
        schema = _schema(measurements,category_fields,dictionary_version)
        (staging/"base").mkdir()
        layout=[]; parts=[]; group_id=0; part_index=0; part_rows=0; part_bytes=0; part_groups=0
        batch=[]; batch_bytes=0

        def flush_group():
            nonlocal writer,group_id,part_index,part_rows,part_bytes,part_groups,batch,batch_bytes
            if not batch:
                return
            columns={field.name:[row.get(field.name) for row in batch] for field in schema}
            for name in category_fields:
                columns[name]=dictionaries[name].encode(columns[name])
            for name in _LIST_CATEGORIES:
                columns[name]=[dictionaries[name].encode(values).to_pylist() for values in columns[name]]
            table=pa.Table.from_pydict(columns,schema=schema)
            if table.nbytes > max_group_bytes and len(batch)>1:
                # Exact Arrow admission can split a conservative spill estimate.
                midpoint = len(batch)//2
                halves = (batch[:midpoint], batch[midpoint:])
                for subset in halves:
                    batch = subset
                    batch_bytes = sum(len(pickle.dumps(row,protocol=5)) for row in batch)
                    flush_group()
                return
            if table.nbytes > max_record_bytes:
                raise ValueError("One decoded cell exceeds the allocated record bytes")
            if writer is not None and part_bytes+table.nbytes > target_part_bytes:
                completed_path=staging/"base"/f"part-{part_index:05d}.parquet"
                size=_close_part(writer,completed_path,schema,part_rows)
                parts.append({"file":str(completed_path.relative_to(staging)),"rows":part_rows,"bytes":size})
                writer=None; part_index+=1
            if writer is None:
                part_rows=part_bytes=part_groups=0
                part_path_new=staging/"base"/f"part-{part_index:05d}.parquet"
                writer=pq.ParquetWriter(part_path_new,schema,compression="zstd",compression_level=1)
                # The current path is derived from the writer's assigned part index.
            current_path=staging/"base"/f"part-{part_index:05d}.parquet"
            writer.write_table(table,row_group_size=len(batch))
            group_bounds=tuple((min if index<2 else max)(row[field] for row in batch) for index,field in enumerate(_BOUNDS))
            layout.append(dict(group_id=group_id,file=str(current_path.relative_to(staging)),row_group=part_groups,row_count=len(batch),decoded_bytes=table.nbytes,**dict(zip(_BOUNDS,group_bounds))))
            if len(layout)*128 > max_index_bytes:
                raise ValueError("The row-group layout exceeds its index allowance")
            connection.executemany("INSERT INTO locations VALUES(?,?,?,?)",((row["cell_id"],group_id,index,row["analysis_domain_id"]) for index,row in enumerate(batch)))
            part_rows+=len(batch);part_bytes+=table.nbytes;part_groups+=1;group_id+=1
            batch=[];batch_bytes=0

        # The immutable spill rows are internal writer state, never an external pickle input.
        for (serialized,) in connection.execute("SELECT data FROM cells ORDER BY block,local,cell_id"):
            row=pickle.loads(serialized)
            estimate=len(serialized)
            if batch and (len(batch)>=max_rows or batch_bytes+estimate>max_group_bytes):
                flush_group()
            batch.append(row);batch_bytes+=estimate
        flush_group()
        if writer is not None:
            part_path=staging/"base"/f"part-{part_index:05d}.parquet"
            size=_close_part(writer,part_path,schema,part_rows)
            writer=None
            parts.append({"file":str(part_path.relative_to(staging)),"rows":part_rows,"bytes":size})
        layout_table = pa.Table.from_pylist(layout,schema=_LAYOUT_SCHEMA)
        if layout_table.nbytes + vocabulary_bytes > max_index_bytes:
            raise ValueError("The layout and category vocabularies exceed their index allowance")
        pq.write_table(layout_table,staging/"layout.parquet",compression="zstd")
        _validate_index(staging/"layout.parquet", _LAYOUT_SCHEMA, len(layout))
        connection.commit()
        id_groups=[]
        ids_digest=SortedIdDigest()
        mapping_digest=SortedIdMappingDigest()
        domain_digests={domain:SortedIdDigest() for domain in scope.get("domains", {})}
        id_writer=pq.ParquetWriter(staging/"id-index.parquet",_ID_SCHEMA,compression="zstd")
        try:
            cursor=connection.execute("SELECT cell_id,group_id,row_offset,domain_id FROM locations ORDER BY cell_id")
            while rows:=cursor.fetchmany(MAX_GROUP_ROWS):
                for cell_id,_,_,domain_id in rows:
                    ids_digest.add(cell_id)
                    mapping_digest.add(cell_id,domain_id)
                    if domain_id not in domain_digests:
                        domain_digests[domain_id]=SortedIdDigest()
                    domain_digests[domain_id].add(cell_id)
                table=pa.Table.from_arrays([pa.array([r[i] for r in rows],type=field.type) for i,field in enumerate(_ID_SCHEMA)],schema=_ID_SCHEMA)
                id_writer.write_table(table,row_group_size=len(rows))
                id_groups.append({"row_group":len(id_groups),"minimum":rows[0][0],"maximum":rows[-1][0],"row_count":len(rows)})
        finally:
            id_writer.close()
        _validate_index(staging/"id-index.parquet", _ID_SCHEMA, count)
        connection.close();connection=None
        (staging/"sort.sqlite").unlink()
        description={"schema_version":CELL_DATASET_SCHEMA_VERSION,"scope":scope,"source_sha256":source_sha256,"row_count":count,"cell_ids_sha256":ids_digest.hexdigest(),"domain_cell_mapping_sha256":mapping_digest.hexdigest(),"domain_inventories":{domain:{"cell_count":digest.count,"cell_ids_sha256":digest.hexdigest()} for domain,digest in sorted(domain_digests.items())},"layout_version":uuid4().hex,"coordinate_system":"global_level0_pixels","home_block_pixels":HOME_BLOCK,"parts":parts,"layout":"layout.parquet","id_index":"id-index.parquet","id_groups":id_groups,"arrow_schema":base64.b64encode(schema.serialize()).decode(),"measurements":sorted(measurements),"dictionaries":{name:d.to_mapping() for name,d in dictionaries.items()}}
        payload=json.dumps(description,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        if len(payload) > max_index_bytes:
            raise ValueError("The cell description exceeds its index allowance")
        with (staging/"dataset.json").open("xb") as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        _sync_directory(staging/"base")
        _sync_directory(staging)
        from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
        publish_no_replace(staging, destination, "win-x86_64" if sys.platform == "win32" else "linux-x86_64")
        _sync_directory(destination.parent)
        return CellDatasetDescription(destination/"dataset.json",count,CELL_DATASET_SCHEMA_VERSION,hashlib.sha256(payload).hexdigest())
    finally:
        if writer is not None:writer.close()
        if connection is not None:connection.close()
        if staging.exists():shutil.rmtree(staging)


class CellDataset:
    """Resolved immutable dataset with narrow group and known-ID operations."""

    def __init__(self,path,*,max_index_bytes=64*1024**2,max_batch_rows=MAX_GROUP_ROWS):
        path=Path(path)
        self.path=path/"dataset.json" if path.is_dir() else path
        self.root=self.path.parent
        if self.path.stat().st_size > max_index_bytes:
            raise ValueError("The cell description exceeds its index allowance")
        payload=self.path.read_bytes()
        self.description=json.loads(payload)
        if self.description.get("schema_version")!=CELL_DATASET_SCHEMA_VERSION:
            raise ValueError("Unsupported cell dataset schema")
        self.row_count=self.description["row_count"]
        self.summary=CellDatasetDescription(self.path,self.row_count,CELL_DATASET_SCHEMA_VERSION,hashlib.sha256(payload).hexdigest())
        self.schema=pa.ipc.read_schema(pa.BufferReader(base64.b64decode(self.description["arrow_schema"])))
        if self.description["layout"] != "layout.parquet" or self.description["id_index"] != "id-index.parquet":
            raise ValueError("The dataset index filenames are invalid")
        layout_path = self.root/"layout.parquet"
        layout_footer = pq.ParquetFile(layout_path)
        uncompressed = sum(layout_footer.metadata.row_group(i).column(j).total_uncompressed_size for i in range(layout_footer.num_row_groups) for j in range(layout_footer.metadata.num_columns))
        if uncompressed > max_index_bytes:
            raise ValueError("The cell layout exceeds the selected index allowance")
        self.layout=layout_footer.read()
        if self.layout.nbytes>max_index_bytes:
            raise ValueError("The cell layout exceeds the selected index allowance")
        self.bounds=np.column_stack([self.layout[name].to_numpy() for name in _BOUNDS])
        self.dictionaries={name:CategoryDictionary.from_mapping(value) for name,value in self.description["dictionaries"].items()}
        if isinstance(max_batch_rows,bool) or not isinstance(max_batch_rows,int) or not 0 < max_batch_rows <= MAX_GROUP_ROWS:
            raise ValueError("Cell read batches must contain at most 8192 rows")
        self.max_batch_rows=max_batch_rows
        self.max_index_bytes=max_index_bytes
        self.last_query={}

    def _groups(self,groups,columns):
        if not columns or any(name not in self.schema.names for name in columns):
            raise ValueError("Select existing cell dataset fields")
        parquet = None
        opened_path = None
        try:
            for group in groups:
                location={name:self.layout[name][group].as_py() for name in ("file","row_group")}
                relative = Path(location["file"])
                if len(relative.parts) != 2 or relative.parts[0] != "base" or relative.name not in {Path(part["file"]).name for part in self.description["parts"]}:
                    raise ValueError("A cell group references an invalid dataset part")
                path = self.root/relative
                current = _regular_snapshot(path)
                if path != opened_path:
                    if parquet is not None:
                        parquet.close()
                        parquet = None
                    parquet = pq.ParquetFile(path)
                    opened_path, original = path, current
                elif current != original:
                    raise ValueError("A cell dataset part changed while its projected group was read")
                # Reuse only this iterator's current part/footer. Each group
                # retains its bounded projection and before/after file checks.
                yield from parquet.iter_batches(batch_size=self.max_batch_rows,row_groups=[location["row_group"]],columns=list(columns))
                if _regular_snapshot(path) != original:
                    raise ValueError("A cell dataset part changed while its projected group was read")
        finally:
            if parquet is not None:
                parquet.close()

    def with_components(self, references, *, geometry_revision=0):
        """Resolve selected field/scope results without copying base geometry.

        References match the native image property's field-to-scope mapping.
        The caller must supply its accepted geometry revision when it differs
        from the unchanged base. Edited geometry itself remains a native overlay.
        """
        from .components import ComponentCellDataset
        return ComponentCellDataset(getattr(self, "_base", self), references, geometry_revision)

    def with_edits(self, reference):
        """Resolve our current sparse checkpoint without copying clean shapes.

        We admit the completed delta against this exact base identity, then
        keep edited records in a bounded disk index for projected reads.
        """
        from .edits import EditIndex, EditedCellDataset
        return EditedCellDataset(self, EditIndex(self, reference))

    def iter_batches(self,*,columns):
        """Yield selected fields over the complete scientific population."""
        yield from self._groups(range(self.layout.num_rows),columns)

    def query(self,bounds,*,columns,predicate="intersects"):
        """Read candidate groups and exact rows intersecting an image rectangle.

        The retained layout supplies conservative full-object bounds. We project
        the requested fields plus only the bounds/WKB needed by the predicate,
        then release each bounded batch after yielding its requested columns.
        """
        if len(bounds)!=4 or not all(math.isfinite(v) for v in bounds) or bounds[0]>bounds[2] or bounds[1]>bounds[3]:
            raise ValueError("A cell query requires finite ordered image bounds")
        if predicate not in {"intersects","bounds","centroid"}:
            raise ValueError("Unsupported cell spatial predicate")
        required=list(dict.fromkeys((*columns,*_BOUNDS,*(('cell_geometry',) if predicate=='intersects' else ('x_level0','y_level0') if predicate=='centroid' else ()))))
        groups=np.flatnonzero((self.bounds[:,0]<=bounds[2])&(self.bounds[:,2]>=bounds[0])&(self.bounds[:,1]<=bounds[3])&(self.bounds[:,3]>=bounds[1]))
        self.last_query={"groups":len(groups),"columns":required,"rows_read":0}
        for batch in self._groups(groups,required):
            self.last_query["rows_read"]+=batch.num_rows
            values={name:batch.column(name).to_numpy(zero_copy_only=False) for name in _BOUNDS}
            keep=(values['min_x']<=bounds[2])&(values['max_x']>=bounds[0])&(values['min_y']<=bounds[3])&(values['max_y']>=bounds[1])
            if predicate=='intersects':
                geometry=batch.column('cell_geometry')
                for index in np.flatnonzero(keep):
                    keep[index]=intersects_rectangle(geometry[int(index)].as_py(),bounds)
            elif predicate=='centroid':
                x=batch.column('x_level0').to_numpy();y=batch.column('y_level0').to_numpy()
                keep=(x>=bounds[0])&(x<=bounds[2])&(y>=bounds[1])&(y<=bounds[3])
            filtered=batch.filter(pa.array(keep)).select(columns)
            if filtered.num_rows:yield filtered

    def lookup_ids(self,ids,*,columns):
        """Resolve known final IDs through the sorted index and bounded groups."""
        ids=tuple(dict.fromkeys(ids))
        if not ids:return
        if len(ids)>self.max_batch_rows:
            raise ValueError("Known-ID requests must fit the selected batch allowance")
        locations={}
        index=pq.ParquetFile(self.root/self.description['id_index'])
        for group in self.description['id_groups']:
            requested=[value for value in ids if group['minimum']<=value<=group['maximum']]
            if not requested:continue
            for batch in index.iter_batches(batch_size=self.max_batch_rows,row_groups=[group['row_group']],columns=['cell_id','group_id','row_offset']):
                selected=batch.filter(pc.is_in(batch.column('cell_id'),value_set=pa.array(requested)))
                for row in selected.to_pylist():locations.setdefault(row['group_id'],set()).add(row['cell_id'])
        required=list(dict.fromkeys(('cell_id',*columns)))
        for group,selected in sorted(locations.items()):
            for batch in self._groups((group,),required):
                result=batch.filter(pc.is_in(batch.column('cell_id'),value_set=pa.array(sorted(selected)))).select(columns)
                if result.num_rows:yield result

    def iter_cell_records(self):
        """Restore finalized records only for a caller requiring complete cell values.

        Geometry and labels are decoded within one projected batch. Ordinary
        progress, provenance and display callers use the narrower operations.
        """
        for batch in self.iter_batches(columns=self.schema.names):
            rows=batch.to_pylist()
            for name,dictionary in self.dictionaries.items():
                if pa.types.is_list(batch.schema.field(name).type):
                    for row in rows:row[name]=dictionary.decode(row[name]) if row[name] is not None else []
                else:
                    for row,label in zip(rows,dictionary.decode(batch.column(name))):row[name]=label
            for row in rows:
                row['cell_geometry']=wkb_polygon(row['cell_geometry'])
                row['nucleus_geometry']=wkb_polygon(row['nucleus_geometry']) if row['nucleus_geometry'] is not None else {}
                row['marker_measurements']={name.removeprefix('measurement:'):row[name] for name in tuple(row) if name.startswith('measurement:') and row.get('present:'+name)}
                selected_states = row.get('marker_states')
                row['marker_states'] = json.loads(selected_states) if isinstance(selected_states, str) else {name.removeprefix('state:'):row[name] for name in tuple(row) if name.startswith('state:') and row.get('present:'+name)}
                for name in tuple(row):
                    if name.startswith(('measurement:','state:','present:')):row.pop(name)
                for name in _BOUNDS:row.pop(name)
                # Native result lineage is available in projected fields; the
                # canonical scientific record retains its declared model fields.
                yield CellRecord.from_mapping({name: value for name, value in row.items() if name in CellRecord.__dataclass_fields__})
