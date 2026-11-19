"""Locate and inspect the isolated Nimbus worker included with the application.

The desktop and scoring worker have separate numerical libraries. We launch
the worker directly; researchers never select or activate a Python environment.
Software setup checks imports without loading weights or opening an image.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Literal


@dataclass(frozen=True, slots=True)
class NimbusRuntimeStatus:
    """Report the bundled executable and the outcome of its import check."""

    state: Literal["ready", "missing", "incompatible"]
    location: Path
    detail: str


def nimbus_runtime_root() -> Path:
    """Return the runtime beside the desktop or inside its source environment.

    Portable releases carry the complete worker under ``runtimes/nimbus``.
    A source installation's builder places the same worker beneath its Python
    prefix. Neither route searches PATH or borrows another Python environment.
    """
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(sys.prefix)
    return base / "runtimes" / "nimbus"


def nimbus_worker_path(root: Path | None = None) -> Path:
    """Select the platform's bundled worker executable."""
    return (root or nimbus_runtime_root()) / ("mif-nimbus.exe" if os.name == "nt" else "mif-nimbus")


def nimbus_worker_environment() -> dict[str, str]:
    """Keep the desktop's frozen libraries out of the worker's import search.

    PyInstaller alters the parent library path. Its original value, when
    present, is restored for this independent process; the worker bootloader
    then selects its own libraries. Changes apply only to the child's copy.
    """
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    if getattr(sys, "frozen", False):
        for name in ("LD_LIBRARY_PATH", "LIBPATH"):
            original = environment.pop(name + "_ORIG", None)
            if original is None:
                environment.pop(name, None)
            else:
                environment[name] = original
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def nimbus_runtime_identity(root: Path | None = None) -> str:
    """Bind retained scores to the worker sources and pinned dependency build."""
    path = (root or nimbus_runtime_root()) / "runtime.json"
    content = path.read_bytes()
    description = json.loads(content)
    if not isinstance(description, dict) or description.get("schema") != "mif-nimbus-runtime/v1":
        raise ValueError("The bundled Nimbus runtime description is unsupported")
    return hashlib.sha256(content).hexdigest()


def inspect_nimbus_runtime(root: Path | None = None) -> NimbusRuntimeStatus:
    """Check the packaged imports once without loading the model or source data.

    A missing or incompatible worker requires repair of the application on
    the processing computer. The bounded subprocess returns dependency
    versions from the same executable used for subsequent scoring.
    """
    executable = nimbus_worker_path(root)
    if not executable.is_file():
        return NimbusRuntimeStatus("missing", executable,
            "Nimbus scoring software is missing from this application.")
    try:
        nimbus_runtime_identity(root)
        result = subprocess.run(
            [str(executable), "--check-runtime"], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60, check=False,
            env=nimbus_worker_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", errors="replace")[-4000:] or "The scoring worker could not load its libraries")
        versions = json.loads(result.stdout)
        if (versions.get("Nimbus-Inference") != "0.0.5"
                or versions.get("numpy") != "1.26.4"
                or str(versions.get("torch", "")).split("+")[0] != "2.7.1"
                or versions.get("opencv") != "4.11.0"):
            raise ValueError("The scoring worker reported different numerical dependencies")
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return NimbusRuntimeStatus("incompatible", executable,
            "Nimbus scoring software could not be verified. " + str(exc))
    return NimbusRuntimeStatus("ready", executable,
        "Nimbus scoring software is included and ready. No Python setup is required.")
