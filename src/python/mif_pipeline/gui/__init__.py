"""Local desktop presentation adapters for the mIF pipeline."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from importlib import import_module

_EXPORTS_BY_MODULE = {
    "desktop_services": (
        "BatchServicePort",
        "BatchServiceProvider",
        "DesktopFileOperations",
    ),
    "interactions": (
        "ControlKind",
        "FormControl",
        "FormModel",
        "FormTable",
        "FormTableRow",
        "apply_stage1_form",
        "build_local_file_form",
        "build_roi_interaction_form",
        "build_stage1_form",
        "submit_form",
    ),
    "manifest_draft": (
        "DraftFieldError",
        "ExecutionDraft",
        "ManifestDraft",
        "ManifestDraftValidation",
        "ProjectChoice",
        "ReusableArtifactDraft",
        "SampleDraft",
        "load_manifest_draft",
        "validate_manifest_draft",
        "write_manifest_draft",
    ),
    "source_series": (
        "LocalSourceSeriesInspectionService",
        "SourceSeriesInspectionPort",
    ),
}
_MODULE_BY_EXPORT = {name: module for module, names in _EXPORTS_BY_MODULE.items() for name in names}


# ==============================================================================
#                               Public interfaces
# ==============================================================================


def __getattr__(name: str) -> object:
    module = _MODULE_BY_EXPORT.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "BatchServicePort",
    "BatchServiceProvider",
    "ControlKind",
    "DesktopFileOperations",
    "DraftFieldError",
    "ExecutionDraft",
    "FormControl",
    "FormModel",
    "FormTable",
    "FormTableRow",
    "LocalSourceSeriesInspectionService",
    "ManifestDraft",
    "ManifestDraftValidation",
    "ProjectChoice",
    "ReusableArtifactDraft",
    "SampleDraft",
    "SourceSeriesInspectionPort",
    "apply_stage1_form",
    "build_local_file_form",
    "build_roi_interaction_form",
    "build_stage1_form",
    "load_manifest_draft",
    "submit_form",
    "validate_manifest_draft",
    "write_manifest_draft",
]
