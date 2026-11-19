"""adapter seam for authenticated cluster-label application.

The `AuthorizedPhenotypeHierarchyAttestation` record carries `attestation`,
`cluster_import`, `raw_inventory`, `source_authorities`, `current_execution` and its
remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
)
from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    decode_committed_accepted_analysis_domain,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    decode_committed_measurements_result,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    decode_committed_phenotyping_mode,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    decode_committed_segmentation_result,
)
from mif_pipeline.orchestration.batch_stage_adapters import (
    build_project_current_image_stage_codec,
)
from mif_pipeline.orchestration.phenotype_views import (
    PhenotypeViewStore,
    PreparedPhenotypeView,
)
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageTargetIntent,
)

from .application_codec import (
    build_cluster_label_import_execution_result,
    build_cluster_label_import_invocation,
    cluster_label_import_result_identity_contract,
    decode_committed_accepted_cluster_label_review,
    decode_committed_cluster_label_import_result,
    decode_committed_clustering_result_for_application,
    decode_committed_semantic_raw_inventory,
    persist_phenotype_hierarchy_attestation,
    prepare_phenotype_hierarchy_attestation_directory,
)
from .application_records import (
    ClusterLabelSectionApplication,
    PhenotypeHierarchyAttestation,
    ValidatedClusterLabelImportProjection,
    ValidatedSemanticRawInventoryProjection,
    phenotype_hierarchy_attestation_identity_contract,
)
from .application_execution import (
    attest_committed_section_cluster_label_application,
    derive_section_cluster_label_application,
    prepare_section_cluster_label_application,
    reopen_cluster_label_section_population,
)
from ._common import BatchPhenotypingError, digest

_AUTHORIZED_APPLICATION_ISSUER = object()
_AUTHORIZED_ATTESTATION_ISSUER = object()


# ==============================================================================
#                              Application adapters
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedClusterLabelApplication:
    """Non-wire result of reopening every committed 13B source owner."""

    application: ClusterLabelSectionApplication
    prepared: PreparedPhenotypeView
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    current_execution: BatchCurrentExecutionAuthority
    seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("cluster-label application authority is issuer-only")


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedPhenotypeHierarchyAttestation:
    """Issuer-only result derived from current import and raw bytes."""

    attestation: PhenotypeHierarchyAttestation
    cluster_import: ValidatedClusterLabelImportProjection
    raw_inventory: ValidatedSemanticRawInventoryProjection
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    current_execution: BatchCurrentExecutionAuthority
    output_directory: Path
    seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("phenotype hierarchy attestation is issuer-only")


def _authority_material(
    authority: AuthorizedClusterLabelApplication,
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "application_object_identity": id(authority.application),
        "prepared_object_identity": id(authority.prepared),
        "source_authority_object_identities": [id(item) for item in authority.source_authorities],
        "source_ref_sha256s": [
            item.artifact_ref.artifact_ref_sha256 for item in authority.source_authorities
        ],
        "current_execution_object_identity": id(authority.current_execution),
        "application_projection_sha256": (authority.application.composite_projection_sha256),
        "prepared_artifact_sha256": authority.prepared.artifact_sha256,
    }


def issue_authorized_cluster_label_application(
    *,
    context: BatchNodeExecutionContext,
    current_execution: BatchCurrentExecutionAuthority,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    authenticated_output_root: Path,
    phenotype_view_store: PhenotypeViewStore,
    source_run_id: str,
    expected_saved_state_before_sha256: str,
    maximum_clustering_result_byte_count: int = 512 * 1024 * 1024,
) -> AuthorizedClusterLabelApplication:
    """Derive and issue solely from current state-owned scientific inputs."""

    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("cluster-label issuance requires execution context")
    try:
        current = validate_current_execution_authority(current_execution, expected_context=context)
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "cluster-label issuance requires current execution authority"
        ) from exc
    if not isinstance(authenticated_output_root, Path):
        raise BatchArtifactError("cluster-label issuance requires output root")
    if type(phenotype_view_store) is not PhenotypeViewStore:
        raise BatchArtifactError("cluster-label issuance requires phenotype store")
    authorities = validate_committed_artifact_authority_set(
        source_authorities,
        expected_active_plan_sha256=current.active_plan_sha256,
    )
    if tuple(item.artifact_ref for item in authorities) != tuple(
        sorted(context.inputs, key=lambda item: item.artifact_ref_sha256)
    ) or {item.state_head_sha256 for item in authorities} != {current.state_head_sha256}:
        raise BatchArtifactError(
            "cluster-label application sources are not the current state authority"
        )
    by_kind: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authorities:
        by_kind.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    expected_kinds = {
        "accepted_analysis_domain",
        "accepted_cluster_label_review",
        "clustered_phenotype_result",
        "measurements_result",
        "phenotyping_mode",
        "segmentation_result",
    }
    if set(by_kind) != expected_kinds or any(
        len(by_kind[kind]) != 1
        for kind in expected_kinds - {"accepted_analysis_domain", "phenotyping_mode"}
    ):
        raise BatchArtifactError("cluster-label application source kinds differ")
    measurement_ref = by_kind["measurements_result"][0].artifact_ref
    if (
        current.validation_domain_id != measurement_ref.scope.validation_domain_id
        or current.participant_ids != (str(measurement_ref.scope.sample_id),)
        or current.project_entry_ids != (str(measurement_ref.scope.project_entry_id),)
        or current.analysis_domain_bindings != measurement_ref.scope.analysis_domain_bindings
        or current.configuration_bindings != measurement_ref.configuration_bindings
    ):
        raise BatchArtifactError("cluster-label application target scope differs")
    try:
        clustering_authority = by_kind["clustered_phenotype_result"][0]
        review_authority = by_kind["accepted_cluster_label_review"][0]
        clustering_result = decode_committed_clustering_result_for_application(
            clustering_authority,
            maximum_byte_count=maximum_clustering_result_byte_count,
        )
        accepted_review = decode_committed_accepted_cluster_label_review(review_authority)
        segmentation = decode_committed_segmentation_result(by_kind["segmentation_result"][0])
        measurements = decode_committed_measurements_result(by_kind["measurements_result"][0])
        accepted_domains = tuple(
            decode_committed_accepted_analysis_domain(item)
            for item in by_kind["accepted_analysis_domain"]
        )
        phenotyping_modes = tuple(
            decode_committed_phenotyping_mode(item) for item in by_kind["phenotyping_mode"]
        )
        population = reopen_cluster_label_section_population(
            segmentation=segmentation,
            measurements=measurements,
            accepted_domains=accepted_domains,
            phenotyping_modes=phenotyping_modes,
            authenticated_output_root=authenticated_output_root,
        )
        application = derive_section_cluster_label_application(
            clustering_result=clustering_result,
            clustering_result_artifact_ref_sha256=(
                clustering_authority.artifact_ref.artifact_ref_sha256
            ),
            accepted_review=accepted_review,
            accepted_review_artifact_ref_sha256=(review_authority.artifact_ref.artifact_ref_sha256),
            population=population,
            source_run_id=source_run_id,
            expected_saved_state_before_sha256=expected_saved_state_before_sha256,
        )
        prepared = prepare_section_cluster_label_application(
            application, store=phenotype_view_store
        )
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchArtifactError("cluster-label application owner derivation failed") from exc
    if (
        tuple(item.artifact_ref.artifact_ref_sha256 for item in authorities)
        != application.source_ref_sha256s
    ):
        raise BatchArtifactError("derived cluster-label source set differs")
    result = object.__new__(AuthorizedClusterLabelApplication)
    for name, value in {
        "application": application,
        "prepared": prepared,
        "source_authorities": authorities,
        "current_execution": current,
        "seal_sha256": "0" * 64,
        "_issuer": _AUTHORIZED_APPLICATION_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "seal_sha256", digest(_authority_material(result)))
    return result


def _attestation_authority_material(
    authority: AuthorizedPhenotypeHierarchyAttestation,
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "attestation_object_identity": id(authority.attestation),
        "cluster_import_object_identity": id(authority.cluster_import),
        "raw_inventory_object_identity": id(authority.raw_inventory),
        "source_authority_object_identities": [id(item) for item in authority.source_authorities],
        "current_execution_object_identity": id(authority.current_execution),
        "output_directory": str(authority.output_directory),
        "attestation_sha256": authority.attestation.attestation_sha256,
    }


def issue_authorized_phenotype_hierarchy_attestation(
    *,
    context: BatchNodeExecutionContext,
    current_execution: BatchCurrentExecutionAuthority,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    phenotype_view_store: PhenotypeViewStore,
    output_directory: Path,
    maximum_raw_export_byte_count: int = 512 * 1024 * 1024,
) -> AuthorizedPhenotypeHierarchyAttestation:
    """Derive the attestation from two exact current committed input owners."""

    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("attestation issuance requires execution context")
    try:
        current = validate_current_execution_authority(current_execution, expected_context=context)
        authorities = validate_committed_artifact_authority_set(
            source_authorities,
            expected_active_plan_sha256=current.active_plan_sha256,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("attestation state authority is invalid") from exc
    if (
        type(phenotype_view_store) is not PhenotypeViewStore
        or tuple(item.artifact_ref for item in authorities)
        != tuple(sorted(context.inputs, key=lambda item: item.artifact_ref_sha256))
        or {item.state_head_sha256 for item in authorities} != {current.state_head_sha256}
    ):
        raise BatchArtifactError("attestation sources are not current")
    by_kind = {item.artifact_ref.artifact_kind: item for item in authorities}
    if len(authorities) != 2 or set(by_kind) != {
        "cluster_label_import_result",
        "semantic_raw_inventory",
    }:
        raise BatchArtifactError("attestation source kinds differ")
    try:
        cluster_import = decode_committed_cluster_label_import_result(
            by_kind["cluster_label_import_result"]
        )
        raw_inventory = decode_committed_semantic_raw_inventory(
            by_kind["semantic_raw_inventory"],
            maximum_byte_count=maximum_raw_export_byte_count,
        )
        import_ref = cluster_import.artifact_ref
        raw_ref = raw_inventory.artifact_ref
        if (
            import_ref.scope != raw_ref.scope
            or import_ref.configuration_bindings != raw_ref.configuration_bindings
            or current.validation_domain_id != raw_ref.scope.validation_domain_id
            or current.participant_ids != (str(raw_ref.scope.sample_id),)
            or current.project_entry_ids != (str(raw_ref.scope.project_entry_id),)
            or current.analysis_domain_bindings != raw_ref.scope.analysis_domain_bindings
            or current.configuration_bindings != raw_ref.configuration_bindings
        ):
            raise BatchArtifactError("attestation source scope differs")
        destination = prepare_phenotype_hierarchy_attestation_directory(output_directory)
        attestation = attest_committed_section_cluster_label_application(
            cluster_import=cluster_import,
            raw_inventory=raw_inventory,
            store=phenotype_view_store,
        )
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchArtifactError("attestation owner derivation failed") from exc
    result = object.__new__(AuthorizedPhenotypeHierarchyAttestation)
    for name, value in {
        "attestation": attestation,
        "cluster_import": cluster_import,
        "raw_inventory": raw_inventory,
        "source_authorities": authorities,
        "current_execution": current,
        "output_directory": destination,
        "seal_sha256": "0" * 64,
        "_issuer": _AUTHORIZED_ATTESTATION_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    object.__setattr__(
        result,
        "seal_sha256",
        digest(_attestation_authority_material(result)),
    )
    return result


def build_phenotype_hierarchy_attestation_execution_result(
    *,
    context: BatchNodeExecutionContext,
    authority: AuthorizedPhenotypeHierarchyAttestation,
) -> BatchNodeExecutionResult:
    """Persist and publish one state-derived hierarchy attestation."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(authority) is not AuthorizedPhenotypeHierarchyAttestation
        or authority._issuer is not _AUTHORIZED_ATTESTATION_ISSUER
        or authority.seal_sha256 != digest(_attestation_authority_material(authority))
        or context.adapter_spec.node_kind != "phenotype_hierarchy_attestation"
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.resource_kind != "scientific_compute"
        or context.adapter_spec.output_identity_contracts
        != (phenotype_hierarchy_attestation_identity_contract(),)
    ):
        raise BatchArtifactError("attestation publication authority is invalid")
    validate_current_execution_authority(authority.current_execution, expected_context=context)
    if tuple(item.artifact_ref for item in authority.source_authorities) != tuple(
        sorted(context.inputs, key=lambda item: item.artifact_ref_sha256)
    ):
        raise BatchArtifactError("attestation publication sources differ")
    destination, payload_sha256, byte_count = persist_phenotype_hierarchy_attestation(
        authority.attestation, authority.output_directory
    )
    raw_ref = authority.raw_inventory.artifact_ref
    owning_identity = {
        key: value
        for key, value in authority.attestation.to_mapping().items()
        if key != "schema_version"
    }
    contract = phenotype_hierarchy_attestation_identity_contract()
    output = BatchArtifactRef(
        artifact_id="phenotype-view-attestation-" + payload_sha256[:48],
        artifact_kind="phenotype_view_attestation",
        schema_name="mif-batch-phenotype-view-attestation",
        schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=destination,
        sha256=payload_sha256,
        byte_count=byte_count,
        scope=raw_ref.scope,
        owning_identity=owning_identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-phenotype-hierarchy-attestation/v1",
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=raw_ref.configuration_bindings,
    )
    return BatchNodeExecutionResult(
        authorization_sha256=context.authorization.authorization_sha256,
        outputs=(output,),
    )


@runtime_checkable
class ClusterLabelApplicationAuthorityResolver(Protocol):
    """State-owner callback; raw artifact refs cannot substitute for it."""

    def __call__(self, context: BatchNodeExecutionContext, *,
                 saved_state_before_sha256: str) -> AuthorizedClusterLabelApplication: ...


def build_cluster_label_import_project_stage_codec(
    spec: StageAdapterSpec,
    *,
    authority_resolver: ClusterLabelApplicationAuthorityResolver,
) -> ProjectStageCodec:
    """Bind the native importer to one exact state-authenticated section."""

    if spec.node_kind != "clustering_label_import":
        raise BatchArtifactError("cluster-label codec requires its exact stage spec")
    if spec.output_identity_contracts != (cluster_label_import_result_identity_contract(),):
        raise BatchArtifactError("cluster-label codec output owner differs")
    if not isinstance(authority_resolver, ClusterLabelApplicationAuthorityResolver):
        raise BatchArtifactError("cluster-label codec requires state authority resolver")

    def resolve(context: BatchNodeExecutionContext, saved_state_before_sha256: str) -> AuthorizedClusterLabelApplication:
        authority = authority_resolver(context, saved_state_before_sha256=saved_state_before_sha256)
        if (
            type(authority) is not AuthorizedClusterLabelApplication
            or authority._issuer is not _AUTHORIZED_APPLICATION_ISSUER
            or authority.seal_sha256 != digest(_authority_material(authority))
        ):
            raise BatchArtifactError("state owner returned invalid cluster-label authority")
        validate_committed_artifact_authority_set(
            authority.source_authorities,
            expected_active_plan_sha256=authority.current_execution.active_plan_sha256,
        )
        validate_current_execution_authority(authority.current_execution, expected_context=context)
        if tuple(sorted(item.artifact_ref_sha256 for item in context.inputs)) != (
            authority.application.source_ref_sha256s
        ):
            raise BatchArtifactError("cluster-label authority source set differs")
        return authority

    def projector(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> object:
        authority = resolve(context, intent.saved_state_before_sha256)
        application = authority.application
        if (
            intent.sample_id != application.sample_id
            or intent.project_entry_id != application.project_entry_id
            or intent.slide_id != application.slide_id
            or intent.saved_state_before_sha256 != application.expected_saved_state_before_sha256
        ):
            raise BatchArtifactError("cluster-label target intent differs")
        return build_cluster_label_import_invocation(
            application,
            authority.prepared,
            config=intent.resolved_sample.config,
            run_id=intent.pipeline_run_id,
        )

    def validator(evidence: object) -> object:
        context = getattr(evidence, "context", None)
        if type(context) is not BatchNodeExecutionContext:
            raise BatchArtifactError("cluster-label result lacks execution context")
        authority = resolve(context, evidence.authorized.saved_state_before_sha256)
        return build_cluster_label_import_execution_result(
            evidence, authority.application, authority.prepared
        )

    return build_project_current_image_stage_codec(
        spec,
        invocation_projector=projector,
        result_validator=validator,
    )


__all__ = [
    "AuthorizedClusterLabelApplication",
    "AuthorizedPhenotypeHierarchyAttestation",
    "ClusterLabelApplicationAuthorityResolver",
    "build_cluster_label_import_project_stage_codec",
    "build_phenotype_hierarchy_attestation_execution_result",
    "issue_authorized_cluster_label_application",
    "issue_authorized_phenotype_hierarchy_attestation",
]
