"""Authenticated batch owner for unsupervised protein-marker phenotyping.

The wire-level feature role token ``expression`` is retained for compatibility; the values
are image-derived protein-marker intensities, not transcript data. Neutral cluster IDs are
projected per entry for later pooled expert review. This package authenticates optional
aggregate expert labels for downstream semantic application; native QuPath label application
remains owned by its later stage."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from ._common import BatchPhenotypingError
from .marker_settings import validate_marker_setup_context, validate_marker_setup_response
from .authority import (
    clustering_result_identity_contract,
    clustering_result_owning_identity,
    unsupervised_preparation_identity_contract,
    unsupervised_preparation_owning_identity,
)
from .codec import (
    decode_committed_batch_clustering_result,
    dumps_batch_clustering_result,
    dumps_unsupervised_preparation,
    persist_batch_clustering_result,
    persist_unsupervised_preparation,
)
from .execution import execute_batch_clustering
from .preparation import build_unsupervised_preparation_execution_result
from .marker_execution import (
    AuthenticatedMarkerMeasurementPopulation,
    MarkerGateDomainAuthority,
    MarkerGateEvidence,
    build_marker_gate_evidence,
    build_marker_gate_execution_result,
    marker_gate_domain_identity_contract,
    open_authenticated_marker_measurement_population,
    resolve_marker_gate_domain_authority,
    validate_marker_gate_target_response,
)
from .marker_application_execution import (
    build_marker_rule_application_invocation,
    build_marker_rule_section_application,
    decode_committed_marker_gate,
)
from .marker_application_adapters import (
    AuthorizedMarkerRuleSectionApplication,
    MarkerRuleApplicationAuthorityResolver,
    build_marker_rule_application_execution_result,
    build_marker_rule_application_project_stage_codec,
    issue_authorized_marker_rule_section_application,
)
from .marker_application_records import (
    MarkerRuleSectionApplication,
    ValidatedMarkerGateProjection,
    marker_section_application_identity_contract,
)
from .supervised_records import (
    AcceptedSupervisedPredictionReview,
    StoredSupervisedNativePrediction,
    SupervisedNativePrediction,
    SupervisedNativePredictionStore,
    supervised_phenotype_result_identity_contract,
)
from .supervised_execution import (
    PreparedSupervisedSectionView,
    SupervisedSectionDeployment,
    build_supervised_section_deployment,
    prepare_supervised_section_view,
    publish_supervised_prediction_review,
    validate_supervised_prediction_target_response,
)
from .supervised_adapters import (
    AuthorizedSupervisedSectionDeployment,
    SupervisedSectionDeploymentAuthorityResolver,
    build_supervised_deployment_execution_result,
    build_supervised_phenotype_project_stage_codec,
    build_supervised_phenotype_stage_spec,
    issue_authorized_supervised_section_deployment,
)
from .semantic_records import (
    ValidatedPhenotypeApplicationProjection,
    normalize_committed_phenotype_application,
    validate_phenotype_application_projection,
)
from .semantic_branch import (
    ValidatedSemanticDomainBranch,
    bind_committed_semantic_domain_branch,
    validate_semantic_domain_branch,
)
from .semantic_completion import (
    build_no_secondary_completion_execution_result,
    no_secondary_completion_identity_contract,
)
from .semantic_applicable import (
    ValidatedFinalCountCapResult,
    ValidatedSemanticEvaluationResult,
    ValidatedSemanticEvidenceResult,
    ValidatedSemanticFinalizationResult,
    decode_committed_final_count_cap_result,
    decode_committed_semantic_evaluation_result,
    decode_committed_semantic_evidence_result,
    decode_committed_semantic_finalization_result,
    final_count_cap_result_identity_contract,
    publish_semantic_evidence_stage_owner_capability,
    publish_semantic_finalization_stage_owner_capability,
    semantic_evaluation_result_identity_contract,
    semantic_evidence_result_identity_contract,
    semantic_finalization_result_identity_contract,
)
from .semantic_adapters import (
    build_semantic_evidence_stage_codec,
    build_semantic_finalization_stage_codec,
    execute_final_count_cap_stage,
    execute_semantic_evaluation_stage,
    validate_final_count_cap_target_response,
    validate_semantic_evaluation_target_response,
)
from .semantic_raw_producer import (
    build_semantic_raw_inventory_project_stage_codec,
)
from .accepted_review import AcceptedClusterLabelReview
from .application_execution import (
    attest_committed_section_cluster_label_application,
    build_section_cluster_label_application,
    derive_section_cluster_label_application,
    prepare_section_cluster_label_application,
    reopen_cluster_label_section_population,
)
from .application_codec import (
    CLUSTER_LABEL_IMPORT_RESULT_KIND,
    build_cluster_label_import_execution_result,
    build_cluster_label_import_invocation,
    cluster_label_import_result_identity_contract,
    decode_committed_accepted_cluster_label_review,
    decode_committed_cluster_label_import_result,
    decode_committed_clustering_result_for_application,
    decode_committed_phenotype_hierarchy_attestation,
    decode_committed_semantic_raw_inventory,
    persist_phenotype_hierarchy_attestation,
)
from .application_adapters import (
    AuthorizedClusterLabelApplication,
    AuthorizedPhenotypeHierarchyAttestation,
    ClusterLabelApplicationAuthorityResolver,
    build_cluster_label_import_project_stage_codec,
    build_phenotype_hierarchy_attestation_execution_result,
    issue_authorized_cluster_label_application,
    issue_authorized_phenotype_hierarchy_attestation,
)
from .application_records import (
    ClusterLabelSectionApplication,
    ClusterLabelSectionPopulation,
    PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA,
    PhenotypeHierarchyAttestation,
    SemanticRawSnapshot,
    ValidatedClusterLabelImportProjection,
    ValidatedPhenotypeHierarchyAttestationProjection,
    ValidatedSemanticRawInventoryProjection,
    phenotype_hierarchy_attestation_identity_contract,
    semantic_raw_inventory_identity_contract,
)
from .review_authority import (
    AuthorizedClusterLabelReviewResponse,
    AuthorizedClusterLabelReviewResponseResolver,
    accept_cluster_label_review,
    accepted_cluster_label_review_identity_contract,
    accepted_cluster_label_review_owning_identity,
    cluster_review_evidence_identity_contract,
    cluster_review_evidence_owning_identity,
    validate_cluster_review_evidence_authority,
)
from .review_codec import (
    dumps_batch_cluster_review_evidence,
    loads_batch_cluster_review_evidence,
)
from .review_execution import build_batch_cluster_review_evidence
from .review_records import (
    BatchClusterReviewEntryCount,
    BatchClusterReviewEvidence,
    BatchClusterReviewMarkerProfile,
    BatchClusterReviewSectionCount,
    BatchClusterReviewSummary,
)
from .review_target import AggregateClusterReviewTarget
from .adapters import (
    PHENOTYPING_STAGE_NODE_KINDS,
    PhenotypingStageOwnerBinding,
    build_phenotyping_stage_owner_bindings,
    build_phenotyping_stage_specs,
)
from .handoff_execution import (
    AuthorizedPhenotypeHandoff,
    PhenotypeHandoffAuthorityResolver,
    build_phenotype_handoff_project_stage_codec,
    build_phenotype_handoff_stage_spec,
    spatial_cell_handoff_identity_contract,
)
from .export_execution import (
    build_final_non_spatial_export_stage_spec,
    execute_final_non_spatial_export_stage,
    final_non_spatial_export_result_identity_contract,
)
from .records import (
    BatchClusterAssignment,
    BatchClusteringEntryComposition,
    BatchClusteringExecutionResult,
    BatchClusteringLabelProjection,
    BatchClusteringRequest,
    PersistedBatchClusteringResult,
    PersistedUnsupervisedPreparation,
    UnsupervisedPreparationInput,
)
from .composition import (
    ClusterLabelReviewRawAdapter,
    PhenotypingCodecDependencies,
    PhenotypingRuntimeDependencies,
    build_cluster_label_review_runtime_binding,
    build_phenotyping_interaction_owner_bindings,
    build_phenotyping_project_stage_codecs,
    build_phenotyping_runtime_adapters,
)

__all__ = [
    "validate_marker_setup_context",
    "validate_marker_setup_response",
    "BatchClusterAssignment",
    "BatchClusteringEntryComposition",
    "BatchClusteringExecutionResult",
    "BatchClusteringLabelProjection",
    "BatchClusteringRequest",
    "BatchPhenotypingError",
    "PHENOTYPING_STAGE_NODE_KINDS",
    "PhenotypingStageOwnerBinding",
    "AuthorizedPhenotypeHandoff",
    "PhenotypeHandoffAuthorityResolver",
    "BatchClusterReviewEntryCount",
    "BatchClusterReviewEvidence",
    "BatchClusterReviewMarkerProfile",
    "BatchClusterReviewSectionCount",
    "BatchClusterReviewSummary",
    "AggregateClusterReviewTarget",
    "AuthorizedClusterLabelReviewResponse",
    "AuthorizedClusterLabelReviewResponseResolver",
    "AcceptedClusterLabelReview",
    "AuthorizedClusterLabelApplication",
    "AuthorizedPhenotypeHierarchyAttestation",
    "CLUSTER_LABEL_IMPORT_RESULT_KIND",
    "ClusterLabelApplicationAuthorityResolver",
    "ClusterLabelSectionApplication",
    "ClusterLabelSectionPopulation",
    "PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA",
    "PhenotypeHierarchyAttestation",
    "SemanticRawSnapshot",
    "ValidatedClusterLabelImportProjection",
    "ValidatedPhenotypeHierarchyAttestationProjection",
    "ValidatedSemanticRawInventoryProjection",
    "phenotype_hierarchy_attestation_identity_contract",
    "semantic_raw_inventory_identity_contract",
    "PersistedBatchClusteringResult",
    "PersistedUnsupervisedPreparation",
    "UnsupervisedPreparationInput",
    "AuthenticatedMarkerMeasurementPopulation",
    "AuthorizedMarkerRuleSectionApplication",
    "MarkerGateDomainAuthority",
    "MarkerGateEvidence",
    "MarkerRuleApplicationAuthorityResolver",
    "MarkerRuleSectionApplication",
    "AcceptedSupervisedPredictionReview",
    "StoredSupervisedNativePrediction",
    "SupervisedNativePrediction",
    "SupervisedNativePredictionStore",
    "PreparedSupervisedSectionView",
    "SupervisedSectionDeployment",
    "AuthorizedSupervisedSectionDeployment",
    "SupervisedSectionDeploymentAuthorityResolver",
    "ValidatedMarkerGateProjection",
    "ValidatedPhenotypeApplicationProjection",
    "ValidatedSemanticDomainBranch",
    "ValidatedFinalCountCapResult",
    "ValidatedSemanticEvaluationResult",
    "ValidatedSemanticEvidenceResult",
    "ValidatedSemanticFinalizationResult",
    "build_unsupervised_preparation_execution_result",
    "build_phenotype_handoff_stage_spec",
    "build_phenotype_handoff_project_stage_codec",
    "build_phenotyping_stage_specs",
    "build_phenotyping_stage_owner_bindings",
    "build_final_non_spatial_export_stage_spec",
    "execute_final_non_spatial_export_stage",
    "spatial_cell_handoff_identity_contract",
    "final_non_spatial_export_result_identity_contract",
    "build_marker_gate_evidence",
    "build_marker_gate_execution_result",
    "marker_gate_domain_identity_contract",
    "open_authenticated_marker_measurement_population",
    "resolve_marker_gate_domain_authority",
    "validate_marker_gate_target_response",
    "build_marker_rule_application_invocation",
    "build_marker_rule_application_execution_result",
    "build_marker_rule_application_project_stage_codec",
    "issue_authorized_marker_rule_section_application",
    "build_marker_rule_section_application",
    "decode_committed_marker_gate",
    "marker_section_application_identity_contract",
    "supervised_phenotype_result_identity_contract",
    "build_supervised_section_deployment",
    "prepare_supervised_section_view",
    "publish_supervised_prediction_review",
    "validate_supervised_prediction_target_response",
    "build_supervised_deployment_execution_result",
    "build_supervised_phenotype_project_stage_codec",
    "build_supervised_phenotype_stage_spec",
    "issue_authorized_supervised_section_deployment",
    "normalize_committed_phenotype_application",
    "validate_phenotype_application_projection",
    "bind_committed_semantic_domain_branch",
    "validate_semantic_domain_branch",
    "build_no_secondary_completion_execution_result",
    "no_secondary_completion_identity_contract",
    "build_semantic_raw_inventory_project_stage_codec",
    "build_semantic_evidence_stage_codec",
    "build_semantic_finalization_stage_codec",
    "execute_final_count_cap_stage",
    "execute_semantic_evaluation_stage",
    "validate_final_count_cap_target_response",
    "validate_semantic_evaluation_target_response",
    "decode_committed_final_count_cap_result",
    "decode_committed_semantic_evaluation_result",
    "decode_committed_semantic_evidence_result",
    "decode_committed_semantic_finalization_result",
    "final_count_cap_result_identity_contract",
    "semantic_evaluation_result_identity_contract",
    "semantic_evidence_result_identity_contract",
    "semantic_finalization_result_identity_contract",
    "publish_semantic_evidence_stage_owner_capability",
    "publish_semantic_finalization_stage_owner_capability",
    "build_batch_cluster_review_evidence",
    "clustering_result_identity_contract",
    "clustering_result_owning_identity",
    "dumps_batch_clustering_result",
    "dumps_unsupervised_preparation",
    "dumps_batch_cluster_review_evidence",
    "loads_batch_cluster_review_evidence",
    "decode_committed_batch_clustering_result",
    "execute_batch_clustering",
    "persist_batch_clustering_result",
    "unsupervised_preparation_identity_contract",
    "persist_unsupervised_preparation",
    "unsupervised_preparation_owning_identity",
    "accept_cluster_label_review",
    "accepted_cluster_label_review_identity_contract",
    "accepted_cluster_label_review_owning_identity",
    "cluster_review_evidence_identity_contract",
    "cluster_review_evidence_owning_identity",
    "validate_cluster_review_evidence_authority",
    "build_cluster_label_import_execution_result",
    "build_cluster_label_import_invocation",
    "build_cluster_label_import_project_stage_codec",
    "cluster_label_import_result_identity_contract",
    "decode_committed_accepted_cluster_label_review",
    "decode_committed_cluster_label_import_result",
    "decode_committed_clustering_result_for_application",
    "decode_committed_phenotype_hierarchy_attestation",
    "decode_committed_semantic_raw_inventory",
    "persist_phenotype_hierarchy_attestation",
    "issue_authorized_cluster_label_application",
    "issue_authorized_phenotype_hierarchy_attestation",
    "build_phenotype_hierarchy_attestation_execution_result",
    "attest_committed_section_cluster_label_application",
    "build_section_cluster_label_application",
    "derive_section_cluster_label_application",
    "prepare_section_cluster_label_application",
    "reopen_cluster_label_section_population",
    "ClusterLabelReviewRawAdapter",
    "PhenotypingCodecDependencies",
    "PhenotypingRuntimeDependencies",
    "build_cluster_label_review_runtime_binding",
    "build_phenotyping_interaction_owner_bindings",
    "build_phenotyping_project_stage_codecs",
    "build_phenotyping_runtime_adapters",
]
