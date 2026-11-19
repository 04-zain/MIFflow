"""Locate the application's built image reader at the installation boundary.

The release build stages the native JAR and its content description together.
Profile setup copies those bytes alongside the other selected extensions; it
never downloads or compiles software while opening an image. The current
processed-image pipeline requires this built reader.
"""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path

READER_NAME = "qupath-extension-mif-0.1.0.jar"


class BundledNativeReaderError(RuntimeError):
    """Required MIF extension files in the installed application need repair."""


_REPAIR_GUIDANCE = (
    "The application's bundled MIF extension is missing, unreadable or damaged. "
    "Repair or reinstall the MIF application, then check QuPath setup again. "
    "Installing InstanSeg or Warpy will not replace these application files."
)


@lru_cache(maxsize=1)
def native_reader_description() -> dict[str, object]:
    """Read the small installed native build description once per process.

    Malformed or mismatched build metadata is an installation error, rather
    than a reason to silently switch the scientific image reader.
    """
    try:
        description = files("mif_pipeline.qupath_runtime.assets").joinpath("native", "reader.json")
        value = json.loads(description.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundledNativeReaderError(_REPAIR_GUIDANCE) from exc
    if (type(value) is not dict or set(value) != {"name", "version", "url", "size_bytes", "sha256"}
            or value["name"] != READER_NAME or value["version"] != "0.1.0"
            or value["url"] != "package://mif_pipeline.qupath_runtime.assets/native/" + READER_NAME
            or type(value["size_bytes"]) is not int or value["size_bytes"] <= 0
            or type(value["sha256"]) is not str or len(value["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["sha256"])):
        raise BundledNativeReaderError(_REPAIR_GUIDANCE)
    return value


def validate_native_reader() -> None:
    """Check bundled files before offering setup or downloading dependencies."""
    description = native_reader_description()
    resource = files("mif_pipeline.qupath_runtime.assets").joinpath("native", READER_NAME)
    digest = sha256()
    count = 0
    try:
        with resource.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                count += len(block)
    except OSError as exc:
        raise BundledNativeReaderError(_REPAIR_GUIDANCE) from exc
    if count != description["size_bytes"] or digest.hexdigest() != description["sha256"]:
        raise BundledNativeReaderError(_REPAIR_GUIDANCE)


def install_native_reader(destination: Path) -> None:
    """Copy and verify the packaged reader once into a new profile generation.

    We stream the JAR rather than retaining another full software archive in
    memory. The caller owns the unpublished profile directory and its atomic
    publication. No image, model or existing profile is changed here.
    """
    description = native_reader_description()
    resource = files("mif_pipeline.qupath_runtime.assets").joinpath("native", READER_NAME)
    digest = sha256()
    count = 0
    with resource.open("rb") as source, (destination / READER_NAME).open("xb") as output:
        while block := source.read(1024 * 1024):
            digest.update(block)
            count += len(block)
            output.write(block)
        output.flush()
        import os
        os.fsync(output.fileno())
    if count != description["size_bytes"] or digest.hexdigest() != description["sha256"]:
        raise BundledNativeReaderError(_REPAIR_GUIDANCE)
