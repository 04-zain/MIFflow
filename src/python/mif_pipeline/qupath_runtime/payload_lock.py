"""

Supplies the selected QuPath, InstanSeg, Warpy and tensor-library file checks.

We read their release descriptions from config/software. InstanSeg's extension
and pretrained model have separate files and downloads. The native PyTorch
libraries and Java bridge are selected for the operating system, while the
DJL and PyTorch engine JARs are supplied by the selected QuPath installation.

File checks compare the expected names and complete-file SHA-256 values with
the chosen installation. Model and native-library folders must contain exactly
the required regular files. A QuPath application or extensions folder can
contain other components, so we inspect the required JARs within that folder.

These descriptions also construct QuPath's prerequisite request. That native
operation checks the running program, loaded extensions and tensor libraries,
and the model when selected. We keep these checks connected to the same
release definitions rather than maintaining another file list in Python.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping

from .launch_binding import QuPathProfileLaunchBinding
from .prerequisite_probe import (
    DEFAULT_PREREQUISITE_TIMEOUT_SECONDS,
    LocalInventoryCheck,
    ProbeFileExpectation,
    QuPathPrerequisiteProbeRequest,
    _component_check,
    _file_check,
    _model_members,
    _path_snapshot,
)

from mif_pipeline._software import read_software_description

_QUPATH = read_software_description("qupath")
_INSTANSEG_RELEASE = read_software_description("instanseg")
_WARPY_RELEASE = read_software_description("warpy")
_PYTORCH_RELEASE = read_software_description("pytorch")


def _archive_values(value):
    """

    Reads a software download's URL, exact byte count and expected SHA-256.

    Args:
        value: Archive description from its installed program JSON.

    Returns:
        URL, size and checksum in the order used by setup operations.

    Raises:
        ValueError: Download fields, HTTPS location, byte count or checksum are invalid.
    """

    from urllib.parse import urlsplit
    import re

    if type(value) is not dict or set(value) != {"url", "size_bytes", "sha256"}:
        raise ValueError("The software download must specify its URL, byte count and checksum.")
    url, size, digest = value["url"], value["size_bytes"], value["sha256"]
    if not isinstance(url, str):
        raise ValueError("The software download location must be text.")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("The software download must use its recorded HTTPS location.")
    if type(size) is not int or size <= 0 or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("The software download has an invalid byte count or checksum.")
    return url, size, digest


def _file_values(value):
    """

    Reads the required installed filenames and checksums in their recorded order.

    We require distinct simple filenames because these extension/model/native
    files are written directly inside their selected component folder. The
    program installer performs the corresponding archive extraction and checksum
    comparison before making that folder available.

    Args:
        value: Nonempty list of path and sha256 dictionaries from the program JSON.

    Returns:
        Ordered tuple of relative filename and expected SHA-256 pairs.

    Raises:
        ValueError: A filename leaves its component folder, repeats another
            filename or has an invalid checksum.
    """

    import re

    if not isinstance(value, list) or not value:
        raise ValueError("The software description requires its installed file list.")
    output = []
    names = set()
    for row in value:
        if type(row) is not dict or set(row) != {"path", "sha256"}:
            raise ValueError("An installed software file requires its path and checksum.")
        name, digest = row["path"], row["sha256"]
        if not isinstance(name, str) or name in {"", ".", ".."} or any(c in name for c in "/\\:\x00"):
            raise ValueError("An installed filename must remain inside its component folder.")
        if name.casefold() in names or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("An installed filename repeats or its checksum is invalid.")
        names.add(name.casefold())
        output.append((name, digest))
    return tuple(output)


QUPATH_VERSION = _QUPATH["version"]
INSTANSEG_VERSION = _INSTANSEG_RELEASE["extension"]["version"]
DJL_EXTENSION_VERSION = _QUPATH["libraries"]["djl"]["version"]
PYTORCH_ENGINE_VERSION = _PYTORCH_RELEASE["java_engine_version"]
MODEL_NAME = _INSTANSEG_RELEASE["model"]["name"]
MODEL_VERSION = _INSTANSEG_RELEASE["model"]["version"]
WARPY_VERSION = _WARPY_RELEASE["version"]
WARPY_SOURCE_ARCHIVE = _archive_values(_WARPY_RELEASE["archive"])
BUILTIN_DJL_SHA256 = _QUPATH["libraries"]["djl"]["sha256"]
BUILTIN_PYTORCH_ENGINE_SHA256 = _QUPATH["libraries"]["pytorch_engine"]["sha256"]

SOURCE_ARCHIVE_LOCK: Mapping[str, tuple[str, int, str]] = MappingProxyType({
    "instanseg": _archive_values(_INSTANSEG_RELEASE["extension"]["archive"]),
    "model": _archive_values(_INSTANSEG_RELEASE["model"]["archive"]),
    "pytorch-jni": _archive_values(_PYTORCH_RELEASE["jni_archive"]),
    **{"pytorch-native-cpu-" + platform: _archive_values(record["archive"])
       for platform, record in _PYTORCH_RELEASE["platforms"].items()},
})
INSTANSEG_MODEL_FILE_LOCK = _file_values(_INSTANSEG_RELEASE["model"]["files"])
_NATIVE = MappingProxyType({
    platform: _file_values(record["files"])
    for platform, record in _PYTORCH_RELEASE["platforms"].items()
})


# ==============================================================================
#                   Selected software files and native checks
# ==============================================================================


def cpu_native_file_lock(platform: str) -> tuple[tuple[str, str], ...]:
    """

    Selects the native PyTorch and Java-bridge filenames for the operating system.

    Args:
        platform: linux-x86_64 or win-x86_64, selecting its entry in pytorch.json.

    Returns:
        Ordered filename and expected SHA-256 pairs for that platform.

    Raises:
        ValueError: The platform has no supported native-library definition.
    """

    try:
        return _NATIVE[platform]
    except KeyError as exc:
        raise ValueError("unsupported platform for QuPath CPU payload") from exc


def _verify_files(
    root: Path,
    records: tuple[tuple[str, str], ...],
) -> LocalInventoryCheck:
    """

    Compares the required files with their recorded checksums and folder layout.

    We inspect the folder before reading its files and require exactly the
    expected regular-file names. The common file checker
    then compares complete checksums. After a successful check, we inspect the
    folder again and require the same file information, detecting files added,
    replaced or removed during verification.

    Args:
        root: Directory containing the selected component's files.
        records: Ordered relative filenames and expected SHA-256 values.

    Returns:
        LocalInventoryCheck reporting verified, missing or incompatible files
        with the diagnostic used by Software setup.
    """

    members, listing_error = _model_members(root)
    if listing_error is not None:
        return listing_error
    expected = {root / name for name, _ in records}
    if set(members) - {root} - expected or any(
        not stat.S_ISREG(members[path][2]) for path in expected if path in members
    ):
        return LocalInventoryCheck(
            "incompatible", None, f"unexpected staged member under {root}"
        )
    items = tuple(ProbeFileExpectation(root / name, digest) for name, digest in records)
    check = _component_check(items, root=root)
    if check.status == "verified":
        after, error = _model_members(root)
        if error is not None or after != members:
            return LocalInventoryCheck(
                "incompatible", None, "staged inventory changed during verification"
            )
    return check


def verify_cpu_native_directory(directory: Path, platform: str) -> LocalInventoryCheck:
    """

    Checks the complete native-library folder against the platform's file definition.

    Args:
        directory: Absolute path to the native PyTorch and Java-bridge files.
        platform: linux-x86_64 or win-x86_64.

    Returns:
        File-check result used before the engine is opened by the native
        prerequisite operation.

    Raises:
        ValueError: The path is relative, contains '..', or uses an unsupported platform.
    """

    if not isinstance(directory, Path) or not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("CPU native directory must be an absolute path without traversal")
    return _verify_files(directory, cpu_native_file_lock(platform))


def prototype_probe_request(
    *,
    executable: Path,
    model_root: Path | None = None,
    extension_jar: Path,
    djl_jar: Path,
    pytorch_engine_jar: Path,
    native_root: Path,
    cache_root: Path,
    platform: str,
    timeout_seconds: float = DEFAULT_PREREQUISITE_TIMEOUT_SECONDS,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    require_warpy: bool = False,
) -> QuPathPrerequisiteProbeRequest:
    """

    Builds the native prerequisite request from the programs, libraries and optional model.

    We associate each explicit local path with the checksum from its program
    definition. The request includes InstanSeg, the QuPath-supplied Java libraries,
    the platform's native tensor libraries and the model files when selected.

    When Warpy is required, we reload the selected managed QuPath configuration
    and compare it with the supplied launch configuration. InstanSeg must belong
    to that installation, and Warpy is selected from its extensions folder.
    The subsequent native operation checks that these files can be loaded together.

    Args:
        executable: Selected QuPath program.
        model_root: Optional absolute directory containing the selected model.
        extension_jar: Selected InstanSeg extension JAR.
        djl_jar: DJL extension JAR supplied by this QuPath installation.
        pytorch_engine_jar: PyTorch Java-engine JAR supplied by QuPath.
        native_root: Directory containing the operating system's PyTorch files.
        cache_root: Internal working/cache directory used by the native check.
        platform: Supported operating system and architecture.
        timeout_seconds: Time allowed for the native prerequisite check.
        launch_binding: Selected managed QuPath launch configuration, including
            its extension locations.
        require_warpy: Whether the operation also requires Warpy.

    Returns:
        QuPathPrerequisiteProbeRequest with selected paths and release checksums.

    Raises:
        TypeError: The Warpy choice or launch configuration has the wrong type.
        ValueError: The files or launch configuration differ from the installation
            used to construct this request.
    """

    native = cpu_native_file_lock(platform)
    if type(require_warpy) is not bool:
        raise TypeError("require_warpy must be bool")
    warpy_jar = None
    instanseg_digest = SOURCE_ARCHIVE_LOCK["instanseg"][2]
    if require_warpy:
        if launch_binding is None:
            raise ValueError("required Warpy needs a selected managed profile")
        if type(launch_binding) is not QuPathProfileLaunchBinding:
            raise TypeError("launch_binding must be a QuPathProfileLaunchBinding")
        from .managed_profile import load_managed_qupath_profile, managed_instanseg_source_paths

        selected = load_managed_qupath_profile(
            launch_binding.profile_root / "manifest.json",
            expected_executable=executable,
        )
        if selected != launch_binding:
            raise ValueError("selected managed profile differs from its setup record")
        extensions = launch_binding.profile_root / "extensions"
        if extension_jar not in managed_instanseg_source_paths(launch_binding):
            raise ValueError(
                "InstanSeg path differs from the selected QuPath installation or profile"
            )
        if verify_instanseg_extension_file(extension_jar).status != "verified":
            raise ValueError("InstanSeg bytes differ from the selected setup")
        warpy_jar = ProbeFileExpectation(
            extensions / WARPY_SOURCE_ARCHIVE[0].rsplit("/", 1)[1],
            WARPY_SOURCE_ARCHIVE[2],
        )
    return QuPathPrerequisiteProbeRequest(
        executable=executable,
        model_root=model_root,
        model_version=MODEL_VERSION if model_root is not None else None,
        model_files=(
            tuple(
                ProbeFileExpectation(model_root / name, digest)
                for name, digest in INSTANSEG_MODEL_FILE_LOCK
            )
            if model_root is not None
            else ()
        ),
        instanseg_jar=ProbeFileExpectation(extension_jar, instanseg_digest),
        djl_jar=ProbeFileExpectation(djl_jar, BUILTIN_DJL_SHA256),
        pytorch_engine_jar=ProbeFileExpectation(pytorch_engine_jar, BUILTIN_PYTORCH_ENGINE_SHA256),
        cpu_engine_files=tuple(
            ProbeFileExpectation(native_root / name, digest) for name, digest in native
        ),
        warpy_jar=warpy_jar,
        require_warpy=require_warpy,
        cache_root=cache_root,
        timeout_seconds=timeout_seconds,
        launch_binding=launch_binding,
    )


def bind_prototype_model_request(
    runtime_request: QuPathPrerequisiteProbeRequest,
    *,
    model_root: Path,
    model_version: str,
) -> QuPathPrerequisiteProbeRequest:
    """

    Adds the model to a request that already describes the programs and libraries.

    We retain the existing selections and add the model's version and required
    files. The model can then undergo the same file and native-loading checks
    using the selected QuPath installation.

    Args:
        runtime_request: Prerequisite request without a selected model.
        model_root: Directory containing the model files to inspect.
        model_version: Supported pretrained-model version from instanseg.json.

    Returns:
        New prerequisite request including the complete model-file list.

    Raises:
        TypeError: The value is not a prerequisite request.
        ValueError: A model is already selected or the version is unsupported.
    """

    if not isinstance(runtime_request, QuPathPrerequisiteProbeRequest):
        raise TypeError("runtime_request must be QuPathPrerequisiteProbeRequest")
    if runtime_request.model_root is not None:
        raise ValueError("runtime_request must have no model")
    if model_version != MODEL_VERSION:
        raise ValueError("unsupported model version")
    return replace(
        runtime_request,
        model_root=model_root,
        model_version=model_version,
        model_files=tuple(
            ProbeFileExpectation(model_root / name, digest)
            for name, digest in INSTANSEG_MODEL_FILE_LOCK
        ),
    )


def _verify_archive(path: Path, *, expected_size: int, expected_sha256: str) -> LocalInventoryCheck:
    """

    Checks a completed download's file type, byte count and checksum.

    We reject a missing, nonregular or incorrectly sized file before reading
    its complete contents. A file with the expected size is then passed to the
    shared SHA-256 check, which also records its observed file information.

    Args:
        path: Downloaded archive or extension JAR.
        expected_size: Exact byte count recorded for the selected release.
        expected_sha256: Recorded complete-file SHA-256.

    Returns:
        Verified, missing or incompatible LocalInventoryCheck for the file.
    """

    try:
        _path_snapshot(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return LocalInventoryCheck(
                "incompatible", None, f"archive is not a regular file: {path}"
            )
        if info.st_size != expected_size:
            return LocalInventoryCheck("incompatible", None, f"archive size mismatch: {path}")
    except FileNotFoundError:
        return LocalInventoryCheck("missing", None, f"archive missing: {path}")
    except (OSError, ValueError) as exc:
        return LocalInventoryCheck(
            "incompatible", None, f"archive cannot be verified: {type(exc).__name__}"
        )
    return _file_check(ProbeFileExpectation(path, expected_sha256))


def verify_bundled_instanseg_model(root: Path) -> LocalInventoryCheck:
    """

    Checks the named model within a prepared or packaged model directory.

    Args:
        root: Absolute parent directory containing model/<model name>/.

    Returns:
        Complete model-file verification result for that named subdirectory.

    Raises:
        ValueError: The parent path is relative or contains '..'.
    """

    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise ValueError("bundled model root must be an absolute path without traversal")
    return verify_instanseg_model_directory(root / "model" / MODEL_NAME)


def verify_instanseg_model_directory(directory: Path) -> LocalInventoryCheck:
    """

    Verifies the eight files belonging to the selected InstanSeg model release.

    The record includes the network weights, model description, preprocessing
    and postprocessing scripts, example arrays, readme and cover image. Their
    filenames and complete checksums must agree with instanseg.json before the
    model is supplied to the native loading operation.

    Args:
        directory: Absolute directory containing the expanded model files.

    Returns:
        Verified, missing or incompatible model-file result.

    Raises:
        ValueError: The model path is not absolute or contains '..'.
    """

    if not isinstance(directory, Path) or not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("model directory must be an absolute path without traversal")
    return _verify_files(directory, INSTANSEG_MODEL_FILE_LOCK)


def verify_instanseg_extension_file(path: Path) -> LocalInventoryCheck:
    """

    Compares the installed InstanSeg extension with its recorded release download.

    Args:
        path: Absolute filename of the selected extension JAR.

    Returns:
        File-check result based on its exact byte count and SHA-256.

    Raises:
        ValueError: The path is not absolute or contains '..'.
    """

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("extension path must be an absolute path without traversal")
    _, size, digest = SOURCE_ARCHIVE_LOCK["instanseg"]
    return _verify_archive(path, expected_size=size, expected_sha256=digest)


