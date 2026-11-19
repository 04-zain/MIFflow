"""Read calibrated regions of the selected multichannel NGFF 0.5 image.

The coordinator supplies a completed generation. We read its descriptions once,
open each Zarr level when pixels are requested, and share read-only channel
decode blocks between channel and window consumers.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
from threading import Lock
from typing import Sequence

import numpy as np


class ProcessedImageError(RuntimeError):
    """A selected image has unsupported metadata or cannot supply real pixels."""


@dataclass(frozen=True)
class ImageLevel:
    """Dimensions and physical calibration in the level's own pixel frame."""

    path: str
    shape: tuple[int, int, int]
    dtype: np.dtype
    y_um_per_px: float
    x_um_per_px: float


class ProcessedImage:
    """Own one selected image reader and its bounded decoded-chunk cache.

    ``cache_bytes`` counts resident decoded pixels. Each active caller separately
    admits its requested output and transient decode buffers through the task's
    resource owner. Concurrent consumers share a chunk read; interruption of one
    consumer does not discard data required by another.
    """

    def __init__(self, path: str | Path, *, cache_bytes: int = 64 * 1024**2):
        if cache_bytes < 0:
            raise ValueError("Image cache allowance must be nonnegative")
        self.path = Path(path).resolve()
        self.cache_bytes = cache_bytes
        self._resident_bytes = 0
        self._cache: OrderedDict[tuple[object, ...], np.ndarray] = OrderedDict()
        self._pending: dict[tuple[object, ...], Future] = {}
        self._lock = Lock()
        self._closed = False
        self.decode_count = 0
        self._arrays = []

        # i) Resolve axes, component meanings and level transformations once.
        try:
            self.root_metadata_bytes = (self.path / "zarr.json").read_bytes()
            root = json.loads(self.root_metadata_bytes)
            self.attributes = root["attributes"]
            ome = root["attributes"]["ome"]
            processing = root["attributes"].get("mif_pipeline")
            if processing is not None and not processing.get("complete"):
                raise ValueError("Processed image preparation has unfinished shards")
            if root["zarr_format"] != 3 or ome["version"] != "0.5":
                raise ValueError("Expected NGFF 0.5 on Zarr 3")
            if len(ome["multiscales"]) != 1:
                raise ValueError("Select one two-dimensional image")
            multiscale = ome["multiscales"][0]
            axes = multiscale["axes"]
            if [axis["name"] for axis in axes] != ["c", "y", "x"]:
                raise ValueError("Expected the selected c,y,x image plane")
            if any(axis.get("unit") != "micrometer" for axis in axes[1:]):
                raise ValueError("Spatial axes require micrometre calibration")
            levels = []
            for dataset in multiscale["datasets"]:
                relative = Path(dataset["path"])
                target = (self.path / relative).resolve()
                if not target.is_relative_to(self.path):
                    raise ValueError("Image level leaves its generation directory")
                raw = json.loads((target / "zarr.json").read_text())
                shape = tuple(raw["shape"])
                dtype = np.dtype(raw["data_type"])
                _require_profile(raw, shape, dtype)
                scale = np.ones(3, dtype=np.float64)
                found = False
                for transform in dataset["coordinateTransformations"]:
                    if transform["type"] == "scale":
                        scale *= transform["scale"]
                        found = True
                    elif transform["type"] == "translation" and not any(transform["translation"]):
                        pass
                    else:
                        raise ValueError("Image coordinate transform requires an explicit mapped-image adapter")
                if not found or not np.isfinite(scale).all() or np.any(scale <= 0):
                    raise ValueError("Image scales must be positive and finite")
                levels.append(ImageLevel(str(relative), shape, dtype, float(scale[1]), float(scale[2])))
            if not levels:
                raise ValueError("Image contains no levels")
            if any(level.shape[0] != levels[0].shape[0] or level.dtype != levels[0].dtype for level in levels):
                raise ValueError("Image levels disagree on component count or scalar type")
            self.channels = tuple(dict(channel) for channel in ome["omero"]["channels"])
            if len(self.channels) != levels[0].shape[0]:
                raise ValueError("Channel descriptions disagree with the accepted component count")
            self.levels = tuple(levels)
            self._arrays = [None] * len(self.levels)
        except (OSError, KeyError, TypeError, ValueError) as failure:
            raise ProcessedImageError(f"Unable to open processed image {self.path}: {failure}") from failure

    @property
    def resident_bytes(self) -> int:
        """Return decoded pixels currently retained by this reader."""
        with self._lock:
            return self._resident_bytes

    def _array(self, level: int):
        """Open a level on its first pixel request and share that handle.

        Metadata-only callers need dimensions and calibration without starting
        Zarr array access. The reader lock ensures concurrent first requests
        open the selected level once; chunk decoding occurs outside this lock.
        """
        with self._lock:
            if self._closed:
                raise ProcessedImageError("Processed image reader is closed")
            array = self._arrays[level]
            if array is None:
                import zarr

                array = zarr.open_array(str(self.path / self.levels[level].path), mode="r")
                self._arrays[level] = array
            return array

    def chunk(self, level: int, chunk_y: int, chunk_x: int, *, channels: Sequence[int] | None = None) -> np.ndarray:
        """Return a read-only C,Y,X block, cropped at the logical image edges."""
        if level < 0 or level >= len(self.levels):
            raise ValueError("Requested image level is unavailable")
        description = self.levels[level]
        y, x = chunk_y * 256, chunk_x * 256
        if min(chunk_y, chunk_x) < 0 or y >= description.shape[1] or x >= description.shape[2]:
            raise ValueError("Requested image chunk is outside the selected level")
        selected = tuple(range(description.shape[0])) if channels is None else tuple(channels)
        if not selected or any(channel < 0 or channel >= description.shape[0] for channel in selected):
            raise ValueError("Requested component channels are outside the image")
        all_channels = selected == tuple(range(description.shape[0]))
        key = (level, chunk_y, chunk_x) if all_channels else (level, chunk_y, chunk_x, selected)
        with self._lock:
            if self._closed:
                raise ProcessedImageError("Processed image reader is closed")
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._pending[key] = future
        if not owner:
            return future.result()
        try:
            # ii) Share the admitted channel block with resident and concurrent borrowers.
            if "mif_pipeline" in self.attributes:
                shard_y, shard_x = chunk_y // 4, chunk_x // 4
                directory = self.path / description.path
                if not (directory / "c" / "0" / str(shard_y) / str(shard_x)).exists():
                    index = shard_y * ((description.shape[2] + 1023) // 1024) + shard_x
                    with (directory / ".stored.bin").open("rb") as presence:
                        presence.seek(index // 8)
                        stored = presence.read(1)
                    if len(stored) != 1 or stored[0] & (1 << (index % 8)):
                        raise IOError("Processed image is missing a completed non-fill shard")
            array = self._array(level)
            block = np.asarray(array[:, y : y + 256, x : x + 256] if all_channels else
                               array.oindex[list(selected), y : y + 256, x : x + 256])
            block.setflags(write=False)
            with self._lock:
                self.decode_count += 1
                if not self._closed and block.nbytes <= self.cache_bytes:
                    while self._cache and self._resident_bytes + block.nbytes > self.cache_bytes:
                        _, evicted = self._cache.popitem(last=False)
                        self._resident_bytes -= evicted.nbytes
                    self._cache[key] = block
                    self._resident_bytes += block.nbytes
            future.set_result(block)
            return block
        except Exception as failure:
            error = ProcessedImageError(f"Unable to read image chunk {key}: {failure}")
            future.set_exception(error)
            raise error from failure
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def channel_chunk(self, level: int, chunk_y: int, chunk_x: int, channel: int) -> np.ndarray:
        """Borrow one channel plane without copying its canonical decoded block."""
        if channel < 0 or channel >= len(self.channels):
            raise ValueError("Requested component channel is outside the image")
        return self.chunk(level, chunk_y, chunk_x)[channel]

    def read_window(self, level: int, *, x: int, y: int, width: int, height: int,
                    channels: Sequence[int] | None = None, max_output_bytes: int | None = None) -> np.ndarray:
        """Assemble actual pixels in level coordinates and the requested channel order.

        We retain the recorded level calibration; model sampling and halo belong
        to the model adapter. A nonaligned window borrows every intersecting
        chunk. Invalid extents and failed reads return an error before any array
        is supplied to the numerical consumer.
        """
        if level < 0 or level >= len(self.levels):
            raise ValueError("Requested image level is unavailable")
        description = self.levels[level]
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > description.shape[2] or y + height > description.shape[1]:
            raise ValueError("Requested image window is outside the selected level")
        selected = tuple(range(description.shape[0])) if channels is None else tuple(channels)
        if not selected or any(channel < 0 or channel >= description.shape[0] for channel in selected):
            raise ValueError("Requested component channels are outside the image")
        requested_bytes = len(selected) * width * height * description.dtype.itemsize
        if max_output_bytes is not None and requested_bytes > max_output_bytes:
            raise MemoryError(f"Image region requires {requested_bytes} output bytes; allowance is {max_output_bytes}")
        output = np.empty((len(selected), height, width), dtype=description.dtype)
        # iii) Copy only requested valid pixels into the admitted output array.
        for cy in range(y // 256, (y + height - 1) // 256 + 1):
            for cx in range(x // 256, (x + width - 1) // 256 + 1):
                block = self.chunk(level, cy, cx)
                y0, x0 = max(y, cy * 256), max(x, cx * 256)
                y1, x1 = min(y + height, (cy + 1) * 256), min(x + width, (cx + 1) * 256)
                for destination, channel in enumerate(selected):
                    output[destination, y0 - y : y1 - y, x0 - x : x1 - x] = block[channel, y0 - cy * 256 : y1 - cy * 256, x0 - cx * 256 : x1 - cx * 256]
        output.setflags(write=False)
        return output

    def read_display_window(self, level: int, *, x: int, y: int, width: int, height: int,
                            output_width: int, output_height: int, channels: Sequence[int],
                            max_output_bytes: int | None = None) -> np.ndarray:
        """Sample display-pixel centres from bounded chunks of a stored level.

        This is nearest-neighbour display sampling, not a model input or a new
        image level. A single-level overview still visits the needed source
        chunks; only the requested channels and bounded output are retained.
        """
        if level < 0 or level >= len(self.levels):
            raise ValueError("Requested image level is unavailable")
        description = self.levels[level]
        if (min(x,y) < 0 or min(width,height) <= 0 or x+width > description.shape[2] or y+height > description.shape[1]
                or type(output_width) is not int or type(output_height) is not int
                or not 1 <= output_width <= width or not 1 <= output_height <= height):
            raise ValueError("Requested display window is outside the image or upsamples its pixels")
        selected = tuple(channels)
        if not selected or any(channel < 0 or channel >= description.shape[0] for channel in selected):
            raise ValueError("Requested component channels are outside the image")
        requested_bytes = len(selected)*output_width*output_height*description.dtype.itemsize
        if max_output_bytes is not None and requested_bytes > max_output_bytes:
            raise MemoryError(f"Image display requires {requested_bytes} output bytes; allowance is {max_output_bytes}")
        output = np.empty((len(selected),output_height,output_width),dtype=description.dtype)
        xs = x + np.floor((np.arange(output_width)+.5)*width/output_width).astype(np.int64)
        ys = y + np.floor((np.arange(output_height)+.5)*height/output_height).astype(np.int64)
        x_chunks, y_chunks = xs//256, ys//256
        for cy in np.unique(y_chunks):
            oy = np.flatnonzero(y_chunks == cy);sy = ys[oy]-cy*256
            for cx in np.unique(x_chunks):
                ox = np.flatnonzero(x_chunks == cx);sx = xs[ox]-cx*256
                block = self.chunk(level,int(cy),int(cx),channels=selected)
                for channel in range(len(selected)):
                    output[channel][np.ix_(oy,ox)] = block[channel][np.ix_(sy,sx)]
        output.setflags(write=False)
        return output

    def close(self) -> None:
        """Release clean cache residency; outstanding immutable borrowers remain valid."""
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._resident_bytes = 0

    def __enter__(self) -> ProcessedImage:
        return self

    def __exit__(self, *unused) -> None:
        self.close()


def _require_profile(raw: dict, shape: tuple[int, ...], dtype: np.dtype) -> None:
    """Check the selected external array's decode and storage profile once."""
    if len(shape) != 3 or min(shape) < 1 or dtype.name not in {"uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64"}:
        raise ValueError("Expected a nonempty c,y,x real numeric image")
    channels = shape[0]
    if raw["chunk_grid"]["configuration"]["chunk_shape"] != [channels, 1024, 1024]:
        raise ValueError("Expected all-channel 1024-square shards")
    codecs = raw["codecs"]
    if len(codecs) != 1 or codecs[0]["name"] != "sharding_indexed":
        raise ValueError("Expected indexed sharding")
    shard = codecs[0]["configuration"]
    if shard["chunk_shape"] != [channels, 256, 256] or shard["index_location"] != "end":
        raise ValueError("Expected 256-square inner chunks and an end-of-shard index")
    index = shard["index_codecs"]
    if len(index) != 2 or index[0]["name"] != "bytes" or index[0]["configuration"]["endian"] != "little" or index[1]["name"] != "crc32c":
        raise ValueError("Expected little-endian index entries with CRC32C")
    inner = shard["codecs"]
    if len(inner) != 2 or inner[0]["name"] != "bytes" or (dtype.itemsize > 1 and inner[0].get("configuration", {}).get("endian") != "little") or inner[1]["name"] != "blosc":
        raise ValueError("Expected little-endian scalar bytes compressed by Blosc")
    config = inner[1]["configuration"]
    if (config["cname"], config["clevel"], config["shuffle"], config["typesize"]) != ("zstd", 1, "shuffle", dtype.itemsize):
        raise ValueError("Expected lossless Zstd level1 byte shuffle matching the image dtype")
    if raw["fill_value"] != 0:
        raise ValueError("The selected processed-image profile requires zero fill")


class ProcessedImageSource:
    """Expose a completed processed generation through the existing source API.

    The metadata digest identifies the immutable selected generation, including
    its unique generation ID. It is not a recursive logical pixel-content hash.
    Measurements and spectral operations request their selected plane directly
    from the shared native-shaped decode buffer. The source API returns owned,
    contiguous regional arrays, with RGB samples in their original H,W,RGB layout.
    """

    def __init__(self, path: str | Path, *, cache_bytes: int = 64 * 1024**2):
        from ome_types import from_xml
        from .backends import BackendVersions, OpenedSourceIdentity, ResolutionInfo, SeriesInfo

        self._reader = ProcessedImage(path, cache_bytes=cache_bytes)
        root = self._reader.path
        record = root / "zarr.json"
        raw = self._reader.root_metadata_bytes
        processing = self._reader.attributes.get("mif_pipeline", {})
        if processing.get("schema") != "mif-processed-image/v1" or not processing.get("complete"):
            self._reader.close()
            raise ProcessedImageError("Pipeline source access requires a completed processed image generation")
        self.metadata = from_xml(processing["ome_xml"])
        from .canonical_ome_tiff import _ome_dtype, _physical_calibration
        pixels = self.metadata.images[0].pixels
        from .writers.processed_image import _rgb_pixels
        self._rgb = _rgb_pixels(pixels)
        level0 = self._reader.levels[0]
        if len(self.metadata.images) != 1 or pixels.size_t != 1 or pixels.size_z != 1:
            raise ProcessedImageError("Processed image metadata must retain one selected two-dimensional plane")
        if level0.shape != (pixels.size_c, pixels.size_y, pixels.size_x) or level0.dtype != _ome_dtype(pixels.type):
            raise ProcessedImageError("Processed image axes, dimensions or scalar type disagree with its source metadata")
        x_um = _physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, "X")[2]
        y_um = _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, "Y")[2]
        if x_um is None or y_um is None or not np.allclose([x_um, y_um], [level0.x_um_per_px, level0.y_um_per_px], rtol=1e-10, atol=0):
            raise ProcessedImageError("Processed image physical calibration disagrees with its source metadata")
        state = record.stat()
        self.identity = OpenedSourceIdentity(root, hashlib.sha256(raw).hexdigest(), state.st_size,
            state.st_mtime_ns, state.st_dev, state.st_ino)
        self.versions = BackendVersions("zarr-python", zarr_version(), "not_used", "not_used")
        self.series = (SeriesInfo(0, self.metadata.images[0].name or root.name,
            tuple(ResolutionInfo(index, level.shape[2], level.shape[1], 1, 1 if self._rgb else level.shape[0], 1,
                3 if self._rgb else 1, level.dtype, pixels.dimension_order.value, False) for index, level in enumerate(self._reader.levels))),)

    def read_region(self, *, series, resolution, plane, region) -> np.ndarray:
        """Read an owned contiguous scalar plane or H,W,RGB sample region."""
        if series != 0 or plane.t != 0 or plane.z != 0:
            raise ValueError("Processed image source contains one selected two-dimensional plane")
        if self._rgb:
            if plane.c != 0:
                raise ValueError("Processed RGB image contains one three-sample channel")
            values = self._reader.read_window(resolution, x=region.x, y=region.y,
                width=region.width, height=region.height)
            return np.require(values.transpose(1, 2, 0), requirements=("C", "O"))
        values = self._reader.read_window(resolution, x=region.x, y=region.y,
            width=region.width, height=region.height, channels=[plane.c])[0]
        return np.require(values, requirements=("C", "O"))

    def close(self) -> None:
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def zarr_version() -> str:
    import zarr
    return zarr.__version__
