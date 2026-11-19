"""Derived presentation bytes for a checked pending classifier review.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ._shared import BatchTrainingError, ClassifierKind, EvaluationIntent
from .record_publication import validate_training_fit_review_context
from .records import ClassifierRecordSet
from .planning import TrainingPlanSet

# ==============================================================================
#                                  Review plots
# ==============================================================================


def render_training_review_plots(
    context: object,
    pending_record_set: ClassifierRecordSet,
    plan_set: TrainingPlanSet,
    *, selection_reports=None, reference_votes=None,
) -> tuple[tuple[str, str, str, bytes], ...]:
    """Project retained counts and metrics into one table for the current review."""

    if type(pending_record_set) is not ClassifierRecordSet:
        raise BatchTrainingError("review figures require a typed pending classifier record set")
    review = validate_training_fit_review_context(context)
    if (
        review["pending_classifier_record_set_sha256"]
        != pending_record_set.classifier_record_set_sha256
    ):
        raise BatchTrainingError("review figure context differs from pending classifier records")
    records = pending_record_set.classifier_records
    rows = review["classifiers"]
    if (
        len(rows) != len(records)
        or tuple(row["classifier_id"] for row in rows)
        != tuple(record.classifier_id for record in records)
        or any(record.review_decision != "pending" for record in records)
    ):
        raise BatchTrainingError("review figures require exact pending classifier coverage")

    # The caller already reopened these exact plans to validate the pending review.
    # Reuse their small metadata; never load a model or image for this projection.
    if plan_set.training_plan_set_sha256 != review["training_plan_set_sha256"]:
        raise BatchTrainingError("Review details differ from the fitted training plan")
    from .review_tables import classifier_metadata_tables, classifier_location_table, classifier_cv_tables, classifier_confusion_table
    details = {row['classifier_id']: row for row in rows}
    outputs = [*classifier_metadata_tables(pending_record_set, plan_set, details),
               *classifier_location_table(pending_record_set, plan_set, details, reference_votes),
               *classifier_cv_tables(pending_record_set, plan_set, selection_reports or {})]
    for record, row in zip(records, rows, strict=True):
        if row["evaluation_intent"] != record.evaluation_intent.value:
            raise BatchTrainingError("review figure intent differs from pending classifier")
        if record.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
            continue
        if record.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            continue
        metrics = {metric.split: metric for metric in record.metrics}
        if tuple(metrics) != tuple(item["split"] for item in row["split_metrics"]):
            raise BatchTrainingError("review figure splits differ from pending classifier")
        for metric_row in row["split_metrics"]:
            split = metric_row["split"]
            metric = metrics[split]
            confusion = metric.pixel_confusion
            if confusion is None:
                raise BatchTrainingError("evaluated pixel review lacks retained confusion counts")
            patient_count = len(confusion.per_subject)
            if (
                patient_count != metric_row["patient_count"]
                or patient_count != row[f"{split}_patient_count"]
                or any(
                    metric.pooled[name] != metric_row[name]
                    for name in ("accuracy", "macro_precision", "macro_recall", "macro_f1")
                )
            ):
                raise BatchTrainingError("review figure counts differ from checked review row")
    return (*outputs, *classifier_confusion_table(pending_record_set))
