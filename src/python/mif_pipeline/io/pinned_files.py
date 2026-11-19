"""Retain an ordinary scientific file and its parent chain during one read.

We hash and decode through the same opened bytes. Directory and filename identity
checks detect replacement; the POSIX file fingerprint detects mutation. Windows
keeps write/delete sharing disabled until the retained native handle is closed.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

_CHUNK_BYTES = 1024 * 1024


class PinnedFileError(ValueError):
    """The admitted ordinary file was redirected, changed or unavailable."""


def _error(message: str) -> PinnedFileError:
    return PinnedFileError(message)


def _is_reparse_point(details: os.stat_result) -> bool:
    """

    Checks whether Windows marks a filesystem object as a path redirection.

    Windows reparse points include objects that can redirect a path to another
    location. We inspect the corresponding stat attribute before accepting a
    data file or parent directory. On platforms without that attribute,
    this predicate returns False and the POSIX link checks apply separately.

    Args:
        details: Operating-system stat result for the inspected object.

    Returns:
        True when the Windows reparse-point attribute is set.
    """

    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attribute and getattr(details, "st_file_attributes", 0) & attribute)


def _is_windows() -> bool:
    """

    Selects the Windows file-reading implementation for a native Windows process.

    Returns:
        True when os.name is "nt".
    """

    return os.name == "nt"


def _win32_primitives_factory() -> object:
    """

    Loads the Windows file operations when opening a data table on that platform.

    Returns:
        Win32FilePrimitives used to open files and directories, read their values
        and compare the identities reported by Windows.
    """

    from mif_pipeline.pipeline_core.win32_file_authority import Win32FilePrimitives

    return Win32FilePrimitives()


def _stat_identity(details: os.stat_result) -> tuple[int, int]:
    """

    Collects the filesystem values identifying a POSIX file or directory.

    Args:
        details: Stat result obtained from an open file or its current path.

    Returns:
        Device and inode numbers used to check that both refer to the same object.
    """

    return (details.st_dev, details.st_ino)


def _stat_mutation_fingerprint(
    details: os.stat_result,
) -> tuple[int, int, int, int]:
    """

    Collects file attributes used to detect changes during a data read.

    Args:
        details: Stat result from the opened data file.

    Returns:
        File mode, byte size, modification time and metadata-change time.
        The read compares these values again before accepting its result.
    """

    return (
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


class _PinnedPosixDirectories:
    """

    Keeps the POSIX parent directories open while locating and reading the data file.

    We open each child relative to the already open parent. The operating system
    returns a descriptor for that directory, and we retain its device/inode
    identity alongside the path. Before accepting a read, each open descriptor
    and its current pathname must still identify that same directory.

    Retaining the directory chain lets the final filename be opened relative
    to its checked parent and detects a parent that was replaced while the
    data file was being read.
    """

    def __init__(self, entries: list[tuple[Path, int, tuple[int, int]]]) -> None:
        """

        Retains the open directory chain and the identities recorded when it was opened.

        Args:
            entries: Ordered tuples of directory Path, open operating-system
                descriptor and device/inode pair.
        """

        self._entries = entries

    @property
    def descriptor(self) -> int:
        """

        Returns the open descriptor of the deepest retained parent directory.

        Returns:
            Integer descriptor used to open the next relative child name.
        """

        return self._entries[-1][1]

    def open_child(self, component: str) -> None:
        """

        Opens the next directory relative to the retained parent and records its identity.

        We use directory-only and no-follow flags, then inspect the opened object
        before adding it to the retained chain. A failed directory check closes
        that newly opened descriptor immediately.

        Args:
            component: Single child-directory name from the already normalized path.

        Raises:
            PinnedFileError: The child cannot be opened as the required
                real directory or its file attributes indicate a redirection.
        """

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_path, parent, _identity = self._entries[-1]
        try:
            descriptor = os.open(component, flags, dir_fd=parent)
            details = os.fstat(descriptor)
            if not stat.S_ISDIR(details.st_mode) or _is_reparse_point(details):
                os.close(descriptor)
                raise _error("scientific data parent contains an unsafe directory")
            self._entries.append((parent_path / component, descriptor, _stat_identity(details)))
        except PinnedFileError:
            raise
        except OSError as exc:
            raise _error("scientific data parent cannot be opened safely") from exc

    @classmethod
    def open(cls, path: Path) -> "_PinnedPosixDirectories":
        """

        Opens every directory from the filesystem root to the selected output folder.

        We start at the root, open each successive component relative to its open
        parent, and retain the resulting path, descriptor and device/inode values.
        The complete chain is compared with its current pathnames before return.
        If any opening or comparison fails, retained descriptors are closed in
        reverse order.

        Args:
            path: Absolute normalized output-directory Path.

        Returns:
            Open directory chain ready for locating the data file beneath it.

        Raises:
            PinnedFileError: A component is missing, linked, replaced or
                is not a directory.
        """

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        entries: list[tuple[Path, int, tuple[int, int]]] = []
        current_path = Path(path.anchor)
        success = False
        try:
            descriptor = os.open(current_path, flags)
            details = os.fstat(descriptor)
            if not stat.S_ISDIR(details.st_mode) or _is_reparse_point(details):
                raise _error("authenticated output root contains an unsafe directory")
            entries.append((current_path, descriptor, _stat_identity(details)))
            for component in path.parts[1:]:
                parent = entries[-1][1]
                current_path /= component
                descriptor = os.open(component, flags, dir_fd=parent)
                details = os.fstat(descriptor)
                if not stat.S_ISDIR(details.st_mode) or _is_reparse_point(details):
                    os.close(descriptor)
                    raise _error("authenticated output root contains an unsafe directory")
                entries.append((current_path, descriptor, _stat_identity(details)))
            result = cls(entries)
            result.validate()
            success = True
            return result
        except PinnedFileError:
            raise
        except OSError as exc:
            if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
                raise _error("authenticated output root contains a link") from exc
            raise _error("authenticated output root cannot be opened safely") from exc
        finally:
            if not success:
                for _path, descriptor, _identity in reversed(entries):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def validate(self) -> None:
        """

        Compares the retained directory descriptors with their original identities and current paths.

        For each directory, we inspect its open descriptor and its pathname without
        following links. Both must remain directories with the same device/inode
        values recorded during opening. A replaced path therefore cannot silently
        redirect the completed read's saved filename.

        Raises:
            PinnedFileError: A directory or its current pathname is absent,
                linked or no longer identifies the retained directory.
        """

        for path, descriptor, identity in self._entries:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(details.st_mode)
                or _is_reparse_point(details)
                or _stat_identity(details) != identity
            ):
                raise _error("authenticated output root retained directory changed")
            try:
                named = os.lstat(path)
            except OSError as exc:
                raise _error("authenticated output root was replaced") from exc
            if (
                stat.S_ISLNK(named.st_mode)
                or _is_reparse_point(named)
                or not stat.S_ISDIR(named.st_mode)
                or _stat_identity(named) != identity
            ):
                raise _error("authenticated output root was replaced")

    def close(self) -> None:
        """

        Closes the retained directory descriptors from the deepest directory back to the root.

        We attempt every close even if another close fails, then clear the stored
        chain so it no longer exposes descriptors for later reading.
        """

        for _path, descriptor, _identity in reversed(self._entries):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._entries = []


class _OpenedPosixFile:
    """

    Retains one open data file and its parent directories throughout a complete read.

    We record the original device/inode, byte size, mode and change timestamps.
    The public stream reads through that same open descriptor. validate() later compares
    those values with both the retained descriptor and the filename in its
    retained parent, detecting ordinary edits or replacement during the read.
    """

    def __init__(
        self,
        directories: _PinnedPosixDirectories,
        descriptor: int,
        identity: tuple[int, int],
        fingerprint: tuple[int, int, int, int],
        size: int,
        leaf: str,
    ) -> None:
        """

        Stores the open data file and the values needed for its final comparison.

        Args:
            directories: Retained directory chain ending at the file's parent.
            descriptor: Open descriptor from which the bytes are read.
            identity: Device/inode pair recorded when that file was opened.
            fingerprint: Original mode, byte size and modification/change timestamps.
            size: Original file size in bytes.
            leaf: Measurement filename relative to the retained parent directory.
        """

        self._directories = directories
        self._descriptor = descriptor
        self._identity = identity
        self._fingerprint = fingerprint
        self.size = size
        self._leaf = leaf


    def validate(self) -> None:
        """

        Checks that the open data file and its relative filename still match the initial read target.

        We first verify the retained parent directories. The file must remain a
        regular single-link file with the same device/inode, mode, size and change
        timestamps. We then inspect its name relative to the open parent and require
        that entry to have the same values.

        Raises:
            PinnedFileError: The file was edited, replaced, linked or
                became unavailable during the data read.
        """

        self._directories.validate()
        details = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or _is_reparse_point(details)
            or details.st_nlink != 1
            or _stat_identity(details) != self._identity
            or _stat_mutation_fingerprint(details) != self._fingerprint
        ):
            raise _error("scientific data artifact changed during streaming")
        try:
            named = os.stat(self._leaf, dir_fd=self._directories.descriptor, follow_symlinks=False)
        except OSError as exc:
            raise _error("scientific data artifact was replaced") from exc
        if (
            stat.S_ISLNK(named.st_mode)
            or _is_reparse_point(named)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or _stat_identity(named) != self._identity
            or _stat_mutation_fingerprint(named) != self._fingerprint
        ):
            raise _error("scientific data artifact was replaced")

    def close(self) -> None:
        """

        Closes the data-file descriptor and releases its retained parent directories.

        The parent-directory cleanup runs even if closing the file raises an error.
        """

        try:
            os.close(self._descriptor)
        finally:
            self._directories.close()


class _OpenedWin32File:
    """

    Retains the native Windows file and directory handles used for a complete data read.

    The file is opened without write or delete sharing for the read interval.
    We retain its Windows identity and byte size, together with its directory
    handles, and compare the parent-relative filename before accepting the read.
    This is the Windows counterpart of the POSIX open-descriptor implementation.
    """

    def __init__(
        self,
        primitives: object,
        directories: list[tuple[Path, object, object]],
        file_handle: object,
        identity: object,
        size: int,
        leaf: str,
    ) -> None:
        """

        Stores the Windows handles and original file identity used by the data reader.

        Args:
            primitives: Windows file operations used to inspect, read and close handles.
            directories: Ordered directory paths, open handles and recorded identities.
            file_handle: Open data-file handle.
            identity: Windows identity recorded for that file.
            size: Original byte size.
            leaf: Measurement filename within the retained parent directory.
        """

        self._primitives = primitives
        self._directories = directories
        self._file_handle = file_handle
        self._identity = identity
        self.size = size
        self._leaf = leaf

    @classmethod
    def open(cls, root: Path, relative: Path) -> "_OpenedWin32File":
        """

        Opens a data file inside the selected output folder using Windows handles.

        We open and inspect the output directory and each relative subdirectory,
        retaining their handles. The final file is opened with no-follow checks
        and with write/delete sharing disabled. It must be a regular single-link
        file with a valid byte size. The full directory/file relationship is checked
        before return; a failed opening closes the handles already acquired.

        Args:
            root: Absolute analysis-output directory.
            relative: Checked path from that directory to the data file.

        Returns:
            Windows retained file exposing validate(), close() and size.

        Raises:
            PinnedFileError: A directory/file is redirected, incorrectly
                typed, linked, unavailable or inconsistent during opening.
        """

        primitives = _win32_primitives_factory()
        directories: list[tuple[Path, object, object]] = []
        file_handle: object | None = None
        success = False
        try:
            current_path = root
            root_handle = primitives.open_directory(root, create=False, no_follow=True)  # type: ignore[attr-defined]
            directories.append((root, root_handle, cls._safe_directory(primitives, root_handle)))
            for component in relative.parts[:-1]:
                current_path /= component
                handle = primitives.open_directory(current_path, create=False, no_follow=True)  # type: ignore[attr-defined]
                directories.append((current_path, handle, cls._safe_directory(primitives, handle)))
            parent = directories[-1][1]
            file_handle = primitives.open_file(  # type: ignore[attr-defined]
                parent,
                relative.parts[-1],
                no_follow=True,
                share_delete=False,
                share_write=False,
            )
            if (
                primitives.is_reparse_point(file_handle)  # type: ignore[attr-defined]
                or not primitives.is_regular_file(file_handle)  # type: ignore[attr-defined]
                or primitives.link_count(file_handle) != 1  # type: ignore[attr-defined]
            ):
                raise _error("scientific data artifact is not one safe regular file")
            identity = primitives.identity(file_handle)  # type: ignore[attr-defined]
            size = primitives.size(file_handle)  # type: ignore[attr-defined]
            if type(size) is not int or size < 0:
                raise _error("scientific data artifact size is invalid")
            result = cls(primitives, directories, file_handle, identity, size, relative.parts[-1])
            result.validate()
            success = True
            return result
        except PinnedFileError:
            raise
        except Exception as exc:
            raise _error("scientific data Win32 authority cannot be opened safely") from exc
        finally:
            if not success:
                if file_handle is not None:
                    try:
                        primitives.close(file_handle)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                for _path, handle, _identity in reversed(directories):
                    try:
                        primitives.close(handle)  # type: ignore[attr-defined]
                    except Exception:
                        pass

    @staticmethod
    def _safe_directory(primitives: object, handle: object) -> object:
        """

        Checks an opened Windows directory and obtains its identity.

        Args:
            primitives: Windows file operations used for the checks.
            handle: Already opened directory handle.

        Returns:
            Native directory identity used for later path comparisons.

        Raises:
            PinnedFileError: The handle identifies a reparse point or
                an object that is not a directory.
        """

        if primitives.is_reparse_point(handle) or not primitives.is_directory(handle):  # type: ignore[attr-defined]
            raise _error("authenticated output root contains an unsafe directory")
        return primitives.identity(handle)  # type: ignore[attr-defined]


    def validate(self) -> None:
        """

        Rechecks the Windows file and directory handles against their current filenames.

        We reopen each retained directory path and compare its identity with the
        original handle. The file must still have the original identity and size,
        remain a regular single-link file, and match the child name in its retained
        parent directory.

        Raises:
            PinnedFileError: An opened object or its named path changed
                during the data read.
        """

        try:
            for path, handle, identity in self._directories:
                if self._safe_directory(self._primitives, handle) != identity:
                    raise _error("authenticated output root retained directory changed")
                reopened = self._primitives.open_directory(path, create=False, no_follow=True)
                try:
                    if self._safe_directory(self._primitives, reopened) != identity:
                        raise _error("authenticated output root was replaced")
                finally:
                    self._primitives.close(reopened)
            parent = self._directories[-1][1]
            if (
                self._primitives.is_reparse_point(self._file_handle)
                or not self._primitives.is_regular_file(self._file_handle)
                or self._primitives.link_count(self._file_handle) != 1
                or self._primitives.identity(self._file_handle) != self._identity
                or self._primitives.size(self._file_handle) != self.size
                or self._primitives.child_identity(parent, self._leaf) != self._identity
            ):
                raise _error("scientific data artifact changed during streaming")
        except PinnedFileError:
            raise
        except Exception as exc:
            raise _error("scientific data Win32 authority changed") from exc

    def close(self) -> None:
        """

        Closes the Windows data file and each retained parent-directory handle.

        Directory handles are closed in reverse order even if file cleanup fails.
        We attempt every directory close so one failed release does not skip the rest.
        """

        try:
            self._primitives.close(self._file_handle)
        finally:
            for _path, handle, _identity in reversed(self._directories):
                try:
                    self._primitives.close(handle)
                except Exception:
                    pass


def _open_regular_under_root(
    root: Path, path: Path
) -> _OpenedPosixFile | _OpenedWin32File:
    """

    Opens the selected data file inside the output directory allowed by this analysis.

    We first require a nonempty path relative to the selected output directory,
    without dot or parent-directory components. Windows uses its native retained-
    handle reader. POSIX opens the parent chain and final file with no-follow
    checks, requires a regular single-link file and records its initial size
    and identity before constructing the reader.

    Args:
        root: Absolute normalized output-directory Path for the current operation.
        path: Absolute normalized data-file Path from its saved description.

    Returns:
        Platform-specific retained file supplying validate(), close()
        and the initial file size.

    Raises:
        PinnedFileError: The path leaves the selected directory or
            its directory/file cannot be opened with the required identity checks.
    """

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _error("scientific data path escapes authenticated output root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _error("scientific data path is not a regular root-relative leaf")
    if _is_windows():
        return _OpenedWin32File.open(root, relative)
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    directories: _PinnedPosixDirectories | None = None
    descriptor: int | None = None
    success = False
    try:
        directories = _PinnedPosixDirectories.open(root)
        for component in relative.parts[:-1]:
            directories.open_child(component)
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directories.descriptor)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or _is_reparse_point(details) or details.st_nlink != 1:
            os.close(descriptor)
            descriptor = None
            raise _error("scientific data artifact is not one safe regular file")
        result = _OpenedPosixFile(
            directories,
            descriptor,
            _stat_identity(details),
            _stat_mutation_fingerprint(details),
            details.st_size,
            relative.parts[-1],
        )
        result.validate()
        success = True
        return result
    except PinnedFileError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
            raise _error("scientific data artifact contains a link") from exc
        raise _error("scientific data artifact cannot be opened safely") from exc
    finally:
        if not success:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if directories is not None:
                directories.close()


class PinnedReadFile:
    """Own one seekable read stream and the retained identity used to admit it.

    Our stream duplicates the retained file descriptor or Windows handle; it
    never reopens a pathname. Closing the stream leaves the original handle
    available for the ending filename and metadata comparison.
    """

    def __init__(self, opened: _OpenedPosixFile | _OpenedWin32File) -> None:
        self._opened = opened
        self.size = opened.size
        self._stream: BinaryIO | None = None

    @property
    def stream(self) -> BinaryIO:
        """Expose read/seek/tell on the same retained file, without write access."""
        if self._opened is None:
            raise PinnedFileError("Pinned file is closed")
        if self._stream is None:
            if isinstance(self._opened, _OpenedPosixFile):
                descriptor = os.dup(self._opened._descriptor)
                try:
                    self._stream = os.fdopen(descriptor, "rb")
                except BaseException:
                    os.close(descriptor)
                    raise
            else:
                self._stream = self._opened._primitives.duplicate_readonly_stream(
                    self._opened._file_handle
                )
        return self._stream

    def chunks(self) -> Iterator[bytes]:
        """Read our opened bytes in blocks of at most one MiB."""
        while chunk := self.stream.read(_CHUNK_BYTES):
            yield chunk

    def validate(self) -> None:
        """Compare the retained metadata and current name without rereading bytes."""
        if self._opened is None:
            raise PinnedFileError("Pinned file is closed")
        self._opened.validate()

    def close(self) -> None:
        """Release both stream and identity handles, including all parents."""
        if self._opened is None:
            return
        opened, self._opened = self._opened, None
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            opened.close()

    def __enter__(self) -> PinnedReadFile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.validate()
        finally:
            self.close()


def open_pinned_member(root: Path, member: str) -> PinnedReadFile:
    """Open an exact POSIX member beneath the caller's admitted absolute root.

    We reject alternate spellings before joining the member. Scientific owners
    still compare this member with their accepted producer identity; this IO
    owner does not grant scientific authority from the filename.
    """
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or os.path.normpath(root) != str(root):
        raise PinnedFileError("Admitted file root must be absolute and normalized")
    if type(member) is not str or not member or any(c in member for c in "\\:\x00"):
        raise PinnedFileError("File member must be canonical relative POSIX text")
    relative = PurePosixPath(member)
    if relative.is_absolute() or any(c in {"", ".", ".."} for c in member.split("/")) or relative.as_posix() != member:
        raise PinnedFileError("File member must be canonical relative POSIX text")
    return PinnedReadFile(_open_regular_under_root(root, root.joinpath(*relative.parts)))
