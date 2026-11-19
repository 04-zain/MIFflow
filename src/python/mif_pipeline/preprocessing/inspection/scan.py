"""Build first-stage scan metadata from authoritative OME and series descriptors.

The `ChannelRecord` record carries `ordinal`, `image_index`, `channel_index`, `channel_id`,
`channel_name` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import asdict, dataclass
from io import StringIO
import csv
import json
from numbers import Integral
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Mapping

import numpy as np
from ome_types import OME

from mif_pipeline.io import (
    SeriesInfo,
    render_biomarker_assignment_csv,
    resolve_channel_metadata_index,
)

_OME_PIXEL_DTYPES = {
    "int8": np.dtype("int8"),
    "int16": np.dtype("int16"),
    "int32": np.dtype("int32"),
    "uint8": np.dtype("uint8"),
    "uint16": np.dtype("uint16"),
    "uint32": np.dtype("uint32"),
    "float": np.dtype("float32"),
    "double": np.dtype("float64"),
    "complex": np.dtype("complex64"),
    "double-complex": np.dtype("complex128"),
    "bit": np.dtype("bool"),
}


# ==============================================================================
#                                      Scan
# ==============================================================================


@dataclass(slots=True)
class ChannelRecord:
    """One OME channel and its standardized linked metadata."""

    ordinal: int
    image_index: int
    channel_index: int
    channel_id: str
    channel_name: str | None
    fluorophore: str | None
    biomarker: str | None
    biomarker_assignment: Literal["assigned", "none", "missing"]
    size_x: int
    size_y: int
    size_z: int
    size_t: int
    pixel_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScanStructureReport:
    """OME-authoritative metadata summary for preprocessing."""

    source_path: str
    image_count: int
    series_count: int
    channel_count: int
    channels: list[ChannelRecord]
    biomarkers_present_count: int
    biomarkers_complete: bool
    pyramidal: bool
    max_resolution_count: int
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _RollbackOutcome:
    recovery_paths: set[Path]
    rollback_failures: list[tuple[str, Exception]]
    cleanup_failures: list[tuple[str, Exception]]


def build_scan_structure_report(
    source_path: str | Path,
    ome: OME,
    series: tuple[SeriesInfo, ...],
) -> ScanStructureReport:
    """Summarize OME channels and pipeline-owned pyramid descriptors.

    Biomarker completeness means every channel was reviewed, not that every
    channel has a named marker. An explicit ``none`` selection is therefore
    complete while remaining absent from ``biomarkers_present_count``.
    """
    _validate_ome_series_consistency(ome, series)
    channels: list[ChannelRecord] = []
    metadata_index = resolve_channel_metadata_index(ome)

    for image_index, image in enumerate(ome.images):
        pixels = image.pixels
        for channel in metadata_index.for_image(image_index):
            channels.append(
                ChannelRecord(
                    ordinal=len(channels) + 1,
                    image_index=image_index,
                    channel_index=channel.channel_index,
                    channel_id=channel.channel_id,
                    channel_name=channel.name,
                    fluorophore=channel.fluorophore,
                    biomarker=channel.biomarker,
                    biomarker_assignment=channel.biomarker_assignment,
                    size_x=pixels.size_x,
                    size_y=pixels.size_y,
                    size_z=pixels.size_z,
                    size_t=pixels.size_t,
                    pixel_type=pixels.type.value,
                )
            )

    unreviewed_biomarker_ids = [
        channel.channel_id for channel in channels if channel.biomarker_assignment == "missing"
    ]
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
    raw = len(ome.images) == 1 and resolve_spectral_state(ome, 0).state == "raw"
    component_inventory = resolve_component_identity_inventory(ome, 0) if raw else None
    if raw:
        complete = component_inventory is not None and component_inventory.component_biomarkers is not None
        unreviewed_biomarker_ids = [] if complete else ["output component panel"]
    warnings = []
    if unreviewed_biomarker_ids:
        warnings.append(
            ("Unreviewed marker assignments: " if raw else "Unreviewed biomarker assignments for channel IDs: ")
            + ", ".join(unreviewed_biomarker_ids)
            + "."
        )

    resolution_counts = [len(item.resolutions) for item in series]
    max_resolution_count = max(resolution_counts, default=0)
    biomarkers_present_count = (sum(marker is not None for marker in (component_inventory.component_biomarkers or ()))
        if raw and component_inventory is not None else 0 if raw
        else sum(channel.biomarker is not None for channel in channels))

    return ScanStructureReport(
        source_path=str(source_path),
        image_count=len(ome.images),
        series_count=len(series),
        channel_count=len(channels),
        channels=channels,
        biomarkers_present_count=biomarkers_present_count,
        biomarkers_complete=bool(channels) and not unreviewed_biomarker_ids,
        pyramidal=max_resolution_count > 1,
        max_resolution_count=max_resolution_count,
        warnings=warnings,
    )


def write_stage1_outputs(
    ome: OME,
    series: tuple[SeriesInfo, ...],
    report: ScanStructureReport,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    stage1_selection: Mapping[str, object] | None = None,
    stage1_checkpoint: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Write authoritative inspection outputs and an editable marker template.

    The descriptive channel table is never used as configuration. A separate
    six-column assignment CSV is staged in the same rollback-protected output
    generation so the user can review markers without editing acquisition or
    raster facts.
    """
    _validate_ome_series_consistency(ome, series)
    expected_report = build_scan_structure_report(report.source_path, ome, series)
    if report != expected_report:
        raise ValueError(
            "report does not match authoritative metadata derived from OME and SeriesInfo"
        )

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    checkpoint_path = destination / "stage1-checkpoint.json"

    paths = {
        "metadata_ome_xml": destination / "metadata.ome.xml",
        "inspection_json": destination / "inspection.json",
        "channels_csv": destination / "channels.csv",
        "biomarker_assignments_csv": destination / "biomarker_assignments.csv",
        "series_inventory_csv": destination / "series_inventory.csv",
    }
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
    raw = len(ome.images) == 1 and resolve_spectral_state(ome, 0).state == "raw"
    if raw:
        paths.pop("biomarker_assignments_csv")
        paths["component_assignments_csv"] = destination / "component_assignments.csv"
    if stage1_checkpoint is not None:
        paths["stage1_checkpoint_json"] = checkpoint_path
    selection_path = destination / "stage1-selection.json"
    if stage1_selection is not None:
        paths["stage1_selection_json"] = selection_path
    if not overwrite:
        for path in (*paths.values(), checkpoint_path):
            if path.exists():
                raise FileExistsError(f"Refusing to overwrite existing file: {path}")

    payloads = {
        paths["metadata_ome_xml"]: ome.to_xml(),
        paths["inspection_json"]: json.dumps(report.to_dict(), indent=2, sort_keys=False),
        paths["channels_csv"]: _channels_csv(report.channels),
        paths["series_inventory_csv"]: _series_inventory_csv(series),
    }
    if raw:
        inventory = resolve_component_identity_inventory(ome, 0)
        output = StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("component_name", "assignment", "biomarker"))
        if inventory is not None:
            for i, name in enumerate(inventory.component_names):
                marker = inventory.component_biomarkers[i] if inventory.component_biomarkers is not None else None
                assignment = "missing" if inventory.component_biomarkers is None else "none" if marker is None else "assigned"
                writer.writerow((name, assignment, marker or ""))
        payloads[paths["component_assignments_csv"]] = output.getvalue()
    else:
        payloads[paths["biomarker_assignments_csv"]] = render_biomarker_assignment_csv(ome)
    if stage1_checkpoint is not None:
        payloads[paths["stage1_checkpoint_json"]] = (
            json.dumps(
                stage1_checkpoint,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    if stage1_selection is not None:
        payloads[paths["stage1_selection_json"]] = (
            json.dumps(
                stage1_selection,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    _write_staged_outputs(
        payloads,
        overwrite=overwrite,
        remove_paths=(
            {
                path
                for path, absent in (
                    (checkpoint_path, stage1_checkpoint is None),
                    (selection_path, stage1_selection is None),
                )
                if absent and path.exists()
            }
        ),
    )
    return {name: str(path) for name, path in paths.items()}


def _validate_ome_series_consistency(
    ome: OME,
    series: tuple[SeriesInfo, ...],
) -> None:
    """Validate the OME images against aligned base and pyramid descriptors."""
    if len(series) < len(ome.images):
        raise ValueError(
            f"OME images require {len(ome.images)} aligned series, but only "
            f"{len(series)} were supplied"
        )

    for position, item in enumerate(series):
        if item.index != position:
            raise ValueError(
                f"series index/order mismatch at position {position}: "
                f"descriptor index is {item.index}"
            )

    for image_index, image in enumerate(ome.images):
        pixels = image.pixels
        base = series[image_index].resolutions[0]
        aligned_fields = (
            ("size_x", pixels.size_x, base.size_x),
            ("size_y", pixels.size_y, base.size_y),
            ("size_z", pixels.size_z, base.size_z),
            ("size_t", pixels.size_t, base.size_t),
        )
        for field, ome_value, descriptor_value in aligned_fields:
            if ome_value != descriptor_value:
                raise ValueError(
                    f"OME image {image_index} {field}={ome_value} does not "
                    f"match aligned series {image_index} base resolution "
                    f"{field}={descriptor_value}"
                )

        channels = pixels.channels
        if len(channels) != base.size_c:
            raise ValueError(
                f"OME image {image_index} logical channel count={len(channels)} "
                f"does not match aligned series {image_index} base resolution "
                f"size_c={base.size_c}; effective channels cannot align"
            )

        channel_sample_total = 0
        for channel_index, channel in enumerate(channels):
            raw_samples = channel.samples_per_pixel
            samples_per_pixel = 1 if raw_samples is None else raw_samples
            if (
                not isinstance(samples_per_pixel, Integral)
                or isinstance(samples_per_pixel, bool)
                or samples_per_pixel <= 0
            ):
                raise ValueError(
                    f"OME image {image_index} channel {channel_index} "
                    f"samples_per_pixel={samples_per_pixel!r} must be a positive "
                    "integer"
                )
            samples_per_pixel = int(samples_per_pixel)
            if samples_per_pixel != base.rgb_samples:
                raise ValueError(
                    f"OME image {image_index} channel {channel_index} "
                    f"samples_per_pixel={samples_per_pixel} does not match aligned "
                    f"series {image_index} base resolution "
                    f"rgb_samples={base.rgb_samples}"
                )
            channel_sample_total += samples_per_pixel

        if channel_sample_total != pixels.size_c:
            raise ValueError(
                f"OME image {image_index} channel sample total="
                f"{channel_sample_total} does not match Pixels.SizeC="
                f"{pixels.size_c}"
            )

        descriptor_channels = base.size_c * base.rgb_samples
        if pixels.size_c != descriptor_channels:
            raise ValueError(
                f"OME image {image_index} effective channels={pixels.size_c} "
                f"does not match aligned series {image_index} base resolution "
                f"size_c*rgb_samples={descriptor_channels}"
            )

        ome_dtype = _OME_PIXEL_DTYPES[pixels.type.value]
        if ome_dtype != base.dtype:
            raise ValueError(
                f"OME image {image_index} dtype={ome_dtype} does not match "
                f"aligned series {image_index} base resolution dtype={base.dtype}"
            )

        ome_order = pixels.dimension_order.value
        if ome_order != base.dimension_order:
            raise ValueError(
                f"OME image {image_index} dimension_order={ome_order} does not "
                f"match aligned series {image_index} base resolution "
                f"dimension_order={base.dimension_order}"
            )

    invariant_fields = (
        "size_z",
        "size_c",
        "size_t",
        "rgb_samples",
        "dtype",
        "dimension_order",
    )
    for item in series:
        base = item.resolutions[0]
        for resolution_position, level in enumerate(item.resolutions):
            if level.index != resolution_position:
                raise ValueError(
                    f"series {item.index} resolution index/order mismatch at "
                    f"position {resolution_position}: descriptor index is {level.index}"
                )
            if resolution_position == 0:
                continue
            previous = item.resolutions[resolution_position - 1]
            if level.size_x > previous.size_x or level.size_y > previous.size_y:
                raise ValueError(
                    f"series {item.index} resolution {level.index} raster "
                    f"{level.size_x}x{level.size_y} increases from "
                    f"{previous.size_x}x{previous.size_y}"
                )
            for field in invariant_fields:
                if getattr(level, field) != getattr(base, field):
                    raise ValueError(
                        f"series {item.index} resolution {level.index} {field}="
                        f"{getattr(level, field)} does not match base resolution "
                        f"{field}={getattr(base, field)}"
                    )


def _channels_csv(channels: list[ChannelRecord]) -> str:
    fieldnames = [
        "ordinal",
        "image_index",
        "channel_index",
        "channel_id",
        "channel_name",
        "fluorophore",
        "biomarker",
        "biomarker_assignment",
        "size_x",
        "size_y",
        "size_z",
        "size_t",
        "pixel_type",
    ]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for channel in channels:
        writer.writerow(channel.to_dict())
    return output.getvalue()


def _series_inventory_csv(series: tuple[SeriesInfo, ...]) -> str:
    fieldnames = [
        "series_index",
        "series_name",
        "resolution_index",
        "size_x",
        "size_y",
        "size_z",
        "size_c",
        "size_t",
        "dtype",
        "dimension_order",
        "rgb_samples",
        "is_thumbnail",
    ]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for item in series:
        for resolution in item.resolutions:
            writer.writerow(
                {
                    "series_index": item.index,
                    "series_name": item.name,
                    "resolution_index": resolution.index,
                    "size_x": resolution.size_x,
                    "size_y": resolution.size_y,
                    "size_z": resolution.size_z,
                    "size_c": resolution.size_c,
                    "size_t": resolution.size_t,
                    "dtype": str(resolution.dtype),
                    "dimension_order": resolution.dimension_order,
                    "rgb_samples": resolution.rgb_samples,
                    "is_thumbnail": resolution.is_thumbnail,
                }
            )
    return output.getvalue()


def _write_staged_outputs(
    payloads: dict[Path, str],
    *,
    overwrite: bool,
    remove_paths: set[Path] | None = None,
) -> None:
    """Install one output generation with rollback for ordinary exceptions.

    Same-directory links and renames make each filesystem operation atomic, but
    this is not a claim of crash-atomicity for the five-file set.
    """
    removals = set() if remove_paths is None else set(remove_paths)
    if set(payloads) & removals:
        raise ValueError("staged output payloads and removals must not overlap")
    targets = tuple(payloads) + tuple(removals)
    temporary_paths: dict[Path, Path] = {}
    backup_paths: dict[Path, Path] = {}
    installed_paths: dict[Path, tuple[int, int]] = {}
    recovery_paths: set[Path] = set()
    rollback_failures: list[tuple[str, Exception]] = []
    cleanup_failures: list[tuple[str, Exception]] = []
    initiating_error: Exception | None = None
    initiating_traceback = None
    try:
        for path, payload in payloads.items():
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_paths[path] = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

        if overwrite:
            for path in targets:
                backup_path = _temporary_path(path, suffix=".backup")
                try:
                    os.replace(path, backup_path)
                except FileNotFoundError:
                    _attempt_cleanup(
                        backup_path,
                        operation=f"cleanup unused backup reservation {backup_path}",
                        failures=cleanup_failures,
                        recovery_paths=recovery_paths,
                    )
                except Exception:
                    _attempt_cleanup(
                        backup_path,
                        operation=f"cleanup failed backup reservation {backup_path}",
                        failures=cleanup_failures,
                        recovery_paths=recovery_paths,
                    )
                    raise
                else:
                    backup_paths[path] = backup_path

        for path, temporary_path in temporary_paths.items():
            identity = _file_identity(temporary_path)
            if identity is None:
                raise FileNotFoundError(
                    f"Staged output disappeared before install: {temporary_path}"
                )
            os.link(temporary_path, path)
            installed_paths[path] = identity
    except Exception as exc:
        initiating_error = exc
        initiating_traceback = exc.__traceback__
        recovery_paths.update(backup_paths.values())
        try:
            outcome = _rollback_outputs(installed_paths, backup_paths)
        except Exception as rollback_exc:
            rollback_failures.append(("rollback orchestration", rollback_exc))
        else:
            recovery_paths.difference_update(backup_paths.values())
            recovery_paths.update(outcome.recovery_paths)
            rollback_failures.extend(outcome.rollback_failures)
            cleanup_failures.extend(outcome.cleanup_failures)

    for temporary_path in temporary_paths.values():
        _attempt_cleanup(
            temporary_path,
            operation=f"cleanup staged output {temporary_path}",
            failures=cleanup_failures,
            recovery_paths=recovery_paths,
        )
    for backup_path in backup_paths.values():
        if backup_path in recovery_paths:
            continue
        _attempt_cleanup(
            backup_path,
            operation=f"cleanup confirmed-restored backup {backup_path}",
            failures=cleanup_failures,
            recovery_paths=recovery_paths,
        )

    if initiating_error is not None:
        if recovery_paths or rollback_failures or cleanup_failures:
            raise _combined_transaction_error(
                initiating_error,
                rollback_failures,
                cleanup_failures,
                recovery_paths,
            ) from initiating_error
        raise initiating_error.with_traceback(initiating_traceback)
    if recovery_paths or rollback_failures or cleanup_failures:
        raise _combined_transaction_error(
            None,
            rollback_failures,
            cleanup_failures,
            recovery_paths,
        )


def _temporary_path(path: Path, *, suffix: str) -> Path:
    with NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=suffix,
        delete=False,
    ) as handle:
        return Path(handle.name)


def _rollback_outputs(
    installed_paths: dict[Path, tuple[int, int]],
    backup_paths: dict[Path, Path],
) -> _RollbackOutcome:
    recovery_paths = set(backup_paths.values())
    rollback_failures: list[tuple[str, Exception]] = []
    cleanup_failures: list[tuple[str, Exception]] = []

    for path, identity in reversed(tuple(installed_paths.items())):
        if path in backup_paths:
            continue
        recovery_path = None
        try:
            status, recovery_path = _claim_owned_destination(
                path,
                identity,
                cleanup_failures=cleanup_failures,
                recovery_paths=recovery_paths,
            )
            if status == "owned" and recovery_path is not None:
                _attempt_cleanup(
                    recovery_path,
                    operation=f"cleanup rolled-back transaction output {recovery_path}",
                    failures=cleanup_failures,
                    recovery_paths=recovery_paths,
                )
            elif recovery_path is not None:
                recovery_paths.add(recovery_path)
            elif status == "foreign":
                _record_existing_path(
                    path,
                    operation=f"inspect preserved replacement {path}",
                    failures=rollback_failures,
                    recovery_paths=recovery_paths,
                )
        except Exception as rollback_exc:
            rollback_failures.append((f"remove transaction output {path}", rollback_exc))
            if recovery_path is not None:
                recovery_paths.add(recovery_path)
            _record_existing_path(
                path,
                operation=f"inspect residual transaction output {path}",
                failures=rollback_failures,
                recovery_paths=recovery_paths,
            )

    for path, backup_path in reversed(tuple(backup_paths.items())):
        identity = installed_paths.get(path)
        transaction_path = None
        try:
            if identity is None:
                status = "absent" if _file_identity(path) is None else "foreign"
            else:
                status, transaction_path = _claim_owned_destination(
                    path,
                    identity,
                    cleanup_failures=cleanup_failures,
                    recovery_paths=recovery_paths,
                )

            if status in {"absent", "owned"}:
                if _restore_without_overwrite(backup_path, path):
                    backup_identity = _file_identity(backup_path)
                    restored_identity = _file_identity(path)
                    if backup_identity is None or restored_identity != backup_identity:
                        raise OSError(
                            f"Could not confirm restored original for {path} " f"from {backup_path}"
                        )
                    recovery_paths.discard(backup_path)
                elif transaction_path is not None:
                    recovery_paths.add(backup_path)
                if transaction_path is not None:
                    _attempt_cleanup(
                        transaction_path,
                        operation=("cleanup displaced transaction output " f"{transaction_path}"),
                        failures=cleanup_failures,
                        recovery_paths=recovery_paths,
                    )
            elif transaction_path is not None:
                recovery_paths.add(transaction_path)
            if status == "foreign":
                _record_existing_path(
                    path,
                    operation=f"inspect preserved replacement {path}",
                    failures=rollback_failures,
                    recovery_paths=recovery_paths,
                )
        except Exception as rollback_exc:
            rollback_failures.append((f"restore displaced original for {path}", rollback_exc))
            recovery_paths.add(backup_path)
            if transaction_path is not None:
                recovery_paths.add(transaction_path)
            _record_existing_path(
                path,
                operation=f"inspect residual destination {path}",
                failures=rollback_failures,
                recovery_paths=recovery_paths,
            )

    return _RollbackOutcome(
        recovery_paths=recovery_paths,
        rollback_failures=rollback_failures,
        cleanup_failures=cleanup_failures,
    )


def _combined_transaction_error(
    initiating_error: Exception | None,
    rollback_failures: list[tuple[str, Exception]],
    cleanup_failures: list[tuple[str, Exception]],
    recovery_paths: set[Path],
) -> OSError:
    details = (
        [f"output transaction failed: {initiating_error}"]
        if initiating_error is not None
        else ["output transaction cleanup failed"]
    )
    if rollback_failures:
        details.append(
            "rollback failures: "
            + "; ".join(f"{operation}: {failure}" for operation, failure in rollback_failures)
        )
    if cleanup_failures:
        details.append(
            "cleanup failures: "
            + "; ".join(f"{operation}: {failure}" for operation, failure in cleanup_failures)
        )
    if recovery_paths:
        locations = ", ".join(str(path) for path in sorted(recovery_paths))
        details.append(f"recoverable at: {locations}")

    combined = OSError("; ".join(details))
    combined.initiating_error = initiating_error
    combined.rollback_failures = tuple(rollback_failures)
    combined.cleanup_failures = tuple(cleanup_failures)
    combined.recovery_paths = tuple(sorted(recovery_paths))
    return combined


def _attempt_cleanup(
    path: Path,
    *,
    operation: str,
    failures: list[tuple[str, Exception]],
    recovery_paths: set[Path],
) -> None:
    """Attempt one unlink without preventing any later cleanup operation."""
    try:
        path.unlink(missing_ok=True)
    except Exception as cleanup_error:
        failures.append((operation, cleanup_error))
        recovery_paths.add(path)
    else:
        recovery_paths.discard(path)


def _record_existing_path(
    path: Path,
    *,
    operation: str,
    failures: list[tuple[str, Exception]],
    recovery_paths: set[Path],
) -> None:
    try:
        identity = _file_identity(path)
    except Exception as inspection_error:
        failures.append((operation, inspection_error))
        recovery_paths.add(path)
    else:
        if identity is not None:
            recovery_paths.add(path)


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _claim_owned_destination(
    path: Path,
    identity: tuple[int, int],
    *,
    cleanup_failures: list[tuple[str, Exception]],
    recovery_paths: set[Path],
) -> tuple[str, Path | None]:
    """Move an owned path aside, preserving a replacement detected during the move."""
    if _file_identity(path) != identity:
        return "foreign", None

    claimed_path = _temporary_path(path, suffix=".rollback")
    try:
        os.replace(path, claimed_path)
    except FileNotFoundError:
        _attempt_cleanup(
            claimed_path,
            operation=f"cleanup unused rollback claim {claimed_path}",
            failures=cleanup_failures,
            recovery_paths=recovery_paths,
        )
        return "absent", None
    except Exception:
        _attempt_cleanup(
            claimed_path,
            operation=f"cleanup failed rollback claim {claimed_path}",
            failures=cleanup_failures,
            recovery_paths=recovery_paths,
        )
        raise

    recovery_paths.add(claimed_path)
    if _file_identity(claimed_path) == identity:
        return "owned", claimed_path

    if _restore_without_overwrite(claimed_path, path):
        _attempt_cleanup(
            claimed_path,
            operation=f"cleanup restored racing replacement {claimed_path}",
            failures=cleanup_failures,
            recovery_paths=recovery_paths,
        )
        return "foreign", None
    return "foreign", claimed_path


def _restore_without_overwrite(source: Path, destination: Path) -> bool:
    """Hard-link source into an absent destination without clobbering a racer."""
    try:
        os.link(source, destination)
    except FileExistsError:
        return False
    return True
