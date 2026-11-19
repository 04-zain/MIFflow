"""Automatic unresolved settings drafts for desktop setup.

An unfinished mapping is a draft, never analysis authority. The pipeline-core parser is the
sole scientific configuration validator."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping

from mif_pipeline.pipeline_core.config import SCHEMA_VERSION as ANALYSIS_CONFIG_SCHEMA_VERSION
from .manifest_draft import ManifestDraft

# ==============================================================================
#                               Analysis settings
# ==============================================================================


def copy_settings(value: object) -> object:
    """Detach JSON-shaped immutable or mutable settings for presentation edits."""
    if isinstance(value, Mapping):
        return {key: copy_settings(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [copy_settings(item) for item in value]
    return value


def new_analysis_settings(draft: ManifestDraft) -> dict[str, object]:
    """Start a visible train-new workflow with unresolved scientific inputs."""
    first = next((sample for sample in draft.samples if sample.enabled), None)
    from mif_pipeline.pipeline_core.config_draft import unresolved_analysis_settings

    return unresolved_analysis_settings(
        draft.batch_id, draft.output_root, first.slide_id if first else None
    )


__all__ = ["copy_settings", "new_analysis_settings"]
