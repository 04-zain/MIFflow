"""Read-only candidate enumeration at a fixed set of local installation roots.

Candidates are suggestions only. The first-run service delegates executable and version
validation to the QuPath runtime owner before offering them."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys

# ==============================================================================
#                                Qupath discovery
# ==============================================================================


def qupath_installation_candidates(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    checkout_root: Path | None = None,
    linux_install_root: Path | None = None,
) -> tuple[Path, ...]:
    """Find native launcher filenames on PATH and in shallow known layouts.

    No recursive search, download, preference write, or executable probe occurs
    here. Explicit roots make enumeration testable without machine state.
    """
    selected = sys.platform if platform is None else platform
    if selected not in {"linux", "win32"}:
        return ()
    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else home
    checkout = Path(__file__).resolve().parents[3] if checkout_root is None else checkout_root
    names = ("QuPath.exe",) if selected == "win32" else ("QuPath", "qupath")
    candidates: list[Path] = []
    separator = ";" if selected == "win32" else ":"
    for entry in env.get("PATH", "").split(separator):
        if not entry:
            continue
        directory = Path(entry)
        if directory.is_absolute():
            candidates.extend(directory / name for name in names)

    searches: list[tuple[Path, str]]
    if selected == "linux":
        searches = [
            (checkout / ".tools", "qupath*/bin/QuPath"),
            (
                Path("/opt") if linux_install_root is None else linux_install_root,
                "QuPath*/bin/QuPath",
            ),
            (user_home / "Applications", "QuPath*/bin/QuPath"),
        ]
    else:
        searches = [(checkout / ".tools", "qupath*/QuPath*.exe")]
        for key in ("LOCALAPPDATA", "ProgramFiles"):
            value = env.get(key)
            if value and Path(value).is_absolute():
                searches.append((Path(value), "QuPath*/QuPath*.exe"))

    for directory, pattern in searches:
        try:
            candidates.extend(sorted(directory.glob(pattern)))
        except OSError:
            # An inaccessible optional root is not an installed candidate.
            continue

    existing: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_file():
                existing.append(candidate)
        except OSError:
            continue
    return tuple(existing)
