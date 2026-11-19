"""Pure resumable state transitions for spectral-reference review.

The `ReferenceWorkflowCheckpoint` record carries `run_id`, `target_image_sha256`,
`fluorophores`, `measurement_ids`, `revision` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .interactions import (
    ArtifactPointer,
    ArtifactRequirement,
    ReferenceAction,
    ReferenceActionCode,
    ReferenceCheckpointKind,
    ReferenceEvidence,
    ReferenceInteractionRequest,
    ReferenceOption,
    ReferenceReason,
    ReferenceReasonCode,
    ReferenceResponse,
    SelectedIdentifier,
    ValidatedArtifactEvidence,
    PublishedArtifactEvidence,
    TargetControlCompatibilityEvidence,
    configured_response,
    make_reference_request,
    validate_reference_response,
)

_MAX_TRANSITIONS = 256
_MAX_CHECKPOINT_BYTES = 2_097_152


# ==============================================================================
#                               Reference workflow
# ==============================================================================


class ReferenceWorkflowStatus(str, Enum):
    AWAITING_RESPONSE = "awaiting_response"
    AWAITING_PUBLICATION = "awaiting_publication"
    COMPLETED = "completed"
    ABORTED = "aborted"


class ReferenceTransitionKind(str, Enum):
    DECISION = "decision"
    PROPOSAL = "proposal"
    INVALIDATION = "invalidation"
    PREPARATION_REVISION = "preparation_revision"
    PUBLICATION = "publication"


_CHECKPOINT_REASON = {
    ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION: ReferenceReasonCode.SHARED_CONTROL_PREPARATION_REQUIRED,
    ReferenceCheckpointKind.BASIS_SOURCE: ReferenceReasonCode.SPECTRAL_BASIS_SOURCE_REQUIRED,
    ReferenceCheckpointKind.CONTROL_STRATEGY: ReferenceReasonCode.REFERENCE_CONTROL_STRATEGY_REQUIRED,
    ReferenceCheckpointKind.REFERENCE_CONTROL_FILES: ReferenceReasonCode.REFERENCE_IMAGE_REQUIRED,
    ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS: ReferenceReasonCode.POSITIVE_REFERENCE_ROIS_REQUIRED,
    ReferenceCheckpointKind.AF_REFERENCE_ROIS: ReferenceReasonCode.AF_REFERENCE_ROIS_REQUIRED,
    ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS: ReferenceReasonCode.NEGATIVE_REFERENCE_ROIS_REQUIRED,
    ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS: ReferenceReasonCode.REFERENCE_BACKGROUND_ROIS_REQUIRED,
    ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS: ReferenceReasonCode.REFERENCE_BAND_SELECTION_REQUIRED,
    ReferenceCheckpointKind.EXTRACTION_QC: ReferenceReasonCode.REFERENCE_SPECTRUM_QC_FAILED,
    ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS: ReferenceReasonCode.REFERENCE_SPECTRUM_REVIEW_REQUIRED,
    ReferenceCheckpointKind.TARGET_AF: ReferenceReasonCode.TARGET_AF_SOURCE_REQUIRED,
    ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW: ReferenceReasonCode.TARGET_AF_REVIEW_REQUIRED,
    ReferenceCheckpointKind.FINAL_RUN_BASIS: ReferenceReasonCode.FINAL_RUN_BASIS_REVIEW_REQUIRED,
}

_CHECKPOINT_MESSAGES = {
    ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION: "Prepare and review this AF control and its own image background.",
    ReferenceCheckpointKind.BASIS_SOURCE: "Select the spectral basis source for this run.",
    ReferenceCheckpointKind.CONTROL_STRATEGY: "Choose one shared unstained control or per-stain negative controls before file and ROI selection.",
    ReferenceCheckpointKind.REFERENCE_CONTROL_FILES: "Select canonical reference and control image artifacts.",
    ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS: "Review positive reference ROIs for the affected fluorophore.",
    ReferenceCheckpointKind.AF_REFERENCE_ROIS: "Review AF-control ROIs or explicitly choose a negative-region fallback.",
    ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS: "Review the fluorophore-negative fallback ROIs.",
    ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS: "Review additive-background ROIs for the affected reference/control pair.",
    ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS: "Explicitly select proposed signal and dark measurements.",
    ReferenceCheckpointKind.EXTRACTION_QC: "Review extraction QC and retry or reselect affected ROIs when needed.",
    ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS: "Review and accept the affected fluorophore consensus spectrum.",
    ReferenceCheckpointKind.TARGET_AF: "Select and review the target-specific autofluorescence source.",
    ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW: "Review the proposed control-derived target-AF model before final basis assembly.",
    ReferenceCheckpointKind.FINAL_RUN_BASIS: "Review the exact fixed run basis before unmixing.",
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty literal text")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _action(code: ReferenceActionCode, label: str) -> ReferenceAction:
    return ReferenceAction(code, label)


def _option(
    value: str,
    label: str,
    description: str,
    action: ReferenceActionCode,
    *,
    selected: int = 0,
    artifacts: int = 0,
    requirements: tuple[ArtifactRequirement, ...] = (),
) -> ReferenceOption:
    return ReferenceOption(
        value=value,
        label=label,
        description=description,
        action=_action(action, label),
        minimum_selected_ids=selected,
        minimum_artifacts=artifacts,
        artifact_requirements=requirements,
    )


_ABORT = _option(
    "abort",
    "Abort workflow",
    "Leave source images and accepted reusable artifacts unchanged.",
    ReferenceActionCode.ABORT,
)


def _requirements(
    *items: tuple[str, str | None],
) -> tuple[ArtifactRequirement, ...]:
    return tuple(ArtifactRequirement(role, subject) for role, subject in items)


def _options_for(
    checkpoint: ReferenceCheckpointKind, *, basis_source: str | None = None,
    source_preparation: bool = False, **values
) -> tuple[ReferenceOption, ...]:
    options = _standard_options_for(checkpoint, **values)
    if (source_preparation and (values.get("control_strategy") == "shared_unstained_control"
            or values.get("fluorophore") is not None) and checkpoint not in {
            ReferenceCheckpointKind.BASIS_SOURCE, ReferenceCheckpointKind.CONTROL_STRATEGY,
            ReferenceCheckpointKind.REFERENCE_CONTROL_FILES, ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION}):
        change = _option("revise_shared_control", "Edit AF source preparation",
            "Revise the control source and repeat its dependent fluorophore extraction.",
            ReferenceActionCode.RESELECT)
        options = options[:-1] + (change, options[-1])
    if checkpoint in {ReferenceCheckpointKind.AF_REFERENCE_ROIS,
                      ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
                      ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS}:
        change = _option("reselect_reference_rois", "Return to region editing",
                         "Edit this reference's regions and repeat the dependent sampling.",
                         ReferenceActionCode.RESELECT, selected=1)
        return options[:-1] + (change, options[-1])
    if checkpoint is ReferenceCheckpointKind.FINAL_RUN_BASIS and basis_source in {
        "phenoimager_motif_profile",
        "curated_reference_library",
    }:
        change = _option(
            "reselect_basis_source",
            "Change spectral references",
            "Return to the source choice and retain the previous reference history.",
            ReferenceActionCode.RESELECT,
        )
        return options[:-1] + (change, options[-1])
    return options


def _uses_source_preparation(decisions):
    for decision in reversed(decisions):
        if decision.response.option_value == "reselect_basis_source":
            return False
        if decision.request.checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES:
            return decision.response.option_value == "accept_reference_control_files_with_source_preparation"
    return False


def _standard_options_for(
    checkpoint: ReferenceCheckpointKind,
    *,
    fluorophore: str | None,
    fluorophores: tuple[str, ...],
    control_strategy: str | None,
    last_fluorophore: bool,
    reference_control_available: bool,
) -> tuple[ReferenceOption, ...]:
    if checkpoint is ReferenceCheckpointKind.BASIS_SOURCE:
        return (
            _option(
                "shared_cycle_reference_library",
                "Reviewed shared cycle references",
                "Use the retained, accepted reference basis for every recorded cycle.",
                ReferenceActionCode.SELECT,
            ),
            _option(
                "curated_reference_library",
                "Import numerical spectra",
                "Use a declared CSV matrix with the recorded measurement bands and fluorophore components.",
                ReferenceActionCode.SELECT,
            ),
            _option(
                "phenoimager_motif_profile",
                "PhenoImager MOTIF profile",
                "Use the exact reviewed MOTIF profile, including its Sample AF column.",
                ReferenceActionCode.SELECT,
            ),
            _option(
                "import_reference_library",
                "Imported reviewed library",
                "Use a validated reviewed portable fluorophore-library artifact.",
                ReferenceActionCode.SELECT,
                requirements=_requirements(("candidate_fluorophore_library", None)),
            ),
            _option(
                "construct_reference_library",
                "Construct a new library",
                "Build a reviewed library from canonical single-fluorophore references.",
                ReferenceActionCode.SELECT,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.CONTROL_STRATEGY:
        return (
            _option(
                "shared_unstained_control",
                "Shared unstained control",
                "Use one compatible reviewed unstained control across references.",
                ReferenceActionCode.SELECT,
            ),
            _option(
                "per_stain_negative_controls",
                "Per-stain negative controls",
                "Prepare a separate control source for every stain.",
                ReferenceActionCode.SELECT,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES:
        file_requirements = _requirements(
            *(("positive_reference_image", item) for item in fluorophores),
            *(
                (("unstained_reference_image", "shared"),)
                if control_strategy == "shared_unstained_control"
                else tuple(("negative_reference_image", item) for item in fluorophores)
            ),
        )
        return (
            *((_option(
                "accept_reference_control_files_with_source_preparation",
                "Prepare AF control sources",
                "Prepare each selected control before its stained reference.",
                ReferenceActionCode.ACCEPT, requirements=file_requirements),)
            if control_strategy in {"shared_unstained_control", "per_stain_negative_controls"} else ()),
            _option(
                "accept_reference_control_files",
                "Use selected files",
                "Continue with the explicitly identified canonical reference/control artifacts.",
                ReferenceActionCode.ACCEPT,
                requirements=file_requirements,
            ),
            _option(
                "reselect_reference_control_files",
                "Reselect files",
                "Replace only the unaccepted file selection.",
                ReferenceActionCode.RETRY,
                requirements=file_requirements,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION:
        return (
            _option("accept_shared_control_preparation", "Use AF preparation",
                "Use these reviewed AF sets and their image-specific background for the stained references.",
                ReferenceActionCode.ACCEPT,
                requirements=_requirements(("shared_control_preparation",
                    "shared" if control_strategy == "shared_unstained_control" else fluorophore))),
            _option("retry_shared_control_preparation", "Edit control regions",
                "Return to control-region selection and resample its regions and image background.", ReferenceActionCode.RETRY),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.AF_REFERENCE_ROIS:
        return (
            _option(
                "accept_af_reference_rois",
                "Accept AF ROIs",
                "Use the reviewed compatible AF-control geometry artifact.",
                ReferenceActionCode.ACCEPT,
                requirements=_requirements(("af_reference_rois",
                    "shared" if control_strategy == "shared_unstained_control" else fluorophore)),
            ),
            _option(
                "retry_af_reference_rois",
                "Retry AF ROI review",
                "Remain at this checkpoint with new evidence.",
                ReferenceActionCode.RETRY,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS:
        return (
            _option(
                "accept_signal_dark_measurements",
                "Accept explicit measurements",
                "Use the explicitly selected signal/dark measurement IDs.",
                ReferenceActionCode.ACCEPT,
                selected=1,
            ),
            _option(
                "reselect_reference_rois",
                "Reselect affected ROIs",
                "Invalidate only this fluorophore's dependent unaccepted work.",
                ReferenceActionCode.RESELECT,
                selected=1,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.EXTRACTION_QC:
        return (
            _option(
                "accept_extraction_qc",
                "Accept extraction QC",
                "Continue with the reviewed extraction evidence.",
                ReferenceActionCode.ACCEPT,
                requirements=_requirements(("extraction_qc", fluorophore)),
            ),
            _option(
                "retry_extraction",
                "Retry extraction",
                "Repeat only this checkpoint from immutable inputs.",
                ReferenceActionCode.RETRY,
            ),
            _option(
                "revise_signal_dark_measurements",
                "Change extraction bands",
                "Change this fluorophore's bands using its reviewed regions and background.",
                ReferenceActionCode.RESELECT,
                selected=1,
            ),
            _option(
                "reselect_reference_rois",
                "Reselect affected ROIs",
                "Return to positive ROI review for this fluorophore.",
                ReferenceActionCode.RESELECT,
                selected=1,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS:
        consensus_requirements = _requirements(
            ("proposed_fluorophore_consensus_qc", fluorophore),
        )
        return (
            _option(
                "accept_fluorophore_consensus",
                "Accept consensus spectrum",
                "Accept the reviewed fluorophore spectrum artifact.",
                ReferenceActionCode.ACCEPT,
                requirements=consensus_requirements,
            ),
            _option(
                "revise_signal_dark_measurements",
                "Change extraction bands",
                "Recalculate this unaccepted spectrum using its reviewed regions and background.",
                ReferenceActionCode.RESELECT,
                selected=1,
            ),
            _option(
                "reselect_reference_rois",
                "Reselect affected ROIs",
                "Return only this fluorophore to positive ROI review.",
                ReferenceActionCode.RESELECT,
                selected=1,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.TARGET_AF:
        reuse = (
            (
                _option(
                    "reuse_reference_unstained_control",
                    "Reuse reference unstained control",
                    "Reuse the validated reference unstained control for target-AF estimation.",
                    ReferenceActionCode.SELECT,
                ),
            )
            if reference_control_available
            else ()
        )
        return (
            _option(
                "review_reference_control_groups",
                "Compare AF regions",
                "Review target suitability and group retained control regions for final autofluorescence.",
                ReferenceActionCode.SELECT,
            ),
            *reuse,
            _option(
                "select_target_unstained_control",
                "Select target unstained control",
                "Use a separately validated target-matched unstained control image.",
                ReferenceActionCode.SELECT,
                requirements=_requirements(("target_unstained_control_image", None)),
            ),
            _option(
                "retry_target_af_source",
                "Retry target-AF source",
                "Remain at target-AF source review with updated evidence.",
                ReferenceActionCode.RETRY,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW:
        return (
            _option(
                "accept_control_target_af",
                "Accept control-derived target AF",
                "Use the exact validated proposed target-AF model.",
                ReferenceActionCode.ACCEPT,
                requirements=_requirements(("proposed_target_af_qc", None)),
            ),
            _option(
                "retry_control_target_af",
                "Retry control-derived target AF",
                "Repeat target-AF extraction from the selected immutable control.",
                ReferenceActionCode.RETRY,
            ),
            _option(
                "reselect_target_af_source",
                "Reselect target-AF source",
                "Return to target-AF source selection.",
                ReferenceActionCode.RESELECT,
            ),
            _ABORT,
        )
    if checkpoint is ReferenceCheckpointKind.FINAL_RUN_BASIS:
        return (
            _option(
                "accept_final_run_basis",
                "Accept final run basis",
                "Freeze the exact reviewed basis for downstream level-0 unmixing.",
                ReferenceActionCode.ACCEPT,
                requirements=_requirements(("proposed_run_basis_qc", None)),
            ),
            _ABORT,
        )
    generic_role = {
        ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS: "positive_reference_rois",
        ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS: "negative_reference_rois",
        ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS: "background_reference_rois",
    }.get(checkpoint)
    return (
        _option(
            f"accept_{checkpoint.value}",
            "Accept reviewed artifacts",
            "Continue using the explicit reviewed immutable artifact references.",
            ReferenceActionCode.ACCEPT,
            requirements=(
                () if generic_role is None else _requirements((generic_role, fluorophore))
            ),
        ),
        _option(
            f"retry_{checkpoint.value}",
            "Retry checkpoint",
            "Remain at this checkpoint with updated immutable evidence.",
            ReferenceActionCode.RETRY,
        ),
        _ABORT,
    )


def _validate_completed_transition_chain(state: "ReferenceWorkflowCheckpoint") -> None:
    if not state.decisions or not state.transition_log:
        raise ValueError("completed workflow requires a complete transition chain")
    if state.decisions[0].request.checkpoint is not ReferenceCheckpointKind.BASIS_SOURCE:
        raise ValueError("completed workflow transition chain lacks basis selection")
    if any(item.response.action is ReferenceActionCode.ABORT for item in state.decisions):
        raise ValueError("completed workflow transition chain contains abort")
    publications = tuple(
        item for item in state.transition_log if item.kind is ReferenceTransitionKind.PUBLICATION
    )
    if not publications or state.final_basis_artifact not in publications[-1].artifact_bindings:
        raise ValueError("completed workflow artifacts contradict transition chain")
    bound = {pointer for transition in publications for pointer in transition.artifact_bindings}
    required = {
        state.final_basis_artifact,
        state.accepted_library,
        state.target_af_artifact,
        *(item.accepted_spectrum for item in state.fluorophore_artifacts),
    }
    if any(pointer is not None and pointer not in bound for pointer in required):
        raise ValueError("completed workflow artifacts contradict transition chain")


@dataclass(frozen=True, slots=True)
class FluorophoreArtifactSlot:
    fluorophore: str
    accepted_spectrum: ArtifactPointer | None = None

    def __post_init__(self) -> None:
        _required_text(self.fluorophore, "fluorophore artifact slot")
        if (
            self.accepted_spectrum is not None
            and type(self.accepted_spectrum) is not ArtifactPointer
        ):
            raise TypeError("accepted fluorophore spectrum binding is invalid")
        if self.accepted_spectrum is not None and (
            self.accepted_spectrum.role != "accepted_fluorophore_spectrum"
            or self.accepted_spectrum.subject_id != self.fluorophore
        ):
            raise ValueError("accepted spectrum must bind its exact fluorophore")

    def to_mapping(self) -> dict[str, object]:
        return {
            "fluorophore": self.fluorophore,
            "accepted_spectrum": (
                None if self.accepted_spectrum is None else self.accepted_spectrum.to_mapping()
            ),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "FluorophoreArtifactSlot":
        if not isinstance(value, dict) or set(value) != {
            "fluorophore",
            "accepted_spectrum",
        }:
            raise ValueError("fluorophore artifact slot schema fields differ")
        raw = value["accepted_spectrum"]
        return cls(
            fluorophore=value["fluorophore"],  # type: ignore[arg-type]
            accepted_spectrum=(None if raw is None else ArtifactPointer.from_mapping(raw)),
        )


@dataclass(frozen=True, slots=True)
class ReferenceDecisionRecord:
    source_checkpoint_sha256: str
    request: ReferenceInteractionRequest
    response: ReferenceResponse
    content_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.source_checkpoint_sha256, "decision source checkpoint SHA-256")
        if type(self.request) is not ReferenceInteractionRequest:
            raise TypeError("decision request is invalid")
        if type(self.response) is not ReferenceResponse:
            raise TypeError("decision response is invalid")
        validate_reference_response(self.request, self.response)
        _sha256(self.content_sha256, "decision record content SHA-256")
        if self.content_sha256 != _digest(self._content_mapping()):
            raise ValueError("decision record content SHA-256 does not match")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "source_checkpoint_sha256": self.source_checkpoint_sha256,
            "request": self.request.to_mapping(),
            "response": self.response.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    @classmethod
    def create(
        cls,
        *,
        source_checkpoint_sha256: str,
        request: ReferenceInteractionRequest,
        response: ReferenceResponse,
    ) -> "ReferenceDecisionRecord":
        content = {
            "source_checkpoint_sha256": source_checkpoint_sha256,
            "request": request.to_mapping(),
            "response": response.to_mapping(),
        }
        return cls(
            source_checkpoint_sha256=source_checkpoint_sha256,
            request=request,
            response=response,
            content_sha256=_digest(content),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceDecisionRecord":
        if not isinstance(value, dict) or set(value) != {
            "source_checkpoint_sha256",
            "request",
            "response",
            "content_sha256",
        }:
            raise ValueError("reference decision record schema fields differ")
        return cls(
            source_checkpoint_sha256=value["source_checkpoint_sha256"],  # type: ignore[arg-type]
            request=ReferenceInteractionRequest.from_mapping(value["request"]),
            response=ReferenceResponse.from_mapping(value["response"]),
            content_sha256=value["content_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReferenceTransitionRecord:
    kind: ReferenceTransitionKind
    revision: int
    source_checkpoint_sha256: str
    source_state_sha256: str
    target_state_sha256: str
    source_state_json: str
    target_state_json: str
    previous_transition_sha256: str | None
    request_id: str | None
    decision_record_sha256: str | None
    affected_fluorophore: str | None
    superseded_request_ids: tuple[str, ...]
    artifact_bindings: tuple[ArtifactPointer, ...]
    publication_evidence: tuple[PublishedArtifactEvidence, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        """Validate the ReferenceTransitionRecord fields when the record is constructed.

        The checks cover ``superseded_request_ids``, ``source_checkpoint_sha256``,
        ``source_state_sha256``, ``target_state_sha256``, ``source_state_json``,
        ``target_state_json``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if not isinstance(self.kind, ReferenceTransitionKind):
            raise TypeError("reference transition kind is invalid")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("reference transition revision must be nonnegative")
        _sha256(self.source_checkpoint_sha256, "transition source checkpoint SHA-256")
        _sha256(self.source_state_sha256, "transition source state SHA-256")
        _sha256(self.target_state_sha256, "transition target state SHA-256")
        for raw, expected, name in (
            (self.source_state_json, self.source_state_sha256, "source"),
            (self.target_state_json, self.target_state_sha256, "target"),
        ):
            if not isinstance(raw, str):
                raise TypeError(f"transition {name} state JSON must be text")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"transition {name} state JSON is invalid") from exc
            if _canonical_json(parsed).decode("utf-8") != raw:
                raise ValueError(f"transition {name} state JSON is not canonical")
            if _digest(parsed) != expected:
                raise ValueError(f"transition {name} state evidence does not match its SHA-256")
        source_state = json.loads(self.source_state_json)
        target_state = json.loads(self.target_state_json)
        if target_state.get("prior_checkpoint_sha256") != self.source_checkpoint_sha256:
            raise ValueError(
                "transition source checkpoint SHA-256 does not match target prior checkpoint"
            )
        source_revision = source_state.get("revision")
        target_revision = target_state.get("revision")
        if (
            type(source_revision) is not int
            or type(target_revision) is not int
            or target_revision != source_revision + 1
            or self.revision != target_revision
        ):
            raise ValueError("transition source/target revision continuity is invalid")
        identity_fields = (
            "run_id",
            "target_image_sha256",
            "measurement_ids",
            "af_behavior",
        )
        if source_state.get("fluorophores") != target_state.get("fluorophores"):
            if (self.kind is not ReferenceTransitionKind.DECISION
                    or source_state.get("fluorophores")
                    or source_state.get("checkpoint") != "basis_source"
                    or target_state.get("basis_source") != "construct_reference_library"):
                raise ValueError("transition component declaration is invalid")
        if any(source_state.get(field) != target_state.get(field) for field in identity_fields):
            raise ValueError("transition source/target identity continuity is invalid")
        if self.previous_transition_sha256 is not None:
            _sha256(self.previous_transition_sha256, "previous transition SHA-256")
        if self.request_id is not None:
            _sha256(self.request_id, "transition request ID")
        if self.decision_record_sha256 is not None:
            _sha256(self.decision_record_sha256, "transition decision record SHA-256")
        if self.affected_fluorophore is not None:
            _required_text(self.affected_fluorophore, "transition affected fluorophore")
        if type(self.superseded_request_ids) is not tuple or any(
            not isinstance(item, str) for item in self.superseded_request_ids
        ):
            raise TypeError("transition superseded request IDs are invalid")
        for item in self.superseded_request_ids:
            _sha256(item, "superseded request ID")
        if type(self.artifact_bindings) is not tuple or any(
            type(item) is not ArtifactPointer for item in self.artifact_bindings
        ):
            raise TypeError("transition artifact bindings are invalid")
        invalidating = self.kind in {ReferenceTransitionKind.INVALIDATION, ReferenceTransitionKind.PREPARATION_REVISION}
        if invalidating and not self.superseded_request_ids:
            raise ValueError("invalidation transition requires explicit superseded lineage")
        if not invalidating and self.superseded_request_ids:
            raise ValueError("only invalidation transitions supersede request lineage")
        if self.kind is ReferenceTransitionKind.PUBLICATION and not self.artifact_bindings:
            raise ValueError("publication transition requires accepted artifact bindings")
        if self.kind is not ReferenceTransitionKind.PUBLICATION and self.artifact_bindings:
            raise ValueError("only publication transitions bind accepted artifacts")
        if self.kind is ReferenceTransitionKind.DECISION:
            if self.decision_record_sha256 is None:
                raise ValueError("decision transition requires exact decision record binding")
        elif self.decision_record_sha256 is not None:
            raise ValueError("only decision transitions bind decision records")
        if type(self.publication_evidence) is not tuple or any(
            type(item) is not PublishedArtifactEvidence for item in self.publication_evidence
        ):
            raise TypeError("transition publication evidence is invalid")
        if self.kind is ReferenceTransitionKind.PUBLICATION:
            if (
                not self.publication_evidence
                or tuple(item.pointer for item in self.publication_evidence)
                != self.artifact_bindings
            ):
                raise ValueError("publication transition lacks exact typed provenance")
        elif self.publication_evidence:
            raise ValueError("only publication transitions retain publication provenance")
        _sha256(self.content_sha256, "transition content SHA-256")
        if self.content_sha256 != _digest(self._content_mapping()):
            raise ValueError("transition content SHA-256 does not match")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "revision": self.revision,
            "source_checkpoint_sha256": self.source_checkpoint_sha256,
            "source_state_sha256": self.source_state_sha256,
            "target_state_sha256": self.target_state_sha256,
            "source_state_json": self.source_state_json,
            "target_state_json": self.target_state_json,
            "previous_transition_sha256": self.previous_transition_sha256,
            "request_id": self.request_id,
            "decision_record_sha256": self.decision_record_sha256,
            "affected_fluorophore": self.affected_fluorophore,
            "superseded_request_ids": list(self.superseded_request_ids),
            "artifact_bindings": [item.to_mapping() for item in self.artifact_bindings],
            "publication_evidence": [item.to_mapping() for item in self.publication_evidence],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    @classmethod
    def create(
        cls,
        *,
        kind: ReferenceTransitionKind,
        revision: int,
        source_checkpoint_sha256: str,
        source_state_sha256: str,
        target_state_sha256: str,
        source_state_json: str,
        target_state_json: str,
        previous_transition_sha256: str | None,
        request_id: str | None,
        decision_record_sha256: str | None,
        affected_fluorophore: str | None,
        superseded_request_ids: tuple[str, ...] = (),
        artifact_bindings: tuple[ArtifactPointer, ...] = (),
        publication_evidence: tuple[PublishedArtifactEvidence, ...] = (),
    ) -> "ReferenceTransitionRecord":
        content = {
            "kind": kind.value,
            "revision": revision,
            "source_checkpoint_sha256": source_checkpoint_sha256,
            "source_state_sha256": source_state_sha256,
            "target_state_sha256": target_state_sha256,
            "source_state_json": source_state_json,
            "target_state_json": target_state_json,
            "previous_transition_sha256": previous_transition_sha256,
            "request_id": request_id,
            "decision_record_sha256": decision_record_sha256,
            "affected_fluorophore": affected_fluorophore,
            "superseded_request_ids": list(superseded_request_ids),
            "artifact_bindings": [item.to_mapping() for item in artifact_bindings],
            "publication_evidence": [item.to_mapping() for item in publication_evidence],
        }
        return cls(
            kind=kind,
            revision=revision,
            source_checkpoint_sha256=source_checkpoint_sha256,
            source_state_sha256=source_state_sha256,
            target_state_sha256=target_state_sha256,
            source_state_json=source_state_json,
            target_state_json=target_state_json,
            previous_transition_sha256=previous_transition_sha256,
            request_id=request_id,
            decision_record_sha256=decision_record_sha256,
            affected_fluorophore=affected_fluorophore,
            superseded_request_ids=superseded_request_ids,
            artifact_bindings=artifact_bindings,
            publication_evidence=publication_evidence,
            content_sha256=_digest(content),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceTransitionRecord":
        """Decode the serialized fields into a validated ReferenceTransitionRecord record.

        The mapping is checked against the declared field inventory, including ``kind``,
        ``revision``, ``source_checkpoint_sha256``, ``source_state_sha256``,
        ``target_state_sha256``, ``source_state_json``. Nested values are decoded through
        the corresponding field validators before the record is constructed."""

        expected = {
            "kind",
            "revision",
            "source_checkpoint_sha256",
            "source_state_sha256",
            "target_state_sha256",
            "source_state_json",
            "target_state_json",
            "previous_transition_sha256",
            "request_id",
            "decision_record_sha256",
            "affected_fluorophore",
            "superseded_request_ids",
            "artifact_bindings",
            "content_sha256",
            "publication_evidence",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("reference transition schema fields differ")
        if not isinstance(value["superseded_request_ids"], list) or not isinstance(
            value["artifact_bindings"], list
        ):
            raise ValueError("reference transition arrays are invalid")
        if not isinstance(value["publication_evidence"], list):
            raise ValueError("reference transition publication evidence is invalid")
        try:
            kind = ReferenceTransitionKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference transition kind is unsupported") from exc
        return cls(
            kind=kind,
            revision=value["revision"],  # type: ignore[arg-type]
            source_checkpoint_sha256=value["source_checkpoint_sha256"],  # type: ignore[arg-type]
            source_state_sha256=value["source_state_sha256"],  # type: ignore[arg-type]
            target_state_sha256=value["target_state_sha256"],  # type: ignore[arg-type]
            source_state_json=value["source_state_json"],  # type: ignore[arg-type]
            target_state_json=value["target_state_json"],  # type: ignore[arg-type]
            previous_transition_sha256=value["previous_transition_sha256"],  # type: ignore[arg-type]
            request_id=value["request_id"],  # type: ignore[arg-type]
            decision_record_sha256=value["decision_record_sha256"],  # type: ignore[arg-type]
            affected_fluorophore=value["affected_fluorophore"],  # type: ignore[arg-type]
            superseded_request_ids=tuple(value["superseded_request_ids"]),
            artifact_bindings=tuple(
                ArtifactPointer.from_mapping(item) for item in value["artifact_bindings"]
            ),
            publication_evidence=tuple(
                PublishedArtifactEvidence.from_mapping(item)
                for item in value["publication_evidence"]
            ),
            content_sha256=value["content_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PendingPublication:
    publication_kind: str
    review_request_id: str
    affected_fluorophore: str | None
    proposal_artifacts: tuple[ArtifactPointer, ...]
    required_artifacts: tuple[ArtifactRequirement, ...]

    def __post_init__(self) -> None:
        _required_text(self.publication_kind, "pending publication kind")
        _sha256(self.review_request_id, "pending publication review request ID")
        if self.affected_fluorophore is not None:
            _required_text(self.affected_fluorophore, "pending publication fluorophore")
        if not self.proposal_artifacts or any(
            type(item) is not ArtifactPointer for item in self.proposal_artifacts
        ):
            raise ValueError("pending publication requires proposal artifacts")
        if not self.required_artifacts or any(
            type(item) is not ArtifactRequirement for item in self.required_artifacts
        ):
            raise ValueError("pending publication requires exact output bindings")

    def to_mapping(self) -> dict[str, object]:
        return {
            "publication_kind": self.publication_kind,
            "review_request_id": self.review_request_id,
            "affected_fluorophore": self.affected_fluorophore,
            "proposal_artifacts": [item.to_mapping() for item in self.proposal_artifacts],
            "required_artifacts": [item.to_mapping() for item in self.required_artifacts],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "PendingPublication":
        if not isinstance(value, dict) or set(value) != {
            "publication_kind",
            "review_request_id",
            "affected_fluorophore",
            "proposal_artifacts",
            "required_artifacts",
        }:
            raise ValueError("pending publication schema fields differ")
        if not isinstance(value["proposal_artifacts"], list) or not isinstance(
            value["required_artifacts"], list
        ):
            raise ValueError("pending publication arrays are invalid")
        return cls(
            publication_kind=value["publication_kind"],  # type: ignore[arg-type]
            review_request_id=value["review_request_id"],  # type: ignore[arg-type]
            affected_fluorophore=value["affected_fluorophore"],  # type: ignore[arg-type]
            proposal_artifacts=tuple(
                ArtifactPointer.from_mapping(item) for item in value["proposal_artifacts"]
            ),
            required_artifacts=tuple(
                ArtifactRequirement.from_mapping(item) for item in value["required_artifacts"]
            ),
        )


def _validate_decision_semantic_replay(
    record: ReferenceDecisionRecord,
    transition: ReferenceTransitionRecord,
) -> None:
    """Replay one response over semantic state without adapter/UI inputs."""
    source = json.loads(transition.source_state_json)
    target = json.loads(transition.target_state_json)
    decision = record.response
    checkpoint = record.request.checkpoint
    expected = dict(source)
    expected.update(
        revision=source["revision"] + 1,
        prior_checkpoint_sha256=transition.source_checkpoint_sha256,
        pending_publication=None,
        final_basis_artifact=None,
    )

    def next_request(
        next_checkpoint: ReferenceCheckpointKind,
        *,
        fluorophore: str | None = None,
    ) -> None:
        expected["status"] = ReferenceWorkflowStatus.AWAITING_RESPONSE.value
        expected["checkpoint"] = next_checkpoint.value
        expected["current_fluorophore"] = fluorophore

    def pending(
        kind: str,
        requirements: tuple[ArtifactRequirement, ...],
        *,
        fluorophore: str | None = None,
    ) -> None:
        expected["status"] = ReferenceWorkflowStatus.AWAITING_PUBLICATION.value
        expected["checkpoint"] = None
        expected["current_fluorophore"] = fluorophore
        expected["reason_code"] = None
        expected["evidence"] = []
        expected["proposed_artifacts"] = []
        expected["pending_publication"] = PendingPublication(
            publication_kind=kind,
            review_request_id=decision.request_id,
            affected_fluorophore=fluorophore,
            proposal_artifacts=decision.artifacts,
            required_artifacts=requirements,
        ).to_mapping()

    if decision.action is ReferenceActionCode.ABORT:
        expected.update(
            status=ReferenceWorkflowStatus.ABORTED.value,
            checkpoint=None,
            current_fluorophore=None,
            reason_code=None,
            evidence=[],
            proposed_artifacts=[],
        )
    elif decision.action is ReferenceActionCode.RETRY:
        next_request(checkpoint, fluorophore=source["current_fluorophore"])
    elif decision.action is ReferenceActionCode.RESELECT:
        if decision.option_value == "revise_shared_control":
            shared = source["control_strategy"] == "shared_unstained_control"
            affected = None if shared else source["current_fluorophore"]
            expected["fluorophore_artifacts"] = [FluorophoreArtifactSlot(slot["fluorophore"]).to_mapping()
                if shared or slot["fluorophore"] == affected else slot
                for slot in source["fluorophore_artifacts"]]
            expected["accepted_library"] = expected["target_af_artifact"] = None
            next_request(ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION, fluorophore=affected)
        elif decision.option_value == "reselect_basis_source":
            if checkpoint is not ReferenceCheckpointKind.FINAL_RUN_BASIS:
                raise ValueError("Source changes require an unfinished final reference review")
            expected.update(
                basis_source=None,
                control_strategy=None,
                available_reference_control=None,
                target_control_compatibility=None,
                accepted_library=None,
                target_af_artifact=None,
            )
            expected["fluorophore_artifacts"] = [
                FluorophoreArtifactSlot(slot["fluorophore"]).to_mapping()
                for slot in source["fluorophore_artifacts"]
            ]
            next_request(ReferenceCheckpointKind.BASIS_SOURCE)
        elif decision.option_value == "revise_signal_dark_measurements":
            next_request(
                ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
                fluorophore=source["current_fluorophore"],
            )
        elif checkpoint in {
            ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
        }:
            expected["target_af_artifact"] = None
            next_request(ReferenceCheckpointKind.TARGET_AF)
        else:
            fluorophore = record.request.fluorophore
            expected["fluorophore_artifacts"] = [
                {
                    **slot,
                    "accepted_spectrum": (
                        None if slot["fluorophore"] == fluorophore else slot["accepted_spectrum"]
                    ),
                }
                for slot in source["fluorophore_artifacts"]
            ]
            expected["accepted_library"] = None
            expected["target_af_artifact"] = None
            next_request(
                ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
                fluorophore=fluorophore,
            )
    elif checkpoint is ReferenceCheckpointKind.BASIS_SOURCE:
        expected["basis_source"] = decision.option_value
        if decision.option_value in {"phenoimager_motif_profile", "curated_reference_library", "shared_cycle_reference_library"}:
            next_request(ReferenceCheckpointKind.FINAL_RUN_BASIS)
        elif decision.option_value == "construct_reference_library":
            if not source["fluorophores"]:
                expected["fluorophores"] = list(decision.selected_ids)
                expected["fluorophore_artifacts"] = [
                    FluorophoreArtifactSlot(name).to_mapping() for name in decision.selected_ids
                ]
            next_request(ReferenceCheckpointKind.CONTROL_STRATEGY)
        elif decision.option_value == "import_reference_library":
            pending(
                "import_library",
                _requirements(("accepted_fluorophore_library", None)),
            )
        else:
            raise ValueError("decision replay has unsupported basis source")
    elif checkpoint is ReferenceCheckpointKind.CONTROL_STRATEGY:
        expected["control_strategy"] = decision.option_value
        next_request(ReferenceCheckpointKind.REFERENCE_CONTROL_FILES)
    elif checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES:
        if source["control_strategy"] == "shared_unstained_control":
            expected["available_reference_control"] = next(
                item.to_mapping()
                for item in decision.artifacts
                if item.role == "unstained_reference_image"
            )
            evidence = source.get("target_control_compatibility")
            if evidence is not None and (
                evidence["control_artifact"] != expected["available_reference_control"]
            ):
                expected["target_control_compatibility"] = None
        else:
            expected["available_reference_control"] = None
            expected["target_control_compatibility"] = None
        if decision.option_value == "accept_reference_control_files_with_source_preparation":
            next_request(ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION,
                fluorophore=None if source["control_strategy"] == "shared_unstained_control" else source["fluorophores"][0])
        else:
            next_request(ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
                         fluorophore=source["fluorophores"][0])
    elif checkpoint is ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION:
        next_request(ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
                     fluorophore=source["current_fluorophore"] or source["fluorophores"][0])
    elif checkpoint is ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS:
        next_request(
            (
                ReferenceCheckpointKind.AF_REFERENCE_ROIS
                if source["control_strategy"] == "shared_unstained_control"
                    or any(item.value == "revise_shared_control" for item in record.request.options)
                else ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS
            ),
            fluorophore=source["current_fluorophore"],
        )
    elif checkpoint in {
        ReferenceCheckpointKind.AF_REFERENCE_ROIS,
        ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
    }:
        next_request(
            ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS,
            fluorophore=source["current_fluorophore"],
        )
    elif checkpoint is ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS:
        next_request(
            ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
            fluorophore=source["current_fluorophore"],
        )
    elif checkpoint is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS:
        next_request(
            ReferenceCheckpointKind.EXTRACTION_QC,
            fluorophore=source["current_fluorophore"],
        )
    elif checkpoint is ReferenceCheckpointKind.EXTRACTION_QC:
        next_request(
            ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS,
            fluorophore=source["current_fluorophore"],
        )
    elif checkpoint is ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS:
        fluorophore = source["current_fluorophore"]
        last = all(
            slot["accepted_spectrum"] is not None or slot["fluorophore"] == fluorophore
            for slot in source["fluorophore_artifacts"]
        )
        pending(
            "fluorophore_consensus",
            _requirements(
                ("accepted_fluorophore_spectrum", fluorophore),
                *(("accepted_fluorophore_library", None),) if last else (),
            ),
            fluorophore=fluorophore,
        )
    elif checkpoint is ReferenceCheckpointKind.TARGET_AF:
        next_request(ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW)
    elif checkpoint in {
        ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
    }:
        pending("target_af", _requirements(("accepted_target_af", None)))
    elif checkpoint is ReferenceCheckpointKind.FINAL_RUN_BASIS:
        pending("final_basis", _requirements(("final_run_basis", None)))
    else:  # pragma: no cover - exhaustive enum guard
        raise ValueError("decision replay checkpoint is unsupported")

    adapter_supplied = {"reason_code", "evidence", "proposed_artifacts"}
    for field, value in expected.items():
        if field not in adapter_supplied and target.get(field) != value:
            raise ValueError(f"decision semantic replay target differs for {field}")


@dataclass(frozen=True, slots=True)
class ReferenceWorkflowCheckpoint:
    """An immutable replay-safe workflow snapshot, never a pixel container."""

    SCHEMA_NAME = "mif_pipeline.spectral_reference_workflow_checkpoint"
    SCHEMA_VERSION = 1

    run_id: str
    target_image_sha256: str
    fluorophores: tuple[str, ...]
    measurement_ids: tuple[str, ...]
    af_behavior: str
    revision: int
    status: ReferenceWorkflowStatus
    basis_source: str | None
    control_strategy: str | None
    current_fluorophore: str | None
    fluorophore_artifacts: tuple[FluorophoreArtifactSlot, ...]
    accepted_library: ArtifactPointer | None
    available_reference_control: ArtifactPointer | None
    target_control_compatibility: TargetControlCompatibilityEvidence | None
    target_af_artifact: ArtifactPointer | None
    final_basis_artifact: ArtifactPointer | None
    decisions: tuple[ReferenceDecisionRecord, ...]
    transition_log: tuple[ReferenceTransitionRecord, ...]
    pending_publication: PendingPublication | None
    request: ReferenceInteractionRequest | None
    prior_checkpoint_sha256: str | None
    content_sha256: str

    def __post_init__(self) -> None:
        """Validate the ReferenceWorkflowCheckpoint fields when the record is constructed.

        The checks cover ``fluorophores``, ``measurement_ids``, ``decisions``,
        ``transition_log``, ``current_checkpoint``, ``run_id``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        _required_text(self.run_id, "workflow run ID")
        if self.af_behavior not in {"retain", "already_removed"}:
            raise ValueError("unsupported target AF choice")
        if self.af_behavior == "already_removed" and self.target_af_artifact is not None:
            raise ValueError("an AF-free target cannot carry a target AF artifact")
        _sha256(self.target_image_sha256, "target image SHA-256")
        if type(self.fluorophores) is not tuple:
            raise ValueError("workflow requires an ordered fluorophore inventory")
        for fluorophore in self.fluorophores:
            _required_text(fluorophore, "workflow fluorophore")
        if len(set(self.fluorophores)) != len(self.fluorophores):
            raise ValueError("workflow fluorophores must be unique")
        if type(self.measurement_ids) is not tuple or not self.measurement_ids:
            raise ValueError("workflow requires an ordered measurement inventory")
        for item in self.measurement_ids:
            _required_text(item, "workflow measurement ID")
        if len(set(self.measurement_ids)) != len(self.measurement_ids):
            raise ValueError("workflow measurement IDs must be unique")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("workflow revision must be nonnegative")
        if not isinstance(self.status, ReferenceWorkflowStatus):
            raise TypeError("workflow status is invalid")
        if self.basis_source is not None:
            _required_text(self.basis_source, "workflow basis source")
        if self.control_strategy not in {
            None,
            "shared_unstained_control",
            "per_stain_negative_controls",
        }:
            raise ValueError("workflow control strategy is unsupported")
        if (
            self.current_fluorophore is not None
            and self.current_fluorophore not in self.fluorophores
        ):
            raise ValueError("current fluorophore is outside the workflow inventory")
        if type(self.fluorophore_artifacts) is not tuple or any(
            type(item) is not FluorophoreArtifactSlot for item in self.fluorophore_artifacts
        ):
            raise TypeError("fluorophore artifact slots are invalid")
        if tuple(item.fluorophore for item in self.fluorophore_artifacts) != self.fluorophores:
            raise ValueError("fluorophore artifact slots must exactly match panel order")
        for pointer in (
            self.accepted_library,
            self.available_reference_control,
            self.target_af_artifact,
            self.final_basis_artifact,
        ):
            if pointer is not None and type(pointer) is not ArtifactPointer:
                raise TypeError("workflow artifact binding is invalid")
        if self.available_reference_control is not None and (
            self.available_reference_control.role != "unstained_reference_image"
        ):
            raise ValueError("available reference control must bind an unstained image")
        if self.target_control_compatibility is not None:
            if type(self.target_control_compatibility) is not TargetControlCompatibilityEvidence:
                raise TypeError("workflow target-control compatibility evidence is invalid")
            if self.target_control_compatibility.target_image_sha256 != self.target_image_sha256:
                raise ValueError("target-control evidence does not bind workflow target image")
            compatibility_control = self.target_control_compatibility.control_artifact
            if (
                compatibility_control.role == "unstained_reference_image"
                and compatibility_control != self.available_reference_control
            ):
                raise ValueError("target-control evidence does not bind available control bytes")
        if type(self.decisions) is not tuple or any(
            type(item) is not ReferenceDecisionRecord for item in self.decisions
        ):
            raise TypeError("workflow decisions are invalid")
        if type(self.transition_log) is not tuple or any(
            type(item) is not ReferenceTransitionRecord for item in self.transition_log
        ):
            raise TypeError("workflow transition log is invalid")
        if len(self.transition_log) > _MAX_TRANSITIONS or len(self.decisions) > _MAX_TRANSITIONS:
            raise ValueError("workflow transition history exceeds versioned v1 limit")
        if (
            self.pending_publication is not None
            and type(self.pending_publication) is not PendingPublication
        ):
            raise TypeError("workflow pending publication is invalid")
        if self.prior_checkpoint_sha256 is not None:
            _sha256(self.prior_checkpoint_sha256, "prior checkpoint SHA-256")
        if self.status is ReferenceWorkflowStatus.AWAITING_RESPONSE:
            if type(self.request) is not ReferenceInteractionRequest:
                raise ValueError("awaiting workflow requires one request")
            if (
                self.request.run_id != self.run_id
                or self.request.target_image_sha256 != self.target_image_sha256
                or self.request.fluorophores != self.fluorophores
                or self.request.measurement_ids != self.measurement_ids
                or self.request.target_control_compatibility != self.target_control_compatibility
                or self.request.authoritative_state_sha256
                != _digest(self._authoritative_state_mapping())
                or self.request.revision != self.revision
            ):
                raise ValueError("workflow request is stale relative to authoritative state")
            if self.request.prior_checkpoint_sha256 != self.prior_checkpoint_sha256:
                raise ValueError("workflow request lineage does not match")
            if self.request.fluorophore != self.current_fluorophore:
                raise ValueError("workflow request fluorophore does not match")
            if self.pending_publication is not None:
                raise ValueError("response state cannot also await publication")
        elif self.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION:
            if self.request is not None or self.pending_publication is None:
                raise ValueError("publication state requires only pending publication evidence")
        elif (
            self.request is not None
            or self.current_fluorophore is not None
            or self.pending_publication is not None
        ):
            raise ValueError("terminal workflow state cannot retain live work")
        if self.status is ReferenceWorkflowStatus.COMPLETED and self.final_basis_artifact is None:
            raise ValueError("completed workflow requires an accepted final basis artifact")
        allowed_sources = {
            "shared_cycle_reference_library",
            "curated_reference_library",
            "phenoimager_motif_profile",
            "import_reference_library",
            "construct_reference_library",
        }
        if self.basis_source is not None and self.basis_source not in allowed_sources:
            raise ValueError("workflow basis source is unsupported")
        if self.basis_source != "construct_reference_library" and self.control_strategy is not None:
            raise ValueError("only constructed libraries use a reference-control strategy")
        if self.revision < len(self.decisions):
            raise ValueError("workflow revision precedes its decision history")
        decision_revisions = tuple(item.response.request_revision for item in self.decisions)
        if decision_revisions != tuple(sorted(decision_revisions)) or len(
            set(item.response.request_id for item in self.decisions)
        ) != len(self.decisions):
            raise ValueError("workflow decision history is reordered or duplicated")
        if any(item.response.request_revision >= self.revision for item in self.decisions):
            raise ValueError("workflow decision history contains a future response")
        declarations = tuple(
            index for index, item in enumerate(self.decisions)
            if item.request.checkpoint is ReferenceCheckpointKind.BASIS_SOURCE
            and item.response.option_value == "construct_reference_library"
            and item.response.selections
        )
        if len(declarations) > 1 or (declarations and
                self.decisions[declarations[0]].response.selected_ids != self.fluorophores):
            raise ValueError("workflow component declaration differs from its panel")
        declaration_index = declarations[0] if declarations else -1
        for index, item in enumerate(self.decisions):
            if (
                item.request.run_id != self.run_id
                or item.request.target_image_sha256 != self.target_image_sha256
                or (item.request.fluorophores != self.fluorophores
                    and not (not item.request.fluorophores and index <= declaration_index))
                or item.request.measurement_ids != self.measurement_ids
            ):
                raise ValueError("workflow decision history crosses run/image/panel identity")
        previous_transition: ReferenceTransitionRecord | None = None
        for transition in self.transition_log:
            expected_previous = (
                None if previous_transition is None else previous_transition.content_sha256
            )
            if transition.previous_transition_sha256 != expected_previous:
                raise ValueError("workflow transition hash chain is broken")
            if previous_transition is not None and (
                previous_transition.target_state_sha256 != transition.source_state_sha256
                or previous_transition.target_state_json != transition.source_state_json
            ):
                raise ValueError("workflow transition state chain is broken")
            if transition.kind is ReferenceTransitionKind.DECISION:
                matches = tuple(
                    item
                    for item in self.decisions
                    if item.request.request_id == transition.request_id
                )
                if (
                    len(matches) != 1
                    or (
                        matches[0].request.authoritative_state_sha256
                        != transition.source_state_sha256
                    )
                    or (matches[0].source_checkpoint_sha256 != transition.source_checkpoint_sha256)
                    or (matches[0].content_sha256 != transition.decision_record_sha256)
                ):
                    raise ValueError(
                        "decision transition does not bind its historical request state"
                    )
                _validate_decision_semantic_replay(matches[0], transition)
            elif transition.kind is ReferenceTransitionKind.PUBLICATION:
                source_state = json.loads(transition.source_state_json)
                pending = source_state.get("pending_publication")
                if not isinstance(pending, dict) or (
                    pending.get("review_request_id") != transition.request_id
                ):
                    raise ValueError("publication transition lacks pending review lineage")
                proposal_mappings = pending.get("proposal_artifacts")
                requirement_mappings = pending.get("required_artifacts")
                if (
                    not isinstance(proposal_mappings, list)
                    or not isinstance(requirement_mappings, list)
                    or any(
                        item.review_request_id != transition.request_id
                        or [pointer.to_mapping() for pointer in item.proposal_artifacts]
                        != proposal_mappings
                        for item in transition.publication_evidence
                    )
                    or [
                        {
                            "role": pointer.role,
                            "subject_id": pointer.subject_id,
                        }
                        for pointer in transition.artifact_bindings
                    ]
                    != requirement_mappings
                ):
                    raise ValueError("publication transition provenance contradicts pending review")
            previous_transition = transition
        if self.revision == 0 and self.prior_checkpoint_sha256 is not None:
            raise ValueError("initial workflow checkpoint cannot declare prior lineage")
        if self.revision > 0 and self.prior_checkpoint_sha256 is None:
            raise ValueError("resumed workflow checkpoint requires prior lineage")

        current = self.current_checkpoint
        fluorophore_scoped = {
            ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
            ReferenceCheckpointKind.AF_REFERENCE_ROIS,
            ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
            ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS,
            ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
            ReferenceCheckpointKind.EXTRACTION_QC,
            ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS,
        }
        if current in fluorophore_scoped:
            if (
                self.basis_source != "construct_reference_library"
                or self.current_fluorophore is None
            ):
                raise ValueError("fluorophore checkpoints require the constructed-library branch")
        elif (
            self.current_fluorophore is not None
            and self.status is not ReferenceWorkflowStatus.AWAITING_PUBLICATION
            and not (current is ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION
                and self.control_strategy == "per_stain_negative_controls"
                and self.current_fluorophore in self.fluorophores)
        ):
            raise ValueError("non-fluorophore checkpoint cannot carry a current fluorophore")

        if self.basis_source in {"phenoimager_motif_profile", "curated_reference_library", "shared_cycle_reference_library"}:
            if self.accepted_fluorophores or self.accepted_library is not None:
                raise ValueError(
                    "MOTIF branch cannot claim constructed/imported fluorophore acceptance"
                )
            if self.target_af_artifact is not None:
                raise ValueError("MOTIF branch uses its exact Sample AF column")
            if current not in {None, ReferenceCheckpointKind.FINAL_RUN_BASIS}:
                raise ValueError("MOTIF branch has an illegal checkpoint order")
        elif self.basis_source == "import_reference_library":
            if self.accepted_fluorophores:
                raise ValueError("imported-library branch cannot claim constructed spectra")
            if self.accepted_library is None and not (
                self.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
                and self.pending_publication is not None
                and self.pending_publication.publication_kind == "import_library"
            ):
                raise ValueError("imported-library branch requires an accepted library artifact")
            if current not in {
                None,
                ReferenceCheckpointKind.TARGET_AF,
                ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
                ReferenceCheckpointKind.FINAL_RUN_BASIS,
            }:
                raise ValueError("imported-library branch has an illegal checkpoint order")
        elif self.basis_source == "construct_reference_library":
            if current is ReferenceCheckpointKind.CONTROL_STRATEGY:
                if self.control_strategy is not None:
                    raise ValueError("control strategy must be selected at its checkpoint")
            elif current is not None and self.control_strategy is None:
                raise ValueError("constructed-library branch requires a control strategy")
            if (
                self.accepted_library is not None
                and self.accepted_fluorophores != self.fluorophores
            ):
                raise ValueError("constructed accepted library requires every fluorophore")
            if (
                current
                in {
                    ReferenceCheckpointKind.TARGET_AF,
                    ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
                    ReferenceCheckpointKind.FINAL_RUN_BASIS,
                }
                and self.accepted_library is None
            ):
                raise ValueError("target AF/final basis requires an accepted fluorophore library")
        elif self.basis_source is None:
            if current not in {None, ReferenceCheckpointKind.BASIS_SOURCE}:
                raise ValueError("unselected basis source has an illegal checkpoint")
            if (
                self.accepted_fluorophores
                or self.accepted_library is not None
                or self.target_af_artifact is not None
            ):
                raise ValueError("unselected basis source cannot carry accepted artifacts")
        if (
            current is ReferenceCheckpointKind.FINAL_RUN_BASIS
            and self.basis_source
            in {
                "import_reference_library",
                "construct_reference_library",
            }
            and self.af_behavior == "retain"
            and self.target_af_artifact is None
        ):
            raise ValueError("final run basis requires accepted target AF")
        if self.status is ReferenceWorkflowStatus.COMPLETED:
            if self.basis_source is None:
                raise ValueError("completed workflow requires a basis source")
            if (
                self.basis_source not in {"phenoimager_motif_profile", "curated_reference_library", "shared_cycle_reference_library"}
                and self.af_behavior == "retain"
                and self.target_af_artifact is None
            ):
                raise ValueError("completed non-MOTIF workflow requires target AF")
            _validate_completed_transition_chain(self)
        _sha256(self.content_sha256, "workflow checkpoint content SHA-256")
        if self.content_sha256 != _digest(self._content_mapping()):
            raise ValueError("workflow checkpoint content SHA-256 does not match")
        if len(_canonical_json(self.to_mapping())) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("workflow checkpoint exceeds serialized v1 size limit")

    @property
    def current_checkpoint(self) -> ReferenceCheckpointKind | None:
        return None if self.request is None else self.request.checkpoint

    @property
    def active_decisions(self) -> tuple[ReferenceDecisionRecord, ...]:
        """Current source branch only; the full historical chain is retained."""
        for index in range(len(self.decisions) - 1, -1, -1):
            if self.decisions[index].response.option_value == "reselect_basis_source":
                return self.decisions[index + 1 :]
        return self.decisions

    @property
    def accepted_fluorophores(self) -> tuple[str, ...]:
        return tuple(
            item.fluorophore
            for item in self.fluorophore_artifacts
            if item.accepted_spectrum is not None
        )

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.SCHEMA_NAME,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "target_image_sha256": self.target_image_sha256,
            "fluorophores": list(self.fluorophores),
            "measurement_ids": list(self.measurement_ids),
            **({"af_behavior": self.af_behavior} if self.af_behavior != "retain" else {}),
            "revision": self.revision,
            "status": self.status.value,
            "basis_source": self.basis_source,
            "control_strategy": self.control_strategy,
            "current_fluorophore": self.current_fluorophore,
            "fluorophore_artifacts": [item.to_mapping() for item in self.fluorophore_artifacts],
            "accepted_library": (
                None if self.accepted_library is None else self.accepted_library.to_mapping()
            ),
            "available_reference_control": (
                None
                if self.available_reference_control is None
                else self.available_reference_control.to_mapping()
            ),
            "target_control_compatibility": (
                None
                if self.target_control_compatibility is None
                else self.target_control_compatibility.to_mapping()
            ),
            "target_af_artifact": (
                None if self.target_af_artifact is None else self.target_af_artifact.to_mapping()
            ),
            "final_basis_artifact": (
                None
                if self.final_basis_artifact is None
                else self.final_basis_artifact.to_mapping()
            ),
            "decisions": [item.to_mapping() for item in self.decisions],
            "transition_log": [item.to_mapping() for item in self.transition_log],
            "pending_publication": (
                None if self.pending_publication is None else self.pending_publication.to_mapping()
            ),
            "request": None if self.request is None else self.request.to_mapping(),
            "prior_checkpoint_sha256": self.prior_checkpoint_sha256,
        }

    def _authoritative_state_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "target_image_sha256": self.target_image_sha256,
            "fluorophores": list(self.fluorophores),
            "measurement_ids": list(self.measurement_ids),
            **({"af_behavior": self.af_behavior} if self.af_behavior != "retain" else {}),
            "revision": self.revision,
            "status": self.status.value,
            "basis_source": self.basis_source,
            "control_strategy": self.control_strategy,
            "current_fluorophore": self.current_fluorophore,
            "fluorophore_artifacts": [item.to_mapping() for item in self.fluorophore_artifacts],
            "accepted_library": (
                None if self.accepted_library is None else self.accepted_library.to_mapping()
            ),
            "available_reference_control": (
                None
                if self.available_reference_control is None
                else self.available_reference_control.to_mapping()
            ),
            "target_control_compatibility": (
                None
                if self.target_control_compatibility is None
                else self.target_control_compatibility.to_mapping()
            ),
            "target_af_artifact": (
                None if self.target_af_artifact is None else self.target_af_artifact.to_mapping()
            ),
            "final_basis_artifact": (
                None
                if self.final_basis_artifact is None
                else self.final_basis_artifact.to_mapping()
            ),
            "prior_checkpoint_sha256": self.prior_checkpoint_sha256,
            "checkpoint": None if self.request is None else self.request.checkpoint.value,
            "reason_code": None if self.request is None else self.request.reason.code.value,
            "evidence": (
                []
                if self.request is None
                else [item.to_mapping() for item in self.request.evidence]
            ),
            "proposed_artifacts": (
                []
                if self.request is None
                else [item.to_mapping() for item in self.request.proposed_artifacts]
            ),
            "pending_publication": (
                None if self.pending_publication is None else self.pending_publication.to_mapping()
            ),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    def _validate_transition_tip(self) -> None:
        if self.transition_log and (
            self.transition_log[-1].target_state_sha256
            != _digest(self._authoritative_state_mapping())
            or self.transition_log[-1].target_state_json
            != _canonical_json(self._authoritative_state_mapping()).decode("utf-8")
        ):
            raise ValueError("workflow transition log does not reach current authoritative state")

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceWorkflowCheckpoint":
        """Decode the serialized fields into a validated ReferenceWorkflowCheckpoint record.

        The mapping is checked against the declared field inventory, including
        ``schema_name``, ``schema_version``, ``run_id``, ``target_image_sha256``,
        ``fluorophores``, ``measurement_ids``. Nested values are decoded through the
        corresponding field validators before the record is constructed."""

        expected = {
            "schema_name",
            "schema_version",
            "run_id",
            "target_image_sha256",
            "fluorophores",
            "measurement_ids",
            "revision",
            "status",
            "basis_source",
            "control_strategy",
            "current_fluorophore",
            "fluorophore_artifacts",
            "accepted_library",
            "available_reference_control",
            "target_control_compatibility",
            "target_af_artifact",
            "final_basis_artifact",
            "decisions",
            "transition_log",
            "pending_publication",
            "request",
            "prior_checkpoint_sha256",
            "content_sha256",
        }
        if isinstance(value, dict) and "af_behavior" in value:
            expected.add("af_behavior")
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("reference workflow checkpoint schema fields differ")
        if (
            value["schema_name"] != cls.SCHEMA_NAME
            or type(value["schema_version"]) is not int
            or value["schema_version"] != cls.SCHEMA_VERSION
        ):
            raise ValueError("unsupported reference workflow checkpoint schema")
        if not all(
            isinstance(value[field], list)
            for field in (
                "fluorophores",
                "measurement_ids",
                "fluorophore_artifacts",
                "decisions",
                "transition_log",
            )
        ):
            raise ValueError("reference workflow checkpoint arrays are invalid")
        try:
            status = ReferenceWorkflowStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference workflow status is unsupported") from exc

        def pointer(field: str) -> ArtifactPointer | None:
            raw = value[field]
            return None if raw is None else ArtifactPointer.from_mapping(raw)

        result = cls(
            run_id=value["run_id"],  # type: ignore[arg-type]
            target_image_sha256=value["target_image_sha256"],  # type: ignore[arg-type]
            fluorophores=tuple(value["fluorophores"]),
            measurement_ids=tuple(value["measurement_ids"]),
            af_behavior=value.get("af_behavior", "retain"),
            revision=value["revision"],  # type: ignore[arg-type]
            status=status,
            basis_source=value["basis_source"],  # type: ignore[arg-type]
            control_strategy=value["control_strategy"],  # type: ignore[arg-type]
            current_fluorophore=value["current_fluorophore"],  # type: ignore[arg-type]
            fluorophore_artifacts=tuple(
                FluorophoreArtifactSlot.from_mapping(item)
                for item in value["fluorophore_artifacts"]
            ),
            accepted_library=pointer("accepted_library"),
            available_reference_control=pointer("available_reference_control"),
            target_control_compatibility=(
                None
                if value["target_control_compatibility"] is None
                else TargetControlCompatibilityEvidence.from_mapping(
                    value["target_control_compatibility"]
                )
            ),
            target_af_artifact=pointer("target_af_artifact"),
            final_basis_artifact=pointer("final_basis_artifact"),
            decisions=tuple(
                ReferenceDecisionRecord.from_mapping(item) for item in value["decisions"]
            ),
            transition_log=tuple(
                ReferenceTransitionRecord.from_mapping(item) for item in value["transition_log"]
            ),
            pending_publication=(
                None
                if value["pending_publication"] is None
                else PendingPublication.from_mapping(value["pending_publication"])
            ),
            request=(
                None
                if value["request"] is None
                else ReferenceInteractionRequest.from_mapping(value["request"])
            ),
            prior_checkpoint_sha256=value["prior_checkpoint_sha256"],  # type: ignore[arg-type]
            content_sha256=value["content_sha256"],  # type: ignore[arg-type]
        )
        result._validate_transition_tip()
        return result


def _authoritative_mapping_values(
    *,
    run_id: str,
    target_image_sha256: str,
    fluorophores: tuple[str, ...],
    measurement_ids: tuple[str, ...],
    af_behavior: str,
    revision: int,
    status: ReferenceWorkflowStatus,
    basis_source: str | None,
    control_strategy: str | None,
    current_fluorophore: str | None,
    fluorophore_artifacts: tuple[FluorophoreArtifactSlot, ...],
    accepted_library: ArtifactPointer | None,
    available_reference_control: ArtifactPointer | None,
    target_control_compatibility: TargetControlCompatibilityEvidence | None,
    target_af_artifact: ArtifactPointer | None,
    final_basis_artifact: ArtifactPointer | None,
    prior_checkpoint_sha256: str | None,
    checkpoint: ReferenceCheckpointKind | None,
    reason_code: ReferenceReasonCode | None,
    evidence: tuple[ReferenceEvidence, ...],
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...],
    pending_publication: PendingPublication | None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "target_image_sha256": target_image_sha256,
        "fluorophores": list(fluorophores),
        "measurement_ids": list(measurement_ids),
        **({"af_behavior": af_behavior} if af_behavior != "retain" else {}),
        "revision": revision,
        "status": status.value,
        "basis_source": basis_source,
        "control_strategy": control_strategy,
        "current_fluorophore": current_fluorophore,
        "fluorophore_artifacts": [item.to_mapping() for item in fluorophore_artifacts],
        "accepted_library": None if accepted_library is None else accepted_library.to_mapping(),
        "available_reference_control": (
            None
            if available_reference_control is None
            else available_reference_control.to_mapping()
        ),
        "target_control_compatibility": (
            None
            if target_control_compatibility is None
            else target_control_compatibility.to_mapping()
        ),
        "target_af_artifact": (
            None if target_af_artifact is None else target_af_artifact.to_mapping()
        ),
        "final_basis_artifact": (
            None if final_basis_artifact is None else final_basis_artifact.to_mapping()
        ),
        "prior_checkpoint_sha256": prior_checkpoint_sha256,
        "checkpoint": None if checkpoint is None else checkpoint.value,
        "reason_code": (
            None if checkpoint is None else (reason_code or _CHECKPOINT_REASON[checkpoint]).value
        ),
        "evidence": [item.to_mapping() for item in evidence],
        "proposed_artifacts": [item.to_mapping() for item in proposed_artifacts],
        "pending_publication": (
            None if pending_publication is None else pending_publication.to_mapping()
        ),
    }


def _build_checkpoint(
    *,
    run_id: str,
    target_image_sha256: str,
    fluorophores: tuple[str, ...],
    measurement_ids: tuple[str, ...],
    af_behavior: str,
    revision: int,
    status: ReferenceWorkflowStatus,
    basis_source: str | None,
    control_strategy: str | None,
    current_fluorophore: str | None,
    fluorophore_artifacts: tuple[FluorophoreArtifactSlot, ...],
    accepted_library: ArtifactPointer | None,
    available_reference_control: ArtifactPointer | None,
    target_control_compatibility: TargetControlCompatibilityEvidence | None,
    target_af_artifact: ArtifactPointer | None,
    final_basis_artifact: ArtifactPointer | None,
    decisions: tuple[ReferenceDecisionRecord, ...],
    transition_log: tuple[ReferenceTransitionRecord, ...],
    pending_publication: PendingPublication | None,
    checkpoint: ReferenceCheckpointKind | None,
    evidence: tuple[ReferenceEvidence, ...],
    prior_checkpoint_sha256: str | None,
    reason_code: ReferenceReasonCode | None = None,
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = (),
) -> ReferenceWorkflowCheckpoint:
    authoritative_state = _authoritative_mapping_values(
        run_id=run_id,
        target_image_sha256=target_image_sha256,
        fluorophores=fluorophores,
        measurement_ids=measurement_ids,
        af_behavior=af_behavior,
        revision=revision,
        status=status,
        basis_source=basis_source,
        control_strategy=control_strategy,
        current_fluorophore=current_fluorophore,
        fluorophore_artifacts=fluorophore_artifacts,
        accepted_library=accepted_library,
        available_reference_control=available_reference_control,
        target_control_compatibility=target_control_compatibility,
        target_af_artifact=target_af_artifact,
        final_basis_artifact=final_basis_artifact,
        prior_checkpoint_sha256=prior_checkpoint_sha256,
        checkpoint=checkpoint,
        reason_code=reason_code,
        evidence=evidence,
        proposed_artifacts=proposed_artifacts,
        pending_publication=pending_publication,
    )
    request = None
    if checkpoint is not None:
        reason = ReferenceReason(
            reason_code or _CHECKPOINT_REASON[checkpoint],
            _CHECKPOINT_MESSAGES[checkpoint],
        )
        request = make_reference_request(
            run_id=run_id,
            target_image_sha256=target_image_sha256,
            fluorophores=fluorophores,
            measurement_ids=measurement_ids,
            target_control_compatibility=target_control_compatibility,
            authoritative_state_sha256=_digest(authoritative_state),
            revision=revision,
            checkpoint=checkpoint,
            fluorophore=current_fluorophore,
            reason=reason,
            options=_options_for(
                checkpoint,
                basis_source=basis_source,
                source_preparation=_uses_source_preparation(decisions),
                fluorophore=current_fluorophore,
                fluorophores=fluorophores,
                control_strategy=control_strategy,
                last_fluorophore=(
                    current_fluorophore is not None
                    and all(
                        item.accepted_spectrum is not None
                        or item.fluorophore == current_fluorophore
                        for item in fluorophore_artifacts
                    )
                ),
                # The control proposal performs target-compatibility review.
                # Offering the source must not require that later result first.
                reference_control_available=available_reference_control is not None,
            ),
            evidence=evidence,
            proposed_artifacts=proposed_artifacts,
            prior_checkpoint_sha256=prior_checkpoint_sha256,
        )
    content = {
        "schema_name": ReferenceWorkflowCheckpoint.SCHEMA_NAME,
        "schema_version": ReferenceWorkflowCheckpoint.SCHEMA_VERSION,
        "run_id": run_id,
        "target_image_sha256": target_image_sha256,
        "fluorophores": list(fluorophores),
        "measurement_ids": list(measurement_ids),
        **({"af_behavior": af_behavior} if af_behavior != "retain" else {}),
        "revision": revision,
        "status": status.value,
        "basis_source": basis_source,
        "control_strategy": control_strategy,
        "current_fluorophore": current_fluorophore,
        "fluorophore_artifacts": [item.to_mapping() for item in fluorophore_artifacts],
        "accepted_library": None if accepted_library is None else accepted_library.to_mapping(),
        "available_reference_control": (
            None
            if available_reference_control is None
            else available_reference_control.to_mapping()
        ),
        "target_control_compatibility": (
            None
            if target_control_compatibility is None
            else target_control_compatibility.to_mapping()
        ),
        "target_af_artifact": (
            None if target_af_artifact is None else target_af_artifact.to_mapping()
        ),
        "final_basis_artifact": (
            None if final_basis_artifact is None else final_basis_artifact.to_mapping()
        ),
        "decisions": [item.to_mapping() for item in decisions],
        "transition_log": [item.to_mapping() for item in transition_log],
        "pending_publication": (
            None if pending_publication is None else pending_publication.to_mapping()
        ),
        "request": None if request is None else request.to_mapping(),
        "prior_checkpoint_sha256": prior_checkpoint_sha256,
    }
    return ReferenceWorkflowCheckpoint(
        run_id=run_id,
        target_image_sha256=target_image_sha256,
        fluorophores=fluorophores,
        measurement_ids=measurement_ids,
        af_behavior=af_behavior,
        revision=revision,
        status=status,
        basis_source=basis_source,
        control_strategy=control_strategy,
        current_fluorophore=current_fluorophore,
        fluorophore_artifacts=fluorophore_artifacts,
        accepted_library=accepted_library,
        available_reference_control=available_reference_control,
        target_control_compatibility=target_control_compatibility,
        target_af_artifact=target_af_artifact,
        final_basis_artifact=final_basis_artifact,
        decisions=decisions,
        transition_log=transition_log,
        pending_publication=pending_publication,
        request=request,
        prior_checkpoint_sha256=prior_checkpoint_sha256,
        content_sha256=_digest(content),
    )


def start_reference_workflow(
    *,
    run_id: str,
    target_image_sha256: str,
    fluorophores: tuple[str, ...],
    measurement_ids: tuple[str, ...],
    af_behavior: str = "retain",
    available_reference_control: ValidatedArtifactEvidence | None = None,
    target_control_compatibility: TargetControlCompatibilityEvidence | None = None,
    evidence: tuple[ReferenceEvidence, ...] = (),
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = (),
) -> ReferenceWorkflowCheckpoint:
    """Create the deterministic first request without selecting a source."""
    if available_reference_control is not None and (
        type(available_reference_control) is not ValidatedArtifactEvidence
        or available_reference_control.pointer.role != "unstained_reference_image"
    ):
        raise ValueError("available reference control must be a validated unstained image")
    if target_control_compatibility is not None:
        if type(target_control_compatibility) is not TargetControlCompatibilityEvidence:
            raise TypeError("target-control compatibility evidence is invalid")
        if target_control_compatibility.target_image_sha256 != target_image_sha256:
            raise ValueError("target-control evidence does not bind target image")
        if (
            target_control_compatibility.control_artifact.role == "unstained_reference_image"
            and available_reference_control is not None
            and (
                available_reference_control.pointer != target_control_compatibility.control_artifact
            )
        ):
            raise ValueError("target-control evidence does not bind available control bytes")
    target = _build_checkpoint(
        run_id=run_id,
        target_image_sha256=target_image_sha256,
        fluorophores=fluorophores,
        measurement_ids=measurement_ids,
        af_behavior=af_behavior,
        revision=0,
        status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=None,
        control_strategy=None,
        current_fluorophore=None,
        fluorophore_artifacts=tuple(FluorophoreArtifactSlot(item) for item in fluorophores),
        accepted_library=None,
        available_reference_control=(
            available_reference_control.pointer
            if available_reference_control is not None
            else (
                target_control_compatibility.control_artifact
                if target_control_compatibility is not None
                and target_control_compatibility.control_artifact.role
                == "unstained_reference_image"
                else None
            )
        ),
        target_control_compatibility=target_control_compatibility,
        target_af_artifact=None,
        final_basis_artifact=None,
        decisions=(),
        transition_log=(),
        pending_publication=None,
        checkpoint=ReferenceCheckpointKind.BASIS_SOURCE,
        evidence=evidence,
        prior_checkpoint_sha256=None,
        proposed_artifacts=proposed_artifacts,
    )
    return target


def _record_transition(
    source: ReferenceWorkflowCheckpoint,
    target: ReferenceWorkflowCheckpoint,
    *,
    kind: ReferenceTransitionKind,
    request_id: str | None,
    affected_fluorophore: str | None = None,
    superseded_request_ids: tuple[str, ...] = (),
    artifact_bindings: tuple[ArtifactPointer, ...] = (),
    decision_record_sha256: str | None = None,
    publication_evidence: tuple[PublishedArtifactEvidence, ...] = (),
) -> ReferenceWorkflowCheckpoint:
    if len(source.transition_log) >= _MAX_TRANSITIONS:
        raise ValueError("workflow transition history exceeds versioned v1 limit")
    source_state_sha256 = _digest(source._authoritative_state_mapping())
    target_state_sha256 = _digest(target._authoritative_state_mapping())
    transition = ReferenceTransitionRecord.create(
        kind=kind,
        revision=target.revision,
        source_checkpoint_sha256=source.content_sha256,
        source_state_sha256=source_state_sha256,
        target_state_sha256=target_state_sha256,
        source_state_json=_canonical_json(source._authoritative_state_mapping()).decode("utf-8"),
        target_state_json=_canonical_json(target._authoritative_state_mapping()).decode("utf-8"),
        previous_transition_sha256=(
            None if not source.transition_log else source.transition_log[-1].content_sha256
        ),
        request_id=request_id,
        decision_record_sha256=decision_record_sha256,
        affected_fluorophore=affected_fluorophore,
        superseded_request_ids=superseded_request_ids,
        artifact_bindings=artifact_bindings,
        publication_evidence=publication_evidence,
    )
    request = target.request
    target = _build_checkpoint(
        run_id=target.run_id,
        target_image_sha256=target.target_image_sha256,
        fluorophores=target.fluorophores,
        measurement_ids=target.measurement_ids,
        af_behavior=target.af_behavior,
        revision=target.revision,
        status=target.status,
        basis_source=target.basis_source,
        control_strategy=target.control_strategy,
        current_fluorophore=target.current_fluorophore,
        fluorophore_artifacts=target.fluorophore_artifacts,
        accepted_library=target.accepted_library,
        available_reference_control=target.available_reference_control,
        target_control_compatibility=target.target_control_compatibility,
        target_af_artifact=target.target_af_artifact,
        final_basis_artifact=target.final_basis_artifact,
        decisions=target.decisions,
        transition_log=(*source.transition_log, transition),
        pending_publication=target.pending_publication,
        checkpoint=None if request is None else request.checkpoint,
        evidence=() if request is None else request.evidence,
        prior_checkpoint_sha256=target.prior_checkpoint_sha256,
        reason_code=None if request is None else request.reason.code,
        proposed_artifacts=(() if request is None else request.proposed_artifacts),
    )
    target._validate_transition_tip()
    return target


def propose_reference_artifacts(
    state: ReferenceWorkflowCheckpoint,
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...],
    *,
    evidence: tuple[ReferenceEvidence, ...] | None = None,
) -> ReferenceWorkflowCheckpoint:
    """Reissue the live request after owning boundaries validate exact bytes/types."""
    if state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE or state.request is None:
        raise ValueError("only an active request can receive artifact proposals")
    bindings = tuple(
        ArtifactRequirement(item.pointer.role, item.pointer.subject_id)
        for item in proposed_artifacts
    )
    allowed = {
        option.artifact_requirements
        for option in state.request.options
        if option.artifact_requirements
    }
    if bindings not in allowed:
        raise ValueError("proposed artifacts do not match an exact live option inventory")
    target = _build_checkpoint(
        run_id=state.run_id,
        target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores,
        measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1,
        status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=state.basis_source,
        control_strategy=state.control_strategy,
        current_fluorophore=state.current_fluorophore,
        fluorophore_artifacts=state.fluorophore_artifacts,
        accepted_library=state.accepted_library,
        available_reference_control=state.available_reference_control,
        target_control_compatibility=state.target_control_compatibility,
        target_af_artifact=state.target_af_artifact,
        final_basis_artifact=state.final_basis_artifact,
        decisions=state.decisions,
        transition_log=state.transition_log,
        pending_publication=None,
        checkpoint=state.request.checkpoint,
        evidence=state.request.evidence if evidence is None else evidence,
        proposed_artifacts=proposed_artifacts,
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=state.request.reason.code,
    )
    return _record_transition(
        state,
        target,
        kind=ReferenceTransitionKind.PROPOSAL,
        request_id=state.request.request_id,
    )


def bind_target_control_compatibility(
    state: ReferenceWorkflowCheckpoint,
    compatibility: TargetControlCompatibilityEvidence,
) -> ReferenceWorkflowCheckpoint:
    """Reissue TARGET_AF with exact service-derived target/control authority."""

    if (
        type(state) is not ReferenceWorkflowCheckpoint
        or state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
        or state.request is None
        or state.request.checkpoint is not ReferenceCheckpointKind.TARGET_AF
    ):
        raise ValueError("target-control compatibility requires a live TARGET_AF request")
    if type(compatibility) is not TargetControlCompatibilityEvidence:
        raise TypeError("target-control compatibility must be exact typed evidence")
    if compatibility.target_image_sha256 != state.target_image_sha256 or not compatibility.accepted:
        raise ValueError("target-control compatibility is not accepted for this target")
    if (
        compatibility.control_artifact.role == "unstained_reference_image"
        and compatibility.control_artifact != state.available_reference_control
    ):
        raise ValueError("target-control compatibility differs from accepted shared control")
    reselected_control = (
        bool(state.decisions)
        and state.decisions[-1].response.option_value == "reselect_target_af_source"
        and state.target_control_compatibility == state.decisions[-1].request.target_control_compatibility
    )
    if state.target_control_compatibility not in {None, compatibility} and not reselected_control:
        raise ValueError("target-control compatibility conflicts with live authority")
    target = _build_checkpoint(
        run_id=state.run_id,
        target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores,
        measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1,
        status=state.status,
        basis_source=state.basis_source,
        control_strategy=state.control_strategy,
        current_fluorophore=state.current_fluorophore,
        fluorophore_artifacts=state.fluorophore_artifacts,
        accepted_library=state.accepted_library,
        available_reference_control=state.available_reference_control,
        target_control_compatibility=compatibility,
        target_af_artifact=state.target_af_artifact,
        final_basis_artifact=state.final_basis_artifact,
        decisions=state.decisions,
        transition_log=state.transition_log,
        pending_publication=None,
        checkpoint=ReferenceCheckpointKind.TARGET_AF,
        evidence=state.request.evidence,
        proposed_artifacts=state.request.proposed_artifacts,
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=state.request.reason.code,
    )
    return _record_transition(
        state,
        target,
        kind=ReferenceTransitionKind.PROPOSAL,
        request_id=state.request.request_id,
    )


def propose_reference_evidence(
    state: ReferenceWorkflowCheckpoint,
    evidence: tuple[ReferenceEvidence, ...],
) -> ReferenceWorkflowCheckpoint:
    """Reissue one live request with exact service-derived scientific evidence."""

    if state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE or state.request is None:
        raise ValueError("only an active request can receive scientific evidence")
    if type(evidence) is not tuple or any(type(item) is not ReferenceEvidence for item in evidence):
        raise TypeError("scientific request evidence must be an exact tuple")
    if state.request.proposed_artifacts:
        raise ValueError("scientific evidence must be proposed with existing artifact helper")
    target = _build_checkpoint(
        run_id=state.run_id,
        target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores,
        measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1,
        status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=state.basis_source,
        control_strategy=state.control_strategy,
        current_fluorophore=state.current_fluorophore,
        fluorophore_artifacts=state.fluorophore_artifacts,
        accepted_library=state.accepted_library,
        available_reference_control=state.available_reference_control,
        target_control_compatibility=state.target_control_compatibility,
        target_af_artifact=state.target_af_artifact,
        final_basis_artifact=state.final_basis_artifact,
        decisions=state.decisions,
        transition_log=state.transition_log,
        pending_publication=None,
        checkpoint=state.request.checkpoint,
        evidence=evidence,
        proposed_artifacts=(),
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=state.request.reason.code,
    )
    return _record_transition(
        state,
        target,
        kind=ReferenceTransitionKind.PROPOSAL,
        request_id=state.request.request_id,
    )


def _artifact_with_role(
    response: ReferenceResponse,
    role: str,
    *,
    required: bool,
) -> ArtifactPointer | None:
    matches = tuple(item for item in response.artifacts if item.role == role)
    if len(matches) > 1 or (required and len(matches) != 1):
        raise ValueError(f"response requires exactly one {role} artifact binding")
    return matches[0] if matches else None


def _pending_publication(
    *,
    kind: str,
    decision: ReferenceResponse,
    fluorophore: str | None,
    requirements: tuple[ArtifactRequirement, ...],
) -> PendingPublication:
    return PendingPublication(
        publication_kind=kind,
        review_request_id=decision.request_id,
        affected_fluorophore=fluorophore,
        proposal_artifacts=decision.artifacts,
        required_artifacts=requirements,
    )


def advance_reference_workflow(
    state: ReferenceWorkflowCheckpoint,
    response: ReferenceResponse | dict[str, object],
    *,
    next_evidence: tuple[ReferenceEvidence, ...] = (),
    next_proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = (),
) -> ReferenceWorkflowCheckpoint:
    """Apply one exact response to the current authoritative checkpoint."""
    if type(state) is not ReferenceWorkflowCheckpoint:
        raise TypeError("state must be a ReferenceWorkflowCheckpoint")
    if state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE or state.request is None:
        raise ValueError("terminal workflow state cannot accept a response")
    decision = validate_reference_response(state.request, response)
    decisions = (
        *state.decisions,
        ReferenceDecisionRecord.create(
            source_checkpoint_sha256=state.content_sha256,
            request=state.request,
            response=decision,
        ),
    )
    next_revision = state.revision + 1
    lineage = state.content_sha256

    def transitioned(target: ReferenceWorkflowCheckpoint) -> ReferenceWorkflowCheckpoint:
        return _record_transition(
            state,
            target,
            kind=ReferenceTransitionKind.DECISION,
            request_id=decision.request_id,
            affected_fluorophore=state.current_fluorophore,
            decision_record_sha256=decisions[-1].content_sha256,
        )

    if decision.action is ReferenceActionCode.ABORT:
        return transitioned(
            _build_checkpoint(
                run_id=state.run_id,
                target_image_sha256=state.target_image_sha256,
                fluorophores=state.fluorophores,
                measurement_ids=state.measurement_ids,
                af_behavior=state.af_behavior,
                revision=next_revision,
                status=ReferenceWorkflowStatus.ABORTED,
                basis_source=state.basis_source,
                control_strategy=state.control_strategy,
                current_fluorophore=None,
                fluorophore_artifacts=state.fluorophore_artifacts,
                accepted_library=state.accepted_library,
                available_reference_control=state.available_reference_control,
                target_control_compatibility=state.target_control_compatibility,
                target_af_artifact=state.target_af_artifact,
                final_basis_artifact=None,
                decisions=decisions,
                transition_log=state.transition_log,
                pending_publication=None,
                checkpoint=None,
                evidence=(),
                prior_checkpoint_sha256=lineage,
            )
        )

    current = state.request.checkpoint
    next_checkpoint: ReferenceCheckpointKind
    next_fluorophore = state.current_fluorophore
    fluorophores = state.fluorophores
    basis_source = state.basis_source
    control_strategy = state.control_strategy
    reference_control = state.available_reference_control
    target_compatibility = state.target_control_compatibility
    fluorophore_artifacts = state.fluorophore_artifacts
    library = state.accepted_library
    target_af = state.target_af_artifact
    next_reason_code: ReferenceReasonCode | None = None

    if decision.action is ReferenceActionCode.RETRY:
        next_checkpoint = current
        if current in {
            ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
        }:
            next_reason_code = ReferenceReasonCode.TARGET_AF_MODEL_QC_FAILED
        elif current is ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS:
            next_reason_code = ReferenceReasonCode.REFERENCE_BACKGROUND_QC_FAILED
    elif decision.action is ReferenceActionCode.RESELECT:
        if decision.option_value == "revise_shared_control":
            shared = control_strategy == "shared_unstained_control"
            if not shared and state.current_fluorophore is None:
                raise ValueError("Choose the separate source to revise")
            next_checkpoint = ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION
            next_fluorophore = None if shared else state.current_fluorophore
            library = target_af = None
            fluorophore_artifacts = tuple(FluorophoreArtifactSlot(slot.fluorophore)
                if shared or slot.fluorophore == next_fluorophore else slot for slot in state.fluorophore_artifacts)
        elif decision.option_value == "reselect_basis_source":
            if current is not ReferenceCheckpointKind.FINAL_RUN_BASIS:
                raise ValueError("Source changes require an unfinished final reference review")
            next_checkpoint = ReferenceCheckpointKind.BASIS_SOURCE
            next_fluorophore = None
            basis_source = control_strategy = reference_control = target_compatibility = None
            library = target_af = None
            fluorophore_artifacts = tuple(
                FluorophoreArtifactSlot(item.fluorophore) for item in state.fluorophore_artifacts
            )
        elif decision.option_value == "revise_signal_dark_measurements":
            if current not in {
                ReferenceCheckpointKind.EXTRACTION_QC,
                ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS,
            }:
                raise ValueError("Band revision requires an unfinished extraction or spectrum review")
            if state.current_fluorophore is None or decision.selected_ids != (
                state.current_fluorophore,
            ):
                raise ValueError("band revision must identify the exact affected fluorophore")
            if state.current_fluorophore in state.accepted_fluorophores:
                raise ValueError("Band revision cannot replace an accepted fluorophore spectrum")
            next_checkpoint = ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
            # Per-stain controls retain the reviewed negative-control evidence
            # offered with the original band choices. No pixels are sampled.
            next_evidence = next(
                (
                    item.request.evidence
                    for item in reversed(state.active_decisions)
                    if item.request.checkpoint is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
                    and item.request.fluorophore == state.current_fluorophore
                    and item.response.option_value == "accept_signal_dark_measurements"
                ),
                (),
            )
        elif current in {
            ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
        }:
            next_checkpoint = ReferenceCheckpointKind.TARGET_AF
            next_fluorophore = None
            target_af = None
        elif current in {
            ReferenceCheckpointKind.AF_REFERENCE_ROIS,
            ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
            ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS,
            ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
            ReferenceCheckpointKind.EXTRACTION_QC,
            ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS,
        }:
            if state.current_fluorophore is None or decision.selected_ids != (
                state.current_fluorophore,
            ):
                raise ValueError("reselection must identify the exact affected fluorophore")
            next_checkpoint = ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS
            fluorophore_artifacts = tuple(
                (
                    FluorophoreArtifactSlot(item.fluorophore)
                    if item.fluorophore == state.current_fluorophore
                    else item
                )
                for item in fluorophore_artifacts
            )
            library = None
            target_af = None
        else:
            raise ValueError("reselection is illegal at the current checkpoint")
    elif current is ReferenceCheckpointKind.BASIS_SOURCE:
        basis_source = decision.option_value
        if not fluorophores and basis_source == "construct_reference_library":
            fluorophores = decision.selected_ids
            fluorophore_artifacts = tuple(FluorophoreArtifactSlot(name) for name in fluorophores)
        if not fluorophores and basis_source != "curated_reference_library":
            raise ValueError("Declare the component panel in the selected reference source")
        if basis_source in {"phenoimager_motif_profile", "curated_reference_library", "shared_cycle_reference_library"}:
            next_checkpoint = ReferenceCheckpointKind.FINAL_RUN_BASIS
        elif basis_source == "import_reference_library":
            return transitioned(
                _build_checkpoint(
                    run_id=state.run_id,
                    target_image_sha256=state.target_image_sha256,
                    fluorophores=state.fluorophores,
                    measurement_ids=state.measurement_ids,
                    af_behavior=state.af_behavior,
                    revision=next_revision,
                    status=ReferenceWorkflowStatus.AWAITING_PUBLICATION,
                    basis_source=basis_source,
                    control_strategy=None,
                    current_fluorophore=None,
                    fluorophore_artifacts=fluorophore_artifacts,
                    accepted_library=None,
                    available_reference_control=reference_control,
                    target_control_compatibility=target_compatibility,
                    target_af_artifact=None,
                    final_basis_artifact=None,
                    decisions=decisions,
                    transition_log=state.transition_log,
                    pending_publication=_pending_publication(
                        kind="import_library",
                        decision=decision,
                        fluorophore=None,
                        requirements=_requirements(("accepted_fluorophore_library", None)),
                    ),
                    checkpoint=None,
                    evidence=(),
                    prior_checkpoint_sha256=lineage,
                )
            )
        elif basis_source == "construct_reference_library":
            next_checkpoint = ReferenceCheckpointKind.CONTROL_STRATEGY
        else:
            raise ValueError("basis-source response is invalid")
    elif current is ReferenceCheckpointKind.CONTROL_STRATEGY:
        control_strategy = decision.option_value
        next_checkpoint = ReferenceCheckpointKind.REFERENCE_CONTROL_FILES
    elif current is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES:
        if control_strategy == "shared_unstained_control":
            reference_control = _artifact_with_role(
                decision, "unstained_reference_image", required=True
            )
            if (
                target_compatibility is not None
                and target_compatibility.control_artifact != reference_control
            ):
                target_compatibility = None
        else:
            reference_control = None
            target_compatibility = None
        if decision.option_value == "accept_reference_control_files_with_source_preparation":
            next_checkpoint = ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION
            next_fluorophore = None if control_strategy == "shared_unstained_control" else state.fluorophores[0]
        else:
            next_checkpoint = ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS
            next_fluorophore = state.fluorophores[0]
    elif current is ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION:
        next_checkpoint = ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS
        next_fluorophore = state.current_fluorophore or state.fluorophores[0]
    elif current is ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS:
        next_checkpoint = (
            ReferenceCheckpointKind.AF_REFERENCE_ROIS
            if control_strategy == "shared_unstained_control" or _uses_source_preparation(decisions)
            else ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS
        )
    elif current is ReferenceCheckpointKind.AF_REFERENCE_ROIS:
        next_checkpoint = ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS
    elif current is ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS:
        next_checkpoint = ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS
    elif current is ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS:
        next_checkpoint = ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
    elif current is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS:
        next_checkpoint = ReferenceCheckpointKind.EXTRACTION_QC
    elif current is ReferenceCheckpointKind.EXTRACTION_QC:
        next_checkpoint = ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS
    elif current is ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS:
        if state.current_fluorophore is None:
            raise ValueError("consensus checkpoint lacks an affected fluorophore")
        last = all(
            item.accepted_spectrum is not None or item.fluorophore == state.current_fluorophore
            for item in fluorophore_artifacts
        )
        requirements = _requirements(
            ("accepted_fluorophore_spectrum", state.current_fluorophore),
            *(("accepted_fluorophore_library", None),) if last else (),
        )
        return transitioned(
            _build_checkpoint(
                run_id=state.run_id,
                target_image_sha256=state.target_image_sha256,
                fluorophores=state.fluorophores,
                measurement_ids=state.measurement_ids,
                af_behavior=state.af_behavior,
                revision=next_revision,
                status=ReferenceWorkflowStatus.AWAITING_PUBLICATION,
                basis_source=basis_source,
                control_strategy=control_strategy,
                current_fluorophore=state.current_fluorophore,
                fluorophore_artifacts=fluorophore_artifacts,
                accepted_library=library,
                available_reference_control=reference_control,
                target_control_compatibility=target_compatibility,
                target_af_artifact=None,
                final_basis_artifact=None,
                decisions=decisions,
                transition_log=state.transition_log,
                pending_publication=_pending_publication(
                    kind="fluorophore_consensus",
                    decision=decision,
                    fluorophore=state.current_fluorophore,
                    requirements=requirements,
                ),
                checkpoint=None,
                evidence=(),
                prior_checkpoint_sha256=lineage,
            )
        )
    elif current is ReferenceCheckpointKind.TARGET_AF:
        if (
            state.basis_source
            not in {
                "import_reference_library",
                "construct_reference_library",
            }
            or library is None
        ):
            raise ValueError("target AF requires an accepted fluorophore library")
        next_checkpoint = ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW
    elif current is ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW:
        return transitioned(
            _build_checkpoint(
                run_id=state.run_id,
                target_image_sha256=state.target_image_sha256,
                fluorophores=state.fluorophores,
                measurement_ids=state.measurement_ids,
                af_behavior=state.af_behavior,
                revision=next_revision,
                status=ReferenceWorkflowStatus.AWAITING_PUBLICATION,
                basis_source=basis_source,
                control_strategy=control_strategy,
                current_fluorophore=None,
                fluorophore_artifacts=fluorophore_artifacts,
                accepted_library=library,
                available_reference_control=reference_control,
                target_control_compatibility=target_compatibility,
                target_af_artifact=None,
                final_basis_artifact=None,
                decisions=decisions,
                transition_log=state.transition_log,
                pending_publication=_pending_publication(
                    kind="target_af",
                    decision=decision,
                    fluorophore=None,
                    requirements=_requirements(("accepted_target_af", None)),
                ),
                checkpoint=None,
                evidence=(),
                prior_checkpoint_sha256=lineage,
            )
        )
    elif current is ReferenceCheckpointKind.FINAL_RUN_BASIS:
        return transitioned(
            _build_checkpoint(
                run_id=state.run_id,
                target_image_sha256=state.target_image_sha256,
                fluorophores=state.fluorophores,
                measurement_ids=state.measurement_ids,
                af_behavior=state.af_behavior,
                revision=next_revision,
                status=ReferenceWorkflowStatus.AWAITING_PUBLICATION,
                basis_source=basis_source,
                control_strategy=control_strategy,
                current_fluorophore=None,
                fluorophore_artifacts=fluorophore_artifacts,
                accepted_library=library,
                available_reference_control=state.available_reference_control,
                target_control_compatibility=state.target_control_compatibility,
                target_af_artifact=target_af,
                final_basis_artifact=None,
                decisions=decisions,
                transition_log=state.transition_log,
                pending_publication=_pending_publication(
                    kind="final_basis",
                    decision=decision,
                    fluorophore=None,
                    requirements=_requirements(("final_run_basis", None)),
                ),
                checkpoint=None,
                evidence=(),
                prior_checkpoint_sha256=lineage,
            )
        )
    else:  # pragma: no cover - exhaustive enum guard
        raise ValueError("unsupported workflow checkpoint")

    return transitioned(
        _build_checkpoint(
            run_id=state.run_id,
            target_image_sha256=state.target_image_sha256,
            fluorophores=fluorophores,
            measurement_ids=state.measurement_ids,
            af_behavior=state.af_behavior,
            revision=next_revision,
            status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
            basis_source=basis_source,
            control_strategy=control_strategy,
            current_fluorophore=next_fluorophore,
            fluorophore_artifacts=fluorophore_artifacts,
            accepted_library=library,
            available_reference_control=reference_control,
            target_control_compatibility=target_compatibility,
            target_af_artifact=target_af,
            final_basis_artifact=None,
            decisions=decisions,
            transition_log=state.transition_log,
            pending_publication=None,
            checkpoint=next_checkpoint,
            evidence=next_evidence,
            prior_checkpoint_sha256=lineage,
            reason_code=next_reason_code,
            proposed_artifacts=next_proposed_artifacts,
        )
    )


def publish_reference_artifacts(
    state: ReferenceWorkflowCheckpoint,
    artifacts: tuple[PublishedArtifactEvidence, ...],
) -> ReferenceWorkflowCheckpoint:
    """Publish accepted outputs after their exact proposal was reviewed."""
    if (
        type(state) is not ReferenceWorkflowCheckpoint
        or state.status is not ReferenceWorkflowStatus.AWAITING_PUBLICATION
        or state.pending_publication is None
    ):
        raise ValueError("only a pending publication state can publish artifacts")
    pending = state.pending_publication
    if type(artifacts) is not tuple or any(
        type(item) is not PublishedArtifactEvidence for item in artifacts
    ):
        raise TypeError("published artifacts must be typed publication evidence")
    actual = tuple(
        ArtifactRequirement(item.pointer.role, item.pointer.subject_id) for item in artifacts
    )
    if actual != pending.required_artifacts:
        raise ValueError("published artifacts do not match exact pending roles/cardinality")
    for item in artifacts:
        if (
            item.review_request_id != pending.review_request_id
            or item.proposal_artifacts != pending.proposal_artifacts
        ):
            raise ValueError("published artifact does not bind exact proposal/review lineage")
    pointers = tuple(item.pointer for item in artifacts)
    slots = state.fluorophore_artifacts
    library = state.accepted_library
    target_af = state.target_af_artifact
    final_basis = state.final_basis_artifact
    next_checkpoint: ReferenceCheckpointKind | None
    next_fluorophore: str | None = None
    status = ReferenceWorkflowStatus.AWAITING_RESPONSE

    if pending.publication_kind == "import_library":
        library = pointers[0]
        next_checkpoint = (ReferenceCheckpointKind.TARGET_AF if state.af_behavior == "retain"
                               else ReferenceCheckpointKind.FINAL_RUN_BASIS)
    elif pending.publication_kind == "fluorophore_consensus":
        fluor = pending.affected_fluorophore
        spectrum = pointers[0]
        if fluor is None or spectrum.subject_id != fluor:
            raise ValueError("published spectrum does not bind affected fluorophore")
        slots = tuple(
            (
                FluorophoreArtifactSlot(item.fluorophore, spectrum)
                if item.fluorophore == fluor
                else item
            )
            for item in slots
        )
        if len(pointers) == 2:
            library = pointers[1]
        remaining = tuple(item.fluorophore for item in slots if item.accepted_spectrum is None)
        if remaining:
            next_fluorophore = remaining[0]
            next_checkpoint = (ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION
                if state.control_strategy == "per_stain_negative_controls" and _uses_source_preparation(state.decisions)
                and not any(item.response.option_value == "accept_shared_control_preparation"
                    and item.request.fluorophore == next_fluorophore for item in state.active_decisions)
                else ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS)
        else:
            if library is None:
                raise ValueError("final spectrum publication requires accepted library")
            next_checkpoint = (ReferenceCheckpointKind.TARGET_AF if state.af_behavior == "retain"
                               else ReferenceCheckpointKind.FINAL_RUN_BASIS)
    elif pending.publication_kind == "target_af":
        target_af = pointers[0]
        next_checkpoint = ReferenceCheckpointKind.FINAL_RUN_BASIS
    elif pending.publication_kind == "final_basis":
        final_basis = pointers[0]
        next_checkpoint = None
        status = ReferenceWorkflowStatus.COMPLETED
    else:
        raise ValueError("pending publication kind is unsupported")

    if status is ReferenceWorkflowStatus.COMPLETED:
        revision = state.revision + 1
        authoritative = _authoritative_mapping_values(
            run_id=state.run_id,
            target_image_sha256=state.target_image_sha256,
            fluorophores=state.fluorophores,
            measurement_ids=state.measurement_ids,
            af_behavior=state.af_behavior,
            revision=revision,
            status=status,
            basis_source=state.basis_source,
            control_strategy=state.control_strategy,
            current_fluorophore=None,
            fluorophore_artifacts=slots,
            accepted_library=library,
            available_reference_control=state.available_reference_control,
            target_control_compatibility=state.target_control_compatibility,
            target_af_artifact=target_af,
            final_basis_artifact=final_basis,
            prior_checkpoint_sha256=state.content_sha256,
            checkpoint=None,
            reason_code=None,
            evidence=(),
            proposed_artifacts=(),
            pending_publication=None,
        )
        transition = ReferenceTransitionRecord.create(
            kind=ReferenceTransitionKind.PUBLICATION,
            revision=revision,
            source_checkpoint_sha256=state.content_sha256,
            source_state_sha256=_digest(state._authoritative_state_mapping()),
            target_state_sha256=_digest(authoritative),
            source_state_json=_canonical_json(state._authoritative_state_mapping()).decode("utf-8"),
            target_state_json=_canonical_json(authoritative).decode("utf-8"),
            previous_transition_sha256=(
                None if not state.transition_log else state.transition_log[-1].content_sha256
            ),
            request_id=pending.review_request_id,
            decision_record_sha256=None,
            affected_fluorophore=pending.affected_fluorophore,
            artifact_bindings=pointers,
            publication_evidence=artifacts,
        )
        completed = _build_checkpoint(
            run_id=state.run_id,
            target_image_sha256=state.target_image_sha256,
            fluorophores=state.fluorophores,
            measurement_ids=state.measurement_ids,
            af_behavior=state.af_behavior,
            revision=revision,
            status=status,
            basis_source=state.basis_source,
            control_strategy=state.control_strategy,
            current_fluorophore=None,
            fluorophore_artifacts=slots,
            accepted_library=library,
            available_reference_control=state.available_reference_control,
            target_control_compatibility=state.target_control_compatibility,
            target_af_artifact=target_af,
            final_basis_artifact=final_basis,
            decisions=state.decisions,
            transition_log=(*state.transition_log, transition),
            pending_publication=None,
            checkpoint=None,
            evidence=(),
            prior_checkpoint_sha256=state.content_sha256,
        )
        completed._validate_transition_tip()
        return completed

    resumed = None
    if pending.publication_kind == "fluorophore_consensus":
        # A preparation edit temporarily owns the current request. Its ordinary
        # transition already retains the interrupted sibling's exact frontier.
        for transition in reversed(state.transition_log):
            if transition.affected_fluorophore != pending.affected_fluorophore:
                continue
            if transition.kind is ReferenceTransitionKind.PUBLICATION:
                break
            if transition.kind is ReferenceTransitionKind.PREPARATION_REVISION:
                source = json.loads(transition.source_state_json)
                sibling = source['current_fluorophore']
                if sibling is not None and sibling != pending.affected_fluorophore and any(
                        slot.fluorophore == sibling and slot.accepted_spectrum is None for slot in slots):
                    resumed = source
                    next_fluorophore = sibling
                    next_checkpoint = ReferenceCheckpointKind(source['checkpoint'])
                break
    target = _build_checkpoint(
        run_id=state.run_id,
        target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores,
        measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1,
        status=status,
        basis_source=state.basis_source,
        control_strategy=state.control_strategy,
        current_fluorophore=next_fluorophore,
        fluorophore_artifacts=slots,
        accepted_library=library,
        available_reference_control=state.available_reference_control,
        target_control_compatibility=state.target_control_compatibility,
        target_af_artifact=target_af,
        final_basis_artifact=final_basis,
        decisions=state.decisions,
        transition_log=state.transition_log,
        pending_publication=None,
        checkpoint=next_checkpoint,
        evidence=() if resumed is None else tuple(ReferenceEvidence.from_mapping(item) for item in resumed['evidence']),
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=None if resumed is None else ReferenceReasonCode(resumed['reason_code']),
        proposed_artifacts=() if resumed is None else tuple(
            ValidatedArtifactEvidence.from_mapping(item) for item in resumed['proposed_artifacts']),
    )
    return _record_transition(
        state,
        target,
        kind=ReferenceTransitionKind.PUBLICATION,
        request_id=pending.review_request_id,
        affected_fluorophore=pending.affected_fluorophore,
        artifact_bindings=pointers,
        publication_evidence=artifacts,
    )


def revise_component_preparation_checkpoint(state, *, fluorophore, checkpoint, evidence):
    """Invalidate one completed component phase, retaining independent work."""
    phases = (ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
        ReferenceCheckpointKind.AF_REFERENCE_ROIS, ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
        ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS, ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
        ReferenceCheckpointKind.EXTRACTION_QC, ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS)
    if (state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
            or state.basis_source != 'construct_reference_library' or fluorophore not in state.fluorophores
            or checkpoint not in phases):
        raise ValueError('This component preparation cannot be revised at the current review')
    if (state.request.proposed_artifacts and state.current_fluorophore is not None
            and state.current_fluorophore != fluorophore
            and state.current_checkpoint is not ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION):
        raise ValueError('Finish the interrupted scientific publication before changing another component')
    affected_phases = {*phases[phases.index(checkpoint):], ReferenceCheckpointKind.TARGET_AF,
        ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW, ReferenceCheckpointKind.FINAL_RUN_BASIS}
    superseded = tuple(dict.fromkeys((*(item.request.request_id for item in state.active_decisions
        if item.request.checkpoint in affected_phases and
        (item.request.fluorophore == fluorophore or item.request.fluorophore is None)), state.request.request_id)))
    target = _build_checkpoint(run_id=state.run_id, target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores, measurement_ids=state.measurement_ids, af_behavior=state.af_behavior,
        revision=state.revision + 1, status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=state.basis_source, control_strategy=state.control_strategy,
        current_fluorophore=fluorophore,
        fluorophore_artifacts=tuple(FluorophoreArtifactSlot(slot.fluorophore)
            if slot.fluorophore == fluorophore else slot for slot in state.fluorophore_artifacts),
        accepted_library=None, available_reference_control=state.available_reference_control,
        target_control_compatibility=None,
        target_af_artifact=None, final_basis_artifact=None, decisions=state.decisions,
        transition_log=state.transition_log, pending_publication=None, checkpoint=checkpoint,
        evidence=evidence, prior_checkpoint_sha256=state.content_sha256)
    return _record_transition(state, target, kind=ReferenceTransitionKind.PREPARATION_REVISION,
        request_id=state.request.request_id, affected_fluorophore=fluorophore,
        superseded_request_ids=superseded)


def reopen_stopped_reference_checkpoint(
    state: ReferenceWorkflowCheckpoint, *, previous_checkpoint_sha256: str,
) -> ReferenceWorkflowCheckpoint:
    """Reopen the exact review that a Stop choice aborted, retaining saved spectra.

    We require the last decision to be the abort of the supplied checkpoint.
    A new request and invalidation transition retain the abort in history while
    superseding that decision; old request tokens cannot submit new responses.
    """
    if state.status is not ReferenceWorkflowStatus.ABORTED or not state.decisions:
        raise ValueError("Only a stopped spectral review can be reopened.")
    stopped = state.decisions[-1]
    if (stopped.response.action is not ReferenceActionCode.ABORT
            or stopped.source_checkpoint_sha256 != previous_checkpoint_sha256
            or state.prior_checkpoint_sha256 != previous_checkpoint_sha256):
        raise ValueError("The stopped review differs from the selected saved checkpoint.")
    request = stopped.request
    target = _build_checkpoint(
        run_id=state.run_id, target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores, measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1, status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=state.basis_source, control_strategy=state.control_strategy,
        current_fluorophore=request.fluorophore, fluorophore_artifacts=state.fluorophore_artifacts,
        accepted_library=state.accepted_library,
        available_reference_control=state.available_reference_control,
        target_control_compatibility=state.target_control_compatibility,
        target_af_artifact=state.target_af_artifact, final_basis_artifact=None,
        decisions=state.decisions, transition_log=state.transition_log,
        pending_publication=None, checkpoint=request.checkpoint, evidence=request.evidence,
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=request.reason.code, proposed_artifacts=request.proposed_artifacts,
    )
    return _record_transition(
        state, target, kind=ReferenceTransitionKind.INVALIDATION,
        request_id=request.request_id, affected_fluorophore=request.fluorophore,
        superseded_request_ids=(request.request_id,),
    )


def resume_failed_reference_checkpoint(
    state: ReferenceWorkflowCheckpoint,
    *,
    checkpoint: ReferenceCheckpointKind,
    fluorophore: str | None,
    evidence: tuple[ReferenceEvidence, ...],
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = (),
    reason_code: ReferenceReasonCode,
) -> ReferenceWorkflowCheckpoint:
    """Resume a failed ROI/control dependency without discarding unrelated spectra."""
    if state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE:
        raise ValueError("only an active workflow can resume a failed checkpoint")
    if checkpoint not in {
        ReferenceCheckpointKind.REFERENCE_CONTROL_FILES,
        ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
        ReferenceCheckpointKind.AF_REFERENCE_ROIS,
        ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
        ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS,
    }:
        raise ValueError("failure resume must target a file or ROI checkpoint")
    if checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES:
        if fluorophore is not None:
            raise ValueError("file/control resume is not fluorophore-scoped")
        fluorophore_artifacts = tuple(FluorophoreArtifactSlot(item) for item in state.fluorophores)
    else:
        if fluorophore not in state.fluorophores:
            raise ValueError("failure resume fluorophore is outside the workflow")
        fluorophore_artifacts = tuple(
            FluorophoreArtifactSlot(item.fluorophore) if item.fluorophore == fluorophore else item
            for item in state.fluorophore_artifacts
        )
    downstream = {
        ReferenceCheckpointKind.TARGET_AF,
        ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
        ReferenceCheckpointKind.FINAL_RUN_BASIS,
    }
    superseded = tuple(
        item.request.request_id
        for item in state.decisions
        if (
            checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES
            or item.request.fluorophore == fluorophore
            or item.request.checkpoint in downstream
        )
    )
    if state.request.request_id not in superseded:
        superseded = (*superseded, state.request.request_id)
    target = _build_checkpoint(
        run_id=state.run_id,
        target_image_sha256=state.target_image_sha256,
        fluorophores=state.fluorophores,
        measurement_ids=state.measurement_ids,
        af_behavior=state.af_behavior,
        revision=state.revision + 1,
        status=ReferenceWorkflowStatus.AWAITING_RESPONSE,
        basis_source=state.basis_source,
        control_strategy=state.control_strategy,
        current_fluorophore=fluorophore,
        fluorophore_artifacts=fluorophore_artifacts,
        accepted_library=None,
        available_reference_control=(
            None
            if checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES
            else state.available_reference_control
        ),
        target_control_compatibility=(
            None
            if checkpoint is ReferenceCheckpointKind.REFERENCE_CONTROL_FILES
            else state.target_control_compatibility
        ),
        target_af_artifact=None,
        final_basis_artifact=None,
        decisions=state.decisions,
        transition_log=state.transition_log,
        pending_publication=None,
        checkpoint=checkpoint,
        evidence=evidence,
        prior_checkpoint_sha256=state.content_sha256,
        reason_code=reason_code,
        proposed_artifacts=proposed_artifacts,
    )
    return _record_transition(
        state,
        target,
        kind=ReferenceTransitionKind.INVALIDATION,
        request_id=state.request.request_id,
        affected_fluorophore=fluorophore,
        superseded_request_ids=superseded,
    )


@dataclass(frozen=True, slots=True)
class HeadlessDecision:
    """One ordered configured decision; no checkpoint receives a default."""

    option_value: str
    reviewer_id: str
    selections: tuple[SelectedIdentifier, ...] = ()
    artifacts: tuple[ArtifactPointer, ...] = ()
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = ()
    next_evidence: tuple[ReferenceEvidence, ...] = ()
    next_proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = ()
    published_artifacts: tuple[PublishedArtifactEvidence, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.option_value, "headless option value")
        _required_text(self.reviewer_id, "headless reviewer ID")


def run_reference_workflow_headless(
    initial: ReferenceWorkflowCheckpoint,
    decisions: tuple[HeadlessDecision, ...],
) -> ReferenceWorkflowCheckpoint:
    """Run only with an explicit validated decision for every reached request."""
    state = initial
    for configured in decisions:
        if state.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE or state.request is None:
            raise ValueError("headless configuration contains unused decisions")
        if configured.proposed_artifacts:
            state = propose_reference_artifacts(state, configured.proposed_artifacts)
        response = configured_response(
            state.request,
            option_value=configured.option_value,
            reviewer_id=configured.reviewer_id,
            selections=configured.selections,
            artifacts=configured.artifacts,
        )
        state = advance_reference_workflow(
            state,
            response,
            next_evidence=configured.next_evidence,
            next_proposed_artifacts=configured.next_proposed_artifacts,
        )
        if state.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION:
            if not configured.published_artifacts:
                raise ValueError("headless workflow lacks a configured publication")
            state = publish_reference_artifacts(state, configured.published_artifacts)
        elif configured.published_artifacts:
            raise ValueError("headless configuration contains unused publication artifacts")
    if state.status in {
        ReferenceWorkflowStatus.AWAITING_RESPONSE,
        ReferenceWorkflowStatus.AWAITING_PUBLICATION,
    }:
        raise ValueError("headless workflow lacks a configured decision")
    return state


def dumps_reference_workflow_checkpoint(state: ReferenceWorkflowCheckpoint) -> str:
    if type(state) is not ReferenceWorkflowCheckpoint:
        raise TypeError("state must be a ReferenceWorkflowCheckpoint")
    return _canonical_json(state.to_mapping()).decode("utf-8")


def loads_reference_workflow_checkpoint(payload: str | bytes) -> ReferenceWorkflowCheckpoint:
    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise ValueError(f"nonfinite JSON number is forbidden: {value}")

    try:
        root = json.loads(
            payload,
            object_pairs_hook=exact_object,
            parse_constant=reject_nonfinite,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("reference workflow checkpoint is not valid JSON") from exc
    if (
        isinstance(root, dict)
        and isinstance(root.get("request"), dict)
        and root["request"].get("checkpoint") == "ssasu_acceptance"
    ):
        raise ValueError(
            "This analysis uses retired background estimation. Start a reference-based unmixing analysis with an autofluorescence library or unstained control."
        )
    return ReferenceWorkflowCheckpoint.from_mapping(root)


__all__ = [
    "FluorophoreArtifactSlot",
    "HeadlessDecision",
    "ReferenceWorkflowCheckpoint",
    "ReferenceDecisionRecord",
    "ReferenceTransitionKind",
    "ReferenceTransitionRecord",
    "ReferenceWorkflowStatus",
    "advance_reference_workflow",
    "dumps_reference_workflow_checkpoint",
    "loads_reference_workflow_checkpoint",
    "propose_reference_artifacts",
    "propose_reference_evidence",
    "publish_reference_artifacts",
    "reopen_stopped_reference_checkpoint",
    "resume_failed_reference_checkpoint",
    "run_reference_workflow_headless",
    "start_reference_workflow",
]
