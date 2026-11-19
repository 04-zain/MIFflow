"""Strict immutable setup record for one candidate QuPath managed profile.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from . import payload_lock
from .native_image_reader import native_reader_description
from .launch_binding import QuPathProfileLaunchBinding
from .prerequisite_probe import (
    ProbeFileExpectation,
    _absolute_clean_path,
    _file_check,
    _identity,
    _path_snapshot,
)

_MAX_MANIFEST_BYTES = 64 * 1024
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_NAME = "mif-qupath-managed-profile"
_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "executable",
        "qupath_version",
        "generation",
        "profile_root",
        "extensions",
    }
)
_EXTENSION_FIELDS = frozenset(
    {
        "name",
        "version",
        "url",
        "size_bytes",
        "sha256",
    }
)


# ==============================================================================
#                                Managed profile
# ==============================================================================


class ManagedQuPathProfileError(ValueError):
    """A selected managed generation cannot be used as a launch candidate."""


class BundledExtensionUpdateRequired(ManagedQuPathProfileError):
    """The saved setup uses an earlier build of the application's MIF extension."""


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ManagedQuPathProfileError("managed profile manifest is not canonical") from exc


def _checked_path(value: Path, label: str) -> Path:
    try:
        _absolute_clean_path(value, label)
    except (TypeError, ValueError) as exc:
        raise ManagedQuPathProfileError(str(exc)) from exc
    if "\0" in str(value):
        raise ManagedQuPathProfileError(f"{label} contains NUL")
    return value


def _extension_rows() -> list[dict[str, object]]:
    rows = []
    for version, pin in (
        (payload_lock.INSTANSEG_VERSION, payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"]),
        (payload_lock.WARPY_VERSION, payload_lock.WARPY_SOURCE_ARCHIVE),
    ):
        if type(version) is not str or not version or type(pin) is not tuple or len(pin) != 3:
            raise ManagedQuPathProfileError("managed extension pin is invalid")
        url, size, digest = pin
        if (
            type(url) is not str
            or not url.startswith("https://")
            or type(size) is not int
            or size <= 0
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
        ):
            raise ManagedQuPathProfileError("managed extension pin is invalid")
        name = url.rsplit("/", 1)[-1]
        if (
            not name.endswith(".jar")
            or name in {"", ".", ".."}
            or "/" in name
            or "\\" in name
            or "\0" in name
        ):
            raise ManagedQuPathProfileError("managed extension filename is invalid")
        rows.append(
            {
                "name": name,
                "version": version,
                "url": url,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if len({row["name"] for row in rows}) != 2:
        raise ManagedQuPathProfileError("managed extension names collide")
    rows.append(dict(native_reader_description()))
    return rows


def managed_instanseg_source_paths(binding: QuPathProfileLaunchBinding) -> tuple[Path, ...]:
    """Allowed exact locations, not evidence that an extension was loaded.

    QuPath's parent classloader can load the pinned InstanSeg JAR from its
    application directory before consulting the isolated user profile.
    Callers must still verify the bytes and observe the actual class source.
    """
    if type(binding) is not QuPathProfileLaunchBinding:
        raise TypeError("binding must be a QuPathProfileLaunchBinding")
    name = payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"][0].rsplit("/", 1)[1]
    executable = binding.executable
    app = (
        executable.parent.parent / "lib" / "app"
        if executable.parent.name == "bin"
        else executable.parent / "app"
    )
    return (binding.profile_root / "extensions" / name, app / name)


def managed_profile_manifest(*, executable: Path, profile_root: Path) -> bytes:
    """Describe pinned setup bytes without reading or creating any files."""

    executable = _checked_path(executable, "executable")
    profile_root = _checked_path(profile_root, "profile_root")
    if _GENERATION.fullmatch(profile_root.name) is None:
        raise ManagedQuPathProfileError("managed profile root lacks a generation name")
    record = {
        "schema_name": _SCHEMA_NAME,
        "schema_version": 1,
        "executable": str(executable),
        "qupath_version": "0.7.0",
        "generation": profile_root.name,
        "profile_root": str(profile_root),
        "extensions": _extension_rows(),
    }
    body = _canonical(record)
    if len(body) > _MAX_MANIFEST_BYTES:
        raise ManagedQuPathProfileError("managed profile manifest is too large")
    return body


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManagedQuPathProfileError("managed profile manifest has duplicate fields")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise ManagedQuPathProfileError(f"managed profile manifest has nonfinite value: {token}")


def _read_manifest(path: Path) -> tuple[bytes, tuple[tuple[Path, tuple[int, ...]], ...]]:
    try:
        before = _path_snapshot(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ManagedQuPathProfileError("managed profile manifest is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = _identity(os.fstat(stream.fileno()))
            named = before[-1][1]
            same = (
                named[0] != 0 and named[1] != 0 and opened[:5] == named[:5]
                if os.name == "nt"
                else opened == named
            )
            if not same:
                raise ManagedQuPathProfileError("managed profile manifest changed while opening")
            body = stream.read(_MAX_MANIFEST_BYTES + 1)
            if len(body) > _MAX_MANIFEST_BYTES:
                raise ManagedQuPathProfileError("managed profile manifest is too large")
            if _identity(os.fstat(stream.fileno())) != opened:
                raise ManagedQuPathProfileError("managed profile manifest changed while reading")
        if _path_snapshot(path) != before:
            raise ManagedQuPathProfileError("managed profile manifest path changed while reading")
        return body, before
    except ManagedQuPathProfileError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise ManagedQuPathProfileError("managed profile manifest cannot be read safely") from exc


def _check_regular_path(path: Path, label: str) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    try:
        snapshot = _path_snapshot(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ManagedQuPathProfileError(f"{label} is not a regular file")
        return snapshot
    except ManagedQuPathProfileError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise ManagedQuPathProfileError(f"{label} cannot be checked safely") from exc


def load_managed_qupath_profile(
    manifest_path: Path,
    *,
    expected_executable: Path | None = None,
) -> QuPathProfileLaunchBinding:
    """Check one selected record and local bytes; grant no readiness authority."""

    manifest_path = _checked_path(manifest_path, "manifest_path")
    if manifest_path.name != "manifest.json":
        raise ManagedQuPathProfileError("managed profile manifest must be named manifest.json")
    if expected_executable is not None:
        expected_executable = _checked_path(expected_executable, "expected_executable")
    body, manifest_before = _read_manifest(manifest_path)
    try:
        record = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ManagedQuPathProfileError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ManagedQuPathProfileError("managed profile manifest is not valid JSON") from exc
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ManagedQuPathProfileError("managed profile manifest has foreign fields")
    canonical = _canonical(record)
    if canonical != body:
        raise ManagedQuPathProfileError("managed profile manifest bytes are not canonical")
    root = (
        _checked_path(Path(record["profile_root"]), "profile_root")
        if type(record["profile_root"]) is str
        else None
    )
    executable = (
        _checked_path(Path(record["executable"]), "executable")
        if type(record["executable"]) is str
        else None
    )
    if (
        root is None
        or executable is None
        or manifest_path.parent != root
        or str(root) != record["profile_root"]
        or str(executable) != record["executable"]
        or _GENERATION.fullmatch(root.name) is None
        or record["schema_name"] != _SCHEMA_NAME
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or record["qupath_version"] != "0.7.0"
        or record["generation"] != root.name
        or (expected_executable is not None and executable != expected_executable)
    ):
        raise ManagedQuPathProfileError("managed profile manifest identity differs")
    extensions = record["extensions"]
    expected_rows = _extension_rows()
    if (
        type(extensions) is not list
        or len(extensions) != len(expected_rows)
        or any(
            type(row) is not dict
            or set(row) != _EXTENSION_FIELDS
            or type(row["size_bytes"]) is not int
            or row["size_bytes"] <= 0
            or any(type(row[field]) is not str for field in ("name", "version", "url", "sha256"))
            or _SHA256.fullmatch(row["sha256"]) is None
            for row in extensions
        )
    ):
        raise ManagedQuPathProfileError("The saved QuPath extension list is invalid.")
    if extensions != expected_rows:
        if (extensions[:-1] == expected_rows[:-1]
                and all(extensions[-1][key] == expected_rows[-1][key]
                        for key in ("name", "version", "url"))):
            raise BundledExtensionUpdateRequired(
                "Update the application's MIF extension for this installation."
            )
        raise ManagedQuPathProfileError("The saved QuPath extensions need updating in Software setup.")
    executable_before = _check_regular_path(executable, "QuPath executable")
    try:
        root_before = _path_snapshot(root)
        directory = root / "extensions"
        directory_before = _path_snapshot(directory)
        if not stat.S_ISDIR(root.lstat().st_mode) or not stat.S_ISDIR(directory.lstat().st_mode):
            raise ManagedQuPathProfileError("managed profile root or extensions is not a directory")
        members = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        member_names = tuple(item.name for item in members)
        jar_names = tuple(sorted(row["name"] for row in expected_rows))
        has_catalogs = "catalogs" in member_names
        if member_names != tuple(sorted((*jar_names, "catalogs") if has_catalogs else jar_names)):
            raise ManagedQuPathProfileError("managed profile extensions contain foreign members")
        catalogs = directory / "catalogs" if has_catalogs else None
        catalogs_before = None
        if catalogs is not None:
            catalogs_before = _path_snapshot(catalogs)
            if not stat.S_ISDIR(catalogs.lstat().st_mode) or tuple(catalogs.iterdir()):
                raise ManagedQuPathProfileError(
                    "managed profile catalogs is not an empty directory"
                )
        file_snapshots = {}
        for row in expected_rows:
            path = directory / row["name"]
            file_snapshots[path] = _check_regular_path(path, "managed extension")
            if file_snapshots[path][-1][1][3] != row["size_bytes"]:
                raise ManagedQuPathProfileError("managed extension byte count differs")
            result = _file_check(ProbeFileExpectation(path, row["sha256"]))
            if result.status != "verified":
                raise ManagedQuPathProfileError(result.diagnostic)
        if any(_path_snapshot(path) != snapshot for path, snapshot in file_snapshots.items()):
            raise ManagedQuPathProfileError("managed extension changed during check")
        if catalogs is not None and (
            _path_snapshot(catalogs) != catalogs_before or tuple(catalogs.iterdir())
        ):
            raise ManagedQuPathProfileError("managed profile catalogs changed during check")
        if _path_snapshot(directory) != directory_before or _path_snapshot(root) != root_before:
            raise ManagedQuPathProfileError("managed profile directory changed during check")
        if _path_snapshot(executable) != executable_before:
            raise ManagedQuPathProfileError("QuPath executable changed during check")
        if _path_snapshot(manifest_path) != manifest_before:
            raise ManagedQuPathProfileError("managed profile manifest changed during check")
    except ManagedQuPathProfileError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise ManagedQuPathProfileError("managed profile files cannot be checked safely") from exc
    return QuPathProfileLaunchBinding(
        executable=executable,
        qupath_version="0.7.0",
        profile_root=root,
        generation=root.name,
        manifest_sha256=hashlib.sha256(body).hexdigest(),
    )


__all__ = [
    "ManagedQuPathProfileError",
    "managed_profile_manifest",
    "load_managed_qupath_profile",
    "managed_instanseg_source_paths",
]
