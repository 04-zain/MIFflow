"""Current-only process-containment journal for authoritative Warpy mutation.

The recorder deliberately depends only on pipeline-core containment values.  Its
caller retains storage authority and supplies one append-only publication seam
plus a current-issuer check.  Equal-looking values from another recorder are
not authority: every predecessor must be the exact object this instance issued."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.process_containment import (
    ContainedMutationDrainRecord,
    ContainedMutationLaunchBinding,
    ContainedMutationLaunchRecord,
    ContainedMutationSpawnAttempt,
    ProcessContainmentDrainEvidence,
    ProcessContainmentLaunchEvidence,
)
from mif_pipeline.pipeline_core.schema_registry import assert_current_writer

_CURRENT_LEASE_SCHEMA = "warpy-transfer-mutation-lease/v5"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_ID = re.compile(r"transfer-attempt-[0-9a-f]{64}\Z")
_RECORD_SCHEMAS = {
    "process_spawn_attempt": "warpy-transfer-process-spawn-attempt/v1",
    "process_launch": "warpy-transfer-process-launch/v1",
    "process_drain": "warpy-transfer-process-drain/v1",
}
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "lease_schema_version",
        "lease_sha256",
        "attempt_id",
        "attempt_sha256",
        "operation",
        "request_sha256",
        "project_identity_sha256",
        "mutation_run_sha256",
        "hierarchy_scope_sha256",
        "record",
        "record_sha256",
        "predecessor_record_sha256",
    }
)


# ==============================================================================
#                           Contained mutation journal
# ==============================================================================


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ROIAnalysisError(f"{label} must be one lowercase SHA-256")
    return value


def _record_sha256(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(value), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ROIAnalysisError("Warpy containment record is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WarpyContainmentLeaseDiagnostic:
    """Side-effect-free classification of one known lease wire generation."""

    schema_version: str
    disposition: Literal["current"]
    authorizes_containment_effects: bool


def diagnose_warpy_containment_lease(
    schema_version: object,
) -> WarpyContainmentLeaseDiagnostic:
    """Checks the current transfer-permission version before reading its process records."""
    if type(schema_version) is str and schema_version == _CURRENT_LEASE_SCHEMA:
        return WarpyContainmentLeaseDiagnostic(
            schema_version=_CURRENT_LEASE_SCHEMA,
            disposition="current",
            authorizes_containment_effects=True,
        )
    raise ROIAnalysisError("Warpy containment lease schema is unsupported")


def validate_warpy_containment_record(
    kind: object,
    value: object,
    *,
    lease_sha256: str,
    attempt_id: str,
    attempt_sha256: str,
    operation: Literal["commit", "cleanup"],
    request_sha256: str,
    project_identity_sha256: str,
    mutation_run_sha256: str,
    hierarchy_scope_sha256: str,
    predecessor_record_sha256: str | None,
) -> ContainedMutationSpawnAttempt | ContainedMutationLaunchRecord | ContainedMutationDrainRecord:
    """Reopen one current envelope against independently supplied authority.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    for candidate, label in (
        (lease_sha256, "lease_sha256"),
        (attempt_sha256, "attempt_sha256"),
        (request_sha256, "request_sha256"),
        (project_identity_sha256, "project_identity_sha256"),
        (mutation_run_sha256, "mutation_run_sha256"),
        (hierarchy_scope_sha256, "hierarchy_scope_sha256"),
    ):
        _require_sha256(candidate, label)
    if type(attempt_id) is not str or _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise ROIAnalysisError("Warpy containment attempt ID is invalid")
    if type(operation) is not str or operation not in {"commit", "cleanup"}:
        raise ROIAnalysisError("Warpy containment operation is invalid")
    if predecessor_record_sha256 is not None:
        _require_sha256(predecessor_record_sha256, "predecessor_record_sha256")
    if type(kind) is not str or kind not in _RECORD_SCHEMAS:
        raise ROIAnalysisError("Warpy containment journal record kind is unsupported")
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_FIELDS:
        raise ROIAnalysisError("Warpy containment journal record fields changed")
    if any(type(key) is not str for key in value):
        raise ROIAnalysisError("Warpy containment journal record fields changed")
    expected = {
        "schema_version": _RECORD_SCHEMAS[kind],
        "lease_schema_version": _CURRENT_LEASE_SCHEMA,
        "lease_sha256": lease_sha256,
        "attempt_id": attempt_id,
        "attempt_sha256": attempt_sha256,
        "operation": operation,
        "request_sha256": request_sha256,
        "project_identity_sha256": project_identity_sha256,
        "mutation_run_sha256": mutation_run_sha256,
        "hierarchy_scope_sha256": hierarchy_scope_sha256,
        "predecessor_record_sha256": predecessor_record_sha256,
    }
    if any(
        type(value.get(field)) is not type(expected_value) or value.get(field) != expected_value
        for field, expected_value in expected.items()
    ):
        raise ROIAnalysisError("Warpy containment journal record scope changed")
    assert_current_writer(_RECORD_SCHEMAS[kind].removesuffix("/v1"), "1")
    record_mapping = value.get("record")
    if not isinstance(record_mapping, Mapping):
        raise ROIAnalysisError("Warpy containment journal nested record is invalid")
    digest = value.get("record_sha256")
    _require_sha256(digest, "record_sha256")
    if digest != _record_sha256(record_mapping):
        raise ROIAnalysisError("Warpy containment journal record digest changed")
    try:
        if kind == "process_spawn_attempt":
            parsed = ContainedMutationSpawnAttempt.from_mapping(record_mapping)
        elif kind == "process_launch":
            parsed = ContainedMutationLaunchRecord.from_mapping(record_mapping)
        else:
            parsed = ContainedMutationDrainRecord.from_mapping(record_mapping)
    except (TypeError, ValueError) as exc:
        raise ROIAnalysisError("Warpy containment journal nested record is invalid") from exc
    if type(parsed) is ContainedMutationSpawnAttempt:
        binding = parsed.binding
    elif type(parsed) is ContainedMutationLaunchRecord:
        binding = parsed.spawn_attempt.binding
    elif type(parsed.launch_or_attempt) is ContainedMutationLaunchRecord:
        binding = parsed.launch_or_attempt.spawn_attempt.binding
    else:
        binding = parsed.launch_or_attempt.binding
    if (
        type(binding) is not ContainedMutationLaunchBinding
        or binding.request_sha256 != request_sha256
        or binding.operation != operation
        or binding.project_identity_sha256 != project_identity_sha256
        or binding.mutation_run_sha256 != mutation_run_sha256
        or binding.hierarchy_scope_sha256 != hierarchy_scope_sha256
    ):
        raise ROIAnalysisError("Warpy containment journal nested record scope changed")
    return parsed


class WarpyContainedMutationJournal:
    """One-use, issuer-bound implementation of ``ContainedMutationRecorder``."""

    @classmethod
    def issue(
        cls,
        *,
        lease_schema_version: str,
        lease_sha256: str,
        attempt_id: str,
        attempt_sha256: str,
        operation: Literal["commit", "cleanup"],
        request_sha256: str,
        project_identity_sha256: str,
        mutation_run_sha256: str,
        hierarchy_scope_sha256: str,
        append_record: Callable[[str, Mapping[str, object]], None],
        issuer_is_current: Callable[[], bool],
    ) -> "WarpyContainedMutationJournal":
        diagnostic = diagnose_warpy_containment_lease(lease_schema_version)
        if not diagnostic.authorizes_containment_effects:
            raise ROIAnalysisError("historical Warpy containment requires manual review")
        assert_current_writer("warpy-transfer-mutation-lease", "5")
        if type(attempt_id) is not str or _ATTEMPT_ID.fullmatch(attempt_id) is None:
            raise ROIAnalysisError("Warpy containment attempt ID is invalid")
        if type(operation) is not str or operation not in {"commit", "cleanup"}:
            raise ROIAnalysisError("Warpy containment operation is invalid")
        if not callable(append_record) or not callable(issuer_is_current):
            raise ROIAnalysisError("Warpy containment issuer is invalid")
        return cls(
            lease_schema_version=lease_schema_version,
            lease_sha256=_require_sha256(lease_sha256, "lease_sha256"),
            attempt_id=attempt_id,
            attempt_sha256=_require_sha256(attempt_sha256, "attempt_sha256"),
            operation=operation,
            request_sha256=_require_sha256(request_sha256, "request_sha256"),
            project_identity_sha256=_require_sha256(
                project_identity_sha256, "project_identity_sha256"
            ),
            mutation_run_sha256=_require_sha256(mutation_run_sha256, "mutation_run_sha256"),
            hierarchy_scope_sha256=_require_sha256(
                hierarchy_scope_sha256, "hierarchy_scope_sha256"
            ),
            append_record=append_record,
            issuer_is_current=issuer_is_current,
        )

    def __init__(
        self,
        *,
        lease_schema_version: str,
        lease_sha256: str,
        attempt_id: str,
        attempt_sha256: str,
        operation: Literal["commit", "cleanup"],
        request_sha256: str,
        project_identity_sha256: str,
        mutation_run_sha256: str,
        hierarchy_scope_sha256: str,
        append_record: Callable[[str, Mapping[str, object]], None],
        issuer_is_current: Callable[[], bool],
    ) -> None:
        self._lease_schema_version = lease_schema_version
        self._lease_sha256 = lease_sha256
        self._attempt_id = attempt_id
        self._attempt_sha256 = attempt_sha256
        self._operation = operation
        self._request_sha256 = request_sha256
        self._project_identity_sha256 = project_identity_sha256
        self._mutation_run_sha256 = mutation_run_sha256
        self._hierarchy_scope_sha256 = hierarchy_scope_sha256
        self._append_record = append_record
        self._issuer_is_current = issuer_is_current
        self._binding: ContainedMutationLaunchBinding | None = None
        self._attempt: ContainedMutationSpawnAttempt | None = None
        self._launch: ContainedMutationLaunchRecord | None = None
        self._drained = False
        self._attempt_record_sha256: str | None = None
        self._launch_record_sha256: str | None = None
        self._publication_ambiguous = False

    def _require_current(self) -> None:
        if self._publication_ambiguous:
            raise ROIAnalysisError(
                "Warpy containment publication is ambiguous; recorder is poisoned"
            )
        try:
            current = self._issuer_is_current()
        except Exception as exc:
            raise ROIAnalysisError("Warpy containment issuer cannot be authenticated") from exc
        if current is not True:
            raise ROIAnalysisError("Warpy containment issuer is foreign or stale")

    def _common(self) -> dict[str, object]:
        return {
            "lease_schema_version": self._lease_schema_version,
            "lease_sha256": self._lease_sha256,
            "attempt_id": self._attempt_id,
            "attempt_sha256": self._attempt_sha256,
            "operation": self._operation,
            "request_sha256": self._request_sha256,
            "project_identity_sha256": self._project_identity_sha256,
            "mutation_run_sha256": self._mutation_run_sha256,
            "hierarchy_scope_sha256": self._hierarchy_scope_sha256,
        }

    def _publish(
        self,
        kind: Literal["process_spawn_attempt", "process_launch", "process_drain"],
        record: Mapping[str, object],
        *,
        predecessor_record_sha256: str | None,
    ) -> str:
        assert_current_writer(_RECORD_SCHEMAS[kind].removesuffix("/v1"), "1")
        digest = _record_sha256(record)
        payload: dict[str, object] = {
            "schema_version": _RECORD_SCHEMAS[kind],
            **self._common(),
            "record": dict(record),
            "record_sha256": digest,
            "predecessor_record_sha256": predecessor_record_sha256,
        }
        try:
            self._append_record(kind, payload)
        except BaseException:
            self._publication_ambiguous = True
            raise
        return digest

    def launch_binding(
        self,
        *,
        request_sha256: str,
        operation: Literal["commit", "cleanup"],
        project_identity_sha256: str,
        mutation_run_sha256: str,
        hierarchy_scope_sha256: str,
    ) -> ContainedMutationLaunchBinding:
        self._require_current()
        if self._binding is not None or self._drained:
            raise ROIAnalysisError("Warpy containment launch binding was already issued")
        for candidate, label in (
            (request_sha256, "request_sha256"),
            (project_identity_sha256, "project_identity_sha256"),
            (mutation_run_sha256, "mutation_run_sha256"),
            (hierarchy_scope_sha256, "hierarchy_scope_sha256"),
        ):
            _require_sha256(candidate, label)
        if type(operation) is not str or operation not in {"commit", "cleanup"}:
            raise ROIAnalysisError("Warpy containment operation is invalid")
        supplied = (
            request_sha256,
            operation,
            project_identity_sha256,
            mutation_run_sha256,
            hierarchy_scope_sha256,
        )
        expected = (
            self._request_sha256,
            self._operation,
            self._project_identity_sha256,
            self._mutation_run_sha256,
            self._hierarchy_scope_sha256,
        )
        if supplied != expected:
            raise ROIAnalysisError("Warpy containment launch scope changed")
        binding = ContainedMutationLaunchBinding(
            request_sha256=request_sha256,
            operation=operation,
            project_identity_sha256=project_identity_sha256,
            mutation_run_sha256=mutation_run_sha256,
            hierarchy_scope_sha256=hierarchy_scope_sha256,
        )
        self._binding = binding
        return binding

    def record_spawn_attempt(
        self, binding: ContainedMutationLaunchBinding
    ) -> ContainedMutationSpawnAttempt:
        self._require_current()
        if binding is not self._binding or self._attempt is not None or self._drained:
            raise ROIAnalysisError("Warpy containment spawn binding is foreign or out of order")
        attempt = ContainedMutationSpawnAttempt(binding=binding)
        digest = self._publish(
            "process_spawn_attempt",
            attempt.to_mapping(),
            predecessor_record_sha256=None,
        )
        self._attempt = attempt
        self._attempt_record_sha256 = digest
        return attempt

    def record_launch(
        self,
        attempt: ContainedMutationSpawnAttempt,
        evidence: ProcessContainmentLaunchEvidence,
    ) -> ContainedMutationLaunchRecord:
        self._require_current()
        if attempt is not self._attempt or self._launch is not None or self._drained:
            raise ROIAnalysisError("Warpy containment spawn attempt is foreign or out of order")
        launch = ContainedMutationLaunchRecord(
            spawn_attempt=attempt,
            evidence=evidence,
        )
        digest = self._publish(
            "process_launch",
            launch.to_mapping(),
            predecessor_record_sha256=self._attempt_record_sha256,
        )
        self._launch = launch
        self._launch_record_sha256 = digest
        return launch

    def record_drain(
        self,
        launch_or_attempt: ContainedMutationLaunchRecord | ContainedMutationSpawnAttempt,
        evidence: ProcessContainmentDrainEvidence,
    ) -> ContainedMutationDrainRecord:
        self._require_current()
        if self._drained:
            raise ROIAnalysisError("Warpy containment drain was already recorded")
        if launch_or_attempt is self._launch:
            publication_state: Literal["published", "publication_failed_before_launch_record"] = (
                "published"
            )
            predecessor = self._launch_record_sha256
        elif self._launch is None and launch_or_attempt is self._attempt:
            publication_state = "publication_failed_before_launch_record"
            predecessor = self._attempt_record_sha256
        else:
            raise ROIAnalysisError("Warpy containment drain predecessor is foreign or out of order")
        drain = ContainedMutationDrainRecord(
            launch_or_attempt=launch_or_attempt,
            evidence=evidence,
            launch_publication_state=publication_state,
        )
        self._publish(
            "process_drain",
            drain.to_mapping(),
            predecessor_record_sha256=predecessor,
        )
        self._drained = True
        return drain


__all__ = [
    "WarpyContainedMutationJournal",
    "WarpyContainmentLeaseDiagnostic",
    "diagnose_warpy_containment_lease",
    "validate_warpy_containment_record",
]
