"""Authenticated producer for one domain-scoped semantic raw export.

The `ValidatedSemanticRawSnapshotInvocationAuthority` record carries `current`,
`resolved_sample`, `accepted_tissue`, `accepted_domains`, `classifier_rois` and its
remaining declared fields.

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

from mif_pipeline.datasets.roi_inventory import open_roi_inventory, roi_inventory_member

from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    ValidatedAcceptedAnalysisDomainLiveAuthority,
    _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
)
from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
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
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_classifier_inference_artifacts import (
    ValidatedClassifierRoiLiveAuthority,
    _CLASSIFIER_ROI_LIVE_ISSUER,
    validate_primary_classifier_inference_result_artifact_ref,
    validate_secondary_classifier_inference_result_artifact_ref,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    ValidatedSegmentationCellLiveAuthority,
    _SEGMENTATION_CELL_LIVE_ISSUER,
    validate_segmentation_result_artifact_ref,
)
from mif_pipeline.orchestration.batch_tissue_artifacts import (
    ValidatedAcceptedTissueLiveAuthority,
    _validate_accepted_tissue_live_authority,
)
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageCodecError,
    ProjectStageTargetIntent,
    _ProjectStageRequestProjection,
)
from mif_pipeline.orchestration.project_stage_owner_capabilities import (
    ProjectStageOwnerCapabilityPublisher,
    ProjectStageOwnerCapabilityResolver,
)
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageTargetExecutionEvidence,
)
from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.pipeline_core.stage_result import StageStatus
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    plain_json,
    read_bounded_file,
)
from .application_codec import decode_committed_cluster_label_import_result
from .application_records import SEMANTIC_LIVE_BINDING_FIELDS, semantic_raw_inventory_identity_contract
from .semantic_records import (
    ValidatedPhenotypeApplicationProjection,
    normalize_committed_phenotype_application,
    validate_phenotype_application_projection,
)

_NODE_KIND = "semantic_raw_inventory_snapshot"
_WIRE_STAGE = "exports"
_CODEC_VERSION = "1"
_MAXIMUM_RAW_BYTES = 512 * 1024 * 1024
_CAPABILITY_ISSUER = object()
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


# ==============================================================================
#                             Semantic raw producer
# ==============================================================================


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _mapping(value: object, *, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BatchPhenotypingError(f"{label} does not have its exact closed fields")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchPhenotypingError(f"{label} must be an array")
    return tuple(value)


def _source_facts(
    authority: BatchCommittedArtifactAuthority,
) -> tuple[object, BatchArtifactRef, str, Mapping[str, object]]:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchPhenotypingError("semantic raw phenotype source requires committed authority")
    kind = authority.artifact_ref.artifact_kind
    if kind in {"marker_phenotype_result", "supervised_phenotype_result"}:
        projection = normalize_committed_phenotype_application((authority,))
        projection = validate_phenotype_application_projection(projection)
        identity = projection.source_artifact_ref.owning_identity
        return (
            projection,
            projection.source_artifact_ref,
            projection.phenotype_mode,
            identity,
        )
    if kind == "cluster_label_import_result":
        projection = decode_committed_cluster_label_import_result(authority)
        return (
            projection,
            projection.artifact_ref,
            "unsupervised_clustering",
            projection.identity,
        )
    raise BatchPhenotypingError(
        "semantic raw snapshot requires exactly one supported phenotype source"
    )


def _projection_state(value: object) -> tuple[str, str]:
    return (str(value.state_head_sha256), str(value.active_plan_sha256))


def _source_value(
    projection: object,
    identity: Mapping[str, object],
    field: str,
) -> object:
    if isinstance(projection, ValidatedPhenotypeApplicationProjection) and hasattr(
        projection, field
    ):
        return getattr(projection, field)
    try:
        return identity[field]
    except KeyError as exc:
        raise BatchPhenotypingError(f"semantic raw phenotype source lacks {field}") from exc


def _accepted_tissue_authority_rows(
    tissue: ValidatedAcceptedTissueLiveAuthority,
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for section_authority in tissue.sections:
        exported = section_authority.section.to_export_mapping()
        geometry = plain_json(exported["geometry"])
        rows.append(
            MappingProxyType(
                {
                    "schema_version": "accepted-tissue-section-live-authority/v1",
                    "slide_id": section_authority.section.slide_id,
                    "section_id": section_authority.section.section_id,
                    "tissue_roi_id": section_authority.section.roi_id,
                    "tissue_path_object_id": (section_authority.section.path_object_id),
                    "tissue_generation_id": (section_authority.section.generation_id),
                    "revision": section_authority.section.revision,
                    "geometry_digest_policy": (section_authority.geometry_digest_policy),
                    "geometry_sha256": section_authority.geometry_sha256,
                    "geometry": _freeze(geometry),
                }
            )
        )
    return tuple(sorted(rows, key=lambda item: str(item["section_id"])))


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticRawSnapshotInvocationAuthority:
    """Sealed same-attempt join of all accepted semantic snapshot owners."""

    current: BatchCurrentExecutionAuthority
    resolved_sample: ResolvedBatchSample
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority
    classifier_rois: ValidatedClassifierRoiLiveAuthority
    segmentation_cells: ValidatedSegmentationCellLiveAuthority
    measurements: ValidatedMeasurementsResultProjection
    phenotype_source: object
    phenotype_source_ref: BatchArtifactRef
    phenotype_mode: str
    phenotype_source_identity: Mapping[str, object]
    target_binding: AnalysisDomainBinding
    target_section_id: str
    request_body: Mapping[str, object]
    cell_ids: tuple[str, ...]
    cell_to_domain_membership_sha256: str
    configured_secondary_ids: tuple[str, ...]
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedSemanticRawSnapshotInvocationAuthority is issuer-only")

    def __copy__(self) -> object:
        raise TypeError("semantic raw snapshot authority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("semantic raw snapshot authority is noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("semantic raw snapshot authority is nonserializable")


def _roi_sources(classifier_rois):
    """Describe the accepted ROI tables used by the native semantic calculation.

    We retain each producing attempt so the native reader derives the exact
    allowed table member. The existing committed-result reference binds its
    image, analysis region, model and saved revision.
    """
    return [
        {"source_kind": "primary" if item is classifier_rois.primary else "secondary",
         "source_result_ref_sha256": item.artifact_ref.artifact_ref_sha256,
         "producer_attempt_id": item.artifact_ref.producer_attempt_id,
         "roi_inventory": item.roi_inventory.to_mapping(),
         "structure_id": None if item is classifier_rois.primary else item.structure_id}
        for item in classifier_rois.projections
    ]


def _roi_batches(projection, producer_output_dirs, columns):
    """Read the selected identity columns from one completed classifier table.

    Hashing and Parquet reads use the same retained file. We derive its member
    from the accepted producing attempt and close it after the bounded batches
    have supplied the IDs needed by this operation.
    """
    with open_roi_inventory(
        projection.roi_inventory, admitted_root=producer_output_dirs[projection.artifact_ref.producer_attempt_id],
        expected_member=roi_inventory_member(projection.artifact_ref.producer_attempt_id),
    ) as table:
        yield from table.iter_batches(columns=columns)


def _capability_material(
    value: ValidatedSemanticRawSnapshotInvocationAuthority,
) -> Mapping[str, object]:
    return {
        "capability_object_identity": id(value),
        "current_object_identity": id(value.current),
        "resolved_sample_object_identity": id(value.resolved_sample),
        "accepted_tissue_object_identity": id(value.accepted_tissue),
        "accepted_domains_object_identity": id(value.accepted_domains),
        "classifier_rois_object_identity": id(value.classifier_rois),
        "segmentation_cells_object_identity": id(value.segmentation_cells),
        "measurements_object_identity": id(value.measurements),
        "phenotype_source_object_identity": id(value.phenotype_source),
        "phenotype_source_ref_sha256": (value.phenotype_source_ref.artifact_ref_sha256),
        "phenotype_mode": value.phenotype_mode,
        "target_binding": value.target_binding.to_mapping(),
        "target_section_id": value.target_section_id,
        "request_body": plain_json(value.request_body),
        "cell_ids": list(value.cell_ids),
        "cell_to_domain_membership_sha256": (value.cell_to_domain_membership_sha256),
        "configured_secondary_ids": list(value.configured_secondary_ids),
    }


def _validate_capability(
    value: object,
    *,
    context: BatchNodeExecutionContext,
) -> ValidatedSemanticRawSnapshotInvocationAuthority:
    if (
        type(value) is not ValidatedSemanticRawSnapshotInvocationAuthority
        or value._issuer is not _CAPABILITY_ISSUER
        or value._seal_sha256 != digest(_capability_material(value))
    ):
        raise BatchPhenotypingError("semantic raw snapshot capability is foreign")
    validate_current_execution_authority(value.current, expected_context=context)
    return value


def _validate_live_authorities(
    *,
    accepted_tissue: object,
    accepted_domains: object,
    classifier_rois: object,
    segmentation_cells: object,
) -> tuple[
    ValidatedAcceptedTissueLiveAuthority,
    ValidatedAcceptedAnalysisDomainLiveAuthority,
    ValidatedClassifierRoiLiveAuthority,
    ValidatedSegmentationCellLiveAuthority,
]:
    """Associate selected immutable records with this native snapshot.

    The domain owner already decoded the manifests and selected their current
    revisions. We reuse those records while comparing the selected revisions,
    tissue association and classifier/segmentation populations consumed by
    this snapshot.
    """

    tissue = _validate_accepted_tissue_live_authority(accepted_tissue)
    if (
        type(accepted_domains) is not ValidatedAcceptedAnalysisDomainLiveAuthority
        or accepted_domains._issuer is not _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER
        or accepted_domains.accepted_tissue is not tissue
    ):
        raise BatchPhenotypingError("accepted analysis-domain live authority is foreign")
    domain_authorities = accepted_domains.domain_authorities
    latest = tuple(
        manifest.revisions[-1] for manifest in accepted_domains.manifests if manifest.revisions
    )
    if (
        tuple(item.analysis_domain_id for item in domain_authorities)
        != tuple(sorted(item.analysis_domain_id for item in domain_authorities))
        or {
            (
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
            )
            for item in domain_authorities
        }
        != {(item.analysis_domain_id, item.revision, item.content_sha256) for item in latest}
    ):
        raise BatchPhenotypingError(
            "accepted analysis-domain manifest history or inventory changed"
        )
    if (
        type(classifier_rois) is not ValidatedClassifierRoiLiveAuthority
        or classifier_rois._issuer is not _CLASSIFIER_ROI_LIVE_ISSUER
    ):
        raise BatchPhenotypingError("classifier ROI live authority is foreign")
    try:
        if classifier_rois.primary is None:
            from ..batch_training.planning import validate_primary_skip_artifact_ref
            validate_primary_skip_artifact_ref(
                classifier_rois.classification_authority.artifact_ref,
                domain_bindings=(classifier_rois.analysis_domain_binding,),
            )
        else:
            validate_primary_classifier_inference_result_artifact_ref(classifier_rois.primary.artifact_ref)
        for item in classifier_rois.secondaries:
            validate_secondary_classifier_inference_result_artifact_ref(item.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("classifier ROI live owner is invalid") from exc
    if (
        classifier_rois.primary_roi_inventory != (None if classifier_rois.primary is None else classifier_rois.primary.roi_inventory)
        or classifier_rois.secondary_roi_inventories != tuple(
            (item.structure_id, item.roi_inventory) for item in classifier_rois.secondaries
        )
        or classifier_rois.domain_status != ("active" if classifier_rois.primary is None else classifier_rois.primary.domain_status)
    ):
        raise BatchPhenotypingError("classifier ROI table selection changed")
    if (
        type(segmentation_cells) is not ValidatedSegmentationCellLiveAuthority
        or segmentation_cells._issuer is not _SEGMENTATION_CELL_LIVE_ISSUER
        or segmentation_cells.accepted_domains is not accepted_domains
    ):
        raise BatchPhenotypingError("segmentation cell live authority is foreign")
    try:
        validate_segmentation_result_artifact_ref(segmentation_cells.committed.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("segmentation cell live owner is invalid") from exc
    return tissue, accepted_domains, classifier_rois, segmentation_cells


def _target_request_body(
    *,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    tissue: ValidatedAcceptedTissueLiveAuthority,
    domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    rois: ValidatedClassifierRoiLiveAuthority,
    cells: ValidatedSegmentationCellLiveAuthority,
    measurements: ValidatedMeasurementsResultProjection,
    source_projection: object,
    source_ref: BatchArtifactRef,
    source_mode: str,
    source_identity: Mapping[str, object],
) -> tuple[
    AnalysisDomainBinding,
    str,
    Mapping[str, object],
    tuple[str, ...],
    str,
    tuple[str, ...],
]:
    if len(current.analysis_domain_bindings) != 1:
        raise BatchPhenotypingError("semantic raw snapshot requires one target domain")
    binding = current.analysis_domain_bindings[0]
    matching = tuple(
        item
        for item in domains.domain_authorities
        if (
            item.analysis_domain_id == binding.analysis_domain_id
            and item.analysis_domain_revision == binding.analysis_domain_revision
            and item.analysis_domain_revision_sha256 == binding.analysis_domain_revision_sha256
        )
    )
    if len(matching) != 1:
        raise BatchPhenotypingError("semantic target domain is not the exact live revision")
    target = matching[0]
    target_section_id = target.section_id
    target_cells = tuple(sorted(
        cell_id
        for batch in cells.cell_dataset.iter_batches(columns=("cell_id", "analysis_domain_id"))
        for cell_id, domain_id in zip(batch.column("cell_id").to_pylist(),
                                    batch.column("analysis_domain_id").to_pylist())
        if domain_id == binding.analysis_domain_id
    ))
    target_membership = {cell_id: binding.analysis_domain_id for cell_id in target_cells}
    source_cell_ids_sha256 = _source_value(source_projection, source_identity, "cell_ids_sha256")
    source_membership_sha256 = _source_value(
        source_projection, source_identity, "cell_to_domain_membership_sha256"
    )
    source_saved_state = _source_value(
        source_projection, source_identity, "saved_state_after_sha256"
    )
    source_saved_state_before = _source_value(
        source_projection, source_identity, "saved_state_before_sha256"
    )
    source_bindings = tuple(source_ref.scope.analysis_domain_bindings)
    configuration_binding = ArtifactConfigurationBinding(
        binding.sample_id,
        resolved_sample.resolved_config_sha256,
    )
    classifier_chain = rois.projections
    supervised_source = source_ref.artifact_kind == "supervised_phenotype_result"
    raw_source_refs = source_identity.get("source_ref_sha256s")
    if supervised_source:
        source_measurement_lineage = (
            isinstance(raw_source_refs, (tuple, list))
            and measurements.artifact_ref.artifact_ref_sha256 in tuple(raw_source_refs)
            and source_ref.scope.validation_domain_id
            == measurements.artifact_ref.scope.validation_domain_id
            == current.validation_domain_id
            == resolved_sample.validation_domain_id
        )
    else:
        source_measurement_lineage = (
            isinstance(raw_source_refs, (tuple, list))
            and measurements.artifact_ref.artifact_ref_sha256 in tuple(raw_source_refs)
            and _source_value(source_projection, source_identity, "canonical_image_id")
            == measurements.canonical_image_id
            and _source_value(source_projection, source_identity, "canonical_image_sha256")
            == measurements.canonical_image_sha256
            and _source_value(source_projection, source_identity, "segmentation_population_id")
            == measurements.segmentation_population_id
            and _source_value(source_projection, source_identity, "measurement_generation_id")
            == measurements.measurement_generation_id
        )
    expected_common = (
        binding.sample_id == resolved_sample.sample_id
        and binding.slide_id == resolved_sample.slide_id
        and binding.domain_set_sha256 == domains.domain_set.domain_set_sha256
        and current.configuration_bindings == (configuration_binding,)
        and tissue.committed.sample_id == binding.sample_id
        and tissue.committed.slide_id == binding.slide_id
        and tissue.committed.project_entry_id == binding.project_entry_id
        and tissue.committed.canonical_image_id == target.canonical_image_id
        and tissue.committed.canonical_image_sha256 == target.canonical_image_sha256
        and tissue.committed.generation_id == target.tissue_generation_id
        and domains.committed.sample_id == binding.sample_id
        and domains.committed.slide_id == binding.slide_id
        and domains.committed.project_entry_id == binding.project_entry_id
        and domains.committed.canonical_image_id == target.canonical_image_id
        and domains.committed.canonical_image_sha256 == target.canonical_image_sha256
        and domains.committed.tissue_generation_id == target.tissue_generation_id
        and domains.committed.domain_set.domain_set_sha256 == binding.domain_set_sha256
        and rois.analysis_domain_binding == binding
        and rois.pipeline_run_id == cells.pipeline_run_id
        and all(item.analysis_domain_bindings == (binding,) for item in classifier_chain)
        and all(
            item.canonical_image_id == target.canonical_image_id
            and item.canonical_image_sha256 == target.canonical_image_sha256
            and item.section_id == target_section_id
            and item.tissue_generation_id == target.tissue_generation_id
            and item.analysis_domain_path_object_id == target.source_path_object_id
            and item.pipeline_run_id == cells.pipeline_run_id
            and item.artifact_ref.scope.sample_id == binding.sample_id
            and item.artifact_ref.scope.slide_id == binding.slide_id
            and item.artifact_ref.scope.project_entry_id == binding.project_entry_id
            and item.artifact_ref.configuration_bindings == (configuration_binding,)
            for item in classifier_chain
        )
        and (rois.primary is None or domains.committed.artifact_ref.artifact_ref_sha256
             in rois.primary.artifact_ref.owning_identity["prerequisite_artifact_ref_sha256s"])
        and all(
            successor.predecessor_result_ref_sha256 == predecessor.artifact_ref.artifact_ref_sha256
            for predecessor, successor in zip(
                classifier_chain,
                classifier_chain[1:],
                strict=False,
            )
        )
        and cells.committed.sample_id == binding.sample_id
        and cells.committed.slide_id == binding.slide_id
        and cells.committed.project_entry_id == binding.project_entry_id
        and cells.committed.canonical_image_id == target.canonical_image_id
        and cells.committed.canonical_image_sha256 == target.canonical_image_sha256
        and cells.committed.section_id == target_section_id
        and cells.committed.tissue_generation_id == target.tissue_generation_id
        and cells.committed.tissue_path_object_id == target.tissue_path_object_id
        and cells.committed.tissue_roi_id == target.tissue_roi_id
        and binding in cells.committed.analysis_domain_bindings
        and cells.committed.domain_set_sha256 == binding.domain_set_sha256
        and (not cells.committed.primary_inference_result_ref_sha256s if rois.primary is None else
             rois.primary.artifact_ref.artifact_ref_sha256 in cells.committed.primary_inference_result_ref_sha256s)
        and cells.committed.artifact_ref.configuration_bindings == (configuration_binding,)
        and measurements.analysis_domain_bindings == source_bindings
        and binding in source_bindings
        and measurements.sample_id == binding.sample_id
        and measurements.slide_id == binding.slide_id
        and measurements.project_entry_id == binding.project_entry_id
        and measurements.canonical_image_id == target.canonical_image_id
        and measurements.canonical_image_sha256 == target.canonical_image_sha256
        and measurements.section_id == target_section_id
        and measurements.tissue_generation_id == target.tissue_generation_id
        and measurements.segmentation_population_id == cells.committed.segmentation_population_id
        and measurements.segmentation_generation_id == cells.committed.generation_ids[0]
        and measurements.artifact_ref.owning_identity["segmentation_result_ref_sha256"]
        == cells.committed.artifact_ref.artifact_ref_sha256
        and measurements.artifact_ref.configuration_bindings == (configuration_binding,)
        and measurements.cell_ids_sha256 == source_cell_ids_sha256
        and measurements.cell_to_domain_membership_sha256 == source_membership_sha256
        and measurements == cells.measurement_result
        and _source_value(source_projection, source_identity, "sample_id") == binding.sample_id
        and _source_value(source_projection, source_identity, "slide_id") == binding.slide_id
        and _source_value(source_projection, source_identity, "project_entry_id")
        == binding.project_entry_id
        and source_measurement_lineage
        and _source_value(source_projection, source_identity, "section_id") == target_section_id
        and _source_value(source_projection, source_identity, "tissue_generation_id")
        == target.tissue_generation_id
        and source_ref.configuration_bindings == (configuration_binding,)
        and source_saved_state != source_saved_state_before
    )
    if not expected_common:
        raise BatchPhenotypingError(
            "semantic raw owner sample, domain, cell, or source lineage differs"
        )
    # The classifier ROI owner has already joined the complete selected
    # definition set to its exact committed results and original config binding.
    configured_secondary_ids = tuple(
        sorted(structure_id for structure_id, _ in rois.secondary_roi_inventories)
    )
    manifests = tuple(item.to_mapping() for item in domains.manifests)
    tissue_rows = _accepted_tissue_authority_rows(tissue)
    if not {item.section_id for item in domains.manifests}.issubset(
        item["section_id"] for item in tissue_rows
    ):
        raise BatchPhenotypingError("semantic tissue and domain histories differ")
    target_revision = next(
        revision
        for manifest in domains.manifests
        for revision in manifest.revisions
        if revision.content_sha256 == binding.analysis_domain_revision_sha256
    )
    request = {
        "schema_version": "roi-workflow-invocation/v1",
        "stage": "semantic_validation",
        "sample_id": binding.sample_id,
        "slide_id": binding.slide_id,
        "run_id": cells.pipeline_run_id,
        "generation_ids": [item.generation_id for item in tissue.generations],
        "generation_id": target.tissue_generation_id,
        "canonical_image_id": target.canonical_image_id,
        "canonical_image_sha256": measurements.canonical_image_sha256,
        "section_id": target_section_id,
        "analysis_domain_id": binding.analysis_domain_id,
        "analysis_domain_revision": binding.analysis_domain_revision,
        "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
        "analysis_domain_path_object_id": target.source_path_object_id,
        "action": None,
        "action_context": {},
        "classifier_reuse_decisions": {},
        "confirmed_marker_rules": {},
        "registration_context": None,
        "domain_set_sha256": binding.domain_set_sha256,
        "analysis_domain_selection_status": "accepted",
        "analysis_domain_manifests": list(manifests),
        "accepted_tissue_section_authorities": [plain_json(item) for item in tissue_rows],
        "analysis_domain_revision_evidence": target_revision.to_mapping(),
        "semantic_roi_sources": _roi_sources(rois),
        "semantic_cell_dataset": plain_json(cells.cell_dataset_ref),
        "semantic_cell_count": len(target_cells),
        "semantic_cell_ids_sha256": digest(list(target_cells)),
        "segmentation_population_id": measurements.segmentation_population_id,
        "segmentation_population_ids": {target_section_id: measurements.segmentation_population_id},
        "selected_measurement_names": list(measurements.selected_measurement_names),
        "selected_phenotype_view": source_mode,
        "phenotype_generation_id": source_identity["phenotype_generation_id"],
    }
    request["semantic_live_binding"] = {
        field: request[field] for field in SEMANTIC_LIVE_BINDING_FIELDS
    }
    if rois.primary is None:
        request["primary_classification_skipped"] = True
        request["semantic_live_binding"]["primary_classification_skipped"] = True
    # The runtime supplies the top-level image hash in the native envelope.
    # Retain it in the scientific binding that the exported result must match.
    del request["canonical_image_sha256"]
    # Select the accepted phenotype component explicitly, independently of the
    # result currently displayed in QuPath.
    frozen = _freeze(request)
    assert isinstance(frozen, Mapping)
    return (
        binding,
        target_section_id,
        frozen,
        target_cells,
        digest(target_membership),
        configured_secondary_ids,
    )


def _input_refs(
    *,
    context: BatchNodeExecutionContext,
    tissue: ValidatedAcceptedTissueLiveAuthority,
    domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    rois: ValidatedClassifierRoiLiveAuthority,
    cells: ValidatedSegmentationCellLiveAuthority,
    measurement_authority: BatchCommittedArtifactAuthority,
    source_authority: BatchCommittedArtifactAuthority,
) -> None:
    expected = {
        tissue.committed.artifact_ref.artifact_ref_sha256,
        domains.committed.artifact_ref.artifact_ref_sha256,
        rois.classification_authority.artifact_ref.artifact_ref_sha256,
        *(item.artifact_ref.artifact_ref_sha256 for item in rois.secondaries),
        cells.committed.artifact_ref.artifact_ref_sha256,
        measurement_authority.artifact_ref.artifact_ref_sha256,
        source_authority.artifact_ref.artifact_ref_sha256,
    }
    actual = {item.artifact_ref_sha256 for item in context.inputs}
    analysis_frames = tuple(
        item for item in context.inputs if item.artifact_kind == "analysis_frame"
    )
    if (
        len(analysis_frames) != 1
        or analysis_frames[0].artifact_ref_sha256 != tissue.committed.analysis_frame_ref_sha256
        or actual != expected | {analysis_frames[0].artifact_ref_sha256}
    ):
        raise BatchPhenotypingError(
            "semantic raw snapshot inputs differ from the exact live owner set"
        )


def publish_semantic_raw_snapshot_invocation_authority(
    publisher: ProjectStageOwnerCapabilityPublisher,
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    resolved_sample: ResolvedBatchSample,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    classifier_rois: ValidatedClassifierRoiLiveAuthority,
    segmentation_cells: ValidatedSegmentationCellLiveAuthority,
    measurement_authority: BatchCommittedArtifactAuthority,
    phenotype_source_authority: BatchCommittedArtifactAuthority,
) -> ValidatedSemanticRawSnapshotInvocationAuthority:
    """Authenticate and publish one D-rem-6 capability after runtime open."""

    if type(publisher) is not ProjectStageOwnerCapabilityPublisher:
        raise BatchPhenotypingError("semantic raw publisher view is invalid")
    if type(context) is not BatchNodeExecutionContext:
        raise BatchPhenotypingError("semantic raw publication requires exact context")
    validated_current = validate_current_execution_authority(current, expected_context=context)
    if type(resolved_sample) is not ResolvedBatchSample:
        raise BatchPhenotypingError("semantic raw resolved sample is invalid")
    if (
        validated_current.node_kind != _NODE_KIND
        or validated_current.participant_ids != (resolved_sample.sample_id,)
        or validated_current.project_entry_ids
        != context.authorization.dispatch_binding.project_entry_ids
        or type(resolved_sample.config) is not AnalysisConfig
        or not resolved_sample.enabled
        or resolved_sample.config.slide_id != resolved_sample.slide_id
        or digest(resolved_sample.config.to_mapping()) != resolved_sample.resolved_config_sha256
    ):
        raise BatchPhenotypingError("semantic raw current execution or config differs")
    tissue, domains, rois, cells = _validate_live_authorities(
        accepted_tissue=accepted_tissue,
        accepted_domains=accepted_domains,
        classifier_rois=classifier_rois,
        segmentation_cells=segmentation_cells,
    )
    try:
        measurement_authority, phenotype_source_authority = (
            validate_committed_artifact_authority_set(
                (measurement_authority, phenotype_source_authority),
                expected_active_plan_sha256=validated_current.active_plan_sha256,
            )
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "semantic raw committed measurement/source authority differs"
        ) from exc
    by_kind = {
        measurement_authority.artifact_ref.artifact_kind: measurement_authority,
        phenotype_source_authority.artifact_ref.artifact_kind: phenotype_source_authority,
    }
    if "measurements_result" not in by_kind or len(by_kind) != 2:
        raise BatchPhenotypingError("semantic raw measurement/source kinds differ")
    measurement_authority = by_kind["measurements_result"]
    phenotype_source_authority = next(
        item for kind, item in by_kind.items() if kind != "measurements_result"
    )
    measurements = decode_committed_measurements_result(measurement_authority)
    source_projection, source_ref, source_mode, source_identity = _source_facts(
        phenotype_source_authority
    )
    state_plan = (
        validated_current.state_head_sha256,
        validated_current.active_plan_sha256,
    )
    projections = (
        tissue.committed,
        domains.committed,
        rois.classification_authority,
        *rois.secondaries,
        cells.committed,
        measurements,
        source_projection,
    )
    if any(_projection_state(item) != state_plan for item in projections):
        raise BatchPhenotypingError("semantic raw live owners use foreign state or plan")
    _input_refs(
        context=context,
        tissue=tissue,
        domains=domains,
        rois=rois,
        cells=cells,
        measurement_authority=measurement_authority,
        source_authority=phenotype_source_authority,
    )
    (
        binding,
        section_id,
        request_body,
        cell_ids,
        membership_sha256,
        configured_secondary_ids,
    ) = _target_request_body(
        current=validated_current,
        resolved_sample=resolved_sample,
        tissue=tissue,
        domains=domains,
        rois=rois,
        cells=cells,
        measurements=measurements,
        source_projection=source_projection,
        source_ref=source_ref,
        source_mode=source_mode,
        source_identity=source_identity,
    )
    value = object.__new__(ValidatedSemanticRawSnapshotInvocationAuthority)
    values = {
        "current": validated_current,
        "resolved_sample": resolved_sample,
        "accepted_tissue": tissue,
        "accepted_domains": domains,
        "classifier_rois": rois,
        "segmentation_cells": cells,
        "measurements": measurements,
        "phenotype_source": source_projection,
        "phenotype_source_ref": source_ref,
        "phenotype_mode": source_mode,
        "phenotype_source_identity": source_identity,
        "target_binding": binding,
        "target_section_id": section_id,
        "request_body": request_body,
        "cell_ids": cell_ids,
        "cell_to_domain_membership_sha256": membership_sha256,
        "configured_secondary_ids": configured_secondary_ids,
        "_seal_sha256": "",
        "_issuer": _CAPABILITY_ISSUER,
    }
    for name, item in values.items():
        object.__setattr__(value, name, item)
    object.__setattr__(value, "_seal_sha256", digest(_capability_material(value)))
    publisher._publish_current(value, context=context, current=validated_current)
    return value


def _resolve(
    resolver: ProjectStageOwnerCapabilityResolver,
    context: BatchNodeExecutionContext,
    *,
    project_entry_id: str,
    binding: AnalysisDomainBinding,
) -> ValidatedSemanticRawSnapshotInvocationAuthority:
    value = resolver._resolve_for_target(
        context,
        project_entry_id=project_entry_id,
        analysis_domain_binding=binding,
    )
    return _validate_capability(value, context=context)


def _validate_raw_bytes(
    raw: object,
    capability: ValidatedSemanticRawSnapshotInvocationAuthority,
) -> None:
    """Validate raw bytes for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    raw = _mapping(
        raw,
        fields=frozenset(
            {
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
                "semantic_live_binding",
                "phenotype_components",
            }
        ),
        label="semantic raw export",
    )
    binding = capability.target_binding
    if canonical_bytes(raw["semantic_live_binding"]) != canonical_bytes(
        capability.request_body["semantic_live_binding"]
    ):
        raise BatchPhenotypingError("semantic raw accepted live binding changed")
    tissue_rows = _sequence(raw["tissue_sections"], "raw tissue_sections")
    roi_rows = _sequence(raw["rois"], "raw rois")
    summary = raw["cell_summary"]
    source_generation = str(capability.phenotype_source_identity["phenotype_generation_id"])
    target_tissue = next(
        item
        for item in capability.accepted_tissue.sections
        if item.section.section_id == capability.target_section_id
    )
    expected_pairs = sorted(
        pair
        for projection in capability.classifier_rois.projections
        for batch in _roi_batches(projection, capability.classifier_rois.producer_output_dirs, ("roi_id", "path_object_id"))
        for pair in zip(batch.column("roi_id").to_pylist(), batch.column("path_object_id").to_pylist())
    )
    expected_roi_ids = tuple(pair[0] for pair in expected_pairs)
    expected_roi_paths = tuple(pair[1] for pair in expected_pairs)
    observed_roi_ids = tuple(
        str(item.get("roi_id")) for item in roi_rows if isinstance(item, Mapping)
    )
    observed_roi_paths = tuple(
        str(item.get("path_object_id")) for item in roi_rows if isinstance(item, Mapping)
    )
    if (
        raw["schema_version"] != "qupath-raw-objects/v1"
        or raw["slide_id"] != binding.slide_id
        or raw["generation_id"] != capability.measurements.tissue_generation_id
        or raw["pipeline_run_id"] != capability.segmentation_cells.pipeline_run_id
        or raw["coordinate_system"] != "global_level0"
        or len(tissue_rows) != 1
        or not isinstance(tissue_rows[0], Mapping)
        or tissue_rows[0].get("slide_id") != binding.slide_id
        or tissue_rows[0].get("section_id") != capability.target_section_id
        or tissue_rows[0].get("roi_id") != target_tissue.section.roi_id
        or tissue_rows[0].get("generation_id") != target_tissue.section.generation_id
        or tissue_rows[0].get("revision") != target_tissue.section.revision
        or digest(tissue_rows[0].get("geometry")) != target_tissue.geometry_sha256
        or observed_roi_ids != expected_roi_ids
        or observed_roi_paths != expected_roi_paths
        or not isinstance(summary, Mapping)
        or summary.get("row_count") != len(capability.cell_ids)
        or summary.get("cell_ids_sha256") != digest(list(capability.cell_ids))
        or summary.get("domain_cell_mapping_sha256") != capability.cell_to_domain_membership_sha256
    ):
        raise BatchPhenotypingError("semantic raw export inventory or scope differs")
    from mif_pipeline.orchestration.analysis_services import _raw_cell_dataset
    from mif_pipeline.cell_analysis.measurement_rows import MarkerIntensityRowsDescriptor, read_measurement_dataset_selection
    rows = capability.measurements.measurement_rows
    measurement_selection = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(rows.path, rows.sha256, rows.byte_count, rows.row_count, rows.content_schema_version),
        authenticated_output_root=rows.path.parent.parent.parent)
    dataset = _raw_cell_dataset(raw)
    try:
        selectors = raw["cell_datasets"]
        selected = next((item for item in selectors if item["section_id"] == capability.target_section_id), None)
        expected_view = {"mode": capability.phenotype_mode, "phenotype_generation_id": source_generation}
        expected_reference = capability.phenotype_source_identity["component_descriptor"]
        actual_reference = raw["phenotype_components"].get(capability.phenotype_mode, {}).get(source_generation, {}).get(capability.target_section_id)
        if (len(selectors) != 1 or selected is None
                or selected["population_id"] != capability.measurements.segmentation_population_id
                or selected["cell_dataset"] != {key: capability.segmentation_cells.cell_dataset_ref[key] for key in ("path", "sha256")}
                or selected["phenotype_view"] != expected_view or actual_reference != plain_json(expected_reference)
                or selected["geometry_revision"] != measurement_selection["geometry_revision"]
                or any(selected["components"].get(field) != scopes
                    for field, scopes in measurement_selection["components"].items())
                or raw["cell_scope"] != {"active_analysis_domain_ids": [binding.analysis_domain_id]}
                or not {"measurement:" + name for name in capability.measurements.selected_measurement_names}.issubset(dataset.schema.names)):
            raise BatchPhenotypingError("semantic raw selected population, measurement, or phenotype differs")
    finally:
        dataset.close()


def _build_result(
    evidence: ProjectStageTargetExecutionEvidence,
    capability: ValidatedSemanticRawSnapshotInvocationAuthority,
    *,
    spec: StageAdapterSpec,
    binding: ProjectStageCodecBinding,
) -> BatchNodeExecutionResult:
    context = evidence.context
    authorized = evidence.authorized
    result = evidence.wire_stage_result
    if (
        context.adapter_spec != spec
        or authorized.adapter_spec_sha256 != spec.spec_sha256
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.request_codec_id != binding.target_script_asset_id
        or authorized.request_codec_version != _CODEC_VERSION
        or authorized.wire_stage_id != _WIRE_STAGE
        or result.stage != _WIRE_STAGE
        or authorized.project_entry_id != capability.target_binding.project_entry_id
        or authorized.analysis_domain_bindings != (capability.target_binding,)
    ):
        raise BatchPhenotypingError("semantic raw Task-10 execution identity differs")
    if result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    validation_inputs = evidence.reopen_validation_inputs()
    if canonical_bytes(validation_inputs.scientific_request_body) != canonical_bytes(
        capability.request_body
    ) or canonical_bytes(validation_inputs.resolved_config) != canonical_bytes(
        capability.resolved_sample.config.to_mapping()
    ):
        raise BatchPhenotypingError("semantic raw retained request or config differs")
    manifest = _mapping(
        evidence.generation_manifest,
        fields=_MANIFEST_FIELDS,
        label="semantic raw generation manifest",
    )
    outputs = _mapping(result.outputs, fields=_OUTPUT_FIELDS, label="semantic raw outputs")
    if (
        manifest["schema_version"] != "roi-generation-manifest/v1"
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _WIRE_STAGE
        or manifest["slide_id"] != capability.target_binding.slide_id
        or tuple(_sequence(manifest["generation_ids"], "generation_ids"))
        != (capability.measurements.tissue_generation_id,)
        or tuple(_sequence(manifest["generations"], "generations")) != ()
        or canonical_bytes(manifest["object_outputs"]) != canonical_bytes(outputs)
        or result.slide_id != capability.target_binding.slide_id
        or result.generation_id != capability.measurements.tissue_generation_id
        or evidence.observation.saved_state_disposition != "authenticated_unchanged"
        or evidence.observation.saved_state_before_sha256
        != evidence.authorized.saved_state_before_sha256
        or evidence.observation.saved_state_after_sha256
        != evidence.observation.saved_state_before_sha256
        or outputs["sha256"] is None
        or outputs["cell_ids_sha256"] != digest(list(capability.cell_ids))
        or outputs["cell_count"] != len(capability.cell_ids)
        or outputs["roi_count"] != sum(
            item.roi_inventory.row_count for item in
            capability.classifier_rois.projections
        )
        or outputs["tissue_section_count"] != 1
        or tuple(_sequence(outputs["selected_measurement_names"], "selected measurements"))
        != capability.measurements.selected_measurement_names
        or outputs["coordinate_system"] != "global_level0"
    ):
        raise BatchPhenotypingError("semantic raw result or saved-state authority differs")
    raw_sha256 = str(outputs["sha256"])
    expected = (
        Path(validation_inputs.operational_output_dir)
        / "exports"
        / capability.target_binding.slide_id
        / f"{capability.target_section_id}-{capability.target_binding.analysis_domain_id}"
        / f"{raw_sha256}.qupath-raw-objects.json"
    )
    raw_path = Path(str(outputs["path"]))
    try:
        normalized_path = raw_path.resolve(strict=True)
        output_root = Path(validation_inputs.operational_output_dir).resolve(strict=True)
        normalized_path.relative_to(output_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "semantic raw output is outside its authenticated output root"
        ) from exc
    if raw_path != expected or normalized_path != expected:
        raise BatchPhenotypingError(
            "semantic raw output path is not the exact content-addressed member"
        )
    byte_count = normalized_path.stat().st_size
    payload = read_bounded_file(
        normalized_path,
        expected_byte_count=byte_count,
        maximum_byte_count=_MAXIMUM_RAW_BYTES,
    )
    if hashlib.sha256(payload).hexdigest() != raw_sha256:
        raise BatchPhenotypingError("semantic raw output bytes changed")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("semantic raw output is malformed JSON") from exc
    _validate_raw_bytes(raw, capability)
    # The classifier identity tables include both candidate and Other regions.
    # Only the authenticated raw export carries their candidate status.
    candidate_secondary_roi_ids = tuple(sorted(
        row["roi_id"] for row in raw["rois"]
        if row.get("roi_level") == "secondary" and row.get("candidate_status") == "candidate"
    ))
    source_generation = str(capability.phenotype_source_identity["phenotype_generation_id"])
    identity = {
        "sample_id": capability.target_binding.sample_id,
        "project_entry_id": capability.target_binding.project_entry_id,
        "slide_id": capability.target_binding.slide_id,
        "canonical_image_id": capability.measurements.canonical_image_id,
        "canonical_image_sha256": capability.measurements.canonical_image_sha256,
        "section_id": capability.target_section_id,
        "tissue_generation_id": capability.measurements.tissue_generation_id,
        "segmentation_population_id": capability.measurements.segmentation_population_id,
        "phenotype_generation_id": source_generation,
        "phenotype_source_artifact_ref_sha256": (
            capability.phenotype_source_ref.artifact_ref_sha256
        ),
        "raw_export_sha256": raw_sha256,
        "raw_export_byte_count": byte_count,
        "saved_state_after_sha256": evidence.observation.saved_state_after_sha256,
        "cell_ids_sha256": digest(list(capability.cell_ids)),
        "cell_to_domain_membership_sha256": capability.cell_to_domain_membership_sha256,
        "configured_secondary_ids": list(capability.configured_secondary_ids),
        "configured_secondary_ids_sha256": digest(list(capability.configured_secondary_ids)),
        "candidate_secondary_roi_ids": list(candidate_secondary_roi_ids),
        "candidate_secondary_roi_ids_sha256": digest(list(candidate_secondary_roi_ids)),
        "primary_domain_status": capability.classifier_rois.domain_status,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
    }
    contract = semantic_raw_inventory_identity_contract()
    measurement_ref = capability.measurements.artifact_ref
    artifact = BatchArtifactRef(
        artifact_id="semantic-raw-inventory-" + digest(identity)[:48],
        artifact_kind="semantic_raw_inventory",
        schema_name="qupath-raw-objects",
        schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authorized.node_id,
        producer_attempt_id=authorized.attempt_id,
        producer_attempt_authorization_sha256=(authorized.attempt_authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=normalized_path,
        sha256=raw_sha256,
        byte_count=byte_count,
        scope=ArtifactScope(
            measurement_ref.scope.validation_domain_id,
            capability.target_binding.sample_id,
            capability.target_binding.slide_id,
            capability.target_binding.project_entry_id,
            (capability.target_binding,),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-batch-semantic-raw-inventory/v1",
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": measurement_ref.runtime_versions["qupath"],
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=(
            ArtifactConfigurationBinding(
                capability.target_binding.sample_id,
                capability.resolved_sample.resolved_config_sha256,
            ),
        ),
    )
    validate_artifact_ref(artifact)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def _validate_spec(spec: StageAdapterSpec) -> None:
    if (
        type(spec) is not StageAdapterSpec
        or spec.node_kind != _NODE_KIND
        or spec.node_version != "1"
        or spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        or spec.resource_kind != "qupath_image_write"
        or spec.declared_outputs != ("semantic_raw_inventory",)
        or spec.output_identity_contracts != (semantic_raw_inventory_identity_contract(),)
    ):
        raise ProjectStageCodecError("semantic raw adapter specification differs")
    required = {item.artifact_kind: item.multiplicity for item in spec.required_inputs}
    optional = {item.artifact_kind: item.multiplicity for item in spec.optional_inputs}
    if required != {
        "analysis_frame": ArtifactMultiplicity.SINGLE,
        "tissue_acceptance_result": ArtifactMultiplicity.SINGLE,
        "accepted_analysis_domain": ArtifactMultiplicity.SINGLE,
        "segmentation_result": ArtifactMultiplicity.SINGLE,
        "measurements_result": ArtifactMultiplicity.SINGLE,
    } or optional != {
        "primary_inference_result": ArtifactMultiplicity.SINGLE,
        "training_plan_set": ArtifactMultiplicity.SINGLE,
        "secondary_inference_result": ArtifactMultiplicity.EXACT_SET,
        "cluster_label_import_result": ArtifactMultiplicity.SINGLE,
        "marker_phenotype_result": ArtifactMultiplicity.SINGLE,
        "supervised_phenotype_result": ArtifactMultiplicity.SINGLE,
    }:
        raise ProjectStageCodecError("semantic raw adapter owner inputs differ")


def build_semantic_raw_inventory_project_stage_codec(
    spec: StageAdapterSpec,
    *,
    owner_resolver: ProjectStageOwnerCapabilityResolver,
) -> ProjectStageCodec:
    """Build the registered export codec around one pre-runtime resolver view."""

    _validate_spec(spec)
    if type(owner_resolver) is not ProjectStageOwnerCapabilityResolver:
        raise ProjectStageCodecError("semantic raw codec requires exact owner resolver")
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
    ) -> _ProjectStageRequestProjection:
        try:
            if len(intent.analysis_domain_bindings) != 1:
                raise BatchPhenotypingError("semantic raw target requires one domain")
            target = intent.analysis_domain_bindings[0]
            capability = _resolve(
                owner_resolver,
                context,
                project_entry_id=intent.project_entry_id,
                binding=target,
            )
            if (
                target != capability.target_binding
                or intent.sample_id != target.sample_id
                or intent.slide_id != target.slide_id
                or intent.pipeline_run_id != capability.segmentation_cells.pipeline_run_id
                or intent.resolved_sample is not capability.resolved_sample
                or intent.prerequisite_artifact_ref_sha256s
                != capability.current.input_artifact_ref_sha256s
            ):
                raise BatchPhenotypingError("semantic raw Task-10 target differs")
            return _ProjectStageRequestProjection(
                capability.request_body, capability.classifier_rois.producer_output_dirs)
        except Exception:
            owner_resolver._retire_for_attempt(context)
            raise

    def validate_result(evidence: object) -> BatchNodeExecutionResult:
        if type(evidence) is not ProjectStageTargetExecutionEvidence:
            raise BatchPhenotypingError(
                "semantic raw result requires exact Task-10 execution evidence"
            )
        context = evidence.context
        try:
            target_bindings = evidence.authorized.analysis_domain_bindings
            if len(target_bindings) != 1:
                raise BatchPhenotypingError("semantic raw result target is ambiguous")
            target = target_bindings[0]
            capability = _resolve(
                owner_resolver,
                context,
                project_entry_id=evidence.authorized.project_entry_id,
                binding=target,
            )
            return _build_result(
                evidence,
                capability,
                spec=spec,
                binding=binding,
            )
        finally:
            owner_resolver._retire_for_attempt(context)

    return ProjectStageCodec.for_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=project_request,
        result_validator=validate_result,
    )


__all__ = [
    "ValidatedSemanticRawSnapshotInvocationAuthority",
    "build_semantic_raw_inventory_project_stage_codec",
    "publish_semantic_raw_snapshot_invocation_authority",
]
