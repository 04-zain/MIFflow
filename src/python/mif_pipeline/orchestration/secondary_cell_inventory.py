"""Recover the phenotype choices defined by an accepted cell result.

Cell-category dictionaries contain observed labels only. Region review also
needs declared labels with zero cells in a section, so we read the exact saved
rules, classifier or cluster review used to produce that section's result.
"""
from __future__ import annotations

from .batch_artifacts import BatchArtifactError, validate_committed_artifact_authority_set


def phenotype_selector_inventory(source_ref, snapshot, *, classifier_record_set_store):
    """Return declared phenotype labels and marker calls without reading cells.

    Only artifacts recorded in this phenotype result's source set may supply
    choices. The existing owner readers check their saved definitions and byte
    limits. Labels are the strings written to ``primary_phenotype``: marker
    display names, supervised class IDs, or reviewed cluster names/unnamed IDs.
    """
    supported = {
        "marker_phenotype_result", "supervised_phenotype_result", "cluster_label_import_result"
    }
    if source_ref.artifact_kind not in supported:
        raise BatchArtifactError("Secondary review requires a completed phenotype result.")
    current = tuple(snapshot.committed_artifact_authorities)
    owners = [item for item in current if item.artifact_ref == source_ref]
    if len(owners) != 1:
        raise BatchArtifactError("The selected phenotype result is not current. Reopen the cell results.")
    validate_committed_artifact_authority_set((owners[0],))
    source_ids = set(source_ref.owning_identity["source_ref_sha256s"])

    def source(kind):
        matches = [item for item in current
                   if item.artifact_ref.artifact_kind == kind
                   and item.artifact_ref.artifact_ref_sha256 in source_ids]
        if len(matches) != 1:
            label = {
                "marker_gate": "marker decisions",
                "marker_combination": "marker-rule definition",
                "classifier_record_set": "cell classifier",
                "accepted_cluster_label_review": "cluster-name review",
            }[kind]
            raise BatchArtifactError(
                f"The saved {label} could not be matched to this phenotype result. Reopen its accepted settings."
            )
        validate_committed_artifact_authority_set((owners[0], matches[0]))
        return matches[0]

    marker_ids = ()
    if source_ref.artifact_kind == "marker_phenotype_result":
        from .batch_phenotyping.marker_application_execution import decode_committed_marker_gate
        from .batch_phenotyping.marker_combinations import decode_marker_combination

        gate = decode_committed_marker_gate(source("marker_gate"))
        definition = decode_marker_combination(source("marker_combination"), gate)
        if not set(source_ref.scope.analysis_domain_bindings) <= set(gate.artifact_ref.scope.analysis_domain_bindings):
            raise BatchArtifactError("The accepted marker definitions do not cover this phenotype result.")
        labels = [item["display_name"] for item in definition["phenotype_classes"]]
        marker_ids = gate.canonical_marker_ids
    elif source_ref.artifact_kind == "supervised_phenotype_result":
        from .batch_training.records import ClassifierKind
        from .batch_training.record_publication import classifier_record_set_identity_contract

        record_ref = source("classifier_record_set").artifact_ref
        if record_ref.identity_contract != classifier_record_set_identity_contract():
            raise BatchArtifactError("The phenotype classifier has no supported saved class definition.")
        record_set = classifier_record_set_store.reopen(
            record_ref.owning_identity["classifier_record_set_sha256"]
        )
        records = [item for item in record_set.classifier_records
                   if item.classifier_record_sha256 == source_ref.owning_identity["classifier_record_sha256"]]
        if (len(records) != 1 or records[0].classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE
                or records[0].review_decision != "accepted"
                or records[0].owner_payload is None
                or records[0].validation_domain_id != source_ref.scope.validation_domain_id):
            raise BatchArtifactError("The saved phenotype classifier does not match this accepted result.")
        # Confidence rejection defines this outcome even when no cell is rejected.
        labels = [*records[0].owner_payload.class_ids, "Unclassified"]
    else:
        from .batch_phenotyping.application_codec import decode_committed_accepted_cluster_label_review

        review = decode_committed_accepted_cluster_label_review(source("accepted_cluster_label_review"))
        if (review.result_sha256 != source_ref.owning_identity["accepted_review_sha256"]
                or not set(source_ref.scope.analysis_domain_bindings) <= set(review.analysis_domain_bindings)):
            raise BatchArtifactError("The accepted cluster names do not belong to this phenotype result.")
        excluded = set(review.excluded_cluster_ids)
        labels = [review.cluster_labels.get(cluster_id, cluster_id)
                  for cluster_id in review.cluster_ids if cluster_id not in excluded]

    if any(not isinstance(label, str) or not label.strip() for label in labels):
        raise BatchArtifactError("The accepted phenotype definitions contain an invalid name.")
    return {
        "phenotype_ids": tuple(sorted({label for label in labels if label != "Not phenotyped"})),
        "marker_ids": tuple(sorted(marker_ids)),
    }
