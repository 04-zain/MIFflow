"""Fixed systemd scope adapter and stop-linked cgroup-v2 drain proofs.

The `UnitIdentity` record carries `unit_name`, `invocation_id`, `control_group`,
`description`, `slice` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import secrets
import signal
import stat
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from .protocol import (
    CgroupDeletedAfterStopProofV1,
    CgroupDirectoryIdentityV1,
    CgroupEventsZeroProofV1,
    CgroupPathComponentIdentityV1,
    SystemdStopJobEvidenceV1,
)

SCOPE_SLICE = "mif-pipeline-qupath.slice"
MAXIMUM_SYSTEMD_USEC = (1 << 64) - 2
MAXIMUM_CGROUP_EVENTS_BYTES = 4_096
MAXIMUM_CGROUP_PROCS_BYTES = 65_536
MAXIMUM_CGROUP_DIRECTORY_ENTRIES = 256
MAXIMUM_PROC_CONTROL_GROUP_BYTES = 65_536
MAXIMUM_MOUNTINFO_BYTES = 1_048_576
MAXIMUM_PROC_STAT_BYTES = 65_536
MAXIMUM_PID1_EXECUTABLE_BYTES = 268_435_456
MAXIMUM_EPOCH_EVENTS = 256
MAXIMUM_COMPLETED_JOB_PATHS = 65_536

_UNIT_NAME = re.compile(r"mif-qupath-[A-Za-z0-9_.:@-]+\.scope\Z")
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_SYSTEMD_JOB_PATH = re.compile(r"/org/freedesktop/systemd1/job/[1-9][0-9]*\Z")
_SYSTEMD_UNIT_PATH = re.compile(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+\Z")
_DBUS_OBJECT_PATH = re.compile(r"/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?\Z")
_UNIQUE_BUS_NAME = re.compile(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z")
_SYSTEM_BUS_ID = re.compile(r"[0-9a-f]{32}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PID1_START_TOKEN = re.compile(r"linux-proc-stat/v1:1:[1-9][0-9]*\Z")
_WATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_EVENT_KEY = re.compile(r"[A-Za-z0-9_]+\Z")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


# ==============================================================================
#                                Systemd adapter
# ==============================================================================


class SystemdVerificationError(RuntimeError):
    """Systemd evidence is missing, malformed, or not the expected identity."""


class SystemdUnitNotFound(SystemdVerificationError):
    """The exact systemd GetUnit call reported that the unit does not exist."""


class CgroupVerificationError(SystemdVerificationError):
    """Kernel cgroup evidence is missing, malformed, replaced, or ambiguous."""


@dataclass(frozen=True, slots=True)
class ScopeStartRequest:
    """The only caller-representable inputs to fixed scope creation."""

    unit_name: str
    description: str
    gate_pid: int
    anchor_pid: int
    timeout_stop_usec: int
    runtime_max_usec: int

    def __post_init__(self) -> None:
        _require_unit_name(self.unit_name, error_type=ValueError)
        if (
            type(self.description) is not str
            or not self.description
            or "\x00" in self.description
            or len(self.description.encode("utf-8")) > 1_024
        ):
            raise ValueError("description must be nonempty bounded UTF-8 text")
        _require_integer(self.gate_pid, "gate_pid", 1, (1 << 32) - 1)
        _require_integer(self.anchor_pid, "anchor_pid", 1, (1 << 32) - 1)
        if self.gate_pid == self.anchor_pid:
            raise ValueError("gate_pid and anchor_pid must be distinct")
        _require_integer(
            self.timeout_stop_usec,
            "timeout_stop_usec",
            1,
            MAXIMUM_SYSTEMD_USEC,
        )
        _require_integer(
            self.runtime_max_usec,
            "runtime_max_usec",
            1,
            MAXIMUM_SYSTEMD_USEC,
        )


@dataclass(frozen=True, slots=True)
class ScopeStartResult:
    unit_name: str
    job_path: str
    job_result: str


class SystemdCapabilityState(str, Enum):
    UNSUPPORTED = "unsupported"
    UNPROVEN = "unproven"
    CERTIFIED = "certified"


@dataclass(frozen=True, slots=True)
class SystemdPrerequisiteReport:
    system_bus_id: str | None
    systemd_unique_owner: str | None
    transient_scopes: SystemdCapabilityState
    initial_pids: SystemdCapabilityState
    invocation_id: SystemdCapabilityState
    control_group: SystemdCapabilityState
    group_stop: SystemdCapabilityState
    cgroup_v2_events: SystemdCapabilityState

    def __post_init__(self) -> None:
        states = (
            self.transient_scopes,
            self.initial_pids,
            self.invocation_id,
            self.control_group,
            self.group_stop,
            self.cgroup_v2_events,
        )
        if any(type(value) is not SystemdCapabilityState for value in states):
            raise ValueError("capability states must use SystemdCapabilityState")
        if SystemdCapabilityState.CERTIFIED in states:
            raise ValueError("a non-mutating prerequisite report cannot certify claims")
        identities_absent = self.system_bus_id is None and self.systemd_unique_owner is None
        identities_exact = (
            type(self.system_bus_id) is str
            and _SYSTEM_BUS_ID.fullmatch(self.system_bus_id) is not None
            and type(self.systemd_unique_owner) is str
            and _UNIQUE_BUS_NAME.fullmatch(self.systemd_unique_owner) is not None
        )
        if not identities_absent and not identities_exact:
            raise ValueError("prerequisite report bus identities must be paired")


@dataclass(frozen=True, slots=True)
class SystemdEpochWatchHandle:
    watch_id: str

    def __post_init__(self) -> None:
        if type(self.watch_id) is not str or _WATCH_ID.fullmatch(self.watch_id) is None:
            raise ValueError("epoch watch ID is malformed")


@dataclass(frozen=True, slots=True)
class SystemdEpochSnapshot:
    watch_id: str
    system_bus_id: str
    systemd_unique_owner: str
    reloading: Literal[False]
    boot_id: str
    pid1_start_token: str
    pid1_executable_sha256: str
    cgroup_mount_device: int
    cgroup_mount_inode: int
    units_load_timestamp_monotonic: int

    def __post_init__(self) -> None:
        """Validate the SystemdEpochSnapshot fields when the record is constructed.

        The checks cover ``watch_id``, ``reloading``, ``cgroup_mount_device``,
        ``cgroup_mount_inode``, ``units_load_timestamp_monotonic``, ``system_bus_id``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        SystemdEpochWatchHandle(self.watch_id)
        if (
            type(self.system_bus_id) is not str
            or _SYSTEM_BUS_ID.fullmatch(self.system_bus_id) is None
        ):
            raise ValueError("system bus ID is malformed")
        if (
            type(self.systemd_unique_owner) is not str
            or _UNIQUE_BUS_NAME.fullmatch(self.systemd_unique_owner) is None
        ):
            raise ValueError("systemd unique owner is malformed")
        if self.reloading is not False:
            raise ValueError("epoch snapshot requires Reloading=false")
        if type(self.boot_id) is not str or _BOOT_ID.fullmatch(self.boot_id) is None:
            raise ValueError("boot ID is malformed")
        if (
            type(self.pid1_start_token) is not str
            or _PID1_START_TOKEN.fullmatch(self.pid1_start_token) is None
        ):
            raise ValueError("PID 1 start identity is malformed")
        if (
            type(self.pid1_executable_sha256) is not str
            or _SHA256.fullmatch(self.pid1_executable_sha256) is None
        ):
            raise ValueError("PID 1 executable digest is malformed")
        _require_integer(self.cgroup_mount_device, "cgroup_mount_device", 1, (1 << 64) - 1)
        _require_integer(self.cgroup_mount_inode, "cgroup_mount_inode", 1, (1 << 64) - 1)
        _require_integer(
            self.units_load_timestamp_monotonic,
            "units_load_timestamp_monotonic",
            0,
            (1 << 64) - 1,
        )


@dataclass(frozen=True, slots=True)
class UnitIdentity:
    unit_name: str
    invocation_id: str
    control_group: str
    description: str
    slice: str
    pids: tuple[int, ...]
    kill_mode: str
    timeout_stop_usec: int
    send_sigkill: bool
    collect_mode: str
    runtime_max_usec: int
    delegate: bool
    controller: str
    active_state: str
    sub_state: str


@dataclass(frozen=True, slots=True)
class SystemdStopJobIdentity:
    unit_name: str
    invocation_id: str
    job_path: str

    def __post_init__(self) -> None:
        _require_unit_name(self.unit_name)
        if _INVOCATION_ID.fullmatch(self.invocation_id) is None:
            raise SystemdVerificationError("InvocationID is missing or malformed")
        if _SYSTEMD_JOB_PATH.fullmatch(self.job_path) is None:
            raise SystemdVerificationError("stop job path is missing or malformed")


@dataclass(frozen=True, slots=True)
class _JobRemoval:
    job_path: str
    unit_name: str
    result: str


@dataclass(slots=True)
class VerifiedCgroup:
    """Exact cgroup identity with live descriptors for the full bound chain."""

    mount_point: str
    control_group: str
    identity: CgroupDirectoryIdentityV1
    mount_descriptor: int
    component_descriptors: tuple[int, ...]
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def descriptor(self) -> int:
        if self._closed or not self.component_descriptors:
            raise CgroupVerificationError("verified cgroup descriptor is closed")
        return self.component_descriptors[-1]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[OSError] = []
        for descriptor in (
            *reversed(self.component_descriptors),
            self.mount_descriptor,
        ):
            try:
                os.close(descriptor)
            except OSError as error:
                # close(2) may already have released the descriptor number;
                # never retry it, but do attempt every independent owner.
                failures.append(error)
        if failures:
            raise failures[0]


@dataclass(frozen=True, slots=True)
class VerifiedScope:
    request: ScopeStartRequest
    identity: UnitIdentity
    cgroup: VerifiedCgroup


@runtime_checkable
class SystemdManager(Protocol):
    async def probe_capabilities(self) -> SystemdPrerequisiteReport: ...

    async def install_epoch_watch(self) -> SystemdEpochWatchHandle: ...

    async def read_epoch_snapshot(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot: ...

    async def assert_epoch_current(
        self, watch: SystemdEpochWatchHandle, snapshot: SystemdEpochSnapshot
    ) -> None: ...

    async def start_scope(self, request: ScopeStartRequest) -> ScopeStartResult: ...

    async def resolve_unit_by_pid(self, pid: int) -> str: ...

    async def read_unit_identity(self, unit_name: str) -> UnitIdentity: ...

    async def begin_stop_unit(
        self, unit_name: str, invocation_id: str
    ) -> SystemdStopJobIdentity: ...

    async def await_stop_job(
        self, job: SystemdStopJobIdentity
    ) -> SystemdStopJobEvidenceV1 | None: ...

    async def kill_unit_all(self, unit_name: str, invocation_id: str) -> None: ...

    async def ref_unit(self, unit_name: str) -> None: ...

    async def unref_unit(self, unit_name: str) -> None: ...


class _SystemdTransport(Protocol):
    async def probe_capabilities(self) -> SystemdPrerequisiteReport: ...

    async def install_epoch_watch(self) -> SystemdEpochWatchHandle: ...

    async def read_epoch_snapshot(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot: ...

    async def assert_epoch_current(
        self, watch: SystemdEpochWatchHandle, snapshot: SystemdEpochSnapshot
    ) -> None: ...

    async def start_transient_unit(
        self,
        unit_name: str,
        mode: str,
        properties: tuple[tuple[str, object], ...],
        auxiliaries: tuple[object, ...],
    ) -> ScopeStartResult: ...

    async def resolve_unit_by_pid(self, pid: int) -> str: ...

    async def read_unit_identity(self, unit_name: str) -> UnitIdentity: ...

    async def begin_stop_unit(self, unit_name: str, mode: str) -> str: ...

    async def await_stop_job(self, job_path: str, unit_name: str) -> tuple[str, str] | None: ...

    async def kill_unit_all(self, unit_name: str) -> None: ...

    async def ref_unit(self, unit_name: str) -> None: ...

    async def unref_unit(self, unit_name: str) -> None: ...


class DbusFastSystemdManager:
    """Fixed-policy manager backed by dbus-fast or an injected test transport."""

    def __init__(
        self,
        *,
        transport: _SystemdTransport | None = None,
        variant_factory: Callable[[str, object], object] | None = None,
    ) -> None:
        if transport is None:
            from dbus_fast import Variant

            transport = _DbusFastTransport()
            variant_factory = Variant
        elif variant_factory is None:
            raise TypeError("an injected transport requires an injected variant_factory")
        self._transport = transport
        self._variant = variant_factory
        self._owned_stop_jobs: set[SystemdStopJobIdentity] = set()
        self._seen_stop_job_paths: set[str] = set()
        self._epoch_watch: SystemdEpochWatchHandle | None = None
        self._epoch_snapshot: SystemdEpochSnapshot | None = None

    def _bind_epoch_revocation_callback(self, callback: Callable[[str], None]) -> None:
        binder = getattr(self._transport, "_bind_epoch_revocation_callback", None)
        if not callable(binder):
            raise SystemdVerificationError("concrete transport cannot publish epoch revocation")
        binder(callback)

    async def probe_capabilities(self) -> SystemdPrerequisiteReport:
        if self._epoch_watch is None or self._epoch_snapshot is None:
            raise SystemdVerificationError(
                "capability probing requires an installed epoch snapshot"
            )
        await self.assert_epoch_current(self._epoch_watch, self._epoch_snapshot)
        report = await self._transport.probe_capabilities()
        if not isinstance(report, SystemdPrerequisiteReport):
            raise SystemdVerificationError("prerequisite report is malformed")
        if (
            report.system_bus_id != self._epoch_snapshot.system_bus_id
            or report.systemd_unique_owner != self._epoch_snapshot.systemd_unique_owner
        ):
            raise SystemdVerificationError(
                "prerequisite report is outside the watched systemd epoch"
            )
        return report

    async def install_epoch_watch(self) -> SystemdEpochWatchHandle:
        if self._epoch_watch is not None:
            raise SystemdVerificationError("systemd epoch watch cannot be replaced")
        watch = await self._transport.install_epoch_watch()
        if not isinstance(watch, SystemdEpochWatchHandle):
            raise SystemdVerificationError("systemd epoch watch is malformed")
        self._epoch_watch = watch
        return watch

    async def read_epoch_snapshot(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot:
        if watch != self._epoch_watch or self._epoch_snapshot is not None:
            raise SystemdVerificationError("systemd epoch snapshot cannot be repinned")
        snapshot = await self._transport.read_epoch_snapshot(watch)
        if not isinstance(snapshot, SystemdEpochSnapshot) or snapshot.watch_id != watch.watch_id:
            raise SystemdVerificationError("systemd epoch snapshot is malformed")
        self._epoch_snapshot = snapshot
        return snapshot

    async def assert_epoch_current(
        self, watch: SystemdEpochWatchHandle, snapshot: SystemdEpochSnapshot
    ) -> None:
        if watch != self._epoch_watch or snapshot != self._epoch_snapshot:
            raise SystemdVerificationError("systemd epoch identity is not the installed snapshot")
        await self._transport.assert_epoch_current(watch, snapshot)

    async def start_scope(self, request: ScopeStartRequest) -> ScopeStartResult:
        properties = (
            ("Description", self._variant("s", request.description)),
            ("Slice", self._variant("s", SCOPE_SLICE)),
            ("PIDs", self._variant("au", [request.gate_pid, request.anchor_pid])),
            ("KillMode", self._variant("s", "control-group")),
            ("TimeoutStopUSec", self._variant("t", request.timeout_stop_usec)),
            ("SendSIGKILL", self._variant("b", True)),
            ("CollectMode", self._variant("s", "inactive-or-failed")),
            ("RuntimeMaxUSec", self._variant("t", request.runtime_max_usec)),
            ("Delegate", self._variant("b", False)),
        )
        return await self._transport.start_transient_unit(
            request.unit_name,
            "fail",
            properties,
            (),
        )

    async def resolve_unit_by_pid(self, pid: int) -> str:
        _require_integer(pid, "pid", 1, (1 << 32) - 1)
        return await self._transport.resolve_unit_by_pid(pid)

    async def read_unit_identity(self, unit_name: str) -> UnitIdentity:
        _require_unit_name(unit_name)
        return await self._transport.read_unit_identity(unit_name)

    async def begin_stop_unit(self, unit_name: str, invocation_id: str) -> SystemdStopJobIdentity:
        await self._require_current_invocation(unit_name, invocation_id)
        path = await self._transport.begin_stop_unit(unit_name, "replace")
        job = SystemdStopJobIdentity(unit_name, invocation_id, path)
        if path in self._seen_stop_job_paths:
            raise SystemdVerificationError("systemd reused a stop job path")
        self._seen_stop_job_paths.add(path)
        self._owned_stop_jobs.add(job)
        return job

    async def await_stop_job(self, job: SystemdStopJobIdentity) -> SystemdStopJobEvidenceV1 | None:
        if not isinstance(job, SystemdStopJobIdentity):
            raise SystemdVerificationError("stop job identity is required")
        if job not in self._owned_stop_jobs:
            return None
        observation = await self._transport.await_stop_job(
            job.job_path,
            job.unit_name,
        )
        if observation is None:
            return None
        self._owned_stop_jobs.remove(job)
        observed_path, result = observation
        if observed_path != job.job_path:
            raise SystemdVerificationError("JobRemoved path does not match stop job")
        try:
            return SystemdStopJobEvidenceV1(job_path=observed_path, result=result)
        except ValueError as error:
            raise SystemdVerificationError("JobRemoved result is malformed") from error

    async def kill_unit_all(self, unit_name: str, invocation_id: str) -> None:
        await self._require_current_invocation(unit_name, invocation_id)
        await self._transport.kill_unit_all(unit_name)

    async def ref_unit(self, unit_name: str) -> None:
        _require_unit_name(unit_name)
        await self._transport.ref_unit(unit_name)

    async def unref_unit(self, unit_name: str) -> None:
        _require_unit_name(unit_name)
        await self._transport.unref_unit(unit_name)

    async def _require_current_invocation(self, unit_name: str, invocation_id: str) -> None:
        _require_unit_name(unit_name)
        if _INVOCATION_ID.fullmatch(invocation_id) is None:
            raise SystemdVerificationError("InvocationID is missing or malformed")
        identity = await self._transport.read_unit_identity(unit_name)
        if identity.unit_name != unit_name or identity.invocation_id != invocation_id:
            raise SystemdVerificationError("unit incarnation changed before operation")


async def verify_scope_attachment(
    manager: SystemdManager,
    request: ScopeStartRequest,
    *,
    previous_invocation_id: str | None,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
    proc_root: str | os.PathLike[str] = "/proc",
) -> VerifiedScope:
    """Start a scope and bind both stopped processes to exact unit/cgroup identity."""

    started = await manager.start_scope(request)
    if (
        started.unit_name != request.unit_name
        or started.job_result != "done"
        or _SYSTEMD_JOB_PATH.fullmatch(started.job_path) is None
    ):
        raise SystemdVerificationError("transient-scope start job did not complete")
    for pid in (request.gate_pid, request.anchor_pid):
        if await manager.resolve_unit_by_pid(pid) != request.unit_name:
            raise SystemdVerificationError(
                "GetUnitByPID does not resolve both requested scope members"
            )
    identity = await manager.read_unit_identity(request.unit_name)
    _verify_identity_matches_request(identity, request)
    if previous_invocation_id is not None and identity.invocation_id == previous_invocation_id:
        raise SystemdVerificationError("InvocationID was reused")
    cgroup = open_verified_cgroup(
        identity.control_group,
        mountinfo_path=mountinfo_path,
    )
    try:
        for pid in (request.gate_pid, request.anchor_pid):
            _verify_pid_membership(
                pid,
                identity.control_group,
                proc_root=proc_root,
            )
        _verify_exact_leaf_membership(
            cgroup,
            (request.gate_pid, request.anchor_pid),
        )
        _verify_no_child_cgroup(cgroup)
    except BaseException as primary:
        try:
            cgroup.close()
        except BaseException as cleanup_error:
            primary.add_note(f"cleanup failure: {type(cleanup_error).__name__}")
        raise
    return VerifiedScope(request=request, identity=identity, cgroup=cgroup)


def open_verified_cgroup(
    control_group: str,
    *,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> VerifiedCgroup:
    """Open and retain the mount plus every exact ControlGroup component."""

    basenames = _control_group_components(control_group)
    mount_point = _resolve_unified_cgroup2_mount(mountinfo_path)
    mount_descriptor = -1
    component_descriptors: list[int] = []
    try:
        mount_descriptor = os.open(mount_point, _DIRECTORY_FLAGS)
        mount_status = os.fstat(mount_descriptor)
        if not stat.S_ISDIR(mount_status.st_mode):
            raise CgroupVerificationError("cgroup2 mount is not a directory")
        parent_descriptor = mount_descriptor
        components: list[CgroupPathComponentIdentityV1] = []
        for basename in basenames:
            descriptor = os.open(
                basename,
                _DIRECTORY_FLAGS,
                dir_fd=parent_descriptor,
            )
            component_descriptors.append(descriptor)
            status = os.fstat(descriptor)
            if not stat.S_ISDIR(status.st_mode):
                raise CgroupVerificationError("ControlGroup component is not a directory")
            components.append(
                CgroupPathComponentIdentityV1(
                    basename=basename,
                    directory_device=status.st_dev,
                    directory_inode=status.st_ino,
                )
            )
            parent_descriptor = descriptor
        identity = CgroupDirectoryIdentityV1(
            mount_device=mount_status.st_dev,
            mount_inode=mount_status.st_ino,
            components=tuple(components),
        )
        return VerifiedCgroup(
            mount_point=mount_point,
            control_group=control_group,
            identity=identity,
            mount_descriptor=mount_descriptor,
            component_descriptors=tuple(component_descriptors),
        )
    except CgroupVerificationError as primary:
        _annotate_close_failures(
            primary,
            _close_descriptors(component_descriptors, mount_descriptor),
        )
        raise
    except ValueError as error:
        primary = CgroupVerificationError("ControlGroup contains an unsafe component")
        _annotate_close_failures(
            primary,
            _close_descriptors(component_descriptors, mount_descriptor),
        )
        raise primary from error
    except OSError as error:
        primary = CgroupVerificationError("cannot resolve ControlGroup securely")
        _annotate_close_failures(
            primary,
            _close_descriptors(component_descriptors, mount_descriptor),
        )
        raise primary from error


def prove_cgroup_events_zero(
    cgroup: VerifiedCgroup,
) -> CgroupEventsZeroProofV1 | None:
    """Produce a typed proof only from an actual zero on the retained leaf."""

    if not isinstance(cgroup, VerifiedCgroup):
        raise CgroupVerificationError("a retained verified cgroup is required")
    populated = read_cgroup_events_populated(cgroup)
    if populated != 0:
        return None
    return CgroupEventsZeroProofV1(
        proof_kind="cgroup-events-zero",
        cgroup_identity=cgroup.identity,
        populated=0,
    )


def read_cgroup_events_populated(cgroup: VerifiedCgroup) -> Literal[0, 1]:
    """Read the exact literal populated state through the retained leaf chain."""

    if not isinstance(cgroup, VerifiedCgroup):
        raise CgroupVerificationError("a retained verified cgroup is required")
    _verify_retained_chain(cgroup)
    populated = _read_cgroup_events_populated(cgroup.descriptor)
    if type(populated) is not int or populated not in (0, 1):
        raise CgroupVerificationError(
            "cgroup.events populated must be the literal integer zero or one"
        )
    return populated


def prove_cgroup_deleted_after_stop(
    cgroup_identity: CgroupDirectoryIdentityV1,
    *,
    durable_stop_intent_cgroup_identity: CgroupDirectoryIdentityV1 | None,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> CgroupDeletedAfterStopProofV1 | None:
    """Reopen a persisted chain and prove its first absence after stop intent."""

    if (
        not isinstance(cgroup_identity, CgroupDirectoryIdentityV1)
        or durable_stop_intent_cgroup_identity != cgroup_identity
    ):
        raise CgroupVerificationError("exact durable stop-intent cgroup identity is required")
    mount_point = _resolve_unified_cgroup2_mount(mountinfo_path)
    descriptor = -1
    try:
        descriptor = os.open(mount_point, _DIRECTORY_FLAGS)
        mount_status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(mount_status.st_mode)
            or mount_status.st_dev != cgroup_identity.mount_device
            or mount_status.st_ino != cgroup_identity.mount_inode
        ):
            raise CgroupVerificationError("cgroup2 mount identity was replaced")
        for index, component in enumerate(cgroup_identity.components):
            try:
                child = os.open(
                    component.basename,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return CgroupDeletedAfterStopProofV1(
                    proof_kind="cgroup-deleted-after-stop",
                    cgroup_identity=cgroup_identity,
                    matched_prefix_length=index,
                    first_absent_component_index=index,
                    first_absent_component_basename=component.basename,
                    replacement_rejected=True,
                )
            status = os.fstat(child)
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_dev != component.directory_device
                or status.st_ino != component.directory_inode
            ):
                os.close(child)
                raise CgroupVerificationError("cgroup path component was replaced")
            os.close(descriptor)
            descriptor = child
        return None
    except CgroupVerificationError:
        raise
    except OSError as error:
        raise CgroupVerificationError("cannot reverify cgroup deletion chain") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_identity_matches_request(
    identity: UnitIdentity,
    request: ScopeStartRequest,
) -> None:
    if _INVOCATION_ID.fullmatch(identity.invocation_id) is None:
        raise SystemdVerificationError("InvocationID is missing or malformed")
    if identity.delegate is not False or type(identity.controller) is not str:
        raise SystemdVerificationError("scope delegation/controller state is malformed")
    expected_fixed = (
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
    )
    observed_fixed = (
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
    )
    if observed_fixed != expected_fixed:
        raise SystemdVerificationError("systemd unit identity or fixed policy differs")
    if (
        request.gate_pid == request.anchor_pid
        or type(identity.pids) is not tuple
        or len(identity.pids) != 2
        or any(type(pid) is not int for pid in identity.pids)
        or len(set(identity.pids)) != 2
        or set(identity.pids) != {request.gate_pid, request.anchor_pid}
    ):
        raise SystemdVerificationError("systemd membership is not the exact two-PID set")
    _control_group_components(identity.control_group)


def _verify_retained_chain(cgroup: VerifiedCgroup) -> None:
    if cgroup._closed or len(cgroup.component_descriptors) != len(cgroup.identity.components):
        raise CgroupVerificationError("complete retained cgroup chain is unavailable")
    mount_status = os.fstat(cgroup.mount_descriptor)
    if (
        mount_status.st_dev != cgroup.identity.mount_device
        or mount_status.st_ino != cgroup.identity.mount_inode
        or not stat.S_ISDIR(mount_status.st_mode)
    ):
        raise CgroupVerificationError("retained mount identity changed")
    for descriptor, component in zip(
        cgroup.component_descriptors,
        cgroup.identity.components,
        strict=True,
    ):
        status = os.fstat(descriptor)
        if (
            status.st_dev != component.directory_device
            or status.st_ino != component.directory_inode
            or not stat.S_ISDIR(status.st_mode)
        ):
            raise CgroupVerificationError("retained component identity changed")


def _read_cgroup_events_populated(directory_descriptor: int) -> int:
    descriptor = -1
    try:
        before = os.stat(
            "cgroup.events",
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAXIMUM_CGROUP_EVENTS_BYTES:
            raise CgroupVerificationError("cgroup.events is not a bounded regular file")
        descriptor = os.open(
            "cgroup.events",
            _READ_FLAGS,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > MAXIMUM_CGROUP_EVENTS_BYTES
        ):
            raise CgroupVerificationError("cgroup.events changed while opening")
        content = _read_bounded_descriptor(
            descriptor,
            MAXIMUM_CGROUP_EVENTS_BYTES,
            "cgroup.events",
        )
        after = os.stat(
            "cgroup.events",
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino:
            raise CgroupVerificationError("cgroup.events was replaced during read")
    except CgroupVerificationError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise CgroupVerificationError("cgroup.events symlink is forbidden") from error
        raise CgroupVerificationError("cannot read cgroup.events securely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _parse_populated(content)


def _verify_pid_membership(
    pid: int,
    control_group: str,
    *,
    proc_root: str | os.PathLike[str],
) -> None:
    path = Path(proc_root) / str(pid) / "cgroup"
    try:
        content = _read_bounded_path(path, MAXIMUM_PROC_CONTROL_GROUP_BYTES, "PID cgroup")
        text = content.decode("ascii")
    except (OSError, UnicodeDecodeError, CgroupVerificationError) as error:
        raise CgroupVerificationError("cannot establish PID cgroup membership") from error
    if text.splitlines() != [f"0::{control_group}"]:
        raise CgroupVerificationError("PID is not in the exact unified ControlGroup")


def _verify_exact_leaf_membership(
    cgroup: VerifiedCgroup,
    expected_pids: tuple[int, int],
) -> None:
    _verify_retained_chain(cgroup)
    content = _read_bounded_cgroup_file(
        cgroup.descriptor,
        "cgroup.procs",
        MAXIMUM_CGROUP_PROCS_BYTES,
    )
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise CgroupVerificationError("cgroup.procs is not ASCII") from error
    lines = text.splitlines()
    if not lines:
        raise CgroupVerificationError("cgroup.procs is empty")
    pids: list[int] = []
    for line in lines:
        if not line.isascii() or not line.isdecimal():
            raise CgroupVerificationError("cgroup.procs contains a malformed PID")
        pid = int(line, 10)
        if not 1 <= pid <= (1 << 32) - 1:
            raise CgroupVerificationError("cgroup.procs PID is outside range")
        pids.append(pid)
    if len(pids) != len(set(pids)) or set(pids) != set(expected_pids):
        raise CgroupVerificationError("cgroup.procs is not the exact two-PID set")


def _verify_no_child_cgroup(cgroup: VerifiedCgroup) -> None:
    _verify_retained_chain(cgroup)
    try:
        with os.scandir(cgroup.descriptor) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > MAXIMUM_CGROUP_DIRECTORY_ENTRIES:
                    raise CgroupVerificationError(
                        "scope leaf directory enumeration exceeds its bound"
                    )
                if entry.is_dir(follow_symlinks=False):
                    raise CgroupVerificationError("scope leaf contains a child cgroup")
        _verify_retained_chain(cgroup)
    except CgroupVerificationError:
        raise
    except OSError as error:
        raise CgroupVerificationError("cannot enumerate the exact scope leaf") from error


def _read_bounded_cgroup_file(
    directory_descriptor: int,
    basename: str,
    maximum: int,
) -> bytes:
    descriptor = -1
    try:
        before = os.stat(
            basename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise CgroupVerificationError(f"{basename} is not a bounded regular file")
        descriptor = os.open(basename, _READ_FLAGS, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > maximum
        ):
            raise CgroupVerificationError(f"{basename} changed while opening")
        content = _read_bounded_descriptor(descriptor, maximum, basename)
        after = os.stat(
            basename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino:
            raise CgroupVerificationError(f"{basename} was replaced during read")
        return content
    except CgroupVerificationError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise CgroupVerificationError(f"{basename} symlink is forbidden") from error
        raise CgroupVerificationError(f"cannot read {basename} securely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _resolve_unified_cgroup2_mount(
    mountinfo_path: str | os.PathLike[str],
) -> str:
    try:
        content = _read_bounded_path(mountinfo_path, MAXIMUM_MOUNTINFO_BYTES, "mountinfo")
        text = content.decode("ascii")
    except (OSError, UnicodeDecodeError, CgroupVerificationError) as error:
        raise CgroupVerificationError("cannot parse mountinfo") from error
    candidates: list[str] = []
    for line in text.splitlines():
        if line.count(" - ") != 1:
            raise CgroupVerificationError("mountinfo record is malformed")
        before, after = line.split(" - ", 1)
        left = before.split(" ")
        right = after.split(" ")
        if len(left) < 6 or len(right) < 3:
            raise CgroupVerificationError("mountinfo record is malformed")
        if right[0] != "cgroup2":
            continue
        root = _decode_mount_field(left[3])
        mount_point = _decode_mount_field(left[4])
        if root == "/" and _is_normal_absolute_path(mount_point):
            candidates.append(mount_point)
    if len(candidates) != 1:
        raise CgroupVerificationError("exactly one usable unified cgroup2 mount is required")
    return candidates[0]


def _observe_local_epoch() -> tuple[str, str, str, int, int]:
    try:
        boot_id = (
            _read_bounded_path(
                "/proc/sys/kernel/random/boot_id",
                128,
                "boot ID",
            )
            .decode("ascii")
            .strip()
        )
        stat_text = _read_bounded_path(
            "/proc/1/stat",
            MAXIMUM_PROC_STAT_BYTES,
            "PID 1 stat",
        ).decode("ascii")
        close_parenthesis = stat_text.rfind(")")
        if close_parenthesis < 3:
            raise SystemdVerificationError("PID 1 stat is malformed")
        fields = stat_text[close_parenthesis + 2 :].split()
        if len(fields) < 20 or not fields[19].isascii() or not fields[19].isdecimal():
            raise SystemdVerificationError("PID 1 start identity is malformed")
        start_value = int(fields[19], 10)
        if start_value <= 0:
            raise SystemdVerificationError("PID 1 start identity is malformed")
        executable_sha256 = _sha256_bounded_path(
            "/proc/1/exe",
            MAXIMUM_PID1_EXECUTABLE_BYTES,
            "PID 1 executable",
        )
        mount_point = _resolve_unified_cgroup2_mount("/proc/self/mountinfo")
        descriptor = os.open(mount_point, _DIRECTORY_FLAGS)
        try:
            mount_status = os.fstat(descriptor)
            if not stat.S_ISDIR(mount_status.st_mode):
                raise SystemdVerificationError("cgroup2 mount is not a directory")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeDecodeError, CgroupVerificationError) as error:
        raise SystemdVerificationError("cannot observe local systemd epoch") from error
    return (
        boot_id,
        f"linux-proc-stat/v1:1:{start_value}",
        executable_sha256,
        mount_status.st_dev,
        mount_status.st_ino,
    )


def _sha256_bounded_path(path: str | os.PathLike[str], maximum: int, label: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1_048_576, maximum - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise SystemdVerificationError(f"{label} exceeds its byte limit")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _decode_mount_field(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            output.append(value[index])
            index += 1
            continue
        match = _MOUNT_ESCAPE.match(value, index)
        if match is None or match.group(1) not in {"040", "011", "012", "134"}:
            raise CgroupVerificationError("mountinfo contains an unsafe escape")
        output.append(chr(int(match.group(1), 8)))
        index = match.end()
    return "".join(output)


def _control_group_components(control_group: str) -> tuple[str, ...]:
    if type(control_group) is not str or not control_group.startswith("/"):
        raise CgroupVerificationError("ControlGroup must be absolute")
    if control_group == "/" or control_group.endswith("/") or "\x00" in control_group:
        raise CgroupVerificationError("ControlGroup must be below the cgroup2 mount")
    components = tuple(control_group[1:].split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise CgroupVerificationError("ControlGroup is not a component path")
    return components


def _is_normal_absolute_path(value: str) -> bool:
    if not value.startswith("/") or "\x00" in value:
        return False
    if value == "/":
        return True
    return not value.endswith("/") and all(
        component not in {"", ".", ".."} for component in value[1:].split("/")
    )


def _parse_populated(content: bytes) -> int:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise CgroupVerificationError("cgroup.events is not ASCII") from error
    values: dict[str, int] = {}
    lines = text.splitlines()
    if not lines:
        raise CgroupVerificationError("cgroup.events is empty")
    for line in lines:
        parts = line.split(" ")
        if len(parts) != 2 or _EVENT_KEY.fullmatch(parts[0]) is None:
            raise CgroupVerificationError("cgroup.events contains a malformed record")
        key, raw_value = parts
        if key in values or not raw_value.isascii() or not raw_value.isdecimal():
            raise CgroupVerificationError("cgroup.events contains a malformed value")
        values[key] = int(raw_value, 10)
    if "populated" not in values:
        raise CgroupVerificationError("cgroup.events lacks exactly one populated key")
    return values["populated"]


def _read_bounded_path(
    path: str | os.PathLike[str],
    maximum: int,
    label: str,
) -> bytes:
    descriptor = os.open(path, _READ_FLAGS)
    try:
        return _read_bounded_descriptor(descriptor, maximum, label)
    finally:
        os.close(descriptor)


def _read_bounded_descriptor(descriptor: int, maximum: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > maximum:
        raise CgroupVerificationError(f"{label} exceeds its byte bound")
    return content


def _close_descriptors(descriptors: list[int], mount_descriptor: int) -> tuple[BaseException, ...]:
    failures: list[BaseException] = []
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except BaseException as error:
            failures.append(error)
    if mount_descriptor >= 0:
        try:
            os.close(mount_descriptor)
        except BaseException as error:
            failures.append(error)
    return tuple(failures)


def _annotate_close_failures(primary: BaseException, failures: tuple[BaseException, ...]) -> None:
    for failure in failures:
        primary.add_note(f"cleanup failure: {type(failure).__name__}")


def _require_integer(value: object, field_name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} is outside its permitted integer range")


def _require_unit_name(
    unit_name: object,
    *,
    error_type: type[Exception] = SystemdVerificationError,
) -> None:
    if type(unit_name) is not str or _UNIT_NAME.fullmatch(unit_name) is None:
        raise error_type("unit name is outside the containment namespace")


def _capabilities_from_introspection(
    manager_xml: str,
    scope_xml: str,
    *,
    cgroup_v2_events: SystemdCapabilityState,
    system_bus_id: str,
    systemd_unique_owner: str,
) -> SystemdPrerequisiteReport:
    """Derive only independently evidenced, exact D-Bus capabilities."""

    if type(cgroup_v2_events) is not SystemdCapabilityState:
        raise ValueError("cgroup_v2_events must use SystemdCapabilityState")
    manager_valid = True
    try:
        manager = ElementTree.fromstring(manager_xml)
    except (ElementTree.ParseError, TypeError):
        manager = ElementTree.Element("node")
        manager_valid = False
    scope_valid = True
    try:
        scope = ElementTree.fromstring(scope_xml)
    except (ElementTree.ParseError, TypeError):
        scope = ElementTree.Element("node")
        scope_valid = False

    transient_scopes = _has_exact_method(
        manager,
        "org.freedesktop.systemd1.Manager",
        "StartTransientUnit",
        ("s", "s", "a(sv)", "a(sa(sv))"),
        ("o",),
    )
    stop_unit = _has_exact_method(
        manager,
        "org.freedesktop.systemd1.Manager",
        "StopUnit",
        ("s", "s"),
        ("o",),
    )
    kill_unit = _has_exact_method(
        manager,
        "org.freedesktop.systemd1.Manager",
        "KillUnit",
        ("s", "s", "i"),
        (),
    )

    def observed_state(valid: bool, present: bool) -> SystemdCapabilityState:
        if not valid:
            return SystemdCapabilityState.UNPROVEN
        if present:
            return SystemdCapabilityState.UNPROVEN
        return SystemdCapabilityState.UNSUPPORTED

    return SystemdPrerequisiteReport(
        system_bus_id=system_bus_id,
        systemd_unique_owner=systemd_unique_owner,
        transient_scopes=observed_state(manager_valid, transient_scopes),
        # Introspection proves the PIDs property exists, not that systemd accepts
        # and atomically places both requested PIDs in a new scope.
        initial_pids=SystemdCapabilityState.UNPROVEN,
        invocation_id=observed_state(
            scope_valid,
            _has_exact_property(
                scope,
                "org.freedesktop.systemd1.Unit",
                "InvocationID",
                "ay",
            ),
        ),
        control_group=observed_state(
            scope_valid,
            _has_exact_property(
                scope,
                "org.freedesktop.systemd1.Scope",
                "ControlGroup",
                "s",
            ),
        ),
        group_stop=observed_state(manager_valid, stop_unit and kill_unit),
        cgroup_v2_events=cgroup_v2_events,
    )


def _has_exact_method(
    root: ElementTree.Element,
    interface_name: str,
    method_name: str,
    input_types: tuple[str, ...],
    output_types: tuple[str, ...],
) -> bool:
    for interface in root.findall("interface"):
        if interface.get("name") != interface_name:
            continue
        for method in interface.findall("method"):
            if method.get("name") != method_name:
                continue
            arguments = method.findall("arg")
            inputs = tuple(
                argument.get("type", "")
                for argument in arguments
                if argument.get("direction", "in") == "in"
            )
            outputs = tuple(
                argument.get("type", "")
                for argument in arguments
                if argument.get("direction") == "out"
            )
            return inputs == input_types and outputs == output_types
    return False


def _has_exact_property(
    root: ElementTree.Element,
    interface_name: str,
    property_name: str,
    signature: str,
) -> bool:
    for interface in root.findall("interface"):
        if interface.get("name") != interface_name:
            continue
        for property_element in interface.findall("property"):
            if property_element.get("name") == property_name:
                return (
                    property_element.get("type") == signature
                    and property_element.get("access") == "read"
                )
    return False


class _DbusFastTransport:
    """Private async dbus-fast transport for the fixed manager operations."""

    _DESTINATION = "org.freedesktop.systemd1"
    _DBUS_DESTINATION = "org.freedesktop.DBus"
    _DBUS_PATH = "/org/freedesktop/DBus"
    _DBUS_INTERFACE = "org.freedesktop.DBus"
    _MANAGER_PATH = "/org/freedesktop/systemd1"
    _MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
    _PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def __init__(self) -> None:
        from dbus_fast import BusType, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus

        self._BusType = BusType
        self._Message = Message
        self._MessageType = MessageType
        self._Variant = Variant
        self._MessageBus = MessageBus
        self._bus: Any | None = None
        self._systemd_owner: str | None = None
        self._subscribed = False
        self._job_subscription_installed = False
        self._job_handler_installed = False
        self._epoch_watch: SystemdEpochWatchHandle | None = None
        self._epoch_snapshot: SystemdEpochSnapshot | None = None
        self._epoch_revoked_reason: str | None = None
        self._epoch_revocation_callback: Callable[[str], None] | None = None
        self._epoch_event_count = 0
        self._epoch_handler_installed = False
        self._epoch_phase = "NEW"
        self._pre_pin_reloading_events: list[tuple[str, bool]] = []
        self._install_false_reloading_seen = False
        self._disconnect_task: asyncio.Task[None] | None = None
        self._job_waiters: dict[str, tuple[str, asyncio.Future[_JobRemoval]]] = {}
        self._early_job_results: dict[str, _JobRemoval] = {}
        self._completed_job_paths: set[str] = set()

    async def _connected_bus(self) -> Any:
        if self._bus is None:
            self._bus = await self._MessageBus(bus_type=self._BusType.SYSTEM).connect()
            self._disconnect_task = asyncio.create_task(self._watch_bus_disconnect(self._bus))
        return self._bus

    async def _watch_bus_disconnect(self, bus: Any) -> None:
        try:
            await bus.wait_for_disconnect()
        except BaseException as error:
            self._handle_epoch_watcher_loss(error)
        else:
            self._handle_bus_disconnect()

    async def _call(
        self,
        *,
        path: str,
        interface: str,
        member: str,
        expected_reply_signature: str,
        signature: str = "",
        body: list[object] | None = None,
        destination: str | None = None,
    ) -> Any:
        bus = await self._connected_bus()
        resolved_destination = destination or self._systemd_owner
        if resolved_destination is None:
            raise SystemdVerificationError("systemd D-Bus owner is not pinned")
        reply = await bus.call(
            self._Message(
                destination=resolved_destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        expected_sender = (
            self._DBUS_DESTINATION
            if resolved_destination == self._DBUS_DESTINATION
            else resolved_destination
        )
        if getattr(reply, "sender", None) != expected_sender:
            self._revoke_epoch("D-Bus reply sender did not match addressed peer")
            raise SystemdVerificationError("D-Bus reply sender is not authenticated")
        message_type = getattr(reply, "message_type", None)
        if message_type == self._MessageType.ERROR:
            detail = getattr(reply, "error_name", None)
            if (
                interface == self._MANAGER_INTERFACE
                and member == "GetUnit"
                and detail == "org.freedesktop.systemd1.NoSuchUnit"
            ):
                raise SystemdUnitNotFound("exact systemd unit does not exist")
            raise SystemdVerificationError(f"systemd D-Bus call failed: {detail}")
        if message_type != self._MessageType.METHOD_RETURN:
            self._revoke_epoch("D-Bus reply is not a method return or error")
            raise SystemdVerificationError("D-Bus reply type is malformed")
        if (
            getattr(reply, "signature", None) != expected_reply_signature
            or type(getattr(reply, "body", None)) is not list
            or len(reply.body) != (0 if expected_reply_signature == "" else 1)
            or not self._reply_body_matches_signature(
                expected_reply_signature,
                reply.body,
            )
        ):
            self._revoke_epoch("D-Bus method reply shape is malformed")
            raise SystemdVerificationError("D-Bus method reply shape is malformed")
        return reply

    def _reply_body_matches_signature(self, signature: str, body: list[object]) -> bool:
        if signature == "":
            return body == []
        value = body[0]
        if signature == "s":
            return type(value) is str
        if signature == "o":
            return type(value) is str and _DBUS_OBJECT_PATH.fullmatch(value) is not None
        if signature == "v":
            return type(value) is self._Variant
        if signature == "a{sv}":
            return type(value) is dict and all(
                type(name) is str and type(variant) is self._Variant
                for name, variant in value.items()
            )
        if signature == "a(sus)":
            return type(value) is list
        return False

    async def _pin_systemd_owner(self) -> None:
        if self._systemd_owner is not None:
            return
        raise SystemdVerificationError("systemd owner requires the installed epoch snapshot")

    async def install_epoch_watch(self) -> SystemdEpochWatchHandle:
        if self._epoch_watch is not None:
            raise SystemdVerificationError("systemd epoch watch cannot be replaced")
        watch = SystemdEpochWatchHandle(f"epoch-{secrets.token_hex(16)}")
        self._epoch_watch = watch
        self._epoch_phase = "INSTALLING_UNKNOWN"
        try:
            bus = await self._connected_bus()
            if not self._epoch_handler_installed:
                bus.add_message_handler(self._handle_epoch_signal)
                self._epoch_handler_installed = True
            rules = (
                "type='signal',sender='org.freedesktop.DBus',"
                "path='/org/freedesktop/DBus',"
                "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                "arg0='org.freedesktop.systemd1'",
                "type='signal',sender='org.freedesktop.systemd1',"
                "path='/org/freedesktop/systemd1',"
                "interface='org.freedesktop.systemd1.Manager',member='Reloading'",
            )
            for rule in rules:
                await self._call(
                    destination=self._DBUS_DESTINATION,
                    path=self._DBUS_PATH,
                    interface=self._DBUS_INTERFACE,
                    member="AddMatch",
                    signature="s",
                    body=[rule],
                    expected_reply_signature="",
                )
        except BaseException:
            self._revoke_epoch("epoch watch installation failed")
            raise
        return watch

    async def read_epoch_snapshot(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot:
        self._require_live_epoch_watch(watch)
        if self._epoch_snapshot is not None:
            raise SystemdVerificationError("systemd epoch snapshot cannot be repinned")
        try:
            snapshot = await self._capture_epoch_snapshot(watch)
        except BaseException:
            self._revoke_epoch("initial epoch snapshot failed")
            raise
        self._require_live_epoch_watch(watch)
        self._epoch_snapshot = snapshot
        return snapshot

    async def assert_epoch_current(
        self, watch: SystemdEpochWatchHandle, snapshot: SystemdEpochSnapshot
    ) -> None:
        self._require_live_epoch_watch(watch)
        if snapshot != self._epoch_snapshot or snapshot.watch_id != watch.watch_id:
            self._revoke_epoch("epoch assertion did not use the installed snapshot")
            raise SystemdVerificationError("systemd epoch snapshot changed")
        try:
            current = await self._capture_epoch_snapshot(watch)
        except BaseException:
            self._revoke_epoch("epoch currentness sample failed")
            raise
        self._require_live_epoch_watch(watch)
        if current != snapshot:
            self._revoke_epoch("systemd epoch identity changed")
            raise SystemdVerificationError("systemd epoch identity changed")

    async def _capture_epoch_snapshot(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot:
        if self._epoch_snapshot is None:
            return await self._establish_epoch_snapshot(watch)
        return await self._sample_current_epoch(watch)

    async def _establish_epoch_snapshot(
        self, watch: SystemdEpochWatchHandle
    ) -> SystemdEpochSnapshot:
        initial_bus_id, initial_owner = await self._read_bus_identity()
        if self._systemd_owner is not None and initial_owner != self._systemd_owner:
            self._revoke_epoch("systemd owner changed while pinning")
            raise SystemdVerificationError("systemd owner changed while pinning")
        self._systemd_owner = initial_owner
        self._validate_installing_reloading_events()
        await self._subscribe_manager()
        self._validate_installing_reloading_events()
        await self._read_manager_property("Version", "s")
        self._validate_installing_reloading_events()
        self._require_live_epoch_watch(watch)
        self._epoch_phase = "FENCE_CANDIDATE"
        units_load_timestamp = await self._read_manager_property(
            "UnitsLoadTimestampMonotonic",
            "t",
        )
        bus_id, owner = await self._read_bus_identity()
        if (bus_id, owner) != (initial_bus_id, initial_owner):
            self._revoke_epoch("system bus identity changed during epoch fence")
            raise SystemdVerificationError("system bus identity changed during epoch fence")
        snapshot = self._snapshot_from_local_observation(
            watch,
            bus_id,
            owner,
            units_load_timestamp,
        )
        self._require_live_epoch_watch(watch)
        self._pre_pin_reloading_events.clear()
        self._epoch_phase = "CURRENT"
        return snapshot

    async def _subscribe_manager(self) -> None:
        if self._subscribed:
            return
        await self._call(
            destination=self._systemd_owner,
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="Subscribe",
            expected_reply_signature="",
        )
        self._subscribed = True

    async def _sample_current_epoch(self, watch: SystemdEpochWatchHandle) -> SystemdEpochSnapshot:
        await self._read_manager_property("Version", "s")
        units_load_timestamp = await self._read_manager_property(
            "UnitsLoadTimestampMonotonic",
            "t",
        )
        bus_id, owner = await self._read_bus_identity()
        return self._snapshot_from_local_observation(
            watch,
            bus_id,
            owner,
            units_load_timestamp,
        )

    async def _read_bus_identity(self) -> tuple[str, str]:
        bus_reply = await self._call(
            destination=self._DBUS_DESTINATION,
            path=self._DBUS_PATH,
            interface=self._DBUS_INTERFACE,
            member="GetId",
            expected_reply_signature="s",
        )
        owner_reply = await self._call(
            destination=self._DBUS_DESTINATION,
            path=self._DBUS_PATH,
            interface=self._DBUS_INTERFACE,
            member="GetNameOwner",
            signature="s",
            body=[self._DESTINATION],
            expected_reply_signature="s",
        )
        bus_id = bus_reply.body[0]
        owner = owner_reply.body[0]
        if (
            type(bus_id) is not str
            or _SYSTEM_BUS_ID.fullmatch(bus_id) is None
            or type(owner) is not str
            or _UNIQUE_BUS_NAME.fullmatch(owner) is None
        ):
            self._revoke_epoch("system bus identity is malformed")
            raise SystemdVerificationError("system bus identity is malformed")
        if self._systemd_owner is not None and owner != self._systemd_owner:
            self._revoke_epoch("systemd owner changed")
            raise SystemdVerificationError("systemd owner changed")
        return bus_id, owner

    async def _read_manager_property(
        self,
        property_name: str,
        expected_variant_signature: str,
    ) -> object:
        reply = await self._call(
            destination=self._systemd_owner,
            path=self._MANAGER_PATH,
            interface=self._PROPERTIES_INTERFACE,
            member="Get",
            signature="ss",
            body=[self._MANAGER_INTERFACE, property_name],
            expected_reply_signature="v",
        )
        variant = reply.body[0]
        value = self._require_variant_value(
            variant,
            expected_variant_signature,
            property_name,
        )
        if expected_variant_signature not in {"s", "t"}:
            raise AssertionError("unexpected manager fence property")
        return value

    def _snapshot_from_local_observation(
        self,
        watch: SystemdEpochWatchHandle,
        bus_id: str,
        owner: str,
        units_load_timestamp: object,
    ) -> SystemdEpochSnapshot:
        local = _observe_local_epoch()
        try:
            return SystemdEpochSnapshot(
                watch_id=watch.watch_id,
                system_bus_id=bus_id,
                systemd_unique_owner=owner,
                reloading=False,
                boot_id=local[0],
                pid1_start_token=local[1],
                pid1_executable_sha256=local[2],
                cgroup_mount_device=local[3],
                cgroup_mount_inode=local[4],
                units_load_timestamp_monotonic=units_load_timestamp,
            )
        except ValueError as error:
            self._revoke_epoch("local systemd epoch identity is malformed")
            raise SystemdVerificationError("local systemd epoch identity is malformed") from error

    def _validate_installing_reloading_events(self) -> None:
        events = tuple(self._pre_pin_reloading_events)
        self._pre_pin_reloading_events.clear()
        for sender, reloading in events:
            if sender != self._systemd_owner:
                self._revoke_epoch("Reloading signal sender is not pinned systemd")
                raise SystemdVerificationError("Reloading signal sender is not pinned systemd")
            if reloading:
                self._revoke_epoch("systemd entered reload during epoch install")
                raise SystemdVerificationError("systemd entered reload during epoch install")
            if self._install_false_reloading_seen:
                self._revoke_epoch("multiple pre-fence Reloading(false) signals")
                raise SystemdVerificationError("multiple pre-fence Reloading(false) signals")
            self._install_false_reloading_seen = True

    def _handle_epoch_signal(self, message: Any) -> bool:
        if self._epoch_watch is None or self._epoch_revoked_reason is not None:
            return False
        member = getattr(message, "member", None)
        if member not in {"NameOwnerChanged", "Reloading"}:
            return False
        self._epoch_event_count += 1
        if self._epoch_event_count > MAXIMUM_EPOCH_EVENTS:
            self._revoke_epoch("epoch event queue overflow")
            return False
        if member == "NameOwnerChanged":
            valid = (
                getattr(message, "message_type", None) == self._MessageType.SIGNAL
                and getattr(message, "sender", None) == self._DBUS_DESTINATION
                and getattr(message, "path", None) == self._DBUS_PATH
                and getattr(message, "interface", None) == self._DBUS_INTERFACE
                and getattr(message, "signature", None) == "sss"
                and type(getattr(message, "body", None)) is list
                and len(message.body) == 3
                and message.body[0] == self._DESTINATION
                and all(type(value) is str for value in message.body)
            )
            if not valid:
                self._revoke_epoch("NameOwnerChanged signal is unauthenticated")
            else:
                self._revoke_epoch("systemd owner changed")
            return False
        valid = (
            getattr(message, "message_type", None) == self._MessageType.SIGNAL
            and type(getattr(message, "sender", None)) is str
            and getattr(message, "path", None) == self._MANAGER_PATH
            and getattr(message, "interface", None) == self._MANAGER_INTERFACE
            and getattr(message, "signature", None) == "b"
            and type(getattr(message, "body", None)) is list
            and len(message.body) == 1
            and type(message.body[0]) is bool
        )
        if not valid:
            self._revoke_epoch("Reloading signal is unauthenticated")
            return False
        if self._epoch_phase == "INSTALLING_UNKNOWN":
            self._pre_pin_reloading_events.append((message.sender, message.body[0]))
            return False
        if message.sender != self._systemd_owner:
            self._revoke_epoch("Reloading signal sender is not pinned systemd")
        else:
            self._revoke_epoch("systemd Reloading signal after synchronization fence")
        return False

    def _handle_bus_disconnect(self) -> None:
        self._revoke_epoch("system bus disconnected")

    def _handle_epoch_watcher_loss(self, error: BaseException) -> None:
        self._revoke_epoch(f"epoch watcher failed: {type(error).__name__}")

    def _revoke_epoch(self, reason: str) -> None:
        if self._epoch_watch is not None and self._epoch_revoked_reason is None:
            self._epoch_revoked_reason = reason
            self._epoch_phase = "REVOKED"
            self._pre_pin_reloading_events.clear()
            self._early_job_results.clear()
            self._completed_job_paths.clear()
            waiters = tuple(self._job_waiters.values())
            self._job_waiters.clear()
            for _unit_name, waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(SystemdVerificationError("systemd epoch watch is revoked"))
            callback = getattr(self, "_epoch_revocation_callback", None)
            if callback is not None:
                callback(reason)

    def _bind_epoch_revocation_callback(self, callback: Callable[[str], None]) -> None:
        if not callable(callback) or self._epoch_revocation_callback is not None:
            raise SystemdVerificationError("epoch revocation callback is not unique")
        self._epoch_revocation_callback = callback
        if self._epoch_revoked_reason is not None:
            callback(self._epoch_revoked_reason)

    def _require_live_epoch_watch(self, watch: SystemdEpochWatchHandle) -> None:
        if watch != self._epoch_watch or self._epoch_revoked_reason is not None:
            raise SystemdVerificationError("systemd epoch watch is revoked")

    async def _ensure_job_subscription(self) -> None:
        if self._job_subscription_installed:
            return
        await self._pin_systemd_owner()
        assert self._systemd_owner is not None
        if not self._subscribed:
            raise SystemdVerificationError("systemd Manager signal subscription is not established")
        bus = await self._connected_bus()
        if not self._job_handler_installed:
            bus.add_message_handler(self._handle_job_removed)
            self._job_handler_installed = True
        match_rule = (
            "type='signal',sender='" + self._systemd_owner + "',path='/org/freedesktop/systemd1',"
            "interface='org.freedesktop.systemd1.Manager',member='JobRemoved'"
        )
        await self._call(
            destination=self._DBUS_DESTINATION,
            path=self._DBUS_PATH,
            interface=self._DBUS_INTERFACE,
            member="AddMatch",
            signature="s",
            body=[match_rule],
            expected_reply_signature="",
        )
        self._job_subscription_installed = True

    def _handle_job_removed(self, message: Any) -> bool:
        if self._epoch_revoked_reason is not None:
            return False
        if getattr(message, "member", None) != "JobRemoved":
            return False
        if getattr(
            message, "sender", None
        ) != self._systemd_owner and not self._job_removed_references_containment(message):
            return False
        try:
            removal = self._validated_job_removal(message)
        except SystemdVerificationError:
            self._revoke_epoch("JobRemoved signal is malformed or unauthenticated")
            return False
        if (
            removal.job_path not in self._job_waiters
            and removal.job_path not in self._early_job_results
            and removal.job_path not in self._completed_job_paths
            and _UNIT_NAME.fullmatch(removal.unit_name) is None
        ):
            return False
        if removal.job_path in self._completed_job_paths:
            self._revoke_epoch("duplicate completed JobRemoved result")
            return False
        waiter_entry = self._job_waiters.get(removal.job_path)
        if waiter_entry is not None:
            expected_unit, waiter = waiter_entry
            if removal.unit_name != expected_unit:
                self._revoke_epoch("JobRemoved unit does not match requested unit")
            elif waiter.done():
                self._revoke_epoch("duplicate JobRemoved result")
            else:
                waiter.set_result(removal)
        else:
            if len(self._early_job_results) >= 256:
                self._revoke_epoch("early JobRemoved result overflow")
            elif removal.job_path in self._early_job_results:
                self._revoke_epoch("duplicate early JobRemoved result")
            else:
                self._early_job_results[removal.job_path] = removal
        return False

    def _job_removed_references_containment(self, message: Any) -> bool:
        body = getattr(message, "body", None)
        if type(body) is not list:
            return False
        job_path = body[1] if len(body) > 1 else None
        unit_name = body[2] if len(body) > 2 else None
        return (
            type(job_path) is str
            and (
                job_path in self._job_waiters
                or job_path in self._early_job_results
                or job_path in self._completed_job_paths
            )
        ) or (type(unit_name) is str and _UNIT_NAME.fullmatch(unit_name) is not None)

    def _validated_job_removal(self, message: Any) -> _JobRemoval:
        if (
            self._systemd_owner is None
            or getattr(message, "message_type", None) != self._MessageType.SIGNAL
            or getattr(message, "sender", None) != self._systemd_owner
            or getattr(message, "path", None) != self._MANAGER_PATH
            or getattr(message, "interface", None) != self._MANAGER_INTERFACE
            or getattr(message, "member", None) != "JobRemoved"
            or getattr(message, "signature", None) != "uoss"
        ):
            raise SystemdVerificationError("JobRemoved envelope is malformed")
        body = getattr(message, "body", None)
        if type(body) is not list or len(body) != 4:
            raise SystemdVerificationError("JobRemoved body is malformed")
        job_id, job_path, unit_name, result = body
        if (
            type(job_id) is not int
            or not 1 <= job_id <= (1 << 32) - 1
            or type(job_path) is not str
            or type(unit_name) is not str
            or type(result) is not str
            or _SYSTEMD_JOB_PATH.fullmatch(job_path) is None
            or int(job_path.rsplit("/", 1)[1]) != job_id
        ):
            raise SystemdVerificationError("JobRemoved body is malformed")
        try:
            SystemdStopJobEvidenceV1(job_path=job_path, result=result)
        except ValueError as error:
            raise SystemdVerificationError("JobRemoved result is malformed") from error
        return _JobRemoval(job_path, unit_name, result)

    async def probe_capabilities(self) -> SystemdPrerequisiteReport:
        if self._epoch_watch is None or self._epoch_snapshot is None:
            raise SystemdVerificationError(
                "capability probing requires an installed epoch snapshot"
            )
        await self.assert_epoch_current(self._epoch_watch, self._epoch_snapshot)
        manager_xml = ""
        try:
            reply = await self._call(
                path=self._MANAGER_PATH,
                interface="org.freedesktop.DBus.Introspectable",
                member="Introspect",
                expected_reply_signature="s",
            )
            manager_xml = reply.body[0]
        except SystemdVerificationError:
            if self._epoch_revoked_reason is not None:
                raise
        try:
            _resolve_unified_cgroup2_mount("/proc/self/mountinfo")
            cgroup_events = SystemdCapabilityState.UNPROVEN
        except CgroupVerificationError:
            cgroup_events = SystemdCapabilityState.UNPROVEN
        return _capabilities_from_introspection(
            manager_xml if type(manager_xml) is str else "",
            "",
            cgroup_v2_events=cgroup_events,
            system_bus_id=self._epoch_snapshot.system_bus_id,
            systemd_unique_owner=self._epoch_snapshot.systemd_unique_owner,
        )

    async def start_transient_unit(
        self,
        unit_name: str,
        mode: str,
        properties: tuple[tuple[str, object], ...],
        auxiliaries: tuple[object, ...],
    ) -> ScopeStartResult:
        await self._ensure_job_subscription()
        reply = await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="StartTransientUnit",
            signature="ssa(sv)a(sa(sv))",
            body=[unit_name, mode, list(properties), list(auxiliaries)],
            expected_reply_signature="o",
        )
        job_path = self._require_job_path(reply.body[0])
        observation = await self.await_stop_job(job_path, unit_name)
        if observation is None:
            raise SystemdVerificationError("start job observation is unavailable")
        return ScopeStartResult(unit_name, observation[0], observation[1])

    async def resolve_unit_by_pid(self, pid: int) -> str:
        await self._pin_systemd_owner()
        reply = await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="GetUnitByPID",
            signature="u",
            body=[pid],
            expected_reply_signature="o",
        )
        unit_path = self._require_unit_path(reply.body[0])
        properties = await self._get_all(
            unit_path,
            "org.freedesktop.systemd1.Unit",
            (("Id", "s"),),
        )
        unit_name = properties["Id"]
        _require_unit_name(unit_name)
        return unit_name

    async def read_unit_identity(self, unit_name: str) -> UnitIdentity:
        await self._pin_systemd_owner()
        unit_path = await self._unit_path(unit_name)
        unit = await self._get_all(
            unit_path,
            "org.freedesktop.systemd1.Unit",
            (
                ("Id", "s"),
                ("InvocationID", "ay"),
                ("Description", "s"),
                ("CollectMode", "s"),
                ("ActiveState", "s"),
                ("SubState", "s"),
            ),
        )
        scope = await self._get_all(
            unit_path,
            "org.freedesktop.systemd1.Scope",
            (
                ("ControlGroup", "s"),
                ("Slice", "s"),
                ("KillMode", "s"),
                ("TimeoutStopUSec", "t"),
                ("SendSIGKILL", "b"),
                ("RuntimeMaxUSec", "t"),
                ("Delegate", "b"),
                ("Controller", "s"),
            ),
        )
        pids = await self._unit_pids(unit_name)
        invocation = unit["InvocationID"]
        invocation_text = bytes(invocation).hex()
        return UnitIdentity(
            unit_name=unit["Id"],
            invocation_id=invocation_text,
            control_group=scope["ControlGroup"],
            description=unit["Description"],
            slice=scope["Slice"],
            pids=pids,
            kill_mode=scope["KillMode"],
            timeout_stop_usec=scope["TimeoutStopUSec"],
            send_sigkill=scope["SendSIGKILL"],
            collect_mode=unit["CollectMode"],
            runtime_max_usec=scope["RuntimeMaxUSec"],
            delegate=scope["Delegate"],
            controller=scope["Controller"],
            active_state=unit["ActiveState"],
            sub_state=unit["SubState"],
        )

    async def begin_stop_unit(self, unit_name: str, mode: str) -> str:
        await self._ensure_job_subscription()
        reply = await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="StopUnit",
            signature="ss",
            body=[unit_name, mode],
            expected_reply_signature="o",
        )
        return self._require_job_path(reply.body[0])

    async def await_stop_job(self, job_path: str, unit_name: str) -> tuple[str, str] | None:
        if self._epoch_watch is not None:
            self._require_live_epoch_watch(self._epoch_watch)
        self._require_job_path(job_path)
        _require_unit_name(unit_name)
        if job_path in self._completed_job_paths:
            self._revoke_epoch("JobRemoved result was already consumed")
            raise SystemdVerificationError("JobRemoved result was already consumed")
        if job_path in self._job_waiters:
            raise SystemdVerificationError(
                "a JobRemoved waiter is already active for this job path"
            )
        early = self._early_job_results.pop(job_path, None)
        if early is not None:
            if early.unit_name != unit_name:
                self._revoke_epoch("JobRemoved unit does not match requested unit")
                raise SystemdVerificationError("JobRemoved unit does not match requested unit")
            self._remember_completed_job_path(job_path)
            return early.job_path, early.result
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[_JobRemoval] = loop.create_future()
        self._job_waiters[job_path] = (unit_name, waiter)
        completed = False
        try:
            removal = await waiter
            if self._epoch_watch is not None:
                self._require_live_epoch_watch(self._epoch_watch)
            self._remember_completed_job_path(job_path)
            completed = True
            return removal.job_path, removal.result
        finally:
            current = self._job_waiters.get(job_path)
            if current is not None and current[1] is waiter:
                self._job_waiters.pop(job_path)
            if (
                not completed
                and self._epoch_revoked_reason is None
                and waiter.done()
                and not waiter.cancelled()
            ):
                try:
                    delivered = waiter.result()
                except BaseException:
                    pass
                else:
                    if job_path in self._early_job_results or len(self._early_job_results) >= 256:
                        self._revoke_epoch("cancelled JobRemoved recovery is ambiguous")
                    else:
                        self._early_job_results[job_path] = delivered

    def _remember_completed_job_path(self, job_path: str) -> None:
        if (
            job_path in self._completed_job_paths
            or len(self._completed_job_paths) >= MAXIMUM_COMPLETED_JOB_PATHS
        ):
            self._revoke_epoch("completed JobRemoved tombstone overflow or reuse")
            raise SystemdVerificationError("completed JobRemoved tombstone overflow or reuse")
        self._completed_job_paths.add(job_path)

    async def kill_unit_all(self, unit_name: str) -> None:
        await self._pin_systemd_owner()
        await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="KillUnit",
            signature="ssi",
            body=[unit_name, "all", signal.SIGKILL],
            expected_reply_signature="",
        )

    async def ref_unit(self, unit_name: str) -> None:
        await self._manager_unit_call("RefUnit", unit_name)

    async def unref_unit(self, unit_name: str) -> None:
        await self._manager_unit_call("UnrefUnit", unit_name)

    async def _manager_unit_call(self, member: str, unit_name: str) -> None:
        await self._pin_systemd_owner()
        await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member=member,
            signature="s",
            body=[unit_name],
            expected_reply_signature="",
        )

    async def _unit_path(self, unit_name: str) -> str:
        reply = await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="GetUnit",
            signature="s",
            body=[unit_name],
            expected_reply_signature="o",
        )
        return self._require_unit_path(reply.body[0])

    async def _unit_pids(self, unit_name: str) -> tuple[int, ...]:
        reply = await self._call(
            path=self._MANAGER_PATH,
            interface=self._MANAGER_INTERFACE,
            member="GetUnitProcesses",
            signature="s",
            body=[unit_name],
            expected_reply_signature="a(sus)",
        )
        processes = reply.body[0]
        if type(processes) is not list:
            self._revoke_epoch("GetUnitProcesses reply is malformed")
            raise SystemdVerificationError("GetUnitProcesses reply is malformed")
        pids: list[int] = []
        for process in processes:
            if (
                type(process) is not tuple
                or len(process) != 3
                or type(process[0]) is not str
                or type(process[1]) is not int
                or not 0 <= process[1] <= (1 << 32) - 1
                or type(process[2]) is not str
            ):
                self._revoke_epoch("GetUnitProcesses reply is malformed")
                raise SystemdVerificationError("GetUnitProcesses reply is malformed")
            pids.append(process[1])
        return tuple(pids)

    async def _get_all(
        self,
        path: str,
        interface_name: str,
        expected_properties: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        reply = await self._call(
            path=path,
            interface=self._PROPERTIES_INTERFACE,
            member="GetAll",
            signature="s",
            body=[interface_name],
            expected_reply_signature="a{sv}",
        )
        properties = reply.body[0]
        if type(properties) is not dict:
            self._revoke_epoch("GetAll reply is malformed")
            raise SystemdVerificationError("GetAll reply is malformed")
        validated: dict[str, object] = {}
        for property_name, signature in expected_properties:
            if property_name not in properties:
                self._revoke_epoch(f"systemd property {property_name} is missing")
                raise SystemdVerificationError(f"systemd property {property_name} is missing")
            validated[property_name] = self._require_variant_value(
                properties[property_name],
                signature,
                property_name,
            )
        return validated

    def _require_variant_value(
        self,
        variant: object,
        signature: str,
        field_name: str,
    ) -> object:
        if type(variant) is not self._Variant or getattr(variant, "signature", None) != signature:
            self._revoke_epoch(f"systemd {field_name} Variant is malformed")
            raise SystemdVerificationError(f"systemd {field_name} Variant is malformed")
        value = variant.value
        valid = False
        if signature == "s":
            valid = type(value) is str
        elif signature == "ay":
            valid = type(value) in (bytes, bytearray) and len(value) == 16
        elif signature == "t":
            valid = type(value) is int and 0 <= value <= (1 << 64) - 1
        elif signature == "b":
            valid = type(value) is bool
        if not valid:
            self._revoke_epoch(f"systemd {field_name} Variant value is malformed")
            raise SystemdVerificationError(f"systemd {field_name} Variant value is malformed")
        return value

    def _require_job_path(self, value: object) -> str:
        if type(value) is not str or _SYSTEMD_JOB_PATH.fullmatch(value) is None:
            self._revoke_epoch("systemd job object path is malformed")
            raise SystemdVerificationError("systemd job object path is malformed")
        return value

    def _require_unit_path(self, value: object) -> str:
        if type(value) is not str or _SYSTEMD_UNIT_PATH.fullmatch(value) is None:
            self._revoke_epoch("systemd unit object path is malformed")
            raise SystemdVerificationError("systemd unit object path is malformed")
        return value
