"""

Keeps annotation-transfer files associated with the directories and open files used by the current operation.

During an annotation write, we retain a filesystem identity for the transfer
directory and its lock file. We compare these values before and after the
write so another file placed at the same path cannot be mistaken for the
object already opened by the operation.

POSIX identifies files by device and inode. Windows identifies them by volume
serial number and a 128-bit file ID obtained from the open file handle.
The saved record names its platform explicitly, allowing both platforms to
represent their identities without converting a large identifier through
an inexact floating-point JSON value.

This module also coordinates the Windows file operations used for temporary
writes, replacement and cleanup. The POSIX implementation performs those
operations through directory-relative descriptors in registration/executor.py.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Iterator, Protocol
import uuid

from mif_pipeline.pipeline_core.models import ROIAnalysisError

# ==============================================================================
#                                  Secure store
# ==============================================================================


@dataclass(frozen=True, slots=True)
class PosixFileIdentity:
    """

    Retains the device and inode of an opened POSIX transfer file or directory.

    We obtain these numbers from filesystem metadata and compare both whenever
    an operation needs to establish that a path still refers to its opened file.
    The current serialized transfer record includes its explicit POSIX format.

    Attributes:
        device: Device identifier returned by the operating system.
        inode: File identifier within that device.
    """

    device: int
    inode: int

    def __post_init__(self) -> None:
        """

        Checks that both filesystem identifiers are nonnegative integers.

        Raises:
            ROIAnalysisError: Either field is negative, boolean or not an integer.
        """

        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.device, self.inode)
        ):
            raise ROIAnalysisError("file identity is invalid")

    def to_mapping(self) -> dict[str, int]:
        """

        Collects the two numerical identity fields used in transfer-checksum construction.

        Returns:
            Dictionary containing device and inode. The current saved file record
            is produced by to_tagged_mapping(), which also includes the format name.
        """

        return {"device": self.device, "inode": self.inode}

    def to_tagged_mapping(self) -> dict[str, object]:
        """

        Encodes the POSIX file identity with the format required by current transfer records.

        The numerical values can pass through Java JSON decoders that represent
        numbers as doubles. We require each identifier to be at most 2**53 - 1,
        the largest interval over which every integer is represented exactly.

        Returns:
            Dictionary containing posix-file-identity/v1, device and inode.

        Raises:
            ROIAnalysisError: An identifier exceeds the supported exact-integer range.
        """

        if self.device > 2**53 - 1 or self.inode > 2**53 - 1:
            raise ROIAnalysisError("POSIX file identity exceeds the exactly represented JSON integer range")
        return {
            "schema_version": "posix-file-identity/v1",
            "device": self.device,
            "inode": self.inode,
        }


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    """

    Retains the volume and file identifiers obtained from an open Windows file handle.

    The volume serial number can occupy 64 bits and the file identifier occupies
    128 bits. We save them as fixed-width hexadecimal text so their complete
    values survive JSON exchange with QuPath.

    Attributes:
        volume_serial_number: Unsigned 64-bit serial number of the containing volume.
        file_id: Thirty-two lowercase hexadecimal characters identifying the file.
    """

    volume_serial_number: int
    file_id: str

    def __post_init__(self) -> None:
        """

        Checks the full Windows volume and file identifiers before they are recorded.

        Raises:
            ROIAnalysisError: The volume is outside the unsigned 64-bit range or
                the file ID is not a 32-character lowercase hexadecimal string.
        """

        if (
            not isinstance(self.volume_serial_number, int)
            or isinstance(self.volume_serial_number, bool)
            or not 0 <= self.volume_serial_number < 2**64
            or not isinstance(self.file_id, str)
            or len(self.file_id) != 32
            or any(character not in "0123456789abcdef" for character in self.file_id)
        ):
            raise ROIAnalysisError("file identity is invalid")

    def to_mapping(self) -> dict[str, object]:
        """

        Encodes the complete Windows identity using its explicit format and hexadecimal fields.

        Returns:
            Dictionary containing windows-file-identity/v1, a 16-character volume
            serial number and the 32-character file ID.
        """

        return {
            "schema_version": "windows-file-identity/v1",
            # JSON numbers cannot carry every uint64 exactly through all Gson
            # Map decoders. Fixed-width hex keeps the FILE_ID_INFO value exact.
            "volume_serial_number": f"{self.volume_serial_number:016x}",
            "file_id": self.file_id,
        }


FileIdentity = PosixFileIdentity | WindowsFileIdentity


def file_identity_from_mapping(value: object) -> FileIdentity:
    """

    Reads the platform-specific file identity used by a current annotation-transfer operation.

    We require an explicit format and its complete field set. POSIX records
    provide the two exact numerical identifiers; Windows records provide fixed-
    width hexadecimal identifiers. Each path reconstructs the corresponding
    checked identity used by directory, lock and completed-write comparisons.

    Args:
        value: Decoded file identity from the current transfer record.

    Returns:
        PosixFileIdentity or WindowsFileIdentity matching the declared platform.

    Raises:
        ROIAnalysisError: The format, required fields, numerical interval or
            hexadecimal representation is invalid.
    """

    if not isinstance(value, dict):
        raise ROIAnalysisError("file identity is invalid")
    if (
        set(value) == {"schema_version", "device", "inode"}
        and value.get("schema_version") == "posix-file-identity/v1"
    ):
        try:
            identity = PosixFileIdentity(value["device"], value["inode"])
        except (TypeError, ROIAnalysisError) as exc:
            raise ROIAnalysisError("file identity is invalid") from exc
        if identity.device > 2**53 - 1 or identity.inode > 2**53 - 1:
            raise ROIAnalysisError("POSIX file identity exceeds the exactly represented JSON integer range")
        return identity
    fields = {"schema_version", "volume_serial_number", "file_id"}
    if set(value) == fields and value.get("schema_version") == "windows-file-identity/v1":
        try:
            volume = value["volume_serial_number"]
            if (
                not isinstance(volume, str)
                or len(volume) != 16
                or any(character not in "0123456789abcdef" for character in volume)
            ):
                raise ROIAnalysisError("file identity is invalid")
            return WindowsFileIdentity(int(volume, 16), value["file_id"])
        except (TypeError, ROIAnalysisError) as exc:
            raise ROIAnalysisError("file identity is invalid") from exc
    raise ROIAnalysisError("file identity is invalid")


class Win32SecureStorePrimitives(Protocol):
    """Native operations required for an end-to-end Windows authority path.

    Implementations must either open children relative to the supplied
    directory handle (for example with ``NtCreateFile.RootDirectory``) or pin
    every ancestor with no-delete-share handles while resolving a child path.
    Identity must come from the open handle as volume serial plus a 128-bit
    file ID. Reparse status must also come from the open handle.
    """

    def open_directory(self, path: Path, *, create: bool) -> object: ...
    def open_child(
        self,
        directory: object,
        name: str,
        *,
        create: bool,
        exclusive: bool,
        rename_capable: bool = False,
    ) -> tuple[object, bool]: ...
    def identity(self, handle: object) -> WindowsFileIdentity: ...
    def is_directory(self, handle: object) -> bool: ...
    def is_regular_file(self, handle: object) -> bool: ...
    def is_reparse_point(self, handle: object) -> bool: ...
    def child_identity(self, directory: object, name: str) -> WindowsFileIdentity | None: ...
    def close(self, handle: object) -> None: ...
    def lock_file_ex(self, handle: object) -> None: ...
    def unlock_file_ex(self, handle: object) -> None: ...
    def flush(self, handle: object) -> None: ...
    def write_all(self, handle: object, value: bytes) -> None: ...
    def publish_new(
        self,
        directory: object,
        source_handle: object,
        source: str,
        destination: str,
    ) -> bool: ...
    def replace_child(
        self,
        directory: object,
        source_handle: object,
        source: str,
        destination: str,
    ) -> None: ...
    def unlink_child(self, directory: object, name: str) -> None: ...
    def discard_open_child(self, directory: object, handle: object, name: str) -> None: ...
    def flush_directory(self, directory: object) -> None: ...
    def read_all(self, handle: object) -> bytes: ...
    def list_children(self, directory: object) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class WindowsDirectory:
    handle: object
    identity: WindowsFileIdentity
    path: Path


class WindowsSecureStoreBackend:
    """Security policy over injected handle-relative Win32 primitives."""

    def __init__(self, primitives: Win32SecureStorePrimitives) -> None:
        self._primitives = primitives

    def _finish_temporary(
        self,
        directory: WindowsDirectory,
        handle: object,
        temporary: str,
        *,
        discard: bool,
    ) -> None:
        """Close a temp without replacing the operation's primary failure."""
        primary_error = sys.exc_info()[1]
        cleanup_error: Exception | None = None
        if discard:
            try:
                self._primitives.discard_open_child(directory.handle, handle, temporary)
            except Exception as exc:
                cleanup_error = exc
        try:
            self._primitives.close(handle)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error.add_note(f"closing temporary also failed: {exc!r}")
        if cleanup_error is None:
            return
        if primary_error is not None:
            primary_error.add_note(f"secure temporary cleanup also failed: {cleanup_error!r}")
            return
        raise cleanup_error

    @contextmanager
    def open_directory(
        self,
        path: Path,
        *,
        create: bool = False,
        expected_identity: WindowsFileIdentity | None = None,
    ) -> Iterator[WindowsDirectory]:
        handle = self._primitives.open_directory(Path(path), create=create)
        try:
            self._validate_directory_handle(handle)
            identity = self._primitives.identity(handle)
            if expected_identity is not None and identity != expected_identity:
                raise ROIAnalysisError("Warpy transfer store directory identity changed")
            directory = WindowsDirectory(handle=handle, identity=identity, path=Path(path))
            try:
                yield directory
            except BaseException as body_failure:
                try:
                    self._validate_pinned_directory(directory)
                except BaseException as authority_failure:
                    raise authority_failure from body_failure
                raise
            else:
                self._validate_pinned_directory(directory)
        finally:
            self._primitives.close(handle)

    @contextmanager
    def lock_regular_child(
        self,
        directory: WindowsDirectory,
        name: str,
        *,
        expected_identity: WindowsFileIdentity,
    ) -> Iterator[None]:
        handle, _created = self._primitives.open_child(
            directory.handle,
            name,
            create=False,
            exclusive=False,
            rename_capable=False,
        )
        locked = False
        try:
            self._validate_regular_handle(handle)
            if self._primitives.identity(handle) != expected_identity:
                raise ROIAnalysisError("Warpy transfer lock identity changed")
            self._primitives.lock_file_ex(handle)
            locked = True
            self._verify_named_child(directory, name, handle, expected_identity)
            try:
                yield
            except BaseException as body_failure:
                try:
                    self._validate_after_locked_body(directory, name, handle, expected_identity)
                except BaseException as authority_failure:
                    raise authority_failure from body_failure
                raise
            else:
                self._validate_after_locked_body(directory, name, handle, expected_identity)
        finally:
            try:
                if locked:
                    self._primitives.unlock_file_ex(handle)
            finally:
                self._primitives.close(handle)

    def _validate_pinned_directory(self, directory: WindowsDirectory) -> None:
        self._validate_directory_handle(directory.handle)
        if self._primitives.identity(directory.handle) != directory.identity:
            raise ROIAnalysisError("Warpy transfer store directory identity changed")

    def _validate_after_locked_body(
        self,
        directory: WindowsDirectory,
        name: str,
        handle: object,
        expected_identity: WindowsFileIdentity,
    ) -> None:
        self._primitives.flush(handle)
        self._verify_named_child(directory, name, handle, expected_identity)

    def write_new_text(self, directory: WindowsDirectory, destination: str, rendered: str) -> bool:
        """Publish immutable bytes once, retaining identity through the boundary."""
        temporary = f".{destination}.{uuid.uuid4().hex}.tmp"
        handle, created = self._primitives.open_child(
            directory.handle,
            temporary,
            create=True,
            exclusive=True,
            rename_capable=True,
        )
        if not created:
            self._primitives.close(handle)
            raise ROIAnalysisError("Warpy transfer temporary file already exists")
        renamed = False
        try:
            self._validate_regular_handle(handle)
            identity = self._primitives.identity(handle)
            self._primitives.write_all(handle, rendered.encode("utf-8"))
            self._primitives.flush(handle)
            published = self._primitives.publish_new(
                directory.handle, handle, temporary, destination
            )
            if published:
                renamed = True
                self._primitives.flush(handle)
                self._primitives.flush_directory(directory.handle)
                if (
                    self._primitives.identity(handle) != identity
                    or self._primitives.child_identity(directory.handle, destination) != identity
                ):
                    raise ROIAnalysisError("Warpy transfer publication identity changed")
            return published
        finally:
            self._finish_temporary(directory, handle, temporary, discard=not renamed)

    def replace_text(self, directory: WindowsDirectory, destination: str, rendered: str) -> None:
        """Atomically replace a child within its pinned directory handle."""
        temporary = f".{destination}.{uuid.uuid4().hex}.tmp"
        handle, created = self._primitives.open_child(
            directory.handle,
            temporary,
            create=True,
            exclusive=True,
            rename_capable=True,
        )
        if not created:
            self._primitives.close(handle)
            raise ROIAnalysisError("Warpy transfer temporary file already exists")
        renamed = False
        try:
            self._validate_regular_handle(handle)
            identity = self._primitives.identity(handle)
            self._primitives.write_all(handle, rendered.encode("utf-8"))
            self._primitives.flush(handle)
            self._primitives.replace_child(directory.handle, handle, temporary, destination)
            renamed = True
            self._primitives.flush(handle)
            self._primitives.flush_directory(directory.handle)
            if (
                self._primitives.identity(handle) != identity
                or self._primitives.child_identity(directory.handle, destination) != identity
            ):
                raise ROIAnalysisError("Warpy transfer replacement identity changed")
        finally:
            self._finish_temporary(directory, handle, temporary, discard=not renamed)

    def directory_identity(
        self,
        path: Path,
        *,
        create: bool = False,
        expected_identity: WindowsFileIdentity | None = None,
    ) -> WindowsFileIdentity:
        with self.open_directory(
            path, create=create, expected_identity=expected_identity
        ) as directory:
            return directory.identity

    def ensure_regular_file(
        self,
        path: Path,
        *,
        expected_identity: WindowsFileIdentity | None = None,
    ) -> WindowsFileIdentity:
        with self.open_directory(Path(path).parent, create=True) as directory:
            handle, created = self._primitives.open_child(
                directory.handle,
                Path(path).name,
                create=True,
                exclusive=False,
                rename_capable=False,
            )
            try:
                self._validate_regular_handle(handle)
                identity = self._primitives.identity(handle)
                if expected_identity is not None and identity != expected_identity:
                    raise ROIAnalysisError("Warpy transfer lock identity changed")
                if created:
                    self._primitives.flush(handle)
                    self._primitives.flush_directory(directory.handle)
                self._verify_named_child(directory, Path(path).name, handle, identity)
                return identity
            finally:
                self._primitives.close(handle)

    def read_text(self, directory: WindowsDirectory, name: str) -> str:
        handle, _created = self._primitives.open_child(
            directory.handle,
            name,
            create=False,
            exclusive=False,
            rename_capable=False,
        )
        try:
            self._validate_regular_handle(handle)
            identity = self._primitives.identity(handle)
            self._verify_named_child(directory, name, handle, identity)
            try:
                rendered = self._primitives.read_all(handle).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ROIAnalysisError("Warpy transfer store text is not UTF-8") from exc
            self._verify_named_child(directory, name, handle, identity)
            return rendered
        finally:
            self._primitives.close(handle)

    def child_identity(self, directory: WindowsDirectory, name: str) -> WindowsFileIdentity | None:
        self._validate_pinned_directory(directory)
        identity = self._primitives.child_identity(directory.handle, name)
        self._validate_pinned_directory(directory)
        return identity

    def list_names(self, directory: WindowsDirectory) -> tuple[str, ...]:
        self._validate_pinned_directory(directory)
        names = self._primitives.list_children(directory.handle)
        if any(
            not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name
            for name in names
        ):
            raise ROIAnalysisError("Warpy transfer store entry name is invalid")
        self._validate_pinned_directory(directory)
        return tuple(names)

    def _verify_named_child(
        self,
        directory: WindowsDirectory,
        name: str,
        handle: object,
        expected_identity: WindowsFileIdentity,
    ) -> None:
        self._validate_directory_handle(directory.handle)
        self._validate_regular_handle(handle)
        if (
            self._primitives.identity(directory.handle) != directory.identity
            or self._primitives.identity(handle) != expected_identity
            or self._primitives.child_identity(directory.handle, name) != expected_identity
        ):
            raise ROIAnalysisError("Warpy transfer lock identity changed")

    def _validate_directory_handle(self, handle: object) -> None:
        if self._primitives.is_reparse_point(handle):
            raise ROIAnalysisError("Warpy transfer store directory cannot be a reparse point")
        if not self._primitives.is_directory(handle):
            raise ROIAnalysisError("trusted Warpy transfer store component is not a directory")

    def _validate_regular_handle(self, handle: object) -> None:
        if self._primitives.is_reparse_point(handle):
            raise ROIAnalysisError("Warpy transfer store file cannot be a reparse point")
        if not self._primitives.is_regular_file(handle):
            raise ROIAnalysisError("Warpy transfer authority is not a regular file")


class UnsupportedSecureStoreBackend:
    """Import-safe backend whose operations always fail closed."""

    def __init__(self, platform: str, reason: str | None = None) -> None:
        self.platform = platform
        self.reason = reason

    @contextmanager
    def open_directory(self, path: Path, **_: object) -> Iterator[None]:
        detail = "" if self.reason is None else f": {self.reason}"
        raise ROIAnalysisError(
            f"secure Warpy transfer journaling is unsupported on "
            f"platform {self.platform!r}{detail}"
        )
        yield  # pragma: no cover - makes this a context manager


def native_win32_primitives() -> Win32SecureStorePrimitives:
    """Load the ctypes provider only when the Windows backend is selected."""
    from .win32_store import Win32CtypesSecureStorePrimitives

    return Win32CtypesSecureStorePrimitives()


def secure_store_backend(
    *,
    platform: str | None = None,
    win32_primitives: Win32SecureStorePrimitives | None = None,
) -> WindowsSecureStoreBackend | UnsupportedSecureStoreBackend | None:
    """Select the platform backend lazily, without platform-only imports.

    ``None`` denotes the existing descriptor-relative POSIX backend in the
    executor. Windows keeps durable authority in Python and selects the native
    provider lazily so importing this package remains portable.
    """
    selected = os.name if platform is None else platform
    if selected == "posix":
        return None
    if selected == "nt":
        try:
            primitives = (
                win32_primitives if win32_primitives is not None else native_win32_primitives()
            )
            return WindowsSecureStoreBackend(primitives)
        except (OSError, ROIAnalysisError) as exc:
            return UnsupportedSecureStoreBackend(selected, str(exc))
    return UnsupportedSecureStoreBackend(selected)
