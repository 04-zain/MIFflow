"""Shared installed-software choices for desktop and command-line launches.

The `DesktopPreferencesV1` record carries `schema_name`, `schema_version`,
`qupath_executable`, `java_home`, `bioformats_root` and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from tempfile import mkstemp
from typing import Literal

from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel

_SCHEMA_NAME = "mif-desktop-preferences"
_SCHEMA_VERSION = 1
_MAX_PREFERENCES_BYTES = 64 * 1024


# ==============================================================================
#                              Software preferences
# ==============================================================================


@dataclass(frozen=True, slots=True)
class DesktopPreferencesV1:
    """The complete local preference schema; it contains no workflow authority."""

    schema_name: Literal["mif-desktop-preferences"]
    schema_version: Literal[1]
    qupath_executable: str
    java_home: str | None = None
    bioformats_root: str | None = None
    qupath_profile_manifest: str | None = None
    elastix_directory: str | None = None
    nimbus_checkpoint_path: str | None = None
    kronos_package_directory: str | None = None
    kronos_marker_text_model: KronosMarkerTextModel | None = None

    def __post_init__(self) -> None:
        if self.schema_name != _SCHEMA_NAME or type(self.schema_name) is not str:
            raise ValueError("desktop preference schema_name is invalid")
        if self.schema_version != _SCHEMA_VERSION or type(self.schema_version) is not int:
            raise ValueError("desktop preference schema_version is invalid")
        if type(self.qupath_executable) is not str or not self.qupath_executable.strip():
            raise ValueError("desktop preference QuPath executable is invalid")
        if not Path(self.qupath_executable).expanduser().is_absolute():
            raise ValueError("desktop preference QuPath executable must be absolute")
        for label, value in (
            ("Java home", self.java_home),
            ("Bio-Formats root", self.bioformats_root),
            ("QuPath profile", self.qupath_profile_manifest),
            ("Elastix folder", self.elastix_directory),
            ("Nimbus checkpoint", self.nimbus_checkpoint_path),
            ("KRONOS model folder", self.kronos_package_directory),
        ):
            if value is not None and (
                type(value) is not str
                or not value.strip()
                or "\0" in value
                or not Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                raise ValueError(f"desktop preference {label} must be an absolute path")

        if self.kronos_marker_text_model is not None and type(self.kronos_marker_text_model) is not KronosMarkerTextModel:
            raise ValueError("desktop preference KRONOS marker text model is invalid")

def desktop_preferences_path(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> Path:
    """Return the platform-local v1 preference path outside every workspace."""

    selected = sys.platform if platform is None else platform
    home_path = Path.home() if home is None else Path(home).expanduser()
    if selected == "win32":
        configured = environ.get("APPDATA")
        root = Path(configured).expanduser() if configured else home_path / "AppData" / "Roaming"
    elif selected == "linux":
        configured = environ.get("XDG_CONFIG_HOME")
        root = Path(configured).expanduser() if configured else home_path / ".config"
    else:
        raise ValueError(f"desktop preferences are unsupported on platform {selected!r}")
    return root / "mif-pipeline" / "preferences-v1.json"


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"desktop preferences contain duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"desktop preferences contain non-finite value {value!r}")


def load_desktop_preferences(path: Path) -> DesktopPreferencesV1 | None:
    """Load one exact v1 preference object, or None when it does not exist.

    The decoded fields are checked through the record constructors and the applicable
    context comparisons before returning the result. The validated representation is
    returned for the subsequent operation."""

    if not isinstance(path, Path):
        raise TypeError("desktop preference path must be a Path")
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"desktop preferences cannot be read: {path}") from exc
    if len(payload) > _MAX_PREFERENCES_BYTES:
        raise ValueError("desktop preferences exceed the size limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("desktop preferences are malformed JSON") from exc
    expected = {"schema_name", "schema_version", "qupath_executable"}
    optional = {"java_home", "bioformats_root", "qupath_profile_manifest",
                "elastix_directory", "nimbus_checkpoint_path", "kronos_package_directory",
                "kronos_marker_text_model"}
    if (not isinstance(value, dict) or not expected <= set(value)
            or set(value) - expected - optional):
        raise ValueError("desktop preferences require the v1 fields")
    return DesktopPreferencesV1(
        value["schema_name"],
        value["schema_version"],
        value["qupath_executable"],
        value.get("java_home"),
        value.get("bioformats_root"),
        value.get("qupath_profile_manifest"),
        value.get("elastix_directory"),
        value.get("nimbus_checkpoint_path"),
        value.get("kronos_package_directory"),
        None if value.get("kronos_marker_text_model") is None else
            KronosMarkerTextModel.from_mapping(value["kronos_marker_text_model"]),
    )


def _canonical_bytes(value: DesktopPreferencesV1) -> bytes:
    return json.dumps(
        {
            "schema_name": value.schema_name,
            "schema_version": value.schema_version,
            "qupath_executable": value.qupath_executable,
            "java_home": value.java_home,
            "bioformats_root": value.bioformats_root,
            "qupath_profile_manifest": value.qupath_profile_manifest,
            "elastix_directory": value.elastix_directory,
            "nimbus_checkpoint_path": value.nimbus_checkpoint_path,
            "kronos_package_directory": value.kronos_package_directory,
            "kronos_marker_text_model": None if value.kronos_marker_text_model is None else value.kronos_marker_text_model.to_mapping(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def write_desktop_preferences(path: Path, value: DesktopPreferencesV1) -> None:
    """Atomically replace canonical preference bytes with private file mode."""

    if not isinstance(path, Path):
        raise TypeError("desktop preference path must be a Path")
    if not isinstance(value, DesktopPreferencesV1):
        raise TypeError("desktop preferences must be DesktopPreferencesV1")
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_text = mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_text)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "DesktopPreferencesV1",
    "desktop_preferences_path",
    "load_desktop_preferences",
    "write_desktop_preferences",
]
