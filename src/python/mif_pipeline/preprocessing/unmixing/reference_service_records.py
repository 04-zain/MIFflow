"""Immutable presentation-neutral result records for the reference service.

The `ReferenceServiceSnapshot` record carries `checkpoint`, `start_authority_sha256`,
`review_data`.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .reference_view_records import ReferenceReviewData
from .reference_workflow import ReferenceWorkflowCheckpoint

# ==============================================================================
#                           Reference service records
# ==============================================================================


class ReferenceServiceAdvanceStatus(str, Enum):
    PERSISTED = "persisted"
    NEEDS_SCIENTIFIC_RESULT = "needs_scientific_result"


@dataclass(frozen=True, slots=True)
class ReferenceServiceSnapshot:
    """One authoritative checkpoint plus optional ephemeral review data."""

    checkpoint: ReferenceWorkflowCheckpoint
    start_authority_sha256: str
    review_data: ReferenceReviewData | None = None

    def __post_init__(self) -> None:
        if type(self.checkpoint) is not ReferenceWorkflowCheckpoint:
            raise TypeError("service snapshot checkpoint has the wrong record type")
        if (
            type(self.start_authority_sha256) is not str
            or len(self.start_authority_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.start_authority_sha256)
        ):
            raise ValueError("service snapshot start authority must be a SHA-256")
        if self.review_data is not None:
            if type(self.review_data) is not ReferenceReviewData:
                raise TypeError("service snapshot review data has the wrong record type")
            request = self.checkpoint.request
            if request is None or (
                self.review_data.request_id != request.request_id
                or self.review_data.request_revision != request.revision
                or self.review_data.checkpoint != request.checkpoint.value
                or self.review_data.authoritative_state_sha256 != request.authoritative_state_sha256
                or self.review_data.workflow_checkpoint_sha256 != self.checkpoint.content_sha256
            ):
                raise ValueError("service snapshot review data does not bind the live checkpoint")


@dataclass(frozen=True, slots=True)
class ReferenceServiceAdvance:
    """Persisted transition or an explicit request for scientific composition."""

    status: ReferenceServiceAdvanceStatus
    snapshot: ReferenceServiceSnapshot
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReferenceServiceAdvanceStatus):
            raise TypeError("reference service advance status is invalid")
        if type(self.snapshot) is not ReferenceServiceSnapshot:
            raise TypeError("reference service advance snapshot is invalid")
        if self.status is ReferenceServiceAdvanceStatus.NEEDS_SCIENTIFIC_RESULT:
            if type(self.reason) is not str or not self.reason:
                raise ValueError("scientific-result state requires a reason")
        elif self.reason is not None:
            raise ValueError("persisted service advance cannot include a reason")


# These records remain implemented in this focused module, but their public
# fully-qualified identity predates the split and belongs to the service facade.
_PUBLIC_MODULE = "mif_pipeline.preprocessing.unmixing.reference_service"
ReferenceServiceAdvanceStatus.__module__ = _PUBLIC_MODULE
ReferenceServiceSnapshot.__module__ = _PUBLIC_MODULE
ReferenceServiceAdvance.__module__ = _PUBLIC_MODULE


__all__ = [
    "ReferenceServiceAdvance",
    "ReferenceServiceAdvanceStatus",
    "ReferenceServiceSnapshot",
]
