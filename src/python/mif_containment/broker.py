"""Peer-authenticated, fail-closed containment broker state machine.

The module exposes injectable host boundaries. The privileged service owns ordering,
authentication, serialization, certificate fencing and per-authority concurrency; Linux
process and systemd effects remain behind narrow ports so the state machine can be
pressure-tested without root access.

The `BrokerConfig` record carries `protocol_min`, `protocol_max`, `state_root`,
`socket_group`, `scope_slice` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import asyncio
import array
import ctypes
import errno
import grp
import inspect
import hashlib
import math
import os
import posixpath
import pwd
import re
import resource
import select
import signal
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Awaitable, Callable, Iterable, Mapping, Protocol

from .codec import dumps_canonical, loads_canonical_packet
from .protocol import (
    ANCHOR_PROMOTION_CAPABILITY,
    COLLECT_MODE_CAPABILITY,
    DESCRIPTOR_EXEC_CAPABILITY,
    DRAIN_PROOF_CAPABILITY,
    LEDGER_RECORD_SCHEMAS,
    MAXIMUM_ARGV_COUNT,
    MAXIMUM_ENVIRONMENT_BYTES,
    MAXIMUM_ENVIRONMENT_COUNT,
    AttachPayloadV1,
    BrokerAttachmentRecordV1,
    BrokerAttemptRecordV1,
    BrokerExecutableRegistrationV1,
    BrokerDrainRecordV1,
    BrokerOperation,
    BrokerReleaseIntentRecordV1,
    BrokerReleaseRecordV1,
    BrokerRequestV1,
    BrokerSecurityProfileV1,
    BrokerStopIntentRecordV1,
    CgroupDeletedAfterStopProofV1,
    CgroupEventsZeroProofV1,
    ContainmentInspection,
    ContainmentSemanticState,
    GateExecAuthorizationV1,
    GateAuthorizeExecPayloadV1,
    LinuxSystemdScopeAttachmentV1,
    LinuxSystemdScopeDrainV1,
    MachineRebootProofV1,
    PROTOCOL_VERSION,
    PROVIDER,
    ReleaseReceiptV1,
    ScopeAnchorIdentityV1,
    SystemdStopJobEvidenceV1,
)
from .scope_anchor import PROMOTION_FD, PROMOTION_TOKEN, encode_bootstrap_frame
from .ledger import (
    ContainmentLedger,
    ContainmentLedgerConflictError,
    ContainmentLedgerIncompatibleError,
    ContainmentLedgerInventory,
    ContainmentLedgerInventoryEntry,
    ContainmentLedgerState,
    ContainmentRequestReservationV1,
    ContainmentRequestReservationSlot,
)
from .startup_certification import (
    BrokerStartupCertification,
    PosixStartupProbeLauncher,
    StartupCertificationBindings,
    StartupCertificationDeadlines,
    StartupCertificationScopePolicy,
    broker_capabilities_from_certification,
    certify_broker_startup,
    observe_certification_epoch,
    scope_policy_sha256_for_request,
    scope_request_template_sha256,
)
from .systemd_adapter import (
    CgroupVerificationError,
    SCOPE_SLICE,
    DbusFastSystemdManager,
    ScopeStartRequest,
    SystemdUnitNotFound,
    SystemdVerificationError,
)

MAXIMUM_CONFIG_BYTES = 65_536
MAXIMUM_REQUEST_BYTES = 65_536
MAXIMUM_ERROR_BYTES = 512
MAXIMUM_TIMEOUT_SECONDS = 86_400.0
MAXIMUM_RECONCILIATION_RECORDS = 1_000_000
BROKER_BUILD_ID = "mif-containment-peer-broker/v1"
SYSTEMD_LISTEN_FD = 3
SYSTEMD_LISTEN_FD_NAME = "mif-containment"
BROKER_CONFIG_PATH = "/etc/mif-pipeline/containment-broker.json"
BROKER_SOCKET_PATH = "/run/mif-pipeline/containment.sock"

_TOP_LEVEL_FIELDS = frozenset(
    {
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
    }
)
_INSTALLED_FIELDS = frozenset({"canonical_path", "sha256", "root_owned"})
_PROBE_FIELDS = _INSTALLED_FIELDS | {"account_name"}
_REGISTRATION_FIELDS = frozenset(
    {
        "registration_id",
        "canonical_qupath_executable",
        "executable_sha256",
        "allowed_command_profiles",
        "allowed_environment_names",
    }
)
_COMMAND_PROFILES = frozenset({"headless-script", "interactive-project"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_GROUP = re.compile(r"[a-z_][a-z0-9_-]{0,63}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_DANGEROUS_ENVIRONMENT = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_DEBUG",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "CLASSPATH",
    }
)
_DANGEROUS_PREFIXES = ("LD_", "DYLD_")


# ==============================================================================
#                                     Broker
# ==============================================================================


class BrokerError(RuntimeError):
    """Base class for bounded, fail-closed broker failures."""


class BrokerConfigurationError(BrokerError):
    """Installed broker configuration or asset identity is unsafe."""


class BrokerAuthenticationError(BrokerError):
    """Kernel peer evidence and the local account database do not agree."""


class BrokerRejectedError(BrokerError):
    """A request is malformed, unauthorized, or outside the closed protocol."""


class BrokerRevokedError(BrokerError):
    """The immutable startup certificate has been permanently revoked."""


class BrokerConflictError(BrokerError):
    """An occupied immutable request slot contains another request."""


class BrokerAmbiguousError(BrokerError):
    """Authority exists but cannot be safely classified or mutated."""


class BrokerIncompatibleError(BrokerError):
    """The caller and installed broker have incompatible closed contracts."""


@dataclass(frozen=True, slots=True)
class InstalledFileIdentity:
    canonical_path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _require_canonical_path(self.canonical_path, "installed file path")
        for name in ("device", "inode", "size"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.mode) is not int:
            raise ValueError("mode must be an integer")
        if type(self.uid) is not int or self.uid < 0 or type(self.gid) is not int or self.gid < 0:
            raise ValueError("uid and gid must be non-negative integers")
        if type(self.nlink) is not int or self.nlink < 1:
            raise ValueError("nlink must be positive")
        _require_sha256(self.sha256, "installed file digest")


@dataclass(frozen=True, slots=True)
class InstalledExecutable:
    canonical_path: str
    sha256: str
    identity: InstalledFileIdentity
    account_name: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutableRegistration:
    registration_id: str
    canonical_path: str
    sha256: str
    command_profiles: tuple[str, ...]
    environment_allowlist: tuple[str, ...]
    identity: InstalledFileIdentity


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    protocol_min: int
    protocol_max: int
    state_root: str
    socket_group: str
    scope_slice: str
    graceful_stop_seconds: float
    maximum_runtime_seconds: float
    maximum_request_bytes: int
    maximum_argument_count: int
    maximum_environment_entries: int
    maximum_environment_bytes: int
    startup_certification_timeout_seconds: float
    startup_reconciliation_timeout_seconds: float
    startup_reconciliation_maximum_records: int
    startup_notify_barrier_timeout_seconds: float
    gate: InstalledExecutable
    anchor: InstalledExecutable
    startup_probe: InstalledExecutable
    registrations: tuple[ExecutableRegistration, ...]
    config_identity: InstalledFileIdentity
    config_sha256: str

    def scope_policy(self) -> StartupCertificationScopePolicy:
        return StartupCertificationScopePolicy(
            graceful_stop_seconds=self.graceful_stop_seconds,
            maximum_runtime_seconds=self.maximum_runtime_seconds,
        )

    def certification_deadlines(self) -> StartupCertificationDeadlines:
        total = self.startup_certification_timeout_seconds
        step = min(total, 10.0)
        return StartupCertificationDeadlines(
            total_seconds=total,
            probe_ready_seconds=step,
            start_job_seconds=step,
            stop_job_seconds=step,
            cleanup_seconds=step,
        )


def _broker_security_profile(config: BrokerConfig) -> BrokerSecurityProfileV1:
    registrations = tuple(
        BrokerExecutableRegistrationV1(
            registration_id=item.registration_id,
            canonical_path=item.canonical_path,
            target_executable_sha256=item.sha256,
            allowed_command_profiles=item.command_profiles,
            allowed_environment_names=item.environment_allowlist,
        )
        for item in sorted(
            config.registrations,
            key=lambda item: item.registration_id,
        )
    )
    return BrokerSecurityProfileV1(
        ledger_record_schemas=LEDGER_RECORD_SCHEMAS,
        descriptor_exec=DESCRIPTOR_EXEC_CAPABILITY,
        anchor_promotion=ANCHOR_PROMOTION_CAPABILITY,
        collect_mode=COLLECT_MODE_CAPABILITY,
        drain_proof=DRAIN_PROOF_CAPABILITY,
        registrations=registrations,
    )


def _configured_scope_policy_sha256(config: BrokerConfig) -> str:
    template = ScopeStartRequest(
        unit_name="mif-qupath-cert-00000000000000000000000000000000.scope",
        description="mIF containment startup certification",
        gate_pid=1,
        anchor_pid=2,
        timeout_stop_usec=_seconds_to_usec(config.graceful_stop_seconds),
        runtime_max_usec=_seconds_to_usec(config.maximum_runtime_seconds),
    )
    return scope_policy_sha256_for_request(template)


def _capabilities_for_retained_certificate(
    config: object,
    certification: object,
) -> dict[str, object]:
    if (
        not isinstance(config, BrokerConfig)
        or not isinstance(certification, BrokerStartupCertification)
        or config.config_sha256 != certification.broker_config_sha256
        or _configured_scope_policy_sha256(config) != certification.scope_policy_sha256
    ):
        raise BrokerIncompatibleError("retained broker configuration conflicts with certification")
    return broker_capabilities_from_certification(
        certification,
        security_profile=_broker_security_profile(config),
    ).to_mapping()


@dataclass(frozen=True, slots=True)
class PasswdAccount:
    name: str
    uid: int
    gid: int
    home: str
    primary_group_name: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _GROUP.fullmatch(self.name) is None:
            raise ValueError("account name is unsafe")
        if type(self.uid) is not int or self.uid < 0 or type(self.gid) is not int or self.gid < 0:
            raise ValueError("account UID/GID is invalid")
        _require_canonical_path(self.home, "account home")
        if (
            type(self.primary_group_name) is not str
            or _GROUP.fullmatch(self.primary_group_name) is None
        ):
            raise ValueError("primary group name is unsafe")


@dataclass(frozen=True, slots=True)
class ActivatedSocketIdentity:
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int


@dataclass(frozen=True, slots=True)
class ProbeAccountIdentity:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    primary_group_name: str
    primary_group_members: tuple[str, ...]
    shadow_lock: str


@dataclass(frozen=True, slots=True)
class SocketGroupIdentity:
    name: str
    gid: int
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.name) is not str or _GROUP.fullmatch(self.name) is None:
            raise ValueError("socket group name is unsafe")
        if type(self.gid) is not int or self.gid < 0:
            raise ValueError("socket group gid is invalid")
        if (
            type(self.members) is not tuple
            or tuple(sorted(set(self.members))) != self.members
            or any(type(item) is not str or _GROUP.fullmatch(item) is None for item in self.members)
        ):
            raise ValueError("socket group members are not canonical")


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    pid: int
    uid: int
    gid: int
    start_token: str
    account: PasswdAccount

    def __post_init__(self) -> None:
        if (
            type(self.pid) is not int
            or self.pid < 1
            or type(self.uid) is not int
            or self.uid < 0
            or type(self.gid) is not int
            or self.gid < 0
            or type(self.start_token) is not str
            or not self.start_token.isascii()
            or not self.start_token.isdigit()
            or int(self.start_token, 10) < 1
            or not isinstance(self.account, PasswdAccount)
        ):
            raise ValueError("peer identity is malformed")


@dataclass(frozen=True, slots=True)
class _RecoveryPeer:
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class AnchorLaunchRequest:
    path: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    cwd: str
    stdin_path: str
    stdout_path: str
    stderr_path: str
    promotion_fd: int
    promotion_read_only: bool
    close_fds: bool
    resuid: tuple[int, int, int]
    resgid: tuple[int, int, int]
    supplementary_groups: tuple[int, ...]
    bootstrap_frame: bytes
    executable_identity: InstalledFileIdentity
    broker_pid: int
    broker_start_token: str


@dataclass(frozen=True, slots=True)
class AnchorHandle:
    identity: object
    pidfd: int
    promotion_write_fd: int


@dataclass(frozen=True, slots=True)
class GateAttachmentEvidence:
    gate_ready: object
    gate_pidfd: int
    registration_id: str
    retained_target_device: int
    retained_target_inode: int
    retained_target_sha256: str
    gate_phase: str = "stopped-gate"


@dataclass(slots=True)
class BoundDrainObserver:
    attachment: object
    verified_cgroup: object
    closed: bool = False


@dataclass(slots=True)
class AuthorityPidfds:
    gate: int | None
    anchor: int | None


class ReconciledAttachmentDisposition(str, Enum):
    """Exact lifecycle fact returned after identity-bound restart inspection."""

    LIVE = "live"
    LEADER_EXITED = "leader_exited"
    ANCHOR_LOST = "anchor_lost"


@dataclass(frozen=True, slots=True)
class ReconciledAttachmentObservation:
    disposition: ReconciledAttachmentDisposition
    gate_phase: str

    def __post_init__(self) -> None:
        if self.gate_phase not in ("stopped-gate", "running-gate", "absent-gate"):
            raise ValueError("reconciled gate phase is invalid")


def _assert_reconciled_member_set(
    attachment: object,
    identity: object,
    disposition: ReconciledAttachmentDisposition,
    gate_phase: str,
) -> None:
    members = frozenset(getattr(identity, "pids", ()))
    gate_pid = attachment.gate_pid
    anchor_pid = attachment.anchor_identity.anchor_pid
    expected = frozenset((gate_pid, anchor_pid))
    if gate_phase == "release-intent":
        if disposition is not ReconciledAttachmentDisposition.LIVE or members != expected:
            raise BrokerRejectedError("pre-exec unit process membership changed")
        return
    if disposition is ReconciledAttachmentDisposition.LIVE:
        valid = expected <= members if gate_phase == "target" else members == expected
        if not valid:
            raise BrokerRejectedError("live unit process membership changed")
    elif disposition is ReconciledAttachmentDisposition.LEADER_EXITED:
        if anchor_pid not in members or gate_pid in members:
            raise BrokerRejectedError("leader-exit unit membership is inconsistent")


class ScopeAnchorLauncher(Protocol):
    async def launch_anchor(self, request: AnchorLaunchRequest) -> AnchorHandle: ...


class ContainmentAuthorityRuntime(ScopeAnchorLauncher, Protocol):
    """Narrow pidfd/process/cgroup boundary used by authority transitions."""

    async def inspect_gate_for_attach(
        self, peer, payload, target_fd, config
    ) -> GateAttachmentEvidence: ...

    async def validate_anchor(self, peer, handle, phase: str): ...

    async def verify_stopped_processes(self, gate, anchor, phase: str) -> None: ...

    async def verify_scope(self, manager, request): ...

    async def promote_anchor(self, handle: AnchorHandle) -> None: ...

    async def close_anchor_promotion(self, handle: AnchorHandle) -> None: ...

    async def signal_pidfd(self, pidfd: int, signum: int) -> None: ...

    async def validate_anchor_descriptors(self, handle: AnchorHandle) -> None: ...

    async def close_target_fd(self, target_fd: int) -> None: ...

    async def gate_pidfd(self, attachment): ...

    async def anchor_pidfd(self, attachment): ...

    async def await_gate_exit(self, attachment, timeout_seconds: float) -> None: ...

    async def await_pidfd_exit(self, pidfd: int, timeout_seconds: float) -> None: ...

    async def close_pidfd(self, pidfd: int) -> None: ...

    async def await_drain_proof(self, attachment, timeout_seconds: float): ...

    async def bind_drain_observer(self, attachment) -> BoundDrainObserver: ...

    async def await_bound_drain_proof(
        self, observer: BoundDrainObserver, timeout_seconds: float
    ): ...

    async def close_drain_observer(self, observer: BoundDrainObserver) -> None: ...

    async def original_owner_is_live(self, pid: int, start_token: str) -> bool: ...

    async def verify_reconciled_attachment(
        self,
        attachment,
        identity,
        *,
        attempt=None,
        gate_phase: str = "target",
    ) -> ReconciledAttachmentDisposition: ...

    async def prove_reconciled_drain(self, attachment): ...


@dataclass(frozen=True, slots=True)
class BrokerReply:
    request_id: str
    status: str
    payload: object | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or not self.request_id
            or len(self.request_id.encode("utf-8")) > 128
        ):
            raise ValueError("reply request_id is invalid")
        if self.status not in (
            "ok",
            "live",
            "drained",
            "ambiguous",
            "rejected",
            "conflict",
            "incompatible",
        ):
            raise ValueError("reply status is outside the closed union")
        if self.error is not None and (
            type(self.error) is not str or len(self.error.encode("utf-8")) > MAXIMUM_ERROR_BYTES
        ):
            raise ValueError("reply error exceeds its UTF-8 bound")

    def canonical_bytes(self) -> bytes:
        packet = dumps_canonical(
            {
                "error": self.error,
                "payload": _wire_value(self.payload),
                "request_id": self.request_id,
                "schema_name": "mif-containment-broker-response",
                "schema_version": 1,
                "status": self.status,
            }
        )
        if len(packet) > MAXIMUM_REQUEST_BYTES:
            raise BrokerRejectedError("broker reply exceeds the maximum packet size")
        return packet


class BrokerNotifier(Protocol):
    def notify(self, state: str) -> None: ...

    async def barrier(self, timeout_seconds: float) -> None: ...


class BrokerTransport(Protocol):
    def bind_activated_socket(self, config: BrokerConfig) -> None: ...

    async def receive_one(self) -> tuple[PeerIdentity, bytes, tuple[int, ...]]: ...

    async def send(self, reply: BrokerReply) -> None: ...


class StartupReconciler(Protocol):
    async def reconcile(self, *, deadline_seconds: float, maximum_records: int) -> None: ...


class BrokerOperationBackend(Protocol):
    async def capabilities(self, context: "BrokerContext") -> object: ...

    async def attach(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object, target_fd: int
    ) -> object: ...

    async def release_intent(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> bool: ...

    async def release_resume(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> None: ...

    async def release_wait(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...

    async def gate_authorize_exec(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...

    async def inspect(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...

    async def await_drain(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...

    async def stop(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...

    async def recover_stop(
        self, context: "BrokerContext", peer: PeerIdentity, payload: object
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class BrokerContext:
    config: BrokerConfig
    certification: object
    manager: object
    authenticated_request: BrokerRequestV1 | None = None
    canonical_request_bytes: bytes | None = None


def parse_broker_config(
    raw: bytes,
    *,
    config_identity: InstalledFileIdentity,
    installed_identities: Mapping[str, InstalledFileIdentity],
) -> BrokerConfig:
    """Validate one exact canonical config and its complete installed files."""

    try:
        if type(raw) is not bytes or not raw or len(raw) > MAXIMUM_CONFIG_BYTES:
            raise ValueError("configuration size is invalid")
        mapping = loads_canonical_packet(raw)
        if dumps_canonical(mapping) != raw:
            raise ValueError("configuration bytes are not canonical")
        if not isinstance(mapping, Mapping) or frozenset(mapping) != _TOP_LEVEL_FIELDS:
            raise ValueError("configuration fields are not the exact closed set")
        _validate_config_identity(config_identity, raw)
        if (
            mapping["schema_name"] != "mif-containment-broker-config"
            or mapping["schema_version"] != 1
        ):
            raise ValueError("configuration schema is incompatible")
        if (
            mapping["protocol_min"] != PROTOCOL_VERSION
            or mapping["protocol_max"] != PROTOCOL_VERSION
        ):
            raise ValueError("configuration protocol range must be exactly version one")
        state_root = _mapping_text(mapping, "state_root")
        _require_canonical_path(state_root, "state_root")
        socket_group = _mapping_text(mapping, "socket_group")
        if _GROUP.fullmatch(socket_group) is None:
            raise ValueError("socket_group is unsafe")
        if mapping["scope_slice"] != SCOPE_SLICE:
            raise ValueError("scope_slice is not the fixed containment slice")
        graceful = _positive_number(mapping, "graceful_stop_seconds")
        maximum_runtime = _positive_number(mapping, "maximum_runtime_seconds")
        maximum_request = _bounded_integer(mapping, "maximum_request_bytes", MAXIMUM_REQUEST_BYTES)
        maximum_arguments = _bounded_integer(mapping, "maximum_argument_count", MAXIMUM_ARGV_COUNT)
        maximum_environment_entries = _bounded_integer(
            mapping, "maximum_environment_entries", MAXIMUM_ENVIRONMENT_COUNT
        )
        maximum_environment_bytes = _bounded_integer(
            mapping, "maximum_environment_bytes", MAXIMUM_ENVIRONMENT_BYTES
        )
        certification_timeout = _positive_number(mapping, "startup_certification_timeout_seconds")
        reconciliation_timeout = _positive_number(mapping, "startup_reconciliation_timeout_seconds")
        reconciliation_records = _bounded_integer(
            mapping,
            "startup_reconciliation_maximum_records",
            MAXIMUM_RECONCILIATION_RECORDS,
        )
        barrier_timeout = _positive_number(mapping, "startup_notify_barrier_timeout_seconds")
        gate = _installed_executable(mapping["gate"], _INSTALLED_FIELDS, installed_identities)
        anchor = _installed_executable(mapping["anchor"], _INSTALLED_FIELDS, installed_identities)
        probe = _installed_executable(
            mapping["startup_probe"], _PROBE_FIELDS, installed_identities, probe=True
        )
        if probe.account_name != "mif-containment-probe":
            raise ValueError("startup probe account is not the fixed locked account")
        registrations_raw = mapping["registrations"]
        if type(registrations_raw) is not tuple:
            raise ValueError("registrations must be an array")
        registrations = tuple(
            _registration(value, installed_identities) for value in registrations_raw
        )
        registration_ids = tuple(value.registration_id for value in registrations)
        if len(registration_ids) != len(set(registration_ids)):
            raise ValueError("registration identifiers must be unique")
        paths = (gate.canonical_path, anchor.canonical_path, probe.canonical_path) + tuple(
            value.canonical_path for value in registrations
        )
        if len(paths) != len(set(paths)):
            raise ValueError("gate, anchor, probe, and registered paths must be distinct")
        return BrokerConfig(
            protocol_min=1,
            protocol_max=1,
            state_root=state_root,
            socket_group=socket_group,
            scope_slice=SCOPE_SLICE,
            graceful_stop_seconds=graceful,
            maximum_runtime_seconds=maximum_runtime,
            maximum_request_bytes=maximum_request,
            maximum_argument_count=maximum_arguments,
            maximum_environment_entries=maximum_environment_entries,
            maximum_environment_bytes=maximum_environment_bytes,
            startup_certification_timeout_seconds=certification_timeout,
            startup_reconciliation_timeout_seconds=reconciliation_timeout,
            startup_reconciliation_maximum_records=reconciliation_records,
            startup_notify_barrier_timeout_seconds=barrier_timeout,
            gate=gate,
            anchor=anchor,
            startup_probe=probe,
            registrations=registrations,
            config_identity=config_identity,
            config_sha256=hashlib.sha256(raw).hexdigest(),
        )
    except BrokerConfigurationError:
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise BrokerConfigurationError("broker configuration is malformed or unsafe") from error


def authenticate_peer(
    connection: object,
    *,
    account_lookup: Callable[[int], PasswdAccount],
    process_start_token: Callable[[int], str],
) -> PeerIdentity:
    """Authenticate solely from the accepted socket's kernel peer credentials."""

    try:
        raw = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        if type(raw) is not bytes or len(raw) != struct.calcsize("3i"):
            raise ValueError("SO_PEERCRED has the wrong size")
        pid, uid, gid = struct.unpack("3i", raw)
        if pid <= 0 or uid < 0 or gid < 0:
            raise ValueError("SO_PEERCRED is malformed")
        start_token = process_start_token(pid)
        account = account_lookup(uid)
        if not isinstance(account, PasswdAccount) or account.uid != uid or account.gid != gid:
            raise ValueError("effective GID differs from passwd primary GID")
        if type(start_token) is not str or not start_token:
            raise ValueError("peer process start identity is unavailable")
        return PeerIdentity(pid=pid, uid=uid, gid=gid, start_token=start_token, account=account)
    except BrokerAuthenticationError:
        raise
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise BrokerAuthenticationError(
            "kernel peer identity could not be authenticated"
        ) from error


def derive_user_environment(
    account: PasswdAccount,
    registration: ExecutableRegistration,
    requested: Mapping[str, str],
) -> dict[str, str]:
    """Build the sole allowed target environment from passwd and allowed names."""

    if not isinstance(account, PasswdAccount) or not isinstance(
        registration, ExecutableRegistration
    ):
        raise TypeError("exact account and registration records are required")
    if not isinstance(requested, Mapping):
        raise BrokerAuthenticationError("target environment is malformed")
    allowed = frozenset(registration.environment_allowlist)
    result: dict[str, str] = {}
    for name, value in requested.items():
        if (
            type(name) is not str
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            or name not in allowed
            or _environment_is_dangerous(name)
            or type(value) is not str
            or "\x00" in value
        ):
            raise BrokerAuthenticationError("target environment contains a forbidden entry")
        result[name] = value
    result.update(
        {
            "HOME": account.home,
            "USER": account.name,
            "LOGNAME": account.name,
            "UID": str(account.uid),
            "GID": str(account.gid),
            "GROUP": account.primary_group_name,
        }
    )
    return result


def _live_passwd_account(
    uid: int,
    expected_gid: int,
    *,
    passwd_by_uid: Callable[[int], object] | None = None,
    group_by_gid: Callable[[int], object] | None = None,
) -> PasswdAccount:
    """Observe one exact live passwd/group mapping as typed host evidence."""

    passwd_by_uid = passwd_by_uid or pwd.getpwuid
    group_by_gid = group_by_gid or grp.getgrgid
    try:
        account = passwd_by_uid(uid)
        if (
            type(account.pw_uid) is not int
            or account.pw_uid != uid
            or type(account.pw_gid) is not int
            or account.pw_gid != expected_gid
        ):
            raise ValueError("passwd numeric mapping changed")
        group = group_by_gid(expected_gid)
        if type(group.gr_gid) is not int or group.gr_gid != expected_gid:
            raise ValueError("group numeric mapping changed")
        return PasswdAccount(
            account.pw_name,
            account.pw_uid,
            account.pw_gid,
            account.pw_dir,
            group.gr_name,
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        raise BrokerAmbiguousError("live account database mapping is unavailable") from error


def derive_scope_unit_name(uid: int, launch_nonce_sha256: str) -> str:
    if type(uid) is not int or uid < 0:
        raise ValueError("uid must be a non-negative integer")
    _require_sha256(launch_nonce_sha256, "launch nonce")
    return f"mif-qupath-u{uid}-{launch_nonce_sha256[:32]}.scope"


def build_anchor_launch_request(
    config: BrokerConfig,
    account: PasswdAccount,
    *,
    broker_pid: int,
    broker_start_token: str,
) -> AnchorLaunchRequest:
    """Build the fixed credential-dropping anchor launch, never generic exec."""

    if not isinstance(config, BrokerConfig) or not isinstance(account, PasswdAccount):
        raise TypeError("broker config and passwd account are required")
    bootstrap = encode_bootstrap_frame(broker_pid, broker_start_token)
    credentials = (account.uid, account.uid, account.uid)
    group_credentials = (account.gid, account.gid, account.gid)
    return AnchorLaunchRequest(
        path=config.anchor.canonical_path,
        argv=(),
        environment=MappingProxyType({}),
        cwd="/",
        stdin_path="/dev/null",
        stdout_path="/dev/null",
        stderr_path="/dev/null",
        promotion_fd=PROMOTION_FD,
        promotion_read_only=True,
        close_fds=True,
        resuid=credentials,
        resgid=group_credentials,
        supplementary_groups=(),
        bootstrap_frame=bootstrap,
        executable_identity=config.anchor.identity,
        broker_pid=broker_pid,
        broker_start_token=broker_start_token,
    )


def safe_error_reply(
    *,
    request_id: str,
    authenticated_uid: int,
    authority_uid: int | None,
    error: BaseException,
) -> BrokerReply:
    if isinstance(error, (BrokerConflictError, ContainmentLedgerConflictError)):
        status = "conflict"
    elif isinstance(error, BrokerAmbiguousError):
        status = "ambiguous"
    elif isinstance(error, BrokerIncompatibleError):
        status = "incompatible"
    else:
        status = "rejected"
    if authority_uid is not None and authority_uid != authenticated_uid:
        message = "request denied; verify ownership and retry"
    else:
        message = "request rejected; verify request state and retry"
    return BrokerReply(request_id=request_id, status=status, error=message)


def validate_socket_activation(
    environment: Mapping[str, str],
    *,
    process_id: int,
    listener: object,
    socket_group_gid: int,
    descriptor_stat: Callable[[int], object] = os.fstat,
    path_stat: Callable[[str], object] = lambda path: os.stat(path, follow_symlinks=False),
) -> object:
    """Validate systemd's sole fixed AF_UNIX/SOCK_SEQPACKET descriptor."""

    try:
        if (
            not isinstance(environment, Mapping)
            or environment.get("LISTEN_PID") != str(process_id)
            or environment.get("LISTEN_FDS") != "1"
            or environment.get("LISTEN_FDNAMES") != SYSTEMD_LISTEN_FD_NAME
            or type(socket_group_gid) is not int
            or socket_group_gid < 0
            or listener.fileno() != SYSTEMD_LISTEN_FD
            or listener.family != socket.AF_UNIX
            or (listener.type & 0xF) != socket.SOCK_SEQPACKET
            or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
            or listener.getsockname() != BROKER_SOCKET_PATH
        ):
            raise ValueError("socket activation tuple is not exact")
        observed = descriptor_stat(SYSTEMD_LISTEN_FD)
        path_observed = path_stat(BROKER_SOCKET_PATH)
        if (
            not stat.S_ISSOCK(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_gid != socket_group_gid
            or stat.S_IMODE(observed.st_mode) != 0o660
            or observed.st_nlink != 1
            or path_observed.st_dev != observed.st_dev
            or path_observed.st_ino != observed.st_ino
            or path_observed.st_mode != observed.st_mode
            or path_observed.st_uid != observed.st_uid
            or path_observed.st_gid != observed.st_gid
            or path_observed.st_nlink != observed.st_nlink
        ):
            raise ValueError("activated socket identity or mode differs from policy")
        return listener
    except (AttributeError, TypeError, ValueError, OSError) as error:
        raise BrokerConfigurationError("systemd socket activation is malformed") from error


def _activated_socket_identity(
    listener: object,
    *,
    descriptor_stat: Callable[[int], object] = os.fstat,
    path_stat: Callable[[str], object] = lambda path: os.stat(path, follow_symlinks=False),
) -> ActivatedSocketIdentity:
    observed = descriptor_stat(SYSTEMD_LISTEN_FD)
    path_observed = path_stat(BROKER_SOCKET_PATH)
    if (
        listener.fileno() != SYSTEMD_LISTEN_FD
        or listener.family != socket.AF_UNIX
        or (listener.type & 0xF) != socket.SOCK_SEQPACKET
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
        or listener.getsockname() != BROKER_SOCKET_PATH
        or (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
            observed.st_uid,
            observed.st_gid,
            observed.st_nlink,
        )
        != (
            path_observed.st_dev,
            path_observed.st_ino,
            path_observed.st_mode,
            path_observed.st_uid,
            path_observed.st_gid,
            path_observed.st_nlink,
        )
    ):
        raise BrokerRevokedError("activated listener identity changed")
    return ActivatedSocketIdentity(
        path=BROKER_SOCKET_PATH,
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
        uid=observed.st_uid,
        gid=observed.st_gid,
        nlink=observed.st_nlink,
    )


def read_root_installed_file(
    path: str,
    maximum_bytes: int,
) -> tuple[InstalledFileIdentity, bytes]:
    """Open and completely hash one immutable root-owned installed file."""

    _require_canonical_path(path, "installed file path")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("installed file byte bound must be positive")
    if os.path.realpath(path) != path:
        raise BrokerConfigurationError("installed file path is not canonical")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        content = bytearray()
        while len(content) <= maximum_bytes:
            chunk = os.read(descriptor, maximum_bytes - len(content) + 1)
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > maximum_bytes:
            raise BrokerConfigurationError("installed file exceeds its byte bound")
        digest = hashlib.sha256(content).hexdigest()
        identity = InstalledFileIdentity(
            canonical_path=path,
            device=status.st_dev,
            inode=status.st_ino,
            mode=status.st_mode,
            uid=status.st_uid,
            gid=status.st_gid,
            nlink=status.st_nlink,
            size=status.st_size,
            sha256=digest,
        )
        _validate_installed_identity(identity, path, digest)
        if status.st_size != len(content):
            raise BrokerConfigurationError("installed file size changed while reading")
        result = identity, bytes(content)
    except BaseException as primary:
        closing_descriptor = descriptor
        descriptor = -1
        failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_descriptor),))
        _annotate_cleanup_failures(primary, failures)
        raise
    closing_descriptor = descriptor
    descriptor = -1
    failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_descriptor),))
    if failures:
        raise failures[0]
    return result


def load_broker_config(
    config_path: str,
    *,
    installed_file_reader: Callable[
        [str, int], tuple[InstalledFileIdentity, bytes]
    ] = read_root_installed_file,
) -> BrokerConfig:
    """Load the root config and every configured executable by exact identity."""

    if not callable(installed_file_reader):
        raise TypeError("installed_file_reader must be callable")
    config_identity, raw = installed_file_reader(config_path, MAXIMUM_CONFIG_BYTES)
    try:
        decoded = loads_canonical_packet(raw)
        if not isinstance(decoded, Mapping):
            raise ValueError("configuration root is not an object")
        ordered_assets = (
            decoded["gate"],
            decoded["anchor"],
            decoded["startup_probe"],
            *decoded["registrations"],
        )
        paths = tuple(
            asset[
                (
                    "canonical_qupath_executable"
                    if "canonical_qupath_executable" in asset
                    else "canonical_path"
                )
            ]
            for asset in ordered_assets
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BrokerConfigurationError(
            "broker configuration cannot name installed assets"
        ) from error
    identities: dict[str, InstalledFileIdentity] = {}
    for path in paths:
        _require_canonical_path(path, "configured installed path")
        if path in identities or path == config_path:
            raise BrokerConfigurationError("installed asset paths must be distinct")
        identity, _content = installed_file_reader(path, 268_435_456)
        identities[path] = identity
    return parse_broker_config(
        raw,
        config_identity=config_identity,
        installed_identities=identities,
    )


def _send_notify_datagram(address: str, packet: bytes) -> None:
    target = ("\0" + address[1:]) if address.startswith("@") else address
    transport = socket.socket(
        socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0)
    )
    try:
        transport.connect(target)
        if transport.send(packet) != len(packet):
            raise BrokerRejectedError("sd_notify datagram was truncated")
    except BaseException as primary:
        failures = _attempt_sync_cleanup_steps((transport.close,))
        _annotate_cleanup_failures(primary, failures)
        raise
    failures = _attempt_sync_cleanup_steps((transport.close,))
    if failures:
        raise failures[0]


async def _notify_barrier(address: str, timeout_seconds: float) -> None:
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    target = ("\0" + address[1:]) if address.startswith("@") else address
    transport = None
    primary_error: BaseException | None = None
    try:
        transport = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        transport.connect(target)
        sent = transport.sendmsg(
            [b"BARRIER=1"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", (write_fd,)))],
        )
        if sent != len(b"BARRIER=1"):
            raise BrokerRejectedError("sd_notify barrier datagram was truncated")
        closing_write_fd = write_fd
        write_fd = -1
        failures = await _attempt_cleanup_steps((lambda: os.close(closing_write_fd),))
        if failures:
            raise failures[0]
        loop = asyncio.get_running_loop()
        acknowledged = loop.create_future()

        def readable() -> None:
            if acknowledged.done():
                return
            try:
                content = os.read(read_fd, 1)
            except BlockingIOError:
                return
            if content:
                acknowledged.set_exception(BrokerRejectedError("sd_notify barrier returned data"))
            else:
                acknowledged.set_result(None)

        loop.add_reader(read_fd, readable)
        try:
            await asyncio.wait_for(acknowledged, timeout=float(timeout_seconds))
        except BaseException as primary:
            failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(read_fd),))
            _annotate_cleanup_failures(primary, failures)
            raise
        failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(read_fd),))
        if failures:
            raise failures[0]
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_steps: list[Callable[[], object]] = []
        if write_fd >= 0:
            closing_write_fd = write_fd
            write_fd = -1
            cleanup_steps.append(lambda: os.close(closing_write_fd))
        closing_read_fd = read_fd
        read_fd = -1
        cleanup_steps.append(lambda: os.close(closing_read_fd))
        if transport is not None:
            closing_transport = transport
            transport = None
            cleanup_steps.append(closing_transport.close)
        failures = await _attempt_cleanup_steps(cleanup_steps)
        if primary_error is not None:
            _annotate_cleanup_failures(primary_error, failures)
        elif failures:
            raise failures[0]


class SystemdBrokerNotifier:
    """Concrete fixed-state sd_notify adapter with acknowledgement barrier."""

    def __init__(
        self,
        address: str,
        *,
        _send: Callable[[str, bytes], object] = _send_notify_datagram,
        _barrier: Callable[[str, float], object] = _notify_barrier,
    ) -> bool:
        if type(address) is not str or not address:
            raise BrokerConfigurationError("NOTIFY_SOCKET is unavailable")
        self.address = address
        self._send = _send
        self._barrier = _barrier

    @classmethod
    def from_environment(cls) -> "SystemdBrokerNotifier":
        return cls(os.environ.get("NOTIFY_SOCKET", ""))

    def notify(self, state: str) -> None:
        if state not in ("READY=1", "STOPPING=1"):
            raise BrokerRejectedError("broker notification state is not fixed")
        self._send(self.address, state.encode("ascii"))

    async def barrier(self, timeout_seconds: float) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise BrokerRejectedError("notify barrier deadline is invalid")
        result = self._barrier(self.address, float(timeout_seconds))
        if inspect.isawaitable(result):
            await result


def receive_bounded_seqpacket(
    connection: socket.socket,
    *,
    maximum_bytes: int,
) -> tuple[bytes, tuple[int, ...]]:
    """Receive exactly one bounded packet and at most one SCM_RIGHTS fd."""

    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAXIMUM_REQUEST_BYTES:
        raise ValueError("seqpacket byte bound is invalid")
    ancillary_size = socket.CMSG_SPACE(2 * array.array("i").itemsize)
    flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    packet, ancillary, message_flags, _address = connection.recvmsg(
        maximum_bytes + 1,
        ancillary_size,
        flags,
    )
    descriptors: list[int] = []
    try:
        input_error: BaseException | None = None
        if message_flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            input_error = BrokerRejectedError("seqpacket or ancillary data was truncated")
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                if input_error is None:
                    input_error = BrokerRejectedError("unexpected ancillary data")
                continue
            values = array.array("i")
            if not data:
                if input_error is None:
                    input_error = BrokerRejectedError("malformed SCM_RIGHTS payload")
                continue
            aligned_length = len(data) - (len(data) % values.itemsize)
            if aligned_length:
                values.frombytes(data[:aligned_length])
            descriptors.extend(values)
            for descriptor in values:
                try:
                    os.set_inheritable(descriptor, False)
                except BaseException as error:
                    if input_error is None:
                        input_error = error
            if aligned_length != len(data) and input_error is None:
                input_error = BrokerRejectedError("malformed SCM_RIGHTS payload")
        if (not packet or len(packet) > maximum_bytes) and input_error is None:
            input_error = BrokerRejectedError("request packet size is invalid")
        if len(descriptors) > 1 and input_error is None:
            input_error = BrokerRejectedError("too many passed descriptors")
        if input_error is not None:
            raise input_error
        return packet, tuple(descriptors)
    except BaseException as primary:
        closing_descriptors = tuple(descriptors)
        descriptors.clear()
        failures = _attempt_sync_cleanup_steps(
            tuple(
                lambda descriptor=descriptor: os.close(descriptor)
                for descriptor in closing_descriptors
            )
        )
        _annotate_cleanup_failures(primary, failures)
        raise


async def _receive_bounded_seqpacket_async(
    connection: socket.socket,
    *,
    maximum_bytes: int,
) -> tuple[bytes, tuple[int, ...]]:
    try:
        descriptor = connection.fileno()
        connection.setblocking(False)
    except AttributeError:
        return receive_bounded_seqpacket(connection, maximum_bytes=maximum_bytes)
    loop = asyncio.get_running_loop()
    while True:
        try:
            return receive_bounded_seqpacket(connection, maximum_bytes=maximum_bytes)
        except BlockingIOError:
            readable = loop.create_future()
            loop.add_reader(
                descriptor,
                lambda: not readable.done() and readable.set_result(None),
            )
            try:
                await readable
            except BaseException as primary:
                failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(descriptor),))
                _annotate_cleanup_failures(primary, failures)
                raise
            failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(descriptor),))
            if failures:
                raise failures[0]


async def _send_seqpacket_async(connection: socket.socket, packet: bytes) -> None:
    try:
        descriptor = connection.fileno()
    except AttributeError:
        sent = connection.send(packet)
        if sent != len(packet):
            raise BrokerRejectedError("broker response was not sent atomically")
        return
    loop = asyncio.get_running_loop()
    while True:
        try:
            sent = connection.send(packet)
            if sent != len(packet):
                raise BrokerRejectedError("broker response was not sent atomically")
            return
        except BlockingIOError:
            writable = loop.create_future()
            loop.add_writer(
                descriptor,
                lambda: not writable.done() and writable.set_result(None),
            )
            try:
                await writable
            except BaseException as primary:
                failures = _attempt_sync_cleanup_steps((lambda: loop.remove_writer(descriptor),))
                _annotate_cleanup_failures(primary, failures)
                raise
            failures = _attempt_sync_cleanup_steps((lambda: loop.remove_writer(descriptor),))
            if failures:
                raise failures[0]


class PosixContainmentAuthorityRuntime:
    """Linux pidfd/procfs/anchor/cgroup implementation of the authority port."""

    def __init__(
        self,
        *,
        _spawn_anchor: Callable[[AnchorLaunchRequest], object] | None = None,
        proc_root: str = "/proc",
        mountinfo_path: str = "/proc/self/mountinfo",
        config: BrokerConfig | None = None,
    ) -> None:
        self._spawn_anchor = _spawn_anchor or self._spawn_fixed_anchor
        self.proc_root = proc_root
        self.mountinfo_path = mountinfo_path
        self.config = config
        self._anchor_requests: dict[int, AnchorLaunchRequest] = {}

    async def launch_anchor(self, request: AnchorLaunchRequest) -> AnchorHandle:
        if not isinstance(request, AnchorLaunchRequest):
            raise BrokerRejectedError("anchor launch request is not fixed")
        result = self._spawn_anchor(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, AnchorHandle):
            raise BrokerRejectedError("anchor launcher returned malformed handle")
        pid = getattr(result.identity, "anchor_pid", None)
        if type(pid) is int:
            self._anchor_requests[pid] = request
        return result

    async def _spawn_fixed_anchor(self, request: AnchorLaunchRequest) -> AnchorHandle:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        pid = -1
        pidfd = -1
        try:
            _write_all(write_fd, request.bootstrap_frame)
            _prove_sole_forking_thread()
            pid = os.fork()
            if pid == 0:  # pragma: no cover - exercised only by native acceptance
                try:
                    _run_fixed_anchor_child(request, read_fd, write_fd)
                except BaseException:
                    os._exit(127)
            closing_read_fd = read_fd
            read_fd = -1
            os.close(closing_read_fd)
            await _wait_for_initial_stop(pid)
            evidence = _read_process_evidence(pid, self.proc_root)
            _validate_anchor_process_evidence(evidence, path=request.path, phase="first-stop")
            identity = ScopeAnchorIdentityV1(
                anchor_pid=pid,
                anchor_start_token=evidence["start_token"],
                anchor_uid=request.resuid[0],
                anchor_gid=request.resgid[0],
                anchor_executable_sha256=request.executable_identity.sha256,
            )
            pidfd = _acquire_proven_pidfd(pid, identity.anchor_start_token, self.proc_root)
            self._anchor_requests[pid] = request
            return AnchorHandle(
                identity=identity,
                pidfd=pidfd,
                promotion_write_fd=write_fd,
            )
        except BaseException as primary:
            steps: list[Callable[[], object]] = []
            if read_fd >= 0:
                closing_read_fd = read_fd
                read_fd = -1
                steps.append(lambda fd=closing_read_fd: os.close(fd))
            closing_write_fd = write_fd
            write_fd = -1
            steps.append(lambda fd=closing_write_fd: os.close(fd))
            if pid > 0:
                steps.append(lambda: _bounded_kill_and_reap_child(pid, pidfd, 10.0))
            if pidfd >= 0:
                closing_pidfd = pidfd
                pidfd = -1
                steps.append(lambda fd=closing_pidfd: os.close(fd))
            failures = await _attempt_cleanup_steps(steps)
            _annotate_cleanup_failures(primary, failures)
            raise

    async def inspect_gate_for_attach(
        self,
        peer,
        payload,
        target_fd,
        config,
        *,
        _require_stopped: bool | None = True,
    ):
        ready = payload.gate_ready
        evidence = _read_process_evidence(ready.gate_pid, self.proc_root)
        argv = evidence["argv"]
        gate_prefix = ("/usr/bin/python3", "-I", config.gate.canonical_path)
        try:
            separator = argv.index("--", len(gate_prefix) + 6)
        except (AttributeError, ValueError) as error:
            raise BrokerRejectedError("gate command profile framing is malformed") from error
        prefix = (
            *gate_prefix,
            "--expected-parent-pid",
            str(peer.pid),
            "--expected-parent-start-token",
            peer.start_token,
            "--launch-nonce-sha256",
            payload.authority.launch_nonce_sha256,
        )
        if tuple(argv[: len(prefix)]) != prefix:
            raise BrokerRejectedError("gate command authority framing changed")
        preserved = argv[len(prefix) : separator]
        if len(preserved) % 2 or any(
            preserved[index] != "--preserve-fd"
            or not preserved[index + 1].isdigit()
            or int(preserved[index + 1], 10) <= 4
            for index in range(0, len(preserved), 2)
        ):
            raise BrokerRejectedError("gate preserved descriptor profile is malformed")
        preserved_descriptors = tuple(
            int(preserved[index + 1], 10) for index in range(0, len(preserved), 2)
        )
        if len(preserved_descriptors) != len(set(preserved_descriptors)):
            raise BrokerRejectedError("gate preserved descriptors are duplicated")
        _validate_gate_process_evidence(evidence, preserved_descriptors=preserved_descriptors)
        target_argv = tuple(argv[separator + 1 :])
        if not target_argv:
            raise BrokerRejectedError("target argv is empty")
        registration = next(
            (item for item in config.registrations if item.canonical_path == target_argv[0]),
            None,
        )
        if registration is None:
            raise BrokerRejectedError("target executable path is not registered")
        profile = _infer_command_profile(target_argv)
        if (
            target_argv[0] != registration.canonical_path
            or len(target_argv) != ready.argv_count
            or len(target_argv) > config.maximum_argument_count
            or any(not value or "\0" in value for value in target_argv)
            or hashlib.sha256(dumps_canonical(list(target_argv))).hexdigest() != ready.argv_sha256
            or profile not in registration.command_profiles
        ):
            raise BrokerRejectedError("target argv does not match registered command profile")
        environment = evidence["environment"]
        fixed_names = frozenset(("HOME", "USER", "LOGNAME", "UID", "GID", "GROUP"))
        requested = {name: value for name, value in environment.items() if name not in fixed_names}
        expected_environment = derive_user_environment(peer.account, registration, requested)
        environment_count, environment_bytes, environment_digest = _environment_facts(environment)
        if (
            environment != expected_environment
            or environment_count != ready.environment_count
            or environment_count > config.maximum_environment_entries
            or environment_bytes != ready.environment_bytes
            or environment_bytes > config.maximum_environment_bytes
            or environment_digest != ready.environment_names_sizes_sha256
            or evidence["cwd_device"] != ready.cwd_device
            or evidence["cwd_inode"] != ready.cwd_inode
        ):
            raise BrokerRejectedError("gate environment or cwd material changed")
        if (
            evidence["start_token"] != ready.gate_start_token
            or evidence["parent_pid"] != peer.pid
            or (_require_stopped is True and evidence["state"] not in ("T", "t"))
            or (_require_stopped is False and evidence["state"] in ("T", "t", "Z", "X", "x"))
            or (_require_stopped is None and evidence["state"] in ("Z", "X", "x"))
            or evidence["uids"] != (peer.uid,) * 4
            or evidence["gids"] != (peer.gid,) * 4
        ):
            raise BrokerRejectedError("gate process identity is not exact")
        _assert_installed_identity_current(config.gate.identity)
        status = os.fstat(target_fd)
        digest = _sha256_descriptor(target_fd, 268_435_456)
        target_path = os.readlink(f"{self.proc_root}/self/fd/{target_fd}")
        gate_target_path = os.readlink(
            f"{self.proc_root}/{ready.gate_pid}/fd/{ready.retained_target_fd}"
        )
        gate_target_fd = os.open(
            f"{self.proc_root}/{ready.gate_pid}/fd/{ready.retained_target_fd}",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            gate_status = os.fstat(gate_target_fd)
            gate_digest = _sha256_descriptor(gate_target_fd, 268_435_456)
        except BaseException as primary:
            closing_gate_target_fd = gate_target_fd
            gate_target_fd = -1
            failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_gate_target_fd),))
            _annotate_cleanup_failures(primary, failures)
            raise
        closing_gate_target_fd = gate_target_fd
        gate_target_fd = -1
        failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_gate_target_fd),))
        if failures:
            raise failures[0]
        if (
            status.st_dev != ready.target_executable_device
            or status.st_ino != ready.target_executable_inode
            or status.st_dev != registration.identity.device
            or status.st_ino != registration.identity.inode
            or not _descriptor_matches_registration(status, target_path, digest, registration)
            or not _descriptor_matches_registration(
                gate_status, gate_target_path, gate_digest, registration
            )
            or digest != ready.target_executable_sha256
            or digest != registration.sha256
        ):
            raise BrokerRejectedError("retained target descriptor is not registered")
        return GateAttachmentEvidence(
            gate_ready=ready,
            gate_pidfd=_acquire_proven_pidfd(
                ready.gate_pid, ready.gate_start_token, self.proc_root
            ),
            registration_id=registration.registration_id,
            retained_target_device=status.st_dev,
            retained_target_inode=status.st_ino,
            retained_target_sha256=digest,
            gate_phase=("stopped-gate" if evidence["state"] in ("T", "t") else "running-gate"),
        )

    async def validate_anchor(self, peer, handle, phase: str):
        pid = getattr(handle.identity, "anchor_pid", None)
        if type(pid) is not int:
            raise BrokerRejectedError("anchor PID is unavailable")
        request = self._anchor_requests.get(pid)
        if request is None:
            # Injected native-test handles already carry their validated identity.
            if isinstance(handle.identity, ScopeAnchorIdentityV1):
                return handle.identity
            raise BrokerRejectedError("anchor request identity is unavailable")
        evidence = _read_process_evidence(pid, self.proc_root)
        _validate_anchor_process_evidence(evidence, path=request.path, phase=phase)
        if (
            evidence["start_token"] != handle.identity.anchor_start_token
            or evidence["parent_pid"] != os.getpid()
            or evidence["state"] not in ("T", "t")
            or evidence["uids"] != (peer.uid,) * 4
            or evidence["gids"] != (peer.gid,) * 4
        ):
            raise BrokerRejectedError(f"anchor {phase} identity is not exact")
        _assert_installed_identity_current(request.executable_identity)
        return ScopeAnchorIdentityV1(
            anchor_pid=pid,
            anchor_start_token=evidence["start_token"],
            anchor_uid=peer.uid,
            anchor_gid=peer.gid,
            anchor_executable_sha256=request.executable_identity.sha256,
        )

    async def verify_stopped_processes(self, gate, anchor, phase: str) -> None:
        for pid, token in (
            (gate.gate_ready.gate_pid, gate.gate_ready.gate_start_token),
            (anchor.identity.anchor_pid, anchor.identity.anchor_start_token),
        ):
            evidence = _read_process_evidence(pid, self.proc_root)
            if evidence["start_token"] != token or evidence["state"] not in ("T", "t"):
                raise BrokerRejectedError(f"process changed before {phase}")

    async def verify_scope(self, manager, request):
        from .systemd_adapter import verify_scope_attachment

        return await verify_scope_attachment(
            manager,
            request,
            previous_invocation_id=None,
            mountinfo_path=self.mountinfo_path,
            proc_root=self.proc_root,
        )

    async def promote_anchor(self, handle: AnchorHandle) -> None:
        descriptor = handle.promotion_write_fd
        if descriptor < 0:
            raise BrokerRejectedError("anchor promotion descriptor is not owned")
        object.__setattr__(handle, "promotion_write_fd", -1)
        try:
            _write_all(descriptor, PROMOTION_TOKEN)
        except BaseException as primary:
            failures = _attempt_sync_cleanup_steps((lambda: os.close(descriptor),))
            _annotate_cleanup_failures(primary, failures)
            raise
        failures = _attempt_sync_cleanup_steps((lambda: os.close(descriptor),))
        if failures:
            raise failures[0]

    async def close_anchor_promotion(self, handle: AnchorHandle) -> None:
        descriptor = handle.promotion_write_fd
        if descriptor < 0:
            return
        object.__setattr__(handle, "promotion_write_fd", -1)
        os.close(descriptor)

    async def signal_pidfd(self, pidfd: int, signum: int) -> None:
        if signum not in (signal.SIGCONT, signal.SIGUSR1, signal.SIGKILL):
            raise BrokerRejectedError("pidfd signal is outside fixed policy")
        signal.pidfd_send_signal(pidfd, signum)

    async def validate_anchor_descriptors(self, handle: AnchorHandle) -> None:
        pid = handle.identity.anchor_pid
        names = os.listdir(f"{self.proc_root}/{pid}/fd")
        if frozenset(names) != frozenset(("0", "1", "2")):
            raise BrokerRejectedError("promoted anchor descriptors are not standard-only")

    async def close_target_fd(self, target_fd: int) -> None:
        os.close(target_fd)

    async def retire_authority(self, attachment: object) -> None:
        self._anchor_requests.pop(attachment.anchor_identity.anchor_pid, None)

    async def gate_pidfd(self, attachment):
        return _acquire_proven_pidfd(
            attachment.gate_pid, attachment.gate_start_token, self.proc_root
        )

    async def anchor_pidfd(self, attachment):
        return _acquire_proven_pidfd(
            attachment.anchor_identity.anchor_pid,
            attachment.anchor_identity.anchor_start_token,
            self.proc_root,
        )

    async def await_gate_exit(self, attachment, timeout_seconds: float) -> None:
        pidfd = await self.gate_pidfd(attachment)
        try:
            await _await_pidfd_readiness(pidfd, timeout_seconds)
        except BaseException as primary:
            closing_pidfd = pidfd
            pidfd = -1
            failures = await _attempt_cleanup_steps((lambda: os.close(closing_pidfd),))
            _annotate_cleanup_failures(primary, failures)
            raise
        closing_pidfd = pidfd
        pidfd = -1
        failures = await _attempt_cleanup_steps((lambda: os.close(closing_pidfd),))
        if failures:
            raise failures[0]
        return None

    async def await_pidfd_exit(self, pidfd: int, timeout_seconds: float) -> None:
        await _await_pidfd_readiness(pidfd, timeout_seconds)

    async def close_pidfd(self, pidfd: int) -> None:
        os.close(pidfd)

    async def await_drain_proof(self, attachment, timeout_seconds: float):
        from .systemd_adapter import (
            CgroupVerificationError,
            open_verified_cgroup,
            prove_cgroup_deleted_after_stop,
            prove_cgroup_events_zero,
        )

        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            try:
                cgroup = open_verified_cgroup(
                    attachment.control_group,
                    mountinfo_path=self.mountinfo_path,
                )
            except CgroupVerificationError:
                proof = prove_cgroup_deleted_after_stop(
                    attachment.cgroup_identity,
                    durable_stop_intent_cgroup_identity=attachment.cgroup_identity,
                    mountinfo_path=self.mountinfo_path,
                )
                if proof is not None:
                    return proof
            else:
                proof = None
                try:
                    if cgroup.identity != attachment.cgroup_identity:
                        raise BrokerRejectedError("cgroup identity was substituted")
                    proof = prove_cgroup_events_zero(cgroup)
                except BaseException as primary:
                    closing_cgroup = cgroup
                    cgroup = None
                    failures = _attempt_sync_cleanup_steps((closing_cgroup.close,))
                    _annotate_cleanup_failures(primary, failures)
                    raise
                closing_cgroup = cgroup
                cgroup = None
                failures = _attempt_sync_cleanup_steps((closing_cgroup.close,))
                if failures:
                    raise failures[0]
                if proof is not None:
                    return proof
            await asyncio.sleep(0.01)
        raise TimeoutError("cgroup did not drain before deadline")

    async def bind_drain_observer(self, attachment) -> BoundDrainObserver:
        from .systemd_adapter import open_verified_cgroup

        verified = open_verified_cgroup(
            attachment.control_group,
            mountinfo_path=self.mountinfo_path,
        )
        if verified.identity != attachment.cgroup_identity:
            error = BrokerRejectedError("stop cgroup observer identity was substituted")
            failures = await _attempt_cleanup_steps((verified.close,))
            _annotate_cleanup_failures(error, failures)
            raise error
        return BoundDrainObserver(attachment=attachment, verified_cgroup=verified)

    async def await_bound_drain_proof(
        self,
        observer: BoundDrainObserver,
        timeout_seconds: float,
    ):
        from .systemd_adapter import (
            CgroupVerificationError,
            prove_cgroup_deleted_after_stop,
            prove_cgroup_events_zero,
        )

        if not isinstance(observer, BoundDrainObserver):
            raise BrokerRejectedError("bound cgroup observer is malformed")
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            try:
                proof = prove_cgroup_events_zero(observer.verified_cgroup)
            except CgroupVerificationError as events_error:
                try:
                    deletion = prove_cgroup_deleted_after_stop(
                        observer.attachment.cgroup_identity,
                        durable_stop_intent_cgroup_identity=observer.attachment.cgroup_identity,
                        mountinfo_path=self.mountinfo_path,
                    )
                except asyncio.CancelledError:
                    raise
                except (
                    CgroupVerificationError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as deletion_error:
                    events_error.add_note(
                        "deletion verification also failed: " + type(deletion_error).__name__
                    )
                    raise events_error
                if (
                    isinstance(deletion, CgroupDeletedAfterStopProofV1)
                    and deletion.cgroup_identity == observer.attachment.cgroup_identity
                ):
                    return deletion
                raise events_error
            if proof is not None:
                return proof
            deletion = prove_cgroup_deleted_after_stop(
                observer.attachment.cgroup_identity,
                durable_stop_intent_cgroup_identity=observer.attachment.cgroup_identity,
                mountinfo_path=self.mountinfo_path,
            )
            if (
                isinstance(deletion, CgroupDeletedAfterStopProofV1)
                and deletion.cgroup_identity == observer.attachment.cgroup_identity
            ):
                return deletion
            if deletion is not None:
                raise BrokerRejectedError("bound deletion proof is malformed")
            await asyncio.sleep(0.01)
        raise TimeoutError("bound cgroup did not drain before deadline")

    async def close_drain_observer(self, observer: BoundDrainObserver) -> None:
        if not isinstance(observer, BoundDrainObserver) or observer.closed:
            raise BrokerRejectedError("bound cgroup observer ownership is invalid")
        observer.closed = True
        observer.verified_cgroup.close()

    async def original_owner_is_live(self, pid: int, start_token: str) -> bool:
        try:
            return _process_start_token(pid, self.proc_root) == start_token
        except (FileNotFoundError, ProcessLookupError):
            return False

    async def verify_reconciled_attachment(
        self,
        attachment,
        identity,
        *,
        attempt=None,
        gate_phase: str = "target",
    ):
        from .systemd_adapter import open_verified_cgroup

        cgroup = open_verified_cgroup(
            attachment.control_group,
            mountinfo_path=self.mountinfo_path,
        )
        try:
            if cgroup.identity != attachment.cgroup_identity:
                raise BrokerRejectedError("reconciled cgroup identity was substituted")
            _verify_no_child_cgroup_local(cgroup)
        except BaseException as primary:
            closing_cgroup = cgroup
            cgroup = None
            failures = _attempt_sync_cleanup_steps((closing_cgroup.close,))
            _annotate_cleanup_failures(primary, failures)
            raise
        closing_cgroup = cgroup
        cgroup = None
        failures = _attempt_sync_cleanup_steps((closing_cgroup.close,))
        if failures:
            raise failures[0]
        gate_live = await self.original_owner_is_live(
            attachment.gate_pid, attachment.gate_start_token
        )
        anchor_live = await self.original_owner_is_live(
            attachment.anchor_identity.anchor_pid,
            attachment.anchor_identity.anchor_start_token,
        )
        if not anchor_live:
            return ReconciledAttachmentDisposition.ANCHOR_LOST
        _require_process_cgroup_membership(
            attachment.anchor_identity.anchor_pid,
            attachment.control_group,
            self.proc_root,
        )
        anchor = _read_process_evidence(attachment.anchor_identity.anchor_pid, self.proc_root)
        if (
            anchor["uids"] != (attachment.peer_uid,) * 4
            or anchor["gids"] != (attachment.peer_gid,) * 4
        ):
            raise BrokerRejectedError("reconciled anchor identity was substituted")
        if self.config is not None:
            _validate_anchor_process_evidence(
                anchor,
                path=self.config.anchor.canonical_path,
                phase="idle",
            )
            _assert_installed_identity_current(self.config.anchor.identity)
        if gate_live:
            _require_process_cgroup_membership(
                attachment.gate_pid,
                attachment.control_group,
                self.proc_root,
            )
            gate_process = _read_process_evidence(attachment.gate_pid, self.proc_root)
            observed_gate_phase = (
                "stopped-gate" if gate_process["state"] in ("T", "t") else "running-gate"
            )
            if self.config is not None and attempt is not None:
                if gate_phase in ("stopped-gate", "running-gate", "release-intent"):
                    account = _live_passwd_account(attachment.peer_uid, attachment.peer_gid)
                    peer = PeerIdentity(
                        pid=attachment.peer_pid,
                        uid=attachment.peer_uid,
                        gid=attachment.peer_gid,
                        start_token=attachment.peer_pid_start_token,
                        account=account,
                    )
                    target_fd = os.open(
                        f"{self.proc_root}/{attachment.gate_pid}/fd/4",
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    observed = None
                    try:
                        observed = await self.inspect_gate_for_attach(
                            peer,
                            type(
                                "_Payload",
                                (),
                                {
                                    "authority": attachment.authority,
                                    "gate_ready": attempt.gate_ready,
                                },
                            )(),
                            target_fd,
                            self.config,
                            _require_stopped=(
                                None
                                if gate_phase == "release-intent"
                                else gate_phase == "stopped-gate"
                            ),
                        )
                    except BaseException as primary:
                        closing_target_fd = target_fd
                        target_fd = -1
                        cleanup_steps: list[Callable[[], object]] = [
                            lambda: os.close(closing_target_fd)
                        ]
                        if observed is not None:
                            closing_observed_pidfd = observed.gate_pidfd
                            observed = None
                            cleanup_steps.append(lambda: os.close(closing_observed_pidfd))
                        failures = await _attempt_cleanup_steps(cleanup_steps)
                        _annotate_cleanup_failures(primary, failures)
                        raise
                    closing_target_fd = target_fd
                    target_fd = -1
                    cleanup_steps = [lambda: os.close(closing_target_fd)]
                    if observed is not None:
                        closing_observed_pidfd = observed.gate_pidfd
                        observed = None
                        cleanup_steps.append(lambda: os.close(closing_observed_pidfd))
                    failures = await _attempt_cleanup_steps(cleanup_steps)
                    if failures:
                        raise failures[0]
                elif gate_phase == "target":
                    _validate_released_target_process(
                        _read_process_evidence(attachment.gate_pid, self.proc_root),
                        attachment,
                        attempt.gate_ready,
                        self.config,
                    )
                else:
                    raise BrokerRejectedError("live gate phase is invalid")
        disposition = (
            ReconciledAttachmentDisposition.LIVE
            if gate_live
            else ReconciledAttachmentDisposition.LEADER_EXITED
        )
        if gate_phase == "release-intent":
            return ReconciledAttachmentObservation(
                disposition=disposition,
                gate_phase=(observed_gate_phase if gate_live else "absent-gate"),
            )
        return disposition

    async def prove_reconciled_drain(self, attachment):
        from .systemd_adapter import (
            CgroupVerificationError,
            open_verified_cgroup,
            prove_cgroup_deleted_after_stop,
            prove_cgroup_events_zero,
        )

        try:
            verified = open_verified_cgroup(
                attachment.control_group,
                mountinfo_path=self.mountinfo_path,
            )
        except CgroupVerificationError:
            verified = None
        if verified is not None:
            zero = None
            try:
                if verified.identity != attachment.cgroup_identity:
                    raise BrokerRejectedError("recovery cgroup identity was substituted")
                zero = prove_cgroup_events_zero(verified)
            except BaseException as primary:
                closing_verified = verified
                verified = None
                failures = _attempt_sync_cleanup_steps((closing_verified.close,))
                _annotate_cleanup_failures(primary, failures)
                raise
            closing_verified = verified
            verified = None
            failures = _attempt_sync_cleanup_steps((closing_verified.close,))
            if failures:
                raise failures[0]
            if zero is not None:
                return zero

        return prove_cgroup_deleted_after_stop(
            attachment.cgroup_identity,
            durable_stop_intent_cgroup_identity=attachment.cgroup_identity,
            mountinfo_path=self.mountinfo_path,
        )

    async def kill_private_anchor(self, handle: AnchorHandle) -> None:
        request = self._anchor_requests.pop(handle.identity.anchor_pid, None)
        promotion_write_fd = handle.promotion_write_fd
        pidfd = handle.pidfd
        object.__setattr__(handle, "promotion_write_fd", -1)
        object.__setattr__(handle, "pidfd", -1)
        if request is None and promotion_write_fd < 0 and pidfd < 0:
            return
        cleanup_steps: list[Callable[[], object]] = []
        if promotion_write_fd >= 0:
            cleanup_steps.append(lambda: os.close(promotion_write_fd))
        if pidfd >= 0:
            cleanup_steps.extend(
                (
                    lambda: _bounded_kill_and_reap_child(handle.identity.anchor_pid, pidfd, 10.0),
                    lambda: os.close(pidfd),
                )
            )
        failures = await _attempt_cleanup_steps(cleanup_steps)
        if failures:
            raise failures[0]


class ContainmentOperationBackend:
    """Crash-safe ledger/systemd transitions for all eight closed operations.

    Every durable boundary is authored by :class:`ContainmentLedger.publish`;
    the runtime is intentionally incapable of choosing a unit, signal, scope
    policy, executable, credential, or cgroup path.
    """

    def __init__(
        self,
        *,
        config: BrokerConfig,
        ledger: object,
        runtime: ContainmentAuthorityRuntime,
        manager: object,
        broker_pid: int,
        broker_start_token: str,
        clock_ns: Callable[[], int],
    ) -> None:
        if not isinstance(config, BrokerConfig):
            raise TypeError("config must be BrokerConfig")
        if type(broker_pid) is not int or broker_pid < 1:
            raise ValueError("broker_pid must be positive")
        if type(broker_start_token) is not str or not broker_start_token:
            raise ValueError("broker_start_token is unavailable")
        self.config = config
        self.ledger = ledger
        self.runtime = runtime
        self.manager = manager
        self.broker_pid = broker_pid
        self.broker_start_token = broker_start_token
        self.clock_ns = clock_ns
        self._authorities: dict[tuple[int, str], object] = {}
        self._release_events: dict[tuple[int, str], asyncio.Event] = {}
        self._release_resume_owned: set[tuple[int, str]] = set()
        self._release_resume_signalled: set[tuple[int, str]] = set()
        self._release_resume_expiries: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self._pidfds: dict[tuple[int, str], AuthorityPidfds] = {}

    def _retire_release_claim(self, key: tuple[int, str]) -> None:
        expiry = self._release_resume_expiries.pop(key, None)
        if expiry is not None:
            expiry.cancel()
        self._release_resume_owned.discard(key)
        self._release_resume_signalled.discard(key)

    def _retire_release_resume(self, key: tuple[int, str], *, wake_waiters: bool = False) -> None:
        self._retire_release_claim(key)
        event = self._release_events.pop(key, None)
        if wake_waiters and event is not None:
            event.set()

    def _mark_release_resume_signalled(self, key: tuple[int, str]) -> None:
        self._release_resume_signalled.add(key)
        previous = self._release_resume_expiries.pop(key, None)
        if previous is not None:
            previous.cancel()
        loop = asyncio.get_running_loop()
        self._release_resume_expiries[key] = loop.call_later(
            self.config.maximum_runtime_seconds,
            lambda: self._retire_release_resume(key, wake_waiters=True),
        )

    def _reserve_mutation(
        self, context: BrokerContext, peer: PeerIdentity, authority: object
    ) -> object | None:
        request = getattr(context, "authenticated_request", None)
        canonical = getattr(context, "canonical_request_bytes", None)
        if request is None and canonical is None:
            return None
        if type(request) is not BrokerRequestV1 or type(canonical) is not bytes:
            raise BrokerRejectedError("authenticated request reservation context is incomplete")
        reservation = ContainmentRequestReservationV1.from_request(
            authenticated_uid=peer.uid,
            primary_gid=peer.gid,
            peer_pid=peer.pid,
            peer_start_token=peer.start_token,
            request=request,
            canonical_request_bytes=canonical,
            broker_build_id=BROKER_BUILD_ID,
            broker_config_sha256=self.config.config_sha256,
        )
        return self.ledger.reserve_request(peer.uid, authority, reservation)

    async def capabilities(self, context: BrokerContext) -> object:
        return _capabilities_for_retained_certificate(
            context.config,
            context.certification,
        )

    async def attach(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
        target_fd: int,
    ) -> object:
        try:
            result = await self._attach_owned(context, peer, payload, target_fd)
        except BaseException as primary:
            failures = await _attempt_cleanup_steps(
                (lambda: self.runtime.close_target_fd(target_fd),)
            )
            _annotate_cleanup_failures(primary, failures)
            raise
        failures = await _attempt_cleanup_steps((lambda: self.runtime.close_target_fd(target_fd),))
        if failures:
            raise failures[0]
        return result

    async def _attach_owned(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
        target_fd: int,
    ) -> object:
        if not isinstance(payload, AttachPayloadV1):
            raise BrokerRejectedError("attach payload is not closed AttachPayloadV1")
        if (
            payload.gate_ready.expected_parent_pid != peer.pid
            or payload.gate_ready.expected_parent_start_token != peer.start_token
        ):
            raise BrokerRejectedError("gate is not the authenticated peer's direct child")
        existing = self._read_chain(peer.uid, payload.authority)
        if existing.attachment is not None:
            attachment = existing.attachment.record
            if (
                existing.attempt is None
                or existing.attempt.record.gate_ready != payload.gate_ready
                or attachment.peer_pid != peer.pid
                or attachment.peer_pid_start_token != peer.start_token
            ):
                raise BrokerRejectedError("attach replay conflicts with durable authority")
            self._reserve_mutation(context, peer, payload.authority)
            await self._revalidate_live_chain(existing, require_gate=True)
            self._authorities[(peer.uid, payload.authority.launch_nonce_sha256)] = payload.authority
            return self._attachment_projection(attachment)
        if existing.attempt is not None:
            raise BrokerRejectedError("attempt-only authority requires startup reconciliation")
        gate: GateAttachmentEvidence | None = None
        attempt_durable = False
        anchor: AnchorHandle | None = None
        promotion_owned = False
        try:
            gate = await self.runtime.inspect_gate_for_attach(peer, payload, target_fd, self.config)
            self._assert_gate_evidence(payload, gate)
            self._reserve_mutation(context, peer, payload.authority)
            launch_request = build_anchor_launch_request(
                self.config,
                peer.account,
                broker_pid=self.broker_pid,
                broker_start_token=self.broker_start_token,
            )
            anchor = await self.runtime.launch_anchor(launch_request)
            promotion_owned = True
            anchor_identity = await self.runtime.validate_anchor(peer, anchor, "first-stop")
            await self.runtime.verify_stopped_processes(gate, anchor, "before-attempt")
            attempt = self._attempt_record(context, peer, payload, gate, anchor_identity)
            persisted_attempt = self.ledger.publish(peer.uid, payload.authority, attempt)
            attempt_durable = True
            request = ScopeStartRequest(
                unit_name=derive_scope_unit_name(peer.uid, payload.authority.launch_nonce_sha256),
                description=f"mIF QuPath containment uid={peer.uid}",
                gate_pid=payload.gate_ready.gate_pid,
                anchor_pid=anchor_identity.anchor_pid,
                timeout_stop_usec=_seconds_to_usec(self.config.graceful_stop_seconds),
                runtime_max_usec=_seconds_to_usec(self.config.maximum_runtime_seconds),
            )
            scope = await self.runtime.verify_scope(self.manager, request)
            try:
                self._assert_scope_identity(request, scope)
                await self.runtime.verify_stopped_processes(gate, anchor, "after-scope")
                attachment = self._attachment_record(
                    context,
                    peer,
                    payload,
                    gate,
                    anchor_identity,
                    scope,
                    persisted_attempt.record.record_sha256,
                )
            except BaseException as primary:
                failures = await _attempt_cleanup_steps((scope.cgroup.close,))
                _annotate_cleanup_failures(primary, failures)
                raise
            failures = await _attempt_cleanup_steps((scope.cgroup.close,))
            if failures:
                raise failures[0]
            persisted_attachment = self.ledger.publish(peer.uid, payload.authority, attachment)
            authority_key = (peer.uid, payload.authority.launch_nonce_sha256)
            self._authorities[authority_key] = payload.authority
            self._pidfds[authority_key] = AuthorityPidfds(
                gate=gate.gate_pidfd,
                anchor=anchor.pidfd,
            )
            promotion_owned = False
            await self.runtime.promote_anchor(anchor)
            await self.runtime.signal_pidfd(anchor.pidfd, signal.SIGCONT)
            second_identity = await self.runtime.validate_anchor(peer, anchor, "second-stop")
            if second_identity != anchor_identity:
                raise BrokerRejectedError("anchor identity changed during promotion")
            await self.runtime.validate_anchor_descriptors(anchor)
            await self.runtime.signal_pidfd(anchor.pidfd, signal.SIGCONT)
            await self.manager.ref_unit(scope.identity.unit_name)
            return self._attachment_projection(persisted_attachment.record)
        except BaseException as primary:
            # Before attempt durability the private anchor is still broker-owned;
            # after it, recovery must follow the immutable ledger/unit chain.
            cleanup_steps: list[Callable[[], object]] = []
            if anchor is not None and not attempt_durable:
                cleanup = getattr(self.runtime, "kill_private_anchor", None)
                if cleanup is not None:
                    cleanup_steps.append(lambda: cleanup(anchor))
            if gate is not None and not attempt_durable:
                cleanup_steps.append(lambda: self.runtime.close_pidfd(gate.gate_pidfd))
            if anchor is not None and attempt_durable and promotion_owned:
                cleanup_steps.append(lambda: self.runtime.close_anchor_promotion(anchor))
            if gate is not None and anchor is not None and attempt_durable:
                cleanup_steps.append(
                    lambda: self._pidfds.setdefault(
                        (peer.uid, payload.authority.launch_nonce_sha256),
                        AuthorityPidfds(gate.gate_pidfd, anchor.pidfd),
                    )
                )
            failures = await _attempt_cleanup_steps(cleanup_steps)
            _annotate_cleanup_failures(primary, failures)
            raise

    async def release_intent(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
    ) -> bool:
        key = (peer.uid, payload.authority.launch_nonce_sha256)
        try:
            chain = self._load_live(peer.uid, payload.authority)
            attachment = chain.attachment.record
            self._assert_application_peer(peer, attachment)
            if (
                payload.attachment_receipt_sha256 != _receipt_sha256(attachment)
                or payload.attachment_receipt_json_utf8 != attachment.canonical_json_utf8()
            ):
                raise BrokerRejectedError("attachment receipt is not the root ledger copy")
            if chain.release is not None:
                await self._revalidate_live_chain(chain, require_gate=True, gate_phase="target")
                self._reserve_mutation(context, peer, payload.authority)
                self._retire_release_resume(key, wake_waiters=True)
                return False
            running_replay = False
            if chain.release_intent is None:
                await self._revalidate_live_chain(
                    chain, require_gate=True, gate_phase="stopped-gate"
                )
            else:
                if (
                    chain.release_intent.record.application_launch_sha256
                    != payload.application_launch_sha256
                ):
                    raise BrokerConflictError("release intent conflicts with durable request")
                observation = await self._revalidate_live_chain(
                    chain,
                    require_gate=True,
                    gate_phase="release-intent",
                    return_observation=True,
                )
                if observation.gate_phase == "running-gate":
                    running_replay = True
                elif observation.gate_phase != "stopped-gate":
                    raise BrokerAmbiguousError("durable release gate phase is ambiguous")
                elif key in self._release_resume_signalled:
                    raise BrokerAmbiguousError(
                        "successfully resumed gate returned to a stopped phase"
                    )
            self._reserve_mutation(context, peer, payload.authority)
            if running_replay:
                if key not in self._release_resume_signalled:
                    self._retire_release_claim(key)
                self._release_events.setdefault(key, asyncio.Event())
                return False
            if chain.release_intent is None:
                intent = self._successor_record(
                    BrokerReleaseIntentRecordV1,
                    attachment,
                    application_launch_sha256=payload.application_launch_sha256,
                    attachment_sha256=attachment.record_sha256,
                )
                self.ledger.publish(peer.uid, payload.authority, intent)
            self._release_events.setdefault(key, asyncio.Event())
            if key in self._release_resume_owned:
                return False
            self._release_resume_owned.add(key)
            return True
        except BaseException:
            if key in self._release_resume_owned and key not in self._release_resume_signalled:
                self._retire_release_resume(key)
            raise

    async def release_resume(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
    ) -> None:
        key = (peer.uid, payload.authority.launch_nonce_sha256)
        handles = None
        gate_pidfd = None
        signal_succeeded = False
        primary_error: BaseException | None = None
        try:
            chain = self._load_live(peer.uid, payload.authority)
            self._assert_application_peer(peer, chain.attachment.record)
            await self._revalidate_live_chain(chain, require_gate=True, gate_phase="stopped-gate")
            attachment = chain.attachment.record
            handles = self._pidfds.get(key)
            gate_pidfd = (
                handles.gate if handles is not None else await self.runtime.gate_pidfd(attachment)
            )
            await self.runtime.signal_pidfd(gate_pidfd, signal.SIGCONT)
            signal_succeeded = True
            self._mark_release_resume_signalled(key)
        except BaseException as error:
            primary_error = error
            if not signal_succeeded:
                self._retire_release_resume(key)
            raise
        finally:
            if handles is None and gate_pidfd is not None:
                failures = await _attempt_cleanup_steps(
                    (lambda: self.runtime.close_pidfd(gate_pidfd),)
                )
                if primary_error is not None:
                    _annotate_cleanup_failures(primary_error, failures)
                elif failures:
                    raise failures[0]

    async def release_wait(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
    ) -> object:
        key = (peer.uid, payload.authority.launch_nonce_sha256)
        chain = self._load_live(peer.uid, payload.authority)
        if chain.release is not None:
            self._retire_release_resume(key, wake_waiters=True)
            return self._release_projection(chain)
        event = self._release_events.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=self.config.maximum_runtime_seconds)
            chain = self._load_live(peer.uid, payload.authority)
            if chain.release is None:
                raise BrokerRejectedError("gate claim did not author a release")
            return self._release_projection(chain)
        finally:
            if self._release_events.get(key) is event:
                self._release_events.pop(key, None)

    async def gate_authorize_exec(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        payload: object,
    ) -> GateExecAuthorizationV1:
        if not isinstance(payload, GateAuthorizeExecPayloadV1):
            raise BrokerRejectedError("gate authorization payload is malformed")
        key = (peer.uid, payload.launch_nonce_sha256)
        authority = self._authorities.get(key)
        if authority is None:
            raise BrokerRejectedError("no current authority exists for gate claim")
        chain = self._load_live(peer.uid, authority)
        if chain.attachment is None or chain.release_intent is None:
            raise BrokerRejectedError("release intent is not durable")
        attachment = chain.attachment.record
        intent = chain.release_intent.record
        await self._revalidate_live_chain(chain, require_gate=True, gate_phase="running-gate")
        if (
            peer.pid != attachment.gate_pid
            or peer.start_token != attachment.gate_start_token
            or peer.uid != attachment.peer_uid
            or peer.gid != attachment.peer_gid
            or chain.attempt is None
            or payload.gate_material_sha256 != chain.attempt.record.gate_ready.gate_material_sha256
        ):
            raise BrokerRejectedError("caller is not the exact stopped gate")
        self._reserve_mutation(context, peer, authority)
        authorization = self._gate_authorization(
            attachment, intent, chain.attempt.record.gate_ready
        )
        if chain.release is not None:
            if chain.release.record.gate_authorization_sha256 != authorization.authorization_sha256:
                raise BrokerRejectedError("gate authorization replay conflicts with ledger")
            self._retire_release_resume(key, wake_waiters=True)
            return authorization
        release = self._successor_record(
            BrokerReleaseRecordV1,
            attachment,
            attachment_sha256=attachment.record_sha256,
            release_intent_sha256=intent.record_sha256,
            gate_authorization_sha256=authorization.authorization_sha256,
        )
        self.ledger.publish(peer.uid, authority, release)
        self._retire_release_resume(key, wake_waiters=True)
        return authorization

    async def inspect(self, context: BrokerContext, peer: PeerIdentity, payload: object) -> object:
        chain = self._read_chain(peer.uid, payload.authority)
        if chain.attachment is None:
            raise BrokerRejectedError("authority has no durable attachment")
        if chain.drain is None:
            disposition = await self._revalidate_live_chain(chain, require_gate=False)
            if disposition is ReconciledAttachmentDisposition.ANCHOR_LOST:
                return self._inspection_projection(chain, ContainmentSemanticState.AMBIGUOUS)
            return self._inspection_projection(chain, ContainmentSemanticState.LIVE)
        return self._inspection_projection(chain, ContainmentSemanticState.DRAINED)

    async def await_drain(
        self, context: BrokerContext, peer: PeerIdentity, payload: object
    ) -> object:
        chain = self._load_attachment(peer.uid, payload.authority)
        self._assert_application_peer(peer, chain.attachment.record)
        if chain.drain is not None:
            await self._cleanup_drained(chain.attachment.record, peer.uid, payload.authority)
            return self._drain_projection(chain)
        if chain.release is None:
            raise BrokerRejectedError("leader exit requires a durable release")
        await self._revalidate_live_chain(chain, require_gate=True)
        key = (peer.uid, payload.authority.launch_nonce_sha256)
        handles = self._pidfds.get(key)
        if handles is None or handles.gate is None:
            raise BrokerAmbiguousError("application pidfd authority is unavailable")
        await self.runtime.await_pidfd_exit(handles.gate, payload.timeout_milliseconds / 1000.0)
        chain = self._load_attachment(peer.uid, payload.authority)
        self._assert_application_peer(peer, chain.attachment.record)
        await self._revalidate_live_chain(chain, require_gate=False)
        reason = payload.termination_reason
        if reason not in ("normal_exit", "nonzero_exit"):
            raise BrokerRejectedError("leader exit report is invalid")
        self._reserve_mutation(context, peer, payload.authority)
        return await self.stop_authority(
            context,
            peer,
            payload.authority,
            termination_reason=reason,
            timeout_seconds=payload.timeout_milliseconds / 1000.0,
        )

    async def stop(self, context: BrokerContext, peer: PeerIdentity, payload: object) -> object:
        chain = self._load_attachment(peer.uid, payload.authority)
        self._assert_application_peer(peer, chain.attachment.record)
        if chain.drain is not None:
            await self._cleanup_drained(chain.attachment.record, peer.uid, payload.authority)
            return self._drain_projection(chain)
        await self._revalidate_live_chain(chain, require_gate=False)
        self._reserve_mutation(context, peer, payload.authority)
        timeout = (
            payload.graceful_timeout_milliseconds + payload.forced_timeout_milliseconds
        ) / 1000.0
        return await self.stop_authority(
            context,
            peer,
            payload.authority,
            termination_reason=payload.termination_reason,
            timeout_seconds=timeout,
        )

    async def recover_stop(
        self, context: BrokerContext, peer: PeerIdentity, payload: object
    ) -> object:
        chain = self._load_attachment(peer.uid, payload.authority)
        attachment = chain.attachment.record
        if peer.uid != attachment.peer_uid or peer.gid != attachment.peer_gid:
            raise BrokerRejectedError("recovery caller is not the attachment account")
        if (
            payload.original_owner_pid != attachment.peer_pid
            or payload.original_owner_start_token != attachment.peer_pid_start_token
        ):
            raise BrokerRejectedError("recover_stop original owner is not exact")
        if await self._observe_owner_liveness(
            payload.original_owner_pid, payload.original_owner_start_token
        ):
            raise BrokerRejectedError("exact original application owner remains live")
        if chain.drain is not None:
            await self._cleanup_drained(attachment, peer.uid, payload.authority)
            return self._drain_projection(chain)
        await self._revalidate_live_chain(chain, require_gate=False)
        takeover_reason = await self._dead_owner_terminal_reason(
            attachment,
            payload.authority,
            current_peer=peer,
            context=context,
        )
        if takeover_reason is None:
            self._reserve_mutation(context, peer, payload.authority)
        timeout = (
            payload.graceful_timeout_milliseconds + payload.forced_timeout_milliseconds
        ) / 1000.0
        return await self.stop_authority(
            context,
            peer,
            payload.authority,
            termination_reason=takeover_reason or "recovery_stop",
            timeout_seconds=timeout,
            recovery=True,
        )

    async def _dead_owner_terminal_reason(
        self,
        attachment: object,
        authority: object,
        *,
        current_peer: object,
        context: object,
    ) -> str | None:
        persisted = self.ledger.load_request_reservations(attachment.peer_uid, authority)
        terminal = tuple(
            item.reservation
            for item in persisted
            if item.reservation.slot is ContainmentRequestReservationSlot.TERMINAL
        )
        if not terminal:
            return None
        if len(terminal) != 1:
            raise BrokerAmbiguousError("terminal reservation state is ambiguous")
        reservation = terminal[0]
        try:
            request = BrokerRequestV1.from_mapping(
                loads_canonical_packet(reservation.canonical_request_json_utf8.encode("utf-8"))
            )
        except (TypeError, ValueError) as error:
            raise BrokerAmbiguousError("terminal reservation is malformed") from error
        if (
            reservation.operation is not request.operation
            or reservation.authenticated_uid != attachment.peer_uid
            or reservation.primary_gid != attachment.peer_gid
            or getattr(request.payload, "authority", None) != authority
        ):
            raise BrokerAmbiguousError("terminal reservation authority is ambiguous")
        if request.operation in (BrokerOperation.AWAIT_DRAIN, BrokerOperation.STOP):
            if (
                reservation.peer_pid != attachment.peer_pid
                or reservation.peer_start_token != attachment.peer_pid_start_token
            ):
                raise BrokerAmbiguousError("application terminal reservation owner is ambiguous")
            reason = getattr(request.payload, "termination_reason", None)
        elif request.operation is BrokerOperation.RECOVER_STOP:
            if (
                request.payload.original_owner_pid != attachment.peer_pid
                or request.payload.original_owner_start_token != attachment.peer_pid_start_token
            ):
                raise BrokerAmbiguousError("recovery reservation original owner is ambiguous")
            same_owner = (
                reservation.peer_pid == current_peer.pid
                and reservation.peer_start_token == current_peer.start_token
            )
            if same_owner:
                current_request = getattr(context, "authenticated_request", None)
                canonical = getattr(context, "canonical_request_bytes", None)
                if (
                    type(current_request) is not BrokerRequestV1
                    or type(canonical) is not bytes
                    or current_request != request
                    or canonical.decode("utf-8", errors="strict")
                    != reservation.canonical_request_json_utf8
                ):
                    raise BrokerConflictError("recovery reservation replay is not exact")
            elif await self._observe_owner_liveness(
                reservation.peer_pid, reservation.peer_start_token
            ):
                raise BrokerAmbiguousError("recovery reservation owner remains live")
            reason = "recovery_stop"
        else:
            raise BrokerAmbiguousError("terminal reservation operation is incompatible")
        if type(reason) is not str:
            raise BrokerAmbiguousError("terminal reservation reason is malformed")
        return reason

    async def _observe_owner_liveness(self, pid: int, start_token: str) -> bool:
        try:
            observed = await self.runtime.original_owner_is_live(pid, start_token)
        except asyncio.CancelledError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise BrokerAmbiguousError("process owner liveness is ambiguous") from error
        if type(observed) is not bool:
            raise BrokerAmbiguousError("process owner liveness is malformed")
        return observed

    async def stop_authority(
        self,
        context: BrokerContext,
        peer: PeerIdentity,
        authority: object,
        *,
        termination_reason: str,
        timeout_seconds: float,
        recovery: bool = False,
        _drain_observer: object | None = None,
        _close_drain_observer: bool = True,
    ) -> object:
        chain = self._read_chain(peer.uid, authority)
        if chain.attachment is None:
            raise BrokerRejectedError("authority has no durable attachment")
        attachment = chain.attachment.record
        if recovery:
            if peer.uid != attachment.peer_uid or peer.gid != attachment.peer_gid:
                raise BrokerRejectedError("recovery caller is not the attachment account")
        else:
            self._assert_application_peer(peer, attachment)
        if chain.drain is not None:
            await self._cleanup_drained(attachment, peer.uid, authority)
            return self._drain_projection(chain)
        disposition = await self._revalidate_live_chain(chain, require_gate=False)
        anchor_lost = disposition is ReconciledAttachmentDisposition.ANCHOR_LOST
        observer = (
            _drain_observer
            if _drain_observer is not None
            else await self.runtime.bind_drain_observer(attachment)
        )
        primary_error: BaseException | None = None
        try:
            loop = asyncio.get_running_loop()
            stop_deadline = loop.time() + float(timeout_seconds)
            graceful_deadline = min(
                stop_deadline,
                loop.time() + self.config.graceful_stop_seconds,
            )

            def remaining(deadline: float) -> float:
                return max(0.001, deadline - loop.time())

            release_intent_sha256 = (
                None if chain.release_intent is None else chain.release_intent.record.record_sha256
            )
            release_sha256 = None if chain.release is None else chain.release.record.record_sha256
            if chain.stop_intent is None:
                intent = self._successor_record(
                    BrokerStopIntentRecordV1,
                    attachment,
                    attachment_sha256=attachment.record_sha256,
                    release_intent_sha256=release_intent_sha256,
                    release_sha256=release_sha256,
                    termination_reason=termination_reason,
                    stop_request="systemd-stop-unit",
                    stop_job_mode="replace",
                )
                persisted_intent = self.ledger.publish(peer.uid, authority, intent)
            else:
                persisted_intent = chain.stop_intent
                if persisted_intent.record.termination_reason != termination_reason:
                    raise BrokerRejectedError("stop conflicts with durable stop intent")
            stop_job = await self.manager.begin_stop_unit(
                attachment.unit_name, attachment.invocation_id
            )
            authority_key = (peer.uid, authority.launch_nonce_sha256)
            handles = self._pidfds.get(authority_key)
            if anchor_lost:
                await self.manager.kill_unit_all(attachment.unit_name, attachment.invocation_id)
            else:
                try:
                    anchor_pidfd = (
                        handles.anchor
                        if handles is not None
                        else await self.runtime.anchor_pidfd(attachment)
                    )
                except FileNotFoundError:
                    await self.manager.kill_unit_all(attachment.unit_name, attachment.invocation_id)
                else:
                    if handles is not None:
                        await self.runtime.signal_pidfd(anchor_pidfd, signal.SIGUSR1)
                    else:
                        try:
                            await self.runtime.signal_pidfd(anchor_pidfd, signal.SIGUSR1)
                        except BaseException as primary:
                            closing_anchor_pidfd = anchor_pidfd
                            anchor_pidfd = None
                            failures = await _attempt_cleanup_steps(
                                (lambda: self.runtime.close_pidfd(closing_anchor_pidfd),)
                            )
                            _annotate_cleanup_failures(primary, failures)
                            raise
                        closing_anchor_pidfd = anchor_pidfd
                        anchor_pidfd = None
                        failures = await _attempt_cleanup_steps(
                            (lambda: self.runtime.close_pidfd(closing_anchor_pidfd),)
                        )
                        if failures:
                            raise failures[0]
            forced = anchor_lost
            try:
                observed_job = await asyncio.wait_for(
                    self.manager.await_stop_job(stop_job),
                    timeout=remaining(graceful_deadline),
                )
            except asyncio.TimeoutError:
                if not forced:
                    await self.manager.kill_unit_all(attachment.unit_name, attachment.invocation_id)
                    forced = True
                observed_job = await asyncio.wait_for(
                    self.manager.await_stop_job(stop_job),
                    timeout=remaining(stop_deadline),
                )
            if not isinstance(observed_job, SystemdStopJobEvidenceV1):
                raise BrokerRejectedError("live stop requires exact completed job evidence")
            if forced:
                proof = await asyncio.wait_for(
                    self.runtime.await_bound_drain_proof(observer, remaining(stop_deadline)),
                    timeout=remaining(stop_deadline),
                )
            else:
                try:
                    proof = await asyncio.wait_for(
                        self.runtime.await_bound_drain_proof(
                            observer, remaining(graceful_deadline)
                        ),
                        timeout=remaining(graceful_deadline),
                    )
                except asyncio.TimeoutError:
                    await self.manager.kill_unit_all(attachment.unit_name, attachment.invocation_id)
                    proof = await asyncio.wait_for(
                        self.runtime.await_bound_drain_proof(observer, remaining(stop_deadline)),
                        timeout=remaining(stop_deadline),
                    )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if _close_drain_observer:
                cleanup_failures = await _attempt_cleanup_steps(
                    (lambda: self.runtime.close_drain_observer(observer),)
                )
                if primary_error is not None:
                    _annotate_cleanup_failures(primary_error, cleanup_failures)
                elif cleanup_failures:
                    raise cleanup_failures[0]
        if (
            not isinstance(proof, (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1))
            or proof.cgroup_identity != attachment.cgroup_identity
        ):
            raise BrokerRejectedError("drain proof does not bind the exact cgroup")
        drain = self._successor_record(
            BrokerDrainRecordV1,
            attachment,
            attachment_sha256=attachment.record_sha256,
            release_sha256=release_sha256,
            stop_intent_sha256=persisted_intent.record.record_sha256,
            termination_reason=termination_reason,
            stop_job=observed_job,
            proof=proof,
        )
        persisted_drain = self.ledger.publish(peer.uid, authority, drain)
        await self._cleanup_drained(attachment, peer.uid, authority)
        final_chain = self._read_chain(peer.uid, authority)
        if (
            final_chain.drain is None
            or final_chain.drain.record.record_sha256 != persisted_drain.record.record_sha256
        ):
            raise BrokerRejectedError("durable drain replay conflicts with publication")
        return self._drain_projection(final_chain)

    async def _cleanup_drained(
        self, attachment: object, authenticated_uid: int, authority: object
    ) -> None:
        """Retire one durable authority without allowing one cleanup failure to skip another."""

        key = (authenticated_uid, authority.launch_nonce_sha256)
        failures: list[BaseException] = []
        try:
            await self.manager.unref_unit(attachment.unit_name)
        except BaseException as error:
            failures.append(error)
        handles = self._pidfds.get(key)
        if handles is not None:
            for name in ("gate", "anchor"):
                descriptor = getattr(handles, name)
                if descriptor is None:
                    continue
                # close(2) consumes numeric ownership even when it reports an
                # error; retire the field before the sole close attempt.
                setattr(handles, name, None)
                try:
                    await self.runtime.close_pidfd(descriptor)
                except BaseException as error:
                    failures.append(error)
            self._pidfds.pop(key, None)
        self._authorities.pop(key, None)
        self._retire_release_resume(key, wake_waiters=True)
        retire = getattr(self.runtime, "retire_authority", None)
        if retire is not None:
            try:
                result = retire(attachment)
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                failures.append(error)
        if failures:
            raise failures[0]

    async def resume_durable_stop(
        self,
        authenticated_uid: int,
        authority: object,
        *,
        timeout_seconds: float,
        _drain_observer: object | None = None,
        _close_drain_observer: bool = True,
    ) -> object:
        """Resume an exact startup-discovered stop intent without a live owner."""

        chain = self._read_chain(authenticated_uid, authority)
        if chain.attachment is None or chain.stop_intent is None:
            raise BrokerRejectedError("startup recovery requires a durable stop intent")
        attachment = chain.attachment.record
        peer = _RecoveryPeer(
            uid=authenticated_uid,
            gid=attachment.peer_gid,
        )
        return await self.stop_authority(
            None,
            peer,
            authority,
            termination_reason=chain.stop_intent.record.termination_reason,
            timeout_seconds=timeout_seconds,
            recovery=True,
            _drain_observer=_drain_observer,
            _close_drain_observer=_close_drain_observer,
        )

    async def recover_missing_stop(
        self,
        authenticated_uid: int,
        authority: object,
        *,
        termination_reason: str,
        timeout_seconds: float,
    ) -> object:
        """Bind recovery proof before publishing its missing stop boundary."""

        chain = self._load_attachment(authenticated_uid, authority)
        if chain.drain is not None or chain.stop_intent is not None:
            raise BrokerRejectedError("missing-stop recovery chain is not exact")
        attachment = chain.attachment.record
        await self._revalidate_live_chain(chain, require_gate=False)
        observer = await self.runtime.bind_drain_observer(attachment)
        primary_error: BaseException | None = None
        try:
            chain = self._load_attachment(authenticated_uid, authority)
            await self._revalidate_live_chain(chain, require_gate=False)
            self.author_recovery_stop_intent(
                authenticated_uid,
                authority,
                termination_reason=termination_reason,
            )
            return await self.resume_durable_stop(
                authenticated_uid,
                authority,
                timeout_seconds=timeout_seconds,
                _drain_observer=observer,
                _close_drain_observer=False,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_failures = await _attempt_cleanup_steps(
                (lambda: self.runtime.close_drain_observer(observer),)
            )
            if primary_error is not None:
                _annotate_cleanup_failures(primary_error, cleanup_failures)
            elif cleanup_failures:
                raise cleanup_failures[0]

    def author_recovery_stop_intent(
        self,
        authenticated_uid: int,
        authority: object,
        *,
        termination_reason: str,
    ) -> object:
        """Persist the recovery stop boundary before any host stop effect."""

        chain = self._read_chain(authenticated_uid, authority)
        if chain.attachment is None or chain.drain is not None:
            raise BrokerRejectedError("recovery stop authority is not attached")
        if chain.stop_intent is not None:
            if chain.stop_intent.record.termination_reason != termination_reason:
                raise BrokerRejectedError("recovery stop intent conflicts with ledger")
            return chain.stop_intent.record
        attachment = chain.attachment.record
        release_intent_sha256 = (
            None if chain.release_intent is None else chain.release_intent.record.record_sha256
        )
        release_sha256 = None if chain.release is None else chain.release.record.record_sha256
        intent = self._successor_record(
            BrokerStopIntentRecordV1,
            attachment,
            attachment_sha256=attachment.record_sha256,
            release_intent_sha256=release_intent_sha256,
            release_sha256=release_sha256,
            termination_reason=termination_reason,
            stop_request="systemd-stop-unit",
            stop_job_mode="replace",
        )
        return self.ledger.publish(authenticated_uid, authority, intent).record

    def author_reconciled_drain(
        self,
        authenticated_uid: int,
        authority: object,
        *,
        proof: CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1,
    ) -> object:
        """Author a jobless drain only after exact pruned-cgroup proof."""

        chain = self._read_chain(authenticated_uid, authority)
        if chain.attachment is None or chain.stop_intent is None or chain.drain is not None:
            raise BrokerRejectedError("pruned recovery chain is incomplete")
        attachment = chain.attachment.record
        if proof.cgroup_identity != attachment.cgroup_identity:
            raise BrokerRejectedError("recovery drain proof changed cgroup identity")
        release_sha256 = None if chain.release is None else chain.release.record.record_sha256
        drain = self._successor_record(
            BrokerDrainRecordV1,
            attachment,
            attachment_sha256=attachment.record_sha256,
            release_sha256=release_sha256,
            stop_intent_sha256=chain.stop_intent.record.record_sha256,
            termination_reason=chain.stop_intent.record.termination_reason,
            stop_job=None,
            proof=proof,
        )
        return self.ledger.publish(authenticated_uid, authority, drain).record

    def author_reboot_drain(
        self,
        authenticated_uid: int,
        authority: object,
        *,
        current_boot_id: str,
    ) -> object:
        """Author the protocol's exact drain for a complete prior-boot chain."""

        chain = self._read_chain(authenticated_uid, authority)
        if (
            chain.attempt is None
            or chain.attachment is None
            or chain.stop_intent is not None
            or chain.drain is not None
        ):
            raise BrokerRejectedError("prior-boot recovery chain is not exact")
        attachment = chain.attachment.record
        proof = MachineRebootProofV1(
            proof_kind="machine-reboot",
            prior_boot_id=attachment.boot_id,
            current_boot_id=current_boot_id,
        )
        release_sha256 = None if chain.release is None else chain.release.record.record_sha256
        drain = self._successor_record(
            BrokerDrainRecordV1,
            attachment,
            attachment_sha256=attachment.record_sha256,
            release_sha256=release_sha256,
            stop_intent_sha256=None,
            termination_reason="reboot",
            stop_job=None,
            proof=proof,
        )
        return self.ledger.publish(authenticated_uid, authority, drain).record

    async def register_reconciled_authority(
        self,
        peer_uid: int,
        authority: object,
        attachment: BrokerAttachmentRecordV1,
    ) -> None:
        """Retain both exact live pidfds before exposing a restart authority."""

        gate_pidfd = await self.runtime.gate_pidfd(attachment)
        try:
            anchor_pidfd = await self.runtime.anchor_pidfd(attachment)
        except BaseException as primary:
            failures = await _attempt_cleanup_steps((lambda: self.runtime.close_pidfd(gate_pidfd),))
            _annotate_cleanup_failures(primary, failures)
            raise
        key = (peer_uid, authority.launch_nonce_sha256)
        existing = self._pidfds.get(key)
        if existing is not None:
            primary = BrokerRejectedError("reconciled authority pidfds were already retained")
            failures = await _attempt_cleanup_steps(
                (
                    lambda: self.runtime.close_pidfd(gate_pidfd),
                    lambda: self.runtime.close_pidfd(anchor_pidfd),
                )
            )
            _annotate_cleanup_failures(primary, failures)
            raise primary
        self._pidfds[key] = AuthorityPidfds(gate_pidfd, anchor_pidfd)
        self._authorities[key] = authority

    def _attempt_record(self, context, peer, payload, gate, anchor_identity):
        material = self._root_material(
            context,
            peer,
            payload.authority,
            gate,
            anchor_identity,
            schema_name="mif-containment-broker-attempt",
            scope=None,
        )
        material["gate_ready"] = payload.gate_ready.to_mapping()
        return _signed_record(BrokerAttemptRecordV1, material)

    def _attachment_record(
        self, context, peer, payload, gate, anchor_identity, scope, attempt_sha256
    ):
        material = self._root_material(
            context,
            peer,
            payload.authority,
            gate,
            anchor_identity,
            schema_name="mif-containment-broker-attachment",
            scope=scope,
        )
        material["attempt_sha256"] = attempt_sha256
        return _signed_record(BrokerAttachmentRecordV1, material)

    def _root_material(
        self, context, peer, authority, gate, anchor_identity, *, schema_name, scope
    ) -> dict[str, object]:
        certification = context.certification
        identity = None if scope is None else scope.identity
        cgroup_identity = None if scope is None else scope.cgroup.identity
        return {
            "schema_name": schema_name,
            "schema_version": 1,
            "protocol_version": PROTOCOL_VERSION,
            "broker_build_id": certification.broker_build_id,
            "broker_config_sha256": certification.broker_config_sha256,
            "boot_id": certification.boot_id,
            "peer_uid": peer.uid,
            "peer_gid": peer.gid,
            "peer_pid": peer.pid,
            "peer_pid_start_token": peer.start_token,
            "authority": authority.to_mapping(),
            "executable_registration_id": gate.registration_id,
            "target_executable_sha256": gate.retained_target_sha256,
            "gate_pid": gate.gate_ready.gate_pid,
            "gate_start_token": gate.gate_ready.gate_start_token,
            "anchor_identity": anchor_identity.to_mapping(),
            "unit_name": None if identity is None else identity.unit_name,
            "invocation_id": None if identity is None else identity.invocation_id,
            "control_group": None if identity is None else identity.control_group,
            "cgroup_identity": None if cgroup_identity is None else cgroup_identity.to_mapping(),
            "scope_policy_sha256": certification.scope_policy_sha256,
            "timestamp_unix_ns": self.clock_ns(),
        }

    def _successor_record(self, record_type, attachment, **extra):
        material = attachment.to_mapping()
        material.pop("attempt_sha256", None)
        material.pop("record_sha256")
        material["schema_name"] = record_type.SCHEMA_NAME
        material["timestamp_unix_ns"] = self.clock_ns()
        material.update({name: _wire_value(value) for name, value in extra.items()})
        return _signed_record(record_type, material)

    def _load_live(self, uid: int, authority: object):
        chain = self._load_attachment(uid, authority)
        if chain.drain is not None:
            raise BrokerRejectedError("authority is not a live durable attachment")
        return chain

    def _load_attachment(self, uid: int, authority: object):
        chain = self._read_chain(uid, authority)
        if chain.attachment is None:
            raise BrokerRejectedError("authority has no durable attachment")
        attachment = chain.attachment.record
        if attachment.peer_uid != uid or attachment.authority != authority:
            raise BrokerRejectedError("authenticated authority differs from ledger")
        return chain

    def _read_chain(self, uid: int, authority: object):
        chain = self.ledger.load_chain(uid, authority)
        if chain.state is ContainmentLedgerState.INCOMPATIBLE:
            raise BrokerIncompatibleError("durable authority schema is unsupported")
        if chain.state is ContainmentLedgerState.AMBIGUOUS:
            raise BrokerAmbiguousError("durable authority state is ambiguous")
        return chain

    async def _revalidate_live_chain(
        self,
        chain: object,
        *,
        require_gate: bool,
        gate_phase: str | None = None,
        return_observation: bool = False,
    ) -> ReconciledAttachmentDisposition | ReconciledAttachmentObservation:
        try:
            result = await self._observe_live_chain(
                chain, require_gate=require_gate, gate_phase=gate_phase
            )
            if return_observation:
                if not isinstance(result, ReconciledAttachmentObservation):
                    raise BrokerRejectedError("live host observation omitted the exact gate phase")
                return result
            if isinstance(result, ReconciledAttachmentObservation):
                return result.disposition
            return result
        except asyncio.CancelledError:
            raise
        except BrokerAmbiguousError:
            raise
        except (
            BrokerRejectedError,
            CgroupVerificationError,
            SystemdVerificationError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise BrokerAmbiguousError("live host authority observation is ambiguous") from error

    async def _observe_live_chain(
        self,
        chain: object,
        *,
        require_gate: bool,
        gate_phase: str | None = None,
    ) -> ReconciledAttachmentDisposition | ReconciledAttachmentObservation:
        attachment = chain.attachment.record
        identity = await self.manager.read_unit_identity(attachment.unit_name)
        expected = (
            attachment.unit_name,
            attachment.invocation_id,
            attachment.control_group,
            SCOPE_SLICE,
            "control-group",
            _seconds_to_usec(self.config.graceful_stop_seconds),
            True,
            "inactive-or-failed",
            _seconds_to_usec(self.config.maximum_runtime_seconds),
            False,
            "",
        )
        observed = (
            getattr(identity, "unit_name", None),
            getattr(identity, "invocation_id", None),
            getattr(identity, "control_group", None),
            getattr(identity, "slice", None),
            getattr(identity, "kill_mode", None),
            getattr(identity, "timeout_stop_usec", None),
            getattr(identity, "send_sigkill", None),
            getattr(identity, "collect_mode", None),
            getattr(identity, "runtime_max_usec", None),
            getattr(identity, "delegate", None),
            getattr(identity, "controller", None),
        )
        if observed != expected:
            raise BrokerRejectedError("live unit authority identity was substituted")
        if gate_phase is None:
            gate_phase = (
                "target"
                if chain.release is not None
                else "release-intent" if chain.release_intent is not None else "stopped-gate"
            )
        disposition = await self.runtime.verify_reconciled_attachment(
            attachment,
            identity,
            attempt=None if chain.attempt is None else chain.attempt.record,
            gate_phase=gate_phase,
        )
        if isinstance(disposition, ReconciledAttachmentObservation):
            raw_disposition = disposition.disposition
            if gate_phase not in (None, "release-intent") and disposition.gate_phase != gate_phase:
                raise BrokerRejectedError("live gate phase differs from required phase")
        else:
            raw_disposition = disposition
        if not isinstance(raw_disposition, ReconciledAttachmentDisposition):
            raise BrokerRejectedError("live process/cgroup observation is malformed")
        _assert_reconciled_member_set(attachment, identity, raw_disposition, gate_phase)
        if require_gate and raw_disposition is not ReconciledAttachmentDisposition.LIVE:
            raise BrokerRejectedError("required live gate/anchor phase is unavailable")
        return disposition

    @staticmethod
    def _assert_application_peer(peer: object, attachment: object) -> None:
        if (
            peer.uid != attachment.peer_uid
            or peer.gid != attachment.peer_gid
            or peer.pid != attachment.peer_pid
            or peer.start_token != attachment.peer_pid_start_token
        ):
            raise BrokerRejectedError("caller is not the exact application peer")

    @staticmethod
    def _assert_gate_evidence(payload, gate) -> None:
        if not isinstance(gate, GateAttachmentEvidence) or gate.gate_ready != payload.gate_ready:
            raise BrokerRejectedError("gate observation differs from passed GateReadyV1")
        ready = payload.gate_ready
        if (
            gate.retained_target_device != ready.target_executable_device
            or gate.retained_target_inode != ready.target_executable_inode
            or gate.retained_target_sha256 != ready.target_executable_sha256
        ):
            raise BrokerRejectedError("retained target descriptor identity changed")

    @staticmethod
    def _assert_scope_identity(request, scope) -> None:
        identity = getattr(scope, "identity", None)
        cgroup = getattr(scope, "cgroup", None)
        if (
            identity is None
            or cgroup is None
            or identity.unit_name != request.unit_name
            or len(tuple(identity.pids)) != 2
            or frozenset(identity.pids) != frozenset((request.gate_pid, request.anchor_pid))
            or identity.slice != SCOPE_SLICE
            or identity.delegate is not False
            or identity.controller != ""
            or identity.kill_mode != "control-group"
            or identity.send_sigkill is not True
            or identity.collect_mode != "inactive-or-failed"
            or identity.timeout_stop_usec != request.timeout_stop_usec
            or identity.runtime_max_usec != request.runtime_max_usec
            or identity.control_group
            != "/" + "/".join(item.basename for item in cgroup.identity.components)
        ):
            raise BrokerRejectedError("systemd scope identity or policy is not exact")

    @staticmethod
    def _attachment_projection(record) -> dict[str, object]:
        return LinuxSystemdScopeAttachmentV1(
            provider=PROVIDER,
            protocol_version=PROTOCOL_VERSION,
            broker_build_id=record.broker_build_id,
            broker_config_sha256=record.broker_config_sha256,
            boot_id=record.boot_id,
            peer_uid=record.peer_uid,
            peer_gid=record.peer_gid,
            unit_name=record.unit_name,
            invocation_id=record.invocation_id,
            control_group=record.control_group,
            scope_policy_sha256=record.scope_policy_sha256,
            attachment_receipt_json_utf8=record.canonical_json_utf8(),
            attachment_receipt_sha256=_receipt_sha256(record),
        ).to_mapping()

    @staticmethod
    def _release_projection(chain) -> dict[str, object]:
        if any(
            item is None
            for item in (
                chain.attempt,
                chain.attachment,
                chain.release_intent,
                chain.release,
            )
        ):
            raise BrokerRejectedError("release chain is incomplete")
        attempt = chain.attempt.record
        attachment = chain.attachment.record
        release_intent = chain.release_intent.record
        release = chain.release.record
        return ReleaseReceiptV1(
            launch_nonce_sha256=release.authority.launch_nonce_sha256,
            attachment_receipt_sha256=_receipt_sha256(attachment),
            release_intent_sha256=_receipt_sha256(release_intent),
            release_receipt_sha256=_receipt_sha256(release),
            gate_material_sha256=attempt.gate_ready.gate_material_sha256,
            application_request_sha256=(release.authority.application_request_sha256),
            released_at_unix_ns=release.timestamp_unix_ns,
        ).to_mapping()

    @staticmethod
    def _drain_projection(chain) -> dict[str, object]:
        if chain.attachment is None or chain.drain is None:
            raise BrokerRejectedError("drain chain is incomplete")
        attachment = chain.attachment.record
        drain = chain.drain.record
        release_json = None
        release_digest = None
        if drain.release_sha256 is not None:
            if chain.release is None:
                raise BrokerRejectedError("drain release chain is incomplete")
            release = chain.release.record
            release_json = release.canonical_json_utf8()
            release_digest = _receipt_sha256(release)
        return LinuxSystemdScopeDrainV1(
            provider=PROVIDER,
            semantic_state="drained",
            attachment_receipt_sha256=_receipt_sha256(attachment),
            release_receipt_json_utf8=release_json,
            release_receipt_sha256=release_digest,
            drain_receipt_json_utf8=drain.canonical_json_utf8(),
            drain_receipt_sha256=_receipt_sha256(drain),
            termination_reason=drain.termination_reason,
        ).to_mapping()

    def _inspection_projection(
        self,
        chain,
        semantic_state: ContainmentSemanticState,
    ) -> dict[str, object]:
        if chain.attachment is None:
            raise BrokerRejectedError("inspection chain is incomplete")
        attachment = LinuxSystemdScopeAttachmentV1.from_mapping(
            self._attachment_projection(chain.attachment.record)
        )
        drain = None
        if semantic_state is ContainmentSemanticState.DRAINED:
            drain = LinuxSystemdScopeDrainV1.from_mapping(self._drain_projection(chain))
        return ContainmentInspection(
            provider=PROVIDER,
            protocol_version=PROTOCOL_VERSION,
            semantic_state=semantic_state,
            attachment=attachment,
            drain=drain,
        ).to_mapping()

    @staticmethod
    def _gate_authorization(attachment, intent, gate_ready) -> GateExecAuthorizationV1:
        material = {
            "launch_nonce_sha256": attachment.authority.launch_nonce_sha256,
            "gate_pid": attachment.gate_pid,
            "gate_start_token": attachment.gate_start_token,
            "gate_material_sha256": gate_ready.gate_material_sha256,
            "attachment_receipt_json_utf8": attachment.canonical_json_utf8(),
            "attachment_receipt_sha256": _receipt_sha256(attachment),
            "release_intent_json_utf8": intent.canonical_json_utf8(),
            "release_intent_sha256": _receipt_sha256(intent),
            "target_executable_sha256": attachment.target_executable_sha256,
            "argv_sha256": gate_ready.argv_sha256,
            "environment_names_sizes_sha256": gate_ready.environment_names_sizes_sha256,
            "cwd_device": gate_ready.cwd_device,
            "cwd_inode": gate_ready.cwd_inode,
        }
        material["authorization_sha256"] = hashlib.sha256(dumps_canonical(material)).hexdigest()
        return GateExecAuthorizationV1.from_mapping(material)


class LedgerStartupReconciler:
    """Bounded ledger-seeded recovery for deterministic broker scope names."""

    def __init__(
        self,
        *,
        ledger: object,
        runtime: ContainmentAuthorityRuntime,
        manager: object,
        operation_backend: ContainmentOperationBackend,
        current_boot_id: str,
        monotonic: Callable[[], float],
        unit_absent: Callable[[BaseException], bool] | None = None,
    ) -> None:
        if type(current_boot_id) is not str or _BOOT_ID.fullmatch(current_boot_id) is None:
            raise ValueError("current_boot_id must be a lowercase UUID")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if unit_absent is not None and not callable(unit_absent):
            raise TypeError("unit_absent must be callable")
        self.ledger = ledger
        self.runtime = runtime
        self.manager = manager
        self.operation_backend = operation_backend
        self.current_boot_id = current_boot_id
        self.monotonic = monotonic
        self.unit_absent = unit_absent or (lambda error: isinstance(error, SystemdUnitNotFound))

    async def reconcile(
        self,
        *,
        deadline_seconds: float,
        maximum_records: int,
    ) -> None:
        if (
            type(deadline_seconds) not in (int, float)
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
        ):
            raise ValueError("reconciliation deadline must be positive and finite")
        if type(maximum_records) is not int or maximum_records < 1:
            raise ValueError("reconciliation record bound must be positive")
        started = self._read_monotonic()
        deadline = started + float(deadline_seconds)
        if not math.isfinite(deadline):
            raise ValueError("reconciliation deadline overflowed")

        try:
            inventory = self.ledger.inventory(
                maximum_records=maximum_records,
                monotonic_deadline=deadline,
                _monotonic=self.monotonic,
            )
        except ContainmentLedgerIncompatibleError as error:
            raise BrokerIncompatibleError("ledger inventory schema is unsupported") from error
        if not isinstance(inventory, ContainmentLedgerInventory):
            raise BrokerRejectedError("ledger inventory result is malformed")
        self._check_deadline(deadline)
        for entry in inventory.entries:
            self._check_deadline(deadline)
            await self._reconcile_entry(entry, deadline)
            self._check_deadline(deadline)

    async def _reconcile_entry(
        self,
        entry: ContainmentLedgerInventoryEntry,
        deadline: float,
    ) -> None:
        if not isinstance(entry, ContainmentLedgerInventoryEntry):
            raise BrokerRejectedError("ledger inventory entry is malformed")
        chain = entry.chain
        if chain.state is ContainmentLedgerState.RESERVATION_ONLY:
            if (
                any(
                    item is not None
                    for item in (
                        chain.attempt,
                        chain.attachment,
                        chain.release_intent,
                        chain.release,
                        chain.stop_intent,
                        chain.drain,
                    )
                )
                or entry.launch_nonce_sha256 != entry.authority.launch_nonce_sha256
            ):
                raise BrokerRejectedError("reservation-only authority is malformed")
            return
        if chain.state is ContainmentLedgerState.AMBIGUOUS or chain.attempt is None:
            raise BrokerRejectedError("ledger inventory authority is ambiguous")
        attempt = chain.attempt.record
        if (
            attempt.peer_uid != entry.authenticated_uid
            or attempt.authority != entry.authority
            or entry.launch_nonce_sha256 != entry.authority.launch_nonce_sha256
        ):
            raise BrokerRejectedError("ledger inventory authority binding changed")
        expected_unit = derive_scope_unit_name(entry.authenticated_uid, entry.launch_nonce_sha256)
        if chain.drain is not None:
            return
        if chain.attachment is None:
            # Neither unit presence nor absence closes the crash interval
            # between attempt fsync and exact attachment publication.
            raise BrokerRejectedError("attempt-only authority is ambiguous and cannot be adopted")

        attachment = chain.attachment.record
        if attachment.unit_name != expected_unit:
            raise BrokerRejectedError("ledger unit differs from deterministic authority")
        if attachment.boot_id != self.current_boot_id:
            if chain.stop_intent is not None:
                raise BrokerRejectedError(
                    "prior-boot chain contains an incompatible same-boot stop intent"
                )
            self.operation_backend.author_reboot_drain(
                entry.authenticated_uid,
                entry.authority,
                current_boot_id=self.current_boot_id,
            )
            return

        identity = await self._read_known_unit(expected_unit, deadline)
        if identity is None:
            if chain.stop_intent is None:
                raise BrokerRejectedError("same-boot scope vanished without durable stop intent")
            proof = await self.runtime.prove_reconciled_drain(attachment)
            self._check_deadline(deadline)
            if (
                not isinstance(proof, (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1))
                or proof.cgroup_identity != attachment.cgroup_identity
            ):
                raise BrokerRejectedError("absent scope lacks an exact stop-linked drain proof")
            self.operation_backend.author_reconciled_drain(
                entry.authenticated_uid,
                entry.authority,
                proof=proof,
            )
            return

        self._assert_live_unit_identity(attachment, identity)
        gate_phase = (
            "target"
            if chain.release is not None
            else "release-intent" if chain.release_intent is not None else "stopped-gate"
        )
        disposition = await self.runtime.verify_reconciled_attachment(
            attachment,
            identity,
            attempt=attempt,
            gate_phase=gate_phase,
        )
        self._check_deadline(deadline)
        if isinstance(disposition, ReconciledAttachmentObservation):
            disposition = disposition.disposition
        if not isinstance(disposition, ReconciledAttachmentDisposition):
            raise BrokerRejectedError("reconciled attachment disposition is malformed")
        _assert_reconciled_member_set(attachment, identity, disposition, gate_phase)
        if chain.stop_intent is not None:
            await self.operation_backend.resume_durable_stop(
                entry.authenticated_uid,
                entry.authority,
                timeout_seconds=max(0.001, deadline - self._read_monotonic()),
            )
            return
        if disposition is not ReconciledAttachmentDisposition.LIVE:
            termination_reason = self._recovery_termination_reason(entry)
            await self.operation_backend.recover_missing_stop(
                entry.authenticated_uid,
                entry.authority,
                termination_reason=termination_reason,
                timeout_seconds=max(0.001, deadline - self._read_monotonic()),
            )
            return
        await self.operation_backend.register_reconciled_authority(
            entry.authenticated_uid, entry.authority, attachment
        )
        await self.manager.ref_unit(expected_unit)

    @staticmethod
    def _recovery_termination_reason(entry: ContainmentLedgerInventoryEntry) -> str:
        for persisted in entry.reservations:
            reservation = persisted.reservation
            if reservation.slot.value != "terminal":
                continue
            if reservation.operation is BrokerOperation.AWAIT_DRAIN:
                request = BrokerRequestV1.from_mapping(
                    loads_canonical_packet(reservation.canonical_request_json_utf8.encode("utf-8"))
                )
                reason = request.payload.termination_reason
                if reason in ("normal_exit", "nonzero_exit"):
                    return reason
            if reservation.operation is BrokerOperation.STOP:
                request = BrokerRequestV1.from_mapping(
                    loads_canonical_packet(reservation.canonical_request_json_utf8.encode("utf-8"))
                )
                return request.payload.termination_reason
            return "recovery_stop"
        return "recovery_stop"

    async def _read_known_unit(self, unit_name: str, deadline: float):
        self._check_deadline(deadline)
        try:
            identity = await self.manager.read_unit_identity(unit_name)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            if self.unit_absent(error):
                self._check_deadline(deadline)
                return None
            raise BrokerRejectedError("known unit observation failed closed") from error
        self._check_deadline(deadline)
        return identity

    def _check_deadline(self, deadline: float) -> None:
        if self._read_monotonic() >= deadline:
            raise BrokerRejectedError("startup reconciliation deadline expired")

    def _read_monotonic(self) -> float:
        try:
            value = self.monotonic()
        except BaseException as error:
            raise BrokerRejectedError("startup monotonic observer failed") from error
        if type(value) not in (int, float) or not math.isfinite(value):
            raise BrokerRejectedError("startup monotonic observation is malformed")
        return float(value)

    def _assert_live_unit_identity(self, attachment: object, identity: object) -> None:
        expected = (
            attachment.unit_name,
            attachment.invocation_id,
            attachment.control_group,
            SCOPE_SLICE,
            "control-group",
            _seconds_to_usec(self.operation_backend.config.graceful_stop_seconds),
            True,
            "inactive-or-failed",
            _seconds_to_usec(self.operation_backend.config.maximum_runtime_seconds),
            False,
            "",
        )
        observed = (
            getattr(identity, "unit_name", None),
            getattr(identity, "invocation_id", None),
            getattr(identity, "control_group", None),
            getattr(identity, "slice", None),
            getattr(identity, "kill_mode", None),
            getattr(identity, "timeout_stop_usec", None),
            getattr(identity, "send_sigkill", None),
            getattr(identity, "collect_mode", None),
            getattr(identity, "runtime_max_usec", None),
            getattr(identity, "delegate", None),
            getattr(identity, "controller", None),
        )
        if observed != expected:
            raise BrokerRejectedError(
                "live unit invocation, cgroup, or policy identity was substituted"
            )


class PeerAuthenticatedBroker:
    """One-certificate broker lifecycle and closed request dispatcher."""

    def __init__(
        self,
        *,
        config: BrokerConfig,
        manager: object,
        binding_observer: object,
        startup_probe_launcher: object,
        certification_bindings_factory: Callable[[BrokerConfig, object], object],
        reconciler: StartupReconciler,
        notifier: BrokerNotifier,
        transport: BrokerTransport,
        operation_backend: BrokerOperationBackend,
        certification_hook: Callable[..., Awaitable[object]] = certify_broker_startup,
        certification_observer: Callable[..., Awaitable[None]] = observe_certification_epoch,
        descriptor_closer: Callable[[int], object] = os.close,
    ) -> None:
        self.config = config
        self.manager = manager
        self.binding_observer = binding_observer
        self.startup_probe_launcher = startup_probe_launcher
        self.certification_bindings_factory = certification_bindings_factory
        self.reconciler = reconciler
        self.notifier = notifier
        self.transport = transport
        self.operation_backend = operation_backend
        self.certification_hook = certification_hook
        self.certification_observer = certification_observer
        self.descriptor_closer = descriptor_closer
        self.certificate: object | None = None
        self.epoch_watch: object | None = None
        self.epoch_snapshot: object | None = None
        self.ready = False
        self.revoked = False
        self._ready_notified = False
        self._authority_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._authority_users: dict[tuple[int, str], int] = {}
        self._revoked_event = asyncio.Event()

    @property
    def notifier(self) -> BrokerNotifier:
        return self._notifier

    @notifier.setter
    def notifier(self, value: BrokerNotifier) -> None:
        self._notifier = value

    async def start(self) -> None:
        if self.revoked:
            raise BrokerRevokedError("broker certificate was permanently revoked")
        if self.ready:
            return
        if self.certificate is not None:
            raise BrokerRevokedError("broker cannot repin a startup certificate")
        try:
            self.epoch_watch = await self.manager.install_epoch_watch()
            self.epoch_snapshot = await self.manager.read_epoch_snapshot(self.epoch_watch)
            bindings = self.certification_bindings_factory(self.config, self.epoch_snapshot)
            self.certificate = await asyncio.wait_for(
                self.certification_hook(
                    manager=self.manager,
                    launcher=self.startup_probe_launcher,
                    binding_observer=self.binding_observer,
                    bindings=bindings,
                    scope_policy=self.config.scope_policy(),
                    deadlines=self.config.certification_deadlines(),
                    epoch_watch=self.epoch_watch,
                    epoch_snapshot=self.epoch_snapshot,
                ),
                timeout=self.config.startup_certification_timeout_seconds,
            )
            capabilities = _capabilities_for_retained_certificate(
                self.config,
                self.certificate,
            )
            BrokerReply(
                request_id="r" * 128,
                status="ok",
                payload=capabilities,
            ).canonical_bytes()
            await asyncio.wait_for(
                self.reconciler.reconcile(
                    deadline_seconds=self.config.startup_reconciliation_timeout_seconds,
                    maximum_records=self.config.startup_reconciliation_maximum_records,
                ),
                timeout=self.config.startup_reconciliation_timeout_seconds,
            )
            await self._observe()
            self.notifier.notify("READY=1")
            self._ready_notified = True
            await asyncio.wait_for(
                self.notifier.barrier(self.config.startup_notify_barrier_timeout_seconds),
                timeout=self.config.startup_notify_barrier_timeout_seconds,
            )
            await self._observe()
            self.ready = True
        except BaseException as error:
            self._revoke()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BrokerRevokedError("broker startup failed closed") from error

    async def serve_one(self) -> BrokerReply:
        self.transport.bind_activated_socket(self.config)
        await self.start()
        peer, packet, descriptors = await self.transport.receive_one()
        reply = await self.handle_packet(peer, packet, descriptors)
        await self.transport.send(reply)
        return reply

    async def handle_packet(
        self,
        peer: PeerIdentity,
        packet: bytes,
        descriptors: tuple[int, ...],
    ) -> BrokerReply:
        request_id = "unknown"
        authority_uid: int | None = None
        descriptor_ownership = [False]
        primary_error: BaseException | None = None
        try:
            if not self.ready or self.revoked:
                raise BrokerRevokedError("broker is not certified for request service")
            await self._observe()
            if (
                type(packet) is not bytes
                or not packet
                or len(packet) > self.config.maximum_request_bytes
            ):
                raise BrokerRejectedError("request packet size is invalid")
            decoded = loads_canonical_packet(packet)
            if isinstance(decoded, Mapping) and (
                decoded.get("schema_name") != "mif-containment-broker-request"
                or (
                    type(decoded.get("schema_version")) is int
                    and decoded.get("schema_version") != 1
                )
            ):
                raise BrokerIncompatibleError("request contract version is unsupported")
            request = BrokerRequestV1.from_mapping(decoded)
            if request.canonical_bytes() != packet:
                raise BrokerRejectedError("request packet is not canonical")
            request_id = request.request_id
            authority = getattr(request.payload, "authority", None)
            if authority is not None:
                authority_uid = peer.uid
            if request.operation is BrokerOperation.ATTACH:
                if len(descriptors) != 1:
                    raise BrokerRejectedError("attach requires exactly one target descriptor")
            elif descriptors:
                raise BrokerRejectedError("ancillary descriptors are forbidden")
            result = await asyncio.wait_for(
                self.dispatch_operation(
                    peer,
                    request.operation,
                    request.payload,
                    descriptors,
                    authenticated_request=request,
                    canonical_request_bytes=packet,
                    transfer_descriptors=lambda: descriptor_ownership.__setitem__(0, True),
                ),
                timeout=self.config.maximum_runtime_seconds,
            )
            # Give an epoch callback queued by the protected operation the
            # boundary before constructing the post-operation observation.
            await asyncio.sleep(0)
            if self.revoked:
                raise BrokerRevokedError("broker certificate was revoked")
            await self._observe()
            status = "ok"
            if isinstance(result, Mapping) and result.get("semantic_state") in (
                "live",
                "drained",
                "ambiguous",
            ):
                status = result["semantic_state"]
            return BrokerReply(request_id=request_id, status=status, payload=result)
        except BrokerRevokedError as error:
            primary_error = error
            raise
        except asyncio.CancelledError as error:
            primary_error = error
            raise
        except BaseException as error:
            primary_error = error
            if self.revoked:
                raise BrokerRevokedError("broker certificate was revoked") from error
            return safe_error_reply(
                request_id=request_id,
                authenticated_uid=peer.uid,
                authority_uid=authority_uid,
                error=error,
            )
        finally:
            if not descriptor_ownership[0]:
                closing_descriptors = descriptors
                descriptors = ()
                failures = await _attempt_cleanup_steps(
                    tuple(
                        lambda descriptor=descriptor: self.descriptor_closer(descriptor)
                        for descriptor in closing_descriptors
                    )
                )
                if primary_error is not None:
                    _annotate_cleanup_failures(primary_error, failures)
                elif failures:
                    raise failures[0]

    async def dispatch_operation(
        self,
        peer: PeerIdentity,
        operation: BrokerOperation,
        payload: object,
        descriptors: tuple[int, ...],
        *,
        authenticated_request: BrokerRequestV1 | None = None,
        canonical_request_bytes: bytes | None = None,
        transfer_descriptors: Callable[[], None] | None = None,
    ) -> object:
        if type(operation) is not BrokerOperation:
            raise BrokerRejectedError("operation is outside the closed union")
        if operation is BrokerOperation.CAPABILITIES:
            return await self.operation_backend.capabilities(
                self._context(authenticated_request, canonical_request_bytes)
            )
        nonce = _operation_nonce(payload)
        key = (peer.uid, nonce)
        lock = self._authority_locks.setdefault(key, asyncio.Lock())
        self._authority_users[key] = self._authority_users.get(key, 0) + 1
        try:
            return await self._dispatch_authority_operation(
                key,
                lock,
                peer,
                operation,
                payload,
                descriptors,
                authenticated_request,
                canonical_request_bytes,
                transfer_descriptors,
            )
        finally:
            remaining = self._authority_users[key] - 1
            if remaining:
                self._authority_users[key] = remaining
            else:
                self._authority_users.pop(key, None)
                if not lock.locked():
                    self._authority_locks.pop(key, None)

    async def _dispatch_authority_operation(
        self,
        key: tuple[int, str],
        lock: asyncio.Lock,
        peer: PeerIdentity,
        operation: BrokerOperation,
        payload: object,
        descriptors: tuple[int, ...],
        authenticated_request: BrokerRequestV1 | None,
        canonical_request_bytes: bytes | None,
        transfer_descriptors: Callable[[], None] | None,
    ) -> object:
        del key
        if operation is BrokerOperation.RELEASE:
            async with lock:
                should_resume = await self.operation_backend.release_intent(
                    self._context(authenticated_request, canonical_request_bytes), peer, payload
                )
                release_resume = getattr(self.operation_backend, "release_resume", None)
                if should_resume and release_resume is not None:
                    await release_resume(
                        self._context(authenticated_request, canonical_request_bytes), peer, payload
                    )
            return await self.operation_backend.release_wait(
                self._context(authenticated_request, canonical_request_bytes), peer, payload
            )
        async with lock:
            if operation is BrokerOperation.ATTACH:
                if len(descriptors) != 1:
                    raise BrokerRejectedError("attach requires exactly one descriptor")
                context = self._context(authenticated_request, canonical_request_bytes)
                if transfer_descriptors is not None:
                    transfer_descriptors()
                return await self.operation_backend.attach(
                    context,
                    peer,
                    payload,
                    descriptors[0],
                )
            if descriptors:
                raise BrokerRejectedError("ancillary descriptors are forbidden")
            handlers = {
                BrokerOperation.GATE_AUTHORIZE_EXEC: self.operation_backend.gate_authorize_exec,
                BrokerOperation.INSPECT: self.operation_backend.inspect,
                BrokerOperation.AWAIT_DRAIN: self.operation_backend.await_drain,
                BrokerOperation.STOP: self.operation_backend.stop,
                BrokerOperation.RECOVER_STOP: self.operation_backend.recover_stop,
            }
            try:
                handler = handlers[operation]
            except KeyError as error:
                raise BrokerRejectedError("operation is outside the closed union") from error
            return await handler(
                self._context(authenticated_request, canonical_request_bytes), peer, payload
            )

    def _context(
        self,
        authenticated_request: BrokerRequestV1 | None = None,
        canonical_request_bytes: bytes | None = None,
    ) -> BrokerContext:
        if self.certificate is None or self.revoked:
            raise BrokerRevokedError("startup certificate is unavailable")
        return BrokerContext(
            config=self.config,
            certification=self.certificate,
            manager=self.manager,
            authenticated_request=authenticated_request,
            canonical_request_bytes=canonical_request_bytes,
        )

    async def _observe(self) -> None:
        if (
            self.revoked
            or self.certificate is None
            or self.epoch_watch is None
            or self.epoch_snapshot is None
        ):
            raise BrokerRevokedError("startup certificate is incomplete")
        try:
            await self.certification_observer(
                self.certificate,
                manager=self.manager,
                binding_observer=self.binding_observer,
                epoch_watch=self.epoch_watch,
                epoch_snapshot=self.epoch_snapshot,
            )
            if self.revoked:
                raise BrokerRevokedError("startup certificate was permanently revoked")
        except BaseException as error:
            self._revoke()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BrokerRevokedError("startup certificate was permanently revoked") from error

    def _observe_idle(self) -> None:
        if self.revoked or not self.ready:
            raise BrokerRevokedError("broker is not ready for idle observation")
        try:
            observer = getattr(self.binding_observer, "assert_idle_bindings_current")
            observer()
            if self.revoked:
                raise BrokerRevokedError("startup certificate was permanently revoked")
        except BaseException as error:
            self._revoke()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise BrokerRevokedError("startup certificate was permanently revoked") from error

    def _revoke(self) -> None:
        self.revoked = True
        self.ready = False
        self._revoked_event.set()
        if self._ready_notified:
            try:
                self.notifier.notify("STOPPING=1")
            except BaseException:
                pass

    def _manager_epoch_revoked(self, _reason: str) -> None:
        self._revoke()


class ActivatedSeqpacketBrokerService:
    """Bounded concurrent service loop for the activated privileged socket."""

    def __init__(
        self,
        *,
        broker: PeerAuthenticatedBroker,
        peer_authenticator: Callable[[socket.socket], PeerIdentity] | None = None,
        maximum_connections: int = 64,
        listener: socket.socket | None = None,
        owned_resources: tuple[object, ...] = (),
    ) -> None:
        if type(maximum_connections) is not int or not 1 <= maximum_connections <= 4096:
            raise ValueError("maximum_connections must be positive and bounded")
        self.broker = broker
        self.listener = listener
        self.maximum_connections = maximum_connections
        if type(owned_resources) is not tuple or any(
            not callable(getattr(resource, "close", None)) for resource in owned_resources
        ):
            raise TypeError("owned_resources must be a tuple of closeable objects")
        self._owned_resources = owned_resources
        self._semaphore = asyncio.Semaphore(maximum_connections)
        self._tasks: set[asyncio.Task[None]] = set()
        self._peer_authenticator = peer_authenticator or self._authenticate

    async def _run_unrevoked(self, awaitable_factory: Callable[[], Awaitable[object]]) -> object:
        if not callable(awaitable_factory):
            raise TypeError("protected operation must be a deferred factory")
        revoked_event = getattr(self.broker, "_revoked_event", None)
        if not isinstance(revoked_event, asyncio.Event):
            revoked_event = asyncio.Event()
        if self.broker.revoked or revoked_event.is_set():
            raise BrokerRevokedError("broker certificate was revoked")
        # Drain callbacks which were already queued before allowing a protected
        # body to be constructed or reach a synchronous socket operation.
        await asyncio.sleep(0)
        if self.broker.revoked or revoked_event.is_set():
            raise BrokerRevokedError("broker certificate was revoked")
        operation = asyncio.create_task(
            asyncio.wait_for(
                awaitable_factory(),
                timeout=self.broker.config.maximum_runtime_seconds,
            )
        )
        revoked = asyncio.create_task(revoked_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (operation, revoked), return_when=asyncio.FIRST_COMPLETED
            )
            if revoked in done or self.broker.revoked or revoked_event.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise BrokerRevokedError("broker certificate was revoked")
            return operation.result()
        finally:
            for task in (operation, revoked):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, revoked, return_exceptions=True)

    def _connection_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            pass

    async def _monitor_epoch(self) -> None:
        try:
            while self.broker.ready and not self.broker.revoked:
                await asyncio.sleep(0.1)
                self.broker._observe_idle()
        except asyncio.CancelledError:
            raise
        except BaseException:
            return

    def _authenticate(self, connection: socket.socket) -> PeerIdentity:
        def lookup(uid: int) -> PasswdAccount:
            account = pwd.getpwuid(uid)
            return PasswdAccount(
                name=account.pw_name,
                uid=account.pw_uid,
                gid=account.pw_gid,
                home=account.pw_dir,
                primary_group_name=grp.getgrgid(account.pw_gid).gr_name,
            )

        return authenticate_peer(
            connection,
            account_lookup=lookup,
            process_start_token=_process_start_token,
        )

    async def serve_connection(self, connection: socket.socket) -> None:
        received_descriptors: tuple[int, ...] = ()
        dispatch_took_ownership = False
        primary_error: BaseException | None = None

        async def dispatch_owned(peer: PeerIdentity, packet: bytes) -> BrokerReply:
            nonlocal dispatch_took_ownership
            dispatch_took_ownership = True
            return await self.broker.handle_packet(peer, packet, received_descriptors)

        async def receive_owned() -> tuple[bytes, tuple[int, ...]]:
            nonlocal received_descriptors
            packet, descriptors = await _receive_bounded_seqpacket_async(
                connection,
                maximum_bytes=self.broker.config.maximum_request_bytes,
            )
            received_descriptors = descriptors
            return packet, descriptors

        try:
            async with self._semaphore:
                try:
                    peer = None
                    try:
                        if self.broker.revoked or not self.broker.ready:
                            raise BrokerRevokedError("broker is not accepting requests")
                        peer = self._peer_authenticator(connection)
                        packet, received_descriptors = await self._run_unrevoked(receive_owned)
                        reply = await self._run_unrevoked(lambda: dispatch_owned(peer, packet))
                    except asyncio.CancelledError:
                        raise
                    except BrokerRevokedError:
                        return
                    except BaseException as error:
                        reply = safe_error_reply(
                            request_id="unknown",
                            authenticated_uid=None if peer is None else peer.uid,
                            authority_uid=None,
                            error=error,
                        )
                    encoded = reply.canonical_bytes()
                    try:
                        await self._run_unrevoked(
                            lambda: _send_seqpacket_async(connection, encoded)
                        )
                    except BrokerRevokedError:
                        return
                except BaseException:
                    raise
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_steps: list[Callable[[], object]] = []
            if received_descriptors and not dispatch_took_ownership:
                closer = getattr(self.broker, "descriptor_closer", os.close)
                closing_descriptors = received_descriptors
                received_descriptors = ()
                cleanup_steps.extend(
                    lambda descriptor=descriptor: closer(descriptor)
                    for descriptor in closing_descriptors
                )
            closing_connection = connection
            connection = None
            cleanup_steps.append(closing_connection.close)
            failures = await _attempt_cleanup_steps(cleanup_steps)
            if primary_error is not None:
                _annotate_cleanup_failures(primary_error, failures)
            elif failures:
                raise failures[0]

    async def serve_forever(self, shutdown: asyncio.Event | None = None) -> None:
        if self.listener is None:
            raise BrokerConfigurationError("activated listener is unavailable")
        monitor_task: asyncio.Task[None] | None = None
        primary_error: BaseException | None = None
        try:
            await self.broker.start()
            monitor_task = asyncio.create_task(self._monitor_epoch())
            self.listener.setblocking(False)
            loop = asyncio.get_running_loop()
            shutdown_event = shutdown or asyncio.Event()
            while self.broker.ready and not self.broker.revoked and not shutdown_event.is_set():
                if len(self._tasks) >= self.maximum_connections:
                    capacity_task = asyncio.create_task(
                        asyncio.wait(tuple(self._tasks), return_when=asyncio.FIRST_COMPLETED)
                    )
                    stop_task = asyncio.create_task(shutdown_event.wait())
                    revoke_task = asyncio.create_task(self.broker._revoked_event.wait())
                    race = (capacity_task, stop_task, revoke_task)
                    try:
                        done, _pending = await asyncio.wait(
                            race, return_when=asyncio.FIRST_COMPLETED
                        )
                        if stop_task in done or revoke_task in done:
                            break
                        capacity_task.result()
                    finally:
                        for task in race:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*race, return_exceptions=True)
                    continue
                accept_task = asyncio.create_task(loop.sock_accept(self.listener))
                stop_task = asyncio.create_task(shutdown_event.wait())
                revoke_task = asyncio.create_task(self.broker._revoked_event.wait())
                race = (accept_task, stop_task, revoke_task)
                try:
                    done, pending = await asyncio.wait(
                        race,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in race:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*race, return_exceptions=True)
                if stop_task in done or revoke_task in done or self.broker.revoked:
                    if accept_task in done:
                        connection, _address = accept_task.result()
                        connection.close()
                    break
                connection, _address = accept_task.result()
                task = asyncio.create_task(self.serve_connection(connection))
                self._tasks.add(task)
                task.add_done_callback(self._connection_done)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            closing_monitor = monitor_task
            monitor_task = None
            closing_tasks = tuple(self._tasks)
            self._tasks.clear()
            closing_listener = self.listener
            self.listener = None
            closing_resources = self._owned_resources
            self._owned_resources = ()
            cleanup_steps: list[Callable[[], object]] = []
            if closing_monitor is not None:
                closing_monitor.cancel()
                cleanup_steps.append(
                    lambda: asyncio.gather(closing_monitor, return_exceptions=True)
                )
            if closing_tasks:
                for task in closing_tasks:
                    task.cancel()
                cleanup_steps.append(lambda: asyncio.gather(*closing_tasks, return_exceptions=True))
            if closing_listener is not None:
                cleanup_steps.append(closing_listener.close)
            cleanup_steps.extend(resource.close for resource in reversed(closing_resources))
            cleanup_task = asyncio.create_task(_attempt_cleanup_steps(cleanup_steps))
            deferred_cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    failures = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError as error:
                    if deferred_cancellation is None:
                        deferred_cancellation = error
            if primary_error is not None:
                _annotate_cleanup_failures(primary_error, failures)
            elif deferred_cancellation is not None:
                _annotate_cleanup_failures(deferred_cancellation, failures)
                raise deferred_cancellation
            elif failures:
                raise failures[0]


def _assert_installed_file_metadata_current(
    expected: InstalledFileIdentity,
    *,
    _stat: Callable[..., object] = os.stat,
) -> None:
    try:
        observed = _stat(expected.canonical_path, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as error:
        raise BrokerRevokedError("retained installed asset identity changed") from error
    if (
        observed.st_dev != expected.device
        or observed.st_ino != expected.inode
        or observed.st_mode != expected.mode
        or observed.st_uid != expected.uid
        or observed.st_gid != expected.gid
        or observed.st_nlink != expected.nlink
        or observed.st_size != expected.size
    ):
        raise BrokerRevokedError("retained installed asset identity changed")


def _socket_group_identity(
    name: str,
    *,
    _by_name: Callable[[str], object] = grp.getgrnam,
    _by_gid: Callable[[int], object] = grp.getgrgid,
) -> SocketGroupIdentity:
    try:
        named = _by_name(name)
        numeric = _by_gid(named.gr_gid)
        named_members = tuple(named.gr_mem)
        numeric_members = tuple(numeric.gr_mem)
        if (
            named.gr_name != name
            or numeric.gr_name != name
            or numeric.gr_gid != named.gr_gid
            or len(set(named_members)) != len(named_members)
            or len(set(numeric_members)) != len(numeric_members)
            or tuple(sorted(named_members)) != tuple(sorted(numeric_members))
        ):
            raise ValueError("socket group mappings disagree")
        return SocketGroupIdentity(
            name=name,
            gid=named.gr_gid,
            members=tuple(sorted(named_members)),
        )
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        raise BrokerConfigurationError("configured socket group is not exact") from error


class ProductionBrokerBindingObserver:
    """Revalidate the retained local half of one broker certification epoch."""

    def __init__(
        self,
        config: BrokerConfig,
        broker_identity: InstalledFileIdentity,
        *,
        probe_account_identity: ProbeAccountIdentity,
        activated_socket_identity: ActivatedSocketIdentity,
        socket_group_identity: SocketGroupIdentity,
        probe_account_observer: Callable[[], ProbeAccountIdentity],
        activated_socket_observer: Callable[[], ActivatedSocketIdentity],
        socket_group_observer: Callable[[], SocketGroupIdentity],
        asset_metadata_observer: Callable[
            [InstalledFileIdentity], None
        ] = _assert_installed_file_metadata_current,
    ) -> None:
        self.config = config
        self.broker_identity = broker_identity
        self._assets = (
            config.config_identity,
            config.gate.identity,
            config.anchor.identity,
            config.startup_probe.identity,
            *(registration.identity for registration in config.registrations),
            broker_identity,
        )
        self._broker_pid = os.getpid()
        self._broker_start_token = _broker_process_start_token(self._broker_pid)
        self._kernel_release = os.uname().release
        self._boot_id = _read_text_file("/proc/sys/kernel/random/boot_id", 128)
        self._probe_account_identity = probe_account_identity
        self._activated_socket_identity = activated_socket_identity
        self._socket_group_identity = socket_group_identity
        self._probe_account_observer = probe_account_observer
        self._activated_socket_observer = activated_socket_observer
        self._socket_group_observer = socket_group_observer
        self._asset_metadata_observer = asset_metadata_observer

    def assert_bindings_current(self, bindings: StartupCertificationBindings) -> None:
        if (
            bindings.broker_process_id != self._broker_pid
            or bindings.broker_process_start_token != self._broker_start_token
            or bindings.broker_build_id != BROKER_BUILD_ID
            or bindings.broker_config_sha256 != self.config.config_sha256
            or bindings.startup_probe_path != self.config.startup_probe.canonical_path
            or bindings.startup_probe_sha256 != self.config.startup_probe.sha256
            or bindings.startup_probe_account != self.config.startup_probe.account_name
            or bindings.boot_id != self._boot_id
            or bindings.kernel_release != self._kernel_release
        ):
            raise BrokerRevokedError("broker startup bindings changed")
        self._assert_local_assets_current()

    def assert_certificate_current(self, certification: object) -> None:
        if (
            getattr(certification, "broker_process_id", None) != self._broker_pid
            or getattr(certification, "broker_process_start_token", None)
            != self._broker_start_token
            or getattr(certification, "broker_build_id", None) != BROKER_BUILD_ID
            or getattr(certification, "broker_config_sha256", None) != self.config.config_sha256
            or getattr(certification, "boot_id", None) != self._boot_id
            or getattr(certification, "kernel_release", None) != self._kernel_release
        ):
            raise BrokerRevokedError("broker certificate local bindings changed")
        self._assert_local_assets_current()

    def _assert_local_assets_current(self) -> None:
        self._assert_epoch_local_facts()
        for expected in self._assets:
            observed, _content = read_root_installed_file(
                expected.canonical_path,
                MAXIMUM_CONFIG_BYTES if expected is self.config.config_identity else 268_435_456,
            )
            if observed != expected:
                raise BrokerRevokedError("retained installed asset identity changed")
        self._assert_non_asset_bindings_current()

    def assert_idle_bindings_current(self) -> None:
        """Perform the bounded, metadata-only half of the epoch fence."""

        self._assert_epoch_local_facts()
        for expected in self._assets:
            self._asset_metadata_observer(expected)
        self._assert_non_asset_bindings_current()

    def _assert_epoch_local_facts(self) -> None:
        if (
            os.getpid() != self._broker_pid
            or _broker_process_start_token(self._broker_pid) != self._broker_start_token
        ):
            raise BrokerRevokedError("broker process identity changed")
        if (
            os.uname().release != self._kernel_release
            or _read_text_file("/proc/sys/kernel/random/boot_id", 128) != self._boot_id
        ):
            raise BrokerRevokedError("broker kernel or boot identity changed")

    def _assert_non_asset_bindings_current(self) -> None:
        if self._probe_account_observer() != self._probe_account_identity:
            raise BrokerRevokedError("startup-probe account identity changed")
        if self._activated_socket_observer() != self._activated_socket_identity:
            raise BrokerRevokedError("activated listener identity changed")
        if self._socket_group_observer() != self._socket_group_identity:
            raise BrokerRevokedError("configured socket group identity changed")


def _certification_bindings(
    config: BrokerConfig,
    snapshot: object,
) -> StartupCertificationBindings:
    template = ScopeStartRequest(
        unit_name="mif-qupath-cert-00000000000000000000000000000000.scope",
        description="mIF containment startup certification",
        gate_pid=1,
        anchor_pid=2,
        timeout_stop_usec=_seconds_to_usec(config.graceful_stop_seconds),
        runtime_max_usec=_seconds_to_usec(config.maximum_runtime_seconds),
    )
    broker_pid = os.getpid()
    return StartupCertificationBindings(
        broker_process_id=broker_pid,
        broker_process_start_token=_broker_process_start_token(broker_pid),
        broker_build_id=BROKER_BUILD_ID,
        broker_config_sha256=config.config_sha256,
        scope_policy_sha256=scope_policy_sha256_for_request(template),
        scope_request_template_sha256=scope_request_template_sha256(template),
        startup_probe_path=config.startup_probe.canonical_path,
        startup_probe_sha256=config.startup_probe.sha256,
        startup_probe_account=config.startup_probe.account_name,
        boot_id=snapshot.boot_id,
        kernel_release=os.uname().release,
        system_bus_id=snapshot.system_bus_id,
        systemd_unique_owner=snapshot.systemd_unique_owner,
        units_load_timestamp_monotonic=snapshot.units_load_timestamp_monotonic,
        pid1_start_token=snapshot.pid1_start_token,
        pid1_executable_sha256=snapshot.pid1_executable_sha256,
        cgroup_mount_device=snapshot.cgroup_mount_device,
        cgroup_mount_inode=snapshot.cgroup_mount_inode,
    )


class _UnusedBrokerTransport:
    def bind_activated_socket(self, config: BrokerConfig) -> None:
        del config


def build_production_broker_service(
    *,
    _socket_group_identity_factory: Callable[[str], SocketGroupIdentity] = _socket_group_identity,
    _binding_observer_factory: Callable[
        ..., ProductionBrokerBindingObserver
    ] = ProductionBrokerBindingObserver,
) -> ActivatedSeqpacketBrokerService:
    """Compose the fixed installed broker without an externally trusted factory."""

    config = load_broker_config(BROKER_CONFIG_PATH)
    broker_identity, _broker_bytes = read_root_installed_file(
        "/usr/libexec/mif-pipeline/mif-containment-broker",
        268_435_456,
    )
    probe_account_identity = _locked_probe_account_identity(config.startup_probe.account_name)
    socket_group_identity = _socket_group_identity_factory(config.socket_group)
    listener = socket.socket(fileno=SYSTEMD_LISTEN_FD)
    ledger = None
    try:
        validate_socket_activation(
            os.environ,
            process_id=os.getpid(),
            listener=listener,
            socket_group_gid=socket_group_identity.gid,
        )
        activated_socket_identity = _activated_socket_identity(listener)
        manager = DbusFastSystemdManager()
        ledger = ContainmentLedger(config.state_root)
        runtime = PosixContainmentAuthorityRuntime(config=config)
        broker_pid = os.getpid()
        backend = ContainmentOperationBackend(
            config=config,
            ledger=ledger,
            runtime=runtime,
            manager=manager,
            broker_pid=broker_pid,
            broker_start_token=_broker_process_start_token(broker_pid),
            clock_ns=time.time_ns,
        )
        reconciler = LedgerStartupReconciler(
            ledger=ledger,
            runtime=runtime,
            manager=manager,
            operation_backend=backend,
            current_boot_id=_read_text_file("/proc/sys/kernel/random/boot_id", 128),
            monotonic=time.monotonic,
        )
        broker = PeerAuthenticatedBroker(
            config=config,
            manager=manager,
            binding_observer=_binding_observer_factory(
                config,
                broker_identity,
                probe_account_identity=probe_account_identity,
                activated_socket_identity=activated_socket_identity,
                socket_group_identity=socket_group_identity,
                probe_account_observer=lambda: _locked_probe_account_identity(
                    config.startup_probe.account_name
                ),
                activated_socket_observer=lambda: _activated_socket_identity(listener),
                socket_group_observer=lambda: _socket_group_identity_factory(config.socket_group),
            ),
            startup_probe_launcher=PosixStartupProbeLauncher(),
            certification_bindings_factory=_certification_bindings,
            reconciler=reconciler,
            notifier=SystemdBrokerNotifier.from_environment(),
            transport=_UnusedBrokerTransport(),
            operation_backend=backend,
        )
        manager._bind_epoch_revocation_callback(broker._manager_epoch_revoked)
        return ActivatedSeqpacketBrokerService(
            broker=broker, listener=listener, owned_resources=(ledger,)
        )
    except BaseException as primary:
        cleanup_steps: list[Callable[[], object]] = []
        if ledger is not None:
            ledger_owner = ledger
            ledger = None
            cleanup_steps.append(ledger_owner.close)
        listener_owner = listener
        listener = None
        cleanup_steps.append(listener_owner.close)
        failures = _attempt_sync_cleanup_steps(cleanup_steps)
        _annotate_cleanup_failures(primary, failures)
        raise


def containment_broker_main() -> int:
    """Compose and run the fixed privileged service; return one bounded status."""

    if os.geteuid() != 0:
        return 1
    try:
        service = build_production_broker_service()
        if not isinstance(service, ActivatedSeqpacketBrokerService):
            return 1
        asyncio.run(service.serve_forever())
    except (BrokerError, KeyError, OSError, ValueError, asyncio.TimeoutError):
        return 1
    return 0


def _operation_nonce(payload: object) -> str:
    if isinstance(payload, GateAuthorizeExecPayloadV1):
        return payload.launch_nonce_sha256
    authority = getattr(payload, "authority", None)
    nonce = getattr(authority, "launch_nonce_sha256", None)
    _require_sha256(nonce, "operation authority nonce")
    return nonce


def _signed_record(record_type, material: Mapping[str, object]):
    signed = dict(material)
    signed["record_sha256"] = hashlib.sha256(dumps_canonical(material)).hexdigest()
    return record_type.from_mapping(signed)


def _wire_value(value: object) -> object:
    return value.to_mapping() if hasattr(value, "to_mapping") else value


def _receipt_sha256(record: object) -> str:
    return hashlib.sha256(record.canonical_json_utf8().encode("utf-8")).hexdigest()


def _seconds_to_usec(seconds: float) -> int:
    value = int(seconds * 1_000_000)
    if value < 1 or value > (1 << 64) - 2:
        raise BrokerConfigurationError("scope policy cannot be represented by systemd")
    return value


def _installed_executable(
    value: object,
    fields: frozenset[str],
    identities: Mapping[str, InstalledFileIdentity],
    *,
    probe: bool = False,
) -> InstalledExecutable:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise ValueError("installed executable fields are not closed")
    path = _mapping_text(value, "canonical_path")
    digest = _mapping_text(value, "sha256")
    _require_canonical_path(path, "installed executable path")
    _require_sha256(digest, "installed executable digest")
    if value["root_owned"] is not True:
        raise ValueError("installed executable must declare root ownership")
    identity = identities.get(path)
    if not isinstance(identity, InstalledFileIdentity):
        raise ValueError("installed executable identity is unavailable")
    _validate_installed_identity(identity, path, digest)
    account = _mapping_text(value, "account_name") if probe else None
    return InstalledExecutable(path, digest, identity, account)


def _registration(
    value: object,
    identities: Mapping[str, InstalledFileIdentity],
) -> ExecutableRegistration:
    if not isinstance(value, Mapping) or frozenset(value) != _REGISTRATION_FIELDS:
        raise ValueError("registration fields are not closed")
    path = _mapping_text(value, "canonical_qupath_executable")
    digest = _mapping_text(value, "executable_sha256")
    _require_canonical_path(path, "registered executable path")
    _require_sha256(digest, "registered executable digest")
    identity = identities.get(path)
    if not isinstance(identity, InstalledFileIdentity):
        raise ValueError("registered executable identity is unavailable")
    _validate_installed_identity(identity, path, digest)
    registration_id = _mapping_text(value, "registration_id")
    raw_profiles = value["allowed_command_profiles"]
    if (
        _IDENTIFIER.fullmatch(registration_id) is None
        or type(raw_profiles) is not tuple
        or not raw_profiles
        or len(raw_profiles) > len(_COMMAND_PROFILES)
        or any(
            type(profile) is not str or profile not in _COMMAND_PROFILES for profile in raw_profiles
        )
        or len(raw_profiles) != len(set(raw_profiles))
    ):
        raise ValueError("registration identifier or command profiles are unsafe")
    raw_allowlist = value["allowed_environment_names"]
    if type(raw_allowlist) is not tuple or len(raw_allowlist) > MAXIMUM_ENVIRONMENT_COUNT:
        raise ValueError("environment allowlist is malformed")
    allowlist: list[str] = []
    for name in raw_allowlist:
        if (
            type(name) is not str
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            or _environment_is_dangerous(name)
        ):
            raise ValueError("environment allowlist contains an injection variable")
        allowlist.append(name)
    if len(allowlist) != len(set(allowlist)):
        raise ValueError("environment allowlist contains duplicates")
    return ExecutableRegistration(
        registration_id=registration_id,
        canonical_path=path,
        sha256=digest,
        command_profiles=tuple(raw_profiles),
        environment_allowlist=tuple(allowlist),
        identity=identity,
    )


def _validate_config_identity(identity: InstalledFileIdentity, raw: bytes) -> None:
    if not isinstance(identity, InstalledFileIdentity):
        raise ValueError("config identity is unavailable")
    if not stat.S_ISREG(identity.mode) or identity.uid != 0 or stat.S_IMODE(identity.mode) & 0o022:
        raise ValueError("config must be root-owned and not group/world writable")
    if identity.nlink != 1 or identity.size != len(raw):
        raise ValueError("config identity or size changed")
    if identity.sha256 != hashlib.sha256(raw).hexdigest():
        raise ValueError("config digest does not cover the complete file")


def _validate_installed_identity(
    identity: InstalledFileIdentity,
    path: str,
    digest: str,
) -> None:
    if identity.canonical_path != path or identity.sha256 != digest:
        raise ValueError("installed identity differs from configured complete digest")
    if (
        not stat.S_ISREG(identity.mode)
        or identity.uid != 0
        or stat.S_IMODE(identity.mode) & 0o022
        or identity.nlink != 1
    ):
        raise ValueError("installed identity is not root-owned and immutable to users")


def _mapping_text(mapping: Mapping[str, object], field: str) -> str:
    value = mapping[field]
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{field} must be nonempty text")
    return value


def _positive_number(mapping: Mapping[str, object], field: str) -> float:
    value = mapping[field]
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value <= 0
        or value > MAXIMUM_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{field} must be a positive bounded number")
    return float(value)


def _bounded_integer(mapping: Mapping[str, object], field: str, maximum: int) -> int:
    value = mapping[field]
    if type(value) is not int or value < 1 or value > maximum:
        raise ValueError(f"{field} must be a positive bounded integer")
    return value


def _require_canonical_path(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or "\x00" in value
        or posixpath.normpath(value) != value
        or len(value.encode("utf-8")) > 4096
    ):
        raise ValueError(f"{label} must be a canonical absolute path")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _environment_is_dangerous(name: str) -> bool:
    return name in _DANGEROUS_ENVIRONMENT or name.startswith(_DANGEROUS_PREFIXES)


def _infer_command_profile(target_argv: tuple[str, ...]) -> str:
    if (
        not target_argv
        or any(type(item) is not str or not item or "\0" in item for item in target_argv)
        or not posixpath.isabs(target_argv[0])
    ):
        raise BrokerRejectedError("target argv does not match a fixed command profile")
    if len(target_argv) >= 5 and target_argv[1] == "script":
        index = 2
        has_project = False
        if index < len(target_argv) - 1 and target_argv[index] == "--project":
            has_project = True
            index += 1
            if index >= len(target_argv) - 1 or not _canonical_suffix_path(
                target_argv[index], ".qpproj"
            ):
                raise BrokerRejectedError("target project argument is not canonical")
            index += 1
        has_image = False
        if index < len(target_argv) - 1 and target_argv[index] == "--image":
            has_image = True
            index += 1
            if (
                index >= len(target_argv) - 1
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", target_argv[index]) is None
            ):
                raise BrokerRejectedError("target image selector is not closed")
            index += 1
        argument_count = 0
        while index < len(target_argv) - 1 and target_argv[index] == "--args":
            index += 1
            if index >= len(target_argv) - 1 or not (
                _canonical_absolute_path(target_argv[index])
                or _SHA256.fullmatch(target_argv[index]) is not None
            ):
                raise BrokerRejectedError("target script argument is not closed")
            argument_count += 1
            index += 1
        has_save = False
        if index < len(target_argv) - 1 and target_argv[index] == "--save":
            has_save = True
            index += 1
        if (
            argument_count >= 1
            and index == len(target_argv) - 1
            and _canonical_suffix_path(target_argv[index], ".groovy")
            and (not has_image or (has_project and argument_count == 3 and has_save))
        ):
            return "headless-script"
    if len(target_argv) == 2 and _canonical_suffix_path(target_argv[1], ".qpproj"):
        return "interactive-project"
    raise BrokerRejectedError("target argv does not match a fixed command profile")


def _canonical_absolute_path(value: str) -> bool:
    return (
        posixpath.isabs(value)
        and not value.startswith("//")
        and value != "/"
        and posixpath.normpath(value) == value
        and len(value.encode("utf-8")) <= 4096
    )


def _canonical_suffix_path(value: str, suffix: str) -> bool:
    return _canonical_absolute_path(value) and value.endswith(suffix)


def _descriptor_matches_registration(
    observed: object,
    path: str,
    digest: str,
    registration: ExecutableRegistration,
) -> bool:
    identity = registration.identity
    return (
        path == registration.canonical_path
        and not path.endswith(" (deleted)")
        and stat.S_ISREG(observed.st_mode)
        and observed.st_dev == identity.device
        and observed.st_ino == identity.inode
        and observed.st_mode == identity.mode
        and observed.st_uid == identity.uid
        and observed.st_gid == identity.gid
        and observed.st_nlink == identity.nlink
        and observed.st_size == identity.size
        and digest == identity.sha256 == registration.sha256
    )


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written < 1:
            raise OSError("short write to private anchor pipe")
        offset += written


async def _attempt_cleanup_steps(
    steps: Iterable[Callable[[], object]],
) -> tuple[BaseException, ...]:
    """Attempt each independent owner once without hiding later cleanup."""

    failures: list[BaseException] = []
    for step in steps:
        try:
            result = step()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            failures.append(error)
    return tuple(failures)


def _attempt_sync_cleanup_steps(
    steps: Iterable[Callable[[], object]],
) -> tuple[BaseException, ...]:
    """Attempt each synchronous owner once without hiding later cleanup."""

    failures: list[BaseException] = []
    for step in steps:
        try:
            result = step()
            if inspect.isawaitable(result):
                raise TypeError("synchronous cleanup returned an awaitable")
        except BaseException as error:
            failures.append(error)
    return tuple(failures)


def _annotate_cleanup_failures(primary: BaseException, failures: Iterable[BaseException]) -> None:
    for failure in failures:
        primary.add_note(f"cleanup failure: {type(failure).__name__}")


def _prove_sole_forking_thread(
    *,
    python_threads: Callable[[], object] = threading.enumerate,
    task_names: Callable[[], object] = lambda: os.listdir("/proc/self/task"),
) -> None:
    """Fail unless the caller is the sole Python thread and Linux task."""

    threads = tuple(python_threads())
    tasks = tuple(task_names())
    native_id = threading.get_native_id()
    if (
        len(threads) != 1
        or threads[0] is not threading.current_thread()
        or len(tasks) != 1
        or tasks[0] != str(native_id)
    ):
        raise BrokerRejectedError("anchor fork requires one exact Python/kernel thread")


def _validate_child_installed_anchor(request: AnchorLaunchRequest) -> None:
    descriptor = os.open(
        request.path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _validate_open_installed_file(descriptor, request.executable_identity, request.path)
    finally:
        os.close(descriptor)


def _run_fixed_anchor_child(
    request: AnchorLaunchRequest,
    read_fd: int,
    write_fd: int,
    *,
    operations: Mapping[str, Callable[..., object]] | None = None,
    allow_exec_return: bool = False,
) -> None:
    """Execute the actual fixed child security sequence through narrow test seams."""

    ops: Mapping[str, Callable[..., object]] = operations or {
        "close": os.close,
        "open_devnull": lambda: os.open("/dev/null", os.O_RDWR),
        "dup2": os.dup2,
        "set_inheritable": os.set_inheritable,
        "chdir": os.chdir,
        "drop_bounding": _drop_capability_bounding_and_ambient_sets,
        "setgroups": os.setgroups,
        "setresgid": os.setresgid,
        "setfsgid": _set_filesystem_gid,
        "setresuid": os.setresuid,
        "setfsuid": _set_filesystem_uid,
        "verify_credentials": _verify_filesystem_credentials,
        "clear_caps": _clear_process_capability_sets,
        "assert_caps": _assert_empty_capability_sets,
        "security_prctls": _apply_anchor_security_prctls,
        "validate_parent": _exact_parent_identity,
        "validate_installed": _validate_child_installed_anchor,
        "close_foreign": _close_foreign_descriptors,
        "execve": os.execve,
    }
    ops["close"](write_fd)
    devnull = ops["open_devnull"]()
    if type(devnull) is not int:
        raise BrokerRejectedError("anchor devnull descriptor is malformed")
    for target in (0, 1, 2):
        ops["dup2"](devnull, target)
    if devnull > 2:
        ops["close"](devnull)
    if read_fd != PROMOTION_FD:
        ops["dup2"](read_fd, PROMOTION_FD)
        ops["close"](read_fd)
    ops["set_inheritable"](PROMOTION_FD, True)
    ops["chdir"]("/")
    ops["drop_bounding"]()
    ops["setgroups"]([])
    ops["setresgid"](*request.resgid)
    ops["setfsgid"](request.resgid[0])
    ops["setresuid"](*request.resuid)
    ops["setfsuid"](request.resuid[0])
    ops["verify_credentials"](request.resuid[0], request.resgid[0])
    ops["clear_caps"]()
    ops["assert_caps"]()
    ops["security_prctls"]()
    if ops["validate_parent"]() != (
        request.broker_pid,
        request.broker_start_token,
    ):
        raise BrokerRejectedError("anchor broker parent identity changed")
    ops["validate_installed"](request)
    ops["close_foreign"](frozenset((0, 1, 2, PROMOTION_FD)))
    ops["execve"](request.path, (request.path,), {})
    if not allow_exec_return:
        raise BrokerRejectedError("fixed anchor exec unexpectedly returned")


async def _await_pidfd_readiness(pidfd: int, timeout_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.add_reader(pidfd, lambda: not ready.done() and ready.set_result(None))
    try:
        await asyncio.wait_for(ready, timeout=float(timeout_seconds))
    except BaseException as primary:
        failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(pidfd),))
        _annotate_cleanup_failures(primary, failures)
        raise
    failures = _attempt_sync_cleanup_steps((lambda: loop.remove_reader(pidfd),))
    if failures:
        raise failures[0]


async def _bounded_kill_and_reap_child(
    pid: int,
    pidfd: int,
    timeout_seconds: float,
) -> None:
    """Kill and reap one still-owned child without an unbounded wait."""

    try:
        if pidfd >= 0:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + float(timeout_seconds)
    while time.monotonic() < deadline:
        try:
            observed_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if observed_pid == pid:
            return
        if observed_pid != 0:
            raise BrokerRejectedError("private anchor reap returned another child")
        await asyncio.sleep(0.001)
    raise TimeoutError("private anchor cleanup exceeded its fixed deadline")


def _close_foreign_descriptors(preserved: frozenset[int]) -> None:
    for name in os.listdir("/proc/self/fd"):
        if not name.isascii() or not name.isdigit():
            raise OSError("open descriptor inventory is malformed")
        descriptor = int(name, 10)
        if descriptor in preserved:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


def _drop_capability_bounding_and_ambient_sets() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    for capability in range(64):
        if prctl(24, capability, 0, 0, 0) != 0:  # PR_CAPBSET_DROP
            error_number = ctypes.get_errno()
            if error_number != errno.EINVAL:
                raise OSError(error_number, os.strerror(error_number))
    if prctl(47, 4, 0, 0, 0) != 0:  # PR_CAP_AMBIENT / PR_CAP_AMBIENT_CLEAR_ALL
        error_number = ctypes.get_errno()
        if error_number != errno.EINVAL:
            raise OSError(error_number, os.strerror(error_number))
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _clear_process_capability_sets() -> None:
    class Header(ctypes.Structure):
        _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))

    class Data(ctypes.Structure):
        _fields_ = (
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        )

    header = Header(0x20080522, 0)
    data = (Data * 2)()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


async def _wait_for_initial_stop(pid: int, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        observed_pid, status = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
        if observed_pid == 0:
            await asyncio.sleep(0.001)
            continue
        if observed_pid == pid and os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP:
            return
        raise BrokerRejectedError("anchor did not reach its exact first stop")
    raise TimeoutError("anchor did not reach its first stop before deadline")


def _validate_open_installed_file(
    descriptor: int,
    identity: InstalledFileIdentity,
    path: str,
) -> None:
    observed = os.fstat(descriptor)
    if (
        identity.canonical_path != path
        or observed.st_dev != identity.device
        or observed.st_ino != identity.inode
        or observed.st_mode != identity.mode
        or observed.st_uid != identity.uid
        or observed.st_gid != identity.gid
        or observed.st_nlink != identity.nlink
        or observed.st_size != identity.size
        or _sha256_descriptor(descriptor, 268_435_456) != identity.sha256
    ):
        raise BrokerRejectedError("installed anchor identity changed before exec")


def _verify_filesystem_credentials(uid: int, gid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    setfsuid = libc.setfsuid
    setfsgid = libc.setfsgid
    setfsuid.argtypes = (ctypes.c_uint,)
    setfsgid.argtypes = (ctypes.c_uint,)
    setfsuid.restype = ctypes.c_int
    setfsgid.restype = ctypes.c_int
    observed_uid = setfsuid(ctypes.c_uint(-1).value)
    observed_gid = setfsgid(ctypes.c_uint(-1).value)
    if observed_uid != uid or observed_gid != gid:
        raise BrokerRejectedError("anchor filesystem credentials are not exact")


def _set_filesystem_gid(gid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.setfsgid
    function.argtypes = (ctypes.c_uint,)
    function.restype = ctypes.c_int
    function(gid)


def _set_filesystem_uid(uid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.setfsuid
    function.argtypes = (ctypes.c_uint,)
    function.restype = ctypes.c_int
    function(uid)


def _apply_anchor_security_prctls(
    *,
    _prctl: Callable[[int, int, int, int, int], int] | None = None,
    _errno: Callable[[], int] = ctypes.get_errno,
) -> None:
    prctl = _prctl
    if prctl is None:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        prctl.restype = ctypes.c_int
    for option, value in ((38, 1), (4, 0), (1, signal.SIGKILL)):
        if prctl(option, value, 0, 0, 0) != 0:
            error_number = _errno()
            raise OSError(error_number, os.strerror(error_number))


def _assert_empty_capability_sets() -> None:
    status_text = _read_bounded_file("/proc/self/status", 65_536).decode("ascii", errors="strict")
    fields = {}
    for line in status_text.splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            fields[name] = value.strip()
    if any(
        fields.get(name) != "0000000000000000"
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise BrokerRejectedError("anchor capability sets are not all empty")


def _read_bounded_file(path: str, maximum: int) -> bytes:
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
    except BaseException as primary:
        closing_descriptor = descriptor
        descriptor = -1
        failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_descriptor),))
        _annotate_cleanup_failures(primary, failures)
        raise
    closing_descriptor = descriptor
    descriptor = -1
    failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_descriptor),))
    if failures:
        raise failures[0]
    if len(content) > maximum:
        raise BrokerRejectedError("process evidence exceeds its byte bound")
    return bytes(content)


def _read_text_file(path: str, maximum: int) -> str:
    return _read_bounded_file(path, maximum).decode("ascii", errors="strict").strip()


def _locked_probe_account_identity(
    name: str,
    *,
    passwd_by_name: Callable[[str], object] = pwd.getpwnam,
    passwd_by_uid: Callable[[int], object] = pwd.getpwuid,
    group_by_gid: Callable[[int], object] = grp.getgrgid,
    group_by_name: Callable[[str], object] = grp.getgrnam,
    shadow_reader: Callable[[], str] = lambda: _read_text_file("/etc/shadow", 4_194_304),
) -> ProbeAccountIdentity:
    if name != "mif-containment-probe":
        raise BrokerConfigurationError("startup-probe account name is not fixed")
    account = passwd_by_name(name)
    if (
        account.pw_uid == 0
        or account.pw_gid == 0
        or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")
    ):
        raise BrokerConfigurationError("startup-probe account is not locked non-login")
    if passwd_by_uid(account.pw_uid) != account:
        raise BrokerConfigurationError("startup-probe passwd mappings disagree")
    group = group_by_gid(account.pw_gid)
    if group_by_name(group.gr_name) != group or group.gr_gid != account.pw_gid:
        raise BrokerConfigurationError("startup-probe primary group mappings disagree")
    shadow = shadow_reader()
    matches = [line.split(":", 2)[1] for line in shadow.splitlines() if line.startswith(name + ":")]
    if len(matches) != 1 or not matches[0].startswith(("!", "*")):
        raise BrokerConfigurationError("startup-probe account password is not locked")
    return ProbeAccountIdentity(
        name=account.pw_name,
        uid=account.pw_uid,
        gid=account.pw_gid,
        home=account.pw_dir,
        shell=account.pw_shell,
        primary_group_name=group.gr_name,
        primary_group_members=tuple(group.gr_mem),
        shadow_lock=matches[0],
    )


def _validate_locked_probe_account(name: str) -> None:
    _locked_probe_account_identity(name)


def _sha256_descriptor(descriptor: int, maximum: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset <= maximum:
        chunk = os.pread(descriptor, min(1_048_576, maximum - offset + 1), offset)
        if not chunk:
            return digest.hexdigest()
        offset += len(chunk)
        if offset > maximum:
            raise BrokerRejectedError("executable descriptor exceeds its byte bound")
        digest.update(chunk)
    raise BrokerRejectedError("executable descriptor exceeds its byte bound")


def _process_start_token(pid: int, proc_root: str = "/proc") -> str:
    content = _read_bounded_file(f"{proc_root}/{pid}/stat", 65_536).decode("ascii", errors="strict")
    close = content.rfind(") ")
    fields = content[close + 2 :].split() if close > 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise BrokerRejectedError("process stat identity is malformed")
    return str(int(fields[19], 10))


def _broker_process_start_token(pid: int, proc_root: str = "/proc") -> str:
    return f"linux-proc-stat/v1:{pid}:{_process_start_token(pid, proc_root)}"


def _exact_parent_identity() -> tuple[int, str]:
    before = os.getppid()
    token = _broker_process_start_token(before)
    after = os.getppid()
    if before != after:
        raise BrokerRejectedError("anchor broker parent changed during observation")
    return before, token


def _validate_gate_process_evidence(
    evidence: Mapping[str, object],
    *,
    preserved_descriptors: tuple[int, ...],
) -> None:
    expected_descriptors = frozenset((0, 1, 2, 3, 4, *preserved_descriptors))
    capability_sets = evidence.get("capability_sets")
    capabilities_empty = _capability_fields_are_empty(capability_sets, include_bounding=False)
    if (
        evidence.get("open_descriptors") != expected_descriptors
        or not capabilities_empty
        or evidence.get("no_new_privileges") != 1
    ):
        raise BrokerRejectedError("gate security or descriptor profile changed")


def _validate_anchor_process_evidence(
    evidence: Mapping[str, object],
    *,
    path: str,
    phase: str,
) -> None:
    expected_descriptors = (
        frozenset((0, 1, 2, PROMOTION_FD)) if phase == "first-stop" else frozenset((0, 1, 2))
    )
    capability_sets = evidence.get("capability_sets")
    if (
        evidence.get("argv") != ("/usr/bin/python3", "-I", path)
        or evidence.get("environment") != {}
        or evidence.get("cwd_path") != "/"
        or evidence.get("groups") != ()
        or evidence.get("open_descriptors") != expected_descriptors
        or evidence.get("standard_descriptor_targets") != ("/dev/null",) * 3
        or not _capability_fields_are_empty(capability_sets, include_bounding=True)
        or evidence.get("no_new_privileges") != 1
    ):
        raise BrokerRejectedError(f"anchor {phase} security profile changed")


def _assert_installed_identity_current(expected: InstalledFileIdentity) -> None:
    observed, _content = read_root_installed_file(expected.canonical_path, 268_435_456)
    if observed != expected:
        raise BrokerRejectedError("installed executable identity changed")


def _capability_fields_are_empty(value: object, *, include_bounding: bool) -> bool:
    if type(value) is not tuple or len(value) != 5:
        return False
    indexes = range(5) if include_bounding else (0, 1, 2, 4)
    try:
        return all(
            type(value[index]) is str
            and re.fullmatch(r"[0-9a-f]{1,16}", value[index]) is not None
            and int(value[index], 16) == 0
            for index in indexes
        )
    except (TypeError, ValueError):
        return False


def _validate_released_target_process(
    evidence: Mapping[str, object],
    attachment: BrokerAttachmentRecordV1,
    gate_ready: object,
    config: BrokerConfig,
) -> None:
    """Validate released target process for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    registrations = tuple(
        item
        for item in config.registrations
        if item.registration_id == attachment.executable_registration_id
    )
    if len(registrations) != 1:
        raise BrokerRejectedError("attachment registration is unavailable")
    registration = registrations[0]
    environment = evidence.get("environment")
    if not isinstance(environment, Mapping):
        raise BrokerRejectedError("target environment observation is malformed")
    count, byte_count, digest = _environment_facts(environment)
    if (
        evidence.get("start_token") != attachment.gate_start_token
        or evidence.get("state") in ("Z", "X", "x")
        or evidence.get("uids") != (attachment.peer_uid,) * 4
        or evidence.get("gids") != (attachment.peer_gid,) * 4
        or not _capability_fields_are_empty(evidence.get("capability_sets"), include_bounding=False)
        or evidence.get("no_new_privileges") != 1
        or hashlib.sha256(dumps_canonical(list(evidence.get("argv", ())))).hexdigest()
        != gate_ready.argv_sha256
        or count != gate_ready.environment_count
        or byte_count != gate_ready.environment_bytes
        or digest != gate_ready.environment_names_sizes_sha256
        or evidence.get("cwd_device") != gate_ready.cwd_device
        or evidence.get("cwd_inode") != gate_ready.cwd_inode
        or not _descriptor_matches_registration(
            evidence.get("executable_status"),
            evidence.get("executable_path"),
            evidence.get("executable_sha256"),
            registration,
        )
    ):
        raise BrokerRejectedError("released target process identity changed")


def _verify_no_child_cgroup_local(cgroup: object) -> None:
    names = os.listdir(cgroup.descriptor)
    if len(names) > 1024:
        raise BrokerRejectedError("scope cgroup directory exceeds its entry bound")
    for name in names:
        try:
            observed = os.stat(name, dir_fd=cgroup.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            raise BrokerRejectedError("scope cgroup changed during enumeration")
        if stat.S_ISDIR(observed.st_mode):
            raise BrokerRejectedError("scope leaf contains a child cgroup")


def _require_process_cgroup_membership(
    pid: int,
    control_group: str,
    proc_root: str,
) -> None:
    content = _read_bounded_file(f"{proc_root}/{pid}/cgroup", 65_536).decode(
        "ascii", errors="strict"
    )
    if content.splitlines() != [f"0::{control_group}"]:
        raise BrokerRejectedError("process left the exact containment cgroup")


def _read_process_evidence(pid: int, proc_root: str = "/proc") -> dict[str, object]:
    stat_content = _read_bounded_file(f"{proc_root}/{pid}/stat", 65_536).decode(
        "ascii", errors="strict"
    )
    close = stat_content.rfind(") ")
    fields = stat_content[close + 2 :].split() if close > 0 else []
    if (
        len(fields) <= 19
        or not fields[1].isdigit()
        or not fields[19].isdigit()
        or len(fields[0]) != 1
    ):
        raise BrokerRejectedError("process stat evidence is malformed")
    status_content = _read_bounded_file(f"{proc_root}/{pid}/status", 65_536).decode(
        "ascii", errors="strict"
    )
    status_fields: dict[str, str] = {}
    for line in status_content.splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            if name in status_fields:
                raise BrokerRejectedError("process status evidence repeats a field")
            status_fields[name] = value.strip()
    try:
        uids = tuple(int(value, 10) for value in status_fields["Uid"].split())
        gids = tuple(int(value, 10) for value in status_fields["Gid"].split())
        groups = tuple(int(value, 10) for value in status_fields["Groups"].split())
        capability_sets = tuple(
            status_fields[name] for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        if any(re.fullmatch(r"[0-9a-f]{16}", value) is None for value in capability_sets):
            raise ValueError("capability field is malformed")
        no_new_privileges = int(status_fields["NoNewPrivs"], 10)
        if no_new_privileges not in (0, 1):
            raise ValueError("NoNewPrivs is malformed")
    except (KeyError, ValueError) as error:
        raise BrokerRejectedError("process credential evidence is malformed") from error
    executable_path = os.readlink(f"{proc_root}/{pid}/exe")
    argv_content = _read_bounded_file(f"{proc_root}/{pid}/cmdline", 262_144)
    environment_content = _read_bounded_file(
        f"{proc_root}/{pid}/environ", MAXIMUM_ENVIRONMENT_BYTES
    )
    try:
        argv = tuple(
            item.decode("utf-8", errors="strict")
            for item in argv_content.rstrip(b"\0").split(b"\0")
            if item
        )
        environment: dict[str, str] = {}
        for item in environment_content.rstrip(b"\0").split(b"\0"):
            if not item:
                continue
            name, separator, value = item.partition(b"=")
            if not separator or not name:
                raise ValueError("environment item is malformed")
            decoded_name = name.decode("utf-8", errors="strict")
            if decoded_name in environment:
                raise ValueError("environment name is duplicated")
            environment[decoded_name] = value.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise BrokerRejectedError("process argv/environment is malformed") from error
    cwd_fd = os.open(
        f"{proc_root}/{pid}/cwd",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        cwd_status = os.fstat(cwd_fd)
        cwd_path = os.readlink(f"{proc_root}/{pid}/cwd")
    except BaseException as primary:
        closing_cwd_fd = cwd_fd
        cwd_fd = -1
        failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_cwd_fd),))
        _annotate_cleanup_failures(primary, failures)
        raise
    closing_cwd_fd = cwd_fd
    cwd_fd = -1
    failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_cwd_fd),))
    if failures:
        raise failures[0]
    executable_fd = os.open(
        f"{proc_root}/{pid}/exe",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        executable_status = os.fstat(executable_fd)
        executable_sha256 = _sha256_descriptor(executable_fd, 268_435_456)
    except BaseException as primary:
        closing_executable_fd = executable_fd
        executable_fd = -1
        failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_executable_fd),))
        _annotate_cleanup_failures(primary, failures)
        raise
    closing_executable_fd = executable_fd
    executable_fd = -1
    failures = _attempt_sync_cleanup_steps((lambda: os.close(closing_executable_fd),))
    if failures:
        raise failures[0]
    descriptor_names = os.listdir(f"{proc_root}/{pid}/fd")
    if any(not name.isascii() or not name.isdigit() for name in descriptor_names):
        raise BrokerRejectedError("process descriptor evidence is malformed")
    open_descriptors = frozenset(int(name, 10) for name in descriptor_names)
    standard_descriptor_targets = tuple(
        os.readlink(f"{proc_root}/{pid}/fd/{descriptor}") for descriptor in (0, 1, 2)
    )
    return {
        "start_token": str(int(fields[19], 10)),
        "parent_pid": int(fields[1], 10),
        "state": fields[0],
        "uids": uids,
        "gids": gids,
        "groups": groups,
        "capability_sets": capability_sets,
        "no_new_privileges": no_new_privileges,
        "executable_path": executable_path,
        "executable_status": executable_status,
        "executable_sha256": executable_sha256,
        "argv": argv,
        "environment": environment,
        "cwd_device": cwd_status.st_dev,
        "cwd_inode": cwd_status.st_ino,
        "cwd_path": cwd_path,
        "open_descriptors": open_descriptors,
        "standard_descriptor_targets": standard_descriptor_targets,
    }


def _environment_facts(environment: Mapping[str, str]) -> tuple[int, int, str]:
    facts: list[dict[str, object]] = []
    total = 0
    for name in sorted(environment):
        encoded_name = name.encode("utf-8")
        encoded_value = environment[name].encode("utf-8")
        total += len(encoded_name) + 1 + len(encoded_value) + 1
        facts.append(
            {
                "name": name,
                "size": len(encoded_value),
                "value_sha256": hashlib.sha256(encoded_value).hexdigest(),
            }
        )
    return len(facts), total, hashlib.sha256(dumps_canonical(facts)).hexdigest()


def _require_live_process_token(pid: int, start_token: str, proc_root: str) -> None:
    if _process_start_token(pid, proc_root) != start_token:
        raise BrokerRejectedError("process start identity was substituted")


def _pidfd_numeric_pid(pidfd: int, proc_root: str) -> int:
    """Read the kernel identity carried by one open pidfd."""

    content = _read_text_file(f"{proc_root}/self/fdinfo/{pidfd}", 4096)
    matches = [line[4:].strip() for line in content.splitlines() if line.startswith("Pid:")]
    if len(matches) != 1 or not matches[0].isdigit():
        raise BrokerAmbiguousError("pidfd kernel identity is unavailable")
    value = int(matches[0], 10)
    if value <= 0:
        raise BrokerAmbiguousError("pidfd kernel identity is unavailable")
    return value


def _pidfd_exit_ready(pidfd: int) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


def _acquire_proven_pidfd(
    pid: int,
    expected_start_token: str,
    proc_root: str,
    *,
    token_reader: Callable[[int, str], str] = _process_start_token,
    pidfd_opener: Callable[[int, int], int] = os.pidfd_open,
    pidfd_identity_reader: Callable[[int, str], int] = _pidfd_numeric_pid,
    exit_ready: Callable[[int], bool] = _pidfd_exit_ready,
    descriptor_closer: Callable[[int], None] = os.close,
) -> int:
    """Acquire one pidfd and fence it to the expected live PID/start identity."""

    if token_reader(pid, proc_root) != expected_start_token:
        raise BrokerAmbiguousError("process start identity changed before pidfd acquisition")
    descriptor = pidfd_opener(pid, 0)
    try:
        if pidfd_identity_reader(descriptor, proc_root) != pid:
            raise BrokerAmbiguousError("pidfd refers to a substituted process")
        if exit_ready(descriptor):
            raise BrokerAmbiguousError("pidfd process exited during acquisition")
        if token_reader(pid, proc_root) != expected_start_token:
            raise BrokerAmbiguousError("process start identity changed during pidfd acquisition")
        if exit_ready(descriptor):
            raise BrokerAmbiguousError("pidfd process exited during acquisition")
        return descriptor
    except BaseException as primary:
        failures = _attempt_sync_cleanup_steps((lambda: descriptor_closer(descriptor),))
        _annotate_cleanup_failures(primary, failures)
        raise


__all__ = [
    "ActivatedSeqpacketBrokerService",
    "AnchorHandle",
    "AnchorLaunchRequest",
    "BROKER_BUILD_ID",
    "BrokerAuthenticationError",
    "BrokerConfig",
    "BrokerConfigurationError",
    "BrokerContext",
    "BrokerError",
    "BrokerOperationBackend",
    "BrokerRejectedError",
    "BrokerReply",
    "BrokerRevokedError",
    "BrokerTransport",
    "ContainmentAuthorityRuntime",
    "ContainmentOperationBackend",
    "ExecutableRegistration",
    "GateAttachmentEvidence",
    "InstalledExecutable",
    "InstalledFileIdentity",
    "LedgerStartupReconciler",
    "PasswdAccount",
    "PeerAuthenticatedBroker",
    "PeerIdentity",
    "PosixContainmentAuthorityRuntime",
    "ReconciledAttachmentDisposition",
    "ScopeAnchorLauncher",
    "StartupReconciler",
    "SystemdBrokerNotifier",
    "authenticate_peer",
    "build_anchor_launch_request",
    "derive_scope_unit_name",
    "derive_user_environment",
    "containment_broker_main",
    "load_broker_config",
    "parse_broker_config",
    "safe_error_reply",
    "read_root_installed_file",
    "receive_bounded_seqpacket",
    "validate_socket_activation",
]
