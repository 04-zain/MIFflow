"""Launch QuPath with the declared project, registered script and managed process lifetime.

The runner constructs the native command and environment, retains logs and process-cleanup
evidence, and returns the operation's exit result. Interactive sessions use the managed
desktop bootstrap. Mutating stages keep their project lifetime resources until the native
process has completed the required cleanup."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.pipeline_core.execution_resources import native_compute_environment
from .registration_transform import validate_transform_reader_request

from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from functools import wraps
import array
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import warnings
from threading import Lock, RLock
from types import MappingProxyType
from weakref import WeakKeyDictionary
from typing import Literal, NoReturn, ParamSpec, Protocol, TypeAlias, TypeVar

from mif_containment.client import (
    ContainmentBrokerClient,
    ContainmentCompatibilityExpectationV1,
    ContainmentRejectedError,
)
from mif_containment.codec import dumps_canonical, loads_canonical_packet
from mif_containment.protocol import (
    CONTROL_SOCKET_FD,
    AttachPayloadV1,
    AuthorityBindingV1,
    GateReadyV1,
)
from mif_pipeline.pipeline_core.errors import ROIAnalysisError
from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityMemberRefV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableDirectoryAuthority,
    DurableRecordError,
    RuntimeAuthorityBindingV2,
    canonical_json_bytes,
)
from mif_pipeline.pipeline_core.process_containment import (
    ContainedMutationDrainRecord,
    ContainedMutationLaunchBinding,
    ContainedMutationLaunchRecord,
    ContainedMutationRecorder,
    ContainedMutationSpawnAttempt,
    ContainedProcessResult,
    ContainmentLaunchRequest,
    ProcessAttemptObserver,
    ProcessContainmentDrainEvidence,
    ProcessContainmentLaunchEvidence,
    ProcessDrainObserver,
    LinuxLocalProcessGroupAttachmentV1,
    LinuxSystemdScopeAttachmentV1,
    WindowsKillOnCloseJobAttachmentV1,
    _ContainedProcessError,
    _ContainedProcessFailure,
    authoritative_lock_descriptors,
    retain_authoritative_lock_descriptor,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleOwner,
)
from mif_pipeline.pipeline_core.win32_process_containment import (
    authoritative_job_name,
)
from mif_pipeline.pipeline_core.stage_result import StageResult

from .launch_binding import QuPathProfileLaunchBinding, qupath_command
from .exchange import (
    ExchangeContractError,
    REQUEST_SCHEMA,
    StageExchangePaths,
    validate_canonical_image_uri,
)
from .project import ProjectLayout
from .scripts import (
    _source_bytes,
    QuPathScriptAsset,
    bundle_script_path,
    catalogue_runtime_authority_binding_v2,
    materialize_script_bundle,
    publish_registered_script_asset,
    registered_script_assets,
    script_path,
    training_runtime_authority_binding_v2,
)
from ._script_asset_registry import relative_script_path
from .registration_session import (
    CurrentRegistrationProjectSnapshot,
    ExistingRegistrationProjectSession,
    RegisteredRegistrationRoleRequest,
    RegistrationRoleExecutionDescriptor,
    RegistrationRuntimeTargetPlanSeed,
    decode_registered_registration_role_request,
)
from .training import (
    QuPathNativeTrainingRequest,
    QuPathNativeTrainingResult,
    _issue_qupath_native_training_request,
    decode_qupath_native_training_request,
    decode_qupath_native_training_result,
)

class ProcessExecutor(Protocol):
    """Launch a command with the caller's selected native resource settings."""

    def __call__(self, command: tuple[str, ...], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]: ...

LinuxContainmentProfile: TypeAlias = Literal[
    "hardened-broker",
    "prototype-local-process-group",
]
_TrainingProcessDisposition: TypeAlias = Literal[
    "completed",
    "drained_failure",
    "never_entered_child_creation",
]
_TrainingProcessFailureKind: TypeAlias = Literal[
    "nonzero_exit",
    "timeout",
    "cancelled",
    "missing_result",
    "invalid_result",
]
_RegistrationProcessDisposition: TypeAlias = Literal[
    "completed",
    "drained_failure",
    "never_entered_child_creation",
]
_RegistrationProcessFailureKind: TypeAlias = Literal[
    "nonzero_exit",
    "timeout",
    "cancelled",
    "missing_evidence",
    "invalid_evidence",
]

_LINUX_EXEC_GATE = "/usr/libexec/mif-pipeline/mif-qupath-exec-gate"
_MAXIMUM_GATE_PACKET_BYTES = 65_536
_MAXIMUM_LOG_EXCERPT_BYTES = 65_536
_GATE_AUTHORITY_LOCK_FDS = (5, 6)
_CANCELLATION_POLL_SECONDS = 0.05
_PROJECT_STAGE_CONTEXT_VERSION = "1"
_PROJECT_STAGE_RESERVED_ENVIRONMENT = frozenset(
    {
        "MIF_QUPATH_PROJECT_STAGE_CONTEXT_VERSION",
        "MIF_QUPATH_PROJECT_STAGE_RUN_PLAN_PATH",
        "MIF_QUPATH_PROJECT_STAGE_PREPARED_DESCRIPTOR_PATH",
        "MIF_QUPATH_PROJECT_STAGE_PROCESS_LAUNCH_PATH",
        "MIF_QUPATH_PROJECT_STAGE_BUNDLE_MANIFEST_PATH",
        "MIF_QUPATH_PROJECT_STAGE_GUARD_SCRIPT_PATH",
        "MIF_QUPATH_PROJECT_STAGE_TARGET_SCRIPT_PATH",
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE",
    }
)
_PROJECT_STAGE_ENVIRONMENT_PREFIX = "mif_qupath_project_stage_"
_PROJECT_STAGE_MAX_INPUT_BYTES = 16 * 1024 * 1024
_REGISTRATION_BUNDLE_MAX_MEMBERS = 4096
_REGISTRATION_BUNDLE_MAX_BYTES = 64 * 1024 * 1024
_REGISTRATION_BUNDLE_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
_NATIVE_TRAINING_CONTEXT_VERSION = "1"
_NATIVE_TRAINING_RESERVED_ENVIRONMENT = frozenset(
    {
        "MIF_QUPATH_NATIVE_TRAINING_CONTEXT_VERSION",
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_PATH",
        "MIF_QUPATH_NATIVE_TRAINING_RESULT_PATH",
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_SHA256",
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE",
    }
)
_NATIVE_TRAINING_ENVIRONMENT_PREFIX = "mif_qupath_native_training_"

_P = ParamSpec("_P")
_R = TypeVar("_R")


# ==============================================================================
#                                     Runner
# ==============================================================================


def _thaw_native_training_request_projection(value: object) -> object:
    """Copy a registry's deeply immutable JSON projection for the codec."""

    if isinstance(value, Mapping):
        return {key: _thaw_native_training_request_projection(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_native_training_request_projection(item) for item in value]
    return value


class _CancellationRequested(Exception):
    """Internal synchronous cancellation marker observed at a safe boundary."""


def _has_exact_backend_cancellation_cause(
    error: _ContainedProcessError,
) -> bool:
    from mif_pipeline.pipeline_core.local_process_group_containment import (
        _CancellationRequested as LocalCancellationRequested,
    )
    from mif_pipeline.pipeline_core.win32_process_containment import (
        _CancellationRequested as WindowsCancellationRequested,
    )

    return type(error.__cause__) in (
        _CancellationRequested,
        LocalCancellationRequested,
        WindowsCancellationRequested,
    )


def _add_bounded_secondary_note(
    primary: BaseException,
    secondary: BaseException,
    *,
    context: str,
) -> None:
    """Best-effort diagnostics that cannot replace an operational failure."""

    try:
        primary.add_note(f"{context}: {type(secondary).__name__}: {str(secondary)[:240]}"[:512])
    except BaseException:
        pass


def format_qupath_process_diagnostics(
    *,
    termination_reason: str | None,
    exit_status: int | None,
    log_paths: tuple[Path, Path],
    excerpts: tuple[str | None, str | None],
    truncated: tuple[bool | None, bool | None],
    log_error: str | None = None,
) -> str:
    """Format retained process facts within the durable failure-message budget.

    Callers supply existing excerpts or read saved logs once after a failed
    process. This formatter performs no reads. Its 4000-byte bound leaves room
    for each stage's introduction within the terminal record's 4096-byte limit.
    """
    lines = [f"QuPath process: {termination_reason or 'not started'}; exit status {exit_status}."]
    # Keep the reported exception ahead of startup messages and stack frames.
    # The full excerpts remain below; we do not infer a correction from the text.
    reported_errors = [
        line.strip()
        for excerpt in excerpts for line in (excerpt or "").splitlines()
        if re.search(r"(?:[\w$]+\.)*[\w$]*(?:Exception|Error):\s*\S", line)
    ]
    if reported_errors:
        lines.append("QuPath reported: " + reported_errors[-1][:1000])
    for stream, path in zip(("stdout", "stderr"), log_paths, strict=True):
        lines.append(f"Saved {stream} log: {path}")
    if log_error is not None:
        lines.append(f"Native log excerpts could not be read: {log_error[:500]}")
    else:
        encoded = [(text or "").replace("\x00", "?").encode("utf-8") for text in excerpts]
        budget = max(0, 4000 - len("\n\n".join(lines).encode("utf-8")) - 256)
        stdout_budget = min(len(encoded[0]), budget - min(len(encoded[1]), budget // 2))
        budgets = (stdout_budget, min(len(encoded[1]), budget - stdout_budget))
        for stream, raw, limit, was_truncated in zip(
            ("stdout", "stderr"), encoded, budgets, truncated, strict=True,
        ):
            if raw:
                label = f"QuPath {stream} excerpt"
                if was_truncated or len(raw) > limit:
                    label += " (truncated; full output in saved log)"
                lines.append(f"{label}:\n{raw[:limit].decode('utf-8', errors='ignore').rstrip()}")
    return "\n\n".join(lines).replace("\x00", "?").encode("utf-8")[:4000].decode("utf-8", errors="ignore")


class _ProjectStageRetainedExitStack(ExitStack):
    """Exhaust project-stage cleanup without replacing an active failure."""

    def __exit__(self, *exc_details: object) -> bool:
        active = exc_details[1] if len(exc_details) > 1 else None
        try:
            return bool(super().__exit__(*exc_details))  # type: ignore[arg-type]
        except BaseException as cleanup:
            if not isinstance(active, BaseException):
                raise
            _add_bounded_secondary_note(
                active,
                cleanup,
                context="project-stage retained cleanup also failed",
            )
            return False


def _posix_spawn_closefrom(*args: object, **kwargs: object) -> object:
    """Load the Linux-only GNU spawn adapter only on the broker path."""

    from mif_pipeline.pipeline_core.local_process_group_containment import (
        _posix_spawn_closefrom as spawn,
    )

    return spawn(*args, **kwargs)


def _project_stage_environment(
    inherited: Mapping[str, str],
    *,
    run_plan_path: Path,
    prepared_descriptor_path: Path,
    process_launch_path: Path,
    bundle_manifest_path: Path,
    guard_script_path: Path,
    target_script_path: Path,
    kernel_threads: int | None = None,
    raw_launch_nonce: bytes,
) -> dict[str, str]:
    if type(inherited) is not dict or any(
        type(name) is not str
        or not name
        or "\x00" in name
        or "=" in name
        or type(value) is not str
        or "\x00" in value
        for name, value in inherited.items()
    ):
        raise QuPathExecutionError("project-stage inherited environment is invalid")
    reserved_casefold = {name.casefold() for name in _PROJECT_STAGE_RESERVED_ENVIRONMENT}
    if any(
        name.casefold().startswith(_PROJECT_STAGE_ENVIRONMENT_PREFIX)
        or name.casefold() in reserved_casefold
        for name in inherited
    ):
        raise QuPathExecutionError(
            "project-stage inherited environment contains reserved authority"
        )
    paths = (
        run_plan_path,
        prepared_descriptor_path,
        process_launch_path,
        bundle_manifest_path,
        guard_script_path,
        target_script_path,
    )
    if (
        type(raw_launch_nonce) is not bytes
        or len(raw_launch_nonce) != 32
        or any(
            not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            for path in paths
        )
    ):
        raise QuPathExecutionError("project-stage private launch context is invalid")
    values = {
        "MIF_QUPATH_PROJECT_STAGE_CONTEXT_VERSION": _PROJECT_STAGE_CONTEXT_VERSION,
        "MIF_QUPATH_PROJECT_STAGE_RUN_PLAN_PATH": os.fspath(run_plan_path),
        "MIF_QUPATH_PROJECT_STAGE_PREPARED_DESCRIPTOR_PATH": os.fspath(prepared_descriptor_path),
        "MIF_QUPATH_PROJECT_STAGE_PROCESS_LAUNCH_PATH": os.fspath(process_launch_path),
        "MIF_QUPATH_PROJECT_STAGE_BUNDLE_MANIFEST_PATH": os.fspath(bundle_manifest_path),
        "MIF_QUPATH_PROJECT_STAGE_GUARD_SCRIPT_PATH": os.fspath(guard_script_path),
        "MIF_QUPATH_PROJECT_STAGE_TARGET_SCRIPT_PATH": os.fspath(target_script_path),
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE": raw_launch_nonce.hex(),
    }
    if set(values) != _PROJECT_STAGE_RESERVED_ENVIRONMENT or any(
        not value or "\x00" in value for value in values.values()
    ):
        raise QuPathExecutionError("project-stage reserved environment is incomplete")
    environment = native_compute_environment(inherited, kernel_threads=kernel_threads)
    environment["MIF_CELL_DATASET_IMPORT_COMMAND"] = json.dumps(
        [sys.executable, "--mif-internal-cell-dataset-import"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "mif_pipeline.datasets.native_cells"]
    )
    environment["MIF_CELLPOSE_COMMAND"] = json.dumps(
        [sys.executable, "--mif-internal-cellpose"] if getattr(sys, "frozen", False)
        else [sys.executable, "-u", "-m", "mif_pipeline.cell_analysis.cellpose_worker"]
    )
    environment.update(values)
    if os.name == "nt":
        environment_units = 1 + sum(
            len(f"{name}={value}\0".encode("utf-16-le")) // 2 for name, value in environment.items()
        )
        if environment_units > 32_767:
            raise QuPathExecutionError("project-stage Windows environment block is too large")
    return environment


def _training_environment(
    inherited: Mapping[str, str],
    *,
    request_path: Path,
    result_path: Path,
    request_sha256: str,
    raw_launch_nonce: bytes,
) -> dict[str, str]:
    if type(inherited) is not dict or any(
        type(name) is not str
        or not name
        or "\x00" in name
        or "=" in name
        or type(value) is not str
        or "\x00" in value
        for name, value in inherited.items()
    ):
        raise QuPathExecutionError("native-training inherited environment is invalid")
    reserved_casefold = {name.casefold() for name in _NATIVE_TRAINING_RESERVED_ENVIRONMENT}
    if any(
        name.casefold().startswith(_NATIVE_TRAINING_ENVIRONMENT_PREFIX)
        or name.casefold() in reserved_casefold
        for name in inherited
    ):
        raise QuPathExecutionError(
            "native-training inherited environment contains reserved authority"
        )
    if (
        not isinstance(request_path, Path)
        or not isinstance(result_path, Path)
        or not _project_stage_path_is_absolute(request_path)
        or not _project_stage_path_is_absolute(result_path)
        or ".." in _project_stage_path_parts(request_path)
        or ".." in _project_stage_path_parts(result_path)
        or type(request_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
        or type(raw_launch_nonce) is not bytes
        or len(raw_launch_nonce) != 32
    ):
        raise QuPathExecutionError("native-training private launch context is invalid")
    values = {
        "MIF_QUPATH_NATIVE_TRAINING_CONTEXT_VERSION": (_NATIVE_TRAINING_CONTEXT_VERSION),
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_PATH": os.fspath(request_path),
        "MIF_QUPATH_NATIVE_TRAINING_RESULT_PATH": os.fspath(result_path),
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_SHA256": request_sha256,
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE": raw_launch_nonce.hex(),
    }
    if set(values) != _NATIVE_TRAINING_RESERVED_ENVIRONMENT or any(
        not value or "\x00" in value for value in values.values()
    ):
        raise QuPathExecutionError("native-training reserved environment is incomplete")
    environment = native_compute_environment(inherited)
    environment.update(values)
    if os.name == "nt":
        environment_units = 1 + sum(
            len(f"{name}={value}\0".encode("utf-16-le")) // 2 for name, value in environment.items()
        )
        if environment_units > 32_767:
            raise QuPathExecutionError("native-training Windows environment block is too large")
    return environment


def _path_state(value: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_nlink),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(getattr(value, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)),
    )


def _project_stage_path_parts(path: Path) -> tuple[str, ...]:
    if os.name == "nt":
        return PureWindowsPath(os.fspath(path)).parts
    return path.parts


def _project_stage_path_is_absolute(path: Path) -> bool:
    if os.name == "nt":
        return PureWindowsPath(os.fspath(path)).is_absolute()
    return path.is_absolute()


def _normalized_windows_final_path(value: object) -> str:
    raw = os.fspath(value).replace("/", "\\")  # type: ignore[arg-type]
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return ntpath.normcase(ntpath.normpath(raw))


def _project_stage_windows_primitives() -> object:
    from mif_pipeline.pipeline_core.win32_file_authority import (
        Win32FilePrimitives,
    )

    class ReadOnlyProjectStagePrimitives(Win32FilePrimitives):
        # Both existing and validation opens exclude write/delete sharing.
        SHARE_READ_WRITE = 0x0001

        def final_path(self, handle: object) -> Path:
            function = self._kernel32.GetFinalPathNameByHandleW
            function.argtypes = [
                self._wintypes.HANDLE,
                self._wintypes.LPWSTR,
                self._wintypes.DWORD,
                self._wintypes.DWORD,
            ]
            function.restype = self._wintypes.DWORD
            capacity = 32_768
            buffer = self._ctypes.create_unicode_buffer(capacity)
            length = function(handle.value, buffer, capacity, 0)  # type: ignore[attr-defined]
            if length == 0 or length >= capacity:
                raise self._failure("GetFinalPathNameByHandleW")
            return Path(buffer.value)

    return ReadOnlyProjectStagePrimitives()


class _PinnedProjectStageFile:
    """One non-inheritable no-follow file plus its retained parent chain."""

    def __init__(
        self,
        path: Path,
        expected_bytes: bytes,
        *,
        label: str,
        _windows_primitives: object | None = None,
    ) -> None:
        if (
            not isinstance(path, Path)
            or not _project_stage_path_is_absolute(path)
            or ".." in _project_stage_path_parts(path)
            or type(expected_bytes) is not bytes
            or len(expected_bytes) > _PROJECT_STAGE_MAX_INPUT_BYTES
        ):
            raise QuPathExecutionError(f"{label} pin inputs are invalid")
        self.path = path
        self.expected_bytes = expected_bytes
        self.label = label
        self.directory_descriptors: list[int] = []
        self.directory_names: list[tuple[int, str, tuple[int, ...]]] = []
        self.file_descriptor: int | None = None
        self._windows_primitives: object | None = None
        self._windows_directory_handle: object | None = None
        self._windows_file_handle: object | None = None
        try:
            if os.name == "posix":
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                current = os.open(path.anchor, flags)
                self.directory_descriptors.append(current)
                for component in path.parent.parts[1:]:
                    named = os.stat(component, dir_fd=current, follow_symlinks=False)
                    child = os.open(component, flags, dir_fd=current)
                    opened = os.fstat(child)
                    if (
                        not stat.S_ISDIR(named.st_mode)
                        or _path_state(named)[:3] != _path_state(opened)[:3]
                    ):
                        os.close(child)
                        raise QuPathExecutionError(f"{label} directory identity changed")
                    self.directory_names.append((current, component, _path_state(named)[:3]))
                    self.directory_descriptors.append(child)
                    current = child
                named = os.stat(path.name, dir_fd=current, follow_symlinks=False)
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or not stat.S_ISREG(opened.st_mode)
                    or named.st_nlink != 1
                    or opened.st_nlink != 1
                    or _path_state(named) != _path_state(opened)
                ):
                    os.close(descriptor)
                    raise QuPathExecutionError(f"{label} file identity changed")
                self.file_descriptor = descriptor
                self.expected_state = _path_state(opened)
            elif os.name == "nt":
                primitives = (
                    _project_stage_windows_primitives()
                    if _windows_primitives is None
                    else _windows_primitives
                )
                self._windows_primitives = primitives
                directory = primitives.open_directory(path.parent, create=False, no_follow=True)
                self._windows_directory_handle = directory
                if (
                    primitives.is_reparse_point(directory)
                    or not primitives.is_directory(directory)
                    or _normalized_windows_final_path(primitives.final_path(directory))
                    != _normalized_windows_final_path(path.parent)
                ):
                    raise QuPathExecutionError(f"{label} directory identity changed")
                self._windows_directory_identity = primitives.identity(directory)
                file_handle = primitives.open_file(
                    directory,
                    path.name,
                    no_follow=True,
                    share_delete=False,
                )
                self._windows_file_handle = file_handle
                if (
                    primitives.is_reparse_point(file_handle)
                    or not primitives.is_regular_file(file_handle)
                    or primitives.link_count(file_handle) != 1
                    or primitives.size(file_handle) != len(expected_bytes)
                    or _normalized_windows_final_path(primitives.final_path(file_handle))
                    != _normalized_windows_final_path(path)
                ):
                    raise QuPathExecutionError(f"{label} file identity changed")
                self._windows_file_identity = primitives.identity(file_handle)
            else:
                raise QuPathExecutionError(f"{label} native file pinning is unsupported")
            self.validate()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                try:
                    primary.add_note(f"{label} pin cleanup also failed: {cleanup!r}"[:512])
                except BaseException:
                    pass
            raise

    def _read(self) -> bytes:
        if self._windows_primitives is not None:
            assert self._windows_file_handle is not None
            return self._windows_primitives.read_bounded(
                self._windows_file_handle,
                _PROJECT_STAGE_MAX_INPUT_BYTES + 1,
            )
        assert self.file_descriptor is not None
        os.lseek(self.file_descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        retained = 0
        while retained <= _PROJECT_STAGE_MAX_INPUT_BYTES:
            chunk = os.read(
                self.file_descriptor,
                min(64 * 1024, _PROJECT_STAGE_MAX_INPUT_BYTES + 1 - retained),
            )
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            retained += len(chunk)
        raise QuPathExecutionError(f"{self.label} exceeds its bounded size")

    def validate(self) -> None:
        if self._windows_primitives is not None:
            primitives = self._windows_primitives
            directory = self._windows_directory_handle
            file_handle = self._windows_file_handle
            if directory is None or file_handle is None:
                raise QuPathExecutionError(f"{self.label} pin is closed")
            if (
                primitives.is_reparse_point(directory)
                or not primitives.is_directory(directory)
                or primitives.identity(directory) != self._windows_directory_identity
                or _normalized_windows_final_path(primitives.final_path(directory))
                != _normalized_windows_final_path(self.path.parent)
                or primitives.is_reparse_point(file_handle)
                or not primitives.is_regular_file(file_handle)
                or primitives.link_count(file_handle) != 1
                or primitives.size(file_handle) != len(self.expected_bytes)
                or primitives.identity(file_handle) != self._windows_file_identity
                or primitives.child_identity(directory, self.path.name)
                != self._windows_file_identity
                or _normalized_windows_final_path(primitives.final_path(file_handle))
                != _normalized_windows_final_path(self.path)
                or self._read() != self.expected_bytes
            ):
                raise QuPathExecutionError(f"{self.label} changed before release")
            return
        if self.file_descriptor is None:
            raise QuPathExecutionError(f"{self.label} pin is closed")
        for parent, component, expected in self.directory_names:
            named = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if _path_state(named)[:3] != expected:
                raise QuPathExecutionError(f"{self.label} directory authority was replaced")
        named = (
            os.stat(
                self.path.name,
                dir_fd=self.directory_descriptors[-1],
                follow_symlinks=False,
            )
            if os.name == "posix"
            else self.path.lstat()
        )
        opened = os.fstat(self.file_descriptor)
        if (
            _path_state(named) != self.expected_state
            or _path_state(opened) != self.expected_state
            or self._read() != self.expected_bytes
        ):
            raise QuPathExecutionError(f"{self.label} changed before release")

    def close(self) -> None:
        failures: list[BaseException] = []
        if self._windows_primitives is not None:
            primitives = self._windows_primitives
            for name in ("_windows_file_handle", "_windows_directory_handle"):
                handle = getattr(self, name)
                if handle is not None:
                    setattr(self, name, None)
                    try:
                        primitives.close(handle)
                    except BaseException as exc:
                        failures.append(exc)
            if failures:
                raise failures[0]
            return
        if self.file_descriptor is not None:
            descriptor = self.file_descriptor
            self.file_descriptor = None
            try:
                os.close(descriptor)
            except BaseException as exc:
                failures.append(exc)
        descriptors = tuple(reversed(self.directory_descriptors))
        self.directory_descriptors.clear()
        self.directory_names.clear()
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            primary = failures[0]
            for cleanup in failures[1:]:
                try:
                    primary.add_note(
                        "additional project-stage pin close failure: " f"{cleanup!r}"[:512]
                    )
                except BaseException:
                    pass
            raise primary


class _PinnedProjectStageDirectory:
    """Retain one absent launch member's exact directory identity."""

    def __init__(
        self,
        member: Path,
        *,
        label: str,
        _windows_primitives: object | None = None,
    ) -> None:
        if (
            not isinstance(member, Path)
            or not _project_stage_path_is_absolute(member)
            or ".." in _project_stage_path_parts(member)
        ):
            raise QuPathExecutionError(f"{label} path is invalid")
        self.member = member
        self.label = label
        self._windows_primitives: object | None = None
        self._windows_directory_handle: object | None = None
        self.pin = _PinnedProjectStageFile.__new__(_PinnedProjectStageFile)
        self.pin.path = member.parent / "."
        self.pin.expected_bytes = b""
        self.pin.label = label
        self.pin.directory_descriptors = []
        self.pin.directory_names = []
        self.pin.file_descriptor = None
        self.pin._windows_primitives = None
        self.pin._windows_directory_handle = None
        self.pin._windows_file_handle = None
        try:
            self._open_directory(
                member.parent,
                _windows_primitives=_windows_primitives,
            )
            self.validate(require_absent=True)
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                try:
                    primary.add_note(f"{label} pin cleanup also failed: {cleanup!r}"[:512])
                except BaseException:
                    pass
            raise

    def _open_directory(self, path: Path, *, _windows_primitives: object | None) -> None:
        if os.name == "nt":
            primitives = (
                _project_stage_windows_primitives()
                if _windows_primitives is None
                else _windows_primitives
            )
            self._windows_primitives = primitives
            directory = primitives.open_directory(path, create=False, no_follow=True)
            self._windows_directory_handle = directory
            if (
                primitives.is_reparse_point(directory)
                or not primitives.is_directory(directory)
                or _normalized_windows_final_path(primitives.final_path(directory))
                != _normalized_windows_final_path(path)
            ):
                raise QuPathExecutionError(f"{self.label} parent identity changed")
            self._windows_directory_identity = primitives.identity(directory)
            return
        if os.name != "posix":
            raise QuPathExecutionError(f"{self.label} native directory pinning is unsupported")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        current = os.open(path.anchor, flags)
        self.pin.directory_descriptors.append(current)
        for component in path.parts[1:]:
            named = os.stat(component, dir_fd=current, follow_symlinks=False)
            child = os.open(component, flags, dir_fd=current)
            opened = os.fstat(child)
            if not stat.S_ISDIR(named.st_mode) or _path_state(named)[:3] != _path_state(opened)[:3]:
                os.close(child)
                raise QuPathExecutionError(f"{self.label} parent identity changed")
            self.pin.directory_names.append((current, component, _path_state(named)[:3]))
            self.pin.directory_descriptors.append(child)
            current = child

    def validate(self, *, require_absent: bool) -> None:
        if self._windows_primitives is not None:
            primitives = self._windows_primitives
            directory = self._windows_directory_handle
            if (
                directory is None
                or primitives.is_reparse_point(directory)
                or not primitives.is_directory(directory)
                or primitives.identity(directory) != self._windows_directory_identity
                or _normalized_windows_final_path(primitives.final_path(directory))
                != _normalized_windows_final_path(self.member.parent)
            ):
                raise QuPathExecutionError(f"{self.label} parent authority was replaced")
            if (
                require_absent
                and primitives.child_identity(directory, self.member.name) is not None
            ):
                raise QuPathExecutionError(f"{self.label} member already exists")
            return
        if os.name == "posix":
            for parent, component, expected in self.pin.directory_names:
                named = os.stat(component, dir_fd=parent, follow_symlinks=False)
                if _path_state(named)[:3] != expected:
                    raise QuPathExecutionError(f"{self.label} parent authority was replaced")
        if require_absent:
            if os.name == "posix":
                try:
                    os.stat(
                        self.member.name,
                        dir_fd=self.pin.directory_descriptors[-1],
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise QuPathExecutionError(f"{self.label} member already exists")

    def close(self) -> None:
        if self._windows_primitives is not None:
            handle = self._windows_directory_handle
            self._windows_directory_handle = None
            if handle is not None:
                self._windows_primitives.close(handle)
            return
        self.pin.close()


def _guarded_qupath_process(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        runner = args[0]
        token = runner._runtime_lifecycle.acquire("qupath-process")  # type: ignore[attr-defined]
        try:
            return method(*args, **kwargs)
        finally:
            token.release()

    return guarded


def _read_contained_log_excerpt(stream: object) -> tuple[str, bool]:
    opener = getattr(stream, "open_reader", None)
    if not callable(opener):
        raise QuPathExecutionError("contained log stream lacks a secure read handle")
    try:
        with opener() as reader:
            reader.seek(0, os.SEEK_END)
            size = reader.tell()
            reader.seek(max(0, size - _MAXIMUM_LOG_EXCERPT_BYTES))
            raw = reader.read(_MAXIMUM_LOG_EXCERPT_BYTES)
    except (OSError, ValueError) as exc:
        raise QuPathExecutionError("contained log could not be reopened") from exc
    if type(size) is not int or size < 0 or type(raw) is not bytes:
        raise QuPathExecutionError("contained log read view is invalid")
    excerpt = raw.decode("utf-8", errors="replace")
    encoded = excerpt.encode("utf-8")
    representation_truncated = len(encoded) > _MAXIMUM_LOG_EXCERPT_BYTES
    if representation_truncated:
        excerpt = encoded[-_MAXIMUM_LOG_EXCERPT_BYTES:].decode("utf-8", errors="ignore")
    return (
        excerpt,
        size > _MAXIMUM_LOG_EXCERPT_BYTES or representation_truncated,
    )


def _finalize_contained_logs(
    stdout_log: object,
    stderr_log: object,
) -> tuple[str, str, bool, bool]:
    """Synchronize both exact writers, then reopen bounded read views."""

    failures: list[BaseException] = []
    for stream in (stdout_log, stderr_log):
        try:
            stream.flush()  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError) as exc:
            failures.append(exc)
        try:
            os.fsync(stream.fileno())  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError) as exc:
            failures.append(exc)
    if failures:
        primary = failures[0]
        for secondary in failures[1:]:
            primary.add_note(
                f"additional contained log synchronization failure: {secondary!r}"[:512]
            )
        raise QuPathExecutionError(
            "contained logs could not be synchronized after drain"
        ) from primary
    try:
        stdout_excerpt, stdout_truncated = _read_contained_log_excerpt(stdout_log)
        stderr_excerpt, stderr_truncated = _read_contained_log_excerpt(stderr_log)
    except QuPathExecutionError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise QuPathExecutionError("contained logs could not be synchronized after drain") from exc
    return (
        stdout_excerpt,
        stderr_excerpt,
        stdout_truncated,
        stderr_truncated,
    )


def _canonical_json_sha256(value: object) -> str:
    try:
        rendered = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise QuPathExecutionError("Warpy mutation request is not canonical JSON") from exc
    return hashlib.sha256(rendered).hexdigest()


def _warpy_project_identity_sha256(project_file: str) -> str:
    return _canonical_json_sha256(
        {
            "schema_version": "qupath-project-file-identity/v1",
            "project_file": project_file,
        }
    )


def _warpy_mutation_lease_sha256(lease: dict[str, object]) -> str:
    """Reproduce the closed Python/Groovy v5 lease signature exactly."""
    scope = lease["hierarchy_lock_scope"]
    assert type(scope) is dict
    values: list[str] = [
        lease["schema_version"],
        lease["attempt_id"],
        str(lease["epoch"]),
        lease["token"],
        lease["owner_id"],
        lease["operation"],
        lease["lease_head_path"],
        lease["hierarchy_lock_path"],
        scope["schema_version"],
        scope["project_file"],
        scope["fixed_project_entry_id"],
        lease["hierarchy_lock_scope_sha256"],
    ]
    for identity_name in ("attempt_store_identity", "hierarchy_lock_identity"):
        identity = lease[identity_name]
        assert type(identity) is dict
        values.append(identity["schema_version"])
        if identity["schema_version"] == "posix-file-identity/v1":
            values.extend((str(identity["device"]), str(identity["inode"])))
        else:
            values.extend((identity["volume_serial_number"], identity["file_id"]))
    if any(type(value) is not str or "\0" in value for value in values):
        raise QuPathExecutionError("Warpy mutation lease digest scope changed")
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _warpy_containment_context(
    project: ProjectLayout,
    request: object,
    *,
    operation: Literal["commit", "cleanup"],
) -> dict[str, str]:
    """Authenticate the closed v5 request before any process-side effect."""
    if type(operation) is not str or operation not in {"commit", "cleanup"}:
        raise QuPathExecutionError("Warpy mutation operation is invalid")
    if type(request) is not dict:
        raise QuPathExecutionError("Warpy mutation request must be one exact mapping")
    lease = request.get("mutation_lease")
    lease_fields = {
        "schema_version",
        "attempt_id",
        "epoch",
        "token",
        "owner_id",
        "operation",
        "lease_head_path",
        "hierarchy_lock_path",
        "hierarchy_lock_scope",
        "hierarchy_lock_scope_sha256",
        "attempt_store_identity",
        "hierarchy_lock_identity",
        "lease_sha256",
    }
    if type(lease) is not dict or set(lease) != lease_fields:
        raise QuPathExecutionError("Warpy mutation lease fields changed")
    if any(type(key) is not str for key in lease):
        raise QuPathExecutionError("Warpy mutation lease fields changed")
    string_fields = (
        "schema_version",
        "attempt_id",
        "token",
        "owner_id",
        "operation",
        "lease_head_path",
        "hierarchy_lock_path",
        "hierarchy_lock_scope_sha256",
        "lease_sha256",
    )
    if any(type(lease.get(name)) is not str for name in string_fields):
        raise QuPathExecutionError("Warpy mutation lease primitive type changed")
    if (
        lease["schema_version"] != "warpy-transfer-mutation-lease/v5"
        or lease["operation"] != operation
        or type(lease["epoch"]) is not int
        or lease["epoch"] < 1
        or type(request.get("attempt_id")) is not str
        or lease["attempt_id"] != request["attempt_id"]
        or re.fullmatch(r"transfer-attempt-[0-9a-f]{64}", lease["attempt_id"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", lease["token"]) is None
        or not lease["owner_id"].strip()
        or not Path(lease["lease_head_path"]).is_absolute()
        or not Path(lease["hierarchy_lock_path"]).is_absolute()
        or re.fullmatch(r"[0-9a-f]{64}", lease["hierarchy_lock_scope_sha256"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", lease["lease_sha256"]) is None
    ):
        raise QuPathExecutionError("Warpy mutation lease scope changed")
    scope = lease["hierarchy_lock_scope"]
    scope_fields = {"schema_version", "project_file", "fixed_project_entry_id"}
    registration = request.get("registration")
    if (
        type(scope) is not dict
        or set(scope) != scope_fields
        or any(type(scope.get(name)) is not str for name in scope_fields)
        or scope["schema_version"] != "qupath-hierarchy-mutation-scope/v1"
        or type(registration) is not dict
        or type(registration.get("fixed_project_entry_id")) is not str
    ):
        raise QuPathExecutionError("Warpy hierarchy scope is invalid")
    project_file = os.fspath(project.project_file.resolve())
    if (
        type(request.get("project_file")) is not str
        or request["project_file"] != project_file
        or scope["project_file"] != project_file
        or scope["fixed_project_entry_id"] != registration["fixed_project_entry_id"]
    ):
        raise QuPathExecutionError("Warpy hierarchy scope changed")
    expected_scope_sha256 = hashlib.sha256(
        "\0".join(
            (
                scope["schema_version"],
                scope["project_file"],
                scope["fixed_project_entry_id"],
            )
        ).encode("utf-8")
    ).hexdigest()
    if lease["hierarchy_lock_scope_sha256"] != expected_scope_sha256:
        raise QuPathExecutionError("Warpy hierarchy scope digest changed")
    for identity_name in ("attempt_store_identity", "hierarchy_lock_identity"):
        identity = lease[identity_name]
        if type(identity) is not dict or any(type(key) is not str for key in identity):
            raise QuPathExecutionError("Warpy mutation file identity is invalid")
        if identity.get("schema_version") == "posix-file-identity/v1":
            if (
                set(identity) != {"schema_version", "device", "inode"}
                or type(identity.get("device")) is not int
                or type(identity.get("inode")) is not int
                or identity["device"] < 0
                or identity["inode"] < 0
            ):
                raise QuPathExecutionError("Warpy mutation file identity is invalid")
        elif identity.get("schema_version") == "windows-file-identity/v1":
            if (
                set(identity) != {"schema_version", "volume_serial_number", "file_id"}
                or type(identity.get("volume_serial_number")) is not str
                or type(identity.get("file_id")) is not str
                or re.fullmatch(r"[0-9a-f]{16}", identity["volume_serial_number"]) is None
                or re.fullmatch(r"[0-9a-f]{32}", identity["file_id"]) is None
            ):
                raise QuPathExecutionError("Warpy mutation file identity is invalid")
        else:
            raise QuPathExecutionError("Warpy mutation file identity is invalid")
    if _warpy_mutation_lease_sha256(lease) != lease["lease_sha256"]:
        raise QuPathExecutionError("Warpy mutation lease digest changed")
    return {
        "request_sha256": _canonical_json_sha256(request),
        "operation": operation,
        "project_identity_sha256": _warpy_project_identity_sha256(project_file),
        "mutation_run_sha256": lease["lease_sha256"],
        "hierarchy_scope_sha256": lease["hierarchy_lock_scope_sha256"],
    }


class _PosixSpawnOperations:
    """Small POSIX launch seam built only on spawn file actions and procfs."""

    def _read_proc_text(self, path: Path, *, max_bytes: int, label: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ROIAnalysisError(f"{label} cannot be opened") from exc
        try:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                try:
                    chunk = os.read(descriptor, min(remaining, 16 * 1024))
                except OSError as exc:
                    raise ROIAnalysisError(f"{label} cannot be read") from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(payload) > max_bytes:
            raise ROIAnalysisError(f"{label} exceeds its bounded size")
        try:
            return payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ROIAnalysisError(f"{label} is not strict UTF-8") from exc

    def _process_start_ticks(self, process_id: int) -> int:
        if type(process_id) is not int or process_id <= 0:
            raise ROIAnalysisError("POSIX process identity is invalid")
        try:
            payload = self._read_proc_text(
                Path(f"/proc/{process_id}/stat"),
                max_bytes=16 * 1024,
                label="POSIX process identity",
            )
            closing = payload.rfind(")")
            if closing < 0:
                raise ValueError("missing comm terminator")
            fields = payload[closing + 2 :].split()
            state = fields[0]
            start_ticks = int(fields[19])
        except (ROIAnalysisError, ValueError, IndexError) as exc:
            raise ROIAnalysisError("POSIX process identity cannot be observed") from exc
        if start_ticks <= 0 or len(state) != 1:
            raise ROIAnalysisError("POSIX process identity is invalid")
        return start_ticks

    def _process_start_epoch_millis(self, start_ticks: int) -> int:
        try:
            boot_line = next(
                line
                for line in self._read_proc_text(
                    Path("/proc/stat"),
                    max_bytes=1024 * 1024,
                    label="POSIX boot identity",
                ).splitlines()
                if line.startswith("btime ")
            )
            boot_epoch_seconds = int(boot_line.split()[1])
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        except (OSError, StopIteration, ValueError, IndexError) as exc:
            raise ROIAnalysisError("POSIX process start epoch cannot be observed") from exc
        if boot_epoch_seconds < 0 or ticks_per_second <= 0:
            raise ROIAnalysisError("POSIX process start epoch is invalid")
        return boot_epoch_seconds * 1000 + start_ticks * 1000 // ticks_per_second

    def parent_identity(self) -> tuple[int, str]:
        process_id = os.getpid()
        return process_id, str(self._process_start_ticks(process_id))

    def socket_pair(self) -> tuple[socket.socket, socket.socket]:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)

    def spawn_gate(
        self,
        gate_argv: tuple[str, ...],
        environment: dict[str, str],
        *,
        control_socket: socket.socket,
        stdout_log: object,
        stderr_log: object,
        lock_descriptors: tuple[int, ...],
        process_attempt_observer: ProcessAttemptObserver | None,
    ) -> int:
        import fcntl

        if (
            len(lock_descriptors) != 2
            or len(set(lock_descriptors)) != 2
            or any(type(fd) is not int or fd < 0 for fd in lock_descriptors)
        ):
            raise QuPathExecutionError("Linux containment requires two distinct authority locks")
        sources = (
            control_socket.fileno(),
            stdout_log.fileno(),  # type: ignore[attr-defined]
            stderr_log.fileno(),  # type: ignore[attr-defined]
            *lock_descriptors,
        )
        duplicated: list[int] = []
        primary: BaseException | None = None
        try:
            child_targets = (
                CONTROL_SOCKET_FD,
                1,
                2,
                *_GATE_AUTHORITY_LOCK_FDS,
            )
            staging_minimum = max(child_targets) + 1
            for source in sources:
                duplicated.append(fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, staging_minimum))
            spawn_result = _posix_spawn_closefrom(
                _LINUX_EXEC_GATE,
                gate_argv,
                environment,
                remaps=tuple(zip(duplicated, child_targets, strict=True)),
                closefrom_start=staging_minimum,
                setsid=False,
                process_attempt_observer=process_attempt_observer,
            )
            for failure in spawn_result.cleanup_failures:
                try:
                    warnings.warn(
                        "native POSIX spawn cleanup failed after process creation: "
                        f"{type(failure).__name__}",
                        ResourceWarning,
                        stacklevel=2,
                    )
                except BaseException:
                    pass
            return spawn_result.process_id
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_failures: list[BaseException] = []
            for descriptor in reversed(duplicated):
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                for failure in cleanup_failures:
                    _add_bounded_secondary_note(
                        primary,
                        failure,
                        context="POSIX staged descriptor cleanup also failed",
                    )
            elif cleanup_failures:
                try:
                    warnings.warn(
                        f"{len(cleanup_failures)} POSIX staged descriptor closes "
                        "failed after process creation",
                        ResourceWarning,
                        stacklevel=2,
                    )
                except BaseException:
                    pass

    def receive_gate_ready(self, control_socket: socket.socket) -> tuple[GateReadyV1, int]:
        descriptors = array.array("i")
        received_fds: list[int] = []
        try:
            payload, ancillary, flags, _address = control_socket.recvmsg(
                _MAXIMUM_GATE_PACKET_BYTES + 1,
                socket.CMSG_SPACE(2 * descriptors.itemsize),
            )
            if (
                not payload
                or len(payload) > _MAXIMUM_GATE_PACKET_BYTES
                or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            ):
                raise QuPathExecutionError("gate readiness packet is invalid")
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    raise QuPathExecutionError("gate readiness ancillary data is invalid")
                usable = len(data) - (len(data) % descriptors.itemsize)
                descriptors.frombytes(data[:usable])
                received_fds.extend(descriptors.tolist())
                descriptors = array.array("i")
            if len(received_fds) != 1:
                raise QuPathExecutionError(
                    "gate readiness must transfer exactly one target descriptor"
                )
            mapping = loads_canonical_packet(payload)
            if dumps_canonical(mapping) != payload:
                raise QuPathExecutionError("gate readiness is not canonical")
            ready = GateReadyV1.from_mapping(mapping)
            target_fd = received_fds.pop()
            os.set_inheritable(target_fd, False)
            return ready, target_fd
        except (TypeError, ValueError) as exc:
            raise QuPathExecutionError("gate readiness is incompatible") from exc
        finally:
            for descriptor in received_fds:
                os.close(descriptor)

    def open_pidfd(self, process_id: int) -> int:
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            raise QuPathExecutionError("Linux pidfd support is unavailable")
        return opener(process_id, 0)

    def wait_stopped(
        self,
        process_id: int,
        *,
        gate_start_token: str,
        timeout_seconds: float = 5.0,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        while True:
            waited, status_value = os.waitpid(process_id, os.WUNTRACED | os.WNOHANG)
            if waited == process_id:
                if not os.WIFSTOPPED(status_value):
                    raise QuPathExecutionError("Linux execution gate exited before attachment")
                try:
                    observed_ticks = self._process_start_ticks(process_id)
                    if str(observed_ticks) != gate_start_token:
                        raise QuPathExecutionError("Linux execution gate start identity changed")
                    return self._process_start_epoch_millis(observed_ticks)
                except ROIAnalysisError as exc:
                    raise QuPathExecutionError(
                        "Linux execution gate identity cannot be observed"
                    ) from exc
            if time.monotonic() >= deadline:
                raise QuPathExecutionError("Linux execution gate did not stop before attachment")
            time.sleep(0.005)

    def wait_exit(self, process_id: int, *, timeout_seconds: float | None) -> int:
        if timeout_seconds is None:
            waited, status_value = os.waitpid(process_id, 0)
            if waited != process_id:
                raise QuPathExecutionError("contained process wait was invalid")
            return os.waitstatus_to_exitcode(status_value)
        deadline = time.monotonic() + timeout_seconds
        while True:
            waited, status_value = os.waitpid(process_id, os.WNOHANG)
            if waited == process_id:
                return os.waitstatus_to_exitcode(status_value)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(("contained-process",), timeout_seconds)
            time.sleep(0.01)

    def terminate_unattached_gate(
        self,
        process_id: int,
        *,
        pidfd: int | None,
    ) -> None:
        """Kill and reap only the exact unreleased child gate."""

        if pidfd is not None:
            sender = getattr(signal, "pidfd_send_signal", None)
            if callable(sender):
                try:
                    sender(pidfd, signal.SIGKILL, None, 0)
                except ProcessLookupError:
                    pass
            else:
                waited, _status = os.waitpid(process_id, os.WNOHANG)
                if waited == process_id:
                    return
                if waited != 0:
                    raise QuPathExecutionError("unattached Linux gate wait identity is invalid")
                os.kill(process_id, signal.SIGKILL)
        else:
            waited, _status = os.waitpid(process_id, os.WNOHANG)
            if waited == process_id:
                return
            if waited != 0:
                raise QuPathExecutionError("unattached Linux gate wait identity is invalid")
            os.kill(process_id, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while True:
            waited, _status = os.waitpid(process_id, os.WNOHANG)
            if waited == process_id:
                return
            if waited != 0:
                raise QuPathExecutionError("unattached Linux gate wait identity is invalid")
            if time.monotonic() >= deadline:
                raise QuPathExecutionError("unattached Linux gate could not be reaped")
            time.sleep(0.005)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)


class _DefaultProcessExecutor:
    """Production subprocess launcher, including parent-death containment."""

    def __init__(
        self,
        *,
        containment_expectation: ContainmentCompatibilityExpectationV1 | None = None,
        broker_client: ContainmentBrokerClient | None = None,
        posix_operations: _PosixSpawnOperations | None = None,
        linux_containment_profile: LinuxContainmentProfile = "hardened-broker",
        local_process_group_executor: object | None = None,
    ) -> None:
        if linux_containment_profile not in (
            "hardened-broker",
            "prototype-local-process-group",
        ):
            raise ValueError("Linux containment profile is unsupported")
        self._containment_expectation = containment_expectation
        self._broker_client = broker_client
        self._posix_operations = posix_operations or _PosixSpawnOperations()
        self._linux_containment_profile = linux_containment_profile
        self._local_process_group_executor = local_process_group_executor
        self._interactive_owner_lock = RLock()
        self._interactive_owner: object | None = None

    def _interactive_native_owner(self) -> object:
        with self._interactive_owner_lock:
            if self._interactive_owner is not None:
                return self._interactive_owner
            if os.name == "nt":
                from mif_pipeline.pipeline_core.win32_process_containment import (
                    Win32CtypesProcessPrimitives,
                    Win32JobProcessExecutor,
                )

                owner: object = Win32JobProcessExecutor(Win32CtypesProcessPrimitives())
            elif os.name == "posix" and sys.platform == "linux":
                if self._linux_containment_profile != "prototype-local-process-group":
                    raise QuPathExecutionError(
                        "interactive QuPath requires the trusted-local Linux profile"
                    )
                if self._local_process_group_executor is not None:
                    owner = self._local_process_group_executor
                else:
                    from mif_pipeline.pipeline_core.local_process_group_containment import (
                        LinuxLocalProcessGroupExecutor,
                    )

                    owner = LinuxLocalProcessGroupExecutor()
            else:
                raise QuPathExecutionError(
                    "interactive QuPath containment is unsupported on this platform"
                )
            if not callable(getattr(owner, "run_interactive", None)) or not callable(
                getattr(owner, "require_interactive_stopped", None)
            ):
                raise QuPathExecutionError("interactive QuPath owner is invalid")
            self._interactive_owner = owner
            return owner

    def require_interactive_stopped(self) -> None:
        self._interactive_native_owner().require_interactive_stopped()

    def run_interactive(
        self,
        command: tuple[str, ...],
        *,
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
    ) -> ContainedProcessResult:
        return self._interactive_native_owner().run_interactive(
            command,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            environment=environment,
        )

    def __call__(self, command: tuple[str, ...], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False, env=environment)

    def observe_linux_process_start_ticks(self, process_id: int) -> int:
        """Read Linux's native non-epoch process-birth identity."""

        if os.name != "posix" or sys.platform != "linux":
            raise QuPathExecutionError("Linux process start ticks are unavailable on this platform")
        try:
            return self._posix_operations._process_start_ticks(process_id)
        except ROIAnalysisError as exc:
            raise QuPathExecutionError("Linux process start ticks cannot be observed") from exc

    def run_with_streams(
        self,
        command: tuple[str, ...],
        *,
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        """Launch a read-only script with its admitted resources and log streams."""

        return subprocess.run(
            command,
            stdout=stdout_log,
            stderr=stderr_log,
            check=False,
            env=environment,
            timeout=_authoritative_timeout_seconds(),
        )

    def preflight_containment(
        self,
        command: tuple[str, ...],
        *,
        launch_request: ContainmentLaunchRequest,
        environment: dict[str, str] | None = None,
    ) -> None:
        if (
            type(command) is not tuple
            or not command
            or any(type(item) is not str or not item for item in command)
            or type(launch_request) is not ContainmentLaunchRequest
            or environment is None
            or type(environment) is not dict
            or any(
                type(name) is not str or type(value) is not str
                for name, value in environment.items()
            )
        ):
            raise QuPathExecutionError("project containment preflight inputs are invalid")
        if os.name == "nt":
            if launch_request.provider != "windows-kill-on-close-job/v1":
                raise QuPathExecutionError("Windows project containment request is invalid")
            return
        if os.name != "posix" or sys.platform != "linux":
            raise QuPathExecutionError(
                "authoritative QuPath containment is unsupported on this platform"
            )
        if self._linux_containment_profile == "prototype-local-process-group":
            if launch_request.provider != "linux-local-process-group/v1":
                raise QuPathExecutionError(
                    "Linux containment launch request differs from local profile"
                )
            return
        expectation = self._containment_expectation
        if type(expectation) is not ContainmentCompatibilityExpectationV1:
            raise QuPathExecutionError("Linux containment compatibility expectation is required")
        if (
            launch_request.provider != "linux-systemd-scope-broker/v1"
            or command[0] != expectation.canonical_executable_path
            or launch_request.registration_id != expectation.registration_id
            or launch_request.command_profile != expectation.command_profile
            or frozenset(environment) != expectation.requested_environment_names
        ):
            if frozenset(environment) != expectation.requested_environment_names:
                raise QuPathExecutionError(
                    "Linux containment environment names differ from expectation"
                )
            raise QuPathExecutionError("Linux containment launch request differs from expectation")
        if self._broker_client is None:
            self._broker_client = ContainmentBrokerClient()
        try:
            self._broker_client.preflight(expectation)
        except Exception as exc:
            raise QuPathExecutionError("Linux containment broker preflight failed") from exc

    def _validate_gate_ready(
        self,
        ready: GateReadyV1,
        *,
        process_id: int,
        parent_process_id: int,
        parent_start_token: str,
        command: tuple[str, ...],
        environment: dict[str, str],
        launch_request: ContainmentLaunchRequest,
    ) -> None:
        """Validate gate ready for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        expectation = self._containment_expectation
        assert type(expectation) is ContainmentCompatibilityExpectationV1
        facts = []
        environment_bytes = 0
        for name in sorted(environment):
            encoded_name = name.encode("utf-8")
            encoded_value = environment[name].encode("utf-8")
            environment_bytes += len(encoded_name) + len(encoded_value) + 2
            facts.append(
                {
                    "name": name,
                    "size": len(encoded_value),
                    "value_sha256": hashlib.sha256(encoded_value).hexdigest(),
                }
            )
        cwd_identity = Path.cwd().stat()
        expected = {
            "launch_nonce_sha256": launch_request.authority.launch_nonce_sha256,
            "gate_pid": process_id,
            "expected_parent_pid": parent_process_id,
            "expected_parent_start_token": parent_start_token,
            "target_executable_sha256": expectation.target_executable_sha256,
            "argv_count": len(command),
            "argv_sha256": hashlib.sha256(dumps_canonical(list(command))).hexdigest(),
            "environment_count": len(environment),
            "environment_bytes": environment_bytes,
            "environment_names_sizes_sha256": hashlib.sha256(dumps_canonical(facts)).hexdigest(),
            "cwd_device": cwd_identity.st_dev,
            "cwd_inode": cwd_identity.st_ino,
        }
        if any(getattr(ready, name) != value for name, value in expected.items()):
            raise QuPathExecutionError(
                "Linux execution gate readiness differs from launch authority"
            )

    def run_contained(
        self,
        command: tuple[str, ...],
        *,
        lock_descriptors: tuple[int, ...],
        launch_request: ContainmentLaunchRequest,
        timeout_seconds: float | None,
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str] | None = None,
        process_start_observer: Callable[..., str],
        process_attempt_observer: ProcessAttemptObserver | None = None,
        process_drain_observer: ProcessDrainObserver | None = None,
        process_terminal_observer: Callable[[int | None, object], None] | None = None,
        cancellation_event: object | None = None,
    ) -> ContainedProcessResult:
        if (
            not callable(process_start_observer)
            or (process_attempt_observer is not None and not callable(process_attempt_observer))
            or (process_drain_observer is not None and not callable(process_drain_observer))
            or (process_terminal_observer is not None and not callable(process_terminal_observer))
            or (
                cancellation_event is not None
                and not callable(getattr(cancellation_event, "is_set", None))
            )
        ):
            raise QuPathExecutionError("process containment observers are invalid")
        if os.name == "nt":
            self.preflight_containment(
                command,
                launch_request=launch_request,
                environment=environment,
            )
            from mif_pipeline.pipeline_core.win32_process_containment import (
                run_in_kill_on_close_job,
            )

            return run_in_kill_on_close_job(
                command,
                launch_request=launch_request,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                timeout_seconds=timeout_seconds,
                environment=environment,
                process_start_observer=process_start_observer,
                process_attempt_observer=process_attempt_observer,
                process_drain_observer=process_drain_observer,
                process_terminal_observer=process_terminal_observer,
                cancellation_event=cancellation_event,
            )
        self.preflight_containment(
            command,
            launch_request=launch_request,
            environment=environment,
        )
        assert environment is not None
        if len(lock_descriptors) != 2 or tuple(dict.fromkeys(lock_descriptors)) != lock_descriptors:
            raise QuPathExecutionError(
                "authoritative QuPath execution requires exactly operation and "
                "hierarchy lock descriptors"
            )
        if self._linux_containment_profile == "prototype-local-process-group":
            local_executor = self._local_process_group_executor
            if local_executor is None:
                from mif_pipeline.pipeline_core.local_process_group_containment import (
                    LinuxLocalProcessGroupExecutor,
                )

                local_executor = LinuxLocalProcessGroupExecutor()
                self._local_process_group_executor = local_executor
            run_local = getattr(local_executor, "run_contained", None)
            if not callable(run_local):
                raise QuPathExecutionError("Linux local process-group executor is invalid")
            return run_local(
                command,
                lock_descriptors=lock_descriptors,
                launch_request=launch_request,
                timeout_seconds=timeout_seconds,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                environment=environment,
                process_start_observer=process_start_observer,
                process_attempt_observer=process_attempt_observer,
                process_drain_observer=process_drain_observer,
                process_terminal_observer=process_terminal_observer,
                cancellation_event=cancellation_event,
            )
        operations = self._posix_operations
        parent_socket: socket.socket | None = None
        child_socket: socket.socket | None = None
        target_fd: int | None = None
        pidfd: int | None = None
        process_id: int | None = None
        start_epoch_millis: int | None = None
        attachment: ProcessContainmentLaunchEvidence | None = None
        drain: ProcessContainmentDrainEvidence | None = None
        release = None
        returncode: int | None = None
        log_facts: tuple[str, str, bool, bool] | None = None
        log_finalization_attempted = False
        result: ContainedProcessResult | None = None
        primary: BaseException | None = None
        publication_complete = False
        cancellation_before_release = False
        attach_attempted = False
        pidfd_open_attempted = False
        drain_observation_attempted = False
        drain_observation_complete = process_drain_observer is None
        terminal_observation_attempted = False
        terminal_observation_complete = process_terminal_observer is None

        def wait_for_exit() -> int:
            if cancellation_event is None:
                return operations.wait_exit(process_id, timeout_seconds=timeout_seconds)
            deadline = (
                None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
            )
            while True:
                if cancellation_event.is_set():  # type: ignore[attr-defined]
                    try:
                        return operations.wait_exit(process_id, timeout_seconds=0.0)
                    except (subprocess.TimeoutExpired, TimeoutError):
                        raise _CancellationRequested("contained process cancellation requested")
                wait_seconds = _CANCELLATION_POLL_SECONDS
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout_seconds)
                    wait_seconds = min(wait_seconds, remaining)
                try:
                    return operations.wait_exit(process_id, timeout_seconds=wait_seconds)
                except (subprocess.TimeoutExpired, TimeoutError):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise

        def publish_drain(evidence: ProcessContainmentDrainEvidence) -> None:
            nonlocal drain_observation_attempted, drain_observation_complete
            if drain_observation_attempted:
                raise QuPathExecutionError("process drain observer repeated")
            drain_observation_attempted = True
            if process_drain_observer is not None:
                process_drain_observer(evidence)
            drain_observation_complete = True

        def publish_terminal(status: int | None, evidence: ProcessContainmentDrainEvidence) -> None:
            nonlocal terminal_observation_attempted, terminal_observation_complete
            if terminal_observation_attempted:
                raise QuPathExecutionError("process terminal observer repeated")
            terminal_observation_attempted = True
            if process_terminal_observer is not None:
                process_terminal_observer(status, evidence)
            terminal_observation_complete = True

        try:
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("contained process cancellation requested")
            parent_process_id, parent_start_token = operations.parent_identity()
            gate_argv = (
                _LINUX_EXEC_GATE,
                "--expected-parent-pid",
                str(parent_process_id),
                "--expected-parent-start-token",
                parent_start_token,
                "--launch-nonce-sha256",
                launch_request.authority.launch_nonce_sha256,
                *(
                    item
                    for descriptor in _GATE_AUTHORITY_LOCK_FDS
                    for item in ("--preserve-fd", str(descriptor))
                ),
                "--",
                *command,
            )
            parent_socket, child_socket = operations.socket_pair()
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("contained process cancellation requested")

            def publish_attempt_at_spawn_boundary() -> None:
                nonlocal cancellation_before_release
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()  # type: ignore[attr-defined]
                ):
                    cancellation_before_release = True
                    raise _CancellationRequested("contained process cancellation requested")
                if process_attempt_observer is not None:
                    process_attempt_observer()

            process_id = operations.spawn_gate(
                gate_argv,
                environment,
                control_socket=child_socket,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                lock_descriptors=lock_descriptors,
                process_attempt_observer=publish_attempt_at_spawn_boundary,
            )
            closing_child = child_socket
            child_socket = None
            closing_child.close()
            pidfd_open_attempted = True
            pidfd = operations.open_pidfd(process_id)
            ready, target_fd = operations.receive_gate_ready(parent_socket)
            closing_parent = parent_socket
            parent_socket = None
            closing_parent.close()
            self._validate_gate_ready(
                ready,
                process_id=process_id,
                parent_process_id=parent_process_id,
                parent_start_token=parent_start_token,
                command=command,
                environment=environment,
                launch_request=launch_request,
            )
            start_epoch_millis = operations.wait_stopped(
                process_id,
                gate_start_token=ready.gate_start_token,
            )
            assert self._broker_client is not None
            attach_attempted = True
            attachment = self._broker_client.attach(
                AttachPayloadV1(
                    authority=launch_request.authority,
                    gate_ready=ready,
                ),
                target_executable_fd=target_fd,
            )
            closing_target_fd = target_fd
            target_fd = None
            operations.close_fd(closing_target_fd)
            process_launch_sha256 = process_start_observer(
                process_id,
                start_epoch_millis,
                attachment,
            )
            if (
                type(process_launch_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", process_launch_sha256) is None
            ):
                raise QuPathExecutionError("process launch observer returned an invalid digest")
            publication_complete = True
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("contained process cancellation requested")
            release = self._broker_client.release(
                launch_request.authority,
                attachment_receipt_sha256=attachment.attachment_receipt_sha256,
                application_process_launch_sha256=process_launch_sha256,
            )
            returncode = wait_for_exit()
            drain = self._broker_client.await_drain(
                launch_request.authority,
                attachment_receipt_sha256=(attachment.attachment_receipt_sha256),
                release_receipt_sha256=release.release_receipt_sha256,
                termination_reason=("normal_exit" if returncode == 0 else "nonzero_exit"),
                timeout_seconds=(30.0 if timeout_seconds is None else timeout_seconds),
            )
            publish_drain(drain)
            publish_terminal(returncode, drain)
            log_finalization_attempted = True
            try:
                log_facts = _finalize_contained_logs(stdout_log, stderr_log)
            except BaseException as log_exc:
                if returncode != 0:
                    nonzero = subprocess.CalledProcessError(returncode, command)
                    nonzero.add_note(f"post-drain log finalization also failed: {log_exc!r}"[:512])
                    raise nonzero from log_exc
                raise
            result = ContainedProcessResult(
                args=command,
                returncode=returncode,
                launch_evidence=attachment,
                drain_evidence=drain,
                stdout_excerpt=log_facts[0],
                stderr_excerpt=log_facts[1],
                stdout_truncated=log_facts[2],
                stderr_truncated=log_facts[3],
            )
        except BaseException as exc:
            primary = exc
            if (
                process_id is not None
                and attachment is None
                and (not attach_attempted or isinstance(exc, ContainmentRejectedError))
            ):
                cleanup_pidfd = pidfd
                if cleanup_pidfd is None and not pidfd_open_attempted:
                    pidfd_open_attempted = True
                    try:
                        pidfd = operations.open_pidfd(process_id)
                        cleanup_pidfd = pidfd
                    except BaseException as cleanup_exc:
                        primary.add_note(
                            "unattached Linux gate pidfd acquisition also failed: "
                            f"{cleanup_exc!r}"[:512]
                        )
                try:
                    operations.terminate_unattached_gate(
                        process_id,
                        pidfd=cleanup_pidfd,
                    )
                except BaseException as cleanup_exc:
                    primary.add_note(
                        "unattached Linux gate cleanup also failed: " f"{cleanup_exc!r}"[:512]
                    )
            if attachment is not None and drain is None:
                stop_reason: str | None = None
                stop_release_sha256: str | None = None
                if not publication_complete or cancellation_before_release:
                    stop_reason = "launch_publication_failed"
                elif isinstance(exc, _CancellationRequested):
                    stop_reason = "cancelled"
                    stop_release_sha256 = release.release_receipt_sha256
                elif release is not None and isinstance(
                    exc, (subprocess.TimeoutExpired, TimeoutError)
                ):
                    stop_reason = "timeout"
                    stop_release_sha256 = release.release_receipt_sha256
                elif release is not None and isinstance(exc, KeyboardInterrupt):
                    stop_reason = "cancelled"
                    stop_release_sha256 = release.release_receipt_sha256
                if stop_reason is not None:
                    try:
                        assert self._broker_client is not None
                        drain = self._broker_client.stop(
                            launch_request.authority,
                            attachment_receipt_sha256=(attachment.attachment_receipt_sha256),
                            release_receipt_sha256=stop_release_sha256,
                            termination_reason=stop_reason,
                            timeout_seconds=(30.0 if timeout_seconds is None else timeout_seconds),
                        )
                    except BaseException as cleanup_exc:
                        primary.add_note(
                            "containment stop/drain also failed: " f"{cleanup_exc!r}"[:512]
                        )
            if drain is not None and not drain_observation_attempted:
                try:
                    publish_drain(drain)
                except BaseException as observation_exc:
                    primary.add_note(
                        "process drain publication also failed: " f"{observation_exc!r}"[:512]
                    )
            if (
                drain is not None
                and drain_observation_complete
                and not terminal_observation_attempted
            ):
                try:
                    publish_terminal(returncode, drain)
                except BaseException as observation_exc:
                    primary.add_note(
                        "process terminal publication also failed: " f"{observation_exc!r}"[:512]
                    )
            if (
                drain is not None
                and drain_observation_complete
                and terminal_observation_complete
                and log_facts is None
                and not log_finalization_attempted
            ):
                try:
                    log_finalization_attempted = True
                    log_facts = _finalize_contained_logs(stdout_log, stderr_log)
                except BaseException as log_exc:
                    primary.add_note(
                        "post-drain log finalization also failed: " f"{log_exc!r}"[:512]
                    )
        finally:
            cleanup_errors: list[BaseException] = []
            if target_fd is not None:
                closing_target_fd = target_fd
                target_fd = None
                try:
                    operations.close_fd(closing_target_fd)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if child_socket is not None:
                closing_child = child_socket
                child_socket = None
                try:
                    closing_child.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if parent_socket is not None:
                closing_parent = parent_socket
                parent_socket = None
                try:
                    closing_parent.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if pidfd is not None:
                closing_pidfd = pidfd
                pidfd = None
                try:
                    operations.close_fd(closing_pidfd)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if primary is None:
                    primary = cleanup_errors.pop(0)
                for cleanup_error in cleanup_errors:
                    primary.add_note(
                        "additional containment cleanup failure: " f"{cleanup_error!r}"[:512]
                    )
        if primary is not None:
            failure = _ContainedProcessFailure(
                process_id=process_id if start_epoch_millis is not None else None,
                start_epoch_millis=start_epoch_millis,
                launch_evidence=attachment,
                drain_evidence=drain,
                returncode=returncode,
                stdout_excerpt=None if log_facts is None else log_facts[0],
                stderr_excerpt=None if log_facts is None else log_facts[1],
                stdout_truncated=None if log_facts is None else log_facts[2],
                stderr_truncated=None if log_facts is None else log_facts[3],
            )
            raise _ContainedProcessError(
                "Linux contained process did not complete normally",
                failure,
            ) from primary
        assert result is not None
        return result


@dataclass(frozen=True, slots=True)
class _QuPathExecutionEvidence:
    process_start_token: object | None = None
    launch_evidence: ProcessContainmentLaunchEvidence | None = None
    drain_evidence: ProcessContainmentDrainEvidence | None = None
    process_drain_sha256: str | None = None


class QuPathExecutionError(ROIAnalysisError):
    """Raised when QuPath cannot execute a requested analysis operation."""

    __slots__ = ("record", "_containment_evidence")

    def __init__(
        self,
        message: str,
        record: "QuPathRunRecord | None" = None,
        *,
        process_drain_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.record = record
        self._containment_evidence = _QuPathExecutionEvidence(
            process_drain_sha256=process_drain_sha256
        )

    @classmethod
    def _with_containment_evidence(
        cls,
        message: str,
        record: "QuPathRunRecord | None" = None,
        *,
        process_start_token: object | None = None,
        launch_evidence: ProcessContainmentLaunchEvidence | None = None,
        drain_evidence: ProcessContainmentDrainEvidence | None = None,
        process_drain_sha256: str | None = None,
    ) -> "QuPathExecutionError":
        error = cls(message, record, process_drain_sha256=process_drain_sha256)
        error._containment_evidence = _QuPathExecutionEvidence(
            process_start_token=process_start_token,
            launch_evidence=launch_evidence,
            drain_evidence=drain_evidence,
            process_drain_sha256=process_drain_sha256,
        )
        return error

    @property
    def process_start_token(self) -> object | None:
        return self._containment_evidence.process_start_token

    @property
    def launch_evidence(self) -> ProcessContainmentLaunchEvidence | None:
        return self._containment_evidence.launch_evidence

    @property
    def drain_evidence(self) -> ProcessContainmentDrainEvidence | None:
        return self._containment_evidence.drain_evidence

    @property
    def process_drain_sha256(self) -> str | None:
        return self._containment_evidence.process_drain_sha256


def _bind_process_drain_error(
    error: Exception,
    *,
    process_drain_sha256: str,
    record: "QuPathRunRecord | None" = None,
    process_start_token: object | None = None,
    launch_evidence: ProcessContainmentLaunchEvidence | None = None,
    drain_evidence: ProcessContainmentDrainEvidence | None = None,
) -> QuPathExecutionError:
    """Preserve the durable drain authority across every later failure."""

    if isinstance(error, QuPathExecutionError):
        message = str(error)
        if error.record is not None:
            record = error.record
        process_start_token = error.process_start_token or process_start_token
        launch_evidence = error.launch_evidence or launch_evidence
        drain_evidence = error.drain_evidence or drain_evidence
    else:
        message = "QuPath result validation failed after process drain"
    return QuPathExecutionError._with_containment_evidence(
        message,
        record,
        process_start_token=process_start_token,
        launch_evidence=launch_evidence,
        drain_evidence=drain_evidence,
        process_drain_sha256=process_drain_sha256,
    )


@dataclass(frozen=True, slots=True)
class QuPathRunRecord:
    """Audit record for one external QuPath script invocation."""

    command: tuple[str, ...]
    executable_version: str
    exit_status: int
    stdout_log: Path
    stderr_log: Path
    runtime_seconds: float
    stage_result: StageResult | None = None


def format_qupath_failure_record(record: QuPathRunRecord) -> str:
    """Read bounded saved-log tails once when a direct desktop operation fails."""
    excerpts, truncated, read_errors = [], [], []
    paths = (record.stdout_log, record.stderr_log)
    for path in paths:
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - _MAXIMUM_LOG_EXCERPT_BYTES))
                excerpts.append(stream.read(_MAXIMUM_LOG_EXCERPT_BYTES).decode("utf-8", errors="replace"))
                truncated.append(size > _MAXIMUM_LOG_EXCERPT_BYTES)
        except OSError as error:
            excerpts.append(None)
            truncated.append(False)
            read_errors.append(str(error))
    detail = format_qupath_process_diagnostics(
        termination_reason="normal_exit" if record.exit_status == 0 else "nonzero_exit",
        exit_status=record.exit_status,
        log_paths=paths,
        excerpts=tuple(excerpts),
        truncated=tuple(truncated),
    )
    if read_errors:
        detail += "\n\nSome saved logs could not be read: " + "; ".join(read_errors)
    return detail


@dataclass(frozen=True, slots=True)
class _ProjectStageQuPathRunRecord:
    """Private path-bearing record for a nullable project-stage status."""

    command: tuple[str, ...]
    executable_version: str
    exit_status: int | None
    stdout_log: Path
    stderr_log: Path
    runtime_seconds: float


@dataclass(slots=True)
class _ProcessDrainFailureBinding:
    process_drain_sha256: str | None = None
    record: QuPathRunRecord | None = None
    process_start_token: object | None = None
    launch_evidence: ProcessContainmentLaunchEvidence | None = None
    drain_evidence: ProcessContainmentDrainEvidence | None = None


@contextmanager
def _preserve_process_drain_failure(
    binding: _ProcessDrainFailureBinding,
):
    try:
        yield
    except Exception as exc:
        if binding.process_drain_sha256 is None:
            raise
        raise _bind_process_drain_error(
            exc,
            process_drain_sha256=binding.process_drain_sha256,
            record=binding.record,
            process_start_token=binding.process_start_token,
            launch_evidence=binding.launch_evidence,
            drain_evidence=binding.drain_evidence,
        ) from exc


@dataclass(frozen=True, slots=True)
class QuPathImagePreflight:
    """Audit record for an executable image-openability probe."""

    command: tuple[str, ...]
    executable_version: str
    exit_status: int
    runtime_seconds: float


@dataclass(frozen=True, slots=True)
class QuPathStageRun:
    """A subprocess audit record paired with validated file-based semantics."""

    record: QuPathRunRecord
    result: StageResult | None
    generation_manifest: object | None
    exchange: StageExchangePaths


@dataclass(frozen=True, slots=True)
class QuPathEvidenceRun:
    """Generic script record plus the immutable bytes it was asked to publish."""

    record: QuPathRunRecord
    evidence_path: Path
    evidence_sha256: str
    script_sha256: str
    request_sha256s: tuple[tuple[Path, str], ...]
    process_start_token: object | None = None
    launch_nonce_sha256: str | None = None
    launch_evidence: ProcessContainmentLaunchEvidence | None = None
    drain_evidence: ProcessContainmentDrainEvidence | None = None
    process_drain_sha256: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class QuPathMutationLaunchBinding:
    """One-use runner-issued raw nonce bound to one live project lease."""

    launch_nonce_sha256: str
    _raw_nonce: bytes
    _issuer: object
    _project_lease: object
    _consumed: list[bool]

    @classmethod
    def _issue(cls, issuer: object, project_lease: object) -> "QuPathMutationLaunchBinding":
        raw = os.urandom(32)
        value = object.__new__(cls)
        object.__setattr__(value, "launch_nonce_sha256", hashlib.sha256(raw).hexdigest())
        object.__setattr__(value, "_raw_nonce", raw)
        object.__setattr__(value, "_issuer", issuer)
        object.__setattr__(value, "_project_lease", project_lease)
        object.__setattr__(value, "_consumed", [False])
        return value


class _ProjectStageRuntimeIdentity:
    """Nominal lower-layer identity implemented only by the owning facade."""

    __slots__ = ()

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise QuPathExecutionError("project-stage runtime identities are facade-issued")


@dataclass(frozen=True, slots=True, init=False)
class _ProjectStageRunnerBinding:
    """One exact facade/runtime/runner composition identity."""

    _runner: object
    _runtime: object
    _composition_binding: object
    _project_operations: object
    _issuer: object

    @classmethod
    def _issue(
        cls,
        *,
        runner: object,
        runtime: object,
        composition_binding: object,
        project_operations: object,
        issuer: object,
    ) -> "_ProjectStageRunnerBinding":
        from .catalogue_operations import QuPathRunnerProjectOperations

        if (
            issuer is not runner
            or not isinstance(runner, QuPathRunner)
            or not isinstance(runtime, _ProjectStageRuntimeIdentity)
            or not isinstance(composition_binding, RuntimeCompositionBinding)
            or not isinstance(project_operations, QuPathRunnerProjectOperations)
        ):
            raise QuPathExecutionError("project-stage runner binding issuer is invalid")
        try:
            project_operations._bind_project_stage_runtime_identity(
                runtime,
                binding=composition_binding,
                runner=runner,
            )
        except Exception as exc:
            raise QuPathExecutionError("project-stage runtime composition is invalid") from exc
        value = object.__new__(cls)
        object.__setattr__(value, "_runner", runner)
        object.__setattr__(value, "_runtime", runtime)
        object.__setattr__(value, "_composition_binding", composition_binding)
        object.__setattr__(value, "_project_operations", project_operations)
        object.__setattr__(value, "_issuer", issuer)
        return value

    def authenticate(self) -> None:
        if (
            self._issuer is not self._runner
            or not isinstance(self._runner, QuPathRunner)
            or not isinstance(self._runtime, _ProjectStageRuntimeIdentity)
            or not isinstance(self._composition_binding, RuntimeCompositionBinding)
        ):
            raise QuPathExecutionError("project-stage runner binding is invalid")
        try:
            self._project_operations._authenticate_project_stage_runtime_identity(
                self._runtime,
                binding=self._composition_binding,
                runner=self._runner,
            )
        except Exception as exc:
            raise QuPathExecutionError("project-stage runner binding differs") from exc


@dataclass(frozen=True, slots=True, init=False)
class _ProjectStageProcessAuthority:
    """Private facade-issued native context for one exact prepared run."""

    _runner: object
    _runtime_binding: _ProjectStageRunnerBinding
    _project_lease: object
    _prepared_descriptor: object
    _authority_layout: RetainedAuthorityLayoutV2
    _workspace_lease: object
    _run_plan: object
    _run_plan_path: Path
    _run_plan_bytes: bytes
    _prepared_descriptor_path: Path
    _prepared_descriptor_bytes: bytes
    _process_launch_path: Path
    _bundle_manifest_path: Path
    _bundle_manifest_bytes: bytes
    _guard_script_path: Path
    _guard_script_bytes: bytes
    _target_script_path: Path
    _target_script_bytes: bytes
    _publish_record: Callable[[object], object]
    _reopen_record: Callable[[object, object], object]
    _publish_launch: Callable[[object], object]
    _reopen_launch: Callable[[object, object], object]
    _evidence_issuer: object
    _consumed: list[bool]

    @classmethod
    def _issue(cls, *, runner: object, runtime: object, **values: object):
        runtime_binding = values.pop("runtime_binding", None)
        if (
            runner is None
            or not isinstance(runtime_binding, _ProjectStageRunnerBinding)
            or runtime_binding._runner is not runner
            or runtime_binding._runtime is not runtime
            or runtime_binding._issuer is not runner
        ):
            raise QuPathExecutionError("project-stage process issuer is invalid")
        runtime_binding.authenticate()
        expected = {
            "project_lease",
            "prepared_descriptor",
            "authority_layout",
            "workspace_lease",
            "run_plan",
            "run_plan_path",
            "run_plan_bytes",
            "prepared_descriptor_path",
            "prepared_descriptor_bytes",
            "process_launch_path",
            "bundle_manifest_path",
            "bundle_manifest_bytes",
            "guard_script_path",
            "guard_script_bytes",
            "target_script_path",
            "target_script_bytes",
            "publish_record",
            "reopen_record",
            "publish_launch",
            "reopen_launch",
        }
        if set(values) != expected:
            raise QuPathExecutionError("project-stage process authority is incomplete")
        from .project_stage import _issue_project_stage_process_evidence_issuer

        value = object.__new__(cls)
        object.__setattr__(value, "_runner", runner)
        object.__setattr__(value, "_runtime_binding", runtime_binding)
        for name in sorted(expected):
            object.__setattr__(value, f"_{name}", values[name])
        object.__setattr__(
            value,
            "_evidence_issuer",
            _issue_project_stage_process_evidence_issuer(runtime_binding=runtime_binding),
        )
        object.__setattr__(value, "_consumed", [False])
        return value


def _freeze_dispatch_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_dispatch_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_dispatch_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_dispatch_value(item) for item in value)
    return value


def _dispatch_json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


class _NativeTrainingDispatchIdentity:
    pass


@dataclass(frozen=True, slots=True)
class _NativeTrainingDispatchRecord:
    authority_identity: int
    seal_sha256: str
    registry: object
    runner: object
    reservation: object
    readiness: object
    request_core_fields: Mapping[str, object]
    consumed: bool = False


def _dispatch_seal(
    *,
    identity: object,
    runner: object,
    reservation: object,
    readiness: object,
    request: Mapping[str, object],
) -> str:
    material = {
        "identity": id(identity),
        "runner": id(runner),
        "reservation": id(reservation),
        "readiness": id(readiness),
        "request": dict(request),
    }
    return hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), default=_dispatch_json_default
        ).encode()
    ).hexdigest()


class _NativeTrainingDispatchIssuer:
    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._records: WeakKeyDictionary[object, _NativeTrainingDispatchRecord] = (
            WeakKeyDictionary()
        )

    def issue(
        self,
        *,
        registry: object,
        runner: object,
        reservation: object,
        readiness: object,
        request: Mapping[str, object],
    ) -> "NativeTrainingDispatchAuthority":
        # Reject malformed or oversized scientific input before taking a project write lease.
        from ._training_codec import _decode_canonical

        _decode_canonical(canonical_json_bytes(_thaw_native_training_request_projection(request)),
                          "native-training dispatch")
        identity = _NativeTrainingDispatchIdentity()
        frozen = _freeze_dispatch_value(request)
        authority = object.__new__(NativeTrainingDispatchAuthority)
        object.__setattr__(authority, "_issuer_identity", identity)
        object.__setattr__(authority, "_request_core_fields", frozen)
        with self._lock:
            self._records[identity] = _NativeTrainingDispatchRecord(
                id(authority),
                _dispatch_seal(
                    identity=identity,
                    runner=runner,
                    reservation=reservation,
                    readiness=readiness,
                    request=request,
                ),
                registry,
                runner,
                reservation,
                readiness,
                frozen,
            )
        return authority

    def consume(self, authority: object, *, runner: object) -> _NativeTrainingDispatchRecord:
        if type(authority) is not NativeTrainingDispatchAuthority:
            raise QuPathExecutionError("native-training dispatch authority is foreign")
        identity = getattr(authority, "_issuer_identity", None)
        with self._lock:
            record = self._records.get(identity)
            if (
                record is None
                or record.authority_identity != id(authority)
                or record.consumed
                or record.runner is not runner
            ):
                raise QuPathExecutionError(
                    "native-training dispatch authority is foreign or consumed"
                )
            if record.seal_sha256 != _dispatch_seal(
                identity=identity,
                runner=record.runner,
                reservation=record.reservation,
                readiness=record.readiness,
                request=record.request_core_fields,
            ):
                raise QuPathExecutionError("native-training dispatch authority changed")
            self._records[identity] = replace(record, consumed=True)
            return record


_NATIVE_TRAINING_DISPATCH_ISSUER = _NativeTrainingDispatchIssuer()


def _issue_native_training_dispatch_authority(
    *,
    registry: object,
    runner: object,
    reservation: object,
    readiness: object,
    request: Mapping[str, object],
) -> "NativeTrainingDispatchAuthority":
    return _NATIVE_TRAINING_DISPATCH_ISSUER.issue(
        registry=registry,
        runner=runner,
        reservation=reservation,
        readiness=readiness,
        request=request,
    )


@dataclass(frozen=True, slots=True, init=False, repr=False, weakref_slot=True)
class NativeTrainingDispatchAuthority:
    """Opaque live dispatch envelope issued by the training registry."""

    _issuer_identity: object
    _request_core_fields: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object) -> "NativeTrainingDispatchAuthority":
        del cls, args, kwargs
        raise TypeError("native-training dispatch authorities are issuer-only")

    def __copy__(self) -> object:
        raise TypeError("native-training dispatch authority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("native-training dispatch authority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("native-training dispatch authority is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class _TrainingProcessAuthority:
    """One-use retained authority for one native-training child process."""

    _runner: "QuPathRunner"
    _project_lease: object
    _mutation_observation: object
    _launch: QuPathMutationLaunchBinding
    _raw_launch_nonce: bytes
    _request: QuPathNativeTrainingRequest
    _request_bytes: bytes
    _request_sha256: str
    _runtime_binding: RuntimeAuthorityBindingV2
    _authority_layout: RetainedAuthorityLayoutV2
    _workspace_storage_scope_sha256: str
    _active_mutation_run_sha256: str
    _script_bytes: bytes
    _script_ref: AuthorityMemberRefV2
    _request_ref: AuthorityMemberRefV2
    _result_ref: AuthorityMemberRefV2
    _stdout_ref: AuthorityMemberRefV2
    _stderr_ref: AuthorityMemberRefV2
    _script_path: Path
    _request_path: Path
    _result_path: Path
    _script_pin: _PinnedProjectStageFile
    _request_pin: _PinnedProjectStageFile
    _result_parent_pin: _PinnedProjectStageDirectory
    _consumed: list[bool]

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("training process authorities are runner-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("training process authorities cannot be subclassed")

    def __copy__(self) -> NoReturn:
        raise TypeError("training process authorities are nonserializable")

    def __deepcopy__(self, memo: object) -> NoReturn:
        del memo
        raise TypeError("training process authorities are nonserializable")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("training process authorities are nonserializable")


def _training_terminal_status_sha256(
    process_start_token: object,
    *,
    exit_status: int | None,
    termination_reason: str,
) -> str:
    exporter = getattr(process_start_token, "to_mapping", None)
    if (
        not callable(exporter)
        or (exit_status is not None and type(exit_status) is not int)
        or type(termination_reason) is not str
        or not termination_reason
    ):
        raise QuPathExecutionError("native-training terminal status is invalid")
    try:
        payload = canonical_json_bytes(
            {
                "process_start_token": exporter(),
                "returncode": exit_status,
                "termination_reason": termination_reason,
            }
        )
    except BaseException as exc:
        raise QuPathExecutionError("native-training terminal status is invalid") from exc
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class QuPathNativeTrainingProcessCompletion:
    """Validated process observation; never an artifact receipt."""

    disposition: _TrainingProcessDisposition
    request: QuPathNativeTrainingRequest
    result: QuPathNativeTrainingResult | None
    spawn_attempt: "ProjectMutationProcessSpawnAttemptRecord | None"
    process_launch: "ProjectMutationProcessLaunchRecord | None"
    process_drain: "ProjectMutationProcessDrainEvidence | None"
    exit_status: int | None
    termination_reason: str | None
    failure_kind: _TrainingProcessFailureKind | None
    runtime_seconds: float
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    stdout_truncated: bool | None
    stderr_truncated: bool | None
    _issuer_identity: "_NativeTrainingCompletionIdentity" = field(
        repr=False,
        compare=False,
    )

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("training process completions are runner-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("training process completions cannot be subclassed")

    @classmethod
    def _create(
        cls,
        *,
        disposition: _TrainingProcessDisposition,
        request: QuPathNativeTrainingRequest,
        result: QuPathNativeTrainingResult | None,
        spawn_attempt: "ProjectMutationProcessSpawnAttemptRecord | None",
        process_launch: "ProjectMutationProcessLaunchRecord | None",
        process_drain: "ProjectMutationProcessDrainEvidence | None",
        exit_status: int | None,
        termination_reason: str | None,
        failure_kind: _TrainingProcessFailureKind | None,
        runtime_seconds: float,
        stdout_excerpt: str | None,
        stderr_excerpt: str | None,
        stdout_truncated: bool | None,
        stderr_truncated: bool | None,
        _issuer_identity: "_NativeTrainingCompletionIdentity",
    ) -> "QuPathNativeTrainingProcessCompletion":
        from .catalogue import (
            ProjectMutationProcessDrainEvidence,
            ProjectMutationProcessLaunchRecord,
            ProjectMutationProcessSpawnAttemptRecord,
        )

        if (
            type(request) is not QuPathNativeTrainingRequest
            or type(_issuer_identity) is not _NativeTrainingCompletionIdentity
            or decode_qupath_native_training_request(request.to_bytes()) != request
            or type(runtime_seconds) not in (int, float)
            or not math.isfinite(float(runtime_seconds))
            or runtime_seconds < 0
            or disposition
            not in {
                "completed",
                "drained_failure",
                "never_entered_child_creation",
            }
        ):
            raise QuPathExecutionError("native-training process completion is invalid")
        if disposition == "never_entered_child_creation":
            if (
                result is not None
                or spawn_attempt is not None
                or process_launch is not None
                or process_drain is not None
                or exit_status is not None
                or termination_reason is not None
                or failure_kind is not None
                or float(runtime_seconds) != 0.0
                or stdout_excerpt is not None
                or stderr_excerpt is not None
                or stdout_truncated is not None
                or stderr_truncated is not None
            ):
                raise QuPathExecutionError(
                    "unlaunched native-training completion carries process facts"
                )
        else:
            if (
                type(spawn_attempt) is not ProjectMutationProcessSpawnAttemptRecord
                or type(process_launch) is not ProjectMutationProcessLaunchRecord
                or type(process_drain) is not ProjectMutationProcessDrainEvidence
                or type(stdout_excerpt) is not str
                or type(stderr_excerpt) is not str
                or type(stdout_truncated) is not bool
                or type(stderr_truncated) is not bool
                or process_drain.containment_drained is not True
                or spawn_attempt.schema_version != 4
                or process_launch.schema_version != 4
                or process_drain.schema_version != 4
            ):
                raise QuPathExecutionError(
                    "native-training completion lacks its durable process chain"
                )
            common = (
                spawn_attempt.authority_layout_sha256
                == process_launch.authority_layout_sha256
                == process_drain.authority_layout_sha256
                == request.authority_layout_sha256
                and spawn_attempt.project_identity_sha256
                == process_launch.project_identity_sha256
                == process_drain.project_identity_sha256
                == request.project_identity_sha256
                and spawn_attempt.active_mutation_run_sha256
                == process_launch.active_mutation_run_sha256
                == process_drain.active_mutation_run_sha256
                == request.active_mutation_run_sha256
                and spawn_attempt.acquisition_record_sha256
                == process_launch.acquisition_record_sha256
                == process_drain.acquisition_record_sha256
                == request.acquisition_record_sha256
                and spawn_attempt.acquisition_session_challenge_sha256
                == process_launch.acquisition_session_challenge_sha256
                == process_drain.acquisition_session_challenge_sha256
                == request.acquisition_session_challenge_sha256
                and spawn_attempt.mutation_request_sha256
                == process_launch.mutation_request_sha256
                == process_drain.mutation_request_sha256
                == request.mutation_request_sha256
                and spawn_attempt.fencing_token_sha256
                == process_launch.fencing_token_sha256
                == process_drain.fencing_token_sha256
                == request.fencing_token_sha256
                and spawn_attempt.artifact_inventory_sha256
                == process_launch.artifact_inventory_sha256
                == request.artifact_inventory_sha256
                and spawn_attempt.mutation_authorization_sha256
                == process_launch.mutation_authorization_sha256
                == request.mutation_authorization_sha256
                and spawn_attempt.request_envelope_sha256 == request.request_envelope_sha256
                and spawn_attempt.launch_nonce_sha256
                == process_launch.launch_nonce_sha256
                == process_drain.launch_nonce_sha256
                == request.launch_nonce_sha256
                and spawn_attempt.operation == "annotation_classifier"
                and spawn_attempt.role
                == process_launch.role
                == process_drain.role
                == "classifier_training"
                and process_launch.spawn_attempt_sha256
                == process_drain.spawn_attempt_sha256
                == spawn_attempt.spawn_attempt_sha256
                and process_drain.process_launch_sha256 == process_launch.process_launch_sha256
                and process_drain.process_start_token == process_launch.process_start_token
                and process_drain.drain_evidence is not None
                and process_drain.drain_evidence.provider == process_launch.launch_evidence.provider
                and process_drain.terminal_status_sha256
                == _training_terminal_status_sha256(
                    process_launch.process_start_token,
                    exit_status=exit_status,
                    termination_reason=termination_reason,  # type: ignore[arg-type]
                )
                and process_drain.drain_evidence.termination_reason == termination_reason
            )
            if not common:
                raise QuPathExecutionError("native-training durable process chain differs")
            if disposition == "completed":
                if (
                    type(result) is not QuPathNativeTrainingResult
                    or decode_qupath_native_training_result(result.to_bytes(), request=request)
                    != result
                    or exit_status != 0
                    or termination_reason != "normal_exit"
                    or failure_kind is not None
                ):
                    raise QuPathExecutionError("completed native-training observation is invalid")
            else:
                valid_failure = (
                    result is None
                    and failure_kind
                    in {
                        "nonzero_exit",
                        "timeout",
                        "cancelled",
                        "missing_result",
                        "invalid_result",
                    }
                    and (
                        (
                            failure_kind == "nonzero_exit"
                            and type(exit_status) is int
                            and exit_status != 0
                            and termination_reason == "nonzero_exit"
                        )
                        or (
                            failure_kind in {"timeout", "cancelled"}
                            and exit_status is None
                            and termination_reason == failure_kind
                        )
                        or (
                            failure_kind in {"missing_result", "invalid_result"}
                            and exit_status == 0
                            and termination_reason == "normal_exit"
                        )
                    )
                )
                if not valid_failure:
                    raise QuPathExecutionError("failed native-training observation is invalid")
        value = object.__new__(cls)
        for name, item in {
            "disposition": disposition,
            "request": request,
            "result": result,
            "spawn_attempt": spawn_attempt,
            "process_launch": process_launch,
            "process_drain": process_drain,
            "exit_status": exit_status,
            "termination_reason": termination_reason,
            "failure_kind": failure_kind,
            "runtime_seconds": float(runtime_seconds),
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "_issuer_identity": _issuer_identity,
        }.items():
            object.__setattr__(value, name, item)
        return value


class _NativeTrainingCompletionIdentity:
    """Weak-referenceable nominal identity for one runner-issued completion."""

    __slots__ = ("__weakref__",)


@dataclass(frozen=True, slots=True)
class _NativeTrainingCompletionRecord:
    completion_identity: int
    completion_seal_sha256: str
    observed_result_payload_sha256: str | None


def _native_training_completion_seal_sha256(
    completion: QuPathNativeTrainingProcessCompletion,
) -> str:
    def mapping(value: object | None) -> object | None:
        if value is None:
            return None
        exporter = getattr(value, "to_mapping", None)
        if not callable(exporter):
            raise QuPathExecutionError("native-training completion member is not canonical")
        return exporter()

    try:
        payload = canonical_json_bytes(
            {
                "disposition": completion.disposition,
                "request": mapping(completion.request),
                "result": mapping(completion.result),
                "spawn_attempt": mapping(completion.spawn_attempt),
                "process_launch": mapping(completion.process_launch),
                "process_drain": mapping(completion.process_drain),
                "exit_status": completion.exit_status,
                "termination_reason": completion.termination_reason,
                "failure_kind": completion.failure_kind,
                "runtime_seconds": completion.runtime_seconds,
                "stdout_excerpt": completion.stdout_excerpt,
                "stderr_excerpt": completion.stderr_excerpt,
                "stdout_truncated": completion.stdout_truncated,
                "stderr_truncated": completion.stderr_truncated,
            }
        )
    except BaseException as exc:
        raise QuPathExecutionError("native-training completion cannot be authenticated") from exc
    return hashlib.sha256(payload).hexdigest()


class _NativeTrainingCompletionIssuer:
    """Issue and authenticate exact, live runner process observations."""

    __slots__ = ("_lock", "_records")

    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._records: WeakKeyDictionary[
            _NativeTrainingCompletionIdentity,
            _NativeTrainingCompletionRecord,
        ] = WeakKeyDictionary()

    def issue(
        self,
        *,
        observed_result_payload_sha256: str | None,
        **values: object,
    ) -> QuPathNativeTrainingProcessCompletion:
        disposition = values.get("disposition")
        result = values.get("result")
        failure_kind = values.get("failure_kind")
        if observed_result_payload_sha256 is not None and (
            type(observed_result_payload_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", observed_result_payload_sha256) is None
        ):
            raise QuPathExecutionError("native-training observed result digest is invalid")
        if disposition == "completed":
            if (
                type(result) is not QuPathNativeTrainingResult
                or observed_result_payload_sha256 != hashlib.sha256(result.to_bytes()).hexdigest()
            ):
                raise QuPathExecutionError(
                    "completed native-training result bytes are unauthenticated"
                )
        elif failure_kind == "invalid_result":
            if observed_result_payload_sha256 is None:
                raise QuPathExecutionError(
                    "invalid native-training result bytes are unauthenticated"
                )
        elif observed_result_payload_sha256 is not None:
            raise QuPathExecutionError("native-training completion carries unexpected result bytes")
        issuer_identity = _NativeTrainingCompletionIdentity()
        completion = QuPathNativeTrainingProcessCompletion._create(
            _issuer_identity=issuer_identity,
            **values,  # type: ignore[arg-type]
        )
        record = _NativeTrainingCompletionRecord(
            completion_identity=id(completion),
            completion_seal_sha256=(_native_training_completion_seal_sha256(completion)),
            observed_result_payload_sha256=observed_result_payload_sha256,
        )
        with self._lock:
            self._records[issuer_identity] = record
        return completion

    def authenticate(
        self,
        completion: object,
    ) -> str | None:
        if type(completion) is not QuPathNativeTrainingProcessCompletion:
            raise QuPathExecutionError("native-training completion was not runner-issued")
        issuer_identity = getattr(completion, "_issuer_identity", None)
        if type(issuer_identity) is not _NativeTrainingCompletionIdentity:
            raise QuPathExecutionError("native-training completion was not runner-issued")
        with self._lock:
            record = self._records.get(issuer_identity)
            if (
                record is None
                or record.completion_identity != id(completion)
                or record.completion_seal_sha256
                != _native_training_completion_seal_sha256(completion)
            ):
                raise QuPathExecutionError("native-training completion is foreign or changed")
            return record.observed_result_payload_sha256


_NATIVE_TRAINING_COMPLETION_ISSUER = _NativeTrainingCompletionIssuer()


def _issue_native_training_process_completion(
    *,
    observed_result_payload_sha256: str | None,
    **values: object,
) -> QuPathNativeTrainingProcessCompletion:
    return _NATIVE_TRAINING_COMPLETION_ISSUER.issue(
        observed_result_payload_sha256=observed_result_payload_sha256,
        **values,
    )


def _authenticate_native_training_process_completion(
    completion: object,
) -> str | None:
    return _NATIVE_TRAINING_COMPLETION_ISSUER.authenticate(completion)


@dataclass(frozen=True, slots=True, init=False)
class _RegistrationProcessAuthority:
    """One-use retained authority for one registered Warpy role."""

    _runner: "QuPathRunner"
    _session: ExistingRegistrationProjectSession
    _project_lease: object
    _mutation_observation: object
    _launch: QuPathMutationLaunchBinding
    _raw_launch_nonce: bytes
    _request: RegisteredRegistrationRoleRequest
    _request_bytes: bytes
    _script_request_bytes: bytes
    _inventory: object
    _project: ProjectLayout
    _target_plan: "QuPathRegistrationRuntimeTargetPlan"
    _script_bytes: bytes
    _script_bundle_sha256: str
    _script_path: Path
    _exchange_directory: Path
    _request_path: Path
    _script_request_path: Path
    _evidence_path: Path
    _descriptor_path: Path
    _stdout_path: Path
    _stderr_path: Path
    _file_pins: tuple[_PinnedProjectStageFile, ...]
    _absent_pins: tuple[_PinnedProjectStageDirectory, ...]
    _consumed: list[bool]

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("registration process authorities are runner-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("registration process authorities cannot be subclassed")

    def __copy__(self) -> NoReturn:
        raise TypeError("registration process authorities are nonserializable")

    def __deepcopy__(self, memo: object) -> NoReturn:
        del memo
        raise TypeError("registration process authorities are nonserializable")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("registration process authorities are nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class QuPathRegistrationProcessCompletion:
    """Runner-authenticated outcome for one registered registration role."""

    disposition: _RegistrationProcessDisposition
    request: RegisteredRegistrationRoleRequest
    script_request: dict[str, object]
    inventory: object
    evidence: dict[str, object] | None
    evidence_bytes: bytes | None
    descriptor: RegistrationRoleExecutionDescriptor | None
    spawn_attempt: "ProjectMutationProcessSpawnAttemptRecord | None"
    process_launch: "ProjectMutationProcessLaunchRecord | None"
    process_drain: "ProjectMutationProcessDrainEvidence | None"
    exit_status: int | None
    termination_reason: str | None
    failure_kind: _RegistrationProcessFailureKind | None
    runtime_seconds: float | None
    stdout_bytes: bytes | None
    stderr_bytes: bytes | None
    stdout_truncated: bool | None
    stderr_truncated: bool | None
    _issuer_identity: "_RegistrationCompletionIdentity" = field(
        repr=False,
        compare=False,
    )

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("registration process completions are runner-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("registration process completions cannot be subclassed")

    @classmethod
    def _create(
        cls,
        *,
        disposition: _RegistrationProcessDisposition,
        request: RegisteredRegistrationRoleRequest,
        script_request: dict[str, object],
        inventory: object,
        evidence: dict[str, object] | None,
        evidence_bytes: bytes | None,
        descriptor: RegistrationRoleExecutionDescriptor | None,
        spawn_attempt: "ProjectMutationProcessSpawnAttemptRecord | None",
        process_launch: "ProjectMutationProcessLaunchRecord | None",
        process_drain: "ProjectMutationProcessDrainEvidence | None",
        exit_status: int | None,
        termination_reason: str | None,
        failure_kind: _RegistrationProcessFailureKind | None,
        runtime_seconds: float | None,
        stdout_bytes: bytes | None,
        stderr_bytes: bytes | None,
        stdout_truncated: bool | None,
        stderr_truncated: bool | None,
        _issuer_identity: "_RegistrationCompletionIdentity",
    ) -> "QuPathRegistrationProcessCompletion":
        from .catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationProcessDrainEvidence,
            ProjectMutationProcessLaunchRecord,
            ProjectMutationProcessSpawnAttemptRecord,
        )

        if (
            type(request) is not RegisteredRegistrationRoleRequest
            or decode_registered_registration_role_request(request.to_bytes()) != request
            or type(script_request) is not dict
            or script_request != request.script_request
            or type(inventory) is not ProjectMutationArtifactInventory
            or inventory.artifact_inventory_sha256 != request.artifact_inventory_sha256
            or type(_issuer_identity) is not _RegistrationCompletionIdentity
            or (
                runtime_seconds is not None
                and (
                    type(runtime_seconds) not in (int, float)
                    or not math.isfinite(float(runtime_seconds))
                    or runtime_seconds < 0
                )
            )
            or (runtime_seconds is None and disposition != "completed")
            or disposition
            not in {
                "completed",
                "drained_failure",
                "never_entered_child_creation",
            }
        ):
            raise QuPathExecutionError("registration process completion is invalid")
        if disposition == "never_entered_child_creation":
            if (
                any(
                    value is not None
                    for value in (
                        evidence,
                        evidence_bytes,
                        descriptor,
                        spawn_attempt,
                        process_launch,
                        process_drain,
                        exit_status,
                        termination_reason,
                        failure_kind,
                        stdout_bytes,
                        stderr_bytes,
                        stdout_truncated,
                        stderr_truncated,
                    )
                )
                or float(runtime_seconds) != 0.0
            ):
                raise QuPathExecutionError(
                    "unlaunched registration completion carries process facts"
                )
        else:
            if (
                type(descriptor) is not RegistrationRoleExecutionDescriptor
                or type(spawn_attempt) is not ProjectMutationProcessSpawnAttemptRecord
                or type(process_launch) is not ProjectMutationProcessLaunchRecord
                or type(process_drain) is not ProjectMutationProcessDrainEvidence
                or type(stdout_bytes) is not bytes
                or type(stderr_bytes) is not bytes
                or type(stdout_truncated) is not bool
                or type(stderr_truncated) is not bool
                or spawn_attempt.schema_version != 4
                or process_launch.schema_version != 4
                or process_drain.schema_version != 4
                or process_drain.containment_drained is not True
            ):
                raise QuPathExecutionError(
                    "registration completion lacks its durable process chain"
                )
            common = (
                spawn_attempt.authority_layout_sha256
                == process_launch.authority_layout_sha256
                == process_drain.authority_layout_sha256
                == request.authority_layout_sha256
                and spawn_attempt.project_identity_sha256
                == process_launch.project_identity_sha256
                == process_drain.project_identity_sha256
                == request.project_identity_sha256
                and spawn_attempt.active_mutation_run_sha256
                == process_launch.active_mutation_run_sha256
                == process_drain.active_mutation_run_sha256
                == request.active_mutation_run_sha256
                and spawn_attempt.acquisition_record_sha256
                == process_launch.acquisition_record_sha256
                == process_drain.acquisition_record_sha256
                == request.acquisition_record_sha256
                and spawn_attempt.acquisition_session_challenge_sha256
                == process_launch.acquisition_session_challenge_sha256
                == process_drain.acquisition_session_challenge_sha256
                == request.acquisition_session_challenge_sha256
                and spawn_attempt.artifact_inventory_sha256
                == process_launch.artifact_inventory_sha256
                == request.artifact_inventory_sha256
                and spawn_attempt.mutation_request_sha256
                == process_launch.mutation_request_sha256
                == process_drain.mutation_request_sha256
                == request.mutation_request_sha256
                and spawn_attempt.mutation_authorization_sha256
                == process_launch.mutation_authorization_sha256
                == request.mutation_authorization_sha256
                and spawn_attempt.fencing_token_sha256
                == process_launch.fencing_token_sha256
                == process_drain.fencing_token_sha256
                == request.fencing_token_sha256
                and spawn_attempt.request_envelope_sha256 == request.request_envelope_sha256
                and spawn_attempt.launch_nonce_sha256
                == process_launch.launch_nonce_sha256
                == process_drain.launch_nonce_sha256
                == request.launch_nonce_sha256
                and spawn_attempt.operation == "registration_project_write"
                and spawn_attempt.role == process_launch.role == process_drain.role == request.role
                and process_launch.spawn_attempt_sha256
                == process_drain.spawn_attempt_sha256
                == spawn_attempt.spawn_attempt_sha256
                and process_drain.process_launch_sha256 == process_launch.process_launch_sha256
                and process_drain.process_start_token == process_launch.process_start_token
                and process_drain.drain_evidence is not None
                and process_drain.drain_evidence.provider == process_launch.launch_evidence.provider
                and process_drain.terminal_status_sha256
                == _training_terminal_status_sha256(
                    process_launch.process_start_token,
                    exit_status=exit_status,
                    termination_reason=termination_reason,  # type: ignore[arg-type]
                )
                and descriptor.role == request.role
                and descriptor.disposition == disposition
                and descriptor.failure_kind == failure_kind
                and descriptor.request_envelope_sha256 == request.request_envelope_sha256
                and descriptor.script_request_sha256
                == hashlib.sha256(request.script_request_bytes).hexdigest()
                and descriptor.artifact_inventory_sha256 == request.artifact_inventory_sha256
                and descriptor.stdout_sha256 == hashlib.sha256(stdout_bytes).hexdigest()
                and descriptor.stderr_sha256 == hashlib.sha256(stderr_bytes).hexdigest()
                and descriptor.stdout_truncated == stdout_truncated
                and descriptor.stderr_truncated == stderr_truncated
                and descriptor.spawn_attempt_sha256 == spawn_attempt.spawn_attempt_sha256
                and descriptor.process_launch_sha256 == process_launch.process_launch_sha256
                and descriptor.process_drain_sha256 == process_drain.process_drain_sha256
            )
            if not common:
                raise QuPathExecutionError("registration durable process chain differs")
            if disposition == "completed":
                if (
                    type(evidence) is not dict
                    or type(evidence_bytes) is not bytes
                    or exit_status != 0
                    or termination_reason != "normal_exit"
                    or failure_kind is not None
                    or descriptor.observed_evidence_sha256
                    != hashlib.sha256(evidence_bytes).hexdigest()
                ):
                    raise QuPathExecutionError("completed registration observation is invalid")
            elif (
                evidence is not None
                or evidence_bytes is not None
                or failure_kind
                not in {
                    "nonzero_exit",
                    "timeout",
                    "cancelled",
                    "missing_evidence",
                    "invalid_evidence",
                }
                or (
                    failure_kind == "nonzero_exit"
                    and not (
                        type(exit_status) is int
                        and exit_status != 0
                        and termination_reason == "nonzero_exit"
                    )
                )
                or (
                    failure_kind in {"timeout", "cancelled"}
                    and not (exit_status is None and termination_reason == failure_kind)
                )
                or (
                    failure_kind in {"missing_evidence", "invalid_evidence"}
                    and not (exit_status == 0 and termination_reason == "normal_exit")
                )
            ):
                raise QuPathExecutionError("failed registration observation is invalid")
        completion = object.__new__(cls)
        for name, value in {
            "disposition": disposition,
            "request": request,
            "script_request": script_request,
            "inventory": inventory,
            "evidence": evidence,
            "evidence_bytes": evidence_bytes,
            "descriptor": descriptor,
            "spawn_attempt": spawn_attempt,
            "process_launch": process_launch,
            "process_drain": process_drain,
            "exit_status": exit_status,
            "termination_reason": termination_reason,
            "failure_kind": failure_kind,
            "runtime_seconds": None if runtime_seconds is None else float(runtime_seconds),
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "_issuer_identity": _issuer_identity,
        }.items():
            object.__setattr__(completion, name, value)
        return completion


class _RegistrationCompletionIdentity:
    __slots__ = ("__weakref__",)


@dataclass(frozen=True, slots=True)
class _RegistrationCompletionRecord:
    completion_identity: int
    seal_sha256: str


def _registration_completion_seal_sha256(
    completion: QuPathRegistrationProcessCompletion,
) -> str:
    def mapping(value: object | None) -> object | None:
        if value is None:
            return None
        exporter = getattr(value, "to_mapping", None)
        return exporter() if callable(exporter) else value

    try:
        payload = canonical_json_bytes(
            {
                "disposition": completion.disposition,
                "request": completion.request.to_mapping(),
                "script_request": completion.script_request,
                "inventory": mapping(completion.inventory),
                "evidence": completion.evidence,
                "evidence_sha256": (
                    None
                    if completion.evidence_bytes is None
                    else hashlib.sha256(completion.evidence_bytes).hexdigest()
                ),
                "descriptor": mapping(completion.descriptor),
                "spawn_attempt": mapping(completion.spawn_attempt),
                "process_launch": mapping(completion.process_launch),
                "process_drain": mapping(completion.process_drain),
                "exit_status": completion.exit_status,
                "termination_reason": completion.termination_reason,
                "failure_kind": completion.failure_kind,
                "runtime_seconds": completion.runtime_seconds,
                "stdout_sha256": (
                    None
                    if completion.stdout_bytes is None
                    else hashlib.sha256(completion.stdout_bytes).hexdigest()
                ),
                "stderr_sha256": (
                    None
                    if completion.stderr_bytes is None
                    else hashlib.sha256(completion.stderr_bytes).hexdigest()
                ),
                "stdout_truncated": completion.stdout_truncated,
                "stderr_truncated": completion.stderr_truncated,
            }
        )
    except BaseException as exc:
        raise QuPathExecutionError("registration completion cannot be authenticated") from exc
    return hashlib.sha256(payload).hexdigest()


class _RegistrationCompletionIssuer:
    __slots__ = ("_lock", "_records")

    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._records: WeakKeyDictionary[
            _RegistrationCompletionIdentity,
            _RegistrationCompletionRecord,
        ] = WeakKeyDictionary()

    def issue(self, **values: object) -> QuPathRegistrationProcessCompletion:
        identity = _RegistrationCompletionIdentity()
        completion = QuPathRegistrationProcessCompletion._create(
            _issuer_identity=identity,
            **values,  # type: ignore[arg-type]
        )
        record = _RegistrationCompletionRecord(
            completion_identity=id(completion),
            seal_sha256=_registration_completion_seal_sha256(completion),
        )
        with self._lock:
            self._records[identity] = record
        return completion

    def authenticate(self, completion: object) -> QuPathRegistrationProcessCompletion:
        if type(completion) is not QuPathRegistrationProcessCompletion:
            raise QuPathExecutionError("registration completion was not runner-issued")
        identity = getattr(completion, "_issuer_identity", None)
        if type(identity) is not _RegistrationCompletionIdentity:
            raise QuPathExecutionError("registration completion was not runner-issued")
        with self._lock:
            record = self._records.get(identity)
            if (
                record is None
                or record.completion_identity != id(completion)
                or record.seal_sha256 != _registration_completion_seal_sha256(completion)
            ):
                raise QuPathExecutionError("registration completion is foreign or changed")
        return completion


_REGISTRATION_COMPLETION_ISSUER = _RegistrationCompletionIssuer()


def _issue_registration_process_completion(
    **values: object,
) -> QuPathRegistrationProcessCompletion:
    return _REGISTRATION_COMPLETION_ISSUER.issue(**values)


def _authenticate_registration_process_completion(
    completion: object,
) -> QuPathRegistrationProcessCompletion:
    return _REGISTRATION_COMPLETION_ISSUER.authenticate(completion)


@dataclass(frozen=True, slots=True)
class RegistrationRoleScriptTarget:
    role: Literal["registration_import", "transfer_qc"]
    script_asset: QuPathScriptAsset
    relative_path: str
    source_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class QuPathRegistrationRuntimeTargetPlan:
    """Pure, read-only registration runtime target plan."""

    registration_exchange_id: str
    snapshot_resolution_sha256: str
    seed_sha256: str
    script_bundle_sha256: str
    script_bundle_root_uri: str
    script_bundle_manifest_uri: str
    script_bundle_evidence_uri: str
    role_scripts: tuple[RegistrationRoleScriptTarget, ...]
    artifact_targets: tuple[object, ...]
    target_plan_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("registration runtime target plan is runner-issued only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("registration runtime target plan is nonserializable")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "registration_exchange_id": self.registration_exchange_id,
            "snapshot_resolution_sha256": self.snapshot_resolution_sha256,
            "seed_sha256": self.seed_sha256,
            "script_bundle_sha256": self.script_bundle_sha256,
            "script_bundle_root_uri": self.script_bundle_root_uri,
            "script_bundle_manifest_uri": self.script_bundle_manifest_uri,
            "script_bundle_evidence_uri": self.script_bundle_evidence_uri,
            "role_scripts": [
                {
                    "role": item.role,
                    "script_asset": item.script_asset.value,
                    "relative_path": item.relative_path,
                    "source_sha256": item.source_sha256,
                }
                for item in self.role_scripts
            ],
            "artifact_targets": [item.to_mapping() for item in self.artifact_targets],
        }

    def _validate(self, *, issuer: object) -> None:
        from .catalogue import ProjectMutationArtifactTarget

        if (
            not re.fullmatch(r"[0-9a-f]{64}", self.registration_exchange_id)
            or not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_resolution_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", self.seed_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", self.script_bundle_sha256)
            or not self.role_scripts
            or not all(type(item) is RegistrationRoleScriptTarget for item in self.role_scripts)
            or not all(
                type(item) is ProjectMutationArtifactTarget for item in self.artifact_targets
            )
            or hashlib.sha256(canonical_json_bytes(self._unsigned_mapping())).hexdigest()
            != self.target_plan_sha256
            or issuer is not self._issuer
        ):
            raise QuPathExecutionError("registration runtime target plan is invalid")

    @classmethod
    def _issue(cls, *, issuer: object, **values: object) -> "QuPathRegistrationRuntimeTargetPlan":
        if issuer is None:
            raise QuPathExecutionError("registration target-plan issuer is invalid")
        plan = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(plan, name, value)
        object.__setattr__(plan, "_issuer", issuer)
        object.__setattr__(
            plan,
            "target_plan_sha256",
            hashlib.sha256(canonical_json_bytes(plan._unsigned_mapping())).hexdigest(),
        )
        plan._validate(issuer=issuer)
        return plan


def _registration_bundle_tree_observation(root: Path) -> object | None:
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink() or not root.is_dir():
        raise QuPathExecutionError("registration script bundle root is unsafe")
    from .catalogue import (
        _bounded_registration_tree_observation,
        ProjectAuthorityError,
    )

    try:
        return _bounded_registration_tree_observation(
            root,
            max_count=_REGISTRATION_BUNDLE_MAX_MEMBERS,
            max_bytes=_REGISTRATION_BUNDLE_MAX_BYTES,
        )
    except ProjectAuthorityError as exc:
        raise QuPathExecutionError("registration script bundle is unsafe") from exc


class QuPathRunner:
    """Run QuPath through an injected process boundary without global settings.

    A selected CPU-library directory accompanies native and interactive launches;
    the model runtime retains the requested device and explicit PyTorch overrides.
    """

    def __init__(
        self,
        executable: str | Path,
        *,
        executor: ProcessExecutor | None = None,
        containment_expectation: ContainmentCompatibilityExpectationV1 | None = None,
        linux_containment_profile: LinuxContainmentProfile = "hardened-broker",
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
        launch_binding: QuPathProfileLaunchBinding | None = None,
        cpu_native_root: Path | None = None,
    ) -> None:
        if linux_containment_profile not in (
            "hardened-broker",
            "prototype-local-process-group",
        ):
            raise ValueError("Linux containment profile is unsupported")
        self._executable = os.fspath(executable)
        self._command_prefix = qupath_command(executable, launch_binding=launch_binding)
        self._launch_binding = launch_binding
        if cpu_native_root is not None and (
            not isinstance(cpu_native_root, Path)
            or not cpu_native_root.is_absolute()
            or ".." in cpu_native_root.parts
        ):
            raise ValueError("CPU native root must be an absolute Path without traversal")
        self._cpu_native_root = cpu_native_root
        self._containment_expectation = containment_expectation
        self._linux_containment_profile = linux_containment_profile
        self._executor = executor or _DefaultProcessExecutor(
            containment_expectation=containment_expectation,
            linux_containment_profile=linux_containment_profile,
        )
        self._runtime_owner = RuntimeLifecycleOwner.open() if runtime_lifecycle is None else None
        self._runtime_lifecycle = (
            self._runtime_owner.borrower if self._runtime_owner is not None else runtime_lifecycle
        )
        assert self._runtime_lifecycle is not None
        self._registration_target_plan_issuer = object()
        self._interactive_attempt_lock = Lock()
        self._interactive_unconfirmed: list[tuple[object, ExitStack]] = []

    def _launch_environment(self) -> dict[str, str]:
        """Copy inherited settings and carry this runner's selected CPU directory.

        An explicit CLI or desktop selection replaces the inherited fallback
        directory. Without one, the existing launch helpers select the managed
        directory. Device selection and explicit PyTorch overrides stay with DJL.
        """
        environment = dict(os.environ)
        if self._cpu_native_root is not None:
            environment["MIF_CPU_NATIVE_ROOT"] = os.fspath(self._cpu_native_root)
        return environment

    @property
    def is_quiescent(self) -> bool:
        return self._runtime_lifecycle.is_kind_quiescent("qupath-process")

    def require_interactive_stopped(self) -> None:
        """Reject an active or unconfirmed GUI without probing native state."""

        if not self._interactive_attempt_lock.acquire(blocking=False):
            raise QuPathExecutionError("interactive QuPath is already active")
        try:
            if self._interactive_unconfirmed:
                raise QuPathExecutionError("interactive QuPath stop is unconfirmed")
        finally:
            self._interactive_attempt_lock.release()

    def run_interactive_project(
        self,
        project_path: Path,
        *,
        log_directory: Path,
        paired_views: tuple[dict, ...] = (),
        reference_display=None,
        tissue_authoring=None,
        image_appearance_path=None,
        classifier_review=None,
        feature_inspection=None,
        reference_authoring=None,
        inspection=None,
    ) -> ContainedProcessResult:
        """Hold the process use and log authority through natural GUI-tree drain."""

        if reference_display is not None and reference_display.get("mode") not in {"overlay", "side_by_side"}:
            raise QuPathExecutionError("Open alignment editing and candidate review in the embedded registration editor.")
        if not self._interactive_attempt_lock.acquire(blocking=False):
            raise QuPathExecutionError("interactive QuPath is already active")
        try:
            from contextlib import nullcontext
            from .inspection_project import inspection_project

            if inspection is not None and (reference_authoring is not None or tissue_authoring is not None):
                raise QuPathExecutionError("Open the annotation or detector editor to save edits.")
            context = inspection_project(project_path) if inspection is not None else nullcontext(project_path)
            with context as opened_project:
                return self._run_interactive_project(
                    opened_project,
                    log_directory=log_directory,
                    paired_views=paired_views,
                    reference_display=reference_display,
                    tissue_authoring=tissue_authoring,
                    image_appearance_path=image_appearance_path,
                    classifier_review=classifier_review,
                    feature_inspection=feature_inspection,
                    reference_authoring=reference_authoring,
                    inspection=inspection,
                )
        finally:
            self._interactive_attempt_lock.release()

    def _run_interactive_project(
        self,
        project_path: Path,
        *,
        log_directory: Path,
        paired_views: tuple[dict, ...] = (),
        reference_display=None,
        tissue_authoring=None,
        image_appearance_path=None,
        classifier_review=None,
        feature_inspection=None,
        reference_authoring=None,
        inspection=None,
    ) -> ContainedProcessResult:
        if (
            not isinstance(project_path, Path)
            or not project_path.is_absolute()
            or not project_path.is_file()
        ):
            raise QuPathExecutionError("interactive QuPath project is unavailable")
        if not isinstance(log_directory, Path) or not log_directory.is_absolute():
            raise QuPathExecutionError("interactive QuPath log directory is invalid")
        run_interactive = getattr(self._executor, "run_interactive", None)
        require_stopped = getattr(self._executor, "require_interactive_stopped", None)
        if not callable(run_interactive) or not callable(require_stopped):
            raise QuPathExecutionError("interactive QuPath executor is unavailable")
        require_stopped()
        from .desktop_bootstrap import prepare_desktop_bootstrap

        try:
            desktop = prepare_desktop_bootstrap(project_path)
        except ValueError as error:
            raise QuPathExecutionError("The registered desktop support is invalid") from error
        if inspection and desktop is None:
            raise QuPathExecutionError("The selected region requires a pipeline QuPath project.")
        if feature_inspection is not None and desktop is None:
            raise QuPathExecutionError("Open the image-feature preview from a pipeline analysis.")
        if tissue_authoring is not None and desktop is None:
            raise QuPathExecutionError(
                "Open this image in a pipeline project before creating a tissue detector."
            )
        if reference_authoring is not None and desktop is None:
            raise QuPathExecutionError('Open reference annotations from their pipeline preparation.')
        command = (
            (*self._command_prefix, os.fspath(project_path))
            if desktop is None
            else (
                *self._command_prefix,
                "script",
                "--args",
                desktop.encoded_request(
                    paired_views,
                    reference_display=reference_display,
                    tissue_authoring=tissue_authoring,
                    image_appearance_path=image_appearance_path,
                    classifier_review=classifier_review,
                    feature_inspection=feature_inspection,
                    reference_authoring=reference_authoring,
                    inspection=inspection,
                ),
                os.fspath(desktop.script_path),
            )
        )
        return self._execute_interactive_command(command, log_directory, run_interactive, require_stopped)

    def run_interactive_image_region(self, image_path: Path, *, script: Path,
            request_path: Path, result_path: Path, log_directory: Path) -> ContainedProcessResult:
        """Open an image-only region picker through the existing interactive lifecycle."""
        if not self._interactive_attempt_lock.acquire(blocking=False):
            raise QuPathExecutionError("interactive QuPath is already active")
        try:
            from mif_pipeline.io.backends.base import image_identity_member
            if not isinstance(image_path, Path) or not image_path.is_absolute():
                raise QuPathExecutionError("The export image requires an absolute path")
            image_identity_member(image_path)
            if (not all(isinstance(path, Path) and path.is_absolute() for path in
                        (script, request_path, result_path, log_directory))
                    or not script.is_file() or not request_path.is_file() or result_path.exists()):
                raise QuPathExecutionError("The export region request is unavailable")
            run_interactive = getattr(self._executor, "run_interactive", None)
            require_stopped = getattr(self._executor, "require_interactive_stopped", None)
            if not callable(run_interactive) or not callable(require_stopped):
                raise QuPathExecutionError("interactive QuPath executor is unavailable")
            require_stopped()
            command = (*self._command_prefix, "script", "--args", os.fspath(request_path),
                       "--args", os.fspath(result_path), os.fspath(script))
            return self._execute_interactive_command(command, log_directory, run_interactive, require_stopped)
        finally:
            self._interactive_attempt_lock.release()

    def run_registration_editor(self, session) -> ContainedProcessResult:
        """Run the registered reader/editor through the managed native lifecycle."""
        from .registration_editor import RegistrationEditorSession
        if not isinstance(session, RegistrationEditorSession):
            raise QuPathExecutionError("A prepared registration session is required")
        if not self._interactive_attempt_lock.acquire(blocking=False):
            raise QuPathExecutionError("interactive QuPath is already active")
        try:
            paths = (session.script_path, session.request_path) + ((session.project,) if session.project is not None else ())
            if any(not path.is_absolute() or not path.is_file() or path.is_symlink()
                   for path in paths):
                raise QuPathExecutionError("The registration editor request is unavailable")
            run_interactive = getattr(self._executor, "run_interactive", None)
            require_stopped = getattr(self._executor, "require_interactive_stopped", None)
            if not callable(run_interactive) or not callable(require_stopped):
                raise QuPathExecutionError("interactive QuPath executor is unavailable")
            require_stopped()
            command = (*self._command_prefix, "script", "--args", os.fspath(session.request_path),
                       os.fspath(session.script_path))
            return self._execute_interactive_command(command, session.log_directory,
                                                     run_interactive, require_stopped)
        finally:
            self._interactive_attempt_lock.release()

    def _execute_interactive_command(self, command: tuple[str, ...], log_directory: Path,
            run_interactive: Callable[..., ContainedProcessResult],
            require_stopped: Callable[[], None]) -> ContainedProcessResult:
        """Retain the shared process use and logs until the native GUI tree drains."""
        from .interactive_resources import interactive_environment
        token = self._runtime_lifecycle.acquire("qupath-process")
        resources = ExitStack()
        result: ContainedProcessResult | None = None
        primary: BaseException | None = None
        try:
            nonce = os.urandom(16).hex()
            logs = resources.enter_context(DurableDirectoryAuthority(log_directory, create=True))
            reserved = resources.enter_context(
                logs.reserve_log_pair(
                    f"interactive-{nonce}.stdout.log",
                    f"interactive-{nonce}.stderr.log",
                )
            )
            result = run_interactive(
                command,
                stdout_log=reserved.stdout,
                stderr_log=reserved.stderr,
                environment=interactive_environment(self._launch_environment()),
            )
            if type(result) is not ContainedProcessResult or result.args != command:
                raise QuPathExecutionError("interactive QuPath result is invalid")
        except BaseException as exc:
            primary = exc
        try:
            require_stopped()
        except BaseException as guard_failure:
            self._interactive_unconfirmed.append((token, resources.pop_all()))
            if primary is not None:
                if hasattr(primary, "add_note"):
                    primary.add_note(
                        "interactive QuPath stop is unconfirmed; process use and logs remain held"
                    )
                raise primary
            raise QuPathExecutionError(
                "interactive QuPath stop is unconfirmed; process use and logs remain held"
            ) from guard_failure
        try:
            resources.close()
        finally:
            token.release()
        if primary is not None:
            raise primary
        assert result is not None
        return result

    def plan_registration_runtime_targets(
        self,
        project: ProjectLayout,
        snapshot: CurrentRegistrationProjectSnapshot,
        seed: RegistrationRuntimeTargetPlanSeed,
    ) -> QuPathRegistrationRuntimeTargetPlan:
        """Plan exact registration runtime paths without materializing anything."""

        from .catalogue import ProjectMutationArtifactTarget

        if (
            type(project) is not ProjectLayout
            or type(snapshot) is not CurrentRegistrationProjectSnapshot
            or type(seed) is not RegistrationRuntimeTargetPlanSeed
            or project.project_file.absolute().as_uri() != snapshot.project_uri
            or seed.snapshot_resolution_sha256 != snapshot.snapshot_resolution_sha256
        ):
            raise QuPathExecutionError("registration target planning inputs disagree")
        project_root = project.root.absolute()
        if (
            project.project_file.parent.absolute() != project_root
            or project.scripts_dir.parent.absolute() != project_root
            or project.data_dir.parent.absolute() != project_root
            or any(
                path.is_symlink() or (path.exists() and not path.is_dir())
                for path in (project_root, project.scripts_dir, project.data_dir)
            )
        ):
            raise QuPathExecutionError("registration project layout is not a safe owned layout")
        try:
            sources = _source_bytes(registered_script_assets())
        except Exception as exc:
            raise QuPathExecutionError("registered script assets cannot be observed") from exc
        source_by_asset = dict(sources)
        manifest = {
            "schema_version": "qupath-script-bundle/v1",
            "assets": [
                {
                    "path": relative_script_path(asset).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for asset, content in sources
            ],
        }
        manifest_bytes = canonical_json_bytes(manifest)
        bundle_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        bundle_root = project.scripts_dir / f"bundle-{bundle_sha256}"
        manifest_path = bundle_root / "qupath-script-bundle.v1.json"
        expected_files = {Path(relative_script_path(asset)): content for asset, content in sources}
        expected_files[Path("qupath-script-bundle.v1.json")] = manifest_bytes
        expected_dirs = {
            parent
            for relative in expected_files
            for parent in relative.parents
            if parent != Path(".")
        }
        if bundle_root.exists() or bundle_root.is_symlink():
            if bundle_root.is_symlink() or not bundle_root.is_dir():
                raise QuPathExecutionError("registration script bundle root is unsafe")
            bundle_observation = _registration_bundle_tree_observation(bundle_root)
            assert bundle_observation is not None
            expected_members = tuple(
                sorted(
                    [
                        {"path": relative.as_posix(), "kind": "directory"}
                        for relative in expected_dirs
                    ]
                    + [
                        {
                            "path": relative.as_posix(),
                            "kind": "file",
                            "sha256": hashlib.sha256(expected).hexdigest(),
                            "byte_count": len(expected),
                        }
                        for relative, expected in expected_files.items()
                    ],
                    key=lambda item: str(item["path"]),
                )
            )
            if bundle_observation.members != expected_members:
                raise QuPathExecutionError("registration script bundle inventory differs")
        else:
            bundle_observation = None
        bundle_predecessor = (
            None if bundle_observation is None else bundle_observation.content_sha256
        )
        evidence_path = project.data_dir / "qupath-script-bundle.evidence.json"
        if evidence_path.is_symlink():
            raise QuPathExecutionError("registration bundle evidence is unsafe")
        evidence_predecessor = None
        if evidence_path.exists():
            if not evidence_path.is_file():
                raise QuPathExecutionError("registration bundle evidence is unsafe")
            from .catalogue import (
                _bounded_registration_regular_file_digest,
                ProjectAuthorityError,
            )

            try:
                evidence_predecessor, _evidence_size = _bounded_registration_regular_file_digest(
                    evidence_path,
                    max_bytes=_REGISTRATION_BUNDLE_EVIDENCE_MAX_BYTES,
                    label="registration bundle evidence",
                )
            except ProjectAuthorityError as exc:
                raise QuPathExecutionError("registration bundle evidence is unsafe") from exc
        role_assets = {
            "registration_import": QuPathScriptAsset.IMPORT_WARPY_REGISTRATION,
            "transfer_qc": QuPathScriptAsset.TRANSFER_WARPY_OBJECTS,
        }
        role_scripts = tuple(
            RegistrationRoleScriptTarget(
                role=role,  # type: ignore[arg-type]
                script_asset=role_assets[role],
                relative_path=relative_script_path(role_assets[role]).as_posix(),
                source_sha256=hashlib.sha256(source_by_asset[role_assets[role]]).hexdigest(),
            )
            for role in seed.ordered_roles
        )
        targets: list[ProjectMutationArtifactTarget] = [
            ProjectMutationArtifactTarget.create(
                target_kind="registration_script_bundle_tree",
                resolution_kind="exact_uri",
                owner_root_uri=project.scripts_dir.absolute().as_uri(),
                relative_locator_or_metadata_key=bundle_root.name,
                predecessor_sha256=bundle_predecessor,
            ),
            ProjectMutationArtifactTarget.create(
                target_kind="registration_script_bundle_evidence",
                resolution_kind="exact_uri",
                owner_root_uri=project.data_dir.absolute().as_uri(),
                relative_locator_or_metadata_key=evidence_path.name,
                predecessor_sha256=evidence_predecessor,
            ),
        ]
        for role_script in role_scripts:
            targets.append(
                ProjectMutationArtifactTarget.create(
                    target_kind="registration_script_asset",
                    resolution_kind="exact_uri",
                    owner_root_uri=bundle_root.absolute().as_uri(),
                    relative_locator_or_metadata_key=role_script.relative_path,
                    predecessor_sha256=(role_script.source_sha256 if bundle_predecessor else None),
                )
            )
            exchange_root = (
                project.data_dir
                / "registration-exchanges"
                / seed.registration_exchange_id
                / role_script.role
            )
            exchange_parents = (
                project.data_dir / "registration-exchanges",
                project.data_dir / "registration-exchanges" / seed.registration_exchange_id,
            )
            if any(
                path.is_symlink() or (path.exists() and not path.is_dir())
                for path in exchange_parents
            ):
                raise QuPathExecutionError("registration exchange parent is unsafe")
            if exchange_root.exists() or exchange_root.is_symlink():
                raise QuPathExecutionError("registration exchange target is not fresh")
            for kind in (
                "registration_request_envelope",
                "registration_script_request",
                "registration_runtime_evidence",
                "registration_evidence_descriptor",
                "registration_stdout_log",
                "registration_stderr_log",
            ):
                targets.append(
                    ProjectMutationArtifactTarget.create(
                        target_kind=kind,
                        resolution_kind="exact_uri",
                        owner_root_uri=project.data_dir.absolute().as_uri(),
                        relative_locator_or_metadata_key=(
                            "registration-exchanges/"
                            f"{seed.semantic_exchange_member_locator(role=role_script.role, target_kind=kind)}"
                        ),
                        predecessor_sha256=None,
                    )
                )
        values = {
            "registration_exchange_id": seed.registration_exchange_id,
            "snapshot_resolution_sha256": snapshot.snapshot_resolution_sha256,
            "seed_sha256": seed.seed_sha256,
            "script_bundle_sha256": bundle_sha256,
            "script_bundle_root_uri": bundle_root.absolute().as_uri(),
            "script_bundle_manifest_uri": manifest_path.absolute().as_uri(),
            "script_bundle_evidence_uri": evidence_path.absolute().as_uri(),
            "role_scripts": role_scripts,
            "artifact_targets": tuple(targets),
        }
        return QuPathRegistrationRuntimeTargetPlan._issue(
            issuer=self._registration_target_plan_issuer,
            **values,
        )

    def reopen_registration_runtime_target_plan(
        self,
        project,
        snapshot,
        seed,
        *,
        inventory,
        expected_target_plan_sha256,
    ) -> QuPathRegistrationRuntimeTargetPlan:
        """Reopen the original inventory; never substitute installed script bytes."""
        from .catalogue import _project_path_from_uri, _read_bounded_relative_regular_file

        def target(kind):
            matches = [item for item in inventory.static_targets if item.target_kind == kind]
            if len(matches) != 1:
                raise QuPathExecutionError("registration recovery target is missing or ambiguous")
            return matches[0]

        if seed.ordered_roles != ("registration_import",):
            raise QuPathExecutionError("registration recovery requires one import role")
        tree = target("registration_script_bundle_tree")
        bundle_root = (
            _project_path_from_uri(tree.owner_root_uri) / tree.relative_locator_or_metadata_key
        )
        manifest_name = "qupath-script-bundle.v1.json"
        manifest_bytes = _read_bounded_relative_regular_file(
            bundle_root,
            Path(manifest_name),
            max_bytes=_REGISTRATION_BUNDLE_EVIDENCE_MAX_BYTES,
            label="retained registration bundle manifest",
        )
        bundle_sha = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = json.loads(manifest_bytes)
        asset = QuPathScriptAsset.IMPORT_WARPY_REGISTRATION
        relative = relative_script_path(asset).as_posix()
        source_sha = next(item["sha256"] for item in manifest["assets"] if item["path"] == relative)
        kinds = (
            "registration_script_bundle_tree",
            "registration_script_bundle_evidence",
            "registration_script_asset",
            "registration_request_envelope",
            "registration_script_request",
            "registration_runtime_evidence",
            "registration_evidence_descriptor",
            "registration_stdout_log",
            "registration_stderr_log",
        )
        evidence_target = target("registration_script_bundle_evidence")
        plan = QuPathRegistrationRuntimeTargetPlan._issue(
            issuer=self._registration_target_plan_issuer,
            registration_exchange_id=seed.registration_exchange_id,
            snapshot_resolution_sha256=snapshot.snapshot_resolution_sha256,
            seed_sha256=seed.seed_sha256,
            script_bundle_sha256=bundle_sha,
            script_bundle_root_uri=bundle_root.as_uri(),
            script_bundle_manifest_uri=(bundle_root / manifest_name).as_uri(),
            script_bundle_evidence_uri=(
                _project_path_from_uri(evidence_target.owner_root_uri)
                / evidence_target.relative_locator_or_metadata_key
            ).as_uri(),
            role_scripts=(
                RegistrationRoleScriptTarget(
                    role="registration_import",
                    script_asset=asset,
                    relative_path=relative,
                    source_sha256=source_sha,
                ),
            ),
            artifact_targets=tuple(target(kind) for kind in kinds),
        )
        if (
            plan.target_plan_sha256 != expected_target_plan_sha256
            or bundle_root.parent != project.scripts_dir.absolute()
            or bundle_root.name != "bundle-" + bundle_sha
        ):
            raise QuPathExecutionError("retained registration target plan differs")
        return plan

    def recover_registration_process(self, session) -> QuPathRegistrationProcessCompletion:
        """Validate a saved normal exit and resume its original completion publication."""
        from .catalogue import ProjectMutationRecoveryLease

        provider = session._provider
        record, snapshot = provider._existing_registration_session_record(session)
        lease = record.lease
        if (
            type(lease) is not ProjectMutationRecoveryLease
            or record.runner is not self
            or record.ordered_roles != ("registration_import",)
            or record.issued_requests
        ):
            raise QuPathExecutionError(
                "registration recovery requires its original uncompleted import"
            )
        active = provider._validate_live_lease_authority(lease, require_project=True)
        inventory = record.inventory
        exchange_path = (
            record.project.data_dir
            / "registration-exchanges"
            / record.target_plan.registration_exchange_id
            / "registration_import"
        )
        with DurableDirectoryAuthority(exchange_path, create=False) as exchange:
            request = decode_registered_registration_role_request(
                exchange.read_immutable(
                    "envelope.json",
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="retained registration request",
                )
            )
            provider._require_registered_runtime_request_authority(
                request, lease=lease, active=active, inventory=inventory
            )
            attempt = provider._persisted_runtime_spawn_attempt(
                active, inventory, launch_nonce_sha256=request.launch_nonce_sha256
            )
            launch = provider._persisted_runtime_process_launch_for_nonce(
                active, inventory, launch_nonce_sha256=request.launch_nonce_sha256
            )
            drain = (
                None
                if attempt is None or launch is None
                else provider._persisted_process_drain_for_launch(active, attempt, launch)
            )
            if (
                drain is None
                or not provider._is_success_process_drain(lease, drain)
                or request.role != "registration_import"
                or request.runtime_target_plan_sha256 != record.target_plan.target_plan_sha256
                or request.snapshot_resolution_sha256 != snapshot.snapshot_resolution_sha256
                or request.script_sha256 != record.target_plan.role_scripts[0].source_sha256
                or request.registration_exchange_id != record.target_plan.registration_exchange_id
            ):
                raise QuPathExecutionError(
                    "registration recovery lacks its exact successful process"
                )
            script_bytes = exchange.read_immutable(
                "request.json",
                max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                label="retained registration script request",
            )
            if script_bytes != request.script_request_bytes:
                raise QuPathExecutionError("retained registration script request differs")
            evidence_bytes = exchange.read_bytes(
                "evidence.json",
                max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                label="retained registration evidence",
            )
            evidence = _validate_registration_evidence_mapping(
                json.loads(evidence_bytes), request.script_request
            )
            stdout = exchange.read_bytes(
                "stdout.log",
                max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                label="retained registration stdout",
            )
            stderr = exchange.read_bytes(
                "stderr.log",
                max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                label="retained registration stderr",
            )
            descriptor = RegistrationRoleExecutionDescriptor.create(
                role=request.role,
                disposition="completed",
                failure_kind=None,
                request_envelope_sha256=request.request_envelope_sha256,
                script_request_sha256=hashlib.sha256(script_bytes).hexdigest(),
                script_sha256=request.script_sha256,
                script_bundle_sha256=record.target_plan.script_bundle_sha256,
                artifact_inventory_sha256=inventory.artifact_inventory_sha256,
                stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr).hexdigest(),
                stdout_truncated=len(stdout) > _MAXIMUM_LOG_EXCERPT_BYTES,
                stderr_truncated=len(stderr) > _MAXIMUM_LOG_EXCERPT_BYTES,
                spawn_attempt_sha256=attempt.spawn_attempt_sha256,
                process_launch_sha256=launch.process_launch_sha256,
                process_drain_sha256=drain.process_drain_sha256,
                observed_evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            )
            # The normal-exit status is authenticated by the original terminal digest
            # in the completion constructor. Original elapsed time was not persisted.
            completion = _issue_registration_process_completion(
                disposition="completed",
                request=request,
                script_request=request.script_request,
                inventory=inventory,
                evidence=evidence,
                evidence_bytes=evidence_bytes,
                descriptor=descriptor,
                spawn_attempt=attempt,
                process_launch=launch,
                process_drain=drain,
                exit_status=0,
                termination_reason="normal_exit",
                failure_kind=None,
                runtime_seconds=None,
                stdout_bytes=stdout,
                stderr_bytes=stderr,
                stdout_truncated=descriptor.stdout_truncated,
                stderr_truncated=descriptor.stderr_truncated,
            )
            exchange.publish_immutable(
                "descriptor.json",
                descriptor.to_bytes(),
                max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                label="recovered registration execution descriptor",
            )
        record.issued_requests[request.role] = (request, None)
        return completion

    def authenticate_registration_runtime_target_plan(
        self, plan: QuPathRegistrationRuntimeTargetPlan
    ) -> QuPathRegistrationRuntimeTargetPlan:
        if type(plan) is not QuPathRegistrationRuntimeTargetPlan:
            raise QuPathExecutionError("registration runtime target plan is foreign")
        plan._validate(issuer=self._registration_target_plan_issuer)
        return plan

    def issue_project_mutation_launch(
        self, *, project_lease: object
    ) -> QuPathMutationLaunchBinding:
        """Issue a one-use nonce only for a live provider-issued project lease."""
        from .catalogue import ProjectMutationLease

        if not isinstance(project_lease, ProjectMutationLease) or not project_lease.is_live:
            raise QuPathExecutionError("live project mutation lease is required")
        return QuPathMutationLaunchBinding._issue(self, project_lease)

    def issue_native_training_process_authority(
        self,
        *,
        project_lease: object,
        dispatch_authority: NativeTrainingDispatchAuthority,
    ) -> _TrainingProcessAuthority:
        """Refresh the project bundle and close runtime-owned training fields."""

        from .catalogue import ProjectMutationLease
        from .catalogue_operations import QuPathRunnerProjectOperations

        if (
            type(self) is not QuPathRunner
            or type(project_lease) is not ProjectMutationLease
            or not project_lease.is_live
            or project_lease.operation != "annotation_classifier"
            or type(dispatch_authority) is not NativeTrainingDispatchAuthority
        ):
            raise QuPathExecutionError("native-training process preparation inputs are invalid")
        record = _NATIVE_TRAINING_DISPATCH_ISSUER.consume(dispatch_authority, runner=self)
        if (
            record.registry.authenticate_native_training_dispatch_authority(
                reservation=record.reservation,
                readiness=record.readiness,
                runner=self,
                request_core_fields=record.request_core_fields,
            )
            is not record.registry
        ):
            raise QuPathExecutionError("native-training dispatch authority is foreign")
        request_core_fields = record.request_core_fields
        launch = self.issue_project_mutation_launch(project_lease=project_lease)
        retained, observation, active, workspace_scope, active_run = (
            project_lease.native_training_runtime_inputs()
        )
        materialize_script_bundle(
            QuPathRunnerProjectOperations._project_layout_for_stage(project_lease),
            registered_script_assets(),
        )
        script_ref = publish_registered_script_asset(
            retained,
            QuPathScriptAsset.TRAIN_NATIVE_CLASSIFIER,
        )
        script_bytes = retained.bucket("immutable").read_member(script_ref)
        runtime_fields = {
            "schema_name": "mif-qupath-native-training-request",
            "schema_version": 1,
            "authority_layout_sha256": retained.layout.layout_sha256,
            "workspace_storage_scope_sha256": workspace_scope,
            "active_mutation_run_sha256": active_run,
            "project_identity_sha256": (project_lease.designation.identity.project_identity_sha256),
            "acquisition_record_sha256": (project_lease.acquisition_record_sha256),
            "acquisition_session_challenge_sha256": (
                project_lease.acquisition_session_challenge_sha256
            ),
            "artifact_inventory_sha256": (project_lease.artifact_inventory_sha256),
            "mutation_request_sha256": project_lease.mutation_request_sha256,
            "mutation_authorization_sha256": (project_lease.mutation_authorization_sha256),
            "fencing_token_sha256": project_lease.fencing_token_sha256,
            "expected_qupath_version": "0.7.0",
            "script_asset_id": QuPathScriptAsset.TRAIN_NATIVE_CLASSIFIER.value,
            "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "launch_nonce_sha256": launch.launch_nonce_sha256,
        }
        overlap = set(request_core_fields) & set(runtime_fields)
        if overlap:
            raise QuPathExecutionError(
                "native-training registry projection contains runtime-owned fields"
            )
        try:
            scientific_fields = _thaw_native_training_request_projection(request_core_fields)
            if not isinstance(scientific_fields, dict):
                raise QuPathExecutionError("native-training registry projection is invalid")
            request = _issue_qupath_native_training_request(
                core_fields={**scientific_fields, **runtime_fields},
                member_ref_issuer=lambda **values: authority_member_ref_v2(
                    retained.layout,
                    **values,
                ),
            )
            binding = training_runtime_authority_binding_v2(
                retained,
                workspace_storage_scope_sha256=(request.workspace_storage_scope_sha256),
                active_mutation_run_sha256=request.active_mutation_run_sha256,
                role="classifier_training",
                request_identity_sha256=request.request_identity_sha256,
                launch_nonce_sha256=request.launch_nonce_sha256,
            )
            return self._issue_training_process_authority(
                project_lease=project_lease,
                mutation_observation=observation,
                launch=launch,
                request=request,
                authority_layout=retained,
                runtime_binding=binding,
            )
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError(
                "native-training process authority could not be prepared"
            ) from exc

    def issue_registration_process_authority(
        self,
        *,
        session: ExistingRegistrationProjectSession,
        request: RegisteredRegistrationRoleRequest,
    ) -> _RegistrationProcessAuthority:
        """Retain one exact provider-issued registration role request."""

        from .catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationLease,
            ProjectMutationLeaseObservation,
        )

        pins: list[object] = []
        try:
            if (
                type(self) is not QuPathRunner
                or type(session) is not ExistingRegistrationProjectSession
                or type(request) is not RegisteredRegistrationRoleRequest
                or session._provider is None
            ):
                raise QuPathExecutionError("registration process preparation inputs are invalid")
            provider = session._provider
            record, snapshot = provider._existing_registration_session_record(session)
            issued = record.issued_requests.get(request.role)
            if (
                record.runner is not self
                or type(record.lease) is not ProjectMutationLease
                or type(record.mutation_observation) is not ProjectMutationLeaseObservation
                or type(record.inventory) is not ProjectMutationArtifactInventory
                or issued is None
                or issued[0] is not request
                or type(issued[1]) is not QuPathMutationLaunchBinding
                or issued[1]._issuer is not self
                or issued[1]._project_lease is not record.lease
                or issued[1]._consumed != [False]
                or snapshot.project_identity_sha256 != request.project_identity_sha256
                or request.runtime_target_plan_sha256 != record.target_plan.target_plan_sha256
                or request.snapshot_resolution_sha256 != snapshot.snapshot_resolution_sha256
            ):
                raise QuPathExecutionError("registration request differs from its provider session")
            launch = issued[1]
            if (
                type(launch._raw_nonce) is not bytes
                or len(launch._raw_nonce) != 32
                or hashlib.sha256(launch._raw_nonce).hexdigest() != launch.launch_nonce_sha256
                or request.launch_nonce_sha256 != launch.launch_nonce_sha256
            ):
                raise QuPathExecutionError("registration launch binding differs")
            active = provider._validate_live_lease_authority(
                record.lease,
                require_project=True,
            )
            provider._require_registered_runtime_request_authority(
                request,
                lease=record.lease,
                active=active,
                inventory=record.inventory,
            )
            role_asset = {
                "registration_import": (QuPathScriptAsset.IMPORT_WARPY_REGISTRATION),
                "transfer_qc": QuPathScriptAsset.TRANSFER_WARPY_OBJECTS,
            }[request.role]
            bundle = materialize_script_bundle(
                record.project,
                registered_script_assets(),
            )
            script = bundle_script_path(bundle, role_asset)
            script_bytes = _source_bytes((role_asset,))[0][1]
            role_target = next(
                (item for item in record.target_plan.role_scripts if item.role == request.role),
                None,
            )
            if (
                bundle.bundle_sha256 != record.target_plan.script_bundle_sha256
                or bundle.root.absolute().as_uri() != record.target_plan.script_bundle_root_uri
                or role_target is None
                or role_target.script_asset is not role_asset
                or role_target.source_sha256 != hashlib.sha256(script_bytes).hexdigest()
                or request.script_asset_id != role_asset.value
                or request.script_sha256 != role_target.source_sha256
                or not script.is_file()
            ):
                raise QuPathExecutionError("registration script bundle differs from its exact plan")
            exchange_directory = (
                record.project.data_dir
                / "registration-exchanges"
                / request.registration_exchange_id
                / request.role
            ).absolute()
            try:
                exchange_directory.mkdir(parents=True, exist_ok=False)
            except OSError as exc:
                raise QuPathExecutionError("registration exchange target is not fresh") from exc
            paths = {
                "request": exchange_directory / "envelope.json",
                "script_request": exchange_directory / "request.json",
                "evidence": exchange_directory / "evidence.json",
                "descriptor": exchange_directory / "descriptor.json",
                "stdout": exchange_directory / "stdout.log",
                "stderr": exchange_directory / "stderr.log",
            }
            request_bytes = request.to_bytes()
            script_request_bytes = request.script_request_bytes
            with DurableDirectoryAuthority(
                exchange_directory,
                create=False,
            ) as exchange:
                exchange.publish_immutable(
                    "envelope.json",
                    request_bytes,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration request envelope",
                )
                exchange.publish_immutable(
                    "request.json",
                    script_request_bytes,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration script request",
                )
            for path, payload, label in (
                (script, script_bytes, "registration script"),
                (paths["request"], request_bytes, "registration request"),
                (
                    paths["script_request"],
                    script_request_bytes,
                    "registration raw script request",
                ),
            ):
                pin = _PinnedProjectStageFile(path, payload, label=label)
                pins.append(pin)
            for path, label in (
                (paths["evidence"], "registration evidence"),
                (paths["descriptor"], "registration descriptor"),
                (paths["stdout"], "registration stdout"),
                (paths["stderr"], "registration stderr"),
            ):
                pin = _PinnedProjectStageDirectory(path, label=label)
                pins.append(pin)
            value = object.__new__(_RegistrationProcessAuthority)
            file_pins = tuple(pin for pin in pins if type(pin) is _PinnedProjectStageFile)
            absent_pins = tuple(pin for pin in pins if type(pin) is _PinnedProjectStageDirectory)
            values = {
                "_runner": self,
                "_session": session,
                "_project_lease": record.lease,
                "_mutation_observation": record.mutation_observation,
                "_launch": launch,
                "_raw_launch_nonce": bytes(launch._raw_nonce),
                "_request": request,
                "_request_bytes": request_bytes,
                "_script_request_bytes": script_request_bytes,
                "_inventory": record.inventory,
                "_project": record.project,
                "_target_plan": record.target_plan,
                "_script_bytes": script_bytes,
                "_script_bundle_sha256": bundle.bundle_sha256,
                "_script_path": script,
                "_exchange_directory": exchange_directory,
                "_request_path": paths["request"],
                "_script_request_path": paths["script_request"],
                "_evidence_path": paths["evidence"],
                "_descriptor_path": paths["descriptor"],
                "_stdout_path": paths["stdout"],
                "_stderr_path": paths["stderr"],
                "_file_pins": file_pins,
                "_absent_pins": absent_pins,
                "_consumed": [False],
            }
            for name, item in values.items():
                object.__setattr__(value, name, item)
            pins.clear()
            return value
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError(
                "registration process authority could not be issued"
            ) from exc
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            for pin in reversed(pins):
                try:
                    pin.close()  # type: ignore[attr-defined]
                except BaseException as exc:
                    if isinstance(active_error, BaseException):
                        _add_bounded_secondary_note(
                            active_error,
                            exc,
                            context=("registration authority issuance cleanup " "also failed"),
                        )
                    elif cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None and active_error is None:
                raise cleanup_error

    def _issue_training_process_authority(
        self,
        *,
        project_lease: object,
        mutation_observation: object,
        launch: QuPathMutationLaunchBinding,
        request: object,
        authority_layout: object,
        runtime_binding: object,
    ) -> _TrainingProcessAuthority:
        """Retain the exact inputs for one classifier-training process."""

        from .catalogue import (
            ProjectMutationLease,
            ProjectMutationLeaseObservation,
        )

        pins: list[object] = []
        try:
            if (
                type(self) is not QuPathRunner
                or type(project_lease) is not ProjectMutationLease
                or type(mutation_observation) is not ProjectMutationLeaseObservation
                or type(launch) is not QuPathMutationLaunchBinding
                or type(request) is not QuPathNativeTrainingRequest
                or type(authority_layout) is not RetainedAuthorityLayoutV2
                or type(runtime_binding) is not RuntimeAuthorityBindingV2
                or launch._issuer is not self
                or launch._project_lease is not project_lease
                or launch._consumed != [False]
                or type(launch._raw_nonce) is not bytes
                or len(launch._raw_nonce) != 32
                or hashlib.sha256(launch._raw_nonce).hexdigest() != launch.launch_nonce_sha256
            ):
                raise QuPathExecutionError("native-training process authority inputs are invalid")
            provider = project_lease._provider
            authenticated_observation = provider.authenticate_live_mutation_observation(
                mutation_observation
            )
            if (
                authenticated_observation is not mutation_observation
                or mutation_observation._lease is not project_lease
                or mutation_observation._provider is not provider
                or mutation_observation.operation != "annotation_classifier"
                or project_lease.operation != "annotation_classifier"
            ):
                raise QuPathExecutionError("native-training mutation observation differs")
            reparsed_request = decode_qupath_native_training_request(request.to_bytes())
            retained, _observation, active, workspace_scope, active_run = (
                project_lease.native_training_runtime_inputs()
            )
            common_matches = (
                reparsed_request == request
                and authority_layout is retained
                and request.authority_layout_sha256 == authority_layout.layout.layout_sha256
                and request.workspace_storage_scope_sha256 == workspace_scope
                and request.active_mutation_run_sha256 == active_run
                and request.project_identity_sha256
                == mutation_observation.project_identity_sha256
                == project_lease.designation.identity.project_identity_sha256
                and request.acquisition_record_sha256
                == mutation_observation.acquisition_record_sha256
                == project_lease.acquisition_record_sha256
                and request.acquisition_session_challenge_sha256
                == mutation_observation.acquisition_session_challenge_sha256
                == project_lease.acquisition_session_challenge_sha256
                and request.artifact_inventory_sha256
                == mutation_observation.artifact_inventory_sha256
                == project_lease.artifact_inventory_sha256
                and request.mutation_request_sha256
                == mutation_observation.mutation_request_sha256
                == project_lease.mutation_request_sha256
                and request.mutation_authorization_sha256
                == mutation_observation.mutation_authorization_sha256
                == project_lease.mutation_authorization_sha256
                and request.fencing_token_sha256
                == mutation_observation.fencing_token_sha256
                == project_lease.fencing_token_sha256
                and request.launch_nonce_sha256 == launch.launch_nonce_sha256
            )
            if not common_matches:
                raise QuPathExecutionError("native-training request differs from live authority")
            inventory = project_lease._provider_inventory
            if inventory is None:
                raise QuPathExecutionError("native-training artifact inventory is unavailable")
            targets = (*inventory.static_targets, *inventory.dynamic_targets)
            qpproj_targets = [item for item in targets if item.target_kind == "qpproj"]
            classifier_targets = [
                item for item in targets if item.target_kind == "native_classifier"
            ]
            from mif_pipeline.pipeline_core.model_selection import request_cv_review_resource_paths
            classifier_paths = {request.project_relative_path,
                                *request_cv_review_resource_paths(request.to_mapping())}
            project_path = project_lease.designation.project_path
            if (
                len(targets) != 1 + len(classifier_paths)
                or len(qpproj_targets) != 1
                or len(classifier_targets) != len(classifier_paths)
                or inventory.dynamic_targets
                or qpproj_targets[0].resolution_kind != "exact_uri"
                or qpproj_targets[0].owner_root_uri != project_path.parent.resolve().as_uri()
                or qpproj_targets[0].relative_locator_or_metadata_key != project_path.name
                or qpproj_targets[0].predecessor_sha256
                != mutation_observation.qpproj_snapshot_sha256
                or {item.relative_locator_or_metadata_key for item in classifier_targets} != classifier_paths
                or any(item.resolution_kind != "exact_uri"
                       or item.owner_root_uri != project_path.parent.resolve().as_uri()
                       or item.predecessor_sha256 is not None for item in classifier_targets)
            ):
                raise QuPathExecutionError("native-training artifact inventory differs")
            authenticated_binding = training_runtime_authority_binding_v2(
                authority_layout,
                workspace_storage_scope_sha256=workspace_scope,
                active_mutation_run_sha256=active_run,
                role="classifier_training",
                request_identity_sha256=request.request_identity_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                binding=runtime_binding,
            )
            registered_script_ref = publish_registered_script_asset(
                authority_layout,
                QuPathScriptAsset.TRAIN_NATIVE_CLASSIFIER,
            )
            if (
                authenticated_binding is not runtime_binding
                and authenticated_binding != runtime_binding
            ):
                raise QuPathExecutionError("native-training runtime binding changed")
            if (
                authenticated_binding.authority_layout_sha256
                != authority_layout.layout.layout_sha256
                or authenticated_binding.workspace_storage_scope_sha256 != workspace_scope
                or authenticated_binding.script_ref != registered_script_ref
                or authenticated_binding.request_ref != request.request_member_ref
                or authenticated_binding.evidence_ref != request.result_member_ref
                or request.script_asset_id != QuPathScriptAsset.TRAIN_NATIVE_CLASSIFIER.value
            ):
                raise QuPathExecutionError("native-training runtime authority differs")
            immutable = authority_layout.bucket("immutable")
            exchange = authority_layout.bucket("exchange")
            script_bytes = immutable.read_member(registered_script_ref)
            if (
                type(script_bytes) is not bytes
                or hashlib.sha256(script_bytes).hexdigest() != request.script_sha256
            ):
                raise QuPathExecutionError("native-training registered script differs")
            script_path_value = immutable.member_path(registered_script_ref)
            request_path = exchange.member_path(request.request_member_ref)
            result_path = exchange.member_path(request.result_member_ref)
            if any(
                not isinstance(path, Path)
                or not _project_stage_path_is_absolute(path)
                or ".." in _project_stage_path_parts(path)
                for path in (script_path_value, request_path, result_path)
            ):
                raise QuPathExecutionError("native-training retained member path is invalid")
            if exchange.read_member(request.result_member_ref, optional=True) is not None:
                raise QuPathExecutionError("native-training result member already exists")
            request_bytes = request.to_bytes()
            exchange.publish_member(request.request_member_ref, request_bytes)
            reopened_request = exchange.read_member(request.request_member_ref)
            if (
                reopened_request != request_bytes
                or decode_qupath_native_training_request(reopened_request) != request
            ):
                raise QuPathExecutionError("native-training request changed after publication")
            provider._publish_native_training_recovery_binding(
                project_lease,
                request=request,
                binding=runtime_binding,
                active=active,
            )
            script_pin = _PinnedProjectStageFile(
                script_path_value,
                script_bytes,
                label="native-training script",
            )
            pins.append(script_pin)
            request_pin = _PinnedProjectStageFile(
                request_path,
                reopened_request,
                label="native-training request",
            )
            pins.append(request_pin)
            result_parent_pin = _PinnedProjectStageDirectory(
                result_path,
                label="native-training result parent",
            )
            pins.append(result_parent_pin)
            script_pin.validate()
            request_pin.validate()
            result_parent_pin.validate(require_absent=True)
            if (
                provider.authenticate_live_mutation_observation(mutation_observation)
                is not mutation_observation
            ):
                raise QuPathExecutionError("native-training mutation observation changed")
            value = object.__new__(_TrainingProcessAuthority)
            values = {
                "_runner": self,
                "_project_lease": project_lease,
                "_mutation_observation": mutation_observation,
                "_launch": launch,
                "_raw_launch_nonce": bytes(launch._raw_nonce),
                "_request": request,
                "_request_bytes": reopened_request,
                "_request_sha256": hashlib.sha256(reopened_request).hexdigest(),
                "_runtime_binding": runtime_binding,
                "_authority_layout": authority_layout,
                "_workspace_storage_scope_sha256": workspace_scope,
                "_active_mutation_run_sha256": active_run,
                "_script_bytes": script_bytes,
                "_script_ref": registered_script_ref,
                "_request_ref": request.request_member_ref,
                "_result_ref": request.result_member_ref,
                "_stdout_ref": runtime_binding.stdout_ref,
                "_stderr_ref": runtime_binding.stderr_ref,
                "_script_path": script_path_value,
                "_request_path": request_path,
                "_result_path": result_path,
                "_script_pin": script_pin,
                "_request_pin": request_pin,
                "_result_parent_pin": result_parent_pin,
                "_consumed": [False],
            }
            for name, item in values.items():
                object.__setattr__(value, name, item)
            pins.clear()
            return value
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError(
                "native-training process authority could not be issued"
            ) from exc
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            for pin in reversed(pins):
                try:
                    pin.close()  # type: ignore[attr-defined]
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    elif active_error is None:
                        _add_bounded_secondary_note(
                            cleanup_error,
                            exc,
                            context=("additional native-training issuance cleanup " "failure"),
                        )
                    if isinstance(active_error, BaseException):
                        _add_bounded_secondary_note(
                            active_error,
                            exc,
                            context="native-training issuance cleanup also failed",
                        )
            if cleanup_error is not None and active_error is None:
                raise cleanup_error

    @_guarded_qupath_process
    def run_training_process(
        self,
        authority: object,
        *,
        cancellation_event: object | None = None,
    ) -> QuPathNativeTrainingProcessCompletion:
        """Run one exact native-training request in one contained QuPath JVM."""

        from .catalogue import (
            ProjectMutationLease,
            ProjectMutationLeaseObservation,
            ProjectMutationProcessDrainEvidence,
            ProjectMutationProcessLaunchRecord,
            ProjectMutationProcessSpawnAttemptRecord,
            QuPathProcessStartToken,
        )

        if (
            type(self) is not QuPathRunner
            or type(authority) is not _TrainingProcessAuthority
            or authority._runner is not self
            or type(authority._project_lease) is not ProjectMutationLease
            or type(authority._mutation_observation) is not ProjectMutationLeaseObservation
            or type(authority._launch) is not QuPathMutationLaunchBinding
            or type(authority._request) is not QuPathNativeTrainingRequest
            or type(authority._runtime_binding) is not RuntimeAuthorityBindingV2
            or type(authority._authority_layout) is not RetainedAuthorityLayoutV2
            or authority._consumed != [False]
            or authority._launch._consumed != [False]
            or authority._launch._issuer is not self
            or authority._launch._project_lease is not authority._project_lease
            or type(authority._raw_launch_nonce) is not bytes
            or len(authority._raw_launch_nonce) != 32
            or authority._raw_launch_nonce != authority._launch._raw_nonce
            or hashlib.sha256(authority._raw_launch_nonce).hexdigest()
            != authority._launch.launch_nonce_sha256
            or authority._request.launch_nonce_sha256 != authority._launch.launch_nonce_sha256
            or hashlib.sha256(authority._request_bytes).hexdigest() != authority._request_sha256
            or decode_qupath_native_training_request(authority._request_bytes) != authority._request
            or (
                cancellation_event is not None
                and not callable(getattr(cancellation_event, "is_set", None))
            )
        ):
            raise QuPathExecutionError("native-training process authority is invalid")
        project_lease = authority._project_lease
        observation = authority._mutation_observation
        launch = authority._launch
        request = authority._request
        layout = authority._authority_layout
        binding = authority._runtime_binding
        provider = project_lease._provider

        def validate_live(*, require_result_absent: bool) -> None:
            if provider.authenticate_live_mutation_observation(observation) is not observation:
                raise QuPathExecutionError("native-training mutation observation changed")
            project_lease.validate_project_target(
                require_exists=True,
                allow_initial_creation=False,
            )
            layout.validate()
            authority._script_pin.validate()
            authority._request_pin.validate()
            authority._result_parent_pin.validate(require_absent=require_result_absent)
            exchange = layout.bucket("exchange")
            immutable = layout.bucket("immutable")
            if (
                exchange.read_member(authority._request_ref) != authority._request_bytes
                or immutable.read_member(authority._script_ref) != authority._script_bytes
            ):
                raise QuPathExecutionError("native-training retained input changed")
            authenticated = training_runtime_authority_binding_v2(
                layout,
                workspace_storage_scope_sha256=(authority._workspace_storage_scope_sha256),
                active_mutation_run_sha256=authority._active_mutation_run_sha256,
                role="classifier_training",
                request_identity_sha256=request.request_identity_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                binding=binding,
            )
            if (
                authenticated != binding
                or binding.script_ref != authority._script_ref
                or binding.request_ref != authority._request_ref
                or binding.evidence_ref != authority._result_ref
                or binding.stdout_ref != authority._stdout_ref
                or binding.stderr_ref != authority._stderr_ref
                or binding.request_ref != request.request_member_ref
                or binding.evidence_ref != request.result_member_ref
            ):
                raise QuPathExecutionError("native-training runtime binding changed")

        try:
            validate_live(require_result_absent=True)
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError(
                "native-training process authority could not be authenticated"
            ) from exc

        authority._consumed[0] = True
        launch._consumed[0] = True
        attempt: ProjectMutationProcessSpawnAttemptRecord | None = None
        process_launch: ProjectMutationProcessLaunchRecord | None = None
        process_token: QuPathProcessStartToken | None = None
        observed_drain: ProcessContainmentDrainEvidence | None = None
        process_drain: ProjectMutationProcessDrainEvidence | None = None
        terminal_drain_verified = False
        launch_evidence: ProcessContainmentLaunchEvidence | None = None
        started: float | None = None

        def bind_error(error: Exception) -> QuPathExecutionError:
            if process_drain is None:
                if (
                    isinstance(error, QuPathExecutionError)
                    and process_token is None
                    and launch_evidence is None
                    and observed_drain is None
                ):
                    return error
                return QuPathExecutionError._with_containment_evidence(
                    (
                        str(error)
                        if isinstance(error, QuPathExecutionError)
                        else "native-training contained process failed"
                    ),
                    process_start_token=process_token,
                    launch_evidence=launch_evidence,
                    drain_evidence=observed_drain,
                    process_drain_sha256=None,
                )
            return _bind_process_drain_error(
                error,
                process_drain_sha256=process_drain.process_drain_sha256,
                process_start_token=process_token,
                launch_evidence=launch_evidence,
                drain_evidence=process_drain.drain_evidence,
            )

        def never_entered_child_creation(
            *,
            proof_message: str,
        ) -> QuPathNativeTrainingProcessCompletion:
            try:
                project_lease.prove_runtime_request_unlaunched(request)
            except BaseException as exc:
                raise QuPathExecutionError(proof_message) from exc
            return _issue_native_training_process_completion(
                observed_result_payload_sha256=None,
                disposition="never_entered_child_creation",
                request=request,
                result=None,
                spawn_attempt=None,
                process_launch=None,
                process_drain=None,
                exit_status=None,
                termination_reason=None,
                failure_kind=None,
                runtime_seconds=0.0,
                stdout_excerpt=None,
                stderr_excerpt=None,
                stdout_truncated=None,
                stderr_truncated=None,
            )

        try:
            command = (
                *self._command_prefix,
                "script",
                "--project",
                os.fspath(project_lease.designation.project_path),
                os.fspath(authority._script_path),
            )
            environment = _training_environment(
                self._launch_environment(),
                request_path=authority._request_path,
                result_path=authority._result_path,
                request_sha256=authority._request_sha256,
                raw_launch_nonce=authority._raw_launch_nonce,
            )
            containment_request = self._project_containment_request(
                project_lease=project_lease,
                application_request_sha256=request.request_envelope_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                stdout_log_name=authority._stdout_ref.physical_name,
                stderr_log_name=authority._stderr_ref.physical_name,
            )
            contained = getattr(self._executor, "run_contained", None)
            preflight = getattr(self._executor, "preflight_containment", None)
            if not callable(contained) or not callable(preflight):
                raise QuPathExecutionError(
                    "native-training executor lacks contained process authority"
                )
            validate_live(require_result_absent=True)
            try:
                preflight(
                    command,
                    launch_request=containment_request,
                    environment=environment,
                )
            except QuPathExecutionError:
                raise
            except BaseException as exc:
                raise QuPathExecutionError("native-training containment preflight failed") from exc
            validate_live(require_result_absent=True)
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                return never_entered_child_creation(
                    proof_message="native-training unlaunched proof failed",
                )

            def publish_attempt() -> None:
                nonlocal attempt
                if attempt is not None:
                    raise QuPathExecutionError("native-training process attempt repeated")
                candidate = project_lease.record_process_spawn_attempt(
                    request,
                    containment_request=containment_request,
                    runtime_authority_binding=binding,
                )
                if type(candidate) is not ProjectMutationProcessSpawnAttemptRecord:
                    raise QuPathExecutionError("native-training process attempt is foreign")
                attempt = candidate

            def publish_launch(
                process_id: int,
                start_epoch_millis: int,
                evidence: ProcessContainmentLaunchEvidence,
            ) -> str:
                nonlocal process_launch, process_token, launch_evidence
                if attempt is None or process_token is not None:
                    raise QuPathExecutionError("native-training process launch order is invalid")
                candidate_token = self._authoritative_process_start_token(
                    process_id,
                    start_epoch_millis,
                    evidence,
                )
                if type(candidate_token) is not QuPathProcessStartToken:
                    raise QuPathExecutionError("native-training process start token is foreign")
                process_token = candidate_token
                launch_evidence = evidence
                candidate = project_lease.record_process_launch(
                    candidate_token,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                    launch_evidence=evidence,
                    environment=environment,
                )
                if type(candidate) is not ProjectMutationProcessLaunchRecord:
                    raise QuPathExecutionError("native-training process launch is foreign")
                process_launch = candidate
                return candidate.process_launch_sha256

            def observe_drain(evidence: ProcessContainmentDrainEvidence) -> None:
                nonlocal observed_drain
                if observed_drain is not None:
                    raise QuPathExecutionError("native-training process drain repeated")
                observed_drain = evidence

            def persist_drain(
                *,
                exit_status: int | None,
                evidence: ProcessContainmentDrainEvidence,
            ) -> ProjectMutationProcessDrainEvidence:
                nonlocal process_drain
                if attempt is None or process_token is None or process_drain is not None:
                    raise QuPathExecutionError("native-training process drain order is invalid")
                candidate = project_lease.record_process_drain(
                    process_token,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                    terminal_status_sha256=_training_terminal_status_sha256(
                        process_token,
                        exit_status=exit_status,
                        termination_reason=evidence.termination_reason,
                    ),
                    drain_evidence=evidence,
                    process_launch_sha256=(
                        None if process_launch is None else process_launch.process_launch_sha256
                    ),
                )
                if type(candidate) is not ProjectMutationProcessDrainEvidence:
                    raise QuPathExecutionError("native-training process drain is foreign")
                process_drain = candidate
                return candidate

            def publish_terminal(
                terminal_exit_status: int | None,
                evidence: ProcessContainmentDrainEvidence,
            ) -> None:
                nonlocal terminal_drain_verified
                if observed_drain is None or evidence != observed_drain:
                    raise QuPathExecutionError("native-training terminal drain observation differs")
                candidate = persist_drain(
                    exit_status=terminal_exit_status,
                    evidence=evidence,
                )
                reopened = provider.load_process_drain_evidence(
                    request.project_identity_sha256,
                    candidate.process_drain_sha256,
                )
                if reopened.to_mapping() != candidate.to_mapping():
                    raise QuPathExecutionError(
                        "native-training process drain changed after publication"
                    )
                terminal_drain_verified = True

            log_bucket = layout.bucket("logs")
            backend_error: _ContainedProcessError | None = None
            completed: ContainedProcessResult | None = None
            exit_status: int | None = None
            termination_reason: str | None = None
            stdout_excerpt: str
            stderr_excerpt: str
            stdout_truncated: bool
            stderr_truncated: bool
            with ExitStack() as retained:
                for descriptor in project_lease.authoritative_lock_descriptors:
                    retained.enter_context(retain_authoritative_lock_descriptor(descriptor))
                with log_bucket.reserve_log_pair(
                    authority._stdout_ref,
                    authority._stderr_ref,
                ) as logs:
                    validate_live(require_result_absent=True)
                    started = time.monotonic()
                    try:
                        candidate_result = contained(
                            command,
                            lock_descriptors=authoritative_lock_descriptors(),
                            launch_request=containment_request,
                            timeout_seconds=_authoritative_timeout_seconds(),
                            stdout_log=logs.stdout,
                            stderr_log=logs.stderr,
                            environment=environment,
                            process_start_observer=publish_launch,
                            process_attempt_observer=publish_attempt,
                            process_drain_observer=observe_drain,
                            process_terminal_observer=publish_terminal,
                            cancellation_event=cancellation_event,
                        )
                        if type(candidate_result) is not ContainedProcessResult:
                            raise QuPathExecutionError(
                                "native-training executor returned invalid evidence"
                            )
                        completed = candidate_result
                        exit_status = completed.returncode
                        termination_reason = completed.drain_evidence.termination_reason
                        if (
                            attempt is None
                            or process_launch is None
                            or process_token is None
                            or completed.args != command
                            or completed.launch_evidence != process_launch.launch_evidence
                            or completed.launch_evidence != launch_evidence
                            or completed.drain_evidence != observed_drain
                            or process_drain is None
                            or not terminal_drain_verified
                            or completed.drain_evidence != process_drain.drain_evidence
                            or termination_reason not in {"normal_exit", "nonzero_exit"}
                            or (exit_status == 0 and termination_reason != "normal_exit")
                            or (exit_status != 0 and termination_reason != "nonzero_exit")
                        ):
                            raise QuPathExecutionError("native-training executor evidence differs")
                    except _ContainedProcessError as exc:
                        backend_error = exc
                        failure = exc.failure
                        exit_status = failure.returncode
                        termination_reason = (
                            None
                            if failure.drain_evidence is None
                            else failure.drain_evidence.termination_reason
                        )
                        if (
                            attempt is None
                            and process_launch is None
                            and process_token is None
                            and launch_evidence is None
                            and observed_drain is None
                            and process_drain is None
                            and failure.process_id is None
                            and failure.start_epoch_millis is None
                            and failure.launch_evidence is None
                            and failure.drain_evidence is None
                            and _has_exact_backend_cancellation_cause(exc)
                            and cancellation_event is not None
                            and cancellation_event.is_set()  # type: ignore[attr-defined]
                        ):
                            return never_entered_child_creation(
                                proof_message=(
                                    "native-training raced cancellation " "unlaunched proof failed"
                                ),
                            )
                        if (
                            attempt is None
                            or process_token is None
                            or process_drain is None
                            or not terminal_drain_verified
                            or failure.launch_evidence != launch_evidence
                            or failure.drain_evidence is None
                            or failure.drain_evidence != observed_drain
                            or failure.drain_evidence != process_drain.drain_evidence
                            or failure.process_id != process_token.process_id
                            or (
                                process_token.provider == "windows-process-creation-epoch-millis/v1"
                                and failure.start_epoch_millis != process_token.start_epoch_millis
                            )
                        ):
                            raise QuPathExecutionError._with_containment_evidence(
                                "native-training backend process chain is incomplete",
                                process_start_token=process_token,
                                launch_evidence=failure.launch_evidence,
                                drain_evidence=failure.drain_evidence,
                            ) from exc
                    if process_drain is None:
                        raise QuPathExecutionError(
                            "native-training execution lacks a persisted drain"
                        )
                    try:
                        (
                            stdout_excerpt,
                            stderr_excerpt,
                            stdout_truncated,
                            stderr_truncated,
                        ) = _finalize_contained_logs(logs.stdout, logs.stderr)
                    except Exception as exc:
                        raise bind_error(exc) from exc
            runtime_seconds = 0.0 if started is None else max(0.0, time.monotonic() - started)
            try:
                validate_live(require_result_absent=False)
                reopened_drain = provider.load_process_drain_evidence(
                    request.project_identity_sha256,
                    process_drain.process_drain_sha256,
                )
                active = provider._validate_live_lease_authority(
                    project_lease,
                    require_project=True,
                )
                inventory = project_lease._provider_inventory
                reopened_attempt = provider._persisted_runtime_spawn_attempt(
                    active,
                    inventory,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                )
                reopened_launch = provider._persisted_runtime_process_launch_for_nonce(
                    active,
                    inventory,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                )
                if (
                    reopened_attempt != attempt
                    or reopened_launch != process_launch
                    or reopened_drain.to_mapping() != process_drain.to_mapping()
                ):
                    raise QuPathExecutionError("native-training persisted process chain changed")
            except Exception as exc:
                raise bind_error(exc) from exc
            assert attempt is not None
            assert process_launch is not None
            assert process_drain is not None
            assert termination_reason is not None
            if backend_error is not None:
                if termination_reason not in {"timeout", "cancelled"}:
                    raise bind_error(
                        QuPathExecutionError(
                            "native-training backend failure disposition is unsupported"
                        )
                    ) from backend_error
                return _issue_native_training_process_completion(
                    observed_result_payload_sha256=None,
                    disposition="drained_failure",
                    request=request,
                    result=None,
                    spawn_attempt=attempt,
                    process_launch=process_launch,
                    process_drain=process_drain,
                    exit_status=exit_status,
                    termination_reason=termination_reason,
                    failure_kind=termination_reason,  # type: ignore[arg-type]
                    runtime_seconds=runtime_seconds,
                    stdout_excerpt=stdout_excerpt,
                    stderr_excerpt=stderr_excerpt,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                )
            if exit_status != 0:
                return _issue_native_training_process_completion(
                    observed_result_payload_sha256=None,
                    disposition="drained_failure",
                    request=request,
                    result=None,
                    spawn_attempt=attempt,
                    process_launch=process_launch,
                    process_drain=process_drain,
                    exit_status=exit_status,
                    termination_reason=termination_reason,
                    failure_kind="nonzero_exit",
                    runtime_seconds=runtime_seconds,
                    stdout_excerpt=stdout_excerpt,
                    stderr_excerpt=stderr_excerpt,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                )
            result_bytes: bytes | None = None
            try:
                result_bytes = layout.bucket("exchange").read_member(
                    authority._result_ref,
                    optional=True,
                )
                if result_bytes is None:
                    failure_kind: _TrainingProcessFailureKind = "missing_result"
                    result = None
                else:
                    result = decode_qupath_native_training_result(
                        result_bytes,
                        request=decode_qupath_native_training_request(
                            layout.bucket("exchange").read_member(authority._request_ref)
                        ),
                    )
                    failure_kind = None  # type: ignore[assignment]
            except Exception:
                failure_kind = "invalid_result"
                result = None
            if result is None:
                return _issue_native_training_process_completion(
                    observed_result_payload_sha256=(
                        None if result_bytes is None else hashlib.sha256(result_bytes).hexdigest()
                    ),
                    disposition="drained_failure",
                    request=request,
                    result=None,
                    spawn_attempt=attempt,
                    process_launch=process_launch,
                    process_drain=process_drain,
                    exit_status=exit_status,
                    termination_reason=termination_reason,
                    failure_kind=failure_kind,
                    runtime_seconds=runtime_seconds,
                    stdout_excerpt=stdout_excerpt,
                    stderr_excerpt=stderr_excerpt,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                )
            assert result_bytes is not None
            return _issue_native_training_process_completion(
                observed_result_payload_sha256=(hashlib.sha256(result_bytes).hexdigest()),
                disposition="completed",
                request=request,
                result=result,
                spawn_attempt=attempt,
                process_launch=process_launch,
                process_drain=process_drain,
                exit_status=exit_status,
                termination_reason=termination_reason,
                failure_kind=None,
                runtime_seconds=runtime_seconds,
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        except QuPathExecutionError as exc:
            bound = bind_error(exc)
            if bound is exc:
                raise
            raise bound from exc
        except BaseException as exc:
            raise bind_error(exc) from exc
        finally:
            object.__setattr__(authority, "_raw_launch_nonce", b"")
            object.__setattr__(launch, "_raw_nonce", b"")
            active = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            for pin in (
                authority._result_parent_pin,
                authority._request_pin,
                authority._script_pin,
            ):
                try:
                    pin.close()
                except BaseException as cleanup:
                    if isinstance(active, BaseException):
                        _add_bounded_secondary_note(
                            active,
                            cleanup,
                            context="native-training retained cleanup also failed",
                        )
                    elif cleanup_error is None:
                        cleanup_error = cleanup
                    else:
                        _add_bounded_secondary_note(
                            cleanup_error,
                            cleanup,
                            context=("additional native-training retained cleanup " "failure"),
                        )
            if cleanup_error is not None and active is None:
                raise cleanup_error

    @_guarded_qupath_process
    def run_registration_process(
        self,
        authority: object,
        *,
        cancellation_event: object | None = None,
    ) -> QuPathRegistrationProcessCompletion:
        """Run one registered role through the generic containment journal."""

        from .catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationLease,
            ProjectMutationLeaseObservation,
            ProjectMutationProcessDrainEvidence,
            ProjectMutationProcessLaunchRecord,
            ProjectMutationProcessSpawnAttemptRecord,
            QuPathProcessStartToken,
        )

        if (
            type(self) is not QuPathRunner
            or type(authority) is not _RegistrationProcessAuthority
            or authority._runner is not self
            or type(authority._session) is not ExistingRegistrationProjectSession
            or type(authority._project_lease) is not ProjectMutationLease
            or type(authority._mutation_observation) is not ProjectMutationLeaseObservation
            or type(authority._launch) is not QuPathMutationLaunchBinding
            or type(authority._request) is not RegisteredRegistrationRoleRequest
            or type(authority._inventory) is not ProjectMutationArtifactInventory
            or type(authority._project) is not ProjectLayout
            or type(authority._target_plan) is not QuPathRegistrationRuntimeTargetPlan
            or authority._consumed != [False]
            or authority._launch._consumed != [False]
            or authority._launch._issuer is not self
            or authority._launch._project_lease is not authority._project_lease
            or type(authority._raw_launch_nonce) is not bytes
            or len(authority._raw_launch_nonce) != 32
            or authority._raw_launch_nonce != authority._launch._raw_nonce
            or hashlib.sha256(authority._raw_launch_nonce).hexdigest()
            != authority._launch.launch_nonce_sha256
            or authority._request.launch_nonce_sha256 != authority._launch.launch_nonce_sha256
            or authority._request.to_bytes() != authority._request_bytes
            or authority._request.script_request_bytes != authority._script_request_bytes
            or hashlib.sha256(authority._script_bytes).hexdigest()
            != authority._request.script_sha256
            or (
                cancellation_event is not None
                and not callable(getattr(cancellation_event, "is_set", None))
            )
        ):
            raise QuPathExecutionError("registration process authority is invalid")
        project_lease = authority._project_lease
        observation = authority._mutation_observation
        request = authority._request
        launch = authority._launch
        provider = project_lease._provider

        def validate_live(*, require_outputs_absent: bool) -> CurrentRegistrationProjectSnapshot:
            record, snapshot = provider._existing_registration_session_record(authority._session)
            active = provider._validate_live_lease_authority(
                project_lease,
                require_project=True,
            )
            provider._require_registered_runtime_request_authority(
                request,
                lease=project_lease,
                active=active,
                inventory=authority._inventory,
            )
            issued = record.issued_requests.get(request.role)
            if (
                record.runner is not self
                or record.lease is not project_lease
                or record.mutation_observation is not observation
                or record.inventory is not authority._inventory
                or record.project is not authority._project
                or record.target_plan is not authority._target_plan
                or issued is None
                or issued[0] is not request
                or issued[1] is not launch
                or snapshot.project_identity_sha256 != request.project_identity_sha256
                or provider.authenticate_live_mutation_observation(observation) is not observation
            ):
                raise QuPathExecutionError("registration process authority changed")
            project_lease.validate_project_target(
                require_exists=True,
                allow_initial_creation=False,
            )
            for pin in authority._file_pins:
                pin.validate()
            for pin in authority._absent_pins:
                pin.validate(require_absent=require_outputs_absent)
            with DurableDirectoryAuthority(
                authority._exchange_directory,
                create=False,
            ) as exchange:
                if (
                    exchange.read_immutable(
                        "envelope.json",
                        max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                        label="registration request envelope",
                    )
                    != authority._request_bytes
                    or exchange.read_immutable(
                        "request.json",
                        max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                        label="registration script request",
                    )
                    != authority._script_request_bytes
                ):
                    raise QuPathExecutionError("registration retained request changed")
            return snapshot

        validate_live(require_outputs_absent=True)
        authority._consumed[0] = True
        launch._consumed[0] = True
        attempt: ProjectMutationProcessSpawnAttemptRecord | None = None
        process_launch: ProjectMutationProcessLaunchRecord | None = None
        process_token: QuPathProcessStartToken | None = None
        observed_drain: ProcessContainmentDrainEvidence | None = None
        process_drain: ProjectMutationProcessDrainEvidence | None = None
        launch_evidence: ProcessContainmentLaunchEvidence | None = None
        terminal_drain_verified = False
        started: float | None = None
        transfer_qc_snapshot_before: CurrentRegistrationProjectSnapshot | None = None

        def bind_error(error: Exception) -> QuPathExecutionError:
            if process_drain is None:
                if (
                    isinstance(error, QuPathExecutionError)
                    and process_token is None
                    and launch_evidence is None
                    and observed_drain is None
                ):
                    return error
                return QuPathExecutionError._with_containment_evidence(
                    str(error),
                    process_start_token=process_token,
                    launch_evidence=launch_evidence,
                    drain_evidence=observed_drain,
                    process_drain_sha256=None,
                )
            return _bind_process_drain_error(
                error,
                process_drain_sha256=process_drain.process_drain_sha256,
                process_start_token=process_token,
                launch_evidence=launch_evidence,
                drain_evidence=process_drain.drain_evidence,
            )

        def never_entered_child_creation(
            *,
            proof_message: str,
        ) -> QuPathRegistrationProcessCompletion:
            try:
                project_lease.prove_runtime_request_unlaunched(request)
            except BaseException as exc:
                raise QuPathExecutionError(proof_message) from exc
            return _issue_registration_process_completion(
                disposition="never_entered_child_creation",
                request=request,
                script_request=request.script_request,
                inventory=authority._inventory,
                evidence=None,
                evidence_bytes=None,
                descriptor=None,
                spawn_attempt=None,
                process_launch=None,
                process_drain=None,
                exit_status=None,
                termination_reason=None,
                failure_kind=None,
                runtime_seconds=0.0,
                stdout_bytes=None,
                stderr_bytes=None,
                stdout_truncated=None,
                stderr_truncated=None,
            )

        try:
            command = (
                *self._command_prefix,
                "script",
                "--project",
                os.fspath(authority._project.project_file),
                "--args",
                os.fspath(authority._script_request_path),
                "--args",
                os.fspath(authority._evidence_path),
                os.fspath(authority._script_path),
            )
            environment = native_compute_environment(self._launch_environment())
            environment["MIF_QUPATH_PROJECT_LAUNCH_NONCE"] = authority._raw_launch_nonce.hex()
            containment_request = self._project_containment_request(
                project_lease=project_lease,
                application_request_sha256=request.request_envelope_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                stdout_log_name=authority._stdout_path.name,
                stderr_log_name=authority._stderr_path.name,
            )
            contained = getattr(self._executor, "run_contained", None)
            preflight = getattr(self._executor, "preflight_containment", None)
            if not callable(contained) or not callable(preflight):
                raise QuPathExecutionError(
                    "registration executor lacks contained process authority"
                )
            validate_live(require_outputs_absent=True)
            try:
                preflight(
                    command,
                    launch_request=containment_request,
                    environment=environment,
                )
            except QuPathExecutionError:
                raise
            except BaseException as exc:
                raise QuPathExecutionError("registration containment preflight failed") from exc
            current_snapshot = validate_live(require_outputs_absent=True)
            if request.role == "transfer_qc":
                transfer_qc_snapshot_before = current_snapshot
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                return never_entered_child_creation(
                    proof_message="registration unlaunched proof failed"
                )

            def publish_attempt() -> None:
                nonlocal attempt
                if attempt is not None:
                    raise QuPathExecutionError("registration process attempt repeated")
                candidate = project_lease.record_process_spawn_attempt(
                    request,
                    containment_request=containment_request,
                )
                if type(candidate) is not ProjectMutationProcessSpawnAttemptRecord:
                    raise QuPathExecutionError("registration process attempt is foreign")
                attempt = candidate

            def publish_launch(
                process_id: int,
                start_epoch_millis: int,
                evidence: ProcessContainmentLaunchEvidence,
            ) -> str:
                nonlocal process_launch, process_token, launch_evidence
                if attempt is None or process_token is not None:
                    raise QuPathExecutionError("registration process launch order is invalid")
                candidate_token = self._authoritative_process_start_token(
                    process_id,
                    start_epoch_millis,
                    evidence,
                )
                if type(candidate_token) is not QuPathProcessStartToken:
                    raise QuPathExecutionError("registration process start token is foreign")
                process_token = candidate_token
                launch_evidence = evidence
                candidate = project_lease.record_process_launch(
                    candidate_token,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                    launch_evidence=evidence,
                    environment=environment,
                )
                if type(candidate) is not ProjectMutationProcessLaunchRecord:
                    raise QuPathExecutionError("registration process launch is foreign")
                process_launch = candidate
                return candidate.process_launch_sha256

            def observe_drain(
                evidence: ProcessContainmentDrainEvidence,
            ) -> None:
                nonlocal observed_drain
                if observed_drain is not None:
                    raise QuPathExecutionError("registration process drain repeated")
                observed_drain = evidence

            def publish_terminal(
                exit_status: int | None,
                evidence: ProcessContainmentDrainEvidence,
            ) -> None:
                nonlocal process_drain, terminal_drain_verified
                if (
                    attempt is None
                    or process_token is None
                    or process_drain is not None
                    or observed_drain != evidence
                ):
                    raise QuPathExecutionError("registration process drain order is invalid")
                candidate = project_lease.record_process_drain(
                    process_token,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                    terminal_status_sha256=_training_terminal_status_sha256(
                        process_token,
                        exit_status=exit_status,
                        termination_reason=evidence.termination_reason,
                    ),
                    drain_evidence=evidence,
                    process_launch_sha256=(
                        None if process_launch is None else process_launch.process_launch_sha256
                    ),
                )
                if type(candidate) is not ProjectMutationProcessDrainEvidence:
                    raise QuPathExecutionError("registration process drain is foreign")
                reopened = provider.load_process_drain_evidence(
                    request.project_identity_sha256,
                    candidate.process_drain_sha256,
                )
                if reopened.to_mapping() != candidate.to_mapping():
                    raise QuPathExecutionError(
                        "registration process drain changed after publication"
                    )
                process_drain = candidate
                terminal_drain_verified = True

            backend_error: _ContainedProcessError | None = None
            completed: ContainedProcessResult | None = None
            exit_status: int | None = None
            termination_reason: str | None = None
            with DurableDirectoryAuthority(
                authority._exchange_directory,
                create=False,
            ) as exchange:
                with ExitStack() as retained:
                    for descriptor in project_lease.authoritative_lock_descriptors:
                        retained.enter_context(retain_authoritative_lock_descriptor(descriptor))
                    with exchange.reserve_log_pair(
                        authority._stdout_path.name,
                        authority._stderr_path.name,
                    ) as logs:
                        validate_live(require_outputs_absent=False)
                        started = time.monotonic()
                        try:
                            candidate = contained(
                                command,
                                lock_descriptors=authoritative_lock_descriptors(),
                                launch_request=containment_request,
                                timeout_seconds=_authoritative_timeout_seconds(),
                                stdout_log=logs.stdout,
                                stderr_log=logs.stderr,
                                environment=environment,
                                process_start_observer=publish_launch,
                                process_attempt_observer=publish_attempt,
                                process_drain_observer=observe_drain,
                                process_terminal_observer=publish_terminal,
                                cancellation_event=cancellation_event,
                            )
                            if type(candidate) is not ContainedProcessResult:
                                raise QuPathExecutionError(
                                    "registration executor returned invalid evidence"
                                )
                            completed = candidate
                            exit_status = candidate.returncode
                            termination_reason = candidate.drain_evidence.termination_reason
                            if (
                                attempt is None
                                or process_launch is None
                                or process_token is None
                                or process_drain is None
                                or not terminal_drain_verified
                                or candidate.args != command
                                or candidate.launch_evidence != launch_evidence
                                or candidate.launch_evidence != process_launch.launch_evidence
                                or candidate.drain_evidence != observed_drain
                                or candidate.drain_evidence != process_drain.drain_evidence
                                or termination_reason not in {"normal_exit", "nonzero_exit"}
                                or (exit_status == 0 and termination_reason != "normal_exit")
                                or (exit_status != 0 and termination_reason != "nonzero_exit")
                            ):
                                raise QuPathExecutionError("registration executor evidence differs")
                        except _ContainedProcessError as exc:
                            backend_error = exc
                            failure = exc.failure
                            exit_status = failure.returncode
                            termination_reason = (
                                None
                                if failure.drain_evidence is None
                                else failure.drain_evidence.termination_reason
                            )
                            if (
                                attempt is None
                                and process_launch is None
                                and process_token is None
                                and launch_evidence is None
                                and observed_drain is None
                                and process_drain is None
                                and failure.process_id is None
                                and failure.start_epoch_millis is None
                                and failure.launch_evidence is None
                                and failure.drain_evidence is None
                                and _has_exact_backend_cancellation_cause(exc)
                                and cancellation_event is not None
                                and cancellation_event.is_set()  # type: ignore[attr-defined]
                            ):
                                return never_entered_child_creation(
                                    proof_message=(
                                        "registration raced cancellation " "unlaunched proof failed"
                                    )
                                )
                            if (
                                attempt is None
                                or process_token is None
                                or process_drain is None
                                or not terminal_drain_verified
                                or failure.launch_evidence != launch_evidence
                                or failure.drain_evidence is None
                                or failure.drain_evidence != observed_drain
                                or failure.drain_evidence != process_drain.drain_evidence
                                or failure.process_id != process_token.process_id
                                or (
                                    process_token.provider
                                    == "windows-process-creation-epoch-millis/v1"
                                    and failure.start_epoch_millis
                                    != process_token.start_epoch_millis
                                )
                            ):
                                raise QuPathExecutionError._with_containment_evidence(
                                    "registration backend process chain is incomplete",
                                    process_start_token=process_token,
                                    launch_evidence=failure.launch_evidence,
                                    drain_evidence=failure.drain_evidence,
                                ) from exc
                        if process_drain is None:
                            raise QuPathExecutionError(
                                "registration execution lacks a persisted drain"
                            )
                        for stream in (logs.stdout, logs.stderr):
                            stream.flush()
                            os.fsync(stream.fileno())
                stdout_bytes = exchange.read_bytes(
                    authority._stdout_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration stdout log",
                )
                stderr_bytes = exchange.read_bytes(
                    authority._stderr_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration stderr log",
                )
                assert stdout_bytes is not None and stderr_bytes is not None
                stdout_truncated = len(stdout_bytes) > _MAXIMUM_LOG_EXCERPT_BYTES
                stderr_truncated = len(stderr_bytes) > _MAXIMUM_LOG_EXCERPT_BYTES
                # Native evidence uses Gson formatting; retain and validate its exact bytes.
                observed_evidence_bytes = exchange.read_bytes(
                    authority._evidence_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration runtime evidence",
                    optional=True,
                )
                runtime_seconds = 0.0 if started is None else max(0.0, time.monotonic() - started)
                evidence: dict[str, object] | None = None
                valid_evidence_bytes: bytes | None = None
                observed_evidence_sha256: str | None = None
                if observed_evidence_bytes is not None:
                    observed_evidence_sha256 = hashlib.sha256(observed_evidence_bytes).hexdigest()
                if backend_error is not None:
                    if termination_reason not in {"timeout", "cancelled"}:
                        raise QuPathExecutionError(
                            "registration backend failure disposition is unsupported"
                        ) from backend_error
                    failure_kind: _RegistrationProcessFailureKind = (
                        termination_reason  # type: ignore[assignment]
                    )
                    disposition: _RegistrationProcessDisposition = "drained_failure"
                    observed_evidence_sha256 = None
                elif exit_status != 0:
                    failure_kind = "nonzero_exit"
                    disposition = "drained_failure"
                    observed_evidence_sha256 = None
                elif observed_evidence_bytes is None:
                    failure_kind = "missing_evidence"
                    disposition = "drained_failure"
                else:
                    try:
                        if request.role == "transfer_qc" and (
                            transfer_qc_snapshot_before is None
                            or not provider._transfer_qc_project_material_is_unchanged(
                                authority._session,
                                transfer_qc_snapshot_before,
                            )
                        ):
                            raise QuPathExecutionError(
                                "transfer QC changed registered project material"
                            )
                        raw_evidence = json.loads(observed_evidence_bytes.decode("utf-8"))
                        if not isinstance(raw_evidence, dict):
                            raise QuPathExecutionError("registration evidence must be an object")
                        evidence = (
                            _validate_registration_evidence_mapping(
                                raw_evidence,
                                request.script_request,
                            )
                            if request.role == "registration_import"
                            else _validate_transfer_evidence_mapping(
                                raw_evidence,
                                request.script_request,
                            )
                        )
                        valid_evidence_bytes = observed_evidence_bytes
                        failure_kind = None  # type: ignore[assignment]
                        disposition = "completed"
                    except Exception:
                        evidence = None
                        valid_evidence_bytes = None
                        failure_kind = "invalid_evidence"
                        disposition = "drained_failure"
                assert attempt is not None
                assert process_launch is not None
                assert process_drain is not None
                assert termination_reason is not None
                descriptor = RegistrationRoleExecutionDescriptor.create(
                    role=request.role,
                    disposition=disposition,
                    failure_kind=failure_kind,
                    request_envelope_sha256=request.request_envelope_sha256,
                    script_request_sha256=hashlib.sha256(
                        authority._script_request_bytes
                    ).hexdigest(),
                    script_sha256=request.script_sha256,
                    script_bundle_sha256=authority._script_bundle_sha256,
                    artifact_inventory_sha256=(authority._inventory.artifact_inventory_sha256),
                    stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
                    stderr_sha256=hashlib.sha256(stderr_bytes).hexdigest(),
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                    spawn_attempt_sha256=attempt.spawn_attempt_sha256,
                    process_launch_sha256=process_launch.process_launch_sha256,
                    process_drain_sha256=process_drain.process_drain_sha256,
                    observed_evidence_sha256=observed_evidence_sha256,
                )
                exchange.publish_immutable(
                    authority._descriptor_path.name,
                    descriptor.to_bytes(),
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration execution descriptor",
                )
                reopened_descriptor_bytes = exchange.read_immutable(
                    authority._descriptor_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration execution descriptor",
                )
                reopened_descriptor = RegistrationRoleExecutionDescriptor.from_bytes(
                    reopened_descriptor_bytes
                )
                reopened_request = exchange.read_immutable(
                    authority._request_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration request envelope",
                )
                reopened_script_request = exchange.read_immutable(
                    authority._script_request_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration script request",
                )
                reopened_evidence = exchange.read_bytes(
                    authority._evidence_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration runtime evidence",
                    optional=True,
                )
                reopened_stdout = exchange.read_bytes(
                    authority._stdout_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration stdout log",
                )
                reopened_stderr = exchange.read_bytes(
                    authority._stderr_path.name,
                    max_bytes=_PROJECT_STAGE_MAX_INPUT_BYTES,
                    label="registration stderr log",
                )
                active = provider._validate_live_lease_authority(
                    project_lease,
                    require_project=True,
                )
                reopened_attempt = provider._persisted_runtime_spawn_attempt(
                    active,
                    authority._inventory,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                )
                reopened_launch = provider._persisted_runtime_process_launch_for_nonce(
                    active,
                    authority._inventory,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                )
                reopened_drain = provider.load_process_drain_evidence(
                    request.project_identity_sha256,
                    process_drain.process_drain_sha256,
                )
                for pin in authority._file_pins:
                    pin.validate()
                if (
                    reopened_descriptor != descriptor
                    or reopened_request != authority._request_bytes
                    or decode_registered_registration_role_request(reopened_request) != request
                    or reopened_script_request != authority._script_request_bytes
                    or reopened_evidence != observed_evidence_bytes
                    or reopened_stdout != stdout_bytes
                    or reopened_stderr != stderr_bytes
                    or reopened_attempt != attempt
                    or reopened_launch != process_launch
                    or reopened_drain.to_mapping() != process_drain.to_mapping()
                    or authority._inventory.artifact_inventory_sha256
                    != request.artifact_inventory_sha256
                    or materialize_script_bundle(
                        authority._project,
                        registered_script_assets(),
                    ).bundle_sha256
                    != authority._script_bundle_sha256
                ):
                    raise QuPathExecutionError("registration persisted completion members changed")
            return _issue_registration_process_completion(
                disposition=disposition,
                request=request,
                script_request=request.script_request,
                inventory=authority._inventory,
                evidence=evidence,
                evidence_bytes=valid_evidence_bytes,
                descriptor=descriptor,
                spawn_attempt=attempt,
                process_launch=process_launch,
                process_drain=process_drain,
                exit_status=exit_status,
                termination_reason=termination_reason,
                failure_kind=failure_kind,
                runtime_seconds=runtime_seconds,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        except QuPathExecutionError as exc:
            bound = bind_error(exc)
            if bound is exc:
                raise
            raise bound from exc
        except BaseException as exc:
            raise bind_error(exc) from exc
        finally:
            object.__setattr__(authority, "_raw_launch_nonce", b"")
            object.__setattr__(launch, "_raw_nonce", b"")
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            for pin in reversed((*authority._file_pins, *authority._absent_pins)):
                try:
                    pin.close()
                except BaseException as cleanup:
                    if isinstance(active_error, BaseException):
                        _add_bounded_secondary_note(
                            active_error,
                            cleanup,
                            context=("registration retained cleanup also failed"),
                        )
                    elif cleanup_error is None:
                        cleanup_error = cleanup
            if cleanup_error is not None and active_error is None:
                raise cleanup_error

    def _issue_project_stage_process_authority(
        self, *, runtime: object, runtime_binding: object, **values: object
    ) -> _ProjectStageProcessAuthority:
        """Compose the private native context supplied by the owning facade."""

        return _ProjectStageProcessAuthority._issue(
            runner=self,
            runtime=runtime,
            runtime_binding=runtime_binding,
            **values,
        )

    def _bind_project_stage_runtime(
        self,
        *,
        runtime: object,
        composition_binding: object,
        project_operations: object,
    ) -> _ProjectStageRunnerBinding:
        """Bind the issuer-protected facade to this exact runner instance."""

        return _ProjectStageRunnerBinding._issue(
            runner=self,
            runtime=runtime,
            composition_binding=composition_binding,
            project_operations=project_operations,
            issuer=self,
        )

    @_guarded_qupath_process
    def run_project_stage_process(
        self,
        *,
        project_lease: object,
        prepared_descriptor: object,
        launch: QuPathMutationLaunchBinding,
        authority: object,
        cancellation_event: object | None = None,
    ) -> object:
        """Run one authenticated project stage in containment."""

        from .catalogue import ProjectMutationLease, QuPathProcessStartToken
        from .project_stage import (
            ProjectStageNoMutationProof,
            ProjectStageProcessAttemptRecord,
            ProjectStageProcessCompletion,
            ProjectStageProcessDrainRecord,
            ProjectStageProcessEvidenceCapability,
            ProjectStageProcessLaunchRecord,
            ProjectStageProcessSummary,
            _ProjectStageProcessEvidenceIssuer,
        )

        if (
            not isinstance(project_lease, ProjectMutationLease)
            or not project_lease.is_live
            or not isinstance(authority, _ProjectStageProcessAuthority)
            or authority._runner is not self
            or not isinstance(authority._runtime_binding, _ProjectStageRunnerBinding)
            or authority._runtime_binding._runner is not self
            or authority._runtime_binding._issuer is not self
            or authority._project_lease is not project_lease
            or authority._prepared_descriptor is not prepared_descriptor
            or authority._consumed[0]
            or not isinstance(launch, QuPathMutationLaunchBinding)
            or launch._issuer is not self
            or launch._project_lease is not project_lease
            or launch._consumed[0]
            or cancellation_event is not None
            and not callable(getattr(cancellation_event, "is_set", None))
            or not isinstance(authority._evidence_issuer, _ProjectStageProcessEvidenceIssuer)
            or authority._evidence_issuer._runtime_binding is not authority._runtime_binding
        ):
            raise QuPathExecutionError("project-stage process authority is invalid")
        descriptor = prepared_descriptor
        plan = authority._run_plan
        layout = authority._authority_layout
        workspace_lease = authority._workspace_lease
        try:
            active = project_lease._provider._validate_live_lease_authority(
                project_lease, require_project=False
            )
            active_sha256 = project_lease._provider._active_mutation_run_sha256(active)
            workspace_scope = project_lease._provider._workspace_storage_scope_sha256(project_lease)
            layout.validate()
        except (ROIAnalysisError, ValueError) as exc:
            raise QuPathExecutionError("project-stage process lease authority is invalid") from exc
        if (
            not isinstance(layout, RetainedAuthorityLayoutV2)
            or descriptor.authority_layout_sha256 != layout.layout.layout_sha256
            or descriptor.workspace_storage_scope_sha256 != workspace_scope
            or descriptor.active_mutation_run_sha256 != active_sha256
            or descriptor.project_uri != project_lease.designation.project_uri
            or descriptor.project_identity_sha256
            != project_lease.designation.identity.project_identity_sha256
            or descriptor.project_authority_head_sha256
            != project_lease.current_authority_head.authority_head_sha256
            or descriptor.fencing_token_sha256 != project_lease.fencing_token_sha256
            or descriptor.acquisition_record_sha256 != project_lease.acquisition_record_sha256
            or descriptor.acquisition_session_challenge_sha256
            != project_lease.acquisition_session_challenge_sha256
            or descriptor.artifact_inventory_sha256 != project_lease.artifact_inventory_sha256
            or descriptor.mutation_request_sha256 != project_lease.mutation_request_sha256
            or descriptor.mutation_authorization_sha256
            != project_lease.mutation_authorization_sha256
            or descriptor.launch_nonce_sha256 != launch.launch_nonce_sha256
            or descriptor.run_plan_sha256 != plan.plan_sha256
            or descriptor.project_uri != plan.project_uri
            or descriptor.project_identity_sha256 != plan.project_identity_sha256
            or descriptor.project_authority_head_sha256 != plan.project_authority_head_sha256
            or descriptor.target_authorization_set_sha256 != plan.target_authorization_set_sha256
            or descriptor.request_member_set_sha256 != plan.request_member_set_sha256
            or descriptor.script_bundle_sha256 != plan.script_bundle_sha256
            or descriptor.script_bundle_manifest_ref != plan.script_bundle_manifest_ref
            or descriptor.script_bundle_manifest_sha256 != plan.script_bundle_manifest_sha256
            or descriptor.script_bundle_manifest_byte_count
            != plan.script_bundle_manifest_byte_count
            or descriptor.target_script_asset_id != plan.target_script_asset_id
            or descriptor.target_script_ref != plan.target_script_ref
            or descriptor.target_script_sha256 != plan.target_script_sha256
            or descriptor.target_script_byte_count != plan.target_script_byte_count
            or descriptor.guard_script_asset_id != plan.guard_script_asset_id
            or descriptor.guard_script_ref != plan.guard_script_ref
            or descriptor.guard_script_sha256 != plan.guard_script_sha256
            or descriptor.guard_script_byte_count != plan.guard_script_byte_count
        ):
            raise QuPathExecutionError("project-stage prepared authority differs from the live run")
        expected_paths_and_bytes = (
            (
                authority._run_plan_path,
                authority._run_plan_bytes,
                plan.to_mapping(),
                "project-stage run plan",
            ),
            (
                authority._prepared_descriptor_path,
                authority._prepared_descriptor_bytes,
                descriptor.to_mapping(),
                "project-stage prepared descriptor",
            ),
        )
        for _path, payload, mapping, label in expected_paths_and_bytes:
            if type(payload) is not bytes or payload != canonical_json_bytes(mapping):
                raise QuPathExecutionError(f"{label} bytes differ")
        if (
            hashlib.sha256(authority._bundle_manifest_bytes).hexdigest()
            != descriptor.script_bundle_manifest_sha256
            or len(authority._bundle_manifest_bytes) != descriptor.script_bundle_manifest_byte_count
            or hashlib.sha256(authority._target_script_bytes).hexdigest()
            != descriptor.target_script_sha256
            or len(authority._target_script_bytes) != descriptor.target_script_byte_count
            or hashlib.sha256(authority._guard_script_bytes).hexdigest()
            != descriptor.guard_script_sha256
            or len(authority._guard_script_bytes) != descriptor.guard_script_byte_count
            or authority._process_launch_path.name != descriptor.process_launch_ref.physical_name
        ):
            raise QuPathExecutionError("project-stage bundle authority differs")

        project_path = project_lease.designation.project_path
        mode = getattr(plan.launch_mode, "value", plan.launch_mode)
        if mode == "headless_project_script":
            image_selector: tuple[str, ...] = ()
            if len(plan.targets) == 1:
                # QuPath selects project entries by name, not by their stable ID.
                # Keep the existing full-project route if that name is ambiguous.
                try:
                    project_data = json.loads(project_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    project_data = None
                entries = project_data.get("images") if isinstance(project_data, dict) else None
                if isinstance(entries, list) and all(isinstance(item, dict) for item in entries):
                    matching = [
                        item for item in entries
                        if str(item.get("entryID")) == plan.targets[0].project_entry_id
                    ]
                    if len(matching) == 1:
                        name = matching[0].get("imageName")
                        if (
                            isinstance(name, str) and name and "\0" not in name
                            and not name.startswith("-")
                            and sum(item.get("imageName") == name for item in entries) == 1
                        ):
                            image_selector = ("--image", name)
            command = (
                *self._command_prefix,
                "script",
                "--project",
                os.fspath(project_path),
                *image_selector,
                os.fspath(authority._target_script_path),
            )
            command_profile: Literal["headless-script", "interactive-project"] = "headless-script"
        elif mode == "interactive_run_for_project":
            command = (*self._command_prefix, "--project", os.fspath(project_path))
            command_profile = "interactive-project"
        else:
            raise QuPathExecutionError("project-stage launch mode is invalid")
        environment = _project_stage_environment(
            self._launch_environment(),
            kernel_threads=1 if plan.target_script_asset_id == QuPathScriptAsset.RUN_INSTANSEG.value else None,
            run_plan_path=authority._run_plan_path,
            prepared_descriptor_path=authority._prepared_descriptor_path,
            process_launch_path=authority._process_launch_path,
            bundle_manifest_path=authority._bundle_manifest_path,
            guard_script_path=authority._guard_script_path,
            target_script_path=authority._target_script_path,
            raw_launch_nonce=launch._raw_nonce,
        )
        containment_request = self._project_containment_request(
            project_lease=project_lease,
            application_request_sha256=descriptor.prepared_descriptor_sha256,
            launch_nonce_sha256=launch.launch_nonce_sha256,
            stdout_log_name=descriptor.stdout_log_ref.physical_name,
            stderr_log_name=descriptor.stderr_log_ref.physical_name,
            command_profile=command_profile,
        )
        preflight = getattr(self._executor, "preflight_containment", None)
        contained = getattr(self._executor, "run_contained", None)
        if not callable(preflight) or not callable(contained):
            raise QuPathExecutionError("project-stage executor lacks authoritative containment")
        executable_version = self._executable_version()
        common = {
            "authority_layout_sha256": descriptor.authority_layout_sha256,
            "workspace_storage_scope_sha256": (descriptor.workspace_storage_scope_sha256),
            "project_identity_sha256": descriptor.project_identity_sha256,
            "active_mutation_run_sha256": descriptor.active_mutation_run_sha256,
            "acquisition_record_sha256": descriptor.acquisition_record_sha256,
            "acquisition_session_challenge_sha256": (
                descriptor.acquisition_session_challenge_sha256
            ),
            "artifact_inventory_sha256": descriptor.artifact_inventory_sha256,
            "mutation_request_sha256": descriptor.mutation_request_sha256,
            "mutation_authorization_sha256": (descriptor.mutation_authorization_sha256),
            "fencing_token_sha256": descriptor.fencing_token_sha256,
            "run_plan_sha256": descriptor.run_plan_sha256,
            "prepared_descriptor_sha256": (descriptor.prepared_descriptor_sha256),
            "application_request_sha256": (descriptor.prepared_descriptor_sha256),
            "launch_nonce_sha256": descriptor.launch_nonce_sha256,
        }
        attempt: ProjectStageProcessAttemptRecord | None = None
        attempt_ref = None
        process_launch: ProjectStageProcessLaunchRecord | None = None
        process_launch_ref = None
        process_drain: ProjectStageProcessDrainRecord | None = None
        process_drain_ref = None
        process_token: QuPathProcessStartToken | None = None

        pins: list[object] = []

        def validate_pins() -> None:
            for pin in pins:
                pin.validate()  # type: ignore[attr-defined]
            launch_parent.validate(require_absent=process_launch is None)  # type: ignore[has-type]
            project_lease.validate_project_target(require_exists=True, allow_initial_creation=False)

        def publish_attempt() -> None:
            nonlocal attempt, attempt_ref
            if attempt is not None:
                raise QuPathExecutionError("project-stage process attempt repeated")
            candidate = ProjectStageProcessAttemptRecord.create(
                **common, containment_request=containment_request
            )
            candidate_ref = authority._publish_record(candidate)
            reopened = authority._reopen_record(candidate_ref, candidate)
            if reopened != candidate:
                raise QuPathExecutionError("project-stage process attempt changed")
            attempt = candidate
            attempt_ref = candidate_ref

        def publish_launch(
            process_id: int,
            start_epoch_millis: int,
            launch_evidence: ProcessContainmentLaunchEvidence,
        ) -> str:
            nonlocal process_launch, process_launch_ref, process_token
            if attempt is None or attempt_ref is None or process_launch is not None:
                raise QuPathExecutionError("project-stage launch order is invalid")
            candidate_token = self._authoritative_process_start_token(
                process_id,
                start_epoch_millis,
                launch_evidence,
            )
            assert isinstance(candidate_token, QuPathProcessStartToken)
            candidate = ProjectStageProcessLaunchRecord.create(
                **common,
                process_launch_ref=descriptor.process_launch_ref,
                launch_request_identity_sha256=(descriptor.launch_request_identity_sha256),
                attempt_sha256=attempt.attempt_sha256,
                process_start_token=candidate_token,
                launch_evidence=launch_evidence,
                command=command,
                environment=environment,
                executable_version=executable_version,
            )
            candidate_ref = authority._publish_launch(candidate)
            reopened = authority._reopen_launch(candidate_ref, candidate)
            if candidate_ref != descriptor.process_launch_ref or reopened != candidate:
                raise QuPathExecutionError("project-stage process launch changed")
            process_token = candidate_token
            process_launch = candidate
            process_launch_ref = candidate_ref
            launch_payload = canonical_json_bytes(candidate.to_mapping())
            launch_pin = _PinnedProjectStageFile(
                authority._process_launch_path,
                launch_payload,
                label="project-stage process launch",
            )
            pins.append(launch_pin)
            retained.callback(launch_pin.close)  # type: ignore[has-type]
            validate_pins()
            return candidate.process_launch_sha256

        def publish_terminal(
            exit_status: int | None,
            drain_evidence: ProcessContainmentDrainEvidence,
        ) -> None:
            nonlocal process_drain, process_drain_ref
            if (
                attempt is None
                or process_launch is None
                or process_token is None
                or process_drain is not None
            ):
                raise QuPathExecutionError("project-stage drain order is invalid")
            candidate = ProjectStageProcessDrainRecord.create(
                **common,
                attempt_sha256=attempt.attempt_sha256,
                process_launch_sha256=process_launch.process_launch_sha256,
                process_start_token=process_token,
                termination_reason=drain_evidence.termination_reason,
                exit_status=exit_status,
                drain_evidence=drain_evidence,
                containment_drained=True,
                stdout_log_ref=descriptor.stdout_log_ref,
                stderr_log_ref=descriptor.stderr_log_ref,
            )
            candidate_ref = authority._publish_record(candidate)
            reopened = authority._reopen_record(candidate_ref, candidate)
            if reopened != candidate:
                raise QuPathExecutionError("project-stage process drain changed")
            process_drain = candidate
            process_drain_ref = candidate_ref

        def never_entered_child_creation() -> ProjectStageProcessCompletion:
            proof = authority._evidence_issuer.no_mutation_proof(
                variant="never_entered_child_creation",
                project_identity_sha256=descriptor.project_identity_sha256,
                active_mutation_run_sha256=(descriptor.active_mutation_run_sha256),
                acquisition_record_sha256=(descriptor.acquisition_record_sha256),
                acquisition_session_challenge_sha256=(
                    descriptor.acquisition_session_challenge_sha256
                ),
                prepared_descriptor_sha256=(descriptor.prepared_descriptor_sha256),
                attempt_sha256=None,
                process_launch_sha256=None,
                process_drain_sha256=None,
            )
            assert isinstance(proof, ProjectStageNoMutationProof)
            return ProjectStageProcessCompletion.create(
                disposition="never_entered_child_creation",
                process_evidence=None,
                no_mutation_proof=proof,
                process_summary=None,
            )

        authority._consumed[0] = True
        launch._consumed[0] = True
        try:
            with _ProjectStageRetainedExitStack() as retained:
                descriptors = project_lease.authoritative_lock_descriptors
                for descriptor_fd in descriptors:
                    retained.enter_context(retain_authoritative_lock_descriptor(descriptor_fd))
                for path, payload, _mapping_value, label in (
                    *expected_paths_and_bytes,
                    (
                        authority._bundle_manifest_path,
                        authority._bundle_manifest_bytes,
                        None,
                        "project-stage bundle manifest",
                    ),
                    (
                        authority._guard_script_path,
                        authority._guard_script_bytes,
                        None,
                        "project-stage guard script",
                    ),
                    (
                        authority._target_script_path,
                        authority._target_script_bytes,
                        None,
                        "project-stage target script",
                    ),
                ):
                    pin = _PinnedProjectStageFile(path, payload, label=label)
                    pins.append(pin)
                    retained.callback(pin.close)
                launch_parent = _PinnedProjectStageDirectory(
                    authority._process_launch_path,
                    label="project-stage launch parent",
                )
                retained.callback(launch_parent.close)
                validate_pins()
                try:
                    preflight(
                        command,
                        launch_request=containment_request,
                        environment=environment,
                    )
                except QuPathExecutionError:
                    raise
                except BaseException as exc:
                    raise QuPathExecutionError(
                        "project-stage containment preflight failed"
                    ) from exc
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()  # type: ignore[attr-defined]
                ):
                    return never_entered_child_creation()
                log_bucket = layout.bucket("logs")
                completed: ContainedProcessResult | None = None
                backend_error: _ContainedProcessError | None = None
                execution_started: float | None = None
                try:
                    with log_bucket.reserve_log_pair(
                        descriptor.stdout_log_ref, descriptor.stderr_log_ref
                    ) as logs:
                        if (
                            cancellation_event is not None
                            and cancellation_event.is_set()  # type: ignore[attr-defined]
                        ):
                            return never_entered_child_creation()
                        execution_started = time.monotonic()
                        try:
                            completed = contained(
                                command,
                                lock_descriptors=authoritative_lock_descriptors(),
                                launch_request=containment_request,
                                timeout_seconds=_authoritative_timeout_seconds(),
                                stdout_log=logs.stdout,
                                stderr_log=logs.stderr,
                                environment=environment,
                                process_start_observer=publish_launch,
                                process_attempt_observer=publish_attempt,
                                process_terminal_observer=publish_terminal,
                                cancellation_event=cancellation_event,
                            )
                        except _ContainedProcessError as exc:
                            backend_error = exc
                            raise
                except BaseException as escaped:
                    if backend_error is None:
                        raise
                    if escaped is not backend_error:
                        _add_bounded_secondary_note(
                            backend_error,
                            escaped,
                            context="project-stage retained log cleanup also failed",
                        )
                chain_complete = (
                    attempt is not None
                    and attempt_ref is not None
                    and process_launch is not None
                    and process_launch_ref is not None
                    and process_drain is not None
                    and process_drain_ref is not None
                    and process_token is not None
                )
                if not chain_complete:
                    if backend_error is not None:
                        raise QuPathExecutionError(
                            "project-stage execution has no complete durable process chain"
                        ) from backend_error
                    raise QuPathExecutionError(
                        "project-stage executor returned invalid process evidence"
                    )
                assert attempt is not None
                assert attempt_ref is not None
                assert process_launch is not None
                assert process_launch_ref is not None
                assert process_drain is not None
                assert process_drain_ref is not None
                assert process_token is not None
                if completed is not None:
                    if (
                        completed.args != command
                        or completed.launch_evidence != process_launch.launch_evidence
                        or completed.drain_evidence != process_drain.drain_evidence
                        or completed.returncode != process_drain.exit_status
                    ):
                        raise QuPathExecutionError(
                            "project-stage executor returned invalid process evidence"
                        )
                elif backend_error is not None:
                    failure = backend_error.failure
                    if (
                        failure.process_id != process_token.process_id
                        or (
                            process_token.provider == "windows-process-creation-epoch-millis/v1"
                            and failure.start_epoch_millis != process_token.start_epoch_millis
                        )
                        or failure.launch_evidence != process_launch.launch_evidence
                        or failure.drain_evidence != process_drain.drain_evidence
                        or failure.returncode != process_drain.exit_status
                    ):
                        raise QuPathExecutionError(
                            "project-stage backend failure evidence differs"
                        ) from backend_error
                else:
                    raise QuPathExecutionError(
                        "project-stage executor returned no terminal evidence"
                    )
                if backend_error is not None and process_drain.termination_reason not in {
                    "launch_publication_failed",
                    "timeout",
                    "cancelled",
                }:
                    raise QuPathExecutionError(
                        "project-stage backend failed after terminal publication"
                    ) from backend_error
                stdout_bytes = log_bucket.read_member(descriptor.stdout_log_ref)
                stderr_bytes = log_bucket.read_member(descriptor.stderr_log_ref)
                if type(stdout_bytes) is not bytes or type(stderr_bytes) is not bytes:
                    raise QuPathExecutionError("project-stage logs are unavailable")
                if execution_started is None:
                    raise QuPathExecutionError("project-stage execution timing is unavailable")
                private_record_type = (
                    QuPathRunRecord
                    if process_drain.exit_status is not None
                    else _ProjectStageQuPathRunRecord
                )
                private_run_record = private_record_type(
                    command=command,
                    executable_version=executable_version,
                    exit_status=process_drain.exit_status,
                    stdout_log=log_bucket.member_path(descriptor.stdout_log_ref),
                    stderr_log=log_bucket.member_path(descriptor.stderr_log_ref),
                    runtime_seconds=max(0.0, time.monotonic() - execution_started),
                )
                if (
                    private_run_record.exit_status != process_drain.exit_status
                    or private_run_record.command != command
                ):
                    raise QuPathExecutionError("project-stage private run record differs")
                reopened_attempt = authority._reopen_record(attempt_ref, attempt)
                reopened_launch = authority._reopen_launch(process_launch_ref, process_launch)
                reopened_drain = authority._reopen_record(process_drain_ref, process_drain)
                if (
                    reopened_attempt != attempt
                    or reopened_launch != process_launch
                    or reopened_drain != process_drain
                ):
                    raise QuPathExecutionError("project-stage process chain changed after drain")
                if process_drain.termination_reason == "launch_publication_failed":
                    proof = authority._evidence_issuer.no_mutation_proof(
                        variant="contained_never_released",
                        project_identity_sha256=descriptor.project_identity_sha256,
                        active_mutation_run_sha256=(descriptor.active_mutation_run_sha256),
                        acquisition_record_sha256=(descriptor.acquisition_record_sha256),
                        acquisition_session_challenge_sha256=(
                            descriptor.acquisition_session_challenge_sha256
                        ),
                        prepared_descriptor_sha256=(descriptor.prepared_descriptor_sha256),
                        attempt_sha256=attempt.attempt_sha256,
                        process_launch_sha256=(process_launch.process_launch_sha256),
                        process_drain_sha256=process_drain.process_drain_sha256,
                    )
                    return ProjectStageProcessCompletion.create(
                        disposition="contained_never_released",
                        process_evidence=None,
                        no_mutation_proof=proof,
                        process_summary=None,
                    )
                if process_drain.termination_reason not in {
                    "normal_exit",
                    "nonzero_exit",
                    "timeout",
                    "cancelled",
                }:
                    raise QuPathExecutionError("project-stage termination reason is unsupported")
                evidence = authority._evidence_issuer.process_evidence(
                    attempt=reopened_attempt,
                    attempt_ref=attempt_ref,
                    launch=reopened_launch,
                    launch_ref=process_launch_ref,
                    drain=reopened_drain,
                    drain_ref=process_drain_ref,
                )
                assert isinstance(evidence, ProjectStageProcessEvidenceCapability)
                summary = ProjectStageProcessSummary.create(
                    process_evidence_sha256=evidence.evidence_set_sha256,
                    process_start_token=process_token,
                    termination_reason=process_drain.termination_reason,
                    exit_status=process_drain.exit_status,
                    stdout_log_ref=descriptor.stdout_log_ref,
                    stdout_payload_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
                    stdout_byte_count=len(stdout_bytes),
                    stderr_log_ref=descriptor.stderr_log_ref,
                    stderr_payload_sha256=hashlib.sha256(stderr_bytes).hexdigest(),
                    stderr_byte_count=len(stderr_bytes),
                    containment_drained=True,
                )
                return ProjectStageProcessCompletion.create(
                    disposition="released_child",
                    process_evidence=evidence,
                    no_mutation_proof=None,
                    process_summary=summary,
                )
        finally:
            object.__setattr__(launch, "_raw_nonce", b"")

    @_guarded_qupath_process
    def run_project_evidence_script(
        self,
        *,
        project_file: Path,
        authority_layout: RetainedAuthorityLayoutV2,
        binding: RuntimeAuthorityBindingV2,
        project_lease: object,
        launch: QuPathMutationLaunchBinding,
    ) -> QuPathEvidenceRun:
        """Run one catalogue mutation while both authoritative locks stay inherited."""
        from .catalogue import (
            ProjectAuthorityError,
            ProjectMutationLease,
            QuPathCatalogueEvidenceEnvelope,
            QuPathCatalogueRequestEnvelope,
            QuPathIdentityInstallRequest,
            QuPathProcessStartToken,
            canonical_project_uri,
        )

        if not isinstance(project_lease, ProjectMutationLease) or not project_lease.is_live:
            raise QuPathExecutionError("live project mutation lease is required")
        if (
            not isinstance(authority_layout, RetainedAuthorityLayoutV2)
            or type(binding) is not RuntimeAuthorityBindingV2
            or binding.authority_layout_sha256 != authority_layout.layout.layout_sha256
        ):
            raise QuPathExecutionError("retained runtime authority binding is invalid")
        try:
            authority_layout.validate()
            script_bucket = authority_layout.bucket("immutable")
            exchange_bucket = authority_layout.bucket("exchange")
            script_bytes = script_bucket.read_member(binding.script_ref)
            request_bytes = exchange_bucket.read_member(binding.request_ref)
            if exchange_bucket.read_member(binding.evidence_ref, optional=True) is not None:
                raise QuPathExecutionError("project evidence authority member already exists")
            if type(script_bytes) is not bytes or type(request_bytes) is not bytes:
                raise QuPathExecutionError("project evidence authority inputs are unavailable")
            native_script = script_bucket.member_path(binding.script_ref)
            native_request = exchange_bucket.member_path(binding.request_ref)
            native_evidence = exchange_bucket.member_path(binding.evidence_ref)
        except DurableRecordError as exc:
            raise QuPathExecutionError("project evidence authority inputs are invalid") from exc
        if (
            not isinstance(launch, QuPathMutationLaunchBinding)
            or launch._issuer is not self
            or launch._project_lease is not project_lease
            or launch._consumed[0]
        ):
            raise QuPathExecutionError("project launch binding is foreign or consumed")
        try:
            supplied_project_uri = canonical_project_uri(Path(project_file))
        except ProjectAuthorityError as exc:
            raise QuPathExecutionError("project path does not match the mutation lease") from exc
        if supplied_project_uri != project_lease.designation.project_uri:
            raise QuPathExecutionError("project path does not match the mutation lease")
        native_project = project_lease.designation.project_path
        try:
            request_mapping = json.loads(request_bytes.decode("utf-8"))
            request = _catalogue_request_from_mapping(request_mapping)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise QuPathExecutionError("project catalogue request is invalid") from exc
        if (
            request.project_identity_sha256
            != project_lease.designation.identity.project_identity_sha256
            or request.fencing_token_sha256 != project_lease.fencing_token_sha256
            or request.launch_nonce_sha256 != launch.launch_nonce_sha256
        ):
            raise QuPathExecutionError("project request does not match launch authority")
        identity_payload = (
            request.payload if request.operation == "ensure_project_identity" else None
        )
        if identity_payload is not None and (
            type(identity_payload) is not QuPathIdentityInstallRequest
            or identity_payload.identity_record != project_lease.designation.identity
        ):
            raise QuPathExecutionError("project identity request does not match launch authority")
        try:
            active = project_lease._provider._validate_live_lease_authority(
                project_lease,
                require_project=False,
                validate_project_target=False,
            )
            catalogue_runtime_authority_binding_v2(
                authority_layout,
                workspace_storage_scope_sha256=(
                    project_lease._provider._workspace_storage_scope_sha256(project_lease)
                ),
                active_mutation_run_sha256=(
                    project_lease._provider._active_mutation_run_sha256(active)
                ),
                role=project_lease._provider._runtime_request_role(request),
                request_identity_sha256=request.request_envelope_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                binding=binding,
            )
        except (ROIAnalysisError, ProjectAuthorityError) as exc:
            raise QuPathExecutionError(
                "project runtime authority binding differs from launch context"
            ) from exc
        script_sha256 = hashlib.sha256(script_bytes).hexdigest()
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        create_reserved_project = bool(
            isinstance(project_lease, ProjectMutationLease)
            and identity_payload is not None
            and identity_payload.create_if_missing
            and project_lease.current_authority_head.materialization_state == "reserved"
            and project_lease.designation.identity.origin_kind == "created"
            and not project_lease._project_path_pin.exists_at_pin
        )
        command = (
            *self._command_prefix,
            "script",
            "--args",
            os.fspath(native_request),
            "--args",
            os.fspath(native_project),
            "--args",
            os.fspath(native_evidence),
            os.fspath(native_script),
        )
        launch._consumed[0] = True
        environment = native_compute_environment(self._launch_environment())
        environment["MIF_QUPATH_PROJECT_LAUNCH_NONCE"] = launch._raw_nonce.hex()
        descriptors = project_lease.authoritative_lock_descriptors
        drain_failure = _ProcessDrainFailureBinding()

        def validate_prelaunch_project_target() -> None:
            try:
                project_lease.validate_project_target(
                    require_exists=not create_reserved_project,
                    allow_initial_creation=False,
                )
            except ProjectAuthorityError as exc:
                raise QuPathExecutionError("project target changed before process launch") from exc

        try:
            with ExitStack() as retained:
                retained.enter_context(_preserve_process_drain_failure(drain_failure))
                for descriptor in descriptors:
                    retained.enter_context(retain_authoritative_lock_descriptor(descriptor))
                validate_prelaunch_project_target()
                containment_request = self._project_containment_request(
                    project_lease=project_lease,
                    application_request_sha256=request.request_envelope_sha256,
                    launch_nonce_sha256=launch.launch_nonce_sha256,
                    stdout_log_name=binding.stdout_ref.physical_name,
                    stderr_log_name=binding.stderr_ref.physical_name,
                    command_profile="headless-script",
                )
                preflight = getattr(self._executor, "preflight_containment", None)
                if not callable(preflight):
                    raise QuPathExecutionError("project executor lacks containment preflight")
                try:
                    preflight(
                        command,
                        launch_request=containment_request,
                        environment=environment,
                    )
                except QuPathExecutionError:
                    raise
                except Exception as exc:
                    raise QuPathExecutionError("project containment preflight failed") from exc
                validate_prelaunch_project_target()
                project_lease.record_process_spawn_attempt(
                    request,
                    containment_request=containment_request,
                    runtime_authority_binding=binding,
                )
                try:
                    (
                        record,
                        observed_token,
                        launch_evidence,
                        drain_evidence,
                        process_drain_sha256,
                    ) = self._execute_project_command(
                        command,
                        environment,
                        authority_layout=authority_layout,
                        binding=binding,
                        project_lease=project_lease,
                        launch_nonce_sha256=launch.launch_nonce_sha256,
                        containment_request=containment_request,
                    )
                    drain_failure.process_drain_sha256 = process_drain_sha256
                    drain_failure.record = record
                    drain_failure.process_start_token = observed_token
                    drain_failure.launch_evidence = launch_evidence
                    drain_failure.drain_evidence = drain_evidence
                except QuPathExecutionError as exc:
                    if exc.record is not None:
                        try:
                            project_lease.validate_project_target(
                                require_exists=True,
                                allow_initial_creation=False,
                            )
                        except ProjectAuthorityError as pin_exc:
                            raise QuPathExecutionError._with_containment_evidence(
                                "project target changed during execution",
                                exc.record,
                                process_start_token=exc.process_start_token,
                                launch_evidence=exc.launch_evidence,
                                drain_evidence=exc.drain_evidence,
                                process_drain_sha256=(exc.process_drain_sha256),
                            ) from pin_exc
                    raise
        finally:
            object.__setattr__(launch, "_raw_nonce", b"")
        with _preserve_process_drain_failure(drain_failure):
            try:
                authority_layout.validate()
                evidence_bytes = exchange_bucket.read_member(binding.evidence_ref, optional=True)
                if evidence_bytes is None:
                    raise QuPathExecutionError("QuPath did not publish project evidence", record)
            except DurableRecordError as exc:
                raise QuPathExecutionError(
                    "project evidence does not match the launch", record
                ) from exc
            try:
                script_after = script_bucket.read_member(binding.script_ref)
                request_after = exchange_bucket.read_member(binding.request_ref)
                if (
                    type(script_after) is not bytes
                    or type(request_after) is not bytes
                    or hashlib.sha256(script_after).hexdigest() != script_sha256
                    or hashlib.sha256(request_after).hexdigest() != request_sha256
                ):
                    raise ValueError("runtime input authority bytes changed")
            except (DurableRecordError, ValueError) as exc:
                raise QuPathExecutionError(
                    "project script or request changed during execution", record
                ) from exc
            try:
                logs = authority_layout.bucket("logs")
                stdout_bytes = logs.read_member(binding.stdout_ref)
                stderr_bytes = logs.read_member(binding.stderr_ref)
                if type(stdout_bytes) is not bytes or type(stderr_bytes) is not bytes:
                    raise ValueError("runtime log authority bytes changed")
                evidence_mapping = json.loads(evidence_bytes.decode("utf-8"))
                # Full owner validation is deliberately performed by the catalogue adapter.
                token_mapping = evidence_mapping.get("process_start_token")
                token = (
                    None
                    if token_mapping is None
                    else QuPathProcessStartToken.from_mapping(token_mapping)
                )
                if evidence_mapping.get("request_envelope_sha256") != (
                    request.request_envelope_sha256
                ):
                    raise ValueError("request digest mismatch")
                if evidence_mapping.get("launch_nonce_sha256") != (launch.launch_nonce_sha256):
                    raise ValueError("launch nonce mismatch")
            except (
                DurableRecordError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                raise QuPathExecutionError(
                    "project evidence does not match the launch", record
                ) from exc
            if token != observed_token:
                raise QuPathExecutionError(
                    "QuPath process start token does not match the spawned process",
                    record,
                )
            try:
                typed_evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(evidence_mapping)
                authorization = project_lease.authorize_project_file_successor(
                    request=request,
                    request_bytes=request_bytes,
                    evidence=typed_evidence,
                    evidence_bytes=evidence_bytes,
                    script_sha256=script_sha256,
                    runtime_binding=binding,
                    process_drain_sha256=process_drain_sha256,
                )
                project_lease.accept_project_file_successor(authorization)
            except ProjectAuthorityError as exc:
                raise QuPathExecutionError._with_containment_evidence(
                    "project successor changed or does not match " "authenticated evidence",
                    record,
                    process_start_token=observed_token,
                    launch_evidence=launch_evidence,
                    drain_evidence=drain_evidence,
                    process_drain_sha256=process_drain_sha256,
                ) from exc
            return QuPathEvidenceRun(
                record=record,
                evidence_path=native_evidence,
                evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
                script_sha256=script_sha256,
                request_sha256s=((native_request, request_sha256),),
                process_start_token=observed_token,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                launch_evidence=launch_evidence,
                drain_evidence=drain_evidence,
                process_drain_sha256=process_drain_sha256,
            )

    def _project_containment_request(
        self,
        *,
        project_lease: object,
        application_request_sha256: str | None = None,
        launch_nonce_sha256: str,
        stdout_log_name: str | None = None,
        stderr_log_name: str | None = None,
        command_profile: Literal["headless-script", "interactive-project"] = "headless-script",
        request: object | None = None,
        binding: RuntimeAuthorityBindingV2 | None = None,
    ) -> ContainmentLaunchRequest:
        from .catalogue import ProjectMutationLease, QuPathCatalogueRequestEnvelope

        if request is not None or binding is not None:
            if (
                not isinstance(request, QuPathCatalogueRequestEnvelope)
                or type(binding) is not RuntimeAuthorityBindingV2
                or application_request_sha256 is not None
                or stdout_log_name is not None
                or stderr_log_name is not None
                or command_profile != "headless-script"
            ):
                raise QuPathExecutionError("project containment authority is invalid")
            application_request_sha256 = request.request_envelope_sha256
            stdout_log_name = binding.stdout_ref.physical_name
            stderr_log_name = binding.stderr_ref.physical_name

        if (
            not isinstance(project_lease, ProjectMutationLease)
            or type(application_request_sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", application_request_sha256)
            or type(launch_nonce_sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", launch_nonce_sha256)
            or command_profile not in ("headless-script", "interactive-project")
            or type(stdout_log_name) is not str
            or type(stderr_log_name) is not str
        ):
            raise QuPathExecutionError("project containment authority is invalid")
        active = project_lease._provider._validate_live_lease_authority(
            project_lease,
            require_project=False,
        )
        active_sha256 = project_lease._provider._active_mutation_run_sha256(active)
        identity_sha256 = project_lease.designation.identity.project_identity_sha256
        authority = AuthorityBindingV1(
            launch_nonce_sha256=launch_nonce_sha256,
            application_request_sha256=application_request_sha256,
            project_identity_sha256=identity_sha256,
            active_mutation_run_sha256=active_sha256,
            containment_scope_sha256=identity_sha256,
        )
        if os.name == "nt":
            return ContainmentLaunchRequest(
                authority=authority,
                provider="windows-kill-on-close-job/v1",
                registration_id=None,
                command_profile=command_profile,
                windows_job_name=authoritative_job_name(
                    identity_sha256,
                    authority_kind="qupath-project",
                ),
                stdout_log_name=stdout_log_name,
                stderr_log_name=stderr_log_name,
            )
        if os.name != "posix" or sys.platform != "linux":
            raise QuPathExecutionError(
                "authoritative QuPath containment is unsupported on this platform"
            )
        expectation = self._containment_expectation
        if self._linux_containment_profile == "prototype-local-process-group":
            return ContainmentLaunchRequest(
                authority=authority,
                provider="linux-local-process-group/v1",
                registration_id=None,
                command_profile=command_profile,
                windows_job_name=None,
                stdout_log_name=stdout_log_name,
                stderr_log_name=stderr_log_name,
            )
        if expectation is None:
            raise QuPathExecutionError("Linux containment compatibility expectation is required")
        return ContainmentLaunchRequest(
            authority=authority,
            provider="linux-systemd-scope-broker/v1",
            registration_id=expectation.registration_id,
            command_profile=command_profile,
            windows_job_name=None,
            stdout_log_name=stdout_log_name,
            stderr_log_name=stderr_log_name,
        )

    def _authoritative_process_start_token(
        self,
        process_id: int,
        start_epoch_millis: int,
        launch_evidence: ProcessContainmentLaunchEvidence,
    ) -> object:
        """Bind a child to the native process-birth identity of its platform."""

        from .catalogue import QuPathProcessStartToken

        if type(process_id) is not int or process_id <= 0:
            raise QuPathExecutionError("contained process ID is invalid")
        if type(launch_evidence) is WindowsKillOnCloseJobAttachmentV1:
            if type(start_epoch_millis) is not int or start_epoch_millis <= 0:
                raise QuPathExecutionError("Windows process creation time is invalid")
            provider = "windows-process-creation-epoch-millis/v1"
            return QuPathProcessStartToken(
                provider=provider,
                process_id=process_id,
                start_epoch_millis=start_epoch_millis,
                canonical_token=f"{provider}:{process_id}:{start_epoch_millis}",
            )
        if type(launch_evidence) is LinuxSystemdScopeAttachmentV1:
            try:
                attachment = json.loads(launch_evidence.attachment_receipt_json_utf8)
                raw_ticks = attachment["gate_start_token"]
                if type(raw_ticks) is not str or re.fullmatch(r"[1-9][0-9]*", raw_ticks) is None:
                    raise ValueError("gate start token is not canonical")
                start_ticks = int(raw_ticks)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise QuPathExecutionError("Linux containment start ticks are invalid") from exc
        elif type(launch_evidence) is LinuxLocalProcessGroupAttachmentV1:
            start_ticks = -1
        else:
            raise QuPathExecutionError("contained process launch evidence is unsupported")
        observer = getattr(self._executor, "observe_linux_process_start_ticks", None)
        if callable(observer):
            observed_ticks = observer(process_id)
            if type(observed_ticks) is not int or observed_ticks <= 0:
                raise QuPathExecutionError("Linux process start ticks are invalid")
            if start_ticks >= 0 and observed_ticks != start_ticks:
                raise QuPathExecutionError("Linux process start ticks differ from containment")
            start_ticks = observed_ticks
        elif start_ticks < 0:
            raise QuPathExecutionError("local Linux process start ticks cannot be authenticated")
        provider = "linux-proc-start-ticks/v1"
        return QuPathProcessStartToken(
            provider=provider,
            process_id=process_id,
            start_ticks=start_ticks,
            canonical_token=f"{provider}:{process_id}:{start_ticks}",
        )

    def _execute_project_command(
        self,
        command: tuple[str, ...],
        environment: dict[str, str],
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        binding: RuntimeAuthorityBindingV2,
        project_lease: object,
        launch_nonce_sha256: str,
        containment_request: ContainmentLaunchRequest,
    ) -> tuple[
        QuPathRunRecord,
        object,
        ProcessContainmentLaunchEvidence,
        ProcessContainmentDrainEvidence,
        str,
    ]:
        from .catalogue import ProjectMutationLease, QuPathProcessStartToken

        contained = getattr(self._executor, "run_contained", None)
        if not callable(contained):
            raise QuPathExecutionError("project executor lacks parent-death containment")
        started = time.monotonic()
        observed: list[tuple[QuPathProcessStartToken, str]] = []
        attached: list[tuple[QuPathProcessStartToken, ProcessContainmentLaunchEvidence]] = []

        def observe_process_start(
            process_id: int,
            start_epoch_millis: int,
            launch_evidence: ProcessContainmentLaunchEvidence,
        ) -> str:
            if attached:
                raise QuPathExecutionError(
                    "contained executor reported more than one process start token"
                )
            token = self._authoritative_process_start_token(
                process_id,
                start_epoch_millis,
                launch_evidence,
            )
            assert isinstance(token, QuPathProcessStartToken)
            if not isinstance(project_lease, ProjectMutationLease):
                raise QuPathExecutionError("project process launch lease is foreign")
            # Keep the independently observed child identity even when publication
            # fails before the backend releases the child to execute the script.
            attached.append((token, launch_evidence))
            launch_record = project_lease.record_process_launch(
                token,
                launch_nonce_sha256=launch_nonce_sha256,
                launch_evidence=launch_evidence,
                environment=environment,
            )
            observed.append((token, launch_record.process_launch_sha256))
            return launch_record.process_launch_sha256

        log_bucket = authority_layout.bucket("logs")
        stdout_path = log_bucket.member_path(binding.stdout_ref)
        stderr_path = log_bucket.member_path(binding.stderr_ref)
        drain_failure = _ProcessDrainFailureBinding()
        try:
            with _preserve_process_drain_failure(drain_failure):
                with log_bucket.reserve_log_pair(
                    binding.stdout_ref,
                    binding.stderr_ref,
                ) as reserved_logs:
                    completed = contained(
                        command,
                        lock_descriptors=authoritative_lock_descriptors(),
                        launch_request=containment_request,
                        timeout_seconds=_authoritative_timeout_seconds(),
                        stdout_log=reserved_logs.stdout,
                        stderr_log=reserved_logs.stderr,
                        environment=environment,
                        process_start_observer=observe_process_start,
                    )
                    if not isinstance(completed, ContainedProcessResult):
                        raise QuPathExecutionError(
                            "project executor returned invalid containment evidence"
                        )
                    if len(observed) != 1:
                        raise QuPathExecutionError(
                            "project executor did not provide a process start token"
                        )
                    observed_token, process_launch_sha256 = observed[0]
                    terminal_status_sha256 = hashlib.sha256(
                        json.dumps(
                            {
                                "process_start_token": observed_token.to_mapping(),
                                "returncode": completed.returncode,
                                "termination_reason": (completed.drain_evidence.termination_reason),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    process_drain = project_lease.record_process_drain(
                        observed_token,
                        launch_nonce_sha256=launch_nonce_sha256,
                        terminal_status_sha256=terminal_status_sha256,
                        drain_evidence=completed.drain_evidence,
                        process_launch_sha256=process_launch_sha256,
                    )
                    drain_failure.process_drain_sha256 = process_drain.process_drain_sha256
                    drain_failure.process_start_token = observed_token
                    drain_failure.launch_evidence = completed.launch_evidence
                    drain_failure.drain_evidence = completed.drain_evidence
                    record = QuPathRunRecord(
                        command=command,
                        executable_version=self._executable_version(),
                        exit_status=completed.returncode,
                        stdout_log=stdout_path,
                        stderr_log=stderr_path,
                        runtime_seconds=time.monotonic() - started,
                    )
                    drain_failure.record = record
                    if completed.returncode != 0:
                        raise QuPathExecutionError._with_containment_evidence(
                            f"QuPath script failed with exit status {completed.returncode}",
                            record,
                            process_start_token=observed_token,
                            launch_evidence=completed.launch_evidence,
                            drain_evidence=completed.drain_evidence,
                            process_drain_sha256=(process_drain.process_drain_sha256),
                        )
                return (
                    record,
                    observed_token,
                    completed.launch_evidence,
                    completed.drain_evidence,
                    process_drain.process_drain_sha256,
                )
        except _ContainedProcessError as backend_error:
            failure = backend_error.failure
            process_token = None
            process_launch_sha256 = None
            if observed:
                observed_token, process_launch_sha256 = observed[0]
                process_token = observed_token
                if failure.process_id != observed_token.process_id or (
                    observed_token.provider == "windows-process-creation-epoch-millis/v1"
                    and failure.start_epoch_millis != observed_token.start_epoch_millis
                ):
                    process_token = None
                    process_launch_sha256 = None
            elif len(attached) == 1 and failure.drain_evidence is not None:
                attached_token, attached_evidence = attached[0]
                if (
                    failure.drain_evidence.termination_reason == "launch_publication_failed"
                    and failure.process_id == attached_token.process_id
                    and failure.launch_evidence == attached_evidence
                    and (
                        attached_token.provider != "windows-process-creation-epoch-millis/v1"
                        or failure.start_epoch_millis == attached_token.start_epoch_millis
                    )
                ):
                    process_token = attached_token
            process_drain_sha256 = None
            translation_cause: BaseException = backend_error
            if failure.drain_evidence is not None and process_token is not None:
                terminal_status_sha256 = hashlib.sha256(
                    json.dumps(
                        {
                            "process_start_token": process_token.to_mapping(),
                            "returncode": failure.returncode,
                            "termination_reason": (failure.drain_evidence.termination_reason),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                try:
                    process_drain = project_lease.record_process_drain(
                        process_token,
                        launch_nonce_sha256=launch_nonce_sha256,
                        terminal_status_sha256=terminal_status_sha256,
                        drain_evidence=failure.drain_evidence,
                        process_launch_sha256=process_launch_sha256,
                    )
                    process_drain_sha256 = process_drain.process_drain_sha256
                except BaseException as publication_error:
                    publication_error.add_note(
                        "backend had already proven containment drain: " f"{backend_error!r}"[:512]
                    )
                    translation_cause = publication_error
            error = QuPathExecutionError._with_containment_evidence(
                "QuPath contained process failed",
                process_start_token=process_token,
                launch_evidence=failure.launch_evidence,
                drain_evidence=failure.drain_evidence,
                process_drain_sha256=process_drain_sha256,
            )
            raise error from translation_cause
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError._with_containment_evidence(
                "QuPath contained process failed before attachment",
                process_start_token=None,
                launch_evidence=None,
                drain_evidence=None,
                process_drain_sha256=None,
            ) from exc

    @_guarded_qupath_process
    def run_script(
        self,
        project: ProjectLayout,
        script: str | Path,
        config_path: str | Path,
    ) -> QuPathRunRecord:
        """Run a project script and capture stdout/stderr under project data."""
        native_script = os.fspath(script)
        native_config = os.fspath(config_path)
        command = (
            *self._command_prefix,
            "script",
            "--project",
            os.fspath(project.project_file),
            "--args",
            native_config,
            "--save",
            native_script,
        )
        executable_version = self._executable_version()
        started = time.monotonic()
        try:
            completed = self._executor(command, environment=native_compute_environment(self._launch_environment()))
        except OSError as exc:
            raise QuPathExecutionError(f"unable to execute QuPath: {self._executable}") from exc
        runtime_seconds = time.monotonic() - started
        stdout_log = project.data_dir / f"{Path(native_script).stem}.stdout.log"
        stderr_log = project.data_dir / f"{Path(native_script).stem}.stderr.log"
        _write_log(stdout_log, completed.stdout or "")
        _write_log(stderr_log, completed.stderr or "")
        record = QuPathRunRecord(
            command=command,
            executable_version=executable_version,
            exit_status=completed.returncode,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            runtime_seconds=runtime_seconds,
        )
        if completed.returncode != 0:
            raise QuPathExecutionError(
                f"QuPath script failed with exit status {completed.returncode}",
                record,
            )
        return record

    @_guarded_qupath_process
    def run_evidence_script(
        self,
        *,
        project_file: str | Path,
        script: str | Path,
        request_paths: Sequence[str | Path],
        log_directory: str | Path,
        require_existing_project: bool,
        evidence_path: str | Path,
    ) -> QuPathEvidenceRun:
        """Run a generic script and bind one newly published evidence file.

        This transport owns no stage schema. Domain code prepares and validates
        typed request/evidence, so dedicated multi-image projects do not need
        the ROI-analysis exchange envelope.
        """
        native_project = Path(project_file).resolve()
        native_script = Path(script).resolve()
        native_evidence = Path(evidence_path).resolve()
        native_log_directory = Path(log_directory).resolve()
        if require_existing_project and not native_project.is_file():
            raise QuPathExecutionError(f"QuPath project does not exist: {native_project}")
        if not native_script.is_file():
            raise QuPathExecutionError(f"required QuPath script is missing: {native_script}")
        if native_evidence.exists():
            raise QuPathExecutionError(
                f"QuPath evidence destination already exists: {native_evidence}"
            )
        native_requests = tuple(Path(item).resolve() for item in request_paths)
        if not native_requests or any(not item.is_file() for item in native_requests):
            raise QuPathExecutionError("QuPath evidence requests must be existing files")
        script_sha256 = _file_sha256(native_script)
        request_sha256s = tuple((item, _file_sha256(item)) for item in native_requests)
        argument_values = tuple(os.fspath(item) for item in native_requests) + (
            os.fspath(native_evidence),
        )
        argument_tokens = tuple(token for item in argument_values for token in ("--args", item))
        command = (*self._command_prefix, "script")
        if require_existing_project:
            command += ("--project", os.fspath(native_project))
        command += argument_tokens + (os.fspath(native_script),)
        record = self._execute_script_command(
            command,
            native_log_directory,
            native_script.stem,
        )
        if not native_evidence.is_file():
            raise QuPathExecutionError("QuPath did not publish the requested evidence", record)
        digest_before = _file_sha256(native_evidence)
        if _file_sha256(native_script) != script_sha256 or any(
            _file_sha256(path) != digest for path, digest in request_sha256s
        ):
            raise QuPathExecutionError(
                "QuPath script or request changed during evidence production", record
            )
        digest_after = _file_sha256(native_evidence)
        if digest_before != digest_after:
            raise QuPathExecutionError("QuPath evidence changed while it was being hashed", record)
        return QuPathEvidenceRun(
            record,
            native_evidence,
            digest_after,
            script_sha256,
            request_sha256s,
        )

    @_guarded_qupath_process
    def run_authority_evidence_script(
        self,
        *,
        project_file: Path,
        authority_layout: RetainedAuthorityLayoutV2,
        binding: RuntimeAuthorityBindingV2,
        require_existing_project: bool,
    ) -> QuPathEvidenceRun:
        """Run one read-only evidence script through exact retained V2 refs."""

        if (
            not isinstance(authority_layout, RetainedAuthorityLayoutV2)
            or type(binding) is not RuntimeAuthorityBindingV2
            or binding.authority_layout_sha256 != authority_layout.layout.layout_sha256
        ):
            raise QuPathExecutionError("retained runtime authority binding is invalid")
        native_project = Path(project_file).resolve(strict=False)
        if require_existing_project and not native_project.is_file():
            raise QuPathExecutionError(f"QuPath project does not exist: {native_project}")
        try:
            authority_layout.validate()
            immutable = authority_layout.bucket("immutable")
            exchange = authority_layout.bucket("exchange")
            logs = authority_layout.bucket("logs")
            script_bytes = immutable.read_member(binding.script_ref)
            request_bytes = exchange.read_member(binding.request_ref)
            if exchange.read_member(binding.evidence_ref, optional=True) is not None:
                raise QuPathExecutionError("QuPath evidence authority member already exists")
            if type(script_bytes) is not bytes or type(request_bytes) is not bytes:
                raise QuPathExecutionError("QuPath evidence inputs are unavailable")
            native_script = immutable.member_path(binding.script_ref)
            native_request = exchange.member_path(binding.request_ref)
            native_evidence = exchange.member_path(binding.evidence_ref)
            stdout_path = logs.member_path(binding.stdout_ref)
            stderr_path = logs.member_path(binding.stderr_ref)
        except DurableRecordError as exc:
            raise QuPathExecutionError("QuPath evidence authority inputs are invalid") from exc
        command = (
            *self._command_prefix,
            "script",
            "--args",
            os.fspath(native_request),
            "--args",
            os.fspath(native_project),
            "--args",
            os.fspath(native_evidence),
            os.fspath(native_script),
        )
        started = time.monotonic()
        execute_with_streams = getattr(self._executor, "run_with_streams", None)
        if not callable(execute_with_streams):
            raise QuPathExecutionError("QuPath executor lacks direct binary log transport")
        try:
            with logs.reserve_log_pair(binding.stdout_ref, binding.stderr_ref) as reserved_logs:
                try:
                    completed = execute_with_streams(
                        command,
                        stdout_log=reserved_logs.stdout,
                        stderr_log=reserved_logs.stderr,
                        environment=native_compute_environment(self._launch_environment()),
                    )
                except subprocess.TimeoutExpired as exc:
                    try:
                        _finalize_contained_logs(reserved_logs.stdout, reserved_logs.stderr)
                    except QuPathExecutionError as log_error:
                        exc.add_note(
                            "read-only QuPath timeout log finalization also "
                            f"failed: {log_error!r}"[:512]
                        )
                    raise QuPathExecutionError("read-only QuPath script timed out") from exc
                if not isinstance(completed, subprocess.CompletedProcess):
                    raise QuPathExecutionError("QuPath executor returned an invalid result")
                _finalize_contained_logs(reserved_logs.stdout, reserved_logs.stderr)
        except QuPathExecutionError:
            raise
        except OSError as exc:
            raise QuPathExecutionError(f"unable to execute QuPath: {self._executable}") from exc
        record = QuPathRunRecord(
            command=command,
            executable_version=self._executable_version(),
            exit_status=completed.returncode,
            stdout_log=stdout_path,
            stderr_log=stderr_path,
            runtime_seconds=time.monotonic() - started,
        )
        if completed.returncode != 0:
            raise QuPathExecutionError(
                f"QuPath script failed with exit status {completed.returncode}",
                record,
            )
        try:
            evidence_bytes = exchange.read_member(binding.evidence_ref)
            script_after = immutable.read_member(binding.script_ref)
            request_after = exchange.read_member(binding.request_ref)
            stdout_after = logs.read_member(binding.stdout_ref)
            stderr_after = logs.read_member(binding.stderr_ref)
        except DurableRecordError as exc:
            raise QuPathExecutionError(
                "QuPath authority evidence could not be reopened", record
            ) from exc
        if (
            any(
                type(value) is not bytes
                for value in (
                    evidence_bytes,
                    script_after,
                    request_after,
                    stdout_after,
                    stderr_after,
                )
            )
            or script_after != script_bytes
            or request_after != request_bytes
        ):
            raise QuPathExecutionError("QuPath authority evidence changed during execution", record)
        return QuPathEvidenceRun(
            record=record,
            evidence_path=native_evidence,
            evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            script_sha256=hashlib.sha256(script_bytes).hexdigest(),
            request_sha256s=((native_request, hashlib.sha256(request_bytes).hexdigest()),),
        )

    @_guarded_qupath_process
    def setup_project(self, project: ProjectLayout) -> QuPathRunRecord:
        """Create/open the real QuPath project and add its canonical image entry."""
        script = script_path(project, QuPathScriptAsset.SETUP_PROJECT)
        evidence_path = project.data_dir / "canonical-image.evidence.json"
        try:
            config = json.loads(project.config_json)
            canonical_path = Path(config["input_image"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise QuPathExecutionError(
                "project configuration lacks the canonical image path"
            ) from exc
        if not (canonical_path.is_file() or (canonical_path / "zarr.json").is_file()):
            raise QuPathExecutionError(f"canonical image does not exist: {canonical_path}")
        canonical_image_sha256 = _file_sha256(canonical_path)
        command = (
            *self._command_prefix,
            "script",
            "--args",
            os.fspath(project.config_path),
            "--args",
            os.fspath(project.project_file),
            "--args",
            os.fspath(evidence_path),
            "--args",
            canonical_image_sha256,
            os.fspath(script),
        )
        return self._execute_script_command(command, project.data_dir, script.stem)

    @_guarded_qupath_process
    def canonical_image_evidence(self, project: ProjectLayout) -> dict[str, object]:
        """Read exact canonical project-entry identity written by project setup."""
        path = project.data_dir / "canonical-image.evidence.json"
        value = _read_json_object(path, "canonical-image project evidence")
        expected = {
            "schema_version",
            "project_file",
            "project_entry_id",
            "project_entry_path",
            "canonical_image_sha256",
            "image_path",
            "entry_name",
            "physical_calibration",
            "native_pyramid_levels",
        }
        if set(value) != expected or value["schema_version"] != "qupath-canonical-image/v3":
            raise QuPathExecutionError("canonical-image project evidence schema is invalid")
        if value["project_file"] != str(project.project_file.resolve()):
            raise QuPathExecutionError("canonical-image evidence targets another project")
        if not isinstance(value["project_entry_id"], str) or not value["project_entry_id"]:
            raise QuPathExecutionError("canonical-image project_entry_id is invalid")
        canonical_sha256 = value["canonical_image_sha256"]
        if (
            not isinstance(canonical_sha256, str)
            or len(canonical_sha256) != 64
            or any(character not in "0123456789abcdef" for character in canonical_sha256)
        ):
            raise QuPathExecutionError("canonical-image canonical_image_sha256 is invalid")
        if (
            not isinstance(value["project_entry_path"], str)
            or not Path(value["project_entry_path"]).is_absolute()
        ):
            raise QuPathExecutionError("canonical-image project_entry_path is invalid")
        calibration = value["physical_calibration"]
        if not _is_valid_physical_calibration_evidence(calibration):
            raise QuPathExecutionError("canonical-image physical calibration is invalid")
        levels = value["native_pyramid_levels"]
        if not isinstance(levels, list) or not levels:
            raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
        expected_level_fields = {
            "native_resolution_index",
            "x_downsample",
            "y_downsample",
            "x_um_per_px",
            "y_um_per_px",
        }
        previous: dict[str, object] | None = None
        base_x_um: float | None = None
        base_y_um: float | None = None
        for index, item in enumerate(levels):
            if not isinstance(item, dict) or set(item) != expected_level_fields:
                raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            if item["native_resolution_index"] != index:
                raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            for field in expected_level_fields - {"native_resolution_index"}:
                number = item[field]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not _finite_positive(float(number))
                ):
                    raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            if abs(float(item["x_downsample"]) - float(item["y_downsample"])) > 1e-9:
                raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            if index == 0:
                if (
                    abs(float(item["x_downsample"]) - 1.0) > 1e-9
                    or abs(float(item["y_downsample"]) - 1.0) > 1e-9
                ):
                    raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
                base_x_um = float(item["x_um_per_px"])
                base_y_um = float(item["y_um_per_px"])
            assert base_x_um is not None and base_y_um is not None
            expected_x_um = base_x_um * float(item["x_downsample"])
            expected_y_um = base_y_um * float(item["y_downsample"])
            if abs(float(item["x_um_per_px"]) - expected_x_um) > max(
                1e-9, expected_x_um * 1e-9
            ) or abs(float(item["y_um_per_px"]) - expected_y_um) > max(1e-9, expected_y_um * 1e-9):
                raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            if previous is not None and any(
                float(item[field]) <= float(previous[field])
                for field in expected_level_fields - {"native_resolution_index"}
            ):
                raise QuPathExecutionError("canonical-image native pyramid evidence is invalid")
            previous = item
        return value

    @_guarded_qupath_process
    def add_moving_image(
        self,
        project: ProjectLayout,
        image_path: str | Path,
        *,
        image_id: str,
        image_sha256: str,
        moving_modality: str,
        source_metadata: dict[str, object],
    ) -> dict[str, object]:
        """Add one exact, physically calibrated moving-modality project entry."""
        moving = Path(image_path).expanduser().resolve()
        if not moving.is_file():
            raise QuPathExecutionError(f"moving image does not exist: {moving}")
        if not isinstance(image_id, str) or not image_id.strip():
            raise QuPathExecutionError("moving image ID must be non-empty")
        if not isinstance(moving_modality, str) or not moving_modality.strip():
            raise QuPathExecutionError("moving modality must be non-empty")
        if not isinstance(source_metadata, dict):
            raise QuPathExecutionError("moving image source_metadata must be a mapping")
        try:
            stable_source_metadata = json.loads(
                json.dumps(source_metadata, allow_nan=False, sort_keys=True)
            )
        except (TypeError, ValueError) as exc:
            raise QuPathExecutionError(
                "moving image source_metadata must contain finite JSON values"
            ) from exc
        actual_sha = _file_sha256(moving)
        if image_sha256 != actual_sha:
            raise QuPathExecutionError("moving image checksum changed")
        selected_series = stable_source_metadata.get("source_series_selection")
        if selected_series is not None:
            from dataclasses import asdict
            from mif_pipeline.io import inspect_source_series_inventory, bind_source_series_selection
            if not isinstance(selected_series, dict) or any(
                type(selected_series.get(field)) is not int or selected_series[field] < 0
                for field in ("source_image_index", "source_series_index")
            ):
                raise QuPathExecutionError("The paired image requires an inspected image and series selection.")
            inventory = inspect_source_series_inventory(moving, require_planar_channels=False)
            candidates = tuple(item for item in inventory.candidates
                if item.eligibility == "eligible" and item.image_id == image_id
                and item.source_image_index == selected_series.get("source_image_index")
                and item.source_series_index == selected_series.get("source_series_index"))
            if len(candidates) != 1:
                raise QuPathExecutionError("The selected paired image is no longer available. Inspect it again.")
            candidate = candidates[0]
            selection = bind_source_series_selection(inventory, candidate_id=candidate.candidate_id)
            observed = {**asdict(selection), "source_file_sha256": inventory.source_identity.file_sha256,
                        "size_x": candidate.size_x, "size_y": candidate.size_y, "size_c": candidate.size_c}
            if observed != selected_series or observed["source_file_sha256"] != actual_sha:
                raise QuPathExecutionError("The selected paired image changed. Inspect it again.")
        directory = project.data_dir / "moving-images"
        directory.mkdir(parents=True, exist_ok=True)
        selection_digest = hashlib.sha256(json.dumps(selected_series, sort_keys=True).encode()).hexdigest()[:16] if selected_series is not None else None
        request_key = f"{actual_sha}-{selection_digest}" if selection_digest else actual_sha
        request_path = directory / f"{request_key}.request.json"
        evidence_path = directory / f"{request_key}.evidence.json"
        request = {
            "schema_version": "qupath-moving-image-request/v1",
            "project_file": str(project.project_file.resolve()),
            "image_path": str(moving),
            "image_id": image_id,
            "image_sha256": actual_sha,
            "entry_name": f"Moving image {moving_modality.strip()} {actual_sha[:12]}",
            "hierarchy_role": "reference_only",
            "moving_modality": moving_modality.strip(),
            "source_metadata": stable_source_metadata,
        }
        _write_json_once(request_path, request)
        script = script_path(project, QuPathScriptAsset.ADD_MOVING_IMAGE)
        if not script.is_file():
            raise QuPathExecutionError(f"QuPath moving-image script is missing: {script}")
        command = (
            *self._command_prefix,
            "script",
            "--project",
            os.fspath(project.project_file),
            "--args",
            os.fspath(request_path),
            "--args",
            os.fspath(evidence_path),
            os.fspath(script),
        )
        self._execute_script_command(command, directory, script.stem)
        if not evidence_path.is_file():
            raise QuPathExecutionError("QuPath did not write moving-image evidence")
        return _read_moving_image_evidence(evidence_path, request)

    @_guarded_qupath_process
    def import_warpy_registration(
        self,
        project: ProjectLayout,
        request: dict[str, object],
    ) -> dict[str, object]:
        """Load and evaluate one Warpy transform for exact project entry IDs."""
        validate_transform_reader_request(request.get("transform_reader"))
        registration_id = _request_identifier(
            request,
            schema="qupath-warpy-registration-request/v2",
            field="registration_id",
        )
        directory = project.data_dir / "registration" / _identifier_digest(registration_id)
        return self._run_evidence_script(
            project,
            request,
            directory=directory,
            request_name="warpy-registration.request.json",
            evidence_name="warpy-registration.evidence.json",
            asset=QuPathScriptAsset.IMPORT_WARPY_REGISTRATION,
            missing_message="QuPath did not write Warpy registration evidence",
            reader=_read_registration_evidence,
        )

    @_guarded_qupath_process
    def transfer_warpy_objects(
        self,
        project: ProjectLayout,
        request: dict[str, object],
    ) -> dict[str, object]:
        """Transform and independently QC requested objects before hierarchy import."""
        validate_transform_reader_request(request.get("transform_reader"))
        _request_identifier(
            request,
            schema="qupath-warpy-transfer-request/v2",
            field="project_file",
        )
        canonical = json.dumps(request, allow_nan=False, sort_keys=True, separators=(",", ":"))
        directory = (
            project.data_dir
            / "registration-transfers"
            / hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        return self._run_evidence_script(
            project,
            request,
            directory=directory,
            request_name="warpy-transfer.request.json",
            evidence_name="warpy-transfer.evidence.json",
            asset=QuPathScriptAsset.TRANSFER_WARPY_OBJECTS,
            missing_message="QuPath did not write Warpy transfer evidence",
            reader=_read_transfer_evidence,
        )

    @_guarded_qupath_process
    def commit_warpy_objects(
        self,
        project: ProjectLayout,
        request: dict[str, object],
        *,
        containment_recorder: ContainedMutationRecorder,
    ) -> dict[str, object]:
        """Commit only transfers already validated into typed Python records."""
        validate_transform_reader_request(request.get("transform_reader"))
        _request_identifier(
            request,
            schema="qupath-warpy-transfer-commit-request/v2",
            field="project_file",
        )
        containment_context = _warpy_containment_context(project, request, operation="commit")
        canonical = json.dumps(request, allow_nan=False, sort_keys=True, separators=(",", ":"))
        directory = (
            project.data_dir
            / "registration-transfer-commits"
            / hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        return self._run_evidence_script(
            project,
            request,
            directory=directory,
            request_name="warpy-transfer-commit.request.json",
            evidence_name="warpy-transfer-commit.evidence.json",
            asset=QuPathScriptAsset.COMMIT_WARPY_OBJECTS,
            missing_message="QuPath did not write Warpy transfer-commit evidence",
            reader=_read_transfer_commit_evidence,
            containment_recorder=containment_recorder,
            containment_context=containment_context,
        )

    @_guarded_qupath_process
    def cleanup_warpy_objects(
        self,
        project: ProjectLayout,
        request: dict[str, object],
        *,
        containment_recorder: ContainedMutationRecorder,
    ) -> dict[str, object]:
        """Remove only exact targets owned by one indeterminate transfer attempt."""
        _request_identifier(
            request,
            schema="qupath-warpy-transfer-cleanup-request/v1",
            field="project_file",
        )
        cleanup_lease = request.get("mutation_lease")
        cleanup_operation = cleanup_lease.get("operation") if type(cleanup_lease) is dict else None
        containment_context = _warpy_containment_context(
            project,
            request,
            operation=cleanup_operation,  # type: ignore[arg-type]
        )
        canonical = json.dumps(request, allow_nan=False, sort_keys=True, separators=(",", ":"))
        directory = (
            project.data_dir
            / "registration-transfer-cleanups"
            / hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        return self._run_evidence_script(
            project,
            request,
            directory=directory,
            request_name="warpy-transfer-cleanup.request.json",
            evidence_name="warpy-transfer-cleanup.evidence.json",
            asset=QuPathScriptAsset.CLEANUP_WARPY_OBJECTS,
            missing_message="QuPath did not write Warpy transfer-cleanup evidence",
            reader=_read_transfer_cleanup_evidence,
            containment_recorder=containment_recorder,
            containment_context=containment_context,
        )

    def _run_evidence_script(
        self,
        project: ProjectLayout,
        request: dict[str, object],
        *,
        directory: Path,
        request_name: str,
        evidence_name: str,
        asset: QuPathScriptAsset,
        missing_message: str,
        reader: Callable[[Path, dict[str, object]], dict[str, object]],
        containment_recorder: ContainedMutationRecorder | None = None,
        containment_context: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        request_path = directory / request_name
        evidence_path = directory / evidence_name
        _write_json_once(request_path, request)
        script = script_path(project, asset)
        if not script.is_file():
            raise QuPathExecutionError(f"required QuPath script is missing: {script}")
        command = (
            *self._command_prefix,
            "script",
            "--project",
            os.fspath(project.project_file),
            "--args",
            os.fspath(request_path),
            "--args",
            os.fspath(evidence_path),
            os.fspath(script),
        )
        if containment_recorder is None:
            if containment_context is not None:
                raise QuPathExecutionError("Warpy containment context is orphaned")
            self._execute_script_command(command, directory, script.stem)
        else:
            if containment_context is None:
                raise QuPathExecutionError("Warpy containment context is missing")
            self._execute_warpy_contained_command(
                command,
                directory,
                script.stem,
                containment_recorder=containment_recorder,
                containment_context=containment_context,
            )
        if not evidence_path.is_file():
            raise QuPathExecutionError(missing_message)
        digest_before = _file_sha256(evidence_path)
        value = reader(evidence_path, request)
        digest_after = _file_sha256(evidence_path)
        if digest_before != digest_after:
            raise QuPathExecutionError("QuPath evidence changed while it was being validated")
        request_sha256 = hashlib.sha256(
            json.dumps(request, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        _write_json_atomic(
            evidence_path.with_name(f"{evidence_path.stem}.sha256.json"),
            {
                "schema_version": "qupath-evidence-descriptor/v1",
                "project_file": str(project.project_file.resolve()),
                "script_asset": asset.value,
                "request_sha256": request_sha256,
                "evidence_sha256": digest_after,
            },
        )
        return value

    def _warpy_containment_request(
        self,
        *,
        context: Mapping[str, str],
        launch_nonce_sha256: str,
        stdout_name: str,
        stderr_name: str,
    ) -> ContainmentLaunchRequest:
        authority = AuthorityBindingV1(
            launch_nonce_sha256=launch_nonce_sha256,
            application_request_sha256=context["request_sha256"],
            project_identity_sha256=context["project_identity_sha256"],
            active_mutation_run_sha256=context["mutation_run_sha256"],
            containment_scope_sha256=context["hierarchy_scope_sha256"],
        )
        if os.name == "nt":
            return ContainmentLaunchRequest(
                authority=authority,
                provider="windows-kill-on-close-job/v1",
                registration_id=None,
                command_profile="headless-script",
                windows_job_name=authoritative_job_name(context["hierarchy_scope_sha256"]),
                stdout_log_name=stdout_name,
                stderr_log_name=stderr_name,
            )
        if os.name != "posix" or sys.platform != "linux":
            raise QuPathExecutionError(
                "authoritative QuPath containment is unsupported on this platform"
            )
        if self._linux_containment_profile == "prototype-local-process-group":
            return ContainmentLaunchRequest(
                authority=authority,
                provider="linux-local-process-group/v1",
                registration_id=None,
                command_profile="headless-script",
                windows_job_name=None,
                stdout_log_name=stdout_name,
                stderr_log_name=stderr_name,
            )
        expectation = self._containment_expectation
        if expectation is None:
            raise QuPathExecutionError("Linux containment compatibility expectation is required")
        return ContainmentLaunchRequest(
            authority=authority,
            provider="linux-systemd-scope-broker/v1",
            registration_id=expectation.registration_id,
            command_profile="headless-script",
            windows_job_name=None,
            stdout_log_name=stdout_name,
            stderr_log_name=stderr_name,
        )

    def _execute_warpy_contained_command(
        self,
        command: tuple[str, ...],
        log_directory: Path,
        log_stem: str,
        *,
        containment_recorder: ContainedMutationRecorder,
        containment_context: Mapping[str, str],
    ) -> QuPathRunRecord:
        if not isinstance(containment_recorder, ContainedMutationRecorder):
            raise QuPathExecutionError("Warpy containment recorder is invalid")
        contained = getattr(self._executor, "run_contained", None)
        if not callable(contained):
            raise QuPathExecutionError(
                "authoritative QuPath executor lacks parent-death containment"
            )
        try:
            binding = containment_recorder.launch_binding(**containment_context)
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise QuPathExecutionError("Warpy containment binding was refused") from exc
        if type(binding) is not ContainedMutationLaunchBinding or binding.to_mapping() != dict(
            containment_context
        ):
            raise QuPathExecutionError("Warpy containment binding is foreign")
        stdout_name = f"{log_stem}.stdout.log"
        stderr_name = f"{log_stem}.stderr.log"
        raw_nonce = os.urandom(32)
        containment_request = self._warpy_containment_request(
            context=containment_context,
            launch_nonce_sha256=hashlib.sha256(raw_nonce).hexdigest(),
            stdout_name=stdout_name,
            stderr_name=stderr_name,
        )
        environment = native_compute_environment(self._launch_environment())
        environment["MIF_QUPATH_PROJECT_LAUNCH_NONCE"] = raw_nonce.hex()
        preflight = getattr(self._executor, "preflight_containment", None)
        if not callable(preflight):
            raise QuPathExecutionError("authoritative QuPath executor lacks containment preflight")
        try:
            preflight(
                command,
                launch_request=containment_request,
                environment=environment,
            )
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise QuPathExecutionError("Warpy containment spawn was refused") from exc
        attempts: list[ContainedMutationSpawnAttempt] = []
        launched: list[ContainedMutationLaunchRecord] = []
        drains: list[ContainedMutationDrainRecord] = []

        def observe_process_attempt() -> None:
            if attempts:
                raise QuPathExecutionError(
                    "Warpy containment reported more than one process attempt"
                )
            attempt = containment_recorder.record_spawn_attempt(binding)
            if type(attempt) is not ContainedMutationSpawnAttempt or attempt.binding is not binding:
                raise QuPathExecutionError("Warpy containment spawn attempt is foreign")
            attempts.append(attempt)

        def observe_process_start(
            _process_id: int,
            _start_epoch_millis: int,
            evidence: ProcessContainmentLaunchEvidence,
        ) -> str:
            if launched or len(attempts) != 1:
                raise QuPathExecutionError(
                    "Warpy containment process launch lacks one exact attempt"
                )
            launch = containment_recorder.record_launch(attempts[0], evidence)
            if (
                type(launch) is not ContainedMutationLaunchRecord
                or launch.spawn_attempt is not attempts[0]
                or launch.evidence is not evidence
            ):
                raise QuPathExecutionError("Warpy containment launch is foreign")
            launched.append(launch)
            return _canonical_json_sha256(launch.to_mapping())

        def observe_process_drain(
            evidence: ProcessContainmentDrainEvidence,
        ) -> None:
            if drains or len(attempts) != 1:
                raise QuPathExecutionError(
                    "Warpy containment process drain lacks one exact attempt"
                )
            predecessor: ContainedMutationLaunchRecord | ContainedMutationSpawnAttempt
            predecessor = launched[0] if launched else attempts[0]
            drain = containment_recorder.record_drain(predecessor, evidence)
            if (
                type(drain) is not ContainedMutationDrainRecord
                or drain.launch_or_attempt is not predecessor
                or drain.evidence is not evidence
            ):
                raise QuPathExecutionError("Warpy containment drain is foreign")
            drains.append(drain)

        started = time.monotonic()
        executable_version = self._executable_version()
        stdout_path = log_directory / stdout_name
        stderr_path = log_directory / stderr_name
        try:
            with DurableDirectoryAuthority(log_directory, create=True) as logs:
                with logs.reserve_log_pair(stdout_name, stderr_name) as reserved:
                    try:
                        completed = contained(
                            command,
                            lock_descriptors=authoritative_lock_descriptors(),
                            launch_request=containment_request,
                            timeout_seconds=_authoritative_timeout_seconds(),
                            stdout_log=reserved.stdout,
                            stderr_log=reserved.stderr,
                            environment=environment,
                            process_start_observer=observe_process_start,
                            process_attempt_observer=observe_process_attempt,
                            process_drain_observer=observe_process_drain,
                        )
                    except _ContainedProcessError as backend_error:
                        failure = backend_error.failure
                        raise QuPathExecutionError._with_containment_evidence(
                            "QuPath contained process failed",
                            launch_evidence=failure.launch_evidence,
                            drain_evidence=failure.drain_evidence,
                        ) from backend_error
                    if (
                        type(completed) is not ContainedProcessResult
                        or len(attempts) != 1
                        or len(launched) != 1
                        or len(drains) != 1
                        or completed.args != command
                        or completed.launch_evidence is not launched[0].evidence
                        or completed.drain_evidence is not drains[0].evidence
                    ):
                        raise QuPathExecutionError(
                            "Warpy executor returned invalid containment evidence"
                        )
        except QuPathExecutionError:
            raise
        except BaseException as exc:
            raise QuPathExecutionError("QuPath contained process failed") from exc
        record = QuPathRunRecord(
            command=command,
            executable_version=executable_version,
            exit_status=completed.returncode,
            stdout_log=stdout_path,
            stderr_log=stderr_path,
            runtime_seconds=time.monotonic() - started,
        )
        if completed.returncode != 0:
            raise QuPathExecutionError._with_containment_evidence(
                f"QuPath script failed with exit status {completed.returncode}",
                record,
                launch_evidence=completed.launch_evidence,
                drain_evidence=completed.drain_evidence,
            )
        return record

    @_guarded_qupath_process
    def preflight_image(self, image_path: str | Path) -> QuPathImagePreflight:
        """Use QuPath itself to open an image without changing project state."""
        path = Path(image_path)
        if not (path.is_file() or (path / "zarr.json").is_file()):
            raise QuPathExecutionError(f"QuPath preflight image does not exist: {path}")
        executable_version = self._executable_version()
        command = (
            *self._command_prefix,
            "script",
            "--image",
            os.fspath(path),
            "--cmd",
            "getCurrentImageData().getServer().getWidth()",
        )
        started = time.monotonic()
        try:
            completed = self._executor(command, environment=native_compute_environment(self._launch_environment()))
        except OSError as exc:
            raise QuPathExecutionError(f"unable to execute QuPath: {self._executable}") from exc
        runtime_seconds = time.monotonic() - started
        record = QuPathImagePreflight(
            command=command,
            executable_version=executable_version,
            exit_status=completed.returncode,
            runtime_seconds=runtime_seconds,
        )
        if completed.returncode != 0:
            raise QuPathExecutionError(
                f"QuPath image preflight failed with exit status {completed.returncode}",
            )
        return record

    @_guarded_qupath_process
    def run_stage(
        self,
        project: ProjectLayout,
        script: str | Path,
        request: dict[str, object],
        *,
        run_id: str,
        stage: str,
        invocation_id: str,
        slide_id: str,
        project_entry_id: str,
        canonical_image_sha256: str,
        result_required: bool = True,
    ) -> QuPathStageRun:
        """Run a semantic stage whose sole authoritative result is validated JSON."""
        native_script, bound_request = _validate_stage_execution_binding(
            project,
            script,
            request,
            project_entry_id=project_entry_id,
            canonical_image_sha256=canonical_image_sha256,
        )
        exchange = StageExchangePaths.create(
            project.exchange_dir,
            run_id=run_id,
            stage=stage,
            invocation_id=invocation_id,
            slide_id=slide_id,
        )
        exchange.write_request(bound_request)
        if exchange.stage_result_path.exists() or exchange.generation_manifest_path.exists():
            raise ExchangeContractError(
                "stage invocation output already exists; use a new invocation_id"
            )
        command = (
            *self._command_prefix,
            "script",
            "--project",
            os.fspath(project.project_file),
            "--image",
            slide_id,
            "--args",
            os.fspath(exchange.request_path),
            "--args",
            os.fspath(exchange.stage_result_path),
            "--args",
            os.fspath(exchange.generation_manifest_path),
            "--save",
            os.fspath(native_script),
        )
        record = self._execute_script_command(command, exchange.directory, native_script.stem)
        if not exchange.stage_result_path.exists():
            if result_required:
                raise QuPathExecutionError(
                    "QuPath stage did not write its required machine-readable result",
                    record,
                )
            return QuPathStageRun(record, None, None, exchange)
        try:
            result = exchange.read_stage_result()
            manifest = (
                exchange.read_generation_manifest()
                if exchange.generation_manifest_path.exists()
                else None
            )
        except ExchangeContractError:
            if result_required:
                raise
            return QuPathStageRun(record, None, None, exchange)
        record = replace(record, stage_result=result)
        return QuPathStageRun(record, result, manifest, exchange)

    def _execute_script_command(
        self,
        command: tuple[str, ...],
        log_directory: Path,
        log_stem: str,
    ) -> QuPathRunRecord:
        executable_version = self._executable_version()
        started = time.monotonic()
        try:
            completed = self._executor(command, environment=native_compute_environment(self._launch_environment()))
        except OSError as exc:
            raise QuPathExecutionError(f"unable to execute QuPath: {self._executable}") from exc
        record = QuPathRunRecord(
            command=command,
            executable_version=executable_version,
            exit_status=completed.returncode,
            stdout_log=log_directory / f"{log_stem}.stdout.log",
            stderr_log=log_directory / f"{log_stem}.stderr.log",
            runtime_seconds=time.monotonic() - started,
        )
        _write_log(record.stdout_log, completed.stdout or "")
        _write_log(record.stderr_log, completed.stderr or "")
        if completed.returncode != 0:
            raise QuPathExecutionError(
                f"QuPath script failed with exit status {completed.returncode}", record
            )
        return record

    def _executable_version(self) -> str:
        command = (*self._command_prefix, "--version")
        try:
            completed = self._executor(command, environment=self._launch_environment())
        except OSError as exc:
            raise QuPathExecutionError(f"unable to execute QuPath: {self._executable}") from exc
        if completed.returncode != 0:
            raise QuPathExecutionError(
                f"QuPath version command failed with exit status {completed.returncode}"
            )
        output = "\n".join((completed.stdout or "", completed.stderr or ""))
        versions = {
            match.group(1)
            for match in re.finditer(
                r"(?m)^QuPath v([^\s]+)[ \t]*$",
                output,
            )
        }
        if len(versions) != 1:
            raise QuPathExecutionError("QuPath version output lacks one canonical version")
        version = next(iter(versions))
        if self._launch_binding is not None and version != self._launch_binding.qupath_version:
            raise QuPathExecutionError("QuPath version differs from the selected launch binding")
        return f"QuPath v{version}"


def _write_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _validate_stage_execution_binding(
    project: ProjectLayout,
    script: str | Path,
    request: dict[str, object],
    *,
    project_entry_id: str,
    canonical_image_sha256: str,
) -> tuple[Path, dict[str, object]]:
    if not isinstance(project, ProjectLayout):
        raise TypeError("project must be a ProjectLayout")
    if not isinstance(request, dict):
        raise QuPathExecutionError("stage request must be a mapping")
    expected_project = str(project.project_file.resolve())
    if request.get("project_file") != expected_project:
        raise QuPathExecutionError("stage request targets another QuPath project file")
    if (
        not isinstance(project_entry_id, str)
        or not project_entry_id.strip()
        or request.get("project_entry_id") != project_entry_id
    ):
        raise QuPathExecutionError("stage request project entry identity does not match")
    if (
        not isinstance(canonical_image_sha256, str)
        or len(canonical_image_sha256) != 64
        or any(character not in "0123456789abcdef" for character in canonical_image_sha256)
        or request.get("canonical_image_sha256") != canonical_image_sha256
    ):
        raise QuPathExecutionError("stage request canonical image identity does not match")
    try:
        validate_canonical_image_uri(request.get("canonical_image_uri"))
    except ExchangeContractError as exc:
        raise QuPathExecutionError("stage request canonical image URI is invalid") from exc
    bundle = materialize_script_bundle(project, tuple(QuPathScriptAsset))
    if request.get("script_bundle_sha256") != bundle.bundle_sha256:
        raise QuPathExecutionError("stage request script bundle identity does not match")
    if request.get("exchange_version") != REQUEST_SCHEMA:
        raise QuPathExecutionError("stage request exchange version does not match")
    requested_script = Path(script).resolve()
    members = {bundle_script_path(bundle, asset).resolve(): asset for asset in bundle.assets}
    if requested_script not in members:
        raise QuPathExecutionError("stage script is not a member of the immutable project bundle")
    asset = members[requested_script]
    relative_path = requested_script.relative_to(bundle.root.resolve()).as_posix()
    asset_sha256 = dict(bundle.asset_sha256s)[asset]
    for field, expected in (
        ("script_asset_path", relative_path),
        ("script_asset_sha256", asset_sha256),
    ):
        if field in request and request[field] != expected:
            raise QuPathExecutionError(
                f"stage request {field} does not match the registered bundle asset"
            )
    bound_request = dict(request)
    bound_request.update(
        script_asset_path=relative_path,
        script_asset_sha256=asset_sha256,
    )
    return requested_script, bound_request


def _authoritative_timeout_seconds() -> float | None:
    raw = os.environ.get("MIF_QUPATH_AUTHORITATIVE_TIMEOUT_SECONDS")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise QuPathExecutionError(
            "MIF_QUPATH_AUTHORITATIVE_TIMEOUT_SECONDS must be finite and positive"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise QuPathExecutionError(
            "MIF_QUPATH_AUTHORITATIVE_TIMEOUT_SECONDS must be finite and positive"
        )
    return value


def _file_sha256(path: Path) -> str:
    # Completed Zarr identity is its immutable root metadata, including generation ID.
    path = path / "zarr.json" if path.is_dir() else path
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_once(path: Path, value: dict[str, object]) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise QuPathExecutionError("QuPath request must contain finite JSON values") from exc
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuPathExecutionError(f"QuPath request is unreadable: {path}") from exc
        if existing != value:
            raise QuPathExecutionError(f"QuPath request conflicts with existing evidence: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise QuPathExecutionError("QuPath evidence descriptor must contain finite JSON") from exc
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _read_moving_image_evidence(
    path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    """Read moving image evidence into its typed representation.

    The decoded fields are checked through the record constructors and the applicable
    context comparisons before returning the result. The validated representation is
    returned for the subsequent operation."""

    value = _read_json_object(path, "moving-image evidence")
    expected_fields = {
        "schema_version",
        "project_file",
        "project_entry_id",
        "project_entry_path",
        "image_id",
        "image_sha256",
        "image_path",
        "entry_name",
        "hierarchy_role",
        "moving_modality",
        "source_metadata",
        "physical_calibration",
    }
    if set(value) != expected_fields:
        raise QuPathExecutionError("moving-image evidence schema is invalid")
    if value["schema_version"] != "qupath-moving-image/v1":
        raise QuPathExecutionError("moving-image evidence version is invalid")
    for field in (
        "project_file",
        "image_id",
        "image_sha256",
        "image_path",
        "entry_name",
        "moving_modality",
        "source_metadata",
    ):
        if value[field] != request[field]:
            raise QuPathExecutionError(f"moving-image evidence {field} does not match request")
    if not isinstance(value["project_entry_id"], str) or not value["project_entry_id"]:
        raise QuPathExecutionError("moving-image project_entry_id is invalid")
    if (
        not isinstance(value["project_entry_path"], str)
        or not Path(value["project_entry_path"]).is_absolute()
    ):
        raise QuPathExecutionError("moving-image project_entry_path is invalid")
    if value["hierarchy_role"] != "reference_only":
        raise QuPathExecutionError("moving image must remain non-authoritative")
    calibration = value["physical_calibration"]
    if not _is_valid_physical_calibration_evidence(calibration):
        raise QuPathExecutionError("moving-image physical calibration is invalid")
    return value


def _read_registration_evidence(
    path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    value = _read_json_object(path, "Warpy registration evidence")
    return _validate_registration_evidence_mapping(value, request)


def _validate_registration_evidence_mapping(
    value: dict[str, object],
    request: dict[str, object],
) -> dict[str, object]:
    """Validate registration evidence mapping for the current operation.

    The required mapping shape includes ``schema_version``, ``registration_id``,
    ``moving_project_entry_id``, ``fixed_project_entry_id``, ``moving_image_id``,
    ``fixed_image_id``. Field checks are applied before the validated value is returned or
    passed to its next constructor."""

    expected = {
        "schema_version",
        "registration_id",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_path",
        "fixed_image_path",
        "moving_image_sha256",
        "fixed_image_sha256",
        "transform_file",
        "transform_file_sha256",
        "provider",
        "provider_version",
        "runtime_versions",
        "moving_physical_calibration",
        "fixed_physical_calibration",
        "coordinate_pre_transform",
        "coordinate_post_transform",
        "global_evidence",
        "section_evidence",
    }
    if set(value) != expected:
        raise QuPathExecutionError("Warpy registration evidence schema is invalid")
    if value["schema_version"] != "qupath-warpy-registration-evidence/v1":
        raise QuPathExecutionError("Warpy registration evidence version is invalid")
    for field in (
        "registration_id",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_path",
        "fixed_image_path",
        "moving_image_sha256",
        "fixed_image_sha256",
        "transform_file",
        "transform_file_sha256",
    ):
        if value[field] != request.get(field):
            raise QuPathExecutionError(
                f"Warpy registration evidence {field} does not match request"
            )
    if value["provider"] != "warpy" or value["provider_version"] != "0.5.0":
        raise QuPathExecutionError("Warpy registration provider identity is invalid")
    if not isinstance(value["runtime_versions"], dict):
        raise QuPathExecutionError("Warpy registration runtime_versions is invalid")
    for field in ("moving_physical_calibration", "fixed_physical_calibration"):
        calibration = value[field]
        if not _is_valid_physical_calibration_evidence(calibration):
            raise QuPathExecutionError(f"Warpy registration {field} is invalid")
    for field in ("coordinate_pre_transform", "coordinate_post_transform"):
        if value[field] != request.get(field):
            raise QuPathExecutionError(
                f"Warpy registration evidence {field} does not match request"
            )
    if not isinstance(value["global_evidence"], dict):
        raise QuPathExecutionError("Warpy registration global_evidence is invalid")
    sections = value["section_evidence"]
    if not isinstance(sections, list) or any(not isinstance(item, dict) for item in sections):
        raise QuPathExecutionError("Warpy registration section_evidence is invalid")
    expected_sections = request.get("accepted_tissue_sections")
    if not isinstance(expected_sections, list) or [
        (item.get("section_id"), item.get("tissue_roi_id")) for item in sections
    ] != [
        (item.get("section_id"), item.get("tissue_roi_id"))
        for item in expected_sections
        if isinstance(item, dict)
    ]:
        raise QuPathExecutionError("Warpy registration tissue-section evidence scope is invalid")
    if any(
        not isinstance(item.get("tissue_path_object_id"), str) or not item["tissue_path_object_id"]
        for item in sections
    ):
        raise QuPathExecutionError(
            "Warpy registration TissueSection PathObject identity is invalid"
        )
    return value


def _read_transfer_evidence(
    path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    value = _read_json_object(path, "Warpy transfer evidence")
    return _validate_transfer_evidence_mapping(value, request)


def _validate_transfer_evidence_mapping(
    value: dict[str, object],
    request: dict[str, object],
) -> dict[str, object]:
    """Validate transfer evidence mapping for the current operation.

    The required mapping shape includes ``schema_version``, ``registration_id``,
    ``registration_revision``, ``tissue_generation_id``, ``moving_project_entry_id``,
    ``fixed_project_entry_id``. Field checks are applied before the validated value is
    returned or passed to its next constructor."""

    expected = {
        "schema_version",
        "registration_id",
        "registration_revision",
        "tissue_generation_id",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "requested_source_object_ids",
        "records",
        "authoritative_object_ids",
        "rejected_object_ids",
        "quarantined_object_ids",
    }
    if set(value) != expected:
        raise QuPathExecutionError("Warpy transfer evidence schema is invalid")
    if value["schema_version"] != "qupath-warpy-transfer-batch/v1":
        raise QuPathExecutionError("Warpy transfer evidence version is invalid")
    registration = request.get("registration")
    if not isinstance(registration, dict):
        raise QuPathExecutionError("Warpy transfer request registration is invalid")
    comparisons = {
        "registration_id": "registration_id",
        "registration_revision": "revision",
        "tissue_generation_id": "tissue_generation_id",
        "moving_project_entry_id": "moving_project_entry_id",
        "fixed_project_entry_id": "fixed_project_entry_id",
    }
    for evidence_field, request_field in comparisons.items():
        if value[evidence_field] != registration.get(request_field):
            raise QuPathExecutionError(
                f"Warpy transfer evidence {evidence_field} does not match request"
            )
    if value["requested_source_object_ids"] != request.get("source_object_ids"):
        raise QuPathExecutionError("Warpy transfer source object scope does not match request")
    for field in (
        "records",
        "authoritative_object_ids",
        "rejected_object_ids",
        "quarantined_object_ids",
    ):
        if not isinstance(value[field], list):
            raise QuPathExecutionError(f"Warpy transfer evidence {field} is invalid")
    if value["authoritative_object_ids"]:
        raise QuPathExecutionError("Warpy transfer QC cannot return authoritative object IDs")
    return value


def _read_transfer_commit_evidence(
    path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    """Read transfer commit evidence into its typed representation.

    The decoded fields are checked through the record constructors and the applicable
    context comparisons before returning the result. The validated representation is
    returned for the subsequent operation."""

    value = _read_json_object(path, "Warpy transfer-commit evidence")
    expected = {
        "schema_version",
        "attempt_id",
        "registration_id",
        "registration_revision",
        "authoritative_object_ids",
        "objects",
    }
    if set(value) != expected or value["schema_version"] != "qupath-warpy-transfer-commit/v1":
        raise QuPathExecutionError("Warpy transfer-commit evidence schema is invalid")
    registration = request.get("registration")
    records = request.get("records")
    if not isinstance(registration, dict) or not isinstance(records, list):
        raise QuPathExecutionError("Warpy transfer-commit request scope is invalid")
    if (
        value["attempt_id"] != request.get("attempt_id")
        or value["registration_id"] != registration.get("registration_id")
        or value["registration_revision"] != registration.get("revision")
    ):
        raise QuPathExecutionError("Warpy transfer-commit registration scope changed")
    expected_ids = [item.get("target_object_id") for item in records if isinstance(item, dict)]
    if value["authoritative_object_ids"] != expected_ids:
        raise QuPathExecutionError("Warpy transfer-commit object scope changed")
    objects = value["objects"]
    if not isinstance(objects, list) or len(objects) != len(records):
        raise QuPathExecutionError("Warpy transfer-commit live object evidence is invalid")
    for record, live in zip(records, objects, strict=True):
        if (
            not isinstance(record, dict)
            or not isinstance(live, dict)
            or set(live)
            != {
                "source_object_id",
                "target_object_id",
                "target_section_id",
                "target_geometry_sha256",
                "reconciled",
            }
        ):
            raise QuPathExecutionError("Warpy transfer-commit live object evidence is invalid")
        for field in ("source_object_id", "target_object_id", "target_section_id"):
            if live[field] != record.get(field):
                raise QuPathExecutionError("Warpy transfer-commit live object scope changed")
        if live["target_geometry_sha256"] != record.get("transformed_geometry_sha256"):
            raise QuPathExecutionError("Warpy transfer-commit geometry evidence changed")
        if not isinstance(live["reconciled"], bool):
            raise QuPathExecutionError("Warpy transfer-commit reconciliation evidence is invalid")
    return value


def _read_transfer_cleanup_evidence(
    path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    """Read transfer cleanup evidence into its typed representation.

    The decoded fields are checked through the record constructors and the applicable
    context comparisons before returning the result. The validated representation is
    returned for the subsequent operation."""

    value = _read_json_object(path, "Warpy transfer-cleanup evidence")
    expected = {
        "schema_version",
        "attempt_id",
        "registration_id",
        "registration_revision",
        "absent_target_object_ids",
        "objects",
    }
    if set(value) != expected or value["schema_version"] != "qupath-warpy-transfer-cleanup/v1":
        raise QuPathExecutionError("Warpy transfer-cleanup evidence schema is invalid")
    registration = request.get("registration")
    records = request.get("records")
    if not isinstance(registration, dict) or not isinstance(records, list):
        raise QuPathExecutionError("Warpy transfer-cleanup request scope is invalid")
    expected_ids = [item.get("target_object_id") for item in records if isinstance(item, dict)]
    if (
        value["attempt_id"] != request.get("attempt_id")
        or value["registration_id"] != registration.get("registration_id")
        or value["registration_revision"] != registration.get("revision")
        or value["absent_target_object_ids"] != expected_ids
    ):
        raise QuPathExecutionError("Warpy transfer-cleanup scope changed")
    objects = value["objects"]
    if not isinstance(objects, list) or len(objects) != len(records):
        raise QuPathExecutionError("Warpy transfer-cleanup absence evidence is invalid")
    for record, live in zip(records, objects, strict=True):
        if (
            not isinstance(record, dict)
            or not isinstance(live, dict)
            or set(live)
            != {
                "source_object_id",
                "target_object_id",
                "transfer_qc_id",
                "absent",
            }
        ):
            raise QuPathExecutionError("Warpy transfer-cleanup absence evidence is invalid")
        for field in ("source_object_id", "target_object_id", "transfer_qc_id"):
            if live[field] != record.get(field):
                raise QuPathExecutionError("Warpy transfer-cleanup object scope changed")
        if live["absent"] is not True:
            raise QuPathExecutionError("Warpy transfer-cleanup did not attest absence")
    return value


def _request_identifier(
    request: dict[str, object],
    *,
    schema: str,
    field: str,
) -> str:
    if not isinstance(request, dict) or request.get("schema_version") != schema:
        raise QuPathExecutionError(f"{schema} request schema is invalid")
    value = request.get(field)
    if not isinstance(value, str) or not value.strip():
        raise QuPathExecutionError(f"{schema} request {field} is invalid")
    return value.strip()


def _catalogue_request_from_mapping(value: object):
    """Decode the closed catalogue request without making runner its owner."""
    from .catalogue import (
        ProjectIdentityRecord,
        QuPathCatalogueRequestEnvelope,
        QuPathEntryUpsertRequest,
        QuPathIdentityInstallRequest,
        QuPathProjectInspectRequest,
    )

    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
        raise ValueError("catalogue request must be an object")
    payload_mapping = value["payload"]
    operation = value.get("operation")
    if operation == "inspect_project":
        payload = QuPathProjectInspectRequest(**payload_mapping)
    elif operation == "ensure_project_identity":
        material = dict(payload_mapping)
        material["identity_record"] = ProjectIdentityRecord.from_mapping(
            material["identity_record"]
        )
        payload = QuPathIdentityInstallRequest(**material)
    elif operation == "upsert_entry":
        payload = QuPathEntryUpsertRequest(**payload_mapping)
    else:
        raise ValueError("catalogue operation is invalid")
    material = dict(value)
    material["payload"] = payload
    return QuPathCatalogueRequestEnvelope(**material)


def _identifier_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuPathExecutionError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise QuPathExecutionError(f"{label} must be a JSON object")
    return value


def _finite_positive(value: float) -> bool:
    return value > 0.0 and value not in (float("inf"), float("-inf")) and value == value


def _is_valid_physical_calibration_evidence(value: object) -> bool:
    """Return whether evidence has the exact closed two-axis calibration wire."""
    if not isinstance(value, dict) or set(value) != {
        "x_um_per_px",
        "y_um_per_px",
    }:
        return False
    return all(
        not isinstance(value[axis], bool)
        and isinstance(value[axis], (int, float))
        and _finite_positive(float(value[axis]))
        for axis in ("x_um_per_px", "y_um_per_px")
    )


# Catalogue operations import runner; bind these public annotation types only
# after runner's own public classes exist to avoid a circular import.
from .catalogue import (  # noqa: E402
    ProjectMutationProcessDrainEvidence,
    ProjectMutationProcessLaunchRecord,
    ProjectMutationProcessSpawnAttemptRecord,
)
