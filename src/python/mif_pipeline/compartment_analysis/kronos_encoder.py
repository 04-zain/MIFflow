"""

Loads the selected pretrained KRONOS2 encoder and calculates image-patch features.

We reconstruct the encoder from its supplied configuration, teacher-backbone
weights and marker CSV. It is placed in evaluation mode with gradient updates
disabled. Each selected patch is normalized using its recorded panel and
passed to forward_features(); x_norm_clstoken supplies the 768 values used
to represent that image patch.

The graph layer and RF/SVM are fitted later from these fixed image features.
This Python implementation prepares the frozen native deployment and supplies
its numerical reference. Routine image inference runs in the native JVM.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import numpy as np
from .kronos_panel import KronosPanel

# ==============================================================================
#                       Selected package and fixed patch encoder
# ==============================================================================


def _file_identity(path):
    """

    Reads the filesystem values used to check model files while loading the encoder.

    Args:
        path: Model-package file already recorded during package inspection.

    Returns:
        File size, modification time, metadata-change time, device ID and file ID.
    """

    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)


def _sha(path):
    """

    Calculates a package-file checksum using sequential one-megabyte reads.

    Args:
        path: Selected configuration, marker CSV, encoder code or checkpoint file.

    Returns:
        SHA-256 of its complete bytes, without loading the full checkpoint into
        memory for the checksum calculation.
    """

    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@dataclass(frozen=True, slots=True)
class KronosPackage:
    """

    Records the model files used to reconstruct one pretrained encoder.

    Package inspection calculates each required file's checksum and retains its
    filesystem values. We compare those values around model loading. Once loaded,
    encoding uses the fixed tensors and panel held in memory.

    Attributes:
        root: Absolute directory containing the selected KRONOS package.
        weights_name: Local teacher-checkpoint filename named by config.json.
        marker_csv_name: Local marker-statistics CSV named by config.json.
        configuration_json: Model-configuration text read during inspection.
        members: Ordered tuples of required filename, complete-file checksum
            and filesystem values observed while reading it.
        sha256: Checksum of the ordered filenames and their content checksums.
    """

    root: Path
    weights_name: str
    marker_csv_name: str
    configuration_json: str
    members: tuple[tuple[str, str, tuple[int, ...]], ...]
    sha256: str

    def verify_current(self):
        """

        Checks that selected model files retain their recorded filesystem values.

        Each file must remain present, regular and without a symbolic link. We
        compare size, timestamps and file identity with the original inspection.
        This check runs around model loading to detect a replacement or edit
        while the worker reads its configuration, code and weights.

        Raises:
            ValueError: A required package file is missing, linked or changed.
        """

        for name, digest, identity in self.members:
            path = self.root / name
            if path.is_symlink() or not path.is_file() or _file_identity(path) != identity:
                raise ValueError("The selected KRONOS package changed: " + name)

    @property
    def marker_csv_sha256(self):
        """

        Retrieves the inspected marker CSV's checksum for comparison with the selected panel.

        Returns:
            Saved SHA-256 associated with marker_csv_name.
        """

        return next(
            digest for name, digest, identity in self.members if name == self.marker_csv_name
        )


def inspect_kronos_resources(directory: str | Path):
    """Read model settings and check the required local files before processing.

    We parse the small configuration and check each resource's location, type and
    size. We return the package directory, checkpoint and marker filenames,
    configuration text, and ordered file identities. Readiness uses this inventory
    to report missing files and incompatible settings without reading weights or
    Python source. Full package inspection hashes the same inventory before the
    worker loads the model.
    """

    root = Path(directory)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("Select an existing absolute local KRONOS package directory")
    root = root.resolve(strict=True)
    config_path = root / "config.json"
    if not config_path.is_file():
        raise ValueError("The KRONOS package is missing config.json")
    if config_path.stat().st_size > 1024 * 1024:
        raise ValueError("KRONOS config.json exceeds the size limit")
    configuration_json = config_path.read_text(encoding="utf-8")
    config = json.loads(configuration_json)
    if (
        not isinstance(config, dict)
        or config.get("model_type") != "kronos2"
        or config.get("embed_dim") != 768
        or config.get("patch_size") != 256
    ):
        raise ValueError("This adapter requires the KRONOS2 768-feature, 256-pixel package")
    weights = config.get("weights_file", "")
    markers = config.get("marker_csv", "")
    if any(
        type(name) is not str or Path(name).name != name or not name for name in (weights, markers)
    ):
        raise ValueError("KRONOS resources must be named local files inside the selected package")
    required = (
        "config.json",
        weights,
        markers,
        "dinov2/models/inference.py",
        "dinov2/models/vision_transformer.py",
        "marker_utils.py",
    )
    names = set(required)
    names.update(str(p.relative_to(root)) for p in (root / "dinov2").rglob("*.py"))
    if len(names) > 128:
        raise ValueError("KRONOS package source inventory exceeds the reviewed bound")
    resources = []
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Missing or linked KRONOS resource: " + name)
        before = _file_identity(path)
        if before[0] <= 0 or before[0] > (1024**3 if name == weights else 8 * 1024**2):
            raise ValueError("KRONOS resource exceeds its supported size: " + name)
        resources.append((name, before))
    return root, weights, markers, configuration_json, tuple(resources)


def inspect_kronos_package(directory: str | Path) -> KronosPackage:
    """

    Checks the selected model package and records the files required to rebuild its encoder.

    We read config.json and require the supported kronos2 model type, 256-pixel
    input size and 768-feature output. Its checkpoint and marker CSV must be
    files inside the package. We also include the required model builders and
    DINOv2 Python source used by that configuration.

    Each required file is hashed through sequential reads, with its filesystem
    values checked before and after reading. The package checksum combines the
    sorted filenames and their content checksums. These selected-model files
    are part of the model description independently of the pipeline worker's
    release/protocol identifiers.

    Args:
        directory: Existing absolute local directory containing the selected package.

    Returns:
        KronosPackage with configuration text, required files and their recorded checksums.

    Raises:
        ValueError: Model settings are incompatible, required files are missing
            or linked, a resource leaves the package, or a file changes while read.
            Inspection allows at most 128 files, 1 GiB for the checkpoint and
            8 MiB for each other required file; config.json also has a 1 MiB limit.
        OSError: A required file cannot be inspected or read.
    """

    root, weights, markers, configuration_json, resources = inspect_kronos_resources(directory)
    members = []
    for name, before in resources:
        path = root / name
        digest = _sha(path)
        if _file_identity(path) != before:
            raise ValueError("KRONOS resource changed while being inspected: " + name)
        members.append((name, digest, before))
    material = json.dumps(
        [(name, digest) for name, digest, identity in members], separators=(",", ":")
    )
    return KronosPackage(
        root,
        weights,
        markers,
        configuration_json,
        tuple(members),
        hashlib.sha256(material.encode()).hexdigest(),
    )


def estimate_activation_bytes(marker_count: int, patch_size: int) -> int:
    """

    Estimates temporary float32 encoder tensors for one patch at the selected marker count.

    For C channels and input side P, the supported attention estimate uses
    (P/16)^2 spatial tokens per channel and 17 additional class/register tokens:

        T = (P/16)^2*C + 17
        bytes = 4 * (3*12*T*T + 12*T*768)

    The first term allows multiple attention matrices whose entries compare
    token pairs; it grows quadratically with the marker count. The second term
    represents token-feature arrays. Four bytes per value converts the estimated
    number of float32 elements to bytes. This is the preparation estimate used
    for device/batch planning, with model weights and other process memory
    considered separately.

    Args:
        marker_count: Number of selected channels, an integer from one to 32.
        patch_size: Saved square image side: 32, 64, 128 or 256.

    Returns:
        Estimated activation bytes for one image patch.

    Raises:
        ValueError: The marker count is outside the supported integer interval.
    """

    if type(marker_count) is not int or not 1 <= marker_count <= 32:
        raise ValueError("Marker count must be an integer between one and 32")
    if type(patch_size) is not int or patch_size not in (32, 64, 128, 256):
        raise ValueError("KRONOS image size must be an integer: 32, 64, 128 or 256")
    tokens = marker_count * (patch_size // 16) ** 2 + 17
    return 4 * (3 * 12 * tokens * tokens + 12 * tokens * 768)


def validate_activation_budget(marker_count: int, patch_size: int, budget_bytes: int) -> int:
    """

    Checks whether one patch's estimated encoder tensors fit the selected activation allowance.

    Args:
        marker_count: Number of selected model-marker channels.
        patch_size: Saved square image side in samples.
        budget_bytes: Positive memory allowance in bytes for these temporary tensors.

    Returns:
        Calculated one-patch estimate when it does not exceed the allowance.

    Raises:
        ValueError: The allowance is invalid or the selected panel exceeds it.
    """

    if type(budget_bytes) is not int or budget_bytes <= 0:
        raise ValueError("The activation budget must be a positive integer")
    estimate = estimate_activation_bytes(marker_count, patch_size)
    if estimate > budget_bytes:
        raise ValueError(
            "The selected marker panel exceeds the encoder activation budget; "
            "reduce the selected channels/image size or configure a larger measured worker budget"
        )
    return estimate


class FrozenKronosEncoder:
    """

    Retains the pretrained encoder while extracting successive image-patch features.

    We inspect the selected package/panel, prepare the local reference
    encoder once, and retain it until close(). The model is used with fixed
    weights; the later graph/classifier stages learn from the current analysis's
    reference annotations.
    """

    def __init__(
        self,
        package: KronosPackage,
        panel: KronosPanel,
        *,
        activation_budget_bytes: int = 512 * 1024**2,
        preparation_directory: Path | None = None,
    ):
        """

        Loads the selected pretrained backbone and establishes its marker preparation and processor.

        We check the panel's marker CSV against the package and require its one-patch
        activation estimate to fit the selected allowance.

        For local encoding, we select a usable processor and load the model builder
        from the inspected package. The teacher checkpoint is read as tensor weights,
        with its known training-head entries separated from backbone entries. The
        backbone keys must match the constructed model exactly before it is moved
        to the selected processor and placed in evaluation mode with gradients disabled.

        Args:
            package: Inspected KRONOS2 package with its configuration, marker CSV and weights.
            panel: Ordered image markers, intensity divisors and model normalization values.
            preparation_directory: Owned worker directory for additional-marker
                CSV/text preparation; unnecessary for an existing-only panel.
            activation_budget_bytes: Allowance for one patch's estimated encoder
                activations, initially 512 MiB. Local device selection also allows
                an additional 2 GiB for the model and other required tensors.

        Raises:
            TypeError: Package or panel has an incompatible type.
            ValueError: Package/panel identities, memory estimate or checkpoint contents
                fail their checks.
            RuntimeError: Another DINOv2 package is already imported or the selected
                builder resolves outside the inspected package.
        """

        if type(package) is not KronosPackage or type(panel) is not KronosPanel:
            raise TypeError("KRONOS encoding requires an inspected package and an explicit panel")
        self.activation_estimate_bytes = validate_activation_budget(
            len(panel.marker_names), panel.patch_size, activation_budget_bytes
        )
        package.verify_current()
        if panel.marker_csv_sha256 != package.marker_csv_sha256:
            raise ValueError("The marker panel does not belong to the selected model vocabulary")
        self.package, self.panel = package, panel
        self._closed = False
        self._model = None
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["XFORMERS_DISABLED"] = "1"
        import torch
        from omegaconf import OmegaConf

        self._torch = torch
        from mif_pipeline._devices import select_torch_device

        self.device_decision = select_torch_device(
            "kronos", torch=torch, required_bytes=self.activation_estimate_bytes + 2 * 1024**3
        )
        self.device = self.device_decision.device
        print(
            "KRONOS execution device: " + self.device + "; " + self.device_decision.reason,
            file=sys.stderr,
            flush=True,
        )
        existing = sys.modules.get("dinov2")
        if existing is not None and not Path(existing.__file__).resolve().is_relative_to(
            package.root
        ):
            raise RuntimeError(
                "Another DINOv2 package is already loaded; use a fresh KRONOS worker"
            )
        if str(package.root) not in sys.path:
            sys.path.insert(0, str(package.root))
        inference = importlib.import_module("dinov2.models.inference")
        if not Path(inference.__file__).resolve().is_relative_to(package.root):
            raise RuntimeError("KRONOS model builder did not resolve to the selected package")
        config = json.loads(package.configuration_json)
        cfg = OmegaConf.create(config["backbone_cfg"])
        cfg.marker.marker_csv_path = str(package.root / package.marker_csv_name)
        cfg.marker.text_encoder_cache = str(package.root)
        model = inference.build_model(cfg)
        checkpoint = torch.load(
            package.root / package.weights_name, map_location="cpu", weights_only=True, mmap=True
        )
        if type(checkpoint) is not dict or set(checkpoint) != {"teacher"}:
            raise ValueError("The supplied checkpoint is not the supported teacher state")
        state = checkpoint["teacher"]
        if not isinstance(state, dict) or any(not torch.is_tensor(v) for v in state.values()):
            raise ValueError("The teacher checkpoint must contain tensor weights only")
        ignored = {
            "dino_head.mlp.0.weight",
            "dino_head.mlp.0.bias",
            "dino_head.mlp.2.weight",
            "dino_head.mlp.2.bias",
            "dino_head.mlp.4.weight",
            "dino_head.mlp.4.bias",
            "dino_head.last_layer.weight_g",
            "dino_head.last_layer.weight_v",
        }
        if set(k for k in state if not k.startswith("backbone.")) != ignored:
            raise ValueError("The checkpoint contains unsupported non-backbone parameters")
        backbone = {
            k.removeprefix("backbone."): v for k, v in state.items() if k.startswith("backbone.")
        }
        model.load_state_dict(backbone, strict=True, assign=True)
        model = model.to(self.device)
        # KRONOS supplies fixed image features. Only the later graph and native
        # classifier stages learn from this analysis's reference annotations.
        model.eval()
        model.requires_grad_(False)
        if panel.additional_markers:
            from .kronos_marker_preparation import prepare_additional_markers

            prepare_additional_markers(model, panel.additional_markers, panel.marker_text_model,
                                       preparation_directory, base_csv=package.root / package.marker_csv_name)
        package.verify_current()
        self._model = model

    def encode(self, patches):
        """

        Calculates one fixed 768-feature vector for each selected image patch.

        Local encoding applies the recorded panel normalization and evaluates one
        patch at a time under torch.inference_mode(). We read x_norm_clstoken from
        the encoder's forward_features() result, require shape (1, 768) and finite
        values, then copy that vector to CPU memory.

        Args:
            patches: Real finite array with shape (N, channels, patch_size, patch_size), with one
                to sixteen patches and the panel's selected channel order.

        Returns:
            Float32 matrix of shape (N, 768) containing the encoder features.

        Raises:
            RuntimeError: The encoder has already been closed.
            ValueError: Input dimensions/range are invalid, or a model result
                has the wrong shape or nonfinite values.
        """

        if self._closed:
            raise RuntimeError("KRONOS encoder is closed")
        values = self.panel.normalize(patches)
        # Evaluate patches individually so attention memory depends on one
        # marker panel rather than the complete requested batch.
        output = []
        with self._torch.inference_mode():
            for patch in values:
                tensor = self._torch.from_numpy(patch[None]).to(self.device)
                result = self._model.forward_features(
                    tensor, marker_names=[list(self.panel.model_marker_names)]
                )["x_norm_clstoken"]
                if tuple(result.shape) != (1, 768) or not bool(result.isfinite().all()):
                    raise ValueError("KRONOS returned invalid feature vectors")
                output.append(result.cpu().numpy().copy())
        return np.concatenate(output, axis=0)

    def close(self):
        """

        Releases the preparation encoder's local model reference.

        We mark the instance closed so another encode() call cannot use its released
        model. A new request needing encoding must construct a new checked instance.
        """

        self._closed = True
        self._model = None

    def __enter__(self):
        """

        Enters a with block using the initialized encoder while it remains open.

        Returns:
            This encoder for repeated patch requests within the block.

        Raises:
            RuntimeError: The encoder was already closed.
        """

        if self._closed:
            raise RuntimeError("KRONOS encoder is closed")
        return self

    def __exit__(self, *_args):
        """

        Releases the encoder when its with block finishes or raises an exception.

        Args:
            _args: Exception type, value and traceback supplied by Python's context manager.
        """

        self.close()
