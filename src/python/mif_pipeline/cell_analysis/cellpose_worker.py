"""One pinned Cellpose model, reused for bounded native image tiles.

The native owner supplies only explicitly selected fluorescence channels. Missing
input planes are zero-filled, never selected from the remaining multiplex panel.
Cellpose output labels describe cells; this worker does not infer nuclear labels.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.metadata
import json
import os
from pathlib import Path
import sys

import numpy as np


def prepare_channels(array: np.ndarray) -> np.ndarray:
    """Require explicit YXC planes and supply the model's three-plane tensor."""
    if array.ndim != 3 or not 1 <= array.shape[2] <= 3:
        raise ValueError("Cellpose requires one to three explicitly selected channels")
    if not np.isfinite(array).all():
        raise ValueError("Cellpose input contains nonfinite intensities")
    result = np.zeros((*array.shape[:2], 3), dtype=np.float32)
    result[..., :array.shape[2]] = array
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    options = parser.parse_args(argv)
    settings = json.loads(options.settings.read_text())
    if importlib.metadata.version("cellpose") != "4.0.9":
        raise RuntimeError("The selected checkpoint requires Cellpose 4.0.9")
    model_path = Path(settings["model_path"]) / "cpsam"
    if not model_path.is_file() or model_path.stat().st_size != 1233587898:
        raise ValueError("Install the pinned original Cellpose-SAM checkpoint in Software setup")
    with redirect_stdout(sys.stderr):
        import torch
        from cellpose.models import CellposeModel
        threads = max(1, int(os.environ.get("MIF_COMPUTE_CPU_THREADS", "1")))
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        selected = settings["device"]
        gpu = selected in {"gpu", "cuda"} or (selected == "auto" and torch.cuda.is_available())
        if gpu and not torch.cuda.is_available():
            raise RuntimeError("Cellpose GPU was selected but CUDA is unavailable")
        device = torch.device("cuda" if gpu else "cpu")
        model = CellposeModel(pretrained_model=str(model_path), device=device,
                              use_bfloat16=gpu)
    print(json.dumps({"ready": True, "device": str(device)}), flush=True)
    parameters = settings["parameters"]
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("stop") is True:
            return 0
        height, width, channels = request["shape"]
        if (not 1 <= height <= settings["tile_size_px"] + 2 or
                not 1 <= width <= settings["tile_size_px"] + 2 or
                channels != len(parameters["channels"])):
            raise ValueError("Native Cellpose tile exceeds the admitted shape or selected channels")
        array = np.fromfile(request["input"], dtype="<f4")
        image = prepare_channels(array.reshape(height, width, channels))
        with redirect_stdout(sys.stderr):
            masks, _, _ = model.eval(
                image, channel_axis=-1, batch_size=1, normalize=True,
                flow_threshold=parameters["flow_threshold"],
                cellprob_threshold=parameters["cellprob_threshold"],
                do_3D=False, augment=False, bsize=256, min_size=15,
            )
        masks = np.asarray(masks, dtype="<i4")
        if masks.shape != (height, width) or masks.min(initial=0) < 0:
            raise RuntimeError("Cellpose returned an invalid tile label image")
        masks.tofile(request["output"])
        print(json.dumps({"shape": [height, width], "labels": int(masks.max(initial=0))}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
