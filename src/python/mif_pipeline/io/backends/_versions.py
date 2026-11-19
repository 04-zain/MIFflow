"""Pinned Bio-Formats dependency policy and runtime checks."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import MutableMapping

from .base import BackendVersions
from .errors import DependencyVersionError

BFFILE_VERSION = "0.1.1"
BIOFORMATS_MAVEN_COORDINATE = "ome:formats-gpl:8.5.0"
BIOFORMATS_RUNTIME_VERSION = "8.5.0"


# ==============================================================================
#                                    Versions
# ==============================================================================


def configure_bioformats_environment(
    environ: MutableMapping[str, str] = os.environ,
) -> None:
    """Pin the process-global Bio-Formats Maven coordinate before import."""

    configured = environ.get("BIOFORMATS_VERSION")
    if configured is None:
        environ["BIOFORMATS_VERSION"] = BIOFORMATS_MAVEN_COORDINATE
    elif configured != BIOFORMATS_MAVEN_COORDINATE:
        raise DependencyVersionError(
            f"BIOFORMATS_VERSION={configured!r}; expected " f"{BIOFORMATS_MAVEN_COORDINATE!r}"
        )


def load_bffile() -> ModuleType:
    """Load bffile only after the approved local JVM has been attested."""

    configure_bioformats_environment()
    from ._offline_runtime import (
        ensure_offline_bioformats_runtime,
        selected_offline_bioformats_inventory,
    )

    ensure_offline_bioformats_runtime(selected_offline_bioformats_inventory())
    return importlib.import_module("bffile")


def read_backend_versions(module: ModuleType) -> BackendVersions:
    """Verify the installed bridge and the loaded Java runtime independently."""

    bffile_version = module.__version__
    maven_coordinate = module.BioFile.bioformats_maven_coordinate()
    runtime_version = module.BioFile.bioformats_version()
    _require_version("bffile", bffile_version, BFFILE_VERSION)
    _require_version("Bio-Formats Maven coordinate", maven_coordinate, BIOFORMATS_MAVEN_COORDINATE)
    _require_version("Bio-Formats runtime", runtime_version, BIOFORMATS_RUNTIME_VERSION)
    return BackendVersions(
        backend="bioformats",
        bffile_version=bffile_version,
        bioformats_maven_coordinate=maven_coordinate,
        bioformats_runtime_version=runtime_version,
    )


def _require_version(name: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise DependencyVersionError(f"{name}={actual!r}; expected {expected!r}")
