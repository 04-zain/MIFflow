"""Neutral owner/borrower lifecycle gate for composed runtime resources.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import RLock
from typing import Literal, TypeAlias

RuntimeUseKind: TypeAlias = Literal[
    "batch-writer-lease",
    "batch-state-operation",
    "batch-catalogue-operation",
    "project-mutation-lease",
    "project-provider-operation",
    "project-authority-operation",
    "qupath-process",
    "qupath-project-operation",
    "qupath-pending-launch",
    "qupath-profile-provider",
    "registration-mutation",
]
RuntimeLifecycleState: TypeAlias = Literal["open", "tearing_down", "closed", "failed"]

_RUNTIME_USE_KINDS = frozenset(
    {
        "batch-writer-lease",
        "batch-state-operation",
        "batch-catalogue-operation",
        "project-mutation-lease",
        "project-provider-operation",
        "project-authority-operation",
        "qupath-process",
        "qupath-project-operation",
        "qupath-pending-launch",
        "qupath-profile-provider",
        "registration-mutation",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE_MARKER = object()


# ==============================================================================
#                               Runtime lifecycle
# ==============================================================================


class RuntimeLifecycleError(RuntimeError):
    """Raised when runtime lifecycle authority or state is invalid."""


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RuntimeLifecycleError(f"{label} must be one lowercase SHA-256")
    return value


@dataclass(slots=True)
class _RuntimeLifecycleShared:
    lock: RLock = field(default_factory=RLock)
    state: RuntimeLifecycleState = "open"
    terminal_failure: BaseException | None = None
    live_counts: dict[RuntimeUseKind, int] = field(default_factory=dict)
    binding_records: dict[int, tuple["RuntimeCompositionBinding", object, object, str]] = field(
        default_factory=dict
    )
    teardown_permit: "RuntimeTeardownPermit | None" = None


@dataclass(frozen=True, slots=True, init=False)
class RuntimeCompositionBinding:
    """Immutable public digests plus an opaque same-owner binding identity."""

    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeLifecycleError(
            "runtime composition bindings are issued only by their lifecycle owner"
        )

    @classmethod
    def _issue(
        cls,
        *,
        authority_layout_sha256: str,
        workspace_storage_scope_sha256: str,
        issuer: object,
        marker: object,
    ) -> "RuntimeCompositionBinding":
        if marker is not _OPAQUE_MARKER:
            raise RuntimeLifecycleError("runtime composition binding issuer is invalid")
        binding = object.__new__(cls)
        object.__setattr__(
            binding,
            "authority_layout_sha256",
            _sha256(authority_layout_sha256, "authority layout digest"),
        )
        object.__setattr__(
            binding,
            "workspace_storage_scope_sha256",
            _sha256(workspace_storage_scope_sha256, "workspace storage scope digest"),
        )
        object.__setattr__(binding, "_issuer", issuer)
        return binding


@dataclass(frozen=True, slots=True, init=False)
class RuntimeTeardownPermit:
    """Opaque same-owner authority for one terminal teardown attempt."""

    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeLifecycleError(
            "runtime teardown permits are issued only by their lifecycle owner"
        )

    @classmethod
    def _issue(cls, *, issuer: object, marker: object) -> "RuntimeTeardownPermit":
        if marker is not _OPAQUE_MARKER:
            raise RuntimeLifecycleError("runtime teardown permit issuer is invalid")
        permit = object.__new__(cls)
        object.__setattr__(permit, "_issuer", issuer)
        return permit


class RuntimeUseToken:
    """One idempotently releasable use admission from a lifecycle borrower."""

    __slots__ = ("_kind", "_released", "_shared")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeLifecycleError("runtime use tokens are borrower-issued")

    @classmethod
    def _issue(
        cls,
        *,
        shared: _RuntimeLifecycleShared,
        kind: RuntimeUseKind,
        marker: object,
    ) -> "RuntimeUseToken":
        if marker is not _OPAQUE_MARKER:
            raise RuntimeLifecycleError("runtime use token issuer is invalid")
        token = object.__new__(cls)
        token._shared = shared
        token._kind = kind
        token._released = False
        return token

    @property
    def is_live(self) -> bool:
        with self._shared.lock:
            return not self._released

    def release(self) -> None:
        with self._shared.lock:
            if self._released:
                return
            remaining = self._shared.live_counts.get(self._kind, 0) - 1
            if remaining < 0:
                raise RuntimeLifecycleError("runtime use-token accounting underflow")
            if remaining:
                self._shared.live_counts[self._kind] = remaining
            else:
                self._shared.live_counts.pop(self._kind, None)
            self._released = True


class RuntimeLifecycleBorrower:
    """Use-only view of a shared lifecycle owner."""

    __slots__ = ("_shared",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeLifecycleError("runtime lifecycle borrowers are owner-issued")

    @classmethod
    def _issue(
        cls, *, shared: _RuntimeLifecycleShared, marker: object
    ) -> "RuntimeLifecycleBorrower":
        if marker is not _OPAQUE_MARKER:
            raise RuntimeLifecycleError("runtime lifecycle borrower issuer is invalid")
        borrower = object.__new__(cls)
        borrower._shared = shared
        return borrower

    @staticmethod
    def _kind(value: object) -> RuntimeUseKind:
        if value not in _RUNTIME_USE_KINDS:
            raise RuntimeLifecycleError(f"unsupported runtime use kind {value!r}")
        return value  # type: ignore[return-value]

    def acquire(self, kind: RuntimeUseKind) -> RuntimeUseToken:
        checked = self._kind(kind)
        with self._shared.lock:
            if self._shared.state != "open":
                raise RuntimeLifecycleError("runtime lifecycle is not open")
            self._shared.live_counts[checked] = self._shared.live_counts.get(checked, 0) + 1
            return RuntimeUseToken._issue(shared=self._shared, kind=checked, marker=_OPAQUE_MARKER)

    def has_live_uses(self, kind: RuntimeUseKind) -> bool:
        checked = self._kind(kind)
        with self._shared.lock:
            return self._shared.live_counts.get(checked, 0) > 0

    def is_kind_quiescent(self, kind: RuntimeUseKind) -> bool:
        return not self.has_live_uses(kind)

    def _binding_record(
        self, binding: RuntimeCompositionBinding
    ) -> tuple[RuntimeCompositionBinding, object, object, str]:
        if not isinstance(binding, RuntimeCompositionBinding):
            raise RuntimeLifecycleError("runtime composition binding is invalid")
        with self._shared.lock:
            if self._shared.state != "open":
                raise RuntimeLifecycleError("runtime lifecycle is not open")
            record = self._shared.binding_records.get(id(binding))
            if record is None or record[0] is not binding:
                raise RuntimeLifecycleError(
                    "runtime composition binding belongs to another lifecycle"
                )
            return record

    def authenticate_authority_binding(
        self,
        binding: RuntimeCompositionBinding,
        *,
        authority_layout: object,
        workspace_storage_scope_sha256: str,
    ) -> None:
        _, expected_layout, _, expected_scope = self._binding_record(binding)
        if (
            authority_layout is not expected_layout
            or _sha256(
                workspace_storage_scope_sha256,
                "workspace storage scope digest",
            )
            != expected_scope
        ):
            raise RuntimeLifecycleError("runtime authority binding differs")

    def authenticate_binding(
        self,
        binding: RuntimeCompositionBinding,
        *,
        authority_layout: object,
        runner: object,
        workspace_storage_scope_sha256: str,
    ) -> None:
        _, expected_layout, expected_runner, expected_scope = self._binding_record(binding)
        if (
            authority_layout is not expected_layout
            or runner is not expected_runner
            or _sha256(
                workspace_storage_scope_sha256,
                "workspace storage scope digest",
            )
            != expected_scope
        ):
            raise RuntimeLifecycleError("runtime composition binding differs")

    def authenticate_teardown(self, permit: RuntimeTeardownPermit) -> None:
        if not isinstance(permit, RuntimeTeardownPermit):
            raise RuntimeLifecycleError("runtime teardown permit is invalid")
        with self._shared.lock:
            if (
                self._shared.state != "tearing_down"
                or self._shared.teardown_permit is not permit
                or permit._issuer is not self._shared
            ):
                raise RuntimeLifecycleError("runtime teardown permit belongs to another lifecycle")


class RuntimeLifecycleOwner:
    """Sole owner allowed to transition a shared runtime to teardown."""

    __slots__ = ("_borrower", "_shared")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeLifecycleError("runtime lifecycle owners are opened explicitly")

    @classmethod
    def open(cls) -> "RuntimeLifecycleOwner":
        owner = object.__new__(cls)
        owner._shared = _RuntimeLifecycleShared()
        owner._borrower = RuntimeLifecycleBorrower._issue(
            shared=owner._shared, marker=_OPAQUE_MARKER
        )
        return owner

    @property
    def borrower(self) -> RuntimeLifecycleBorrower:
        return self._borrower

    @property
    def state(self) -> RuntimeLifecycleState:
        with self._shared.lock:
            return self._shared.state

    @property
    def terminal_failure(self) -> BaseException | None:
        with self._shared.lock:
            return self._shared.terminal_failure

    def issue_binding(
        self,
        *,
        authority_layout: object,
        runner: object,
        workspace_storage_scope_sha256: str,
    ) -> RuntimeCompositionBinding:
        try:
            layout_sha256 = authority_layout.layout.layout_sha256  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise RuntimeLifecycleError("retained authority layout is invalid") from exc
        checked_layout = _sha256(layout_sha256, "authority layout digest")
        checked_scope = _sha256(workspace_storage_scope_sha256, "workspace storage scope digest")
        with self._shared.lock:
            if self._shared.state != "open":
                raise RuntimeLifecycleError("runtime lifecycle is not open")
            binding = RuntimeCompositionBinding._issue(
                authority_layout_sha256=checked_layout,
                workspace_storage_scope_sha256=checked_scope,
                issuer=self._shared,
                marker=_OPAQUE_MARKER,
            )
            self._shared.binding_records[id(binding)] = (
                binding,
                authority_layout,
                runner,
                checked_scope,
            )
            return binding

    def begin_teardown(self) -> RuntimeTeardownPermit:
        with self._shared.lock:
            if self._shared.state != "open":
                raise RuntimeLifecycleError("runtime teardown is already terminal or active")
            if any(self._shared.live_counts.values()):
                raise RuntimeLifecycleError("runtime teardown requires quiescence")
            permit = RuntimeTeardownPermit._issue(issuer=self._shared, marker=_OPAQUE_MARKER)
            self._shared.teardown_permit = permit
            self._shared.state = "tearing_down"
            return permit

    def finish_teardown(
        self,
        permit: RuntimeTeardownPermit,
        *,
        failure: BaseException | None,
    ) -> None:
        if failure is not None and not isinstance(failure, BaseException):
            raise RuntimeLifecycleError("runtime teardown failure is invalid")
        with self._shared.lock:
            if (
                self._shared.state != "tearing_down"
                or self._shared.teardown_permit is not permit
                or permit._issuer is not self._shared
            ):
                raise RuntimeLifecycleError("runtime teardown permit is invalid")
            self._shared.terminal_failure = failure
            self._shared.state = "failed" if failure is not None else "closed"


__all__ = [
    "RuntimeCompositionBinding",
    "RuntimeLifecycleBorrower",
    "RuntimeLifecycleError",
    "RuntimeLifecycleOwner",
    "RuntimeLifecycleState",
    "RuntimeTeardownPermit",
    "RuntimeUseKind",
    "RuntimeUseToken",
]
