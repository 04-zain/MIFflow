"""Plan one direct sharded generation and bounded numerical work before processing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True, slots=True)
class CapacityEstimate:
    """Storage estimate for new generations and the requested active array budget.

    Output includes padded decode chunks, conservative codec/index overhead and
    metadata. Temporary storage is the largest single generation being prepared;
    atomic selection moves it without making a second full image copy. Existing
    accepted generations already occupy space and are counted by filesystem free
    space. Memory is planning demand, not a process-residency measurement.
    """
    image_count: int
    output_upper_bound_bytes: int
    temporary_upper_bound_bytes: int
    working_memory_bytes: int


def estimate_capacity(images, *, output_bytes=4, workers=1, working_array_budget_bytes=2 * 1024**3):
    """Estimate the actual pyramid stopping at a longest dimension of 512.

    Reserve whole inner chunks at odd edges, Blosc overhead, shard indexes and
    level metadata. Compression generally makes the resulting files smaller.
    Source input and completed output do not require another TIFF staging copy.
    """
    images = tuple(images)
    if not images or type(output_bytes) is not int or output_bytes not in (1, 2, 4, 8):
        raise ValueError("Capacity requires image dimensions and a supported scalar width")
    if (type(workers) is not int or not 1 <= workers <= 64
            or type(working_array_budget_bytes) is not int or working_array_budget_bytes <= 0):
        raise ValueError("Capacity requires positive bounded worker settings")
    outputs = []
    for item in images:
        if len(item) != 3 or any(type(v) is not int or v <= 0 for v in item):
            raise ValueError("Image dimensions and channel counts must be positive integers")
        x, y, c = item
        total = 64 * 1024  # root metadata and completion record
        while True:
            chunks = ((x + 255) // 256) * ((y + 255) // 256)
            shards = ((x + 1023) // 1024) * ((y + 1023) // 1024)
            total += chunks * (c * 256 * 256 * output_bytes + 32)
            total += shards * (16 * 16 + 4) + 64 * 1024
            if max(x, y) <= 512:
                break
            x, y = (x + 1) // 2, (y + 1) // 2
        outputs.append(total)
    return CapacityEstimate(len(images), sum(outputs), max(outputs), workers * working_array_budget_bytes)


def available_memory_bytes():
    """Read currently available system memory without counting residency twice."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except ImportError:
        if hasattr(os, "sysconf"):
            try:
                return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
            except (OSError, ValueError):
                pass
    return None


def require_processing_space(folder, image, *, output_bytes=4):
    """Require room for one completed new generation on its destination volume."""
    import shutil
    folder = Path(folder)
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    estimate = estimate_capacity((tuple(image),), output_bytes=output_bytes)
    required = estimate.temporary_upper_bound_bytes
    free = shutil.disk_usage(folder).free
    if free < required:
        raise ValueError(
            f"Insufficient free storage: reserve {required/1024**3:.1f} GiB for the new image generation; "
            f"{free/1024**3:.1f} GiB is available. Choose another output volume or the configured cluster."
        )
    return estimate
