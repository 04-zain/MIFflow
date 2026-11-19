"""Validate and persist the computing settings for one local analysis folder.

The record includes execution mode, device/precision preferences, cluster command paths,
mounted-image mapping and preparation/submission receipts. Reopening checks that a retained
cluster association belongs to the local setup before restoring it. Saving replaces the
settings file atomically."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path, PurePosixPath
import json, os, tempfile, re
from mif_pipeline._compute_preferences import saved_compute_preferences
from mif_pipeline.orchestration.remote_compute import RemoteProfile

MODES = ("local", "desktop", "streaming")
MAX_SETTINGS_BYTES = 65536


# ==============================================================================
#                               Execution location
# ==============================================================================


def validate_location(value):
    """Validate location for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if not isinstance(value, dict):
        raise ValueError("The saved computing settings are invalid.")
    allowed = {
        "mode",
        "device",
        "precision",
        "remote",
        "mounted_images",
        "paths_policy",
        "remote_prepared",
        "remote_receipt",
        "submission_uncertain",
        "pending_submission",
        "mounted_host_override",
        "retained_annotation_draft",
        "remote_opened",
    }
    if set(value) - allowed:
        raise ValueError("The saved computing settings contain unknown fields.")
    mode = value.get("mode", "local")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Choose This computer, Cluster desktop or Cluster streaming.")
    device, precision = saved_compute_preferences(value)
    result = dict(value, mode=mode, device=device)
    if "precision" in value:
        result["precision"] = precision
    if result.get("remote") is not None:
        result["remote"] = RemoteProfile.from_mapping(result["remote"]).to_mapping()
    if mode == "streaming" and result.get("remote") is None:
        raise ValueError("Complete the cluster connection and results location before continuing.")
    if result.get("mounted_images") is not None:
        from .mounted_images import MountedImageAccess

        result["mounted_images"] = MountedImageAccess.from_mapping(
            result["mounted_images"]
        ).to_mapping()
    if result.get("paths_policy", "derived") not in {"derived", "explicit"}:
        raise ValueError("The saved analysis path selection is invalid.")
    prepared = result.get("remote_prepared")
    receipt = result.get("remote_receipt")
    if prepared is not None:
        remote = result.get("remote", {})
        if (
            mode != "streaming"
            or not isinstance(prepared, dict)
            or set(prepared) != {"selection_sha256", "result"}
            or not isinstance(prepared["selection_sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", prepared["selection_sha256"])
        ):
            raise ValueError("The saved cluster preparation is invalid.")
        saved = prepared["result"]
        if (
            not isinstance(saved, dict)
            or saved.get("batch_id") != remote.get("batch_id")
            or saved.get("manifest") != remote.get("manifest")
            or type(saved.get("sample_count")) is not int
            or saved["sample_count"] < 1
            or not isinstance(saved.get("manifest_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", saved["manifest_sha256"])
        ):
            raise ValueError(
                "The saved cluster preparation belongs to another analysis or is incomplete."
            )
    opened = result.get("remote_opened")
    if opened is not None:
        if (
            mode != "streaming"
            or prepared is None
            or not isinstance(opened, dict)
            or set(opened)
            not in (
                {"manifest_sha256", "state_head_sha256"},
                {"manifest_sha256", "state_head_sha256", "local_setup_sha256"},
            )
            or any(
                not isinstance(item, str) or not re.fullmatch("[0-9a-f]{64}", item)
                for item in opened.values()
            )
            or opened["manifest_sha256"] != prepared["result"]["manifest_sha256"]
        ):
            raise ValueError("The opened cluster analysis does not match its saved setup.")
    if receipt is not None:
        from mif_pipeline.orchestration.cluster.cluster_annotation_jobs import STAGE_OPERATIONS
        if (
            prepared is None
            or not isinstance(receipt, dict)
            or receipt.get("host") != result["remote"]["host"]
            or receipt.get("batch_id") != result["remote"]["batch_id"]
            or receipt.get("operation") not in {"run", "resume"} | STAGE_OPERATIONS
        ):
            raise ValueError("The saved cluster job receipt belongs to another analysis.")
        from mif_pipeline.orchestration.remote_compute import RemoteComputeClient

        RemoteComputeClient._job(receipt.get("job_id"))
        if receipt["operation"] in STAGE_OPERATIONS:
            path = PurePosixPath(receipt.get("result_file", ""))
            if (path.parent != PurePosixPath(result["remote"]["working_directory"])
                    or not re.fullmatch(r"\.mif-annotations-[0-9a-f]{32}\.json", path.name)):
                raise ValueError("The saved operation result belongs to another cluster job folder.")
            if "result_received" in receipt and type(receipt["result_received"]) is not bool:
                raise ValueError("The saved cluster result state is invalid.")
        resume_head = receipt.get("resume_from_state_head_sha256")
        if resume_head is not None and (
            receipt["operation"] != "resume"
            or not isinstance(resume_head, str)
            or not re.fullmatch("[0-9a-f]{64}", resume_head)
        ):
            raise ValueError("The saved resume receipt has an invalid starting state.")
    if "mounted_host_override" in result and type(result["mounted_host_override"]) is not bool:
        raise ValueError("The image connection selection is invalid.")
    retained = result.get("retained_annotation_draft")
    if retained is not None and (
        not isinstance(retained, str)
        or not Path(retained).is_absolute()
        or len(retained) > 4096
        or any(ord(c) < 32 for c in retained)
    ):
        raise ValueError("The retained annotation draft location is invalid.")
    if "submission_uncertain" in result and type(result["submission_uncertain"]) is not bool:
        raise ValueError("The saved submission state is invalid.")
    pending = result.get("pending_submission")
    if pending is not None:
        from mif_pipeline.orchestration.cluster.cluster_annotation_jobs import STAGE_OPERATIONS
        if (not isinstance(pending, dict)
                or set(pending) - {"token", "operation", "requested_at", "resume_from_state_head_sha256"}
                or not re.fullmatch(r"[0-9a-f]{32}", str(pending.get("token", "")))
                or pending.get("operation") not in STAGE_OPERATIONS | {"run", "resume"}
                or type(pending.get("requested_at")) is not int or pending["requested_at"] <= 0):
            raise ValueError("The saved cluster submission identity is invalid.")
        if "resume_from_state_head_sha256" in pending and (pending["operation"] != "resume"
                or not re.fullmatch(r"[0-9a-f]{64}", str(pending["resume_from_state_head_sha256"]))):
            raise ValueError("The retained resume starting state is invalid.")
    return result


def load_location(path):
    path = Path(path)
    if not path.exists():
        return {"mode": "local", "device": "auto"}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
        raise ValueError(
            "The saved computing settings could not be read. Review them before processing."
        )
    try:
        value = validate_location(json.loads(path.read_text(encoding="utf-8")))
        expected = value.get("remote_opened", {}).get("local_setup_sha256")
        if expected is not None:
            import hashlib

            setup = path.parent / "analysis.setup.json"
            if (
                setup.is_symlink()
                or not setup.is_file()
                or hashlib.sha256(setup.read_bytes()).hexdigest() != expected
            ):
                raise ValueError(
                    "The local setup does not match this cluster connection. Reopen the original local analysis folder; the cluster analysis is unchanged."
                )
        return value
    except (OSError, TypeError, KeyError) as error:
        raise ValueError("The saved computing settings are incomplete.") from error


def save_location(path, value):
    result = validate_location(value)
    payload = json.dumps(result, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_SETTINGS_BYTES:
        raise ValueError("Computing settings exceed the supported size.")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Select the analysis settings file directly.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".computing-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return result


def streaming_image_access(settings):
    """Require one mounted parent covering the processed images before running."""
    from .mounted_images import MountedImageAccess

    mounted = MountedImageAccess.from_mapping(settings.get("mounted_images"))
    remote = RemoteProfile.from_mapping(settings["remote"])
    try:
        mounted.local_path(str(PurePosixPath(remote.output_root) / remote.batch_id / "samples"))
    except ValueError as error:
        raise ValueError(
            "The mounted folder must include your images and the location selected for saving this analysis."
        ) from error
    return mounted
