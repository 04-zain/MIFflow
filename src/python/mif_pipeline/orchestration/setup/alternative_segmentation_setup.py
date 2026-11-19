"""Optional detector assets, using the existing explicit setup download lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import file_digest
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile

from mif_pipeline._software import read_software_description
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
from .setup_transfer import download_pinned_file


def detector_release(method: str) -> dict:
    if method not in {"cellpose", "stardist"}:
        raise ValueError("Choose Cellpose or StarDist")
    return read_software_description(method)


def default_detector_directory(method: str) -> Path:
    release = detector_release(method)
    explicit = os.environ.get("MIF_" + method.upper() + "_MODEL_DIRECTORY")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("The selected detector model directory must be an absolute path")
        return path
    root = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
            if sys.platform == "win32" else
            Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"))
    return root / "mif-pipeline" / method / release["version"]


@dataclass(frozen=True, slots=True)
class DetectorSetupStatus:
    state: str
    location: Path
    detail: str


def inspect_detector(method: str, directory: Path, *, verify_bytes: bool = False) -> DetectorSetupStatus:
    """Reuse installation metadata; an explicit verification may check the asset bytes."""
    release = detector_release(method)
    label = "StarDist" if method == "stardist" else "Cellpose"
    if not verify_bytes:
        try:
            if json.loads((directory / "installation.json").read_text()) != release:
                raise ValueError("Installation metadata differs from the pinned release")
        except (OSError, ValueError) as error:
            return DetectorSetupStatus("missing", directory, f"Complete {label} model setup: {error}")
    for asset in release["files"]:
        path = directory / asset["filename"]
        if not path.exists():
            return DetectorSetupStatus("missing", directory, f"Missing {asset['filename']}. Install the {method} model in Software setup.")
        if not path.is_file() or path.stat().st_size != asset["size_bytes"]:
            return DetectorSetupStatus("incompatible", directory, f"{asset['filename']} differs from the pinned release.")
        if verify_bytes:
            with path.open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != asset["sha256"]:
                    return DetectorSetupStatus("incompatible", directory, f"{asset['filename']} checksum differs from the pinned release.")
    if method == "cellpose":
        try:
            if importlib.metadata.version("cellpose") != release["code_version"]:
                raise ValueError("The application requires Cellpose " + release["code_version"])
        except (importlib.metadata.PackageNotFoundError, ValueError) as error:
            return DetectorSetupStatus("incompatible", directory, f"Cellpose runtime is unavailable: {error}. Install the application with its Cellpose dependency.")
    return DetectorSetupStatus("installed", directory, f"{label} {release['version']} is ready.")


class AlternativeSegmentationSetupService:
    """Install one exact optional model package without modifying other methods."""
    def __init__(self, method: str, *, directory: Path | None = None, opener=None):
        self.method = method
        self.release = detector_release(method)
        self.directory = directory or default_detector_directory(method)
        self.opener = opener

    def inspect(self) -> DetectorSetupStatus:
        return inspect_detector(self.method, self.directory)

    def install(self, *, consent: bool) -> DetectorSetupStatus:
        if consent is not True:
            raise ValueError("Approve the displayed optional model download before installation")
        receipt = self.directory / "installation.json"
        verified = False
        if receipt.is_file():
            try:
                verified = json.loads(receipt.read_text()) == self.release
            except (OSError, ValueError):
                pass
            status = self.inspect()
            if status.state == "installed":
                return status
        self.directory.mkdir(parents=True, exist_ok=True)
        for asset in self.release["files"]:
            target = self.directory / asset["filename"]
            if target.exists():
                if not target.is_file() or target.stat().st_size != asset["size_bytes"]:
                    raise ValueError(f"Existing {target.name} differs; setup will not overwrite it")
                if not verified:
                    with target.open("rb") as stream:
                        if file_digest(stream, "sha256").hexdigest() != asset["sha256"]:
                            raise ValueError(f"Existing {target.name} differs; setup will not overwrite it")
                continue
            with tempfile.TemporaryDirectory(prefix=".detector-install-", dir=self.directory) as temporary:
                downloaded = Path(temporary) / asset["filename"]
                download_pinned_file(asset["url"], downloaded, asset["sha256"], asset["size_bytes"],
                    opener=self.opener, maximum_bytes=asset["size_bytes"])
                publish_no_replace(downloaded, target, "win-x86_64" if sys.platform == "win32" else "linux-x86_64")
        receipt.write_text(json.dumps(self.release, sort_keys=True) + "\n")
        return inspect_detector(self.method, self.directory)


def detector_model_evidence(settings) -> dict:
    release = detector_release(settings.method)
    if settings.model_version != release["version"]:
        raise ValueError("Select the pinned model version from Software setup")
    status = inspect_detector(settings.method, settings.model_path)
    if status.state != "installed":
        raise ValueError(status.detail)
    from hashlib import sha256
    return {"model_path": str(settings.model_path), "model_version": settings.model_version,
            "evidence_sha256": sha256(json.dumps(release, sort_keys=True).encode()).hexdigest(),
            "model_files": [{"relative_path": asset["filename"], "sha256": asset["sha256"],
                             "size_bytes": asset["size_bytes"]} for asset in release["files"]]}
