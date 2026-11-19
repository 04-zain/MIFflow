"""Neutral opaque authority for reopening one retained transfer review.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Callable

from .errors import ROIAnalysisError

_TRANSFER_REVIEW_RESOLVER_ISSUER = object()


# ==============================================================================
#                           Transfer review authority
# ==============================================================================


class TransferReviewAuthorityError(ROIAnalysisError):
    """Raised when retained transfer-review authority is absent or altered."""


def _seal_material(
    resolver: "TransferReviewResponseResolver",
    *,
    state_owner: object,
    execution_context: object,
    resolve: Callable[..., object],
) -> bytes:
    return (f"{id(resolver)}:{id(state_owner)}:{id(execution_context)}:{id(resolve)}").encode(
        "ascii"
    )


@dataclass(frozen=True, slots=True, init=False)
class TransferReviewResponseResolver:
    """Issuer-only resolver retaining one exact state owner and retry context."""

    _state_owner: object = field(repr=False, compare=False)
    _execution_context: object = field(repr=False, compare=False)
    _resolve: Callable[..., object] = field(repr=False, compare=False)
    _state_owner_binding: object = field(repr=False, compare=False)
    _execution_context_binding: object = field(repr=False, compare=False)
    _resolve_binding: Callable[..., object] = field(repr=False, compare=False)
    _seal_sha256: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("TransferReviewResponseResolver is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("TransferReviewResponseResolver cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("TransferReviewResponseResolver is nonserializable")

    def resolve_transfer_review_response(
        self,
        *,
        project_file: str,
        moving_project_entry_id: str,
        fixed_project_entry_id: str,
        tissue_generation_id: str,
    ) -> object:
        authenticate_transfer_review_response_resolver(self)
        resolved = self._resolve(
            self._execution_context,
            project_file=project_file,
            moving_project_entry_id=moving_project_entry_id,
            fixed_project_entry_id=fixed_project_entry_id,
            tissue_generation_id=tissue_generation_id,
        )
        authenticate_transfer_review_response_resolver(self)
        return resolved


def _issue_transfer_review_response_resolver(
    *,
    state_owner: object,
    execution_context: object,
    resolve: Callable[..., object],
) -> TransferReviewResponseResolver:
    """Issue from an owning state module after it authenticates current state."""

    if state_owner is None or execution_context is None:
        raise TransferReviewAuthorityError("transfer review resolver requires retained owner state")
    if not callable(resolve) or getattr(resolve, "__self__", None) is not state_owner:
        raise TransferReviewAuthorityError(
            "transfer review resolver callback is not bound to its state owner"
        )
    resolver = object.__new__(TransferReviewResponseResolver)
    values = {
        "_state_owner": state_owner,
        "_execution_context": execution_context,
        "_resolve": resolve,
        "_state_owner_binding": state_owner,
        "_execution_context_binding": execution_context,
        "_resolve_binding": resolve,
        "_seal_sha256": hashlib.sha256(
            _seal_material(
                resolver,
                state_owner=state_owner,
                execution_context=execution_context,
                resolve=resolve,
            )
        ).hexdigest(),
        "_issuer": _TRANSFER_REVIEW_RESOLVER_ISSUER,
    }
    for name in TransferReviewResponseResolver.__dataclass_fields__:
        object.__setattr__(resolver, name, values[name])
    return authenticate_transfer_review_response_resolver(resolver)


def authenticate_transfer_review_response_resolver(
    resolver: object,
) -> TransferReviewResponseResolver:
    if (
        type(resolver) is not TransferReviewResponseResolver
        or resolver._issuer is not _TRANSFER_REVIEW_RESOLVER_ISSUER
        or resolver._state_owner is not resolver._state_owner_binding
        or resolver._execution_context is not resolver._execution_context_binding
        or resolver._resolve is not resolver._resolve_binding
        or getattr(resolver._resolve, "__self__", None) is not resolver._state_owner
    ):
        raise TransferReviewAuthorityError("transfer review resolver is not state-owner-issued")
    expected_seal = hashlib.sha256(
        _seal_material(
            resolver,
            state_owner=resolver._state_owner,
            execution_context=resolver._execution_context,
            resolve=resolver._resolve,
        )
    ).hexdigest()
    if resolver._seal_sha256 != expected_seal:
        raise TransferReviewAuthorityError("transfer review resolver seal is invalid")
    return resolver


__all__ = [
    "TransferReviewAuthorityError",
    "TransferReviewResponseResolver",
    "authenticate_transfer_review_response_resolver",
]
