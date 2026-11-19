"""Final-global export owner for the canonical spatial handoff.

The `AuthorizedPhenotypeHandoff` record carries `current`, `resolved_sample`,
`accepted_tissue`, `accepted_domains`, `segmentation_cells` and its remaining declared
fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor, read_measurement_dataset_selection,
)
from mif_pipeline.datasets.references import compose_cell_dataset_selection

from mif_pipeline.cell_analysis import (
    DomainCellInventoryEvidence,
    DomainHandoffEvidence,
    DomainSetEvidence,
    FinalROIState,
    FinalizedSecondaryDomainEvidence,
    NoSecondaryDomainCompletionEvidence,
    PrimaryExcludedDomainCompletionEvidence,
    SpatialCellHandoff,
    build_spatial_handoff,
    semantic_parent_content_key,
    semantic_secondary_content_key,
)
from mif_pipeline.orchestration.accepted_analysis_domain_records import (
    accepted_analysis_domain_identity_contract,
)
from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    ValidatedAcceptedAnalysisDomainLiveAuthority,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    analysis_preflight_identity_contract,
    validate_analysis_frame_artifact_ref,
)
from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
    measurements_result_identity_contract,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    ValidatedSegmentationCellLiveAuthority,
    segmentation_result_identity_contract,
    validate_segmentation_result_artifact_ref,
)
from mif_pipeline.orchestration.batch_tissue_artifacts import (
    ValidatedAcceptedTissueLiveAuthority,
    tissue_acceptance_result_identity_contract,
)
from mif_pipeline.orchestration.exports import (
    ExportError,
    SpatialHandoffArtifacts,
    load_spatial_handoff_with_artifacts,
    write_spatial_handoff,
)
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageCodecError,
    ProjectStageTargetIntent,
)
from mif_pipeline.orchestration.project_stage_owner_capabilities import (
    ProjectStageOwnerCapabilityPublisher,
    ProjectStageOwnerCapabilityResolver,
)
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageRecordError,
    ProjectStageTargetExecutionEvidence,
)
from mif_pipeline.datasets.collection import CellDatasetCollection
from mif_pipeline.pipeline_core import (
    AnalysisDomainKind,
    ROIRecord,
    ReviewStatus,
    SemanticStatus,
    SourceKind,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.stage_result import StageStatus
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from ._common import BatchPhenotypingError, canonical_bytes, digest, plain_json, read_bounded_file
from .application_codec import cluster_label_import_result_identity_contract
from .application_records import (
    phenotype_hierarchy_attestation_identity_contract,
    semantic_raw_inventory_identity_contract,
)
from .marker_application_records import marker_section_application_identity_contract
from .semantic_applicable import semantic_finalization_result_identity_contract
from .semantic_completion import no_secondary_completion_identity_contract
from .semantic_records import (
    ValidatedPhenotypeApplicationProjection,
    normalize_committed_phenotype_application,
    validate_phenotype_application_projection,
)
from .supervised_records import supervised_phenotype_result_identity_contract

_NODE_KIND = "phenotype_handoff"
_WIRE_STAGE = "exports"
_CODEC_VERSION = "1"
_PRODUCER_VERSION = "mif-pipeline-batch-phenotype-handoff/v1"
_MAXIMUM_RAW_BYTES = 512 * 1024 * 1024
_CAPABILITY_ISSUER = object()
_LIVE_HANDOFF_ISSUER = object()
_ZERO_SHA256 = "0" * 64
_TEMPLATE_SCOPE = ArtifactScope(None, None, None, None, ())
_TEMPLATE_BINDING = ArtifactConfigurationBinding("template", _ZERO_SHA256)


# ==============================================================================
#                               Handoff execution
# ==============================================================================


def _cell_domain_pairs(owner: ValidatedSegmentationCellLiveAuthority):
    """Read only stable IDs and accepted domain IDs in bounded base groups."""
    for batch in owner.cell_dataset.iter_batches(columns=("cell_id", "analysis_domain_id")):
        yield from zip(batch.column("cell_id").to_pylist(),
                       batch.column("analysis_domain_id").to_pylist())


def _streaming_file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


HANDOFF_IDENTITY_FIELDS = (
    "sample_id",
    "project_entry_id",
    "slide_id",
    "spatial_frame_sha256",
    "domain_set_sha256",
    "accepted_domain_ref_sha256s",
    "segmentation_ref_sha256s",
    "measurements_ref_sha256s",
    "selected_phenotype_view",
    "phenotype_generation_id",
    "phenotype_inventory_sha256",
    "phenotype_source_ref_sha256s",
    "semantic_raw_ref_sha256s",
    "completion_ref_sha256s",
    "source_ref_set_sha256",
    "global_raw.sha256",
    "global_raw.byte_count",
    "global_raw.saved_state_before_sha256",
    "global_raw.saved_state_after_sha256",
    "global_raw.saved_state_disposition",
    "domain_result_inventory_sha256",
    "cell_selection_sha256",
    "cell_count",
    "descriptor.sha256",
    "descriptor.byte_count",
    "cell_artifact.sha256",
    "cell_artifact.byte_count",
    "execution_exchange_sha256",
    "node_id",
    "attempt_id",
    "attempt_authorization_sha256",
    "request_codec_id",
    "request_codec_version",
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "generations",
        "object_outputs",
    }
)
_OUTPUT_FIELDS = frozenset(
    {
        "path",
        "sha256",
        "roi_count",
        "cell_ids_sha256",
        "cell_count",
        "tissue_section_count",
        "selected_measurement_names",
        "coordinate_system",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "phenotype_components",
        "schema_version",
        "slide_id",
        "generation_id",
        "pipeline_run_id",
        "coordinate_system",
        "tissue_sections",
        "rois",
        "cell_datasets",
        "cell_scope",
        "cell_summary",
    }
)
_TISSUE_FIELDS = frozenset(
    {
        "slide_id",
        "section_id",
        "roi_id",
        "generation_id",
        "revision",
        "geometry",
    }
)
_ROI_FIELDS = frozenset(
    {
        "roi_id",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_kind",
        "structure_id",
        "path_object_id",
        "geometry_digest",
        "class",
        "roi_level",
        "primary_class_id",
        "parent_roi_id",
        "geometry",
        "source",
        "classifier_id",
        "classifier_version",
        "classifier_confidence",
        "semantic_profile",
        "semantic_status",
        "semantic_rule_results",
        "semantic_classification_status",
        "semantic_class_id",
        "review_rationale",
        "review_status",
        "original_predicted_geometry",
        "generation_id",
        "pipeline_stage",
        "pipeline_run_id",
        "classifier_status",
        "candidate_status",
        "registration_id",
        "registration_qc",
        "moving_assistance_decision_id",
        "moving_assistance_decision_source",
        "moving_assistance_decided_by",
    }
)


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


def spatial_cell_handoff_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-spatial-cell-handoff-identity/v3",
        artifact_kind="spatial_cell_handoff",
        schema_name="spatial-cell-handoff",
        schema_version=3,
        required_fields=HANDOFF_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPhenotypeHandoffLiveAuthority:
    """Current v3 handoff plus its exact retained all-sections raw export."""

    committed: BatchCommittedArtifactAuthority
    handoff: SpatialCellHandoff
    artifacts: SpatialHandoffArtifacts
    global_raw_descriptor: Mapping[str, object]
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("phenotype handoff live authority is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("phenotype handoff live authority is nonserializable")


def build_phenotype_handoff_stage_spec() -> StageAdapterSpec:
    from ..batch_classifier_inference_contracts import primary_classifier_inference_result_identity_contract
    required = (
        (analysis_preflight_identity_contract(), ArtifactMultiplicity.SINGLE),
        (tissue_acceptance_result_identity_contract(), ArtifactMultiplicity.SINGLE),
        (accepted_analysis_domain_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (segmentation_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
    )
    optional = (
        (primary_classifier_inference_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (measurements_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_raw_inventory_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (marker_section_application_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (supervised_phenotype_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (cluster_label_import_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (phenotype_hierarchy_attestation_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (no_secondary_completion_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (semantic_finalization_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
    )
    output = spatial_cell_handoff_identity_contract()
    return StageAdapterSpec(
        node_kind=_NODE_KIND,
        node_version="1",
        required_inputs=tuple(_requirement(*item) for item in required),
        optional_inputs=tuple(_requirement(*item) for item in optional),
        declared_outputs=(output.artifact_kind,),
        output_identity_contracts=(output,),
        compatibility_signature_id="mif-batch-phenotype-handoff-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="mif-batch-phenotype-handoff-equivalence/v1",
            byte_deterministic=False,
            canonical_identity_paths=tuple(
                (output.artifact_kind, field) for field in HANDOFF_IDENTITY_FIELDS[:14]
            ),
            stable_inventory_paths=tuple(
                (output.artifact_kind, field) for field in HANDOFF_IDENTITY_FIELDS[14:24]
            ),
            scientific_value_paths=tuple(
                (output.artifact_kind, field) for field in HANDOFF_IDENTITY_FIELDS[24:28]
            ),
            qc_evidence_paths=tuple(
                (output.artifact_kind, field) for field in HANDOFF_IDENTITY_FIELDS[28:]
            ),
        ),
        dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        resource_kind="qupath_image_write",
    )


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedPhenotypeHandoff:
    current: BatchCurrentExecutionAuthority
    resolved_sample: ResolvedBatchSample
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority
    segmentation_cells: tuple[ValidatedSegmentationCellLiveAuthority, ...]
    measurements: tuple[ValidatedMeasurementsResultProjection, ...]
    selected_phenotypes: tuple[ValidatedPhenotypeApplicationProjection, ...]
    semantic_raws: tuple[object, ...]
    finalization_by_domain: Mapping[str, object]
    semantic_components_by_domain: Mapping[str, Mapping[str, object]]
    committed_inputs: tuple[BatchCommittedArtifactAuthority, ...]
    request_body: Mapping[str, object]
    output_root: Path
    target_binding: AnalysisDomainBinding
    selected_measurement_names: tuple[str, ...]
    saved_state_before_sha256: str
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("phenotype handoff capability is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("phenotype handoff capability is nonserializable")


PhenotypeHandoffAuthorityResolver = ProjectStageOwnerCapabilityResolver


def _capability_material(value: AuthorizedPhenotypeHandoff) -> dict[str, object]:
    return {
        "current": id(value.current),
        "resolved_sample": id(value.resolved_sample),
        "accepted_tissue": id(value.accepted_tissue),
        "accepted_domains": id(value.accepted_domains),
        "segmentation_cells": [id(item) for item in value.segmentation_cells],
        "measurements": [id(item) for item in value.measurements],
        "selected_phenotypes": [id(item) for item in value.selected_phenotypes],
        "semantic_raws": [id(item) for item in value.semantic_raws],
        "finalization_by_domain": {
            key: plain_json(item.identity_mapping())
            for key, item in value.finalization_by_domain.items()
        },
        "semantic_components_by_domain": plain_json(value.semantic_components_by_domain),
        "committed_inputs": [
            item.artifact_ref.artifact_ref_sha256 for item in value.committed_inputs
        ],
        "request_body": plain_json(value.request_body),
        "output_root": str(value.output_root),
        "target_binding": value.target_binding.to_mapping(),
        "selected_measurement_names": list(value.selected_measurement_names),
        "saved_state_before_sha256": value.saved_state_before_sha256,
    }


def _validate_capability(
    value: object,
    *,
    context: BatchNodeExecutionContext,
) -> AuthorizedPhenotypeHandoff:
    if (
        type(value) is not AuthorizedPhenotypeHandoff
        or value._issuer is not _CAPABILITY_ISSUER
        or value._seal_sha256 != digest(_capability_material(value))
    ):
        raise BatchPhenotypingError("phenotype handoff capability is foreign")
    validate_current_execution_authority(value.current, expected_context=context)
    return value


def _by_kind(
    values: tuple[BatchCommittedArtifactAuthority, ...],
) -> dict[str, tuple[BatchCommittedArtifactAuthority, ...]]:
    grouped: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for item in values:
        grouped.setdefault(item.artifact_ref.artifact_kind, []).append(item)
    return {key: tuple(items) for key, items in grouped.items()}


def _normalize_exact_selected_phenotypes(
    inputs: tuple[BatchCommittedArtifactAuthority, ...],
    *,
    expected_sections: tuple[str, ...],
) -> tuple[ValidatedPhenotypeApplicationProjection, ...]:
    grouped = _by_kind(inputs)
    direct = (
        *grouped.get("marker_phenotype_result", ()),
        *grouped.get("supervised_phenotype_result", ()),
    )
    projections = [normalize_committed_phenotype_application((item,)) for item in direct]
    cluster = grouped.get("cluster_label_import_result", ())
    attestations = list(grouped.get("phenotype_view_attestation", ()))
    for source in cluster:
        matches = [
            item for item in attestations if item.artifact_ref.scope == source.artifact_ref.scope
        ]
        if len(matches) != 1:
            raise BatchPhenotypingError(
                "cluster source requires one matching hierarchy attestation"
            )
        projections.append(normalize_committed_phenotype_application((source, matches[0])))
        attestations.remove(matches[0])
    if attestations:
        raise BatchPhenotypingError("phenotype hierarchy attestation has no cluster source")
    projections = [validate_phenotype_application_projection(item) for item in projections]
    sections = tuple(sorted(item.section_id for item in projections))
    heads = {
        (item.phenotype_mode, item.phenotype_generation_id, item.phenotype_view_inventory_sha256)
        for item in projections
    }
    if sections != expected_sections or len(heads) != bool(expected_sections):
        raise BatchPhenotypingError("selected phenotype sources do not form one exact section head")
    return tuple(sorted(projections, key=lambda item: item.section_id))


def _completion_evidence(
    authority: BatchCommittedArtifactAuthority,
    semantic_components: dict[str, Mapping[str, object]],
) -> object:
    if authority.artifact_ref.artifact_kind == "semantic_finalization_result":
        from .semantic_applicable import decode_committed_semantic_finalization_result
        selected = decode_committed_semantic_finalization_result(authority)
        semantic_components[selected.evidence.analysis_domain_id] = selected.component_descriptor
        return selected.evidence
    ref = validate_artifact_ref(authority.artifact_ref)
    if ref.artifact_kind == "no_secondary_completion_evidence":
        contract = no_secondary_completion_identity_contract()
        expected_producer = "no_secondary_completion"
        factory = NoSecondaryDomainCompletionEvidence.from_mapping
    else:
        raise BatchPhenotypingError("unsupported handoff completion authority")
    if (
        authority.producer_node_kind != expected_producer
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or len(ref.scope.analysis_domain_bindings) != 1
    ):
        raise BatchPhenotypingError("handoff completion committed owner differs")
    return factory(ref.owning_identity)


def _decode_exact_finalization_union(
    inputs: tuple[BatchCommittedArtifactAuthority, ...],
    *,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    resolved_sample: ResolvedBatchSample,
    excluded_domain_ids: frozenset[str],
    secondary_selection=None,
) -> tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]:
    grouped = _by_kind(inputs)
    authorities = (
        *grouped.get("no_secondary_completion_evidence", ()),
        *grouped.get("semantic_finalization_result", ()),
    )
    result: dict[str, object] = {}
    semantic_components: dict[str, Mapping[str, object]] = {}
    from ..batch_secondary_definitions import resolve_secondary_structures
    configured_secondary = bool(resolve_secondary_structures(resolved_sample, secondary_selection))
    for authority in authorities:
        evidence = _completion_evidence(authority, semantic_components)
        if configured_secondary != isinstance(evidence, FinalizedSecondaryDomainEvidence):
            raise BatchPhenotypingError(
                "semantic completion kind differs from resolved configuration"
            )
        domain_id = str(evidence.analysis_domain_id)
        if domain_id in result:
            raise BatchPhenotypingError("semantic completion union overlaps")
        if (
            isinstance(evidence, NoSecondaryDomainCompletionEvidence)
            and evidence.analysis_config_sha256 != resolved_sample.resolved_config_sha256
        ):
            raise BatchPhenotypingError("semantic completion configuration differs")
        result[domain_id] = evidence
    expected = {item.analysis_domain_id for item in accepted_domains.domain_set.members} - excluded_domain_ids
    if set(result) != expected:
        raise BatchPhenotypingError("semantic completion union is incomplete")
    return (MappingProxyType(dict(sorted(result.items()))),
            MappingProxyType(dict(sorted(semantic_components.items()))))


def _selected_tissue_sections(capability):
    """Keep the final image export within sections that contain selected domains."""
    selected = {item.section_id for item in capability.accepted_domains.domain_set.members}
    return tuple(item for item in capability.accepted_tissue.sections if item.section.section_id in selected)


def _global_export_request(
    *,
    resolved_sample: ResolvedBatchSample,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    segmentation_cells: tuple[ValidatedSegmentationCellLiveAuthority, ...],
    selected_phenotypes: tuple[ValidatedPhenotypeApplicationProjection, ...],
    selected_measurement_names: tuple[str, ...],
    committed_inputs: tuple[BatchCommittedArtifactAuthority, ...],
) -> Mapping[str, object]:
    from ..batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result
    excluded_primaries = tuple(decode_committed_primary_classifier_inference_result(item)
        for item in committed_inputs if item.artifact_ref.artifact_kind == "primary_inference_result")
    first = accepted_domains.domain_authorities[0]
    populations = {
        item.committed.section_id: item.committed.segmentation_population_id
        for item in segmentation_cells
    }
    phenotype = selected_phenotypes[0] if selected_phenotypes else None
    request = {
        "schema_version": "roi-workflow-invocation/v1",
        "stage": _NODE_KIND,
        "sample_id": resolved_sample.sample_id,
        "slide_id": resolved_sample.slide_id,
        "run_id": segmentation_cells[0].pipeline_run_id,
        "generation_ids": [item.generation_id for item in accepted_tissue.generations],
        "generation_id": first.tissue_generation_id,
        "canonical_image_id": first.canonical_image_id,
        "action": None,
        "action_context": {},
        "classifier_reuse_decisions": {},
        "confirmed_marker_rules": {},
        "registration_context": None,
        "domain_set_sha256": accepted_domains.domain_set.domain_set_sha256,
        "analysis_domain_selection_status": "accepted",
        "analysis_domain_manifests": [item.to_mapping() for item in accepted_domains.manifests],
        "accepted_tissue_section_authorities": [
            plain_json(item) for item in accepted_tissue.accepted_tissue_request_rows
        ],
        "segmentation_population_ids": dict(sorted(populations.items())),
        "selected_measurement_names": list(selected_measurement_names),
        "selected_phenotype_view": None if phenotype is None else phenotype.phenotype_mode,
        "phenotype_generation_id": None if phenotype is None else phenotype.phenotype_generation_id,
        "phenotype_inventory_sha256": None if phenotype is None else phenotype.phenotype_view_inventory_sha256,
        "excluded_section_ids": sorted(item.committed.section_id for item in segmentation_cells
            if item.committed.segmentation_skipped_excluded_domains),
        "excluded_primary_sources": [{
            "analysis_domain_id": primary.analysis_domain_bindings[0].analysis_domain_id,
            "section_id": primary.section_id,
            "producer_attempt_id": primary.artifact_ref.producer_attempt_id,
            "roi_inventory": primary.roi_inventory.to_mapping(),
        } for primary in sorted(excluded_primaries, key=lambda item: item.analysis_domain_bindings[0].analysis_domain_id)],
        "expected_analysis_domain_ids": sorted(
            item.analysis_domain_id for item in accepted_domains.domain_set.members
        ),
        "expected_cell_ids": sorted(
            cell_id
            for owner in segmentation_cells
            for cell_id, _ in _cell_domain_pairs(owner)
        ),
    }
    return MappingProxyType(request)


def _validate_public_live_authorities(
    *,
    accepted_tissue: object,
    accepted_domains: object,
    segmentation_cells: object,
) -> tuple[
    ValidatedAcceptedTissueLiveAuthority,
    ValidatedAcceptedAnalysisDomainLiveAuthority,
    tuple[ValidatedSegmentationCellLiveAuthority, ...],
]:
    """Associate already opened domain and segmentation data with their tissue.

    The domain owner decoded and selected immutable manifests when opening the
    accepted result. We reuse those records and check the tissue association,
    complete section coverage and cell-ID uniqueness required by this handoff.
    """

    if type(accepted_tissue) is not ValidatedAcceptedTissueLiveAuthority:
        raise BatchPhenotypingError("accepted tissue live authority is foreign")
    tissue = accepted_tissue
    try:
        request_rows = tuple(item.to_request_mapping() for item in tissue.sections)
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("accepted tissue live authority is invalid") from exc
    if request_rows != tuple(tissue.accepted_tissue_request_rows):
        raise BatchPhenotypingError("accepted tissue live inventory changed")
    if (
        type(accepted_domains) is not ValidatedAcceptedAnalysisDomainLiveAuthority
        or accepted_domains.accepted_tissue is not tissue
    ):
        raise BatchPhenotypingError("accepted domain live authority is foreign")
    domains = accepted_domains
    if not isinstance(segmentation_cells, tuple) or not segmentation_cells:
        raise BatchPhenotypingError("handoff requires exact per-section segmentation owners")
    cells = segmentation_cells
    for owner in cells:
        if (
            type(owner) is not ValidatedSegmentationCellLiveAuthority
            or owner.accepted_domains is not domains
        ):
            raise BatchPhenotypingError("segmentation live authority is foreign")
        validate_segmentation_result_artifact_ref(owner.committed.artifact_ref)
    section_ids = tuple(sorted(item.section.section_id for item in tissue.sections))
    cell_sections = tuple(sorted(item.committed.section_id for item in cells))
    domain_sections = tuple(sorted({item.section_id for item in domains.domain_set.members}))
    if not set(domain_sections).issubset(section_ids) or cell_sections != domain_sections:
        raise BatchPhenotypingError("tissue, domain, and segmentation section cover differs")
    if len(set(cell_sections)) != len(cell_sections):
        raise BatchPhenotypingError("segmentation section cover is duplicated")
    cell_ids: set[str] = set()
    for owner in cells:
        for cell_id, _ in _cell_domain_pairs(owner):
            identifier = str(cell_id)
            if identifier in cell_ids:
                raise BatchPhenotypingError("segmentation cell identity overlaps across sections")
            cell_ids.add(identifier)
    return tissue, domains, tuple(sorted(cells, key=lambda item: item.committed.section_id))


def _excluded_section_completions(*, inputs, cells, domains, frame):
    """Join exact primary exclusions to sections that published an empty base.

    The segmentation owner authenticated these same primary references before
    skipping the detector. We retain one compact count decision per domain and
    reject missing/extra primary inputs, mixed active sections, or nonempty
    populations before authorizing the terminal branch.
    """
    from ..batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result

    primaries = tuple(decode_committed_primary_classifier_inference_result(item)
        for item in inputs if item.artifact_ref.artifact_kind == "primary_inference_result")
    primary_by_domain = {item.analysis_domain_bindings[0].analysis_domain_id: item for item in primaries}
    if len(primary_by_domain) != len(primaries):
        raise BatchPhenotypingError("excluded primary domain authority overlaps")
    members = {item.analysis_domain_id: item for item in domains.domain_set.members}
    result = {}
    for owner in cells:
        section = owner.committed
        if not section.segmentation_skipped_excluded_domains:
            if section.cell_count == 0:
                raise BatchPhenotypingError("active zero-cell section requires segmentation review")
            continue
        section_members = {key: value for key, value in members.items() if value.section_id == section.section_id}
        selected = {key: primary_by_domain[key] for key in section_members if key in primary_by_domain}
        if (set(selected) != set(section_members) or section.cell_count != 0
                or owner.cell_dataset.row_count != 0
                or set(owner.domain_cell_counts) != set(section_members)
                or any(owner.domain_cell_counts.values())
                or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in selected.values()))
                   != section.primary_inference_result_ref_sha256s):
            raise BatchPhenotypingError("excluded section does not bind exact primary and empty cell authority")
        for domain_id, primary in selected.items():
            member = section_members[domain_id]
            binding = primary.analysis_domain_bindings[0]
            if (primary.domain_status == "active"
                    or primary.pipeline_run_id != owner.pipeline_run_id
                    or primary.section_id != member.section_id
                    or primary.tissue_generation_id != member.tissue_generation_id
                    or primary.canonical_image_id != frame.canonical_image_id
                    or primary.canonical_image_sha256 != frame.canonical_image_sha256
                    or binding.analysis_domain_revision != member.analysis_domain_revision
                    or binding.analysis_domain_revision_sha256 != member.analysis_domain_revision_sha256
                    or binding.domain_set_sha256 != domains.domain_set.domain_set_sha256
                    or primary.artifact_ref.configuration_bindings != section.artifact_ref.configuration_bindings):
                raise BatchPhenotypingError("excluded primary authority has stale scope or active disposition")
            result[domain_id] = PrimaryExcludedDomainCompletionEvidence(
                primary_result_ref_sha256=primary.artifact_ref.artifact_ref_sha256,
                segmentation_result_ref_sha256=section.artifact_ref.artifact_ref_sha256,
                pipeline_run_id=owner.pipeline_run_id,
                primary_count_decision=primary.primary_count_decision.to_mapping(),
                domain_status=primary.domain_status)
    if set(result) != set(primary_by_domain):
        raise BatchPhenotypingError("primary terminal inputs extend beyond excluded sections")
    return result


def _validate_authority_union(
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    tissue: ValidatedAcceptedTissueLiveAuthority,
    domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    cells: tuple[ValidatedSegmentationCellLiveAuthority, ...],
    inputs: tuple[BatchCommittedArtifactAuthority, ...],
    secondary_selection=None,
) -> tuple[
    tuple[ValidatedMeasurementsResultProjection, ...],
    tuple[ValidatedPhenotypeApplicationProjection, ...],
    tuple[object, ...],
    Mapping[str, object],
    Mapping[str, Mapping[str, object]],
    tuple[str, ...],
]:
    """Partition accepted sections into performed work and primary exclusions.

    Current tissue/domain/segmentation owners cover the entire accepted sample.
    Excluded sections join exact primary references to real empty bases; all
    other sections retain exact measurement, selected phenotype and semantic
    completion coverage. The two routes cannot overlap or omit a domain.
    """

    if type(resolved_sample) is not ResolvedBatchSample:
        raise TypeError("phenotype handoff requires one resolved sample")
    if digest(resolved_sample.config.to_mapping()) != resolved_sample.resolved_config_sha256:
        raise BatchPhenotypingError("resolved handoff configuration digest differs")
    grouped = _by_kind(inputs)
    required_counts = {
        "analysis_frame": 1,
        "tissue_acceptance_result": 1,
        "accepted_analysis_domain": 1,
    }
    if any(len(grouped.get(kind, ())) != count for kind, count in required_counts.items()):
        raise BatchPhenotypingError("handoff singleton authority inventory differs")
    known = {
        "analysis_frame",
        "tissue_acceptance_result",
        "accepted_analysis_domain",
        "segmentation_result",
        "primary_inference_result",
        "measurements_result",
        "semantic_raw_inventory",
        "marker_phenotype_result",
        "supervised_phenotype_result",
        "cluster_label_import_result",
        "phenotype_view_attestation",
        "no_secondary_completion_evidence",
        "semantic_finalization_result",
    }
    if set(grouped) - known:
        raise BatchPhenotypingError("handoff input authority kind is unsupported")
    if (
        grouped["tissue_acceptance_result"][0].artifact_ref != tissue.committed.artifact_ref
        or grouped["accepted_analysis_domain"][0].artifact_ref != domains.committed.artifact_ref
        or {
            item.artifact_ref.artifact_ref_sha256 for item in grouped.get("segmentation_result", ())
        }
        != {item.committed.artifact_ref.artifact_ref_sha256 for item in cells}
        or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in inputs))
        != current.input_artifact_ref_sha256s
        or tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        != current.input_artifact_ref_sha256s
    ):
        raise BatchPhenotypingError("handoff inputs differ from exact live/current owners")
    if any(
        item.state_head_sha256 != current.state_head_sha256
        or item.active_plan_sha256 != current.active_plan_sha256
        for item in inputs
    ):
        raise BatchArtifactError("handoff committed inputs are not current")
    if (
        tissue.committed.state_head_sha256 != current.state_head_sha256
        or tissue.committed.active_plan_sha256 != current.active_plan_sha256
        or domains.committed.state_head_sha256 != current.state_head_sha256
        or domains.committed.active_plan_sha256 != current.active_plan_sha256
        or any(
            owner.committed.state_head_sha256 != current.state_head_sha256
            or owner.committed.active_plan_sha256 != current.active_plan_sha256
            for owner in cells
        )
    ):
        raise BatchArtifactError("handoff live authorities are not current")
    if (
        resolved_sample.sample_id != current.participant_ids[0]
        or resolved_sample.slide_id != domains.committed.slide_id
        or current.configuration_bindings
        != (
            ArtifactConfigurationBinding(
                resolved_sample.sample_id, resolved_sample.resolved_config_sha256
            ),
        )
        or any(
            item.configuration_bindings != current.configuration_bindings for item in context.inputs
        )
    ):
        raise BatchPhenotypingError("handoff sample or configuration authority differs")
    frame_authority = grouped["analysis_frame"][0]
    frame = validate_analysis_frame_artifact_ref(frame_authority.artifact_ref)
    members = domains.domain_set.members
    member_scope = {
        item.analysis_domain_id: (
            item.analysis_domain_revision,
            item.analysis_domain_revision_sha256,
        )
        for item in members
    }
    committed_domain_scope = {
        item.analysis_domain_id: (
            item.analysis_domain_revision,
            item.analysis_domain_revision_sha256,
        )
        for item in domains.committed.artifact_ref.scope.analysis_domain_bindings
    }
    if (
        any(
            item.slide_id != resolved_sample.slide_id
            or item.canonical_image_id != frame.spatial_frame.canonical_image_id
            or item.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
            for item in domains.domain_authorities
        )
        or committed_domain_scope != member_scope
    ):
        raise BatchPhenotypingError("handoff domain set differs from canonical frame")

    exclusions = _excluded_section_completions(inputs=inputs, cells=cells, domains=domains,
        frame=frame.spatial_frame)
    excluded_sections = {member.section_id for member in members if member.analysis_domain_id in exclusions}
    measurements = tuple(
        sorted(
            (
                decode_committed_measurements_result(item)
                for item in grouped.get("measurements_result", ())
            ),
            key=lambda item: item.section_id,
        )
    )
    section_ids = tuple(sorted({item.section_id for item in members}))
    analyzed_sections = tuple(section_id for section_id in section_ids if section_id not in excluded_sections)
    if tuple(item.section_id for item in measurements) != analyzed_sections:
        raise BatchPhenotypingError("measurement section cover is incomplete or duplicated")
    cells_by_section = {item.committed.section_id: item for item in cells}
    member_bindings_by_section = {
        section_id: {
            item.analysis_domain_id: (
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
            )
            for item in members
            if item.section_id == section_id
        }
        for section_id in section_ids
    }
    selected_names = {item.selected_measurement_names for item in measurements}
    if len(selected_names) != bool(analyzed_sections):
        raise BatchPhenotypingError("selected measurement inventory differs by section")
    for measurement in measurements:
        segmentation = cells_by_section[measurement.section_id]
        expected_section_bindings = member_bindings_by_section[measurement.section_id]
        observed_segmentation_bindings = {
            item.analysis_domain_id: (
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
            )
            for item in segmentation.committed.analysis_domain_bindings
        }
        observed_measurement_bindings = {
            item.analysis_domain_id: (
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
            )
            for item in measurement.analysis_domain_bindings
        }
        if (
            measurement.state_head_sha256 != current.state_head_sha256
            or measurement.active_plan_sha256 != current.active_plan_sha256
            or measurement.segmentation_population_id
            != segmentation.committed.segmentation_population_id
            or measurement != segmentation.measurement_result
            or observed_segmentation_bindings != expected_section_bindings
            or observed_measurement_bindings != expected_section_bindings
        ):
            raise BatchPhenotypingError("measurement and segmentation authority differs")

    selected = _normalize_exact_selected_phenotypes(inputs, expected_sections=analyzed_sections)
    measurement_by_section = {item.section_id: item for item in measurements}
    for item in selected:
        measurement = measurement_by_section[item.section_id]
        observed_phenotype_bindings = {
            binding.analysis_domain_id: (
                binding.analysis_domain_revision,
                binding.analysis_domain_revision_sha256,
            )
            for binding in item.analysis_domain_bindings
        }
        if (
            item.state_head_sha256 != current.state_head_sha256
            or item.active_plan_sha256 != current.active_plan_sha256
            or item.cell_ids_sha256 != measurement.cell_ids_sha256
            or item.cell_to_domain_membership_sha256 != measurement.cell_to_domain_membership_sha256
            or item.source_artifact_ref.scope.validation_domain_id
            != measurement.artifact_ref.scope.validation_domain_id
            or observed_phenotype_bindings != member_bindings_by_section[item.section_id]
        ):
            raise BatchPhenotypingError("phenotype and measurement authority differs")

    from .application_codec import decode_committed_semantic_raw_inventory

    raws = tuple(
        decode_committed_semantic_raw_inventory(item)
        for item in grouped.get("semantic_raw_inventory", ())
    )
    raw_by_domain: dict[str, object] = {}
    selected_by_section = {item.section_id: item for item in selected}
    for raw in raws:
        bindings = raw.artifact_ref.scope.analysis_domain_bindings
        if len(bindings) != 1 or bindings[0].analysis_domain_id in raw_by_domain:
            raise BatchPhenotypingError("semantic raw domain cover overlaps")
        binding = bindings[0]
        member = next(
            (item for item in members if item.analysis_domain_id == binding.analysis_domain_id),
            None,
        )
        phenotype = selected_by_section.get(raw.snapshot.section_id)
        segmentation = cells_by_section.get(member.section_id) if member is not None else None
        expected_domain_membership = (
            {
                cell_id: domain_id
                for cell_id, domain_id in (_cell_domain_pairs(segmentation))
                if domain_id == binding.analysis_domain_id
            }
            if segmentation is not None
            else {}
        )
        expected_domain_cells = tuple(sorted(expected_domain_membership))
        if (
            member is None
            or phenotype is None
            or segmentation is None
            or raw.snapshot.section_id != member.section_id
            or binding.analysis_domain_revision != member.analysis_domain_revision
            or binding.analysis_domain_revision_sha256 != member.analysis_domain_revision_sha256
            or binding.domain_set_sha256 != domains.domain_set.domain_set_sha256
            or raw.state_head_sha256 != current.state_head_sha256
            or raw.active_plan_sha256 != current.active_plan_sha256
            or raw.phenotype_source_artifact_ref_sha256
            != phenotype.source_artifact_ref.artifact_ref_sha256
            or raw.snapshot.cell_ids_sha256 != digest(list(expected_domain_cells))
            or raw.snapshot.cell_to_domain_membership_sha256 != digest(expected_domain_membership)
            or raw.snapshot.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
        ):
            raise BatchPhenotypingError("semantic raw and selected phenotype authority differs")
        raw_by_domain[binding.analysis_domain_id] = raw
    if set(raw_by_domain) != {item.analysis_domain_id for item in members} - set(exclusions):
        raise BatchPhenotypingError("semantic raw domain cover is incomplete")
    finalizations, semantic_components = _decode_exact_finalization_union(
        inputs, accepted_domains=domains, resolved_sample=resolved_sample,
        excluded_domain_ids=frozenset(exclusions),
        secondary_selection=secondary_selection,
    )
    members_by_id = {item.analysis_domain_id: item for item in members}
    for domain_id, evidence in finalizations.items():
        member = members_by_id[domain_id]
        raw = raw_by_domain[domain_id]
        completion_authority = next(
            item
            for item in inputs
            if item.artifact_ref.artifact_kind
            in {"no_secondary_completion_evidence", "semantic_finalization_result"}
            and item.artifact_ref.scope.analysis_domain_bindings[0].analysis_domain_id == domain_id
        )
        completion_binding = completion_authority.artifact_ref.scope.analysis_domain_bindings[0]
        if (
            evidence.analysis_domain_revision_sha256 != member.analysis_domain_revision_sha256
            or evidence.domain_set_sha256 != domains.domain_set.domain_set_sha256
            or evidence.spatial_frame_sha256 != frame.spatial_frame.spatial_frame_sha256
            or completion_binding.analysis_domain_revision != member.analysis_domain_revision
            or completion_binding.analysis_domain_revision_sha256
            != member.analysis_domain_revision_sha256
            or completion_binding.domain_set_sha256 != domains.domain_set.domain_set_sha256
            or (
                isinstance(evidence, NoSecondaryDomainCompletionEvidence)
                and (
                    evidence.raw_input_sha256 != raw.snapshot.raw_export_sha256
                    or evidence.domain_status != raw.snapshot.primary_domain_status
                )
            )
        ):
            raise BatchPhenotypingError("semantic completion scope or raw lineage differs")
    return (
        measurements,
        selected,
        tuple(raw_by_domain[key] for key in sorted(raw_by_domain)),
        MappingProxyType(dict(sorted({**finalizations, **exclusions}.items()))),
        semantic_components,
        next(iter(selected_names), ()),
    )


def _issue_handoff_authority(
    *,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    segmentation_cells: tuple[ValidatedSegmentationCellLiveAuthority, ...],
    measurements: tuple[ValidatedMeasurementsResultProjection, ...],
    selected_phenotypes: tuple[ValidatedPhenotypeApplicationProjection, ...],
    semantic_raws: tuple[object, ...],
    finalization_by_domain: Mapping[str, object],
    semantic_components_by_domain: Mapping[str, Mapping[str, object]],
    committed_inputs: tuple[BatchCommittedArtifactAuthority, ...],
    request_body: Mapping[str, object],
    output_root: Path,
    target_binding: AnalysisDomainBinding,
    selected_measurement_names: tuple[str, ...],
    saved_state_before_sha256: str,
) -> AuthorizedPhenotypeHandoff:
    values = {
        "current": current,
        "resolved_sample": resolved_sample,
        "accepted_tissue": accepted_tissue,
        "accepted_domains": accepted_domains,
        "segmentation_cells": segmentation_cells,
        "measurements": measurements,
        "selected_phenotypes": selected_phenotypes,
        "semantic_raws": semantic_raws,
        "finalization_by_domain": finalization_by_domain,
        "semantic_components_by_domain": semantic_components_by_domain,
        "committed_inputs": committed_inputs,
        "request_body": request_body,
        "output_root": output_root,
        "target_binding": target_binding,
        "selected_measurement_names": selected_measurement_names,
        "saved_state_before_sha256": saved_state_before_sha256,
    }
    value = object.__new__(AuthorizedPhenotypeHandoff)
    for name in AuthorizedPhenotypeHandoff.__dataclass_fields__:
        object.__setattr__(value, name, values.get(name))
    object.__setattr__(value, "_issuer", _CAPABILITY_ISSUER)
    object.__setattr__(value, "_seal_sha256", digest(_capability_material(value)))
    return value


def publish_phenotype_handoff_stage_owner_capability(
    publisher: ProjectStageOwnerCapabilityPublisher,
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    segmentation_cells: tuple[ValidatedSegmentationCellLiveAuthority, ...],
    committed_inputs: tuple[BatchCommittedArtifactAuthority, ...],
    output_root: Path,
    workspace_root: Path,
    saved_state_before_sha256: str,
    secondary_selection=None,
) -> AuthorizedPhenotypeHandoff:
    if type(publisher) is not ProjectStageOwnerCapabilityPublisher:
        raise TypeError("phenotype handoff requires exact capability publisher")
    validated_current = validate_current_execution_authority(current, expected_context=context)
    if secondary_selection is not None and (
            secondary_selection.state_head_sha256 != validated_current.state_head_sha256
            or secondary_selection.active_plan_sha256 != validated_current.active_plan_sha256):
        raise BatchPhenotypingError("Handoff definitions belong to another execution revision")
    validated_inputs = validate_committed_artifact_authority_set(
        committed_inputs,
        expected_active_plan_sha256=validated_current.active_plan_sha256,
    )
    tissue, domains, cells = _validate_public_live_authorities(
        accepted_tissue=accepted_tissue,
        accepted_domains=accepted_domains,
        segmentation_cells=segmentation_cells,
    )
    measurements, selected, raws, finalizations, semantic_components, measurement_names = _validate_authority_union(
        context=context,
        current=validated_current,
        resolved_sample=resolved_sample,
        tissue=tissue,
        domains=domains,
        cells=cells,
        inputs=validated_inputs,
        secondary_selection=secondary_selection,
    )
    bindings = _canonical_analysis_domain_bindings(
        accepted_domains=domains,
        segmentation_cells=cells,
    )
    if (
        validated_current.node_kind != _NODE_KIND
        or validated_current.node_scope != "sample"
        or validated_current.analysis_domain_bindings != bindings
    ):
        raise BatchArtifactError("phenotype handoff current execution scope differs")
    root = _authorized_handoff_output_root(
        resolved_sample=resolved_sample,
        output_root=output_root,
        workspace_root=workspace_root,
    )
    request = _global_export_request(
        resolved_sample=resolved_sample,
        accepted_tissue=tissue,
        accepted_domains=domains,
        segmentation_cells=cells,
        selected_phenotypes=selected,
        selected_measurement_names=measurement_names,
        committed_inputs=validated_inputs,
    )
    value = _issue_handoff_authority(
        current=validated_current,
        resolved_sample=resolved_sample,
        accepted_tissue=tissue,
        accepted_domains=domains,
        segmentation_cells=cells,
        measurements=measurements,
        selected_phenotypes=selected,
        semantic_raws=raws,
        finalization_by_domain=finalizations,
        semantic_components_by_domain=semantic_components,
        committed_inputs=validated_inputs,
        request_body=request,
        output_root=root,
        target_binding=bindings[0],
        selected_measurement_names=measurement_names,
        saved_state_before_sha256=saved_state_before_sha256,
    )
    publisher._publish_current(
        value,
        context=context,
        current=validated_current,
        analysis_domain_binding=bindings[0],
    )
    return value


def _mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BatchPhenotypingError(f"{label} does not have its exact closed fields")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchPhenotypingError(f"{label} must be an array")
    return tuple(value)


def _roi_state(row: Mapping[str, object]) -> FinalROIState:
    record = ROIRecord(
        roi_id=str(row["roi_id"]),
        slide_id=str(row["slide_id"]),
        section_id=str(row["section_id"]),
        roi_class=str(row["class"]),
        roi_level=str(row["roi_level"]),
        geometry=row["geometry"],  # type: ignore[arg-type]
        source=SourceKind(str(row["source"])),
        classifier_id=row["classifier_id"],  # type: ignore[arg-type]
        classifier_version=row["classifier_version"],  # type: ignore[arg-type]
        classifier_confidence=row["classifier_confidence"],  # type: ignore[arg-type]
        parent_roi_id=row["parent_roi_id"],  # type: ignore[arg-type]
        semantic_profile=row["semantic_profile"],  # type: ignore[arg-type]
        semantic_status=SemanticStatus(str(row["semantic_status"])),
        semantic_rule_results=row["semantic_rule_results"],  # type: ignore[arg-type]
        semantic_classification_status=row["semantic_classification_status"],
        semantic_class_id=row["semantic_class_id"],
        review_rationale=row["review_rationale"],
        review_status=ReviewStatus(str(row["review_status"])),
        original_predicted_geometry=row["original_predicted_geometry"],  # type: ignore[arg-type]
        registration_id=row["registration_id"],  # type: ignore[arg-type]
        registration_qc=row["registration_qc"],  # type: ignore[arg-type]
        analysis_domain_id=str(row["analysis_domain_id"]),
        analysis_domain_kind=AnalysisDomainKind(str(row["analysis_domain_kind"])),
    )
    secondary = str(row["pipeline_stage"]) == "secondary_classifier"
    parent_key = semantic_parent_content_key(
        analysis_domain_id=record.analysis_domain_id,
        parent_roi_id=record.parent_roi_id or "",
    )
    content_key = (
        semantic_secondary_content_key(
            analysis_domain_id=record.analysis_domain_id,
            roi_id=record.roi_id,
            path_object_id=str(row["path_object_id"]),
            structure_id=str(row["structure_id"]),
            parent_content_key=parent_key,
            geometry_sha256=str(row["geometry_digest"]),
        )
        if secondary
        else None
    )
    return FinalROIState(
        record=record,
        generation_id=str(row["generation_id"]),
        pipeline_run_id=str(row["pipeline_run_id"]),
        pipeline_stage=str(row["pipeline_stage"]),
        classifier_status=(
            None if row["classifier_status"] is None else str(row["classifier_status"])
        ),
        candidate_status=(
            None if row["candidate_status"] is None else str(row["candidate_status"])
        ),
        path_object_id=(str(row["path_object_id"]) if secondary else None),
        structure_id=(str(row["structure_id"]) if secondary else None),
        geometry_sha256=(str(row["geometry_digest"]) if secondary else None),
        content_key=content_key,
    )


def _handoff_dataset_selections(capability: AuthorizedPhenotypeHandoff) -> tuple[dict, ...]:
    """Select our current section bases and accepted scalar result components.

    We reuse the scientific measurement selection and exact chosen phenotype
    references. Domain finalization references add only their current memberships;
    no display pointer can choose a different result for this scientific export.
    """
    segmentations = {owner.committed.section_id: owner for owner in capability.segmentation_cells}
    phenotypes = {result.section_id: result for result in capability.selected_phenotypes}
    domain_sections = {member.analysis_domain_id: member.section_id
                       for member in capability.accepted_domains.domain_set.members}
    selections = []
    for measurement in capability.measurements:
        rows = measurement.measurement_rows
        descriptor = MarkerIntensityRowsDescriptor(rows.path, rows.sha256,
            rows.byte_count, rows.row_count, rows.content_schema_version)
        selected = read_measurement_dataset_selection(descriptor,
            authenticated_output_root=rows.path.parent.parent.parent)
        owner = segmentations[measurement.section_id]
        phenotype = phenotypes[measurement.section_id]
        semantic = {domain: reference for domain, reference in capability.semantic_components_by_domain.items()
                    if domain_sections[domain] == measurement.section_id}
        selections.append(compose_cell_dataset_selection(
            section_id=measurement.section_id,
            population_id=owner.committed.segmentation_population_id,
            cell_dataset=owner.cell_dataset_ref, measurement_selection=selected,
            phenotype_component=phenotype.component_descriptor,
            phenotype_view={'mode': phenotype.phenotype_mode,
                            'phenotype_generation_id': phenotype.phenotype_generation_id},
            semantic_components=semantic))
    for section_id, owner in sorted(segmentations.items()):
        if owner.committed.segmentation_skipped_excluded_domains:
            selections.append({"section_id": section_id,
                "population_id": owner.committed.segmentation_population_id,
                "cell_dataset": {key: owner.cell_dataset_ref[key] for key in ("path", "sha256")},
                "components": {}, "geometry_revision": 0, "cell_edits": None,
                "phenotype_view": None})
    return tuple(sorted(selections, key=lambda item: item["section_id"]))


def _decode_global_raw(
    *,
    path: Path,
    raw_sha256: str,
    capability: AuthorizedPhenotypeHandoff,
) -> tuple[
    CellDatasetCollection,
    tuple[FinalROIState, ...],
    int,
]:
    content = read_bounded_file(
        path,
        expected_byte_count=path.stat().st_size,
        maximum_byte_count=_MAXIMUM_RAW_BYTES,
    )
    if hashlib.sha256(content).hexdigest() != raw_sha256:
        raise BatchPhenotypingError("global raw export bytes changed")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("global raw export is malformed JSON") from exc
    raw = _mapping(raw, _TOP_LEVEL_FIELDS, "global raw export")
    tissues = _sequence(raw["tissue_sections"], "global tissue sections")
    rois = _sequence(raw["rois"], "global ROIs")

    generation_ids = {item.section.generation_id for item in capability.accepted_tissue.sections}
    if len(generation_ids) != 1:
        raise BatchPhenotypingError("global export requires one accepted tissue generation")
    generation_id = next(iter(generation_ids))
    if (
        raw["schema_version"] != "qupath-raw-objects/v1"
        or raw["slide_id"] != capability.resolved_sample.slide_id
        or raw["generation_id"] != generation_id
        or raw["pipeline_run_id"] != capability.segmentation_cells[0].pipeline_run_id
        or raw["coordinate_system"] != "global_level0"
    ):
        raise BatchPhenotypingError("global raw export scope differs")
    expected_tissues = {
        item.section.section_id: item for item in _selected_tissue_sections(capability)
    }
    observed_tissues: set[str] = set()
    for raw_row in tissues:
        row = _mapping(raw_row, _TISSUE_FIELDS, "global tissue row")
        section_id = str(row["section_id"])
        owner = expected_tissues.get(section_id)
        if (
            owner is None
            or section_id in observed_tissues
            or row["slide_id"] != owner.section.slide_id
            or row["roi_id"] != owner.section.roi_id
            or row["generation_id"] != owner.section.generation_id
            or row["revision"] != owner.section.revision
            or digest(row["geometry"]) != owner.geometry_sha256
        ):
            raise BatchPhenotypingError("global tissue inventory differs")
        observed_tissues.add(section_id)
    if observed_tissues != set(expected_tissues):
        raise BatchPhenotypingError("global tissue section cover is incomplete")

    members = {
        item.analysis_domain_id: item for item in capability.accepted_domains.domain_set.members
    }
    segmentations = {item.committed.section_id: item for item in capability.segmentation_cells}
    phenotypes = {item.section_id: item for item in capability.selected_phenotypes}
    from mif_pipeline.orchestration.analysis_services import _raw_cell_dataset
    dataset = _raw_cell_dataset(raw)
    selected_refs = {item["section_id"]: item for item in raw["cell_datasets"]}
    if set(selected_refs) != set(segmentations):
        dataset.close()
        raise BatchPhenotypingError("global cell selection section scope differs")
    for section_id, phenotype in phenotypes.items():
        selector = selected_refs[section_id]
        expected_ref = raw["phenotype_components"].get(phenotype.phenotype_mode, {}).get(phenotype.phenotype_generation_id, {}).get(section_id)
        if (selector["phenotype_view"] != {"mode": phenotype.phenotype_mode, "phenotype_generation_id": phenotype.phenotype_generation_id}
                or expected_ref != dict(phenotype.component_descriptor)
                or selector["cell_dataset"] != {key: segmentations[section_id].cell_dataset_ref[key] for key in ("path", "sha256")}
                or not {"measurement:" + name for name in capability.selected_measurement_names}.issubset(dataset.schema.names)):
            dataset.close()
            raise BatchPhenotypingError("global selected base, measurement or phenotype differs")

    for section_id, owner in segmentations.items():
        if owner.committed.segmentation_skipped_excluded_domains:
            selector = selected_refs[section_id]
            if (selector["phenotype_view"] is not None or selector["components"]
                    or selector["cell_edits"] is not None or selector["geometry_revision"] != 0
                    or selector["cell_dataset"] != {key: owner.cell_dataset_ref[key] for key in ("path", "sha256")}):
                dataset.close()
                raise BatchPhenotypingError("excluded section exported a performed cell-analysis selection")
    from mif_pipeline.orchestration.analysis_services import _select_current_raw_dataset
    dataset.close()
    states: list[FinalROIState] = []
    observed_roi_ids: set[str] = set()
    for raw_row in rois:
        row = _mapping(raw_row, _ROI_FIELDS, "global ROI row")
        roi_id = str(row["roi_id"])
        member = members.get(str(row["analysis_domain_id"]))
        completion = (
            capability.finalization_by_domain.get(member.analysis_domain_id)
            if member is not None
            else None
        )
        completion_run_id = (
            completion.pipeline_run_id
            if isinstance(completion, (NoSecondaryDomainCompletionEvidence, PrimaryExcludedDomainCompletionEvidence))
            else (
                completion.finalization_receipt["pipeline_run_id"]
                if isinstance(completion, FinalizedSecondaryDomainEvidence)
                else None
            )
        )
        if (
            member is None
            or completion is None
            or roi_id in observed_roi_ids
            or row["slide_id"] != member.slide_id
            or row["section_id"] != member.section_id
            or row["analysis_domain_kind"] != member.analysis_domain_kind.value
            or row["generation_id"] != member.tissue_generation_id
            or row["pipeline_run_id"] != completion_run_id
        ):
            raise BatchPhenotypingError("global ROI authority differs")
        observed_roi_ids.add(roi_id)
        states.append(_roi_state(row))
    expected_roi_sources: list[tuple[str, str, str]] = []
    for domain_raw in capability.semantic_raws:
        ref = domain_raw.artifact_ref
        if ref.locator is None or ref.sha256 is None or ref.byte_count is None:
            raise BatchPhenotypingError("semantic raw source lacks retained bytes")
        if Path(ref.locator).resolve(strict=True) == path:
            raise BatchPhenotypingError("global raw output substituted a domain snapshot")
        source_bytes = read_bounded_file(
            ref.locator,
            expected_byte_count=ref.byte_count,
            maximum_byte_count=_MAXIMUM_RAW_BYTES,
        )
        if hashlib.sha256(source_bytes).hexdigest() != ref.sha256:
            raise BatchPhenotypingError("semantic raw source bytes changed")
        try:
            source = json.loads(source_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchPhenotypingError("semantic raw source is malformed") from exc
        if not isinstance(source, Mapping):
            raise BatchPhenotypingError("semantic raw source is not an object")
        domain_id = ref.scope.analysis_domain_bindings[0].analysis_domain_id
        source_rows = _sequence(source.get("rois"), "semantic raw ROIs")
        for source_row in source_rows:
            if not isinstance(source_row, Mapping):
                raise BatchPhenotypingError("semantic raw ROI is not an object")
            roi_id = source_row.get("roi_id")
            path_id = source_row.get("path_object_id")
            if (
                not isinstance(roi_id, str)
                or not roi_id
                or not isinstance(path_id, str)
                or not path_id
            ):
                raise BatchPhenotypingError("semantic raw ROI stable identity is invalid")
            expected_roi_sources.append((domain_id, roi_id, path_id))
    # Native export matched every excluded primary table row to its exact live
    # ID/revision/class. Reuse that authenticated boundary; do not scan it again.
    excluded_sources = {item["analysis_domain_id"]: item
                        for item in capability.request_body["excluded_primary_sources"]}
    excluded_roi_counts = dict.fromkeys(excluded_sources, 0)
    for state in states:
        domain_id = state.record.analysis_domain_id
        if domain_id in excluded_roi_counts:
            if state.pipeline_stage != "primary_classifier":
                raise BatchPhenotypingError("excluded section contains a secondary ROI")
            excluded_roi_counts[domain_id] += 1
    if any(excluded_roi_counts[domain_id] != source["roi_inventory"]["row_count"]
           for domain_id, source in excluded_sources.items()):
        raise BatchPhenotypingError("excluded primary raw inventory differs from its bound table")
    if len(set(expected_roi_sources)) != len(expected_roi_sources):
        raise BatchPhenotypingError("semantic raw ROI stable identity overlaps")
    observed_roi_sources = {
        (
            state.record.analysis_domain_id,
            state.record.roi_id,
            str(state.path_object_id or row["path_object_id"]),
        )
        for state, row in zip(states, rois, strict=True)
        if state.record.analysis_domain_id not in excluded_sources
    }
    if observed_roi_sources != set(expected_roi_sources):
        raise BatchPhenotypingError("global ROI inventory differs from domain snapshots")
    dataset = _select_current_raw_dataset(raw, _handoff_dataset_selections(capability))
    return dataset, tuple(states), len(content)


def _domain_results(
    *,
    capability: AuthorizedPhenotypeHandoff,
    raw_sha256: str,
    cell_dataset,
) -> tuple[DomainHandoffEvidence, ...]:
    frame_authority = next(
        item
        for item in capability.committed_inputs
        if item.artifact_ref.artifact_kind == "analysis_frame"
    )
    frame = validate_analysis_frame_artifact_ref(frame_authority.artifact_ref).spatial_frame
    segmentation_by_section = {
        item.committed.section_id: item for item in capability.segmentation_cells
    }
    phenotype_by_section = {item.section_id: item for item in capability.selected_phenotypes}
    raw_by_domain = {
        item.artifact_ref.scope.analysis_domain_bindings[0].analysis_domain_id: item
        for item in capability.semantic_raws
    }
    results: list[DomainHandoffEvidence] = []
    for member in capability.accepted_domains.domain_set.members:
        segmentation = segmentation_by_section[member.section_id]
        phenotype = phenotype_by_section.get(member.section_id)
        domain_raw = raw_by_domain.get(member.analysis_domain_id)
        segmented = next(item for item in segmentation.domain_cell_inventories
            if item["analysis_domain_id"] == member.analysis_domain_id)
        exported = cell_dataset.source_summary["domain_inventories"][member.analysis_domain_id]
        if (exported["cell_count"] != segmented["cell_count"] or exported["cell_ids_sha256"] != segmented["cell_ids_sha256"]):
            raise BatchPhenotypingError("global raw domain identity differs")
        finalization = capability.finalization_by_domain[member.analysis_domain_id]
        primary_status = (finalization.domain_status if isinstance(finalization, PrimaryExcludedDomainCompletionEvidence)
            else domain_raw.snapshot.primary_domain_status)
        domain_status = (
            primary_status if primary_status != "active" else finalization.domain_status
        )
        source_run_id = None if phenotype is None else phenotype.source_artifact_ref.producer_attempt_id
        results.append(
            DomainHandoffEvidence.create(
                member=member,
                spatial_frame_sha256=frame.spatial_frame_sha256,
                segmentation_population_id=segmentation.committed.segmentation_population_id,
                segmentation_canonical_image_id=segmentation.committed.canonical_image_id,
                segmentation_canonical_image_sha256=segmentation.committed.canonical_image_sha256,
                segmentation_domain_set_sha256=segmentation.committed.domain_set_sha256,
                segmentation_section_population_sha256=segmentation.committed.section_population_sha256,
                measurement_rows_sha256=(None if segmentation.measurement_result is None
                    else segmentation.measurement_result.measurement_rows.sha256),
                segmentation_domain_sha256=segmentation.committed.segmentation_domain_sha256,
                segmentation_domain_cell_mapping_sha256=(segmentation.committed.domain_cell_mapping_sha256
                    if segmentation.measurement_result is None
                    else segmentation.measurement_result.cell_to_domain_membership_sha256),
                selected_phenotype_view=None if phenotype is None else phenotype.phenotype_mode,
                phenotype_generation_id=None if phenotype is None else phenotype.phenotype_generation_id,
                phenotype_inventory_sha256=None if phenotype is None else phenotype.phenotype_view_inventory_sha256,
                phenotype_section_artifact_sha256=None if phenotype is None else phenotype.phenotype_view_artifact_sha256,
                phenotype_section_lineage_sha256=None if phenotype is None else phenotype.phenotype_view_lineage_sha256,
                phenotype_source_run_id=source_run_id,
                analysis_config_sha256=capability.resolved_sample.resolved_config_sha256,
                semantic_raw_input_sha256=None if domain_raw is None else domain_raw.snapshot.raw_export_sha256,
                segmented_inventory=DomainCellInventoryEvidence.from_summary(
                    stage="segmentation",
                    analysis_domain_id=member.analysis_domain_id,
                    analysis_domain_revision_sha256=member.analysis_domain_revision_sha256,
                    count=segmented["cell_count"],
                    cell_ids_sha256=segmented["cell_ids_sha256"],
                    source_schema_version=("segmentation-population/v2" if segmentation.measurement_result is None
                        else segmentation.measurement_result.measurement_rows.content_schema_version),
                    source_evidence_sha256=(segmentation.committed.section_population_sha256
                        if segmentation.measurement_result is None else segmentation.measurement_result.measurement_rows.sha256),
                ),
                phenotyped_inventory=None if phenotype is None else DomainCellInventoryEvidence.from_summary(
                    stage="phenotyping",
                    analysis_domain_id=member.analysis_domain_id,
                    analysis_domain_revision_sha256=member.analysis_domain_revision_sha256,
                    count=segmented["cell_count"],
                    cell_ids_sha256=segmented["cell_ids_sha256"],
                    source_schema_version="phenotype-view-artifact/v1",
                    source_evidence_sha256=phenotype.phenotype_view_artifact_sha256,
                ),
                exported_inventory=DomainCellInventoryEvidence.from_summary(
                    stage="export",
                    analysis_domain_id=member.analysis_domain_id,
                    analysis_domain_revision_sha256=member.analysis_domain_revision_sha256,
                    count=exported["cell_count"],
                    cell_ids_sha256=exported["cell_ids_sha256"],
                    source_schema_version="qupath-raw-objects/v1",
                    source_evidence_sha256=raw_sha256,
                ),
                finalization=finalization,
                primary_domain_status=primary_status,
                domain_status=domain_status,
            )
        )
    return tuple(results)


def _canonical_analysis_domain_bindings(
    *,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    segmentation_cells: tuple[ValidatedSegmentationCellLiveAuthority, ...],
) -> tuple[AnalysisDomainBinding, ...]:
    by_id: dict[str, AnalysisDomainBinding] = {}
    for owner in segmentation_cells:
        for binding in owner.committed.analysis_domain_bindings:
            if binding.analysis_domain_id in by_id and by_id[binding.analysis_domain_id] != binding:
                raise BatchPhenotypingError("handoff domain binding cover conflicts")
            by_id[binding.analysis_domain_id] = binding
    members = {item.analysis_domain_id: item for item in accepted_domains.domain_set.members}
    if set(by_id) != set(members) or any(
        binding.analysis_domain_revision != members[domain_id].analysis_domain_revision
        or binding.analysis_domain_revision_sha256
        != members[domain_id].analysis_domain_revision_sha256
        or binding.domain_set_sha256 != accepted_domains.domain_set.domain_set_sha256
        for domain_id, binding in by_id.items()
    ):
        raise BatchPhenotypingError("handoff domain bindings do not cover accepted set")
    return tuple(by_id[key] for key in sorted(by_id))


def _analysis_domain_bindings(
    capability: AuthorizedPhenotypeHandoff,
) -> tuple[AnalysisDomainBinding, ...]:
    return _canonical_analysis_domain_bindings(
        accepted_domains=capability.accepted_domains,
        segmentation_cells=capability.segmentation_cells,
    )


def _authorized_handoff_output_root(
    *, resolved_sample: ResolvedBatchSample, output_root: Path, workspace_root: Path,
) -> Path:
    """Require a new handoff beneath the selected workspace's sample directory."""
    from ..batch_manifest import operational_sample_output_root

    expected = operational_sample_output_root(resolved_sample, workspace_root) / "analysis" / "exports"
    if not isinstance(output_root, Path) or output_root != expected:
        raise BatchArtifactError("phenotype handoff output root differs from its authenticated sample")
    return expected


def build_phenotype_handoff_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    authority: AuthorizedPhenotypeHandoff,
) -> BatchNodeExecutionResult:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchPhenotypingError("phenotype handoff result requires exact Task-10 evidence")
    capability = _validate_capability(authority, context=evidence.context)
    context = evidence.context
    authorized = evidence.authorized
    result = evidence.wire_stage_result
    spec = build_phenotype_handoff_stage_spec()
    bindings = _analysis_domain_bindings(capability)
    if (
        context.adapter_spec != spec
        or authorized.adapter_spec_sha256 != spec.spec_sha256
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.request_codec_id != QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value
        or authorized.request_codec_version != _CODEC_VERSION
        or authorized.wire_stage_id != _WIRE_STAGE
        or result.stage != _WIRE_STAGE
        or authorized.project_entry_id != capability.target_binding.project_entry_id
        or authorized.sample_id != capability.target_binding.sample_id
        or authorized.slide_id != capability.target_binding.slide_id
        or authorized.analysis_domain_bindings != bindings
        or authorized.prerequisite_artifact_ref_sha256s
        != capability.current.input_artifact_ref_sha256s
        or authorized.saved_state_before_sha256 != capability.saved_state_before_sha256
    ):
        raise BatchPhenotypingError("phenotype handoff Task-10 identity differs")
    if result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    validation_inputs = evidence.reopen_validation_inputs()
    if canonical_bytes(validation_inputs.scientific_request_body) != canonical_bytes(
        capability.request_body
    ) or canonical_bytes(validation_inputs.resolved_config) != canonical_bytes(
        capability.resolved_sample.config.to_mapping()
    ):
        raise BatchPhenotypingError("phenotype handoff retained request or config differs")
    manifest = _mapping(evidence.generation_manifest, _MANIFEST_FIELDS, "handoff manifest")
    outputs = _mapping(result.outputs, _OUTPUT_FIELDS, "handoff outputs")
    generation_ids = tuple(
        sorted({item.section.generation_id for item in capability.accepted_tissue.sections})
    )
    expected_cells = tuple(
        sorted(
            cell_id
            for owner in capability.segmentation_cells
            for cell_id, _ in _cell_domain_pairs(owner)
        )
    )
    expected_saved = capability.saved_state_before_sha256
    if (
        manifest["schema_version"] != "roi-generation-manifest/v1"
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _WIRE_STAGE
        or manifest["slide_id"] != authorized.slide_id
        or tuple(_sequence(manifest["generation_ids"], "handoff generation_ids")) != generation_ids
        or tuple(_sequence(manifest["generations"], "handoff generations")) != ()
        or canonical_bytes(manifest["object_outputs"]) != canonical_bytes(outputs)
        or result.slide_id != authorized.slide_id
        or result.generation_id != generation_ids[0]
        or evidence.observation.saved_state_disposition != "authenticated_unchanged"
        or evidence.observation.saved_state_before_sha256 != expected_saved
        or evidence.observation.saved_state_after_sha256 != expected_saved
        or outputs["cell_ids_sha256"] != digest(list(expected_cells))
        or outputs["cell_count"] != len(expected_cells)
        or outputs["tissue_section_count"] != len(_selected_tissue_sections(capability))
        or tuple(_sequence(outputs["selected_measurement_names"], "selected measurements"))
        != capability.selected_measurement_names
        or outputs["coordinate_system"] != "global_level0"
    ):
        raise BatchPhenotypingError("phenotype handoff result or saved state differs")
    raw_sha256 = str(outputs["sha256"])
    expected_path = (
        Path(validation_inputs.operational_output_dir)
        / "exports"
        / authorized.slide_id
        / "all-sections"
        / f"{raw_sha256}.qupath-raw-objects.json"
    )
    raw_path = Path(str(outputs["path"]))
    try:
        normalized_path = raw_path.resolve(strict=True)
        normalized_path.relative_to(
            Path(validation_inputs.operational_output_dir).resolve(strict=True)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise BatchPhenotypingError("global raw output is outside authenticated root") from exc
    if raw_path != expected_path or normalized_path != expected_path:
        raise BatchPhenotypingError("global raw output is not the all-sections snapshot")
    cell_dataset, roi_states, raw_byte_count = _decode_global_raw(
        path=normalized_path,
        raw_sha256=raw_sha256,
        capability=capability,
    )
    if outputs["roi_count"] != len(roi_states):
        raise BatchPhenotypingError("global ROI count differs")
    domain_results = _domain_results(
        capability=capability,
        raw_sha256=raw_sha256,
        cell_dataset=cell_dataset,
    )
    active_domain_ids = {
        item.member.analysis_domain_id for item in domain_results if item.domain_status == "active"
    }
    frame_authority = next(
        item
        for item in capability.committed_inputs
        if item.artifact_ref.artifact_kind == "analysis_frame"
    )
    frame = validate_analysis_frame_artifact_ref(frame_authority.artifact_ref).spatial_frame
    selected = capability.selected_phenotypes[0] if capability.selected_phenotypes else None
    handoff = build_spatial_handoff(
        tuple(item.section for item in _selected_tissue_sections(capability)),
        cell_dataset,
        spatial_frame=frame,
        domain_set=capability.accepted_domains.domain_set,
        domain_results=domain_results,
        selected_phenotype_view=None if selected is None else selected.phenotype_mode,
        phenotype_generation_id=None if selected is None else selected.phenotype_generation_id,
        phenotype_inventory_sha256=None if selected is None else selected.phenotype_view_inventory_sha256,
        final_roi_states=roi_states,
    )
    output_root = Path(validation_inputs.operational_output_dir) / "exports"
    if capability.output_root != output_root:
        raise BatchArtifactError("handoff publication differs from its recorded request directory")
    artifacts = write_spatial_handoff(
        # Each attempt retains its descriptor so a later review cannot replace
        # the file referenced by an earlier accepted result.
        output_root / authorized.attempt_id / "spatial-cell-handoff.json",
        handoff,
        dataset_scope={
            "sample_id": authorized.sample_id,
            "image_entry_id": authorized.project_entry_id,
            "domains": {
                result.member.analysis_domain_id: {
                    "measurement_generation_id": next((
                        measurement.measurement_generation_id
                        for measurement in capability.measurements
                        if measurement.section_id == result.member.section_id
                    ), None),
                }
                for result in domain_results
            },
        },
    )
    descriptor_bytes = artifacts.descriptor.path.stat().st_size
    cell_bytes = artifacts.cells.path.stat().st_size
    input_refs = tuple(item.artifact_ref for item in capability.committed_inputs)
    accepted_refs = tuple(
        sorted(
            item.artifact_ref.artifact_ref_sha256
            for item in capability.committed_inputs
            if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
        )
    )
    segmentation_refs = tuple(
        sorted(
            item.committed.artifact_ref.artifact_ref_sha256
            for item in capability.segmentation_cells
        )
    )
    measurement_refs = tuple(
        sorted(item.artifact_ref.artifact_ref_sha256 for item in capability.measurements)
    )
    phenotype_refs = tuple(
        sorted(
            item.artifact_ref.artifact_ref_sha256
            for item in capability.committed_inputs
            if item.artifact_ref.artifact_kind
            in {
                "marker_phenotype_result",
                "supervised_phenotype_result",
                "cluster_label_import_result",
                "phenotype_view_attestation",
            }
        )
    )
    raw_refs = tuple(
        sorted(item.artifact_ref.artifact_ref_sha256 for item in capability.semantic_raws)
    )
    completion_refs = tuple(
        sorted(
            item.artifact_ref.artifact_ref_sha256
            for item in capability.committed_inputs
            if item.artifact_ref.artifact_kind
            in {"no_secondary_completion_evidence", "semantic_finalization_result"}
        )
    )
    identity = {
        "sample_id": authorized.sample_id,
        "project_entry_id": authorized.project_entry_id,
        "slide_id": authorized.slide_id,
        "spatial_frame_sha256": frame.spatial_frame_sha256,
        "domain_set_sha256": capability.accepted_domains.domain_set.domain_set_sha256,
        "accepted_domain_ref_sha256s": list(accepted_refs),
        "segmentation_ref_sha256s": list(segmentation_refs),
        "measurements_ref_sha256s": list(measurement_refs),
        "selected_phenotype_view": None if selected is None else selected.phenotype_mode,
        "phenotype_generation_id": None if selected is None else selected.phenotype_generation_id,
        "phenotype_inventory_sha256": None if selected is None else selected.phenotype_view_inventory_sha256,
        "phenotype_source_ref_sha256s": list(phenotype_refs),
        "semantic_raw_ref_sha256s": list(raw_refs),
        "completion_ref_sha256s": list(completion_refs),
        "source_ref_set_sha256": artifact_ref_set_sha256(input_refs),
        "global_raw": {
            "sha256": raw_sha256,
            "byte_count": raw_byte_count,
            "saved_state_before_sha256": expected_saved,
            "saved_state_after_sha256": expected_saved,
            "saved_state_disposition": "authenticated_unchanged",
        },
        "domain_result_inventory_sha256": digest([item.to_mapping() for item in domain_results]),
        "cell_selection_sha256": handoff.cell_selection_sha256,
        "cell_count": handoff.cell_count,
        "descriptor": {
            "sha256": artifacts.descriptor.sha256,
            "byte_count": descriptor_bytes,
        },
        "cell_artifact": {
            "sha256": artifacts.cells.sha256,
            "byte_count": cell_bytes,
        },
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "node_id": authorized.node_id,
        "attempt_id": authorized.attempt_id,
        "attempt_authorization_sha256": authorized.attempt_authorization_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    contract = spatial_cell_handoff_identity_contract()
    output = BatchArtifactRef(
        artifact_id="spatial-cell-handoff-" + digest(identity)[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authorized.node_id,
        producer_attempt_id=authorized.attempt_id,
        producer_attempt_authorization_sha256=authorized.attempt_authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=artifacts.descriptor.path,
        sha256=artifacts.descriptor.sha256,
        byte_count=descriptor_bytes,
        scope=ArtifactScope(
            capability.resolved_sample.validation_domain_id,
            authorized.sample_id,
            authorized.slide_id,
            authorized.project_entry_id,
            bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": capability.segmentation_cells[0].committed.artifact_ref.runtime_versions["qupath"],
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=capability.current.configuration_bindings,
    )
    validate_artifact_ref(output)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (output,))


def reopen_committed_phenotype_handoff_live_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    resolved_sample: ResolvedBatchSample,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
) -> ValidatedPhenotypeHandoffLiveAuthority:
    """Reopen the exact committed v3 handoff and retained global raw export."""

    if (
        type(authority) is not BatchCommittedArtifactAuthority
        or type(resolved_sample) is not ResolvedBatchSample
        or type(exchange_reopener) is not ProjectStageCommittedExchangeReopener
    ):
        raise BatchArtifactError("phenotype handoff reopen requires exact current owners")
    (committed,) = validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    artifact = validate_artifact_ref(committed.artifact_ref)
    identity = artifact.owning_identity
    contract = spatial_cell_handoff_identity_contract()
    try:
        handoff, artifacts = load_spatial_handoff_with_artifacts(artifact.locator)
    except (ExportError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "committed phenotype handoff artifacts cannot be reopened"
        ) from exc
    if (
        committed.producer_node_kind != _NODE_KIND
        or committed.execution_exchange_sha256 is None
        or artifact.artifact_kind != contract.artifact_kind
        or artifact.identity_contract != contract
        or artifact.owning_identity_contract_sha256 != contract.contract_sha256
        or artifact.locator is None
        or artifact.sha256 is None
        or artifact.byte_count is None
        or artifact.producer_node_id is None
        or artifact.producer_attempt_id is None
        or artifact.producer_attempt_authorization_sha256 is None
        or artifact.scope.sample_id != resolved_sample.sample_id
        or artifact.scope.slide_id != resolved_sample.slide_id
        or artifact.scope.validation_domain_id != resolved_sample.validation_domain_id
        or identity["sample_id"] != resolved_sample.sample_id
        or identity["slide_id"] != resolved_sample.slide_id
        or any(
            result.analysis_config_sha256 != resolved_sample.resolved_config_sha256
            for result in handoff.domain_results.values()
        )
    ):
        raise BatchArtifactError("committed phenotype handoff owner differs")
    try:
        reopened = exchange_reopener._reopen_for_owner(
            committed,
            producer_artifact_ref_sha256=artifact.artifact_ref_sha256,
            producer_node_id=artifact.producer_node_id,
            producer_attempt_id=artifact.producer_attempt_id,
            producer_attempt_authorization_sha256=(artifact.producer_attempt_authorization_sha256),
            terminal_record_sha256=committed.terminal_record_sha256,
            execution_exchange_sha256=committed.execution_exchange_sha256,
            expected_binding=(
                build_phenotype_handoff_stage_spec().spec_sha256,
                QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
                _WIRE_STAGE,
                _NODE_KIND,
                _CODEC_VERSION,
            ),
        )
        evidence = reopened.evidence
    except (ProjectStageRecordError, TypeError, ValueError) as exc:
        raise BatchArtifactError("phenotype handoff retained exchange cannot be reopened") from exc
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError("phenotype handoff retained evidence is foreign")
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    result = evidence.wire_stage_result
    manifest = _mapping(evidence.generation_manifest, _MANIFEST_FIELDS, "handoff manifest")
    outputs = _mapping(result.outputs, _OUTPUT_FIELDS, "handoff outputs")
    input_refs = tuple(context.inputs)

    def refs_for(*kinds: str) -> tuple[str, ...]:
        selected = set(kinds)
        return tuple(
            sorted(
                item.artifact_ref_sha256 for item in input_refs if item.artifact_kind in selected
            )
        )

    expected_path = (
        Path(evidence.reopen_validation_inputs().operational_output_dir)
        / "exports"
        / resolved_sample.slide_id
        / "all-sections"
        / f"{outputs['sha256']}.qupath-raw-objects.json"
    )
    raw_path = Path(str(outputs["path"]))
    try:
        normalized_raw_path = raw_path.resolve(strict=True)
        normalized_raw_path.relative_to(Path(evidence.reopen_validation_inputs().operational_output_dir).resolve(strict=True))
        raw_byte_count = normalized_raw_path.stat().st_size
        if (
            raw_byte_count != identity["global_raw"]["byte_count"]
            or raw_byte_count > _MAXIMUM_RAW_BYTES
        ):
            raise ValueError("global raw byte count differs")
        raw_sha256 = _streaming_file_sha256(normalized_raw_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise BatchArtifactError("phenotype handoff global raw output is unavailable") from exc
    domain_result_inventory_sha256 = digest(
        [
            handoff.domain_results[domain_id].to_mapping()
            for domain_id in sorted(handoff.domain_results)
        ]
    )
    phenotype_kinds = {
        "marker_phenotype_result",
        "supervised_phenotype_result",
        "cluster_label_import_result",
        "phenotype_view_attestation",
    }
    expected_values = {
        "accepted_domain_ref_sha256s": refs_for("accepted_analysis_domain"),
        "segmentation_ref_sha256s": refs_for("segmentation_result"),
        "measurements_ref_sha256s": refs_for("measurements_result"),
        "phenotype_source_ref_sha256s": tuple(
            sorted(
                item.artifact_ref_sha256
                for item in input_refs
                if item.artifact_kind in phenotype_kinds
            )
        ),
        "semantic_raw_ref_sha256s": refs_for("semantic_raw_inventory"),
        "completion_ref_sha256s": refs_for(
            "no_secondary_completion_evidence", "semantic_finalization_result"
        ),
    }
    if (
        context.adapter_spec != build_phenotype_handoff_stage_spec()
        or authorized.adapter_spec_sha256 != context.adapter_spec.spec_sha256
        or authorized.node_id != identity["node_id"]
        or authorized.attempt_id != identity["attempt_id"]
        or authorized.attempt_authorization_sha256 != identity["attempt_authorization_sha256"]
        or authorized.request_codec_id != identity["request_codec_id"]
        or authorized.request_codec_version != identity["request_codec_version"]
        or authorized.wire_stage_id != _WIRE_STAGE
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.sample_id != identity["sample_id"]
        or authorized.project_entry_id != identity["project_entry_id"]
        or authorized.slide_id != identity["slide_id"]
        or result.status is not StageStatus.COMPLETED
        or result.stage != _WIRE_STAGE
        or result.slide_id != identity["slide_id"]
        or manifest["schema_version"] != "roi-generation-manifest/v1"
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _WIRE_STAGE
        or manifest["slide_id"] != identity["slide_id"]
        or canonical_bytes(manifest["object_outputs"]) != canonical_bytes(outputs)
        or evidence.execution_exchange_sha256 != identity["execution_exchange_sha256"]
        or committed.execution_exchange_sha256 != identity["execution_exchange_sha256"]
        or observation.saved_state_disposition != identity["global_raw"]["saved_state_disposition"]
        or observation.saved_state_before_sha256
        != identity["global_raw"]["saved_state_before_sha256"]
        or observation.saved_state_after_sha256
        != identity["global_raw"]["saved_state_after_sha256"]
        or raw_path != expected_path
        or normalized_raw_path != expected_path
        or outputs["sha256"] != identity["global_raw"]["sha256"]
        or raw_sha256 != outputs["sha256"]
        or artifact.locator != artifacts.descriptor.path
        or artifact.sha256 != artifacts.descriptor.sha256
        or artifact.byte_count != artifacts.descriptor.path.stat().st_size
        or identity["descriptor"]["sha256"] != artifacts.descriptor.sha256
        or identity["descriptor"]["byte_count"] != artifacts.descriptor.path.stat().st_size
        or identity["cell_artifact"]["sha256"] != artifacts.cells.sha256
        or identity["cell_artifact"]["byte_count"] != artifacts.cells.path.stat().st_size
        or identity["cell_count"] != handoff.cell_count
        or identity["cell_selection_sha256"] != handoff.cell_selection_sha256
        or identity["spatial_frame_sha256"] != handoff.spatial_frame.spatial_frame_sha256
        or identity["domain_set_sha256"] != handoff.domain_set.domain_set_sha256
        or identity["domain_result_inventory_sha256"] != domain_result_inventory_sha256
        or identity["selected_phenotype_view"] != handoff.selected_phenotype_view
        or identity["phenotype_generation_id"] != handoff.phenotype_generation_id
        or identity["phenotype_inventory_sha256"] != handoff.phenotype_inventory_sha256
        or identity["source_ref_set_sha256"] != artifact_ref_set_sha256(input_refs)
        or any(tuple(identity[field]) != expected for field, expected in expected_values.items())
        or {
            item.exported_inventory.source_evidence_sha256
            for item in handoff.domain_results.values()
        }
        != {outputs["sha256"]}
    ):
        raise BatchArtifactError("phenotype handoff retained exchange or nested authority differs")
    artifacts.descriptor.validate_file()
    artifacts.cells.validate_file()
    value = object.__new__(ValidatedPhenotypeHandoffLiveAuthority)
    for name, item in {
        "committed": committed,
        "handoff": handoff,
        "artifacts": artifacts,
        "global_raw_descriptor": MappingProxyType(
            {
                "path": str(normalized_raw_path),
                "sha256": str(outputs["sha256"]),
            }
        ),
        "_issuer": _LIVE_HANDOFF_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


def _validate_spec(spec: StageAdapterSpec) -> None:
    expected = build_phenotype_handoff_stage_spec()
    if type(spec) is not StageAdapterSpec or spec != expected:
        raise ProjectStageCodecError("phenotype handoff adapter specification differs")


def build_phenotype_handoff_project_stage_codec(
    spec: StageAdapterSpec,
    *,
    owner_resolver: PhenotypeHandoffAuthorityResolver,
) -> ProjectStageCodec:
    _validate_spec(spec)
    if type(owner_resolver) is not ProjectStageOwnerCapabilityResolver:
        raise ProjectStageCodecError("phenotype handoff codec requires exact owner resolver")
    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
        wire_stage_id=_WIRE_STAGE,
        terminal_stage_id=_NODE_KIND,
        request_codec_version=_CODEC_VERSION,
    )

    def project_request(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> Mapping[str, object]:
        try:
            if not intent.analysis_domain_bindings:
                raise BatchPhenotypingError("phenotype handoff target requires accepted domains")
            capability = owner_resolver._resolve_for_target(
                context,
                project_entry_id=intent.project_entry_id,
                analysis_domain_binding=intent.analysis_domain_bindings[0],
            )
            capability = _validate_capability(capability, context=context)
            if (
                intent.analysis_domain_bindings != _analysis_domain_bindings(capability)
                or intent.analysis_domain_bindings[0] != capability.target_binding
                or intent.sample_id != capability.resolved_sample.sample_id
                or intent.slide_id != capability.resolved_sample.slide_id
                or intent.pipeline_run_id != capability.segmentation_cells[0].pipeline_run_id
                or intent.resolved_sample is not capability.resolved_sample
                or intent.saved_state_before_sha256 != capability.saved_state_before_sha256
                or intent.prerequisite_artifact_ref_sha256s
                != capability.current.input_artifact_ref_sha256s
            ):
                raise BatchPhenotypingError("phenotype handoff Task-10 target differs")
            return capability.request_body
        except Exception:
            owner_resolver._retire_for_attempt(context)
            raise

    def validate_result(evidence: object) -> BatchNodeExecutionResult:
        if type(evidence) is not ProjectStageTargetExecutionEvidence:
            raise BatchPhenotypingError("phenotype handoff result requires exact evidence")
        context = evidence.context
        try:
            bindings = evidence.authorized.analysis_domain_bindings
            if not bindings:
                raise BatchPhenotypingError("phenotype handoff result target is empty")
            capability = owner_resolver._resolve_for_target(
                context,
                project_entry_id=evidence.authorized.project_entry_id,
                analysis_domain_binding=bindings[0],
            )
            return build_phenotype_handoff_execution_result(evidence, capability)
        finally:
            owner_resolver._retire_for_attempt(context)

    return ProjectStageCodec.for_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=project_request,
        result_validator=validate_result,
    )


__all__ = [
    "AuthorizedPhenotypeHandoff",
    "HANDOFF_IDENTITY_FIELDS",
    "PhenotypeHandoffAuthorityResolver",
    "build_phenotype_handoff_execution_result",
    "build_phenotype_handoff_project_stage_codec",
    "build_phenotype_handoff_stage_spec",
    "publish_phenotype_handoff_stage_owner_capability",
    "spatial_cell_handoff_identity_contract",
]
