"""

Builds metadata for processed spectral images and records how each image was produced.

Three processing paths need different records: unmixing full-resolution measurements,
copying an already-unmixed image and its existing pyramid, or reusing a completed
output. The saved record contains the source image, ordered components, numerical
settings, reported solver result and output-writing choices applicable to that path.

OME metadata for a new unmixed image names the fitted component channels and preserves
the source's spatial calibration. Original imaging settings remain in the
processing record because they describe the measured bands from which those components
were calculated. The builders and readers below keep these descriptions consistent.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum
import json
import math
from numbers import Integral
import os
from pathlib import Path
import re
from typing import Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray
from ome_types import OME
from ome_types.model import (
    AnnotationRef,
    Channel,
    Image,
    MapAnnotation,
    Pixels,
    StructuredAnnotations,
)

from ..backends import MetadataCanonicalizationError, MetadataConflictError
from ..canonical_ome_tiff import (
    CanonicalOmeTiffError,
    DigitalRangeAuthority,
    EffectiveDigitalRange,
)
from ..source_facts import SourcePyramidLevel
from .canonical import (
    _all_annotations,
    BiomarkerAssignment,
    OffsetAuthority,
    UnmixingInputMetadata,
    canonicalize_ome,
    applied_detector_offset,
    validate_offset_specification,
)
from .biomarkers import resolve_biomarker_assignment_rows
from .resolved_basis import (
    ResolvedBasisAuthorityProvenance,
    validate_resolved_basis_authority_provenance,
)

SPECTRAL_PROCESSING_NAMESPACE = "urn:mif-pipeline:spectral-processing:v1"
PREPARED_CHANNELS_NAMESPACE = "urn:mif-pipeline:prepared-channels:v1"
SPECTRAL_PROVENANCE_NAMESPACE = "urn:mif-pipeline:spectral-unmixing-provenance:v2"
SPECTRAL_METADATA_COPY_PROVENANCE_NAMESPACE = (
    "urn:mif-pipeline:spectral-metadata-copy-provenance:v2"
)
_PROCESSING_ID = "Annotation:SpectralProcessing:0"
_PROVENANCE_ID = "Annotation:SpectralUnmixingProvenance:0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NORMALIZATION_POLICY = "mif_pipeline_offset_exposure_v1"
_NORMALIZATION_FORMULA = (
    "Y_star[b] = (Y_raw[b] - applied_offset[b]) / (effective_maximum * "
    "gain[b] * binning_area[b] * exposure_seconds[b]) * "
    "response_correction_multiplier[b]"
)


# ==============================================================================
#                                    Spectral
# ==============================================================================


class ProvenanceApplicability(str, Enum):
    """

    Marks a saved processing field as NOT_APPLICABLE when its operation was not performed.

    For example, reusing an existing output has no new image chunk size or compression
    result to report, and a pixel-preserving copy has no applied normalization factors.
    """

    NOT_APPLICABLE = "not_applicable"


@runtime_checkable
class SpectralLibraryMetadata(Protocol):
    """

    Supplies the reference information needed to name and describe output component channels.

    Attributes:
        identifier: Selected library or reference name.
        sha256: Checksum associated with that selection.
        band_names: Measurement names in the order used for fitting.
        component_names: Fluorescence-component names in output order.
        fluorophores: Explicit dye identities for the component columns.
        biomarkers: Explicit biological targets available for output annotation.
        column_sums: Reporting weights used after fitting.
        autofluorescence_components: Output positions representing AF.
    """

    identifier: str
    sha256: str
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    biomarkers: tuple[str | None, ...]
    column_sums: NDArray[np.float64]
    autofluorescence_components: tuple[int, ...]

    @property
    def component_count(self) -> int:
        """

        Returns the number of fluorescence components represented by the reference.

        Returns:
            The output channel count.
        """

        ...


@dataclass(frozen=True, slots=True)
class ResolvedSpectralState:
    """

    Records what the image metadata declares about spectral separation and AF subtraction.

    Attributes:
        state: "raw", "unmixed" or "unknown".
        source: Source of the declaration, or "missing" when absent.
        autofluorescence_subtracted: Recorded subtraction state, or None when unknown.
        component_order: Component names in channel order for an unmixed image.
    """

    state: Literal["raw", "unmixed", "unknown"]
    source: str
    autofluorescence_subtracted: bool | None
    component_order: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ProcessingProvenance:
    """

    Collects the source, calculation and writing information saved with a processed image.

    Per-band values follow input_band_order, while per-component values follow
    output_component_order. The workflow fills this record from the reviewed acquisition
    settings, fitted reference, numerical result and completed output path. Validation
    then checks that these related facts agree before the record is written as JSON.

    For numerical processing, normalization_divisors contain the actual divisor used
    for each band and normalization_factors contain its reciprocal. Response multipliers
    are applied separately. Non-numerical paths use empty numerical vectors and the
    NOT_APPLICABLE value where no calculation or write was performed.

    Attributes:
        input_path: Source image location recorded by the workflow.
        output_path: Processed or reused image location.
        timestamp: Recorded operation time.
        pipeline_version: Application version used for processing.
        library_identifier: Selected reference identity, or None for a library-free copy.
        library_sha256: Checksum paired with that reference identity.
        input_band_order: Measurement bands in calculation order.
        output_component_order: Components in output-channel order.
        exposure_seconds: Per-band exposure times.
        normalization_factors: Reciprocals of the applied acquisition divisors.
        source_storage_dtype: Recorded source pixel-container type.
        source_storage_bits: Bit width of that representation.
        source_storage_max: Applied effective intensity maximum, None for as-stored fitting,
            or 0.0 on a non-numerical copy/reuse path.
        camera_bit_depth: Optional retained detector-depth provenance; empty when absent.
            It does not determine the intensity correction.
        gain: Per-band detector gain.
        binning: Square acquisition-binning side length.
        binning_area: Corresponding number of acquisition pixels per bin.
        source_spectral_state: Recorded source raw/unmixed state.
        source_autofluorescence_subtracted: Source AF-subtraction declaration.
        state_source: Source of the state used for this operation.
        state_override: Explicit state decision when supplied by the researcher.
        solver: Estimator used, or "not_applied" for a library-free copy.
        nnls_tolerance: Requested NNLS stopping tolerance when applicable.
        nnls_max_iterations: Requested NNLS fitting budget when applicable.
        solver_converged: Reported numerical convergence, or None when no solve ran.
        solver_iterations: Reported iteration count; clipped LS records zero.
        solver_max_projected_gradient: Largest reported NNLS gradient violation.
        normalization_policy: Applied correction-rule identifier or "not_applied".
        normalization_formula: Written acquisition-correction expression.
        spectral_basis: Description of how the selected reference spectra were obtained.
        spectral_column_sums: Complete per-component reporting weights applied after fitting.
        af_subtraction_performed: Whether this operation performed AF subtraction.
        processing_chunk_width: Width of a processing region in pixels, when applicable.
        processing_chunk_height: Height of a processing region in pixels.
        pyramid_levels: Number of output levels written, including full resolution.
        source_resolution_policy: Full-resolution solve, all-level copy or no-read reuse.
        output_pyramid_policy: Generated averaging pyramid, native copy or reused output.
        writer_tile_width: Zarr inner-chunk width when a writer ran.
        writer_tile_height: Zarr inner-chunk height when a writer ran.
        compression: Fixed Blosc/Zstd level 1 with byte shuffle for new image writes.
        output_dtype: Written pixel dtype, or NOT_APPLICABLE for no-write reuse.
        warnings: Warnings retained with the operation.
        channel_name_replacement: Whether marker names were requested as display names.
        input_sha256: Checksum identifying the source image contents.
        container_dtype: Pixel-container dtype read from the source image facts.
        container_bits: Width of that pixel container.
        source_dimension_order: OME axis order of the selected source image.
        ome_significant_bits: Meaningful bits explicitly reported in OME, when present.
        effective_digital_maxima: Per-band maxima actually applied during normalization.
        digital_range_authority: Metadata field or profile supplying those maxima.
        offsets: Original recorded detector offsets, possibly absent when not applied.
        applied_offsets: Actual subtraction vector; zero for metadata-known dark
            correction, or None when no offset step is performed.
        source_intensity_representation: Metadata-known processing of stored values.
        offset_authority: Recorded source of the offsets.
        offset_profile_identifier: Explicit reviewed offset profile when used.
        native_pyramid_levels: Descriptions of the source's stored image levels.
        normalization_divisors: Applied per-band divisor: the full acquisition
            product, actual exposure seconds for exposure-only correction, or one for as-stored fitting.
        response_transform_id: Selected measurement-response correction.
        response_transform_semantic: Operation used to apply that correction.
        response_transform_authority: Source or policy supplying the correction.
        response_measurement_ids: Measurement IDs associated with its multipliers.
        response_correction_multipliers: Positive multipliers applied after acquisition division.
        resolved_basis_authority: Detailed accepted reference-selection record, when supplied.
        output_biomarkers: Explicit biological targets associated with output components.
        schema_version: Saved processing-record format version.
        effective_digital_range: Complete meaningful-intensity-range record for numerical processing.
        execution_runtime: Sorted key/value descriptions of the numerical execution environment.
        input_imaging_cycles: Zero-based cycle of each measured band for cycle-aware fitting.
        output_imaging_cycles: Zero-based cycle of each distinct fitted component.
    """

    input_path: str
    output_path: str
    timestamp: str
    pipeline_version: str
    library_identifier: str | None
    library_sha256: str | None
    input_band_order: tuple[str, ...]
    output_component_order: tuple[str, ...]
    exposure_seconds: tuple[float | None, ...]
    normalization_factors: tuple[float, ...]
    source_storage_dtype: str
    source_storage_bits: int
    source_storage_max: float | None
    camera_bit_depth: tuple[int, ...]
    gain: tuple[float | None, ...]
    binning: tuple[int | None, ...]
    binning_area: tuple[int | None, ...]
    source_spectral_state: str
    source_autofluorescence_subtracted: bool | None
    state_source: str
    state_override: str | None
    solver: str
    nnls_tolerance: float | None
    nnls_max_iterations: int | None
    solver_converged: bool | None
    solver_iterations: int | None
    solver_max_projected_gradient: float | None
    normalization_policy: str
    normalization_formula: str
    spectral_basis: str
    spectral_column_sums: tuple[float, ...]
    af_subtraction_performed: bool
    processing_chunk_width: int | ProvenanceApplicability
    processing_chunk_height: int | ProvenanceApplicability
    pyramid_levels: int
    source_resolution_policy: Literal[
        "level0_only",
        "copy_all_native_levels",
        "not_read",
    ]
    output_pyramid_policy: Literal[
        "recursive_2x_area_average_from_processed_level0",
        "pixel_identical_source_pyramid",
        "linear_resampled_cycle_grid",
        "reuse_existing_source_pyramid",
    ]
    writer_tile_width: int | ProvenanceApplicability
    writer_tile_height: int | ProvenanceApplicability
    compression: Literal["blosc_zstd_level1_byte_shuffle"] | ProvenanceApplicability
    output_dtype: str | ProvenanceApplicability
    warnings: tuple[str, ...]
    target_af_behavior: str | None = None
    channel_name_replacement: bool = False
    input_sha256: str = ""
    container_dtype: str = "unknown"
    container_bits: int = 0
    source_dimension_order: str = "unknown"
    ome_significant_bits: int | None = None
    effective_digital_maxima: tuple[float, ...] = ()
    digital_range_authority: DigitalRangeAuthority | ProvenanceApplicability = (
        ProvenanceApplicability.NOT_APPLICABLE
    )
    offsets: tuple[float | None, ...] = ()
    applied_offsets: tuple[float, ...] | None = None
    source_intensity_representation: str = "unknown"
    offset_authority: OffsetAuthority | ProvenanceApplicability = (
        ProvenanceApplicability.NOT_APPLICABLE
    )
    offset_profile_identifier: str | None = None
    native_pyramid_levels: tuple[SourcePyramidLevel, ...] = ()
    normalization_divisors: tuple[float, ...] = ()
    response_transform_id: str | ProvenanceApplicability = ProvenanceApplicability.NOT_APPLICABLE
    response_transform_semantic: str | ProvenanceApplicability = (
        ProvenanceApplicability.NOT_APPLICABLE
    )
    response_transform_authority: str | ProvenanceApplicability = (
        ProvenanceApplicability.NOT_APPLICABLE
    )
    response_measurement_ids: tuple[str, ...] = ()
    response_correction_multipliers: tuple[float, ...] = ()
    resolved_basis_authority: ResolvedBasisAuthorityProvenance | None = None
    output_biomarkers: tuple[str | None, ...] = ()
    schema_version: int = 2
    effective_digital_range: EffectiveDigitalRange | None = None
    execution_runtime: tuple[tuple[str, str], ...] = ()
    input_imaging_cycles: tuple[int, ...] = ()
    output_imaging_cycles: tuple[int, ...] = ()
    spatial_registration: dict | None = None


# ==============================================================================
#                     Checking the recorded processing path
# ==============================================================================


def _validate_imaging_cycle_provenance(provenance: ProcessingProvenance) -> None:
    from .cycles import split_cycle_component
    inputs, outputs = provenance.input_imaging_cycles, provenance.output_imaging_cycles
    if not inputs and not outputs:
        if provenance.schema_version == 4:
            raise MetadataCanonicalizationError("Cycle-aware provenance requires complete imaging cycle identities")
        return
    if (provenance.schema_version != 4 or provenance.source_resolution_policy != "level0_only"
            or type(inputs) is not tuple or type(outputs) is not tuple
            or len(inputs) != len(provenance.input_band_order)
            or len(outputs) != len(provenance.output_component_order)
            or any(type(cycle) is not int or cycle < 0 for cycle in inputs + outputs)
            or set(inputs) != set(outputs)):
        raise MetadataCanonicalizationError("Numerical cycle provenance must cover every measured band and output component")
    for name, cycle in zip(provenance.output_component_order, outputs, strict=True):
        declared = split_cycle_component(name)[1]
        if declared != cycle and not (declared is None and len(set(inputs)) == 1):
            raise MetadataCanonicalizationError("An output component differs from its recorded imaging cycle")
    if provenance.resolved_basis_authority is None:
        raise MetadataCanonicalizationError("Cycle unmixing requires its accepted spectral reference authority")


def validate_processing_provenance(provenance: ProcessingProvenance) -> None:
    """

    Checks that the saved fields consistently describe the operation performed on the image.

    Validate source checksums, pixel storage, dimensions and native levels first.
    Then use the source-resolution and output-pyramid policies to select the path-specific
    checks. Numerical output requires valid normalization and successful solver diagnostics;
    a copy preserves source dtype and levels; no-read reuse has no newly written levels
    or writer settings.

    Args:
        provenance: Complete processing record assembled by the workflow.

    Raises:
        MetadataCanonicalizationError: Source facts, processing policies, numerical
            results or reference/writer relationships are inconsistent.
    """

    if provenance.target_af_behavior not in {None, "retain", "already_removed"}:
        raise MetadataCanonicalizationError("processing target AF choice is unsupported")
    _validate_imaging_cycle_provenance(provenance)
    if provenance.spatial_registration is not None:
        from ..cycle_registration import AcceptedCycleRegistration
        AcceptedCycleRegistration.from_mapping(provenance.spatial_registration)
        if provenance.output_dtype != "float32" or provenance.source_resolution_policy == "not_read":
            raise MetadataCanonicalizationError("Registered cycle pixels require a recorded float32 resampling output")
    elif provenance.output_pyramid_policy == "linear_resampled_cycle_grid":
        raise MetadataCanonicalizationError("Cycle resampling requires its accepted spatial registration")
    runtime = provenance.execution_runtime
    if type(runtime) is not tuple or any(
        type(row) is not tuple
        or len(row) != 2
        or any(type(value) is not str or not value or value != value.strip() for value in row)
        for row in runtime
    ):
        raise MetadataCanonicalizationError(
            "processing execution runtime requires nonempty string key/value pairs"
        )
    runtime_keys = tuple(key for key, _ in runtime)
    if runtime_keys != tuple(sorted(set(runtime_keys))):
        raise MetadataCanonicalizationError(
            "processing execution runtime keys must be unique and sorted"
        )
    if type(provenance.schema_version) is not int or provenance.schema_version not in {2, 3, 4}:
        raise MetadataCanonicalizationError(
            "processing provenance requires supported schema version 2, 3 or 4"
        )
    library_free_copy = provenance.schema_version == 3
    if library_free_copy and (
        provenance.library_identifier is not None
        or provenance.library_sha256 is not None
        or provenance.source_resolution_policy not in {"copy_all_native_levels", "not_read"}
        or provenance.solver != "not_applied"
        or provenance.spectral_basis != "not_applied"
        or provenance.spectral_column_sums
        or provenance.resolved_basis_authority is not None
        or provenance.nnls_tolerance is not None
        or provenance.nnls_max_iterations is not None
    ):
        raise MetadataCanonicalizationError(
            "Library-free copy provenance cannot claim a spectral basis or numerical solver"
        )
    if (
        not isinstance(provenance.input_sha256, str)
        or _SHA256.fullmatch(provenance.input_sha256) is None
    ):
        raise MetadataCanonicalizationError(
            "processing provenance requires a complete lowercase input SHA-256"
        )
    if not library_free_copy and (
        not isinstance(provenance.library_sha256, str)
        or _SHA256.fullmatch(provenance.library_sha256) is None
    ):
        raise MetadataCanonicalizationError(
            "processing provenance requires a complete lowercase library SHA-256"
        )
    if (
        not isinstance(provenance.container_bits, Integral)
        or isinstance(provenance.container_bits, bool)
        or provenance.container_bits <= 0
        or not isinstance(provenance.container_dtype, str)
        or provenance.container_dtype == "unknown"
    ):
        raise MetadataCanonicalizationError(
            "processing provenance requires container dtype and bit width"
        )
    levels = provenance.native_pyramid_levels
    if (
        not isinstance(levels, tuple)
        or not levels
        or any(type(level) is not SourcePyramidLevel for level in levels)
    ):
        raise MetadataCanonicalizationError(
            "processing provenance requires an ordered native-pyramid inventory"
        )
    _validate_native_pyramid_inventory(levels)
    level0 = levels[0]
    if (
        level0.dtype != provenance.container_dtype
        or level0.container_bits != provenance.container_bits
    ):
        raise MetadataCanonicalizationError(
            "native level 0 conflicts with provenance container facts"
        )
    if provenance.ome_significant_bits is not None and (
        not isinstance(provenance.ome_significant_bits, Integral)
        or isinstance(provenance.ome_significant_bits, bool)
        or not 0 < provenance.ome_significant_bits <= provenance.container_bits
    ):
        raise MetadataCanonicalizationError(
            "provenance OME SignificantBits conflicts with the container"
        )
    if provenance.source_dimension_order not in {
        "XYZCT",
        "XYZTC",
        "XYCZT",
        "XYCTZ",
        "XYTCZ",
        "XYTZC",
    }:
        raise MetadataCanonicalizationError(
            "processing provenance requires a canonical source dimension order"
        )
    if (
        not isinstance(provenance.pyramid_levels, Integral)
        or isinstance(provenance.pyramid_levels, bool)
        or provenance.pyramid_levels < 0
    ):
        raise MetadataCanonicalizationError(
            "processing provenance requires a nonnegative output pyramid count"
        )

    # Numerical output, pixel-identical copying and no-read reuse have
    # different required facts. Select the check from the complete policy pair.
    path = (
        provenance.source_resolution_policy,
        provenance.output_pyramid_policy,
    )
    raw_path = (
        "level0_only",
        "recursive_2x_area_average_from_processed_level0",
    )
    copy_path = ("copy_all_native_levels", "pixel_identical_source_pyramid")
    skip_path = ("not_read", "reuse_existing_source_pyramid")
    if path == raw_path:
        _validate_writer_publication_facts(provenance, raw=True)
        _validate_raw_processing_provenance(provenance)
    elif path == copy_path or path == ("copy_all_native_levels", "linear_resampled_cycle_grid"):
        _validate_writer_publication_facts(provenance, raw=False)
        _validate_non_numerical_processing_provenance(provenance)
        if provenance.pyramid_levels != len(levels):
            raise MetadataCanonicalizationError(
                "metadata-only copy pyramid count must match the native inventory"
            )
    elif path == skip_path:
        _validate_non_numerical_processing_provenance(provenance)
        if provenance.pyramid_levels != 0:
            raise MetadataCanonicalizationError(
                "no-read skip provenance cannot report written pyramid levels"
            )
        nonwriter_facts = (
            provenance.processing_chunk_width,
            provenance.processing_chunk_height,
            provenance.writer_tile_width,
            provenance.writer_tile_height,
            provenance.compression,
            provenance.output_dtype,
        )
        if any(value is not ProvenanceApplicability.NOT_APPLICABLE for value in nonwriter_facts):
            raise MetadataCanonicalizationError(
                "no-read skip requires typed not-applicable nonwriter facts"
            )
    else:
        raise MetadataCanonicalizationError(
            "processing provenance contains an invalid resolution/pyramid policy pair"
        )
    _validate_resolved_basis_provenance(provenance)


def _validate_resolved_basis_provenance(provenance: ProcessingProvenance) -> None:
    """

    Checks the detailed reference record against the image and settings of this operation.

    Compare the reference identity, source-image checksum, measurement/component order,
    response correction and reporting factors with the surrounding processing record.
    This prevents a reference approved for one image or correction from being attached
    to another operation.

    Args:
        provenance: Processing record with an optional detailed reference description.

    Raises:
        MetadataCanonicalizationError: Reference fields or their relationship to the operation differ.
    """

    authority = provenance.resolved_basis_authority
    if authority is None:
        return
    if type(authority) is not ResolvedBasisAuthorityProvenance:
        raise MetadataCanonicalizationError(
            "resolved basis authority must be exact typed provenance"
        )
    try:
        validate_resolved_basis_authority_provenance(authority)
    except (TypeError, ValueError) as exc:
        raise MetadataCanonicalizationError(
            f"resolved basis authority failed live validation: {exc}"
        ) from exc
    if provenance.source_resolution_policy != "level0_only":
        raise MetadataCanonicalizationError(
            "resolved basis authority is valid only for numerical level0 processing"
        )
    if authority.authority_id != provenance.library_identifier:
        raise MetadataCanonicalizationError(
            "resolved basis authority identifier conflicts with enclosing library identifier"
        )
    if authority.authority_sha256 != provenance.library_sha256:
        raise MetadataCanonicalizationError(
            "resolved basis authority SHA-256 conflicts with enclosing library SHA-256"
        )
    if authority.target_image_sha256 != provenance.input_sha256:
        raise MetadataCanonicalizationError(
            "resolved basis target SHA-256 conflicts with processing input"
        )
    if authority.band_names != provenance.input_band_order:
        raise MetadataCanonicalizationError(
            "resolved basis band order conflicts with processing provenance"
        )
    if (
        authority.response_transform_id != provenance.response_transform_id
        or authority.response_transform_semantic != provenance.response_transform_semantic
        or authority.response_transform_authority != provenance.response_transform_authority
        or authority.response_measurement_ids != provenance.response_measurement_ids
        or authority.response_correction_multipliers != provenance.response_correction_multipliers
    ):
        raise MetadataCanonicalizationError(
            "resolved basis response transform conflicts with processing provenance"
        )
    if authority.component_names != provenance.output_component_order:
        raise MetadataCanonicalizationError(
            "resolved basis component order conflicts with processing provenance"
        )
    if authority.conversion_factors != provenance.spectral_column_sums:
        raise MetadataCanonicalizationError(
            "resolved basis conversion factors conflict with processing provenance"
        )


def _validate_native_pyramid_inventory(
    levels: tuple[SourcePyramidLevel, ...],
) -> None:
    """

    Checks that the source-level descriptions form a consistently typed image pyramid.

    Require consecutive level numbers beginning at zero, positive dimensions and the
    same channels and storage type at every level. Successive levels must reduce at
    least one spatial dimension and cannot increase either dimension.

    Args:
        levels: Ordered SourcePyramidLevel records.

    Raises:
        MetadataCanonicalizationError: Level numbering, dimensions, channel layout or storage differs.
    """

    previous_size: tuple[int, int] | None = None
    expected_channels = levels[0].channel_count
    expected_dtype = levels[0].dtype
    expected_bits = levels[0].container_bits
    for expected_index, level in enumerate(levels):
        positive_integer_facts = (
            level.size_x,
            level.size_y,
            level.channel_count,
            level.container_bits,
        )
        if (
            not isinstance(level.level_index, Integral)
            or isinstance(level.level_index, bool)
            or level.level_index != expected_index
            or any(
                not isinstance(value, Integral) or isinstance(value, bool) or value <= 0
                for value in positive_integer_facts
            )
        ):
            raise MetadataCanonicalizationError(
                "native-pyramid levels require monotonic indices and positive dimensions"
            )
        if (
            not isinstance(level.samples_per_pixel, Integral)
            or isinstance(level.samples_per_pixel, bool)
            or level.samples_per_pixel != 1
        ):
            raise MetadataCanonicalizationError("native-pyramid samples per pixel must equal one")
        try:
            dtype = np.dtype(level.dtype)
        except (TypeError, ValueError) as exc:
            raise MetadataCanonicalizationError(
                "native-pyramid dtype/container facts are invalid"
            ) from exc
        if dtype.itemsize * 8 != level.container_bits:
            raise MetadataCanonicalizationError("native-pyramid dtype/container bits do not agree")
        if (
            level.channel_count != expected_channels
            or level.dtype != expected_dtype
            or level.container_bits != expected_bits
        ):
            raise MetadataCanonicalizationError(
                "native-pyramid channel and dtype/container facts must be homogeneous"
            )
        size = (level.size_x, level.size_y)
        if previous_size is not None and (
            size[0] > previous_size[0] or size[1] > previous_size[1] or size == previous_size
        ):
            raise MetadataCanonicalizationError(
                "native-pyramid dimensions must decrease monotonically"
            )
        previous_size = size


def _validate_writer_publication_facts(
    provenance: ProcessingProvenance,
    *,
    raw: bool,
) -> None:
    """

    Checks processing-region and Zarr-writing settings for an operation that produced a new image.

    Numerical output uses float32 or float64, while a pixel-preserving copy retains
    the source dtype. Both require positive region/chunk dimensions and the fixed
    Blosc/Zstd level 1 codec with byte shuffle.

    Args:
        provenance: Processing record containing output settings.
        raw: True for numerical unmixing, False for a pixel-preserving copy.

    Raises:
        MetadataCanonicalizationError: Writer dimensions, compression or output dtype are invalid.
    """

    named_dimensions = (
        (provenance.processing_chunk_width, "processing chunk"),
        (provenance.processing_chunk_height, "processing chunk"),
        (provenance.writer_tile_width, "writer tile"),
        (provenance.writer_tile_height, "writer tile"),
    )
    for value, label in named_dimensions:
        if not isinstance(value, Integral) or isinstance(value, bool) or value <= 0:
            raise MetadataCanonicalizationError(
                f"writing provenance requires a positive {label} dimension"
            )
    if (
        not isinstance(provenance.pyramid_levels, Integral)
        or isinstance(provenance.pyramid_levels, bool)
        or provenance.pyramid_levels <= 0
    ):
        raise MetadataCanonicalizationError(
            "writing provenance requires a positive output pyramid count"
        )
    if provenance.compression != "blosc_zstd_level1_byte_shuffle":
        raise MetadataCanonicalizationError(
            "writing provenance requires Blosc/Zstd level 1 with byte shuffle"
        )
    if raw:
        if provenance.output_dtype not in {"float32", "float64"}:
            raise MetadataCanonicalizationError(
                "raw writing provenance requires float32 or float64 output dtype"
            )
    elif provenance.output_dtype != provenance.native_pyramid_levels[0].dtype:
        raise MetadataCanonicalizationError(
            "metadata-copy output dtype must match the source pixel dtype"
        )


def _validate_raw_processing_provenance(provenance: ProcessingProvenance) -> None:
    """

    Checks the numerical result and the intensity corrections recorded for raw-image unmixing.

    Require successful convergence and appropriate solver diagnostics. Check complete
    per-band normalization vectors, the effective digital range and the offset source.
    Response-correction IDs and values must agree with the selected reference; the
    ordinary library route uses its explicit identity correction.

    Each recorded normalization factor must equal the reciprocal of its applied divisor,
    within relative tolerance 1e-6. This catches inconsistent correction records before
    component-image metadata is published.

    Args:
        provenance: Record describing full-resolution numerical unmixing.

    Raises:
        MetadataCanonicalizationError: Solver diagnostics, normalization values or
            source/reference relationships are inconsistent.
    """

    count = len(provenance.input_band_order)
    if count <= 0:
        raise MetadataCanonicalizationError("raw provenance requires a measurement-band inventory")
    if (
        type(provenance.solver_converged) is not bool
        or provenance.solver_converged is not True
        or type(provenance.solver_iterations) is not int
        or provenance.solver_iterations < 0
        or type(provenance.solver_max_projected_gradient) is not float
        or not math.isfinite(provenance.solver_max_projected_gradient)
        or provenance.solver_max_projected_gradient < 0.0
    ):
        raise MetadataCanonicalizationError(
            "numerical success requires exact converged finite solver diagnostics"
        )
    if provenance.solver == "clipped_ls":
        if (
            provenance.nnls_tolerance is not None
            or provenance.nnls_max_iterations is not None
            or provenance.solver_iterations != 0
            or provenance.solver_max_projected_gradient != 0.0
        ):
            raise MetadataCanonicalizationError(
                "clipped-LS success requires its exact noniterative diagnostics"
            )
    elif provenance.solver == "nnls":
        if (
            type(provenance.nnls_tolerance) is not float
            or not math.isfinite(provenance.nnls_tolerance)
            or provenance.nnls_tolerance <= 0.0
            or type(provenance.nnls_max_iterations) is not int
            or provenance.nnls_max_iterations <= 0
            or provenance.solver_iterations > provenance.nnls_max_iterations
        ):
            raise MetadataCanonicalizationError(
                "NNLS success requires exact configured and bounded diagnostics"
            )
    else:
        raise MetadataCanonicalizationError("numerical provenance solver is unsupported")
    if provenance.output_biomarkers and (
        type(provenance.output_biomarkers) is not tuple
        or len(provenance.output_biomarkers) != len(provenance.output_component_order)
        or any(
            item is not None and (type(item) is not str or not item or item.strip() != item)
            for item in provenance.output_biomarkers
        )
    ):
        raise MetadataCanonicalizationError(
            "numerical provenance requires exact output biomarker identity"
        )
    as_stored = provenance.normalization_policy == "mif_pipeline_as_stored_v1"
    per_band = (
        provenance.exposure_seconds,
        provenance.normalization_factors,
        provenance.gain,
        provenance.binning,
        provenance.binning_area,
        *((provenance.effective_digital_maxima,) if not as_stored else ()),
        provenance.offsets,
        provenance.normalization_divisors,
        provenance.response_correction_multipliers,
    )
    if any(len(values) != count for values in per_band):
        raise MetadataCanonicalizationError(
            "raw provenance requires complete per-band range, offset, and divisor facts"
        )
    if as_stored:
        if (provenance.effective_digital_range is not None or provenance.effective_digital_maxima
            or provenance.source_storage_max is not None
            or provenance.digital_range_authority is not ProvenanceApplicability.NOT_APPLICABLE):
            raise MetadataCanonicalizationError("As-stored fitting cannot claim an applied detector intensity range")
    else:
        if not isinstance(provenance.digital_range_authority, DigitalRangeAuthority):
            raise MetadataCanonicalizationError("Raw provenance requires a typed digital-range authority")
        _validate_effective_digital_range(provenance)
    if not isinstance(provenance.offset_authority, OffsetAuthority):
        raise MetadataCanonicalizationError("raw provenance requires a typed offset authority")
    try:
        validate_offset_specification(
            tuple(value for value in provenance.offsets if value is not None),
            provenance.offset_authority,
            provenance.offset_profile_identifier,
        )
    except MetadataConflictError as exc:
        raise MetadataCanonicalizationError(str(exc)) from exc
    expected_formula = {
        _NORMALIZATION_POLICY: _NORMALIZATION_FORMULA,
        "mif_pipeline_exposure_only_v1": "Y_star[b] = Y_raw[b] / exposure_seconds[b]",
        "mif_pipeline_as_stored_v1": "Y_star[b] = Y_raw[b]",
    }.get(provenance.normalization_policy)
    if expected_formula is None or provenance.normalization_formula != expected_formula:
        raise MetadataCanonicalizationError("Raw provenance requires its exact selected intensity-correction formula")
    if provenance.source_intensity_representation not in {"unknown", "raw_counts", "dark_corrected_counts", "normalized_counts"}:
        raise MetadataCanonicalizationError("Numerical provenance requires a supported stored intensity representation")
    if as_stored:
        if (provenance.source_intensity_representation != "normalized_counts"
            or provenance.applied_offsets is not None
            or provenance.normalization_divisors != (1.0,) * count
            or provenance.response_correction_multipliers != (1.0,) * count):
            raise MetadataCanonicalizationError("As-stored fitting must preserve known normalized input values")
    elif provenance.source_intensity_representation == "normalized_counts":
        raise MetadataCanonicalizationError("Normalized input values cannot receive intensity correction again")
    elif provenance.normalization_policy == "mif_pipeline_exposure_only_v1":
        if provenance.applied_offsets is not None:
            raise MetadataCanonicalizationError("Exposure-only correction must not apply offsets")
        if provenance.response_correction_multipliers != (1.0,) * count or any(
            not math.isclose(divisor, exposure, rel_tol=1e-6, abs_tol=0.0)
            for divisor, exposure in zip(provenance.normalization_divisors, provenance.exposure_seconds, strict=True)
        ):
            raise MetadataCanonicalizationError("Exposure-only provenance requires actual exposure divisors and no response correction")
    else:
        if any(value is None for vector in (provenance.gain, provenance.binning, provenance.binning_area) for value in vector):
            raise MetadataCanonicalizationError("Full correction provenance requires complete applied imaging settings")
        expected_offsets = tuple(applied_detector_offset(value, provenance.source_intensity_representation) for value in provenance.offsets)
        if (provenance.applied_offsets is None or len(provenance.applied_offsets) != count
            or any(value is None for value in expected_offsets)
            or any(not math.isfinite(value) for value in provenance.applied_offsets)
            or not np.array_equal(np.asarray(expected_offsets, dtype=np.float32), np.asarray(provenance.applied_offsets))):
            raise MetadataCanonicalizationError("Applied offsets disagree with the recorded intensity representation")
    for value, label in (
        (provenance.response_transform_id, "response transform ID"),
        (provenance.response_transform_authority, "response transform authority"),
    ):
        if type(value) is not str or not value or value.strip() != value:
            raise MetadataCanonicalizationError(f"raw provenance requires a canonical {label}")
    expected_response_ids = (
        provenance.resolved_basis_authority.measurement_ids
        if provenance.resolved_basis_authority is not None
        else provenance.input_band_order
    )
    if (
        provenance.response_transform_semantic != "multiply_normalized_measurement"
        or type(provenance.response_measurement_ids) is not tuple
        or provenance.response_measurement_ids != expected_response_ids
        or len(set(provenance.response_measurement_ids)) != count
    ):
        raise MetadataCanonicalizationError(
            "raw provenance response transform axis or semantic is invalid"
        )
    if provenance.resolved_basis_authority is None and (
        provenance.response_transform_id != "mif_pipeline_identity_response_correction_v1"
        or provenance.response_transform_authority != "mif_pipeline_legacy_library_path_identity_v1"
        or provenance.response_correction_multipliers != (1.0,) * count
    ):
        raise MetadataCanonicalizationError(
            "legacy library provenance requires its explicit identity response transform"
        )
    if any(
        not math.isfinite(float(value)) or float(value) <= 0
        for values in (
            provenance.effective_digital_maxima,
            provenance.normalization_divisors,
            provenance.normalization_factors,
            provenance.response_correction_multipliers,
        )
        for value in values
    ):
        raise MetadataCanonicalizationError(
            "raw provenance range, divisors, and factors must be finite and positive"
        )
    if (
        provenance.source_storage_dtype != provenance.container_dtype
        or provenance.source_storage_bits != provenance.container_bits
        or (not as_stored and not math.isclose(
            provenance.source_storage_max,
            provenance.effective_digital_maxima[0],
            rel_tol=0.0,
            abs_tol=0.0,
        ))
    ):
        raise MetadataCanonicalizationError(
            "raw provenance legacy storage facts conflict with header-owned range"
        )
    for factor, divisor in zip(
        provenance.normalization_factors,
        provenance.normalization_divisors,
        strict=True,
    ):
        if not math.isclose(factor, 1.0 / divisor, rel_tol=1e-6, abs_tol=0.0):
            raise MetadataCanonicalizationError(
                "raw provenance normalization factors conflict with divisors"
            )


def _validate_effective_digital_range(provenance: ProcessingProvenance) -> None:
    """

    Checks the intensity range used for normalization against its recorded header or profile source.

    A header-derived unsigned range uses maximum = 2**meaningful_bits - 1. An explicit
    profile supplies its ID, meaningful bits and positive maximum, which must fit within
    the stored unsigned container when applicable. Every applied band maximum must agree
    with the chosen range record.

    Args:
        provenance: Numerical record containing the effective range and container facts.

    Raises:
        MetadataCanonicalizationError: Bits, maxima, profile or declared range source disagree.
    """

    digital_range = provenance.effective_digital_range
    if type(digital_range) is not EffectiveDigitalRange:
        raise MetadataCanonicalizationError(
            "raw provenance requires a typed effective digital range"
        )
    if (
        digital_range.container_dtype != provenance.container_dtype
        or type(digital_range.container_bits) is not int
        or digital_range.container_bits != provenance.container_bits
        or type(digital_range.meaningful_bits) is not int
        or not 0 < digital_range.meaningful_bits <= digital_range.container_bits
        or type(digital_range.maximum) is not float
        or not math.isfinite(digital_range.maximum)
        or digital_range.maximum <= 0
        or digital_range.authority is not provenance.digital_range_authority
        or any(value != digital_range.maximum for value in provenance.effective_digital_maxima)
    ):
        raise MetadataCanonicalizationError(
            "raw provenance digital range conflicts with its exact range facts"
        )
    unsigned = np.issubdtype(np.dtype(provenance.container_dtype), np.unsignedinteger)
    if digital_range.authority is DigitalRangeAuthority.EXPLICIT_PROFILE:
        if (
            type(digital_range.profile_id) is not str
            or not digital_range.profile_id
            or digital_range.profile_id.strip() != digital_range.profile_id
            or (unsigned and digital_range.maximum > float((1 << digital_range.container_bits) - 1))
        ):
            raise MetadataCanonicalizationError(
                "raw provenance digital range has an invalid explicit profile"
            )
    elif (
        not unsigned
        or digital_range.profile_id is not None
        or digital_range.maximum != float((1 << digital_range.meaningful_bits) - 1)
        or (
            provenance.ome_significant_bits is not None
            and (
                digital_range.authority is not DigitalRangeAuthority.OME_SIGNIFICANT_BITS
                or digital_range.meaningful_bits != provenance.ome_significant_bits
            )
        )
        or (
            provenance.ome_significant_bits is None
            and digital_range.authority
            not in {
                DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE,
                DigitalRangeAuthority.SOURCE_STORAGE_BITS_PER_SAMPLE,
            }
        )
    ):
        raise MetadataCanonicalizationError(
            "raw provenance digital range conflicts with its header authority"
        )


def _validate_non_numerical_processing_provenance(
    provenance: ProcessingProvenance,
) -> None:
    """

    Checks that a copy or reuse operation records the absence of numerical normalization.

    Applied offset/range/response vectors must be empty and their operation-specific
    fields must use NOT_APPLICABLE. Original acquisition facts can still be retained
    for interpretation alongside those operation fields.

    Args:
        provenance: Record for a metadata-copy or no-read reuse path.

    Raises:
        MetadataCanonicalizationError: The record claims numerical correction on a copy/reuse path.
    """

    if provenance.effective_digital_range is not None:
        raise MetadataCanonicalizationError(
            "copy/skip provenance cannot carry an effective digital range"
        )
    if (
        provenance.digital_range_authority is not ProvenanceApplicability.NOT_APPLICABLE
        or provenance.offset_authority is not ProvenanceApplicability.NOT_APPLICABLE
    ):
        raise MetadataCanonicalizationError(
            "copy/skip provenance requires typed not-applicable authorities"
        )
    if provenance.offset_profile_identifier is not None or provenance.applied_offsets is not None:
        raise MetadataCanonicalizationError(
            "copy/skip provenance cannot carry an offset profile or applied subtraction"
        )
    if any(
        (
            provenance.effective_digital_maxima,
            provenance.offsets,
            provenance.normalization_divisors,
            provenance.normalization_factors,
            provenance.response_measurement_ids,
            provenance.response_correction_multipliers,
        )
    ):
        raise MetadataCanonicalizationError(
            "copy/skip provenance cannot carry numerical normalization facts"
        )
    if any(
        value is not ProvenanceApplicability.NOT_APPLICABLE
        for value in (
            provenance.response_transform_id,
            provenance.response_transform_semantic,
            provenance.response_transform_authority,
        )
    ):
        raise MetadataCanonicalizationError(
            "copy/skip provenance requires response transform as not applicable"
        )
    if (
        provenance.normalization_policy != "not_applied"
        or provenance.normalization_formula != "not_applied"
    ):
        raise MetadataCanonicalizationError(
            "copy/skip provenance must explicitly record normalization as not applied"
        )


# ==============================================================================
#                Constructing numerical and copied image metadata
# ==============================================================================


def build_unmixed_ome(
    input_ome: OME,
    *,
    image_index: int,
    library: SpectralLibraryMetadata,
    component_biomarkers: tuple[str | None, ...] | None,
    use_biomarker_channel_names: bool,
    output_dtype: str,
    provenance: ProcessingProvenance,
) -> OME:
    """

    Creates OME metadata for the fitted component images.

    Check the source geometry and selected reference against the processing record,
    then create one channel per component in the fitted order. Preserve source spatial
    sizes and pixel calibration. AF channels receive their AF identity, while explicit
    component biomarkers can supply display names when requested.

    Exposure planes belong to the original measured bands. Their values remain in the
    processing record while the new channels describe the separated components.

    Args:
        input_ome: Metadata for the single selected analytical image.
        image_index: Position zero of that selected image.
        library: Reference components, dyes, marker choices and reporting weights.
        component_biomarkers: Explicit output markers, or None to use the library's list.
        use_biomarker_channel_names: Whether assigned markers become display names.
        output_dtype: Float32 or float64 storage of the component image.
        provenance: Numerical settings, results and output facts to embed.

    Returns:
        A single-image OME model with component channels and saved processing information.

    Raises:
        MetadataCanonicalizationError: Source, output or reference information differs from the record.
    """

    validate_processing_provenance(provenance)
    _validate_spectral_library_provenance(library, provenance)
    _validate_source_ome_against_provenance(
        input_ome,
        image_index=image_index,
        provenance=provenance,
    )
    if output_dtype not in {"float32", "float64"}:
        raise MetadataCanonicalizationError("output dtype must be float32 or float64")
    if output_dtype != provenance.output_dtype:
        raise MetadataCanonicalizationError(
            "requested output dtype conflicts with processing provenance output dtype"
        )
    if not isinstance(use_biomarker_channel_names, bool):
        raise MetadataCanonicalizationError("biomarker channel-name choice must be boolean")
    requested_biomarkers = component_biomarkers or library.biomarkers
    if len(requested_biomarkers) != library.component_count:
        raise MetadataCanonicalizationError(
            "component biomarker count must match spectral-library columns"
        )
    biomarkers = tuple(
        None if index in library.autofluorescence_components else value
        for index, value in enumerate(requested_biomarkers)
    )
    if provenance.output_biomarkers and biomarkers != provenance.output_biomarkers:
        raise MetadataCanonicalizationError(
            "generated component biomarkers conflict with output biomarker provenance"
        )

    # Preserve spatial geometry and calibration while creating channels for the fitted
    # components.
    source_pixels = input_ome.images[image_index].pixels
    channels: list[Channel] = []
    for component_index, component in enumerate(library.component_names):
        biomarker = biomarkers[component_index]
        is_af = component_index in library.autofluorescence_components
        channels.append(
            Channel(
                id=f"Channel:0:{component_index}",
                name=(
                    biomarker
                    if use_biomarker_channel_names and biomarker is not None
                    else component
                ),
                fluor=None if is_af else library.fluorophores[component_index],
            )
        )
    pixel_type = "float" if output_dtype == "float32" else "double"
    pixels = Pixels(
        id="Pixels:0",
        dimension_order="XYCZT",
        type=pixel_type,
        size_x=source_pixels.size_x,
        size_y=source_pixels.size_y,
        size_z=1,
        size_c=library.component_count,
        size_t=1,
        physical_size_x=source_pixels.physical_size_x,
        physical_size_x_unit=source_pixels.physical_size_x_unit,
        physical_size_y=source_pixels.physical_size_y,
        physical_size_y_unit=source_pixels.physical_size_y_unit,
        physical_size_z=source_pixels.physical_size_z,
        physical_size_z_unit=source_pixels.physical_size_z_unit,
        channels=channels,
    )
    result = OME(
        images=[
            Image(
                id="Image:0",
                name=f"{input_ome.images[image_index].name or "Image"} — unmixed",
                description="Canonical spectrally unmixed component intensities",
                pixels=pixels,
            )
        ],
        structured_annotations=StructuredAnnotations(),
    )
    result = canonicalize_ome(
        result,
        assignments=tuple(
            BiomarkerAssignment(0, index, biomarker=value) for index, value in enumerate(biomarkers)
        ),
    )
    processing = MapAnnotation(
        id=_PROCESSING_ID,
        namespace=SPECTRAL_PROCESSING_NAMESPACE,
        value={
            "spectral_state": "unmixed",
            "is_unmixed_component": "true",
            "autofluorescence_subtracted": str(provenance.af_subtraction_performed).lower(),
            "component_order": json.dumps(library.component_names, separators=(",", ":")),
        },
    )
    provenance_annotation = MapAnnotation(
        id=_PROVENANCE_ID,
        namespace=SPECTRAL_PROVENANCE_NAMESPACE,
        value={"record": render_processing_provenance(provenance)},
    )
    result.structured_annotations.map_annotations.extend([processing, provenance_annotation])
    result.images[0].annotation_refs.extend(
        [AnnotationRef(id=_PROCESSING_ID), AnnotationRef(id=_PROVENANCE_ID)]
    )
    # The fitted channels differ from the acquired channel mapping. Retain the
    # original collection as source provenance, not as output channel metadata.
    from ..aligned_collection import COLLECTION_NAMESPACE
    source_refs = {ref.id for ref in input_ome.images[image_index].annotation_refs}
    source_collections = [] if input_ome.structured_annotations is None else [
        item for item in input_ome.structured_annotations.map_annotations
        if item.id in source_refs and item.namespace == COLLECTION_NAMESPACE]
    if len(source_collections) > 1:
        raise MetadataCanonicalizationError("Source has multiple aligned collection declarations")
    if source_collections:
        retained_collection = source_collections[0].model_copy(deep=True)
        retained_collection.id = "Annotation:SourceCollectionProvenance:0"
        retained_collection.namespace = "urn:mif-pipeline:source-collection-provenance:v1"
        result.structured_annotations.map_annotations.append(retained_collection)
        result.images[0].annotation_refs.append(AnnotationRef(id=retained_collection.id))
    from .canonical import (resolve_component_identity_inventory, apply_component_identity_inventory,
                            remap_nuclear_component, ComponentIdentityMode)
    reviewed = resolve_component_identity_inventory(input_ome, image_index)
    if reviewed is not None and reviewed.nuclear_stain_reviewed:
        result = apply_component_identity_inventory(
            result, 0, library.component_names,
            mode=ComponentIdentityMode.CHANNEL_FLUOROPHORES,
            promote_channel_fluorophores=False,
            decision_source="reviewed_output_components", component_biomarkers=biomarkers,
            nuclear_component_index=remap_nuclear_component(reviewed, library.component_names),
            nuclear_stain_reviewed=True,
        )
    if provenance.output_imaging_cycles:
        from .cycles import apply_output_cycles
        result = apply_output_cycles(result, provenance.output_imaging_cycles)
    return result


def build_metadata_copy_ome(
    input_ome: OME,
    provenance: ProcessingProvenance,
    *,
    use_biomarker_channel_names: bool,
) -> OME:
    """

    Updates metadata for prepared channels while retaining their order and pixel meaning.

    Check the source dtype, geometry and established copy decision. Optionally apply
    assigned biomarkers as display names. In the metadata copy, replace the preceding
    copy-step record with the current one while preserving the original unmixing history.

    Args:
        input_ome: Single-image metadata for the existing component image.
        provenance: Record describing the all-native-level copy operation.
        use_biomarker_channel_names: Whether marker assignments should name the displayed channels.

    Returns:
        OME metadata describing the copied image and its current copy operation.

    Raises:
        MetadataCanonicalizationError: Source state, order, dtype or copy settings are inconsistent.
    """

    validate_processing_provenance(provenance)
    if provenance.source_resolution_policy != "copy_all_native_levels":
        raise MetadataCanonicalizationError(
            "metadata-copy OME requires copy_all_native_levels provenance"
        )
    _validate_source_ome_against_provenance(
        input_ome,
        image_index=0,
        provenance=provenance,
    )
    pixels = input_ome.images[0].pixels
    pixel_dtype = _ome_pixel_dtype_name(pixels.type)
    if provenance.output_dtype != pixel_dtype:
        raise MetadataCanonicalizationError(
            "metadata-copy provenance output dtype conflicts with copied pixel dtype"
        )
    spectral_state = resolve_spectral_state(input_ome, 0)
    component_order = (spectral_state.component_order if spectral_state.state == "unmixed"
                       else resolve_prepared_channel_order(input_ome, 0))
    if component_order is None:
        raise MetadataCanonicalizationError(
            "metadata-copy OME requires confirmed prepared channels or unmixed components"
        )
    if component_order != provenance.output_component_order:
        raise MetadataCanonicalizationError(
            "metadata-copy provenance output component order conflicts with copied "
            "canonical component order; a spectral-state user override does not "
            "authorize component-order replacement"
        )
    if not isinstance(use_biomarker_channel_names, bool):
        raise MetadataCanonicalizationError("biomarker channel-name choice must be boolean")

    # Only the returned metadata copy receives display-name or copy-record
    # changes. Pixel values and their already established component order stay put.
    result = input_ome.model_copy(deep=True)
    if use_biomarker_channel_names:
        for row in resolve_biomarker_assignment_rows(result):
            if row.biomarker_assignment == "assigned":
                assert row.biomarker is not None
                result.images[row.image_index].pixels.channels[
                    row.channel_index
                ].name = row.biomarker
    if result.structured_annotations is None:
        result.structured_annotations = StructuredAnnotations()
    referenced = {item.id for item in result.images[0].annotation_refs}

    # Replace the preceding copy-step record while preserving the original unmixing history.
    retired_copy_ids = {
        annotation.id
        for annotation in result.structured_annotations.map_annotations
        if annotation.id in referenced
        and annotation.namespace == SPECTRAL_METADATA_COPY_PROVENANCE_NAMESPACE
    }
    if retired_copy_ids:
        result.structured_annotations.map_annotations = [
            annotation
            for annotation in result.structured_annotations.map_annotations
            if annotation.id not in retired_copy_ids
        ]
        result.images[0].annotation_refs = [
            reference
            for reference in result.images[0].annotation_refs
            if reference.id not in retired_copy_ids
        ]
    used_ids = {annotation.id for annotation in _all_annotations(result)}
    index = 0
    while True:
        annotation_id = f"Annotation:SpectralMetadataCopyProvenance:{index}"
        if annotation_id not in used_ids:
            break
        index += 1
    annotation = MapAnnotation(
        id=annotation_id,
        namespace=SPECTRAL_METADATA_COPY_PROVENANCE_NAMESPACE,
        value={"record": render_processing_provenance(provenance)},
    )
    result.structured_annotations.map_annotations.append(annotation)
    result.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
    return result


def _ome_pixel_dtype_name(pixel_type: object) -> str:
    """

    Converts supported OME pixel-type labels to their NumPy dtype names.

    Args:
        pixel_type: OME type such as "float", "double" or "uint16".

    Returns:
        "float32", "float64" or the corresponding supported integer dtype name.

    Raises:
        MetadataCanonicalizationError: The pixel type is unsupported.
    """

    value = getattr(pixel_type, "value", pixel_type)
    mapping = {
        "uint8": "uint8",
        "uint16": "uint16",
        "uint32": "uint32",
        "int8": "int8",
        "int16": "int16",
        "int32": "int32",
        "float": "float32",
        "double": "float64",
    }
    try:
        return mapping[str(value)]
    except KeyError as exc:
        raise MetadataCanonicalizationError(
            f"metadata-copy OME pixel dtype is unsupported: {value!r}"
        ) from exc


def _validate_spectral_library_provenance(
    library: SpectralLibraryMetadata,
    provenance: ProcessingProvenance,
) -> None:
    """

    Checks that output-channel metadata uses the same reference selected for numerical fitting.

    Compare the reference name/checksum, measurement order, component order and reporting
    weights. Detailed resolved references also compare component kinds, fluorophores and
    AF positions so fitted values receive the correct identities and scales.

    Args:
        library: Runtime reference used to construct output channels.
        provenance: Processing record identifying the fitted reference.

    Raises:
        MetadataCanonicalizationError: Reference fields are unavailable or disagree.
    """

    try:
        identifier = library.identifier
        sha256 = library.sha256
        band_names = library.band_names
        component_names = library.component_names
        column_sums = tuple(float(value) for value in library.column_sums)
    except (AttributeError, TypeError, ValueError) as exc:
        raise MetadataCanonicalizationError(
            "spectral library does not satisfy the public metadata contract"
        ) from exc
    if not isinstance(identifier, str) or not identifier.strip():
        raise MetadataCanonicalizationError("spectral library requires a stable identifier")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise MetadataCanonicalizationError(
            "spectral library requires a complete lowercase SHA-256"
        )
    if provenance.library_identifier != identifier:
        raise MetadataCanonicalizationError(
            "processing provenance library identifier conflicts with the spectral library"
        )
    if provenance.library_sha256 != sha256:
        raise MetadataCanonicalizationError(
            "processing provenance library SHA-256 conflicts with the spectral library"
        )
    if provenance.input_band_order != tuple(band_names):
        raise MetadataCanonicalizationError(
            "processing provenance input band order conflicts with the spectral library"
        )
    if provenance.output_component_order != tuple(component_names):
        raise MetadataCanonicalizationError(
            "processing provenance output component order conflicts with the spectral library"
        )
    if any(not math.isfinite(value) for value in column_sums) or (
        provenance.spectral_column_sums != column_sums
    ):
        raise MetadataCanonicalizationError(
            "processing provenance spectral column sums conflict with the spectral library"
        )
    authority = provenance.resolved_basis_authority
    if authority is not None:
        try:
            actual = (
                tuple(library.component_kinds),
                tuple(library.fluorophores),
                tuple(library.autofluorescence_components),
            )
        except (AttributeError, TypeError) as exc:
            raise MetadataCanonicalizationError(
                "resolved spectral basis lacks complete component semantics"
            ) from exc
        expected = (
            authority.component_kinds,
            authority.fluorophores,
            authority.autofluorescence_components,
        )
        if actual != expected:
            raise MetadataCanonicalizationError(
                "resolved basis component semantics conflict with runtime library"
            )


def _validate_source_ome_against_provenance(
    source: OME,
    *,
    image_index: int,
    provenance: ProcessingProvenance,
) -> None:
    """

    Compares source OME geometry and storage with the recorded full-resolution image level.

    Require one selected image, one Z plane and one time point. Check dimensions,
    channel count, axis order, samples per pixel and container type/width. This ensures
    the output metadata is derived from the same source layout used during processing.

    Args:
        source: Selected image metadata.
        image_index: Position zero of the selected image.
        provenance: Checked record containing the native level-zero description.

    Raises:
        MetadataCanonicalizationError: The selected image layout or storage differs.
    """

    if len(source.images) != 1 or image_index != 0:
        raise MetadataCanonicalizationError(
            "canonical source metadata requires exactly one image at index 0"
        )
    pixels = source.images[0].pixels
    if provenance.input_imaging_cycles:
        from .cycles import resolve_channel_cycles
        if resolve_channel_cycles(source, require_complete=True) != provenance.input_imaging_cycles:
            raise MetadataCanonicalizationError("Source imaging cycles differ from numerical processing provenance")
    level0 = provenance.native_pyramid_levels[0]
    if (
        pixels.size_z != 1
        or pixels.size_t != 1
        or (pixels.size_x, pixels.size_y, pixels.size_c)
        != (level0.size_x, level0.size_y, level0.channel_count)
        or len(pixels.channels) != level0.channel_count
    ):
        raise MetadataCanonicalizationError(
            "source pixel topology conflicts with provenance native level 0"
        )
    dimension_order = getattr(pixels.dimension_order, "value", pixels.dimension_order)
    if dimension_order != provenance.source_dimension_order:
        raise MetadataCanonicalizationError(
            "source OME dimension order conflicts with processing provenance"
        )
    if pixels.interleaved is True:
        raise MetadataCanonicalizationError("canonical source OME pixels must not be interleaved")
    effective_samples = tuple(
        1 if channel.samples_per_pixel is None else channel.samples_per_pixel
        for channel in pixels.channels
    )
    if any(value != level0.samples_per_pixel for value in effective_samples):
        raise MetadataCanonicalizationError(
            "source channel samples per pixel conflict with provenance native level 0"
        )
    source_dtype = _ome_pixel_dtype_name(pixels.type)
    source_bits = np.dtype(source_dtype).itemsize * 8
    if source_dtype != level0.dtype or source_bits != level0.container_bits:
        raise MetadataCanonicalizationError(
            "source pixel dtype/container bits conflict with provenance native level 0"
        )


# ==============================================================================
#                   Reading and confirming the spectral state
# ==============================================================================


def resolve_spectral_state(ome: OME, image_index: int) -> ResolvedSpectralState:
    """

    Reads the processing-state declaration linked to one image.

    Require one matching state record and agreement between its state and unmixed flag.
    An unmixed declaration also needs an explicit component order. AF-subtraction state
    is read separately and preserves unknown information.

    Args:
        ome: Metadata containing processing-state annotations.
        image_index: Image position whose references should be followed.

    Returns:
        A ResolvedSpectralState, with state="unknown" when no record is linked.

    Raises:
        MetadataConflictError: Multiple records or inconsistent state/component fields are present.
    """

    if image_index < 0 or image_index >= len(ome.images):
        raise MetadataCanonicalizationError(f"OME image index out of range: {image_index}")
    if ome.structured_annotations is None:
        return ResolvedSpectralState("unknown", "missing", None, None)
    referenced = {reference.id for reference in ome.images[image_index].annotation_refs}
    candidates = [
        annotation
        for annotation in ome.structured_annotations.map_annotations
        if annotation.namespace == SPECTRAL_PROCESSING_NAMESPACE and annotation.id in referenced
    ]
    if not candidates:
        return ResolvedSpectralState("unknown", "missing", None, None)
    if len(candidates) != 1:
        raise MetadataConflictError(
            f"image {image_index} has multiple canonical spectral-state annotations"
        )
    values = _strict_map(candidates[0])
    state = values.get("spectral_state")
    if state not in {"raw", "unmixed", "unknown"}:
        raise MetadataConflictError("canonical spectral_state is invalid")
    is_unmixed = values.get("is_unmixed_component")
    if is_unmixed not in {"true", "false"} or (is_unmixed == "true") != (state == "unmixed"):
        raise MetadataConflictError("canonical IsUnmixedComponent conflicts with spectral_state")
    af_value = values.get("autofluorescence_subtracted")
    if af_value not in {"true", "false", "unknown"}:
        raise MetadataConflictError("canonical autofluorescence_subtracted is invalid")
    component_order = _component_order(values.get("component_order"))
    if state == "unmixed" and component_order is None:
        raise MetadataConflictError("canonical unmixed metadata must provide component_order")
    return ResolvedSpectralState(
        state=state,
        source="canonical_spectral_processing",
        autofluorescence_subtracted=None if af_value == "unknown" else af_value == "true",
        component_order=component_order,
    )


def _resolve_source_spectral_state(
    ome: OME,
    image_index: int,
    *,
    spectral: ResolvedSpectralState,
    acquisition: UnmixingInputMetadata | None,
) -> tuple[str, str]:
    """Combine the selected image's processing and acquisition declarations.

    Callers supply the descriptors already resolved from this OME image. The retained
    imaging-settings inventory can establish state before numerical calibration is complete;
    it does not supply component order, AF subtraction or missing calibration values.
    Known declarations must agree. Unknown declarations supply no competing evidence.
    """
    from .calibration import retained_acquisition_state

    declarations = [(spectral.state, spectral.source)]
    if acquisition is not None:
        declarations.append((acquisition.spectral_state, acquisition.state_source))
    declarations.append(
        (retained_acquisition_state(ome, image_index), "retained imaging-settings inventory")
    )
    known = [(state, source) for state, source in declarations if state != "unknown"]
    if len({state for state, _ in known}) > 1:
        raise MetadataConflictError("The recorded spectral processing states disagree")
    return known[0] if known else ("unknown", "missing")


def confirmed_raw_band_metadata(ome: OME) -> OME:
    """Record an explicit raw-band review without assigning output components."""
    from .canonical import resolve_unmixing_input_metadata

    if not isinstance(ome, OME) or len(ome.images) != 1:
        raise MetadataCanonicalizationError("Raw-band review requires one selected image")
    spectral = resolve_spectral_state(ome, 0)
    acquisition = resolve_unmixing_input_metadata(ome, 0)
    state, _ = _resolve_source_spectral_state(ome, 0, spectral=spectral, acquisition=acquisition)
    if state == "unmixed":
        raise MetadataConflictError("An already-unmixed image cannot be declared raw measurement bands")
    result = ome.model_copy(deep=True)
    if spectral.state == "raw":
        return result
    if result.structured_annotations is None:
        result.structured_annotations = StructuredAnnotations()
    used = {annotation.id for annotation in _all_annotations(result)}
    index = 0
    while f"Annotation:ConfirmedRaw:{index}" in used:
        index += 1
    annotation = MapAnnotation(
        id=f"Annotation:ConfirmedRaw:{index}",
        namespace=SPECTRAL_PROCESSING_NAMESPACE,
        value={"spectral_state": "raw", "is_unmixed_component": "false",
               "autofluorescence_subtracted": "unknown", "declaration_source": "user_confirmation"},
    )
    result.structured_annotations.map_annotations.append(annotation)
    result.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
    return result


def resolve_prepared_channel_order(ome: OME, image_index: int) -> tuple[str, ...] | None:
    """Read a channel-bound copy decision without asserting prior spectral unmixing."""
    from .canonical import ComponentIdentityMode, resolve_component_identity_inventory

    image = ome.images[image_index]
    referenced = {reference.id for reference in image.annotation_refs}
    annotations = ome.structured_annotations
    records = [] if annotations is None else [
        annotation for annotation in annotations.map_annotations
        if annotation.id in referenced and annotation.namespace == PREPARED_CHANNELS_NAMESPACE
    ]
    if not records:
        return None
    if len(records) != 1:
        raise MetadataConflictError("Prepared-channel copy decision must be unique")
    values = _strict_map(records[0])
    inventory = resolve_component_identity_inventory(ome, image_index)
    order = _component_order(values.get("component_order"))
    ids = _component_order(values.get("channel_ids"))
    if (values.get("declaration_source") not in {"user_confirmed_prepared_channels", "user_confirmed_measurement_band_isolation"}
            or inventory is None or inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
            or order != inventory.component_names
            or ids != tuple(channel.id for channel in image.pixels.channels)
            or len(order or ()) != image.pixels.size_c):
        raise MetadataConflictError("Prepared-channel copy decision differs from reviewed channels")
    return order


def confirmed_unmixed_copy_metadata(ome: OME, *, user_declared: bool,
                                   measurement_band_components: tuple[int, ...] | None = None) -> OME:
    """

    Records a copy decision for reviewed channels ready for analysis.

    Check acquisition and processing-state records together. Known raw measurements
    block the ordinary previously-unmixed path. An explicit one-to-one measurement-band
    mapping instead records the researcher's isolation decision while retaining raw
    source state. When state is unknown, require explicit user confirmation and
    a reviewed component list. Unknown prior processing remains unknown: the separate
    prepared-channel record authorizes copying, not a claim of numerical unmixing.
    The historical function name is retained for callers using the existing No route.

    Args:
        ome: Metadata of the single selected image.
        user_declared: Whether the researcher confirmed channels ready for analysis.
        measurement_band_components: Reviewed component row for each isolated measurement band.

    Returns:
        OME metadata with the copy decision and existing scientific history.

    Raises:
        MetadataCanonicalizationError: Confirmation or required component information is missing.
        MetadataConflictError: Source metadata identifies raw measurements or conflicting states.
    """

    from .canonical import ComponentIdentityMode, resolve_unmixing_input_metadata, resolve_component_identity_inventory

    if type(user_declared) is not bool or not isinstance(ome, OME) or len(ome.images) != 1:
        raise MetadataCanonicalizationError(
            "A selected image and explicit state confirmation are required"
        )
    if measurement_band_components is not None:
        if not user_declared:
            raise MetadataCanonicalizationError("Confirm that the measurement bands isolate the fluorophore signals")
        from .measurement_band_copy import prepare_measurement_band_copy
        return prepare_measurement_band_copy(ome, measurement_band_components)
    spectral = resolve_spectral_state(ome, 0)
    acquisition = resolve_unmixing_input_metadata(ome, 0)
    state, _ = _resolve_source_spectral_state(
        ome, 0, spectral=spectral, acquisition=acquisition
    )
    if state == "raw":
        raise MetadataConflictError(
            "The source records raw spectral measurements; these cannot use the prepared-channel copy route"
        )
    prepared_order = resolve_prepared_channel_order(ome, 0)
    if state == "unknown" and not user_declared and prepared_order is None:
        raise MetadataCanonicalizationError(
            "Confirm that this image has separate channels ready for analysis before using the copy route"
        )
    result = ome.model_copy(deep=True)
    if spectral.state == "unmixed" or prepared_order is not None:
        return result
    inventory = resolve_component_identity_inventory(result, 0)
    if inventory is None or inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES:
        raise MetadataCanonicalizationError(
            "Confirm each existing channel in Channels and markers before importing a prepared image"
        )
    components = inventory.component_names
    if len(components) != result.images[0].pixels.size_c:
        raise MetadataCanonicalizationError(
            "The confirmed components do not match the current image channels"
        )
    if result.structured_annotations is None:
        result.structured_annotations = StructuredAnnotations()
    used = {annotation.id for annotation in _all_annotations(result)}
    if state == "unknown":
        index = 0
        while f"Annotation:PreparedChannels:{index}" in used:
            index += 1
        annotation = MapAnnotation(
            id=f"Annotation:PreparedChannels:{index}", namespace=PREPARED_CHANNELS_NAMESPACE,
            value={"declaration_source": "user_confirmed_prepared_channels",
                   "channel_ids": json.dumps(inventory.channel_ids, separators=(",", ":")),
                   "component_order": json.dumps(components, separators=(",", ":"))},
        )
        result.structured_annotations.map_annotations.append(annotation)
        result.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
        return result
    index = 0
    while f"Annotation:ConfirmedUnmixed:{index}" in used:
        index += 1
    annotation_id = f"Annotation:ConfirmedUnmixed:{index}"
    af = acquisition.autofluorescence_subtracted if acquisition else None
    annotation = MapAnnotation(
        id=annotation_id,
        namespace=SPECTRAL_PROCESSING_NAMESPACE,
        value={
            "spectral_state": "unmixed",
            "is_unmixed_component": "true",
            "autofluorescence_subtracted": "unknown" if af is None else str(af).lower(),
            "component_order": json.dumps(components, separators=(",", ":")),
            "declaration_source": "canonical_acquisition_metadata",
        },
    )
    result.structured_annotations.map_annotations.append(annotation)
    result.images[0].annotation_refs.append(AnnotationRef(id=annotation_id))
    return result


# ==============================================================================
#                  Writing and reading exact processing records
# ==============================================================================


def render_processing_provenance(
    provenance: ProcessingProvenance,
    path: str | Path | None = None,
) -> str:
    """

    Validates the processing record and serializes it in the format used for storage and comparison.

    Write sorted-key JSON with two-space indentation and a final newline. Optional empty
    reference/runtime fields are omitted consistently. When a path is supplied, write
    a temporary sibling file and replace the destination after the JSON is complete.

    Args:
        provenance: Processing record to validate and encode.
        path: Optional destination filename.

    Returns:
        The rendered JSON text.

    Raises:
        MetadataCanonicalizationError: Record consistency checks fail.
        OSError: The requested file cannot be written or replaced.
    """

    validate_processing_provenance(provenance)

    # Optional empty fields have one stored representation: omission.
    # The reader reconstructs them and checks the exact rendering again.
    mapping = asdict(provenance)
    if provenance.resolved_basis_authority is None:
        mapping.pop("resolved_basis_authority")
    if not provenance.output_biomarkers:
        mapping.pop("output_biomarkers")
    if provenance.spatial_registration is None:
        mapping.pop("spatial_registration")
    if not provenance.execution_runtime:
        mapping.pop("execution_runtime")
    if not provenance.input_imaging_cycles:
        mapping.pop("input_imaging_cycles")
        mapping.pop("output_imaging_cycles")
    rendered = json.dumps(mapping, sort_keys=True, indent=2) + "\n"
    if path is None:
        return rendered
    destination = Path(path)
    temporary = destination.with_name(f".{destination.name}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return rendered


def _read_processing_provenance(
    rendered: str | bytes,
) -> ProcessingProvenance:
    """

    Reconstructs a saved processing record and checks that its fields and JSON representation agree.

    Decode with duplicate-key detection, reconstruct tuple and nested record fields,
    then validate the selected numerical/copy/reuse path. Render the result again and
    compare it with the saved text. This ensures the record used in checksum comparisons
    has the expected field set, numerical declarations and serialization.

    Args:
        rendered: Processing JSON text or its UTF-8 bytes.

    Returns:
        The checked ProcessingProvenance, including its saved schema version.

    Raises:
        MetadataCanonicalizationError: Encoding, fields, numerical relationships or
            the saved representation are invalid.
    """

    if isinstance(rendered, bytes):
        try:
            rendered = rendered.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MetadataCanonicalizationError("processing provenance must be UTF-8 JSON") from exc
    if type(rendered) is not str:
        raise MetadataCanonicalizationError(
            "processing provenance must be JSON text or UTF-8 bytes"
        )

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        """

        Builds one decoded JSON object while rejecting repeated field names.

        Args:
            items: Key/value pairs supplied by the JSON decoder.

        Returns:
            The dictionary for this object.

        Raises:
            MetadataCanonicalizationError: A field name occurs more than once.
        """

        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise MetadataCanonicalizationError(
                    f"processing provenance contains duplicate field {key!r}"
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(rendered, object_pairs_hook=pairs)
        if type(decoded) is not dict:
            raise TypeError("top-level value is not an object")
        allowed = {item.name for item in fields(ProcessingProvenance)}
        optional = {"resolved_basis_authority", "output_biomarkers", "execution_runtime", "input_imaging_cycles", "output_imaging_cycles", "spatial_registration"}
        if type(decoded.get("schema_version")) is not int or decoded["schema_version"] not in {
            2,
            3,
            4,
        }:
            raise MetadataCanonicalizationError(
                "processing provenance requires exact supported schema version 2, 3 or 4"
            )
        _exact_keys(decoded, allowed - optional, allowed, "processing provenance")
        values = dict(decoded)
        if values["applied_offsets"] is not None:
            values["applied_offsets"] = _json_tuple(values["applied_offsets"], "applied_offsets")
        for name in (
            "input_band_order",
            "output_component_order",
            "exposure_seconds",
            "normalization_factors",
            "camera_bit_depth",
            "gain",
            "binning",
            "binning_area",
            "spectral_column_sums",
            "warnings",
            "effective_digital_maxima",
            "offsets",
            "normalization_divisors",
            "response_measurement_ids",
            "response_correction_multipliers",
        ):
            values[name] = _json_tuple(values[name], name)
        values["native_pyramid_levels"] = tuple(
            _load_exact_dataclass(item, SourcePyramidLevel, "native pyramid level")
            for item in _json_tuple(values["native_pyramid_levels"], "native_pyramid_levels")
        )
        digital_range = values["effective_digital_range"]
        if digital_range is not None:
            names = {item.name for item in fields(EffectiveDigitalRange)}
            mapping = dict(_exact_keys(digital_range, names, names, "effective digital range"))
            mapping["authority"] = DigitalRangeAuthority(mapping["authority"])
            values["effective_digital_range"] = EffectiveDigitalRange(**mapping)
        for name, enum_type in (
            ("digital_range_authority", DigitalRangeAuthority),
            ("offset_authority", OffsetAuthority),
        ):
            value = values[name]
            values[name] = (
                ProvenanceApplicability.NOT_APPLICABLE
                if value == ProvenanceApplicability.NOT_APPLICABLE.value
                else enum_type(value)
            )
        for name in (
            "compression",
            "processing_chunk_width",
            "processing_chunk_height",
            "writer_tile_width",
            "writer_tile_height",
            "output_dtype",
            "response_transform_id",
            "response_transform_semantic",
            "response_transform_authority",
        ):
            if values[name] == ProvenanceApplicability.NOT_APPLICABLE.value:
                values[name] = ProvenanceApplicability.NOT_APPLICABLE
        authority = values.get("resolved_basis_authority")
        if authority is not None:
            values["resolved_basis_authority"] = _load_resolved_basis_authority(
                authority,
            )
        if "output_biomarkers" in values:
            values["output_biomarkers"] = _json_tuple(
                values["output_biomarkers"], "output_biomarkers"
            )
        if "execution_runtime" in values:
            values["execution_runtime"] = tuple(
                _json_tuple(row, "execution runtime row")
                for row in _json_tuple(values["execution_runtime"], "execution_runtime")
            )
        for name in ("input_imaging_cycles", "output_imaging_cycles"):
            if name in values:
                values[name] = _json_tuple(values[name], name)
        record = ProcessingProvenance(**values)

        # Compare the reconstructed JSON with the saved representation used by checksum-based
        # records.
        if render_processing_provenance(record) != rendered:
            raise MetadataCanonicalizationError(
                "processing provenance JSON is not the canonical rendered record"
            )
        return record
    except MetadataCanonicalizationError:
        raise
    except (CanonicalOmeTiffError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MetadataCanonicalizationError(
            f"processing provenance JSON is invalid: {exc}"
        ) from exc


def loads_processing_provenance(rendered: str | bytes) -> ProcessingProvenance:
    """

    Reads and validates a saved processing record from JSON.

    Args:
        rendered: Canonical processing JSON text or bytes.

    Returns:
        The reconstructed ProcessingProvenance.
    """

    return _read_processing_provenance(rendered)


# ==============================================================================
#                  Finding an image's current processing record
# ==============================================================================


def _resolve_processing_provenance_rendered(ome: OME, image_index: int) -> str:
    """

    Finds the current processing JSON linked to an image.

    Prefer a linked metadata-copy record when present; otherwise use the linked
    unmixing record. Exactly one record must remain in the selected category. This
    allows an image copy to describe its latest write while retaining its original
    unmixing history.

    Args:
        ome: Metadata with image-linked processing annotations.
        image_index: Position of the image to inspect.

    Returns:
        The saved processing JSON text.

    Raises:
        MetadataCanonicalizationError: The selected record is absent, ambiguous or malformed.
    """

    if not isinstance(ome, OME) or not 0 <= image_index < len(ome.images):
        raise MetadataCanonicalizationError("processing provenance image index is invalid")
    referenced = {item.id for item in ome.images[image_index].annotation_refs}
    current_copy = tuple(
        item
        for item in _all_annotations(ome)
        if item.id in referenced
        and isinstance(item, MapAnnotation)
        and item.namespace == SPECTRAL_METADATA_COPY_PROVENANCE_NAMESPACE
    )
    predecessors = tuple(
        item
        for item in _all_annotations(ome)
        if item.id in referenced
        and isinstance(item, MapAnnotation)
        and item.namespace == SPECTRAL_PROVENANCE_NAMESPACE
    )

    # A copy has its own latest-operation record while retaining the
    # original unmixing record. Prefer the copy when both categories are linked.
    matches = current_copy if current_copy else predecessors
    if len(matches) != 1:
        raise MetadataCanonicalizationError(
            "canonical unmixed image requires exactly one processing provenance record"
        )
    mapping = _strict_map(matches[0])
    if set(mapping) != {"record"}:
        raise MetadataCanonicalizationError(
            "processing provenance annotation must contain only the record field"
        )
    rendered = mapping["record"]
    if type(rendered) is not str:
        raise MetadataCanonicalizationError(
            "processing provenance annotation record must be JSON text"
        )
    return rendered


def resolve_processing_provenance(ome: OME, image_index: int = 0) -> ProcessingProvenance:
    """

    Finds and validates the processing record linked to the selected image.

    Args:
        ome: Metadata containing processing annotations.
        image_index: Image position, defaulting to zero.

    Returns:
        The current ProcessingProvenance record.
    """

    return loads_processing_provenance(_resolve_processing_provenance_rendered(ome, image_index))


# ==============================================================================
#                   Decoding nested records and ordered names
# ==============================================================================


def _exact_keys(
    value: object,
    required: set[str],
    allowed: set[str],
    label: str,
) -> dict[str, object]:
    """

    Checks a decoded JSON object's required and permitted field names.

    Args:
        value: Dictionary to inspect.
        required: Fields that must be present.
        allowed: Complete set of permitted fields.
        label: Record name used in errors.

    Returns:
        The dictionary after field checks.

    Raises:
        TypeError, ValueError: The object type or field set is invalid.
    """

    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - allowed
    if missing or unknown:
        raise ValueError(
            f"{label} fields are not exact (missing={sorted(missing)!r}, "
            f"unknown={sorted(unknown)!r})"
        )
    return value


def _json_tuple(value: object, label: str) -> tuple[object, ...]:
    """

    Converts a decoded JSON list into the tuple used by a saved processing field.

    Args:
        value: List returned by JSON decoding.
        label: Field name used in errors.

    Returns:
        Its values as a tuple.

    Raises:
        TypeError: The value is not a JSON list.
    """

    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    return tuple(value)


def _load_exact_dataclass(value: object, cls: type, label: str):
    """

    Constructs a nested processing record after checking its complete field set.

    Args:
        value: Decoded dictionary for the nested record.
        cls: Dataclass defining the required fields.
        label: Record description used in errors.

    Returns:
        An instance of cls constructed from the decoded values.

    Raises:
        TypeError, ValueError: Fields or constructor values are invalid.
    """

    names = {item.name for item in fields(cls)}
    mapping = _exact_keys(value, names, names, label)
    return cls(**mapping)


def _load_resolved_basis_authority(value: object) -> ResolvedBasisAuthorityProvenance:
    """

    Decodes the detailed reference-selection record saved with processed image metadata.

    Check its required fields, convert measurement/component lists to tuples, and
    construct the record for subsequent comparison with target image and numerical settings.

    Args:
        value: Decoded reference-selection dictionary.

    Returns:
        A ResolvedBasisAuthorityProvenance describing the accepted reference.

    Raises:
        TypeError, ValueError: Record fields or list representations are invalid.
        MetadataCanonicalizationError: An unsupported non-null ssasu_application is supplied.
    """

    names = {item.name for item in fields(ResolvedBasisAuthorityProvenance)}
    mapping = dict(
        _exact_keys(
            value,
            names - {"ssasu_application"},
            names,
            "resolved basis authority",
        )
    )
    for name in (
        "measurement_ids",
        "band_names",
        "component_names",
        "component_kinds",
        "response_measurement_ids",
        "response_correction_multipliers",
        "fluorophores",
        "biomarkers",
        "autofluorescence_components",
        "conversion_factors",
        "source_artifact_sha256s",
    ):
        mapping[name] = _json_tuple(mapping[name], name)
    ssasu = mapping.get("ssasu_application")
    if ssasu is not None:
        raise MetadataCanonicalizationError(
            "This result used a retired background-estimation method. Choose a reference-based analysis."
        )
    return ResolvedBasisAuthorityProvenance(**mapping)


def _strict_map(annotation: MapAnnotation) -> dict[str, str]:
    """

    Reads an annotation's text fields while rejecting repeated keys.

    Args:
        annotation: Processing-state or processing-history MapAnnotation.

    Returns:
        Its named text values as a dictionary.

    Raises:
        MetadataConflictError: A key is repeated.
    """

    result: dict[str, str] = {}
    for entry in annotation.value.ms:
        if entry.k is None:
            continue
        if entry.k in result:
            raise MetadataConflictError(f"duplicate key {entry.k!r} in {annotation.id}")
        result[entry.k] = entry.value
    return result


def _component_order(value: str | None) -> tuple[str, ...] | None:
    """

    Reads component labels in stored-channel order, including repeated fluorophores.

    Channel IDs identify the separate measurements. A repeated dye label can occur
    in an already-separated image acquired over several staining cycles.

    Args:
        value: JSON component-list text, or None when absent.

    Returns:
        Ordered component names as a tuple, or None for an absent declaration.

    Raises:
        MetadataConflictError: JSON or component labels are invalid.
    """

    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MetadataConflictError("canonical component_order is invalid JSON") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) or not item.strip() for item in decoded)
    ):
        raise MetadataConflictError(
            "canonical component_order must be a non-empty string list"
        )
    return tuple(decoded)
