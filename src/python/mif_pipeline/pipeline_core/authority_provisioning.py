"""Neutral provision/open composition for application authority layout v2.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path
import sys
from typing import Protocol

from .authority_layout import (
    AUTHORITY_BUCKET_NAMES_V2,
    ApplicationAuthorityLayoutV2,
    AuthorityMemberRefV2,
)
from .durable_records import (
    DurableRecordError,
    ReservedAuthorityLogPair,
    _preserve_primary_failure,
)

# ==============================================================================
#                             Authority provisioning
# ==============================================================================


class AuthorityProvisioningRequiredError(DurableRecordError):
    """The fixed authority layout is absent, unsafe, or no longer exact."""


class _RetainedDirectory(Protocol):
    @property
    def identity(self) -> object: ...

    def validate(self) -> None: ...

    def read_immutable(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None: ...

    def publish_immutable(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None: ...

    def read_bytes(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None: ...

    def publish_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None: ...

    def replace_pointer_cas(
        self,
        pointer_name: str,
        immutable_authority: "_RetainedDirectory",
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes: ...

    def remove_durable(self, name: str) -> None: ...

    def _member_path(self, name: str) -> Path: ...

    def reserve_log_pair(self, stdout_name: str, stderr_name: str) -> ReservedAuthorityLogPair: ...

    def close(self) -> None: ...


class _RetainedAuthorityBucketV2:
    """Layout-bound durable-record capability for one retained v2 bucket."""

    def __init__(
        self,
        *,
        authority: _RetainedDirectory,
        layout: ApplicationAuthorityLayoutV2,
        bucket_name: str,
        strategy_identity: object,
    ) -> None:
        self._authority = authority
        self._layout = layout
        self._bucket_name = bucket_name
        self.authority_layout_sha256 = layout.layout_sha256
        self.strategy_identity = strategy_identity

    @property
    def identity(self) -> object:
        return self._authority.identity

    def validate(self) -> None:
        self._authority.validate()

    def read_immutable(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        return self._authority.read_bytes(
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
        self._authority.publish_bytes(
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
        )

    def replace_pointer_cas(
        self,
        pointer_name: str,
        immutable_authority: "_RetainedAuthorityBucketV2",
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes:
        if (
            not isinstance(immutable_authority, _RetainedAuthorityBucketV2)
            or self.strategy_identity is not immutable_authority.strategy_identity
            or self.authority_layout_sha256 != immutable_authority.authority_layout_sha256
        ):
            raise AuthorityProvisioningRequiredError(
                f"{label} bucket capabilities do not share one retained layout"
            )
        return self._authority.replace_pointer_cas(
            pointer_name,
            immutable_authority._authority,
            immutable_name,
            expected_predecessor_bytes=expected_predecessor_bytes,
            max_bytes=max_bytes,
            label=label,
        )

    def remove_durable(self, name: str) -> None:
        self._authority.remove_durable(name)

    def _member_family(self, ref: AuthorityMemberRefV2):
        if (
            type(ref) is not AuthorityMemberRefV2
            or ref.authority_layout_sha256 != self.authority_layout_sha256
        ):
            raise AuthorityProvisioningRequiredError(
                "authority member does not belong to the retained layout"
            )
        family = self._layout.family(ref.family)
        if family.physical_bucket != self._bucket_name:
            raise AuthorityProvisioningRequiredError(
                "authority member does not belong to the retained bucket"
            )
        return family

    def member_path(self, ref: AuthorityMemberRefV2) -> Path:
        """Bridge one exact retained member into an external process argument."""

        self._member_family(ref)
        return self._authority._member_path(ref.physical_name)

    def read_member(
        self,
        ref: AuthorityMemberRefV2,
        *,
        optional: bool = False,
    ) -> bytes | None:
        family = self._member_family(ref)
        payload = self._authority.read_bytes(
            ref.physical_name,
            max_bytes=family.max_member_bytes,
            label=f"{ref.family} authority member",
            optional=optional,
        )
        if payload is not None:
            family.validate_payload(payload)
        return payload

    def publish_member(self, ref: AuthorityMemberRefV2, payload: bytes) -> None:
        family = self._member_family(ref)
        family.validate_payload(payload)
        self._authority.publish_bytes(
            ref.physical_name,
            payload,
            max_bytes=family.max_member_bytes,
            label=f"{ref.family} authority member",
        )
        if self.read_member(ref) != payload:
            raise AuthorityProvisioningRequiredError(
                "published authority member changed after reopen"
            )

    def reserve_log_pair(
        self,
        stdout_ref: AuthorityMemberRefV2,
        stderr_ref: AuthorityMemberRefV2,
    ) -> ReservedAuthorityLogPair:
        stdout_family = self._member_family(stdout_ref)
        stderr_family = self._member_family(stderr_ref)
        if (
            stdout_family.family != "runtime_log"
            or stderr_family.family != "runtime_log"
            or stdout_ref == stderr_ref
        ):
            raise AuthorityProvisioningRequiredError(
                "runtime log reservation references are invalid"
            )
        return self._authority.reserve_log_pair(
            stdout_ref.physical_name,
            stderr_ref.physical_name,
        )

    def close(self) -> None:
        self._authority.close()


def _merge_cleanup_failure(
    primary: BaseException | None,
    cleanup: BaseException,
) -> BaseException:
    if primary is None:
        return cleanup
    if isinstance(primary, Exception) and not isinstance(cleanup, Exception):
        _preserve_primary_failure(
            cleanup,
            primary,
            context="earlier authority cleanup failure",
        )
        return cleanup
    _preserve_primary_failure(
        primary,
        cleanup,
        context="later authority cleanup failure",
    )
    return primary


class RetainedAuthorityLayoutV2:
    """Own one exact marker plus retained root and five bucket authorities."""

    def __init__(
        self,
        *,
        layout: ApplicationAuthorityLayoutV2,
        root: _RetainedDirectory,
        buckets: tuple[tuple[str, _RetainedDirectory], ...],
    ) -> None:
        names = tuple(name for name, _authority in buckets)
        marker_names = tuple(bucket.name for bucket in layout.buckets)
        if names != AUTHORITY_BUCKET_NAMES_V2 or marker_names != names:
            raise AuthorityProvisioningRequiredError(
                "retained authority bucket closure is not the exact v2 inventory"
            )
        self.layout = layout
        self.root: _RetainedDirectory | None = root
        strategy_identity = object()
        self.buckets: tuple[tuple[str, _RetainedAuthorityBucketV2], ...] = tuple(
            (
                name,
                _RetainedAuthorityBucketV2(
                    authority=authority,
                    layout=layout,
                    bucket_name=name,
                    strategy_identity=strategy_identity,
                ),
            )
            for name, authority in buckets
        )

    def bucket(self, name: str) -> _RetainedAuthorityBucketV2:
        for candidate, authority in self.buckets:
            if candidate == name:
                return authority
        raise AuthorityProvisioningRequiredError("authority bucket is outside the closed v2 layout")

    def validate(self) -> None:
        if self.root is None:
            raise AuthorityProvisioningRequiredError("retained authority layout is closed")
        self.root.validate()
        for _name, authority in self.buckets:
            authority.validate()

    def close(self) -> None:
        root = self.root
        if root is None:
            return
        self.root = None
        buckets = self.buckets
        self.buckets = ()
        failure: BaseException | None = None
        for _name, authority in reversed(buckets):
            try:
                authority.close()
            except BaseException as cleanup:
                failure = _merge_cleanup_failure(failure, cleanup)
        try:
            root.close()
        except BaseException as cleanup:
            failure = _merge_cleanup_failure(failure, cleanup)
        if failure is not None:
            raise failure

    def __enter__(self) -> "RetainedAuthorityLayoutV2":
        try:
            self.validate()
            return self
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup:
                failure = _merge_cleanup_failure(exc, cleanup)
                if failure is not exc:
                    raise failure from exc
            raise

    def __exit__(self, *_exc: object) -> None:
        active = _exc[1] if len(_exc) > 1 else None
        try:
            self.close()
        except BaseException as cleanup:
            if isinstance(active, BaseException):
                if isinstance(active, Exception) and not isinstance(cleanup, Exception):
                    _preserve_primary_failure(
                        cleanup,
                        active,
                        context="authority body failure",
                    )
                    raise cleanup from active
                _preserve_primary_failure(
                    active,
                    cleanup,
                    context="authority cleanup failure",
                )
                return
            raise


def provision_application_authority_layout_v2() -> ApplicationAuthorityLayoutV2:
    """Run the one setup owner selected for this host."""

    if sys.platform == "linux":
        from .posix_authority_provisioning import (
            provision_posix_authority_layout_v2,
        )

        return provision_posix_authority_layout_v2()
    if sys.platform == "win32":
        from .win32_authority_provisioning import (
            provision_windows_authority_layout_v2,
        )

        return provision_windows_authority_layout_v2()
    raise AuthorityProvisioningRequiredError(
        "application authority layout v2 is unsupported on this platform"
    )


def open_application_authority_layout_v2() -> RetainedAuthorityLayoutV2:
    """Open the fixed host layout without any provisioning side effect."""

    if sys.platform == "linux":
        from .posix_authority_provisioning import open_posix_authority_layout_v2

        return open_posix_authority_layout_v2()
    if sys.platform == "win32":
        from .win32_authority_provisioning import open_windows_authority_layout_v2

        return open_windows_authority_layout_v2()
    raise AuthorityProvisioningRequiredError(
        "application authority layout v2 is unsupported on this platform"
    )


__all__ = [
    "AuthorityProvisioningRequiredError",
    "RetainedAuthorityLayoutV2",
    "open_application_authority_layout_v2",
    "provision_application_authority_layout_v2",
]
