"""Materialization of the stage-owned QuPath project and run layout.

The `ProjectLayout` record carries `root`, `project_file`, `scripts_dir`,
`pixel_classifiers_dir`, `object_classifiers_dir` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.pipeline_core.errors import (
    ProjectPreparationError,
    validate_safe_path_segment,
)

# ==============================================================================
#                                    Project
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    """Stable locations owned by one ROI-analysis QuPath project."""

    root: Path
    project_file: Path
    scripts_dir: Path
    pixel_classifiers_dir: Path
    object_classifiers_dir: Path
    data_dir: Path
    exchange_dir: Path
    checkpoints_dir: Path
    provenance_dir: Path
    runs_dir: Path
    config_path: Path
    config_json: str


@dataclass(frozen=True, slots=True)
class ProjectRunLayout:
    """Stable project-owned locations and identity for one orchestration run."""

    root: Path
    manifest_path: Path
    run_id: str
    config_hash: str


def prepare_project(config: AnalysisConfig) -> ProjectLayout:
    """Create missing stage directories and materialize a JSON config atomically."""
    root = config.output_dir
    scripts_dir = root / "scripts"
    classifiers_dir = root / "classifiers"
    pixel_classifiers_dir = classifiers_dir / "pixel_classifiers"
    object_classifiers_dir = classifiers_dir / "object_classifiers"
    data_dir = root / "data"
    exchange_dir = root / "exchange"
    checkpoints_dir = root / "checkpoints"
    provenance_dir = root / "provenance"
    runs_dir = root / "runs"
    for directory in (
        scripts_dir,
        pixel_classifiers_dir,
        object_classifiers_dir,
        data_dir,
        exchange_dir,
        checkpoints_dir,
        provenance_dir,
        runs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    project_file = root / "project.qpproj"
    config_path = data_dir / "analysis-config.json"
    config_json = json.dumps(config.to_mapping(), indent=2, sort_keys=True) + "\n"
    _ensure_matching_config(config_path, config.to_mapping(), config_json)
    project = ProjectLayout(
        root=root,
        project_file=project_file,
        scripts_dir=scripts_dir,
        pixel_classifiers_dir=pixel_classifiers_dir,
        object_classifiers_dir=object_classifiers_dir,
        data_dir=data_dir,
        exchange_dir=exchange_dir,
        checkpoints_dir=checkpoints_dir,
        provenance_dir=provenance_dir,
        runs_dir=runs_dir,
        config_path=config_path,
        config_json=config_json,
    )
    _materialize_version_controlled_scripts(project)
    return project


def prepare_project_run(
    project: ProjectLayout,
    *,
    run_id: str,
    config_hash: str,
    overwrite: bool = False,
) -> ProjectRunLayout:
    """Create an idempotent run identity, rejecting material conflicts by default."""
    validate_safe_path_segment(run_id, "run_id", error_type=ProjectPreparationError)
    if (
        not isinstance(config_hash, str)
        or len(config_hash) != 64
        or any(character not in "0123456789abcdef" for character in config_hash)
    ):
        raise ProjectPreparationError("config_hash must be a lowercase SHA-256")
    root = project.runs_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "run.v1.json"
    expected = {
        "schema_version": "roi-project-run/v1",
        "run_id": run_id,
        "config_hash": config_hash,
    }
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectPreparationError(f"conflicting run manifest: {manifest_path}") from exc
        if existing != expected and not overwrite:
            raise ProjectPreparationError(
                f"conflicting run configuration requires explicit overwrite: {manifest_path}"
            )
        if existing != expected:
            _write_atomic(
                manifest_path,
                json.dumps(expected, indent=2, sort_keys=True) + "\n",
            )
    else:
        _write_atomic(
            manifest_path,
            json.dumps(expected, indent=2, sort_keys=True) + "\n",
        )
    return ProjectRunLayout(root, manifest_path, run_id, config_hash)


def _materialize_version_controlled_scripts(project: ProjectLayout) -> None:
    from ._script_asset_registry import registered_script_assets
    from .scripts import materialize_script_bundle

    materialize_script_bundle(project, registered_script_assets())


def _ensure_matching_config(
    path: Path,
    expected: dict[str, object],
    rendered: str,
) -> None:
    if not path.exists():
        _write_atomic(path, rendered)
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectPreparationError(
            f"conflicting existing ROI-analysis configuration: {path} is not valid JSON"
        ) from exc
    if existing != expected:
        raise ProjectPreparationError(f"conflicting existing ROI-analysis configuration: {path}")


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "ProjectLayout",
    "ProjectPreparationError",
    "ProjectRunLayout",
    "prepare_project",
    "prepare_project_run",
]
