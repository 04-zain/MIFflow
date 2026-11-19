"""Native Win32 primitives for the authoritative Warpy transfer store.

The `Win32Handle` record carries `value`, `path`, `ancestors`."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path

from mif_pipeline.pipeline_core.models import ROIAnalysisError

from .secure_store import WindowsFileIdentity

# ==============================================================================
#                                  Win32 store
# ==============================================================================


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _FILE_RENAME_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


@dataclass(slots=True)
class Win32Handle:
    value: int
    path: Path
    ancestors: tuple["Win32Handle", ...] = ()


class Win32CtypesSecureStorePrimitives:
    """Local-NTFS/ReFS provider using pinned, no-delete-share handles.

    ``CreateFileW`` cannot name a child relative to a directory handle.  The
    equivalent boundary used here opens every ancestor with
    ``OPEN_REPARSE_POINT`` and without ``FILE_SHARE_DELETE``, retains those
    handles, and verifies the leaf through ``FILE_ID_INFO``.
    """

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_LIST_DIRECTORY = 0x1
    FILE_READ_ATTRIBUTES = 0x80
    DELETE = 0x00010000
    SYNCHRONIZE = 0x00100000
    SHARE_READ_WRITE = 0x1 | 0x2
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ID_INFO_CLASS = 18
    FILE_STANDARD_INFO_CLASS = 1
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    FILE_RENAME_INFO_CLASS = 3
    FILE_DISPOSITION_INFO_CLASS = 4
    LOCKFILE_EXCLUSIVE_LOCK = 0x2
    DRIVE_FIXED = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, kernel32: object | None = None) -> None:
        if kernel32 is None:
            if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
                raise ROIAnalysisError("native Win32 secure-store API is unavailable")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32 = kernel32
        self._locks: dict[int, _OVERLAPPED] = {}
        self._configure_ctypes_signatures()

    def _configure_ctypes_signatures(self) -> None:
        """Prevent 64-bit HANDLE truncation by ctypes' default ``c_int``."""
        signatures = {
            "CreateFileW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ],
                wintypes.HANDLE,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetFileInformationByHandleEx": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "LockFileEx": (
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(_OVERLAPPED),
                ],
                wintypes.BOOL,
            ),
            "UnlockFileEx": (
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(_OVERLAPPED),
                ],
                wintypes.BOOL,
            ),
            "FlushFileBuffers": ([wintypes.HANDLE], wintypes.BOOL),
            "SetFilePointerEx": (
                [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "SetEndOfFile": ([wintypes.HANDLE], wintypes.BOOL),
            "ReadFile": (
                [
                    wintypes.HANDLE,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "WriteFile": (
                [
                    wintypes.HANDLE,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "SetFileInformationByHandle": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "DeleteFileW": ([wintypes.LPCWSTR], wintypes.BOOL),
            "CreateDirectoryW": (
                [wintypes.LPCWSTR, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "GetVolumePathNameW": (
                [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "GetDriveTypeW": ([wintypes.LPCWSTR], wintypes.UINT),
            "GetVolumeInformationW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPWSTR,
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.kernel32, name, None)
            if function is not None and hasattr(function, "argtypes"):
                function.argtypes = argtypes
                function.restype = restype

    def _failure(self, operation: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{operation} failed", None, code)

    def _truthy(self, operation: str, *args: object) -> object:
        result = getattr(self.kernel32, operation)(*args)
        if not result:
            raise self._failure(operation)
        return result

    @staticmethod
    def _component(name: str) -> None:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "\0" in name
            or "\\" in name
            or "/" in name
        ):
            raise ROIAnalysisError("Warpy transfer store entry name is invalid")

    def _open_raw(
        self,
        path: Path,
        *,
        directory: bool,
        create: bool,
        exclusive: bool,
        rename_capable: bool = False,
        share_delete: bool = False,
    ) -> Win32Handle:
        disposition = (
            (self.CREATE_NEW if exclusive else self.OPEN_ALWAYS) if create else self.OPEN_EXISTING
        )
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= self.FILE_FLAG_BACKUP_SEMANTICS
        desired_access = (
            self.FILE_LIST_DIRECTORY | self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
            if directory
            else self.GENERIC_READ
            | self.GENERIC_WRITE
            | self.FILE_READ_ATTRIBUTES
            | (self.DELETE if rename_capable else 0)
        )
        value = getattr(self.kernel32, "CreateFileW")(
            str(path),
            desired_access,
            self.SHARE_READ_WRITE | (0x4 if share_delete else 0),
            None,
            disposition,
            flags,
            None,
        )
        if value in (None, 0, self.INVALID_HANDLE_VALUE):
            code = ctypes.get_last_error()
            if code in {2, 3}:
                raise FileNotFoundError(str(path))
            if code in {80, 183} and exclusive:
                raise FileExistsError(str(path))
            raise self._failure("CreateFileW")
        return Win32Handle(int(value), path)

    def _supported_volume(self, path: Path) -> None:
        volume = ctypes.create_unicode_buffer(32768)
        self._truthy("GetVolumePathNameW", str(path), volume, len(volume))
        if getattr(self.kernel32, "GetDriveTypeW")(volume.value) != self.DRIVE_FIXED:
            raise ROIAnalysisError("secure Warpy transfer requires a local fixed Windows volume")
        filesystem = ctypes.create_unicode_buffer(64)
        self._truthy(
            "GetVolumeInformationW",
            volume.value,
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        )
        if filesystem.value.upper() not in {"NTFS", "REFS"}:
            raise ROIAnalysisError("secure Warpy transfer supports only local NTFS or ReFS")

    def open_directory(self, path: Path, *, create: bool) -> Win32Handle:
        absolute = Path(os.path.abspath(os.fspath(path)))
        self._supported_volume(absolute)
        pinned: list[Win32Handle] = []
        cursor = Path(absolute.anchor)
        try:
            pinned.append(self._open_raw(cursor, directory=True, create=False, exclusive=False))
            if self.is_reparse_point(pinned[0]) or not self.is_directory(pinned[0]):
                raise ROIAnalysisError("Warpy transfer volume root is not a safe directory")
            for component in absolute.parts[1:]:
                self._component(component)
                cursor /= component
                try:
                    child = self._open_raw(cursor, directory=True, create=False, exclusive=False)
                except FileNotFoundError:
                    if not create:
                        raise
                    result = getattr(self.kernel32, "CreateDirectoryW")(str(cursor), None)
                    if not result and ctypes.get_last_error() not in {80, 183}:
                        raise self._failure("CreateDirectoryW")
                    child = self._open_raw(cursor, directory=True, create=False, exclusive=False)
                try:
                    unsafe = self.is_reparse_point(child) or not self.is_directory(child)
                except BaseException:
                    self.close(child)
                    raise
                if unsafe:
                    self.close(child)
                    raise ROIAnalysisError(
                        "Warpy transfer path contains a reparse or nondirectory component"
                    )
                pinned.append(child)
            leaf = pinned.pop()
            leaf.ancestors = tuple(pinned)
            return leaf
        except Exception:
            for handle in reversed(pinned):
                self.close(handle)
            raise

    def open_child(
        self,
        directory: Win32Handle,
        name: str,
        *,
        create: bool,
        exclusive: bool,
        rename_capable: bool = False,
        share_delete: bool = False,
    ) -> tuple[Win32Handle, bool]:
        self._component(name)
        path = directory.path / name
        if create and not exclusive:
            try:
                return (
                    self._open_raw(
                        path,
                        directory=False,
                        create=True,
                        exclusive=True,
                        rename_capable=rename_capable,
                        share_delete=share_delete,
                    ),
                    True,
                )
            except FileExistsError:
                return (
                    self._open_raw(
                        path,
                        directory=False,
                        create=False,
                        exclusive=False,
                        rename_capable=rename_capable,
                        share_delete=share_delete,
                    ),
                    False,
                )
        return (
            self._open_raw(
                path,
                directory=False,
                create=create,
                exclusive=exclusive,
                rename_capable=rename_capable,
                share_delete=share_delete,
            ),
            create,
        )

    def identity(self, handle: Win32Handle) -> WindowsFileIdentity:
        info = _FILE_ID_INFO()
        self._truthy(
            "GetFileInformationByHandleEx",
            handle.value,
            self.FILE_ID_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return WindowsFileIdentity(
            int(info.VolumeSerialNumber), bytes(info.FileId.Identifier).hex()
        )

    def _standard(self, handle: Win32Handle) -> _FILE_STANDARD_INFO:
        info = _FILE_STANDARD_INFO()
        self._truthy(
            "GetFileInformationByHandleEx",
            handle.value,
            self.FILE_STANDARD_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return info

    def is_directory(self, handle: Win32Handle) -> bool:
        return bool(self._standard(handle).Directory)

    def is_regular_file(self, handle: Win32Handle) -> bool:
        return not self.is_directory(handle) and not self.is_reparse_point(handle)

    def is_reparse_point(self, handle: Win32Handle) -> bool:
        info = _FILE_ATTRIBUTE_TAG_INFO()
        self._truthy(
            "GetFileInformationByHandleEx",
            handle.value,
            self.FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return bool(info.FileAttributes & self.FILE_ATTRIBUTE_REPARSE_POINT)

    def child_identity(self, directory: Win32Handle, name: str) -> WindowsFileIdentity | None:
        try:
            child, _created = self.open_child(
                directory,
                name,
                create=False,
                exclusive=False,
                rename_capable=False,
                # A rename-capable temporary handle requests DELETE access.
                # Its no-delete-share authority remains open, so this brief
                # metadata probe must itself share DELETE to coexist without
                # making replacement possible for any other process.
                share_delete=True,
            )
        except FileNotFoundError:
            return None
        try:
            if self.is_reparse_point(child) or not self.is_regular_file(child):
                raise ROIAnalysisError("Warpy transfer child identity is unsafe")
            return self.identity(child)
        finally:
            self.close(child)

    def close(self, handle: Win32Handle) -> None:
        if handle.value:
            getattr(self.kernel32, "CloseHandle")(handle.value)
            handle.value = 0
        for ancestor in reversed(handle.ancestors):
            self.close(ancestor)
        handle.ancestors = ()

    def lock_file_ex(self, handle: Win32Handle) -> None:
        overlapped = _OVERLAPPED()
        self._truthy(
            "LockFileEx",
            handle.value,
            self.LOCKFILE_EXCLUSIVE_LOCK,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        )
        self._locks[handle.value] = overlapped

    def unlock_file_ex(self, handle: Win32Handle) -> None:
        overlapped = self._locks.pop(handle.value, None)
        if overlapped is None:
            raise ROIAnalysisError("Win32 Warpy lock was not held")
        self._truthy(
            "UnlockFileEx",
            handle.value,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        )

    def flush(self, handle: Win32Handle) -> None:
        self._truthy("FlushFileBuffers", handle.value)

    def write_all(self, handle: Win32Handle, value: bytes) -> None:
        self._truthy("SetFilePointerEx", handle.value, ctypes.c_longlong(0), None, 0)
        self._truthy("SetEndOfFile", handle.value)
        offset = 0
        while offset < len(value):
            count = wintypes.DWORD()
            chunk = value[offset : offset + 1024 * 1024]
            buffer = ctypes.create_string_buffer(chunk)
            self._truthy(
                "WriteFile",
                handle.value,
                buffer,
                len(chunk),
                ctypes.byref(count),
                None,
            )
            if count.value <= 0:
                raise ROIAnalysisError("Win32 Warpy write made no progress")
            offset += count.value

    def read_all(self, handle: Win32Handle) -> bytes:
        self._truthy("SetFilePointerEx", handle.value, ctypes.c_longlong(0), None, 0)
        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            count = wintypes.DWORD()
            self._truthy(
                "ReadFile",
                handle.value,
                buffer,
                len(buffer),
                ctypes.byref(count),
                None,
            )
            if count.value == 0:
                return b"".join(chunks)
            chunks.append(buffer.raw[: count.value])

    def publish_new(
        self,
        directory: Win32Handle,
        source_handle: Win32Handle,
        source: str,
        destination: str,
    ) -> bool:
        self._component(source)
        self._component(destination)
        if self.child_identity(directory, destination) is not None:
            return False
        try:
            self._rename_open_child(directory, source_handle, destination, replace=False)
            return True
        except OSError as exc:
            if exc.errno in {errno.EEXIST, 80, 183} or getattr(exc, "winerror", None) in {80, 183}:
                return False
            raise

    def replace_child(
        self,
        directory: Win32Handle,
        source_handle: Win32Handle,
        source: str,
        destination: str,
    ) -> None:
        self._component(source)
        self._component(destination)
        self._rename_open_child(directory, source_handle, destination, replace=True)

    def _rename_open_child(
        self,
        directory: Win32Handle,
        source_handle: Win32Handle,
        destination: str,
        *,
        replace: bool,
    ) -> None:
        self._component(destination)
        encoded = destination.encode("utf-16-le")
        offset = _FILE_RENAME_INFO_HEADER.FileName.offset
        storage = ctypes.create_string_buffer(offset + len(encoded))
        info = ctypes.cast(storage, ctypes.POINTER(_FILE_RENAME_INFO_HEADER)).contents
        info.ReplaceIfExists = bool(replace)
        info.RootDirectory = directory.value
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(storage) + offset, encoded, len(encoded))
        self._truthy(
            "SetFileInformationByHandle",
            source_handle.value,
            self.FILE_RENAME_INFO_CLASS,
            storage,
            len(storage),
        )

    def unlink_child(self, directory: Win32Handle, name: str) -> None:
        self._component(name)
        path = directory.path / name
        result = getattr(self.kernel32, "DeleteFileW")(str(path))
        if not result and ctypes.get_last_error() not in {2, 3}:
            raise self._failure("DeleteFileW")

    def discard_open_child(self, directory: Win32Handle, handle: Win32Handle, name: str) -> None:
        self._component(name)
        expected = self.child_identity(directory, name)
        if expected is None:
            return
        if expected != self.identity(handle):
            raise ROIAnalysisError("Win32 Warpy temporary-file identity changed before cleanup")
        disposition = _FILE_DISPOSITION_INFO(True)
        self._truthy(
            "SetFileInformationByHandle",
            handle.value,
            self.FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )

    def flush_directory(self, directory: Win32Handle) -> None:
        # Bytes are flushed before and after handle-based atomic rename. Windows
        # does not expose a portable directory-flush primitive; this revalidates
        # the pinned namespace. We promise process-crash atomicity, not stronger
        # power-loss durability than the local NTFS/ReFS volume provides.
        if self.is_reparse_point(directory) or not self.is_directory(directory):
            raise ROIAnalysisError("Warpy transfer directory authority changed")

    def list_children(self, directory: Win32Handle) -> tuple[str, ...]:
        return tuple(item.name for item in directory.path.iterdir())
