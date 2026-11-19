"""Immutable candidate profile selection, without readiness or mutation authority."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Literal

# ==============================================================================
#                                 Launch binding
# ==============================================================================


@dataclass(frozen=True, slots=True)
class QuPathProfileLaunchBinding:
    executable: Path
    qupath_version: Literal["0.7.0"]
    profile_root: Path
    generation: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        for name in ("executable", "profile_root"):
            path = getattr(self, name, None)
            if not isinstance(path, Path):
                raise TypeError(f"{name} must be a Path")
            if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
                raise ValueError(f"{name} must be an absolute path without traversal or NUL")
        version = getattr(self, "qupath_version", None)
        if type(version) is not str or version != "0.7.0":
            raise ValueError("managed profile requires QuPath 0.7.0")
        for name, length in (("generation", 32), ("manifest_sha256", 64)):
            value = getattr(self, name, None)
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
                raise ValueError(f"{name} must be {length} lowercase hexadecimal characters")

    @property
    def preferences_namespace(self) -> str:
        return f"mif-warpy-{self.generation}"


def qupath_command(
    executable: str | Path,
    *arguments: str,
    launch_binding: QuPathProfileLaunchBinding | None = None,
) -> tuple[str, ...]:
    """Build the native command from the admitted immutable profile selection.

    We retain the executable comparison here because it belongs to this launch.
    Profile fields were checked when the binding was constructed; current files
    and loaded extension classes are checked by their runtime owners. Without a
    selected binding, use the executable's normal profile.
    """
    if launch_binding is None:
        return (os.fspath(executable), *arguments)
    if type(launch_binding) is not QuPathProfileLaunchBinding:
        raise TypeError("launch_binding must be a QuPathProfileLaunchBinding")
    if Path(executable) != launch_binding.executable:
        raise ValueError("QuPath executable differs from the selected launch binding")
    return (
        os.fspath(executable),
        "-Dqupath.prefs.name=" + launch_binding.preferences_namespace,
        *arguments,
    )
