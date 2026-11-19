"""Small closed authority bindings for service-owned source selection.

The `BasisSourceDecisionBinding` record carries `source_checkpoint_sha256`, `request_id`,
`choice`, `reviewer_id`, `reviewed_at` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .compatibility import BasisSourceChoice
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                        Reference service source records
# ==============================================================================


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    result = _text(value, label)
    if len(result) != 64 or any(item not in "0123456789abcdef" for item in result):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return result


@dataclass(frozen=True, slots=True)
class BasisSourceDecisionBinding:
    """Replay inputs for one reviewed source decision; never a ready basis proof."""

    source_checkpoint_sha256: str
    request_id: str
    choice: BasisSourceChoice
    reviewer_id: str
    reviewed_at: str
    target_input_authority_sha256: str
    target_image_sha256: str
    imported_library_content_sha256: str | None = None
    imported_library_bytes_sha256: str | None = None
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the BasisSourceDecisionBinding fields when the record is constructed.

        The checks cover ``source_checkpoint_sha256``, ``request_id``, ``reviewer_id``,
        ``reviewed_at``, ``target_input_authority_sha256``, ``target_image_sha256``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _sha256(self.source_checkpoint_sha256, "source checkpoint SHA-256")
        _sha256(self.request_id, "source request ID")
        if type(self.choice) is not BasisSourceChoice:
            raise TypeError("basis source binding choice must be exact")
        _text(self.reviewer_id, "basis source reviewer ID")
        timestamp = _text(self.reviewed_at, "basis source review timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("basis source review timestamp must be RFC 3339") from exc
        if parsed.tzinfo is None:
            raise ValueError("basis source review timestamp must include a timezone")
        _sha256(self.target_input_authority_sha256, "target input authority SHA-256")
        _sha256(self.target_image_sha256, "target image SHA-256")
        imported = self.choice is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY
        if imported:
            _sha256(
                self.imported_library_content_sha256,
                "imported library content SHA-256",
            )
            _sha256(
                self.imported_library_bytes_sha256,
                "imported library bytes SHA-256",
            )
        elif (
            self.imported_library_content_sha256 is not None
            or self.imported_library_bytes_sha256 is not None
        ):
            raise ValueError("only an imported source may bind library bytes")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "basis_source_decision_binding_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "request_id": self.request_id,
                    "choice": self.choice.value,
                    "reviewer_id": self.reviewer_id,
                    "reviewed_at": self.reviewed_at,
                    "target_input_authority_sha256": self.target_input_authority_sha256,
                    "target_image_sha256": self.target_image_sha256,
                    "imported_library_content_sha256": self.imported_library_content_sha256,
                    "imported_library_bytes_sha256": self.imported_library_bytes_sha256,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReferenceControlFilesBinding:
    """Exact canonical input IDs selected in the live panel order."""

    source_checkpoint_sha256: str
    request_id: str
    reviewer_id: str
    target_input_authority_sha256: str
    control_strategy: str
    positive_reference_input_ids: tuple[str, ...]
    shared_unstained_input_id: str | None
    negative_control_input_ids: tuple[str, ...]
    prepare_shared_control: bool = field(default=False, metadata={"omit_empty": True})
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the ReferenceControlFilesBinding fields when the record is constructed.

        The checks cover ``positive_reference_input_ids``, ``source_checkpoint_sha256``,
        ``request_id``, ``reviewer_id``, ``target_input_authority_sha256``,
        ``control_strategy``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        _sha256(self.source_checkpoint_sha256, "file-selection checkpoint SHA-256")
        _sha256(self.request_id, "file-selection request ID")
        _text(self.reviewer_id, "file-selection reviewer ID")
        _sha256(self.target_input_authority_sha256, "target input authority SHA-256")
        if type(self.prepare_shared_control) is not bool:
            raise ValueError("Source preparation flag must be an exact boolean")
        if self.control_strategy not in {
            "shared_unstained_control",
            "per_stain_negative_controls",
        }:
            raise ValueError("file-selection control strategy is unsupported")
        if (
            type(self.positive_reference_input_ids) is not tuple
            or not self.positive_reference_input_ids
        ):
            raise TypeError("positive reference IDs must be a nonempty tuple")
        for item in self.positive_reference_input_ids:
            _text(item, "positive reference input ID")
        if len(set(self.positive_reference_input_ids)) != len(self.positive_reference_input_ids):
            raise ValueError("positive reference input IDs must be unique")
        if self.control_strategy == "shared_unstained_control":
            _text(self.shared_unstained_input_id, "shared unstained input ID")
            if self.negative_control_input_ids:
                raise ValueError("shared unstained selection cannot include negatives")
        else:
            if self.shared_unstained_input_id is not None:
                raise ValueError("per-stain negatives cannot include shared unstained")
            if (
                type(self.negative_control_input_ids) is not tuple
                or not self.negative_control_input_ids
            ):
                raise TypeError("negative-control IDs must be a nonempty tuple")
            for item in self.negative_control_input_ids:
                _text(item, "negative-control input ID")
            if len(set(self.negative_control_input_ids)) != len(self.negative_control_input_ids):
                raise ValueError("negative-control input IDs must be unique")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "reference_control_files_binding_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "request_id": self.request_id,
                    "reviewer_id": self.reviewer_id,
                    "target_input_authority_sha256": self.target_input_authority_sha256,
                    "control_strategy": self.control_strategy,
                    "positive_reference_input_ids": list(self.positive_reference_input_ids),
                    "shared_unstained_input_id": self.shared_unstained_input_id,
                    "negative_control_input_ids": list(self.negative_control_input_ids),
                    **({"prepare_shared_control": True} if self.prepare_shared_control else {}),
                }
            ),
        )


__all__ = ["BasisSourceDecisionBinding", "ReferenceControlFilesBinding"]
