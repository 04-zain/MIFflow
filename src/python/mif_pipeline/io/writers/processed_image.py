"""Complete standard sharded image levels and their NGFF metadata in one generation."""

from __future__ import annotations

import math
import json
import os
from pathlib import Path
import uuid
from typing import Sequence

import numpy as np
from ome_types import OME, to_xml
from ome_types.model import Pixels
import zarr
from ..channel_colors import channel_color


def create_image_group(path: str | Path, *, request_sha256: str | None = None):
    """Open an owned incomplete v3 generation with one nested codec thread.

    The surrounding operation reserves its source, solver, shard and codec
    buffers. The standard LocalStore writes each key through a temporary sibling
    and atomic replacement. Completed shard writes are synchronized below before
    their completion bit is advanced.
    """
    if int(zarr.__version__.split(".")[0]) != 3:
        raise RuntimeError("Processed image writing requires the pinned Zarr-Python 3 installation")
    from numcodecs import blosc
    blosc.set_nthreads(1)
    path = Path(path)
    if request_sha256 is not None and (path / "zarr.json").exists():
        group = zarr.open_group(str(path), mode="a", zarr_format=3)
        if group.attrs.get("processing_request_sha256") != request_sha256:
            raise ValueError("Incomplete processed image belongs to different source or numerical settings")
        return group
    group = zarr.open_group(str(path), mode="w", zarr_format=3)
    if request_sha256 is not None:
        group.attrs["processing_request_sha256"] = request_sha256
    return group


def create_image_level(group, level: int, shape: tuple[int, int, int], dtype: np.dtype):
    """Create all-channel 256-square chunks inside 1024-square indexed shards."""
    channels, height, width = shape
    dtype = np.dtype(dtype)
    if min(shape) < 1 or dtype.name not in {"uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64"}:
        raise ValueError("Processed image requires nonempty real numeric pixels")
    if str(level) in group:
        array = group[str(level)]
        if array.shape != shape or array.dtype != dtype or array.chunks != (channels, 256, 256) or array.shards != (channels, 1024, 1024):
            raise ValueError("Incomplete image level does not match the requested image profile")
        return array
    array = group.create_array(
        str(level), shape=shape, dtype=dtype,
        chunks=(channels, 256, 256), shards=(channels, 1024, 1024),
        compressors=[zarr.codecs.BloscCodec(cname="zstd", clevel=1, shuffle="shuffle", typesize=dtype.itemsize)],
        dimension_names=("c", "y", "x"),
    )
    required = math.ceil(height / 1024) * math.ceil(width / 1024)
    array.attrs["mif_pipeline"] = {"required_shards": required, "completion_file": ".completed.bin"}
    completion = Path(array.store.root) / array.path / ".completed.bin"
    with completion.open("xb") as output:
        output.write(bytes(math.ceil(required / 8)))
        output.flush()
        os.fsync(output.fileno())
    with (completion.parent / ".stored.bin").open("xb") as output:
        output.write(bytes(math.ceil(required / 8)))
        output.flush()
        os.fsync(output.fileno())
    return array


def write_image_shard(array, block: np.ndarray, *, x: int, y: int, summary: dict | None = None) -> None:
    """Publish one complete shard and mark its valid or fill-only contribution.

    We supply all component channels over the complete valid 1024-square
    footprint. The standard sharding codec handles out-of-image fill padding.
    A one-byte completion update follows the completed, synchronized shard;
    a failed write leaves its bit unset and the previous selected image intact.
    """
    channels, height, width = array.shape
    expected = channels, min(1024, height - y), min(1024, width - x)
    if min(x, y) < 0 or x % 1024 or y % 1024 or min(expected) <= 0 or block.shape != expected or block.dtype != array.dtype:
        raise ValueError("A writer must supply the complete valid all-channel shard footprint")
    shard_x, shard_y = x // 1024, y // 1024
    index = shard_y * math.ceil(width / 1024) + shard_x
    root = Path(array.store.root) / array.path
    completion = root / ".completed.bin"
    with completion.open("r+b") as finished:
        finished.seek(index // 8)
        old = finished.read(1)
        if len(old) != 1:
            raise IOError("Image completion map is truncated")
        bit = 1 << (index % 8)
        if old[0] & bit:
            raise ValueError("The image shard already has a completed writer")
        stored = root / "c" / "0" / str(shard_y) / str(shard_x)
        # Recovery retries only this owned unfinished unit, including a truncated old attempt.
        stored.unlink(missing_ok=True)
        array[:, y : y + expected[1], x : x + expected[2]] = block
        if stored.exists():
            _synchronize(stored)
            _synchronize_directory(stored.parent)
            with (root / ".stored.bin").open("r+b") as presence:
                presence.seek(index // 8)
                previous = presence.read(1)
                presence.seek(index // 8)
                presence.write(bytes([previous[0] | bit]))
                presence.flush()
                os.fsync(presence.fileno())
        if summary is not None:
            record = shard_summary_path(array, x=x, y=y)
            record.parent.mkdir(parents=True, exist_ok=True)
            temporary = record.with_suffix(".partial")
            with temporary.open("w") as output:
                json.dump(summary, output, separators=(",", ":"), allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, record)
            _synchronize_directory(record.parent)
        finished.seek(index // 8)
        finished.write(bytes([old[0] | bit]))
        finished.flush()
        os.fsync(finished.fileno())


def completed_shard_bitmap(array) -> bytes:
    """Read the compact completion map only when continuing owned unfinished work."""
    return (Path(array.store.root) / array.path / ".completed.bin").read_bytes()


def shard_summary_path(array, *, x: int, y: int) -> Path:
    return Path(array.store.root) / array.path / ".summaries" / f"{y // 1024}-{x // 1024}.json"


def finish_processed_image(path: str | Path, ome: OME, *, level_downsamples: Sequence[float] | None = None) -> dict:
    """Attach calibrated NGFF and source meanings after all shards are complete.

    The OME model retains the pipeline's component identities, biomarker
    assignments, scientific annotations and processing provenance. Standard
    NGFF axes, channel labels, display colours and level scales allow the same-JVM image reader
    to open it directly. Completion reads one compact bitmap per level, without
    rereading the image payload. The publisher subsequently selects this
    immutable directory through its existing result reference.
    """
    from ..canonical_ome_tiff import _physical_calibration
    # Calibration conversion follows the same OME unit owner as source admission.
    path = Path(path)
    group = zarr.open_group(str(path), mode="r+")
    pixels = ome.images[0].pixels
    x_um = _physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, "X")[2]
    y_um = _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, "Y")[2]
    if x_um is None or y_um is None:
        raise ValueError("Processed image requires accepted physical X/Y calibration")
    levels = sorted(int(name) for name in group.array_keys())
    if levels != list(range(len(levels))) or not levels:
        raise ValueError("Processed image levels must be consecutive from full resolution")
    if level_downsamples is None:
        level_downsamples = [2.0**level for level in levels]
    if len(level_downsamples) != len(levels):
        raise ValueError("Processed image scale list does not match its levels")
    completed = []
    datasets = []
    for level, downsample in zip(levels, level_downsamples):
        array = group[str(level)]
        if array.shape[0] != pixels.size_c:
            raise ValueError("Processed image component count disagrees with its metadata")
        count = math.ceil(array.shape[1] / 1024) * math.ceil(array.shape[2] / 1024)
        bitmap = (path / str(level) / ".completed.bin").read_bytes()
        if len(bitmap) != math.ceil(count / 8) or any(not (bitmap[i // 8] & (1 << (i % 8))) for i in range(count)):
            raise ValueError(f"Processed image level {level} contains unfinished shards")
        if not math.isfinite(downsample) or downsample <= 0:
            raise ValueError("Processed image downsample must be positive and finite")
        completed.append(count)
        datasets.append({"path": str(level), "coordinateTransformations": [{"type": "scale", "scale": [1.0, y_um * downsample, x_um * downsample]}]})
    descriptions = []
    if _rgb_pixels(pixels):
        # The array axis stores RGB samples of one original OME channel.
        for sample, (label, color) in enumerate(zip(
                ("Red", "Green", "Blue"), ("FF0000", "00FF00", "0000FF"))):
            descriptions.append({"label": label, "color": color,
                "source_channel_id": pixels.channels[0].id, "sample_index": sample})
    else:
        for index, channel in enumerate(pixels.channels):
            # NGFF stores RGB; the OME colour API separates its optional alpha value.
            # ome-types exposes white even when the source omitted Color.
            missing_color = "color" not in channel.model_fields_set or channel.color is None
            color = channel_color({}, index)[1:].upper() if missing_color else "".join(
                f"{value:02X}" for value in channel.color.as_rgb_tuple(alpha=False)
            )
            descriptions.append({"label": channel.name or channel.id, "color": color, "component_id": channel.id})
    metadata = {
        "ome": {"version": "0.5", "multiscales": [{"name": ome.images[0].name or path.name,
            "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"}, {"name": "x", "type": "space", "unit": "micrometer"}], "datasets": datasets}],
            "omero": {"channels": descriptions}},
        "mif_pipeline": {"schema": "mif-processed-image/v1", "generation_id": uuid.uuid4().hex,
            "image_id": ome.images[0].id, "complete": True, "ome_xml": to_xml(ome), "completed_shards": completed},
    }
    group.attrs.update(metadata)
    _synchronize(path / "zarr.json")
    _synchronize_directory(path)
    return metadata


def _synchronize(path: Path) -> None:
    """Flush a newly written output file before marking its data complete.

    Windows requires write access when flushing the file's operating-system
    buffers. Reopening with r+b preserves all bytes and works on both platforms.
    """
    with path.open("r+b") as completed:
        os.fsync(completed.fileno())


def _synchronize_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def copy_source_pyramid(source, ome: OME, path: str | Path, *, series: int = 0,
        request_sha256: str | None = None, block_size: tuple[int, int] = (512, 512),
        progress=None) -> tuple[int, int, np.dtype]:
    """Copy exact scalar planes or RGB samples with one all-channel shard in memory.

    Completed shards are skipped when reopening the same interrupted request.
    Every source region is bounded by 512 square pixels. No full-level image
    allocation or TIFF encode/decode occurs at this boundary. RGB regions are
    read once and their three sample planes are copied into the shard together.
    """
    from ..backends import PlaneIndex, Region
    from ..canonical_ome_tiff import _ome_dtype
    descriptors = source.series[series].resolutions
    pixels = ome.images[0].pixels
    rgb = _rgb_pixels(pixels)
    dtype = _ome_dtype(pixels.type)
    if not descriptors or (descriptors[0].size_x, descriptors[0].size_y) != (pixels.size_x, pixels.size_y):
        raise ValueError("Source native level geometry disagrees with reviewed OME")
    group = create_image_group(path, request_sha256=request_sha256)
    count = 0
    try:
        for index, descriptor in enumerate(descriptors):
            if descriptor.index != index or descriptor.size_c != (1 if rgb else pixels.size_c) or descriptor.size_z != 1 or descriptor.size_t != 1 or descriptor.rgb_samples != (3 if rgb else 1) or np.dtype(descriptor.dtype) != dtype:
                raise ValueError("Source level scalar/channel topology disagrees with reviewed OME")
            array = create_image_level(group,index,(pixels.size_c,descriptor.size_y,descriptor.size_x),dtype)
            completed = completed_shard_bitmap(array)
            columns = math.ceil(descriptor.size_x/1024)
            for y in range(0,descriptor.size_y,1024):
                for x in range(0,descriptor.size_x,1024):
                    unit = y//1024*columns+x//1024
                    count += 1
                    if completed[unit//8] & (1 << (unit%8)):
                        continue
                    height,width = min(1024,descriptor.size_y-y),min(1024,descriptor.size_x-x)
                    shard = np.empty((pixels.size_c,height,width),dtype=dtype)
                    for by in range(0,height,min(512,block_size[0])):
                        for bx in range(0,width,min(512,block_size[1])):
                            region = Region(x+bx,y+by,min(512,block_size[1],width-bx),min(512,block_size[0],height-by))
                            if rgb:
                                plane = np.asarray(source.read_region(series=series,resolution=index,plane=PlaneIndex(c=0),region=region))
                                if plane.shape != (region.height,region.width,3) or plane.dtype != dtype:
                                    raise ValueError("Source read returned an unexpected RGB scalar type or region shape")
                                shard[:,by:by+region.height,bx:bx+region.width] = plane.transpose(2,0,1)
                                continue
                            for c in range(pixels.size_c):
                                plane = np.asarray(source.read_region(series=series,resolution=index,plane=PlaneIndex(c=c),region=region))
                                if plane.shape != (region.height,region.width) or plane.dtype != dtype:
                                    raise ValueError("Source read returned an unexpected scalar type or region shape")
                                shard[c,by:by+region.height,bx:bx+region.width] = plane
                    write_image_shard(array,shard,x=x,y=y)
                    if progress is not None:
                        progress(index,x,y,count)
    finally:
        group.store.close()
    return count,len(descriptors),dtype


def _rgb_pixels(pixels: Pixels) -> bool:
    """Identify the original OME sample organization before copying its pixels.

    We preserve one uint8 RGB channel as three stored sample planes. Scalar
    channels retain their existing identities and their independent planes.
    """
    samples = tuple(int(channel.samples_per_pixel or 1) for channel in pixels.channels)
    if samples == (3,) and pixels.size_c == 3 and pixels.type.value == "uint8":
        return True
    if samples != (1,) * pixels.size_c:
        raise ValueError("Processed images require scalar channels or one uint8 RGB channel")
    return False
