"""Calculate bounded spectral blocks and complete the selected sharded image directly.

Each worker owns one all-channel 1024-square output shard. A shared source lock
admits at-most-512-square measurement blocks through the selected vendor reader.
The prepared solver retains its band order, intensity correction, precision,
reporting weights and numerical acceptance. Independently completed shards are
published as they finish within the two-unit and numerical-memory allowances.

Reduced levels average completed component intensities with actual odd-edge
pixel counts. The surrounding workflow attaches scientific metadata and selects
the finished immutable NGFF 0.5/Zarr 3 generation without another pixel copy.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline._defaults import default_section

_STARTING_VALUES = default_section("unmixing.execution")

from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
import os
import json
from pathlib import Path
from typing import Any, Protocol
from threading import Lock

import numpy as np
from numpy.typing import NDArray
from threadpoolctl import threadpool_info
import zarr

from mif_pipeline.io import PlaneIndex, Region
from mif_pipeline.pipeline_core.execution_resources import current_operation_budget
from mif_pipeline.io.writers.processed_image import create_image_group, create_image_level, write_image_shard, completed_shard_bitmap, shard_summary_path

from .normalization import NormalizationFactors, normalize_region
from .application import ResolvedBasisApplication, ResolvedBasisTargetBinding
from .solvers import SolverDiagnostics, SolverResult
from .validation import ValidatedUnmixingInput

# ==============================================================================
#                    Regional interfaces and progress records
# ==============================================================================


class RegionSource(Protocol):
    """

    Defines the channel-region read used by the unmixing engine.

    The supplied reader opens the image and implements read_region(); the engine
    requests rectangles from its selected full-resolution series.
    """

    def read_region(
        self,
        *,
        series: int,
        resolution: int,
        plane: PlaneIndex,
        region: Region,
    ) -> NDArray[np.generic]:
        """

        Reads one channel within the requested image rectangle.

        Args:
            series: Selected image-series position.
            resolution: Stored-resolution number; the numerical engine requests level 0.
            plane: Channel and applicable Z/time positions.
            region: Rectangle origin, width and height in source pixels.

        Returns:
            A two-dimensional pixel array with shape (region.height, region.width).
        """

        ...


class RegionSolver(Protocol):
    """

    Defines the spectral solve used by the regional processing workers.

    One prepared instance can receive calls from multiple worker threads. Its implementation
    must keep per-call arrays separate or serialize shared numerical work internally.
    """

    def solve(self, normalized_signals: NDArray[np.generic]) -> SolverResult:
        """

        Fits a normalized band-by-pixel measurement block using the prepared reference matrix.

        Args:
            normalized_signals: Measurements with shape (bands, pixels).

        Returns:
            Component coefficients with shape (components, pixels) and solver diagnostics.
            The regional engine applies the component reporting weights afterward.
        """

        ...


class SpectralBasisRuntime(Protocol):
    """

    Supplies the selected spectra and component descriptions used by the regional engine.

    The interface supports a selected library or a reviewed reference. A reviewed
    reference may combine its column sum and an accepted abundance-rescaling factor
    into the reporting weights returned by column_sums.

    Attributes:
        identifier: Name of the selected reference.
        sha256: Checksum identifying the accepted reference contents.
        band_names: Measurement names in matrix-row order.
        component_names: Fluorescence-source names in output-column order.
        fluorophores: Explicit dyes corresponding to the components.
        biomarkers: Explicit biological targets when supplied by review.
        autofluorescence_components: Positions of the AF components.
    """

    identifier: str
    sha256: str
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    biomarkers: tuple[str | None, ...]
    autofluorescence_components: tuple[int, ...]

    @property
    def matrix(self) -> NDArray[np.float64]:
        """

        Supplies the selected band-by-component reference matrix.

        Returns:
            The float64 spectral matrix used to prepare the solver.
        """

        ...

    @property
    def column_sums(self) -> NDArray[np.float64]:
        """

        Supplies the complete factors converting component coefficients to reported intensities.

        Returns:
            One reporting weight per component, including any accepted abundance rescaling.
        """

        ...

    @property
    def band_count(self) -> int:
        """

        Returns the number of measurement rows expected by the spectral reference.

        Returns:
            The input band count.
        """

        ...

    @property
    def component_count(self) -> int:
        """

        Returns the number of component columns and resulting image channels.

        Returns:
            The output component count.
        """

        ...


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """

    Reports a completed region write or pyramid step to the application.

    Attributes:
        phase: "regions", "pyramid" or "complete".
        completed: Number of rectangles finished within the phase or current pyramid level.
        total: Number of rectangles expected for that phase or level.
        region: Written rectangle, or None for a completion event.
        level: Pyramid level number when reporting pyramid work.
    """

    phase: str
    completed: int
    total: int
    region: Region | None = None
    level: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessingSummary:
    """

    Collects the staged-image location and numerical results after regional processing.

    Attributes:
        store_path: Disk-backed Zarr location containing component arrays.
        region_count: Number of full-resolution regions processed.
        level_count: Stored level count, including full resolution.
        max_region_pixels: Largest full-resolution rectangle area processed.
        column_sums: Complete per-component reporting weights applied after fitting.
        diagnostics: Aggregate solver counts and convergence result used before publication.
    """

    store_path: Path
    region_count: int
    level_count: int
    max_region_pixels: int
    column_sums: tuple[float, ...]
    diagnostics: SolverDiagnostics


class RegionExecutionError(ValueError):
    """

    Reports settings that cannot provide a valid regional worker or numerical-memory allocation.
    """

# ==============================================================================
#                     Scheduling limits and image traversal
# ==============================================================================


@dataclass(frozen=True, slots=True)
class RegionExecutionPolicy:
    """

    Limits concurrent regional fits and the estimated arrays retained during processing.

    Attributes:
        max_workers: Maximum Python worker threads for spectral fitting.
        max_inflight_regions: Maximum prepared regions held in a processing batch.
        working_array_budget_bytes: Budget for estimated numerical input, scratch and
            output arrays. The process also needs memory for its reader, codecs and runtime.
    """

    max_workers: int = _STARTING_VALUES["max_workers"]
    max_inflight_regions: int = _STARTING_VALUES["max_inflight_regions"]
    working_array_budget_bytes: int = _STARTING_VALUES["working_array_budget_bytes"]

    def __post_init__(self) -> None:
        """

        Checks that worker count, batch limit and array-memory budget are positive integers.

        Raises:
            RegionExecutionError: A limit is invalid, nonpositive or boolean.
        """

        for name in ("max_workers", "max_inflight_regions", "working_array_budget_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise RegionExecutionError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class RegionGrid:
    """

    Describes the rectangular reads that cover an image without overlapping.

    Rectangles at the right and bottom edges are shortened to the remaining pixels.

    Attributes:
        size_x: Width of the image level in pixels.
        size_y: Height of the image level in pixels.
        chunk_size: Requested rectangle width and height.
    """

    size_x: int
    size_y: int
    chunk_size: tuple[int, int]

    def __post_init__(self) -> None:
        """

        Checks positive image dimensions and a two-value positive region size.

        Raises:
            ValueError: An image dimension or region size is invalid.
        """

        if self.size_x <= 0 or self.size_y <= 0:
            raise ValueError("region-grid dimensions must be positive")
        if len(self.chunk_size) != 2 or any(value <= 0 for value in self.chunk_size):
            raise ValueError("region-grid chunk size must contain two positive values")

    def __iter__(self) -> Iterator[Region]:
        """

        Generates region rectangles across each image row, then moves to the next strip.

        Each rectangle is clipped against the image boundary, covering every pixel once
        without constructing a list of all regions before processing starts.

        Yields:
            Region objects with pixel origin, width and height.
        """

        width, height = self.chunk_size
        for y in range(0, self.size_y, height):
            for x in range(0, self.size_x, width):
                yield Region(
                    x=x,
                    y=y,
                    width=min(width, self.size_x - x),
                    height=min(height, self.size_y - y),
                )

    def __len__(self) -> int:
        """

        Calculates how many edge-clipped rectangles are needed to cover the image.

        Returns:
            ceil(size_x / chunk_width) × ceil(size_y / chunk_height).
        """

        width, height = self.chunk_size
        return ((self.size_x + width - 1) // width) * ((self.size_y + height - 1) // height)


# ==============================================================================
#                 Reading, solving and staging component regions
# ==============================================================================


def process_regions_to_zarr(
    source: RegionSource,
    validated: ValidatedUnmixingInput,
    library: SpectralBasisRuntime,
    factors: NormalizationFactors,
    solver: RegionSolver,
    store_path: str | Path,
    *,
    progress: Callable[[ProgressEvent], None] | None = None,
    target_binding: ResolvedBasisTargetBinding | None = None,
    execution_policy: RegionExecutionPolicy | None = None,
    request_sha256: str | None = None,
) -> ProcessingSummary:
    """

    Processes a checked raw image into component arrays without reading the complete slide into memory.

    Check the source, reference and normalization relationships, then choose a region
    shard and worker count within the numerical-array budget. Open the owned generation,
    run the regional read/solve/write sequence and close the Zarr store when processing
    finishes or fails. Source-reader lifetime belongs to the enclosing workflow.

    Args:
        source: Open reader providing one selected channel rectangle per request.
        validated: Raw-image decision, dimensions, channel order and processing settings.
        library: Selected spectral matrix, component names and complete reporting factors.
        factors: Per-band acquisition and response factors for this image.
        solver: Prepared spectral solver supporting the chosen numerical method.
        store_path: Reserved incomplete processed-image generation for the component arrays.
        progress: Optional callback after completed region and pyramid writes.
        target_binding: Target-image identity and measurement order required by a
            resolved reference.
        execution_policy: Optional worker, batch-size and numerical-array limits.

    Returns:
        ProcessingSummary containing staged array location, region/level counts,
        reporting weights and combined numerical diagnostics.

    Raises:
        RegionExecutionError: A region cannot fit the selected array budget or native
            numerical-thread settings cannot establish a feasible schedule.
        TypeError or ValueError: Source, reference, factors or target fields disagree.
    """

    _validate_region_contract(
        validated,
        library,
        factors,
        target_binding,
    )
    if len(set(validated.imaging_cycles)) > 1:
        from .cyclic import CycleSolver, cycle_solve_groups
        expected_groups = cycle_solve_groups(validated.imaging_cycles, library.component_names, library.matrix)
        if type(solver) is not CycleSolver or solver.groups != expected_groups:
            raise ValueError("Cyclic unmixing requires an independent solver for every accepted cycle reference")
    policy = RegionExecutionPolicy() if execution_policy is None else execution_policy
    if type(policy) is not RegionExecutionPolicy:
        raise RegionExecutionError("execution_policy must be a RegionExecutionPolicy")
    admitted = current_operation_budget()
    if admitted is not None:
        policy = replace(policy,
            max_workers=min(policy.max_workers, admitted.cpu_threads),
            max_inflight_regions=min(2, policy.max_inflight_regions),
            working_array_budget_bytes=min(policy.working_array_budget_bytes,
                admitted.memory_bytes - 256 * 1024**2))
    batch_size, workers, block_size = _region_schedule(validated, library, policy, solver=solver)
    destination = Path(store_path)
    group = create_image_group(destination, request_sha256=request_sha256)
    try:
        return _process_regions_to_open_zarr(
            source,
            validated,
            library,
            factors,
            solver,
            destination,
            group=group,
            progress=progress,
            target_binding=target_binding,
            batch_size=batch_size,
            workers=workers,
            block_size=block_size,
        )
    finally:
        _close_zarr_store(group)


def _process_regions_to_open_zarr(
    source: RegionSource,
    validated: ValidatedUnmixingInput,
    library: SpectralBasisRuntime,
    factors: NormalizationFactors,
    solver: RegionSolver,
    destination: Path,
    *,
    group: Any,
    progress: Callable[[ProgressEvent], None] | None,
    target_binding: ResolvedBasisTargetBinding | None,
    batch_size: int,
    workers: int,
    block_size: tuple[int, int],
) -> ProcessingSummary:
    """Complete independently scheduled output shards and the intensity pyramid.

    Each admitted task owns its output footprint, serial source-reader admission
    and bounded spectral work. The coordinator retires whichever task finishes,
    writes that entire shard once and reduces diagnostics into constant-size
    totals. The executor waits for its workers before the source can close on
    success, cancellation or failure. Stored units remain independent of source
    blocks and the solver's numerical microbatches.
    """

    output_dtype = np.dtype(validated.config.output_dtype)
    level0 = create_image_level(group, 0,
        (library.component_count, validated.size_y, validated.size_x), output_dtype)
    shards = RegionGrid(validated.size_x, validated.size_y, (1024, 1024))
    source_lock = Lock()
    totals = SolverDiagnostics(0, 0, 0, 0, True, 0.0)
    completed = region_count = max_pixels = 0
    bitmap = completed_shard_bitmap(level0)
    for index, shard in enumerate(shards):
        if bitmap[index // 8] & (1 << (index % 8)):
            record = json.loads(shard_summary_path(level0, x=shard.x, y=shard.y).read_text())
            totals = _aggregate_diagnostics([totals, SolverDiagnostics(**record["diagnostics"])])
            region_count += record["region_count"]
            max_pixels = max(max_pixels, record["max_region_pixels"])
            completed += 1
    iterator = (shard for index, shard in enumerate(shards) if not bitmap[index // 8] & (1 << (index % 8)))
    active = {}

    # i) Admit at most two whole-shard tasks with separately bounded source blocks.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        def admit():
            while len(active) < min(2, batch_size):
                shard = next(iterator, None)
                if shard is None:
                    break
                future = executor.submit(_solve_shard, source, validated, library, factors,
                    solver, shard, output_dtype, source_lock, block_size)
                active[future] = shard
        admit()
        while active:
            ready, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in ready:
                shard = active.pop(future)
                block, diagnostics, count, largest = future.result()
                # ii) Publish independently completed shards once; slow peers keep their own buffers.
                write_image_shard(level0, block, x=shard.x, y=shard.y, summary={"diagnostics": asdict(diagnostics), "region_count": count, "max_region_pixels": largest})
                totals = _aggregate_diagnostics([totals, diagnostics])
                completed += 1
                region_count += count
                max_pixels = max(max_pixels, largest)
                if progress is not None:
                    progress(ProgressEvent("regions", completed, len(shards), region=shard))
                del block
            # Completed futures own their result arrays; release them before replacement work.
            del ready, future
            admit()

    # iii) Reduce completed intensities level by level, preserving odd-edge contributor counts.
    level_count = _build_pyramid(group, component_count=library.component_count,
        chunk_size=validated.config.chunk_size, output_dtype=output_dtype, progress=progress)
    group.attrs.update({"axes": ["C", "Y", "X"],
        "component_order": list(library.component_names),
        "spectral_column_sums": [float(value) for value in library.column_sums]})
    if progress is not None:
        progress(ProgressEvent("complete", len(shards), len(shards)))
    return ProcessingSummary(destination, region_count, level_count, max_pixels,
        tuple(float(value) for value in library.column_sums), totals)


def _solve_shard(source, validated, library, factors, solver, shard, output_dtype,
                 source_lock, block_size):
    """Fill one owned all-channel shard using bounded, serial source-reader admission.

    Workers overlap calculations across independent shards. The shared source
    lock protects the vendor reader while preparing each at-most-512-square band
    block. The existing solver and reporting-weight function retain their exact
    scientific calculation. Only one completed buffer returns to the publishing
    coordinator, with running diagnostic counts rather than a slide-sized list.
    """
    block = np.empty((library.component_count, shard.height, shard.width), dtype=output_dtype)
    totals = SolverDiagnostics(0, 0, 0, 0, True, 0.0)
    count = largest = 0
    for local in RegionGrid(shard.width, shard.height, block_size):
        region = Region(shard.x + local.x, shard.y + local.y, local.width, local.height)
        with source_lock:
            signals = _prepare_region(source, validated, library, factors, region)
        output, diagnostics = _solve_region(solver, signals, library.column_sums,
            library.component_count, region, output_dtype)
        block[:, local.y:local.y + local.height, local.x:local.x + local.width] = output
        totals = _aggregate_diagnostics([totals, diagnostics])
        count += 1
        largest = max(largest, region.width * region.height)
        del signals, output
    return block, totals, count, largest


def _region_schedule(
    validated: ValidatedUnmixingInput,
    library: SpectralBasisRuntime,
    policy: RegionExecutionPolicy,
    *,
    solver: RegionSolver | None = None,
) -> tuple[int, int, tuple[int, int]]:
    """Fit numerical blocks and concurrent shard work within the array allowance.

    i) We cap the requested numerical dimensions at 512 pixels and the image
    extent. Each task reserves its measured-band arrays, solver scratch, owned
    output shard and codec buffers. The CPU batched NNLS estimate uses its actual
    inner pixel limit. Reader and process memory require separate capacity.

    ii) When one task exceeds the allowance, halve its longer numerical side
    until it fits. Storage chunks and shards keep their fixed dimensions. An
    allowance that cannot hold one pixel plus the shard/codec buffers fails
    before opening an output generation.

    iii) Divide available CPU capacity by active native-library thread use,
    then admit at most two complete shard tasks within the array allowance.
    Numerical blocks can become smaller without changing the solver settings,
    channel order, output precision or the researcher's requested upper limit.

    Args:
        validated: Image dimensions and selected numerical/storage settings.
        library: Measurement-band and output-component counts.
        policy: Worker, in-flight and numerical-array limits.
        solver: Prepared solver supplying its bounded NNLS scratch dimensions.

    Returns:
        Concurrent shard count, executor thread count and numerical (width, height).

    Raises:
        RegionExecutionError: A minimal task cannot fit, or native numerical
            thread counts are missing or invalid.
    """
    from .solvers import BatchedNnlsSolver

    width = min(512, validated.config.chunk_size[0], validated.size_x)
    height = min(512, validated.config.chunk_size[1], validated.size_y)
    output_itemsize = np.dtype(validated.config.output_dtype).itemsize
    shard_bytes = (library.component_count * min(1024, validated.size_x)
        * min(1024, validated.size_y) * output_itemsize)
    chunk_bytes = library.component_count * 256 * 256 * output_itemsize
    fixed_bytes = 3 * shard_bytes + 4 * chunk_bytes
    while True:
        pixels = width * height
        if type(solver) is BatchedNnlsSolver or getattr(solver, "working_pixels_per_block", None) is not None:
            block_pixels = min(pixels, solver.working_pixels_per_block)
            estimated_bytes = pixels * (
                8 * (3 * library.band_count + 4 * library.component_count)
                + output_itemsize * library.component_count
            ) + block_pixels * 8 * (32 * library.component_count + 6 * library.band_count + 32)
        else:
            estimated_bytes = pixels * (
                8 * (3 * library.band_count + 12 * library.component_count + 16)
                + output_itemsize * library.component_count
            )
        estimated_bytes += fixed_bytes
        if estimated_bytes <= policy.working_array_budget_bytes:
            break
        if width == height == 1:
            raise RegionExecutionError(
                "working-array budget cannot hold one numerical pixel and its shard/codec buffers"
            )
        if width >= height and width > 1:
            width = (width + 1) // 2
        else:
            height = (height + 1) // 2
    native_counts = [entry.get("num_threads") for entry in threadpool_info()]
    if not native_counts or any(type(count) is not int or count <= 0 for count in native_counts):
        raise RegionExecutionError("native numerical thread count is unknown or invalid")
    cores_per_worker = max(native_counts)
    workers = min(policy.max_workers, max(1, (os.cpu_count() or 1) // cores_per_worker))
    batch_size = min(workers, 2, policy.max_inflight_regions,
        policy.working_array_budget_bytes // estimated_bytes)
    return batch_size, workers, (width, height)


def _prepare_region(
    source: RegionSource,
    validated: ValidatedUnmixingInput,
    library: SpectralBasisRuntime,
    factors: NormalizationFactors,
    region: Region,
) -> NDArray[np.generic]:
    """

    Reads and normalizes one full-resolution rectangle for all selected measurement bands.

    Check each returned channel plane against the requested rectangle shape, stack
    planes as (bands, height, width), and apply the per-band offset/divisor/response
    calculation. Flatten the two spatial axes to (bands, pixels), preserving each
    pixel's complete spectral vector in one column.

    Normalization owns a fresh working array, so its reshaped view can pass directly
    to the solver. The view retains that array independently of later source reads.

    Args:
        source: Open reader used serially for channel-region requests.
        validated: Selected image index and ordered channel positions.
        library: Expected number of measurement bands.
        factors: Image-specific acquisition and response arrays.
        region: Full-resolution x/y rectangle to read.

    Returns:
        Independent normalized array with shape (bands, region.height*region.width).

    Raises:
        ValueError: A returned plane or correction result has a different shape.
    """

    planes = []
    for channel_index in validated.band_indices:
        plane = source.read_region(
            series=validated.image_index,
            resolution=0,
            plane=PlaneIndex(c=channel_index),
            region=region,
        )
        array = np.asarray(plane)
        if array.shape != (region.height, region.width):
            raise ValueError(
                f"region read returned shape {array.shape}; expected "
                f"{(region.height, region.width)}"
            )
        planes.append(array)
    raw_region = np.stack(planes, axis=0)
    normalized = normalize_region(raw_region, factors)
    return normalized.reshape(library.band_count, -1)


def _solve_region(
    solver: RegionSolver,
    normalized_signals: NDArray[np.generic],
    column_sums: NDArray[np.float64],
    component_count: int,
    region: Region,
    output_dtype: np.dtype,
) -> tuple[NDArray[np.generic], SolverDiagnostics]:
    """

    Fits a region's component coefficients and applies their complete intensity conversion once.

    For component j, multiply its fitted coefficient row by column_sums[j]. A resolved
    reference supplies its spectral sum combined with any recorded abundance-rescaling
    factor in this vector. Reshape the result back to (components, height, width)
    and cast it to the selected output storage dtype.

    Args:
        solver: Prepared spectral solver for the selected reference matrix.
        normalized_signals: Band-by-pixel measurement matrix for one region.
        column_sums: Complete per-component reporting factors in solver output order.
        component_count: Number of output fluorescence components.
        region: Rectangle defining the result's spatial dimensions.
        output_dtype: Float32 or float64 component-pixel storage type.

    Returns:
        Component image region and the solver diagnostics for its pixels.
    """

    result = solver.solve(normalized_signals)
    solver_coefficients = result.coefficients

    # Apply the complete per-component reporting conversion exactly once.
    # This is separate from both acquisition normalization and the spectral fit.
    reported_intensities = column_sums[:, None] * solver_coefficients
    output_region = np.asarray(
        reported_intensities.reshape(component_count, region.height, region.width),
        dtype=output_dtype,
    )
    return output_region, result.diagnostics


def _close_zarr_store(group: object) -> None:
    """

    Closes the backing store when the Zarr group provides a close operation.

    Args:
        group: Open group whose store should be released.
    """

    close = getattr(getattr(group, "store", None), "close", None)
    if callable(close):
        close()


# ==============================================================================
#                Checking target and normalization compatibility
# ==============================================================================


def _validate_region_contract(
    validated: ValidatedUnmixingInput,
    library: SpectralBasisRuntime,
    factors: NormalizationFactors,
    target_binding: ResolvedBasisTargetBinding | None,
) -> None:
    """

    Checks that the selected target image, reference and normalization factors agree before reading pixels.

    For a resolved reference, recheck its accepted source record and compare the target
    checksum, measurement order and image dimensions. Require a raw-input decision,
    matching band/component counts and factor vectors covering those measurements.

    Args:
        validated: Raw-image decision, geometry and acquisition record.
        library: Selected library or resolved reference.
        factors: Normalization and response-correction vectors.
        target_binding: Image identity and dimensions required for a resolved reference.

    Raises:
        TypeError, ValueError: Target/reference identity, processing state or dimensions disagree.
    """

    _validate_normalization_response(factors, library)
    if type(library) is ResolvedBasisApplication:
        if type(target_binding) is not ResolvedBasisTargetBinding:
            raise TypeError("ResolvedBasisApplication requires exact ResolvedBasisTargetBinding")
        library.validate_target_binding(target_binding)
        if (
            target_binding.width_px != validated.size_x
            or target_binding.height_px != validated.size_y
        ):
            raise ValueError("resolved basis target dimensions differ from validated input")
    if validated.skip_unmixing or validated.acquisition is None:
        raise ValueError("region processing requires a validated raw image")
    if type(library) is ResolvedBasisApplication and (
        validated.acquisition.band_order != target_binding.band_names
    ):
        raise ValueError("resolved basis target bands differ from validated image")
    if validated.input_band_count != library.band_count:
        raise ValueError("validated input band count conflicts with spectral library")
    if validated.component_count != library.component_count:
        raise ValueError("validated component count conflicts with spectral library")
    if factors.per_band_divisor.shape != (library.band_count,):
        raise ValueError("normalization factor count conflicts with spectral library")
    return None


def _validate_normalization_response(
    factors: NormalizationFactors,
    library: SpectralBasisRuntime,
) -> None:
    """

    Checks that the measurement-response correction is the one associated with the selected reference.

    Require a positive finite float32 multiplier per band. A resolved reference must
    agree on correction name, source, measurement order and numerical multipliers.
    An ordinary selected library uses the declared all-one correction. This prevents
    reference spectra and target measurements from entering different correction conventions.

    Args:
        factors: Prepared response declaration and numerical multiplier vector.
        library: Reference against which the correction is checked.

    Raises:
        ValueError: Correction identity, operation, dimensions or values differ.
    """

    multipliers = np.asarray(factors.per_band_response_multiplier)
    if (
        multipliers.dtype != np.dtype("float32")
        or multipliers.shape != (library.band_count,)
        or not np.all(np.isfinite(multipliers))
        or np.any(multipliers <= 0.0)
    ):
        raise ValueError(
            "normalization response transform requires one finite positive float32 "
            "multiplier per measurement"
        )
    if factors.response_transform_semantic != "multiply_normalized_measurement":
        raise ValueError("normalization response transform semantic is unsupported")
    if type(library) is ResolvedBasisApplication:
        expected = np.asarray(
            library.response_correction_multipliers,
            dtype=np.float32,
        )
        if (
            factors.response_transform_id != library.response_transform_id
            or factors.response_transform_authority != library.response_transform_authority
            or factors.response_measurement_ids != library.response_measurement_ids
            or factors.response_correction_multipliers != library.response_correction_multipliers
            or not np.array_equal(multipliers, expected)
        ):
            raise ValueError(
                "normalization response transform differs from resolved basis authority"
            )
        return
    if (
        factors.response_transform_id != "mif_pipeline_identity_response_correction_v1"
        or factors.response_transform_authority != "mif_pipeline_legacy_library_path_identity_v1"
        or factors.response_measurement_ids != library.band_names
        or factors.response_correction_multipliers != (1.0,) * library.band_count
        or not np.array_equal(multipliers, np.ones(library.band_count, dtype=np.float32))
    ):
        raise ValueError(
            "legacy spectral library requires its explicit identity response transform"
        )


# ==============================================================================
#                Constructing reduced-resolution component levels
# ==============================================================================


def _build_pyramid(
    group: zarr.Group,
    *,
    component_count: int,
    chunk_size: tuple[int, int],
    output_dtype: np.dtype,
    progress: Callable[[ProgressEvent], None] | None,
) -> int:
    """

    Builds lower-resolution component arrays by repeatedly averaging the previous level's 2-by-2 pixel groups.

    Each next width and height is ceil(previous_dimension/2). Read only the source
    rectangle needed for the current target rectangle, reduce it, and write it to
    the new level. Continue until the longest side is at most 512 pixels.

    These levels are derived from fitted component intensities. Averaging raw spectral
    measurements before NNLS or clipping can produce a different result because those
    fits are nonlinear. Building the pyramid after fitting keeps one component
    calculation across the displayed resolutions.

    Args:
        group: Writable Zarr group containing completed component pixels at level "0".
        component_count: Channel count shared by every output level.
        chunk_size: Target-region width and height used for pyramid processing.
        output_dtype: Floating storage type used for the output component pyramid.
        progress: Optional callback receiving progress for each target-level write.

    Returns:
        Total number of stored levels, including level zero.
    """

    level = 0
    while max(group[str(level)].shape[1:]) > 512:
        source = group[str(level)]
        source_y, source_x = source.shape[1:]
        target_y, target_x = (source_y + 1) // 2, (source_x + 1) // 2
        target = create_image_level(group, level + 1,
            (component_count, target_y, target_x), output_dtype)
        grid = RegionGrid(target_x, target_y, (1024, 1024))
        bitmap = completed_shard_bitmap(target)
        for completed, shard in enumerate(grid, start=1):
            if bitmap[(completed - 1) // 8] & (1 << ((completed - 1) % 8)):
                continue
            block = np.empty((component_count, shard.height, shard.width), dtype=output_dtype)
            # A source footprint stays bounded while one writer completes the target shard.
            for local in RegionGrid(shard.width, shard.height, (256, 256)):
                x, y = shard.x + local.x, shard.y + local.y
                patch = np.asarray(source[:, y * 2:min(source_y, (y + local.height) * 2),
                    x * 2:min(source_x, (x + local.width) * 2)], dtype=output_dtype)
                block[:, local.y:local.y + local.height, local.x:local.x + local.width] = _area_average_2x(patch)
            write_image_shard(target, block, x=shard.x, y=shard.y)
            if progress is not None:
                progress(ProgressEvent("pyramid", completed, len(grid), region=shard, level=level + 1))
        level += 1
    return level + 1


def _area_average_2x(patch: NDArray[np.generic]) -> NDArray[np.generic]:
    """

    Averages each component's available 2-by-2 pixels to produce the next smaller image patch.

    The four parity slices select one position from each 2-by-2 group. Sum those
    slices and count how many source pixels contribute. Interior groups have four
    pixels; an odd edge can have two and an odd corner one. Divide each output
    sum by its actual contributor count.

    For a one-channel patch with rows [1, 2, 3], [4, 5, 6], [7, 8, 9], the result
    is [[3, 4.5], [7.5, 9]]. The smaller edge groups use only their available values.

    Args:
        patch: Floating array with shape (components, height, width).

    Returns:
        Array with shape (components, ceil(height/2), ceil(width/2)), in patch.dtype.
    """

    target_y = (patch.shape[1] + 1) // 2
    target_x = (patch.shape[2] + 1) // 2
    total = np.zeros((patch.shape[0], target_y, target_x), dtype=patch.dtype)

    # Count actual contributors at the edges instead of padding the patch
    # or dividing every output pixel by four.
    count = np.zeros((target_y, target_x), dtype=np.uint8)
    for y_offset in (0, 1):
        for x_offset in (0, 1):

            # Each offset selects one position from every 2-by-2 group.
            values = patch[:, y_offset::2, x_offset::2]
            if not values.size:
                continue
            total[:, : values.shape[1], : values.shape[2]] += values
            count[: values.shape[1], : values.shape[2]] += 1
    return np.divide(total, count[None, :, :], dtype=patch.dtype)


def _aggregate_diagnostics(items: list[SolverDiagnostics]) -> SolverDiagnostics:
    """

    Combines the numerical reports from independently processed image regions.

    Add pixel counts, retain the largest regional iteration count and gradient value,
    and require every region to pass for the combined convergence flag.

    Args:
        items: SolverDiagnostics records for the completed full-resolution regions.

    Returns:
        One SolverDiagnostics summary for the image.
    """

    if not items:
        return SolverDiagnostics(0, 0, 0, 0, True, 0.0)
    return SolverDiagnostics(
        total_pixels=sum(item.total_pixels for item in items),
        ls_fast_path_pixels=sum(item.ls_fast_path_pixels for item in items),
        constrained_pixels=sum(item.constrained_pixels for item in items),
        iterations=max(item.iterations for item in items),
        converged=all(item.converged for item in items),
        max_projected_gradient=max(item.max_projected_gradient for item in items),
    )
