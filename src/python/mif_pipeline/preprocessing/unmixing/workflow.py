"""

Processes the selected image into a standardized component image using its reviewed metadata and spectral reference.

A raw vendor image enters through the retained source inspection and completed
OME metadata. The workflow reopens that selected vendor series, reads its
full-resolution regions, applies acquisition correction and fits the component
coefficients. Independent bounded shard tasks calculate the component intensities directly
in the final Zarr layout, then construct the intensity pyramid from those values.

Metadata review therefore precedes pixel conversion. The workflow does not need
a complete standardized raw TIFF before it can unmix vendor measurements. It
writes the standardized image from the actual unmixing results, removing that
intermediate raw-image write/read cycle.

Already-unmixed images use their explicit component order and the pixel-copy or
reuse path. The recorded operation distinguishes numerical fitting, copying all
stored levels and reusing an accepted output. Source images and earlier accepted
results remain available while a new output generation is prepared.

See "docs/stages/images/processing.md" for regional reading and writing,
and "docs/stages/unmixing/index.md" for the numerical algorithms.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import hashlib
import json
import os
import platform
import tempfile
from typing import Any, Callable

import numpy as np
from ome_types import OME, to_xml
import zarr

from mif_pipeline.io import (
    BackendVersions,
    CanonicalIngestionError,
    ComponentIdentityMode,
    CanonicalOmeTiffError,
    CanonicalOmeTiffHeader,
    BiomarkerAssignment,
    EffectiveDigitalRange,
    InspectedSourceAuthority,
    MetadataCanonicalizationError,
    MetadataConflictError,
    PlaneIndex,
    ProcessingProvenance,
    ProvenanceApplicability,
    ResolvedBasisAuthorityProvenance,
    SourceFactsError,
    SourceImageFacts,
    SourcePyramidLevel,
    build_metadata_copy_ome,
    build_unmixed_ome,
    canonicalize_ome,
    resolve_component_identity_inventory,
    get_default_backend,
    loads_processing_provenance,
    open_reviewed_source,
    render_processing_provenance,
    resolve_channel_metadata,
    resolve_biomarker_assignment_rows,
    resolve_spectral_state,
    resolve_processing_provenance,
    resolve_unmixing_input_metadata,
    resolve_acquisition_digital_range,
    resolve_source_image_facts,
    validate_canonical_ome_tiff_header,
)
from mif_pipeline.io.metadata.spectral import _resolve_source_spectral_state, resolve_prepared_channel_order
from mif_pipeline.io.metadata.canonical import apply_component_identity_inventory
from mif_pipeline.io.writers.processed_image import (copy_source_pyramid, finish_processed_image)
from .library import SpectralLibrary, SpectralLibraryError, load_spectral_library
from .application import ResolvedBasisApplication, ResolvedBasisTargetBinding
from .output_publication import (
    PublishedUnmixingOutput,
    authority_pointer_path,
    load_unmixing_output_publication,
    publish_unmixing_output,
)
from .normalization import CanonicalOffsetExposureNormalization
from .solvers import BatchedNnlsSolver, ClippedLeastSquaresSolver
from .engine import (
    ProgressEvent,
    RegionExecutionPolicy,
    process_regions_to_zarr,
)
from .validation import (
    SolverKind,
    SpectralState,
    UnmixingConfig,
    UnmixingValidationError,
    ValidatedUnmixingInput,
    validate_unmixing_input,
)

# ==============================================================================
#                                    Workflow
# ==============================================================================


class UnmixingWorkflowError(RuntimeError):
    """The canonical preprocessing stage cannot complete deterministically."""


@dataclass(frozen=True, slots=True)
class UnmixingResult:
    status: str
    input_path: Path
    output_path: Path
    provenance_path: Path
    regions_processed: int
    pyramid_levels: int
    warnings: tuple[str, ...]
    authority_pointer_path: Path | None = None
    publication_manifest_sha256: str | None = None
    publication_generation_id: str | None = None
    publication_request_sha256: str | None = None
    publication_reused: bool = False
    publication_warnings: tuple[str, ...] = ()
    clipping_evidence_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _PyramidCopySummary:
    region_count: int
    level_count: int
    dtype: np.dtype


@contextmanager
def _owned_spectral_solver(solver, expected_runtime=None, calculation_precision=None):
    """

    Keeps one selected numerical solver active for the regional pass and releases its worker before publication.

    Capture the numerical-runtime selection before yielding the solver, then check
    that it remains the same after the regional work. Close any owned numerical
    worker on completion or failure. If cleanup also fails during another exception,
    retain the original error and add the cleanup information to it.

    Args:
        solver: Prepared spectral solver for the accepted reference and numerical settings.
        expected_runtime: Optional previously recorded numerical-runtime selection;
            otherwise capture the current selection on entry.
        calculation_precision: Precision saved with this unmixing request; when
            absent, retain the earlier computing preference.

    Yields:
        Solver used by the regional processing pass.

    Raises:
        ValueError: Runtime preferences change before or during numerical processing.
    """

    primary = None
    captured_runtime = _region_execution_runtime(calculation_precision) if expected_runtime is None else expected_runtime
    try:
        if _region_execution_runtime(calculation_precision) != captured_runtime:
            raise ValueError("The numerical runtime preference changed before execution")
        yield solver
        if _region_execution_runtime(calculation_precision) != captured_runtime:
            raise ValueError("The numerical runtime preference changed during execution")
    except BaseException as error:
        primary = error
        raise
    finally:
        import logging

        logging.getLogger(__name__).info(
            "Spectral execution: device=%s precision=%s verified=%s refined=%s",
            getattr(solver, "execution_device", "cpu"),
            getattr(solver, "execution_precision_policy", "float64"),
            getattr(solver, "verified_pixels", 0),
            getattr(solver, "refined_pixels", 0),
        )
        close = getattr(solver, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                if primary is None:
                    raise
                if hasattr(primary, "add_note"):
                    primary.add_note("Numerical worker cleanup also failed: " + str(error))


def _source_sha256(facts: CanonicalOmeTiffHeader | SourceImageFacts) -> str:
    return facts.identity.file_sha256 if isinstance(facts, SourceImageFacts) else facts.sha256


def _source_native_levels(
    facts: CanonicalOmeTiffHeader | SourceImageFacts,
) -> tuple[SourcePyramidLevel, ...]:
    return tuple(
        SourcePyramidLevel(
            level.level_index,
            level.size_x,
            level.size_y,
            level.channel_count,
            level.dtype,
            level.container_bits,
            level.samples_per_pixel,
        )
        for level in facts.native_pyramid_levels
    )


def run_unmixing(
    input_path: str | Path,
    output_path: str | Path,
    library_path: str | Path | None,
    config: UnmixingConfig,
    *,
    backend: Any | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
    canonical_metadata_override: OME | None = None,
    replace_channel_names_with_biomarkers: bool,
    basis_application: ResolvedBasisApplication | None = None,
    reviewed_source_authority: InspectedSourceAuthority | None = None,
) -> UnmixingResult:
    """

    Unmixes the selected source image and prepares its standardized component output.

    When reviewed_source_authority is supplied, reopen the original vendor image
    with the completed OME metadata. The image's selected vendor series is presented
    as series zero to the regional engine. Raw pixels are read only when their
    rectangles are prepared for normalization and fitting.

    The accepted basis supplies the ordered reference spectra and measurement
    correction. After fitting, the engine applies the component reporting weights
    once, stages the component array, and derives its lower-resolution pyramid.
    The publisher selects that completed immutable processed-image directory. Existing standardized images can enter through the direct TIFF-reader
    path; already-unmixed inputs follow the corresponding copy or reuse decision.

    Args:
        input_path: Original vendor image or supported standardized OME-TIFF.
        output_path: Planned .ome.zarr destination for the accepted processed generation.
        library_path: Optional library used to check already-unmixed component identity.
            Raw numerical processing uses basis_application.
        config: Selected solver, state choice, processing chunk, output dtype,
            convergence settings.
        backend: Optional selected Bio-Formats backend for source and output checks.
        progress: Optional callback receiving completed region and pyramid progress.
        canonical_metadata_override: Reviewed OME metadata containing accepted channel
            and acquisition choices; required with a retained vendor-source inspection.
        replace_channel_names_with_biomarkers: Whether assigned biomarkers should be
            used as output display names.
        basis_application: Accepted target-specific reference matrix, correction and
            component reporting factors for numerical unmixing.
        reviewed_source_authority: Retained vendor-image inspection, including selected
            source series, file identity and original reader metadata.

    Returns:
        UnmixingResult identifying the numerical output, copied output or accepted reuse,
        together with its processing record and recorded warnings.

    Raises:
        UnmixingWorkflowError: Source selection, reviewed metadata, destination, basis
            choice or a recorded processing relationship is invalid.
    """

    source_path = Path(input_path)
    _validate_basis_source_choice(library_path, basis_application)
    destination = Path(output_path)
    if not destination.name.casefold().endswith(".ome.zarr"):
        raise UnmixingWorkflowError("Output image must end in .ome.zarr")
    direct_source = reviewed_source_authority is not None
    if direct_source:
        if type(reviewed_source_authority) is not InspectedSourceAuthority:
            raise UnmixingWorkflowError("reviewed source authority has the wrong type")
        if not isinstance(canonical_metadata_override, OME):
            raise UnmixingWorkflowError("reviewed source requires accepted canonical OME metadata")
        if source_path.resolve() != reviewed_source_authority.source_identity.resolved_path:
            raise UnmixingWorkflowError("input path differs from inspected source identity")
    elif not _is_processed_or_tiff_source(source_path):
        raise UnmixingWorkflowError(f"input must be a canonical OME-TIFF, not {source_path.name!r}")
    if source_path.resolve() == destination.resolve():
        raise UnmixingWorkflowError("spectral unmixing cannot modify its input in place")
    from mif_pipeline.io.aligned_collection import validate_collection_output
    validate_collection_output(source_path, destination)
    resolved_name_choice = _resolve_channel_name_choice(
        replace_channel_names_with_biomarkers,
    )
    catalog = load_spectral_library(library_path) if library_path is not None else None
    selected_backend = backend or get_default_backend()
    source_context = (
        open_reviewed_source(
            reviewed_source_authority,
            canonical_metadata_override,
            backend=selected_backend,
        )
        if direct_source
        else selected_backend.open(source_path)
    )
    try:
        return _run_unmixing_open_source(
            source_context,
            source_path,
            destination,
            catalog,
            config,
            selected_backend=selected_backend,
            progress=progress,
            canonical_metadata_override=canonical_metadata_override,
            resolved_name_choice=resolved_name_choice,
            basis_application=basis_application,
            direct_source=direct_source,
        )
    except (CanonicalIngestionError, SourceFactsError) as exc:
        raise UnmixingWorkflowError(f"reviewed source admission failed: {exc}") from exc


def _run_unmixing_open_source(
    source_context: Any,
    source_path: Path,
    destination: Path,
    catalog: SpectralLibrary | None,
    config: UnmixingConfig,
    *,
    selected_backend: Any,
    progress: Callable[[ProgressEvent], None] | None,
    canonical_metadata_override: OME | None,
    resolved_name_choice: bool,
    basis_application: ResolvedBasisApplication | None,
    direct_source: bool,
) -> UnmixingResult:
    """

    Executes the numerical, copy or reuse path while keeping the source reader open for required pixel reads.

    Resolve the source's spectral state and selected component basis. A usable
    existing output can be verified and reused. An already-unmixed source keeps its
    component interpretation and either copies its native levels or returns the
    appropriate unchanged input result.

    For raw measurements, resolve the effective digital range and build the per-band
    normalization factors. Check the target image against the accepted reference,
    reserve temporary storage and run the bounded regional engine. Its output
    contains component intensities with the reporting conversion already applied.
    An unconverged NNLS result stops final publication.

    After regional staging, close the vendor reader and numerical worker. Construct
    component-channel OME metadata, write the staged arrays as a pyramidal TIFF and
    save the associated processing record. Only the completed, checked output
    becomes the current accepted generation.

    Args:
        source_context: Context manager opening the reviewed vendor source or standardized TIFF.
        source_path: Original image location.
        destination: Checked output OME-Zarr location.
        catalog: Optional loaded library for component checks on the copy/reuse path.
        config: Selected unmixing and writing settings.
        selected_backend: Image backend used for output reopening and verification.
        progress: Optional completed-region/pyramid callback.
        canonical_metadata_override: Reviewed metadata for the selected analytical image.
        resolved_name_choice: Explicit biomarker-display-name choice.
        basis_application: Accepted numerical reference for the raw-image path.
        direct_source: Whether source_context wraps the original selected vendor image.

    Returns:
        UnmixingResult describing the completed operation and its recorded numerical/writing facts.

    Raises:
        UnmixingWorkflowError: Source state, quantitative settings, target/reference
            comparison, existing output or numerical completion fails a required check.
    """

    with source_context as source:
        if direct_source:
            source_ome = source.metadata
            ome = source_ome
            header = resolve_source_image_facts(source)
        else:
            try:
                header = validate_canonical_ome_tiff_header(source_path, source)
            except CanonicalOmeTiffError as exc:
                raise UnmixingWorkflowError(
                    f"input is not a valid canonical OME-TIFF: {exc}"
                ) from exc
            source_ome = source.metadata
            _validate_source_topology(source, source_ome)
            ome = _validated_canonical_metadata_override(
                source_ome,
                canonical_metadata_override,
                replace_channel_names_with_biomarkers=resolved_name_choice,
            )
        acquisition = resolve_unmixing_input_metadata(ome, 0)
        canonical_state = resolve_spectral_state(ome, 0)
        prepared_order = resolve_prepared_channel_order(ome, 0)
        try:
            observed, _ = _resolve_source_spectral_state(
                ome, 0, spectral=canonical_state, acquisition=acquisition
            )
        except MetadataConflictError as exc:
            raise UnmixingWorkflowError(str(exc)) from exc
        if basis_application is not None:
            library = basis_application
        elif catalog is None:
            isolated = config.spectral_state_override is SpectralState.ISOLATED
            if (
                config.spectral_state_override is not None
                and not isolated
                and observed != "unknown"
                and config.spectral_state_override.value != observed
            ):
                raise UnmixingWorkflowError(
                    "The selected spectral state conflicts with the recorded image state"
                )
            if not isolated and observed != "unmixed" and not (
                observed == "unknown" and (
                    config.spectral_state_override is SpectralState.UNMIXED
                    or prepared_order is not None
                )
            ):
                raise UnmixingWorkflowError(
                    "Raw or unknown-state images require a reviewed spectral basis; they cannot use the already-unmixed copy route"
                )
            from .copy_semantics import copy_component_inventory
            from mif_pipeline.io import confirmed_unmixed_copy_metadata

            try:
                ome = confirmed_unmixed_copy_metadata(
                    ome, user_declared=config.spectral_state_override in {SpectralState.UNMIXED, SpectralState.ISOLATED},
                    measurement_band_components=config.measurement_band_components if isolated else None,
                )
                canonical_state = resolve_spectral_state(ome, 0)
                prepared_order = resolve_prepared_channel_order(ome, 0)
                library = copy_component_inventory(ome, canonical_state)
            except (ValueError, MetadataCanonicalizationError) as exc:
                raise UnmixingWorkflowError(str(exc)) from exc
        else:
            assert catalog is not None
            try:
                library = _select_spectral_basis(
                    ome,
                    catalog,
                    observed_state=observed,
                    canonical_component_order=canonical_state.component_order,
                    configured_state=config.spectral_state_override,
                )
            except SpectralLibraryError as exc:
                raise UnmixingWorkflowError(str(exc)) from exc
        validation_config = config
        isolated = config.spectral_state_override is SpectralState.ISOLATED
        if prepared_order is not None and observed == "unknown" and not isolated:
            if config.spectral_state_override not in {None, SpectralState.UNMIXED}:
                raise UnmixingWorkflowError("Prepared channels cannot be processed as raw spectral bands")
            # Legacy UNMIXED is a routing token here; it does not establish source history.
            validation_config = replace(config, spectral_state_override=SpectralState.UNMIXED)
        metadata_state_source = False
        if canonical_state.state != "unknown":
            known = SpectralState(canonical_state.state)
            if (
                known == SpectralState.UNMIXED
                and canonical_state.component_order != library.component_names
            ):
                raise UnmixingWorkflowError(
                    "canonical unmixed component order conflicts with the spectral library"
                )
        resolved_state = canonical_state.state if canonical_state.state != "unknown" else observed
        if resolved_state != "unknown" and not isolated:
            known = SpectralState(resolved_state)
            if config.spectral_state_override not in {None, known}:
                raise UnmixingWorkflowError(
                    f"spectral state override {config.spectral_state_override.value} "
                    f"conflicts with metadata {known.value}"
                )
            validation_config = replace(config, spectral_state_override=known)
            metadata_state_source = True
        try:
            validated = validate_unmixing_input(
                ome,
                0,
                acquisition,
                library,
                validation_config,
                canonical_component_order=canonical_state.component_order or prepared_order,
            )
        except UnmixingValidationError as exc:
            raise UnmixingWorkflowError(str(exc)) from exc
        if metadata_state_source:
            declared_copy = library.identifier is None and observed == "unknown"
            validated = replace(
                validated, state_source="user_override" if declared_copy else "metadata"
            )
        if prepared_order is not None and observed == "unknown" and not isolated:
            validated = replace(validated, state_source="user_confirmed_prepared_channels")

        provenance_path = _provenance_path(destination)
        if validated.skip_unmixing:
            if basis_application is not None:
                raise UnmixingWorkflowError(
                    "a resolved basis application cannot be used for an already-unmixed input"
                )
            metadata_only_copy = (
                direct_source
                or library.identifier is None
                or _biomarker_review_differs(source_ome, ome)
                or (resolved_name_choice and _channel_name_replacement_changes(ome))
            )
            if metadata_only_copy:
                request_sha256 = _unmixing_request_sha256(
                    header=header,
                    ome=ome,
                    config=config,
                    channel_name_replacement=resolved_name_choice,
                    library=library,
                    basis_application=None,
                    source_resolution_policy="copy_all_native_levels",
                    backend_versions=_source_backend_versions(source),
                    writer_runtime=_writer_runtime(config),
                    solver_policy_version=_solver_policy_version(config),
                    normalization_policy_version=_normalization_policy_version(),
                )
                reused = _matching_existing_publication(
                    destination=destination,
                    provenance_path=provenance_path,
                    request_sha256=request_sha256,
                    source_path=source_path,
                    backend=selected_backend,
                    header=header,
                    ome=ome,
                    config=config,
                    library=library,
                    basis_application=None,
                    validated=validated,
                    factors=None,
                    digital_range=None,
                    warnings=(
                        "spectral unmixing skipped; metadata-only canonical copy "
                        "applied reviewed biomarker assignments",
                    ),
                    channel_name_replacement=resolved_name_choice,
                    source_resolution_policy="copy_all_native_levels",
                )
                if reused is not None:
                    return _reused_unmixing_result(
                        publication=reused,
                        status="skipped_already_unmixed",
                        source_path=source_path,
                        pyramid_levels=len(header.native_pyramid_levels),
                        warnings=(
                            "spectral unmixing skipped; metadata-only canonical copy "
                            "applied reviewed biomarker assignments",
                        ),
                    )
                _reject_existing_destination_without_overwrite(destination, config)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging_state: list[PublishedUnmixingOutput] = []
                cleanup_warnings: list[str] = []
                with _workflow_staging_directory(
                    prefix=f".{destination.name}.staging-",
                    dir=destination.parent,
                    request_sha256=request_sha256,
                    committed=staging_state,
                    warnings=cleanup_warnings,
                ) as temporary_directory:
                    store_path = Path(temporary_directory) / "pyramid.zarr"
                    summary = _copy_source_pyramid_to_zarr(
                        source,
                        ome,
                        store_path,
                        chunk_size=config.chunk_size,
                        request_sha256=request_sha256,
                        progress=progress,
                    )
                    if direct_source:
                        source.close()
                    record = _build_provenance(
                        source_path,
                        destination,
                        library,
                        validated,
                        config,
                        factors=None,
                        diagnostics=None,
                        pyramid_levels=summary.level_count,
                        source_resolution_policy="copy_all_native_levels",
                        output_pyramid_policy="pixel_identical_source_pyramid",
                        warnings=(
                            "spectral unmixing skipped; metadata-only canonical copy "
                            "applied reviewed biomarker assignments",
                        ),
                        channel_name_replacement=resolved_name_choice,
                        output_dtype=summary.dtype.name,
                        header=header,
                        digital_range=None,
                    )
                    group = zarr.open_group(store_path, mode="r")
                    levels = tuple(group[str(index)] for index in range(summary.level_count))
                    output_ome = build_metadata_copy_ome(
                        ome,
                        record,
                        use_biomarker_channel_names=resolved_name_choice,
                    )
                    try:
                        publication = _publish_staged_output(
                            store_path=store_path,
                            levels=levels,
                            output_ome=output_ome,
                            record=record,
                            request_sha256=request_sha256,
                            destination=destination,
                            provenance_path=provenance_path,
                            config=config,
                        )
                        staging_state.append(publication)
                    finally:
                        _close_zarr_group(group)
                if cleanup_warnings:
                    publication = replace(
                        publication,
                        warnings=publication.warnings + tuple(cleanup_warnings),
                    )
                return UnmixingResult(
                    status="skipped_already_unmixed",
                    input_path=source_path,
                    output_path=publication.image_path,
                    provenance_path=publication.provenance_path,
                    regions_processed=summary.region_count,
                    pyramid_levels=summary.level_count,
                    warnings=record.warnings + publication.warnings,
                    authority_pointer_path=publication.pointer_path,
                    publication_manifest_sha256=(publication.manifest.manifest_content_sha256),
                    publication_generation_id=publication.pointer.generation_id,
                    publication_request_sha256=publication.pointer.request_sha256,
                    publication_reused=publication.reused,
                    publication_warnings=publication.warnings,
                )
            _reject_existing_destination_without_overwrite(destination, config)
            _refuse_occupied_no_read_publication_namespace(
                destination,
                provenance_path,
            )
            record = _build_provenance(
                source_path,
                source_path,
                library,
                validated,
                config,
                factors=None,
                diagnostics=None,
                pyramid_levels=0,
                source_resolution_policy="not_read",
                output_pyramid_policy="reuse_existing_source_pyramid",
                warnings=("spectral unmixing skipped: canonical metadata reports unmixed",),
                channel_name_replacement=resolved_name_choice,
                header=header,
                digital_range=None,
            )
            render_processing_provenance(record, provenance_path)
            return UnmixingResult(
                status="skipped_already_unmixed",
                input_path=source_path,
                output_path=source_path,
                provenance_path=provenance_path,
                regions_processed=0,
                pyramid_levels=0,
                warnings=record.warnings,
            )

        if basis_application is None:
            raise UnmixingWorkflowError(
                "Raw images need a reviewed spectral library. "
                "Complete reference setup before unmixing."
            )
        assert validated.acquisition is not None
        target_binding = _resolved_basis_target_binding(
            header,
            ome,
            validated,
            basis_application,
        )
        try:
            digital_range = resolve_acquisition_digital_range(header, validated.acquisition)
        except CanonicalOmeTiffError as exc:
            raise UnmixingWorkflowError(
                f"raw input has no validated effective digital range: {exc}"
            ) from exc
        execution_runtime = _region_execution_runtime(config.calculation_precision)
        request_sha256 = _unmixing_request_sha256(
            header=header,
            ome=ome,
            config=config,
            channel_name_replacement=resolved_name_choice,
            library=library,
            basis_application=basis_application,
            source_resolution_policy="level0_only",
            backend_versions=_source_backend_versions(source),
            writer_runtime=_writer_runtime(config),
            solver_policy_version=_solver_policy_version(config),
            normalization_policy_version=_normalization_policy_version(validated.acquisition),
            digital_range=digital_range,
        )
        warning_items: list[str] = []
        if (validated.acquisition.autofluorescence_subtracted is None
                and library.autofluorescence_components):
            warning_items.append(
                "source AF-subtraction state is unknown; AF was retained as a solved component"
            )
        warnings = tuple(warning_items)
        component_biomarkers = _component_biomarkers(ome, library)
        factors = CanonicalOffsetExposureNormalization().build_factors(
            validated.acquisition,
            digital_range,
            response_transform=basis_application.response_transform,
        )
        reused = _matching_existing_publication(
            destination=destination,
            provenance_path=provenance_path,
            request_sha256=request_sha256,
            source_path=source_path,
            backend=selected_backend,
            header=header,
            ome=ome,
            config=config,
            library=library,
            basis_application=basis_application,
            validated=validated,
            factors=factors,
            digital_range=digital_range,
            warnings=warnings,
            output_biomarkers=component_biomarkers,
            channel_name_replacement=resolved_name_choice,
            source_resolution_policy="level0_only",
        )
        if reused is not None:
            return _reused_unmixing_result(
                publication=reused,
                status="processed",
                source_path=source_path,
                pyramid_levels=_recursive_pyramid_level_count(
                    header.size_x,
                    header.size_y,
                ),
                warnings=warnings,
            )
        _reject_existing_destination_without_overwrite(destination, config)
        from mif_pipeline._capacity import require_processing_space

        require_processing_space(
            destination.parent,
            (validated.size_x, validated.size_y, library.component_count),
            output_bytes=np.dtype(validated.config.output_dtype).itemsize,
        )
        from .tensor_solver import create_spectral_solver
        from .cyclic import CycleSolver, cycle_solve_groups
        cycle_groups = cycle_solve_groups(
            validated.imaging_cycles or None, library.component_names, library.matrix
        )

        def prepare_solver(matrix):
            return create_spectral_solver(
                matrix, config.solver.value, tolerance=config.nnls_tolerance,
                max_iterations=config.nnls_max_iterations,
                precision_policy=config.calculation_precision,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_state = []
        cleanup_warnings = []
        with _workflow_staging_directory(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
            request_sha256=request_sha256,
            committed=staging_state,
            warnings=cleanup_warnings,
        ) as temporary_directory:
            store_path = Path(temporary_directory) / "pyramid.zarr"
            with _owned_spectral_solver(
                CycleSolver(library.matrix, cycle_groups, prepare_solver)
                if len(cycle_groups) > 1 else prepare_solver(library.matrix),
                expected_runtime=execution_runtime,
                calculation_precision=config.calculation_precision,
            ) as solver:
                summary = process_regions_to_zarr(
                    source,
                    validated,
                    library,
                    factors,
                    solver,
                    store_path,
                    progress=progress,
                    target_binding=target_binding,
                    request_sha256=request_sha256,
                )
                if config.solver == SolverKind.NNLS and not summary.diagnostics.converged:
                    raise UnmixingWorkflowError(
                        "NNLS did not converge for every region; no output was finalized"
                    )
            if direct_source:
                source.close()
            clipping_evidence_path = None
            clipping_evidence_bytes = None
            record = _build_provenance(
                source_path,
                destination,
                library,
                validated,
                config,
                factors=factors,
                diagnostics=summary.diagnostics,
                pyramid_levels=summary.level_count,
                source_resolution_policy="level0_only",
                output_pyramid_policy=("recursive_2x_area_average_from_processed_level0"),
                warnings=warnings,
                channel_name_replacement=resolved_name_choice,
                header=header,
                digital_range=digital_range,
                resolved_basis_authority=_resolved_basis_authority_provenance(
                    basis_application,
                    clipping_evidence_path=clipping_evidence_path,
                    clipping_evidence_bytes=clipping_evidence_bytes,
                ),
                output_biomarkers=component_biomarkers,
            )
            output_ome = build_unmixed_ome(
                ome,
                image_index=0,
                library=library,
                component_biomarkers=component_biomarkers,
                use_biomarker_channel_names=resolved_name_choice,
                output_dtype=config.output_dtype,
                provenance=record,
            )
            group = zarr.open_group(store_path, mode="r")
            levels = tuple(group[str(index)] for index in range(summary.level_count))
            try:
                if _region_execution_runtime(config.calculation_precision) != execution_runtime:
                    raise ValueError("The numerical runtime preference changed before publication")
                publication = _publish_staged_output(
                    store_path=store_path,
                    levels=levels,
                    output_ome=output_ome,
                    record=record,
                    request_sha256=request_sha256,
                    destination=destination,
                    provenance_path=provenance_path,
                    config=config,
                    clipping_evidence_path=clipping_evidence_path,
                    clipping_evidence_bytes=clipping_evidence_bytes,
                )
                staging_state.append(publication)
            finally:
                _close_zarr_group(group)
        if cleanup_warnings:
            publication = replace(
                publication,
                warnings=publication.warnings + tuple(cleanup_warnings),
            )
    return UnmixingResult(
        status="processed",
        input_path=source_path,
        output_path=publication.image_path,
        provenance_path=publication.provenance_path,
        regions_processed=summary.region_count,
        pyramid_levels=summary.level_count,
        warnings=record.warnings + publication.warnings,
        authority_pointer_path=publication.pointer_path,
        publication_manifest_sha256=publication.manifest.manifest_content_sha256,
        publication_generation_id=publication.pointer.generation_id,
        publication_request_sha256=publication.pointer.request_sha256,
        publication_reused=publication.reused,
        publication_warnings=publication.warnings,
        clipping_evidence_path=publication.clipping_evidence_path,
    )


@contextmanager
def _workflow_staging_directory(
    *,
    prefix: str,
    dir: Path,
    request_sha256: str,
    committed: list[PublishedUnmixingOutput],
    warnings: list[str],
):
    from .publication_lock import _AuthorityPublicationLock
    import shutil
    temporary = dir / f"{prefix}{request_sha256[:24]}"
    temporary.mkdir(parents=True, exist_ok=True)
    with _AuthorityPublicationLock(dir / f"{prefix}{request_sha256[:24]}.processing"):
        try:
            yield str(temporary)
        except BaseException as failure:
            failure.add_note(f"Incomplete image work is retained for retry at {temporary}")
            raise
        else:
            if committed:
                try:
                    shutil.rmtree(temporary)
                except OSError as failure:
                    warnings.append(f"Completed image remains selected; unused work directory could not be removed: {failure}")


def _close_zarr_group(group: Any) -> None:
    store = getattr(group, "store", None)
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _matching_existing_publication(
    *,
    destination: Path,
    provenance_path: Path,
    request_sha256: str,
    source_path: Path,
    backend: Any,
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    ome: OME,
    config: UnmixingConfig,
    library: Any,
    basis_application: ResolvedBasisApplication | None,
    validated: ValidatedUnmixingInput,
    factors: Any | None,
    digital_range: EffectiveDigitalRange | None,
    warnings: tuple[str, ...],
    channel_name_replacement: bool,
    source_resolution_policy: str,
    output_biomarkers: tuple[str | None, ...] = (),
) -> PublishedUnmixingOutput | None:
    pointer_path = authority_pointer_path(destination)
    if not pointer_path.exists():
        return None
    publication = load_unmixing_output_publication(
        image_path=destination,
        provenance_path=provenance_path,
    )
    if publication.pointer.request_sha256 != request_sha256:
        return None
    try:
        sidecar_bytes = _verified_publication_artifact_bytes(
            publication,
            "provenance",
        )
        sidecar = loads_processing_provenance(sidecar_bytes)
        _validate_reuse_provenance(
            sidecar,
            source_path=source_path,
            destination=destination,
            header=header,
            config=config,
            library=library,
            basis_application=basis_application,
            validated=validated,
            factors=factors,
            digital_range=digital_range,
            warnings=warnings,
            output_biomarkers=output_biomarkers,
            channel_name_replacement=channel_name_replacement,
            source_resolution_policy=source_resolution_policy,
        )
        with backend.open(publication.image_path) as output_source:
            output_header = validate_canonical_ome_tiff_header(
                publication.image_path,
                output_source,
            )
            output_ome = output_source.metadata
            _validate_reuse_output_metadata(
                output_header,
                output_ome,
                sidecar=sidecar,
                source_header=header,
                ome=ome,
                library=library,
                source_resolution_policy=source_resolution_policy,
                config=config,
            )
        embedded = resolve_processing_provenance(output_ome, 0)
        if embedded != sidecar:
            raise UnmixingWorkflowError(
                "reused output embedded provenance differs from its sidecar"
            )
        _validate_reuse_clipping_evidence(
            publication,
            sidecar=sidecar,
            basis_application=basis_application,
        )
    except UnmixingWorkflowError:
        raise
    except Exception as exc:
        raise UnmixingWorkflowError(
            f"existing output cannot be reused as scientific authority: {exc}"
        ) from exc
    return replace(publication, reused=True)


def _verified_publication_artifact_bytes(
    publication: PublishedUnmixingOutput,
    role: str,
) -> bytes:
    artifacts = tuple(item for item in publication.manifest.artifacts if item.role == role)
    if len(artifacts) != 1:
        raise UnmixingWorkflowError(f"reuse publication lacks exact {role} artifact")
    path = {
        "image": publication.image_path,
        "provenance": publication.provenance_path,
        "clipping_evidence": publication.clipping_evidence_path,
    }[role]
    if path is None:
        raise UnmixingWorkflowError(f"reuse publication lacks {role} path")
    raw = path.read_bytes()
    artifact = artifacts[0]
    if (
        len(raw) != artifact.byte_length
        or hashlib.sha256(raw).hexdigest() != artifact.content_sha256
    ):
        raise UnmixingWorkflowError(f"reuse publication {role} changed after validation")
    return raw


def _validate_reuse_provenance(
    record: ProcessingProvenance,
    *,
    source_path: Path,
    destination: Path,
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    config: UnmixingConfig,
    library: Any,
    basis_application: ResolvedBasisApplication | None,
    validated: ValidatedUnmixingInput,
    factors: Any | None,
    digital_range: EffectiveDigitalRange | None,
    warnings: tuple[str, ...],
    output_biomarkers: tuple[str | None, ...],
    channel_name_replacement: bool,
    source_resolution_policy: str,
) -> None:
    authority = record.resolved_basis_authority
    expected_authority = None
    if basis_application is not None:
        expected_authority = _resolved_basis_authority_provenance(basis_application)
    if source_resolution_policy == "level0_only":
        pyramid_levels = _recursive_pyramid_level_count(header.size_x, header.size_y)
        output_policy = "recursive_2x_area_average_from_processed_level0"
        output_dtype = None
    else:
        pyramid_levels = len(header.native_pyramid_levels)
        output_policy = "pixel_identical_source_pyramid"
        output_dtype = header.container_dtype
    expected_record = _build_provenance(
        source_path,
        destination,
        library,
        validated,
        config,
        factors=factors,
        diagnostics=None,
        pyramid_levels=pyramid_levels,
        source_resolution_policy=source_resolution_policy,
        output_pyramid_policy=output_policy,
        warnings=warnings,
        channel_name_replacement=channel_name_replacement,
        header=header,
        digital_range=digital_range,
        output_dtype=output_dtype,
        resolved_basis_authority=expected_authority,
        output_biomarkers=output_biomarkers,
    )
    expected_record = replace(
        expected_record,
        timestamp=record.timestamp,
        solver_converged=record.solver_converged,
        solver_iterations=record.solver_iterations,
        solver_max_projected_gradient=record.solver_max_projected_gradient,
    )
    if expected_record != record:
        mismatches = tuple(
            name
            for name in record.__dataclass_fields__
            if getattr(expected_record, name) != getattr(record, name)
        )
        raise UnmixingWorkflowError(
            "reuse provenance differs from the complete current request: " + ", ".join(mismatches)
        )
    if basis_application is None:
        if authority is not None:
            raise UnmixingWorkflowError("reuse provenance has unexpected resolved authority")
    else:
        if authority is None:
            raise UnmixingWorkflowError("reuse provenance lacks resolved authority")
        current = _resolved_basis_authority_provenance(
            basis_application,
            clipping_evidence=None,
            clipping_evidence_path=None,
            clipping_evidence_bytes=None,
        )
        if current != authority:
            raise UnmixingWorkflowError("reuse provenance resolved authority differs")
        for name in (
            "authority_kind",
            "authority_id",
            "authority_sha256",
            "run_id",
            "target_image_sha256",
            "measurement_ids",
            "band_names",
            "response_transform_id",
            "response_transform_semantic",
            "response_transform_authority",
            "response_measurement_ids",
            "response_correction_multipliers",
            "component_names",
            "component_kinds",
            "fluorophores",
            "biomarkers",
            "autofluorescence_components",
            "conversion_factors",
            "source_artifact_sha256s",
        ):
            current_value = getattr(basis_application, name)
            if name == "conversion_factors":
                current_value = tuple(float(item) for item in current_value)
            if getattr(authority, name) != current_value:
                raise UnmixingWorkflowError(
                    f"reuse resolved authority {name} differs from current application"
                )


def _validate_reuse_output_metadata(
    output_header: CanonicalOmeTiffHeader,
    output_ome: OME,
    *,
    sidecar: ProcessingProvenance,
    source_header: CanonicalOmeTiffHeader | SourceImageFacts,
    ome: OME,
    library: Any,
    source_resolution_policy: str,
    config: UnmixingConfig,
) -> None:
    output_image = output_ome.images[0] if len(output_ome.images) == 1 else None
    output_pixels = None if output_image is None else output_image.pixels
    expected_channels = (
        source_header.size_c
        if source_resolution_policy == "copy_all_native_levels"
        else library.component_count
    )
    if (
        len(output_ome.images) != 1
        or output_header.size_x != source_header.size_x
        or output_header.size_y != source_header.size_y
        or output_header.size_z != 1
        or output_header.size_t != 1
        or output_header.size_c != expected_channels
        or output_header.container_dtype != sidecar.output_dtype
        or output_pixels is None
        or output_header.size_c != len(output_pixels.channels)
        or len(output_header.native_pyramid_levels) != sidecar.pyramid_levels
    ):
        raise UnmixingWorkflowError("reused output topology or dtype is not canonical")
    actual_levels = output_header.native_pyramid_levels
    if source_resolution_policy == "copy_all_native_levels":
        expected_geometry = tuple(
            (item.level_index, item.size_x, item.size_y)
            for item in source_header.native_pyramid_levels
        )
    else:
        width, height = source_header.size_x, source_header.size_y
        geometry: list[tuple[int, int, int]] = []
        for index in range(sidecar.pyramid_levels):
            geometry.append((index, width, height))
            width, height = (width + 1) // 2, (height + 1) // 2
        expected_geometry = tuple(geometry)
    expected_dtype = sidecar.output_dtype
    expected_bits = (
        32
        if expected_dtype == "float32"
        else (64 if expected_dtype == "float64" else source_header.container_bits)
    )
    # Every processed generation uses the standard lossless image profile.
    # The decoded header reports its Blosc/Zstd codec at each stored level.
    expected_compression = "blosc_zstd"
    for level, geometry in zip(actual_levels, expected_geometry, strict=True):
        facts = (
            level.level_index,
            level.size_x,
            level.size_y,
            level.channel_count,
            level.dtype,
            level.container_bits,
            level.samples_per_pixel,
            level.tile_width,
            level.tile_height,
            level.compression,
        )
        expected_facts = (
            *geometry,
            expected_channels,
            expected_dtype,
            expected_bits,
            1,
            256,
            256,
            expected_compression,
        )
        if facts != expected_facts:
            raise UnmixingWorkflowError(
                f"reused output pyramid level {level.level_index} differs from policy: "
                f"actual={facts!r}, expected={expected_facts!r}"
            )
    state = resolve_spectral_state(output_ome, 0)
    if source_resolution_policy == "copy_all_native_levels":
        from mif_pipeline.io.metadata.cycles import resolve_channel_cycles
        if resolve_channel_cycles(output_ome, 0) != resolve_channel_cycles(ome, 0):
            raise UnmixingWorkflowError("Reused copied-image cycles differ from the source")
        expected_state = resolve_spectral_state(ome, 0)
        output_order = (state.component_order if state.state == "unmixed"
                        else resolve_prepared_channel_order(output_ome, 0))
        state_matches = state == expected_state and output_order == tuple(library.component_names)
        if config.spectral_state_override is SpectralState.ISOLATED:
            from mif_pipeline.io.metadata.measurement_band_copy import validate_measurement_band_mapping
            validate_measurement_band_mapping(output_ome, config.measurement_band_components)
    else:
        state_matches = state.state == "unmixed" and state.component_order == tuple(library.component_names)
    if not state_matches:
        raise UnmixingWorkflowError("reused output spectral state/component order differs")
    source_image = ome.images[0]
    source_pixels = source_image.pixels
    assert output_image is not None and output_pixels is not None
    if source_resolution_policy == "level0_only":
        expected_biomarkers = sidecar.output_biomarkers
        expected_names = tuple(
            biomarker if sidecar.channel_name_replacement and biomarker is not None else component
            for component, biomarker in zip(
                library.component_names,
                expected_biomarkers,
                strict=True,
            )
        )
        expected_image = (
            "Image:0",
            "Pixels:0",
            f"{source_image.name or "Image"} — unmixed",
        )
        expected_channel_ids = tuple(f"Channel:0:{index}" for index in range(expected_channels))
        expected_fluors = tuple(library.fluorophores)
    else:
        source_channels = resolve_channel_metadata(ome, 0)
        expected_biomarkers = tuple(item.biomarker for item in source_channels)
        expected_names = tuple(
            (
                item.biomarker
                if sidecar.channel_name_replacement and item.biomarker is not None
                else source_pixels.channels[index].name
            )
            for index, item in enumerate(source_channels)
        )
        expected_image = (source_image.id, source_pixels.id, source_image.name)
        expected_channel_ids = tuple(item.id for item in source_pixels.channels)
        expected_fluors = tuple(item.fluorophore for item in source_channels)
    actual_image = (output_image.id, output_pixels.id, output_image.name)
    actual_channel_ids = tuple(item.id for item in output_pixels.channels)
    actual_names = tuple(item.name for item in output_pixels.channels)
    channels = resolve_channel_metadata(output_ome, 0)
    actual_fluors = tuple(item.fluorophore for item in channels)
    actual_biomarkers = tuple(item.biomarker for item in channels)
    if sidecar.output_imaging_cycles:
        from mif_pipeline.io.metadata.cycles import resolve_channel_cycles
        if resolve_channel_cycles(output_ome, require_complete=True) != sidecar.output_imaging_cycles:
            raise UnmixingWorkflowError("Reused output imaging cycles differ from its accepted reference")
    if (
        actual_image != expected_image
        or output_header.image_id != expected_image[0]
        or output_header.pixels_id != expected_image[1]
        or actual_channel_ids != expected_channel_ids
        or actual_names != expected_names
        or actual_fluors != expected_fluors
        or actual_biomarkers != expected_biomarkers
    ):
        raise UnmixingWorkflowError(
            "reused output image/channel identity or reviewed semantics differ"
        )
    if _physical_calibration_signature(output_pixels) != _physical_calibration_signature(
        source_pixels
    ):
        raise UnmixingWorkflowError("reused output physical calibration differs")
    if (
        output_header.physical_size_x,
        output_header.physical_size_x_unit,
        output_header.physical_size_y,
        output_header.physical_size_y_unit,
    ) != (
        source_header.physical_size_x,
        source_header.physical_size_x_unit,
        source_header.physical_size_y,
        source_header.physical_size_y_unit,
    ):
        raise UnmixingWorkflowError("reused output header calibration differs")


def _physical_calibration_signature(pixels: Any) -> tuple[object, ...]:
    def unit(value: object) -> object:
        return getattr(value, "value", value)

    return (
        pixels.physical_size_x,
        unit(pixels.physical_size_x_unit),
        pixels.physical_size_y,
        unit(pixels.physical_size_y_unit),
        pixels.physical_size_z,
        unit(pixels.physical_size_z_unit),
    )


def _validate_reuse_clipping_evidence(publication, *, sidecar, basis_application):
    """Preserve reference-based output reuse; reject retired learned-background results."""

    authority = sidecar.resolved_basis_authority
    if (
        authority is not None and authority.ssasu_application is not None
    ) or publication.clipping_evidence_path is not None:
        raise UnmixingWorkflowError(
            "This output used a retired background-estimation method. Start a reference-based analysis."
        )


def _reused_unmixing_result(
    *,
    publication: PublishedUnmixingOutput,
    status: str,
    source_path: Path,
    pyramid_levels: int,
    warnings: tuple[str, ...],
) -> UnmixingResult:
    if publication.reused is not True:
        raise ValueError("reused output publication must be marked reused")
    retained = loads_processing_provenance(publication.provenance_path.read_bytes())
    if retained.spatial_registration is not None:
        warnings = retained.warnings
    return UnmixingResult(
        status=status,
        input_path=source_path,
        output_path=publication.image_path,
        provenance_path=publication.provenance_path,
        regions_processed=0,
        pyramid_levels=pyramid_levels,
        warnings=warnings + publication.warnings,
        authority_pointer_path=publication.pointer_path,
        publication_manifest_sha256=publication.manifest.manifest_content_sha256,
        publication_generation_id=publication.pointer.generation_id,
        publication_request_sha256=publication.pointer.request_sha256,
        publication_reused=True,
        publication_warnings=publication.warnings,
        clipping_evidence_path=publication.clipping_evidence_path,
    )


def _reject_existing_destination_without_overwrite(
    destination: Path,
    config: UnmixingConfig,
) -> None:
    if destination.exists() and not config.overwrite:
        raise FileExistsError(f"output OME-Zarr already exists: {destination}")


def _refuse_occupied_no_read_publication_namespace(
    destination: Path,
    provenance_path: Path,
) -> None:
    occupied: list[str] = []
    for label, path in (
        ("authority pointer", authority_pointer_path(destination)),
        ("image alias", destination),
        ("provenance alias", provenance_path),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnmixingWorkflowError(
                f"cannot inspect no-read publication namespace {label}: {exc}"
            ) from exc
        occupied.append(label)
    if occupied:
        raise UnmixingWorkflowError(
            "already-unmixed no-read output refuses an occupied Task9B1 publication "
            "namespace: " + ", ".join(occupied)
        )


def _recursive_pyramid_level_count(width: int, height: int) -> int:
    levels = 1
    while max(width, height) > 512:
        width = (width + 1) // 2
        height = (height + 1) // 2
        levels += 1
    return levels


def _validate_basis_source_choice(
    library_path: str | Path | None,
    basis_application: ResolvedBasisApplication | None,
) -> None:
    if library_path is not None and basis_application is not None:
        raise UnmixingWorkflowError(
            "exactly one spectral library path or resolved basis application is required"
        )
    if library_path is not None and (
        not isinstance(library_path, (str, Path))
        or (isinstance(library_path, str) and not library_path)
    ):
        raise UnmixingWorkflowError("library_path must be a nonempty string or Path")
    if basis_application is not None and type(basis_application) is not ResolvedBasisApplication:
        raise UnmixingWorkflowError("basis_application must be exact ResolvedBasisApplication")


def _resolved_basis_target_binding(
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    ome: OME,
    validated: ValidatedUnmixingInput,
    application: ResolvedBasisApplication,
) -> ResolvedBasisTargetBinding:
    application.revalidate_authority()
    pixels = ome.images[validated.image_index].pixels
    image = ome.images[validated.image_index]
    if (
        header.image_id != image.id
        or header.pixels_id != pixels.id
        or (header.size_x, header.size_y, header.size_c)
        != (pixels.size_x, pixels.size_y, pixels.size_c)
    ):
        raise UnmixingWorkflowError(
            "canonical header image identity or dimensions differ from reviewed metadata"
        )
    assert validated.acquisition is not None
    binding = ResolvedBasisTargetBinding(
        target_image_sha256=_source_sha256(header),
        canonical_image_id=header.image_id,
        measurement_ids=application.measurement_ids,
        band_names=validated.acquisition.band_order,
        width_px=header.size_x,
        height_px=header.size_y,
    )
    application.validate_target_binding(binding)
    return binding


def _select_spectral_basis(
    ome: OME,
    catalog: SpectralLibrary,
    *,
    observed_state: str,
    canonical_component_order: tuple[str, ...] | None,
    configured_state: SpectralState | None,
) -> SpectralLibrary:
    """Resolve a basis without retaining any implicit active component set."""

    if configured_state == SpectralState.ABORT:
        return catalog
    state = observed_state
    if state == "unknown" and configured_state is not None:
        state = configured_state.value
    if state == "raw":
        channel_fluors = tuple(
            channel.fluorophore
            for channel in resolve_channel_metadata(ome, 0)
            if channel.fluorophore is not None
        )
        return catalog.select_fluorophores(channel_fluors)
    if state == "unmixed":
        if canonical_component_order is not None:
            return catalog.select_components(canonical_component_order)
        return catalog
    return catalog


def _biomarker_review_differs(source_ome: OME, reviewed_ome: OME) -> bool:
    source_rows = resolve_biomarker_assignment_rows(source_ome)
    reviewed_rows = resolve_biomarker_assignment_rows(reviewed_ome)
    return tuple((row.biomarker_assignment, row.biomarker) for row in source_rows) != tuple(
        (row.biomarker_assignment, row.biomarker) for row in reviewed_rows
    )


def _resolve_channel_name_choice(
    choice: bool,
) -> bool:
    """Reject omitted/``None`` display choices at the service boundary."""

    if not isinstance(choice, bool):
        raise UnmixingWorkflowError("channel-name replacement choice must be an explicit boolean")
    return choice


def _channel_name_replacement_changes(ome: OME) -> bool:
    return any(
        row.biomarker_assignment == "assigned"
        and row.biomarker is not None
        and row.channel_name != row.biomarker
        for row in resolve_biomarker_assignment_rows(ome)
    )


def _copy_source_pyramid_to_zarr(source, ome, store_path, *, chunk_size,
        request_sha256, progress):
    def report(level, x, y, count):
        if progress is not None:
            progress(ProgressEvent("metadata_copy", count, count, level=level))
    count, levels, dtype = copy_source_pyramid(source, ome, store_path,
        request_sha256=request_sha256, block_size=chunk_size, progress=report)
    return _PyramidCopySummary(count, levels, dtype)


def inspect_canonical_spectral_state(
    input_path: str | Path,
    *,
    backend: Any | None = None,
) -> SpectralState:
    """Read agreeing source declarations for presentation-layer decision prompts."""

    source_path = Path(input_path)
    if not _is_processed_or_tiff_source(source_path):
        raise UnmixingWorkflowError("input must be a canonical OME-TIFF")
    selected_backend = backend or get_default_backend()
    with selected_backend.open(source_path) as source:
        ome = source.metadata
        canonical = resolve_spectral_state(ome, 0)
        acquisition = resolve_unmixing_input_metadata(ome, 0)
        state, _ = _resolve_source_spectral_state(
            ome, 0, spectral=canonical, acquisition=acquisition
        )
        return SpectralState(state)


def _build_provenance(
    input_path: Path,
    output_path: Path,
    library: SpectralLibrary,
    validated: ValidatedUnmixingInput,
    config: UnmixingConfig,
    *,
    factors: Any | None,
    diagnostics: Any | None,
    pyramid_levels: int,
    source_resolution_policy: str,
    output_pyramid_policy: str,
    warnings: tuple[str, ...],
    channel_name_replacement: bool,
    output_dtype: str | None = None,
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    digital_range: EffectiveDigitalRange | None,
    resolved_basis_authority: ResolvedBasisAuthorityProvenance | None = None,
    output_biomarkers: tuple[str | None, ...] = (),
) -> ProcessingProvenance:
    acquisition = validated.acquisition
    writes_output = source_resolution_policy != "not_read"
    library_free_copy = library.identifier is None
    from mif_pipeline.io.aligned_collection import is_aligned_source_collection, load_aligned_source_collection
    spatial_registration = None
    if is_aligned_source_collection(input_path):
        registered = load_aligned_source_collection(input_path).registration
        if registered is not None:
            spatial_registration = registered.to_mapping()
            if source_resolution_policy == "copy_all_native_levels":
                output_pyramid_policy = "linear_resampled_cycle_grid"
                warnings = ("Spectral unmixing skipped; original cycle intensities were spatially resampled once "
                            "with linear interpolation onto the reviewed common reference crop.",)
    return ProcessingProvenance(
        spatial_registration=spatial_registration,
        input_path=str(input_path.resolve()),
        output_path=str(output_path.resolve()),
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        pipeline_version=_pipeline_version(),
        execution_runtime=(
            _region_execution_runtime(config.calculation_precision) if source_resolution_policy == "level0_only" else ()
        ),
        library_identifier=library.identifier,
        library_sha256=library.sha256,
        input_band_order=acquisition.band_order if acquisition else (),
        output_component_order=library.component_names,
        exposure_seconds=acquisition.exposure_seconds if acquisition else (),
        normalization_factors=(
            tuple(float(1.0 / value) for value in factors.per_band_divisor)
            if factors is not None
            else ()
        ),
        source_storage_dtype=header.container_dtype,
        source_storage_bits=header.container_bits,
        source_storage_max=(digital_range.maximum if digital_range is not None else (None if factors is not None else 0.0)),
        camera_bit_depth=acquisition.camera_bit_depth if acquisition else (),
        gain=acquisition.gain if acquisition else (),
        binning=acquisition.binning if acquisition else (),
        binning_area=acquisition.binning_area if acquisition else (),
        source_spectral_state=(
            "unknown" if validated.state_source == "user_confirmed_prepared_channels"
            else acquisition.spectral_state if acquisition else validated.spectral_state.value
        ),
        target_af_behavior=validated.config.af_behavior.value,
        source_autofluorescence_subtracted=(
            acquisition.autofluorescence_subtracted if acquisition else None
        ),
        state_source=validated.state_source,
        state_override=(
            config.spectral_state_override.value
            if validated.state_source == "user_override"
            and config.spectral_state_override is not None
            else None
        ),
        solver="not_applied" if library_free_copy else config.solver.value,
        nnls_tolerance=(
            config.nnls_tolerance
            if not library_free_copy and config.solver == SolverKind.NNLS
            else None
        ),
        nnls_max_iterations=(
            config.nnls_max_iterations
            if not library_free_copy and config.solver == SolverKind.NNLS
            else None
        ),
        solver_converged=diagnostics.converged if diagnostics is not None else None,
        solver_iterations=diagnostics.iterations if diagnostics is not None else None,
        solver_max_projected_gradient=(
            diagnostics.max_projected_gradient if diagnostics is not None else None
        ),
        normalization_policy=(factors.policy if factors is not None else "not_applied"),
        normalization_formula=(factors.formula if factors is not None else "not_applied"),
        spectral_basis=(
            "not_applied"
            if library_free_copy
            else "accepted_resolved_spectral_basis"
        ),
        spectral_column_sums=tuple(float(value) for value in library.column_sums),
        af_subtraction_performed=False,
        processing_chunk_width=(
            config.chunk_size[0] if writes_output else ProvenanceApplicability.NOT_APPLICABLE
        ),
        processing_chunk_height=(
            config.chunk_size[1] if writes_output else ProvenanceApplicability.NOT_APPLICABLE
        ),
        pyramid_levels=pyramid_levels,
        source_resolution_policy=source_resolution_policy,
        output_pyramid_policy=output_pyramid_policy,
        writer_tile_width=(256 if writes_output else ProvenanceApplicability.NOT_APPLICABLE),
        writer_tile_height=(256 if writes_output else ProvenanceApplicability.NOT_APPLICABLE),
        compression=(
            "blosc_zstd_level1_byte_shuffle" if writes_output else ProvenanceApplicability.NOT_APPLICABLE
        ),
        output_dtype=(
            (config.output_dtype if output_dtype is None else output_dtype)
            if writes_output
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        warnings=warnings,
        channel_name_replacement=channel_name_replacement,
        input_sha256=_source_sha256(header),
        container_dtype=header.container_dtype,
        container_bits=header.container_bits,
        source_dimension_order=header.dimension_order,
        ome_significant_bits=header.significant_bits,
        effective_digital_maxima=(
            (digital_range.maximum,) * len(acquisition.band_order)
            if digital_range is not None and acquisition is not None
            else ()
        ),
        digital_range_authority=(
            digital_range.authority
            if digital_range is not None
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        offsets=(
            acquisition.offsets if acquisition is not None and factors is not None else ()
        ),
        applied_offsets=(tuple(float(value) for value in factors.offsets)
                         if factors is not None and factors.offsets is not None else None),
        source_intensity_representation=(acquisition.intensity_representation if acquisition is not None else "unknown"),
        offset_authority=(
            acquisition.offset_authority
            if acquisition is not None and factors is not None
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        offset_profile_identifier=(
            acquisition.offset_profile_identifier
            if acquisition is not None and factors is not None
            else None
        ),
        native_pyramid_levels=_source_native_levels(header),
        normalization_divisors=(
            tuple(float(value) for value in factors.per_band_divisor) if factors is not None else ()
        ),
        response_transform_id=(
            factors.response_transform_id
            if factors is not None
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        response_transform_semantic=(
            factors.response_transform_semantic
            if factors is not None
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        response_transform_authority=(
            factors.response_transform_authority
            if factors is not None
            else ProvenanceApplicability.NOT_APPLICABLE
        ),
        response_measurement_ids=(factors.response_measurement_ids if factors is not None else ()),
        response_correction_multipliers=(
            factors.response_correction_multipliers if factors is not None else ()
        ),
        resolved_basis_authority=resolved_basis_authority,
        output_biomarkers=output_biomarkers,
        schema_version=4 if validated.imaging_cycles and factors is not None else 3 if library_free_copy else 2,
        effective_digital_range=digital_range,
        input_imaging_cycles=validated.imaging_cycles if factors is not None else (),
        output_imaging_cycles=(
            _output_imaging_cycles(validated.imaging_cycles, library.component_names)
            if factors is not None else ()
        ),
    )


def _output_imaging_cycles(input_cycles, component_names):
    from mif_pipeline.io.metadata.cycles import split_cycle_component
    if not input_cycles:
        return ()
    cycles = set(input_cycles)
    return tuple(split_cycle_component(name)[1] if split_cycle_component(name)[1] is not None
                 else next(iter(cycles)) for name in component_names)


def _resolved_basis_authority_provenance(
    application: ResolvedBasisApplication | None,
    *,
    clipping_evidence: None = None,
    clipping_evidence_path: Path | None = None,
    clipping_evidence_bytes: bytes | None = None,
) -> ResolvedBasisAuthorityProvenance | None:
    if application is None:
        return None
    application.revalidate_authority()
    if any(
        value is not None
        for value in (clipping_evidence, clipping_evidence_path, clipping_evidence_bytes)
    ):
        raise UnmixingWorkflowError(
            "Background-estimation artifacts are not supported in this release"
        )
    return ResolvedBasisAuthorityProvenance(
        authority_kind=application.authority_kind,
        authority_id=application.authority_id,
        authority_sha256=application.authority_sha256,
        run_id=application.run_id,
        target_image_sha256=application.target_image_sha256,
        measurement_ids=application.measurement_ids,
        band_names=application.band_names,
        response_transform_id=application.response_transform_id,
        response_transform_semantic=application.response_transform_semantic,
        response_transform_authority=application.response_transform_authority,
        response_measurement_ids=application.response_measurement_ids,
        response_correction_multipliers=(application.response_correction_multipliers),
        component_names=application.component_names,
        component_kinds=application.component_kinds,
        fluorophores=application.fluorophores,
        biomarkers=application.biomarkers,
        autofluorescence_components=application.autofluorescence_components,
        conversion_factors=tuple(float(value) for value in application.conversion_factors),
        source_artifact_sha256s=application.source_artifact_sha256s,
        ssasu_application=None,
    )


def _publish_staged_output(
    *,
    store_path: Path,
    levels: tuple[Any, ...],
    output_ome: OME,
    record: ProcessingProvenance,
    request_sha256: str,
    destination: Path,
    provenance_path: Path,
    config: UnmixingConfig,
    clipping_evidence_path: Path | None = None,
    clipping_evidence_bytes: bytes | None = None,
) -> PublishedUnmixingOutput:
    provenance_bytes = render_processing_provenance(record).encode("utf-8")
    if record.source_resolution_policy == "copy_all_native_levels":
        width, height = levels[0].shape[2], levels[0].shape[1]
        downsamples = [0.5 * (width / level.shape[2] + height / level.shape[1]) for level in levels]
    else:
        downsamples = [2.0 ** index for index in range(len(levels))]
    finish_processed_image(store_path, output_ome, level_downsamples=downsamples)

    def write_image(private_destination: Path) -> None:
        # The numerical owner completed this exact immutable directory already.
        # Publication moves it once without decoding, copying or encoding the image again.
        os.replace(store_path, private_destination)

    return publish_unmixing_output(
        request_sha256=request_sha256,
        image_path=destination,
        provenance_path=provenance_path,
        image_writer=write_image,
        provenance_bytes=provenance_bytes,
        clipping_evidence_path=clipping_evidence_path,
        clipping_evidence_bytes=clipping_evidence_bytes,
        overwrite=config.overwrite,
    )


def _unmixing_request_sha256(
    *,
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    ome: OME,
    config: UnmixingConfig,
    channel_name_replacement: bool,
    library: Any,
    basis_application: ResolvedBasisApplication | None,
    source_resolution_policy: str,
    backend_versions: BackendVersions | None = None,
    writer_runtime: tuple[tuple[str, str], ...] | None = None,
    solver_policy_version: str | None = None,
    normalization_policy_version: str | None = None,
    response_correction_policy_version: str | None = None,
    digital_range: EffectiveDigitalRange | None = None,
) -> str:
    rows = resolve_biomarker_assignment_rows(ome)
    payload = {
        "schema_name": "mif_pipeline.unmixing_output_request",
        "schema_version": 2,
        "source": {
            "sha256": _source_sha256(header),
            "opened_identity": (
                {
                    "resolved_path": str(header.identity.resolved_path),
                    "file_sha256": header.identity.file_sha256,
                    "size_bytes": header.identity.size_bytes,
                    "modified_time_ns": header.identity.modified_time_ns,
                    "device_id": header.identity.device_id,
                    "inode_id": header.identity.inode_id,
                }
                if isinstance(header, SourceImageFacts)
                else None
            ),
            "image_id": header.image_id,
            "pixels_id": header.pixels_id,
            "dimensions": [
                header.size_x,
                header.size_y,
                header.size_z,
                header.size_c,
                header.size_t,
            ],
            "dimension_order": header.dimension_order,
            "container_dtype": header.container_dtype,
            "container_bits": header.container_bits,
            "significant_bits": header.significant_bits,
            "native_pyramid_levels": [asdict(item) for item in _source_native_levels(header)],
            "storage_bits": (
                asdict(header.storage_bits)
                if isinstance(header, SourceImageFacts) and header.storage_bits is not None
                else None
            ),
        },
        "reviewed_ome_sha256": hashlib.sha256(to_xml(ome).encode("utf-8")).hexdigest(),
        "effective_digital_range": None if digital_range is None else asdict(digital_range),
        "reviewed_channel_semantics": [
            {
                "image_index": row.image_index,
                "channel_index": row.channel_index,
                "channel_id": row.channel_id,
                "channel_name": row.channel_name,
                "fluorophore": row.fluorophore,
                "component_name": row.component_name,
                "biomarker": row.biomarker,
                "biomarker_assignment": row.biomarker_assignment,
            }
            for row in rows
        ],
        "config": {
            "solver": config.solver.value,
            "spectral_state_override": (
                None
                if config.spectral_state_override is None
                else config.spectral_state_override.value
            ),
            **({"measurement_band_components": list(config.measurement_band_components)}
               if config.measurement_band_components else {}),
            "af_behavior": config.af_behavior.value,
            "chunk_size": list(config.chunk_size),
            "output_dtype": config.output_dtype,
            **({"calculation_precision": config.calculation_precision}
               if config.calculation_precision is not None else {}),
            "nnls_tolerance": config.nnls_tolerance,
            "nnls_max_iterations": config.nnls_max_iterations,
            "overwrite": config.overwrite,
            "channel_name_replacement": channel_name_replacement,
            "source_resolution_policy": source_resolution_policy,
            "writer_tile_size": [256, 256],
        },
        "basis": {
            "identifier": library.identifier,
            "sha256": library.sha256,
            "band_names": list(library.band_names),
            "component_names": list(library.component_names),
            "conversion_factors": [float(value) for value in library.column_sums],
            "authority_kind": (
                None if basis_application is None else basis_application.authority_kind
            ),
            "source_artifact_sha256s": (
                [] if basis_application is None else list(basis_application.source_artifact_sha256s)
            ),
            "resolved_application": (
                None
                if basis_application is None
                else {
                    "authority_id": basis_application.authority_id,
                    "authority_sha256": basis_application.authority_sha256,
                    "run_id": basis_application.run_id,
                    "target_image_sha256": (basis_application.target_image_sha256),
                    "measurement_ids": list(basis_application.measurement_ids),
                    "response_transform_id": (basis_application.response_transform_id),
                    "response_transform_semantic": (basis_application.response_transform_semantic),
                    "response_transform_authority": (
                        basis_application.response_transform_authority
                    ),
                    "response_measurement_ids": list(basis_application.response_measurement_ids),
                    "response_correction_multipliers": list(
                        basis_application.response_correction_multipliers
                    ),
                    "component_kinds": list(basis_application.component_kinds),
                    "fluorophores": list(basis_application.fluorophores),
                    "biomarkers": list(basis_application.biomarkers),
                    "autofluorescence_components": list(
                        basis_application.autofluorescence_components
                    ),
                    "full_slide_policy": None,
                    "tissue_mask_binding_sha256": None,
                    "global_background_digest_policy": None,
                    "global_background_sha256": None,
                    "ssasu_artifact_companion_sha256s": [],
                }
            ),
        },
        "runtime": {
            "pipeline_version": _pipeline_version(),
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "region_execution": (
                list(_region_execution_runtime(config.calculation_precision))
                if source_resolution_policy == "level0_only"
                else []
            ),
            "zarr_version": getattr(zarr, "__version__", "unknown"),
            "backend_versions": asdict(backend_versions or _unreported_backend_versions()),
            "writer_runtime": list(writer_runtime or _writer_runtime(config)),
            "solver_policy_version": (solver_policy_version or _solver_policy_version(config)),
            "normalization_policy_version": (
                normalization_policy_version or _normalization_policy_version()
            ),
            "response_correction_policy_version": (
                response_correction_policy_version or _response_correction_policy_version()
            ),
        },
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _unreported_backend_versions() -> BackendVersions:
    return BackendVersions(
        backend="unreported",
        bffile_version="unreported",
        bioformats_maven_coordinate="unreported",
        bioformats_runtime_version="unreported",
    )


def _source_backend_versions(source: Any) -> BackendVersions:
    value = getattr(source, "versions", None)
    return value if type(value) is BackendVersions else _unreported_backend_versions()


def _reported_package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unavailable"


def _writer_runtime(config: UnmixingConfig) -> tuple[tuple[str, str], ...]:
    return (("policy", "mif_pipeline_ngff05_zarr3_sharded_writer_v1"),
        ("ome_types", _reported_package_version("ome-types")),
        ("zarr", zarr.__version__), ("numcodecs", _reported_package_version("numcodecs")),
        ("codec_runtime", "Blosc/Zstd level1 byte shuffle; one codec thread"))


def _region_execution_runtime(calculation_precision=None) -> tuple[tuple[str, str], ...]:
    """Record the fixed scheduling limits used by the production region engine."""

    policy = RegionExecutionPolicy()
    from mif_pipeline._remote_transport import selected_profile

    remote = selected_profile("spectral_unmixing")
    remote_sha = (
        "not_used"
        if remote is None
        else hashlib.sha256(
            json.dumps(remote, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    return tuple(
        sorted(
            {
                "selective_profile_sha256": remote_sha,
                "spectral_device_preference": os.environ.get("MIF_COMPUTE_DEVICE", "auto"),
                "spectral_precision_policy": calculation_precision or os.environ.get(
                    "MIF_SPECTRAL_PRECISION", "device_selected"
                ),
                "calculation_precision_preference": calculation_precision or os.environ.get(
                    "MIF_COMPUTE_PRECISION", "device_selected"
                ),
                "spectral_kernel_policy": "svd-clipped32-residual-nnls32-or-certified64/v4",
                "scheduler": "concurrent.futures.shards",
                "scheduler_version": platform.python_version(),
                "scheduling_policy": "bounded-regions/v1",
                "max_workers": str(policy.max_workers),
                "max_inflight_regions": str(policy.max_inflight_regions),
                "working_array_budget_bytes": str(policy.working_array_budget_bytes),
                "threadpoolctl_version": version("threadpoolctl"),
            }.items()
        )
    )


def _solver_policy_version(config: UnmixingConfig) -> str:
    return {
        SolverKind.CLIPPED_LS: "mif_pipeline_clipped_least_squares_v1",
        SolverKind.NNLS: "mif_pipeline_batched_nnls_v1",
    }[config.solver]


def _normalization_policy_version(acquisition=None) -> str:
    """Identify the selected numerical correction in the saved execution request."""
    return ({"exposure_only": "mif_pipeline_exposure_only_v1", "as_stored": "mif_pipeline_as_stored_v1"}.get(acquisition.correction_mode, CanonicalOffsetExposureNormalization.policy)
            if acquisition is not None else CanonicalOffsetExposureNormalization.policy)


def _response_correction_policy_version() -> str:
    return "mif_pipeline_multiply_normalized_measurement_float32_v1"


def _component_biomarkers(ome: Any, library: SpectralLibrary) -> tuple[str | None, ...]:
    """Map the reviewed component markers to the final library column order."""
    inventory = resolve_component_identity_inventory(ome, 0)
    if inventory is not None and inventory.component_biomarkers is not None:
        assignments = dict(zip(inventory.component_names, inventory.component_biomarkers, strict=True))
        resolved = []
        for index, component in enumerate(library.component_names):
            if index in library.autofluorescence_components:
                resolved.append(None)
                continue
            if component not in assignments:
                raise UnmixingWorkflowError(
                    f"Review {component!r} in Channels and markers before unmixing"
                )
            marker = assignments[component]
            if library.biomarkers[index] is not None and library.biomarkers[index] != marker:
                raise UnmixingWorkflowError(
                    f"The reference library and channel review assign different markers to {component!r}"
                )
            resolved.append(marker)
        return tuple(resolved)
    channels = resolve_channel_metadata(ome, 0)
    resolved: list[str | None] = []
    for index, component in enumerate(library.component_names):
        if index in library.autofluorescence_components:
            resolved.append(None)
            continue
        fluorophore = library.fluorophores[index]
        matches = tuple(channel for channel in channels if fluorophore == channel.fluorophore)
        assigned = {channel.biomarker for channel in matches if channel.biomarker is not None}
        if len(assigned) > 1:
            raise UnmixingWorkflowError(
                "distinct reviewed biomarkers map to selected "
                f"component {component!r} through Channel.Fluor "
                f"{fluorophore!r}: {", ".join(sorted(assigned))}"
            )
        declared = library.biomarkers[index]
        if declared is not None and assigned and assigned != {declared}:
            raise UnmixingWorkflowError(
                f"Reviewed marker for {component!r} conflicts with its known source assignment"
            )
        resolved.append(declared if declared is not None else next(iter(assigned)) if assigned else None)
    return tuple(resolved)


def _validate_source_topology(source: Any, ome: Any) -> None:
    if len(ome.images) != 1 or len(source.series) != 1:
        raise UnmixingWorkflowError(
            "canonical unmixing currently requires exactly one OME image/series"
        )
    if not source.series[0].resolutions:
        raise UnmixingWorkflowError("canonical source has no readable base resolution")
    descriptor = source.series[0].resolutions[0]
    pixels = ome.images[0].pixels
    expected_dtype = {
        "uint8": np.dtype("uint8"),
        "uint16": np.dtype("uint16"),
        "uint32": np.dtype("uint32"),
        "int8": np.dtype("int8"),
        "int16": np.dtype("int16"),
        "int32": np.dtype("int32"),
        "float": np.dtype("float32"),
        "double": np.dtype("float64"),
    }.get(getattr(pixels.type, "value", pixels.type))
    descriptor_contract = (
        descriptor.size_x,
        descriptor.size_y,
        descriptor.size_z,
        descriptor.size_c,
        descriptor.size_t,
        descriptor.rgb_samples,
        descriptor.dtype,
    )
    ome_contract = (
        pixels.size_x,
        pixels.size_y,
        pixels.size_z,
        pixels.size_c,
        pixels.size_t,
        1,
        expected_dtype,
    )
    if expected_dtype is None or descriptor_contract != ome_contract:
        raise UnmixingWorkflowError(
            "canonical reader descriptor conflicts with OME dimensions/dtype"
        )


def _validated_canonical_metadata_override(
    source_ome: OME,
    override: OME | None,
    *,
    replace_channel_names_with_biomarkers: bool | None,
) -> OME:
    """Apply only reviewed biomarker semantics to a deep source snapshot.

    The GUI-held OME object is never trusted as replacement image metadata.
    Image, Pixels, and Channel identities/topology must match the freshly read
    canonical source.  Complete reviewed biomarker rows are then reapplied to
    a copy of that source, discarding every other difference in the override.
    """

    if override is None:
        return source_ome
    if not isinstance(replace_channel_names_with_biomarkers, bool):
        raise UnmixingWorkflowError(
            "reviewed canonical metadata requires an explicit channel-name replacement choice"
        )
    if not isinstance(override, OME):
        raise UnmixingWorkflowError("canonical metadata override must be an OME model")
    if len(override.images) != len(source_ome.images):
        raise UnmixingWorkflowError(
            "canonical metadata override image topology differs from source"
        )
    topology_fields = (
        "size_x",
        "size_y",
        "size_z",
        "size_c",
        "size_t",
        "dimension_order",
        "type",
    )
    for image_index, (source_image, reviewed_image) in enumerate(
        zip(source_ome.images, override.images)
    ):
        if source_image.id != reviewed_image.id:
            raise UnmixingWorkflowError(
                f"canonical metadata override image identity differs at image {image_index}"
            )
        source_pixels = source_image.pixels
        reviewed_pixels = reviewed_image.pixels
        if source_pixels.id != reviewed_pixels.id or any(
            getattr(source_pixels, field) != getattr(reviewed_pixels, field)
            for field in topology_fields
        ):
            raise UnmixingWorkflowError(
                f"canonical metadata override pixel topology differs at image {image_index}"
            )
        if len(source_pixels.channels) != len(reviewed_pixels.channels):
            raise UnmixingWorkflowError(
                f"canonical metadata override channel topology differs at image {image_index}"
            )
        for channel_index, (source_channel, reviewed_channel) in enumerate(
            zip(source_pixels.channels, reviewed_pixels.channels)
        ):
            if source_channel.id != reviewed_channel.id:
                raise UnmixingWorkflowError(
                    "canonical metadata override channel identity differs at "
                    f"image {image_index}, channel {channel_index}"
                )
    try:
        from mif_pipeline.io.metadata.cycles import replay_channel_cycle_review
        source_ome = replay_channel_cycle_review(source_ome, override)
        inventory = resolve_component_identity_inventory(override, 0)
        if inventory is not None:
            # The accepted review replaces this inventory, including a prior stain choice.
            source_ome = source_ome.model_copy(deep=True)
            annotation_id = "Annotation:ComponentIdentity:0"
            source_ome.images[0].annotation_refs = [ref for ref in source_ome.images[0].annotation_refs
                if ref.id != annotation_id]
            if source_ome.structured_annotations is not None:
                source_ome.structured_annotations.map_annotations = [annotation
                    for annotation in source_ome.structured_annotations.map_annotations if annotation.id != annotation_id]
        if inventory is not None and inventory.mode is ComponentIdentityMode.PLANNED_INVENTORY:
            if inventory.component_biomarkers is None:
                raise UnmixingWorkflowError("Complete Channels and markers before unmixing")
            return apply_component_identity_inventory(
                source_ome, 0, inventory.component_names, mode=inventory.mode,
                decision_source=inventory.decision_source,
                component_biomarkers=inventory.component_biomarkers,
                nuclear_component_index=inventory.nuclear_component_index,
                nuclear_stain_reviewed=inventory.nuclear_stain_reviewed,
            )
        rows = resolve_biomarker_assignment_rows(override)
        _validate_override_channel_names(
            source_ome,
            rows,
            replace_channel_names_with_biomarkers=replace_channel_names_with_biomarkers,
        )
        assignments: list[BiomarkerAssignment] = []
        from mif_pipeline.io.metadata.biomarkers import duplicate_channel_biomarker
        duplicate = duplicate_channel_biomarker(override, rows)
        if duplicate is not None:
            raise UnmixingWorkflowError(f"canonical metadata override has duplicate biomarker {duplicate!r}")
        for row in rows:
            if row.biomarker_assignment == "missing":
                raise UnmixingWorkflowError(
                    "canonical metadata override contains an unreviewed biomarker"
                )
            assignments.append(
                BiomarkerAssignment(
                    row.image_index,
                    row.channel_index,
                    row.biomarker,
                )
            )
        reviewed = canonicalize_ome(source_ome, assignments=assignments)
        if inventory is not None:
            reviewed = apply_component_identity_inventory(reviewed, 0, inventory.component_names,
                mode=inventory.mode, decision_source=inventory.decision_source,
                component_biomarkers=inventory.component_biomarkers,
                nuclear_component_index=inventory.nuclear_component_index,
                nuclear_stain_reviewed=inventory.nuclear_stain_reviewed,
                promote_channel_fluorophores=inventory.channel_fluor_promoted)
        return reviewed
    except Exception as exc:
        if isinstance(exc, UnmixingWorkflowError):
            raise
        raise UnmixingWorkflowError(
            f"canonical metadata override is not a complete reviewed biomarker snapshot: {exc}"
        ) from exc


def _validate_override_channel_names(
    source_ome: OME,
    rows: tuple[Any, ...],
    *,
    replace_channel_names_with_biomarkers: bool,
) -> None:
    """Permit only the resolved slide-wide display-name transformation."""

    for row in rows:
        source_name = source_ome.images[row.image_index].pixels.channels[row.channel_index].name
        expected_name = source_name
        if replace_channel_names_with_biomarkers and row.biomarker_assignment == "assigned":
            assert row.biomarker is not None
            expected_name = row.biomarker
        if row.channel_name != expected_name:
            raise UnmixingWorkflowError(
                "canonical metadata override Channel.Name does not match the "
                "explicit slide-wide replacement choice"
            )


def _pipeline_version() -> str:
    try:
        return version("mif-pipeline")
    except PackageNotFoundError:
        return "0.1.0"


def _provenance_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".provenance.json")


def _is_processed_or_tiff_source(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(".ome.tif") or name.endswith(".ome.tiff") or name.endswith(".ome.zarr")
