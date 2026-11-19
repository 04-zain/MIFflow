"""POSIX setup and create-disabled open for flat authority layout v2.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import stat

from .authority_layout import (
    AUTHORITY_BUCKET_NAMES_V2,
    AUTHORITY_LAYOUT_MARKER_NAME_V2,
    AUTHORITY_LAYOUT_MAX_BYTES_V2,
    ApplicationAuthorityLayoutV2,
    AuthorityBucketV2,
    PosixAuthorityProvisioningV2,
    PosixFileIdentityV2,
)
from .authority_provisioning import (
    AuthorityProvisioningRequiredError,
    RetainedAuthorityLayoutV2,
    _merge_cleanup_failure,
)
from .durable_records import DurableDirectoryAuthority, DurableRecordError

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


# ==============================================================================
#                          Posix authority provisioning
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _ResolvedPosixAuthorityRoot:
    trusted_base: Path
    root: Path


def _resolve_posix_authority_root_v2() -> _ResolvedPosixAuthorityRoot:
    uid = os.getuid()
    candidate = os.environ.get("XDG_STATE_HOME")
    if candidate and Path(candidate).is_absolute():
        base = Path(candidate)
        return _ResolvedPosixAuthorityRoot(
            trusted_base=base,
            root=base / "mif_pipeline" / "authority-v2",
        )
    try:
        raw_home = pwd.getpwuid(uid).pw_dir
    except (KeyError, OSError) as exc:
        raise AuthorityProvisioningRequiredError(
            "current UID has no passwd-database authority home"
        ) from exc
    home = Path(raw_home) if isinstance(raw_home, str) and raw_home else Path()
    if not raw_home or not home.is_absolute():
        raise AuthorityProvisioningRequiredError(
            "current UID passwd-database authority home is not absolute"
        )
    base = home
    return _ResolvedPosixAuthorityRoot(
        trusted_base=base,
        root=base / ".local" / "state" / "mif_pipeline" / "authority-v2",
    )


def _validate_owned_directory(
    metadata: object,
    *,
    owner_uid: int,
    owner_gid: int | None,
    exact_mode: int | None,
) -> None:
    mode = getattr(metadata, "st_mode", None)
    if (
        type(mode) is not int
        or not stat.S_ISDIR(mode)
        or getattr(metadata, "st_uid", None) != owner_uid
        or (owner_gid is not None and getattr(metadata, "st_gid", None) != owner_gid)
        or mode & 0o022
        or (exact_mode is not None and stat.S_IMODE(mode) != exact_mode)
    ):
        raise AuthorityProvisioningRequiredError(
            "POSIX authority directory ownership or mode is unsafe"
        )


def _posix_identity(metadata: object) -> PosixFileIdentityV2:
    try:
        return PosixFileIdentityV2(
            device=int(getattr(metadata, "st_dev")),
            inode=int(getattr(metadata, "st_ino")),
        )
    except (TypeError, ValueError) as exc:
        raise AuthorityProvisioningRequiredError(
            "POSIX authority directory identity is unavailable"
        ) from exc


def _close_descriptors(descriptors: list[int], primary: BaseException | None) -> None:
    failure = primary
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            failure = _merge_cleanup_failure(failure, cleanup)
    if failure is not None and failure is not primary:
        raise failure


def _retain_existing_parent_chain(
    resolution: _ResolvedPosixAuthorityRoot,
    *,
    owner_uid: int,
) -> list[int]:
    base = resolution.trusted_base
    try:
        relative_parent = resolution.root.parent.relative_to(base)
    except ValueError as exc:
        raise AuthorityProvisioningRequiredError(
            "POSIX authority root escapes its trusted base"
        ) from exc
    descriptors = _retain_absolute_base(base, owner_uid=owner_uid)
    try:
        descriptor = descriptors[-1]
        for component in relative_parent.parts:
            if component in {"", ".", ".."}:
                raise AuthorityProvisioningRequiredError("POSIX authority parent path is invalid")
            descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(descriptor)
            _validate_owned_directory(
                os.fstat(descriptor),
                owner_uid=owner_uid,
                owner_gid=None,
                exact_mode=None,
            )
        return descriptors
    except BaseException as exc:
        try:
            _close_descriptors(descriptors, exc)
        except BaseException as cleanup:
            if cleanup is not exc:
                raise
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, AuthorityProvisioningRequiredError):
            raise
        raise AuthorityProvisioningRequiredError(
            "POSIX authority parent chain must be pre-established and safe"
        ) from exc


def _retain_absolute_base(base: Path, *, owner_uid: int) -> list[int]:
    """Retain an absolute base component-by-component without following links."""

    if not base.is_absolute():
        raise AuthorityProvisioningRequiredError("POSIX authority trusted base must be absolute")
    descriptors: list[int] = []
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        for component in base.parts[1:]:
            if component in {"", ".", ".."}:
                raise AuthorityProvisioningRequiredError(
                    "POSIX authority trusted base path is invalid"
                )
            descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(descriptor)
        _validate_owned_directory(
            os.fstat(descriptor),
            owner_uid=owner_uid,
            owner_gid=None,
            exact_mode=None,
        )
        return descriptors
    except BaseException as exc:
        try:
            _close_descriptors(descriptors, exc)
        except BaseException as cleanup:
            if cleanup is not exc:
                raise
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, AuthorityProvisioningRequiredError):
            raise
        raise AuthorityProvisioningRequiredError(
            "POSIX authority trusted base cannot be retained safely"
        ) from exc


def _retain_or_establish_setup_parent_chain(
    resolution: _ResolvedPosixAuthorityRoot,
    *,
    owner_uid: int,
) -> list[int]:
    """Retain the base, then establish only the fixed namespace parents."""

    try:
        relative_parent = resolution.root.parent.relative_to(resolution.trusted_base)
    except ValueError as exc:
        raise AuthorityProvisioningRequiredError(
            "POSIX authority root escapes its trusted base"
        ) from exc
    descriptors = _retain_absolute_base(
        resolution.trusted_base,
        owner_uid=owner_uid,
    )
    try:
        parent_descriptor = descriptors[-1]
        for component in relative_parent.parts:
            if component in {"", ".", ".."}:
                raise AuthorityProvisioningRequiredError(
                    "POSIX authority namespace path is invalid"
                )
            descriptor, _metadata, _created = _open_or_create_directory(
                parent_descriptor,
                component,
                owner_uid=owner_uid,
                owner_gid=None,
                exact_mode=None,
            )
            descriptors.append(descriptor)
            parent_descriptor = descriptor
        return descriptors
    except BaseException as exc:
        try:
            _close_descriptors(descriptors, exc)
        except BaseException as cleanup:
            if cleanup is not exc:
                raise
        raise


def _open_or_create_directory(
    parent_descriptor: int,
    name: str,
    *,
    owner_uid: int,
    owner_gid: int | None,
    exact_mode: int | None = 0o700,
) -> tuple[int, object, bool]:
    created = False
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        created = True
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        _validate_owned_directory(
            metadata,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            exact_mode=exact_mode,
        )
        if not created:
            os.fsync(parent_descriptor)
    except BaseException as exc:
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            failure = _merge_cleanup_failure(exc, cleanup)
            if failure is not exc:
                raise failure from exc
        raise
    return descriptor, metadata, created


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("POSIX marker write made no progress")
        view = view[written:]


def _validate_marker_metadata(
    metadata: object,
    *,
    owner_uid: int,
    owner_gid: int,
) -> tuple[int, int, int, int, int, int]:
    mode = getattr(metadata, "st_mode", None)
    size = getattr(metadata, "st_size", None)
    if (
        type(mode) is not int
        or not stat.S_ISREG(mode)
        or getattr(metadata, "st_nlink", None) != 1
        or getattr(metadata, "st_uid", None) != owner_uid
        or getattr(metadata, "st_gid", None) != owner_gid
        or stat.S_IMODE(mode) != 0o600
        or type(size) is not int
        or not 0 <= size <= AUTHORITY_LAYOUT_MAX_BYTES_V2
    ):
        raise AuthorityProvisioningRequiredError("POSIX authority marker metadata is unsafe")
    return (
        int(getattr(metadata, "st_dev")),
        int(getattr(metadata, "st_ino")),
        mode,
        int(getattr(metadata, "st_nlink")),
        int(getattr(metadata, "st_uid")),
        int(getattr(metadata, "st_gid")),
    )


def _read_marker_descriptor(
    descriptor: int,
    *,
    owner_uid: int,
    owner_gid: int,
) -> bytes:
    metadata = os.fstat(descriptor)
    _validate_marker_metadata(
        metadata,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    chunks: list[bytes] = []
    remaining = AUTHORITY_LAYOUT_MAX_BYTES_V2 + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > AUTHORITY_LAYOUT_MAX_BYTES_V2:
        raise AuthorityProvisioningRequiredError(
            "POSIX authority marker exceeds its bounded byte contract"
        )
    return payload


def _publish_setup_marker(
    root_descriptor: int,
    payload: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    descriptor: int | None = None
    primary: BaseException | None = None
    try:
        try:
            descriptor = os.open(
                AUTHORITY_LAYOUT_MARKER_NAME_V2,
                _FILE_CREATE_FLAGS,
                0o600,
                dir_fd=root_descriptor,
            )
        except FileExistsError:
            descriptor = os.open(
                AUTHORITY_LAYOUT_MARKER_NAME_V2,
                _FILE_READ_FLAGS,
                dir_fd=root_descriptor,
            )
            if (
                _read_marker_descriptor(
                    descriptor,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
                != payload
            ):
                raise AuthorityProvisioningRequiredError(
                    "existing POSIX authority marker conflicts with this layout"
                )
        else:
            _validate_marker_metadata(
                os.fstat(descriptor),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException as exc:
        primary = exc
    if descriptor is not None:
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            primary = _merge_cleanup_failure(primary, cleanup)
    if primary is not None:
        raise primary
    os.fsync(root_descriptor)


def provision_posix_authority_layout_v2() -> ApplicationAuthorityLayoutV2:
    """Explicitly establish the root/five buckets, marker, then reopen."""

    if os.name != "posix":
        raise AuthorityProvisioningRequiredError(
            "POSIX authority provisioning is unavailable on this platform"
        )
    resolution = _resolve_posix_authority_root_v2()
    owner_uid = os.getuid()
    owner_gid = os.getgid()
    descriptors: list[int] = []
    primary: BaseException | None = None
    layout: ApplicationAuthorityLayoutV2 | None = None
    try:
        descriptors.extend(
            _retain_or_establish_setup_parent_chain(
                resolution,
                owner_uid=owner_uid,
            )
        )
        parent_descriptor = descriptors[-1]
        root_descriptor, root_metadata, _created = _open_or_create_directory(
            parent_descriptor,
            resolution.root.name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        descriptors.append(root_descriptor)
        bucket_records: list[AuthorityBucketV2] = []
        for name in AUTHORITY_BUCKET_NAMES_V2:
            descriptor, metadata, _created = _open_or_create_directory(
                root_descriptor,
                name,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            descriptors.append(descriptor)
            bucket_records.append(
                AuthorityBucketV2(
                    name=name,
                    directory_identity=_posix_identity(metadata),
                )
            )
        allowed = {*AUTHORITY_BUCKET_NAMES_V2, AUTHORITY_LAYOUT_MARKER_NAME_V2}
        if not set(os.listdir(root_descriptor)) <= allowed:
            raise AuthorityProvisioningRequiredError("POSIX authority root has an unexpected child")
        layout = ApplicationAuthorityLayoutV2.create(
            platform="linux",
            filesystem_contract="posix-fsync-v2",
            root_identity=_posix_identity(root_metadata),
            provisioning=PosixAuthorityProvisioningV2(
                setup_schema_version=1,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                root_mode=0o700,
                bucket_mode=0o700,
            ),
            buckets=tuple(bucket_records),
        )
        _publish_setup_marker(
            root_descriptor,
            layout.to_bytes(),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    except BaseException as exc:
        primary = exc
    try:
        _close_descriptors(descriptors, primary)
    except BaseException as cleanup:
        primary = cleanup
    if primary is not None:
        if not isinstance(primary, Exception):
            raise primary
        if isinstance(primary, AuthorityProvisioningRequiredError):
            raise primary
        raise AuthorityProvisioningRequiredError(
            "POSIX authority setup did not complete exactly"
        ) from primary
    assert layout is not None
    retained = _open_posix_authority_layout_v2(resolution)
    primary = None
    try:
        if retained.layout != layout:
            raise AuthorityProvisioningRequiredError(
                "POSIX authority changed during independent setup reopen"
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


def _close_authorities(
    authorities: list[DurableDirectoryAuthority],
    primary: BaseException | None,
) -> BaseException | None:
    failure = primary
    while authorities:
        authority = authorities.pop()
        try:
            authority.close()
        except BaseException as cleanup:
            failure = _merge_cleanup_failure(failure, cleanup)
    return failure


def _open_posix_authority_layout_v2(
    resolution: _ResolvedPosixAuthorityRoot,
) -> RetainedAuthorityLayoutV2:
    """Open and cross-validate the existing layout without creating anything."""

    if os.name != "posix":
        raise AuthorityProvisioningRequiredError(
            "POSIX authority open is unavailable on this platform"
        )
    owner_uid = os.getuid()
    owner_gid = os.getgid()
    security_descriptors: list[int] = []
    authorities: list[DurableDirectoryAuthority] = []
    try:
        security_descriptors = _retain_existing_parent_chain(resolution, owner_uid=owner_uid)
        _close_descriptors(security_descriptors, None)
        root = DurableDirectoryAuthority(resolution.root, create=False)
        authorities.append(root)
        root_metadata = os.fstat(root.descriptor)
        _validate_owned_directory(
            root_metadata,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            exact_mode=0o700,
        )
        buckets: list[tuple[str, DurableDirectoryAuthority]] = []
        bucket_records: list[AuthorityBucketV2] = []
        for name in AUTHORITY_BUCKET_NAMES_V2:
            authority = root.retain_child(Path(name), create=False)
            authorities.append(authority)
            metadata = os.fstat(authority.descriptor)
            _validate_owned_directory(
                metadata,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                exact_mode=0o700,
            )
            buckets.append((name, authority))
            bucket_records.append(
                AuthorityBucketV2(
                    name=name,
                    directory_identity=_posix_identity(metadata),
                )
            )
        if set(os.listdir(root.descriptor)) != {
            *AUTHORITY_BUCKET_NAMES_V2,
            AUTHORITY_LAYOUT_MARKER_NAME_V2,
        }:
            raise AuthorityProvisioningRequiredError(
                "POSIX authority root child closure is not exact"
            )
        marker_before = _validate_marker_metadata(
            os.stat(
                AUTHORITY_LAYOUT_MARKER_NAME_V2,
                dir_fd=root.descriptor,
                follow_symlinks=False,
            ),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        payload = root.read_immutable(
            AUTHORITY_LAYOUT_MARKER_NAME_V2,
            max_bytes=AUTHORITY_LAYOUT_MAX_BYTES_V2,
            label="application authority layout v2 marker",
        )
        assert payload is not None
        marker_after = _validate_marker_metadata(
            os.stat(
                AUTHORITY_LAYOUT_MARKER_NAME_V2,
                dir_fd=root.descriptor,
                follow_symlinks=False,
            ),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        if marker_after != marker_before:
            raise AuthorityProvisioningRequiredError(
                "POSIX authority marker changed during retained validation"
            )
        layout = ApplicationAuthorityLayoutV2.from_bytes(payload)
        expected_provisioning = PosixAuthorityProvisioningV2(
            setup_schema_version=1,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            root_mode=0o700,
            bucket_mode=0o700,
        )
        if (
            layout.platform != "linux"
            or layout.filesystem_contract != "posix-fsync-v2"
            or layout.root_identity != _posix_identity(root_metadata)
            or layout.provisioning != expected_provisioning
            or layout.buckets != tuple(bucket_records)
        ):
            raise AuthorityProvisioningRequiredError(
                "POSIX authority marker does not match retained identities"
            )
        retained = RetainedAuthorityLayoutV2(
            layout=layout,
            root=root,
            buckets=tuple(buckets),
        )
        retained.validate()
        authorities.clear()
        return retained
    except BaseException as exc:
        failure = _close_authorities(authorities, exc)
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        if isinstance(failure, AuthorityProvisioningRequiredError):
            raise failure
        raise AuthorityProvisioningRequiredError(
            "POSIX authority layout v2 must be provisioned before runtime"
        ) from failure


def open_posix_authority_layout_v2() -> RetainedAuthorityLayoutV2:
    """Resolve once and open the existing layout without provisioning effects."""

    return _open_posix_authority_layout_v2(_resolve_posix_authority_root_v2())


__all__ = [
    "open_posix_authority_layout_v2",
    "provision_posix_authority_layout_v2",
]
