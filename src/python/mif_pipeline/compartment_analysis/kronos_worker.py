"""Serve bounded encoder and spatial-model operations for a managed QuPath client.

Each frame carries a request ID, structured metadata and explicit binary array payloads.
Initialization checks the selected model package and returns the worker release/protocol.
The worker retains the current numerical session and its requested device, then returns
calculated arrays and diagnostics to the caller."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import json
import struct
from typing import BinaryIO

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024


# ==============================================================================
#                                 Kronos worker
# ==============================================================================


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise EOFError("The KRONOS worker frame ended before its declared length")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate worker metadata key: " + key)
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON values are not valid worker metadata")


def read_frame(stream: BinaryIO):
    prefix = stream.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise EOFError("Truncated KRONOS frame prefix")
    size = struct.unpack(">I", prefix)[0]
    if not 1 <= size <= MAX_METADATA_BYTES:
        raise ValueError("Worker metadata exceeds the frame limit")
    header = json.loads(
        _read_exact(stream, size).decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if type(header) is not dict:
        raise ValueError("Worker metadata must be an object")
    length = header.get("byte_count", 0)
    if type(length) is not int or not 0 <= length <= MAX_BINARY_BYTES:
        raise ValueError("Worker binary payload exceeds the frame limit")
    return header, _read_exact(stream, length)


def write_frame(stream: BinaryIO, header: dict, body: bytes = b"") -> None:
    if type(header) is not dict or type(body) is not bytes or len(body) > MAX_BINARY_BYTES:
        raise ValueError("Worker output requires bounded metadata and binary bytes")
    values = {**header, "byte_count": len(body)}
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("Worker metadata exceeds the frame limit")
    stream.write(struct.pack(">I", len(data)))
    stream.write(data)
    stream.write(body)
    stream.flush()


def train_graph_frame(header, body, *, cancelled, on_epoch, source_features=None):
    import numpy as np
    from mif_pipeline.compartment_analysis.patch_graph import PatchNode, PatchScope, PatchGraph
    from mif_pipeline.compartment_analysis.graph_training import (
        GraphTrainingSettings,
        train_mean_self,
    )

    required = {"nodes", "labels", "class_order", "feature_width", "settings"}
    if set(header) != required:
        raise ValueError("Graph-fit metadata fields are invalid")
    nodes = header["nodes"]
    width = header["feature_width"]
    if (
        type(nodes) is not list
        or not 1 <= len(nodes) <= 50000
        or type(width) is not int
        or not 1 <= width <= 4096
        or (source_features is None and len(body) != len(nodes) * width * 4)
    ):
        raise ValueError("Graph-feature dimensions or byte count are invalid")
    records = []
    for node in nodes:
        if type(node) is not dict or set(node) != {"node_id", "grid_row", "grid_col", "scope"}:
            raise ValueError("Graph node fields are invalid")
        scope = node["scope"]
        if type(scope) is not dict or set(scope) != set(PatchScope.__dataclass_fields__):
            raise ValueError("Graph scope fields are invalid")
        records.append(
            PatchNode(node["node_id"], node["grid_row"], node["grid_col"], PatchScope(**scope))
        )
    graph = PatchGraph(records, max_nodes=50000, max_index_bytes=50000 * 16)
    if source_features is None:
        matrix = np.frombuffer(body, dtype="<f4").reshape(len(nodes), width)
        features = {node.node_id: matrix[row] for row, node in enumerate(records)}
    else:
        if len(source_features) != len(nodes) or any(
                value.shape != (width,) or value.dtype != np.dtype("<f4")
                for value in source_features):
            raise ValueError("Mapped graph rows differ from the declared feature dimensions")
        features = {node.node_id: source_features[row] for row, node in enumerate(records)}
    settings = GraphTrainingSettings(**header["settings"])
    from mif_pipeline._remote_transport import selected_profile, RemoteKernelSession

    profile = selected_profile("graph_training")
    if profile is not None:
        import hashlib

        # The selected fitter validates and prepares numerical training arrays.
        # Retain only the expected labelled-node order for its bounded result.
        ids = tuple(node.node_id for node in graph.nodes if node.node_id in header["labels"])
        if cancelled is not None and cancelled():
            raise InterruptedError("Remote graph fit cancelled before transfer")
        session = RemoteKernelSession(profile)
        try:
            result, data = session.request("graph_train", header,
                body or np.stack([features[node.node_id] for node in records]).tobytes(order="C"), cancelled=cancelled)
        finally:
            session.close()
        shapes = [
            ("self_weight", [width, settings.hidden_width]),
            ("neighbour_weight", [width, settings.hidden_width]),
            ("bias", [settings.hidden_width]),
            ("head_weight", [settings.hidden_width, len(header["class_order"])]),
            ("head_bias", [len(header["class_order"])]),
        ]
        expected_arrays = [{"name": name, "shape": shape} for name, shape in shapes]
        assignment = hashlib.sha256(
            json.dumps(
                [(name, header["labels"][name]) for name in ids],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        expected_fields = {
            "kind",
            "class_order",
            "training_node_ids",
            "loss_history",
            "label_assignments_sha256",
            "arrays",
        }
        if (
            set(result) != expected_fields
            or result["kind"] != "graph_fit"
            or result["class_order"] != header["class_order"]
            or result["training_node_ids"] != list(ids)
            or result["label_assignments_sha256"] != assignment
            or result["arrays"] != expected_arrays
            or len(data) != 4 * sum(int(np.prod(shape)) for _, shape in shapes)
            or len(result["loss_history"]) != settings.epochs
            or not np.isfinite(result["loss_history"]).all()
            or not np.isfinite(np.frombuffer(data, dtype="<f4")).all()
        ):
            raise ValueError(
                "Remote graph result differs from the current training scope or array contract"
            )
        return result, data
    learned = train_mean_self(
        graph,
        features,
        header["labels"],
        tuple(header["class_order"]),
        settings,
        cancelled=cancelled,
        on_epoch=on_epoch,
    )
    arrays = [
        ("self_weight", learned.parameters.self_weight),
        ("neighbour_weight", learned.parameters.neighbour_weight),
        ("bias", learned.parameters.bias),
        ("head_weight", learned.head_weight),
        ("head_bias", learned.head_bias),
    ]
    metadata = {
        "kind": "graph_fit",
        "class_order": list(learned.training_class_ids),
        "training_node_ids": list(learned.training_node_ids),
        "loss_history": list(learned.loss_history),
        "label_assignments_sha256": learned.label_assignments_sha256,
        "arrays": [{"name": name, "shape": list(value.shape)} for name, value in arrays],
    }
    return metadata, b"".join(value.astype("<f4", copy=False).tobytes() for name, value in arrays)


class WorkerSession:
    """Prepare a native encoder or fit supplied features; never receive image crops."""

    def __init__(self):
        self.encoder = None
        self.session_dir = None
        self.last_request_id = 0

    @staticmethod
    def _fields(header, expected):
        if set(header) != set(expected) | {"op", "request_id", "byte_count"}:
            raise ValueError("Worker request fields do not match this operation")

    def dispatch(self, header, body, *, on_progress=None):
        from pathlib import Path

        request_id = header.get("request_id")
        if type(request_id) is not int or request_id <= self.last_request_id:
            raise ValueError("Worker request IDs must increase within the session")
        self.last_request_id = request_id
        op = header.get("op")
        if op == "initialize_fit":
            self._fields(header, {"threads", "session_dir"})
            directory = Path(header["session_dir"])
            if (body or self.session_dir is not None or type(header["threads"]) is not int
                    or not 1 <= header["threads"] <= 8 or not directory.is_absolute()
                    or not directory.is_dir()):
                raise ValueError("Fit initialization requires a local session and bounded threads")
            import torch
            torch.set_num_threads(header["threads"])
            self.session_dir = directory
            from mif_pipeline._release import RELEASE_ID, KRONOS_PROTOCOL
            return {"kind": "ready", "request_id": request_id,
                    "worker_release": RELEASE_ID, "worker_protocol": KRONOS_PROTOCOL}, b""
        if op == "initialize":
            fields = {
                "package_dir",
                "package_sha256",
                "marker_names",
                "divisors",
                "pixel_size_um",
                "patch_size",
                "additional_markers",
                "marker_text_model",
                "nuclear_channel",
                "threads",
                "session_dir",
            }
            if "activation_budget_bytes" in header:
                fields.add("activation_budget_bytes")
            self._fields(header, fields)
            if body or self.session_dir is not None:
                raise ValueError("The worker can only initialize once, without a binary payload")
            threads = header["threads"]
            if type(threads) is not int or not 1 <= threads <= 8:
                raise ValueError("Worker thread count must be between one and eight")
            directory = Path(header["session_dir"])
            if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
                raise ValueError(
                    "Worker session directory must be an existing absolute local directory"
                )
            from mif_pipeline.compartment_analysis.kronos_encoder import (
                inspect_kronos_package,
                FrozenKronosEncoder,
            )
            from mif_pipeline.compartment_analysis.kronos_panel import KronosPanel

            package = inspect_kronos_package(Path(header["package_dir"]))
            if package.sha256 != header["package_sha256"]:
                raise ValueError("The selected KRONOS package differs from the requested revision")
            from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
                KronosAdditionalMarker, KronosMarkerTextModel,
            )
            panel = KronosPanel.from_csv(
                package.root / package.marker_csv_name,
                patch_size=header["patch_size"],
                additional_markers=tuple(KronosAdditionalMarker.from_mapping(row)
                                         for row in header["additional_markers"]),
                marker_text_model=(None if header["marker_text_model"] is None else
                                   KronosMarkerTextModel.from_mapping(header["marker_text_model"])),
                marker_names=tuple(header["marker_names"]),
                intensity_divisors=tuple(header["divisors"]),
                pixel_size_um=tuple(header["pixel_size_um"]),
                nuclear_channel=header["nuclear_channel"],
            )
            import torch

            torch.set_num_threads(threads)
            self.encoder = FrozenKronosEncoder(
                package,
                panel,
                activation_budget_bytes=header.get("activation_budget_bytes", 512 * 1024**2),
                preparation_directory=directory,
            )
            self.session_dir = directory
            from mif_pipeline._release import RELEASE_ID, KRONOS_PROTOCOL

            return {
                "kind": "ready",
                "request_id": request_id,
                "worker_release": RELEASE_ID,
                "worker_protocol": KRONOS_PROTOCOL,
                "package_sha256": package.sha256,
                "panel_sha256": panel.sha256,
                "model_marker_names": list(panel.model_marker_names),
            }, b""
        if op == "close":
            self._fields(header, set())
            if body:
                raise ValueError("Close must not contain a binary payload")
            self.close()
            return {"kind": "closed", "request_id": request_id}, b""
        if self.session_dir is None:
            raise ValueError("Initialize the private preparation or fitting worker")
        if op == "export_native":
            self._fields(header, {"destination"})
            if body or self.encoder is None:
                raise ValueError("Native export requires an initialized model without image payload")
            destination = Path(header["destination"])
            if not destination.is_relative_to(self.session_dir):
                raise ValueError("Native export must remain in its owned session directory")
            from mif_pipeline.compartment_analysis.kronos_native import export_native_encoder
            deployment = export_native_encoder(self.encoder, destination)
            return {"kind": "native_export", "request_id": request_id,
                    "deployment": deployment}, b""
        if op == "train_graph_dataset":
            self._fields(header, {"dataset"})
            if body:
                raise ValueError("A local graph fit reads its dataset directly")
            directory = Path(header["dataset"])
            if (not directory.is_absolute() or directory.is_symlink()
                    or not directory.resolve().is_relative_to(self.session_dir.resolve())):
                raise ValueError("Graph fitting requires a completed input inside its owned session")
            request_path = directory / "task.json"
            if request_path.stat().st_size > 32 * 1024**2:
                raise ValueError("Graph task metadata exceeds its node budget")
            task = json.loads(request_path.read_bytes(), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
            cancel_path = self.session_dir / f"cancel-{request_id}"

            def progress(epoch, loss):
                if on_progress is not None:
                    on_progress(
                        {"kind": "progress", "request_id": request_id, "epoch": epoch, "loss": loss}
                    )

            from mif_pipeline.compartment_analysis.frozen_features import mapped_patch_rows
            selection = task.pop("feature_sources")
            with mapped_patch_rows(selection, len(task["nodes"])) as source_features:
                metadata, payload = train_graph_frame(task, b"", source_features=source_features,
                    cancelled=cancel_path.exists, on_epoch=progress)
            metadata["request_id"] = request_id
            return metadata, payload
        raise ValueError("Unsupported KRONOS worker operation")

    def close(self):
        if self.encoder is not None:
            self.encoder.close()
            self.encoder = None


def main():
    import sys

    # Only the framed protocol goes to stdout; custom-model diagnostics stay on stderr.
    output = sys.stdout.buffer
    source = sys.stdin.buffer
    session = WorkerSession()
    from contextlib import redirect_stdout

    try:
        while True:
            frame = read_frame(source)
            if frame is None:
                return 0
            header, body = frame
            try:
                with redirect_stdout(sys.stderr):
                    response, binary = session.dispatch(
                        header, body, on_progress=lambda event: write_frame(output, event)
                    )
            except Exception as error:
                write_frame(
                    output,
                    {
                        "kind": "error",
                        "request_id": header.get("request_id"),
                        "message": f"{type(error).__name__}: {error}"[:2000],
                    },
                )
                return 1
            write_frame(output, response, binary)
            if response["kind"] == "closed":
                return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
