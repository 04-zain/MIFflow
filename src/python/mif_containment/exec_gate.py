"""Stopped same-user gate for descriptor-bound QuPath execution.

The `TargetFileIdentity` record carries `device`, `inode`, `mode`, `size`.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import array
import ctypes
import errno
import fcntl
import hashlib
import importlib
import os
import re
import signal
import socket
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .codec import dumps_canonical
from .protocol import (
    CONTROL_SOCKET_FD,
    MAXIMUM_ARGV_COUNT,
    MAXIMUM_ENVIRONMENT_BYTES,
    MAXIMUM_ENVIRONMENT_COUNT,
    RETAINED_TARGET_FD,
    GateExecAuthorizationV1,
    GateReadyV1,
)

PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38

GATE_INPUT_FAILURE = 64
GATE_UNAUTHORIZED = 65
GATE_EXEC_FAILURE = 66

MAXIMUM_PROC_STATUS_BYTES = 65_536
MAXIMUM_PROC_STAT_BYTES = 65_536
MAXIMUM_ARGUMENT_BYTES = 65_536
MAXIMUM_ARGV_BYTES = 1_048_576
MAXIMUM_PRESERVED_DESCRIPTORS = 64

_CAPABILITY_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
_AUTHORITY_CAPABILITY_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapAmb")
_STATUS_FIELDS = frozenset((*_CAPABILITY_FIELDS, "NoNewPrivs"))
_CAPABILITY_VALUE = re.compile(r"[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_MAXIMUM_PID = (1 << 31) - 1
_MAXIMUM_DESCRIPTOR = (1 << 20) - 1
_MAXIMUM_START_TOKEN = (1 << 64) - 1


# ==============================================================================
#                                   Exec gate
# ==============================================================================


class GateError(RuntimeError):
    """Base class for a closed execution-gate failure."""


class GateInputError(GateError):
    """Gate startup material or host evidence was invalid."""


class GateAuthorizationError(GateError):
    """The resumed gate did not receive exact broker authorization."""


class GateExecutionError(GateError):
    """A post-authorization session or descriptor execution failed."""


@dataclass(frozen=True, slots=True)
class TargetFileIdentity:
    """Identity facts retained across the target descriptor duplication."""

    device: int
    inode: int
    mode: int
    size: int


@dataclass(frozen=True, slots=True)
class _GateCommand:
    expected_parent_pid: int
    expected_parent_start_token: str
    launch_nonce_sha256: str
    preserved_descriptors: frozenset[int]
    target_argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CommandMaterial:
    argv_count: int
    argv_sha256: str
    environment: dict[str, str]
    environment_count: int
    environment_bytes: int
    environment_names_sizes_sha256: str
    cwd: str
    cwd_device: int
    cwd_inode: int


class GatePrimitives(Protocol):
    """Injectable boundary around every gate operating-system operation."""

    def getpid(self) -> int: ...

    def getppid(self) -> int: ...

    def process_start_token(self, pid: int) -> str: ...

    def security_status(self) -> Mapping[str, str]: ...

    def prctl(self, option: int, value: int) -> None: ...

    def get_argv(self) -> tuple[str, ...]: ...

    def get_environment(self) -> Mapping[str, str]: ...

    def get_cwd(self) -> str: ...

    def cwd_identity(self) -> tuple[int, int]: ...

    def open_executable_nofollow(self, path: str) -> int: ...

    def descriptor_identity(self, descriptor: int) -> TargetFileIdentity: ...

    def descriptor_sha256(self, descriptor: int) -> str: ...

    def duplicate_to(self, source: int, target: int) -> None: ...

    def close_fd(self, descriptor: int) -> None: ...

    def send_ready(self, descriptor: int, payload: bytes, target_fd: int) -> None: ...

    def stop_self(self) -> None: ...

    def setsid(self) -> None: ...

    def close_descriptors_except(self, preserved: frozenset[int]) -> None: ...

    def exec_descriptor(
        self,
        descriptor: int,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> None: ...


class GateBrokerAuthorizationPort(Protocol):
    """Gate-only authorization seam implemented by the Task 6 client."""

    def authorize_exec(
        self,
        *,
        launch_nonce_sha256: str,
        gate_material_sha256: str,
    ) -> GateExecAuthorizationV1: ...


def exec_gate_main(
    primitives: GatePrimitives | None = None,
    broker_client: GateBrokerAuthorizationPort | None = None,
) -> int:
    """Run the stopped gate and map its closed failure phases to exit codes."""

    operations = primitives if primitives is not None else PosixGatePrimitives()
    try:
        _run_gate(operations, broker_client)
    except GateInputError:
        return GATE_INPUT_FAILURE
    except GateAuthorizationError:
        return GATE_UNAUTHORIZED
    except GateExecutionError:
        return GATE_EXEC_FAILURE
    return 0


def _run_gate(
    operations: GatePrimitives,
    broker_client: GateBrokerAuthorizationPort | None,
) -> None:
    gate_pid = _input_call("read gate PID", operations.getpid)
    _require_positive_integer(gate_pid, "gate PID", maximum=_MAXIMUM_PID)
    gate_start_token = _input_call(
        "read gate start identity",
        operations.process_start_token,
        gate_pid,
    )
    _require_start_token(gate_start_token, "gate start token")

    _require_safe_security_status(
        _input_call("read initial process security status", operations.security_status),
        require_no_new_privileges=False,
    )
    _input_call(
        "set no-new-privileges",
        operations.prctl,
        PR_SET_NO_NEW_PRIVS,
        1,
    )
    _require_safe_security_status(
        _input_call("re-read process security status", operations.security_status),
        require_no_new_privileges=True,
    )

    raw_argv = _input_call("read fixed gate arguments", operations.get_argv)
    expected_parent = _parse_expected_parent(raw_argv)
    parent_before = _observe_parent_identity(operations)
    if parent_before != expected_parent:
        raise GateInputError("gate parent was absent or did not match startup framing")
    _input_call(
        "arm parent-death protection",
        operations.prctl,
        PR_SET_PDEATHSIG,
        signal.SIGKILL,
    )
    if _observe_parent_identity(operations) != expected_parent:
        raise GateInputError("gate parent identity changed after parent-death arming")

    command = _parse_command_line(raw_argv)
    material = _observe_command_material(operations, command)

    source_fd = _input_call(
        "open target executable without following its final component",
        operations.open_executable_nofollow,
        command.target_argv[0],
    )
    _require_descriptor(source_fd, "opened target descriptor")
    source_identity = _input_call(
        "observe opened target identity",
        operations.descriptor_identity,
        source_fd,
    )
    _require_executable_identity(source_identity)
    source_digest = _input_call(
        "hash opened target descriptor",
        operations.descriptor_sha256,
        source_fd,
    )
    _require_sha256(source_digest, "target executable digest")

    _input_call(
        "retain target descriptor",
        operations.duplicate_to,
        source_fd,
        RETAINED_TARGET_FD,
    )
    if source_fd != RETAINED_TARGET_FD:
        _input_call("close original target descriptor", operations.close_fd, source_fd)
    retained_identity = _input_call(
        "observe retained target identity",
        operations.descriptor_identity,
        RETAINED_TARGET_FD,
    )
    retained_digest = _input_call(
        "hash retained target descriptor",
        operations.descriptor_sha256,
        RETAINED_TARGET_FD,
    )
    if retained_identity != source_identity or retained_digest != source_digest:
        raise GateInputError("retained target descriptor identity or digest changed")

    ready = _build_gate_ready(
        command=command,
        material=material,
        gate_pid=gate_pid,
        gate_start_token=gate_start_token,
        target_identity=retained_identity,
        target_digest=retained_digest,
    )
    _input_call(
        "send gate readiness descriptor",
        operations.send_ready,
        CONTROL_SOCKET_FD,
        ready.canonical_json_utf8().encode("utf-8"),
        RETAINED_TARGET_FD,
    )
    _input_call("stop gate before authorization", operations.stop_self)

    client = broker_client
    if client is None:
        try:
            client_module = importlib.import_module("mif_containment.client")
            client = client_module.GateBrokerClient()
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            raise GateAuthorizationError("gate broker client is unavailable") from exc
    try:
        authorization = client.authorize_exec(
            launch_nonce_sha256=ready.launch_nonce_sha256,
            gate_material_sha256=ready.gate_material_sha256,
        )
    except Exception as exc:
        raise GateAuthorizationError("gate broker authorization failed") from exc
    _validate_authorization(authorization, ready)
    _revalidate_local_material(operations, command, material, ready)

    _execution_call(
        "clear parent-death protection",
        operations.prctl,
        PR_SET_PDEATHSIG,
        0,
    )
    _execution_call("create target session", operations.setsid)
    preserved = frozenset((0, 1, 2, RETAINED_TARGET_FD, *command.preserved_descriptors))
    _execution_call(
        "close gate-private descriptors",
        operations.close_descriptors_except,
        preserved,
    )
    _execution_call(
        "execute retained target descriptor",
        operations.exec_descriptor,
        RETAINED_TARGET_FD,
        command.target_argv,
        material.environment,
    )


def _parse_command_line(argv: object) -> _GateCommand:
    if type(argv) is not tuple or not argv or any(type(value) is not str for value in argv):
        raise GateInputError("gate argv must be an exact tuple of strings")
    if len(argv) < 9:
        raise GateInputError("gate fixed option framing is incomplete")
    if argv[1] != "--expected-parent-pid":
        raise GateInputError("gate expected-parent PID option is missing")
    expected_parent_pid = _parse_positive_decimal(
        argv[2],
        "expected parent PID",
        maximum=_MAXIMUM_PID,
    )
    if argv[3] != "--expected-parent-start-token":
        raise GateInputError("gate expected-parent start option is missing")
    expected_parent_start_token = argv[4]
    _require_start_token(expected_parent_start_token, "expected parent start token")
    if argv[5] != "--launch-nonce-sha256":
        raise GateInputError("gate launch nonce option is missing")
    launch_nonce_sha256 = argv[6]
    _require_sha256(launch_nonce_sha256, "launch nonce digest")

    preserved: list[int] = []
    index = 7
    while index < len(argv) and argv[index] == "--preserve-fd":
        if index + 1 >= len(argv):
            raise GateInputError("gate preserve descriptor option is incomplete")
        descriptor = _parse_positive_decimal(
            argv[index + 1],
            "preserved descriptor",
            maximum=_MAXIMUM_DESCRIPTOR,
        )
        if descriptor <= RETAINED_TARGET_FD:
            raise GateInputError("gate cannot preserve a reserved descriptor as a project lock")
        if descriptor in preserved:
            raise GateInputError("gate preserved descriptors must be unique")
        preserved.append(descriptor)
        if len(preserved) > MAXIMUM_PRESERVED_DESCRIPTORS:
            raise GateInputError("gate preserved descriptor count exceeds its bound")
        index += 2
    if index >= len(argv) or argv[index] != "--":
        raise GateInputError("gate target argv must follow a literal --")
    target_argv = argv[index + 1 :]
    _validate_target_argv(target_argv)
    return _GateCommand(
        expected_parent_pid=expected_parent_pid,
        expected_parent_start_token=expected_parent_start_token,
        launch_nonce_sha256=launch_nonce_sha256,
        preserved_descriptors=frozenset(preserved),
        target_argv=target_argv,
    )


def _parse_expected_parent(argv: object) -> tuple[int, str]:
    if (
        type(argv) is not tuple
        or len(argv) < 5
        or any(type(value) is not str for value in argv[:5])
        or argv[1] != "--expected-parent-pid"
        or argv[3] != "--expected-parent-start-token"
    ):
        raise GateInputError("gate expected-parent framing is incomplete")
    parent_pid = _parse_positive_decimal(
        argv[2],
        "expected parent PID",
        maximum=_MAXIMUM_PID,
    )
    parent_start = argv[4]
    _require_start_token(parent_start, "expected parent start token")
    return parent_pid, parent_start


def _validate_target_argv(argv: tuple[str, ...]) -> None:
    if not argv or len(argv) > MAXIMUM_ARGV_COUNT:
        raise GateInputError("target argv count is outside its fixed bound")
    total = 0
    for value in argv:
        if type(value) is not str or "\0" in value:
            raise GateInputError("target argv contains invalid text")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise GateInputError("target argv is not valid UTF-8") from exc
        if not encoded or len(encoded) > MAXIMUM_ARGUMENT_BYTES:
            raise GateInputError("target argument is empty or exceeds its fixed bound")
        total += len(encoded) + 1
        if total > MAXIMUM_ARGV_BYTES:
            raise GateInputError("target argv bytes exceed their fixed bound")
    if not os.path.isabs(argv[0]):
        raise GateInputError("target executable path must be absolute")


def _observe_command_material(
    operations: GatePrimitives,
    command: _GateCommand,
) -> _CommandMaterial:
    environment = _validate_environment(
        _input_call("read target environment", operations.get_environment)
    )
    cwd = _input_call("read target working directory", operations.get_cwd)
    if type(cwd) is not str or "\0" in cwd or not os.path.isabs(cwd):
        raise GateInputError("target working directory must be an absolute path")
    try:
        cwd.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise GateInputError("target working directory is not valid UTF-8") from exc
    cwd_identity = _input_call(
        "observe target working-directory identity",
        operations.cwd_identity,
    )
    cwd_device, cwd_inode = _validate_directory_identity(cwd_identity)
    environment_count, environment_bytes, environment_digest = _environment_facts(environment)
    return _CommandMaterial(
        argv_count=len(command.target_argv),
        argv_sha256=hashlib.sha256(dumps_canonical(list(command.target_argv))).hexdigest(),
        environment=environment,
        environment_count=environment_count,
        environment_bytes=environment_bytes,
        environment_names_sizes_sha256=environment_digest,
        cwd=cwd,
        cwd_device=cwd_device,
        cwd_inode=cwd_inode,
    )


def _validate_environment(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAXIMUM_ENVIRONMENT_COUNT:
        raise GateInputError("target environment count is outside its fixed bound")
    result: dict[str, str] = {}
    total = 0
    for name, item in value.items():
        if (
            type(name) is not str
            or type(item) is not str
            or not name
            or "=" in name
            or "\0" in name
            or "\0" in item
        ):
            raise GateInputError("target environment contains malformed text")
        try:
            encoded_name = name.encode("utf-8", errors="strict")
            encoded_value = item.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise GateInputError("target environment is not valid UTF-8") from exc
        total += len(encoded_name) + 1 + len(encoded_value) + 1
        if total > MAXIMUM_ENVIRONMENT_BYTES:
            raise GateInputError("target environment bytes exceed their fixed bound")
        result[name] = item
    return result


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


def _observe_parent_identity(operations: GatePrimitives) -> tuple[int, str]:
    parent_pid = _input_call("read gate parent PID", operations.getppid)
    if type(parent_pid) is not int or not 2 <= parent_pid <= _MAXIMUM_PID:
        raise GateInputError("gate parent is absent or outside the fixed PID range")
    parent_start = _input_call(
        "read gate parent start identity",
        operations.process_start_token,
        parent_pid,
    )
    _require_start_token(parent_start, "gate parent start token")
    return parent_pid, parent_start


def _build_gate_ready(
    *,
    command: _GateCommand,
    material: _CommandMaterial,
    gate_pid: int,
    gate_start_token: str,
    target_identity: TargetFileIdentity,
    target_digest: str,
) -> GateReadyV1:
    record_material = {
        "launch_nonce_sha256": command.launch_nonce_sha256,
        "gate_pid": gate_pid,
        "gate_start_token": gate_start_token,
        "expected_parent_pid": command.expected_parent_pid,
        "expected_parent_start_token": command.expected_parent_start_token,
        "control_socket_fd": CONTROL_SOCKET_FD,
        "retained_target_fd": RETAINED_TARGET_FD,
        "target_executable_device": target_identity.device,
        "target_executable_inode": target_identity.inode,
        "target_executable_sha256": target_digest,
        "argv_count": material.argv_count,
        "argv_sha256": material.argv_sha256,
        "environment_count": material.environment_count,
        "environment_bytes": material.environment_bytes,
        "environment_names_sizes_sha256": material.environment_names_sizes_sha256,
        "cwd_device": material.cwd_device,
        "cwd_inode": material.cwd_inode,
    }
    return GateReadyV1.from_mapping(
        {
            **record_material,
            "gate_material_sha256": hashlib.sha256(dumps_canonical(record_material)).hexdigest(),
        }
    )


def _validate_authorization(
    authorization: object,
    ready: GateReadyV1,
) -> None:
    if type(authorization) is not GateExecAuthorizationV1:
        raise GateAuthorizationError("broker returned the wrong authorization type")
    try:
        checked = GateExecAuthorizationV1.from_mapping(authorization.to_mapping())
    except (AttributeError, TypeError, ValueError) as exc:
        raise GateAuthorizationError("broker authorization self-digest is invalid") from exc
    expected = {
        "launch_nonce_sha256": ready.launch_nonce_sha256,
        "gate_pid": ready.gate_pid,
        "gate_start_token": ready.gate_start_token,
        "gate_material_sha256": ready.gate_material_sha256,
        "target_executable_sha256": ready.target_executable_sha256,
        "argv_sha256": ready.argv_sha256,
        "environment_names_sizes_sha256": ready.environment_names_sizes_sha256,
        "cwd_device": ready.cwd_device,
        "cwd_inode": ready.cwd_inode,
    }
    if any(getattr(checked, name) != value for name, value in expected.items()):
        raise GateAuthorizationError("broker authorization material does not match this gate")


def _revalidate_local_material(
    operations: GatePrimitives,
    command: _GateCommand,
    original: _CommandMaterial,
    ready: GateReadyV1,
) -> None:
    identity = _authorization_call(
        "re-observe retained target identity",
        operations.descriptor_identity,
        RETAINED_TARGET_FD,
    )
    digest = _authorization_call(
        "re-hash retained target descriptor",
        operations.descriptor_sha256,
        RETAINED_TARGET_FD,
    )
    try:
        _require_executable_identity(identity)
        _require_sha256(digest, "target executable digest")
    except GateInputError as exc:
        raise GateAuthorizationError("retained target evidence became malformed") from exc
    if (
        identity.device != ready.target_executable_device
        or identity.inode != ready.target_executable_inode
        or digest != ready.target_executable_sha256
    ):
        raise GateAuthorizationError("retained target changed while the gate was stopped")

    argv = _authorization_call("re-read fixed gate arguments", operations.get_argv)
    try:
        current_command = _parse_command_line(argv)
    except GateInputError as exc:
        raise GateAuthorizationError("gate framing changed while stopped") from exc
    if current_command != command:
        raise GateAuthorizationError("gate arguments changed while stopped")
    try:
        current = _observe_command_material(operations, current_command)
    except GateInputError as exc:
        raise GateAuthorizationError("gate material changed while stopped") from exc
    if current != original:
        raise GateAuthorizationError("gate material changed while stopped")


def _require_safe_security_status(
    status_value: object,
    *,
    require_no_new_privileges: bool,
) -> None:
    if not isinstance(status_value, Mapping) or set(status_value) != _STATUS_FIELDS:
        raise GateInputError("process security status fields are incomplete")
    for field_name in _CAPABILITY_FIELDS:
        value = status_value[field_name]
        if type(value) is not str or _CAPABILITY_VALUE.fullmatch(value) is None:
            raise GateInputError("process capability status is malformed")
    if any(
        status_value[field_name] != "0000000000000000"
        for field_name in _AUTHORITY_CAPABILITY_FIELDS
    ):
        raise GateInputError("gate requires empty authority-bearing capability sets")
    no_new_privileges = status_value["NoNewPrivs"]
    if no_new_privileges not in ("0", "1"):
        raise GateInputError("NoNewPrivs status is malformed")
    if require_no_new_privileges and no_new_privileges != "1":
        raise GateInputError("NoNewPrivileges was not applied")


def _require_executable_identity(value: object) -> None:
    if (
        type(value) is not TargetFileIdentity
        or type(value.device) is not int
        or value.device < 0
        or type(value.inode) is not int
        or value.inode < 1
        or type(value.mode) is not int
        or not stat.S_ISREG(value.mode)
        or value.mode & 0o111 == 0
        or type(value.size) is not int
        or value.size < 0
    ):
        raise GateInputError("target descriptor does not identify an executable regular file")


def _validate_directory_identity(value: object) -> tuple[int, int]:
    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not int
        or value[0] < 0
        or type(value[1]) is not int
        or value[1] < 1
    ):
        raise GateInputError("working-directory identity is malformed")
    return value


def _require_descriptor(value: object, label: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAXIMUM_DESCRIPTOR:
        raise GateInputError(f"{label} is outside the fixed range")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise GateInputError(f"{label} is malformed")


def _require_start_token(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) > len(str(_MAXIMUM_START_TOKEN))
        or _POSITIVE_DECIMAL.fullmatch(value) is None
    ):
        raise GateInputError(f"{label} is malformed")
    try:
        numeric = int(value, 10)
    except ValueError as exc:
        raise GateInputError(f"{label} is malformed") from exc
    if numeric > _MAXIMUM_START_TOKEN:
        raise GateInputError(f"{label} is outside its fixed range")


def _require_positive_integer(value: object, label: str, *, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise GateInputError(f"{label} is outside its fixed range")


def _parse_positive_decimal(value: object, label: str, *, maximum: int) -> int:
    if (
        type(value) is not str
        or len(value) > len(str(maximum))
        or _POSITIVE_DECIMAL.fullmatch(value) is None
    ):
        raise GateInputError(f"{label} is malformed")
    try:
        result = int(value, 10)
    except ValueError as exc:
        raise GateInputError(f"{label} is malformed") from exc
    if result > maximum:
        raise GateInputError(f"{label} is outside its fixed range")
    return result


def _input_call(label: str, function, *arguments):
    try:
        return function(*arguments)
    except GateInputError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise GateInputError(label) from exc


def _authorization_call(label: str, function, *arguments):
    try:
        return function(*arguments)
    except GateAuthorizationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise GateAuthorizationError(label) from exc


def _execution_call(label: str, function, *arguments):
    try:
        return function(*arguments)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GateExecutionError(label) from exc


class PosixGatePrimitives:
    """Production Linux primitive implementation used by the installed gate."""

    def __init__(self) -> None:
        self._libc: ctypes.CDLL | None = None

    def getpid(self) -> int:
        return os.getpid()

    def getppid(self) -> int:
        return os.getppid()

    def process_start_token(self, pid: int) -> str:
        content = self._read_bounded_proc_file(
            f"/proc/{pid}/stat",
            MAXIMUM_PROC_STAT_BYTES,
            "process stat",
        )
        try:
            text = content.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GateInputError("process stat is not ASCII") from exc
        closing = text.rfind(") ")
        if closing < 1 or text[:closing].split("(", 1)[0].strip() != str(pid):
            raise GateInputError("process stat is malformed")
        fields = text[closing + 2 :].split()
        if len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdigit():
            raise GateInputError("process start token is missing")
        start = int(fields[19], 10)
        if start < 1:
            raise GateInputError("process start token is invalid")
        return str(start)

    def security_status(self) -> Mapping[str, str]:
        content = self._read_bounded_proc_file(
            "/proc/self/status",
            MAXIMUM_PROC_STATUS_BYTES,
            "process status",
        )
        if len(content) > MAXIMUM_PROC_STATUS_BYTES:
            raise GateInputError("process status exceeds its fixed bound")
        try:
            text = content.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GateInputError("process status is not ASCII") from exc
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, raw_value = line.partition(":")
            if not separator or key not in _STATUS_FIELDS:
                continue
            if key in values:
                raise GateInputError("process status repeats a security field")
            value = raw_value.strip()
            if key in _CAPABILITY_FIELDS:
                if _CAPABILITY_VALUE.fullmatch(value) is None:
                    raise GateInputError("process status capability value is malformed")
            elif value not in ("0", "1"):
                raise GateInputError("process status NoNewPrivs value is malformed")
            values[key] = value
        if set(values) != _STATUS_FIELDS:
            raise GateInputError("process status security fields are incomplete")
        return values

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
        if self._libc.prctl(option, value, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    def get_argv(self) -> tuple[str, ...]:
        return tuple(sys.argv)

    def get_environment(self) -> Mapping[str, str]:
        return dict(os.environ)

    def get_cwd(self) -> str:
        return os.getcwd()

    def cwd_identity(self) -> tuple[int, int]:
        observed = os.stat(".", follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise GateInputError("current working directory is not a directory")
        return observed.st_dev, observed.st_ino

    def open_executable_nofollow(self, path: str) -> int:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        return os.open(path, flags)

    def descriptor_identity(self, descriptor: int) -> TargetFileIdentity:
        observed = os.fstat(descriptor)
        return TargetFileIdentity(
            device=observed.st_dev,
            inode=observed.st_ino,
            mode=observed.st_mode,
            size=observed.st_size,
        )

    def descriptor_sha256(self, descriptor: int) -> str:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, name) != getattr(after, name) for name in identity_fields):
            raise GateInputError("target descriptor changed while hashing")
        return digest.hexdigest()

    def duplicate_to(self, source: int, target: int) -> None:
        if source != target:
            os.dup2(source, target, inheritable=True)
        else:
            os.set_inheritable(target, True)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    def send_ready(self, descriptor: int, payload: bytes, target_fd: int) -> None:
        try:
            control = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_SEQPACKET,
                0,
                fileno=descriptor,
            )
        except OSError as exc:
            raise GateInputError("gate control descriptor is not a socket") from exc
        try:
            try:
                domain = control.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN)
                socket_type = control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                control.getpeername()
            except OSError as exc:
                raise GateInputError("gate control descriptor is not a connected socket") from exc
            if domain != socket.AF_UNIX or socket_type != socket.SOCK_SEQPACKET:
                raise GateInputError("gate control descriptor must be AF_UNIX SOCK_SEQPACKET")
            rights = array.array("i", (target_fd,))
            try:
                sent = control.sendmsg(
                    (payload,),
                    ((socket.SOL_SOCKET, socket.SCM_RIGHTS, rights),),
                )
            except OSError as exc:
                raise GateInputError("gate readiness packet could not be sent") from exc
            if sent != len(payload):
                raise GateInputError("gate readiness packet was not sent atomically")
        finally:
            control.detach()

    def stop_self(self) -> None:
        os.kill(os.getpid(), signal.SIGSTOP)

    def setsid(self) -> None:
        os.setsid()

    def close_descriptors_except(self, preserved: frozenset[int]) -> None:
        for name in os.listdir("/proc/self/fd"):
            if not name.isascii() or not name.isdigit():
                raise GateExecutionError("non-numeric entry in /proc/self/fd")
            descriptor = int(name, 10)
            if descriptor in preserved:
                continue
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    continue
                raise
            os.close(descriptor)

    def exec_descriptor(
        self,
        descriptor: int,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> None:
        if not os.supports_fd or os.execve not in os.supports_fd:
            raise NotImplementedError("descriptor execution is unsupported")
        os.set_inheritable(descriptor, True)
        os.execve(descriptor, argv, dict(environment))

    def _read_bounded_proc_file(self, path: str, maximum: int, label: str) -> bytes:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
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
            raise GateInputError(f"{label} exceeds its fixed bound")
        return bytes(content)


if __name__ == "__main__":
    raise SystemExit(exec_gate_main())
