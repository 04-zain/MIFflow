"""Authenticated records for the applicable four-node semantic batch chain.

The `ValidatedSemanticEvaluationResult` record carries `artifact_ref`, `state_head_sha256`,
`active_plan_sha256`, `terminal_record_sha256`, `result_rows` and its remaining declared
fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.pipeline_core.domain import AnalysisDomainRef
from mif_pipeline.cell_analysis import (
    FinalizedSecondaryDomainEvidence,
    FinalObjectDispositionEvidence,
    ManualSelectionEvidence,
    SemanticResultArtifactEvidence,
)
from mif_pipeline.compartment_analysis import ManualSelectionLineage, SelectionContext
from mif_pipeline.orchestration.batch_analysis_preflight import (
    validate_analysis_frame_artifact_ref,
)
from mif_pipeline.orchestration.accepted_analysis_domain_records import (
    decode_committed_accepted_analysis_domain,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    decode_committed_measurements_result,
)
from mif_pipeline.orchestration.batch_phenotyping_contracts import (
    BatchROISemanticProfileBinding,
)
from mif_pipeline.orchestration.semantic_execution import (
    authenticate_roi_semantic_result_artifact,
    prepare_roi_semantic_evidence_request,
    prepare_roi_semantic_finalization_request,
    resolve_roi_semantic_count,
    validate_roi_semantic_evidence_response,
    validate_roi_semantic_raw_inventory,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.roi_validation import (
    SemanticEvidenceSnapshot,
    SemanticRegionDecision,
    load_semantic_profile_v3,
)

from ..batch_secondary_definitions import resolve_secondary_structures

from ..project_stage_owner_capabilities import (
    ProjectStageOwnerCapabilityPublisher,
    SemanticEvidenceStageOwnerCapability,
    SemanticFinalizationStageOwnerCapability,
    _issue_applicable_capability,
)
from ._common import BatchPhenotypingError, digest, plain_json, read_bounded_file, text
from .application_codec import (
    decode_committed_phenotype_hierarchy_attestation,
    decode_committed_semantic_raw_inventory,
)
from .semantic_branch import ValidatedSemanticDomainBranch, validate_semantic_domain_branch

_EVIDENCE_FIELDS = (
    "schema_version",
    "sample_id",
    "slide_id",
    "project_entry_id",
    "analysis_domain_id",
    "analysis_domain_revision_sha256",
    "domain_set_sha256",
    "section_id",
    "tissue_generation_id",
    "pipeline_run_id",
    "analysis_config_sha256",
    "raw_inventory_ref_sha256",
    "analysis_frame_ref_sha256",
    "accepted_domain_ref_sha256",
    "measurements_ref_sha256",
    "semantic_profile_ref_sha256s",
    "phenotype_source_ref_sha256",
    "phenotype_view_attestation_ref_sha256",
    "segmentation_population_id",
    "physical_calibration.x_um_per_px",
    "physical_calibration.y_um_per_px",
    "phenotype_view.mode",
    "phenotype_view.generation_id",
    "phenotype_view.inventory_sha256",
    "membership_artifact.path",
    "membership_artifact.sha256",
    "membership_artifact.byte_count",
    "evidence_snapshot_sha256s",
    "evidence_count",
    "saved_state_before_sha256",
    "saved_state_after_sha256",
    "execution_exchange_sha256",
)
_EVALUATION_FIELDS = (
    "schema_version",
    "sample_id",
    "slide_id",
    "project_entry_id",
    "analysis_domain_id",
    "analysis_domain_revision_sha256",
    "domain_set_sha256",
    "section_id",
    "tissue_generation_id",
    "pipeline_run_id",
    "analysis_config_sha256",
    "semantic_config_sha256",
    "semantic_evidence_ref_sha256",
    "raw_inventory_ref_sha256",
    "semantic_profile_ref_sha256s",
    "semantic_result_artifact.schema_version",
    "semantic_result_artifact.path",
    "semantic_result_artifact.sha256",
    "semantic_result_artifact.record_count",
    "semantic_result_artifact.semantic_result_set_sha256",
    "semantic_result_set_sha256",
    "semantic_qc.status",
    "semantic_qc.result_count",
    "semantic_qc.evaluated_roi_ids",
    "semantic_qc.semantic_config_sha256",
    "review_status",
    "review_action",
)
_SEMANTIC_DECISION_LEAVES = (
    "schema_version",
    "slide_id",
    "section_id",
    "analysis_domain_id",
    "candidate_roi_id",
    "profile_id",
    "profile_version",
    "profile_content_sha256",
    "criterion_id",
    "subtype_status",
    "semantic_class_id",
    "geometry_digest",
    "evidence_digest",
    "checkpoint_id",
    "reviewer_id",
    "action",
    "rationale",
)
_EVALUATION_OPTIONAL_FIELDS = (
    "semantic_region_decisions",
    *(f"semantic_region_decisions.{field}" for field in _SEMANTIC_DECISION_LEAVES),
)
_DOMAIN_REF_LEAVES = (
    "analysis_domain_id",
    "kind",
    "slide_id",
    "section_id",
    "tissue_generation_id",
)
_MANUAL_LINEAGE_LEAVES = (
    "analysis_domain_sha256",
    "config_sha256",
    "result_sha256",
    "inventory_sha256",
    "structure_id",
    "algorithm",
    "selected_content_keys",
    "selection_criterion",
    "reviewer_id",
    "rationale",
)
_DETERMINISTIC_LINEAGE_LEAVES = (
    "analysis_domain_sha256",
    "config_sha256",
    "result_sha256",
    "inventory_sha256",
    "structure_id",
    "algorithm",
    "sampling_seed",
)
_LINEAGE_LEAVES = tuple(sorted(set(_MANUAL_LINEAGE_LEAVES) | set(_DETERMINISTIC_LINEAGE_LEAVES)))
_INVENTORY_LEAVES = (
    "content_key",
    "structure_id",
    "parent_content_key",
    "geometry_sha256",
    "disposition",
    "observed_count",
    "inclusion_fraction",
    "inventory_sha256",
    "inclusion_probability_status",
    "inclusion_probability",
)
_OUTPUT_LEAVES = (
    "output_id",
    "status",
    "observed_count",
    "minimum",
    "maximum",
    "inventory_sha256",
    "included_content_keys",
    "not_selected_content_keys",
    "inclusion_fraction",
    "inclusion_probability_status",
    "inclusion_probability",
)
_DISPOSITION_LEAVES = (
    "schema_version",
    "content_key",
    "roi_id",
    "path_object_id",
    "structure_id",
    "parent_content_key",
    "geometry_sha256",
    "disposition",
    "selection_lineage_sha256",
    "disposition_sha256",
)
_MANUAL_EVIDENCE_LEAVES = (
    "schema_version",
    "structure_id",
    "inventory_descriptor.artifact_id",
    "inventory_descriptor.schema_version",
    "inventory_descriptor.path",
    "inventory_descriptor.sha256",
    "inventory_descriptor.record_count",
    *(f"selection_lineage.{field}" for field in _MANUAL_LINEAGE_LEAVES),
    "lineage_sha256",
)
_COUNT_DECISION_REQUIRED_LEAVES = (
    "schema_version",
    "decision_sha256",
    "component_selection_action",
    *(f"domain.{field}" for field in _DOMAIN_REF_LEAVES),
    *(f"outputs.{field}" for field in _OUTPUT_LEAVES),
)
_COUNT_DECISION_OPTIONAL_LEAVES = (
    "domain_exclusion",
    *(f"domain_exclusion.domain.{field}" for field in _DOMAIN_REF_LEAVES),
    "domain_exclusion.status",
    "domain_exclusion.failing_output_ids",
    "domain_exclusion.reason",
    "outputs.inventory",
    *(f"outputs.inventory.{field}" for field in _INVENTORY_LEAVES),
    "outputs.inventory.selection_lineage",
    *(f"outputs.inventory.selection_lineage.{field}" for field in _LINEAGE_LEAVES),
    "outputs.selection_lineage",
    *(f"outputs.selection_lineage.{field}" for field in _LINEAGE_LEAVES),
)
_COUNT_FIELDS = (
    "schema_version",
    "sample_id",
    "slide_id",
    "project_entry_id",
    "analysis_domain_id",
    "analysis_domain_revision_sha256",
    "domain_set_sha256",
    "section_id",
    "tissue_generation_id",
    "pipeline_run_id",
    "analysis_config_sha256",
    "semantic_evaluation_ref_sha256",
    "raw_inventory_ref_sha256",
    "accepted_domain_ref_sha256",
    *(f"count_decision.{field}" for field in _COUNT_DECISION_REQUIRED_LEAVES),
    "active_secondary_roi_ids",
    "domain_status",
)
_COUNT_OPTIONAL_FIELDS = (
    *(f"count_decision.{field}" for field in _COUNT_DECISION_OPTIONAL_LEAVES),
    "final_object_dispositions",
    *(f"final_object_dispositions.{field}" for field in _DISPOSITION_LEAVES),
    "manual_selection_lineages",
    *(f"manual_selection_lineages.{field}" for field in _MANUAL_LINEAGE_LEAVES),
    "manual_selection_evidence",
    *(f"manual_selection_evidence.{field}" for field in _MANUAL_EVIDENCE_LEAVES),
)
_FINALIZATION_RECEIPT_LEAVES = (
    "schema_version",
    "analysis_domain_id",
    "section_id",
    "tissue_generation_id",
    "pipeline_run_id",
    "semantic_result_set_sha256",
    "semantic_result_artifact.schema_version",
    "semantic_result_artifact.path",
    "semantic_result_artifact.sha256",
    "semantic_result_artifact.record_count",
    "semantic_result_artifact.semantic_result_set_sha256",
    "active_secondary_roi_ids",
    "accepted_secondary_roi_ids",
    "accepted_secondary_count",
    "reconciled_cell_ids_sha256",
    "membership_cell_count",
)
_FINALIZATION_FIELDS = (
    "schema_version",
    "kind",
    "stage_status",
    "applicability_status",
    "analysis_domain_id",
    "analysis_domain_revision_sha256",
    "domain_set_sha256",
    "spatial_frame_sha256",
    "semantic_result_artifact.schema_version",
    "semantic_result_artifact.path",
    "semantic_result_artifact.sha256",
    "semantic_result_artifact.record_count",
    "semantic_result_artifact.semantic_result_set_sha256",
    *(f"count_decision.{field}" for field in _COUNT_DECISION_REQUIRED_LEAVES),
    "decision_sha256",
    *(f"finalization_receipt.{field}" for field in _FINALIZATION_RECEIPT_LEAVES),
    "finalization_sha256",
    "domain_status",
)
_FINALIZATION_OPTIONAL_FIELDS = (
    *(f"count_decision.{field}" for field in _COUNT_DECISION_OPTIONAL_LEAVES),
    "dispositions",
    *(f"dispositions.{field}" for field in _DISPOSITION_LEAVES),
    "manual_selections",
    *(f"manual_selections.{field}" for field in _MANUAL_EVIDENCE_LEAVES),
)


# ==============================================================================
#                              Semantic applicable
# ==============================================================================


def semantic_evidence_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-roi-semantic-evidence-reference-identity/v1",
        artifact_kind="semantic_evidence_result",
        schema_name="mif-batch-roi-semantic-evidence-reference",
        schema_version=1,
        required_fields=_EVIDENCE_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def semantic_evaluation_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-roi-semantic-result-reference-identity/v1",
        artifact_kind="semantic_evaluation_result",
        schema_name="mif-batch-roi-semantic-result-reference",
        schema_version=1,
        required_fields=_EVALUATION_FIELDS,
        optional_fields=_EVALUATION_OPTIONAL_FIELDS,
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def final_count_cap_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-structure-count-decision-identity/v1",
        artifact_kind="final_count_cap_result",
        schema_name="structure-count-decision",
        schema_version=1,
        required_fields=_COUNT_FIELDS,
        optional_fields=_COUNT_OPTIONAL_FIELDS,
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def semantic_finalization_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-domain-finalization-evidence-identity/v1",
        artifact_kind="semantic_finalization_result",
        schema_name="domain-finalization-evidence",
        schema_version=1,
        required_fields=_FINALIZATION_FIELDS,
        optional_fields=_FINALIZATION_OPTIONAL_FIELDS,
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _committed(
    value: object, *, kind: str, producer: str, contract: ArtifactIdentityContract
) -> BatchCommittedArtifactAuthority:
    if type(value) is not BatchCommittedArtifactAuthority:
        raise TypeError(f"{kind} requires committed state authority")
    (authority,) = validate_committed_artifact_authority_set((value,))
    ref = validate_artifact_ref(authority.artifact_ref)
    if (
        ref.artifact_kind != kind
        or authority.producer_node_kind != producer
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
    ):
        raise BatchPhenotypingError(f"{kind} committed owner differs")
    return authority


def _sample_config(sample: object) -> ResolvedBatchSample:
    if type(sample) is not ResolvedBatchSample:
        raise TypeError("semantic composition requires one resolved sample")
    if digest(sample.config.to_mapping()) != sample.resolved_config_sha256:
        raise BatchPhenotypingError("resolved semantic configuration digest differs")
    return sample


def _single_domain(accepted: object, binding: object):
    matches = tuple(
        item
        for item in accepted.domain_authorities
        if item.analysis_domain_id == binding.analysis_domain_id
        and item.analysis_domain_revision == binding.analysis_domain_revision
        and item.analysis_domain_revision_sha256 == binding.analysis_domain_revision_sha256
    )
    if len(matches) != 1:
        raise BatchPhenotypingError("semantic composition domain is ambiguous")
    return matches[0]


def _profile_owner(authorities: object, sample: ResolvedBatchSample, secondary_selection=None) -> tuple[
    tuple[BatchCommittedArtifactAuthority, ...],
    tuple[BatchROISemanticProfileBinding, ...],
    tuple[bytes, ...],
]:
    if isinstance(authorities, (str, bytes)) or not isinstance(authorities, Sequence):
        raise TypeError("semantic profiles require an exact committed authority set")
    values = tuple(authorities)
    if not values or not all(type(item) is BatchCommittedArtifactAuthority for item in values):
        raise TypeError("semantic profiles require committed state authorities")
    validated = validate_committed_artifact_authority_set(values)
    structures = {item.structure_id: item for item in resolve_secondary_structures(sample, secondary_selection)}
    references = {(item.semantic_profile.profile_id, item.semantic_profile.profile_version): item.semantic_profile
                  for item in structures.values()}
    if secondary_selection is not None and any(
            item.state_head_sha256 != secondary_selection.state_head_sha256
            or item.active_plan_sha256 != secondary_selection.active_plan_sha256 for item in validated):
        raise BatchPhenotypingError("Semantic profiles and structure definitions belong to different revisions")
    validation_domains = {item.classifier.validation_domain_id for item in structures.values()}
    if (
        not structures
        or len(references) != len(structures)
        or len(validation_domains) != 1
        or None in validation_domains
    ):
        raise BatchPhenotypingError("semantic profile configuration is not exact")
    by_structure: dict[
        str,
        tuple[
            BatchCommittedArtifactAuthority,
            BatchROISemanticProfileBinding,
            bytes,
        ],
    ] = {}
    seen_refs: set[str] = set()
    for authority in validated:
        ref = validate_artifact_ref(authority.artifact_ref)
        if (
            ref.artifact_kind != "semantic_profile"
            or ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL
            or ref.locator is None
            or ref.sha256 is None
            or ref.byte_count is None
            or ref.artifact_ref_sha256 in seen_refs
        ):
            raise BatchPhenotypingError("semantic profile authority set differs")
        content = read_bounded_file(
            ref.locator,
            expected_byte_count=ref.byte_count,
            maximum_byte_count=16 * 1024 * 1024,
        )
        try:
            profile = load_semantic_profile_v3(Path(ref.locator))
        except (OSError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError("semantic profile bytes are invalid") from exc
        reference = references.get((profile.profile_id, profile.profile_version))
        structure = structures.get(profile.structure_id)
        configured_path = None if reference is None else reference.artifact_path
        if configured_path is not None and not configured_path.is_absolute():
            configured_path = sample.config.output_dir / configured_path
        if (
            reference is None
            or structure is None
            or structure.semantic_profile != reference
            or configured_path is None
            or configured_path.resolve(strict=False) != Path(ref.locator).resolve(strict=False)
            or hashlib.sha256(content).hexdigest() != ref.sha256
            or reference.content_sha256 != ref.sha256
            or profile.structure_id in by_structure
        ):
            raise BatchPhenotypingError("semantic profile authority differs from config")
        seen_refs.add(ref.artifact_ref_sha256)
        by_structure[profile.structure_id] = (
            authority,
            BatchROISemanticProfileBinding.create(
                validation_domain_id=next(iter(validation_domains)),
                profile_locator="batch/" + Path(ref.locator).name,
                profile_bytes=content,
                profile_schema_version=int(profile.schema_version.rsplit("v", 1)[1]),
            ),
            content,
        )
    if set(by_structure) != set(structures):
        raise BatchPhenotypingError("semantic profile structure coverage differs")
    ordered = tuple(by_structure[key] for key in sorted(by_structure))
    return (
        tuple(item[0] for item in ordered),
        tuple(item[1] for item in ordered),
        tuple(item[2] for item in ordered),
    )


def _raw_inventory(raw: object, *, pipeline_run_id: str) -> Mapping[str, object]:
    ref = raw.artifact_ref
    return validate_roi_semantic_raw_inventory(
        Path(str(ref.locator)),
        raw_export_sha256=raw.snapshot.raw_export_sha256,
        slide_id=raw.snapshot.slide_id,
        section_id=raw.snapshot.section_id,
        analysis_domain_id=ref.scope.analysis_domain_bindings[0].analysis_domain_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
    )


def _phenotype_view(branch: ValidatedSemanticDomainBranch) -> dict[str, object]:
    phenotype = branch.phenotype_application
    return {
        "mode": phenotype.phenotype_mode,
        "generation_id": phenotype.phenotype_generation_id,
        "inventory_sha256": phenotype.phenotype_view_inventory_sha256,
    }


def _validated_evidence_attestation(
    *,
    semantic_branch: ValidatedSemanticDomainBranch,
    raw_inventory: BatchCommittedArtifactAuthority,
    phenotype_view_attestation: BatchCommittedArtifactAuthority | None,
) -> BatchCommittedArtifactAuthority | None:
    """Bind the existing cluster hierarchy attestation without changing S1/S2."""

    branch = validate_semantic_domain_branch(semantic_branch)
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    phenotype = branch.phenotype_application
    if phenotype.source_artifact_kind in {
        "marker_phenotype_result",
        "supervised_phenotype_result",
    }:
        if phenotype_view_attestation is not None:
            raise BatchArtifactError(
                f"{phenotype.source_artifact_kind} semantic evidence forbids "
                "a hierarchy attestation"
            )
        return None
    if phenotype.source_artifact_kind != "cluster_label_import_result":
        raise BatchArtifactError("semantic evidence phenotype source is unsupported")
    if type(phenotype_view_attestation) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError(
            "cluster semantic evidence requires its exact hierarchy attestation"
        )
    try:
        projection = decode_committed_phenotype_hierarchy_attestation(phenotype_view_attestation)
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "cluster semantic evidence hierarchy attestation is invalid"
        ) from exc
    attestation = projection.attestation
    source = phenotype.source_artifact_ref
    if (
        projection.state_head_sha256 != branch.state_head_sha256
        or projection.active_plan_sha256 != branch.active_plan_sha256
        or raw.state_head_sha256 != branch.state_head_sha256
        or raw.active_plan_sha256 != branch.active_plan_sha256
        or projection.artifact_ref.scope != source.scope
        or projection.artifact_ref.scope != raw.artifact_ref.scope
        or projection.artifact_ref.configuration_bindings != source.configuration_bindings
        or projection.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
        or attestation.cluster_label_import_result_ref_sha256 != source.artifact_ref_sha256
        or attestation.raw_export_ref_sha256 != raw.artifact_ref.artifact_ref_sha256
        or attestation.raw_export_sha256 != raw.snapshot.raw_export_sha256
        or attestation.sample_id != phenotype.sample_id
        or attestation.project_entry_id != phenotype.project_entry_id
        or attestation.slide_id != phenotype.slide_id
        or attestation.section_id != phenotype.section_id
        or attestation.canonical_image_sha256
        != source.owning_identity.get("canonical_image_sha256")
        or attestation.tissue_generation_id != phenotype.tissue_generation_id
        or attestation.segmentation_population_id != raw.snapshot.segmentation_population_id
        or attestation.phenotype_generation_id != phenotype.phenotype_generation_id
        or attestation.cell_ids_sha256 != phenotype.cell_ids_sha256
        or attestation.cell_to_domain_membership_sha256
        != phenotype.cell_to_domain_membership_sha256
        or attestation.phenotype_view_artifact_sha256 != phenotype.phenotype_view_artifact_sha256
        or attestation.phenotype_view_lineage_sha256 != phenotype.phenotype_view_lineage_sha256
        or attestation.phenotype_view_inventory_sha256 != phenotype.phenotype_view_inventory_sha256
        or attestation.saved_state_after_sha256 != phenotype.saved_state_after_sha256
        or attestation.saved_state_after_sha256 != raw.snapshot.saved_state_after_sha256
    ):
        raise BatchArtifactError("cluster semantic evidence hierarchy attestation differs")
    return phenotype_view_attestation


def _common_join(
    *,
    raw_inventory: BatchCommittedArtifactAuthority,
    analysis_frame: BatchCommittedArtifactAuthority,
    accepted_domain: BatchCommittedArtifactAuthority,
    measurements: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
    semantic_branch: ValidatedSemanticDomainBranch,
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> dict[str, object]:
    sample = _sample_config(resolved_sample)
    branch = validate_semantic_domain_branch(semantic_branch)
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    if raw is not branch.raw_inventory:
        # Decoders intentionally issue fresh projections; compare all retained authority.
        if (
            raw.artifact_ref != branch.raw_inventory.artifact_ref
            or raw.state_head_sha256 != branch.state_head_sha256
            or raw.active_plan_sha256 != branch.active_plan_sha256
        ):
            raise BatchPhenotypingError("semantic branch raw owner differs")
    if branch.branch_kind != "applicable":
        raise BatchPhenotypingError("semantic composition requires the applicable branch")
    frame_authority = analysis_frame
    frame = validate_analysis_frame_artifact_ref(frame_authority.artifact_ref)
    domain = decode_committed_accepted_analysis_domain(accepted_domain)
    measurement = decode_committed_measurements_result(measurements)
    phenotype = branch.phenotype_application
    profile_authorities, profile_bindings, profile_bytes = _profile_owner(semantic_profiles, sample, secondary_selection)
    authorities = (
        raw_inventory,
        analysis_frame,
        accepted_domain,
        measurements,
        *profile_authorities,
    )
    if any(
        item.state_head_sha256 != raw.state_head_sha256
        or item.active_plan_sha256 != raw.active_plan_sha256
        for item in authorities
    ):
        raise BatchPhenotypingError("semantic committed owners use foreign state or plan")
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    member = _single_domain(domain, binding)
    run_id = text(
        measurement.artifact_ref.owning_identity.get("pipeline_run_id"),
        "semantic pipeline_run_id",
    )
    if phenotype.source_artifact_kind == "supervised_phenotype_result" and (
        measurement.artifact_ref.artifact_ref_sha256
        not in tuple(phenotype.source_artifact_ref.owning_identity.get("source_ref_sha256s", ()))
        or phenotype.source_artifact_ref.scope.validation_domain_id
        != measurement.artifact_ref.scope.validation_domain_id
        or phenotype.source_artifact_ref.scope.validation_domain_id
        != raw.artifact_ref.scope.validation_domain_id
        or phenotype.source_artifact_ref.scope.validation_domain_id != sample.validation_domain_id
    ):
        raise BatchPhenotypingError(
            "semantic evidence validation domain or measurement lineage differs"
        )
    if (
        binding.sample_id != sample.sample_id
        or binding.slide_id != sample.slide_id
        or binding not in domain.artifact_ref.scope.analysis_domain_bindings
        or measurement.analysis_domain_bindings
        != domain.artifact_ref.scope.analysis_domain_bindings
        or phenotype.analysis_domain_bindings != domain.artifact_ref.scope.analysis_domain_bindings
        or member.section_id != raw.snapshot.section_id
        or member.tissue_generation_id != raw.snapshot.tissue_generation_id
        or measurement.section_id != member.section_id
        or measurement.tissue_generation_id != member.tissue_generation_id
        or measurement.segmentation_population_id != raw.snapshot.segmentation_population_id
        or frame.spatial_frame.canonical_image_sha256 != domain.canonical_image_sha256
        or measurement.canonical_image_sha256 != domain.canonical_image_sha256
        or raw.snapshot.canonical_image_sha256 != domain.canonical_image_sha256
        or raw.artifact_ref.configuration_bindings
        != measurement.artifact_ref.configuration_bindings
        or any(
            item.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
            or item.artifact_ref.scope.validation_domain_id
            != raw.artifact_ref.scope.validation_domain_id
            or item.artifact_ref.scope.sample_id not in {None, binding.sample_id}
            or item.artifact_ref.scope.slide_id not in {None, binding.slide_id}
            for item in profile_authorities
        )
    ):
        raise BatchPhenotypingError("semantic committed owner lineage differs")
    rows = _raw_inventory(raw, pipeline_run_id=run_id)
    if (
        tuple(sorted(str(item["roi_id"]) for item in rows["secondary"]))
        != raw.candidate_secondary_roi_ids
        or tuple(sorted(item.structure_id for item in resolve_secondary_structures(sample, secondary_selection)))
        != raw.configured_secondary_ids
    ):
        raise BatchPhenotypingError("semantic raw/config candidate inventory differs")
    return {
        "sample": sample,
        "branch": branch,
        "raw": raw,
        "frame": frame,
        "domain": domain,
        "member": member,
        "measurements": measurement,
        "profile_authorities": profile_authorities,
        "profile_bindings": profile_bindings,
        "profile_bytes": profile_bytes,
        "raw_inventory": rows,
        "binding": binding,
        "pipeline_run_id": run_id,
    }


def _exact_context_inputs(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    expected: Sequence[BatchArtifactRef],
) -> None:
    actual = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    wanted = tuple(sorted(item.artifact_ref_sha256 for item in expected))
    if (
        actual != wanted
        or current.input_artifact_ref_sha256s != wanted
        or len(actual) != len(set(actual))
    ):
        raise BatchPhenotypingError("semantic stage exact input owner set differs")


def publish_semantic_evidence_stage_owner_capability(
    publisher: ProjectStageOwnerCapabilityPublisher,
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
    cell_selector_inventory=None,
    semantic_branch: ValidatedSemanticDomainBranch,
    raw_inventory: BatchCommittedArtifactAuthority,
    phenotype_view_attestation: BatchCommittedArtifactAuthority | None,
    analysis_frame: BatchCommittedArtifactAuthority,
    accepted_domain: BatchCommittedArtifactAuthority,
    measurements: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
) -> SemanticEvidenceStageOwnerCapability:
    """Publish the exact live owners required by the evidence codec."""

    if type(publisher) is not ProjectStageOwnerCapabilityPublisher:
        raise TypeError("semantic evidence requires the capability publisher")
    validated_current = validate_current_execution_authority(current, expected_context=context)
    if validated_current.node_kind != "semantic_evidence":
        raise BatchPhenotypingError("semantic evidence current node differs")
    joined = _common_join(
        raw_inventory=raw_inventory,
        analysis_frame=analysis_frame,
        accepted_domain=accepted_domain,
        measurements=measurements,
        semantic_profiles=semantic_profiles,
        semantic_branch=semantic_branch,
        resolved_sample=resolved_sample,
        secondary_selection=secondary_selection,
    )
    raw = joined["raw"]
    binding = joined["binding"]
    branch = joined["branch"]
    if (
        branch.phenotype_application.source_artifact_kind == "supervised_phenotype_result"
        and validated_current.validation_domain_id != resolved_sample.validation_domain_id
    ):
        raise BatchPhenotypingError("semantic evidence validation domain differs")
    attestation_owner = _validated_evidence_attestation(
        semantic_branch=branch,
        raw_inventory=raw_inventory,
        phenotype_view_attestation=phenotype_view_attestation,
    )
    source_ref = branch.phenotype_application.source_artifact_ref
    by_ref = {item.artifact_ref_sha256: item for item in context.inputs}
    source_refs = [source_ref]
    if source_ref.artifact_kind == "cluster_label_import_result":
        assert attestation_owner is not None
        source_refs.append(attestation_owner.artifact_ref)
    elif source_ref.artifact_kind not in {
        "marker_phenotype_result",
        "supervised_phenotype_result",
    }:
        raise BatchPhenotypingError("semantic evidence phenotype source is unsupported")
    exact = [
        raw.artifact_ref,
        analysis_frame.artifact_ref,
        accepted_domain.artifact_ref,
        measurements.artifact_ref,
        *(item.artifact_ref for item in joined["profile_authorities"]),
        *source_refs,
    ]
    if any(item.artifact_ref_sha256 not in by_ref for item in exact):
        raise BatchPhenotypingError("semantic evidence input owner is unavailable")
    _exact_context_inputs(context, validated_current, exact)
    frame = joined["frame"]
    from mif_pipeline.pipeline_core.config import AnalysisConfig
    from mif_pipeline.roi_validation import SemanticProfileV3
    from ..analysis_services import validate_semantic_profile_selectors
    import yaml

    if cell_selector_inventory is None:
        raise BatchPhenotypingError("The saved cell measurements and phenotype definitions are unavailable for region review. Reopen the completed cell results, then return to this review.")
    selector_config = (resolved_sample.config if secondary_selection is None else
                       AnalysisConfig.from_mapping(secondary_selection.context["validation_config"]))
    selector_structures = (resolved_sample.config.secondary_structures if secondary_selection is None
                           else secondary_selection.structures)
    for content in joined["profile_bytes"]:
        profile = SemanticProfileV3.from_mapping(yaml.safe_load(content.decode("utf-8")))
        validate_semantic_profile_selectors(
            profile, selector_config, secondary_structures=selector_structures,
            cell_selector_inventory=cell_selector_inventory,
        )
    request = prepare_roi_semantic_evidence_request(
        raw_inventory=joined["raw_inventory"],
        raw_export_sha256=raw.snapshot.raw_export_sha256,
        profile_bindings=joined["profile_bindings"],
        profile_bytes=joined["profile_bytes"],
        expected_validation_domain_id=validated_current.validation_domain_id,
        physical_calibration={
            "x_um_per_px": frame.spatial_frame.x_um_per_px,
            "y_um_per_px": frame.spatial_frame.y_um_per_px,
        },
        phenotype_view=_phenotype_view(branch),
    )
    # The runtime envelope supplies the authenticated top-level image hash.
    request = MappingProxyType(
        {
            **{key: value for key, value in raw.semantic_live_binding.items()
               if key != "canonical_image_sha256"},
            **request,
            "schema_version": "roi-workflow-invocation/v1",
            "stage": "semantic_validation",
            "sample_id": binding.sample_id,
            "slide_id": binding.slide_id,
            "raw_export_path": str(raw.artifact_ref.locator),
            "semantic_evidence_output_dir": str(
                resolved_sample.config.output_dir / "semantic-evidence"
            ),
        }
    )
    value = _issue_applicable_capability(
        SemanticEvidenceStageOwnerCapability,
        current=validated_current,
        resolved_sample=resolved_sample,
        semantic_branch=branch,
        phenotype_view_attestation=attestation_owner,
        analysis_frame=analysis_frame,
        accepted_domain=accepted_domain,
        measurements=measurements,
        semantic_profile_authorities=joined["profile_authorities"],
        profile_bindings=joined["profile_bindings"],
        profile_bytes=joined["profile_bytes"],
        raw_inventory=joined["raw_inventory"],
        request_body=request,
    )
    assert isinstance(value, SemanticEvidenceStageOwnerCapability)
    publisher._publish_current(value, context=context, current=validated_current)
    return value


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticEvidenceResult:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    snapshots: tuple[SemanticEvidenceSnapshot, ...]
    membership_artifact_path: Path
    membership_artifact_bytes: bytes
    pipeline_run_id: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic evidence result is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticEvaluationResult:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    result_rows: tuple[Mapping[str, object], ...]
    result_artifact: SemanticResultArtifactEvidence
    result_artifact_bytes: bytes
    result_set_sha256: str
    semantic_qc: Mapping[str, object]
    review_status: str
    review_action: str | None
    decisions: tuple[SemanticRegionDecision, ...]
    pipeline_run_id: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic evaluation result is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedFinalCountCapResult:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    count_decision: object
    count_decision_mapping: Mapping[str, object]
    dispositions: tuple[FinalObjectDispositionEvidence, ...]
    active_secondary_roi_ids: tuple[str, ...]
    manual_lineages: Mapping[str, ManualSelectionLineage]
    manual_evidence: tuple[ManualSelectionEvidence, ...]
    domain_status: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("final count/cap result is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticFinalizationResult:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    evidence: FinalizedSecondaryDomainEvidence
    component_descriptor: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic finalization result is decoder-only")


def _projection(cls: type, **values: object):
    result = object.__new__(cls)
    for name in cls.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _membership_descriptor(path: Path, content: bytes) -> dict[str, object]:
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_count": len(content),
    }


def _same_canonical_locator(left: object, right: object) -> bool:
    """Compare an owner-normalized ``Path`` with an existing record string."""

    if not isinstance(left, (str, Path)) or not isinstance(right, (str, Path)):
        return False
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def decode_committed_semantic_evidence_result(
    authority: object,
    *,
    raw_inventory: BatchCommittedArtifactAuthority,
    semantic_branch: ValidatedSemanticDomainBranch,
    phenotype_view_attestation: BatchCommittedArtifactAuthority | None,
    analysis_frame: BatchCommittedArtifactAuthority,
    accepted_domain: BatchCommittedArtifactAuthority,
    measurements: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> ValidatedSemanticEvidenceResult:
    committed = _committed(
        authority,
        kind="semantic_evidence_result",
        producer="semantic_evidence",
        contract=semantic_evidence_result_identity_contract(),
    )
    joined = _common_join(
        raw_inventory=raw_inventory,
        analysis_frame=analysis_frame,
        accepted_domain=accepted_domain,
        measurements=measurements,
        semantic_profiles=semantic_profiles,
        semantic_branch=semantic_branch,
        resolved_sample=resolved_sample,
        secondary_selection=secondary_selection,
    )
    identity = committed.artifact_ref.owning_identity
    raw = joined["raw"]
    attestation_owner = _validated_evidence_attestation(
        semantic_branch=joined["branch"],
        raw_inventory=raw_inventory,
        phenotype_view_attestation=phenotype_view_attestation,
    )
    binding = joined["binding"]
    member = joined["member"]
    membership = identity.get("membership_artifact")
    if not isinstance(membership, Mapping) or set(membership) != {"path", "sha256", "byte_count"}:
        raise BatchPhenotypingError("semantic evidence membership descriptor differs")
    content = read_bounded_file(
        Path(str(membership["path"])),
        expected_byte_count=int(membership["byte_count"]),
        maximum_byte_count=512 * 1024 * 1024,
    )
    if hashlib.sha256(content).hexdigest() != membership["sha256"]:
        raise BatchPhenotypingError("semantic evidence membership bytes changed")
    expected = {
        "schema_version": "mif-batch-roi-semantic-evidence-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": joined["pipeline_run_id"],
        "analysis_config_sha256": resolved_sample.resolved_config_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "analysis_frame_ref_sha256": analysis_frame.artifact_ref.artifact_ref_sha256,
        "accepted_domain_ref_sha256": accepted_domain.artifact_ref.artifact_ref_sha256,
        "measurements_ref_sha256": measurements.artifact_ref.artifact_ref_sha256,
        "semantic_profile_ref_sha256s": tuple(
            item.artifact_ref.artifact_ref_sha256 for item in joined["profile_authorities"]
        ),
        "phenotype_source_ref_sha256": raw.phenotype_source_artifact_ref_sha256,
        "phenotype_view_attestation_ref_sha256": (
            None
            if attestation_owner is None
            else attestation_owner.artifact_ref.artifact_ref_sha256
        ),
        "segmentation_population_id": joined["measurements"].segmentation_population_id,
        "physical_calibration": {
            "x_um_per_px": joined["frame"].spatial_frame.x_um_per_px,
            "y_um_per_px": joined["frame"].spatial_frame.y_um_per_px,
        },
        "phenotype_view": _phenotype_view(joined["branch"]),
        "saved_state_before_sha256": (
            joined["branch"].phenotype_application.saved_state_after_sha256
        ),
        "saved_state_after_sha256": (
            joined["branch"].phenotype_application.saved_state_after_sha256
        ),
        "execution_exchange_sha256": committed.execution_exchange_sha256,
    }
    if (
        any(identity.get(key) != value for key, value in expected.items())
        or committed.state_head_sha256 != raw.state_head_sha256
        or committed.active_plan_sha256 != raw.active_plan_sha256
        or committed.artifact_ref.scope != raw.artifact_ref.scope
        or committed.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
        or committed.execution_exchange_sha256 is None
        or raw.snapshot.saved_state_after_sha256
        != joined["branch"].phenotype_application.saved_state_after_sha256
    ):
        raise BatchPhenotypingError("semantic evidence committed lineage differs")
    snapshots = validate_roi_semantic_evidence_response(
        raw_objects_path=Path(str(raw.artifact_ref.locator)),
        raw_export_sha256=raw.snapshot.raw_export_sha256,
        membership_artifact_path=Path(str(membership["path"])),
        membership_artifact_sha256=str(membership["sha256"]),
        slide_id=binding.slide_id,
        section_id=member.section_id,
        analysis_domain_id=binding.analysis_domain_id,
        tissue_generation_id=member.tissue_generation_id,
        pipeline_run_id=str(joined["pipeline_run_id"]),
        segmentation_population_id=joined["measurements"].segmentation_population_id,
        physical_calibration={
            "x_um_per_px": joined["frame"].spatial_frame.x_um_per_px,
            "y_um_per_px": joined["frame"].spatial_frame.y_um_per_px,
        },
        phenotype_view=_phenotype_view(joined["branch"]),
    )
    if (
        identity.get("membership_artifact")
        != _membership_descriptor(Path(str(membership["path"])), content)
        or identity.get("evidence_snapshot_sha256s") != tuple(item.digest for item in snapshots)
        or identity.get("evidence_count") != len(snapshots)
        or not _same_canonical_locator(committed.artifact_ref.locator, membership["path"])
        or committed.artifact_ref.sha256 != membership["sha256"]
        or committed.artifact_ref.byte_count != membership["byte_count"]
    ):
        raise BatchPhenotypingError("semantic evidence snapshot authority differs")
    return _projection(
        ValidatedSemanticEvidenceResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        snapshots=tuple(snapshots),
        membership_artifact_path=Path(str(membership["path"])),
        membership_artifact_bytes=content,
        pipeline_run_id=str(identity["pipeline_run_id"]),
    )


def _decode_semantic_evidence_for_evaluation(
    authority: object,
    *,
    raw_inventory: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> ValidatedSemanticEvidenceResult:
    """Reauthenticate evidence from the evaluation node's closed immediate edges."""

    committed = _committed(
        authority,
        kind="semantic_evidence_result",
        producer="semantic_evidence",
        contract=semantic_evidence_result_identity_contract(),
    )
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    profile, _bindings, _contents = _profile_owner(
        semantic_profiles, _sample_config(resolved_sample), secondary_selection
    )
    identity = committed.artifact_ref.owning_identity
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    expected = {
        "schema_version": "mif-batch-roi-semantic-evidence-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "analysis_config_sha256": resolved_sample.resolved_config_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "semantic_profile_ref_sha256s": tuple(
            item.artifact_ref.artifact_ref_sha256 for item in profile
        ),
        "phenotype_source_ref_sha256": raw.phenotype_source_artifact_ref_sha256,
        "segmentation_population_id": raw.snapshot.segmentation_population_id,
    }
    if (
        any(identity.get(key) != value for key, value in expected.items())
        or committed.state_head_sha256 != raw.state_head_sha256
        or committed.active_plan_sha256 != raw.active_plan_sha256
        or committed.artifact_ref.scope != raw.artifact_ref.scope
        or committed.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
        or identity.get("saved_state_before_sha256") != raw.snapshot.saved_state_after_sha256
        or identity.get("saved_state_after_sha256") != raw.snapshot.saved_state_after_sha256
        or committed.execution_exchange_sha256 is None
        or identity.get("execution_exchange_sha256") != committed.execution_exchange_sha256
        or any(
            item.state_head_sha256 != raw.state_head_sha256
            or item.active_plan_sha256 != raw.active_plan_sha256
            for item in profile
        )
    ):
        raise BatchPhenotypingError("semantic evidence immediate lineage differs")
    membership = identity.get("membership_artifact")
    calibration = identity.get("physical_calibration")
    phenotype_view = identity.get("phenotype_view")
    if not isinstance(membership, Mapping):
        raise BatchPhenotypingError("semantic evidence membership authority is invalid")
    path = Path(text(membership.get("path"), "semantic evidence membership path"))
    try:
        byte_count = int(membership["byte_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic evidence membership size is invalid") from exc
    content = read_bounded_file(
        path, expected_byte_count=byte_count, maximum_byte_count=512 * 1024 * 1024
    )
    snapshots = validate_roi_semantic_evidence_response(
        raw_objects_path=Path(str(raw.artifact_ref.locator)),
        raw_export_sha256=raw.snapshot.raw_export_sha256,
        membership_artifact_path=path,
        membership_artifact_sha256=text(
            membership.get("sha256"), "semantic evidence membership SHA-256"
        ),
        slide_id=binding.slide_id,
        section_id=raw.snapshot.section_id,
        analysis_domain_id=binding.analysis_domain_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=text(identity.get("pipeline_run_id"), "semantic pipeline run"),
        segmentation_population_id=raw.snapshot.segmentation_population_id,
        physical_calibration=(calibration if isinstance(calibration, Mapping) else {}),
        phenotype_view=(phenotype_view if isinstance(phenotype_view, Mapping) else {}),
    )
    if (
        identity.get("membership_artifact") != _membership_descriptor(path, content)
        or identity.get("evidence_snapshot_sha256s") != tuple(item.digest for item in snapshots)
        or identity.get("evidence_count") != len(snapshots)
        or not _same_canonical_locator(committed.artifact_ref.locator, path)
        or committed.artifact_ref.sha256 != membership.get("sha256")
        or committed.artifact_ref.byte_count != byte_count
    ):
        raise BatchPhenotypingError("semantic evidence immediate authority differs")
    return _projection(
        ValidatedSemanticEvidenceResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        snapshots=tuple(snapshots),
        membership_artifact_path=path,
        membership_artifact_bytes=content,
        pipeline_run_id=str(identity["pipeline_run_id"]),
    )


def _result_artifact(
    identity: Mapping[str, object],
) -> tuple[SemanticResultArtifactEvidence, bytes]:
    typed = SemanticResultArtifactEvidence.from_mapping(identity.get("semantic_result_artifact"))
    path = Path(typed.path)
    content = read_bounded_file(
        path,
        expected_byte_count=path.stat().st_size,
        maximum_byte_count=512 * 1024 * 1024,
    )
    if hashlib.sha256(content).hexdigest() != typed.sha256:
        raise BatchPhenotypingError("semantic result artifact bytes changed")
    return typed, content


def _evaluation_inputs(
    *,
    semantic_evidence: ValidatedSemanticEvidenceResult,
    raw_inventory: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> tuple[object, object, object]:
    if type(semantic_evidence) is not ValidatedSemanticEvidenceResult:
        raise TypeError("semantic evaluation requires decoded evidence")
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    sample = _sample_config(resolved_sample)
    profiles, bindings, contents = _profile_owner(semantic_profiles, sample, secondary_selection)
    if any(
        item != semantic_evidence.active_plan_sha256
        for item in (
            raw.active_plan_sha256,
            *(profile.active_plan_sha256 for profile in profiles),
        )
    ) or any(
        item != semantic_evidence.state_head_sha256
        for item in (
            raw.state_head_sha256,
            *(profile.state_head_sha256 for profile in profiles),
        )
    ):
        raise BatchPhenotypingError("semantic evaluation owners use foreign state")
    return raw, (bindings, contents), profiles


def decode_committed_semantic_evaluation_result(
    authority: object,
    *,
    semantic_evidence: ValidatedSemanticEvidenceResult,
    raw_inventory: BatchCommittedArtifactAuthority,
    semantic_profiles: Sequence[BatchCommittedArtifactAuthority],
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> ValidatedSemanticEvaluationResult:
    committed = _committed(
        authority,
        kind="semantic_evaluation_result",
        producer="semantic_evaluation",
        contract=semantic_evaluation_result_identity_contract(),
    )
    raw, _profile_values, profile = _evaluation_inputs(
        semantic_evidence=semantic_evidence,
        raw_inventory=raw_inventory,
        semantic_profiles=semantic_profiles,
        resolved_sample=resolved_sample,
        secondary_selection=secondary_selection,
    )
    identity = committed.artifact_ref.owning_identity
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    expected = {
        "schema_version": "mif-batch-roi-semantic-result-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": semantic_evidence.pipeline_run_id,
        "analysis_config_sha256": resolved_sample.resolved_config_sha256,
        "semantic_config_sha256": digest(resolved_sample.config.semantic.to_mapping()),
        "semantic_evidence_ref_sha256": semantic_evidence.artifact_ref.artifact_ref_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "semantic_profile_ref_sha256s": tuple(
            item.artifact_ref.artifact_ref_sha256 for item in profile
        ),
    }
    if (
        any(identity.get(key) != value for key, value in expected.items())
        or committed.state_head_sha256 != semantic_evidence.state_head_sha256
        or committed.active_plan_sha256 != semantic_evidence.active_plan_sha256
        or committed.artifact_ref.scope != raw.artifact_ref.scope
        or committed.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
    ):
        raise BatchPhenotypingError("semantic evaluation committed lineage differs")
    artifact, content = _result_artifact(identity)
    rows = authenticate_roi_semantic_result_artifact(
        result_rows=None,
        result_set_sha256=str(identity.get("semantic_result_set_sha256")),
        result_artifact=artifact,
        result_artifact_bytes=content,
        analysis_domain_id=binding.analysis_domain_id,
        section_id=raw.snapshot.section_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=str(identity.get("pipeline_run_id")),
    )
    try:
        decisions = tuple(
            SemanticRegionDecision.from_mapping(item)
            for item in identity.get("semantic_region_decisions", ())
        )
    except (TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic region decision lineage is invalid") from exc
    if (
        identity.get("review_status") not in {"accepted", "rejected"}
        or identity.get("review_action") not in {None, "accept", "reject"}
        or not _same_canonical_locator(committed.artifact_ref.locator, artifact.path)
        or committed.artifact_ref.sha256 != artifact.sha256
        or committed.artifact_ref.byte_count != len(content)
    ):
        raise BatchPhenotypingError("semantic evaluation result identity differs")
    return _projection(
        ValidatedSemanticEvaluationResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        result_rows=tuple(rows),
        result_artifact=artifact,
        result_artifact_bytes=content,
        result_set_sha256=str(identity["semantic_result_set_sha256"]),
        semantic_qc=MappingProxyType(dict(identity["semantic_qc"])),
        review_status=str(identity["review_status"]),
        review_action=identity["review_action"],
        decisions=decisions,
        pipeline_run_id=str(identity["pipeline_run_id"]),
    )


def _decode_semantic_evaluation_for_count(
    authority: object,
    *,
    raw_inventory: BatchCommittedArtifactAuthority,
    resolved_sample: ResolvedBatchSample,
) -> ValidatedSemanticEvaluationResult:
    """Reauthenticate evaluation from the count node's immediate edge set."""

    committed = _committed(
        authority,
        kind="semantic_evaluation_result",
        producer="semantic_evaluation",
        contract=semantic_evaluation_result_identity_contract(),
    )
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    sample = _sample_config(resolved_sample)
    identity = committed.artifact_ref.owning_identity
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    expected = {
        "schema_version": "mif-batch-roi-semantic-result-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "analysis_config_sha256": sample.resolved_config_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
    }
    if (
        any(identity.get(key) != value for key, value in expected.items())
        or committed.state_head_sha256 != raw.state_head_sha256
        or committed.active_plan_sha256 != raw.active_plan_sha256
        or committed.artifact_ref.scope != raw.artifact_ref.scope
        or committed.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
    ):
        raise BatchPhenotypingError("semantic evaluation immediate lineage differs")
    artifact, content = _result_artifact(identity)
    rows = authenticate_roi_semantic_result_artifact(
        result_rows=None,
        result_set_sha256=text(identity.get("semantic_result_set_sha256"), "semantic result set"),
        result_artifact=artifact,
        result_artifact_bytes=content,
        analysis_domain_id=binding.analysis_domain_id,
        section_id=raw.snapshot.section_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=text(identity.get("pipeline_run_id"), "semantic pipeline run"),
    )
    try:
        decisions = tuple(
            SemanticRegionDecision.from_mapping(item)
            for item in identity.get("semantic_region_decisions", ())
        )
    except (TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic evaluation decision history is invalid") from exc
    if (
        identity.get("review_status") not in {"accepted", "rejected"}
        or identity.get("review_action") not in {None, "accept", "reject"}
        or not _same_canonical_locator(committed.artifact_ref.locator, artifact.path)
        or committed.artifact_ref.sha256 != artifact.sha256
        or committed.artifact_ref.byte_count != len(content)
    ):
        raise BatchPhenotypingError("semantic evaluation immediate result differs")
    return _projection(
        ValidatedSemanticEvaluationResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        result_rows=tuple(rows),
        result_artifact=artifact,
        result_artifact_bytes=content,
        result_set_sha256=str(identity["semantic_result_set_sha256"]),
        semantic_qc=MappingProxyType(dict(identity["semantic_qc"])),
        review_status=str(identity["review_status"]),
        review_action=identity["review_action"],
        decisions=decisions,
        pipeline_run_id=str(identity["pipeline_run_id"]),
    )


def _count_inputs(
    evaluation: ValidatedSemanticEvaluationResult,
    raw_authority: BatchCommittedArtifactAuthority,
    domain_authority: BatchCommittedArtifactAuthority,
    sample: ResolvedBatchSample,
    secondary_selection=None,
) -> tuple[object, object, AnalysisDomainRef, dict[str, object], dict[str, SelectionContext]]:
    if type(evaluation) is not ValidatedSemanticEvaluationResult:
        raise TypeError("semantic count requires decoded evaluation")
    raw = decode_committed_semantic_raw_inventory(raw_authority)
    domain = decode_committed_accepted_analysis_domain(domain_authority)
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    member = _single_domain(domain, binding)
    if any(
        value != evaluation.state_head_sha256
        for value in (raw.state_head_sha256, domain.state_head_sha256)
    ) or any(
        value != evaluation.active_plan_sha256
        for value in (raw.active_plan_sha256, domain.active_plan_sha256)
    ):
        raise BatchPhenotypingError("semantic count owners use foreign state")
    domain_ref = AnalysisDomainRef(
        analysis_domain_id=binding.analysis_domain_id,
        kind=member.analysis_domain_kind,
        slide_id=binding.slide_id,
        section_id=member.section_id,
        tissue_generation_id=member.tissue_generation_id,
    )
    policies = {item.structure_id: item.count_bounds
                for item in resolve_secondary_structures(sample, secondary_selection)}
    if secondary_selection is not None and (
            secondary_selection.state_head_sha256 != raw.state_head_sha256
            or secondary_selection.active_plan_sha256 != raw.active_plan_sha256):
        raise BatchPhenotypingError("Count policy definitions belong to another execution revision")
    contexts = {
        key: SelectionContext(
            analysis_domain_sha256=binding.analysis_domain_revision_sha256,
            config_sha256=sample.resolved_config_sha256,
            result_sha256=evaluation.result_set_sha256,
        )
        for key in policies
    }
    inventory = _raw_inventory(
        raw,
        pipeline_run_id=str(evaluation.artifact_ref.owning_identity["pipeline_run_id"]),
    )
    return raw, domain, domain_ref, {"policies": policies, "inventory": inventory}, contexts


def _lineage_mapping(lineage: ManualSelectionLineage) -> dict[str, object]:
    mapped = asdict(lineage)
    mapped["algorithm"] = lineage.algorithm.value
    return plain_json(mapped)  # type: ignore[return-value]


def _lineage_identity_sequence(
    manual_lineages: Mapping[str, ManualSelectionLineage],
) -> list[dict[str, object]]:
    if not isinstance(manual_lineages, Mapping) or any(
        type(structure_id) is not str
        or not isinstance(lineage, ManualSelectionLineage)
        or lineage.structure_id != structure_id
        for structure_id, lineage in manual_lineages.items()
    ):
        raise TypeError("semantic count manual lineages are not exact typed owners")
    return [
        _lineage_mapping(manual_lineages[structure_id]) for structure_id in sorted(manual_lineages)
    ]


def decode_committed_final_count_cap_result(
    authority: object,
    *,
    semantic_evaluation: ValidatedSemanticEvaluationResult,
    raw_inventory: BatchCommittedArtifactAuthority,
    accepted_domain: BatchCommittedArtifactAuthority,
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> ValidatedFinalCountCapResult:
    committed = _committed(
        authority,
        kind="final_count_cap_result",
        producer="final_count_cap",
        contract=final_count_cap_result_identity_contract(),
    )
    identity = committed.artifact_ref.owning_identity
    try:
        retained_lineages = tuple(
            ManualSelectionLineage(**item)
            for item in identity.get("manual_selection_lineages", ())
        )
        manual_lineages = {item.structure_id: item for item in retained_lineages}
        evidence = tuple(
            ManualSelectionEvidence.from_mapping(item)
            for item in identity.get("manual_selection_evidence", ())
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("committed manual count authority is invalid") from exc
    if (
        len(manual_lineages) != len(retained_lineages)
        or tuple(item.structure_id for item in evidence) != tuple(sorted(manual_lineages))
        or any(
            plain_json(item.selection_lineage)
            != _lineage_mapping(manual_lineages[item.structure_id])
            for item in evidence
        )
    ):
        raise BatchPhenotypingError("committed manual count lineage and evidence differ")
    sample = _sample_config(resolved_sample)
    raw, _domain, domain_ref, values, contexts = _count_inputs(
        semantic_evaluation, raw_inventory, accepted_domain, sample, secondary_selection
    )
    lineage_identity = _lineage_identity_sequence(manual_lineages)
    resolution = resolve_roi_semantic_count(
        domain=domain_ref,
        result_rows=semantic_evaluation.result_rows,
        raw_inventory=values["inventory"],
        policies=values["policies"],
        selection_contexts=contexts,
        manual_selections=manual_lineages,
    )
    if resolution["manual_selection_requirement"] is not None:
        raise BatchPhenotypingError("committed semantic count still needs a manual cap")
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    expected = {
        "schema_version": "structure-count-decision/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": semantic_evaluation.pipeline_run_id,
        "analysis_config_sha256": sample.resolved_config_sha256,
        "semantic_evaluation_ref_sha256": semantic_evaluation.artifact_ref.artifact_ref_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "accepted_domain_ref_sha256": accepted_domain.artifact_ref.artifact_ref_sha256,
        "count_decision": plain_json(resolution["count_decision_mapping"]),
        "final_object_dispositions": plain_json(resolution["final_object_dispositions"]),
        "active_secondary_roi_ids": list(resolution["active_secondary_roi_ids"]),
        "manual_selection_lineages": lineage_identity,
        "manual_selection_evidence": [item.to_mapping() for item in evidence],
        "domain_status": (
            "excluded_below_minimum"
            if resolution["count_decision"].domain_exclusion is not None
            else "active"
        ),
    }
    if (
        any(plain_json(identity.get(key)) != plain_json(value) for key, value in expected.items())
        or committed.state_head_sha256 != semantic_evaluation.state_head_sha256
        or committed.active_plan_sha256 != semantic_evaluation.active_plan_sha256
        or committed.artifact_ref.scope != raw.artifact_ref.scope
        or committed.artifact_ref.configuration_bindings != raw.artifact_ref.configuration_bindings
    ):
        raise BatchPhenotypingError("committed semantic count mapping changed")
    dispositions = tuple(
        FinalObjectDispositionEvidence.from_mapping(item)
        for item in resolution["final_object_dispositions"]
    )
    return _projection(
        ValidatedFinalCountCapResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        count_decision=resolution["count_decision"],
        count_decision_mapping=MappingProxyType(dict(resolution["count_decision_mapping"])),
        dispositions=dispositions,
        active_secondary_roi_ids=tuple(resolution["active_secondary_roi_ids"]),
        manual_lineages=MappingProxyType(dict(manual_lineages)),
        manual_evidence=evidence,
        domain_status=str(expected["domain_status"]),
    )


def decode_committed_semantic_finalization_result(
    authority: object,
) -> ValidatedSemanticFinalizationResult:
    """Read a finalized owner without repeating its completed review pipeline.

    The consuming handoff joins this evidence to its current domain and frame;
    dataset selection authenticates the component and its cell population.
    """
    committed = _committed(
        authority,
        kind="semantic_finalization_result",
        producer="semantic_finalization",
        contract=semantic_finalization_result_identity_contract(),
    )
    evidence = FinalizedSecondaryDomainEvidence.from_mapping(committed.artifact_ref.owning_identity)
    bindings = committed.artifact_ref.scope.analysis_domain_bindings
    if (
        len(bindings) != 1
        or evidence.analysis_domain_id != bindings[0].analysis_domain_id
        or evidence.analysis_domain_revision_sha256 != bindings[0].analysis_domain_revision_sha256
        or evidence.domain_set_sha256 != bindings[0].domain_set_sha256
    ):
        raise BatchPhenotypingError("semantic finalization committed domain differs")
    return _projection(
        ValidatedSemanticFinalizationResult,
        artifact_ref=committed.artifact_ref,
        state_head_sha256=committed.state_head_sha256,
        active_plan_sha256=committed.active_plan_sha256,
        terminal_record_sha256=committed.terminal_record_sha256,
        evidence=evidence,
        component_descriptor=MappingProxyType({
            "path": str(committed.artifact_ref.locator),
            "sha256": committed.artifact_ref.sha256,
        }),
    )


def _output_ref(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    contract: ArtifactIdentityContract,
    identity: Mapping[str, object],
    locator: str | None,
    sha256: str | None,
    byte_count: int | None,
    runtime_versions: Mapping[str, str],
) -> BatchArtifactRef:
    binding = current.analysis_domain_bindings[0]
    ref = BatchArtifactRef(
        artifact_id=contract.artifact_kind.replace("_", "-") + "-" + digest(identity)[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=locator,
        sha256=sha256,
        byte_count=byte_count,
        scope=ArtifactScope(
            current.validation_domain_id,
            binding.sample_id,
            binding.slide_id,
            binding.project_entry_id,
            (binding,),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-batch-semantic-applicable/v1",
        runtime_versions=runtime_versions,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings,
    )
    return validate_artifact_ref(ref)


def build_semantic_evidence_execution_result(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    capability: SemanticEvidenceStageOwnerCapability,
    snapshots: Sequence[SemanticEvidenceSnapshot],
    membership_path: Path,
    membership_bytes: bytes,
    saved_state_before_sha256: str,
    saved_state_after_sha256: str,
    execution_exchange_sha256: str,
) -> BatchNodeExecutionResult:
    current = validate_current_execution_authority(current, expected_context=context)
    raw = capability.semantic_branch.raw_inventory
    binding = current.analysis_domain_bindings[0]
    attestation = capability.phenotype_view_attestation
    descriptor = _membership_descriptor(membership_path, membership_bytes)
    identity = {
        "schema_version": "mif-batch-roi-semantic-evidence-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": capability.measurements.artifact_ref.owning_identity["pipeline_run_id"],
        "analysis_config_sha256": capability.resolved_sample.resolved_config_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "analysis_frame_ref_sha256": capability.analysis_frame.artifact_ref.artifact_ref_sha256,
        "accepted_domain_ref_sha256": capability.accepted_domain.artifact_ref.artifact_ref_sha256,
        "measurements_ref_sha256": capability.measurements.artifact_ref.artifact_ref_sha256,
        "semantic_profile_ref_sha256s": [
            item.artifact_ref.artifact_ref_sha256
            for item in capability.semantic_profile_authorities
        ],
        "phenotype_source_ref_sha256": raw.phenotype_source_artifact_ref_sha256,
        "phenotype_view_attestation_ref_sha256": (
            None if attestation is None else attestation.artifact_ref.artifact_ref_sha256
        ),
        "segmentation_population_id": raw.snapshot.segmentation_population_id,
        "physical_calibration": dict(capability.request_body["physical_calibration"]),
        "phenotype_view": dict(capability.request_body["phenotype_view"]),
        "membership_artifact": descriptor,
        "evidence_snapshot_sha256s": [item.digest for item in snapshots],
        "evidence_count": len(tuple(snapshots)),
        "saved_state_before_sha256": saved_state_before_sha256,
        "saved_state_after_sha256": saved_state_after_sha256,
        "execution_exchange_sha256": execution_exchange_sha256,
    }
    ref = _output_ref(
        context,
        current,
        contract=semantic_evidence_result_identity_contract(),
        identity=identity,
        locator=str(membership_path.resolve(strict=False)),
        sha256=descriptor["sha256"],
        byte_count=len(membership_bytes),
        runtime_versions=raw.artifact_ref.runtime_versions,
    )
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (ref,))


def build_semantic_evaluation_execution_result(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    evidence: ValidatedSemanticEvidenceResult,
    raw: object,
    profile_authorities: Sequence[BatchCommittedArtifactAuthority],
    resolved_sample: ResolvedBatchSample,
    evaluated: Mapping[str, object],
    result_artifact: SemanticResultArtifactEvidence,
    review_action: str | None,
) -> BatchNodeExecutionResult:
    current = validate_current_execution_authority(current, expected_context=context)
    binding = current.analysis_domain_bindings[0]
    identity = {
        "schema_version": "mif-batch-roi-semantic-result-reference/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": evaluated["pipeline_run_id"],
        "analysis_config_sha256": resolved_sample.resolved_config_sha256,
        "semantic_config_sha256": evaluated["semantic_config_sha256"],
        "semantic_evidence_ref_sha256": evidence.artifact_ref.artifact_ref_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "semantic_profile_ref_sha256s": [
            item.artifact_ref.artifact_ref_sha256 for item in profile_authorities
        ],
        "semantic_result_artifact": result_artifact.to_mapping(),
        "semantic_result_set_sha256": evaluated["semantic_result_set_sha256"],
        "semantic_qc": plain_json(evaluated["semantic_qc"]),
        "review_status": evaluated["review_status"],
        "review_action": review_action,
        "semantic_region_decisions": plain_json(evaluated["semantic_region_decisions"]),
    }
    content = Path(result_artifact.path).read_bytes()
    ref = _output_ref(
        context,
        current,
        contract=semantic_evaluation_result_identity_contract(),
        identity=identity,
        locator=result_artifact.path,
        sha256=result_artifact.sha256,
        byte_count=len(content),
        runtime_versions=raw.artifact_ref.runtime_versions,
    )
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (ref,))


def build_final_count_cap_execution_result(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    evaluation: ValidatedSemanticEvaluationResult,
    raw: object,
    accepted_domain: BatchCommittedArtifactAuthority,
    resolved_sample: ResolvedBatchSample,
    resolution: Mapping[str, object],
    manual_lineages: Mapping[str, ManualSelectionLineage],
    manual_evidence: Sequence[ManualSelectionEvidence],
) -> BatchNodeExecutionResult:
    current = validate_current_execution_authority(current, expected_context=context)
    binding = current.analysis_domain_bindings[0]
    decision = resolution["count_decision"]
    identity = {
        "schema_version": "structure-count-decision/v1",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "project_entry_id": binding.project_entry_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "domain_set_sha256": binding.domain_set_sha256,
        "section_id": raw.snapshot.section_id,
        "tissue_generation_id": raw.snapshot.tissue_generation_id,
        "pipeline_run_id": evaluation.artifact_ref.owning_identity["pipeline_run_id"],
        "analysis_config_sha256": resolved_sample.resolved_config_sha256,
        "semantic_evaluation_ref_sha256": evaluation.artifact_ref.artifact_ref_sha256,
        "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
        "accepted_domain_ref_sha256": accepted_domain.artifact_ref.artifact_ref_sha256,
        "count_decision": plain_json(resolution["count_decision_mapping"]),
        "final_object_dispositions": plain_json(resolution["final_object_dispositions"]),
        "active_secondary_roi_ids": list(resolution["active_secondary_roi_ids"]),
        "manual_selection_lineages": _lineage_identity_sequence(manual_lineages),
        "manual_selection_evidence": [item.to_mapping() for item in manual_evidence],
        "domain_status": (
            "excluded_below_minimum" if decision.domain_exclusion is not None else "active"
        ),
    }
    ref = _output_ref(
        context,
        current,
        contract=final_count_cap_result_identity_contract(),
        identity=identity,
        locator=None,
        sha256=None,
        byte_count=None,
        runtime_versions=raw.artifact_ref.runtime_versions,
    )
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (ref,))


def build_semantic_finalization_execution_result(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    evidence: FinalizedSecondaryDomainEvidence,
    component_descriptor: Mapping[str, object],
    population_id: str,
    runtime_versions: Mapping[str, str],
) -> BatchNodeExecutionResult:
    current = validate_current_execution_authority(current, expected_context=context)
    identity = evidence.to_mapping()
    from mif_pipeline.datasets.references import read_membership_component_reference

    component = read_membership_component_reference(
        component_descriptor, domain_id=evidence.analysis_domain_id
    )
    if component.get("population_id") != population_id:
        raise BatchPhenotypingError("semantic membership component belongs to another population")
    ref = _output_ref(
        context,
        current,
        contract=semantic_finalization_result_identity_contract(),
        identity=identity,
        locator=component_descriptor["path"],
        sha256=component_descriptor["sha256"],
        byte_count=Path(component_descriptor["path"]).stat().st_size,
        runtime_versions=runtime_versions,
    )
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (ref,))


def publish_semantic_finalization_stage_owner_capability(
    publisher: ProjectStageOwnerCapabilityPublisher,
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    semantic_evaluation: ValidatedSemanticEvaluationResult,
    final_count_cap: ValidatedFinalCountCapResult,
    raw_inventory: BatchCommittedArtifactAuthority,
    analysis_frame: BatchCommittedArtifactAuthority,
    accepted_domain: BatchCommittedArtifactAuthority,
    measurements: BatchCommittedArtifactAuthority,
) -> SemanticFinalizationStageOwnerCapability:
    """Publish exact finalization owners after authenticating every direct edge."""

    if type(publisher) is not ProjectStageOwnerCapabilityPublisher:
        raise TypeError("semantic finalization requires the capability publisher")
    validated_current = validate_current_execution_authority(current, expected_context=context)
    if validated_current.node_kind != "semantic_finalization":
        raise BatchPhenotypingError("semantic finalization current node differs")
    raw = decode_committed_semantic_raw_inventory(raw_inventory)
    frame = validate_analysis_frame_artifact_ref(analysis_frame.artifact_ref)
    domain = decode_committed_accepted_analysis_domain(accepted_domain)
    measurement = decode_committed_measurements_result(measurements)
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    member = _single_domain(domain, binding)
    upstream = (
        semantic_evaluation,
        final_count_cap,
        raw,
        domain,
        measurement,
    )
    if (
        any(item.state_head_sha256 != validated_current.state_head_sha256 for item in upstream)
        or any(item.active_plan_sha256 != validated_current.active_plan_sha256 for item in upstream)
        or analysis_frame.state_head_sha256 != validated_current.state_head_sha256
        or analysis_frame.active_plan_sha256 != validated_current.active_plan_sha256
        or binding not in measurement.analysis_domain_bindings
        or domain.artifact_ref.scope.analysis_domain_bindings
        != measurement.analysis_domain_bindings
        or member.section_id != raw.snapshot.section_id
        or member.tissue_generation_id != raw.snapshot.tissue_generation_id
        or measurement.segmentation_population_id != raw.snapshot.segmentation_population_id
        or frame.spatial_frame.canonical_image_sha256 != raw.snapshot.canonical_image_sha256
    ):
        raise BatchPhenotypingError("semantic finalization owner lineage differs")
    _exact_context_inputs(
        context,
        validated_current,
        (
            semantic_evaluation.artifact_ref,
            final_count_cap.artifact_ref,
            raw.artifact_ref,
            analysis_frame.artifact_ref,
            accepted_domain.artifact_ref,
            measurements.artifact_ref,
        ),
    )
    result_bytes = bytes(semantic_evaluation.result_artifact_bytes)
    request = prepare_roi_semantic_finalization_request(
        result_rows=semantic_evaluation.result_rows,
        result_set_sha256=semantic_evaluation.result_set_sha256,
        result_artifact=semantic_evaluation.result_artifact,
        result_artifact_bytes=result_bytes,
        count_decision=final_count_cap.count_decision,
        final_object_dispositions=final_count_cap.dispositions,
        active_secondary_roi_ids=final_count_cap.active_secondary_roi_ids,
        analysis_domain_id=binding.analysis_domain_id,
        section_id=raw.snapshot.section_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=str(semantic_evaluation.artifact_ref.owning_identity["pipeline_run_id"]),
        review_status=semantic_evaluation.review_status,
        review_action=semantic_evaluation.review_action,
    )
    value = _issue_applicable_capability(
        SemanticFinalizationStageOwnerCapability,
        current=validated_current,
        resolved_sample=_sample_config(resolved_sample),
        semantic_evaluation=semantic_evaluation,
        final_count_cap=final_count_cap,
        raw_inventory=raw,
        analysis_frame=analysis_frame,
        accepted_domain=accepted_domain,
        measurements=measurements,
        request_body=MappingProxyType({
            **{key: value for key, value in raw.semantic_live_binding.items()
               if key != "canonical_image_sha256"},
            **request,
            "schema_version": "roi-workflow-invocation/v1",
            "stage": "semantic_validation",
            "sample_id": binding.sample_id,
            "slide_id": binding.slide_id,
        }),
        result_artifact_bytes=result_bytes,
    )
    assert isinstance(value, SemanticFinalizationStageOwnerCapability)
    publisher._publish_current(value, context=context, current=validated_current)
    return value


__all__ = [
    "ValidatedFinalCountCapResult",
    "ValidatedSemanticEvaluationResult",
    "ValidatedSemanticEvidenceResult",
    "ValidatedSemanticFinalizationResult",
    "build_final_count_cap_execution_result",
    "build_semantic_evidence_execution_result",
    "build_semantic_evaluation_execution_result",
    "build_semantic_finalization_execution_result",
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
]
