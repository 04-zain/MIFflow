"""Prepare read-only image access; never execute a mount or adopt remote state.

Paths here are viewing locations, not authenticated analysis inputs. The cluster remains the
writer of its analysis and published image generations. No per-tile hashing or filesystem
traversal is needed to prepare the connection command."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
import shlex
import sys

# ==============================================================================
#                                 Mounted images
# ==============================================================================


def _clean(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        raise ValueError(f"Enter a complete {label}.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"The {label} contains an unsupported control character.")
    return value


def _local_path(value: str):
    _clean(value, "local folder path")
    path = (
        PureWindowsPath(value)
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")
        else PurePosixPath(value)
    )
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Enter an absolute local mount path without parent-directory shortcuts.")
    return path


@dataclass(frozen=True)
class MountedImageAccess:
    host: str
    cluster_folder: str
    local_folder: str

    def __post_init__(self):
        if not isinstance(self.host, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}", self.host
        ):
            raise ValueError("Enter the cluster file-access host or its saved SSH alias.")
        remote = PurePosixPath(_clean(self.cluster_folder, "cluster image folder"))
        if not remote.is_absolute() or ".." in remote.parts:
            raise ValueError(
                "Enter an absolute cluster image folder without parent-directory shortcuts."
            )
        _local_path(self.local_folder)

    def command(self, *, platform: str | None = None) -> str:
        local = _local_path(self.local_folder)
        if (platform or sys.platform) == "win32" or isinstance(local, PureWindowsPath):
            raise ValueError(
                "On Windows, connect the folder through your installed SSHFS/WinFsp client in read-only mode. Use the mounted drive path for local viewing."
            )
        arguments = [
            "sshfs",
            "-o",
            "ro",
            "-o",
            "reconnect",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=yes",
            self.host + ":" + self.cluster_folder,
            self.local_folder,
        ]
        return shlex.join(arguments)

    def cluster_path(self, local_image: str) -> str:
        root = _local_path(self.local_folder)
        image = _local_path(local_image)
        try:
            relative = image.relative_to(root)
        except ValueError as error:
            raise ValueError("Select an image within the configured mounted folder.") from error
        if not relative.parts:
            raise ValueError("Select an image file within the mounted folder.")
        return str(PurePosixPath(self.cluster_folder).joinpath(*relative.parts))

    def local_path(self, cluster_image: str) -> str:
        remote = PurePosixPath(_clean(cluster_image, "cluster image path"))
        if not remote.is_absolute() or ".." in remote.parts:
            raise ValueError("Choose an absolute image path within the mounted cluster folder.")
        try:
            relative = remote.relative_to(PurePosixPath(self.cluster_folder))
        except ValueError as error:
            raise ValueError(
                "This image is outside the configured mounted cluster folder."
            ) from error
        if not relative.parts:
            raise ValueError("Choose an image file within the mounted cluster folder.")
        return str(_local_path(self.local_folder).joinpath(*relative.parts))

    def to_mapping(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict) or set(value) != {"host", "cluster_folder", "local_folder"}:
            raise ValueError(
                "Mounted image settings require the host, cluster image folder and local mount folder."
            )
        return cls(**value)


MOUNTED_IMAGE_GUIDANCE = """Connect a shared cluster folder to this computer in read-only mode using the installed SSHFS client. Select images from that folder on Samples. QuPath reads images from the mounted folder for viewing and annotation.

The current review, opened from Run, prepares a separate local annotation project. Choose Annotate image, save the annotations in QuPath and close it. The application returns the saved draft automatically when automatic return is selected. Current image, model and review checks are applied before the cluster accepts the annotations.

The image-access connection normally uses the same host as job submission. A different host is optional when your institution provides a separate server for file access."""
