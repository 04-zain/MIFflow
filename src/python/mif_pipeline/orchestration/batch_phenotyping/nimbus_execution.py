"""

Connects a reviewed marker choice to automatic bounded native Nimbus inference.

The marker owner supplies authenticated measurement populations; this adapter
opens their exact canonical images and finalized cells through a read-only
QuPath operation. A single optional Python worker retains the frozen model
while native sources advance slide by slide. Completed scores can be reused
across population selections and intensity distillation because their identity
binds the full segmentation population, image, marker channel and weights.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from mif_pipeline.cell_analysis.nimbus import NimbusScoreError, NimbusScoreResult, load_nimbus_scores
from mif_pipeline.pipeline_core.execution_resources import current_operation_budget, operation_budget
from mif_pipeline.orchestration.setup.nimbus_requirements import NimbusPrerequisiteError
from mif_pipeline.orchestration.setup.nimbus_runtime import (
    inspect_nimbus_runtime, nimbus_runtime_identity, nimbus_worker_environment, nimbus_worker_path,
)
from mif_pipeline.qupath_runtime.nimbus_source import open_nimbus_native_sources
from mif_pipeline.qupath_runtime.scripts import registered_script_asset_sha256
from mif_pipeline.qupath_runtime._script_asset_registry import QuPathScriptAsset


def _file_sha256(path: Path) -> str:
    """Hashes a potentially large checkpoint without allocating its full bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _read_completed_record(path: Path) -> dict[str, str]:
    """Read the saved score association, reporting an incomplete record clearly."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        required = ("configuration_sha256", "score_path", "score_sha256", "request_sha256")
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ValueError("completion fields are missing")
    except (OSError, ValueError) as exc:
        raise NimbusScoreError(f"The saved Nimbus completion record is unreadable or incomplete: {path}") from exc
    return value


def _sync_directory(path: Path) -> None:
    """Retain published filenames across an interruption on POSIX filesystems."""
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _native_prerequisite(directory: Path, source_count: int) -> NimbusPrerequisiteError | None:
    """Recognize only a bounded prerequisite emitted by this native invocation."""
    for index in range(source_count):
        path = directory / f"source-{index}" / "prerequisite.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if (type(value) is dict and set(value) == {"code", "message"}
                    and value["code"] == "magnification_missing"):
                return NimbusPrerequisiteError(value["code"], value["message"])
        except (OSError, ValueError, TypeError):
            continue
    return None


def _publish_completed_record(temporary: Path, destination: Path, completed: dict[str, str]) -> None:
    """Synchronize the score association before making it available for reuse."""
    with temporary.open("xb") as stream:
        stream.write(_json_bytes(completed))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(temporary.parent)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        existing = _read_completed_record(destination)
        if existing["score_sha256"] != completed["score_sha256"]:
            raise NimbusScoreError("Concurrent Nimbus computations produced different retained score files")
    _sync_directory(destination.parent)


class AutomaticNimbusScoreProvider:
    """

    Loads one marker's identity-bound scores, producing them when needed.

    The cache key includes the actual checkpoint and source-adapter checksums;
    changing a cutoff, fitting scope or selected tissue subset does not rerun
    the image model. An incomplete invocation has its own directory and never
    replaces a completed record. An expected score checksum only permits reuse.
    """

    def __init__(self, *, runner, project_file: Path, output_directory: Path, preferences_path: Path | None = None):
        self.runner = runner
        self.project_file = Path(project_file).resolve()
        self.output_directory = Path(output_directory).resolve()
        self.preferences_path = preferences_path

    def __call__(self, population, marker: str, expected_sha256: str | None = None) -> NimbusScoreResult:
        rule = next(item for item in population.marker_settings["marker_rules"] if item["marker_id"] == marker)
        checkpoint_value = rule.get("nimbus_checkpoint_path")
        if not checkpoint_value:
            from mif_pipeline.pipeline_core.software_preferences import desktop_preferences_path, load_desktop_preferences
            from mif_pipeline.orchestration.setup.nimbus_setup import default_nimbus_model_target
            try:
                preferences = load_desktop_preferences(self.preferences_path or desktop_preferences_path())
            except (ValueError, OSError) as exc:
                raise NimbusPrerequisiteError("preferences_unreadable", "The processing computer's software preferences cannot be read. Select the Nimbus checkpoint explicitly or correct Software setup, then fit this marker again.") from exc
            import sys
            checkpoint_value = (preferences.nimbus_checkpoint_path if preferences else None) or default_nimbus_model_target(
                "win-x86_64" if sys.platform == "win32" else "linux-x86_64")
        try:
            checkpoint = Path(checkpoint_value).expanduser().resolve()
            if not checkpoint.is_file():
                raise NimbusPrerequisiteError("checkpoint_missing", "The Nimbus checkpoint is missing on the processing computer. Install it in Software setup or select an existing checkpoint for this marker, then fit again. Your marker choices are retained.")
            weights_sha256 = _file_sha256(checkpoint)
        except OSError as exc:
            raise NimbusPrerequisiteError("checkpoint_unreadable", "The Nimbus checkpoint cannot be read on the processing computer. Select a readable checkpoint or correct its access, then fit this marker again.") from exc
        semantic = next(item for item in population.marker_semantics if item.biomarker == marker)
        grouped = defaultdict(list)
        channel_names = set()
        for measurement in population.measurement_projections:
            matching = [item for item in measurement.canonical_channels if item["channel_id"] == semantic.channel_id]
            if len(matching) != 1:
                raise NimbusScoreError("Nimbus marker channel is absent or ambiguous in a canonical image")
            channel_names.add(matching[0]["name"])
            grouped[(measurement.sample_id, measurement.project_entry_id, measurement.slide_id)].append(measurement)
        if len(channel_names) != 1:
            raise NimbusScoreError("Nimbus needs the selected marker to have one exact canonical channel name across this validation group")
        sources = []
        for identity, measurements in sorted(grouped.items()):
            if len({(item.canonical_image_id, item.canonical_image_sha256) for item in measurements}) != 1:
                raise NimbusScoreError("Nimbus image sections disagree about their canonical image")
            sources.append({
                "project_entry_id": identity[1],
                "canonical_image_sha256": measurements[0].canonical_image_sha256,
                "magnification_fallback": rule.get("nimbus_magnification"),
                "scopes": [{
                    "identity": [item.sample_id, item.project_entry_id, item.slide_id, item.section_id],
                    "canonical_image_id": item.canonical_image_id,
                    "segmentation_population_id": item.segmentation_population_id,
                    "segmentation_population_sha256": item.segmentation_population_sha256,
                    "tissue_generation_id": item.tissue_generation_id,
                    "cell_ids_sha256": item.cell_ids_sha256,
                } for item in sorted(measurements, key=lambda item: item.section_id)],
            })
        try:
            runtime_identity = nimbus_runtime_identity()
        except (OSError, ValueError) as exc:
            raise NimbusPrerequisiteError("runtime_missing",
                "The application's Nimbus scoring software is missing or incomplete. Repair or reinstall the application on the processing computer, then fit this marker again.") from exc
        configuration = {
            "adapter": "mif-nimbus-streaming/v1", "marker": marker,
            "channel": next(iter(channel_names)), "weights_sha256": weights_sha256,
            "sources": sources, "project_file": str(self.project_file),
            "runtime_sha256": runtime_identity,
            "device_preference": os.environ.get("MIF_COMPUTE_DEVICE", "auto"),
            "native_sha256": registered_script_asset_sha256(QuPathScriptAsset.NIMBUS_IMAGE_SOURCE),
        }
        key = hashlib.sha256(_json_bytes(configuration)).hexdigest()
        cache = self.output_directory / key
        cache.mkdir(parents=True, exist_ok=True)
        completed_path = cache / "completed.json"
        if completed_path.is_file():
            completed = _read_completed_record(completed_path)
            if completed.get("configuration_sha256") != key:
                raise NimbusScoreError("Retained Nimbus source configuration changed")
            score_path = Path(completed["score_path"])
            if not score_path.is_file() or (expected_sha256 is not None and completed["score_sha256"] != expected_sha256):
                raise NimbusScoreError("The retained Nimbus scores changed or are incomplete")
            expected_sha256 = completed["score_sha256"]
            request_sha256 = completed["request_sha256"]
        else:
            completed = None
            if expected_sha256 is not None:
                raise NimbusScoreError("Accepted Nimbus scores are missing; inference is not repeated during acceptance")
            invocation = cache / ("run-" + uuid4().hex)
            invocation.mkdir()
            score_path = invocation / "scores.ndjson"
            log_path = invocation / "worker.log"
            # Check the bundled worker before opening a WSI or allocating native masks.
            runtime = inspect_nimbus_runtime()
            if runtime.state != "ready":
                log_path.write_text(runtime.detail, encoding="utf-8")
                raise NimbusPrerequisiteError(
                    "runtime_missing" if runtime.state == "missing" else "runtime_incompatible",
                    "Nimbus scoring software is unavailable on the processing computer. Repair or reinstall the application, then fit this marker again. Your marker settings are retained; details are in phenotyping-nimbus worker.log.")
            executable = nimbus_worker_path()
            budget = current_operation_budget()
            threads = min(8, budget.cpu_threads) if budget is not None else 2
            native_directory = invocation / "native"
            try:
                with ExitStack() as native_sources:
                    # The native reader captures its own budget in the serving thread.
                    # Limit it to one quarter of the shared reservation, leaving the
                    # remaining three quarters for the model worker and its tile buffers.
                    native_bytes = budget.memory_bytes // 4 if budget is not None else 1024**3
                    with operation_budget(native_bytes, threads):
                        descriptors = native_sources.enter_context(open_nimbus_native_sources(
                            runner=self.runner, project_file=self.project_file,
                            sources=sources, work_directory=native_directory))
                    request_path = invocation / "worker-request.json"
                    request_path.write_bytes(_json_bytes({
                        "schema_version": "mif-nimbus-streaming/v1", "checkpoint": str(checkpoint),
                        "weights_sha256": weights_sha256, "sources": list(descriptors),
                        "markers": {marker: {"channel": configuration["channel"], "output_path": str(score_path)}},
                        "threads": threads, "device": configuration["device_preference"],
                    }))
                    with log_path.open("xb") as log:
                        try:
                            result = subprocess.run([str(executable), "--request", str(request_path)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, check=False,
                                env=nimbus_worker_environment(),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        except OSError as exc:
                            raise NimbusScoreError(f"Nimbus worker could not start; see {log_path}") from exc
                    if result.returncode:
                        raise NimbusScoreError(f"Nimbus inference failed; see {log_path}")
            except NimbusScoreError as exc:
                requirement = _native_prerequisite(native_directory, len(sources))
                if requirement is not None:
                    raise requirement from exc
                raise
            request_sha256 = _file_sha256(request_path)
        scopes = {(item.sample_id, item.project_entry_id, item.slide_id, item.section_id): item for item in population.measurement_projections}
        verified = load_nimbus_scores(score_path, marker=marker,
            expected_identities=tuple(item.identity for item in population._observations), allow_superset=True,
            expected_sha256=expected_sha256,
            expected_population_by_scope={scope: item.segmentation_population_id for scope, item in scopes.items()},
            expected_image_by_scope={scope: item.canonical_image_id for scope, item in scopes.items()})
        if verified.provenance.get("inference", {}).get("request_sha256") != request_sha256:
            raise NimbusScoreError("Nimbus completed scores do not match their frozen worker request")
        if completed is None:
            completed = {"configuration_sha256": key, "score_path": str(score_path),
                         "score_sha256": verified.provenance["sha256"], "request_sha256": request_sha256}
            # A pointer appears only after both native and model outputs pass validation.
            temporary = invocation / "completed.json"
            _publish_completed_record(temporary, completed_path, completed)
        return verified
