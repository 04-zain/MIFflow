"""State-authenticated adapter for marker-rule section application.

The `AuthorizedMarkerRuleSectionApplication` record carries `application`,
`source_authorities`, `current_execution`, `resolved_config_sha256`, `seal_sha256`.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from mif_pipeline.datasets.references import freeze_component_reference

from mif_pipeline.orchestration.batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    StageAdapterSpec,
    artifact_ref_set_sha256,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_stage_adapters import build_project_current_image_stage_codec
from mif_pipeline.orchestration.phenotype_views import PhenotypeViewStore
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageTargetIntent,
)
from mif_pipeline.orchestration.project_stage_plan import ProjectStageTargetExecutionEvidence
from mif_pipeline.pipeline_core import StageStatus
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from ._common import BatchPhenotypingError, canonical_bytes, digest
from .marker_application_execution import (
    build_marker_rule_application_invocation,
    build_marker_rule_section_application,
)
from .marker_application_records import (
    MarkerRuleSectionApplication,
    marker_section_application_identity_contract,
)

_AUTHORIZED_MARKER_APPLICATION_ISSUER = object()


# ==============================================================================
#                          Marker application adapters
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedMarkerRuleSectionApplication:
    """Issuer-only marker application derived from current batch state."""

    application: MarkerRuleSectionApplication
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    current_execution: BatchCurrentExecutionAuthority
    resolved_config_sha256: str
    seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("marker-rule section application is issuer-only")


def _authority_material(
    authority: AuthorizedMarkerRuleSectionApplication,
) -> dict[str, object]:
    application = authority.application
    return {
        "authority_object_identity": id(authority),
        "application_object_identity": id(application),
        "prepared_object_identity": id(application.prepared),
        "view_store_object_identity": id(application.view_store),
        "source_authority_object_identities": [id(item) for item in authority.source_authorities],
        "source_ref_sha256s": [
            item.artifact_ref.artifact_ref_sha256 for item in authority.source_authorities
        ],
        "current_execution_object_identity": id(authority.current_execution),
        "resolved_config_sha256": authority.resolved_config_sha256,
        "prepared_artifact_sha256": application.prepared.artifact_sha256,
    }


def _source_configuration_bindings_match_current_section(
    refs: tuple[BatchArtifactRef, ...],
    current: BatchCurrentExecutionAuthority,
) -> bool:
    """Keep section-local inputs exact while admitting a pooled domain gate."""

    if len(current.configuration_bindings) != 1:
        return False
    target = current.configuration_bindings[0]
    gates = tuple(item for item in refs if item.artifact_kind == "marker_gate")
    combinations = tuple(item for item in refs if item.artifact_kind == "marker_combination")
    measurements = tuple(item for item in refs if item.artifact_kind == "measurements_result")
    modes = tuple(item for item in refs if item.artifact_kind == "phenotyping_mode")
    if len(gates) != 1 or len(combinations) != 1 or len(measurements) != 1 or not modes:
        return False
    target_gate_bindings = tuple(
        item for item in gates[0].configuration_bindings if item.sample_id == target.sample_id
    )
    return target_gate_bindings == (target,) and combinations[0].configuration_bindings == gates[0].configuration_bindings and all(
        item.configuration_bindings == current.configuration_bindings
        for item in (*measurements, *modes)
    )


def _validate_authority(
    context: BatchNodeExecutionContext,
    authority: AuthorizedMarkerRuleSectionApplication,
) -> MarkerRuleSectionApplication:
    if (
        type(context) is not BatchNodeExecutionContext
        or type(authority) is not AuthorizedMarkerRuleSectionApplication
        or authority._issuer is not _AUTHORIZED_MARKER_APPLICATION_ISSUER
        or authority.seal_sha256 != digest(_authority_material(authority))
    ):
        raise BatchArtifactError("marker application authority is invalid")
    current = validate_current_execution_authority(
        authority.current_execution, expected_context=context
    )
    authorities = validate_committed_artifact_authority_set(
        authority.source_authorities,
        expected_active_plan_sha256=current.active_plan_sha256,
    )
    refs = tuple(sorted(context.inputs, key=lambda item: item.artifact_ref_sha256))
    if (
        tuple(item.artifact_ref for item in authorities) != refs
        or {item.state_head_sha256 for item in authorities} != {current.state_head_sha256}
        or tuple(item.artifact_ref.artifact_ref_sha256 for item in authorities)
        != authority.application.source_ref_sha256s
        or not _source_configuration_bindings_match_current_section(refs, current)
        or current.configuration_bindings[0].resolved_config_sha256
        != authority.resolved_config_sha256
    ):
        raise BatchArtifactError("marker application authority is not current")
    return authority.application


def issue_authorized_marker_rule_section_application(
    *,
    context: BatchNodeExecutionContext,
    current_execution: BatchCurrentExecutionAuthority,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    resolved_sample: ResolvedBatchSample,
    authenticated_output_root: Path,
    phenotype_view_store: PhenotypeViewStore,
    source_run_id: str,
    expected_saved_state_before_sha256: str | None = None,
    expected_population_source_ref_sha256s: frozenset[str] | None = None,
    completed_failure=None,
) -> AuthorizedMarkerRuleSectionApplication:
    """Issue one application solely from exact current committed inputs."""

    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("marker issuance requires execution context")
    try:
        current = validate_current_execution_authority(current_execution, expected_context=context)
        authorities = validate_committed_artifact_authority_set(
            source_authorities,
            expected_active_plan_sha256=current.active_plan_sha256,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("marker issuance state authority is invalid") from exc
    refs = tuple(sorted(context.inputs, key=lambda item: item.artifact_ref_sha256))
    if (
        type(resolved_sample) is not ResolvedBatchSample
        or not isinstance(authenticated_output_root, Path)
        or type(phenotype_view_store) is not PhenotypeViewStore
        or context.adapter_spec.node_kind != "marker_rule_application"
        or tuple(item.artifact_ref for item in authorities) != refs
        or {item.state_head_sha256 for item in authorities} != {current.state_head_sha256}
    ):
        raise BatchArtifactError("marker application sources are not current")
    by_kind: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authorities:
        by_kind.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    if (
        set(by_kind) != {"marker_gate", "marker_combination", "measurements_result", "phenotyping_mode"}
        or len(by_kind["marker_gate"]) != 1
        or len(by_kind["marker_combination"]) != 1
        or len(by_kind["measurements_result"]) != 1
        or not by_kind["phenotyping_mode"]
    ):
        raise BatchArtifactError("marker application source kinds differ")
    measurement_ref = by_kind["measurements_result"][0].artifact_ref
    if (
        current.validation_domain_id != measurement_ref.scope.validation_domain_id
        or current.participant_ids != (str(measurement_ref.scope.sample_id),)
        or current.project_entry_ids != (str(measurement_ref.scope.project_entry_id),)
        or current.analysis_domain_bindings != measurement_ref.scope.analysis_domain_bindings
        or current.configuration_bindings != measurement_ref.configuration_bindings
        or len(current.configuration_bindings) != 1
        or current.configuration_bindings[0].sample_id != resolved_sample.sample_id
        or current.configuration_bindings[0].resolved_config_sha256
        != resolved_sample.resolved_config_sha256
        or not _source_configuration_bindings_match_current_section(refs, current)
    ):
        raise BatchArtifactError("marker application target scope differs")
    completed_evidence = None
    if completed_failure is not None:
        from mif_pipeline.orchestration.project_stage_plan import _ReopenedProjectStageFailure
        proof = context.authorization.failed_retry
        if (type(completed_failure) is not _ReopenedProjectStageFailure
                or proof is None or proof.terminal_record_sha256 != completed_failure.terminal_record_sha256
                or completed_failure.state_head_sha256 != current.state_head_sha256
                or completed_failure.context.node_id != context.node_id
                or completed_failure.context.inputs != context.inputs
                or completed_failure.completed_evidence is None):
            raise BatchArtifactError("Marker recovery is not bound to this retry")
        completed_evidence = completed_failure.completed_evidence
    try:
        application = build_marker_rule_section_application(
            gate_authority=by_kind["marker_gate"][0],
            combination_authority=by_kind["marker_combination"][0],
            measurement_authority=by_kind["measurements_result"][0],
            mode_authorities=tuple(by_kind["phenotyping_mode"]),
            resolved_sample=resolved_sample,
            authenticated_output_root=authenticated_output_root,
            phenotype_view_store=phenotype_view_store,
            source_run_id=source_run_id,
            expected_saved_state_before_sha256=expected_saved_state_before_sha256,
            expected_population_source_ref_sha256s=expected_population_source_ref_sha256s,
            completed_evidence=completed_evidence,
        )
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchArtifactError("marker application owner derivation failed") from exc
    if (
        tuple(item.artifact_ref.artifact_ref_sha256 for item in authorities)
        != application.source_ref_sha256s
    ):
        raise BatchArtifactError("derived marker application source set differs")
    result = object.__new__(AuthorizedMarkerRuleSectionApplication)
    for name, value in {
        "application": application,
        "source_authorities": authorities,
        "current_execution": current,
        "resolved_config_sha256": resolved_sample.resolved_config_sha256,
        "seal_sha256": "0" * 64,
        "_issuer": _AUTHORIZED_MARKER_APPLICATION_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "seal_sha256", digest(_authority_material(result)))
    return result


@runtime_checkable
class MarkerRuleApplicationAuthorityResolver(Protocol):
    def __call__(
        self, context: BatchNodeExecutionContext, *, saved_state_before_sha256: str
    ) -> AuthorizedMarkerRuleSectionApplication: ...


def build_marker_rule_application_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    authority: AuthorizedMarkerRuleSectionApplication,
    *, acceptance_context: BatchNodeExecutionContext | None = None,
) -> BatchNodeExecutionResult:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError("marker application requires Task-10 evidence")
    context = evidence.context if acceptance_context is None else acceptance_context
    if acceptance_context is not None and (
            context.authorization.failed_retry is None
            or context.node_id != evidence.context.node_id
            or context.inputs != evidence.context.inputs
            or context.authorization.retry_ordinal != evidence.context.authorization.retry_ordinal + 1):
        raise BatchArtifactError("Marker acceptance does not continue its failed native attempt")
    application = _validate_authority(context, authority)
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        context.adapter_spec.node_kind != "marker_rule_application"
        or authorized.terminal_stage_id != "marker_rule_application"
        or authorized.wire_stage_id != "phenotyping"
        or authorized.request_codec_id
        != QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value
        or authorized.request_codec_version != "1"
        or authorized.sample_id != application.sample_id
        or authorized.project_entry_id != application.project_entry_id
        or authorized.slide_id != application.slide_id
        or authorized.saved_state_before_sha256 != application.expected_saved_state_before_sha256
        or tuple(sorted(authorized.prerequisite_artifact_ref_sha256s))
        != application.source_ref_sha256s
        or tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        != application.source_ref_sha256s
    ):
        raise BatchArtifactError("marker Task-10 target differs from application")
    result = evidence.wire_stage_result
    if result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    outputs = result.outputs
    required = {
        "component_descriptor",
        "cell_count",
        "section_id",
        "cell_to_domain_membership_sha256",
        "cell_ids_sha256",
        "selected_display_view",
        "phenotype_generation_id",
        "phenotype_view_artifact",
        "materialized_display_classes",
        "coordinate_system",
    }
    if (
        set(outputs) != required
        or result.stage != "phenotyping"
        or result.slide_id != application.slide_id
        or result.generation_id != application.tissue_generation_id
        or outputs["cell_count"] != application.prepared.payload["cell_count"]
        or outputs["cell_ids_sha256"] != application.cell_ids_sha256
        or outputs["cell_to_domain_membership_sha256"] != application.cell_to_domain_membership_sha256
        or outputs["section_id"] != application.section_id
        or outputs["selected_display_view"] != "marker_rules"
        or outputs["phenotype_generation_id"] != application.phenotype_generation_id
        or outputs["phenotype_view_artifact"] != application.prepared.to_request_mapping()
        or isinstance(outputs["phenotype_view_artifact"].get("cell_count"), bool)
        or outputs["materialized_display_classes"] is not True
        or outputs["coordinate_system"] != "global_level0"
        or observation.saved_state_after_sha256 is None
        or observation.saved_state_disposition == "unattestable"
    ):
        raise BatchArtifactError("native marker application result is incomplete or stale")
    manifest = evidence.generation_manifest
    if (
        not isinstance(manifest, Mapping)
        or set(manifest)
        != {
            "schema_version",
            "invocation_id",
            "run_id",
            "stage",
            "slide_id",
            "generation_ids",
            "generations",
            "object_outputs",
        }
        or (
            manifest["schema_version"] != "roi-generation-manifest/v1"
            or manifest["invocation_id"] != authorized.attempt_id
            or manifest["run_id"] != authorized.pipeline_run_id
            or manifest["stage"] != "phenotyping"
            or manifest["slide_id"] != application.slide_id
            or tuple(manifest["generation_ids"]) != (application.tissue_generation_id,)
            or tuple(manifest["generations"]) != ()
            or canonical_bytes(manifest["object_outputs"]) != canonical_bytes(result.outputs)
        )
    ):
        raise BatchArtifactError("native marker generation manifest differs")
    identity_base = {
        "component_descriptor": dict(freeze_component_reference(outputs["component_descriptor"])),
        "sample_id": application.sample_id,
        "project_entry_id": application.project_entry_id,
        "slide_id": application.slide_id,
        "canonical_image_id": application.canonical_image_id,
        "canonical_image_sha256": application.canonical_image_sha256,
        "section_id": application.section_id,
        "tissue_generation_id": application.tissue_generation_id,
        "segmentation_population_id": application.segmentation_population_id,
        "measurement_generation_id": application.measurement_generation_id,
        "validation_domain_id": application.validation_domain_id,
        "phenotype_generation_id": application.phenotype_generation_id,
        "cell_ids_sha256": application.cell_ids_sha256,
        "cell_to_domain_membership_sha256": application.cell_to_domain_membership_sha256,
        "phenotype_view_artifact_sha256": application.prepared.artifact_sha256,
        "phenotype_view_lineage_sha256": application.prepared.lineage_sha256,
        "source_ref_sha256s": list(application.source_ref_sha256s),
        "source_ref_set_sha256": artifact_ref_set_sha256(tuple(context.inputs)),
        "saved_state_before_sha256": authorized.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "node_id": context.node_id,
        "attempt_id": context.authorization.attempt_id,
        "attempt_authorization_sha256": context.authorization.authorization_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    measurement_refs = tuple(
        item for item in context.inputs if item.artifact_kind == "measurements_result"
    )
    if len(measurement_refs) != 1:
        raise BatchArtifactError("marker application requires one measurement owner")
    measurement = measurement_refs[0]
    contract = marker_section_application_identity_contract()

    def build_artifact(inventory_sha256: str) -> BatchArtifactRef:
        identity = {
            **identity_base,
            "phenotype_view_inventory_sha256": inventory_sha256,
        }
        return BatchArtifactRef(
            artifact_id="marker-section-application-" + digest(identity)[:48],
            artifact_kind="marker_phenotype_result",
            schema_name="mif-batch-marker-gate-section-application",
            schema_version=1,
            origin=ArtifactOrigin.INTERNAL_NODE,
            producer_node_id=context.node_id,
            producer_attempt_id=context.authorization.attempt_id,
            producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
            external_declaration_sha256=None,
            external_validation_receipt_sha256=None,
            locator=None,
            sha256=None,
            byte_count=None,
            scope=ArtifactScope(
                application.validation_domain_id,
                application.sample_id,
                application.slide_id,
                application.project_entry_id,
                measurement.scope.analysis_domain_bindings,
            ),
            owning_identity=identity,
            identity_contract=contract,
            owning_identity_contract_sha256=contract.contract_sha256,
            producer_version="mif-pipeline-batch-marker-section-application/v1",
            runtime_versions={
                "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
                "qupath": measurement.runtime_versions["qupath"],
            },
            compatibility_signature_sha256=context.compatibility_signature_sha256,
            configuration_bindings=measurement.configuration_bindings,
        )

    # Validate every result-publication field before advancing the view head.
    build_artifact("0" * 64)
    view_store = application.view_store
    view_store.mark_component_applied(application.prepared, outputs["component_descriptor"])
    inventory = view_store.commit(application.prepared)
    artifact = build_artifact(inventory.inventory_sha256)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def build_marker_rule_application_project_stage_codec(
    spec: StageAdapterSpec,
    *,
    authority_resolver: MarkerRuleApplicationAuthorityResolver,
) -> ProjectStageCodec:
    if spec.node_kind != "marker_rule_application":
        raise BatchArtifactError("marker codec requires marker application spec")
    if spec.output_identity_contracts != (marker_section_application_identity_contract(),):
        raise BatchArtifactError("marker codec output owner differs")
    if not isinstance(authority_resolver, MarkerRuleApplicationAuthorityResolver):
        raise BatchArtifactError("marker codec requires state authority resolver")

    def resolve(context, saved_state_before_sha256):
        authority = authority_resolver(
            context, saved_state_before_sha256=saved_state_before_sha256
        )
        _validate_authority(context, authority)
        return authority

    def projector(context, intent: ProjectStageTargetIntent):
        authority = resolve(context, intent.saved_state_before_sha256)
        application = authority.application
        if (
            intent.sample_id != application.sample_id
            or intent.project_entry_id != application.project_entry_id
            or intent.slide_id != application.slide_id
            or intent.saved_state_before_sha256 != application.expected_saved_state_before_sha256
            or intent.resolved_sample.resolved_config_sha256 != authority.resolved_config_sha256
        ):
            raise BatchArtifactError("marker target differs from application")
        return build_marker_rule_application_invocation(
            application,
            config=intent.resolved_sample.config,
            run_id=intent.pipeline_run_id,
        )

    def validator(evidence):
        context = getattr(evidence, "context", None)
        if type(context) is not BatchNodeExecutionContext:
            raise BatchArtifactError("marker result lacks execution context")
        authority = resolve(context, evidence.authorized.saved_state_before_sha256)
        return build_marker_rule_application_execution_result(evidence, authority)

    return build_project_current_image_stage_codec(
        spec, invocation_projector=projector, result_validator=validator
    )


__all__ = [
    "AuthorizedMarkerRuleSectionApplication",
    "MarkerRuleApplicationAuthorityResolver",
    "build_marker_rule_application_execution_result",
    "build_marker_rule_application_project_stage_codec",
    "issue_authorized_marker_rule_section_application",
]
