"""Shared resolution and bounded version probe for native QuPath.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys

_STABLE_QUPATH_07 = re.compile(r"(?<![0-9.])0\.7\.([0-9]+)(?![0-9A-Za-z_.-])")


# ==============================================================================
#                                   Executable
# ==============================================================================


@dataclass(frozen=True, slots=True)
class QuPathExecutableInfo:
    """One resolved executable and its freshly probed stable version."""

    path: Path
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("QuPath executable info requires an absolute path")
        if type(self.version) is not str or _STABLE_QUPATH_07.fullmatch(self.version) is None:
            raise ValueError("QuPath executable info requires stable version 0.7.x")


def _is_native_bare_command_name(
    text: str,
    *,
    platform: str | None = None,
) -> bool:
    selected = sys.platform if platform is None else platform
    path_type = PureWindowsPath if selected == "win32" else PurePosixPath
    lexical = path_type(text)
    return (
        not lexical.drive
        and not lexical.root
        and lexical.parent == path_type()
        and lexical.name == text
    )


def _resolve_executable(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("QuPath executable must be non-empty")
    text = str(value).strip()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        if not _is_native_bare_command_name(text):
            raise ValueError(
                "QuPath executable must be absolute or platform-discoverable " "bare command name"
            )
        discovered = shutil.which(text)
        if discovered is None:
            raise ValueError("QuPath executable must be absolute or platform-discoverable")
        candidate = Path(discovered).expanduser()
    executable = candidate.resolve(strict=False)
    if not executable.is_file():
        raise ValueError(f"QuPath executable does not exist or is not a file: {executable}")
    if not os.access(executable, os.X_OK):
        raise ValueError(f"QuPath executable is not executable: {executable}")
    return executable


def resolve_and_probe_qupath(
    value: str | Path,
    *,
    timeout_seconds: float = 60.0,
) -> QuPathExecutableInfo:
    """Resolve and freshly validate one supported stable QuPath 0.7.x runtime."""

    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("QuPath probe timeout must be a positive finite number")
    executable = _resolve_executable(value)
    command = (str(executable), "--version")
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("QuPath --version probe timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"QuPath --version probe failed with exit status {exc.returncode}"
        ) from exc
    except OSError as exc:
        raise ValueError("QuPath --version probe could not execute") from exc
    reported = "\n".join((completed.stdout or "", completed.stderr or ""))
    match = _STABLE_QUPATH_07.search(reported)
    if match is None:
        raise ValueError("ROI analysis requires a supported stable QuPath 0.7.x runtime")
    return QuPathExecutableInfo(executable, f"0.7.{match.group(1)}")


__all__ = ["QuPathExecutableInfo", "resolve_and_probe_qupath"]
