"""Immutable QuPath prerequisite inputs and exact local artifact preflight.

Local verification authenticates bytes, but does not attest a live QuPath load.

The `QuPathPrerequisiteProbeRequest` record carries `executable`, `model_root`,
`model_version`, `model_files`, `instanseg_jar` and its remaining declared fields.

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
import os
from pathlib import Path
import re
import stat
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

from .launch_binding import QuPathProfileLaunchBinding, qupath_command

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK_SIZE = 1024 * 1024


# ==============================================================================
#                               Prerequisite probe
# ==============================================================================


DEFAULT_PREREQUISITE_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class ProbeFileExpectation:
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        _absolute_clean_path(self.path, "artifact path")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("artifact SHA-256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class QuPathPrerequisiteProbeRequest:
    executable: Path
    model_root: Path | None
    model_version: str | None
    model_files: tuple[ProbeFileExpectation, ...]
    instanseg_jar: ProbeFileExpectation
    djl_jar: ProbeFileExpectation
    pytorch_engine_jar: ProbeFileExpectation
    cpu_engine_files: tuple[ProbeFileExpectation, ...]
    warpy_jar: ProbeFileExpectation | None
    require_warpy: bool
    cache_root: Path
    timeout_seconds: float = DEFAULT_PREREQUISITE_TIMEOUT_SECONDS
    launch_binding: QuPathProfileLaunchBinding | None = None

    def __post_init__(self) -> None:
        """Validate the QuPathPrerequisiteProbeRequest fields when the record is constructed.

        The checks cover ``require_warpy``, ``executable``, ``model_root``,
        ``instanseg_jar``, ``djl_jar``, ``pytorch_engine_jar``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        for name in ("executable", "cache_root"):
            _absolute_clean_path(getattr(self, name), name)
        qupath_command(self.executable, launch_binding=self.launch_binding)
        if self.model_root is not None:
            _absolute_clean_path(self.model_root, "model_root")
        if not isinstance(self.model_files, tuple):
            raise TypeError("model_files must be a tuple")
        if self.model_root is None or self.model_version is None or not self.model_files:
            if not (
                self.model_root is None and self.model_version is None and not self.model_files
            ):
                raise ValueError("model root, version, and files must be all absent or all present")
        if self.model_version is not None and (
            not isinstance(self.model_version, str)
            or not self.model_version
            or self.model_version.strip() != self.model_version
            or any(ord(c) < 32 for c in self.model_version)
        ):
            raise ValueError(
                "model_version must be a nonempty version without surrounding whitespace or control characters"
            )
        if not isinstance(self.cpu_engine_files, tuple) or not self.cpu_engine_files:
            raise ValueError("cpu_engine_files must be a nonempty tuple")
        if not isinstance(self.require_warpy, bool):
            raise TypeError("require_warpy must be bool")
        if self.require_warpy and self.warpy_jar is None:
            raise ValueError("required Warpy needs a file expectation")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")

        expected = (
            *self.model_files,
            self.instanseg_jar,
            self.djl_jar,
            self.pytorch_engine_jar,
            *self.cpu_engine_files,
        )
        if self.require_warpy:
            expected += (self.warpy_jar,)
        if not all(isinstance(item, ProbeFileExpectation) for item in expected):
            raise TypeError("all active artifacts must be ProbeFileExpectation values")
        paths = tuple(item.path for item in expected)
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        if self.model_root is not None:
            if (
                self.model_root in (self.executable, self.cache_root)
                or _is_within(self.cache_root, self.model_root)
                or _is_within(self.model_root, self.cache_root)
            ):
                raise ValueError("cache and model roots must be separate")
            if any(not _is_within(item.path, self.model_root) for item in self.model_files):
                raise ValueError("model files must be beneath model_root")
            if self.model_root / "rdf.yaml" not in (item.path for item in self.model_files):
                raise ValueError("model inventory must include rdf.yaml")
            if any(
                _is_within(item.path, self.model_root) for item in expected[len(self.model_files) :]
            ):
                raise ValueError("extension and engine files must be outside model_root")
        if any(
            _is_within(item.path, self.cache_root) or _is_within(self.cache_root, item.path)
            for item in expected
        ):
            raise ValueError("cache root must be separate from artifacts")
        _validate_cache_root(self.cache_root)


@dataclass(frozen=True)
class QuPathProbeFinding:
    status: Literal["ready", "missing", "incompatible", "error"]
    diagnostic: str
    observed_version: str | None = None
    evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("ready", "missing", "incompatible", "error"):
            raise ValueError("invalid probe finding status")
        if self.evidence_sha256 is not None and _SHA256.fullmatch(self.evidence_sha256) is None:
            raise ValueError("invalid evidence SHA-256")
        if self.status == "ready" and self.evidence_sha256 is None:
            raise ValueError("ready finding requires checked evidence")


@dataclass(frozen=True)
class QuPathPrerequisiteProbeResult:
    qupath: QuPathProbeFinding
    instanseg: QuPathProbeFinding
    model: QuPathProbeFinding | None
    cpu_engine: QuPathProbeFinding
    warpy: QuPathProbeFinding | None = None


@dataclass(frozen=True)
class LocalInventoryCheck:
    status: Literal["verified", "missing", "incompatible"]
    digest_sha256: str | None
    diagnostic: str


def _absolute_clean_path(path: Path, label: str) -> None:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be absolute and contain no traversal")


def _is_within(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


def _validate_cache_root(path: Path) -> None:
    # The caller must provision this private application directory beforehand.
    # This preflight never creates cache state or grants ownership of a user path.
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("cache_root must already exist") from exc
    if _is_link(path) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("cache_root must be a real directory")
    if any(_is_link(parent) for parent in path.parents):
        raise ValueError("cache_root must not have a symlink or junction ancestor")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("cache_root must be owned by this user and private")


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction_check = getattr(path, "is_junction", None)
    if junction_check is not None and junction_check():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & reparse_flag)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Stable fields shared by stat and an opened handle on supported platforms."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _path_snapshot(path: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    snapshot = []
    for member in (*reversed(path.parents), path):
        if _is_link(member):
            raise ValueError(f"symlink or junction in artifact path: {member}")
        info = member.lstat()
        if not info.st_ino:
            raise ValueError(f"artifact path has no stable file identity: {member}")
        # Ancestor contents can change independently (e.g. another /tmp user).
        # Pin their identity and type, while retaining all leaf change checks.
        identity = (
            _identity(info)
            if member == path
            else (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        )
        snapshot.append((member, identity))
    return tuple(snapshot)


def _same_opened_identity(named: tuple, opened: tuple) -> bool:
    if os.name == "nt":
        # Windows path stat infers executable permission bits from the suffix;
        # CRT fstat does not. Creation-time views can differ as well. Retain
        # exact volume/file IDs, regular-file kind, byte size and mtime.
        return (
            named[0] != 0
            and named[1] != 0
            and named[:2] == opened[:2]
            and stat.S_IFMT(named[2]) == stat.S_IFMT(opened[2])
            and named[3:5] == opened[3:5]
        )
    return named == opened


def _file_check(item: ProbeFileExpectation) -> LocalInventoryCheck:
    path = item.path
    try:
        before = _path_snapshot(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return LocalInventoryCheck(
                "incompatible", None, f"artifact is not a regular file: {path}"
            )
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            opened_identity = _identity(opened)
            path_identity = before[-1][1]
            same_opened_file = _same_opened_identity(path_identity, opened_identity)
            if not stat.S_ISREG(opened.st_mode) or not same_opened_file:
                return LocalInventoryCheck(
                    "incompatible", None, f"artifact identity changed while opening: {path}"
                )
            if _path_snapshot(path) != before:
                return LocalInventoryCheck(
                    "incompatible", None, f"artifact path changed while opening: {path}"
                )
            for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                digest.update(block)
            if _identity(os.fstat(stream.fileno())) != _identity(opened):
                return LocalInventoryCheck(
                    "incompatible", None, f"artifact changed while hashing: {path}"
                )
        if _path_snapshot(path) != before:
            return LocalInventoryCheck(
                "incompatible", None, f"artifact path changed while hashing: {path}"
            )
    except FileNotFoundError:
        return LocalInventoryCheck("missing", None, f"artifact missing: {path}")
    except ValueError as exc:
        return LocalInventoryCheck("incompatible", None, str(exc))
    except OSError as exc:
        return LocalInventoryCheck(
            "incompatible",
            None,
            f"artifact cannot be verified: {path}: {exc.strerror or type(exc).__name__}",
        )
    observed = digest.hexdigest()
    if observed != item.sha256:
        return LocalInventoryCheck("incompatible", observed, f"artifact digest mismatch: {path}")
    return LocalInventoryCheck("verified", observed, f"verified artifact: {path}")


def _model_members(
    root: Path,
) -> tuple[dict[Path, tuple[int, int, int, int, int, int]], LocalInventoryCheck | None]:
    try:
        _path_snapshot(root)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return {}, LocalInventoryCheck("incompatible", None, "model root is not a directory")
        members = {root: _identity(info)}
        pending = [root]
        while pending:
            directory = pending.pop()
            for member in sorted(directory.iterdir()):
                if _is_link(member):
                    return {}, LocalInventoryCheck(
                        "incompatible", None, f"model contains a symlink or junction: {member}"
                    )
                member_info = member.lstat()
                kind = member_info.st_mode
                if not member_info.st_ino:
                    return {}, LocalInventoryCheck(
                        "incompatible", None, f"model member has no stable file identity: {member}"
                    )
                members[member] = _identity(member_info)
                if stat.S_ISDIR(kind):
                    pending.append(member)
                elif not stat.S_ISREG(kind):
                    return {}, LocalInventoryCheck(
                        "incompatible", None, f"model contains a non-regular member: {member}"
                    )
        return members, None
    except FileNotFoundError:
        return {}, LocalInventoryCheck("missing", None, f"model root missing: {root}")
    except ValueError as exc:
        return {}, LocalInventoryCheck("incompatible", None, str(exc))
    except OSError as exc:
        return {}, LocalInventoryCheck(
            "incompatible", None, f"model cannot be listed: {exc.strerror or type(exc).__name__}"
        )


def _component_check(
    items: tuple[ProbeFileExpectation, ...], *, root: Path | None = None
) -> LocalInventoryCheck:
    checks = tuple(_file_check(item) for item in items)
    for status in ("incompatible", "missing"):
        for check in checks:
            if check.status == status:
                return check
    if len(items) == 1 and root is None:
        return LocalInventoryCheck(
            "verified", checks[0].digest_sha256, "exact local artifact verified"
        )
    inventory = hashlib.sha256()
    for item, check in sorted(zip(items, checks), key=lambda pair: pair[0].path.as_posix()):
        name = item.path.relative_to(root).as_posix() if root is not None else item.path.as_posix()
        inventory.update(name.encode("utf-8"))
        inventory.update(b"\0")
        inventory.update(check.digest_sha256.encode("ascii"))
        inventory.update(b"\n")
    return LocalInventoryCheck(
        "verified", inventory.hexdigest(), "exact local artifact inventory verified"
    )


def _verify_local_inventory(
    request: QuPathPrerequisiteProbeRequest,
) -> Mapping[str, LocalInventoryCheck]:
    """Check active local bytes without launching QuPath or changing inputs."""
    if not isinstance(request, QuPathPrerequisiteProbeRequest):
        raise TypeError("request must be QuPathPrerequisiteProbeRequest")
    model = None
    if request.model_root is not None:
        members, listing_error = _model_members(request.model_root)
        model = listing_error
        if model is None:
            expected = {item.path for item in request.model_files}
            extra = {path for path in members if stat.S_ISREG(members[path][2])} - expected
            if extra:
                model = LocalInventoryCheck(
                    "incompatible", None, f"unexpected model file: {min(extra)}"
                )
            else:
                model = _component_check(request.model_files, root=request.model_root)
                if model.status == "verified":
                    after, after_error = _model_members(request.model_root)
                    if after_error is not None or after != members:
                        model = LocalInventoryCheck(
                            "incompatible", None, "model inventory changed during verification"
                        )
    checks = {
        "instanseg": _component_check((request.instanseg_jar,)),
        "djl": _component_check((request.djl_jar,)),
        "pytorch_engine": _component_check((request.pytorch_engine_jar,)),
        "cpu_engine": _component_check(request.cpu_engine_files),
    }
    if model is not None:
        checks["model"] = model
    if request.require_warpy:
        checks["warpy"] = _component_check((request.warpy_jar,))
    return MappingProxyType(checks)


_INVENTORY_COMPONENT_LABELS = {
    "instanseg": "InstanSeg extension",
    "djl": "DJL extension",
    "pytorch_engine": "PyTorch engine",
    "cpu_engine": "CPU libraries",
    "model": "InstanSeg model",
    "warpy": "Warpy extension",
}


def _failed_inventory_diagnostic(checks: Mapping[str, LocalInventoryCheck]) -> str:
    failures = (
        f"{_INVENTORY_COMPONENT_LABELS[key]} — {check.diagnostic}"
        for key, check in checks.items()
        if check.status != "verified"
    )
    return (
        "Required QuPath files could not be verified: "
        + "; ".join(failures)
        + "; no live observation"
    )


_PROBE_LIMIT = 65536
_PROBE_SCHEMA = "qupath-prerequisite-probe/v3"


class ProbeProcessRunner(Protocol):
    def __call__(
        self, command: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess: ...


def _run_bounded_probe(
    command: tuple[str, ...], *, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Bound streams and lifetime; use a process group or native kill-on-close Job."""
    deadline = time.monotonic() + timeout
    overflow = threading.Event()
    output = [bytearray(), bytearray()]
    threads = []
    readers = []
    writers = []
    process = job = native = None

    def read(stream, index):
        try:
            while block := stream.read(4096):
                remaining = _PROBE_LIMIT - len(output[index])
                output[index].extend(block[:remaining])
                if len(block) > remaining:
                    overflow.set()
                    terminate()
                    break
        except OSError:
            overflow.set()

    def terminate():
        if native is not None and job is not None:
            native.terminate_job(job)
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        if os.name == "nt":
            # Reuse the native owner: its suspended creation attaches the Job
            # atomically, before any user code can execute or spawn children.
            from mif_pipeline.pipeline_core.win32_process_containment import (
                Win32CtypesProcessPrimitives,
            )

            native = Win32CtypesProcessPrimitives()
            job = native.create_job("Local\\mif-prerequisite-" + secrets.token_hex(16))
            native.set_kill_on_close(job)
            for _ in range(2):
                read_fd, write_fd = os.pipe()
                readers.append(os.fdopen(read_fd, "rb", buffering=0))
                writers.append(os.fdopen(write_fd, "wb", buffering=0))
            process = native.create_suspended(
                command, job, stdout_log=writers[0], stderr_log=writers[1], environment=env
            )
            for writer in writers:
                writer.close()
            native.resume(process)
        elif os.name == "posix":
            process = subprocess.Popen(
                command,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            readers = [process.stdout, process.stderr]
        else:
            raise RuntimeError("Unsupported probe process platform")
        for index, stream in enumerate(readers):
            thread = threading.Thread(target=read, args=(stream, index))
            threads.append(thread)
            thread.start()
        remaining = max(0.001, deadline - time.monotonic())
        if native is not None:
            native.wait_job_empty(job, timeout_seconds=remaining)
            returncode = native.exit_code(process)
        else:
            returncode = process.wait(timeout=remaining)
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired(command, timeout)
        if overflow.is_set():
            raise RuntimeError("Probe output exceeds limit")
        return subprocess.CompletedProcess(command, returncode, bytes(output[0]), bytes(output[1]))
    finally:
        terminate()
        if native is not None:
            if job is not None:
                native.wait_job_empty(job, timeout_seconds=5)
            if process is not None:
                native.close_process(process)
            if job is not None:
                native.close_job(job)
        elif process is not None:
            process.wait(timeout=5)
        for writer in writers:
            writer.close()
        for thread in threads:
            thread.join(5)
        for stream in readers:
            stream.close()


def _nonlive_result(request, diagnostic, *, checks=None, qupath_status="error"):
    absent = QuPathProbeFinding("error", diagnostic)

    def finding(name):
        check = None if checks is None else checks.get(name)
        if check is not None and check.status != "verified":
            return QuPathProbeFinding(check.status, check.diagnostic)
        return absent

    instanseg = finding("instanseg")
    if instanseg.status == "error" and checks is not None and checks["djl"].status != "verified":
        instanseg = finding("djl")
    cpu_engine = finding("cpu_engine")
    if (
        cpu_engine.status == "error"
        and checks is not None
        and checks["pytorch_engine"].status != "verified"
    ):
        cpu_engine = finding("pytorch_engine")
    return QuPathPrerequisiteProbeResult(
        QuPathProbeFinding(qupath_status, diagnostic),
        instanseg,
        finding("model") if request.model_root is not None else None,
        cpu_engine,
        finding("warpy") if request.require_warpy else None,
    )


def _native_route(items):
    """Map reviewed unpacked payload bytes onto DJL's documented override route.

    JAR extraction and arbitrary cache layouts are intentionally unsupported.
    DJL loads libtorch from PYTORCH_LIBRARY_PATH and checks unversioned JNI there
    before consulting its engine cache. This does not attest OS mapped bytes.
    """
    if os.name == "nt":
        required, jni = {"torch.dll", "torch_cpu.dll", "c10.dll"}, "djl_torch.dll"
        suffix = r".*\.dll"
    elif os.name == "posix" and os.uname().sysname == "Linux":
        required, jni = {"libtorch.so", "libtorch_cpu.so", "libc10.so"}, "libdjl_torch.so"
        suffix = r".*\.so(?:\.[0-9]+)*"
    else:
        raise ValueError("Unsupported native payload platform")
    routed = {}
    for item in items:
        name = item.path.name
        if not re.fullmatch(suffix, name):
            raise ValueError("CPU payload must contain unpacked platform libraries")
        if re.fullmatch(r"(?:[0-9]+\.[0-9]+\.[0-9]+-)?" + re.escape(jni), name):
            name = jni
        if name in routed:
            raise ValueError("CPU payload has duplicate native destinations")
        routed[name] = item
    if not required.union({jni}).issubset(routed):
        raise ValueError("CPU payload lacks libtorch, CPU dependencies, or JNI")
    return routed


def _probe_environment(cache, native):
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "DISPLAY",
        "XAUTHORITY",
        "LANG",
        "LC_ALL",
        "TMP",
        "TEMP",
        "TMPDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    # The selected executable uses its packaged JVM; avoid inherited launcher,
    # JVM, LD_*, PYTORCH_*, CLASSPATH, and engine configuration overrides.
    environment.update(
        DJL_OFFLINE="true",
        DJL_DEFAULT_ENGINE="PyTorch",
        DJL_CACHE_DIR=str(cache),
        ENGINE_CACHE_DIR=str(cache),
        PYTORCH_LIBRARY_PATH=str(native),
        PYTORCH_FLAVOR="cpu",
        OPT_OUT_TRACKING="true",
    )
    return environment


def _closed_object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("Invalid evidence object fields")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evidence field")
        result[key] = value
    return result


def _decode_probe_evidence(encoded, request, challenge, inventory):
    evidence = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )
    keys = {
        "schema",
        "challenge",
        "qupath_version",
        "preferences_namespace",
        "user_directory",
        "instanseg",
        "djl",
        "model",
        "cpu_engine",
    }
    if request.require_warpy:
        keys.add("warpy")
    _closed_object(evidence, keys)
    if evidence["schema"] != _PROBE_SCHEMA or evidence["challenge"] != challenge:
        raise ValueError("Evidence identity mismatch")
    namespace = evidence["preferences_namespace"]
    user_directory = evidence["user_directory"]
    if (
        type(namespace) is not str
        or not namespace.startswith("/")
        or "\0" in namespace
        or not namespace.strip()
        or (user_directory is not None and type(user_directory) is not str)
    ):
        raise ValueError("Invalid native profile observation")
    digest = hashlib.sha256(encoded).hexdigest()

    def observation(name, fields):
        item = _closed_object(evidence[name], {"status", "diagnostic", *fields})
        if item["status"] not in ("ready", "missing", "incompatible") or not isinstance(
            item["diagnostic"], str
        ):
            raise ValueError("Invalid observation status")
        return item

    def nullable_strings(item, fields):
        for field in fields:
            if item[field] is not None and not isinstance(item[field], str):
                raise ValueError("Invalid observation text type")

    def failed_fields(item, empty):
        if item["status"] != "ready" and any(item[key] != value for key, value in empty.items()):
            raise ValueError("Failed observation carries successful identity fields")

    def result(item, valid, version=None):
        status = item["status"]
        if status == "ready" and not valid:
            status = "incompatible"
        return QuPathProbeFinding(
            status,
            (
                "Fresh offline observation accepted"
                if status == "ready"
                else "Local component absent or incompatible with required evidence"
            ),
            version,
            digest if status == "ready" else None,
        )

    def extension(name, expectation, version):
        item = observation(name, {"loaded_version", "code_source", "code_source_sha256"})
        nullable_strings(item, ("loaded_version", "code_source", "code_source_sha256"))
        failed_fields(item, dict(loaded_version=None, code_source=None, code_source_sha256=None))
        return result(
            item,
            item["loaded_version"] == version
            and item["code_source"] == str(expectation.path)
            and item["code_source_sha256"] == expectation.sha256,
            item["loaded_version"] if isinstance(item["loaded_version"], str) else None,
        )

    instanseg = extension("instanseg", request.instanseg_jar, "0.1.7")
    djl = extension("djl", request.djl_jar, "0.4.2")
    if instanseg.status == "ready" and djl.status != "ready":
        instanseg = djl
    model_result = None
    if request.model_root is None:
        if evidence["model"] is not None or inventory is not None:
            raise ValueError("Runtime-only evidence must have no model")
    else:
        model = observation("model", {"version", "inventory_sha256", "valid"})
        nullable_strings(model, ("version", "inventory_sha256"))
        if type(model["valid"]) is not bool:
            raise ValueError("Invalid model validity type")
        failed_fields(model, dict(version=None, inventory_sha256=None, valid=False))
        model_result = result(
            model,
            model["valid"] is True
            and model["version"] == request.model_version
            and model["inventory_sha256"] == inventory,
            model["version"] if isinstance(model["version"], str) else None,
        )
    cpu = observation(
        "cpu_engine",
        {"engine_name", "native_loaded", "tensor_scalar", "code_source", "code_source_sha256"},
    )
    nullable_strings(cpu, ("engine_name", "code_source", "code_source_sha256"))
    if type(cpu["native_loaded"]) is not bool:
        raise ValueError("Invalid native load flag type")
    failed_fields(
        cpu,
        dict(
            engine_name=None,
            native_loaded=False,
            tensor_scalar=None,
            code_source=None,
            code_source_sha256=None,
        ),
    )
    cpu_result = result(
        cpu,
        cpu["engine_name"] == "PyTorch"
        and cpu["native_loaded"] is True
        and type(cpu["tensor_scalar"]) in (int, float)
        and cpu["tensor_scalar"] == 2.0
        and cpu["code_source"] == str(request.pytorch_engine_jar.path)
        and cpu["code_source_sha256"] == request.pytorch_engine_jar.sha256,
    )
    if cpu_result.status == "ready" and djl.status != "ready":
        cpu_result = QuPathProbeFinding(
            djl.status, "DJL extension did not satisfy the pinned requirement"
        )
    warpy = extension("warpy", request.warpy_jar, "0.5.0") if request.require_warpy else None
    version = evidence["qupath_version"]
    if not isinstance(version, str):
        raise ValueError("Invalid QuPath version")
    if re.fullmatch(r"0\.7\.[0-9]+", version) is None:
        return _nonlive_result(request, "Unsupported QuPath version", qupath_status="incompatible")
    if request.launch_binding is not None and version != request.launch_binding.qupath_version:
        return _nonlive_result(
            request,
            "Observed QuPath version differs from the selected launch binding",
            qupath_status="incompatible",
        )
    if request.launch_binding is not None and (
        namespace != "/" + request.launch_binding.preferences_namespace
        or user_directory != str(request.launch_binding.profile_root)
    ):
        return _nonlive_result(
            request,
            "Observed native profile differs from the selected launch binding",
            qupath_status="incompatible",
        )
    return QuPathPrerequisiteProbeResult(
        QuPathProbeFinding("ready", "Fresh QuPath observation accepted", version, digest),
        instanseg,
        model_result,
        cpu_result,
        warpy,
    )


def probe_qupath_prerequisites(
    request: QuPathPrerequisiteProbeRequest, *, process_runner: ProbeProcessRunner | None = None
) -> QuPathPrerequisiteProbeResult:
    """Observe prerequisites offline without a project or image.

    CPU readiness means hash-checked reviewed local payload on DJL's supported
    native route plus a fresh CPU tensor operation, not mapped-library identity.
    The injected runner is a trusted application/test seam, not evidence authority.
    """
    if not isinstance(request, QuPathPrerequisiteProbeRequest):
        raise TypeError("request must be QuPathPrerequisiteProbeRequest")
    if not request.executable.is_file():
        return _nonlive_result(
            request, "QuPath executable is missing; no live observation", qupath_status="missing"
        )
    checks = _verify_local_inventory(request)
    if any(check.status != "verified" for check in checks.values()):
        return _nonlive_result(request, _failed_inventory_diagnostic(checks), checks=checks)
    try:
        route = _native_route(request.cpu_engine_files)
    except ValueError as exc:
        checks = dict(checks, cpu_engine=LocalInventoryCheck("incompatible", None, str(exc)))
        return _nonlive_result(
            request, "Native load route unavailable; no live observation", checks=checks
        )
    try:
        _validate_cache_root(request.cache_root)
        with (
            tempfile.TemporaryDirectory(prefix="qupath-probe-") as exchange_name,
            tempfile.TemporaryDirectory(prefix="probe-", dir=request.cache_root) as cache_name,
        ):
            exchange, cache = Path(exchange_name).resolve(), Path(cache_name).resolve()
            if request.model_root is not None and (
                exchange == request.model_root or _is_within(exchange, request.model_root)
            ):
                raise ValueError("Probe exchange overlaps model")
            native = cache / "native"
            native.mkdir(mode=0o700)
            for name, item in route.items():
                target = native / name
                shutil.copyfile(item.path, target)
                if _file_check(ProbeFileExpectation(target, item.sha256)).status != "verified":
                    raise ValueError("Native payload changed while staging")
            script = exchange / "probe.groovy"
            from .scripts import internal_probe_bytes
            script.write_bytes(internal_probe_bytes("general/03_probe_prerequisites.groovy"))
            request_path, evidence_path = exchange / "request.json", exchange / "evidence.json"
            challenge = secrets.token_hex(32)
            inventory = checks["model"].digest_sha256 if request.model_root is not None else None
            request_path.write_text(
                json.dumps(
                    dict(
                        schema=_PROBE_SCHEMA,
                        challenge=challenge,
                        model_version=request.model_version,
                        model_inventory_sha256=inventory,
                        require_warpy=request.require_warpy,
                    )
                ),
                encoding="utf-8",
            )
            command = qupath_command(
                request.executable,
                "script",
                "--args",
                str(request_path),
                "--args",
                str(evidence_path),
                "--args",
                str(request.model_root) if request.model_root is not None else "",
                "--args",
                str(cache),
                str(script),
                launch_binding=request.launch_binding,
            )
            completed = (process_runner or _run_bounded_probe)(
                command, env=_probe_environment(cache, native), timeout=request.timeout_seconds
            )
            if completed.returncode != 0 or any(
                len(stream or b"") > _PROBE_LIMIT for stream in (completed.stdout, completed.stderr)
            ):
                raise ValueError("Probe process failed or exceeded output cap")
            if _is_link(evidence_path) or not stat.S_ISREG(evidence_path.lstat().st_mode):
                raise ValueError("Unsafe evidence member")
            with evidence_path.open("rb") as stream:
                encoded = stream.read(_PROBE_LIMIT + 1)
            if len(encoded) > _PROBE_LIMIT:
                raise ValueError("Evidence exceeds cap")
            return _decode_probe_evidence(encoded, request, challenge, inventory)
    except subprocess.TimeoutExpired:
        return _nonlive_result(
            request,
            f"QuPath did not finish checking the segmentation runtime within {request.timeout_seconds:g} seconds. "
            "Retry after other processing finishes. If the check stops again, open Software setup and check the InstanSeg processing libraries.",
            checks=checks,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return _nonlive_result(
            request, "Probe failed without accepted evidence: " + type(exc).__name__
        )
