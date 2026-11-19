"""Typed, bounded software requirements for a resumable Nimbus marker review."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mif_pipeline.cell_analysis.nimbus import NimbusScoreError


_FIELDS_BY_CODE = {
    "runtime_missing": "nimbus_runtime",
    "runtime_incomplete": "nimbus_runtime",
    "runtime_incompatible": "nimbus_runtime",
    "preferences_unreadable": "nimbus_checkpoint_path",
    "checkpoint_missing": "nimbus_checkpoint_path",
    "checkpoint_unreadable": "nimbus_checkpoint_path",
    "magnification_missing": "nimbus_magnification",
}


@dataclass(frozen=True, slots=True)
class NimbusSoftwareRequirement:
    """Identifies a processing-computer setting that the researcher can repair."""

    code: str
    message: str
    field: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _FIELDS_BY_CODE:
            raise ValueError("Unsupported Nimbus software requirement")
        if self.field != _FIELDS_BY_CODE[self.code]:
            raise ValueError("Nimbus software requirement names the wrong setting")
        if type(self.message) is not str or not self.message.strip() or len(self.message) > 1024:
            raise ValueError("Nimbus software requirement needs a bounded explanation")

    def to_mapping(self) -> dict[str, str]:
        return {"software": "nimbus", "code": self.code,
                "message": self.message, "field": self.field}

    @classmethod
    def from_mapping(cls, value: object) -> NimbusSoftwareRequirement:
        if (not isinstance(value, Mapping)
                or set(value) != {"software", "code", "message", "field"}
                or value["software"] != "nimbus"):
            raise ValueError("Nimbus software requirement has invalid fields")
        return cls(code=value["code"], message=value["message"], field=value["field"])


class NimbusPrerequisiteError(NimbusScoreError):
    """A software failure recoverable without abandoning the current marker."""

    def __init__(self, code: str, message: str):
        self.requirement = NimbusSoftwareRequirement(code, message, _FIELDS_BY_CODE[code])
        super().__init__(message)
