"""Start the desktop, a Bio-Formats probe, or the canonical cell importer.

Internal workers dispatch before Qt imports. The same installed executable can
publish finalized native cells without opening a desktop or requiring a second
Python installation. Portable diagnostics report the installed distribution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import multiprocessing
from pathlib import Path
import sys

# ==============================================================================
#                           Internal process routes
# ==============================================================================

FROZEN_INTERNAL_SWITCH_PREFIX = "--mif-internal-"
FROZEN_BIOFORMATS_PROBE_SWITCH = "--mif-internal-bioformats-probe-v1"
FROZEN_CELL_DATASET_IMPORT_SWITCH = "--mif-internal-cell-dataset-import"


# ==============================================================================
#                                Frozen bootstrap
# ==============================================================================


def _write_diagnostics(record: dict, arguments: Sequence[str]) -> None:
    """Write the requested installation report to stdout or a new JSON file."""
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if not arguments:
        if sys.stdout is not None:
            sys.stdout.write(encoded)
        return
    if len(arguments) != 2 or arguments[0] != "--result-json":
        raise ValueError("Use --result-json followed by an absolute output filename.")
    path = Path(arguments[1])
    if not path.is_absolute():
        raise ValueError("The diagnostic output path must be absolute.")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)


# ==============================================================================
#                              Application entry
# ==============================================================================


def main(
    argv: Sequence[str] | None = None,
    *,
    freeze_support: Callable[[], None] = multiprocessing.freeze_support,
    application_main: Callable[[Sequence[str]], int] | None = None,
    bioformats_probe_main: Callable[[Sequence[str]], int] | None = None,
) -> int:
    """Dispatch one invocation to the desktop or its declared image worker.

    The explicit worker switches are used by the image-reading owner when a
    packaged application requires a separate JVM. Unknown internal switches are
    rejected before loading the GUI. Callers can provide entry functions for
    embedding the launcher; ordinary users start it through ``mif-desktop``.
    """
    freeze_support()
    if getattr(sys, "frozen", False) and Path(sys.executable).stem == "mif-numerical-worker":
        from mif_pipeline.compartment_analysis.kronos_worker import main as numerical_main
        return numerical_main()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        print(
            "Usage: mif-desktop [--help] [--portable-diagnostics-json]\n\n"
            "Open the application to configure an analysis, review images and "
            "continue saved work. For command-line processing, use mif-batch."
        )
        return 0
    try:
        if arguments[:1] == ["--mif-internal-cellpose"]:
            from mif_pipeline.cell_analysis.cellpose_worker import main as cellpose_main
            return cellpose_main(arguments[1:])
        if arguments[:1] == [FROZEN_CELL_DATASET_IMPORT_SWITCH]:
            from mif_pipeline.datasets.native_cells import main as import_cells

            return import_cells(arguments[1:])
        if arguments[:1] == [FROZEN_BIOFORMATS_PROBE_SWITCH]:
            if bioformats_probe_main is None:
                from mif_pipeline.io.backends._bioformats_probe_worker import (
                    main as bioformats_probe_main,
                )
            return bioformats_probe_main(arguments[1:])
        if arguments and arguments[0].startswith(FROZEN_INTERNAL_SWITCH_PREFIX):
            raise ValueError("The internal application command is unsupported.")
        if arguments[:1] == ["--portable-diagnostics-json"]:
            from .portable_diagnostics import run_portable_diagnostics

            report = run_portable_diagnostics()
            _write_diagnostics(report.to_mapping(), arguments[1:])
            return 0 if report.passed else 1
    except Exception:
        if sys.stderr is not None:
            import traceback

            traceback.print_exc(file=sys.stderr)
        return 1
    if application_main is None:
        from .application import main as application_main
    return application_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
