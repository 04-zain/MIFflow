"""Process-local stage-owner capabilities used by pre-runtime codecs.

The codec registry is closed before a project runtime exists. This small seam lets a service
publish one already-authenticated scientific owner for an exact attempt after the runtime
has opened, while the codec retains only the resolver view."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from threading import Lock
from collections.abc import Mapping
from types import MappingProxyType

from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    _execution_context_authority_material,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_resources import validate_sha256
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes

# ==============================================================================
#                        Project stage owner capabilities
# ==============================================================================


class ProjectStageOwnerCapabilityError(RuntimeError):
    """A stage-owner capability publication or resolution is invalid."""


@dataclass(frozen=True, slots=True)
class _CapabilityKey:
    node_id: str
    attempt_id: str
    project_entry_id: str
    target_binding: tuple[str, str, str, str, int, str, str]
    execution_context_sha256: str
    input_artifact_set_sha256: str


@dataclass(slots=True)
class _CapabilityState:
    active: dict[_CapabilityKey, object]
    retired: set[_CapabilityKey]
    lock: Lock


_VIEW_ISSUER = object()
_APPLICABLE_CAPABILITY_ISSUER = object()


def _freeze_applicable_json(value: object) -> object:
    """Freeze our scientific JSON once before issuing its process-local owner."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_applicable_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_applicable_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True, init=False)
class SemanticEvidenceStageOwnerCapability:
    """Sealed process-local owners for one candidate-evidence Task-10 attempt."""

    current: object
    resolved_sample: object
    semantic_branch: object
    phenotype_view_attestation: object
    analysis_frame: object
    accepted_domain: object
    measurements: object
    semantic_profile_authorities: tuple[object, ...]
    profile_bindings: tuple[object, ...]
    profile_bytes: tuple[bytes, ...]
    raw_inventory: Mapping[str, object]
    request_body: Mapping[str, object]
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic-evidence capability is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("semantic-evidence capability is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class SemanticFinalizationStageOwnerCapability:
    """Sealed process-local owners for one semantic-finalization Task-10 attempt."""

    current: object
    resolved_sample: object
    semantic_evaluation: object
    final_count_cap: object
    raw_inventory: object
    analysis_frame: object
    accepted_domain: object
    measurements: object
    request_body: Mapping[str, object]
    result_artifact_bytes: bytes
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic-finalization capability is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("semantic-finalization capability is nonserializable")


def _issue_applicable_capability(cls: type, **values: object) -> object:
    # i) Our stage owners have already admitted their scientific records.
    # ii) We freeze their JSON at issuance; the attempt registry retains the
    # current head, input-set and target binding for subsequent lookups.
    result = object.__new__(cls)
    for name in cls.__dataclass_fields__:
        object.__setattr__(result, name, _freeze_applicable_json(values.get(name)))
    object.__setattr__(result, "_issuer", _APPLICABLE_CAPABILITY_ISSUER)
    return result


def _validate_applicable_capability(value: object, cls: type) -> object:
    if type(value) is not cls or value._issuer is not _APPLICABLE_CAPABILITY_ISSUER:
        raise ProjectStageOwnerCapabilityError("applicable capability is invalid")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ProjectStageOwnerCapabilityError(f"{field} must be non-empty text")
    return value


def _binding_key(value: object) -> tuple[str, str, str, str, int, str, str]:
    if type(value) is not AnalysisDomainBinding:
        raise ProjectStageOwnerCapabilityError(
            "capability target requires one exact analysis-domain binding"
        )
    return (
        value.project_entry_id,
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.analysis_domain_revision_sha256,
        value.domain_set_sha256,
    )


def _key(context: object) -> _CapabilityKey:
    """Close the only callback facts needed for one capability lookup.

    The private publication context is deliberately structural: 's
    request callback and result callback carry different issuer-owned record
    types, but both project these exact immutable facts."""

    try:
        authorization = context.authorization
        dispatch = authorization.dispatch_binding
        bindings = tuple(context.analysis_domain_bindings)
        project_entry_ids = tuple(dispatch.project_entry_ids)
    except (AttributeError, TypeError) as exc:
        raise ProjectStageOwnerCapabilityError(
            "capability context does not expose exact attempt authority"
        ) from exc
    if len(bindings) != 1 or len(project_entry_ids) != 1:
        raise ProjectStageOwnerCapabilityError(
            "capability context requires one project entry and target domain"
        )
    binding = bindings[0]
    binding_material = _binding_key(binding)
    project_entry_id = _text(project_entry_ids[0], "project_entry_id")
    if binding_material[0] != project_entry_id:
        raise ProjectStageOwnerCapabilityError(
            "capability project entry differs from target domain"
        )
    try:
        execution_context_sha256 = validate_sha256(
            context.execution_context_sha256,
            "execution_context_sha256",
        )
        input_artifact_set_sha256 = validate_sha256(
            authorization.input_artifact_set_sha256,
            "input_artifact_set_sha256",
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProjectStageOwnerCapabilityError(
            "capability context digest authority is invalid"
        ) from exc
    return _CapabilityKey(
        node_id=_text(context.node_id, "node_id"),
        attempt_id=_text(authorization.attempt_id, "attempt_id"),
        project_entry_id=project_entry_id,
        target_binding=binding_material,
        execution_context_sha256=execution_context_sha256,
        input_artifact_set_sha256=input_artifact_set_sha256,
    )


@dataclass(frozen=True, slots=True)
class _PublishedCapabilityContext:
    node_id: str
    authorization: object
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    execution_context_sha256: str


def _publication_context(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    *,
    analysis_domain_binding: AnalysisDomainBinding | None = None,
) -> _PublishedCapabilityContext:
    """Project a state-issued execution capability into the private key shape."""

    validated = validate_current_execution_authority(current, expected_context=context)
    bindings = validated.analysis_domain_bindings
    if analysis_domain_binding is not None:
        if analysis_domain_binding not in bindings:
            raise ProjectStageOwnerCapabilityError(
                "capability lookup target is absent from current authority"
            )
        bindings = (analysis_domain_binding,)
    return _PublishedCapabilityContext(
        node_id=context.node_id,
        authorization=context.authorization,
        analysis_domain_bindings=bindings,
        execution_context_sha256=validated.execution_context_sha256,
    )


def _callback_key_prefix(
    context: BatchNodeExecutionContext,
    *,
    project_entry_id: str,
    analysis_domain_binding: AnalysisDomainBinding,
) -> tuple[
    str,
    str,
    str,
    tuple[str, str, str, str, int, str, str],
    str,
    str,
]:
    if type(context) is not BatchNodeExecutionContext:
        raise ProjectStageOwnerCapabilityError(
            "capability callback requires one exact execution context"
        )
    entry = _text(project_entry_id, "project_entry_id")
    binding = _binding_key(analysis_domain_binding)
    if binding[0] != entry:
        raise ProjectStageOwnerCapabilityError(
            "capability callback target binding differs from project entry"
        )
    authorization = context.authorization
    execution_context_sha256 = hashlib.sha256(
        canonical_json_bytes(_execution_context_authority_material(context))
    ).hexdigest()
    return (
        context.node_id,
        authorization.attempt_id,
        entry,
        binding,
        execution_context_sha256,
        authorization.input_artifact_set_sha256,
    )


def _callback_attempt_prefix(
    context: BatchNodeExecutionContext,
) -> tuple[str, str, str, str, str]:
    """Derive the real attempt without trusting a callback target."""

    if type(context) is not BatchNodeExecutionContext:
        raise ProjectStageOwnerCapabilityError(
            "capability callback requires one exact execution context"
        )
    project_entry_ids = context.authorization.dispatch_binding.project_entry_ids
    if len(project_entry_ids) != 1:
        raise ProjectStageOwnerCapabilityError(
            "capability attempt requires one dispatched project entry"
        )
    execution_context_sha256 = hashlib.sha256(
        canonical_json_bytes(_execution_context_authority_material(context))
    ).hexdigest()
    return (
        context.node_id,
        context.authorization.attempt_id,
        project_entry_ids[0],
        execution_context_sha256,
        context.authorization.input_artifact_set_sha256,
    )


class ProjectStageOwnerCapabilityPublisher:
    """Issuer-only publication and abort view for one process-local seam."""

    __slots__ = ("_state", "_issuer")

    def __new__(cls, *args: object, **kwargs: object):
        if kwargs.pop("_issuer", None) is not _VIEW_ISSUER or args or kwargs:
            raise TypeError("project-stage owner capability publisher is issuer-only")
        return object.__new__(cls)

    def __init__(self, *, _issuer: object) -> None:
        del _issuer

    def __copy__(self) -> object:
        raise TypeError("project-stage owner capability publisher is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("project-stage owner capability publisher is noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("project-stage owner capability publisher is nonserializable")

    def publish(self, capability: object, *, context: object) -> None:
        if capability is None:
            raise ProjectStageOwnerCapabilityError("capability must be present")
        key = _key(context)
        with self._state.lock:
            if key in self._state.retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if key in self._state.active:
                raise ProjectStageOwnerCapabilityError("duplicate capability publication")
            self._state.active[key] = capability

    def _publish_current(
        self,
        capability: object,
        *,
        context: BatchNodeExecutionContext,
        current: BatchCurrentExecutionAuthority,
        analysis_domain_binding: AnalysisDomainBinding | None = None,
    ) -> None:
        self.publish(
            capability,
            context=_publication_context(
                context,
                current,
                analysis_domain_binding=analysis_domain_binding,
            ),
        )

    def abort(self, context: object) -> None:
        key = _key(context)
        with self._state.lock:
            if key in self._state.retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if key not in self._state.active:
                raise ProjectStageOwnerCapabilityError("capability was not published")
            del self._state.active[key]
            self._state.retired.add(key)

    def _abort_current(
        self,
        *,
        context: BatchNodeExecutionContext,
        current: BatchCurrentExecutionAuthority,
    ) -> None:
        self.abort(_publication_context(context, current))


class ProjectStageOwnerCapabilityResolver:
    """Issuer-only resolution and terminal-retirement view for codecs."""

    __slots__ = ("_state", "_issuer")

    def __new__(cls, *args: object, **kwargs: object):
        if kwargs.pop("_issuer", None) is not _VIEW_ISSUER or args or kwargs:
            raise TypeError("project-stage owner capability resolver is issuer-only")
        return object.__new__(cls)

    def __init__(self, *, _issuer: object) -> None:
        del _issuer

    def __copy__(self) -> object:
        raise TypeError("project-stage owner capability resolver is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("project-stage owner capability resolver is noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("project-stage owner capability resolver is nonserializable")

    def require_paired_publisher(
        self,
        publisher: ProjectStageOwnerCapabilityPublisher,
    ) -> None:
        """Authenticate the publisher issued for this exact resolver seam."""

        if (
            type(self) is not ProjectStageOwnerCapabilityResolver
            or self._issuer is not _VIEW_ISSUER
            or type(publisher) is not ProjectStageOwnerCapabilityPublisher
            or publisher._issuer is not _VIEW_ISSUER
            or publisher._state is not self._state
        ):
            raise ProjectStageOwnerCapabilityError(
                "project-stage capability publisher/resolver pair differs"
            )

    def resolve(self, context: object) -> object:
        key = _key(context)
        with self._state.lock:
            if key in self._state.retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            try:
                return self._state.active[key]
            except KeyError as exc:
                raise ProjectStageOwnerCapabilityError(
                    "capability was not published for this attempt"
                ) from exc

    def _resolve_for_target(
        self,
        context: BatchNodeExecutionContext,
        *,
        project_entry_id: str,
        analysis_domain_binding: AnalysisDomainBinding,
    ) -> object:
        prefix = _callback_key_prefix(
            context,
            project_entry_id=project_entry_id,
            analysis_domain_binding=analysis_domain_binding,
        )
        with self._state.lock:
            active = tuple(
                (key, capability)
                for key, capability in self._state.active.items()
                if (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.target_binding,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
            )
            retired = tuple(
                key
                for key in self._state.retired
                if (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.target_binding,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
            )
            if retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if len(active) != 1:
                raise ProjectStageOwnerCapabilityError(
                    "capability was not published for this exact target"
                )
            return active[0][1]

    def _retire_for_target(
        self,
        context: BatchNodeExecutionContext,
        *,
        project_entry_id: str,
        analysis_domain_binding: AnalysisDomainBinding,
    ) -> None:
        prefix = _callback_key_prefix(
            context,
            project_entry_id=project_entry_id,
            analysis_domain_binding=analysis_domain_binding,
        )
        with self._state.lock:
            active = tuple(
                key
                for key in self._state.active
                if (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.target_binding,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
            )
            if not active and any(
                (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.target_binding,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
                for key in self._state.retired
            ):
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if len(active) != 1:
                raise ProjectStageOwnerCapabilityError(
                    "capability was not published for this exact target"
                )
            key = active[0]
            del self._state.active[key]
            self._state.retired.add(key)

    def _retire_for_attempt(self, context: BatchNodeExecutionContext) -> None:
        """Retire the context-owned attempt without accepting caller target facts."""

        prefix = _callback_attempt_prefix(context)
        with self._state.lock:
            active = tuple(
                key
                for key in self._state.active
                if (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
            )
            retired = tuple(
                key
                for key in self._state.retired
                if (
                    key.node_id,
                    key.attempt_id,
                    key.project_entry_id,
                    key.execution_context_sha256,
                    key.input_artifact_set_sha256,
                )
                == prefix
            )
            if not active and retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if len(active) != 1:
                raise ProjectStageOwnerCapabilityError(
                    "capability was not published for this exact attempt"
                )
            key = active[0]
            del self._state.active[key]
            self._state.retired.add(key)

    def retire_terminally(self, context: object) -> None:
        key = _key(context)
        with self._state.lock:
            if key in self._state.retired:
                raise ProjectStageOwnerCapabilityError("capability attempt is retired")
            if key not in self._state.active:
                raise ProjectStageOwnerCapabilityError("capability was not published")
            del self._state.active[key]
            self._state.retired.add(key)


def create_project_stage_owner_capability_seam() -> tuple[
    ProjectStageOwnerCapabilityPublisher,
    ProjectStageOwnerCapabilityResolver,
]:
    """Create paired least-authority views over one empty capability store."""

    state = _CapabilityState(active={}, retired=set(), lock=Lock())
    publisher = ProjectStageOwnerCapabilityPublisher(_issuer=_VIEW_ISSUER)
    resolver = ProjectStageOwnerCapabilityResolver(_issuer=_VIEW_ISSUER)
    object.__setattr__(publisher, "_state", state)
    object.__setattr__(publisher, "_issuer", _VIEW_ISSUER)
    object.__setattr__(resolver, "_state", state)
    object.__setattr__(resolver, "_issuer", _VIEW_ISSUER)
    return publisher, resolver


__all__ = [
    "ProjectStageOwnerCapabilityError",
    "ProjectStageOwnerCapabilityPublisher",
    "ProjectStageOwnerCapabilityResolver",
    "SemanticEvidenceStageOwnerCapability",
    "SemanticFinalizationStageOwnerCapability",
    "create_project_stage_owner_capability_seam",
]
