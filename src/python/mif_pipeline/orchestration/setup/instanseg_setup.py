"""

Inspects and installs the selected InstanSeg extension and pretrained cell-segmentation model.

The extension adds the QuPath commands and model-running interface. The model
supplies its trained network, processing scripts and model description. We
inspect and install these components separately: the extension goes into
QuPath's selected extensions folder, while the model goes into its versioned
application-data folder or is read from an explicitly supplied packaged location.

Both download descriptions and the model's exact file list are read from
config/software/instanseg.json. We compare each installed component with its
selected release before using it. The later native prerequisite check opens
the extension, model and PyTorch libraries together to confirm cell-segmentation
execution. This module reports the file and installation results used by that check.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
import stat
import sys
import tempfile
from typing import Literal
import zipfile
import zlib

from mif_pipeline.qupath_runtime import payload_lock
from mif_pipeline.qupath_runtime.class_origin_candidate import QuPathClassOriginResult, probe_qupath_class_origins
from mif_pipeline.qupath_runtime.prerequisite_probe import LocalInventoryCheck

from .setup_transfer import SetupTransferError
from .setup_transfer import download_pinned_file

_EXTENSION_CLASS = "qupath/ext/instanseg/ui/InstanSegExtension.class"
_MODEL_EXPANDED_LIMIT = 32 * 1024 * 1024


# ==============================================================================
#                     Extension and model installation
# ==============================================================================


class InstanSegSetupError(RuntimeError):
    """

    Reports an InstanSeg download, file or installation failure.

    Attributes:
        code: Failure category used by Software setup to identify the affected operation.
    """

    def __init__(self, code: str, message: str):
        """

        Associates an installation failure with its explanation.

        Args:
            code: Stable category describing the failed operation.
            message: Explanation retained in the exception text.
        """

        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class InstanSegComponentStatus:
    """

    Records the result of checking the extension or model files.

    Attributes:
        state: Installed when the selected files match their release, missing
            when absent, incompatible when conflicting or damaged, or not_checked
            when the required QuPath information could not be obtained.
        location: Selected file or model directory, or None when not yet resolved.
        detail: Explanation used beside the component's setup controls.
    """

    state: Literal["missing", "installed", "incompatible", "not_checked"]
    location: Path | None
    detail: str


@dataclass(frozen=True, slots=True)
class InstanSegSetupStatus:
    """

    Combines extension and model file checks with the information reported by QuPath.

    Attributes:
        extension: Result for the selected extension JAR.
        model: Result for the selected pretrained model directory.
        origins: QuPath version, extensions folder and loaded-library locations
            observed while checking this installation.
    """

    extension: InstanSegComponentStatus
    model: InstanSegComponentStatus
    origins: QuPathClassOriginResult


@dataclass(frozen=True, slots=True)
class InstanSegModelSelection:
    """

    Records which model directory was selected and the result of checking its files.

    Attributes:
        source: Bundled when an explicit packaged location was supplied, otherwise managed.
        directory: Selected directory containing the eight model files.
        inventory: Result of comparing those files with the selected model release.
    """

    source: Literal["bundled", "managed"]
    directory: Path
    inventory: LocalInventoryCheck


@dataclass(frozen=True, slots=True)
class InstanSegDownloadInfo:
    """

    Describes the specific extension or model download shown before confirmation.

    Attributes:
        component: Extension or model.
        version: Version selected for that component.
        approximate_size_bytes: Recorded archive or JAR download size.
        sources: Tuple containing the recorded download address.
    """

    component: Literal["extension", "model"]
    version: str
    approximate_size_bytes: int
    sources: tuple[str, ...]


def instanseg_download_info(component: str) -> InstanSegDownloadInfo:
    """

    Retrieves the selected component's version, download location and byte count.

    Args:
        component: Extension or model, corresponding to its separate setup action.

    Returns:
        InstanSegDownloadInfo used by the download-confirmation dialog.

    Raises:
        ValueError: The requested component is unsupported.
    """

    if component not in ("extension", "model"):
        raise ValueError("unsupported InstanSeg setup component")
    key = "instanseg" if component == "extension" else "model"
    url, size, _ = payload_lock.SOURCE_ARCHIVE_LOCK[key]
    version = (
        payload_lock.INSTANSEG_VERSION if component == "extension" else payload_lock.MODEL_VERSION
    )
    return InstanSegDownloadInfo(component, version, size, (url,))


def _absolute(path: Path) -> Path:
    """

    Checks the directory or filename supplied to an InstanSeg setup operation.

    Args:
        path: Path selected by the setup controller.

    Returns:
        Absolute path without parent-directory components or a NUL character.

    Raises:
        ValueError: The value has an invalid type or path representation.
    """

    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or "\0" in str(path)
    ):
        raise ValueError("Setup path must be absolute without traversal")
    return path


def default_instanseg_model_target(
    platform: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """

    Selects the versioned application-data directory used for a downloaded model.

    Windows uses LOCALAPPDATA, with AppData/Local under the home directory as
    the fallback. Linux uses XDG_DATA_HOME, with .local/share under the home
    directory as the fallback. We append mif-pipeline/instanseg and the selected
    model version. Analyses using that release can therefore reuse one installation.

    Args:
        platform: linux-x86_64 or win-x86_64.
        environ: Optional environment mapping; omission uses the current environment.
        home: Optional home directory used when the platform variable is absent.

    Returns:
        Absolute directory in which the selected model package is installed.

    Raises:
        ValueError: The platform or resulting application-data path is invalid.
    """

    if platform not in ("linux-x86_64", "win-x86_64"):
        raise ValueError("unsupported model setup platform")
    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    root = (
        Path(values.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        if platform == "win-x86_64"
        else Path(values.get("XDG_DATA_HOME") or user_home / ".local" / "share")
    )
    return _absolute(root) / "mif-pipeline" / "instanseg" / payload_lock.MODEL_VERSION


def resolve_default_instanseg_model(
    *,
    bundled_payload_root: Path | None,
    managed_payload_root: Path,
) -> InstanSegModelSelection:
    """

    Selects the explicitly supplied packaged model or the application-managed model directory.

    We append model/<selected model name> to the chosen root and check its
    complete file list. The result retains both the selected source and any
    missing or incompatible files, allowing the controller to present the
    appropriate installation or reinstallation action.

    Args:
        bundled_payload_root: Optional package directory explicitly containing
            model/<model name>/.
        managed_payload_root: Versioned application-data directory used when
            no packaged model root is supplied.

    Returns:
        InstanSegModelSelection with the chosen directory and its file-check result.

    Raises:
        ValueError: The chosen root is not an acceptable absolute path.
    """

    source = "bundled" if bundled_payload_root is not None else "managed"
    root = _absolute(
        bundled_payload_root if bundled_payload_root is not None else managed_payload_root
    )
    directory = root / "model" / payload_lock.MODEL_NAME
    return InstanSegModelSelection(
        source, directory, payload_lock.verify_instanseg_model_directory(directory)
    )


def _status(check: LocalInventoryCheck, location: Path) -> InstanSegComponentStatus:
    """

    Converts the shared file-check result into the extension/model setup state.

    A verified file set becomes installed. Missing and incompatible results
    retain their corresponding state and diagnostic so the interface can
    describe which files need attention.

    Args:
        check: Result from the shared installed-file comparison.
        location: Extension filename or model directory that was checked.

    Returns:
        InstanSegComponentStatus for the current setup selection.
    """

    state = {"verified": "installed", "missing": "missing", "incompatible": "incompatible"}[
        check.status
    ]
    detail = (
        "Pinned files installed; runtime verification is separate."
        if state == "installed"
        else check.diagnostic
    )
    return InstanSegComponentStatus(state, location, detail)


def _safe_directory(path: Path, *, create: bool) -> tuple[int, int]:
    """

    Checks the selected installation directory and optionally creates its missing parents.

    We visit the path from the filesystem root downward. Every existing component
    must be a directory without a symbolic link or Windows junction. With create
    enabled, missing directories are created with mode 0700. On systems exposing
    a user ID, the final directory must belong to the current user.

    Args:
        path: Selected absolute destination directory or its required parent.
        create: Whether missing directories may be created by this operation.

    Returns:
        Device and file IDs of the final directory, used to detect replacement
        while a download is in progress.

    Raises:
        InstanSegSetupError: A path is linked, absent when creation is disabled,
            not a directory or owned by another user.
        OSError: A required directory cannot be inspected or created.
    """

    for member in (*reversed(path.parents), path):
        if member.is_symlink() or getattr(member, "is_junction", lambda: False)():
            raise InstanSegSetupError("unsafe_target", "Setup directory contains a link")
        try:
            info = member.lstat()
        except FileNotFoundError:
            if not create:
                raise InstanSegSetupError("unsafe_target", "Setup directory is absent")
            member.mkdir(mode=0o700)
            info = member.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise InstanSegSetupError("unsafe_target", "Setup parent is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise InstanSegSetupError("unsafe_target", "Setup directory must belong to this user")
    return info.st_dev, info.st_ino


def _existing(path: Path) -> bool:
    """

    Checks whether a destination already contains a file, directory or link.

    Args:
        path: Proposed installation path.

    Returns:
        True for an existing filesystem entry, including a dangling symbolic
        link or Windows junction that must not be replaced by installation.
    """

    return path.exists() or path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _extract_model(archive: Path, destination: Path) -> None:
    """

    Expands and verifies the eight files belonging to the selected InstanSeg model.

    We require the archive's complete filename set and entry count to match
    instanseg.json. Each entry must be an unencrypted regular file at that
    flat filename. Content is written to new files in one-megabyte blocks and
    the total expanded bytes are limited to 32 MiB.

    After flushing and synchronizing the written files, we compare the complete
    model directory with the required names and SHA-256 values. The temporary
    installation remains unavailable to segmentation until this comparison passes.

    Args:
        archive: Downloaded model ZIP whose archive checksum was already checked.
        destination: New directory receiving the expanded model files.

    Raises:
        InstanSegSetupError: Entry names/types, encryption, expanded size,
            extraction or final file checks fail.
    """

    expected = {name for name, _ in payload_lock.INSTANSEG_MODEL_FILE_LOCK}
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) != len(expected) or {item.filename for item in members} != expected:
                raise ValueError("model archive member set differs")
            total = 0
            for item in members:
                mode = item.external_attr >> 16
                if (
                    item.is_dir()
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or item.flag_bits & 1
                    or item.file_size < 0
                ):
                    raise ValueError("model archive has unsupported members")
                with (
                    source.open(item) as incoming,
                    (destination / item.filename).open("xb") as outgoing,
                ):
                    while block := incoming.read(1024 * 1024):
                        total += len(block)
                        if total > _MODEL_EXPANDED_LIMIT:
                            raise ValueError("expanded model exceeds limit")
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise InstanSegSetupError(
            "model_archive_rejected", "Model archive cannot be safely extracted"
        ) from exc
    if payload_lock.verify_instanseg_model_directory(destination).status != "verified":
        raise InstanSegSetupError(
            "model_archive_rejected", "Expanded model differs from pinned member hashes"
        )


class InstanSegSetupService:
    """

    Provides separate inspection and installation operations for the InstanSeg extension and model.

    The controller supplies its selected QuPath program and any managed or
    packaged model locations. We inspect the active extension locations through
    QuPath before selecting the extension destination. Each installation requires
    its own confirmation and preserves any existing conflicting files for review.
    """

    def __init__(
        self,
        *,
        executable: Path,
        managed_payload_root: Path,
        extension_directory: Path | None = None,
        bundled_payload_root: Path | None = None,
        origin_probe: Callable[[Path], QuPathClassOriginResult] | None = None,
        opener=None,
    ):
        """

        Retains the selected QuPath program, model locations and native inspection function.

        Args:
            executable: Absolute QuPath executable used to inspect loaded extensions.
            managed_payload_root: Versioned application-data directory for model installation.
            extension_directory: Optional explicit extensions folder, which must
                agree with the directory later reported by QuPath.
            bundled_payload_root: Optional explicit package directory containing the model.
            origin_probe: Optional native inspection function returning QuPath's
                version and extension sources; omission uses the registered probe.
            opener: Optional HTTP opener passed to the verified download operation.

        Raises:
            ValueError: A supplied program or directory path is invalid.
        """

        self.executable = _absolute(executable)
        self.managed_payload_root = _absolute(managed_payload_root)
        self.extension_directory = (
            _absolute(extension_directory) if extension_directory is not None else None
        )
        self.bundled_payload_root = (
            _absolute(bundled_payload_root) if bundled_payload_root is not None else None
        )
        self._probe = origin_probe or probe_qupath_class_origins
        self._opener = opener

    def _model_selection(self) -> InstanSegModelSelection:
        """

        Resolves the model directory using this setup instance's packaged and managed locations.

        Returns:
            Selected model source, directory and complete file-check result.
        """

        return resolve_default_instanseg_model(
            bundled_payload_root=self.bundled_payload_root,
            managed_payload_root=self.managed_payload_root,
        )

    def _target_directory(self, origins: QuPathClassOriginResult) -> Path | None:
        """

        Resolves the extension destination from the supported QuPath installation.

        We require the expected QuPath version and a successful extension-folder
        observation. When an explicit destination was selected, it must agree with
        the folder reported by that QuPath process.

        Args:
            origins: Native observation of the selected QuPath program and extensions.

        Returns:
            Selected extensions directory, or None when QuPath has not configured one.

        Raises:
            InstanSegSetupError: QuPath could not report the required information
                or the configured and observed extension locations differ.
        """

        profile = origins.extensions
        if origins.qupath_version != payload_lock.QUPATH_VERSION or profile.status == "error":
            raise InstanSegSetupError(
                "observation_failed", "Supported QuPath profile was not observed"
            )
        if (
            self.extension_directory is not None
            and profile.directory is not None
            and self.extension_directory != profile.directory
        ):
            raise InstanSegSetupError(
                "profile_changed", "Selected target differs from QuPath's active profile"
            )
        return self.extension_directory or profile.directory

    def _extension(self, origins: QuPathClassOriginResult) -> InstanSegComponentStatus:
        """

        Finds the installed InstanSeg copies and compares them with the selected extension release.

        We combine QuPath's reported JAR list and loaded source with files currently
        in its extensions folder. Known InstanSeg locations are checked directly;
        other JARs are inspected for the extension's Java class. Each identified
        InstanSeg copy must have the selected release's exact size and checksum.

        Identical verified copies are accepted, preferring the copy actually loaded
        by QuPath. A differing copy produces an incompatible result identifying its
        location and the action needed before restarting. We do not remove extension
        files during inspection. The search is limited to 1024 candidate files of
        at most 512 MiB each.

        Args:
            origins: Native QuPath version, extension-folder contents and loaded
                InstanSeg source information.

        Returns:
            Extension state, selected JAR location and actionable setup explanation.
        """

        try:
            directory = self._target_directory(origins)
        except InstanSegSetupError as exc:
            return InstanSegComponentStatus("not_checked", None, str(exc))
        target = (
            directory / payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"][0].rsplit("/", 1)[1]
            if directory is not None
            else None
        )
        if origins.instanseg.status in ("error", "incompatible"):
            detail = origins.instanseg.diagnostic
            detail += "\nClose QuPath and check the named extension. For a conflicting file in the extensions folder, move it outside that folder or remove its extension-manager entry. For a file bundled inside QuPath, select or reinstall the supported QuPath installation. Restart the application and select Check again."
            return InstanSegComponentStatus("incompatible", target, detail)
        candidates = set(origins.extensions.jars)
        if origins.instanseg.code_source is not None:
            candidates.add(origins.instanseg.code_source)
        if target is not None and _existing(target):
            candidates.add(target)
        providers = []
        try:
            if directory is not None and _existing(directory):
                _safe_directory(directory, create=False)
                # Include JARs added directly to the extensions folder since
                # the native process reported its loaded and registered sources.
                candidates.update(directory.glob("*.jar"))
            if len(candidates) > 1024:
                raise ValueError("excessive extension inventory")
            for path in sorted(candidates):
                _absolute(path)
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 512 * 1024 * 1024
                ):
                    raise ValueError("unsafe extension file")
                if path == target or path == origins.instanseg.code_source:
                    providers.append(path)
                    continue
                with zipfile.ZipFile(path) as jar:
                    if _EXTENSION_CLASS in jar.namelist():
                        providers.append(path)
            if providers:
                checks = {
                    path: payload_lock.verify_instanseg_extension_file(path) for path in providers
                }
                conflicting = [path for path, check in checks.items() if check.status != "verified"]
                loaded = origins.instanseg.code_source
                if conflicting:
                    detail = "QuPath loaded: " + str(loaded) + "\n" if loaded else ""
                    detail += "Conflicting InstanSeg files:\n" + "\n".join(
                        str(path) for path in conflicting
                    )
                    detail += "\nClose QuPath and move these files outside its extensions folder, or remove their entry in QuPath’s extension manager. Keep the verified InstanSeg 0.1.7 copy. Restart the application and select Check again."
                    return InstanSegComponentStatus("incompatible", loaded or target, detail)
                # When all copies match the selected release, retain the
                # verified copy that QuPath actually reported as loaded.
                selected = loaded if loaded in checks else providers[0]
                return _status(checks[selected], selected)
        except (OSError, ValueError, zipfile.BadZipFile, InstanSegSetupError) as exc:
            return InstanSegComponentStatus(
                "incompatible",
                target,
                f"The QuPath extension files could not be checked: {exc}\nCheck read access to the extension folder and remove unsupported linked or damaged JAR files from that folder. Restart the application and select Check again.",
            )
        if directory is None:
            return InstanSegComponentStatus(
                "missing", None, "Choose an extension directory and configure it in QuPath."
            )
        return InstanSegComponentStatus(
            "missing", target, "Pinned InstanSeg extension is not installed."
        )

    def inspect(self) -> InstanSegSetupStatus:
        """

        Checks the selected extension and model using one fresh QuPath observation.

        Returns:
            InstanSegSetupStatus containing separate extension/model file results
            and the QuPath locations used to establish them.
        """

        origins = self._probe(self.executable)
        model = self._model_selection()
        return InstanSegSetupStatus(
            self._extension(origins), _status(model.inventory, model.directory), origins
        )

    def _download(self, component: str, target: Path) -> None:
        """

        Downloads one selected InstanSeg component and translates transfer failures for its setup controls.

        Args:
            component: Internal download key for the extension or model.
            target: New temporary filename receiving the verified bytes.

        Raises:
            InstanSegSetupError: Network, byte-count or checksum verification fails.
        """

        url, size, digest = payload_lock.SOURCE_ARCHIVE_LOCK[component]
        try:
            download_pinned_file(url, target, digest, size, opener=self._opener)
        except SetupTransferError as exc:
            raise InstanSegSetupError(exc.code, exc.detail) from exc
        except (OSError, ValueError) as exc:
            raise InstanSegSetupError(
                "download_rejected", "Pinned download was interrupted or failed verification"
            ) from exc

    @staticmethod
    def _publish(source: Path, target: Path) -> None:
        """

        Installs a verified temporary component at its final unused path.

        Args:
            source: Completed temporary JAR or model directory.
            target: Selected final filename or versioned model root.

        Raises:
            InstanSegSetupError: The platform cannot perform a no-overwrite rename,
                the destination exists or the filesystem move fails.
        """

        platform = {"linux": "linux-x86_64", "win32": "win-x86_64"}.get(sys.platform)
        try:
            publish_no_replace(source, target, platform)
        except (OSError, SetupTransferError) as exc:
            raise InstanSegSetupError(
                "publication_failed", "Verified files could not be published without replacement"
            ) from exc

    def install_extension(self, *, consent: bool) -> InstanSegComponentStatus:
        """

        Downloads and installs the extension after confirming its destination and current QuPath state.

        We reuse a verified existing extension and stop on a conflicting copy. A
        missing extension requires explicit download consent. Its bytes are staged
        beside the extensions directory so QuPath cannot discover and load a
        partially downloaded JAR while monitoring that directory.

        After download and file verification, we inspect QuPath again. Its selected
        extensions folder and file list must remain unchanged, with no InstanSeg
        installation having appeared meanwhile. We also compare the directory's
        device/file IDs before moving the verified JAR into its final unused name.

        Args:
            consent: Explicit approval of the extension download.

        Returns:
            File-check status for the installed extension. The subsequent native
            prerequisite check confirms its use with the model and tensor libraries.

        Raises:
            InstanSegSetupError: Consent or destination is missing, files conflict,
                the QuPath selection changes, or download/installation fails.
            TypeError: consent is not boolean.
        """

        if type(consent) is not bool:
            raise TypeError("Download consent must be bool")
        origins = self._probe(self.executable)
        directory = self._target_directory(origins)
        status = self._extension(origins)
        if status.state == "installed":
            return status
        if status.state == "incompatible":
            raise InstanSegSetupError("existing_conflict", status.detail)
        if directory is None:
            raise InstanSegSetupError("target_required", status.detail)
        if not consent:
            raise InstanSegSetupError(
                "consent_required", "Approve the pinned extension download before installation"
            )
        target = status.location
        assert target is not None
        try:
            identity = _safe_directory(directory, create=True)
            # Keep the unfinished download outside QuPath's extension search
            # directory; only the complete verified JAR is moved into that folder.
            with tempfile.TemporaryDirectory(
                prefix="mif-instanseg-", dir=directory.parent
            ) as temporary:
                staged = Path(temporary) / "extension.part"
                self._download("instanseg", staged)
                if payload_lock.verify_instanseg_extension_file(staged).status != "verified":
                    raise InstanSegSetupError(
                        "download_rejected", "Staged extension differs from pin"
                    )
                current = self._probe(self.executable)
                if (
                    self._target_directory(current) != directory
                    or current.extensions != origins.extensions
                ):
                    raise InstanSegSetupError(
                        "profile_changed", "QuPath extension profile changed during acquisition"
                    )
                if self._extension(current).state != "missing":
                    raise InstanSegSetupError(
                        "existing_conflict", "An extension appeared during acquisition"
                    )
                if _safe_directory(directory, create=False) != identity:
                    raise InstanSegSetupError(
                        "profile_changed", "Extension directory identity changed"
                    )
                self._publish(staged, target)
            return _status(payload_lock.verify_instanseg_extension_file(target), target)
        except OSError as exc:
            raise InstanSegSetupError(
                "publication_failed", "Extension setup could not safely publish files"
            ) from exc

    def install_model(self, *, consent: bool) -> InstanSegComponentStatus:
        """

        Downloads the model and installs its fully checked directory after confirmation.

        We reuse a verified model. An explicitly selected packaged model with missing
        or incompatible files requires package reinstallation. For a new managed
        model, its versioned root must remain unused and download consent must be true.

        We prepare the archive and expanded files in a temporary sibling directory,
        verify the eight model files, and check that the parent directory was not
        replaced during the operation. The completed model directory is then moved
        into its final location and checked once more before its status is returned.

        Args:
            consent: Explicit approval of this model download.

        Returns:
            Installed model's file-check status and directory.

        Raises:
            InstanSegSetupError: Consent is absent, an existing or packaged model
                requires correction, or download/extraction/installation fails.
            TypeError: consent is not boolean.
        """

        if type(consent) is not bool:
            raise TypeError("Download consent must be bool")
        selection = self._model_selection()
        status = _status(selection.inventory, selection.directory)
        if status.state == "installed":
            return status
        if self.bundled_payload_root is not None:
            raise InstanSegSetupError(
                "bundled_model_invalid", "The bundled model needs application reinstallation"
            )
        root = self.managed_payload_root
        if _existing(root):
            raise InstanSegSetupError(
                "existing_conflict", "Existing model payload will not be overwritten"
            )
        if not consent:
            raise InstanSegSetupError(
                "consent_required", "Approve the pinned model download before installation"
            )
        try:
            identity = _safe_directory(root.parent, create=True)
            with tempfile.TemporaryDirectory(prefix="mif-model-", dir=root.parent) as temporary:
                stage = Path(temporary)
                acquired = stage / "model.zip"
                self._download("model", acquired)
                payload = stage / "payload"
                model = payload / "model" / payload_lock.MODEL_NAME
                model.mkdir(parents=True, mode=0o700)
                _extract_model(acquired, model)
                if _safe_directory(root.parent, create=False) != identity:
                    raise InstanSegSetupError("publication_failed", "Model parent identity changed")
                self._publish(payload, root)
            return _status(
                payload_lock.verify_instanseg_model_directory(selection.directory),
                selection.directory,
            )
        except OSError as exc:
            raise InstanSegSetupError(
                "publication_failed", "Model setup could not safely publish files"
            ) from exc
