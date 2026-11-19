"""Immutable state-owned checkpoints for completed native classifier fits.

These records are control-plane identity only. They never make a classifier scientifically
accepted and never carry classifier bytes.

The `BatchNativeFitCheckpointEntry` record carries `classifier_id`, `classifier_kind`,
`fit_plan_sha256`, `fit_execution_sha256`, `training_material_evidence_sha256` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, fields
from collections.abc import Mapping
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Literal
from types import MappingProxyType

from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2

from .batch_training._shared import BatchTrainingError

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_ID_RE = re.compile(r"attempt-[0-9a-f]{32}\Z")
_MAX_TEXT_BYTES = 4096
_MAX_CLASSIFIER_BYTES = 64 * 1024 * 1024 * 1024
_CLASSIFIER_KINDS = frozenset(
    {"primary_compartment", "secondary_structure", "supervised_phenotype"}
)


# ==============================================================================
#                           Batch training checkpoint
# ==============================================================================


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BatchTrainingError("native fit checkpoint is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _closed(value: object, *, expected: frozenset[str], label: str) -> dict[str, object]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != expected
    ):
        raise BatchTrainingError(f"{label} fields are invalid")
    return dict(value)


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise BatchTrainingError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict") if type(value) is str else b""
    except UnicodeError as exc:
        raise BatchTrainingError(f"{label} is invalid text") from exc
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(encoded) > _MAX_TEXT_BYTES
    ):
        raise BatchTrainingError(f"{label} is invalid text")
    return value


def _project_relative_path(value: object, native_type: str, fit_execution: str) -> str:
    text = _text(value, "project_relative_path")
    path = PurePosixPath(text)
    directory = "object_classifiers" if native_type == "object" else "pixel_classifiers"
    expected = f"classifiers/{directory}/mif-{fit_execution}.json"
    if (
        text != path.as_posix()
        or path.is_absolute()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or text != expected
    ):
        raise BatchTrainingError("project_relative_path is not the deterministic native target")
    return text


@dataclass(frozen=True, slots=True)
class BatchNativeFitCheckpointEntry:
    classifier_id: str
    classifier_kind: str
    fit_plan_sha256: str
    fit_execution_sha256: str
    training_material_evidence_sha256: str
    engine_id: str
    engine_version: str
    engine_capabilities_sha256: str
    native_classifier_type: Literal["pixel", "object"]
    classifier_version: str
    project_relative_path: str
    native_classifier_sha256: str
    native_classifier_byte_count: int
    project_identity_sha256: str
    predecessor_project_authority_head_sha256: str
    successor_project_authority_head_sha256: str
    qpproj_snapshot_sha256: str
    mutation_attestation_sha256: str
    runtime_fit_material_sha256: str
    checkpoint_entry_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchNativeFitCheckpointEntry fields when the record is constructed.

        The checks cover ``classifier_kind``, ``native_classifier_type``,
        ``project_relative_path``, ``fit_execution_sha256``, ``classifier_version``,
        ``checkpoint_entry_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        for name in (
            "fit_plan_sha256",
            "fit_execution_sha256",
            "training_material_evidence_sha256",
            "engine_capabilities_sha256",
            "native_classifier_sha256",
            "project_identity_sha256",
            "predecessor_project_authority_head_sha256",
            "successor_project_authority_head_sha256",
            "qpproj_snapshot_sha256",
            "mutation_attestation_sha256",
            "runtime_fit_material_sha256",
        ):
            _sha256(getattr(self, name), name)
        for name in ("classifier_id", "engine_id", "engine_version"):
            _text(getattr(self, name), name)
        classifier_kind = _text(self.classifier_kind, "classifier_kind")
        if classifier_kind not in _CLASSIFIER_KINDS:
            raise BatchTrainingError("classifier_kind is invalid")
        expected_type = "object" if classifier_kind == "supervised_phenotype" else "pixel"
        if self.native_classifier_type != expected_type:
            raise BatchTrainingError("native_classifier_type differs from classifier kind")
        expected_version = f"mif-fit-{self.fit_execution_sha256}"
        if type(self.classifier_version) is not str or self.classifier_version != expected_version:
            raise BatchTrainingError("classifier_version differs from fit execution")
        _project_relative_path(
            self.project_relative_path,
            self.native_classifier_type,
            self.fit_execution_sha256,
        )
        if (
            type(self.native_classifier_byte_count) is not int
            or not 1 <= self.native_classifier_byte_count <= _MAX_CLASSIFIER_BYTES
        ):
            raise BatchTrainingError("native_classifier_byte_count is invalid")
        if _sha256(self.checkpoint_entry_sha256, "checkpoint_entry_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise BatchTrainingError("checkpoint_entry_sha256 is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name != "checkpoint_entry_sha256"
        }

    @classmethod
    def create(cls, **values: object) -> "BatchNativeFitCheckpointEntry":
        unsigned = dict(values)
        if "checkpoint_entry_sha256" in unsigned:
            raise BatchTrainingError("checkpoint entry creation owns its digest")
        return cls(
            **unsigned,
            checkpoint_entry_sha256=_digest(unsigned),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchNativeFitCheckpointEntry":
        mapping = _closed(
            value,
            expected=frozenset(field.name for field in fields(cls)),
            label="native fit checkpoint entry",
        )
        return cls(**mapping)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "checkpoint_entry_sha256": self.checkpoint_entry_sha256}


@dataclass(frozen=True, slots=True)
class BatchNativeFitCheckpointSet:
    schema_name: Literal["mif-batch-native-fit-checkpoint-set"]
    schema_version: Literal[2]
    batch_id: str
    node_id: str
    originating_attempt_id: str
    originating_attempt_authorization_sha256: str
    admission_plan_sha256: str
    node_material_sha256: str
    input_artifact_set_sha256: str
    validation_domain_id: str
    training_plan_set_sha256: str
    entries: tuple[BatchNativeFitCheckpointEntry, ...]
    completed_lease_archive_ref_by_fit_material_sha256: Mapping[str, AuthorityMemberRefV2]
    checkpoint_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchNativeFitCheckpointSet fields when the record is constructed.

        The checks cover ``completed_lease_archive_ref_by_fit_material_sha256``,
        ``entries``, ``schema_name``, ``schema_version``, ``checkpoint_set_sha256``,
        ``originating_attempt_id``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (
            self.schema_name != "mif-batch-native-fit-checkpoint-set"
            or type(self.schema_version) is not int
            or self.schema_version != 2
        ):
            raise BatchTrainingError("native fit checkpoint set schema is unsupported")
        for name in ("batch_id", "node_id", "validation_domain_id"):
            _text(getattr(self, name), name)
        if (
            type(self.originating_attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(self.originating_attempt_id) is None
        ):
            raise BatchTrainingError("originating_attempt_id is invalid")
        for name in (
            "originating_attempt_authorization_sha256",
            "admission_plan_sha256",
            "node_material_sha256",
            "input_artifact_set_sha256",
            "training_plan_set_sha256",
        ):
            _sha256(getattr(self, name), name)
        entries = tuple(self.entries)
        if not entries or any(
            type(entry) is not BatchNativeFitCheckpointEntry for entry in entries
        ):
            raise BatchTrainingError("native fit checkpoint entries are invalid")
        canonical = tuple(
            sorted(entries, key=lambda item: (item.classifier_id, item.fit_execution_sha256))
        )
        if entries != canonical:
            raise BatchTrainingError("native fit checkpoint entries are not canonical")
        if len({item.classifier_id for item in entries}) != len(entries):
            raise BatchTrainingError("native fit checkpoint classifier IDs are not unique")
        if len({item.fit_execution_sha256 for item in entries}) != len(entries):
            raise BatchTrainingError("native fit checkpoint executions are not unique")
        kinds = {item.classifier_kind for item in entries}
        if len(kinds) != 1:
            raise BatchTrainingError("native fit checkpoint entries mix classifier kinds")
        if "secondary_structure" not in kinds and len(entries) != 1:
            raise BatchTrainingError("primary and supervised checkpoints require exactly one entry")
        object.__setattr__(self, "entries", entries)
        completions = self.completed_lease_archive_ref_by_fit_material_sha256
        if not isinstance(completions, Mapping):
            raise BatchTrainingError("native fit completion references must be a mapping")
        material_digests = {entry.runtime_fit_material_sha256 for entry in entries}
        checked = {}
        for digest, ref in completions.items():
            _sha256(digest, "completed fit material digest")
            if (
                digest not in material_digests
                or type(ref) is not AuthorityMemberRefV2
                or ref.family != "project_lease_archive"
            ):
                raise BatchTrainingError("native fit completion reference is foreign")
            checked[digest] = ref
        object.__setattr__(
            self,
            "completed_lease_archive_ref_by_fit_material_sha256",
            MappingProxyType(dict(sorted(checked.items()))),
        )
        if _sha256(self.checkpoint_set_sha256, "checkpoint_set_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise BatchTrainingError("checkpoint_set_sha256 is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "node_id": self.node_id,
            "originating_attempt_id": self.originating_attempt_id,
            "originating_attempt_authorization_sha256": (
                self.originating_attempt_authorization_sha256
            ),
            "admission_plan_sha256": self.admission_plan_sha256,
            "node_material_sha256": self.node_material_sha256,
            "input_artifact_set_sha256": self.input_artifact_set_sha256,
            "validation_domain_id": self.validation_domain_id,
            "training_plan_set_sha256": self.training_plan_set_sha256,
            "entries": [entry.to_mapping() for entry in self.entries],
            "completed_lease_archive_ref_by_fit_material_sha256": {
                digest: ref.to_mapping()
                for digest, ref in self.completed_lease_archive_ref_by_fit_material_sha256.items()
            },
        }

    @classmethod
    def create(cls, **values: object) -> "BatchNativeFitCheckpointSet":
        unsigned = {
            "schema_name": "mif-batch-native-fit-checkpoint-set",
            "schema_version": 2,
            "completed_lease_archive_ref_by_fit_material_sha256": {},
            **values,
        }
        if "checkpoint_set_sha256" in unsigned:
            raise BatchTrainingError("checkpoint set creation owns its digest")
        refs = unsigned["completed_lease_archive_ref_by_fit_material_sha256"]
        if not isinstance(refs, Mapping) or any(
            type(ref) is not AuthorityMemberRefV2 for ref in refs.values()
        ):
            raise BatchTrainingError("native fit completion references are invalid")
        raw_entries = unsigned.get("entries")
        if isinstance(raw_entries, (list, tuple)):
            unsigned["entries"] = tuple(
                sorted(
                    raw_entries,
                    key=lambda item: (item.classifier_id, item.fit_execution_sha256),
                )
            )
        digest_mapping = {
            **unsigned,
            "entries": [entry.to_mapping() for entry in unsigned["entries"]],
            "completed_lease_archive_ref_by_fit_material_sha256": {
                digest: ref.to_mapping() for digest, ref in refs.items()
            },
        }
        return cls(
            **unsigned,
            checkpoint_set_sha256=_digest(digest_mapping),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchNativeFitCheckpointSet":
        mapping = _closed(
            value,
            expected=frozenset(field.name for field in fields(cls)),
            label="native fit checkpoint set",
        )
        raw_entries = mapping.get("entries")
        if type(raw_entries) is not list:
            raise BatchTrainingError("native fit checkpoint entries must be an array")
        mapping["entries"] = tuple(
            BatchNativeFitCheckpointEntry.from_mapping(item) for item in raw_entries
        )
        refs = mapping["completed_lease_archive_ref_by_fit_material_sha256"]
        if type(refs) is not dict:
            raise BatchTrainingError("native fit completion references must be an object")
        try:
            mapping["completed_lease_archive_ref_by_fit_material_sha256"] = {
                key: AuthorityMemberRefV2.from_mapping(value) for key, value in refs.items()
            }
        except ValueError as exc:
            raise BatchTrainingError("native fit completion reference is invalid") from exc
        return cls(**mapping)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "checkpoint_set_sha256": self.checkpoint_set_sha256}

    def with_completed_lease_archive_reference(
        self,
        fit_material_sha256: str,
        archive_ref: AuthorityMemberRefV2,
    ) -> "BatchNativeFitCheckpointSet":
        """Construct only a completion-ref addition; never replace scientific data."""
        refs = dict(self.completed_lease_archive_ref_by_fit_material_sha256)
        if fit_material_sha256 in refs:
            if refs[fit_material_sha256] != archive_ref:
                raise BatchTrainingError("native fit completion cannot replace retained proof")
            return self
        refs[fit_material_sha256] = archive_ref
        values = self.to_mapping()
        values.pop("checkpoint_set_sha256")
        values["entries"] = self.entries
        values["completed_lease_archive_ref_by_fit_material_sha256"] = refs
        return type(self).create(**values)


__all__ = ["BatchNativeFitCheckpointEntry", "BatchNativeFitCheckpointSet"]
