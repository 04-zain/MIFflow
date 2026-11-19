"""Direct final non-spatial export from authenticated authorities.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ..batch_phenotyping_contracts import semantic_profile_identity_contract

from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat

from mif_pipeline.cell_analysis import (
    FinalizedSecondaryDomainEvidence,
    NoSecondaryDomainCompletionEvidence,
    PrimaryExcludedDomainCompletionEvidence,
)
from mif_pipeline.cell_analysis.spatial_handoff import FinalROIState
from mif_pipeline.pipeline_core import StageResult
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.models import ROIRecord

from ..accepted_analysis_domain_records import (
    accepted_analysis_domain_identity_contract,
)
from ..batch_analysis_domain_artifacts import (
    ValidatedAcceptedAnalysisDomainLiveAuthority,
    reopen_committed_accepted_analysis_domain_live_authority,
)
from ..batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    BatchAnalysisPreflightEvidence,
    analysis_preflight_identity_contract,
    validate_analysis_frame_artifact_ref,
)
from ..batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchAttemptAuthorization,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
    artifact_ref_set_sha256,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ..batch_dag import ResourceKind
from ..batch_manifest import ResolvedBatchSample
from ..batch_measurement_artifacts import (
    decode_committed_measurements_result,
    measurements_result_identity_contract,
)
from ..batch_stage_adapters import (
    BatchRawNodeOutcome,
    compose_single_output_direct_stage_outcome,
)
from ..batch_state import BatchStateStore
from ..batch_tissue_artifacts import (
    ValidatedAcceptedTissueLiveAuthority,
    _validate_accepted_tissue_live_authority,
    tissue_acceptance_result_identity_contract,
)
from ..exports import (
    ExportError,
    OutputArtifact,
    write_roi_table,
    write_tissue_generation,
)
from ..phenotype_views import (
    PhenotypeViewGeneration,
    PhenotypeViewInventory,
    PhenotypeViewStore,
)
from ..project_stage_plan import ProjectStageCommittedExchangeReopener
from ..provenance import AnalysisProvenance, write_analysis_provenance
from ._common import DEFAULT_MAXIMUM_INPUT_BYTES, digest, plain_json
from .application_codec import (
    cluster_label_import_result_identity_contract,
    decode_committed_semantic_raw_inventory,
)
from .application_records import (
    ValidatedSemanticRawInventoryProjection,
    phenotype_hierarchy_attestation_identity_contract,
    semantic_raw_inventory_identity_contract,
)
from .handoff_execution import (
    ValidatedPhenotypeHandoffLiveAuthority,
    reopen_committed_phenotype_handoff_live_authority,
    spatial_cell_handoff_identity_contract,
)
from .marker_application_records import marker_section_application_identity_contract
from .semantic_applicable import (
    ValidatedSemanticEvaluationResult,
    ValidatedSemanticEvidenceResult,
    decode_committed_semantic_evaluation_result,
    decode_committed_semantic_evidence_result,
    semantic_evaluation_result_identity_contract,
    semantic_evidence_result_identity_contract,
    semantic_finalization_result_identity_contract,
)
from .semantic_branch import bind_committed_semantic_domain_branch
from .semantic_completion import no_secondary_completion_identity_contract
from .semantic_records import (
    ValidatedPhenotypeApplicationProjection,
    normalize_committed_phenotype_application,
)
from .supervised_records import supervised_phenotype_result_identity_contract

_NODE_KIND = "final_non_spatial_export"
_PRODUCER_VERSION = "mif-pipeline-batch-final-non-spatial-export/v1"
_ZERO_SHA256 = "0" * 64
_TEMPLATE_SCOPE = ArtifactScope(None, None, None, None, ())
_TEMPLATE_BINDING = ArtifactConfigurationBinding("template", _ZERO_SHA256)

FINAL_EXPORT_IDENTITY_FIELDS = (
    "sample_id",
    "project_entry_id",
    "slide_id",
    "resolved_config_sha256",
    "spatial_frame_sha256",
    "domain_set_sha256",
    "spatial_handoff_ref_sha256",
    "domain_result_inventory_sha256",
    "source_ref_set_sha256",
    "batch_output_index.logical_name",
    "batch_output_index.schema_version",
    "batch_output_index.media_type",
    "batch_output_index.record_count",
    "batch_output_index.sha256",
    "batch_output_index_sha256",
    "provenance_artifact.path",
    "provenance_artifact.schema_version",
    "provenance_artifact.media_type",
    "provenance_artifact.record_count",
    "provenance_artifact.sha256",
    "node_id",
    "attempt_id",
    "attempt_authorization_sha256",
)


# ==============================================================================
#                                Export execution
# ==============================================================================


def _identity_template(fields: tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in fields:
        cursor = result
        parts = field.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
        cursor[parts[-1]] = _ZERO_SHA256 if field.endswith("_sha256") else "template"
    return result


def _requirement(
    contract: ArtifactIdentityContract,
    multiplicity: ArtifactMultiplicity,
) -> StageArtifactRequirement:
    return StageArtifactRequirement(
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=_identity_template(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="mif-pipeline-task13f-owner/v1",
        runtime_versions={name: "template" for name in contract.required_runtime_names},
        configuration_bindings=(_TEMPLATE_BINDING,),
        multiplicity=multiplicity,
    )


def final_non_spatial_export_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-final-non-spatial-export-identity/v1",
        artifact_kind="final_non_spatial_export_result",
        schema_name="roi-analysis-provenance",
        schema_version=2,
        required_fields=FINAL_EXPORT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _verify_result_descriptor_bytes(artifact: BatchArtifactRef) -> None:
    """Hash a committed descriptor with bounded memory and an exact read limit."""
    path, expected, expected_hash = artifact.locator, artifact.byte_count, artifact.sha256
    if path is None or expected is None or expected_hash is None or not path.is_absolute():
        raise BatchArtifactError("results descriptor lacks absolute byte identity")
    if expected > DEFAULT_MAXIMUM_INPUT_BYTES:
        raise BatchArtifactError("results descriptor exceeds its read bound")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        if path.is_symlink():
            raise BatchArtifactError("results descriptor cannot be a symlink")
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size != expected:
                raise BatchArtifactError("results descriptor file type or byte count differs")
            digest_value = hashlib.sha256()
            count = 0
            while chunk := stream.read(min(1024 * 1024, expected - count + 1)):
                count += len(chunk)
                if count > expected:
                    raise BatchArtifactError("results descriptor byte count differs")
                digest_value.update(chunk)
    except OSError as exc:
        raise BatchArtifactError("results descriptor bytes cannot be read") from exc
    if count != expected or digest_value.hexdigest() != expected_hash:
        raise BatchArtifactError("results descriptor checksum or byte count differs")


def project_committed_final_export_for_results(
    authority: BatchCommittedArtifactAuthority,
    *,
    spatial_handoff_authority: BatchCommittedArtifactAuthority,
    resolved_sample: ResolvedBatchSample,
    state_store: BatchStateStore,
) -> tuple[BatchArtifactRef, BatchArtifactRef, str]:
    """Read completed publication evidence, never fresh scientific authority.

    The committed final owner validated scientific inputs before publication.
    Display verifies only its exact provenance and spatial pointer bytes. A real
    successor must reopen and validate its full inputs at execution time.
    """
    if type(resolved_sample) is not ResolvedBatchSample or type(state_store) is not BatchStateStore:
        raise BatchArtifactError("results projection requires exact sample and state owners")
    validate_committed_artifact_authority_set((authority, spatial_handoff_authority))
    final = authority.artifact_ref
    handoff = spatial_handoff_authority.artifact_ref
    binding = (
        ArtifactConfigurationBinding(
            resolved_sample.sample_id, resolved_sample.resolved_config_sha256
        ),
    )
    for owner, contract, kind in (
        (authority, final_non_spatial_export_result_identity_contract(), _NODE_KIND),
        (spatial_handoff_authority, spatial_cell_handoff_identity_contract(), "phenotype_handoff"),
    ):
        ref = owner.artifact_ref
        identity = ref.owning_identity
        if (
            ref.identity_contract != contract
            or ref.owning_identity_contract_sha256 != contract.contract_sha256
            or owner.producer_node_kind != kind
            or ref.scope.sample_id != resolved_sample.sample_id
            or ref.scope.slide_id != resolved_sample.slide_id
            or ref.scope.validation_domain_id != resolved_sample.validation_domain_id
            or ref.scope.project_entry_id is None
            or ref.configuration_bindings != binding
            or identity["sample_id"] != ref.scope.sample_id
            or identity["slide_id"] != ref.scope.slide_id
            or identity["project_entry_id"] != ref.scope.project_entry_id
            or identity["node_id"] != ref.producer_node_id
            or identity["attempt_id"] != ref.producer_attempt_id
            or identity["attempt_authorization_sha256"] != ref.producer_attempt_authorization_sha256
        ):
            raise BatchArtifactError("results owner identity or current sample scope differs")
        authorization = state_store.load_authorization(ref.producer_attempt_authorization_sha256)
        if (
            type(authorization) is not BatchAttemptAuthorization
            or authorization.authorization_sha256 != ref.producer_attempt_authorization_sha256
            or authorization.node_id != ref.producer_node_id
            or authorization.attempt_id != ref.producer_attempt_id
            or authorization.input_artifact_set_sha256 != identity["source_ref_set_sha256"]
        ):
            raise BatchArtifactError(
                "results owner source set differs from its producer authorization"
            )
    if (
        final.scope != handoff.scope
        or final.owning_identity["resolved_config_sha256"] != resolved_sample.resolved_config_sha256
        or final.owning_identity["spatial_handoff_ref_sha256"] != handoff.artifact_ref_sha256
        or any(
            final.owning_identity[field] != handoff.owning_identity[field]
            for field in (
                "spatial_frame_sha256",
                "domain_set_sha256",
                "domain_result_inventory_sha256",
            )
        )
        or authority.execution_exchange_sha256 is not None
        or spatial_handoff_authority.execution_exchange_sha256
        != handoff.owning_identity["execution_exchange_sha256"]
    ):
        raise BatchArtifactError("results final and spatial owner bindings differ")
    try:
        provenance = OutputArtifact.from_mapping(final.owning_identity["provenance_artifact"])
    except (ExportError, TypeError, ValueError) as exc:
        raise BatchArtifactError("results provenance descriptor is invalid") from exc
    if (
        provenance.path != final.locator
        or provenance.sha256 != final.sha256
        or provenance.schema_version != f"{final.schema_name}/v{final.schema_version}"
        or provenance.media_type != "application/json"
        or provenance.record_count != 0
        or handoff.owning_identity["descriptor"]["sha256"] != handoff.sha256
        or handoff.owning_identity["descriptor"]["byte_count"] != handoff.byte_count
    ):
        raise BatchArtifactError(
            "results publication descriptor differs from its committed reference"
        )
    _verify_result_descriptor_bytes(final)
    _verify_result_descriptor_bytes(handoff)
    return final, handoff, provenance.sha256


def build_final_non_spatial_export_stage_spec() -> StageAdapterSpec:
    required = (
        (analysis_preflight_identity_contract(), ArtifactMultiplicity.SINGLE),
        (tissue_acceptance_result_identity_contract(), ArtifactMultiplicity.SINGLE),
        (accepted_analysis_domain_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (spatial_cell_handoff_identity_contract(), ArtifactMultiplicity.SINGLE),
    )
    optional = (
        (measurements_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_raw_inventory_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (marker_section_application_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (supervised_phenotype_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (cluster_label_import_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (phenotype_hierarchy_attestation_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_profile_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_evidence_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_evaluation_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (no_secondary_completion_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_finalization_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
    )
    output = final_non_spatial_export_result_identity_contract()
    return StageAdapterSpec(
        node_kind=_NODE_KIND,
        node_version="1",
        required_inputs=tuple(_requirement(*item) for item in required),
        optional_inputs=tuple(_requirement(*item) for item in optional),
        declared_outputs=(output.artifact_kind,),
        output_identity_contracts=(output,),
        compatibility_signature_id="mif-batch-final-non-spatial-export-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="mif-batch-final-non-spatial-export-equivalence/v1",
            byte_deterministic=False,
            canonical_identity_paths=tuple(
                (output.artifact_kind, field) for field in FINAL_EXPORT_IDENTITY_FIELDS[:9]
            ),
            stable_inventory_paths=tuple(
                (output.artifact_kind, field) for field in FINAL_EXPORT_IDENTITY_FIELDS[9:15]
            ),
            scientific_value_paths=tuple(
                (output.artifact_kind, field) for field in FINAL_EXPORT_IDENTITY_FIELDS[16:20]
            ),
            qc_evidence_paths=tuple(
                (output.artifact_kind, field) for field in FINAL_EXPORT_IDENTITY_FIELDS[20:]
            ),
        ),
        dispatch_mode=DispatchMode.SINGLE,
        resource_kind=ResourceKind.SCIENTIFIC_COMPUTE.value,
    )


def _by_kind(
    values: Sequence[BatchCommittedArtifactAuthority],
) -> dict[str, tuple[BatchCommittedArtifactAuthority, ...]]:
    grouped: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for item in values:
        grouped.setdefault(item.artifact_ref.artifact_kind, []).append(item)
    return {
        key: tuple(sorted(items, key=lambda value: value.artifact_ref.artifact_ref_sha256))
        for key, items in grouped.items()
    }


def _single(
    grouped: Mapping[str, tuple[BatchCommittedArtifactAuthority, ...]], kind: str
) -> BatchCommittedArtifactAuthority:
    values = grouped.get(kind, ())
    if len(values) != 1:
        raise BatchArtifactError(f"final export requires one exact {kind}")
    return values[0]


def _domain_id(authority: BatchCommittedArtifactAuthority) -> str:
    bindings = authority.artifact_ref.scope.analysis_domain_bindings
    if len(bindings) != 1:
        raise BatchArtifactError("final export domain input scope is not singular")
    return bindings[0].analysis_domain_id


def _one_per_domain(
    values: Sequence[BatchCommittedArtifactAuthority], *, label: str
) -> dict[str, BatchCommittedArtifactAuthority]:
    result: dict[str, BatchCommittedArtifactAuthority] = {}
    for authority in values:
        domain_id = _domain_id(authority)
        if domain_id in result:
            raise BatchArtifactError(f"final export {label} domain is ambiguous")
        result[domain_id] = authority
    return result


def _selected_phenotypes(
    grouped: Mapping[str, tuple[BatchCommittedArtifactAuthority, ...]],
) -> tuple[ValidatedPhenotypeApplicationProjection, ...]:
    direct = (
        *grouped.get("marker_phenotype_result", ()),
        *grouped.get("supervised_phenotype_result", ()),
    )
    clusters = grouped.get("cluster_label_import_result", ())
    attestations = grouped.get("phenotype_view_attestation", ())
    values: list[ValidatedPhenotypeApplicationProjection] = [
        normalize_committed_phenotype_application((item,)) for item in direct
    ]
    if clusters or attestations:
        if len(clusters) != len(attestations):
            raise BatchArtifactError("final export cluster source coverage differs")
        attestations_by_section = {
            str(item.artifact_ref.owning_identity["section_id"]): item for item in attestations
        }
        for cluster in clusters:
            section_id = str(cluster.artifact_ref.owning_identity["section_id"])
            attestation = attestations_by_section.get(section_id)
            if attestation is None:
                raise BatchArtifactError("final export cluster attestation is missing")
            values.append(normalize_committed_phenotype_application((cluster, attestation)))
    result = tuple(sorted(values, key=lambda item: item.section_id))
    if len({item.section_id for item in result}) != len(result):
        raise BatchArtifactError("final export selected phenotype sections overlap")
    return result


def _load_selected_phenotype_generation(
    *,
    store: PhenotypeViewStore,
    handoff_live: ValidatedPhenotypeHandoffLiveAuthority,
    slide_id: str,
    section_inventories: Mapping[str, Mapping[str, object]],
) -> tuple[PhenotypeViewInventory, PhenotypeViewGeneration]:
    handoff = handoff_live.handoff
    tissue_generations = {
        item.member.tissue_generation_id for item in handoff.domain_results.values()
    }
    if len(tissue_generations) != 1:
        raise BatchArtifactError("final export requires one phenotype tissue generation")
    inventory = store.load(
        slide_id=slide_id,
        canonical_image_sha256=handoff.spatial_frame.canonical_image_sha256,
        tissue_generation_id=next(iter(tissue_generations)),
        expected_inventory_sha256=handoff.phenotype_inventory_sha256,
    )
    selected = store.select(
        inventory,
        mode=handoff.selected_phenotype_view,
        generation_id=handoff.phenotype_generation_id,
        expected_section_inventories=section_inventories,
    )
    return inventory, selected


def _segmentation_stage_results(handoff: object, slide_id: str) -> tuple[Mapping[str, object], ...]:
    by_section: dict[str, list[object]] = {}
    for result in handoff.domain_results.values():
        by_section.setdefault(result.member.section_id, []).append(result)
    stages = []
    for section_id in sorted(by_section):
        results = sorted(by_section[section_id], key=lambda item: item.member.analysis_domain_id)
        first = results[0]
        fixed = (
            first.segmentation_population_id,
            first.segmentation_canonical_image_id,
            first.segmentation_canonical_image_sha256,
            first.segmentation_domain_set_sha256,
            first.segmentation_section_population_sha256,
            first.segmentation_domain_sha256,
            first.segmentation_domain_cell_mapping_sha256,
        )
        if any(
            (
                item.segmentation_population_id,
                item.segmentation_canonical_image_id,
                item.segmentation_canonical_image_sha256,
                item.segmentation_domain_set_sha256,
                item.segmentation_section_population_sha256,
                item.segmentation_domain_sha256,
                item.segmentation_domain_cell_mapping_sha256,
            )
            != fixed
            for item in results
        ):
            raise BatchArtifactError("final export segmentation section lineage differs")
        stages.append(
            StageResult.completed(
                stage="segmentation",
                slide_id=slide_id,
                outputs={
                    "segmentation_skipped_excluded_domains": all(isinstance(
                        item.finalization, PrimaryExcludedDomainCompletionEvidence) for item in results),
                    "segmentation_population": {
                        "segmentation_population_id": first.segmentation_population_id,
                        "canonical_image_id": first.segmentation_canonical_image_id,
                        "canonical_image_sha256": first.segmentation_canonical_image_sha256,
                        "section_id": section_id,
                        "domain_set_sha256": first.segmentation_domain_set_sha256,
                        "section_population_sha256": first.segmentation_section_population_sha256,
                        "segmentation_domain_sha256": first.segmentation_domain_sha256,
                        "domain_cell_mapping_sha256": first.segmentation_domain_cell_mapping_sha256,
                        "domain_cell_inventories": [
                            {
                                "analysis_domain_id": item.member.analysis_domain_id,
                                "cell_count": item.segmented_inventory.count,
                                "cell_ids_sha256": item.segmented_inventory.cell_ids_sha256,
                            }
                            for item in results
                        ],
                    }
                },
            ).to_mapping()
        )
    return tuple(stages)


def _phenotype_stage_results(
    *,
    inventory: PhenotypeViewInventory | None,
    selected: PhenotypeViewGeneration | None,
    slide_id: str,
) -> tuple[Mapping[str, object], ...]:
    if inventory is None and selected is None:
        return ()
    if inventory is None or selected is None:
        raise BatchArtifactError("performed phenotype generation is incomplete")
    generations = {
        mode: tuple(
            {
                "generation_id": generation.generation_id,
                "artifact_sha256": generation.artifact_sha256,
            }
            for generation in values
        )
        for mode, values in inventory.views.items()
    }
    return tuple(
        StageResult.completed(
            stage="phenotyping",
            slide_id=slide_id,
            outputs={
                "phenotype_generations": generations,
                "selected_phenotype_view": selected.mode,
                "selected_phenotype_generation_id": selected.generation_id,
                "phenotype_inventory_sha256": inventory.inventory_sha256,
                "phenotype_view_artifact": {
                    "schema_version": "phenotype-view-reference/v1",
                    "mode": selected.mode,
                    "slide_id": inventory.slide_id,
                    "canonical_image_sha256": inventory.canonical_image_sha256,
                    "tissue_generation_id": inventory.tissue_generation_id,
                    "phenotype_generation_id": selected.generation_id,
                    "section_id": section.section_id,
                    "source_run_id": section.source_run_id,
                    "artifact_path": str(section.artifact_path),
                    "artifact_sha256": section.artifact_sha256,
                    "lineage_sha256": section.lineage_sha256,
                    "cell_ids_sha256": section.cell_ids_sha256,
                    "cell_count": section.cell_count,
                    "cell_to_domain_membership_sha256": section.cell_to_domain_membership_sha256,
                    "assignments_sha256": section.assignments_sha256,
                },
            },
        ).to_mapping()
        for section in selected.sections
    )


def _semantic_stage_results(
    *,
    handoff: object,
    raw_by_domain: Mapping[str, ValidatedSemanticRawInventoryProjection],
    evidence_by_domain: Mapping[str, ValidatedSemanticEvidenceResult],
    evaluation_by_domain: Mapping[str, ValidatedSemanticEvaluationResult],
    analysis_config: Mapping[str, object],
    slide_id: str,
) -> tuple[Mapping[str, object], ...]:
    stages = []
    for domain_id in sorted(handoff.domain_results):
        result = handoff.domain_results[domain_id]
        raw = raw_by_domain.get(domain_id)
        if isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence):
            if raw is not None or domain_id in evidence_by_domain or domain_id in evaluation_by_domain:
                raise BatchArtifactError("excluded domain cannot claim performed semantic analysis")
            continue
        if raw is None or raw.snapshot.raw_export_sha256 != result.semantic_raw_input_sha256:
            raise BatchArtifactError("final export semantic raw authority differs")
        outputs: dict[str, object] = {
            "analysis_domain_id": result.member.analysis_domain_id,
            "section_id": result.member.section_id,
            "analysis_config_sha256": result.analysis_config_sha256,
            "raw_input_descriptor": {
                "path": str(raw.artifact_ref.locator),
                "sha256": raw.snapshot.raw_export_sha256,
            },
            "domain_finalization": result.finalization.to_mapping(),
        }
        if isinstance(result.finalization, FinalizedSecondaryDomainEvidence):
            evidence = evidence_by_domain.get(domain_id)
            evaluation = evaluation_by_domain.get(domain_id)
            if (
                evidence is None
                or evaluation is None
                or evaluation.result_artifact != result.finalization.semantic_result_artifact
                or evaluation.semantic_qc.get("status") != "passed"
            ):
                raise BatchArtifactError("final export applicable semantic authority differs")
            outputs.update(
                {
                    "analysis_domain_revision": result.member.analysis_domain_revision,
                    "analysis_domain_revision_sha256": result.member.analysis_domain_revision_sha256,
                    "domain_set_sha256": handoff.domain_set.domain_set_sha256,
                    "spatial_frame_sha256": handoff.spatial_frame.spatial_frame_sha256,
                    "domain_status": result.domain_status,
                    "coordinate_system": "global_level0",
                    "analysis_config": plain_json(analysis_config),
                    "semantic_candidate_membership": {
                        "path": str(evidence.membership_artifact_path),
                        "sha256": evidence.artifact_ref.sha256,
                    },
                    "semantic_result_artifact": evaluation.result_artifact.to_mapping(),
                    "semantic_result_set_sha256": (
                        evaluation.result_artifact.semantic_result_set_sha256
                    ),
                    "semantic_finalization": plain_json(result.finalization.finalization_receipt),
                    "final_secondary_count_decision": plain_json(
                        result.finalization.count_decision
                    ),
                    "manual_selection_evidence": [
                        item.to_mapping() for item in result.finalization.manual_selections
                    ],
                    "final_object_dispositions": [
                        item.to_mapping() for item in result.finalization.dispositions
                    ],
                    "active_secondary_roi_ids": list(
                        result.finalization.finalization_receipt["active_secondary_roi_ids"]
                    ),
                    "validation_status": "validated",
                    "qc_passed": True,
                    "semantic_qc": plain_json(evaluation.semantic_qc),
                    "review_status": evaluation.review_status,
                    "review_action": evaluation.review_action,
                    "semantic_region_decisions": [
                        item.to_mapping() for item in evaluation.decisions
                    ],
                }
            )
        elif domain_id in evidence_by_domain or domain_id in evaluation_by_domain:
            raise BatchArtifactError("final export no-secondary domain has applicable evidence")
        stages.append(
            StageResult.completed(
                stage="semantic_validation", slide_id=slide_id, outputs=outputs
            ).to_mapping()
        )
    return tuple(stages)


def _analysis_provenance_from_batch_authorities(
    *,
    resolved_sample: ResolvedBatchSample,
    frame: BatchAnalysisPreflightEvidence,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    semantic_raw: tuple[ValidatedSemanticRawInventoryProjection, ...],
    handoff_live: ValidatedPhenotypeHandoffLiveAuthority,
    phenotype_inventory: PhenotypeViewInventory | None,
    selected_phenotype: PhenotypeViewGeneration | None,
    semantic_evidence_by_domain: Mapping[str, ValidatedSemanticEvidenceResult],
    semantic_evaluation_by_domain: Mapping[str, ValidatedSemanticEvaluationResult],
    pipeline_version: str,
) -> AnalysisProvenance:
    handoff = handoff_live.handoff
    raw_by_domain = {
        item.artifact_ref.scope.analysis_domain_bindings[0].analysis_domain_id: item
        for item in semantic_raw
    }
    domain_outputs = {
        "schema_version": "analysis-domain-candidate-manifest/v1",
        "selection_status": "accepted",
        "canonical_image_id": handoff.spatial_frame.canonical_image_id,
        "canonical_image_sha256": handoff.spatial_frame.canonical_image_sha256,
        "project_entry_id": accepted_domains.committed.project_entry_id,
        "tissue_generation_id": accepted_domains.committed.tissue_generation_id,
        "analysis_domain_manifests": [item.to_mapping() for item in accepted_domains.manifests],
        "overlap_evidence": [item.to_mapping() for item in accepted_domains.overlap_evidence],
        "domain_set_sha256": accepted_domains.domain_set.domain_set_sha256,
        "saved_closed_acknowledged": True,
        "selected_annotations_locked": True,
    }
    handoff_outputs = {
        "coordinate_system": "global_level0_pixels",
        "spatial_frame_sha256": handoff.spatial_frame.spatial_frame_sha256,
        "domain_set_sha256": handoff.domain_set.domain_set_sha256,
        "phenotype_generation_id": handoff.phenotype_generation_id,
        "selected_phenotype_view": handoff.selected_phenotype_view,
        "selected_phenotype_generation_id": handoff.phenotype_generation_id,
        "phenotype_inventory_sha256": handoff.phenotype_inventory_sha256,
        "cell_count": handoff.cell_count,
        "segmented_count": sum(
            item.segmented_inventory.count for item in handoff.domain_results.values()
        ),
        "phenotyped_count": sum(
            item.phenotyped_inventory.count for item in handoff.domain_results.values()
            if item.phenotyped_inventory is not None
        ),
        "exported_count": sum(
            item.exported_inventory.count for item in handoff.domain_results.values()
        ),
        "active_domain_ids": sorted(handoff.by_domain),
        "excluded_domain_ids": sorted(set(handoff.domain_results) - set(handoff.by_domain)),
        "excluded_section_completions": {
            domain_id: result.finalization.to_mapping()
            for domain_id, result in handoff.domain_results.items()
            if isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence)
        },
        "cell_analysis_status_by_section": {
            result.member.section_id: ("not_performed_primary_exclusion" if isinstance(
                result.finalization, PrimaryExcludedDomainCompletionEvidence) else "completed")
            for result in handoff.domain_results.values()
        },
        "domain_result_sha256s": {
            domain_id: handoff.domain_results[domain_id].domain_result_sha256
            for domain_id in sorted(handoff.domain_results)
        },
        "tissue_group_count": len(handoff.by_tissue),
        "descriptor": handoff_live.artifacts.descriptor.to_mapping(),
        "cells": handoff_live.artifacts.cells.to_mapping(),
        "raw_export_sha256": handoff_live.global_raw_descriptor["sha256"],
        "raw_input_descriptor": dict(handoff_live.global_raw_descriptor),
        "spatial_statistics_performed": False,
    }
    stage_results = (
        StageResult.completed(
            stage="preflight",
            slide_id=resolved_sample.slide_id,
            outputs={
                "spatial_frame": frame.spatial_frame.to_mapping(),
                "level0_size_x_px": frame.spatial_frame.level0_size_x_px,
                "level0_size_y_px": frame.spatial_frame.level0_size_y_px,
            },
        ).to_mapping(),
        StageResult.completed(
            stage="tissue_acceptance",
            slide_id=resolved_sample.slide_id,
            outputs={
                "accepted_generation_id": accepted_tissue.generation.generation_id,
                "accepted_section_ids": [
                    item.section.section_id for item in accepted_tissue.sections
                ],
            },
        ).to_mapping(),
        StageResult.completed(
            stage="analysis_domain_selection",
            slide_id=resolved_sample.slide_id,
            outputs=domain_outputs,
        ).to_mapping(),
        *_segmentation_stage_results(handoff, resolved_sample.slide_id),
        *_phenotype_stage_results(
            inventory=phenotype_inventory,
            selected=selected_phenotype,
            slide_id=resolved_sample.slide_id,
        ),
        *_semantic_stage_results(
            handoff=handoff,
            raw_by_domain=raw_by_domain,
            evidence_by_domain=semantic_evidence_by_domain,
            evaluation_by_domain=semantic_evaluation_by_domain,
            analysis_config=resolved_sample.config.to_mapping(),
            slide_id=resolved_sample.slide_id,
        ),
        StageResult.completed(
            stage="phenotype_handoff",
            slide_id=resolved_sample.slide_id,
            outputs=handoff_outputs,
        ).to_mapping(),
    )
    return replace(
        AnalysisProvenance.minimal(
            slide_id=resolved_sample.slide_id,
            canonical_image_id=handoff.spatial_frame.canonical_image_id,
            canonical_image_sha256=handoff.spatial_frame.canonical_image_sha256,
            config_sha256=resolved_sample.resolved_config_sha256,
            pipeline_version=pipeline_version,
        ),
        stage_results=stage_results,
    )


def _batch_output_index(
    values: Sequence[tuple[str, OutputArtifact]],
) -> tuple[tuple[Mapping[str, object], ...], str]:
    rows = tuple(
        {
            "logical_name": logical_name,
            "schema_version": artifact.schema_version,
            "media_type": artifact.media_type,
            "record_count": artifact.record_count,
            "sha256": artifact.sha256,
        }
        for logical_name, artifact in values
    )
    return rows, hashlib.sha256(canonical_json_bytes(list(rows))).hexdigest()


def _authorized_final_export_root(
    *,
    resolved_sample: ResolvedBatchSample,
    output_root: Path,
    workspace_root: Path,
) -> Path:
    """Require new exports beneath the selected workspace's sample directory.

    The retained configuration and prior inputs keep their original locations;
    current state supplies the destination for this new publication.
    """
    from ..batch_manifest import operational_sample_output_root

    expected = operational_sample_output_root(resolved_sample, workspace_root) / "analysis"
    if not isinstance(output_root, Path) or output_root != expected:
        raise BatchArtifactError("final export output root differs from its authenticated sample")
    return expected


def _biological_roi_records(
    states: Mapping[str, FinalROIState],
) -> tuple[ROIRecord, ...]:
    """Select biological ROI records from the authenticated final handoff.

    Secondary classifier background is identified by its saved candidate status,
    not its class name. We retain every primary region and every secondary
    candidate, including rejected candidates and their review observations. The
    complete classifier inventory remains in the raw handoff for provenance.
    """
    return tuple(
        state.record
        for _roi_id, state in sorted(states.items())
        if not (
            state.pipeline_stage == "secondary_classifier"
            and state.candidate_status == "other"
        )
    )


def execute_final_non_spatial_export_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    phenotype_view_store: PhenotypeViewStore,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
    output_root: Path,
    pipeline_version: str,
) -> BatchRawNodeOutcome:
    """Write one final export generation without committing batch state."""

    spec = build_final_non_spatial_export_stage_spec()
    if (
        type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(resolved_sample) is not ResolvedBatchSample
        or type(phenotype_view_store) is not PhenotypeViewStore
        or type(exchange_reopener) is not ProjectStageCommittedExchangeReopener
        or not isinstance(output_root, Path)
        or not isinstance(pipeline_version, str)
        or not pipeline_version.strip()
        or context.adapter_spec != spec
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
    ):
        raise BatchArtifactError("final export requires exact direct owners")
    tissue_live = _validate_accepted_tissue_live_authority(accepted_tissue)
    current = validate_current_execution_authority(
        state_store.authenticate_current_execution_context(context),
        expected_context=context,
    )
    authorities = []
    for ref in context.inputs:
        authority = (
            state_store.authenticate_available_manifest_external_artifact(ref)
            if ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL
            else state_store.authenticate_available_artifact(ref)
        )
        if authority.artifact_ref != ref:
            raise BatchArtifactError("final export input authority changed")
        authorities.append(authority)
    committed = validate_committed_artifact_authority_set(
        tuple(authorities), expected_active_plan_sha256=current.active_plan_sha256
    )
    if (
        any(item.state_head_sha256 != current.state_head_sha256 for item in committed)
        or current.node_kind != _NODE_KIND
        or current.node_scope != "sample"
        or current.participant_ids != (resolved_sample.sample_id,)
        or len(current.project_entry_ids) != 1
        or current.validation_domain_id != resolved_sample.validation_domain_id
        or current.configuration_bindings
        != (
            ArtifactConfigurationBinding(
                resolved_sample.sample_id,
                resolved_sample.resolved_config_sha256,
            ),
        )
        or current.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        or hashlib.sha256(canonical_json_bytes(resolved_sample.config.to_mapping())).hexdigest()
        != resolved_sample.resolved_config_sha256
    ):
        raise BatchArtifactError("final export current execution scope differs")
    authorized_output_root = _authorized_final_export_root(
        resolved_sample=resolved_sample,
        output_root=output_root,
        workspace_root=state_store.layout.root,
    )
    grouped = _by_kind(committed)
    frame_owner = _single(grouped, "analysis_frame")
    tissue_owner = _single(grouped, "tissue_acceptance_result")
    domain_owner = _single(grouped, "accepted_analysis_domain")
    handoff_owner = _single(grouped, "spatial_cell_handoff")
    if (
        frame_owner.artifact_ref.scope.validation_domain_id != resolved_sample.validation_domain_id
        or frame_owner.artifact_ref.scope.sample_id != resolved_sample.sample_id
        or frame_owner.artifact_ref.scope.slide_id != resolved_sample.slide_id
        or tissue_live.committed.artifact_ref != tissue_owner.artifact_ref
        or tissue_live.committed.state_head_sha256 != current.state_head_sha256
        or tissue_live.committed.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("final export accepted tissue authority differs")
    frame = validate_analysis_frame_artifact_ref(frame_owner.artifact_ref)
    accepted_domains = reopen_committed_accepted_analysis_domain_live_authority(
        domain_owner,
        accepted_tissue=tissue_live,
        exchange_reopener=exchange_reopener,
    )
    handoff_live = reopen_committed_phenotype_handoff_live_authority(
        handoff_owner,
        resolved_sample=resolved_sample,
        exchange_reopener=exchange_reopener,
    )
    handoff = handoff_live.handoff
    if (
        frame.spatial_frame != handoff.spatial_frame
        or accepted_domains.domain_set != handoff.domain_set
        or accepted_domains.accepted_tissue is not tissue_live
        or current.analysis_domain_bindings
        != domain_owner.artifact_ref.scope.analysis_domain_bindings
        or current.analysis_domain_bindings
        != handoff_owner.artifact_ref.scope.analysis_domain_bindings
    ):
        raise BatchArtifactError("final export handoff frame or domain authority differs")

    measurements = tuple(
        decode_committed_measurements_result(item)
        for item in grouped.get("measurements_result", ())
    )
    measurement_by_section = {item.section_id: item for item in measurements}
    if len(measurement_by_section) != len(measurements):
        raise BatchArtifactError("final export measurement sections overlap")
    phenotype = _selected_phenotypes(grouped)
    phenotype_by_section = {item.section_id: item for item in phenotype}
    analyzed_domains = {domain_id for domain_id, item in handoff.domain_results.items()
        if not isinstance(item.finalization, PrimaryExcludedDomainCompletionEvidence)}
    expected_sections = {handoff.domain_results[domain_id].member.section_id for domain_id in analyzed_domains}
    if (
        set(measurement_by_section) != expected_sections
        or set(phenotype_by_section) != expected_sections
        or any(
            item.phenotype_mode != handoff.selected_phenotype_view
            or item.phenotype_generation_id != handoff.phenotype_generation_id
            or item.phenotype_view_inventory_sha256 != handoff.phenotype_inventory_sha256
            for item in phenotype
        )
    ):
        raise BatchArtifactError("final export section authority coverage differs")
    phenotype_inventory, selected_generation = (None, None) if not expected_sections else _load_selected_phenotype_generation(
        store=phenotype_view_store,
        handoff_live=handoff_live,
        slide_id=resolved_sample.slide_id,
        section_inventories={item.section_id: {
            "cell_count": measurement_by_section[item.section_id].cell_count,
            "cell_ids_sha256": item.cell_ids_sha256,
            "cell_to_domain_membership_sha256": item.cell_to_domain_membership_sha256,
        } for item in phenotype},
    )

    raw_owners = grouped.get("semantic_raw_inventory", ())
    raw_projections = tuple(decode_committed_semantic_raw_inventory(item) for item in raw_owners)
    raw_owner_by_domain = _one_per_domain(raw_owners, label="semantic raw")
    raw_by_domain = {
        _domain_id(item): projection
        for item, projection in zip(raw_owners, raw_projections, strict=True)
    }
    expected_domains = analyzed_domains
    if set(raw_by_domain) != expected_domains:
        raise BatchArtifactError("final export semantic raw coverage differs")

    profile_owners = grouped.get("semantic_profile", ())
    evidence_owners = _one_per_domain(
        grouped.get("semantic_evidence_result", ()),
        label="semantic evidence",
    )
    evaluation_owners = _one_per_domain(
        grouped.get("semantic_evaluation_result", ()),
        label="semantic evaluation",
    )
    no_secondary_owners = _one_per_domain(
        grouped.get("no_secondary_completion_evidence", ()),
        label="no-secondary completion",
    )
    finalization_owners = _one_per_domain(
        grouped.get("semantic_finalization_result", ()),
        label="semantic finalization",
    )
    from ..batch_secondary_definitions import resolve_secondary_structures, secondary_definition_selections
    secondary_selection = secondary_definition_selections(
        state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256), state_store=state_store,
        participant_ids=(resolved_sample.sample_id,), node_id=context.node_id,
    ).get(resolved_sample.sample_id)
    applicable = bool(resolve_secondary_structures(resolved_sample, secondary_selection))
    if applicable:
        if (
            set(evidence_owners) != expected_domains
            or set(evaluation_owners) != expected_domains
            or set(finalization_owners) != expected_domains
            or no_secondary_owners
            or (bool(profile_owners) != bool(expected_domains))
        ):
            raise BatchArtifactError("final export applicable semantic coverage differs")
    elif (
        evidence_owners
        or evaluation_owners
        or finalization_owners
        or profile_owners
        or set(no_secondary_owners) != expected_domains
    ):
        raise BatchArtifactError("final export no-secondary coverage differs")

    evidence_by_domain: dict[str, ValidatedSemanticEvidenceResult] = {}
    evaluation_by_domain: dict[str, ValidatedSemanticEvaluationResult] = {}
    attestation_by_section = {
        str(item.artifact_ref.owning_identity["section_id"]): item
        for item in grouped.get("phenotype_view_attestation", ())
    }
    for domain_id in sorted(expected_domains):
        domain_result = handoff.domain_results[domain_id]
        raw_owner = raw_owner_by_domain[domain_id]
        selected_source = phenotype_by_section[domain_result.member.section_id]
        source_matches = tuple(
            item
            for item in committed
            if item.artifact_ref.artifact_ref_sha256
            == selected_source.source_artifact_ref.artifact_ref_sha256
        )
        if len(source_matches) != 1:
            raise BatchArtifactError("final export phenotype source is ambiguous")
        source_authorities = source_matches
        if source_matches[0].artifact_ref.artifact_kind == "cluster_label_import_result":
            attestation = attestation_by_section.get(domain_result.member.section_id)
            if attestation is None:
                raise BatchArtifactError("final export cluster attestation is missing")
            source_authorities = (*source_authorities, attestation)
        branch = bind_committed_semantic_domain_branch(
            phenotype_application=normalize_committed_phenotype_application(source_authorities),
            raw_inventory_authority=raw_owner,
        )
        nested = domain_result.finalization
        completion_owner = (
            finalization_owners[domain_id] if applicable else no_secondary_owners[domain_id]
        )
        parsed_completion = (
            FinalizedSecondaryDomainEvidence.from_mapping(
                completion_owner.artifact_ref.owning_identity
            )
            if applicable
            else NoSecondaryDomainCompletionEvidence.from_mapping(
                completion_owner.artifact_ref.owning_identity
            )
        )
        if parsed_completion != nested:
            raise BatchArtifactError("final export nested semantic completion differs")
        if not applicable:
            if branch.branch_kind != "no_secondary":
                raise BatchArtifactError("final export semantic branch differs")
            continue
        if branch.branch_kind != "applicable":
            raise BatchArtifactError("final export semantic branch differs")
        evidence = decode_committed_semantic_evidence_result(
            evidence_owners[domain_id],
            raw_inventory=raw_owner,
            semantic_branch=branch,
            phenotype_view_attestation=attestation_by_section.get(domain_result.member.section_id),
            analysis_frame=frame_owner,
            accepted_domain=domain_owner,
            measurements=next(
                item
                for item in grouped["measurements_result"]
                if decode_committed_measurements_result(item).section_id
                == domain_result.member.section_id
            ),
            semantic_profiles=profile_owners,
            resolved_sample=resolved_sample,
            secondary_selection=secondary_selection,
        )
        evaluation = decode_committed_semantic_evaluation_result(
            evaluation_owners[domain_id],
            semantic_evidence=evidence,
            raw_inventory=raw_owner,
            semantic_profiles=profile_owners,
            resolved_sample=resolved_sample,
            secondary_selection=secondary_selection,
        )
        evidence_by_domain[domain_id] = evidence
        evaluation_by_domain[domain_id] = evaluation

    provenance = _analysis_provenance_from_batch_authorities(
        resolved_sample=resolved_sample,
        frame=frame,
        accepted_tissue=tissue_live,
        accepted_domains=accepted_domains,
        semantic_raw=raw_projections,
        handoff_live=handoff_live,
        phenotype_inventory=phenotype_inventory,
        selected_phenotype=selected_generation,
        semantic_evidence_by_domain=evidence_by_domain,
        semantic_evaluation_by_domain=evaluation_by_domain,
        pipeline_version=pipeline_version,
    )
    authorized_output_root = _authorized_final_export_root(
        resolved_sample=resolved_sample,
        output_root=authorized_output_root,
        workspace_root=state_store.layout.root,
    )
    generation_root = (
        authorized_output_root
        / "batch-exports"
        / resolved_sample.sample_id
        / artifact_ref_set_sha256(tuple(context.inputs))
    )
    final_rois = _biological_roi_records(handoff.final_roi_states)
    tissue = write_tissue_generation(
        generation_root / "tissue-sections.json", tissue_live.generations
    )
    roi_table = write_roi_table(generation_root / "rois.parquet", final_rois)
    cell_dataset = handoff_live.artifacts.cells
    provenance_artifact = write_analysis_provenance(
        generation_root / "analysis-provenance.json",
        provenance,
        outputs=(
            tissue,
            roi_table,
            cell_dataset,
            handoff_live.artifacts.descriptor,
        ),
    )
    index, index_sha256 = _batch_output_index(
        (
            ("tissue_sections", tissue),
            ("roi_table", roi_table),
            ("cell_dataset", cell_dataset),
            ("spatial_handoff_descriptor", handoff_live.artifacts.descriptor),
        )
    )
    identity = {
        "sample_id": resolved_sample.sample_id,
        "project_entry_id": current.project_entry_ids[0],
        "slide_id": resolved_sample.slide_id,
        "resolved_config_sha256": resolved_sample.resolved_config_sha256,
        "spatial_frame_sha256": handoff.spatial_frame.spatial_frame_sha256,
        "domain_set_sha256": handoff.domain_set.domain_set_sha256,
        "spatial_handoff_ref_sha256": handoff_owner.artifact_ref.artifact_ref_sha256,
        "domain_result_inventory_sha256": handoff_owner.artifact_ref.owning_identity[
            "domain_result_inventory_sha256"
        ],
        "source_ref_set_sha256": artifact_ref_set_sha256(tuple(context.inputs)),
        "batch_output_index": list(index),
        "batch_output_index_sha256": index_sha256,
        "provenance_artifact": provenance_artifact.to_mapping(),
        "node_id": context.node_id,
        "attempt_id": context.authorization.attempt_id,
        "attempt_authorization_sha256": context.authorization.authorization_sha256,
    }
    contract = final_non_spatial_export_result_identity_contract()
    output = BatchArtifactRef(
        artifact_id="final-non-spatial-export-" + digest(identity)[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=provenance_artifact.path,
        sha256=provenance_artifact.sha256,
        byte_count=provenance_artifact.path.stat().st_size,
        scope=ArtifactScope(
            resolved_sample.validation_domain_id,
            resolved_sample.sample_id,
            resolved_sample.slide_id,
            current.project_entry_ids[0],
            current.analysis_domain_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": handoff_owner.artifact_ref.runtime_versions["qupath"],
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings,
    )
    return compose_single_output_direct_stage_outcome(
        context,
        StageResult.completed(
            stage=_NODE_KIND,
            slide_id=resolved_sample.slide_id,
            generation_id=index_sha256,
        ),
        output,
    )


__all__ = [
    "FINAL_EXPORT_IDENTITY_FIELDS",
    "build_final_non_spatial_export_stage_spec",
    "execute_final_non_spatial_export_stage",
    "final_non_spatial_export_result_identity_contract",
    "project_committed_final_export_for_results",
]
