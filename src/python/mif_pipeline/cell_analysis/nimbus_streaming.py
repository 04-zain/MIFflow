"""

Applies Nimbus to bounded reads of a canonical image and finalized cell masks.

The native source owns image reading, rasterization and the association between
temporary integer mask labels and stable cell identities. Python owns one
frozen Nimbus model session, exact image-level normalization and reductions of
the upstream globally anchored predictions. Storage chunks never become model
fields, and cell scores include every native-resolution cell pixel, including
cell boundaries. No complete image or prediction plane is allocated here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
import hashlib
from importlib import metadata
import json
from itertools import groupby
from math import ceil, floor
import os
from pathlib import Path
import sys
import time
from typing import Protocol

import numpy as np

if __package__:
    from .nimbus import NimbusCellIdentity, NimbusScoreError, write_nimbus_scores
    from . import nimbus_tiling as tiling
else:
    from nimbus import NimbusCellIdentity, NimbusScoreError, write_nimbus_scores
    import nimbus_tiling as tiling


class NimbusImageSource(Protocol):
    """

    Supplies bounded level-zero pixels and finalized masks for one original image.

    Integer mask label i+1 refers to identities[i]; zero is background. These
    temporary dense indices never replace a pipeline cell ID. The source must
    retain the accepted segmentation context of all participating measured
    sections, including cells excluded only by a later fitting-region choice.
    A requested rectangle is clipped inside the image and returns its exact extent.
    Both operations run on a computation worker, never the native UI thread.
    """

    width: int
    height: int
    magnification: float
    channels: tuple[str, ...]
    identities: tuple[NimbusCellIdentity, ...]

    def read_pixels(
        self, x: int, y: int, width: int, height: int, channel_names: Sequence[str],
    ) -> np.ndarray:
        """Returns C,H,W channel values at original image resolution."""
        ...

    def read_labels(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        """Returns H,W integer cell-mask labels under the fixed identity mapping."""
        ...

    def may_have_cells(self, x: int, y: int, width: int, height: int) -> bool:
        """Conservatively checks accepted cell bounds without reading image pixels."""
        ...


# ==============================================================================
#                     Exact upstream image-level normalization
# ==============================================================================


def normalization_factors(
    source: NimbusImageSource,
    channel_names: Sequence[str],
    *,
    block_size: int | None = None,
) -> dict[str, float]:
    """

    Calculates the official positive-float32 0.999 quantile per original image.

    Each image is one Nimbus normalization field; exporting smaller storage or
    inference blocks does not change that field. Positive finite IEEE-754 words
    have the same unsigned and numerical order. Four bounded native-image passes
    select both order statistics with 256-bin radix histograms; the first pass
    also counts positive values. No pixel or scalar spill files are created.
    NumPy 1.26.4's linear interpolation, including its float32 difference, is
    preserved. All-zero channels use the upstream 1e-8 fallback. Nonfinite values
    and negative-only channels fail.
    """

    channels = tuple(channel_names)
    if not channels or len(set(channels)) != len(channels):
        raise NimbusScoreError("Nimbus normalization needs distinct selected channels")
    if block_size is None:
        # At most 16 MiB of selected float32 pixels per exchange, even when a
        # future caller groups a larger panel into one resident model session.
        block_size = min(1024, max(1, int((16 * 1024 * 1024 / (4 * len(channels))) ** 0.5)))
    if type(block_size) is not int or block_size < 1 or block_size > 1024:
        raise NimbusScoreError("Nimbus source block size must be between 1 and 1024")
    counts = [0] * len(channels)
    nonzero = [False] * len(channels)
    prefixes = [[0, 0] for channel in channels]
    remaining = [[0, 0] for channel in channels]
    fractions = [0.0] * len(channels)
    for shift in (24, 16, 8, 0):
        histograms = [{prefix: np.zeros(256, dtype=np.int64) for prefix in pair} for pair in prefixes]
        high_mask = np.uint32((0xFFFFFFFF << (shift + 8)) & 0xFFFFFFFF)
        for y in range(0, source.height, block_size):
            height = min(block_size, source.height - y)
            for x in range(0, source.width, block_size):
                width = min(block_size, source.width - x)
                pixels = np.asarray(source.read_pixels(x, y, width, height, channels), dtype=np.float32)
                if pixels.shape != (len(channels), height, width) or not np.isfinite(pixels).all():
                    raise NimbusScoreError("Nimbus source returned incomplete or nonfinite channel pixels")
                for index, values in enumerate(pixels):
                    positive = values[values > 0]
                    if shift == 24:
                        nonzero[index] |= bool(np.any(values))
                        counts[index] += positive.size
                    words = positive.view(np.uint32)
                    for prefix, histogram in histograms[index].items():
                        selected = words if not high_mask else words[(words & high_mask) == prefix]
                        histogram += np.bincount((selected >> shift) & 255, minlength=256)
        if shift == 24:
            for index, count in enumerate(counts):
                if not count and nonzero[index]:
                    raise NimbusScoreError("Nimbus normalization requires positive pixels in a nonzero image")
                rank = np.float64(max(count - 1, 0)) * np.float64(0.999)
                remaining[index] = [floor(rank), ceil(rank)]
                fractions[index] = rank - floor(rank)
            if not any(counts):
                break
        for index, count in enumerate(counts):
            if not count:
                continue
            for side, prefix in enumerate(prefixes[index]):
                cumulative = np.cumsum(histograms[index][prefix])
                bucket = int(np.searchsorted(cumulative, remaining[index][side], side="right"))
                if bucket > 255:
                    raise NimbusScoreError("Nimbus source pixels changed during normalization")
                if bucket:
                    remaining[index][side] -= int(cumulative[bucket - 1])
                prefixes[index][side] = prefix | (bucket << shift)
    result = {}
    for index, channel in enumerate(channels):
        if not counts[index]:
            result[channel] = 1e-8
            continue
        left, right = np.asarray(prefixes[index], dtype=np.uint32).view(np.float32)
        fraction = fractions[index]
        difference = np.float32(right - left)
        quantile = (np.float64(right) - np.float64(difference) * (1 - fraction)
                    if fraction >= 0.5 else
                    np.float64(left) + np.float64(difference) * fraction)
        result[channel] = float(quantile)
    return result


# ==============================================================================
#                   Reductions over original native cell pixels
# ==============================================================================


class CellScoreAccumulator:
    """

    Reduces globally placed prediction tiles directly into narrow per-cell arrays.

    Upsampling uses OpenCV 4's nearest-neighbor coordinate rule. For each model
    rectangle we read only native mask blocks whose nearest model coordinate
    falls inside that rectangle. A cell crossing tile boundaries contributes
    every native pixel exactly once per distinct augmentation. Repeated identical
    augmentations contribute their original multiplicity without rereading pixels
    or evaluating the model again. Each evaluated augmentation must
    cover the same cell-pixel counts; missing raster pixels remain NaN instead
    of marker-negative. Memory is proportional to cell count plus one mask block.
    """

    def __init__(self, cell_count: int, *, block_size: int = 256):
        if type(cell_count) is not int or cell_count < 0 or type(block_size) is not int or block_size < 1:
            raise NimbusScoreError("Nimbus reduction requires valid cell and block counts")
        self.sums = np.zeros(cell_count + 1, dtype=np.float64)
        self.counts = np.zeros(cell_count + 1, dtype=np.uint64)
        self.base_counts = None
        self.transforms = 0
        self.active = False
        self.block_size = block_size

    def begin_transform(self, *, repetitions: int = 1) -> None:
        """Starts an augmentation with its positive integer averaging weight."""
        if self.active:
            raise NimbusScoreError("Nimbus augmentation reduction is already active")
        if type(repetitions) is not int or repetitions < 1:
            raise NimbusScoreError("Nimbus augmentation repetitions must be a positive integer")
        self.counts.fill(0)
        self.repetitions = repetitions
        self.active = True

    def add(
        self, source: NimbusImageSource, prediction: np.ndarray,
        *, origin: tuple[int, int], prediction_shape: tuple[int, int],
    ) -> None:
        """

        Adds one nonoverlapping original-orientation model rectangle to cell sums.

        Prediction values already have the augmentation inverted. Rectangle
        coordinates refer to the complete model prediction extent, not its local
        tile. Integer bounds are conservatively expanded by one native pixel;
        the actual OpenCV nearest indices select the exact contributing pixels.
        """
        if not self.active:
            raise NimbusScoreError("Nimbus reduction has no active augmentation")
        prediction = np.asarray(prediction)
        if prediction.ndim != 2:
            raise NimbusScoreError("Nimbus prediction must be a two-dimensional rectangle")
        model_height, model_width = prediction_shape
        x0, y0 = origin
        x1, y1 = x0 + prediction.shape[1], y0 + prediction.shape[0]
        if x0 < 0 or y0 < 0 or x1 > model_width or y1 > model_height:
            raise NimbusScoreError("Nimbus prediction rectangle is outside its original image")
        if not np.isfinite(prediction).all() or np.any((prediction < 0) | (prediction > 1)):
            raise NimbusScoreError("Nimbus prediction contains invalid expression confidence")
        left, top, width, height = tiling.native_output_bounds(
            source, prediction_shape, (x0, y0, prediction.shape[1], prediction.shape[0]))
        right, bottom = left + width, top + height
        for y in range(top, bottom, self.block_size):
            height = min(self.block_size, bottom - y)
            ys = tiling.nearest_indices(np.arange(y, y + height), model_height, source.height)
            rows = (ys >= y0) & (ys < y1)
            if not rows.any():
                continue
            for x in range(left, right, self.block_size):
                width = min(self.block_size, right - x)
                xs = tiling.nearest_indices(np.arange(x, x + width), model_width, source.width)
                columns = (xs >= x0) & (xs < x1)
                if not columns.any():
                    continue
                labels = np.asarray(source.read_labels(x, y, width, height))
                if labels.shape != (height, width) or labels.dtype.kind not in "ui" or np.any(labels < 0) or np.any(labels >= len(self.sums)):
                    raise NimbusScoreError("Nimbus source mask differs from its declared native-cell mapping")
                labels = labels[np.ix_(rows, columns)]
                values = prediction[np.ix_(ys[rows] - y0, xs[columns] - x0)]
                foreground = labels > 0
                indices, inverse = np.unique(labels[foreground], return_inverse=True)
                # Local unique labels avoid an N-cell bincount allocation for every block.
                contributions = np.bincount(inverse, weights=values[foreground], minlength=len(indices))
                if self.repetitions != 1:
                    contributions *= self.repetitions
                self.sums[indices] += contributions
                self.counts[indices] += np.bincount(inverse, minlength=len(indices)).astype(np.uint64)

    def end_transform(self) -> None:
        """Confirms identical native-pixel coverage across completed augmentations."""
        if not self.active:
            raise NimbusScoreError("Nimbus reduction has no active augmentation")
        if self.base_counts is None:
            self.base_counts = self.counts.copy()
        elif not np.array_equal(self.base_counts, self.counts):
            raise NimbusScoreError("Nimbus augmentation cell-pixel coverage differs")
        self.transforms += self.repetitions
        self.active = False

    def finish(self) -> np.ndarray:
        """Returns mean scores, preserving cells with no raster pixels as unknown."""
        if self.active or not self.transforms:
            raise NimbusScoreError("Nimbus reduction is incomplete")
        result = np.full(len(self.sums) - 1, np.nan, dtype=np.float64)
        counts = self.base_counts[1:]
        valid = counts > 0
        result[valid] = self.sums[1:][valid] / (counts[valid] * self.transforms)
        return result


# ==============================================================================
#                    One frozen model for the selected panel
# ==============================================================================


def runtime_versions() -> dict[str, str]:
    """Load and identify the scoring libraries without allocating model weights."""
    import cv2
    import torch
    from nimbus_inference.unet import UNet, ConvBlock, UpconvBlock, Pad2D

    versions = {name: metadata.version(name) for name in ("Nimbus-Inference", "numpy", "torch")}
    versions["opencv"] = cv2.__version__
    if (versions["Nimbus-Inference"] != "0.0.5" or versions["numpy"] != "1.26.4"
            or versions["torch"].split("+")[0] != "2.7.1" or versions["opencv"] != "4.11.0"):
        raise NimbusScoreError("The bundled Nimbus numerical libraries differ from the supported runtime")
    return versions


class NimbusModelSession:
    """

    Owns one official UNet and checkpoint for an entire isolated worker session.

    Only padding modes change between upstream's small-image and tiled branches;
    learned parameters are loaded once. Batch size is one, inference is float32,
    and neither model compilation nor mixed precision changes the tested path.
    Numerical dependency versions and the selected device are retained with scores.
    The worker never acquires weights or installs dependencies implicitly.
    """

    def __init__(self, checkpoint: str | Path, *, weights_sha256: str, threads: int = 2, device: str | None = None):
        import cv2
        import torch
        from nimbus_inference.unet import UNet, ConvBlock, UpconvBlock, Pad2D

        versions = runtime_versions()
        if type(threads) is not int or not 1 <= threads <= 8:
            raise NimbusScoreError("Nimbus CPU threads must be between one and eight")
        checkpoint = Path(checkpoint).resolve(strict=True)
        digest = hashlib.sha256()
        with checkpoint.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != weights_sha256:
            raise NimbusScoreError("Nimbus checkpoint checksum differs from the frozen request")
        torch.set_num_threads(threads)
        self.torch = torch
        from mif_pipeline._devices import select_torch_device
        decision = select_torch_device("nimbus", preference=device, torch=torch,
            required_bytes=int(os.environ.get("MIF_DEVICE_MEMORY_BYTES", 2 * 1024**3)))
        self.device = torch.device(decision.device)
        self._padding_classes = (UNet, ConvBlock, UpconvBlock)
        self._pad_class = Pad2D
        self.model = UNet(num_classes=1, padding="reflect")
        self.model.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True))
        self.model.eval().to(self.device)
        self.linear_mode = "opencv4_ipp" if cv2.ipp.useIPP() else "opencv4_classic"
        self.provenance = {
            "model_id": f"Nimbus-Inference/{checkpoint.name}", "package_version": "0.0.5",
            "weights_sha256": digest.hexdigest(),
            "runtime": {**versions, "opencv": cv2.__version__, "linear_resize_backend": self.linear_mode,
                        "device": decision.device, "device_reason": decision.reason, "precision": "float32", "batch_size": 1, "threads": threads},
        }

    def predict(self, inputs: np.ndarray, *, padding: str) -> np.ndarray:
        """Predicts one bounded model window without reloading checkpoint tensors."""
        if padding not in ("reflect", "valid"):
            raise NimbusScoreError("Nimbus padding must be reflect or valid")
        inputs = np.asarray(inputs)
        if inputs.ndim != 4 or inputs.shape[:2] != (1, 2) or max(inputs.shape[-2:]) > 1024:
            raise NimbusScoreError("Nimbus model inputs must be one bounded two-channel window")
        if self.model.padding != padding:
            for module in self.model.modules():
                if isinstance(module, self._padding_classes):
                    module.padding = padding
                if isinstance(module, self._pad_class):
                    module.mode = padding
        with self.torch.inference_mode():
            prediction = self.model(self.torch.from_numpy(
                np.ascontiguousarray(inputs, dtype=np.float32)).to(self.device))
            return prediction[0, 0].cpu().numpy()


def predict_source(
    source: NimbusImageSource, channel: str, factor: float, session: NimbusModelSession,
    *, test_time_augmentation: bool = True, block_size: int = 256,
) -> np.ndarray:
    """

    Scores one channel using the complete original-image Nimbus inference grid.

    We evaluate each distinct released augmentation once and weight its per-cell
    sums by the number of times it appears in the original six-call sequence.
    In pinned 0.0.5 the four rotation lambdas all use the final loop value (270
    degrees), followed by vertical and horizontal flips. We preserve that actual
    released weighting and record both the sequence and reuse. Float64 reduction
    replaces repeated additions with integer multiplication. The
    1024-input, 832-output valid grid is globally centered, including reflected
    image edges. Tiles whose output cannot contain an accepted cell are omitted;
    retained inputs keep every surrounding cell mask and the original image
    normalization. Smaller images use the official reflect branch. Two released
    upstream shape failures are reported explicitly, never silently repaired.
    """
    shape = tiling.scaled_shape(source)
    small = max(shape) <= 1024
    if small and min(shape) < 32:
        raise NimbusScoreError("Nimbus reflect inference requires both scaled image dimensions to be at least 32")
    prediction_shape = tuple((axis // 16) * 16 for axis in shape) if small else shape
    if small and source.magnification == 10 and prediction_shape != shape:
        raise NimbusScoreError("Nimbus-Inference 0.0.5 cannot align a small 10x image whose dimensions are not multiples of 16")
    accumulator = CellScoreAccumulator(len(source.identities), block_size=block_size)
    transforms = tiling.UPSTREAM_TRANSFORMS if test_time_augmentation else (0,)
    for transform, repeated in groupby(transforms):
        accumulator.begin_transform(repetitions=sum(1 for _ in repeated))
        if small:
            height, width = tiling.transformed_shape(shape, transform)
            inputs = tiling.compose_source_tile(source, channel, factor, shape, transform,
                                                (0, 0, width, height), max_read_size=block_size,
                                                linear_mode=session.linear_mode)
            prediction = session.predict(inputs, padding="reflect")
            prediction = np.rot90(prediction, -transform) if transform < 4 else np.flip(prediction, axis=0 if transform == 4 else 1)
            accumulator.add(source, prediction, origin=(0, 0), prediction_shape=prediction_shape)
        else:
            for plan in tiling.global_tile_plan(shape, transform):
                bounds = tiling.restored_output_bounds(plan)
                if not source.may_have_cells(*tiling.native_output_bounds(source, shape, bounds)):
                    continue
                inputs = tiling.compose_source_tile(source, channel, factor, shape, transform,
                                                    plan.input_bounds, max_read_size=block_size,
                                                    linear_mode=session.linear_mode)
                prediction, bounds = tiling.restore_output(session.predict(inputs, padding="valid"), plan)
                accumulator.add(source, prediction, origin=bounds[:2], prediction_shape=shape)
        accumulator.end_transform()
    return accumulator.finish()


# ==============================================================================
#                Bounded native file bridge and worker entry point
# ==============================================================================


def _wait_for(path: Path, timeout: float) -> None:
    """Waits for an atomically published native reply within a finite allowance."""
    deadline = time.monotonic() + timeout
    while not path.is_file():
        error = path.parent / "error.json"
        if error.is_file():
            raise NimbusScoreError(f"Nimbus native source failed: {error.read_text(encoding='utf-8')[:2000]}")
        if time.monotonic() >= deadline:
            raise NimbusScoreError(f"Nimbus native source timed out waiting for {path.name}")
        time.sleep(0.02)


def _read_object(path: Path, *, payload: bytes | None = None) -> dict:
    """Reads a finite, unambiguous JSON object from an internal exchange file."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NimbusScoreError(f"Nimbus request contains duplicate key {key!r}")
            result[key] = value
        return result
    try:
        content = path.read_text(encoding="utf-8") if payload is None else payload.decode("utf-8")
        value = json.loads(content, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as exc:
        raise NimbusScoreError(f"Nimbus could not read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise NimbusScoreError(f"Nimbus {path.name} must contain an object")
    return value


class FileBridgeSource:
    """

    Reads one already-authorized native image through a single bounded exchange.

    The native owner supplies ready.json after validating canonical image and
    finalized population bindings. Each request has a monotonically increasing
    ID; response metadata and binary byte counts are checked before allocation.
    The source is opened lazily, so one native process can release its current
    hierarchy before opening the next image. No network server is created.
    """

    def __init__(self, descriptor: Mapping[str, object], *, timeout_seconds: float = 120):
        if not isinstance(descriptor.get("bridge_directory"), str):
            raise NimbusScoreError("Nimbus source requires a native bridge directory")
        self.directory = Path(descriptor["bridge_directory"]).resolve(strict=True)
        if not np.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise NimbusScoreError("Nimbus native bridge timeout must be positive and finite")
        self.timeout = float(timeout_seconds)
        _wait_for(self.directory / "ready.json", self.timeout)
        ready = _read_object(self.directory / "ready.json")
        self.width, self.height = ready.get("width"), ready.get("height")
        self.magnification = ready.get("magnification")
        self.magnification_source = ready.get("magnification_source", "image_metadata")
        if any(type(value) is not int or value < 1 for value in (self.width, self.height)):
            raise NimbusScoreError("Nimbus source dimensions must be positive integers")
        if isinstance(self.magnification, bool) or not isinstance(self.magnification, (float, int)) or not np.isfinite(self.magnification) or self.magnification <= 0:
            raise NimbusScoreError("Nimbus source requires calibrated positive magnification")
        if self.magnification_source not in ("image_metadata", "configured_acquisition"):
            raise NimbusScoreError("Nimbus source must identify the objective magnification from the image metadata or Nimbus settings")
        channels = ready.get("channels")
        if not isinstance(channels, list) or not channels or any(not isinstance(value, str) or not value for value in channels) or len(set(channels)) != len(channels):
            raise NimbusScoreError("Nimbus source channels must be distinct exact names")
        self.channels = tuple(channels)
        identities = ready.get("identities")
        if not isinstance(identities, list) or any(not isinstance(row, list) or len(row) != 5 or any(not isinstance(value, str) or not value for value in row) for row in identities):
            raise NimbusScoreError("Nimbus source identities must contain five-part native identities")
        self.identities = tuple(tuple(row) for row in identities)
        if len(set(self.identities)) != len(self.identities) or len(self.identities) > np.iinfo(np.int32).max:
            raise NimbusScoreError("Nimbus source native identities are duplicated or exceed the label index range")
        self.population_by_scope, self.image_by_scope = {}, {}
        for scope in ready.get("scopes", []):
            if not isinstance(scope, dict) or not isinstance(scope.get("identity"), list) or len(scope["identity"]) != 4 or any(not isinstance(value, str) or not value for value in scope["identity"]):
                raise NimbusScoreError("Nimbus source requires complete scope identity bindings")
            identity = tuple(scope["identity"])
            if identity in self.population_by_scope or any(not isinstance(scope.get(key), str) or not scope[key] for key in ("segmentation_population_id", "canonical_image_id")):
                raise NimbusScoreError("Nimbus source scope bindings are incomplete or duplicated")
            self.population_by_scope[identity] = scope["segmentation_population_id"]
            self.image_by_scope[identity] = scope["canonical_image_id"]
        if {row[:4] for row in self.identities} != set(self.population_by_scope):
            raise NimbusScoreError("Nimbus source cells differ from its declared scope bindings")
        self.request_id = 0
        self.closed = False

    def _exchange(self, operation: str, *, shape: tuple[int, ...] | None = None,
                  dtype: str | None = None, **fields) -> np.ndarray | dict:
        if self.closed:
            raise NimbusScoreError("Nimbus native source is already closed")
        request_path, response_path = self.directory / "request.json", self.directory / "response.json"
        if request_path.exists() or response_path.exists():
            raise NimbusScoreError("Nimbus native exchange contains an unconsumed request or reply")
        self.request_id += 1
        request = {"request_id": self.request_id, "operation": operation, **fields}
        temporary = self.directory / "request.tmp"
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(request, stream, allow_nan=False, separators=(",", ":"))
        os.replace(temporary, request_path)
        _wait_for(response_path, self.timeout)
        reply = _read_object(response_path)
        if reply.get("request_id") != self.request_id or reply.get("status") != "ok":
            raise NimbusScoreError(f"Nimbus native source rejected request {self.request_id}: {reply.get('error', 'invalid reply identity/status')}")
        result = reply
        payload = None
        if shape is not None:
            if reply.get("dtype") != dtype or reply.get("shape") != list(shape) or not isinstance(reply.get("path"), str):
                raise NimbusScoreError("Nimbus native reply has an unexpected array shape or dtype")
            payload = Path(reply["path"])
            payload = (self.directory / payload).resolve() if not payload.is_absolute() else payload.resolve()
            if payload.parent != self.directory or payload.name != "response.bin":
                raise NimbusScoreError("Nimbus native reply payload must be response.bin inside its bridge directory")
            expected_bytes = int(np.prod(shape, dtype=np.int64)) * np.dtype(dtype).itemsize
            if payload.stat().st_size != expected_bytes:
                raise NimbusScoreError("Nimbus native reply byte count differs from its declared bounded array")
            result = np.fromfile(payload, dtype=dtype).reshape(shape)
        if payload is not None:
            payload.unlink()
        response_path.unlink()
        request_path.unlink()
        return result

    def _bounds(self, x: int, y: int, width: int, height: int, *, array_read: bool = True) -> None:
        if any(type(value) is not int for value in (x, y, width, height)) or width < 1 or height < 1 or (array_read and (width > 1024 or height > 1024)) or x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise NimbusScoreError("Nimbus native read is outside its bounded source extent")

    def may_have_cells(self, x: int, y: int, width: int, height: int) -> bool:
        """Queries existing native group bounds and moved edits without cell hydration."""
        self._bounds(x, y, width, height, array_read=False)
        reply = self._exchange("has_cells", x=x, y=y, width=width, height=height)
        possible = reply.get("has_cells")
        if type(possible) is not bool:
            raise NimbusScoreError("Nimbus native occupancy reply must be a boolean")
        return possible

    def read_pixels(self, x: int, y: int, width: int, height: int, channel_names: Sequence[str]) -> np.ndarray:
        """Reads original-resolution float32 CHW pixels for exact canonical names."""
        self._bounds(x, y, width, height)
        channels = tuple(channel_names)
        if not channels or len(set(channels)) != len(channels) or any(channel not in self.channels for channel in channels):
            raise NimbusScoreError("Nimbus native pixel request has invalid channels")
        if len(channels) * height * width * 4 > 16 * 1024 * 1024:
            raise NimbusScoreError("Nimbus native pixel request exceeds the 16 MiB exchange budget")
        return self._exchange("pixels", shape=(len(channels), height, width), dtype="<f4",
                              x=x, y=y, width=width, height=height, channels=list(channels))

    def read_labels(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        """Reads temporary dense int32 labels under the ready-file identity map."""
        self._bounds(x, y, width, height)
        labels = self._exchange("labels", shape=(height, width), dtype="<i4",
                                x=x, y=y, width=width, height=height)
        if np.any(labels < 0) or np.any(labels > len(self.identities)):
            raise NimbusScoreError("Nimbus native labels differ from the declared cell-identity mapping")
        return labels

    def close(self) -> None:
        """Allows the one native owner to release this image and open the next."""
        if not self.closed:
            self._exchange("close")
            self.closed = True


def run_streaming_request(request_path: str | Path) -> tuple[Path, ...]:
    """

    Runs automatic canonical-image Nimbus in one isolated model process.

    Sources are opened sequentially and all selected markers are scored before
    releasing each image. Completed per-cell arrays are the only cross-image
    scientific data retained. Output files are published only after every source
    succeeds, with frozen model, runtime, normalization and native-scope evidence.
    """
    request_path = Path(request_path).resolve(strict=True)
    request_bytes = request_path.read_bytes()
    request = _read_object(request_path, payload=request_bytes)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    if request.get("schema_version") != "mif-nimbus-streaming/v1":
        raise NimbusScoreError("Automatic Nimbus requires schema mif-nimbus-streaming/v1")
    markers = request.get("markers")
    sources = request.get("sources")
    if not isinstance(markers, dict) or not markers or not isinstance(sources, list) or not sources:
        raise NimbusScoreError("Automatic Nimbus requires selected markers and native sources")
    outputs = {}
    for marker, specification in markers.items():
        if not isinstance(marker, str) or not marker or not isinstance(specification, dict) or any(not isinstance(specification.get(key), str) or not specification[key] for key in ("channel", "output_path")):
            raise NimbusScoreError("Automatic Nimbus marker configuration is incomplete")
        path = Path(specification["output_path"])
        path = path.resolve() if path.is_absolute() else (request_path.parent / path).resolve()
        if path.exists() or not path.parent.is_dir() or path in outputs.values():
            raise NimbusScoreError("Automatic Nimbus output paths must be distinct new files in existing directories")
        outputs[marker] = path
    if any(not isinstance(source, dict) for source in sources):
        raise NimbusScoreError("Automatic Nimbus source descriptors must be objects")
    checkpoint = Path(request.get("checkpoint", ""))
    if not checkpoint.is_absolute():
        checkpoint = request_path.parent / checkpoint
    session = NimbusModelSession(checkpoint, weights_sha256=request.get("weights_sha256"), threads=request.get("threads", 2), device=request.get("device"))
    identities, populations, images = [], {}, {}
    marker_scores = {marker: [] for marker in markers}
    source_evidence = []
    channels = tuple(dict.fromkeys(specification["channel"] for specification in markers.values()))
    for descriptor in sources:
        source = FileBridgeSource(descriptor, timeout_seconds=request.get("bridge_timeout_seconds", 120))
        try:
            if any(channel not in source.channels for channel in channels):
                raise NimbusScoreError("A selected Nimbus marker channel is absent from a canonical source image")
            if set(source.population_by_scope) & set(populations):
                raise NimbusScoreError("Automatic Nimbus sources repeat a native section population")
            factors = normalization_factors(source, channels)
            channel_scores = {channel: predict_source(source, channel, factors[channel], session) for channel in channels}
            for marker, specification in markers.items():
                marker_scores[marker].append(channel_scores[specification["channel"]])
            identities.extend(source.identities)
            populations.update(source.population_by_scope)
            images.update(source.image_by_scope)
            source_evidence.append({"scopes": [list(scope) for scope in source.population_by_scope],
                                    "width": source.width, "height": source.height,
                                    "magnification": source.magnification,
                                    "magnification_source": source.magnification_source,
                                    "normalization_factors": factors})
        finally:
            if sys.exc_info()[0] is None:
                source.close()
            else:
                # Preserve the scientific/source failure if an outstanding
                # failed bridge request prevents a graceful close exchange.
                with suppress(Exception):
                    source.close()
    provenance = {**session.provenance, "inference": {
        "adapter": "mif-nimbus-streaming/v1", "request_sha256": request_sha256,
        "runtime": session.provenance["runtime"], "test_time_augmentation": True,
        "augmentation_sequence": list(tiling.UPSTREAM_TRANSFORMS),
        "augmentation_evaluations": [
            {"transform": transform, "multiplicity": sum(1 for _ in repeated)}
            for transform, repeated in groupby(tiling.UPSTREAM_TRANSFORMS)
        ],
        "normalization": "per_original_image_positive_float32_quantile_0.999",
        "grid": "official_global_1024_input_832_output", "sources": source_evidence,
    }}
    for marker, path in outputs.items():
        write_nimbus_scores(path, marker=marker, identities=identities,
                            scores=np.concatenate(marker_scores[marker]), population_by_scope=populations,
                            image_by_scope=images, provenance=provenance)
    return tuple(outputs.values())


def main() -> None:
    """Inspect bundled libraries or score one explicit native-source request."""
    import argparse
    parser = argparse.ArgumentParser(description="Automatic bounded Nimbus inference worker")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--request")
    action.add_argument("--check-runtime", action="store_true")
    arguments = parser.parse_args()
    if arguments.check_runtime:
        result = runtime_versions()
    else:
        completed_paths = run_streaming_request(arguments.request)
        result = {"score_paths": [str(path) for path in completed_paths]}
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
