"""Authenticated direct issuer for capacity-zero semantic domain completion.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib

from mif_pipeline.cell_analysis.spatial_handoff import (
    NoSecondaryDomainCompletionEvidence,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes

from ..accepted_analysis_domain_records import (
    decode_committed_accepted_analysis_domain,
)
from ..batch_analysis_preflight import validate_analysis_frame_artifact_ref
from ..batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ..batch_dag import ResourceKind
from ..batch_manifest import ResolvedBatchSample
from ..batch_measurement_artifacts import decode_committed_measurements_result
from ._common import BatchPhenotypingError, digest, text
from .semantic_branch import (
    ValidatedSemanticDomainBranch,
    validate_semantic_domain_branch,
)

_NODE_KIND = "no_secondary_completion"
_OUTPUT_KIND = "no_secondary_completion_evidence"
_PRODUCER_VERSION = "mif-pipeline-batch-no-secondary-completion/v1"
_REQUIRED_INPUT_KINDS = frozenset(
    {
        "analysis_frame",
        "accepted_analysis_domain",
        "measurements_result",
        "semantic_raw_inventory",
    }
)


# ==============================================================================
#                              Semantic completion
# ==============================================================================


def no_secondary_completion_identity_contract() -> ArtifactIdentityContract:
    """Return the batch envelope contract for the existing completion record."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-no-secondary-completion-identity/v1",
        artifact_kind=_OUTPUT_KIND,
        schema_name="semantic-domain-completion",
        schema_version=1,
        required_fields=tuple(NoSecondaryDomainCompletionEvidence.__dataclass_fields__),
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _require_authority(
    value: object,
    *,
    artifact_kind: str,
    producer_node_kind: str,
) -> BatchCommittedArtifactAuthority:
    if type(value) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError(f"{artifact_kind} requires committed state authority")
    (authority,) = validate_committed_artifact_authority_set((value,))
    if (
        authority.artifact_ref.artifact_kind != artifact_kind
        or authority.producer_node_kind != producer_node_kind
    ):
        raise BatchArtifactError(f"{artifact_kind} committed owner differs")
    return authority


def _context_inputs(
    context: BatchNodeExecutionContext,
    branch: ValidatedSemanticDomainBranch,
) -> dict[str, BatchArtifactRef]:
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if len(by_kind) != len(context.inputs) or not _REQUIRED_INPUT_KINDS <= set(by_kind):
        raise BatchPhenotypingError("no-secondary completion input inventory differs")
    source_kind = branch.phenotype_application.source_artifact_kind
    expected = set(_REQUIRED_INPUT_KINDS)
    if source_kind in {
        "marker_phenotype_result",
        "supervised_phenotype_result",
    }:
        expected.add(source_kind)
    elif source_kind == "cluster_label_import_result":
        expected.update({source_kind, "phenotype_view_attestation"})
    else:
        raise BatchPhenotypingError("no-secondary phenotype source is unsupported")
    if set(by_kind) != expected:
        raise BatchPhenotypingError("no-secondary completion source inventory differs")
    if (
        by_kind[source_kind] != branch.phenotype_application.source_artifact_ref
        or by_kind["semantic_raw_inventory"] != branch.raw_inventory.artifact_ref
    ):
        raise BatchPhenotypingError("no-secondary completion source authority differs")
    return by_kind


def build_no_secondary_completion_execution_result(
    *,
    context: BatchNodeExecutionContext,
    current_execution: BatchCurrentExecutionAuthority,
    semantic_branch: ValidatedSemanticDomainBranch,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    accepted_domain_authority: BatchCommittedArtifactAuthority,
    measurements_authority: BatchCommittedArtifactAuthority,
    resolved_sample: ResolvedBatchSample,
    secondary_selection=None,
) -> BatchNodeExecutionResult:
    """Issue one locatorless completion from exact authenticated owner joins."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(resolved_sample) is not ResolvedBatchSample
        or context.adapter_spec.node_kind != _NODE_KIND
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.resource_kind != ResourceKind.SCIENTIFIC_COMPUTE.value
        or context.adapter_spec.declared_outputs != (_OUTPUT_KIND,)
        or context.adapter_spec.output_identity_contracts
        != (no_secondary_completion_identity_contract(),)
    ):
        raise BatchArtifactError("no-secondary completion execution contract differs")
    current = validate_current_execution_authority(current_execution, expected_context=context)
    from ..batch_secondary_definitions import resolve_secondary_structures
    structures = resolve_secondary_structures(resolved_sample, secondary_selection)
    if secondary_selection is not None and (
            secondary_selection.state_head_sha256 != current.state_head_sha256
            or secondary_selection.active_plan_sha256 != current.active_plan_sha256):
        raise BatchArtifactError("No-secondary choice belongs to another execution revision")
    branch = validate_semantic_domain_branch(semantic_branch)
    if (
        branch.branch_kind != "no_secondary"
        or branch.configured_secondary_ids
        or branch.candidate_secondary_roi_ids
        or branch.raw_inventory.configured_secondary_ids
        or branch.raw_inventory.candidate_secondary_roi_ids
    ):
        raise BatchPhenotypingError(
            "no-secondary completion requires empty configured and candidate inventories"
        )
    inputs = _context_inputs(context, branch)
    frame_authority = _require_authority(
        analysis_frame_authority,
        artifact_kind="analysis_frame",
        producer_node_kind="analysis_preflight",
    )
    domain_authority = _require_authority(
        accepted_domain_authority,
        artifact_kind="accepted_analysis_domain",
        producer_node_kind="analysis_domain_selection",
    )
    measurement_authority = _require_authority(
        measurements_authority,
        artifact_kind="measurements_result",
        producer_node_kind="measurements",
    )
    if (
        inputs["analysis_frame"] != frame_authority.artifact_ref
        or inputs["accepted_analysis_domain"] != domain_authority.artifact_ref
        or inputs["measurements_result"] != measurement_authority.artifact_ref
    ):
        raise BatchArtifactError("no-secondary completion committed inputs differ")
    frame = validate_analysis_frame_artifact_ref(frame_authority.artifact_ref)
    domain = decode_committed_accepted_analysis_domain(domain_authority)
    measurements = decode_committed_measurements_result(measurement_authority)

    authorities = (frame_authority, domain_authority, measurement_authority)
    if any(
        item.state_head_sha256 != current.state_head_sha256
        or item.active_plan_sha256 != current.active_plan_sha256
        for item in authorities
    ) or (
        branch.state_head_sha256 != current.state_head_sha256
        or branch.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("no-secondary completion owners are not current")
    if (
        current.node_kind != _NODE_KIND
        or current.node_scope != "sample"
        or len(current.participant_ids) != 1
        or len(current.project_entry_ids) != 1
        or len(current.analysis_domain_bindings) != 1
        or len(current.configuration_bindings) != 1
        or current.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    ):
        raise BatchArtifactError("no-secondary completion execution scope differs")

    binding = (
        branch.analysis_domain_bindings[0] if len(branch.analysis_domain_bindings) == 1 else None
    )
    if binding is None or current.analysis_domain_bindings != (binding,):
        raise BatchPhenotypingError("no-secondary completion domain binding differs")
    phenotype = branch.phenotype_application
    raw = branch.raw_inventory
    snapshot = raw.snapshot
    source_identity = phenotype.source_artifact_ref.owning_identity
    members = tuple(
        item
        for item in domain.domain_authorities
        if item.analysis_domain_id == binding.analysis_domain_id
        and item.analysis_domain_revision_sha256 == binding.analysis_domain_revision_sha256
    )
    if len(members) != 1:
        raise BatchArtifactError("no-secondary completion active domain is ambiguous")
    member = members[0]
    frame_scope = frame_authority.artifact_ref.scope
    config_binding = current.configuration_bindings[0]
    config_digest = hashlib.sha256(
        canonical_json_bytes(resolved_sample.config.to_mapping())
    ).hexdigest()
    measurement_run_id = text(
        measurements.artifact_ref.owning_identity.get("pipeline_run_id"),
        "measurements pipeline_run_id",
    )
    supervised_source = phenotype.source_artifact_kind == "supervised_phenotype_result"
    raw_source_refs = source_identity.get("source_ref_sha256s")
    if supervised_source:
        source_measurement_lineage = (
            isinstance(raw_source_refs, (tuple, list))
            and measurements.artifact_ref.artifact_ref_sha256 in tuple(raw_source_refs)
            and phenotype.source_artifact_ref.scope.validation_domain_id
            == measurements.artifact_ref.scope.validation_domain_id
            == raw.artifact_ref.scope.validation_domain_id
            == current.validation_domain_id
            == resolved_sample.validation_domain_id
        )
    else:
        source_measurement_lineage = (
            isinstance(raw_source_refs, (tuple, list))
            and measurements.artifact_ref.artifact_ref_sha256 in tuple(raw_source_refs)
            and source_identity.get("measurement_generation_id")
            == measurements.measurement_generation_id
            and source_identity.get("canonical_image_id") == domain.canonical_image_id
            and source_identity.get("canonical_image_sha256") == domain.canonical_image_sha256
            and source_identity.get("segmentation_population_id")
            == measurements.segmentation_population_id
        )
    if supervised_source and not source_measurement_lineage:
        raise BatchPhenotypingError(
            "no-secondary completion validation domain or measurement lineage differs"
        )
    if (
        config_binding.sample_id != resolved_sample.sample_id
        or config_binding.resolved_config_sha256 != resolved_sample.resolved_config_sha256
        or config_digest != resolved_sample.resolved_config_sha256
        or any(
            item.configuration_bindings != current.configuration_bindings for item in context.inputs
        )
        or binding.sample_id != resolved_sample.sample_id
        or binding.slide_id != resolved_sample.slide_id
        or binding.project_entry_id != current.project_entry_ids[0]
        or current.participant_ids != (binding.sample_id,)
        or structures
        or binding not in phenotype.analysis_domain_bindings
        or raw.artifact_ref.scope.analysis_domain_bindings != (binding,)
        or measurements.analysis_domain_bindings != phenotype.analysis_domain_bindings
        or domain.artifact_ref.scope.analysis_domain_bindings != phenotype.analysis_domain_bindings
        or domain.domain_set.domain_set_sha256 != binding.domain_set_sha256
        or member.analysis_domain_revision != binding.analysis_domain_revision
        or member.analysis_domain_revision_sha256 != binding.analysis_domain_revision_sha256
        or member.project_entry_id != binding.project_entry_id
        or member.slide_id != binding.slide_id
        or frame_scope.sample_id != binding.sample_id
        or frame_scope.slide_id != binding.slide_id
        or frame.spatial_frame.canonical_image_id != domain.canonical_image_id
        or frame.spatial_frame.canonical_image_sha256 != domain.canonical_image_sha256
        or measurements.canonical_image_id != domain.canonical_image_id
        or measurements.canonical_image_sha256 != domain.canonical_image_sha256
        or snapshot.canonical_image_sha256 != domain.canonical_image_sha256
        or (not supervised_source and not source_measurement_lineage)
        or member.canonical_image_id != domain.canonical_image_id
        or member.canonical_image_sha256 != domain.canonical_image_sha256
        or phenotype.section_id != member.section_id
        or snapshot.section_id != member.section_id
        or measurements.section_id != member.section_id
        or phenotype.tissue_generation_id != member.tissue_generation_id
        or snapshot.tissue_generation_id != member.tissue_generation_id
        or measurements.tissue_generation_id != member.tissue_generation_id
        or snapshot.segmentation_population_id != measurements.segmentation_population_id
        or phenotype.cell_ids_sha256 != measurements.cell_ids_sha256
        or phenotype.cell_to_domain_membership_sha256
        != measurements.cell_to_domain_membership_sha256
        or phenotype.saved_state_after_sha256 != snapshot.saved_state_after_sha256
    ):
        raise BatchPhenotypingError("no-secondary completion owner join differs")

    completion = NoSecondaryDomainCompletionEvidence.create(
        analysis_domain_id=binding.analysis_domain_id,
        analysis_domain_revision_sha256=binding.analysis_domain_revision_sha256,
        domain_set_sha256=binding.domain_set_sha256,
        spatial_frame_sha256=frame.spatial_frame.spatial_frame_sha256,
        section_id=phenotype.section_id,
        tissue_generation_id=phenotype.tissue_generation_id,
        pipeline_run_id=measurement_run_id,
        analysis_config_sha256=resolved_sample.resolved_config_sha256,
        raw_input_sha256=snapshot.raw_export_sha256,
        domain_status=snapshot.primary_domain_status,
    )
    identity = completion.to_mapping()
    contract = no_secondary_completion_identity_contract()
    output = BatchArtifactRef(
        artifact_id="no-secondary-completion-" + digest(identity)[:48],
        artifact_kind=_OUTPUT_KIND,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            raw.artifact_ref.scope.validation_domain_id,
            binding.sample_id,
            binding.slide_id,
            binding.project_entry_id,
            (binding,),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions=raw.artifact_ref.runtime_versions,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings,
    )
    return BatchNodeExecutionResult(
        authorization_sha256=context.authorization.authorization_sha256,
        outputs=(output,),
    )


__all__ = [
    "build_no_secondary_completion_execution_result",
    "no_secondary_completion_identity_contract",
]
