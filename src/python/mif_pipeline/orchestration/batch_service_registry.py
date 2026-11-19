"""Closed route proof for batch-service adapter composition.

This registry deliberately remains separate from the accepted raw-direct
``RuntimeStageAdapterRegistry``. It validates the route selected by owning composition code
before the service can dispatch an adapter.

The `Task15RuntimeAdapterBinding` record carries `spec`, `route`, `adapter`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from inspect import getattr_static
from typing import Literal, Protocol, runtime_checkable

from .batch_artifacts import (
    BatchArtifactError,
    BatchNodeExecutionContext,
    DispatchMode,
    StageAdapterRegistry,
    StageAdapterSpec,
)
from .batch_state import (
    BatchRawNodeOutcome,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)

RuntimeAdapterRoute = Literal["raw_direct", "owner_committed", "project_current_image"]

_RAW_DIRECT_RESOURCE_KINDS = frozenset(
    {
        "bioformats_conversion",
        "spectral_preprocessing",
        "scientific_compute",
        "qupath_image_read",
    }
)
_OWNER_COMMITTED_RESOURCE_KINDS = frozenset(
    {
        "project_catalogue_mutation",
        "shared_classifier_mutation",
        "qupath_image_write",
    }
)
_PROJECT_CURRENT_IMAGE_RESOURCE_KINDS = frozenset({"qupath_image_read", "qupath_image_write"})
_ROUTE_METHODS = {
    "raw_direct": "execute_raw",
    "owner_committed": "execute_committed",
    "project_current_image": "execute_project_current_image",
}


# ==============================================================================
#                             Batch service registry
# ==============================================================================


@runtime_checkable
class RawDirectAdapter(Protocol):
    spec: StageAdapterSpec

    def execute_raw(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
    ) -> BatchRawNodeOutcome:
        """Return one uncommitted owner outcome for scheduler publication."""


@runtime_checkable
class OwnerCommittedAdapter(Protocol):
    spec: StageAdapterSpec

    def execute_committed(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> ScheduledNodeResult:
        """Execute and publish one single-dispatch terminal result."""


@runtime_checkable
class ProjectCurrentImageAdapter(Protocol):
    spec: StageAdapterSpec

    def execute_project_current_image(
        self,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> tuple[ScheduledNodeResult, ...]:
        """Execute and publish one current-image dispatch group."""


@dataclass(frozen=True, slots=True)
class Task15RuntimeAdapterBinding:
    spec: StageAdapterSpec
    route: RuntimeAdapterRoute
    adapter: RawDirectAdapter | OwnerCommittedAdapter | ProjectCurrentImageAdapter


def _has_exclusive_callable_route_method(adapter: object, route: RuntimeAdapterRoute) -> bool:
    """Require one callable, statically declared route method and no other route."""

    selected_name = _ROUTE_METHODS[route]
    selected = getattr_static(type(adapter), selected_name, None)
    return callable(selected) and all(
        getattr_static(type(adapter), name, None) is None
        for candidate_route, name in _ROUTE_METHODS.items()
        if candidate_route != route
    )


def _route_is_compatible(binding: Task15RuntimeAdapterBinding) -> bool:
    spec = binding.spec
    adapter = binding.adapter
    if binding.route == "raw_direct":
        return (
            spec.dispatch_mode is DispatchMode.SINGLE
            and spec.resource_kind in _RAW_DIRECT_RESOURCE_KINDS
            and isinstance(adapter, RawDirectAdapter)
            and _has_exclusive_callable_route_method(adapter, binding.route)
        )
    if binding.route == "owner_committed":
        return (
            spec.dispatch_mode is DispatchMode.SINGLE
            and spec.resource_kind in _OWNER_COMMITTED_RESOURCE_KINDS
            and isinstance(adapter, OwnerCommittedAdapter)
            and _has_exclusive_callable_route_method(adapter, binding.route)
        )
    if binding.route == "project_current_image":
        return (
            spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
            and spec.resource_kind in _PROJECT_CURRENT_IMAGE_RESOURCE_KINDS
            and isinstance(adapter, ProjectCurrentImageAdapter)
            and _has_exclusive_callable_route_method(adapter, binding.route)
        )
    return False


@dataclass(frozen=True, slots=True)
class Task15RuntimeStageRegistry:
    stage_registry: StageAdapterRegistry
    bindings: tuple[Task15RuntimeAdapterBinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_registry, StageAdapterRegistry):
            raise BatchArtifactError("stage_registry must be a StageAdapterRegistry")
        bindings = tuple(self.bindings)
        if not all(isinstance(item, Task15RuntimeAdapterBinding) for item in bindings):
            raise BatchArtifactError("runtime registry requires typed route bindings")
        keys = tuple((item.spec.node_kind, item.spec.node_version) for item in bindings)
        if len(set(keys)) != len(keys):
            raise BatchArtifactError("runtime registry has duplicate node-kind/version bindings")
        expected = {(item.node_kind, item.node_version): item for item in self.stage_registry.specs}
        if set(keys) != set(expected):
            raise BatchArtifactError("runtime registry must cover every closed stage specification")
        for binding in bindings:
            expected_spec = expected[(binding.spec.node_kind, binding.spec.node_version)]
            if binding.spec is not expected_spec or binding.adapter.spec is not expected_spec:
                raise BatchArtifactError(
                    "route binding must retain the exact registered stage specification"
                )
            if not _route_is_compatible(binding):
                raise BatchArtifactError(
                    "route binding is incompatible with its adapter or stage facts"
                )
        object.__setattr__(
            self,
            "bindings",
            tuple(sorted(bindings, key=lambda item: (item.spec.node_kind, item.spec.node_version))),
        )

    def resolve(self, node_kind: str, node_version: str, /) -> Task15RuntimeAdapterBinding:
        spec = self.stage_registry.resolve(node_kind, node_version)
        for binding in self.bindings:
            if binding.spec is spec:
                return binding
        raise BatchArtifactError(
            f"no runtime route binding for {spec.node_kind}/{spec.node_version}"
        )
