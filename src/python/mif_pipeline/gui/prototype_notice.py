"""Canonical user-visible classification for the trusted-local prototype."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ==============================================================================
#                                Prototype notice
# ==============================================================================


@dataclass(frozen=True, slots=True)
class PrototypeNotice:
    """The complete immutable prototype claim shown by every full notice surface."""

    classification: Literal["trusted-local research prototype"]
    limitations: tuple[str, ...]


_TRUSTED_LOCAL_NOTICE = PrototypeNotice(
    classification="trusted-local research prototype",
    limitations=(
        "For one trusted local operator on one workstation.",
        "Only one running copy may change a workspace or QuPath project at a time.",
        "You can exit after a pause only when current work is saved at a safe stopping point.",
        "Work interrupted before that safe pause cannot automatically resume.",
        "Recovery after an operating system crash, power loss, or app crash is not guaranteed.",
        "Interrupted workspaces are not automatically fixed or reused.",
        "This prototype is not protected against a malicious local user or workspace files changed outside the app.",
        "This is not a clinical device and is not certified for production use.",
        "Source images are never overwritten.",
    ),
)


def trusted_local_prototype_notice() -> PrototypeNotice:
    """Return the single canonical notice consumed by full-notice UI surfaces."""

    return _TRUSTED_LOCAL_NOTICE


__all__ = ["PrototypeNotice", "trusted_local_prototype_notice"]
