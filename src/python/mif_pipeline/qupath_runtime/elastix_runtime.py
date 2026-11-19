"""

Resolves the selected Elastix release and local installation directory.

We read the operating-system download and required-file list from
config/software/elastix.json. Software setup uses that record to inspect
or install the programs. Registration parameters are loaded separately
from config/defaults/registration.json.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from mif_pipeline._software import read_software_description
import os
import re
import subprocess

VERSION = "5.3.1"


def check_elastix_programs(directory, *, process_runner=None, platform=None):
    """Run both selected programs once to check version and shared-library loading."""
    from .prerequisite_probe import _run_bounded_probe

    platform = platform or ("win-x86_64" if os.name == "nt" else "linux-x86_64")
    suffix = ".exe" if platform == "win-x86_64" else ""
    runner = process_runner or _run_bounded_probe
    for name in ("elastix", "transformix"):
        program = Path(directory) / (name + suffix)
        if not program.is_file() or not os.access(program, os.X_OK):
            raise ValueError(f"{name.capitalize()} is missing or cannot run: {program}. Open Software setup to select its installation.")
        try:
            done = runner((str(program), "--version"), env=dict(os.environ), timeout=15.0)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"{name.capitalize()} could not start. Open Software setup and select Check again. {error}") from error
        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")
        output = decoded(done.stdout)
        if (done.returncode != 0 or len(output) > 65536
                or not re.search(r"(?i)" + name + r"\s+(?:version:?\s*)?"
                                 + re.escape(VERSION) + r"(?:\s|$)", output)):
            detail = decoded(done.stderr).strip() or output.strip()
            guidance = ("Install the Microsoft Visual C++ 2015–2022 x64 Redistributable if a VCRUNTIME or MSVCP DLL is missing. "
                        if platform == "win-x86_64" else "")
            raise ValueError(f"{name.capitalize()} did not run as version {VERSION}. "
                             + guidance + "Open Software setup and select Check again."
                             + (" " + detail[:1000] if detail else ""))
    return f"Elastix and Transformix {VERSION} both run."

# ==============================================================================
#                                Elastix runtime
# ==============================================================================


def release_pin(platform):
    """

    Reads the selected platform's Elastix archive and required program files.

    Args:
        platform: linux-x86_64 or win-x86_64.

    Returns:
        Independent dictionary containing the download URL, byte count, checksum,
        program subfolder and required installed files.

    Raises:
        ValueError: The record identifies an unsupported release or platform.
    """

    record = read_software_description("elastix")
    if record["version"] != VERSION or platform not in record["platforms"]:
        raise ValueError("Automatic alignment setup supports Linux x86-64 and Windows x86-64.")
    return record["platforms"][platform]


def default_elastix_root(platform=None):
    """

    Selects the versioned folder under the user's application-data directory.

    Windows uses LOCALAPPDATA and Linux uses XDG_DATA_HOME, with their usual
    home-directory locations when those variables are absent. The resulting
    folder is shared by analyses using this installed release.

    Args:
        platform: Optional supported platform; omission selects the current
            operating system.

    Returns:
        Path ending in mif-pipeline/registration/elastix/<version>.

    Raises:
        ValueError: The platform is unsupported.
    """

    platform = platform or ("win-x86_64" if os.name == "nt" else "linux-x86_64")
    if platform == "win-x86_64":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif platform == "linux-x86_64":
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    else:
        raise ValueError("Automatic alignment setup supports Linux x86-64 and Windows x86-64.")
    return base / "mif-pipeline" / "registration" / "elastix" / VERSION
