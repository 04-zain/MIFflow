"""

Prepares a separately selected QuPath launch configuration containing the supported extensions.

The command-line setup operation creates a new extensions directory with
verified InstanSeg and Warpy JARs, the bundled MIF extension and a description
of their selected QuPath program. Existing verified extensions can be copied
into that directory; downloads require confirmation.

We initialize QuPath's settings for the new directory and run the native
check to confirm both extensions were loaded from their expected files.
The completed launch description is returned to the calling selection
operation. Failed initialization retains the prior software selection.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import os
from pathlib import Path
from mif_pipeline.pipeline_core.atomic_publication import PublicationError, publish_no_replace
import re
import secrets
import shutil
import stat
import tempfile

from mif_pipeline.qupath_runtime import payload_lock
from mif_pipeline.qupath_runtime.class_origin_candidate import QuPathClassOriginResult, probe_qupath_class_origins
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding
from mif_pipeline.qupath_runtime.managed_profile import ManagedQuPathProfileError, load_managed_qupath_profile, managed_profile_manifest

from mif_pipeline.qupath_runtime.native_image_reader import (
    BundledNativeReaderError, install_native_reader, validate_native_reader,
)
from mif_pipeline.qupath_runtime.prerequisite_probe import _path_snapshot

from .setup_transfer import SetupTransferError
from .setup_transfer import download_pinned_file

# ==============================================================================
#                              Qupath profile setup
# ==============================================================================


class ManagedQuPathSetupError(RuntimeError):
    """

    Reports a failed step while preparing a separate QuPath extension directory.

    Attributes:
        code: Setup failure category.
        detail: Explanation used by the calling command or controller.
    """

    def __init__(self, code: str, detail: str) -> None:
        """

        Records the failed setup operation and its explanation.

        Args:
            code: Failure category.
            detail: Complete setup message retained in the exception.
        """

        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _safe_profiles_root(path: Path) -> tuple[int, int]:
    """

    Checks the parent directory in which new QuPath extension selections can be created.

    The path must be absolute and every ancestor an existing directory without
    symbolic links or junctions. Where user IDs are available, the final parent
    must belong to the current user and must not permit group/other writes.

    Args:
        path: Selected existing parent directory for the new extension selections.

    Returns:
        Device and file IDs of that directory for comparison after installation.

    Raises:
        ManagedQuPathSetupError: Path, directory type, access permissions or
            filesystem inspection is invalid.
    """

    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or "\0" in str(path)
    ):
        raise ManagedQuPathSetupError("unsafe_target", "Choose an existing local setup folder")
    try:
        for member in (*reversed(path.parents), path):
            if member.is_symlink() or getattr(member, "is_junction", lambda: False)():
                raise ManagedQuPathSetupError("unsafe_target", "Setup folder contains a link")
            if not stat.S_ISDIR(member.lstat().st_mode):
                raise ManagedQuPathSetupError("unsafe_target", "Setup folder is not a directory")
        details = path.lstat()
        if hasattr(os, "getuid") and (
            details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise ManagedQuPathSetupError(
                "unsafe_target", "Setup folder must be owned by you and not shared for writing"
            )
        return details.st_dev, details.st_ino
    except ManagedQuPathSetupError:
        raise
    except (OSError, ValueError) as exc:
        raise ManagedQuPathSetupError(
            "unsafe_target", "Setup folder cannot be checked safely"
        ) from exc


def _require_observed_profile(result: QuPathClassOriginResult) -> None:
    # The native check has compared the selected directory and loaded file
    # contents. Both extension results must confirm that prepared selection.
    """

    Requires the native loading check to identify the selected InstanSeg and Warpy files.

    The selected-launch probe has already compared the configured folder,
    loaded class sources and extension contents. We require its successful
    candidate result for both extensions before returning the new selection.

    Args:
        result: QuPathClassOriginResult from the prepared launch configuration.

    Raises:
        ManagedQuPathSetupError: Both selected extensions were not confirmed as loaded.
    """

    if (
        not isinstance(result, QuPathClassOriginResult)
        or result.instanseg.status != "candidate"
        or result.warpy.status != "candidate"
    ):
        raise ManagedQuPathSetupError(
            "probe_rejected",
            "QuPath did not load the exact files from this setup folder",
        )


def _existing_extension_bytes(source: Path, pin: tuple[str, int, str]) -> bytes:
    """

    Reads an installed extension for reuse after checking its release checksum.

    We check the selected path and opened file, then read at most one byte
    beyond the pinned size. The returned immutable buffer must match that size
    and checksum; those exact checked bytes are written into the new directory.

    Args:
        source: Installed extension JAR selected for reuse.
        pin: Expected download URL, byte count and SHA-256 checksum.

    Returns:
        Verified JAR bytes to write into the new extensions directory.

    Raises:
        ManagedQuPathSetupError: The file is unavailable, differs from the
            selected extension release or changes while being read.
    """

    try:
        if not isinstance(source, Path) or not source.is_absolute() or ".." in source.parts:
            raise ValueError("extension path must be absolute without traversal")
        snapshot = _path_snapshot(source)
        if not stat.S_ISREG(snapshot[-1][1][2]):
            raise ValueError("extension source must be a regular file")
        _, size, digest = pin
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != size:
                raise ValueError("opened extension must be a regular file of the pinned size")
            content = stream.read(size + 1)
        if len(content) != size or sha256(content).hexdigest() != digest:
            raise ManagedQuPathSetupError(
                "existing_extension_rejected",
                f"The installed extension {source.name} differs from the selected release",
            )
        return content
    except (OSError, ValueError, TypeError) as exc:
        raise ManagedQuPathSetupError(
            "existing_extension_rejected",
            "The installed extension cannot be reused safely",
        ) from exc


class ManagedQuPathSetupService:
    """

    Creates and tests a new QuPath extension selection without replacing its predecessor.

    The returned launch description can be accepted by the caller after its
    native check passes. Construction and download do not edit image projects
    or train segmentation/registration models.
    """

    def __init__(
        self,
        *,
        executable: Path,
        profiles_root: Path,
        platform: str,
        opener: object | None = None,
        seed: Callable[[QuPathProfileLaunchBinding], None] | None = None,
        probe: Callable[..., QuPathClassOriginResult] | None = None,
    ) -> None:
        """

        Retains the selected program, installation parent and setup/check functions.

        Args:
            executable: Existing absolute regular QuPath executable file.
            profiles_root: Checked existing parent for new extension configurations.
            platform: linux-x86_64 or win-x86_64.
            opener: Optional HTTP opener for the selected extension downloads.
            seed: Optional function initializing QuPath preferences for the new
                launch description; omission uses the registered native initializer.
            probe: Optional native class-source inspection function.

        Raises:
            ManagedQuPathSetupError: Program, platform or installation parent is invalid.
        """

        if (
            not isinstance(executable, Path)
            or not executable.is_absolute()
            or ".." in executable.parts
            or "\0" in str(executable)
        ):
            raise ManagedQuPathSetupError("invalid_executable", "Choose an exact QuPath executable")
        try:
            if not stat.S_ISREG(executable.lstat().st_mode):
                raise ManagedQuPathSetupError(
                    "invalid_executable", "QuPath executable is not a regular file"
                )
        except FileNotFoundError as exc:
            raise ManagedQuPathSetupError(
                "invalid_executable", "QuPath executable is missing"
            ) from exc
        if platform not in {"linux-x86_64", "win-x86_64"}:
            raise ManagedQuPathSetupError(
                "unsupported_platform", "This setup platform is unsupported"
            )
        _safe_profiles_root(profiles_root)
        self.executable = executable
        self.profiles_root = profiles_root
        self.platform = platform
        self._opener = opener
        self._seed = seed
        self._probe = probe or probe_qupath_class_origins

    def install(
        self, *, consent: bool, existing_instanseg: Path | None = None,
        existing_warpy: Path | None = None,
    ) -> QuPathProfileLaunchBinding:
        """

        Creates the selected extension files, initializes their QuPath settings and verifies both loaded sources.

        We generate a new directory under the checked parent and prepare all
        three extensions in a temporary sibling directory. Existing verified
        InstanSeg and Warpy files are reused when supplied. Any downloads require
        confirmation; the MIF extension comes from the installed application.

        After confirming that the parent directory has not changed, we move the
        completed directory into its unused final name. We reopen its launch
        description, initialize QuPath preferences and run the native extension
        check. Only a successful result for both extensions is returned to the
        caller for selection. A failed unpublished directory is removed; a directory
        already installed remains available for inspection when initialization fails.

        Args:
            consent: Approval to download dependencies that were not supplied locally.
            existing_instanseg: Optional installed InstanSeg JAR that can be checked
                and reused instead of downloading the same extension again.
            existing_warpy: Optional installed Warpy JAR, checked in the same way.
                When both files are supplied, installation needs no downloads.

        Returns:
            QuPathProfileLaunchBinding for the completed and observed extension configuration.

        Raises:
            ManagedQuPathSetupError: Consent, file reuse, download, installation,
                preference initialization or loaded-extension verification fails.
        """

        validate_native_reader()
        if consent is not True and (existing_instanseg is None or existing_warpy is None):
            raise ManagedQuPathSetupError(
                "consent_required", "Approve the pinned QuPath extension downloads first"
            )
        reused = {}
        for source, pin in (
            (existing_instanseg, payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"]),
            (existing_warpy, payload_lock.WARPY_SOURCE_ARCHIVE),
        ):
            if source is not None:
                try:
                    reused[pin[0]] = _existing_extension_bytes(source, pin)
                except ManagedQuPathSetupError:
                    if not consent:
                        raise
        root_identity = _safe_profiles_root(self.profiles_root)
        generation = secrets.token_hex(16)
        if re.fullmatch(r"[0-9a-f]{32}", generation) is None:
            raise ManagedQuPathSetupError(
                "generation_invalid", "Cannot create a safe setup generation"
            )
        final = self.profiles_root / generation
        if final.exists() or final.is_symlink() or getattr(final, "is_junction", lambda: False)():
            raise ManagedQuPathSetupError(
                "existing_conflict", "This setup generation already exists"
            )
        try:
            manifest = managed_profile_manifest(executable=self.executable, profile_root=final)
        except ManagedQuPathProfileError as exc:
            raise ManagedQuPathSetupError(
                "profile_rejected",
                "The pinned QuPath setup record is invalid",
            ) from exc
        staging: Path | None = None
        published = False
        try:
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.profiles_root))
            extensions = staging / "extensions"
            extensions.mkdir(mode=0o700)
            for url, size, digest in (
                payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"],
                payload_lock.WARPY_SOURCE_ARCHIVE,
            ):
                name = url.rsplit("/", 1)[-1]
                if url in reused:
                    with (extensions / name).open("xb") as output:
                        output.write(reused[url])
                        output.flush()
                        os.fsync(output.fileno())
                else:
                    download_pinned_file(
                        url,
                        extensions / name,
                        digest,
                        size,
                        opener=self._opener,
                    )
            install_native_reader(extensions)
            with (staging / "manifest.json").open("xb") as output:
                output.write(manifest)
                output.flush()
                os.fsync(output.fileno())
            if _safe_profiles_root(self.profiles_root) != root_identity:
                raise ManagedQuPathSetupError(
                    "unsafe_target", "Setup folder changed during installation"
                )
            try:
                publish_no_replace(staging, final, self.platform)
            except (OSError, SetupTransferError) as exc:
                raise ManagedQuPathSetupError(
                    "publication_failed",
                    "The new QuPath setup could not be published safely",
                ) from exc
            published = True
            binding = load_managed_qupath_profile(
                final / "manifest.json",
                expected_executable=self.executable,
            )
            try:
                if self._seed is None:
                    from mif_pipeline.qupath_runtime.profile_setup import seed_managed_qupath_profile

                    seed_managed_qupath_profile(binding)
                else:
                    self._seed(binding)
            except Exception as exc:
                raise ManagedQuPathSetupError(
                    "seed_failed",
                    "QuPath could not prepare the new setup folder; the previous selection is unchanged",
                ) from exc
            try:
                result = self._probe(self.executable, launch_binding=binding)
            except Exception as exc:
                raise ManagedQuPathSetupError(
                    "probe_failed",
                    "QuPath could not check the new setup; the previous selection is unchanged",
                ) from exc
            _require_observed_profile(result)
            return binding
        except (ManagedQuPathSetupError, BundledNativeReaderError):
            raise
        except (SetupTransferError, PublicationError) as exc:
            raise ManagedQuPathSetupError(exc.code, exc.detail) from exc
        except ManagedQuPathProfileError as exc:
            raise ManagedQuPathSetupError(
                "profile_rejected",
                "The installed QuPath profile did not match its pinned files",
            ) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise ManagedQuPathSetupError(
                "setup_failed",
                "QuPath setup could not finish; the previous selection is unchanged",
            ) from exc
        finally:
            if (
                not published
                and staging is not None
                and staging.is_dir()
                and not staging.is_symlink()
            ):
                shutil.rmtree(staging)


__all__ = ["ManagedQuPathSetupError", "ManagedQuPathSetupService"]
