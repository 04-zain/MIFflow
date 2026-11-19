"""Cross-platform exclusive lock for one immutable publication pointer.

The owner-record wire schema retains its name for durable compatibility. The lock algorithm
itself is publication-agnostic.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import time
from types import TracebackType
from typing import BinaryIO, Iterator
import uuid

_LOCK_SCHEMA_NAME = "mif_pipeline.ssasu_publication_lock_owner"
_LOCK_SCHEMA_VERSION = 1
_DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 30.0
_DEFAULT_STALE_AFTER_SECONDS = 0.25
_DEFAULT_POLL_SECONDS = 0.01
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[tuple[int, str], threading.Lock] = {}


# ==============================================================================
#                                Publication lock
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _PublicationLockFileIdentity:
    """Identity and link state pinned from an already-open lock file."""

    device: int
    inode: int
    link_count: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "_PublicationLockFileIdentity":
        return cls(
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            link_count=int(metadata.st_nlink),
        )


def _is_reparse_point(metadata: object) -> bool:
    """Read Windows reparse state without importing a platform-only API."""
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validate_regular_lock_metadata(metadata: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise ValueError(f"{label} cannot be a symlink or reparse point")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label} must have exactly one link")


def _validate_authority_entry(path: Path) -> None:
    """Reject a final authority entry whose path semantics are ambiguous."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise ValueError("authority path cannot be a symlink or reparse point")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("authority path must be a regular file when present")


def _canonical_authority_path(path: Path) -> Path:
    """Resolve only the parent; the final authority entry is never followed."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.name or absolute.name in {".", ".."}:
        raise ValueError("authority path must name a file")
    try:
        parent = absolute.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("authority parent cannot be resolved safely") from exc
    parent_metadata = os.lstat(parent)
    if _is_reparse_point(parent_metadata) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("resolved authority parent is not a safe directory")
    canonical = parent / absolute.name
    _validate_authority_entry(canonical)
    return canonical


def _open_lock_descriptor(path: Path) -> tuple[int, _PublicationLockFileIdentity]:
    """Open a lock without following its final entry and pin its named identity."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None:
        _validate_regular_lock_metadata(before, label="publication lock")

    try:
        if os.name == "nt":
            descriptor = _open_windows_lock_descriptor(path)
        else:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
            raise ValueError("publication lock cannot be a symlink or reparse point") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        _validate_regular_lock_metadata(opened, label="publication lock")
        try:
            named = os.lstat(path)
        except FileNotFoundError as exc:
            raise RuntimeError("publication lock identity disappeared while opening") from exc
        _validate_regular_lock_metadata(named, label="publication lock")
        opened_identity = _PublicationLockFileIdentity.from_stat(opened)
        named_identity = _PublicationLockFileIdentity.from_stat(named)
        if opened_identity != named_identity:
            raise RuntimeError("publication lock named identity differs from opened file")
        if before is not None and _PublicationLockFileIdentity.from_stat(before) != opened_identity:
            raise RuntimeError("publication lock changed while opening")
        return descriptor, opened_identity
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_lock_descriptor(path: Path) -> int:
    """Open a Win32 lock handle without delete sharing, preserving pointer width."""
    if os.name != "nt":
        raise RuntimeError("Win32 lock open is unavailable on this platform")
    import ctypes
    from ctypes import wintypes
    import msvcrt

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read_write = 0x1 | 0x2
    open_always = 4
    file_attribute_normal = 0x80
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value
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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        generic_read | generic_write,
        share_read_write,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle in {None, 0, invalid_handle_value}:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code), str(path))
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _thread_lock_for(path: Path) -> threading.Lock:
    key = (os.getpid(), os.path.normcase(os.path.abspath(os.fspath(path))))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate publication lock owner field")
        value[key] = item
    return value


def _exact_nonnegative_seconds(value: object, label: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{label} must be an exact number")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return result


@dataclass(frozen=True, slots=True)
class _PublicationLockOwner:
    owner_id: str
    process_id: int
    thread_id: int
    host_name: str
    acquired_at_unix_ns: int
    authority_path: str

    @classmethod
    def create(cls, authority_path: Path) -> "_PublicationLockOwner":
        return cls(
            owner_id=uuid.uuid4().hex,
            process_id=os.getpid(),
            thread_id=threading.get_ident(),
            host_name=socket.gethostname(),
            acquired_at_unix_ns=time.time_ns(),
            authority_path=str(authority_path.absolute()),
        )

    def __post_init__(self) -> None:
        if (
            type(self.owner_id) is not str
            or len(self.owner_id) != 32
            or any(character not in "0123456789abcdef" for character in self.owner_id)
        ):
            raise ValueError("publication lock owner ID is invalid")
        if type(self.process_id) is not int or self.process_id <= 0:
            raise ValueError("publication lock process ID is invalid")
        if type(self.thread_id) is not int or self.thread_id <= 0:
            raise ValueError("publication lock thread ID is invalid")
        if type(self.host_name) is not str or not self.host_name:
            raise ValueError("publication lock host name is invalid")
        if type(self.acquired_at_unix_ns) is not int or self.acquired_at_unix_ns <= 0:
            raise ValueError("publication lock acquisition time is invalid")
        if type(self.authority_path) is not str or not self.authority_path:
            raise ValueError("publication lock authority path is invalid")

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema_name": _LOCK_SCHEMA_NAME,
                    "schema_version": _LOCK_SCHEMA_VERSION,
                    "owner_id": self.owner_id,
                    "process_id": self.process_id,
                    "thread_id": self.thread_id,
                    "host_name": self.host_name,
                    "acquired_at_unix_ns": self.acquired_at_unix_ns,
                    "authority_path": self.authority_path,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "_PublicationLockOwner":
        value = json.loads(raw, object_pairs_hook=_strict_object)
        expected = {
            "schema_name",
            "schema_version",
            "owner_id",
            "process_id",
            "thread_id",
            "host_name",
            "acquired_at_unix_ns",
            "authority_path",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("publication lock owner fields are invalid")
        if (value["schema_name"], value["schema_version"]) != (
            _LOCK_SCHEMA_NAME,
            _LOCK_SCHEMA_VERSION,
        ):
            raise ValueError("publication lock owner schema is invalid")
        return cls(
            owner_id=value["owner_id"],
            process_id=value["process_id"],
            thread_id=value["thread_id"],
            host_name=value["host_name"],
            acquired_at_unix_ns=value["acquired_at_unix_ns"],
            authority_path=value["authority_path"],
        )


class _AuthorityPublicationLock:
    """Serialize publishers using one persistent, kernel-locked file.

    The named file is never unlinked: replacing a locked inode would allow a
    second publisher to lock a different inode under the same path. A crashed
    process leaves owner metadata, but its kernel lock is released by the OS.
    A successor may replace that metadata only after it acquires the kernel
    lock and the configured stale age has elapsed. Parent aliases resolve to
    one canonical path, and every owner pins and revalidates the opened file's
    named identity and link state before authority installation and cleanup.
    """

    def __init__(
        self,
        authority_path: Path,
        *,
        acquire_timeout_seconds: float = _DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        if not isinstance(authority_path, Path):
            raise TypeError("authority path must be a Path")
        self._requested_authority_path = authority_path
        self.authority_path = authority_path
        self.lock_path = authority_path.with_name(f".{authority_path.name}.publication.lock")
        self.acquire_timeout_seconds = _exact_nonnegative_seconds(
            acquire_timeout_seconds, "publication lock timeout"
        )
        self.stale_after_seconds = _exact_nonnegative_seconds(
            stale_after_seconds, "publication lock stale age"
        )
        self.poll_seconds = _exact_nonnegative_seconds(
            poll_seconds, "publication lock poll interval"
        )
        if self.poll_seconds == 0.0:
            raise ValueError("publication lock poll interval must be positive")
        self._stream: BinaryIO | None = None
        self._owner_bytes: bytes | None = None
        self._lock_identity: _PublicationLockFileIdentity | None = None
        self._locked = False
        self._thread_lock: threading.Lock | None = None
        self._thread_locked = False

    def __enter__(self) -> "_AuthorityPublicationLock":
        if self._stream is not None:
            raise RuntimeError("publication lock instances are single-use")
        self._requested_authority_path.parent.mkdir(parents=True, exist_ok=True)
        self.authority_path = _canonical_authority_path(self._requested_authority_path)
        self.lock_path = self.authority_path.with_name(
            f".{self.authority_path.name}.publication.lock"
        )
        self._thread_lock = _thread_lock_for(self.lock_path)
        deadline = time.monotonic() + self.acquire_timeout_seconds
        if not self._thread_lock.acquire(timeout=self.acquire_timeout_seconds):
            raise self._timeout_error()
        self._thread_locked = True
        try:
            descriptor, identity = _open_lock_descriptor(self.lock_path)
            self._lock_identity = identity
            try:
                stream = os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                self._lock_identity = None
                raise
            self._stream = stream
            try:
                while True:
                    if self._try_kernel_lock():
                        self._validate_lock_identity()
                        if self._may_claim_locked_file():
                            owner = _PublicationLockOwner.create(self.authority_path)
                            self._owner_bytes = owner.to_bytes()
                            self._write_raw(self._owner_bytes)
                            self.validate_identity()
                            return self
                        self._release_kernel_lock()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        raise self._timeout_error()
                    time.sleep(min(self.poll_seconds, remaining))
            except BaseException as exc:
                if self._locked:
                    try:
                        self._release_kernel_lock()
                    except BaseException as cleanup_exc:
                        exc.add_note(
                            "releasing failed publication lock acquisition "
                            f"also failed: {cleanup_exc!r}"
                        )
                try:
                    stream.close()
                except BaseException as cleanup_exc:
                    exc.add_note(
                        "closing failed publication lock acquisition also "
                        f"failed: {cleanup_exc!r}"
                    )
                finally:
                    self._stream = None
                    self._lock_identity = None
                raise
        except BaseException:
            self._release_thread_lock()
            raise

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        cleanup_error: BaseException | None = None
        stream = self._require_stream()
        if self._locked:
            try:
                self.validate_identity()
                self._write_raw(b"\n")
                self._validate_lock_identity()
            except BaseException as exc:
                cleanup_error = exc
            try:
                self._release_kernel_lock()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(f"unlocking publication lock also failed: {exc!r}")
        try:
            stream.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error.add_note(f"closing publication lock also failed: {exc!r}")
        finally:
            self._stream = None
            self._owner_bytes = None
            self._lock_identity = None
        try:
            self._release_thread_lock()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error.add_note(f"releasing thread lock also failed: {exc!r}")
        if cleanup_error is not None:
            if exception is not None:
                exception.add_note(f"publication lock cleanup also failed: {cleanup_error!r}")
                return False
            raise cleanup_error
        return False

    def _timeout_error(self) -> TimeoutError:
        return TimeoutError(f"timed out acquiring publication lock {self.lock_path}")

    def _release_thread_lock(self) -> None:
        if self._thread_locked:
            if self._thread_lock is None:
                raise RuntimeError("publication thread lock is missing")
            self._thread_lock.release()
            self._thread_locked = False

    def _require_stream(self) -> BinaryIO:
        if self._stream is None:
            raise RuntimeError("publication lock is not open")
        return self._stream

    def _write_raw(self, raw: bytes) -> None:
        stream = self._require_stream()
        stream.seek(0)
        stream.write(raw)
        stream.truncate(len(raw))
        stream.flush()
        os.fsync(stream.fileno())

    def _read_raw(self) -> bytes:
        stream = self._require_stream()
        stream.seek(0)
        return stream.read()

    def _validate_lock_identity(self) -> None:
        stream = self._require_stream()
        expected = self._lock_identity
        if expected is None:
            raise RuntimeError("publication lock identity is not pinned")
        try:
            opened = os.fstat(stream.fileno())
            _validate_regular_lock_metadata(opened, label="publication lock")
            named = os.lstat(self.lock_path)
            _validate_regular_lock_metadata(named, label="publication lock")
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(
                "publication lock named identity was deleted or became unsafe"
            ) from exc
        if (
            _PublicationLockFileIdentity.from_stat(opened) != expected
            or _PublicationLockFileIdentity.from_stat(named) != expected
        ):
            raise RuntimeError(
                "publication lock named identity was replaced or its link " "state changed"
            )

    def validate_identity(self) -> None:
        """Fence a held owner against lock-path and authority-entry replacement."""
        if not self._locked:
            raise RuntimeError("publication lock is not held")
        self._validate_lock_identity()
        _validate_authority_entry(self.authority_path)
        if self._owner_bytes is None or self._read_raw() != self._owner_bytes:
            raise RuntimeError("publication lock owner metadata changed while held")

    @contextmanager
    def authority_install_fence(self) -> Iterator[None]:
        """Serialize the final pointer transition on the canonical parent.

        POSIX permits a process that can write the parent directory to unlink a
        locked file.  A short advisory lock on the already-canonical parent
        therefore gives cooperating publishers a second, inode-stable fence
        around successor revalidation and pointer installation.  Windows
        instead opens the publication-lock handle without delete sharing.
        """
        self.validate_identity()
        if os.name != "posix":
            yield
            return

        parent = self.authority_path.parent
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(parent, flags)
        acquired = False
        primary_error: BaseException | None = None
        try:
            opened = os.fstat(descriptor)
            named = os.lstat(parent)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _is_reparse_point(opened)
                or _is_reparse_point(named)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise RuntimeError("authority parent identity changed before installation")

            import fcntl

            deadline = time.monotonic() + self.acquire_timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        raise TimeoutError(
                            "timed out acquiring authority install fence " f"for {parent}"
                        ) from exc
                    time.sleep(min(self.poll_seconds, remaining))
                else:
                    acquired = True
                    break

            named = os.lstat(parent)
            if (
                not stat.S_ISDIR(named.st_mode)
                or _is_reparse_point(named)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise RuntimeError(
                    "authority parent identity changed while acquiring " "the install fence"
                )
            self.validate_identity()
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if acquired:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except BaseException as exc:
                    cleanup_error = exc
            try:
                os.close(descriptor)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(
                        "closing the authority install-fence descriptor " f"also failed: {exc!r}"
                    )
            if cleanup_error is not None:
                if primary_error is not None:
                    primary_error.add_note(
                        "releasing the authority install fence also " f"failed: {cleanup_error!r}"
                    )
                else:
                    raise cleanup_error

    def _may_claim_locked_file(self) -> bool:
        self._validate_lock_identity()
        raw = self._read_raw()
        if not raw.strip():
            return True
        try:
            prior_owner = _PublicationLockOwner.from_bytes(raw)
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("publication lock owner metadata is malformed") from exc
        if prior_owner.authority_path != str(self.authority_path):
            raise ValueError("publication lock owner authority path differs")
        prior_acquired_at = prior_owner.acquired_at_unix_ns
        modified_at = os.fstat(self._require_stream().fileno()).st_mtime_ns
        last_owner_write = max(prior_acquired_at, modified_at)
        age_ns = max(0, time.time_ns() - last_owner_write)
        return age_ns >= int(self.stale_after_seconds * 1_000_000_000)

    def _try_kernel_lock(self) -> bool:
        stream = self._require_stream()
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
        elif os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
        else:
            raise RuntimeError(f"publication locking is unsupported on platform {os.name!r}")
        self._locked = True
        return True

    def _release_kernel_lock(self) -> None:
        if not self._locked:
            return
        stream = self._require_stream()
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                raise RuntimeError(f"publication locking is unsupported on platform {os.name!r}")
        finally:
            self._locked = False
