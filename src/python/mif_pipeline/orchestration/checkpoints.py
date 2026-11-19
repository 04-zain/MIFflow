"""Append-only, scoped ROI-analysis checkpoint persistence.

The `CheckpointKey` record carries `slide_id`, `canonical_image_sha256`, `config_hash`,
`stage`, `stage_version` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from mif_pipeline.pipeline_core.errors import ROIAnalysisError
from mif_pipeline.qupath_runtime.exchange import (
    ExchangeContractError,
    StageExchangePaths,
    _atomic_write_json,
    _content_digest,
    _export_json,
    _freeze_json,
    _nonblank,
    _read_json,
    _safe_segment,
    _sha256,
)

CHECKPOINT_SCHEMA = "roi-checkpoint/v2"
SCOPED_KEY_SCHEMA = "roi-checkpoint-key/v2"
ACTIVE_POINTER_SCHEMA = "roi-checkpoint-active-pointer/v2"
ACCEPTED_ARTIFACT_SCHEMA = "roi-checkpoint-accepted-artifact/v2"


# ==============================================================================
#                                  Checkpoints
# ==============================================================================


class CheckpointConflictError(ROIAnalysisError):
    """Raised when persisted checkpoint state is corrupt or incompatible."""


@dataclass(frozen=True, slots=True)
class CheckpointInvocationScope:
    """Applicable biological identities for one checkpointed stage invocation."""

    region_id: str
    analysis_domain_id: str | None = None
    analysis_domain_revision_sha256: str | None = None
    structure_id: str | None = None
    classifier_id: str | None = None
    eligible_parent_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the CheckpointInvocationScope fields when the record is constructed.

        The checks cover ``region_id``, ``analysis_domain_id``,
        ``analysis_domain_revision_sha256``, ``structure_id``, ``classifier_id``,
        ``eligible_parent_ids``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        _nonblank(self.region_id, "region_id")
        for field in (
            "analysis_domain_id",
            "analysis_domain_revision_sha256",
            "structure_id",
            "classifier_id",
        ):
            value = getattr(self, field)
            if value is not None:
                _nonblank(value, field)
        domain_fields = (
            self.analysis_domain_id,
            self.analysis_domain_revision_sha256,
        )
        if (domain_fields[0] is None) != (domain_fields[1] is None):
            raise ROIAnalysisError(
                "analysis-domain ID and revision digest must be declared together"
            )
        if self.analysis_domain_revision_sha256 is not None:
            _sha256(self.analysis_domain_id, "analysis_domain_id")
            _sha256(
                self.analysis_domain_revision_sha256,
                "analysis_domain_revision_sha256",
            )
        parents = tuple(_nonblank(item, "eligible_parent_ids") for item in self.eligible_parent_ids)
        if len(set(parents)) != len(parents):
            raise ROIAnalysisError("eligible_parent_ids must be unique")
        object.__setattr__(self, "eligible_parent_ids", parents)
        if (self.structure_id is None) != (not parents):
            raise ROIAnalysisError("structure_id and eligible_parent_ids must be declared together")
        if self.structure_id is not None and self.classifier_id is None:
            raise ROIAnalysisError("a structure-scoped invocation requires classifier_id")
        if self.classifier_id is not None and self.analysis_domain_id is None:
            raise ROIAnalysisError(
                "current classifier invocation scopes require exact analysis-domain identity"
            )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"region_id": self.region_id}
        if self.analysis_domain_id is not None:
            result.update(
                {
                    "analysis_domain_id": self.analysis_domain_id,
                    "analysis_domain_revision_sha256": (self.analysis_domain_revision_sha256),
                }
            )
        if self.classifier_id is not None:
            result["classifier_id"] = self.classifier_id
        if self.structure_id is not None:
            result.update(
                {
                    "structure_id": self.structure_id,
                    "eligible_parent_ids": list(self.eligible_parent_ids),
                }
            )
        return result

    @classmethod
    def from_mapping(cls, value: object) -> "CheckpointInvocationScope":
        """Decode the serialized fields into a validated CheckpointInvocationScope record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        if not isinstance(value, Mapping):
            raise ROIAnalysisError("checkpoint invocation scope must be an object")
        region_fields = {"region_id"}
        domain_fields = {
            "region_id",
            "analysis_domain_id",
            "analysis_domain_revision_sha256",
        }
        primary_fields = domain_fields | {
            "classifier_id",
        }
        secondary_fields = primary_fields | {"structure_id", "eligible_parent_ids"}
        value_fields = set(value)
        if not any(
            value_fields == expected
            for expected in (region_fields, domain_fields, primary_fields, secondary_fields)
        ):
            raise ROIAnalysisError("checkpoint invocation scope fields do not match the schema")
        if value_fields == secondary_fields and value.get("structure_id") is None:
            raise ROIAnalysisError(
                "secondary checkpoint invocation requires structure_id and eligible_parent_ids"
            )
        raw_parents = value.get("eligible_parent_ids", ())
        if isinstance(raw_parents, (str, bytes)) or not isinstance(raw_parents, Sequence):
            raise ROIAnalysisError("checkpoint eligible_parent_ids must be a sequence")
        return cls(
            region_id=value["region_id"],
            analysis_domain_id=value.get("analysis_domain_id"),
            analysis_domain_revision_sha256=value.get("analysis_domain_revision_sha256"),
            structure_id=value.get("structure_id"),
            classifier_id=value.get("classifier_id"),
            eligible_parent_ids=tuple(raw_parents),
        )


_REQUIRED_SCOPE_FIELDS = frozenset(
    {
        "slide_id",
        "canonical_image_id",
        "canonical_image_sha256",
        "config_digest",
        "analysis_domain_config_digest",
        "generation_ids",
    }
)
_OPTIONAL_SCOPE_FIELDS = frozenset(
    {
        "domain_id",
        "domain_ids",
        "domain_digest",
        "domain_set_digest",
        "domain_stage_record_digests",
        "invocations",
    }
)


def _validated_checkpoint_scope(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("checkpoint scope must be a mapping")
    fields = set(value)
    if not _REQUIRED_SCOPE_FIELDS <= fields or not fields <= (
        _REQUIRED_SCOPE_FIELDS | _OPTIONAL_SCOPE_FIELDS
    ):
        raise ROIAnalysisError("checkpoint scope fields do not match the schema")
    parsed: dict[str, object] = {
        "slide_id": _nonblank(value["slide_id"], "slide_id"),
        "canonical_image_id": _nonblank(value["canonical_image_id"], "canonical_image_id"),
        "canonical_image_sha256": _sha256(
            value["canonical_image_sha256"], "canonical_image_sha256"
        ),
        "config_digest": _sha256(value["config_digest"], "config_digest"),
        "analysis_domain_config_digest": _sha256(
            value["analysis_domain_config_digest"],
            "analysis_domain_config_digest",
        ),
    }
    for field in ("domain_id",):
        if field in value:
            parsed[field] = _nonblank(value[field], field)
    for field in ("domain_digest", "domain_set_digest"):
        if field in value:
            parsed[field] = _sha256(value[field], field)
    for field in ("generation_ids", "domain_ids", "domain_stage_record_digests"):
        if field not in value:
            continue
        items = value[field]
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ROIAnalysisError(f"checkpoint {field} scope must be a sequence")
        parsed_items = tuple(
            (
                _sha256(item, field)
                if field == "domain_stage_record_digests"
                else _nonblank(item, field)
            )
            for item in items
        )
        if len(set(parsed_items)) != len(parsed_items):
            raise ROIAnalysisError(f"checkpoint {field} must be unique")
        parsed[field] = parsed_items
    raw_invocations = value.get("invocations", ())
    if isinstance(raw_invocations, (str, bytes)) or not isinstance(raw_invocations, Sequence):
        raise ROIAnalysisError("checkpoint invocations scope must be a sequence")
    invocations = tuple(
        (
            item
            if isinstance(item, CheckpointInvocationScope)
            else CheckpointInvocationScope.from_mapping(item)
        )
        for item in raw_invocations
    )
    invocation_mappings = tuple(item.to_mapping() for item in invocations)
    if len({_content_digest(item) for item in invocation_mappings}) != len(invocation_mappings):
        raise ROIAnalysisError("checkpoint invocation scopes must be unique")
    parsed["invocations"] = invocation_mappings
    return _freeze_json(parsed, "scope")


def _publication_json_bytes(value: object) -> bytes:
    return (
        json.dumps(_export_json(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _publish_json_exclusive(path: Path, content: bytes) -> bool:
    """Atomically publish complete JSON only when the destination is absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    """Audit-only v1 key shape retained without v2 resume eligibility."""

    slide_id: str
    canonical_image_sha256: str
    config_hash: str
    stage: str
    stage_version: str
    run_id: str
    generation_ids: tuple[str, ...] = ()
    canonical_image_id: str = "Image:0"

    def __post_init__(self) -> None:
        for field in ("slide_id", "canonical_image_id", "stage", "stage_version", "run_id"):
            _nonblank(getattr(self, field), field)
        _sha256(self.canonical_image_sha256, "canonical_image_sha256")
        _sha256(self.config_hash, "config_hash")
        generations = tuple(_nonblank(item, "generation_ids") for item in self.generation_ids)
        if len(set(generations)) != len(generations):
            raise ROIAnalysisError("generation_ids must be unique")
        object.__setattr__(self, "generation_ids", generations)

    def to_mapping(self) -> dict[str, object]:
        return {
            "slide_id": self.slide_id,
            "canonical_image_id": self.canonical_image_id,
            "canonical_image_sha256": self.canonical_image_sha256,
            "config_hash": self.config_hash,
            "stage": self.stage,
            "stage_version": self.stage_version,
            "run_id": self.run_id,
            "generation_ids": list(self.generation_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "CheckpointKey":
        if not isinstance(value, Mapping):
            raise CheckpointConflictError("checkpoint key must be an object")
        required = {
            "slide_id",
            "canonical_image_id",
            "canonical_image_sha256",
            "config_hash",
            "stage",
            "stage_version",
            "run_id",
            "generation_ids",
        }
        if set(value) != required:
            raise CheckpointConflictError("checkpoint key fields do not match the schema")
        generations = value["generation_ids"]
        if isinstance(generations, (str, bytes)) or not isinstance(generations, Sequence):
            raise CheckpointConflictError("checkpoint generation_ids must be a sequence")
        try:
            return cls(
                slide_id=value["slide_id"],
                canonical_image_id=value["canonical_image_id"],
                canonical_image_sha256=value["canonical_image_sha256"],
                config_hash=value["config_hash"],
                stage=value["stage"],
                stage_version=value["stage_version"],
                run_id=value["run_id"],
                generation_ids=tuple(generations),
            )
        except (KeyError, TypeError, ROIAnalysisError) as exc:
            raise CheckpointConflictError("invalid checkpoint key") from exc

    def digest(self) -> str:
        return _content_digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """Audit-only v1 record shape retained for public API compatibility."""

    key: CheckpointKey
    payload: Mapping[str, Any]
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise ROIAnalysisError("checkpoint payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(self.payload, "payload"))

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key.to_mapping(), "payload": _export_json(self.payload)}

    def resume_context(self, *, checkpoint_id: str) -> dict[str, object]:
        return {
            "checkpoint_id": _nonblank(checkpoint_id, "checkpoint_id"),
            "checkpoint_key": self.key.to_mapping(),
            "checkpoint_path": str(self.path),
        }


@dataclass(frozen=True, slots=True)
class ScopedCheckpointKey:
    """Exact run/stage scope for one independently reusable checkpoint lineage."""

    run_id: str
    stage: str
    stage_version: str
    scope: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field in ("run_id", "stage", "stage_version"):
            _nonblank(getattr(self, field), field)
        object.__setattr__(self, "scope", _validated_checkpoint_scope(self.scope))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SCOPED_KEY_SCHEMA,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_version": self.stage_version,
            "scope": _export_json(self.scope),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ScopedCheckpointKey":
        if not isinstance(value, Mapping):
            raise CheckpointConflictError("scoped checkpoint key must be an object")
        required = {"schema_version", "run_id", "stage", "stage_version", "scope"}
        if set(value) != required or value.get("schema_version") != SCOPED_KEY_SCHEMA:
            raise CheckpointConflictError("checkpoint resume requires a v2 scoped key")
        try:
            return cls(
                run_id=value["run_id"],
                stage=value["stage"],
                stage_version=value["stage_version"],
                scope=value["scope"],
            )
        except (KeyError, TypeError, ROIAnalysisError) as exc:
            raise CheckpointConflictError("invalid scoped checkpoint key") from exc

    def digest(self) -> str:
        return _content_digest(self.to_mapping())

    def scope_digest(self) -> str:
        return _content_digest(
            {
                "stage_version": self.stage_version,
                "scope": _export_json(self.scope),
            }
        )

    def _scope_text(self, field: str) -> str:
        try:
            return _nonblank(self.scope[field], field)
        except KeyError as exc:
            raise ROIAnalysisError(f"checkpoint scope lacks {field}") from exc

    @property
    def slide_id(self) -> str:
        return self._scope_text("slide_id")

    @property
    def canonical_image_id(self) -> str:
        return self._scope_text("canonical_image_id")

    @property
    def canonical_image_sha256(self) -> str:
        return _sha256(self.scope.get("canonical_image_sha256"), "canonical_image_sha256")

    @property
    def config_hash(self) -> str:
        value = self.scope.get("config_digest", self.scope.get("config_hash"))
        return _sha256(value, "config_digest")

    @property
    def generation_ids(self) -> tuple[str, ...]:
        value = self.scope.get("generation_ids", ())
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ROIAnalysisError("checkpoint generation_ids scope must be a sequence")
        return tuple(_nonblank(item, "generation_ids") for item in value)


@dataclass(frozen=True, slots=True)
class CheckpointGeneration:
    """One immutable canonical checkpoint record addressed by its digest."""

    key: ScopedCheckpointKey
    generation_id: str
    record: Mapping[str, Any]
    path: Path

    def __post_init__(self) -> None:
        _sha256(self.generation_id, "generation_id")
        if not isinstance(self.record, Mapping):
            raise ROIAnalysisError("checkpoint generation record must be a mapping")
        object.__setattr__(self, "record", _freeze_json(self.record, "record"))

    @property
    def payload(self) -> Mapping[str, Any]:
        """Compatibility name for consumers migrating from checkpoint v1."""
        return self.record

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key.to_mapping(), "record": _export_json(self.record)}

    def resume_context(self, *, checkpoint_id: str) -> dict[str, object]:
        return {
            "checkpoint_id": _nonblank(checkpoint_id, "checkpoint_id"),
            "checkpoint_key": self.key.to_mapping(),
            "checkpoint_generation_id": self.generation_id,
            "checkpoint_path": str(self.path),
        }


class CheckpointStore:
    """Publish immutable generations and atomically select one active generation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def scope_path_for(self, key: ScopedCheckpointKey) -> Path:
        return (
            self.root
            / "runs"
            / _safe_segment(key.run_id, "run_id")
            / "checkpoints"
            / _safe_segment(key.stage, "stage")
            / key.scope_digest()
        )

    def generation_path_for(self, key: ScopedCheckpointKey, *, generation_id: str) -> Path:
        digest = _sha256(generation_id, "generation_id")
        return self.scope_path_for(key) / "generations" / f"{digest}.json"

    def active_pointer_path_for(self, key: ScopedCheckpointKey) -> Path:
        return self.scope_path_for(key) / "active.v2.json"

    def accepted_artifact_path_for(self, key: ScopedCheckpointKey, *, artifact_id: str) -> Path:
        return (
            self.scope_path_for(key)
            / "accepted-artifacts"
            / f"{_safe_segment(artifact_id, 'artifact_id')}.v2.json"
        )

    def publish_generation(
        self,
        key: ScopedCheckpointKey,
        record: Mapping[str, Any],
    ) -> CheckpointGeneration:
        if not isinstance(key, ScopedCheckpointKey):
            raise ROIAnalysisError("checkpoint key must be a ScopedCheckpointKey")
        if not isinstance(record, Mapping):
            raise ROIAnalysisError("checkpoint generation record must be a mapping")
        frozen = _freeze_json(record, "record")
        body = {
            "schema_version": CHECKPOINT_SCHEMA,
            "key": key.to_mapping(),
            "key_digest": key.digest(),
            "record": _export_json(frozen),
            "record_sha256": _content_digest(frozen),
        }
        generation_id = _content_digest(body)
        envelope = {**body, "generation_id": generation_id}
        path = self.generation_path_for(key, generation_id=generation_id)
        if path.exists():
            existing = self._read_generation_path(path)
            if existing.key != key or existing.record != frozen:
                raise CheckpointConflictError("checkpoint generation is immutable")
            generation = existing
        else:
            _atomic_write_json(path, envelope)
            generation = CheckpointGeneration(
                key=key,
                generation_id=generation_id,
                record=frozen,
                path=path,
            )
        self._write_pointer(key, generation_id=generation_id, status="active", reason=None)
        return generation

    def load_active(self, key: ScopedCheckpointKey) -> CheckpointGeneration | None:
        path = self.active_pointer_path_for(key)
        if not path.exists():
            return None
        pointer = self._read_pointer(path)
        if pointer["key"] != key.to_mapping():
            raise CheckpointConflictError("active checkpoint scope does not match")
        if pointer["status"] == "retired":
            return None
        generation = self._read_generation_path(
            self.generation_path_for(key, generation_id=pointer["generation_id"])
        )
        if generation.key != key:
            raise CheckpointConflictError("active checkpoint generation scope does not match")
        return generation

    def load_generation(self, generation_id: str) -> CheckpointGeneration:
        digest = _sha256(generation_id, "generation_id")
        matches = tuple(self.root.glob(f"runs/*/checkpoints/*/*/generations/{digest}.json"))
        if not matches:
            raise CheckpointConflictError(f"checkpoint generation does not exist: {generation_id}")
        generations = tuple(self._read_generation_path(path) for path in matches)
        first = generations[0]
        if any(item != first for item in generations[1:]):
            raise CheckpointConflictError("checkpoint generation digest is ambiguous")
        return first

    def retire_active(self, key: ScopedCheckpointKey, *, reason: str) -> Path | None:
        path = self.active_pointer_path_for(key)
        if not path.exists():
            return None
        pointer = self._read_pointer(path)
        if pointer["key"] != key.to_mapping():
            raise CheckpointConflictError("active checkpoint scope does not match")
        if pointer["status"] == "retired":
            return path
        self._write_pointer(
            key,
            generation_id=pointer["generation_id"],
            status="retired",
            reason=_nonblank(reason, "retirement reason"),
        )
        return path

    def retire_active_generation(
        self,
        key: ScopedCheckpointKey,
        *,
        generation_id: str,
        reason: str,
    ) -> Path | None:
        """Retire only the exact generation still selected by the active pointer."""
        expected = _sha256(generation_id, "generation_id")
        path = self.active_pointer_path_for(key)
        if not path.exists():
            return None
        pointer = self._read_pointer(path)
        if pointer["key"] != key.to_mapping():
            raise CheckpointConflictError("active checkpoint scope does not match")
        if pointer["status"] == "retired" or pointer["generation_id"] != expected:
            return None
        self._write_pointer(
            key,
            generation_id=expected,
            status="retired",
            reason=_nonblank(reason, "retirement reason"),
        )
        return path

    def publish_accepted_artifact(
        self,
        key: ScopedCheckpointKey,
        *,
        artifact_id: str,
        record: Mapping[str, Any],
    ) -> CheckpointGeneration:
        if not isinstance(record, Mapping):
            raise ROIAnalysisError("accepted artifact record must be a mapping")
        frozen = _freeze_json(record, "accepted artifact record")
        body = {
            "schema_version": ACCEPTED_ARTIFACT_SCHEMA,
            "key": key.to_mapping(),
            "key_digest": key.digest(),
            "artifact_id": _nonblank(artifact_id, "artifact_id"),
            "record": _export_json(frozen),
            "record_sha256": _content_digest(frozen),
        }
        artifact_digest = _content_digest(body)
        envelope = {**body, "artifact_digest": artifact_digest}
        path = self.accepted_artifact_path_for(key, artifact_id=artifact_id)
        intended_bytes = _publication_json_bytes(envelope)
        if _publish_json_exclusive(path, intended_bytes):
            return CheckpointGeneration(
                key=key,
                generation_id=artifact_digest,
                record=frozen,
                path=path,
            )
        try:
            existing_bytes = path.read_bytes()
        except OSError as exc:
            raise CheckpointConflictError(
                "accepted interaction artifact cannot be verified"
            ) from exc
        if existing_bytes != intended_bytes:
            raise CheckpointConflictError("accepted interaction artifact is immutable")
        existing = self._read_accepted_artifact_path(path)
        if (
            existing.key == key
            and existing.generation_id == artifact_digest
            and existing.record == frozen
        ):
            return existing
        raise CheckpointConflictError("accepted interaction artifact is immutable")

    def load_accepted_artifact(
        self, key: ScopedCheckpointKey, *, artifact_id: str
    ) -> CheckpointGeneration:
        path = self.accepted_artifact_path_for(key, artifact_id=artifact_id)
        if not path.exists():
            raise CheckpointConflictError(f"accepted interaction artifact does not exist: {path}")
        artifact = self._read_accepted_artifact_path(path)
        if artifact.key != key:
            raise CheckpointConflictError("accepted interaction artifact context does not match")
        return artifact

    @staticmethod
    def parse_resume_context(
        context: Mapping[str, Any],
    ) -> tuple[ScopedCheckpointKey, str]:
        if not isinstance(context, Mapping):
            raise CheckpointConflictError("resume context must be an object")
        try:
            key = ScopedCheckpointKey.from_mapping(context["checkpoint_key"])
            checkpoint_id = _nonblank(context["checkpoint_id"], "checkpoint_id")
        except (KeyError, ROIAnalysisError) as exc:
            raise CheckpointConflictError(
                "resume context lacks a valid v2 scoped checkpoint key"
            ) from exc
        return key, checkpoint_id

    def find_active_for_stage(self, *, run_id: str, stage: str) -> CheckpointGeneration | None:
        stage_root = (
            self.root
            / "runs"
            / _safe_segment(run_id, "run_id")
            / "checkpoints"
            / _safe_segment(stage, "stage")
        )
        active: list[CheckpointGeneration] = []
        for path in sorted(stage_root.glob("*/active.v2.json")):
            pointer = self._read_pointer(path)
            key = ScopedCheckpointKey.from_mapping(pointer["key"])
            self._validate_discovered_pointer(path, key=key, run_id=run_id, stage=stage)
            if pointer["status"] != "active":
                continue
            generation = self.load_active(key)
            if generation is not None:
                active.append(generation)
        if len(active) > 1:
            raise CheckpointConflictError("multiple active checkpoint scopes exist for stage")
        return None if not active else active[0]

    def retire_from(
        self,
        *,
        run_id: str,
        changed_stage: str,
        ordered_stages: Sequence[str],
        reason: str,
    ) -> tuple[Path, ...]:
        stages = self._validated_stages(changed_stage, ordered_stages)
        return self._retire_stages(
            run_id=run_id,
            stages=stages[stages.index(changed_stage) :],
            reason=reason,
        )

    def retire_downstream(
        self,
        *,
        run_id: str,
        changed_stage: str,
        ordered_stages: Sequence[str],
        reason: str,
    ) -> tuple[Path, ...]:
        stages = self._validated_stages(changed_stage, ordered_stages)
        return self._retire_stages(
            run_id=run_id,
            stages=stages[stages.index(changed_stage) + 1 :],
            reason=reason,
        )

    def _retire_stages(
        self, *, run_id: str, stages: Sequence[str], reason: str
    ) -> tuple[Path, ...]:
        retired: list[Path] = []
        run_root = self.root / "runs" / _safe_segment(run_id, "run_id") / "checkpoints"
        for stage in stages:
            for pointer_path in sorted(
                (run_root / _safe_segment(stage, "stage")).glob("*/active.v2.json")
            ):
                pointer = self._read_pointer(pointer_path)
                key = ScopedCheckpointKey.from_mapping(pointer["key"])
                self._validate_discovered_pointer(pointer_path, key=key, run_id=run_id, stage=stage)
                if pointer["status"] != "active":
                    continue
                path = self.retire_active(key, reason=reason)
                if path is not None:
                    retired.append(path)
        return tuple(retired)

    def _validate_discovered_pointer(
        self,
        pointer_path: Path,
        *,
        key: ScopedCheckpointKey,
        run_id: str,
        stage: str,
    ) -> None:
        if key.run_id != run_id or key.stage != stage:
            raise CheckpointConflictError(
                "active checkpoint pointer does not match requested run/stage scope"
            )
        if pointer_path != self.active_pointer_path_for(key):
            raise CheckpointConflictError(
                "active checkpoint pointer path does not match its embedded scope"
            )

    @staticmethod
    def _validated_stages(changed_stage: str, ordered_stages: Sequence[str]) -> tuple[str, ...]:
        stages = tuple(_nonblank(item, "ordered_stages") for item in ordered_stages)
        if len(set(stages)) != len(stages) or changed_stage not in stages:
            raise ROIAnalysisError("ordered_stages must uniquely contain changed_stage")
        return stages

    def _write_pointer(
        self,
        key: ScopedCheckpointKey,
        *,
        generation_id: str,
        status: str,
        reason: str | None,
    ) -> None:
        if status not in {"active", "retired"}:
            raise ROIAnalysisError("checkpoint pointer status is invalid")
        if (status == "active") != (reason is None):
            raise ROIAnalysisError("only retired checkpoint pointers carry a reason")
        body = {
            "schema_version": ACTIVE_POINTER_SCHEMA,
            "key": key.to_mapping(),
            "key_digest": key.digest(),
            "status": status,
            "generation_id": _sha256(generation_id, "generation_id"),
            "generation_sha256": generation_id,
            "reason": reason,
        }
        _atomic_write_json(
            self.active_pointer_path_for(key),
            {**body, "pointer_sha256": _content_digest(body)},
        )

    def _read_pointer(self, path: Path) -> Mapping[str, Any]:
        envelope = _read_json(path, CheckpointConflictError)
        required = {
            "schema_version",
            "key",
            "key_digest",
            "status",
            "generation_id",
            "generation_sha256",
            "reason",
            "pointer_sha256",
        }
        if set(envelope) != required or envelope.get("schema_version") != ACTIVE_POINTER_SCHEMA:
            raise CheckpointConflictError("active checkpoint pointer schema is invalid")
        body = {key: value for key, value in envelope.items() if key != "pointer_sha256"}
        if envelope["pointer_sha256"] != _content_digest(body):
            raise CheckpointConflictError("active checkpoint pointer checksum is invalid")
        key = ScopedCheckpointKey.from_mapping(envelope["key"])
        if envelope["key_digest"] != key.digest():
            raise CheckpointConflictError("active checkpoint key checksum is invalid")
        if envelope["status"] not in {"active", "retired"}:
            raise CheckpointConflictError("active checkpoint pointer status is invalid")
        if envelope["generation_sha256"] != envelope["generation_id"]:
            raise CheckpointConflictError("active checkpoint generation checksum is invalid")
        _sha256(envelope["generation_id"], "generation_id")
        if (envelope["status"] == "active") != (envelope["reason"] is None):
            raise CheckpointConflictError("active checkpoint retirement reason is invalid")
        return MappingProxyType(dict(envelope))

    def _read_generation_path(self, path: Path) -> CheckpointGeneration:
        if not path.exists():
            raise CheckpointConflictError(f"checkpoint generation does not exist: {path}")
        envelope = _read_json(path, CheckpointConflictError)
        required = {
            "schema_version",
            "key",
            "key_digest",
            "record",
            "record_sha256",
            "generation_id",
        }
        if set(envelope) != required or envelope.get("schema_version") != CHECKPOINT_SCHEMA:
            raise CheckpointConflictError("checkpoint generation schema is invalid")
        key = ScopedCheckpointKey.from_mapping(envelope["key"])
        if envelope["key_digest"] != key.digest():
            raise CheckpointConflictError("checkpoint generation key checksum is invalid")
        record = envelope["record"]
        if not isinstance(record, Mapping):
            raise CheckpointConflictError("checkpoint generation record must be an object")
        if envelope["record_sha256"] != _content_digest(record):
            raise CheckpointConflictError("checkpoint generation record checksum is invalid")
        body = {key: value for key, value in envelope.items() if key != "generation_id"}
        generation_id = envelope["generation_id"]
        if generation_id != _content_digest(body) or path.stem != generation_id:
            raise CheckpointConflictError("checkpoint generation checksum is invalid")
        return CheckpointGeneration(key=key, generation_id=generation_id, record=record, path=path)

    def _read_accepted_artifact_path(self, path: Path) -> CheckpointGeneration:
        envelope = _read_json(path, CheckpointConflictError)
        required = {
            "schema_version",
            "key",
            "key_digest",
            "artifact_id",
            "record",
            "record_sha256",
            "artifact_digest",
        }
        if set(envelope) != required or envelope.get("schema_version") != ACCEPTED_ARTIFACT_SCHEMA:
            raise CheckpointConflictError("accepted interaction artifact schema is invalid")
        key = ScopedCheckpointKey.from_mapping(envelope["key"])
        if envelope["key_digest"] != key.digest():
            raise CheckpointConflictError("accepted interaction artifact key checksum is invalid")
        record = envelope["record"]
        if not isinstance(record, Mapping) or envelope["record_sha256"] != _content_digest(record):
            raise CheckpointConflictError(
                "accepted interaction artifact record checksum is invalid"
            )
        body = {key: value for key, value in envelope.items() if key != "artifact_digest"}
        digest = envelope["artifact_digest"]
        if digest != _content_digest(body):
            raise CheckpointConflictError("accepted interaction artifact checksum is invalid")
        return CheckpointGeneration(key=key, generation_id=digest, record=record, path=path)

    # Compatibility facades retain callability while enforcing v2 append-only semantics.
    def path_for(self, key: ScopedCheckpointKey) -> Path:
        return self.active_pointer_path_for(key)

    def write(
        self,
        key: ScopedCheckpointKey,
        payload: Mapping[str, Any],
        *,
        overwrite: bool = False,
    ) -> CheckpointGeneration:
        del overwrite
        return self.publish_generation(key, payload)

    def read(self, expected_key: ScopedCheckpointKey) -> CheckpointGeneration:
        generation = self.load_active(expected_key)
        if generation is None:
            raise CheckpointConflictError("active checkpoint does not exist")
        return generation

    def find_for_stage(self, *, run_id: str, stage: str) -> CheckpointGeneration | None:
        return self.find_active_for_stage(run_id=run_id, stage=stage)

    def write_accepted_artifact(
        self,
        key: ScopedCheckpointKey,
        *,
        artifact_id: str,
        payload: Mapping[str, Any],
    ) -> CheckpointGeneration:
        return self.publish_accepted_artifact(key, artifact_id=artifact_id, record=payload)

    def read_accepted_artifact(
        self, key: ScopedCheckpointKey, *, artifact_id: str
    ) -> CheckpointGeneration:
        return self.load_accepted_artifact(key, artifact_id=artifact_id)

    def invalidate_from(
        self,
        *,
        run_id: str,
        changed_stage: str,
        ordered_stages: Sequence[str],
    ) -> tuple[Path, ...]:
        return self.retire_from(
            run_id=run_id,
            changed_stage=changed_stage,
            ordered_stages=ordered_stages,
            reason="legacy_invalidation_request",
        )

    def invalidate_downstream(
        self,
        *,
        run_id: str,
        changed_stage: str,
        ordered_stages: Sequence[str],
    ) -> tuple[Path, ...]:
        return self.retire_downstream(
            run_id=run_id,
            changed_stage=changed_stage,
            ordered_stages=ordered_stages,
            reason="legacy_invalidation_request",
        )


__all__ = [
    "CHECKPOINT_SCHEMA",
    "CheckpointConflictError",
    "CheckpointGeneration",
    "CheckpointInvocationScope",
    "CheckpointKey",
    "CheckpointRecord",
    "CheckpointStore",
    "ExchangeContractError",
    "ScopedCheckpointKey",
    "StageExchangePaths",
]
