"""Authenticated terminal tissue-candidate and direct-acceptance artifacts.

The `ValidatedTissueAcceptanceResultProjection` record carries `artifact_ref`,
`state_head_sha256`, `active_plan_sha256`, `terminal_record_sha256`, `sample_id` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final

from mif_pipeline.pipeline_core import (
    AnalysisConfig,
    InteractionReason,
    ROIAnalysisError,
    StageStatus,
    TissueSection,
)
from mif_pipeline.qupath_runtime.project_stage import ProjectStageTargetObservation
from mif_pipeline.pipeline_core.config import ChannelSpec, ReviewPolicy, TissueDetectionConfig
from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.tissue_detection.generations import (
    GenerationStatus,
    TissueDetectionQC,
    TissueSectionGeneration,
    validate_generation_manifest,
)
from mif_pipeline.tissue_detection.project import accept_tissue_candidate
from mif_pipeline.tissue_detection.resolution import (
    NativeLevelReadAttestation,
    NativePyramidLevel,
    TissueResolutionSelection,
)

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    BatchAnalysisPreflightError,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from .batch_tissue_channel_artifacts import (
    validate_tissue_channel_result_artifact_ref,
)
from .batch_tissue_setup_artifacts import (
    decode_committed_tissue_setup_result,
    validate_tissue_setup_result_artifact_ref,
)
from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from .project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageAuthorizedEntry,
    ProjectStageRecordError,
    ProjectStageTargetExecutionEvidence,
)

_SCHEMA_NAME: Final[str] = "roi-generation-manifest"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-tissue-artifacts/v1"
_MANIFEST_SCHEMA: Final[str] = "roi-generation-manifest/v1"
_REQUEST_SCHEMA: Final[str] = "roi-workflow-invocation/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "generations",
        "revision_events",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "slide_id",
        "run_id",
        "generation_ids",
        "generation_id",
        "canonical_image_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "analysis_domain_path_object_id",
        "structure_id",
        "eligible_parent_id",
        "eligible_parent_ids",
        "action",
        "action_context",
        "classifier_reuse_decisions",
        "confirmed_marker_rules",
        "registration_context",
        "config",
        "detector_settings_hash",
        "tissue_setup_result_ref_sha256",
        "tissue_setup_result_sha256",
        "detector_validation",
        "tissue_resolution_selection",
        "native_pyramid_inventory",
        "native_level_read_attestation",
        "generation_history",
        "accepted_generation_id",
        "max_display_number",
        "sample_id",
        "review_required",
        "new_native_read",
    }
)
_SECTION_INVENTORY_FIELDS = frozenset(
    {
        "section_id",
        "roi_id",
        "path_object_id",
        "display_number",
        "revision",
        "review_status",
        "geometry_sha256",
        "provenance_pointer",
        "derived_from",
        "supersedes",
    }
)
_GENERATION_LINEAGE_FIELDS = frozenset(
    {
        "generation_id",
        "status",
        "derived_from_generation_id",
        "generation_content_sha256",
    }
)
_CHANNEL_FIELDS = frozenset({"channel_id", "name", "component", "fluorophore", "biomarker"})
_ACCEPTED_TISSUE_LIVE_ISSUER = object()

_SCIENTIFIC_TOP_LEVEL_FIELDS = frozenset(
    {
        "sample_id",
        "slide_id",
        "image_role",
        "project_entry_id",
        "canonical_image_id",
        "canonical_image_sha256",
        "current_image_identity_sha256",
        "analysis_frame_ref_sha256",
        "tissue_channel_result_ref_sha256",
        "tissue_setup_result_ref_sha256",
        "tissue_setup_result_sha256",
        "generation_id",
        "generation_status",
        "generation_content_sha256",
        "manifest_generation_ids",
        "prior_accepted_generation_id",
        "generation_lineage_sha256",
        "section_ids",
        "roi_ids",
        "path_object_ids",
        "section_inventory_sha256",
        "tissue_detection_config_sha256",
        "detector_id",
        "detector_version",
        "classifier_name",
        "classifier_sha256",
        "detector_settings_hash",
        "reference_channel_id",
        "reference_channel_selection_source",
        "stage_version",
        "review_policy",
        "validation_domain_id",
        "detector_validation_sha256",
        "canonical_channels_sha256",
        "tissue_resolution_selection_sha256",
        "native_pyramid_inventory_sha256",
        "native_level_read_attestation_sha256",
        "tissue_detection_qc_sha256",
        "revision_events_sha256",
        "wire_stage_status",
        "tissue_scientific_identity_sha256",
    }
)
_TASK10_FIELDS = frozenset(
    {
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "pipeline_run_id",
        "input_artifact_set_sha256",
        "prerequisite_artifact_ref_sha256s",
        "target_request_sha256",
        "authorized_entry_sha256",
        "generation_manifest_payload_sha256",
        "generation_manifest_byte_count",
        "result_payload_sha256",
        "result_byte_count",
        "process_evidence_sha256",
        "project_snapshot_sha256",
        "saved_state_disposition",
        "saved_state_before_sha256",
        "saved_state_after_sha256",
        "scientific_request_body_sha256",
        "scientific_request_body_byte_count",
        "resolved_config_sha256",
        "request_capability_sha256",
        "validation_input_authority_sha256",
        "execution_exchange_sha256",
        "validated_exchange_sha256",
        "adapter_spec_sha256",
        "projection_target_sha256",
        "script_sha256",
        "project_identity_sha256",
        "project_authority_head_sha256",
        "project_qpproj_snapshot_sha256",
        "project_entry_projection_sha256",
        "compatibility_signature_sha256",
        "request_codec_id",
        "request_codec_version",
        "task10_execution_identity_sha256",
    }
)
_NESTED_SCIENTIFIC_LEAF_FIELDS = frozenset(
    {
        "generation_lineage.generation_id",
        "generation_lineage.status",
        "generation_lineage.derived_from_generation_id",
        "generation_lineage.generation_content_sha256",
        "section_inventory.section_id",
        "section_inventory.roi_id",
        "section_inventory.path_object_id",
        "section_inventory.display_number",
        "section_inventory.revision",
        "section_inventory.review_status",
        "section_inventory.geometry_sha256",
        "section_inventory.provenance_pointer",
        "section_inventory.derived_from",
        "section_inventory.supersedes",
        "canonical_channels.channel_id",
        "canonical_channels.name",
        "canonical_channels.component",
        "canonical_channels.fluorophore",
        "canonical_channels.biomarker",
        "tissue_resolution_selection.schema_version",
        "tissue_resolution_selection.selection_source",
        "tissue_resolution_selection.native_pyramid_inventory_sha256",
        "tissue_resolution_selection.native_resolution_index",
        "tissue_resolution_selection.x_downsample",
        "tissue_resolution_selection.y_downsample",
        "tissue_resolution_selection.x_um_per_px",
        "tissue_resolution_selection.y_um_per_px",
        "tissue_resolution_selection.resolution_source",
        "tissue_resolution_selection.coordinate_system",
        "tissue_resolution_selection.level0_opt_in",
        "tissue_resolution_selection.selection_record_sha256",
        "native_pyramid_inventory.native_resolution_index",
        "native_pyramid_inventory.x_downsample",
        "native_pyramid_inventory.y_downsample",
        "native_pyramid_inventory.x_um_per_px",
        "native_pyramid_inventory.y_um_per_px",
        "native_level_read_attestation.native_resolution_index",
        "native_level_read_attestation.requested_downsample",
        "native_level_read_attestation.returned_width_px",
        "native_level_read_attestation.returned_height_px",
        "native_level_read_attestation.resolution_source",
        "native_level_read_attestation.coordinate_system",
        "native_level_read_attestation.selection_record_sha256",
    }
)

_COMMON_IDENTITY_FIELDS = (
    _SCIENTIFIC_TOP_LEVEL_FIELDS | _TASK10_FIELDS | _NESTED_SCIENTIFIC_LEAF_FIELDS
)
TISSUE_CANDIDATE_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(sorted(_COMMON_IDENTITY_FIELDS))
TISSUE_ACCEPTANCE_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(
        _COMMON_IDENTITY_FIELDS
        | {
            "authoritative_accepted_generation_id",
            "prior_candidate_result_ref_sha256",
            "prior_candidate_state_head_sha256",
            "prior_candidate_active_plan_sha256",
            "prior_candidate_terminal_record_sha256",
            "prior_candidate_execution_exchange_sha256",
            "prior_candidate_saved_state_after_sha256",
            "prior_candidate_generation_content_sha256",
            "prior_candidate_section_inventory_sha256",
            "acceptance_node_id",
            "acceptance_attempt_id",
            "acceptance_attempt_authorization_sha256",
            "acceptance_execution_context_sha256",
            "acceptance_state_head_sha256",
            "acceptance_active_plan_sha256",
            "acceptance_input_artifact_set_sha256",
            "acceptance_projection_target_sha256",
            "acceptance_compatibility_signature_sha256",
            "acceptance_dispatch_binding_sha256",
            "acceptance_node_material_sha256",
        }
    )
)

_SCIENTIFIC_DIGEST_FIELDS = tuple(
    sorted(
        (_SCIENTIFIC_TOP_LEVEL_FIELDS - {"tissue_scientific_identity_sha256"})
        | {
            "generation_lineage",
            "section_inventory",
            "canonical_channels",
            "tissue_resolution_selection",
            "native_pyramid_inventory",
            "native_level_read_attestation",
        }
    )
)
_TASK10_DIGEST_FIELDS = tuple(sorted(_TASK10_FIELDS - {"task10_execution_identity_sha256"}))
_CANDIDATE_TOP_LEVEL_FIELDS = frozenset(
    {
        *_SCIENTIFIC_TOP_LEVEL_FIELDS,
        *_TASK10_FIELDS,
        "generation_lineage",
        "section_inventory",
        "canonical_channels",
        "tissue_resolution_selection",
        "native_pyramid_inventory",
        "native_level_read_attestation",
    }
)
_ACCEPTANCE_LINK_FIELDS = frozenset(
    {
        "authoritative_accepted_generation_id",
        "prior_candidate_result_ref_sha256",
        "prior_candidate_state_head_sha256",
        "prior_candidate_active_plan_sha256",
        "prior_candidate_terminal_record_sha256",
        "prior_candidate_execution_exchange_sha256",
        "prior_candidate_saved_state_after_sha256",
        "prior_candidate_generation_content_sha256",
        "prior_candidate_section_inventory_sha256",
    }
)
_ACCEPTANCE_TRANSITION_FIELDS = frozenset(
    {
        "acceptance_node_id",
        "acceptance_attempt_id",
        "acceptance_attempt_authorization_sha256",
        "acceptance_execution_context_sha256",
        "acceptance_state_head_sha256",
        "acceptance_active_plan_sha256",
        "acceptance_input_artifact_set_sha256",
        "acceptance_projection_target_sha256",
        "acceptance_compatibility_signature_sha256",
        "acceptance_dispatch_binding_sha256",
        "acceptance_node_material_sha256",
    }
)
_ACCEPTANCE_TOP_LEVEL_FIELDS = (
    _CANDIDATE_TOP_LEVEL_FIELDS | _ACCEPTANCE_LINK_FIELDS | _ACCEPTANCE_TRANSITION_FIELDS
)
_ACCEPTANCE_SCIENTIFIC_DIGEST_FIELDS = tuple(
    sorted(set(_SCIENTIFIC_DIGEST_FIELDS) | _ACCEPTANCE_LINK_FIELDS | _ACCEPTANCE_TRANSITION_FIELDS)
)


# ==============================================================================
#                             Batch tissue artifacts
# ==============================================================================


class _DecoderOnlyProjection:
    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError(f"{cls.__name__} is decoder-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        if cls.__module__ == __name__:
            return
        raise TypeError(f"{cls.__name__} cannot be subclassed")

    def __reduce__(self) -> object:
        raise TypeError(f"{type(self).__name__} is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError(f"{type(self).__name__} is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueCandidateResultArtifactRefProjection(_DecoderOnlyProjection):
    """Validated terminal tissue-candidate-node data without a state claim."""

    artifact_ref: BatchArtifactRef
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    analysis_frame_ref_sha256: str
    tissue_channel_result_ref_sha256: str
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    generation_id: str
    generation_status: str
    generation_content_sha256: str
    manifest_generation_ids: tuple[str, ...]
    prior_accepted_generation_id: str | None
    generation_lineage: tuple[Mapping[str, object], ...]
    section_inventory: tuple[Mapping[str, object], ...]
    section_inventory_sha256: str
    section_ids: tuple[str, ...]
    roi_ids: tuple[str, ...]
    path_object_ids: tuple[str, ...]
    tissue_resolution_selection: Mapping[str, object]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    native_level_read_attestation: Mapping[str, object]
    detector_id: str
    detector_version: str
    detector_settings_hash: str
    reference_channel_id: str | None
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    wire_stage_status: str


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueCandidateResultProjection(_DecoderOnlyProjection):
    """Validated terminal tissue-candidate-node data committed by batch state."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    analysis_frame_ref_sha256: str
    tissue_channel_result_ref_sha256: str
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    generation_id: str
    generation_status: str
    generation_content_sha256: str
    manifest_generation_ids: tuple[str, ...]
    prior_accepted_generation_id: str | None
    generation_lineage: tuple[Mapping[str, object], ...]
    section_inventory: tuple[Mapping[str, object], ...]
    section_inventory_sha256: str
    section_ids: tuple[str, ...]
    roi_ids: tuple[str, ...]
    path_object_ids: tuple[str, ...]
    tissue_resolution_selection: Mapping[str, object]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    native_level_read_attestation: Mapping[str, object]
    detector_id: str
    detector_version: str
    detector_settings_hash: str
    reference_channel_id: str | None
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    wire_stage_status: str


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueAcceptanceResultArtifactRefProjection(_DecoderOnlyProjection):
    """Validated locatorless accepted-generation data without state authority."""

    artifact_ref: BatchArtifactRef
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    analysis_frame_ref_sha256: str
    tissue_channel_result_ref_sha256: str
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    generation_id: str
    generation_status: str
    generation_content_sha256: str
    manifest_generation_ids: tuple[str, ...]
    authoritative_accepted_generation_id: str
    prior_candidate_result_ref_sha256: str
    prior_candidate_state_head_sha256: str
    prior_candidate_active_plan_sha256: str
    prior_candidate_terminal_record_sha256: str
    prior_candidate_execution_exchange_sha256: str
    prior_candidate_saved_state_after_sha256: str
    prior_candidate_generation_content_sha256: str
    prior_candidate_section_inventory_sha256: str
    acceptance_node_id: str
    acceptance_attempt_id: str
    acceptance_attempt_authorization_sha256: str
    acceptance_execution_context_sha256: str
    acceptance_state_head_sha256: str
    acceptance_active_plan_sha256: str
    acceptance_input_artifact_set_sha256: str
    acceptance_projection_target_sha256: str
    acceptance_compatibility_signature_sha256: str
    acceptance_dispatch_binding_sha256: str
    acceptance_node_material_sha256: str
    generation_lineage: tuple[Mapping[str, object], ...]
    section_inventory: tuple[Mapping[str, object], ...]
    section_inventory_sha256: str
    section_ids: tuple[str, ...]
    roi_ids: tuple[str, ...]
    path_object_ids: tuple[str, ...]
    tissue_resolution_selection: Mapping[str, object]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    native_level_read_attestation: Mapping[str, object]
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    wire_stage_status: str


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueAcceptanceResultProjection(_DecoderOnlyProjection):
    """Validated accepted-generation data authenticated by batch state."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    analysis_frame_ref_sha256: str
    tissue_channel_result_ref_sha256: str
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    generation_id: str
    generation_status: str
    generation_content_sha256: str
    manifest_generation_ids: tuple[str, ...]
    authoritative_accepted_generation_id: str
    prior_candidate_result_ref_sha256: str
    prior_candidate_state_head_sha256: str
    prior_candidate_active_plan_sha256: str
    prior_candidate_terminal_record_sha256: str
    prior_candidate_execution_exchange_sha256: str
    prior_candidate_saved_state_after_sha256: str
    prior_candidate_generation_content_sha256: str
    prior_candidate_section_inventory_sha256: str
    acceptance_node_id: str
    acceptance_attempt_id: str
    acceptance_attempt_authorization_sha256: str
    acceptance_execution_context_sha256: str
    acceptance_state_head_sha256: str
    acceptance_active_plan_sha256: str
    acceptance_input_artifact_set_sha256: str
    acceptance_projection_target_sha256: str
    acceptance_compatibility_signature_sha256: str
    acceptance_dispatch_binding_sha256: str
    acceptance_node_material_sha256: str
    generation_lineage: tuple[Mapping[str, object], ...]
    section_inventory: tuple[Mapping[str, object], ...]
    section_inventory_sha256: str
    section_ids: tuple[str, ...]
    roi_ids: tuple[str, ...]
    path_object_ids: tuple[str, ...]
    tissue_resolution_selection: Mapping[str, object]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    native_level_read_attestation: Mapping[str, object]
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    wire_stage_status: str


@dataclass(frozen=True, slots=True, init=False)
class AcceptedTissueSectionLiveAuthority(_DecoderOnlyProjection):
    """One exact accepted TissueSection retained from the Task-5 exchange."""

    section: TissueSection
    geometry_sha256: str
    geometry_digest_policy: str
    _issuer: object

    def __copy__(self) -> object:
        raise TypeError("AcceptedTissueSectionLiveAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("AcceptedTissueSectionLiveAuthority is noncopyable")

    def to_request_mapping(self) -> dict[str, object]:
        if self._issuer is not _ACCEPTED_TISSUE_LIVE_ISSUER:
            raise BatchArtifactError("accepted TissueSection live authority is foreign")
        return {
            "section_id": self.section.section_id,
            "tissue_roi_id": self.section.roi_id,
            "tissue_path_object_id": self.section.path_object_id,
            "tissue_generation_id": self.section.generation_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class ValidatedAcceptedTissueLiveAuthority(_DecoderOnlyProjection):
    """Full accepted generation reauthenticated from exact retained bytes."""

    committed: ValidatedTissueAcceptanceResultProjection
    generation: TissueSectionGeneration
    generations: tuple[TissueSectionGeneration, ...]
    sections: tuple[AcceptedTissueSectionLiveAuthority, ...]
    accepted_tissue_request_rows: tuple[Mapping[str, object], ...]
    _issuer: object

    def __copy__(self) -> object:
        raise TypeError("ValidatedAcceptedTissueLiveAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ValidatedAcceptedTissueLiveAuthority is noncopyable")


def _issue_accepted_tissue_section_live_authority(
    section: TissueSection,
) -> AcceptedTissueSectionLiveAuthority:
    value = object.__new__(AcceptedTissueSectionLiveAuthority)
    for name, item in {
        "section": section,
        "geometry_sha256": _digest(section.to_export_mapping()["geometry"]),
        "geometry_digest_policy": "canonical-geojson-sha256/v1",
        "_issuer": _ACCEPTED_TISSUE_LIVE_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


def reopen_committed_accepted_tissue_live_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
) -> ValidatedAcceptedTissueLiveAuthority:
    """Reopen the exact accepted TissueSection generation behind acceptance."""

    committed = decode_committed_tissue_acceptance_result(authority)
    if type(exchange_reopener) is not ProjectStageCommittedExchangeReopener:
        raise BatchArtifactError("accepted tissue reopen requires an exact exchange reopener")
    identity = authority.artifact_ref.owning_identity
    try:
        reopened = exchange_reopener._reopen_for_owner(
            authority,
            producer_artifact_ref_sha256=committed.prior_candidate_result_ref_sha256,
            producer_node_id=identity["node_id"],
            producer_attempt_id=identity["attempt_id"],
            producer_attempt_authorization_sha256=identity["attempt_authorization_sha256"],
            terminal_record_sha256=committed.prior_candidate_terminal_record_sha256,
            execution_exchange_sha256=(committed.prior_candidate_execution_exchange_sha256),
            expected_binding=(
                identity["adapter_spec_sha256"],
                identity["request_codec_id"],
                "tissue_detection",
                "tissue_candidate",
                identity["request_codec_version"],
            ),
        )
        evidence = reopened.evidence
        context, authorized, observation, validation_inputs = _validated_evidence_sources(
            evidence,
            node_kind="tissue_candidate",
            contract=tissue_candidate_result_identity_contract(),
        )
        config = AnalysisConfig.from_mapping(_plain(validation_inputs.resolved_config))
        frame_refs = [ref for ref in context.inputs if ref.artifact_kind == "analysis_frame"]
        if len(frame_refs) != 1:
            raise BatchArtifactError(
                "accepted tissue exchange requires its exact retained frame input"
            )
        frame_ref = frame_refs[0]
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        if (
            frame_ref.artifact_ref_sha256 != committed.analysis_frame_ref_sha256
            or frame.spatial_frame.canonical_image_id != committed.canonical_image_id
            or frame.spatial_frame.canonical_image_sha256 != committed.canonical_image_sha256
        ):
            raise BatchArtifactError("accepted tissue retained frame lineage differs")
        resolved_channels = resolve_analysis_channels(
            config,
            frame_ref,
            sample_id=authorized.sample_id,
            validation_domain_id=frame_ref.scope.validation_domain_id,
        )
        setup_refs = [ref for ref in context.inputs if ref.artifact_kind == "tissue_setup_result"]
        if len(setup_refs) != 1:
            raise BatchArtifactError(
                "accepted tissue exchange requires its exact retained setup input"
            )
        setup = validate_tissue_setup_result_artifact_ref(setup_refs[0])
        if (
            setup.artifact_ref.artifact_ref_sha256 != committed.tissue_setup_result_ref_sha256
            or setup.tissue_setup_result_sha256 != committed.tissue_setup_result_sha256
            or setup.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
            or setup.resolved_config_sha256 != validation_inputs.resolved_config_sha256
            or setup.canonical_image_id != committed.canonical_image_id
            or setup.canonical_image_sha256 != committed.canonical_image_sha256
        ):
            raise BatchArtifactError("accepted tissue retained setup lineage differs")
        request, selection, _levels, attestation = _parse_tissue_request(
            validation_inputs.scientific_request_body,
            authorized=authorized,
            config=config,
            resolved_channels=resolved_channels,
            tissue=setup.tissue_detection,
            setup=setup,
        )
        _manifest, generations = _parse_tissue_manifest(
            evidence,
            authorized=authorized,
            tissue=setup.tissue_detection,
            selection=selection,
            attestation=attestation,
            canonical_image_id=committed.canonical_image_id,
            canonical_image_sha256=committed.canonical_image_sha256,
        )
    except (KeyError, ProjectStageRecordError, ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("accepted tissue retained exchange cannot be reopened") from exc
    selected = tuple(item for item in generations if item.generation_id == committed.generation_id)
    if len(selected) != 1:
        raise BatchArtifactError("accepted tissue retained generation is missing or ambiguous")
    generation = selected[0]
    lineage = _generation_lineage(generations)
    inventory = _section_inventory(generation)
    reopened_task10 = _task10_identity(
        context=context,
        authorized=authorized,
        observation=observation,
        validation_inputs=validation_inputs,
        evidence=evidence,
    )
    reopened_task10["task10_execution_identity_sha256"] = _identity_digest(
        reopened_task10, _TASK10_DIGEST_FIELDS
    )
    if (
        generation.status is not GenerationStatus.ACCEPTED
        or tuple(item.generation_id for item in generations) != committed.manifest_generation_ids
        or _plain(lineage) != _plain(committed.generation_lineage)
        or _plain(inventory) != _plain(committed.section_inventory)
        or _digest(generation.to_mapping()) != committed.generation_content_sha256
        or _digest(list(inventory)) != committed.section_inventory_sha256
        or request["accepted_generation_id"] != generation.derived_from_generation_id
        or evidence.execution_exchange_sha256 != committed.prior_candidate_execution_exchange_sha256
        or observation.saved_state_after_sha256
        != committed.prior_candidate_saved_state_after_sha256
        or any(
            _plain(reopened_task10[field]) != _plain(identity[field]) for field in _TASK10_FIELDS
        )
    ):
        raise BatchArtifactError("accepted tissue compact and retained authority differ")
    sections = tuple(
        _issue_accepted_tissue_section_live_authority(item)
        for item in sorted(generation.sections, key=lambda item: item.section_id)
    )
    if (
        tuple(item.section.section_id for item in sections) != committed.section_ids
        or tuple(sorted(item.section.roi_id for item in sections)) != committed.roi_ids
        or tuple(sorted(item.section.path_object_id for item in sections))
        != committed.path_object_ids
    ):
        raise BatchArtifactError("accepted tissue retained section identities differ")
    rows = tuple(MappingProxyType(item.to_request_mapping()) for item in sections)
    value = object.__new__(ValidatedAcceptedTissueLiveAuthority)
    for name, item in {
        "committed": committed,
        "generation": generation,
        "generations": generations,
        "sections": sections,
        "accepted_tissue_request_rows": rows,
        "_issuer": _ACCEPTED_TISSUE_LIVE_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


def _validate_accepted_tissue_live_authority(
    value: object,
) -> ValidatedAcceptedTissueLiveAuthority:
    """Validate accepted tissue live authority for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(value) is not ValidatedAcceptedTissueLiveAuthority
        or value._issuer is not _ACCEPTED_TISSUE_LIVE_ISSUER
        or type(value.committed) is not ValidatedTissueAcceptanceResultProjection
        or type(value.generation) is not TissueSectionGeneration
        or type(value.generations) is not tuple
        or any(type(item) is not TissueSectionGeneration for item in value.generations)
        or sum(item is value.generation for item in value.generations) != 1
        or any(
            type(item) is not AcceptedTissueSectionLiveAuthority
            or item._issuer is not _ACCEPTED_TISSUE_LIVE_ISSUER
            or type(item.section) is not TissueSection
            or item.geometry_digest_policy != "canonical-geojson-sha256/v1"
            or item.geometry_sha256 != _digest(item.section.to_export_mapping()["geometry"])
            for item in value.sections
        )
        or value.sections != tuple(sorted(value.sections, key=lambda item: item.section.section_id))
        or value.accepted_tissue_request_rows
        != tuple(item.to_request_mapping() for item in value.sections)
        or tuple(item.section for item in value.sections)
        != tuple(sorted(value.generation.sections, key=lambda item: item.section_id))
        or any(
            item.section is not expected
            for item, expected in zip(
                value.sections,
                sorted(value.generation.sections, key=lambda item: item.section_id),
                strict=True,
            )
        )
        or value.generation.generation_id != value.committed.generation_id
    ):
        raise BatchArtifactError("accepted tissue live authority is foreign")
    return value


def _projection(kind: type[object], **values: object) -> object:
    result = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def tissue_candidate_result_identity_contract() -> ArtifactIdentityContract:
    """Return the exact owner contract for a completed tissue-candidate node."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-tissue-candidate-result-identity/v1",
        artifact_kind="tissue_candidate_result",
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=TISSUE_CANDIDATE_RESULT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def tissue_acceptance_result_identity_contract() -> ArtifactIdentityContract:
    """Return the exact owner contract for one accepted tissue generation."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-tissue-acceptance-result-identity/v1",
        artifact_kind="tissue_acceptance_result",
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=TISSUE_ACCEPTANCE_RESULT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise BatchArtifactError("tissue evidence requires string keys")
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    if value is None or type(value) in {str, bool, int, float}:
        return value
    raise BatchArtifactError("tissue evidence must contain passive JSON values")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
        raise BatchArtifactError("tissue evidence is not finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()






def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _sha256(value: object, field: str) -> str:
    result = _text(value, field)
    if _SHA256.fullmatch(result) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return result


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return tuple(value)


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} does not have its exact closed fields")
    return value


def _identity_digest(identity: Mapping[str, object], fields: tuple[str, ...]) -> str:
    return _digest({field: identity[field] for field in fields})




def _validated_evidence_sources(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    node_kind: str,
    contract: ArtifactIdentityContract,
) -> tuple[
    BatchNodeExecutionContext,
    ProjectStageAuthorizedEntry,
    ProjectStageTargetObservation,
    object,
]:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError(
            "tissue artifact composition requires issuer-owned execution evidence"
        )
    try:
        validation_inputs = evidence.reopen_validation_inputs()
    except ProjectStageRecordError as exc:
        raise BatchArtifactError(
            "tissue execution evidence cannot reopen validation inputs"
        ) from exc
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        type(context) is not BatchNodeExecutionContext
        or type(authorized) is not ProjectStageAuthorizedEntry
        or type(observation) is not ProjectStageTargetObservation
        or context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or context.authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
        or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
        or context.node_id != authorized.node_id
        or context.adapter_spec.spec_sha256 != authorized.adapter_spec_sha256
        or context.adapter_spec.node_kind != node_kind
        or tuple(context.adapter_spec.declared_outputs) != (contract.artifact_kind,)
        or context.adapter_spec.output_identity_contracts != (contract,)
        or authorized.wire_stage_id != "tissue_detection"
        or authorized.terminal_stage_id != node_kind
        or authorized.image_role != "primary_mif"
        or authorized.request_codec_id != QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value
        or authorized.request_codec_version != "1"
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.attempt_authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or observation.saved_state_before_sha256 != authorized.saved_state_before_sha256
        or observation.saved_state_disposition == "unattestable"
        or observation.saved_state_after_sha256 is None
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
        or observation.result_ref != authorized.exchange.result_ref
        or observation.generation_manifest_ref != authorized.exchange.generation_manifest_ref
    ):
        raise BatchArtifactError("tissue execution evidence sources disagree")
    # The immutable parsed result retains its original source-member identity.
    # The current input and scientific scope checks above establish this use.
    return context, authorized, observation, validation_inputs


def _parse_tissue_request(
    value: object,
    *,
    authorized: ProjectStageAuthorizedEntry,
    config: AnalysisConfig,
    resolved_channels: tuple[ChannelSpec, ...],
    tissue: TissueDetectionConfig,
    setup: object,
) -> tuple[
    Mapping[str, object],
    TissueResolutionSelection,
    tuple[NativePyramidLevel, ...],
    NativeLevelReadAttestation | None,
]:
    request = _closed_mapping(_plain(value), _REQUEST_FIELDS, "tissue scientific request")
    if not resolved_channels or (
        config.channels is not None and tuple(config.channels) != resolved_channels
    ):
        raise BatchArtifactError("tissue request processed channels differ")
    expected_tissue_config = {
        "tissue_detection": tissue.to_mapping(),
        "channels": [item.to_mapping() for item in resolved_channels],
    }
    if (
        request["schema_version"] != _REQUEST_SCHEMA
        or request["stage"] not in {"tissue_candidate", "tissue_acceptance"}
        or request["sample_id"] != authorized.sample_id
        or request["slide_id"] != authorized.slide_id
        or request["run_id"] != authorized.pipeline_run_id
        or request["config"] != expected_tissue_config
        or request["tissue_setup_result_ref_sha256"] != setup.artifact_ref.artifact_ref_sha256
        or request["tissue_setup_result_sha256"] != setup.tissue_setup_result_sha256
        or request["detector_settings_hash"] != tissue.settings_hash
        or request["detector_validation"] is not None
        or type(request["review_required"]) is not bool
        or type(request["new_native_read"]) is not bool
        or (request["new_native_read"] and not request["review_required"])
        or any(
            request[field] is not None
            for field in (
                "section_id",
                "analysis_domain_id",
                "analysis_domain_revision",
                "analysis_domain_revision_sha256",
                "analysis_domain_path_object_id",
                "structure_id",
                "eligible_parent_id",
            )
        )
        or tuple(_sequence(request["eligible_parent_ids"], "eligible_parent_ids")) != ()
    ):
        raise BatchArtifactError("authenticated tissue request scope differs")
    try:
        selection = TissueResolutionSelection.from_mapping(request["tissue_resolution_selection"])
        levels = NativePyramidLevel.sequence_from_mappings(
            _sequence(request["native_pyramid_inventory"], "native_pyramid_inventory")
        )
        selection.validate_inventory_binding(tuple(item.to_mapping() for item in levels))
        raw_attestation = request["native_level_read_attestation"]
        attestation = (
            None
            if raw_attestation is None
            else NativeLevelReadAttestation.from_mapping(raw_attestation)
        )
        if attestation is not None:
            attestation.validate_selection_binding(selection)
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue request resolution evidence is invalid") from exc
    return request, selection, levels, attestation


def _parse_tissue_manifest(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    authorized: ProjectStageAuthorizedEntry,
    tissue: TissueDetectionConfig,
    selection: TissueResolutionSelection,
    attestation: NativeLevelReadAttestation | None,
    canonical_image_id: str,
    canonical_image_sha256: str,
) -> tuple[Mapping[str, object], tuple[TissueSectionGeneration, ...]]:
    manifest = _closed_mapping(
        evidence.generation_manifest,
        _MANIFEST_FIELDS,
        "tissue generation manifest",
    )
    if (
        manifest["schema_version"] != _MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != "tissue_detection"
        or manifest["slide_id"] != authorized.slide_id
    ):
        raise BatchArtifactError("tissue generation manifest identity differs")
    try:
        generations = validate_generation_manifest(
            _sequence(manifest["generations"], "manifest.generations"),
            tissue_detection=tissue,
            expected_canonical_image_id=canonical_image_id,
            expected_canonical_image_sha256=canonical_image_sha256,
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue generation manifest science is invalid") from exc
    manifest_ids = tuple(
        _text(item, "manifest generation ID")
        for item in _sequence(manifest["generation_ids"], "manifest.generation_ids")
    )
    if manifest_ids != tuple(item.generation_id for item in generations):
        raise BatchArtifactError("tissue manifest generation inventory differs")
    expected_selection = selection.to_mapping()
    expected_attestation = None if attestation is None else attestation.to_mapping()
    for generation in generations:
        if (
            generation.slide_id != authorized.slide_id
            or generation.canonical_image_id != canonical_image_id
            or generation.canonical_image_sha256 != canonical_image_sha256
            or (generation.generation_id == evidence.wire_stage_result.generation_id and (
                generation.detector_id != tissue.detector_id
                or generation.detector_version != tissue.detector_version
                or generation.settings_hash != tissue.settings_hash
                or generation.reference_channel_id != tissue.reference_channel_id
                or generation.reference_channel_selection_source
                != tissue.reference_channel_selection_source
                or generation.stage_version != tissue.stage_version
            ))
            or (generation.generation_id == evidence.wire_stage_result.generation_id
                and generation.tissue_resolution_selection.to_mapping() != expected_selection)
            or (
                generation.generation_id == evidence.wire_stage_result.generation_id
                and expected_attestation is not None
                and generation.native_level_read_attestation.to_mapping() != expected_attestation
            )
        ):
            raise BatchArtifactError(
                "tissue generation differs from request detector/image authority"
            )
    return manifest, generations


def _generation_lineage(
    generations: tuple[TissueSectionGeneration, ...],
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "generation_id": item.generation_id,
            "status": item.status.value,
            "derived_from_generation_id": item.derived_from_generation_id,
            "generation_content_sha256": _digest(item.to_mapping()),
        }
        for item in generations
    )


def _section_inventory(
    generation: TissueSectionGeneration,
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "section_id": section.section_id,
            "roi_id": section.roi_id,
            "path_object_id": section.path_object_id,
            "display_number": section.display_number,
            "revision": section.revision,
            "review_status": section.review_status.value,
            "geometry_sha256": _digest(section.to_export_mapping()["geometry"]),
            "provenance_pointer": section.provenance_pointer,
            "derived_from": list(sorted(section.derived_from)),
            "supersedes": list(sorted(section.supersedes)),
        }
        for section in sorted(generation.sections, key=lambda item: item.section_id)
    )


def _task10_identity(
    *,
    context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    observation: ProjectStageTargetObservation,
    validation_inputs: object,
    evidence: ProjectStageTargetExecutionEvidence,
) -> dict[str, object]:
    return {
        "node_id": authorized.node_id,
        "attempt_id": authorized.attempt_id,
        "attempt_authorization_sha256": authorized.attempt_authorization_sha256,
        "pipeline_run_id": authorized.pipeline_run_id,
        "input_artifact_set_sha256": authorized.input_artifact_set_sha256,
        "prerequisite_artifact_ref_sha256s": list(authorized.prerequisite_artifact_ref_sha256s),
        "target_request_sha256": authorized.target_request_sha256,
        "authorized_entry_sha256": authorized.authorized_entry_sha256,
        "generation_manifest_payload_sha256": (observation.generation_manifest_payload_sha256),
        "generation_manifest_byte_count": observation.generation_manifest_byte_count,
        "result_payload_sha256": observation.result_payload_sha256,
        "result_byte_count": observation.result_byte_count,
        "process_evidence_sha256": observation.process_evidence_sha256,
        "project_snapshot_sha256": observation.project_snapshot_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "saved_state_before_sha256": authorized.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "scientific_request_body_sha256": (validation_inputs.scientific_request_body_sha256),
        "scientific_request_body_byte_count": (
            validation_inputs.scientific_request_body_byte_count
        ),
        "resolved_config_sha256": validation_inputs.resolved_config_sha256,
        "request_capability_sha256": validation_inputs.request_capability_sha256,
        "validation_input_authority_sha256": validation_inputs.authority_sha256,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "projection_target_sha256": context.projection_target_sha256,
        "script_sha256": context.script_sha256,
        "project_identity_sha256": context.project_identity_sha256,
        "project_authority_head_sha256": context.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }


def _scientific_identity(
    *,
    context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    config: AnalysisConfig,
    channels: Sequence[object],
    tissue: TissueDetectionConfig,
    request: Mapping[str, object],
    manifest: Mapping[str, object],
    generations: tuple[TissueSectionGeneration, ...],
    generation: TissueSectionGeneration,
    tissue_qc: TissueDetectionQC | None,
    frame_ref: BatchArtifactRef,
    tissue_channel_result_ref_sha256: str,
    wire_stage_status: StageStatus,
) -> dict[str, object]:
    lineage = _generation_lineage(generations)
    inventory = _section_inventory(generation)
    channels = tuple(
        sorted(
            (item.to_mapping() for item in channels),
            key=lambda item: item["channel_id"],
        )
    )
    levels = tuple(
        item.to_mapping()
        for item in NativePyramidLevel.sequence_from_mappings(
            _sequence(request["native_pyramid_inventory"], "native_pyramid_inventory")
        )
    )
    prior_accepted_generation_id = generation.derived_from_generation_id
    if prior_accepted_generation_id is not None:
        prior = tuple(
            item for item in generations if item.generation_id == prior_accepted_generation_id
        )
        if len(prior) != 1 or prior[0].status not in {
            GenerationStatus.ACCEPTED,
            GenerationStatus.SUPERSEDED,
        }:
            raise BatchArtifactError(
                "tissue generation predecessor lacks accepted lineage authority"
            )
    return {
        "sample_id": authorized.sample_id,
        "slide_id": authorized.slide_id,
        "image_role": authorized.image_role,
        "project_entry_id": authorized.project_entry_id,
        "canonical_image_id": generation.canonical_image_id,
        "canonical_image_sha256": generation.canonical_image_sha256,
        "current_image_identity_sha256": context.current_image_identity_sha256,
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "tissue_channel_result_ref_sha256": tissue_channel_result_ref_sha256,
        "tissue_setup_result_ref_sha256": request["tissue_setup_result_ref_sha256"],
        "tissue_setup_result_sha256": request["tissue_setup_result_sha256"],
        "generation_id": generation.generation_id,
        "generation_status": generation.status.value,
        "generation_content_sha256": _digest(generation.to_mapping()),
        "manifest_generation_ids": [item.generation_id for item in generations],
        "prior_accepted_generation_id": prior_accepted_generation_id,
        "generation_lineage": list(lineage),
        "generation_lineage_sha256": _digest(list(lineage)),
        "section_inventory": list(inventory),
        "section_ids": sorted(item["section_id"] for item in inventory),
        "roi_ids": sorted(item["roi_id"] for item in inventory),
        "path_object_ids": sorted(item["path_object_id"] for item in inventory),
        "section_inventory_sha256": _digest(list(inventory)),
        "tissue_detection_config_sha256": _digest(tissue.to_mapping()),
        "detector_id": tissue.detector_id,
        "detector_version": tissue.detector_version,
        "classifier_name": tissue.classifier_name,
        "classifier_sha256": tissue.classifier_sha256,
        "detector_settings_hash": tissue.settings_hash,
        "reference_channel_id": tissue.reference_channel_id,
        "reference_channel_selection_source": (tissue.reference_channel_selection_source),
        "stage_version": tissue.stage_version,
        "review_policy": tissue.review_policy.value,
        "validation_domain_id": tissue.validation_domain_id,
        "detector_validation_sha256": _digest(request["detector_validation"]),
        "canonical_channels": list(channels),
        "canonical_channels_sha256": _digest(list(channels)),
        "tissue_resolution_selection": generation.tissue_resolution_selection.to_mapping(),
        "tissue_resolution_selection_sha256": _digest(
            generation.tissue_resolution_selection.to_mapping()
        ),
        "native_pyramid_inventory": list(levels),
        "native_pyramid_inventory_sha256": _digest(list(levels)),
        "native_level_read_attestation": generation.native_level_read_attestation.to_mapping(),
        "native_level_read_attestation_sha256": _digest(
            generation.native_level_read_attestation.to_mapping()
        ),
        "tissue_detection_qc_sha256": _digest(
            None if tissue_qc is None else tissue_qc.to_mapping()
        ),
        "revision_events_sha256": _digest(manifest["revision_events"]),
        "wire_stage_status": wire_stage_status.value,
    }


def _candidate_inputs(
    context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    validation_inputs: object,
) -> tuple[BatchArtifactRef, BatchArtifactRef, object]:
    if len(context.inputs) != 3:
        raise BatchArtifactError("tissue candidate requires exact frame and channel prerequisites")
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if set(by_kind) != {"analysis_frame", "tissue_channel_result", "tissue_setup_result"}:
        raise BatchArtifactError("tissue candidate prerequisite kinds are invalid")
    frame_ref = by_kind["analysis_frame"]
    try:
        channel = validate_tissue_channel_result_artifact_ref(by_kind["tissue_channel_result"])
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "tissue candidate channel prerequisite lacks public owner authority"
        ) from exc
    channel_ref = channel.artifact_ref
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("tissue candidate frame authority is invalid") from exc
    if (
        authorized.prerequisite_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        or frame_ref.scope.sample_id != authorized.sample_id
        or frame_ref.scope.slide_id != authorized.slide_id
        or frame_ref.scope.project_entry_id is not None
        or channel_ref.scope.sample_id != authorized.sample_id
        or channel_ref.scope.slide_id != authorized.slide_id
        or channel_ref.scope.project_entry_id != authorized.project_entry_id
        or channel_ref.scope.analysis_domain_bindings
        or frame_ref.configuration_bindings
        != (
            ArtifactConfigurationBinding(
                authorized.sample_id, validation_inputs.resolved_config_sha256
            ),
        )
        or channel_ref.configuration_bindings != frame_ref.configuration_bindings
    ):
        raise BatchArtifactError("tissue candidate prerequisite scope differs")
    return frame_ref, channel, frame


def build_tissue_candidate_result_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
    review_revision: Mapping[str, object] | None = None,
) -> BatchNodeExecutionResult:
    """Publish tissue accepted by review or the configured native QC policy.

    We bind the returned sections to their image, detector and native read, then
    check the transition against the requested history. Manual acceptance retains
    the reviewed geometry; automatic acceptance requires passing QC under
    ``if_qc_failed`` because this batch route supplies no detector-validation record.
    Prior generations retain their contents when the accepted predecessor is
    superseded. An inconsistent result cannot become the downstream tissue input.
    """

    contract = tissue_candidate_result_identity_contract()
    context, authorized, observation, validation_inputs = _validated_evidence_sources(
        evidence, node_kind="tissue_candidate", contract=contract
    )
    frame_ref, channel, frame = _candidate_inputs(context, authorized, validation_inputs)
    setup = decode_committed_tissue_setup_result(tissue_setup_authority)
    tissue = setup.tissue_detection
    try:
        config = AnalysisConfig.from_mapping(_plain(validation_inputs.resolved_config))
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("resolved tissue configuration is invalid") from exc
    try:
        resolved_channels = resolve_analysis_channels(
            config,
            frame_ref,
            sample_id=authorized.sample_id,
            validation_domain_id=frame_ref.scope.validation_domain_id,
        )
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("tissue candidate analysis-frame channels differ") from exc
    selected_channel = next(
        (item for item in resolved_channels if item.channel_id == channel.channel_id),
        None,
    )
    if (
        selected_channel is None
        or channel.selected_channel != selected_channel.to_mapping()
        or channel.preflight_channel_ids != tuple(item.channel_id for item in resolved_channels)
        or channel.channel_id != tissue.reference_channel_id
        or channel.selection_source != tissue.reference_channel_selection_source
        or setup.artifact_ref not in context.inputs
        or setup.artifact_ref.configuration_bindings != frame_ref.configuration_bindings
        or setup.resolved_config_sha256 != validation_inputs.resolved_config_sha256
        or setup.sample_id != authorized.sample_id
        or setup.slide_id != authorized.slide_id
        or setup.project_entry_id != authorized.project_entry_id
        or setup.validation_domain_id != frame_ref.scope.validation_domain_id
        or setup.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or setup.canonical_image_id != frame.spatial_frame.canonical_image_id
        or setup.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
        or setup.project_identity_sha256 != context.project_identity_sha256
        or setup.project_entry_projection_sha256 != context.project_entry_projection_sha256
        or setup.project_saved_state_attestation_sha256
        != channel.project_saved_state_attestation_sha256
        or setup.artifact_ref.artifact_ref_sha256 != channel.tissue_setup_result_ref_sha256
        or setup.tissue_setup_result_sha256 != channel.tissue_setup_result_sha256
        or setup.native_pyramid_inventory != channel.native_pyramid_inventory
        or channel.selection.native_resolution_index
        not in setup.native_observation["compatible_native_resolution_indices"]
        or channel.resolved_config_sha256 != validation_inputs.resolved_config_sha256
        or channel.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or channel.canonical_image_id != frame.spatial_frame.canonical_image_id
        or channel.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
        or channel.tissue_detection_config_sha256 != _digest(tissue.to_mapping())
        or channel.project_identity_sha256 != context.project_identity_sha256
        or channel.project_entry_projection_sha256 != context.project_entry_projection_sha256
    ):
        raise BatchArtifactError(
            "tissue candidate channel prerequisite differs from detector or project authority"
        )
    request, selection, _levels, attestation = _parse_tissue_request(
        validation_inputs.scientific_request_body,
        authorized=authorized,
        config=config,
        resolved_channels=resolved_channels,
        tissue=tissue,
        setup=setup,
    )
    if request["stage"] != "tissue_candidate":
        raise BatchArtifactError("candidate request stage differs")
    if review_revision is not None:
        from .batch_settled_review_revision import SettledReviewRevision
        SettledReviewRevision.from_mapping(review_revision)
    if request["review_required"] != (review_revision is not None):
        raise BatchArtifactError("tissue review policy differs from its admitted return")
    if request["new_native_read"] and (review_revision is None or request["action"] != "repeat"):
        raise BatchArtifactError("a new tissue read requires its admitted resolution return")
    if request["action"] is not None and not (
        review_revision is not None and request["action"] == "repeat"
        or context.authorization.retry_ordinal > 0
        and context.authorization.interaction_response_sha256 is not None
        and context.authorization.interaction_predecessor_terminal_record_sha256 is not None
    ):
        raise BatchArtifactError("tissue continuation lacks its admitted review predecessor")
    if (
        selection.to_mapping() != channel.selection.to_mapping()
        or _digest([item.to_mapping() for item in _levels])
        != channel.native_pyramid_inventory_sha256
        or tuple(item.to_mapping() for item in _levels)
        != tuple(item.to_mapping() for item in channel.native_pyramid_inventory)
    ):
        raise BatchArtifactError(
            "candidate request resolution lineage differs from channel authority"
        )
    spatial_frame = frame.spatial_frame
    if (
        request["canonical_image_id"] != spatial_frame.canonical_image_id
        or spatial_frame.canonical_image_sha256 is None
    ):
        raise BatchArtifactError("candidate request image authority differs")
    manifest, generations = _parse_tissue_manifest(
        evidence,
        authorized=authorized,
        tissue=tissue,
        selection=selection,
        attestation=attestation,
        canonical_image_id=spatial_frame.canonical_image_id,
        canonical_image_sha256=spatial_frame.canonical_image_sha256,
    )
    result = evidence.wire_stage_result
    target = tuple(item for item in generations if item.generation_id == result.generation_id)
    if (
        result.stage != "tissue_detection"
        or result.slide_id != authorized.slide_id
        or len(target) != 1
    ):
        raise BatchArtifactError("candidate result and generation manifest authority differ")
    generation = target[0]
    if attestation is None:
        if (
            not request["new_native_read"] and (
            request["action"] is not None
            or tuple(_sequence(request["generation_history"], "generation_history"))
            or request["accepted_generation_id"] is not None
            or request["max_display_number"] != 0
            or len(generations) != 1)
        ):
            raise BatchArtifactError("candidate request lacks prior native read authority")
        attestation = generation.native_level_read_attestation
        try:
            attestation.validate_selection_binding(selection)
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchArtifactError(
                "candidate generation native read authority is invalid"
            ) from exc
    tissue_qc: TissueDetectionQC | None
    publish = result.status is StageStatus.COMPLETED
    if result.status is StageStatus.NEEDS_INTERACTION:
        resume = result.resume_context
        if (
            result.interaction_reason is not InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED
            or tuple(result.allowed_actions) != ("accept", "edit", "repeat")
            or result.outputs is not None
            or generation.status is not GenerationStatus.CANDIDATE
            or not isinstance(resume, Mapping)
            or resume.get("schema_version") != "tissue-section-review-interaction/v1"
            or resume.get("checkpoint_id") != result.checkpoint_id
            or resume.get("generation_id") != result.generation_id
            or resume.get("tissue_resolution_selection") != selection.to_mapping()
            or resume.get("native_level_read_attestation") != attestation.to_mapping()
            or resume.get("coordinate_system") != "global_level0_pixels"
        ):
            raise BatchArtifactError("candidate result and generation manifest authority differ")
        try:
            tissue_qc = TissueDetectionQC.from_mapping(resume.get("tissue_detection_qc"))
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchArtifactError("candidate tissue QC is invalid") from exc
        result_rois = tuple(result.affected_object_ids)
        generation_rois = {item.roi_id for item in generation.sections}
        if (
            not result_rois
            or len(set(result_rois)) != len(result_rois)
            or not set(result_rois).issubset(generation_rois)
        ):
            raise BatchArtifactError("candidate affected-object inventory differs")
    elif result.status is StageStatus.COMPLETED:
        outputs = result.outputs
        if (
            generation.status is not GenerationStatus.ACCEPTED
            or result.affected_object_ids
            or not isinstance(outputs, Mapping)
            or set(outputs)
            not in (
                {
                    "generation_status",
                    "tissue_resolution_selection",
                    "native_level_read_attestation",
                    "coordinate_system",
                },
                {
                    "generation_status",
                    "tissue_resolution_selection",
                    "native_level_read_attestation",
                    "coordinate_system",
                    "tissue_detection_qc",
                },
            )
            or outputs.get("generation_status") != "accepted"
            or outputs.get("tissue_resolution_selection") != selection.to_mapping()
            or outputs.get("native_level_read_attestation") != attestation.to_mapping()
            or outputs.get("coordinate_system") != "global_level0_pixels"
            or tuple(
                item.generation_id
                for item in generations
                if item.status is GenerationStatus.ACCEPTED
            )
            != (generation.generation_id,)
        ):
            raise BatchArtifactError(
                "completed candidate result lacks an accepted generation"
            )
        tissue_qc = None
        if "tissue_detection_qc" in outputs:
            try:
                tissue_qc = TissueDetectionQC.from_mapping(outputs["tissue_detection_qc"])
            except (ROIAnalysisError, TypeError, ValueError) as exc:
                raise BatchArtifactError("candidate tissue QC is invalid") from exc
        if request["action"] != "accept" and (
            request["action"] not in (None, "repeat")
            or request["review_required"]
            or tissue.review_policy is not ReviewPolicy.IF_QC_FAILED
            or tissue_qc is None
            or not tissue_qc.passes(
                {"min_tissue_area_fraction": 0.0, "max_tissue_area_fraction": 1.0}
            )
            or tissue_qc.component_count != len(generation.sections)
        ):
            raise BatchArtifactError(
                "automatic tissue acceptance requires the configured passing-QC route"
            )
    else:
        raise BatchArtifactError("candidate result and generation manifest authority differ")
    request_history = tuple(
        TissueSectionGeneration.from_mapping(item)
        for item in _sequence(request["generation_history"], "generation_history")
    )
    request_ids = tuple(
        _text(item, "request generation ID")
        for item in _sequence(request["generation_ids"], "generation_ids")
    )
    request_history_ids = tuple(item.generation_id for item in request_history)
    manifest_ids = tuple(item.generation_id for item in generations)
    expected_history_ids = (
        manifest_ids if generation.generation_id in request_history_ids else manifest_ids[:-1]
    )
    request_accepted_ids = tuple(
        item.generation_id for item in request_history if item.status is GenerationStatus.ACCEPTED
    )
    manifest_by_id = {item.generation_id: item for item in generations}
    if (
        request_ids != ()
        or request_history_ids != expected_history_ids
        or request["accepted_generation_id"]
        != (request_accepted_ids[0] if len(request_accepted_ids) == 1 else None)
        or len(request_accepted_ids) > 1
        or (
            not publish
            and any(
                item.generation_id != generation.generation_id
                and item.to_mapping() != manifest_by_id[item.generation_id].to_mapping()
                for item in request_history
            )
        )
        or request["max_display_number"]
        != max(
            (item.display_number for item in request_history for item in item.sections),
            default=0,
        )
    ):
        raise BatchArtifactError("candidate request history authority differs")
    if publish:
        request_target = tuple(
            item for item in request_history if item.generation_id == generation.generation_id
        )
        output_by_id = {item.generation_id: item for item in generations}
        accepted_predecessor_id = request["accepted_generation_id"]
        accepted_predecessor = tuple(
            item for item in request_history if item.generation_id == accepted_predecessor_id
        )
        if (
            (request["action"] == "accept" and len(request_target) != 1)
            or not generation.sections
            or generation.derived_from_generation_id != accepted_predecessor_id
            or any(section.supersedes != section.derived_from for section in generation.sections)
            or (accepted_predecessor_id is None and accepted_predecessor)
            or (accepted_predecessor_id is not None and len(accepted_predecessor) != 1)
        ):
            raise BatchArtifactError(
                "completed candidate changed the reviewed generation authority"
            )
        if request_target:
            if (
                request_target[0].status is not GenerationStatus.CANDIDATE
                or not request_target[0].sections
            ):
                raise BatchArtifactError("completed candidate lacks a reviewable prior generation")
            try:
                canonical_transition = accept_tissue_candidate(
                    accepted_predecessor[0] if accepted_predecessor else None,
                    request_target[0],
                    expected_context=dict(request_target[0].sections[0].detector_context),
                    excluded_roi_ids=resolve_resume_envelope(request["action_context"]).response.get(
                        "excluded_roi_ids", ()
                    ),
                )
            except ROIAnalysisError as exc:
                raise BatchArtifactError("completed candidate generation promotion is invalid") from exc
            if canonical_transition[-1].to_mapping() != generation.to_mapping():
                raise BatchArtifactError(
                    "completed candidate changed the reviewed generation authority"
                )
        for prior in request_history:
            if prior.generation_id == generation.generation_id:
                continue
            expected = prior.to_mapping()
            if (
                prior.generation_id == request["accepted_generation_id"]
                and prior.status is GenerationStatus.ACCEPTED
            ):
                expected["status"] = GenerationStatus.SUPERSEDED.value
            if expected != output_by_id[prior.generation_id].to_mapping():
                raise BatchArtifactError("completed candidate changed reviewed generation history")
    else:
        return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, ())
    identity = _scientific_identity(
        context=context,
        authorized=authorized,
        config=config,
        channels=resolved_channels,
        tissue=tissue,
        request=request,
        manifest=manifest,
        generations=generations,
        generation=generation,
        tissue_qc=tissue_qc,
        frame_ref=frame_ref,
        tissue_channel_result_ref_sha256=channel.artifact_ref.artifact_ref_sha256,
        wire_stage_status=result.status,
    )
    identity.update(
        _task10_identity(
            context=context,
            authorized=authorized,
            observation=observation,
            validation_inputs=validation_inputs,
            evidence=evidence,
        )
    )
    identity["tissue_scientific_identity_sha256"] = _identity_digest(
        identity, _SCIENTIFIC_DIGEST_FIELDS
    )
    identity["task10_execution_identity_sha256"] = _identity_digest(identity, _TASK10_DIGEST_FIELDS)
    artifact = BatchArtifactRef(
        artifact_id="tissue-candidate-" + str(identity["generation_content_sha256"])[:48],
        artifact_kind=contract.artifact_kind,
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
            validation_domain_id=frame_ref.scope.validation_domain_id,
            sample_id=authorized.sample_id,
            slide_id=authorized.slide_id,
            project_entry_id=authorized.project_entry_id,
            analysis_domain_bindings=(),
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
        configuration_bindings=frame_ref.configuration_bindings,
    )
    return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (artifact,))


def _validate_tissue_artifact_ref(
    artifact_ref: BatchArtifactRef,
    *,
    acceptance: bool,
) -> tuple[BatchArtifactRef, Mapping[str, object], dict[str, object]]:
    """Validate tissue artifact ref for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    label = "acceptance" if acceptance else "candidate"
    if type(artifact_ref) is not BatchArtifactRef:
        raise BatchArtifactError(f"tissue {label} requires an exact artifact ref")
    ref = validate_artifact_ref(artifact_ref)
    contract = (
        tissue_acceptance_result_identity_contract()
        if acceptance
        else tissue_candidate_result_identity_contract()
    )
    if (
        ref.artifact_kind != contract.artifact_kind
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
        raise BatchArtifactError(f"tissue {label} lacks its locatorless owner contract")
    _text(ref.runtime_versions.get("qupath"), "runtime_versions.qupath")
    identity = _closed_mapping(
        ref.owning_identity,
        _ACCEPTANCE_TOP_LEVEL_FIELDS if acceptance else _CANDIDATE_TOP_LEVEL_FIELDS,
        f"tissue {label} identity",
    )
    sample_id = _text(identity["sample_id"], "sample_id")
    slide_id = _text(identity["slide_id"], "slide_id")
    project_entry_id = _text(identity["project_entry_id"], "project_entry_id")
    canonical_image_id = _text(identity["canonical_image_id"], "canonical_image_id")
    canonical_image_sha256 = _sha256(identity["canonical_image_sha256"], "canonical_image_sha256")
    generation_id = _text(identity["generation_id"], "generation_id")
    generation_status = _text(identity["generation_status"], "generation_status")
    manifest_ids = tuple(
        _text(item, "manifest generation ID")
        for item in _sequence(identity["manifest_generation_ids"], "manifest_generation_ids")
    )
    if not manifest_ids or len(set(manifest_ids)) != len(manifest_ids):
        raise BatchArtifactError("manifest generation IDs must be nonempty and unique")
    prior_accepted = _optional_text(
        identity["prior_accepted_generation_id"], "prior_accepted_generation_id"
    )
    lineage = tuple(
        _closed_mapping(item, _GENERATION_LINEAGE_FIELDS, "generation lineage row")
        for item in _sequence(identity["generation_lineage"], "generation_lineage")
    )
    lineage_ids = tuple(_text(item["generation_id"], "lineage generation_id") for item in lineage)
    lineage_statuses: list[GenerationStatus] = []
    for index, item in enumerate(lineage):
        try:
            lineage_statuses.append(GenerationStatus(item["status"]))
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("lineage status is invalid") from exc
        parent = _optional_text(item["derived_from_generation_id"], "derived_from_generation_id")
        if parent is not None and (
            parent not in lineage_ids[:index]
            or lineage_statuses[lineage_ids[:index].index(parent)]
            not in {GenerationStatus.ACCEPTED, GenerationStatus.SUPERSEDED}
        ):
            raise BatchArtifactError("compact generation lineage is invalid")
        _sha256(item["generation_content_sha256"], "generation_content_sha256")
    inventory = tuple(
        _closed_mapping(item, _SECTION_INVENTORY_FIELDS, "section inventory row")
        for item in _sequence(identity["section_inventory"], "section_inventory")
    )
    if not inventory:
        raise BatchArtifactError("section inventory must be nonempty")
    for item in inventory:
        for field in (
            "section_id",
            "roi_id",
            "path_object_id",
            "provenance_pointer",
        ):
            _text(item[field], f"section inventory {field}")
        _positive_integer(item["display_number"], "display_number")
        if type(item["revision"]) is not int or item["revision"] < 0:
            raise BatchArtifactError("revision must be a nonnegative integer")
        _text(item["review_status"], "review_status")
        _sha256(item["geometry_sha256"], "geometry_sha256")
        for field in ("derived_from", "supersedes"):
            values = tuple(_text(value, f"{field} item") for value in _sequence(item[field], field))
            if values != tuple(sorted(values)) or len(set(values)) != len(values):
                raise BatchArtifactError(f"{field} must be sorted and unique")
    section_ids = tuple(
        _text(item, "section ID") for item in _sequence(identity["section_ids"], "section_ids")
    )
    roi_ids = tuple(_text(item, "ROI ID") for item in _sequence(identity["roi_ids"], "roi_ids"))
    path_object_ids = tuple(
        _text(item, "PathObject ID")
        for item in _sequence(identity["path_object_ids"], "path_object_ids")
    )
    channel_rows = tuple(
        _closed_mapping(item, _CHANNEL_FIELDS, "canonical channel")
        for item in _sequence(identity["canonical_channels"], "canonical_channels")
    )
    for item in channel_rows:
        _text(item["channel_id"], "channel_id")
        for field in ("name", "component", "fluorophore", "biomarker"):
            _optional_text(item[field], f"canonical channel {field}")
    channel_ids = tuple(item["channel_id"] for item in channel_rows)
    reference_channel_id = _optional_text(identity["reference_channel_id"], "reference_channel_id")
    reference_channel_source = _optional_text(
        identity["reference_channel_selection_source"],
        "reference_channel_selection_source",
    )
    _optional_text(identity["validation_domain_id"], "validation_domain_id")
    if (
        channel_ids != tuple(sorted(channel_ids))
        or len(set(channel_ids)) != len(channel_ids)
        or (reference_channel_id is None and reference_channel_source is not None)
        or (
            reference_channel_id is not None
            and (
                reference_channel_id not in channel_ids
                or reference_channel_source not in {"canonical_metadata", "user_override"}
            )
        )
    ):
        raise BatchArtifactError("tissue compact channel authority differs")
    try:
        selection = TissueResolutionSelection.from_mapping(identity["tissue_resolution_selection"])
        levels = NativePyramidLevel.sequence_from_mappings(
            _sequence(identity["native_pyramid_inventory"], "native_pyramid_inventory")
        )
        selection.validate_inventory_binding(tuple(item.to_mapping() for item in levels))
        attestation = NativeLevelReadAttestation.from_mapping(
            identity["native_level_read_attestation"]
        )
        attestation.validate_selection_binding(selection)
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue compact resolution evidence is invalid") from exc
    sha_fields = (
        "adapter_spec_sha256",
        "analysis_frame_ref_sha256",
        "attempt_authorization_sha256",
        "authorized_entry_sha256",
        "canonical_channels_sha256",
        "classifier_sha256",
        "compatibility_signature_sha256",
        "current_image_identity_sha256",
        "detector_settings_hash",
        "detector_validation_sha256",
        "execution_exchange_sha256",
        "generation_content_sha256",
        "generation_lineage_sha256",
        "generation_manifest_payload_sha256",
        "input_artifact_set_sha256",
        "native_level_read_attestation_sha256",
        "native_pyramid_inventory_sha256",
        "process_evidence_sha256",
        "project_authority_head_sha256",
        "project_entry_projection_sha256",
        "project_identity_sha256",
        "project_qpproj_snapshot_sha256",
        "project_snapshot_sha256",
        "projection_target_sha256",
        "request_capability_sha256",
        "resolved_config_sha256",
        "result_payload_sha256",
        "revision_events_sha256",
        "saved_state_after_sha256",
        "saved_state_before_sha256",
        "scientific_request_body_sha256",
        "script_sha256",
        "section_inventory_sha256",
        "target_request_sha256",
        "task10_execution_identity_sha256",
        "tissue_detection_config_sha256",
        "tissue_detection_qc_sha256",
        "tissue_channel_result_ref_sha256",
        "tissue_setup_result_ref_sha256",
        "tissue_setup_result_sha256",
        "tissue_resolution_selection_sha256",
        "tissue_scientific_identity_sha256",
        "validated_exchange_sha256",
        "validation_input_authority_sha256",
    )
    for field in sha_fields:
        _sha256(identity[field], field)
    prerequisites = tuple(
        _sha256(item, "prerequisite artifact ref")
        for item in _sequence(
            identity["prerequisite_artifact_ref_sha256s"],
            "prerequisite_artifact_ref_sha256s",
        )
    )
    for field in (
        "node_id",
        "attempt_id",
        "pipeline_run_id",
        "detector_id",
        "detector_version",
        "classifier_name",
        "stage_version",
        "review_policy",
        "request_codec_id",
        "request_codec_version",
        "saved_state_disposition",
        "wire_stage_status",
    ):
        _text(identity[field], field)
    for field in (
        "generation_manifest_byte_count",
        "result_byte_count",
        "scientific_request_body_byte_count",
    ):
        _positive_integer(identity[field], field)
    expected_sections = tuple(item["section_id"] for item in inventory)
    expected_rois = tuple(sorted(item["roi_id"] for item in inventory))
    expected_objects = tuple(sorted(item["path_object_id"] for item in inventory))
    display_numbers = tuple(item["display_number"] for item in inventory)
    expected_prerequisites = tuple(
        sorted(
            (
                identity["analysis_frame_ref_sha256"],
                identity["tissue_channel_result_ref_sha256"],
                identity["tissue_setup_result_ref_sha256"],
            )
        )
    )
    expected_producer_node_id = (
        identity["acceptance_node_id"] if acceptance else identity["node_id"]
    )
    expected_producer_attempt_id = (
        identity["acceptance_attempt_id"] if acceptance else identity["attempt_id"]
    )
    expected_producer_authorization = (
        identity["acceptance_attempt_authorization_sha256"]
        if acceptance
        else identity["attempt_authorization_sha256"]
    )
    expected_compatibility = (
        identity["acceptance_compatibility_signature_sha256"]
        if acceptance
        else identity["compatibility_signature_sha256"]
    )
    target_lineage = tuple(item for item in lineage if item["generation_id"] == generation_id)
    target_status = tuple(
        status
        for item, status in zip(lineage, lineage_statuses, strict=True)
        if item["generation_id"] == generation_id
    )
    accepted_lineage_ids = tuple(
        item["generation_id"]
        for item, status in zip(lineage, lineage_statuses, strict=True)
        if status is GenerationStatus.ACCEPTED
    )
    if (
        lineage_ids != manifest_ids
        or len(target_lineage) != 1
        or len(target_status) != 1
        or target_status[0].value != generation_status
        or target_lineage[0]["generation_content_sha256"] != identity["generation_content_sha256"]
        or section_ids != tuple(sorted(section_ids))
        or section_ids != expected_sections
        or roi_ids != expected_rois
        or path_object_ids != expected_objects
        or len(set(section_ids)) != len(section_ids)
        or len(set(roi_ids)) != len(roi_ids)
        or len(set(path_object_ids)) != len(path_object_ids)
        or len(set(display_numbers)) != len(display_numbers)
        or identity["review_policy"]
        not in {"always", "if_unvalidated", "if_qc_failed", "unattended"}
        or _digest(list(lineage)) != identity["generation_lineage_sha256"]
        or _digest(list(inventory)) != identity["section_inventory_sha256"]
        or _digest([dict(item) for item in channel_rows]) != identity["canonical_channels_sha256"]
        or _digest(selection.to_mapping()) != identity["tissue_resolution_selection_sha256"]
        or _digest([item.to_mapping() for item in levels])
        != identity["native_pyramid_inventory_sha256"]
        or _digest(attestation.to_mapping()) != identity["native_level_read_attestation_sha256"]
        or identity["input_artifact_set_sha256"]
        != _digest({"artifact_ref_sha256s": list(prerequisites)})
        or prerequisites != expected_prerequisites
        or identity["request_codec_id"] != QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value
        or identity["request_codec_version"] != "1"
        or identity["saved_state_disposition"]
        not in {"authenticated_unchanged", "authenticated_changed"}
        or (
            (identity["saved_state_before_sha256"] == identity["saved_state_after_sha256"])
            != (identity["saved_state_disposition"] == "authenticated_unchanged")
        )
        or ref.producer_node_id != expected_producer_node_id
        or ref.producer_attempt_id != expected_producer_attempt_id
        or ref.producer_attempt_authorization_sha256 != expected_producer_authorization
        or ref.compatibility_signature_sha256 != expected_compatibility
        or ref.scope.validation_domain_id is None
        or ref.scope.sample_id != sample_id
        or ref.scope.slide_id != slide_id
        or ref.scope.project_entry_id != project_entry_id
        or ref.scope.analysis_domain_bindings
        or ref.configuration_bindings
        != (ArtifactConfigurationBinding(sample_id, identity["resolved_config_sha256"]),)
        or identity["image_role"] != "primary_mif"
        or _identity_digest(
            identity,
            (_ACCEPTANCE_SCIENTIFIC_DIGEST_FIELDS if acceptance else _SCIENTIFIC_DIGEST_FIELDS),
        )
        != identity["tissue_scientific_identity_sha256"]
        or _identity_digest(identity, _TASK10_DIGEST_FIELDS)
        != identity["task10_execution_identity_sha256"]
    ):
        raise BatchArtifactError(f"tissue {label} compact evidence differs")
    if acceptance:
        for field in _ACCEPTANCE_LINK_FIELDS - {"authoritative_accepted_generation_id"}:
            _sha256(identity[field], field)
        for field in _ACCEPTANCE_TRANSITION_FIELDS - {
            "acceptance_node_id",
            "acceptance_attempt_id",
        }:
            _sha256(identity[field], field)
        _text(identity["acceptance_node_id"], "acceptance_node_id")
        _text(identity["acceptance_attempt_id"], "acceptance_attempt_id")
        if (
            generation_status != GenerationStatus.ACCEPTED.value
            or identity["wire_stage_status"] != StageStatus.COMPLETED.value
            or accepted_lineage_ids != (generation_id,)
            or (
                prior_accepted is not None
                and (
                    prior_accepted not in lineage_ids
                    or lineage_statuses[lineage_ids.index(prior_accepted)]
                    is not GenerationStatus.SUPERSEDED
                )
            )
            or any(item["review_status"] != "accepted" for item in inventory)
            or _text(
                identity["authoritative_accepted_generation_id"],
                "authoritative_accepted_generation_id",
            )
            != generation_id
            or identity["prior_candidate_saved_state_after_sha256"]
            != identity["saved_state_after_sha256"]
            or identity["prior_candidate_execution_exchange_sha256"]
            != identity["execution_exchange_sha256"]
            or identity["prior_candidate_generation_content_sha256"]
            != identity["generation_content_sha256"]
            or identity["prior_candidate_section_inventory_sha256"]
            != identity["section_inventory_sha256"]
            or identity["prior_candidate_state_head_sha256"]
            != identity["acceptance_state_head_sha256"]
            or identity["prior_candidate_active_plan_sha256"]
            != identity["acceptance_active_plan_sha256"]
            or identity["acceptance_input_artifact_set_sha256"]
            != _digest(
                {
                    "artifact_ref_sha256s": sorted(
                        (
                            identity["analysis_frame_ref_sha256"],
                            identity["prior_candidate_result_ref_sha256"],
                            identity["tissue_setup_result_ref_sha256"],
                        )
                    )
                }
            )
        ):
            raise BatchArtifactError("tissue acceptance authority differs")
    elif (
        generation_status != GenerationStatus.ACCEPTED.value
        or identity["wire_stage_status"] != StageStatus.COMPLETED.value
        or tuple(
            item["generation_id"]
            for item, status in zip(lineage, lineage_statuses, strict=True)
            if status is GenerationStatus.ACCEPTED
        )
        != (generation_id,)
        or any(item["review_status"] != "accepted" for item in inventory)
    ):
        raise BatchArtifactError("tissue candidate status authority differs")
    common = {
        "artifact_ref": ref,
        "sample_id": sample_id,
        "slide_id": slide_id,
        "project_entry_id": project_entry_id,
        "canonical_image_id": canonical_image_id,
        "canonical_image_sha256": canonical_image_sha256,
        "analysis_frame_ref_sha256": identity["analysis_frame_ref_sha256"],
        "tissue_channel_result_ref_sha256": identity["tissue_channel_result_ref_sha256"],
        "tissue_setup_result_ref_sha256": identity["tissue_setup_result_ref_sha256"],
        "tissue_setup_result_sha256": identity["tissue_setup_result_sha256"],
        "generation_id": generation_id,
        "generation_status": generation_status,
        "generation_content_sha256": identity["generation_content_sha256"],
        "manifest_generation_ids": manifest_ids,
        "generation_lineage": lineage,
        "section_inventory": inventory,
        "section_inventory_sha256": identity["section_inventory_sha256"],
        "section_ids": section_ids,
        "roi_ids": roi_ids,
        "path_object_ids": path_object_ids,
        "tissue_resolution_selection": selection.to_mapping(),
        "native_pyramid_inventory": tuple(item.to_mapping() for item in levels),
        "native_level_read_attestation": attestation.to_mapping(),
        "saved_state_before_sha256": identity["saved_state_before_sha256"],
        "saved_state_after_sha256": identity["saved_state_after_sha256"],
        "execution_exchange_sha256": identity["execution_exchange_sha256"],
        "validated_exchange_sha256": identity["validated_exchange_sha256"],
        "wire_stage_status": identity["wire_stage_status"],
    }
    if acceptance:
        common.update(
            authoritative_accepted_generation_id=identity["authoritative_accepted_generation_id"],
            prior_candidate_result_ref_sha256=identity["prior_candidate_result_ref_sha256"],
            prior_candidate_state_head_sha256=identity["prior_candidate_state_head_sha256"],
            prior_candidate_active_plan_sha256=identity["prior_candidate_active_plan_sha256"],
            prior_candidate_terminal_record_sha256=identity[
                "prior_candidate_terminal_record_sha256"
            ],
            prior_candidate_execution_exchange_sha256=identity[
                "prior_candidate_execution_exchange_sha256"
            ],
            prior_candidate_saved_state_after_sha256=identity[
                "prior_candidate_saved_state_after_sha256"
            ],
            prior_candidate_generation_content_sha256=identity[
                "prior_candidate_generation_content_sha256"
            ],
            prior_candidate_section_inventory_sha256=identity[
                "prior_candidate_section_inventory_sha256"
            ],
            acceptance_node_id=identity["acceptance_node_id"],
            acceptance_attempt_id=identity["acceptance_attempt_id"],
            acceptance_attempt_authorization_sha256=identity[
                "acceptance_attempt_authorization_sha256"
            ],
            acceptance_execution_context_sha256=identity["acceptance_execution_context_sha256"],
            acceptance_state_head_sha256=identity["acceptance_state_head_sha256"],
            acceptance_active_plan_sha256=identity["acceptance_active_plan_sha256"],
            acceptance_input_artifact_set_sha256=identity["acceptance_input_artifact_set_sha256"],
            acceptance_projection_target_sha256=identity["acceptance_projection_target_sha256"],
            acceptance_compatibility_signature_sha256=identity[
                "acceptance_compatibility_signature_sha256"
            ],
            acceptance_dispatch_binding_sha256=identity["acceptance_dispatch_binding_sha256"],
            acceptance_node_material_sha256=identity["acceptance_node_material_sha256"],
        )
    else:
        common.update(
            prior_accepted_generation_id=prior_accepted,
            detector_id=identity["detector_id"],
            detector_version=identity["detector_version"],
            detector_settings_hash=identity["detector_settings_hash"],
            reference_channel_id=reference_channel_id,
        )
    return ref, identity, common


def validate_tissue_candidate_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedTissueCandidateResultArtifactRefProjection:
    """Decode completed candidate-node data without asserting state commitment."""

    _ref, _identity, values = _validate_tissue_artifact_ref(artifact_ref, acceptance=False)
    return _projection(
        ValidatedTissueCandidateResultArtifactRefProjection, **values
    )  # type: ignore[return-value]


def _acceptance_inputs(
    context: BatchNodeExecutionContext,
    candidate_authority: BatchCommittedArtifactAuthority,
    current_execution: BatchCurrentExecutionAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueCandidateResultProjection:
    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("tissue acceptance requires an exact execution context")
    try:
        current = validate_current_execution_authority(current_execution, expected_context=context)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue acceptance requires current execution authority") from exc
    if type(candidate_authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("tissue acceptance requires exact committed candidate authority")
    candidate = decode_committed_tissue_candidate_result(candidate_authority)
    setup = decode_committed_tissue_setup_result(tissue_setup_authority)
    validate_committed_artifact_authority_set(
        (candidate_authority, tissue_setup_authority),
        expected_active_plan_sha256=current.active_plan_sha256,
    )
    if len(context.inputs) != 3:
        raise BatchArtifactError(
            "tissue acceptance requires exact frame and candidate prerequisites"
        )
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if set(by_kind) != {"analysis_frame", "tissue_candidate_result", "tissue_setup_result"}:
        raise BatchArtifactError("tissue acceptance prerequisite kinds are invalid")
    frame_ref = by_kind["analysis_frame"]
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("tissue acceptance frame authority is invalid") from exc
    requirements = {item.artifact_kind: item for item in context.adapter_spec.required_inputs}
    if (
        by_kind["tissue_candidate_result"] != candidate.artifact_ref
        or by_kind["tissue_setup_result"] != setup.artifact_ref
        or setup.state_head_sha256 != current.state_head_sha256
        or setup.artifact_ref.artifact_ref_sha256 != candidate.tissue_setup_result_ref_sha256
        or setup.tissue_setup_result_sha256 != candidate.tissue_setup_result_sha256
        or setup.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or setup.artifact_ref.configuration_bindings != frame_ref.configuration_bindings
        or setup.sample_id != candidate.sample_id
        or setup.slide_id != candidate.slide_id
        or setup.project_entry_id != candidate.project_entry_id
        or setup.validation_domain_id != frame_ref.scope.validation_domain_id
        or setup.canonical_image_id != candidate.canonical_image_id
        or setup.canonical_image_sha256 != candidate.canonical_image_sha256
        or setup.tissue_detection.settings_hash != candidate.detector_settings_hash
        or setup.project_identity_sha256
        != candidate.artifact_ref.owning_identity["project_identity_sha256"]
        or setup.project_entry_projection_sha256
        != candidate.artifact_ref.owning_identity["project_entry_projection_sha256"]
        or current.state_head_sha256 != candidate_authority.state_head_sha256
        or current.active_plan_sha256 != candidate_authority.active_plan_sha256
        or current.node_kind != "tissue_acceptance"
        or current.node_scope != "sample"
        or current.validation_domain_id != frame_ref.scope.validation_domain_id
        or current.participant_ids != (candidate.sample_id,)
        or current.project_entry_ids != (candidate.project_entry_id,)
        or current.analysis_domain_bindings
        or current.configuration_bindings != candidate.artifact_ref.configuration_bindings
        or context.adapter_spec.node_kind != "tissue_acceptance"
        or context.adapter_spec.dispatch_mode.value != "single"
        or context.adapter_spec.resource_kind != "scientific_compute"
        or tuple(context.adapter_spec.declared_outputs)
        != (tissue_acceptance_result_identity_contract().artifact_kind,)
        or context.adapter_spec.output_identity_contracts
        != (tissue_acceptance_result_identity_contract(),)
        or context.adapter_spec.optional_inputs
        or set(requirements) != {"analysis_frame", "tissue_candidate_result", "tissue_setup_result"}
        or any(requirement.multiplicity.value != "single" for requirement in requirements.values())
        or artifact_ref_set_sha256(context.inputs)
        != context.authorization.input_artifact_set_sha256
        or frame_ref.scope.sample_id != candidate.sample_id
        or frame_ref.scope.slide_id != candidate.slide_id
        or frame_ref.scope.project_entry_id is not None
        or frame_ref.artifact_ref_sha256 != candidate.analysis_frame_ref_sha256
        or candidate.artifact_ref.scope.validation_domain_id != frame_ref.scope.validation_domain_id
        or candidate.artifact_ref.configuration_bindings != frame_ref.configuration_bindings
        or _plain(candidate.artifact_ref.owning_identity["canonical_channels"])
        != sorted(
            (item.to_mapping() for item in frame.channels),
            key=lambda item: item["channel_id"],
        )
        or dict(candidate.artifact_ref.runtime_versions) != dict(frame_ref.runtime_versions)
        or any(
            value is not None
            for value in (
                context.current_image_identity_sha256,
                context.script_sha256,
                context.project_identity_sha256,
                context.project_authority_head_sha256,
                context.project_qpproj_snapshot_sha256,
                context.project_entry_projection_sha256,
            )
        )
    ):
        raise BatchArtifactError(
            "tissue acceptance requires the exact current candidate prerequisite"
        )
    return candidate


def build_tissue_acceptance_result_execution_result(
    context: BatchNodeExecutionContext,
    candidate_authority: BatchCommittedArtifactAuthority,
    *,
    current_execution: BatchCurrentExecutionAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
) -> BatchNodeExecutionResult:
    """Project an accepted candidate through one current direct transition."""

    contract = tissue_acceptance_result_identity_contract()
    candidate = _acceptance_inputs(
        context, candidate_authority, current_execution, tissue_setup_authority
    )
    identity = dict(candidate.artifact_ref.owning_identity)
    identity.update(
        {
            "authoritative_accepted_generation_id": candidate.generation_id,
            "prior_candidate_result_ref_sha256": (candidate.artifact_ref.artifact_ref_sha256),
            "prior_candidate_state_head_sha256": candidate.state_head_sha256,
            "prior_candidate_active_plan_sha256": candidate.active_plan_sha256,
            "prior_candidate_terminal_record_sha256": (candidate.terminal_record_sha256),
            "prior_candidate_execution_exchange_sha256": (candidate.execution_exchange_sha256),
            "prior_candidate_saved_state_after_sha256": (candidate.saved_state_after_sha256),
            "prior_candidate_generation_content_sha256": (candidate.generation_content_sha256),
            "prior_candidate_section_inventory_sha256": (candidate.section_inventory_sha256),
            "acceptance_node_id": context.node_id,
            "acceptance_attempt_id": context.authorization.attempt_id,
            "acceptance_attempt_authorization_sha256": (context.authorization.authorization_sha256),
            "acceptance_execution_context_sha256": (current_execution.execution_context_sha256),
            "acceptance_state_head_sha256": current_execution.state_head_sha256,
            "acceptance_active_plan_sha256": current_execution.active_plan_sha256,
            "acceptance_input_artifact_set_sha256": (
                context.authorization.input_artifact_set_sha256
            ),
            "acceptance_projection_target_sha256": (context.projection_target_sha256),
            "acceptance_compatibility_signature_sha256": (context.compatibility_signature_sha256),
            "acceptance_dispatch_binding_sha256": (
                context.authorization.dispatch_binding.dispatch_binding_sha256
            ),
            "acceptance_node_material_sha256": (context.authorization.node_material_sha256),
        }
    )
    identity["tissue_scientific_identity_sha256"] = _identity_digest(
        identity, _ACCEPTANCE_SCIENTIFIC_DIGEST_FIELDS
    )
    identity["task10_execution_identity_sha256"] = _identity_digest(identity, _TASK10_DIGEST_FIELDS)
    artifact = BatchArtifactRef(
        artifact_id="tissue-acceptance-"
        + _digest(
            {
                "candidate": candidate.artifact_ref.artifact_ref_sha256,
                "acceptance_attempt": context.authorization.authorization_sha256,
            }
        )[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=candidate.artifact_ref.scope,
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions=dict(candidate.artifact_ref.runtime_versions),
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=candidate.artifact_ref.configuration_bindings,
    )
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def validate_tissue_acceptance_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedTissueAcceptanceResultArtifactRefProjection:
    """Decode accepted owner data without asserting state commitment."""

    _ref, _identity, values = _validate_tissue_artifact_ref(artifact_ref, acceptance=True)
    return _projection(
        ValidatedTissueAcceptanceResultArtifactRefProjection, **values
    )  # type: ignore[return-value]


def _decode_committed(
    authority: BatchCommittedArtifactAuthority,
    *,
    node_kind: str,
    validator: object,
    projection_type: type[object],
) -> object:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("tissue result decoding requires committed state authority")
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    if authority.producer_node_kind != node_kind:
        raise BatchArtifactError("committed tissue producer kind is invalid")
    ref_projection = validator(authority.artifact_ref)
    expected_exchange = (
        None if node_kind == "tissue_acceptance" else ref_projection.execution_exchange_sha256
    )
    if authority.execution_exchange_sha256 != expected_exchange:
        raise BatchArtifactError("committed attempt exchange differs from tissue identity")
    values = {
        name: getattr(ref_projection, name) for name in type(ref_projection).__dataclass_fields__
    }
    values.update(
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )
    return _projection(projection_type, **values)


def decode_committed_tissue_candidate_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueCandidateResultProjection:
    """Decode one completed candidate-node result from state authority."""

    return _decode_committed(
        authority,
        node_kind="tissue_candidate",
        validator=validate_tissue_candidate_result_artifact_ref,
        projection_type=ValidatedTissueCandidateResultProjection,
    )  # type: ignore[return-value]


def decode_committed_tissue_acceptance_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueAcceptanceResultProjection:
    """Decode one accepted result only from state-owner-issued authority."""

    return _decode_committed(
        authority,
        node_kind="tissue_acceptance",
        validator=validate_tissue_acceptance_result_artifact_ref,
        projection_type=ValidatedTissueAcceptanceResultProjection,
    )  # type: ignore[return-value]


__all__ = [
    "AcceptedTissueSectionLiveAuthority",
    "TISSUE_ACCEPTANCE_RESULT_IDENTITY_FIELDS",
    "TISSUE_CANDIDATE_RESULT_IDENTITY_FIELDS",
    "ValidatedAcceptedTissueLiveAuthority",
    "ValidatedTissueAcceptanceResultArtifactRefProjection",
    "ValidatedTissueAcceptanceResultProjection",
    "ValidatedTissueCandidateResultArtifactRefProjection",
    "ValidatedTissueCandidateResultProjection",
    "build_tissue_acceptance_result_execution_result",
    "build_tissue_candidate_result_execution_result",
    "decode_committed_tissue_acceptance_result",
    "decode_committed_tissue_candidate_result",
    "reopen_committed_accepted_tissue_live_authority",
    "tissue_acceptance_result_identity_contract",
    "tissue_candidate_result_identity_contract",
    "validate_tissue_acceptance_result_artifact_ref",
    "validate_tissue_candidate_result_artifact_ref",
]
