"""Represent the editable image, sample and execution inputs used to create an analysis
manifest.

Draft records retain incomplete researcher choices without losing sample and subject
identity. The validation boundary resolves the supplied paths and per-sample settings, then
writes the complete manifest through the existing schema and output-location checks."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import mkstemp
from types import MappingProxyType

from mif_pipeline.orchestration.batch_manifest import (
    BatchManifest,
    BatchManifestError,
    ValidatedBatchManifest,
    validate_manifest,
    revalidate_retained_manifest,
)
from mif_pipeline.pipeline_core import AnalysisConfig, ROIAnalysisError

# ==============================================================================
#                                 Manifest draft
# ==============================================================================


class ProjectChoice(str, Enum):
    """The one project locator role selected by the author."""

    CREATE = "create"
    OPEN = "open"


def _local_path(value: Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _freeze_json(value: object) -> object:
    """Retain editable JSON input without sharing mutable nested containers."""

    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and isfinite(value):
        return value
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("nested draft value must contain finite JSON values")


def _json_mapping(value: Mapping[str, object]) -> dict[str, object]:
    def thaw(item: object) -> object:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [thaw(child) for child in item]
        return item

    return {key: thaw(item) for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class ExecutionDraft:
    """Presentation-owned scheduler inputs awaiting owner validation."""

    max_workers: int
    max_in_flight: int
    resource_capacities: Mapping[str, int]
    # Read-only provenance from a saved definition; omitted from new analyses.
    max_working_set_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_capacities",
            MappingProxyType(dict(self.resource_capacities)),
        )

    def to_mapping(self) -> dict[str, object]:
        value = {"max_workers": self.max_workers, "max_in_flight": self.max_in_flight,
                 "resource_capacities": dict(self.resource_capacities)}
        if self.max_working_set_bytes is not None:
            value["max_working_set_bytes"] = self.max_working_set_bytes
        return value


@dataclass(frozen=True, slots=True)
class SampleDraft:
    """One editable sample row without scientific authority."""

    enabled: bool
    sample_id: str
    slide_id: str
    subject_id: str
    validation_domain_id: str | None
    source_mif_path: Path
    moving_reference_path: Path | None
    moving_reference_modality: str | None
    source_image_index: int | None = None
    source_series_index: int | None = None
    source_file_sha256: str | None = None
    moving_reference_id: str | None = None
    moving_reference_source_image_id: str | None = None
    moving_reference_source_metadata: Mapping[str, object] = field(default_factory=dict)
    overrides: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        indices = (self.source_image_index, self.source_series_index)
        if any(value is not None for value in indices) and not all(
            type(value) is int and value >= 0 for value in indices
        ):
            raise ValueError("source image and series indices must be paired nonnegative integers")
        if self.source_file_sha256 is not None and (
            self.source_image_index is None
            or type(self.source_file_sha256) is not str
            or len(self.source_file_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_file_sha256)
        ):
            raise ValueError("source file SHA-256 requires a selected pair and lowercase digest")
        object.__setattr__(self, "source_mif_path", _local_path(self.source_mif_path))
        if self.moving_reference_path is not None:
            object.__setattr__(
                self,
                "moving_reference_path",
                _local_path(self.moving_reference_path),
            )
        for field in ("moving_reference_source_metadata", "overrides"):
            frozen = _freeze_json(getattr(self, field))
            if not isinstance(frozen, Mapping):
                raise ValueError(f"{field} must be a JSON object")
            object.__setattr__(self, field, frozen)


@dataclass(frozen=True, slots=True)
class ReusableArtifactDraft:
    artifact_id: str
    artifact_kind: str
    schema_name: str
    schema_version: str
    locator: Path
    sha256: str
    byte_count: int
    owning_identity: Mapping[str, object]
    runtime_versions: Mapping[str, str]
    validation_domain_id: str | None
    compatibility_signature_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "locator", _local_path(self.locator))
        identity = _freeze_json(self.owning_identity)
        versions = _freeze_json(self.runtime_versions)
        if not isinstance(identity, Mapping) or not isinstance(versions, Mapping):
            raise ValueError("artifact identity and runtime versions must be JSON objects")
        object.__setattr__(self, "owning_identity", identity)
        object.__setattr__(self, "runtime_versions", versions)

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "locator": str(self.locator),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "owning_identity": _json_mapping(self.owning_identity),
            "runtime_versions": _json_mapping(self.runtime_versions),
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
        }
        if self.validation_domain_id is not None:
            value["validation_domain_id"] = self.validation_domain_id
        return value


@dataclass(frozen=True, slots=True)
class ManifestDraft:
    """Complete presentation-owned input for one strict batch manifest."""

    batch_id: str
    validation_domain_id: str
    output_root: Path
    project_choice: ProjectChoice
    project_path: Path
    shared_analysis_config_path: Path
    execution: ExecutionDraft
    samples: tuple[SampleDraft, ...]
    validation_domains: tuple[str, ...] = ()
    reusable_artifacts: tuple[ReusableArtifactDraft, ...] = ()

    def __post_init__(self) -> None:
        choice = ProjectChoice(self.project_choice)
        samples = tuple(self.samples)
        if not isinstance(self.execution, ExecutionDraft):
            raise TypeError("execution must be an ExecutionDraft")
        if not all(isinstance(item, SampleDraft) for item in samples):
            raise TypeError("samples must contain SampleDraft values")
        artifacts = tuple(self.reusable_artifacts)
        if not all(isinstance(item, ReusableArtifactDraft) for item in artifacts):
            raise TypeError("reusable_artifacts must contain ReusableArtifactDraft values")
        object.__setattr__(self, "validation_domains", tuple(self.validation_domains))
        object.__setattr__(self, "reusable_artifacts", artifacts)
        object.__setattr__(self, "project_choice", choice)
        object.__setattr__(self, "output_root", _local_path(self.output_root))
        object.__setattr__(self, "project_path", _local_path(self.project_path))
        object.__setattr__(
            self,
            "shared_analysis_config_path",
            _local_path(self.shared_analysis_config_path),
        )
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True, slots=True)
class DraftFieldError:
    """One owner rejection bound to a form field or sample-table cell."""

    field: str
    message: str
    row_index: int | None = None

    @property
    def row(self) -> int | None:
        """Concise alias used by table-oriented presentation adapters."""

        return self.row_index


@dataclass(frozen=True, slots=True)
class ManifestDraftValidation:
    """Either accepted owner evidence or immutable presentation errors."""

    draft: ManifestDraft
    errors: tuple[DraftFieldError, ...]
    validated_manifest: ValidatedBatchManifest | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.validated_manifest is not None

    @property
    def manifest(self) -> ValidatedBatchManifest | None:
        """Compatibility alias for callers that do not need the qualifier."""

        return self.validated_manifest


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON object has duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"JSON contains a non-finite number {value!r}")


def _read_json_object(path: Path) -> Mapping[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read JSON object from {path}: {exc}") from exc
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise ValueError(f"JSON document at {path} must contain one object")
    return value


def _load_shared_analysis_config(path: Path) -> Mapping[str, object]:
    mapping = _read_json_object(path)
    # A loaded strict manifest is a read-only container for its embedded config.
    # The batch schema intentionally has no field for the original config locator.
    if mapping.get("schema_name") == "mif-batch-manifest":
        embedded = mapping.get("shared_analysis_config")
        if not isinstance(embedded, Mapping):
            raise ValueError("batch manifest shared_analysis_config must be an object")
        mapping = embedded
    return AnalysisConfig.from_mapping(mapping).to_mapping()


def _sample_mapping(sample: SampleDraft) -> dict[str, object]:
    value: dict[str, object] = {
        "sample_id": sample.sample_id,
        "slide_id": sample.slide_id,
        "subject_id": sample.subject_id,
        "source_mif_path": str(sample.source_mif_path),
        "enabled": sample.enabled,
    }
    if sample.source_image_index is not None:
        value["source_image_index"] = sample.source_image_index
        value["source_series_index"] = sample.source_series_index
        if sample.source_file_sha256 is not None:
            value["source_file_sha256"] = sample.source_file_sha256
    for field in ():
        item = getattr(sample, field)
        if item is not None:
            value[field] = item
    if sample.validation_domain_id is not None:
        value["validation_domain_id"] = sample.validation_domain_id
    if (
        sample.moving_reference_path is not None
        or sample.moving_reference_modality is not None
        or sample.moving_reference_id is not None
        or sample.moving_reference_source_image_id is not None
        or bool(sample.moving_reference_source_metadata)
    ):
        reference: dict[str, object] = {
            "reference_id": sample.moving_reference_id,
        }
        if sample.moving_reference_path is not None:
            reference["source_path"] = str(sample.moving_reference_path)
        if sample.moving_reference_modality is not None:
            reference["modality"] = sample.moving_reference_modality
        if sample.moving_reference_source_image_id is not None:
            reference["source_image_id"] = sample.moving_reference_source_image_id
        if sample.moving_reference_source_metadata:
            reference["source_metadata"] = _json_mapping(sample.moving_reference_source_metadata)
        value["paired_reference_image"] = reference
    if sample.overrides:
        value["overrides"] = _json_mapping(sample.overrides)
    return value


def _declared_validation_domains(draft: ManifestDraft) -> list[str]:
    """Retain explicit declarations; the default domain is implicit."""

    return sorted({*draft.validation_domains, draft.validation_domain_id.strip()})


def _manifest_mapping(
    draft: ManifestDraft,
    shared_analysis_config: Mapping[str, object],
) -> dict[str, object]:
    samples = [_sample_mapping(item) for item in draft.samples]
    value: dict[str, object] = {
        "schema_name": "mif-batch-manifest",
        "schema_version": "1",
        "batch_id": draft.batch_id,
        "validation_domain_id": draft.validation_domain_id,
        "output_root": str(draft.output_root),
        "shared_analysis_config": dict(shared_analysis_config),
        "execution_config": draft.execution.to_mapping(),
        "samples": samples,
    }
    declared_domains = _declared_validation_domains(draft)
    if declared_domains != [draft.validation_domain_id.strip()]:
        value["validation_domains"] = declared_domains
    if draft.reusable_artifacts:
        value["reusable_artifacts"] = [item.to_mapping() for item in draft.reusable_artifacts]
    if draft.project_choice is ProjectChoice.CREATE:
        value["create_project_path"] = str(draft.project_path)
    else:
        value["project_path"] = str(draft.project_path)
    return value


_SAMPLE_FIELD_PATTERN = re.compile(r"samples\[(?P<row>\d+)]\.(?P<field>[a-z_0-9]+)")
_EXECUTION_FIELD_PATTERN = re.compile(
    r"execution_config\.(?:resource_capacities\.)?(?P<field>[a-z_]+)"
)
_VALIDATION_DOMAIN_PATTERN = re.compile(r"validation_domains\[(?P<index>\d+)]")
_ARTIFACT_FIELD_PATTERN = re.compile(r"reusable_artifacts\[(?P<row>\d+)]\.(?P<field>[a-z_0-9]+)")


def _duplicate_errors(
    draft: ManifestDraft,
    field: str,
    message: str,
    *,
    casefold: bool = False,
) -> tuple[DraftFieldError, ...]:
    values = [getattr(item, field) for item in draft.samples]
    if field == "slide_id":
        keys = [value.strip() for value in values]
    else:
        keys = [value.casefold() if casefold else value for value in values]
    counts = Counter(keys)
    errors = tuple(
        DraftFieldError(field=field, message=message, row_index=index)
        for index, key in enumerate(keys)
        if counts[key] > 1
    )
    if errors:
        return errors
    return (DraftFieldError(field=field, message=message),)


def _field_errors(draft: ManifestDraft, message: str) -> tuple[DraftFieldError, ...]:
    artifact_match = _ARTIFACT_FIELD_PATTERN.search(message)
    if artifact_match is not None:
        return (
            DraftFieldError(
                field=f"reusable_artifacts.{artifact_match.group('field')}",
                message=message,
                row_index=int(artifact_match.group("row")),
            ),
        )
    for index, artifact in enumerate(draft.reusable_artifacts):
        if f"reusable artifact {artifact.artifact_id} " in message:
            field = next(
                (
                    name
                    for name in ("sha256", "byte_count", "locator", "validation_domain_id")
                    if name in message
                ),
                "artifact_id",
            )
            return (
                DraftFieldError(
                    field=f"reusable_artifacts.{field}",
                    message=message,
                    row_index=index,
                ),
            )
    if "duplicate sample_id" in message:
        return _duplicate_errors(draft, "sample_id", message)
    if "duplicate slide_id" in message:
        return _duplicate_errors(draft, "slide_id", message)
    if "sample output paths would collide" in message:
        return _duplicate_errors(draft, "sample_id", message, casefold=True)
    if any(locator in message for locator in ("create_project_path", "project_path")):
        return (DraftFieldError(field="project_path", message=message),)

    execution_match = _EXECUTION_FIELD_PATTERN.search(message)
    if execution_match is not None:
        return (
            DraftFieldError(
                field=execution_match.group("field"),
                message=message,
            ),
        )

    domain_match = _VALIDATION_DOMAIN_PATTERN.search(message)
    if domain_match is not None:
        declared_domains = _declared_validation_domains(draft)
        domain = declared_domains[int(domain_match.group("index"))]
        errors: list[DraftFieldError] = []
        if domain == draft.validation_domain_id.strip():
            errors.append(
                DraftFieldError(
                    field="validation_domain_id",
                    message=message,
                )
            )
        errors.extend(
            DraftFieldError(field="validation_domain_id", message=message, row_index=index)
            for index, sample in enumerate(draft.samples)
            if sample.validation_domain_id is not None
            and sample.validation_domain_id.strip() == domain
        )
        if errors:
            return tuple(errors)
        return (DraftFieldError(field="validation_domain_id", message=message),)

    match = _SAMPLE_FIELD_PATTERN.search(message)
    if match is not None:
        field = match.group("field")
        if field == "paired_reference_image":
            if "source_path" in message:
                field = "source_path"
            elif "modality" in message:
                field = "modality"
            elif "reference_id" in message:
                field = "reference_id"
            elif "source_image_id" in message:
                field = "source_image_id"
            elif "source_metadata" in message:
                field = "source_metadata"
        presentation_field = {
            "source_path": "moving_reference_path",
            "modality": "moving_reference_modality",
            "reference_id": "moving_reference_id",
            "source_image_id": "moving_reference_source_image_id",
            "source_metadata": "moving_reference_source_metadata",
        }.get(field, field)
        return (
            DraftFieldError(
                field=presentation_field,
                message=message,
                row_index=int(match.group("row")),
            ),
        )

    for index, sample in enumerate(draft.samples):
        marker = f"sample {sample.sample_id} "
        if marker not in message:
            continue
        field = "validation_domain_id"
        if "source_mif_path" in message:
            field = "source_mif_path"
        elif "reference source_path" in message:
            field = "moving_reference_path"
        elif "resolved config v4" in message:
            field = "shared_analysis_config_path"
            index = None
        return (DraftFieldError(field=field, message=message, row_index=index),)

    field = "batch_id"
    for candidate in (
        "project_path",
        "create_project_path",
        "output_root",
        "validation_domain_id",
        "execution_config",
        "samples",
        "batch_id",
    ):
        if candidate in message:
            field = "project_path" if candidate == "create_project_path" else candidate
            break
    return (DraftFieldError(field=field, message=message),)


def validate_manifest_draft(
    draft: ManifestDraft,
    *,
    analysis_config: Mapping[str, object] | None = None,
) -> ManifestDraftValidation:
    """Validate a draft entirely through the configuration and batch owners.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if not isinstance(draft, ManifestDraft):
        raise TypeError("draft must be a ManifestDraft")
    for index, sample in enumerate(draft.samples):
        if (
            sample.moving_reference_path is not None
            and sample.moving_reference_modality is not None
            and sample.moving_reference_id is None
        ):
            return ManifestDraftValidation(
                draft=draft,
                errors=(
                    DraftFieldError(
                        "moving_reference_id", "moving reference ID is required", index
                    ),
                ),
                validated_manifest=None,
            )
    try:
        shared_config = (
            _load_shared_analysis_config(draft.shared_analysis_config_path)
            if analysis_config is None
            else AnalysisConfig.from_mapping(analysis_config).to_mapping()
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        return ManifestDraftValidation(
            draft=draft,
            errors=(
                DraftFieldError(
                    field="shared_analysis_config_path",
                    message=str(exc),
                ),
            ),
            validated_manifest=None,
        )

    mapping = _manifest_mapping(draft, shared_config)
    data = json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        manifest = BatchManifest.from_json_bytes(data)
        validated = validate_manifest(manifest)
    except (BatchManifestError, ROIAnalysisError, TypeError, ValueError) as exc:
        return ManifestDraftValidation(
            draft=draft,
            errors=_field_errors(draft, str(exc)),
            validated_manifest=None,
        )
    return ManifestDraftValidation(
        draft=draft,
        errors=(),
        validated_manifest=validated,
    )


def _paths_alias(first: Path, second: Path) -> bool:
    if first == second:
        return True
    try:
        return first.samefile(second)
    except OSError:
        return False


def _validate_manifest_destination(draft: ManifestDraft, target: Path) -> None:
    protected_paths: list[tuple[str, Path]] = [
        ("project_path", draft.project_path),
    ]
    for index, sample in enumerate(draft.samples):
        protected_paths.append((f"samples[{index}].source_mif_path", sample.source_mif_path))
        if sample.moving_reference_path is not None:
            protected_paths.append(
                (
                    f"samples[{index}].moving_reference_path",
                    sample.moving_reference_path,
                )
            )
    protected_paths.extend(
        (f"reusable_artifacts[{index}].locator", artifact.locator)
        for index, artifact in enumerate(draft.reusable_artifacts)
    )
    for field, protected in protected_paths:
        if _paths_alias(target, protected):
            raise ValueError(f"manifest destination must not alias {field}: {target}")


def load_manifest_draft(path: Path, *, existing_analysis: bool = False) -> ManifestDraft:
    """Load and validate one strict batch manifest into presentation records."""

    source = _local_path(path)
    data = source.read_bytes()
    manifest = BatchManifest.from_json_bytes(data)
    validated = (revalidate_retained_manifest(data, json.dumps(manifest.to_mapping(), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")) if existing_analysis else validate_manifest(manifest))
    return manifest_draft_from_manifest(validated, source)


def manifest_draft_from_manifest(validated: ValidatedBatchManifest, source: Path) -> ManifestDraft:
    """Project retained inputs without reopening source files or admitting a new analysis."""
    manifest = validated.manifest
    choice = (
        ProjectChoice.CREATE if manifest.create_project_path is not None else ProjectChoice.OPEN
    )
    project_path = manifest.create_project_path or manifest.project_path
    assert project_path is not None
    return ManifestDraft(
        batch_id=manifest.batch_id,
        validation_domain_id=manifest.validation_domain_id,
        validation_domains=manifest.validation_domains,
        output_root=manifest.output_root,
        project_choice=choice,
        project_path=project_path,
        shared_analysis_config_path=source,
        execution=ExecutionDraft(
            max_workers=manifest.execution_config.max_workers,
            max_in_flight=manifest.execution_config.max_in_flight,
            resource_capacities=manifest.execution_config.resource_capacities,
            max_working_set_bytes=manifest.execution_config.max_working_set_bytes,
        ),
        reusable_artifacts=tuple(
            ReusableArtifactDraft(
                artifact_id=item.artifact_id,
                artifact_kind=item.artifact_kind,
                schema_name=item.schema_name,
                schema_version=item.schema_version,
                locator=item.locator,
                sha256=item.sha256,
                byte_count=item.byte_count,
                owning_identity=item.owning_identity,
                runtime_versions=item.runtime_versions,
                validation_domain_id=item.validation_domain_id,
                compatibility_signature_sha256=item.compatibility_signature_sha256,
            )
            for item in manifest.reusable_artifacts
        ),
        samples=tuple(
            SampleDraft(
                enabled=item.enabled,
                sample_id=item.sample_id,
                slide_id=item.slide_id,
                subject_id=item.subject_id,
                validation_domain_id=item.validation_domain_id,
                overrides=item.overrides,
                source_mif_path=item.source_mif_path,
                source_image_index=item.source_image_index,
                source_series_index=item.source_series_index,
                source_file_sha256=item.source_file_sha256,
                moving_reference_path=(
                    None if item.reference is None else item.reference.source_path
                ),
                moving_reference_modality=(
                    None if item.reference is None else item.reference.modality
                ),
                moving_reference_id=(
                    None if item.reference is None else item.reference.reference_id
                ),
                moving_reference_source_image_id=(
                    None if item.reference is None else item.reference.source_image_id
                ),
                moving_reference_source_metadata=(
                    {} if item.reference is None else item.reference.source_metadata
                ),
            )
            for item in manifest.samples
        ),
    )


def write_manifest_draft(
    draft: ManifestDraft,
    destination: Path,
    *,
    overwrite: bool = False,
    analysis_config: Mapping[str, object] | None = None,
) -> Path:
    """Validate, then atomically publish a normalized strict batch manifest."""

    validation = validate_manifest_draft(draft, analysis_config=analysis_config)
    if not validation.is_valid:
        details = "; ".join(error.message for error in validation.errors)
        raise ValueError(f"manifest draft is invalid: {details}")
    assert validation.validated_manifest is not None

    target = _local_path(destination)
    _validate_manifest_destination(draft, target)
    if target.exists() and not overwrite:
        raise FileExistsError(f"manifest already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(validation.validated_manifest.normalized_manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "DraftFieldError",
    "ExecutionDraft",
    "ManifestDraft",
    "ManifestDraftValidation",
    "ProjectChoice",
    "ReusableArtifactDraft",
    "SampleDraft",
    "load_manifest_draft",
    "manifest_draft_from_manifest",
    "validate_manifest_draft",
    "write_manifest_draft",
]
