"""Private pinned-filesystem primitives for reference checkpoint authority.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
from types import TracebackType
from typing import Callable
import uuid

from .publication_lock import _AuthorityPublicationLock

# ==============================================================================
#                               Reference store fs
# ==============================================================================


class ReferenceCheckpointStoreError(ValueError):
    """A checkpoint store is missing, corrupt, or structurally unsafe."""


class ReferenceCheckpointConflictError(ReferenceCheckpointStoreError):
    """A create or compare-and-swap conflicts with current authority."""


def _is_reparse_point(metadata: object) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    requested_path: Path
    canonical_path: Path
    device: int
    inode: int
    mode: int
    link_count: int
    reparse_point: bool

    @classmethod
    def capture(cls, path: Path, label: str) -> "_DirectoryIdentity":
        absolute = Path(os.path.abspath(os.fspath(path)))
        try:
            metadata = os.lstat(absolute)
            canonical = absolute.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReferenceCheckpointStoreError(f"{label} is unavailable") from exc
        reparse = _is_reparse_point(metadata)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or reparse
            or not stat.S_ISDIR(metadata.st_mode)
            or canonical != absolute
        ):
            raise ReferenceCheckpointStoreError(f"{label} must be one canonical real directory")
        return cls(
            requested_path=absolute,
            canonical_path=canonical,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            mode=int(metadata.st_mode),
            link_count=int(metadata.st_nlink),
            reparse_point=reparse,
        )

    def revalidate(self, label: str, *, exact_link_count: bool = True) -> None:
        current = type(self).capture(self.requested_path, label)
        expected = (
            self.canonical_path,
            self.device,
            self.inode,
            self.mode,
            self.reparse_point,
        )
        observed = (
            current.canonical_path,
            current.device,
            current.inode,
            current.mode,
            current.reparse_point,
        )
        if observed != expected or (exact_link_count and current.link_count != self.link_count):
            raise ReferenceCheckpointStoreError(f"{label} identity changed")


class _PinnedDirectoryHandle:
    """Pin one directory for relative POSIX mutation or Windows rename denial."""

    def __init__(
        self,
        path: Path,
        label: str,
        *,
        expected: _DirectoryIdentity | None = None,
    ) -> None:
        self.identity = _DirectoryIdentity.capture(path, label)
        if expected is not None and self.identity != expected:
            raise ReferenceCheckpointStoreError(f"{label} changed before pinning")
        self.label = label
        self._descriptor: int | None = None
        self._windows_handle: object | None = None

    def __enter__(self) -> "_PinnedDirectoryHandle":
        try:
            if os.name == "nt":
                self._windows_handle = _open_windows_directory_handle(self.identity.canonical_path)
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                self._descriptor = os.open(self.identity.canonical_path, flags)
                opened = os.fstat(self._descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                    or (int(opened.st_dev), int(opened.st_ino))
                    != (self.identity.device, self.identity.inode)
                ):
                    raise ReferenceCheckpointStoreError(
                        f"{self.label} changed while pinning its descriptor"
                    )
            self.validate()
            return self
        except BaseException:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
            if self._windows_handle is not None:
                _close_windows_handle(self._windows_handle)
                self._windows_handle = None
            raise

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, exception, traceback
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._windows_handle is not None:
            _close_windows_handle(self._windows_handle)
            self._windows_handle = None
        return False

    @staticmethod
    def _name(value: str) -> str:
        if (
            type(value) is not str
            or not value
            or value in {".", ".."}
            or Path(value).name != value
            or "/" in value
            or "\\" in value
        ):
            raise ReferenceCheckpointStoreError(
                "pinned directory mutation requires one literal child name"
            )
        return value

    def validate(self) -> None:
        self.identity.revalidate(self.label)
        if self._descriptor is not None:
            opened = os.fstat(self._descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _is_reparse_point(opened)
                or (int(opened.st_dev), int(opened.st_ino))
                != (self.identity.device, self.identity.inode)
            ):
                raise ReferenceCheckpointStoreError(
                    f"{self.label} pinned descriptor identity changed"
                )
        elif os.name == "nt" and self._windows_handle is None:
            raise RuntimeError("Windows directory handle is not open")

    def refresh_after_owned_child_directory_change(self) -> _DirectoryIdentity:
        current = _DirectoryIdentity.capture(self.identity.requested_path, self.label)
        if (
            current.canonical_path,
            current.device,
            current.inode,
            current.mode,
            current.reparse_point,
        ) != (
            self.identity.canonical_path,
            self.identity.device,
            self.identity.inode,
            self.identity.mode,
            self.identity.reparse_point,
        ):
            raise ReferenceCheckpointStoreError(
                f"{self.label} changed during owned directory creation"
            )
        self.identity = current
        self.validate()
        return current

    def mkdir(self, name: str, mode: int = 0o700) -> None:
        child = self._name(name)
        self.validate()
        if self._descriptor is not None:
            os.mkdir(child, mode=mode, dir_fd=self._descriptor)
        else:
            (self.identity.canonical_path / child).mkdir(mode=mode)

    def open_exclusive(self, name: str, mode: int = 0o600) -> int:
        child = self._name(name)
        self.validate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        if self._descriptor is not None:
            return os.open(child, flags, mode, dir_fd=self._descriptor)
        return os.open(self.identity.canonical_path / child, flags, mode)

    def link(self, source: str, target: str) -> None:
        source_name = self._name(source)
        target_name = self._name(target)
        self.validate()
        if self._descriptor is not None:
            os.link(
                source_name,
                target_name,
                src_dir_fd=self._descriptor,
                dst_dir_fd=self._descriptor,
                follow_symlinks=False,
            )
        else:
            os.link(
                self.identity.canonical_path / source_name,
                self.identity.canonical_path / target_name,
                follow_symlinks=False,
            )

    def unlink(self, name: str) -> None:
        child = self._name(name)
        self.validate()
        if self._descriptor is not None:
            os.unlink(child, dir_fd=self._descriptor)
        else:
            os.unlink(self.identity.canonical_path / child)

    def replace(self, source: str, target: str) -> None:
        source_name = self._name(source)
        target_name = self._name(target)
        self.validate()
        if self._descriptor is not None:
            os.replace(
                source_name,
                target_name,
                src_dir_fd=self._descriptor,
                dst_dir_fd=self._descriptor,
            )
        else:
            os.replace(
                self.identity.canonical_path / source_name,
                self.identity.canonical_path / target_name,
            )

    def fsync(self) -> None:
        self.validate()
        if self._descriptor is not None:
            os.fsync(self._descriptor)


def _open_windows_directory_handle(path: Path) -> object:
    if os.name != "nt":
        raise RuntimeError("Windows directory pin is unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    share_read_write = 0x1 | 0x2
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    invalid = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        generic_read,
        share_read_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle in {None, 0, invalid}:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code), str(path))
    return handle


def _close_windows_handle(handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    if not close(handle):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))


def _validate_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReferenceCheckpointStoreError(f"{label} must be a lowercase SHA-256")
    return value


def _prepare_root(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("checkpoint output root must be a Path")
    absolute = Path(os.path.abspath(os.fspath(path)))
    missing: list[str] = []
    existing = absolute
    while not existing.exists() and not existing.is_symlink():
        if existing.parent == existing:
            raise ReferenceCheckpointStoreError(
                "checkpoint output root has no existing canonical ancestor"
            )
        missing.append(existing.name)
        existing = existing.parent
    current = _DirectoryIdentity.capture(existing, "checkpoint output ancestor")
    current_path = current.canonical_path
    for component in reversed(missing):
        with _PinnedDirectoryHandle(
            current_path,
            "checkpoint output ancestor",
            expected=current,
        ) as parent_handle:
            try:
                parent_handle.mkdir(component)
            except FileExistsError:
                pass
        current_path = current_path / component
        current = _DirectoryIdentity.capture(current_path, "checkpoint output component")
    _DirectoryIdentity.capture(absolute, "checkpoint output root")
    return absolute


def _prepare_run_root(
    output_root: Path,
    run_sha256: str,
    *,
    create: bool,
    output_handle: _PinnedDirectoryHandle | None = None,
    before_mutation: Callable[[], None] | None = None,
) -> Path:
    root = output_root / run_sha256
    if create:
        try:
            if before_mutation is not None:
                before_mutation()
            if output_handle is None:
                raise RuntimeError("run creation requires a pinned output directory")
            output_handle.mkdir(run_sha256)
        except FileExistsError:
            pass
    elif not root.exists() and not root.is_symlink():
        raise ReferenceCheckpointStoreError("checkpoint run does not exist")
    _DirectoryIdentity.capture(root, "checkpoint run directory")
    if root.parent != output_root or root.name != run_sha256:
        raise ReferenceCheckpointStoreError("checkpoint run path escaped output root")
    return root


def _regular_file_metadata(path: Path, label: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ReferenceCheckpointStoreError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise ReferenceCheckpointStoreError(f"{label} cannot be a symlink or reparse point")
    if not stat.S_ISREG(metadata.st_mode):
        raise ReferenceCheckpointStoreError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise ReferenceCheckpointStoreError(f"{label} has an unsafe hard-link count")
    return metadata


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(_is_reparse_point(metadata)),
    )


def _read_bounded_regular(path: Path, *, limit: int, label: str) -> bytes:
    before = _regular_file_metadata(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
            raise ReferenceCheckpointStoreError(f"{label} is an unsafe link") from exc
        raise ReferenceCheckpointStoreError(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ReferenceCheckpointStoreError(
                f"{label} identity or hard-link state changed while opening"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ReferenceCheckpointStoreError(f"{label} exceeds its size limit")
        opened_after = os.fstat(descriptor)
        after = _regular_file_metadata(path, label)
        if not (
            _file_identity(before)
            == _file_identity(opened)
            == _file_identity(opened_after)
            == _file_identity(after)
        ):
            raise ReferenceCheckpointStoreError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _write_exclusive_temp(
    directory: _PinnedDirectoryHandle,
    prefix: str,
    raw: bytes,
    before_mutation: Callable[[], None],
) -> Path:
    name = f".{prefix}.{uuid.uuid4().hex}.tmp"
    path = directory.identity.canonical_path / name
    before_mutation()
    descriptor = directory.open_exclusive(name)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            before_mutation()
            directory.unlink(name)
        except (OSError, ReferenceCheckpointStoreError):
            pass
        raise
    finally:
        os.close(descriptor)
    return path


def _install_immutable(
    path: Path,
    raw: bytes,
    before_mutation: Callable[[], None],
    *,
    directory: _PinnedDirectoryHandle,
    read_limit: int,
    label: str,
) -> None:
    if path.parent != directory.identity.canonical_path:
        raise ReferenceCheckpointStoreError(f"{label} escaped pinned directory")
    try:
        existing = _read_bounded_regular(path, limit=read_limit, label=label)
    except ReferenceCheckpointStoreError:
        if path.exists() or path.is_symlink():
            raise
    else:
        if existing != raw:
            raise ReferenceCheckpointConflictError(f"{label} name already binds different bytes")
        return

    temporary = _write_exclusive_temp(directory, "generation", raw, before_mutation)
    try:
        try:
            before_mutation()
            directory.link(temporary.name, path.name)
        except FileExistsError:
            existing = _read_bounded_regular(path, limit=read_limit, label=label)
            if existing != raw:
                raise ReferenceCheckpointConflictError(f"{label} raced with different bytes")
        finally:
            before_mutation()
            directory.unlink(temporary.name)
        stored = _read_bounded_regular(path, limit=read_limit, label=label)
        if stored != raw:
            raise ReferenceCheckpointStoreError(f"{label} differs after installation")
        directory.fsync()
    except BaseException:
        if temporary.exists():
            before_mutation()
            directory.unlink(temporary.name)
        raise


def _replace_current_pointer(
    current_path: Path,
    raw: bytes,
    lock: _AuthorityPublicationLock,
    before_mutation: Callable[[], None],
    directory: _PinnedDirectoryHandle,
) -> None:
    if current_path.parent != directory.identity.canonical_path:
        raise ReferenceCheckpointStoreError("checkpoint pointer escaped pinned directory")
    temporary = _write_exclusive_temp(directory, "current", raw, before_mutation)
    try:
        before_mutation()
        with lock.authority_install_fence():
            before_mutation()
            directory.replace(temporary.name, current_path.name)
            _regular_file_metadata(current_path, "checkpoint current pointer")
            directory.fsync()
    finally:
        if temporary.exists():
            before_mutation()
            directory.unlink(temporary.name)


__all__ = [
    "ReferenceCheckpointConflictError",
    "ReferenceCheckpointStoreError",
]
