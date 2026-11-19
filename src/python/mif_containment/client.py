"""Dependency-neutral unprivileged client for the Linux containment broker.

The `ContainmentCompatibilityExpectationV1` record carries `broker_build_id`,
`broker_config_sha256`, `scope_policy_sha256`, `registration_id`,
`canonical_executable_path` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import array
import hashlib
import math
import os
import platform
import posixpath
import re
import secrets
import stat
import socket
import struct
import sys
import time
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass

from .codec import loads_canonical_packet
from .protocol import (
    BrokerCapabilitiesV1,
    BrokerOperation,
    BrokerRequestV1,
    CapabilitiesPayloadV1,
    AttachPayloadV1,
    GateAuthorizeExecPayloadV1,
    GateExecAuthorizationV1,
    LinuxSystemdScopeAttachmentV1,
    ReleasePayloadV1,
    ReleaseReceiptV1,
    InspectPayloadV1,
    ContainmentInspection,
    AwaitDrainPayloadV1,
    StopPayloadV1,
    RecoverStopPayloadV1,
    LinuxSystemdScopeDrainV1,
    AuthorityBindingV1,
    BrokerAttachmentRecordV1,
    BrokerDrainRecordV1,
    BrokerExecutableRegistrationV1,
)

BROKER_SOCKET_PATH = "/run/mif-pipeline/containment.sock"
MAXIMUM_PACKET_BYTES = 65_536
_MAXIMUM_EXECUTABLE_BYTES = 268_435_456
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

AttachRequestV1 = AttachPayloadV1


# ==============================================================================
#                                     Client
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ContainmentCompatibilityExpectationV1:
    """Immutable application-side compatibility and registration expectation."""

    broker_build_id: str
    broker_config_sha256: str
    scope_policy_sha256: str
    registration_id: str
    canonical_executable_path: str
    target_executable_sha256: str
    command_profile: str
    allowed_environment_names: tuple[str, ...]
    requested_environment_names: frozenset[str]

    def __post_init__(self) -> None:
        """Validate the ContainmentCompatibilityExpectationV1 fields when the record is
        constructed.

        The checks cover ``command_profile``, ``broker_build_id``, ``registration_id``,
        ``canonical_executable_path``, ``allowed_environment_names``,
        ``requested_environment_names``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if (
            type(self.broker_build_id) is not str
            or not self.broker_build_id
            or len(self.broker_build_id.encode("utf-8")) > 128
        ):
            raise ValueError("broker_build_id must be non-empty bounded text")
        for name in (
            "broker_config_sha256",
            "scope_policy_sha256",
            "target_executable_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if (
            type(self.registration_id) is not str
            or not self.registration_id
            or len(self.registration_id.encode("utf-8")) > 128
        ):
            raise ValueError("registration_id must be non-empty bounded text")
        if (
            type(self.canonical_executable_path) is not str
            or not self.canonical_executable_path.startswith("/")
            or self.canonical_executable_path.startswith("//")
            or posixpath.normpath(self.canonical_executable_path) != self.canonical_executable_path
        ):
            raise ValueError("canonical_executable_path must be canonical and absolute")
        if self.command_profile not in ("headless-script", "interactive-project"):
            raise ValueError("command_profile is outside the closed set")
        if (
            type(self.allowed_environment_names) is not tuple
            or len(self.allowed_environment_names) != len(set(self.allowed_environment_names))
            or any(type(name) is not str for name in self.allowed_environment_names)
        ):
            raise ValueError("allowed_environment_names must be an exact tuple")
        if type(self.requested_environment_names) is not frozenset or any(
            type(name) is not str for name in self.requested_environment_names
        ):
            raise ValueError("requested_environment_names must be a frozenset")


class ContainmentClientError(RuntimeError):
    """Base class for bounded fail-closed client failures."""


class ContainmentUnavailableError(ContainmentClientError):
    """The fixed local broker endpoint was unavailable before a request."""


class _RetryableUnavailableError(ContainmentUnavailableError):
    """A transient transport failure on the read-only capabilities operation."""


class ContainmentIncompatibleError(ContainmentClientError):
    """The installed broker and this client have incompatible contracts."""


class ContainmentRejectedError(ContainmentClientError):
    """The authenticated broker rejected this closed request."""


class ContainmentConflictError(ContainmentClientError):
    """The request conflicts with an occupied immutable transition slot."""


class ContainmentAmbiguousError(ContainmentClientError):
    """The broker cannot safely classify the current containment authority."""


class ContainmentIndeterminateError(ContainmentClientError):
    """A mutation may have been accepted but no fenced result was received."""


class ContainmentBrokerClient:
    """Unprivileged one-connection-per-operation containment client."""

    def __init__(
        self,
        *,
        _operations: object | None = None,
        _request_id_factory: Callable[[], str] | None = None,
        connect_timeout_seconds: float = 2.0,
        packet_timeout_seconds: float = 5.0,
        operation_timeout_seconds: float = 60.0,
    ) -> None:
        for label, value in (
            ("connect timeout", connect_timeout_seconds),
            ("packet timeout", packet_timeout_seconds),
            ("operation timeout", operation_timeout_seconds),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
                or value > 3_600.0
            ):
                raise ValueError(f"{label} must be positive and bounded")
        self._operations = _PosixClientOperations() if _operations is None else _operations
        self._request_id_factory = (
            (lambda: secrets.token_hex(16)) if _request_id_factory is None else _request_id_factory
        )
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._packet_timeout_seconds = float(packet_timeout_seconds)
        self._operation_timeout_seconds = float(operation_timeout_seconds)
        self._attachment_receipts: dict[str, str] = {}
        self._validated_expectation: ContainmentCompatibilityExpectationV1 | None = None
        self._validated_boot_id: str | None = None

    def capabilities(self) -> BrokerCapabilitiesV1:
        capabilities, _peer_pid = self._capabilities_with_peer()
        return capabilities

    def _capabilities_with_peer(
        self,
        *,
        operation_deadline: float | None = None,
    ) -> tuple[BrokerCapabilitiesV1, int]:
        for attempt in range(2):
            try:
                return self._read_capabilities_once(operation_deadline=operation_deadline)
            except _RetryableUnavailableError:
                if attempt:
                    raise
        raise AssertionError("unreachable capability retry state")

    def _read_capabilities_once(
        self,
        *,
        operation_deadline: float | None = None,
    ) -> tuple[BrokerCapabilitiesV1, int]:
        request = BrokerRequestV1(
            schema_name="mif-containment-broker-request",
            schema_version=1,
            operation=BrokerOperation.CAPABILITIES,
            request_id=self._request_id_factory(),
            payload=CapabilitiesPayloadV1(),
        )
        payload, peer_pid = self._exchange(
            request,
            operation_deadline=operation_deadline,
        )
        try:
            capabilities = BrokerCapabilitiesV1.from_mapping(payload)
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment broker capabilities are incompatible"
            ) from exc
        return capabilities, peer_pid

    def preflight(
        self,
        expectation: ContainmentCompatibilityExpectationV1,
    ) -> BrokerExecutableRegistrationV1:
        if type(expectation) is not ContainmentCompatibilityExpectationV1:
            raise TypeError("expectation must be a ContainmentCompatibilityExpectationV1")
        preflight_deadline = self._operations.monotonic() + self._operation_timeout_seconds
        self._validated_expectation = None
        self._validated_boot_id = None
        if self._operations.platform_name() != "Linux":
            raise ContainmentIncompatibleError("Linux containment is unavailable on this platform")
        try:
            local_boot_id = self._operations.read_boot_id()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ContainmentUnavailableError("current Linux boot identity is unavailable") from exc
        if type(local_boot_id) is not str or _BOOT_ID.fullmatch(local_boot_id) is None:
            raise ContainmentUnavailableError("current Linux boot identity is malformed")
        self._assert_preflight_deadline(preflight_deadline)
        capabilities, peer_pid = self._capabilities_with_peer(operation_deadline=preflight_deadline)
        summary = capabilities.startup_certification
        expected_bindings = {
            "broker_build_id": expectation.broker_build_id,
            "broker_config_sha256": expectation.broker_config_sha256,
            "scope_policy_sha256": expectation.scope_policy_sha256,
            "boot_id": local_boot_id,
        }
        if (
            any(
                getattr(capabilities, name) != expected or getattr(summary, name) != expected
                for name, expected in expected_bindings.items()
            )
            or summary.broker_process_id != peer_pid
        ):
            raise ContainmentIncompatibleError(
                "containment broker certification is stale or incompatible"
            )
        registrations = tuple(
            registration
            for registration in capabilities.security_profile.registrations
            if registration.registration_id == expectation.registration_id
        )
        if len(registrations) != 1:
            raise ContainmentIncompatibleError("containment executable registration is unavailable")
        registration = registrations[0]
        if (
            registration.canonical_path != expectation.canonical_executable_path
            or registration.target_executable_sha256 != expectation.target_executable_sha256
            or expectation.command_profile not in registration.allowed_command_profiles
            or registration.allowed_environment_names != expectation.allowed_environment_names
            or not expectation.requested_environment_names.issubset(
                registration.allowed_environment_names
            )
        ):
            raise ContainmentIncompatibleError(
                "containment executable registration is incompatible"
            )
        self._validate_registered_executable(
            registration,
            deadline=preflight_deadline,
        )
        self._validated_expectation = expectation
        self._validated_boot_id = local_boot_id
        return registration

    def _validate_registered_executable(
        self,
        registration: BrokerExecutableRegistrationV1,
        *,
        deadline: float,
    ) -> None:
        """Validate registered executable for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        descriptor: int | None = None
        try:
            self._assert_preflight_deadline(deadline)
            path_identity = self._operations.path_identity_nofollow(registration.canonical_path)
            self._assert_preflight_deadline(deadline)
            self._validate_executable_identity(path_identity)
            descriptor = self._operations.open_path_nofollow(registration.canonical_path)
            self._assert_preflight_deadline(deadline)
            opened_identity = self._operations.descriptor_identity(descriptor)
            self._assert_preflight_deadline(deadline)
            self._validate_executable_identity(opened_identity)
            if opened_identity != path_identity:
                raise ContainmentIncompatibleError(
                    "registered executable identity changed while opening"
                )
            digest = self._operations.descriptor_sha256(
                descriptor,
                deadline=deadline,
            )
            self._assert_preflight_deadline(deadline)
            if (
                digest != registration.target_executable_sha256
                or self._operations.descriptor_identity(descriptor) != opened_identity
                or self._operations.path_identity_nofollow(registration.canonical_path)
                != opened_identity
            ):
                raise ContainmentIncompatibleError(
                    "registered executable identity or digest changed"
                )
            self._assert_preflight_deadline(deadline)
        except ContainmentClientError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "registered executable is unavailable or unsafe"
            ) from exc
        finally:
            if descriptor is not None:
                cleanup_error: BaseException | None = None
                try:
                    self._operations.close_fd(descriptor)
                except BaseException as exc:
                    cleanup_error = exc
                if cleanup_error is not None:
                    primary_error = sys.exception()
                    if primary_error is None:
                        raise cleanup_error
                    primary_error.add_note(
                        "additional containment client cleanup failure: "
                        f"{type(cleanup_error).__name__}"
                    )

    def _assert_preflight_deadline(self, deadline: float) -> None:
        if self._operations.monotonic() >= deadline:
            raise ContainmentUnavailableError(
                "containment compatibility preflight deadline expired"
            )

    @staticmethod
    def _validate_executable_identity(identity: object) -> None:
        try:
            valid = (
                stat.S_ISREG(identity.mode)
                and stat.S_IMODE(identity.mode) & 0o111 != 0
                and identity.device >= 0
                and identity.inode >= 1
                and identity.nlink >= 1
                and 0 <= identity.size <= _MAXIMUM_EXECUTABLE_BYTES
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ContainmentIncompatibleError("registered executable identity is unsafe")

    def _assert_validated_root_bindings(self, record: object) -> None:
        expectation = self._validated_expectation
        if expectation is None:
            return
        if (
            record.broker_build_id != expectation.broker_build_id
            or record.broker_config_sha256 != expectation.broker_config_sha256
            or record.scope_policy_sha256 != expectation.scope_policy_sha256
            or record.boot_id != self._validated_boot_id
        ):
            raise ContainmentIncompatibleError(
                "containment root projection conflicts with preflight"
            )

    def attach(
        self,
        request: AttachRequestV1,
        *,
        target_executable_fd: int,
    ) -> LinuxSystemdScopeAttachmentV1:
        if type(request) is not AttachPayloadV1:
            raise TypeError("attach request must be an AttachPayloadV1")
        broker_request = BrokerRequestV1(
            schema_name="mif-containment-broker-request",
            schema_version=1,
            operation=BrokerOperation.ATTACH,
            request_id=self._request_id_factory(),
            payload=request,
        )
        payload, _peer_pid = self._exchange(
            broker_request,
            mutation=True,
            target_executable_fd=target_executable_fd,
        )
        try:
            attachment = LinuxSystemdScopeAttachmentV1.from_mapping(payload)
            attachment_record = BrokerAttachmentRecordV1.from_mapping(
                loads_canonical_packet(attachment.attachment_receipt_json_utf8.encode("utf-8"))
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment broker attachment is incompatible"
            ) from exc
        self._assert_validated_root_bindings(attachment_record)
        if (
            attachment_record.authority != request.authority
            or attachment_record.gate_pid != request.gate_ready.gate_pid
            or attachment_record.gate_start_token != request.gate_ready.gate_start_token
            or attachment_record.target_executable_sha256
            != request.gate_ready.target_executable_sha256
            or attachment.peer_uid != self._operations.current_uid()
            or attachment.peer_gid != self._operations.current_gid()
        ):
            raise ContainmentIncompatibleError("containment attachment does not match the request")
        self._attachment_receipts[attachment.attachment_receipt_sha256] = (
            attachment.attachment_receipt_json_utf8
        )
        return attachment

    def release(
        self,
        authority: AuthorityBindingV1,
        *,
        attachment_receipt_sha256: str,
        application_process_launch_sha256: str,
    ) -> ReleaseReceiptV1:
        try:
            attachment_text = self._attachment_receipts[attachment_receipt_sha256]
        except KeyError as exc:
            raise ContainmentRejectedError(
                "attachment receipt copy is unavailable for release"
            ) from exc
        payload = ReleasePayloadV1(
            authority=authority,
            attachment_receipt_json_utf8=attachment_text,
            attachment_receipt_sha256=attachment_receipt_sha256,
            application_launch_sha256=application_process_launch_sha256,
        )
        response = self._perform(
            BrokerOperation.RELEASE,
            payload,
            mutation=True,
        )
        try:
            receipt = ReleaseReceiptV1.from_mapping(response)
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment release receipt is incompatible"
            ) from exc
        if (
            receipt.launch_nonce_sha256 != authority.launch_nonce_sha256
            or receipt.attachment_receipt_sha256 != attachment_receipt_sha256
            or receipt.application_request_sha256 != authority.application_request_sha256
        ):
            raise ContainmentIncompatibleError(
                "containment release receipt does not match the request"
            )
        return receipt

    def inspect(
        self,
        authority: AuthorityBindingV1,
        *,
        attachment_receipt_sha256: str,
    ) -> ContainmentInspection:
        response = self._perform(
            BrokerOperation.INSPECT,
            InspectPayloadV1(authority=authority),
        )
        try:
            inspection = ContainmentInspection.from_mapping(response)
            if inspection.attachment is None:
                raise ValueError("inspection omitted attachment evidence")
            attachment_record = BrokerAttachmentRecordV1.from_mapping(
                loads_canonical_packet(
                    inspection.attachment.attachment_receipt_json_utf8.encode("utf-8")
                )
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError("containment inspection is incompatible") from exc
        self._assert_validated_root_bindings(attachment_record)
        if (
            inspection.attachment is None
            or inspection.attachment.attachment_receipt_sha256 != attachment_receipt_sha256
            or attachment_record.authority != authority
        ):
            raise ContainmentIncompatibleError(
                "containment inspection attachment does not match the request"
            )
        return inspection

    def await_drain(
        self,
        authority: AuthorityBindingV1,
        *,
        attachment_receipt_sha256: str,
        release_receipt_sha256: str,
        termination_reason: str,
        timeout_seconds: float,
    ) -> LinuxSystemdScopeDrainV1:
        milliseconds = _timeout_milliseconds(timeout_seconds)
        response = self._perform(
            BrokerOperation.AWAIT_DRAIN,
            AwaitDrainPayloadV1(
                authority=authority,
                timeout_milliseconds=milliseconds,
                termination_reason=termination_reason,
            ),
            mutation=True,
            operation_timeout_seconds=(2 * (milliseconds / 1000.0)) + self._packet_timeout_seconds,
        )
        return self._parse_drain(
            response,
            authority=authority,
            attachment_receipt_sha256=attachment_receipt_sha256,
            release_receipt_sha256=release_receipt_sha256,
        )

    def stop(
        self,
        authority: AuthorityBindingV1,
        *,
        attachment_receipt_sha256: str,
        release_receipt_sha256: str | None,
        termination_reason: str,
        timeout_seconds: float,
    ) -> LinuxSystemdScopeDrainV1:
        milliseconds = _timeout_milliseconds(timeout_seconds)
        response = self._perform(
            BrokerOperation.STOP,
            StopPayloadV1(
                authority=authority,
                graceful_timeout_milliseconds=milliseconds,
                forced_timeout_milliseconds=milliseconds,
                termination_reason=termination_reason,
            ),
            mutation=True,
            operation_timeout_seconds=(2 * (milliseconds / 1000.0)) + self._packet_timeout_seconds,
        )
        return self._parse_drain(
            response,
            authority=authority,
            attachment_receipt_sha256=attachment_receipt_sha256,
            release_receipt_sha256=release_receipt_sha256,
        )

    def recover_stop(
        self,
        authority: AuthorityBindingV1,
        *,
        attachment_receipt_sha256: str,
        expected_owner_process_id: int,
        expected_owner_process_start_token: str,
        timeout_seconds: float,
    ) -> LinuxSystemdScopeDrainV1:
        milliseconds = _timeout_milliseconds(timeout_seconds)
        response = self._perform(
            BrokerOperation.RECOVER_STOP,
            RecoverStopPayloadV1(
                authority=authority,
                original_owner_pid=expected_owner_process_id,
                original_owner_start_token=expected_owner_process_start_token,
                graceful_timeout_milliseconds=milliseconds,
                forced_timeout_milliseconds=milliseconds,
            ),
            mutation=True,
            operation_timeout_seconds=(2 * (milliseconds / 1000.0)) + self._packet_timeout_seconds,
        )
        try:
            drain = LinuxSystemdScopeDrainV1.from_mapping(response)
            drain_record = BrokerDrainRecordV1.from_mapping(
                loads_canonical_packet(drain.drain_receipt_json_utf8.encode("utf-8"))
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment recovery drain receipt is incompatible"
            ) from exc
        self._assert_validated_root_bindings(drain_record)
        if (
            drain.attachment_receipt_sha256 != attachment_receipt_sha256
            or drain_record.authority != authority
        ):
            raise ContainmentIncompatibleError(
                "containment recovery drain does not match the request"
            )
        return drain

    def _perform(
        self,
        operation: BrokerOperation,
        payload: object,
        *,
        mutation: bool = False,
        operation_timeout_seconds: float | None = None,
    ) -> object:
        request = BrokerRequestV1(
            schema_name="mif-containment-broker-request",
            schema_version=1,
            operation=operation,
            request_id=self._request_id_factory(),
            payload=payload,
        )
        response, _peer_pid = self._exchange(
            request,
            mutation=mutation,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        return response

    def _parse_drain(
        self,
        payload: object,
        *,
        authority: AuthorityBindingV1,
        attachment_receipt_sha256: str,
        release_receipt_sha256: str | None,
    ) -> LinuxSystemdScopeDrainV1:
        try:
            drain = LinuxSystemdScopeDrainV1.from_mapping(payload)
            drain_record = BrokerDrainRecordV1.from_mapping(
                loads_canonical_packet(drain.drain_receipt_json_utf8.encode("utf-8"))
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError("containment drain receipt is incompatible") from exc
        self._assert_validated_root_bindings(drain_record)
        if (
            drain.attachment_receipt_sha256 != attachment_receipt_sha256
            or drain.release_receipt_sha256 != release_receipt_sha256
            or drain_record.authority != authority
        ):
            raise ContainmentIncompatibleError(
                "containment drain receipt does not match the request"
            )
        return drain

    def _exchange(
        self,
        request: BrokerRequestV1,
        *,
        mutation: bool = False,
        target_executable_fd: int | None = None,
        operation_timeout_seconds: float | None = None,
        operation_deadline: float | None = None,
    ) -> tuple[object, int]:
        descriptor: int | None = None
        owned_target_fd: int | None = None
        connection = None
        send_attempted = False
        operation_timeout = (
            self._operation_timeout_seconds
            if operation_timeout_seconds is None
            else float(operation_timeout_seconds)
        )
        deadline = (
            self._operations.monotonic() + operation_timeout
            if operation_deadline is None
            else operation_deadline
        )
        try:
            path_identity = self._operations.path_identity_nofollow(BROKER_SOCKET_PATH)
            self._validate_socket_identity(path_identity)
            descriptor = self._operations.open_path_nofollow(BROKER_SOCKET_PATH)
            opened_identity = self._operations.descriptor_identity(descriptor)
            self._validate_socket_identity(opened_identity)
            if opened_identity != path_identity:
                raise ContainmentUnavailableError("containment broker socket identity changed")
            connection = self._operations.create_seqpacket_socket()
            self._set_phase_timeout(
                connection,
                deadline=deadline,
                maximum=self._connect_timeout_seconds,
            )
            connection.connect(f"/proc/self/fd/{descriptor}")
            if (
                self._operations.descriptor_identity(descriptor) != opened_identity
                or self._operations.path_identity_nofollow(BROKER_SOCKET_PATH) != opened_identity
            ):
                raise ContainmentUnavailableError(
                    "containment broker socket identity changed during connect"
                )
            peer = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            if type(peer) is not bytes or len(peer) != struct.calcsize("3i"):
                raise ContainmentUnavailableError(
                    "containment broker peer credentials are malformed"
                )
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", peer)
            if peer_pid < 1 or peer_uid != 0 or peer_gid < 0:
                raise ContainmentUnavailableError(
                    "containment broker server is not the root broker"
                )
            packet = request.canonical_bytes()
            self._set_phase_timeout(
                connection,
                deadline=deadline,
                maximum=self._packet_timeout_seconds,
            )
            if target_executable_fd is None:
                send_attempted = True
                sent = connection.send(packet)
            else:
                if type(target_executable_fd) is not int or target_executable_fd < 0:
                    raise ValueError("target executable descriptor is invalid")
                owned_target_fd = self._operations.duplicate_fd_cloexec(target_executable_fd)
                send_attempted = True
                sent = connection.sendmsg(
                    (packet,),
                    (
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_RIGHTS,
                            array.array("i", (owned_target_fd,)),
                        ),
                    ),
                )
            if type(sent) is not int or sent != len(packet):
                raise _RetryableUnavailableError(
                    "containment broker request was not sent atomically"
                )
            self._set_phase_timeout(
                connection,
                deadline=deadline,
                maximum=None,
            )
            response_packet = connection.recv(MAXIMUM_PACKET_BYTES + 1)
            if (
                type(response_packet) is not bytes
                or not response_packet
                or len(response_packet) > MAXIMUM_PACKET_BYTES
            ):
                raise _RetryableUnavailableError(
                    "containment broker response packet is unavailable"
                )
            return (
                self._decode_response(
                    request=request,
                    packet=response_packet,
                ),
                peer_pid,
            )
        except ContainmentUnavailableError as exc:
            if mutation and send_attempted:
                raise ContainmentIndeterminateError(
                    "containment mutation outcome is indeterminate"
                ) from exc
            raise
        except ContainmentClientError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if mutation and send_attempted:
                raise ContainmentIndeterminateError(
                    "containment mutation outcome is indeterminate"
                ) from exc
            raise _RetryableUnavailableError("containment broker socket is unavailable") from exc
        except BaseException as exc:
            if mutation and send_attempted:
                raise ContainmentIndeterminateError(
                    "containment mutation outcome is indeterminate"
                ) from exc
            raise
        finally:
            cleanup_steps: list[Callable[[], object]] = []
            if owned_target_fd is not None:
                cleanup_steps.append(lambda: self._operations.close_fd(owned_target_fd))
            if connection is not None:
                cleanup_steps.append(connection.close)
            if descriptor is not None:
                cleanup_steps.append(lambda: self._operations.close_fd(descriptor))
            cleanup_failures: list[BaseException] = []
            for cleanup in cleanup_steps:
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    cleanup_failures.append(cleanup_error)
            if cleanup_failures:
                primary_error = sys.exception()
                if primary_error is None:
                    raise cleanup_failures[0]
                for cleanup_error in cleanup_failures:
                    primary_error.add_note(
                        "additional containment client cleanup failure: "
                        f"{type(cleanup_error).__name__}"
                    )

    def _set_phase_timeout(
        self,
        connection: object,
        *,
        deadline: float,
        maximum: float | None,
    ) -> None:
        remaining = deadline - self._operations.monotonic()
        if remaining <= 0:
            raise ContainmentUnavailableError("containment broker operation deadline expired")
        connection.settimeout(remaining if maximum is None else min(maximum, remaining))

    @staticmethod
    def _decode_response(
        *,
        request: BrokerRequestV1,
        packet: bytes,
    ) -> object:
        try:
            decoded = loads_canonical_packet(
                packet,
                maximum_bytes=MAXIMUM_PACKET_BYTES,
            )
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment broker response is not canonical"
            ) from exc
        if not isinstance(decoded, Mapping) or set(decoded) != {
            "error",
            "payload",
            "request_id",
            "schema_name",
            "schema_version",
            "status",
        }:
            raise ContainmentIncompatibleError(
                "containment broker response fields are incompatible"
            )
        if (
            decoded["schema_name"] != "mif-containment-broker-response"
            or type(decoded["schema_version"]) is not int
            or decoded["schema_version"] != 1
            or decoded["request_id"] != request.request_id
        ):
            raise ContainmentIncompatibleError(
                "containment broker response identity is incompatible"
            )
        status = decoded["status"]
        error = decoded["error"]
        payload = decoded["payload"]
        if status in ("rejected", "conflict", "incompatible") or (
            status == "ambiguous" and payload is None
        ):
            if (
                type(error) is not str
                or not error
                or len(error.encode("utf-8")) > 512
                or any(ord(character) < 32 for character in error)
                or payload is not None
            ):
                raise ContainmentIncompatibleError("containment broker remote error is malformed")
            error_types = {
                "rejected": ContainmentRejectedError,
                "conflict": ContainmentConflictError,
                "ambiguous": ContainmentAmbiguousError,
                "incompatible": ContainmentIncompatibleError,
            }
            messages = {
                "rejected": "containment broker rejected the request",
                "conflict": "containment broker request conflicts with durable state",
                "ambiguous": "containment broker authority is ambiguous",
                "incompatible": "containment broker contract is incompatible",
            }
            raise error_types[status](messages[status])
        if (
            status not in ("ok", "live", "drained", "ambiguous")
            or error is not None
            or not isinstance(payload, Mapping)
        ):
            raise ContainmentIncompatibleError("containment broker response status is incompatible")
        expected_statuses = {
            BrokerOperation.CAPABILITIES: ("ok",),
            BrokerOperation.ATTACH: ("ok",),
            BrokerOperation.RELEASE: ("ok",),
            BrokerOperation.GATE_AUTHORIZE_EXEC: ("ok",),
            BrokerOperation.INSPECT: ("live", "drained", "ambiguous"),
            BrokerOperation.AWAIT_DRAIN: ("drained",),
            BrokerOperation.STOP: ("drained",),
            BrokerOperation.RECOVER_STOP: ("drained",),
        }
        if status not in expected_statuses[request.operation]:
            raise ContainmentIncompatibleError(
                "containment broker response operation is incompatible"
            )
        return payload

    def _validate_socket_identity(self, identity: object) -> None:
        try:
            expected_gid = self._operations.socket_group_gid()
            valid = (
                stat.S_ISSOCK(identity.mode)
                and stat.S_IMODE(identity.mode) == 0o660
                and identity.uid == 0
                and identity.gid == expected_gid
                and identity.nlink == 1
                and identity.device >= 0
                and identity.inode >= 1
                and identity.size >= 0
            )
        except (AttributeError, LookupError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ContainmentUnavailableError("containment broker socket identity is unsafe")


class GateBrokerClient(ContainmentBrokerClient):
    """Gate-only client that carries no application authority material."""

    def authorize_exec(
        self,
        *,
        launch_nonce_sha256: str,
        gate_material_sha256: str,
    ) -> GateExecAuthorizationV1:
        request = BrokerRequestV1(
            schema_name="mif-containment-broker-request",
            schema_version=1,
            operation=BrokerOperation.GATE_AUTHORIZE_EXEC,
            request_id=self._request_id_factory(),
            payload=GateAuthorizeExecPayloadV1(
                launch_nonce_sha256=launch_nonce_sha256,
                gate_material_sha256=gate_material_sha256,
            ),
        )
        payload, _peer_pid = self._exchange(request, mutation=True)
        try:
            authorization = GateExecAuthorizationV1.from_mapping(payload)
        except (TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "containment gate authorization is incompatible"
            ) from exc
        if (
            authorization.launch_nonce_sha256 != launch_nonce_sha256
            or authorization.gate_material_sha256 != gate_material_sha256
        ):
            raise ContainmentIncompatibleError(
                "containment gate authorization does not match this gate"
            )
        return authorization


class LinuxContainmentRecovery:
    """Dependency-neutral adapter over fresh broker-backed recovery evidence."""

    def __init__(
        self,
        client: ContainmentBrokerClient,
        *,
        timeout_seconds: float,
    ) -> None:
        _timeout_milliseconds(timeout_seconds)
        self._client = client
        self._timeout_seconds = float(timeout_seconds)

    def inspect(self, request: object) -> ContainmentInspection:
        authority, copied_attachment = self._validate_request_copy(request)
        inspection = self._client.inspect(
            authority,
            attachment_receipt_sha256=(copied_attachment.attachment_receipt_sha256),
        )
        try:
            checked = ContainmentInspection.from_mapping(inspection.to_mapping())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "root-backed containment inspection is incompatible"
            ) from exc
        if (
            checked.attachment is None
            or checked.attachment.attachment_receipt_sha256
            != copied_attachment.attachment_receipt_sha256
            or checked.attachment.attachment_receipt_json_utf8
            != copied_attachment.attachment_receipt_json_utf8
        ):
            raise ContainmentIncompatibleError(
                "root-backed attachment differs from the application copy"
            )
        return checked

    def recover_stop(self, request: object) -> LinuxSystemdScopeDrainV1:
        authority, copied_attachment = self._validate_request_copy(request)
        try:
            owner_pid = request.original_owner_process_id
            owner_start = request.original_owner_process_start_token
        except AttributeError as exc:
            raise ContainmentIncompatibleError(
                "containment recovery request is incomplete"
            ) from exc
        drain = self._client.recover_stop(
            authority,
            attachment_receipt_sha256=(copied_attachment.attachment_receipt_sha256),
            expected_owner_process_id=owner_pid,
            expected_owner_process_start_token=owner_start,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            checked = LinuxSystemdScopeDrainV1.from_mapping(drain.to_mapping())
            drain_record = BrokerDrainRecordV1.from_mapping(
                loads_canonical_packet(checked.drain_receipt_json_utf8.encode("utf-8"))
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "root-backed containment drain is incompatible"
            ) from exc
        if (
            checked.attachment_receipt_sha256 != copied_attachment.attachment_receipt_sha256
            or drain_record.authority != authority
        ):
            raise ContainmentIncompatibleError(
                "root-backed drain differs from the recovery request"
            )
        return checked

    @staticmethod
    def _validate_request_copy(
        request: object,
    ) -> tuple[AuthorityBindingV1, LinuxSystemdScopeAttachmentV1]:
        try:
            authority = request.authority
            launch_evidence = request.launch_evidence
            if type(authority) is not AuthorityBindingV1:
                raise TypeError("authority has the wrong type")
            checked_authority = AuthorityBindingV1.from_mapping(authority.to_mapping())
            if type(launch_evidence) is not LinuxSystemdScopeAttachmentV1:
                raise TypeError("launch evidence has the wrong type")
            checked_attachment = LinuxSystemdScopeAttachmentV1.from_mapping(
                launch_evidence.to_mapping()
            )
            attachment_record = BrokerAttachmentRecordV1.from_mapping(
                loads_canonical_packet(
                    checked_attachment.attachment_receipt_json_utf8.encode("utf-8")
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ContainmentIncompatibleError(
                "application containment recovery evidence is incompatible"
            ) from exc
        if attachment_record.authority != checked_authority:
            raise ContainmentIncompatibleError(
                "application attachment authority differs from recovery authority"
            )
        return checked_authority, checked_attachment


def _timeout_milliseconds(value: float) -> int:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > 3_600.0:
        raise ValueError("timeout_seconds must be positive and bounded")
    milliseconds = math.ceil(float(value) * 1000.0)
    if not 1 <= milliseconds <= 3_600_000:
        raise ValueError("timeout_seconds is outside the protocol bound")
    return milliseconds


def preflight_linux_containment(
    expectation: ContainmentCompatibilityExpectationV1,
    *,
    client: ContainmentBrokerClient | None = None,
) -> BrokerExecutableRegistrationV1:
    """Validate live Linux containment compatibility before gate construction."""

    selected_client = ContainmentBrokerClient() if client is None else client
    if not isinstance(selected_client, ContainmentBrokerClient):
        raise TypeError("client must be a ContainmentBrokerClient")
    return selected_client.preflight(expectation)


class _PosixClientOperations:
    """Small standard-library boundary around unprivileged Linux operations."""

    def platform_name(self) -> str:
        return platform.system()

    def monotonic(self) -> float:
        return time.monotonic()

    def current_uid(self) -> int:
        return os.getuid()

    def current_gid(self) -> int:
        return os.getgid()

    def read_boot_id(self) -> str:
        descriptor = os.open(
            "/proc/sys/kernel/random/boot_id",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            raw = os.read(descriptor, 65)
            if len(raw) > 64:
                raise OSError("boot identity exceeds its fixed bound")
        finally:
            os.close(descriptor)
        try:
            return raw.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise OSError("boot identity is not ASCII") from exc

    def socket_group_gid(self) -> int:
        import grp

        return grp.getgrnam("mif-pipeline-containment").gr_gid

    def path_identity_nofollow(self, path: str) -> _FileIdentity:
        return self._identity(os.stat(path, follow_symlinks=False))

    def open_path_nofollow(self, path: str) -> int:
        if path == BROKER_SOCKET_PATH:
            flags = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_PATH
        else:
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        return os.open(path, flags)

    def descriptor_identity(self, descriptor: int) -> _FileIdentity:
        return self._identity(os.fstat(descriptor))

    def descriptor_sha256(self, descriptor: int, *, deadline: float) -> str:
        before = self.descriptor_identity(descriptor)
        if before.size > _MAXIMUM_EXECUTABLE_BYTES:
            raise OSError("executable exceeds its fixed byte bound")
        digest = hashlib.sha256()
        offset = 0
        while True:
            if self.monotonic() >= deadline:
                raise TimeoutError("executable hashing deadline expired")
            remaining = _MAXIMUM_EXECUTABLE_BYTES - offset
            chunk = os.pread(
                descriptor,
                min(1024 * 1024, remaining + 1),
                offset,
            )
            if not chunk:
                break
            if len(chunk) > remaining:
                raise OSError("executable exceeds its fixed byte bound")
            digest.update(chunk)
            offset += len(chunk)
        if offset != before.size or self.descriptor_identity(descriptor) != before:
            raise OSError("executable changed while hashing")
        return digest.hexdigest()

    def create_seqpacket_socket(self) -> socket.socket:
        return socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET, 0)

    def duplicate_fd_cloexec(self, descriptor: int) -> int:
        import fcntl

        return fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 0)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    @staticmethod
    def _identity(observed: os.stat_result) -> _FileIdentity:
        return _FileIdentity(
            device=observed.st_dev,
            inode=observed.st_ino,
            mode=observed.st_mode,
            uid=observed.st_uid,
            gid=observed.st_gid,
            nlink=observed.st_nlink,
            size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            ctime_ns=observed.st_ctime_ns,
        )
