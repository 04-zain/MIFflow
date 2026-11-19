"""Append-only processing provenance for QuPath tissue revisions.

The `AnalysisProvenance` record carries `slide_id`, `canonical_image_id`,
`canonical_image_sha256`, `config_sha256`, `stage_hashes` and its remaining declared fields.

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
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from mif_pipeline.qupath_runtime.exchange import (
    _atomic_write_json,
    _content_digest,
    _export_json,
    _freeze_json,
    _safe_segment,
)
from mif_pipeline.datasets.identity import SortedIdDigest, SortedIdMappingDigest
from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.config import (
    AnalysisConfig,
    PhenotypingMode,
    ReviewPolicy,
)
from mif_pipeline.cell_analysis import (
    CanonicalSpatialFrameEvidence,
    FinalizedSecondaryDomainEvidence,
    ManualSelectionEvidence,
    NoSecondaryDomainCompletionEvidence,
    PrimaryExcludedDomainCompletionEvidence,
    SemanticResultArtifactEvidence,
    canonical_json_sha256,
    semantic_parent_content_key,
    semantic_secondary_content_key,
)
from mif_pipeline.registration.backend import (
    RegistrationRecord,
    TransferAttemptRecord,
    TransferQCRecord,
    import_transfer_qc_records,
    transfer_attempt_sha256,
    validate_registration_lineage,
)
from mif_pipeline.pipeline_core.stage_result import (
    InteractionAction,
    StageResult,
    validate_review_action,
)
from mif_pipeline.tissue_detection.generations import GenerationStatus
from mif_pipeline.tissue_detection.resolution import (
    NativeLevelReadAttestation,
    TissueResolutionSelection,
)
from mif_pipeline.orchestration.exports import OutputArtifact, load_spatial_handoff, SPATIAL_HANDOFF_SCHEMA_VERSION
from mif_pipeline.datasets.collection import CELL_COLLECTION_SCHEMA_VERSION

REVISION_SCHEMA = "roi-tissue-revision/v1"
ANALYSIS_PROVENANCE_SCHEMA = "roi-analysis-provenance/v2"
EVENT_TYPES = {"automatic_detection", "manual_edit", "review_action"}
TISSUE_REVISION_ACTIONS = {
    InteractionAction.ACCEPT.value,
    InteractionAction.EDIT.value,
    InteractionAction.REPEAT.value,
    InteractionAction.REJECT.value,
}


# ==============================================================================
#                                   Provenance
# ==============================================================================


class TissueRevisionStoreError(ROIAnalysisError):
    """Raised when an append would corrupt tissue revision history."""


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TissueRevisionStoreError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    text = _nonblank(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise TissueRevisionStoreError(f"{field} must be a lowercase SHA-256")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _freeze_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TissueRevisionStoreError(f"{field} must be a mapping")
    try:
        frozen = _freeze_json(value, field)
    except ROIAnalysisError as exc:
        raise TissueRevisionStoreError(str(exc)) from exc
    assert isinstance(frozen, Mapping)
    return frozen


def _freeze_mapping_sequence(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TissueRevisionStoreError(f"{field} must be a sequence")
    return tuple(_freeze_mapping(item, field) for item in value)


@dataclass(frozen=True, slots=True)
class ReviewActionRecord:
    """Strict audit record for one persisted human review decision."""

    stage: str
    slide_id: str
    generation_id: str | None
    action: str
    object_ids: tuple[str, ...]
    checkpoint_id: str

    def __post_init__(self) -> None:
        for field in ("stage", "slide_id", "checkpoint_id"):
            _nonblank(getattr(self, field), field)
        if self.generation_id is not None:
            _nonblank(self.generation_id, "generation_id")
        try:
            action = validate_review_action(self.stage, self.action)
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError("review action is invalid") from exc
        object.__setattr__(self, "action", action)
        if isinstance(self.object_ids, (str, bytes)) or not isinstance(self.object_ids, Sequence):
            raise TissueRevisionStoreError("review action object_ids must be a sequence")
        object_ids = tuple(_nonblank(item, "object_ids") for item in self.object_ids)
        if not object_ids or len(set(object_ids)) != len(object_ids):
            raise TissueRevisionStoreError(
                "review action object_ids must contain unique object IDs"
            )
        object.__setattr__(self, "object_ids", object_ids)

    @classmethod
    def from_mapping(cls, value: object) -> "ReviewActionRecord":
        if not isinstance(value, Mapping):
            raise TissueRevisionStoreError("review action must be a mapping")
        required = {
            "stage",
            "slide_id",
            "generation_id",
            "action",
            "object_ids",
            "checkpoint_id",
        }
        if set(value) != required:
            raise TissueRevisionStoreError("review action fields do not match the schema")
        object_ids = value["object_ids"]
        if isinstance(object_ids, (str, bytes)) or not isinstance(object_ids, Sequence):
            raise TissueRevisionStoreError("review action object_ids must be a sequence")
        try:
            return cls(
                stage=value["stage"],
                slide_id=value["slide_id"],
                generation_id=value["generation_id"],
                action=value["action"],
                object_ids=tuple(object_ids),
                checkpoint_id=value["checkpoint_id"],
            )
        except (KeyError, TypeError, ROIAnalysisError) as exc:
            raise TissueRevisionStoreError("review action record is invalid") from exc

    def to_mapping(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "slide_id": self.slide_id,
            "generation_id": self.generation_id,
            "action": self.action,
            "object_ids": list(self.object_ids),
            "checkpoint_id": self.checkpoint_id,
        }


def _registration_record(
    value: object,
    *,
    slide_id: str,
    canonical_image_id: str,
    canonical_image_sha256: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TissueRevisionStoreError("registration record must be a mapping")
    try:
        parsed = RegistrationRecord.from_mapping(value)
        if (
            parsed.fixed_image_id != canonical_image_id
            or parsed.fixed_image_sha256 != canonical_image_sha256
        ):
            raise TissueRevisionStoreError(
                "registration record does not target the canonical image identity"
            )
    except ROIAnalysisError as exc:
        if isinstance(exc, TissueRevisionStoreError):
            raise
        raise TissueRevisionStoreError("registration record is invalid") from exc
    return _freeze_mapping(parsed.to_mapping(), "registrations")


@dataclass(frozen=True, slots=True)
class AnalysisProvenance:
    """Complete reproducibility context for one ROI-analysis output set."""

    slide_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    config_sha256: str
    stage_hashes: Mapping[str, str]
    pipeline_version: str
    qupath: Mapping[str, Any]
    extensions: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    models: tuple[Mapping[str, Any], ...]
    classifiers: tuple[Mapping[str, Any], ...]
    channel_selection: Mapping[str, Any]
    accepted_generation_context: Mapping[str, Any]
    registrations: tuple[Mapping[str, Any], ...]
    review_actions: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    checkpoint_ids: tuple[str, ...]
    stage_results: tuple[Mapping[str, Any], ...]
    stage_runtimes: tuple[Mapping[str, Any], ...]
    transfer_qc_records: tuple[Mapping[str, Any], ...] = ()
    transfer_attempts: tuple[Mapping[str, Any], ...] = ()
    transfer_commit_evidence: tuple[Mapping[str, Any], ...] = ()
    transfer_cleanup_evidence: tuple[Mapping[str, Any], ...] = ()
    transfer_commit_outcomes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Validate the AnalysisProvenance fields when the record is constructed.

        The checks cover ``transfer_qc_records``, ``transfer_attempts``,
        ``transfer_commit_evidence``, ``transfer_cleanup_evidence``,
        ``transfer_commit_outcomes``, ``review_actions``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        for field in ("slide_id", "canonical_image_id", "pipeline_version"):
            _nonblank(getattr(self, field), field)
        _sha256(self.canonical_image_sha256, "canonical_image_sha256")
        _sha256(self.config_sha256, "config_sha256")
        stage_hashes = _freeze_mapping(self.stage_hashes, "stage_hashes")
        for stage, digest in stage_hashes.items():
            _nonblank(stage, "stage_hashes key")
            _sha256(digest, f"stage_hashes.{stage}")
        object.__setattr__(self, "stage_hashes", stage_hashes)
        qupath = _freeze_mapping(self.qupath, "qupath")
        if qupath:
            _nonblank(qupath.get("version"), "qupath.version")
            _sha256(qupath.get("sha256"), "qupath.sha256")
        object.__setattr__(self, "qupath", qupath)
        identity_fields = {
            "extensions": "name",
            "tools": "name",
            "models": "model_id",
            "classifiers": "classifier_id",
        }
        for field in ("extensions", "tools", "models", "classifiers"):
            values = _freeze_mapping_sequence(getattr(self, field), field)
            for index, item in enumerate(values):
                identity_field = identity_fields[field]
                _nonblank(item.get(identity_field), f"{field}[{index}].{identity_field}")
                _nonblank(item.get("version"), f"{field}[{index}].version")
                _sha256(item.get("sha256"), f"{field}[{index}].sha256")
            object.__setattr__(self, field, values)
        channel_selection = _freeze_mapping(self.channel_selection, "channel_selection")
        if channel_selection:
            if set(channel_selection) != {
                "reference_channel_id",
                "reference_channel_selection_source",
            }:
                raise TissueRevisionStoreError("channel_selection fields do not match the schema")
            _nonblank(
                channel_selection["reference_channel_id"],
                "channel_selection.reference_channel_id",
            )
            if channel_selection["reference_channel_selection_source"] not in {
                "canonical_metadata",
                "user_override",
            }:
                raise TissueRevisionStoreError(
                    "channel_selection.reference_channel_selection_source is invalid"
                )
        object.__setattr__(self, "channel_selection", channel_selection)
        accepted_context = _freeze_mapping(
            self.accepted_generation_context, "accepted_generation_context"
        )
        if accepted_context:
            required_context = {
                "generation_id",
                "canonical_image_id",
                "canonical_image_sha256",
                "detector_id",
                "detector_version",
                "detector_settings_hash",
                "reference_channel_id",
                "reference_channel_selection_source",
                "stage_version",
                "tissue_resolution_selection",
                "native_level_read_attestation",
            }
            if set(accepted_context) != required_context:
                raise TissueRevisionStoreError(
                    "accepted_generation_context fields do not match the schema"
                )
            for field in (
                "generation_id",
                "canonical_image_id",
                "detector_id",
                "detector_version",
                "reference_channel_id",
                "stage_version",
            ):
                _nonblank(accepted_context[field], f"accepted_generation_context.{field}")
            _sha256(
                accepted_context["canonical_image_sha256"],
                "accepted_generation_context.canonical_image_sha256",
            )
            _sha256(
                accepted_context["detector_settings_hash"],
                "accepted_generation_context.detector_settings_hash",
            )
            if accepted_context["reference_channel_selection_source"] not in {
                "canonical_metadata",
                "user_override",
            }:
                raise TissueRevisionStoreError(
                    "accepted_generation_context reference-channel source is invalid"
                )
            try:
                selection = TissueResolutionSelection.from_mapping(
                    accepted_context["tissue_resolution_selection"]
                )
                read_attestation = NativeLevelReadAttestation.from_mapping(
                    accepted_context["native_level_read_attestation"]
                )
                read_attestation.validate_selection_binding(selection)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "accepted_generation_context tissue resolution evidence is invalid"
                ) from exc
            if (
                accepted_context["canonical_image_id"] != self.canonical_image_id
                or accepted_context["canonical_image_sha256"] != self.canonical_image_sha256
            ):
                raise TissueRevisionStoreError(
                    "accepted_generation_context canonical image identity does not match"
                )
        object.__setattr__(self, "accepted_generation_context", accepted_context)
        if isinstance(self.registrations, (str, bytes)) or not isinstance(
            self.registrations, Sequence
        ):
            raise TissueRevisionStoreError("registrations must be a sequence")
        parsed_registrations = tuple(
            RegistrationRecord.from_mapping(
                _registration_record(
                    item,
                    slide_id=self.slide_id,
                    canonical_image_id=self.canonical_image_id,
                    canonical_image_sha256=self.canonical_image_sha256,
                )
            )
            for item in self.registrations
        )
        try:
            validate_registration_lineage(parsed_registrations)
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError("registration provenance lineage is invalid") from exc
        object.__setattr__(
            self,
            "registrations",
            tuple(
                _freeze_mapping(item.to_mapping(), "registrations") for item in parsed_registrations
            ),
        )
        registrations_by_id = {item.registration_id: item for item in parsed_registrations}
        if isinstance(self.transfer_qc_records, (str, bytes)) or not isinstance(
            self.transfer_qc_records, Sequence
        ):
            raise TissueRevisionStoreError("transfer_qc_records must be a sequence")
        transfer_records: list[Mapping[str, Any]] = []
        for raw in self.transfer_qc_records:
            try:
                record = TransferQCRecord.from_mapping(raw)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError("transfer QC provenance record is invalid") from exc
            registration = registrations_by_id.get(record.registration_id)
            if registration is None or registration.revision != record.registration_revision:
                raise TissueRevisionStoreError(
                    "transfer QC provenance lacks its exact registration revision"
                )
            try:
                import_transfer_qc_records(
                    (record.to_mapping(),),
                    registration=registration,
                    tissue_generation_id=registration.tissue_generation_id,
                    tissue_section_ids=registration.tissue_section_ids,
                    policy=record.policy,
                )
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "transfer QC provenance scope conflicts with registration"
                ) from exc
            transfer_records.append(_freeze_mapping(record.to_mapping(), "transfer_qc_records"))
        if len({item["transfer_qc_id"] for item in transfer_records}) != len(transfer_records):
            raise TissueRevisionStoreError("transfer QC provenance IDs must be unique")
        object.__setattr__(self, "transfer_qc_records", tuple(transfer_records))
        if isinstance(self.transfer_attempts, (str, bytes)) or not isinstance(
            self.transfer_attempts, Sequence
        ):
            raise TissueRevisionStoreError("transfer_attempts must be a sequence")
        attempts: list[Mapping[str, Any]] = []
        qc_owners: dict[str, str] = {}
        attempt_digests: set[str] = set()
        for raw in self.transfer_attempts:
            try:
                attempt = TransferAttemptRecord.from_mapping(raw)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "transfer attempt provenance record is invalid"
                ) from exc
            registration = registrations_by_id.get(attempt.registration.registration_id)
            if (
                registration is None
                or registration.to_mapping() != attempt.registration.to_mapping()
            ):
                raise TissueRevisionStoreError(
                    "transfer attempt provenance registration scope conflicts"
                )
            for qc_record in attempt.qc_records:
                stored = next(
                    (
                        item
                        for item in transfer_records
                        if item["transfer_qc_id"] == qc_record.transfer_qc_id
                    ),
                    None,
                )
                if stored is None or _export_json(stored) != qc_record.to_mapping():
                    raise TissueRevisionStoreError(
                        "transfer attempt provenance conflicts with transfer QC evidence"
                    )
                owner = qc_owners.setdefault(qc_record.transfer_qc_id, attempt.attempt_id)
                if owner != attempt.attempt_id:
                    raise TissueRevisionStoreError(
                        "transfer QC provenance row has multiple attempt owners"
                    )
            digest = transfer_attempt_sha256(attempt)
            if digest in attempt_digests:
                raise TissueRevisionStoreError("transfer attempt provenance digest is duplicated")
            attempt_digests.add(digest)
            attempts.append(_freeze_mapping(attempt.to_mapping(), "transfer_attempts"))
        if len({item["attempt_id"] for item in attempts}) != len(attempts):
            raise TissueRevisionStoreError("transfer attempt provenance IDs must be unique")
        if set(qc_owners) != {item["transfer_qc_id"] for item in transfer_records}:
            raise TissueRevisionStoreError(
                "transfer QC provenance lacks its exact append-only attempt"
            )
        object.__setattr__(self, "transfer_attempts", tuple(attempts))
        attempts_by_id = {item["attempt_id"]: item for item in attempts}
        if isinstance(self.transfer_commit_evidence, (str, bytes)) or not isinstance(
            self.transfer_commit_evidence, Sequence
        ):
            raise TissueRevisionStoreError("transfer_commit_evidence must be a sequence")
        commits: list[Mapping[str, Any]] = []
        committed_attempt_ids: set[str] = set()
        committed_target_ids: set[str] = set()
        for raw in self.transfer_commit_evidence:
            if (
                not isinstance(raw, Mapping)
                or set(raw)
                != {
                    "schema_version",
                    "attempt_id",
                    "registration_id",
                    "registration_revision",
                    "authoritative_object_ids",
                    "objects",
                }
                or raw.get("schema_version") != "qupath-warpy-transfer-commit/v1"
            ):
                raise TissueRevisionStoreError("transfer commit provenance is invalid")
            attempt = attempts_by_id.get(raw.get("attempt_id"))
            ids = raw.get("authoritative_object_ids")
            objects = raw.get("objects")
            accepted = (
                {
                    item["target_object_id"]: item
                    for item in attempt["qc_records"]
                    if item["disposition"] == "accepted"
                }
                if attempt is not None
                else {}
            )
            if (
                attempt is None
                or raw.get("registration_id") != attempt["registration"]["registration_id"]
                or raw.get("registration_revision") != attempt["registration"]["revision"]
                or not isinstance(ids, Sequence)
                or isinstance(ids, (str, bytes))
                or not isinstance(objects, Sequence)
                or isinstance(objects, (str, bytes))
                or len(set(ids)) != len(ids)
                or list(ids) != list(accepted)
                or len(objects) != len(ids)
                or raw.get("attempt_id") in committed_attempt_ids
                or bool(set(ids) & committed_target_ids)
            ):
                raise TissueRevisionStoreError(
                    "transfer commit provenance conflicts with accepted transfer QC"
                )
            for target_id, row in zip(ids, objects, strict=True):
                record = accepted.get(target_id)
                if record is None or not isinstance(row, Mapping):
                    raise TissueRevisionStoreError(
                        "transfer commit provenance object row conflicts with QC"
                    )
                expected = {
                    "source_object_id": record["source_object_id"],
                    "target_object_id": record["target_object_id"],
                    "target_section_id": record["target_section_id"],
                    "target_geometry_sha256": record["transformed_geometry_sha256"],
                }
                if (
                    set(row) != {*expected, "reconciled"}
                    or any(row.get(field) != value for field, value in expected.items())
                    or not isinstance(row.get("reconciled"), bool)
                ):
                    raise TissueRevisionStoreError(
                        "transfer commit provenance object row conflicts with QC"
                    )
            committed_attempt_ids.add(raw["attempt_id"])
            committed_target_ids.update(ids)
            commits.append(_freeze_mapping(raw, "transfer_commit_evidence"))
        object.__setattr__(self, "transfer_commit_evidence", tuple(commits))
        if isinstance(self.transfer_cleanup_evidence, (str, bytes)) or not isinstance(
            self.transfer_cleanup_evidence, Sequence
        ):
            raise TissueRevisionStoreError("transfer_cleanup_evidence must be a sequence")
        cleanups: list[Mapping[str, Any]] = []
        cleanups_by_attempt: dict[str, Mapping[str, Any]] = {}
        for raw in self.transfer_cleanup_evidence:
            if (
                not isinstance(raw, Mapping)
                or set(raw)
                != {
                    "schema_version",
                    "attempt_id",
                    "registration_id",
                    "registration_revision",
                    "absent_target_object_ids",
                    "objects",
                }
                or raw.get("schema_version") != "qupath-warpy-transfer-cleanup/v1"
            ):
                raise TissueRevisionStoreError("transfer cleanup provenance is invalid")
            attempt = attempts_by_id.get(raw.get("attempt_id"))
            accepted = (
                [item for item in attempt["qc_records"] if item["disposition"] == "accepted"]
                if attempt is not None
                else []
            )
            ids = raw.get("absent_target_object_ids")
            rows = raw.get("objects")
            if (
                attempt is None
                or raw.get("attempt_id") in cleanups_by_attempt
                or raw.get("registration_id") != attempt["registration"]["registration_id"]
                or raw.get("registration_revision") != attempt["registration"]["revision"]
                or not isinstance(ids, Sequence)
                or isinstance(ids, (str, bytes))
                or list(ids) != [item["target_object_id"] for item in accepted]
                or not isinstance(rows, Sequence)
                or isinstance(rows, (str, bytes))
                or len(rows) != len(accepted)
            ):
                raise TissueRevisionStoreError("transfer cleanup provenance scope conflicts")
            for record, row in zip(accepted, rows, strict=True):
                expected = {
                    "source_object_id": record["source_object_id"],
                    "target_object_id": record["target_object_id"],
                    "transfer_qc_id": record["transfer_qc_id"],
                    "absent": True,
                }
                if not isinstance(row, Mapping) or dict(row) != expected:
                    raise TissueRevisionStoreError("transfer cleanup absence row conflicts with QC")
            frozen = _freeze_mapping(raw, "transfer_cleanup_evidence")
            cleanups_by_attempt[raw["attempt_id"]] = frozen
            cleanups.append(frozen)
        object.__setattr__(self, "transfer_cleanup_evidence", tuple(cleanups))
        if isinstance(self.transfer_commit_outcomes, (str, bytes)) or not isinstance(
            self.transfer_commit_outcomes, Sequence
        ):
            raise TissueRevisionStoreError("transfer_commit_outcomes must be a sequence")
        outcomes: list[Mapping[str, Any]] = []
        outcomes_by_attempt: dict[str, list[Mapping[str, Any]]] = {}
        outcome_fields = {
            "schema_version",
            "attempt_id",
            "sequence",
            "status",
            "failure",
            "failure_sha256",
            "live_attestation",
        }
        commit_json = [_export_json(item) for item in commits]
        for raw in self.transfer_commit_outcomes:
            if (
                not isinstance(raw, Mapping)
                or set(raw) != outcome_fields
                or raw.get("schema_version") != "warpy-transfer-commit-outcome/v1"
            ):
                raise TissueRevisionStoreError("transfer commit outcome is invalid")
            attempt = attempts_by_id.get(raw.get("attempt_id"))
            status = raw.get("status")
            if attempt is None or status not in {
                "failed",
                "committed",
                "reconciled",
                "not_required",
                "quarantined",
                "aborted",
            }:
                raise TissueRevisionStoreError("transfer commit outcome lacks its exact attempt")
            history = outcomes_by_attempt.setdefault(raw["attempt_id"], [])
            if raw.get("sequence") != len(history) + 1:
                raise TissueRevisionStoreError("transfer commit outcome sequence is not contiguous")
            failure = raw.get("failure")
            failure_sha256 = raw.get("failure_sha256")
            live = raw.get("live_attestation")
            authoritative = any(item["disposition"] == "accepted" for item in attempt["qc_records"])
            if status == "committed":
                expected_ids = {
                    item["target_object_id"]
                    for item in attempt["qc_records"]
                    if item["disposition"] == "accepted"
                }
                if (
                    failure is not None
                    or failure_sha256 is not None
                    or not isinstance(live, Mapping)
                    or _export_json(live) not in commit_json
                    or live.get("attempt_id") != raw["attempt_id"]
                    or set(live.get("authoritative_object_ids", ())) != expected_ids
                ):
                    raise TissueRevisionStoreError(
                        "committed transfer outcome conflicts with live attestation"
                    )
            elif status == "reconciled":
                accepted = [
                    item for item in attempt["qc_records"] if item["disposition"] == "accepted"
                ]
                ids = live.get("authoritative_object_ids") if isinstance(live, Mapping) else None
                rows = live.get("objects") if isinstance(live, Mapping) else None
                if (
                    failure is not None
                    or failure_sha256 is not None
                    or not isinstance(live, Mapping)
                    or set(live)
                    != {
                        "schema_version",
                        "attempt_id",
                        "registration_id",
                        "registration_revision",
                        "authoritative_object_ids",
                        "objects",
                    }
                    or live.get("schema_version") != "qupath-warpy-transfer-commit/v1"
                    or live.get("attempt_id") != raw["attempt_id"]
                    or live.get("registration_id") != attempt["registration"]["registration_id"]
                    or live.get("registration_revision") != attempt["registration"]["revision"]
                    or not isinstance(ids, Sequence)
                    or isinstance(ids, (str, bytes))
                    or list(ids) != [item["target_object_id"] for item in accepted]
                    or not isinstance(rows, Sequence)
                    or isinstance(rows, (str, bytes))
                    or len(rows) != len(accepted)
                ):
                    raise TissueRevisionStoreError(
                        "reconciled transfer outcome conflicts with live attestation"
                    )
                for record, row in zip(accepted, rows, strict=True):
                    expected = {
                        "source_object_id": record["source_object_id"],
                        "target_object_id": record["target_object_id"],
                        "target_section_id": record["target_section_id"],
                        "target_geometry_sha256": record["transformed_geometry_sha256"],
                    }
                    if (
                        not isinstance(row, Mapping)
                        or set(row) != {*expected, "reconciled"}
                        or any(row.get(key) != value for key, value in expected.items())
                        or not isinstance(row.get("reconciled"), bool)
                    ):
                        raise TissueRevisionStoreError(
                            "reconciled transfer outcome object scope conflicts"
                        )
            elif status == "not_required":
                if (
                    authoritative
                    or failure is not None
                    or failure_sha256 is not None
                    or live is not None
                ):
                    raise TissueRevisionStoreError(
                        "not-required transfer outcome conflicts with QC"
                    )
            else:
                if (
                    not isinstance(failure, Mapping)
                    or set(failure)
                    != {
                        "schema_version",
                        "attempt_id",
                        "exception_type",
                        "message",
                        "mutation_state",
                    }
                    or failure.get("schema_version") != "warpy-transfer-commit-failure/v1"
                    or failure.get("attempt_id") != raw["attempt_id"]
                    or failure.get("mutation_state")
                    not in {"proven_pre_mutation", "indeterminate_or_persisted"}
                    or live is not None
                ):
                    raise TissueRevisionStoreError(
                        "noncommitted transfer outcome requires failure evidence"
                    )
                digest = hashlib.sha256(
                    json.dumps(_export_json(failure), sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if failure_sha256 != digest:
                    raise TissueRevisionStoreError(
                        "transfer commit outcome failure digest is invalid"
                    )
            frozen = _freeze_mapping(raw, "transfer_commit_outcomes")
            history.append(frozen)
            outcomes.append(frozen)
        if set(outcomes_by_attempt) != set(attempts_by_id):
            raise TissueRevisionStoreError(
                "transfer attempt provenance lacks a terminal commit outcome"
            )
        for history in outcomes_by_attempt.values():
            terminal = history[-1]
            statuses = [item["status"] for item in history]
            live_state = False
            reconciliation_failed = False
            valid = statuses[-1] in {
                "committed",
                "reconciled",
                "not_required",
                "quarantined",
                "aborted",
            }
            for index, status in enumerate(statuses):
                if status == "failed":
                    if live_state:
                        reconciliation_failed = True
                elif status == "committed":
                    if live_state or reconciliation_failed:
                        valid = False
                    live_state = True
                elif status == "reconciled":
                    if not live_state:
                        valid = False
                    reconciliation_failed = False
                elif status == "not_required":
                    if index != 0 or len(statuses) != 1:
                        valid = False
                elif status in {"quarantined", "aborted"}:
                    if index != len(statuses) - 1:
                        valid = False
            if not valid or statuses.count("committed") > 1:
                raise TissueRevisionStoreError(
                    "transfer commit outcome history lacks one terminal disposition"
                )
            cleanup_required = terminal["status"] in {"quarantined", "aborted"} and (
                "committed" in statuses
                or any(
                    item["status"] == "failed"
                    and item["failure"]["mutation_state"] == "indeterminate_or_persisted"
                    for item in history
                )
            )
            if cleanup_required != (terminal["attempt_id"] in cleanups_by_attempt):
                raise TissueRevisionStoreError(
                    "indeterminate noncommit outcome lacks exact cleanup attestation"
                )
        outcome_attestations = [
            _export_json(item["live_attestation"])
            for item in outcomes
            if item["status"] == "committed"
        ]
        if sorted(
            json.dumps(item, sort_keys=True, separators=(",", ":")) for item in outcome_attestations
        ) != sorted(
            json.dumps(item, sort_keys=True, separators=(",", ":")) for item in commit_json
        ):
            raise TissueRevisionStoreError(
                "live transfer commit evidence lacks its exact attempt outcome"
            )
        object.__setattr__(self, "transfer_commit_outcomes", tuple(outcomes))
        if isinstance(self.review_actions, (str, bytes)) or not isinstance(
            self.review_actions, Sequence
        ):
            raise TissueRevisionStoreError("review_actions must be a sequence")
        review_actions: list[Mapping[str, Any]] = []
        for item in self.review_actions:
            action = ReviewActionRecord.from_mapping(item)
            if action.slide_id != self.slide_id:
                raise TissueRevisionStoreError("review action slide_id does not match provenance")
            review_actions.append(_freeze_mapping(action.to_mapping(), "review_actions"))
        object.__setattr__(self, "review_actions", tuple(review_actions))
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TissueRevisionStoreError("warnings must be a sequence")
        warnings = tuple(_nonblank(item, "warnings") for item in self.warnings)
        object.__setattr__(self, "warnings", warnings)
        if isinstance(self.checkpoint_ids, (str, bytes)) or not isinstance(
            self.checkpoint_ids, Sequence
        ):
            raise TissueRevisionStoreError("checkpoint_ids must be a sequence")
        checkpoints = tuple(_nonblank(item, "checkpoint_ids") for item in self.checkpoint_ids)
        if len(set(checkpoints)) != len(checkpoints):
            raise TissueRevisionStoreError("checkpoint_ids must be unique")
        object.__setattr__(self, "checkpoint_ids", checkpoints)
        if isinstance(self.stage_results, (str, bytes)) or not isinstance(
            self.stage_results, Sequence
        ):
            raise TissueRevisionStoreError("stage_results must be a sequence")
        stage_results: list[Mapping[str, Any]] = []
        for index, value in enumerate(self.stage_results):
            try:
                result = StageResult.from_mapping(value)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(f"stage_results[{index}] is invalid") from exc
            if result.slide_id != self.slide_id:
                raise TissueRevisionStoreError(
                    f"stage_results[{index}].slide_id does not match provenance"
                )
            stage_results.append(_freeze_mapping(result.to_mapping(), f"stage_results[{index}]"))
        object.__setattr__(self, "stage_results", tuple(stage_results))
        runtimes = _freeze_mapping_sequence(self.stage_runtimes, "stage_runtimes")
        for index, runtime in enumerate(runtimes):
            stage = runtime.get("stage")
            seconds = runtime.get("runtime_seconds")
            _nonblank(stage, f"stage_runtimes[{index}].stage")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not math.isfinite(float(seconds))
                or seconds < 0
            ):
                raise TissueRevisionStoreError(
                    f"stage_runtimes[{index}].runtime_seconds must be finite and non-negative"
                )
        object.__setattr__(self, "stage_runtimes", runtimes)

    @classmethod
    def minimal(
        cls,
        *,
        slide_id: str,
        canonical_image_id: str,
        canonical_image_sha256: str,
        config_sha256: str,
        pipeline_version: str,
    ) -> "AnalysisProvenance":
        """Create an explicit empty-context record for early-stage/failure exports."""
        return cls(
            slide_id=slide_id,
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            config_sha256=config_sha256,
            stage_hashes={},
            pipeline_version=pipeline_version,
            qupath={},
            extensions=(),
            tools=(),
            models=(),
            classifiers=(),
            channel_selection={},
            accepted_generation_context={},
            registrations=(),
            review_actions=(),
            warnings=(),
            checkpoint_ids=(),
            stage_results=(),
            stage_runtimes=(),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "slide_id": self.slide_id,
            "canonical_image": {
                "ome_image_id": self.canonical_image_id,
                "sha256": self.canonical_image_sha256,
            },
            "configuration": {
                "sha256": self.config_sha256,
                "stage_hashes": _export_json(self.stage_hashes),
            },
            "software": {
                "pipeline_version": self.pipeline_version,
                "qupath": _export_json(self.qupath),
                "extensions": _export_json(self.extensions),
                "tools": _export_json(self.tools),
            },
            "models": _export_json(self.models),
            "classifiers": _export_json(self.classifiers),
            "channel_selection": _export_json(self.channel_selection),
            "accepted_generation_context": _export_json(self.accepted_generation_context),
            "registrations": _export_json(self.registrations),
            "transfer_qc_records": _export_json(self.transfer_qc_records),
            "transfer_attempts": _export_json(self.transfer_attempts),
            "transfer_commit_evidence": _export_json(self.transfer_commit_evidence),
            "transfer_cleanup_evidence": _export_json(self.transfer_cleanup_evidence),
            "transfer_commit_outcomes": _export_json(self.transfer_commit_outcomes),
            "review_actions": _export_json(self.review_actions),
            "warnings": list(self.warnings),
            "checkpoint_ids": list(self.checkpoint_ids),
            "stage_results": _export_json(self.stage_results),
            "stage_runtimes": _export_json(self.stage_runtimes),
            "phenotyping": {
                **_phenotyping_provenance(self.stage_results),
            },
            "semantic": _semantic_provenance(
                self.stage_results,
            ),
        }


def _phenotyping_provenance(
    stage_results: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Distill coexistence from the completed workflow result without API drift."""
    candidate: tuple[object, object, object, object] | None = None
    for result in stage_results:
        outputs = result.get("outputs") if isinstance(result, Mapping) else None
        if not isinstance(outputs, Mapping):
            continue
        if "phenotype_generations" in outputs:
            candidate = (
                outputs.get("phenotype_generations", {}),
                outputs.get("selected_phenotype_view"),
                outputs.get("selected_phenotype_generation_id"),
                outputs.get("phenotype_inventory_sha256"),
            )
    if candidate is None:
        return {
            "views": {},
            "selected_active_view": None,
            "selected_active_generation_id": None,
            "inventory_sha256": None,
        }
    raw_views, raw_selected, raw_generation, raw_inventory_sha = candidate
    if not isinstance(raw_views, Mapping):
        raise TissueRevisionStoreError("phenotype generations must be a mapping")
    views: dict[str, list[dict[str, object]]] = {}
    for raw_mode, raw_generations in raw_views.items():
        try:
            mode = PhenotypingMode(raw_mode).value
        except (TypeError, ValueError) as exc:
            raise TissueRevisionStoreError("phenotype view mode is invalid") from exc
        if isinstance(raw_generations, (str, bytes)) or not isinstance(raw_generations, Sequence):
            raise TissueRevisionStoreError("phenotype view generations must be a sequence")
        generation_ids: set[str] = set()
        generations: list[dict[str, object]] = []
        for raw_generation_entry in raw_generations:
            frozen = _freeze_mapping(raw_generation_entry, f"phenotype views.{mode}")
            generation_id = _nonblank(
                frozen.get("generation_id"), f"phenotype views.{mode}.generation_id"
            )
            if generation_id in generation_ids:
                raise TissueRevisionStoreError(
                    "phenotype view generation IDs must be unique per mode"
                )
            generation_ids.add(generation_id)
            digest = frozen.get("artifact_sha256")
            if digest is not None:
                _sha256(digest, f"phenotype views.{mode}.artifact_sha256")
            exported = _export_json(frozen)
            assert isinstance(exported, dict)
            generations.append(exported)
        views[mode] = generations
    try:
        selected = PhenotypingMode(raw_selected).value
    except (TypeError, ValueError) as exc:
        raise TissueRevisionStoreError("selected phenotype view is invalid") from exc
    if selected not in views:
        raise TissueRevisionStoreError(
            "selected phenotype view must exist in phenotype generations"
        )
    selected_generation = _nonblank(raw_generation, "selected phenotype generation")
    if selected_generation not in {generation["generation_id"] for generation in views[selected]}:
        raise TissueRevisionStoreError(
            "selected phenotype generation must exist in the selected view"
        )
    inventory_sha = (
        None
        if raw_inventory_sha is None
        else _sha256(raw_inventory_sha, "phenotype inventory SHA-256")
    )
    return {
        "views": views,
        "selected_active_view": selected,
        "selected_active_generation_id": selected_generation,
        "inventory_sha256": inventory_sha,
    }


def _semantic_provenance(
    stage_results: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Project bounded semantic descriptors and exact reviewer lineage."""
    result_artifacts: list[object] = []
    finalizations: list[object] = []
    region_decisions: list[object] = []
    count_decisions: list[object] = []
    for result in stage_results:
        if result.get("stage") != "semantic_validation":
            continue
        outputs = result.get("outputs") if isinstance(result, Mapping) else None
        if not isinstance(outputs, Mapping):
            continue
        finalization: (
            FinalizedSecondaryDomainEvidence | NoSecondaryDomainCompletionEvidence | None
        ) = None
        raw_finalization = outputs.get("domain_finalization")
        if raw_finalization is None and outputs.get("kind") in {
            "secondary_finalized",
            "no_secondary_configured",
        }:
            raw_finalization = outputs
        if raw_finalization is not None:
            try:
                if raw_finalization.get("kind") == "secondary_finalized":
                    finalization = FinalizedSecondaryDomainEvidence.from_mapping(raw_finalization)
                elif raw_finalization.get("kind") == "no_secondary_configured":
                    finalization = NoSecondaryDomainCompletionEvidence.from_mapping(
                        raw_finalization
                    )
                else:
                    raise TissueRevisionStoreError("semantic domain finalization kind is invalid")
            except (AttributeError, ROIAnalysisError) as exc:
                raise TissueRevisionStoreError(
                    "semantic domain finalization evidence is invalid"
                ) from exc
            finalizations.append(finalization.to_mapping())
            if isinstance(finalization, NoSecondaryDomainCompletionEvidence):
                raw_descriptor = outputs.get("raw_input_descriptor")
                expected = {
                    "analysis_domain_id": finalization.analysis_domain_id,
                    "section_id": finalization.section_id,
                    "analysis_config_sha256": (finalization.analysis_config_sha256),
                }
                if (
                    any(
                        _export_json(outputs.get(field)) != _export_json(value)
                        for field, value in expected.items()
                    )
                    or not isinstance(raw_descriptor, Mapping)
                    or set(raw_descriptor) != {"path", "sha256"}
                    or not isinstance(raw_descriptor.get("path"), str)
                    or not str(raw_descriptor["path"]).strip()
                    or raw_descriptor.get("sha256") != finalization.raw_input_sha256
                ):
                    raise TissueRevisionStoreError(
                        "no-secondary outer semantic projections conflict with completion evidence"
                    )
        semantic_keys = {
            "semantic_result_artifact",
            "semantic_finalization",
            "final_secondary_count_decision",
            "manual_selection_evidence",
            "final_object_dispositions",
        }
        if any(key in outputs for key in semantic_keys) and finalization is None:
            raise TissueRevisionStoreError(
                "completed semantic evidence requires domain finalization evidence"
            )
        if isinstance(finalization, FinalizedSecondaryDomainEvidence):
            _validate_semantic_review_projection(
                outputs,
            )
            raw_artifact = outputs.get("semantic_result_artifact")
            try:
                artifact = SemanticResultArtifactEvidence.from_mapping(raw_artifact)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "semantic result artifact descriptor is invalid"
                ) from exc
            if artifact.to_mapping() != finalization.semantic_result_artifact.to_mapping():
                raise TissueRevisionStoreError(
                    "semantic result artifact conflicts with domain finalization"
                )
            if _export_json(outputs.get("semantic_finalization")) != _export_json(
                finalization.finalization_receipt
            ) or _export_json(outputs.get("final_secondary_count_decision")) != _export_json(
                finalization.count_decision
            ):
                raise TissueRevisionStoreError(
                    "semantic receipt/count evidence conflicts with domain finalization"
                )
            raw_manual = outputs.get("manual_selection_evidence", ())
            if isinstance(raw_manual, (str, bytes)) or not isinstance(raw_manual, Sequence):
                raise TissueRevisionStoreError("semantic manual-selection evidence is invalid")
            try:
                manual = tuple(ManualSelectionEvidence.from_mapping(item) for item in raw_manual)
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "semantic manual-selection evidence is invalid"
                ) from exc
            if [item.to_mapping() for item in manual] != [
                item.to_mapping() for item in finalization.manual_selections
            ]:
                raise TissueRevisionStoreError(
                    "semantic manual-selection evidence conflicts with finalization"
                )
            receipt = finalization.finalization_receipt
            expected_projections = {
                "analysis_domain_id": finalization.analysis_domain_id,
                "section_id": receipt["section_id"],
                "analysis_domain_revision_sha256": (finalization.analysis_domain_revision_sha256),
                "domain_set_sha256": finalization.domain_set_sha256,
                "spatial_frame_sha256": finalization.spatial_frame_sha256,
                "semantic_result_set_sha256": (
                    finalization.semantic_result_artifact.semantic_result_set_sha256
                ),
                "final_object_dispositions": [
                    item.to_mapping() for item in finalization.dispositions
                ],
                "active_secondary_roi_ids": receipt["active_secondary_roi_ids"],
                "domain_status": finalization.domain_status,
                "coordinate_system": "global_level0",
            }
            revision = outputs.get("analysis_domain_revision")
            raw_descriptor = outputs.get("raw_input_descriptor")
            semantic_qc = outputs.get("semantic_qc")
            if (
                any(
                    _export_json(outputs.get(field)) != _export_json(value)
                    for field, value in expected_projections.items()
                )
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= 0
                or not isinstance(outputs.get("review_status"), str)
                or outputs.get("review_status") not in {"accepted", "rejected", "needs_review"}
                or not isinstance(semantic_qc, Mapping)
                or not isinstance(raw_descriptor, Mapping)
                or set(raw_descriptor) != {"path", "sha256"}
                or not isinstance(raw_descriptor.get("path"), str)
                or not str(raw_descriptor["path"]).strip()
            ):
                raise TissueRevisionStoreError(
                    "outer semantic projections conflict with domain finalization evidence"
                )
            _sha256(
                outputs.get("analysis_config_sha256"),
                "semantic analysis config SHA-256",
            )
            _sha256(
                raw_descriptor.get("sha256"),
                "semantic raw input SHA-256",
            )
            result_artifacts.append(artifact.to_mapping())
            count_decisions.append(_export_json(finalization.count_decision))
        raw_decisions = outputs.get("semantic_region_decisions", ())
        if isinstance(raw_decisions, Sequence) and not isinstance(raw_decisions, (str, bytes)):
            region_decisions.extend(_export_json(item) for item in raw_decisions)
    return {
        "result_artifacts": _export_json(result_artifacts),
        "finalizations": _export_json(finalizations),
        "region_decisions": _export_json(region_decisions),
        "count_decisions": _export_json(count_decisions),
    }


def _validate_semantic_review_projection(
    outputs: Mapping[str, object],
) -> None:
    """Bind terminal review resolution to this record's strict v4 config.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    raw_config = outputs.get("analysis_config")
    if not isinstance(raw_config, Mapping):
        raise TissueRevisionStoreError("semantic review policy lacks the accepted analysis config")
    try:
        parsed_config = AnalysisConfig.from_mapping(raw_config)
    except (TypeError, ValueError, ROIAnalysisError) as exc:
        raise TissueRevisionStoreError("semantic review policy analysis config is invalid") from exc
    parsed_mapping = parsed_config.to_mapping()
    declared_config_sha256 = _sha256(
        outputs.get("analysis_config_sha256"),
        "semantic analysis config SHA-256",
    )
    if (
        _export_json(raw_config) != _export_json(parsed_mapping)
        or canonical_json_sha256(parsed_mapping) != declared_config_sha256
    ):
        raise TissueRevisionStoreError("semantic review policy analysis config binding is stale")
    semantic_qc = outputs.get("semantic_qc")
    semantic_config_sha256 = canonical_json_sha256(parsed_config.semantic.to_mapping())
    if (
        not isinstance(semantic_qc, Mapping)
        or semantic_qc.get("semantic_config_sha256") != semantic_config_sha256
    ):
        raise TissueRevisionStoreError(
            "semantic review policy conflicts with semantic config evidence"
        )
    if "review_action" not in outputs:
        raise TissueRevisionStoreError("semantic review action is missing from completed evidence")
    action = outputs["review_action"]
    raw_decisions = outputs.get("semantic_region_decisions")
    if raw_decisions is not None:
        from mif_pipeline.roi_validation.evidence import SemanticRegionDecision

        if isinstance(raw_decisions, (str, bytes)) or not isinstance(raw_decisions, Sequence):
            raise TissueRevisionStoreError("semantic region decisions must be an array")
        try:
            decisions = tuple(SemanticRegionDecision.from_mapping(item) for item in raw_decisions)
        except (TypeError, ValueError) as exc:
            raise TissueRevisionStoreError("semantic region decision evidence is invalid") from exc
        if any(item.schema_version == "semantic-region-decision/v2" for item in decisions) or (
            not decisions and action is None and semantic_qc.get("result_count") == 0
        ):
            evaluated_ids = semantic_qc.get("evaluated_roi_ids")
            reviewed_ids = tuple(item.candidate_roi_id for item in decisions)
            if (
                action is not None
                or outputs.get("review_status") != "accepted"
                or any(item.schema_version != "semantic-region-decision/v2" for item in decisions)
                or any(item.analysis_domain_id != outputs.get("analysis_domain_id")
                       or item.section_id != outputs.get("section_id") for item in decisions)
                or isinstance(evaluated_ids, (str, bytes))
                or not isinstance(evaluated_ids, Sequence)
                or any(not isinstance(item, str) for item in evaluated_ids)
                or len(set(reviewed_ids)) != len(reviewed_ids)
                or len(set(evaluated_ids)) != len(evaluated_ids)
                or set(reviewed_ids) != set(evaluated_ids)
                or semantic_qc.get("result_count") != len(reviewed_ids)
            ):
                raise TissueRevisionStoreError("completed semantic region review coverage differs")
            return
    if action is None:
        policy = parsed_config.semantic.roi_review_policy
        if policy is ReviewPolicy.ALWAYS:
            raise TissueRevisionStoreError(
                "semantic review policy always requires an explicit review action"
            )
        if policy is ReviewPolicy.IF_UNVALIDATED:
            automatically_resolved = outputs.get("validation_status") == "validated"
        elif policy is ReviewPolicy.IF_QC_FAILED:
            automatically_resolved = outputs.get("qc_passed") is True
        else:
            evaluated_ids = semantic_qc.get("evaluated_roi_ids")
            automatically_resolved = (
                outputs.get("validation_status") == "validated"
                and outputs.get("qc_passed") is True
                and semantic_qc.get("status") == "passed"
                and isinstance(evaluated_ids, Sequence)
                and not isinstance(evaluated_ids, (str, bytes))
                and all(isinstance(item, str) for item in evaluated_ids)
                and semantic_qc.get("result_count") == len(evaluated_ids)
                and len(set(evaluated_ids)) == len(evaluated_ids)
            )
        if not automatically_resolved:
            raise TissueRevisionStoreError(
                "semantic review policy did not automatically resolve acceptance"
            )
        expected_status = "accepted"
    elif action == "accept":
        expected_status = "accepted"
    elif action == "reject":
        expected_status = "rejected"
    else:
        raise TissueRevisionStoreError("semantic review action is outside its closed domain")
    if outputs.get("review_status") != expected_status:
        raise TissueRevisionStoreError("semantic review action conflicts with review status")


@dataclass(frozen=True, slots=True)
class TissueRevisionRecord:
    """One immutable geometry/review event in a tissue PathObject's history."""

    provenance_record_id: str
    slide_id: str
    section_id: str
    roi_id: str
    generation_id: str
    generation_status: GenerationStatus
    revision: int
    event_type: str
    geometry: Mapping[str, Any]
    detector_context: Mapping[str, Any]
    parent_record_id: str | None = None
    review_action: str | None = None
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the TissueRevisionRecord fields when the record is constructed.

        The checks cover ``event_type``, ``parent_record_id``, ``supersedes``, ``revision``,
        ``generation_status``, ``geometry``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        for field in ("provenance_record_id", "slide_id", "section_id", "roi_id", "generation_id"):
            _nonblank(getattr(self, field), field)
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise TissueRevisionStoreError("revision must be a non-negative integer")
        if not isinstance(self.generation_status, GenerationStatus):
            raise TissueRevisionStoreError("generation_status must be a GenerationStatus")
        if self.event_type not in EVENT_TYPES:
            raise TissueRevisionStoreError("event_type is invalid")
        if not isinstance(self.geometry, Mapping) or not isinstance(self.detector_context, Mapping):
            raise TissueRevisionStoreError("geometry and detector_context must be mappings")
        object.__setattr__(self, "geometry", _freeze_json(self.geometry, "geometry"))
        object.__setattr__(
            self, "detector_context", _freeze_json(self.detector_context, "detector_context")
        )
        if self.parent_record_id is not None:
            _nonblank(self.parent_record_id, "parent_record_id")
        if self.event_type == "review_action":
            if self.review_action not in TISSUE_REVISION_ACTIONS:
                raise TissueRevisionStoreError("review_action is required for a review event")
        elif self.review_action is not None:
            raise TissueRevisionStoreError("review_action is only valid for a review event")
        if self.event_type in {"automatic_detection", "manual_edit"}:
            if self.generation_status is not GenerationStatus.CANDIDATE:
                raise TissueRevisionStoreError(
                    f"{self.event_type} must record candidate generation status"
                )
        elif self.review_action == "accept":
            if self.generation_status is not GenerationStatus.ACCEPTED:
                raise TissueRevisionStoreError(
                    "accept review must record accepted generation status"
                )
        elif self.review_action == "reject":
            if self.generation_status is not GenerationStatus.REJECTED:
                raise TissueRevisionStoreError(
                    "reject review must record rejected generation status"
                )
        elif self.review_action == "edit":
            if self.generation_status is not GenerationStatus.CANDIDATE:
                raise TissueRevisionStoreError(
                    "edit review must record candidate generation status"
                )
        for field in ("derived_from", "supersedes"):
            values = tuple(_nonblank(item, field) for item in getattr(self, field))
            if len(set(values)) != len(values) or self.roi_id in values:
                raise TissueRevisionStoreError(f"{field} must contain unique non-self ROI IDs")
            object.__setattr__(self, field, values)
        if self.supersedes and not (
            self.event_type == "review_action"
            and self.review_action == "accept"
            and self.generation_status is GenerationStatus.ACCEPTED
        ):
            raise TissueRevisionStoreError("supersedes becomes effective only on acceptance")

    def to_mapping(self) -> dict[str, object]:
        return {
            "provenance_record_id": self.provenance_record_id,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "roi_id": self.roi_id,
            "generation_id": self.generation_id,
            "generation_status": self.generation_status.value,
            "revision": self.revision,
            "event_type": self.event_type,
            "geometry": _export_json(self.geometry),
            "detector_context": _export_json(self.detector_context),
            "parent_record_id": self.parent_record_id,
            "review_action": self.review_action,
            "derived_from": list(self.derived_from),
            "supersedes": list(self.supersedes),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TissueRevisionRecord":
        if not isinstance(value, Mapping):
            raise TissueRevisionStoreError("tissue revision must be an object")
        required = {
            "provenance_record_id",
            "slide_id",
            "section_id",
            "roi_id",
            "generation_id",
            "generation_status",
            "revision",
            "event_type",
            "geometry",
            "detector_context",
            "parent_record_id",
            "review_action",
            "derived_from",
            "supersedes",
        }
        if set(value) != required:
            raise TissueRevisionStoreError("tissue revision fields do not match the schema")
        try:
            return cls(
                provenance_record_id=value["provenance_record_id"],
                slide_id=value["slide_id"],
                section_id=value["section_id"],
                roi_id=value["roi_id"],
                generation_id=value["generation_id"],
                generation_status=GenerationStatus(value["generation_status"]),
                revision=value["revision"],
                event_type=value["event_type"],
                geometry=value["geometry"],
                detector_context=value["detector_context"],
                parent_record_id=value["parent_record_id"],
                review_action=value["review_action"],
                derived_from=tuple(value["derived_from"]),
                supersedes=tuple(value["supersedes"]),
            )
        except (KeyError, TypeError, ROIAnalysisError) as exc:
            raise TissueRevisionStoreError("invalid tissue revision record") from exc


@dataclass(frozen=True, slots=True)
class _StoredRevision:
    sequence: int
    record: TissueRevisionRecord
    path: Path


class TissueRevisionStore:
    """Persist each tissue event as a separately immutable atomic record."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.records_dir = self.root / "tissue_revisions"
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def append(self, record: TissueRevisionRecord) -> TissueRevisionRecord:
        if not isinstance(record, TissueRevisionRecord):
            raise TissueRevisionStoreError("record must be a TissueRevisionRecord")
        entries = self._entries()
        if any(item.record.provenance_record_id == record.provenance_record_id for item in entries):
            raise TissueRevisionStoreError(
                f"provenance record already exists: {record.provenance_record_id}"
            )
        self._validate_append(record, entries)
        sequence = max((item.sequence for item in entries), default=0) + 1
        path = (
            self.records_dir
            / f"{sequence:012d}-{_safe_segment(record.provenance_record_id, 'provenance_record_id')}.json"
        )
        record_mapping = record.to_mapping()
        _atomic_write_json(
            path,
            {
                "schema_version": REVISION_SCHEMA,
                "sequence": sequence,
                "record": record_mapping,
                "record_sha256": _content_digest(record_mapping),
            },
        )
        return record

    def records(self, slide_id: str, roi_id: str) -> tuple[TissueRevisionRecord, ...]:
        return tuple(
            item.record
            for item in self._entries()
            if item.record.slide_id == slide_id and item.record.roi_id == roi_id
        )

    def all_records(self, slide_id: str | None = None) -> tuple[TissueRevisionRecord, ...]:
        """Return the append order needed for a complete geometry audit export."""
        if slide_id is not None:
            _nonblank(slide_id, "slide_id")
        return tuple(
            item.record
            for item in self._entries()
            if slide_id is None or item.record.slide_id == slide_id
        )

    def current_qupath_pointer(self, slide_id: str, roi_id: str) -> dict[str, str]:
        records = self.records(slide_id, roi_id)
        if not records:
            raise TissueRevisionStoreError("no tissue revision exists for this slide and ROI")
        return {"provenance_record_id": records[-1].provenance_record_id}

    def _entries(self) -> tuple[_StoredRevision, ...]:
        entries: list[_StoredRevision] = []
        for path in self.records_dir.glob("*.json"):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(envelope, Mapping)
                    or set(envelope) != {"schema_version", "sequence", "record", "record_sha256"}
                    or envelope["schema_version"] != REVISION_SCHEMA
                    or isinstance(envelope["sequence"], bool)
                    or not isinstance(envelope["sequence"], int)
                    or envelope["sequence"] <= 0
                ):
                    raise ValueError("invalid envelope")
                if envelope["record_sha256"] != _content_digest(envelope["record"]):
                    raise TissueRevisionStoreError(
                        f"tissue revision checksum does not match: {path}"
                    )
                entries.append(
                    _StoredRevision(
                        sequence=envelope["sequence"],
                        record=TissueRevisionRecord.from_mapping(envelope["record"]),
                        path=path,
                    )
                )
            except TissueRevisionStoreError:
                raise
            except (OSError, json.JSONDecodeError, ValueError, ROIAnalysisError) as exc:
                raise TissueRevisionStoreError(f"invalid tissue revision file: {path}") from exc
        entries.sort(key=lambda item: item.sequence)
        if len({item.sequence for item in entries}) != len(entries):
            raise TissueRevisionStoreError("duplicate tissue revision sequence")
        return tuple(entries)

    def _validate_append(
        self,
        record: TissueRevisionRecord,
        entries: tuple[_StoredRevision, ...],
    ) -> None:
        by_record_id = {item.record.provenance_record_id: item.record for item in entries}
        same_roi = [item.record for item in entries if item.record.roi_id == record.roi_id]
        current = same_roi[-1] if same_roi else None
        if current is None:
            if record.parent_record_id is not None:
                raise TissueRevisionStoreError("initial tissue revision cannot name a parent")
            if record.revision != 0 or record.event_type != "automatic_detection":
                raise TissueRevisionStoreError(
                    "initial tissue geometry must be automatic revision zero"
                )
        else:
            if record.parent_record_id is None or record.parent_record_id not in by_record_id:
                raise TissueRevisionStoreError("tissue revision is missing its parent revision")
            parent = by_record_id[record.parent_record_id]
            if parent != current:
                raise TissueRevisionStoreError("tissue revision parent must be the latest revision")
            if (parent.slide_id, parent.section_id, parent.roi_id) != (
                record.slide_id,
                record.section_id,
                record.roi_id,
            ):
                raise TissueRevisionStoreError("tissue revision must preserve biological identity")
            if (
                record.event_type in {"manual_edit", "review_action"}
                and record.detector_context != parent.detector_context
            ):
                raise TissueRevisionStoreError(
                    "manual edits and reviews must preserve parent detector context"
                )
            if record.event_type == "manual_edit":
                if record.generation_id != parent.generation_id:
                    raise TissueRevisionStoreError(
                        "manual edit must preserve its generation identity"
                    )
                if record.revision != parent.revision + 1:
                    raise TissueRevisionStoreError("manual edit must create the next revision")
            elif record.event_type == "review_action":
                if record.generation_id != parent.generation_id:
                    raise TissueRevisionStoreError(
                        "review action must preserve its generation identity"
                    )
                if record.revision != parent.revision or record.geometry != parent.geometry:
                    raise TissueRevisionStoreError(
                        "review action must reference the current geometry revision"
                    )
                if (
                    record.review_action == "repeat"
                    and record.generation_status is not parent.generation_status
                ):
                    raise TissueRevisionStoreError(
                        "repeat review must preserve the current generation status"
                    )
            else:
                expected = parent.revision + (record.geometry != parent.geometry)
                if record.revision != expected:
                    raise TissueRevisionStoreError(
                        "automatic geometry must preserve or increment revision"
                    )
        for lineage_roi in (*record.derived_from, *record.supersedes):
            lineage = [item.record for item in entries if item.record.roi_id == lineage_roi]
            if not lineage:
                raise TissueRevisionStoreError(f"unknown lineage ROI: {lineage_roi}")
            same_slide = [item for item in lineage if item.slide_id == record.slide_id]
            if not same_slide:
                raise TissueRevisionStoreError("tissue lineage cannot cross slides")
            if any(item.generation_id == record.generation_id for item in same_slide):
                raise TissueRevisionStoreError("tissue lineage must reference a prior generation")
            accepted_evidence = any(
                item.generation_status
                in {
                    GenerationStatus.ACCEPTED,
                    GenerationStatus.SUPERSEDED,
                }
                for item in same_slide
            )
            historical_supersession = any(
                lineage_roi in item.record.supersedes
                and item.record.slide_id == record.slide_id
                and item.record.generation_status is GenerationStatus.ACCEPTED
                for item in entries
            )
            if not accepted_evidence and not historical_supersession:
                raise TissueRevisionStoreError(
                    "tissue lineage requires prior authoritative accepted or "
                    "historical superseded generation evidence"
                )


def _output_artifact(value: object) -> OutputArtifact:
    if isinstance(value, OutputArtifact):
        artifact = value
    else:
        try:
            artifact = OutputArtifact.from_mapping(value)
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError(
                "output inventory requires a valid lowercase SHA-256"
            ) from exc
    try:
        artifact.validate_file()
    except ROIAnalysisError as exc:
        raise TissueRevisionStoreError(
            f"output inventory validation failed: {artifact.path}"
        ) from exc
    return artifact


def _completed_stage_outputs(
    provenance: AnalysisProvenance,
    stage: str,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        result["outputs"]
        for result in provenance.stage_results
        if result.get("stage") == stage
        and result.get("status") == "completed"
        and isinstance(result.get("outputs"), Mapping)
    )


def _validate_semantic_result_artifact_file(
    artifact: SemanticResultArtifactEvidence,
    finalization: FinalizedSecondaryDomainEvidence,
) -> tuple[Mapping[str, object], ...]:
    """Validate semantic result artifact file for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    path = Path(artifact.path).expanduser().resolve(strict=False)
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TissueRevisionStoreError(
            "semantic result artifact is unreadable or invalid JSON"
        ) from exc
    rows = payload.get("semantic_results") if isinstance(payload, Mapping) else None
    required_row_fields = {
        "schema_version",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "tissue_generation_id",
        "pipeline_run_id",
        "roi_id",
        "path_object_id",
        "structure_id",
        "parent_roi_id",
        "semantic_profile",
        "semantic_status",
        "semantic_rule_results",
        "review_status",
        "profile_id",
        "profile_version",
        "profile_content_sha256",
        "evidence_snapshot_sha256",
        "geometry_sha256",
        "semantic_classification_status",
        "semantic_class_id",
        "semantic_classification_rule_results",
    }
    receipt = finalization.finalization_receipt
    if (
        hashlib.sha256(content).hexdigest() != artifact.sha256
        or not isinstance(payload, Mapping)
        or set(payload)
        != {
            "schema_version",
            "semantic_result_set_sha256",
            "semantic_results",
        }
        or payload.get("schema_version") != "semantic-result-set/v1"
        or payload.get("semantic_result_set_sha256") != artifact.semantic_result_set_sha256
        or isinstance(rows, (str, bytes))
        or not isinstance(rows, Sequence)
        or not all(isinstance(item, Mapping) for item in rows)
        or len(rows) != artifact.record_count
        or any(
            set(row) != required_row_fields | ({"region_decision", "measurement_results"} if row.get("schema_version") == "semantic-result/v3" else set())
            or row.get("schema_version") not in {"semantic-result/v2", "semantic-result/v3"}
            or row.get("analysis_domain_id") != finalization.analysis_domain_id
            or row.get("section_id") != receipt["section_id"]
            or row.get("tissue_generation_id") != receipt["tissue_generation_id"]
            or row.get("pipeline_run_id") != receipt["pipeline_run_id"]
            or any(
                not isinstance(row.get(field), str) or not row[field].strip()
                for field in (
                    "slide_id",
                    "roi_id",
                    "path_object_id",
                    "structure_id",
                    "parent_roi_id",
                    "semantic_profile",
                    "profile_id",
                    "profile_version",
                )
            )
            or any(
                not isinstance(row.get(field), str)
                or len(row[field]) != 64
                or any(character not in "0123456789abcdef" for character in row[field])
                for field in (
                    "profile_content_sha256",
                    "evidence_snapshot_sha256",
                    "geometry_sha256",
                )
            )
            for row in rows
        )
        or len({row["roi_id"] for row in rows}) != len(rows)
        or canonical_json_sha256(
            {
                "schema_version": "semantic-result-set/v1",
                "semantic_results": rows,
            }
        )
        != artifact.semantic_result_set_sha256
    ):
        raise TissueRevisionStoreError(
            "semantic result artifact bytes conflict with its descriptor"
        )
    row_ids = {str(row["roi_id"]) for row in rows}
    external_primary_ids = tuple(
        sorted(
            {
                str(row["parent_roi_id"])
                for row in rows
                if row["parent_roi_id"] != finalization.analysis_domain_id
                and row["parent_roi_id"] not in row_ids
            }
        )
    )
    try:
        from mif_pipeline.orchestration.workflow import (
            validate_semantic_result_graph,
        )

        rows = validate_semantic_result_graph(
            rows,
            analysis_domain_id=finalization.analysis_domain_id,
            analysis_domain_parent_id=finalization.analysis_domain_id,
            accepted_primary_roi_ids=external_primary_ids,
        )
    except ROIAnalysisError as exc:
        raise TissueRevisionStoreError(
            "semantic result artifact contains an invalid result row"
        ) from exc
    dispositions = {item.roi_id: item for item in finalization.dispositions}
    accepted_roi_ids: set[str] = set()
    seen_path_object_ids: set[str] = set()
    for row in rows:
        roi_id = str(row["roi_id"])
        path_object_id = str(row["path_object_id"])
        if path_object_id in seen_path_object_ids:
            raise TissueRevisionStoreError(
                "semantic result identity duplicates a source path object"
            )
        seen_path_object_ids.add(path_object_id)
        parent_content_key = semantic_parent_content_key(
            analysis_domain_id=finalization.analysis_domain_id,
            parent_roi_id=str(row["parent_roi_id"]),
        )
        content_key = semantic_secondary_content_key(
            analysis_domain_id=finalization.analysis_domain_id,
            roi_id=roi_id,
            path_object_id=path_object_id,
            structure_id=str(row["structure_id"]),
            parent_content_key=parent_content_key,
            geometry_sha256=str(row["geometry_sha256"]),
        )
        disposition = dispositions.get(roi_id)
        accepted = (
            row.get("semantic_status") == "accepted" and row.get("review_status") == "accepted"
        )
        if accepted:
            accepted_roi_ids.add(roi_id)
            if (
                disposition is None
                or disposition.path_object_id != path_object_id
                or disposition.content_key != content_key
                or disposition.structure_id != row["structure_id"]
                or disposition.parent_content_key != parent_content_key
                or disposition.geometry_sha256 != row["geometry_sha256"]
            ):
                raise TissueRevisionStoreError(
                    "semantic result identity conflicts with count disposition"
                )
        elif disposition is not None:
            raise TissueRevisionStoreError(
                "semantic audit result unexpectedly has a count disposition"
            )
    if accepted_roi_ids != set(dispositions):
        raise TissueRevisionStoreError(
            "accepted semantic result identity is not a disposition bijection"
        )
    return tuple(rows)


def _validate_manual_selection_artifact_file(
    evidence: ManualSelectionEvidence,
    finalization: FinalizedSecondaryDomainEvidence,
    *,
    expected_analysis_domain_revision: int | None = None,
) -> None:
    """Validate manual selection artifact file for the current operation.

    The required mapping shape includes ``schema_version``, ``slide_id``, ``section_id``,
    ``analysis_domain_id``, ``analysis_domain_revision``,
    ``analysis_domain_revision_sha256``. Field checks are applied before the validated value
    is returned or passed to its next constructor."""

    descriptor = evidence.inventory_descriptor
    path = Path(str(descriptor["path"])).expanduser().resolve(strict=False)
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TissueRevisionStoreError(
            "manual selection inventory artifact is unreadable or invalid JSON"
        ) from exc
    required = {
        "schema_version",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "tissue_generation_id",
        "pipeline_run_id",
        "config_sha256",
        "semantic_result_set_sha256",
        "structure_id",
        "maximum",
        "observed_count",
        "inventory_sha256",
        "selection_algorithm",
        "items",
    }
    items = payload.get("items") if isinstance(payload, Mapping) else None
    lineage = evidence.selection_lineage
    if (
        hashlib.sha256(content).hexdigest() != descriptor["sha256"]
        or not isinstance(payload, Mapping)
        or set(payload) != required
        or payload.get("schema_version") != "structure-count-inventory/v1"
        or payload.get("structure_id") != evidence.structure_id
        or payload.get("selection_algorithm") != "manual-select/v1"
        or isinstance(items, (str, bytes))
        or not isinstance(items, Sequence)
        or not all(
            isinstance(item, Mapping)
            and set(item)
            == {
                "content_key",
                "roi_id",
                "parent_content_key",
                "geometry_sha256",
            }
            for item in items
        )
        or payload.get("observed_count") != len(items)
        or descriptor["record_count"] != len(items)
        or payload.get("analysis_domain_revision_sha256") != lineage["analysis_domain_sha256"]
        or payload.get("analysis_domain_id") != finalization.analysis_domain_id
        or payload.get("analysis_domain_revision_sha256")
        != finalization.analysis_domain_revision_sha256
        or payload.get("section_id") != finalization.finalization_receipt["section_id"]
        or payload.get("tissue_generation_id")
        != finalization.finalization_receipt["tissue_generation_id"]
        or payload.get("pipeline_run_id") != finalization.finalization_receipt["pipeline_run_id"]
        or payload.get("config_sha256") != lineage["config_sha256"]
        or payload.get("semantic_result_set_sha256") != lineage["result_sha256"]
        or payload.get("inventory_sha256") != lineage["inventory_sha256"]
        or canonical_json_sha256(
            {
                "schema_version": "object-inventory/v1",
                "items": [
                    {
                        "content_key": item["content_key"],
                        "geometry_sha256": item["geometry_sha256"],
                        "parent_content_key": item["parent_content_key"],
                        "structure_id": evidence.structure_id,
                    }
                    for item in items or ()
                ],
            }
        )
        != payload.get("inventory_sha256")
    ):
        raise TissueRevisionStoreError(
            "manual selection inventory artifact bytes conflict with its descriptor"
        )
    matching_outputs = tuple(
        output
        for output in finalization.count_decision["outputs"]
        if output["output_id"] == evidence.structure_id
    )
    if len(matching_outputs) != 1:
        raise TissueRevisionStoreError("manual selection inventory has no unique count output")
    dispositions = {item.content_key: item for item in finalization.dispositions}
    inventory_content_keys = tuple(
        str(item["content_key"]) for item in matching_outputs[0]["inventory"]
    )
    if any(content_key not in dispositions for content_key in inventory_content_keys):
        raise TissueRevisionStoreError(
            "manual selection inventory has no matching final disposition"
        )
    matching_output = matching_outputs[0]
    count_domain = finalization.count_decision.get("domain")
    if (
        not isinstance(count_domain, Mapping)
        or payload.get("slide_id") != count_domain.get("slide_id")
        or payload.get("maximum") != matching_output.get("maximum")
        or payload.get("observed_count") != matching_output.get("observed_count")
        or payload.get("inventory_sha256") != matching_output.get("inventory_sha256")
        or (
            expected_analysis_domain_revision is not None
            and payload.get("analysis_domain_revision") != expected_analysis_domain_revision
        )
    ):
        raise TissueRevisionStoreError(
            "manual selection artifact conflicts with count/domain scope"
        )
    expected_items = [
        {
            "content_key": item["content_key"],
            "roi_id": dispositions[item["content_key"]].roi_id,
            "parent_content_key": item["parent_content_key"],
            "geometry_sha256": item["geometry_sha256"],
        }
        for item in matching_output["inventory"]
    ]
    if _export_json(items) != _export_json(expected_items):
        raise TissueRevisionStoreError(
            "manual selection items conflict with count/disposition identity"
        )


def _checksummed_raw_descriptor(value: object, *, label: str) -> tuple[Path, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise TissueRevisionStoreError(f"{label} fields are invalid")
    path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path, str) or not path.strip():
        raise TissueRevisionStoreError(f"{label} path is invalid")
    _sha256(digest, f"{label} SHA-256")
    resolved = Path(path).expanduser().resolve(strict=False)
    try:
        actual = _file_sha256(resolved)
    except OSError as exc:
        raise TissueRevisionStoreError(f"{label} is unreadable") from exc
    if actual != digest:
        raise TissueRevisionStoreError(f"{label} bytes conflict with descriptor")
    return resolved, str(digest)


def _validate_current_domain_selection(
    provenance: AnalysisProvenance,
    handoff: object,
) -> None:
    """Validate current domain selection for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from mif_pipeline.analysis_domain import validate_domain_manifest
    from mif_pipeline.orchestration.workflow import (
        _domain_manifest_from_mapping,
        _domain_overlap_from_mapping,
    )

    selections = _completed_stage_outputs(provenance, "analysis_domain_selection")
    if not selections:
        raise TissueRevisionStoreError("phenotype handoff lacks current accepted domain selection")
    current = selections[-1]
    manifests = current.get("analysis_domain_manifests")
    raw_overlap = current.get("overlap_evidence")
    if (
        current.get("schema_version")
        not in {
            "analysis-domain-selection/v1",
            "analysis-domain-candidate-manifest/v1",
        }
        or current.get("selection_status") != "accepted"
        or current.get("selected_annotations_locked") is not True
        or current.get("saved_closed_acknowledged") is not True
        or current.get("canonical_image_id") != handoff.spatial_frame.canonical_image_id
        or current.get("canonical_image_sha256") != handoff.spatial_frame.canonical_image_sha256
        or current.get("domain_set_sha256") != handoff.domain_set.domain_set_sha256
        or isinstance(manifests, (str, bytes))
        or not isinstance(manifests, Sequence)
        or isinstance(raw_overlap, (str, bytes))
        or not isinstance(raw_overlap, Sequence)
    ):
        raise TissueRevisionStoreError("current analysis-domain selection conflicts with handoff")
    try:
        parsed_manifests = tuple(_domain_manifest_from_mapping(item) for item in manifests)
        parsed_overlap = tuple(_domain_overlap_from_mapping(item) for item in raw_overlap)
    except (TypeError, ValueError, ROIAnalysisError) as exc:
        raise TissueRevisionStoreError(
            "current analysis-domain manifest/revision evidence is invalid"
        ) from exc

    overlap_by_key: dict[tuple[str, tuple[str, ...]], object] = {}
    for evidence in parsed_overlap:
        key = (
            evidence.section_id,
            tuple(sorted(item.path_object_id for item in evidence.geometry_evidence)),
        )
        if key in overlap_by_key:
            raise TissueRevisionStoreError("current analysis-domain overlap evidence is ambiguous")
        overlap_by_key[key] = evidence

    actual: dict[str, tuple[object, object]] = {}
    manifest_keys: set[tuple[str, tuple[str, ...]]] = set()
    for manifest in parsed_manifests:
        if not manifest.revisions:
            raise TissueRevisionStoreError("current analysis-domain manifest history is empty")
        lineage_ids = {revision.ref.analysis_domain_id for revision in manifest.revisions}
        source_ids = {revision.source_path_object_id for revision in manifest.revisions}
        if len(lineage_ids) != 1 or len(source_ids) != 1:
            raise TissueRevisionStoreError("current analysis-domain manifest lineage is ambiguous")
        latest = manifest.revisions[-1]
        key = (
            manifest.section_id,
            tuple(sorted(source_ids)) if latest.ref.kind.value == "selected_roi" else (),
        )
        evidence = overlap_by_key.get(key)
        if evidence is None or key in manifest_keys:
            raise TissueRevisionStoreError(
                "current analysis-domain manifest lacks unique overlap evidence"
            )
        try:
            validate_domain_manifest(manifest, evidence)
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError(
                "current analysis-domain manifest/revision evidence is invalid"
            ) from exc
        manifest_keys.add(key)
        domain_id = latest.ref.analysis_domain_id
        if domain_id in actual:
            raise TissueRevisionStoreError("current analysis-domain manifest identity is ambiguous")
        actual[domain_id] = (latest, manifest)
    if set(overlap_by_key) != manifest_keys:
        raise TissueRevisionStoreError("current analysis-domain overlap evidence is not exact")
    expected = {member.analysis_domain_id: member for member in handoff.domain_set.members}
    if set(actual) != set(expected):
        raise TissueRevisionStoreError("current analysis-domain selection is incomplete")
    for domain_id, member in expected.items():
        revision, manifest = actual[domain_id]
        if (
            manifest.content_sha256 != member.analysis_domain_manifest_sha256
            or manifest.canonical_image_id != handoff.spatial_frame.canonical_image_id
            or manifest.canonical_image_sha256 != handoff.spatial_frame.canonical_image_sha256
            or manifest.slide_id != member.slide_id
            or manifest.section_id != member.section_id
            or manifest.tissue_generation_id != member.tissue_generation_id
            or manifest.tissue_roi_id != member.tissue_roi_id
            or revision.ref.analysis_domain_id != member.analysis_domain_id
            or revision.ref.kind is not member.analysis_domain_kind
            or revision.ref.slide_id != member.slide_id
            or revision.ref.section_id != member.section_id
            or revision.ref.tissue_generation_id != member.tissue_generation_id
            or revision.tissue_roi_id != member.tissue_roi_id
            or revision.revision != member.analysis_domain_revision
            or revision.content_sha256 != member.analysis_domain_revision_sha256
        ):
            raise TissueRevisionStoreError(
                "current analysis-domain manifest/revision conflicts with handoff"
            )
    manifest_set_sha256 = canonical_json_sha256(
        {
            "schema_version": "analysis-domain-set/v1",
            "analysis_domain_manifest_sha256s": sorted(
                manifest.content_sha256 for manifest in parsed_manifests
            ),
        }
    )
    tissue_generation_ids = {member.tissue_generation_id for member in expected.values()}
    if manifest_set_sha256 != handoff.domain_set.domain_set_sha256 or tissue_generation_ids != {
        current.get("tissue_generation_id")
    }:
        raise TissueRevisionStoreError(
            "current analysis-domain selection scope/set conflicts with handoff"
        )


def _validate_semantic_identity_chain(
    handoff: object,
    result: object,
    rows: Sequence[Mapping[str, object]],
    candidate_authority: Mapping[str, object],
) -> None:
    finalization = result.finalization
    if not isinstance(finalization, FinalizedSecondaryDomainEvidence):
        return
    dispositions = {item.roi_id: item for item in finalization.dispositions}
    raw_candidates = candidate_authority.get("candidates")
    if not isinstance(raw_candidates, Mapping) or not all(
        isinstance(candidate_id, str) and isinstance(identity, Mapping)
        for candidate_id, identity in raw_candidates.items()
    ):
        raise TissueRevisionStoreError("semantic candidate membership authority is invalid")
    authority_ids = set(raw_candidates)
    row_ids = {str(row["roi_id"]) for row in rows}
    if row_ids != authority_ids:
        raise TissueRevisionStoreError(
            "semantic candidate membership and result rows are not a bijection"
        )
    domain_secondary_states = {
        state.record.roi_id: state
        for state in handoff.final_roi_states.values()
        if state.pipeline_stage == "secondary_classifier"
        and state.record.analysis_domain_id == result.member.analysis_domain_id
    }
    if any(
        candidate_id not in domain_secondary_states
        or domain_secondary_states[candidate_id].candidate_status != "candidate"
        for candidate_id in authority_ids
    ) or any(
        state.candidate_status != "other"
        for roi_id, state in domain_secondary_states.items()
        if roi_id not in authority_ids
    ):
        raise TissueRevisionStoreError(
            "semantic candidate membership conflicts with final ROI candidate status"
        )
    accepted: set[str] = set()
    seen_paths: set[str] = set()
    for row in rows:
        roi_id = str(row["roi_id"])
        path_object_id = str(row["path_object_id"])
        candidate_identity = raw_candidates[roi_id]
        if path_object_id in seen_paths:
            raise TissueRevisionStoreError("semantic result source path-object IDs are duplicated")
        seen_paths.add(path_object_id)
        parent_key = semantic_parent_content_key(
            analysis_domain_id=result.member.analysis_domain_id,
            parent_roi_id=str(row["parent_roi_id"]),
        )
        content_key = semantic_secondary_content_key(
            analysis_domain_id=result.member.analysis_domain_id,
            roi_id=roi_id,
            path_object_id=path_object_id,
            structure_id=str(row["structure_id"]),
            parent_content_key=parent_key,
            geometry_sha256=str(row["geometry_sha256"]),
        )
        state = handoff.final_roi_states.get(roi_id)
        if (
            state is None
            or state.pipeline_stage != "secondary_classifier"
            or state.candidate_status != "candidate"
            or candidate_identity.get("path_object_id") != path_object_id
            or candidate_identity.get("geometry_sha256") != row["geometry_sha256"]
            or row["analysis_domain_id"] != result.member.analysis_domain_id
            or row["slide_id"] != result.member.slide_id
            or row["section_id"] != result.member.section_id
            or row["tissue_generation_id"] != result.member.tissue_generation_id
            or row["pipeline_run_id"] != finalization.finalization_receipt["pipeline_run_id"]
            or state.record.roi_id != roi_id
            or state.record.analysis_domain_id != row["analysis_domain_id"]
            or state.record.slide_id != row["slide_id"]
            or state.record.section_id != row["section_id"]
            or state.generation_id != row["tissue_generation_id"]
            or state.pipeline_run_id != row["pipeline_run_id"]
            or state.path_object_id != path_object_id
            or state.structure_id != row["structure_id"]
            or state.geometry_sha256 != row["geometry_sha256"]
            or state.content_key != content_key
            or state.record.parent_roi_id != row["parent_roi_id"]
            or state.record.semantic_profile != row["semantic_profile"]
            or state.record.semantic_status.value != row["semantic_status"]
            or _export_json(state.record.semantic_rule_results)
            != _export_json(row["semantic_rule_results"])
            or state.record.review_status.value != row["review_status"]
            or (row.get("schema_version") == "semantic-result/v3" and (
                state.record.semantic_classification_status != row["semantic_classification_status"]
                or state.record.semantic_class_id != row["semantic_class_id"]
                or state.record.review_rationale != row["region_decision"].get("rationale")
            ))
        ):
            raise TissueRevisionStoreError("semantic result row conflicts with final ROI identity")
        is_accepted = (
            row.get("semantic_status") == "accepted" and row.get("review_status") == "accepted"
        )
        disposition = dispositions.get(roi_id)
        if is_accepted:
            accepted.add(roi_id)
            if (
                disposition is None
                or disposition.path_object_id != path_object_id
                or disposition.content_key != content_key
                or disposition.parent_content_key != parent_key
                or disposition.structure_id != row["structure_id"]
                or disposition.geometry_sha256 != row["geometry_sha256"]
            ):
                raise TissueRevisionStoreError(
                    "semantic result row conflicts with count disposition identity"
                )
        elif disposition is not None:
            raise TissueRevisionStoreError(
                "rejected/indeterminate semantic result has a count disposition"
            )
    if accepted != set(dispositions):
        raise TissueRevisionStoreError("accepted semantic results are not a disposition bijection")


def _validate_semantic_artifact_files(
    provenance: AnalysisProvenance,
) -> None:
    """Validate semantic artifact files for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    for output in _completed_stage_outputs(provenance, "semantic_validation"):
        raw = output.get("domain_finalization")
        if raw is None and output.get("kind") == "secondary_finalized":
            raw = output
        if not isinstance(raw, Mapping) or raw.get("kind") != "secondary_finalized":
            continue
        try:
            finalization = FinalizedSecondaryDomainEvidence.from_mapping(raw)
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError(
                "semantic domain finalization evidence is invalid"
            ) from exc
        rows = _validate_semantic_result_artifact_file(
            finalization.semantic_result_artifact,
            finalization,
        )
        semantic_qc = output.get("semantic_qc")
        expected_roi_ids = sorted(str(row["roi_id"]) for row in rows)
        review_statuses = {row["review_status"] for row in rows}
        if (
            output.get("validation_status") != "validated"
            or output.get("qc_passed") is not True
            or not isinstance(semantic_qc, Mapping)
            or set(semantic_qc)
            != {
                "status",
                "result_count",
                "evaluated_roi_ids",
                "semantic_config_sha256",
            }
            or semantic_qc.get("status") != "passed"
            or semantic_qc.get("result_count") != len(rows)
            or _export_json(semantic_qc.get("evaluated_roi_ids")) != expected_roi_ids
            or not isinstance(semantic_qc.get("semantic_config_sha256"), str)
            or len(str(semantic_qc["semantic_config_sha256"])) != 64
            or any(
                character not in "0123456789abcdef"
                for character in str(semantic_qc["semantic_config_sha256"])
            )
            or (output.get("review_status") != "accepted" if all(
                row.get("schema_version") == "semantic-result/v3" for row in rows
            ) else bool(review_statuses and review_statuses != {output.get("review_status")}))
        ):
            raise TissueRevisionStoreError(
                "outer semantic QC/review projections conflict with semantic results"
            )
        for evidence in finalization.manual_selections:
            _validate_manual_selection_artifact_file(evidence, finalization)


def _validate_completed_spatial_handoff_provenance(
    provenance: AnalysisProvenance,
    artifacts: Sequence[OutputArtifact],
) -> None:
    """Cross-check the one published spatial handoff against stage history.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    handoff_outputs = _completed_stage_outputs(provenance, "phenotype_handoff")
    spatial_artifacts = tuple(
        artifact for artifact in artifacts if artifact.schema_version == SPATIAL_HANDOFF_SCHEMA_VERSION
    )
    if not handoff_outputs:
        if spatial_artifacts:
            raise TissueRevisionStoreError(
                "output inventory has a spatial handoff without a completed stage"
            )
        return
    if len(spatial_artifacts) != 1:
        raise TissueRevisionStoreError(
            "output inventory must contain exactly one current handoff descriptor"
        )
    current_descriptor_mapping = spatial_artifacts[0].to_mapping()
    current_heads = tuple(
        output
        for output in handoff_outputs
        if _export_json(output.get("descriptor")) == current_descriptor_mapping
    )
    if len(current_heads) != 1:
        raise TissueRevisionStoreError(
            "completed phenotype handoff history must identify one explicit current head"
        )
    output = current_heads[0]
    try:
        descriptor = _output_artifact(output.get("descriptor"))
        cells_artifact = _output_artifact(output.get("cells"))
    except TissueRevisionStoreError as exc:
        raise TissueRevisionStoreError("phenotype handoff artifacts are invalid") from exc
    if (
        descriptor.schema_version != SPATIAL_HANDOFF_SCHEMA_VERSION
        or cells_artifact.schema_version != CELL_COLLECTION_SCHEMA_VERSION
    ):
        raise TissueRevisionStoreError("phenotype handoff artifact schemas are invalid")
    try:
        handoff = load_spatial_handoff(descriptor.path)
    except ROIAnalysisError as exc:
        raise TissueRevisionStoreError(
            "phenotype handoff descriptor is not a valid current spatial handoff"
        ) from exc

    export_sha256s = {
        result.exported_inventory.source_evidence_sha256
        for result in handoff.domain_results.values()
    }
    if len(export_sha256s) != 1:
        raise TissueRevisionStoreError(
            "phenotype handoff export inventories do not share one raw snapshot"
        )
    expected_raw_sha256 = next(iter(export_sha256s))
    _raw_path, described_raw_sha256 = _checksummed_raw_descriptor(
        output.get("raw_input_descriptor"),
        label="phenotype handoff raw descriptor",
    )
    if (
        output.get("raw_export_sha256") != expected_raw_sha256
        or described_raw_sha256 != expected_raw_sha256
    ):
        raise TissueRevisionStoreError(
            "phenotype handoff raw export/descriptor conflicts with domain evidence"
        )
    if any(
        result.analysis_config_sha256 != provenance.config_sha256
        for result in handoff.domain_results.values()
    ):
        raise TissueRevisionStoreError(
            "phenotype handoff analysis config conflicts with provenance"
        )
    _validate_current_domain_selection(provenance, handoff)

    descriptor_mapping = descriptor.to_mapping()
    cells_mapping = cells_artifact.to_mapping()
    if sum(artifact.to_mapping() == descriptor_mapping for artifact in artifacts) != 1:
        raise TissueRevisionStoreError(
            "output inventory requires exactly one current handoff descriptor artifact"
        )
    if sum(artifact.to_mapping() == cells_mapping for artifact in artifacts) != 1:
        raise TissueRevisionStoreError("output inventory requires the matching cell artifact")
    expected_results = {
        domain_id: result.domain_result_sha256
        for domain_id, result in handoff.domain_results.items()
    }
    expected_active = sorted(handoff.by_domain)
    expected_excluded = sorted(set(handoff.domain_results) - set(handoff.by_domain))
    expected_values = {
        "coordinate_system": "global_level0_pixels",
        "spatial_frame_sha256": handoff.spatial_frame.spatial_frame_sha256,
        "domain_set_sha256": handoff.domain_set.domain_set_sha256,
        "phenotype_generation_id": handoff.phenotype_generation_id,
        "selected_phenotype_view": handoff.selected_phenotype_view,
        "selected_phenotype_generation_id": handoff.phenotype_generation_id,
        "phenotype_inventory_sha256": handoff.phenotype_inventory_sha256,
        "cell_count": handoff.cell_count,
        "segmented_count": sum(
            result.segmented_inventory.count for result in handoff.domain_results.values()
        ),
        "phenotyped_count": sum(
            result.phenotyped_inventory.count for result in handoff.domain_results.values()
            if result.phenotyped_inventory is not None
        ),
        "exported_count": sum(
            result.exported_inventory.count for result in handoff.domain_results.values()
        ),
        "active_domain_ids": expected_active,
        "excluded_domain_ids": expected_excluded,
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
        "domain_result_sha256s": expected_results,
        "tissue_group_count": len(handoff.by_tissue),
        "spatial_statistics_performed": False,
    }
    for field, expected in expected_values.items():
        if _export_json(output.get(field)) != _export_json(expected):
            label = "domain-result" if field == "domain_result_sha256s" else field
            raise TissueRevisionStoreError(
                f"phenotype handoff {label} evidence conflicts with descriptor"
            )
    if (
        _export_json(output.get("descriptor")) != descriptor_mapping
        or _export_json(output.get("cells")) != cells_mapping
        or cells_artifact.record_count != handoff.cell_count
    ):
        raise TissueRevisionStoreError(
            "phenotype handoff artifact inventory conflicts with descriptor"
        )
    if (
        provenance.canonical_image_id != handoff.spatial_frame.canonical_image_id
        or provenance.canonical_image_sha256 != handoff.spatial_frame.canonical_image_sha256
    ):
        raise TissueRevisionStoreError(
            "phenotype handoff canonical image conflicts with provenance"
        )

    preflight_outputs = _completed_stage_outputs(provenance, "preflight")
    frames: list[CanonicalSpatialFrameEvidence] = []
    for preflight in preflight_outputs:
        if "spatial_frame" not in preflight:
            continue
        try:
            frame = CanonicalSpatialFrameEvidence.from_mapping(preflight["spatial_frame"])
        except ROIAnalysisError as exc:
            raise TissueRevisionStoreError("preflight spatial-frame evidence is invalid") from exc
        if (
            preflight.get("level0_size_x_px") != frame.level0_size_x_px
            or preflight.get("level0_size_y_px") != frame.level0_size_y_px
        ):
            raise TissueRevisionStoreError(
                "preflight dimensions conflict with spatial-frame evidence"
            )
        frames.append(frame)
    if not frames or frames[-1].to_mapping() != handoff.spatial_frame.to_mapping():
        raise TissueRevisionStoreError(
            "phenotype handoff spatial frame conflicts with accepted preflight"
        )

    selected = _phenotyping_provenance(provenance.stage_results)
    if (
        selected["selected_active_view"] != handoff.selected_phenotype_view
        or selected["selected_active_generation_id"] != handoff.phenotype_generation_id
        or selected["inventory_sha256"] != handoff.phenotype_inventory_sha256
    ):
        raise TissueRevisionStoreError(
            "phenotype handoff selected phenotype head conflicts with stage history"
        )

    selected_references: dict[str, Mapping[str, object]] = {}
    for stage_result in provenance.stage_results:
        if stage_result.get("status") != "completed":
            continue
        outputs = stage_result.get("outputs")
        reference = outputs.get("phenotype_view_artifact") if isinstance(outputs, Mapping) else None
        if not isinstance(reference, Mapping) or (
            reference.get("mode") != handoff.selected_phenotype_view
            or reference.get("phenotype_generation_id") != handoff.phenotype_generation_id
        ):
            continue
        section_id = reference.get("section_id")
        if not isinstance(section_id, str):
            raise TissueRevisionStoreError("selected phenotype section reference is invalid")
        prior = selected_references.get(section_id)
        if prior is not None and _export_json(prior) != _export_json(reference):
            raise TissueRevisionStoreError("selected phenotype section reference is ambiguous")
        selected_references[section_id] = reference
    phenotype_sections: dict[str, list[object]] = {}
    for result in handoff.domain_results.values():
        if not isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence):
            phenotype_sections.setdefault(result.member.section_id, []).append(result)
    if set(selected_references) != set(phenotype_sections):
        raise TissueRevisionStoreError(
            "phenotype handoff lacks complete selected phenotype section evidence"
        )
    for section_id, section_results in phenotype_sections.items():
        first = section_results[0]
        # i) We query only this accepted section's IDs and domain identities.
        # ii) A bounded disk sort retains exact membership without JSON ID arrays.
        with tempfile.TemporaryDirectory(prefix='mif-handoff-membership-') as temporary:
            connection=sqlite3.connect(Path(temporary)/'ids.sqlite')
            try:
                connection.execute('PRAGMA cache_size=-8192')
                connection.execute('PRAGMA temp_store=FILE')
                connection.execute('CREATE TABLE cells(cell_id TEXT PRIMARY KEY,domain_id TEXT)')
                for selector,section in zip(handoff.cell_dataset.description['sections'],handoff.cell_dataset.sections):
                    if selector['section_id']!=section_id:
                        continue
                    for batch in section.iter_batches(columns=['cell_id','analysis_domain_id']):
                        rows=zip(batch.column('cell_id').to_pylist(),batch.column('analysis_domain_id').to_pylist())
                        connection.executemany('INSERT INTO cells VALUES(?,?)',rows)
                expected_ids=SortedIdDigest();expected_mapping=SortedIdMappingDigest()
                for id,domain in connection.execute('SELECT cell_id,domain_id FROM cells ORDER BY cell_id'):
                    expected_ids.add(id);expected_mapping.add(id,domain)
            finally:
                connection.close()
        if any(
            result.phenotype_section_artifact_sha256 != first.phenotype_section_artifact_sha256
            or result.phenotype_section_lineage_sha256 != first.phenotype_section_lineage_sha256
            or result.phenotype_source_run_id != first.phenotype_source_run_id
            or result.member.slide_id != first.member.slide_id
            or result.member.tissue_generation_id != first.member.tissue_generation_id
            for result in section_results
        ):
            raise TissueRevisionStoreError(
                "phenotype handoff domain results conflict within a section"
            )
        reference = selected_references[section_id]
        expected = {
            "schema_version": "phenotype-view-reference/v1",
            "mode": handoff.selected_phenotype_view,
            "slide_id": first.member.slide_id,
            "canonical_image_sha256": handoff.spatial_frame.canonical_image_sha256,
            "tissue_generation_id": first.member.tissue_generation_id,
            "phenotype_generation_id": handoff.phenotype_generation_id,
            "section_id": section_id,
            "artifact_sha256": first.phenotype_section_artifact_sha256,
            "lineage_sha256": first.phenotype_section_lineage_sha256,
            "cell_ids_sha256": expected_ids.hexdigest(),
            "cell_count": expected_ids.count,
            "cell_to_domain_membership_sha256": expected_mapping.hexdigest(),
        }
        if (
            # The view retains its preparation attempt when a later attempt
            # accepts recovered application output. The handoff records that
            # application attempt; their exact view identity is the metadata hash.
            set(reference) != {*expected, "source_run_id", "artifact_path", "assignments_sha256"}
            or not isinstance(reference.get("source_run_id"), str)
            or not reference["source_run_id"].strip()
            or not isinstance(reference.get("assignments_sha256"),str)
            or len(reference["assignments_sha256"])!=64
            or any(character not in "0123456789abcdef" for character in reference["assignments_sha256"])
            or not isinstance(reference.get("artifact_path"), str)
            or not reference["artifact_path"].strip()
            or any(
                _export_json(reference.get(field)) != _export_json(value)
                for field, value in expected.items()
            )
        ):
            raise TissueRevisionStoreError(
                "phenotype handoff selected phenotype section evidence conflicts"
            )

    semantic_history: dict[str, list[Mapping[str, object]]] = {}
    for semantic_output in _completed_stage_outputs(provenance, "semantic_validation"):
        raw = semantic_output.get("domain_finalization")
        if raw is None and semantic_output.get("kind") in {
            "secondary_finalized",
            "no_secondary_configured",
        }:
            raw = semantic_output
        if not isinstance(raw, Mapping):
            continue
        domain_id = raw.get("analysis_domain_id")
        if not isinstance(domain_id, str):
            raise TissueRevisionStoreError(
                "semantic domain finalization history has an invalid domain"
            )
        semantic_history.setdefault(domain_id, []).append(semantic_output)
    for domain_id, result in handoff.domain_results.items():
        history = semantic_history.get(domain_id, ())
        if isinstance(result.finalization, PrimaryExcludedDomainCompletionEvidence):
            # The handoff output above retains the exact primary exclusion and
            # its zero segmentation identity. No semantic stage ran here.
            if history:
                raise TissueRevisionStoreError("excluded section claims semantic finalization")
            continue
        if not history:
            raise TissueRevisionStoreError(
                "phenotype handoff domain result lacks one exact current semantic finalization"
            )
        current = history[-1]
        raw = current.get("domain_finalization")
        if raw is None and current.get("kind") in {
            "secondary_finalized",
            "no_secondary_configured",
        }:
            raw = current
        if _export_json(raw) != result.finalization.to_mapping():
            raise TissueRevisionStoreError(
                "phenotype handoff domain result conflicts with current semantic finalization"
            )
        common_outer_scope = {
            "analysis_domain_id": result.member.analysis_domain_id,
            "section_id": result.member.section_id,
        }
        if any(
            _export_json(current.get(field)) != _export_json(expected)
            for field, expected in common_outer_scope.items()
        ):
            raise TissueRevisionStoreError(
                "current outer semantic scope conflicts with domain member evidence"
            )
        if isinstance(result.finalization, FinalizedSecondaryDomainEvidence):
            applicable_outer_scope = {
                "analysis_domain_revision": (result.member.analysis_domain_revision),
                "analysis_domain_revision_sha256": (result.member.analysis_domain_revision_sha256),
                "domain_set_sha256": handoff.domain_set.domain_set_sha256,
                "spatial_frame_sha256": (handoff.spatial_frame.spatial_frame_sha256),
                "domain_status": result.domain_status,
                "coordinate_system": "global_level0",
            }
            if any(
                _export_json(current.get(field)) != _export_json(expected)
                for field, expected in applicable_outer_scope.items()
            ):
                raise TissueRevisionStoreError(
                    "current outer semantic revision/domain/frame projection conflicts"
                )
        if (
            current.get("analysis_config_sha256") != result.analysis_config_sha256
            or current.get("analysis_config_sha256") != provenance.config_sha256
        ):
            raise TissueRevisionStoreError(
                "current semantic analysis config conflicts with domain result"
            )
        _semantic_path, semantic_raw_sha256 = _checksummed_raw_descriptor(
            current.get("raw_input_descriptor"),
            label="current semantic raw descriptor",
        )
        if semantic_raw_sha256 != result.semantic_raw_input_sha256:
            raise TissueRevisionStoreError(
                "current semantic raw descriptor conflicts with domain result"
            )
        if isinstance(result.finalization, NoSecondaryDomainCompletionEvidence):
            if (
                result.finalization.raw_input_sha256 != semantic_raw_sha256
                or result.finalization.analysis_config_sha256 != provenance.config_sha256
            ):
                raise TissueRevisionStoreError(
                    "no-secondary completion conflicts with current raw/config evidence"
                )
        else:
            candidate_path, candidate_artifact_sha256 = _checksummed_raw_descriptor(
                current.get("semantic_candidate_membership"),
                label="current semantic candidate membership",
            )
            receipt = result.finalization.finalization_receipt
            expected_candidate_scope = {
                "slide_id": result.member.slide_id,
                "section_id": result.member.section_id,
                "analysis_domain_id": result.member.analysis_domain_id,
                "generation_id": result.member.tissue_generation_id,
                "pipeline_run_id": receipt["pipeline_run_id"],
                "segmentation_population_id": (result.segmentation_population_id),
                "raw_export_path": str(_semantic_path),
            }
            from mif_pipeline.orchestration.analysis_services import (
                _validated_semantic_candidate_membership_authority,
            )

            try:
                candidate_authority = _validated_semantic_candidate_membership_authority(
                    _semantic_path,
                    candidate_path,
                    raw_export_sha256=semantic_raw_sha256,
                    membership_artifact_sha256=(candidate_artifact_sha256),
                    expected_scope=expected_candidate_scope,
                    physical_calibration={
                        "x_um_per_px": (handoff.spatial_frame.x_um_per_px),
                        "y_um_per_px": (handoff.spatial_frame.y_um_per_px),
                    },
                    phenotype_view={
                        "mode": result.selected_phenotype_view,
                        "generation_id": (result.phenotype_generation_id),
                        "inventory_sha256": (result.phenotype_inventory_sha256),
                    },
                )
            except ROIAnalysisError as exc:
                raise TissueRevisionStoreError(
                    "current semantic candidate membership evidence is invalid"
                ) from exc
            rows = _validate_semantic_result_artifact_file(
                result.finalization.semantic_result_artifact,
                result.finalization,
            )
            _validate_semantic_identity_chain(handoff, result, rows, candidate_authority)
            for evidence in result.finalization.manual_selections:
                _validate_manual_selection_artifact_file(
                    evidence,
                    result.finalization,
                    expected_analysis_domain_revision=(result.member.analysis_domain_revision),
                )

    segmentation_history: dict[str, list[Mapping[str, object]]] = {}
    for segmentation_output in _completed_stage_outputs(provenance, "segmentation"):
        population = segmentation_output.get("segmentation_population")
        if not isinstance(population, Mapping):
            continue
        section_id = population.get("section_id")
        if not isinstance(section_id, str):
            raise TissueRevisionStoreError("segmentation population history has an invalid section")
        segmentation_history.setdefault(section_id, []).append(population)
    for result in handoff.domain_results.values():
        fixed = {
            "segmentation_population_id": result.segmentation_population_id,
            "canonical_image_id": result.segmentation_canonical_image_id,
            "canonical_image_sha256": result.segmentation_canonical_image_sha256,
            "domain_set_sha256": result.segmentation_domain_set_sha256,
            "section_population_sha256": (result.segmentation_section_population_sha256),
            "segmentation_domain_sha256": result.segmentation_domain_sha256,
            "domain_cell_mapping_sha256": (result.segmentation_domain_cell_mapping_sha256),
        }
        matches = []
        section_history = segmentation_history.get(result.member.section_id, ())
        current_populations = section_history[-1:] if section_history else ()
        for population in current_populations:
            if any(population.get(field) != expected for field, expected in fixed.items()):
                continue
            raw_inventories = population.get("domain_cell_inventories")
            if isinstance(raw_inventories, (str, bytes)) or not isinstance(
                raw_inventories, Sequence
            ):
                continue
            domain_inventories = tuple(
                item
                for item in raw_inventories
                if isinstance(item, Mapping)
                and item.get("analysis_domain_id") == result.member.analysis_domain_id
            )
            if len(domain_inventories) != 1:
                continue
            inventory = domain_inventories[0]
            if (
                inventory.get("cell_count") == result.segmented_inventory.count
                and inventory.get("cell_ids_sha256") == result.segmented_inventory.cell_ids_sha256
            ):
                matches.append(population)
        if len(matches) != 1:
            raise TissueRevisionStoreError(
                "phenotype handoff lacks one exact current segmentation population"
            )


def _validate_analysis_provenance_file(
    path: Path,
    *,
    revision_count: int,
    output_count: int,
) -> None:
    """Validate analysis provenance file for the current operation.

    The required mapping shape includes ``schema_version``, ``slide_id``,
    ``canonical_image``, ``configuration``, ``software``, ``models``. Field checks are
    applied before the validated value is returned or passed to its next constructor."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TissueRevisionStoreError("written analysis provenance is invalid JSON") from exc
    required = {
        "schema_version",
        "slide_id",
        "canonical_image",
        "configuration",
        "software",
        "models",
        "classifiers",
        "channel_selection",
        "accepted_generation_context",
        "registrations",
        "transfer_qc_records",
        "transfer_attempts",
        "transfer_commit_evidence",
        "transfer_cleanup_evidence",
        "transfer_commit_outcomes",
        "review_actions",
        "warnings",
        "checkpoint_ids",
        "stage_results",
        "stage_runtimes",
        "phenotyping",
        "semantic",
        "tissue_revision_records",
        "outputs",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise TissueRevisionStoreError("written analysis provenance fields are invalid")
    if payload.get("schema_version") != ANALYSIS_PROVENANCE_SCHEMA:
        raise TissueRevisionStoreError("written analysis provenance schema is invalid")
    if (
        not isinstance(payload["tissue_revision_records"], list)
        or len(payload["tissue_revision_records"]) != revision_count
    ):
        raise TissueRevisionStoreError("written tissue revision count does not match")
    if not isinstance(payload["outputs"], list) or len(payload["outputs"]) != output_count:
        raise TissueRevisionStoreError("written output inventory count does not match")


def write_analysis_provenance(
    path: str | Path,
    provenance: AnalysisProvenance,
    *,
    revision_store: TissueRevisionStore | None = None,
    outputs: Sequence[OutputArtifact | Mapping[str, object]] = (),
) -> OutputArtifact:
    """Atomically write provenance after revalidating every output checksum."""
    if not isinstance(provenance, AnalysisProvenance):
        raise TissueRevisionStoreError("provenance must be an AnalysisProvenance")
    if revision_store is not None and not isinstance(revision_store, TissueRevisionStore):
        raise TissueRevisionStoreError("revision_store must be a TissueRevisionStore")
    artifacts = tuple(_output_artifact(item) for item in outputs)
    _validate_semantic_artifact_files(provenance)
    _validate_completed_spatial_handoff_provenance(provenance, artifacts)
    revisions = (
        () if revision_store is None else revision_store.all_records(slide_id=provenance.slide_id)
    )
    payload = {
        "schema_version": ANALYSIS_PROVENANCE_SCHEMA,
        **provenance.to_mapping(),
        "tissue_revision_records": [record.to_mapping() for record in revisions],
        "outputs": [artifact.to_mapping() for artifact in artifacts],
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _validate_analysis_provenance_file(
            temporary,
            revision_count=len(revisions),
            output_count=len(artifacts),
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return OutputArtifact(
        path=destination,
        schema_version=ANALYSIS_PROVENANCE_SCHEMA,
        media_type="application/json",
        record_count=len(revisions),
        sha256=_file_sha256(destination),
    )
