#!/usr/bin/python3 -I
"""Fixed root-only no-request startup-certification service entry point.

The `StartupCertificationConfig` record carries `startup_probe_path`,
`startup_probe_sha256`, `startup_probe_account`, `startup_probe_root_owned`,
`graceful_stop_seconds` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import array
import asyncio
import ctypes
import errno
import hashlib
import os
import pwd
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .codec import loads_canonical_packet
from .startup_certification import (
    BrokerStartupCertification,
    PosixStartupProbeLauncher,
    StartupBindingObserver,
    StartupCertificationBindings,
    StartupCertificationDeadlines,
    StartupCertificationScopePolicy,
    certify_broker_startup,
    observe_certification_epoch,
    scope_policy_sha256_for_request,
    scope_request_template_sha256,
)
from .systemd_adapter import (
    SCOPE_SLICE,
    DbusFastSystemdManager,
    ScopeStartRequest,
)

CONFIG_PATH = "/etc/mif-pipeline/containment-broker.json"
RUNNER_BUILD_ID = "mif-containment-startup-certification-runner/v1"
STARTUP_CERTIFICATION_FAILURE = 1
_MAXIMUM_CONFIG_BYTES = 65_536
_MAXIMUM_FAILURE_DIAGNOSTIC_BYTES = 1_024
_FAILURE_DIAGNOSTIC_PREFIX = "mif-containment-startup-certification failure "
_CERTIFICATION_DESCRIPTION = "mIF containment startup certification"
_TEMPLATE_UNIT = "mif-qupath-cert-00000000000000000000000000000000.scope"
_STARTUP_PROBE_PATH = "/usr/libexec/mif-pipeline/mif-containment-startup-probe"
_EXPECTED_SERVICE_CAPABILITIES = frozenset((5, 6, 7, 8, 19))
_EXPECTED_SERVICE_CAPABILITY_MASK = sum(1 << item for item in _EXPECTED_SERVICE_CAPABILITIES)
_TOP_LEVEL_FIELDS = frozenset(
    (
        "schema_name",
        "schema_version",
        "protocol_min",
        "protocol_max",
        "state_root",
        "socket_group",
        "scope_slice",
        "graceful_stop_seconds",
        "maximum_runtime_seconds",
        "maximum_request_bytes",
        "maximum_argument_count",
        "maximum_environment_entries",
        "maximum_environment_bytes",
        "startup_certification_timeout_seconds",
        "startup_reconciliation_timeout_seconds",
        "startup_reconciliation_maximum_records",
        "startup_notify_barrier_timeout_seconds",
        "gate",
        "anchor",
        "startup_probe",
        "registrations",
    )
)
_INSTALLED_EXECUTABLE_FIELDS = frozenset(("canonical_path", "sha256", "root_owned"))
_STARTUP_PROBE_FIELDS = frozenset(("canonical_path", "sha256", "account_name", "root_owned"))


# ==============================================================================
#                          Startup certification runner
# ==============================================================================


class StartupCertificationRunnerError(RuntimeError):
    """The fixed service could not close every startup-certification boundary."""


@dataclass(frozen=True, slots=True)
class StartupCertificationConfig:
    startup_probe_path: str
    startup_probe_sha256: str
    startup_probe_account: str
    startup_probe_root_owned: bool
    graceful_stop_seconds: float
    maximum_runtime_seconds: float
    startup_certification_timeout_seconds: float
    startup_reconciliation_timeout_seconds: float
    startup_reconciliation_maximum_records: int
    startup_notify_barrier_timeout_seconds: float

    def deadlines(self) -> StartupCertificationDeadlines:
        total = self.startup_certification_timeout_seconds
        bounded_step = min(total, 10.0)
        return StartupCertificationDeadlines(
            total_seconds=total,
            probe_ready_seconds=bounded_step,
            start_job_seconds=bounded_step,
            stop_job_seconds=bounded_step,
            cleanup_seconds=bounded_step,
        )

    def scope_policy(self) -> StartupCertificationScopePolicy:
        return StartupCertificationScopePolicy(
            graceful_stop_seconds=self.graceful_stop_seconds,
            maximum_runtime_seconds=self.maximum_runtime_seconds,
        )


@dataclass(frozen=True, slots=True)
class StartupCertificationRunnerStatus:
    result: str
    drain_proof_kind: str
    probe_capability_sets: str = "all-zero"
    service_capabilities: str = "exact-five"
    cross_uid_proc: str = "verified"
    forbidden_syscalls: str = "blocked"
    network: str = "blocked"
    project_writes: str = "blocked"

    def __post_init__(self) -> None:
        if self.result != "passed":
            raise ValueError("runner status can represent only complete success")
        if self.drain_proof_kind not in (
            "cgroup-events-zero",
            "cgroup-deleted-after-stop",
        ):
            raise ValueError("runner status requires one closed drain proof")

    def to_mapping(self) -> dict[str, str]:
        return {
            "result": self.result,
            "drain_proof_kind": self.drain_proof_kind,
            "probe_capability_sets": self.probe_capability_sets,
            "service_capabilities": self.service_capabilities,
            "cross_uid_proc": self.cross_uid_proc,
            "forbidden_syscalls": self.forbidden_syscalls,
            "network": self.network,
            "project_writes": self.project_writes,
        }

    def status_text(self) -> str:
        values = self.to_mapping()
        return (
            "mif startup certification passed; "
            f"drain_proof={values['drain_proof_kind']}; "
            f"probe_capability_sets={values['probe_capability_sets']}; "
            f"service_capabilities={values['service_capabilities']}; "
            f"cross_uid_proc={values['cross_uid_proc']}; "
            f"forbidden_syscalls={values['forbidden_syscalls']}; "
            f"network={values['network']}; "
            f"project_writes={values['project_writes']}"
        )


@dataclass(frozen=True, slots=True)
class _RootAssetIdentity:
    path: str
    device: int
    inode: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProductionStartupBindingObserver(StartupBindingObserver):
    config_asset: _RootAssetIdentity
    probe_asset: _RootAssetIdentity
    scope_policy: StartupCertificationScopePolicy
    startup_probe_account: str
    startup_probe_uid: int
    startup_probe_gid: int

    def assert_bindings_current(self, bindings: StartupCertificationBindings) -> None:
        current_pid = os.getpid()
        template_request = _scope_template_request(self.scope_policy)
        if (
            bindings.broker_process_id != current_pid
            or bindings.broker_process_start_token != _process_start_token(current_pid)
            or bindings.kernel_release != os.uname().release
            or bindings.broker_build_id != RUNNER_BUILD_ID
            or bindings.broker_config_sha256 != self.config_asset.sha256
            or bindings.scope_policy_sha256 != scope_policy_sha256_for_request(template_request)
            or bindings.scope_request_template_sha256
            != scope_request_template_sha256(template_request)
            or bindings.startup_probe_path != _STARTUP_PROBE_PATH
            or self.probe_asset.path != _STARTUP_PROBE_PATH
            or bindings.startup_probe_sha256 != self.probe_asset.sha256
            or bindings.startup_probe_account != self.startup_probe_account
        ):
            raise StartupCertificationRunnerError("local startup binding changed")
        _assert_same_root_asset(self.config_asset)
        _assert_same_root_asset(self.probe_asset)
        account = _probe_account(self.startup_probe_account)
        if (account.pw_uid, account.pw_gid) != (
            self.startup_probe_uid,
            self.startup_probe_gid,
        ):
            raise StartupCertificationRunnerError("startup probe account changed")

    def assert_certificate_current(
        self,
        certification: BrokerStartupCertification,
    ) -> None:
        if not isinstance(certification, BrokerStartupCertification):
            raise StartupCertificationRunnerError("startup certificate is malformed")
        bindings = StartupCertificationBindings(
            broker_process_id=certification.broker_process_id,
            broker_process_start_token=certification.broker_process_start_token,
            broker_build_id=certification.broker_build_id,
            broker_config_sha256=certification.broker_config_sha256,
            scope_policy_sha256=certification.scope_policy_sha256,
            scope_request_template_sha256=certification.scope_request_template_sha256,
            startup_probe_path=certification.startup_probe_path,
            startup_probe_sha256=certification.startup_probe_sha256,
            startup_probe_account=certification.startup_probe_account,
            boot_id=certification.boot_id,
            kernel_release=certification.kernel_release,
            system_bus_id=certification.system_bus_id,
            systemd_unique_owner=certification.systemd_unique_owner,
            units_load_timestamp_monotonic=certification.units_load_timestamp_monotonic,
            pid1_start_token=certification.pid1_start_token,
            pid1_executable_sha256=certification.pid1_executable_sha256,
            cgroup_mount_device=certification.cgroup_identity.mount_device,
            cgroup_mount_inode=certification.cgroup_identity.mount_inode,
        )
        self.assert_bindings_current(bindings)


class SystemdNotifier:
    """Minimal sd_notify transport with the required acknowledgement barrier."""

    def __init__(self, address: str) -> None:
        if type(address) is not str or not address:
            raise StartupCertificationRunnerError("NOTIFY_SOCKET is unavailable")
        self._address = ("\0" + address[1:]) if address.startswith("@") else address

    @classmethod
    def from_environment(cls) -> "SystemdNotifier":
        return cls(os.environ.get("NOTIFY_SOCKET", ""))

    def ready(self, status: StartupCertificationRunnerStatus) -> None:
        self._send(f"READY=1\nSTATUS={status.status_text()}".encode("utf-8"))

    def stopping(self, status: str) -> None:
        self._send(f"STOPPING=1\nSTATUS={status}".encode("utf-8"))

    async def barrier(self, timeout_seconds: float) -> None:
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise StartupCertificationRunnerError("notify barrier deadline is invalid")
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        try:
            transport.connect(self._address)
            transport.sendmsg(
                [b"BARRIER=1"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", (write_fd,)))],
            )
            os.close(write_fd)
            write_fd = -1
            loop = asyncio.get_running_loop()
            acknowledged: asyncio.Future[None] = loop.create_future()

            def readable() -> None:
                if acknowledged.done():
                    return
                try:
                    content = os.read(read_fd, 1)
                except BlockingIOError:
                    return
                if content:
                    acknowledged.set_exception(
                        StartupCertificationRunnerError("notify barrier returned data")
                    )
                else:
                    acknowledged.set_result(None)

            loop.add_reader(read_fd, readable)
            try:
                await asyncio.wait_for(acknowledged, float(timeout_seconds))
            finally:
                loop.remove_reader(read_fd)
        except StartupCertificationRunnerError:
            raise
        except BaseException as error:
            raise StartupCertificationRunnerError("notify barrier was incomplete") from error
        finally:
            transport.close()
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def _send(self, payload: bytes) -> None:
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        try:
            transport.sendto(payload, self._address)
        except OSError as error:
            raise StartupCertificationRunnerError("systemd notification failed") from error
        finally:
            transport.close()


def parse_startup_certification_config(raw: bytes) -> StartupCertificationConfig:
    """Parse the closed canonical broker config subset used by the early runner."""

    decoded = loads_canonical_packet(raw, maximum_bytes=_MAXIMUM_CONFIG_BYTES)
    mapping = _exact_mapping(decoded, _TOP_LEVEL_FIELDS, "broker config")
    if (
        mapping["schema_name"] != "mif-containment-broker-config"
        or mapping["schema_version"] != 1
        or mapping["protocol_min"] != 1
        or mapping["protocol_max"] != 1
        or mapping["scope_slice"] != SCOPE_SLICE
        or type(mapping["registrations"]) is not tuple
    ):
        raise ValueError("broker config identity is incompatible")
    _validate_installed_executable(mapping["gate"], "gate")
    _validate_installed_executable(mapping["anchor"], "anchor")
    probe = _exact_mapping(mapping["startup_probe"], _STARTUP_PROBE_FIELDS, "startup_probe")
    path = _absolute_path(probe["canonical_path"], "startup probe path")
    if path != _STARTUP_PROBE_PATH:
        raise ValueError("startup probe path is not fixed")
    digest = _sha256(probe["sha256"], "startup probe sha256")
    account = probe["account_name"]
    if type(account) is not str or account != "mif-containment-probe":
        raise ValueError("startup probe account is not fixed")
    if probe["root_owned"] is not True:
        raise ValueError("startup probe must be root-owned")
    return StartupCertificationConfig(
        startup_probe_path=path,
        startup_probe_sha256=digest,
        startup_probe_account=account,
        startup_probe_root_owned=True,
        graceful_stop_seconds=_positive_number(
            mapping["graceful_stop_seconds"], "graceful_stop_seconds"
        ),
        maximum_runtime_seconds=_positive_number(
            mapping["maximum_runtime_seconds"], "maximum_runtime_seconds"
        ),
        startup_certification_timeout_seconds=_positive_number(
            mapping["startup_certification_timeout_seconds"],
            "startup_certification_timeout_seconds",
        ),
        startup_reconciliation_timeout_seconds=_positive_number(
            mapping["startup_reconciliation_timeout_seconds"],
            "startup_reconciliation_timeout_seconds",
        ),
        startup_reconciliation_maximum_records=_positive_integer(
            mapping["startup_reconciliation_maximum_records"],
            "startup_reconciliation_maximum_records",
        ),
        startup_notify_barrier_timeout_seconds=_positive_number(
            mapping["startup_notify_barrier_timeout_seconds"],
            "startup_notify_barrier_timeout_seconds",
        ),
    )


async def _certify_and_notify(
    *,
    manager: object,
    launcher: object,
    binding_observer: object,
    bindings: object,
    scope_policy: StartupCertificationScopePolicy,
    deadlines: object,
    epoch_watch: object,
    epoch_snapshot: object,
    notifier: object,
    barrier_timeout_seconds: float,
) -> StartupCertificationRunnerStatus:
    """Delegate proof to , then close readiness without exporting proof."""

    _assert_native_service_boundary()
    certificate = await certify_broker_startup(
        manager=manager,
        launcher=launcher,
        binding_observer=binding_observer,
        bindings=bindings,
        scope_policy=scope_policy,
        deadlines=deadlines,
        epoch_watch=epoch_watch,
        epoch_snapshot=epoch_snapshot,
    )
    await _observe_epoch_bounded(
        certificate,
        manager=manager,
        binding_observer=binding_observer,
        epoch_watch=epoch_watch,
        epoch_snapshot=epoch_snapshot,
        timeout_seconds=barrier_timeout_seconds,
    )
    proof = getattr(getattr(certificate, "drain_proof", None), "proof_kind", None)
    try:
        status = StartupCertificationRunnerStatus(
            result="passed",
            drain_proof_kind=proof,
        )
    except ValueError as error:
        raise StartupCertificationRunnerError("certificate result is incomplete") from error
    ready = False
    try:
        notifier.ready(status)
        ready = True
        await notifier.barrier(barrier_timeout_seconds)
        await _observe_epoch_bounded(
            certificate,
            manager=manager,
            binding_observer=binding_observer,
            epoch_watch=epoch_watch,
            epoch_snapshot=epoch_snapshot,
            timeout_seconds=barrier_timeout_seconds,
        )
    except BaseException as error:
        if ready:
            try:
                notifier.stopping("mif startup certification closure failed")
            except BaseException:
                pass
        raise StartupCertificationRunnerError("startup readiness closure was incomplete") from error
    return status


async def _run_fixed_certification() -> StartupCertificationRunnerStatus:
    if os.geteuid() != 0:
        raise StartupCertificationRunnerError("startup certification runner requires root")
    config_asset, raw_config = _read_root_asset(CONFIG_PATH, _MAXIMUM_CONFIG_BYTES)
    config = parse_startup_certification_config(raw_config)
    probe_asset, _unused = _read_root_asset(config.startup_probe_path, 268_435_456)
    if probe_asset.sha256 != config.startup_probe_sha256:
        raise StartupCertificationRunnerError("startup probe digest differs from config")
    manager = DbusFastSystemdManager()
    epoch_watch = await manager.install_epoch_watch()
    epoch_snapshot = await manager.read_epoch_snapshot(epoch_watch)
    broker_pid = os.getpid()
    broker_start_token = _process_start_token(broker_pid)
    probe_account = _probe_account(config.startup_probe_account)
    observer = ProductionStartupBindingObserver(
        config_asset=config_asset,
        probe_asset=probe_asset,
        scope_policy=config.scope_policy(),
        startup_probe_account=config.startup_probe_account,
        startup_probe_uid=probe_account.pw_uid,
        startup_probe_gid=probe_account.pw_gid,
    )
    deadlines = config.deadlines()
    template_request = _scope_template_request(config.scope_policy())
    bindings = StartupCertificationBindings(
        broker_process_id=broker_pid,
        broker_process_start_token=broker_start_token,
        broker_build_id=RUNNER_BUILD_ID,
        broker_config_sha256=config_asset.sha256,
        scope_policy_sha256=scope_policy_sha256_for_request(template_request),
        scope_request_template_sha256=scope_request_template_sha256(template_request),
        startup_probe_path=config.startup_probe_path,
        startup_probe_sha256=config.startup_probe_sha256,
        startup_probe_account=config.startup_probe_account,
        boot_id=epoch_snapshot.boot_id,
        kernel_release=os.uname().release,
        system_bus_id=epoch_snapshot.system_bus_id,
        systemd_unique_owner=epoch_snapshot.systemd_unique_owner,
        units_load_timestamp_monotonic=epoch_snapshot.units_load_timestamp_monotonic,
        pid1_start_token=epoch_snapshot.pid1_start_token,
        pid1_executable_sha256=epoch_snapshot.pid1_executable_sha256,
        cgroup_mount_device=epoch_snapshot.cgroup_mount_device,
        cgroup_mount_inode=epoch_snapshot.cgroup_mount_inode,
    )
    return await _certify_and_notify(
        manager=manager,
        launcher=PosixStartupProbeLauncher(),
        binding_observer=observer,
        bindings=bindings,
        scope_policy=config.scope_policy(),
        deadlines=deadlines,
        epoch_watch=epoch_watch,
        epoch_snapshot=epoch_snapshot,
        notifier=SystemdNotifier.from_environment(),
        barrier_timeout_seconds=config.startup_notify_barrier_timeout_seconds,
    )


async def _observe_epoch_bounded(
    certificate: object,
    *,
    manager: object,
    binding_observer: object,
    epoch_watch: object,
    epoch_snapshot: object,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(
            observe_certification_epoch(
                certificate,
                manager=manager,
                binding_observer=binding_observer,
                epoch_watch=epoch_watch,
                epoch_snapshot=epoch_snapshot,
            ),
            timeout=float(timeout_seconds),
        )
    except TimeoutError as error:
        raise StartupCertificationRunnerError(
            "startup epoch observation deadline expired"
        ) from error


def _scope_template_request(
    policy: StartupCertificationScopePolicy,
) -> ScopeStartRequest:
    return ScopeStartRequest(
        unit_name=_TEMPLATE_UNIT,
        description=_CERTIFICATION_DESCRIPTION,
        gate_pid=1,
        anchor_pid=2,
        timeout_stop_usec=int(policy.graceful_stop_seconds * 1_000_000),
        runtime_max_usec=int(policy.maximum_runtime_seconds * 1_000_000),
    )


def startup_certification_main() -> int:
    """Run the fixed service. No argument or protocol-request surface exists."""

    if len(sys.argv) != 1:
        return STARTUP_CERTIFICATION_FAILURE
    try:
        asyncio.run(_run_fixed_certification())
    except BaseException as error:
        try:
            sys.stderr.write(_failure_diagnostic_line(error))
        except BaseException:
            pass
        return STARTUP_CERTIFICATION_FAILURE
    return 0


def _failure_diagnostic_line(error: BaseException) -> str:
    exception_class = _one_line_printable(type(error).__name__)
    try:
        message = _one_line_printable(str(error))
    except BaseException:
        message = "message unavailable"
    content = f"{_FAILURE_DIAGNOSTIC_PREFIX}{exception_class}: {message}"
    maximum_content_bytes = _MAXIMUM_FAILURE_DIAGNOSTIC_BYTES - 1
    encoded = content.encode("utf-8", errors="replace")[:maximum_content_bytes]
    return encoded.decode("utf-8", errors="ignore") + "\n"


def _one_line_printable(value: str) -> str:
    return "".join(character if character.isprintable() else "?" for character in value)


def _assert_native_service_boundary() -> None:
    status = _proc_status()
    expected = f"{_EXPECTED_SERVICE_CAPABILITY_MASK:016x}"
    if (
        status.get("CapInh") != "0000000000000000"
        or status.get("CapAmb") != "0000000000000000"
        or any(status.get(name) != expected for name in ("CapPrm", "CapEff", "CapBnd"))
    ):
        raise StartupCertificationRunnerError("service capability sets are not exact")
    _assert_forbidden_process_memory_syscalls()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        if error.errno not in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES):
            raise StartupCertificationRunnerError("network denial is ambiguous") from error
    else:
        probe.close()
        raise StartupCertificationRunnerError("IP network access is available")
    project_probe = f"/home/.mif-containment-write-probe-{os.getpid()}"
    try:
        descriptor = os.open(
            project_probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as error:
        if error.errno not in (errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOENT):
            raise StartupCertificationRunnerError("project write denial is ambiguous") from error
    else:
        os.close(descriptor)
        os.unlink(project_probe)
        raise StartupCertificationRunnerError("project filesystem is writable")


def _assert_forbidden_process_memory_syscalls() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    if libc.ptrace(0, 0, None, None) != -1 or ctypes.get_errno() not in (
        errno.EPERM,
        errno.EACCES,
        errno.ENOSYS,
    ):
        raise StartupCertificationRunnerError("ptrace syscall is not blocked")
    local = ctypes.c_char(b"x")

    class IOVec(ctypes.Structure):
        _fields_ = (("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t))

    vector = IOVec(ctypes.addressof(local), 1)
    for name in ("process_vm_readv", "process_vm_writev"):
        operation = getattr(libc, name, None)
        if operation is None:
            raise StartupCertificationRunnerError(f"{name} libc wrapper is unavailable")
        ctypes.set_errno(0)
        result = operation(
            os.getpid(),
            ctypes.byref(vector),
            1,
            ctypes.byref(vector),
            1,
            0,
        )
        if result != -1 or ctypes.get_errno() not in (errno.EPERM, errno.EACCES, errno.ENOSYS):
            raise StartupCertificationRunnerError(f"{name} syscall is not blocked")


def _proc_status() -> dict[str, str]:
    content = Path("/proc/self/status").read_text(encoding="ascii")
    return {
        key: value.strip()
        for line in content.splitlines()
        if ":" in line
        for key, value in (line.split(":", 1),)
    }


def _probe_account(name: str):
    try:
        account = pwd.getpwnam(name)
    except KeyError as error:
        raise StartupCertificationRunnerError("startup probe account is unavailable") from error
    if (
        account.pw_uid < 1
        or account.pw_gid < 1
        or account.pw_dir != "/nonexistent"
        or account.pw_shell != "/usr/sbin/nologin"
        or set(os.getgrouplist(name, account.pw_gid)) != {account.pw_gid}
        or not _shadow_password_is_locked(name)
    ):
        raise StartupCertificationRunnerError("startup probe account is not locked down")
    return account


def _shadow_password_is_locked(name: str) -> bool:
    descriptor = os.open(
        "/etc/shadow",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != 0
            or information.st_mode & 0o022
        ):
            raise StartupCertificationRunnerError("shadow account source is unsafe")
        content = bytearray()
        while len(content) <= 1_048_576:
            chunk = os.read(descriptor, 1_048_577 - len(content))
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(descriptor)
    if len(content) > 1_048_576:
        raise StartupCertificationRunnerError("shadow account source exceeds its bound")
    try:
        matches = [
            fields[1]
            for line in bytes(content).decode("utf-8", errors="strict").splitlines()
            if len(fields := line.split(":")) >= 2 and fields[0] == name
        ]
    except UnicodeDecodeError as error:
        raise StartupCertificationRunnerError("shadow account source is malformed") from error
    return len(matches) == 1 and matches[0].startswith(("!", "*"))


def _read_root_asset(path: str, maximum: int) -> tuple[_RootAssetIdentity, bytes]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != 0
            or information.st_gid != 0
            or information.st_mode & 0o022
        ):
            raise StartupCertificationRunnerError("installed asset ownership is unsafe")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, maximum - len(content) + 1)
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(descriptor)
    if len(content) > maximum:
        raise StartupCertificationRunnerError("installed asset exceeds its byte bound")
    raw = bytes(content)
    return (
        _RootAssetIdentity(
            path=path,
            device=information.st_dev,
            inode=information.st_ino,
            size=information.st_size,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
        raw,
    )


def _assert_same_root_asset(expected: _RootAssetIdentity) -> None:
    actual, _raw = _read_root_asset(expected.path, max(expected.size, 1))
    if actual != expected:
        raise StartupCertificationRunnerError("installed startup asset changed")


def _process_start_token(pid: int) -> str:
    content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = content.rfind(") ")
    fields = content[close + 2 :].split() if close > 0 else ()
    if len(fields) <= 19 or not fields[19].isdigit() or int(fields[19]) < 1:
        raise StartupCertificationRunnerError("runner process identity is malformed")
    return f"linux-proc-stat/v1:{pid}:{fields[19]}"


def _exact_mapping(value: object, keys: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        raise ValueError(f"{label} fields are not exact")
    return value


def _validate_installed_executable(value: object, label: str) -> None:
    mapping = _exact_mapping(value, _INSTALLED_EXECUTABLE_FIELDS, label)
    _absolute_path(mapping["canonical_path"], f"{label} path")
    _sha256(mapping["sha256"], f"{label} sha256")
    if mapping["root_owned"] is not True:
        raise ValueError(f"{label} must be root-owned")


def _absolute_path(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("/")
        or value == "/"
        or os.path.normpath(value) != value
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be a canonical absolute path")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is malformed")
    return value


def _positive_number(value: object, label: str) -> float:
    if type(value) not in (int, float) or value <= 0 or not float(value) < float("inf"):
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


if __name__ == "__main__":
    raise SystemExit(startup_certification_main())
