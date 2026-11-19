"""Read-only checks for the portable desktop runtime.

The `PortableDiagnosticReport` record carries `schema_name`, `schema_version`, `platform`,
`architecture`, `frozen` and its remaining declared fields.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import multiprocessing
import platform
import sys
import tempfile
from typing import Literal

from .binding import QtBindingInfo, require_supported_binding

_RESOURCE_NAMES = (
    "spectral_library",
    "release_compatibility",
    "qupath_scripts",
    "qt_binding",
)


# ==============================================================================
#                              Portable diagnostics
# ==============================================================================


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("diagnostic check name must be non-empty")
        if type(self.passed) is not bool:
            raise TypeError("diagnostic check result must be boolean")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("diagnostic check detail must be non-empty")

    def to_mapping(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PortableDiagnosticReport:
    schema_name: Literal["mif-portable-diagnostics"]
    schema_version: Literal[1]
    platform: str
    architecture: str
    frozen: bool
    qt_binding: str
    qt_version: str
    resource_checks: tuple[DiagnosticCheck, ...]
    spawn_check: DiagnosticCheck

    def __post_init__(self) -> None:
        if (self.schema_name, self.schema_version) != ("mif-portable-diagnostics", 1):
            raise ValueError("portable diagnostics schema is unsupported")
        for name in ("platform", "architecture", "qt_binding", "qt_version"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be non-empty")
        if type(self.frozen) is not bool:
            raise TypeError("frozen must be boolean")
        if (
            type(self.resource_checks) is not tuple
            or not all(type(check) is DiagnosticCheck for check in self.resource_checks)
            or tuple(check.name for check in self.resource_checks) != _RESOURCE_NAMES
        ):
            raise ValueError("portable resource check inventory is incomplete")
        if (
            type(self.spawn_check) is not DiagnosticCheck
            or self.spawn_check.name != "preprocessing_spawn"
        ):
            raise ValueError("portable spawn check is invalid")

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.resource_checks) and self.spawn_check.passed

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "frozen": self.frozen,
            "qt_binding": self.qt_binding,
            "qt_version": self.qt_version,
            "resource_checks": [check.to_mapping() for check in self.resource_checks],
            "spawn_check": self.spawn_check.to_mapping(),
        }


def _checked(name: str, probe: Callable[[], str]) -> DiagnosticCheck:
    try:
        return DiagnosticCheck(name, True, probe())
    except Exception as exc:
        return DiagnosticCheck(name, False, f"{type(exc).__name__}: {exc}")


def _resource_checks() -> tuple[DiagnosticCheck, ...]:
    def spectral_library() -> str:
        from mif_pipeline.preprocessing.unmixing import load_motif_spectral_library

        return load_motif_spectral_library().sha256

    def release_compatibility() -> str:
        from mif_pipeline.pipeline_core import ReleaseCompatibilityManifest

        manifest = ReleaseCompatibilityManifest.load()
        return f"{manifest.schema_version}; {len(manifest.families)} families"

    def qupath_scripts() -> str:
        from mif_pipeline.qupath_runtime.scripts import registered_script_bundle_sha256

        return registered_script_bundle_sha256()

    return (
        _checked("spectral_library", spectral_library),
        _checked("release_compatibility", release_compatibility),
        _checked("qupath_scripts", qupath_scripts),
    )


def _tiny_unmixing_probe() -> str:
    """Exercise the production bounded solve inside the frozen spawned child."""
    from importlib.metadata import version
    from pathlib import Path

    import numpy as np
    import zarr

    from mif_pipeline.io import DigitalRangeAuthority, EffectiveDigitalRange, Region
    from mif_pipeline.io.metadata.canonical import OffsetAuthority, UnmixingInputMetadata
    from mif_pipeline.preprocessing.unmixing.engine import (
        RegionExecutionPolicy,
        process_regions_to_zarr,
    )
    from mif_pipeline.preprocessing.unmixing.library import SpectralLibrary
    from mif_pipeline.preprocessing.unmixing.normalization import (
        CanonicalOffsetExposureNormalization,
    )
    from mif_pipeline.preprocessing.unmixing.solvers import ClippedLeastSquaresSolver
    from mif_pipeline.preprocessing.unmixing.validation import (
        SolverKind,
        SpectralState,
        UnmixingConfig,
        ValidatedUnmixingInput,
    )
    from mif_pipeline.preprocessing.unmixing.workflow import _region_execution_runtime

    matrix = np.eye(2, dtype=np.float64)
    column_sums = np.ones(2, dtype=np.float64)
    library = SpectralLibrary(
        identifier="portable-diagnostic",
        sha256="synthetic",
        band_names=("b0", "b1"),
        component_names=("c0", "c1"),
        fluorophores=("c0", "c1"),
        biomarkers=(None, None),
        matrix=matrix,
        column_sums=column_sums,
        autofluorescence_components=(),
    )
    acquisition = UnmixingInputMetadata(
        image_index=0,
        spectral_state="raw",
        state_source="metadata",
        band_order=library.band_names,
        exposure_seconds=(1.0, 1.0),
        gain=(1.0, 1.0),
        binning=(1, 1),
        binning_area=(1, 1),
        source_storage_dtype="uint8",
        source_storage_bits=8,
        source_storage_max=255.0,
        camera_bit_depth=(8, 8),
        autofluorescence_subtracted=False,
        offsets=(0.0, 0.0),
        offset_authority=OffsetAuthority.DECLARED_ALREADY_CORRECTED,
    )
    digital_range = EffectiveDigitalRange(
        container_dtype="uint8",
        container_bits=8,
        meaningful_bits=8,
        maximum=255.0,
        authority=DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE,
    )
    factors = CanonicalOffsetExposureNormalization().build_factors(acquisition, digital_range)
    validated = ValidatedUnmixingInput(
        image_index=0,
        spectral_state=SpectralState.RAW,
        state_source="metadata",
        skip_unmixing=False,
        size_x=2,
        size_y=1,
        input_band_count=2,
        component_count=2,
        band_indices=(0, 1),
        acquisition=acquisition,
        config=UnmixingConfig(solver=SolverKind.CLIPPED_LS, chunk_size=(1, 1)),
    )

    class SyntheticSource:
        def __init__(self) -> None:
            self.raw = np.asarray((((255, 0),), ((0, 255),)), dtype=np.uint8)
            self.reads: list[tuple[int, Region]] = []

        def read_region(self, *, series, resolution, plane, region):
            if series != 0 or resolution != 0:
                raise ValueError("diagnostic source request differs")
            self.reads.append((plane.c, region))
            return self.raw[
                plane.c, region.y : region.y + region.height, region.x : region.x + region.width
            ].copy()

    source = SyntheticSource()
    with tempfile.TemporaryDirectory(prefix="mif-unmixing-diagnostic-") as temporary:
        destination = Path(temporary) / "output"
        summary = process_regions_to_zarr(
            source,
            validated,
            library,
            factors,
            ClippedLeastSquaresSolver(matrix),
            destination,
            execution_policy=RegionExecutionPolicy(2, 2, 4 * 1024**2),
        )
        output = np.asarray(zarr.open_group(destination, mode="r")["0"])
        expected = np.asarray((((1.0, 0.0),), ((0.0, 1.0),)), dtype=np.float32)
        np.testing.assert_allclose(output, expected, rtol=0, atol=1e-6)
        if (
            output.dtype != np.dtype("float32")
            or summary.region_count != 2
            or summary.max_region_pixels != 1
            or summary.diagnostics.total_pixels != 2
            or summary.diagnostics.ls_fast_path_pixels != 2
            or summary.diagnostics.constrained_pixels != 0
            or source.reads
            != [
                (0, Region(0, 0, 1, 1)),
                (1, Region(0, 0, 1, 1)),
                (0, Region(1, 0, 1, 1)),
                (1, Region(1, 0, 1, 1)),
            ]
        ):
            raise ValueError("tiny unmixing region counts or source reads differ")
    if destination.exists():
        raise ValueError("tiny unmixing output was not removed")

    runtime = dict(_region_execution_runtime())
    policy = RegionExecutionPolicy()
    expected_runtime = {
        "scheduler": "concurrent.futures.shards",
        "scheduler_version": platform.python_version(),
        "scheduling_policy": "bounded-regions/v1",
        "max_workers": str(policy.max_workers),
        "max_inflight_regions": str(policy.max_inflight_regions),
        "working_array_budget_bytes": str(policy.working_array_budget_bytes),
        "threadpoolctl_version": version("threadpoolctl"),
    }
    if (
        any(runtime.get(key) != value for key, value in expected_runtime.items())
        or not runtime.get("scheduler_version")
        or not runtime.get("threadpoolctl_version")
    ):
        raise ValueError("tiny unmixing execution runtime facts differ")
    return (
        "two-region LS output passed; "
        f"python={runtime['scheduler_version']}; "
        f"threadpoolctl={runtime['threadpoolctl_version']}"
    )


def _spawn_roundtrip(context: multiprocessing.context.BaseContext) -> DiagnosticCheck:
    from mif_pipeline.orchestration.batch_preprocessing_wire import (
        PREPROCESSING_JOB_REQUEST_SCHEMA_NAME,
        PreprocessingJobRequest,
        canonical_bytes,
    )

    request = PreprocessingJobRequest(
        schema_name=PREPROCESSING_JOB_REQUEST_SCHEMA_NAME,
        schema_version=1,
        node_id="node-" + "1" * 48,
        node_kind="inspection",
        node_version="1",
        node_material_sha256="a" * 64,
        adapter_spec_sha256="b" * 64,
        authorization_sha256="c" * 64,
        resolved_config_sha256="d" * 64,
        node_json=canonical_bytes({"kind": "inspection"}),
        authorization_json=canonical_bytes({"attempt_id": "diagnostic"}),
        resolved_sample_json=canonical_bytes({"sample_id": "diagnostic"}),
        input_artifact_refs_json=canonical_bytes([]),
        accepted_interaction_response_json=None,
    )
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as executor:
        returned = executor.submit(
            PreprocessingJobRequest.from_mapping, request.to_mapping()
        ).result(timeout=30)
        if returned != request:
            raise ValueError("preprocessing request changed across spawn")
        numerical_detail = executor.submit(_tiny_unmixing_probe).result(timeout=30)
    return DiagnosticCheck(
        "preprocessing_spawn",
        True,
        f"spawn DTO roundtrip passed; {numerical_detail}",
    )


def run_portable_diagnostics(
    *,
    spawn_probe: Callable[
        [multiprocessing.context.BaseContext], DiagnosticCheck
    ] = _spawn_roundtrip,
    binding_probe: Callable[[], QtBindingInfo] = require_supported_binding,
    resource_probe: Callable[[], tuple[DiagnosticCheck, ...]] = _resource_checks,
) -> PortableDiagnosticReport:
    """Check packaged resources and one child process without opening a workspace."""

    try:
        resources = resource_probe()
    except Exception as exc:
        resources = tuple(
            DiagnosticCheck(name, False, f"{type(exc).__name__}: {exc}")
            for name in _RESOURCE_NAMES[:-1]
        )
    try:
        binding = binding_probe()
        qt_binding, qt_version = binding.binding, binding.qt_version
        binding_check = DiagnosticCheck("qt_binding", True, qt_version)
    except Exception as exc:
        qt_binding = qt_version = "unavailable"
        binding_check = DiagnosticCheck("qt_binding", False, f"{type(exc).__name__}: {exc}")
    try:
        context = multiprocessing.get_context("spawn")
        spawn_check = spawn_probe(context)
    except Exception as exc:
        spawn_check = DiagnosticCheck("preprocessing_spawn", False, f"{type(exc).__name__}: {exc}")
    return PortableDiagnosticReport(
        schema_name="mif-portable-diagnostics",
        schema_version=1,
        platform=sys.platform,
        architecture=platform.machine() or "unknown",
        frozen=bool(getattr(sys, "frozen", False)),
        qt_binding=qt_binding,
        qt_version=qt_version,
        resource_checks=(*resources, binding_check),
        spawn_check=spawn_check,
    )
