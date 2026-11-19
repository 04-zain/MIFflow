"""Bounded, class-only QuPath origin discovery before full prerequisite admission.

An origin candidate is not model, native-engine, or scientific readiness.

The `QuPathClassOriginResult` record carries `qupath_version`, `instanseg`, `djl`,
`pytorch_engine`, `extensions` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Literal

from .launch_binding import QuPathProfileLaunchBinding, qupath_command
from .payload_lock import (
    BUILTIN_DJL_SHA256,
    BUILTIN_PYTORCH_ENGINE_SHA256,
    SOURCE_ARCHIVE_LOCK,
    WARPY_SOURCE_ARCHIVE,
    WARPY_VERSION,
)
from .prerequisite_probe import (
    ProbeFileExpectation,
    ProbeProcessRunner,
    _PROBE_LIMIT,
    _absolute_clean_path,
    _closed_object,
    _file_check,
    _is_link,
    _probe_environment,
    _run_bounded_probe,
    _unique_object,
)

_SCHEMA = "qupath-class-origin-candidate/v3"
_MAX_EXTENSION_JARS = 1024
_CLASSES = {
    "instanseg": ("qupath.ext.instanseg.ui.InstanSegExtension", "0.1.7"),
    "djl": ("qupath.ext.djl.DjlExtension", "0.4.2"),
    "pytorch_engine": ("ai.djl.pytorch.engine.PtEngine", None),
    "warpy": ("qupath.ext.warpy.WarpyExtension", WARPY_VERSION),
}


# ==============================================================================
#                             Class origin candidate
# ==============================================================================


@dataclass(frozen=True)
class ClassOriginCandidate:
    status: Literal["candidate", "missing", "incompatible", "error"]
    diagnostic: str
    code_source: Path | None = None
    evidence_sha256: str | None = None


@dataclass(frozen=True)
class QuPathExtensionInventory:
    """Native profile and installed paths, not authenticated runtime readiness."""

    status: Literal["observed", "unconfigured", "error"]
    directory: Path | None
    jars: tuple[Path, ...]
    diagnostic: str


@dataclass(frozen=True)
class QuPathClassOriginResult:
    qupath_version: str | None
    instanseg: ClassOriginCandidate
    djl: ClassOriginCandidate
    pytorch_engine: ClassOriginCandidate
    extensions: QuPathExtensionInventory = QuPathExtensionInventory(
        "error",
        None,
        (),
        "Extension inventory was not observed",
    )
    warpy: ClassOriginCandidate = ClassOriginCandidate(
        "error",
        "Warpy class origin was not observed",
    )
    preferences_namespace: str | None = None


def _nonlive(diagnostic: str) -> QuPathClassOriginResult:
    absent = ClassOriginCandidate("error", diagnostic)
    return QuPathClassOriginResult(None, absent, absent, absent, warpy=absent)


def _decode(encoded: bytes, challenge: str) -> QuPathClassOriginResult:
    evidence = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )
    _closed_object(
        evidence,
        {
            "schema",
            "challenge",
            "qupath_version",
            "instanseg",
            "djl",
            "pytorch_engine",
            "warpy",
            "extensions",
            "preferences_namespace",
        },
    )
    if evidence["schema"] != _SCHEMA or evidence["challenge"] != challenge:
        raise ValueError("evidence identity mismatch")
    version = evidence["qupath_version"]
    if not isinstance(version, str):
        raise ValueError("invalid QuPath version")
    namespace = evidence["preferences_namespace"]
    if (
        type(namespace) is not str
        or not namespace.startswith("/")
        or "\0" in namespace
        or len(namespace) > 1024
    ):
        raise ValueError("invalid observed preferences namespace")
    extensions = _decode_extensions(evidence["extensions"])
    supported = re.fullmatch(r"0\.7\.[0-9]+", version) is not None
    digest = hashlib.sha256(encoded).hexdigest()
    findings = {}
    for name, (class_name, locked_version) in _CLASSES.items():
        item = _closed_object(
            evidence[name],
            {"status", "class_name", "loaded_version", "code_source", "code_source_sha256"},
        )
        if item["class_name"] != class_name or item["status"] not in (
            "observed",
            "missing",
            "incompatible",
        ):
            raise ValueError("invalid class observation")
        for field in ("loaded_version", "code_source", "code_source_sha256"):
            if item[field] is not None and not isinstance(item[field], str):
                raise ValueError("invalid class observation field")
        if item["status"] != "observed":
            if any(
                item[field] is not None
                for field in ("loaded_version", "code_source", "code_source_sha256")
            ):
                raise ValueError("failed class observation carries identity")
            findings[name] = ClassOriginCandidate(item["status"], "Class origin was not observed")
            continue
        source_text = item["code_source"]
        if not source_text:
            findings[name] = ClassOriginCandidate(
                "incompatible", "Loaded class has no local code source"
            )
            continue
        source = Path(source_text)
        try:
            _absolute_clean_path(source, "observed code source")
        except (TypeError, ValueError):
            findings[name] = ClassOriginCandidate(
                "incompatible", "Loaded class code source is not a clean absolute path"
            )
            continue
        locked_digest = (
            SOURCE_ARCHIVE_LOCK["instanseg"][2]
            if name == "instanseg"
            else (
                BUILTIN_DJL_SHA256
                if name == "djl"
                else (
                    BUILTIN_PYTORCH_ENGINE_SHA256
                    if name == "pytorch_engine"
                    else WARPY_SOURCE_ARCHIVE[2]
                )
            )
        )
        local = _file_check(ProbeFileExpectation(source, locked_digest))
        if local.status != "verified":
            findings[name] = ClassOriginCandidate(
                local.status, f"The loaded {name} file differs from the verified release: {source}"
            )
        elif (
            supported
            and item["loaded_version"] == locked_version
            and item["code_source_sha256"] == locked_digest
        ):
            findings[name] = ClassOriginCandidate(
                "candidate", "Exact live class origin observed", source, digest
            )
        else:
            findings[name] = ClassOriginCandidate(
                "incompatible",
                f"The loaded {name} version differs from the required {locked_version}: {source}",
            )
    if not supported:
        findings = {
            name: ClassOriginCandidate("incompatible", "Unsupported QuPath version")
            for name in findings
        }
    return QuPathClassOriginResult(
        version,
        findings["instanseg"],
        findings["djl"],
        findings["pytorch_engine"],
        extensions,
        findings["warpy"],
        namespace,
    )


def _decode_extensions(value: object) -> QuPathExtensionInventory:
    item = _closed_object(value, {"status", "directory", "jars"})
    status, directory, jars = item["status"], item["directory"], item["jars"]
    if status not in ("observed", "unconfigured", "error") or type(jars) is not list:
        raise ValueError("invalid extension inventory")
    if status != "observed":
        if directory is not None or jars:
            raise ValueError("unobserved inventory carries installation paths")
        return QuPathExtensionInventory(
            status, None, (), "Native extension profile was not observed"
        )
    if (
        type(directory) is not str
        or not directory
        or "\0" in directory
        or len(jars) > _MAX_EXTENSION_JARS
    ):
        raise ValueError("invalid extension directory or excessive inventory")
    root = Path(directory)
    _absolute_clean_path(root, "extension directory")
    if any(type(path) is not str or not path or "\0" in path for path in jars) or jars != sorted(
        set(jars)
    ):
        raise ValueError("extension inventory must contain unique sorted paths")
    paths = tuple(Path(path) for path in jars)
    for path in paths:
        _absolute_clean_path(path, "installed extension")
        if path.suffix.lower() != ".jar" or not path.is_relative_to(root):
            raise ValueError("installed extension is outside the native profile")
    return QuPathExtensionInventory(
        "observed", root, paths, "Native installed extension paths observed"
    )


def probe_qupath_class_origins(
    executable: Path,
    *,
    process_runner: ProbeProcessRunner | None = None,
    timeout_seconds: float = 15.0,
    launch_binding: QuPathProfileLaunchBinding | None = None,
) -> QuPathClassOriginResult:
    """Discover pinned extension and engine class origins in one project-free process."""
    _absolute_clean_path(executable, "executable")
    command_prefix = qupath_command(executable, launch_binding=launch_binding)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (float, int))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        raise ValueError("timeout_seconds must be finite and within (0, 30]")
    if not executable.is_file():
        return _nonlive("QuPath executable missing; no class observation")
    try:
        with tempfile.TemporaryDirectory(prefix="qupath-origin-") as exchange_name:
            exchange = Path(exchange_name).resolve()
            script = exchange / "origin.groovy"
            from .scripts import internal_probe_bytes
            script.write_bytes(internal_probe_bytes("general/04_probe_class_origins.groovy"))
            request_path, evidence_path = exchange / "request.json", exchange / "evidence.json"
            challenge = secrets.token_hex(32)
            request_path.write_text(
                json.dumps({"schema": _SCHEMA, "challenge": challenge}), encoding="utf-8"
            )
            command = (
                *command_prefix,
                "script",
                "--args",
                str(request_path),
                "--args",
                str(evidence_path),
                str(script),
            )
            completed = (process_runner or _run_bounded_probe)(
                command, env=_probe_environment(exchange, exchange), timeout=timeout_seconds
            )
            if completed.returncode != 0 or any(
                len(stream or b"") > _PROBE_LIMIT for stream in (completed.stdout, completed.stderr)
            ):
                raise ValueError("probe process failed or exceeded output cap")
            if _is_link(evidence_path) or not stat.S_ISREG(evidence_path.lstat().st_mode):
                raise ValueError("unsafe evidence member")
            with evidence_path.open("rb") as stream:
                encoded = stream.read(_PROBE_LIMIT + 1)
            if len(encoded) > _PROBE_LIMIT:
                raise ValueError("evidence exceeds cap")
            result = _decode(encoded, challenge)
            if launch_binding is not None:
                from .managed_profile import managed_instanseg_source_paths

                extension_root = launch_binding.profile_root / "extensions"
                expected_instanseg = (
                    extension_root / SOURCE_ARCHIVE_LOCK["instanseg"][0].rsplit("/", 1)[1]
                )
                expected_warpy = extension_root / WARPY_SOURCE_ARCHIVE[0].rsplit("/", 1)[1]
                from .native_image_reader import READER_NAME
                expected_extensions = [expected_instanseg, expected_warpy]
                expected_extensions.append(extension_root / READER_NAME)
                # Keep authenticating the profile copy even when the parent
                # classloader uses the matching application-directory JAR.
                profile_instanseg = _file_check(
                    ProbeFileExpectation(expected_instanseg, SOURCE_ARCHIVE_LOCK["instanseg"][2])
                )
                if (
                    profile_instanseg.status != "verified"
                    or result.qupath_version != launch_binding.qupath_version
                    or result.preferences_namespace != "/" + launch_binding.preferences_namespace
                    or result.extensions.status != "observed"
                    or result.extensions.directory != extension_root
                    or result.extensions.jars != tuple(sorted(expected_extensions))
                    or result.instanseg.status != "candidate"
                    or result.instanseg.code_source
                    not in managed_instanseg_source_paths(launch_binding)
                    or result.warpy.status != "candidate"
                    or result.warpy.code_source != expected_warpy
                ):
                    return _nonlive("Observed class origins differ from the selected profile")
            return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return _nonlive("Class origin probe failed: " + type(exc).__name__)
