"""

Inspects and installs the selected Warpy extension in QuPath's configured extensions folder.

We obtain the extensions folder and loaded Warpy source from the selected
QuPath program. Identified Warpy JARs are checked against the release in
config/software/warpy.json. A matching loaded copy is ready; a matching
installed copy that was not loaded requires QuPath to be restarted and checked.

Installation downloads the Warpy JAR only after confirmation and stages it
outside the watched extensions folder. Elastix and Transformix have their
own installation operation, while InstanSeg's extension and model are handled
by the cell-segmentation setup operations.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
from pathlib import Path
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
import os, tempfile, zipfile
from mif_pipeline.qupath_runtime import payload_lock
from mif_pipeline.qupath_runtime.class_origin_candidate import probe_qupath_class_origins
from mif_pipeline.qupath_runtime.prerequisite_probe import ProbeFileExpectation, _file_check
from .setup_transfer import download_pinned_file

# ==============================================================================
#                                  Warpy setup
# ==============================================================================


@dataclass(frozen=True, slots=True)
class WarpySetupStatus:
    """

    Reports the Warpy file and loading result for the selected QuPath installation.

    Attributes:
        state: Ready when the verified extension was loaded, installed when its
            verified file is present, missing when absent, incompatible for
            conflicting files, or not_checked when QuPath could not report the result.
        location: Loaded or expected Warpy JAR location, or None without a configured folder.
        detail: Explanation and corrective action for the current setup state.
    """

    state: str
    location: Path | None
    detail: str


def _directory(path):
    """

    Checks QuPath's extension directory and records its filesystem identity before installation.

    Args:
        path: Absolute configured extensions folder.

    Returns:
        Device and file IDs used to detect directory replacement during download.

    Raises:
        ValueError: The location is relative, contains '..', traverses a symbolic
            link or junction, or is not an existing directory.
    """

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Choose an absolute QuPath extensions folder.")
    for item in (*reversed(path.parents), path):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError(
                "The QuPath extension path contains a link. Select the installation folder directly."
            )
    if not path.is_dir():
        raise ValueError(
            "Open QuPath and configure its extensions folder, then select Check again."
        )
    info = path.stat()
    return info.st_dev, info.st_ino


class WarpySetupService:
    """

    Provides Warpy file inspection and a separately confirmed extension installation.
    """

    def __init__(self, *, executable, launch_binding=None, origin_probe=None, opener=None):
        """

        Retains the selected QuPath executable and the native inspection used to locate its extension.

        Args:
            executable: Absolute QuPath program path.
            launch_binding: Optional managed launch settings selecting that program's
                extension locations.
            origin_probe: Optional callable returning QuPath's version, extensions
                folder and loaded class sources.
            opener: Optional HTTP opener passed to the verified JAR download.

        Raises:
            ValueError: The selected program path is relative or contains '..'.
        """

        self.executable = Path(executable)
        self.launch_binding = launch_binding
        self.opener = opener
        if not self.executable.is_absolute() or ".." in self.executable.parts:
            raise ValueError("Select the installed QuPath executable.")
        self.probe = origin_probe or (
            lambda path: probe_qupath_class_origins(path, launch_binding=launch_binding)
        )

    def _inspect(self, observed):
        """

        Checks every identified Warpy copy and determines whether the selected version was loaded.

        We combine the native extension list, loaded Warpy source and the current
        direct JAR files in the configured folder. Other JARs are inspected for
        WarpyExtension.class. Each identified copy must match the selected release's
        complete checksum. The search allows at most 1024 candidate JARs, each no
        larger than 512 MiB and without a symbolic link.

        A differing Warpy file produces an incompatible result listing the paths
        to move or remove before restarting. Identical verified copies can coexist;
        the loaded copy establishes readiness when QuPath reported one. Otherwise
        verified files are reported as installed and await a restart/loading check.

        Args:
            observed: Native observation of the selected QuPath version, extension
                directory, listed JARs and loaded Warpy source.

        Returns:
            WarpySetupStatus with the relevant location and next setup action.
        """

        pin = payload_lock.WARPY_SOURCE_ARCHIVE
        name = pin[0].rsplit("/", 1)[-1]
        directory = observed.extensions.directory
        loaded = observed.warpy
        target = directory / name if directory is not None else None
        if observed.qupath_version != "0.7.0" or observed.extensions.status == "error":
            return WarpySetupStatus(
                "not_checked",
                target,
                "QuPath could not report its extension folder. Check the selected QuPath program, close other QuPath windows and select Check again.",
            )
        candidates = set(observed.extensions.jars)
        if loaded.code_source is not None:
            candidates.add(loaded.code_source)
        providers = []
        try:
            if directory is not None:
                _directory(directory)
                candidates.update(directory.glob("*.jar"))
                if target.exists():
                    candidates.add(target)
            if len(candidates) > 1024:
                raise ValueError("Too many extension files were found.")
            for path in sorted(candidates):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 512 * 1024 * 1024
                ):
                    raise ValueError("The extension file cannot be read safely: " + str(path))
                if path in (loaded.code_source, target):
                    providers.append(path)
                    continue
                with zipfile.ZipFile(path) as jar:
                    if "qupath/ext/warpy/WarpyExtension.class" in jar.namelist():
                        providers.append(path)
            conflicts = [
                p
                for p in providers
                if _file_check(ProbeFileExpectation(p, pin[2])).status != "verified"
            ]
            if conflicts:
                return WarpySetupStatus(
                    "incompatible",
                    loaded.code_source or target,
                    "Conflicting Warpy files:\n"
                    + "\n".join(str(p) for p in conflicts)
                    + "\nClose QuPath and move these files outside its extensions folder, or remove the corresponding extension-manager entry. Keep Warpy 0.5.0, restart the application and select Check again.",
                )
            if loaded.status == "candidate" and loaded.code_source in providers:
                return WarpySetupStatus(
                    "ready", loaded.code_source, "QuPath loaded the verified Warpy 0.5.0 extension."
                )
            if providers:
                return WarpySetupStatus(
                    "installed",
                    providers[0],
                    "Warpy 0.5.0 is installed. Restart the application and select Check again to confirm QuPath loads it.",
                )
            if loaded.status in ("error", "incompatible"):
                return WarpySetupStatus(
                    "not_checked",
                    target,
                    "Warpy could not be loaded. Check the QuPath log for its extension error, then restart and select Check again.\n"
                    + loaded.diagnostic,
                )
            return WarpySetupStatus(
                "missing",
                target,
                (
                    "Install Warpy 0.5.0 to align paired images."
                    if target is not None
                    else "Open QuPath and configure its user directory in Preferences, then select Check again. Warpy will be installed in that directory’s extensions folder."
                ),
            )
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            return WarpySetupStatus(
                "incompatible",
                target,
                "The extension folder could not be checked: "
                + str(error)
                + "\nCheck file access and restart QuPath before selecting Check again.",
            )

    def inspect(self):
        """

        Checks Warpy using a fresh observation of the selected QuPath installation.

        Returns:
            Current Warpy state, JAR location and setup explanation.
        """

        return self._inspect(self.probe(self.executable))

    def install(self, *, consent):
        """

        Downloads the missing Warpy JAR and installs it without replacing an existing extension.

        We first inspect the current installation and reuse ready or installed
        files. A missing extension with a configured target requires explicit
        download consent. The verified JAR is prepared in a temporary directory
        beside QuPath's extensions folder.

        Before the final move, we repeat the native observation and require the
        same extensions folder and filesystem identity, with Warpy still absent.
        A no-overwrite rename installs the file, and a fresh inspection reports
        whether QuPath has loaded it or still requires a restart.

        Args:
            consent: Explicit confirmation of the Warpy download.

        Returns:
            WarpySetupStatus after installing or reusing the selected extension.

        Raises:
            ValueError: Consent, extension location or current file state is invalid
                or changes during the download.
            OSError or SetupTransferError: Downloading or moving the verified JAR fails.
        """

        if type(consent) is not bool:
            raise ValueError("Confirm the Warpy download before installation.")
        before = self.probe(self.executable)
        status = self._inspect(before)
        if status.state in ("ready", "installed"):
            return status
        if status.state != "missing" or status.location is None:
            raise ValueError(status.detail)
        if not consent:
            raise ValueError("Confirm the Warpy download before installation.")
        target = status.location
        directory = target.parent
        identity = _directory(directory)
        pin = payload_lock.WARPY_SOURCE_ARCHIVE
        with tempfile.TemporaryDirectory(prefix="mif-warpy-", dir=directory.parent) as temporary:
            stage = Path(temporary) / target.name
            download_pinned_file(pin[0], stage, pin[2], pin[1], opener=self.opener)
            current = self.probe(self.executable)
            if current.extensions != before.extensions or _directory(directory) != identity:
                raise ValueError(
                    "QuPath’s extension folder changed during installation. Select Check again before retrying."
                )
            if self._inspect(current).state != "missing":
                raise ValueError(
                    "A Warpy installation appeared during the download. Select Check again to inspect it."
                )
            publish_no_replace(stage, target, "win-x86_64" if os.name == "nt" else "linux-x86_64")
        return self.inspect()
