"""

Saves local progress through manual alignment and cluster fitting.

We retain the selected cluster review, local QuPath project, submitted job
and whether final alignment was accepted for application. The local alignment
draft and global-fit receipt carry its phase: starting position, returned global
fit and optional local refinement. These records let the desktop reopen the same
project after restarting without duplicating the scientific phase in GUI state.
A completed job receipt remains until local preparation succeeds, so a failed
mounted-image read can be retried without submitting the fit again.

A submission with an uncertain result is recorded explicitly so restarting
the desktop does not automatically submit the same fitting job again.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
import json, os, re, tempfile

FIELDS = {
    "schema",
    "scope",
    "request",
    "local_project",
    "receipt",
    "returned",
    "submission_uncertain",
}


# ==============================================================================
#                             Cluster landmark state
# ==============================================================================


def validate_state(value, scope, folder):
    """

    Checks that the saved editing progress belongs to this analysis connection and local folder.

    The connection description must equal the one currently selected. When a
    local project is recorded, its absolute path must remain inside this analysis
    folder. A saved job must identify either landmark export or landmark import,
    with a positive scheduler ID and a text result location.

    Args:
        value: Saved landmark-session-state/v1 dictionary with the current review,
            local project, job receipt and returned/submission flags.
        scope: Analysis-connection description expected by the current desktop.
        folder: Local directory within which the landmark project must reside.

    Returns:
        Checked state used by the desktop to resume editing or job retrieval.

    Raises:
        ValueError: The connection, project location, flags, review or saved job
            fields are invalid or belong to another analysis.
    """

    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or value["schema"] != "landmark-session-state/v1"
        or value["scope"] != scope
    ):
        raise ValueError("The saved alignment session belongs to a different analysis connection.")
    if type(value["returned"]) is not bool or type(value["submission_uncertain"]) is not bool:
        raise ValueError("The saved alignment session is invalid.")
    if value["request"] is not None:
        from mif_pipeline.orchestration.batch_cli import _response_text

        _response_text(json.dumps(value["request"], allow_nan=False))
    project = value["local_project"]
    if project is not None:
        if (
            not isinstance(project, str)
            or not Path(project).is_absolute()
            or ".." in Path(project).parts
            or not Path(project).is_relative_to(folder)
        ):
            raise ValueError("The saved alignment draft is outside this analysis folder.")
    receipt = value["receipt"]
    if receipt is not None:
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"job_id", "operation", "result_file"}
            or receipt["operation"] not in {"landmark-export", "landmark-import"}
            or not re.fullmatch("[1-9][0-9]{0,17}", str(receipt["job_id"]))
        ):
            raise ValueError("The saved alignment job is invalid.")
        if type(receipt["result_file"]) is not str:
            raise ValueError("The saved alignment job has no result location.")
    return value


def load_state(path, scope):
    """

    Reads previously saved alignment progress when the desktop reopens the analysis.

    We limit the file to 64 KiB and decode it with duplicate-field and nonfinite-
    number rejection. The expected connection and local directory are then
    checked before the saved project or job can be offered in the interface.

    Args:
        path: Local filename containing the manual alignment progress.
        scope: Current analysis-connection description.

    Returns:
        Checked state dictionary, or None when no progress file exists.

    Raises:
        ValueError: The file cannot be read in its current format or its saved
            connection/project/job no longer matches the analysis.
    """

    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("The saved alignment session cannot be read safely.")

    def unique(pairs):
        """

        Requires each field name in a decoded JSON object to occur once.

        Args:
            pairs: Ordered key/value pairs from the JSON decoder.

        Returns:
            Dictionary containing the saved fields.

        Raises:
            ValueError: Repeated text keys would overwrite an earlier value.
        """

        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Repeated alignment session field.")
            value[key] = item
        return value

    value = json.loads(
        path.read_text(),
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(
            ValueError("Invalid alignment session number.")
        ),
    )
    return validate_state(value, scope, path.parent)


def save_state(path, value):
    """

    Writes the current manual alignment progress through a temporary file beside its destination.

    We validate the state, encode the complete fields and require the result
    to fit within 64 KiB. The bytes are written and flushed to a new temporary
    file before replacing the progress filename. Cleanup removes a remaining
    temporary file if writing or replacement raises an exception.

    Args:
        path: Progress filename inside the local analysis folder.
        value: Current review, local project, job result and submission flags.

    Raises:
        ValueError: State fields, project location or encoded size are invalid.
        OSError: Creating, writing, flushing or replacing the progress file fails.
    """

    path = Path(path)
    validate_state(value, value["scope"], path.parent)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 65536:
        raise ValueError("The alignment session is too large.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("The alignment session folder is linked.")
    descriptor, name = tempfile.mkstemp(prefix=".landmark-session-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
