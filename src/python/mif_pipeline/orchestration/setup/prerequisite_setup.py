"""

Installs the selected CPU PyTorch libraries used by the InstanSeg extension in QuPath.

We obtain the native-library and Java/native-bridge archives from the selected
PyTorch software description. Their complete byte counts and checksums are
verified before reading the required library entries. The extracted files
are then checked against the platform-specific list and moved into a
versioned application-data directory.

This operation is invoked through the explicit cell-segmentation libraries
setup action. The later QuPath prerequisite check opens these libraries
with the extension and model to confirm that inference can run.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
import re
import stat
import sys
import tempfile
from typing import Callable, Literal, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from mif_pipeline.qupath_runtime.payload_lock import SOURCE_ARCHIVE_LOCK, cpu_native_file_lock, verify_cpu_native_directory
from mif_pipeline.qupath_runtime.prerequisite_probe import LocalInventoryCheck

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK = 1024 * 1024
_PLATFORMS = ("linux-x86_64", "win-x86_64")


# ==============================================================================
#                               Prerequisite setup
# ==============================================================================


def default_cpu_setup_target(
    platform: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """

    Selects the versioned application-data directory for the platform's CPU inference libraries.

    Windows uses LOCALAPPDATA or the home directory's AppData/Local folder.
    Linux uses XDG_DATA_HOME or .local/share. We append the pipeline, CPU release
    and platform directories so all analyses selecting this library pair can
    reuse the same installation. The version directory combines the PyTorch
    version and Java-engine version read from software/pytorch.json.

    Args:
        platform: linux-x86_64 or win-x86_64.
        environ: Optional environment mapping, otherwise the current process environment.
        home: Optional home directory used by the platform fallback.

    Returns:
        Absolute directory for the selected native PyTorch and Java bridge versions.

    Raises:
        ValueError: Platform, application-data root or the selected program
            version fields are invalid.
    """

    if platform not in _PLATFORMS:
        raise ValueError("unsupported CPU setup platform")
    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    root = (
        Path(values.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        if platform == "win-x86_64"
        else Path(values.get("XDG_DATA_HOME") or user_home / ".local" / "share")
    )
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("CPU setup data root must be absolute")
    from mif_pipeline._software import read_software_description

    software = read_software_description("pytorch")
    versions = (software.get("version"), software.get("java_engine_version"))
    if any(type(value) is not str or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None for value in versions):
        raise ValueError("The selected PyTorch and Java-engine versions must use major.minor.patch numbers.")
    return root / "mif-pipeline" / "cpu" / "-".join(versions) / platform


class CpuSetupError(RuntimeError):
    """

    Reports failure to obtain, check or install the selected native CPU libraries.
    """

class CpuSetupConsentRequired(CpuSetupError):
    """

    Reports that the missing CPU libraries require explicit download confirmation.
    """

@dataclass(frozen=True)
class ArchivePin:
    """

    Retains the exact Maven download selected for a native-library or Java-bridge archive.

    Attributes:
        url: HTTPS JAR location under the supported DJL PyTorch Maven directory.
        size: Positive expected archive byte count.
        sha256: Checksum of the complete selected archive.
    """

    url: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        """

        Checks the archive's download location, required byte count and checksum.

        We require the supported Maven server and DJL PyTorch path, a JAR filename,
        and no query or fragment. The byte count must be a positive integer and
        the checksum complete lowercase hexadecimal SHA-256 text.

        Raises:
            ValueError: A download field is invalid or leaves the supported location.
        """

        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "repo.maven.apache.org"
            or not parsed.path.startswith("/maven2/ai/djl/pytorch/")
            or not parsed.path.endswith(".jar")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CPU setup archive URL must be an exact Maven HTTPS URL")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ValueError("CPU setup archive size must be positive")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("CPU setup archive SHA-256 is invalid")


@dataclass(frozen=True)
class CpuSetupResult:
    """

    Identifies whether the checked CPU installation was newly written or reused.

    Attributes:
        status: Installed for a new directory, or already_present for a verified
            existing installation.
        directory: Final directory containing the selected native libraries.
    """

    status: Literal["installed", "already_present"]
    directory: Path


@dataclass(frozen=True)
class CpuSetupStatus:
    """

    Reports the CPU-library file-check result used by Software setup.

    Attributes:
        state: Ready when required files match, missing when absent, or incompatible
            when existing files differ from the selected release.
        target: Selected installation directory.
        detail: Explanation displayed beside the library setup controls.
    """

    state: Literal["ready", "missing", "incompatible"]
    target: Path
    detail: str

    @property
    def ready(self) -> bool:
        """

        Reports whether the selected native-library files passed their installation check.

        Returns:
            True when state equals ready.
        """

        return self.state == "ready"


class CpuSetupService:
    """

    Provides the file inspection and confirmed installation actions for CPU inference libraries.
    """

    def __init__(self, target: Path, platform: str) -> None:
        """

        Retains the selected platform and absolute library installation directory.

        Args:
            target: Versioned application-data directory to inspect or create.
            platform: linux-x86_64 or win-x86_64.

        Raises:
            ValueError: Platform or path representation is invalid.
        """

        if not isinstance(target, Path) or not target.is_absolute() or ".." in target.parts:
            raise ValueError("CPU setup target must be an absolute path without traversal")
        if platform not in _PLATFORMS:
            raise ValueError("unsupported CPU setup platform")
        self.target = target
        self.platform = platform

    def inspect(self) -> CpuSetupStatus:
        """

        Compares the selected directory with the platform's required native-library files.

        Returns:
            CpuSetupStatus with the required-file result and the selected directory.
            Runtime loading is performed by the separate QuPath prerequisite check.
        """

        check = verify_cpu_native_directory(self.target, self.platform)
        state = {
            "verified": "ready",
            "missing": "missing",
            "incompatible": "incompatible",
        }[check.status]
        detail = {
            "ready": "Pinned CPU libraries verified.",
            "missing": f"CPU libraries are not installed at {self.target}.",
            "incompatible": (
                f"CPU library files at {self.target} are incomplete or differ "
                "from the pinned release. They will not be overwritten."
            ),
        }[state]
        return CpuSetupStatus(state, self.target, detail)

    def install(self, *, consent: bool) -> CpuSetupStatus:
        """

        Prepares the selected CPU library directory after download confirmation.

        With consent, we create missing parent directories from the highest absent
        parent downward, using mode 0700. The common installer then checks that the
        destination is unused or already contains the verified library set before
        downloading and preparing a new installation.

        Args:
            consent: Explicit confirmation of the required archive downloads.

        Returns:
            Fresh file-check status for the completed or reused library directory.

        Raises:
            TypeError: Consent is not boolean.
            CpuSetupError: Parent directories, existing files, downloads or installation fail.
        """

        if type(consent) is not bool:
            raise TypeError("CPU setup consent must be bool")
        if consent:
            if any(parent.is_symlink() for parent in self.target.parents):
                raise CpuSetupError("CPU setup parent contains a link")
            absent: list[Path] = []
            member = self.target.parent
            while not member.exists():
                if member.is_symlink():
                    raise CpuSetupError("CPU setup parent contains a link")
                absent.append(member)
                member = member.parent
            for directory in reversed(absent):
                directory.mkdir(mode=0o700)
        setup_cpu_payload(self.target, self.platform, consent=consent)
        return self.inspect()


class _OpenResponse(Protocol):
    """

    Defines the response operations required by the checked CPU archive download.

    The downloader uses the response as a context manager, confirms its final
    URL and reads bounded byte blocks. A supplied test or custom opener must
    provide those same operations.
    """

    def __enter__(self) -> "_OpenResponse":
        """

        Opens the response for checked archive reads within a with block.

        Returns:
            Response exposing geturl() and read().
        """

        ...
    def __exit__(self, *exc: object) -> object:
        """

        Releases the response when archive reading completes or raises an exception.

        Args:
            exc: Exception type, value and traceback supplied by the context manager.

        Returns:
            Context-manager result supplied by the concrete HTTP response.
        """

        ...
    def geturl(self) -> str:
        """

        Reports the final address used for the downloaded response.

        Returns:
            URL compared with the archive's recorded exact location.
        """

        ...
    def read(self, size: int) -> bytes:
        """

        Reads the next bounded block of response bytes.

        Args:
            size: Maximum requested bytes, normally one MiB.

        Returns:
            Next archive bytes, or an empty value at the end of the response.
        """

        ...


class _Opener(Protocol):
    """

    Defines the HTTP opening operation supplied to CPU setup.
    """

    def open(self, request: Request, timeout: float) -> _OpenResponse:
        """

        Opens the selected archive request using the given network timeout.

        Args:
            request: Request containing the recorded HTTPS URL and application User-Agent.
            timeout: Connection timeout supplied by the downloader, in seconds.

        Returns:
            Response supporting the context-manager and bounded-read operations above.
        """

        ...


class _NoRedirect(HTTPRedirectHandler):
    """

    Rejects redirects for the selected exact Maven archive locations.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """

        Stops a CPU download when its server proposes another location.

        Args:
            req: Original archive request.
            fp: Current response stream.
            code: HTTP redirect status.
            msg: Status message.
            headers: Redirect response headers.
            newurl: Proposed replacement location.

        Raises:
            CpuSetupError: The selected archive download cannot be redirected.
        """

        raise CpuSetupError("CPU setup archive redirect is not permitted")


def _private_parent(target: Path) -> None:
    """

    Checks the existing parent directory before preparing a CPU-library installation.

    The selected path must be absolute. Its parent must be a regular directory,
    with no symbolic link or Windows junction among its ancestors. Where user
    IDs are available, the parent must belong to the current user and have no
    group/other access permissions.

    Args:
        target: Final versioned directory beneath the checked parent.

    Raises:
        ValueError: The target path is invalid.
        CpuSetupError: The parent is missing, linked or has incompatible access permissions.
    """

    if not isinstance(target, Path) or not target.is_absolute() or ".." in target.parts:
        raise ValueError("CPU setup target must be an absolute path without traversal")
    parent = target.parent
    try:
        info = parent.lstat()
        for member in (parent, *parent.parents):
            if member.is_symlink() or (getattr(member, "is_junction", lambda: False)()):
                raise CpuSetupError("CPU setup parent has a linked ancestor")
    except OSError as exc:
        raise CpuSetupError("CPU setup parent is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise CpuSetupError("CPU setup parent must be a directory")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise CpuSetupError("CPU setup parent must be owned by this user and private")


def _download(pin: ArchivePin, destination: Path, opener: _Opener) -> None:
    """

    Writes one exact CPU archive while checking its location, byte count and checksum.

    We open the selected URL with a 30-second network timeout and compare the
    response's effective address with the request. Each one-megabyte block
    updates the running SHA-256 and byte count before writing. Reading stops
    with an error if the expected size is exceeded; end-of-response requires
    both the exact size and complete checksum.

    Args:
        pin: Checked download URL, byte count and archive checksum.
        destination: New temporary filename for the archive.
        opener: HTTP opener implementing the required response operations.

    Raises:
        CpuSetupError: URL, transfer, byte count or checksum fails its comparison.
    """

    digest = hashlib.sha256()
    count = 0
    request = Request(pin.url, headers={"User-Agent": "mif-pipeline-prototype-cpu-setup"})
    try:
        with opener.open(request, timeout=30.0) as response, destination.open("xb") as output:
            if response.geturl() != pin.url:
                raise CpuSetupError("CPU setup archive effective URL changed")
            while chunk := response.read(_BLOCK):
                count += len(chunk)
                if count > pin.size:
                    raise CpuSetupError("CPU setup archive exceeds pinned size")
                digest.update(chunk)
                output.write(chunk)
    except CpuSetupError:
        raise
    except (OSError, HTTPException) as exc:
        raise CpuSetupError("CPU setup download was interrupted") from exc
    if count != pin.size or digest.hexdigest() != pin.sha256:
        raise CpuSetupError("CPU setup archive size or digest does not match its pin")


def _extract(
    native_archive: Path,
    jni_archive: Path,
    directory: Path,
    platform: str,
    files: tuple[tuple[str, str], ...],
) -> None:
    """

    Reads the required native libraries from their selected CPU and Java-bridge archives.

    For each expected filename, the Java bridge is read beneath
    jnilib/<platform>/cpu/ and the other libraries beneath pytorch/cpu/<platform>/.
    We copy the selected entry into a new flat destination file in one-megabyte
    blocks and calculate its checksum while writing. Each resulting library
    must match the corresponding required-file record.

    Args:
        native_archive: Verified platform CPU-library JAR archive.
        jni_archive: Verified Java/native-bridge JAR archive.
        directory: New temporary directory receiving the extracted libraries.
        platform: Selected operating system and architecture.
        files: Ordered expected filenames and complete-library checksums.

    Raises:
        CpuSetupError: A required archive entry is missing, unreadable or produces
            a library whose checksum differs from the selected release.
    """

    try:
        with zipfile.ZipFile(native_archive) as native, zipfile.ZipFile(jni_archive) as jni:
            for name, expected in files:
                if name.startswith(("libdjl_torch", "djl_torch")):
                    archive, member = jni, f"jnilib/{platform}/cpu/{name}"
                else:
                    archive, member = native, f"pytorch/cpu/{platform}/{name}"
                digest = hashlib.sha256()
                target = directory / name
                with (
                    archive.open(member) as source,
                    os.fdopen(
                        os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                    ) as output,
                ):
                    while chunk := source.read(_BLOCK):
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != expected:
                    raise CpuSetupError(f"CPU setup extracted library digest mismatch: {name}")
    except CpuSetupError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise CpuSetupError("CPU setup archive extraction failed") from exc


def _setup_with_lock(
    target: Path,
    platform: str,
    *,
    consent: bool,
    pins: tuple[ArchivePin, ArchivePin],
    native_files: tuple[tuple[str, str], ...],
    verifier: Callable[[Path], LocalInventoryCheck],
    opener: _Opener,
) -> CpuSetupResult:
    """

    Performs the confirmed download, extraction and installation using one complete set of selected file checks.

    We check the parent and reuse an existing verified installation. A missing
    destination requires consent. The two selected archives are downloaded into
    a temporary sibling directory, and their required files are extracted and
    checked as a complete native-library set.

    Before installation, the destination must still be unused. The verified
    directory is moved into place with the no-overwrite operation, and the
    temporary archive directory is cleaned up when the operation exits.

    Args:
        target: Absolute final library directory.
        platform: Supported platform.
        consent: Explicit archive-download confirmation.
        pins: CPU-library and Java-bridge archive descriptions, in that order.
        native_files: Expected extracted filenames and checksums.
        verifier: Function checking a completed native-library directory.
        opener: HTTP opener used for both checked downloads.

    Returns:
        CpuSetupResult identifying the new or reused installation.

    Raises:
        CpuSetupError: Consent, parent access, existing files, archive checks,
            extraction or final directory move fails.
        ValueError or TypeError: Platform or consent type is invalid.
    """

    _private_parent(target)
    if platform not in _PLATFORMS:
        raise ValueError("unsupported CPU setup platform")
    if not isinstance(consent, bool):
        raise TypeError("CPU setup consent must be bool")
    if target.exists() or target.is_symlink():
        if verifier(target).status == "verified":
            return CpuSetupResult("already_present", target)
        raise CpuSetupError("existing CPU setup target is incompatible; it was not overwritten")
    if not consent:
        raise CpuSetupConsentRequired("CPU setup requires explicit download consent")
    with tempfile.TemporaryDirectory(prefix="cpu-setup-", dir=target.parent) as temporary:
        stage = Path(temporary)
        native_jar, jni_jar = stage / "native.jar", stage / "jni.jar"
        _download(pins[0], native_jar, opener)
        _download(pins[1], jni_jar, opener)
        native = stage / "native"
        native.mkdir(mode=0o700)
        _extract(native_jar, jni_jar, native, platform, native_files)
        if verifier(native).status != "verified":
            raise CpuSetupError("extracted CPU setup inventory failed verification")
        if target.exists() or target.is_symlink():
            raise CpuSetupError("CPU setup target appeared during staging; it was not overwritten")
        try:
            publish_no_replace(native, target, platform)
        except OSError as exc:
            raise CpuSetupError(
                "CPU setup could not publish the verified native directory"
            ) from exc
    return CpuSetupResult("installed", target)


def setup_cpu_payload(
    target: Path,
    platform: str,
    *,
    consent: bool,
    opener: _Opener | None = None,
) -> CpuSetupResult:
    """

    Selects the platform's two archive descriptions and starts the checked CPU-library installation.

    We obtain the CPU and Java-bridge downloads and extracted-file list through
    the selected PyTorch software definition. The supplied directory verifier
    uses that same platform file list, keeping acquisition and later inspection
    on the same version and library contents.

    Args:
        target: Final absolute library installation directory.
        platform: linux-x86_64 or win-x86_64.
        consent: Explicit confirmation of the downloads.
        opener: Optional HTTP opener; omission uses the no-redirect implementation.

    Returns:
        CpuSetupResult describing the completed or already present installation.

    Raises:
        CpuSetupError: Installation fails its directory, network or file checks.
        ValueError or TypeError: A supplied platform, path or consent value is invalid.
    """

    files = cpu_native_file_lock(platform)
    native = SOURCE_ARCHIVE_LOCK[f"pytorch-native-cpu-{platform}"]
    jni = SOURCE_ARCHIVE_LOCK["pytorch-jni"]
    pins = (ArchivePin(*native), ArchivePin(*jni))
    return _setup_with_lock(
        target,
        platform,
        consent=consent,
        pins=pins,
        native_files=files,
        verifier=lambda path: verify_cpu_native_directory(path, platform),
        opener=opener or build_opener(_NoRedirect()),
    )
