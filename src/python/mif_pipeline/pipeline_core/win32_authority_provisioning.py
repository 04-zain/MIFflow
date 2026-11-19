"""Fixed-LocalAppData Win32 setup and create-disabled authority open.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
from dataclasses import dataclass

from .authority_layout import (
    AUTHORITY_BUCKET_NAMES_V2,
    AUTHORITY_LAYOUT_MARKER_NAME_V2,
    AUTHORITY_LAYOUT_MAX_BYTES_V2,
    ApplicationAuthorityLayoutV2,
    AuthorityBucketV2,
    Win32AuthorityProvisioningV2,
)
from .authority_provisioning import (
    AuthorityProvisioningRequiredError,
    RetainedAuthorityLayoutV2,
    _merge_cleanup_failure,
)
from .win32_file_authority import (
    Win32FileAuthority,
    Win32FileAuthorityError,
    Win32FileIdentity,
    Win32FilePrimitives,
)

_EXPECTED_BUCKETS = ("immutable", "mutable", "locks", "logs", "exchange")


# ==============================================================================
#                          Win32 authority provisioning
# ==============================================================================


class _NativeWin32DirectoryAuthority:
    def __init__(self, strategy: Win32FileAuthority, retained: object, path: Path):
        self._strategy = strategy
        self._retained: object | None = retained
        self.path = path
        try:
            self.identity = strategy.directory_identity(retained)  # type: ignore[arg-type]
        except BaseException as exc:
            try:
                strategy.close_directory(retained)  # type: ignore[arg-type]
            except BaseException as cleanup:
                failure = _merge_cleanup_failure(exc, cleanup)
                if failure is not exc:
                    raise failure from exc
            raise

    def _require(self) -> object:
        if self._retained is None:
            raise Win32FileAuthorityError("Win32 retained directory is closed")
        return self._retained

    def validate(self) -> None:
        self._strategy.validate_directory(self._require())  # type: ignore[arg-type]

    def read_immutable(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        return self._strategy.read_immutable(
            self._require(),  # type: ignore[arg-type]
            name,
            max_bytes=max_bytes,
            label=label,
            optional=optional,
        )

    def publish_immutable(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self._strategy.publish_immutable(
            self._require(),  # type: ignore[arg-type]
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
        )

    def read_bytes(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        return self._strategy.read_bytes(
            self._require(),  # type: ignore[arg-type]
            name,
            max_bytes=max_bytes,
            label=label,
            optional=optional,
        )

    def publish_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self._strategy.publish_bytes(
            self._require(),  # type: ignore[arg-type]
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
        )

    def _member_path(self, name: str) -> Path:
        return self._strategy._member_path(self._require(), name)  # type: ignore[arg-type]

    def reserve_log_pair(self, stdout_name: str, stderr_name: str):
        return self._strategy.reserve_log_pair(
            self._require(),  # type: ignore[arg-type]
            stdout_name,
            stderr_name,
        )

    def replace_pointer_cas(
        self,
        pointer_name: str,
        immutable_authority: "_NativeWin32DirectoryAuthority",
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes:
        if (
            not isinstance(immutable_authority, _NativeWin32DirectoryAuthority)
            or immutable_authority._strategy is not self._strategy
        ):
            raise Win32FileAuthorityError(f"{label} retained Win32 strategies do not match")
        return self._strategy.replace_pointer_cas(
            self._require(),  # type: ignore[arg-type]
            pointer_name,
            immutable_authority._require(),  # type: ignore[arg-type]
            immutable_name,
            expected_predecessor_bytes=expected_predecessor_bytes,
            max_bytes=max_bytes,
            label=label,
        )

    def remove_durable(self, name: str) -> None:
        self._strategy.remove_durable(
            self._require(),  # type: ignore[arg-type]
            name,
        )

    def child_names(self, *, maximum_entries: int) -> tuple[str, ...]:
        return self._strategy.directory_child_names(
            self._require(),  # type: ignore[arg-type]
            maximum_entries=maximum_entries,
        )

    def close(self) -> None:
        retained = self._retained
        if retained is None:
            return
        self._retained = None
        self._strategy.close_directory(retained)  # type: ignore[arg-type]


class _CurrentUserDacl:
    """Apply and reopen one exact protected current-user-only DACL."""

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SDDL_REVISION_1 = 1

    def __init__(self, *, _native: object | None = None) -> None:
        self._native = _native
        if _native is not None:
            return
        import ctypes
        from ctypes import wintypes

        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise Win32FileAuthorityError("native Windows DACL authority is unavailable")
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        signatures = {
            "OpenProcessToken": (
                [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
                wintypes.BOOL,
            ),
            "GetTokenInformation": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "ConvertSidToStringSidW": (
                [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)],
                wintypes.BOOL,
            ),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.LPWSTR),
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "SetFileSecurityW": (
                [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "GetFileSecurityW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self._advapi32, name)
            function.argtypes = argtypes
            function.restype = restype
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _failure(self, operation: str) -> OSError:
        if self._native is not None:
            return OSError(f"{operation} failed")
        code = self._ctypes.get_last_error()
        return OSError(code, f"{operation} failed", None, code)

    def _free_local(self, allocation: object, operation: str) -> None:
        if self._native is not None:
            result = self._native.local_free(allocation)
        else:
            result = self._kernel32.LocalFree(allocation)
        if result is not None and result != 0:
            raise self._failure(operation)

    def _close_token(self, token: object) -> None:
        if self._native is not None:
            result = self._native.close_handle(token)
        else:
            result = self._kernel32.CloseHandle(token)
        if not result:
            raise self._failure("CloseHandle")

    @staticmethod
    def _raise_cleanup(
        primary: BaseException | None,
        cleanup: BaseException,
    ) -> BaseException:
        return _merge_cleanup_failure(primary, cleanup)

    def _current_sid(self) -> str:
        if self._native is not None:
            token = self._native.open_current_process_token()
            allocation: object | None = None
            sid: str | None = None
            primary: BaseException | None = None
            try:
                sid, allocation = self._native.current_sid_from_token(token)
                if not isinstance(sid, str) or not sid:
                    raise Win32FileAuthorityError("current Windows SID is unavailable")
            except BaseException as exc:
                primary = exc
            if allocation is not None:
                try:
                    self._free_local(allocation, "LocalFree(SID)")
                except BaseException as cleanup:
                    primary = self._raise_cleanup(primary, cleanup)
            try:
                self._close_token(token)
            except BaseException as cleanup:
                primary = self._raise_cleanup(primary, cleanup)
            if primary is not None:
                raise primary
            assert sid is not None
            return sid

        ctypes = self._ctypes
        wintypes = self._wintypes
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), self._TOKEN_QUERY, ctypes.byref(token)
        ):
            raise self._failure("OpenProcessToken")
        allocation: object | None = None
        sid: str | None = None
        primary: BaseException | None = None
        try:
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token, self._TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if not required.value:
                raise self._failure("GetTokenInformation")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise self._failure("GetTokenInformation")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                raise self._failure("ConvertSidToStringSidW")
            allocation = text
            if not text.value:
                raise Win32FileAuthorityError("current Windows SID is unavailable")
            sid = text.value
        except BaseException as exc:
            primary = exc
        if allocation is not None:
            try:
                self._free_local(allocation, "LocalFree(SID)")
            except BaseException as cleanup:
                primary = self._raise_cleanup(primary, cleanup)
        try:
            self._close_token(token)
        except BaseException as cleanup:
            primary = self._raise_cleanup(primary, cleanup)
        if primary is not None:
            raise primary
        assert sid is not None
        return sid

    @staticmethod
    def _sddl(sid: str) -> str:
        return f"O:{sid}D:P(A;OICI;FA;;;{sid})"

    def _descriptor_to_sddl(self, descriptor: object) -> str:
        if self._native is not None:
            value, allocation = self._native.sddl_from_descriptor(descriptor)
            primary: BaseException | None = None
            try:
                if not isinstance(value, str) or not value:
                    raise Win32FileAuthorityError("Windows DACL SDDL is unavailable")
            except BaseException as exc:
                primary = exc
            try:
                self._free_local(allocation, "LocalFree(SDDL)")
            except BaseException as cleanup:
                primary = self._raise_cleanup(primary, cleanup)
            if primary is not None:
                raise primary
            return value

        ctypes = self._ctypes
        wintypes = self._wintypes
        flags = self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION
        text = wintypes.LPWSTR()
        if not self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            self._SDDL_REVISION_1,
            flags,
            ctypes.byref(text),
            None,
        ):
            raise self._failure("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        value: str | None = None
        primary: BaseException | None = None
        try:
            if not text.value:
                raise Win32FileAuthorityError("Windows DACL SDDL is unavailable")
            value = text.value
        except BaseException as exc:
            primary = exc
        try:
            self._free_local(text, "LocalFree(SDDL)")
        except BaseException as cleanup:
            primary = self._raise_cleanup(primary, cleanup)
        if primary is not None:
            raise primary
        assert value is not None
        return value

    def _read_sddl(self, path: Path) -> str:
        if self._native is not None:
            return self._descriptor_to_sddl(self._native.read_descriptor(path))
        ctypes = self._ctypes
        wintypes = self._wintypes
        flags = self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION
        required = wintypes.DWORD()
        self._advapi32.GetFileSecurityW(str(path), flags, None, 0, ctypes.byref(required))
        if not required.value:
            raise self._failure("GetFileSecurityW")
        descriptor = ctypes.create_string_buffer(required.value)
        if not self._advapi32.GetFileSecurityW(
            str(path), flags, descriptor, required, ctypes.byref(required)
        ):
            raise self._failure("GetFileSecurityW")
        return self._descriptor_to_sddl(descriptor)

    def _descriptor_from_sddl(self, expected: str) -> object:
        if self._native is not None:
            return self._native.descriptor_from_sddl(expected)
        ctypes = self._ctypes
        wintypes = self._wintypes
        descriptor = ctypes.c_void_p()
        size = wintypes.DWORD()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            expected,
            self._SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(size),
        ):
            raise self._failure("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        return descriptor

    def _canonical_expected(self, sid: str) -> tuple[object, str]:
        descriptor = self._descriptor_from_sddl(self._sddl(sid))
        try:
            canonical = self._descriptor_to_sddl(descriptor)
        except BaseException as exc:
            try:
                self._free_local(descriptor, "LocalFree(security descriptor)")
            except BaseException as cleanup:
                failure = self._raise_cleanup(exc, cleanup)
                if failure is not exc:
                    raise failure from exc
            raise
        return descriptor, canonical

    def apply(self, path: Path) -> tuple[str, str]:
        sid = self._current_sid()
        descriptor, expected = self._canonical_expected(sid)
        primary: BaseException | None = None
        result: tuple[str, str] | None = None
        try:
            flags = (
                self._OWNER_SECURITY_INFORMATION
                | self._DACL_SECURITY_INFORMATION
                | self._PROTECTED_DACL_SECURITY_INFORMATION
            )
            if self._native is not None:
                self._native.set_file_security(path, descriptor)
            elif not self._advapi32.SetFileSecurityW(str(path), flags, descriptor):
                raise self._failure("SetFileSecurityW")
            actual = self._read_sddl(path)
            if actual != expected:
                raise Win32FileAuthorityError("Windows authority DACL is not current-user-only")
            result = sid, hashlib.sha256(expected.encode("utf-8")).hexdigest()
        except BaseException as exc:
            primary = exc
        try:
            self._free_local(descriptor, "LocalFree(security descriptor)")
        except BaseException as cleanup:
            primary = self._raise_cleanup(primary, cleanup)
        if primary is not None:
            raise primary
        assert result is not None
        return result

    def validate(self, path: Path) -> tuple[str, str]:
        sid = self._current_sid()
        descriptor, expected = self._canonical_expected(sid)
        primary: BaseException | None = None
        result: tuple[str, str] | None = None
        try:
            actual = self._read_sddl(path)
            if actual != expected:
                raise Win32FileAuthorityError("Windows authority DACL is not current-user-only")
            result = sid, hashlib.sha256(expected.encode("utf-8")).hexdigest()
        except BaseException as exc:
            primary = exc
        try:
            self._free_local(descriptor, "LocalFree(security descriptor)")
        except BaseException as cleanup:
            primary = self._raise_cleanup(primary, cleanup)
        if primary is not None:
            raise primary
        assert result is not None
        return result


class _WindowsDirectoryCreator:
    """Setup-only native directory creation, kept outside runtime authority."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise Win32FileAuthorityError("native Windows directory setup is unavailable")
        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateDirectoryW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        ]
        self._kernel32.CreateDirectoryW.restype = wintypes.BOOL

    def create(self, path: Path) -> bool:
        if self._kernel32.CreateDirectoryW(str(path), None):
            return True
        code = self._ctypes.get_last_error()
        if code in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            return False
        raise OSError(code, "CreateDirectoryW failed", None, code)


class _WindowsAuthoritySystem:
    def __init__(self) -> None:
        from .win32_process_containment import windows_local_app_data

        self._local_app_data = windows_local_app_data
        self._primitives = Win32FilePrimitives()
        self._strategy = Win32FileAuthority(_primitives=self._primitives)
        self._dacl = _CurrentUserDacl()
        self._directory_creator = _WindowsDirectoryCreator()

    def resolve_local_app_data(self) -> Path:
        return self._local_app_data()

    def validate_supported_location(self, path: Path) -> None:
        if not path.is_absolute() or any(":" in component for component in path.parts[1:]):
            raise Win32FileAuthorityError("Windows authority root is invalid")
        self._primitives.validate_supported_volume(path.parent)

    def create_directory(self, path: Path) -> bool:
        created = self._directory_creator.create(path)
        authority = self.open_directory(path, create=False)
        primary: BaseException | None = None
        try:
            authority.validate()
        except BaseException as exc:
            primary = exc
        try:
            authority.close()
        except BaseException as cleanup:
            primary = _merge_cleanup_failure(primary, cleanup)
        if primary is not None:
            raise primary
        return created

    def apply_current_user_dacl(self, path: Path) -> tuple[str, str]:
        return self._dacl.apply(path)

    def validate_current_user_dacl(self, path: Path) -> tuple[str, str]:
        return self._dacl.validate(path)

    def open_directory(self, path: Path, *, create: bool) -> _NativeWin32DirectoryAuthority:
        retained = self._strategy.retain_directory(path, create=create)
        return _NativeWin32DirectoryAuthority(self._strategy, retained, path)

    @staticmethod
    def list_children(
        authority: _NativeWin32DirectoryAuthority,
    ) -> tuple[str, ...]:
        return authority.child_names(maximum_entries=7)


@dataclass(frozen=True, slots=True)
class _ResolvedWindowsAuthorityRoot:
    local_app_data: Path
    namespace: Path
    root: Path


def _root_from_system(system: object) -> _ResolvedWindowsAuthorityRoot:
    local_app_data = Path(system.resolve_local_app_data())
    namespace = local_app_data / "mif_pipeline"
    root = local_app_data / "mif_pipeline" / "authority-v2"
    if (
        not PureWindowsPath(os.fspath(local_app_data)).is_absolute()
        or AUTHORITY_BUCKET_NAMES_V2 != _EXPECTED_BUCKETS
    ):
        raise AuthorityProvisioningRequiredError(
            "Windows authority root or closed bucket table is invalid"
        )
    try:
        system.validate_supported_location(root)
    except Exception as exc:
        raise AuthorityProvisioningRequiredError(
            "Windows authority requires fixed local NTFS"
        ) from exc
    return _ResolvedWindowsAuthorityRoot(
        local_app_data=local_app_data,
        namespace=namespace,
        root=root,
    )


def _close_opened(authorities: list[object], primary: BaseException | None) -> BaseException | None:
    failure = primary
    while authorities:
        authority = authorities.pop()
        try:
            authority.close()
        except BaseException as cleanup:
            failure = _merge_cleanup_failure(failure, cleanup)
    return failure


def _provision_windows_current_user_directory_closure(
    system: object,
    root: Path,
    *,
    child_names: tuple[str, ...],
) -> Path:
    """Create, ACL-close, and independently reopen one exact directory set."""

    requested = Path(root)
    names = tuple(child_names)
    if (
        not PureWindowsPath(os.fspath(requested)).is_absolute()
        or ".." in requested.parts
        or not names
        or names != tuple(sorted(names))
        or len(set(names)) != len(names)
        or any(
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or any(character in name for character in ("\0", ":", "/", "\\"))
            for name in names
        )
    ):
        raise Win32FileAuthorityError("Windows current-user directory closure is invalid")
    paths = (requested, *(requested / name for name in names))
    try:
        system.validate_supported_location(requested)
        anchor = system.open_directory(requested.parent, create=False)
        primary: BaseException | None = None
        applied: list[tuple[str, str]] = []
        try:
            for path in paths:
                system.create_directory(path)
                applied.append(system.apply_current_user_dacl(path))
        except BaseException as exc:
            primary = exc
        try:
            anchor.close()
        except BaseException as cleanup:
            primary = _merge_cleanup_failure(primary, cleanup)
        if primary is not None:
            raise primary
        if len(set(applied)) != 1:
            raise Win32FileAuthorityError(
                "Windows current-user directory DACL closure is inconsistent"
            )

        opened: list[object] = []
        primary = None
        try:
            observed: list[tuple[str, str]] = []
            for path in paths:
                authority = system.open_directory(path, create=False)
                opened.append(authority)
                authority.validate()
                if Path(authority.path) != path:
                    raise Win32FileAuthorityError("Windows current-user directory identity changed")
                observed.append(system.validate_current_user_dacl(path))
            if len(set(observed)) != 1 or observed[0] != applied[0]:
                raise Win32FileAuthorityError("Windows current-user directory DACL is not exact")
        except BaseException as exc:
            primary = exc
        failure = _close_opened(opened, primary)
        if failure is not None:
            raise failure
        return requested
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, Win32FileAuthorityError):
            raise
        raise Win32FileAuthorityError(
            "Windows current-user directory closure could not be provisioned"
        ) from exc


def provision_windows_current_user_directory_closure(
    root: Path,
    *,
    child_names: tuple[str, ...],
) -> Path:
    """Provision current-user-only directories before create-disabled use."""

    return _provision_windows_current_user_directory_closure(
        _WindowsAuthoritySystem(),
        root,
        child_names=child_names,
    )


def _open_windows_authority_layout_v2(
    system: object,
    resolution: _ResolvedWindowsAuthorityRoot | None = None,
) -> RetainedAuthorityLayoutV2:
    resolved = resolution if resolution is not None else _root_from_system(system)
    root_path = resolved.root
    opened: list[object] = []
    try:
        root = system.open_directory(root_path, create=False)
        opened.append(root)
        buckets: list[tuple[str, object]] = []
        bucket_records: list[AuthorityBucketV2] = []
        for name in AUTHORITY_BUCKET_NAMES_V2:
            authority = system.open_directory(root_path / name, create=False)
            opened.append(authority)
            buckets.append((name, authority))
            bucket_records.append(
                AuthorityBucketV2(name=name, directory_identity=authority.identity)
            )
        if set(system.list_children(root)) != {
            *AUTHORITY_BUCKET_NAMES_V2,
            AUTHORITY_LAYOUT_MARKER_NAME_V2,
        }:
            raise AuthorityProvisioningRequiredError(
                "Windows authority root child closure is not exact"
            )
        payload = root.read_immutable(
            AUTHORITY_LAYOUT_MARKER_NAME_V2,
            max_bytes=AUTHORITY_LAYOUT_MAX_BYTES_V2,
            label="application authority layout v2 marker",
        )
        layout = ApplicationAuthorityLayoutV2.from_bytes(payload)
        observed_dacls = tuple(
            system.validate_current_user_dacl(path)
            for path in (
                resolved.namespace,
                root_path,
                *(root_path / name for name in AUTHORITY_BUCKET_NAMES_V2),
            )
        )
        if len(set(observed_dacls)) != 1:
            raise AuthorityProvisioningRequiredError(
                "Windows authority DACL closure is inconsistent"
            )
        owner_sid, dacl_sha256 = observed_dacls[0]
        expected_provisioning = Win32AuthorityProvisioningV2(
            setup_schema_version=1,
            known_folder="FOLDERID_LocalAppData",
            filesystem_name="NTFS",
            owner_sid=owner_sid,
            dacl_sha256=dacl_sha256,
        )
        if (
            layout.platform != "windows"
            or layout.filesystem_contract != "windows-fixed-ntfs-v2"
            or layout.root_identity != root.identity
            or layout.provisioning != expected_provisioning
            or layout.buckets != tuple(bucket_records)
        ):
            raise AuthorityProvisioningRequiredError(
                "Windows authority marker does not match retained closure"
            )
        retained = RetainedAuthorityLayoutV2(
            layout=layout,
            root=root,
            buckets=tuple(buckets),
        )
        retained.validate()
        opened.clear()
        return retained
    except BaseException as exc:
        failure = _close_opened(opened, exc)
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        if isinstance(failure, AuthorityProvisioningRequiredError):
            raise failure
        raise AuthorityProvisioningRequiredError(
            "Windows authority layout v2 must be provisioned before runtime"
        ) from failure


def _provision_windows_authority_layout_v2(
    system: object,
) -> ApplicationAuthorityLayoutV2:
    resolution = _root_from_system(system)
    root_path = resolution.root
    paths = (resolution.namespace, root_path) + tuple(
        root_path / name for name in AUTHORITY_BUCKET_NAMES_V2
    )
    try:
        anchor = system.open_directory(resolution.local_app_data, create=False)
        observed_dacls = []
        primary: BaseException | None = None
        try:
            for path in paths:
                system.create_directory(path)
                observed_dacls.append(system.apply_current_user_dacl(path))
        except BaseException as exc:
            primary = exc
        try:
            anchor.close()
        except BaseException as cleanup:
            primary = _merge_cleanup_failure(primary, cleanup)
        if primary is not None:
            raise primary
        if len(set(observed_dacls)) != 1:
            raise AuthorityProvisioningRequiredError("Windows setup DACL closure is inconsistent")
        opened: list[object] = []
        primary: BaseException | None = None
        layout: ApplicationAuthorityLayoutV2 | None = None
        try:
            root = system.open_directory(root_path, create=False)
            opened.append(root)
            bucket_records: list[AuthorityBucketV2] = []
            for name in AUTHORITY_BUCKET_NAMES_V2:
                authority = system.open_directory(root_path / name, create=False)
                opened.append(authority)
                bucket_records.append(
                    AuthorityBucketV2(
                        name=name,
                        directory_identity=authority.identity,
                    )
                )
            allowed = {*AUTHORITY_BUCKET_NAMES_V2, AUTHORITY_LAYOUT_MARKER_NAME_V2}
            if not set(system.list_children(root)) <= allowed:
                raise AuthorityProvisioningRequiredError(
                    "Windows authority root has an unexpected child"
                )
            owner_sid, dacl_sha256 = observed_dacls[0]
            layout = ApplicationAuthorityLayoutV2.create(
                platform="windows",
                filesystem_contract="windows-fixed-ntfs-v2",
                root_identity=root.identity,
                provisioning=Win32AuthorityProvisioningV2(
                    setup_schema_version=1,
                    known_folder="FOLDERID_LocalAppData",
                    filesystem_name="NTFS",
                    owner_sid=owner_sid,
                    dacl_sha256=dacl_sha256,
                ),
                buckets=tuple(bucket_records),
            )
            root.publish_immutable(
                AUTHORITY_LAYOUT_MARKER_NAME_V2,
                layout.to_bytes(),
                max_bytes=AUTHORITY_LAYOUT_MAX_BYTES_V2,
                label="application authority layout v2 marker",
            )
        except BaseException as exc:
            primary = exc
        failure = _close_opened(opened, primary)
        if failure is not None:
            raise failure
        assert layout is not None
        retained = _open_windows_authority_layout_v2(system, resolution)
        primary = None
        try:
            if retained.layout != layout:
                raise AuthorityProvisioningRequiredError(
                    "Windows authority changed during independent setup reopen"
                )
        except BaseException as exc:
            primary = exc
        try:
            retained.close()
        except BaseException as cleanup:
            primary = _merge_cleanup_failure(primary, cleanup)
        if primary is not None:
            raise primary
        return layout
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, AuthorityProvisioningRequiredError):
            raise
        raise AuthorityProvisioningRequiredError(
            "Windows authority setup did not complete exactly"
        ) from exc


def provision_windows_authority_layout_v2() -> ApplicationAuthorityLayoutV2:
    return _provision_windows_authority_layout_v2(_WindowsAuthoritySystem())


def open_windows_authority_layout_v2() -> RetainedAuthorityLayoutV2:
    return _open_windows_authority_layout_v2(_WindowsAuthoritySystem())


__all__ = [
    "open_windows_authority_layout_v2",
    "provision_windows_current_user_directory_closure",
    "provision_windows_authority_layout_v2",
]
