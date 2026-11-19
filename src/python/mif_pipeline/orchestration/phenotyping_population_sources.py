"""Bind phenotyping region memberships to current accepted classifier results."""
from __future__ import annotations

from .batch_artifacts import BatchArtifactError
from .batch_classifier_inference_artifacts import (
    decode_committed_primary_classifier_inference_result,
)


def current_population_sources(*, snapshot, measurement, config):
    """Select the completed classifier tables for the measured section.

    The execution snapshot contains the currently accepted results. We match
    their image, segmentation generation, configuration and analysis revisions;
    an ambiguous current result is an error rather than a guessed population.
    These results are already predecessors of section segmentation. Keeping
    their exact references in the measurement header also lets later readers
    reject changed classifier sources without comparing cell display classes.
    """
    domains = {item.analysis_domain_id: item for item in measurement.analysis_domain_bindings}
    sources = []
    seen = set()
    for authority in snapshot.committed_artifact_authorities:
        ref = authority.artifact_ref
        if ref.artifact_kind != "primary_inference_result":
            continue
        if (ref.scope.sample_id != measurement.sample_id or ref.scope.project_entry_id != measurement.project_entry_id
                or ref.scope.slide_id != measurement.slide_id
                or ref.configuration_bindings != measurement.artifact_ref.configuration_bindings):
            continue
        projected = decode_committed_primary_classifier_inference_result(authority)
        if projected.section_id != measurement.section_id or projected.domain_status != "active":
            continue
        if len(projected.analysis_domain_bindings) != 1:
            raise BatchArtifactError("A phenotyping classifier source covers more than one analysis region")
        binding = projected.analysis_domain_bindings[0]
        if binding.analysis_domain_id not in domains:
            continue
        if (binding != domains[binding.analysis_domain_id]
                or projected.canonical_image_id != measurement.canonical_image_id
                or projected.tissue_generation_id != measurement.tissue_generation_id):
            raise BatchArtifactError("The phenotyping classifier source uses an earlier image or analysis region")
        kind = "primary_structure"
        classes = sorted(class_id for class_id in projected.primary_count_decision.outputs[0].class_ids
                         if class_id != "Ignore")
        if not classes:
            continue
        for class_id in classes:
            key = (kind, binding.analysis_domain_id, class_id)
            if key in seen:
                raise BatchArtifactError("More than one accepted classifier result describes the selected phenotyping region")
            seen.add(key)
        sources.append({"kind": kind, "class_ids": classes,
            "analysis_domain_id": binding.analysis_domain_id,
            "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
            "source_result_sha256": ref.artifact_ref_sha256,
            "producer_attempt_id": ref.producer_attempt_id,
            "roi_inventory": projected.roi_inventory.to_mapping()})
    return sorted(sources, key=lambda item: (item["kind"], item["analysis_domain_id"], item["class_ids"]))


def population_source_references(sources):
    """Project exact accepted source references for a later header comparison."""
    return frozenset(item["source_result_sha256"] for item in sources)
