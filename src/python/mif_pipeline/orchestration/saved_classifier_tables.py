"""Reopen small accepted classifier records for the existing Results exporter."""
import json
from .batch_training.review_tables import classifier_metadata_tables, classifier_location_table, classifier_cv_tables, classifier_confusion_table, load_classifier_reference_votes


def _saved_training_plan(record_set, snapshot, dependencies):
    from .batch_training.runtime import decode_current_committed_training_plan_set

    plan_authority = next((item for item in snapshot.committed_artifact_authorities
        if item.artifact_ref.artifact_kind == 'training_plan_set'
        and item.artifact_ref.artifact_id == 'training-plan-set-' + record_set.source_training_plan_set_sha256[:48]), None)
    if plan_authority is None:
        raise ValueError('The saved classifier training plan is unavailable.')
    plan_set = decode_current_committed_training_plan_set(plan_authority,
        store=dependencies.training.training_plan_set_store,
        state_store=dependencies.runtime.state_store).plan_set
    if plan_set.training_plan_set_sha256 != record_set.source_training_plan_set_sha256:
        raise ValueError('The saved training plan does not match this classifier.')
    return plan_set


def saved_classifier_cell(authority, snapshot, dependencies, options):
    """Resolve one accepted held-out row through its retained model and geometry."""
    from .batch_training.record_publication import decode_current_committed_classifier_record_set
    from .batch_training.supervised_review_gallery import render_supervised_gallery

    records = decode_current_committed_classifier_record_set(authority,
        store=dependencies.training.classifier_record_set_store,
        state_store=dependencies.runtime.state_store)
    plans = _saved_training_plan(records, snapshot, dependencies)
    record = next((item for item in records.classifier_records
                   if item.classifier_id == options.get('classifier')), None)
    if record is None or record.classifier_kind.value != 'supervised_phenotype':
        raise ValueError('Choose an evaluated cell from this accepted cell classifier.')
    provider = dependencies.training.project_provider
    material = provider.load_native_classifier_fit_material(
        record.native_source_project_identity_sha256, record.fit_material_sha256,
        workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
    votes = {record.classifier_id: load_classifier_reference_votes(record, material, provider)}
    return render_supervised_gallery(snapshot, records, plans, votes, options)


def saved_classifier_tables(record_set, snapshot, dependencies):
    from .batch_training.cross_validation import QuPathGroupedCVConfiguration, compact_selection_summary

    plan_set = _saved_training_plan(record_set, snapshot, dependencies)
    plans = {plan.request.classifier_id: plan for plan in plan_set.plans}
    details = {}
    reports = {}
    votes = {}
    for record in record_set.classifier_records:
        material = dependencies.training.project_provider.load_native_classifier_fit_material(
            record.native_source_project_identity_sha256, record.fit_material_sha256,
            workspace_storage_scope_sha256=dependencies.runtime.state_store.workspace_storage_scope_sha256)
        votes[record.classifier_id] = load_classifier_reference_votes(
            record, material, dependencies.training.project_provider)
        row = {}
        if material.training_observations_json is not None:
            row['training_observations'] = json.loads(material.training_observations_json)
        plan = plans[record.classifier_id]
        if plan.request.model_configuration.get('configuration_schema') == 'qupath-grouped-cross-validation/v1':
            search = QuPathGroupedCVConfiguration.from_mapping(plan.request.model_configuration)
            if material.model_selection_json is None:
                raise ValueError('The saved classifier has no cross-validation selection report.')
            report = json.loads(material.model_selection_json)
            reports[record.classifier_id] = report
            row['model_selection'] = compact_selection_summary(report,
                plan.split_subject_ids['training'], plan.request.required_classes,
                len(search.candidates), search.folds, plan.request.random_seed)
        details[record.classifier_id] = row
    return (*classifier_metadata_tables(record_set, plan_set, details),
            *classifier_location_table(record_set, plan_set, details, votes),
            *classifier_cv_tables(record_set, plan_set, reports),
            *classifier_confusion_table(record_set))
