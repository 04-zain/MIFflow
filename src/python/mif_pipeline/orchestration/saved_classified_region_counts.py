"""Describe the saved classified objects in one accepted analysis domain."""
import json

from mif_pipeline.cell_analysis.result_plots import _csv_output
from .batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result
from .batch_manifest import operational_sample_output_root
from .batch_training.record_publication import decode_current_committed_classifier_record_set


def classified_region_counts(authority, snapshot, dependencies, summaries):
    selected = decode_committed_primary_classifier_inference_result(authority)
    ref = selected.artifact_ref
    sample = next(item for item in snapshot.manifest.enabled_samples
                  if item.sample_id == ref.scope.sample_id)
    source = next((item for item in snapshot.committed_artifact_authorities
                   if item.artifact_ref.artifact_ref_sha256 == selected.classifier_record_set_ref_sha256), None)
    if source is None:
        raise ValueError('The classifier used for these regions is unavailable.')
    records = decode_current_committed_classifier_record_set(source,
        store=dependencies.training.classifier_record_set_store,
        state_store=dependencies.runtime.state_store)
    record = next((item for item in records.classifier_records
                   if item.classifier_record_sha256 == selected.selected_classifier_record_sha256), None)
    if record is None:
        raise ValueError('The classifier used for these regions is unavailable.')
    labels = {}
    if record.fit_material_sha256 is not None:
        material = dependencies.training.project_provider.load_native_classifier_fit_material(
            record.native_source_project_identity_sha256, record.fit_material_sha256,
            workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
        if material.training_observations_json is not None:
            labels = {item['class_id']: item['name']
                      for item in json.loads(material.training_observations_json).get('classes', ())}
    classes = record.owner_payload.classifier_definition.output_classes
    request, domain_area = geometry_request(selected, snapshot, dependencies)
    rows = sorted(read_region_geometry(request, dependencies.runtime.runner, summaries),
                  key=lambda row: (-row['area_um2'], row['roi_id']))
    if any(row['class_id'] not in classes for row in rows):
        raise ValueError('A saved classified region has an unavailable class.')
    measured = summarize_regions(rows, classes, domain_area)
    domain = selected.analysis_domain_bindings[0].analysis_domain_id
    total = len(rows)
    return (
        _csv_output('classified-region-counts', 'Class areas and sizes',
            ('image', 'analysis_domain', 'class_id', 'class_name', 'region_count', 'total_regions',
             'area_um2', 'analysis_domain_area_um2', 'fraction_of_analysis_domain',
             'median_region_area_um2', 'region_area_q1_um2', 'region_area_q3_um2'),
            ((sample.sample_id, domain, row['class_id'], labels.get(row['class_id'], row['class_id']),
              row['region_count'], total, row['area_um2'], domain_area, row['fraction_of_analysis_domain'],
              row['median_region_area_um2'], row['region_area_q1_um2'], row['region_area_q3_um2'])
             for row in measured)),
        _csv_output('classified-region-sizes', 'Individual region sizes (largest first)',
            ('image', 'analysis_domain', 'roi_id', 'path_object_id', 'class_id', 'class_name',
             'area_um2', 'x_level0', 'y_level0', 'width_level0', 'height_level0'),
            ((sample.sample_id, domain, row['roi_id'], row['path_object_id'], row['class_id'],
              labels.get(row['class_id'], row['class_id']), row['area_um2'], *row['bounds_level0'])
             for row in rows)),
    )


def summarize_regions(rows, classes, domain_area):
    """Keep the accepted-domain denominator, including any Ignore class.

    Quartiles use linear interpolation between ordered object areas. No spatial
    union is computed here; we do not label the complement of summed areas a gap.
    """
    from math import fsum
    import numpy as np

    grouped = {class_id: [] for class_id in classes}
    for row in rows:
        grouped[row['class_id']].append(row['area_um2'])
    result = []
    for class_id, values in grouped.items():
        q1, median, q3 = (np.quantile(values, (.25, .5, .75), method='linear').tolist()
                          if values else (None, None, None))
        area = fsum(values)
        result.append(dict(class_id=class_id, region_count=len(values), area_um2=area,
            fraction_of_analysis_domain=area/domain_area, median_region_area_um2=median,
            region_area_q1_um2=q1, region_area_q3_um2=q3))
    return result


def geometry_request(selected, snapshot, dependencies):
    """Bind the existing saved inventory to its accepted domain and native entry."""
    from pathlib import Path
    from .region_review_geometry import saved_region_review

    ref = selected.artifact_ref
    binding = selected.analysis_domain_bindings[0]
    domains = [item for item in snapshot.committed_artifact_authorities
               if item.artifact_ref.artifact_kind == 'accepted_analysis_domain'
               and binding in item.artifact_ref.scope.analysis_domain_bindings]
    if len(domains) != 1:
        raise ValueError('The accepted analysis domain for these regions is unavailable.')
    review = saved_region_review(dependencies.runtime.state_store, domains[0], snapshot)
    domain = next(row for row in review['regions']
                  if row['layer'] == 'analysis' and row['region_id'] == binding.analysis_domain_id)
    sample = next(item for item in snapshot.manifest.enabled_samples
                  if item.sample_id == ref.scope.sample_id)
    output_root = operational_sample_output_root(sample, dependencies.runtime.state_store.layout.root) / 'analysis'
    hierarchy = Path(snapshot.head.project_path).parent / 'data' / binding.project_entry_id / 'data.qpdata'
    if not hierarchy.is_file():
        raise ValueError('The saved native hierarchy is unavailable in this analysis workspace.')
    return dict(
        hierarchy_file=str(hierarchy),
        inventory_root=str(output_root), roi_inventory=selected.roi_inventory.to_mapping(),
        producer_attempt_id=ref.producer_attempt_id,
        analysis_domain_id=binding.analysis_domain_id,
        analysis_domain_revision_sha256=binding.analysis_domain_revision_sha256,
        domain_set_sha256=binding.domain_set_sha256,
        analysis_domain_path_object_id=selected.analysis_domain_path_object_id,
        x_um_per_px=review['x_um_per_px'], y_um_per_px=review['y_um_per_px'],
    ), domain['area_um2']


def read_region_geometry(request, runner, summaries):
    """Run a read-only hierarchy projection through the existing evidence runner.

    It opens one qpdata and the admitted narrow inventory, without loading an
    ImageServer, visiting other entries, or saving the hierarchy. Results owns
    reuse of the returned tables in its existing head/result cache.
    """
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from mif_pipeline.qupath_runtime.scripts import materialize_evidence_script, QuPathScriptAsset

    Path(summaries).mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='classified-regions-', dir=summaries) as directory:
        folder = Path(directory)
        script = materialize_evidence_script(folder, QuPathScriptAsset.EXPORT_CLASSIFIED_REGION_GEOMETRY)
        request_path, result_path = folder/'request.json', folder/'result.json'
        request_path.write_text(json.dumps(request, allow_nan=False), encoding='utf-8')
        runner.run_evidence_script(project_file=folder/'unused.qpproj', script=script,
            request_paths=(request_path,), log_directory=folder/'logs', require_existing_project=False,
            evidence_path=result_path)
        result = json.loads(result_path.read_text(encoding='utf-8'))
        return result if request.get('segmentation_union') else result['regions']
