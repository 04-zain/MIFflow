"""Authority-bound optional routes for successor analyses.

The `BoundSuccessorRoutes` record carries `results_view`, `spatial_handoffs`,
`subject_id_by_sample`, `spatial_results`, `spatial_route` and its remaining declared
fields.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from mif_pipeline.gui.presenters.results import (
    ResultsView,
    SPATIAL_HANDOFF_COLUMNS,
    SPATIAL_HANDOFF_KIND,
    SPATIAL_HANDOFF_SCHEMA,
    SUBJECT_COLUMNS,
)
from mif_pipeline.orchestration.batch_artifacts import (
    BatchArtifactRef,
    validate_artifact_ref,
)

# ==============================================================================
#                               Successor services
# ==============================================================================


class SpatialAnalysisService(Protocol):
    def status(self, handoffs: tuple[object, ...]) -> object: ...

    def run(self, handoffs: tuple[object, ...]) -> object: ...


class PatientAnalysisService(Protocol):
    def status(
        self,
        spatial_results: tuple[object, ...],
        subject_id_by_sample: Mapping[str, str],
    ) -> object: ...

    def run(
        self,
        spatial_results: tuple[object, ...],
        subject_id_by_sample: Mapping[str, str],
    ) -> object: ...


RouteState = Literal[
    "not_ready",
    "handoff_ready",
    "spatial_results_ready",
    "service_available",
]
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_MAX_IMMUTABLE_DEPTH = 64
_MAX_IMMUTABLE_ITEMS = 10_000


def _consume_immutable_budget(
    *,
    label: str,
    depth: int,
    budget: list[int],
) -> None:
    if depth > _MAX_IMMUTABLE_DEPTH:
        raise TypeError(f"{label} exceeds the immutable nesting limit")
    budget[0] -= 1
    if budget[0] < 0:
        raise TypeError(f"{label} exceeds the immutable item limit")


def _deeply_immutable(
    value: object,
    *,
    label: str,
    active: set[int],
    budget: list[int],
    depth: int,
) -> None:
    _consume_immutable_budget(label=label, depth=depth, budget=budget)
    if (
        value is None
        or type(value) in {str, int, float, bool, bytes, object}
        or isinstance(value, (Path, Enum))
    ):
        return
    if type(value) is BatchArtifactRef:
        validate_artifact_ref(value)
        return
    if type(value) is _MAPPING_PROXY_TYPE:
        raise TypeError(f"{label} contains an untrusted mapping proxy alias")

    recursive = type(value) in {tuple, frozenset} or (
        is_dataclass(value) and not isinstance(value, type)
    )
    if not recursive:
        raise TypeError(f"{label} must be deeply immutable")
    identity = id(value)
    if identity in active:
        raise TypeError(f"{label} contains cyclic data")
    active.add(identity)
    try:
        if type(value) in {tuple, frozenset}:
            for item in value:  # type: ignore[union-attr]
                _deeply_immutable(
                    item,
                    label=label,
                    active=active,
                    budget=budget,
                    depth=depth + 1,
                )
            return
        params = getattr(type(value), "__dataclass_params__", None)
        if (
            params is None
            or not params.frozen
            or getattr(type(value), "__slots__", None) is None
            or hasattr(value, "__dict__")
        ):
            raise TypeError(f"{label} must be a frozen slotted typed record")
        for field in fields(value):
            _deeply_immutable(
                getattr(value, field.name),
                label=label,
                active=active,
                budget=budget,
                depth=depth + 1,
            )
    finally:
        active.remove(identity)


def _require_deeply_immutable(value: object, *, label: str) -> object:
    _deeply_immutable(
        value,
        label=label,
        active=set(),
        budget=[_MAX_IMMUTABLE_ITEMS],
        depth=0,
    )
    return value


def _snapshot_status(
    value: object,
    *,
    label: str,
    active: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
) -> object:
    current_active = set() if active is None else active
    remaining = [_MAX_IMMUTABLE_ITEMS] if budget is None else budget
    _consume_immutable_budget(label=label, depth=depth, budget=remaining)
    if (
        value is None
        or type(value) in {str, int, float, bool, bytes, object}
        or isinstance(value, (Path, Enum))
    ):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _require_deeply_immutable(value, label=label)

    identity = id(value)
    if identity in current_active:
        raise TypeError(f"{label} contains cyclic data")
    current_active.add(identity)
    try:
        if isinstance(value, Mapping):
            if not all(type(key) is str for key in value):
                raise TypeError(f"{label} mappings require text keys")
            return MappingProxyType(
                {
                    key: _snapshot_status(
                        item,
                        label=label,
                        active=current_active,
                        budget=remaining,
                        depth=depth + 1,
                    )
                    for key, item in value.items()
                }
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(
                _snapshot_status(
                    item,
                    label=label,
                    active=current_active,
                    budget=remaining,
                    depth=depth + 1,
                )
                for item in value
            )
        if isinstance(value, Set):
            return frozenset(
                _snapshot_status(
                    item,
                    label=label,
                    active=current_active,
                    budget=remaining,
                    depth=depth + 1,
                )
                for item in value
            )
    finally:
        current_active.remove(identity)
    raise TypeError(f"{label} must be deeply immutable or JSON-like data")


@dataclass(frozen=True, slots=True)
class SuccessorRouteStatus:
    """Truthful route availability plus detached real-service status."""

    state: RouteState
    run_available: bool
    service_status: object | None = None

    def __post_init__(self) -> None:
        if self.state not in {
            "not_ready",
            "handoff_ready",
            "spatial_results_ready",
            "service_available",
        }:
            raise ValueError("successor route state is invalid")
        if type(self.run_available) is not bool:
            raise TypeError("successor route availability must be a bool")
        if self.run_available != (self.state == "service_available"):
            raise ValueError("only a real available service can expose a run route")
        object.__setattr__(
            self,
            "service_status",
            _snapshot_status(
                self.service_status,
                label="successor service status",
            ),
        )


def _tuple(value: object, *, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    return value


def _subject_mapping(
    value: object,
    *,
    required: bool,
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("subject mapping must implement Mapping")
    if required and not value:
        raise ValueError("subject mapping must not be empty with spatial results")
    if not all(
        type(sample_id) is str
        and sample_id.strip()
        and type(subject_id) is str
        and subject_id.strip()
        for sample_id, subject_id in value.items()
    ):
        raise ValueError("subject mapping requires non-empty sample and subject IDs")
    return value


def _require_service(value: object, *, label: str) -> None:
    for member_name in ("status", "run"):
        if not callable(getattr(value, member_name, None)):
            raise TypeError(f"{label} service requires callable status and run members")


@dataclass(frozen=True, slots=True)
class SuccessorServiceRegistry:
    """Register concrete optional services and forward exact authorized inputs."""

    spatial_service: SpatialAnalysisService | None = None
    patient_service: PatientAnalysisService | None = None

    def __post_init__(self) -> None:
        if self.spatial_service is not None:
            _require_service(self.spatial_service, label="spatial")
        if self.patient_service is not None:
            _require_service(self.patient_service, label="patient")

    @property
    def spatial_available(self) -> bool:
        return self.spatial_service is not None

    @property
    def patient_available(self) -> bool:
        return self.patient_service is not None

    def spatial_route(self, handoffs: tuple[object, ...]) -> SuccessorRouteStatus:
        exact_handoffs = _tuple(handoffs, label="spatial handoffs")
        if not exact_handoffs:
            return SuccessorRouteStatus("not_ready", False)
        if self.spatial_service is None:
            return SuccessorRouteStatus("handoff_ready", False)
        return SuccessorRouteStatus(
            "service_available",
            True,
            self.spatial_service.status(exact_handoffs),
        )

    def run_spatial(self, handoffs: tuple[object, ...]) -> object:
        exact_handoffs = _tuple(handoffs, label="spatial handoffs")
        if self.spatial_service is None:
            raise LookupError("no spatial analysis service is registered")
        if not exact_handoffs:
            raise ValueError("spatial analysis requires at least one handoff")
        return self.spatial_service.run(exact_handoffs)

    def patient_route(
        self,
        spatial_results: tuple[object, ...],
        subject_id_by_sample: Mapping[str, str],
    ) -> SuccessorRouteStatus:
        exact_results = _tuple(spatial_results, label="spatial results")
        _require_deeply_immutable(exact_results, label="spatial results")
        exact_subjects = _subject_mapping(
            subject_id_by_sample,
            required=bool(exact_results),
        )
        if not exact_results:
            return SuccessorRouteStatus("not_ready", False)
        if self.patient_service is None:
            return SuccessorRouteStatus("spatial_results_ready", False)
        return SuccessorRouteStatus(
            "service_available",
            True,
            self.patient_service.status(exact_results, exact_subjects),
        )

    def run_patient(
        self,
        spatial_results: tuple[object, ...],
        subject_id_by_sample: Mapping[str, str],
    ) -> object:
        exact_results = _tuple(spatial_results, label="spatial results")
        _require_deeply_immutable(exact_results, label="spatial results")
        exact_subjects = _subject_mapping(
            subject_id_by_sample,
            required=bool(exact_results),
        )
        if self.patient_service is None:
            raise LookupError("no patient analysis service is registered")
        if not exact_results:
            raise ValueError("patient analysis requires spatial results")
        return self.patient_service.run(exact_results, exact_subjects)


@dataclass(frozen=True, slots=True, init=False)
class BoundSuccessorRoutes:
    """One inseparable displayed result and optional-route authority snapshot."""

    results_view: ResultsView
    spatial_handoffs: tuple[BatchArtifactRef, ...]
    subject_id_by_sample: Mapping[str, str]
    spatial_results: tuple[object, ...]
    spatial_route: SuccessorRouteStatus
    patient_route: SuccessorRouteStatus

    @classmethod
    def _create(
        cls,
        *,
        results_view: ResultsView,
        spatial_handoffs: tuple[BatchArtifactRef, ...],
        subject_id_by_sample: Mapping[str, str],
        spatial_results: tuple[object, ...],
        spatial_route: SuccessorRouteStatus,
        patient_route: SuccessorRouteStatus,
    ) -> "BoundSuccessorRoutes":
        value = object.__new__(cls)
        object.__setattr__(value, "results_view", results_view)
        object.__setattr__(value, "spatial_handoffs", spatial_handoffs)
        object.__setattr__(value, "subject_id_by_sample", subject_id_by_sample)
        object.__setattr__(value, "spatial_results", spatial_results)
        object.__setattr__(value, "spatial_route", spatial_route)
        object.__setattr__(value, "patient_route", patient_route)
        return value


def _subjects_from_view(view: ResultsView) -> Mapping[str, str]:
    if view.subject_table.columns != SUBJECT_COLUMNS:
        raise ValueError("subject table does not match the result sample schema")
    subjects: dict[str, str] = {}
    for row in view.subject_table.rows:
        sample_id, subject_id = row.values
        if (
            type(sample_id) is not str
            or not sample_id.strip()
            or type(subject_id) is not str
            or not subject_id.strip()
        ):
            raise ValueError("subject table contains an invalid sample mapping")
        if sample_id in subjects:
            raise ValueError("subject table contains a duplicate sample mapping")
        subjects[sample_id] = subject_id
    output_samples = {str(row.values[0]) for row in view.output_table.rows}
    if set(subjects) != output_samples:
        raise ValueError("subject sample mapping differs from displayed samples")
    return MappingProxyType(dict(sorted(subjects.items())))


def _canonical_handoffs(
    view: ResultsView,
    values: object,
) -> tuple[BatchArtifactRef, ...]:
    if type(values) is not tuple or not all(type(item) is BatchArtifactRef for item in values):
        raise TypeError("bound spatial handoffs require a BatchArtifactRef tuple")
    handoffs = tuple(sorted(values, key=lambda item: str(item.scope.sample_id)))
    for item in handoffs:
        validate_artifact_ref(item)
        if item.artifact_kind != SPATIAL_HANDOFF_KIND:
            raise ValueError("bound payload contains a non-spatial handoff")
        if (item.schema_name, item.schema_version) != SPATIAL_HANDOFF_SCHEMA:
            raise ValueError("bound payload contains a non-v3 spatial handoff")
        if item.locator is None or item.sha256 is None or item.byte_count is None:
            raise ValueError("bound spatial handoffs must be materialized")
        if (
            item.scope.validation_domain_id is None
            or item.scope.sample_id is None
            or item.scope.slide_id is None
        ):
            raise ValueError("bound spatial handoffs require exact scope")
        if len(item.configuration_bindings) != 1:
            raise ValueError("bound spatial handoffs require one binding")
    for field_name, field_values in (
        ("artifact ID", tuple(item.artifact_id for item in handoffs)),
        ("locator", tuple(item.locator for item in handoffs)),
        ("sample scope", tuple(item.scope.sample_id for item in handoffs)),
    ):
        if len(set(field_values)) != len(field_values):
            raise ValueError(f"bound spatial handoffs repeat a {field_name}")
    projected = tuple(
        (
            item.scope.sample_id,
            item.artifact_id,
            item.artifact_kind,
            item.schema_name,
            item.schema_version,
            None if item.locator is None else str(item.locator),
            item.sha256,
            item.byte_count,
            item.configuration_bindings[0].resolved_config_sha256,
            item.artifact_ref_sha256,
        )
        for item in handoffs
    )
    displayed = tuple(row.values for row in view.spatial_handoff_table.rows)
    if view.spatial_handoff_table.columns != SPATIAL_HANDOFF_COLUMNS or projected != displayed:
        raise ValueError("payload differs from displayed spatial handoffs")
    return handoffs


def bind_successor_routes(
    results_view: ResultsView,
    *,
    spatial_handoffs: tuple[BatchArtifactRef, ...],
    spatial_results: tuple[object, ...],
    registry: SuccessorServiceRegistry,
) -> BoundSuccessorRoutes:
    """Bind displayed evidence and registered route status without execution."""

    if type(results_view) is not ResultsView:
        raise TypeError("successor route binding requires a ResultsView")
    if type(registry) is not SuccessorServiceRegistry:
        raise TypeError("successor route binding requires a service registry")
    canonical_handoffs = _canonical_handoffs(results_view, spatial_handoffs)
    subjects = _subjects_from_view(results_view)
    exact_results = _tuple(spatial_results, label="spatial results")
    _require_deeply_immutable(exact_results, label="spatial results")
    if exact_results and not results_view.handoff_ready:
        raise ValueError("spatial results require a ready displayed handoff")
    if exact_results and not subjects:
        raise ValueError("spatial results require a non-empty subject mapping")

    spatial_route = (
        registry.spatial_route(canonical_handoffs)
        if results_view.handoff_ready
        else SuccessorRouteStatus("not_ready", False)
    )
    patient_route = registry.patient_route(exact_results, subjects)
    return BoundSuccessorRoutes._create(
        results_view=results_view,
        spatial_handoffs=canonical_handoffs,
        subject_id_by_sample=subjects,
        spatial_results=exact_results,
        spatial_route=spatial_route,
        patient_route=patient_route,
    )


__all__ = [
    "BoundSuccessorRoutes",
    "PatientAnalysisService",
    "RouteState",
    "SpatialAnalysisService",
    "SuccessorRouteStatus",
    "SuccessorServiceRegistry",
    "bind_successor_routes",
]
