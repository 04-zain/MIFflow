"""Immutable, presentation-neutral spectral-reference interactions.

The records in this module deliberately retain identifiers, hashes, and small review
summaries only. WSI pixels and sampled arrays belong to stage-owned artifacts, never to a
resumable interaction payload.

The `TargetControlCompatibilityEvidence` record carries `control_artifact`,
`target_image_sha256`, `preparation_compatibility_decision`,
`tissue_context_compatibility_decision`, `decision_authority_id` and its remaining declared
fields.

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
import math
import re
from typing import TypeAlias

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_LENGTH = 4096
_MAX_SUMMARY_FIELDS = 128
_MAX_SELECTIONS = 512
_MAX_INTERACTION_BYTES = 262_144


# ==============================================================================
#                                  Interactions
# ==============================================================================


class ReferenceCheckpointKind(str, Enum):
    BASIS_SOURCE = "basis_source"
    CONTROL_STRATEGY = "control_strategy"
    REFERENCE_CONTROL_FILES = "reference_control_files"
    SHARED_CONTROL_PREPARATION = "shared_control_preparation"
    POSITIVE_REFERENCE_ROIS = "positive_reference_rois"
    AF_REFERENCE_ROIS = "af_reference_rois"
    NEGATIVE_REFERENCE_ROIS = "negative_reference_rois"
    BACKGROUND_REFERENCE_ROIS = "background_reference_rois"
    SIGNAL_DARK_MEASUREMENTS = "signal_dark_measurements"
    EXTRACTION_QC = "extraction_qc"
    FLUOROPHORE_CONSENSUS = "fluorophore_consensus"
    TARGET_AF = "target_af"
    TARGET_AF_CONTROL_REVIEW = "target_af_control_review"
    FINAL_RUN_BASIS = "final_run_basis"


class ReferenceReasonCode(str, Enum):
    SPECTRAL_BASIS_SOURCE_REQUIRED = "spectral_basis_source_required"
    REFERENCE_IMAGE_REQUIRED = "reference_image_required"
    REFERENCE_CONTROL_STRATEGY_REQUIRED = "reference_control_strategy_required"
    UNSTAINED_REFERENCE_REQUIRED_OR_FALLBACK_CHOICE = (
        "unstained_reference_required_or_fallback_choice"
    )
    TARGET_AF_SOURCE_REQUIRED = "target_af_source_required"
    POSITIVE_REFERENCE_ROIS_REQUIRED = "positive_reference_rois_required"
    AF_REFERENCE_ROIS_REQUIRED = "af_reference_rois_required"
    SHARED_CONTROL_PREPARATION_REQUIRED = "shared_control_preparation_required"
    NEGATIVE_REFERENCE_ROIS_REQUIRED = "negative_reference_rois_required"
    REFERENCE_BACKGROUND_MODEL_REQUIRED = "reference_background_model_required"
    REFERENCE_BACKGROUND_ROIS_REQUIRED = "reference_background_rois_required"
    REFERENCE_BACKGROUND_QC_FAILED = "reference_background_qc_failed"
    REFERENCE_BAND_SELECTION_REQUIRED = "reference_band_selection_required"
    REFERENCE_SPECTRUM_QC_FAILED = "reference_spectrum_qc_failed"
    REFERENCE_SPECTRUM_REVIEW_REQUIRED = "reference_spectrum_review_required"
    TARGET_AF_REVIEW_REQUIRED = "target_af_review_required"
    TARGET_AF_MODEL_QC_FAILED = "target_af_model_qc_failed"
    FINAL_RUN_BASIS_REVIEW_REQUIRED = "final_run_basis_review_required"


class ReferenceActionCode(str, Enum):
    SELECT = "select"
    ACCEPT = "accept"
    RETRY = "retry"
    RESELECT = "reselect"
    ABORT = "abort"


_ALLOWED_REASONS = {
    ReferenceCheckpointKind.SHARED_CONTROL_PREPARATION: {
        ReferenceReasonCode.SHARED_CONTROL_PREPARATION_REQUIRED,
    },
    ReferenceCheckpointKind.BASIS_SOURCE: {
        ReferenceReasonCode.SPECTRAL_BASIS_SOURCE_REQUIRED,
    },
    ReferenceCheckpointKind.REFERENCE_CONTROL_FILES: {
        ReferenceReasonCode.REFERENCE_IMAGE_REQUIRED,
        ReferenceReasonCode.UNSTAINED_REFERENCE_REQUIRED_OR_FALLBACK_CHOICE,
    },
    ReferenceCheckpointKind.CONTROL_STRATEGY: {
        ReferenceReasonCode.REFERENCE_CONTROL_STRATEGY_REQUIRED,
    },
    ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS: {
        ReferenceReasonCode.POSITIVE_REFERENCE_ROIS_REQUIRED,
    },
    ReferenceCheckpointKind.AF_REFERENCE_ROIS: {
        ReferenceReasonCode.AF_REFERENCE_ROIS_REQUIRED,
        ReferenceReasonCode.UNSTAINED_REFERENCE_REQUIRED_OR_FALLBACK_CHOICE,
    },
    ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS: {
        ReferenceReasonCode.NEGATIVE_REFERENCE_ROIS_REQUIRED,
    },
    ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS: {
        ReferenceReasonCode.REFERENCE_BACKGROUND_MODEL_REQUIRED,
        ReferenceReasonCode.REFERENCE_BACKGROUND_ROIS_REQUIRED,
        ReferenceReasonCode.REFERENCE_BACKGROUND_QC_FAILED,
    },
    ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS: {
        ReferenceReasonCode.REFERENCE_BAND_SELECTION_REQUIRED,
    },
    ReferenceCheckpointKind.EXTRACTION_QC: {
        ReferenceReasonCode.REFERENCE_SPECTRUM_QC_FAILED,
    },
    ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS: {
        ReferenceReasonCode.REFERENCE_SPECTRUM_REVIEW_REQUIRED,
    },
    ReferenceCheckpointKind.TARGET_AF: {
        ReferenceReasonCode.TARGET_AF_SOURCE_REQUIRED,
        ReferenceReasonCode.TARGET_AF_REVIEW_REQUIRED,
        ReferenceReasonCode.TARGET_AF_MODEL_QC_FAILED,
    },
    ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW: {
        ReferenceReasonCode.TARGET_AF_REVIEW_REQUIRED,
        ReferenceReasonCode.TARGET_AF_MODEL_QC_FAILED,
    },
    ReferenceCheckpointKind.FINAL_RUN_BASIS: {
        ReferenceReasonCode.FINAL_RUN_BASIS_REVIEW_REQUIRED,
    },
}


Scalar: TypeAlias = str | int | float | bool | None


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be nonempty literal text")
    if len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{field_name} is too long for interaction state")
    return value


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _tuple_of_text(value: object, field_name: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_SELECTIONS:
        raise TypeError(f"{field_name} must be a bounded tuple of text")
    values = tuple(_text(item, field_name) for item in value)
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} values must be unique")
    return values


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _exact_mapping(value: object, expected: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} schema fields differ")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceField:
    name: str
    value: Scalar

    def __post_init__(self) -> None:
        _text(self.name, "evidence field name")
        if not (self.value is None or type(self.value) in {str, int, float, bool}):
            raise ValueError("evidence field value must be a JSON scalar")
        if isinstance(self.value, str):
            _text(self.value, "evidence field value")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("evidence field value must be finite")
        if type(self.value) is int and abs(self.value) > 2**63 - 1:
            raise ValueError("evidence integer exceeds signed 64-bit summary range")

    def to_mapping(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_mapping(cls, value: object) -> "EvidenceField":
        root = _exact_mapping(value, {"name", "value"}, "evidence field")
        return cls(name=root["name"], value=root["value"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ArtifactPointer:
    role: str
    artifact_id: str
    artifact_sha256: str
    subject_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.role, "artifact role")
        _text(self.artifact_id, "artifact ID")
        _sha256(self.artifact_sha256, "artifact SHA-256")
        if self.subject_id is not None:
            _text(self.subject_id, "artifact subject ID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "role": self.role,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactPointer":
        root = _exact_mapping(
            value,
            {"role", "artifact_id", "artifact_sha256", "subject_id"},
            "artifact pointer",
        )
        return cls(**root)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TargetControlCompatibilityEvidence:
    """Reviewed target/control compatibility; never inferred from image type alone."""

    control_artifact: ArtifactPointer
    target_image_sha256: str
    preparation_compatibility_decision: str
    tissue_context_compatibility_decision: str
    decision_authority_id: str
    decision_evidence_sha256: str
    structural_report_id: str
    structural_report_sha256: str
    structural_report_accepted: bool
    quantitative_report_id: str
    quantitative_report_sha256: str
    quantitative_report_accepted: bool
    reviewer_id: str
    policy_id: str
    policy_version: str

    def __post_init__(self) -> None:
        if type(self.control_artifact) is not ArtifactPointer or (
            self.control_artifact.role
            not in {"unstained_reference_image", "target_unstained_control_image", "negative_reference_image"}
        ):
            raise ValueError("target-control evidence requires an unstained control artifact")
        _sha256(self.target_image_sha256, "target-control target image SHA-256")
        for decision, name in (
            (self.preparation_compatibility_decision, "preparation compatibility"),
            (self.tissue_context_compatibility_decision, "tissue-context compatibility"),
        ):
            if decision not in {"accepted", "rejected"}:
                raise ValueError(f"{name} decision is unsupported")
        _text(self.decision_authority_id, "target-control decision authority ID")
        _sha256(self.decision_evidence_sha256, "target-control decision evidence SHA-256")
        _text(self.structural_report_id, "target-control structural report ID")
        _sha256(self.structural_report_sha256, "target-control structural report SHA-256")
        if type(self.structural_report_accepted) is not bool:
            raise TypeError("target-control structural report acceptance must be bool")
        _text(self.quantitative_report_id, "target-control quantitative report ID")
        _sha256(self.quantitative_report_sha256, "target-control quantitative report SHA-256")
        if type(self.quantitative_report_accepted) is not bool:
            raise TypeError("target-control quantitative report acceptance must be bool")
        _text(self.reviewer_id, "target-control reviewer ID")
        _text(self.policy_id, "target-control policy ID")
        _text(self.policy_version, "target-control policy version")

    @property
    def accepted(self) -> bool:
        return (
            self.preparation_compatibility_decision == "accepted"
            and self.tissue_context_compatibility_decision == "accepted"
            and self.structural_report_accepted
            and self.quantitative_report_accepted
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "control_artifact": self.control_artifact.to_mapping(),
            "target_image_sha256": self.target_image_sha256,
            "preparation_compatibility_decision": self.preparation_compatibility_decision,
            "tissue_context_compatibility_decision": self.tissue_context_compatibility_decision,
            "decision_authority_id": self.decision_authority_id,
            "decision_evidence_sha256": self.decision_evidence_sha256,
            "structural_report_id": self.structural_report_id,
            "structural_report_sha256": self.structural_report_sha256,
            "structural_report_accepted": self.structural_report_accepted,
            "quantitative_report_id": self.quantitative_report_id,
            "quantitative_report_sha256": self.quantitative_report_sha256,
            "quantitative_report_accepted": self.quantitative_report_accepted,
            "reviewer_id": self.reviewer_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TargetControlCompatibilityEvidence":
        expected = {
            "control_artifact",
            "target_image_sha256",
            "preparation_compatibility_decision",
            "tissue_context_compatibility_decision",
            "decision_authority_id",
            "decision_evidence_sha256",
            "structural_report_id",
            "structural_report_sha256",
            "structural_report_accepted",
            "quantitative_report_id",
            "quantitative_report_sha256",
            "quantitative_report_accepted",
            "reviewer_id",
            "policy_id",
            "policy_version",
        }
        root = _exact_mapping(value, expected, "target-control compatibility evidence")
        return cls(
            control_artifact=ArtifactPointer.from_mapping(root["control_artifact"]),
            target_image_sha256=root["target_image_sha256"],  # type: ignore[arg-type]
            preparation_compatibility_decision=root["preparation_compatibility_decision"],  # type: ignore[arg-type]
            tissue_context_compatibility_decision=root["tissue_context_compatibility_decision"],  # type: ignore[arg-type]
            decision_authority_id=root["decision_authority_id"],  # type: ignore[arg-type]
            decision_evidence_sha256=root["decision_evidence_sha256"],  # type: ignore[arg-type]
            structural_report_id=root["structural_report_id"],  # type: ignore[arg-type]
            structural_report_sha256=root["structural_report_sha256"],  # type: ignore[arg-type]
            structural_report_accepted=root["structural_report_accepted"],  # type: ignore[arg-type]
            quantitative_report_id=root["quantitative_report_id"],  # type: ignore[arg-type]
            quantitative_report_sha256=root["quantitative_report_sha256"],  # type: ignore[arg-type]
            quantitative_report_accepted=root["quantitative_report_accepted"],  # type: ignore[arg-type]
            reviewer_id=root["reviewer_id"],  # type: ignore[arg-type]
            policy_id=root["policy_id"],  # type: ignore[arg-type]
            policy_version=root["policy_version"],  # type: ignore[arg-type]
        )


_ARTIFACT_TYPES = {
    "canonical_ome_tiff",
    "reviewed_fluorophore_library",
    "qupath_roi_geometry",
    "measurement_selection",
    "extracted_fluorophore_spectrum",
    "target_af_model",
    "resolved_run_basis",
    "final_run_basis",
    "qc_evidence",
}
_ROLE_ARTIFACT_TYPES = {
    "shared_control_preparation": "qupath_roi_geometry",
    "candidate_fluorophore_library": "reviewed_fluorophore_library",
    "proposed_fluorophore_consensus_qc": "qc_evidence",
    "proposed_target_af_qc": "qc_evidence",
    "proposed_run_basis_qc": "qc_evidence",
    "accepted_fluorophore_library": "reviewed_fluorophore_library",
    "positive_reference_image": "canonical_ome_tiff",
    "unstained_reference_image": "canonical_ome_tiff",
    "negative_reference_image": "canonical_ome_tiff",
    "target_unstained_control_image": "canonical_ome_tiff",
    "positive_reference_rois": "qupath_roi_geometry",
    "af_reference_rois": "qupath_roi_geometry",
    "negative_reference_rois": "qupath_roi_geometry",
    "background_reference_rois": "qupath_roi_geometry",
    "extraction_qc": "qc_evidence",
    "accepted_fluorophore_spectrum": "extracted_fluorophore_spectrum",
    "accepted_target_af": "target_af_model",
    "resolved_run_basis": "resolved_run_basis",
    "final_run_basis": "final_run_basis",
}

_PUBLISHED_ROLE_ARTIFACT_TYPES = {
    "accepted_fluorophore_library": "reviewed_fluorophore_library",
    "accepted_fluorophore_spectrum": "extracted_fluorophore_spectrum",
    "accepted_target_af": "target_af_model",
    "resolved_run_basis": "resolved_run_basis",
    "final_run_basis": "final_run_basis",
}


@dataclass(frozen=True, slots=True)
class ValidatedArtifactEvidence:
    """Typed evidence that bytes passed their owning strict artifact boundary."""

    pointer: ArtifactPointer
    artifact_type: str
    validator_id: str
    validation_evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.pointer) is not ArtifactPointer:
            raise TypeError("validated artifact pointer is invalid")
        if self.artifact_type not in _ARTIFACT_TYPES:
            raise ValueError("validated artifact type is unsupported")
        if _ROLE_ARTIFACT_TYPES.get(self.pointer.role) != self.artifact_type:
            raise ValueError("validated artifact type does not match its semantic role")
        _text(self.validator_id, "artifact validator ID")
        _sha256(
            self.validation_evidence_sha256,
            "artifact validation evidence SHA-256",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "pointer": self.pointer.to_mapping(),
            "artifact_type": self.artifact_type,
            "validator_id": self.validator_id,
            "validation_evidence_sha256": self.validation_evidence_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ValidatedArtifactEvidence":
        root = _exact_mapping(
            value,
            {
                "pointer",
                "artifact_type",
                "validator_id",
                "validation_evidence_sha256",
            },
            "validated artifact evidence",
        )
        return cls(
            pointer=ArtifactPointer.from_mapping(root["pointer"]),
            artifact_type=root["artifact_type"],  # type: ignore[arg-type]
            validator_id=root["validator_id"],  # type: ignore[arg-type]
            validation_evidence_sha256=root["validation_evidence_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PublishedArtifactEvidence:
    """Accepted output published after, and bound to, an exact review decision."""

    pointer: ArtifactPointer
    artifact_type: str
    publisher_id: str
    validator_id: str
    publication_evidence_sha256: str
    review_request_id: str
    proposal_artifacts: tuple[ArtifactPointer, ...]

    def __post_init__(self) -> None:
        if type(self.pointer) is not ArtifactPointer:
            raise TypeError("published artifact pointer is invalid")
        if _PUBLISHED_ROLE_ARTIFACT_TYPES.get(self.pointer.role) != self.artifact_type:
            raise ValueError("published artifact type does not match its accepted role")
        _text(self.publisher_id, "artifact publisher ID")
        _text(self.validator_id, "published artifact validator ID")
        _sha256(
            self.publication_evidence_sha256,
            "artifact publication evidence SHA-256",
        )
        _sha256(self.review_request_id, "artifact publication review request ID")
        if (
            type(self.proposal_artifacts) is not tuple
            or not self.proposal_artifacts
            or len(self.proposal_artifacts) > 512
            or any(type(item) is not ArtifactPointer for item in self.proposal_artifacts)
        ):
            raise ValueError("published artifact requires bounded proposal lineage")
        if len(set(self.proposal_artifacts)) != len(self.proposal_artifacts):
            raise ValueError("published proposal lineage must be unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "pointer": self.pointer.to_mapping(),
            "artifact_type": self.artifact_type,
            "publisher_id": self.publisher_id,
            "validator_id": self.validator_id,
            "publication_evidence_sha256": self.publication_evidence_sha256,
            "review_request_id": self.review_request_id,
            "proposal_artifacts": [item.to_mapping() for item in self.proposal_artifacts],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "PublishedArtifactEvidence":
        root = _exact_mapping(
            value,
            {
                "pointer",
                "artifact_type",
                "publisher_id",
                "validator_id",
                "publication_evidence_sha256",
                "review_request_id",
                "proposal_artifacts",
            },
            "published artifact evidence",
        )
        if not isinstance(root["proposal_artifacts"], list):
            raise ValueError("published proposal lineage must be an array")
        return cls(
            pointer=ArtifactPointer.from_mapping(root["pointer"]),
            artifact_type=root["artifact_type"],  # type: ignore[arg-type]
            publisher_id=root["publisher_id"],  # type: ignore[arg-type]
            validator_id=root["validator_id"],  # type: ignore[arg-type]
            publication_evidence_sha256=root["publication_evidence_sha256"],  # type: ignore[arg-type]
            review_request_id=root["review_request_id"],  # type: ignore[arg-type]
            proposal_artifacts=tuple(
                ArtifactPointer.from_mapping(item) for item in root["proposal_artifacts"]
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRequirement:
    role: str
    subject_id: str | None

    def __post_init__(self) -> None:
        _text(self.role, "artifact requirement role")
        if self.subject_id is not None:
            _text(self.subject_id, "artifact requirement subject ID")

    def to_mapping(self) -> dict[str, object]:
        return {"role": self.role, "subject_id": self.subject_id}

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactRequirement":
        root = _exact_mapping(
            value,
            {"role", "subject_id"},
            "artifact requirement",
        )
        return cls(
            role=root["role"],  # type: ignore[arg-type]
            subject_id=root["subject_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class SelectedIdentifier:
    role: str
    identifier: str

    def __post_init__(self) -> None:
        _text(self.role, "selected identifier role")
        _text(self.identifier, "selected identifier")

    def to_mapping(self) -> dict[str, object]:
        return {"role": self.role, "identifier": self.identifier}

    @classmethod
    def from_mapping(cls, value: object) -> "SelectedIdentifier":
        root = _exact_mapping(
            value,
            {"role", "identifier"},
            "selected identifier",
        )
        return cls(
            role=root["role"],  # type: ignore[arg-type]
            identifier=root["identifier"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReferenceEvidence:
    evidence_id: str
    kind: str
    artifact_id: str
    artifact_sha256: str
    summary: tuple[EvidenceField, ...] = ()
    suggested_ids: tuple[str, ...] = ()
    ambiguous: bool = False

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence ID")
        _text(self.kind, "evidence kind")
        _text(self.artifact_id, "evidence artifact ID")
        _sha256(self.artifact_sha256, "evidence artifact SHA-256")
        if type(self.summary) is not tuple or len(self.summary) > _MAX_SUMMARY_FIELDS:
            raise TypeError("evidence summary must be a bounded tuple")
        if any(type(item) is not EvidenceField for item in self.summary):
            raise TypeError("evidence summary must contain EvidenceField records")
        if len({item.name for item in self.summary}) != len(self.summary):
            raise ValueError("evidence summary field names must be unique")
        _tuple_of_text(self.suggested_ids, "suggested IDs")
        if type(self.ambiguous) is not bool:
            raise TypeError("evidence ambiguity must be bool")
        if self.ambiguous and len(self.suggested_ids) < 2:
            raise ValueError("ambiguous suggestion evidence requires multiple candidates")
        if len(_canonical_json(self.to_mapping())) > _MAX_INTERACTION_BYTES:
            raise ValueError("reference evidence exceeds serialized size limit")

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "summary": [item.to_mapping() for item in self.summary],
            "suggested_ids": list(self.suggested_ids),
            "ambiguous": self.ambiguous,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceEvidence":
        root = _exact_mapping(
            value,
            {
                "evidence_id",
                "kind",
                "artifact_id",
                "artifact_sha256",
                "summary",
                "suggested_ids",
                "ambiguous",
            },
            "reference evidence",
        )
        if not isinstance(root["summary"], list) or not isinstance(root["suggested_ids"], list):
            raise ValueError("reference evidence arrays are invalid")
        return cls(
            evidence_id=root["evidence_id"],  # type: ignore[arg-type]
            kind=root["kind"],  # type: ignore[arg-type]
            artifact_id=root["artifact_id"],  # type: ignore[arg-type]
            artifact_sha256=root["artifact_sha256"],  # type: ignore[arg-type]
            summary=tuple(EvidenceField.from_mapping(item) for item in root["summary"]),
            suggested_ids=tuple(root["suggested_ids"]),
            ambiguous=root["ambiguous"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReferenceReason:
    code: ReferenceReasonCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ReferenceReasonCode):
            raise TypeError("reference reason code is invalid")
        _text(self.message, "reference reason message")

    def to_mapping(self) -> dict[str, object]:
        return {"code": self.code.value, "message": self.message}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReason":
        root = _exact_mapping(value, {"code", "message"}, "reference reason")
        try:
            code = ReferenceReasonCode(root["code"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference reason code is unsupported") from exc
        return cls(code=code, message=root["message"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReferenceAction:
    code: ReferenceActionCode
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ReferenceActionCode):
            raise TypeError("reference action code is invalid")
        _text(self.label, "reference action label")

    def to_mapping(self) -> dict[str, object]:
        return {"code": self.code.value, "label": self.label}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceAction":
        root = _exact_mapping(value, {"code", "label"}, "reference action")
        try:
            code = ReferenceActionCode(root["code"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference action code is unsupported") from exc
        return cls(code=code, label=root["label"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReferenceOption:
    value: str
    label: str
    description: str
    action: ReferenceAction
    minimum_selected_ids: int = 0
    minimum_artifacts: int = 0
    artifact_requirements: tuple[ArtifactRequirement, ...] = ()

    def __post_init__(self) -> None:
        _text(self.value, "reference option value")
        _text(self.label, "reference option label")
        _text(self.description, "reference option description")
        if type(self.action) is not ReferenceAction:
            raise TypeError("reference option action is invalid")
        if (
            type(self.minimum_selected_ids) is not int
            or self.minimum_selected_ids < 0
            or type(self.minimum_artifacts) is not int
            or self.minimum_artifacts < 0
        ):
            raise ValueError("reference option minimum counts must be nonnegative integers")
        if type(self.artifact_requirements) is not tuple or any(
            type(item) is not ArtifactRequirement for item in self.artifact_requirements
        ):
            raise TypeError("reference option artifact requirements are invalid")
        requirement_keys = tuple(
            (item.role, item.subject_id) for item in self.artifact_requirements
        )
        if len(set(requirement_keys)) != len(requirement_keys):
            raise ValueError("reference option artifact requirements must be unique")
        if self.artifact_requirements and self.minimum_artifacts:
            raise ValueError("exact artifact requirements replace minimum artifact counts")

    def to_mapping(self) -> dict[str, object]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
            "action": self.action.to_mapping(),
            "minimum_selected_ids": self.minimum_selected_ids,
            "minimum_artifacts": self.minimum_artifacts,
            "artifact_requirements": [item.to_mapping() for item in self.artifact_requirements],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceOption":
        root = _exact_mapping(
            value,
            {
                "value",
                "label",
                "description",
                "action",
                "minimum_selected_ids",
                "minimum_artifacts",
                "artifact_requirements",
            },
            "reference option",
        )
        if not isinstance(root["artifact_requirements"], list):
            raise ValueError("reference option artifact requirements are invalid")
        return cls(
            value=root["value"],  # type: ignore[arg-type]
            label=root["label"],  # type: ignore[arg-type]
            description=root["description"],  # type: ignore[arg-type]
            action=ReferenceAction.from_mapping(root["action"]),
            minimum_selected_ids=root["minimum_selected_ids"],  # type: ignore[arg-type]
            minimum_artifacts=root["minimum_artifacts"],  # type: ignore[arg-type]
            artifact_requirements=tuple(
                ArtifactRequirement.from_mapping(item) for item in root["artifact_requirements"]
            ),
        )


@dataclass(frozen=True, slots=True)
class ReferenceInteractionRequest:
    SCHEMA_NAME = "mif_pipeline.spectral_reference_interaction_request"
    SCHEMA_VERSION = 1

    request_id: str
    run_id: str
    target_image_sha256: str
    fluorophores: tuple[str, ...]
    measurement_ids: tuple[str, ...]
    target_control_compatibility: TargetControlCompatibilityEvidence | None
    authoritative_state_sha256: str
    revision: int
    checkpoint: ReferenceCheckpointKind
    fluorophore: str | None
    reason: ReferenceReason
    options: tuple[ReferenceOption, ...]
    evidence: tuple[ReferenceEvidence, ...]
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...]
    prior_checkpoint_sha256: str | None

    def __post_init__(self) -> None:
        """Validate the ReferenceInteractionRequest fields when the record is constructed.

        The checks cover ``proposed_artifacts``, ``request_id``, ``run_id``,
        ``target_image_sha256``, ``fluorophores``, ``measurement_ids``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _sha256(self.request_id, "request ID")
        _text(self.run_id, "request run ID")
        _sha256(self.target_image_sha256, "request target image SHA-256")
        _tuple_of_text(self.fluorophores, "request fluorophores")
        _tuple_of_text(self.measurement_ids, "request measurement IDs")
        if not self.measurement_ids:
            raise ValueError("request measurement inventory must be nonempty")
        if self.target_control_compatibility is not None and (
            type(self.target_control_compatibility) is not TargetControlCompatibilityEvidence
            or self.target_control_compatibility.target_image_sha256 != self.target_image_sha256
        ):
            raise ValueError("request target-control evidence does not bind target image")
        _sha256(
            self.authoritative_state_sha256,
            "request authoritative state SHA-256",
        )
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("request revision must be a nonnegative integer")
        if not isinstance(self.checkpoint, ReferenceCheckpointKind):
            raise TypeError("request checkpoint is invalid")
        if self.fluorophore is not None:
            _text(self.fluorophore, "request fluorophore")
            if self.fluorophore not in self.fluorophores:
                raise ValueError(
                    "request fluorophore must belong to the declared fluorophore inventory"
                )
        if type(self.reason) is not ReferenceReason:
            raise TypeError("request reason is invalid")
        if self.reason.code not in _ALLOWED_REASONS[self.checkpoint]:
            raise ValueError("request reason is invalid for its checkpoint")
        if type(self.options) is not tuple or not self.options or len(self.options) > 128:
            raise ValueError("request requires an immutable option inventory")
        if any(type(item) is not ReferenceOption for item in self.options):
            raise TypeError("request options are invalid")
        if len({item.value for item in self.options}) != len(self.options):
            raise ValueError("request option values must be unique")
        if (
            type(self.evidence) is not tuple
            or len(self.evidence) > 128
            or any(type(item) is not ReferenceEvidence for item in self.evidence)
        ):
            raise TypeError("request evidence is invalid")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("request evidence IDs must be unique")
        if (
            type(self.proposed_artifacts) is not tuple
            or len(self.proposed_artifacts) > 512
            or any(type(item) is not ValidatedArtifactEvidence for item in self.proposed_artifacts)
        ):
            raise TypeError("request proposed artifacts are invalid")
        proposed_keys = tuple(
            (item.pointer.role, item.pointer.subject_id) for item in self.proposed_artifacts
        )
        if len(set(proposed_keys)) != len(proposed_keys):
            raise ValueError("request proposed artifact bindings must be unique")
        if self.proposed_artifacts:
            allowed_inventories = {
                tuple((item.role, item.subject_id) for item in option.artifact_requirements)
                for option in self.options
                if option.artifact_requirements
            }
            if proposed_keys not in allowed_inventories:
                raise ValueError(
                    "request proposed artifacts do not match an exact option inventory"
                )
        if self.prior_checkpoint_sha256 is not None:
            _sha256(self.prior_checkpoint_sha256, "prior checkpoint SHA-256")
        if self.request_id != _request_id(self._content_mapping()):
            raise ValueError("request ID does not match immutable request content")
        if len(_canonical_json(self.to_mapping())) > _MAX_INTERACTION_BYTES:
            raise ValueError("reference request exceeds serialized size limit")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.SCHEMA_NAME,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "target_image_sha256": self.target_image_sha256,
            "fluorophores": list(self.fluorophores),
            "measurement_ids": list(self.measurement_ids),
            "target_control_compatibility": (
                None
                if self.target_control_compatibility is None
                else self.target_control_compatibility.to_mapping()
            ),
            "authoritative_state_sha256": self.authoritative_state_sha256,
            "revision": self.revision,
            "checkpoint": self.checkpoint.value,
            "fluorophore": self.fluorophore,
            "reason": self.reason.to_mapping(),
            "options": [item.to_mapping() for item in self.options],
            "evidence": [item.to_mapping() for item in self.evidence],
            "proposed_artifacts": [item.to_mapping() for item in self.proposed_artifacts],
            "prior_checkpoint_sha256": self.prior_checkpoint_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "request_id": self.request_id}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceInteractionRequest":
        """Decode the serialized fields into a validated ReferenceInteractionRequest record.

        The mapping is checked against the declared field inventory, including
        ``schema_name``, ``schema_version``, ``request_id``, ``run_id``,
        ``target_image_sha256``, ``fluorophores``. Nested values are decoded through the
        corresponding field validators before the record is constructed."""

        expected = {
            "schema_name",
            "schema_version",
            "request_id",
            "run_id",
            "target_image_sha256",
            "fluorophores",
            "measurement_ids",
            "target_control_compatibility",
            "authoritative_state_sha256",
            "revision",
            "checkpoint",
            "fluorophore",
            "reason",
            "options",
            "evidence",
            "proposed_artifacts",
            "prior_checkpoint_sha256",
        }
        root = _exact_mapping(value, expected, "reference interaction request")
        if (
            root["schema_name"] != cls.SCHEMA_NAME
            or type(root["schema_version"]) is not int
            or root["schema_version"] != cls.SCHEMA_VERSION
        ):
            raise ValueError("unsupported reference interaction request schema")
        if (
            not isinstance(root["options"], list)
            or not isinstance(root["evidence"], list)
            or not isinstance(root["proposed_artifacts"], list)
        ):
            raise ValueError("reference interaction request arrays are invalid")
        if not isinstance(root["fluorophores"], list) or not isinstance(
            root["measurement_ids"], list
        ):
            raise ValueError("reference interaction fluorophore inventory is invalid")
        try:
            checkpoint = ReferenceCheckpointKind(root["checkpoint"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference interaction checkpoint is unsupported") from exc
        return cls(
            request_id=root["request_id"],  # type: ignore[arg-type]
            run_id=root["run_id"],  # type: ignore[arg-type]
            target_image_sha256=root["target_image_sha256"],  # type: ignore[arg-type]
            fluorophores=tuple(root["fluorophores"]),
            measurement_ids=tuple(root["measurement_ids"]),
            target_control_compatibility=(
                None
                if root["target_control_compatibility"] is None
                else TargetControlCompatibilityEvidence.from_mapping(
                    root["target_control_compatibility"]
                )
            ),
            authoritative_state_sha256=root["authoritative_state_sha256"],  # type: ignore[arg-type]
            revision=root["revision"],  # type: ignore[arg-type]
            checkpoint=checkpoint,
            fluorophore=root["fluorophore"],  # type: ignore[arg-type]
            reason=ReferenceReason.from_mapping(root["reason"]),
            options=tuple(ReferenceOption.from_mapping(item) for item in root["options"]),
            evidence=tuple(ReferenceEvidence.from_mapping(item) for item in root["evidence"]),
            proposed_artifacts=tuple(
                ValidatedArtifactEvidence.from_mapping(item) for item in root["proposed_artifacts"]
            ),
            prior_checkpoint_sha256=root["prior_checkpoint_sha256"],  # type: ignore[arg-type]
        )


def make_reference_request(
    *,
    run_id: str,
    target_image_sha256: str,
    fluorophores: tuple[str, ...],
    measurement_ids: tuple[str, ...],
    target_control_compatibility: TargetControlCompatibilityEvidence | None,
    authoritative_state_sha256: str,
    revision: int,
    checkpoint: ReferenceCheckpointKind,
    fluorophore: str | None,
    reason: ReferenceReason,
    options: tuple[ReferenceOption, ...],
    evidence: tuple[ReferenceEvidence, ...] = (),
    proposed_artifacts: tuple[ValidatedArtifactEvidence, ...] = (),
    prior_checkpoint_sha256: str | None = None,
) -> ReferenceInteractionRequest:
    content = {
        "schema_name": ReferenceInteractionRequest.SCHEMA_NAME,
        "schema_version": ReferenceInteractionRequest.SCHEMA_VERSION,
        "run_id": run_id,
        "target_image_sha256": target_image_sha256,
        "fluorophores": list(fluorophores),
        "measurement_ids": list(measurement_ids),
        "target_control_compatibility": (
            None
            if target_control_compatibility is None
            else target_control_compatibility.to_mapping()
        ),
        "authoritative_state_sha256": authoritative_state_sha256,
        "revision": revision,
        "checkpoint": checkpoint.value,
        "fluorophore": fluorophore,
        "reason": reason.to_mapping(),
        "options": [item.to_mapping() for item in options],
        "evidence": [item.to_mapping() for item in evidence],
        "proposed_artifacts": [item.to_mapping() for item in proposed_artifacts],
        "prior_checkpoint_sha256": prior_checkpoint_sha256,
    }
    return ReferenceInteractionRequest(
        request_id=_request_id(content),
        run_id=run_id,
        target_image_sha256=target_image_sha256,
        fluorophores=fluorophores,
        measurement_ids=measurement_ids,
        target_control_compatibility=target_control_compatibility,
        authoritative_state_sha256=authoritative_state_sha256,
        revision=revision,
        checkpoint=checkpoint,
        fluorophore=fluorophore,
        reason=reason,
        options=options,
        evidence=evidence,
        proposed_artifacts=proposed_artifacts,
        prior_checkpoint_sha256=prior_checkpoint_sha256,
    )


def _request_id(content: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(content)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferenceResponse:
    SCHEMA_NAME = "mif_pipeline.spectral_reference_interaction_response"
    SCHEMA_VERSION = 1

    request_id: str
    request_revision: int
    action: ReferenceActionCode
    option_value: str
    selections: tuple[SelectedIdentifier, ...]
    artifacts: tuple[ArtifactPointer, ...]
    acknowledged_evidence_ids: tuple[str, ...]
    reviewer_id: str

    def __post_init__(self) -> None:
        _sha256(self.request_id, "response request ID")
        if type(self.request_revision) is not int or self.request_revision < 0:
            raise ValueError("response request revision must be nonnegative")
        if not isinstance(self.action, ReferenceActionCode):
            raise TypeError("response action is invalid")
        _text(self.option_value, "response option value")
        if type(self.selections) is not tuple or any(
            type(item) is not SelectedIdentifier for item in self.selections
        ):
            raise TypeError("response selections are invalid")
        if len(self.selections) > _MAX_SELECTIONS or len(
            {(item.role, item.identifier) for item in self.selections}
        ) != len(self.selections):
            raise ValueError("response selections must be bounded and unique")
        if (
            type(self.artifacts) is not tuple
            or len(self.artifacts) > 512
            or any(type(item) is not ArtifactPointer for item in self.artifacts)
        ):
            raise TypeError("response artifacts are invalid")
        if len({(item.role, item.artifact_id) for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("response artifact identities must be unique")
        _tuple_of_text(
            self.acknowledged_evidence_ids,
            "response acknowledged evidence IDs",
        )
        _text(self.reviewer_id, "response reviewer ID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.SCHEMA_NAME,
            "schema_version": self.SCHEMA_VERSION,
            "request_id": self.request_id,
            "request_revision": self.request_revision,
            "action": self.action.value,
            "option_value": self.option_value,
            "selections": [item.to_mapping() for item in self.selections],
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "acknowledged_evidence_ids": list(self.acknowledged_evidence_ids),
            "reviewer_id": self.reviewer_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceResponse":
        """Decode the serialized fields into a validated ReferenceResponse record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        root = _exact_mapping(
            value,
            {
                "schema_name",
                "schema_version",
                "request_id",
                "request_revision",
                "action",
                "option_value",
                "selections",
                "artifacts",
                "acknowledged_evidence_ids",
                "reviewer_id",
            },
            "reference response",
        )
        if (
            root["schema_name"] != cls.SCHEMA_NAME
            or type(root["schema_version"]) is not int
            or root["schema_version"] != cls.SCHEMA_VERSION
        ):
            raise ValueError("unsupported reference response schema")
        if not all(
            isinstance(root[field], list)
            for field in ("selections", "artifacts", "acknowledged_evidence_ids")
        ):
            raise ValueError("reference response arrays are invalid")
        try:
            action = ReferenceActionCode(root["action"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reference response action is unsupported") from exc
        return cls(
            request_id=root["request_id"],  # type: ignore[arg-type]
            request_revision=root["request_revision"],  # type: ignore[arg-type]
            action=action,
            option_value=root["option_value"],  # type: ignore[arg-type]
            selections=tuple(SelectedIdentifier.from_mapping(item) for item in root["selections"]),
            artifacts=tuple(ArtifactPointer.from_mapping(item) for item in root["artifacts"]),
            acknowledged_evidence_ids=tuple(root["acknowledged_evidence_ids"]),
            reviewer_id=root["reviewer_id"],  # type: ignore[arg-type]
        )

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.identifier for item in self.selections)


def validate_reference_response(
    request: ReferenceInteractionRequest,
    response: ReferenceResponse | dict[str, object],
) -> ReferenceResponse:
    """Validate one adapter/checkpoint response against the exact live request.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    if type(request) is not ReferenceInteractionRequest:
        raise TypeError("request must be a ReferenceInteractionRequest")
    parsed = (
        response
        if type(response) is ReferenceResponse
        else ReferenceResponse.from_mapping(response)
    )
    if parsed.request_id != request.request_id or parsed.request_revision != request.revision:
        raise ValueError("response is stale or belongs to another request")
    matching = tuple(item for item in request.options if item.value == parsed.option_value)
    if len(matching) != 1:
        raise ValueError("response option is not available for this request")
    option = matching[0]
    if parsed.action is not option.action.code:
        raise ValueError("response action does not match its request option")
    if len(parsed.selections) < option.minimum_selected_ids:
        raise ValueError("response lacks required explicit selected IDs")
    if len(parsed.artifacts) < option.minimum_artifacts:
        raise ValueError("response lacks required immutable artifact references")
    if option.artifact_requirements:
        actual_bindings = tuple(
            ArtifactRequirement(item.role, item.subject_id) for item in parsed.artifacts
        )
        if actual_bindings != option.artifact_requirements:
            raise ValueError("response artifact roles/cardinality do not match request")
    proposed_pointers = tuple(item.pointer for item in request.proposed_artifacts)
    if parsed.artifacts and any(item not in proposed_pointers for item in parsed.artifacts):
        raise ValueError(
            "response artifact was not byte/type validated and proposed by the request"
        )
    expected_evidence = tuple(item.evidence_id for item in request.evidence)
    if parsed.acknowledged_evidence_ids != expected_evidence:
        raise ValueError("response must acknowledge the exact presented evidence inventory")
    if (
        request.checkpoint is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
        and option.action.code is ReferenceActionCode.ACCEPT
    ):
        signal = tuple(item for item in parsed.selections if item.role == "signal_measurement")
        dark = tuple(item for item in parsed.selections if item.role == "dark_measurement")
        if len(signal) != 1 or not dark or len(signal) + len(dark) != len(parsed.selections):
            raise ValueError(
                "signal/dark selection requires exactly one signal and one or more dark IDs"
            )
        if signal[0].identifier in {item.identifier for item in dark}:
            raise ValueError("signal and dark measurement IDs must be distinct")
        if any(item.identifier not in request.measurement_ids for item in parsed.selections):
            raise ValueError("signal/dark measurement selection is outside the authoritative axis")
    elif (
        request.checkpoint is ReferenceCheckpointKind.EXTRACTION_QC
        and option.action.code is ReferenceActionCode.ACCEPT
        and parsed.selections
    ):
        allowed_roles = {"consensus_included_roi", "consensus_excluded_roi"}
        if any(item.role not in allowed_roles for item in parsed.selections):
            raise ValueError("extraction QC contains an unsupported consensus ROI disposition")
        identifiers = tuple(item.identifier for item in parsed.selections)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("consensus ROI dispositions must identify unique ROIs")
    elif (request.checkpoint is ReferenceCheckpointKind.BASIS_SOURCE
          and parsed.option_value == "construct_reference_library"):
        if parsed.selections:
            if request.fluorophores:
                raise ValueError("The existing component panel cannot be redeclared")
            names = parsed.selected_ids
            if (any(item.role != "reference_component" for item in parsed.selections)
                    or len(set(names)) != len(names)
                    or any(name in {"AF", "Sample AF"} for name in names)):
                raise ValueError("Declare unique fluorophore components; autofluorescence is reviewed separately")
        elif not request.fluorophores:
            raise ValueError("Enter the fluorophore components for the reference images")
    elif option.action.code is ReferenceActionCode.RESELECT:
        if request.fluorophore is None or parsed.option_value == "revise_shared_control":
            if parsed.selections:
                raise ValueError("target-level reselection carries no fluorophore selection")
        elif parsed.selections != (
            SelectedIdentifier("affected_fluorophore", request.fluorophore),
        ):
            raise ValueError("reselection must bind the exact affected fluorophore")
    elif (
        parsed.selections
        and request.checkpoint is not ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
    ):
        raise ValueError("response contains unexpected typed selections")
    return parsed


def configured_response(
    request: ReferenceInteractionRequest,
    *,
    option_value: str,
    reviewer_id: str,
    selections: tuple[SelectedIdentifier, ...] = (),
    artifacts: tuple[ArtifactPointer, ...] = (),
) -> ReferenceResponse:
    """Bind one explicit configured/UI decision to an exact live request."""
    matching = tuple(item for item in request.options if item.value == option_value)
    if len(matching) != 1:
        raise ValueError("configured response option is not available")
    response = ReferenceResponse(
        request_id=request.request_id,
        request_revision=request.revision,
        action=matching[0].action.code,
        option_value=option_value,
        selections=selections,
        artifacts=artifacts,
        acknowledged_evidence_ids=tuple(item.evidence_id for item in request.evidence),
        reviewer_id=reviewer_id,
    )
    return validate_reference_response(request, response)


__all__ = [
    "ArtifactPointer",
    "ArtifactRequirement",
    "EvidenceField",
    "ReferenceAction",
    "ReferenceActionCode",
    "ReferenceCheckpointKind",
    "ReferenceEvidence",
    "ReferenceInteractionRequest",
    "ReferenceOption",
    "PublishedArtifactEvidence",
    "ReferenceReason",
    "ReferenceReasonCode",
    "ReferenceResponse",
    "SelectedIdentifier",
    "TargetControlCompatibilityEvidence",
    "ValidatedArtifactEvidence",
    "configured_response",
    "make_reference_request",
    "validate_reference_response",
]
