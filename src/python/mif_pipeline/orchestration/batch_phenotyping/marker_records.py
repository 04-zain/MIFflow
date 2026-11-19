"""Closed records for marker-gate interaction responses.

The `SectionOverride` record carries `section_id`, `selection_source`, `manual_threshold`,
`reason`, `reviewer_id` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from math import isfinite
import re
from typing import Final


# ==============================================================================
#                                 Marker records
# ==============================================================================


class MarkerResponseError(ValueError):
    """A marker-gate response is not a valid closed contract."""


_SOURCES: Final[frozenset[str]] = frozenset({"manual", "triangle", "gmm", "gamma", "nimbus", "nimbus_distilled"})
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarkerResponseError(f"{field} must be nonblank canonical text")
    return value


def _digest(value: object, field: str) -> str:
    value = _text(value, field)
    if _SHA256.fullmatch(value) is None:
        raise MarkerResponseError(f"{field} must be lowercase SHA-256")
    return value


def _threshold(value: object, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarkerResponseError(f"{field} must be finite numeric")
    try:
        finite = isfinite(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise MarkerResponseError(f"{field} must be finite numeric") from exc
    if not finite:
        raise MarkerResponseError(f"{field} must be finite numeric")
    return value


def _adjustment(value: object) -> None:
    intensity = value.adjusted_threshold
    score = value.adjusted_score_cutoff
    source = value.adjustment_source
    if source is None:
        if intensity is not None or score is not None:
            raise MarkerResponseError("an adjusted decision must record drag or numeric input")
        return
    if source not in {"drag", "numeric"} or (intensity is None) == (score is None):
        raise MarkerResponseError("adjust exactly one intensity gate or score cutoff")
    if score is not None:
        _threshold(score, "adjusted_score_cutoff")
        if not 0 <= score <= 1 or value.selection_source != "nimbus":
            raise MarkerResponseError("only direct Nimbus uses a score cutoff between zero and one")
    else:
        _threshold(intensity, "adjusted_threshold")
        if value.selection_source == "nimbus":
            raise MarkerResponseError("direct Nimbus adjustments use score units")


@dataclass(frozen=True, slots=True)
class SectionOverride:
    section_id: str
    selection_source: str
    manual_threshold: float | int | None
    reason: str
    reviewer_id: str
    review_record_id: str

    def __post_init__(self) -> None:
        _text(self.section_id, "section_id")
        if self.selection_source not in _SOURCES:
            raise MarkerResponseError("section selection_source is unsupported")
        if self.selection_source == "manual":
            _threshold(self.manual_threshold, "manual_threshold")
        elif self.manual_threshold is not None:
            raise MarkerResponseError("non-manual section selection cannot supply threshold")
        _text(self.reason, "reason")
        _text(self.reviewer_id, "reviewer_id")
        _text(self.review_record_id, "review_record_id")


@dataclass(frozen=True, slots=True)
class SlideOverride:
    """An explicit method or manual cutoff for one sample/image slide identity."""

    image_identity: str
    selection_source: str
    manual_threshold: float | int | None
    reason: str
    reviewer_id: str
    review_record_id: str
    adjusted_threshold: float | None = None
    adjusted_score_cutoff: float | None = None
    adjustment_source: str | None = None

    def __post_init__(self) -> None:
        _adjustment(self)
        import json
        try:
            identity = json.loads(self.image_identity)
            if not isinstance(identity, list) or len(identity) != 3:
                raise ValueError()
            for item in identity:
                _text(item, "slide identity component")
            if json.dumps(identity, separators=(",", ":"), ensure_ascii=False) != self.image_identity:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise MarkerResponseError("image_identity must be canonical sample/entry/slide JSON") from exc
        if self.selection_source not in _SOURCES:
            raise MarkerResponseError("slide selection_source is unsupported")
        if self.selection_source == "manual":
            _threshold(self.manual_threshold, "manual_threshold")
        elif self.manual_threshold is not None:
            raise MarkerResponseError("non-manual slide selection cannot supply threshold")
        _text(self.reason, "reason")
        _text(self.reviewer_id, "reviewer_id")
        _text(self.review_record_id, "review_record_id")


@dataclass(frozen=True, slots=True)
class MarkerSelection:
    marker_id: str
    selection_source: str
    manual_threshold: float | int | None
    reviewer_id: str
    review_record_id: str
    section_overrides: tuple[SectionOverride, ...] = ()
    slide_overrides: tuple[SlideOverride, ...] = ()
    nimbus_distill: bool = False
    adjusted_threshold: float | None = None
    adjusted_score_cutoff: float | None = None
    adjustment_source: str | None = None

    def __post_init__(self) -> None:
        _text(self.marker_id, "marker_id")
        _adjustment(self)
        if type(self.nimbus_distill) is not bool or (self.nimbus_distill and self.selection_source != "nimbus"):
            raise MarkerResponseError("Nimbus distillation requires a direct Nimbus review")
        if self.selection_source not in _SOURCES:
            raise MarkerResponseError("selection_source is unsupported")
        if self.selection_source == "manual":
            if self.manual_threshold is not None or not self.slide_overrides:
                _threshold(self.manual_threshold, "manual_threshold")
        elif self.manual_threshold is not None:
            raise MarkerResponseError("non-manual selection cannot supply threshold")
        _text(self.reviewer_id, "reviewer_id")
        _text(self.review_record_id, "review_record_id")
        if not isinstance(self.section_overrides, tuple) or any(
            not isinstance(item, SectionOverride) for item in self.section_overrides
        ):
            raise MarkerResponseError("section_overrides must be typed records")
        ids = tuple(item.section_id for item in self.section_overrides)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise MarkerResponseError("section_overrides must be sorted and unique")
        if not isinstance(self.slide_overrides, tuple) or any(not isinstance(item, SlideOverride) for item in self.slide_overrides):
            raise MarkerResponseError("slide_overrides must be typed records")
        slide_ids = tuple(item.image_identity for item in self.slide_overrides)
        if slide_ids != tuple(sorted(set(slide_ids))):
            raise MarkerResponseError("slide_overrides must be sorted and unique")


@dataclass(frozen=True, slots=True)
class MarkerGateResponse:
    validation_domain_id: str
    evidence_sha256: str
    selections: tuple[MarkerSelection, ...]
    refit_current_marker: bool = False
    marker_settings: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _text(self.validation_domain_id, "validation_domain_id")
        _digest(self.evidence_sha256, "evidence_sha256")
        if not isinstance(self.selections, tuple) or any(
            not isinstance(item, MarkerSelection) for item in self.selections
        ):
            raise MarkerResponseError("selections must be typed records")
        if type(self.refit_current_marker) is not bool:
            raise MarkerResponseError("refit_current_marker must be boolean")
        if self.marker_settings is not None and (not self.refit_current_marker or not isinstance(self.marker_settings, Mapping)):
            raise MarkerResponseError('Revised settings require a marker refit response.')
        if self.refit_current_marker and self.selections:
            raise MarkerResponseError("refitting cannot also accept marker decisions")
        if not self.selections and not self.refit_current_marker:
            raise MarkerResponseError("selections must not be empty")
        ids = tuple(item.marker_id for item in self.selections)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise MarkerResponseError("selections must be sorted and unique")


__all__ = ["MarkerGateResponse", "MarkerResponseError", "MarkerSelection", "SectionOverride", "SlideOverride"]
