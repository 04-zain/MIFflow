"""Isolated detector drafts; the existing tissue-setup owner imports the chosen model.

The `TissueAuthoringResult` record carries `classifier_path`, `classifier_name`,
`classifier_sha256`, `channel_index`, `resolution_index` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
from pathlib import Path
import hashlib, json, os, re, secrets

from mif_pipeline.tissue_detection.setup import MAX_TISSUE_CLASSIFIER_BYTES

_LIMIT = 16 * 1024 * 1024

# ==============================================================================
#                             Tissue authoring draft
# ==============================================================================


def _read(path, limit=_LIMIT):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("The tissue-detector draft is missing, linked or too large: " + str(path))
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The tissue-detector draft exceeds its size limit.")
    return data


def _json(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("The tissue-detector draft contains repeated fields.")
            value[key] = item
        return value

    return json.loads(_read(path), object_pairs_hook=unique)


def _write(path, value):
    body = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    with temporary.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class TissueAuthoringResult:
    classifier_path: Path
    classifier_name: str
    classifier_sha256: str
    channel_index: int
    resolution_index: int
    draft_project: Path


def _prepare_native(runner, folder, request):
    from mif_pipeline.qupath_runtime.project import ProjectLayout
    from mif_pipeline.qupath_runtime.scripts import (
        materialize_script_bundle,
        bundle_script_path,
        QuPathScriptAsset,
    )

    support = folder / "support"
    support.mkdir(exist_ok=True)
    layout = ProjectLayout(
        support,
        support / "project.qpproj",
        support / "scripts",
        support / "pixel",
        support / "object",
        support / "data",
        support / "exchange",
        support / "checkpoints",
        support / "provenance",
        support / "runs",
        support / "config.json",
        "{}",
    )
    bundle = materialize_script_bundle(layout, (QuPathScriptAsset.TISSUE_AUTHORING_DRAFT,))
    script = bundle_script_path(bundle, QuPathScriptAsset.TISSUE_AUTHORING_DRAFT)
    operation = folder / ("prepare-" + secrets.token_hex(8))
    operation.mkdir()
    request_path = operation / "request.json"
    result_path = operation / "result.json"
    _write(request_path, {**request, "draft_folder": str(folder / "project")})
    runner.run_evidence_script(
        project_file=folder / "project/project.qpproj",
        script=script,
        request_paths=(request_path,),
        log_directory=operation / "logs",
        require_existing_project=False,
        evidence_path=result_path,
    )
    return _json(result_path)


def edit_tissue_detector(runner, source_project, selection, *, image_appearance_path=None):
    source = Path(source_project)
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("Open the saved analysis project before creating a detector.")
    if (
        not isinstance(selection, dict)
        or set(selection) != ({"entry_id", "method"} if selection.get("method") == "threshold"
                              else {"entry_id", "method", "setup"})
        or selection["method"] not in ("threshold", "rf", "svm")
    ):
        raise ValueError("Select the current tissue image and a detector method.")
    entry = selection["entry_id"]
    if not isinstance(entry, str) or not entry or len(entry) > 128:
        raise ValueError("Select the tissue image to edit.")
    before = hashlib.sha256(_read(source, 4 * 1024 * 1024)).hexdigest()
    identity = hashlib.sha256((str(source) + "\n" + entry + "\n" + before).encode()).hexdigest()[
        :24
    ]
    folder = source.parent.parent / "tissue-detector-drafts" / identity
    for ancestor in (*reversed(folder.parents), folder):
        if ancestor.is_symlink():
            raise ValueError("Select a local analysis folder without linked draft directories.")
    folder.mkdir(parents=True, exist_ok=True)
    request = {
        "schema": "tissue-authoring-draft/v1",
        "source_project": str(source),
        "source_sha256": before,
        "source_entry_id": entry,
    }
    receipt = folder / "draft.json"
    if receipt.exists():
        result = _json(receipt)
    else:
        result = _prepare_native(runner, folder, request)
        _write(receipt, result)
    expected = {**request, "project_file": str(folder / "project/project.qpproj"), "entry_id": "1"}
    if result != expected or not (folder / "project/project.qpproj").is_file():
        raise ValueError(
            "This detector draft does not match the current project image. Retain the draft and create a new local analysis to continue."
        )
    if hashlib.sha256(_read(source, 4 * 1024 * 1024)).hexdigest() != before:
        raise ValueError("The source project changed while preparing the detector draft.")
    project = Path(result["project_file"])
    session = secrets.token_hex(16)
    from mif_pipeline.qupath_runtime.project import ProjectLayout
    from mif_pipeline.qupath_runtime.scripts import (
        materialize_script_bundle,
        registered_script_assets,
    )

    base = project.parent
    (base / "data").mkdir(exist_ok=True)
    layout = ProjectLayout(
        base,
        project,
        base / "scripts",
        base / "classifiers/pixel_classifiers",
        base / "classifiers/object_classifiers",
        base / "data",
        base / "exchange",
        base / "checkpoints",
        base / "provenance",
        base / "runs",
        base / "config.json",
        "{}",
    )
    materialize_script_bundle(layout, registered_script_assets())
    _write(
        project.parent / "tissue-session.json",
        {"schema": "tissue-authoring-session/v1", "session_id": session},
    )
    completed = runner.run_interactive_project(
        project,
        log_directory=folder / "logs",
        tissue_authoring={**selection, "entry_id": "1"},
        image_appearance_path=image_appearance_path,
    )
    if (
        getattr(completed, "returncode", None) != 0
        or getattr(getattr(completed, "drain_evidence", None), "termination_reason", None)
        != "normal_exit"
    ):
        raise ValueError(
            "QuPath did not close normally. The detector draft remains at "
            + str(project)
            + ". Reopen the draft through Tissue detection, save the detector and close QuPath before importing it."
        )
    if hashlib.sha256(_read(source, 4 * 1024 * 1024)).hexdigest() != before:
        raise ValueError(
            "The analysis project changed in another application. The detector draft was retained; reopen the original analysis before importing it."
        )
    output = project.parent / "tissue-result.json"
    if not output.exists():
        return None
    saved = _json(output)
    if not isinstance(saved, dict) or saved.get("session_id") != session:
        return None
    fields = {
        "schema",
        "session_id",
        "classifier_name",
        "classifier_sha256",
        "channel_index",
        "resolution_index",
    }
    if set(saved) != fields or saved["schema"] != "tissue-authoring-result/v1":
        raise ValueError("QuPath did not return a valid detector save.")
    name = saved["classifier_name"]
    if not isinstance(name, str) or re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name) is None:
        raise ValueError("The returned detector name is invalid.")
    for key in ("channel_index", "resolution_index"):
        if type(saved[key]) is not int or not 0 <= saved[key] < 512:
            raise ValueError("The returned detector image settings are invalid.")
    classifier = project.parent / "classifiers/pixel_classifiers" / (name + ".json")
    if hashlib.sha256(_read(classifier, MAX_TISSUE_CLASSIFIER_BYTES)).hexdigest() != saved["classifier_sha256"]:
        raise ValueError(
            "The saved detector changed after QuPath closed. Reopen its draft and save it again."
        )
    return TissueAuthoringResult(
        classifier,
        name,
        saved["classifier_sha256"],
        saved["channel_index"],
        saved["resolution_index"],
        project,
    )
