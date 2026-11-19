"""Exact saved outlines and calibrated measurements for review and figure export.

These projections do not change accepted scientific scope. Native QuPath remains
responsible for topology, containment and overlap admission.
"""
from collections.abc import Mapping
from math import fsum

from mif_pipeline.datasets.geometry import wkb_polygon


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def geometry_measurements(geometry, x_um_per_px, y_um_per_px):
    """Measure the exact level-0 rings, subtracting every interior hole."""
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    areas = []
    min_x = min_y = float("inf")
    max_x = max_y = -float("inf")
    for polygon in polygons:
        for index, ring in enumerate(polygon):
            # Translate before the shoelace sum to retain precision at WSI offsets.
            ox, oy = ring[0]
            area = abs(fsum((a[0]-ox)*(b[1]-oy)-(b[0]-ox)*(a[1]-oy)
                            for a, b in zip(ring, ring[1:]))) / 2
            areas.append(area if index == 0 else -area)
            for x, y in ring:
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
    return {'area_um2': fsum(areas)*x_um_per_px*y_um_per_px,
            'width_um': (max_x-min_x)*x_um_per_px,
            'height_um': (max_y-min_y)*y_um_per_px,
            'bounds_level0': [min_x, min_y, max_x, max_y]}


def project_region_review(generation, *, domain_manifests=None, changed=()):
    """Project one selected generation and optional validated domain manifests.

    Coordinates retain their full precision, components and holes. Domain area
    fractions are meaningful because the source owner has admitted containment
    and zero positive-area overlap; this reader does not independently admit ROIs.
    """
    selection = generation.tissue_resolution_selection
    x_scale = selection.x_um_per_px / selection.x_downsample
    y_scale = selection.y_um_per_px / selection.y_downsample
    changed = set(changed)
    regions, sections = [], {}
    for section in sorted(generation.sections, key=lambda item: item.display_number):
        geometry = _plain(section.geometry)
        row = {'region_id': section.roi_id, 'roi_id': section.roi_id,
               'section_id': section.section_id, 'display_number': section.display_number,
               'display_label': f'Section {section.display_number}',
               'geometry': geometry, 'edited': section.roi_id in changed,
               'layer': 'tissue', 'parent_region_id': None,
               **geometry_measurements(geometry, x_scale, y_scale)}
        regions.append(row)
        sections[section.section_id] = row
    if domain_manifests is not None:
        number = 0
        from mif_pipeline.analysis_domain.validation import _active_revisions
        for manifest in domain_manifests:
            if (manifest.tissue_generation_id != generation.generation_id
                    or manifest.canonical_image_sha256 != generation.canonical_image_sha256):
                raise ValueError('Analysis regions belong to a different tissue generation')
            section = sections[manifest.section_id]
            if manifest.tissue_roi_id != section['roi_id']:
                raise ValueError('Analysis region parent differs from its tissue section')
            for revision in _active_revisions(manifest):
                geometry = (section['geometry'] if revision.normalized_wkb is None
                            else wkb_polygon(revision.normalized_wkb))
                measurement = geometry_measurements(geometry, x_scale, y_scale)
                number += 1
                regions.append({'region_id': revision.analysis_domain_id,
                    'roi_id': revision.source_path_object_id, 'section_id': manifest.section_id,
                    'display_number': number,
                    'display_label': f'Region {number} · Section {section["display_number"]}',
                    'geometry': geometry, 'edited': revision.revision > 1,
                    'layer': 'analysis', 'parent_region_id': section['region_id'],
                    'section_fraction': measurement['area_um2']/section['area_um2'],
                    **measurement})
    return {'kind': 'tissue' if domain_manifests is None else 'analysis_domains',
            'slide_id': generation.slide_id, 'generation_id': generation.generation_id,
            'status': generation.status.value,
            'x_um_per_px': x_scale, 'y_um_per_px': y_scale,
            'regions': regions, 'overlaps': []}


def _accepted_tissue_generation(store, authority):
    from .batch_tissue_artifacts import decode_committed_tissue_acceptance_result
    from .batch_tissue_review_presentation import read_native_geometry_exchange
    from mif_pipeline.tissue_detection.generations import TissueSectionGeneration

    selected = decode_committed_tissue_acceptance_result(authority)
    terminal = store.load_attempt(selected.prior_candidate_terminal_record_sha256)
    if terminal.execution_exchange_sha256 != selected.prior_candidate_execution_exchange_sha256:
        raise ValueError('Saved tissue exchange differs from the accepted result')
    _, parsed = read_native_geometry_exchange(store, terminal, project_entry_id=selected.project_entry_id)
    generations = [row for row in parsed.generation_manifest['generations']
                   if row['generation_id'] == selected.generation_id]
    if len(generations) != 1:
        raise ValueError('The accepted tissue generation is missing')
    generation = TissueSectionGeneration.from_mapping(generations[0])
    if generation.canonical_image_sha256 != selected.canonical_image_sha256:
        raise ValueError('The saved tissue belongs to a different image')
    return generation


def saved_region_review(store, authority, snapshot):
    """Read the exact selected accepted tissue/domain result, without image IO."""
    from .batch_analysis_domain_artifacts import decode_committed_accepted_analysis_domain
    from .batch_tissue_review_presentation import read_native_geometry_exchange
    from mif_pipeline.analysis_domain.models import AnalysisDomainManifest

    if authority.artifact_ref.artifact_kind == 'tissue_acceptance_result':
        return project_region_review(_accepted_tissue_generation(store, authority))
    selected = decode_committed_accepted_analysis_domain(authority)
    tissue = [item for item in snapshot.committed_artifact_authorities
              if item.artifact_ref.artifact_kind == 'tissue_acceptance_result'
              and item.artifact_ref.artifact_ref_sha256 in selected.source_input_artifact_ref_sha256s]
    if len(tissue) != 1:
        raise ValueError('The analysis regions lack their selected tissue result')
    generation = _accepted_tissue_generation(store, tissue[0])
    if (generation.generation_id != selected.tissue_generation_id
            or generation.canonical_image_sha256 != selected.canonical_image_sha256):
        raise ValueError('The selected analysis regions differ from their tissue input')
    if selected.selection_mode == 'selected_roi':
        terminal = store.load_attempt(selected.terminal_record_sha256)
        _, parsed = read_native_geometry_exchange(store, terminal, project_entry_id=selected.project_entry_id)
        manifests = tuple(AnalysisDomainManifest.from_mapping(_plain(row))
                          for row in parsed.generation_manifest['object_outputs']['analysis_domain_manifests'])
    else:
        from .workflow import build_whole_tissue_analysis_domain_output
        output = build_whole_tissue_analysis_domain_output(
            canonical_image_id=generation.canonical_image_id,
            canonical_image_sha256=generation.canonical_image_sha256,
            project_entry_id=selected.project_entry_id, slide_id=generation.slide_id,
            tissue_generation_id=generation.generation_id,
            accepted_tissue_sections=[{'section_id': row.section_id, 'tissue_roi_id': row.roi_id,
                'tissue_path_object_id': row.path_object_id, 'tissue_generation_id': generation.generation_id}
                for row in generation.sections],
            analysis_domain_assistance={'schema_version': 'analysis-domain-assistance/v1',
                'mode': 'unavailable', 'canonical_project_entry_id': selected.project_entry_id,
                'annotation_project_entry_id': selected.project_entry_id,
                'coordinate_system': 'global_level0_pixels', 'coordinate_transfer_authorized': False})
        manifests = tuple(AnalysisDomainManifest.from_mapping(row) for row in output['analysis_domain_manifests'])
    result = project_region_review(generation, domain_manifests=manifests)
    result['selection_mode'] = selected.selection_mode
    return result


def pending_domain_review(store, target):
    """Combine the current accepted tissue with its pending selected-region review."""
    import hashlib
    import json
    from pathlib import Path
    from mif_pipeline.analysis_domain.models import AnalysisDomainManifest

    snapshot = store.reopen_execution_snapshot()
    tissues = [item for item in snapshot.committed_artifact_authorities
               if item.artifact_ref.artifact_kind == 'tissue_acceptance_result'
               and item.artifact_ref.scope.sample_id == target.sample_id
               and item.artifact_ref.scope.project_entry_id == target.project_entry_id]
    if len(tissues) != 1:
        raise ValueError('Analysis-region review requires its current accepted tissue')
    generation = _accepted_tissue_generation(store, tissues[0])
    if generation.generation_id != target.stage_result.generation_id:
        raise ValueError('The analysis-region review refers to different tissue')
    context = target.stage_result.resume_context
    reference = context.get('candidate_manifest') or context.get('lineage_predecessor_candidate')
    if reference is None:
        result = project_region_review(generation)
        result['kind'] = 'analysis_domains'
        result['selection_mode'] = 'choose_regions'
        return result
    with Path(reference['path']).open('rb') as stream:
        payload = stream.read(reference['byte_count'] + 1)
    if (len(payload) != reference['byte_count']
            or hashlib.sha256(payload).hexdigest() != reference['sha256']):
        raise ValueError('The saved analysis-region review has changed')
    output = json.loads(payload)
    manifests = tuple(AnalysisDomainManifest.from_mapping(row) for row in output['analysis_domain_manifests'])
    result = project_region_review(generation, domain_manifests=manifests)
    result['status'] = output['selection_status']
    result['selection_mode'] = 'selected_rois'
    return result


def pending_structure_review(store, target):
    """Project only the selected structure and its saved containing outline."""
    from pathlib import Path
    from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
    from mif_pipeline.io.processed_image import ProcessedImage
    from .analysis_services import _stream_json_object
    from .region_review_image import _selected_image

    context = resolve_resume_envelope(target.stage_result.resume_context).service_context
    snapshot = store.reopen_execution_snapshot()
    domain_id = context['analysis_domain_id']
    matches = [authority.artifact_ref for authority in snapshot.committed_artifact_authorities
               if authority.artifact_ref.artifact_kind == 'semantic_raw_inventory'
               and authority.artifact_ref.scope.sample_id == target.sample_id
               and authority.artifact_ref.scope.project_entry_id == target.project_entry_id
               and any(binding.analysis_domain_id == domain_id
                       for binding in authority.artifact_ref.scope.analysis_domain_bindings)]
    if len(matches) != 1:
        raise ValueError('The saved outlines for this structure review are unavailable.')
    selected_id, parent_id = context['roi_id'], context.get('parent_roi_id')
    selected_rows = {}

    def collect(row):
        if row.get('roi_id') in {selected_id, parent_id} and row.get('analysis_domain_id') == domain_id:
            selected_rows[row['roi_id']] = row

    _stream_json_object(Path(matches[0].locator), array_consumers={
        'rois': collect, 'tissue_sections': lambda row: None, 'cells': lambda row: None,
    }, required_fields=('rois',))
    selected = selected_rows.get(selected_id)
    if selected is None or selected.get('geometry_digest') != context['geometry_sha256']:
        raise ValueError('The saved outline does not match the region being reviewed.')
    image, _ = _selected_image(store, snapshot, target)
    with ProcessedImage(image.locator, cache_bytes=0) as reader:
        level = reader.levels[0]
        x_scale, y_scale = level.x_um_per_px, level.y_um_per_px
        width, height = level.shape[2], level.shape[1]
    regions = []
    for identifier in (parent_id, selected_id):
        row = selected_rows.get(identifier)
        if row is None:
            continue
        geometry = row['geometry']
        regions.append(dict(region_id=identifier, roi_id=identifier,
            path_object_id=row['path_object_id'], parent_region_id=row.get('parent_roi_id'),
            display_label=context.get('region_label', 'Selected region') if identifier == selected_id else 'Containing region',
            layer='secondary' if identifier == selected_id else 'context',
            geometry=geometry, **geometry_measurements(geometry, x_scale, y_scale)))
    bounds = next(row['bounds_level0'] for row in regions if row['region_id'] == selected_id)
    margin = max(bounds[2]-bounds[0], bounds[3]-bounds[1]) * .12
    crop = [max(0., bounds[0]-margin), max(0., bounds[1]-margin),
            min(float(width), bounds[2]+margin), min(float(height), bounds[3]+margin)]
    return dict(kind='secondary', slide_id=context['slide_id'], regions=regions,
                structure_display_name=context.get('structure_display_name', context['structure_id']),
                x_um_per_px=x_scale, y_um_per_px=y_scale, overlaps=[],
                selected_region_id=selected_id, crop_level0=crop)


def pending_structure_selection_review(store, target):
    """Join the count inventory to its retained outlines and recorded measurements."""
    from pathlib import Path
    from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope, _manual_secondary_inventory
    from mif_pipeline.io.processed_image import ProcessedImage
    from .analysis_services import _stream_json_object, _file_sha256
    from .region_review_image import _selected_image

    context = resolve_resume_envelope(target.stage_result.resume_context).service_context
    inventory = _manual_secondary_inventory(context)
    snapshot = store.reopen_execution_snapshot()
    terminal = store.load_attempt(target.needs_interaction_terminal_record_sha256)
    inputs = terminal.execution_context_binding.input_artifact_ref_sha256s
    descriptor = context['semantic_count_state_descriptor']
    references = {}
    for kind, field in (('semantic_raw_inventory', 'raw_inventory_ref_sha256'),
                        ('semantic_evaluation_result', 'semantic_evaluation_ref_sha256')):
        matches = [a.artifact_ref for a in snapshot.committed_artifact_authorities
                   if a.artifact_ref.artifact_kind == kind
                   and a.artifact_ref.artifact_ref_sha256 == descriptor[field]
                   and a.artifact_ref.artifact_ref_sha256 in inputs
                   and a.artifact_ref.scope.sample_id == target.sample_id
                   and a.artifact_ref.scope.project_entry_id == target.project_entry_id
                   and any(binding.analysis_domain_id == context['analysis_domain_id']
                           and binding.analysis_domain_revision_sha256 == context['analysis_domain_revision_sha256']
                           for binding in a.artifact_ref.scope.analysis_domain_bindings)]
        if len(matches) != 1 or not matches[0].locator:
            raise ValueError('The saved structures for this selection are unavailable.')
        reference = matches[0]
        if _file_sha256(Path(reference.locator)) != reference.sha256:
            raise ValueError('The saved structure review changed. Reopen this selection.')
        references[kind] = reference
    wanted = {item['roi_id']: item for item in inventory['items']}
    outlines, evidence = {}, {}

    def collect(row, destination):
        if row.get('roi_id') in wanted and row.get('analysis_domain_id') == context['analysis_domain_id']:
            if row['roi_id'] in destination:
                raise ValueError('The saved structure review contains a repeated region.')
            destination[row['roi_id']] = row

    _stream_json_object(Path(references['semantic_raw_inventory'].locator), array_consumers={
        'rois': lambda row: collect(row, outlines), 'tissue_sections': lambda row: None,
        'cells': lambda row: None}, required_fields=('rois',))
    result = _stream_json_object(Path(references['semantic_evaluation_result'].locator),
        array_consumers={'semantic_results': lambda row: collect(row, evidence)},
        required_fields=('semantic_results',))
    if (result.get('semantic_result_set_sha256') != context['semantic_result_set_sha256']
            or outlines.keys() != wanted.keys() or evidence.keys() != wanted.keys()):
        raise ValueError('The saved structures do not match this count selection.')
    image, _ = _selected_image(store, snapshot, target)
    with ProcessedImage(image.locator, cache_bytes=0) as reader:
        level = reader.levels[0]
        x_scale, y_scale = level.x_um_per_px, level.y_um_per_px
    regions = []
    for index, item in enumerate(inventory['items'], 1):
        row, reviewed = outlines[item['roi_id']], evidence[item['roi_id']]
        if (row.get('geometry_digest') != item['geometry_sha256']
                or reviewed.get('geometry_sha256') != item['geometry_sha256']
                or reviewed.get('review_status') != 'accepted'):
            raise ValueError('A saved outline differs from the accepted structure.')
        measurements = {value['kind']: value['value'] for value in reviewed.get('measurement_results', ())
                        if value.get('outcome') == 'available'}
        geometry = row['geometry']
        regions.append(dict(region_id=item['roi_id'], roi_id=item['roi_id'],
            content_key=item['content_key'], path_object_id=row['path_object_id'],
            parent_region_id=row.get('parent_roi_id'), display_number=index,
            display_label=f'Structure {index}', layer='secondary', geometry=geometry,
            cell_count=measurements.get('segmented_cell_count'),
            **geometry_measurements(geometry, x_scale, y_scale)))
        if measurements.get('candidate_area') is not None:
            regions[-1]['area_um2'] = measurements['candidate_area']
    return dict(kind='secondary_selection', slide_id=context['slide_id'], regions=regions,
                x_um_per_px=x_scale, y_um_per_px=y_scale, overlaps=[])
