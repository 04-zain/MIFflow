"""Registration-owned immutable review evidence for Warpy object transfer.

The `TransferReviewDecision` record carries `schema_version`, `decision_id`, `proposal_id`,
`proposal_sha256`, `accepted_interaction_response_sha256` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.schema_registry import assert_current_writer

from .backend import (
    RegistrationRecord,
    TransferQCRecord,
    import_transfer_qc_records,
    registration_record_sha256,
)

TRANSFER_REVIEW_PROPOSAL_SCHEMA_VERSION = "warpy-transfer-review-proposal/v1"
TRANSFER_REVIEW_DECISION_SCHEMA_VERSION = "warpy-transfer-review-decision/v1"
_ACTIONS = frozenset({"accept_transfers", "reject_transfers"})
_PROPOSAL_ID = re.compile(r"transfer-review-proposal-[0-9a-f]{64}")


# ==============================================================================
#                                Transfer review
# ==============================================================================


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ROIAnalysisError("transfer review material is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ROIAnalysisError(f"{label} must be a string-keyed mapping")
    if set(value) != fields:
        raise ROIAnalysisError(f"{label} fields do not match the schema")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256(value: object, field: str) -> str:
    result = _text(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _strings(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError(f"{field} must be an array")
    result = tuple(_text(item, field) for item in value)
    if not result:
        raise ROIAnalysisError(f"{field} must not be empty")
    if len(result) > 128:
        raise ROIAnalysisError(f"{field} exceeds the transfer review limit")
    if len(set(result)) != len(result):
        raise ROIAnalysisError(f"{field} must be unique")
    return result


def _qc_records_sha256(records: Sequence[TransferQCRecord]) -> str:
    return _digest([item.to_mapping() for item in records])


def _proposal_identity_material(
    registration_sha256: str,
    source_object_ids: Sequence[str],
    qc_records: Sequence[TransferQCRecord],
) -> dict[str, object]:
    return {
        "schema_version": TRANSFER_REVIEW_PROPOSAL_SCHEMA_VERSION,
        "registration_sha256": registration_sha256,
        "source_object_ids": list(source_object_ids),
        "qc_records": [
            {
                "transfer_qc_id": item.transfer_qc_id,
                "row_sha256": _digest(item.to_mapping()),
            }
            for item in qc_records
        ],
    }


@dataclass(frozen=True, slots=True)
class TransferReviewProposal:
    schema_version: str
    proposal_id: str
    registration: RegistrationRecord
    registration_sha256: str
    source_object_ids: tuple[str, ...]
    qc_records: tuple[TransferQCRecord, ...]
    qc_records_sha256: str

    def __post_init__(self) -> None:
        """Validate the TransferReviewProposal fields when the record is constructed.

        The checks cover ``schema_version``, ``registration``, ``source_object_ids``,
        ``qc_records``, ``proposal_id``, ``registration_sha256``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if self.schema_version != TRANSFER_REVIEW_PROPOSAL_SCHEMA_VERSION:
            raise ROIAnalysisError("transfer review proposal schema is unsupported")
        if not isinstance(self.registration, RegistrationRecord) or (
            self.registration.acceptance_state != "accepted"
        ):
            raise ROIAnalysisError("transfer review proposal requires an accepted registration")
        registration_digest = registration_record_sha256(self.registration)
        if _sha256(self.registration_sha256, "registration_sha256") != registration_digest:
            raise ROIAnalysisError("transfer review proposal registration digest mismatch")
        sources = _strings(self.source_object_ids, "source_object_ids")
        records = tuple(self.qc_records)
        if (
            not records
            or len(records) > 128
            or any(not isinstance(item, TransferQCRecord) for item in records)
        ):
            raise ROIAnalysisError("transfer review proposal QC records are invalid")
        if tuple(item.source_object_id for item in records) != sources:
            raise ROIAnalysisError("transfer review proposal QC order differs from source requests")
        if any(item.authoritative for item in records):
            raise ROIAnalysisError("transfer review proposal cannot contain authoritative rows")
        if not any(
            item.computed_status == "passed"
            and item.review_state == "pending"
            and item.disposition == "pending_review"
            for item in records
        ):
            raise ROIAnalysisError("transfer review proposal requires a pending passing row")
        if any(
            item.computed_status == "passed"
            and (item.review_state != "pending" or item.disposition != "pending_review")
            for item in records
        ):
            raise ROIAnalysisError("passing proposal rows must remain pending review")
        policy = records[0].policy
        validated = import_transfer_qc_records(
            [item.to_mapping() for item in records],
            registration=self.registration,
            tissue_generation_id=self.registration.tissue_generation_id,
            tissue_section_ids=self.registration.tissue_section_ids,
            policy=policy,
        )
        if validated != records:
            raise ROIAnalysisError("transfer review proposal QC records changed on import")
        records_digest = _qc_records_sha256(records)
        if _sha256(self.qc_records_sha256, "qc_records_sha256") != records_digest:
            raise ROIAnalysisError("transfer review proposal QC digest mismatch")
        expected_id = "transfer-review-proposal-" + _digest(
            _proposal_identity_material(registration_digest, sources, records)
        )
        proposal_id = _text(self.proposal_id, "proposal_id")
        if (
            self.proposal_id != proposal_id
            or _PROPOSAL_ID.fullmatch(proposal_id) is None
            or proposal_id != expected_id
        ):
            raise ROIAnalysisError("transfer review proposal ID is not canonical")
        object.__setattr__(self, "registration_sha256", registration_digest)
        object.__setattr__(self, "source_object_ids", sources)
        object.__setattr__(self, "qc_records", records)
        object.__setattr__(self, "qc_records_sha256", records_digest)

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("warpy-transfer-review-proposal", "1")
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "registration": self.registration.to_mapping(),
            "registration_sha256": self.registration_sha256,
            "source_object_ids": list(self.source_object_ids),
            "qc_records": [item.to_mapping() for item in self.qc_records],
            "qc_records_sha256": self.qc_records_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TransferReviewProposal":
        """Decode the serialized fields into a validated TransferReviewProposal record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _strict_mapping(
            value,
            frozenset(
                {
                    "schema_version",
                    "proposal_id",
                    "registration",
                    "registration_sha256",
                    "source_object_ids",
                    "qc_records",
                    "qc_records_sha256",
                }
            ),
            "transfer review proposal",
        )
        raw_records = mapping["qc_records"]
        raw_sources = mapping["source_object_ids"]
        if isinstance(raw_sources, (str, bytes)) or not isinstance(raw_sources, Sequence):
            raise ROIAnalysisError("transfer review proposal source IDs must be an array")
        if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
            raise ROIAnalysisError("transfer review proposal QC records must be an array")
        registration = RegistrationRecord.from_mapping(mapping["registration"])
        if registration.to_mapping() != mapping["registration"]:
            raise ROIAnalysisError("transfer review proposal registration bytes are not canonical")
        result = cls(
            schema_version=mapping["schema_version"],
            proposal_id=mapping["proposal_id"],
            registration=registration,
            registration_sha256=mapping["registration_sha256"],
            source_object_ids=tuple(raw_sources),
            qc_records=tuple(TransferQCRecord.from_mapping(item) for item in raw_records),
            qc_records_sha256=mapping["qc_records_sha256"],
        )
        if result.to_mapping() != mapping:
            raise ROIAnalysisError("transfer review proposal mapping is not canonical")
        return result


def build_transfer_review_proposal(
    *,
    registration: RegistrationRecord,
    source_object_ids: Sequence[str],
    qc_records: Sequence[TransferQCRecord],
) -> TransferReviewProposal:
    sources = _strings(source_object_ids, "source_object_ids")
    records = tuple(qc_records)
    if any(not isinstance(item, TransferQCRecord) for item in records):
        raise ROIAnalysisError("transfer review proposal requires typed QC records")
    registration_digest = registration_record_sha256(registration)
    return TransferReviewProposal(
        schema_version=TRANSFER_REVIEW_PROPOSAL_SCHEMA_VERSION,
        proposal_id="transfer-review-proposal-"
        + _digest(_proposal_identity_material(registration_digest, sources, records)),
        registration=registration,
        registration_sha256=registration_digest,
        source_object_ids=sources,
        qc_records=records,
        qc_records_sha256=_qc_records_sha256(records),
    )


def transfer_review_proposal_sha256(proposal: TransferReviewProposal) -> str:
    if not isinstance(proposal, TransferReviewProposal):
        raise ROIAnalysisError("proposal digest requires a TransferReviewProposal")
    return _digest(proposal.to_mapping())


def _decision_unsigned(
    *,
    proposal_id: str,
    proposal_sha256: str,
    accepted_interaction_response_sha256: str,
    action: str,
    reviewed_by: str,
    reviewed_qc_records: Sequence[TransferQCRecord],
    reviewed_qc_records_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": TRANSFER_REVIEW_DECISION_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "proposal_sha256": proposal_sha256,
        "accepted_interaction_response_sha256": (accepted_interaction_response_sha256),
        "action": action,
        "reviewed_by": reviewed_by,
        "reviewed_qc_records": [item.to_mapping() for item in reviewed_qc_records],
        "reviewed_qc_records_sha256": reviewed_qc_records_sha256,
    }


@dataclass(frozen=True, slots=True)
class TransferReviewDecision:
    schema_version: str
    decision_id: str
    proposal_id: str
    proposal_sha256: str
    accepted_interaction_response_sha256: str
    action: str
    reviewed_by: str
    reviewed_qc_records: tuple[TransferQCRecord, ...]
    reviewed_qc_records_sha256: str

    def __post_init__(self) -> None:
        """Validate the TransferReviewDecision fields when the record is constructed.

        The checks cover ``schema_version``, ``proposal_id``, ``proposal_sha256``,
        ``accepted_interaction_response_sha256``, ``action``, ``reviewed_by``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if self.schema_version != TRANSFER_REVIEW_DECISION_SCHEMA_VERSION:
            raise ROIAnalysisError("transfer review decision schema is unsupported")
        proposal_id = _text(self.proposal_id, "proposal_id")
        if self.proposal_id != proposal_id or _PROPOSAL_ID.fullmatch(proposal_id) is None:
            raise ROIAnalysisError("transfer review decision proposal ID is not canonical")
        proposal_digest = _sha256(self.proposal_sha256, "proposal_sha256")
        response_digest = _sha256(
            self.accepted_interaction_response_sha256,
            "accepted_interaction_response_sha256",
        )
        action = _text(self.action, "action")
        if action not in _ACTIONS:
            raise ROIAnalysisError("transfer review decision action is unsupported")
        reviewer = _text(self.reviewed_by, "reviewed_by")
        records = tuple(self.reviewed_qc_records)
        if (
            not records
            or len(records) > 128
            or any(not isinstance(item, TransferQCRecord) for item in records)
        ):
            raise ROIAnalysisError("transfer review decision rows are invalid")
        if len({item.transfer_qc_id for item in records}) != len(records):
            raise ROIAnalysisError("transfer review decision row IDs must be unique")
        if any(
            item.review_state == "pending" or item.disposition == "pending_review"
            for item in records
        ):
            raise ROIAnalysisError("transfer review decision rows must be reviewed")
        for item in records:
            expected_review = (
                "accepted"
                if item.computed_status == "passed" and action == "accept_transfers"
                else "rejected"
            )
            expected_disposition = (
                "accepted"
                if item.computed_status == "passed" and action == "accept_transfers"
                else "rejected" if item.computed_status in {"passed", "failed"} else "quarantined"
            )
            if item.review_state != expected_review or item.disposition != expected_disposition:
                raise ROIAnalysisError("transfer review decision row conflicts with its action")
        expected_records_digest = _qc_records_sha256(records)
        if (
            _sha256(self.reviewed_qc_records_sha256, "reviewed_qc_records_sha256")
            != expected_records_digest
        ):
            raise ROIAnalysisError("transfer review decision row digest mismatch")
        unsigned = _decision_unsigned(
            proposal_id=proposal_id,
            proposal_sha256=proposal_digest,
            accepted_interaction_response_sha256=response_digest,
            action=action,
            reviewed_by=reviewer,
            reviewed_qc_records=records,
            reviewed_qc_records_sha256=expected_records_digest,
        )
        expected_id = "transfer-review-decision-" + _digest(unsigned)
        if _text(self.decision_id, "decision_id") != expected_id:
            raise ROIAnalysisError("transfer review decision ID is not canonical")
        object.__setattr__(self, "proposal_id", proposal_id)
        object.__setattr__(self, "proposal_sha256", proposal_digest)
        object.__setattr__(self, "accepted_interaction_response_sha256", response_digest)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "reviewed_by", reviewer)
        object.__setattr__(self, "reviewed_qc_records", records)
        object.__setattr__(self, "reviewed_qc_records_sha256", expected_records_digest)

    def to_mapping(self) -> dict[str, object]:
        assert_current_writer("warpy-transfer-review-decision", "1")
        return {
            "decision_id": self.decision_id,
            **_decision_unsigned(
                proposal_id=self.proposal_id,
                proposal_sha256=self.proposal_sha256,
                accepted_interaction_response_sha256=(self.accepted_interaction_response_sha256),
                action=self.action,
                reviewed_by=self.reviewed_by,
                reviewed_qc_records=self.reviewed_qc_records,
                reviewed_qc_records_sha256=self.reviewed_qc_records_sha256,
            ),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TransferReviewDecision":
        """Decode the serialized fields into a validated TransferReviewDecision record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _strict_mapping(
            value,
            frozenset(
                {
                    "schema_version",
                    "decision_id",
                    "proposal_id",
                    "proposal_sha256",
                    "accepted_interaction_response_sha256",
                    "action",
                    "reviewed_by",
                    "reviewed_qc_records",
                    "reviewed_qc_records_sha256",
                }
            ),
            "transfer review decision",
        )
        raw_records = mapping["reviewed_qc_records"]
        if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
            raise ROIAnalysisError("transfer review decision rows must be an array")
        result = cls(
            schema_version=mapping["schema_version"],
            decision_id=mapping["decision_id"],
            proposal_id=mapping["proposal_id"],
            proposal_sha256=mapping["proposal_sha256"],
            accepted_interaction_response_sha256=mapping["accepted_interaction_response_sha256"],
            action=mapping["action"],
            reviewed_by=mapping["reviewed_by"],
            reviewed_qc_records=tuple(TransferQCRecord.from_mapping(item) for item in raw_records),
            reviewed_qc_records_sha256=mapping["reviewed_qc_records_sha256"],
        )
        if result.to_mapping() != mapping:
            raise ROIAnalysisError("transfer review decision mapping is not canonical")
        return result


def issue_transfer_review_decision(
    proposal: TransferReviewProposal,
    *,
    action: Literal["accept_transfers", "reject_transfers"],
    reviewed_by: str,
    accepted_interaction_response_sha256: str,
) -> TransferReviewDecision:
    if not isinstance(proposal, TransferReviewProposal):
        raise ROIAnalysisError("transfer review decision requires a typed proposal")
    selected_action = _text(action, "action")
    if selected_action not in _ACTIONS:
        raise ROIAnalysisError("transfer review decision action is unsupported")
    reviewer = _text(reviewed_by, "reviewed_by")
    response_digest = _sha256(
        accepted_interaction_response_sha256,
        "accepted_interaction_response_sha256",
    )
    reviewed: list[TransferQCRecord] = []
    for source in proposal.qc_records:
        mapping = source.to_mapping()
        if source.computed_status == "passed":
            accepted = selected_action == "accept_transfers"
            mapping["review_state"] = "accepted" if accepted else "rejected"
            mapping["disposition"] = "accepted" if accepted else "rejected"
        else:
            mapping["review_state"] = "rejected"
            mapping["disposition"] = (
                "rejected" if source.computed_status == "failed" else "quarantined"
            )
        reviewed.append(TransferQCRecord.from_mapping(mapping))
    records = tuple(reviewed)
    records_digest = _qc_records_sha256(records)
    proposal_digest = transfer_review_proposal_sha256(proposal)
    unsigned = _decision_unsigned(
        proposal_id=proposal.proposal_id,
        proposal_sha256=proposal_digest,
        accepted_interaction_response_sha256=response_digest,
        action=selected_action,
        reviewed_by=reviewer,
        reviewed_qc_records=records,
        reviewed_qc_records_sha256=records_digest,
    )
    return TransferReviewDecision(
        schema_version=TRANSFER_REVIEW_DECISION_SCHEMA_VERSION,
        decision_id="transfer-review-decision-" + _digest(unsigned),
        proposal_id=proposal.proposal_id,
        proposal_sha256=proposal_digest,
        accepted_interaction_response_sha256=response_digest,
        action=selected_action,
        reviewed_by=reviewer,
        reviewed_qc_records=records,
        reviewed_qc_records_sha256=records_digest,
    )
