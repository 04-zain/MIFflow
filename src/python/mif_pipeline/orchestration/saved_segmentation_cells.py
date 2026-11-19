"""Read a selected segmentation's existing dataset and shape descriptions."""
from mif_pipeline.datasets.cell_store import CellDataset


def segmentation_cells(authority, state_store, *, snapshot=None):
    """Resolve the recorded dataset without revisiting image pixels or cell rows.

    The committed native result selects its original exchange and descriptor.
    Descriptor identity, population and frame are compared before the ordinary
    indexed reader is returned. Its projected operations open bounded Parquet
    groups only when the chosen distribution or image field needs them.
    """
    from .batch_segmentation_artifacts import decode_committed_segmentation_result, _digest
    from .batch_tissue_review_presentation import read_native_geometry_exchange

    if state_store is None:
        raise ValueError('The saved segmentation source is unavailable.')
    selected = decode_committed_segmentation_result(authority)
    terminal = state_store.load_attempt(selected.terminal_record_sha256)
    ref = authority.artifact_ref
    if (terminal.node_id != ref.producer_node_id
            or terminal.attempt_id != ref.producer_attempt_id
            or terminal.authorization_sha256 != ref.producer_attempt_authorization_sha256
            or terminal.execution_exchange_sha256 != selected.execution_exchange_sha256):
        raise ValueError('The saved segmentation differs from its completed operation.')
    _, parsed = read_native_geometry_exchange(state_store, terminal,
        project_entry_id=selected.project_entry_id)
    output = parsed.result.outputs
    if _digest(output) != ref.owning_identity['object_outputs_sha256']:
        raise ValueError('The recorded segmentation output has changed.')
    reference = output['cell_dataset']
    cells = CellDataset(reference['path'])
    description, scope = cells.description, cells.description['scope']
    if (cells.summary.sha256 != reference['sha256']
            or cells.summary.sha256 != selected.cell_evidence_sha256
            or cells.row_count != reference['row_count']
            or cells.row_count != selected.cell_count
            or description['source_sha256'] != reference['source_sha256']
            or description['domain_cell_mapping_sha256'] != selected.domain_cell_mapping_sha256
            or scope['population_id'] != selected.segmentation_population_id
            or scope['canonical_image_id'] != selected.canonical_image_id
            or scope['canonical_image_sha256'] != selected.canonical_image_sha256):
        raise ValueError('The saved cell dataset differs from the selected segmentation.')
    if snapshot is not None:
        from .batch_cell_corrections import (
            current_cell_correction, decode_committed_cell_correction, current_measurement_geometry,
            read_correction_submission, correction_source_selection,
        )
        correction = current_cell_correction(snapshot, ref)
        _measurement, selection = current_measurement_geometry(snapshot, ref, correction)
        if selection is None and correction is not None:
            selection = decode_committed_cell_correction(correction, ref)
        # Acceptance retires the old measurement before the correction task
        # runs. Its bound predecessor remains the accepted before-geometry
        # during that interval (and during a failed publication/retry).
        preceding = None if correction is None else correction.artifact_ref.artifact_ref_sha256
        pending = [node.cell_correction for node in snapshot.plan.nodes
            if node.cell_correction is not None
            and node.cell_correction.segmentation_result_ref_sha256 == ref.artifact_ref_sha256
            and node.cell_correction.previous_correction_ref_sha256 == preceding]
        if len(pending) > 1:
            raise ValueError('The segmentation has more than one pending correction.')
        if _measurement is None and pending:
            submission = read_correction_submission(state_store, pending[0])
            selection = correction_source_selection(state_store, pending[0], submission, ref, selection)
        if selection is not None and selection['cell_edits'] is not None:
            cells = cells.with_edits(dict(selection['cell_edits']))
            if (cells.row_count != selection['cell_count'] or
                    cells._edit_index.description['geometry_revision'] != selection['geometry_revision']):
                raise ValueError('The selected cell corrections differ from their accepted checkpoint.')
    return cells


def shape_features(cells):
    """Describe only saved shape columns, without scanning their values."""
    from mif_pipeline.cell_analysis.clustering import FeatureRole, FeatureSpec
    from mif_pipeline.cell_analysis.measurement_plan import shape_measurement_choices
    from mif_pipeline.cell_analysis.measurement_setup import validate_segmentation_compartments

    def available(row):
        try:
            validate_segmentation_compartments((row,), cells.description['scope'].get('cell_boundary_source'))
            return True
        except ValueError:
            return False

    return tuple(FeatureSpec(measurement=row['measurement_name'], role=FeatureRole.MORPHOLOGY,
        **{key: row[key] for key in ('feature_id', 'kind', 'channel_id', 'biomarker',
                                    'compartment', 'units', 'opt_in')})
        for _, _, row in shape_measurement_choices()
        if 'measurement:' + row['measurement_name'] in cells.schema.names and available(row))


def segmentation_tissue_geometry(authority, snapshot, state_store):
    """Read the accepted section geometry from its retained, digest-checked exchange."""
    from .batch_segmentation_artifacts import decode_committed_segmentation_result
    from .batch_tissue_artifacts import decode_committed_tissue_acceptance_result, _digest
    from .batch_tissue_review_presentation import read_native_geometry_exchange
    from mif_pipeline.tissue_detection.generations import TissueSectionGeneration, GenerationStatus

    selected = decode_committed_segmentation_result(authority)
    matches = []
    for item in snapshot.committed_artifact_authorities:
        if (item.artifact_ref.artifact_kind != 'tissue_acceptance_result'
                or item.artifact_ref.scope.sample_id != selected.sample_id):
            continue
        tissue = decode_committed_tissue_acceptance_result(item)
        if (tissue.generation_id == selected.tissue_generation_id
                and tissue.canonical_image_sha256 == selected.canonical_image_sha256
                and selected.section_id in tissue.section_ids):
            matches.append(tissue)
    if len(matches) != 1:
        raise ValueError('The accepted tissue for this segmentation is unavailable.')
    tissue = matches[0]
    terminal = state_store.load_attempt(tissue.prior_candidate_terminal_record_sha256)
    if terminal.execution_exchange_sha256 != tissue.prior_candidate_execution_exchange_sha256:
        raise ValueError('The retained tissue exchange differs from its acceptance.')
    _, parsed = read_native_geometry_exchange(state_store, terminal, project_entry_id=selected.project_entry_id)
    generations = [TissueSectionGeneration.from_mapping(row)
        for row in parsed.generation_manifest['generations'] if row['generation_id'] == tissue.generation_id]
    if (len(generations) != 1 or generations[0].status is not GenerationStatus.ACCEPTED
            or _digest(generations[0].to_mapping()) != tissue.generation_content_sha256):
        raise ValueError('The retained tissue geometry differs from its acceptance.')
    sections = [row for row in generations[0].sections if row.section_id == selected.section_id]
    if (len(sections) != 1 or sections[0].path_object_id != selected.tissue_path_object_id
            or sections[0].roi_id != selected.tissue_roi_id):
        raise ValueError('The selected tissue section differs from the segmentation.')
    return sections[0].to_export_mapping()['geometry']


def segmentation_area_geometry(authority, snapshot, dependencies, summaries):
    """Reopen the exact segmented union, including excluded/primary-area choices.

    The existing read-only native projection checks accepted primary revisions
    and recreates the native union digest. No live unaccepted region is used.
    """
    from pathlib import Path
    from .batch_segmentation_artifacts import decode_committed_segmentation_result, _digest
    from .batch_tissue_review_presentation import read_native_geometry_exchange
    from .batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result
    from .saved_classified_region_counts import geometry_request, read_region_geometry
    from .region_review_geometry import saved_region_review
    from mif_pipeline.pipeline_core.config import SegmentationConfig
    from mif_pipeline.datasets.geometry import polygon_wkb, wkb_polygon

    selected = decode_committed_segmentation_result(authority)
    if selected.segmentation_skipped_excluded_domains:
        raise ValueError('No area was segmented because the selected analysis areas were excluded.')
    store = dependencies.runtime.state_store
    terminal = store.load_attempt(selected.terminal_record_sha256)
    if (terminal.node_id != authority.artifact_ref.producer_node_id
            or terminal.attempt_id != authority.artifact_ref.producer_attempt_id
            or terminal.authorization_sha256 != authority.artifact_ref.producer_attempt_authorization_sha256
            or terminal.execution_exchange_sha256 != selected.execution_exchange_sha256):
        raise ValueError('The saved segmentation differs from its completed operation.')
    request, parsed = read_native_geometry_exchange(store, terminal, project_entry_id=selected.project_entry_id)
    settings = SegmentationConfig.from_mapping(request['segmentation_settings'], field='segmentation_settings')
    if (_digest(settings.to_mapping()) != selected.segmentation_config_sha256
            or _digest(parsed.result.outputs) != authority.artifact_ref.owning_identity['object_outputs_sha256']):
        raise ValueError('The retained segmentation settings or output have changed.')
    current = {item.artifact_ref.artifact_ref_sha256:item for item in snapshot.committed_artifact_authorities}
    bindings = {item.analysis_domain_id:item for item in selected.analysis_domain_bindings}
    primary = {}
    for reference in selected.primary_inference_result_ref_sha256s:
        source = decode_committed_primary_classifier_inference_result(current[reference])
        binding = source.analysis_domain_bindings[0]
        if bindings.get(binding.analysis_domain_id) != binding or binding.analysis_domain_id in primary:
            raise ValueError('Accepted primary regions differ from the segmented analysis areas.')
        primary[binding.analysis_domain_id] = source
    if primary and set(primary) != set(bindings):
        raise ValueError('Accepted primary regions do not cover the segmentation scope.')
    geometries = {}
    for reference in selected.accepted_analysis_domain_ref_sha256s:
        owner = current[reference]
        if not set(selected.analysis_domain_bindings) <= set(owner.artifact_ref.scope.analysis_domain_bindings):
            raise ValueError('The accepted analysis areas differ from this segmentation.')
        review = saved_region_review(store, owner, snapshot)
        for row in review['regions']:
            if row['layer'] == 'analysis' and row['region_id'] in bindings:
                geometries[row['region_id']] = row['geometry']
    if set(geometries) != set(bindings):
        raise ValueError('An accepted segmented analysis area has no saved outline.')
    domains = []
    for identity, geometry in geometries.items():
        source = primary.get(identity)
        if source is not None and source.domain_status != 'active':
            continue
        owner = None
        if settings.scope == 'configured_union':
            if source is None:
                raise ValueError('The configured segmentation union lacks accepted primary regions.')
            owner, _area = geometry_request(source, snapshot, dependencies)
        domains.append(dict(geometry_wkb_hex=polygon_wkb(geometry)[0].hex(), primary=owner))
    hierarchy = Path(snapshot.head.project_path).parent/'data'/selected.project_entry_id/'data.qpdata'
    from contextlib import nullcontext
    from mif_pipeline.pipeline_core.execution_resources import current_operation_budget, operation_budget
    budget = current_operation_budget()
    # The service reserves 768 MiB: retain 128 MiB for its image/index/geometry
    # buffers while the existing native projection receives at most 640 MiB.
    allowance = (operation_budget(budget.memory_bytes, budget.cpu_threads,
        native_memory_bytes=min(640*1024**2, budget.memory_bytes-128*1024**2))
        if budget is not None else nullcontext())
    with allowance:
        result = read_region_geometry(dict(segmentation_union=True, hierarchy_file=str(hierarchy),
            scope=settings.scope, domains=domains, union_geometry_sha256=selected.union_geometry_sha256),
            dependencies.runtime.runner, summaries)
    if result['union_geometry_sha256'] != selected.union_geometry_sha256:
        raise ValueError('The coverage area differs from the accepted segmentation.')
    return wkb_polygon(bytes.fromhex(result['geometry_wkb_hex']))
