"""Dependency-light identity contracts for batch classifier inference.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .batch_artifacts import ArtifactIdentityContract

_COMMON_REQUIRED_FIELDS = (
    "adapter_spec_sha256",
    "analysis_domain_binding_set_sha256",
    "analysis_domain_path_object_id",
    "application_request_sha256",
    "attempt_authorization_sha256",
    "authorized_entry_sha256",
    "canonical_image_id",
    "canonical_image_sha256",
    "classifier_id",
    "classifier_kind",
    "classifier_record_set_ref_sha256",
    "classifier_record_set_sha256",
    "classifier_version",
    "current_image_identity_sha256",
    "deployment_binding_set_sha256",
    "execution_exchange_sha256",
    "generation_ids",
    "generation_manifest_byte_count",
    "generation_manifest_payload_sha256",
    "input_artifact_set_sha256",
    "native_classifier_byte_count",
    "native_classifier_sha256",
    "node_id",
    "object_outputs_sha256",
    "pipeline_run_id",
    "pixel_classifier_definition_sha256",
    "prerequisite_artifact_ref_sha256s",
    "process_evidence_sha256",
    "project_authority_head_sha256",
    "project_entry_projection_sha256",
    "project_identity_sha256",
    "project_qpproj_snapshot_sha256",
    "project_snapshot_sha256",
    "projection_target_sha256",
    "request_capability_sha256",
    "request_codec_id",
    "request_codec_version",
    "result_byte_count",
    "result_payload_sha256",
    "saved_state_after_sha256",
    "saved_state_before_sha256",
    "saved_state_disposition",
    "scientific_request_body_byte_count",
    "scientific_request_body_sha256",
    "script_sha256",
    "section_id",
    "selected_classifier_record_sha256",
    "selected_output_id",
    "target_evidence_set_sha256",
    "target_project_identity_sha256",
    "target_request_sha256",
    "tissue_generation_id",
    "validated_exchange_sha256",
    "validation_input_authority_sha256",
)

_ROI_REQUIRED_FIELDS = tuple(f"roi_inventory.{field}" for field in ("path", "sha256", "byte_count", "row_count"))
_PRIMARY_DECISION_REQUIRED_FIELDS = (
    "primary_count_decision.schema_version", "primary_count_decision.roi_inventory_sha256",
    "primary_count_decision.decision_sha256",
    *(f"primary_count_decision.domain.{field}" for field in (
        "analysis_domain_id", "kind", "slide_id", "section_id", "tissue_generation_id")),
    *(f"primary_count_decision.outputs.{field}" for field in (
        "output_id", "class_ids", "observed_count", "minimum", "maximum", "status")),
)
_PRIMARY_DECISION_OPTIONAL_FIELDS = (
    "primary_count_decision.domain_exclusion",
    *(f"primary_count_decision.domain_exclusion.domain.{field}" for field in (
        "analysis_domain_id", "kind", "slide_id", "section_id", "tissue_generation_id")),
    *(f"primary_count_decision.domain_exclusion.{field}" for field in (
        "status", "failing_output_ids", "reason")),
)


# ==============================================================================
#                      Batch classifier inference contracts
# ==============================================================================


def primary_classifier_inference_result_identity_contract() -> ArtifactIdentityContract:
    """Return the locatorless owner contract for primary inference results."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-primary-classifier-inference-result-identity/v1",
        artifact_kind="primary_inference_result",
        schema_name="roi-generation-manifest",
        schema_version=1,
        required_fields=tuple(
            sorted(
                {
                    *_COMMON_REQUIRED_FIELDS,
                    *_ROI_REQUIRED_FIELDS,
                    *_PRIMARY_DECISION_REQUIRED_FIELDS,
                    "domain_status",
                    "primary_count_review_action",
                }
            )
        ),
        optional_fields=tuple(sorted(_PRIMARY_DECISION_OPTIONAL_FIELDS)),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def secondary_classifier_inference_result_identity_contract() -> ArtifactIdentityContract:
    """Return the locatorless owner contract for secondary inference results."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-secondary-classifier-inference-result-identity/v1",
        artifact_kind="secondary_inference_result",
        schema_name="roi-generation-manifest",
        schema_version=1,
        required_fields=tuple(
            sorted(
                {
                    *_COMMON_REQUIRED_FIELDS,
                    *_ROI_REQUIRED_FIELDS,
                    "eligible_parent_ids_sha256",
                    "predecessor_result_ref_sha256",
                    "primary_domain_status",
                    *(f"parent_sources.{field}" for field in (
                        "eligible_parent_id", "source_kind", "source_result_ref_sha256",
                        "producer_attempt_id", "analysis_domain_path_object_id")),
                    "primary_inference_result_ref_sha256",
                    "structure_id",
                }
            )
        ),
        optional_fields=tuple(sorted({
            "eligible_parent_ids", "parent_sources.roi_inventory",
            *(f"parent_sources.roi_inventory.{field}" for field in ("path", "sha256", "byte_count", "row_count")),
        })),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


__all__ = [
    "primary_classifier_inference_result_identity_contract",
    "secondary_classifier_inference_result_identity_contract",
]
