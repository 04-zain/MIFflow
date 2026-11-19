"""Import one completed native finalized-cell stream into canonical storage.

The owning segmentation operation supplies already reconciled stable IDs and
independent scientific coordinates. This private intermediate is read by row
group and released while the canonical writer performs its disk-backed sort.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq

from mif_pipeline.pipeline_core.models import CellRecord
from .cell_store import GROUP_BYTES, MAX_GROUP_ROWS, write_cell_dataset
from .geometry import wkb_polygon

NATIVE_CELL_STREAM_SCHEMA_VERSION = "mif-native-cell-stream/v1"
_COLUMNS = ('cell_id', 'cell_geometry', 'nucleus_geometry', 'c', 'z', 't', 'primary_phenotype', 'x_level0', 'y_level0', 'x_um', 'y_um', 'metadata', 'measurements')


def _map(values, *, numeric=False):
    if not isinstance(values, list):
        raise ValueError("Native cell metadata and measurements require typed maps")
    result = {}
    for key, value in values:
        if not isinstance(key, str) or not key or key in result:
            raise ValueError("A native cell map repeats or omits a scientific key")
        if numeric:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Native measurements require double values")
        elif not isinstance(value, str):
            raise ValueError("Native cell metadata requires string values")
        result[key] = value
    return result


def _record(row, scope):
    if (row['c'], row['z'], row['t']) != (-1, 0, 0):
        raise ValueError("The current cell dataset requires the default two-dimensional image plane")
    metadata = _map(row['metadata'])
    measurements = _map(row['measurements'], numeric=True)
    if not isinstance(row['cell_id'], str) or not row['cell_id'] or metadata.get('cell_id') != row['cell_id']:
        raise ValueError("Finalized native cell identity disagrees with its scientific metadata")
    required = ('slide_id', 'section_id', 'analysis_domain_id', 'analysis_domain_kind', 'generation_id')
    if any(not metadata.get(name) for name in required):
        raise ValueError("Native finalized cells require their exact scientific scope")
    if 'primary_compartment' not in metadata or not isinstance(metadata['primary_compartment'], str):
        raise ValueError("Native finalized cells require their exact primary compartment value")
    domain = scope.get('domains', {}).get(metadata['analysis_domain_id'])
    if not isinstance(domain, dict) or domain.get('generation_id') != metadata['generation_id']:
        raise ValueError("A native finalized cell belongs to an unselected domain generation")
    for name in ('x_level0', 'y_level0'):
        if row[name] is None or not math.isfinite(row[name]):
            raise ValueError("Native cell coordinates must remain finite independent values")
    return CellRecord.from_mapping({
        'cell_id': row['cell_id'], 'slide_id': metadata['slide_id'], 'section_id': metadata['section_id'],
        'analysis_domain_id': metadata['analysis_domain_id'], 'analysis_domain_kind': metadata['analysis_domain_kind'],
        'x_level0': row['x_level0'], 'y_level0': row['y_level0'], 'x_um': row['x_um'], 'y_um': row['y_um'],
        'cell_geometry': wkb_polygon(row['cell_geometry']),
        'nucleus_geometry': wkb_polygon(row['nucleus_geometry']) if row['nucleus_geometry'] is not None else {},
        'primary_compartment': metadata['primary_compartment'], 'secondary_roi_ids': [],
        'marker_measurements': measurements, 'marker_states': {},
        'primary_phenotype': row['primary_phenotype'], 'phenotype_components': [], 'phenotype_source': None,
        'cluster_id': None, 'tissue_roi_id': metadata.get('tissue_roi_id'),
        'tissue_generation_id': metadata['generation_id'],
        'pipeline_run_id': metadata.get('pipeline_run_id') or metadata.get('run_id'),
    })


def _records(path, description):
    total = 0
    seen = set()
    for part in description['parts']:
        relative = Path(part['file'])
        if relative.is_absolute() or len(relative.parts) != 1 or not re.fullmatch(r'part-\d+\.parquet', relative.name) or relative.name in seen:
            raise ValueError("A native stream part must name one distinct local Parquet file")
        seen.add(relative.name)
        source = path.parent / relative
        if source.stat().st_size != part['bytes']:
            raise ValueError("A completed native cell part changed its recorded byte count")
        with pq.ParquetFile(source) as parquet:
            if set(parquet.schema_arrow.names) != set(_COLUMNS) or parquet.metadata.num_rows != part['row_count']:
                raise ValueError("A native cell part disagrees with its finalized typed columns or count")
            for name, dtype in {'cell_id': pa.string(), 'cell_geometry': pa.binary(), 'nucleus_geometry': pa.binary(), 'primary_phenotype': pa.string(), 'c': pa.int32(), 'z': pa.int32(), 't': pa.int32(), 'x_level0': pa.float64(), 'y_level0': pa.float64(), 'x_um': pa.float64(), 'y_um': pa.float64()}.items():
                if parquet.schema_arrow.field(name).type != dtype:
                    raise ValueError("A native finalized column changes its scientific type")
            for name, value_type in (('metadata', pa.string()), ('measurements', pa.float64())):
                dtype = parquet.schema_arrow.field(name).type
                if not pa.types.is_map(dtype) or dtype.key_type != pa.string() or dtype.item_type != value_type:
                    raise ValueError("Native finalized maps change their exact key or scientific value types")
            for index in range(parquet.num_row_groups):
                group = parquet.metadata.row_group(index)
                if group.num_rows > MAX_GROUP_ROWS or sum(group.column(i).total_uncompressed_size for i in range(group.num_columns)) > GROUP_BYTES:
                    raise ValueError("A native finalized group exceeds its row or byte allowance")
                for batch in parquet.iter_batches(batch_size=MAX_GROUP_ROWS, row_groups=[index], columns=list(_COLUMNS)):
                    if batch.nbytes > GROUP_BYTES:
                        raise ValueError("A decoded native finalized group exceeds its byte allowance")
                    for row in batch.to_pylist():
                        total += 1
                        yield _record(row, description['scope'])
    if total != description['row_count']:
        raise ValueError("Native stream parts disagree with the completed population count")


def import_native_cell_stream(source, destination, *, expected_source_sha256):
    """Publish the exact finalized native population under its owning stage.

    This imports only the current private stream contract. The stage remains
    responsible for selecting and saving the completed descriptor in ImageData.
    An interrupted or mismatched stream never replaces a canonical generation.
    """
    source = Path(source)
    path = source / 'stream.json' if source.is_dir() else source
    if path.stat().st_size > 64 * 1024**2:
        raise ValueError("Native cell stream metadata exceeds its allowance")
    description = json.loads(path.read_bytes())
    if (description.get('schema_version') != NATIVE_CELL_STREAM_SCHEMA_VERSION or
            description.get('complete') is not True or
            description.get('coordinate_system') != 'global_level0_pixels' or
            not isinstance(expected_source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_source_sha256) or
            description.get('source_sha256') != expected_source_sha256):
        raise ValueError("The finalized native stream is incomplete or belongs to another accepted scientific source")
    if (not isinstance(description.get('scope'), dict) or not description['scope'].get('population_id') or
            not isinstance(description.get('row_count'), int) or isinstance(description['row_count'], bool) or description['row_count'] < 0 or
            not isinstance(description.get('parts'), list)):
        raise ValueError("The native finalized population summary is invalid")
    return write_cell_dataset(destination, _records(path, description), scope=description['scope'], source_sha256=expected_source_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Publish finalized native cells in canonical spatial storage')
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--result-json', required=True)
    args = parser.parse_args(argv)
    result_path = Path(args.result_json)
    if not result_path.is_absolute():
        raise ValueError("The native importer requires an absolute result filename")
    result = import_native_cell_stream(args.source, args.destination, expected_source_sha256=args.source_sha256)
    # Windowed packaged executables have no stdout; the stage waits for process
    # completion before reading this owned response file.
    with result_path.open('x', encoding='utf-8') as stream:
        json.dump({'dataset': str(result.path), 'row_count': result.row_count,
                   'schema_version': result.schema_version, 'sha256': result.sha256},
                  stream, separators=(',', ':'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
