"""Strict version-one records for the Linux containment broker protocol.

This module depends only on the Python standard library and the neutral canonical codec. It
is safe to import on non-Linux platforms.

The `GateReadyV1` record carries `launch_nonce_sha256`, `gate_pid`, `gate_start_token`,
`expected_parent_pid`, `expected_parent_start_token` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import posixpath
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, ClassVar, Literal, Union, get_args, get_origin, get_type_hints

from .codec import dumps_canonical, loads_canonical_packet, sha256_hex

PROTOCOL_VERSION = 1
PROVIDER = "linux-systemd-scope-broker/v1"
CONTROL_SOCKET_FD = 3
RETAINED_TARGET_FD = 4
MAXIMUM_ARGV_COUNT = 256
MAXIMUM_ENVIRONMENT_COUNT = 4_096
MAXIMUM_ENVIRONMENT_BYTES = 1_048_576
_MAXIMUM_ROOT_RECORD_BYTES = 16_384

_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_UNIT_NAME = re.compile(r"mif-qupath-[A-Za-z0-9_.:@-]+\.scope\Z")
_TOKEN = re.compile(r"[0-9]+\Z")
_CGROUP_BASENAME = re.compile(r"[A-Za-z0-9_.:@-]{1,255}\Z")
_SYSTEMD_JOB_PATH = re.compile(r"/org/freedesktop/systemd1/job/[1-9][0-9]*\Z")
_SYSTEMD_RESULT = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_LINUX_PROCESS_START_TOKEN = re.compile(r"linux-proc-stat/v1:(?P<pid>[1-9][0-9]*):[1-9][0-9]*\Z")
_REGISTRATION_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
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
_DANGEROUS_ENVIRONMENT_PREFIXES = ("LD_", "DYLD_")

CERTIFIED_CLAIMS = (
    "two-stopped-initial-pids/v1",
    "fixed-scope-policy/v1",
    "exact-unit-cgroup-identity/v1",
    "recursive-populated-transition/v1",
    "fixed-stop-and-drain/v1",
)
LEDGER_RECORD_SCHEMAS = (
    "mif-containment-broker-attempt/v1",
    "mif-containment-broker-attachment/v1",
    "mif-containment-broker-release-intent/v1",
    "mif-containment-broker-release/v1",
    "mif-containment-broker-stop-intent/v1",
    "mif-containment-broker-drain/v1",
)
COMMAND_PROFILES = ("headless-script", "interactive-project")
DESCRIPTOR_EXEC_CAPABILITY = "posix-fd-exec/v1"
ANCHOR_PROMOTION_CAPABILITY = "pdeathsig-two-phase/v1"
COLLECT_MODE_CAPABILITY = "inactive-or-failed"
DRAIN_PROOF_CAPABILITY = "cgroup-events-zero-or-bound-exact-deletion/v1"


# ==============================================================================
#                                    Protocol
# ==============================================================================


class BrokerOperation(str, Enum):
    CAPABILITIES = "capabilities"
    ATTACH = "attach"
    RELEASE = "release"
    GATE_AUTHORIZE_EXEC = "gate_authorize_exec"
    INSPECT = "inspect"
    AWAIT_DRAIN = "await_drain"
    STOP = "stop"
    RECOVER_STOP = "recover_stop"


class ContainmentSemanticState(str, Enum):
    LIVE = "live"
    DRAINED = "drained"
    AMBIGUOUS = "ambiguous"


class BrokerResponseStatus(str, Enum):
    OK = "ok"
    LIVE = "live"
    DRAINED = "drained"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    INCOMPATIBLE = "incompatible"


class ReplayClassification(str, Enum):
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    DISTINCT = "distinct"


class ClosedModel:
    """Shared strict mapping and canonical serialization behavior."""

    def to_mapping(self) -> dict[str, object]:
        return {item.name: _serialize(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def from_mapping(cls, value: object):
        mapping = _closed_mapping(value, cls)
        hints = get_type_hints(cls)
        parsed = {
            item.name: _deserialize(mapping[item.name], hints[item.name], item.name)
            for item in fields(cls)
        }
        return cls(**parsed)

    def canonical_json_utf8(self) -> str:
        return dumps_canonical(self.to_mapping()).decode("utf-8")

    def canonical_sha256(self) -> str:
        return sha256_hex(self.canonical_json_utf8().encode("utf-8"))


@dataclass(frozen=True, slots=True)
class AuthorityBindingV1(ClosedModel):
    launch_nonce_sha256: str
    application_request_sha256: str
    project_identity_sha256: str
    active_mutation_run_sha256: str
    containment_scope_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "launch_nonce_sha256",
            "application_request_sha256",
            "project_identity_sha256",
            "active_mutation_run_sha256",
            "containment_scope_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class ScopeAnchorIdentityV1(ClosedModel):
    anchor_pid: int
    anchor_start_token: str
    anchor_uid: int
    anchor_gid: int
    anchor_executable_sha256: str

    def __post_init__(self) -> None:
        _require_integer(self.anchor_pid, "anchor_pid", minimum=1)
        _require_start_token(self.anchor_start_token, "anchor_start_token")
        _require_integer(self.anchor_uid, "anchor_uid", minimum=0)
        _require_integer(self.anchor_gid, "anchor_gid", minimum=0)
        _require_sha256(
            self.anchor_executable_sha256,
            "anchor_executable_sha256",
        )


@dataclass(frozen=True, slots=True)
class CgroupPathComponentIdentityV1(ClosedModel):
    basename: str
    directory_device: int
    directory_inode: int

    def __post_init__(self) -> None:
        if (
            type(self.basename) is not str
            or self.basename in (".", "..")
            or _CGROUP_BASENAME.fullmatch(self.basename) is None
        ):
            raise ValueError("basename must be one safe cgroup path component")
        _require_integer(self.directory_device, "directory_device", minimum=1)
        _require_integer(self.directory_inode, "directory_inode", minimum=1)


@dataclass(frozen=True, slots=True)
class CgroupDirectoryIdentityV1(ClosedModel):
    mount_device: int
    mount_inode: int
    components: tuple[CgroupPathComponentIdentityV1, ...]

    def __post_init__(self) -> None:
        _require_integer(self.mount_device, "mount_device", minimum=1)
        _require_integer(self.mount_inode, "mount_inode", minimum=1)
        if type(self.components) is not tuple or not self.components:
            raise ValueError("components must be a non-empty ordered tuple")
        if any(
            not isinstance(component, CgroupPathComponentIdentityV1)
            for component in self.components
        ):
            raise ValueError("components must contain only cgroup component identities")


@dataclass(frozen=True, slots=True)
class SystemdStopJobEvidenceV1(ClosedModel):
    job_path: str
    result: str

    def __post_init__(self) -> None:
        if type(self.job_path) is not str or _SYSTEMD_JOB_PATH.fullmatch(self.job_path) is None:
            raise ValueError("job_path must be an exact systemd job object path")
        if type(self.result) is not str or _SYSTEMD_RESULT.fullmatch(self.result) is None:
            raise ValueError("result must be a bounded lowercase systemd result token")


@dataclass(frozen=True, slots=True)
class CgroupEventsZeroProofV1(ClosedModel):
    proof_kind: Literal["cgroup-events-zero"]
    cgroup_identity: CgroupDirectoryIdentityV1
    populated: Literal[0]

    def __post_init__(self) -> None:
        _require_literal(self.proof_kind, "cgroup-events-zero", "proof_kind")
        if not isinstance(self.cgroup_identity, CgroupDirectoryIdentityV1):
            raise ValueError("cgroup_identity must be a CgroupDirectoryIdentityV1")
        _require_exact_integer(self.populated, 0, "populated")


@dataclass(frozen=True, slots=True)
class CgroupDeletedAfterStopProofV1(ClosedModel):
    proof_kind: Literal["cgroup-deleted-after-stop"]
    cgroup_identity: CgroupDirectoryIdentityV1
    matched_prefix_length: int
    first_absent_component_index: int
    first_absent_component_basename: str
    replacement_rejected: Literal[True]

    def __post_init__(self) -> None:
        _require_literal(
            self.proof_kind,
            "cgroup-deleted-after-stop",
            "proof_kind",
        )
        if not isinstance(self.cgroup_identity, CgroupDirectoryIdentityV1):
            raise ValueError("cgroup_identity must be a CgroupDirectoryIdentityV1")
        _require_integer(
            self.matched_prefix_length,
            "matched_prefix_length",
            minimum=0,
        )
        _require_integer(
            self.first_absent_component_index,
            "first_absent_component_index",
            minimum=0,
        )
        if self.matched_prefix_length != self.first_absent_component_index:
            raise ValueError("matched_prefix_length must equal first_absent_component_index")
        if self.first_absent_component_index >= len(self.cgroup_identity.components):
            raise ValueError("first_absent_component_index must identify a recorded component")
        expected_basename = self.cgroup_identity.components[
            self.first_absent_component_index
        ].basename
        if self.first_absent_component_basename != expected_basename:
            raise ValueError("first_absent_component_basename must match the recorded component")
        _require_literal(self.replacement_rejected, True, "replacement_rejected")


@dataclass(frozen=True, slots=True)
class MachineRebootProofV1(ClosedModel):
    proof_kind: Literal["machine-reboot"]
    prior_boot_id: str
    current_boot_id: str

    def __post_init__(self) -> None:
        _require_literal(self.proof_kind, "machine-reboot", "proof_kind")
        _require_boot_id(self.prior_boot_id)
        _require_boot_id(self.current_boot_id)
        if self.prior_boot_id == self.current_boot_id:
            raise ValueError("machine reboot proof requires different complete boot IDs")


BrokerDrainProofV1 = CgroupEventsZeroProofV1 | CgroupDeletedAfterStopProofV1 | MachineRebootProofV1


@dataclass(frozen=True, slots=True)
class GateReadyV1(ClosedModel):
    launch_nonce_sha256: str
    gate_pid: int
    gate_start_token: str
    expected_parent_pid: int
    expected_parent_start_token: str
    control_socket_fd: int
    retained_target_fd: int
    target_executable_device: int
    target_executable_inode: int
    target_executable_sha256: str
    argv_count: int
    argv_sha256: str
    environment_count: int
    environment_bytes: int
    environment_names_sizes_sha256: str
    cwd_device: int
    cwd_inode: int
    gate_material_sha256: str

    def __post_init__(self) -> None:
        """Validate the GateReadyV1 fields when the record is constructed.

        The checks cover ``gate_pid``, ``gate_start_token``, ``expected_parent_pid``,
        ``expected_parent_start_token``, ``control_socket_fd``, ``retained_target_fd``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        for field_name in (
            "launch_nonce_sha256",
            "target_executable_sha256",
            "argv_sha256",
            "environment_names_sizes_sha256",
            "gate_material_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _require_integer(self.gate_pid, "gate_pid", minimum=1)
        _require_start_token(self.gate_start_token, "gate_start_token")
        _require_integer(self.expected_parent_pid, "expected_parent_pid", minimum=1)
        _require_start_token(
            self.expected_parent_start_token,
            "expected_parent_start_token",
        )
        _require_exact_integer(self.control_socket_fd, CONTROL_SOCKET_FD, "control_socket_fd")
        _require_exact_integer(self.retained_target_fd, RETAINED_TARGET_FD, "retained_target_fd")
        _require_integer(self.target_executable_device, "target_executable_device", minimum=0)
        _require_integer(self.target_executable_inode, "target_executable_inode", minimum=1)
        _require_integer(self.argv_count, "argv_count", minimum=1, maximum=MAXIMUM_ARGV_COUNT)
        _require_integer(
            self.environment_count,
            "environment_count",
            minimum=0,
            maximum=MAXIMUM_ENVIRONMENT_COUNT,
        )
        _require_integer(
            self.environment_bytes,
            "environment_bytes",
            minimum=0,
            maximum=MAXIMUM_ENVIRONMENT_BYTES,
        )
        _require_integer(self.cwd_device, "cwd_device", minimum=0)
        _require_integer(self.cwd_inode, "cwd_inode", minimum=1)
        material = self.to_mapping()
        material.pop("gate_material_sha256")
        if sha256_hex(dumps_canonical(material)) != self.gate_material_sha256:
            raise ValueError("gate material digest does not match the complete gate material")


@dataclass(frozen=True, slots=True)
class BrokerExecutableRegistrationV1(ClosedModel):
    registration_id: str
    canonical_path: str
    target_executable_sha256: str
    allowed_command_profiles: tuple[str, ...]
    allowed_environment_names: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate the BrokerExecutableRegistrationV1 fields when the record is constructed.

        The checks cover ``allowed_environment_names``, ``canonical_path``,
        ``target_executable_sha256``, ``allowed_command_profiles``, ``registration_id``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if (
            type(self.registration_id) is not str
            or _REGISTRATION_IDENTIFIER.fullmatch(self.registration_id) is None
        ):
            raise ValueError("registration_id must be a safe bounded identifier")
        _require_canonical_absolute_path(self.canonical_path, "canonical_path")
        _require_sha256(
            self.target_executable_sha256,
            "target_executable_sha256",
        )
        if (
            type(self.allowed_command_profiles) is not tuple
            or not self.allowed_command_profiles
            or len(self.allowed_command_profiles) > len(COMMAND_PROFILES)
            or any(
                type(profile) is not str or profile not in COMMAND_PROFILES
                for profile in self.allowed_command_profiles
            )
            or len(self.allowed_command_profiles) != len(set(self.allowed_command_profiles))
        ):
            raise ValueError(
                "allowed_command_profiles must be an ordered non-empty "
                "duplicate-free subset of the closed command profiles"
            )
        if (
            type(self.allowed_environment_names) is not tuple
            or len(self.allowed_environment_names) > MAXIMUM_ENVIRONMENT_COUNT
        ):
            raise ValueError("allowed_environment_names must be a bounded ordered tuple")
        for name in self.allowed_environment_names:
            if (
                type(name) is not str
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name in _DANGEROUS_ENVIRONMENT
                or name.startswith(_DANGEROUS_ENVIRONMENT_PREFIXES)
            ):
                raise ValueError("allowed_environment_names contains an unsafe environment name")
        if len(self.allowed_environment_names) != len(set(self.allowed_environment_names)):
            raise ValueError("allowed_environment_names contains duplicates")


@dataclass(frozen=True, slots=True)
class BrokerSecurityProfileV1(ClosedModel):
    ledger_record_schemas: tuple[str, ...]
    descriptor_exec: str
    anchor_promotion: str
    collect_mode: str
    drain_proof: str
    registrations: tuple[BrokerExecutableRegistrationV1, ...]

    def __post_init__(self) -> None:
        """Validate the BrokerSecurityProfileV1 fields when the record is constructed.

        The checks cover ``registrations``, ``descriptor_exec``, ``anchor_promotion``,
        ``collect_mode``, ``drain_proof``, ``ledger_record_schemas``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if (
            type(self.ledger_record_schemas) is not tuple
            or self.ledger_record_schemas != LEDGER_RECORD_SCHEMAS
        ):
            raise ValueError("ledger_record_schemas must contain the exact ordered ledger family")
        _require_literal(
            self.descriptor_exec,
            DESCRIPTOR_EXEC_CAPABILITY,
            "descriptor_exec",
        )
        _require_literal(
            self.anchor_promotion,
            ANCHOR_PROMOTION_CAPABILITY,
            "anchor_promotion",
        )
        _require_literal(self.collect_mode, COLLECT_MODE_CAPABILITY, "collect_mode")
        _require_literal(self.drain_proof, DRAIN_PROOF_CAPABILITY, "drain_proof")
        if type(self.registrations) is not tuple or any(
            not isinstance(registration, BrokerExecutableRegistrationV1)
            for registration in self.registrations
        ):
            raise ValueError("registrations must contain only executable registration capabilities")
        for registration in self.registrations:
            registration.__post_init__()
        registration_ids = tuple(
            registration.registration_id for registration in self.registrations
        )
        if registration_ids != tuple(sorted(registration_ids)):
            raise ValueError("registrations must be sorted by registration_id")
        if len(registration_ids) != len(set(registration_ids)):
            raise ValueError("registration identifiers must be unique")
        paths = tuple(registration.canonical_path for registration in self.registrations)
        if len(paths) != len(set(paths)):
            raise ValueError("registration canonical paths must be unique")


@dataclass(frozen=True, slots=True)
class StartupCertificationSummaryV1(ClosedModel):
    certification_sha256: str
    broker_build_id: str
    broker_config_sha256: str
    boot_id: str
    scope_policy_sha256: str
    broker_process_id: int
    broker_process_start_token: str
    system_bus_id: str
    systemd_unique_owner: str
    certified_claims: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.certification_sha256, "certification_sha256")
        _require_text(self.broker_build_id, "broker_build_id", maximum=128)
        _require_sha256(self.broker_config_sha256, "broker_config_sha256")
        _require_boot_id(self.boot_id)
        _require_sha256(self.scope_policy_sha256, "scope_policy_sha256")
        _require_integer(self.broker_process_id, "broker_process_id", minimum=1)
        if (
            type(self.broker_process_start_token) is not str
            or (match := _LINUX_PROCESS_START_TOKEN.fullmatch(self.broker_process_start_token))
            is None
            or int(match.group("pid"), 10) != self.broker_process_id
        ):
            raise ValueError("broker_process_start_token must name the exact broker process")
        if (
            type(self.system_bus_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.system_bus_id) is None
        ):
            raise ValueError("system_bus_id must be exactly 32 lowercase hexadecimal characters")
        if (
            type(self.systemd_unique_owner) is not str
            or re.fullmatch(
                r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
                self.systemd_unique_owner,
            )
            is None
        ):
            raise ValueError("systemd_unique_owner must be an exact unique bus name")
        if type(self.certified_claims) is not tuple or self.certified_claims != CERTIFIED_CLAIMS:
            raise ValueError("certified_claims must contain the exact closed claim set")


@dataclass(frozen=True, slots=True)
class BrokerCapabilitiesV1(ClosedModel):
    provider: Literal["linux-systemd-scope-broker/v1"]
    protocol_min: Literal[1]
    protocol_max: Literal[1]
    protocol_version: Literal[1]
    broker_build_id: str
    broker_config_sha256: str
    boot_id: str
    scope_policy_sha256: str
    control_socket_fd: int
    retained_target_fd: int
    operations: tuple[BrokerOperation, ...]
    startup_certification: StartupCertificationSummaryV1
    security_profile: BrokerSecurityProfileV1

    def __post_init__(self) -> None:
        """Validate the BrokerCapabilitiesV1 fields when the record is constructed.

        The checks cover ``provider``, ``protocol_min``, ``protocol_max``,
        ``protocol_version``, ``broker_build_id``, ``broker_config_sha256``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _require_literal(self.provider, PROVIDER, "provider")
        _require_exact_integer(self.protocol_min, PROTOCOL_VERSION, "protocol_min")
        _require_exact_integer(self.protocol_max, PROTOCOL_VERSION, "protocol_max")
        _require_exact_integer(self.protocol_version, PROTOCOL_VERSION, "protocol_version")
        _require_text(self.broker_build_id, "broker_build_id", maximum=128)
        _require_sha256(self.broker_config_sha256, "broker_config_sha256")
        _require_boot_id(self.boot_id)
        _require_sha256(self.scope_policy_sha256, "scope_policy_sha256")
        _require_exact_integer(self.control_socket_fd, CONTROL_SOCKET_FD, "control_socket_fd")
        _require_exact_integer(self.retained_target_fd, RETAINED_TARGET_FD, "retained_target_fd")
        if type(self.operations) is not tuple or self.operations != tuple(BrokerOperation):
            raise ValueError("operations must contain the exact closed broker operation set")
        if not isinstance(
            self.startup_certification,
            StartupCertificationSummaryV1,
        ):
            raise ValueError("startup_certification must be a StartupCertificationSummaryV1")
        if not isinstance(self.security_profile, BrokerSecurityProfileV1):
            raise ValueError("security profile must be a BrokerSecurityProfileV1")
        try:
            self.security_profile.__post_init__()
        except ValueError as exc:
            raise ValueError("security profile is malformed") from exc
        bindings = (
            "broker_build_id",
            "broker_config_sha256",
            "boot_id",
            "scope_policy_sha256",
        )
        if any(
            getattr(self, name) != getattr(self.startup_certification, name) for name in bindings
        ):
            raise ValueError("startup certification conflicts with capabilities")

    def assert_compatible(self, *, protocol_version: int, broker_config_sha256: str) -> None:
        self.__post_init__()
        if type(protocol_version) is not int or protocol_version != self.protocol_version:
            raise ValueError("broker protocol version is incompatible")
        _require_sha256(broker_config_sha256, "broker_config_sha256")
        if broker_config_sha256 != self.broker_config_sha256:
            raise ValueError("broker configuration digest is incompatible")


@dataclass(frozen=True, slots=True)
class ReleaseReceiptV1(ClosedModel):
    launch_nonce_sha256: str
    attachment_receipt_sha256: str
    release_intent_sha256: str
    release_receipt_sha256: str
    gate_material_sha256: str
    application_request_sha256: str
    released_at_unix_ns: int

    def __post_init__(self) -> None:
        for field_name in (
            "launch_nonce_sha256",
            "attachment_receipt_sha256",
            "release_intent_sha256",
            "release_receipt_sha256",
            "gate_material_sha256",
            "application_request_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _require_integer(self.released_at_unix_ns, "released_at_unix_ns", minimum=1)


@dataclass(frozen=True, slots=True)
class GateExecAuthorizationV1(ClosedModel):
    launch_nonce_sha256: str
    gate_pid: int
    gate_start_token: str
    gate_material_sha256: str
    attachment_receipt_json_utf8: str
    attachment_receipt_sha256: str
    release_intent_json_utf8: str
    release_intent_sha256: str
    target_executable_sha256: str
    argv_sha256: str
    environment_names_sizes_sha256: str
    cwd_device: int
    cwd_inode: int
    authorization_sha256: str

    def __post_init__(self) -> None:
        """Validate the GateExecAuthorizationV1 fields when the record is constructed.

        The checks cover ``gate_pid``, ``gate_start_token``, ``cwd_device``, ``cwd_inode``,
        ``attachment_receipt_json_utf8``, ``attachment_receipt_sha256``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        for field_name in (
            "launch_nonce_sha256",
            "gate_material_sha256",
            "attachment_receipt_sha256",
            "release_intent_sha256",
            "target_executable_sha256",
            "argv_sha256",
            "environment_names_sizes_sha256",
            "authorization_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _require_integer(self.gate_pid, "gate_pid", minimum=1)
        _require_start_token(self.gate_start_token, "gate_start_token")
        _require_integer(self.cwd_device, "cwd_device", minimum=0)
        _require_integer(self.cwd_inode, "cwd_inode", minimum=1)
        attachment = _validate_typed_json_copy(
            self.attachment_receipt_json_utf8,
            self.attachment_receipt_sha256,
            label="attachment receipt",
            record_type=BrokerAttachmentRecordV1,
        )
        release_intent = _validate_typed_json_copy(
            self.release_intent_json_utf8,
            self.release_intent_sha256,
            label="release intent",
            record_type=BrokerReleaseIntentRecordV1,
        )
        _require_same_root_identity(
            attachment,
            release_intent,
            label="attachment and release-intent receipts",
        )
        if release_intent.attachment_sha256 != attachment.record_sha256:
            raise ValueError(
                "release-intent attachment predecessor does not match attachment receipt"
            )
        local_bindings = {
            "launch_nonce_sha256": attachment.authority.launch_nonce_sha256,
            "gate_pid": attachment.gate_pid,
            "gate_start_token": attachment.gate_start_token,
            "target_executable_sha256": attachment.target_executable_sha256,
        }
        for field_name, expected in local_bindings.items():
            if getattr(self, field_name) != expected:
                raise ValueError(
                    f"authorization {field_name} does not match its attachment receipt"
                )
        material = self.to_mapping()
        material.pop("authorization_sha256")
        if sha256_hex(dumps_canonical(material)) != self.authorization_sha256:
            raise ValueError(
                "authorization digest does not match the complete authorization material"
            )


@dataclass(frozen=True, slots=True)
class CapabilitiesPayloadV1(ClosedModel):
    pass


@dataclass(frozen=True, slots=True)
class AttachPayloadV1(ClosedModel):
    authority: AuthorityBindingV1
    gate_ready: GateReadyV1

    def __post_init__(self) -> None:
        if self.authority.launch_nonce_sha256 != self.gate_ready.launch_nonce_sha256:
            raise ValueError("gate launch nonce does not match authority binding")


@dataclass(frozen=True, slots=True)
class ReleasePayloadV1(ClosedModel):
    authority: AuthorityBindingV1
    attachment_receipt_json_utf8: str
    attachment_receipt_sha256: str
    application_launch_sha256: str

    def __post_init__(self) -> None:
        attachment_record = _validate_typed_json_copy(
            self.attachment_receipt_json_utf8,
            self.attachment_receipt_sha256,
            label="attachment receipt",
            record_type=BrokerAttachmentRecordV1,
        )
        if attachment_record.authority != self.authority:
            raise ValueError("attachment receipt authority does not match release authority")
        _require_sha256(self.application_launch_sha256, "application_launch_sha256")


@dataclass(frozen=True, slots=True)
class GateAuthorizeExecPayloadV1(ClosedModel):
    launch_nonce_sha256: str
    gate_material_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.launch_nonce_sha256, "launch_nonce_sha256")
        _require_sha256(self.gate_material_sha256, "gate_material_sha256")


@dataclass(frozen=True, slots=True)
class InspectPayloadV1(ClosedModel):
    authority: AuthorityBindingV1


@dataclass(frozen=True, slots=True)
class AwaitDrainPayloadV1(ClosedModel):
    authority: AuthorityBindingV1
    timeout_milliseconds: int
    termination_reason: Literal["normal_exit", "nonzero_exit"]

    def __post_init__(self) -> None:
        _require_integer(
            self.timeout_milliseconds,
            "timeout_milliseconds",
            minimum=1,
            maximum=3_600_000,
        )
        if self.termination_reason not in ("normal_exit", "nonzero_exit"):
            raise ValueError("termination_reason is not valid for await_drain")


_STOP_REASONS = (
    "timeout",
    "cancelled",
    "launch_publication_failed",
    "recovery_stop",
)


@dataclass(frozen=True, slots=True)
class StopPayloadV1(ClosedModel):
    authority: AuthorityBindingV1
    graceful_timeout_milliseconds: int
    forced_timeout_milliseconds: int
    termination_reason: Literal[
        "timeout", "cancelled", "launch_publication_failed", "recovery_stop"
    ]

    def __post_init__(self) -> None:
        _validate_stop_timeouts(self)
        if self.termination_reason not in _STOP_REASONS:
            raise ValueError("termination_reason is not valid for stop")


@dataclass(frozen=True, slots=True)
class RecoverStopPayloadV1(ClosedModel):
    authority: AuthorityBindingV1
    original_owner_pid: int
    original_owner_start_token: str
    graceful_timeout_milliseconds: int
    forced_timeout_milliseconds: int

    def __post_init__(self) -> None:
        _require_integer(self.original_owner_pid, "original_owner_pid", minimum=1)
        _require_start_token(
            self.original_owner_start_token,
            "original_owner_start_token",
        )
        _validate_stop_timeouts(self)


OperationPayloadV1 = Union[
    CapabilitiesPayloadV1,
    AttachPayloadV1,
    ReleasePayloadV1,
    GateAuthorizeExecPayloadV1,
    InspectPayloadV1,
    AwaitDrainPayloadV1,
    StopPayloadV1,
    RecoverStopPayloadV1,
]

_PAYLOAD_TYPES: dict[BrokerOperation, type[ClosedModel]] = {
    BrokerOperation.CAPABILITIES: CapabilitiesPayloadV1,
    BrokerOperation.ATTACH: AttachPayloadV1,
    BrokerOperation.RELEASE: ReleasePayloadV1,
    BrokerOperation.GATE_AUTHORIZE_EXEC: GateAuthorizeExecPayloadV1,
    BrokerOperation.INSPECT: InspectPayloadV1,
    BrokerOperation.AWAIT_DRAIN: AwaitDrainPayloadV1,
    BrokerOperation.STOP: StopPayloadV1,
    BrokerOperation.RECOVER_STOP: RecoverStopPayloadV1,
}


@dataclass(frozen=True, slots=True)
class BrokerRequestV1(ClosedModel):
    schema_name: Literal["mif-containment-broker-request"]
    schema_version: Literal[1]
    operation: BrokerOperation
    request_id: str
    payload: OperationPayloadV1

    def __post_init__(self) -> None:
        _require_literal(
            self.schema_name,
            "mif-containment-broker-request",
            "schema_name",
        )
        _require_exact_integer(self.schema_version, 1, "schema_version")
        if not isinstance(self.operation, BrokerOperation):
            raise ValueError("operation must be a BrokerOperation")
        _require_identifier(self.request_id, "request_id")
        expected_type = _PAYLOAD_TYPES[self.operation]
        if type(self.payload) is not expected_type:
            raise ValueError(
                f"payload for {self.operation.value!r} must be {expected_type.__name__}"
            )

    @classmethod
    def from_mapping(cls, value: object) -> "BrokerRequestV1":
        mapping = _closed_mapping(value, cls)
        operation = _deserialize(mapping["operation"], BrokerOperation, "operation")
        payload_type = _PAYLOAD_TYPES[operation]
        return cls(
            schema_name=_deserialize(
                mapping["schema_name"],
                Literal["mif-containment-broker-request"],
                "schema_name",
            ),
            schema_version=_deserialize(mapping["schema_version"], Literal[1], "schema_version"),
            operation=operation,
            request_id=_deserialize(mapping["request_id"], str, "request_id"),
            payload=payload_type.from_mapping(mapping["payload"]),
        )

    def canonical_bytes(self) -> bytes:
        return dumps_canonical(self.to_mapping())

    def classify_replay(
        self,
        candidate: "BrokerRequestV1",
        *,
        existing_peer_uid: int,
        candidate_peer_uid: int,
    ) -> ReplayClassification:
        _require_integer(existing_peer_uid, "existing_peer_uid", minimum=0)
        _require_integer(candidate_peer_uid, "candidate_peer_uid", minimum=0)
        existing_key = (
            existing_peer_uid,
            self._launch_nonce(),
            self.operation,
            self.request_id,
        )
        candidate_key = (
            candidate_peer_uid,
            candidate._launch_nonce(),
            candidate.operation,
            candidate.request_id,
        )
        if existing_key != candidate_key:
            if (
                self._launch_nonce() == candidate._launch_nonce()
                and self.operation == candidate.operation
                and self.request_id == candidate.request_id
            ):
                return ReplayClassification.CONFLICT
            return ReplayClassification.DISTINCT
        if self.canonical_bytes() == candidate.canonical_bytes():
            return ReplayClassification.IDEMPOTENT
        return ReplayClassification.CONFLICT

    def _launch_nonce(self) -> str | None:
        if type(self.payload) is GateAuthorizeExecPayloadV1:
            return self.payload.launch_nonce_sha256
        authority = getattr(self.payload, "authority", None)
        return None if authority is None else authority.launch_nonce_sha256


@dataclass(frozen=True, slots=True)
class LinuxSystemdScopeAttachmentV1(ClosedModel):
    provider: Literal["linux-systemd-scope-broker/v1"]
    protocol_version: Literal[1]
    broker_build_id: str
    broker_config_sha256: str
    boot_id: str
    peer_uid: int
    peer_gid: int
    unit_name: str
    invocation_id: str
    control_group: str
    scope_policy_sha256: str
    attachment_receipt_json_utf8: str
    attachment_receipt_sha256: str

    def __post_init__(self) -> None:
        _validate_provider_protocol(self.provider, self.protocol_version)
        _require_text(self.broker_build_id, "broker_build_id", maximum=128)
        _require_sha256(self.broker_config_sha256, "broker_config_sha256")
        _require_boot_id(self.boot_id)
        _require_integer(self.peer_uid, "peer_uid", minimum=0)
        _require_integer(self.peer_gid, "peer_gid", minimum=0)
        _require_unit_name(self.unit_name)
        _require_invocation_id(self.invocation_id)
        _require_control_group(self.control_group)
        _require_sha256(self.scope_policy_sha256, "scope_policy_sha256")
        attachment_record = _validate_typed_json_copy(
            self.attachment_receipt_json_utf8,
            self.attachment_receipt_sha256,
            label="attachment receipt",
            record_type=BrokerAttachmentRecordV1,
        )
        surfaced = {
            "protocol_version": self.protocol_version,
            "broker_build_id": self.broker_build_id,
            "broker_config_sha256": self.broker_config_sha256,
            "boot_id": self.boot_id,
            "peer_uid": self.peer_uid,
            "peer_gid": self.peer_gid,
            "unit_name": self.unit_name,
            "invocation_id": self.invocation_id,
            "control_group": self.control_group,
            "scope_policy_sha256": self.scope_policy_sha256,
        }
        for field_name, expected in surfaced.items():
            if getattr(attachment_record, field_name) != expected:
                raise ValueError(
                    f"attachment receipt {field_name} does not match surfaced evidence"
                )


_TERMINATION_REASONS = (
    "normal_exit",
    "nonzero_exit",
    "timeout",
    "cancelled",
    "launch_publication_failed",
    "recovery_stop",
    "reboot",
)
_NO_RELEASE_TERMINATION_REASONS = (*_STOP_REASONS, "reboot")


@dataclass(frozen=True, slots=True)
class LinuxSystemdScopeDrainV1(ClosedModel):
    provider: Literal["linux-systemd-scope-broker/v1"]
    semantic_state: Literal["drained"]
    attachment_receipt_sha256: str
    release_receipt_json_utf8: str | None
    release_receipt_sha256: str | None
    drain_receipt_json_utf8: str
    drain_receipt_sha256: str
    termination_reason: Literal[
        "normal_exit",
        "nonzero_exit",
        "timeout",
        "cancelled",
        "launch_publication_failed",
        "recovery_stop",
        "reboot",
    ]

    def __post_init__(self) -> None:
        """Validate the LinuxSystemdScopeDrainV1 fields when the record is constructed.

        The checks cover ``provider``, ``semantic_state``, ``attachment_receipt_sha256``,
        ``release_receipt_json_utf8``, ``drain_receipt_json_utf8``,
        ``drain_receipt_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        _require_literal(self.provider, PROVIDER, "provider")
        _require_literal(self.semantic_state, "drained", "semantic_state")
        _require_sha256(self.attachment_receipt_sha256, "attachment_receipt_sha256")
        if (self.release_receipt_json_utf8 is None) != (self.release_receipt_sha256 is None):
            raise ValueError("release receipt text and digest must be both present or both absent")
        release_record: BrokerReleaseRecordV1 | None = None
        if self.release_receipt_json_utf8 is not None:
            release_record = _validate_typed_json_copy(
                self.release_receipt_json_utf8,
                self.release_receipt_sha256,
                label="release receipt",
                record_type=BrokerReleaseRecordV1,
            )
        drain_record = _validate_typed_json_copy(
            self.drain_receipt_json_utf8,
            self.drain_receipt_sha256,
            label="drain receipt",
            record_type=BrokerDrainRecordV1,
        )
        if self.termination_reason not in _TERMINATION_REASONS:
            raise ValueError("termination_reason is not supported")
        if (
            self.release_receipt_json_utf8 is None
            and self.termination_reason not in _NO_RELEASE_TERMINATION_REASONS
        ):
            raise ValueError(
                "absent release receipt is valid only for a pre-release stop or reboot"
            )
        if drain_record.termination_reason != self.termination_reason:
            raise ValueError("drain receipt termination_reason does not match surfaced evidence")
        if release_record is None:
            if drain_record.release_sha256 is not None:
                raise ValueError("pre-release drain receipt must not name a release predecessor")
        else:
            _require_same_root_identity(
                release_record,
                drain_record,
                label="release and drain receipts",
            )
            if release_record.attachment_sha256 != drain_record.attachment_sha256:
                raise ValueError(
                    "release attachment predecessor does not match " "drain attachment predecessor"
                )
            if drain_record.release_sha256 != release_record.record_sha256:
                raise ValueError("drain release predecessor does not match release receipt")


@dataclass(frozen=True, slots=True)
class ContainmentInspection(ClosedModel):
    provider: Literal["linux-systemd-scope-broker/v1"]
    protocol_version: Literal[1]
    semantic_state: ContainmentSemanticState
    attachment: LinuxSystemdScopeAttachmentV1 | None
    drain: LinuxSystemdScopeDrainV1 | None

    def __post_init__(self) -> None:
        """Validate the ContainmentInspection fields when the record is constructed.

        The checks cover ``provider``, ``protocol_version``, ``semantic_state``, ``drain``,
        ``attachment``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        _validate_provider_protocol(self.provider, self.protocol_version)
        if type(self.semantic_state) is not ContainmentSemanticState:
            raise ValueError("semantic_state must be a ContainmentSemanticState")
        if (
            self.attachment is not None
            and type(self.attachment) is not LinuxSystemdScopeAttachmentV1
        ):
            raise ValueError(
                "attachment must be exact LinuxSystemdScopeAttachmentV1 evidence or null"
            )
        if self.drain is not None and type(self.drain) is not LinuxSystemdScopeDrainV1:
            raise ValueError("drain must be exact LinuxSystemdScopeDrainV1 evidence or null")
        if self.semantic_state is ContainmentSemanticState.LIVE:
            if self.attachment is None or self.drain is not None:
                raise ValueError("live inspection requires attachment and forbids drain")
        elif self.semantic_state is ContainmentSemanticState.DRAINED:
            if self.attachment is None or self.drain is None:
                raise ValueError("drained inspection requires attachment and drain")
        elif self.attachment is None or self.drain is not None:
            raise ValueError("ambiguous inspection requires attachment and forbids drain")
        if self.drain is not None:
            if self.attachment is None:
                raise ValueError("drain evidence requires attachment evidence")
            if self.drain.attachment_receipt_sha256 != self.attachment.attachment_receipt_sha256:
                raise ValueError("drain attachment digest does not match attachment evidence")
            attachment_record = _validate_typed_json_copy(
                self.attachment.attachment_receipt_json_utf8,
                self.attachment.attachment_receipt_sha256,
                label="attachment receipt",
                record_type=BrokerAttachmentRecordV1,
            )
            drain_record = _validate_typed_json_copy(
                self.drain.drain_receipt_json_utf8,
                self.drain.drain_receipt_sha256,
                label="drain receipt",
                record_type=BrokerDrainRecordV1,
            )
            _require_same_root_identity(
                attachment_record,
                drain_record,
                label="attachment and drain receipts",
            )
            if drain_record.attachment_sha256 != attachment_record.record_sha256:
                raise ValueError("drain attachment predecessor does not match attachment receipt")
            if self.drain.release_receipt_json_utf8 is not None:
                release_record = _validate_typed_json_copy(
                    self.drain.release_receipt_json_utf8,
                    self.drain.release_receipt_sha256,
                    label="release receipt",
                    record_type=BrokerReleaseRecordV1,
                )
                _require_same_root_identity(
                    attachment_record,
                    release_record,
                    label="attachment and release receipts",
                )
                if release_record.attachment_sha256 != attachment_record.record_sha256:
                    raise ValueError(
                        "release attachment predecessor does not match " "attachment receipt"
                    )


@dataclass(frozen=True, slots=True)
class _RootRecordV1(ClosedModel):
    schema_name: str
    schema_version: Literal[1]
    protocol_version: Literal[1]
    broker_build_id: str
    broker_config_sha256: str
    boot_id: str
    peer_uid: int
    peer_gid: int
    peer_pid: int
    peer_pid_start_token: str
    authority: AuthorityBindingV1
    executable_registration_id: str
    target_executable_sha256: str
    gate_pid: int
    gate_start_token: str
    anchor_identity: ScopeAnchorIdentityV1
    unit_name: str | None
    invocation_id: str | None
    control_group: str | None
    cgroup_identity: CgroupDirectoryIdentityV1 | None
    scope_policy_sha256: str
    timestamp_unix_ns: int
    record_sha256: str

    SCHEMA_NAME: ClassVar[str]

    def __post_init__(self) -> None:
        """Validate the _RootRecordV1 fields when the record is constructed.

        The checks cover ``schema_name``, ``SCHEMA_NAME``, ``schema_version``,
        ``protocol_version``, ``broker_build_id``, ``broker_config_sha256``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _require_literal(self.schema_name, self.SCHEMA_NAME, "schema_name")
        _require_exact_integer(self.schema_version, 1, "schema_version")
        _require_exact_integer(self.protocol_version, PROTOCOL_VERSION, "protocol_version")
        _require_text(self.broker_build_id, "broker_build_id", maximum=128)
        _require_sha256(self.broker_config_sha256, "broker_config_sha256")
        _require_boot_id(self.boot_id)
        _require_integer(self.peer_uid, "peer_uid", minimum=0)
        _require_integer(self.peer_gid, "peer_gid", minimum=0)
        _require_integer(self.peer_pid, "peer_pid", minimum=1)
        _require_start_token(self.peer_pid_start_token, "peer_pid_start_token")
        _require_identifier(
            self.executable_registration_id,
            "executable_registration_id",
        )
        _require_sha256(self.target_executable_sha256, "target_executable_sha256")
        _require_integer(self.gate_pid, "gate_pid", minimum=1)
        _require_start_token(self.gate_start_token, "gate_start_token")
        if not isinstance(self.anchor_identity, ScopeAnchorIdentityV1):
            raise ValueError("anchor_identity must be a ScopeAnchorIdentityV1")
        if self.anchor_identity.anchor_uid != self.peer_uid:
            raise ValueError("anchor_uid must equal the authenticated peer_uid")
        if self.anchor_identity.anchor_gid != self.peer_gid:
            raise ValueError("anchor_gid must equal the authenticated peer_gid")
        if self.unit_name is not None:
            _require_unit_name(self.unit_name)
        if self.invocation_id is not None:
            _require_invocation_id(self.invocation_id)
        if self.cgroup_identity is not None and not isinstance(
            self.cgroup_identity,
            CgroupDirectoryIdentityV1,
        ):
            raise ValueError("cgroup_identity must be a CgroupDirectoryIdentityV1 or null")
        _require_sha256(self.scope_policy_sha256, "scope_policy_sha256")
        _require_integer(self.timestamp_unix_ns, "timestamp_unix_ns", minimum=1)
        _require_sha256(self.record_sha256, "record_sha256")
        material = self.to_mapping()
        material.pop("record_sha256")
        if sha256_hex(dumps_canonical(material)) != self.record_sha256:
            raise ValueError("record digest does not match canonical record material")
        if len(dumps_canonical(self.to_mapping())) > _MAXIMUM_ROOT_RECORD_BYTES:
            raise ValueError("root record exceeds maximum byte length")


@dataclass(frozen=True, slots=True)
class BrokerAttemptRecordV1(_RootRecordV1):
    gate_ready: GateReadyV1

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-attempt"

    def __post_init__(self) -> None:
        _RootRecordV1.__post_init__(self)
        if any(
            value is not None
            for value in (
                self.unit_name,
                self.invocation_id,
                self.control_group,
                self.cgroup_identity,
            )
        ):
            raise ValueError("attempt scope identity fields must all be absent")


@dataclass(frozen=True, slots=True)
class BrokerAttachmentRecordV1(_RootRecordV1):
    attempt_sha256: str

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-attachment"

    def __post_init__(self) -> None:
        _RootRecordV1.__post_init__(self)
        _require_complete_scope_identity(self)
        _require_sha256(self.attempt_sha256, "attempt_sha256")


@dataclass(frozen=True, slots=True)
class BrokerReleaseIntentRecordV1(_RootRecordV1):
    attachment_sha256: str
    application_launch_sha256: str

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-release-intent"

    def __post_init__(self) -> None:
        _RootRecordV1.__post_init__(self)
        _require_complete_scope_identity(self)
        _require_sha256(self.attachment_sha256, "attachment_sha256")
        _require_sha256(self.application_launch_sha256, "application_launch_sha256")


@dataclass(frozen=True, slots=True)
class BrokerReleaseRecordV1(_RootRecordV1):
    attachment_sha256: str
    release_intent_sha256: str
    gate_authorization_sha256: str

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-release"

    def __post_init__(self) -> None:
        _RootRecordV1.__post_init__(self)
        _require_complete_scope_identity(self)
        _require_sha256(self.attachment_sha256, "attachment_sha256")
        _require_sha256(self.release_intent_sha256, "release_intent_sha256")
        _require_sha256(self.gate_authorization_sha256, "gate_authorization_sha256")


@dataclass(frozen=True, slots=True)
class BrokerStopIntentRecordV1(_RootRecordV1):
    attachment_sha256: str
    release_intent_sha256: str | None
    release_sha256: str | None
    termination_reason: Literal[
        "normal_exit",
        "nonzero_exit",
        "timeout",
        "cancelled",
        "launch_publication_failed",
        "recovery_stop",
    ]
    stop_request: Literal["systemd-stop-unit"]
    stop_job_mode: Literal["replace"]

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-stop-intent"

    def __post_init__(self) -> None:
        _RootRecordV1.__post_init__(self)
        _require_complete_scope_identity(self)
        _require_sha256(self.attachment_sha256, "attachment_sha256")
        if self.release_intent_sha256 is not None:
            _require_sha256(
                self.release_intent_sha256,
                "release_intent_sha256",
            )
        if self.release_sha256 is not None:
            _require_sha256(self.release_sha256, "release_sha256")
            if self.release_intent_sha256 is None:
                raise ValueError("release_sha256 requires release_intent_sha256")
        if self.termination_reason not in _TERMINATION_REASONS[:-1]:
            raise ValueError("termination_reason is not valid for stop intent")
        if self.termination_reason in ("normal_exit", "nonzero_exit") and (
            self.release_intent_sha256 is None or self.release_sha256 is None
        ):
            raise ValueError(
                "target-exit stop intent requires release-intent and release predecessors"
            )
        _require_literal(
            self.stop_request,
            "systemd-stop-unit",
            "stop_request",
        )
        _require_literal(self.stop_job_mode, "replace", "stop_job_mode")


@dataclass(frozen=True, slots=True)
class BrokerDrainRecordV1(_RootRecordV1):
    attachment_sha256: str
    release_sha256: str | None
    stop_intent_sha256: str | None
    termination_reason: Literal[
        "normal_exit",
        "nonzero_exit",
        "timeout",
        "cancelled",
        "launch_publication_failed",
        "recovery_stop",
        "reboot",
    ]
    stop_job: SystemdStopJobEvidenceV1 | None
    proof: BrokerDrainProofV1

    SCHEMA_NAME: ClassVar[str] = "mif-containment-broker-drain"

    def __post_init__(self) -> None:
        """Validate the BrokerDrainRecordV1 fields when the record is constructed.

        The checks cover ``attachment_sha256``, ``release_sha256``, ``termination_reason``,
        ``proof``, ``stop_intent_sha256``, ``stop_job``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _RootRecordV1.__post_init__(self)
        _require_complete_scope_identity(self)
        _require_sha256(self.attachment_sha256, "attachment_sha256")
        if self.release_sha256 is not None:
            _require_sha256(self.release_sha256, "release_sha256")
        if self.termination_reason not in _TERMINATION_REASONS:
            raise ValueError("termination_reason is not supported")
        if (
            self.termination_reason in ("normal_exit", "nonzero_exit")
            and self.release_sha256 is None
        ):
            raise ValueError("target-exit drain requires a release predecessor")
        if isinstance(self.proof, MachineRebootProofV1):
            if self.termination_reason != "reboot":
                raise ValueError("machine reboot proof requires reboot termination_reason")
            if self.stop_intent_sha256 is not None:
                raise ValueError("machine reboot proof forbids stop_intent_sha256")
            if self.stop_job is not None:
                raise ValueError("machine reboot proof forbids stop_job evidence")
            if self.proof.prior_boot_id != self.boot_id:
                raise ValueError("machine reboot prior_boot_id must equal record boot_id")
        elif isinstance(
            self.proof,
            (CgroupEventsZeroProofV1, CgroupDeletedAfterStopProofV1),
        ):
            if self.termination_reason == "reboot":
                raise ValueError("same-boot cgroup proof forbids reboot termination_reason")
            _require_sha256(self.stop_intent_sha256, "stop_intent_sha256")
            if self.proof.cgroup_identity != self.cgroup_identity:
                raise ValueError("drain proof cgroup identity differs from root record")
        else:
            raise ValueError("proof must be one closed BrokerDrainProofV1 variant")
        if self.stop_job is not None and not isinstance(
            self.stop_job,
            SystemdStopJobEvidenceV1,
        ):
            raise ValueError("stop_job must be exact systemd job evidence or null")


def _closed_mapping(value: object, model_type: type[ClosedModel]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{model_type.__name__} must be an object")
    expected = {item.name for item in fields(model_type)}
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ValueError(f"{model_type.__name__} has unknown fields: {sorted(unknown)!r}")
    if missing:
        raise ValueError(f"{model_type.__name__} is missing fields: {sorted(missing)!r}")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{model_type.__name__} field names must be strings")
    return value


def _serialize(value: object) -> object:
    if isinstance(value, ClosedModel):
        return value.to_mapping()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    return value


def _deserialize(value: object, expected: object, field_name: str) -> Any:
    origin = get_origin(expected)
    if origin is Literal:
        options = get_args(expected)
        if not any(type(value) is type(option) and value == option for option in options):
            raise ValueError(f"{field_name} must be one of {options!r}")
        return value
    if origin in (Union, types.UnionType):
        errors: list[ValueError] = []
        for option in get_args(expected):
            try:
                return _deserialize(value, option, field_name)
            except ValueError as exc:
                errors.append(exc)
        raise ValueError(f"{field_name} does not match any closed variant") from errors[-1]
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{field_name} must be an array")
        item_type, marker = get_args(expected)
        if marker is not Ellipsis:
            raise ValueError(f"{field_name} has unsupported tuple schema")
        return tuple(_deserialize(item, item_type, field_name) for item in value)
    if expected is type(None):
        if value is not None:
            raise ValueError(f"{field_name} must be null")
        return None
    if isinstance(expected, type) and issubclass(expected, ClosedModel):
        if isinstance(value, expected):
            return value
        return expected.from_mapping(value)
    if isinstance(expected, type) and issubclass(expected, Enum):
        if isinstance(value, expected):
            return value
        try:
            return expected(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} is not a supported {expected.__name__}") from exc
    if expected is int:
        if type(value) is not int:
            raise ValueError(f"{field_name} must be an integer")
        return value
    if expected is str:
        if type(value) is not str:
            raise ValueError(f"{field_name} must be a string")
        return value
    raise ValueError(f"{field_name} has an unsupported schema")


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _LOWER_SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be exactly 64 lowercase hexadecimal characters")


def _require_integer(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ValueError(f"{field_name} must be an integer {bounds}")


def _require_exact_integer(value: object, expected: int, field_name: str) -> None:
    if type(value) is not int or value != expected:
        raise ValueError(f"{field_name} must be the fixed integer {expected}")


def _require_literal(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{field_name} must be {expected!r}")


def _require_text(value: object, field_name: str, *, maximum: int) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ValueError(f"{field_name} must be non-empty bounded text without controls")


def _require_identifier(value: object, field_name: str) -> None:
    _require_text(value, field_name, maximum=128)
    if re.fullmatch(r"[A-Za-z0-9_.:@-]+", value) is None:
        raise ValueError(f"{field_name} contains unsafe characters")


def _require_start_token(value: object, field_name: str) -> None:
    if type(value) is not str or _TOKEN.fullmatch(value) is None or int(value) <= 0:
        raise ValueError(f"{field_name} must be a positive decimal process start token")


def _require_boot_id(value: object) -> None:
    if type(value) is not str or _BOOT_ID.fullmatch(value) is None:
        raise ValueError("boot_id must be a lowercase UUID")


def _require_invocation_id(value: object) -> None:
    if type(value) is not str or _INVOCATION_ID.fullmatch(value) is None:
        raise ValueError("invocation_id must be exactly 32 lowercase hexadecimal characters")


def _require_unit_name(value: object) -> None:
    if type(value) is not str or _UNIT_NAME.fullmatch(value) is None:
        raise ValueError("unit_name is not a safe broker-owned QuPath scope name")


def _require_control_group(value: object) -> None:
    if (
        type(value) is not str
        or not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or "\x00" in value
    ):
        raise ValueError("control_group is not a canonical absolute path")
    components = tuple(value[1:].split("/"))
    if any(
        item in {"", ".", ".."} or _CGROUP_BASENAME.fullmatch(item) is None for item in components
    ):
        raise ValueError("control_group contains an unsafe component")


def _require_canonical_absolute_path(value: object, field_name: str) -> None:
    try:
        encoded_length = len(value.encode("utf-8")) if type(value) is str else 0
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be a canonical absolute path") from exc
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or "\x00" in value
        or posixpath.normpath(value) != value
        or encoded_length > 4_096
    ):
        raise ValueError(f"{field_name} must be a canonical absolute path")


def _require_complete_scope_identity(value: _RootRecordV1) -> None:
    if any(
        item is None
        for item in (
            value.unit_name,
            value.invocation_id,
            value.control_group,
            value.cgroup_identity,
        )
    ):
        raise ValueError("post-attachment record requires a complete scope identity")
    _require_control_group(value.control_group)
    expected_components = tuple(value.control_group.removeprefix("/").split("/"))
    actual_components = tuple(component.basename for component in value.cgroup_identity.components)
    if actual_components != expected_components:
        raise ValueError("cgroup identity components must exactly match the ControlGroup path")


def _validate_provider_protocol(provider: object, protocol_version: object) -> None:
    _require_literal(provider, PROVIDER, "provider")
    _require_exact_integer(protocol_version, PROTOCOL_VERSION, "protocol_version")


def _require_same_root_identity(
    predecessor: _RootRecordV1,
    successor: _RootRecordV1,
    *,
    label: str,
) -> None:
    for field_name in (
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
        "unit_name",
        "invocation_id",
        "control_group",
        "cgroup_identity",
        "scope_policy_sha256",
    ):
        if getattr(predecessor, field_name) != getattr(successor, field_name):
            raise ValueError(f"{label} {field_name} bindings differ")


def _validate_json_copy(text: object, digest: object, *, label: str) -> Mapping[str, object]:
    if type(text) is not str:
        raise ValueError(f"{label} JSON copy must be text")
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} JSON copy must be valid UTF-8") from exc
    decoded = loads_canonical_packet(raw)
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{label} JSON copy must contain a canonical object")
    _require_sha256(digest, f"{label} digest")
    if sha256_hex(raw) != digest:
        raise ValueError(f"{label} digest does not match its canonical JSON copy")
    return decoded


def _validate_typed_json_copy(
    text: object,
    digest: object,
    *,
    label: str,
    record_type: type[_RootRecordV1],
) -> _RootRecordV1:
    decoded = _validate_json_copy(text, digest, label=label)
    try:
        return record_type.from_mapping(decoded)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid {record_type.__name__}: {exc}") from exc


def _validate_stop_timeouts(value: object) -> None:
    _require_integer(
        getattr(value, "graceful_timeout_milliseconds"),
        "graceful_timeout_milliseconds",
        minimum=1,
        maximum=3_600_000,
    )
    _require_integer(
        getattr(value, "forced_timeout_milliseconds"),
        "forced_timeout_milliseconds",
        minimum=1,
        maximum=3_600_000,
    )
