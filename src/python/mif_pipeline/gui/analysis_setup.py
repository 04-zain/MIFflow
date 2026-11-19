"""Create, save and reopen the local analysis setup assembled by the researcher.

The setup document retains sample definitions, computing choices and incomplete stage
settings in the named local folder. Saving checks that the file still matches the opened
version. Preparing an executable manifest delegates to the configuration and manifest
validators before publishing the resolved input."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import mkstemp

from mif_pipeline.pipeline_core import AnalysisConfig, ROIAnalysisError
from mif_pipeline.orchestration.batch_semantic_profile_import import (
    author_missing_semantic_profile_declarations,
)

from .analysis_settings import new_analysis_settings
from .manifest_draft import (
    ExecutionDraft,
    ManifestDraft,
    ProjectChoice,
    ReusableArtifactDraft,
    SampleDraft,
    _freeze_json,
    _read_json_object,
    _reject_duplicate_pairs,
    _reject_nonfinite,
    _validate_manifest_destination,
    validate_manifest_draft,
    write_manifest_draft,
)

_SCHEMA = "mif-analysis-setup"
_SETUP_FILENAME = "analysis.setup.json"
_RESOURCES = (
    "bioformats_conversion",
    "spectral_preprocessing",
    "scientific_compute",
    "qupath_image_read",
    "qupath_image_write",
    "project_catalogue_mutation",
    "shared_classifier_mutation",
)
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# ==============================================================================
#                                 Analysis setup
# ==============================================================================


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("analysis name is required")
    if any(character in name for character in '/\\<>:"|?*') or any(
        ord(character) < 32 for character in name
    ):
        raise ValueError("analysis name must not contain path separators or reserved characters")
    segment = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    if not segment or segment in _RESERVED or len(segment) > 100:
        raise ValueError("analysis name does not produce a safe folder name")
    return segment


def _local_path(value: Path) -> Path:
    if not isinstance(value, Path):
        raise TypeError("local paths must be Path values")
    return value.expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class NewAnalysisRequest:
    name: str
    parent_directory: Path
    imported_settings_path: Path | None = None
    existing_project_path: Path | None = None

    def __post_init__(self) -> None:
        _safe_name(self.name)
        object.__setattr__(self, "name", self.name.strip())
        for field in ("parent_directory", "imported_settings_path", "existing_project_path"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _local_path(value))

    @property
    def analysis_folder(self) -> Path:
        """Return the named folder that creation will publish beneath the parent.

        We use the accepted name's filesystem form for both the Home preview and
        saved setup. Reading this location creates no directories or files.
        """
        return self.parent_directory / _safe_name(self.name)


@dataclass(frozen=True, slots=True)
class AnalysisSetupDocument:
    path: Path
    name: str
    draft: ManifestDraft
    analysis_settings: Mapping[str, object] | None
    source_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.path, Path) and self.path.is_symlink():
            raise ValueError("setup document must not be a symbolic link")
        object.__setattr__(self, "path", _local_path(self.path))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("analysis name is required")
        if type(self.draft) is not ManifestDraft:
            raise TypeError("analysis setup requires a ManifestDraft")
        if (
            not isinstance(self.source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
        ):
            raise ValueError("setup source digest must be a SHA-256 from the saved file")
        if self.analysis_settings is not None:
            if not isinstance(self.analysis_settings, Mapping):
                raise ValueError("analysis settings must be a JSON object or null")
            object.__setattr__(self, "analysis_settings", _freeze_json(self.analysis_settings))
        _validate_locations(self.path, self.draft)


def _validate_locations(path: Path, draft: ManifestDraft) -> None:
    root = path.parent
    if path.name != _SETUP_FILENAME or root != draft.output_root / draft.batch_id:
        raise ValueError("setup path must belong to its analysis workspace")
    if draft.shared_analysis_config_path != root / "analysis.settings.json":
        raise ValueError("setup settings path must belong to its analysis workspace")
    if draft.project_choice is ProjectChoice.CREATE and (
        draft.project_path != root / "qupath" / "project.qpproj"
    ):
        raise ValueError("new project path must belong to its analysis workspace")


def _json_value(value: object) -> object:
    if isinstance(value, ExecutionDraft):
        return value.to_mapping()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _payload(name: str, draft: ManifestDraft, settings: Mapping | None) -> bytes:
    return (
        json.dumps(
            {
                "schema_name": _SCHEMA,
                "schema_version": "3",
                "name": name,
                "draft": _json_value(draft),
                "analysis_settings": _json_value(settings),
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _publish(path: Path, data: bytes, *, replace_existing: bool = False) -> None:
    descriptor, temporary_name = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace_existing:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def create_analysis_setup(request: NewAnalysisRequest) -> AnalysisSetupDocument:
    """Create a new named local analysis folder and its initial editable setup.

    The request's name and selected locations are validated first. The new manifest draft
    receives explicit execution presets and empty stage choices, then the setup is written
    through the local publication helper. Existing source images are referenced at their
    selected locations rather than copied into the folder."""
    if type(request) is not NewAnalysisRequest:
        raise TypeError("new analysis requires a NewAnalysisRequest")
    if not request.parent_directory.is_dir():
        raise ValueError("analysis parent directory must already exist")
    settings = None
    if request.imported_settings_path is not None:
        try:
            settings = AnalysisConfig.from_mapping(
                _read_json_object(request.imported_settings_path)
            ).to_mapping()
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise ValueError(f"cannot import analysis settings: {exc}") from exc
    if request.existing_project_path is not None and not request.existing_project_path.is_file():
        raise ValueError("existing QuPath project must be a file")
    from mif_pipeline._defaults import default_section

    execution_defaults = default_section("execution")
    batch_id = _safe_name(request.name)
    root = request.analysis_folder
    draft = ManifestDraft(
        batch_id=batch_id,
        validation_domain_id="group-1",
        output_root=request.parent_directory,
        project_choice=(
            ProjectChoice.OPEN if request.existing_project_path else ProjectChoice.CREATE
        ),
        project_path=request.existing_project_path or root / "qupath" / "project.qpproj",
        shared_analysis_config_path=root / "analysis.settings.json",
        execution=ExecutionDraft(
            execution_defaults["max_workers"],
            execution_defaults["max_in_flight"],
            {name: execution_defaults["resource_capacity"] for name in _RESOURCES},
        ),
        samples=(),
    )
    if settings is None:
        settings = new_analysis_settings(draft)
    return create_analysis_setup_from_draft(draft, name=request.name, settings=settings)


def create_analysis_setup_from_draft(draft, *, name, settings, computing_settings=None):
    """Publish the local setup and its connection together into a new folder."""
    from tempfile import mkdtemp
    import shutil
    from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace

    root = draft.output_root / draft.batch_id
    path = root / _SETUP_FILENAME
    data = _payload(name, draft, settings)
    document = AnalysisSetupDocument(path, name, draft, settings, hashlib.sha256(data).hexdigest())
    if root.exists() or root.is_symlink():
        raise FileExistsError(
            "A local folder with this analysis name already exists. Open that folder or choose another parent folder."
        )
    companion = None
    if computing_settings is not None:
        from .execution_location import validate_location, MAX_SETTINGS_BYTES

        companion = json.dumps(
            validate_location(computing_settings), sort_keys=True, allow_nan=False
        ).encode()
        if len(companion) > MAX_SETTINGS_BYTES:
            raise ValueError("The connection settings are too large.")
    staging = Path(mkdtemp(prefix=".opening-analysis-", dir=draft.output_root))
    published = False
    try:
        if companion is not None:
            _publish(staging / "analysis.computing.json", companion)
        _publish(staging / _SETUP_FILENAME, data)
        publish_no_replace(staging, root, "win-x86_64" if os.name == "nt" else "linux-x86_64")
        published = True
        if os.name != "nt":
            descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if not published and staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
    return document


def _record_fields(value: object, record_type: type) -> dict:
    expected = {field.name for field in fields(record_type)}
    optional = {"max_working_set_bytes"} if record_type is ExecutionDraft else set()
    if not isinstance(value, Mapping) or not expected - optional <= set(value) <= expected:
        raise ValueError(f"setup {record_type.__name__} has missing or unknown fields")
    return dict(value)


def _draft_from_mapping(value: object) -> ManifestDraft:
    mapping = _record_fields(value, ManifestDraft)
    mapping["execution"] = ExecutionDraft(**_record_fields(mapping["execution"], ExecutionDraft))
    for field in ("output_root", "project_path", "shared_analysis_config_path"):
        mapping[field] = Path(mapping[field])
    samples = []
    for item in mapping["samples"]:
        sample = _record_fields(item, SampleDraft)
        sample["source_mif_path"] = Path(sample["source_mif_path"])
        if sample["moving_reference_path"] is not None:
            sample["moving_reference_path"] = Path(sample["moving_reference_path"])
        samples.append(SampleDraft(**sample))
    artifacts = []
    for item in mapping["reusable_artifacts"]:
        artifact = _record_fields(item, ReusableArtifactDraft)
        artifact["locator"] = Path(artifact["locator"])
        artifacts.append(ReusableArtifactDraft(**artifact))
    mapping["samples"] = tuple(samples)
    mapping["reusable_artifacts"] = tuple(artifacts)
    return ManifestDraft(**mapping)


def load_analysis_setup(path: Path) -> AnalysisSetupDocument:
    """Read complete authoring input without validating unfinished science."""
    if isinstance(path, Path) and path.is_symlink():
        raise ValueError("setup document must not be a symbolic link")
    path = _local_path(path)
    data = path.read_bytes()
    # Parse exactly the bytes whose digest authorizes the next save.
    try:
        mapping = json.loads(
            data, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_nonfinite
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read analysis setup: {exc}") from exc
    if not isinstance(mapping, Mapping):
        raise ValueError("analysis setup must be a JSON object")
    if mapping.get("schema_name") != _SCHEMA or mapping.get("schema_version") != "3":
        raise ValueError("unsupported analysis setup schema")
    if set(mapping) != {"schema_name", "schema_version", "name", "draft", "analysis_settings"}:
        raise ValueError("analysis setup has missing or unknown fields")
    try:
        draft = _draft_from_mapping(mapping["draft"])
        return AnalysisSetupDocument(
            path,
            mapping["name"],
            draft,
            mapping["analysis_settings"],
            hashlib.sha256(data).hexdigest(),
        )
    except (TypeError, KeyError) as exc:
        raise ValueError(f"invalid analysis setup: {exc}") from exc


def _unchanged(document: AnalysisSetupDocument) -> None:
    if document.path.is_symlink():
        raise ValueError("setup document must not be a symbolic link")
    if hashlib.sha256(document.path.read_bytes()).hexdigest() != document.source_sha256:
        raise ValueError("analysis setup changed since it was opened; reopen before saving")


def save_analysis_setup(document: AnalysisSetupDocument) -> AnalysisSetupDocument:
    """Atomically replace only the exact unchanged document opened by this session."""
    _unchanged(document)
    _validate_manifest_destination(document.draft, document.path)
    previous = load_analysis_setup(document.path)
    if previous.draft.batch_id != document.draft.batch_id:
        raise ValueError("saved analysis identity cannot be changed")
    data = _payload(document.name, document.draft, document.analysis_settings)
    _unchanged(document)
    _publish(document.path, data, replace_existing=True)
    return replace(document, source_sha256=hashlib.sha256(data).hexdigest())


def prepare_analysis_manifest(document: AnalysisSetupDocument) -> Path:
    """Validate the current setup and publish the executable manifest used to start processing.

    The function checks the opened setup version and resolves its per-sample configuration
    through the established validators. The resulting manifest fixes the input inventory and
    accepted initial settings. Later stage choices and progress are stored in their own
    records rather than editing this input history."""
    _unchanged(document)
    if document.analysis_settings is None:
        raise ValueError("analysis settings are incomplete")
    draft = document.draft
    has_configured_secondary = bool(document.analysis_settings.get("secondary_structures")) or any(
        sample.enabled and sample.overrides.get("secondary_structures") for sample in draft.samples
    )
    if has_configured_secondary:
        validation = validate_manifest_draft(draft, analysis_config=document.analysis_settings)
        if not validation.is_valid:
            details = "; ".join(error.message for error in validation.errors)
            raise ValueError(f"manifest draft is invalid: {details}")
        assert validation.validated_manifest is not None
        declarations = author_missing_semantic_profile_declarations(validation.validated_manifest)
        draft = replace(
            draft,
            reusable_artifacts=(
                *draft.reusable_artifacts,
                *(
                    ReusableArtifactDraft(
                        **{
                            field.name: getattr(item, field.name)
                            for field in fields(ReusableArtifactDraft)
                        }
                    )
                    for item in declarations
                ),
            ),
        )
    return write_manifest_draft(
        draft,
        document.path.parent / "analysis.manifest.json",
        analysis_config=document.analysis_settings,
    )


__all__ = [
    "AnalysisSetupDocument",
    "NewAnalysisRequest",
    "create_analysis_setup",
    "load_analysis_setup",
    "save_analysis_setup",
    "prepare_analysis_manifest",
]
