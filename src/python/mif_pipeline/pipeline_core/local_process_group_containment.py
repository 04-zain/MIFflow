"""Explicit trusted-local Linux process-group containment.

This backend makes only a bounded, within-session process-group drain claim. It deliberately
has no recovery surface and is never selected implicitly.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
import os
import platform
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time
from threading import RLock
from typing import Callable, Protocol, runtime_checkable

from .errors import ROIAnalysisError
from .process_containment import (
    ContainedProcessResult,
    ContainmentLaunchRequest,
    LinuxLocalProcessGroupAttachmentV1,
    LinuxLocalProcessGroupDrainV1,
    ProcessAttemptObserver,
    ProcessDrainObserver,
    ProcessStartObserver,
    _ContainedProcessError,
    _ContainedProcessFailure,
)

_SHELL_PATH = "/bin/sh"
_READY_BYTE = b"R"
_RELEASE_LINE = b"release\n"
_MAXIMUM_LOG_EXCERPT_BYTES = 65_536
_MAXIMUM_CLEANUP_SECONDS = 5.0
_MINIMUM_CLEANUP_SECONDS = 0.05
_CANCELLATION_POLL_SECONDS = 0.05
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GLIBC_MINIMUM_CLOSEFROM_VERSION = (2, 34)
_GLIBC_X86_64_FILE_ACTIONS_BYTES = 80
_GLIBC_X86_64_SPAWN_ATTRIBUTES_BYTES = 336


# ==============================================================================
#                        Local process group containment
# ==============================================================================


class _GlibcSpawnFileActionsStorage(ctypes.Union):
    _fields_ = (
        (
            "bytes",
            ctypes.c_ubyte * _GLIBC_X86_64_FILE_ACTIONS_BYTES,
        ),
        ("alignment", ctypes.c_longdouble),
    )


class _GlibcSpawnAttributesStorage(ctypes.Union):
    _fields_ = (
        (
            "bytes",
            ctypes.c_ubyte * _GLIBC_X86_64_SPAWN_ATTRIBUTES_BYTES,
        ),
        ("alignment", ctypes.c_longdouble),
    )


class _CancellationRequested(Exception):
    """Internal synchronous cancellation marker observed at a safe boundary."""


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


@dataclass(frozen=True)
class _CloseFromSpawnResult:
    process_id: int
    cleanup_failures: tuple[BaseException, ...]


def _posix_spawn_closefrom(
    executable: str,
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    remaps: tuple[tuple[int, int], ...],
    closefrom_start: int,
    setsid: bool,
    process_attempt_observer: ProcessAttemptObserver | None,
) -> _CloseFromSpawnResult:
    """Spawn with GNU's child-atomic close-all-except file action.

    Python 3.12 does not expose ``posix_spawn_file_actions_addclosefrom_np``.
    This Linux-only adapter uses aligned opaque storage for the libc-owned
    action/attribute objects and fails closed when the GNU primitive is absent.
    All fallible setup precedes the attempt callback; the native spawn call is
    the next operation, so descriptors opened concurrently or by that callback
    are still closed in the child.
    """

    if (
        os.name != "posix"
        or not sys.platform.startswith("linux")
        or type(executable) is not str
        or not os.path.isabs(executable)
        or "\x00" in executable
        or type(argv) is not tuple
        or not argv
        or any(type(value) is not str or "\x00" in value for value in argv)
        or type(environment) is not dict
        or any(
            type(name) is not str
            or not name
            or "=" in name
            or "\x00" in name
            or type(value) is not str
            or "\x00" in value
            for name, value in environment.items()
        )
        or type(remaps) is not tuple
        or not remaps
        or any(
            type(remap) is not tuple
            or len(remap) != 2
            or any(type(descriptor) is not int for descriptor in remap)
            for remap in remaps
        )
        or type(closefrom_start) is not int
        or closefrom_start < 3
        or type(setsid) is not bool
        or (process_attempt_observer is not None and not callable(process_attempt_observer))
    ):
        raise ROIAnalysisError("Linux close-from spawn inputs are invalid")
    sources = tuple(source for source, _target in remaps)
    targets = tuple(target for _source, target in remaps)
    if (
        len(set(targets)) != len(targets)
        or any(source < closefrom_start for source in sources)
        or any(target <= 0 or target >= closefrom_start for target in targets)
        or closefrom_start != max(targets) + 1
    ):
        raise ROIAnalysisError("Linux close-from spawn remaps are invalid")

    try:
        executable_bytes = os.fsencode(executable)
        argv_bytes = tuple(os.fsencode(value) for value in argv)
        environment_bytes = tuple(
            os.fsencode(f"{name}={value}") for name, value in sorted(environment.items())
        )
        libc = ctypes.CDLL(None, use_errno=True)
        glibc_version = libc.gnu_get_libc_version
        actions_init = libc.posix_spawn_file_actions_init
        actions_destroy = libc.posix_spawn_file_actions_destroy
        add_open = libc.posix_spawn_file_actions_addopen
        add_close = libc.posix_spawn_file_actions_addclose
        add_dup2 = libc.posix_spawn_file_actions_adddup2
        add_closefrom = libc.posix_spawn_file_actions_addclosefrom_np
        spawn = libc.posix_spawn
        attr_init = libc.posix_spawnattr_init
        attr_destroy = libc.posix_spawnattr_destroy
        attr_setflags = libc.posix_spawnattr_setflags
    except (AttributeError, OSError, UnicodeError) as exc:
        raise ROIAnalysisError("Linux atomic close-from spawn is unavailable") from exc

    if (
        platform.machine().lower() not in {"x86_64", "amd64"}
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(_GlibcSpawnFileActionsStorage) != _GLIBC_X86_64_FILE_ACTIONS_BYTES
        or ctypes.sizeof(_GlibcSpawnAttributesStorage) != _GLIBC_X86_64_SPAWN_ATTRIBUTES_BYTES
    ):
        raise ROIAnalysisError("Linux atomic close-from spawn ABI is unsupported")

    glibc_version.argtypes = []
    glibc_version.restype = ctypes.c_char_p
    try:
        raw_glibc_version = glibc_version()
        if not isinstance(raw_glibc_version, bytes):
            raise ValueError("missing glibc version")
        version_parts = raw_glibc_version.decode("ascii", errors="strict").split(".")
        version = (int(version_parts[0]), int(version_parts[1]))
    except (IndexError, UnicodeError, ValueError) as exc:
        raise ROIAnalysisError("Linux atomic close-from spawn requires identifiable glibc") from exc
    if version < _GLIBC_MINIMUM_CLOSEFROM_VERSION:
        raise ROIAnalysisError("Linux atomic close-from spawn requires glibc 2.34 or newer")

    opaque_pointer = ctypes.c_void_p
    actions_init.argtypes = [opaque_pointer]
    actions_init.restype = ctypes.c_int
    actions_destroy.argtypes = [opaque_pointer]
    actions_destroy.restype = ctypes.c_int
    add_open.argtypes = [
        opaque_pointer,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    add_open.restype = ctypes.c_int
    add_close.argtypes = [opaque_pointer, ctypes.c_int]
    add_close.restype = ctypes.c_int
    add_dup2.argtypes = [opaque_pointer, ctypes.c_int, ctypes.c_int]
    add_dup2.restype = ctypes.c_int
    add_closefrom.argtypes = [opaque_pointer, ctypes.c_int]
    add_closefrom.restype = ctypes.c_int
    attr_init.argtypes = [opaque_pointer]
    attr_init.restype = ctypes.c_int
    attr_destroy.argtypes = [opaque_pointer]
    attr_destroy.restype = ctypes.c_int
    attr_setflags.argtypes = [opaque_pointer, ctypes.c_short]
    attr_setflags.restype = ctypes.c_int
    char_array = ctypes.POINTER(ctypes.c_char_p)
    spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        opaque_pointer,
        opaque_pointer,
        char_array,
        char_array,
    ]
    spawn.restype = ctypes.c_int

    # These are the verified glibc x86-64 ABI sizes.  Unrecognized libc,
    # architecture, and word-size combinations fail before the attempt callback.
    actions_storage = _GlibcSpawnFileActionsStorage()
    attributes_storage = _GlibcSpawnAttributesStorage()
    actions_pointer = ctypes.cast(ctypes.byref(actions_storage), opaque_pointer)
    attributes_pointer = ctypes.cast(ctypes.byref(attributes_storage), opaque_pointer)
    argv_array = (ctypes.c_char_p * (len(argv_bytes) + 1))(*argv_bytes, None)
    environment_array = (ctypes.c_char_p * (len(environment_bytes) + 1))(*environment_bytes, None)

    actions_initialized = False
    attributes_initialized = False
    primary: BaseException | None = None
    process_id: int | None = None
    cleanup_failures: list[BaseException] = []

    def require_success(status_value: int, operation: str) -> None:
        if status_value != 0:
            raise OSError(
                status_value,
                f"{operation}: {os.strerror(status_value)}",
            )

    try:
        require_success(actions_init(actions_pointer), "posix spawn actions init")
        actions_initialized = True
        require_success(
            add_open(
                actions_pointer,
                0,
                os.fsencode(os.devnull),
                os.O_RDONLY,
                0,
            ),
            "posix spawn stdin action",
        )
        for source, target in remaps:
            require_success(
                add_dup2(actions_pointer, source, target),
                "posix spawn descriptor remap",
            )
        target_set = set(targets)
        for descriptor in range(3, closefrom_start):
            if descriptor not in target_set:
                require_success(
                    add_close(actions_pointer, descriptor),
                    "posix spawn low descriptor close",
                )
        require_success(
            add_closefrom(actions_pointer, closefrom_start),
            "posix spawn close-from action",
        )
        attribute_argument = None
        if setsid:
            require_success(attr_init(attributes_pointer), "posix spawn attr init")
            attributes_initialized = True
            require_success(
                attr_setflags(attributes_pointer, 0x80),
                "posix spawn setsid attribute",
            )
            attribute_argument = attributes_pointer
        native_process_id = ctypes.c_int()
        native_process_pointer = ctypes.byref(native_process_id)
        if process_attempt_observer is not None:
            process_attempt_observer()
        spawn_status = spawn(
            native_process_pointer,
            executable_bytes,
            actions_pointer,
            attribute_argument,
            argv_array,
            environment_array,
        )
        require_success(
            spawn_status,
            "posix spawn",
        )
        process_id = int(native_process_id.value)
        if process_id <= 0:
            raise ROIAnalysisError("Linux close-from spawn returned invalid identity")
    except BaseException as exc:
        primary = exc
    finally:
        if attributes_initialized:
            try:
                require_success(
                    attr_destroy(attributes_pointer),
                    "posix spawn attr destroy",
                )
            except BaseException as exc:
                cleanup_failures.append(exc)
        if actions_initialized:
            try:
                require_success(
                    actions_destroy(actions_pointer),
                    "posix spawn actions destroy",
                )
            except BaseException as exc:
                cleanup_failures.append(exc)

    if primary is not None:
        for cleanup in cleanup_failures:
            _add_bounded_secondary_note(
                primary,
                cleanup,
                context="native close-from spawn cleanup also failed",
            )
        raise primary
    if process_id is None:
        if cleanup_failures:
            raise cleanup_failures[0]
        raise ROIAnalysisError("Linux close-from spawn did not return identity")
    return _CloseFromSpawnResult(process_id, tuple(cleanup_failures))


@dataclass
class _LocalSpawnedProcess:
    """Minimal wait handle for the safely file-actioned local child."""

    pid: int
    args: tuple[str, ...]
    returncode: int | None = None
    staging_cleanup_failure: BaseException | None = None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            options = 0 if deadline is None else os.WNOHANG
            waited, status_value = os.waitpid(self.pid, options)
            if waited == self.pid:
                self.returncode = os.waitstatus_to_exitcode(status_value)
                break
            if waited != 0:
                raise ROIAnalysisError("local contained process wait identity is invalid")
            assert deadline is not None
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        waited, status_value = os.waitpid(self.pid, os.WNOHANG)
        if waited == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status_value)
        elif waited != 0:
            raise ROIAnalysisError("local contained process poll identity is invalid")
        return self.returncode


@dataclass(frozen=True)
class _GroupDrainOutcome:
    group_empty_proved: bool
    leader_reaped: bool
    failures: tuple[BaseException, ...]

    @property
    def complete(self) -> bool:
        return self.group_empty_proved and self.leader_reaped


# ``sh -c`` binds the first word after the script to $0.  The two descriptor
# numbers are therefore $1/$2, and shifting them exposes only the target argv
# to the final exec.  The numbers originate in this process and are validated
# before launch; eval is needed because POSIX redirection grammar does not
# permit a parameter expansion in the IO-number position.
GATE_SCRIPT = """\
ready_fd=$1
release_fd=$2
shift 2
case $ready_fd:$release_fd in
    *[!0-9:]*|:*|*:) exit 64 ;;
esac
eval "printf R >&${ready_fd}" || exit 65
if eval "IFS= read -r gate_line <&${release_fd}"; then
    :
else
    exit 66
fi
[ "$gate_line" = release ] || exit 67
eval "exec ${ready_fd}>&-" || exit 68
eval "exec ${release_fd}<&-" || exit 69
exec "$@"
"""


@runtime_checkable
class LinuxLocalProcessGroupOperations(Protocol):
    """Every operating-system interaction used by the local executor."""

    def authenticate_shell(self, shell_path: str) -> tuple[int, int]: ...

    def create_pipe(self) -> tuple[int, int]: ...

    def spawn(
        self,
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...],
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
        process_attempt_observer: ProcessAttemptObserver | None,
    ) -> object: ...

    def close_fd(self, descriptor: int) -> None: ...

    def wait_ready(self, descriptor: int, *, timeout_seconds: float) -> None: ...

    def observe_identity(
        self,
        process: object,
        *,
        shell_identity: tuple[int, int],
    ) -> tuple[int, int]: ...

    def write_release(self, descriptor: int) -> None: ...

    def wait_leader(
        self,
        process: object,
        *,
        timeout_seconds: float | None,
    ) -> int: ...

    def wait_group_empty(
        self,
        process: object,
        process_group_id: int,
        *,
        timeout_seconds: float,
    ) -> bool: ...

    def signal_group(self, process_group_id: int, signal_number: int) -> None: ...

    def reap_leader(self, process: object, *, timeout_seconds: float) -> None: ...

    def finalize_logs(
        self,
        stdout_log: object,
        stderr_log: object,
    ) -> tuple[str, str, bool, bool]: ...


class _LinuxLocalProcessGroupOperations:
    """Standard-library implementation for the supported Ubuntu host."""

    @staticmethod
    def _process_id(process: object) -> int:
        process_id = getattr(process, "pid", None)
        if type(process_id) is not int or process_id <= 0:
            raise ROIAnalysisError("local contained process ID is invalid")
        return process_id

    @staticmethod
    def _read_proc(path: Path, *, maximum_bytes: int, label: str) -> str:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                try:
                    chunk = os.read(descriptor, min(remaining, 16 * 1024))
                except InterruptedError:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        except OSError as exc:
            raise ROIAnalysisError(f"{label} cannot be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(payload) > maximum_bytes:
            raise ROIAnalysisError(f"{label} exceeds its bounded size")
        try:
            return payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ROIAnalysisError(f"{label} is not strict UTF-8") from exc

    @classmethod
    def _stat_fields(cls, process_id: int) -> tuple[str, int, int]:
        payload = cls._read_proc(
            Path(f"/proc/{process_id}/stat"),
            maximum_bytes=16 * 1024,
            label="local contained process identity",
        )
        closing = payload.rfind(")")
        if closing < 0:
            raise ROIAnalysisError("local contained process identity is invalid")
        fields = payload[closing + 2 :].split()
        try:
            state = fields[0]
            process_group_id = int(fields[2])
            start_ticks = int(fields[19])
        except (IndexError, ValueError) as exc:
            raise ROIAnalysisError("local contained process identity is invalid") from exc
        if len(state) != 1 or process_group_id < 0 or start_ticks < 0:
            raise ROIAnalysisError("local contained process identity is invalid")
        return state, process_group_id, start_ticks

    @classmethod
    def _start_epoch_millis(cls, start_ticks: int) -> int:
        boot_lines = cls._read_proc(
            Path("/proc/stat"),
            maximum_bytes=1024 * 1024,
            label="local boot identity",
        ).splitlines()
        try:
            boot_epoch_seconds = int(
                next(line for line in boot_lines if line.startswith("btime ")).split()[1]
            )
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        except (StopIteration, IndexError, ValueError, OSError) as exc:
            raise ROIAnalysisError(
                "local contained process start epoch cannot be observed"
            ) from exc
        if boot_epoch_seconds <= 0 or ticks_per_second <= 0:
            raise ROIAnalysisError("local contained process start epoch is invalid")
        return boot_epoch_seconds * 1000 + start_ticks * 1000 // ticks_per_second

    def authenticate_shell(self, shell_path: str) -> tuple[int, int]:
        if shell_path != _SHELL_PATH:
            raise ROIAnalysisError("local containment shell path is unsupported")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                shell_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            identity = os.fstat(descriptor)
        except OSError as exc:
            raise ROIAnalysisError("local containment shell cannot be opened") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_mode & 0o111 == 0:
            raise ROIAnalysisError("local containment shell is not executable")
        return identity.st_dev, identity.st_ino

    def create_pipe(self) -> tuple[int, int]:
        pipe2 = getattr(os, "pipe2", None)
        if callable(pipe2):
            return pipe2(getattr(os, "O_CLOEXEC", 0))
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        return read_fd, write_fd

    def spawn(
        self,
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...],
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
        process_attempt_observer: ProcessAttemptObserver | None,
    ) -> object:
        import fcntl

        if (
            len(argv) < 7
            or argv[:4] != (_SHELL_PATH, "-c", GATE_SCRIPT, "mif-local-gate")
            or len(pass_fds) < 2
            or tuple(dict.fromkeys(pass_fds)) != pass_fds
            or any(type(descriptor) is not int or descriptor < 0 for descriptor in pass_fds)
        ):
            raise ROIAnalysisError("local containment spawn authority is invalid")
        ready_write, release_read, *lock_descriptors = pass_fds
        try:
            stdout_descriptor = stdout_log.fileno()  # type: ignore[attr-defined]
            stderr_descriptor = stderr_log.fileno()  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError) as exc:
            raise ROIAnalysisError("local containment log handles are invalid") from exc
        sources = (
            ready_write,
            release_read,
            stdout_descriptor,
            stderr_descriptor,
            *lock_descriptors,
        )
        child_targets = (
            3,
            4,
            1,
            2,
            *(5 + offset for offset in range(len(lock_descriptors))),
        )
        # Stage every source above the complete child target range.  This makes
        # the file actions collision-free even when parent descriptors exceed
        # dash's single-digit dynamic-redirection limit, without preexec_fn.
        staged_minimum = max(child_targets) + 1
        duplicated: list[int] = []
        process: _LocalSpawnedProcess | None = None
        primary: BaseException | None = None
        try:
            for source in sources:
                duplicated.append(fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, staged_minimum))
            child_argv = (*argv[:4], "3", "4", *argv[6:])
            spawn_result = _posix_spawn_closefrom(
                _SHELL_PATH,
                child_argv,
                environment,
                remaps=tuple(zip(duplicated, child_targets, strict=True)),
                closefrom_start=staged_minimum,
                setsid=True,
                process_attempt_observer=process_attempt_observer,
            )
            process = _LocalSpawnedProcess(
                pid=spawn_result.process_id,
                args=argv,
            )
            if spawn_result.cleanup_failures:
                cleanup_primary = spawn_result.cleanup_failures[0]
                for cleanup in spawn_result.cleanup_failures[1:]:
                    _add_bounded_secondary_note(
                        cleanup_primary,
                        cleanup,
                        context=("additional native close-from cleanup failure"),
                    )
                process.staging_cleanup_failure = cleanup_primary
            return process
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
                for cleanup in cleanup_failures:
                    _add_bounded_secondary_note(
                        primary,
                        cleanup,
                        context="local staged descriptor cleanup also failed",
                    )
            elif cleanup_failures:
                cleanup_primary = cleanup_failures[0]
                for cleanup in cleanup_failures[1:]:
                    _add_bounded_secondary_note(
                        cleanup_primary,
                        cleanup,
                        context=("additional local staged descriptor cleanup failure"),
                    )
                if process is None:
                    raise cleanup_primary
                if process.staging_cleanup_failure is None:
                    process.staging_cleanup_failure = cleanup_primary
                else:
                    _add_bounded_secondary_note(
                        process.staging_cleanup_failure,
                        cleanup_primary,
                        context="local staged descriptor cleanup also failed",
                    )

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    def wait_ready(self, descriptor: int, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("local execution gate readiness timed out")
            try:
                readable, _, _ = select.select((descriptor,), (), (), remaining)
            except InterruptedError:
                continue
            if not readable:
                raise TimeoutError("local execution gate readiness timed out")
            try:
                ready = os.read(descriptor, 1)
            except InterruptedError:
                continue
            if ready != _READY_BYTE:
                raise ROIAnalysisError("local execution gate readiness is invalid")
            return

    def observe_identity(
        self,
        process: object,
        *,
        shell_identity: tuple[int, int],
    ) -> tuple[int, int]:
        process_id = self._process_id(process)
        try:
            executable = os.stat(f"/proc/{process_id}/exe")
        except OSError as exc:
            raise ROIAnalysisError(
                "local execution gate executable cannot be authenticated"
            ) from exc
        if (executable.st_dev, executable.st_ino) != shell_identity:
            raise ROIAnalysisError("local execution gate is not the authenticated shell")
        _state, process_group_id, start_ticks = self._stat_fields(process_id)
        if process_group_id != process_id:
            raise ROIAnalysisError("local execution gate did not establish its own group")
        try:
            observed_group = os.getpgid(process_id)
        except OSError as exc:
            raise ROIAnalysisError("local execution gate process group cannot be observed") from exc
        if observed_group != process_group_id:
            raise ROIAnalysisError("local execution gate process group changed")
        return self._start_epoch_millis(start_ticks), process_group_id

    def write_release(self, descriptor: int) -> None:
        remaining = memoryview(_RELEASE_LINE)
        while remaining:
            try:
                written = os.write(descriptor, remaining)
            except InterruptedError:
                continue
            if written <= 0:
                raise ROIAnalysisError("local execution gate release failed")
            remaining = remaining[written:]

    def wait_leader(
        self,
        process: object,
        *,
        timeout_seconds: float | None,
    ) -> int:
        process_id = self._process_id(process)
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            options = os.WEXITED | os.WNOWAIT
            if deadline is not None:
                options |= os.WNOHANG
            try:
                result = os.waitid(os.P_PID, process_id, options)
            except InterruptedError:
                continue
            except (ChildProcessError, OSError) as exc:
                raise ROIAnalysisError("local contained process exit cannot be observed") from exc
            if result is not None:
                if result.si_code == os.CLD_EXITED:
                    return result.si_status
                if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                    return -result.si_status
                raise ROIAnalysisError("local contained process exit status is invalid")
            assert deadline is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout_seconds)
            time.sleep(min(0.01, remaining))

    @classmethod
    def _group_is_empty(
        cls,
        process_group_id: int,
        leader_process_id: int,
    ) -> bool:
        try:
            entries = tuple(Path("/proc").iterdir())
        except OSError as exc:
            raise ROIAnalysisError("local process group cannot be inspected") from exc
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                observed_process_id = int(entry.name)
                state, observed_group, _start_ticks = cls._stat_fields(observed_process_id)
            except ROIAnalysisError as exc:
                if isinstance(exc.__cause__, (FileNotFoundError, ProcessLookupError)):
                    continue
                raise
            if observed_group == process_group_id and not (
                observed_process_id == leader_process_id and state == "Z"
            ):
                return False
        return True

    def wait_group_empty(
        self,
        process: object,
        process_group_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        leader_process_id = self._process_id(process)
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._group_is_empty(process_group_id, leader_process_id):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def signal_group(self, process_group_id: int, signal_number: int) -> None:
        while True:
            try:
                os.killpg(process_group_id, signal_number)
                return
            except InterruptedError:
                continue
            except ProcessLookupError:
                return
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return
                raise

    def reap_leader(self, process: object, *, timeout_seconds: float) -> None:
        waiter = getattr(process, "wait", None)
        if not callable(waiter):
            raise ROIAnalysisError("local contained process handle is invalid")
        waiter(timeout=timeout_seconds)

    @staticmethod
    def _read_log(stream: object) -> tuple[str, bool]:
        opener = getattr(stream, "open_reader", None)
        if callable(opener):
            with opener() as reader:
                reader.seek(0, os.SEEK_END)
                size = reader.tell()
                reader.seek(max(0, size - _MAXIMUM_LOG_EXCERPT_BYTES))
                raw = reader.read(_MAXIMUM_LOG_EXCERPT_BYTES)
        else:
            original = stream.tell()  # type: ignore[attr-defined]
            try:
                stream.seek(0, os.SEEK_END)  # type: ignore[attr-defined]
                size = stream.tell()  # type: ignore[attr-defined]
                stream.seek(max(0, size - _MAXIMUM_LOG_EXCERPT_BYTES))  # type: ignore[attr-defined]
                raw = stream.read(_MAXIMUM_LOG_EXCERPT_BYTES)  # type: ignore[attr-defined]
            finally:
                stream.seek(original)  # type: ignore[attr-defined]
        if type(size) is not int or size < 0 or type(raw) is not bytes:
            raise ROIAnalysisError("local contained log read view is invalid")
        excerpt = raw.decode("utf-8", errors="replace")
        encoded = excerpt.encode("utf-8")
        representation_truncated = len(encoded) > _MAXIMUM_LOG_EXCERPT_BYTES
        if representation_truncated:
            excerpt = encoded[-_MAXIMUM_LOG_EXCERPT_BYTES:].decode("utf-8", errors="ignore")
        return excerpt, size > _MAXIMUM_LOG_EXCERPT_BYTES or representation_truncated

    def finalize_logs(
        self,
        stdout_log: object,
        stderr_log: object,
    ) -> tuple[str, str, bool, bool]:
        failures: list[BaseException] = []
        for stream in (stdout_log, stderr_log):
            try:
                stream.flush()  # type: ignore[attr-defined]
                os.fsync(stream.fileno())  # type: ignore[attr-defined]
            except (AttributeError, OSError, ValueError) as exc:
                failures.append(exc)
        if failures:
            primary = failures[0]
            for failure in failures[1:]:
                _add_bounded_secondary_note(
                    primary,
                    failure,
                    context="additional local log sync failure",
                )
            raise ROIAnalysisError("local contained logs cannot be synchronized") from primary
        stdout, stdout_truncated = self._read_log(stdout_log)
        stderr, stderr_truncated = self._read_log(stderr_log)
        return stdout, stderr, stdout_truncated, stderr_truncated


class LinuxLocalProcessGroupExecutor:
    """Run one target behind a publication gate in a new Linux session."""

    def __init__(
        self,
        *,
        operations: LinuxLocalProcessGroupOperations | None = None,
    ) -> None:
        self._operations = operations or _LinuxLocalProcessGroupOperations()
        self._interactive_lock = RLock()
        self._interactive_active = False
        self._interactive_resources: tuple[object, ...] | None = None

    def require_interactive_stopped(self) -> None:
        """Reject active or unresolved GUI lifetime without new native inspection."""
        with self._interactive_lock:
            if self._interactive_active:
                raise ROIAnalysisError(
                    "interactive Linux process group has not been confirmed stopped"
                )

    def run_interactive(
        self,
        command: tuple[str, ...],
        *,
        stdout_log: object,
        stderr_log: object,
        environment: dict[str, str],
    ) -> ContainedProcessResult:
        """Wait for natural current-session group drain, without project authority."""
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
            raise ROIAnalysisError("interactive Linux process inputs are invalid")
        with self._interactive_lock:
            self.require_interactive_stopped()
            self._interactive_active = True
        operations = self._operations
        owned_fds: list[int] = []
        uncertain_fds: list[int] = []
        process = None
        process_id = None
        stopped = True
        release_attempted = False
        primary = None
        result = None

        def close_owned(descriptor: int) -> None:
            owned_fds.remove(descriptor)
            try:
                operations.close_fd(descriptor)
            except BaseException:
                # A failed close may have closed it already. Never retry an FD
                # that another thread could have reused for an unrelated file.
                uncertain_fds.append(descriptor)
                raise

        try:
            shell_identity = operations.authenticate_shell(_SHELL_PATH)
            ready_read, ready_write = operations.create_pipe()
            owned_fds.extend((ready_read, ready_write))
            release_read, release_write = operations.create_pipe()
            owned_fds.extend((release_read, release_write))
            process = operations.spawn(
                (
                    _SHELL_PATH,
                    "-c",
                    GATE_SCRIPT,
                    "mif-local-gate",
                    str(ready_write),
                    str(release_read),
                    *command,
                ),
                pass_fds=(ready_write, release_read),
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                environment=environment,
                process_attempt_observer=None,
            )
            stopped = False
            process_id = getattr(process, "pid", None)
            if type(process_id) is not int or process_id <= 0:
                raise ROIAnalysisError("interactive process ID is invalid")
            staging_failure = getattr(process, "staging_cleanup_failure", None)
            if staging_failure is not None:
                raise staging_failure
            close_owned(ready_write)
            close_owned(release_read)
            operations.wait_ready(ready_read, timeout_seconds=_MAXIMUM_CLEANUP_SECONDS)
            close_owned(ready_read)
            started, group = operations.observe_identity(process, shell_identity=shell_identity)
            if type(group) is not int or group != process_id:
                raise ROIAnalysisError("interactive execution gate identity is invalid")
            attachment = LinuxLocalProcessGroupAttachmentV1(
                provider="linux-local-process-group/v1",
                leader_process_id=process_id,
                leader_start_epoch_millis=started,
                process_group_id=group,
            )
            release_attempted = True
            operations.write_release(release_write)
            close_owned(release_write)
            returncode = operations.wait_leader(process, timeout_seconds=None)
            # Do not reap the leader before group proof: it anchors this PGID.
            while True:
                empty = operations.wait_group_empty(process, group, timeout_seconds=0.1)
                if type(empty) is not bool:
                    raise ROIAnalysisError("interactive group inspection result is invalid")
                if empty:
                    break
            operations.reap_leader(process, timeout_seconds=_MAXIMUM_CLEANUP_SECONDS)
            stopped = True
            drain = LinuxLocalProcessGroupDrainV1(
                provider="linux-local-process-group/v1",
                semantic_state="drained",
                leader_process_id=process_id,
                leader_start_epoch_millis=started,
                process_group_id=group,
                termination_reason="normal_exit" if returncode == 0 else "nonzero_exit",
            )
            logs = operations.finalize_logs(stdout_log, stderr_log)
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
        if not stopped and not release_attempted and type(process_id) is int and process_id > 0:
            try:
                outcome = self._drain_group(
                    process,
                    process_id,
                    cleanup_timeout=_MAXIMUM_CLEANUP_SECONDS,
                    allow_initial_exit=True,
                )
                stopped = outcome.complete
                for failure in outcome.failures:
                    assert primary is not None
                    _add_bounded_secondary_note(
                        primary, failure, context="unreleased interactive group cleanup also failed"
                    )
            except BaseException as exc:
                assert primary is not None
                _add_bounded_secondary_note(
                    primary, exc, context="unreleased interactive group cleanup also failed"
                )
        if stopped:
            for descriptor in tuple(owned_fds):
                try:
                    close_owned(descriptor)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        _add_bounded_secondary_note(
                            primary, exc, context="interactive gate descriptor cleanup also failed"
                        )
        with self._interactive_lock:
            if stopped and not owned_fds and not uncertain_fds:
                self._interactive_active = False
                self._interactive_resources = None
            else:
                self._interactive_resources = (
                    process,
                    owned_fds,
                    uncertain_fds,
                    stdout_log,
                    stderr_log,
                )
        if primary is not None:
            raise ROIAnalysisError("interactive Linux process did not complete safely") from primary
        assert result is not None
        return result

    @staticmethod
    def _cleanup_timeout(timeout_seconds: float | None) -> float:
        if timeout_seconds is None:
            return _MAXIMUM_CLEANUP_SECONDS
        return max(
            _MINIMUM_CLEANUP_SECONDS,
            min(timeout_seconds, _MAXIMUM_CLEANUP_SECONDS),
        )

    def _drain_group(
        self,
        process: object,
        process_group_id: int,
        *,
        cleanup_timeout: float,
        allow_initial_exit: bool,
    ) -> _GroupDrainOutcome:
        operations = self._operations
        failures: list[BaseException] = []

        def wait_until_empty() -> bool:
            try:
                empty = operations.wait_group_empty(
                    process,
                    process_group_id,
                    timeout_seconds=cleanup_timeout,
                )
            except BaseException as exc:
                failures.append(exc)
                return False
            if type(empty) is not bool:
                failures.append(
                    ROIAnalysisError("local process-group inspection result is invalid")
                )
                return False
            return empty

        def signal_exact_group(signal_number: int) -> None:
            try:
                operations.signal_group(process_group_id, signal_number)
            except BaseException as exc:
                failures.append(exc)

        empty = False
        if allow_initial_exit:
            empty = wait_until_empty()
        if not empty:
            signal_exact_group(signal.SIGTERM)
            empty = wait_until_empty()
        if not empty:
            signal_exact_group(signal.SIGKILL)
            empty = wait_until_empty()
        if not empty:
            failures.append(ROIAnalysisError("local process group could not be proven empty"))
            return _GroupDrainOutcome(
                group_empty_proved=False,
                leader_reaped=False,
                failures=tuple(failures),
            )
        try:
            operations.reap_leader(process, timeout_seconds=cleanup_timeout)
        except BaseException as exc:
            failures.append(exc)
            return _GroupDrainOutcome(
                group_empty_proved=True,
                leader_reaped=False,
                failures=tuple(failures),
            )
        return _GroupDrainOutcome(
            group_empty_proved=True,
            leader_reaped=True,
            failures=tuple(failures),
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
        process_start_observer: ProcessStartObserver,
        process_attempt_observer: ProcessAttemptObserver | None = None,
        process_drain_observer: ProcessDrainObserver | None = None,
        process_terminal_observer: Callable[[int | None, object], None] | None = None,
        cancellation_event: object | None = None,
    ) -> ContainedProcessResult:
        if (
            type(command) is not tuple
            or not command
            or any(type(item) is not str or not item or "\x00" in item for item in command)
            or type(lock_descriptors) is not tuple
            or any(type(item) is not int or item < 0 for item in lock_descriptors)
            or len(set(lock_descriptors)) != len(lock_descriptors)
            or type(launch_request) is not ContainmentLaunchRequest
            or launch_request.provider != "linux-local-process-group/v1"
            or environment is None
            or type(environment) is not dict
            or any(
                type(key) is not str or type(value) is not str for key, value in environment.items()
            )
            or not callable(process_start_observer)
            or (process_attempt_observer is not None and not callable(process_attempt_observer))
            or (process_drain_observer is not None and not callable(process_drain_observer))
            or (process_terminal_observer is not None and not callable(process_terminal_observer))
            or (
                timeout_seconds is not None
                and (type(timeout_seconds) not in (int, float) or timeout_seconds <= 0)
            )
            or (
                cancellation_event is not None
                and not callable(getattr(cancellation_event, "is_set", None))
            )
        ):
            raise ROIAnalysisError("local process-group containment inputs are invalid")

        operations = self._operations
        cleanup_timeout = self._cleanup_timeout(timeout_seconds)
        owned_fds: list[int] = []
        process: object | None = None
        process_id: int | None = None
        process_group_id: int | None = None
        start_epoch_millis: int | None = None
        attachment: LinuxLocalProcessGroupAttachmentV1 | None = None
        drain: LinuxLocalProcessGroupDrainV1 | None = None
        returncode: int | None = None
        log_facts: tuple[str, str, bool, bool] | None = None
        publication_complete = False
        released = False
        release_outcome_unresolved = False
        cancellation_before_release = False
        drain_attempted = False
        drain_proved = False
        primary: BaseException | None = None
        drain_observation_attempted = False
        drain_observation_complete = process_drain_observer is None
        terminal_observation_attempted = False
        terminal_observation_complete = process_terminal_observer is None

        def publish_drain(evidence: LinuxLocalProcessGroupDrainV1) -> None:
            nonlocal drain_observation_attempted, drain_observation_complete
            if drain_observation_attempted:
                raise ROIAnalysisError("local process drain observer repeated")
            drain_observation_attempted = True
            if process_drain_observer is not None:
                process_drain_observer(evidence)
            drain_observation_complete = True

        def publish_terminal(status: int | None, evidence: LinuxLocalProcessGroupDrainV1) -> None:
            nonlocal terminal_observation_attempted, terminal_observation_complete
            if terminal_observation_attempted:
                raise ROIAnalysisError("local process terminal observer repeated")
            terminal_observation_attempted = True
            if process_terminal_observer is not None:
                process_terminal_observer(status, evidence)
            terminal_observation_complete = True

        def close_owned(descriptor: int) -> None:
            owned_fds.remove(descriptor)
            operations.close_fd(descriptor)

        def wait_for_leader() -> int:
            if cancellation_event is None:
                return operations.wait_leader(
                    process,
                    timeout_seconds=timeout_seconds,
                )
            deadline = (
                None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
            )
            while True:
                if cancellation_event.is_set():  # type: ignore[attr-defined]
                    try:
                        return operations.wait_leader(
                            process,
                            timeout_seconds=0.0,
                        )
                    except (subprocess.TimeoutExpired, TimeoutError):
                        raise _CancellationRequested(
                            "local contained process cancellation requested"
                        )
                wait_seconds = _CANCELLATION_POLL_SECONDS
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout_seconds)
                    wait_seconds = min(wait_seconds, remaining)
                try:
                    return operations.wait_leader(
                        process,
                        timeout_seconds=wait_seconds,
                    )
                except (subprocess.TimeoutExpired, TimeoutError):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise

        try:
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("local contained process cancellation requested")
            shell_identity = operations.authenticate_shell(_SHELL_PATH)
            ready_read, ready_write = operations.create_pipe()
            owned_fds.extend((ready_read, ready_write))
            release_read, release_write = operations.create_pipe()
            owned_fds.extend((release_read, release_write))
            gate_argv = (
                _SHELL_PATH,
                "-c",
                GATE_SCRIPT,
                "mif-local-gate",
                str(ready_write),
                str(release_read),
                *command,
            )
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("local contained process cancellation requested")

            def publish_attempt_at_spawn_boundary() -> None:
                nonlocal cancellation_before_release
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()  # type: ignore[attr-defined]
                ):
                    cancellation_before_release = True
                    raise _CancellationRequested("local contained process cancellation requested")
                if process_attempt_observer is not None:
                    process_attempt_observer()

            process = operations.spawn(
                gate_argv,
                pass_fds=(ready_write, release_read, *lock_descriptors),
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                environment=environment,
                process_attempt_observer=publish_attempt_at_spawn_boundary,
            )
            process_id = getattr(process, "pid", None)
            if type(process_id) is not int or process_id <= 0:
                raise ROIAnalysisError("local contained process ID is invalid")
            staging_cleanup_failure = getattr(process, "staging_cleanup_failure", None)
            if staging_cleanup_failure is not None:
                if not isinstance(staging_cleanup_failure, BaseException):
                    raise ROIAnalysisError("local spawn cleanup result is invalid")
                raise staging_cleanup_failure
            close_owned(ready_write)
            close_owned(release_read)
            operations.wait_ready(
                ready_read,
                timeout_seconds=cleanup_timeout,
            )
            close_owned(ready_read)
            start_epoch_millis, process_group_id = operations.observe_identity(
                process,
                shell_identity=shell_identity,
            )
            if (
                type(start_epoch_millis) is not int
                or start_epoch_millis <= 0
                or type(process_group_id) is not int
                or process_group_id != process_id
            ):
                raise ROIAnalysisError("local execution gate identity is invalid")
            attachment = LinuxLocalProcessGroupAttachmentV1(
                provider="linux-local-process-group/v1",
                leader_process_id=process_id,
                leader_start_epoch_millis=start_epoch_millis,
                process_group_id=process_group_id,
            )
            process_launch_sha256 = process_start_observer(
                process_id,
                start_epoch_millis,
                attachment,
            )
            if (
                type(process_launch_sha256) is not str
                or _DIGEST.fullmatch(process_launch_sha256) is None
            ):
                raise ROIAnalysisError("local process launch observer returned an invalid digest")
            publication_complete = True
            if (
                cancellation_event is not None
                and cancellation_event.is_set()  # type: ignore[attr-defined]
            ):
                cancellation_before_release = True
                raise _CancellationRequested("local contained process cancellation requested")
            try:
                operations.write_release(release_write)
            except BaseException:
                release_outcome_unresolved = True
                raise
            released = True
            close_owned(release_write)
            returncode = wait_for_leader()
            reason = "normal_exit" if returncode == 0 else "nonzero_exit"
            drain_attempted = True
            drain_outcome = self._drain_group(
                process,
                process_group_id,
                cleanup_timeout=cleanup_timeout,
                allow_initial_exit=True,
            )
            drain_proved = drain_outcome.complete
            if drain_outcome.failures:
                drain_failure = drain_outcome.failures[0]
                for cleanup_error in drain_outcome.failures[1:]:
                    _add_bounded_secondary_note(
                        drain_failure,
                        cleanup_error,
                        context="additional local process-group drain failure",
                    )
                raise drain_failure
            if not drain_proved:
                raise ROIAnalysisError("local process-group drain did not complete")
            drain = LinuxLocalProcessGroupDrainV1(
                provider="linux-local-process-group/v1",
                semantic_state="drained",
                leader_process_id=process_id,
                leader_start_epoch_millis=start_epoch_millis,
                process_group_id=process_group_id,
                termination_reason=reason,
            )
            publish_drain(drain)
            publish_terminal(returncode, drain)
            try:
                log_facts = operations.finalize_logs(stdout_log, stderr_log)
            except BaseException as log_error:
                if returncode != 0:
                    nonzero = subprocess.CalledProcessError(returncode, command)
                    _add_bounded_secondary_note(
                        nonzero,
                        log_error,
                        context="post-drain local log finalization also failed",
                    )
                    raise nonzero from log_error
                raise
            return ContainedProcessResult(
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
            if attachment is not None:
                if returncode is not None:
                    reason = "normal_exit" if returncode == 0 else "nonzero_exit"
                elif not publication_complete or cancellation_before_release:
                    reason = "launch_publication_failed"
                elif release_outcome_unresolved:
                    reason = None
                elif isinstance(exc, _CancellationRequested):
                    reason = "cancelled"
                elif isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
                    reason = "timeout"
                else:
                    reason = "cancelled"
            else:
                reason = None

            for descriptor in tuple(owned_fds):
                try:
                    close_owned(descriptor)
                except BaseException as cleanup_error:
                    _add_bounded_secondary_note(
                        primary,
                        cleanup_error,
                        context="local gate descriptor cleanup also failed",
                    )

            if process is not None and not drain_attempted:
                cleanup_group = process_id if process_group_id is None else process_group_id
                if type(cleanup_group) is int and cleanup_group > 0:
                    drain_attempted = True
                    try:
                        drain_outcome = self._drain_group(
                            process,
                            cleanup_group,
                            cleanup_timeout=cleanup_timeout,
                            allow_initial_exit=not released,
                        )
                        drain_proved = drain_outcome.complete
                        for cleanup_error in drain_outcome.failures:
                            _add_bounded_secondary_note(
                                primary,
                                cleanup_error,
                                context="local process-group drain also failed",
                            )
                    except BaseException as cleanup_error:
                        _add_bounded_secondary_note(
                            primary,
                            cleanup_error,
                            context="local process-group drain also failed",
                        )
            if drain_proved and attachment is not None and reason is not None and drain is None:
                drain = LinuxLocalProcessGroupDrainV1(
                    provider="linux-local-process-group/v1",
                    semantic_state="drained",
                    leader_process_id=attachment.leader_process_id,
                    leader_start_epoch_millis=attachment.leader_start_epoch_millis,
                    process_group_id=attachment.process_group_id,
                    termination_reason=reason,  # type: ignore[arg-type]
                )
            if drain is not None and not drain_observation_attempted:
                try:
                    publish_drain(drain)
                except BaseException as drain_observer_error:
                    _add_bounded_secondary_note(
                        primary,
                        drain_observer_error,
                        context="local process drain publication also failed",
                    )
            if (
                drain is not None
                and drain_observation_complete
                and not terminal_observation_attempted
            ):
                try:
                    publish_terminal(returncode, drain)
                except BaseException as terminal_observer_error:
                    _add_bounded_secondary_note(
                        primary,
                        terminal_observer_error,
                        context="local process terminal publication also failed",
                    )
            if (
                drain_proved
                and drain_observation_complete
                and terminal_observation_complete
                and log_facts is None
            ):
                try:
                    log_facts = operations.finalize_logs(stdout_log, stderr_log)
                except BaseException as log_error:
                    _add_bounded_secondary_note(
                        primary,
                        log_error,
                        context="post-drain local log finalization also failed",
                    )
        finally:
            for descriptor in tuple(owned_fds):
                try:
                    close_owned(descriptor)
                except BaseException as cleanup_error:
                    if primary is None:
                        primary = cleanup_error
                    else:
                        _add_bounded_secondary_note(
                            primary,
                            cleanup_error,
                            context="additional local descriptor cleanup failure",
                        )

        assert primary is not None
        raise _ContainedProcessError(
            "Linux local contained process did not complete normally",
            _ContainedProcessFailure(
                process_id=(process_id if start_epoch_millis is not None else None),
                start_epoch_millis=start_epoch_millis,
                launch_evidence=attachment,
                drain_evidence=drain,
                returncode=returncode,
                stdout_excerpt=None if log_facts is None else log_facts[0],
                stderr_excerpt=None if log_facts is None else log_facts[1],
                stdout_truncated=None if log_facts is None else log_facts[2],
                stderr_truncated=None if log_facts is None else log_facts[3],
            ),
        ) from primary


__all__ = [
    "GATE_SCRIPT",
    "LinuxLocalProcessGroupExecutor",
    "LinuxLocalProcessGroupOperations",
]
