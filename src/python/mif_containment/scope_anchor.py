"""Fixed two-phase same-user process that keeps a systemd scope alive.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import signal
import stat
import sys
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Protocol

PROMOTION_FD = 3
PROMOTION_TOKEN = b"mif-scope-anchor-promote/v1\n"
MAXIMUM_BOOTSTRAP_FRAME_BYTES = 256
MAXIMUM_PROMOTION_FRAME_BYTES = len(PROMOTION_TOKEN)
ANCHOR_FAILURE = 1

PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38

_MAXIMUM_PID = (1 << 31) - 1
_MAXIMUM_INITIAL_ENVIRONMENT_BYTES = 65_536
_MAXIMUM_PROC_STAT_BYTES = 65_536
_MAXIMUM_PROC_STATUS_BYTES = 65_536
_START_TOKEN = re.compile(r"linux-proc-stat/v1:(?P<pid>[1-9][0-9]*):(?P<start>[1-9][0-9]*)\Z")
_CAPABILITY_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
_CAPABILITY_VALUE = re.compile(r"[0-9a-f]{16}\Z")
_EXPECTED_DESCRIPTORS = frozenset((0, 1, 2, PROMOTION_FD))


# ==============================================================================
#                                  Scope anchor
# ==============================================================================


class ScopeAnchorError(RuntimeError):
    """The anchor's fixed execution or promotion contract was not satisfied."""


class _SignalPhase(Enum):
    PRE_FINAL_STOP = "pre_final_stop"
    FINAL_STOP = "final_stop"


class ScopeAnchorPrimitives(Protocol):
    """Small injectable boundary around every operating-system operation."""

    def get_argv(self) -> tuple[str, ...]: ...

    def get_environment(self) -> Mapping[str, str]: ...

    def get_cwd(self) -> str: ...

    def getresuid(self) -> tuple[int, int, int]: ...

    def getresgid(self) -> tuple[int, int, int]: ...

    def getgroups(self) -> tuple[int, ...]: ...

    def capability_sets_empty(self) -> bool: ...

    def list_open_fds(self) -> frozenset[int]: ...

    def fd_access_mode(self, descriptor: int) -> int: ...

    def fd_is_fifo(self, descriptor: int) -> bool: ...

    def prctl(self, option: int, value: int) -> None: ...

    def read_bootstrap_frame(self, descriptor: int, maximum: int) -> bytes: ...

    def parent_identity(self) -> tuple[int, str]: ...

    def install_signal_handlers(self, handler: Callable[[int], None]) -> None: ...

    def stop_self(self) -> None: ...

    def read_promotion_to_eof(self, descriptor: int, maximum: int) -> bytes: ...

    def close_fd(self, descriptor: int) -> None: ...

    def block_signal(self, signum: int) -> frozenset[int]: ...

    def is_signal_pending(self, signum: int) -> bool: ...

    def wait_for_signal(self, signum: int) -> int: ...


def encode_bootstrap_frame(broker_pid: int, broker_start_token: str) -> bytes:
    """Build the anchor's only broker-authored identity frame."""

    _validate_broker_identity(broker_pid, broker_start_token)
    encoded = (
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
    if len(encoded) > MAXIMUM_BOOTSTRAP_FRAME_BYTES:
        raise ValueError("anchor bootstrap frame exceeds its fixed bound")
    return encoded


def scope_anchor_main(primitives: ScopeAnchorPrimitives | None = None) -> int:
    """Validate, promote, and idle the fixed scope anchor fail-closed."""

    operations = primitives if primitives is not None else _PosixScopeAnchorPrimitives()
    try:
        _run_anchor(operations)
    except (ScopeAnchorError, OSError, ValueError):
        return ANCHOR_FAILURE
    return 0


def _run_anchor(operations: ScopeAnchorPrimitives) -> None:
    _validate_execution_context(operations)
    operations.prctl(PR_SET_NO_NEW_PRIVS, 1)

    frame = operations.read_bootstrap_frame(
        PROMOTION_FD,
        MAXIMUM_BOOTSTRAP_FRAME_BYTES,
    )
    broker_pid, broker_start_token = _decode_bootstrap_frame(frame)

    operations.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    _require_parent(operations, broker_pid, broker_start_token)

    signal_phase = _SignalPhase.PRE_FINAL_STOP

    def handle_signal(signum: int) -> None:
        if signum in (signal.SIGTERM, signal.SIGINT):
            return
        if signum == signal.SIGUSR1:
            if signal_phase is _SignalPhase.PRE_FINAL_STOP:
                raise ScopeAnchorError("anchor finalization preceded its final stop")
            raise ScopeAnchorError("blocked finalization signal reached its handler")
        raise ScopeAnchorError("unexpected anchor signal")

    operations.install_signal_handlers(handle_signal)
    operations.stop_self()

    promotion = operations.read_promotion_to_eof(
        PROMOTION_FD,
        MAXIMUM_PROMOTION_FRAME_BYTES,
    )
    if promotion != PROMOTION_TOKEN:
        raise ScopeAnchorError("anchor promotion frame is malformed")

    operations.prctl(PR_SET_PDEATHSIG, 0)
    _require_parent(operations, broker_pid, broker_start_token)
    operations.close_fd(PROMOTION_FD)
    previous_mask = operations.block_signal(signal.SIGUSR1)
    if signal.SIGUSR1 in previous_mask:
        raise ScopeAnchorError("anchor inherited a blocked finalization signal")
    if operations.is_signal_pending(signal.SIGUSR1):
        raise ScopeAnchorError("anchor finalization preceded its final stop")
    signal_phase = _SignalPhase.FINAL_STOP
    operations.stop_self()
    if operations.wait_for_signal(signal.SIGUSR1) != signal.SIGUSR1:
        raise ScopeAnchorError("anchor finalization wait returned another signal")


def _validate_execution_context(operations: ScopeAnchorPrimitives) -> None:
    argv = operations.get_argv()
    if type(argv) is not tuple or len(argv) != 1 or type(argv[0]) is not str:
        raise ScopeAnchorError("scope anchor accepts no arguments")

    environment = operations.get_environment()
    if not isinstance(environment, Mapping) or environment:
        raise ScopeAnchorError("scope anchor requires an empty environment")
    if operations.get_cwd() != "/":
        raise ScopeAnchorError("scope anchor requires / as its working directory")

    _validate_credentials("UID", operations.getresuid())
    _validate_credentials("GID", operations.getresgid())
    groups = operations.getgroups()
    if type(groups) is not tuple or groups:
        raise ScopeAnchorError("scope anchor requires cleared supplementary groups")
    if operations.capability_sets_empty() is not True:
        raise ScopeAnchorError("scope anchor requires empty capability sets")

    if operations.list_open_fds() != _EXPECTED_DESCRIPTORS:
        raise ScopeAnchorError("scope anchor descriptor inventory is not exact")
    if operations.fd_access_mode(PROMOTION_FD) != os.O_RDONLY:
        raise ScopeAnchorError("scope anchor promotion descriptor is not read-only")
    if operations.fd_is_fifo(PROMOTION_FD) is not True:
        raise ScopeAnchorError("scope anchor promotion descriptor is not a pipe")


def _validate_credentials(label: str, credentials: tuple[int, int, int]) -> None:
    if (
        type(credentials) is not tuple
        or len(credentials) != 3
        or any(type(value) is not int for value in credentials)
        or credentials[0] == 0
        or credentials[0] != credentials[1]
        or credentials[0] != credentials[2]
    ):
        raise ScopeAnchorError(
            f"scope anchor requires equal non-root real/effective/saved {label}s"
        )


def _decode_bootstrap_frame(frame: bytes) -> tuple[int, str]:
    if (
        type(frame) is not bytes
        or not frame.endswith(b"\n")
        or len(frame) > MAXIMUM_BOOTSTRAP_FRAME_BYTES
    ):
        raise ScopeAnchorError("anchor bootstrap frame is missing or overlong")
    try:
        payload = json.loads(
            frame[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_object_from_unique_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ScopeAnchorError) as exc:
        raise ScopeAnchorError("anchor bootstrap frame is malformed") from exc
    if type(payload) is not dict or set(payload) != {
        "broker_pid",
        "broker_start_token",
    }:
        raise ScopeAnchorError("anchor bootstrap fields are not exact")
    broker_pid = payload["broker_pid"]
    broker_start_token = payload["broker_start_token"]
    try:
        _validate_broker_identity(broker_pid, broker_start_token)
        canonical = encode_bootstrap_frame(broker_pid, broker_start_token)
    except ValueError as exc:
        raise ScopeAnchorError("anchor bootstrap identity is malformed") from exc
    if frame != canonical:
        raise ScopeAnchorError("anchor bootstrap frame is not canonical")
    return broker_pid, broker_start_token


def _object_from_unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScopeAnchorError("anchor bootstrap contains a duplicate key")
        result[key] = value
    return result


def _validate_broker_identity(broker_pid: int, broker_start_token: str) -> None:
    if type(broker_pid) is not int or not 1 <= broker_pid <= _MAXIMUM_PID:
        raise ValueError("broker PID is outside the fixed range")
    if type(broker_start_token) is not str:
        raise ValueError("broker start token must be text")
    match = _START_TOKEN.fullmatch(broker_start_token)
    if match is None or int(match.group("pid"), 10) != broker_pid:
        raise ValueError("broker start token is malformed or names another PID")


def _require_parent(
    operations: ScopeAnchorPrimitives,
    broker_pid: int,
    broker_start_token: str,
) -> None:
    if operations.parent_identity() != (broker_pid, broker_start_token):
        raise ScopeAnchorError("scope anchor broker parent identity changed")


class _PosixScopeAnchorPrimitives:
    def __init__(self) -> None:
        self._libc: ctypes.CDLL | None = None

    def get_argv(self) -> tuple[str, ...]:
        return tuple(sys.argv)

    def get_environment(self) -> Mapping[str, str]:
        content = _read_bounded_proc_file(
            "/proc/self/environ",
            _MAXIMUM_INITIAL_ENVIRONMENT_BYTES,
            "initial environment",
        )
        if not content:
            return {}
        return {"<inherited-environment-present>": ""}

    def get_cwd(self) -> str:
        return os.getcwd()

    def getresuid(self) -> tuple[int, int, int]:
        return os.getresuid()

    def getresgid(self) -> tuple[int, int, int]:
        return os.getresgid()

    def getgroups(self) -> tuple[int, ...]:
        return tuple(os.getgroups())

    def capability_sets_empty(self) -> bool:
        content = _read_bounded_proc_file(
            "/proc/self/status",
            _MAXIMUM_PROC_STATUS_BYTES,
            "process status",
        )
        try:
            text = content.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise ScopeAnchorError("process status is not ASCII") from exc

        values: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, raw_value = line.partition(":")
            if not separator or key not in _CAPABILITY_FIELDS:
                continue
            if key in values:
                raise ScopeAnchorError("process status repeats a capability set")
            value = raw_value.strip()
            if _CAPABILITY_VALUE.fullmatch(value) is None:
                raise ScopeAnchorError("process status capability value is malformed")
            values[key] = value

        if set(values) != set(_CAPABILITY_FIELDS):
            raise ScopeAnchorError("process status capability sets are incomplete")
        return all(value == "0000000000000000" for value in values.values())

    def list_open_fds(self) -> frozenset[int]:
        descriptors: set[int] = set()
        for name in os.listdir("/proc/self/fd"):
            if not name.isascii() or not name.isdigit():
                raise ScopeAnchorError("non-numeric entry in /proc/self/fd")
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

    def prctl(self, option: int, value: int) -> None:
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
        result = self._libc.prctl(option, value, 0, 0, 0)
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    def read_bootstrap_frame(self, descriptor: int, maximum: int) -> bytes:
        content = bytearray()
        while len(content) < maximum:
            chunk = os.read(descriptor, 1)
            if not chunk:
                break
            content.extend(chunk)
            if chunk == b"\n":
                break
        return bytes(content)

    def parent_identity(self) -> tuple[int, str]:
        parent_before = os.getppid()
        token = _read_proc_start_token(parent_before)
        parent_after = os.getppid()
        if parent_before != parent_after:
            raise ScopeAnchorError("scope anchor was reparented during identity read")
        return parent_before, token

    def install_signal_handlers(self, handler: Callable[[int], None]) -> None:
        def dispatch(signum: int, _frame: object) -> None:
            handler(signum)

        signal.signal(signal.SIGTERM, dispatch)
        signal.signal(signal.SIGINT, dispatch)
        signal.signal(signal.SIGUSR1, dispatch)

    def stop_self(self) -> None:
        os.kill(os.getpid(), signal.SIGSTOP)

    def read_promotion_to_eof(self, descriptor: int, maximum: int) -> bytes:
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, maximum - len(content) + 1)
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        return bytes(content)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    def block_signal(self, signum: int) -> frozenset[int]:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signum})
        return frozenset(int(item) for item in previous)

    def is_signal_pending(self, signum: int) -> bool:
        return signum in signal.sigpending()

    def wait_for_signal(self, signum: int) -> int:
        return int(signal.sigwait({signum}))


def _read_proc_start_token(pid: int) -> str:
    path = f"/proc/{pid}/stat"
    content = _read_bounded_proc_file(
        path,
        _MAXIMUM_PROC_STAT_BYTES,
        "parent /proc stat",
    )
    try:
        text = content.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScopeAnchorError("parent /proc stat is not ASCII") from exc
    closing = text.rfind(") ")
    if closing < 1 or text[:closing].split("(", 1)[0].strip() != str(pid):
        raise ScopeAnchorError("parent /proc stat is malformed")
    fields = text[closing + 2 :].split()
    if len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdigit():
        raise ScopeAnchorError("parent /proc start token is missing")
    start = int(fields[19], 10)
    if start < 1:
        raise ScopeAnchorError("parent /proc start token is invalid")
    return f"linux-proc-stat/v1:{pid}:{start}"


def _read_bounded_proc_file(path: str, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(
                descriptor,
                maximum - len(content) + 1,
            )
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(descriptor)
    if len(content) > maximum:
        raise ScopeAnchorError(f"{label} exceeds its fixed bound")
    return bytes(content)


if __name__ == "__main__":
    raise SystemExit(scope_anchor_main())
