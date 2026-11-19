"""Closed audit binding for a validated classifier-settings revision."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, fields
import re
from typing import ClassVar

from .batch_artifacts import BatchArtifactError, _closed_mapping, _sha256

_NODE_ID = re.compile(r"node-[0-9a-f]{48}\Z")


# ==============================================================================
#                            Batch training settings
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TrainingSettingsRevision:
    """Immutable wire identity; the state owner separately proves its authority."""

    schema_version: ClassVar[str] = "training-settings-revision/v1"

    predecessor_plan_review_node_id: str
    predecessor_training_plan_set_sha256: str
    accepted_response_sha256: str
    response_authorization_sha256: str
    predecessor_review_sha256: str
    adjusted_after_validation: bool

    def __post_init__(self) -> None:
        if (
            type(self.predecessor_plan_review_node_id) is not str
            or _NODE_ID.fullmatch(self.predecessor_plan_review_node_id) is None
        ):
            raise BatchArtifactError("predecessor plan-review node ID is invalid")
        for field in (
            "predecessor_training_plan_set_sha256",
            "accepted_response_sha256",
            "response_authorization_sha256",
            "predecessor_review_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        if type(self.adjusted_after_validation) is not bool:
            raise BatchArtifactError("adjusted_after_validation must be boolean")

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingSettingsRevision":
        names = tuple(item.name for item in fields(cls))
        mapping = _closed_mapping(
            value,
            fields=frozenset({"schema_version", *names}),
            field="training settings revision",
        )
        if mapping["schema_version"] != cls.schema_version:
            raise BatchArtifactError("training settings revision schema is unsupported")
        return cls(**{name: mapping[name] for name in names})

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "predecessor_plan_review_node_id": self.predecessor_plan_review_node_id,
            "predecessor_training_plan_set_sha256": self.predecessor_training_plan_set_sha256,
            "accepted_response_sha256": self.accepted_response_sha256,
            "response_authorization_sha256": self.response_authorization_sha256,
            "predecessor_review_sha256": self.predecessor_review_sha256,
            "adjusted_after_validation": self.adjusted_after_validation,
        }


__all__ = ["TrainingSettingsRevision"]
