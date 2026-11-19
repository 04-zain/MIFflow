"""Choose a source-pixel export rectangle in a temporary native image view."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mif_pipeline.qupath_runtime.runner import QuPathRunner
    from .saved_reconstruction_figures import ReconstructionRecord


def choose_region(runner: QuPathRunner, record: ReconstructionRecord,
                  initial: dict[str, int], output_root: Path, *,
                  appearance_path: Path | None = None) -> dict[str, int] | None:
    """Open the accepted prepared image; return coordinates without importing annotations.

    Reconstruction has proved that prepared and source level-zero grids have
    equal dimensions and physical calibration. QuPath returns that grid even
    when the viewer displays a lower-resolution pyramid.
    """
    from mif_pipeline.qupath_runtime.scripts import materialize_evidence_script, QuPathScriptAsset
    from mif_pipeline.gui.image_appearance import load_image_appearance
    from .saved_reconstruction_figures import MAX_SIDE
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix='export-region-', dir=output_root))
    script = materialize_evidence_script(folder, QuPathScriptAsset.EXPORT_REGION_SELECTION)
    request = dict(image_path=str(record.image_path), size_x=record.size_x, size_y=record.size_y,
                   pixel_width_um=record.scale[0], pixel_height_um=record.scale[1],
                   max_width=MAX_SIDE, max_height=MAX_SIDE, initial=initial)
    appearance = load_image_appearance(appearance_path, record.image_path.absolute().as_uri())
    if appearance is not None:
        # Preferences are optional display data. Invalid or oversized values must
        # not stop field selection; leave room inside the native request bound.
        try:
            payload = json.dumps(appearance, allow_nan=False)
            if len(payload.encode('utf-8')) <= 128 * 1024:
                request['appearance'] = appearance
        except (TypeError, ValueError):
            pass
    request_path, result = folder / 'request.json', folder / 'region.json'
    request_path.write_text(json.dumps(request, allow_nan=False))
    completed = runner.run_interactive_image_region(record.image_path, script=script,
        request_path=request_path, result_path=result, log_directory=folder / 'logs')
    if (getattr(completed, 'returncode', None) != 0
            or getattr(getattr(completed, 'drain_evidence', None), 'termination_reason', None) != 'normal_exit'):
        raise ValueError('QuPath did not close normally. The existing export coordinates were retained.')
    if not result.exists():
        return None
    if result.is_symlink() or not result.is_file() or result.stat().st_size > 1024:
        raise ValueError('QuPath did not return a valid export rectangle.')
    region = json.loads(result.read_text())
    if (not isinstance(region, dict) or set(region) != {'x', 'y', 'width', 'height'}
            or any(type(value) is not int for value in region.values())):
        raise ValueError('QuPath did not return whole-pixel export coordinates.')
    return region
