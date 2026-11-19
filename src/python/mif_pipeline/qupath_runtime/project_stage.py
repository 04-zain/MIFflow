"""Strict, path-free evidence values for one contained project-stage run.

The `ProjectStageMutationEvidenceRecord` record carries `schema_name`, `schema_version`,
`authority_layout_sha256`, `workspace_storage_scope_sha256`, `project_identity_sha256` and
its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import re
from typing import Literal, Mapping

from mif_containment.protocol import (
    LinuxSystemdScopeAttachmentV1,
    LinuxSystemdScopeDrainV1,
)
from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.process_containment import (
    ContainmentLaunchRequest,
    LinuxLocalProcessGroupAttachmentV1,
    LinuxLocalProcessGroupDrainV1,
    ProcessContainmentDrainEvidence,
    ProcessContainmentLaunchEvidence,
    WindowsKillOnCloseJobAttachmentV1,
    WindowsKillOnCloseJobDrainV1,
)

from .catalogue import QuPathProcessStartToken

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_TERMINATION_REASONS = frozenset(
    {"normal_exit", "nonzero_exit", "timeout", "cancelled", "launch_publication_failed"}
)
_COMMON_FIELDS = (
    "authority_layout_sha256",
    "workspace_storage_scope_sha256",
    "project_identity_sha256",
    "active_mutation_run_sha256",
    "acquisition_record_sha256",
    "acquisition_session_challenge_sha256",
    "artifact_inventory_sha256",
    "mutation_request_sha256",
    "mutation_authorization_sha256",
    "fencing_token_sha256",
    "run_plan_sha256",
    "prepared_descriptor_sha256",
    "application_request_sha256",
    "launch_nonce_sha256",
)


# ==============================================================================
#                                 Project stage
# ==============================================================================


class ProjectStageEvidenceError(ValueError):
    """A project-stage runtime value violates its closed evidence contract."""


def _wire(value: object) -> object:
    if isinstance(value, AuthorityMemberRefV2):
        return value.to_mapping()
    if hasattr(value, "to_mapping") and callable(value.to_mapping):
        return value.to_mapping()
    if type(value) is tuple:
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    return value


def _mapping(value: object, *, omit: str | None = None) -> dict[str, object]:
    return {
        item.name: _wire(getattr(value, item.name))
        for item in fields(value)  # type: ignore[arg-type]
        if item.name != omit and not item.name.startswith("_")
    }


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_wire(value))).hexdigest()
    except Exception as exc:
        raise ProjectStageEvidenceError("project-stage evidence is not canonical JSON") from exc


def _payload_facts(record: object) -> tuple[str, int]:
    try:
        payload = canonical_json_bytes(record.to_mapping())
    except Exception as exc:
        raise ProjectStageEvidenceError("project-stage payload is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _closed(value: object, expected: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or frozenset(value) != expected
    ):
        raise ProjectStageEvidenceError(f"{label} fields are not the current closed schema")
    return value


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ProjectStageEvidenceError(f"{label} must be one lowercase SHA-256")
    return value


def _text(value: object, label: str) -> str:
    try:
        length = len(value.encode("utf-8", errors="strict")) if type(value) is str else 0
    except UnicodeError as exc:
        raise ProjectStageEvidenceError(f"{label} is invalid text") from exc
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or length > 4096
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectStageEvidenceError(f"{label} must be bounded canonical text")
    return value


def _safe_id(value: object, label: str) -> str:
    result = _text(value, label)
    if _SAFE_ID.fullmatch(result) is None or result in {".", ".."}:
        raise ProjectStageEvidenceError(f"{label} must be one safe identifier")
    return result


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProjectStageEvidenceError(f"{label} must be a positive integer")
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProjectStageEvidenceError(f"{label} must be a nonnegative integer")
    return value


def _ref(value: object, *, layout: str, family: str, label: str) -> AuthorityMemberRefV2:
    try:
        ref = (
            value
            if type(value) is AuthorityMemberRefV2
            else AuthorityMemberRefV2.from_mapping(value, expected_layout_sha256=layout)
        )
    except Exception as exc:
        raise ProjectStageEvidenceError(f"{label} is invalid") from exc
    if ref.authority_layout_sha256 != layout or ref.family != family:
        raise ProjectStageEvidenceError(f"{label} uses the wrong retained authority")
    return ref


def _validate_schema(record: object, expected: str, digest_field: str) -> None:
    if (
        getattr(record, "schema_name") != expected
        or type(getattr(record, "schema_version")) is not int
        or getattr(record, "schema_version") != 1
    ):
        raise ProjectStageEvidenceError(f"{expected} schema is unsupported")
    for name in _COMMON_FIELDS:
        _sha(getattr(record, name), name)
    if record.application_request_sha256 != record.prepared_descriptor_sha256:
        raise ProjectStageEvidenceError("application request must be the prepared descriptor")
    if _sha(getattr(record, digest_field), digest_field) != _digest(
        _mapping(record, omit=digest_field)
    ):
        raise ProjectStageEvidenceError(f"{digest_field} does not match the complete record")


def _create(cls, values: Mapping[str, object], digest_field: str):
    return cls(**dict(values), **{digest_field: _digest(values)})


def _from_array(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not list:
        raise ProjectStageEvidenceError(f"{label} must be an array")
    return tuple(value)


def _launch_evidence(value: object) -> ProcessContainmentLaunchEvidence:
    if not isinstance(value, Mapping):
        raise ProjectStageEvidenceError("launch evidence must be an object")
    provider = value.get("provider")
    try:
        if provider == "linux-systemd-scope-broker/v1":
            return LinuxSystemdScopeAttachmentV1.from_mapping(value)
        if provider == "linux-local-process-group/v1":
            return LinuxLocalProcessGroupAttachmentV1.from_mapping(value)
        if provider == "windows-kill-on-close-job/v1":
            return WindowsKillOnCloseJobAttachmentV1.from_mapping(value)
    except ValueError as exc:
        raise ProjectStageEvidenceError("launch evidence is invalid") from exc
    raise ProjectStageEvidenceError("launch evidence provider is unsupported")


def _drain_evidence(value: object) -> ProcessContainmentDrainEvidence:
    if not isinstance(value, Mapping):
        raise ProjectStageEvidenceError("drain evidence must be an object")
    provider = value.get("provider")
    try:
        if provider == "linux-systemd-scope-broker/v1":
            return LinuxSystemdScopeDrainV1.from_mapping(value)
        if provider == "linux-local-process-group/v1":
            return LinuxLocalProcessGroupDrainV1.from_mapping(value)
        if provider == "windows-kill-on-close-job/v1":
            return WindowsKillOnCloseJobDrainV1.from_mapping(value)
    except ValueError as exc:
        raise ProjectStageEvidenceError("drain evidence is invalid") from exc
    raise ProjectStageEvidenceError("drain evidence provider is unsupported")


def _evidence_matches(
    launch: ProcessContainmentLaunchEvidence, drain: ProcessContainmentDrainEvidence
) -> bool:
    if type(launch) is LinuxLocalProcessGroupAttachmentV1:
        return (
            type(drain) is LinuxLocalProcessGroupDrainV1
            and launch.leader_process_id == drain.leader_process_id
            and launch.leader_start_epoch_millis == drain.leader_start_epoch_millis
            and launch.process_group_id == drain.process_group_id
        )
    if type(launch) is WindowsKillOnCloseJobAttachmentV1:
        return type(drain) is WindowsKillOnCloseJobDrainV1 and launch.job_name == drain.job_name
    return (
        type(launch) is LinuxSystemdScopeAttachmentV1
        and type(drain) is LinuxSystemdScopeDrainV1
        and launch.attachment_receipt_sha256 == drain.attachment_receipt_sha256
    )


def _shared_equal(*records: object) -> bool:
    return all(
        all(getattr(record, name) == getattr(records[0], name) for name in _COMMON_FIELDS)
        for record in records[1:]
    )


@dataclass(frozen=True, slots=True)
class ProjectStageTargetObservation:
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    authorized_entry_sha256: str
    project_entry_id: str
    sample_id: str
    slide_id: str
    process_evidence_sha256: str
    project_snapshot_sha256: str
    result_ref: AuthorityMemberRefV2
    result_payload_sha256: str | None
    result_byte_count: int | None
    generation_manifest_ref: AuthorityMemberRefV2
    generation_manifest_payload_sha256: str | None
    generation_manifest_byte_count: int | None
    saved_state_before_sha256: str
    saved_state_disposition: Literal[
        "authenticated_unchanged", "authenticated_changed", "unattestable"
    ]
    saved_state_after_sha256: str | None
    observation_sha256: str

    def __post_init__(self) -> None:
        for name in ("node_id", "attempt_id", "project_entry_id", "sample_id"):
            _safe_id(getattr(self, name), name)
        _text(self.slide_id, "slide_id")
        for name in (
            "attempt_authorization_sha256",
            "authorized_entry_sha256",
            "process_evidence_sha256",
            "project_snapshot_sha256",
            "saved_state_before_sha256",
        ):
            _sha(getattr(self, name), name)
        layout = (
            self.result_ref.authority_layout_sha256
            if type(self.result_ref) is AuthorityMemberRefV2
            else ""
        )
        _ref(self.result_ref, layout=layout, family="runtime_exchange", label="result_ref")
        _ref(
            self.generation_manifest_ref,
            layout=layout,
            family="runtime_exchange",
            label="generation_manifest_ref",
        )
        if self.result_ref == self.generation_manifest_ref:
            raise ProjectStageEvidenceError("result and generation refs must be distinct")
        for prefix in ("result", "generation_manifest"):
            digest = getattr(self, f"{prefix}_payload_sha256")
            count = getattr(self, f"{prefix}_byte_count")
            if (digest is None) != (count is None):
                raise ProjectStageEvidenceError(
                    f"{prefix} content facts must be complete or absent"
                )
            if digest is not None:
                _sha(digest, f"{prefix}_payload_sha256")
                _nonnegative(count, f"{prefix}_byte_count")
        if self.saved_state_disposition not in {
            "authenticated_unchanged",
            "authenticated_changed",
            "unattestable",
        }:
            raise ProjectStageEvidenceError("saved_state_disposition is invalid")
        if self.saved_state_disposition == "unattestable":
            if self.saved_state_after_sha256 is not None:
                raise ProjectStageEvidenceError(
                    "unattestable saved state cannot claim an after digest"
                )
        else:
            after = _sha(self.saved_state_after_sha256, "saved_state_after_sha256")
            equal = after == self.saved_state_before_sha256
            if equal != (self.saved_state_disposition == "authenticated_unchanged"):
                raise ProjectStageEvidenceError(
                    "saved-state disposition disagrees with its digests"
                )
        if _sha(self.observation_sha256, "observation_sha256") != _digest(
            _mapping(self, omit="observation_sha256")
        ):
            raise ProjectStageEvidenceError("observation digest is invalid")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageTargetObservation":
        return _create(cls, values, "observation_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageTargetObservation":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "target observation"))
        result_mapping = raw["result_ref"]
        if not isinstance(result_mapping, Mapping):
            raise ProjectStageEvidenceError("result_ref is invalid")
        layout = _sha(result_mapping.get("authority_layout_sha256"), "authority_layout_sha256")
        raw["result_ref"] = _ref(
            result_mapping, layout=layout, family="runtime_exchange", label="result_ref"
        )
        raw["generation_manifest_ref"] = _ref(
            raw["generation_manifest_ref"],
            layout=layout,
            family="runtime_exchange",
            label="generation_manifest_ref",
        )
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageProcessAttemptRecord:
    schema_name: Literal["mif-project-stage-process-attempt"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    project_identity_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    run_plan_sha256: str
    prepared_descriptor_sha256: str
    application_request_sha256: str
    launch_nonce_sha256: str
    containment_request: ContainmentLaunchRequest
    attempt_sha256: str

    def __post_init__(self) -> None:
        if type(self.containment_request) is not ContainmentLaunchRequest:
            raise ProjectStageEvidenceError("containment_request must be typed")
        authority = self.containment_request.authority
        if (
            authority.launch_nonce_sha256 != self.launch_nonce_sha256
            or authority.application_request_sha256 != self.prepared_descriptor_sha256
            or authority.project_identity_sha256 != self.project_identity_sha256
            or authority.active_mutation_run_sha256 != self.active_mutation_run_sha256
        ):
            raise ProjectStageEvidenceError("containment request authority differs")
        _validate_schema(self, "mif-project-stage-process-attempt", "attempt_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageProcessAttemptRecord":
        complete = {
            "schema_name": "mif-project-stage-process-attempt",
            "schema_version": 1,
            **values,
        }
        return _create(cls, complete, "attempt_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageProcessAttemptRecord":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "process attempt"))
        raw["containment_request"] = ContainmentLaunchRequest.from_mapping(
            raw["containment_request"]
        )
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageProcessLaunchRecord:
    schema_name: Literal["mif-project-stage-process-launch"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    project_identity_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    run_plan_sha256: str
    prepared_descriptor_sha256: str
    application_request_sha256: str
    launch_nonce_sha256: str
    process_launch_ref: AuthorityMemberRefV2
    launch_request_identity_sha256: str
    attempt_sha256: str
    process_start_token: QuPathProcessStartToken
    launch_evidence: ProcessContainmentLaunchEvidence
    command_sha256: str
    environment_sha256: str
    executable_version: str
    process_launch_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectStageProcessLaunchRecord fields when the record is constructed.

        The checks cover ``authority_layout_sha256``, ``process_launch_ref``,
        ``launch_request_identity_sha256``, ``executable_version``, ``process_start_token``,
        ``launch_evidence``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        _ref(
            self.process_launch_ref,
            layout=layout,
            family="runtime_exchange",
            label="process_launch_ref",
        )
        for name in (
            "launch_request_identity_sha256",
            "attempt_sha256",
            "command_sha256",
            "environment_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.launch_request_identity_sha256 != _digest(
            {
                "active_mutation_run_sha256": self.active_mutation_run_sha256,
                "launch_nonce_sha256": self.launch_nonce_sha256,
                "run_plan_sha256": self.run_plan_sha256,
            }
        ):
            raise ProjectStageEvidenceError("launch request identity digest is invalid")
        if type(self.process_start_token) is not QuPathProcessStartToken:
            raise ProjectStageEvidenceError("process_start_token must be typed")
        if type(self.launch_evidence) not in (
            LinuxSystemdScopeAttachmentV1,
            LinuxLocalProcessGroupAttachmentV1,
            WindowsKillOnCloseJobAttachmentV1,
        ):
            raise ProjectStageEvidenceError("launch_evidence must be typed")
        if type(self.launch_evidence) is LinuxSystemdScopeAttachmentV1:
            attachment = json.loads(self.launch_evidence.attachment_receipt_json_utf8)
            if (
                self.process_start_token.provider != "linux-proc-start-ticks/v1"
                or self.process_start_token.process_id != attachment["gate_pid"]
                or str(self.process_start_token.start_ticks) != attachment["gate_start_token"]
            ):
                raise ProjectStageEvidenceError("Linux process identity differs from its gate")
        elif type(self.launch_evidence) is LinuxLocalProcessGroupAttachmentV1:
            if (
                self.process_start_token.provider != "linux-proc-start-ticks/v1"
                or self.process_start_token.process_id != self.launch_evidence.leader_process_id
            ):
                raise ProjectStageEvidenceError(
                    "local process identity differs from its group leader"
                )
        elif self.process_start_token.provider != "windows-process-creation-epoch-millis/v1":
            raise ProjectStageEvidenceError("Windows process identity provider is invalid")
        _text(self.executable_version, "executable_version")
        _validate_schema(self, "mif-project-stage-process-launch", "process_launch_sha256")

    @classmethod
    def create(
        cls, *, command: tuple[str, ...], environment: Mapping[str, str], **values: object
    ) -> "ProjectStageProcessLaunchRecord":
        if (
            type(command) is not tuple
            or not command
            or any(type(item) is not str or not item or "\0" in item for item in command)
        ):
            raise ProjectStageEvidenceError("command must be a non-empty string tuple")
        if not isinstance(environment, Mapping) or any(
            type(key) is not str or type(item) is not str or not key
            for key, item in environment.items()
        ):
            raise ProjectStageEvidenceError("environment must be a string mapping")
        complete = {
            "schema_name": "mif-project-stage-process-launch",
            "schema_version": 1,
            **values,
            "command_sha256": _digest({"argv": list(command)}),
            "environment_sha256": _digest({"environment": dict(environment)}),
        }
        return _create(cls, complete, "process_launch_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageProcessLaunchRecord":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "process launch"))
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        raw["process_launch_ref"] = _ref(
            raw["process_launch_ref"],
            layout=layout,
            family="runtime_exchange",
            label="process_launch_ref",
        )
        raw["process_start_token"] = QuPathProcessStartToken.from_mapping(
            raw["process_start_token"]
        )
        raw["launch_evidence"] = _launch_evidence(raw["launch_evidence"])
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageProcessDrainRecord:
    schema_name: Literal["mif-project-stage-process-drain"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    project_identity_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    run_plan_sha256: str
    prepared_descriptor_sha256: str
    application_request_sha256: str
    launch_nonce_sha256: str
    attempt_sha256: str
    process_launch_sha256: str | None
    process_start_token: QuPathProcessStartToken
    termination_reason: str
    exit_status: int | None
    terminal_status_sha256: str
    drain_evidence: ProcessContainmentDrainEvidence
    containment_drained: Literal[True]
    stdout_log_ref: AuthorityMemberRefV2
    stderr_log_ref: AuthorityMemberRefV2
    process_drain_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectStageProcessDrainRecord fields when the record is constructed.

        The checks cover ``authority_layout_sha256``, ``attempt_sha256``,
        ``process_launch_sha256``, ``termination_reason``, ``containment_drained``,
        ``stdout_log_ref``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        _sha(self.attempt_sha256, "attempt_sha256")
        if self.process_launch_sha256 is not None:
            _sha(self.process_launch_sha256, "process_launch_sha256")
        if (
            self.process_launch_sha256 is None
            and self.termination_reason != "launch_publication_failed"
        ):
            raise ProjectStageEvidenceError(
                "only launch publication failure may omit launch digest"
            )
        if type(self.process_start_token) is not QuPathProcessStartToken:
            raise ProjectStageEvidenceError("process_start_token must be typed")
        if self.termination_reason not in _TERMINATION_REASONS:
            raise ProjectStageEvidenceError("termination_reason is unsupported")
        if (
            type(self.drain_evidence)
            not in (
                LinuxSystemdScopeDrainV1,
                LinuxLocalProcessGroupDrainV1,
                WindowsKillOnCloseJobDrainV1,
            )
            or self.drain_evidence.termination_reason != self.termination_reason
        ):
            raise ProjectStageEvidenceError("drain evidence termination reason differs")
        if type(self.drain_evidence) is LinuxSystemdScopeDrainV1:
            drain = json.loads(self.drain_evidence.drain_receipt_json_utf8)
            if (
                self.process_start_token.provider != "linux-proc-start-ticks/v1"
                or self.process_start_token.process_id != drain["gate_pid"]
                or str(self.process_start_token.start_ticks) != drain["gate_start_token"]
            ):
                raise ProjectStageEvidenceError(
                    "Linux drain identity differs from its process gate"
                )
        elif type(self.drain_evidence) is LinuxLocalProcessGroupDrainV1:
            if (
                self.process_start_token.provider != "linux-proc-start-ticks/v1"
                or self.process_start_token.process_id != self.drain_evidence.leader_process_id
            ):
                raise ProjectStageEvidenceError(
                    "local drain identity differs from its process-group leader"
                )
        elif self.process_start_token.provider != "windows-process-creation-epoch-millis/v1":
            raise ProjectStageEvidenceError("Windows drain identity provider is invalid")
        if self.termination_reason == "normal_exit":
            if type(self.exit_status) is not int or self.exit_status != 0:
                raise ProjectStageEvidenceError("normal exit requires status zero")
        elif self.termination_reason == "nonzero_exit":
            if type(self.exit_status) is not int or self.exit_status == 0:
                raise ProjectStageEvidenceError("nonzero exit requires a nonzero status")
        elif self.exit_status is not None and type(self.exit_status) is not int:
            raise ProjectStageEvidenceError("exceptional exit status must be an integer or null")
        expected_status = _digest(
            {
                "process_start_token": self.process_start_token.to_mapping(),
                "returncode": self.exit_status,
                "termination_reason": self.termination_reason,
            }
        )
        if _sha(self.terminal_status_sha256, "terminal_status_sha256") != expected_status:
            raise ProjectStageEvidenceError("terminal status digest is invalid")
        if self.containment_drained is not True:
            raise ProjectStageEvidenceError("containment must be authoritatively drained")
        stdout = _ref(
            self.stdout_log_ref, layout=layout, family="runtime_log", label="stdout_log_ref"
        )
        stderr = _ref(
            self.stderr_log_ref, layout=layout, family="runtime_log", label="stderr_log_ref"
        )
        if stdout == stderr:
            raise ProjectStageEvidenceError("stdout and stderr refs must be distinct")
        _validate_schema(self, "mif-project-stage-process-drain", "process_drain_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageProcessDrainRecord":
        token = values.get("process_start_token")
        if type(token) is not QuPathProcessStartToken:
            raise ProjectStageEvidenceError("process_start_token must be typed")
        complete = {
            "schema_name": "mif-project-stage-process-drain",
            "schema_version": 1,
            **values,
            "terminal_status_sha256": _digest(
                {
                    "process_start_token": token.to_mapping(),
                    "returncode": values.get("exit_status"),
                    "termination_reason": values.get("termination_reason"),
                }
            ),
        }
        return _create(cls, complete, "process_drain_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageProcessDrainRecord":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "process drain"))
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        raw["process_start_token"] = QuPathProcessStartToken.from_mapping(
            raw["process_start_token"]
        )
        raw["drain_evidence"] = _drain_evidence(raw["drain_evidence"])
        raw["stdout_log_ref"] = _ref(
            raw["stdout_log_ref"], layout=layout, family="runtime_log", label="stdout_log_ref"
        )
        raw["stderr_log_ref"] = _ref(
            raw["stderr_log_ref"], layout=layout, family="runtime_log", label="stderr_log_ref"
        )
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageMutationEvidenceRecord:
    schema_name: Literal["mif-project-stage-mutation-evidence"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    project_identity_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    run_plan_sha256: str
    prepared_descriptor_sha256: str
    application_request_sha256: str
    launch_nonce_sha256: str
    attempt_sha256: str
    process_launch_sha256: str
    process_drain_sha256: str
    termination_reason: str
    predecessor_project_authority_head_sha256: str
    observed_project_snapshot_sha256: str
    target_observations: tuple[ProjectStageTargetObservation, ...]
    target_observation_set_sha256: str
    result_set_sha256: str
    stdout_log_ref: AuthorityMemberRefV2
    stdout_payload_sha256: str
    stdout_byte_count: int
    stderr_log_ref: AuthorityMemberRefV2
    stderr_payload_sha256: str
    stderr_byte_count: int
    mutation_evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectStageMutationEvidenceRecord fields when the record is
        constructed.

        The checks cover ``target_observations``, ``authority_layout_sha256``,
        ``termination_reason``, ``stdout_log_ref``, ``stderr_log_ref``,
        ``stdout_byte_count``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        for name in (
            "attempt_sha256",
            "process_launch_sha256",
            "process_drain_sha256",
            "predecessor_project_authority_head_sha256",
            "observed_project_snapshot_sha256",
            "stdout_payload_sha256",
            "stderr_payload_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.termination_reason not in _TERMINATION_REASONS:
            raise ProjectStageEvidenceError("termination_reason is unsupported")
        observations = self.target_observations
        if (
            type(observations) is not tuple
            or not observations
            or not all(type(item) is ProjectStageTargetObservation for item in observations)
        ):
            raise ProjectStageEvidenceError("target_observations must be a non-empty typed tuple")
        if observations != tuple(
            sorted(observations, key=lambda item: item.project_entry_id)
        ) or len({item.project_entry_id for item in observations}) != len(observations):
            raise ProjectStageEvidenceError("target observations must be sorted and unique")
        if (
            len({item.process_evidence_sha256 for item in observations}) != 1
            or any(
                item.project_snapshot_sha256 != self.observed_project_snapshot_sha256
                for item in observations
            )
            or any(
                item.result_ref.authority_layout_sha256 != layout
                or item.generation_manifest_ref.authority_layout_sha256 != layout
                for item in observations
            )
        ):
            raise ProjectStageEvidenceError(
                "target observations differ from common process/project evidence"
            )
        expected_observations = _digest(
            {"observation_sha256s": [item.observation_sha256 for item in observations]}
        )
        if (
            _sha(self.target_observation_set_sha256, "target_observation_set_sha256")
            != expected_observations
        ):
            raise ProjectStageEvidenceError("target observation set digest is invalid")
        results = [_result_projection(item) for item in observations]
        if _sha(self.result_set_sha256, "result_set_sha256") != _digest({"results": results}):
            raise ProjectStageEvidenceError("result set digest is invalid")
        stdout = _ref(
            self.stdout_log_ref, layout=layout, family="runtime_log", label="stdout_log_ref"
        )
        stderr = _ref(
            self.stderr_log_ref, layout=layout, family="runtime_log", label="stderr_log_ref"
        )
        if stdout == stderr:
            raise ProjectStageEvidenceError("stdout and stderr refs must be distinct")
        _nonnegative(self.stdout_byte_count, "stdout_byte_count")
        _nonnegative(self.stderr_byte_count, "stderr_byte_count")
        _validate_schema(self, "mif-project-stage-mutation-evidence", "mutation_evidence_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageMutationEvidenceRecord":
        observations = values.get("target_observations")
        if type(observations) is not tuple:
            raise ProjectStageEvidenceError("target_observations must be a tuple")
        complete = {
            "schema_name": "mif-project-stage-mutation-evidence",
            "schema_version": 1,
            **values,
            "target_observation_set_sha256": _digest({"observation_sha256s": [item.observation_sha256 for item in observations]}),  # type: ignore[union-attr]
            "result_set_sha256": _digest(
                {"results": [_result_projection(item) for item in observations]}
            ),
        }
        return _create(cls, complete, "mutation_evidence_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageMutationEvidenceRecord":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "mutation evidence"))
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        raw["target_observations"] = tuple(
            ProjectStageTargetObservation.from_mapping(item)
            for item in _from_array(raw["target_observations"], "target_observations")
        )
        raw["stdout_log_ref"] = _ref(
            raw["stdout_log_ref"], layout=layout, family="runtime_log", label="stdout_log_ref"
        )
        raw["stderr_log_ref"] = _ref(
            raw["stderr_log_ref"], layout=layout, family="runtime_log", label="stderr_log_ref"
        )
        return cls(**raw)  # type: ignore[arg-type]


def _result_projection(item: ProjectStageTargetObservation) -> dict[str, object]:
    return {
        "project_entry_id": item.project_entry_id,
        "result_ref": item.result_ref.to_mapping(),
        "result_payload_sha256": item.result_payload_sha256,
        "result_byte_count": item.result_byte_count,
        "generation_manifest_ref": item.generation_manifest_ref.to_mapping(),
        "generation_manifest_payload_sha256": item.generation_manifest_payload_sha256,
        "generation_manifest_byte_count": item.generation_manifest_byte_count,
    }


def validate_project_stage_process_evidence(
    *,
    attempt: ProjectStageProcessAttemptRecord,
    attempt_ref: AuthorityMemberRefV2,
    attempt_payload_sha256: str,
    attempt_byte_count: int,
    launch: ProjectStageProcessLaunchRecord,
    launch_ref: AuthorityMemberRefV2,
    launch_payload_sha256: str,
    launch_byte_count: int,
    drain: ProjectStageProcessDrainRecord,
    drain_ref: AuthorityMemberRefV2,
    drain_payload_sha256: str,
    drain_byte_count: int,
) -> str:
    """Check typed process-record correspondence and return its canonical digest.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller.

    Callers must reopen authentic retained bytes before calling this function. The returned
    digest grants no operation or process capability."""
    if (
        type(attempt) is not ProjectStageProcessAttemptRecord
        or type(launch) is not ProjectStageProcessLaunchRecord
        or type(drain) is not ProjectStageProcessDrainRecord
    ):
        raise ProjectStageEvidenceError("process evidence chain must be typed")
    if (
        not _shared_equal(attempt, launch, drain)
        or launch.attempt_sha256 != attempt.attempt_sha256
        or drain.attempt_sha256 != attempt.attempt_sha256
        or drain.process_launch_sha256 != launch.process_launch_sha256
        or drain.process_start_token != launch.process_start_token
    ):
        raise ProjectStageEvidenceError("process evidence chain identities differ")
    if (
        attempt.containment_request.provider != launch.launch_evidence.provider
        or launch.launch_evidence.provider != drain.drain_evidence.provider
        or not _evidence_matches(launch.launch_evidence, drain.drain_evidence)
    ):
        raise ProjectStageEvidenceError("process containment evidence differs")
    if (
        attempt.containment_request.stdout_log_name != drain.stdout_log_ref.physical_name
        or attempt.containment_request.stderr_log_name != drain.stderr_log_ref.physical_name
    ):
        raise ProjectStageEvidenceError("process log refs differ from containment request")
    layout = attempt.authority_layout_sha256
    roles = []
    for role, record, ref_value, payload_value, count_value, family in (
        (
            "attempt",
            attempt,
            attempt_ref,
            attempt_payload_sha256,
            attempt_byte_count,
            "batch_project_runtime_evidence",
        ),
        (
            "launch",
            launch,
            launch_ref,
            launch_payload_sha256,
            launch_byte_count,
            "runtime_exchange",
        ),
        (
            "drain",
            drain,
            drain_ref,
            drain_payload_sha256,
            drain_byte_count,
            "batch_project_runtime_evidence",
        ),
    ):
        ref = _ref(ref_value, layout=layout, family=family, label=f"{role}_ref")
        payload = _sha(payload_value, f"{role}_payload_sha256")
        count = _positive(count_value, f"{role}_byte_count")
        expected_payload, expected_count = _payload_facts(record)
        if payload != expected_payload or count != expected_count:
            raise ProjectStageEvidenceError(f"{role} payload facts differ from record")
        if role == "launch" and ref != launch.process_launch_ref:
            raise ProjectStageEvidenceError("launch ref differs from launch record")
        roles.append(
            {
                "role": role,
                "record_ref": ref.to_mapping(),
                "payload_sha256": payload,
                "byte_count": count,
            }
        )
    return _digest({"evidence": roles})


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageProcessEvidenceCapability:
    attempt: ProjectStageProcessAttemptRecord
    attempt_ref: AuthorityMemberRefV2
    attempt_payload_sha256: str
    attempt_byte_count: int
    launch: ProjectStageProcessLaunchRecord
    launch_ref: AuthorityMemberRefV2
    launch_payload_sha256: str
    launch_byte_count: int
    drain: ProjectStageProcessDrainRecord
    drain_ref: AuthorityMemberRefV2
    drain_payload_sha256: str
    drain_byte_count: int
    evidence_set_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError("ProjectStageProcessEvidenceCapability is issuer-only")

    @classmethod
    def _issue(cls, *, issuer: object, **values: object) -> "ProjectStageProcessEvidenceCapability":
        if issuer is None:
            raise ProjectStageEvidenceError("capability issuer is required")
        evidence_set_sha256 = validate_project_stage_process_evidence(**values)  # type: ignore[arg-type]
        instance = object.__new__(cls)
        for name, item in values.items():
            object.__setattr__(instance, name, item)
        object.__setattr__(instance, "evidence_set_sha256", evidence_set_sha256)
        object.__setattr__(instance, "_issuer", issuer)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageMutationEvidenceCapability:
    mutation: ProjectStageMutationEvidenceRecord
    mutation_ref: AuthorityMemberRefV2
    mutation_payload_sha256: str
    mutation_byte_count: int
    process_evidence: ProjectStageProcessEvidenceCapability
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError("ProjectStageMutationEvidenceCapability is issuer-only")

    @classmethod
    def _issue(
        cls,
        *,
        issuer: object,
        mutation: ProjectStageMutationEvidenceRecord,
        mutation_ref: AuthorityMemberRefV2,
        mutation_payload_sha256: str,
        mutation_byte_count: int,
        process_evidence: ProjectStageProcessEvidenceCapability,
    ) -> "ProjectStageMutationEvidenceCapability":
        if (
            issuer is None
            or type(mutation) is not ProjectStageMutationEvidenceRecord
            or type(process_evidence) is not ProjectStageProcessEvidenceCapability
        ):
            raise ProjectStageEvidenceError("mutation capability inputs are invalid")
        if (
            not _shared_equal(mutation, process_evidence.attempt)
            or mutation.attempt_sha256 != process_evidence.attempt.attempt_sha256
            or mutation.process_launch_sha256 != process_evidence.launch.process_launch_sha256
            or mutation.process_drain_sha256 != process_evidence.drain.process_drain_sha256
            or mutation.termination_reason != process_evidence.drain.termination_reason
        ):
            raise ProjectStageEvidenceError("mutation evidence differs from process chain")
        if (
            mutation.stdout_log_ref != process_evidence.drain.stdout_log_ref
            or mutation.stderr_log_ref != process_evidence.drain.stderr_log_ref
        ):
            raise ProjectStageEvidenceError("mutation log authority differs from process drain")
        if any(
            item.process_evidence_sha256 != process_evidence.evidence_set_sha256
            for item in mutation.target_observations
        ):
            raise ProjectStageEvidenceError("target observations differ from process evidence")
        _ref(
            mutation_ref,
            layout=mutation.authority_layout_sha256,
            family="batch_project_runtime_evidence",
            label="mutation_ref",
        )
        payload = _sha(mutation_payload_sha256, "mutation_payload_sha256")
        count = _positive(mutation_byte_count, "mutation_byte_count")
        expected_payload, expected_count = _payload_facts(mutation)
        if payload != expected_payload or count != expected_count:
            raise ProjectStageEvidenceError("mutation payload facts differ from record")
        instance = object.__new__(cls)
        for name, item in (
            ("mutation", mutation),
            ("mutation_ref", mutation_ref),
            ("mutation_payload_sha256", mutation_payload_sha256),
            ("mutation_byte_count", mutation_byte_count),
            ("process_evidence", process_evidence),
            ("_issuer", issuer),
        ):
            object.__setattr__(instance, name, item)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageNoMutationProof:
    variant: Literal["never_entered_child_creation", "contained_never_released"]
    project_identity_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    prepared_descriptor_sha256: str
    attempt_sha256: str | None
    process_launch_sha256: str | None
    process_drain_sha256: str | None
    proof_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError("ProjectStageNoMutationProof is issuer-only")

    @classmethod
    def _issue(cls, *, issuer: object, **values: object) -> "ProjectStageNoMutationProof":
        input_fields = (
            "variant",
            "project_identity_sha256",
            "active_mutation_run_sha256",
            "acquisition_record_sha256",
            "acquisition_session_challenge_sha256",
            "prepared_descriptor_sha256",
            "attempt_sha256",
            "process_launch_sha256",
            "process_drain_sha256",
        )
        if not set(values).issubset(input_fields):
            raise ProjectStageEvidenceError("no-mutation proof inputs are not closed")
        complete = {name: values.get(name) for name in input_fields}
        if issuer is None or complete.get("variant") not in {
            "never_entered_child_creation",
            "contained_never_released",
        }:
            raise ProjectStageEvidenceError("no-mutation proof issuer/variant is invalid")
        for name in (
            "project_identity_sha256",
            "active_mutation_run_sha256",
            "acquisition_record_sha256",
            "acquisition_session_challenge_sha256",
            "prepared_descriptor_sha256",
        ):
            _sha(complete.get(name), name)
        evidence = tuple(
            complete[name]
            for name in ("attempt_sha256", "process_launch_sha256", "process_drain_sha256")
        )
        if complete["variant"] == "never_entered_child_creation":
            if any(item is not None for item in evidence):
                raise ProjectStageEvidenceError("pre-creation proof cannot carry process evidence")
        elif any(item is None for item in evidence):
            raise ProjectStageEvidenceError("contained proof requires the complete process chain")
        for item in evidence:
            if item is not None:
                _sha(item, "process evidence digest")
        instance = object.__new__(cls)
        for name, item in complete.items():
            object.__setattr__(instance, name, item)
        object.__setattr__(instance, "proof_sha256", _digest(complete))
        object.__setattr__(instance, "_issuer", issuer)
        return instance


@dataclass(frozen=True, slots=True)
class ProjectStageProcessSummary:
    process_evidence_sha256: str
    process_start_token_sha256: str
    termination_reason: str
    exit_status: int | None
    stdout_log_ref: AuthorityMemberRefV2
    stdout_payload_sha256: str
    stdout_byte_count: int
    stderr_log_ref: AuthorityMemberRefV2
    stderr_payload_sha256: str
    stderr_byte_count: int
    containment_drained: Literal[True]
    summary_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "process_evidence_sha256",
            "process_start_token_sha256",
            "stdout_payload_sha256",
            "stderr_payload_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.termination_reason not in _TERMINATION_REASONS:
            raise ProjectStageEvidenceError("termination_reason is unsupported")
        if self.termination_reason == "normal_exit" and (
            type(self.exit_status) is not int or self.exit_status != 0
        ):
            raise ProjectStageEvidenceError("normal summary requires status zero")
        if self.termination_reason == "nonzero_exit" and (
            type(self.exit_status) is not int or self.exit_status == 0
        ):
            raise ProjectStageEvidenceError("nonzero summary requires a nonzero status")
        if self.exit_status is not None and type(self.exit_status) is not int:
            raise ProjectStageEvidenceError("exit_status must be an integer or null")
        layout = (
            self.stdout_log_ref.authority_layout_sha256
            if type(self.stdout_log_ref) is AuthorityMemberRefV2
            else ""
        )
        _ref(self.stdout_log_ref, layout=layout, family="runtime_log", label="stdout_log_ref")
        _ref(self.stderr_log_ref, layout=layout, family="runtime_log", label="stderr_log_ref")
        _nonnegative(self.stdout_byte_count, "stdout_byte_count")
        _nonnegative(self.stderr_byte_count, "stderr_byte_count")
        if self.containment_drained is not True:
            raise ProjectStageEvidenceError("summary requires drained containment")
        if _sha(self.summary_sha256, "summary_sha256") != _digest(
            _mapping(self, omit="summary_sha256")
        ):
            raise ProjectStageEvidenceError("summary digest is invalid")

    @classmethod
    def create(
        cls, *, process_start_token: QuPathProcessStartToken, **values: object
    ) -> "ProjectStageProcessSummary":
        if type(process_start_token) is not QuPathProcessStartToken:
            raise ProjectStageEvidenceError("process_start_token must be typed")
        complete = {
            **values,
            "process_start_token_sha256": _digest(process_start_token.to_mapping()),
        }
        return _create(cls, complete, "summary_sha256")


@dataclass(frozen=True, slots=True)
class ProjectStageProcessCompletion:
    disposition: Literal[
        "released_child", "never_entered_child_creation", "contained_never_released"
    ]
    process_evidence: ProjectStageProcessEvidenceCapability | None
    no_mutation_proof: ProjectStageNoMutationProof | None
    process_summary: ProjectStageProcessSummary | None
    completion_sha256: str

    def __post_init__(self) -> None:
        if self.disposition == "released_child":
            if (
                type(self.process_evidence) is not ProjectStageProcessEvidenceCapability
                or self.no_mutation_proof is not None
                or type(self.process_summary) is not ProjectStageProcessSummary
            ):
                raise ProjectStageEvidenceError(
                    "released completion requires process evidence and summary only"
                )
            if (
                self.process_summary.process_evidence_sha256
                != self.process_evidence.evidence_set_sha256
            ):
                raise ProjectStageEvidenceError("completion summary differs from process evidence")
            drain = self.process_evidence.drain
            if (
                self.process_summary.process_start_token_sha256
                != _digest(drain.process_start_token.to_mapping())
                or self.process_summary.termination_reason != drain.termination_reason
                or self.process_summary.exit_status != drain.exit_status
                or self.process_summary.stdout_log_ref != drain.stdout_log_ref
                or self.process_summary.stderr_log_ref != drain.stderr_log_ref
            ):
                raise ProjectStageEvidenceError(
                    "completion summary differs from reopened drain evidence"
                )
        elif self.disposition in {"never_entered_child_creation", "contained_never_released"}:
            if (
                self.process_evidence is not None
                or self.process_summary is not None
                or type(self.no_mutation_proof) is not ProjectStageNoMutationProof
                or self.no_mutation_proof.variant != self.disposition
            ):
                raise ProjectStageEvidenceError(
                    "no-mutation completion requires its exact proof only"
                )
        else:
            raise ProjectStageEvidenceError("completion disposition is invalid")
        material = {
            "disposition": self.disposition,
            "evidence_set_sha256": (
                None if self.process_evidence is None else self.process_evidence.evidence_set_sha256
            ),
            "proof_sha256": (
                None if self.no_mutation_proof is None else self.no_mutation_proof.proof_sha256
            ),
            "summary_sha256": (
                None if self.process_summary is None else self.process_summary.summary_sha256
            ),
        }
        if _sha(self.completion_sha256, "completion_sha256") != _digest(material):
            raise ProjectStageEvidenceError("completion digest is invalid")

    @classmethod
    def create(
        cls,
        *,
        disposition: str,
        process_evidence: ProjectStageProcessEvidenceCapability | None,
        no_mutation_proof: ProjectStageNoMutationProof | None,
        process_summary: ProjectStageProcessSummary | None,
    ) -> "ProjectStageProcessCompletion":
        material = {
            "disposition": disposition,
            "evidence_set_sha256": (
                None if process_evidence is None else process_evidence.evidence_set_sha256
            ),
            "proof_sha256": None if no_mutation_proof is None else no_mutation_proof.proof_sha256,
            "summary_sha256": None if process_summary is None else process_summary.summary_sha256,
        }
        return cls(disposition=disposition, process_evidence=process_evidence, no_mutation_proof=no_mutation_proof, process_summary=process_summary, completion_sha256=_digest(material))  # type: ignore[arg-type]


_PROJECT_STAGE_PROCESS_EVIDENCE_ISSUER = object()


class _ProjectStageProcessEvidenceIssuer:
    """Private composer for one authenticated project-stage execution."""

    __slots__ = ("_identity", "_runtime_binding")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("project-stage process evidence issuer is private")

    @classmethod
    def _issue(
        cls, *, runtime_binding: object, _issuer: object
    ) -> "_ProjectStageProcessEvidenceIssuer":
        if _issuer is not _PROJECT_STAGE_PROCESS_EVIDENCE_ISSUER:
            raise TypeError("project-stage process evidence issuer is invalid")
        value = object.__new__(cls)
        object.__setattr__(value, "_identity", object())
        object.__setattr__(value, "_runtime_binding", runtime_binding)
        return value

    def process_evidence(
        self,
        *,
        attempt: ProjectStageProcessAttemptRecord,
        attempt_ref: AuthorityMemberRefV2,
        launch: ProjectStageProcessLaunchRecord,
        launch_ref: AuthorityMemberRefV2,
        drain: ProjectStageProcessDrainRecord,
        drain_ref: AuthorityMemberRefV2,
    ) -> ProjectStageProcessEvidenceCapability:
        records = (("attempt", attempt), ("launch", launch), ("drain", drain))
        facts: dict[str, object] = {}
        for role, record in records:
            payload = canonical_json_bytes(record.to_mapping())
            facts[f"{role}_payload_sha256"] = hashlib.sha256(payload).hexdigest()
            facts[f"{role}_byte_count"] = len(payload)
        return ProjectStageProcessEvidenceCapability._issue(
            issuer=self._identity,
            attempt=attempt,
            attempt_ref=attempt_ref,
            launch=launch,
            launch_ref=launch_ref,
            drain=drain,
            drain_ref=drain_ref,
            **facts,
        )

    def no_mutation_proof(
        self,
        *,
        variant: str,
        project_identity_sha256: str,
        active_mutation_run_sha256: str,
        acquisition_record_sha256: str,
        acquisition_session_challenge_sha256: str,
        prepared_descriptor_sha256: str,
        attempt_sha256: str | None,
        process_launch_sha256: str | None,
        process_drain_sha256: str | None,
    ) -> ProjectStageNoMutationProof:
        return ProjectStageNoMutationProof._issue(
            issuer=self._identity,
            variant=variant,
            project_identity_sha256=project_identity_sha256,
            active_mutation_run_sha256=active_mutation_run_sha256,
            acquisition_record_sha256=acquisition_record_sha256,
            acquisition_session_challenge_sha256=(acquisition_session_challenge_sha256),
            prepared_descriptor_sha256=prepared_descriptor_sha256,
            attempt_sha256=attempt_sha256,
            process_launch_sha256=process_launch_sha256,
            process_drain_sha256=process_drain_sha256,
        )


def _issue_project_stage_process_evidence_issuer(
    *, runtime_binding: object
) -> _ProjectStageProcessEvidenceIssuer:
    from .runner import _ProjectStageRunnerBinding

    if not isinstance(runtime_binding, _ProjectStageRunnerBinding):
        raise TypeError("project-stage process runtime binding is invalid")
    runtime_binding.authenticate()
    return _ProjectStageProcessEvidenceIssuer._issue(
        runtime_binding=runtime_binding, _issuer=_PROJECT_STAGE_PROCESS_EVIDENCE_ISSUER
    )


__all__ = [
    "ProjectStageEvidenceError",
    "ProjectStageMutationEvidenceCapability",
    "ProjectStageMutationEvidenceRecord",
    "ProjectStageNoMutationProof",
    "ProjectStageProcessAttemptRecord",
    "ProjectStageProcessCompletion",
    "ProjectStageProcessDrainRecord",
    "ProjectStageProcessEvidenceCapability",
    "ProjectStageProcessLaunchRecord",
    "ProjectStageProcessSummary",
    "ProjectStageTargetObservation",
]
