"""Checks, selects and installs the public Nimbus V1 marker-expression model.

Software setup downloads the pinned checkpoint into a versioned application-data
folder and publishes it only after its byte count and SHA-256 match. An existing
local checkpoint can instead be checked and selected without network access.
The bundled scoring worker remains separate from this model setup;
scientific execution never downloads a checkpoint or changes its runtime contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import file_digest
import os
from pathlib import Path
from mif_pipeline.pipeline_core.atomic_publication import PublicationError, publish_no_replace
import re
import tempfile
from typing import Literal

from mif_pipeline._software import read_software_description
from .setup_transfer import SetupTransferError
from .setup_transfer import download_pinned_file


class NimbusSetupError(RuntimeError):
    """Reports a model download, verification or selection failure."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class NimbusSetupStatus:
    """Retains the checked checkpoint path and its Software setup explanation."""

    state: Literal["installed", "missing", "incompatible"]
    location: Path
    detail: str


@dataclass(frozen=True, slots=True)
class NimbusDownloadInfo:
    """Describes the fixed public model before download confirmation."""

    version: str
    approximate_size_bytes: int
    sources: tuple[str, ...]
    sha256: str
    license: str
    license_url: str


def _release() -> dict:
    """Reads the supported checkpoint and its immutable download address."""

    try:
        model = read_software_description("nimbus")["model"]
        archive = model["archive"]
        revision = model["revision"]
        base = "https://huggingface.co/JLrumberger/Nimbus-Inference"
        if (
            model["name"] != "V1"
            or model["filename"] != "V1.pt"
            or model["package_version"] != "0.0.5"
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or archive["url"] != f"{base}/resolve/{revision}/V1.pt"
            or type(archive["size_bytes"]) is not int
            or not 0 < archive["size_bytes"] <= 512 * 1024 * 1024
            or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
            or model["license"] != "MIT"
            or model["license_url"] != f"{base}/blob/{revision}/README.md"
        ):
            raise ValueError("Unsupported Nimbus model description")
        return model
    except (KeyError, TypeError, ValueError) as exc:
        raise NimbusSetupError("pin_record_invalid", "Cannot read the installed Nimbus model description") from exc


def nimbus_download_info() -> NimbusDownloadInfo:
    """Returns the model version, source, size and license shown before installation."""

    model = _release()
    archive = model["archive"]
    return NimbusDownloadInfo(
        model["name"], archive["size_bytes"], (archive["url"],),
        archive["sha256"], model["license"], model["license_url"],
    )


def _absolute(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise ValueError("Select an absolute Nimbus checkpoint path without traversal.")
    return path


def default_nimbus_model_target(
    platform: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Locates the shared user checkpoint under its exact model revision."""

    if platform not in {"linux-x86_64", "win-x86_64"}:
        raise ValueError("Unsupported Nimbus setup platform")
    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    root = (
        Path(values.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        if platform == "win-x86_64"
        else Path(values.get("XDG_DATA_HOME") or user_home / ".local" / "share")
    )
    model = _release()
    return _absolute(root) / "mif-pipeline" / "nimbus" / model["revision"] / model["filename"]


def _safe_parent(path: Path, *, create: bool) -> tuple[int, int]:
    """Checks the managed destination and retains its identity during transfer."""

    for item in (*reversed(path.parents), path):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise NimbusSetupError("unsafe_target", "The model installation folder contains a link.")
        if not item.exists() and create:
            item.mkdir(mode=0o700)
        if not item.is_dir():
            raise NimbusSetupError("unsafe_target", "The model installation parent is not a directory.")
    value = path.stat()
    return value.st_dev, value.st_ino


class NimbusSetupService:
    """Provides off-thread model checking, offline selection and explicit installation."""

    def __init__(
        self,
        *,
        platform: str,
        checkpoint_path: Path | None = None,
        managed_target: Path | None = None,
        preferences_path: Path | None = None,
        opener=None,
    ) -> None:
        if platform not in {"linux-x86_64", "win-x86_64"}:
            raise ValueError("Unsupported Nimbus setup platform")
        self.platform = platform
        self.target = _absolute(managed_target) if managed_target is not None else default_nimbus_model_target(platform)
        self.selected = _absolute(checkpoint_path) if checkpoint_path is not None else None
        self.preferences_path = _absolute(preferences_path) if preferences_path is not None else None
        self.opener = opener

    def _check(self, location: Path) -> NimbusSetupStatus:
        pin = _release()["archive"]
        try:
            if not location.exists() and not location.is_symlink():
                return NimbusSetupStatus("missing", location, "Nimbus model checkpoint is missing. Choose Install or Use existing.")
            if not location.is_file() or location.stat().st_size != pin["size_bytes"]:
                raise ValueError("Checkpoint file or byte count differs from the pinned Nimbus V1 model.")
            with location.open("rb") as source:
                if file_digest(source, "sha256").hexdigest() != pin["sha256"]:
                    raise ValueError("Checkpoint SHA-256 differs from the pinned Nimbus V1 model.")
        except (OSError, ValueError) as exc:
            return NimbusSetupStatus("incompatible", location, str(exc))
        return NimbusSetupStatus("installed", location, "Nimbus V1 model verified. Bundled scoring software is checked separately.")

    def inspect(self) -> NimbusSetupStatus:
        """Checks the explicit or saved selection, then the managed checkpoint."""

        location = self.selected
        if location is None and self.preferences_path is not None:
            from mif_pipeline.pipeline_core.software_preferences import load_desktop_preferences

            preferences = load_desktop_preferences(self.preferences_path)
            selected = getattr(preferences, "nimbus_checkpoint_path", None)
            location = Path(selected) if selected else None
        return self._check(location or self.target)

    def _preferences(self):
        if self.preferences_path is None:
            return None
        from mif_pipeline.pipeline_core.software_preferences import load_desktop_preferences

        preferences = load_desktop_preferences(self.preferences_path)
        if preferences is None:
            raise NimbusSetupError("selection_required", "Select QuPath in Software setup before saving the Nimbus model location.")
        return preferences

    def _remember(self, status: NimbusSetupStatus) -> NimbusSetupStatus:
        if status.state != "installed":
            raise NimbusSetupError("checkpoint_incompatible", status.detail)
        preferences = self._preferences()
        if preferences is not None:
            from mif_pipeline.pipeline_core.software_preferences import write_desktop_preferences

            write_desktop_preferences(
                self.preferences_path, replace(preferences, nimbus_checkpoint_path=str(status.location))
            )
        self.selected = status.location
        return status

    def select(self, location: Path) -> NimbusSetupStatus:
        """Verifies and saves an existing checkpoint without a download or copy."""

        return self._remember(self._check(_absolute(location)))

    def install(self, *, consent: bool) -> NimbusSetupStatus:
        """Downloads and atomically publishes one verified checkpoint without replacement."""

        if type(consent) is not bool:
            raise TypeError("Download consent must be bool")
        status = self._check(self.target)
        if status.state == "installed":
            return self._remember(status)
        if status.state != "missing":
            raise NimbusSetupError("existing_conflict", "Existing Nimbus model files will not be overwritten. Use an existing verified checkpoint or resolve the files listed in Details.")
        if not consent:
            raise NimbusSetupError("consent_required", "Approve the pinned Nimbus model download before installation.")
        self._preferences()
        pin = _release()["archive"]
        try:
            identity = _safe_parent(self.target.parent, create=True)
            with tempfile.TemporaryDirectory(prefix=".nimbus-install-", dir=self.target.parent) as temporary:
                checkpoint = Path(temporary) / "V1.pt"
                download_pinned_file(pin["url"], checkpoint, pin["sha256"], pin["size_bytes"], opener=self.opener)
                if _safe_parent(self.target.parent, create=False) != identity:
                    raise NimbusSetupError("publication_failed", "Model installation parent changed during the download.")
                publish_no_replace(checkpoint, self.target, self.platform)
        except (SetupTransferError, PublicationError) as exc:
            raise NimbusSetupError(exc.code, exc.detail) from exc
        except OSError as exc:
            raise NimbusSetupError("publication_failed", "Nimbus setup could not safely publish the checkpoint.") from exc
        return self._remember(NimbusSetupStatus("installed", self.target, "Nimbus V1 model verified and installed."))
