"""Build and publish the metadata and licence inventory of a portable application.

The builder checks its selected platform, dependency lock, staged resources and required
notices before recording the release. Archive checksums describe the actual distributed
files. Original upstream licence texts remain unchanged when copied into the portable
bundle."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import tarfile

_HASH_BLOCK_SIZE = 1024 * 1024
_PLATFORM_CLAIMS = {
    "windows-x86_64": ("Windows 11 x86-64, ordinary user, local NTFS project/output"),
    "ubuntu24.04-x86_64": ("Ubuntu 24.04 x86-64, ordinary user, local filesystem, including WSL2"),
}
_NOTICE_NAMES = ("LICENSE", "THIRD_PARTY_NOTICES.txt")
_BIOFORMATS_NOTICES_ROOT = Path(__file__).with_name("bioformats") / "required-notices"
_QT_NOTICES_ROOT = Path(__file__).with_name("qt") / "upstream"
PROTOTYPE_LIMITATIONS = (
    "intended for one trusted local operator on one workstation",
    "exactly one application instance may mutate a workspace or QuPath project",
    "continuation and retry depend on the saved run state and the affected stage",
    "active or ambiguous native work requires its process outcome to be resolved before retry",
    "operating-system, power-loss, or application-crash recovery is not guaranteed",
    "not hardened against a malicious local user or tampered workspace",
    "not a clinical device or a production-certified release",
    "source images are never overwritten",
)


# ==============================================================================
#                                Release metadata
# ==============================================================================


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_relative_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("release metadata path is not canonical POSIX-relative")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("release metadata path is not canonical POSIX-relative")
    return value


def _hash_regular_file(path: Path) -> str:
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ValueError(f"release input is missing: {path}") from error
    if not stat.S_ISREG(mode):
        raise ValueError(f"release input is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_HASH_BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()



def snapshot_source_tree(source_root: Path, archive_path: Path) -> str:
    """Capture ordinary distribution source and return its completed archive SHA.

    We select the same source roots as the release build, without Git metadata,
    development caches or external build/model directories. Required package data
    stays with its source. Stable member order and metadata make the snapshot
    reproducible; changed source entries reject it before any build can use it.
    The caller supplies a new archive path outside the source directory.
    """
    source_root = Path(source_root).resolve(strict=True)
    archive_path = Path(archive_path)
    archive_path = archive_path.parent.resolve(strict=True) / archive_path.name
    if not source_root.is_dir() or archive_path.is_relative_to(source_root):
        raise ValueError("source snapshot requires a directory and an external archive path")
    roots = (
        "LICENSE", "README.md", "MANIFEST.in", "pyproject.toml", "setup.py",
        "docs", "src", "qupath", "scripts", "packaging",
    )
    # External environments, model downloads and build outputs live outside
    # these source roots. Only generated caches and maintenance/test files are
    # pruned within them; authored folders retain their ordinary names.
    ignored = {"_agent", "tests", "__pycache__"}

    def inventory():
        entries = {}

        def visit(path):
            name = path.name
            if (name.startswith(".") or name in ignored
                    or name.endswith((".egg-info", ".dist-info", ".pyc", ".pyo", ".log"))):
                return
            value = path.lstat()
            if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
                raise ValueError(f"source snapshot entry is not a regular file or directory: {path}")
            relative = _canonical_relative_path(path.relative_to(source_root).as_posix())
            if relative in {
                "packaging/bioformats/artifact-review.json",
                "packaging/bioformats/acquire_payload.py",
                "packaging/bioformats/stage_payload.py",
            }:
                return
            if relative == "docs/superpowers":
                return
            if relative == "docs/development" or relative.startswith("docs/development/"):
                return
            if relative.startswith("docs/assets/screenshots/") and path.suffix == ".json":
                return
            if relative in {"docs/data/captures", "docs/assets/data",
                            "docs/assets/figures/pixel-feature-data"}:
                return
            if relative.startswith("docs/data/") and path.suffix in {".json", ".csv"}:
                return
            if relative.startswith("docs/assets/results/") and path.suffix in {".json", ".csv", ".npz"}:
                return
            if relative.startswith("docs/assets/figures/") and (
                    path.suffix == ".json" or (name.startswith("render_") and path.suffix == ".py")):
                return
            entries[relative] = value
            if stat.S_ISDIR(value.st_mode):
                for child in sorted(path.iterdir()):
                    visit(child)

        for name in roots:
            visit(source_root / name)
        return entries

    def identity(value):
        if stat.S_ISDIR(value.st_mode):
            return (value.st_dev, value.st_ino, value.st_mode)
        return (
            value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )

    before = inventory()
    # CREATE_NEW leaves an existing archive untouched. Only this unpublished
    # output is removed if source capture fails.
    with archive_path.open("xb") as output:
        try:
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, value in sorted(before.items()):
                    member = tarfile.TarInfo(relative)
                    member.mtime = 0
                    member.mode = 0o755 if value.st_mode & 0o111 else 0o644
                    if stat.S_ISDIR(value.st_mode):
                        member.type = tarfile.DIRTYPE
                        archive.addfile(member)
                        continue
                    path = source_root / relative
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as source:
                        if identity(os.fstat(source.fileno())) != identity(value):
                            raise ValueError(f"source changed before snapshot read: {relative}")
                        member.size = value.st_size
                        archive.addfile(member, source)
                        if identity(os.fstat(source.fileno())) != identity(value):
                            raise ValueError(f"source changed during snapshot read: {relative}")
            after = inventory()
            if ({key: identity(value) for key, value in before.items()}
                    != {key: identity(value) for key, value in after.items()}):
                raise ValueError("source changed during snapshot capture")
        except BaseException:
            output.close()
            archive_path.unlink()
            raise
    return _hash_regular_file(archive_path)

def _validate_symlink_target(relative_name: str, target: str) -> None:
    if "\\" in target:
        raise ValueError(f"bundle symlink target is not POSIX-relative: {relative_name}")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise ValueError(f"bundle symlink target escapes the bundle: {relative_name}")
    resolved_parts: list[str] = []
    for part in (PurePosixPath(relative_name).parent / target_path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved_parts:
                raise ValueError(f"bundle symlink target escapes the bundle: {relative_name}")
            resolved_parts.pop()
        else:
            resolved_parts.append(part)


def _walk_bundle(bundle_root: Path) -> tuple[tuple[str, Path], tuple[dict[str, str], ...]]:
    bundle_root = Path(bundle_root)
    try:
        root_mode = bundle_root.lstat().st_mode
    except OSError as error:
        raise ValueError(f"bundle root is missing: {bundle_root}") from error
    if not stat.S_ISDIR(root_mode):
        raise ValueError("bundle root must be a real directory")

    regular_files: list[tuple[str, Path]] = []
    symlinks: list[dict[str, str]] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise ValueError(f"cannot inspect bundle directory: {directory}") from error
        for entry in entries:
            entry_relative = relative / entry.name
            relative_name = _canonical_relative_path(entry_relative.as_posix())
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as error:
                raise ValueError(f"cannot inspect bundle entry: {relative_name}") from error
            entry_path = Path(entry.path)
            if stat.S_ISLNK(mode):
                target = os.readlink(entry.path)
                if not isinstance(target, str) or not target or "\x00" in target:
                    raise ValueError(f"invalid bundle symlink target: {relative_name}")
                _validate_symlink_target(relative_name, target)
                symlinks.append({"path": relative_name, "target": target})
            elif stat.S_ISDIR(mode):
                visit(entry_path, entry_relative)
            elif stat.S_ISREG(mode):
                regular_files.append((relative_name, entry_path))
            else:
                raise ValueError(f"unexpected non-regular bundle entry: {relative_name}")

    visit(bundle_root, PurePosixPath("."))
    return (
        tuple(sorted(regular_files, key=lambda item: item[0])),
        tuple(sorted(symlinks, key=lambda item: item["path"])),
    )


def _nonempty_text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be nonempty canonical text")
    return value


def _sorted_text(values: Sequence[str], field: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a sequence of strings")
    result = [_nonempty_text(value, field) for value in values]
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return sorted(result)


def _inventory_hashes(
    resource_inventory: object,
    resource_subdirectory: str,
) -> dict[str, str]:
    """Rebase the resource collector's checked paths to the application folder."""
    resource_prefix = PurePosixPath(_canonical_relative_path(resource_subdirectory))
    resources = getattr(resource_inventory, "resources", None)
    if not isinstance(resources, tuple):
        raise ValueError("resource_inventory must be a verified portable inventory")
    verified: dict[str, str] = {}
    for resource in resources:
        resource_relative = _canonical_relative_path(getattr(resource, "relative_path", None))
        relative = (resource_prefix / resource_relative).as_posix()
        expected = getattr(resource, "sha256", None)
        if not _is_sha256(expected):
            raise ValueError(f"invalid inventory digest: {relative}")
        if relative in verified:
            raise ValueError(f"duplicate inventory path: {relative}")
        # The resource collector has just verified these bytes against the installed wheel.
        verified[relative] = expected
    return dict(sorted(verified.items()))


def build_manifest(
    bundle_root: Path,
    *,
    project_version: str,
    source_archive_sha256: str,
    platform_tag: str,
    python_version: str,
    pyinstaller_version: str,
    pyside_version: str,
    qt_version: str,
    dependency_lock: Path,
    pyinstaller_spec: Path,
    resource_inventory: object,
    external_prerequisites: Sequence[str],
    prototype_limitations: Sequence[str],
    resource_subdirectory: str = ".",
) -> Mapping[str, object]:
    """Record selected build inputs and the collector's verified resource inventory."""
    bundle_root = Path(bundle_root)
    if platform_tag not in _PLATFORM_CLAIMS:
        raise ValueError(f"unsupported portable platform tag: {platform_tag!r}")
    if not _is_sha256(source_archive_sha256):
        raise ValueError("source_archive_sha256 must identify the completed source snapshot")

    limitations = _sorted_text(prototype_limitations, "prototype_limitations")
    if set(limitations) != set(PROTOTYPE_LIMITATIONS):
        raise ValueError("prototype_limitations must match the trusted-local design")
    dependency_lock = Path(dependency_lock).absolute()
    pyinstaller_spec = Path(pyinstaller_spec).absolute()
    asset_hashes = _inventory_hashes(
        resource_inventory,
        resource_subdirectory,
    )
    lock_hash = _hash_regular_file(dependency_lock)
    spec_hash = _hash_regular_file(pyinstaller_spec)
    manifest: dict[str, object] = {
        "schema_name": "mif-portable-build-manifest",
        "schema_version": 1,
        "project_version": _nonempty_text(project_version, "project_version"),
        "source_archive_sha256": source_archive_sha256,
        "platform_tag": platform_tag,
        "supported_host_claim": _PLATFORM_CLAIMS[platform_tag],
        "architecture": "x86_64",
        "python_version": _nonempty_text(python_version, "python_version"),
        "pyinstaller_version": _nonempty_text(pyinstaller_version, "pyinstaller_version"),
        "pyside_version": _nonempty_text(pyside_version, "pyside_version"),
        "qt_version": _nonempty_text(qt_version, "qt_version"),
        "dependency_lock_sha256": lock_hash,
        "pyinstaller_spec_sha256": spec_hash,
        "packaged_asset_sha256s": asset_hashes,
        "symlinks": [],  # Filled from the final payload walk when writing metadata.
        "external_prerequisites": _sorted_text(external_prerequisites, "external_prerequisites"),
        "prototype_limitations": limitations,
    }
    return manifest


def _require_fresh_output(path: Path) -> None:
    if path.exists() or path.is_symlink():
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise ValueError(f"cannot inspect release metadata output: {path}") from error
        kind = "regular file" if stat.S_ISREG(mode) else "non-regular entry"
        raise ValueError(f"release metadata output already exists as {kind}: {path}")


def _read_regular_file(path: Path) -> bytes:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"release input is not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read release input: {path}") from error


def _bioformats_notice_bytes() -> dict[str, bytes]:
    source_root = _BIOFORMATS_NOTICES_ROOT
    try:
        mode = source_root.lstat().st_mode
    except OSError as error:
        raise ValueError(f"Bio-Formats notice source is missing: {source_root}") from error
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Bio-Formats notice source is not a real directory: {source_root}")
    try:
        sources = sorted(
            (path for path in source_root.iterdir() if path.suffix == ".txt"),
            key=lambda path: path.name,
        )
    except OSError as error:
        raise ValueError(f"cannot inspect Bio-Formats notice source: {source_root}") from error
    if not sources:
        raise ValueError(f"Bio-Formats notice source has no .txt files: {source_root}")
    contents: dict[str, bytes] = {}
    for source in sources:
        name = _canonical_relative_path(source.name)
        content = _read_regular_file(source)
        if not content:
            raise ValueError(f"Bio-Formats notice source is empty: {source}")
        contents[name] = content
    return contents


def _require_real_or_absent_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"cannot inspect release metadata directory: {path}") from error
    if not stat.S_ISDIR(mode):
        raise ValueError(f"release metadata directory is not a real directory: {path}")


def _qt_notice_bytes() -> dict[str, bytes]:
    sources, symlinks = _walk_bundle(_QT_NOTICES_ROOT)
    if symlinks:
        raise ValueError("Qt notice source must not contain symlinks")
    if not sources:
        raise ValueError("Qt notice source has no files")
    contents: dict[str, bytes] = {}
    for relative, source in sources:
        content = _read_regular_file(source)
        if not content:
            raise ValueError(f"Qt notice source is empty: {source}")
        contents[relative] = content
    relocated = {
        "qtbase-everywhere-src-6.11.2/src/3rdparty/md4c/LICENSE.md": "md4c-license.md",
        "qtbase-everywhere-src-6.11.2/src/3rdparty/pcre2/LICENCE.md": "pcre2-license.md",
    }
    documentation = Path(__file__).resolve().parents[1] / "docs" / "third-party"
    for original_name, filename in relocated.items():
        contents[original_name] = _read_regular_file(documentation / filename)
    return contents


def _atomic_publish(outputs: Sequence[tuple[Path, bytes]]) -> None:
    staged: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for destination, content in outputs:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=".mif-release-",
                delete=False,
            ) as stream:
                staged.append((Path(stream.name), destination))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, destination in staged:
            _require_fresh_output(destination)
            os.replace(temporary, destination)
            published.append(destination)
    except BaseException:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        raise
    finally:
        for temporary, _destination in staged:
            temporary.unlink(missing_ok=True)


def write_release_metadata(bundle_root: Path, manifest: Mapping[str, object]) -> None:
    """Atomically publish notices, manifest, then the final checksum marker."""
    bundle_root = Path(bundle_root)
    output_paths = {
        name: bundle_root / name for name in (*_NOTICE_NAMES, "BUILD-MANIFEST.json", "SHA256SUMS")
    }
    for path in output_paths.values():
        _require_fresh_output(path)

    # This is an internal handoff from build_manifest, not an imported manifest.
    snapshot = dict(manifest)
    regular_files, symlinks = _walk_bundle(bundle_root)
    snapshot["symlinks"] = list(symlinks)

    notice_root = Path(__file__).with_name("notices")
    notice_bytes = {
        name: _read_regular_file(
            Path(__file__).resolve().parents[1] / name if name == "LICENSE" else notice_root / name
        )
        for name in _NOTICE_NAMES
    }
    bioformats_notice_bytes = _bioformats_notice_bytes()
    qt_notice_bytes = _qt_notice_bytes()
    license_root = bundle_root / "licenses"
    bioformats_license_root = license_root / "bioformats"
    qt_license_root = license_root / "qt"
    bioformats_output_paths = {
        name: bioformats_license_root / name for name in bioformats_notice_bytes
    }
    qt_output_paths = {
        name: qt_license_root.joinpath(*PurePosixPath(name).parts) for name in qt_notice_bytes
    }
    license_directories = {license_root, bioformats_license_root, qt_license_root}
    for path in qt_output_paths.values():
        directory = path.parent
        while directory != qt_license_root:
            license_directories.add(directory)
            directory = directory.parent
    ordered_directories = sorted(
        license_directories, key=lambda path: (len(path.parts), path.as_posix())
    )
    for directory in ordered_directories:
        _require_real_or_absent_directory(directory)
    for path in (*bioformats_output_paths.values(), *qt_output_paths.values()):
        _require_fresh_output(path)
    manifest_bytes = (
        json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    checksums: dict[str, str] = {}
    for relative, path in regular_files:
        checksums[relative] = _hash_regular_file(path)
    for name, content in notice_bytes.items():
        checksums[name] = hashlib.sha256(content).hexdigest()
    for name, content in bioformats_notice_bytes.items():
        checksums[f"licenses/bioformats/{name}"] = hashlib.sha256(content).hexdigest()
    for name, content in qt_notice_bytes.items():
        checksums[f"licenses/qt/{name}"] = hashlib.sha256(content).hexdigest()
    checksums["BUILD-MANIFEST.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_bytes = "".join(
        f"{checksums[relative]}  {relative}\n" for relative in sorted(checksums)
    ).encode("utf-8")

    created_directories: list[Path] = []
    try:
        for directory in ordered_directories:
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)
        _atomic_publish(
            (
                *((output_paths[name], notice_bytes[name]) for name in _NOTICE_NAMES),
                *(
                    (bioformats_output_paths[name], bioformats_notice_bytes[name])
                    for name in bioformats_notice_bytes
                ),
                *((qt_output_paths[name], qt_notice_bytes[name]) for name in qt_notice_bytes),
                (output_paths["BUILD-MANIFEST.json"], manifest_bytes),
                (output_paths["SHA256SUMS"], checksum_bytes),
            )
        )
    except BaseException:
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def write_archive_digest(archive: Path) -> Path:
    """Write a checksum sidecar beside a completed archive."""
    archive = Path(archive)
    _canonical_relative_path(archive.name)
    digest = _hash_regular_file(archive)
    digest_path = archive.with_name(f"{archive.name}.sha256")
    _require_fresh_output(digest_path)
    _atomic_publish(((digest_path, f"{digest}  {archive.name}\n".encode("utf-8")),))
    return digest_path


__all__ = [
    "PROTOTYPE_LIMITATIONS",
    "build_manifest",
    "write_archive_digest",
    "write_release_metadata",
]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Capture current source for a portable build")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("archive_path", type=Path)
    arguments = parser.parse_args()
    print(snapshot_source_tree(arguments.source_root, arguments.archive_path))
