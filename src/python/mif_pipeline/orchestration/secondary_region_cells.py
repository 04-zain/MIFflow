"""Bounded read-only cell outlines from a pending region's exact native selection."""
from io import BytesIO
import hashlib
import json
from pathlib import Path

import numpy as np

MAX_CELLS = 5000
MAX_VERTICES = 300_000
MAX_FIELD_UM = 4096.


def selected_raw(store, snapshot, target):
    from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
    from .analysis_services import _stream_json_object
    if target.stage_result.interaction_reason != 'semantic_region_decision_required':
        raise ValueError('Cell inspection requires the current secondary-region review.')
    context = resolve_resume_envelope(target.stage_result.resume_context).service_context
    terminal = store.load_attempt(target.needs_interaction_terminal_record_sha256)
    inputs = terminal.execution_context_binding.input_artifact_ref_sha256s
    refs = [a.artifact_ref for a in snapshot.committed_artifact_authorities
            if a.artifact_ref.artifact_kind == 'semantic_raw_inventory'
            and a.artifact_ref.artifact_ref_sha256 in inputs
            and a.artifact_ref.scope.sample_id == target.sample_id
            and a.artifact_ref.scope.project_entry_id == target.project_entry_id
            and any(b.analysis_domain_id == context['analysis_domain_id']
                    for b in a.artifact_ref.scope.analysis_domain_bindings)]
    if len(refs) != 1:
        raise ValueError('The saved cells for this review are unavailable.')
    ref = refs[0]
    path = Path(ref.locator)
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ref.sha256:
            raise ValueError('The saved cell selection changed from this review.')
    legacy_cells = []
    raw = _stream_json_object(path, array_consumers={
        'cells': lambda row: legacy_cells.append(True) if not legacy_cells else None,
        'rois': lambda row: None, 'tissue_sections': lambda row: None},
        required_fields=('cell_datasets', 'cell_scope', 'cell_summary'))
    if legacy_cells or raw.get('slide_id') != context['slide_id']:
        raise ValueError('This review does not have a current native cell selection.')
    return ref, context, raw


def _empty(metadata):
    return dict(metadata=metadata, outline_vertices_um=np.empty((0, 2)),
                outline_offsets=np.asarray([0], dtype=np.int64),
                outline_cell_indices=np.empty(0, dtype=np.int64),
                outline_kinds=np.asarray([], dtype='U7'))


def read_field(dataset, domain_id, field_um, *, pixel_size, image_extent_um):
    """Query only the visible field, retaining exact domains and category meanings."""
    from mif_pipeline.datasets.geometry import wkb_polygon
    import pyarrow.compute as pc
    field = np.asarray(field_um, dtype=float)
    extent = np.asarray(image_extent_um, dtype=float)
    if (field.shape != (4,) or not np.isfinite(field).all()
            or not extent[0] <= field[0] < field[2] <= extent[2]
            or not extent[1] <= field[1] < field[3] <= extent[3]):
        raise ValueError('Choose a finite field inside the prepared image.')
    metadata = dict(field_um=field.tolist(), analysis_domain_id=domain_id,
                    visible_cells=[], outline_detail_available=False,
                    scope='accepted_cells_in_visible_analysis_domain')
    if max(field[2]-field[0], field[3]-field[1]) > MAX_FIELD_UM:
        metadata['message'] = 'Zoom in to see cells.'
        return _empty(metadata)
    bounds = field / np.tile(pixel_size, 2)
    fields = ('cell_id', 'section_id', 'analysis_domain_id', 'primary_phenotype',
              'cell_geometry', 'nucleus_geometry')
    if any(name not in dataset.schema.names for name in fields):
        raise ValueError('The accepted cell geometry and phenotype fields are unavailable.')
    rows = []
    geometry_bytes = 0
    for batch in dataset.query(bounds.tolist(), columns=fields, predicate='intersects'):
        batch = batch.filter(pc.equal(batch.column('analysis_domain_id'), domain_id))
        geometry_bytes += batch.nbytes
        if len(rows)+batch.num_rows > MAX_CELLS or geometry_bytes > 32 * 1024**2:
            metadata['message'] = 'Zoom in to see cells.'
            return _empty(metadata)
        values = batch.to_pylist()
        dictionary = dataset.dictionaries.get('primary_phenotype')
        if dictionary is not None:
            labels = dictionary.decode(batch.column('primary_phenotype'))
            for row, label in zip(values, labels, strict=True):
                row['primary_phenotype'] = label
        rows.extend(values)
    vertices, offsets, indices, kinds, cells = [], [0], [], [], []
    boundaries = {section.description['scope'].get('section_id'): section.description['scope'].get('cell_boundary_source')
                  for section in getattr(dataset, 'sections', ())}
    for index, row in enumerate(rows):
        label = row['primary_phenotype']
        if label is not None and not isinstance(label, str):
            raise ValueError('The saved phenotype vocabulary is unavailable.')
        cells.append(dict(cell_id=row['cell_id'], section_id=row['section_id'],
                          phenotype=label))
        boundary = boundaries.get(row['section_id'], dataset.description['scope'].get('cell_boundary_source'))
        for field_name, kind in (('cell_geometry', 'cell'), ('nucleus_geometry', 'nucleus')):
            if kind == 'cell' and boundary == 'nuclear_outline':
                continue
            shape = wkb_polygon(row[field_name]) if row[field_name] is not None else None
            polygons = [] if shape is None else [shape['coordinates']] if shape['type'] == 'Polygon' else shape['coordinates']
            for polygon in polygons:
                for ring in polygon:
                    points = np.asarray(ring, dtype=float) * pixel_size
                    if offsets[-1]+len(points) > MAX_VERTICES:
                        metadata['message'] = 'Zoom in to see cells.'
                        return _empty(metadata)
                    vertices.extend(points); offsets.append(len(vertices))
                    indices.append(index); kinds.append(kind)
    metadata.update(visible_cells=cells, outline_detail_available=True,
                    field_cell_count=len(cells), message='')
    return dict(metadata=metadata, outline_vertices_um=np.asarray(vertices).reshape(-1, 2),
                outline_offsets=np.asarray(offsets, dtype=np.int64),
                outline_cell_indices=np.asarray(indices, dtype=np.int64),
                outline_kinds=np.asarray(kinds, dtype='U7'))


def render_secondary_region_cells(store, snapshot, target, options):
    from mif_pipeline.io.processed_image import ProcessedImage
    from .analysis_services import _raw_cell_dataset
    from .region_review_image import _selected_image
    if (not isinstance(options, dict) or set(options) != {'field_um', 'request_key'}
            or type(options['request_key']) is not int or options['request_key'] < 0):
        raise ValueError('Cell inspection requires an exact current field request.')
    ref, context, raw = selected_raw(store, snapshot, target)
    image, _ = _selected_image(store, snapshot, target)
    with ProcessedImage(image.locator, cache_bytes=0) as reader:
        level = reader.levels[0]
        pixel_size = np.asarray((level.x_um_per_px, level.y_um_per_px))
        extent = (0., 0., level.shape[2]*pixel_size[0], level.shape[1]*pixel_size[1])
    dataset = _raw_cell_dataset(raw)
    try:
        if any(not np.allclose(pixel_size, [section.description['scope']['x_um_per_px'],
                section.description['scope']['y_um_per_px']], rtol=1e-10, atol=0)
               for section in dataset.sections):
            raise ValueError('The saved cell coordinates differ from the prepared image.')
        if any(section.description['scope'].get('canonical_image_sha256') != image.sha256
               for section in dataset.sections):
            raise ValueError('The saved cells belong to a different prepared image.')
        bundle = read_field(dataset, context['analysis_domain_id'], options['field_um'],
                            pixel_size=pixel_size, image_extent_um=extent)
    finally:
        dataset.close()
    bundle['metadata'].update(request=options, raw_inventory_ref_sha256=ref.artifact_ref_sha256,
        sample_id=target.sample_id, project_entry_id=target.project_entry_id,
        slide_id=context['slide_id'], image_sha256=image.sha256,
        phenotype_selections=[dict(section_id=s['section_id'], phenotype_view=s['phenotype_view'])
                              for s in raw['cell_datasets']])
    output = BytesIO()
    np.savez_compressed(output, **{**bundle, 'metadata': np.asarray(json.dumps(bundle['metadata']))})
    return (('region-cells', 'Accepted cells in the visible field', 'npz', output.getvalue()),)
