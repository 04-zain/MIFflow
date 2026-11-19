"""Terminal adapter for the GUI-neutral stage-one workflow service."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Sequence, TextIO

from mif_pipeline.io import (
    BioFormatsBackend,
    CanonicalIngestionError,
    IngestionError,
    publish_canonical_image,
    read_biomarker_assignment_csv,
)

from .workflow import (
    apply_stage1_biomarkers,
    apply_stage1_channel_name_replacement,
    inspect_wsi,
    write_stage1_workflow,
)

# ==============================================================================
#                                      Cli
# ==============================================================================


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: BioFormatsBackend | None = None,
    publisher: Callable[..., object] = publish_canonical_image,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run a stage-one CLI command and translate expected failures to status 1.

    Dependency injection keeps argument and filesystem behavior testable while
    leaving the workflow service free of terminal concerns. Argparse retains
    its standard usage errors; operational errors become one concise line so
    batch callers can distinguish them from unexpected programming failures.
    """
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    arguments = _build_parser().parse_args(argv)

    try:
        if arguments.command == "inspect":
            return _inspect_command(arguments, backend, output_stream)
        return _apply_biomarkers_command(
            arguments,
            backend,
            publisher,
            output_stream,
        )
    except (
        CanonicalIngestionError,
            IngestionError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=error_stream)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a WSI and record explicit channel biomarkers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser(
        "inspect",
        help="inspect a WSI and save a biomarker review CSV locally",
    )
    inspect_parser.add_argument("source", type=Path)
    inspect_parser.add_argument("output_dir", type=Path)
    inspect_parser.add_argument("--image-index", type=int)
    inspect_parser.add_argument("--series-index", type=int)
    inspect_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing complete stage-one output generation",
    )

    apply_parser = commands.add_parser(
        "apply-biomarkers",
        help="validate the edited CSV and write ready stage-one metadata",
    )
    apply_parser.add_argument("output_dir", type=Path)
    replacement = apply_parser.add_mutually_exclusive_group(required=True)
    replacement.add_argument(
        "--replace-channel-names-with-biomarkers",
        action="store_true",
        dest="replace_channel_names_with_biomarkers",
        help="write assigned biomarkers into OME Channel.Name for this slide",
    )
    replacement.add_argument(
        "--keep-channel-names",
        action="store_false",
        dest="replace_channel_names_with_biomarkers",
        help="preserve source Channel.Name values in the canonical output",
    )
    return parser


def _inspect_command(
    arguments: argparse.Namespace,
    backend: BioFormatsBackend | None,
    stdout: TextIO,
) -> int:
    workflow = inspect_wsi(
        arguments.source,
        backend=backend,
        image_index=arguments.image_index,
        series_index=arguments.series_index,
    )
    written = write_stage1_workflow(
        workflow,
        arguments.output_dir,
        overwrite=arguments.overwrite,
    )
    print(f"Stage-one status: {workflow.status}", file=stdout)
    if workflow.status in {"needs_biomarkers", "needs_channel_name_replacement"}:
        assignment_path = Path(written["biomarker_assignments_csv"]).resolve()
        output_dir = Path(arguments.output_dir).expanduser().resolve()
        print(f"Biomarker review CSV saved locally: {assignment_path}", file=stdout)
        if workflow.status == "needs_biomarkers":
            print(
                "Replace every blank biomarker with a marker name or exact None. "
                "If Bio-Formats left a fluorophore blank, enter its explicit name "
                "in component_name. Every row is required; the reviewed value "
                "fills the missing Channel.Fluor in the new canonical OME-Zarr.",
                file=stdout,
            )
        else:
            print(
                "Biomarker review is complete; resolve the slide-wide channel-name choice.",
                file=stdout,
            )
        print(
            "Next: python scripts/01_inspect_wsi.py apply-biomarkers "
            f'"{output_dir}" --replace-channel-names-with-biomarkers '
            "(or --keep-channel-names)",
            file=stdout,
        )
    else:
        print("Biomarker review is already complete.", file=stdout)
    return 0


def _apply_biomarkers_command(
    arguments: argparse.Namespace,
    backend: BioFormatsBackend | None,
    publisher: Callable[..., object],
    stdout: TextIO,
) -> int:
    output_dir = Path(arguments.output_dir).expanduser().resolve()
    source_path = _source_path_from_inspection(output_dir / "inspection.json")
    selection = _selection_from_stage1(output_dir / "stage1-selection.json")
    pending = inspect_wsi(
        source_path,
        backend=backend,
        image_index=selection["source_image_index"],
        series_index=selection["source_series_index"],
    )
    if (
        pending.selection.image_id != selection["source_image_id"]
        or pending.selection.pixels_id != selection["source_pixels_id"]
        or pending.selection.series_name != selection["source_series_name"]
    ):
        raise ValueError("stage-one selection identity differs from the source")
    rows = read_biomarker_assignment_csv(output_dir / "biomarker_assignments.csv")
    reviewed = apply_stage1_biomarkers(pending, rows)
    ready = apply_stage1_channel_name_replacement(
        reviewed,
        replace_channel_names_with_biomarkers=(arguments.replace_channel_names_with_biomarkers),
    )
    if ready.status != "ready":
        raise ValueError("Biomarker review did not produce a ready workflow")
    canonical_path = output_dir / "canonical-input.ome.zarr"
    publisher(
        ready.source_path,
        canonical_path,
        ready.metadata,
        ready.selection,
        backend=backend,
        reuse_existing=True,
        require_stage1_review=True,
    )
    written = write_stage1_workflow(ready, output_dir, overwrite=True)
    metadata_path = Path(written["metadata_ome_xml"]).resolve()
    print("Stage-one status: ready", file=stdout)
    print(f"Annotated OME metadata: {metadata_path}", file=stdout)
    print(f"Canonical OME-Zarr: {canonical_path.resolve()}", file=stdout)
    return 0


def _source_path_from_inspection(path: Path) -> Path:
    """Recover the immutable WSI identity recorded by the initial inspection."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid inspection JSON in {path}: expected an object")
    source_value = payload.get("source_path")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError(f"Invalid inspection JSON in {path}: missing source_path")
    source_path = Path(source_value)
    if not source_path.is_absolute():
        raise ValueError(f"Invalid inspection JSON in {path}: source_path must be absolute")
    return source_path.resolve()


def _selection_from_stage1(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "source_image_index",
        "source_series_index",
        "source_image_id",
        "source_pixels_id",
        "source_series_name",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"Invalid stage-one selection JSON in {path}")
    if payload["schema_version"] != 1:
        raise ValueError(f"Unsupported stage-one selection schema in {path}")
    for field in ("source_image_index", "source_series_index"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Invalid {field} in {path}")
    for field in ("source_image_id", "source_pixels_id", "source_series_name"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid {field} in {path}")
    return payload
