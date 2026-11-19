"""Deterministic lookup of a selected managed analysis folder."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path

# ==============================================================================
#                               Analysis location
# ==============================================================================


def resolve_analysis_folder(folder: Path) -> Path:
    """Choose the published manifest before authoring state; never repair it."""
    if not isinstance(folder, Path) or not folder.is_absolute():
        raise ValueError("Select an absolute analysis folder")
    if not folder.is_dir():
        raise ValueError("The selected analysis folder is not a directory")
    for filename in ("analysis.manifest.json", "analysis.setup.json"):
        candidate = folder / filename
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        return candidate
    raise ValueError("The selected folder contains no saved analysis")
