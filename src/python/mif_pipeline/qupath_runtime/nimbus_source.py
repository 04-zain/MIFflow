"""

Keeps one read-only QuPath process available to a bounded Nimbus worker.

The native operation opens images sequentially. A source is advertised only
once its canonical image, stable cell inventory and finalized geometry have
been checked. The Python model process exchanges one bounded rectangle at a
time through private temporary files; neither process writes the hierarchy.
The existing runner retains process ownership, launch checks and native logs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import copy_context
import json
from pathlib import Path
from threading import Thread
import time
from typing import Iterator

from mif_pipeline.cell_analysis.nimbus import NimbusScoreError
from mif_pipeline.qupath_runtime._script_asset_registry import QuPathScriptAsset
from mif_pipeline.qupath_runtime.runner import QuPathRunner
from mif_pipeline.qupath_runtime.scripts import materialize_evidence_script


@contextmanager
def open_nimbus_native_sources(
    *,
    runner: QuPathRunner,
    project_file: Path,
    sources: Sequence[Mapping[str, object]],
    work_directory: Path,
    startup_timeout_seconds: float = 120,
) -> Iterator[tuple[dict[str, str], ...]]:
    """

    Opens authenticated image sources for one worker invocation and closes them.

    Sources contain exact project-entry/image bindings and committed section
    population inventories. Native code validates those against the saved
    hierarchy before publishing each descriptor. Only the first image opens
    during startup; closing a source allows the native process to open the next.
    We wait for the native evidence result after normal completion and signal
    cancellation on every failure, including a missing model or failed worker.
    The native thread retains this operation's admitted memory/thread allowance;
    each invocation receives its own context and keeps the existing join lifetime.
    """
    if not sources:
        raise NimbusScoreError("Nimbus requires at least one canonical image")
    root = Path(work_directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    native_sources = []
    for index, source in enumerate(sources):
        directory = root / f"source-{index}"
        directory.mkdir()
        native_sources.append({**dict(source), "bridge_directory": str(directory)})
    request_path = root / "native-request.json"
    request_path.write_text(json.dumps({
        "schema_version": "mif-nimbus-native-source/v1",
        "project_file": str(Path(project_file).resolve()),
        "sources": native_sources,
    }, allow_nan=False, ensure_ascii=False), encoding="utf-8")
    evidence_path = root / "native-evidence.json"
    script = materialize_evidence_script(root, QuPathScriptAsset.NIMBUS_IMAGE_SOURCE)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            runner.run_evidence_script(
                project_file=project_file, script=script,
                request_paths=(request_path,), log_directory=root,
                require_existing_project=False, evidence_path=evidence_path,
            )
        except BaseException as exc:
            errors.append(exc)
            for source in native_sources:
                path = Path(source["bridge_directory"]) / "error.json"
                try:
                    path.write_text(json.dumps({"message": str(exc)}), encoding="utf-8")
                except OSError:
                    pass

    thread = Thread(target=copy_context().run, args=(serve,),
                    name="Nimbus native image source", daemon=True)
    thread.start()
    successful = False
    try:
        deadline = time.monotonic() + startup_timeout_seconds
        ready = Path(native_sources[0]["bridge_directory"]) / "ready.json"
        while not ready.is_file():
            if errors or not thread.is_alive():
                raise NimbusScoreError(f"Nimbus could not open its native image source; see {root}") from (errors[0] if errors else None)
            if time.monotonic() >= deadline:
                raise NimbusScoreError(f"Nimbus native image startup timed out; see {root}")
            time.sleep(0.05)
        yield tuple({"bridge_directory": item["bridge_directory"]} for item in native_sources)
        thread.join(timeout=30)
        if thread.is_alive():
            raise NimbusScoreError("Nimbus worker did not close every native image source")
        if errors:
            raise NimbusScoreError(f"Nimbus native image validation failed; see {root}") from errors[0]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence.get("schema_version") != "mif-nimbus-native-evidence/v1" or len(evidence.get("sources", ())) != len(sources):
            raise NimbusScoreError("Nimbus native source evidence is incomplete")
        successful = True
    finally:
        if not successful:
            (root / "cancel").touch(exist_ok=True)
            thread.join(timeout=10)
