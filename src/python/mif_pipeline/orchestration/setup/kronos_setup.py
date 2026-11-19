"""Select a user-obtained KRONOS package for the installed application runtime.

Software setup retains the model folder. Classifier forms select scientific
inputs; they do not select interpreters or install another environment.
"""

from dataclasses import replace
import os
import json
from pathlib import Path
import sys

from mif_pipeline.pipeline_core.software_preferences import (
    desktop_preferences_path, load_desktop_preferences, write_desktop_preferences,
)


def worker_command() -> tuple[str, ...]:
    """Launch the current installed worker with pipe-capable standard streams."""
    if getattr(sys, "frozen", False):
        name = "mif-numerical-worker.exe" if os.name == "nt" else "mif-numerical-worker"
        command = (str(Path(sys.executable).with_name(name)),)
    else:
        command = (sys.executable, "-B", "-u", "-m", "mif_pipeline.compartment_analysis.kronos_worker")
    require_worker_command(command)
    return command


def require_worker_command(command) -> None:
    """Check launch structure and the executable; startup checks its release."""
    if (not isinstance(command, (tuple, list)) or not 1 <= len(command) <= 8
            or any(type(value) is not str or not value or "\0" in value for value in command)
            or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()
            or not os.access(command[0], os.X_OK)):
        raise ValueError("The application processing runtime is unavailable. Repair the application installation and retry.")


def select_package(directory: Path, *, preferences_path: Path | None = None) -> Path:
    """Inspect the downloaded package and save its folder without changing it."""
    from mif_pipeline.compartment_analysis.kronos_encoder import inspect_kronos_package
    package = inspect_kronos_package(directory.expanduser().resolve(strict=True))
    path = preferences_path or desktop_preferences_path()
    preferences = load_desktop_preferences(path)
    if preferences is None:
        raise ValueError("Connect QuPath in Software setup before selecting the KRONOS model.")
    write_desktop_preferences(path, replace(preferences, kronos_package_directory=str(package.root)))
    return package.root


def selected_package(*, preferences_path: Path | None = None) -> Path | None:
    """Return the model folder selected on this execution host."""
    preferences = load_desktop_preferences(preferences_path or desktop_preferences_path())
    value = getattr(preferences, "kronos_package_directory", None)
    return Path(value) if value else None


def model_setup_snapshot() -> dict:
    """Present inspected model identity and marker data for a classifier form.

    The small snapshot can cross the streaming connection. It contains no
    weights or model implementation; native execution checks the same package
    on the compute host before using the accepted scientific input choices.
    """
    from mif_pipeline.compartment_analysis.kronos_encoder import inspect_kronos_package
    configured = os.environ.get("MIF_KRONOS_PACKAGE_DIRECTORY")
    directory = Path(configured) if configured else selected_package()
    if directory is None:
        return {}
    package = inspect_kronos_package(directory)
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel
    configured_text = os.environ.get("MIF_KRONOS_MARKER_TEXT_MODEL")
    text_model = (KronosMarkerTextModel.from_mapping(json.loads(configured_text))
                  if configured_text else selected_marker_text_model())
    return {"marker_text_model": None if text_model is None else text_model.to_mapping(),
            "package_directory": str(package.root), "package_sha256": package.sha256,
            "worker_command": list(worker_command()),
            "marker_csv": (package.root / package.marker_csv_name).read_text(encoding="utf-8")}

def marker_text_download_info() -> dict:
    """Return the pinned public helper files and licence before installation.

    KRONOS weights remain user-obtained under their own access terms. This
    separate Apache-2.0 text model is downloaded only by an explicit Software
    action; execution never fetches either model.
    """
    from mif_pipeline._software import read_software_description
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import TEXT_FILES, TEXT_REPOSITORY

    model = read_software_description("biolinkbert")["model"]
    base = "https://huggingface.co/" + TEXT_REPOSITORY
    revision = model["revision"]
    if (model["repository"] != TEXT_REPOSITORY or model["license"] != "Apache-2.0"
            or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision)
            or model["license_url"] != f"{base}/blob/{revision}/README.md"
            or len(model["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in model["sha256"])
            or set(model["files"]) != set(TEXT_FILES)):
        raise ValueError("The installed BioLinkBERT download description is invalid")
    for name, pin in model["files"].items():
        if (pin["url"] != f"{base}/resolve/{revision}/{name}"
                or type(pin["size_bytes"]) is not int or not 0 < pin["size_bytes"] <= 2 * 1024**3
                or len(pin["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in pin["sha256"])):
            raise ValueError("The installed BioLinkBERT file pin is invalid: " + name)
    return model


def marker_text_target() -> Path:
    """Place the helper in versioned application data outside an analysis."""
    root = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            if os.name == "nt" else
            Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"))
    return root / "mif-pipeline" / "biolinkbert" / marker_text_download_info()["revision"]


def selected_marker_text_model(*, preferences_path: Path | None = None):
    """Return the inspected helper identity selected on this processing host."""
    preferences = load_desktop_preferences(preferences_path or desktop_preferences_path())
    return None if preferences is None else preferences.kronos_marker_text_model


def select_marker_text_model(directory: Path, *, preferences_path: Path | None = None):
    """Verify the pinned local helper and save its existing scientific identity."""
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import inspect_marker_text_model

    pin = marker_text_download_info()
    selected = inspect_marker_text_model(directory.expanduser().resolve(strict=True), revision=pin["revision"])
    if selected.sha256 != pin["sha256"]:
        raise ValueError("Select the pinned BioLinkBERT-large snapshot, or install it from Software setup")
    path = preferences_path or desktop_preferences_path()
    preferences = load_desktop_preferences(path)
    if preferences is None:
        raise ValueError("Connect QuPath in Software setup before selecting the marker text model")
    write_desktop_preferences(path, replace(preferences, kronos_marker_text_model=selected))
    return selected


def install_marker_text_model(*, consent: bool, preferences_path: Path | None = None,
                              target: Path | None = None, opener=None):
    """Download verified files and publish a new helper directory without replacement.

    A failed transfer leaves the selected model untouched. Repeating the action
    selects an already complete matching snapshot; conflicting existing files
    require choosing the correct folder or correcting Software setup first.
    """
    import tempfile
    from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
    from .setup_transfer import download_pinned_file

    if consent is not True:
        raise ValueError("Approve the pinned marker text model download before installation")
    path = preferences_path or desktop_preferences_path()
    if load_desktop_preferences(path) is None:
        raise ValueError("Connect QuPath in Software setup before installing the marker text model")
    destination = marker_text_target() if target is None else target
    if not isinstance(destination, Path) or not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("Select an absolute marker text model installation directory")
    if destination.exists() or destination.is_symlink():
        return select_marker_text_model(destination, preferences_path=path)
    pin = marker_text_download_info()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".biolinkbert-install-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "model"
        stage.mkdir()
        for name, file_pin in pin["files"].items():
            download_pinned_file(file_pin["url"], stage / name, file_pin["sha256"],
                                file_pin["size_bytes"], opener=opener,
                                maximum_bytes=2 * 1024**3 if name == "pytorch_model.bin" else 32 * 1024**2)
        publish_no_replace(stage, destination, "win-x86_64" if os.name == "nt" else "linux-x86_64")
    # Every loading file was created exclusively and verified against its fixed
    # content pin. Publication only moves those ordinary files, so retain the
    # aggregate pinned identity without hashing the same 1.3 GB again.
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel
    selected = KronosMarkerTextModel(str(destination), pin["repository"], pin["revision"], pin["sha256"])
    preferences = load_desktop_preferences(path)
    write_desktop_preferences(path, replace(preferences, kronos_marker_text_model=selected))
    return selected


def marker_authoring_snapshot(*, setup=None) -> dict:
    """Read category suggestions and selected helper metadata without loading weights.

    These are authoring hints. Submission still inspects the selected KRONOS
    package, and preparation verifies the saved helper before its single load.
    Streaming forms supply the processing host's bounded setup snapshot instead.
    """
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import marker_vocabulary

    if setup is not None:
        raw = setup.get("marker_csv")
        if not raw:
            return {}
        payload = raw.encode("utf-8")
    else:
        configured = os.environ.get("MIF_KRONOS_PACKAGE_DIRECTORY")
        directory = Path(configured) if configured else selected_package()
        if directory is None:
            return {}
        with (directory / "config.json").open("rb") as source:
            config_bytes = source.read(1024**2 + 1)
        if len(config_bytes) > 1024**2:
            raise ValueError("KRONOS config.json exceeds the authoring size limit")
        config = json.loads(config_bytes)
        name = config["marker_csv"]
        if type(name) is not str or Path(name).name != name:
            raise ValueError("The KRONOS marker vocabulary must be inside its model folder")
        with (directory / name).open("rb") as source:
            payload = source.read(4 * 1024**2 + 1)
    return marker_vocabulary(payload)
