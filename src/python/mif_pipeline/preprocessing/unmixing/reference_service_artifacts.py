"""Immutable local storage for exact spectral-reference scientific results.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace as dataclass_replace
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterator

from .background import BackgroundModelResolution
from .canonical_reference_input import (
    CanonicalReferenceInput,
    canonical_reference_input_from_wire,
    canonical_reference_input_to_wire,
)
from .interactions import ArtifactPointer
from .publication_lock import _AuthorityPublicationLock
from .purification import (
    ConsensusInclusionDecision,
    FluorophoreConsensusCandidate,
    MatchedControlReviewEvidence,
    NegativeFallbackReviewEvidence,
    PurifiedReferenceRoi,
    SignalDarkMeasurementReviewEvidence,
)
from .reference_review_bindings import NegativeFallbackReviewProposal
from .reference_models import AcceptedSpectrum, FluorophoreReferenceLibrary
from .reference_service_source_records import (
    BasisSourceDecisionBinding,
    ReferenceControlFilesBinding,
)
from .reference_service_roi_purification import (
    ReferenceBackgroundOperation,
    ReferenceRoiSelectionPlan,
    ReferenceSignalPurificationOperation,
    SampledReferenceRoiBundle,
)
from .reference_service_target_af import TargetAfSampleBundle, TargetAfSamplingPlan
from .reference_service_control_preparation import (
    SharedControlPreparationPlan, SampledSharedControlPreparation, ReferenceRoiSampleUse, StainedReferenceRegionChoice, ControlPreparationSetReview,
)
from .target_af import (
    ProposalReviewDecision,
    TargetControlBinding,
    TargetAutofluorescenceArtifact,
    TargetAutofluorescenceProposal,
    dumps_target_autofluorescence_artifact,
)
from .final_basis import (
    FinalRunBasisArtifact,
    FinalRunBasisProposal,
    FinalRunBasisRuntimeHandoff,
    dumps_final_run_basis_artifact,
    loads_final_run_basis_artifact,
    validate_final_run_basis_authority,
    validate_final_run_basis_proposal,
)
from .reference_store_fs import (
    ReferenceCheckpointConflictError,
    ReferenceCheckpointStoreError,
    _DirectoryIdentity,
    _PinnedDirectoryHandle,
    _install_immutable,
    _prepare_run_root,
    _read_bounded_regular,
    _replace_current_pointer,
    _validate_sha256,
)
from .reference_evidence_wire import _decode, _encode, _registry
from .typed_checksum import typed_checksum_sha256

_SCHEMA_NAME = "mif_pipeline.spectral_reference_service_artifact"
_SCHEMA_VERSION = 3
_LOCATOR_SCHEMA_NAME = "mif_pipeline.canonical_reference_locator_binding"
_LOCATOR_SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 16_777_216
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


# ==============================================================================
#                          Reference service artifacts
# ==============================================================================


class ReferenceArtifactRepositoryError(ValueError):
    """A local scientific result is missing, conflicting, or corrupt."""


from .curated_library import CuratedSourceDecision
from .reference_catalogue import (SharedReferenceCatalogue, SharedReferenceSession,
    SharedConstructionScope, SharedCycleSourceDecision, dumps_reference_source, loads_reference_source)

_EXACT_ROOT_TYPES = {
    "shared_control_preparation_plan": SharedControlPreparationPlan,
    "shared_control_preparation": SampledSharedControlPreparation,
    "control_preparation_set_review": ControlPreparationSetReview,
    "roi_sample_use": ReferenceRoiSampleUse,
    "stained_region_choice": StainedReferenceRegionChoice,
    "shared_reference_session": SharedReferenceSession,
    "shared_cycle_source": SharedCycleSourceDecision,
    "shared_construction_scope": SharedConstructionScope,
    "curated_source": CuratedSourceDecision,
    "canonical_input": CanonicalReferenceInput,
    "basis_source_decision": BasisSourceDecisionBinding,
    "reference_control_files": ReferenceControlFilesBinding,
    "roi_selection_plan": ReferenceRoiSelectionPlan,
    "sampled_roi_bundle": SampledReferenceRoiBundle,
    "background_operation": ReferenceBackgroundOperation,
    "signal_purification_operation": ReferenceSignalPurificationOperation,
    "background_resolution": BackgroundModelResolution,
    "consensus_candidate": FluorophoreConsensusCandidate,
    "accepted_spectrum": AcceptedSpectrum,
    "imported_library_candidate": FluorophoreReferenceLibrary,
    "accepted_library": FluorophoreReferenceLibrary,
    "target_control_binding": TargetControlBinding,
    "target_af_sampling_plan": TargetAfSamplingPlan,
    "target_af_sample_bundle": TargetAfSampleBundle,
    "target_af_background": BackgroundModelResolution,
    "target_af_proposal": TargetAutofluorescenceProposal,
    "target_af_review_decision": ProposalReviewDecision,
    "target_af_artifact": TargetAutofluorescenceArtifact,
    "final_basis_proposal": FinalRunBasisProposal,
    "final_basis_review_decision": ProposalReviewDecision,
    "final_basis_artifact": FinalRunBasisArtifact,
    "final_basis_runtime_handoff": FinalRunBasisRuntimeHandoff,
}
_EXACT_TUPLE_TYPES = {
    "purifications": PurifiedReferenceRoi,
    "consensus_inclusions": ConsensusInclusionDecision,
    "matched_control_reviews": MatchedControlReviewEvidence,
    "background_resolutions": BackgroundModelResolution,
    "signal_dark_reviews": SignalDarkMeasurementReviewEvidence,
    "negative_fallback_reviews": NegativeFallbackReviewEvidence,
    "negative_fallback_proposals": NegativeFallbackReviewProposal,
}
_OPERATION_BINDING_KINDS = {
    "component_preparation_revision": "component_preparation_revision_v1",
    "extraction_operation": "reference_extraction_operation_v1",
    "consensus_operation": "reference_consensus_operation_v1",
}


def _validate_kind(kind: object) -> str:
    if type(kind) is not str or _SAFE_NAME.fullmatch(kind) is None:
        raise ValueError("reference artifact kind is invalid")
    if kind not in {
        *_EXACT_ROOT_TYPES,
        *_EXACT_TUPLE_TYPES,
        *_OPERATION_BINDING_KINDS,
    }:
        raise ValueError("reference artifact kind is unsupported")
    return kind


def _validate_subject(subject_id: object) -> str:
    if type(subject_id) is not str or not subject_id or subject_id != subject_id.strip():
        raise ValueError("reference artifact subject must be literal text")
    return subject_id


def _validate_record(kind: str, record: object) -> object:
    """Validate record for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if kind == "accepted_library" and type(record) is SharedReferenceCatalogue:
        if loads_reference_source(dumps_reference_source(record)) != record:
            raise ValueError("Shared reference catalogue authority is stale")
        return record
    if kind in _EXACT_ROOT_TYPES:
        if type(record) is not _EXACT_ROOT_TYPES[kind]:
            raise TypeError(f"{kind} has the wrong exact record type")
        if kind in {"shared_reference_session", "shared_cycle_source", "shared_construction_scope"}:
            from dataclasses import fields
            rebuilt = type(record)(**{item.name: getattr(record, item.name)
                                     for item in fields(record) if item.init})
            if rebuilt != record:
                raise ValueError("Shared cycle reference authority is stale")
        elif kind == "curated_source":
            rebuilt = CuratedSourceDecision(
                record.source_checkpoint_sha256,
                record.target_input_sha256,
                record.target_image_sha256,
                record.library,
                record.reviewer_id,
            )
            if rebuilt != record:
                raise ValueError("Curated source decision is stale")
        elif kind == "basis_source_decision":
            rebuilt = BasisSourceDecisionBinding(
                source_checkpoint_sha256=record.source_checkpoint_sha256,
                request_id=record.request_id,
                choice=record.choice,
                reviewer_id=record.reviewer_id,
                reviewed_at=record.reviewed_at,
                target_input_authority_sha256=record.target_input_authority_sha256,
                target_image_sha256=record.target_image_sha256,
                imported_library_content_sha256=record.imported_library_content_sha256,
                imported_library_bytes_sha256=record.imported_library_bytes_sha256,
            )
            if rebuilt != record:
                raise ValueError("basis source decision binding authority is stale")
        elif kind == "reference_control_files":
            rebuilt = ReferenceControlFilesBinding(
                source_checkpoint_sha256=record.source_checkpoint_sha256,
                request_id=record.request_id,
                reviewer_id=record.reviewer_id,
                target_input_authority_sha256=record.target_input_authority_sha256,
                control_strategy=record.control_strategy,
                positive_reference_input_ids=record.positive_reference_input_ids,
                shared_unstained_input_id=record.shared_unstained_input_id,
                negative_control_input_ids=record.negative_control_input_ids,
                prepare_shared_control=record.prepare_shared_control,
            )
            if rebuilt != record:
                raise ValueError("reference/control file binding authority is stale")
        elif kind == "roi_selection_plan":
            rebuilt = ReferenceRoiSelectionPlan(
                record.source_checkpoint_sha256,
                record.checkpoint,
                record.fluorophore,
                record.export,
                record.selections,
                record.pairings,
                record.sampling_policy,
                record.reviewer_id,
                record.source_preparation_sha256,
            )
            if rebuilt != record:
                raise ValueError("ROI selection plan authority is stale")
        elif kind == "sampled_roi_bundle":
            if SampledReferenceRoiBundle(record.plan, record.samples) != record:
                raise ValueError("sampled ROI bundle authority is stale")
        elif kind == "background_operation":
            rebuilt = ReferenceBackgroundOperation(
                record.source_checkpoint_sha256,
                record.reviewer_id,
                record.positive_bundle_sha256,
                record.control_bundle_sha256,
                record.background_bundle_sha256,
                record.policy,
                record.negative_fallback_policy,
                record.resolution_sha256s,
            )
            if rebuilt != record:
                raise ValueError("background operation authority is stale")
        elif kind == "signal_purification_operation":
            rebuilt = ReferenceSignalPurificationOperation(
                record.source_checkpoint_sha256,
                record.reviewer_id,
                record.selections,
                record.background_operation_sha256,
                record.purification_policy,
                record.purification_evidence_sha256s,
                record.af_control_set_review_sha256s,
            )
            if rebuilt != record:
                raise ValueError("signal purification operation authority is stale")
        elif kind == "target_af_sampling_plan":
            rebuilt = TargetAfSamplingPlan(
                record.source_checkpoint_sha256,
                record.selection_kind,
                record.control_input_id,
                record.accepted_library_sha256,
                record.export,
                record.af_selections,
                record.background_selections,
                record.roi_reviews,
                record.sampling_policy,
                record.background_policy,
                record.estimation_policy,
                record.qc_policy,
                record.artifact_id,
                record.created_at,
                record.reviewer_id,
                record.software_versions,
                record.policy_versions,
                record.shared_authorities,
                record.additional_control_input_ids,
                record.retained_authorities,
                record.source_reviews,
            )
            if rebuilt != record:
                raise ValueError("target-AF sampling plan authority is stale")
        elif kind == "target_af_sample_bundle":
            if TargetAfSampleBundle(record.plan, record.samples) != record:
                raise ValueError("target-AF sample bundle authority is stale")
        elif kind == "final_basis_proposal":
            validate_final_run_basis_proposal(record)
        elif kind == "final_basis_artifact":
            validate_final_run_basis_authority(record)
        elif kind == "final_basis_runtime_handoff":
            from dataclasses import replace

            if replace(record) != record:
                raise ValueError("final-basis runtime handoff is stale")
        return record
    if kind in _OPERATION_BINDING_KINDS:
        if (
            type(record) is not tuple
            or len(record) != 2
            or record[0] != _OPERATION_BINDING_KINDS[kind]
            or type(record[1]) is not str
        ):
            raise TypeError(f"{kind} must contain its exact operation binding")
        try:
            _validate_sha256(record[1], f"{kind} binding SHA-256")
        except ReferenceCheckpointStoreError as exc:
            raise ValueError(str(exc)) from exc
        return record
    if type(record) is not tuple or not record:
        raise TypeError(f"{kind} must be a nonempty tuple")
    expected = _EXACT_TUPLE_TYPES[kind]
    if any(type(item) is not expected for item in record):
        raise TypeError(f"{kind} must contain exact {expected.__name__} records")
    return record


def _semantic_sha256(kind: str, record: object) -> str:
    if kind in {"curated_source", "shared_reference_session", "shared_cycle_source", "shared_construction_scope"}:
        return record.content_sha256
    if kind in _OPERATION_BINDING_KINDS:
        return record[1]  # type: ignore[index]
    if kind in {"background_resolution", "target_af_background"}:
        return record.resolution_sha256  # type: ignore[union-attr]
    if kind == "canonical_input":
        return record.authority_sha256  # type: ignore[union-attr]
    if kind in {
        "shared_control_preparation_plan", "shared_control_preparation", "control_preparation_set_review", "roi_sample_use", "stained_region_choice",
        "basis_source_decision",
        "reference_control_files",
        "roi_selection_plan",
        "sampled_roi_bundle",
        "background_operation",
        "signal_purification_operation",
    }:
        return record.content_sha256  # type: ignore[union-attr]
    if kind == "consensus_candidate":
        return record.candidate_sha256  # type: ignore[union-attr]
    if kind == "accepted_spectrum":
        return record.spectrum_sha256  # type: ignore[union-attr]
    if kind in {"imported_library_candidate", "accepted_library"}:
        return record.content_sha256  # type: ignore[union-attr]
    if kind == "target_control_binding":
        return record.evidence_sha256  # type: ignore[union-attr]
    if kind in {"target_af_sampling_plan", "target_af_sample_bundle", "target_af_artifact"}:
        return record.content_sha256  # type: ignore[union-attr]
    if kind == "target_af_proposal":
        return record.proposal_sha256  # type: ignore[union-attr]
    if kind == "target_af_review_decision":
        return record.decision_sha256  # type: ignore[union-attr]
    if kind == "final_basis_proposal":
        return record.proposal_sha256  # type: ignore[union-attr]
    if kind == "final_basis_review_decision":
        return record.decision_sha256  # type: ignore[union-attr]
    if kind == "final_basis_artifact":
        return record.content_sha256  # type: ignore[union-attr]
    if kind == "final_basis_runtime_handoff":
        return record.handoff_sha256  # type: ignore[union-attr]
    if kind == "purifications":
        return typed_checksum_sha256(
            {
                "semantic": "consensus_inclusion_review_inventory_v1",
                "fluorophore": (record[0].positive.input_evidence.fluorophore_identity),
                "purification_evidence_sha256s": [item.evidence_sha256 for item in record],
                "roi_ids": [item.positive.selection.roi.roi_id for item in record],
            }
        )
    semantic = {
        "matched_control_reviews": "ordered_matched_control_reviews_v1",
        "background_resolutions": "ordered_background_resolutions_v1",
        "signal_dark_reviews": "ordered_signal_dark_reviews_v1",
        "negative_fallback_reviews": "ordered_negative_fallback_reviews_v1",
        "negative_fallback_proposals": "ordered_negative_fallback_proposals_v1",
    }.get(kind)
    if semantic is not None:
        digest_field = {
            "matched_control_reviews": "evidence_sha256",
            "background_resolutions": "resolution_sha256",
            "signal_dark_reviews": "evidence_sha256",
            "negative_fallback_reviews": "evidence_sha256",
            "negative_fallback_proposals": "proposal_sha256",
        }[kind]
        return typed_checksum_sha256(
            {
                "semantic": semantic,
                "evidence_sha256s": [getattr(item, digest_field) for item in record],
            }
        )
    return typed_checksum_sha256(
        {
            "semantic": "ordered_consensus_inclusions_v1",
            "review_binding_sha256s": [item.review_binding_sha256 for item in record],
        }
    )


@dataclass(frozen=True, slots=True)
class _StoredReferenceArtifact:
    kind: str
    subject_id: str
    authority_checkpoint_sha256: str
    semantic_sha256: str
    content_sha256: str
    portable_bytes_sha256: str | None
    record: object


def _artifact_bytes(
    *,
    run_sha256: str,
    start_authority_sha256: str,
    authority_checkpoint_sha256: str,
    kind: str,
    subject_id: str,
    record: object,
) -> tuple[bytes, str, str]:
    kind = _validate_kind(kind)
    subject_id = _validate_subject(subject_id)
    _validate_sha256(run_sha256, "reference artifact run SHA-256")
    _validate_sha256(start_authority_sha256, "reference artifact start authority")
    _validate_sha256(authority_checkpoint_sha256, "reference artifact checkpoint")
    record = _validate_record(kind, record)
    semantic_sha256 = _semantic_sha256(kind, record)
    portable_bytes_sha256 = (
        hashlib.sha256(
            (
                dumps_target_autofluorescence_artifact(record)
                if kind == "target_af_artifact"
                else (
                    dumps_final_run_basis_artifact(record)
                    if kind == "final_basis_artifact"
                    else dumps_reference_source(record)
                )
            ).encode("utf-8")
        ).hexdigest()
        if kind
        in {
            "imported_library_candidate",
            "accepted_library",
            "target_af_artifact",
            "final_basis_artifact",
        }
        else None
    )
    payload = {
        "schema_name": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "run_sha256": run_sha256,
        "start_authority_sha256": start_authority_sha256,
        "authority_checkpoint_sha256": authority_checkpoint_sha256,
        "kind": kind,
        "subject_id": subject_id,
        "semantic_sha256": semantic_sha256,
        "portable_bytes_sha256": portable_bytes_sha256,
        "record": (
            canonical_reference_input_to_wire(record)
            if kind == "canonical_input"
            else _encode(record)
        ),
    }
    content_sha256 = typed_checksum_sha256(payload)
    raw = (
        json.dumps(
            {**payload, "content_sha256": content_sha256},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return raw, semantic_sha256, content_sha256


def _strict_object(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ReferenceArtifactRepositoryError(
                    f"duplicate reference artifact field {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ReferenceArtifactRepositoryError(f"nonfinite reference artifact value {item!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReferenceArtifactRepositoryError("reference artifact is not strict JSON") from exc
    if type(value) is not dict:
        raise ReferenceArtifactRepositoryError("reference artifact must be an object")
    return value


def _locator_pointer_name(subject_id: str) -> str:
    subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
    return f"canonical_locator-{subject_sha}.json"


def _locator_pointer_bytes(
    *,
    run_sha256: str,
    start_authority_sha256: str,
    subject_id: str,
    record: CanonicalReferenceInput,
) -> bytes:
    payload = {
        "schema_name": _LOCATOR_SCHEMA_NAME,
        "schema_version": _LOCATOR_SCHEMA_VERSION,
        "run_sha256": run_sha256,
        "start_authority_sha256": start_authority_sha256,
        "subject_id": subject_id,
        "scientific_identity_sha256": record.scientific_identity_sha256,
        "locator_binding_sha256": record.locator_binding.binding_sha256,
        "record": canonical_reference_input_to_wire(record),
    }
    content_sha256 = typed_checksum_sha256(payload)
    return (
        json.dumps(
            {**payload, "content_sha256": content_sha256},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _parse_locator_pointer(
    raw: bytes,
    *,
    run_sha256: str,
    start_authority_sha256: str,
    subject_id: str,
    scientific_record: CanonicalReferenceInput,
) -> CanonicalReferenceInput:
    root = _strict_object(raw)
    expected = {
        "schema_name",
        "schema_version",
        "run_sha256",
        "start_authority_sha256",
        "subject_id",
        "scientific_identity_sha256",
        "locator_binding_sha256",
        "record",
        "content_sha256",
    }
    if set(root) != expected:
        raise ReferenceArtifactRepositoryError("canonical locator pointer fields differ")
    if (root["schema_name"], root["schema_version"]) != (
        _LOCATOR_SCHEMA_NAME,
        _LOCATOR_SCHEMA_VERSION,
    ):
        raise ReferenceArtifactRepositoryError("canonical locator pointer schema is unsupported")
    if (
        root["run_sha256"] != run_sha256
        or root["start_authority_sha256"] != start_authority_sha256
        or root["subject_id"] != subject_id
        or root["scientific_identity_sha256"] != scientific_record.scientific_identity_sha256
    ):
        raise ReferenceArtifactRepositoryError("canonical locator pointer authority differs")
    payload = {key: value for key, value in root.items() if key != "content_sha256"}
    if root["content_sha256"] != typed_checksum_sha256(payload):
        raise ReferenceArtifactRepositoryError("canonical locator pointer checksum differs")
    # The usual locator points to the record just authenticated above. Reuse
    # that parsed record when every wire field agrees, including its locator.
    # A relocated source still follows the full decode and identity comparison.
    if json.dumps(root["record"], sort_keys=True) == json.dumps(
        canonical_reference_input_to_wire(scientific_record), sort_keys=True
    ):
        record = scientific_record
    else:
        try:
            record = canonical_reference_input_from_wire(root["record"])
        except (TypeError, ValueError) as exc:
            raise ReferenceArtifactRepositoryError(
                "canonical locator pointer record is invalid"
            ) from exc
    if (
        record.input_id != scientific_record.input_id
        or record.scientific_identity_sha256 != scientific_record.scientific_identity_sha256
        or root["locator_binding_sha256"] != record.locator_binding.binding_sha256
    ):
        raise ReferenceArtifactRepositoryError(
            "canonical locator pointer differs from scientific authority"
        )
    return record


def _parse_artifact(
    raw: bytes,
    *,
    run_sha256: str,
    start_authority_sha256: str,
    authority_checkpoint_sha256: str,
    kind: str,
    subject_id: str,
) -> _StoredReferenceArtifact:
    root = _strict_object(raw)
    expected = {
        "schema_name",
        "schema_version",
        "run_sha256",
        "start_authority_sha256",
        "authority_checkpoint_sha256",
        "kind",
        "subject_id",
        "semantic_sha256",
        "portable_bytes_sha256",
        "record",
        "content_sha256",
    }
    if set(root) != expected:
        raise ReferenceArtifactRepositoryError("reference artifact fields differ")
    if (
        root["schema_name"] == _SCHEMA_NAME
        and type(root["schema_version"]) is int
        and root["schema_version"] in (1, 2)
    ):
        raise ReferenceArtifactRepositoryError(
            "spectral reference service artifact versions 1 and 2 require source-backed migration"
        )
    if (root["schema_name"], root["schema_version"]) != (
        _SCHEMA_NAME,
        _SCHEMA_VERSION,
    ):
        raise ReferenceArtifactRepositoryError("reference artifact schema is unsupported")
    expected_binding = (
        run_sha256,
        start_authority_sha256,
        authority_checkpoint_sha256,
        kind,
        subject_id,
    )
    if (
        tuple(
            root[name]
            for name in (
                "run_sha256",
                "start_authority_sha256",
                "authority_checkpoint_sha256",
                "kind",
                "subject_id",
            )
        )
        != expected_binding
    ):
        raise ReferenceArtifactRepositoryError("reference artifact authority differs")
    declared = root["content_sha256"]
    if type(declared) is not str:
        raise ReferenceArtifactRepositoryError("reference artifact checksum is invalid")
    payload = {key: value for key, value in root.items() if key != "content_sha256"}
    if typed_checksum_sha256(payload) != declared:
        raise ReferenceArtifactRepositoryError("reference artifact checksum differs")
    if (
        kind == "canonical_input"
        and type(root["record"]) is dict
        and root["record"].get("version") == 1
    ):
        raise ReferenceArtifactRepositoryError(
            "legacy canonical input artifact requires explicit outer-authority migration"
        )
    try:
        record = (
            canonical_reference_input_from_wire(root["record"])
            if kind == "canonical_input"
            else _decode(root["record"], _registry())
        )
        _validate_record(kind, record)
    except (TypeError, ValueError) as exc:
        raise ReferenceArtifactRepositoryError(
            "reference artifact typed payload is invalid"
        ) from exc
    semantic = _semantic_sha256(kind, record)
    if root["semantic_sha256"] != semantic:
        raise ReferenceArtifactRepositoryError("reference artifact semantic checksum differs")
    rebuilt, rebuilt_semantic, rebuilt_content = _artifact_bytes(
        run_sha256=run_sha256,
        start_authority_sha256=start_authority_sha256,
        authority_checkpoint_sha256=authority_checkpoint_sha256,
        kind=kind,
        subject_id=subject_id,
        record=record,
    )
    if rebuilt != raw:
        raise ReferenceArtifactRepositoryError("reference artifact bytes are noncanonical")
    return _StoredReferenceArtifact(
        kind,
        subject_id,
        authority_checkpoint_sha256,
        rebuilt_semantic,
        rebuilt_content,
        root["portable_bytes_sha256"],  # type: ignore[arg-type]
        record,
    )


class _ReferenceArtifactRepository:
    """Service-owned immutable result store; never accepts caller paths."""

    def __init__(
        self,
        checkpoint_output_root: Path,
        checkpoint_output_identity: _DirectoryIdentity,
    ) -> None:
        if not isinstance(checkpoint_output_root, Path):
            raise TypeError("checkpoint output root must be a Path")
        if type(checkpoint_output_identity) is not _DirectoryIdentity:
            raise TypeError("checkpoint output identity must be exact")
        if checkpoint_output_identity.requested_path != checkpoint_output_root:
            raise ValueError("checkpoint output identity does not bind its root")
        self.output_root = checkpoint_output_root
        self._output_identity = checkpoint_output_identity
        self.root = checkpoint_output_root / ".reference-artifacts"
        self._root_identity: _DirectoryIdentity | None = None

    @staticmethod
    def _run_sha256(run_id: str) -> str:
        if type(run_id) is not str or not run_id or run_id != run_id.strip():
            raise ValueError("workflow run ID must be nonempty literal text")
        return hashlib.sha256(run_id.encode("utf-8")).hexdigest()

    @contextmanager
    def _locked_run(
        self, run_id: str, *, create: bool
    ) -> Iterator[tuple[Path, _PinnedDirectoryHandle]]:
        run_sha256 = self._run_sha256(run_id)
        self._output_identity.revalidate("checkpoint output root", exact_link_count=False)
        output_expected = _DirectoryIdentity.capture(self.output_root, "checkpoint output root")
        with (
            _PinnedDirectoryHandle(
                self.output_root,
                "checkpoint output root",
                expected=output_expected,
            ) as output_handle,
            _AuthorityPublicationLock(
                self.output_root / ".reference-artifact-namespace"
            ) as namespace_lock,
        ):

            def output_guard() -> None:
                self._output_identity.revalidate("checkpoint output root", exact_link_count=False)
                output_handle.validate()
                namespace_lock.validate_identity()
                if namespace_lock.authority_path.parent != self._output_identity.canonical_path:
                    raise ReferenceCheckpointStoreError(
                        "reference artifact namespace lock escaped checkpoint root"
                    )

            output_guard()
            if not self.root.exists() and not self.root.is_symlink():
                if not create:
                    raise ReferenceArtifactRepositoryError(
                        "reference artifact repository does not exist"
                    )
                try:
                    output_handle.mkdir(self.root.name)
                except FileExistsError:
                    pass
                output_handle.refresh_after_owned_child_directory_change()
            root_identity = _DirectoryIdentity.capture(self.root, "reference artifact repository")
            if output_handle._descriptor is not None:
                relative = os.stat(
                    self.root.name,
                    dir_fd=output_handle._descriptor,
                    follow_symlinks=False,
                )
                if (int(relative.st_dev), int(relative.st_ino), int(relative.st_mode)) != (
                    root_identity.device,
                    root_identity.inode,
                    root_identity.mode,
                ):
                    raise ReferenceArtifactRepositoryError(
                        "reference artifact repository changed before child pinning"
                    )
            if self._root_identity is None:
                self._root_identity = root_identity
            elif self._root_identity != root_identity:
                raise ReferenceArtifactRepositoryError(
                    "reference artifact repository identity changed"
                )
            with _PinnedDirectoryHandle(
                self.root,
                "reference artifact repository",
                expected=root_identity,
            ) as root_handle:
                output_guard()

                def root_guard() -> None:
                    output_guard()
                    root_identity.revalidate("reference artifact repository")
                    root_handle.validate()

                run_root = _prepare_run_root(
                    self.root,
                    run_sha256,
                    create=create,
                    output_handle=root_handle,
                    before_mutation=root_guard,
                )
                if create:
                    root_identity = root_handle.refresh_after_owned_child_directory_change()
                    self._root_identity = root_identity
                run_identity = _DirectoryIdentity.capture(
                    run_root, "reference artifact run directory"
                )
                if root_handle._descriptor is not None:
                    relative = os.stat(
                        run_sha256,
                        dir_fd=root_handle._descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        int(relative.st_dev),
                        int(relative.st_ino),
                        int(relative.st_mode),
                    ) != (
                        run_identity.device,
                        run_identity.inode,
                        run_identity.mode,
                    ):
                        raise ReferenceArtifactRepositoryError(
                            "reference artifact run changed before child pinning"
                        )
                root_guard()
                with _PinnedDirectoryHandle(
                    run_root,
                    "reference artifact run directory",
                    expected=run_identity,
                ) as run_handle:
                    root_guard()
                    run_handle.validate()
                    yield run_root, run_handle
                    root_guard()
                    run_handle.validate()

    @staticmethod
    def _name(
        kind: str,
        subject_id: str,
        authority_checkpoint_sha256: str,
        semantic_sha256: str,
    ) -> str:
        subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
        return f"{kind}-{subject_sha}-{authority_checkpoint_sha256}-" f"{semantic_sha256}.json"

    @staticmethod
    def _replace_locator_pointer_locked(
        *,
        run_root: Path,
        run_handle: _PinnedDirectoryHandle,
        lock: _AuthorityPublicationLock,
        guard,
        run_sha256: str,
        start_authority_sha256: str,
        subject_id: str,
        record: CanonicalReferenceInput,
        expected_binding_sha256: str | None = None,
    ) -> None:
        raw = _locator_pointer_bytes(
            run_sha256=run_sha256,
            start_authority_sha256=start_authority_sha256,
            subject_id=subject_id,
            record=record,
        )
        path = run_root / _locator_pointer_name(subject_id)
        if expected_binding_sha256 is not None:
            _validate_sha256(
                expected_binding_sha256,
                "expected canonical locator binding",
            )
            existing = _read_bounded_regular(
                path,
                limit=_MAX_ARTIFACT_BYTES,
                label="canonical locator binding pointer",
            )
            prior = _parse_locator_pointer(
                existing,
                run_sha256=run_sha256,
                start_authority_sha256=start_authority_sha256,
                subject_id=subject_id,
                scientific_record=record,
            )
            if prior.locator_binding.binding_sha256 != expected_binding_sha256:
                raise ReferenceArtifactRepositoryError(
                    "canonical locator binding CAS expectation is stale"
                )
        _replace_current_pointer(path, raw, lock, guard, run_handle)
        stored = _read_bounded_regular(
            path,
            limit=_MAX_ARTIFACT_BYTES,
            label="canonical locator binding pointer",
        )
        parsed = _parse_locator_pointer(
            stored,
            run_sha256=run_sha256,
            start_authority_sha256=start_authority_sha256,
            subject_id=subject_id,
            scientific_record=record,
        )
        if parsed.locator_binding != record.locator_binding:
            raise ReferenceArtifactRepositoryError(
                "canonical locator binding differs after replacement"
            )

    @staticmethod
    def _portable_name(
        kind: str,
        subject_id: str,
        authority_checkpoint_sha256: str,
        semantic_sha256: str,
    ) -> str:
        subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
        return (
            f"{kind}-{subject_sha}-{authority_checkpoint_sha256}-"
            f"{semantic_sha256}.reference.json"
        )

    @staticmethod
    def _validate_portable(
        run_root: Path,
        stored: _StoredReferenceArtifact,
    ) -> None:
        """Validate portable for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        if stored.kind not in {
            "imported_library_candidate",
            "accepted_library",
            "target_af_artifact",
            "final_basis_artifact",
        }:
            if stored.portable_bytes_sha256 is not None:
                raise ReferenceArtifactRepositoryError("nonportable artifact claims portable bytes")
            return
        name = _ReferenceArtifactRepository._portable_name(
            stored.kind,
            stored.subject_id,
            stored.authority_checkpoint_sha256,
            stored.semantic_sha256,
        )
        try:
            raw = _read_bounded_regular(
                run_root / name,
                limit=_MAX_ARTIFACT_BYTES,
                label="portable reference library",
            )
            loaded = (
                loads_final_run_basis_artifact(raw)
                if stored.kind == "final_basis_artifact"
                else (
                    __import__(
                        "mif_pipeline.preprocessing.unmixing.target_af",
                        fromlist=["loads_target_autofluorescence_artifact"],
                    ).loads_target_autofluorescence_artifact(raw)
                    if stored.kind == "target_af_artifact"
                    else loads_reference_source(raw)
                )
            )
        except (ReferenceCheckpointStoreError, TypeError, ValueError) as exc:
            raise ReferenceArtifactRepositoryError("portable reference library is invalid") from exc
        if (
            hashlib.sha256(raw).hexdigest() != stored.portable_bytes_sha256
            or loaded != stored.record
        ):
            raise ReferenceArtifactRepositoryError(
                "portable reference library differs from repository manifest"
            )

    def _validate_portable_locked(
        self,
        run_id: str,
        stored: _StoredReferenceArtifact,
    ) -> None:
        if stored.kind not in {
            "imported_library_candidate",
            "accepted_library",
            "target_af_artifact",
            "final_basis_artifact",
        }:
            self._validate_portable(self.root, stored)
            return
        with self._locked_run(run_id, create=False) as locked:
            run_root, run_handle = locked
            run_handle.validate()
            self._validate_portable(run_root, stored)

    def _put(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        kind: str,
        subject_id: str,
        record: object,
        unique_authority: bool,
        expected_locator_binding_sha256: str | None = None,
    ) -> _StoredReferenceArtifact:
        run_sha256 = self._run_sha256(run_id)
        raw, semantic, _ = _artifact_bytes(
            run_sha256=run_sha256,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind=kind,
            subject_id=subject_id,
            record=record,
        )
        name = self._name(kind, subject_id, authority_checkpoint_sha256, semantic)
        try:
            with self._locked_run(run_id, create=True) as locked:
                run_root, run_handle = locked
                run_identity = run_handle.identity
                with _AuthorityPublicationLock(run_root / ".artifacts") as lock:

                    def guard() -> None:
                        run_identity.revalidate("reference artifact run directory")
                        run_handle.validate()
                        lock.validate_identity()

                    if kind == "canonical_input":
                        canonical_names = tuple(
                            item.name
                            for item in os.scandir(
                                run_handle._descriptor
                                if run_handle._descriptor is not None
                                else run_root
                            )
                            if item.name.startswith("canonical_input-")
                            and item.name.endswith(".json")
                        )
                        parsed_inventory: list[_StoredReferenceArtifact] = []
                        matching: list[_StoredReferenceArtifact] = []
                        for canonical_name in canonical_names:
                            canonical_raw = _read_bounded_regular(
                                run_root / canonical_name,
                                limit=_MAX_ARTIFACT_BYTES,
                                label="canonical reference input",
                            )
                            canonical_root = _strict_object(canonical_raw)
                            canonical_subject = _validate_subject(canonical_root.get("subject_id"))
                            canonical_authority = _validate_sha256(
                                canonical_root.get("authority_checkpoint_sha256"),
                                "canonical input checkpoint",
                            )
                            parsed_canonical = _parse_artifact(
                                canonical_raw,
                                run_sha256=run_sha256,
                                start_authority_sha256=start_authority_sha256,
                                authority_checkpoint_sha256=canonical_authority,
                                kind="canonical_input",
                                subject_id=canonical_subject,
                            )
                            parsed_inventory.append(parsed_canonical)
                            if parsed_canonical.record.input_id == record.input_id:
                                matching.append(parsed_canonical)
                        if matching:
                            if (
                                len(matching) == 1
                                and matching[0].record == record
                                and matching[0].subject_id == subject_id
                            ):
                                if expected_locator_binding_sha256 is not None:
                                    self._replace_locator_pointer_locked(
                                        run_root=run_root,
                                        run_handle=run_handle,
                                        lock=lock,
                                        guard=guard,
                                        run_sha256=run_sha256,
                                        start_authority_sha256=start_authority_sha256,
                                        subject_id=subject_id,
                                        record=record,
                                        expected_binding_sha256=(expected_locator_binding_sha256),
                                    )
                                    current_record = record
                                else:
                                    current_raw = _read_bounded_regular(
                                        run_root / _locator_pointer_name(subject_id),
                                        limit=_MAX_ARTIFACT_BYTES,
                                        label="canonical locator binding pointer",
                                    )
                                    current_record = _parse_locator_pointer(
                                        current_raw,
                                        run_sha256=run_sha256,
                                        start_authority_sha256=start_authority_sha256,
                                        subject_id=subject_id,
                                        scientific_record=matching[0].record,
                                    )
                                return dataclass_replace(matching[0], record=current_record)
                            raise ReferenceArtifactRepositoryError(
                                "canonical input ID already has a conflicting claim"
                            )
                        if expected_locator_binding_sha256 is not None:
                            raise ReferenceArtifactRepositoryError(
                                "canonical locator binding CAS target is missing"
                            )
                        target_count = sum(
                            item.record.input_role == "target_image" for item in parsed_inventory
                        )
                        if record.input_role != "target_image" and target_count != 1:
                            raise ReferenceArtifactRepositoryError(
                                "canonical non-target input requires exactly one persisted target"
                            )

                    if unique_authority:
                        subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
                        prefix = (
                            f"{kind}-{subject_sha}-"
                            if kind == "canonical_input"
                            else f"{kind}-{subject_sha}-{authority_checkpoint_sha256}-"
                        )
                        existing = tuple(
                            item.name
                            for item in os.scandir(
                                run_handle._descriptor
                                if run_handle._descriptor is not None
                                else run_root
                            )
                            if item.name.startswith(prefix)
                            and item.name.endswith(".json")
                            and not item.name.endswith(".reference.json")
                        )
                        if kind == "canonical_input" and existing and existing != (name,):
                            if len(existing) != 1:
                                raise ReferenceArtifactRepositoryError(
                                    "canonical input ID has multiple persisted authorities"
                                )
                            existing_raw = _read_bounded_regular(
                                run_root / existing[0],
                                limit=_MAX_ARTIFACT_BYTES,
                                label="canonical reference input",
                            )
                            existing_root = _strict_object(existing_raw)
                            existing_authority = _validate_sha256(
                                existing_root.get("authority_checkpoint_sha256"),
                                "canonical input checkpoint",
                            )
                            parsed_existing = _parse_artifact(
                                existing_raw,
                                run_sha256=run_sha256,
                                start_authority_sha256=start_authority_sha256,
                                authority_checkpoint_sha256=existing_authority,
                                kind=kind,
                                subject_id=subject_id,
                            )
                            if parsed_existing.record != record:
                                raise ReferenceArtifactRepositoryError(
                                    "canonical input ID already binds conflicting authority"
                                )
                            return parsed_existing
                        if existing and existing != (name,):
                            raise ReferenceArtifactRepositoryError(
                                "reference service operation inputs conflict"
                            )

                    if kind in {
                        "imported_library_candidate",
                        "accepted_library",
                        "target_af_artifact",
                        "final_basis_artifact",
                    }:
                        portable_raw = (
                            dumps_target_autofluorescence_artifact(record)
                            if kind == "target_af_artifact"
                            else (
                                dumps_final_run_basis_artifact(record)
                                if kind == "final_basis_artifact"
                                else dumps_reference_source(record)
                            )
                        ).encode("utf-8")
                        _install_immutable(
                            run_root
                            / self._portable_name(
                                kind,
                                subject_id,
                                authority_checkpoint_sha256,
                                semantic,
                            ),
                            portable_raw,
                            guard,
                            directory=run_handle,
                            read_limit=_MAX_ARTIFACT_BYTES,
                            label="portable reference library",
                        )

                    _install_immutable(
                        run_root / name,
                        raw,
                        guard,
                        directory=run_handle,
                        read_limit=_MAX_ARTIFACT_BYTES,
                        label="reference scientific artifact",
                    )
                    if kind == "canonical_input":
                        self._replace_locator_pointer_locked(
                            run_root=run_root,
                            run_handle=run_handle,
                            lock=lock,
                            guard=guard,
                            run_sha256=run_sha256,
                            start_authority_sha256=start_authority_sha256,
                            subject_id=subject_id,
                            record=record,
                            expected_binding_sha256=None,
                        )
                loaded = _read_bounded_regular(
                    run_root / name,
                    limit=_MAX_ARTIFACT_BYTES,
                    label="reference scientific artifact",
                )
        except (ReferenceCheckpointStoreError, ReferenceCheckpointConflictError) as exc:
            raise ReferenceArtifactRepositoryError(str(exc)) from exc
        parsed = _parse_artifact(
            loaded,
            run_sha256=run_sha256,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind=kind,
            subject_id=subject_id,
        )
        self._validate_portable_locked(run_id, parsed)
        return parsed

    def put(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        kind: str,
        subject_id: str,
        record: object,
    ) -> _StoredReferenceArtifact:
        return self._put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind=kind,
            subject_id=subject_id,
            record=record,
            unique_authority=False,
            expected_locator_binding_sha256=None,
        )

    def put_unique(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        kind: str,
        subject_id: str,
        record: object,
    ) -> _StoredReferenceArtifact:
        """Persist one exact service-operation binding per source authority."""

        return self._put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind=kind,
            subject_id=subject_id,
            record=record,
            unique_authority=True,
            expected_locator_binding_sha256=None,
        )

    def replace_canonical_input_locator(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        subject_id: str,
        previous_record: CanonicalReferenceInput,
        record: CanonicalReferenceInput,
    ) -> _StoredReferenceArtifact:
        """CAS-replace only the locator pointer for unchanged science."""

        if (
            type(previous_record) is not CanonicalReferenceInput
            or type(record) is not CanonicalReferenceInput
            or previous_record.scientific_identity_sha256 != record.scientific_identity_sha256
        ):
            raise ReferenceArtifactRepositoryError(
                "canonical locator replacement requires unchanged scientific identity"
            )
        return self._put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind="canonical_input",
            subject_id=subject_id,
            record=record,
            unique_authority=True,
            expected_locator_binding_sha256=(previous_record.locator_binding.binding_sha256),
        )

    def load(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        authority_checkpoint_sha256: str,
        kind: str,
        subject_id: str,
    ) -> _StoredReferenceArtifact:
        run_sha256 = self._run_sha256(run_id)
        kind = _validate_kind(kind)
        subject_id = _validate_subject(subject_id)
        subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
        prefix = f"{kind}-{subject_sha}-{authority_checkpoint_sha256}-"
        try:
            with self._locked_run(run_id, create=False) as locked:
                run_root, run_handle = locked
                run_handle.validate()
                names = tuple(
                    sorted(
                        item.name
                        for item in os.scandir(
                            run_handle._descriptor
                            if run_handle._descriptor is not None
                            else run_root
                        )
                        if item.name.startswith(prefix)
                        and item.name.endswith(".json")
                        and not item.name.endswith(".reference.json")
                    )
                )
                if len(names) != 1:
                    raise ReferenceArtifactRepositoryError(
                        "reference scientific artifact is missing or ambiguous"
                    )
                path = run_root / names[0]
                raw = _read_bounded_regular(
                    path,
                    limit=_MAX_ARTIFACT_BYTES,
                    label="reference scientific artifact",
                )
                locator_raw = (
                    _read_bounded_regular(
                        run_root / _locator_pointer_name(subject_id),
                        limit=_MAX_ARTIFACT_BYTES,
                        label="canonical locator binding pointer",
                    )
                    if kind == "canonical_input"
                    else None
                )
        except ReferenceArtifactRepositoryError:
            raise
        except (OSError, ReferenceCheckpointStoreError) as exc:
            raise ReferenceArtifactRepositoryError(str(exc)) from exc
        parsed = _parse_artifact(
            raw,
            run_sha256=run_sha256,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=authority_checkpoint_sha256,
            kind=kind,
            subject_id=subject_id,
        )
        expected_name = self._name(
            kind,
            subject_id,
            authority_checkpoint_sha256,
            parsed.semantic_sha256,
        )
        if names[0] != expected_name:
            raise ReferenceArtifactRepositoryError(
                "reference scientific artifact name differs from content"
            )
        if locator_raw is not None:
            parsed = dataclass_replace(
                parsed,
                record=_parse_locator_pointer(
                    locator_raw,
                    run_sha256=run_sha256,
                    start_authority_sha256=start_authority_sha256,
                    subject_id=subject_id,
                    scientific_record=parsed.record,
                ),
            )
        self._validate_portable_locked(run_id, parsed)
        return parsed

    def load_all(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        kind: str,
        subject_id: str,
    ) -> tuple[_StoredReferenceArtifact, ...]:
        """Strictly reopen every immutable generation for one semantic subject."""

        run_sha256 = self._run_sha256(run_id)
        kind = _validate_kind(kind)
        subject_id = _validate_subject(subject_id)
        subject_sha = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
        prefix = f"{kind}-{subject_sha}-"
        try:
            with self._locked_run(run_id, create=False) as locked:
                run_root, run_handle = locked
                run_handle.validate()
                names = tuple(
                    sorted(
                        item.name
                        for item in os.scandir(
                            run_handle._descriptor
                            if run_handle._descriptor is not None
                            else run_root
                        )
                        if item.name.startswith(prefix)
                        and item.name.endswith(".json")
                        and not item.name.endswith(".reference.json")
                    )
                )
                if not names:
                    raise ReferenceArtifactRepositoryError(
                        "reference scientific artifact is missing"
                    )
                raw_values = tuple(
                    (
                        name,
                        _read_bounded_regular(
                            run_root / name,
                            limit=_MAX_ARTIFACT_BYTES,
                            label="reference scientific artifact",
                        ),
                    )
                    for name in names
                )
        except ReferenceArtifactRepositoryError:
            raise
        except (OSError, ReferenceCheckpointStoreError) as exc:
            raise ReferenceArtifactRepositoryError(str(exc)) from exc
        results: list[_StoredReferenceArtifact] = []
        for name, raw in raw_values:
            root = _strict_object(raw)
            authority = root.get("authority_checkpoint_sha256")
            try:
                authority = _validate_sha256(authority, "reference artifact checkpoint")
            except ReferenceCheckpointStoreError as exc:
                raise ReferenceArtifactRepositoryError(str(exc)) from exc
            parsed = _parse_artifact(
                raw,
                run_sha256=run_sha256,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=authority,
                kind=kind,
                subject_id=subject_id,
            )
            if name != self._name(
                kind,
                subject_id,
                authority,
                parsed.semantic_sha256,
            ):
                raise ReferenceArtifactRepositoryError(
                    "reference scientific artifact name differs from content"
                )
            results.append(parsed)
            self._validate_portable_locked(run_id, parsed)
        return tuple(results)

    def _load_all_canonical_inputs(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
    ) -> tuple[_StoredReferenceArtifact, ...]:
        """Reopen every closed canonical-input record for one live service run."""

        run_sha256 = self._run_sha256(run_id)
        try:
            with self._locked_run(run_id, create=False) as locked:
                run_root, run_handle = locked
                run_handle.validate()
                names = tuple(
                    sorted(
                        item.name
                        for item in os.scandir(
                            run_handle._descriptor
                            if run_handle._descriptor is not None
                            else run_root
                        )
                        if item.name.startswith("canonical_input-") and item.name.endswith(".json")
                    )
                )
                raw_values = tuple(
                    (
                        name,
                        _read_bounded_regular(
                            run_root / name,
                            limit=_MAX_ARTIFACT_BYTES,
                            label="canonical reference input",
                        ),
                    )
                    for name in names
                )
                locator_values = {
                    item.name: _read_bounded_regular(
                        run_root / item.name,
                        limit=_MAX_ARTIFACT_BYTES,
                        label="canonical locator binding pointer",
                    )
                    for item in os.scandir(
                        run_handle._descriptor if run_handle._descriptor is not None else run_root
                    )
                    if item.name.startswith("canonical_locator-") and item.name.endswith(".json")
                }
        except (OSError, ReferenceCheckpointStoreError) as exc:
            raise ReferenceArtifactRepositoryError(str(exc)) from exc
        results: list[_StoredReferenceArtifact] = []
        for name, raw in raw_values:
            root = _strict_object(raw)
            try:
                subject_id = _validate_subject(root.get("subject_id"))
                authority = _validate_sha256(
                    root.get("authority_checkpoint_sha256"),
                    "canonical input checkpoint",
                )
            except (ReferenceCheckpointStoreError, ValueError) as exc:
                raise ReferenceArtifactRepositoryError(str(exc)) from exc
            parsed = _parse_artifact(
                raw,
                run_sha256=run_sha256,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=authority,
                kind="canonical_input",
                subject_id=subject_id,
            )
            if name != self._name("canonical_input", subject_id, authority, parsed.semantic_sha256):
                raise ReferenceArtifactRepositoryError(
                    "canonical input artifact name differs from content"
                )
            pointer_name = _locator_pointer_name(subject_id)
            pointer_raw = locator_values.get(pointer_name)
            if pointer_raw is None:
                raise ReferenceArtifactRepositoryError(
                    "canonical locator binding pointer is missing"
                )
            rebound = _parse_locator_pointer(
                pointer_raw,
                run_sha256=run_sha256,
                start_authority_sha256=start_authority_sha256,
                subject_id=subject_id,
                scientific_record=parsed.record,
            )
            parsed = dataclass_replace(parsed, record=rebound)
            results.append(parsed)
        if set(locator_values) != {_locator_pointer_name(item.subject_id) for item in results}:
            raise ReferenceArtifactRepositoryError(
                "canonical locator binding pointer inventory differs"
            )
        inventory = tuple(results)
        if not inventory:
            return inventory
        targets = tuple(item for item in inventory if item.record.input_role == "target_image")
        if len(targets) != 1:
            raise ReferenceArtifactRepositoryError(
                "canonical input inventory must contain exactly one target"
            )
        claimed_ids: set[str] = set()
        for item in inventory:
            record = item.record
            expected_subject = "target" if record.input_role == "target_image" else record.input_id
            if item.subject_id != expected_subject:
                raise ReferenceArtifactRepositoryError(
                    "canonical input subject and role convention differs"
                )
            if record.input_id in claimed_ids:
                raise ReferenceArtifactRepositoryError(
                    "canonical input ID has duplicate persisted authorities"
                )
            claimed_ids.add(record.input_id)
        return inventory

    def load_pointer(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        kind: str,
        subject_id: str,
        pointer: ArtifactPointer,
    ) -> _StoredReferenceArtifact:
        if type(pointer) is not ArtifactPointer:
            raise TypeError("pointer must be exact ArtifactPointer")
        matches = tuple(
            item
            for item in self.load_all(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind=kind,
                subject_id=subject_id,
            )
            if item.content_sha256 == pointer.artifact_id
            and item.semantic_sha256 == pointer.artifact_sha256
        )
        if len(matches) != 1:
            raise ReferenceArtifactRepositoryError(
                "workflow artifact pointer does not resolve exactly"
            )
        return matches[0]


__all__ = ["ReferenceArtifactRepositoryError"]
