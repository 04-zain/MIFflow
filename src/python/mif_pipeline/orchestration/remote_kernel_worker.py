"""Execute the numerical operations offered by an allocated compute-node worker.

The worker verifies release/protocol identity before dispatching a request to the supported
graph-training operation. Length and checksum checks bound the exchanged
arrays. Results return through the same framed channel; the local stage remains responsible
for review and scientific publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
import sys
from mif_pipeline._remote_transport import PROTOCOL, read_frame, write_frame
from mif_pipeline._release import RELEASE_ID

# ==============================================================================
#                              Remote kernel worker
# ==============================================================================


class KernelWorker:
    def __init__(self):
        self.last_id = 0
        self.release = RELEASE_ID

    def dispatch(self, operation, metadata, payload):
        if operation == "probe":
            if metadata or payload:
                raise ValueError("Probe must not contain data")
            versions = {name: importlib.metadata.version(name) for name in ("numpy", "scipy")}
            return {
                "kind": "ready",
                "job_id": os.environ.get("SLURM_JOB_ID"),
                "versions": versions,
            }, b""
        if operation == "graph_train":
            from mif_pipeline.compartment_analysis.kronos_worker import train_graph_frame

            return train_graph_frame(metadata, payload, cancelled=lambda: False, on_epoch=None)
        raise ValueError("Unsupported numerical operation")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run isolated numerical kernels on an allocated compute node"
    )
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    os.environ["MIF_REMOTE_WORKER"] = "1"
    worker = KernelWorker()
    if args.probe:
        value, _ = worker.dispatch("probe", {}, b"")
        print(json.dumps(dict(value, protocol=PROTOCOL, release=RELEASE_ID)))
        return 0
    if not args.stdio:
        parser.error("Select --stdio or --probe")
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    sys.stdout = sys.stderr  # Third-party model diagnostics must never corrupt the binary stream.
    while True:
        frame = read_frame(input_stream)
        if frame is None:
            return 0
        request, payload = frame
        expected = {
            "protocol",
            "request_id",
            "operation",
            "release",
            "metadata",
            "byte_count",
            "payload_sha256",
        }
        if (
            set(request) != expected
            or request["protocol"] != PROTOCOL
            or type(request["request_id"]) is not int
            or request["request_id"] <= worker.last_id
        ):
            raise ValueError("Remote request envelope is invalid or replayed")
        worker.last_id = request["request_id"]
        metadata = {}
        body = b""
        error = None
        try:
            if request["release"] != worker.release:
                raise ValueError(
                    "The worker release differs. Install the same prototype release on the cluster before processing."
                )
            metadata, body = worker.dispatch(request["operation"], request["metadata"], payload)
        except Exception as cause:
            error = (type(cause).__name__ + ": " + str(cause))[:4000]
        response = {k: request[k] for k in ("protocol", "request_id", "operation", "release")}
        write_frame(output_stream, dict(response, metadata=metadata, error=error), body)



if __name__ == "__main__":
    raise SystemExit(main())
