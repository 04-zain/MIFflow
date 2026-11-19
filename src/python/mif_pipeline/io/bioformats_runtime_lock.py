"""

Resolves the complete Java-library list used by the selected Bio-Formats image reader.

We read the required 64 JARs from config/software/bioformats.json. Their
relative paths, dependency versions and complete-file checksums remain in
a recorded order. The selected external directory supplies those files when
Python opens the Java image reader.

This module checks the installed definition and associates it with the
selected directory. The shared prerequisite functions then compare the
files and open the Java/Bio-Formats combination. Source images are read only
after those checks establish the image-reading environment.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from mif_pipeline._software import read_software_description
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Mapping

from .bioformats_prerequisite import (
    BioFormatsRuntimeInventory,
    InventoryIncompatible,
    InventoryMissing,
    RuntimeJar,
    _validate_inventory,
)

BIOFORMATS_PAYLOAD_ENV = "MIF_BIOFORMATS_PAYLOAD_ROOT"
_MAX_LOCK_BYTES = 128 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                    Required Bio-Formats files and location
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BioFormatsRuntimeLock:
    """

    Retains the selected JAR definitions and supported Java version for image reading.

    Attributes:
        jars: Ordered relative filenames and expected complete-file SHA-256 values.
        coordinates: Corresponding organization:library:version dependency names in
            the same order as jars.
        supported_java_features: Java feature versions admitted by this definition;
            the current list contains Java 21.
        sha256: Checksum of the consistently encoded complete required-file list.
    """

    jars: tuple[RuntimeJar, ...]
    coordinates: tuple[str, ...]
    supported_java_features: tuple[int, ...]
    sha256: str


def _read_lock_bytes() -> bytes:
    """

    Encodes the required Bio-Formats files using the representation saved with existing image-reader checks.

    The program JSON stores the ordered file record in runtime_files and its
    separate archive locations in downloads. We encode runtime_files with sorted
    keys, fixed compact separators and a trailing newline. Its checksum therefore
    depends on the selected file/version/order values rather than the layout
    of the larger software JSON file.

    Returns:
        UTF-8 bytes of the required-file record, limited to 128 KiB.

    Raises:
        InventoryIncompatible: The installed description is missing, invalid
            or exceeds the allowed encoded size.
    """

    try:
        record = read_software_description("bioformats")["runtime_files"]
        raw = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    except (KeyError, ValueError, TypeError) as exc:
        raise InventoryIncompatible("lock_unreadable", "Cannot read the installed Bio-Formats file definition") from exc
    if len(raw) > _MAX_LOCK_BYTES:
        raise InventoryIncompatible("lock_invalid", "The installed Bio-Formats file definition is too large")
    return raw


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """

    Decodes one JSON object while rejecting repeated field names.

    Args:
        pairs: Ordered field/value pairs supplied by the JSON decoder.

    Returns:
        Dictionary retaining each field once.

    Raises:
        ValueError: A repeated key would overwrite an earlier file-definition value.
    """

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate lock key: {key}")
        result[key] = value
    return result


def load_approved_bioformats_lock() -> BioFormatsRuntimeLock:
    """

    Checks the complete installed Bio-Formats file list and its Java requirement.

    We require the current record format, Java 21 and exactly 64 ordered JAR
    entries. Every entry supplies a distinct dependency coordinate, relative
    filename and valid checksum. Filenames must remain beneath jars/ or modules/,
    with 41 entries in jars/ and 23 in modules/ for this supported distribution.

    The saved order is retained when constructing RuntimeJar values. Comparing
    that order later prevents a different Java classpath from being accepted
    under the same required-file description. The complete encoded record is
    hashed once for association with the corresponding download list.

    Returns:
        BioFormatsRuntimeLock with the checked file list, dependency coordinates,
        Java feature requirement and complete-record checksum.

    Raises:
        InventoryIncompatible: Record fields, Java version, number/order of
            entries, dependency identities, relative paths or checksums are invalid.
    """

    raw = _read_lock_bytes()
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        canonical = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if (
            raw != canonical
            or type(record) is not dict
            or set(record) != {"schema_name", "schema_version", "supported_java_features", "jars"}
            or record["schema_name"] != "bioformats-runtime-lock"
            or (type(record["schema_version"]) is not int or record["schema_version"] != 1)
            or type(record["supported_java_features"]) is not list
            or (
                len(record["supported_java_features"]) != 1
                or type(record["supported_java_features"][0]) is not int
                or record["supported_java_features"][0] != 21
            )
            or type(record["jars"]) is not list
        ):
            raise ValueError("Invalid lock schema or policy")
        if len(record["jars"]) != 64:
            raise ValueError("Invalid lock closure length")
        jars: list[RuntimeJar] = []
        coordinates: list[str] = []
        seen_paths: set[str] = set()
        seen_coordinates: set[str] = set()
        for row in record["jars"]:
            if type(row) is not dict or set(row) != {"coordinate", "relative_path", "sha256"}:
                raise ValueError("Invalid lock row")
            coordinate, path, digest = row["coordinate"], row["relative_path"], row["sha256"]
            if (
                type(coordinate) is not str
                or len(coordinate.split(":")) != 3
                or any(not part for part in coordinate.split(":"))
                or coordinate in seen_coordinates
            ):
                raise ValueError("Invalid or repeated coordinate")
            if type(path) is not str or not path or "\\" in path or ":" in path:
                raise ValueError("Invalid JAR path")
            relative = PurePosixPath(path)
            if (
                relative.is_absolute()
                or relative.as_posix() != path
                or (relative.parts[0] not in ("jars", "modules"))
                or ".." in relative.parts
                or relative.suffix != ".jar"
                or path in seen_paths
            ):
                raise ValueError("Unsafe or repeated JAR path")
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise ValueError("Invalid JAR digest")
            coordinates.append(coordinate)
            jars.append(RuntimeJar(relative, digest))
            seen_coordinates.add(coordinate)
            seen_paths.add(path)
        if sum(path.relative_path.parts[0] == "jars" for path in jars) != 41 or (
            sum(path.relative_path.parts[0] == "modules" for path in jars) != 23
        ):
            raise ValueError("Invalid lock layout")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, IndexError) as exc:
        raise InventoryIncompatible("lock_invalid", "Invalid installed Bio-Formats lock") from exc
    return BioFormatsRuntimeLock(tuple(jars), tuple(coordinates), (21,), sha256(raw).hexdigest())


def require_approved_bioformats_inventory(inventory: BioFormatsRuntimeInventory) -> None:
    """

    Compares a proposed reader configuration with the installed ordered JAR definitions.

    Args:
        inventory: BioFormatsRuntimeInventory containing a selected directory,
            ordered JARs and admitted Java feature versions.

    Raises:
        InventoryIncompatible: Its JAR paths/order/checksums or Java version list
            differs from the selected software definition.
    """

    lock = load_approved_bioformats_lock()
    if not isinstance(inventory, BioFormatsRuntimeInventory) or (
        inventory.jars != lock.jars
        or inventory.supported_java_features != lock.supported_java_features
    ):
        raise InventoryIncompatible(
            "inventory_not_approved", "Runtime inventory differs from installed Bio-Formats lock"
        )


def verify_approved_bioformats_payload(payload_root: Path) -> BioFormatsRuntimeInventory:
    """

    Checks the selected directory's files against the complete installed Bio-Formats definition.

    We combine the absolute directory with the ordered relative JAR paths and
    supported Java version, then call the shared file verifier. The resulting
    description can be supplied to the separate process that tests Java loading.

    Args:
        payload_root: Absolute external directory containing jars/ and modules/.

    Returns:
        BioFormatsRuntimeInventory after the installed files have passed verification.

    Raises:
        ValueError: The root is not an absolute Path or contains '..'.
        InventoryMissing or InventoryIncompatible: Required files are absent
            or fail their path, type or checksum comparisons.
    """

    if (
        not isinstance(payload_root, Path)
        or not payload_root.is_absolute()
        or ".." in payload_root.parts
    ):
        raise ValueError("Bio-Formats payload root must be an absolute clean Path")
    lock = load_approved_bioformats_lock()
    inventory = BioFormatsRuntimeInventory(payload_root, lock.jars, lock.supported_java_features)
    _validate_inventory(inventory)
    return inventory


def require_external_bioformats_root(
    payload_root: Path | None,
    *,
    frozen_root: Path | None = None,
) -> None:
    """

    Checks that a frozen application uses an external Bio-Formats installation.

    We obtain the frozen application's extraction directory from the supplied
    path or sys._MEIPASS. Its files must not contain a bioformats-runtime folder,
    and a selected Bio-Formats directory must lie outside the application.
    Source installations without a frozen root have no additional package-location check.

    Args:
        payload_root: Optional selected external Bio-Formats directory.
        frozen_root: Optional explicit frozen-application directory.

    Raises:
        InventoryIncompatible: Bio-Formats was included in the frozen application
            or the selected path points inside that application's directory.
    """

    selected_frozen = (
        Path(sys._MEIPASS) if frozen_root is None and getattr(sys, "frozen", False) else frozen_root
    )
    if selected_frozen is not None:
        bundle = Path(selected_frozen).resolve()
        included = bundle / "bioformats-runtime"
        if included.exists() or included.is_symlink():
            raise InventoryIncompatible(
                "bundled_runtime_unsupported",
                "This application includes unsupported Bio-Formats files. Reinstall the app.",
            )
        if payload_root is not None and payload_root.resolve().is_relative_to(bundle):
            raise InventoryIncompatible(
                "payload_inside_bundle",
                "Select or install Bio-Formats outside the application folder.",
            )


def resolve_bioformats_runtime_inventory(
    *, environ: Mapping[str, str] | None = None, frozen_root: Path | None = None
) -> BioFormatsRuntimeInventory:
    """

    Associates the selected external image-reader directory with its installed JAR and Java definitions.

    We read MIF_BIOFORMATS_PAYLOAD_ROOT from the provided environment. The desktop
    launch supplies this value after Software setup has selected the directory;
    a command-line environment may provide it directly. The directory must be
    absolute, exist and satisfy the frozen-application location check.

    Args:
        environ: Optional environment mapping; omission reads the current process environment.
        frozen_root: Optional frozen-application directory used by the location check.

    Returns:
        BioFormatsRuntimeInventory combining the selected folder and ordered
        installed-file description for the later prerequisite verification.

    Raises:
        InventoryMissing: No suitable absolute existing directory was supplied.
        InventoryIncompatible: The selected or packaged location is unsupported,
            or the installed file definition is invalid.
    """

    variables = os.environ if environ is None else environ
    raw = variables.get(BIOFORMATS_PAYLOAD_ENV)
    root = Path(raw) if isinstance(raw, str) and raw else None
    require_external_bioformats_root(root, frozen_root=frozen_root)
    if root is None or not root.is_absolute() or ".." in root.parts:
        raise InventoryMissing(
            "payload_absent", "Set MIF_BIOFORMATS_PAYLOAD_ROOT to an absolute directory"
        )
    if not root.is_dir():
        raise InventoryMissing("payload_absent", f"Missing Bio-Formats payload directory: {root}")
    lock = load_approved_bioformats_lock()
    return BioFormatsRuntimeInventory(root, lock.jars, lock.supported_java_features)
