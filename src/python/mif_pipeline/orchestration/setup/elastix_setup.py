"""

Installs or selects the complete Elastix and Transformix release used for automatic alignment.

We read the archive URL, byte count, checksum and installed file list from
config/software/elastix.json. Software setup checks each required program,
shared library and accompanying notice, then runs both programs with --version.
A successful selection is saved as the program folder used by registration.

Installation first prepares the complete release in a temporary directory
beside its destination. We check the extracted files and run both programs
there before moving that directory to its final versioned name. A failed
attempt therefore leaves the previously selected installation in place.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
import os, stat, tempfile, zipfile
from mif_pipeline.qupath_runtime.elastix_runtime import release_pin, default_elastix_root
from mif_pipeline.qupath_runtime.prerequisite_probe import ProbeFileExpectation, _file_check, _run_bounded_probe
from .setup_transfer import download_pinned_file

# ==============================================================================
#                                 Elastix setup
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ElastixSetupStatus:
    """

    Records whether the selected automatic-alignment programs can be used.

    Attributes:
        state: Ready, missing, incompatible or not_checked, as determined by
            the complete file and program-execution checks.
        location: Selected program folder when available.
        detail: Explanation supplied with the Software setup status.
    """

    state: str
    location: Path | None
    detail: str


def _safe_parent(path):
    """

    Creates and checks the parent directory in which the completed release will be installed.

    We inspect the selected absolute path and each ancestor, rejecting symbolic
    links, junctions and parent-directory components. Missing directories are
    created with mode 0700, and every component must be a directory. We record
    this parent's device and file ID so installation can check that it was not
    replaced while the archive was downloaded and verified.

    Args:
        path: Parent directory that will contain the versioned release folder.

    Returns:
        Parent directory's device ID and file ID.

    Raises:
        ValueError: A parent path or its directory type is unsuitable.
        OSError: The installation parent cannot be created or inspected.
    """

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Choose an absolute installation folder.")
    for item in (*reversed(path.parents), path):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Select the installation folder directly, without a linked directory.")
        if not item.exists():
            item.mkdir(mode=0o700)
        if not item.is_dir():
            raise ValueError("The installation parent is not a directory: " + str(item))
    value = path.stat()
    return value.st_dev, value.st_ino


class ElastixSetupService:
    """

    Checks, selects and installs the programs needed by automatic registration.

    We keep the selected installation separate from the managed download
    destination. An existing checked release can be selected without downloading
    another copy. The managed installation is created only by an explicit
    installation action with download permission.
    """

    def __init__(
        self,
        *,
        platform,
        target=None,
        selected=None,
        preferences_path=None,
        opener=None,
        process_runner=None,
        pin=None,
    ):
        """

        Resolves the platform, installation destination and program-checking functions.

        Args:
            platform: Supported operating system and architecture.
            target: Optional destination for the managed release.
            selected: Optional existing program folder to inspect.
            pin: Optional complete release description; normally read from elastix.json.
            preferences_path: Optional desktop settings filename used to retain a
                successful program selection.
            opener: Optional HTTP opener used for downloading the recorded archive.
            process_runner: Optional subprocess function used for the two --version checks.
        """

        self.platform = platform
        self.pin = pin or release_pin(platform)
        self.target = Path(target) if target is not None else default_elastix_root(platform)
        self.selected = Path(selected) if selected is not None else None
        self.preferences_path = Path(preferences_path) if preferences_path is not None else None
        self.opener = opener
        self.runner = process_runner or _run_bounded_probe

    def _programs(self, root):
        """

        Resolves the executable folder within an extracted release.

        Args:
            root: Directory containing all files of the selected release.

        Returns:
            root/bin for the Linux archive or root for the Windows archive, according
            to the release's program_directory field.
        """

        return root / self.pin["program_directory"]

    def _check(self, root):
        """

        Verifies every release file and runs Elastix and Transformix to confirm the selected version.

        We compare file type, exact byte count and complete SHA-256 for each required
        entry. Both executable paths must then run successfully and report the
        supported version through --version. Each program receives a 15-second
        timeout, and captured version output is limited before parsing.

        Args:
            root: Extracted release directory containing the program and library layout.

        Returns:
            ElastixSetupStatus with the executable folder and either a ready state
            or an explanation of the missing files, conflicting version or execution failure.
        """

        root = Path(root)
        programs = self._programs(root)
        if not root.is_dir() or root.is_symlink():
            return ElastixSetupStatus(
                "missing",
                programs,
                "Install Elastix and Transformix 5.3.1 or select their extracted program folder.",
            )
        try:
            for name, row in self.pin["files"].items():
                path = root / name
                if (
                    not path.is_file()
                    or path.stat().st_size != row["size_bytes"]
                    or _file_check(ProbeFileExpectation(path, row["sha256"])).status != "verified"
                ):
                    return ElastixSetupStatus(
                        "incompatible",
                        programs,
                        "This file is missing or differs from Elastix 5.3.1: "
                        + str(path)
                        + ". Select a complete extracted release using Use existing, or move the incomplete installation aside before selecting Install.",
                    )
            from mif_pipeline.qupath_runtime.elastix_runtime import check_elastix_programs
            check_elastix_programs(programs, process_runner=self.runner, platform=self.platform)
            return ElastixSetupStatus(
                "ready",
                programs,
                "Elastix and Transformix 5.3.1 are installed and both programs run.",
            )
        except (OSError, ValueError, RuntimeError) as error:
            return ElastixSetupStatus("incompatible", programs, str(error))

    def _selected_root(self, folder):
        """

        Resolves the complete release directory from a selected executable folder.

        Linux programs are stored in bin/, with shared libraries and notices in the
        surrounding release. Windows keeps its required files together. We use the
        recorded layout to find that complete release for verification.

        Args:
            folder: Existing program folder selected in Software setup.

        Returns:
            Release root used by the complete file checks.
        """

        folder = Path(folder)
        suffix = ".exe" if self.platform == "win-x86_64" else ""
        if (folder / ("elastix" + suffix)).is_file():
            return folder.parent if self.pin["program_directory"] == "bin" else folder
        return folder

    def inspect(self):
        """

        Finds and checks the currently selected automatic-alignment programs.

        We prefer the explicit selection, then the environment selection and saved
        desktop setting. With no selection, we inspect the managed versioned folder.
        The resulting state includes complete file checks and the two program calls.

        Returns:
            Status of the selected or managed Elastix/Transformix release.
        """

        selected = self.selected or os.environ.get("MIF_ELASTIX_DIRECTORY")
        if selected is None and self.preferences_path is not None:
            from mif_pipeline.pipeline_core.software_preferences import load_desktop_preferences

            prefs = load_desktop_preferences(self.preferences_path)
            selected = getattr(prefs, "elastix_directory", None)
        return self._check(self._selected_root(selected) if selected else self.target)

    def _remember(self, status):
        """

        Saves a successfully checked program folder in the desktop software settings.

        Args:
            status: Completed ElastixSetupStatus from the program check.

        Returns:
            The status used by the interface after recording a ready installation.
        """

        if status.state != "ready":
            raise ValueError(status.detail)
        if self.preferences_path is not None:
            from mif_pipeline.pipeline_core.software_preferences import load_desktop_preferences, write_desktop_preferences

            prefs = load_desktop_preferences(self.preferences_path)
            if prefs is None:
                raise ValueError(
                    "Select QuPath in Software setup before saving the automatic-alignment program location."
                )
            write_desktop_preferences(
                self.preferences_path, replace(prefs, elastix_directory=str(status.location))
            )
        self.selected = status.location
        return status

    def select(self, folder):
        """

        Checks an existing program folder and records it after successful verification.

        Args:
            folder: Absolute directory containing Elastix and Transformix.

        Returns:
            Status from the complete release and --version checks. A ready selection
            is retained in the desktop preferences for subsequent alignment requests.

        Raises:
            ValueError: The selected location is not an absolute directory path.
        """

        path = Path(folder)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Select the extracted Elastix 5.3.1 folder or its bin folder.")
        return self._remember(self._check(self._selected_root(path)))

    def install(self, *, consent):
        """

        Downloads, verifies and selects the supported release without replacing an existing version.

        We first inspect the managed destination and reuse it when ready. A new
        installation requires explicit download permission. The archive's size and
        checksum are checked before extraction; its filenames must match the complete
        recorded list. Each extracted member must have its expected size, and total
        extracted data cannot exceed 256 MiB.

        We set the required executable permissions, check the complete extracted
        release and run both programs. The original parent directory identity must
        still agree before the completed directory is moved to its final name.
        Temporary files are removed when the operation finishes or raises.

        Args:
            consent: Explicit permission to download the selected software release.

        Returns:
            Final ElastixSetupStatus after selecting the verified managed programs.

        Raises:
            ValueError: Permission is absent, an existing destination is incompatible,
                archive contents differ or the installation parent changes.
            OSError: Downloading, extraction, synchronization or final installation fails.
        """

        if type(consent) is not bool:
            raise ValueError("Confirm the automatic-alignment download first.")
        if self.target.exists() or self.target.is_symlink():
            status = self._check(self.target)
            if status.state != "ready":
                raise ValueError(status.detail)
            return self._remember(status)
        if not consent:
            raise ValueError("Confirm the Elastix and Transformix download before installation.")
        identity = _safe_parent(self.target.parent)
        with tempfile.TemporaryDirectory(
            prefix=".elastix-install-", dir=self.target.parent
        ) as temporary:
            temporary = Path(temporary)
            archive = temporary / "release.zip"
            stage = temporary / "release"
            stage.mkdir(mode=0o700)
            download_pinned_file(
                self.pin["url"],
                archive,
                self.pin["sha256"],
                self.pin["size_bytes"],
                opener=self.opener,
            )
            with zipfile.ZipFile(archive) as zipped:
                members = zipped.infolist()
                expected = self.pin["files"]
                if len(members) != len(expected) or {x.filename for x in members} != set(expected):
                    raise ValueError(
                        "The Elastix archive contents differ from the verified release."
                    )
                total = 0
                for item in members:
                    path = PurePosixPath(item.filename)
                    mode = item.external_attr >> 16
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in item.filename
                        or item.is_dir()
                        or item.flag_bits & 1
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    ):
                        raise ValueError("The Elastix archive contains an unsupported file path.")
                    row = expected[item.filename]
                    if item.file_size != row["size_bytes"]:
                        raise ValueError("An Elastix archive member has changed size.")
                    destination = stage / item.filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(item) as source, destination.open("xb") as output:
                        count = 0
                        while block := source.read(1024 * 1024):
                            count += len(block)
                            total += len(block)
                            if count > row["size_bytes"] or total > 256 * 1024 * 1024:
                                raise ValueError(
                                    "The expanded Elastix archive exceeds its size limit."
                                )
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if self.platform == "linux-x86_64":
                        destination.chmod(0o700 if row["executable"] else 0o600)
            checked = self._check(stage)
            if checked.state != "ready":
                raise ValueError(checked.detail)
            if _safe_parent(self.target.parent) != identity:
                raise ValueError(
                    "The installation folder changed during the download. Select Install again after checking its location."
                )
            publish_no_replace(stage, self.target, self.platform)
        return self._remember(self._check(self.target))
