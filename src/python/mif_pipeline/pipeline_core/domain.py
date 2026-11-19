"""Neutral immutable analysis-domain identity primitives.

The `AnalysisDomainRef` record carries `analysis_domain_id`, `kind`, `slide_id`,
`section_id`, `tissue_generation_id`.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re

from .errors import ROIAnalysisError

_SHA256 = re.compile(r"[0-9a-f]{64}")


# ==============================================================================
#                                     Domain
# ==============================================================================


class AnalysisDomainKind(str, Enum):
    """Supported biological analysis scopes."""

    WHOLE_TISSUE_SECTION = "whole_tissue_section"
    SELECTED_ROI = "selected_roi"


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    result = _nonblank(value, field)
    if _SHA256.fullmatch(result) is None:
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256")
    return result


def analysis_domain_id(
    *,
    kind: AnalysisDomainKind,
    canonical_image_sha256: str,
    tissue_generation_id: str,
    section_id: str,
    source_path_object_id: str,
) -> str:
    """Derive a stable path-independent domain identity."""
    if not isinstance(kind, AnalysisDomainKind):
        raise ROIAnalysisError("kind must be an AnalysisDomainKind")
    payload = {
        "canonical_image_sha256": _sha256(canonical_image_sha256, "canonical_image_sha256"),
        "kind": kind.value,
        "schema_version": "analysis-domain/v1",
        "section_id": _nonblank(section_id, "section_id"),
        "source_path_object_id": _nonblank(source_path_object_id, "source_path_object_id"),
        "tissue_generation_id": _nonblank(tissue_generation_id, "tissue_generation_id"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalysisDomainRef:
    """Shared reference to one immutable analysis-domain identity."""

    analysis_domain_id: str
    kind: AnalysisDomainKind
    slide_id: str
    section_id: str
    tissue_generation_id: str

    def __post_init__(self) -> None:
        _sha256(self.analysis_domain_id, "analysis_domain_id")
        if not isinstance(self.kind, AnalysisDomainKind):
            raise ROIAnalysisError("kind must be an AnalysisDomainKind")
        for field in ("slide_id", "section_id", "tissue_generation_id"):
            _nonblank(getattr(self, field), field)


__all__ = ["AnalysisDomainKind", "AnalysisDomainRef", "analysis_domain_id"]
