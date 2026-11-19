"""

Selects the installed application files copied into a portable build and checks their completed locations.

We collect module configuration JSON, saved-format definitions, the supplied
spectral library and registered QuPath scripts from the installed Python
package. The four internal software-check scripts are included alongside
the workflow scripts. Their relative paths are preserved so the frozen
application resolves them through the same package interfaces.

An explicitly supplied InstanSeg model can also be included after its complete
file set is verified. Java, Bio-Formats and QuPath installations are selected
externally through Software setup. Before release, the collected directories
are compared with the installed package's paths and complete file checksums.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
from pathlib import Path, PurePosixPath

from mif_pipeline.pipeline_core import _EXPORTS as _CORE_EXPORTS
from mif_pipeline.qupath_runtime import QuPathScriptAsset, payload_lock
from mif_pipeline.qupath_runtime._script_asset_registry import INTERNAL_PROBE_PATHS
from mif_pipeline.qupath_runtime.payload_lock import MODEL_NAME, verify_bundled_instanseg_model

REQUIRED_HIDDEN_IMPORTS = (
    "mif_pipeline.datasets.native_cells",
    "mif_pipeline.compartment_analysis.kronos_worker",
    "mif_pipeline.orchestration.batch_preprocessing_execution",
    "mif_pipeline.pipeline_core.posix_authority_provisioning",
    "mif_pipeline.pipeline_core.win32_authority_provisioning",
    *sorted({f"mif_pipeline.pipeline_core.{module}" for module, _ in _CORE_EXPORTS.values()}),
)

_QUPATH_ASSETS_DESTINATION = PurePosixPath("qupath-assets")

_RESOURCE_CLASSES = (
    ("mif_pipeline", PurePosixPath("config"), frozenset({".json"}), PurePosixPath("mif_pipeline/config")),
    ("mif_pipeline.cell_analysis", PurePosixPath("data/fonts"), frozenset({".ttf", ""}), PurePosixPath("mif_pipeline/cell_analysis/data/fonts")),
    ("mif_pipeline.pipeline_core", PurePosixPath("data"), frozenset({".json"}), PurePosixPath("mif_pipeline/pipeline_core/data")),
    ("mif_pipeline.preprocessing.unmixing", PurePosixPath("data"), frozenset({".umxlib"}), PurePosixPath("mif_pipeline/preprocessing/unmixing/data")),
    ("mif_pipeline.qupath_runtime.assets", PurePosixPath("."), frozenset({".groovy", ".jar", ".json"}), PurePosixPath("mif_pipeline/qupath_runtime/assets")),
)

_REQUIRED_NONEMPTY_CLASSES = frozenset(row[0] for row in _RESOURCE_CLASSES)


_FORBIDDEN_TOP_LEVEL_NAMES = frozenset(
    {".git", "images", "java", "jgo", "models", "qupath", "tests"}
)


# ==============================================================================
#                               Resource inventory
# ==============================================================================


class PortableResourceError(ValueError):
    """

    Reports missing, changed or unexpected files while preparing or checking a portable application.
    """

@dataclass(frozen=True, slots=True)
class PortableResource:
    """

    Identifies one included application file by its relative destination and content checksum.

    Attributes:
        relative_path: Forward-slash relative filename under the completed build directory.
        sha256: SHA-256 of the selected file's complete contents.
    """

    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        """

        Checks the included file's relative path and SHA-256 text.

        The destination must stay within the build directory and use the same
        normalized forward-slash representation when read as a PurePosixPath.

        Raises:
            PortableResourceError: The path is absolute, leaves its directory or
                has another representation, or the checksum is invalid.
        """

        path = PurePosixPath(self.relative_path)
        if (
            type(self.relative_path) is not str
            or not self.relative_path
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != self.relative_path
        ):
            raise PortableResourceError("portable resource path is not canonical")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise PortableResourceError("portable resource digest is invalid")


@dataclass(frozen=True, slots=True)
class PortableResourceInventory:
    """

    Retains the complete checked list of included application files.

    Attributes:
        resources: PortableResource records sorted by relative destination path.
    """

    resources: tuple[PortableResource, ...]

    def __post_init__(self) -> None:
        """

        Requires distinct sorted file destinations in the recorded portable application.

        We compare names without regard to case when detecting duplicates, preserving
        a file layout that can also be copied onto a case-insensitive Windows filesystem.

        Raises:
            TypeError: The records are not a tuple of PortableResource values.
            PortableResourceError: Relative destinations repeat or are not sorted.
        """

        if type(self.resources) is not tuple or any(
            not isinstance(resource, PortableResource) for resource in self.resources
        ):
            raise TypeError("portable resources must be a tuple")
        paths = tuple(resource.relative_path for resource in self.resources)
        if paths != tuple(sorted(paths)) or len({path.casefold() for path in paths}) != len(paths):
            raise PortableResourceError("portable resource paths are not unique and sorted")


@dataclass(frozen=True, slots=True)
class _InstalledResource:
    """

    Associates a selected installed file with its intended portable-build destination.

    Attributes:
        source: Actual installed file to pass to the packager.
        resource: Relative output path and expected checksum for that file.
    """

    source: Path
    resource: PortableResource


def _file_sha256(path: Path) -> str:
    """Hash a resource without retaining its complete archive or model in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _children(root, relative: PurePosixPath = PurePosixPath(".")):
    """

    Traverses the selected installed directory in name order and yields its regular file paths.

    We visit subdirectories recursively while retaining each file's path relative
    to the starting directory. A missing resource directory yields no files;
    the calling resource-class check decides whether that absence is permitted.
    Linked directories are rejected before descending.

    Args:
        root: Package directory or traversable directory currently being visited.
        relative: Accumulated relative path from the original collection root.

    Yields:
        File object and relative path for each descendant regular file.

    Raises:
        PortableResourceError: A traversed directory is a symbolic link.
    """

    is_symlink = getattr(root, "is_symlink", None)
    if is_symlink is not None and is_symlink():
        raise PortableResourceError(f"portable resource directory is a symlink: {root}")
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name)
    except FileNotFoundError:
        return
    for child in children:
        child_relative = relative / child.name
        if child.is_dir():
            yield from _children(child, child_relative)
        elif child.is_file():
            yield child, child_relative


def _resource_class_entries(
    package: str,
    source_subdirectory: PurePosixPath,
    suffixes: frozenset[str],
    destination_root: PurePosixPath,
) -> tuple[_InstalledResource, ...]:
    """

    Collects one kind of installed package data using its permitted filename suffixes.

    We select matching Python source, JSON, Groovy or spectral-library files and require a
    regular installed filesystem file for each. Its contents are hashed and
    associated with the requested relative destination. Required data groups
    must contain at least one matching file.

    Args:
        package: Installed Python package from which files are read.
        source_subdirectory: Relative directory within that package.
        suffixes: File extensions admitted for this data group.
        destination_root: Relative portable-build directory receiving the group.

    Returns:
        Tuple of installed source paths with their destination/checksum records.

    Raises:
        PortableResourceError: A selected file is linked or not available as
            an installed regular file, or a required group is empty.
    """

    package_root = resources.files(package)
    source_root = package_root.joinpath(*source_subdirectory.parts)
    entries: list[_InstalledResource] = []
    for traversable, relative in _children(source_root):
        if PurePosixPath(traversable.name).suffix.casefold() not in suffixes:
            continue
        try:
            source = Path(traversable)
        except TypeError as error:
            raise PortableResourceError(
                f"portable resource is not an installed filesystem member: {package}"
            ) from error
        if source.is_symlink() or not source.is_file():
            raise PortableResourceError(
                f"portable resource is not a regular installed file: {source}"
            )
        relative_path = (destination_root / relative).as_posix()
        entries.append(
            _InstalledResource(
                source=source,
                resource=PortableResource(
                    relative_path=relative_path,
                    sha256=_file_sha256(source),
                ),
            )
        )
    if package in _REQUIRED_NONEMPTY_CLASSES and not entries:
        raise PortableResourceError(f"required installed resource class is empty: {package}")
    return tuple(entries)


def _installed_resources() -> tuple[_InstalledResource, ...]:
    """

    Collects all current application data and requires complete agreement with the native script list.

    We combine the selected resource groups, reject duplicate destination paths
    and compare the Groovy filenames with both the workflow registry and the
    internal software probes. The comparison detects missing as well as extra
    scripts before the portable application is built.

    Returns:
        Installed-file records sorted by relative destination path.

    Raises:
        PortableResourceError: Required data is absent, destinations repeat,
            or the installed Groovy list differs from the current registered scripts.
    """

    entries = tuple(
        entry
        for resource_class in _RESOURCE_CLASSES
        for entry in _resource_class_entries(*resource_class)
    )
    paths = tuple(entry.resource.relative_path for entry in entries)
    if len({path.casefold() for path in paths}) != len(paths):
        raise PortableResourceError("installed portable resources contain duplicates")

    groovy_root = PurePosixPath("mif_pipeline/qupath_runtime/assets")
    registered_paths = {
        (
            groovy_root
            / PurePosixPath(asset.value).relative_to(PurePosixPath("qupath") / "scripts")
        ).as_posix()
        for asset in QuPathScriptAsset
    }
    registered_paths.update((groovy_root / relative).as_posix() for relative in INTERNAL_PROBE_PATHS)
    installed_groovy_paths = {
        entry.resource.relative_path
        for entry in entries
        if PurePosixPath(entry.resource.relative_path).suffix == ".groovy"
    }
    if installed_groovy_paths != registered_paths:
        missing = sorted(registered_paths - installed_groovy_paths)
        unexpected = sorted(installed_groovy_paths - registered_paths)
        raise PortableResourceError(
            "installed Groovy resources differ from the typed registry; "
            f"missing={missing!r}; unexpected={unexpected!r}"
        )
    return tuple(sorted(entries, key=lambda entry: entry.resource.relative_path))


def collect_portable_datas() -> tuple[tuple[str, str], ...]:
    """

    Creates the installed-file and destination-directory pairs consumed by PyInstaller.

    Returns:
        Tuple of source filenames and relative output directories for the
        complete checked application-data list.
    """

    return tuple(
        (
            str(entry.source),
            PurePosixPath(entry.resource.relative_path).parent.as_posix(),
        )
        for entry in _installed_resources()
    )


def _verified_instanseg_model_resources(root: Path) -> tuple[_InstalledResource, ...]:
    """

    Collects an explicitly supplied InstanSeg model after checking all of its expected files.

    We require the model root to contain only model/<selected model name>/ and
    the exact required model files beneath it. Their checksums are compared
    again while constructing the output records, so a file changed after the
    initial model check cannot be copied under its earlier expected description.

    Args:
        root: Absolute directory containing the selected model/ subdirectory.

    Returns:
        File records with destinations beneath qupath-assets/model/<model name>/.

    Raises:
        PortableResourceError: Root layout, model content or file readability
            differs from the selected model release.
    """

    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise PortableResourceError("InstanSeg build model requires an absolute clean root")
    if verify_bundled_instanseg_model(root).status != "verified":
        raise PortableResourceError("bundled InstanSeg model differs from the installed lock")
    try:
        for parent, expected in (
            (root, {"model"}),
            (root / "model", {MODEL_NAME}),
        ):
            children = tuple(parent.iterdir())
            if {child.name for child in children} != expected or any(
                child.is_symlink() or not child.is_dir() for child in children
            ):
                raise PortableResourceError("bundled InstanSeg model contains an unexpected member")
        entries = tuple(_children(root))
    except OSError as exc:
        raise PortableResourceError("bundled InstanSeg model cannot be listed") from exc
    pinned = {
        PurePosixPath("model") / MODEL_NAME / name: digest
        for name, digest in payload_lock.INSTANSEG_MODEL_FILE_LOCK
    }
    if {relative for _path, relative in entries} != set(pinned):
        raise PortableResourceError(
            "bundled InstanSeg model member set differs from installed lock"
        )
    installed: list[_InstalledResource] = []
    for path, relative in entries:
        try:
            observed = _file_sha256(path)
        except OSError as exc:
            raise PortableResourceError("bundled InstanSeg model member cannot be read") from exc
        if observed != pinned[relative]:
            raise PortableResourceError("bundled InstanSeg model changed after lock verification")
        installed.append(
            _InstalledResource(
                source=path,
                resource=PortableResource(
                    relative_path=(_QUPATH_ASSETS_DESTINATION / relative).as_posix(),
                    sha256=pinned[relative],
                ),
            )
        )
    return tuple(installed)


def collect_frozen_datas(
    instanseg_model_root: Path,
) -> tuple[tuple[str, str], ...]:
    """

    Combines the current application-data files with the verified model selected for this frozen build.

    Args:
        instanseg_model_root: Directory containing the complete selected model.

    Returns:
        PyInstaller source/destination pairs for application data and the model files.
    """

    model = tuple(
        (str(entry.source), PurePosixPath(entry.resource.relative_path).parent.as_posix())
        for entry in _verified_instanseg_model_resources(instanseg_model_root)
    )
    return (*collect_portable_datas(), *model)


def _actual_resources(
    root: Path, expected: tuple[PortableResource, ...]
) -> tuple[PortableResource, ...]:
    """

    Reads the data files present in a completed portable-build directory.

    We first reject packaged Bio-Formats directories, unexpected Java archives
    and unsupported top-level image/model/development folders. JPype's bridge
    and JAR paths in the installed-resource inventory are permitted. The caller
    compares the latter with their installed checksums, so a packaged native
    extension remains subject to the same identity check as other resources.
    Within each application-data destination, we stream hashes of all present
    files and require distinct relative paths.

    Args:
        root: Completed portable application directory to inspect.
        expected: Current installed resources supplying the allowed application
            JAR paths and the caller's subsequent content comparison.

    Returns:
        PortableResource records sorted by relative filename.

    Raises:
        PortableResourceError: The root or an included file is linked, forbidden
            content is present, or relative filenames repeat.
    """

    if root.is_symlink() or not root.is_dir():
        raise PortableResourceError("portable resource root must be a directory")
    permitted_jars = {"org.jpype.jar"} | {
        resource.relative_path
        for resource in expected
        if PurePosixPath(resource.relative_path).suffix.casefold() == ".jar"
    }
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if "bioformats-runtime" in {part.casefold() for part in relative.parts}:
            raise PortableResourceError("Bio-Formats runtime must not be bundled")
        if path.suffix.casefold() == ".jar" and relative.as_posix() not in permitted_jars:
            raise PortableResourceError(
                "unexpected bundled JAR; only installed application resources and the JPype bridge are permitted"
            )
    forbidden = sorted(
        child.name
        for child in root.iterdir()
        if child.name.casefold() in _FORBIDDEN_TOP_LEVEL_NAMES
    )
    if forbidden:
        raise PortableResourceError(
            f"portable resource root contains forbidden data: {forbidden!r}"
        )

    actual: list[PortableResource] = []
    # Source resources share the package root with JSON and Groovy groups.
    # Visit each physical subtree once, retaining detection of every extra file.
    destinations = {definition[3] for definition in _RESOURCE_CLASSES}
    roots = sorted(
        destination for destination in destinations
        if not any(parent != destination and destination.is_relative_to(parent)
                   for parent in destinations)
    )
    for destination_root in roots:
        category_root = root.joinpath(*destination_root.parts)
        for path, relative in _children(category_root):
            if path.is_symlink():
                raise PortableResourceError(f"collected portable resource is a symlink: {path}")
            actual.append(
                PortableResource(
                    relative_path=(destination_root / relative).as_posix(),
                    sha256=_file_sha256(path),
                )
            )
    paths = tuple(resource.relative_path for resource in actual)
    if len({path.casefold() for path in paths}) != len(paths):
        raise PortableResourceError("collected portable resources contain duplicates")
    return tuple(sorted(actual, key=lambda resource: resource.relative_path))


def verify_portable_resources(
    root: Path,
    *,
    require_instanseg_model: bool = True,
) -> PortableResourceInventory:
    """

    Compares the completed portable application with the selected installed data and optional model.

    We collect the expected installed-file records and inspect the actual build
    directories. When a model is required, its complete file list and checksums
    are verified and included on both sides. Matching relative paths must have
    matching checksums, and neither side may contain additional or missing files.

    Args:
        root: Completed portable application directory.
        require_instanseg_model: Whether this build is expected to include the
            verified model under qupath-assets/.

    Returns:
        PortableResourceInventory containing all checked output files.

    Raises:
        TypeError: The model-inclusion choice is not boolean.
        PortableResourceError: Required files or model content are missing,
            changed or unexpected.
    """

    if type(require_instanseg_model) is not bool:
        raise TypeError("require_instanseg_model must be bool")
    expected = tuple(entry.resource for entry in _installed_resources())
    actual = _actual_resources(Path(root), expected)
    if require_instanseg_model:
        model_entries = _verified_instanseg_model_resources(Path(root) / _QUPATH_ASSETS_DESTINATION)
        expected += tuple(entry.resource for entry in model_entries)
        actual += tuple(
            PortableResource(
                relative_path=entry.resource.relative_path,
                sha256=_file_sha256(entry.source),
            )
            for entry in model_entries
        )
    expected_by_path = {resource.relative_path: resource for resource in expected}
    actual_by_path = {resource.relative_path: resource for resource in actual}

    missing = sorted(set(expected_by_path) - set(actual_by_path))
    unexpected = sorted(set(actual_by_path) - set(expected_by_path))
    changed = sorted(
        path
        for path in set(expected_by_path) & set(actual_by_path)
        if expected_by_path[path].sha256 != actual_by_path[path].sha256
    )
    if missing or unexpected or changed:
        raise PortableResourceError(
            "collected portable resources differ from the installed inventory; "
            f"missing={missing!r}; changed={changed!r}; unexpected={unexpected!r}"
        )
    return PortableResourceInventory(
        resources=tuple(sorted(actual, key=lambda item: item.relative_path))
    )


__all__ = [
    "PortableResource",
    "PortableResourceError",
    "PortableResourceInventory",
    "REQUIRED_HIDDEN_IMPORTS",
    "collect_portable_datas",
    "collect_frozen_datas",
    "verify_portable_resources",
]
