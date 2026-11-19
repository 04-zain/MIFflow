"""Configure the process-local Java runtime and Bio-Formats classpath.

The selected Java home and verified JAR inventory are bound before the JVM is used.
Subsequent image readers share that selection. Once initialization has occurred, an
incompatible runtime change requires a new process rather than changing the libraries
beneath an active reader."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from hashlib import sha256
import importlib
import os
from pathlib import Path, PurePosixPath
import sys
from threading import RLock
from typing import Callable

from ..bioformats_prerequisite import (
    BioFormatsRuntimeEvidence,
    BioFormatsRuntimeInventory,
    InventoryIncompatible,
    InventoryMissing,
    LoadedJavaClass,
    _validate_inventory,
)
from ..bioformats_runtime_lock import (
    BIOFORMATS_PAYLOAD_ENV,
    require_approved_bioformats_inventory,
    require_external_bioformats_root,
    resolve_bioformats_runtime_inventory,
)
from ._versions import (
    BFFILE_VERSION,
    BIOFORMATS_MAVEN_COORDINATE,
    BIOFORMATS_RUNTIME_VERSION,
)
from .errors import DependencyVersionError

_REQUIRED_CLASSES = (
    "loci.formats.FormatTools",
    "loci.formats.in.VectraReader",
    "loci.formats.tools.ImageConverter",
)
_TOOLS_COORDINATE = "ome:bio-formats-tools:8.5.0"
_runtime_lock = RLock()
_selected_inventory: BioFormatsRuntimeInventory | None = None
_active_runtime: BioFormatsRuntimeEvidence | None = None
_active_payload_root: Path | None = None
_configured_java_home: Path | None = None
_configured_bioformats_root: Path | None = None


# ==============================================================================
#                                Offline runtime
# ==============================================================================


def _explicit_java_library(java_home: Path) -> Path:
    if (
        not isinstance(java_home, Path)
        or not java_home.is_absolute()
        or (".." in java_home.parts or not java_home.is_dir())
    ):
        raise DependencyVersionError("java_home_invalid: Select an absolute Java directory")
    relative = (
        Path("bin/server/jvm.dll")
        if sys.platform == "win32"
        else Path("lib/server/libjvm.so") if sys.platform.startswith("linux") else None
    )
    if relative is None:
        raise DependencyVersionError(f"java_platform_unsupported: {sys.platform}")
    library = java_home / relative
    if not library.is_file():
        raise DependencyVersionError(f"jvm_library_missing: {library}")
    return library


def configure_offline_runtime_selection(
    *,
    java_home: Path | None = None,
    bioformats_root: Path | None = None,
) -> None:
    """Choose external dependencies before the IO owner binds its runtime."""
    global _configured_java_home, _configured_bioformats_root
    with _runtime_lock:
        if (_selected_inventory is not None or _active_runtime is not None) and (
            java_home,
            bioformats_root,
        ) != (_configured_java_home, _configured_bioformats_root):
            raise DependencyVersionError(
                "runtime_selection_frozen: restart the application to change Java or Bio-Formats"
            )
        if java_home is not None:
            _explicit_java_library(java_home)
        if bioformats_root is not None:
            if (
                not isinstance(bioformats_root, Path)
                or not bioformats_root.is_absolute()
                or ".." in bioformats_root.parts
                or not bioformats_root.is_dir()
            ):
                raise DependencyVersionError(
                    "payload_root_invalid: Select an absolute Bio-Formats directory"
                )
        try:
            require_external_bioformats_root(bioformats_root)
        except InventoryIncompatible as exc:
            raise DependencyVersionError(str(exc)) from exc
        _configured_java_home = java_home
        _configured_bioformats_root = bioformats_root


def selected_java_home() -> Path | None:
    """Return the explicitly chosen Java home, if any."""
    with _runtime_lock:
        return _configured_java_home


def selected_offline_bioformats_inventory() -> BioFormatsRuntimeInventory:
    """Bind the explicit external selection without scanning its JARs."""
    global _selected_inventory
    with _runtime_lock:
        if _selected_inventory is None:
            environment = (
                {BIOFORMATS_PAYLOAD_ENV: str(_configured_bioformats_root)}
                if _configured_bioformats_root is not None
                else None
            )
            _selected_inventory = resolve_bioformats_runtime_inventory(environ=environment)
        return _selected_inventory


def _start_verified_jvm(
    paths: tuple[Path, ...],
    *,
    java_home: Path | None = None,
) -> None:
    for name in ("CLASSPATH", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        if os.environ.get(name):
            raise DependencyVersionError(
                f"unverified_jvm_options: Unset {name} before starting offline Bio-Formats"
            )
    import jpype

    if jpype.isJVMStarted():
        raise DependencyVersionError("unverified JVM was already started")
    chosen = java_home if java_home is not None else selected_java_home()
    if chosen is not None:
        jvm_path = str(_explicit_java_library(chosen))
    else:
        try:
            jvm_path = jpype.getDefaultJVMPath()
        except jpype.JVMNotFoundException as exc:
            raise DependencyVersionError(f"java_jvm_missing: {exc}") from exc
        except jpype.JVMNotSupportedException as exc:
            raise DependencyVersionError(f"java_jvm_unsupported: {exc}") from exc
        except OSError as exc:
            raise DependencyVersionError(f"java_jvm_lookup_failed: {exc}") from exc
    try:
        jpype.startJVM(
            jvm_path,
            classpath=[str(path) for path in paths],
            convertStrings=False,
        )
    except jpype.JVMNotSupportedException as exc:
        raise DependencyVersionError(f"java_jvm_unsupported: {exc}") from exc
    except OSError as exc:
        raise DependencyVersionError(f"java_start_failed: {exc}") from exc


def _loaded_origin(class_name: str) -> Path:
    import jpype

    try:
        java_class = jpype.JClass("java.lang.Class").forName(class_name)
    except Exception as exc:
        raise DependencyVersionError(
            f"required_class_missing: Missing {class_name}: {exc}"
        ) from exc
    source = java_class.getProtectionDomain().getCodeSource()
    if source is None or source.getLocation() is None:
        raise DependencyVersionError(f"class_code_source_missing: No code source for {class_name}")
    try:
        return Path(
            str(jpype.JClass("java.nio.file.Paths").get(source.getLocation().toURI()).toString())
        ).resolve()
    except Exception as exc:
        raise DependencyVersionError(
            f"class_origin_invalid: Invalid code source for {class_name}: {exc}"
        ) from exc


def _hash_loaded_jar(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise DependencyVersionError(
            f"class_origin_unreadable: Cannot hash loaded JAR {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _attest_validated_classpath(
    paths: tuple[Path, ...],
    digest: str,
    policy: tuple[int, ...],
    *,
    start_jvm: Callable[[tuple[Path, ...]], None] | None = None,
    class_origin: Callable[[str], Path] | None = None,
    expected_java_home: Path | None = None,
) -> BioFormatsRuntimeEvidence:
    """Start and attest one already validated classpath in a fresh process."""
    os.environ["BFF_JAVA_FETCH"] = "never"
    os.environ["BIOFORMATS_VERSION"] = BIOFORMATS_MAVEN_COORDINATE
    try:
        import jpype

        (start_jvm or _start_verified_jvm)(paths)
    except ModuleNotFoundError as exc:
        raise DependencyVersionError(f"jpype_unavailable: {exc}") from exc

    system = jpype.JClass("java.lang.System")
    java_version = str(system.getProperty("java.version"))
    specification = str(system.getProperty("java.specification.version"))
    java_home = str(system.getProperty("java.home"))
    selected_home = expected_java_home if expected_java_home is not None else selected_java_home()
    if selected_home is not None and Path(java_home).resolve() != selected_home.resolve():
        raise DependencyVersionError(
            f"java_home_mismatch: Loaded Java home {java_home} differs from {selected_home}"
        )
    try:
        feature = int(specification)
    except ValueError as exc:
        raise DependencyVersionError(f"java_feature_invalid: {specification}") from exc
    if feature not in policy:
        raise DependencyVersionError(f"java_feature_unsupported: {feature}")

    try:
        module = importlib.import_module("bffile")
    except ModuleNotFoundError as exc:
        raise DependencyVersionError(f"bffile_unavailable: {exc}") from exc
    if str(module.__version__) != BFFILE_VERSION:
        raise DependencyVersionError(f"runtime_version_mismatch: bffile={module.__version__!r}")
    coordinate = str(module.BioFile.bioformats_maven_coordinate())
    if coordinate != BIOFORMATS_MAVEN_COORDINATE:
        raise DependencyVersionError(
            f"runtime_version_mismatch: Bio-Formats coordinate={coordinate!r}"
        )
    try:
        runtime_version = str(jpype.JClass("loci.formats.FormatTools").VERSION)
    except Exception as exc:
        raise DependencyVersionError(
            f"required_class_missing: Missing loci.formats.FormatTools: {exc}"
        ) from exc
    if runtime_version != BIOFORMATS_RUNTIME_VERSION:
        raise DependencyVersionError(
            f"runtime_version_mismatch: Loaded Bio-Formats={runtime_version!r}"
        )

    admitted = {path.resolve(): path for path in paths}
    common_root = Path(os.path.commonpath([str(path.parent) for path in paths]))
    loaded = []
    for name in _REQUIRED_CLASSES:
        try:
            origin = Path((class_origin or _loaded_origin)(name)).resolve()
        except (KeyError, LookupError) as exc:
            raise DependencyVersionError(f"required_class_missing: Missing {name}: {exc}") from exc
        if origin not in admitted:
            raise DependencyVersionError(
                f"class_origin_outside_inventory: {name} loaded from {origin}"
            )
        if name == "loci.formats.in.VectraReader" and origin.name != "formats-gpl-8.5.0.jar":
            raise DependencyVersionError(
                f"vectra_reader_unavailable: VectraReader loaded from {origin}"
            )
        path = admitted[origin]
        loaded.append(
            LoadedJavaClass(
                name,
                PurePosixPath(path.relative_to(common_root).as_posix()),
                _hash_loaded_jar(path),
            )
        )
    return BioFormatsRuntimeEvidence(
        digest,
        java_version,
        feature,
        policy,
        java_home,
        str(module.__version__),
        coordinate,
        runtime_version,
        _TOOLS_COORDINATE,
        tuple(loaded),
    )


def ensure_offline_bioformats_runtime(
    inventory: BioFormatsRuntimeInventory,
) -> BioFormatsRuntimeEvidence:
    """Validate the approved closure once and reuse only this owner's JVM."""
    global _active_runtime, _active_payload_root, _selected_inventory
    with _runtime_lock:
        try:
            require_approved_bioformats_inventory(inventory)
        except (InventoryMissing, InventoryIncompatible) as exc:
            raise DependencyVersionError(str(exc)) from exc
        if _selected_inventory is not None and _selected_inventory != inventory:
            raise DependencyVersionError("Bio-Formats inventory changed after selection")
        if (
            _configured_bioformats_root is not None
            and inventory.payload_root != _configured_bioformats_root
        ):
            raise DependencyVersionError("Bio-Formats inventory differs from selected root")
        _selected_inventory = inventory
        if _active_runtime is not None:
            if _active_payload_root != inventory.payload_root:
                raise DependencyVersionError("Bio-Formats inventory changed after JVM startup")
            try:
                import jpype
            except ModuleNotFoundError as exc:
                raise DependencyVersionError(f"jpype_unavailable: {exc}") from exc
            if not jpype.isJVMStarted():
                raise DependencyVersionError("Attested Bio-Formats JVM is no longer running")
            return _active_runtime
        try:
            paths, digest = _validate_inventory(inventory)
        except (InventoryMissing, InventoryIncompatible) as exc:
            raise DependencyVersionError(str(exc)) from exc
        try:
            import jpype
        except ModuleNotFoundError as exc:
            raise DependencyVersionError(f"jpype_unavailable: {exc}") from exc
        if jpype.isJVMStarted():
            raise DependencyVersionError("unverified JVM was already started")
        evidence = _attest_validated_classpath(
            paths,
            digest,
            inventory.supported_java_features,
            expected_java_home=_configured_java_home,
        )
        expected_hashes = {jar.relative_path: jar.sha256 for jar in inventory.jars}
        for loaded in evidence.loaded_classes:
            if expected_hashes.get(loaded.jar_relative_path) != loaded.jar_sha256:
                raise DependencyVersionError(
                    f"jar_changed: Loaded {loaded.name} differs from approved inventory"
                )
        _active_runtime = evidence
        _active_payload_root = inventory.payload_root
        return evidence
