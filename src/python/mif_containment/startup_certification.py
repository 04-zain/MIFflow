"""Process-local active proof that fixed systemd containment is usable.

The `BrokerStartupCertification` record carries `broker_process_id`,
`broker_process_start_token`, `broker_build_id`, `broker_config_sha256`,
`scope_policy_sha256` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import errno
import hashlib
import math
import os
import posixpath
import pwd
import re
import secrets
import signal
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from typing import Iterator, Protocol

from .codec import dumps_canonical, sha256_hex
from .protocol import (
    CONTROL_SOCKET_FD,
    CERTIFIED_CLAIMS,
    PROVIDER,
    PROTOCOL_VERSION,
    RETAINED_TARGET_FD,
    BrokerCapabilitiesV1,
    BrokerOperation,
    BrokerSecurityProfileV1,
    CgroupDeletedAfterStopProofV1,
    CgroupDirectoryIdentityV1,
    CgroupEventsZeroProofV1,
    StartupCertificationSummaryV1,
    SystemdStopJobEvidenceV1,
)
from .systemd_adapter import (
    SCOPE_SLICE,
    CgroupVerificationError,
    ScopeStartRequest,
    ScopeStartResult,
    SystemdCapabilityState,
    SystemdEpochSnapshot,
    SystemdEpochWatchHandle,
    SystemdManager,
    SystemdPrerequisiteReport,
    SystemdStopJobIdentity,
    UnitIdentity,
    VerifiedScope,
    prove_cgroup_deleted_after_stop,
    prove_cgroup_events_zero,
    read_cgroup_events_populated,
    verify_scope_attachment,
)
from .startup_probe import (
    BOOTSTRAP_FD,
    READINESS_FD,
    READINESS_FRAME,
    encode_probe_bootstrap_frame,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_BUS_ID = re.compile(r"[0-9a-f]{32}\Z")
_OWNER = re.compile(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z")
_START_TOKEN = re.compile(r"linux-proc-stat/v1:(?P<pid>[1-9][0-9]*):(?P<start>[1-9][0-9]*)\Z")
_CERTIFICATION_UNIT = re.compile(r"mif-qupath-cert-[0-9a-f]{32}\.scope\Z")
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_JOB_PATH = re.compile(r"/org/freedesktop/systemd1/job/[1-9][0-9]*\Z")
_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,30}\Z")
_MAXIMUM_PROC_BYTES = 65_536
_MAXIMUM_EXECUTABLE_BYTES = 268_435_456
_CERTIFICATION_DESCRIPTION = "mIF containment startup certification"
_EXPECTED_PROBE_FDS = frozenset((0, 1, 2, BOOTSTRAP_FD, READINESS_FD))
_PROBE_SHEBANG = b"#!/usr/bin/python3 -I\n"
_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_MAXIMUM_CAPABILITY_NUMBER = 63
_SCOPE_POLICY_SCHEMA = "mif-containment-fixed-scope-properties/v1"
_SCOPE_REQUEST_TEMPLATE_SCHEMA = "mif-containment-scope-request-template/v1"
_CERTIFICATION_UNIT_GRAMMAR = "mif-qupath-cert-[0-9a-f]{32}.scope"
_CERTIFICATION_PID_ROLES = ("startup-probe-1", "startup-probe-2")


# ==============================================================================
#                             Startup certification
# ==============================================================================


def _scope_policy_mapping(
    *,
    timeout_stop_usec: int,
    runtime_max_usec: int,
    slice_name: str,
    kill_mode: str,
    send_sigkill: bool,
    collect_mode: str,
    delegate: bool,
    controller: str,
) -> dict[str, object]:
    return {
        "schema": _SCOPE_POLICY_SCHEMA,
        "Slice": slice_name,
        "KillMode": kill_mode,
        "TimeoutStopUSec": timeout_stop_usec,
        "SendSIGKILL": send_sigkill,
        "CollectMode": collect_mode,
        "RuntimeMaxUSec": runtime_max_usec,
        "Delegate": delegate,
        "Controller": controller,
    }


def _scope_policy_mapping_for_request(request: ScopeStartRequest) -> dict[str, object]:
    if not isinstance(request, ScopeStartRequest):
        raise TypeError("request must be a ScopeStartRequest")
    return _scope_policy_mapping(
        timeout_stop_usec=request.timeout_stop_usec,
        runtime_max_usec=request.runtime_max_usec,
        slice_name=SCOPE_SLICE,
        kill_mode="control-group",
        send_sigkill=True,
        collect_mode="inactive-or-failed",
        delegate=False,
        controller="",
    )


def scope_policy_sha256_for_request(request: ScopeStartRequest) -> str:
    """Hash the closed fixed-scope policy encoded by a start request."""

    return sha256_hex(dumps_canonical(_scope_policy_mapping_for_request(request)))


def scope_policy_sha256_for_identity(identity: UnitIdentity) -> str:
    """Hash the closed fixed-scope policy actually observed from systemd."""

    if not isinstance(identity, UnitIdentity):
        raise TypeError("identity must be a UnitIdentity")
    return sha256_hex(
        dumps_canonical(
            _scope_policy_mapping(
                timeout_stop_usec=identity.timeout_stop_usec,
                runtime_max_usec=identity.runtime_max_usec,
                slice_name=identity.slice,
                kill_mode=identity.kill_mode,
                send_sigkill=identity.send_sigkill,
                collect_mode=identity.collect_mode,
                delegate=identity.delegate,
                controller=identity.controller,
            )
        )
    )


def scope_request_template_sha256(request: ScopeStartRequest) -> str:
    """Hash the normalized, closed certification StartTransientUnit template."""

    if not isinstance(request, ScopeStartRequest):
        raise TypeError("request must be a ScopeStartRequest")
    if _CERTIFICATION_UNIT.fullmatch(request.unit_name) is None:
        raise StartupCertificationError("certification unit name is outside its grammar")
    mapping = {
        "schema": _SCOPE_REQUEST_TEMPLATE_SCHEMA,
        "unit_name_grammar": _CERTIFICATION_UNIT_GRAMMAR,
        "description": request.description,
        "pid_roles": list(_CERTIFICATION_PID_ROLES),
        "job_mode": "fail",
        "auxiliary_units": [],
        "policy": _scope_policy_mapping_for_request(request),
    }
    return sha256_hex(dumps_canonical(mapping))


class StartupCertificationError(RuntimeError):
    """The active startup proof was incomplete, ambiguous, or unsupported."""


class StartupCertificationRevoked(StartupCertificationError):
    """A previously completed process-local certificate is no longer current."""


@dataclass(frozen=True, slots=True)
class StartupProbeIdentity:
    process_id: int
    process_start_token: str
    uid: int
    gid: int
    executable_sha256: str

    def __post_init__(self) -> None:
        _require_integer(self.process_id, "process_id", 1)
        _require_process_token(
            self.process_start_token,
            self.process_id,
            "process_start_token",
        )
        _require_integer(self.uid, "uid", 1)
        _require_integer(self.gid, "gid", 1)
        _require_sha256(self.executable_sha256, "executable_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class StartupProbeHandle:
    identity: StartupProbeIdentity
    pidfd: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, StartupProbeIdentity):
            raise ValueError("identity must be a StartupProbeIdentity")
        _require_integer(self.pidfd, "pidfd", 0)


@dataclass(frozen=True, slots=True)
class StartupCertificationBindings:
    broker_process_id: int
    broker_process_start_token: str
    broker_build_id: str
    broker_config_sha256: str
    scope_policy_sha256: str
    scope_request_template_sha256: str
    startup_probe_path: str
    startup_probe_sha256: str
    startup_probe_account: str
    boot_id: str
    kernel_release: str
    system_bus_id: str
    systemd_unique_owner: str
    units_load_timestamp_monotonic: int
    pid1_start_token: str
    pid1_executable_sha256: str
    cgroup_mount_device: int
    cgroup_mount_inode: int

    def __post_init__(self) -> None:
        """Validate the StartupCertificationBindings fields when the record is constructed.

        The checks cover ``broker_process_id``, ``broker_process_start_token``,
        ``broker_build_id``, ``startup_probe_path``, ``kernel_release``,
        ``units_load_timestamp_monotonic``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        _require_integer(self.broker_process_id, "broker_process_id", 1)
        _require_process_token(
            self.broker_process_start_token,
            self.broker_process_id,
            "broker_process_start_token",
        )
        _require_text(self.broker_build_id, "broker_build_id", 128)
        for name in (
            "broker_config_sha256",
            "scope_policy_sha256",
            "scope_request_template_sha256",
            "startup_probe_sha256",
            "pid1_executable_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        _require_canonical_absolute_path(self.startup_probe_path)
        if (
            type(self.startup_probe_account) is not str
            or _ACCOUNT.fullmatch(self.startup_probe_account) is None
        ):
            raise ValueError("startup_probe_account is malformed")
        if type(self.boot_id) is not str or _BOOT_ID.fullmatch(self.boot_id) is None:
            raise ValueError("boot_id is malformed")
        _require_text(self.kernel_release, "kernel_release", 256)
        if type(self.system_bus_id) is not str or _BUS_ID.fullmatch(self.system_bus_id) is None:
            raise ValueError("system_bus_id is malformed")
        if (
            type(self.systemd_unique_owner) is not str
            or _OWNER.fullmatch(self.systemd_unique_owner) is None
        ):
            raise ValueError("systemd_unique_owner is malformed")
        _require_integer(
            self.units_load_timestamp_monotonic,
            "units_load_timestamp_monotonic",
            0,
        )
        _require_process_token(self.pid1_start_token, 1, "pid1_start_token")
        _require_integer(self.cgroup_mount_device, "cgroup_mount_device", 1)
        _require_integer(self.cgroup_mount_inode, "cgroup_mount_inode", 1)


class StartupBindingObserver(Protocol):
    def assert_bindings_current(
        self,
        bindings: StartupCertificationBindings,
    ) -> None: ...

    def assert_certificate_current(
        self,
        certification: "BrokerStartupCertification",
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StartupCertificationScopePolicy:
    """Administrator-fixed scope policy, separate from certification waits."""

    graceful_stop_seconds: float
    maximum_runtime_seconds: float

    def __post_init__(self) -> None:
        values = (self.graceful_stop_seconds, self.maximum_runtime_seconds)
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in values
        ):
            raise ValueError("startup certification scope policy must be finite and positive")


@dataclass(frozen=True, slots=True)
class StartupCertificationDeadlines:
    total_seconds: float
    probe_ready_seconds: float
    start_job_seconds: float
    stop_job_seconds: float
    cleanup_seconds: float

    def __post_init__(self) -> None:
        values = tuple(getattr(self, item.name) for item in fields(self))
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in values
        ):
            raise ValueError("startup certification deadlines must be finite and positive")
        if any(value > self.total_seconds for value in values[1:]):
            raise ValueError("step deadline cannot exceed the total deadline")


class StartupProbeLauncher(Protocol):
    async def launch_pair(
        self,
        bindings: StartupCertificationBindings,
        deadlines: StartupCertificationDeadlines,
    ) -> tuple[StartupProbeHandle, StartupProbeHandle]: ...

    async def kill_and_reap(
        self,
        handles: tuple[StartupProbeHandle, StartupProbeHandle],
        *,
        timeout_seconds: float,
    ) -> None: ...


class _StartupProbeTrampolinePrimitives(Protocol):
    def redirect_standard_descriptors_to_devnull(self) -> None: ...

    def remap_descriptor(self, source: int, target: int) -> None: ...

    def close_fds_except(self, retained: frozenset[int]) -> None: ...

    def chdir(self, path: str) -> None: ...

    def clear_supplementary_groups(self) -> None: ...

    def clear_capabilities(self) -> None: ...

    def setresgid(self, gid: int) -> None: ...

    def setfsgid(self, gid: int) -> None: ...

    def setresuid(self, uid: int) -> None: ...

    def setfsuid(self, uid: int) -> None: ...

    def set_no_new_privileges(self) -> None: ...

    def set_dumpable_off(self) -> None: ...

    def set_parent_death_signal(self, signum: int) -> None: ...

    def parent_identity(self) -> tuple[int, str]: ...

    def exec_probe(self, path: str) -> None: ...


def _run_probe_trampoline(
    operations: _StartupProbeTrampolinePrimitives,
    *,
    bindings: StartupCertificationBindings,
    bootstrap_read_fd: int,
    readiness_write_fd: int,
    probe_uid: int,
    probe_gid: int,
) -> None:
    """Enter the fixed credential-dropped child context and exec the probe."""

    if bootstrap_read_fd == readiness_write_fd:
        raise StartupCertificationError("startup-probe pipe ends are not distinct")
    if type(probe_uid) is not int or type(probe_gid) is not int or probe_uid < 1 or probe_gid < 1:
        raise StartupCertificationError("startup-probe account must be non-root")
    operations.redirect_standard_descriptors_to_devnull()
    operations.remap_descriptor(bootstrap_read_fd, BOOTSTRAP_FD)
    operations.remap_descriptor(readiness_write_fd, READINESS_FD)
    operations.close_fds_except(_EXPECTED_PROBE_FDS)
    operations.chdir("/")
    operations.clear_supplementary_groups()
    operations.clear_capabilities()
    operations.setresgid(probe_gid)
    operations.setfsgid(probe_gid)
    operations.setresuid(probe_uid)
    operations.setfsuid(probe_uid)
    operations.set_no_new_privileges()
    operations.set_dumpable_off()
    # Credential changes clear PDEATHSIG; arm it only after every final drop.
    operations.set_parent_death_signal(signal.SIGKILL)
    if operations.parent_identity() != (
        bindings.broker_process_id,
        bindings.broker_process_start_token,
    ):
        raise StartupCertificationError("startup-probe broker parent changed")
    try:
        operations.exec_probe(bindings.startup_probe_path)
    except BaseException as error:
        raise StartupCertificationError("startup-probe exec failed") from error
    raise StartupCertificationError("startup-probe exec returned unexpectedly")


class _StartupProbeLaunchRuntime(_StartupProbeTrampolinePrimitives, Protocol):
    def monotonic(self) -> float: ...

    def claim_single_forking_thread(self) -> object: ...

    def assert_forking_thread_current(self, identity: object) -> None: ...

    def validate_probe_asset(self, bindings: StartupCertificationBindings) -> None: ...

    def probe_account(self, name: str) -> tuple[int, int]: ...

    def create_pipe(self) -> tuple[int, int]: ...

    def fork_probe(
        self,
        *,
        bindings: StartupCertificationBindings,
        bootstrap_read_fd: int,
        readiness_write_fd: int,
        probe_uid: int,
        probe_gid: int,
    ) -> int: ...

    def close_fd(self, descriptor: int) -> None: ...

    async def write_bootstrap(
        self,
        descriptor: int,
        frame: bytes,
        deadline: float,
    ) -> None: ...

    async def read_readiness(
        self,
        descriptor: int,
        frame: bytes,
        deadline: float,
    ) -> None: ...

    def open_pidfd(self, pid: int) -> int: ...

    def observe_initial_probe(
        self,
        pid: int,
        pidfd: int,
        bindings: StartupCertificationBindings,
        deadline: float,
    ) -> StartupProbeIdentity: ...

    def kill_pidfd(self, pidfd: int) -> None: ...

    def kill_unreaped_child(self, pid: int) -> None: ...

    async def reap_pid(self, pid: int, deadline: float) -> None: ...


@dataclass(slots=True)
class PosixStartupProbeLauncher:
    """One-shot Linux launcher owning the two exact probe child lifecycles."""

    _forking_thread: object | None = field(default=None, init=False)
    _owned_handles: tuple[StartupProbeHandle, StartupProbeHandle] | None = field(
        default=None,
        init=False,
    )
    _reaped: bool = field(default=False, init=False)

    async def launch_pair(
        self,
        bindings: StartupCertificationBindings,
        deadlines: StartupCertificationDeadlines,
    ) -> tuple[StartupProbeHandle, StartupProbeHandle]:
        if self._forking_thread is not None:
            raise StartupCertificationError("startup-probe launch cannot be repeated")
        if not isinstance(bindings, StartupCertificationBindings):
            raise TypeError("bindings must be StartupCertificationBindings")
        if not isinstance(deadlines, StartupCertificationDeadlines):
            raise TypeError("deadlines must be StartupCertificationDeadlines")
        runtime = _STARTUP_PROBE_LAUNCH_RUNTIME.get()
        thread_identity = runtime.claim_single_forking_thread()
        self._forking_thread = thread_identity
        runtime.validate_probe_asset(bindings)
        probe_uid, probe_gid = runtime.probe_account(bindings.startup_probe_account)
        if (
            type(probe_uid) is not int
            or type(probe_gid) is not int
            or probe_uid < 1
            or probe_gid < 1
        ):
            raise StartupCertificationError("startup-probe account must be non-root")
        deadline = runtime.monotonic() + deadlines.probe_ready_seconds
        bootstrap = encode_probe_bootstrap_frame(
            bindings.broker_process_id,
            bindings.broker_process_start_token,
        )
        records: list[_ProbeChildRecord] = []
        handles: list[StartupProbeHandle] = []
        open_descriptors: set[int] = set()
        try:
            for _ in range(2):
                runtime.assert_forking_thread_current(thread_identity)
                bootstrap_read, bootstrap_write = runtime.create_pipe()
                open_descriptors.update((bootstrap_read, bootstrap_write))
                readiness_read, readiness_write = runtime.create_pipe()
                open_descriptors.update((readiness_read, readiness_write))
                pid = runtime.fork_probe(
                    bindings=bindings,
                    bootstrap_read_fd=bootstrap_read,
                    readiness_write_fd=readiness_write,
                    probe_uid=probe_uid,
                    probe_gid=probe_gid,
                )
                record = _ProbeChildRecord(pid=pid)
                records.append(record)
                runtime.close_fd(bootstrap_read)
                open_descriptors.remove(bootstrap_read)
                runtime.close_fd(readiness_write)
                open_descriptors.remove(readiness_write)
                pidfd = runtime.open_pidfd(pid)
                record.pidfd = pidfd
                await runtime.write_bootstrap(bootstrap_write, bootstrap, deadline)
                runtime.close_fd(bootstrap_write)
                open_descriptors.remove(bootstrap_write)
                await runtime.read_readiness(readiness_read, READINESS_FRAME, deadline)
                runtime.close_fd(readiness_read)
                open_descriptors.remove(readiness_read)
                identity = runtime.observe_initial_probe(
                    pid,
                    pidfd,
                    bindings,
                    deadline,
                )
                handles.append(StartupProbeHandle(identity=identity, pidfd=pidfd))
            runtime.assert_forking_thread_current(thread_identity)
            pair = _validate_probe_pair(tuple(handles), bindings)
            self._owned_handles = pair
            return pair
        except BaseException as error:
            cleanup_error: BaseException | None = None
            for descriptor in tuple(open_descriptors):
                try:
                    runtime.close_fd(descriptor)
                    open_descriptors.remove(descriptor)
                except BaseException as close_error:
                    cleanup_error = cleanup_error or close_error
            cleanup_deadline = runtime.monotonic() + deadlines.cleanup_seconds
            child_cleanup_error = await _kill_and_reap_records(
                runtime,
                records,
                cleanup_deadline,
            )
            cleanup_error = cleanup_error or child_cleanup_error
            if cleanup_error is not None:
                raise StartupCertificationError(
                    "startup-probe launch cleanup was incomplete"
                ) from cleanup_error
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, StartupCertificationError):
                raise
            raise StartupCertificationError("startup-probe launch failed") from error

    async def kill_and_reap(
        self,
        handles: tuple[StartupProbeHandle, StartupProbeHandle],
        *,
        timeout_seconds: float,
    ) -> None:
        if self._forking_thread is None or self._owned_handles != handles or self._reaped:
            raise StartupCertificationError("startup-probe reaper ownership was lost")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise StartupCertificationError("startup-probe reap deadline is invalid")
        runtime = _STARTUP_PROBE_LAUNCH_RUNTIME.get()
        ownership_error: BaseException | None = None
        try:
            runtime.assert_forking_thread_current(self._forking_thread)
        except BaseException as error:
            ownership_error = error
        deadline = runtime.monotonic() + float(timeout_seconds)
        records = [
            _ProbeChildRecord(handle.identity.process_id, handle.pidfd) for handle in handles
        ]
        error = await _kill_and_reap_records(runtime, records, deadline)
        if error is not None:
            raise StartupCertificationError("startup-probe exact reap failed") from error
        self._reaped = True
        if ownership_error is not None:
            raise StartupCertificationError(
                "startup-probe forking thread/reaper changed"
            ) from ownership_error


@dataclass(slots=True)
class _ProbeChildRecord:
    pid: int
    pidfd: int | None = None
    reaped: bool = False


async def _kill_and_reap_records(
    runtime: _StartupProbeLaunchRuntime,
    records: list[_ProbeChildRecord],
    deadline: float,
) -> BaseException | None:
    failure: BaseException | None = None
    for record in records:
        try:
            if record.pidfd is None:
                runtime.kill_unreaped_child(record.pid)
            else:
                runtime.kill_pidfd(record.pidfd)
        except ProcessLookupError:
            pass
        except BaseException as error:
            failure = failure or error
    for record in records:
        try:
            await runtime.reap_pid(record.pid, deadline)
            record.reaped = True
        except BaseException as error:
            failure = failure or error
    for record in records:
        if record.pidfd is None or not record.reaped:
            continue
        try:
            runtime.close_fd(record.pidfd)
        except BaseException as error:
            failure = failure or error
    return failure


class _PosixStartupProbeLaunchRuntime:
    def __init__(self) -> None:
        self._libc: ctypes.CDLL | None = None

    def monotonic(self) -> float:
        return time.monotonic()

    def claim_single_forking_thread(self) -> object:
        current = threading.current_thread()
        live = tuple(thread for thread in threading.enumerate() if thread.is_alive())
        if live != (current,):
            raise StartupCertificationError(
                "startup certification requires one sole forking thread"
            )
        return current

    def assert_forking_thread_current(self, identity: object) -> None:
        current = threading.current_thread()
        live = tuple(thread for thread in threading.enumerate() if thread.is_alive())
        if identity is not current or not current.is_alive() or live != (current,):
            raise StartupCertificationError(
                "startup-probe forking thread/reaper is no longer exclusive"
            )

    def validate_probe_asset(self, bindings: StartupCertificationBindings) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(bindings.startup_probe_path, flags)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != 0
                or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise StartupCertificationError("startup-probe asset is not locked and root-owned")
            prefix = os.read(descriptor, len(_PROBE_SHEBANG))
            if prefix != _PROBE_SHEBANG:
                raise StartupCertificationError(
                    "startup-probe asset lacks the fixed isolated shebang"
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _sha256_fd(descriptor) != bindings.startup_probe_sha256:
                raise StartupCertificationError("startup-probe asset digest changed")
        finally:
            os.close(descriptor)

    def probe_account(self, name: str) -> tuple[int, int]:
        try:
            account = pwd.getpwnam(name)
        except KeyError as error:
            raise StartupCertificationError("startup-probe account is unavailable") from error
        return account.pw_uid, account.pw_gid

    def create_pipe(self) -> tuple[int, int]:
        return os.pipe2(getattr(os, "O_CLOEXEC", 0))

    def fork_probe(
        self,
        *,
        bindings: StartupCertificationBindings,
        bootstrap_read_fd: int,
        readiness_write_fd: int,
        probe_uid: int,
        probe_gid: int,
    ) -> int:
        pid = os.fork()
        if pid != 0:
            return pid
        try:
            _run_probe_trampoline(
                self,
                bindings=bindings,
                bootstrap_read_fd=bootstrap_read_fd,
                readiness_write_fd=readiness_write_fd,
                probe_uid=probe_uid,
                probe_gid=probe_gid,
            )
        except BaseException:
            os._exit(1)
        os._exit(1)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    async def write_bootstrap(
        self,
        descriptor: int,
        frame: bytes,
        deadline: float,
    ) -> None:
        os.set_blocking(descriptor, False)
        offset = 0
        while offset < len(frame):
            if time.monotonic() >= deadline:
                raise StartupCertificationError("startup-probe bootstrap deadline expired")
            try:
                written = os.write(descriptor, frame[offset:])
            except BlockingIOError:
                await asyncio.sleep(0)
                continue
            if written <= 0:
                raise StartupCertificationError("startup-probe bootstrap write failed")
            offset += written

    async def read_readiness(
        self,
        descriptor: int,
        frame: bytes,
        deadline: float,
    ) -> None:
        os.set_blocking(descriptor, False)
        content = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise StartupCertificationError("startup-probe readiness deadline expired")
            try:
                chunk = os.read(descriptor, len(frame) - len(content) + 1)
            except BlockingIOError:
                await asyncio.sleep(0)
                continue
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > len(frame):
                raise StartupCertificationError("startup-probe readiness is overlong")
        if bytes(content) != frame:
            raise StartupCertificationError("startup-probe readiness is incomplete")

    def open_pidfd(self, pid: int) -> int:
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            raise StartupCertificationError("pidfd_open is unavailable")
        return int(opener(pid, 0))

    def observe_initial_probe(
        self,
        pid: int,
        pidfd: int,
        bindings: StartupCertificationBindings,
        deadline: float,
    ) -> StartupProbeIdentity:
        self._wait_stopped(pid, deadline)
        signal.pidfd_send_signal(pidfd, 0, None, 0)
        process_start_token, parent_pid, state = _process_stat_identity(pid)
        if state not in ("T", "t") or parent_pid != bindings.broker_process_id:
            raise StartupCertificationError("startup probe is not the exact stopped child")
        uid, gid = self.probe_account(bindings.startup_probe_account)
        status = _parse_status(pid)
        expected_uid = f"{uid}\t{uid}\t{uid}\t{uid}"
        expected_gid = f"{gid}\t{gid}\t{gid}\t{gid}"
        if status.get("Uid") != expected_uid or status.get("Gid") != expected_gid:
            raise StartupCertificationError("startup-probe credentials are not exact")
        if status.get("Groups", "") != "" or status.get("NoNewPrivs") != "1":
            raise StartupCertificationError("startup-probe group or NNP state is not exact")
        if any(
            status.get(name) != "0000000000000000"
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ):
            raise StartupCertificationError("startup-probe capabilities are not empty")
        expected_cmdline = (
            b"/usr/bin/python3\0-I\0" + os.fsencode(bindings.startup_probe_path) + b"\0"
        )
        if _read_bounded_path(f"/proc/{pid}/cmdline", _MAXIMUM_PROC_BYTES) != expected_cmdline:
            raise StartupCertificationError("startup-probe isolated execution changed")
        if _open_fds(pid) != frozenset((0, 1, 2)):
            raise StartupCertificationError("startup probe retained a foreign descriptor")
        if any(
            os.readlink(f"/proc/{pid}/fd/{descriptor}") != "/dev/null" for descriptor in (0, 1, 2)
        ):
            raise StartupCertificationError("startup-probe standard descriptors changed")
        self.validate_probe_asset(bindings)
        return StartupProbeIdentity(
            process_id=pid,
            process_start_token=process_start_token,
            uid=uid,
            gid=gid,
            executable_sha256=bindings.startup_probe_sha256,
        )

    def kill_pidfd(self, pidfd: int) -> None:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)

    def kill_unreaped_child(self, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)

    async def reap_pid(self, pid: int, deadline: float) -> None:
        while True:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError as error:
                raise StartupCertificationError(
                    "startup-probe reaper ownership was lost"
                ) from error
            if waited == pid:
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    return
                raise StartupCertificationError("startup-probe reap returned a nonterminal state")
            if waited != 0:
                raise StartupCertificationError("startup-probe reap returned another child")
            if time.monotonic() >= deadline:
                raise StartupCertificationError("startup-probe reap deadline expired")
            await asyncio.sleep(0)

    def redirect_standard_descriptors_to_devnull(self) -> None:
        descriptor = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        try:
            for target in (0, 1, 2):
                os.dup2(descriptor, target, inheritable=True)
        finally:
            if descriptor > 2:
                os.close(descriptor)

    def remap_descriptor(self, source: int, target: int) -> None:
        if source == target:
            os.set_inheritable(target, True)
        else:
            os.dup2(source, target, inheritable=True)

    def close_fds_except(self, retained: frozenset[int]) -> None:
        for name in os.listdir("/proc/self/fd"):
            if not name.isascii() or not name.isdigit():
                raise StartupCertificationError("child descriptor inventory is malformed")
            descriptor = int(name, 10)
            if descriptor in retained:
                continue
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise

    def chdir(self, path: str) -> None:
        os.chdir(path)

    def clear_supplementary_groups(self) -> None:
        os.setgroups([])

    def clear_capabilities(self) -> None:
        for capability in range(_MAXIMUM_CAPABILITY_NUMBER + 1):
            try:
                self._prctl(_PR_CAPBSET_DROP, capability)
            except OSError as error:
                if error.errno != errno.EINVAL:
                    raise
        try:
            self._prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise

        class _CapabilityHeader(ctypes.Structure):
            _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))

        class _CapabilityData(ctypes.Structure):
            _fields_ = (
                ("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32),
            )

        libc = self._get_libc()
        libc.capset.argtypes = (
            ctypes.POINTER(_CapabilityHeader),
            ctypes.POINTER(_CapabilityData),
        )
        libc.capset.restype = ctypes.c_int
        header = _CapabilityHeader(_LINUX_CAPABILITY_VERSION_3, 0)
        data = (_CapabilityData * 2)()
        # The final UID/GID transition clears permitted/effective capabilities,
        # but it first needs these two narrow powers to complete all four IDs.
        credential_drop = (1 << 6) | (1 << 7)  # CAP_SETGID | CAP_SETUID
        data[0].effective = credential_drop
        data[0].permitted = credential_drop
        if libc.capset(ctypes.byref(header), data) != 0:
            self._raise_errno()

    def setresgid(self, gid: int) -> None:
        os.setresgid(gid, gid, gid)

    def setfsgid(self, gid: int) -> None:
        libc = self._get_libc()
        libc.setfsgid.argtypes = (ctypes.c_uint,)
        libc.setfsgid.restype = ctypes.c_int
        libc.setfsgid(gid)
        if libc.setfsgid(gid) != gid:
            raise StartupCertificationError("startup-probe filesystem GID drop failed")

    def setresuid(self, uid: int) -> None:
        os.setresuid(uid, uid, uid)

    def setfsuid(self, uid: int) -> None:
        libc = self._get_libc()
        libc.setfsuid.argtypes = (ctypes.c_uint,)
        libc.setfsuid.restype = ctypes.c_int
        libc.setfsuid(uid)
        if libc.setfsuid(uid) != uid:
            raise StartupCertificationError("startup-probe filesystem UID drop failed")

    def set_no_new_privileges(self) -> None:
        self._prctl(_PR_SET_NO_NEW_PRIVS, 1)

    def set_dumpable_off(self) -> None:
        self._prctl(_PR_SET_DUMPABLE, 0)

    def set_parent_death_signal(self, signum: int) -> None:
        self._prctl(_PR_SET_PDEATHSIG, signum)

    def parent_identity(self) -> tuple[int, str]:
        before = os.getppid()
        token, _, _ = _process_stat_identity(before)
        after = os.getppid()
        if before != after:
            raise StartupCertificationError("startup-probe parent changed during observation")
        return before, token

    def exec_probe(self, path: str) -> None:
        os.execve(path, (path,), {})

    def _wait_stopped(self, pid: int, deadline: float) -> None:
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
            if waited == pid:
                if os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP:
                    return
                raise StartupCertificationError("startup probe did not enter exact SIGSTOP")
            if waited != 0:
                raise StartupCertificationError("startup-probe stop wait returned another child")
            if time.monotonic() >= deadline:
                raise StartupCertificationError("startup-probe stop deadline expired")
            time.sleep(0.001)

    def _prctl(self, option: int, argument: int) -> None:
        libc = self._get_libc()
        libc.prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(option, argument, 0, 0, 0) != 0:
            self._raise_errno()

    def _get_libc(self) -> ctypes.CDLL:
        if self._libc is None:
            self._libc = ctypes.CDLL(None, use_errno=True)
        return self._libc

    @staticmethod
    def _raise_errno() -> None:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


_PRODUCTION_STARTUP_PROBE_LAUNCH_RUNTIME = _PosixStartupProbeLaunchRuntime()
_STARTUP_PROBE_LAUNCH_RUNTIME: contextvars.ContextVar[_StartupProbeLaunchRuntime] = (
    contextvars.ContextVar(
        "mif_containment_startup_probe_launch_runtime",
        default=_PRODUCTION_STARTUP_PROBE_LAUNCH_RUNTIME,
    )
)


@contextmanager
def _override_startup_probe_launch_runtime(
    runtime: _StartupProbeLaunchRuntime,
) -> Iterator[None]:
    token = _STARTUP_PROBE_LAUNCH_RUNTIME.set(runtime)
    try:
        yield
    finally:
        _STARTUP_PROBE_LAUNCH_RUNTIME.reset(token)


@dataclass(frozen=True, slots=True)
class SystemdCompletedJobEvidence:
    job_path: str
    result: str

    def __post_init__(self) -> None:
        if type(self.job_path) is not str or _JOB_PATH.fullmatch(self.job_path) is None:
            raise ValueError("job_path must be an exact systemd job path")
        if self.result != "done" or type(self.result) is not str:
            raise ValueError("certification job result must be exactly done")

    def to_mapping(self) -> dict[str, object]:
        return {"job_path": self.job_path, "result": self.result}


@dataclass(frozen=True, slots=True)
class BrokerStartupCertification:
    broker_process_id: int
    broker_process_start_token: str
    broker_build_id: str
    broker_config_sha256: str
    scope_policy_sha256: str
    scope_request_template_sha256: str
    startup_probe_path: str
    startup_probe_sha256: str
    startup_probe_account: str
    boot_id: str
    kernel_release: str
    system_bus_id: str
    systemd_unique_owner: str
    units_load_timestamp_monotonic: int
    pid1_start_token: str
    pid1_executable_sha256: str
    cgroup_identity: CgroupDirectoryIdentityV1
    probe_identities: tuple[StartupProbeIdentity, StartupProbeIdentity]
    unit_name: str
    invocation_id: str
    control_group: str
    fixed_scope_properties_sha256: str
    initial_populated: int
    start_job: SystemdCompletedJobEvidence
    stop_job: SystemdCompletedJobEvidence
    probes_reaped: bool
    drain_proof: CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1
    certified_claims: tuple[str, ...]
    certification_sha256: str

    def __post_init__(self) -> None:
        """Validate the BrokerStartupCertification fields when the record is constructed.

        The checks cover ``probe_identities``, ``control_group``,
        ``fixed_scope_properties_sha256``, ``scope_policy_sha256``, ``probes_reaped``,
        ``certification_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        bindings = StartupCertificationBindings(
            broker_process_id=self.broker_process_id,
            broker_process_start_token=self.broker_process_start_token,
            broker_build_id=self.broker_build_id,
            broker_config_sha256=self.broker_config_sha256,
            scope_policy_sha256=self.scope_policy_sha256,
            scope_request_template_sha256=self.scope_request_template_sha256,
            startup_probe_path=self.startup_probe_path,
            startup_probe_sha256=self.startup_probe_sha256,
            startup_probe_account=self.startup_probe_account,
            boot_id=self.boot_id,
            kernel_release=self.kernel_release,
            system_bus_id=self.system_bus_id,
            systemd_unique_owner=self.systemd_unique_owner,
            units_load_timestamp_monotonic=self.units_load_timestamp_monotonic,
            pid1_start_token=self.pid1_start_token,
            pid1_executable_sha256=self.pid1_executable_sha256,
            cgroup_mount_device=self.cgroup_identity.mount_device,
            cgroup_mount_inode=self.cgroup_identity.mount_inode,
        )
        del bindings
        if (
            type(self.probe_identities) is not tuple
            or len(self.probe_identities) != 2
            or any(
                not isinstance(identity, StartupProbeIdentity) for identity in self.probe_identities
            )
            or self.probe_identities[0] == self.probe_identities[1]
            or self.probe_identities[0].process_id == self.probe_identities[1].process_id
        ):
            raise ValueError("probe_identities must contain two distinct exact probes")
        first, second = self.probe_identities
        if (
            first.uid != second.uid
            or first.gid != second.gid
            or first.executable_sha256 != self.startup_probe_sha256
            or second.executable_sha256 != self.startup_probe_sha256
        ):
            raise ValueError("probe identities must share the fixed account and executable")
        if type(self.unit_name) is not str or _CERTIFICATION_UNIT.fullmatch(self.unit_name) is None:
            raise ValueError("unit_name is outside the certification namespace")
        if (
            type(self.invocation_id) is not str
            or _INVOCATION_ID.fullmatch(self.invocation_id) is None
        ):
            raise ValueError("invocation_id is malformed")
        expected_control_group = "/" + "/".join(
            item.basename for item in self.cgroup_identity.components
        )
        if self.control_group != expected_control_group:
            raise ValueError("control_group does not match the verified cgroup identity")
        expected_components = tuple(self.control_group.removeprefix("/").split("/"))
        if tuple(item.basename for item in self.cgroup_identity.components) != expected_components:
            raise ValueError("cgroup identity does not match the certification ControlGroup")
        _require_sha256(
            self.fixed_scope_properties_sha256,
            "fixed_scope_properties_sha256",
        )
        if self.fixed_scope_properties_sha256 != self.scope_policy_sha256:
            raise ValueError("fixed scope properties do not match the bound policy")
        if type(self.initial_populated) is not int or self.initial_populated != 1:
            raise ValueError("initial_populated must be the exact integer one")
        if not isinstance(self.start_job, SystemdCompletedJobEvidence):
            raise ValueError("start_job must be closed completed-job evidence")
        if not isinstance(self.stop_job, SystemdCompletedJobEvidence):
            raise ValueError("stop_job must be closed completed-job evidence")
        if self.start_job.job_path == self.stop_job.job_path:
            raise ValueError("start and stop jobs must be distinct")
        if self.probes_reaped is not True:
            raise ValueError("probes_reaped must be exactly true")
        if (
            type(self.drain_proof)
            not in (
                CgroupEventsZeroProofV1,
                CgroupDeletedAfterStopProofV1,
            )
            or self.drain_proof.cgroup_identity != self.cgroup_identity
        ):
            raise ValueError("drain proof must close the exact certification cgroup")
        if type(self.certified_claims) is not tuple or self.certified_claims != CERTIFIED_CLAIMS:
            raise ValueError("certified_claims must contain the exact closed claim set")
        _require_sha256(self.certification_sha256, "certification_sha256")
        if sha256_hex(dumps_canonical(self._material_mapping())) != self.certification_sha256:
            raise ValueError("certification digest does not match its complete material")

    def _material_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for item in fields(self):
            if item.name == "certification_sha256":
                continue
            value = getattr(self, item.name)
            if isinstance(value, CgroupDirectoryIdentityV1):
                result[item.name] = value.to_mapping()
            elif isinstance(value, StartupProbeIdentity):
                result[item.name] = value.to_mapping()
            elif isinstance(value, SystemdCompletedJobEvidence):
                result[item.name] = value.to_mapping()
            elif isinstance(value, (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1)):
                result[item.name] = value.to_mapping()
            elif type(value) is tuple:
                result[item.name] = [
                    member.to_mapping() if hasattr(member, "to_mapping") else member
                    for member in value
                ]
            else:
                result[item.name] = value
        return result

    def to_summary(self) -> StartupCertificationSummaryV1:
        return StartupCertificationSummaryV1(
            certification_sha256=self.certification_sha256,
            broker_build_id=self.broker_build_id,
            broker_config_sha256=self.broker_config_sha256,
            boot_id=self.boot_id,
            scope_policy_sha256=self.scope_policy_sha256,
            broker_process_id=self.broker_process_id,
            broker_process_start_token=self.broker_process_start_token,
            system_bus_id=self.system_bus_id,
            systemd_unique_owner=self.systemd_unique_owner,
            certified_claims=self.certified_claims,
        )


def assert_broker_capabilities_match(
    certification: BrokerStartupCertification,
    capabilities: BrokerCapabilitiesV1,
    *,
    security_profile: BrokerSecurityProfileV1,
) -> None:
    """Reject a capabilities response not wholly derived from one certificate."""

    if not isinstance(certification, BrokerStartupCertification):
        raise TypeError("certification must be a BrokerStartupCertification")
    if not isinstance(capabilities, BrokerCapabilitiesV1):
        raise TypeError("capabilities must be a BrokerCapabilitiesV1")
    if not isinstance(security_profile, BrokerSecurityProfileV1):
        raise TypeError("security_profile must be a BrokerSecurityProfileV1")
    expected = (
        PROVIDER,
        PROTOCOL_VERSION,
        PROTOCOL_VERSION,
        PROTOCOL_VERSION,
        certification.broker_build_id,
        certification.broker_config_sha256,
        certification.boot_id,
        certification.scope_policy_sha256,
        CONTROL_SOCKET_FD,
        RETAINED_TARGET_FD,
        tuple(BrokerOperation),
        certification.to_summary(),
        security_profile,
    )
    actual = (
        capabilities.provider,
        capabilities.protocol_min,
        capabilities.protocol_max,
        capabilities.protocol_version,
        capabilities.broker_build_id,
        capabilities.broker_config_sha256,
        capabilities.boot_id,
        capabilities.scope_policy_sha256,
        capabilities.control_socket_fd,
        capabilities.retained_target_fd,
        capabilities.operations,
        capabilities.startup_certification,
        capabilities.security_profile,
    )
    if actual != expected:
        raise ValueError("broker capabilities do not match startup certification")


def broker_capabilities_from_certification(
    certification: BrokerStartupCertification,
    *,
    security_profile: BrokerSecurityProfileV1,
) -> BrokerCapabilitiesV1:
    """Author the sole trusted capabilities response from retained proof."""

    if not isinstance(certification, BrokerStartupCertification):
        raise TypeError("certification must be a BrokerStartupCertification")
    if not isinstance(security_profile, BrokerSecurityProfileV1):
        raise TypeError("security_profile must be a BrokerSecurityProfileV1")
    capabilities = BrokerCapabilitiesV1(
        provider=PROVIDER,
        protocol_min=PROTOCOL_VERSION,
        protocol_max=PROTOCOL_VERSION,
        protocol_version=PROTOCOL_VERSION,
        broker_build_id=certification.broker_build_id,
        broker_config_sha256=certification.broker_config_sha256,
        boot_id=certification.boot_id,
        scope_policy_sha256=certification.scope_policy_sha256,
        control_socket_fd=CONTROL_SOCKET_FD,
        retained_target_fd=RETAINED_TARGET_FD,
        operations=tuple(BrokerOperation),
        startup_certification=certification.to_summary(),
        security_profile=security_profile,
    )
    assert_broker_capabilities_match(
        certification,
        capabilities,
        security_profile=security_profile,
    )
    return capabilities


class _CertificationRuntime(Protocol):
    def monotonic(self) -> float: ...

    def new_unit_name(self) -> str: ...

    async def verify_scope_attachment(
        self,
        manager: SystemdManager,
        request: ScopeStartRequest,
    ) -> object: ...

    def assert_probe_pair_current(
        self,
        handles: tuple[StartupProbeHandle, StartupProbeHandle],
        bindings: StartupCertificationBindings,
        control_group: str,
    ) -> None: ...

    def read_populated(self, scope: object) -> int: ...

    async def await_drain_proof(
        self,
        scope: object,
        timeout_seconds: float,
    ) -> CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1: ...

    def close_scope(self, scope: object) -> None: ...


@dataclass(frozen=True, slots=True)
class _ProductionCertificationRuntime:
    def monotonic(self) -> float:
        return time.monotonic()

    def new_unit_name(self) -> str:
        return f"mif-qupath-cert-{secrets.token_hex(16)}.scope"

    async def verify_scope_attachment(
        self,
        manager: SystemdManager,
        request: ScopeStartRequest,
    ) -> VerifiedScope:
        return await verify_scope_attachment(
            manager,
            request,
            previous_invocation_id=None,
        )

    def assert_probe_pair_current(
        self,
        handles: tuple[StartupProbeHandle, StartupProbeHandle],
        bindings: StartupCertificationBindings,
        control_group: str,
    ) -> None:
        for handle in handles:
            _assert_probe_current(handle, bindings, control_group)

    def read_populated(self, scope: object) -> int:
        if not isinstance(scope, VerifiedScope):
            raise StartupCertificationError("verified certification scope is unavailable")
        return read_cgroup_events_populated(scope.cgroup)

    async def await_drain_proof(
        self,
        scope: object,
        timeout_seconds: float,
    ) -> CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1:
        if not isinstance(scope, VerifiedScope):
            raise StartupCertificationError("verified certification scope is unavailable")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                zero = prove_cgroup_events_zero(scope.cgroup)
            except CgroupVerificationError:
                zero = None
            if zero is not None:
                return zero
            deleted = prove_cgroup_deleted_after_stop(
                scope.cgroup.identity,
                durable_stop_intent_cgroup_identity=scope.cgroup.identity,
            )
            if deleted is not None:
                return deleted
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StartupCertificationError(
                    "certification scope did not reach a closed drain proof"
                )
            await asyncio.sleep(min(0.01, remaining))

    def close_scope(self, scope: object) -> None:
        if isinstance(scope, VerifiedScope):
            scope.cgroup.close()


_PRODUCTION_RUNTIME = _ProductionCertificationRuntime()
_CURRENT_RUNTIME: contextvars.ContextVar[_CertificationRuntime] = contextvars.ContextVar(
    "mif_containment_startup_certification_runtime",
    default=_PRODUCTION_RUNTIME,
)


@contextmanager
def _override_certification_runtime(
    runtime: _CertificationRuntime,
) -> Iterator[None]:
    """Context-local deterministic seam used only by portable unit tests."""

    token = _CURRENT_RUNTIME.set(runtime)
    try:
        yield
    finally:
        _CURRENT_RUNTIME.reset(token)


class _RecordingSystemdManager:
    """Exact manager proxy retaining the start-job result hidden by verification."""

    def __init__(self, manager: SystemdManager) -> None:
        self._manager = manager
        self.start_result: ScopeStartResult | None = None

    async def probe_capabilities(self):
        return await self._manager.probe_capabilities()

    async def install_epoch_watch(self):
        return await self._manager.install_epoch_watch()

    async def read_epoch_snapshot(self, watch):
        return await self._manager.read_epoch_snapshot(watch)

    async def assert_epoch_current(self, watch, snapshot):
        return await self._manager.assert_epoch_current(watch, snapshot)

    async def start_scope(self, request):
        if self.start_result is not None:
            raise StartupCertificationError("certification scope start was repeated")
        result = await self._manager.start_scope(request)
        self.start_result = result
        return result

    async def resolve_unit_by_pid(self, pid):
        return await self._manager.resolve_unit_by_pid(pid)

    async def read_unit_identity(self, unit_name):
        return await self._manager.read_unit_identity(unit_name)

    async def begin_stop_unit(self, unit_name, invocation_id):
        return await self._manager.begin_stop_unit(unit_name, invocation_id)

    async def await_stop_job(self, job):
        return await self._manager.await_stop_job(job)

    async def kill_unit_all(self, unit_name, invocation_id):
        return await self._manager.kill_unit_all(unit_name, invocation_id)

    async def ref_unit(self, unit_name):
        return await self._manager.ref_unit(unit_name)

    async def unref_unit(self, unit_name):
        return await self._manager.unref_unit(unit_name)


async def certify_broker_startup(
    *,
    manager: SystemdManager,
    launcher: StartupProbeLauncher,
    binding_observer: StartupBindingObserver,
    bindings: StartupCertificationBindings,
    scope_policy: StartupCertificationScopePolicy,
    deadlines: StartupCertificationDeadlines,
    epoch_watch: SystemdEpochWatchHandle,
    epoch_snapshot: SystemdEpochSnapshot,
) -> BrokerStartupCertification:
    """Run the one fixed active systemd fixture and return process-local proof."""

    if not isinstance(bindings, StartupCertificationBindings):
        raise TypeError("bindings must be StartupCertificationBindings")
    if not isinstance(scope_policy, StartupCertificationScopePolicy):
        raise TypeError("scope_policy must be a StartupCertificationScopePolicy")
    if not isinstance(deadlines, StartupCertificationDeadlines):
        raise TypeError("deadlines must be StartupCertificationDeadlines")
    if not isinstance(epoch_watch, SystemdEpochWatchHandle):
        raise TypeError("epoch_watch must be SystemdEpochWatchHandle")
    if not isinstance(epoch_snapshot, SystemdEpochSnapshot):
        raise TypeError("epoch_snapshot must be SystemdEpochSnapshot")
    _assert_snapshot_bindings(epoch_watch, epoch_snapshot, bindings)

    runtime = _CURRENT_RUNTIME.get()
    total_deadline = runtime.monotonic() + deadlines.total_seconds
    handles: tuple[StartupProbeHandle, StartupProbeHandle] | None = None
    reaped = False
    scope: object | None = None
    try:
        await _assert_epoch_and_bindings(
            manager,
            binding_observer,
            bindings,
            epoch_watch,
            epoch_snapshot,
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        report = await _wait_step(
            manager.probe_capabilities(),
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        _assert_prerequisite_report(report, epoch_snapshot)

        launched = await _wait_step(
            launcher.launch_pair(bindings, deadlines),
            deadlines.probe_ready_seconds,
            total_deadline,
            runtime,
        )
        handles = _validate_probe_pair(launched, bindings)

        await _assert_epoch_and_bindings(
            manager,
            binding_observer,
            bindings,
            epoch_watch,
            epoch_snapshot,
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        request = ScopeStartRequest(
            unit_name=runtime.new_unit_name(),
            description=_CERTIFICATION_DESCRIPTION,
            gate_pid=handles[0].identity.process_id,
            anchor_pid=handles[1].identity.process_id,
            timeout_stop_usec=_seconds_to_usec(scope_policy.graceful_stop_seconds),
            runtime_max_usec=_seconds_to_usec(scope_policy.maximum_runtime_seconds),
        )
        request_policy_sha256 = scope_policy_sha256_for_request(request)
        if request_policy_sha256 != bindings.scope_policy_sha256:
            raise StartupCertificationError(
                "certification request policy differs from its configured binding"
            )
        if scope_request_template_sha256(request) != bindings.scope_request_template_sha256:
            raise StartupCertificationError(
                "certification request template differs from its configured binding"
            )
        recorder = _RecordingSystemdManager(manager)
        scope = await _wait_step(
            runtime.verify_scope_attachment(recorder, request),
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        start_result = recorder.start_result
        identity, cgroup_identity = _validate_verified_scope(scope, request, bindings)
        observed_policy_sha256 = scope_policy_sha256_for_identity(identity)
        if (
            observed_policy_sha256 != request_policy_sha256
            or observed_policy_sha256 != bindings.scope_policy_sha256
        ):
            raise StartupCertificationError(
                "observed certification policy differs from its request binding"
            )
        start_job = _start_job_evidence(start_result, request)
        runtime.assert_probe_pair_current(handles, bindings, identity.control_group)
        populated = runtime.read_populated(scope)
        if type(populated) is not int or populated != 1:
            raise StartupCertificationError("certification scope was not observably populated")

        stop_identity = await _wait_step(
            manager.begin_stop_unit(identity.unit_name, identity.invocation_id),
            deadlines.stop_job_seconds,
            total_deadline,
            runtime,
        )
        if (
            not isinstance(stop_identity, SystemdStopJobIdentity)
            or stop_identity.unit_name != identity.unit_name
            or stop_identity.invocation_id != identity.invocation_id
        ):
            raise StartupCertificationError("certification stop job identity is malformed")
        observed_stop = await _wait_step(
            manager.await_stop_job(stop_identity),
            deadlines.stop_job_seconds,
            total_deadline,
            runtime,
        )
        stop_job = _stop_job_evidence(observed_stop, stop_identity)

        await _wait_step(
            launcher.kill_and_reap(
                handles,
                timeout_seconds=deadlines.cleanup_seconds,
            ),
            deadlines.cleanup_seconds,
            total_deadline,
            runtime,
        )
        reaped = True
        drain_proof = await _wait_step(
            runtime.await_drain_proof(
                scope,
                _remaining(
                    deadlines.cleanup_seconds,
                    total_deadline,
                    runtime,
                ),
            ),
            deadlines.cleanup_seconds,
            total_deadline,
            runtime,
        )
        if (
            type(drain_proof) not in (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1)
            or drain_proof.cgroup_identity != cgroup_identity
        ):
            raise StartupCertificationError(
                "certification drain proof does not bind the exact cgroup"
            )

        await _assert_epoch_and_bindings(
            manager,
            binding_observer,
            bindings,
            epoch_watch,
            epoch_snapshot,
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        certificate = _build_certificate(
            bindings=bindings,
            cgroup_identity=cgroup_identity,
            handles=handles,
            identity=identity,
            start_job=start_job,
            stop_job=stop_job,
            drain_proof=drain_proof,
            fixed_scope_properties_sha256=observed_policy_sha256,
        )
        await _wait_step(
            _observe_certification_epoch(
                certificate,
                manager=manager,
                binding_observer=binding_observer,
                epoch_watch=epoch_watch,
                epoch_snapshot=epoch_snapshot,
            ),
            deadlines.start_job_seconds,
            total_deadline,
            runtime,
        )
        return certificate
    except BaseException as error:
        if handles is not None and not reaped:
            try:
                await asyncio.wait_for(
                    launcher.kill_and_reap(
                        handles,
                        timeout_seconds=deadlines.cleanup_seconds,
                    ),
                    timeout=deadlines.cleanup_seconds,
                )
            except BaseException as cleanup_error:
                raise StartupCertificationError(
                    "startup certification cleanup was incomplete"
                ) from cleanup_error
        if isinstance(error, asyncio.CancelledError):
            raise
        if isinstance(error, StartupCertificationError):
            raise
        raise StartupCertificationError("startup certification failed closed") from error
    finally:
        if scope is not None:
            try:
                runtime.close_scope(scope)
            except BaseException as close_error:
                raise StartupCertificationError(
                    "startup certification scope cleanup was incomplete"
                ) from close_error


async def observe_certification_epoch(
    certification: BrokerStartupCertification,
    *,
    manager: SystemdManager,
    binding_observer: StartupBindingObserver,
    epoch_watch: SystemdEpochWatchHandle,
    epoch_snapshot: SystemdEpochSnapshot,
) -> None:
    """Revalidate both adapter and process-local halves without refreshing."""

    if not isinstance(certification, BrokerStartupCertification):
        raise TypeError("certification must be BrokerStartupCertification")
    try:
        await _observe_certification_epoch(
            certification,
            manager=manager,
            binding_observer=binding_observer,
            epoch_watch=epoch_watch,
            epoch_snapshot=epoch_snapshot,
        )
    except StartupCertificationRevoked:
        raise
    except BaseException as error:
        raise StartupCertificationRevoked(
            "startup certification epoch is no longer current"
        ) from error


async def _observe_certification_epoch(
    certification: BrokerStartupCertification,
    *,
    manager: SystemdManager,
    binding_observer: StartupBindingObserver,
    epoch_watch: SystemdEpochWatchHandle,
    epoch_snapshot: SystemdEpochSnapshot,
) -> None:
    _assert_certificate_snapshot(certification, epoch_watch, epoch_snapshot)
    await manager.assert_epoch_current(epoch_watch, epoch_snapshot)
    binding_observer.assert_certificate_current(certification)


async def _assert_epoch_and_bindings(
    manager: SystemdManager,
    observer: StartupBindingObserver,
    bindings: StartupCertificationBindings,
    watch: SystemdEpochWatchHandle,
    snapshot: SystemdEpochSnapshot,
    timeout: float,
    total_deadline: float,
    runtime: _CertificationRuntime,
) -> None:
    await _wait_step(
        manager.assert_epoch_current(watch, snapshot),
        timeout,
        total_deadline,
        runtime,
    )
    observer.assert_bindings_current(bindings)


async def _wait_step(
    awaitable,
    step_timeout: float,
    total_deadline: float,
    runtime: _CertificationRuntime,
):
    try:
        timeout = _remaining(step_timeout, total_deadline, runtime)
    except BaseException:
        closer = getattr(awaitable, "close", None)
        if callable(closer):
            closer()
        raise
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as error:
        raise StartupCertificationError("startup certification deadline expired") from error


def _remaining(
    step_timeout: float,
    total_deadline: float,
    runtime: _CertificationRuntime,
) -> float:
    total_remaining = total_deadline - runtime.monotonic()
    if total_remaining <= 0:
        raise StartupCertificationError("startup certification total deadline expired")
    return min(float(step_timeout), total_remaining)


def _assert_snapshot_bindings(
    watch: SystemdEpochWatchHandle,
    snapshot: SystemdEpochSnapshot,
    bindings: StartupCertificationBindings,
) -> None:
    expected = (
        watch.watch_id,
        bindings.system_bus_id,
        bindings.systemd_unique_owner,
        False,
        bindings.boot_id,
        bindings.pid1_start_token,
        bindings.pid1_executable_sha256,
        bindings.cgroup_mount_device,
        bindings.cgroup_mount_inode,
        bindings.units_load_timestamp_monotonic,
    )
    actual = (
        snapshot.watch_id,
        snapshot.system_bus_id,
        snapshot.systemd_unique_owner,
        snapshot.reloading,
        snapshot.boot_id,
        snapshot.pid1_start_token,
        snapshot.pid1_executable_sha256,
        snapshot.cgroup_mount_device,
        snapshot.cgroup_mount_inode,
        snapshot.units_load_timestamp_monotonic,
    )
    if actual != expected:
        raise StartupCertificationError("startup bindings do not match the pinned systemd epoch")


def _assert_certificate_snapshot(
    certificate: BrokerStartupCertification,
    watch: SystemdEpochWatchHandle,
    snapshot: SystemdEpochSnapshot,
) -> None:
    if (
        snapshot.watch_id != watch.watch_id
        or certificate.system_bus_id != snapshot.system_bus_id
        or certificate.systemd_unique_owner != snapshot.systemd_unique_owner
        or certificate.boot_id != snapshot.boot_id
        or certificate.pid1_start_token != snapshot.pid1_start_token
        or certificate.pid1_executable_sha256 != snapshot.pid1_executable_sha256
        or certificate.cgroup_identity.mount_device != snapshot.cgroup_mount_device
        or certificate.cgroup_identity.mount_inode != snapshot.cgroup_mount_inode
        or certificate.units_load_timestamp_monotonic != snapshot.units_load_timestamp_monotonic
    ):
        raise StartupCertificationRevoked("certificate does not match the supplied systemd epoch")


def _assert_prerequisite_report(
    report: object,
    snapshot: SystemdEpochSnapshot,
) -> None:
    if not isinstance(report, SystemdPrerequisiteReport):
        raise StartupCertificationError("systemd prerequisite report is malformed")
    if (
        report.system_bus_id != snapshot.system_bus_id
        or report.systemd_unique_owner != snapshot.systemd_unique_owner
    ):
        raise StartupCertificationError("systemd prerequisite report is outside the pinned epoch")
    states = (
        report.transient_scopes,
        report.initial_pids,
        report.invocation_id,
        report.control_group,
        report.group_stop,
        report.cgroup_v2_events,
    )
    if SystemdCapabilityState.UNSUPPORTED in states:
        raise StartupCertificationError("systemd prerequisite is unsupported")
    if any(state is not SystemdCapabilityState.UNPROVEN for state in states):
        raise StartupCertificationError("systemd prerequisite remains ambiguous")


def _validate_probe_pair(
    handles: object,
    bindings: StartupCertificationBindings,
) -> tuple[StartupProbeHandle, StartupProbeHandle]:
    if (
        type(handles) is not tuple
        or len(handles) != 2
        or any(not isinstance(handle, StartupProbeHandle) for handle in handles)
    ):
        raise StartupCertificationError("launcher did not return two exact probe handles")
    first, second = handles
    identities = (first.identity, second.identity)
    if (
        first.pidfd == second.pidfd
        or first.identity.process_id == second.identity.process_id
        or first.identity.process_start_token == second.identity.process_start_token
        or first.identity.uid != second.identity.uid
        or first.identity.gid != second.identity.gid
        or any(
            identity.executable_sha256 != bindings.startup_probe_sha256 for identity in identities
        )
    ):
        raise StartupCertificationError("startup probes are not two distinct fixed identities")
    return first, second


def _validate_verified_scope(
    scope: object,
    request: ScopeStartRequest,
    bindings: StartupCertificationBindings,
) -> tuple[UnitIdentity, CgroupDirectoryIdentityV1]:
    identity = getattr(scope, "identity", None)
    cgroup = getattr(scope, "cgroup", None)
    cgroup_identity = getattr(cgroup, "identity", None)
    if not isinstance(identity, UnitIdentity) or not isinstance(
        cgroup_identity,
        CgroupDirectoryIdentityV1,
    ):
        raise StartupCertificationError("verified certification scope is malformed")
    expected_policy = (
        request.unit_name,
        request.description,
        SCOPE_SLICE,
        "control-group",
        request.timeout_stop_usec,
        True,
        "inactive-or-failed",
        request.runtime_max_usec,
        False,
        "",
        "active",
        "running",
    )
    observed_policy = (
        identity.unit_name,
        identity.description,
        identity.slice,
        identity.kill_mode,
        identity.timeout_stop_usec,
        identity.send_sigkill,
        identity.collect_mode,
        identity.runtime_max_usec,
        identity.delegate,
        identity.controller,
        identity.active_state,
        identity.sub_state,
    )
    if (
        observed_policy != expected_policy
        or _INVOCATION_ID.fullmatch(identity.invocation_id) is None
        or type(identity.pids) is not tuple
        or len(identity.pids) != 2
        or any(type(pid) is not int for pid in identity.pids)
        or set(identity.pids) != {request.gate_pid, request.anchor_pid}
    ):
        raise StartupCertificationError("certification scope identity or policy differs")
    if (
        cgroup_identity.mount_device != bindings.cgroup_mount_device
        or cgroup_identity.mount_inode != bindings.cgroup_mount_inode
        or tuple(item.basename for item in cgroup_identity.components)
        != tuple(identity.control_group.removeprefix("/").split("/"))
    ):
        raise StartupCertificationError(
            "certification cgroup identity is outside the bound mount or path"
        )
    return identity, cgroup_identity


def _start_job_evidence(
    result: ScopeStartResult | None,
    request: ScopeStartRequest,
) -> SystemdCompletedJobEvidence:
    if not isinstance(result, ScopeStartResult) or result.unit_name != request.unit_name:
        raise StartupCertificationError("certification start result is unavailable")
    try:
        return SystemdCompletedJobEvidence(
            job_path=result.job_path,
            result=result.job_result,
        )
    except ValueError as error:
        raise StartupCertificationError(
            "certification start job did not complete exactly"
        ) from error


def _stop_job_evidence(
    result: object,
    identity: SystemdStopJobIdentity,
) -> SystemdCompletedJobEvidence:
    if not isinstance(result, SystemdStopJobEvidenceV1) or result.job_path != identity.job_path:
        raise StartupCertificationError("certification stop job evidence is unavailable")
    try:
        return SystemdCompletedJobEvidence(
            job_path=result.job_path,
            result=result.result,
        )
    except ValueError as error:
        raise StartupCertificationError(
            "certification stop job did not complete exactly"
        ) from error


def _build_certificate(
    *,
    bindings: StartupCertificationBindings,
    cgroup_identity: CgroupDirectoryIdentityV1,
    handles: tuple[StartupProbeHandle, StartupProbeHandle],
    identity: UnitIdentity,
    start_job: SystemdCompletedJobEvidence,
    stop_job: SystemdCompletedJobEvidence,
    drain_proof: CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1,
    fixed_scope_properties_sha256: str,
) -> BrokerStartupCertification:
    values: dict[str, object] = {
        **{item.name: getattr(bindings, item.name) for item in fields(bindings)},
        "cgroup_identity": cgroup_identity,
        "probe_identities": tuple(handle.identity for handle in handles),
        "unit_name": identity.unit_name,
        "invocation_id": identity.invocation_id,
        "control_group": identity.control_group,
        "fixed_scope_properties_sha256": fixed_scope_properties_sha256,
        "initial_populated": 1,
        "start_job": start_job,
        "stop_job": stop_job,
        "probes_reaped": True,
        "drain_proof": drain_proof,
        "certified_claims": CERTIFIED_CLAIMS,
        "certification_sha256": "0" * 64,
    }
    # The certificate carries the cgroup mount through cgroup_identity, not as
    # duplicate top-level fields.
    values.pop("cgroup_mount_device")
    values.pop("cgroup_mount_inode")
    provisional = object.__new__(BrokerStartupCertification)
    for item in fields(BrokerStartupCertification):
        object.__setattr__(provisional, item.name, values[item.name])
    values["certification_sha256"] = sha256_hex(dumps_canonical(provisional._material_mapping()))
    return BrokerStartupCertification(**values)


def _assert_probe_current(
    handle: StartupProbeHandle,
    bindings: StartupCertificationBindings,
    control_group: str,
) -> None:
    identity = handle.identity
    try:
        _assert_pidfd_live(handle.pidfd)
        stat_content = _read_bounded_path(
            f"/proc/{identity.process_id}/stat",
            _MAXIMUM_PROC_BYTES,
        ).decode("ascii", errors="strict")
        close = stat_content.rfind(") ")
        fields_after = stat_content[close + 2 :].split()
        if close < 1 or len(fields_after) <= 19:
            raise StartupCertificationError("probe process stat is malformed")
        process_token = f"linux-proc-stat/v1:{identity.process_id}:{fields_after[19]}"
        if (
            fields_after[0] not in ("T", "t")
            or int(fields_after[1], 10) != bindings.broker_process_id
            or process_token != identity.process_start_token
        ):
            raise StartupCertificationError("probe is not the exact stopped broker child")
        status = _parse_status(identity.process_id)
        expected_credentials = f"{identity.uid}\t{identity.uid}\t{identity.uid}\t{identity.uid}"
        expected_groups = ""
        if status.get("Uid") != expected_credentials:
            raise StartupCertificationError("probe UID state changed")
        expected_gid = f"{identity.gid}\t{identity.gid}\t{identity.gid}\t{identity.gid}"
        if status.get("Gid") != expected_gid or status.get("Groups", "") != expected_groups:
            raise StartupCertificationError("probe GID/group state changed")
        if status.get("NoNewPrivs") != "1" or any(
            status.get(name) != "0000000000000000"
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ):
            raise StartupCertificationError("probe hardening state changed")
        cgroup = _read_bounded_path(
            f"/proc/{identity.process_id}/cgroup",
            _MAXIMUM_PROC_BYTES,
        ).decode("ascii", errors="strict")
        if cgroup.splitlines() != [f"0::{control_group}"]:
            raise StartupCertificationError("probe cgroup membership changed")
        _PRODUCTION_STARTUP_PROBE_LAUNCH_RUNTIME.validate_probe_asset(bindings)
        expected_cmdline = (
            b"/usr/bin/python3\0-I\0" + os.fsencode(bindings.startup_probe_path) + b"\0"
        )
        if (
            _read_bounded_path(
                f"/proc/{identity.process_id}/cmdline",
                _MAXIMUM_PROC_BYTES,
            )
            != expected_cmdline
        ):
            raise StartupCertificationError("probe executable changed")
        if _open_fds(identity.process_id) != frozenset((0, 1, 2)):
            raise StartupCertificationError("probe retained a foreign descriptor")
        for descriptor in (0, 1, 2):
            target = os.readlink(f"/proc/{identity.process_id}/fd/{descriptor}")
            if target != "/dev/null":
                raise StartupCertificationError("probe standard descriptor is not /dev/null")
    except StartupCertificationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise StartupCertificationError("cannot revalidate stopped startup probe") from error


def _assert_pidfd_live(pidfd: int) -> None:
    signal.pidfd_send_signal(pidfd, 0, None, 0)


def _parse_status(pid: int) -> dict[str, str]:
    text = _read_bounded_path(
        f"/proc/{pid}/status",
        _MAXIMUM_PROC_BYTES,
    ).decode("ascii", errors="strict")
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            result[name] = value.strip()
    return result


def _open_fds(pid: int) -> frozenset[int]:
    values: set[int] = set()
    for name in os.listdir(f"/proc/{pid}/fd"):
        if not name.isascii() or not name.isdigit():
            raise StartupCertificationError("probe descriptor inventory is malformed")
        values.add(int(name, 10))
    return frozenset(values)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(
            descriptor,
            min(1_048_576, _MAXIMUM_EXECUTABLE_BYTES - total + 1),
        )
        if not chunk:
            return digest.hexdigest()
        total += len(chunk)
        if total > _MAXIMUM_EXECUTABLE_BYTES:
            raise StartupCertificationError("probe executable exceeds its bound")
        digest.update(chunk)


def _process_stat_identity(pid: int) -> tuple[str, int, str]:
    content = _read_bounded_path(
        f"/proc/{pid}/stat",
        _MAXIMUM_PROC_BYTES,
    ).decode("ascii", errors="strict")
    close = content.rfind(") ")
    if close < 1 or content[:close].split("(", 1)[0].strip() != str(pid):
        raise StartupCertificationError("probe process stat is malformed")
    fields_after = content[close + 2 :].split()
    if (
        len(fields_after) <= 19
        or not fields_after[1].isascii()
        or not fields_after[1].isdigit()
        or not fields_after[19].isascii()
        or not fields_after[19].isdigit()
    ):
        raise StartupCertificationError("probe process identity is missing")
    start = int(fields_after[19], 10)
    if start < 1:
        raise StartupCertificationError("probe process start identity is invalid")
    return f"linux-proc-stat/v1:{pid}:{start}", int(fields_after[1], 10), fields_after[0]


def _read_bounded_path(path: str, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
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
        raise StartupCertificationError("process evidence exceeds its byte bound")
    return bytes(content)


def _seconds_to_usec(seconds: float) -> int:
    value = int(float(seconds) * 1_000_000)
    if value < 1 or value > (1 << 64) - 2:
        raise StartupCertificationError("certification duration is outside systemd range")
    return value


def _require_integer(value: object, name: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _require_process_token(value: object, pid: int, name: str) -> None:
    if type(value) is not str or (match := _START_TOKEN.fullmatch(value)) is None:
        raise ValueError(f"{name} is malformed")
    if int(match.group("pid"), 10) != pid:
        raise ValueError(f"{name} names another process")


def _require_sha256(value: object, name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")


def _require_text(value: object, name: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be nonempty bounded text")


def _require_canonical_absolute_path(value: object) -> None:
    if (
        type(value) is not str
        or not value.startswith("/")
        or value == "/"
        or posixpath.normpath(value) != value
        or "\x00" in value
    ):
        raise ValueError("startup_probe_path must be a canonical absolute path")
