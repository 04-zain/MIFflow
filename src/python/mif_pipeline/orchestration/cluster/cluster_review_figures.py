"""Small derived figures transported with their exact current review identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import fields
from pathlib import Path
import base64, hashlib, json, os
from ..batch_service_models import BatchReviewPlot, BatchReviewPlots

LIMIT = 6 * 1024 * 1024
SCHEMA = "cluster-review-figures/v1"


# ==============================================================================
#                             Cluster review figures
# ==============================================================================


def encode_figures(result):
    if type(result) is not BatchReviewPlots:
        raise TypeError("Expected current review figures")
    value = {
        key: getattr(result, key)
        for key in (
            "target_id",
            "interaction_request_id",
            "state_head_sha256",
            "interaction_context_sha256",
        )
    }
    plots = []
    total = 0
    for plot in result.plots:
        path = plot.path
        if path.is_symlink() or path.suffix not in {".png", ".pdf", ".svg", ".csv"}:
            raise ValueError("Unsupported review figure")
        with path.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        total += len(data)
        if total > LIMIT or hashlib.sha256(data).hexdigest() != plot.sha256:
            raise ValueError("Review figure changed or exceeds the response limit")
        plots.append(
            {
                "artifact_id": plot.artifact_id,
                "title": plot.title,
                "sha256": plot.sha256,
                "format": path.suffix[1:],
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    return {"schema_version": SCHEMA, **value, "plots": plots}


def materialize_figures(value, folder):
    keys = {
        "schema_version",
        "target_id",
        "interaction_request_id",
        "state_head_sha256",
        "interaction_context_sha256",
        "plots",
    }
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != SCHEMA:
        raise ValueError("Invalid review-figure response")
    if not isinstance(value["plots"], list) or len(value["plots"]) > 16:
        raise ValueError("Invalid review-figure count")
    folder = Path(folder)
    if not folder.is_absolute():
        raise ValueError("Choose an absolute figure cache folder")
    data_by_path = {}
    plots = []
    total = 0
    for item in value["plots"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"artifact_id", "title", "sha256", "format", "data"}
            or item["format"] not in {"png", "pdf", "svg", "csv"}
        ):
            raise ValueError("Invalid review figure")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid review figure bytes") from error
        total += len(data)
        if total > LIMIT or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Review figure checksum differs")
        path = folder / (item["sha256"] + "." + item["format"])
        plots.append(
            BatchReviewPlot(item["artifact_id"], item["title"], path, item["sha256"], "available")
        )
        data_by_path[path] = data
    result = BatchReviewPlots(
        value["target_id"],
        value["interaction_request_id"],
        value["state_head_sha256"],
        value["interaction_context_sha256"],
        tuple(plots),
    )
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("Figure cache is a linked folder")
    for path, data in data_by_path.items():
        if path.is_symlink():
            raise ValueError("Figure cache entry was replaced")
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            with path.open("rb") as stream:
                previous = stream.read(LIMIT + 1)
            if previous != data:
                raise ValueError("Cached review figure differs from the current result")
    return result
