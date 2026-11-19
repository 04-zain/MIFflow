"""Bounded presentation of existing cluster review records, with no file adoption.

The wire digest detects changed transport content. The existing response owner still checks
the current batch head, exact target, action and scientific values.

The `ClusterReviewPage` record carries the current review page, saved-state
identity and processing-machine segmentation model location.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import hashlib, json, re, sys
from mif_pipeline.pipeline_core import StageResult
from ..batch_artifacts import AnalysisDomainBinding
from ..batch_interactions import BatchInteractionPage, BatchInteractionTarget, BatchInteractionGroup, BatchAggregateInteractionGroup, ReferenceOnlyQuPathView, group_interactions, BatchInteractionQuery
from ..batch_phenotyping.review_records import BatchClusterReviewEvidence
from ..batch_phenotyping.review_target import AggregateClusterReviewTarget

SCHEMA = "mif-cluster-review-page/v1"
MAX_BYTES = 8 * 1024 * 1024


# ==============================================================================
#                              Cluster review pages
# ==============================================================================


def _plain(value):
    if isinstance(value, StageResult):
        return _plain(value.to_mapping())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Unsupported cluster review value")


def _bytes(value):
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(result) > MAX_BYTES:
        raise ValueError("Cluster review page exceeds its size limit")
    return result


def _closed(value, names, label):
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise ValueError(label + " fields are invalid")
    return dict(value)


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ClusterReviewPage:
    batch_id: str
    state_head_sha256: str
    page: BatchInteractionPage
    segmentation_model_directory: str | None = None
    kronos_setup: dict | None = None
    segmentation_model_directories: dict | None = None


def encode_page(batch_id, state_head_sha256, page, *, segmentation_model_directory=None, kronos_setup=None, segmentation_model_directories=None):
    if type(page) is not BatchInteractionPage:
        raise TypeError("Expected a current review page")
    groups = []
    for group in page.groups:
        if type(group) is BatchInteractionGroup:
            groups.append({"kind": "individual", "record": _plain(group)})
        elif type(group) is BatchAggregateInteractionGroup:
            groups.append(
                {
                    "kind": "pooled",
                    "record": {
                        "target": group.target.to_mapping(),
                        "evidence": group.evidence.to_mapping(),
                        "validation_domain_ids": list(group.validation_domain_ids),
                    },
                }
            )
        else:
            raise ValueError("Unknown cluster review kind")
    value = {
        "schema_version": SCHEMA,
        "batch_id": batch_id,
        "state_head_sha256": state_head_sha256,
        "groups": groups,
        "total_count": page.total_count,
        "offset": page.offset,
        "limit": page.limit,
        "next_offset": page.next_offset,
        "qupath_project_path": page.qupath_project_path,
        "segmentation_model_directory": segmentation_model_directory,
        "segmentation_model_directories": segmentation_model_directories or {},
        "kronos_setup": kronos_setup or {},
    }
    value["content_sha256"] = _digest(value)
    return value


def _target(value):
    item = _closed(value, (f.name for f in fields(BatchInteractionTarget)), "Review target")
    item["stage_result"] = StageResult.from_mapping(item["stage_result"])
    bindings = item["analysis_domain_bindings"]
    if not isinstance(bindings, list) or len(bindings) > 10000:
        raise ValueError("Invalid review region inventory")
    item["analysis_domain_bindings"] = tuple(
        AnalysisDomainBinding.from_mapping(b) for b in bindings
    )
    if item["reference_view"] is not None:
        item["reference_view"] = ReferenceOnlyQuPathView(
            **_closed(
                item["reference_view"],
                (f.name for f in fields(ReferenceOnlyQuPathView)),
                "Paired reference",
            )
        )
    return BatchInteractionTarget(**item)


def decode_page(value):
    item = _closed(
        value,
        {
            "schema_version",
            "batch_id",
            "state_head_sha256",
            "groups",
            "total_count",
            "offset",
            "limit",
            "next_offset",
            "qupath_project_path",
            "content_sha256",
            "segmentation_model_directory",
            "segmentation_model_directories",
            "kronos_setup",
        },
        "Cluster review page",
    )
    checksum = item.pop("content_sha256")
    if item["schema_version"] != SCHEMA or checksum != _digest(item):
        raise ValueError("Cluster review content has changed")
    setup = item["kronos_setup"]
    if (not isinstance(setup, dict) or (setup and set(setup) != {
            "package_directory", "package_sha256", "worker_command", "marker_csv", "marker_text_model"})):
        raise ValueError("The cluster KRONOS setup is invalid")
    if setup and setup["marker_text_model"] is not None:
        from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel
        KronosMarkerTextModel.from_mapping(setup["marker_text_model"])
    model_directory = item["segmentation_model_directory"]
    directories = item["segmentation_model_directories"]
    if (not isinstance(directories, dict) or not set(directories) <= {"instanseg", "cellpose", "stardist"}
            or any(not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                   or ".." in PurePosixPath(path).parts or "\0" in path for path in directories.values())):
        raise ValueError("The cluster segmentation model folders are invalid")
    if model_directory is not None and (
        not isinstance(model_directory, str)
        or not PurePosixPath(model_directory).is_absolute()
        or ".." in PurePosixPath(model_directory).parts
        or "\0" in model_directory
    ):
        raise ValueError("The cluster segmentation model folder is invalid")
    if not isinstance(item["batch_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item["batch_id"]
    ):
        raise ValueError("Invalid cluster analysis name")
    if not isinstance(item["state_head_sha256"], str) or not re.fullmatch(
        "[0-9a-f]{64}", item["state_head_sha256"]
    ):
        raise ValueError("Invalid saved review identity")
    raw = item["groups"]
    if not isinstance(raw, list) or len(raw) > 200:
        raise ValueError("Cluster review list is too large")
    groups = []
    for value in raw:
        group = _closed(value, {"kind", "record"}, "Cluster review group")
        if group["kind"] == "individual":
            record = _closed(
                group["record"],
                (f.name for f in fields(BatchInteractionGroup)),
                "Individual review",
            )
            if not isinstance(record["targets"], list) or len(record["targets"]) > 1000:
                raise ValueError("Invalid review target count")
            targets = tuple(_target(t) for t in record["targets"])
            built = group_interactions(targets)
            if len(built) != 1 or _plain(built[0]) != record:
                raise ValueError("The review list differs from its displayed targets")
            groups.append(built[0])
        elif group["kind"] == "pooled":
            record = _closed(
                group["record"], {"target", "evidence", "validation_domain_ids"}, "Pooled review"
            )
            target = AggregateClusterReviewTarget.from_mapping(record["target"])
            evidence = BatchClusterReviewEvidence.from_mapping(record["evidence"])
            groups.append(
                BatchAggregateInteractionGroup(
                    target, tuple(record["validation_domain_ids"]), evidence
                )
            )
        else:
            raise ValueError("Unsupported cluster review kind")
    if len({g.group_id for g in groups}) != len(groups):
        raise ValueError("Duplicate cluster review groups")
    page = BatchInteractionPage(
        tuple(groups),
        item["total_count"],
        item["offset"],
        item["limit"],
        item["next_offset"],
        qupath_project_path=item["qupath_project_path"],
    )
    return ClusterReviewPage(item["batch_id"], item["state_head_sha256"], page, model_directory, item["kronos_setup"], directories)


def current_page(service, batch_id, query=None):
    """Return current reviews and the cluster's installed segmentation location.

    We locate the model only when its setup form is present. Software setup and
    the segmentation launch check the model files; loading a form does not hash
    those files again. The path stays in the cluster's POSIX format in transit.
    """
    before = service.status(batch_id).state_head_sha256
    page = service.list_interactions(batch_id, query or BatchInteractionQuery(limit=50))
    if service.status(batch_id).state_head_sha256 != before:
        raise ValueError(
            "The cluster analysis advanced while reviews were loading. Refresh the page."
        )
    directory = None
    directories = {}
    if any(group.interaction_reason == "segmentation_setup_required" for group in page.groups):
        from ..setup.instanseg_setup import default_instanseg_model_target
        from mif_pipeline.qupath_runtime.payload_lock import MODEL_NAME

        root = (
            Path(sys._MEIPASS) / "qupath-assets"
            if getattr(sys, "frozen", False)
            else default_instanseg_model_target("linux-x86_64")
        )
        model = root / "model" / MODEL_NAME
        if model.is_dir():
            directory = str(model)
            directories["instanseg"] = directory
        from ..setup.alternative_segmentation_setup import default_detector_directory, inspect_detector
        for method in ("cellpose", "stardist"):
            target = default_detector_directory(method)
            if inspect_detector(method, target).state == "installed":
                directories[method] = str(target)
    kronos = {}
    if any(group.interaction_reason == "training_plan_selection_required"
           for group in page.groups):
        from ..setup.kronos_setup import model_setup_snapshot
        kronos = model_setup_snapshot()
    return encode_page(batch_id, before, page, segmentation_model_directory=directory, kronos_setup=kronos,
        segmentation_model_directories=directories)
