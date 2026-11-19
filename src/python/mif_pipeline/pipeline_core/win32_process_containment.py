"""Win32 parent-death containment for authoritative external mutations.

The `Win32ContainedProcess` record carries `process_handle`, `thread_handle`, `stdout`,
`stderr`, `stdin` and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from threading import RLock
from typing import Callable, Mapping, Protocol
import uuid

from .errors import ROIAnalysisError
from .process_containment import (
    ContainedProcessResult,
    ContainmentInspection,
    ContainmentLaunchRequest,
    ContainmentRecoveryRequest,
    ProcessAttemptObserver,
    ProcessDrainObserver,
    ProcessStartObserver,
    WindowsKillOnCloseJobAttachmentV1,
    WindowsKillOnCloseJobDrainV1,
    _ContainedProcessError,
    _ContainedProcessFailure,
    _bounded_log_excerpt,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAXIMUM_SECONDARY_NOTE_CHARACTERS = 512
_CANCELLATION_POLL_SECONDS = 0.05


# ==============================================================================
#                           Win32 process containment
# ==============================================================================


class _CancellationRequested(Exception):
    """Internal synchronous cancellation marker observed at a safe boundary."""


def _add_secondary_note(
    primary: BaseException,
    label: str,
    secondary: BaseException,
) -> None:
    note = f"{label}: {secondary!r}"
    secondary_notes = getattr(secondary, "__notes__", ())
    if secondary_notes:
        note = f"{note}; {'; '.join(secondary_notes)}"
    primary.add_note(note[:_MAXIMUM_SECONDARY_NOTE_CHARACTERS])


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> "_GUID":
        raw = value.bytes_le
        return cls(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8)(*raw[8:]),
        )


def _resolve_windows_local_app_data() -> Path:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise ROIAnalysisError("Windows LocalAppData is unavailable")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    known_folder = _GUID.from_uuid(uuid.UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091"))
    value = ctypes.c_wchar_p()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(known_folder), 0, None, ctypes.byref(value))
    if result != 0 or not value.value:
        raise ROIAnalysisError("Windows LocalAppData is unavailable")
    try:
        return Path(value.value)
    finally:
        ole32.CoTaskMemFree(value)


def windows_local_app_data(*, resolver: Callable[[], Path] | None = None) -> Path:
    """Resolve LocalAppData through the Windows known-folder authority."""
    path = Path((resolver or _resolve_windows_local_app_data)())
    if not path.is_absolute():
        raise ROIAnalysisError("Windows LocalAppData is not absolute")
    return path


def _apply_and_validate_current_user_acl(path: Path) -> None:
    executable = shutil.which("powershell.exe")
    if executable is None:
        raise ROIAnalysisError("Windows DACL validation requires Windows PowerShell")
    script = """
$ErrorActionPreference = 'Stop'
$path = [Environment]::GetEnvironmentVariable('MIF_PIPELINE_WINDOWS_AUTHORITY_PATH', 'Process')
if ([string]::IsNullOrWhiteSpace($path)) { exit 22 }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rights = [Security.AccessControl.FileSystemRights]::FullControl
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$propagate = [Security.AccessControl.PropagationFlags]::None
$allow = [Security.AccessControl.AccessControlType]::Allow
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $rights, $inherit, $propagate, $allow)
[void]$acl.AddAccessRule($rule)
(Get-Item -LiteralPath $path).SetAccessControl($acl)
$check = Get-Acl -LiteralPath $path
$writeMask = [Security.AccessControl.FileSystemRights]'Write, Modify, FullControl, ChangePermissions, TakeOwnership'
$currentFull = $false
foreach ($entry in @($check.Access)) {
  if ($entry.AccessControlType -ne $allow) { continue }
  $entrySid = $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier])
  if ($entrySid -eq $sid -and (($entry.FileSystemRights -band $rights) -eq $rights)) { $currentFull = $true; continue }
  if (($entry.FileSystemRights -band $writeMask) -ne 0) { exit 23 }
}
if (-not $currentFull -or -not $check.AreAccessRulesProtected) { exit 24 }
"""
    child_environment = dict(os.environ)
    child_environment["MIF_PIPELINE_WINDOWS_AUTHORITY_PATH"] = os.fspath(path)
    completed = subprocess.run(
        (
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ),
        capture_output=True,
        text=True,
        check=False,
        env=child_environment,
    )
    if completed.returncode != 0:
        raise ROIAnalysisError("Windows project-authority DACL is not current-user-only")


def prepare_private_windows_authority_directory(
    path: Path,
    *,
    acl_policy: Callable[[Path], None] | None = None,
) -> Path:
    """Create and pin a non-reparse, current-user-only authority root."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(absolute.anchor)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def validate_component(component: Path) -> None:
        metadata = component.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            raise ROIAnalysisError("Windows project-authority path cannot traverse a reparse point")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ROIAnalysisError(
                "Windows project-authority path contains a nondirectory component"
            )

    validate_component(cursor)
    for component in absolute.parts[1:]:
        candidate = cursor / component
        try:
            validate_component(candidate)
        except FileNotFoundError:
            try:
                candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                # A concurrent creator is acceptable only after the same
                # no-reparse/directory validation below.
                pass
            validate_component(candidate)
        cursor = candidate
    (acl_policy or _apply_and_validate_current_user_acl)(absolute)
    return absolute


def _require_supported_windows_host(major_version: int, pointer_bytes: int) -> None:
    if major_version < 10 or pointer_bytes != 8:
        raise ROIAnalysisError(
            "authoritative containment requires 64-bit Windows 10/11 "
            "or supported Windows Server 2016 or newer"
        )


def authoritative_job_name(
    hierarchy_scope_sha256: str,
    *,
    authority_kind: str = "warpy",
) -> str:
    """Return a bounded Job name shared by one authoritative mutation scope."""
    if (
        not isinstance(hierarchy_scope_sha256, str)
        or _SHA256.fullmatch(hierarchy_scope_sha256) is None
    ):
        raise ROIAnalysisError("authoritative process containment digest is invalid")
    if authority_kind not in {"warpy", "qupath-project"}:
        raise ROIAnalysisError("authoritative process containment kind is invalid")
    return f"Local\\mif-pipeline-{authority_kind}-v1-{hierarchy_scope_sha256}"


@dataclass(slots=True)
class Win32ContainedProcess:
    process_handle: object
    thread_handle: object
    stdout: object
    stderr: object
    stdin: object
    process_id: int
    start_epoch_millis: int
    child_session_id: int


class Win32PostCreateFailure(Exception):
    """Private facts retained after native creation but before safe return."""

    __slots__ = (
        "original_failure",
        "process_id",
        "start_epoch_millis",
        "child_session_id",
        "job_drained",
    )

    def __init__(
        self,
        original_failure: BaseException,
        *,
        process_id: int | None,
        start_epoch_millis: int | None,
        child_session_id: int | None,
        job_drained: bool,
    ) -> None:
        super().__init__("Win32 process creation failed during post-create setup")
        if not isinstance(original_failure, BaseException):
            raise TypeError("original_failure must be an exception")
        identity = (process_id, start_epoch_millis, child_session_id)
        if any(value is None for value in identity):
            if any(value is not None for value in identity):
                raise ValueError("Win32 post-create identity must be complete or absent")
        elif (
            type(process_id) is not int
            or process_id <= 0
            or type(start_epoch_millis) is not int
            or start_epoch_millis <= 0
            or type(child_session_id) is not int
            or child_session_id < 0
        ):
            raise ValueError("Win32 post-create identity is invalid")
        if type(job_drained) is not bool:
            raise ValueError("Win32 post-create drain fact must be exact bool")
        self.original_failure = original_failure
        self.process_id = process_id
        self.start_epoch_millis = start_epoch_millis
        self.child_session_id = child_session_id
        self.job_drained = job_drained


@dataclass(slots=True)
class _PinnedWin32Process:
    process_handle: object
    process_id: int
    start_epoch_millis: int


class Win32ProcessPrimitives(Protocol):
    def create_job(self, name: str) -> object: ...
    def set_kill_on_close(self, job: object) -> None: ...
    def create_suspended(
        self,
        command: tuple[str, ...],
        job: object,
        *,
        stdout_log: object,
        stderr_log: object,
        environment: Mapping[str, str] | None = None,
        process_attempt_observer: ProcessAttemptObserver | None = None,
    ) -> Win32ContainedProcess: ...
    def resume(self, process: Win32ContainedProcess) -> None: ...
    def process_start_identity(self, process: Win32ContainedProcess) -> tuple[int, int]: ...
    def process_session_id(self, process: Win32ContainedProcess) -> int: ...
    def process_start_identity_by_id(self, process_id: int) -> tuple[int, int] | None: ...
    def _pin_process_for_recovery(self, process_id: int) -> _PinnedWin32Process | None: ...
    def _is_pinned_process_in_job(self, process: _PinnedWin32Process, job: object) -> bool: ...
    def _close_pinned_process(self, process: _PinnedWin32Process) -> None: ...
    def wait_job_empty(self, job: object, *, timeout_seconds: float | None = None) -> None: ...
    def exit_code(self, process: Win32ContainedProcess) -> int: ...
    def terminate_job(self, job: object) -> None: ...
    def terminate_process(self, process: Win32ContainedProcess) -> None: ...
    def close_process(self, process: Win32ContainedProcess) -> None: ...
    def close_job(self, job: object) -> None: ...
    def open_job(self, name: str) -> object | None: ...


def _terminate_job_and_wait_empty(
    primitives: Win32ProcessPrimitives,
    job: object,
    *,
    timeout_seconds: float,
) -> None:
    primary: BaseException | None = None
    try:
        primitives.terminate_job(job)
    except BaseException as exc:
        primary = exc
    try:
        primitives.wait_job_empty(job, timeout_seconds=timeout_seconds)
    except BaseException as exc:
        if primary is None:
            raise
        _add_secondary_note(
            primary,
            "Win32 Job-empty proof also failed",
            exc,
        )
    if primary is not None:
        raise primary


def _finalize_windows_logs(
    stdout_log: object,
    stderr_log: object,
) -> tuple[str, str, bool, bool]:
    """Synchronize native log handles before constructing bounded views."""

    failures: list[BaseException] = []
    for stream in (stdout_log, stderr_log):
        try:
            stream.flush()  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError) as exc:
            failures.append(exc)
        try:
            descriptor = stream.fileno()  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            # In-memory streams exist only at the injected primitive seam; the
            # native launcher itself requires real inheritable OS handles.
            descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                failures.append(exc)
    if failures:
        primary = failures[0]
        for secondary in failures[1:]:
            _add_secondary_note(
                primary,
                "additional Win32 log synchronization failure",
                secondary,
            )
        raise ROIAnalysisError(
            "contained process logs could not be synchronized after Job drain"
        ) from primary
    stdout_excerpt, stdout_truncated = _bounded_log_excerpt(stdout_log)
    stderr_excerpt, stderr_truncated = _bounded_log_excerpt(stderr_log)
    return (
        stdout_excerpt,
        stderr_excerpt,
        stdout_truncated,
        stderr_truncated,
    )


class WindowsJobContainmentRecovery:
    """Recover only an exact named Windows Job and PID/start identity."""

    def __init__(
        self,
        primitives: Win32ProcessPrimitives,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ROIAnalysisError("Windows recovery timeout is invalid")
        self._primitives = primitives
        self._timeout_seconds = float(timeout_seconds)

    def inspect(self, request: ContainmentRecoveryRequest) -> ContainmentInspection:
        del request
        raise ROIAnalysisError(
            "Windows read-only containment inspection is unsupported by the "
            "locked v1 evidence projection"
        )

    def recover_stop(self, request: ContainmentRecoveryRequest) -> WindowsKillOnCloseJobDrainV1:
        if (
            type(request) is not ContainmentRecoveryRequest
            or type(request.launch_evidence) is not WindowsKillOnCloseJobAttachmentV1
        ):
            raise ROIAnalysisError("Windows containment recovery request is invalid")
        launch_evidence = request.launch_evidence
        job = self._primitives.open_job(launch_evidence.job_name)
        if job is None:
            raise ROIAnalysisError("authoritative Windows Job is absent")
        expected = (
            request.contained_process_id,
            request.contained_process_start_epoch_millis,
        )
        pinned_process: _PinnedWin32Process | None = None
        primary: BaseException | None = None
        try:
            pinned_process = self._primitives._pin_process_for_recovery(
                request.contained_process_id
            )
            if pinned_process is None:
                raise ROIAnalysisError("recovered Windows process identity cannot be verified")
            observed = (
                pinned_process.process_id,
                pinned_process.start_epoch_millis,
            )
            if observed != expected:
                raise ROIAnalysisError("recovered Windows PID belongs to another process identity")
            if not self._primitives._is_pinned_process_in_job(pinned_process, job):
                raise ROIAnalysisError("recovered Windows process is not a member of the exact Job")
            _terminate_job_and_wait_empty(
                self._primitives,
                job,
                timeout_seconds=self._timeout_seconds,
            )
            return WindowsKillOnCloseJobDrainV1(
                provider="windows-kill-on-close-job/v1",
                semantic_state="drained",
                job_name=launch_evidence.job_name,
                termination_reason="recovery_stop",
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error: Exception | None = None
            if pinned_process is not None:
                try:
                    self._primitives._close_pinned_process(pinned_process)
                except Exception as exc:
                    cleanup_error = exc
            try:
                self._primitives.close_job(job)
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                elif primary is not None:
                    _add_secondary_note(
                        primary,
                        "Win32 Job-handle cleanup also failed",
                        exc,
                    )
            if cleanup_error is not None:
                if primary is None:
                    raise cleanup_error
                _add_secondary_note(
                    primary,
                    "Win32 recovery cleanup also failed",
                    cleanup_error,
                )


class Win32JobProcessExecutor:
    """Sequence a suspended child into a named kill-on-close Job."""

    def __init__(
        self,
        primitives: Win32ProcessPrimitives,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ROIAnalysisError("Windows launch timeout is invalid")
        self._primitives = primitives
        self._timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)
        self._interactive_lock = RLock()
        self._interactive_active = False
        self._interactive_resources: tuple[object, ...] | None = None

    def require_interactive_stopped(self) -> None:
        """Reject active or unresolved GUI lifetime without probing or killing it."""
        with self._interactive_lock:
            if self._interactive_active:
                raise ROIAnalysisError("interactive Windows Job has not been confirmed stopped")

    def run_interactive(
        self,
        command: tuple[str, ...],
        *,
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
    ) -> ContainedProcessResult:
        """Wait for natural whole-Job exit; never terminate released operator work."""
        if (
            type(command) is not tuple
            or not command
            or any(type(item) is not str or not item or "\0" in item for item in command)
            or stdout_log is stderr_log
            or type(environment) is not dict
            or any(
                type(k) is not str
                or not k
                or "=" in k
                or "\0" in k
                or type(v) is not str
                or "\0" in v
                for k, v in environment.items()
            )
        ):
            raise ROIAnalysisError("interactive Windows process inputs are invalid")
        with self._interactive_lock:
            self.require_interactive_stopped()
            self._interactive_active = True
        primitives = self._primitives
        job = None
        process = None
        stopped = True
        release_attempted = False
        primary = None
        result = None
        try:
            # This is a random native Job identity only, never project authority.
            job_name = authoritative_job_name(
                uuid.uuid4().hex + uuid.uuid4().hex, authority_kind="qupath-project"
            )
            job = primitives.create_job(job_name)
            primitives.set_kill_on_close(job)
            try:
                process = primitives.create_suspended(
                    command,
                    job,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                    environment=environment,
                )
            except Win32PostCreateFailure as exc:
                stopped = exc.job_drained
                raise
            stopped = False
            attachment = WindowsKillOnCloseJobAttachmentV1(
                provider="windows-kill-on-close-job/v1",
                job_name=job_name,
                child_session_id=process.child_session_id,
            )
            release_attempted = True
            primitives.resume(process)
            primitives.wait_job_empty(job, timeout_seconds=None)
            stopped = True
            returncode = primitives.exit_code(process)
            drain = WindowsKillOnCloseJobDrainV1(
                provider="windows-kill-on-close-job/v1",
                semantic_state="drained",
                job_name=job_name,
                termination_reason="normal_exit" if returncode == 0 else "nonzero_exit",
            )
            logs = _finalize_windows_logs(stdout_log, stderr_log)
            result = ContainedProcessResult(
                args=command,
                returncode=returncode,
                launch_evidence=attachment,
                drain_evidence=drain,
                stdout_excerpt=logs[0],
                stderr_excerpt=logs[1],
                stdout_truncated=logs[2],
                stderr_truncated=logs[3],
            )
        except BaseException as exc:
            primary = exc
        if not stopped and not release_attempted and job is not None:
            try:
                _terminate_job_and_wait_empty(primitives, job, timeout_seconds=30.0)
                stopped = True
            except BaseException as exc:
                assert primary is not None
                _add_secondary_note(primary, "unreleased interactive Job cleanup also failed", exc)
        cleanup_complete = stopped
        if stopped:
            for resource, close in (
                (process, primitives.close_process),
                (job, primitives.close_job),
            ):
                if resource is not None:
                    try:
                        close(resource)
                    except BaseException as exc:
                        cleanup_complete = False
                        if primary is None:
                            primary = exc
                        else:
                            _add_secondary_note(
                                primary, "interactive handle cleanup also failed", exc
                            )
        with self._interactive_lock:
            if cleanup_complete:
                self._interactive_active = False
                self._interactive_resources = None
            else:
                # Keep the Job HANDLE alive: closing it could kill unsaved work.
                self._interactive_resources = (job, process, stdout_log, stderr_log)
        if primary is not None:
            raise ROIAnalysisError(
                "interactive Windows process did not complete safely"
            ) from primary
        assert result is not None
        return result

    def run(
        self,
        command: tuple[str, ...],
        *,
        launch_request: ContainmentLaunchRequest,
        stdout_log: object,
        stderr_log: object,
        environment: Mapping[str, str] | None = None,
        process_start_observer: ProcessStartObserver,
        process_attempt_observer: ProcessAttemptObserver | None = None,
        process_drain_observer: ProcessDrainObserver | None = None,
        process_terminal_observer: Callable[[int | None, object], None] | None = None,
        cancellation_event: object | None = None,
    ) -> ContainedProcessResult:
        if (
            type(launch_request) is not ContainmentLaunchRequest
            or launch_request.provider != "windows-kill-on-close-job/v1"
            or launch_request.windows_job_name is None
        ):
            raise ROIAnalysisError("Windows containment launch request is invalid")
        if stdout_log is stderr_log:
            raise ROIAnalysisError("Windows containment log streams must be distinct")
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
            raise ROIAnalysisError("Windows containment observers are invalid")
        job_name = launch_request.windows_job_name
        launch_evidence: WindowsKillOnCloseJobAttachmentV1 | None = None
        job: object | None = None
        process: Win32ContainedProcess | None = None
        assigned = False
        process_id: int | None = None
        start_epoch_millis: int | None = None
        publication_complete = False
        released = False
        release_outcome_unresolved = False
        cancellation_before_release = False
        job_empty = False
        drain_evidence: WindowsKillOnCloseJobDrainV1 | None = None
        returncode: int | None = None
        log_facts: tuple[str, str, bool, bool] | None = None
        log_finalization_attempted = False
        result: ContainedProcessResult | None = None
        primary: BaseException | None = None
        post_create_failure: Win32PostCreateFailure | None = None
        drain_observation_attempted = False
        drain_observation_complete = process_drain_observer is None
        terminal_observation_attempted = False
        terminal_observation_complete = process_terminal_observer is None

        def publish_drain(evidence: WindowsKillOnCloseJobDrainV1) -> None:
            nonlocal drain_observation_attempted, drain_observation_complete
            if drain_observation_attempted:
                raise ROIAnalysisError("Windows process drain observer repeated")
            drain_observation_attempted = True
            if process_drain_observer is not None:
                process_drain_observer(evidence)
            drain_observation_complete = True

        def publish_terminal(status: int | None, evidence: WindowsKillOnCloseJobDrainV1) -> None:
            nonlocal terminal_observation_attempted, terminal_observation_complete
            if terminal_observation_attempted:
                raise ROIAnalysisError("Windows process terminal observer repeated")
            terminal_observation_attempted = True
            if process_terminal_observer is not None:
                process_terminal_observer(status, evidence)
            terminal_observation_complete = True

        def wait_for_job_empty(job_handle: object) -> None:
            if cancellation_event is None:
                self._primitives.wait_job_empty(job_handle, timeout_seconds=self._timeout_seconds)
                return
            deadline = (
                None if self._timeout_seconds is None else time.monotonic() + self._timeout_seconds
            )
            while True:
                if cancellation_event.is_set():  # type: ignore[attr-defined]
                    try:
                        self._primitives.wait_job_empty(job_handle, timeout_seconds=0.0)
                    except (subprocess.TimeoutExpired, TimeoutError):
                        raise _CancellationRequested(
                            "Windows contained process cancellation requested"
                        )
                    return
                wait_seconds = _CANCELLATION_POLL_SECONDS
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Windows Job did not terminate")
                    wait_seconds = min(wait_seconds, remaining)
                try:
                    self._primitives.wait_job_empty(job_handle, timeout_seconds=wait_seconds)
                    return
                except (subprocess.TimeoutExpired, TimeoutError):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise

        try:
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("Windows contained process cancellation requested")
            job = self._primitives.create_job(job_name)
            self._primitives.set_kill_on_close(job)
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("Windows contained process cancellation requested")

            def publish_attempt_at_create_boundary() -> None:
                nonlocal cancellation_before_release
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()  # type: ignore[attr-defined]
                ):
                    cancellation_before_release = True
                    raise _CancellationRequested("Windows contained process cancellation requested")
                if process_attempt_observer is not None:
                    process_attempt_observer()

            if environment is None:
                process = self._primitives.create_suspended(
                    command,
                    job,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                    process_attempt_observer=publish_attempt_at_create_boundary,
                )
            else:
                process = self._primitives.create_suspended(
                    command,
                    job,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                    environment=environment,
                    process_attempt_observer=publish_attempt_at_create_boundary,
                )
            assigned = True
            process_id = process.process_id
            start_epoch_millis = process.start_epoch_millis
            launch_evidence = WindowsKillOnCloseJobAttachmentV1(
                provider="windows-kill-on-close-job/v1",
                job_name=job_name,
                child_session_id=process.child_session_id,
            )
            process_launch_sha256 = process_start_observer(
                process_id,
                start_epoch_millis,
                launch_evidence,
            )
            if (
                type(process_launch_sha256) is not str
                or _SHA256.fullmatch(process_launch_sha256) is None
            ):
                raise ROIAnalysisError("process start observer did not return a launch digest")
            publication_complete = True
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("Windows contained process cancellation requested")
            try:
                self._primitives.resume(process)
            except BaseException:
                release_outcome_unresolved = True
                raise
            released = True
            wait_for_job_empty(job)
            job_empty = True
            returncode = self._primitives.exit_code(process)
            termination_reason = "normal_exit" if returncode == 0 else "nonzero_exit"
            drain_evidence = WindowsKillOnCloseJobDrainV1(
                provider="windows-kill-on-close-job/v1",
                semantic_state="drained",
                job_name=job_name,
                termination_reason=termination_reason,
            )
            publish_drain(drain_evidence)
            publish_terminal(returncode, drain_evidence)
            log_finalization_attempted = True
            try:
                log_facts = _finalize_windows_logs(stdout_log, stderr_log)
            except BaseException as log_exc:
                if returncode != 0:
                    nonzero = subprocess.CalledProcessError(returncode, command)
                    _add_secondary_note(
                        nonzero,
                        "post-drain log finalization also failed",
                        log_exc,
                    )
                    raise nonzero from log_exc
                raise
            result = ContainedProcessResult(
                args=command,
                returncode=returncode,
                launch_evidence=launch_evidence,
                drain_evidence=drain_evidence,
                stdout_excerpt=log_facts[0],
                stderr_excerpt=log_facts[1],
                stdout_truncated=log_facts[2],
                stderr_truncated=log_facts[3],
            )
        except BaseException as exc:
            post_create_failure = exc if type(exc) is Win32PostCreateFailure else None
            if post_create_failure is not None:
                primary = post_create_failure.original_failure
                process_id = post_create_failure.process_id
                start_epoch_millis = post_create_failure.start_epoch_millis
                if process_id is not None:
                    assert (
                        start_epoch_millis is not None
                        and post_create_failure.child_session_id is not None
                    )
                    launch_evidence = WindowsKillOnCloseJobAttachmentV1(
                        provider="windows-kill-on-close-job/v1",
                        job_name=job_name,
                        child_session_id=(post_create_failure.child_session_id),
                    )
                    if post_create_failure.job_drained:
                        job_empty = True
                        drain_evidence = WindowsKillOnCloseJobDrainV1(
                            provider="windows-kill-on-close-job/v1",
                            semantic_state="drained",
                            job_name=job_name,
                            termination_reason="launch_publication_failed",
                        )
            else:
                primary = exc
            if process is not None and assigned and job is not None:
                if not job_empty:
                    cleanup_succeeded = True
                    try:
                        self._primitives.terminate_job(job)
                    except BaseException as cleanup_exc:
                        cleanup_succeeded = False
                        _add_secondary_note(
                            primary,
                            "Win32 Job termination also failed",
                            cleanup_exc,
                        )
                    try:
                        self._primitives.wait_job_empty(job, timeout_seconds=30.0)
                    except BaseException as cleanup_exc:
                        cleanup_succeeded = False
                        _add_secondary_note(
                            primary,
                            "Win32 Job-empty proof also failed",
                            cleanup_exc,
                        )
                    if cleanup_succeeded:
                        job_empty = True
                        if not publication_complete or cancellation_before_release:
                            termination_reason = "launch_publication_failed"
                        elif release_outcome_unresolved:
                            termination_reason = None
                        elif isinstance(exc, _CancellationRequested):
                            termination_reason = "cancelled"
                        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                            termination_reason = "timeout"
                        else:
                            # This is the runtime-initiated Job cancellation
                            # event; the distinct original cause remains chained.
                            termination_reason = "cancelled"
                        if termination_reason is not None:
                            drain_evidence = WindowsKillOnCloseJobDrainV1(
                                provider="windows-kill-on-close-job/v1",
                                semantic_state="drained",
                                job_name=job_name,
                                termination_reason=termination_reason,
                            )
                elif drain_evidence is None:
                    termination_reason = (
                        "recovery_stop"
                        if returncode is None
                        else "normal_exit" if returncode == 0 else "nonzero_exit"
                    )
                    drain_evidence = WindowsKillOnCloseJobDrainV1(
                        provider="windows-kill-on-close-job/v1",
                        semantic_state="drained",
                        job_name=job_name,
                        termination_reason=termination_reason,
                    )
                if drain_evidence is not None and not drain_observation_attempted:
                    try:
                        publish_drain(drain_evidence)
                    except BaseException as observation_exc:
                        _add_secondary_note(
                            primary,
                            "process drain publication also failed",
                            observation_exc,
                        )
                if (
                    drain_evidence is not None
                    and drain_observation_complete
                    and not terminal_observation_attempted
                ):
                    try:
                        publish_terminal(returncode, drain_evidence)
                    except BaseException as observation_exc:
                        _add_secondary_note(
                            primary,
                            "process terminal publication also failed",
                            observation_exc,
                        )
                if (
                    drain_evidence is not None
                    and drain_observation_complete
                    and terminal_observation_complete
                    and log_facts is None
                    and not log_finalization_attempted
                ):
                    try:
                        log_finalization_attempted = True
                        log_facts = _finalize_windows_logs(stdout_log, stderr_log)
                    except BaseException as log_exc:
                        _add_secondary_note(
                            primary,
                            "post-drain log finalization also failed",
                            log_exc,
                        )
        finally:
            cleanup_errors: list[BaseException] = []
            if process is not None and primary is not None and not assigned:
                try:
                    self._primitives.terminate_process(process)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if process is not None:
                try:
                    self._primitives.close_process(process)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if job is not None:
                try:
                    self._primitives.close_job(job)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if primary is None:
                    primary = cleanup_errors.pop(0)
                for cleanup_error in cleanup_errors:
                    _add_secondary_note(
                        primary,
                        "Win32 containment cleanup also failed",
                        cleanup_error,
                    )
        if primary is not None:
            if (
                not assigned
                and post_create_failure is None
                and not isinstance(primary, _CancellationRequested)
            ):
                raise primary
            failure = _ContainedProcessFailure(
                process_id=process_id,
                start_epoch_millis=start_epoch_millis,
                launch_evidence=launch_evidence,
                drain_evidence=drain_evidence,
                returncode=returncode,
                stdout_excerpt=None if log_facts is None else log_facts[0],
                stderr_excerpt=None if log_facts is None else log_facts[1],
                stdout_truncated=None if log_facts is None else log_facts[2],
                stderr_truncated=None if log_facts is None else log_facts[3],
            )
            raise _ContainedProcessError(
                "Windows contained process did not complete normally",
                failure,
            ) from primary
        assert result is not None
        return result


def run_in_kill_on_close_job(
    command: tuple[str, ...],
    *,
    launch_request: ContainmentLaunchRequest,
    stdout_log: object,
    stderr_log: object,
    timeout_seconds: float | None = None,
    primitives: Win32ProcessPrimitives | None = None,
    environment: Mapping[str, str] | None = None,
    process_start_observer: ProcessStartObserver,
    process_attempt_observer: ProcessAttemptObserver | None = None,
    process_drain_observer: ProcessDrainObserver | None = None,
    process_terminal_observer: Callable[[int | None, object], None] | None = None,
    cancellation_event: object | None = None,
) -> ContainedProcessResult:
    provider = primitives or Win32CtypesProcessPrimitives()
    return Win32JobProcessExecutor(provider, timeout_seconds=timeout_seconds).run(
        command,
        launch_request=launch_request,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        environment=environment,
        process_start_observer=process_start_observer,
        process_attempt_observer=process_attempt_observer,
        process_drain_observer=process_drain_observer,
        process_terminal_observer=process_terminal_observer,
        cancellation_event=cancellation_event,
    )


def require_job_empty(job_name: str, *, primitives: Win32ProcessPrimitives | None = None) -> bool:
    """Fail closed unless an existing named mutation Job is already empty."""
    provider = primitives or Win32CtypesProcessPrimitives()
    job = provider.open_job(job_name)
    if job is None:
        return False
    try:
        provider.wait_job_empty(job, timeout_seconds=0.0)
        return True
    finally:
        provider.close_job(job)


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class Win32CtypesProcessPrimitives:
    """ctypes provider that never resumes a child before Job assignment."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    CREATE_SUSPENDED = 0x4
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESTDHANDLES = 0x100
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
    JOB_OBJECT_QUERY = 0x4
    JOB_OBJECT_TERMINATE = 0x8
    ERROR_FILE_NOT_FOUND = 2
    ERROR_INVALID_PARAMETER = 87
    ERROR_ALREADY_EXISTS = 183
    INVALID_DWORD = 0xFFFFFFFF
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self, kernel32: object | None = None) -> None:
        if kernel32 is None:
            if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
                raise ROIAnalysisError("native Win32 process containment is unavailable")
            _require_supported_windows_host(
                sys.getwindowsversion().major,
                ctypes.sizeof(ctypes.c_void_p),
            )
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32 = kernel32
        self._configure()

    def _configure(self) -> None:
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "CreateProcessW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    ctypes.c_void_p,
                    ctypes.POINTER(_PROCESS_INFORMATION),
                ],
                wintypes.BOOL,
            ),
            "InitializeProcThreadAttributeList": (
                [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)],
                wintypes.BOOL,
            ),
            "UpdateProcThreadAttribute": (
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "GetExitCodeProcess": (
                [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            "GetProcessTimes": (
                [
                    wintypes.HANDLE,
                    ctypes.POINTER(_FILETIME),
                    ctypes.POINTER(_FILETIME),
                    ctypes.POINTER(_FILETIME),
                    ctypes.POINTER(_FILETIME),
                ],
                wintypes.BOOL,
            ),
            "ProcessIdToSessionId": (
                [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "IsProcessInJob": (
                [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel32, name, None)
            if function is not None and hasattr(function, "argtypes"):
                function.argtypes = args
                function.restype = result

    @staticmethod
    def _last_error() -> int:
        getter = getattr(ctypes, "get_last_error", None)
        return 0 if getter is None else int(getter())

    def _failure(self, operation: str) -> OSError:
        code = self._last_error()
        return OSError(code, f"{operation} failed", None, code)

    def _truthy(self, operation: str, *args: object) -> object:
        result = getattr(self.kernel32, operation)(*args)
        if not result:
            raise self._failure(operation)
        return result

    def create_job(self, name: str) -> object:
        setter = getattr(ctypes, "set_last_error", None)
        if setter is not None:
            setter(0)
        handle = getattr(self.kernel32, "CreateJobObjectW")(None, name)
        error = self._last_error()
        if not handle:
            raise self._failure("CreateJobObjectW")
        if error == self.ERROR_ALREADY_EXISTS:
            getattr(self.kernel32, "CloseHandle")(handle)
            raise ROIAnalysisError("authoritative mutation Job already exists")
        return int(handle)

    def open_job(self, name: str) -> object | None:
        handle = getattr(self.kernel32, "OpenJobObjectW")(
            self.JOB_OBJECT_QUERY | self.JOB_OBJECT_TERMINATE,
            False,
            name,
        )
        if handle:
            return int(handle)
        if self._last_error() == self.ERROR_FILE_NOT_FOUND:
            return None
        raise self._failure("OpenJobObjectW")

    def set_kill_on_close(self, job: object) -> None:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._truthy(
            "SetInformationJobObject",
            job,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

    def create_suspended(
        self,
        command: tuple[str, ...],
        job: object,
        *,
        stdout_log: object,
        stderr_log: object,
        environment: Mapping[str, str] | None = None,
        process_attempt_observer: ProcessAttemptObserver | None = None,
    ) -> Win32ContainedProcess:
        executable = shutil.which(command[0])
        if executable is None:
            candidate = Path(command[0])
            if not candidate.is_file():
                raise FileNotFoundError(command[0])
            executable = str(candidate.resolve())
        owned_streams: list[object] = []
        handles: tuple[wintypes.HANDLE, ...] = ()
        inheritable_handles: list[wintypes.HANDLE] = []
        attribute_storage: object | None = None
        attribute_pointer: ctypes.c_void_p | None = None
        initialized = False
        process: Win32ContainedProcess | None = None
        failure: BaseException | None = None
        identity_captured = False
        try:
            stdin = open(os.devnull, "rb")
            owned_streams.append(stdin)
            handles = tuple(
                wintypes.HANDLE(msvcrt_handle(stream.fileno()))
                for stream in (stdin, stdout_log, stderr_log)
            )
            if len({int(handle.value) for handle in handles}) != len(handles):
                raise ROIAnalysisError("Win32 child standard handles must be distinct")
            for handle in handles:
                os.set_handle_inheritable(int(handle.value), True)
                inheritable_handles.append(handle)
            size = ctypes.c_size_t()
            getattr(self.kernel32, "InitializeProcThreadAttributeList")(
                None, 2, 0, ctypes.byref(size)
            )
            if size.value <= 0:
                raise self._failure("InitializeProcThreadAttributeList")
            attribute_storage = ctypes.create_string_buffer(size.value)
            attribute_pointer = ctypes.cast(attribute_storage, ctypes.c_void_p)
            self._truthy(
                "InitializeProcThreadAttributeList",
                attribute_pointer,
                2,
                0,
                ctypes.byref(size),
            )
            initialized = True
            handle_array = (wintypes.HANDLE * len(handles))(*[handle.value for handle in handles])
            self._truthy(
                "UpdateProcThreadAttribute",
                attribute_pointer,
                0,
                self.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handle_array,
                ctypes.sizeof(handle_array),
                None,
                None,
            )
            job_array = (wintypes.HANDLE * 1)(job)
            self._truthy(
                "UpdateProcThreadAttribute",
                attribute_pointer,
                0,
                self.PROC_THREAD_ATTRIBUTE_JOB_LIST,
                job_array,
                ctypes.sizeof(job_array),
                None,
                None,
            )
            startup = _STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = self.STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = handles[0]
            startup.StartupInfo.hStdOutput = handles[1]
            startup.StartupInfo.hStdError = handles[2]
            startup.lpAttributeList = attribute_pointer
            information = _PROCESS_INFORMATION()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            environment_block = None
            creation_flags = self.CREATE_SUSPENDED | self.EXTENDED_STARTUPINFO_PRESENT
            if environment is not None:
                items: list[str] = []
                for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
                    if (
                        not isinstance(key, str)
                        or not key
                        or "=" in key
                        or "\x00" in key
                        or not isinstance(value, str)
                        or "\x00" in value
                    ):
                        raise ROIAnalysisError("Win32 child environment is invalid")
                    items.append(f"{key}={value}")
                environment_block = ctypes.create_unicode_buffer("\x00".join(items) + "\x00\x00")
                creation_flags |= self.CREATE_UNICODE_ENVIRONMENT
            create_process = getattr(self.kernel32, "CreateProcessW")
            environment_pointer = (
                None
                if environment_block is None
                else ctypes.cast(environment_block, ctypes.c_void_p)
            )
            startup_pointer = ctypes.byref(startup)
            information_pointer = ctypes.byref(information)
            if process_attempt_observer is not None:
                process_attempt_observer()
            if not create_process(
                executable,
                command_line,
                None,
                None,
                True,
                creation_flags,
                environment_pointer,
                None,
                startup_pointer,
                information_pointer,
            ):
                raise self._failure("CreateProcessW")
            process = Win32ContainedProcess(
                int(information.hProcess),
                int(information.hThread),
                stdout_log,
                stderr_log,
                stdin,
                int(information.dwProcessId),
                0,
                0,
            )
            identity_failure: BaseException | None = None
            try:
                observed_process_id, observed_start_epoch_millis = self.process_start_identity(
                    process
                )
                process.process_id = observed_process_id
                process.start_epoch_millis = observed_start_epoch_millis
            except BaseException as exc:
                identity_failure = exc
            try:
                process.child_session_id = self.process_session_id(process)
            except BaseException as exc:
                if identity_failure is None:
                    identity_failure = exc
                else:
                    _add_secondary_note(
                        identity_failure,
                        "Win32 child-session query also failed",
                        exc,
                    )
            if identity_failure is not None:
                raise identity_failure
            identity_captured = True
        except BaseException as exc:
            failure = exc

        cleanup_failures: list[BaseException] = []
        try:
            if initialized:
                getattr(self.kernel32, "DeleteProcThreadAttributeList")(attribute_pointer)
        except BaseException as exc:
            cleanup_failures.append(exc)
        for handle in reversed(inheritable_handles):
            try:
                os.set_handle_inheritable(int(handle.value), False)
            except BaseException as exc:
                cleanup_failures.append(exc)

        if failure is not None or cleanup_failures:
            primary = failure or cleanup_failures.pop(0)
            assert primary is not None
            for secondary in cleanup_failures:
                _add_secondary_note(
                    primary,
                    "Win32 suspended-process setup cleanup also failed",
                    secondary,
                )
            job_drained = False
            if process is not None:
                try:
                    _terminate_job_and_wait_empty(
                        self,
                        job,
                        timeout_seconds=30.0,
                    )
                except BaseException as secondary:
                    _add_secondary_note(
                        primary,
                        "Win32 post-create Job drain also failed",
                        secondary,
                    )
                else:
                    job_drained = True
                try:
                    self.close_process(process)
                except BaseException as secondary:
                    _add_secondary_note(
                        primary,
                        "Win32 post-create process cleanup also failed",
                        secondary,
                    )
                post_create = Win32PostCreateFailure(
                    primary,
                    process_id=process.process_id if identity_captured else None,
                    start_epoch_millis=(process.start_epoch_millis if identity_captured else None),
                    child_session_id=(process.child_session_id if identity_captured else None),
                    job_drained=job_drained,
                )
                raise post_create from primary
            else:
                for stream in reversed(owned_streams):
                    try:
                        stream.close()
                    except BaseException as secondary:
                        _add_secondary_note(
                            primary,
                            "Win32 setup stream cleanup also failed",
                            secondary,
                        )
            raise primary

        assert process is not None
        return process

    def resume(self, process: Win32ContainedProcess) -> None:
        count = getattr(self.kernel32, "ResumeThread")(process.thread_handle)
        if count != 1:
            if count == self.INVALID_DWORD:
                raise self._failure("ResumeThread")
            raise ROIAnalysisError("suspended QuPath thread count is unsafe")

    def process_start_identity(self, process: Win32ContainedProcess) -> tuple[int, int]:
        return self._process_start_identity_from_handle(process.process_handle, process.process_id)

    def process_session_id(self, process: Win32ContainedProcess) -> int:
        if (
            not process.process_handle
            or type(process.process_id) is not int
            or process.process_id <= 0
        ):
            raise ROIAnalysisError("Win32 process identity is invalid")
        session_id = wintypes.DWORD()
        self._truthy(
            "ProcessIdToSessionId",
            process.process_id,
            ctypes.byref(session_id),
        )
        return int(session_id.value)

    def _process_start_identity_from_handle(
        self, process_handle: object, process_id: int
    ) -> tuple[int, int]:
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        self._truthy(
            "GetProcessTimes",
            process_handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        windows_ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        unix_epoch_ticks = 116_444_736_000_000_000
        if process_id <= 0 or windows_ticks < unix_epoch_ticks:
            raise ROIAnalysisError("Win32 process start identity is invalid")
        return (
            process_id,
            (windows_ticks - unix_epoch_ticks) // 10_000,
        )

    def process_start_identity_by_id(self, process_id: int) -> tuple[int, int] | None:
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise ROIAnalysisError("Win32 process ID is invalid")
        handle = getattr(self.kernel32, "OpenProcess")(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            error = ctypes.get_last_error()
            if error == self.ERROR_INVALID_PARAMETER:
                return None
            raise self._failure("OpenProcess")
        try:
            return self._process_start_identity_from_handle(handle, process_id)
        finally:
            self._truthy("CloseHandle", handle)

    def is_exact_process_running(
        self,
        process_id: int,
        start_epoch_millis: int,
        *,
        wait_timeout_millis: int = 0,
    ) -> bool:
        """Test the running state of one PID/start identity, including exited handles."""
        if (
            type(process_id) is not int
            or process_id <= 0
            or type(start_epoch_millis) is not int
            or start_epoch_millis <= 0
        ):
            raise ROIAnalysisError("Win32 process identity is invalid")
        if type(wait_timeout_millis) is not int or not 0 <= wait_timeout_millis <= 2000:
            raise ROIAnalysisError("Win32 process wait timeout is invalid")
        handle = getattr(self.kernel32, "OpenProcess")(
            self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE,
            False,
            process_id,
        )
        if not handle:
            if self._last_error() == self.ERROR_INVALID_PARAMETER:
                return False
            raise self._failure("OpenProcess")
        try:
            _, observed_start = self._process_start_identity_from_handle(handle, process_id)
            if observed_start != start_epoch_millis:
                return False
            state = getattr(self.kernel32, "WaitForSingleObject")(handle, wait_timeout_millis)
            if state == self.WAIT_TIMEOUT:
                return True
            if state == self.WAIT_OBJECT_0:
                return False
            if state == self.WAIT_FAILED:
                raise self._failure("WaitForSingleObject")
            raise ROIAnalysisError("Win32 process wait state is invalid")
        finally:
            self._truthy("CloseHandle", handle)

    def _pin_process_for_recovery(self, process_id: int) -> _PinnedWin32Process | None:
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise ROIAnalysisError("Win32 process ID is invalid")
        handle = getattr(self.kernel32, "OpenProcess")(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            if self._last_error() == self.ERROR_INVALID_PARAMETER:
                return None
            raise self._failure("OpenProcess")
        try:
            observed_process_id, start_epoch_millis = self._process_start_identity_from_handle(
                handle, process_id
            )
            return _PinnedWin32Process(
                process_handle=int(handle),
                process_id=observed_process_id,
                start_epoch_millis=start_epoch_millis,
            )
        except BaseException as primary:
            try:
                self._truthy("CloseHandle", handle)
            except Exception as secondary:
                _add_secondary_note(
                    primary,
                    "Win32 recovery process-handle cleanup also failed",
                    secondary,
                )
            raise

    def _is_pinned_process_in_job(self, process: _PinnedWin32Process, job: object) -> bool:
        if type(process) is not _PinnedWin32Process or not process.process_handle:
            raise ROIAnalysisError("pinned Win32 process identity is invalid")
        is_member = wintypes.BOOL()
        self._truthy(
            "IsProcessInJob",
            process.process_handle,
            job,
            ctypes.byref(is_member),
        )
        return bool(is_member.value)

    def _close_pinned_process(self, process: _PinnedWin32Process) -> None:
        if type(process) is not _PinnedWin32Process or not process.process_handle:
            raise ROIAnalysisError("pinned Win32 process identity is invalid")
        self._truthy("CloseHandle", process.process_handle)
        process.process_handle = 0

    def wait_job_empty(self, job: object, *, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ROIAnalysisError("Windows Job-empty timeout is invalid")
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        while True:
            info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            self._truthy(
                "QueryInformationJobObject",
                job,
                self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            if info.ActiveProcesses == 0:
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    "authoritative mutation Job did not terminate before its deadline"
                )
            time.sleep(0.025)

    def exit_code(self, process: Win32ContainedProcess) -> int:
        code = wintypes.DWORD()
        self._truthy("GetExitCodeProcess", process.process_handle, ctypes.byref(code))
        return int(code.value)

    def terminate_job(self, job: object) -> None:
        self._truthy("TerminateJobObject", job, 1)

    def terminate_process(self, process: Win32ContainedProcess) -> None:
        self._truthy("TerminateProcess", process.process_handle, 1)

    def close_process(self, process: Win32ContainedProcess) -> None:
        cleanup_error: BaseException | None = None
        for handle_name in ("thread_handle", "process_handle"):
            handle = getattr(process, handle_name)
            if handle:
                try:
                    self._truthy("CloseHandle", handle)
                    setattr(process, handle_name, 0)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
        try:
            process.stdin.close()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise cleanup_error

    def close_job(self, job: object) -> None:
        self._truthy("CloseHandle", job)


def msvcrt_handle(descriptor: int) -> int:
    if os.name != "nt":
        raise ROIAnalysisError("Win32 file-handle conversion is unavailable")
    import msvcrt

    return int(msvcrt.get_osfhandle(descriptor))
