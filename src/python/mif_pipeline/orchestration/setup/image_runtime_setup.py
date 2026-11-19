"""

Selects and prepares the Java and Bio-Formats installation used to read source images.

We inspect the chosen Java program, the JVM library that Python will open,
and the complete Bio-Formats JAR set. A separate image-reading check then
confirms that the selected files can be loaded together. This check allows
Software setup to distinguish missing files, incompatible files and a
working image reader.

The supported download descriptions are read from config/software/java.json
and bioformats.json. Downloading requires explicit consent. We write into a
temporary directory, verify the files and required program execution, and
move the completed installation into its versioned application-data folder.
Existing installations are inspected before any download is attempted.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from mif_pipeline._software import read_software_description
import os
from pathlib import Path, PurePosixPath
from mif_pipeline.pipeline_core.atomic_publication import PublicationError, publish_no_replace
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import build_opener
import zipfile

from mif_pipeline.io.bioformats_prerequisite import BioFormatsRuntimeInventory, InventoryIncompatible, InventoryMissing, probe_bioformats_prerequisite
from mif_pipeline.io.bioformats_runtime_lock import load_approved_bioformats_lock, verify_approved_bioformats_payload, require_external_bioformats_root
from .setup_transfer import PinnedRedirects
from .setup_transfer import SetupTransferError
from .setup_transfer import download_pinned_file
from .setup_transfer import validate_https_url

_PLATFORMS = frozenset({"linux-x86_64", "win-x86_64"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK = 1024 * 1024
_JAVA_VERSION = "21.0.12.1+1"
_JAVA_DIRECTORY = "temurin-21.0.12.1+1-jre"
_BF_DIRECTORY = "bioformats-8.5.0"
_ARCHIVE_ROOT = "jdk-21.0.12.1+1-jre"
_JAVA_FAILURE_CODES = frozenset(
    {
        "java_home_invalid",
        "java_platform_unsupported",
        "jvm_library_missing",
        "java_home_mismatch",
        "java_feature_invalid",
        "java_feature_unsupported",
        "java_policy_mismatch",
        "unverified_jvm_options",
    }
)
_JAVA_MISSING_CODES = frozenset({"java_jvm_missing", "jvm_library_missing"})


# ==============================================================================
#                              Image runtime setup
# ==============================================================================


class ImageRuntimeSetupError(RuntimeError):
    """

    Reports the failed Java or Bio-Formats setup operation.

    Attributes:
        code: Failure category used to select the relevant setup message.
    """

    def __init__(self, code: str, detail: str):
        """

        Associates the setup failure category with its explanation.

        Args:
            code: Stable category identifying the failed operation.
            detail: Explanation included in the exception message.
        """

        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ImageRuntimeSetupStatus:
    """

    Records the current result of checking one image-reading component.

    Attributes:
        component: Java or Bio-Formats.
        state: Ready after the required check succeeds, missing when files
            are absent, incompatible when a required file/version check fails,
            or not_checked when the required execution check remains incomplete.
        location: Selected component directory, or None when none was resolved.
        included: Whether the component is supplied inside the application.
            The current setup selects external Java and Bio-Formats directories.
        detail: Explanation shown beside the relevant Software setup controls.
    """

    component: Literal["java", "bioformats"]
    state: Literal["ready", "missing", "incompatible", "not_checked"]
    location: Path | None
    included: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ImageRuntimeDownloadInfo:
    """

    Describes the selected download before we confirm installation.

    Attributes:
        component: Java or Bio-Formats.
        version: Release selected by the installed program description.
        approximate_size_bytes: Archive size for Java or sum of the selected
            JAR download sizes for Bio-Formats.
        sources: Servers providing these downloads, displayed for review.
    """

    component: Literal["java", "bioformats"]
    version: str
    approximate_size_bytes: int
    sources: tuple[str, ...]


def _clean_absolute(path: Path, label: str) -> Path:
    """

    Checks the absolute directory supplied for Java, Bio-Formats or application-managed files.

    Args:
        path: Path selected by the setup controller.
        label: Directory description used in an error.

    Returns:
        Checked absolute path without parent-directory components.

    Raises:
        ValueError: The value is not a Path, is relative or contains '..'.
    """

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute path without traversal")
    return path


def _resource_json(name: str) -> dict:
    """

    Reads the release or download section used by the selected image-reading component.

    Java's section contains its platform archives, source archive and release
    information. Bio-Formats' section lists each JAR download and the checksum
    of the required-file description to which those downloads correspond.

    Args:
        name: Java or Bio-Formats selected by the setup operation.

    Returns:
        Independent release/download dictionary for _pins() to check.

    Raises:
        ImageRuntimeSetupError: The program description is missing or invalid.
    """

    try:
        description = read_software_description(name)
        return description["release" if name == "java" else "downloads"]
    except (KeyError, ValueError, TypeError) as exc:
        raise ImageRuntimeSetupError("pin_record_invalid", f"Cannot read the installed {name} software description") from exc


def _pins(platform: str, component: str | None = None) -> tuple[dict, list[dict]]:
    """

    Checks the selected download descriptions against the supported Java and Bio-Formats files.

    For Java, we require the selected Temurin release, the supported platform
    entries and complete HTTPS URL, size and checksum fields. For Bio-Formats,
    each download must match the corresponding required JAR in its recorded
    order, including its dependency coordinate, relative path and checksum.
    The download list's recorded file-list checksum must also agree.

    This comparison keeps the files used by installation and by image-reader
    verification consistent. We validate only the component being requested
    when component is supplied, allowing Java and Bio-Formats to be installed
    through separate setup actions.

    Args:
        platform: linux-x86_64 or win-x86_64.
        component: Optional Java or Bio-Formats selection. None checks both.

    Returns:
        Java archive dictionary and ordered Bio-Formats JAR list. The unrequested
        component is represented by an empty dictionary or list.

    Raises:
        ImageRuntimeSetupError: Releases, downloads, HTTPS locations or their
            relationship to the required files are invalid.
    """

    java = _resource_json("java") if component != "bioformats" else None
    bf = _resource_json("bioformats") if component != "java" else None
    try:
        if java is not None and (
            set(java)
            != {
                "distribution",
                "license_url",
                "platforms",
                "release_url",
                "schema_name",
                "schema_version",
                "source",
                "version",
            }
            or java["schema_name"] != "java-runtime-download"
            or java["schema_version"] != 1
            or java["version"] != _JAVA_VERSION
            or java["distribution"] != "Eclipse Temurin"
            or set(java["platforms"]) != _PLATFORMS
        ):
            raise ValueError("Java record schema differs")
        release = java["platforms"][platform] if java is not None else {}
        if java is not None:
            _pin_fields(release)
            for field in ("release_url", "license_url"):
                validate_https_url(java[field], frozenset({"github.com", "raw.githubusercontent.com"}))
            _pin_fields(java["source"])
        if bf is None:
            return release, []
        lock = load_approved_bioformats_lock()
        if (
            bf["schema_name"] != "bioformats-runtime-download"
            or bf["schema_version"] != 1
            or bf["runtime_lock_sha256"] != lock.sha256
            or len(bf["jars"]) != len(lock.jars)
        ):
            raise ValueError("record schema or lock binding differs")
        for row, jar, coordinate in zip(bf["jars"], lock.jars, lock.coordinates, strict=True):
            if (
                set(row) != {"coordinate", "relative_path", "sha256", "size", "url"}
                or row["coordinate"] != coordinate
                or row["relative_path"] != jar.relative_path.as_posix()
                or row["sha256"] != jar.sha256
                or type(row["size"]) is not int
                or row["size"] <= 0
            ):
                raise ValueError("Bio-Formats projection differs from lock")
            validate_https_url(
                row["url"],
                frozenset(
                    {
                        "repo.maven.apache.org",
                        "artifacts.openmicroscopy.org",
                        "maven.scijava.org",
                    }
                ),
            )
        return release, bf["jars"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ImageRuntimeSetupError(
            "pin_record_invalid", "Runtime download record is invalid"
        ) from exc


def image_runtime_download_info(component: str, platform: str) -> ImageRuntimeDownloadInfo:
    """

    Calculates the selected component's download size and source labels for the confirmation message.

    Java uses its selected platform archive size. Bio-Formats adds the sizes of
    all required JAR downloads and lists their distinct servers alphabetically.

    Args:
        component: Java or Bio-Formats.
        platform: Supported operating system and architecture.

    Returns:
        ImageRuntimeDownloadInfo used before confirming the network transfer.

    Raises:
        ValueError: The component or platform is unsupported.
        ImageRuntimeSetupError: Its installed download description fails validation.
    """

    if component not in ("java", "bioformats") or platform not in _PLATFORMS:
        raise ValueError("unsupported image runtime download")
    java, jars = _pins(platform, component)
    if component == "java":
        return ImageRuntimeDownloadInfo(
            "java", _JAVA_VERSION, java["size"], ("github.com/adoptium/temurin21-binaries",)
        )
    hosts = tuple(sorted({urlsplit(row["url"]).hostname for row in jars}))
    return ImageRuntimeDownloadInfo("bioformats", "8.5.0", sum(row["size"] for row in jars), hosts)




def _pin_fields(row: dict) -> None:
    """

    Checks one exact software download's location, byte count and checksum.

    Args:
        row: Dictionary containing url, sha256 and positive integer size.

    Raises:
        ValueError: Required fields, checksum or size are invalid, or the Java
            download URL leaves its permitted HTTPS server.
    """

    if (
        type(row) is not dict
        or set(row) != {"url", "sha256", "size"}
        or type(row["sha256"]) is not str
        or _HASH.fullmatch(row["sha256"]) is None
        or (type(row["size"]) is not int or row["size"] <= 0)
    ):
        raise ValueError("invalid pinned artifact")
    validate_https_url(row["url"], frozenset({"github.com"}))


class _PinnedRedirects(PinnedRedirects):
    """

    Uses the shared redirect checks and reports failures through the image-setup exception type.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """

        Checks a proposed redirect before urllib creates its next request.

        Args:
            req: Original urllib request.
            fp: Current response stream supplied by urllib.
            code: Redirect response status.
            msg: Response status message.
            headers: HTTP response headers.
            newurl: Proposed next download location.

        Returns:
            Redirect request after the shared HTTPS/server checks succeed.

        Raises:
            ImageRuntimeSetupError: The redirect leaves a permitted download location.
        """

        try:
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        except SetupTransferError as exc:
            raise ImageRuntimeSetupError(exc.code, exc.detail) from exc


def _download(
    url: str, destination: Path, expected_hash: str, expected_size: int | None = None
) -> None:
    """

    Writes a selected download using the shared byte-count and SHA-256 checks.

    Args:
        url: Recorded software download address.
        destination: New temporary file selected by the installer.
        expected_hash: Complete checksum recorded for this download.
        expected_size: Expected byte count, or None for a checksum-only caller.

    Raises:
        ImageRuntimeSetupError: Download, redirected location, byte count or
            complete checksum fails its check.
    """

    try:
        download_pinned_file(
            url,
            destination,
            expected_hash,
            expected_size,
            opener=build_opener(_PinnedRedirects(urlsplit(url).hostname)),
        )
    except SetupTransferError as exc:
        raise ImageRuntimeSetupError(exc.code, exc.detail) from exc


def _entry_path(name: str) -> PurePosixPath:
    """

    Checks the location of one entry in the downloaded Java archive.

    We require the selected Java release's archive directory as the first
    component. The next component, when present, must be a program, configuration,
    library, release or licence location used by that distribution. Absolute
    paths, parent-directory components, backslashes and drive-name separators
    are rejected before constructing an extraction destination.

    Args:
        name: File or directory path read from the archive.

    Returns:
        Relative archive path under the expected Java release directory.

    Raises:
        ImageRuntimeSetupError: The path leaves the release directory or uses
            an unsupported entry location.
    """

    if type(name) is not str or "\\" in name or ":" in name or name.startswith("/"):
        raise ImageRuntimeSetupError("unsafe_archive", "Unsafe Java archive entry")
    path = PurePosixPath(name.rstrip("/"))
    if (
        path == PurePosixPath(".")
        or not path.parts
        or path.parts[0] != _ARCHIVE_ROOT
        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
    ):
        raise ImageRuntimeSetupError("unsafe_archive", "Java archive escapes its runtime root")
    if len(path.parts) > 1 and path.parts[1] not in {
        "NOTICE",
        "bin",
        "conf",
        "legal",
        "lib",
        "release",
    }:
        raise ImageRuntimeSetupError("unsafe_archive", "Unexpected Java archive entry")
    return path


def _extract_tar(archive: Path, destination: Path) -> None:
    """

    Expands the Linux Java archive into the selected temporary installation directory.

    We validate each entry and create directories or regular files beneath that
    location. File content is copied in one-megabyte blocks; executable entries
    receive mode 0700 and other files mode 0600. Duplicate paths and unsupported
    special entries are rejected.

    The distribution contains a small set of licence aliases pointing into
    legal/java.base. After regular files are written, we require each alias
    to match its permitted target and copy that target's text as an ordinary
    file. This preserves the licence content without installing directory links.

    Args:
        archive: Downloaded and checksum-verified .tar.gz Java distribution.
        destination: New temporary directory receiving the expanded contents.

    Raises:
        ImageRuntimeSetupError: An entry, duplicate path, licence alias or
            extraction operation is invalid or incomplete.
    """

    aliases = []
    seen: set[PurePosixPath] = set()
    try:
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                path = _entry_path(member.name)
                if path in seen:
                    raise ImageRuntimeSetupError("unsafe_archive", "Duplicate Java archive entry")
                seen.add(path)
                relative = Path(*path.parts[1:])
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as incoming, target.open("xb") as output:
                        shutil.copyfileobj(incoming, output, _BLOCK)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
                elif member.issym():
                    aliases.append((path, member.linkname))
                else:
                    raise ImageRuntimeSetupError("unsafe_archive", "Unsupported Java archive entry")
            for path, link in aliases:
                if (
                    len(path.parts) != 4
                    or path.parts[1] != "legal"
                    or path.name
                    not in {"LICENSE", "ASSEMBLY_EXCEPTION", "ADDITIONAL_LICENSE_INFO", "cldr.md"}
                ):
                    raise ImageRuntimeSetupError("unsafe_archive", "Unexpected Java archive link")
                expected = f"../java.base/{path.name}"
                if link != expected:
                    raise ImageRuntimeSetupError(
                        "unsafe_archive", "Java legal alias escapes archive"
                    )
                origin = PurePosixPath(_ARCHIVE_ROOT, "legal", "java.base", path.name)
                if origin not in seen:
                    raise ImageRuntimeSetupError(
                        "unsafe_archive", "Java legal alias target is absent"
                    )
                source_file = destination / "legal" / "java.base" / path.name
                if not source_file.is_file() or source_file.is_symlink():
                    raise ImageRuntimeSetupError(
                        "unsafe_archive", "Java legal alias target is unsafe"
                    )
                alias = destination.joinpath(*path.parts[1:])
                alias.parent.mkdir(parents=True, exist_ok=True)
                with source_file.open("rb") as incoming, alias.open("xb") as output:
                    shutil.copyfileobj(incoming, output, _BLOCK)
    except ImageRuntimeSetupError:
        raise
    except (OSError, tarfile.TarError, EOFError, AttributeError) as exc:
        raise ImageRuntimeSetupError(
            "unsafe_archive", "Cannot safely extract Java archive"
        ) from exc


def _extract_zip(archive: Path, destination: Path) -> None:
    """

    Expands the Windows Java archive into a new temporary directory.

    We validate every relative entry and reject duplicate paths using a
    case-insensitive comparison. Only directories and regular files are admitted.
    Each file is created exclusively and copied in one-megabyte blocks beneath
    the checked Java release location.

    Args:
        archive: Downloaded and checksum-verified Java ZIP file.
        destination: New temporary directory for the expanded distribution.

    Raises:
        ImageRuntimeSetupError: An entry type/path, duplicate name or extraction
            operation fails the required checks.
    """

    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                path = _entry_path(member.filename)
                folded = path.as_posix().casefold()
                if folded in seen:
                    raise ImageRuntimeSetupError("unsafe_archive", "Duplicate Java archive entry")
                seen.add(folded)
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ImageRuntimeSetupError("unsafe_archive", "Unsupported Java archive entry")
                target = destination.joinpath(*path.parts[1:])
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    if kind == stat.S_IFDIR:
                        raise ImageRuntimeSetupError("unsafe_archive", "Invalid Java archive file")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as incoming, target.open("xb") as output:
                        shutil.copyfileobj(incoming, output, _BLOCK)
    except ImageRuntimeSetupError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ImageRuntimeSetupError(
            "unsafe_archive", "Cannot safely extract Java archive"
        ) from exc


def _private_parent(target: Path) -> None:
    """

    Checks the application-managed directory before writing a completed software installation.

    We require an existing parent and reject symbolic links or Windows junctions
    along its path. On systems exposing a user ID, the parent must belong to
    the current user and have no group/other access bits. Installation creates
    that directory with mode 0700 before invoking this check.

    Args:
        target: Final versioned software directory to be created beneath its parent.

    Raises:
        ImageRuntimeSetupError: The parent is missing, linked or has incompatible permissions.
    """

    for path in (*reversed(target.parents), target.parent):
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            raise ImageRuntimeSetupError(
                "unsafe_target", "Managed installation path has a linked ancestor"
            )
    parent = target.parent
    if not parent.is_dir():
        raise ImageRuntimeSetupError("unsafe_target", "Managed installation parent is absent")
    details = parent.stat()
    if hasattr(os, "getuid") and (
        details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ImageRuntimeSetupError("unsafe_target", "Managed installation parent must be private")


def _publish_no_replace(source: Path, target: Path, platform: str) -> None:
    """

    Moves a completed installation into its final name through the shared no-overwrite operation.

    Args:
        source: Verified temporary installation directory.
        target: Final versioned Java or Bio-Formats directory.
        platform: Supported platform selecting the native rename operation.

    Raises:
        ImageRuntimeSetupError: The platform cannot provide the required rename.
        OSError: The destination exists or the filesystem move fails.
    """

    try:
        publish_no_replace(source, target, platform)
    except PublicationError as exc:
        raise ImageRuntimeSetupError(exc.code, exc.detail) from exc


class ImageRuntimeSetupService:
    """

    Checks the selected image reader and prepares requested Java or Bio-Formats installations.

    We keep the selected component directories for inspection and use an
    application-data directory for new installations. The controller persists
    a successful selection in the desktop settings after reviewing the returned
    status. Packaged application files remain separate from these installations.
    """

    def __init__(
        self,
        platform: str,
        *,
        java_home: Path | None,
        bioformats_root: Path | None,
        managed_root: Path,
        frozen_root: Path | None = None,
    ) -> None:
        """

        Records the selected component paths and the directories permitted for installation.

        Args:
            platform: linux-x86_64 or win-x86_64.
            java_home: Optional selected Java directory containing bin and its JVM library.
            bioformats_root: Optional directory containing the required JAR set.
            managed_root: Application-data directory receiving new versioned installations.
            frozen_root: Packaged application directory when running a frozen executable.

        Raises:
            ValueError: A path or platform is invalid, or the installation directory
                is the filesystem root or lies inside the packaged application.
        """

        if platform not in _PLATFORMS:
            raise ValueError("unsupported image runtime setup platform")
        self.platform = platform
        self.java_home = _clean_absolute(java_home, "Java home") if java_home is not None else None
        self.bioformats_root = (
            _clean_absolute(bioformats_root, "Bio-Formats root")
            if bioformats_root is not None
            else None
        )
        self.managed_root = _clean_absolute(managed_root, "managed root")
        self.frozen_root = (
            _clean_absolute(frozen_root, "frozen root") if frozen_root is not None else None
        )
        if self.managed_root == Path(self.managed_root.anchor) or (
            self.frozen_root is not None
            and self.managed_root.resolve().is_relative_to(self.frozen_root.resolve())
        ):
            raise ValueError(
                "managed installation root must be separate from the bundle and filesystem root"
            )
        require_external_bioformats_root(None, frozen_root=self.frozen_root)

    def _candidate_statuses(
        self, java_home: Path | None, bf_root: Path | None
    ) -> tuple[ImageRuntimeSetupStatus, ...]:
        """

        Checks the selected files and then tests whether Java and Bio-Formats load together.

        We first compare the Bio-Formats directory with the complete required JAR
        list and inspect the Java program and JVM library. If both selections are
        usable, the prerequisite process opens the image-reading environment. When
        no Java directory was explicitly selected, that process can discover the
        installed JVM and report its actual location.

        A successful combined check marks both components ready. A Java failure
        leaves verified Bio-Formats files awaiting a working JVM; a Bio-Formats
        failure identifies its file/loading problem separately. A process error
        leaves the check incomplete rather than treating the file listing alone
        as confirmation that image reading works.

        Args:
            java_home: Java directory to test, or None for discovery by the prerequisite process.
            bf_root: Bio-Formats directory to test, or None when absent.

        Returns:
            Java and Bio-Formats status records in that order, with locations and
            the specific file or loading results.
        """

        included = False
        bf_inventory: BioFormatsRuntimeInventory | None = None
        if bf_root is None:
            bf = ImageRuntimeSetupStatus(
                "bioformats",
                "missing",
                None,
                included,
                "Select or install the locked Bio-Formats files.",
            )
        else:
            try:
                require_external_bioformats_root(bf_root, frozen_root=self.frozen_root)
                bf_inventory = verify_approved_bioformats_payload(bf_root)
                bf = ImageRuntimeSetupStatus(
                    "bioformats",
                    "not_checked",
                    bf_root,
                    included,
                    "Locked files verified; checking the loaded runtime.",
                )
            except InventoryMissing as exc:
                bf = ImageRuntimeSetupStatus("bioformats", "missing", bf_root, included, str(exc))
            except InventoryIncompatible as exc:
                bf = ImageRuntimeSetupStatus(
                    "bioformats", "incompatible", bf_root, included, str(exc)
                )
        java = self._check_java(java_home)
        if bf_inventory is None and java.state == "ready":
            java = ImageRuntimeSetupStatus(
                "java",
                "not_checked",
                java_home,
                False,
                "Java 21 runs; add Bio-Formats to check image reading.",
            )
        if bf_inventory is not None and (java.state == "ready" or java_home is None):
            probe = probe_bioformats_prerequisite(bf_inventory, java_home=java_home)
            if probe.status == "ready":
                observed_home = (
                    java_home
                    if java_home is not None
                    else Path(probe.evidence.java_home) if probe.evidence is not None else None
                )
                java = ImageRuntimeSetupStatus("java", "ready", observed_home, False, probe.detail)
                bf = ImageRuntimeSetupStatus("bioformats", "ready", bf_root, included, probe.detail)
            elif probe.status == "error":
                detail = f"Image-reading check failed ({probe.code}): {probe.detail}"
                java = ImageRuntimeSetupStatus("java", "not_checked", java_home, False, detail)
                bf = ImageRuntimeSetupStatus("bioformats", "not_checked", bf_root, included, detail)
            elif (
                probe.code in _JAVA_FAILURE_CODES
                or probe.code.startswith("java_")
                or probe.code.startswith("jvm_")
            ):
                java = ImageRuntimeSetupStatus(
                    "java",
                    "missing" if probe.code in _JAVA_MISSING_CODES else "incompatible",
                    java_home,
                    False,
                    probe.detail,
                )
                bf = ImageRuntimeSetupStatus(
                    "bioformats",
                    "not_checked",
                    bf_root,
                    included,
                    "Bio-Formats files verified; Java runtime check failed.",
                )
            else:
                bf = ImageRuntimeSetupStatus(
                    "bioformats", "incompatible", bf_root, included, probe.detail
                )
        return java, bf

    def _check_java(self, home: Path | None) -> ImageRuntimeSetupStatus:
        """

        Checks the selected Java program, JVM shared library and reported feature version.

        We require regular java/java.exe, JVM-library and release files. On the
        matching operating system we run java -version with a 15-second limit and
        require a successful Java 21 response. When inspecting a directory for a
        different operating system, execution remains not_checked.

        Args:
            home: Selected Java home directory, or None when not selected.

        Returns:
            ImageRuntimeSetupStatus describing missing files, execution incompatibility,
            an unperformed platform check or a successfully running Java 21 program.
        """

        if home is None:
            return ImageRuntimeSetupStatus(
                "java", "not_checked", None, False, "Select Java 21 to verify image reading."
            )
        binary = home / "bin" / ("java.exe" if self.platform == "win-x86_64" else "java")
        library = home / (
            "bin/server/jvm.dll" if self.platform == "win-x86_64" else "lib/server/libjvm.so"
        )
        for path in (binary, library, home / "release"):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return ImageRuntimeSetupStatus(
                    "java", "missing", home, False, f"Java runtime file is missing: {path}"
                )
            if not stat.S_ISREG(info.st_mode):
                return ImageRuntimeSetupStatus(
                    "java", "incompatible", home, False, f"Unsafe Java runtime file: {path}"
                )
        if (self.platform == "win-x86_64") != (sys.platform == "win32"):
            return ImageRuntimeSetupStatus(
                "java",
                "not_checked",
                home,
                False,
                "Java execution must be checked on the selected platform.",
            )
        try:
            result = subprocess.run(
                [str(binary), "-version"], capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ImageRuntimeSetupStatus(
                "java", "incompatible", home, False, f"Java cannot run: {exc}"
            )
        match = re.search(r'version "(\d+)(?:[.+-][^\"]*)?"', result.stderr + result.stdout)
        if result.returncode or match is None or int(match.group(1)) != 21:
            return ImageRuntimeSetupStatus(
                "java",
                "incompatible",
                home,
                False,
                "Java must execute successfully as feature version 21.",
            )
        return ImageRuntimeSetupStatus(
            "java", "ready", home, False, "Java 21 executable and JVM library verified."
        )

    def inspect(self) -> tuple[ImageRuntimeSetupStatus, ...]:
        """

        Checks the currently selected Java and Bio-Formats directories.

        Returns:
            Status records for Java and Bio-Formats after their file and combined
            image-reading checks.
        """

        return self._candidate_statuses(self.java_home, self.bioformats_root)

    def select(self, component: str, location: Path) -> ImageRuntimeSetupStatus:
        """

        Tests one supplied component location together with the other current selection.

        Args:
            component: Java or Bio-Formats being changed in Software setup.
            location: Absolute directory selected for that component.

        Returns:
            Status of the tested component. The controller uses this result when
            deciding whether to save the location in desktop settings.

        Raises:
            ValueError: The component or selected directory format is invalid.
        """

        if component not in ("java", "bioformats"):
            raise ValueError("unknown image runtime component")
        location = _clean_absolute(location, "selection")
        statuses = self._candidate_statuses(
            location if component == "java" else self.java_home,
            location if component == "bioformats" else self.bioformats_root,
        )
        return statuses[0 if component == "java" else 1]

    def install(self, component: str, *, consent: bool) -> ImageRuntimeSetupStatus:
        """

        Downloads, checks and installs the selected image-reading component after confirmation.

        We first inspect an existing versioned destination and reuse a ready or
        not-yet-fully-checked installation. An incompatible existing directory is
        retained for correction. A new installation requires consent and is prepared
        beside its final destination so the last rename stays on one filesystem.

        For Java, we download and verify its archive, extract the distribution and
        require Java 21 to run. For Bio-Formats, we download each recorded JAR to
        its relative location and verify the complete resulting file set. The
        destination must still be unused before the verified temporary directory
        is moved into place. Finally, we test that new location with the currently
        selected complementary component.

        Args:
            component: Java or Bio-Formats to prepare.
            consent: Explicit approval of this component's download.

        Returns:
            Status of the installed component, including any remaining combined
            image-reading check that depends on the other selection.

        Raises:
            ImageRuntimeSetupError: Consent is absent or a download, archive entry,
                program check, existing directory or final installation fails.
            ValueError or TypeError: Component name or consent type is invalid.
        """

        if component not in ("java", "bioformats"):
            raise ValueError("unknown image runtime component")
        if type(consent) is not bool:
            raise TypeError("image runtime setup consent must be bool")
        target = (
            self.managed_root
            / ("java" if component == "java" else "bioformats")
            / (_JAVA_DIRECTORY if component == "java" else _BF_DIRECTORY)
        )
        if target.exists() or target.is_symlink():
            status = self.select(component, target)
            if status.state in ("ready", "not_checked"):
                return status
            raise ImageRuntimeSetupError(
                "existing_target_incompatible", "Existing setup target was not overwritten"
            )
        if not consent:
            raise ImageRuntimeSetupError("consent_required", "Download requires explicit consent")
        java_pin, jars = _pins(self.platform, component)
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _private_parent(target)
        with tempfile.TemporaryDirectory(prefix="image-runtime-", dir=target.parent) as scratch:
            stage = Path(scratch)
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            if component == "java":
                archive = stage / ("java.zip" if self.platform == "win-x86_64" else "java.tar.gz")
                _download(java_pin["url"], archive, java_pin["sha256"], java_pin["size"])
                (_extract_zip if self.platform == "win-x86_64" else _extract_tar)(archive, payload)
                if self._check_java(payload).state != "ready":
                    raise ImageRuntimeSetupError(
                        "java_validation_failed", "Downloaded Java cannot execute as Java 21"
                    )
            else:
                for row in jars:
                    relative = PurePosixPath(row["relative_path"])
                    destination = payload.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _download(row["url"], destination, row["sha256"], row["size"])
                verify_approved_bioformats_payload(payload)
            if target.exists() or target.is_symlink():
                raise ImageRuntimeSetupError(
                    "existing_target_incompatible", "Setup target appeared during staging"
                )
            try:
                _publish_no_replace(payload, target, self.platform)
            except OSError as exc:
                raise ImageRuntimeSetupError(
                    "publication_failed", "Verified setup could not be published"
                ) from exc
        return self.select(component, target)
