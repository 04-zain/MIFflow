"""Authenticated compact segmentation-result artifacts for batch execution.

The `ValidatedSegmentationResultProjection` record carries `artifact_ref`,
`state_head_sha256`, `active_plan_sha256`, `terminal_record_sha256`, `sample_id` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from mif_pipeline.analysis_domain import (
    ActiveAnalysisDomainAuthority,
    AnalysisDomainManifest,
)
from mif_pipeline.cell_analysis import DomainSetEvidence
from mif_pipeline.datasets.cell_store import CellDataset
from mif_pipeline.pipeline_core import ROIAnalysisError, StageStatus, StageResult
from mif_pipeline.pipeline_core.config import SegmentationConfig
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from .batch_analysis_domain_artifacts import (
    ValidatedAcceptedAnalysisDomainLiveAuthority,
    _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER,
    decode_committed_accepted_analysis_domain,
    reopen_committed_accepted_analysis_domain_live_authority,
    validate_accepted_analysis_domain_artifact_ref,
)
from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    StageAdapterSpec,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from .batch_classifier_inference_artifacts import (
    bind_committed_classifier_roi_live_authority,
    validate_primary_classifier_inference_result_artifact_ref,
)
from .batch_state import BatchStateStore
from .batch_segmentation_settings import SegmentationSettingsRevision
from .batch_segmentation_setup import (
    probe_selected_segmentation,
    resolve_current_segmentation_setup,
)
from .batch_tissue_artifacts import (
    reopen_committed_accepted_tissue_live_authority,
)
from .project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageCodecValidationResult,
    ProjectStageTargetIntent,
)
from .project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageRecordError,
    ProjectStageTargetExecutionEvidence,
)
from .workflow import (
    CurrentImageScientificInvocation,
    SegmentationResultValidationContext,
    prepare_current_image_scientific_request,
    validate_segmentation_population_result,
)

if TYPE_CHECKING:
    from .batch_dag import BatchPlan
    from .batch_manifest import ValidatedBatchManifest
    from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs


_CONTRACT_ID: Final[str] = "mif-batch-segmentation-result-identity/v1"
_ARTIFACT_KIND: Final[str] = "segmentation_result"
_SCHEMA_NAME: Final[str] = "roi-generation-manifest"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-segmentation-result/v1"
_NODE_KIND: Final[str] = "segmentation"
_GENERATION_MANIFEST_SCHEMA: Final[str] = "roi-generation-manifest/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_GENERATION_MANIFEST_FIELDS = frozenset(
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
_IDENTITY_FIELDS = {
    "accepted_analysis_domain_ref_set_sha256",
    "accepted_analysis_domain_ref_sha256s",
    "active_domain_revision_set_sha256",
    "adapter_spec_sha256",
    "analysis_domain_binding_set_sha256",
    "canonical_image_id",
    "canonical_image_sha256",
    "cell_count",
    "cell_evidence_sha256",
    "cell_ids_sha256",
    "compatibility_signature_sha256",
    "coordinate_system",
    "current_image_identity_sha256",
    "domain_cell_mapping_sha256",
    "domain_set_sha256",
    "execution_exchange_sha256",
    "generation_ids",
    "generation_manifest_byte_count",
    "generation_manifest_payload_sha256",
    "object_outputs_sha256",
    "pixels_processed",
    "project_authority_head_sha256",
    "project_entry_id",
    "project_entry_projection_sha256",
    "project_identity_sha256",
    "project_qpproj_snapshot_sha256",
    "projection_target_sha256",
    "request_capability_sha256",
    "request_codec_id",
    "request_codec_version",
    "resolved_config_sha256",
    "sample_id",
    "saved_state_after_sha256",
    "saved_state_before_sha256",
    "scientific_request_body_byte_count",
    "scientific_request_body_sha256",
    "script_sha256",
    "section_id",
    "section_population_sha256",
    "segmentation_config_sha256",
    "segmentation_domain_sha256",
    "segmentation_model_version",
    "segmentation_passes",
    "segmentation_population_id",
    "segmentation_reused_without_recomputation",
    "segmentation_skipped_excluded_domains",
    "primary_inference_result_ref_sha256s",
    "segmentation_status",
    "selected_annotations_locked",
    "slide_id",
    "tiles_processed",
    "tissue_generation_id",
    "tissue_path_object_id",
    "tissue_roi_id",
    "union_geometry_sha256",
    "validated_exchange_sha256",
    "validation_input_authority_sha256",
}
SEGMENTATION_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(sorted(_IDENTITY_FIELDS))


# ==============================================================================
#                          Batch segmentation artifacts
# ==============================================================================


def _segmentation_primary_routes(inputs, bindings):
    """Bind every selected domain to its actual accepted classification choice."""
    from .batch_training.planning import validate_primary_skip_artifact_ref
    selected = set(bindings)
    routes = {}
    skipped = set()
    for ref in inputs:
        if ref.artifact_kind == "training_plan_set":
            covered = selected.intersection(ref.scope.analysis_domain_bindings)
            validate_primary_skip_artifact_ref(ref, domain_bindings=tuple(covered))
            skipped.update(covered)
        elif ref.artifact_kind == "primary_inference_result":
            projection = validate_primary_classifier_inference_result_artifact_ref(ref)
            covered = set(projection.analysis_domain_bindings)
        else:
            continue
        if not covered or not covered <= selected or covered.intersection(routes):
            raise BatchArtifactError("Segmentation classification choices have foreign or duplicate analysis regions")
        routes.update((binding, ref) for binding in covered)
    if set(routes) != selected:
        raise BatchArtifactError("Segmentation requires an accepted primary classifier or explicit skip for every selected analysis region")
    if skipped and skipped != selected:
        raise BatchArtifactError("Segmentation analysis regions have inconsistent primary classification choices")
    return routes, bool(skipped)


def section_cell_analysis_route(*, primary_domain_statuses: Mapping[str, str],
        cell_count: int, segmentation_skipped_excluded_domains: bool) -> str:
    """Choose downstream work after the caller authenticates section owners.

    All primary exclusions require the explicit skipped, empty population. A
    mixed section keeps the ordinary positive-cell fitting path. An active
    section whose detector found no cells requires review, never exclusion.
    """
    statuses = tuple(primary_domain_statuses.values())
    if (not statuses or any(value not in {"active", "excluded_below_minimum",
            "excluded_by_primary_count_review"} for value in statuses)
            or type(cell_count) is not int or cell_count < 0
            or type(segmentation_skipped_excluded_domains) is not bool):
        raise BatchArtifactError("section analysis route has invalid source dispositions")
    excluded = all(value != "active" for value in statuses)
    if segmentation_skipped_excluded_domains != excluded or excluded and cell_count != 0:
        raise BatchArtifactError("section exclusion differs from segmentation completion")
    return "excluded" if excluded else "review_zero_cells" if cell_count == 0 else "analyzed"


def build_segmentation_scientific_invocation(
    context: BatchNodeExecutionContext,
    intent: ProjectStageTargetIntent,
    *,
    state_store: BatchStateStore,
    committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    prerequisite_inputs: (
        Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
    ) = None,
) -> CurrentImageScientificInvocation:
    """Reopen the exact retained section fan-in for one InstanSeg request."""

    from .batch_stage_adapters import SectionSegmentationFanIn

    if (
        type(context) is not BatchNodeExecutionContext
        or type(intent) is not ProjectStageTargetIntent
        or type(state_store) is not BatchStateStore
        or type(committed_exchange_reopener) is not ProjectStageCommittedExchangeReopener
    ):
        raise BatchArtifactError("segmentation request authorities are invalid")
    current = state_store.authenticate_current_execution_context(context)
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    nodes = tuple(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    samples = tuple(
        item for item in snapshot.manifest.samples if item.sample_id == intent.sample_id
    )
    if len(nodes) != 1 or samples != (intent.resolved_sample,):
        raise BatchArtifactError("segmentation plan node or sample differs")
    node = nodes[0]
    bindings = tuple(node.analysis_domain_bindings)
    input_digests = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    expected_config = (
        ArtifactConfigurationBinding(
            intent.sample_id, intent.resolved_sample.resolved_config_sha256
        ),
    )
    section = node.section_target
    # Current authentication proves the exact admitted node and inputs still
    # apply, including after a change to the analysis memory limit.
    if (
        snapshot.head.sha256 != current.state_head_sha256
        or snapshot.head.active_plan_sha256 != current.active_plan_sha256
        or current.node_kind != "segmentation"
        or current.node_scope != "sample"
        or node.kind != "segmentation"
        or current.participant_ids != (intent.sample_id,)
        or node.participant_ids != (intent.sample_id,)
        or current.project_entry_ids != (intent.project_entry_id,)
        or node.project_entry_ids != (intent.project_entry_id,)
        or current.analysis_domain_bindings != bindings
        or bindings != tuple(intent.analysis_domain_bindings)
        or current.configuration_bindings != expected_config
        or tuple(node.configuration_bindings) != expected_config
        or current.input_artifact_ref_sha256s != input_digests
        or input_digests != intent.prerequisite_artifact_ref_sha256s
        or section is None
        or section.project_entry_id != intent.project_entry_id
    ):
        raise BatchArtifactError("segmentation execution or section cover differs")
    by_kind: dict[str, list[BatchArtifactRef]] = {}
    for ref in context.inputs:
        by_kind.setdefault(ref.artifact_kind, []).append(ref)
    if (
        set(by_kind)
        - {
            "analysis_frame",
            "accepted_analysis_domain",
            "primary_inference_result",
            "training_plan_set",
        }
        or len(by_kind.get("analysis_frame", ())) != 1
        or len(by_kind.get("accepted_analysis_domain", ())) != 1
    ):
        raise BatchArtifactError("segmentation input inventory differs")
    frame = validate_analysis_frame_artifact_ref(by_kind["analysis_frame"][0])
    authorities = {
        item.artifact_ref.artifact_ref_sha256: item
        for item in snapshot.committed_artifact_authorities
    }
    if len(authorities) != len(snapshot.committed_artifact_authorities):
        raise BatchArtifactError("segmentation committed authorities are duplicated")
    try:
        domain_authority = authorities[by_kind["accepted_analysis_domain"][0].artifact_ref_sha256]
        classifier_authorities = tuple(
            authorities[ref.artifact_ref_sha256]
            for kind in ("primary_inference_result",)
            for ref in by_kind.get(kind, ())
        )
    except KeyError as exc:
        raise BatchArtifactError("segmentation committed input is unavailable") from exc
    compact = decode_committed_accepted_analysis_domain(domain_authority)
    accepted_refs = {item.artifact_ref_sha256: item for item in snapshot.accepted_artifacts}
    tissue_refs = tuple(
        accepted_refs[digest]
        for digest in compact.source_input_artifact_ref_sha256s
        if digest in accepted_refs
        and accepted_refs[digest].artifact_kind == "tissue_acceptance_result"
    )
    if len(tissue_refs) != 1 or tissue_refs[0].artifact_ref_sha256 not in authorities:
        raise BatchArtifactError("segmentation tissue predecessor is unavailable")
    tissue = reopen_committed_accepted_tissue_live_authority(
        authorities[tissue_refs[0].artifact_ref_sha256],
        exchange_reopener=committed_exchange_reopener,
    )
    domains = reopen_committed_accepted_analysis_domain_live_authority(
        domain_authority,
        accepted_tissue=tissue,
        exchange_reopener=committed_exchange_reopener,
    )
    active = {
        item.analysis_domain_id: item
        for item in domains.domain_authorities
        if item.section_id == section.section_id
    }
    tissue_sections = tuple(
        item for item in tissue.sections if item.section.section_id == section.section_id
    )
    if (
        set(active) != {item.analysis_domain_id for item in bindings}
        or len(tissue_sections) != 1
        or tissue_sections[0].section.path_object_id != section.tissue_path_object_id
        or domains.committed.state_head_sha256 != current.state_head_sha256
        or domains.committed.active_plan_sha256 != current.active_plan_sha256
        or domains.committed.tissue_generation_id != section.tissue_generation_id
        or domains.committed.project_entry_id != intent.project_entry_id
        or domains.committed.sample_id != intent.sample_id
        or domains.committed.slide_id != intent.slide_id
        or frame.spatial_frame.canonical_image_id != domains.committed.canonical_image_id
        or frame.spatial_frame.canonical_image_sha256 != domains.committed.canonical_image_sha256
    ):
        raise BatchArtifactError("segmentation retained domain cover differs")
    for binding in bindings:
        authority = active[binding.analysis_domain_id]
        if (
            binding.project_entry_id != intent.project_entry_id
            or binding.sample_id != intent.sample_id
            or binding.slide_id != intent.slide_id
            or binding.analysis_domain_revision != authority.analysis_domain_revision
            or binding.analysis_domain_revision_sha256 != authority.analysis_domain_revision_sha256
            or binding.domain_set_sha256 != domains.domain_set.domain_set_sha256
        ):
            raise BatchArtifactError("segmentation domain binding differs")

    classification_routes, primary_skipped = _segmentation_primary_routes(context.inputs, bindings)
    primaries: dict[AnalysisDomainBinding, BatchCommittedArtifactAuthority] = {}
    for authority in classifier_authorities:
        ref_bindings = tuple(authority.artifact_ref.scope.analysis_domain_bindings)
        if len(ref_bindings) != 1:
            raise BatchArtifactError("segmentation classifier domain scope differs")
        binding = ref_bindings[0]
        if binding in primaries:
            raise BatchArtifactError("segmentation primary cover is duplicated")
        primaries[binding] = authority
    fan_in = SectionSegmentationFanIn(bindings)
    try:
        fan_in.validate_coverage(
            primary_bindings=tuple(primaries),
            primary_skipped_bindings=bindings if primary_skipped else (),
        )
    except ValueError as exc:
        raise BatchArtifactError("segmentation classifier cover differs") from exc

    primary_domains: dict[str, object] = {}
    for binding in bindings:
        live = bind_committed_classifier_roi_live_authority(
            authorities[classification_routes[binding].artifact_ref_sha256],
            (),
            analysis_domain_binding=binding,
            resolved_sample=intent.resolved_sample,
            include_secondary=False,
            accepted_domain_authority=domain_authority,
            pipeline_run_id=intent.pipeline_run_id,
        )
        if live.pipeline_run_id != intent.pipeline_run_id:
            raise BatchArtifactError("segmentation classifier run differs")
        if primary_skipped:
            continue
        primary_domains[binding.analysis_domain_id] = {
            "primary": {
                **binding.to_mapping(),
                "analysis_domain_path_object_id": active[
                    binding.analysis_domain_id
                ].source_path_object_id,
                "section_id": section.section_id,
                "producer_attempt_id": live.primary.artifact_ref.producer_attempt_id,
                "roi_inventory": live.primary.roi_inventory.to_mapping(),
                "domain_status": live.domain_status,
            }
        }
    selected = resolve_current_segmentation_setup(context, state_store=state_store)
    if type(selected) is not SegmentationConfig:
        raise BatchArtifactError("segmentation setup is unresolved or invalid")
    if primary_skipped and selected.scope != "accepted_tissue":
        raise BatchArtifactError("Without primary classification, choose the accepted analysis regions as the area to segment")
    skipped = not primary_skipped and all(item["primary"]["domain_status"] != "active" for item in primary_domains.values())
    model_evidence = None
    if not skipped:
        if prerequisite_inputs is None or not callable(prerequisite_inputs):
            raise BatchArtifactError("segmentation prerequisite supplier is unavailable")
        model_evidence = probe_selected_segmentation(
            selected, prerequisite_inputs(snapshot.manifest, snapshot.plan)
        )
    if state_store.load_head() != snapshot.head:
        raise BatchArtifactError("segmentation state changed during model probe")
    state = {
        "preflight": {
            "canonical_image_id": domains.committed.canonical_image_id,
            "canonical_image_sha256": domains.committed.canonical_image_sha256,
        },
        "tissue_acceptance": {
            "generation_id": tissue.generation.generation_id,
            "sections": [
                {
                    "section_id": item.section.section_id,
                    "roi_id": item.section.roi_id,
                    "path_object_id": item.section.path_object_id,
                }
                for item in tissue.sections
            ],
        },
        "analysis_domain_selection": {
            "selection_status": "accepted",
            "selected_annotations_locked": True,
            "domain_set_sha256": domains.domain_set.domain_set_sha256,
            "analysis_domain_manifests": [item.to_mapping() for item in domains.manifests],
        },
        "primary_classification_skipped": primary_skipped,
        **({} if primary_skipped else {
            "primary_classifier": {section.section_id: {"analysis_domains": primary_domains}}
        }),
        "segmentation_setup": {
            "settings": selected.to_mapping(),
            "model_evidence": model_evidence,
            **({"settings_revision": node.segmentation_settings_revision.to_mapping()}
               if node.segmentation_settings_revision is not None else {}),
        },
    }
    return CurrentImageScientificInvocation(
        stage="segmentation",
        config=intent.resolved_sample.config,
        run_id=intent.pipeline_run_id,
        project_entry_id=intent.project_entry_id,
        scientific_state=state,
        section_id=section.section_id,
    )


def active_zero_cell_review(projection) -> StageResult:
    """Stop actual zero detections without changing the primary disposition.

    The completed native exchange remains retained for inspection. We can revise
    this section's segmentation options while retaining accepted upstream work.
    """
    if projection.cell_count != 0 or projection.segmentation_skipped_excluded_domains:
        raise BatchArtifactError("zero-cell review requires an active detector result")
    return StageResult.failed(
        stage="segmentation", slide_id=projection.slide_id,
        generation_id=projection.tissue_generation_id,
        error_code="segmentation_zero_cells_review_required",
        error_message=("Segmentation detected no cells in an included tissue section. Review the image, "
            "analysis regions and segmentation settings. Open Segmentation options, "
            "preview the affected steps and apply revised settings to process this section again."),
        error_context={"section_id": projection.section_id,
            "execution_exchange_sha256": projection.execution_exchange_sha256,
            "segmentation_population_id": projection.segmentation_population_id,
            "cell_count": 0, "domain_disposition": "active",
            "required_recovery": "revise_segmentation_options"})


def build_segmentation_stage_codec(
    spec: StageAdapterSpec,
    *,
    state_store: BatchStateStore,
    prerequisite_inputs: (
        Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
    ) = None,
) -> ProjectStageCodec:
    """Bind the retained-exchange request owner to the existing result owner."""

    if type(spec) is not StageAdapterSpec or type(state_store) is not BatchStateStore:
        raise BatchArtifactError("segmentation codec dependencies are invalid")
    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=QuPathScriptAsset.RUN_INSTANSEG.value,
        wire_stage_id="segmentation",
        terminal_stage_id="segmentation",
        request_codec_version="2",
    )

    def project(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    ) -> Mapping[str, object]:
        invocation = build_segmentation_scientific_invocation(
            context,
            intent,
            state_store=state_store,
            committed_exchange_reopener=committed_exchange_reopener,
            prerequisite_inputs=prerequisite_inputs,
        )
        request = prepare_current_image_scientific_request(invocation)
        request["sample_id"] = intent.sample_id
        return committed_exchange_reopener.retain_pending_segmentation_request(
            context, request)

    def validate_result(evidence):
        current = state_store.authenticate_current_execution_context(evidence.context)
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
        node = next(item for item in snapshot.plan.nodes if item.node_id == evidence.context.node_id)
        expected = (None if node.segmentation_settings_revision is None
                    else node.segmentation_settings_revision.to_mapping())
        request = evidence.reopen_validation_inputs().scientific_request_body
        if request.get("segmentation_settings_revision") != expected:
            raise BatchArtifactError("segmentation result lacks its exact current plan selection")
        validated = build_segmentation_result_execution_result(evidence)
        if not validated.outputs:
            return validated
        projection = validate_segmentation_result_artifact_ref(validated.outputs[0])
        if projection.cell_count or projection.segmentation_skipped_excluded_domains:
            return validated
        return ProjectStageCodecValidationResult(
            BatchNodeExecutionResult(validated.authorization_sha256, ()),
            active_zero_cell_review(projection))

    return ProjectStageCodec.for_exchange_reopening_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=project,
        result_validator=validate_result,
    )


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSegmentationResultArtifactRefProjection:
    """Compact owner projection without batch-state authority."""

    artifact_ref: BatchArtifactRef
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    accepted_analysis_domain_ref_sha256s: tuple[str, ...]
    selected_annotations_locked: bool
    generation_ids: tuple[str, ...]
    segmentation_population_id: str
    segmentation_config_sha256: str
    segmentation_domain_sha256: str
    segmentation_model_version: str
    domain_set_sha256: str
    union_geometry_sha256: str
    cell_ids_sha256: str
    cell_count: int
    domain_cell_mapping_sha256: str
    cell_evidence_sha256: str
    section_population_sha256: str
    segmentation_status: str
    segmentation_skipped_excluded_domains: bool
    primary_inference_result_ref_sha256s: tuple[str, ...]
    coordinate_system: str
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedSegmentationResultArtifactRefProjection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSegmentationResultProjection:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    accepted_analysis_domain_ref_sha256s: tuple[str, ...]
    selected_annotations_locked: bool
    generation_ids: tuple[str, ...]
    segmentation_population_id: str
    segmentation_config_sha256: str
    segmentation_domain_sha256: str
    segmentation_model_version: str
    domain_set_sha256: str
    union_geometry_sha256: str
    cell_ids_sha256: str
    cell_count: int
    domain_cell_mapping_sha256: str
    cell_evidence_sha256: str
    section_population_sha256: str
    segmentation_status: str
    segmentation_skipped_excluded_domains: bool
    primary_inference_result_ref_sha256s: tuple[str, ...]
    coordinate_system: str
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedSegmentationResultProjection is decoder-only")


_SEGMENTATION_CELL_LIVE_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSegmentationCellLiveAuthority:
    """Read-only retained segmentation evidence for semantic consumption."""

    committed: ValidatedSegmentationResultProjection
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority
    pipeline_run_id: str
    cell_dataset: CellDataset
    cell_dataset_ref: Mapping[str, object]
    domain_cell_inventories: tuple[Mapping[str, object], ...]
    domain_cell_counts: Mapping[str, int]
    measurement_result: object | None
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedSegmentationCellLiveAuthority is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedSegmentationCellLiveAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedSegmentationCellLiveAuthority is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("ValidatedSegmentationCellLiveAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ValidatedSegmentationCellLiveAuthority is noncopyable")


def _artifact_ref_projection(
    **values: object,
) -> ValidatedSegmentationResultArtifactRefProjection:
    result = object.__new__(ValidatedSegmentationResultArtifactRefProjection)
    for name in ValidatedSegmentationResultArtifactRefProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _projection(**values: object) -> ValidatedSegmentationResultProjection:
    result = object.__new__(ValidatedSegmentationResultProjection)
    for name in ValidatedSegmentationResultProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("segmentation artifact evidence is not finite JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BatchArtifactError(f"{field} must be a nonnegative integer")
    return value


def _positive_integer(value: object, field: str) -> int:
    result = _nonnegative_integer(value, field)
    if result == 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return result


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} does not have its exact closed fields")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return tuple(value)


def _binding_set_sha256(bindings: tuple[AnalysisDomainBinding, ...]) -> str:
    return _digest({"analysis_domain_bindings": [item.to_mapping() for item in bindings]})


def _segmentation_population_id(
    *,
    canonical_image_id: str,
    canonical_image_sha256: str,
    slide_id: str,
    tissue_generation_id: str,
    section_id: str,
    tissue_roi_id: str,
    segmentation_config_sha256: str,
    segmentation_domain_sha256: str,
) -> str:
    return "segmentation-population/v2/" + _digest(
        {
            "schema_version": "segmentation-population/v2",
            "canonical_image_id": canonical_image_id,
            "canonical_image_sha256": canonical_image_sha256,
            "slide_id": slide_id,
            "tissue_generation_id": tissue_generation_id,
            "section_id": section_id,
            "tissue_roi_id": tissue_roi_id,
            "segmentation_config_sha256": segmentation_config_sha256,
            "segmentation_domain_sha256": segmentation_domain_sha256,
        }
    )


def _binding_from_authority(
    authority: ActiveAnalysisDomainAuthority, *, sample_id: str, domain_set_sha256: str
) -> AnalysisDomainBinding:
    return AnalysisDomainBinding(
        project_entry_id=authority.project_entry_id,
        sample_id=sample_id,
        slide_id=authority.slide_id,
        analysis_domain_id=authority.analysis_domain_id,
        analysis_domain_revision=authority.analysis_domain_revision,
        analysis_domain_revision_sha256=authority.analysis_domain_revision_sha256,
        domain_set_sha256=domain_set_sha256,
    )


def _request_domain_crosscheck(
    *,
    request: Mapping[str, object],
    domain_set: DomainSetEvidence,
    authorities: tuple[ActiveAnalysisDomainAuthority, ...],
) -> None:
    if (
        request.get("selected_annotations_locked") is not True
        or request.get("analysis_domain_selection_status") != "accepted"
        or request.get("domain_set_sha256") != domain_set.domain_set_sha256
        or request.get("canonical_image_id") != authorities[0].canonical_image_id
    ):
        raise BatchArtifactError(
            "authenticated segmentation request differs from selected-domain lock authority"
        )
    raw_manifests = _sequence(request.get("analysis_domain_manifests"), "analysis_domain_manifests")
    try:
        manifests = tuple(
            AnalysisDomainManifest.from_mapping(_plain(item)) for item in raw_manifests
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "authenticated segmentation request domain manifests are invalid"
        ) from exc
    by_domain = {item.analysis_domain_id: item for item in authorities}
    if len(by_domain) != len(authorities) or len(manifests) != len(authorities):
        raise BatchArtifactError("accepted-domain refs do not exactly cover the scientific request")
    for manifest in manifests:
        latest = manifest.revisions[-1]
        authority = by_domain.get(latest.analysis_domain_id)
        if authority is None or any(
            actual != expected
            for actual, expected in (
                (authority.analysis_domain_kind, latest.ref.kind),
                (authority.analysis_domain_revision, latest.revision),
                (authority.analysis_domain_revision_sha256, latest.content_sha256),
                (authority.analysis_domain_manifest_sha256, manifest.content_sha256),
                (authority.project_entry_id, manifest.project_entry_id),
                (authority.slide_id, manifest.slide_id),
                (authority.section_id, manifest.section_id),
                (authority.tissue_generation_id, manifest.tissue_generation_id),
                (authority.tissue_roi_id, manifest.tissue_roi_id),
                (authority.tissue_path_object_id, manifest.tissue_path_object_id),
                (authority.source_path_object_id, latest.source_path_object_id),
                (authority.geometry_sha256, latest.geometry_sha256),
            )
        ):
            raise BatchArtifactError("accepted-domain identity differs from the scientific request")


def segmentation_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id=_CONTRACT_ID,
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=SEGMENTATION_RESULT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _validated_evidence_sources(
    evidence: ProjectStageTargetExecutionEvidence,
) -> tuple[object, object, object, object]:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError(
            "segmentation result composition requires issuer-owned execution evidence"
        )
    try:
        validation_inputs = evidence.reopen_validation_inputs()
    except ProjectStageRecordError as exc:
        raise BatchArtifactError(
            "segmentation execution evidence cannot reopen validation inputs"
        ) from exc
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or context.authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
        or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
        or context.node_id != authorized.node_id
        or context.adapter_spec.node_kind != _NODE_KIND
        or authorized.wire_stage_id != _NODE_KIND
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.request_codec_id != QuPathScriptAsset.RUN_INSTANSEG.value
        or authorized.request_codec_version != "2"
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.attempt_authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
    ):
        raise BatchArtifactError("segmentation execution evidence sources disagree")
    _sha256(evidence.validated_exchange_sha256, "validated_exchange_sha256")
    return context, authorized, observation, validation_inputs


def build_segmentation_result_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
) -> BatchNodeExecutionResult:
    context, authorized, observation, validation_inputs = _validated_evidence_sources(evidence)
    request = validation_inputs.scientific_request_body
    resolved_config = validation_inputs.resolved_config
    if (
        request.get("schema_version") != "roi-workflow-invocation/v1"
        or request.get("stage") != _NODE_KIND
        or request.get("sample_id") != authorized.sample_id
        or request.get("slide_id") != authorized.slide_id
        or request.get("run_id") != authorized.pipeline_run_id
        or request.get("project_entry_id") not in {None, authorized.project_entry_id}
    ):
        raise BatchArtifactError("authenticated segmentation request scope differs")
    selected_mapping = request.get("segmentation_settings")
    try:
        selected = SegmentationConfig.from_mapping(selected_mapping, field="segmentation_settings")
        configured_mapping = resolved_config.get("segmentation")
        configured = (
            None
            if configured_mapping is None
            else SegmentationConfig.from_mapping(
                configured_mapping, field="resolved_config.segmentation"
            )
        )
    except ROIAnalysisError as exc:
        raise BatchArtifactError("segmentation settings are invalid") from exc
    segmentation_config = selected.to_mapping()
    revision_mapping = request.get("segmentation_settings_revision")
    revision = None if revision_mapping is None else SegmentationSettingsRevision.from_mapping(revision_mapping)
    if revision is not None and revision.settings != selected:
        raise BatchArtifactError("segmentation request differs from its current plan selection")
    if _sha256(
        request.get("segmentation_settings_sha256"),
        "segmentation_settings_sha256",
    ) != _digest(segmentation_config) or (revision is None and configured is not None and configured != selected):
        raise BatchArtifactError("segmentation settings differ from admitted configuration")

    frame_refs = tuple(item for item in context.inputs if item.artifact_kind == "analysis_frame")
    if len(frame_refs) != 1:
        raise BatchArtifactError("segmentation requires one analysis-frame input")
    frame = validate_analysis_frame_artifact_ref(frame_refs[0])
    accepted_refs = tuple(
        item for item in context.inputs if item.artifact_kind == "accepted_analysis_domain"
    )
    if len(accepted_refs) != 1:
        raise BatchArtifactError(
            "segmentation requires one accepted-domain owner ref for its section cover"
        )
    accepted_projection = validate_accepted_analysis_domain_artifact_ref(accepted_refs[0])
    if (
        accepted_projection.artifact_ref.scope.sample_id != authorized.sample_id
        or accepted_projection.artifact_ref.scope.slide_id != authorized.slide_id
        or accepted_projection.project_entry_id != authorized.project_entry_id
        or accepted_projection.artifact_ref.configuration_bindings
        != (
            ArtifactConfigurationBinding(
                authorized.sample_id, validation_inputs.resolved_config_sha256
            ),
        )
    ):
        raise BatchArtifactError("accepted-domain input authority or scope differs")
    domain_set = accepted_projection.domain_set
    domain_authorities = accepted_projection.domain_authorities
    _request_domain_crosscheck(
        request=request,
        domain_set=domain_set,
        authorities=domain_authorities,
    )
    section_id = _text(request.get("section_id"), "request.section_id")
    section_authorities = tuple(
        item for item in domain_authorities if item.section_id == section_id
    )
    expected_bindings = tuple(
        sorted(
            (
                _binding_from_authority(
                    item,
                    sample_id=authorized.sample_id,
                    domain_set_sha256=domain_set.domain_set_sha256,
                )
                for item in section_authorities
            ),
            key=lambda item: (
                item.sample_id,
                item.slide_id,
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.project_entry_id,
            ),
        )
    )
    if not section_authorities or tuple(authorized.analysis_domain_bindings) != expected_bindings:
        raise BatchArtifactError(
            "accepted-domain refs do not exactly cover the segmentation section"
        )

    classification_routes, primary_skipped = _segmentation_primary_routes(context.inputs, expected_bindings)
    if request.get("primary_classification_skipped") is not primary_skipped:
        raise BatchArtifactError("Segmentation request differs from its accepted classification choice")
    if primary_skipped and segmentation_config["scope"] != "accepted_tissue":
        raise BatchArtifactError("Skipped primary classification requires segmentation within accepted analysis regions")
    primary_by_domain = {}
    for ref in context.inputs:
        if ref.artifact_kind != "primary_inference_result":
            continue
        primary = validate_primary_classifier_inference_result_artifact_ref(ref)
        if len(primary.analysis_domain_bindings) != 1:
            raise BatchArtifactError("A segmentation primary result must describe one analysis region")
        binding = primary.analysis_domain_bindings[0]
        if (binding not in expected_bindings or primary.section_id != section_id
                or ref.configuration_bindings != accepted_refs[0].configuration_bindings
                or binding.analysis_domain_id in primary_by_domain):
            raise BatchArtifactError("Segmentation primary inputs differ from its accepted analysis regions")
        primary_by_domain[binding.analysis_domain_id] = primary
    if not primary_skipped and set(primary_by_domain) != {item.analysis_domain_id for item in expected_bindings}:
        raise BatchArtifactError("Segmentation primary inputs do not cover its accepted analysis regions")

    section_records: dict[str, dict[str, str]] = {}
    for item in domain_authorities:
        record = {
            "section_id": item.section_id,
            "tissue_roi_id": item.tissue_roi_id,
            "tissue_path_object_id": item.tissue_path_object_id,
            "tissue_generation_id": item.tissue_generation_id,
        }
        if item.section_id in section_records and section_records[item.section_id] != record:
            raise BatchArtifactError("accepted-domain physical section authority differs")
        section_records[item.section_id] = record
    target_section = section_records[section_id]
    domain_selection = {
        "selection_status": request.get("analysis_domain_selection_status"),
        "selected_annotations_locked": request.get("selected_annotations_locked"),
        "domain_set_sha256": request.get("domain_set_sha256"),
        "analysis_domain_manifests": _plain(request.get("analysis_domain_manifests")),
    }
    try:
        scientific_context = SegmentationResultValidationContext(
            segmentation_config=segmentation_config,
            segmentation_settings_revision_sha256=None if revision is None else revision.sha256,
            canonical_image_id=_text(
                accepted_refs[0].owning_identity.get("canonical_image_id"),
                "canonical_image_id",
            ),
            canonical_image_sha256=_sha256(
                accepted_refs[0].owning_identity.get("canonical_image_sha256"),
                "canonical_image_sha256",
            ),
            project_entry_id=authorized.project_entry_id,
            slide_id=authorized.slide_id,
            section_id=section_id,
            tissue_generation_id=target_section["tissue_generation_id"],
            accepted_tissue_sections=tuple(section_records[key] for key in sorted(section_records)),
            analysis_domain_selection=domain_selection,
            domain_set_sha256=domain_set.domain_set_sha256,
            selected_annotations_locked=request.get("selected_annotations_locked"),
            primary_domain_statuses=(
                {item.analysis_domain_id: "active" for item in expected_bindings}
                if primary_skipped else {
                    domain_id: primary.domain_status for domain_id, primary in primary_by_domain.items()
                }
            ),
        )
    except ROIAnalysisError as exc:
        raise BatchArtifactError("segmentation scientific validation context is invalid") from exc

    manifest = _closed_mapping(
        evidence.generation_manifest,
        _GENERATION_MANIFEST_FIELDS,
        "segmentation generation manifest",
    )
    # Cell rows are retained in the dataset; this stage creates no ROI generations.
    if _sequence(manifest["generations"], "segmentation generations"):
        raise BatchArtifactError("segmentation generation manifest must not contain ROI generations")
    result = evidence.wire_stage_result
    if (
        result.status is not StageStatus.COMPLETED
        or result.stage != _NODE_KIND
        or result.slide_id != authorized.slide_id
        or result.outputs is None
        or manifest["schema_version"] != _GENERATION_MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _NODE_KIND
        or manifest["slide_id"] != authorized.slide_id
        or _plain(manifest["object_outputs"]) != _plain(result.outputs)
    ):
        raise BatchArtifactError("segmentation result and generation manifest authority differ")
    generation_ids = tuple(
        _text(item, "generation_ids item")
        for item in _sequence(manifest["generation_ids"], "generation_ids")
    )
    if generation_ids != (result.generation_id,):
        raise BatchArtifactError("segmentation requires one exact generation")
    scientific_output = _plain(result.outputs)
    if not isinstance(scientific_output, Mapping):
        raise BatchArtifactError("segmentation scientific output is not an object")
    try:
        validate_segmentation_population_result(scientific_output, scientific_context)
    except ROIAnalysisError as exc:
        raise BatchArtifactError("segmentation-population/v2 scientific validation failed") from exc

    output = scientific_output
    population = output["segmentation_population"]
    if not isinstance(population, Mapping):
        raise BatchArtifactError("segmentation population identity is missing")
    active_revisions = population.get("active_domain_revisions")
    accepted_ref_sha256s = tuple(sorted(item.artifact_ref_sha256 for item in accepted_refs))
    identity = {
        "sample_id": authorized.sample_id,
        "slide_id": authorized.slide_id,
        "project_entry_id": authorized.project_entry_id,
        "canonical_image_id": scientific_context.canonical_image_id,
        "canonical_image_sha256": scientific_context.canonical_image_sha256,
        "section_id": section_id,
        "tissue_generation_id": target_section["tissue_generation_id"],
        "tissue_path_object_id": target_section["tissue_path_object_id"],
        "tissue_roi_id": target_section["tissue_roi_id"],
        "analysis_domain_binding_set_sha256": _binding_set_sha256(expected_bindings),
        "accepted_analysis_domain_ref_sha256s": list(accepted_ref_sha256s),
        "accepted_analysis_domain_ref_set_sha256": _digest(
            {"artifact_ref_sha256s": list(accepted_ref_sha256s)}
        ),
        "active_domain_revision_set_sha256": _digest(active_revisions),
        "selected_annotations_locked": request["selected_annotations_locked"],
        "generation_ids": list(generation_ids),
        "segmentation_population_id": output["segmentation_population_id"],
        "segmentation_config_sha256": output["segmentation_config_sha256"],
        "segmentation_domain_sha256": output["segmentation_domain_sha256"],
        "segmentation_model_version": selected.model_version,
        "domain_set_sha256": output["domain_set_sha256"],
        "union_geometry_sha256": output["union_geometry_sha256"],
        "cell_ids_sha256": output["cell_ids_sha256"],
        "cell_count": output["cell_count"],
        "domain_cell_mapping_sha256": output["domain_cell_mapping_sha256"],
        "cell_evidence_sha256": output["cell_evidence_sha256"],
        "section_population_sha256": output["section_population_sha256"],
        "segmentation_status": output["segmentation_status"],
        "segmentation_passes": output["segmentation_passes"],
        "segmentation_skipped_excluded_domains": output["segmentation_skipped_excluded_domains"],
        "primary_inference_result_ref_sha256s": sorted(
            {item.artifact_ref_sha256 for item in classification_routes.values()
             if item.artifact_kind == "primary_inference_result"}),
        "segmentation_reused_without_recomputation": output[
            "segmentation_reused_without_recomputation"
        ],
        "tiles_processed": output["tiles_processed"],
        "pixels_processed": output["pixels_processed"],
        "coordinate_system": output["coordinate_system"],
        "generation_manifest_payload_sha256": observation.generation_manifest_payload_sha256,
        "generation_manifest_byte_count": observation.generation_manifest_byte_count,
        "object_outputs_sha256": _digest(output),
        "saved_state_before_sha256": authorized.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "scientific_request_body_sha256": validation_inputs.scientific_request_body_sha256,
        "scientific_request_body_byte_count": validation_inputs.scientific_request_body_byte_count,
        "resolved_config_sha256": validation_inputs.resolved_config_sha256,
        "request_capability_sha256": validation_inputs.request_capability_sha256,
        "validation_input_authority_sha256": validation_inputs.authority_sha256,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "projection_target_sha256": context.projection_target_sha256,
        "current_image_identity_sha256": context.current_image_identity_sha256,
        "script_sha256": context.script_sha256,
        "project_identity_sha256": context.project_identity_sha256,
        "project_authority_head_sha256": context.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    contract = segmentation_result_identity_contract()
    artifact = BatchArtifactRef(
        artifact_id="segmentation-" + str(output["section_population_sha256"])[:48],
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authorized.node_id,
        producer_attempt_id=authorized.attempt_id,
        producer_attempt_authorization_sha256=authorized.attempt_authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=accepted_refs[0].scope.validation_domain_id,
            sample_id=authorized.sample_id,
            slide_id=authorized.slide_id,
            project_entry_id=authorized.project_entry_id,
            analysis_domain_bindings=expected_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": frame.qupath_executable_version,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=(
            ArtifactConfigurationBinding(
                authorized.sample_id, validation_inputs.resolved_config_sha256
            ),
        ),
    )
    return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (artifact,))


def validate_segmentation_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedSegmentationResultArtifactRefProjection:
    """Validate compact owner data without asserting batch-state availability.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    ref = validate_artifact_ref(artifact_ref)
    contract = segmentation_result_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or set(ref.runtime_versions) != {"mif_pipeline", "qupath"}
        or ref.runtime_versions.get("mif_pipeline") != MIF_PIPELINE_BATCH_RUNTIME_VERSION
    ):
        raise BatchArtifactError("segmentation result lacks its locatorless owner contract")
    _text(ref.runtime_versions.get("qupath"), "runtime_versions.qupath")
    identity = _closed_mapping(
        ref.owning_identity, frozenset(_IDENTITY_FIELDS), "segmentation identity"
    )
    sample_id = _text(identity["sample_id"], "sample_id")
    slide_id = _text(identity["slide_id"], "slide_id")
    project_entry_id = _text(identity["project_entry_id"], "project_entry_id")
    section_id = _text(identity["section_id"], "section_id")
    tissue_generation_id = _text(identity["tissue_generation_id"], "tissue_generation_id")
    tissue_path_object_id = _text(identity["tissue_path_object_id"], "tissue_path_object_id")
    tissue_roi_id = _text(identity["tissue_roi_id"], "tissue_roi_id")
    canonical_image_id = _text(identity["canonical_image_id"], "canonical_image_id")
    canonical_image_sha256 = _sha256(identity["canonical_image_sha256"], "canonical_image_sha256")
    bindings = ref.scope.analysis_domain_bindings
    accepted_ref_sha256s = tuple(
        _sha256(item, "accepted_analysis_domain_ref_sha256s item")
        for item in _sequence(
            identity["accepted_analysis_domain_ref_sha256s"],
            "accepted_analysis_domain_ref_sha256s",
        )
    )
    generation_ids = tuple(
        _text(item, "generation_ids item")
        for item in _sequence(identity["generation_ids"], "generation_ids")
    )
    section_population_sha256 = _sha256(
        identity["section_population_sha256"], "section_population_sha256"
    )
    execution_exchange_sha256 = _sha256(
        identity["execution_exchange_sha256"], "execution_exchange_sha256"
    )
    segmentation_population_id = _text(
        identity["segmentation_population_id"], "segmentation_population_id"
    )
    segmentation_config_sha256 = _sha256(
        identity["segmentation_config_sha256"], "segmentation_config_sha256"
    )
    segmentation_domain_sha256 = _sha256(
        identity["segmentation_domain_sha256"], "segmentation_domain_sha256"
    )
    segmentation_model_version = _text(
        identity["segmentation_model_version"], "segmentation_model_version"
    )
    domain_set_sha256 = _sha256(identity["domain_set_sha256"], "domain_set_sha256")
    compatibility_signature_sha256 = _sha256(
        identity["compatibility_signature_sha256"],
        "compatibility_signature_sha256",
    )
    resolved_config_sha256 = _sha256(identity["resolved_config_sha256"], "resolved_config_sha256")
    cell_count = _nonnegative_integer(identity["cell_count"], "cell_count")
    segmentation_passes = _nonnegative_integer(
        identity["segmentation_passes"], "segmentation_passes"
    )
    segmentation_reused = identity["segmentation_reused_without_recomputation"]
    segmentation_skipped = identity["segmentation_skipped_excluded_domains"]
    primary_refs = tuple(_sequence(identity["primary_inference_result_ref_sha256s"], "primary inference refs"))
    if (len(primary_refs) not in {0, len(bindings)} or primary_refs != tuple(sorted(set(primary_refs)))
            or any(not isinstance(item, str) or len(item) != 64
                   or any(char not in "0123456789abcdef" for char in item) for item in primary_refs)):
        raise BatchArtifactError("segmentation primary input cover is invalid")
    tiles_processed = _nonnegative_integer(identity["tiles_processed"], "tiles_processed")
    pixels_processed = _nonnegative_integer(identity["pixels_processed"], "pixels_processed")
    for field in (
        "active_domain_revision_set_sha256",
        "adapter_spec_sha256",
        "analysis_domain_binding_set_sha256",
        "cell_evidence_sha256",
        "cell_ids_sha256",
        "current_image_identity_sha256",
        "domain_cell_mapping_sha256",
        "generation_manifest_payload_sha256",
        "object_outputs_sha256",
        "project_authority_head_sha256",
        "project_entry_projection_sha256",
        "project_identity_sha256",
        "project_qpproj_snapshot_sha256",
        "projection_target_sha256",
        "request_capability_sha256",
        "saved_state_after_sha256",
        "saved_state_before_sha256",
        "scientific_request_body_sha256",
        "script_sha256",
        "union_geometry_sha256",
        "validated_exchange_sha256",
        "validation_input_authority_sha256",
    ):
        _sha256(identity[field], field)
    if (
        ref.artifact_id != "segmentation-" + section_population_sha256[:48]
        or ref.scope.validation_domain_id is None
        or ref.scope.sample_id != sample_id
        or ref.scope.slide_id != slide_id
        or ref.scope.project_entry_id != project_entry_id
        or not bindings
        or any(
            item.sample_id != sample_id
            or item.slide_id != slide_id
            or item.project_entry_id != project_entry_id
            or item.domain_set_sha256 != domain_set_sha256
            for item in bindings
        )
        or _binding_set_sha256(bindings) != identity["analysis_domain_binding_set_sha256"]
        or len(accepted_ref_sha256s) != 1
        or accepted_ref_sha256s != tuple(sorted(set(accepted_ref_sha256s)))
        or _digest({"artifact_ref_sha256s": list(accepted_ref_sha256s)})
        != identity["accepted_analysis_domain_ref_set_sha256"]
        or identity["selected_annotations_locked"] is not True
        or len(generation_ids) != 1
        or identity["coordinate_system"] != "global_level0"
        or identity["request_codec_id"] != QuPathScriptAsset.RUN_INSTANSEG.value
        or identity["request_codec_version"] != "2"
        or compatibility_signature_sha256 != ref.compatibility_signature_sha256
        or identity["segmentation_status"] not in {"completed_zero_cells", "completed_with_cells"}
        or segmentation_population_id
        != _segmentation_population_id(
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            slide_id=slide_id,
            tissue_generation_id=tissue_generation_id,
            section_id=section_id,
            tissue_roi_id=tissue_roi_id,
            segmentation_config_sha256=segmentation_config_sha256,
            segmentation_domain_sha256=segmentation_domain_sha256,
        )
        or ((cell_count == 0) != (identity["segmentation_status"] == "completed_zero_cells"))
        or type(segmentation_reused) is not bool
        or segmentation_passes not in {0, 1}
        or type(segmentation_skipped) is not bool
        or (segmentation_skipped and (cell_count != 0 or segmentation_reused))
        or (segmentation_passes == 0) != (segmentation_reused or segmentation_skipped)
        or (segmentation_passes == 0 and (tiles_processed != 0 or pixels_processed != 0))
        or (segmentation_passes == 1 and (tiles_processed <= 0 or pixels_processed <= 0))
        or _positive_integer(
            identity["generation_manifest_byte_count"],
            "generation_manifest_byte_count",
        )
        <= 0
        or _positive_integer(
            identity["scientific_request_body_byte_count"],
            "scientific_request_body_byte_count",
        )
        <= 0
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0]
        != ArtifactConfigurationBinding(sample_id, resolved_config_sha256)
    ):
        raise BatchArtifactError("segmentation result identity relationships differ")
    return _artifact_ref_projection(
        artifact_ref=ref,
        sample_id=sample_id,
        slide_id=slide_id,
        project_entry_id=project_entry_id,
        canonical_image_id=canonical_image_id,
        canonical_image_sha256=canonical_image_sha256,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        tissue_path_object_id=tissue_path_object_id,
        tissue_roi_id=tissue_roi_id,
        analysis_domain_bindings=bindings,
        accepted_analysis_domain_ref_sha256s=accepted_ref_sha256s,
        selected_annotations_locked=True,
        generation_ids=generation_ids,
        segmentation_population_id=segmentation_population_id,
        segmentation_config_sha256=segmentation_config_sha256,
        segmentation_domain_sha256=segmentation_domain_sha256,
        segmentation_model_version=segmentation_model_version,
        domain_set_sha256=domain_set_sha256,
        union_geometry_sha256=identity["union_geometry_sha256"],
        cell_ids_sha256=identity["cell_ids_sha256"],
        cell_count=cell_count,
        domain_cell_mapping_sha256=identity["domain_cell_mapping_sha256"],
        cell_evidence_sha256=identity["cell_evidence_sha256"],
        section_population_sha256=section_population_sha256,
        segmentation_status=identity["segmentation_status"],
        segmentation_skipped_excluded_domains=segmentation_skipped,
        primary_inference_result_ref_sha256s=primary_refs,
        coordinate_system="global_level0",
        saved_state_before_sha256=identity["saved_state_before_sha256"],
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        execution_exchange_sha256=execution_exchange_sha256,
        validated_exchange_sha256=identity["validated_exchange_sha256"],
    )


def reopen_committed_segmentation_cell_live_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    accepted_domains: ValidatedAcceptedAnalysisDomainLiveAuthority,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
    measurement_result=None,
) -> ValidatedSegmentationCellLiveAuthority:
    """Reauthenticate one committed population without rerunning it."""

    committed = decode_committed_segmentation_result(authority)
    if (
        type(accepted_domains) is not ValidatedAcceptedAnalysisDomainLiveAuthority
        or accepted_domains._issuer is not _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER
        or type(exchange_reopener) is not ProjectStageCommittedExchangeReopener
    ):
        raise BatchArtifactError("segmentation cell reopen requires exact live authorities")
    artifact_ref = committed.artifact_ref
    if (
        artifact_ref.producer_node_id is None
        or artifact_ref.producer_attempt_id is None
        or artifact_ref.producer_attempt_authorization_sha256 is None
    ):
        raise BatchArtifactError("segmentation result lacks exact producer identity")
    try:
        reopened = exchange_reopener._reopen_for_owner(
            authority,
            producer_artifact_ref_sha256=artifact_ref.artifact_ref_sha256,
            producer_node_id=artifact_ref.producer_node_id,
            producer_attempt_id=artifact_ref.producer_attempt_id,
            producer_attempt_authorization_sha256=(
                artifact_ref.producer_attempt_authorization_sha256
            ),
            terminal_record_sha256=committed.terminal_record_sha256,
            execution_exchange_sha256=committed.execution_exchange_sha256,
            expected_binding=(
                _sha256(
                    artifact_ref.owning_identity.get("adapter_spec_sha256"),
                    "adapter_spec_sha256",
                ),
                QuPathScriptAsset.RUN_INSTANSEG.value,
                _NODE_KIND,
                _NODE_KIND,
                "2",
            ),
        )
        evidence = reopened.evidence
        rebuilt = build_segmentation_result_execution_result(evidence)
    except (ProjectStageRecordError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "committed segmentation retained exchange cannot be reopened"
        ) from exc
    if (
        len(rebuilt.outputs) != 1
        or rebuilt.outputs[0] != artifact_ref
        or rebuilt.authorization_sha256 != artifact_ref.producer_attempt_authorization_sha256
    ):
        raise BatchArtifactError("committed segmentation compact and retained evidence differ")

    domain_committed = accepted_domains.committed
    source_inputs = tuple(
        item for item in evidence.context.inputs if item.artifact_kind == "accepted_analysis_domain"
    )
    if (
        len(source_inputs) != 1
        or source_inputs[0] != domain_committed.artifact_ref
        or committed.accepted_analysis_domain_ref_sha256s
        != (domain_committed.artifact_ref.artifact_ref_sha256,)
        or domain_committed.state_head_sha256 != committed.state_head_sha256
        or domain_committed.active_plan_sha256 != committed.active_plan_sha256
        or domain_committed.sample_id != committed.sample_id
        or domain_committed.slide_id != committed.slide_id
        or domain_committed.project_entry_id != committed.project_entry_id
        or domain_committed.canonical_image_id != committed.canonical_image_id
        or domain_committed.canonical_image_sha256 != committed.canonical_image_sha256
        or accepted_domains.domain_set.domain_set_sha256 != committed.domain_set_sha256
    ):
        raise BatchArtifactError(
            "segmentation accepted-domain source, state, plan, or scope differs"
        )

    output = evidence.wire_stage_result.outputs
    reference = output["cell_dataset"]
    try:
        dataset = CellDataset(reference["path"])
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise BatchArtifactError("segmentation canonical cell dataset cannot be reopened") from exc
    if (dataset.summary.sha256 != committed.cell_evidence_sha256
            or dataset.summary.sha256 != reference["sha256"]
            or dataset.row_count != committed.cell_count
            or dataset.description["domain_cell_mapping_sha256"] != committed.domain_cell_mapping_sha256):
        raise BatchArtifactError("segmentation selected cell dataset differs from its result")
    inventories = output["domain_cell_inventories"]
    counts = output["domain_cell_counts"]
    if measurement_result is not None:
        dataset, inventories, counts = _reopen_measured_cell_population(
            committed, dataset, measurement_result)
    value = object.__new__(ValidatedSegmentationCellLiveAuthority)
    for name, item in {
        "committed": committed,
        "accepted_domains": accepted_domains,
        "pipeline_run_id": evidence.authorized.pipeline_run_id,
        "cell_dataset": dataset,
        "cell_dataset_ref": reference,
        "domain_cell_inventories": inventories,
        "domain_cell_counts": counts,
        "measurement_result": measurement_result,
        "_issuer": _SEGMENTATION_CELL_LIVE_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


def _reopen_measured_cell_population(committed, dataset, measurement):
    """Apply one accepted measurement checkpoint to its original segmentation.

    Reconstruct the original ID/domain commitments from surviving rows and
    explicit deletions before exposing their current geometry and inventories.
    The immutable base reference and committed segmentation remain unchanged.
    """
    from .batch_measurement_artifacts import ValidatedMeasurementsResultProjection
    from mif_pipeline.cell_analysis.measurement_rows import (
        MarkerIntensityRowsDescriptor, MeasurementRowStreamExpectation,
        read_measurement_dataset_selection, validate_measurement_row_stream,
    )

    if (type(measurement) is not ValidatedMeasurementsResultProjection
            or measurement.artifact_ref.owning_identity["segmentation_result_ref_sha256"]
            != committed.artifact_ref.artifact_ref_sha256
            or (measurement.state_head_sha256, measurement.active_plan_sha256,
                measurement.segmentation_population_id, measurement.analysis_domain_bindings)
            != (committed.state_head_sha256, committed.active_plan_sha256,
                committed.segmentation_population_id, committed.analysis_domain_bindings)
            or dataset.summary.sha256 != committed.cell_evidence_sha256):
        raise BatchArtifactError("Current cell corrections require their accepted segmentation and measurement result")
    rows = measurement.measurement_rows
    descriptor = MarkerIntensityRowsDescriptor(rows.path, rows.sha256, rows.byte_count,
        rows.row_count, rows.content_schema_version)
    features = tuple(item for item in measurement.measurement_plan.features if item.kind == "expression")
    root = rows.path.parent.parent.parent
    stream = validate_measurement_row_stream(descriptor=descriptor,
        authenticated_output_root=root,
        expected=MeasurementRowStreamExpectation(
            project_entry_id=committed.project_entry_id, slide_id=committed.slide_id,
            section_id=committed.section_id,
            segmentation_generation_id=measurement.measurement_generation_id,
            measurement_generation_id=measurement.measurement_generation_id,
            segmentation_population_id=committed.segmentation_population_id,
            cell_dataset_sha256=committed.cell_evidence_sha256,
            analysis_domain_bindings=tuple({"analysis_domain_id": item.analysis_domain_id,
                "analysis_domain_revision_sha256": item.analysis_domain_revision_sha256}
                for item in committed.analysis_domain_bindings),
            feature_ids=tuple(item.feature_id for item in features),
            measurement_names=tuple(item.measurement for item in features)),
        expected_base_population=(committed.cell_count, committed.cell_ids_sha256,
                                  committed.domain_cell_mapping_sha256))
    if (stream.cell_count != measurement.cell_count
            or stream.cell_ids_sha256 != measurement.cell_ids_sha256
            or stream.cell_to_domain_membership_sha256 != measurement.cell_to_domain_membership_sha256
            or stream.domain_cell_commitments_sha256 != measurement.domain_cell_commitments_sha256
            or stream.row_inventory_sha256 != rows.row_inventory_sha256):
        raise BatchArtifactError("Current cell corrections differ from the accepted measurements")
    selected = read_measurement_dataset_selection(descriptor, authenticated_output_root=root)
    if selected["cell_edits"] is not None:
        dataset = dataset.with_edits(dict(selected["cell_edits"]))
    inventories = tuple(MappingProxyType({"analysis_domain_id": item.analysis_domain_id,
        "cell_count": item.cell_count, "cell_ids_sha256": item.cell_ids_sha256})
        for item in stream.domain_cell_commitments)
    counts = MappingProxyType({item["analysis_domain_id"]: item["cell_count"] for item in inventories})
    return dataset, inventories, counts


def decode_committed_segmentation_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedSegmentationResultProjection:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("segmentation result decoding requires committed state authority")
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    if authority.producer_node_kind != _NODE_KIND:
        raise BatchArtifactError("segmentation result producer kind is invalid")
    ref_projection = validate_segmentation_result_artifact_ref(authority.artifact_ref)
    if authority.execution_exchange_sha256 != ref_projection.execution_exchange_sha256:
        raise BatchArtifactError("committed attempt exchange differs from segmentation identity")
    values = {
        name: getattr(ref_projection, name)
        for name in ValidatedSegmentationResultArtifactRefProjection.__dataclass_fields__
    }
    values.update(
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )
    return _projection(**values)


__all__ = [
    "SEGMENTATION_RESULT_IDENTITY_FIELDS",
    "ValidatedSegmentationCellLiveAuthority",
    "ValidatedSegmentationResultArtifactRefProjection",
    "ValidatedSegmentationResultProjection",
    "build_segmentation_scientific_invocation",
    "build_segmentation_stage_codec",
    "build_segmentation_result_execution_result",
    "decode_committed_segmentation_result",
    "reopen_committed_segmentation_cell_live_authority",
    "segmentation_result_identity_contract",
    "validate_segmentation_result_artifact_ref",
]
