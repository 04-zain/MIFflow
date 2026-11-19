"""Native Win32 HANDLE authority for durable files and recovery logs.

The module is imported lazily by the shared durable-record facade. It keeps all Win32 ctypes
ownership in ``pipeline_core`` and is independently testable on non-Windows hosts through an
injected closed primitive object.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Iterator, Mapping

from .durable_records import (
    DurableRecordError,
    ReservedAuthorityLogPair,
    _ReservedAuthorityLogStream,
    _preserve_primary_failure,
    _validate_canonical_payload,
)
from .errors import ROIAnalysisError

_FILE_ID_128 = re.compile(r"^[0-9a-f]{32}$")


# ==============================================================================
#                              Win32 file authority
# ==============================================================================


class Win32FileAuthorityError(ROIAnalysisError):
    """A Win32 path or file lost its exact retained HANDLE authority."""


@dataclass(frozen=True, slots=True)
class Win32FileIdentity:
    """Full Windows volume serial plus FILE_ID_128 identity."""

    volume_serial_number: int
    file_id_128: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.volume_serial_number, int)
            or isinstance(self.volume_serial_number, bool)
            or self.volume_serial_number < 0
            or self.volume_serial_number > (1 << 64) - 1
            or not isinstance(self.file_id_128, str)
            or _FILE_ID_128.fullmatch(self.file_id_128) is None
        ):
            raise Win32FileAuthorityError("Win32 file identity is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "win32-file-identity/v1",
            "volume_serial_number": self.volume_serial_number,
            "file_id_128": self.file_id_128,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Win32FileIdentity":
        if not isinstance(value, Mapping) or set(value) != {
            "kind",
            "volume_serial_number",
            "file_id_128",
        }:
            raise Win32FileAuthorityError("Win32 file identity fields are invalid")
        if value.get("kind") != "win32-file-identity/v1":
            raise Win32FileAuthorityError("Win32 file identity kind is invalid")
        return cls(
            volume_serial_number=value.get("volume_serial_number"),  # type: ignore[arg-type]
            file_id_128=value.get("file_id_128"),  # type: ignore[arg-type]
        )


class _Win32Handle:
    __slots__ = ("ancestors", "path", "value")

    def __init__(self, value: int, path: Path) -> None:
        self.value = value
        self.path = path
        self.ancestors: tuple[_Win32Handle, ...] = ()


class Win32FilePrimitives:
    """Direct local-NTFS primitives with no reparse/delete sharing."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_LIST_DIRECTORY = 0x0001
    FILE_READ_ATTRIBUTES = 0x0080
    DELETE = 0x00010000
    SYNCHRONIZE = 0x00100000
    FILE_SHARE_READ = 0x0001
    FILE_SHARE_WRITE = 0x0002
    SHARE_READ_WRITE = FILE_SHARE_READ | FILE_SHARE_WRITE
    SHARE_DELETE = 0x0004
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_WRITE_THROUGH = 0x80000000
    FILE_STANDARD_INFO_CLASS = 1
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10
    FILE_ID_INFO_CLASS = 18
    FILE_RENAME_INFORMATION_CLASS = 10
    FILE_DISPOSITION_INFO_CLASS = 4
    DRIVE_FIXED = 3

    def __init__(self) -> None:
        if os.name != "nt":
            raise Win32FileAuthorityError("native Win32 file authority is unavailable")
        import ctypes
        from ctypes import wintypes

        class FileId128(ctypes.Structure):
            _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

        class FileIdInfo(ctypes.Structure):
            _fields_ = [
                ("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", FileId128),
            ]

        class AttributeTagInfo(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        class StandardInfo(ctypes.Structure):
            _fields_ = [
                ("AllocationSize", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wintypes.DWORD),
                ("DeletePending", wintypes.BOOLEAN),
                ("Directory", wintypes.BOOLEAN),
            ]

        class FileIdBothDirectoryInfo(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),
                ("ShortNameLength", ctypes.c_ubyte),
                ("ShortName", wintypes.WCHAR * 12),
                ("FileId", ctypes.c_longlong),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class RenameInfoHeader(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class IoStatusResult(ctypes.Union):
            _fields_ = [
                ("Status", ctypes.c_long),
                ("Pointer", ctypes.c_void_p),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [
                ("Result", IoStatusResult),
                ("Information", ctypes.c_size_t),
            ]

        class DispositionInfo(ctypes.Structure):
            _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._file_id_info_type = FileIdInfo
        self._attribute_tag_info_type = AttributeTagInfo
        self._standard_info_type = StandardInfo
        self._file_id_both_directory_info_type = FileIdBothDirectoryInfo
        self._rename_info_header_type = RenameInfoHeader
        self._io_status_block_type = IoStatusBlock
        self._disposition_info_type = DispositionInfo
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
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
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "DuplicateHandle": (
                [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                 ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                 wintypes.BOOL, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "GetFileInformationByHandleEx": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
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
            function = getattr(self._kernel32, name)
            function.argtypes = argtypes
            function.restype = restype
        self._ntdll.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(self._io_status_block_type),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        self._ntdll.NtSetInformationFile.restype = ctypes.c_long
        self._ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self._ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    @staticmethod
    def _component(name: str) -> None:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "\0" in name
            or "/" in name
            or "\\" in name
            or ":" in name
        ):
            raise Win32FileAuthorityError("Win32 child name is invalid")

    def _failure(self, operation: str) -> OSError:
        code = self._ctypes.get_last_error()
        return OSError(code, f"{operation} failed", None, code)

    def _truthy(self, operation: str, *args: object) -> object:
        result = getattr(self._kernel32, operation)(*args)
        if not result:
            raise self._failure(operation)
        return result

    def _nt_success(self, operation: str, status: int) -> None:
        if status < 0:
            code = int(self._ntdll.RtlNtStatusToDosError(status))
            raise OSError(code, f"{operation} failed", None, code)

    def _open_raw(
        self,
        path: Path,
        *,
        directory: bool,
        create: bool,
        rename_capable: bool = False,
        share_delete: bool = False,
        share_write: bool = True,
    ) -> _Win32Handle:
        if share_delete and (directory or create or rename_capable):
            raise Win32FileAuthorityError(
                "Win32 delete sharing is reserved for a mutable read target"
            )
        if type(share_write) is not bool:
            raise Win32FileAuthorityError("Win32 write-sharing policy must be explicit boolean")
        mutating_file = not directory and (create or rename_capable)
        desired_access = (
            self.FILE_LIST_DIRECTORY | self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
            if directory
            else self.GENERIC_READ
            | self.FILE_READ_ATTRIBUTES
            | (self.GENERIC_WRITE if mutating_file else 0)
            | (self.DELETE if rename_capable else 0)
        )
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= self.FILE_FLAG_BACKUP_SEMANTICS
        elif mutating_file:
            flags |= self.FILE_FLAG_WRITE_THROUGH
        value = self._kernel32.CreateFileW(
            str(path),
            desired_access,
            self.FILE_SHARE_READ
            | (self.FILE_SHARE_WRITE if share_write else 0)
            | (self.SHARE_DELETE if share_delete else 0),
            None,
            self.CREATE_NEW if create else self.OPEN_EXISTING,
            flags,
            None,
        )
        if value in (None, 0, self._invalid_handle):
            code = self._ctypes.get_last_error()
            if code in {2, 3}:
                raise FileNotFoundError(str(path))
            if code in {80, 183} and create:
                raise FileExistsError(str(path))
            raise self._failure("CreateFileW")
        return _Win32Handle(int(value), path)

    def _open_mutable_target_raw(self, path: Path) -> _Win32Handle:
        """Open one replaceable qpproj reader without changing durable opens."""

        return self._open_raw(
            path,
            directory=False,
            create=False,
            rename_capable=False,
            share_delete=True,
        )

    def _supported_volume(self, path: Path) -> None:
        volume = self._ctypes.create_unicode_buffer(32768)
        self._truthy("GetVolumePathNameW", str(path), volume, len(volume))
        if self._kernel32.GetDriveTypeW(volume.value) != self.DRIVE_FIXED:
            raise Win32FileAuthorityError("Win32 file authority requires a local fixed volume")
        filesystem = self._ctypes.create_unicode_buffer(64)
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
        if filesystem.value.upper() != "NTFS":
            raise Win32FileAuthorityError("Win32 durable file authority requires local NTFS")

    def validate_supported_volume(self, path: Path) -> None:
        self._supported_volume(Path(path))

    def _open_directory_chain(self, path: Path, *, no_follow: bool = True) -> _Win32Handle:
        if not no_follow:
            raise Win32FileAuthorityError("Win32 authority must not follow reparses")
        absolute = Path(os.path.abspath(os.fspath(path)))
        if ".." in Path(path).parts:
            raise Win32FileAuthorityError("Win32 authority path contains traversal")
        self._supported_volume(absolute)
        pinned: list[_Win32Handle] = []
        cursor = Path(absolute.anchor)
        try:
            root = self._open_raw(cursor, directory=True, create=False)
            pinned.append(root)
            if self.is_reparse_point(root) or not self.is_directory(root):
                raise Win32FileAuthorityError("Win32 volume root is unsafe")
            for component in absolute.parts[1:]:
                self._component(component)
                cursor /= component
                child = self._open_raw(cursor, directory=True, create=False)
                try:
                    if self.is_reparse_point(child) or not self.is_directory(child):
                        raise Win32FileAuthorityError(
                            "Win32 authority path traverses an unsafe component"
                        )
                except BaseException as primary:
                    try:
                        self.close(child)
                    except BaseException as cleanup:
                        _preserve_primary_failure(
                            primary,
                            cleanup,
                            context="Win32 cleanup failure",
                        )
                    raise
                pinned.append(child)
            retained = pinned.pop()
            retained.ancestors = tuple(pinned)
            return retained
        except BaseException as primary:
            for handle in reversed(pinned):
                try:
                    self.close(handle)
                except BaseException as cleanup:
                    _preserve_primary_failure(
                        primary,
                        cleanup,
                        context="Win32 cleanup failure",
                    )
            raise

    def open_directory(
        self, path: Path, *, create: bool = False, no_follow: bool = True
    ) -> _Win32Handle:
        if create:
            raise Win32FileAuthorityError(
                "direct Win32 authority creation is forbidden; provision the "
                "fixed authority layout before application mutation"
            )
        return self._open_directory_chain(path, no_follow=no_follow)

    def open_file(
        self,
        directory: _Win32Handle,
        leaf: str,
        *,
        no_follow: bool,
        share_delete: bool,
        create: bool = False,
        exclusive: bool = False,
        rename_capable: bool = False,
        share_write: bool = True,
    ) -> _Win32Handle:
        if (
            not no_follow
            or share_delete
            or type(share_write) is not bool
            or (create and not exclusive)
        ):
            raise Win32FileAuthorityError("Win32 file open policy is invalid")
        self._component(leaf)
        return self._open_raw(
            directory.path / leaf,
            directory=False,
            create=create,
            rename_capable=rename_capable,
            share_write=share_write,
        )

    def open_mutable_target_file(
        self,
        directory: _Win32Handle,
        leaf: str,
        *,
        no_follow: bool,
        share_delete: bool,
    ) -> _Win32Handle:
        """Open only a caller-designated mutable target with FILE_SHARE_DELETE."""

        if not no_follow or share_delete is not True:
            raise Win32FileAuthorityError("Win32 mutable-target open policy is invalid")
        self._component(leaf)
        return self._open_mutable_target_raw(directory.path / leaf)

    def _information(self, handle: _Win32Handle, kind: int, value: object) -> None:
        self._truthy(
            "GetFileInformationByHandleEx",
            handle.value,
            kind,
            self._ctypes.byref(value),
            self._ctypes.sizeof(value),
        )

    def _standard(self, handle: _Win32Handle) -> object:
        value = self._standard_info_type()
        self._information(handle, self.FILE_STANDARD_INFO_CLASS, value)
        return value

    def is_directory(self, handle: _Win32Handle) -> bool:
        return bool(self._standard(handle).Directory)

    def is_regular_file(self, handle: _Win32Handle) -> bool:
        return not self.is_directory(handle) and not self.is_reparse_point(handle)

    def is_reparse_point(self, handle: _Win32Handle) -> bool:
        value = self._attribute_tag_info_type()
        self._information(handle, self.FILE_ATTRIBUTE_TAG_INFO_CLASS, value)
        return bool(value.FileAttributes & self.FILE_ATTRIBUTE_REPARSE_POINT)

    def link_count(self, handle: _Win32Handle) -> int:
        return int(self._standard(handle).NumberOfLinks)

    def size(self, handle: _Win32Handle) -> int:
        return int(self._standard(handle).EndOfFile)

    def identity(self, handle: _Win32Handle) -> Win32FileIdentity:
        value = self._file_id_info_type()
        self._information(handle, self.FILE_ID_INFO_CLASS, value)
        return Win32FileIdentity(
            int(value.VolumeSerialNumber), bytes(value.FileId.Identifier).hex()
        )

    def child_identity(self, directory: _Win32Handle, leaf: str) -> Win32FileIdentity | None:
        try:
            child = self.open_file(
                directory,
                leaf,
                no_follow=True,
                share_delete=False,
            )
        except FileNotFoundError:
            return None
        primary: BaseException | None = None
        try:
            if (
                self.is_reparse_point(child)
                or not self.is_regular_file(child)
                or self.link_count(child) != 1
            ):
                raise Win32FileAuthorityError("Win32 child identity is unsafe")
            return self.identity(child)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self.close(child)
            except BaseException as cleanup:
                if primary is not None:
                    _preserve_primary_failure(
                        primary,
                        cleanup,
                        context="Win32 child identity cleanup failure",
                    )
                else:
                    raise

    def list_directory_names(
        self,
        directory: _Win32Handle,
        maximum_entries: int,
    ) -> tuple[str, ...]:
        """Enumerate one retained directory through its HANDLE, never a path."""

        if type(maximum_entries) is not int or not 1 <= maximum_entries <= 64:
            raise Win32FileAuthorityError("Win32 directory inventory bound is invalid")
        ctypes = self._ctypes
        info_type = self._file_id_both_directory_info_type
        names: list[str] = []
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            result = self._kernel32.GetFileInformationByHandleEx(
                directory.value,
                self.FILE_ID_BOTH_DIRECTORY_INFO_CLASS,
                buffer,
                len(buffer),
            )
            if not result:
                code = ctypes.get_last_error()
                if code == 18:  # ERROR_NO_MORE_FILES
                    break
                raise self._failure("GetFileInformationByHandleEx")
            offset = 0
            while True:
                info = info_type.from_buffer(buffer, offset)
                name_offset = offset + info_type.FileName.offset
                name_length = int(info.FileNameLength)
                if name_length % 2 or name_length > len(buffer) - name_offset:
                    raise Win32FileAuthorityError("Win32 directory inventory is malformed")
                raw_name = bytes(buffer[name_offset : name_offset + name_length])
                try:
                    name = raw_name.decode("utf-16-le", errors="strict")
                except UnicodeError as exc:
                    raise Win32FileAuthorityError("Win32 directory child name is invalid") from exc
                if name not in {".", ".."}:
                    self._component(name)
                    names.append(name)
                    if len(names) > maximum_entries:
                        raise Win32FileAuthorityError("Win32 directory inventory exceeds its bound")
                next_offset = int(info.NextEntryOffset)
                if next_offset == 0:
                    break
                if next_offset < info_type.FileName.offset + name_length:
                    raise Win32FileAuthorityError("Win32 directory inventory is malformed")
                offset += next_offset
                if offset >= len(buffer):
                    raise Win32FileAuthorityError("Win32 directory inventory is malformed")
        if len(set(names)) != len(names):
            raise Win32FileAuthorityError("Win32 directory inventory contains duplicate names")
        return tuple(sorted(names))

    def flush(self, handle: _Win32Handle) -> None:
        self._truthy("FlushFileBuffers", handle.value)

    def _seek(self, handle: _Win32Handle, offset: int) -> None:
        self._truthy(
            "SetFilePointerEx",
            handle.value,
            self._ctypes.c_longlong(offset),
            None,
            0,
        )

    def read_bounded(self, handle: _Win32Handle, maximum: int) -> bytes:
        return b"".join(self.iter_read_chunks(handle, maximum, 64 * 1024))

    def iter_read_chunks(
        self,
        handle: _Win32Handle,
        maximum: int,
        chunk_size: int,
    ) -> Iterator[bytes]:
        self._seek(handle, 0)
        retained = 0
        while retained < maximum:
            capacity = min(chunk_size, maximum - retained)
            buffer = self._ctypes.create_string_buffer(capacity)
            count = self._wintypes.DWORD()
            self._truthy(
                "ReadFile",
                handle.value,
                buffer,
                capacity,
                self._ctypes.byref(count),
                None,
            )
            if count.value == 0:
                break
            chunk = buffer.raw[: count.value]
            retained += count.value
            yield chunk

    def read_tail(self, handle: _Win32Handle, maximum: int) -> bytes:
        size = self.size(handle)
        self._seek(handle, max(0, size - maximum))
        chunks: list[bytes] = []
        retained = 0
        while retained < maximum:
            capacity = min(16_384, maximum - retained)
            buffer = self._ctypes.create_string_buffer(capacity)
            count = self._wintypes.DWORD()
            self._truthy(
                "ReadFile",
                handle.value,
                buffer,
                capacity,
                self._ctypes.byref(count),
                None,
            )
            if count.value == 0:
                break
            chunks.append(buffer.raw[: count.value])
            retained += count.value
        return b"".join(chunks)

    def write_all(self, handle: _Win32Handle, payload: bytes) -> None:
        self._seek(handle, 0)
        self._truthy("SetEndOfFile", handle.value)
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + 1024 * 1024]
            buffer = self._ctypes.create_string_buffer(chunk)
            count = self._wintypes.DWORD()
            self._truthy(
                "WriteFile",
                handle.value,
                buffer,
                len(chunk),
                self._ctypes.byref(count),
                None,
            )
            if count.value <= 0:
                raise Win32FileAuthorityError("Win32 write made no progress")
            offset += count.value

    def duplicate_readonly_stream(self, handle: _Win32Handle) -> object:
        """Read the same pinned bytes while retaining our original native handle.

        We duplicate the admitted read handle into a read-only CRT stream.
        Closing that stream releases only its duplicate, so the original handle
        still supports the ending identity and filename checks.
        """
        import msvcrt

        if not isinstance(handle, _Win32Handle) or not handle.value:
            raise Win32FileAuthorityError("Win32 retained read handle is invalid")
        process = self._kernel32.GetCurrentProcess()
        duplicate = self._wintypes.HANDLE()
        self._truthy(
            "DuplicateHandle", process, handle.value, process,
            self._ctypes.byref(duplicate), 0, False, 2,  # DUPLICATE_SAME_ACCESS
        )
        descriptor: int | None = None
        try:
            descriptor = msvcrt.open_osfhandle(
                duplicate.value, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
            duplicate.value = None
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
            return stream
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif duplicate.value:
                self._truthy("CloseHandle", duplicate.value)

    def open_binary_stream(self, handle: _Win32Handle) -> object:
        """Transfer one native file HANDLE into a binary CRT stream."""

        import msvcrt

        if not isinstance(handle, _Win32Handle) or not handle.value:
            raise Win32FileAuthorityError("Win32 log handle is invalid")
        descriptor: int | None = None
        try:
            descriptor = msvcrt.open_osfhandle(
                handle.value,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            handle.value = 0
            stream = os.fdopen(descriptor, "r+b")
            descriptor = None
            return stream
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def replace_child(
        self,
        directory: _Win32Handle,
        source_handle: _Win32Handle,
        source: str,
        destination: str,
    ) -> None:
        self._component(source)
        self._component(destination)
        encoded = destination.encode("utf-16-le")
        header = self._rename_info_header_type
        offset = header.FileName.offset
        storage = self._ctypes.create_string_buffer(
            max(self._ctypes.sizeof(header), offset + len(encoded))
        )
        info = self._ctypes.cast(storage, self._ctypes.POINTER(header)).contents
        info.ReplaceIfExists = True
        info.RootDirectory = directory.value
        info.FileNameLength = len(encoded)
        self._ctypes.memmove(self._ctypes.addressof(storage) + offset, encoded, len(encoded))
        io_status = self._io_status_block_type()
        status = self._ntdll.NtSetInformationFile(
            source_handle.value,
            self._ctypes.byref(io_status),
            storage,
            len(storage),
            self.FILE_RENAME_INFORMATION_CLASS,
        )
        self._nt_success("NtSetInformationFile", int(status))
        source_handle.path = directory.path / destination

    def discard_open_child(self, directory: _Win32Handle, handle: _Win32Handle, leaf: str) -> None:
        self._component(leaf)
        if handle.path != directory.path / leaf:
            raise Win32FileAuthorityError("Win32 temporary identity changed before cleanup")
        disposition = self._disposition_info_type(True)
        self._truthy(
            "SetFileInformationByHandle",
            handle.value,
            self.FILE_DISPOSITION_INFO_CLASS,
            self._ctypes.byref(disposition),
            self._ctypes.sizeof(disposition),
        )

    def close(self, handle: _Win32Handle) -> None:
        cleanup: BaseException | None = None
        if handle.value:
            if not self._kernel32.CloseHandle(handle.value):
                cleanup = self._failure("CloseHandle")
            handle.value = 0
        for ancestor in reversed(handle.ancestors):
            try:
                self.close(ancestor)
            except BaseException as exc:
                if cleanup is None:
                    cleanup = exc
                elif isinstance(cleanup, Exception) and not isinstance(exc, Exception):
                    _preserve_primary_failure(
                        exc,
                        cleanup,
                        context="earlier Win32 close failure",
                    )
                    cleanup = exc
                else:
                    _preserve_primary_failure(
                        cleanup,
                        exc,
                        context="additional ancestor close failure",
                    )
        handle.ancestors = ()
        if cleanup is not None:
            raise cleanup


@dataclass(slots=True)
class _RetainedWin32Directory:
    path: Path
    handle: object
    identity: Win32FileIdentity


@dataclass(slots=True)
class _RetainedWin32MutableFile:
    path: Path
    handle: object
    identity: Win32FileIdentity


@dataclass(slots=True)
class _RetainedWin32ReadOnlyFile:
    path: Path
    handle: object
    identity: Win32FileIdentity


class Win32FileAuthority:
    """High-level durable strategy over exact retained Win32 HANDLEs."""

    def __init__(self, *, _primitives: object | None = None) -> None:
        self._primitives = _primitives if _primitives is not None else Win32FilePrimitives()

    @staticmethod
    def _component(name: str, *, label: str) -> None:
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or "\0" in name
            or ":" in name
            or "/" in name
            or "\\" in name
        ):
            raise Win32FileAuthorityError(f"{label} filename is invalid")

    @staticmethod
    def _identity(value: object) -> Win32FileIdentity:
        if isinstance(value, Win32FileIdentity):
            return value
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
            and not isinstance(value[0], bool)
            and isinstance(value[1], str)
        ):
            return Win32FileIdentity(value[0], value[1])
        raise Win32FileAuthorityError("Win32 primitive returned an invalid identity")

    @staticmethod
    def _note(primary: BaseException, cleanup: BaseException) -> None:
        _preserve_primary_failure(
            primary,
            cleanup,
            context="Win32 file authority cleanup failure",
        )

    @staticmethod
    def _canonical_json(
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        try:
            _validate_canonical_payload(
                payload,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError as exc:
            raise Win32FileAuthorityError(str(exc)) from exc

    def _close_handle(
        self,
        handle: object,
        *,
        primary: BaseException | None,
        label: str,
    ) -> None:
        try:
            self._primitives.close(handle)
        except BaseException as cleanup:
            if primary is not None:
                if isinstance(primary, Exception) and not isinstance(cleanup, Exception):
                    self._note(cleanup, primary)
                    raise cleanup
                self._note(primary, cleanup)
            elif isinstance(cleanup, Exception):
                raise Win32FileAuthorityError(f"{label} handle could not be closed") from cleanup
            else:
                raise cleanup

    def _safe_directory(self, handle: object) -> Win32FileIdentity:
        if self._primitives.is_reparse_point(handle) or not self._primitives.is_directory(handle):
            raise Win32FileAuthorityError("Win32 retained authority is not one safe directory")
        return self._identity(self._primitives.identity(handle))

    def retain_directory(self, path: Path, *, create: bool) -> _RetainedWin32Directory:
        original = Path(path)
        if ".." in original.parts:
            raise Win32FileAuthorityError("Win32 authority path contains traversal")
        if create:
            raise Win32FileAuthorityError(
                "direct Win32 authority creation is forbidden; provision the "
                "fixed authority layout before application mutation"
            )
        handle: object | None = None
        transferred = False
        try:
            handle = self._primitives.open_directory(original, create=create, no_follow=True)
            identity = self._safe_directory(handle)
            retained = _RetainedWin32Directory(
                Path(getattr(handle, "path", original)), handle, identity
            )
            self.validate_directory(retained)
            transferred = True
            return retained
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError(
                "Win32 retained directory is unavailable or unsupported"
            ) from exc
        finally:
            if handle is not None and not transferred:
                self._close_handle(
                    handle,
                    primary=sys.exc_info()[1],
                    label="Win32 retained directory",
                )

    def retain_child_directory(
        self,
        retained: _RetainedWin32Directory,
        relative_path: Path,
        *,
        create: bool,
    ) -> _RetainedWin32Directory:
        if create:
            raise Win32FileAuthorityError(
                "direct Win32 child authority creation is forbidden; provision "
                "the fixed authority layout before application mutation"
            )
        self.validate_directory(retained)
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise Win32FileAuthorityError("Win32 child authority path is invalid")
        for component in relative.parts:
            self._component(component, label="Win32 child authority")
        child = self.retain_directory(retained.path / relative, create=create)
        try:
            self.validate_directory(retained)
            return child
        except BaseException as primary:
            try:
                self.close_directory(child)
            except BaseException as cleanup:
                self._note(primary, cleanup)
            raise

    def validate_directory(self, retained: _RetainedWin32Directory) -> None:
        if not isinstance(retained, _RetainedWin32Directory):
            raise Win32FileAuthorityError("Win32 retained directory is invalid")
        if self._safe_directory(retained.handle) != retained.identity:
            raise Win32FileAuthorityError("Win32 retained directory was replaced")
        reopened: object | None = None
        primary: BaseException | None = None
        try:
            reopened = self._primitives.open_directory(retained.path, create=False, no_follow=True)
            if self._safe_directory(reopened) != retained.identity:
                raise Win32FileAuthorityError("Win32 retained directory was replaced")
        except Win32FileAuthorityError as exc:
            primary = exc
            raise
        except Exception as exc:
            primary = exc
            raise Win32FileAuthorityError("Win32 retained directory was replaced") from exc
        finally:
            if reopened is not None:
                self._close_handle(
                    reopened,
                    primary=primary if primary is not None else sys.exc_info()[1],
                    label="Win32 directory validation",
                )

    def directory_identity(self, retained: _RetainedWin32Directory) -> Win32FileIdentity:
        self.validate_directory(retained)
        return retained.identity

    def directory_child_names(
        self,
        retained: _RetainedWin32Directory,
        *,
        maximum_entries: int,
    ) -> tuple[str, ...]:
        self.validate_directory(retained)
        try:
            names = self._primitives.list_directory_names(
                retained.handle,
                maximum_entries,
            )
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError("Win32 directory inventory is unavailable") from exc
        if (
            type(names) is not tuple
            or len(names) > maximum_entries
            or any(type(name) is not str for name in names)
            or tuple(sorted(set(names))) != names
        ):
            raise Win32FileAuthorityError("Win32 directory inventory is not closed and canonical")
        self.validate_directory(retained)
        return names

    def directory_descriptor(self, retained: _RetainedWin32Directory) -> int:
        raise Win32FileAuthorityError("Win32 retained directories are HANDLEs, not CRT descriptors")

    def synchronize_directory(self, retained: _RetainedWin32Directory) -> None:
        self.validate_directory(retained)
        raise Win32FileAuthorityError("standalone Win32 directory synchronization is unsupported")

    def remove_durable(self, retained: _RetainedWin32Directory, name: str) -> None:
        self._component(name, label="durable retirement member")
        self.validate_directory(retained)
        raise Win32FileAuthorityError(
            "Win32 physical deletion cannot certify power-loss-durable absence; "
            "publish an immutable logical retirement record"
        )

    def _member_path(self, retained: _RetainedWin32Directory, name: str) -> Path:
        self._component(name, label="authority member")
        self.validate_directory(retained)
        return retained.path / name

    def reserve_log_pair(
        self,
        retained: _RetainedWin32Directory,
        stdout_name: str,
        stderr_name: str,
    ) -> ReservedAuthorityLogPair:
        self._component(stdout_name, label="stdout log")
        self._component(stderr_name, label="stderr log")
        if stdout_name == stderr_name:
            raise Win32FileAuthorityError("authority log members must be distinct")
        self.validate_directory(retained)
        streams: list[_ReservedAuthorityLogStream] = []
        try:
            for name in (stdout_name, stderr_name):
                handle: object | None = None
                try:
                    handle = self._primitives.open_file(
                        retained.handle,
                        name,
                        no_follow=True,
                        share_delete=False,
                        create=True,
                        exclusive=True,
                        rename_capable=False,
                    )
                    if (
                        self._primitives.is_reparse_point(handle)
                        or not self._primitives.is_regular_file(handle)
                        or self._primitives.link_count(handle) != 1
                    ):
                        raise Win32FileAuthorityError(
                            "authority log member is not one safe regular file"
                        )
                    stream = self._primitives.open_binary_stream(handle)
                    handle = None

                    @contextmanager
                    def same_stream_reader(stream: object = stream):
                        stream.seek(0)  # type: ignore[attr-defined]
                        yield stream

                    streams.append(_ReservedAuthorityLogStream(stream, same_stream_reader))
                finally:
                    if handle is not None:
                        self._close_handle(
                            handle,
                            primary=sys.exc_info()[1],
                            label="authority log",
                        )
            self.validate_directory(retained)
            return ReservedAuthorityLogPair(stdout=streams[0], stderr=streams[1])
        except BaseException as primary:
            for stream in reversed(streams):
                try:
                    stream.close()
                except BaseException as cleanup:
                    self._note(primary, cleanup)
            raise

    def close_directory(self, retained: _RetainedWin32Directory) -> None:
        if not isinstance(retained, _RetainedWin32Directory):
            raise Win32FileAuthorityError("Win32 retained directory is invalid")
        self._primitives.close(retained.handle)

    def retain_mutable_target(
        self,
        retained: _RetainedWin32Directory,
        name: str,
    ) -> _RetainedWin32MutableFile:
        """Retain one replaceable qpproj HANDLE; durable members never use this."""

        self._component(name, label="mutable project target")
        self.validate_directory(retained)
        handle: object | None = None
        transferred = False
        try:
            handle = self._primitives.open_mutable_target_file(
                retained.handle,
                name,
                no_follow=True,
                share_delete=True,
            )
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError("mutable project target is not one safe regular file")
            identity = self._identity(self._primitives.identity(handle))
            result = _RetainedWin32MutableFile(
                retained.path / name,
                handle,
                identity,
            )
            self.validate_mutable_target_name(retained, name, result)
            transferred = True
            return result
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError("mutable project target cannot be retained") from exc
        finally:
            if handle is not None and not transferred:
                self._close_handle(
                    handle,
                    primary=sys.exc_info()[1],
                    label="mutable project target",
                )

    def mutable_target_identity(
        self,
        retained: _RetainedWin32MutableFile,
        *,
        allow_unlinked_predecessor: bool,
    ) -> Win32FileIdentity:
        if not isinstance(retained, _RetainedWin32MutableFile):
            raise Win32FileAuthorityError("mutable project target pin is invalid")
        links = self._primitives.link_count(retained.handle)
        if (
            self._primitives.is_reparse_point(retained.handle)
            or not self._primitives.is_regular_file(retained.handle)
            or links not in ((0, 1) if allow_unlinked_predecessor else (1,))
            or self._identity(self._primitives.identity(retained.handle)) != retained.identity
        ):
            raise Win32FileAuthorityError("mutable project target pin changed")
        return retained.identity

    def validate_mutable_target_name(
        self,
        directory: _RetainedWin32Directory,
        name: str,
        retained: _RetainedWin32MutableFile,
    ) -> None:
        self._component(name, label="mutable project target")
        self.validate_directory(directory)
        reopened: object | None = None
        try:
            reopened = self._primitives.open_mutable_target_file(
                directory.handle,
                name,
                no_follow=True,
                share_delete=True,
            )
            if (
                self._primitives.is_reparse_point(reopened)
                or not self._primitives.is_regular_file(reopened)
                or self._primitives.link_count(reopened) != 1
                or self._identity(self._primitives.identity(reopened)) != retained.identity
            ):
                raise Win32FileAuthorityError("mutable project target name changed")
        finally:
            if reopened is not None:
                self._close_handle(
                    reopened,
                    primary=sys.exc_info()[1],
                    label="mutable project target validation",
                )

    def hash_mutable_target(
        self,
        retained: _RetainedWin32MutableFile,
    ) -> str:
        identity = self.mutable_target_identity(
            retained,
            allow_unlinked_predecessor=False,
        )
        size = self._primitives.size(retained.handle)
        if type(size) is not int or size < 0:
            raise Win32FileAuthorityError("mutable project target size is invalid")
        digest = hashlib.sha256()
        retained_bytes = 0
        for chunk in self._primitives.iter_read_chunks(
            retained.handle,
            size + 1,
            1024 * 1024,
        ):
            if not isinstance(chunk, bytes) or not chunk:
                raise Win32FileAuthorityError("mutable project target read is invalid")
            retained_bytes += len(chunk)
            digest.update(chunk)
        if (
            retained_bytes != size
            or self._primitives.size(retained.handle) != size
            or self.mutable_target_identity(
                retained,
                allow_unlinked_predecessor=False,
            )
            != identity
        ):
            raise Win32FileAuthorityError("mutable project target changed during read")
        return digest.hexdigest()

    def read_mutable_target(
        self,
        retained: _RetainedWin32MutableFile,
    ) -> bytes:
        """Read one qpproj from its exact retained mutable-target HANDLE."""

        identity = self.mutable_target_identity(
            retained,
            allow_unlinked_predecessor=False,
        )
        size = self._primitives.size(retained.handle)
        if type(size) is not int or size < 0:
            raise Win32FileAuthorityError("mutable project target size is invalid")
        chunks: list[bytes] = []
        retained_bytes = 0
        for chunk in self._primitives.iter_read_chunks(
            retained.handle,
            size + 1,
            1024 * 1024,
        ):
            if not isinstance(chunk, bytes) or not chunk:
                raise Win32FileAuthorityError("mutable project target read is invalid")
            retained_bytes += len(chunk)
            if retained_bytes > size:
                raise Win32FileAuthorityError("mutable project target changed during read")
            chunks.append(chunk)
        if (
            retained_bytes != size
            or self._primitives.size(retained.handle) != size
            or self.mutable_target_identity(
                retained,
                allow_unlinked_predecessor=False,
            )
            != identity
        ):
            raise Win32FileAuthorityError("mutable project target changed during read")
        return b"".join(chunks)

    def close_mutable_target(
        self,
        retained: _RetainedWin32MutableFile,
    ) -> None:
        if not isinstance(retained, _RetainedWin32MutableFile):
            raise Win32FileAuthorityError("mutable project target pin is invalid")
        self._primitives.close(retained.handle)

    def retain_read_only_target(
        self,
        retained: _RetainedWin32Directory,
        name: str,
    ) -> _RetainedWin32ReadOnlyFile:
        """Retain one read-only file while denying write and delete sharing."""

        self._component(name, label="read-only project target")
        self.validate_directory(retained)
        handle: object | None = None
        transferred = False
        try:
            handle = self._primitives.open_file(
                retained.handle,
                name,
                no_follow=True,
                share_delete=False,
                share_write=False,
            )
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError(
                    "read-only project target is not one safe regular file"
                )
            identity = self._identity(self._primitives.identity(handle))
            result = _RetainedWin32ReadOnlyFile(
                retained.path / name,
                handle,
                identity,
            )
            self.validate_read_only_target_name(retained, name, result)
            transferred = True
            return result
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError("read-only project target cannot be retained") from exc
        finally:
            if handle is not None and not transferred:
                self._close_handle(
                    handle,
                    primary=sys.exc_info()[1],
                    label="read-only project target",
                )

    def read_only_target_identity(
        self,
        retained: _RetainedWin32ReadOnlyFile,
    ) -> Win32FileIdentity:
        if not isinstance(retained, _RetainedWin32ReadOnlyFile):
            raise Win32FileAuthorityError("read-only project target pin is invalid")
        if (
            self._primitives.is_reparse_point(retained.handle)
            or not self._primitives.is_regular_file(retained.handle)
            or self._primitives.link_count(retained.handle) != 1
            or self._identity(self._primitives.identity(retained.handle)) != retained.identity
        ):
            raise Win32FileAuthorityError("read-only project target pin changed")
        return retained.identity

    def validate_read_only_target_name(
        self,
        directory: _RetainedWin32Directory,
        name: str,
        retained: _RetainedWin32ReadOnlyFile,
    ) -> None:
        self._component(name, label="read-only project target")
        self.validate_directory(directory)
        self.read_only_target_identity(retained)
        reopened: object | None = None
        try:
            reopened = self._primitives.open_file(
                directory.handle,
                name,
                no_follow=True,
                share_delete=False,
                share_write=False,
            )
            if (
                self._primitives.is_reparse_point(reopened)
                or not self._primitives.is_regular_file(reopened)
                or self._primitives.link_count(reopened) != 1
                or self._identity(self._primitives.identity(reopened)) != retained.identity
            ):
                raise Win32FileAuthorityError("read-only project target name changed")
        finally:
            if reopened is not None:
                self._close_handle(
                    reopened,
                    primary=sys.exc_info()[1],
                    label="read-only project target validation",
                )

    def read_read_only_target(
        self,
        retained: _RetainedWin32ReadOnlyFile,
        *,
        maximum_bytes: int,
    ) -> bytes:
        """Read bounded bytes from the exact non-write-shared retained HANDLE."""

        identity = self.read_only_target_identity(retained)
        size = self._primitives.size(retained.handle)
        if (
            type(maximum_bytes) is not int
            or maximum_bytes < 1
            or type(size) is not int
            or size < 0
            or size > maximum_bytes
        ):
            raise Win32FileAuthorityError("read-only project target exceeds its read bound")
        chunks: list[bytes] = []
        retained_bytes = 0
        for chunk in self._primitives.iter_read_chunks(
            retained.handle,
            size + 1,
            1024 * 1024,
        ):
            if not isinstance(chunk, bytes) or not chunk:
                raise Win32FileAuthorityError("read-only project target read is invalid")
            retained_bytes += len(chunk)
            if retained_bytes > size:
                raise Win32FileAuthorityError("read-only project target changed during read")
            chunks.append(chunk)
        if (
            retained_bytes != size
            or self._primitives.size(retained.handle) != size
            or self.read_only_target_identity(retained) != identity
        ):
            raise Win32FileAuthorityError("read-only project target changed during read")
        return b"".join(chunks)

    def close_read_only_target(
        self,
        retained: _RetainedWin32ReadOnlyFile,
    ) -> None:
        if not isinstance(retained, _RetainedWin32ReadOnlyFile):
            raise Win32FileAuthorityError("read-only project target pin is invalid")
        self._primitives.close(retained.handle)

    def _open_safe_file(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        *,
        label: str,
        delete_capable: bool = False,
    ) -> tuple[object, Win32FileIdentity, int]:
        self._component(name, label=label)
        self.validate_directory(retained)
        handle = self._primitives.open_file(
            retained.handle,
            name,
            no_follow=True,
            share_delete=False,
            rename_capable=delete_capable,
        )
        try:
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError(f"{label} is not one safe regular file")
            identity = self._identity(self._primitives.identity(handle))
            size = self._primitives.size(handle)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise Win32FileAuthorityError(f"{label} size is invalid")
            return handle, identity, size
        except BaseException as primary:
            try:
                self._primitives.close(handle)
            except BaseException as cleanup:
                self._note(primary, cleanup)
            raise

    def read_immutable(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        handle: object | None = None
        try:
            try:
                handle, identity, size = self._open_safe_file(retained, name, label=label)
            except FileNotFoundError:
                if optional:
                    self.validate_directory(retained)
                    return None
                raise Win32FileAuthorityError(f"{label} is unavailable")
            if size > max_bytes:
                raise Win32FileAuthorityError(f"{label} exceeds its {max_bytes}-byte maximum")
            payload = self._primitives.read_bounded(handle, max_bytes + 1)
            if not isinstance(payload, bytes):
                raise Win32FileAuthorityError(f"{label} payload is invalid")
            current_size = self._primitives.size(handle)
            if (
                len(payload) > max_bytes
                or len(payload) != size
                or current_size != size
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._identity(self._primitives.identity(handle)) != identity
                or self._identity(self._primitives.child_identity(retained.handle, name))
                != identity
            ):
                raise Win32FileAuthorityError(f"{label} changed during bounded read")
            self.validate_directory(retained)
            return payload
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError(f"{label} cannot be read safely") from exc
        finally:
            if handle is not None:
                self._close_handle(
                    handle,
                    primary=sys.exc_info()[1],
                    label=label,
                )

    def read_bytes(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        return self.read_immutable(
            retained,
            name,
            max_bytes=max_bytes,
            label=label,
            optional=optional,
        )

    def publish_immutable(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self._component(name, label=label)
        if len(payload) > max_bytes:
            raise Win32FileAuthorityError(f"{label} exceeds its {max_bytes}-byte maximum")
        self.validate_directory(retained)
        handle: object | None = None
        try:
            try:
                handle = self._primitives.open_file(
                    retained.handle,
                    name,
                    no_follow=True,
                    share_delete=False,
                    create=True,
                    exclusive=True,
                    rename_capable=False,
                )
            except FileExistsError:
                existing = self.read_immutable(retained, name, max_bytes=max_bytes, label=label)
                if existing != payload:
                    raise Win32FileAuthorityError(f"{label} immutable bytes conflict")
                return
            identity = self._identity(self._primitives.identity(handle))
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError(f"{label} created an unsafe immutable file")
            self._primitives.write_all(handle, payload)
            self._primitives.flush(handle)
            if (
                self._primitives.size(handle) != len(payload)
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._identity(self._primitives.identity(handle)) != identity
                or self._identity(self._primitives.child_identity(retained.handle, name))
                != identity
            ):
                raise Win32FileAuthorityError(f"{label} immutable bytes changed")
            created_handle = handle
            handle = None
            self._primitives.close(created_handle)
            reopened = self.read_immutable(retained, name, max_bytes=max_bytes, label=label)
            if reopened != payload:
                raise Win32FileAuthorityError(f"{label} immutable bytes changed")
            self.validate_directory(retained)
        except Win32FileAuthorityError:
            raise
        except Exception as exc:
            raise Win32FileAuthorityError(f"{label} cannot be published safely") from exc
        finally:
            if handle is not None:
                self._close_handle(
                    handle,
                    primary=sys.exc_info()[1],
                    label=label,
                )

    def publish_bytes(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self.publish_immutable(
            retained,
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
        )

    def replace_bytes(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        """Atomically replace bounded arbitrary bytes under one retained handle.

        The file data is flushed and revalidated exactly.  Windows does not
        expose the directory-metadata synchronization used by the POSIX
        authority, so this operation claims atomic visibility but not
        standalone power-loss durability of the directory entry.
        """

        self._component(name, label=label)
        if not isinstance(payload, bytes) or len(payload) > max_bytes:
            raise Win32FileAuthorityError(f"{label} exceeds its {max_bytes}-byte maximum")
        self.validate_directory(retained)
        handle, temporary_name = self._new_temporary(retained, name)
        replaced = False
        active: BaseException | None = None
        try:
            identity = self._identity(self._primitives.identity(handle))
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError(f"{label} temporary is unsafe")
            self._primitives.write_all(handle, payload)
            self._primitives.flush(handle)
            if (
                self._primitives.read_bounded(handle, max_bytes + 1) != payload
                or self._primitives.size(handle) != len(payload)
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._identity(self._primitives.identity(handle)) != identity
            ):
                raise Win32FileAuthorityError(f"{label} temporary changed")
            self.validate_directory(retained)
            self._primitives.replace_child(retained.handle, handle, temporary_name, name)
            replaced = True
            published = handle
            handle = None
            self._primitives.close(published)
            reopened: object | None = None
            try:
                reopened, reopened_identity, reopened_size = self._open_safe_file(
                    retained, name, label=label
                )
                reopened_payload = self._primitives.read_bounded(reopened, max_bytes + 1)
                if (
                    reopened_identity != identity
                    or reopened_size != len(payload)
                    or reopened_payload != payload
                    or self._primitives.size(reopened) != len(payload)
                    or self._primitives.is_reparse_point(reopened)
                    or not self._primitives.is_regular_file(reopened)
                    or self._primitives.link_count(reopened) != 1
                    or self._identity(self._primitives.identity(reopened)) != identity
                    or self._identity(self._primitives.child_identity(retained.handle, name))
                    != identity
                ):
                    raise Win32FileAuthorityError(f"{label} identity or bytes changed")
                self.validate_directory(retained)
            finally:
                if reopened is not None:
                    self._close_handle(
                        reopened,
                        primary=sys.exc_info()[1],
                        label=label,
                    )
        except BaseException as exc:
            if isinstance(exc, Win32FileAuthorityError):
                active = exc
                raise
            if isinstance(exc, Exception):
                failure = Win32FileAuthorityError(f"{label} cannot be replaced safely")
                active = failure
                raise failure from exc
            active = exc
            raise
        finally:
            if handle is not None:
                if replaced:
                    self._close_handle(handle, primary=active, label=label)
                else:
                    self._discard_and_close(
                        retained,
                        handle,
                        temporary_name,
                        primary=active,
                    )

    def _new_temporary(
        self, retained: _RetainedWin32Directory, pointer_name: str
    ) -> tuple[object, str]:
        for _ in range(100):
            name = f".{pointer_name}.{secrets.token_hex(16)}"
            try:
                return (
                    self._primitives.open_file(
                        retained.handle,
                        name,
                        no_follow=True,
                        share_delete=False,
                        create=True,
                        exclusive=True,
                        rename_capable=True,
                    ),
                    name,
                )
            except FileExistsError:
                continue
        raise Win32FileAuthorityError("Win32 temporary name space is exhausted")

    def _discard_and_close(
        self,
        retained: _RetainedWin32Directory,
        handle: object,
        name: str,
        *,
        primary: BaseException | None,
    ) -> None:
        cleanup: BaseException | None = None
        try:
            self._primitives.discard_open_child(retained.handle, handle, name)
        except BaseException as exc:
            cleanup = exc
        try:
            self._primitives.close(handle)
        except BaseException as exc:
            if cleanup is None:
                cleanup = exc
            elif isinstance(cleanup, Exception) and not isinstance(exc, Exception):
                self._note(exc, cleanup)
                cleanup = exc
            else:
                _preserve_primary_failure(
                    cleanup,
                    exc,
                    context="additional Win32 close failure",
                )
        if cleanup is not None:
            if primary is not None:
                if isinstance(primary, Exception) and not isinstance(cleanup, Exception):
                    self._note(cleanup, primary)
                    raise cleanup
                self._note(primary, cleanup)
            elif isinstance(cleanup, Exception):
                raise Win32FileAuthorityError(
                    "Win32 temporary could not be removed safely"
                ) from cleanup
            else:
                raise cleanup

    def _rollback_pointer(
        self,
        retained: _RetainedWin32Directory,
        pointer_name: str,
        predecessor: bytes | None,
        *,
        label: str,
    ) -> None:
        if predecessor is None:
            handle, _identity, _size = self._open_safe_file(
                retained,
                pointer_name,
                label=label,
                delete_capable=True,
            )
            self._discard_and_close(retained, handle, pointer_name, primary=None)
            if (
                self.read_immutable(
                    retained,
                    pointer_name,
                    max_bytes=1,
                    label=label,
                    optional=True,
                )
                is not None
            ):
                raise Win32FileAuthorityError(f"{label} unsafe publication could not be retired")
            raise Win32FileAuthorityError(
                f"{label} rollback observed absence but cannot certify "
                "power-loss-durable physical deletion"
            )
        handle, name = self._new_temporary(retained, pointer_name)
        active: BaseException | None = None
        replaced = False
        try:
            identity = self._identity(self._primitives.identity(handle))
            if (
                self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
            ):
                raise Win32FileAuthorityError(f"{label} rollback temporary is unsafe")
            self._primitives.write_all(handle, predecessor)
            self._primitives.flush(handle)
            if (
                self._primitives.size(handle) != len(predecessor)
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._identity(self._primitives.identity(handle)) != identity
            ):
                raise Win32FileAuthorityError(f"{label} rollback temporary changed")
            self._primitives.replace_child(retained.handle, handle, name, pointer_name)
            replaced = True
            published = handle
            handle = None
            self._primitives.close(published)
        except BaseException as exc:
            active = exc
            if isinstance(exc, Exception):
                raise Win32FileAuthorityError(
                    f"{label} unsafe publication could not be rolled back"
                ) from exc
            raise
        finally:
            if handle is None:
                pass
            elif not replaced:
                self._discard_and_close(retained, handle, name, primary=active)
            else:
                self._close_handle(
                    handle,
                    primary=active,
                    label=f"{label} rollback",
                )
        reopened: object | None = None
        try:
            reopened, reopened_identity, reopened_size = self._open_safe_file(
                retained, pointer_name, label=label
            )
            reopened_payload = self._primitives.read_bounded(reopened, max(len(predecessor), 1) + 1)
            if (
                reopened_identity != identity
                or reopened_size != len(predecessor)
                or reopened_payload != predecessor
                or self._primitives.size(reopened) != len(predecessor)
                or self._identity(self._primitives.child_identity(retained.handle, pointer_name))
                != identity
            ):
                raise Win32FileAuthorityError(
                    f"{label} unsafe publication could not be rolled back"
                )
            self.validate_directory(retained)
        finally:
            if reopened is not None:
                self._close_handle(
                    reopened,
                    primary=sys.exc_info()[1],
                    label=f"{label} rollback",
                )

    def replace_pointer_cas(
        self,
        pointer_retained: _RetainedWin32Directory,
        pointer_name: str,
        immutable_retained: _RetainedWin32Directory,
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes:
        self._component(pointer_name, label=label)
        self._component(immutable_name, label=f"{label} immutable generation")
        immutable = self.read_immutable(
            immutable_retained,
            immutable_name,
            max_bytes=max_bytes,
            label=f"{label} immutable generation",
        )
        assert immutable is not None
        self._canonical_json(
            immutable,
            max_bytes=max_bytes,
            label=f"{label} immutable generation",
        )
        if expected_predecessor_bytes is not None:
            self._canonical_json(
                expected_predecessor_bytes,
                max_bytes=max_bytes,
                label=f"{label} expected predecessor",
            )
        observed = self.read_immutable(
            pointer_retained,
            pointer_name,
            max_bytes=max_bytes,
            label=label,
            optional=True,
        )
        if observed is not None:
            self._canonical_json(
                observed,
                max_bytes=max_bytes,
                label=label,
            )
        if observed != expected_predecessor_bytes:
            raise Win32FileAuthorityError(f"{label} predecessor bytes changed")
        temporary, temporary_name = self._new_temporary(pointer_retained, pointer_name)
        replaced = False
        active: BaseException | None = None
        try:
            temporary_identity = self._identity(self._primitives.identity(temporary))
            if (
                self._primitives.is_reparse_point(temporary)
                or not self._primitives.is_regular_file(temporary)
                or self._primitives.link_count(temporary) != 1
            ):
                raise Win32FileAuthorityError(f"{label} temporary is unsafe")
            self._primitives.write_all(temporary, immutable)
            self._primitives.flush(temporary)
            temporary_bytes = self._primitives.read_bounded(temporary, max_bytes + 1)
            if (
                temporary_bytes != immutable
                or self._primitives.size(temporary) != len(immutable)
                or self._primitives.is_reparse_point(temporary)
                or not self._primitives.is_regular_file(temporary)
                or self._primitives.link_count(temporary) != 1
                or self._identity(self._primitives.identity(temporary)) != temporary_identity
            ):
                raise Win32FileAuthorityError(
                    f"{label} temporary differs from immutable generation"
                )
            rechecked_predecessor = self.read_immutable(
                pointer_retained,
                pointer_name,
                max_bytes=max_bytes,
                label=label,
                optional=True,
            )
            if rechecked_predecessor is not None:
                self._canonical_json(
                    rechecked_predecessor,
                    max_bytes=max_bytes,
                    label=label,
                )
            if rechecked_predecessor != expected_predecessor_bytes:
                raise Win32FileAuthorityError(f"{label} predecessor bytes changed")
            self.validate_directory(pointer_retained)
            self.validate_directory(immutable_retained)
            self._primitives.replace_child(
                pointer_retained.handle,
                temporary,
                temporary_name,
                pointer_name,
            )
            replaced = True
            temporary_name = pointer_name
            published_handle = temporary
            temporary = None
            self._primitives.close(published_handle)
            reopened: object | None = None
            try:
                reopened, reopened_identity, reopened_size = self._open_safe_file(
                    pointer_retained, pointer_name, label=label
                )
                current = self._primitives.read_bounded(reopened, max_bytes + 1)
                if (
                    reopened_identity != temporary_identity
                    or reopened_size != len(immutable)
                    or current != immutable
                    or self._primitives.size(reopened) != len(immutable)
                    or self._identity(
                        self._primitives.child_identity(pointer_retained.handle, pointer_name)
                    )
                    != temporary_identity
                ):
                    raise Win32FileAuthorityError(
                        f"{label} changed after handle-relative replacement"
                    )
                self.validate_directory(pointer_retained)
            finally:
                if reopened is not None:
                    self._close_handle(
                        reopened,
                        primary=sys.exc_info()[1],
                        label=label,
                    )
            immutable_after = self.read_immutable(
                immutable_retained,
                immutable_name,
                max_bytes=max_bytes,
                label=f"{label} immutable generation",
            )
            if current != immutable or immutable_after != immutable:
                raise Win32FileAuthorityError(f"{label} is not an exact immutable-generation copy")
            return current
        except BaseException as exc:
            active = exc
            if replaced:
                if temporary is not None:
                    replaced_handle = temporary
                    temporary = None
                    self._close_handle(
                        replaced_handle,
                        primary=exc,
                        label=label,
                    )
                try:
                    self._rollback_pointer(
                        pointer_retained,
                        pointer_name,
                        expected_predecessor_bytes,
                        label=label,
                    )
                except BaseException as rollback:
                    if not isinstance(rollback, Exception):
                        self._note(rollback, exc)
                        raise
                    if not isinstance(exc, Exception):
                        self._note(exc, rollback)
                        raise exc
                    if isinstance(rollback, Win32FileAuthorityError):
                        raise rollback from exc
                    raise Win32FileAuthorityError(
                        f"{label} unsafe publication could not be rolled back"
                    ) from exc
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, Win32FileAuthorityError):
                raise
            raise Win32FileAuthorityError(f"{label} could not be replaced atomically") from exc
        finally:
            if temporary is not None:
                if replaced:
                    try:
                        self._primitives.close(temporary)
                    except BaseException as cleanup:
                        if active is not None:
                            self._note(active, cleanup)
                        else:
                            raise Win32FileAuthorityError(
                                f"{label} temporary could not be closed"
                            ) from cleanup
                else:
                    self._discard_and_close(
                        pointer_retained,
                        temporary,
                        temporary_name,
                        primary=active,
                    )

    def read_tail(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        *,
        maximum: int,
        label: str,
    ) -> bytes:
        handle: object | None = None
        primary: BaseException | None = None
        try:
            handle, identity, size = self._open_safe_file(retained, name, label=label)
            raw = self._primitives.read_tail(handle, maximum)
            if (
                not isinstance(raw, bytes)
                or len(raw) > maximum
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._primitives.size(handle) != size
                or self._identity(self._primitives.identity(handle)) != identity
                or self._identity(self._primitives.child_identity(retained.handle, name))
                != identity
            ):
                raise Win32FileAuthorityError(f"{label} changed during tail read")
            return raw
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if handle is not None:
                self._close_handle(
                    handle,
                    primary=primary,
                    label=label,
                )

    def sha256(
        self,
        retained: _RetainedWin32Directory,
        name: str,
        *,
        max_bytes: int,
        label: str,
    ) -> str:
        handle: object | None = None
        primary: BaseException | None = None
        try:
            handle, identity, size = self._open_safe_file(retained, name, label=label)
            if size > max_bytes:
                raise Win32FileAuthorityError(f"{label} exceeds its {max_bytes}-byte maximum")
            digest = hashlib.sha256()
            retained_bytes = 0
            for chunk in self._primitives.iter_read_chunks(handle, max_bytes + 1, 64 * 1024):
                if not isinstance(chunk, bytes) or not chunk or len(chunk) > 64 * 1024:
                    raise Win32FileAuthorityError(f"{label} streaming payload is invalid")
                retained_bytes += len(chunk)
                if retained_bytes > max_bytes:
                    raise Win32FileAuthorityError(f"{label} exceeds its {max_bytes}-byte maximum")
                digest.update(chunk)
            if (
                retained_bytes != size
                or self._primitives.is_reparse_point(handle)
                or not self._primitives.is_regular_file(handle)
                or self._primitives.link_count(handle) != 1
                or self._primitives.size(handle) != size
                or self._identity(self._primitives.identity(handle)) != identity
                or self._identity(self._primitives.child_identity(retained.handle, name))
                != identity
            ):
                raise Win32FileAuthorityError(f"{label} changed during streaming hash")
            self.validate_directory(retained)
            return digest.hexdigest()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if handle is not None:
                self._close_handle(handle, primary=primary, label=label)


__all__ = [
    "Win32FileAuthority",
    "Win32FileAuthorityError",
    "Win32FileIdentity",
    "Win32FilePrimitives",
]
