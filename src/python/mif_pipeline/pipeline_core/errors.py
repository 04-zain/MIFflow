"""Shared ROI-analysis errors and filesystem identity guards."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path

# ==============================================================================
#                                     Errors
# ==============================================================================


class ROIAnalysisError(ValueError):
    """Raised when ROI-analysis inputs or derived records violate a contract."""


class ProjectPreparationError(ROIAnalysisError):
    """Raised when stage-owned project content conflicts with this run."""


def validate_safe_path_segment(
    value: object,
    field: str,
    *,
    error_type: type[ROIAnalysisError] = ROIAnalysisError,
) -> str:
    """Return one non-special path segment or raise the requested domain error."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise error_type(f"{field} must be a safe non-empty path segment")
    return value


__all__ = [
    "ProjectPreparationError",
    "ROIAnalysisError",
    "validate_safe_path_segment",
]
