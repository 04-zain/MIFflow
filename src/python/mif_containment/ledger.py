"""Descriptor-relative immutable root ledger for containment authority.

The `ContainmentRequestReservationV1` record carries `schema_name`, `schema_version`,
`slot`, `authenticated_uid`, `primary_gid` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, TypeAlias

from .codec import dumps_canonical, loads_canonical_packet
from .protocol import (
    AttachPayloadV1,
    AuthorityBindingV1,
    AwaitDrainPayloadV1,
    BrokerAttachmentRecordV1,
    BrokerAttemptRecordV1,
    BrokerDrainRecordV1,
    BrokerReleaseIntentRecordV1,
    BrokerReleaseRecordV1,
    BrokerStopIntentRecordV1,
    CgroupDeletedAfterStopProofV1,
    CgroupEventsZeroProofV1,
    BrokerOperation,
    BrokerRequestV1,
    GateAuthorizeExecPayloadV1,
    RecoverStopPayloadV1,
    ReleasePayloadV1,
    StopPayloadV1,
)

_MAXIMUM_RECORD_BYTES = 65_536
_DIRECTORY_MODE = 0o700
_RECORD_MODE = 0o600
_ROOT_UID = 0
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CANONICAL_UID = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_AUTHORITY_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_START_TOKEN = re.compile(r"[0-9]+\Z")
_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


# ==============================================================================
#                                     Ledger
# ==============================================================================


def _linux_rename_noreplace(
    source: str,
    target: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename one descriptor-relative name without replacement."""

    if _RENAMEAT2 is None:
        raise OSError("renameat2(RENAME_NOREPLACE) is unavailable")
    result = _RENAMEAT2(
        src_dir_fd,
        os.fsencode(source),
        dst_dir_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), target)


RootRecordV1: TypeAlias = (
    BrokerAttemptRecordV1
    | BrokerAttachmentRecordV1
    | BrokerReleaseIntentRecordV1
    | BrokerReleaseRecordV1
    | BrokerStopIntentRecordV1
    | BrokerDrainRecordV1
)


class ContainmentLedgerError(RuntimeError):
    """Base class for fail-closed ledger errors."""


class ContainmentLedgerSecurityError(ContainmentLedgerError):
    """The configured ledger or an opened member violates security policy."""


class ContainmentLedgerConflictError(ContainmentLedgerError):
    """An immutable family already contains different canonical bytes."""


class ContainmentLedgerIncompatibleError(ContainmentLedgerError):
    """A known leaf declares a recognizable unsupported schema identity."""


class ContainmentLedgerOrderError(ContainmentLedgerError):
    """A record does not extend the exact durable predecessor chain."""


class ContainmentLedgerState(str, Enum):
    PRISTINE = "pristine"
    RESERVATION_ONLY = "reservation_only"
    ATTEMPT_ONLY = "attempt_only"
    ATTACHED_STOPPED = "attached_stopped"
    RELEASED_LIVE = "released_live"
    EMPTY_WITHOUT_DRAIN = "empty_without_drain"
    DRAINED = "drained"
    PRIOR_BOOT_CANDIDATE = "prior_boot_candidate"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class PersistedContainmentRecord:
    """A parsed record paired with the exact canonical bytes reopened from disk."""

    record: RootRecordV1
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class ContainmentReconciliationObservation:
    """Narrow trusted host facts used only to refine a valid ledger chain."""

    current_boot_id: str
    unit_name: str | None = None
    invocation_id: str | None = None
    control_group: str | None = None
    drain_proof: CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1 | None = None

    def __post_init__(self) -> None:
        if type(self.current_boot_id) is not str or not _BOOT_ID.fullmatch(self.current_boot_id):
            raise ValueError("current_boot_id must be a lowercase UUID")
        scope_values = (self.unit_name, self.invocation_id, self.control_group)
        present = tuple(value is not None for value in scope_values)
        if any(present) != all(present):
            raise ValueError("scope observation identity must be complete or absent")
        if self.drain_proof is not None and not isinstance(
            self.drain_proof,
            (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1),
        ):
            raise ValueError("drain_proof must be one exact same-boot cgroup proof")
        if isinstance(self.drain_proof, CgroupEventsZeroProofV1) and not all(present):
            raise ValueError("cgroup-events-zero proof requires complete scope identity")
        if all(present) and self.drain_proof is None:
            raise ValueError("observed scope identity requires a drain_proof")


@dataclass(frozen=True, slots=True)
class ContainmentLedgerChain:
    """Exact parsed record chain and its conservative reconciliation state."""

    state: ContainmentLedgerState
    attempt: PersistedContainmentRecord | None = None
    attachment: PersistedContainmentRecord | None = None
    release_intent: PersistedContainmentRecord | None = None
    release: PersistedContainmentRecord | None = None
    stop_intent: PersistedContainmentRecord | None = None
    drain: PersistedContainmentRecord | None = None
    ambiguity_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContainmentLedgerInventoryEntry:
    """One path-bound durable authority discovered through the retained root."""

    authenticated_uid: int
    launch_nonce_sha256: str
    authority: AuthorityBindingV1
    chain: ContainmentLedgerChain
    reservations: tuple["PersistedContainmentRequestReservation", ...] = ()


@dataclass(frozen=True, slots=True)
class ContainmentLedgerInventory:
    """Immutable bounded snapshot of every durable ledger authority."""

    entries: tuple[ContainmentLedgerInventoryEntry, ...]
    record_count: int


class ContainmentRequestReservationSlot(str, Enum):
    ATTACH = "attach"
    RELEASE = "release"
    GATE_AUTHORIZATION = "gate-authorization"
    TERMINAL = "terminal"


_RESERVATION_SLOT_OPERATIONS = {
    ContainmentRequestReservationSlot.ATTACH: (BrokerOperation.ATTACH,),
    ContainmentRequestReservationSlot.RELEASE: (BrokerOperation.RELEASE,),
    ContainmentRequestReservationSlot.GATE_AUTHORIZATION: (BrokerOperation.GATE_AUTHORIZE_EXEC,),
    ContainmentRequestReservationSlot.TERMINAL: (
        BrokerOperation.AWAIT_DRAIN,
        BrokerOperation.STOP,
        BrokerOperation.RECOVER_STOP,
    ),
}


def _request_launch_nonce(request: BrokerRequestV1) -> str | None:
    payload_nonce = getattr(request.payload, "launch_nonce_sha256", None)
    authority = getattr(request.payload, "authority", None)
    authority_nonce = getattr(authority, "launch_nonce_sha256", None)
    if payload_nonce is not None and authority_nonce is not None:
        raise ValueError("request carries two launch nonce authorities")
    return payload_nonce if payload_nonce is not None else authority_nonce


@dataclass(frozen=True, slots=True)
class ContainmentRequestReservationV1:
    """One fixed-slot authenticated mutation reservation."""

    schema_name: Literal["mif-containment-broker-request-reservation"]
    schema_version: Literal[1]
    slot: ContainmentRequestReservationSlot
    authenticated_uid: int
    primary_gid: int
    peer_pid: int
    peer_start_token: str
    launch_nonce_sha256: str
    operation: BrokerOperation
    request_id: str
    canonical_request_json_utf8: str
    canonical_request_sha256: str
    broker_build_id: str
    broker_config_sha256: str
    reservation_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_name != "mif-containment-broker-request-reservation"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("reservation schema identity is invalid")
        if not isinstance(self.slot, ContainmentRequestReservationSlot) or not isinstance(
            self.operation, BrokerOperation
        ):
            raise ValueError("reservation slot or operation is invalid")
        if self.operation not in _RESERVATION_SLOT_OPERATIONS[self.slot]:
            raise ValueError("reservation operation does not match its fixed slot")

    @classmethod
    def from_request(
        cls,
        *,
        authenticated_uid: int,
        primary_gid: int,
        peer_pid: int,
        peer_start_token: str,
        request: BrokerRequestV1,
        canonical_request_bytes: bytes,
        broker_build_id: str,
        broker_config_sha256: str,
    ) -> "ContainmentRequestReservationV1":
        if type(request) is not BrokerRequestV1:
            raise TypeError("request must be BrokerRequestV1")
        if (
            type(canonical_request_bytes) is not bytes
            or canonical_request_bytes != request.canonical_bytes()
        ):
            raise ValueError("canonical request bytes do not match the exact request")
        launch_nonce = _request_launch_nonce(request)
        if launch_nonce is None:
            raise ValueError("read-only request cannot occupy a mutation reservation")
        slot = next(
            (
                candidate
                for candidate, operations in _RESERVATION_SLOT_OPERATIONS.items()
                if request.operation in operations
            ),
            None,
        )
        if slot is None:
            raise ValueError("request operation has no durable reservation slot")
        material = {
            "schema_name": "mif-containment-broker-request-reservation",
            "schema_version": 1,
            "slot": slot.value,
            "authenticated_uid": authenticated_uid,
            "primary_gid": primary_gid,
            "peer_pid": peer_pid,
            "peer_start_token": peer_start_token,
            "launch_nonce_sha256": launch_nonce,
            "operation": request.operation.value,
            "request_id": request.request_id,
            "canonical_request_json_utf8": canonical_request_bytes.decode("utf-8"),
            "canonical_request_sha256": hashlib.sha256(canonical_request_bytes).hexdigest(),
            "broker_build_id": broker_build_id,
            "broker_config_sha256": broker_config_sha256,
        }
        return cls._from_mapping(
            {
                **material,
                "reservation_sha256": hashlib.sha256(dumps_canonical(material)).hexdigest(),
            }
        )

    @classmethod
    def _from_mapping(cls, value: object) -> "ContainmentRequestReservationV1":
        if not isinstance(value, Mapping):
            raise ValueError("reservation must be an object")
        required = {
            "schema_name",
            "schema_version",
            "slot",
            "authenticated_uid",
            "primary_gid",
            "peer_pid",
            "peer_start_token",
            "launch_nonce_sha256",
            "operation",
            "request_id",
            "canonical_request_json_utf8",
            "canonical_request_sha256",
            "broker_build_id",
            "broker_config_sha256",
            "reservation_sha256",
        }
        if set(value) != required:
            raise ValueError("reservation fields are not exact")
        if (
            value["schema_name"] != "mif-containment-broker-request-reservation"
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("reservation schema identity is invalid")
        try:
            slot = ContainmentRequestReservationSlot(value["slot"])
            operation = BrokerOperation(value["operation"])
        except (TypeError, ValueError) as exc:
            raise ValueError("reservation slot or operation is invalid") from exc
        if operation not in _RESERVATION_SLOT_OPERATIONS[slot]:
            raise ValueError("reservation operation does not match its fixed slot")
        for name in ("authenticated_uid", "primary_gid", "peer_pid"):
            item = value[name]
            minimum = 1 if name == "peer_pid" else 0
            if type(item) is not int or item < minimum:
                raise ValueError(f"reservation {name} is invalid")
        peer_start_token = value["peer_start_token"]
        if (
            type(peer_start_token) is not str
            or len(peer_start_token) > 128
            or _PROCESS_START_TOKEN.fullmatch(peer_start_token) is None
            or int(peer_start_token, 10) < 1
        ):
            raise ValueError("reservation peer_start_token is invalid")
        request_id = value["request_id"]
        if type(request_id) is not str or not request_id or len(request_id.encode("utf-8")) > 128:
            raise ValueError("reservation request_id is invalid")
        broker_build_id = value["broker_build_id"]
        if (
            type(broker_build_id) is not str
            or not broker_build_id
            or len(broker_build_id) > 128
            or any(ord(character) < 32 for character in broker_build_id)
        ):
            raise ValueError("reservation broker_build_id is invalid")
        for name in (
            "launch_nonce_sha256",
            "canonical_request_sha256",
            "broker_config_sha256",
            "reservation_sha256",
        ):
            if type(value[name]) is not str or _AUTHORITY_NONCE.fullmatch(value[name]) is None:
                raise ValueError(f"reservation {name} is invalid")
        raw_text = value["canonical_request_json_utf8"]
        if type(raw_text) is not str:
            raise ValueError("reservation canonical request is invalid")
        raw = raw_text.encode("utf-8")
        if not raw or len(raw) > _MAXIMUM_RECORD_BYTES:
            raise ValueError("reservation canonical request exceeds its fixed bound")
        parsed_request = BrokerRequestV1.from_mapping(loads_canonical_packet(raw))
        if parsed_request.canonical_bytes() != raw:
            raise ValueError("reservation request bytes are not canonical")
        if (
            parsed_request.operation is not operation
            or parsed_request.request_id != value["request_id"]
            or _request_launch_nonce(parsed_request) != value["launch_nonce_sha256"]
        ):
            raise ValueError("reservation request binding is inconsistent")
        if hashlib.sha256(raw).hexdigest() != value["canonical_request_sha256"]:
            raise ValueError("reservation request digest is inconsistent")
        material = dict(value)
        digest = material.pop("reservation_sha256")
        if hashlib.sha256(dumps_canonical(material)).hexdigest() != digest:
            raise ValueError("reservation self digest is inconsistent")
        return cls(
            schema_name=value["schema_name"],
            schema_version=value["schema_version"],
            slot=slot,
            authenticated_uid=value["authenticated_uid"],
            primary_gid=value["primary_gid"],
            peer_pid=value["peer_pid"],
            peer_start_token=value["peer_start_token"],
            launch_nonce_sha256=value["launch_nonce_sha256"],
            operation=operation,
            request_id=value["request_id"],
            canonical_request_json_utf8=raw_text,
            canonical_request_sha256=value["canonical_request_sha256"],
            broker_build_id=value["broker_build_id"],
            broker_config_sha256=value["broker_config_sha256"],
            reservation_sha256=digest,
        )

    def canonical_bytes(self) -> bytes:
        return dumps_canonical(
            {
                "schema_name": self.schema_name,
                "schema_version": self.schema_version,
                "slot": self.slot.value,
                "authenticated_uid": self.authenticated_uid,
                "primary_gid": self.primary_gid,
                "peer_pid": self.peer_pid,
                "peer_start_token": self.peer_start_token,
                "launch_nonce_sha256": self.launch_nonce_sha256,
                "operation": self.operation.value,
                "request_id": self.request_id,
                "canonical_request_json_utf8": self.canonical_request_json_utf8,
                "canonical_request_sha256": self.canonical_request_sha256,
                "broker_build_id": self.broker_build_id,
                "broker_config_sha256": self.broker_config_sha256,
                "reservation_sha256": self.reservation_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class PersistedContainmentRequestReservation:
    reservation: ContainmentRequestReservationV1
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class _Family:
    leaf: str
    record_type: type[RootRecordV1]


_FAMILIES = (
    _Family("attempt.json", BrokerAttemptRecordV1),
    _Family("attachment.json", BrokerAttachmentRecordV1),
    _Family("release-intent.json", BrokerReleaseIntentRecordV1),
    _Family("release.json", BrokerReleaseRecordV1),
    _Family("stop-intent.json", BrokerStopIntentRecordV1),
    _Family("drain.json", BrokerDrainRecordV1),
)
_FAMILY_BY_TYPE = {family.record_type: family for family in _FAMILIES}


@dataclass(frozen=True, slots=True)
class _ReservationFamily:
    slot: ContainmentRequestReservationSlot
    leaf: str


_RESERVATION_FAMILIES = (
    _ReservationFamily(ContainmentRequestReservationSlot.ATTACH, "request-attach.json"),
    _ReservationFamily(ContainmentRequestReservationSlot.RELEASE, "request-release.json"),
    _ReservationFamily(
        ContainmentRequestReservationSlot.GATE_AUTHORIZATION, "request-gate-authorization.json"
    ),
    _ReservationFamily(ContainmentRequestReservationSlot.TERMINAL, "request-terminal.json"),
)
_RESERVATION_BY_SLOT = {family.slot: family for family in _RESERVATION_FAMILIES}


class _AuthorityDirectoryMissing(Exception):
    pass


def _reject_incompatible_schema(decoded: object, *, expected_name: str, leaf: str) -> None:
    """Separate typed future schema identity from malformed known-leaf bytes."""

    if not isinstance(decoded, Mapping):
        return
    name = decoded.get("schema_name")
    version = decoded.get("schema_version")
    if type(name) is str and type(version) is int and (name != expected_name or version != 1):
        raise ContainmentLedgerIncompatibleError(f"{leaf} declares an unsupported schema identity")


class ContainmentLedger:
    """Persist containment records beneath one retained root directory identity."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        _fstat: Callable[[int], object] = os.fstat,
        _fdatasync: Callable[[int], None] = getattr(os, "fdatasync", os.fsync),
        _fsync: Callable[[int], None] = os.fsync,
        _crash_hook: Callable[[str], None] | None = None,
        _rename_noreplace: Callable[..., None] = _linux_rename_noreplace,
    ) -> None:
        self._fstat = _fstat
        self._fdatasync = _fdatasync
        self._fsync = _fsync
        self._crash_hook = _crash_hook or (lambda _point: None)
        self._rename_noreplace = _rename_noreplace
        self._root_fd = -1
        try:
            self._root_fd = os.open(Path(root), _DIRECTORY_FLAGS)
            root_stat = self._fstat(self._root_fd)
            self._validate_root(root_stat)
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        except OSError as exc:
            self.close()
            raise ContainmentLedgerSecurityError(
                "ledger root must be an opened no-follow directory"
            ) from exc
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "ContainmentLedger":
        self._require_open_root()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._root_fd >= 0:
            descriptor = self._root_fd
            self._root_fd = -1
            os.close(descriptor)

    def publish(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
        record: RootRecordV1,
    ) -> PersistedContainmentRecord:
        """Create one fixed-family record or return an exact immutable replay."""

        self._validate_request_identity(authenticated_uid, authority)
        if type(record) not in _FAMILY_BY_TYPE:
            raise TypeError("record must be one of the six containment root record types")
        if record.peer_uid != authenticated_uid or record.authority != authority:
            raise ContainmentLedgerOrderError(
                "record UID and authority must match authenticated ledger authority"
            )
        directory_fd = self._open_authority_directory(
            authenticated_uid,
            authority,
            create=True,
        )
        try:
            with self._authority_transaction(directory_fd):
                family = _FAMILY_BY_TYPE[type(record)]
                intended = record.canonical_json_utf8().encode("utf-8")
                entries = self._read_all(directory_fd)
                family_index = _FAMILIES.index(family)
                existing = entries[family_index]
                if existing is not None:
                    if existing.canonical_bytes != intended:
                        raise ContainmentLedgerConflictError(
                            f"immutable {family.leaf} conflicts with requested canonical bytes"
                        )
                    observed = self._classify(
                        authenticated_uid,
                        authority,
                        entries,
                        observation=None,
                    )
                    if observed.state is ContainmentLedgerState.AMBIGUOUS:
                        raise ContainmentLedgerOrderError(
                            "exact replay belongs to an ambiguous chain: "
                            f"{observed.ambiguity_reason or 'ambiguous'}"
                        )
                    self._fsync(directory_fd)
                    return existing

                if any(entry is not None for entry in entries[family_index + 1 :]):
                    raise ContainmentLedgerOrderError(
                        "an earlier record family cannot be authored after a successor"
                    )
                proposed = list(entries)
                proposed[family_index] = PersistedContainmentRecord(
                    record=record,
                    canonical_bytes=intended,
                )
                chain = self._classify(
                    authenticated_uid,
                    authority,
                    tuple(proposed),
                    observation=None,
                )
                if chain.state is ContainmentLedgerState.AMBIGUOUS:
                    raise ContainmentLedgerOrderError(
                        "record does not extend the exact predecessor chain: "
                        f"{chain.ambiguity_reason or 'ambiguous'}"
                    )
                return self._publish_bytes(directory_fd, family, intended)
        finally:
            os.close(directory_fd)

    @contextmanager
    def _authority_transaction(self, directory_fd: int):
        try:
            lock_fd = os.open(
                ".transition.lock",
                _LOCK_FLAGS,
                _RECORD_MODE,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ContainmentLedgerSecurityError("authority transaction lock is unsafe") from exc
        try:
            self._validate_record_stat(
                self._fstat(lock_fd),
                label="authority transaction lock",
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def load_chain(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
        *,
        observation: ContainmentReconciliationObservation | None = None,
    ) -> ContainmentLedgerChain:
        """Load and classify an exact authority chain without inferring host facts.

        The decoded fields are checked through the record constructors and the applicable
        context comparisons before returning the result. The validated representation is
        returned for the subsequent operation."""

        self._validate_request_identity(authenticated_uid, authority)
        if observation is not None and not isinstance(
            observation, ContainmentReconciliationObservation
        ):
            raise TypeError("observation must be ContainmentReconciliationObservation")
        try:
            directory_fd = self._open_authority_directory(
                authenticated_uid,
                authority,
                create=False,
            )
        except _AuthorityDirectoryMissing:
            return ContainmentLedgerChain(state=ContainmentLedgerState.PRISTINE)
        except ContainmentLedgerSecurityError as exc:
            return self._ambiguous(str(exc))
        try:
            try:
                with self._authority_transaction(directory_fd):
                    self._fsync(directory_fd)
                    durable_leaves = self._live_authority_leaves(directory_fd)
                    entries = self._read_all(directory_fd)
                    reservations = tuple(
                        item
                        for family in _RESERVATION_FAMILIES
                        if (item := self._read_reservation(directory_fd, family)) is not None
                    )
                    if sum(item is not None for item in entries) + len(reservations) != len(
                        durable_leaves
                    ):
                        raise ContainmentLedgerSecurityError(
                            "ledger authority changed during live reopening"
                        )
                    for persisted in reservations:
                        reservation = persisted.reservation
                        if (
                            reservation.authenticated_uid != authenticated_uid
                            or reservation.launch_nonce_sha256 != authority.launch_nonce_sha256
                        ):
                            raise ContainmentLedgerSecurityError(
                                "reservation path authority is inconsistent"
                            )
                        request = BrokerRequestV1.from_mapping(
                            loads_canonical_packet(
                                reservation.canonical_request_json_utf8.encode("utf-8")
                            )
                        )
                        request_authority = getattr(request.payload, "authority", None)
                        if request_authority is not None and request_authority != authority:
                            raise ContainmentLedgerSecurityError(
                                "reservation request authority is inconsistent"
                            )
                    chain = self._classify(
                        authenticated_uid,
                        authority,
                        entries,
                        observation=observation,
                    )
                    if chain.attempt is not None or any(item is not None for item in entries):
                        return chain
                    if len(reservations) != 1:
                        return chain
                    reservation = reservations[0].reservation
                    if (
                        reservation.slot is not ContainmentRequestReservationSlot.ATTACH
                        or reservation.operation is not BrokerOperation.ATTACH
                        or reservation.authenticated_uid != authenticated_uid
                        or reservation.launch_nonce_sha256 != authority.launch_nonce_sha256
                    ):
                        return chain
                    request = BrokerRequestV1.from_mapping(
                        loads_canonical_packet(
                            reservation.canonical_request_json_utf8.encode("utf-8")
                        )
                    )
                    if getattr(request.payload, "authority", None) != authority:
                        return chain
                    return ContainmentLedgerChain(state=ContainmentLedgerState.RESERVATION_ONLY)
            except ContainmentLedgerIncompatibleError as exc:
                return ContainmentLedgerChain(
                    state=ContainmentLedgerState.INCOMPATIBLE,
                    ambiguity_reason=str(exc),
                )
            except (
                ContainmentLedgerSecurityError,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                return self._ambiguous(str(exc))
        finally:
            os.close(directory_fd)

    def reserve_request(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
        reservation: ContainmentRequestReservationV1,
    ) -> PersistedContainmentRequestReservation:
        """Create one fixed mutation slot, or return its exact immutable replay."""

        self._validate_request_identity(authenticated_uid, authority)
        if type(reservation) is not ContainmentRequestReservationV1:
            raise TypeError("reservation must be ContainmentRequestReservationV1")
        if (
            reservation.authenticated_uid != authenticated_uid
            or reservation.launch_nonce_sha256 != authority.launch_nonce_sha256
        ):
            raise ContainmentLedgerOrderError("reservation does not match ledger authority")
        intended = reservation.canonical_bytes()
        try:
            validated_reservation = ContainmentRequestReservationV1._from_mapping(
                loads_canonical_packet(intended)
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentLedgerSecurityError(
                "request reservation is not a closed canonical record"
            ) from exc
        if validated_reservation != reservation:
            raise ContainmentLedgerSecurityError(
                "request reservation changed during canonical validation"
            )
        request = BrokerRequestV1.from_mapping(
            loads_canonical_packet(reservation.canonical_request_json_utf8.encode("utf-8"))
        )
        request_authority = getattr(request.payload, "authority", None)
        if request_authority is not None and request_authority != authority:
            raise ContainmentLedgerOrderError("reservation request authority is inconsistent")
        directory_fd = self._open_authority_directory(authenticated_uid, authority, create=True)
        try:
            with self._authority_transaction(directory_fd):
                family = _RESERVATION_BY_SLOT[reservation.slot]
                existing = self._read_reservation(directory_fd, family)
                if existing is not None:
                    if existing.canonical_bytes != intended:
                        raise ContainmentLedgerConflictError(
                            f"immutable {family.slot.value} request reservation conflicts"
                        )
                    self._fsync(directory_fd)
                    return existing
                return self._publish_reservation(directory_fd, family, intended)
        finally:
            os.close(directory_fd)

    def load_request_reservations(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
    ) -> tuple[PersistedContainmentRequestReservation, ...]:
        """Load all occupied fixed reservation slots for one exact authority."""

        self._validate_request_identity(authenticated_uid, authority)
        try:
            directory_fd = self._open_authority_directory(
                authenticated_uid, authority, create=False
            )
        except _AuthorityDirectoryMissing:
            return ()
        try:
            with self._authority_transaction(directory_fd):
                try:
                    self._fsync(directory_fd)
                except OSError as exc:
                    raise ContainmentLedgerSecurityError(
                        "request reservation authority durability healing failed"
                    ) from exc
                reservations = tuple(
                    item
                    for family in _RESERVATION_FAMILIES
                    if (item := self._read_reservation(directory_fd, family)) is not None
                )
                for persisted in reservations:
                    reservation = persisted.reservation
                    if (
                        reservation.authenticated_uid != authenticated_uid
                        or reservation.launch_nonce_sha256 != authority.launch_nonce_sha256
                    ):
                        raise ContainmentLedgerSecurityError(
                            "reservation path authority is inconsistent"
                        )
                return reservations
        finally:
            os.close(directory_fd)

    def inventory(
        self,
        *,
        maximum_records: int,
        monotonic_deadline: float,
        _monotonic: Callable[[], float] = time.monotonic,
        _scandir: Callable[[int], object] = os.scandir,
        _flock: Callable[[int, int], object] = fcntl.flock,
    ) -> ContainmentLedgerInventory:
        """Discover every durable authority through one bounded safe traversal.

        Directory layout and record-family details remain encapsulated here;
        callers receive only path-bound typed authorities and classified chains.
        """

        if type(maximum_records) is not int or maximum_records < 1:
            raise ValueError("maximum_records must be a positive finite integer")
        if (
            type(monotonic_deadline) not in (int, float)
            or not math.isfinite(monotonic_deadline)
            or monotonic_deadline <= 0
        ):
            raise ValueError("monotonic_deadline must be a positive finite number")
        if not callable(_monotonic):
            raise TypeError("_monotonic must be callable")
        if not callable(_scandir):
            raise TypeError("_scandir must be callable")
        if not callable(_flock):
            raise TypeError("_flock must be callable")

        def check_deadline() -> None:
            try:
                now = _monotonic()
            except BaseException as exc:
                raise ContainmentLedgerSecurityError(
                    "ledger inventory monotonic observer failed"
                ) from exc
            if type(now) not in (int, float) or not math.isfinite(now):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory monotonic observation is malformed"
                )
            if now >= monotonic_deadline:
                raise ContainmentLedgerSecurityError("ledger inventory deadline expired")

        structural_limit = 8 + (maximum_records * 16)
        structural_count = 0

        def consume_structural_entry() -> None:
            nonlocal structural_count
            structural_count += 1
            if structural_count > structural_limit:
                raise ContainmentLedgerSecurityError(
                    "ledger inventory structural entry budget exceeded"
                )

        def inventory_names(directory_fd: int) -> tuple[str, ...]:
            return self._inventory_names(
                directory_fd,
                check_deadline=check_deadline,
                consume_structural_entry=consume_structural_entry,
                scandir=_scandir,
            )

        check_deadline()
        self._require_open_root()
        check_deadline()
        root_names = inventory_names(self._root_fd)
        check_deadline()
        if not root_names:
            return ContainmentLedgerInventory(entries=(), record_count=0)
        if root_names != ("v1",):
            raise ContainmentLedgerSecurityError(
                "ledger root contains an unknown inventory component"
            )

        v1_fd = self._inventory_open_directory(self._root_fd, "v1")
        try:
            check_deadline()
            version_names = inventory_names(v1_fd)
            check_deadline()
            if not version_names:
                return ContainmentLedgerInventory(entries=(), record_count=0)
            if version_names != ("users",):
                raise ContainmentLedgerSecurityError(
                    "ledger version contains an unknown inventory component"
                )
            users_fd = self._inventory_open_directory(v1_fd, "users")
            try:
                check_deadline()
                entries: list[ContainmentLedgerInventoryEntry] = []
                record_count = 0
                for uid_name in inventory_names(users_fd):
                    check_deadline()
                    if _CANONICAL_UID.fullmatch(uid_name) is None:
                        raise ContainmentLedgerSecurityError(
                            "ledger inventory UID component is not canonical"
                        )
                    authenticated_uid = int(uid_name, 10)
                    if str(authenticated_uid) != uid_name:
                        raise ContainmentLedgerSecurityError(
                            "ledger inventory UID component changed on parse"
                        )
                    uid_fd = self._inventory_open_directory(users_fd, uid_name)
                    try:
                        check_deadline()
                        for nonce in inventory_names(uid_fd):
                            check_deadline()
                            if _AUTHORITY_NONCE.fullmatch(nonce) is None:
                                raise ContainmentLedgerSecurityError(
                                    "ledger inventory nonce component is malformed"
                                )
                            authority_fd = self._inventory_open_directory(uid_fd, nonce)
                            try:
                                check_deadline()
                                leaf_names = inventory_names(authority_fd)
                                check_deadline()
                                durable_leaves = self._validate_inventory_leaves(
                                    authority_fd,
                                    leaf_names,
                                    check_deadline=check_deadline,
                                )
                                if not durable_leaves:
                                    continue
                                if record_count + len(durable_leaves) > maximum_records:
                                    raise ContainmentLedgerSecurityError(
                                        "ledger inventory record budget exceeded"
                                    )
                                with self._inventory_authority_transaction(
                                    authority_fd,
                                    check_deadline=check_deadline,
                                    flock=_flock,
                                ):
                                    check_deadline()
                                    try:
                                        self._fsync(authority_fd)
                                    except OSError as exc:
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory authority durability healing failed"
                                        ) from exc
                                    check_deadline()
                                    reopened: list[PersistedContainmentRecord | None] = []
                                    for family in _FAMILIES:
                                        check_deadline()
                                        reopened.append(self._read_record(authority_fd, family))
                                        check_deadline()
                                    reopened_reservations: list[
                                        PersistedContainmentRequestReservation
                                    ] = []
                                    for family in _RESERVATION_FAMILIES:
                                        check_deadline()
                                        reservation = self._read_reservation(authority_fd, family)
                                        if reservation is not None:
                                            reopened_reservations.append(reservation)
                                        check_deadline()
                                actual_count = sum(item is not None for item in reopened) + len(
                                    reopened_reservations
                                )
                                if actual_count != len(durable_leaves):
                                    raise ContainmentLedgerSecurityError(
                                        "ledger inventory changed during reopening"
                                    )
                                record_count += actual_count
                                parsed_reservations: list[
                                    tuple[PersistedContainmentRequestReservation, BrokerRequestV1]
                                ] = []
                                for persisted_reservation in reopened_reservations:
                                    reservation = persisted_reservation.reservation
                                    request = BrokerRequestV1.from_mapping(
                                        loads_canonical_packet(
                                            reservation.canonical_request_json_utf8.encode("utf-8")
                                        )
                                    )
                                    if (
                                        reservation.authenticated_uid != authenticated_uid
                                        or reservation.launch_nonce_sha256 != nonce
                                    ):
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory reservation path differs from authority"
                                        )
                                    parsed_reservations.append((persisted_reservation, request))
                                attempt = reopened[0]
                                if attempt is None:
                                    if (
                                        any(item is not None for item in reopened[1:])
                                        or len(parsed_reservations) != 1
                                        or parsed_reservations[0][0].reservation.slot
                                        is not ContainmentRequestReservationSlot.ATTACH
                                        or parsed_reservations[0][1].operation
                                        is not BrokerOperation.ATTACH
                                    ):
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory authority has no exact attempt"
                                        )
                                    authority = getattr(
                                        parsed_reservations[0][1].payload, "authority", None
                                    )
                                    if (
                                        type(authority) is not AuthorityBindingV1
                                        or authority.launch_nonce_sha256 != nonce
                                    ):
                                        raise ContainmentLedgerSecurityError(
                                            "reservation-only path differs from request authority"
                                        )
                                    self._validate_inventory_reservation(
                                        parsed_reservations[0][0].reservation,
                                        parsed_reservations[0][1],
                                        attempt=None,
                                        attachment=None,
                                        release_intent=None,
                                        stop_intent=None,
                                        drain=None,
                                    )
                                    chain = ContainmentLedgerChain(
                                        state=ContainmentLedgerState.RESERVATION_ONLY
                                    )
                                else:
                                    if not isinstance(attempt.record, BrokerAttemptRecordV1):
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory authority has no exact attempt"
                                        )
                                    authority = attempt.record.authority
                                    if (
                                        attempt.record.peer_uid != authenticated_uid
                                        or authority.launch_nonce_sha256 != nonce
                                    ):
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory path differs from attempt authority"
                                        )
                                    chain = self._classify(
                                        authenticated_uid,
                                        authority,
                                        tuple(reopened),
                                        observation=None,
                                    )
                                    if chain.state is ContainmentLedgerState.AMBIGUOUS:
                                        raise ContainmentLedgerSecurityError(
                                            "ledger inventory contains an ambiguous chain"
                                        )
                                    for persisted_reservation, request in parsed_reservations:
                                        self._validate_inventory_reservation(
                                            persisted_reservation.reservation,
                                            request,
                                            attempt=attempt.record,
                                            attachment=(
                                                None
                                                if chain.attachment is None
                                                else chain.attachment.record
                                            ),
                                            release_intent=(
                                                None
                                                if chain.release_intent is None
                                                else chain.release_intent.record
                                            ),
                                            stop_intent=(
                                                None
                                                if chain.stop_intent is None
                                                else chain.stop_intent.record
                                            ),
                                            drain=(
                                                None if chain.drain is None else chain.drain.record
                                            ),
                                        )
                                entries.append(
                                    ContainmentLedgerInventoryEntry(
                                        authenticated_uid=authenticated_uid,
                                        launch_nonce_sha256=nonce,
                                        authority=authority,
                                        chain=chain,
                                        reservations=tuple(reopened_reservations),
                                    )
                                )
                                check_deadline()
                            finally:
                                os.close(authority_fd)
                    finally:
                        os.close(uid_fd)
            finally:
                os.close(users_fd)
        finally:
            os.close(v1_fd)
        return ContainmentLedgerInventory(
            entries=tuple(entries),
            record_count=record_count,
        )

    @staticmethod
    def _validate_inventory_reservation(
        reservation: ContainmentRequestReservationV1,
        request: BrokerRequestV1,
        *,
        attempt: BrokerAttemptRecordV1 | None,
        attachment: BrokerAttachmentRecordV1 | None,
        release_intent: BrokerReleaseIntentRecordV1 | None,
        stop_intent: BrokerStopIntentRecordV1 | None,
        drain: BrokerDrainRecordV1 | None,
    ) -> None:
        """Bind one parsed reservation to the exact durable authority chain.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        if request.operation is not reservation.operation:
            raise ContainmentLedgerSecurityError(
                "ledger inventory reservation operation is inconsistent"
            )
        if attempt is None:
            if (
                request.operation is not BrokerOperation.ATTACH
                or type(request.payload) is not AttachPayloadV1
                or reservation.peer_pid != request.payload.gate_ready.expected_parent_pid
                or reservation.peer_start_token
                != request.payload.gate_ready.expected_parent_start_token
            ):
                raise ContainmentLedgerSecurityError(
                    "reservation-only attach owner differs from gate-ready parent"
                )
            return

        request_authority = getattr(request.payload, "authority", None)
        if (
            reservation.authenticated_uid != attempt.peer_uid
            or reservation.primary_gid != attempt.peer_gid
            or reservation.broker_build_id != attempt.broker_build_id
            or reservation.broker_config_sha256 != attempt.broker_config_sha256
            or (request_authority is not None and request_authority != attempt.authority)
        ):
            raise ContainmentLedgerSecurityError(
                "ledger inventory reservation differs from attempt authority"
            )

        application_operations = (
            BrokerOperation.ATTACH,
            BrokerOperation.RELEASE,
            BrokerOperation.AWAIT_DRAIN,
            BrokerOperation.STOP,
        )
        if request.operation in application_operations and (
            reservation.peer_pid != attempt.peer_pid
            or reservation.peer_start_token != attempt.peer_pid_start_token
        ):
            raise ContainmentLedgerSecurityError(
                "ledger inventory application reservation owner differs from attempt"
            )

        if request.operation is BrokerOperation.ATTACH:
            if (
                type(request.payload) is not AttachPayloadV1
                or request.payload.gate_ready != attempt.gate_ready
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory attach request differs from attempt"
                )
        elif request.operation is BrokerOperation.RELEASE:
            if attachment is None or type(request.payload) is not ReleasePayloadV1:
                raise ContainmentLedgerSecurityError(
                    "ledger inventory release reservation has no attachment"
                )
            attachment_text = attachment.canonical_json_utf8()
            if (
                request.payload.attachment_receipt_json_utf8 != attachment_text
                or request.payload.attachment_receipt_sha256
                != hashlib.sha256(attachment_text.encode("utf-8")).hexdigest()
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory release request differs from attachment"
                )
            if (
                release_intent is not None
                and request.payload.application_launch_sha256
                != release_intent.application_launch_sha256
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory release request differs from release intent"
                )
        elif request.operation is BrokerOperation.GATE_AUTHORIZE_EXEC:
            if (
                attachment is None
                or type(request.payload) is not GateAuthorizeExecPayloadV1
                or reservation.peer_pid != attempt.gate_pid
                or reservation.peer_start_token != attempt.gate_start_token
                or request.payload.launch_nonce_sha256 != attempt.authority.launch_nonce_sha256
                or request.payload.gate_material_sha256 != attempt.gate_ready.gate_material_sha256
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory gate reservation differs from attempt"
                )
        elif request.operation is BrokerOperation.AWAIT_DRAIN:
            if attachment is None or type(request.payload) is not AwaitDrainPayloadV1:
                raise ContainmentLedgerSecurityError(
                    "ledger inventory await reservation has no attachment"
                )
        elif request.operation is BrokerOperation.STOP:
            if attachment is None or type(request.payload) is not StopPayloadV1:
                raise ContainmentLedgerSecurityError(
                    "ledger inventory stop reservation has no attachment"
                )
        elif request.operation is BrokerOperation.RECOVER_STOP:
            if (
                attachment is None
                or type(request.payload) is not RecoverStopPayloadV1
                or request.payload.original_owner_pid != attachment.peer_pid
                or request.payload.original_owner_start_token != attachment.peer_pid_start_token
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory recovery request differs from attachment owner"
                )
        else:
            raise ContainmentLedgerSecurityError(
                "ledger inventory reservation operation is incompatible"
            )

        requested_termination_reason: str | None = None
        if type(request.payload) in (AwaitDrainPayloadV1, StopPayloadV1):
            requested_termination_reason = request.payload.termination_reason
        elif type(request.payload) is RecoverStopPayloadV1:
            requested_termination_reason = "recovery_stop"
        if requested_termination_reason is not None:
            if (
                stop_intent is not None
                and requested_termination_reason != stop_intent.termination_reason
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory terminal request differs from stop intent"
                )
            if (
                drain is not None
                and drain.termination_reason != "reboot"
                and requested_termination_reason != drain.termination_reason
            ):
                raise ContainmentLedgerSecurityError(
                    "ledger inventory terminal request differs from drain"
                )

    @staticmethod
    def _inventory_names(
        directory_fd: int,
        *,
        check_deadline: Callable[[], None],
        consume_structural_entry: Callable[[], None],
        scandir: Callable[[int], object],
    ) -> tuple[str, ...]:
        names: list[str] = []
        try:
            with scandir(directory_fd) as iterator:  # type: ignore[attr-defined]
                for entry in iterator:
                    check_deadline()
                    consume_structural_entry()
                    name = entry.name
                    if type(name) is not str or not name:
                        raise ContainmentLedgerSecurityError(
                            "ledger inventory contains a malformed directory entry"
                        )
                    names.append(name)
                    check_deadline()
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                "ledger inventory directory enumeration failed"
            ) from exc
        return tuple(sorted(names))

    def _inventory_open_directory(self, parent_fd: int, component: str) -> int:
        try:
            return self._open_and_validate_directory(parent_fd, component)
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                "ledger inventory component is not a safe no-follow directory"
            ) from exc

    def _validate_inventory_leaves(
        self,
        authority_fd: int,
        leaf_names: tuple[str, ...],
        *,
        check_deadline: Callable[[], None],
    ) -> tuple[str, ...]:
        family_leaves = frozenset(family.leaf for family in _FAMILIES)
        reservation_leaves = frozenset(family.leaf for family in _RESERVATION_FAMILIES)
        durable: list[str] = []
        for leaf in leaf_names:
            check_deadline()
            if leaf in family_leaves or leaf in reservation_leaves:
                durable.append(leaf)
                continue
            if leaf == ".transition.lock":
                self._validate_inventory_nondurable_leaf(authority_fd, leaf)
                continue
            if any(
                re.fullmatch(
                    rf"\.{re.escape(family.leaf)}\.[0-9a-f]{{32}}\.tmp",
                    leaf,
                )
                is not None
                for family in (*_FAMILIES, *_RESERVATION_FAMILIES)
            ):
                self._validate_inventory_nondurable_leaf(authority_fd, leaf)
                continue
            raise ContainmentLedgerSecurityError(
                "ledger authority contains an unknown inventory leaf"
            )
        return tuple(durable)

    def _live_authority_leaves(self, authority_fd: int) -> tuple[str, ...]:
        """Enumerate the closed authority leaf set under its held transaction."""

        names: list[str] = []
        try:
            with os.scandir(authority_fd) as iterator:
                for entry in iterator:
                    if len(names) >= 256:
                        raise ContainmentLedgerSecurityError("ledger authority leaf bound exceeded")
                    if type(entry.name) is not str or not entry.name:
                        raise ContainmentLedgerSecurityError(
                            "ledger authority contains a malformed leaf"
                        )
                    names.append(entry.name)
        except OSError as exc:
            raise ContainmentLedgerSecurityError("ledger authority enumeration failed") from exc
        return self._validate_inventory_leaves(
            authority_fd,
            tuple(sorted(names)),
            check_deadline=lambda: None,
        )

    @contextmanager
    def _inventory_authority_transaction(
        self,
        directory_fd: int,
        *,
        check_deadline: Callable[[], None],
        flock: Callable[[int, int], object],
    ):
        try:
            lock_fd = os.open(
                ".transition.lock",
                _LOCK_FLAGS,
                _RECORD_MODE,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                "ledger inventory authority lock is unsafe"
            ) from exc
        locked = False
        try:
            self._validate_record_stat(
                self._fstat(lock_fd),
                label="ledger inventory authority lock",
            )
            while not locked:
                check_deadline()
                try:
                    flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    check_deadline()
            check_deadline()
            yield
        finally:
            if locked:
                try:
                    flock(lock_fd, fcntl.LOCK_UN)
                except OSError as exc:
                    raise ContainmentLedgerSecurityError(
                        "ledger inventory authority unlock failed"
                    ) from exc
            os.close(lock_fd)

    def _validate_inventory_nondurable_leaf(
        self,
        authority_fd: int,
        leaf: str,
    ) -> None:
        try:
            descriptor = os.open(leaf, _READ_FLAGS, dir_fd=authority_fd)
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                "ledger inventory nondurable leaf is unsafe"
            ) from exc
        try:
            self._validate_record_stat(self._fstat(descriptor), label=leaf)
        finally:
            os.close(descriptor)

    def _publish_bytes(
        self,
        directory_fd: int,
        family: _Family,
        intended: bytes,
    ) -> PersistedContainmentRecord:
        if len(intended) > _MAXIMUM_RECORD_BYTES:
            raise ContainmentLedgerSecurityError("record exceeds maximum byte length")
        temp_leaf = f".{family.leaf}.{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temp_leaf,
                _WRITE_FLAGS,
                _RECORD_MODE,
                dir_fd=directory_fd,
            )
            self._validate_record_stat(self._fstat(descriptor), label="temporary record")
            view = memoryview(intended)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ContainmentLedgerSecurityError("record write made no progress")
                view = view[written:]
            self._fdatasync(descriptor)
            self._crash_hook("content_synced")
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        try:
            self._rename_noreplace(
                temp_leaf,
                family.leaf,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except FileExistsError:
            os.unlink(temp_leaf, dir_fd=directory_fd)
            self._fsync(directory_fd)
            raise ContainmentLedgerConflictError(
                f"immutable {family.leaf} appeared during serialized publication"
            )
        except OSError as exc:
            raise ContainmentLedgerSecurityError("exclusive record publication failed") from exc

        self._crash_hook("published")
        self._fsync(directory_fd)
        self._crash_hook("directory_synced")
        try:
            reopened = self._read_record(directory_fd, family)
        except (ContainmentLedgerSecurityError, ValueError, TypeError) as exc:
            raise ContainmentLedgerSecurityError(
                "record reopen validation failed after publication"
            ) from exc
        if reopened is None or reopened.canonical_bytes != intended:
            raise ContainmentLedgerSecurityError(
                "record reopen did not match exact published canonical bytes"
            )
        return reopened

    def _read_all(
        self,
        directory_fd: int,
    ) -> tuple[PersistedContainmentRecord | None, ...]:
        return tuple(self._read_record(directory_fd, family) for family in _FAMILIES)

    def _publish_reservation(
        self,
        directory_fd: int,
        family: _ReservationFamily,
        intended: bytes,
    ) -> PersistedContainmentRequestReservation:
        if len(intended) > _MAXIMUM_RECORD_BYTES:
            raise ContainmentLedgerSecurityError("reservation exceeds maximum byte length")
        temp_leaf = f".{family.leaf}.{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(temp_leaf, _WRITE_FLAGS, _RECORD_MODE, dir_fd=directory_fd)
            self._validate_record_stat(self._fstat(descriptor), label="temporary reservation")
            view = memoryview(intended)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ContainmentLedgerSecurityError("reservation write made no progress")
                view = view[written:]
            self._fdatasync(descriptor)
            self._crash_hook("reservation_content_synced")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            self._rename_noreplace(
                temp_leaf, family.leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
        except FileExistsError:
            os.unlink(temp_leaf, dir_fd=directory_fd)
            self._fsync(directory_fd)
            raise ContainmentLedgerConflictError(
                f"immutable {family.slot.value} request reservation appeared during publication"
            )
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                "exclusive reservation publication failed"
            ) from exc
        self._crash_hook("reservation_published")
        self._fsync(directory_fd)
        self._crash_hook("reservation_directory_synced")
        reopened = self._read_reservation(directory_fd, family)
        if reopened is None or reopened.canonical_bytes != intended:
            raise ContainmentLedgerSecurityError("reservation did not reopen as exact bytes")
        return reopened

    def _read_reservation(
        self,
        directory_fd: int,
        family: _ReservationFamily,
    ) -> PersistedContainmentRequestReservation | None:
        try:
            descriptor = os.open(family.leaf, _READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                f"{family.leaf} is not a safe no-follow reservation"
            ) from exc
        try:
            self._validate_record_stat(self._fstat(descriptor), label=family.leaf)
            raw = self._read_bounded(descriptor)
        finally:
            os.close(descriptor)
        try:
            decoded = loads_canonical_packet(raw)
            _reject_incompatible_schema(
                decoded,
                expected_name="mif-containment-broker-request-reservation",
                leaf=family.leaf,
            )
            reservation = ContainmentRequestReservationV1._from_mapping(decoded)
        except ContainmentLedgerIncompatibleError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContainmentLedgerSecurityError(
                f"{family.leaf} contains malformed reservation bytes"
            ) from exc
        if reservation.slot is not family.slot or reservation.canonical_bytes() != raw:
            raise ContainmentLedgerSecurityError("reservation slot or canonical bytes mismatch")
        return PersistedContainmentRequestReservation(reservation, raw)

    def _read_record(
        self,
        directory_fd: int,
        family: _Family,
    ) -> PersistedContainmentRecord | None:
        """Read record into its typed representation.

        The decoded fields are checked through the record constructors and the applicable
        context comparisons before returning the result. The validated representation is
        returned for the subsequent operation."""

        try:
            descriptor = os.open(family.leaf, _READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                f"{family.leaf} is not a safe no-follow record"
            ) from exc
        try:
            self._validate_record_stat(self._fstat(descriptor), label=family.leaf)
            raw = self._read_bounded(descriptor)
        finally:
            os.close(descriptor)
        try:
            decoded = loads_canonical_packet(raw)
            _reject_incompatible_schema(
                decoded,
                expected_name=family.record_type.SCHEMA_NAME,
                leaf=family.leaf,
            )
            parsed = family.record_type.from_mapping(decoded)
        except ContainmentLedgerIncompatibleError:
            raise
        except (ValueError, TypeError) as exc:
            raise ContainmentLedgerSecurityError(
                f"{family.leaf} contains malformed or noncanonical bytes"
            ) from exc
        if parsed.canonical_json_utf8().encode("utf-8") != raw:
            raise ContainmentLedgerSecurityError(
                f"{family.leaf} did not reopen as its exact canonical model"
            )
        return PersistedContainmentRecord(record=parsed, canonical_bytes=raw)

    def _read_bounded(self, descriptor: int) -> bytes:
        chunks: list[bytes] = []
        remaining = _MAXIMUM_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16_384))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw:
            raise ContainmentLedgerSecurityError("ledger record is empty")
        if len(raw) > _MAXIMUM_RECORD_BYTES:
            raise ContainmentLedgerSecurityError("ledger record exceeds maximum byte length")
        return raw

    def _open_authority_directory(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
        *,
        create: bool,
    ) -> int:
        self._require_open_root()
        descriptor = os.dup(self._root_fd)
        os.set_inheritable(descriptor, False)
        try:
            for component in (
                "v1",
                "users",
                str(authenticated_uid),
                authority.launch_nonce_sha256,
            ):
                next_descriptor = self._open_directory_component(
                    descriptor,
                    component,
                    create=create,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_directory_component(
        self,
        parent_fd: int,
        component: str,
        *,
        create: bool,
    ) -> int:
        try:
            existing = self._open_and_validate_directory(parent_fd, component)
        except FileNotFoundError:
            if not create:
                raise _AuthorityDirectoryMissing from None
            try:
                os.mkdir(component, _DIRECTORY_MODE, dir_fd=parent_fd)
                self._fsync(parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ContainmentLedgerSecurityError(
                    f"cannot securely create ledger directory component {component!r}"
                ) from exc
            try:
                return self._open_and_validate_directory(parent_fd, component)
            except OSError as exc:
                raise ContainmentLedgerSecurityError(
                    f"ledger directory component {component!r} is unsafe"
                ) from exc
        except OSError as exc:
            raise ContainmentLedgerSecurityError(
                f"ledger directory component {component!r} is unsafe"
            ) from exc
        try:
            self._fsync(parent_fd)
        except BaseException:
            os.close(existing)
            raise
        return existing

    def _open_and_validate_directory(self, parent_fd: int, component: str) -> int:
        descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            self._validate_private_directory(
                self._fstat(descriptor),
                label=f"ledger directory component {component!r}",
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _require_open_root(self) -> None:
        if self._root_fd < 0:
            raise ContainmentLedgerSecurityError("ledger root descriptor is closed")
        root_stat = self._fstat(self._root_fd)
        self._validate_root(root_stat)
        if (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
            raise ContainmentLedgerSecurityError("retained ledger root identity changed")

    @staticmethod
    def _validate_request_identity(
        authenticated_uid: int,
        authority: AuthorityBindingV1,
    ) -> None:
        if type(authenticated_uid) is not int or authenticated_uid < 0:
            raise ValueError("authenticated_uid must be a non-negative integer")
        if not isinstance(authority, AuthorityBindingV1):
            raise TypeError("authority must be AuthorityBindingV1")

    @staticmethod
    def _validate_root(root_stat: object) -> None:
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ContainmentLedgerSecurityError("ledger root is not a directory")
        if root_stat.st_uid != _ROOT_UID:
            raise ContainmentLedgerSecurityError("ledger root owner must be root")
        if stat.S_IMODE(root_stat.st_mode) & 0o022:
            raise ContainmentLedgerSecurityError("ledger root must not be group/world writable")

    @staticmethod
    def _validate_private_directory(directory_stat: object, *, label: str) -> None:
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ContainmentLedgerSecurityError(f"{label} is not a directory")
        if directory_stat.st_uid != _ROOT_UID:
            raise ContainmentLedgerSecurityError(f"{label} owner must be root")
        if stat.S_IMODE(directory_stat.st_mode) != _DIRECTORY_MODE:
            raise ContainmentLedgerSecurityError(f"{label} mode must be 0700")

    @staticmethod
    def _validate_record_stat(record_stat: object, *, label: str) -> None:
        if not stat.S_ISREG(record_stat.st_mode):
            raise ContainmentLedgerSecurityError(f"{label} is not a regular file")
        if record_stat.st_uid != _ROOT_UID:
            raise ContainmentLedgerSecurityError(f"{label} owner must be root")
        if stat.S_IMODE(record_stat.st_mode) != _RECORD_MODE:
            raise ContainmentLedgerSecurityError(f"{label} mode must be 0600")
        if record_stat.st_nlink != 1:
            raise ContainmentLedgerSecurityError(f"{label} has an unexpected hard link")

    def _classify(
        self,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
        entries: tuple[PersistedContainmentRecord | None, ...],
        *,
        observation: ContainmentReconciliationObservation | None,
    ) -> ContainmentLedgerChain:
        (
            attempt_entry,
            attachment_entry,
            intent_entry,
            release_entry,
            stop_intent_entry,
            drain_entry,
        ) = entries
        kwargs = {
            "attempt": attempt_entry,
            "attachment": attachment_entry,
            "release_intent": intent_entry,
            "release": release_entry,
            "stop_intent": stop_intent_entry,
            "drain": drain_entry,
        }
        if attempt_entry is None:
            return self._ambiguous("attempt predecessor is absent", **kwargs)
        attempt = attempt_entry.record
        if not isinstance(attempt, BrokerAttemptRecordV1):
            return self._ambiguous("attempt family type is wrong", **kwargs)
        reason = self._validate_attempt(attempt, authenticated_uid, authority)
        if reason is not None:
            return self._ambiguous(reason, **kwargs)

        if attachment_entry is None:
            if any(
                item is not None
                for item in (
                    intent_entry,
                    release_entry,
                    stop_intent_entry,
                    drain_entry,
                )
            ):
                return self._ambiguous("attachment predecessor is absent", **kwargs)
            if observation is not None:
                return self._ambiguous(
                    "host observation cannot refine an unattached attempt",
                    **kwargs,
                )
            return ContainmentLedgerChain(
                state=ContainmentLedgerState.ATTEMPT_ONLY,
                **kwargs,
            )

        attachment = attachment_entry.record
        if not isinstance(attachment, BrokerAttachmentRecordV1):
            return self._ambiguous("attachment family type is wrong", **kwargs)
        reason = self._validate_successor(attempt, attachment)
        if reason is None and attachment.attempt_sha256 != attempt.record_sha256:
            reason = "attachment digest does not match attempt predecessor"
        if reason is not None:
            return self._ambiguous(reason, **kwargs)

        intent: BrokerReleaseIntentRecordV1 | None = None
        if intent_entry is not None:
            if not isinstance(intent_entry.record, BrokerReleaseIntentRecordV1):
                return self._ambiguous("release-intent family type is wrong", **kwargs)
            intent = intent_entry.record
            reason = self._validate_successor(attachment, intent)
            if reason is None and intent.attachment_sha256 != attachment.record_sha256:
                reason = "release-intent digest does not match attachment predecessor"
            if reason is not None:
                return self._ambiguous(reason, **kwargs)

        release: BrokerReleaseRecordV1 | None = None
        if release_entry is not None:
            if intent is None:
                return self._ambiguous("release-intent predecessor is absent", **kwargs)
            if not isinstance(release_entry.record, BrokerReleaseRecordV1):
                return self._ambiguous("release family type is wrong", **kwargs)
            release = release_entry.record
            reason = self._validate_successor(attachment, release)
            if reason is None and release.attachment_sha256 != attachment.record_sha256:
                reason = "release attachment digest does not match predecessor"
            if reason is None and release.release_intent_sha256 != intent.record_sha256:
                reason = "release digest does not match release-intent predecessor"
            if reason is not None:
                return self._ambiguous(reason, **kwargs)

        stop_intent: BrokerStopIntentRecordV1 | None = None
        if stop_intent_entry is not None:
            if not isinstance(stop_intent_entry.record, BrokerStopIntentRecordV1):
                return self._ambiguous("stop-intent family type is wrong", **kwargs)
            stop_intent = stop_intent_entry.record
            reason = self._validate_successor(attachment, stop_intent)
            if reason is None and stop_intent.attachment_sha256 != attachment.record_sha256:
                reason = "stop-intent attachment digest does not match predecessor"
            if release is not None:
                if stop_intent.release_intent_sha256 != intent.record_sha256:
                    reason = "stop-intent release-intent digest does not match predecessor"
                elif stop_intent.release_sha256 != release.record_sha256:
                    reason = "stop-intent release digest does not match predecessor"
            elif intent is not None:
                if stop_intent.release_intent_sha256 != intent.record_sha256:
                    reason = "stop-intent release-intent digest does not match predecessor"
                elif stop_intent.release_sha256 is not None:
                    reason = "stop-intent release predecessor is absent"
            elif (
                stop_intent.release_intent_sha256 is not None
                or stop_intent.release_sha256 is not None
            ):
                reason = "stop-intent release predecessors are absent"
            if (
                reason is None
                and stop_intent.termination_reason in ("normal_exit", "nonzero_exit")
                and (intent is None or release is None)
            ):
                reason = "target-exit stop intent has no complete release predecessor"
            if reason is not None:
                return self._ambiguous(reason, **kwargs)

        if drain_entry is not None:
            if not isinstance(drain_entry.record, BrokerDrainRecordV1):
                return self._ambiguous("drain family type is wrong", **kwargs)
            drain = drain_entry.record
            reason = self._validate_successor(attachment, drain)
            if reason is None and drain.attachment_sha256 != attachment.record_sha256:
                reason = "drain attachment digest does not match predecessor"
            if release is None:
                if drain.release_sha256 is not None:
                    reason = "drain release predecessor is absent"
            elif drain.release_sha256 != release.record_sha256:
                reason = "drain digest does not match release predecessor"
            if (
                reason is None
                and drain.termination_reason in ("normal_exit", "nonzero_exit")
                and release is None
            ):
                reason = "target-exit drain has no release predecessor"
            if isinstance(
                drain.proof,
                (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1),
            ):
                if stop_intent is None:
                    reason = "same-boot drain stop-intent predecessor is absent"
                elif drain.stop_intent_sha256 != stop_intent.record_sha256:
                    reason = "drain digest does not match stop-intent predecessor"
                elif drain.termination_reason != stop_intent.termination_reason:
                    reason = "drain termination reason differs from stop intent"
            elif stop_intent is not None:
                reason = "machine-reboot drain cannot follow a same-boot stop intent"
            if reason is not None:
                return self._ambiguous(reason, **kwargs)
            return ContainmentLedgerChain(
                state=ContainmentLedgerState.DRAINED,
                **kwargs,
            )

        observed_state = self._classify_observation(
            attachment,
            stop_intent,
            observation,
        )
        if observed_state is ContainmentLedgerState.AMBIGUOUS:
            return self._ambiguous(
                "host observation does not match ledger identity",
                **kwargs,
            )
        if observed_state is not None:
            return ContainmentLedgerChain(state=observed_state, **kwargs)
        if intent is not None or release is not None or stop_intent is not None:
            return ContainmentLedgerChain(
                state=ContainmentLedgerState.RELEASED_LIVE,
                **kwargs,
            )
        return ContainmentLedgerChain(
            state=ContainmentLedgerState.ATTACHED_STOPPED,
            **kwargs,
        )

    @staticmethod
    def _validate_attempt(
        attempt: BrokerAttemptRecordV1,
        authenticated_uid: int,
        authority: AuthorityBindingV1,
    ) -> str | None:
        if attempt.peer_uid != authenticated_uid or attempt.authority != authority:
            return "attempt UID or authority binding does not match ledger authority"
        gate = attempt.gate_ready
        expected = (
            (gate.launch_nonce_sha256, authority.launch_nonce_sha256),
            (gate.gate_pid, attempt.gate_pid),
            (gate.gate_start_token, attempt.gate_start_token),
            (gate.expected_parent_pid, attempt.peer_pid),
            (gate.expected_parent_start_token, attempt.peer_pid_start_token),
            (gate.target_executable_sha256, attempt.target_executable_sha256),
        )
        if any(left != right for left, right in expected):
            return "attempt gate material does not match root authority fields"
        return None

    @staticmethod
    def _validate_successor(
        predecessor: BrokerAttemptRecordV1 | BrokerAttachmentRecordV1,
        successor: (
            BrokerAttachmentRecordV1
            | BrokerReleaseIntentRecordV1
            | BrokerReleaseRecordV1
            | BrokerStopIntentRecordV1
            | BrokerDrainRecordV1
        ),
    ) -> str | None:
        identity_fields = (
            "protocol_version",
            "broker_build_id",
            "broker_config_sha256",
            "boot_id",
            "peer_uid",
            "peer_gid",
            "peer_pid",
            "peer_pid_start_token",
            "authority",
            "executable_registration_id",
            "target_executable_sha256",
            "gate_pid",
            "gate_start_token",
            "anchor_identity",
            "scope_policy_sha256",
        )
        for field_name in identity_fields:
            if getattr(predecessor, field_name) != getattr(successor, field_name):
                return f"successor {field_name} binding differs from predecessor"
        if isinstance(predecessor, BrokerAttachmentRecordV1):
            for field_name in (
                "unit_name",
                "invocation_id",
                "control_group",
                "cgroup_identity",
            ):
                if getattr(predecessor, field_name) != getattr(successor, field_name):
                    return f"successor {field_name} binding differs from attachment"
        return None

    @staticmethod
    def _classify_observation(
        attachment: BrokerAttachmentRecordV1,
        stop_intent: BrokerStopIntentRecordV1 | None,
        observation: ContainmentReconciliationObservation | None,
    ) -> ContainmentLedgerState | None:
        if observation is None:
            return None
        observed_scope = (
            observation.unit_name,
            observation.invocation_id,
            observation.control_group,
        )
        expected_scope = (
            attachment.unit_name,
            attachment.invocation_id,
            attachment.control_group,
        )
        if any(value is not None for value in observed_scope) and (
            observed_scope != expected_scope
        ):
            return ContainmentLedgerState.AMBIGUOUS
        if observation.current_boot_id != attachment.boot_id:
            if observation.drain_proof is not None:
                return ContainmentLedgerState.AMBIGUOUS
            return ContainmentLedgerState.PRIOR_BOOT_CANDIDATE
        if observation.drain_proof is None:
            if stop_intent is None:
                return ContainmentLedgerState.AMBIGUOUS
            return None
        if stop_intent is None:
            return ContainmentLedgerState.AMBIGUOUS
        if observation.drain_proof.cgroup_identity != attachment.cgroup_identity:
            return ContainmentLedgerState.AMBIGUOUS
        return ContainmentLedgerState.EMPTY_WITHOUT_DRAIN

    @staticmethod
    def _ambiguous(
        reason: str,
        *,
        attempt: PersistedContainmentRecord | None = None,
        attachment: PersistedContainmentRecord | None = None,
        release_intent: PersistedContainmentRecord | None = None,
        release: PersistedContainmentRecord | None = None,
        stop_intent: PersistedContainmentRecord | None = None,
        drain: PersistedContainmentRecord | None = None,
    ) -> ContainmentLedgerChain:
        return ContainmentLedgerChain(
            state=ContainmentLedgerState.AMBIGUOUS,
            attempt=attempt,
            attachment=attachment,
            release_intent=release_intent,
            release=release,
            stop_intent=stop_intent,
            drain=drain,
            ambiguity_reason=reason,
        )


__all__ = [
    "ContainmentLedger",
    "ContainmentLedgerChain",
    "ContainmentLedgerConflictError",
    "ContainmentLedgerError",
    "ContainmentLedgerIncompatibleError",
    "ContainmentLedgerOrderError",
    "ContainmentLedgerSecurityError",
    "ContainmentLedgerState",
    "ContainmentLedgerInventory",
    "ContainmentLedgerInventoryEntry",
    "ContainmentReconciliationObservation",
    "PersistedContainmentRecord",
]
