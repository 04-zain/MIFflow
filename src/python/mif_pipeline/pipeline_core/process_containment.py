"""Neutral context for retaining authoritative subprocess lock resources.

The `ContainedProcessResult` record carries `args`, `returncode`, `launch_evidence`,
`drain_evidence`, `stdout_excerpt` and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
import re
from typing import (
    Callable,
    Iterator,
    Literal,
    Mapping,
    Protocol,
    TypeAlias,
    runtime_checkable,
)

from mif_containment.protocol import (
    AuthorityBindingV1,
    ContainmentInspection,
    LinuxSystemdScopeAttachmentV1,
    LinuxSystemdScopeDrainV1,
)

from .errors import ROIAnalysisError

_AUTHORITATIVE_LOCK_DESCRIPTORS: ContextVar[tuple[int, ...]] = ContextVar(
    "authoritative_lock_descriptors", default=()
)
_REGISTRATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_WINDOWS_JOB_NAME = re.compile(r"Local\\mif-pipeline-(?:warpy|qupath-project)-v1-[0-9a-f]{64}\Z")
_PROCESS_START_TOKEN = re.compile(r"[1-9][0-9]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDERS = (
    "linux-systemd-scope-broker/v1",
    "linux-local-process-group/v1",
    "windows-kill-on-close-job/v1",
)
_COMMAND_PROFILES = ("headless-script", "interactive-project")
_WINDOWS_TERMINATION_REASONS = (
    "normal_exit",
    "nonzero_exit",
    "timeout",
    "cancelled",
    "launch_publication_failed",
    "recovery_stop",
)
_LOCAL_PROCESS_GROUP_TERMINATION_REASONS = (
    "normal_exit",
    "nonzero_exit",
    "timeout",
    "cancelled",
    "launch_publication_failed",
)
_MAXIMUM_LOG_EXCERPT_BYTES = 65_536


# ==============================================================================
#                              Process containment
# ==============================================================================


def _closed_mapping(value: object, expected_fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError(f"{label} mapping is invalid")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{label} mapping keys are invalid")
    return value


def _require_literal(value: object, expected: object, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{label} must be {expected!r}")


def _require_windows_job_name(value: object) -> None:
    if type(value) is not str or _WINDOWS_JOB_NAME.fullmatch(value) is None:
        raise ValueError("Windows containment Job name is invalid")


def _require_log_leaf(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8", errors="strict")) > 255
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be one bounded log leaf name")


def _require_positive_integer(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class WindowsKillOnCloseJobAttachmentV1:
    provider: Literal["windows-kill-on-close-job/v1"]
    job_name: str
    child_session_id: int

    def __post_init__(self) -> None:
        _require_literal(
            self.provider,
            "windows-kill-on-close-job/v1",
            "provider",
        )
        _require_windows_job_name(self.job_name)
        if type(self.child_session_id) is not int or self.child_session_id < 0:
            raise ValueError("Windows child session ID is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "job_name": self.job_name,
            "child_session_id": self.child_session_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "WindowsKillOnCloseJobAttachmentV1":
        mapping = _closed_mapping(
            value,
            {"provider", "job_name", "child_session_id"},
            "Windows attachment",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class WindowsKillOnCloseJobDrainV1:
    provider: Literal["windows-kill-on-close-job/v1"]
    semantic_state: Literal["drained"]
    job_name: str
    termination_reason: Literal[
        "normal_exit",
        "nonzero_exit",
        "timeout",
        "cancelled",
        "launch_publication_failed",
        "recovery_stop",
    ]

    def __post_init__(self) -> None:
        _require_literal(
            self.provider,
            "windows-kill-on-close-job/v1",
            "provider",
        )
        _require_literal(self.semantic_state, "drained", "semantic_state")
        _require_windows_job_name(self.job_name)
        if type(self.termination_reason) is not str or (
            self.termination_reason not in _WINDOWS_TERMINATION_REASONS
        ):
            raise ValueError("Windows termination_reason is unsupported")

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "semantic_state": self.semantic_state,
            "job_name": self.job_name,
            "termination_reason": self.termination_reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "WindowsKillOnCloseJobDrainV1":
        mapping = _closed_mapping(
            value,
            {"provider", "semantic_state", "job_name", "termination_reason"},
            "Windows drain",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LinuxLocalProcessGroupAttachmentV1:
    """Exact local-session identity; it makes no broker or cgroup claim."""

    provider: Literal["linux-local-process-group/v1"]
    leader_process_id: int
    leader_start_epoch_millis: int
    process_group_id: int

    def __post_init__(self) -> None:
        _require_literal(
            self.provider,
            "linux-local-process-group/v1",
            "provider",
        )
        _require_positive_integer(self.leader_process_id, "leader_process_id")
        _require_positive_integer(self.leader_start_epoch_millis, "leader_start_epoch_millis")
        _require_positive_integer(self.process_group_id, "process_group_id")
        if self.process_group_id != self.leader_process_id:
            raise ValueError("local process group must be led by its leader process")

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "leader_process_id": self.leader_process_id,
            "leader_start_epoch_millis": self.leader_start_epoch_millis,
            "process_group_id": self.process_group_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "LinuxLocalProcessGroupAttachmentV1":
        mapping = _closed_mapping(
            value,
            {
                "provider",
                "leader_process_id",
                "leader_start_epoch_millis",
                "process_group_id",
            },
            "local process-group attachment",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LinuxLocalProcessGroupDrainV1:
    """Within-session local-process-group drain evidence without recovery claims."""

    provider: Literal["linux-local-process-group/v1"]
    semantic_state: Literal["drained"]
    leader_process_id: int
    leader_start_epoch_millis: int
    process_group_id: int
    termination_reason: Literal[
        "normal_exit",
        "nonzero_exit",
        "timeout",
        "cancelled",
        "launch_publication_failed",
    ]

    def __post_init__(self) -> None:
        _require_literal(
            self.provider,
            "linux-local-process-group/v1",
            "provider",
        )
        _require_literal(self.semantic_state, "drained", "semantic_state")
        _require_positive_integer(self.leader_process_id, "leader_process_id")
        _require_positive_integer(self.leader_start_epoch_millis, "leader_start_epoch_millis")
        _require_positive_integer(self.process_group_id, "process_group_id")
        if self.process_group_id != self.leader_process_id:
            raise ValueError("local process group must be led by its leader process")
        if (
            type(self.termination_reason) is not str
            or self.termination_reason not in _LOCAL_PROCESS_GROUP_TERMINATION_REASONS
        ):
            raise ValueError("local process-group termination_reason is unsupported")

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "semantic_state": self.semantic_state,
            "leader_process_id": self.leader_process_id,
            "leader_start_epoch_millis": self.leader_start_epoch_millis,
            "process_group_id": self.process_group_id,
            "termination_reason": self.termination_reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "LinuxLocalProcessGroupDrainV1":
        mapping = _closed_mapping(
            value,
            {
                "provider",
                "semantic_state",
                "leader_process_id",
                "leader_start_epoch_millis",
                "process_group_id",
                "termination_reason",
            },
            "local process-group drain",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


ProcessContainmentLaunchEvidence: TypeAlias = (
    LinuxSystemdScopeAttachmentV1
    | LinuxLocalProcessGroupAttachmentV1
    | WindowsKillOnCloseJobAttachmentV1
)
ProcessContainmentDrainEvidence: TypeAlias = (
    LinuxSystemdScopeDrainV1 | LinuxLocalProcessGroupDrainV1 | WindowsKillOnCloseJobDrainV1
)


def _launch_evidence_from_mapping(value: object) -> ProcessContainmentLaunchEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("containment launch evidence mapping is invalid")
    provider = value.get("provider")
    if provider == "linux-systemd-scope-broker/v1":
        return LinuxSystemdScopeAttachmentV1.from_mapping(value)
    if provider == "linux-local-process-group/v1":
        return LinuxLocalProcessGroupAttachmentV1.from_mapping(value)
    if provider == "windows-kill-on-close-job/v1":
        return WindowsKillOnCloseJobAttachmentV1.from_mapping(value)
    raise ValueError("containment launch evidence provider is unsupported")


def _drain_evidence_from_mapping(value: object) -> ProcessContainmentDrainEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("containment drain evidence mapping is invalid")
    provider = value.get("provider")
    if provider == "linux-systemd-scope-broker/v1":
        return LinuxSystemdScopeDrainV1.from_mapping(value)
    if provider == "linux-local-process-group/v1":
        return LinuxLocalProcessGroupDrainV1.from_mapping(value)
    if provider == "windows-kill-on-close-job/v1":
        return WindowsKillOnCloseJobDrainV1.from_mapping(value)
    raise ValueError("containment drain evidence provider is unsupported")


@dataclass(frozen=True, slots=True)
class ContainmentLaunchRequest:
    authority: AuthorityBindingV1
    provider: Literal[
        "linux-systemd-scope-broker/v1",
        "linux-local-process-group/v1",
        "windows-kill-on-close-job/v1",
    ]
    registration_id: str | None
    command_profile: Literal["headless-script", "interactive-project"]
    windows_job_name: str | None
    stdout_log_name: str
    stderr_log_name: str

    def __post_init__(self) -> None:
        if type(self.authority) is not AuthorityBindingV1:
            raise ValueError("containment authority is invalid")
        if type(self.provider) is not str or self.provider not in _PROVIDERS:
            raise ValueError("containment provider is unsupported")
        if type(self.command_profile) is not str or (self.command_profile not in _COMMAND_PROFILES):
            raise ValueError("containment command profile is unsupported")
        _require_log_leaf(self.stdout_log_name, "stdout_log_name")
        _require_log_leaf(self.stderr_log_name, "stderr_log_name")
        if self.stdout_log_name == self.stderr_log_name:
            raise ValueError("containment log leaf names must be distinct")
        if self.provider == "linux-systemd-scope-broker/v1":
            if (
                type(self.registration_id) is not str
                or _REGISTRATION_ID.fullmatch(self.registration_id) is None
                or self.windows_job_name is not None
            ):
                raise ValueError(
                    "Linux containment requires registration_id and forbids a Windows Job"
                )
        elif self.provider == "linux-local-process-group/v1":
            if self.registration_id is not None or self.windows_job_name is not None:
                raise ValueError(
                    "local Linux containment forbids broker registration and Windows Job"
                )
        elif self.registration_id is not None or self.windows_job_name is None:
            raise ValueError("Windows containment requires a Job and forbids broker registration")
        else:
            _require_windows_job_name(self.windows_job_name)

    def to_mapping(self) -> dict[str, object]:
        return {
            "authority": self.authority.to_mapping(),
            "provider": self.provider,
            "registration_id": self.registration_id,
            "command_profile": self.command_profile,
            "windows_job_name": self.windows_job_name,
            "stdout_log_name": self.stdout_log_name,
            "stderr_log_name": self.stderr_log_name,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainmentLaunchRequest":
        mapping = _closed_mapping(
            value,
            {
                "authority",
                "provider",
                "registration_id",
                "command_profile",
                "windows_job_name",
                "stdout_log_name",
                "stderr_log_name",
            },
            "containment launch request",
        )
        values = dict(mapping)
        values["authority"] = AuthorityBindingV1.from_mapping(values["authority"])
        return cls(**values)  # type: ignore[arg-type]


def _evidence_pair_matches(
    launch_evidence: ProcessContainmentLaunchEvidence,
    drain_evidence: ProcessContainmentDrainEvidence,
) -> bool:
    if type(launch_evidence) is WindowsKillOnCloseJobAttachmentV1:
        return (
            type(drain_evidence) is WindowsKillOnCloseJobDrainV1
            and launch_evidence.job_name == drain_evidence.job_name
        )
    if type(launch_evidence) is LinuxSystemdScopeAttachmentV1:
        return (
            type(drain_evidence) is LinuxSystemdScopeDrainV1
            and launch_evidence.attachment_receipt_sha256
            == drain_evidence.attachment_receipt_sha256
        )
    return (
        type(launch_evidence) is LinuxLocalProcessGroupAttachmentV1
        and type(drain_evidence) is LinuxLocalProcessGroupDrainV1
        and launch_evidence.leader_process_id == drain_evidence.leader_process_id
        and launch_evidence.leader_start_epoch_millis == drain_evidence.leader_start_epoch_millis
        and launch_evidence.process_group_id == drain_evidence.process_group_id
    )


@dataclass(frozen=True, slots=True)
class ContainedMutationLaunchBinding:
    """Identity supplied before a mutation process is spawned."""

    request_sha256: str
    operation: Literal["commit", "cleanup"]
    project_identity_sha256: str
    mutation_run_sha256: str
    hierarchy_scope_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.request_sha256, "request_sha256")
        if type(self.operation) is not str or self.operation not in {
            "commit",
            "cleanup",
        }:
            raise ValueError("contained mutation operation is unsupported")
        _require_sha256(self.project_identity_sha256, "project_identity_sha256")
        _require_sha256(self.mutation_run_sha256, "mutation_run_sha256")
        _require_sha256(self.hierarchy_scope_sha256, "hierarchy_scope_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "request_sha256": self.request_sha256,
            "operation": self.operation,
            "project_identity_sha256": self.project_identity_sha256,
            "mutation_run_sha256": self.mutation_run_sha256,
            "hierarchy_scope_sha256": self.hierarchy_scope_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainedMutationLaunchBinding":
        mapping = _closed_mapping(
            value,
            {
                "request_sha256",
                "operation",
                "project_identity_sha256",
                "mutation_run_sha256",
                "hierarchy_scope_sha256",
            },
            "contained mutation launch binding",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ContainedMutationSpawnAttempt:
    """The one exact binding that authorized a spawn attempt."""

    binding: ContainedMutationLaunchBinding

    def __post_init__(self) -> None:
        if type(self.binding) is not ContainedMutationLaunchBinding:
            raise ValueError("contained mutation spawn binding is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {"binding": self.binding.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> "ContainedMutationSpawnAttempt":
        mapping = _closed_mapping(value, {"binding"}, "contained mutation spawn attempt")
        return cls(binding=ContainedMutationLaunchBinding.from_mapping(mapping["binding"]))


@dataclass(frozen=True, slots=True)
class ContainedMutationLaunchRecord:
    """Published launch evidence paired with its one spawn attempt."""

    spawn_attempt: ContainedMutationSpawnAttempt
    evidence: ProcessContainmentLaunchEvidence

    def __post_init__(self) -> None:
        if type(self.spawn_attempt) is not ContainedMutationSpawnAttempt:
            raise ValueError("contained mutation launch spawn attempt is invalid")
        if type(self.evidence) not in (
            LinuxSystemdScopeAttachmentV1,
            LinuxLocalProcessGroupAttachmentV1,
            WindowsKillOnCloseJobAttachmentV1,
        ):
            raise ValueError("contained mutation launch evidence is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "spawn_attempt": self.spawn_attempt.to_mapping(),
            "evidence": self.evidence.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainedMutationLaunchRecord":
        mapping = _closed_mapping(
            value,
            {"spawn_attempt", "evidence"},
            "contained mutation launch record",
        )
        return cls(
            spawn_attempt=ContainedMutationSpawnAttempt.from_mapping(mapping["spawn_attempt"]),
            evidence=_launch_evidence_from_mapping(mapping["evidence"]),
        )


_LAUNCH_PUBLICATION_STATES = (
    "published",
    "publication_failed_before_launch_record",
)


@dataclass(frozen=True, slots=True)
class ContainedMutationDrainRecord:
    """Terminal evidence tied to publication, or its exact failed predecessor."""

    launch_or_attempt: ContainedMutationLaunchRecord | ContainedMutationSpawnAttempt
    evidence: ProcessContainmentDrainEvidence
    launch_publication_state: Literal["published", "publication_failed_before_launch_record"]

    def __post_init__(self) -> None:
        if type(self.evidence) not in (
            LinuxSystemdScopeDrainV1,
            LinuxLocalProcessGroupDrainV1,
            WindowsKillOnCloseJobDrainV1,
        ):
            raise ValueError("contained mutation drain evidence is invalid")
        if (
            type(self.launch_publication_state) is not str
            or self.launch_publication_state not in _LAUNCH_PUBLICATION_STATES
        ):
            raise ValueError("contained mutation launch publication state is invalid")
        if self.launch_publication_state == "published":
            if type(self.launch_or_attempt) is not ContainedMutationLaunchRecord:
                raise ValueError("published drain must retain its launch record")
            if self.evidence.termination_reason == "launch_publication_failed":
                raise ValueError("published launch cannot fail launch publication")
            if not _evidence_pair_matches(self.launch_or_attempt.evidence, self.evidence):
                raise ValueError("contained mutation published evidence differs")
        else:
            if type(self.launch_or_attempt) is not ContainedMutationSpawnAttempt:
                raise ValueError("unpublished drain must retain its spawn attempt")
            if self.evidence.termination_reason != "launch_publication_failed":
                raise ValueError("unpublished drain requires publication failure")

    def to_mapping(self) -> dict[str, object]:
        if type(self.launch_or_attempt) is ContainedMutationLaunchRecord:
            predecessor = {
                "kind": "launch_record",
                "record": self.launch_or_attempt.to_mapping(),
            }
        else:
            predecessor = {
                "kind": "spawn_attempt",
                "record": self.launch_or_attempt.to_mapping(),
            }
        return {
            "predecessor": predecessor,
            "evidence": self.evidence.to_mapping(),
            "launch_publication_state": self.launch_publication_state,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainedMutationDrainRecord":
        mapping = _closed_mapping(
            value,
            {"predecessor", "evidence", "launch_publication_state"},
            "contained mutation drain record",
        )
        predecessor = _closed_mapping(
            mapping["predecessor"],
            {"kind", "record"},
            "contained mutation drain predecessor",
        )
        if predecessor["kind"] == "launch_record":
            launch_or_attempt: ContainedMutationLaunchRecord | ContainedMutationSpawnAttempt = (
                ContainedMutationLaunchRecord.from_mapping(predecessor["record"])
            )
        elif predecessor["kind"] == "spawn_attempt":
            launch_or_attempt = ContainedMutationSpawnAttempt.from_mapping(predecessor["record"])
        else:
            raise ValueError("contained mutation drain predecessor is unsupported")
        return cls(
            launch_or_attempt=launch_or_attempt,
            evidence=_drain_evidence_from_mapping(mapping["evidence"]),
            launch_publication_state=mapping["launch_publication_state"],  # type: ignore[arg-type]
        )


@runtime_checkable
class ContainedMutationRecorder(Protocol):
    """Dependency-neutral recorder seam shared by QuPath and registration."""

    def launch_binding(
        self,
        *,
        request_sha256: str,
        operation: Literal["commit", "cleanup"],
        project_identity_sha256: str,
        mutation_run_sha256: str,
        hierarchy_scope_sha256: str,
    ) -> ContainedMutationLaunchBinding: ...

    def record_spawn_attempt(
        self,
        binding: ContainedMutationLaunchBinding,
    ) -> ContainedMutationSpawnAttempt: ...

    def record_launch(
        self,
        attempt: ContainedMutationSpawnAttempt,
        evidence: ProcessContainmentLaunchEvidence,
    ) -> ContainedMutationLaunchRecord: ...

    def record_drain(
        self,
        launch_or_attempt: ContainedMutationLaunchRecord | ContainedMutationSpawnAttempt,
        evidence: ProcessContainmentDrainEvidence,
    ) -> ContainedMutationDrainRecord: ...


@dataclass(frozen=True, slots=True)
class ContainedProcessResult:
    args: tuple[str, ...]
    returncode: int
    launch_evidence: ProcessContainmentLaunchEvidence
    drain_evidence: ProcessContainmentDrainEvidence
    stdout_excerpt: str
    stderr_excerpt: str
    stdout_truncated: bool
    stderr_truncated: bool

    def __post_init__(self) -> None:
        if (
            type(self.args) is not tuple
            or not self.args
            or any(type(item) is not str or not item or "\x00" in item for item in self.args)
        ):
            raise ValueError("contained process arguments are invalid")
        if type(self.returncode) is not int:
            raise ValueError("contained process returncode is invalid")
        if type(self.launch_evidence) not in (
            LinuxSystemdScopeAttachmentV1,
            LinuxLocalProcessGroupAttachmentV1,
            WindowsKillOnCloseJobAttachmentV1,
        ) or type(self.drain_evidence) not in (
            LinuxSystemdScopeDrainV1,
            LinuxLocalProcessGroupDrainV1,
            WindowsKillOnCloseJobDrainV1,
        ):
            raise ValueError("contained process evidence is invalid")
        if self.launch_evidence.provider != self.drain_evidence.provider:
            raise ValueError("contained process evidence providers differ")
        if not _evidence_pair_matches(self.launch_evidence, self.drain_evidence):
            raise ValueError("contained process launch and drain evidence differ")
        if type(self.stdout_excerpt) is not str or type(self.stderr_excerpt) is not str:
            raise ValueError("contained process log excerpts are invalid")
        if type(self.stdout_truncated) is not bool or type(self.stderr_truncated) is not bool:
            raise ValueError("contained process truncation flags are invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "args": list(self.args),
            "returncode": self.returncode,
            "launch_evidence": self.launch_evidence.to_mapping(),
            "drain_evidence": self.drain_evidence.to_mapping(),
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainedProcessResult":
        mapping = _closed_mapping(
            value,
            {
                "args",
                "returncode",
                "launch_evidence",
                "drain_evidence",
                "stdout_excerpt",
                "stderr_excerpt",
                "stdout_truncated",
                "stderr_truncated",
            },
            "contained process result",
        )
        values = dict(mapping)
        args = values["args"]
        if type(args) is not list:
            raise ValueError("contained process args mapping must be an array")
        values["args"] = tuple(args)
        values["launch_evidence"] = _launch_evidence_from_mapping(values["launch_evidence"])
        values["drain_evidence"] = _drain_evidence_from_mapping(values["drain_evidence"])
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _ContainedProcessFailure:
    """Private exact facts retained when a containment backend cannot return."""

    process_id: int | None
    start_epoch_millis: int | None
    launch_evidence: ProcessContainmentLaunchEvidence | None
    drain_evidence: ProcessContainmentDrainEvidence | None
    returncode: int | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    stdout_truncated: bool | None = None
    stderr_truncated: bool | None = None

    def __post_init__(self) -> None:
        """Validate the _ContainedProcessFailure fields when the record is constructed.

        The checks cover ``drain_evidence``, ``stdout_excerpt``, ``stderr_excerpt``,
        ``stdout_truncated``, ``stderr_truncated``, ``process_id``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if (self.process_id is None) != (self.start_epoch_millis is None):
            raise ValueError("contained process identity must be complete or absent")
        if self.process_id is not None and (
            type(self.process_id) is not int
            or self.process_id <= 0
            or type(self.start_epoch_millis) is not int
            or self.start_epoch_millis <= 0
        ):
            raise ValueError("contained process identity is invalid")
        if self.launch_evidence is not None and type(self.launch_evidence) not in (
            LinuxSystemdScopeAttachmentV1,
            LinuxLocalProcessGroupAttachmentV1,
            WindowsKillOnCloseJobAttachmentV1,
        ):
            raise ValueError("contained process failure launch evidence is invalid")
        if type(self.launch_evidence) is LinuxLocalProcessGroupAttachmentV1 and (
            self.process_id != self.launch_evidence.leader_process_id
            or self.start_epoch_millis != self.launch_evidence.leader_start_epoch_millis
        ):
            raise ValueError("local contained process failure identity differs")
        if self.drain_evidence is not None:
            if (
                type(self.drain_evidence)
                not in (
                    LinuxSystemdScopeDrainV1,
                    LinuxLocalProcessGroupDrainV1,
                    WindowsKillOnCloseJobDrainV1,
                )
                or self.launch_evidence is None
            ):
                raise ValueError("contained process failure drain evidence is invalid")
            if self.launch_evidence.provider != self.drain_evidence.provider:
                raise ValueError("contained process failure evidence providers differ")
            if not _evidence_pair_matches(self.launch_evidence, self.drain_evidence):
                raise ValueError("contained process failure evidence differs")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("contained process failure returncode is invalid")
        values = (
            self.stdout_excerpt,
            self.stderr_excerpt,
            self.stdout_truncated,
            self.stderr_truncated,
        )
        if any(value is not None for value in values) and (
            type(self.stdout_excerpt) is not str
            or type(self.stderr_excerpt) is not str
            or type(self.stdout_truncated) is not bool
            or type(self.stderr_truncated) is not bool
        ):
            raise ValueError("contained process failure log facts are incomplete")


class _ContainedProcessError(ROIAnalysisError):
    """Private backend error that never converts uncertainty into a result."""

    __slots__ = ("_failure",)

    def __init__(self, message: str, failure: _ContainedProcessFailure) -> None:
        super().__init__(message)
        if type(failure) is not _ContainedProcessFailure:
            raise TypeError("failure must be exact contained process evidence")
        self._failure = failure

    @property
    def failure(self) -> _ContainedProcessFailure:
        return self._failure


@dataclass(frozen=True, slots=True)
class ContainmentRecoveryRequest:
    authority: AuthorityBindingV1
    launch_evidence: ProcessContainmentLaunchEvidence
    contained_process_id: int
    contained_process_start_epoch_millis: int | None
    contained_process_start_ticks: int | None
    original_owner_process_id: int
    original_owner_process_start_token: str

    def __post_init__(self) -> None:
        """Validate the ContainmentRecoveryRequest fields when the record is constructed.

        The checks cover ``authority``, ``launch_evidence``, ``contained_process_id``,
        ``original_owner_process_id``, ``contained_process_start_epoch_millis``,
        ``contained_process_start_ticks``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if type(self.authority) is not AuthorityBindingV1:
            raise ValueError("containment recovery authority is invalid")
        if type(self.launch_evidence) not in (
            LinuxSystemdScopeAttachmentV1,
            WindowsKillOnCloseJobAttachmentV1,
        ):
            raise ValueError("containment recovery launch evidence is invalid")
        for label, value in (
            ("contained_process_id", self.contained_process_id),
            ("original_owner_process_id", self.original_owner_process_id),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if type(self.launch_evidence) is LinuxSystemdScopeAttachmentV1:
            if (
                self.contained_process_start_epoch_millis is not None
                or type(self.contained_process_start_ticks) is not int
                or self.contained_process_start_ticks <= 0
            ):
                raise ValueError("Linux recovery process start ticks are invalid")
            attachment = json.loads(self.launch_evidence.attachment_receipt_json_utf8)
            if attachment.get("gate_pid") != self.contained_process_id or attachment.get(
                "gate_start_token"
            ) != str(self.contained_process_start_ticks):
                raise ValueError("Linux recovery process identity differs from attachment")
        elif (
            type(self.contained_process_start_epoch_millis) is not int
            or self.contained_process_start_epoch_millis <= 0
            or self.contained_process_start_ticks is not None
        ):
            raise ValueError("Windows recovery process creation time is invalid")
        if (
            type(self.original_owner_process_start_token) is not str
            or _PROCESS_START_TOKEN.fullmatch(self.original_owner_process_start_token) is None
        ):
            raise ValueError("original owner process start token is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "authority": self.authority.to_mapping(),
            "launch_evidence": self.launch_evidence.to_mapping(),
            "contained_process_id": self.contained_process_id,
            "contained_process_start_epoch_millis": (self.contained_process_start_epoch_millis),
            "contained_process_start_ticks": self.contained_process_start_ticks,
            "original_owner_process_id": self.original_owner_process_id,
            "original_owner_process_start_token": (self.original_owner_process_start_token),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ContainmentRecoveryRequest":
        mapping = _closed_mapping(
            value,
            {
                "authority",
                "launch_evidence",
                "contained_process_id",
                "contained_process_start_epoch_millis",
                "contained_process_start_ticks",
                "original_owner_process_id",
                "original_owner_process_start_token",
            },
            "containment recovery request",
        )
        values = dict(mapping)
        values["authority"] = AuthorityBindingV1.from_mapping(values["authority"])
        values["launch_evidence"] = _launch_evidence_from_mapping(values["launch_evidence"])
        return cls(**values)  # type: ignore[arg-type]


ProcessStartObserver: TypeAlias = Callable[[int, int, ProcessContainmentLaunchEvidence], str]
ProcessAttemptObserver: TypeAlias = Callable[[], None]
ProcessDrainObserver: TypeAlias = Callable[[ProcessContainmentDrainEvidence], None]


class ProcessContainmentRecovery(Protocol):
    def inspect(self, request: ContainmentRecoveryRequest) -> ContainmentInspection: ...

    def recover_stop(
        self, request: ContainmentRecoveryRequest
    ) -> ProcessContainmentDrainEvidence: ...


def _bounded_log_excerpt(
    stream: object, *, maximum_bytes: int = _MAXIMUM_LOG_EXCERPT_BYTES
) -> tuple[str, bool]:
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum log excerpt size is invalid")
    original_offset: int | None = None
    failure: BaseException | None = None
    raw: object = None
    size: object = None
    try:
        stream.flush()  # type: ignore[attr-defined]
        original_offset = stream.tell()  # type: ignore[attr-defined]
        stream.seek(0, os.SEEK_END)  # type: ignore[attr-defined]
        size = stream.tell()  # type: ignore[attr-defined]
        if type(size) is not int or size < 0:
            raise ROIAnalysisError("contained process log stream is invalid")
        stream.seek(max(0, size - maximum_bytes), os.SEEK_SET)  # type: ignore[attr-defined]
        raw = stream.read(maximum_bytes)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError) as exc:
        failure = exc
    finally:
        if original_offset is not None:
            try:
                stream.seek(original_offset, os.SEEK_SET)  # type: ignore[attr-defined]
            except (AttributeError, OSError, ValueError) as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(f"log position restoration also failed: {exc!r}")
    if failure is not None:
        raise ROIAnalysisError("contained process log cannot be excerpted") from failure
    if type(raw) is not bytes or type(size) is not int:
        raise ROIAnalysisError("contained process log stream is invalid")
    excerpt = raw.decode("utf-8", errors="replace")
    encoded = excerpt.encode("utf-8")
    representation_truncated = len(encoded) > maximum_bytes
    if representation_truncated:
        excerpt = encoded[-maximum_bytes:].decode("utf-8", errors="ignore")
    return excerpt, size > maximum_bytes or representation_truncated


__all__ = [
    "AuthorityBindingV1",
    "ContainedMutationDrainRecord",
    "ContainedMutationLaunchBinding",
    "ContainedMutationLaunchRecord",
    "ContainedMutationRecorder",
    "ContainedMutationSpawnAttempt",
    "ContainedProcessResult",
    "ContainmentInspection",
    "ContainmentLaunchRequest",
    "ContainmentRecoveryRequest",
    "LinuxLocalProcessGroupAttachmentV1",
    "LinuxLocalProcessGroupDrainV1",
    "LinuxSystemdScopeAttachmentV1",
    "LinuxSystemdScopeDrainV1",
    "ProcessContainmentDrainEvidence",
    "ProcessContainmentLaunchEvidence",
    "ProcessContainmentRecovery",
    "ProcessStartObserver",
    "WindowsKillOnCloseJobAttachmentV1",
    "WindowsKillOnCloseJobDrainV1",
    "authoritative_lock_descriptors",
    "retain_authoritative_lock_descriptor",
]


@contextmanager
def retain_authoritative_lock_descriptor(descriptor: int) -> Iterator[None]:
    """Retain one already-locked POSIX descriptor through a contained exec."""
    if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0:
        raise ROIAnalysisError("authoritative lock descriptor is invalid")
    current = _AUTHORITATIVE_LOCK_DESCRIPTORS.get()
    if descriptor in current:
        raise ROIAnalysisError("authoritative lock descriptor is duplicated")
    token = _AUTHORITATIVE_LOCK_DESCRIPTORS.set((*current, descriptor))
    try:
        yield
    finally:
        _AUTHORITATIVE_LOCK_DESCRIPTORS.reset(token)


def authoritative_lock_descriptors() -> tuple[int, ...]:
    """Return the exact descriptors registered in the current call context."""
    return _AUTHORITATIVE_LOCK_DESCRIPTORS.get()
