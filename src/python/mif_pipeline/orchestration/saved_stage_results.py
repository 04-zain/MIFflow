"""Read current completed-stage records and draw already-saved scientific values.

The execution snapshot selects results. This module neither advances a review nor
reconstructs missing scientific work; temporary figure files belong to the service.
"""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
from urllib.parse import quote

from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

from .batch_service_models import BatchSavedStageFigure, BatchSavedStageResult, BatchSavedStageResults


# Scientific results in workflow order; control and retirement records stay internal.
_STAGES = {
    "processed_image": "Prepared image",
    "tissue_acceptance_result": "Tissue regions",
    "registration_result": "Image alignment",
    "accepted_analysis_domain": "Analysis regions",
    "classifier_record_set": "Accepted classifiers",
    "primary_inference_result": "Primary tissue classification",
    "secondary_inference_result": "Secondary tissue classification",
    "segmentation_result": "Cell segmentation",
    "measurements_result": "Cell measurements",
    "marker_gate": "Accepted marker gates",
    "marker_phenotype_result": "Marker phenotypes",
    "cluster_review_evidence": "Unsupervised phenotyping",
    "cluster_label_import_result": "Accepted cluster labels",
    "supervised_phenotype_result": "Supervised cell phenotypes",
    "spatial_cell_handoff": "Accepted cell results",
}


def _classifier_records(authority, dependencies):
    from .batch_training.record_publication import decode_current_committed_classifier_record_set

    return decode_current_committed_classifier_record_set(
        authority, store=dependencies.training.classifier_record_set_store,
        state_store=dependencies.runtime.state_store,
    )


def _confusions(record_set):
    from .batch_training._shared import ClassifierKind, EvaluationIntent

    for record in record_set.classifier_records:
        if (record.evaluation_intent is EvaluationIntent.TRAINING_ONLY
                or record.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE):
            continue
        for metric in record.metrics:
            if metric.pixel_confusion is not None:
                identity = f"confusion/{quote(record.classifier_id, safe='')}/{metric.split}"
                yield identity, record, metric, None
                for subject in metric.pixel_confusion.per_subject:
                    yield f"{identity}/subject/{quote(subject, safe='')}", record, metric, subject


def _umap_choices(authority, snapshot, summaries, *, evidence=None):
    """Match existing coordinate metadata to the exact selected clustering inputs."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from mif_pipeline.cell_analysis.embeddings import EmbeddingPolicy
    from .batch_phenotyping._common import digest
    from .batch_phenotyping.review_authority import reopen_cluster_review_evidence_authority

    if evidence is None:
        evidence = reopen_cluster_review_evidence_authority(authority)
    current = {item.artifact_ref.artifact_ref_sha256 for item in snapshot.committed_artifact_authorities}
    if not {evidence.clustering_result_ref_sha256, *evidence.source_authority_sha256s} <= current:
        raise ValueError("Saved coordinates belong to a different clustering result.")
    directory = summaries / "umap"
    if directory.is_symlink():
        raise ValueError("The saved UMAP folder has moved.")
    if not directory.exists():
        return ()
    matched = []
    policy_fields = tuple(item.name for item in fields(EmbeddingPolicy))
    profiles = evidence.clusters[0].marker_profiles
    attributes = (("cluster_id", "Clusters"), *((item.feature_id, item.measurement) for item in profiles))
    for path in sorted(directory.glob("*.parquet")):
        if path.is_symlink():
            continue
        try:
            footer = pq.read_metadata(path)
            metadata = json.loads(footer.metadata[b"mif:umap"])
            policy = {name: metadata["parameters"][name] for name in policy_fields}
            EmbeddingPolicy(**policy)
            key = digest({"clustering": evidence.clustering_result_ref_sha256,
                          "sources": evidence.source_authority_sha256s,
                          "policy": policy, "umap": metadata["backend_version"]})
            if path.stem != key or metadata["key"] != key or not 0 < footer.num_rows <= evidence.cell_count:
                continue
        except (pa.ArrowInvalid, KeyError, TypeError, ValueError):
            continue
        detail = (f"Saved {footer.num_rows:,}-cell UMAP; neighbors {policy['neighbors']}, "
                  f"minimum distance {policy['min_dist']}, seed {policy['seed']}. "
                  "Uses retained coordinates; no recalculation.")
        matched.append((key, policy, metadata["backend_version"], detail))
    different = tuple(name for name in policy_fields
                      if len({policy[name] for _, policy, _, _ in matched}) > 1)
    versions_differ = len({version for _, _, version, _ in matched}) > 1
    labels = {
        "max_cells": lambda value: f"maximum {value:,} cells",
        "neighbors": lambda value: f"{value} neighbours",
        "min_dist": lambda value: f"distance {value:g}",
        "metric": lambda value: value,
        "seed": lambda value: f"seed {value}",
        "full_cell": lambda value: "all cells required" if value else "sampling allowed",
    }
    choices = []
    for key, policy, version, detail in matched:
        settings = [labels[name](policy[name]) for name in different]
        if versions_differ:
            settings.append(f"UMAP {version}")
        suffix = " (" + ", ".join(settings) + ")" if settings else ""
        choices.extend(BatchSavedStageFigure(
            f"umap/{key}/{quote(attribute, safe='')}", f"UMAP · {label}{suffix}", detail,
            data_formats=("csv",)) for attribute, label in attributes)
    return tuple(choices)


def _figure_choices(authority, snapshot, dependencies, summaries):
    kind = authority.artifact_ref.artifact_kind
    if kind in {"segmentation_result", "measurements_result"}:
        from .saved_cell_figures import cell_figure_choices
        from .saved_cell_gallery import gallery_choice
        store = dependencies.runtime.state_store
        choices = (*cell_figure_choices(authority, snapshot, state_store=store),
                   *gallery_choice(authority, snapshot, state_store=store))
        return choices, ("Select a saved shape measurement to locate cells and inspect their outlines."
            if kind == 'segmentation_result' else
            "Select a measurement to compare its values with the cell images.")
    if kind in {"tissue_acceptance_result", "accepted_analysis_domain"}:
        return (BatchSavedStageFigure(
            "region-outlines", "Tissue outlines" if kind == "tissue_acceptance_result" else "Analysis-region outlines",
            "Outlines at their saved image positions, with areas and dimensions available as CSV.",
            data_formats=("csv",)),), ""
    if kind == "spatial_cell_handoff":
        from .saved_coexpression_figures import coexpression_choices
        from .saved_cell_figures import cell_figure_choices
        from .saved_secondary_results import secondary_choices

        calls, call_message = coexpression_choices(authority, snapshot)
        measurements = cell_figure_choices(authority, snapshot)
        from .saved_cell_gallery import gallery_choice
        choices = (*measurements, *calls, *secondary_choices(authority), *gallery_choice(authority, snapshot))
        return choices, ("Saved cell measurements, phenotype counts and available marker calls." if choices else call_message)
    if kind == "processed_image":
        from .saved_reference_figures import reference_basis

        basis = reference_basis(authority, snapshot)
        if basis is None:
            return (), "This image was prepared without a saved unmixing reference library."
        from .saved_reconstruction_figures import reconstruction_choice
        diagnostics, message = reconstruction_choice(authority, snapshot, basis)
        return (BatchSavedStageFigure("reference-spectra", "Accepted reference spectra",
                "Component spectra used to prepare this image, displayed as fractions of total signal.",
                data_formats=("csv",)), *diagnostics), \
               (message or "The accepted reference spectra and reconstruction diagnostic are available.")
    if kind == "primary_inference_result":
        return (BatchSavedStageFigure("classified-region-counts", "Classified regions",
            "Areas exclude holes; percentages use the accepted analysis-domain area. "
            "Each saved object counts as one region, including any disconnected parts.",
            data_formats=("csv",)),), ""
    if kind == "classifier_record_set":
        records = _classifier_records(authority, dependencies)
        choices = tuple(BatchSavedStageFigure(identity,
                        f"{record.classifier_id} · {metric.split.title()} confusion" +
                        (f" · Subject {subject}" if subject is not None else " · All subjects"),
                        "Each reference region contributes one majority-class prediction. "
                        "Counts and percentages use the selected subject's references." if subject is not None else
                        "Each reference region contributes one majority-class prediction. "
                        "Counts and percentages combine all subjects in this split.", data_formats=("csv",))
                        for identity, record, metric, subject in _confusions(records))
        from .saved_precision_recall import precision_recall_choices
        pr_choices = precision_recall_choices(records, dependencies)
        choices = (*choices, *pr_choices)
        pr_message = (" Precision–recall is unavailable without retained native RF held-out pixel scores."
                      if not pr_choices and any(record.classifier_kind.value != "supervised_phenotype"
                                                for record in records.classifier_records) else "")
        if not all(record.fit_material_sha256 is not None for record in records.classifier_records):
            return choices, "The accepted classifier result is saved." + pr_message
        tables = BatchSavedStageFigure("classifier-recorded-tables", "Classifier review and data",
            "Saved recipe, subject allocation, fitting contributions and available evaluation locations. Counts retain their recorded units.",
            data_formats=("csv",))
        return (*choices, tables), "Saved classifier evidence is available for review and export." + pr_message
    if kind == "cluster_review_evidence":
        from .batch_phenotyping.review_authority import reopen_cluster_review_evidence_authority

        evidence = reopen_cluster_review_evidence_authority(authority)
        choices = _umap_choices(authority, snapshot, summaries, evidence=evidence)
        current = BatchSavedStageFigure("umap-current", "UMAP · Current fit",
            "Calculate a view of the accepted clustering measurements, or reuse matching saved coordinates.",
            settings={"measurements": tuple((item.measurement, item.feature_id)
                for item in evidence.clusters[0].marker_profiles)}, data_formats=("csv",))
        result = next(item for item in snapshot.committed_artifact_authorities
                      if item.artifact_ref.artifact_ref_sha256 == evidence.clustering_result_ref_sha256)
        model = (BatchSavedStageFigure("flowsom-map", "FlowSOM learned profiles",
            "The learned marker profiles and their minimum spanning tree. Profile and connection data are available for export.",
            data_formats=("csv",)),) if (evidence.refit_summary['method']=='flowsom' if evidence.refit_summary is not None
                else result.artifact_ref.owning_identity['graph_algorithm'] == 'flowsom/v1') else ()
        lineage=BatchSavedStageFigure('cluster-memberships','Cluster memberships and lineage',
            'Current, original and parent cluster IDs with the exact image, section and cell identities.',data_formats=('csv',))
        return (current, *model, lineage, *choices), ""
    if kind == "marker_gate":
        from .saved_marker_figures import marker_figures

        choices = tuple(item.descriptor for item in marker_figures(authority, snapshot, dependencies.runtime.state_store))
        return choices, ("Accepted gates and their saved review distributions are available." if choices else
                         "No retained review distribution matches these accepted marker gates.")
    return (), "The accepted result is saved. Figure reopening is not available for this stage."


@schema_validation_snapshot()
def project_saved_stages(snapshot, dependencies, summaries: Path) -> BatchSavedStageResults:
    """List selected completed scientific outputs without requiring final publication."""
    rows = []
    order = {kind: index for index, kind in enumerate(_STAGES)}
    sample_order = {sample.sample_id:index for index,sample in enumerate(snapshot.manifest.enabled_samples)}
    node_order = {node.node_id:index for index,node in enumerate(snapshot.plan.nodes)}
    authorities = sorted(snapshot.committed_artifact_authorities, key=lambda item: (
        order.get(item.artifact_ref.artifact_kind, len(order)),
        sample_order.get(item.artifact_ref.scope.sample_id, len(sample_order)),
        node_order.get(item.artifact_ref.producer_node_id, len(node_order))))
    for authority in authorities:
        ref = authority.artifact_ref
        if ref.artifact_kind not in _STAGES:
            continue
        sample_ids = ((ref.scope.sample_id,) if ref.scope.sample_id else
                      tuple(sorted({item.sample_id for item in ref.scope.analysis_domain_bindings})))
        try:
            choices, message = _figure_choices(authority, snapshot, dependencies, summaries)
        except (OSError, ValueError) as error:
            choices, message = (), f"Saved figure unavailable: {error}"
        reference_display = None
        if ref.artifact_kind == "registration_result":
            from .batch_registration import decode_committed_registration_result

            registration = decode_committed_registration_result(authority)
            if registration.mode == "warpy":
                reference_display = {"mode": "overlay",
                    "fixed_entry_id": registration.primary_project_entry_id,
                    "moving_entry_id": registration.moving_project_entry_id}
                message = "Inspect the accepted alignment using the saved transform and original images."
        rows.append(BatchSavedStageResult(ref.artifact_ref_sha256, authority.producer_node_kind,
                    _STAGES[ref.artifact_kind], sample_ids, ref.locator, choices, message,
                    reference_display=reference_display))
    return BatchSavedStageResults(snapshot.head.batch_id, snapshot.head.sha256, tuple(rows))


def render_saved_stage(authority, figure_id, snapshot, dependencies, summaries, *, figure_options=None):
    """Render an explicitly selected saved figure; missing records never trigger a fit."""
    if authority.artifact_ref.artifact_kind in {"tissue_acceptance_result", "accepted_analysis_domain"}:
        if figure_id != "region-outlines":
            raise ValueError("Select the outline figure for this saved result.")
        from .region_review_geometry import saved_region_review
        from .region_review_figures import region_review_outputs
        return region_review_outputs(saved_region_review(dependencies.runtime.state_store, authority, snapshot),
                                     palette=(figure_options or {}).get("palette", "teal"))
    if authority.artifact_ref.artifact_kind == "spatial_cell_handoff":
        from .saved_coexpression_figures import render_coexpression
        from .saved_cell_figures import render_cell_figure

        if figure_id == 'secondary-structure-tables':
            from .saved_secondary_results import secondary_tables
            return secondary_tables(authority)
        if figure_id == 'secondary-outlines':
            from .saved_secondary_results import secondary_outlines
            return secondary_outlines(authority, palette=(figure_options or {}).get('palette', 'teal'))
        if figure_id.startswith("coexpression/"):
            return render_coexpression(authority, figure_id, snapshot, figure_options=figure_options)
        return render_cell_figure(authority, figure_id, snapshot, dependencies, figure_options=figure_options)
    if authority.artifact_ref.artifact_kind == "processed_image":
        from .saved_reference_figures import reference_basis, render_reference_figures

        basis = reference_basis(authority, snapshot)
        if basis is None or figure_id != "reference-spectra":
            raise ValueError("This figure is not available for the selected saved result.")
        return render_reference_figures(basis)
    if authority.artifact_ref.artifact_kind == "primary_inference_result":
        if figure_id != "classified-region-counts":
            raise ValueError("Select classified region counts for this result.")
        from .saved_classified_region_counts import classified_region_counts
        return classified_region_counts(authority, snapshot, dependencies, summaries)
    if authority.artifact_ref.artifact_kind == "classifier_record_set":
        from mif_pipeline.compartment_analysis.evaluation_plots import render_classifier_confusion_figure

        record_set = _classifier_records(authority, dependencies)
        if figure_id.startswith("precision-recall/"):
            from .saved_precision_recall import render_saved_precision_recall
            return render_saved_precision_recall(record_set, dependencies, figure_id)
        if figure_id == "classifier-recorded-tables":
            from .saved_classifier_tables import saved_classifier_tables
            return saved_classifier_tables(record_set, snapshot, dependencies)
        selected = next((item for item in _confusions(record_set) if item[0] == figure_id), None)
        if selected is None:
            raise ValueError("This figure is not available for the selected saved result.")
        _, record, metric, subject = selected
        confusion = metric.pixel_confusion
        counts = confusion.pooled if subject is None else confusion.per_subject[subject]
        subject_count = len(confusion.per_subject) if subject is None else 1
        label = f"{record.classifier_id} · {metric.split.title()}" + (
            f" · Subject {subject}" if subject is not None else " · All subjects")
        class_names = {}
        if record.fit_material_sha256 is not None:
            material = dependencies.training.project_provider.load_native_classifier_fit_material(
                record.native_source_project_identity_sha256, record.fit_material_sha256,
                workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
            if material.training_observations_json is not None:
                import json
                class_names = {item["class_id"]: item["name"]
                               for item in json.loads(material.training_observations_json).get("classes", ())}
        values = dict(classifier_label=record.classifier_id, split=metric.split,
                      patient_count=subject_count,
                      class_ids=tuple(class_names.get(identity, identity) for identity in confusion.class_ids),
                      confusion_matrix=counts)
        outputs = tuple((figure_id + "/" + fmt, label, fmt,
                      render_classifier_confusion_figure(image_format=fmt, **values))
                     for fmt in ("png", "pdf", "svg"))
        from mif_pipeline.cell_analysis.result_plots import _csv_output
        data = _csv_output(figure_id, "Classifier confusion data",
            ("classifier", "split", "subject_count", "reference_class", "predicted_class",
             "count", "reference_total", "fraction_of_reference_class", "subject_id", "observation_unit"),
            ((record.classifier_id, metric.split, subject_count, reference, predicted,
              count, total, count / total if total else None, subject, confusion.observation_unit)
             for reference, row in zip(confusion.class_ids, counts, strict=True)
             for total in (sum(row),)
             for predicted, count in zip(confusion.class_ids, row, strict=True)))
        return (*outputs, data)
    if authority.artifact_ref.artifact_kind == "cluster_review_evidence":
        from urllib.parse import unquote
        from .batch_phenotyping.authority import decode_committed_preparation
        from .batch_phenotyping.review_authority import reopen_cluster_review_evidence_authority
        from .batch_phenotyping.review_plots import read_view, render_view

        evidence = reopen_cluster_review_evidence_authority(authority)
        if figure_id=='cluster-memberships':
            from .batch_phenotyping.codec import decode_committed_batch_clustering_result
            from mif_pipeline.cell_analysis.result_plots import _csv_output
            authorities={item.artifact_ref.artifact_ref_sha256:item for item in snapshot.committed_artifact_authorities}
            owner=authorities[evidence.clustering_result_ref_sha256]
            result=decode_committed_batch_clustering_result(owner,
                source_authorities=tuple(authorities[key] for key in evidence.source_authority_sha256s),
                maximum_byte_count=owner.artifact_ref.byte_count)
            if result.result_sha256!=evidence.clustering_result_sha256:
                raise ValueError('The memberships differ from the current cluster review.')
            columns=('sample_id','project_entry_id','slide_id','section_id','analysis_domain_id','cell_id',
                'original_cluster_id','parent_cluster_id','cluster_id')
            return (_csv_output(figure_id,'Cluster memberships and lineage',columns,
                ((row.sample_id,row.project_entry_id,row.slide_id,row.section_id,row.analysis_domain_id,row.cell_id,
                  row.original_cluster_id or row.cluster_id,row.parent_cluster_id or row.cluster_id,row.cluster_id)
                 for row in result.assignments)),)
        if figure_id == 'flowsom-map':
            from .batch_phenotyping.flowsom_review import model_view
            from mif_pipeline.cell_analysis.flowsom_plots import model_outputs
            model = model_view(snapshot, evidence)
            if model is None:
                raise ValueError('Select a FlowSOM result to inspect its learned profiles.')
            return model_outputs(model)
        choices = _umap_choices(authority, snapshot, summaries, evidence=evidence)
        if figure_id not in {item.figure_id for item in choices}:
            raise ValueError("This figure is not available for the selected saved result.")
        preparations = [item for item in snapshot.committed_artifact_authorities
                        if item.artifact_ref.artifact_ref_sha256 in evidence.source_authority_sha256s
                        and item.artifact_ref.artifact_kind == "unsupervised_preparation_result"]
        decoded = [decode_committed_preparation(item, maximum_byte_count=64 * 1024 * 1024)
                   for item in preparations]
        if not decoded or any(item.feature_specs != decoded[0].feature_specs for item in decoded):
            raise ValueError("Saved UMAP feature definitions are unavailable or inconsistent.")
        _, key, attribute = figure_id.split("/", 2)
        import pyarrow.parquet as pq
        from mif_pipeline.pipeline_core.execution_resources import operation_budget
        from .batch_resources import BatchResourceDemand, ResourceLeasePool
        from .batch_dag import ResourceKind
        from .batch_phenotyping._common import digest

        path = summaries / "umap" / (key + ".parquet")
        count = pq.read_metadata(path).num_rows
        memory_bytes = 128 * 1024**2 + 512 * count + 32 * count * len(decoded[0].feature_specs)
        demand = BatchResourceDemand(
            node_id=authority.artifact_ref.producer_node_id,
            adapter_spec_sha256=digest({"operation": "saved-umap-view"}),
            input_artifact_set_sha256=key, resource_kind=ResourceKind.SCIENTIFIC_COMPUTE,
            worker_slots=1, future_slots=1, working_set_bytes=memory_bytes, cpu_threads=1)
        pool = ResourceLeasePool(snapshot.manifest.manifest.execution_config)
        reservation = pool.try_acquire(demand)
        if reservation is None:
            raise ValueError("Memory is in use. Wait for processing to finish and open the saved figure again.")
        try:
            with operation_budget(memory_bytes, 1):
                view = read_view(path, key, decoded[0].feature_specs)
                if view is None:
                    raise ValueError("Saved UMAP coordinates are missing or damaged; no calculation was started.")
                return render_view(view, unquote(attribute))
        finally:
            reservation.release()
    if authority.artifact_ref.artifact_kind == "marker_gate":
        from .saved_marker_figures import marker_figures, render_marker_figure

        selected = next((item for item in marker_figures(authority, snapshot, dependencies.runtime.state_store)
                         if item.descriptor.figure_id == figure_id), None)
        if selected is None:
            raise ValueError("This figure is not available for the selected saved result.")
        return render_marker_figure(selected)
    raise ValueError("Saved figure rendering is unavailable for this stage.")
