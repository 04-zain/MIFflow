"""Dependency-neutral Linux containment protocol package."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from importlib import import_module

from .protocol import (
    BrokerCapabilitiesV1,
    BrokerDrainProofV1,
    BrokerExecutableRegistrationV1,
    BrokerSecurityProfileV1,
    BrokerStopIntentRecordV1,
    CgroupDeletedAfterStopProofV1,
    CgroupDirectoryIdentityV1,
    CgroupEventsZeroProofV1,
    CgroupPathComponentIdentityV1,
    MachineRebootProofV1,
    ScopeAnchorIdentityV1,
    StartupCertificationSummaryV1,
    SystemdStopJobEvidenceV1,
)

_CLIENT_EXPORTS = frozenset(
    {
        "AttachRequestV1",
        "ContainmentAmbiguousError",
        "ContainmentBrokerClient",
        "ContainmentClientError",
        "ContainmentCompatibilityExpectationV1",
        "ContainmentConflictError",
        "ContainmentIncompatibleError",
        "ContainmentIndeterminateError",
        "ContainmentRejectedError",
        "ContainmentUnavailableError",
        "GateBrokerClient",
        "LinuxContainmentRecovery",
        "preflight_linux_containment",
    }
)


# ==============================================================================
#                               Public interfaces
# ==============================================================================


def __getattr__(name: str) -> object:
    if name not in _CLIENT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".client", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "AttachRequestV1",
    "BrokerCapabilitiesV1",
    "BrokerDrainProofV1",
    "BrokerExecutableRegistrationV1",
    "BrokerSecurityProfileV1",
    "BrokerStopIntentRecordV1",
    "ContainmentAmbiguousError",
    "ContainmentBrokerClient",
    "ContainmentClientError",
    "ContainmentCompatibilityExpectationV1",
    "ContainmentConflictError",
    "ContainmentIncompatibleError",
    "ContainmentIndeterminateError",
    "ContainmentRejectedError",
    "ContainmentUnavailableError",
    "CgroupDeletedAfterStopProofV1",
    "CgroupDirectoryIdentityV1",
    "CgroupEventsZeroProofV1",
    "CgroupPathComponentIdentityV1",
    "MachineRebootProofV1",
    "GateBrokerClient",
    "LinuxContainmentRecovery",
    "ScopeAnchorIdentityV1",
    "StartupCertificationSummaryV1",
    "SystemdStopJobEvidenceV1",
    "preflight_linux_containment",
]
