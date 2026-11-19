"""Presentation-neutral facade for the spectral-reference workflow service.

The `ReferenceServiceView` record carries `run_id`, `status`, `target_id`,
`checkpoint_token`, `revision` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component

import hashlib
import math
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Mapping

import numpy as np
import scipy
from ome_types import OME

from mif_pipeline.io import (
    CanonicalIngestionError,
    InspectedSourceAuthority,
    resolve_source_image_facts,
    snapshot_opened_source_identity,
)

from .compatibility import BasisSourceChoice, resolve_channel_fluor_identities
from .compatibility import (
    compare_quantitative_transformability,
    compare_structural_compatibility,
)
from .canonical_reference_input import (
    CanonicalReferenceInput,
    CanonicalReferenceInputError,
    canonical_reference_input_authority_from_opened_source,
    canonical_reference_input_authority_from_reviewed_source,
    canonical_reference_input_from_opened_source,
    canonical_target_reference_input_from_reviewed_source,
)
from .interactions import (
    ArtifactPointer,
    ReferenceActionCode,
    ReferenceResponse,
    SelectedIdentifier,
    TargetControlCompatibilityEvidence,
    ValidatedArtifactEvidence,
    PublishedArtifactEvidence,
    configured_response,
    validate_reference_response,
)
from .background import (
    BackgroundEquivalencePolicy,
    BackgroundModelResolution,
    SampledReferenceRoi,
    resolve_background_model,
)
from .purification import (
    AfControlSetEvidence, AfControlSetMember, AfControlSignalError, purify_reference_control_set,
    ConsensusPolicy,
    ConsensusInclusionDecision,
    FluorophoreConsensusCandidate,
    PurifiedReferenceRoi,
    PurificationPolicy,
    build_fluorophore_consensus,
    purify_reference_roi,
)
from .reference_review_bindings import (
    NegativeFallbackReviewPolicy,
    bind_matched_control_review,
    bind_negative_fallback_review,
    bind_signal_dark_measurement_review,
    extraction_request_evidence,
    matched_control_review_request_evidence,
    negative_fallback_review_request_evidence,
    propose_matched_control_review,
    propose_negative_fallback_review,
)
from .reference_rois import (
    AcceptedReferenceRoiSelection,
    ReferenceRoiExport,
    select_accepted_reference_roi,
)
from .reference_sampling import (
    CompleteVectorQcPolicy,
    MeasurementPlane,
    ProcessingChunkShape,
    TissueMaskBinding,
    TargetSamplingPolicy,
    sample_target_vectors,
    validate_tissue_mask_binding_authority,
)
from .reference_service_roi_purification import (
    ReferenceBackgroundOperation,
    ReferenceRoiPairingSelection,
    ReferenceRoiSelectionPlan,
    ReferenceSignalPurificationOperation,
    sample_reference_roi_selection_plan,
)
from .reference_service_target_af import (
    TargetAfRoiReviewSelection,
    TargetAfSampleBundle,
    TargetAfSamplingPlan,
    TargetAfSharedRoiAuthority,
    TargetControlReviewResponse,
    sample_target_af_plan,
)
from .reference_models import FluorophoreReferenceLibrary, ReferenceInputEvidence
from .reference_catalogue import SharedReferenceCatalogue
from .reference_models import VersionRecord
from .reference_qc import SpectrumMatrixQcPolicy
from .reference_service_composition import ReferenceLibraryPublicationMetadata
from .reference_service_result_operations import (
    _ReferenceResultOperations,
)
from .reference_service_source_choice import _ReferenceSourceChoiceOperations
from .reference_service_records import (
    ReferenceServiceAdvance,
    ReferenceServiceAdvanceStatus,
    ReferenceServiceSnapshot,
)
from .reference_service_storage import (
    ReferenceCheckpointConflictError,
    ReferenceCheckpointStore,
)
from .reference_service_artifacts import ReferenceArtifactRepositoryError
from .reference_view_records import ReferenceReviewData
from .reference_view_builders import (
    purification_clipping_metrics, build_run_basis_review_data, build_target_af_review_data,
)
from .application import ResolvedBasisApplication
from .final_basis import (
    FinalRunBasisArtifact,
    FinalRunBasisProposal,
    FinalRunBasisRuntimeHandoff,
    accept_final_run_basis,
    propose_control_final_run_basis,
    propose_fluorophore_final_run_basis,
    propose_motif_final_run_basis,
)
from .target_af import (
    FixedBasisQcPolicy,
    ProposalReviewDecision,
    ReviewedTargetAfRoiSelection,
    TargetAfEstimationPolicy,
    TargetAfRoiProposal,
    TargetAutofluorescenceArtifact,
    TargetAutofluorescenceProposal,
    build_target_autofluorescence_model,
    estimate_target_af_candidates,
    evaluate_target_af_qc,
    preview_control_run_basis,
    propose_target_autofluorescence_model,
    validate_target_control_binding,
)
from .typed_checksum import typed_checksum_sha256
from .reference_workflow import (
    ReferenceDecisionRecord,
    ReferenceWorkflowCheckpoint,
    ReferenceWorkflowStatus,
    advance_reference_workflow,
    bind_target_control_compatibility,
    propose_reference_artifacts,
    propose_reference_evidence,
    publish_reference_artifacts,
    start_reference_workflow,
)

_FINAL_APPLICATION_SERVICE_AUTHORITY = object()


def _multiple_component_cycles(names):
    from mif_pipeline.io.metadata.cycles import split_cycle_component
    return len({cycle for name in names if (cycle := split_cycle_component(name)[1]) is not None}) > 1


def _reference_region_label(sample, role="Reference"):
    """Use the same per-image, per-role numbering as the saved native inventory."""
    selection = sample.selection
    peers = tuple(roi for roi in selection.export.rois
                  if roi.project_entry_id == selection.roi.project_entry_id
                  and roi.roi_role == selection.roi.roi_role)
    ordinal = next(index + 1 for index, roi in enumerate(peers)
                   if roi.roi_id == selection.roi.roi_id)
    return f"{role} region {ordinal}"


def _reference_wavelength_fields(axis):
    measurements = axis.measurements
    fields = ()
    if measurements and any(item.wavelength is not None for item in measurements):
        fields = (("measurement_wavelengths", tuple(item.wavelength for item in measurements)),
                ("wavelength_unit", next(item.wavelength_unit for item in measurements if item.wavelength is not None)))
    if any(item.optical_setting is not None for item in measurements):
        fields += (("measurement_optical_settings", tuple(
            (("setting_id", item.optical_setting.setting_id),
             ("label", item.optical_setting.label),
             ("provenance", item.optical_setting.provenance))
            if item.optical_setting is not None else None for item in measurements)),)
    return fields


def _require_single_cycle_reference_route(names):
    if _multiple_component_cycles(names):
        raise ValueError(
            "This single-cycle reference source cannot be applied directly to a cyclic image. "
            "Use shared reference-image construction or import a compatible fluorophore library or spectral table."
        )

_BACKGROUND_POLICY_VERSION = "background-v1"
_PURIFICATION_POLICY_VERSION = "purification-v1"
_NEGATIVE_FALLBACK_POLICY_VERSION = "negative-v1"
_CONSENSUS_POLICY_VERSION = "consensus-v1"
_MATRIX_QC_POLICY_VERSION = "matrix-qc-v1"
_TARGET_AF_ESTIMATION_POLICY_VERSION = "target-af-estimation-v1"
_TARGET_AF_QC_POLICY_VERSION = "target-af-qc-v1"
_FINAL_BASIS_QC_POLICY_VERSION = "fixed-basis-qc-v1"
_TARGET_CONTROL_REVIEW_POLICY_ID = "control-review"
_TARGET_CONTROL_REVIEW_POLICY_VERSION = "1"

# ==============================================================================
#                               Reference service
# ==============================================================================


def _derived_service_timestamp(authority: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{authority}:{purpose}".encode("utf-8")).digest()
    seconds = int.from_bytes(digest[:4], "big") % (100 * 365 * 24 * 60 * 60)
    value = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _fixed_basis_qc_policy_from_mapping(
    value: Mapping[str, object], *, purpose: str, policy_version: str
) -> FixedBasisQcPolicy:
    policy_fields = {
        "svd_absolute_tolerance",
        "svd_relative_tolerance",
        "cosine_review_threshold",
        "cosine_fail_threshold",
        "condition_review_threshold",
        "condition_fail_threshold",
    }
    if type(value) is not dict or set(value) != policy_fields:
        raise ValueError("fixed-basis QC policy fields differ")
    authority = typed_checksum_sha256(
        {
            "kind": purpose,
            "policy_version": policy_version,
            "configuration": value,
        }
    )
    return FixedBasisQcPolicy(
        **value,
        policy_version=policy_version,
        authority_sha256=authority,
    )


def _strict_record_mapping(value: object, record_type: type, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    expected = {item.name for item in fields(record_type) if item.init}
    if set(value) != expected:
        raise ValueError(f"{label} fields differ")
    return dict(value)


def _complete_vector_policy_from_mapping(value: object) -> CompleteVectorQcPolicy:
    mapping = _strict_record_mapping(value, CompleteVectorQcPolicy, "complete-vector policy")
    shape = mapping["processing_chunk_shape"]
    shape_value = _strict_record_mapping(
        shape, ProcessingChunkShape, "complete-vector processing chunk shape"
    )
    mapping["processing_chunk_shape"] = ProcessingChunkShape(**shape_value)
    return CompleteVectorQcPolicy(**mapping)


def _purification_sampling_policy_from_mapping(
    value: object,
) -> CompleteVectorQcPolicy:
    expected = {"processing_chunk_shape", "saturation_fraction", "near_zero_l2_threshold"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("complete-vector policy fields differ")
    shape = value["processing_chunk_shape"]
    if type(shape) is not list or len(shape) != 2 or any(type(item) is not int for item in shape):
        raise ValueError("processing chunk shape must be [width, height]")
    return CompleteVectorQcPolicy(
        processing_chunk_shape=ProcessingChunkShape(shape[0], shape[1]),
        saturation_fraction=value["saturation_fraction"],
        near_zero_l2_threshold=value["near_zero_l2_threshold"],
    )


def _background_policy_from_mapping(value: object) -> BackgroundEquivalencePolicy:
    expected = {
        "per_band_absolute_tolerance",
        "per_band_relative_tolerance",
        "l2_absolute_tolerance",
        "l2_relative_tolerance",
        "scale_floor",
        "maximum_relative_roi_heterogeneity",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("background policy fields differ")
    return BackgroundEquivalencePolicy(policy_version=_BACKGROUND_POLICY_VERSION, **value)


def _purification_policy_from_mapping(value: object) -> PurificationPolicy:
    expected = {
        "denominator_tolerance",
        "af_minimum_l2_norm",
        "scale_floor",
        "purified_minimum_l2_norm",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("purification policy fields differ")
    return PurificationPolicy(policy_version=_PURIFICATION_POLICY_VERSION, **value)


def _control_target_af_policies_from_mappings(
    *, sampling: object, background: object, estimation: object, qc: object
) -> tuple[
    CompleteVectorQcPolicy,
    BackgroundEquivalencePolicy,
    TargetAfEstimationPolicy,
    FixedBasisQcPolicy,
]:
    estimation_fields = {
        item.name
        for item in fields(TargetAfEstimationPolicy)
        if item.init and item.name not in {"authority_sha256", "policy_version"}
    }
    if type(estimation) is not dict or set(estimation) != estimation_fields:
        raise ValueError("target-AF estimation policy fields differ")
    estimation_value = dict(estimation)
    estimation_value["authority_sha256"] = typed_checksum_sha256(
        {
            "kind": "reference-unmixing-target-af-estimation-policy/v1",
            "policy_version": _TARGET_AF_ESTIMATION_POLICY_VERSION,
            "configuration": estimation_value,
        }
    )
    estimation_value["policy_version"] = _TARGET_AF_ESTIMATION_POLICY_VERSION
    return (
        _complete_vector_policy_from_mapping(sampling),
        _background_policy_from_mapping(background),
        TargetAfEstimationPolicy(**estimation_value),
        _fixed_basis_qc_policy_from_mapping(
            qc,
            purpose="reference-unmixing-target-af-qc-policy/v1",
            policy_version=_TARGET_AF_QC_POLICY_VERSION,
        ),
    )


_SAFE_PARAMETER_FREE_OPTIONS = {
    "revise_shared_control", "retry_shared_control_preparation",
    "reselect_basis_source",
    "reselect_target_af_source",
    "reselect_reference_rois",
    "revise_signal_dark_measurements",
    "construct_reference_library",
    "shared_unstained_control",
    "per_stain_negative_controls",
}


@dataclass(frozen=True, slots=True)
class ReferenceServiceArtifactRequirement:
    """Bounded artifact role/subject requirement without a pointer or authority."""

    role: str
    subject_id: str | None

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role.strip():
            raise ValueError("service artifact requirement role must be non-empty text")
        if self.subject_id is not None and (
            type(self.subject_id) is not str or not self.subject_id.strip()
        ):
            raise ValueError("service artifact requirement subject must be non-empty text or null")


@dataclass(frozen=True, slots=True)
class ReferenceServiceOptionRequirement:
    """Bounded display and selection requirements for one live option."""

    option_value: str
    label: str
    description: str
    minimum_selected_ids: int
    minimum_artifacts: int
    artifact_requirements: tuple[ReferenceServiceArtifactRequirement, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.option_value, "option value"),
            (self.label, "option label"),
            (self.description, "option description"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"service {label} must be non-empty text")
        if (
            type(self.minimum_selected_ids) is not int
            or self.minimum_selected_ids < 0
            or type(self.minimum_artifacts) is not int
            or self.minimum_artifacts < 0
        ):
            raise ValueError("service option minimum counts must be nonnegative integers")
        if type(self.artifact_requirements) is not tuple or any(
            type(item) is not ReferenceServiceArtifactRequirement
            for item in self.artifact_requirements
        ):
            raise TypeError("service option artifact requirements are invalid")


@dataclass(frozen=True, slots=True)
class ReferenceServiceView:
    """Persisted-only bounded view; never carries workflow or authority records."""

    run_id: str
    status: str
    target_id: str
    checkpoint_token: str
    revision: int
    checkpoint: str | None
    fluorophore: str | None
    measurement_ids: tuple[str, ...]
    offered_actions: tuple[str, ...]
    offered_options: tuple[ReferenceServiceOptionRequirement, ...]
    proposed_artifact_requirements: tuple[ReferenceServiceArtifactRequirement, ...]
    offered_ids: tuple[str, ...]
    review_summary: tuple[tuple[str, object], ...]


class ReferenceWorkflowService:
    """Own persistence while hiding caller-authored workflow authority fields."""

    def __init__(self, store: ReferenceCheckpointStore, *, backend=None) -> None:
        if type(store) is not ReferenceCheckpointStore:
            raise TypeError("reference workflow service requires its checkpoint store")
        self._store = store
        self._result_operations = _ReferenceResultOperations(store)
        # Kept as a private compatibility seam for focused repository tests.
        self._artifacts = self._result_operations._artifacts
        self._source_choice_operations = _ReferenceSourceChoiceOperations(store, self._artifacts)
        self._backend = backend

    def _sampling_backend(self):
        if self._backend is None:
            from mif_pipeline.io import get_default_backend

            return get_default_backend()
        return self._backend

    def _bounded_view(
        self,
        checkpoint: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
    ) -> ReferenceServiceView:
        request = checkpoint.request
        # Stored requests keep their original bytes; retired actions are no
        # longer offered by the live operation.
        failed_extraction = request is not None and any(
            item.kind == "reference_extraction_failure" for item in request.evidence
        )
        spectral_summary = self._spectral_review_summary(checkpoint, start_authority_sha256)
        af_groups_summary = self._target_af_groups_summary(checkpoint, start_authority_sha256)
        failed_combined_review = (
            request is not None and request.checkpoint.value == "target_af_control_review"
            and dict(dict(spectral_summary).get("spectral_review", ())).get("quality") == "failed"
        )
        options = (() if request is None else tuple(
            option for option in request.options
            if option.value not in {"retry_final_run_basis", "shared_cycle_reference_library"}
            and not (spectral_summary and request.checkpoint.value == "target_af_control_review"
                     and option.value == "retry_control_target_af")
            and not (failed_extraction and option.value in {"accept_extraction_qc", "retry_extraction"})
            and not (failed_combined_review and option.value == "accept_control_target_af")
            and not (option.value == "review_reference_control_groups" and not af_groups_summary)
            and not (option.value == "reuse_reference_unstained_control" and af_groups_summary)
            and not (request.checkpoint.value == "basis_source"
                     and _multiple_component_cycles(checkpoint.fluorophores)
                     and option.value not in {"curated_reference_library", "construct_reference_library", "abort"})
        ))
        offered_actions = tuple(option.value for option in options)
        offered_options = (
            ()
            if request is None
            else tuple(
                ReferenceServiceOptionRequirement(
                    option_value=option.value,
                    label=option.label,
                    description=option.description,
                    minimum_selected_ids=option.minimum_selected_ids,
                    minimum_artifacts=option.minimum_artifacts,
                    artifact_requirements=tuple(
                        ReferenceServiceArtifactRequirement(
                            role=item.role,
                            subject_id=item.subject_id,
                        )
                        for item in option.artifact_requirements
                    ),
                )
                for option in options
            )
        )
        proposed_artifact_requirements = (
            ()
            if request is None
            else tuple(
                ReferenceServiceArtifactRequirement(
                    role=item.pointer.role,
                    subject_id=item.pointer.subject_id,
                )
                for item in request.proposed_artifacts
            )
        )
        offered_ids = (
            ()
            if request is None
            else tuple(
                dict.fromkeys(
                    item for evidence in request.evidence for item in evidence.suggested_ids
                )
            )
        )
        if af_groups_summary:
            inventory = dict(af_groups_summary)["target_af_groups"]
            offered_ids = tuple(row["input_id"] for row in inventory["sources"]) + tuple(
                row["roi_id"] for row in inventory["regions"])
        saved_positive_regions = ()
        if (
            request is not None
            and request.checkpoint.value in {"af_reference_rois", "negative_reference_rois", "background_reference_rois"}
            and checkpoint.current_fluorophore is not None
        ):
            positive_plan = self._live_roi_phase_artifact(
                checkpoint,
                start_authority_sha256,
                "roi_selection_plan",
                checkpoint.current_fluorophore,
                "positive_reference_rois",
            ).record
            if request.checkpoint.value != "background_reference_rois":
                offered_ids = tuple(item.roi_id for item in positive_plan.selections)
            saved_positive_regions = (("saved_positive_regions", self._saved_positive_region_choices(checkpoint, start_authority_sha256, plan=positive_plan)),)
        review_summary = (
            ("portable_library_available", checkpoint.accepted_library is not None),
            ("control_strategy", checkpoint.control_strategy),
            ("reason", None if request is None else request.reason.message),
            (
                "evidence",
                (
                    ()
                    if request is None
                    else tuple(
                        (
                            ("evidence_id", evidence.evidence_id),
                            ("kind", evidence.kind),
                            (
                                "summary",
                                tuple(
                                    (("name", field.name), ("value", field.value))
                                    for field in evidence.summary
                                ),
                            ),
                            ("suggested_ids", evidence.suggested_ids),
                            ("ambiguous", evidence.ambiguous),
                        )
                        for evidence in request.evidence
                    )
                ),
            ),
        )
        af_control_summary = self._af_control_set_review_summary(checkpoint, start_authority_sha256)
        return ReferenceServiceView(
            run_id=checkpoint.run_id,
            status=checkpoint.status.value,
            target_id=checkpoint.target_image_sha256,
            checkpoint_token=checkpoint.content_sha256,
            revision=checkpoint.revision,
            checkpoint=(
                None
                if checkpoint.current_checkpoint is None
                else checkpoint.current_checkpoint.value
            ),
            fluorophore=checkpoint.current_fluorophore,
            measurement_ids=checkpoint.measurement_ids,
            offered_actions=offered_actions,
            offered_options=offered_options,
            proposed_artifact_requirements=proposed_artifact_requirements,
            offered_ids=offered_ids,
            review_summary=review_summary + saved_positive_regions
            + self._consensus_review_summary(checkpoint, start_authority_sha256)
            + self._extraction_review_summary(checkpoint, start_authority_sha256)
            + af_control_summary
            + af_groups_summary
            + self._target_af_review_summary(checkpoint, start_authority_sha256)
            + spectral_summary
            + self._curated_input_summary(checkpoint, start_authority_sha256)
            + self._measurement_choice_summary(checkpoint, start_authority_sha256, af_control_summary)
            + self._reference_file_choice_summary(checkpoint, start_authority_sha256)
            + self._reference_component_progress_summary(checkpoint)
            + self._reference_preparation_summary(checkpoint, start_authority_sha256),
        )

    @staticmethod
    def _reference_component_progress_summary(checkpoint):
        """Project the accepted/current inventory without loading scientific records."""
        if checkpoint.basis_source != "construct_reference_library":
            return ()
        accepted = set(checkpoint.accepted_fluorophores)
        return (("reference_component_progress", {
            "components": [{"name": name, "status": "accepted" if name in accepted
                else "current" if name == checkpoint.current_fluorophore else "not_started"}
                for name in checkpoint.fluorophores],
            "current_component": checkpoint.current_fluorophore,
            "current_checkpoint": (None if checkpoint.current_checkpoint is None
                                   else checkpoint.current_checkpoint.value),
        }),)

    def _target_af_groups_summary(self, checkpoint, start_authority_sha256):
        if checkpoint.request is None or checkpoint.request.checkpoint.value != "target_af":
            return ()
        from .reference_service_af_groups import target_af_groups_inventory
        inventory, _, _ = target_af_groups_inventory(self, checkpoint, start_authority_sha256)
        return () if inventory is None else (("target_af_groups", inventory),)

    def _reference_preparation_summary(self, checkpoint, start):
        from .reference_service_control_preparation import preparation_summary
        return preparation_summary(self, checkpoint, start)

    def propose_target_af_groups_from_ids(self, run_id, expected_content_sha256, *, target_af_groups, reviewer_id):
        from .reference_service_af_groups import propose_target_af_groups
        return propose_target_af_groups(self, run_id, expected_content_sha256,
            selection=target_af_groups, reviewer_id=reviewer_id)

    def _saved_positive_region_choices(self, checkpoint, start_authority_sha256, *, plan=None):
        """Read accepted positive choices without a historical response authority.

        The bounded view passes its already decoded plan; explicit region Load
        uses the same current-checkpoint lookup and projection.
        """
        if plan is None:
            plan = self._live_roi_phase_artifact(checkpoint, start_authority_sha256,
                "roi_selection_plan", checkpoint.current_fluorophore, "positive_reference_rois").record
        entry_ids = {item.roi.project_entry_id for item in plan.selections}
        entries = {item.project_entry_id: item for item in plan.export.entries}
        choices = tuple(roi for roi in plan.export.rois
                        if roi.roi_role == "positive" and roi.review_state == "accepted"
                        and roi.project_entry_id in entry_ids)
        policy = plan.sampling_policy
        return (
            ("export_sha256", plan.export.content_sha256),
            ("selected_roi_ids", tuple(item.roi_id for item in plan.selections)),
            ("roi_choices", tuple((("roi_id", roi.roi_id),
                                    ("label", f"{entries[roi.project_entry_id].image.source_path.name} — Positive region {index + 1}"))
                                   for index, roi in enumerate(choices))),
            ("sampling_values", (policy.processing_chunk_shape.width,
                                 policy.processing_chunk_shape.height,
                                 policy.saturation_fraction, policy.near_zero_l2_threshold)),
        )

    def _reference_file_choice_summary(self, checkpoint, start):
        """Expose saved filenames and declared roles for ordered control selection.

        Read the retained canonical inventory only at file selection. Source
        pixels and native metadata are not reopened for these display choices.
        """
        if checkpoint.current_checkpoint is None or checkpoint.current_checkpoint.value != "reference_control_files":
            return ()
        from pathlib import Path
        inventory = self._artifacts._load_all_canonical_inputs(
            run_id=checkpoint.run_id, start_authority_sha256=start)
        rows = tuple((
            ("input_id", item.record.input_id),
            ("filename", Path(item.record.locator_binding.source_locator).name),
            ("role", item.record.input_role),
            ("fluorophore", item.record.evidence.fluorophore_identity),
        ) for item in inventory if item.record.input_role != "target_image")
        return (("reference_file_choices", (
            ("fluorophores", checkpoint.fluorophores), ("inputs", rows),
        )),)

    def _measurement_choice_summary(self, checkpoint, start, af_control_summary=None):
        """Pair offered band IDs with names from the retained target record.

        Read saved canonical authority, without opening a source image or changing
        the measurement IDs accepted by the workflow.
        """
        if checkpoint.request is None or checkpoint.request.checkpoint.value != "signal_dark_measurements":
            return ()
        target = self._require_canonical_target(checkpoint.run_id, start)
        labels = tuple(zip(checkpoint.measurement_ids, target.evidence.axis.parent_band_ids, strict=True))
        fluorophore = checkpoint.current_fluorophore
        from .reference_workflow import _uses_source_preparation
        control_phase = ("negative_reference_rois" if checkpoint.control_strategy == "per_stain_negative_controls"
                         and not _uses_source_preparation(checkpoint.active_decisions) else "af_reference_rois")
        positive = self._live_roi_phase_artifact(checkpoint, start, "sampled_roi_bundle",
                                               fluorophore, "positive_reference_rois").record
        control = self._live_roi_phase_artifact(checkpoint, start, "sampled_roi_bundle",
                                              fluorophore, control_phase).record
        backgrounds = self._live_roi_phase_artifact(checkpoint, start, "background_resolutions",
                                                  fluorophore, "background_reference_rois").record
        positives = {item.selection.roi.roi_id: item for item in positive.samples}
        controls = {item.selection.roi.roi_id: item for item in control.samples}
        regions = []
        if af_control_summary is None:
            af_control_summary = self._af_control_set_review_summary(checkpoint, start)
        review = dict(dict(af_control_summary).get("af_control_set_review", ()))
        control_sets = tuple(dict(row) for row in review.get("sets", ()))
        if control_sets:
            background_by_pair = {(pair.positive_roi_id, pair.control_roi_id): background
                for pair, background in zip(control.plan.pairings, backgrounds, strict=True)}
            for group in control_sets:
                p, c = positives[group["positive_roi_id"]], controls[group["roi_ids"][0]]
                background = background_by_pair[(group["positive_roi_id"], group["roi_ids"][0])]
                regions.append((("roi_id", p.selection.roi.roi_id), ("label", _reference_region_label(p)),
                    ("positive_filename", p.selection.entry.image.source_path.name),
                    ("control_filename", c.selection.entry.image.source_path.name),
                    ("control_label", (_reference_region_label(c, "Control") if review.get("review_kind") == "matched_negative"
                                       else f"AF control set ({len(group['roi_ids'])} regions)")),
                    ("control_roi_ids", group["roi_ids"]), ("control_set_sha256", group["evidence_sha256"]),
                    ("positive_values", tuple(max(float(v-b), 0.) for v,b in zip(
                        p.sampling.mean_vector, background.model.stained_background.values, strict=True))),
                    ("control_values", group["corrected_values"])))
        else:
            for pair, background in zip(control.plan.pairings, backgrounds, strict=True):
                p, c = positives[pair.positive_roi_id], controls[pair.control_roi_id]
                if background.model is None:
                    raise ReferenceCheckpointConflictError("Reviewed image background is unavailable.")
                regions.append((("roi_id", pair.positive_roi_id), ("label", _reference_region_label(p)),
                    ("positive_filename", p.selection.entry.image.source_path.name),
                    ("control_filename", c.selection.entry.image.source_path.name),
                    ("control_label", _reference_region_label(c, "Control")),
                    ("positive_values", tuple(max(float(v - b), 0.) for v, b in zip(
                        p.sampling.mean_vector, background.model.stained_background.values, strict=True))),
                    ("control_values", tuple(max(float(v - b), 0.) for v, b in zip(
                        c.sampling.mean_vector, background.model.control_background.values, strict=True)))))
        previous = next((decision for decision in reversed(checkpoint.decisions)
            if decision.request.fluorophore == fluorophore
            and decision.response.option_value == "accept_signal_dark_measurements"), None)
        selections = () if previous is None else previous.response.selections
        policy = None if previous is None else self._historical_purification_policy(
            checkpoint, start, previous)
        return (("measurement_labels", labels), ("band_review", (
            ("component", fluorophore), ("measurement_ids", checkpoint.measurement_ids),
            ("measurement_names", target.evidence.axis.parent_band_ids),
            *_reference_wavelength_fields(target.evidence.axis),
            ("signal_measurement_id", next((item.identifier for item in selections if item.role == "signal_measurement"), None)),
            ("dark_measurement_ids", tuple(item.identifier for item in selections if item.role == "dark_measurement")),
            ("purification_policy", policy),
            ("correction_mode", positive.samples[0].input_evidence.quantitative_signature.measurements[0].correction_mode),
            ("regions", tuple(regions)),
        )))

    def _historical_purification_policy(self, checkpoint, start, decision):
        """Recover settings only; superseded extractions remain scientifically stale."""
        source, result, loaded_start = self._store._load_service_decision_authority(
            checkpoint.run_id, source_checkpoint_sha256=decision.source_checkpoint_sha256,
            decision_record_sha256=decision.content_sha256)
        if (loaded_start != start or source.request != decision.request
                or source.content_sha256 != decision.source_checkpoint_sha256
                or not result.decisions or result.decisions[-1] != decision
                or source.current_checkpoint.value != "signal_dark_measurements"
                or source.current_fluorophore != checkpoint.current_fluorophore
                or decision.request.fluorophore != checkpoint.current_fluorophore
                or decision.response.option_value != "accept_signal_dark_measurements"):
            raise ReferenceCheckpointConflictError("Saved extraction settings lineage differs")
        operation = self._artifacts.load(run_id=checkpoint.run_id,
            start_authority_sha256=start, authority_checkpoint_sha256=source.content_sha256,
            kind="signal_purification_operation", subject_id=checkpoint.current_fluorophore).record
        if (type(operation) is not ReferenceSignalPurificationOperation
                or operation.source_checkpoint_sha256 != source.content_sha256
                or operation.reviewer_id != decision.response.reviewer_id
                or operation.selections != decision.response.selections):
            raise ReferenceCheckpointConflictError("Saved extraction settings binding differs")
        return tuple((name, getattr(operation.purification_policy, name)) for name in (
            "denominator_tolerance", "af_minimum_l2_norm", "scale_floor", "purified_minimum_l2_norm"))

    def _curated_input_summary(self, checkpoint, start):
        if checkpoint.request is None or checkpoint.request.checkpoint.value != "basis_source":
            return ()
        target = self._require_canonical_target(checkpoint.run_id, start)
        from .cycle_references import shared_cycle_reference_layout
        layout = shared_cycle_reference_layout(target, checkpoint.af_behavior)
        names = tuple(
            dict.fromkeys(v for v in checkpoint.fluorophores if not is_autofluorescence_component(v))
        )
        af = tuple(v for v in target.component_names if is_autofluorescence_component(v)) or ("Sample AF",)
        return (
            (
                "spectral_input",
                (
                    ("band_names", target.evidence.axis.parent_band_ids if layout is None else layout.band_names),
                    ("af_behavior", checkpoint.af_behavior),
                    ("component_names", (names + af if names and checkpoint.af_behavior == "retain" else names)
                     if layout is None else layout.component_names),
                    # Acquisition-convention choices are derived from the
                    # required quantitative evidence, even without a reusable
                    # reference-acquisition compatibility profile.
                    ("correction_mode", target.evidence.quantitative_signature.measurements[0].correction_mode),
                ),
            ),
        )

    def read_supplied_library(
        self, run_id, expected_content_sha256, *, library_locator=None, spectral_csv=None,
        normalization=None, measurement_convention=None,
    ):
        """Read the selected bounded file and validate its existing scientific format.

        Portable packages/JSON keep their recorded axis, normalization, calibration
        and provenance. CSV needs the researcher's explicit declarations and the
        target's recorded band order. Neither route reads source-image pixels.
        """
        from .curated_library import validate_curated_target

        library, csv_context = self._read_supplied_library(
            run_id, expected_content_sha256, library_locator=library_locator, spectral_csv=spectral_csv,
            normalization=normalization, measurement_convention=measurement_convention,
        )
        if type(library) in {FluorophoreReferenceLibrary, SharedReferenceCatalogue}:
            self.validate_basis_source_choice(run_id, expected_content_sha256,
                BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY.value, imported_library=library)
        else:
            current, _start, target = csv_context
            validate_curated_target(library, target, current.af_behavior)
        return library

    def _read_supplied_library(
        self, run_id, expected_content_sha256, *, library_locator=None, spectral_csv=None,
        normalization=None, measurement_convention=None,
    ):
        """Parse one supplied file or inline table using the same scientific reader.

        Typed files retain their recorded correction and normalization. CSV and
        entered values use the correction selected beside the spectral table.
        """
        from pathlib import Path
        from .curated_library import parse_spectral_csv
        from .library_exchange import read_library_payload, load_library_exchange

        if bool(library_locator) == bool(spectral_csv):
            raise ValueError("Import one library file or enter one spectral table.")
        if spectral_csv is not None:
            if type(spectral_csv) is not str or len(spectral_csv.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("Enter a spectral table smaller than 2 MiB.")
            text = spectral_csv
        else:
            path = Path(library_locator)
            payload = read_library_payload(path)
            if path.suffix.casefold() != ".csv":
                if not (path.name.casefold().endswith(".miflib.zip") or path.suffix.casefold() == ".json"):
                    raise ValueError("Choose a .miflib.zip package, reviewed library JSON or spectral CSV.")
                return load_library_exchange(payload), None
            text = payload.decode("utf-8-sig")
        current, start = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 != expected_content_sha256:
            raise ReferenceCheckpointConflictError("The reference form has changed")
        target = self._require_canonical_target(run_id, start)
        from .cycle_references import shared_cycle_reference_layout
        from mif_pipeline.io.metadata.cycles import split_cycle_component
        import csv, io
        # Existing whole-stack CSVs retain their explicitly cycle-qualified dyes.
        # New tables declare each shared fluorophore only once.
        headings = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), ())
        legacy_cycles = any(split_cycle_component(name)[1] is not None
                            for name in headings[1:] if not is_autofluorescence_component(name))
        layout = None if legacy_cycles else shared_cycle_reference_layout(target, current.af_behavior)
        library = parse_spectral_csv(
            text,
            band_names=target.evidence.axis.parent_band_ids if layout is None else layout.band_names,
            component_names=layout.component_names if layout is not None else (tuple(name for name in target.component_names
                if current.af_behavior == "retain" or not is_autofluorescence_component(name))
                if target.component_inventory is not None else None),
            normalization=normalization,
            measurement_convention=measurement_convention,
            allow_cycle_subsets=layout is not None,
        )
        return library, (current, start, target)

    def _component_biomarker_defaults(self, target, columns):
        """Carry the earlier component-to-marker review into the accepted spectra.

        Raw spectral bands retain their original metadata. Their output markers
        come from the independent component table reviewed before unmixing.
        """
        inventory = target.component_inventory
        if inventory is not None and inventory.component_biomarkers is None:
            raise ValueError("Complete the component-to-marker assignments in Channels and markers")
        if inventory is not None and inventory.component_biomarkers is not None:
            assignments = dict(zip(inventory.component_names,
                                   inventory.component_biomarkers, strict=True))
            missing = [column.component_identity for column in columns
                       if column.component_kind == "fluorophore"
                       and column.component_identity not in assignments]
            if missing:
                raise ValueError("Review these components in Channels and markers: "
                                 + ", ".join(missing))
            return tuple(assignments[column.component_identity]
                         if column.component_kind == "fluorophore" else None
                         for column in columns)
        defaults = []
        for column in columns:
            assigned = {
                marker for fluorophore, marker in zip(
                    target.channel_fluors, target.channel_biomarkers)
                if column.component_kind == "fluorophore"
                and fluorophore == column.fluorophore_identity
                and marker is not None
            }
            if len(assigned) > 1:
                raise ValueError("Source marker assignments conflict for an output component")
            defaults.append(next(iter(assigned)) if assigned else None)
        return tuple(defaults)

    def _consensus_review_summary(self, checkpoint, start_authority_sha256):
        """Project saved regional shapes for the current comparison; never reopen pixels."""
        if checkpoint.request is None or checkpoint.request.checkpoint.value != "fluorophore_consensus":
            return ()
        fluorophore = checkpoint.current_fluorophore
        purifications, decisions = self._result_operations._reviewed_extraction_for_consensus(
            checkpoint.run_id, checkpoint, start_authority_sha256, fluorophore)
        regions = tuple((
            ("roi_id", item.positive.selection.roi.roi_id),
            ("label", _reference_region_label(item.positive)),
            ("filename", item.positive.selection.entry.image.source_path.name),
            ("values", item.purified_spectrum), ("included", decision.included),
        ) for item, decision in zip(purifications, decisions, strict=True))
        return (("consensus_review", (("component", fluorophore),
            ("measurement_names", purifications[0].positive.input_evidence.axis.parent_band_ids),
            *_reference_wavelength_fields(purifications[0].positive.input_evidence.axis),
            ("regions", regions))),)

    def _extraction_review_summary(self, checkpoint, start_authority_sha256):
        """Expose stored per-region residuals on their original quantitative scale."""
        request = checkpoint.request
        if request is None or request.checkpoint.value != "extraction_qc":
            return ()
        results = self._live_roi_phase_artifact(
            checkpoint, start_authority_sha256, "purifications",
            checkpoint.current_fluorophore, "signal_dark_measurements",
        ).record
        regions = []
        for result in results:
            clipped_l2, clipped_ratio = purification_clipping_metrics(result)
            axis = result.positive.input_evidence.axis
            mode = result.positive.input_evidence.quantitative_signature.measurements[0].correction_mode
            regions.append((
                ("region", result.positive.selection.roi.roi_id),
                ("label", _reference_region_label(result.positive)),
                ("filename", result.positive.selection.entry.image.source_path.name),
                ("control_filename", result.control.selection.entry.image.source_path.name),
                ("control_label", _reference_region_label(result.control, "Control") if result.control_set is None
                 else f"AF control set ({len(result.control_set.members)} regions)"),
                ("control_roi_ids", (result.control.selection.roi.roi_id,) if result.control_set is None else result.control_set.roi_ids),
                ("disposition", result.disposition),
                ("measurement_names", axis.parent_band_ids),
                *_reference_wavelength_fields(axis),
                ("correction_mode", mode),
                ("signed_residual", result.raw_residual),
                ("clipped_residual", None if result.raw_residual is None else
                 tuple(max(value, 0.0) for value in result.raw_residual)),
                ("clipped_count", len(result.negative_residual_clamps)),
                ("clipped_l2", clipped_l2),
                ("clipped_to_retained_l2_ratio", clipped_ratio),
                ("positive_values", result.background_corrected_positive),
                ("control_values", result.background_corrected_control),
                ("af_subtraction_factor", result.af_subtraction_factor),
                ("signal_measurement_id", result.signal_dark_review.signal_measurement_id),
                ("dark_measurement_ids", tuple(item.measurement_id for item in result.dark_measurements)),
                ("measurement_ids", result.measurement_ids),
            ))
        return (("extraction_review", (("component", checkpoint.current_fluorophore),
                                      ("regions", tuple(regions)))),)

    def _accepted_curated_source(self, checkpoint, start_authority_sha256):
        """Load the numerical source selected by the current active decision."""
        from .curated_library import CuratedSourceDecision
        choices = tuple(item for item in checkpoint.active_decisions
                        if item.response.option_value == "curated_reference_library")
        if len(choices) != 1:
            raise ReferenceCheckpointConflictError("Curated source review is missing or ambiguous")
        stored = self._artifacts.load_all(
            run_id=checkpoint.run_id, start_authority_sha256=start_authority_sha256,
            kind="curated_source", subject_id="curated:" + choices[0].source_checkpoint_sha256)
        if len(stored) != 1 or type(stored[0].record) is not CuratedSourceDecision:
            raise ReferenceCheckpointConflictError("The reviewed numerical spectra are unavailable")
        return stored[0].record

    def _saved_spectral_source(self, checkpoint, start, target):
        """Display accepted source values without resurrecting an earlier prompt."""
        if not any(option.value == "reselect_basis_source" for option in checkpoint.request.options):
            return ()
        library = None
        band_names = target.evidence.axis.parent_band_ids
        component_names = target.component_names
        if checkpoint.basis_source == "curated_reference_library":
            record = self._accepted_curated_source(checkpoint, start).library
            band_names, component_names = record.band_names, record.component_names
            library = tuple((name, getattr(record, name)) for name in (
                "band_names", "component_names", "values", "normalization",
                "measurement_convention"))
        return (("saved_spectral_source", (
            ("source", checkpoint.basis_source), ("library", library),
            ("band_names", band_names),
            ("component_names", component_names),
            ("shared_cycle_reference", band_names != target.evidence.axis.parent_band_ids),
            ("correction_mode", target.evidence.quantitative_signature.measurements[0].correction_mode),
        )),)

    def _pending_target_af_proposal(self, checkpoint, start_authority_sha256):
        """Resolve the exact checkpoint-bound proposal without accepting it."""
        request = checkpoint.request
        if (request is None or request.checkpoint.value != "target_af_control_review"
                or len(request.proposed_artifacts) != 1):
            raise ValueError("workflow is not awaiting exact control target-AF review")
        pointer = request.proposed_artifacts[0].pointer
        if pointer.role != "proposed_target_af_qc":
            raise ValueError("workflow is not awaiting exact control target-AF review")
        stored = self._artifacts.load_all(
            run_id=checkpoint.run_id, start_authority_sha256=start_authority_sha256,
            kind="target_af_proposal", subject_id="target-af")
        matches = tuple(item.record for item in stored
                        if type(item.record) is TargetAutofluorescenceProposal
                        and item.record.artifact_id == pointer.artifact_id
                        and item.record.proposal_sha256 == pointer.artifact_sha256)
        if len(matches) != 1:
            raise ReferenceCheckpointConflictError("The autofluorescence spectrum for this review is unavailable.")
        proposal = matches[0]
        build_target_af_review_data(request, proposal,
                                    workflow_checkpoint_sha256=checkpoint.content_sha256)
        return proposal

    def _target_af_review_summary(self, checkpoint, start_authority_sha256):
        """Show the exact pending AF proposal using its retained measurements only."""
        request = checkpoint.request
        if (request is None or request.checkpoint.value != "target_af_control_review"
                or len(request.proposed_artifacts) != 1):
            return ()
        proposal = self._pending_target_af_proposal(checkpoint, start_authority_sha256)
        if proposal.control_binding.selection_kind == "review_reference_control_groups":
            from .reference_service_af_groups import target_af_groups_inventory
            inventory, _, _ = target_af_groups_inventory(self, checkpoint, start_authority_sha256)
            if inventory is None:
                raise ReferenceCheckpointConflictError("The retained AF comparison inventory is missing")
            included_sources = {item.control_input.input_id for item in proposal.control_bindings}
            filenames = [item["filename"] for item in inventory["sources"] if item["input_id"] in included_sources]
            return (("target_af_review", {**inventory,
                "source_kind": "review_reference_control_groups",
                "control_filename": (filenames[0] if len(filenames) == 1
                                     else f"{len(filenames)} included control sources"),
                "regions": [{**row, "component": row["component_identity"]} for row in inventory["regions"]],
                "correction_mode": proposal.control_binding.control_input.quantitative_signature.measurements[0].correction_mode,
            }),)
        regions = []
        components = {item.component_identity: item for item in proposal.estimation.components}
        for candidate in proposal.estimation.candidates:
            selection = candidate.source_roi.selection
            signed = candidate.signed_background_difference
            shape_norm = math.hypot(*candidate.corrected_values)
            combined = components[candidate.component_identity].values
            combined_norm = math.hypot(*combined)
            cosine = math.fsum(a*b for a,b in zip(candidate.corrected_values, combined, strict=True)) / (shape_norm * combined_norm)
            regions.append((("roi_id", selection.roi.roi_id),
                ("label", _reference_region_label(candidate.source_roi, "Autofluorescence")),
                ("component", candidate.component_identity), ("values", candidate.values),
                ("signed_values", signed), ("corrected_values", candidate.corrected_values),
                ("accepted_pixel_count", candidate.source_roi.sampling.accepted_vector_count),
                ("corrected_l2", shape_norm), ("clipped_count", sum(value < 0. for value in signed)),
                ("clipped_l2", math.hypot(*(min(value, 0.) for value in signed))),
                ("angle_to_combined_degrees", math.degrees(math.acos(max(-1., min(1., cosine)))))))
        control = proposal.control_binding.control_input
        return (("target_af_review", (
            ("control_filename", proposal.estimation.candidates[0].source_roi.selection.entry.image.source_path.name),
            ("source_kind", proposal.control_binding.selection_kind),
            ("measurement_names", control.axis.parent_band_ids),
            *_reference_wavelength_fields(control.axis),
            ("correction_mode", control.quantitative_signature.measurements[0].correction_mode),
            ("regions", tuple(regions)),
            ("components", tuple((("component", item.component_identity),
                ("roi_ids", tuple(member.source_roi.selection.roi.roi_id for member in
                    getattr(item, "members", (item,)))),
                ("roi_weights", tuple(1.0 / len(getattr(item, "members", (item,))) for _ in getattr(item, "members", (item,)))),
                ("signed_values", item.signed_background_difference),
                ("corrected_values", item.corrected_values), ("values", item.values))
                for item in proposal.estimation.components)),
        )),)

    def _spectral_review_summary(self, checkpoint, start_authority_sha256):
        """Project the current stored spectra and quality results without source reads."""
        request = checkpoint.request
        if (request is not None and request.checkpoint.value == "target_af_control_review"
                and request.proposed_artifacts):
            proposal = self._pending_target_af_proposal(checkpoint, start_authority_sha256)
            target = self._require_canonical_target(checkpoint.run_id, start_authority_sha256)
            columns, qc, _ = self._preview_control_final_basis(checkpoint, start_authority_sha256, proposal)
            return self._spectral_review_fields(target, proposal.control_binding.target_input.axis, columns, qc)
        if (
            request is None
            or request.checkpoint.value != "final_run_basis"
            or len(request.proposed_artifacts) != 1
        ):
            return ()
        pointer = request.proposed_artifacts[0].pointer
        if pointer.role != "proposed_run_basis_qc":
            return ()
        candidates = self._artifacts.load_all(
            run_id=checkpoint.run_id,
            start_authority_sha256=start_authority_sha256,
            kind="final_basis_proposal",
            subject_id="final-basis",
        )
        matches = tuple(
            item.record
            for item in candidates
            if item.record.proposal_sha256 == pointer.artifact_sha256
        )
        if len(matches) != 1 or type(matches[0]) is not FinalRunBasisProposal:
            raise ReferenceCheckpointConflictError("The spectra for this review are unavailable.")
        proposal = matches[0]
        target = self._require_canonical_target(checkpoint.run_id, start_authority_sha256)
        return self._spectral_review_fields(target, proposal.axis, proposal.columns, proposal.qc)

    def _spectral_review_fields(self, target, axis, columns, qc):
        biomarkers = self._component_biomarker_defaults(target, columns)
        spectra = tuple(
            (
                ("component", column.component_identity),
                ("kind", column.component_kind),
                ("biomarker", biomarkers[index]),
                ("values", tuple(column.values)),
            )
            for index, column in enumerate(columns)
        )
        pairs = tuple(
            (
                ("first", item.first_component_identity),
                ("second", item.second_component_identity),
                ("similarity", item.cosine_similarity),
            )
            for item in qc.pairwise_spectra
        )
        wavelength_fields = _reference_wavelength_fields(axis)
        return (
            (
                "spectral_review",
                (
                    ("measurement_names", axis.parent_band_ids),
                    *wavelength_fields,
                    ("spectra", spectra),
                    ("quality", qc.disposition),
                    ("condition_number", qc.condition_number),
                    ("rank", qc.rank),
                    ("component_count", len(columns)),
                    ("pairwise_spectra", pairs),
                    ("flagged_pairs", tuple(
                        pair for pair in pairs
                        if dict(pair)["similarity"] >= qc.policy.cosine_review_threshold
                    )),
                    ("review_limits", (
                        ("condition_number", qc.policy.condition_review_threshold),
                        ("cosine_similarity", qc.policy.cosine_review_threshold),
                    )),
                    ("rejection_limits", (
                        ("condition_number", qc.policy.condition_fail_threshold),
                        ("cosine_similarity", qc.policy.cosine_fail_threshold),
                    )),
                ),
            ),
        )

    def load_persisted_view(self, run_id: str) -> ReferenceServiceView:
        """Load bounded persisted state without reopening canonical inputs."""

        checkpoint, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        return self._bounded_view(checkpoint, start_authority_sha256)

    def bounded_view(self, snapshot: ReferenceServiceSnapshot) -> ReferenceServiceView:
        """Project one live internal snapshot to the same bounded public view."""

        if type(snapshot) is not ReferenceServiceSnapshot:
            raise TypeError("bounded view requires exact service snapshot")
        return self._bounded_view(snapshot.checkpoint, snapshot.start_authority_sha256)

    def _canonical_input_from_path(
        self,
        path,
        evidence: ReferenceInputEvidence,
        measurement_planes: tuple[object, ...],
        *,
        compatibility_target: CanonicalReferenceInput | None = None,
    ) -> CanonicalReferenceInput:
        if self._backend is None:
            from mif_pipeline.io import get_default_backend

            backend = get_default_backend()
        else:
            backend = self._backend
        with backend.open(path) as source:
            return canonical_reference_input_from_opened_source(
                path,
                source,
                evidence,
                measurement_planes,
                compatibility_target=compatibility_target,
            )

    def _persist_canonical_input(
        self,
        *,
        run_id: str,
        checkpoint: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        record: CanonicalReferenceInput,
    ) -> CanonicalReferenceInput:
        try:
            stored = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=checkpoint.content_sha256,
                kind="canonical_input",
                subject_id=("target" if record.input_role == "target_image" else record.input_id),
                record=record,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "canonical input registration conflicts with existing authority"
            ) from exc
        if type(stored.record) is not CanonicalReferenceInput:
            raise ReferenceCheckpointConflictError(
                "canonical input persistence returned an invalid authority"
            )
        return stored.record

    def _start_with_target_authority(
        self,
        run_id: str,
        target_input: ReferenceInputEvidence,
        channel_fluors: tuple[object, ...],
        af_behavior: str = "retain",
    ) -> ReferenceServiceSnapshot:
        """Create the initial checkpoint from exact service-owned authority."""

        if type(target_input) is not ReferenceInputEvidence:
            raise TypeError("target input must be exact ReferenceInputEvidence")
        if target_input.input_role != "target_image":
            raise ValueError("reference workflow target input must have target_image role")
        fluorophores = tuple(name for name in resolve_channel_fluor_identities(channel_fluors)
                             if not is_autofluorescence_component(name))
        checkpoint = start_reference_workflow(
            run_id=run_id,
            target_image_sha256=target_input.source_image_sha256,
            fluorophores=fluorophores,
            measurement_ids=target_input.axis.measurement_ids,
            af_behavior=af_behavior,
        )
        persisted, start_authority_sha256 = self._store._create_service_checkpoint(
            checkpoint, target_input
        )
        return ReferenceServiceSnapshot(persisted, start_authority_sha256)


    def start_from_reviewed_target(
        self,
        run_id: str,
        authority: InspectedSourceAuthority,
        reviewed_ome: OME,
        *, af_behavior: str = "retain",
    ) -> ReferenceServiceSnapshot:
        """Start from an authenticated reviewed source without publishing raw TIFF."""
        record = canonical_target_reference_input_from_reviewed_source(
            authority, reviewed_ome, backend=self._sampling_backend()
        )
        snapshot = self._start_with_target_authority(run_id, record.evidence, record.component_names, af_behavior)
        self._persist_canonical_input(
            run_id=run_id,
            checkpoint=snapshot.checkpoint,
            start_authority_sha256=snapshot.start_authority_sha256,
            record=record,
        )
        return snapshot

    def register_canonical_input(
        self,
        run_id: str,
        path,
        input_evidence: ReferenceInputEvidence,
        measurement_planes: tuple[object, ...],
    ) -> CanonicalReferenceInput:
        """Register one exact non-target reference/control input without advancing."""
        if type(input_evidence) is not ReferenceInputEvidence:
            raise TypeError("reference input must be exact ReferenceInputEvidence")
        if input_evidence.input_role == "target_image":
            raise ValueError("target inputs must use start_from_reviewed_target")
        current, start_authority_sha256 = self._revalidate_canonical_inputs(run_id)
        try:
            inventory = self._artifacts._load_all_canonical_inputs(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
            )
        except ReferenceArtifactRepositoryError as exc:
            if str(exc) != "reference artifact repository does not exist":
                raise ReferenceCheckpointConflictError(
                    "canonical input inventory is invalid"
                ) from exc
            inventory = ()
        targets = tuple(
            item.record for item in inventory if item.record.input_role == "target_image"
        )
        if len(targets) != 1:
            raise ReferenceCheckpointConflictError(
                "canonical non-target input requires exactly one persisted target authority"
            )
        if targets[0].evidence.structural_signature is None:
            raise CanonicalReferenceInputError(
                "non-target registration requires target imaging-settings compatibility profile"
            )
        prior_claims = tuple(
            item for item in inventory if item.record.input_id == input_evidence.input_id
        )
        if any(item.record.input_role == "target_image" for item in prior_claims):
            raise ReferenceCheckpointConflictError("canonical input ID duplicates the target input")
        record = self._canonical_input_from_path(
            path,
            input_evidence,
            measurement_planes,
            compatibility_target=targets[0],
        )
        fenced, fenced_start = self._revalidate_canonical_inputs(run_id)
        if (
            fenced.content_sha256 != current.content_sha256
            or fenced_start != start_authority_sha256
        ):
            raise ReferenceCheckpointConflictError(
                "canonical target authority changed during input registration"
            )
        existing = prior_claims
        if existing and any(item.record != record for item in existing):
            raise ReferenceCheckpointConflictError(
                "canonical input ID already binds conflicting authority"
            )
        persisted_record = self._persist_canonical_input(
            run_id=run_id,
            checkpoint=current,
            start_authority_sha256=start_authority_sha256,
            record=record,
        )
        return persisted_record

    def register_canonical_input_locator(
        self,
        run_id: str,
        expected_content_sha256: str,
        path,
        *,
        input_id: str,
        input_role: str,
        fluorophore_identity: str | None = None,
    ) -> ReferenceServiceAdvance:
        """Register one canonical locator and return the persisted service state."""

        self.register_canonical_input_path(
            run_id,
            expected_content_sha256,
            path,
            input_id=input_id,
            input_role=input_role,
            fluorophore_identity=fluorophore_identity,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            self.resume(run_id),
        )

    def register_canonical_input_path(
        self,
        run_id: str,
        expected_content_sha256: str,
        path,
        *,
        input_id: str,
        input_role: str,
        fluorophore_identity: str | None = None,
    ) -> CanonicalReferenceInput:
        """Derive one non-target canonical authority without caller evidence."""

        current, start_authority_sha256 = self._revalidate_canonical_inputs(run_id)
        if current.content_sha256 != expected_content_sha256:
            raise ReferenceCheckpointConflictError(
                "canonical input registration checkpoint expectation is stale"
            )
        if input_role == "target_image":
            raise ValueError("target inputs must use canonical target start")
        target = self._require_canonical_target(run_id, start_authority_sha256)
        if target.evidence.structural_signature is None:
            raise CanonicalReferenceInputError(
                "non-target registration requires target imaging-settings compatibility profile"
            )
        with self._sampling_backend().open(path) as source:
            record = canonical_reference_input_authority_from_opened_source(
                path,
                source,
                input_id=input_id,
                input_role=input_role,
                fluorophore_identity=fluorophore_identity,
                compatibility_target=target,
            )
        fenced, fenced_start = self._revalidate_canonical_inputs(run_id)
        if (
            fenced.content_sha256 != current.content_sha256
            or fenced_start != start_authority_sha256
        ):
            raise ReferenceCheckpointConflictError(
                "canonical target authority changed during input registration"
            )
        return self._persist_canonical_input(
            run_id=run_id,
            checkpoint=current,
            start_authority_sha256=start_authority_sha256,
            record=record,
        )

    def register_reviewed_input(
        self,
        run_id: str,
        expected_content_sha256: str,
        authority: InspectedSourceAuthority,
        reviewed_ome: OME,
        *,
        input_id: str,
        input_role: str,
        fluorophore_identity: str | None = None,
    ) -> CanonicalReferenceInput:
        """Register one reviewed empirical input against the exact live target."""
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 != expected_content_sha256:
            raise ReferenceCheckpointConflictError(
                "canonical input registration checkpoint expectation is stale"
            )
        if input_role == "target_image":
            raise ValueError("target inputs must use reviewed target start")
        target = self._require_canonical_target(run_id, start_authority_sha256)
        if target.evidence.structural_signature is None:
            raise CanonicalReferenceInputError(
                "non-target registration requires target imaging-settings compatibility profile"
            )
        # The persisted target already owns its inspected acquisition facts.
        # Check its location now; source readers validate those facts again when
        # sampling pixels. Registration opens only the newly reviewed reference.
        self._validate_reference_locations((target,))
        record = canonical_reference_input_authority_from_reviewed_source(
            authority,
            reviewed_ome,
            input_id=input_id,
            input_role=input_role,
            fluorophore_identity=fluorophore_identity,
            compatibility_target=target,
            backend=self._sampling_backend(),
        )
        fenced, fenced_start = self._store._load_service_checkpoint(run_id)
        if (
            fenced.content_sha256 != current.content_sha256
            or fenced_start != start_authority_sha256
        ):
            raise ReferenceCheckpointConflictError(
                "canonical target authority changed during input registration"
            )
        self._validate_reference_locations((target, record))
        return self._persist_canonical_input(
            run_id=run_id,
            checkpoint=current,
            start_authority_sha256=start_authority_sha256,
            record=record,
        )

    def rebind_canonical_input_locator(
        self,
        run_id: str,
        input_id: str,
        path,
    ) -> CanonicalReferenceInput:
        """CAS-publish one freshly validated locator for unchanged science."""

        checkpoint, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        try:
            inventory = self._artifacts._load_all_canonical_inputs(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError("canonical input inventory is invalid") from exc
        matches = tuple(item.record for item in inventory if item.record.input_id == input_id)
        targets = tuple(
            item.record for item in inventory if item.record.input_role == "target_image"
        )
        if len(matches) != 1 or len(targets) != 1:
            raise ReferenceCheckpointConflictError(
                "canonical locator rebind requires one exact input and target"
            )
        record = matches[0]
        target = targets[0]
        if record.locator_binding.source_kind != "canonical_tiff":
            raise CanonicalReferenceInputError(
                "reviewed source relocation requires explicit inspected and reviewed authority"
            )
        if record.input_role != "target_image":
            try:
                with target.open_source(self._sampling_backend()):
                    pass
            except (
                CanonicalReferenceInputError,
                CanonicalIngestionError,
                OSError,
                ValueError,
            ) as exc:
                raise ReferenceCheckpointConflictError(
                    "canonical target cannot be fenced before candidate relocation"
                ) from exc
        with self._sampling_backend().open(path) as source:
            rebound = record.bind_locator_from_opened_source(
                path,
                source,
                compatibility_target=(None if record.input_role == "target_image" else target),
            )
        if record.input_role != "target_image":
            try:
                with target.open_source(self._sampling_backend()):
                    pass
            except (
                CanonicalReferenceInputError,
                CanonicalIngestionError,
                OSError,
                ValueError,
            ) as exc:
                raise ReferenceCheckpointConflictError(
                    "canonical target changed before candidate relocation"
                ) from exc
        try:
            self._artifacts.replace_canonical_input_locator(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=checkpoint.content_sha256,
                subject_id=("target" if record.input_role == "target_image" else record.input_id),
                previous_record=record,
                record=rebound,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "canonical locator rebind conflicts with live binding authority"
            ) from exc
        try:
            fenced, fenced_start = self._revalidate_canonical_inputs(run_id)
            if (
                fenced.content_sha256 != checkpoint.content_sha256
                or fenced_start != start_authority_sha256
            ):
                raise ReferenceCheckpointConflictError(
                    "canonical locator rebind raced with workflow authority"
                )
        except Exception as exc:
            try:
                self._artifacts.replace_canonical_input_locator(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    authority_checkpoint_sha256=checkpoint.content_sha256,
                    subject_id=(
                        "target" if record.input_role == "target_image" else record.input_id
                    ),
                    previous_record=rebound,
                    record=record,
                )
            except ReferenceArtifactRepositoryError as rollback_exc:
                raise ReferenceCheckpointConflictError(
                    "canonical locator rebind failed its final fence and rollback CAS"
                ) from rollback_exc
            if isinstance(exc, ReferenceCheckpointConflictError):
                raise
            raise ReferenceCheckpointConflictError(
                "canonical locator rebind failed its final live-input fence"
            ) from exc
        return rebound

    def _require_canonical_target(
        self,
        run_id: str,
        start_authority_sha256: str,
    ) -> CanonicalReferenceInput:
        """Return the one persisted target or reject legacy-started runs."""

        try:
            inventory = self._artifacts._load_all_canonical_inputs(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
            )
        except ReferenceArtifactRepositoryError as exc:
            if str(exc) == "reference artifact repository does not exist":
                inventory = ()
            else:
                raise ReferenceCheckpointConflictError(
                    "canonical input inventory is invalid"
                ) from exc
        targets = tuple(
            item.record
            for item in inventory
            if type(item.record) is CanonicalReferenceInput
            and item.record.input_role == "target_image"
        )
        if len(targets) != 1:
            raise ValueError("source-choice operations require canonical target authority")
        return targets[0]

    def export_library(self, run_id: str, destination: Path) -> Path:
        """Copy the current accepted portable library without rereading source images."""
        from pathlib import Path
        from .library_exchange import write_library_exchange

        current, start = self._store._load_service_checkpoint(run_id)
        if current.accepted_library is None:
            from .shared_reference_workflow import SharedReferenceCoordinator
            coordinator = SharedReferenceCoordinator(self)
            session = coordinator._session(run_id, start)
            catalogue = None if session is None else coordinator._catalogue(session)
            if catalogue is None:
                raise ValueError("No accepted portable library is available to export yet")
            return write_library_exchange(catalogue, Path(destination))
        stored = self._artifacts.load_pointer(
            run_id=run_id,
            start_authority_sha256=start,
            kind="accepted_library",
            subject_id="library",
            pointer=current.accepted_library,
        )
        if type(stored.record) not in {FluorophoreReferenceLibrary, SharedReferenceCatalogue}:
            raise ValueError("The retained library artifact has a different type")
        return write_library_exchange(stored.record, Path(destination))

    @staticmethod
    def _validate_reference_locations(inputs: tuple[CanonicalReferenceInput, ...]) -> None:
        """Reject changed source files before a metadata-only review is saved."""
        for record in inputs:
            try:
                record.validate_source_location()
            except (CanonicalIngestionError, OSError, ValueError) as exc:
                raise ReferenceCheckpointConflictError(
                    "The source or reference image has changed since it was reviewed. "
                    "Return to image setup and review the current file before continuing."
                ) from exc

    def reference_region_inputs(self, run_id: str, checkpoint_token: str):
        """Return the accepted stain/control inputs for the current region review.

        Read retained input records and source observations only. The target is
        checked for compatibility but is never added to the annotation project.
        """
        current, start, _target, inputs = self._basis_choice_inputs(run_id)
        if (current.content_sha256 != checkpoint_token or current.current_checkpoint is None
                or current.current_checkpoint.value not in {
                    "shared_control_preparation",
                    "positive_reference_rois", "af_reference_rois",
                    "negative_reference_rois", "background_reference_rois"}):
            raise ValueError("Open the current reference region review before editing regions.")
        binding = self._one_artifact(run_id, start, "reference_control_files",
                                     "reference-control-files").record
        if current.current_checkpoint.value == "shared_control_preparation":
            from .reference_service_control_preparation import source_input_id
            selected_ids = [source_input_id(binding, current)]
        else:
            index = current.fluorophores.index(current.current_fluorophore)
            selected_ids = [binding.positive_reference_input_ids[index]]
        if binding.prepare_shared_control:
            pass
        elif binding.shared_unstained_input_id is not None:
            selected_ids.append(binding.shared_unstained_input_id)
        else:
            selected_ids.append(binding.negative_control_input_ids[index])
        by_id = {item.input_id: item for item in inputs}
        selected = tuple(by_id[input_id] for input_id in selected_ids)
        return current, selected

    def _basis_choice_inputs(
        self, run_id: str,
    ) -> tuple[ReferenceWorkflowCheckpoint, str, CanonicalReferenceInput,
               tuple[CanonicalReferenceInput, ...]]:
        """Read the authenticated metadata needed to choose a spectral library.

        This choice reads no image pixels. Reuse the parsed records throughout
        the decision, checking file observations again at publication boundaries.
        Scientific sampling opens and validates the full source independently.
        """
        current, start = self._store._load_service_checkpoint(run_id)
        inputs = tuple(item.record for item in self._artifacts._load_all_canonical_inputs(
            run_id=run_id, start_authority_sha256=start,
        ))
        targets = tuple(record for record in inputs if record.input_role == "target_image")
        if len(targets) != 1:
            raise ReferenceCheckpointConflictError("reference choice requires one canonical target")
        target = targets[0]
        for record in inputs:
            binding = record.compatibility
            if (binding.target_image_sha256 != target.evidence.source_image_sha256
                    or binding.target_axis != target.evidence.axis
                    or binding.target_structural_signature != target.evidence.structural_signature
                    or binding.target_quantitative_signature != target.evidence.quantitative_signature):
                raise ReferenceCheckpointConflictError("reference input target compatibility differs")
        self._validate_reference_locations(inputs)
        return current, start, target, inputs

    def validate_basis_source_choice(
        self, run_id: str, expected_content_sha256: str, choice: str, *, imported_library=None
    ) -> None:
        """Check a source choice without accepting it or publishing any artifact."""
        from .compatibility import resolve_basis_source
        from .reference_models import ReviewRecord

        current, _start, target, _inputs = self._basis_choice_inputs(run_id)
        if _multiple_component_cycles(target.component_names) and choice in {"construct_reference_library", "import_reference_library"}:
            from .cycle_references import shared_cycle_reference_layout
            shared_cycle_reference_layout(target, current.af_behavior)
            if choice == "import_reference_library":
                from .shared_reference_workflow import compatible_imported_catalogue
                from .library_exchange import load_library_exchange
                source = load_library_exchange(imported_library) if type(imported_library) is bytes else imported_library
                compatible_imported_catalogue(self, current, target, source)
            elif imported_library is not None:
                raise ValueError("A new construction cannot carry an imported library")
            if current.content_sha256 != expected_content_sha256 or current.current_checkpoint.value != "basis_source":
                raise ValueError("The spectral-reference form has changed")
            return
        else:
            _require_single_cycle_reference_route(target.component_names)
        if (
            current.content_sha256 != expected_content_sha256
            or current.request is None
            or current.request.checkpoint.value != "basis_source"
        ):
            raise ValueError("The spectral-reference form has changed. Reopen the current form.")
        selected = BasisSourceChoice(choice)
        if selected is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE:
            result = resolve_basis_source(
                selected,
                run_id=run_id,
                channel_fluors=target.component_names,
                target_axis=target.evidence.axis,
                target_structural_signature=None,
                target_quantitative_signature=target.evidence.quantitative_signature,
                review=ReviewRecord("pending", "preflight", datetime.now(timezone.utc).isoformat()),
                target_image_sha256=target.evidence.source_image_sha256,
            )
            if result.required_action != "accept_resolved_basis_review":
                raise ValueError(
                    "The PhenoImager MOTiF library does not match this image's imaging settings."
                )
        else:
            structural = target.evidence.structural_signature
            if structural is None:
                raise ValueError(
                    "Imaging settings and intensity-correction details are missing. Complete those image details before using reference images or an imported library."
                )
            if selected is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY:
                library, _ = self._source_choice_operations._strict_imported_library(
                    imported_library
                )
                self._source_choice_operations._validate_imported_library(current, target, library)
            elif imported_library is not None:
                raise ValueError(
                    "Choose Import or enter a spectral library to use the selected library file."
                )

    def accept_supplied_file(self, run_id, checkpoint_token, *, reviewer_id, **values):
        """Use the existing portable-library or curated-matrix acceptance route."""
        library, csv_context = self._read_supplied_library(run_id, checkpoint_token, **values)
        if type(library) in {FluorophoreReferenceLibrary, SharedReferenceCatalogue}:
            return self.accept_basis_source_choice(run_id, checkpoint_token,
                BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY,
                reviewer_id=reviewer_id, imported_library=library)
        current, start, target = csv_context
        return self._accept_curated_library(
            run_id, checkpoint_token, library, reviewer_id=reviewer_id,
            current=current, start=start, target=target,
        )

    def accept_curated_library(self, run_id, expected_content_sha256, library, *, reviewer_id):
        """Accept declared numerical spectra, never invented reference-image evidence."""
        current, start = self._store._load_service_checkpoint(run_id)
        target = self._require_canonical_target(run_id, start)
        return self._accept_curated_library(
            run_id, expected_content_sha256, library, reviewer_id=reviewer_id,
            current=current, start=start, target=target,
        )

    def _accept_curated_library(
        self, run_id, expected_content_sha256, library, *, reviewer_id,
        current, start, target,
    ):
        """Validate and publish against the target authenticated for this action."""
        from .curated_library import CuratedSourceDecision, validate_curated_target

        validate_curated_target(library, target, current.af_behavior)
        if (
            current.content_sha256 != expected_content_sha256
            or current.request is None
            or current.request.checkpoint.value != "basis_source"
        ):
            raise ReferenceCheckpointConflictError(
                "The spectral source choice has changed. Reopen its current form."
            )
        decision = CuratedSourceDecision(
            current.content_sha256,
            target.authority_sha256,
            target.evidence.source_image_sha256,
            library,
            reviewer_id,
        )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start,
            authority_checkpoint_sha256=current.content_sha256,
            kind="curated_source",
            subject_id="curated:" + current.content_sha256,
            record=decision,
        )
        response = configured_response(
            current.request, option_value="curated_reference_library", reviewer_id=reviewer_id
        )
        candidate = advance_reference_workflow(current, response)
        persisted, retained_start = self._store._compare_and_swap_service(
            current.content_sha256, candidate, start
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, retained_start),
        )

    def accept_basis_source_choice(
        self,
        run_id: str,
        expected_content_sha256: str,
        choice: BasisSourceChoice | str,
        *,
        reviewer_id: str,
        imported_library: FluorophoreReferenceLibrary | bytes | None = None,
        component_names: tuple[str, ...] = (),
    ) -> ReferenceServiceAdvance:
        """Accept one service-derived basis source for a canonical-start run."""

        _current, _start, target, inputs = self._basis_choice_inputs(run_id)
        if _multiple_component_cycles(target.component_names) and choice in {"construct_reference_library", "import_reference_library"}:
            from .shared_reference_workflow import SharedReferenceCoordinator
            from .library_exchange import load_library_exchange
            source = load_library_exchange(imported_library) if type(imported_library) is bytes else imported_library
            if choice == "construct_reference_library" and source is not None:
                raise ValueError("A new construction cannot carry an imported library")
            if choice == "import_reference_library" and source is None:
                raise ValueError("Select the accepted fluorophore library")
            SharedReferenceCoordinator(self).start(run_id, expected_content_sha256,
                reviewer_id=reviewer_id, imported_library=source)
            return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED,
                                            ReferenceServiceSnapshot(_current, _start))
        _require_single_cycle_reference_route(target.component_names)
        return self._source_choice_operations.accept_basis_source_choice(
            run_id,
            expected_content_sha256,
            choice,
            reviewer_id=reviewer_id,
            imported_library=imported_library,
            component_names=component_names,
            target=target,
            precommit_fence=lambda: self._validate_reference_locations(inputs),
        )

    def accept_reference_control_files(
        self,
        run_id: str,
        expected_content_sha256: str,
        positive_reference_input_ids: tuple[str, ...],
        *,
        reviewer_id: str,
        shared_unstained_input_id: str | None = None,
        negative_control_input_ids: tuple[str, ...] = (),
        prepare_shared_control: bool = False,
    ) -> ReferenceServiceAdvance:
        """Bind registered canonical reference/control inputs in live panel order."""

        _, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        target = self._require_canonical_target(run_id, start_authority_sha256)
        return self._source_choice_operations.accept_reference_control_files(
            run_id,
            expected_content_sha256,
            positive_reference_input_ids,
            shared_unstained_input_id=shared_unstained_input_id,
            negative_control_input_ids=negative_control_input_ids,
            reviewer_id=reviewer_id,
            target=target,
            prepare_shared_control=prepare_shared_control,
        )

    def prepare_shared_control(self, run_id, checkpoint_token, **values):
        from .reference_service_control_preparation import prepare_shared_control
        return prepare_shared_control(self, run_id, checkpoint_token, **values)

    def accept_shared_control(self, run_id, checkpoint_token, *, reviewer_id):
        from .reference_service_control_preparation import accept_shared_control
        return accept_shared_control(self, run_id, checkpoint_token, reviewer_id=reviewer_id)

    def review_control_sets(self, run_id, checkpoint_token, **values):
        from .reference_service_control_preparation import review_control_sets
        return review_control_sets(self, run_id, checkpoint_token, **values)

    def inspect_component_preparation(self, run_id, checkpoint_token, *, component, phase):
        from .reference_service_component_revision import inspect_component_preparation
        return inspect_component_preparation(self, run_id, checkpoint_token, component=component, phase=phase)

    def revise_component_preparation(self, run_id, checkpoint_token, *, component, phase, reviewer_id, values):
        from .reference_service_component_revision import revise_component_preparation
        return revise_component_preparation(self, run_id, checkpoint_token, component=component,
            phase=phase, reviewer_id=reviewer_id, values=values)

    def shared_control_preparation(self, run_id):
        from .reference_service_control_preparation import accepted_preparation
        current, start = self._store._load_service_checkpoint(run_id)
        return accepted_preparation(self, current, start)

    def prepare_stained_reference(self, run_id, checkpoint_token, **values):
        from .reference_service_control_preparation import prepare_stained_reference
        return prepare_stained_reference(self, run_id, checkpoint_token, **values)

    def assign_shared_control_sets(self, run_id, checkpoint_token, **values):
        from .reference_service_control_preparation import assign_shared_control_sets
        return assign_shared_control_sets(self, run_id, checkpoint_token, **values)

    def start(
        self,
        run_id: str,
        target_input: ReferenceInputEvidence,
        channel_fluors: tuple[object, ...],
        af_behavior: str = "retain",
    ) -> ReferenceServiceSnapshot:
        return self._start_with_target_authority(run_id, target_input, channel_fluors, af_behavior)

    def resume(self, run_id: str) -> ReferenceServiceSnapshot:
        checkpoint, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        return ReferenceServiceSnapshot(checkpoint, start_authority_sha256)

    def reopen_stopped_review(self, run_id: str, expected_content_sha256: str,
                             previous_checkpoint_sha256: str, *, publish: bool) -> ReferenceServiceSnapshot:
        """Check or reopen one stopped review against its exact saved predecessor.

        Read-only eligibility checks saved checkpoint and artifact identities.
        Publication also revalidates canonical source inputs before appending the
        ordinary revision, preserving accepted artifacts and the historical Stop.
        """
        from .reference_workflow import reopen_stopped_reference_checkpoint

        current, start = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 != expected_content_sha256:
            # The reference checkpoint can be saved before the batch publishes
            # its recovered form. Reopen that exact successor after interruption.
            if (current.prior_checkpoint_sha256 != expected_content_sha256
                    or current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
                    or not current.decisions):
                raise ReferenceCheckpointConflictError("The stopped spectral review has changed.")
            _previous, stopped, saved_start = self._store._load_service_decision_authority(
                run_id, source_checkpoint_sha256=previous_checkpoint_sha256,
                decision_record_sha256=current.decisions[-1].content_sha256)
            if (saved_start != start or stopped.content_sha256 != expected_content_sha256
                    or reopen_stopped_reference_checkpoint(stopped,
                        previous_checkpoint_sha256=previous_checkpoint_sha256) != current):
                raise ReferenceCheckpointConflictError("The recovered spectral review differs from the selected Stop.")
            return ReferenceServiceSnapshot(current, start)
        candidate = reopen_stopped_reference_checkpoint(
            current, previous_checkpoint_sha256=previous_checkpoint_sha256)
        if publish:
            candidate, start = self._store._compare_and_swap_service(
                current.content_sha256, candidate, start)
        return ReferenceServiceSnapshot(candidate, start)

    def _revalidate_canonical_inputs(self, run_id: str) -> tuple[ReferenceWorkflowCheckpoint, str]:
        """Service-owned fence required before a live run can mutate or publish."""
        checkpoint, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        try:
            canonical_inputs = self._artifacts._load_all_canonical_inputs(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
            )
        except ReferenceArtifactRepositoryError as exc:
            if str(exc) != "reference artifact repository does not exist":
                raise
            canonical_inputs = ()
        targets = tuple(
            item.record
            for item in canonical_inputs
            if type(item.record) is CanonicalReferenceInput
            and item.record.input_role == "target_image"
        )
        target = targets[0] if len(targets) == 1 else None
        ordered_inputs = tuple(
            item for item in canonical_inputs if item.record.input_role == "target_image"
        ) + tuple(item for item in canonical_inputs if item.record.input_role != "target_image")
        for stored in ordered_inputs:
            record = stored.record
            if type(record) is not CanonicalReferenceInput:
                raise ReferenceCheckpointConflictError(
                    "stored canonical input has the wrong exact record type"
                )
            try:
                with record.open_source(
                    self._sampling_backend(),
                    compatibility_target=(None if record.input_role == "target_image" else target),
                ):
                    pass
            except (
                CanonicalReferenceInputError,
                CanonicalIngestionError,
                OSError,
                ValueError,
            ) as exc:
                raise ReferenceCheckpointConflictError(
                    "canonical input cannot be revalidated at its persisted locator"
                ) from exc
        # Recheck the target after comparing other images. With only the target,
        # the preceding loop has already opened and checked that same source.
        if target is not None and any(
            item.record.input_role != "target_image" for item in canonical_inputs
        ):
            try:
                with target.open_source(self._sampling_backend()):
                    pass
            except (
                CanonicalReferenceInputError,
                CanonicalIngestionError,
                OSError,
                ValueError,
            ) as exc:
                raise ReferenceCheckpointConflictError(
                    "canonical target cannot be revalidated after candidate comparison"
                ) from exc
        return checkpoint, start_authority_sha256

    def respond(
        self,
        run_id: str,
        expected_content_sha256: str,
        response: ReferenceResponse | dict[str, object],
    ) -> ReferenceServiceAdvance:
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 != expected_content_sha256:
            raise ReferenceCheckpointConflictError(
                "service response checkpoint expectation is stale"
            )
        if (
            current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
            or current.request is None
        ):
            raise ValueError("current reference workflow cannot accept a response")
        decision = validate_reference_response(current.request, response)
        safely_parameter_free = (
            decision.action is ReferenceActionCode.ABORT
            or decision.option_value in _SAFE_PARAMETER_FREE_OPTIONS
        )
        if not safely_parameter_free:
            retained, retained_start = self._store._compare_and_swap_service(
                expected_content_sha256, current, start_authority_sha256
            )
            return ReferenceServiceAdvance(
                status=ReferenceServiceAdvanceStatus.NEEDS_SCIENTIFIC_RESULT,
                snapshot=ReferenceServiceSnapshot(retained, retained_start),
                reason=(
                    "validated response requires an exact typed scientific result "
                    "before the workflow can advance"
                ),
            )
        candidate = advance_reference_workflow(current, decision)
        persisted, persisted_start = self._store._compare_and_swap_service(
            expected_content_sha256, candidate, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            status=ReferenceServiceAdvanceStatus.PERSISTED,
            snapshot=ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def propose_control_target_af_from_ids(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        control_input_id: str | None,
        roi_export: ReferenceRoiExport,
        af_roi_ids: tuple[str, ...],
        background_roi_ids: tuple[str, ...],
        roi_reviews: tuple[Mapping[str, object], ...],
        sampling_policy: Mapping[str, object],
        background_policy: Mapping[str, object],
        estimation_policy: Mapping[str, object],
        qc_policy: Mapping[str, object],
        control_review: Mapping[str, object],
        reviewer_id: str,
    ) -> ReferenceServiceAdvance:
        """Build a control-derived target-AF proposal from reviewed IDs only."""

        if type(roi_export) is not ReferenceRoiExport:
            raise TypeError("control target-AF requires exact internally loaded ROI export")
        for values, label, allow_empty in (
            (af_roi_ids, "AF ROI IDs", False),
            (
                background_roi_ids,
                "background ROI IDs",
                control_input_id is None,
            ),
        ):
            if (
                type(values) is not tuple
                or (not values and not allow_empty)
                or any(type(item) is not str or not item for item in values)
                or len(values) != len(set(values))
            ):
                qualifier = "possibly empty" if allow_empty else "nonempty"
                raise ValueError(f"{label} must be a {qualifier} unique tuple")
        if control_input_id is None and background_roi_ids:
            raise ValueError("shared target-AF control cannot carry background ROI IDs")
        if type(roi_reviews) is not tuple:
            raise TypeError("control target-AF ROI reviews must be an exact tuple")
        reviews = tuple(
            TargetAfRoiReviewSelection(
                **_strict_record_mapping(
                    item, TargetAfRoiReviewSelection, "control target-AF ROI review"
                )
            )
            for item in roi_reviews
        )
        control_review_fields = {"preparation_disposition", "tissue_context_disposition"}
        if type(control_review) is not dict or set(control_review) != control_review_fields:
            raise ValueError("target-control review fields differ")
        control_review_value = dict(control_review)
        control_review_value.update(
            {
                "reviewer_id": reviewer_id,
                "policy_id": _TARGET_CONTROL_REVIEW_POLICY_ID,
                "policy_version": _TARGET_CONTROL_REVIEW_POLICY_VERSION,
            }
        )
        sampling, background, estimation, qc = _control_target_af_policies_from_mappings(
            sampling=sampling_policy,
            background=background_policy,
            estimation=estimation_policy,
            qc=qc_policy,
        )
        operation_id = typed_checksum_sha256(
            {
                "run_id": run_id,
                "checkpoint": expected_content_sha256,
                "roi_export": roi_export.content_sha256,
                "af_roi_ids": list(af_roi_ids),
                "background_roi_ids": list(background_roi_ids),
                "roi_reviews": [dict(item) for item in roi_reviews],
                "sampling_policy": sampling_policy,
                "background_policy": background_policy,
                "estimation_policy": estimation_policy,
                "qc_policy": qc_policy,
                "control_review": control_review,
                "reviewer_id": reviewer_id,
            }
        )
        created_at = _derived_service_timestamp(
            expected_content_sha256, "control-target-af-proposal"
        )
        return self.propose_control_target_af(
            run_id,
            expected_content_sha256,
            control_input_id=control_input_id,
            roi_export=roi_export,
            af_selections=tuple(
                select_accepted_reference_roi(roi_export, item) for item in af_roi_ids
            ),
            background_selections=tuple(
                select_accepted_reference_roi(roi_export, item) for item in background_roi_ids
            ),
            roi_reviews=reviews,
            sampling_policy=sampling,
            background_policy=background,
            estimation_policy=estimation,
            qc_policy=qc,
            artifact_id="control-target-af-" + operation_id[:24],
            created_at=created_at,
            control_review=TargetControlReviewResponse(**control_review_value),
            roi_reviewer_id=reviewer_id,
            software_versions=(VersionRecord("mif_pipeline", "reference-unmixing-operation/v1"),),
            policy_versions=(
                VersionRecord("background", background.policy_version),
                VersionRecord("target_af", estimation.policy_version),
                VersionRecord("fixed_basis_qc", qc.policy_version),
            ),
        )

    def propose_control_target_af(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        control_input_id: str | None,
        roi_export: ReferenceRoiExport,
        af_selections: tuple[AcceptedReferenceRoiSelection, ...],
        background_selections: tuple[AcceptedReferenceRoiSelection, ...],
        roi_reviews: tuple[TargetAfRoiReviewSelection, ...],
        sampling_policy: CompleteVectorQcPolicy,
        background_policy: BackgroundEquivalencePolicy,
        estimation_policy: TargetAfEstimationPolicy,
        qc_policy: FixedBasisQcPolicy,
        artifact_id: str,
        created_at: str,
        control_review: TargetControlReviewResponse,
        roi_reviewer_id: str,
        software_versions: tuple[VersionRecord, ...],
        policy_versions: tuple[VersionRecord, ...],
    ) -> ReferenceServiceAdvance:
        """Compose a control-derived target-AF proposal from reviewed inputs."""

        if type(control_review) is not TargetControlReviewResponse:
            raise TypeError("target-AF control review must be exact typed response")
        control_reviewer_id = control_review.reviewer_id

        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        retrying = current.content_sha256 != expected_content_sha256
        if retrying and not any(
            item.source_checkpoint_sha256 == expected_content_sha256
            for item in current.transition_log
        ):
            raise ReferenceCheckpointConflictError(
                "target-AF proposal checkpoint expectation is stale"
            )
        if (
            current.request is None
            or current.request.checkpoint.value not in {"target_af", "target_af_control_review"}
            or current.accepted_library is None
            or current.basis_source
            not in {
                BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY.value,
                BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY.value,
            }
        ):
            raise ValueError("control target AF requires a live accepted-library request")
        library_stored = self._artifacts.load_pointer(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="accepted_library",
            subject_id="library",
            pointer=current.accepted_library,
        )
        library = library_stored.record
        if (
            type(library) is not FluorophoreReferenceLibrary
            or library.review.decision != "accepted"
        ):
            raise ValueError("target-AF service requires an authoritative accepted library")
        stored_inputs = self._artifacts._load_all_canonical_inputs(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
        )
        by_id = {item.record.input_id: item for item in stored_inputs}
        if len(by_id) != len(stored_inputs):
            raise ReferenceCheckpointConflictError("canonical target-AF inputs are ambiguous")
        targets = tuple(item for item in stored_inputs if item.record.input_role == "target_image")
        if len(targets) != 1:
            raise ValueError("target-AF service requires one canonical target")
        target_stored = targets[0]
        target = target_stored.record
        if target.evidence.structural_signature is None:
            raise ValueError("control target-AF requires target imaging-settings compatibility profile")
        if control_input_id is None:
            selection_kind = "reuse_reference_unstained_control"
            if current.available_reference_control is None:
                raise ValueError("workflow has no accepted shared unstained control")
            controls = tuple(
                item
                for item in stored_inputs
                if item.record.input_role == "unstained_control"
                and item.record.evidence.source_image_sha256
                == current.available_reference_control.artifact_sha256
                and item.content_sha256 == current.available_reference_control.artifact_id
            )
            if len(controls) != 1:
                raise ReferenceCheckpointConflictError(
                    "accepted shared control does not resolve exactly"
                )
            control_stored = controls[0]
            control_input_id = control_stored.record.input_id
            workflow_control_pointer = current.available_reference_control
        else:
            selection_kind = "select_target_unstained_control"
            if control_input_id not in by_id:
                raise ValueError("selected target blank is not registered")
            control_stored = by_id[control_input_id]
            if control_stored.record.input_role != "target_unstained_control":
                raise ValueError("selected target blank has the wrong canonical role")
            workflow_control_pointer = ArtifactPointer(
                "target_unstained_control_image",
                control_stored.content_sha256,
                control_stored.record.evidence.source_image_sha256,
            )
        control = control_stored.record
        control_pointer = ArtifactPointer(
            (
                "unstained_reference_image"
                if selection_kind == "reuse_reference_unstained_control"
                else "target_unstained_control_image"
            ),
            control_stored.content_sha256,
            control.evidence.source_image_sha256,
            (
                current.available_reference_control.subject_id
                if selection_kind == "reuse_reference_unstained_control"
                and current.available_reference_control is not None
                else None
            ),
        )
        structural = compare_structural_compatibility(
            reference_axis=control.evidence.axis,
            candidate_axis=target.evidence.axis,
            reference_signature=control.evidence.structural_signature,
            candidate_signature=target.evidence.structural_signature,
        )
        quantitative = compare_quantitative_transformability(
            reference_signature=control.evidence.quantitative_signature,
            candidate_signature=target.evidence.quantitative_signature,
        )
        compatibility = TargetControlCompatibilityEvidence(
            control_artifact=control_pointer,
            target_image_sha256=target.evidence.source_image_sha256,
            preparation_compatibility_decision=control_review.preparation_disposition,
            tissue_context_compatibility_decision=control_review.tissue_context_disposition,
            decision_authority_id=control_reviewer_id,
            decision_evidence_sha256=typed_checksum_sha256(
                {
                    "semantic": "service_target_control_review_v1",
                    "source_checkpoint_sha256": expected_content_sha256,
                    "selection_kind": selection_kind,
                    "target_input_authority_sha256": target.authority_sha256,
                    "control_input_authority_sha256": control.authority_sha256,
                    "reviewer_id": control_reviewer_id,
                }
            ),
            structural_report_id="service-recomputed-structural-v1",
            structural_report_sha256=structural.content_sha256,
            structural_report_accepted=structural.compatible,
            quantitative_report_id="service-recomputed-quantitative-v1",
            quantitative_report_sha256=quantitative.content_sha256,
            quantitative_report_accepted=quantitative.transformable,
            reviewer_id=control_reviewer_id,
            policy_id=control_review.policy_id,
            policy_version=control_review.policy_version,
        )
        control_binding = validate_target_control_binding(
            selection_kind=selection_kind,
            target_input=target.evidence,
            control_input=control.evidence,
            compatibility=compatibility,
            structural_report=structural,
            quantitative_report=quantitative,
        )
        shared_authorities = ()
        resolved_shared = None
        if selection_kind == "reuse_reference_unstained_control":
            if background_selections:
                raise ValueError("shared target-AF control reuses accepted background evidence")
            resolved_shared = self._resolve_live_shared_target_af_evidence(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                control=control,
                roi_export=roi_export,
                af_selections=af_selections,
                sampling_policy=sampling_policy,
                background_policy=background_policy,
            )
            _, _, shared_authorities = resolved_shared
        plan = TargetAfSamplingPlan(
            expected_content_sha256,
            selection_kind,
            control_input_id,
            library.content_sha256,
            roi_export,
            af_selections,
            background_selections,
            roi_reviews,
            sampling_policy,
            background_policy,
            estimation_policy,
            qc_policy,
            artifact_id,
            created_at,
            roi_reviewer_id,
            software_versions,
            policy_versions,
            shared_authorities,
        )
        stored_binding = self._artifact_at_authority_or_none(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="target_control_binding",
            subject_id="target-af",
        )
        stored_plan = self._artifact_at_authority_or_none(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="target_af_sampling_plan",
            subject_id="target-af",
        )
        if stored_binding is not None and stored_binding.record != control_binding:
            raise ReferenceCheckpointConflictError(
                "target-AF control binding conflicts with persisted authority"
            )
        if stored_plan is not None and stored_plan.record != plan:
            raise ReferenceCheckpointConflictError(
                "target-AF proposal inputs conflict with persisted authority"
            )
        if stored_plan is not None:
            stored_bundle = self._artifact_at_authority_or_none(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="target_af_sample_bundle",
                subject_id="target-af",
            )
            stored_background = self._artifact_at_authority_or_none(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="target_af_background",
                subject_id="target-af",
            )
            if stored_bundle is None or stored_background is None:
                raise ReferenceCheckpointConflictError(
                    "persisted target-AF sampling transaction is incomplete"
                )
            bundle = stored_bundle.record
            background = stored_background.record
            if bundle.plan != plan:
                raise ReferenceCheckpointConflictError(
                    "persisted target-AF samples differ from the exact plan"
                )
            af_samples = bundle.samples[: len(plan.af_selections)]
            if selection_kind == "reuse_reference_unstained_control":
                assert resolved_shared is not None
                shared_samples, backgrounds, _ = resolved_shared
                if af_samples != shared_samples or background != backgrounds[0]:
                    raise ReferenceCheckpointConflictError(
                        "persisted shared target-AF evidence differs from live Task-5/6 authority"
                    )
            else:
                backgrounds = tuple(background for _ in af_samples)
        elif retrying:
            raise ReferenceCheckpointConflictError(
                "target-AF retry lacks its persisted sampling authority"
            )
        elif selection_kind == "reuse_reference_unstained_control":
            assert resolved_shared is not None
            af_samples, backgrounds, _ = resolved_shared
            background = backgrounds[0]
            bundle = TargetAfSampleBundle(plan, af_samples)
        else:
            bundle = sample_target_af_plan(
                self._sampling_backend(),
                self._require_canonical_target(run_id, start_authority_sha256),
                control,
                plan,
            )
            af_samples = bundle.samples[: len(plan.af_selections)]
            background_samples = bundle.samples[len(plan.af_selections) :]
            background = resolve_background_model(
                af_samples[0],
                af_samples[0],
                background_samples,
                background_samples,
                (),
                None,
                background_policy,
            )
            backgrounds = tuple(background for _ in af_samples)
        if background.status != "resolved":
            raise ValueError("target-AF background requires accepted sampled evidence")
        reviewed = tuple(
            ReviewedTargetAfRoiSelection(
                TargetAfRoiProposal(sample, review.component_identity),
                ProposalReviewDecision(
                    TargetAfRoiProposal(sample, review.component_identity).proposal_sha256,
                    "accepted" if review.included else "rejected",
                    roi_reviewer_id,
                    created_at,
                    1,
                ),
            )
            for sample, review in zip(af_samples, roi_reviews, strict=True)
        )
        estimation = estimate_target_af_candidates(
            selections=reviewed,
            background=background,
            policy=estimation_policy,
            backgrounds=backgrounds,
        )
        if any(item.disposition != "pass" for item in estimation.candidates):
            raise ValueError("failed target-AF candidate cannot be proposed")
        qc = evaluate_target_af_qc(estimation.components, qc_policy)
        if qc.disposition == "failed":
            raise ValueError("failed target-AF QC cannot be proposed")
        proposal = propose_target_autofluorescence_model(
            artifact_id=artifact_id,
            created_at=created_at,
            run_id=run_id,
            control_binding=control_binding,
            estimation=estimation,
            qc=qc,
            software_versions=software_versions,
            policy_versions=policy_versions,
        )
        existing_proposal = self._artifact_at_authority_or_none(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="target_af_proposal",
            subject_id="target-af",
        )
        if existing_proposal is not None and existing_proposal.record != proposal:
            raise ReferenceCheckpointConflictError(
                "derived target-AF proposal differs from persisted authority"
            )

        def persist(kind: str, subject_id: str, record):
            return self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind=kind,
                subject_id=subject_id,
                record=record,
            )

        persist("target_control_binding", "target-af", control_binding)
        persist("target_af_sampling_plan", "target-af", plan)
        persist("target_af_sample_bundle", "target-af", bundle)
        persist("target_af_background", "target-af", background)
        stored_proposal = persist("target_af_proposal", "target-af", proposal)
        proposal_pointer = ArtifactPointer(
            "proposed_target_af_qc",
            proposal.artifact_id,
            proposal.proposal_sha256,
        )
        validated_proposal = self._result_operations._validated_artifact(
            proposal_pointer,
            "qc_evidence",
            stored_proposal.content_sha256,
        )

        active = current
        if active.request.checkpoint.value == "target_af":
            if selection_kind == "select_target_unstained_control":
                validated_control = self._result_operations._validated_artifact(
                    workflow_control_pointer,
                    "canonical_ome_tiff",
                    control_stored.content_sha256,
                )
                if not active.request.proposed_artifacts:
                    control_source = propose_reference_artifacts(active, (validated_control,))
                    active, _ = self._store._compare_and_swap_service(
                        active.content_sha256,
                        control_source,
                        start_authority_sha256,
                    )
                elif active.request.proposed_artifacts != (validated_control,):
                    raise ReferenceCheckpointConflictError(
                        "target-AF target-blank proposal differs from exact authority"
                    )
            elif active.request.proposed_artifacts:
                raise ReferenceCheckpointConflictError(
                    "shared target-AF control has unexpected proposed artifacts"
                )
            if active.target_control_compatibility != compatibility:
                choice_source = bind_target_control_compatibility(active, compatibility)
                active, _ = self._store._compare_and_swap_service(
                    active.content_sha256,
                    choice_source,
                    start_authority_sha256,
                )
            assert active.request is not None
            response = configured_response(
                active.request,
                option_value=selection_kind,
                reviewer_id=control_reviewer_id,
                artifacts=(
                    (workflow_control_pointer,)
                    if selection_kind == "select_target_unstained_control"
                    else ()
                ),
            )
            review_source = advance_reference_workflow(active, response)
            active, _ = self._store._compare_and_swap_service(
                active.content_sha256,
                review_source,
                start_authority_sha256,
            )
        if (
            active.request is None
            or active.request.checkpoint.value != "target_af_control_review"
            or active.target_control_compatibility != compatibility
        ):
            raise ReferenceCheckpointConflictError(
                "target-AF retry is outside the exact review phase"
            )
        decisions = active.active_decisions
        last_reselection = max((index for index, item in enumerate(decisions)
                                if item.response.option_value == "reselect_target_af_source"), default=-1)
        control_decisions = tuple(item for item in decisions[last_reselection + 1:]
                                  if item.request.checkpoint.value == "target_af")
        if (
            len(control_decisions) != 1
            or control_decisions[0].response.option_value != selection_kind
            or control_decisions[0].response.reviewer_id != control_reviewer_id
            or control_decisions[0].response.artifacts
            != (
                (workflow_control_pointer,)
                if selection_kind == "select_target_unstained_control"
                else ()
            )
        ):
            raise ReferenceCheckpointConflictError(
                "target-AF retry control decision differs from exact authority"
            )
        if active.request.proposed_artifacts:
            if active.request.proposed_artifacts != (validated_proposal,):
                raise ReferenceCheckpointConflictError(
                    "live target-AF proposal pointer differs from persisted authority"
                )
            persisted = active
            persisted_start = start_authority_sha256
        else:
            proposed_checkpoint = propose_reference_artifacts(active, (validated_proposal,))
            persisted, persisted_start = self._store._compare_and_swap_service(
                active.content_sha256,
                proposed_checkpoint,
                start_authority_sha256,
            )
        assert persisted.request is not None
        review_data = build_target_af_review_data(
            persisted.request,
            proposal,
            workflow_checkpoint_sha256=persisted.content_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start, review_data),
        )

    def accept_control_target_af(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        reviewer_id: str,
        reviewed_at: str,
        revision: int,
    ) -> ReferenceServiceAdvance:
        """Accept and publish the exact live target-AF proposal."""

        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        phase = "source"
        review_request = current.request
        reviewed_decision = None
        if current.content_sha256 != expected_content_sha256:
            matches = tuple(
                item
                for item in current.decisions
                if item.source_checkpoint_sha256 == expected_content_sha256
                and item.request.checkpoint.value == "target_af_control_review"
                and item.response.option_value == "accept_control_target_af"
            )
            if len(matches) != 1:
                raise ReferenceCheckpointConflictError(
                    "target-AF acceptance checkpoint expectation is stale"
                )
            reviewed_decision = matches[0]
            review_request = reviewed_decision.request
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
                and current.pending_publication is not None
                and current.pending_publication.publication_kind == "target_af"
            ):
                phase = "pending"
            elif current.target_af_artifact is not None:
                phase = "published"
            else:
                raise ReferenceCheckpointConflictError(
                    "target-AF acceptance checkpoint expectation is stale"
                )
        if (
            review_request is None
            or review_request.checkpoint.value != "target_af_control_review"
            or len(review_request.proposed_artifacts) != 1
            or review_request.proposed_artifacts[0].pointer.role != "proposed_target_af_qc"
        ):
            raise ValueError("workflow is not awaiting exact control target-AF review")
        proposals = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="target_af_proposal",
            subject_id="target-af",
        )
        pointer = review_request.proposed_artifacts[0].pointer
        matching = tuple(
            item
            for item in proposals
            if item.record.artifact_id == pointer.artifact_id
            and item.record.proposal_sha256 == pointer.artifact_sha256
        )
        if len(matching) != 1:
            raise ReferenceCheckpointConflictError(
                "live target-AF proposal does not resolve exactly"
            )
        proposal = matching[0].record
        if proposal.run_id != run_id or proposal.qc.disposition == "failed":
            raise ValueError("failed or foreign target-AF proposal cannot be accepted")
        if phase == "source" and proposal.control_binding.selection_kind == "review_reference_control_groups":
            from .reference_service_af_groups import validate_retained_target_af_proposal
            validate_retained_target_af_proposal(self, current, start_authority_sha256, proposal,
                                                 source_locations=True)
        decision = ProposalReviewDecision(
            proposal.proposal_sha256,
            "accepted",
            reviewer_id,
            reviewed_at,
            revision,
        )
        artifact = build_target_autofluorescence_model(
            proposal=proposal,
            decision=decision,
        )

        def persist(kind: str, record):
            try:
                return self._artifacts.put_unique(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    authority_checkpoint_sha256=expected_content_sha256,
                    kind=kind,
                    subject_id="target-af",
                    record=record,
                )
            except ReferenceArtifactRepositoryError as exc:
                raise ReferenceCheckpointConflictError(
                    "target-AF acceptance inputs conflict"
                ) from exc

        persist("target_af_review_decision", decision)
        stored_artifact = persist("target_af_artifact", artifact)
        if reviewed_decision is not None and (
            reviewed_decision.response.reviewer_id != reviewer_id
            or reviewed_decision.response.artifacts != (pointer,)
        ):
            raise ReferenceCheckpointConflictError("target-AF acceptance retry inputs conflict")
        if phase == "published":
            if (
                current.target_af_artifact is None
                or current.target_af_artifact.artifact_id != artifact.proposal.artifact_id
                or current.target_af_artifact.artifact_sha256 != artifact.content_sha256
            ):
                raise ReferenceCheckpointConflictError(
                    "published target-AF artifact differs from exact retry"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        if phase == "source":
            assert current.request is not None
            response = configured_response(
                current.request,
                option_value="accept_control_target_af",
                reviewer_id=reviewer_id,
                artifacts=(pointer,),
            )
            pending = advance_reference_workflow(current, response)
            persisted_pending, _ = self._store._compare_and_swap_service(
                current.content_sha256,
                pending,
                start_authority_sha256,
            )
        else:
            persisted_pending = current
        assert persisted_pending.pending_publication is not None
        accepted_pointer = ArtifactPointer(
            "accepted_target_af",
            artifact.proposal.artifact_id,
            artifact.content_sha256,
        )
        published = publish_reference_artifacts(
            persisted_pending,
            (
                self._result_operations._published_artifact(
                    accepted_pointer,
                    "target_af_model",
                    persisted_pending.pending_publication,
                    stored_artifact.content_sha256,
                ),
            ),
        )
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_pending.content_sha256,
            published,
            start_authority_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def accept_control_target_af_decision(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        reviewer_id: str,
    ) -> ReferenceServiceAdvance:
        """Accept the live control-derived target AF with service-owned review fields."""

        return self.accept_control_target_af(
            run_id,
            expected_content_sha256,
            reviewer_id=reviewer_id,
            reviewed_at=_derived_service_timestamp(
                expected_content_sha256, "control-target-af-review"
            ),
            revision=1,
        )

    def _accepted_final_library(self, current, start_authority_sha256):
        if current.accepted_library is None:
            raise ValueError("final library basis requires accepted library authority")
        stored = self._artifacts.load_pointer(
            run_id=current.run_id, start_authority_sha256=start_authority_sha256,
            kind="accepted_library", subject_id="library", pointer=current.accepted_library)
        library = stored.record
        if type(library) is not FluorophoreReferenceLibrary or library.review.decision != "accepted":
            raise ReferenceCheckpointConflictError("final accepted-library pointer has invalid bytes")
        return library

    def _preview_control_final_basis(self, current, start_authority_sha256, proposal):
        from .reference_defaults import fixed_basis_review_defaults

        if (current.basis_source not in {"import_reference_library", "construct_reference_library"}
                or current.af_behavior != "retain"):
            raise ValueError("combined spectral review requires a library and pending target AF")
        target = self._require_canonical_target(current.run_id, start_authority_sha256)
        if proposal.control_binding.target_input != target.evidence:
            raise ReferenceCheckpointConflictError("pending target-AF proposal differs from the canonical target")
        library = self._accepted_final_library(current, start_authority_sha256)
        policy = _fixed_basis_qc_policy_from_mapping(
            fixed_basis_review_defaults(), purpose="reference-unmixing-final-basis-qc-policy/v1",
            policy_version=_FINAL_BASIS_QC_POLICY_VERSION)
        return preview_control_run_basis(channel_fluors=current.fluorophores,
            fluorophore_library=library, target_af_proposal=proposal, qc_policy=policy)

    def _control_final_review_source(self, current, start, expected_content_sha256, reviewer_id):
        if current.content_sha256 == expected_content_sha256:
            return current
        matches = tuple(item for item in current.decisions
                        if item.source_checkpoint_sha256 == expected_content_sha256
                        and item.response.option_value == "accept_control_target_af"
                        and item.response.reviewer_id == reviewer_id)
        if len(matches) != 1:
            raise ReferenceCheckpointConflictError("combined spectral review checkpoint expectation is stale")
        accepted_af = matches[0]
        source, _, saved_start = self._store._load_service_decision_authority(
            current.run_id, source_checkpoint_sha256=expected_content_sha256,
            decision_record_sha256=accepted_af.content_sha256)
        following = current.decisions[len(source.decisions):]
        final_phase = (
            current.status is ReferenceWorkflowStatus.COMPLETED
            or (current.request is not None and current.request.checkpoint.value == "final_run_basis")
            or (current.pending_publication is not None
                and current.pending_publication.publication_kind in {"target_af", "final_basis"})
        )
        if (saved_start != start or current.accepted_library != source.accepted_library
                or current.decisions[:len(source.decisions)] != source.decisions
                or not following or following[0] != accepted_af or len(following) > 2
                or (len(following) == 2 and (
                    following[1].response.option_value != "accept_final_run_basis"
                    or following[1].response.reviewer_id != reviewer_id))
                or not final_phase):
            raise ReferenceCheckpointConflictError("combined spectral review checkpoint expectation is stale")
        return source

    def accept_control_final_basis_decision(self, run_id, expected_content_sha256, *, reviewer_id):
        """Accept one displayed full-basis review through the existing publication steps.

        The checkpoint token binds both the accepted library and pending AF pointer.
        No accepted AF is written until their combined QC has been revalidated.
        Exact retries use the retained acceptance decisions; source revisions remain stale.
        """
        current, start = self._store._load_service_checkpoint(run_id)
        source = self._control_final_review_source(current, start, expected_content_sha256, reviewer_id)
        proposal = self._pending_target_af_proposal(source, start)
        columns, qc, _ = self._preview_control_final_basis(source, start, proposal)
        if qc.disposition == "failed":
            raise ValueError("The combined reference matrix failed its spectral-separation QC")
        target = self._require_canonical_target(run_id, start)
        displayed = self._spectral_review_fields(target, proposal.control_binding.target_input.axis, columns, qc)
        accepted_af = self.accept_control_target_af_decision(
            run_id, expected_content_sha256, reviewer_id=reviewer_id)
        final = accepted_af.snapshot.checkpoint
        if final.request is not None and final.request.checkpoint.value == "final_run_basis":
            if not final.request.proposed_artifacts:
                final = self.propose_final_run_basis(run_id, final.content_sha256,
                                                     qc_policy=qc.policy).snapshot.checkpoint
        else:
            decision = final.decisions[-1]
            if decision.response.option_value != "accept_final_run_basis":
                raise ReferenceCheckpointConflictError("combined spectral review has no final acceptance authority")
            final, _, saved_start = self._store._load_service_decision_authority(
                run_id, source_checkpoint_sha256=decision.source_checkpoint_sha256,
                decision_record_sha256=decision.content_sha256)
            if saved_start != start:
                raise ReferenceCheckpointConflictError("combined spectral review final authority differs")
        if self._spectral_review_summary(final, start) != displayed:
            raise ReferenceCheckpointConflictError("Final basis differs from the displayed spectral review")
        return self.review_final_run_basis_decision(
            run_id, final.content_sha256, decision="accepted", reviewer_id=reviewer_id)

    def respond_with_offered_ids(
        self,
        run_id: str,
        checkpoint_token: str,
        *,
        option: str,
        reviewer_id: str,
        selected_ids: tuple[tuple[str, str], ...] = (),
    ) -> ReferenceServiceAdvance:
        """Bind primitive offered IDs to the exact live interaction request."""

        if type(selected_ids) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or any(type(part) is not str or not part for part in item)
            for item in selected_ids
        ):
            raise TypeError("selected IDs must be exact (kind, ID) text pairs")
        current, _ = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 != checkpoint_token or current.request is None:
            raise ReferenceCheckpointConflictError("configured decision checkpoint token is stale")
        if (option in {"reselect_reference_rois", "revise_signal_dark_measurements"} and not selected_ids
                and current.current_fluorophore is not None):
            selected_ids = (("affected_fluorophore", current.current_fluorophore),)
        response = configured_response(
            current.request,
            option_value=option,
            reviewer_id=reviewer_id,
            selections=tuple(
                SelectedIdentifier(kind, identifier) for kind, identifier in selected_ids
            ),
        )
        return self.respond(run_id, checkpoint_token, response)

    def _compose_final_run_basis(
        self,
        *,
        run_id: str,
        source_checkpoint_sha256: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        qc_policy: FixedBasisQcPolicy,
        target: CanonicalReferenceInput,
        reviewed_proposal: FinalRunBasisProposal | None = None,
    ) -> FinalRunBasisProposal:
        """Compose QC from the already authenticated target and spectral artifacts."""
        live_final_review = (
            current.request is not None
            and current.request.checkpoint.value == "final_run_basis"
            and current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
        )
        pending_final_publication = (
            current.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
            and current.pending_publication is not None
            and current.pending_publication.publication_kind == "final_basis"
        )
        completed_final = (
            current.status is ReferenceWorkflowStatus.COMPLETED
            and current.final_basis_artifact is not None
            and current.final_basis_artifact.role == "final_run_basis"
        )
        if (
            not (live_final_review or pending_final_publication or completed_final)
            or current.target_image_sha256 != target.evidence.source_image_sha256
            or current.measurement_ids != target.evidence.axis.measurement_ids
            or (target.fluorophore_names
                and current.fluorophores != target.fluorophore_names)
        ):
            raise ValueError(
                "final-basis proposal requires the exact live canonical final checkpoint"
            )
        if current.basis_source == "curated_reference_library":
            from .curated_library import propose_curated_basis

            return propose_curated_basis(
                self._accepted_curated_source(current, start_authority_sha256),
                target, run_id, source_checkpoint_sha256, qc_policy, current.af_behavior
            )
        if current.basis_source == "shared_cycle_reference_library":
            from .shared_reference_workflow import compose_shared_cycle_basis
            return compose_shared_cycle_basis(self, current, start_authority_sha256,
                                             target, source_checkpoint_sha256, qc_policy)
        if current.basis_source == BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE.value:
            bindings = self._artifacts.load_all(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind="basis_source_decision",
                subject_id="basis-source",
            )
            choices = tuple(
                item.record
                for item in bindings
                if item.record.choice is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
                and item.record.source_checkpoint_sha256
                == self._source_choice_operations._accepted_decision(
                    current, BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
                ).source_checkpoint_sha256
            )
            if len(choices) != 1:
                raise ReferenceCheckpointConflictError(
                    "final MOTIF source decision is missing or ambiguous"
                )
            resolution = self._source_choice_operations._motif_resolution(
                current=current,
                target=target,
                binding=choices[0],
            )
            return propose_motif_final_run_basis(
                source_checkpoint_sha256=source_checkpoint_sha256,
                resolution=resolution,
                af_behavior=current.af_behavior,
                channel_fluors=current.fluorophores,
                qc_policy=qc_policy,
            )
        if current.basis_source not in {
            BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY.value,
            BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY.value,
        }:
            raise ValueError("final-basis library source is unsupported")
        library = self._accepted_final_library(current, start_authority_sha256)
        if current.af_behavior == "already_removed":
            return propose_fluorophore_final_run_basis(
                source_checkpoint_sha256=source_checkpoint_sha256, source_choice=current.basis_source,
                channel_fluors=current.fluorophores, fluorophore_library=library,
                target=target.evidence, run_id=run_id, qc_policy=qc_policy,
            )
        if current.target_af_artifact is None:
            raise ValueError("The retained-AF basis requires accepted target AF authority")
        target_records = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="target_af_artifact",
            subject_id="target-af",
        )
        target_matches = tuple(
            item
            for item in target_records
            if item.semantic_sha256 == current.target_af_artifact.artifact_sha256
            and item.record.proposal.artifact_id == current.target_af_artifact.artifact_id
        )
        if (
            len(target_matches) != 1
            or type(target_matches[0].record) is not TargetAutofluorescenceArtifact
        ):
            raise ReferenceCheckpointConflictError("final target-AF pointer has invalid bytes")
        return propose_control_final_run_basis(
            source_checkpoint_sha256=source_checkpoint_sha256,
            source_choice=current.basis_source,
            channel_fluors=current.fluorophores,
            fluorophore_library=library,
            target_af_artifact=target_matches[0].record,
            qc_policy=qc_policy,
        )

    def propose_final_run_basis_from_policy(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        qc_policy: Mapping[str, object],
    ) -> ReferenceServiceAdvance:
        """Compose the final proposal from strict primitive policy configuration."""

        return self.propose_final_run_basis(
            run_id,
            expected_content_sha256,
            qc_policy=_fixed_basis_qc_policy_from_mapping(
                qc_policy,
                purpose="reference-unmixing-final-basis-qc-policy/v1",
                policy_version=_FINAL_BASIS_QC_POLICY_VERSION,
            ),
        )

    def propose_final_run_basis(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        qc_policy: FixedBasisQcPolicy,
    ) -> ReferenceServiceAdvance:
        """Persist and expose the exact source-neutral final-basis proposal."""

        if type(qc_policy) is not FixedBasisQcPolicy:
            raise TypeError("qc_policy must be exact FixedBasisQcPolicy")
        current, start_authority_sha256, target, inputs = self._basis_choice_inputs(run_id)
        source_checkpoint_sha256 = expected_content_sha256
        if current.content_sha256 != expected_content_sha256:
            if (
                current.request is None
                or current.request.checkpoint.value != "final_run_basis"
                or current.request.prior_checkpoint_sha256 != expected_content_sha256
                or not current.request.proposed_artifacts
            ):
                raise ReferenceCheckpointConflictError(
                    "final-basis proposal checkpoint expectation is stale"
                )
            source_checkpoint_sha256 = current.request.prior_checkpoint_sha256
        proposal = self._compose_final_run_basis(
            target=target,
            run_id=run_id,
            source_checkpoint_sha256=source_checkpoint_sha256,
            current=current,
            start_authority_sha256=start_authority_sha256,
            qc_policy=qc_policy,
        )
        self._validate_reference_locations(inputs)
        try:
            stored = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=source_checkpoint_sha256,
                kind="final_basis_proposal",
                subject_id="final-basis",
                record=proposal,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError("final-basis proposal inputs conflict") from exc
        pointer = ArtifactPointer("proposed_run_basis_qc", run_id, proposal.proposal_sha256)
        validated = self._result_operations._validated_artifact(
            pointer, "qc_evidence", stored.content_sha256
        )
        if current.request is None:
            raise ReferenceCheckpointConflictError("final-basis review request is unavailable")
        if current.request.proposed_artifacts:
            if current.request.proposed_artifacts != (validated,):
                raise ReferenceCheckpointConflictError(
                    "live final-basis proposal differs from persisted authority"
                )
            persisted = current
            persisted_start = start_authority_sha256
        else:
            proposed = propose_reference_artifacts(current, (validated,))
            # put_unique has already read back and authenticated the immutable
            # proposal. No pixels or upstream decisions changed while composing
            # it; check the file observations and let CAS bind the exact head.
            self._validate_reference_locations(inputs)
            persisted, persisted_start = self._store._compare_and_swap_service(
                current.content_sha256, proposed, start_authority_sha256
            )
        assert persisted.request is not None
        review_data = build_run_basis_review_data(
            persisted.request,
            proposal,
            workflow_checkpoint_sha256=persisted.content_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start, review_data),
        )

    def review_final_run_basis_decision(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        decision: str,
        reviewer_id: str,
    ) -> ReferenceServiceAdvance:
        """Review the final proposal with service-owned time and revision."""

        return self.review_final_run_basis_for_operation(
            run_id,
            expected_content_sha256,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewed_at=_derived_service_timestamp(
                expected_content_sha256, "final-run-basis-review"
            ),
            revision=1,
        )

    def review_final_run_basis_for_operation(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        decision: str,
        reviewer_id: str,
        reviewed_at: str,
        revision: int,
    ) -> ReferenceServiceAdvance:
        """Review final authority with service-owned publication identity."""

        artifact_id = (
            "final-run-basis-"
            + hashlib.sha256((run_id + "\0" + expected_content_sha256).encode("utf-8")).hexdigest()
        )
        return self.review_final_run_basis(
            run_id,
            expected_content_sha256,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            revision=revision,
            artifact_id=artifact_id,
            created_at=reviewed_at,
        )

    def review_final_run_basis(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        decision: str,
        reviewer_id: str,
        reviewed_at: str,
        revision: int,
        artifact_id: str | None = None,
        created_at: str | None = None,
    ) -> ReferenceServiceAdvance:
        """Accept the exact final proposal and publish its authority."""

        if decision != "accepted":
            raise ValueError("final-basis review decision is unsupported")
        current, start_authority_sha256, target, inputs = self._basis_choice_inputs(run_id)
        phase = "source"
        review_request = current.request
        decision_record = None
        if current.content_sha256 != expected_content_sha256:
            matches = tuple(
                item
                for item in current.decisions
                if item.source_checkpoint_sha256 == expected_content_sha256
                and item.request.checkpoint.value == "final_run_basis"
            )
            if len(matches) != 1:
                raise ReferenceCheckpointConflictError(
                    "final-basis review checkpoint expectation is stale"
                )
            decision_record = matches[0]
            review_request = decision_record.request
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
                and current.pending_publication is not None
                and current.pending_publication.publication_kind == "final_basis"
            ):
                phase = "pending"
            elif current.final_basis_artifact is not None:
                phase = "published"
            else:
                raise ReferenceCheckpointConflictError(
                    "final-basis review checkpoint expectation is stale"
                )
        if (
            review_request is None
            or review_request.checkpoint.value != "final_run_basis"
            or len(review_request.proposed_artifacts) != 1
            or review_request.proposed_artifacts[0].pointer.role != "proposed_run_basis_qc"
        ):
            raise ValueError("workflow is not awaiting exact final-basis review")
        pointer = review_request.proposed_artifacts[0].pointer
        proposals = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="final_basis_proposal",
            subject_id="final-basis",
        )
        matches = tuple(
            item
            for item in proposals
            if item.record.run_id == run_id
            and item.record.proposal_sha256 == pointer.artifact_sha256
        )
        if len(matches) != 1 or type(matches[0].record) is not FinalRunBasisProposal:
            raise ReferenceCheckpointConflictError(
                "live final-basis proposal does not resolve exactly"
            )
        proposal = matches[0].record
        recomposed = self._compose_final_run_basis(
            target=target,
            run_id=run_id,
            source_checkpoint_sha256=proposal.source_checkpoint_sha256,
            current=current,
            start_authority_sha256=start_authority_sha256,
            qc_policy=proposal.qc_policy,
            reviewed_proposal=proposal,
        )
        if recomposed != proposal:
            raise ReferenceCheckpointConflictError(
                "live final-basis upstream authority differs from stored proposal"
            )
        biomarkers = self._component_biomarker_defaults(target, proposal.columns)
        review = ProposalReviewDecision(
            proposal.proposal_sha256,
            decision,
            reviewer_id,
            reviewed_at,
            revision,
        )
        self._validate_reference_locations(inputs)
        try:
            self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="final_basis_review_decision",
                subject_id="final-basis",
                record=review,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError("final-basis review inputs conflict") from exc
        if artifact_id is None or created_at is None:
            raise ValueError("accepted final-basis review requires artifact_id and created_at")

        artifact = accept_final_run_basis(
            proposal=proposal,
            decision=review,
            artifact_id=artifact_id,
            created_at=created_at,
            biomarkers=biomarkers,
        )
        handoff = FinalRunBasisRuntimeHandoff(
            run_id=run_id,
            source_checkpoint_sha256=proposal.source_checkpoint_sha256,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.content_sha256,
            proposal_sha256=proposal.proposal_sha256,
        )
        self._validate_reference_locations(inputs)
        try:
            stored_artifact = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="final_basis_artifact",
                subject_id="final-basis",
                record=artifact,
            )
            stored_handoff = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="final_basis_runtime_handoff",
                subject_id="final-basis",
                record=handoff,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "final-basis publication inputs conflict"
            ) from exc
        if stored_artifact.record != artifact or stored_handoff.record != handoff:
            raise ReferenceCheckpointConflictError(
                "persisted final-basis publication authority differs"
            )
        handoff.validate_artifact(artifact)
        if decision_record is not None and (
            decision_record.response.option_value != "accept_final_run_basis"
            or decision_record.response.reviewer_id != reviewer_id
            or decision_record.response.artifacts != (pointer,)
        ):
            raise ReferenceCheckpointConflictError("final-basis acceptance retry inputs conflict")
        if phase == "published":
            if (
                current.final_basis_artifact is None
                or current.final_basis_artifact.role != "final_run_basis"
                or current.final_basis_artifact.artifact_id != artifact.artifact_id
                or current.final_basis_artifact.artifact_sha256 != artifact.content_sha256
            ):
                raise ReferenceCheckpointConflictError(
                    "published final-basis artifact differs from exact retry"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        if phase == "source":
            assert current.request is not None
            response = configured_response(
                current.request,
                option_value="accept_final_run_basis",
                reviewer_id=reviewer_id,
                artifacts=(pointer,),
            )
            pending = advance_reference_workflow(current, response)
            self._validate_reference_locations(inputs)
            persisted_pending, _ = self._store._compare_and_swap_service(
                current.content_sha256, pending, start_authority_sha256
            )
        else:
            persisted_pending = current
        assert persisted_pending.pending_publication is not None

        final_pointer = ArtifactPointer(
            "final_run_basis", artifact.artifact_id, artifact.content_sha256
        )
        published = publish_reference_artifacts(
            persisted_pending,
            (
                self._result_operations._published_artifact(
                    final_pointer,
                    "final_run_basis",
                    persisted_pending.pending_publication,
                    stored_artifact.content_sha256,
                ),
            ),
        )
        self._validate_reference_locations(inputs)
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_pending.content_sha256,
            published,
            start_authority_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def load_final_basis_application(self, run_id: str) -> ResolvedBasisApplication:
        """Authenticate the completed basis and its recorded source observations.

        The numerical reader checks full source bytes, metadata and geometry
        before reading pixels or creating output.
        """

        current, start_authority_sha256, target, inputs = self._basis_choice_inputs(run_id)
        pointer = current.final_basis_artifact
        if (
            current.status is not ReferenceWorkflowStatus.COMPLETED
            or pointer is None
            or pointer.role != "final_run_basis"
        ):
            raise ValueError("workflow has no accepted final-basis authority")
        artifacts = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="final_basis_artifact",
            subject_id="final-basis",
        )
        matches = tuple(
            item.record
            for item in artifacts
            if item.record.artifact_id == pointer.artifact_id
            and item.record.content_sha256 == pointer.artifact_sha256
        )
        handoffs = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="final_basis_runtime_handoff",
            subject_id="final-basis",
        )
        matched_handoffs = tuple(
            item.record
            for item in handoffs
            if item.record.artifact_id == pointer.artifact_id
            and item.record.artifact_sha256 == pointer.artifact_sha256
        )
        if (
            len(matches) != 1
            or type(matches[0]) is not FinalRunBasisArtifact
            or len(matched_handoffs) != 1
            or type(matched_handoffs[0]) is not FinalRunBasisRuntimeHandoff
        ):
            raise ReferenceCheckpointConflictError(
                "completed final-basis authority is missing or ambiguous"
            )
        # Recompose from live upstream bytes before allowing the runtime handoff.
        recomposed = self._compose_final_run_basis(
            target=target,
            run_id=run_id,
            source_checkpoint_sha256=matches[0].proposal.source_checkpoint_sha256,
            current=current,
            start_authority_sha256=start_authority_sha256,
            qc_policy=matches[0].proposal.qc_policy,
            reviewed_proposal=matches[0].proposal,
        )
        if recomposed != matches[0].proposal:
            raise ReferenceCheckpointConflictError("completed final-basis source authority changed")
        self._validate_reference_locations(inputs)
        return ResolvedBasisApplication._from_service_final_authority(
            matches[0],
            matched_handoffs[0],
            service_authority_proof=_FINAL_APPLICATION_SERVICE_AUTHORITY,
        )

    def advance_reference_purification_from_configuration(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        reviewer_id: str,
        roi_export: ReferenceRoiExport | None = None,
        selections: tuple[AcceptedReferenceRoiSelection, ...] = (),
        pairings: tuple[ReferenceRoiPairingSelection, ...] = (),
        sampling_policy: Mapping[str, object] | None = None,
        background_policy: Mapping[str, object] | None = None,
        signal_dark_selections: tuple[SelectedIdentifier, ...] = (),
        purification_policy: Mapping[str, object] | None = None,
        extraction_dispositions: tuple[SelectedIdentifier, ...] = (),
        af_control_set_review_sha256s: tuple[str, ...] = (),
    ) -> ReferenceServiceAdvance:
        """Advance using primitive thresholds and service-owned policy identity."""

        return self.advance_reference_purification(
            run_id,
            expected_content_sha256,
            reviewer_id=reviewer_id,
            roi_export=roi_export,
            selections=selections,
            pairings=pairings,
            sampling_policy=(
                None
                if sampling_policy is None
                else _purification_sampling_policy_from_mapping(sampling_policy)
            ),
            background_policy=(
                None
                if background_policy is None
                else _background_policy_from_mapping(background_policy)
            ),
            signal_dark_selections=signal_dark_selections,
            purification_policy=(
                None
                if purification_policy is None
                else _purification_policy_from_mapping(purification_policy)
            ),
            extraction_dispositions=extraction_dispositions,
            af_control_set_review_sha256s=af_control_set_review_sha256s,
        )

    def advance_reference_purification(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        reviewer_id: str,
        roi_export: ReferenceRoiExport | None = None,
        selections: tuple[AcceptedReferenceRoiSelection, ...] = (),
        pairings: tuple[ReferenceRoiPairingSelection, ...] = (),
        sampling_policy: CompleteVectorQcPolicy | None = None,
        background_policy: BackgroundEquivalencePolicy | None = None,
        signal_dark_selections: tuple[SelectedIdentifier, ...] = (),
        purification_policy: PurificationPolicy | None = None,
        extraction_dispositions: tuple[SelectedIdentifier, ...] = (),
        af_control_set_review_sha256s: tuple[str, ...] = (),
    ) -> ReferenceServiceAdvance:
        """Advance the live constructed-reference role from caller review inputs."""

        collections = {
            "selections": selections,
            "pairings": pairings,
            "signal_dark_selections": signal_dark_selections,
            "extraction_dispositions": extraction_dispositions,
            "af_control_set_review_sha256s": af_control_set_review_sha256s,
        }
        if any(type(value) is not tuple for value in collections.values()):
            raise TypeError("reference purification collections must be exact tuples")
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        from .reference_workflow import _uses_source_preparation
        negative_fallback_policy = (
            NegativeFallbackReviewPolicy(
                policy_version=_NEGATIVE_FALLBACK_POLICY_VERSION,
                negativity_scale_floor=background_policy.scale_floor,
            )
            if current.control_strategy == "per_stain_negative_controls"
            and not _uses_source_preparation(current.active_decisions)
            and type(background_policy) is BackgroundEquivalencePolicy
            else None
        )
        if current.content_sha256 != expected_content_sha256:
            recovered = self._recover_reference_purification_retry(
                af_control_set_review_sha256s=af_control_set_review_sha256s,
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                expected_content_sha256=expected_content_sha256,
                reviewer_id=reviewer_id,
                roi_export=roi_export,
                selections=selections,
                pairings=pairings,
                sampling_policy=sampling_policy,
                background_policy=background_policy,
                signal_dark_selections=signal_dark_selections,
                negative_fallback_policy=negative_fallback_policy,
                purification_policy=purification_policy,
                extraction_dispositions=extraction_dispositions,
            )
            if recovered is not None:
                return recovered
            raise ReferenceCheckpointConflictError(
                "reference purification checkpoint expectation is stale"
            )
        if (
            current.basis_source != BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY.value
            or current.request is None
            or current.current_fluorophore is None
        ):
            raise ValueError("reference purification requires a live constructed-library request")
        checkpoint = current.request.checkpoint.value
        if af_control_set_review_sha256s and checkpoint != "signal_dark_measurements":
            raise ValueError("AF control-set review is only accepted before extraction")
        allowed = {
            "positive_reference_rois",
            "af_reference_rois",
            "negative_reference_rois",
            "background_reference_rois",
            "signal_dark_measurements",
            "extraction_qc",
        }
        if checkpoint not in allowed:
            raise ValueError("live request is outside the ROI purification service slice")
        if checkpoint in {"af_reference_rois", "negative_reference_rois"}:
            return self._accept_control_roi_phase(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                reviewer_id=reviewer_id,
                roi_export=roi_export,
                selections=selections,
                pairings=pairings,
                sampling_policy=sampling_policy,
                background_policy=background_policy,
                signal_dark_selections=signal_dark_selections,
                negative_fallback_policy=negative_fallback_policy,
                purification_policy=purification_policy,
                extraction_dispositions=extraction_dispositions,
            )
        if checkpoint == "background_reference_rois":
            return self._accept_background_roi_phase(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                reviewer_id=reviewer_id,
                roi_export=roi_export,
                selections=selections,
                pairings=pairings,
                sampling_policy=sampling_policy,
                background_policy=background_policy,
                signal_dark_selections=signal_dark_selections,
                negative_fallback_policy=negative_fallback_policy,
                purification_policy=purification_policy,
                extraction_dispositions=extraction_dispositions,
            )
        if checkpoint == "signal_dark_measurements":
            return self._accept_signal_dark_phase(
                af_control_set_review_sha256s=af_control_set_review_sha256s,
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                reviewer_id=reviewer_id,
                roi_export=roi_export,
                selections=selections,
                pairings=pairings,
                sampling_policy=sampling_policy,
                background_policy=background_policy,
                signal_dark_selections=signal_dark_selections,
                negative_fallback_policy=negative_fallback_policy,
                purification_policy=purification_policy,
                extraction_dispositions=extraction_dispositions,
            )
        if checkpoint == "extraction_qc":
            if (
                roi_export is not None
                or selections
                or pairings
                or sampling_policy is not None
                or background_policy is not None
                or signal_dark_selections
                or negative_fallback_policy is not None
                or purification_policy is not None
                or type(extraction_dispositions) is not tuple
                or not extraction_dispositions
            ):
                raise ValueError("extraction review requires only exact reviewer dispositions")
            stored = self._live_roi_phase_artifact(
                current,
                start_authority_sha256,
                "purifications",
                current.current_fluorophore,
                "signal_dark_measurements",
            )
            return self._result_operations.accept_extraction(
                run_id,
                current.content_sha256,
                stored.record,
                extraction_dispositions,
                reviewer_id=reviewer_id,
            )
        if (
            type(roi_export) is not ReferenceRoiExport
            or type(selections) is not tuple
            or not selections
            or type(sampling_policy) is not CompleteVectorQcPolicy
        ):
            raise TypeError(
                "positive ROI review requires exact export, selections, and sampling policy"
            )
        if (
            pairings
            or background_policy is not None
            or signal_dark_selections
            or negative_fallback_policy is not None
            or purification_policy is not None
            or extraction_dispositions
        ):
            raise ValueError("positive ROI review received irrelevant checkpoint inputs")
        fluorophore = current.current_fluorophore
        if any(
            item.roi.roi_role != "positive" or item.roi.role_link_id != fluorophore
            for item in selections
        ):
            raise ValueError("positive ROI selections differ from the live fluorophore")
        controls = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind="reference_control_files",
            subject_id="reference-control-files",
        )
        if len(controls) != 1:
            raise ReferenceCheckpointConflictError(
                "constructed reference/control authority is ambiguous"
            )
        file_binding = controls[0].record
        panel_index = current.fluorophores.index(fluorophore)
        expected_input_id = file_binding.positive_reference_input_ids[panel_index]
        if any(item.entry.image.reference_input_id != expected_input_id for item in selections):
            raise ValueError("positive ROI selection uses an unaccepted reference input")
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256) if file_binding.prepare_shared_control else None
        if file_binding.prepare_shared_control and prepared is None:
            raise ValueError("Accept the AF preparation before preparing stained references")
        plan = ReferenceRoiSelectionPlan.create(
            source_checkpoint_sha256=current.content_sha256,
            checkpoint=checkpoint,
            fluorophore=fluorophore,
            export=roi_export,
            selections=selections,
            pairings=(),
            sampling_policy=sampling_policy,
            reviewer_id=reviewer_id,
            source_preparation_sha256=None if prepared is None else prepared.content_sha256,
        )
        stored_plan, _, _ = self._persist_roi_plan_and_samples(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            current=current,
            plan=plan,
        )
        pointer = ArtifactPointer(
            "positive_reference_rois",
            stored_plan.content_sha256,
            roi_export.content_sha256,
            fluorophore,
        )
        proposed = propose_reference_artifacts(
            current,
            (
                self._result_operations._validated_artifact(
                    pointer, "qupath_roi_geometry", stored_plan.content_sha256
                ),
            ),
        )
        assert proposed.request is not None
        response = configured_response(
            proposed.request,
            option_value="accept_positive_reference_rois",
            reviewer_id=reviewer_id,
            artifacts=(pointer,),
        )
        result = advance_reference_workflow(proposed, response)
        persisted_proposal, _ = self._store._compare_and_swap_service(
            current.content_sha256, proposed, start_authority_sha256
        )
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_proposal.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def _recover_reference_purification_retry(
        self,
        *,
        run_id,
        current,
        start_authority_sha256,
        expected_content_sha256,
        reviewer_id,
        roi_export,
        selections,
        pairings,
        sampling_policy,
        background_policy,
        signal_dark_selections,
        negative_fallback_policy,
        purification_policy,
        extraction_dispositions,
        af_control_set_review_sha256s,
    ) -> ReferenceServiceAdvance | None:
        superseded = {identity for transition in current.transition_log
                      for identity in transition.superseded_request_ids}
        active_decisions = tuple(item for item in current.active_decisions
                                 if item.request.request_id not in superseded)
        for index in range(len(active_decisions) - 1, -1, -1):
            if (active_decisions[index].response.option_value == "revise_shared_control"
                    and (current.control_strategy == "shared_unstained_control" or active_decisions[index].request.fluorophore is None
                         or active_decisions[index].request.fluorophore == current.current_fluorophore)):
                active_decisions = active_decisions[index + 1:]
                break
        proposal = (
            current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
            and current.request is not None
            and current.request.checkpoint.value
            in {
                "positive_reference_rois",
                "af_reference_rois",
                "negative_reference_rois",
                "background_reference_rois",
                "extraction_qc",
            }
            and current.request.prior_checkpoint_sha256 == expected_content_sha256
        )
        decision = next(
            (
                item
                for item in reversed(active_decisions)
                if item.request.checkpoint.value
                in {
                    "positive_reference_rois",
                    "af_reference_rois",
                    "negative_reference_rois",
                    "background_reference_rois",
                    "signal_dark_measurements",
                    "extraction_qc",
                }
                and (
                    item.request.prior_checkpoint_sha256 == expected_content_sha256
                    or item.source_checkpoint_sha256 == expected_content_sha256
                )
            ),
            None,
        )
        if not proposal and decision is None:
            return None
        checkpoint = (
            decision.request.checkpoint.value
            if decision is not None
            else current.request.checkpoint.value
        )
        fluorophore = (
            decision.request.fluorophore if decision is not None else current.request.fluorophore
        )
        if checkpoint == "extraction_qc":
            if (
                roi_export is not None
                or selections
                or pairings
                or sampling_policy is not None
                or background_policy is not None
                or signal_dark_selections
                or negative_fallback_policy is not None
                or purification_policy is not None
                or not extraction_dispositions
            ):
                raise ReferenceCheckpointConflictError("extraction retry operation inputs differ")
            purifications = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="purifications",
                subject_id=fluorophore,
            ).record
            return self._result_operations.accept_extraction(
                run_id,
                expected_content_sha256,
                purifications,
                extraction_dispositions,
                reviewer_id=reviewer_id,
            )
        if checkpoint == "signal_dark_measurements":
            if (
                roi_export is not None
                or selections
                or pairings
                or sampling_policy is not None
                or background_policy is not None
                or negative_fallback_policy is not None
                or extraction_dispositions
            ):
                raise ReferenceCheckpointConflictError("signal/dark retry operation inputs differ")
            operation = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="signal_purification_operation",
                subject_id=fluorophore,
            ).record
            if not af_control_set_review_sha256s:
                control = self._live_roi_phase_artifact(current, start_authority_sha256,
                    "roi_selection_plan", fluorophore, "af_reference_rois").record if current.control_strategy == "shared_unstained_control" else None
                if control is not None and control.source_preparation_sha256 is not None:
                    af_control_set_review_sha256s = operation.af_control_set_review_sha256s
            if (
                operation.reviewer_id != reviewer_id
                or operation.selections != signal_dark_selections
                or operation.purification_policy != purification_policy
                or operation.af_control_set_review_sha256s != af_control_set_review_sha256s
            ):
                raise ReferenceCheckpointConflictError("signal/dark retry operation inputs differ")
            purifications = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="purifications",
                subject_id=fluorophore,
            ).record
            evidence = extraction_request_evidence(purifications)
            if (
                current.request is not None
                and current.request.checkpoint.value == "extraction_qc"
                and not current.request.evidence
            ):
                extraction = propose_reference_evidence(current, evidence)
                persisted, persisted_start = self._store._compare_and_swap_service(
                    current.content_sha256, extraction, start_authority_sha256
                )
                return ReferenceServiceAdvance(
                    ReferenceServiceAdvanceStatus.PERSISTED,
                    ReferenceServiceSnapshot(persisted, persisted_start),
                )
            if (
                current.request is not None
                and current.request.checkpoint.value == "extraction_qc"
                and current.request.evidence != evidence
            ):
                raise ReferenceCheckpointConflictError(
                    "signal/dark retry purification evidence differs"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        stored = self._artifacts.load(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="roi_selection_plan",
            subject_id=f"{fluorophore}:{checkpoint}",
        ).record
        if (
            stored.reviewer_id != reviewer_id
            or stored.export != roi_export
            or stored.selections != selections
            or stored.pairings != pairings
            or stored.sampling_policy != sampling_policy
            or signal_dark_selections
            or purification_policy is not None
            or extraction_dispositions
        ):
            raise ReferenceCheckpointConflictError("ROI retry operation inputs differ")
        if checkpoint == "background_reference_rois":
            operation = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="background_operation",
                subject_id=fluorophore,
            ).record
            if (
                operation.policy != background_policy
                or operation.negative_fallback_policy != negative_fallback_policy
            ):
                raise ReferenceCheckpointConflictError("background retry operation inputs differ")
        elif background_policy is not None or negative_fallback_policy is not None:
            raise ReferenceCheckpointConflictError("ROI retry operation inputs differ")
        if decision is not None:
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        assert current.request is not None
        pointers = tuple(item.pointer for item in current.request.proposed_artifacts)
        response = configured_response(
            current.request,
            option_value=f"accept_{checkpoint}",
            reviewer_id=reviewer_id,
            artifacts=pointers,
        )
        next_evidence = ()
        if (
            checkpoint == "background_reference_rois"
            and current.control_strategy == "per_stain_negative_controls"
            and not any(item.response.option_value == "accept_reference_control_files_with_source_preparation"
                        for item in current.active_decisions)
        ):
            proposals = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="negative_fallback_proposals",
                subject_id=fluorophore,
            ).record
            next_evidence = tuple(
                negative_fallback_review_request_evidence(item) for item in proposals
            )
        result = advance_reference_workflow(current, response, next_evidence=next_evidence)
        persisted, persisted_start = self._store._compare_and_swap_service(
            current.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def _one_artifact(
        self,
        run_id: str,
        start_authority_sha256: str,
        kind: str,
        subject_id: str,
    ):
        values = self._artifacts.load_all(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            kind=kind,
            subject_id=subject_id,
        )
        if len(values) != 1:
            raise ReferenceCheckpointConflictError(
                f"{kind} authority for {subject_id!r} is missing or ambiguous"
            )
        return values[0]

    def _canonical_inventory(self, run_id: str, start_authority_sha256: str):
        return tuple(
            item.record
            for item in self._artifacts._load_all_canonical_inputs(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
            )
        )

    def _artifact_at_authority_or_none(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        kind: str,
        subject_id: str,
    ):
        try:
            return self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=authority_checkpoint_sha256,
                kind=kind,
                subject_id=subject_id,
            )
        except ReferenceArtifactRepositoryError as exc:
            if str(exc) != "reference scientific artifact is missing or ambiguous":
                raise
        try:
            history = self._artifacts.load_all(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind=kind,
                subject_id=subject_id,
            )
        except ReferenceArtifactRepositoryError as exc:
            if str(exc) == "reference scientific artifact is missing":
                return None
            raise
        matching = tuple(
            item
            for item in history
            if item.authority_checkpoint_sha256 == authority_checkpoint_sha256
        )
        if matching:
            raise ReferenceCheckpointConflictError(
                f"{kind} authority for {subject_id!r} is ambiguous"
            )
        return None

    def _live_roi_phase_artifact(
        self,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        kind: str,
        fluorophore: str,
        checkpoint: str,
        *,
        accepted_source=None,
    ):
        _, authority = self._active_roi_decision_authority(
            run_id=current.run_id,
            current=current,
            start_authority_sha256=start_authority_sha256,
            checkpoint=checkpoint,
            fluorophore=fluorophore,
            option_value=f"accept_{checkpoint}",
            artifact_role=None if checkpoint == "signal_dark_measurements" else checkpoint,
        )
        subject = (
            f"{fluorophore}:{checkpoint}"
            if kind in {"roi_selection_plan", "sampled_roi_bundle"}
            else fluorophore
        )
        return self._load_roi_artifact(
            current, start_authority_sha256, authority, kind, subject, accepted_source=accepted_source)

    def _load_roi_artifact(self, current, start, authority, kind, subject, *, accepted_source=None):
        if kind == "sampled_roi_bundle":
            from .reference_service_control_preparation import load_sample_use
            reused = load_sample_use(self, current, start, authority, subject, accepted_source=accepted_source)
            if reused is not None:
                return reused
        return self._artifacts.load(
            run_id=current.run_id,
            start_authority_sha256=start,
            authority_checkpoint_sha256=authority,
            kind=kind,
            subject_id=subject,
        )

    def _active_roi_decision_authority(
        self,
        *,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        checkpoint: str,
        fluorophore: str,
        option_value: str,
        artifact_role: str | None,
    ) -> tuple[ReferenceDecisionRecord, str]:
        superseded = {
            request_id
            for transition in current.transition_log
            for request_id in transition.superseded_request_ids
        }
        decisions = current.active_decisions
        for index in range(len(decisions) - 1, -1, -1):
            response = decisions[index].response
            resets_phase = response.option_value == "reselect_reference_rois" or (
                response.option_value == "revise_signal_dark_measurements"
                and checkpoint == "signal_dark_measurements"
            )
            if (response.option_value == "revise_shared_control" and (current.control_strategy == "shared_unstained_control" or decisions[index].request.fluorophore is None
                    or decisions[index].request.fluorophore == fluorophore)) or resets_phase and any(
                item.role == "affected_fluorophore" and item.identifier == fluorophore
                for item in response.selections
            ):
                decisions = decisions[index + 1 :]
                break
        matches = tuple(
            item
            for item in decisions
            if item.request.request_id not in superseded
            and item.request.checkpoint.value == checkpoint
            and item.request.fluorophore == fluorophore
            and item.response.option_value == option_value
            and (
                not item.response.artifacts
                if artifact_role is None
                else len(item.response.artifacts) == 1
                and item.response.artifacts[0].role == artifact_role
            )
        )
        if len(matches) != 1:
            raise ReferenceCheckpointConflictError(
                f"live {checkpoint} decision authority is missing or ambiguous"
            )
        decision = matches[0]
        source, result, loaded_start = self._store._load_service_decision_authority(
            run_id,
            source_checkpoint_sha256=decision.source_checkpoint_sha256,
            decision_record_sha256=decision.content_sha256,
        )
        if (
            loaded_start != start_authority_sha256
            or source.request != decision.request
            or not result.decisions
            or result.decisions[-1] != decision
            or (artifact_role is not None and source.prior_checkpoint_sha256 is None)
        ):
            raise ReferenceCheckpointConflictError(f"live {checkpoint} decision lineage differs")
        # ROI plans precede the artifact proposal; signal/dark results are
        # recorded directly against the accepted decision's source checkpoint.
        authority = (
            source.content_sha256 if artifact_role is None else source.prior_checkpoint_sha256
        )
        assert authority is not None
        return decision, authority

    def _resolve_live_shared_target_af_evidence(
        self,
        *,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        control: CanonicalReferenceInput,
        roi_export: ReferenceRoiExport,
        af_selections: tuple[AcceptedReferenceRoiSelection, ...],
        sampling_policy: CompleteVectorQcPolicy,
        background_policy: BackgroundEquivalencePolicy,
    ) -> tuple[
        tuple[SampledReferenceRoi, ...],
        tuple[BackgroundModelResolution, ...],
        tuple[TargetAfSharedRoiAuthority, ...],
    ]:
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256)
        selected_remaining = list(af_selections)
        selected_samples = []
        selected_resolutions = []
        authorities = []
        for fluorophore in current.fluorophores:
            af_decision, af_authority = self._active_roi_decision_authority(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                checkpoint="af_reference_rois",
                fluorophore=fluorophore,
                option_value="accept_af_reference_rois",
                artifact_role="af_reference_rois",
            )
            af_plan_stored = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=af_authority,
                kind="roi_selection_plan",
                subject_id=f"{fluorophore}:af_reference_rois",
            )
            af_bundle_stored = self._load_roi_artifact(current, start_authority_sha256,
                af_authority, "sampled_roi_bundle", f"{fluorophore}:af_reference_rois")
            af_plan = af_plan_stored.record
            pointer = af_decision.response.artifacts[0]
            if (
                af_bundle_stored.record.plan != af_plan
                or pointer.artifact_id != af_plan_stored.content_sha256
                or pointer.artifact_sha256 != af_plan.export.content_sha256
                or pointer.subject_id != "shared"
            ):
                raise ReferenceCheckpointConflictError(
                    "live Task-5 AF plan/bundle pointer lineage differs"
                )
            group = tuple(item for item in selected_remaining if item in af_plan.selections)
            if not group:
                continue
            if (
                group != af_plan.selections
                or af_plan.export != roi_export
                or af_plan.sampling_policy != sampling_policy
            ):
                raise ReferenceCheckpointConflictError(
                    "live Task-5 sampling authority differs from target-AF request"
                )
            for item in group:
                selected_remaining.remove(item)
            samples_by_selection = {
                item.selection: item for item in af_bundle_stored.record.samples
            }
            if len(samples_by_selection) != len(af_bundle_stored.record.samples):
                raise ReferenceCheckpointConflictError(
                    "live Task-5 AF sample authority is ambiguous"
                )
            group_samples = tuple(samples_by_selection[item] for item in group)
            if any(item.input_evidence != control.evidence for item in group_samples):
                raise ReferenceCheckpointConflictError(
                    "live Task-5 AF samples bind a foreign shared control"
                )

            background_decision, background_authority = self._active_roi_decision_authority(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                checkpoint="background_reference_rois",
                fluorophore=fluorophore,
                option_value="accept_background_reference_rois",
                artifact_role="background_reference_rois",
            )
            background_plan_stored = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=background_authority,
                kind="roi_selection_plan",
                subject_id=f"{fluorophore}:background_reference_rois",
            )
            background_bundle_stored = self._load_roi_artifact(current, start_authority_sha256,
                background_authority, "sampled_roi_bundle", f"{fluorophore}:background_reference_rois")
            background_plan = background_plan_stored.record
            background_pointer = background_decision.response.artifacts[0]
            if (
                background_bundle_stored.record.plan != background_plan
                or background_pointer.artifact_id != background_plan_stored.content_sha256
                or background_pointer.artifact_sha256 != background_plan.export.content_sha256
                or (prepared is None and (background_plan.export != roi_export
                    or background_plan.sampling_policy != sampling_policy))
                or (prepared is not None and background_plan.source_preparation_sha256 != prepared.content_sha256)
            ):
                raise ReferenceCheckpointConflictError(
                    "live Task-6 background sampling authority differs"
                )
            resolutions_stored = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=background_authority,
                kind="background_resolutions",
                subject_id=fluorophore,
            )
            resolutions = resolutions_stored.record
            if len(resolutions) != len(af_plan.pairings):
                raise ReferenceCheckpointConflictError(
                    "live Task-6 resolution inventory differs from AF pairings"
                )
            resolution_by_roi = {}
            for pairing, resolution in zip(af_plan.pairings, resolutions, strict=True):
                previous = resolution_by_roi.setdefault(pairing.control_roi_id, resolution)
                # Several stained ROIs may use the same measured AF region and
                # identical background resolution. Conflicting bindings remain invalid.
                if previous != resolution:
                    raise ReferenceCheckpointConflictError(
                        "live Task-6 AF/background pairing is ambiguous"
                    )
            group_resolutions = tuple(resolution_by_roi[item.roi_id] for item in group)
            background_samples = set(background_bundle_stored.record.samples)
            if prepared is not None:
                background_samples.update(prepared.record.backgrounds)
            if any(
                resolution.status != "resolved"
                or resolution.policy != background_policy
                or resolution.control_side is None
                or resolution.control_side.source_input_id != control.input_id
                or resolution.control_side.source_image_sha256
                != control.evidence.source_image_sha256
                or not set(resolution.control_side.source_rois).issubset(background_samples)
                for resolution in group_resolutions
            ):
                raise ReferenceCheckpointConflictError(
                    "live Task-6 shared-control background authority differs"
                )
            selected_samples.extend(group_samples)
            selected_resolutions.extend(group_resolutions)
            authorities.append(
                TargetAfSharedRoiAuthority(
                    fluorophore,
                    tuple(item.roi_id for item in group),
                    af_decision.content_sha256,
                    af_plan_stored.content_sha256,
                    af_bundle_stored.content_sha256,
                    background_decision.content_sha256,
                    background_plan_stored.content_sha256,
                    background_bundle_stored.content_sha256,
                    tuple(item.resolution_sha256 for item in group_resolutions),
                )
            )
        if selected_remaining or not selected_samples:
            raise ReferenceCheckpointConflictError(
                "target-AF selections do not resolve to one exact live Task-5 authority"
            )
        control_sides = tuple(item.control_side for item in selected_resolutions)
        if any(item is None for item in control_sides) or len(set(control_sides)) != 1:
            raise ReferenceCheckpointConflictError(
                "selected live Task-6 authorities differ on shared-control background"
            )
        return (
            tuple(selected_samples),
            tuple(selected_resolutions),
            tuple(authorities),
        )

    def _persist_roi_plan_and_samples(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        current: ReferenceWorkflowCheckpoint,
        plan: ReferenceRoiSelectionPlan,
    ):
        subject = f"{plan.fluorophore}:{plan.checkpoint}"
        authority = current.content_sha256
        stored_plan = self._artifact_at_authority_or_none(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority,
            kind="roi_selection_plan",
            subject_id=subject,
        )
        stored_samples = self._artifact_at_authority_or_none(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority,
            kind="sampled_roi_bundle",
            subject_id=subject,
        )
        if stored_plan is not None and stored_plan.record != plan:
            raise ReferenceCheckpointConflictError(
                "ROI operation inputs conflict with persisted authority"
            )
        if plan.source_preparation_sha256 is not None:
            from .reference_service_control_preparation import persist_sample_use
            if stored_plan is None:
                stored_plan = self._artifacts.put_unique(run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    authority_checkpoint_sha256=authority, kind="roi_selection_plan",
                    subject_id=subject, record=plan)
            stored_samples = persist_sample_use(self, current, start_authority_sha256, plan)
            return stored_plan, stored_samples, stored_samples.record
        if stored_samples is not None:
            if stored_plan is None or stored_samples.record.plan != plan:
                raise ReferenceCheckpointConflictError(
                    "sampled ROI bundle conflicts with persisted plan authority"
                )
            return stored_plan, stored_samples, stored_samples.record
        try:
            if stored_plan is None:
                stored_plan = self._artifacts.put_unique(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    authority_checkpoint_sha256=authority,
                    kind="roi_selection_plan",
                    subject_id=subject,
                    record=plan,
                )
            sampled = sample_reference_roi_selection_plan(
                self._sampling_backend(),
                self._canonical_inventory(run_id, start_authority_sha256),
                plan,
            )
            stored_samples = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=authority,
                kind="sampled_roi_bundle",
                subject_id=subject,
                record=sampled,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "ROI operation inputs conflict with persisted authority"
            ) from exc
        return stored_plan, stored_samples, sampled

    def _persist_two_cas(
        self,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        proposed: ReferenceWorkflowCheckpoint,
        result: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
    ) -> ReferenceServiceAdvance:
        persisted_proposal, _ = self._store._compare_and_swap_service(
            current.content_sha256, proposed, start_authority_sha256
        )
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_proposal.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def _accept_control_roi_phase(
        self,
        *,
        run_id,
        current,
        start_authority_sha256,
        reviewer_id,
        roi_export,
        selections,
        pairings,
        sampling_policy,
        background_policy,
        signal_dark_selections,
        negative_fallback_policy,
        purification_policy,
        extraction_dispositions,
    ) -> ReferenceServiceAdvance:
        checkpoint = current.request.checkpoint.value
        if (
            type(roi_export) is not ReferenceRoiExport
            or type(selections) is not tuple
            or not selections
            or type(pairings) is not tuple
            or not pairings
            or type(sampling_policy) is not CompleteVectorQcPolicy
        ):
            raise TypeError(
                "control ROI review requires exact export, selections, pairings, and sampling policy"
            )
        if (
            background_policy is not None
            or signal_dark_selections
            or negative_fallback_policy is not None
            or purification_policy is not None
            or extraction_dispositions
        ):
            raise ValueError("control ROI review received irrelevant checkpoint inputs")
        fluorophore = current.current_fluorophore
        positive = self._live_roi_phase_artifact(
            current,
            start_authority_sha256,
            "sampled_roi_bundle",
            fluorophore,
            "positive_reference_rois",
        ).record
        role = "af" if checkpoint == "af_reference_rois" else "negative"
        if any(item.roi.roi_role != role for item in selections):
            raise ValueError("control ROI role differs from the live checkpoint")
        if checkpoint == "negative_reference_rois":
            binding = self._one_artifact(run_id, start_authority_sha256,
                "reference_control_files", "reference-control-files").record
            index = current.fluorophores.index(fluorophore)
            if any(item.entry.image.reference_input_id != binding.negative_control_input_ids[index]
                   or item.roi.role_link_id != fluorophore for item in selections):
                raise ValueError("Negative ROI selection uses an unaccepted stain-specific control")
        if checkpoint == "af_reference_rois":
            binding = self._one_artifact(
                run_id,
                start_authority_sha256,
                "reference_control_files",
                "reference-control-files",
            ).record
            from .reference_service_control_preparation import source_input_id
            control_input_id = source_input_id(binding, current)
            control_link = "shared" if binding.shared_unstained_input_id is not None else fluorophore
            if any(item.entry.image.reference_input_id != control_input_id
                    or item.roi.role_link_id != control_link for item in selections):
                raise ValueError("AF ROI selection uses an unaccepted control source")
        positive_ids = tuple(item.selection.roi.roi_id for item in positive.samples)
        control_ids = {item.roi.roi_id for item in selections}
        paired_positive_ids = tuple(dict.fromkeys(item.positive_roi_id for item in pairings))
        expected_pairs = tuple((positive_id, item.roi_id) for positive_id in positive_ids
            for item in selections if any(pair.positive_roi_id == positive_id
                and pair.control_roi_id == item.roi_id for pair in pairings))
        if (paired_positive_ids != positive_ids
                or tuple((item.positive_roi_id, item.control_roi_id) for item in pairings) != expected_pairs
                or {item.control_roi_id for item in pairings} != control_ids
                or (role == "negative" and len(pairings) != len(positive_ids))):
            raise ValueError("control pairings must cover positive ROIs and selected controls in exact order")
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256) if role == "af" else None
        if prepared is not None:
            if roi_export != prepared.record.plan.export:
                raise ValueError("Assign AF sets from the accepted control source export")
            for positive_id in positive_ids:
                members = tuple(pair.control_roi_id for pair in pairings if pair.positive_roi_id == positive_id)
                if members not in prepared.record.control_sets:
                    raise ValueError("Assign one accepted AF set to each stained region")
        plan = ReferenceRoiSelectionPlan.create(
            source_checkpoint_sha256=current.content_sha256,
            checkpoint=checkpoint,
            fluorophore=fluorophore,
            export=roi_export,
            selections=selections,
            pairings=pairings,
            sampling_policy=sampling_policy,
            reviewer_id=reviewer_id,
            source_preparation_sha256=None if prepared is None else prepared.content_sha256,
        )
        stored_plan, _, sampled = self._persist_roi_plan_and_samples(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            current=current,
            plan=plan,
        )
        positive_by_id = {item.selection.roi.roi_id: item for item in positive.samples}
        control_by_id = {item.selection.roi.roi_id: item for item in sampled.samples}
        proposals = tuple(
            propose_matched_control_review(
                positive_by_id[item.positive_roi_id],
                control_by_id[item.control_roi_id],
            )
            for item in pairings
        )
        pointer = ArtifactPointer(
            "af_reference_rois" if role == "af" else "negative_reference_rois",
            stored_plan.content_sha256,
            roi_export.content_sha256,
            "shared" if role == "af" and current.control_strategy == "shared_unstained_control" else fluorophore,
        )
        proposed = propose_reference_artifacts(
            current,
            (
                self._result_operations._validated_artifact(
                    pointer, "qupath_roi_geometry", stored_plan.content_sha256
                ),
            ),
            evidence=tuple(matched_control_review_request_evidence(item) for item in proposals),
        )
        assert proposed.request is not None
        response = configured_response(
            proposed.request,
            option_value=f"accept_{checkpoint}",
            reviewer_id=reviewer_id,
            artifacts=(pointer,),
        )
        result = advance_reference_workflow(proposed, response)
        reviews = tuple(bind_matched_control_review(item, proposed, result) for item in proposals)
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="matched_control_reviews",
            subject_id=fluorophore,
            record=reviews,
        )
        return self._persist_two_cas(run_id, current, proposed, result, start_authority_sha256)

    def _accept_background_roi_phase(
        self,
        *,
        run_id,
        current,
        start_authority_sha256,
        reviewer_id,
        roi_export,
        selections,
        pairings,
        sampling_policy,
        background_policy,
        signal_dark_selections,
        negative_fallback_policy,
        purification_policy,
        extraction_dispositions,
    ) -> ReferenceServiceAdvance:
        if (
            type(roi_export) is not ReferenceRoiExport
            or type(selections) is not tuple
            or not selections
            or pairings
            or type(sampling_policy) is not CompleteVectorQcPolicy
            or type(background_policy) is not BackgroundEquivalencePolicy
            or signal_dark_selections
            or purification_policy is not None
            or extraction_dispositions
        ):
            raise ValueError("background review received missing or irrelevant checkpoint inputs")
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256)
        negative = current.control_strategy == "per_stain_negative_controls" and prepared is None
        if negative != (type(negative_fallback_policy) is NegativeFallbackReviewPolicy):
            raise ValueError("negative-fallback policy must match the reviewed control strategy")
        if any(item.roi.roi_role != "background" for item in selections):
            raise ValueError("background checkpoint accepts only background ROIs")
        fluorophore = current.current_fluorophore
        positive_stored = self._live_roi_phase_artifact(
            current,
            start_authority_sha256,
            "sampled_roi_bundle",
            fluorophore,
            "positive_reference_rois",
        )
        control_checkpoint = "negative_reference_rois" if negative else "af_reference_rois"
        control_stored = self._live_roi_phase_artifact(
            current,
            start_authority_sha256,
            "sampled_roi_bundle",
            fluorophore,
            control_checkpoint,
        )
        control_plan = control_stored.record.plan
        positive_by_id = {
            item.selection.roi.roi_id: item for item in positive_stored.record.samples
        }
        control_by_id = {item.selection.roi.roi_id: item for item in control_stored.record.samples}
        paired_input_ids = {
            input_id
            for pair in control_plan.pairings
            for input_id in (
                positive_by_id[pair.positive_roi_id].input_evidence.input_id,
                control_by_id[pair.control_roi_id].input_evidence.input_id,
            )
        }
        if any(item.entry.image.reference_input_id not in paired_input_ids for item in selections):
            raise ValueError("background ROI selection is outside the exact pairing input IDs")
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256) if not negative else None
        if prepared is not None and background_policy != prepared.record.plan.background_policy:
            raise ValueError("AF source background settings require a source preparation revision")
        plan = ReferenceRoiSelectionPlan.create(
            source_checkpoint_sha256=current.content_sha256,
            checkpoint="background_reference_rois",
            fluorophore=fluorophore,
            export=roi_export,
            selections=selections,
            pairings=(),
            sampling_policy=sampling_policy,
            reviewer_id=reviewer_id,
            source_preparation_sha256=None if prepared is None else prepared.content_sha256,
        )
        stored_plan, background_stored, background_samples = self._persist_roi_plan_and_samples(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            current=current,
            plan=plan,
        )
        backgrounds = background_samples.samples
        if prepared is not None:
            backgrounds = tuple(item for item in backgrounds if item.input_evidence.input_id != prepared.record.plan.input_id)
            backgrounds += prepared.record.backgrounds
        resolutions = tuple(
            resolve_background_model(
                positive_by_id[pair.positive_roi_id],
                control_by_id[pair.control_roi_id],
                tuple(
                    item
                    for item in backgrounds
                    if item.input_evidence.input_id
                    == positive_by_id[pair.positive_roi_id].input_evidence.input_id
                ),
                tuple(
                    item
                    for item in backgrounds
                    if item.input_evidence.input_id
                    == control_by_id[pair.control_roi_id].input_evidence.input_id
                ),
                (),
                None,
                background_policy,
            )
            for pair in control_plan.pairings
        )
        operation = ReferenceBackgroundOperation(
            current.content_sha256,
            reviewer_id,
            positive_stored.record.content_sha256,
            control_stored.record.content_sha256,
            background_stored.record.content_sha256,
            background_policy,
            negative_fallback_policy,
            tuple(item.resolution_sha256 for item in resolutions),
        )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="background_operation",
            subject_id=fluorophore,
            record=operation,
        )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="background_resolutions",
            subject_id=fluorophore,
            record=resolutions,
        )
        if any(item.status == "request_required" for item in resolutions):
            retained, retained_start = self._store._load_service_checkpoint(run_id)
            if retained.content_sha256 != current.content_sha256:
                raise ReferenceCheckpointConflictError(
                    "background checkpoint changed before missing-evidence return"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.NEEDS_SCIENTIFIC_RESULT,
                ReferenceServiceSnapshot(retained, retained_start),
                "background resolution requires missing sampled image-specific evidence",
            )
        next_evidence = ()
        if negative:
            assert negative_fallback_policy is not None
            proposals = tuple(
                propose_negative_fallback_review(
                    positive_by_id[pair.positive_roi_id],
                    control_by_id[pair.control_roi_id],
                    resolution,
                    negative_fallback_policy,
                )
                for pair, resolution in zip(control_plan.pairings, resolutions, strict=True)
            )
            self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=current.content_sha256,
                kind="negative_fallback_proposals",
                subject_id=fluorophore,
                record=proposals,
            )
            next_evidence = tuple(
                negative_fallback_review_request_evidence(item) for item in proposals
            )
        pointer = ArtifactPointer(
            "background_reference_rois",
            stored_plan.content_sha256,
            roi_export.content_sha256,
            fluorophore,
        )
        proposed = propose_reference_artifacts(
            current,
            (
                self._result_operations._validated_artifact(
                    pointer, "qupath_roi_geometry", stored_plan.content_sha256
                ),
            ),
        )
        assert proposed.request is not None
        response = configured_response(
            proposed.request,
            option_value="accept_background_reference_rois",
            reviewer_id=reviewer_id,
            artifacts=(pointer,),
        )
        result = advance_reference_workflow(proposed, response, next_evidence=next_evidence)
        return self._persist_two_cas(run_id, current, proposed, result, start_authority_sha256)

    def _af_control_set_members(self, current, start_authority_sha256):
        from .reference_service_control_preparation import accepted_preparation
        if current.control_strategy == "per_stain_negative_controls" and accepted_preparation(self, current, start_authority_sha256) is None:
            return ()
        fluorophore = current.current_fluorophore
        positive = self._live_roi_phase_artifact(current, start_authority_sha256,
            "sampled_roi_bundle", fluorophore, "positive_reference_rois").record
        control = self._live_roi_phase_artifact(current, start_authority_sha256,
            "sampled_roi_bundle", fluorophore, "af_reference_rois").record
        matches = self._live_roi_phase_artifact(current, start_authority_sha256,
            "matched_control_reviews", fluorophore, "af_reference_rois").record
        resolutions = self._live_roi_phase_artifact(current, start_authority_sha256,
            "background_resolutions", fluorophore, "background_reference_rois").record
        controls = {item.selection.roi.roi_id: item for item in control.samples}
        return tuple((source, tuple(
            AfControlSetMember(controls[pair.control_roi_id], background, match)
            for pair, background, match in zip(control.plan.pairings, resolutions, matches, strict=True)
            if pair.positive_roi_id == source.selection.roi.roi_id)) for source in positive.samples)

    def _af_control_sets(self, current, start_authority_sha256):
        return tuple(AfControlSetEvidence(source, members) for source, members in
            self._af_control_set_members(current, start_authority_sha256))

    def preview_af_control_sets(self, run_id: str) -> tuple[AfControlSetEvidence, ...]:
        """Return actual corrected control spectra before any fluorophore extraction."""
        current, start = self._store._load_service_checkpoint(run_id)
        if current.request is None or current.request.checkpoint.value != "signal_dark_measurements":
            raise ValueError("AF control spectrum review is available before extraction")
        return self._af_control_sets(current, start)

    def _af_control_set_review_summary(self, checkpoint, start):
        if checkpoint.request is None or checkpoint.request.checkpoint.value != "signal_dark_measurements":
            return ()
        from .reference_service_control_preparation import accepted_preparation
        if checkpoint.control_strategy == "per_stain_negative_controls" and accepted_preparation(self, checkpoint, start) is None:
            return self._negative_control_spectrum_review_summary(checkpoint, start)
        control_sets = self._af_control_set_members(checkpoint, start)
        if not control_sets:
            return ()
        axis = control_sets[0][0].input_evidence.axis
        sets = []
        for positive, members in control_sets:
            # Invalid signal remains visible for region reselection, but cannot
            # acquire the accepted control-set evidence/hash used for extraction.
            try:
                group = AfControlSetEvidence(positive, members)
            except AfControlSignalError as exc:
                group, diagnostic = None, str(exc)
            else:
                diagnostic = None
            vectors = tuple(member.signed_values for member in members)
            signed = (group.signed_corrected_mean if group is not None else tuple(
                float(math.fsum(vector[index] / len(vectors) for vector in vectors))
                for index in range(len(axis.measurement_ids))))
            shape = None if group is None else group.normalized_shape
            regions = []
            for member, values in zip(members, vectors, strict=True):
                corrected = tuple(max(value, 0.) for value in values)
                norm = math.hypot(*corrected)
                region_shape = (tuple(value / norm for value in corrected)
                    if math.isfinite(norm) and norm > 0. else None)
                angle = (math.degrees(math.acos(max(-1., min(1., math.fsum(
                    a*b for a,b in zip(region_shape, shape, strict=True))))))
                    if region_shape is not None and shape is not None else None)
                regions.append((("roi_id", member.control.selection.roi.roi_id),
                    ("label", _reference_region_label(member.control, "Autofluorescence")),
                    ("control_filename", member.control.selection.entry.image.source_path.name),
                    ("signed_values", values), ("corrected_values", corrected),
                    ("values", region_shape), ("accepted_pixel_count", member.control.sampling.accepted_vector_count),
                    ("corrected_l2", norm), ("clipped_count", sum(value < 0. for value in values)),
                    ("clipped_l2", math.hypot(*(min(value, 0.) for value in values))),
                    ("angle_to_combined_degrees", angle),
                    ("status", "pass" if region_shape is not None else "failed"),
                    ("diagnostic", None if region_shape is not None else
                        "No usable AF signal remains after image-background correction.")))
            sets.append((("positive_roi_id", positive.selection.roi.roi_id),
                ("positive_label", _reference_region_label(positive)),
                ("roi_ids", tuple(member.control.selection.roi.roi_id for member in members)),
                ("roi_weights", tuple(1. / len(members) for _ in members)), ("signed_values", signed),
                ("corrected_values", tuple(max(value, 0.) for value in signed)), ("values", shape),
                ("clipped_measurement_ids", tuple(mid for mid, value in zip(axis.measurement_ids,
                    signed, strict=True) if value < 0.)),
                ("evidence_sha256", None if group is None else group.evidence_sha256),
                ("status", "failed" if group is None else "pass"), ("diagnostic", diagnostic),
                ("regions", tuple(regions)),
                ("control_filename", members[0].control.selection.entry.image.source_path.name),
                ("correction_mode", positive.input_evidence.quantitative_signature.measurements[0].correction_mode)))
        return (("af_control_set_review", (("measurement_names", axis.parent_band_ids),
            *_reference_wavelength_fields(axis), ("sets", tuple(sets)))),)

    def _negative_control_spectrum_review_summary(self, checkpoint, start):
        """Display the existing matched singleton negatives without new fit authority."""
        fluor = checkpoint.current_fluorophore
        positives = self._live_roi_phase_artifact(checkpoint, start, "sampled_roi_bundle",
            fluor, "positive_reference_rois").record
        negatives = self._live_roi_phase_artifact(checkpoint, start, "sampled_roi_bundle",
            fluor, "negative_reference_rois").record
        backgrounds = self._live_roi_phase_artifact(checkpoint, start, "background_resolutions",
            fluor, "background_reference_rois").record
        by_positive = {item.selection.roi_id: item for item in positives.samples}
        by_negative = {item.selection.roi_id: item for item in negatives.samples}
        axis = positives.samples[0].input_evidence.axis
        sets = []
        for pairing, background in zip(negatives.plan.pairings, backgrounds, strict=True):
            positive, control = by_positive[pairing.positive_roi_id], by_negative[pairing.control_roi_id]
            if background.model is None:
                raise ReferenceCheckpointConflictError("The matched negative background is unavailable")
            signed = tuple(float(value - offset) for value, offset in zip(control.sampling.mean_vector,
                background.model.control_background.values, strict=True))
            corrected = tuple(max(value, 0.) for value in signed)
            norm = math.hypot(*corrected)
            shape = tuple(value / norm for value in corrected) if math.isfinite(norm) and norm > 0. else None
            diagnostic = None if shape is not None else "No usable signal remains in this matched control after background correction."
            filename = control.selection.entry.image.source_path.name
            region = (("roi_id", control.selection.roi_id), ("label", _reference_region_label(control, "Control")),
                ("control_filename", filename), ("origin_role", "negative"), ("signed_values", signed),
                ("corrected_values", corrected), ("values", shape),
                ("accepted_pixel_count", control.sampling.accepted_vector_count), ("corrected_l2", norm),
                ("clipped_count", sum(value < 0. for value in signed)),
                ("clipped_l2", math.hypot(*(min(value, 0.) for value in signed))),
                ("angle_to_combined_degrees", 0. if shape is not None else None),
                ("status", "pass" if shape is not None else "failed"), ("diagnostic", diagnostic))
            sets.append((("positive_roi_id", positive.selection.roi_id),
                ("positive_label", _reference_region_label(positive)), ("roi_ids", (control.selection.roi_id,)),
                ("roi_weights", (1.,)), ("signed_values", signed), ("corrected_values", corrected),
                ("values", shape), ("evidence_sha256", None),
                ("status", "pass" if shape is not None else "failed"), ("diagnostic", diagnostic),
                ("regions", (region,)), ("control_filename", filename),
                ("correction_mode", control.input_evidence.quantitative_signature.measurements[0].correction_mode)))
        return (("af_control_set_review", (("review_kind", "matched_negative"),
            ("measurement_names", axis.parent_band_ids), *_reference_wavelength_fields(axis), ("sets", tuple(sets)))),)

    def _accept_signal_dark_phase(
        self,
        *,
        run_id,
        current,
        start_authority_sha256,
        reviewer_id,
        roi_export,
        selections,
        pairings,
        sampling_policy,
        background_policy,
        signal_dark_selections,
        negative_fallback_policy,
        purification_policy,
        extraction_dispositions,
        af_control_set_review_sha256s,
    ) -> ReferenceServiceAdvance:
        if (
            roi_export is not None
            or selections
            or pairings
            or sampling_policy is not None
            or background_policy is not None
            or type(signal_dark_selections) is not tuple
            or not signal_dark_selections
            or negative_fallback_policy is not None
            or type(purification_policy) is not PurificationPolicy
            or extraction_dispositions
        ):
            raise ValueError("signal/dark review received missing or irrelevant checkpoint inputs")
        fluorophore = current.current_fluorophore
        from .reference_service_control_preparation import accepted_preparation
        prepared = accepted_preparation(self, current, start_authority_sha256)
        negative = current.control_strategy == "per_stain_negative_controls" and prepared is None
        control_checkpoint = "negative_reference_rois" if negative else "af_reference_rois"
        positive = self._live_roi_phase_artifact(
            current,
            start_authority_sha256,
            "sampled_roi_bundle",
            fluorophore,
            "positive_reference_rois",
        ).record
        control = self._live_roi_phase_artifact(
            current,
            start_authority_sha256,
            "sampled_roi_bundle",
            fluorophore,
            control_checkpoint,
        ).record
        matches = self._live_roi_phase_artifact(
            current, start_authority_sha256, "matched_control_reviews", fluorophore, control_checkpoint
        ).record
        resolutions = self._live_roi_phase_artifact(
            current, start_authority_sha256, "background_resolutions", fluorophore, "background_reference_rois"
        ).record
        background_operation = self._live_roi_phase_artifact(
            current, start_authority_sha256, "background_operation", fluorophore, "background_reference_rois"
        ).record
        positive_by_id = {item.selection.roi.roi_id: item for item in positive.samples}
        control_by_id = {item.selection.roi.roi_id: item for item in control.samples}
        # Preserve the saved singleton path, including its failed-purification
        # evidence for weak controls. Multi-region sets must still be usable
        # and explicitly reviewed before any extraction can be performed.
        needs_control_set = (control.plan.source_preparation_sha256 is not None
            or bool(af_control_set_review_sha256s) or len(control.plan.pairings) > len(positive.samples))
        control_sets = (self._af_control_sets(current, start_authority_sha256)
            if not negative and needs_control_set else ())
        expected_reviews = tuple(item.evidence_sha256 for item in control_sets)
        if control.plan.source_preparation_sha256 is not None and not af_control_set_review_sha256s:
            # Source-set spectra were already explicitly accepted before any stain.
            af_control_set_review_sha256s = expected_reviews
        if ((af_control_set_review_sha256s and af_control_set_review_sha256s != expected_reviews)
                or (any(len(item.members) > 1 for item in control_sets)
                    and af_control_set_review_sha256s != expected_reviews)):
            raise ValueError("Review the current AF control-set spectra before extraction")
        response = configured_response(
            current.request,
            option_value="accept_signal_dark_measurements",
            reviewer_id=reviewer_id,
            selections=signal_dark_selections,
        )
        bare_result = advance_reference_workflow(current, response)
        signal_reviews = tuple(
            bind_signal_dark_measurement_review(
                positive_by_id[pair.positive_roi_id],
                control_by_id[pair.control_roi_id],
                current,
                bare_result,
            )
            for pair in control.plan.pairings
        )
        negative_reviews = ()
        if negative:
            proposals = self._live_roi_phase_artifact(
                current, start_authority_sha256, "negative_fallback_proposals",
                fluorophore, "background_reference_rois",
            ).record
            anatomy_decision, _ = self._active_roi_decision_authority(
                run_id=run_id,
                current=current,
                start_authority_sha256=start_authority_sha256,
                checkpoint="negative_reference_rois",
                fluorophore=fluorophore,
                option_value="accept_negative_reference_rois",
                artifact_role="negative_reference_rois",
            )
            anatomy_source, anatomy_result, loaded_start = (
                self._store._load_service_decision_authority(
                    run_id,
                    source_checkpoint_sha256=anatomy_decision.source_checkpoint_sha256,
                    decision_record_sha256=anatomy_decision.content_sha256,
                )
            )
            if loaded_start != start_authority_sha256:
                raise ReferenceCheckpointConflictError("negative review start authority differs")
            negative_reviews = tuple(
                bind_negative_fallback_review(
                    proposal,
                    anatomy_source,
                    anatomy_result,
                    current,
                    bare_result,
                )
                for proposal in proposals
            )
        if not negative and af_control_set_review_sha256s:
            review_by_pair = {(pair.positive_roi_id, pair.control_roi_id): review
                for pair, review in zip(control.plan.pairings, signal_reviews, strict=True)}
            purifications = tuple(purify_reference_control_set(group,
                tuple(review_by_pair[(group.positive.selection.roi.roi_id, roi_id)] for roi_id in group.roi_ids),
                purification_policy, signal_dark_source=current, signal_dark_result=bare_result)
                for group in control_sets)
        else:
            purifications = tuple(
                purify_reference_roi(
                    positive_by_id[pair.positive_roi_id],
                    control_by_id[pair.control_roi_id],
                    resolution,
                    match,
                    signal_review,
                    purification_policy,
                    signal_dark_source=current,
                    signal_dark_result=bare_result,
                    negative_fallback_review=(None if not negative else negative_reviews[index]),
                )
                for index, (pair, resolution, match, signal_review) in enumerate(
                    zip(control.plan.pairings, resolutions, matches, signal_reviews, strict=True)
                )
            )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="signal_dark_reviews",
            subject_id=fluorophore,
            record=signal_reviews,
        )
        if negative_reviews:
            self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=current.content_sha256,
                kind="negative_fallback_reviews",
                subject_id=fluorophore,
                record=negative_reviews,
            )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="purifications",
            subject_id=fluorophore,
            record=purifications,
        )
        operation = ReferenceSignalPurificationOperation(
            current.content_sha256,
            reviewer_id,
            signal_dark_selections,
            background_operation.content_sha256,
            purification_policy,
            tuple(item.evidence_sha256 for item in purifications),
            af_control_set_review_sha256s,
        )
        self._artifacts.put_unique(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=current.content_sha256,
            kind="signal_purification_operation",
            subject_id=fluorophore,
            record=operation,
        )
        persisted_result, _ = self._store._compare_and_swap_service(
            current.content_sha256, bare_result, start_authority_sha256
        )
        extraction = propose_reference_evidence(
            persisted_result,
            extraction_request_evidence(purifications),
        )
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_result.content_sha256, extraction, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    def accept_extraction(
        self,
        run_id: str,
        expected_content_sha256: str,
        purifications: tuple[PurifiedReferenceRoi, ...],
        dispositions: tuple[SelectedIdentifier, ...],
        *,
        reviewer_id: str,
    ) -> ReferenceServiceAdvance:
        return self._result_operations.accept_extraction(
            run_id,
            expected_content_sha256,
            purifications,
            dispositions,
            reviewer_id=reviewer_id,
        )

    def extraction_inclusion_decisions(self, run_id: str) -> tuple[ConsensusInclusionDecision, ...]:
        return self._result_operations.extraction_inclusion_decisions(run_id)

    def accept_live_fluorophore_consensus(
        self,
        run_id: str,
        expected_content_sha256: str,
        *,
        reviewer_id: str,
        consensus_policy: Mapping[str, object],
        qc_policy: Mapping[str, object],
        roi_review: dict[str, str],
    ) -> ReferenceServiceAdvance:
        """Accept explicitly reviewed regional choices against saved purification evidence."""

        if type(consensus_policy) is not dict or set(consensus_policy) != {
            "outlier_review_angle_degrees"
        }:
            raise ValueError("consensus policy fields differ")
        qc_fields = {
            "svd_absolute_tolerance",
            "svd_relative_tolerance",
            "cosine_review_threshold",
            "cosine_fail_threshold",
            "condition_review_threshold",
            "condition_fail_threshold",
            "normalization_tolerance",
        }
        if type(qc_policy) is not dict or set(qc_policy) != qc_fields:
            raise ValueError("spectrum-matrix QC policy fields differ")
        consensus = ConsensusPolicy(policy_version=_CONSENSUS_POLICY_VERSION, **consensus_policy)
        qc_authority = typed_checksum_sha256(
            {
                "kind": "reference-unmixing-spectrum-matrix-qc-policy/v1",
                "policy_version": _MATRIX_QC_POLICY_VERSION,
                "configuration": qc_policy,
            }
        )
        matrix_qc = SpectrumMatrixQcPolicy(
            **qc_policy,
            policy_version=_MATRIX_QC_POLICY_VERSION,
            authority_sha256=qc_authority,
        )
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        if current.content_sha256 == expected_content_sha256:
            if (
                current.request is None
                or current.request.checkpoint.value != "fluorophore_consensus"
                or current.current_fluorophore is None
            ):
                raise ValueError("workflow is not awaiting live fluorophore consensus")
            fluorophore = current.current_fluorophore
        else:
            superseded = {identity for transition in current.transition_log
                          for identity in transition.superseded_request_ids}
            reviewed = tuple(
                item
                for item in current.decisions
                if item.request.checkpoint.value == "fluorophore_consensus"
                and item.request.prior_checkpoint_sha256 == expected_content_sha256
                and item.request.fluorophore is not None
                and item.request.request_id not in superseded
            )
            if len(reviewed) != 1:
                raise ReferenceCheckpointConflictError(
                    "live consensus checkpoint expectation is stale"
                )
            fluorophore = reviewed[0].request.fluorophore
        purifications, decisions = self._result_operations._reviewed_extraction_for_consensus(
            run_id, current, start_authority_sha256, fluorophore
        )
        from .reference_service_result_operations import reviewed_consensus_inclusions, validate_consensus_retention
        decisions = reviewed_consensus_inclusions(
            decisions, roi_review, checkpoint_sha256=expected_content_sha256,
            reviewer_id=reviewer_id,
        )
        candidate = build_fluorophore_consensus(
            fluorophore, fluorophore, purifications, decisions, consensus
        )
        validate_consensus_retention(candidate, roi_review)
        reviewed_at = _derived_service_timestamp(
            expected_content_sha256, "fluorophore-consensus-review"
        )
        final = all(
            item.accepted_spectrum is not None or item.fluorophore == fluorophore
            for item in current.fluorophore_artifacts
        )
        publication = None
        if final:
            publication_id = (
                "reference-library-"
                + typed_checksum_sha256(
                    {
                        "run_id": run_id,
                        "checkpoint": expected_content_sha256,
                        "candidate": candidate.candidate_sha256,
                        "qc_policy": matrix_qc.content_sha256,
                    }
                )[:24]
            )
            publication = ReferenceLibraryPublicationMetadata(
                artifact_id=publication_id,
                created_at=reviewed_at,
                reviewed_at=reviewed_at,
                software_versions=(("mif_pipeline", "reference-unmixing-operation/v1"),),
                policy_versions=(
                    ("consensus", consensus.policy_version),
                    ("matrix_qc", matrix_qc.policy_version),
                ),
                creation_reason="reviewed live spectral-reference consensus",
            )
        return self._result_operations.accept_consensus(
            run_id,
            expected_content_sha256,
            candidate,
            matrix_qc,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            publication=publication,
            roi_review=roi_review,
        )

    def accept_consensus(
        self,
        run_id: str,
        expected_content_sha256: str,
        candidate: FluorophoreConsensusCandidate,
        qc_policy: SpectrumMatrixQcPolicy,
        *,
        reviewer_id: str,
        reviewed_at: str,
        publication: ReferenceLibraryPublicationMetadata | None = None,
    ) -> ReferenceServiceAdvance:
        return self._result_operations.accept_consensus(
            run_id,
            expected_content_sha256,
            candidate,
            qc_policy,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            publication=publication,
        )


__all__ = [
    "ReferenceLibraryPublicationMetadata",
    "ReferenceServiceArtifactRequirement",
    "ReferenceServiceAdvance",
    "ReferenceServiceAdvanceStatus",
    "ReferenceServiceOptionRequirement",
    "ReferenceServiceSnapshot",
    "ReferenceWorkflowService",
]
