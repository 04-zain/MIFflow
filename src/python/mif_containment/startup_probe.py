#!/usr/bin/python3 -I
"""Fixed isolated inert process used by broker startup certification.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import ctypes
import contextvars
import errno
import fcntl
import json
import os
import re
import signal
import stat
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Iterator, Protocol

BOOTSTRAP_FD = 3
READINESS_FD = 4
READINESS_FRAME = b"mif-containment-startup-probe-ready/v1\n"
MAXIMUM_BOOTSTRAP_FRAME_BYTES = 256
PROBE_FAILURE = 1

PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
PR_GET_NO_NEW_PRIVS = 39

_MAXIMUM_INITIAL_ENVIRONMENT_BYTES = 65_536
_MAXIMUM_PROC_STATUS_BYTES = 65_536
_MAXIMUM_PROC_STAT_BYTES = 65_536
_EXPECTED_DESCRIPTORS = frozenset((0, 1, 2, BOOTSTRAP_FD, READINESS_FD))
_START_TOKEN = re.compile(r"linux-proc-stat/v1:(?P<pid>[1-9][0-9]*):(?P<start>[1-9][0-9]*)\Z")


# ==============================================================================
#                                 Startup probe
# ==============================================================================


class StartupProbeError(RuntimeError):
    """The fixed probe execution or readiness contract was not satisfied."""


class StartupProbePrimitives(Protocol):
    """Narrow injectable boundary around probe-local operating-system reads."""

    def get_argv(self) -> tuple[str, ...]: ...

    def get_environment(self) -> Mapping[str, str]: ...

    def get_cwd(self) -> str: ...

    def getresuid(self) -> tuple[int, int, int]: ...

    def getresgid(self) -> tuple[int, int, int]: ...

    def getgroups(self) -> tuple[int, ...]: ...

    def capability_sets_empty(self) -> bool: ...

    def get_no_new_privileges(self) -> bool: ...

    def set_dumpable_off(self) -> None: ...

    def get_dumpable(self) -> bool: ...

    def get_parent_death_signal(self) -> int: ...

    def list_open_fds(self) -> frozenset[int]: ...

    def fd_access_mode(self, descriptor: int) -> int: ...

    def fd_is_fifo(self, descriptor: int) -> bool: ...

    def read_bootstrap_to_eof(self, descriptor: int, maximum: int) -> bytes: ...

    def parent_identity(self) -> tuple[int, str]: ...

    def close_fd(self, descriptor: int) -> None: ...

    def write_readiness(self, descriptor: int, frame: bytes) -> int: ...

    def stop_self(self) -> None: ...


def encode_probe_bootstrap_frame(
    broker_pid: int,
    broker_start_token: str,
) -> bytes:
    """Build the probe's one bounded canonical parent-identity frame."""

    _validate_broker_identity(broker_pid, broker_start_token)
    frame = (
        json.dumps(
            {
                "broker_pid": broker_pid,
                "broker_start_token": broker_start_token,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(frame) > MAXIMUM_BOOTSTRAP_FRAME_BYTES:
        raise ValueError("startup-probe bootstrap frame exceeds its fixed bound")
    return frame


def startup_probe_main() -> int:
    """Validate the fixed context, emit readiness, and remain stopped."""

    operations = _STARTUP_PROBE_PRIMITIVES.get()
    try:
        _run_probe(operations)
    except (StartupProbeError, OSError, ValueError):
        return PROBE_FAILURE
    # A correct probe cannot return from its stop.  Returning is itself failure.
    return PROBE_FAILURE


def _run_probe(operations: StartupProbePrimitives) -> None:
    _validate_execution_context(operations)
    frame = operations.read_bootstrap_to_eof(
        BOOTSTRAP_FD,
        MAXIMUM_BOOTSTRAP_FRAME_BYTES,
    )
    broker_pid, broker_start_token = _decode_bootstrap_frame(frame)
    if operations.parent_identity() != (broker_pid, broker_start_token):
        raise StartupProbeError("startup-probe broker parent identity changed")
    operations.close_fd(BOOTSTRAP_FD)
    try:
        written = operations.write_readiness(READINESS_FD, READINESS_FRAME)
    except BaseException:
        operations.close_fd(READINESS_FD)
        raise
    if type(written) is not int or written != len(READINESS_FRAME):
        operations.close_fd(READINESS_FD)
        raise StartupProbeError("startup-probe readiness write was incomplete")
    operations.close_fd(READINESS_FD)
    operations.stop_self()
    raise StartupProbeError("startup probe resumed unexpectedly")


def _validate_execution_context(operations: StartupProbePrimitives) -> None:
    argv = operations.get_argv()
    if type(argv) is not tuple or len(argv) != 1 or type(argv[0]) is not str:
        raise StartupProbeError("startup probe accepts no arguments")
    environment = operations.get_environment()
    if not isinstance(environment, Mapping) or environment:
        raise StartupProbeError("startup probe requires an empty environment")
    if operations.get_cwd() != "/":
        raise StartupProbeError("startup probe requires / as its working directory")
    _validate_dropped_credentials("UID", operations.getresuid())
    _validate_dropped_credentials("GID", operations.getresgid())
    if operations.getgroups() != ():
        raise StartupProbeError("startup probe requires cleared supplementary groups")
    if operations.capability_sets_empty() is not True:
        raise StartupProbeError("startup probe requires empty capability sets")
    if operations.get_no_new_privileges() is not True:
        raise StartupProbeError("startup probe requires no-new-privileges")
    # A non-privileged exec resets dumpability on Linux, so the isolated probe
    # must restore the trampoline's locked state before exposing readiness.
    operations.set_dumpable_off()
    if operations.get_dumpable() is not False:
        raise StartupProbeError("startup probe must not be dumpable")
    if operations.get_parent_death_signal() != signal.SIGKILL:
        raise StartupProbeError("startup probe requires the broker-death backstop")
    if operations.list_open_fds() != _EXPECTED_DESCRIPTORS:
        raise StartupProbeError("startup probe descriptor inventory is not exact")
    expected_descriptors = (
        (BOOTSTRAP_FD, os.O_RDONLY),
        (READINESS_FD, os.O_WRONLY),
    )
    for descriptor, access_mode in expected_descriptors:
        if operations.fd_access_mode(descriptor) != access_mode:
            raise StartupProbeError("startup probe pipe direction is not exact")
        if operations.fd_is_fifo(descriptor) is not True:
            raise StartupProbeError("startup probe inherited descriptor is not a pipe")


def _validate_dropped_credentials(
    label: str,
    credentials: tuple[int, int, int],
) -> None:
    if (
        type(credentials) is not tuple
        or len(credentials) != 3
        or any(type(value) is not int for value in credentials)
        or credentials[0] == 0
        or credentials[0] != credentials[1]
        or credentials[0] != credentials[2]
    ):
        raise StartupProbeError(
            f"startup probe requires equal non-root real/effective/saved {label}s"
        )


def _decode_bootstrap_frame(frame: bytes) -> tuple[int, str]:
    if (
        type(frame) is not bytes
        or not frame.endswith(b"\n")
        or len(frame) > MAXIMUM_BOOTSTRAP_FRAME_BYTES
    ):
        raise StartupProbeError("startup-probe bootstrap is missing or overlong")
    try:
        payload = json.loads(
            frame[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_object_from_unique_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, StartupProbeError) as exc:
        raise StartupProbeError("startup-probe bootstrap is malformed") from exc
    if type(payload) is not dict or set(payload) != {
        "broker_pid",
        "broker_start_token",
    }:
        raise StartupProbeError("startup-probe bootstrap fields are not exact")
    broker_pid = payload["broker_pid"]
    broker_start_token = payload["broker_start_token"]
    try:
        canonical = encode_probe_bootstrap_frame(broker_pid, broker_start_token)
    except ValueError as exc:
        raise StartupProbeError("startup-probe bootstrap identity is malformed") from exc
    if canonical != frame:
        raise StartupProbeError("startup-probe bootstrap is not canonical")
    return broker_pid, broker_start_token


def _object_from_unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StartupProbeError("startup-probe bootstrap contains a duplicate key")
        result[key] = value
    return result


def _validate_broker_identity(broker_pid: int, broker_start_token: str) -> None:
    if type(broker_pid) is not int or not 1 <= broker_pid <= (1 << 31) - 1:
        raise ValueError("broker PID is outside the fixed range")
    if type(broker_start_token) is not str:
        raise ValueError("broker start token must be text")
    match = _START_TOKEN.fullmatch(broker_start_token)
    if match is None or int(match.group("pid"), 10) != broker_pid:
        raise ValueError("broker start token is malformed or names another PID")


class _PosixProbePrimitives:
    def __init__(self) -> None:
        self._libc: ctypes.CDLL | None = None

    def get_argv(self) -> tuple[str, ...]:
        return tuple(sys.argv)

    def get_environment(self) -> Mapping[str, str]:
        content = _read_bounded_path(
            "/proc/self/environ",
            _MAXIMUM_INITIAL_ENVIRONMENT_BYTES,
            "initial environment",
        )
        return {} if not content else {"<inherited-environment-present>": ""}

    def get_cwd(self) -> str:
        return os.getcwd()

    def getresuid(self) -> tuple[int, int, int]:
        return os.getresuid()

    def getresgid(self) -> tuple[int, int, int]:
        return os.getresgid()

    def getgroups(self) -> tuple[int, ...]:
        return tuple(os.getgroups())

    def capability_sets_empty(self) -> bool:
        fields = _status_fields()
        names = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        return all(fields.get(name) == "0000000000000000" for name in names)

    def get_no_new_privileges(self) -> bool:
        return self._prctl(PR_GET_NO_NEW_PRIVS) == 1

    def set_dumpable_off(self) -> None:
        self._prctl(PR_SET_DUMPABLE, 0)

    def get_dumpable(self) -> bool:
        return self._prctl(PR_GET_DUMPABLE) != 0

    def get_parent_death_signal(self) -> int:
        value = ctypes.c_int(0)
        self._prctl(PR_GET_PDEATHSIG, ctypes.addressof(value))
        return int(value.value)

    def list_open_fds(self) -> frozenset[int]:
        descriptors: set[int] = set()
        for name in os.listdir("/proc/self/fd"):
            if not name.isascii() or not name.isdigit():
                raise StartupProbeError("non-numeric entry in /proc/self/fd")
            descriptor = int(name, 10)
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    continue
                raise
            descriptors.add(descriptor)
        return frozenset(descriptors)

    def fd_access_mode(self, descriptor: int) -> int:
        return fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE

    def fd_is_fifo(self, descriptor: int) -> bool:
        return stat.S_ISFIFO(os.fstat(descriptor).st_mode)

    def read_bootstrap_to_eof(self, descriptor: int, maximum: int) -> bytes:
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, maximum - len(content) + 1)
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        return bytes(content)

    def parent_identity(self) -> tuple[int, str]:
        parent_before = os.getppid()
        token = _read_proc_start_token(parent_before)
        parent_after = os.getppid()
        if parent_before != parent_after:
            raise StartupProbeError("startup probe was reparented during identity read")
        return parent_before, token

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    def write_readiness(self, descriptor: int, frame: bytes) -> int:
        return os.write(descriptor, frame)

    def stop_self(self) -> None:
        os.kill(os.getpid(), signal.SIGSTOP)

    def _prctl(self, option: int, argument: int = 0) -> int:
        if self._libc is None:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl.argtypes = (
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            )
            libc.prctl.restype = ctypes.c_int
            self._libc = libc
        result = self._libc.prctl(option, argument, 0, 0, 0)
        if result < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        return int(result)


_PRODUCTION_PROBE_PRIMITIVES = _PosixProbePrimitives()
_STARTUP_PROBE_PRIMITIVES: contextvars.ContextVar[StartupProbePrimitives] = contextvars.ContextVar(
    "mif_containment_startup_probe_primitives",
    default=_PRODUCTION_PROBE_PRIMITIVES,
)


@contextmanager
def _override_startup_probe_primitives(
    primitives: StartupProbePrimitives,
) -> Iterator[None]:
    token = _STARTUP_PROBE_PRIMITIVES.set(primitives)
    try:
        yield
    finally:
        _STARTUP_PROBE_PRIMITIVES.reset(token)


def _status_fields() -> dict[str, str]:
    content = _read_bounded_path(
        "/proc/self/status",
        _MAXIMUM_PROC_STATUS_BYTES,
        "process status",
    )
    try:
        text = content.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise StartupProbeError("process status is not ASCII") from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        fields[name] = value.strip()
    return fields


def _read_proc_start_token(pid: int) -> str:
    content = _read_bounded_path(
        f"/proc/{pid}/stat",
        _MAXIMUM_PROC_STAT_BYTES,
        "parent stat",
    )
    try:
        text = content.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise StartupProbeError("parent stat is not ASCII") from exc
    closing = text.rfind(") ")
    if closing < 1 or text[:closing].split("(", 1)[0].strip() != str(pid):
        raise StartupProbeError("parent stat is malformed")
    fields = text[closing + 2 :].split()
    if len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdigit():
        raise StartupProbeError("parent start identity is missing")
    start = int(fields[19], 10)
    if start < 1:
        raise StartupProbeError("parent start identity is invalid")
    return f"linux-proc-stat/v1:{pid}:{start}"


def _read_bounded_path(path: str, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, maximum - len(content) + 1)
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(descriptor)
    if len(content) > maximum:
        raise StartupProbeError(f"{label} exceeds its fixed bound")
    return bytes(content)


if __name__ == "__main__":
    raise SystemExit(startup_probe_main())
