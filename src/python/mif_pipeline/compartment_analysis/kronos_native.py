"""Prepare a panel-specific frozen encoder once for native Java inference.

Python owns model preparation. The exported graph retains the accepted marker
order, normalization and class-token output; Java supplies raw masked patches.
No labels or fitted head parameters enter this encoder identity.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import MethodType

import numpy as np

from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace


def export_native_encoder(encoder, destination: str | Path) -> dict:
    """Freeze an opened local encoder for one fixed C×P×P panel at its saved image size.

    We trace the accepted normalization and marker lookup around the frozen
    backbone, preserving its 768-value class-token output. The resulting model
    is compared with the opened encoder before publication. We return the
    completed deployment description, including the saved model digest.

    The destination belongs to this model and panel. A complete matching export
    is reused; a different selection at that location requires correction.
    Temporary outputs are removed if preparation or publication fails.
    """
    import torch

    # i) Establish the selected local model, panel and publication destination.
    if encoder._model is None:
        raise ValueError("Native model preparation must run beside the selected local model package")
    destination = Path(destination)
    if not destination.is_absolute():
        raise ValueError("Native encoder destination must be an absolute owned directory")
    descriptor = destination / "deployment.json"
    if descriptor.is_file():
        existing = json.loads(descriptor.read_bytes())
        if (existing["package_sha256"] != encoder.package.sha256
                or existing["panel_sha256"] != encoder.panel.sha256):
            raise ValueError("Native encoder destination belongs to another model or panel")
        return existing
    panel = encoder.panel
    channels = len(panel.marker_names)
    encoder.package.verify_current()

    # ii) Freeze the panel calculation with its original arithmetic and output.
    class FrozenPanel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = encoder._model
            for name, values in (("divisor", panel.intensity_divisors),
                                 ("mean", panel.means), ("std", panel.standard_deviations)):
                self.register_buffer(name, torch.tensor(values, dtype=torch.float32,
                    device=encoder.device).view(1, -1, 1, 1))

        def forward(self, raw):
            normalized = (raw / self.divisor - self.mean) / self.std
            return self.backbone.forward_features(normalized,
                marker_names=[list(panel.model_marker_names)])["x_norm_clstoken"]

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".encoder-", dir=destination.parent))
    try:
        # One legal input traces the fixed numerical operation. Public-image
        # equivalence is exercised separately through the actual native runtime.
        raw = np.zeros((1, channels, panel.patch_size, panel.patch_size), dtype=np.float32)
        with torch.inference_mode():
            sample = torch.from_numpy(raw).to(encoder.device)
            # The official positional method adds its offset to the image side.
            # During tracing a shape-derived scalar becomes float32 before that
            # addition. Supply this fixed export's Python integers so its scale
            # matches ordinary execution; keep the official interpolation itself.
            backbone = encoder._model
            original_positions = backbone.interpolate_pos_encoding

            def fixed_positions(model, tokens, width, height):
                return original_positions(tokens, panel.patch_size, panel.patch_size)

            backbone.interpolate_pos_encoding = MethodType(fixed_positions, backbone)
            try:
                traced = torch.jit.trace(FrozenPanel().eval(), sample, check_trace=False)
            finally:
                del backbone.interpolate_pos_encoding
            frozen = torch.jit.freeze(traced)
            expected = encoder.encode(raw)
            observed = frozen(sample).cpu().numpy()
            np.testing.assert_allclose(observed, expected, rtol=1e-5, atol=1e-6)
            model_path = staging / "encoder.pt"
            frozen.save(str(model_path))
        # iii) Publish the model and its descriptor as one completed directory.
        # Hash the completed staging file and flush it through a writable handle.
        with model_path.open("r+b") as model_file:
            digest = hashlib.file_digest(model_file, "sha256").hexdigest()
            os.fsync(model_file.fileno())
        record = {
            "schema": "mif-native-kronos-encoder/v1",
            "package_sha256": encoder.package.sha256,
            "panel_sha256": panel.sha256,
            "model_marker_names": list(panel.model_marker_names),
            "channels": channels, "patch_size": panel.patch_size, "feature_width": 768,
            "model_file": "encoder.pt", "model_sha256": digest,
            "model_bytes": model_path.stat().st_size,
            "torch_version": torch.__version__, "panel": panel.to_mapping(),
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        with (staging / "deployment.json").open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        publish_no_replace(staging, destination, "win-x86_64" if os.name == "nt" else "linux-x86_64")
        if os.name != "nt":
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return record
    finally:
        if staging.exists():
            shutil.rmtree(staging)
