"""Authenticated locatorless batch classifier-inference result owners.

The `ValidatedSecondaryClassifierInferenceResultArtifactRefProjection` record carries
`artifact_ref`, `canonical_image_id`, `canonical_image_sha256`, `tissue_generation_id`,
`section_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final, Literal

from mif_pipeline.pipeline_core import (
    AnalysisConfig,
    ROIAnalysisError,
    StageStatus,
)
from mif_pipeline.pipeline_core.config import (
    PrimaryClassifierSettings, SecondaryStructureSpec, require_primary_settings_compatible,
)
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.compartment_analysis.count_qc import PrimaryCountDecision
from mif_pipeline.datasets.roi_inventory import (
    RoiInventoryRef, RoiInventoryError, parse_roi_inventory_ref, open_roi_inventory,
    roi_inventory_member,
)

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    BatchAnalysisPreflightError,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionResult,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from .batch_manifest import ResolvedBatchSample
from .batch_classifier_inference_contracts import (
    primary_classifier_inference_result_identity_contract,
    secondary_classifier_inference_result_identity_contract,
)
from .project_stage_plan import (
    ProjectStageRecordError,
    ProjectStageCommittedExchangeReopener,
    ProjectStageTargetExecutionEvidence,
)
from .workflow import (
    CurrentImageScientificInvocation,
    PrimaryClassifierDisposition,
    _primary_count_decision,
    _primary_settings_for_invocation,
    secondary_structures_for_invocation,
    validate_classifier_output_scope,
)

_SCHEMA_NAME: Final[str] = "roi-generation-manifest"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-classifier-inference/v1"
_MANIFEST_SCHEMA: Final[str] = "roi-generation-manifest/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_APPLICATION_FIELDS = frozenset(
    {
        "classifier_record_set_ref_sha256",
        "classifier_record_set_sha256",
        "output_id",
        "classifier_record_sha256",
        "classifier_id",
        "classifier_version",
        "classifier_kind",
        "application_request_sha256",
        "target_project_identity_sha256",
        "deployment_binding_set_sha256",
        "target_evidence_set_sha256",
        "project_relative_path",
        "source_model_path",
        "native_classifier_sha256",
        "native_classifier_byte_count",
        "pixel_classifier_definition",
        "pixel_classifier_definition_sha256",
    }
)
_COMMON_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "slide_id",
        "run_id",
        "generation_ids",
        "generation_id",
        "canonical_image_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "analysis_domain_path_object_id",
        "structure_id",
        "eligible_parent_id",
        "eligible_parent_ids",
        "action",
        "action_context",
        "classifier_reuse_decisions",
        "confirmed_marker_rules",
        "registration_context",
        "classifier_review_status",
        "domain_set_sha256",
        "analysis_domain_revision_evidence",
        "classifier_application",
        "sample_id",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "generations",
        "object_outputs",
    }
)
_PARENT_SOURCE_FIELDS = frozenset({
    "eligible_parent_id", "source_kind", "source_result_ref_sha256", "producer_attempt_id",
    "roi_inventory", "analysis_domain_path_object_id",
})


# ==============================================================================
#                      Batch classifier inference artifacts
# ==============================================================================


def _validate_classifier_inference_input_configuration_bindings(
    bindings_by_kind: Mapping[str, Sequence[ArtifactConfigurationBinding]],
    target_binding: ArtifactConfigurationBinding,
) -> None:
    """Keep target inputs exact while accepting an authenticated aggregate set."""

    if not isinstance(target_binding, ArtifactConfigurationBinding):
        raise BatchArtifactError("classifier target binding must be typed")
    if "classifier_record_set" not in bindings_by_kind:
        raise BatchArtifactError("classifier record-set target binding is missing")
    for artifact_kind, raw_bindings in bindings_by_kind.items():
        bindings = tuple(raw_bindings)
        if not all(isinstance(item, ArtifactConfigurationBinding) for item in bindings):
            raise BatchArtifactError("classifier input configuration binding is invalid")
        if artifact_kind in {"classifier_record_set", "training_plan_set"}:
            if sum(item == target_binding for item in bindings) != 1:
                raise BatchArtifactError(
                    "classifier selection lacks the exact current target binding"
                )
        elif not bindings or any(item != target_binding for item in bindings) or (
            artifact_kind != "secondary_inference_result" and len(bindings) != 1
        ):
            raise BatchArtifactError("target-scoped classifier input configuration binding differs")


@dataclass(frozen=True, slots=True)
class RoiParentSource:
    """One selected parent class bound to its accepted domain or classifier output.

    A table source uses its producer attempt to derive the sole allowed member.
    The domain case names the accepted domain PathObject and has no table/attempt.
    Native code compares selected table rows with the current saved hierarchy.
    """

    eligible_parent_id: str
    source_kind: Literal["analysis_domain", "primary", "secondary"]
    source_result_ref_sha256: str
    producer_attempt_id: str | None
    roi_inventory: RoiInventoryRef | None
    analysis_domain_path_object_id: str | None

    def __post_init__(self) -> None:
        _text(self.eligible_parent_id, "eligible_parent_id")
        _sha256(self.source_result_ref_sha256, "source_result_ref_sha256")
        if self.source_kind == "analysis_domain":
            if self.eligible_parent_id != "AnalysisDomain" or self.producer_attempt_id is not None or self.roi_inventory is not None:
                raise BatchArtifactError("analysis-domain parent cannot carry classifier table authority")
            _text(self.analysis_domain_path_object_id, "analysis_domain_path_object_id")
        elif self.source_kind in {"primary", "secondary"}:
            if self.analysis_domain_path_object_id is not None or type(self.roi_inventory) is not RoiInventoryRef:
                raise BatchArtifactError("classifier parent requires only its typed ROI table")
            producer = _text(self.producer_attempt_id, "producer_attempt_id")
            if self.roi_inventory.path != roi_inventory_member(producer):
                raise BatchArtifactError("parent ROI table member differs from its producer")
        else:
            raise BatchArtifactError("parent source kind is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "eligible_parent_id": self.eligible_parent_id, "source_kind": self.source_kind,
            "source_result_ref_sha256": self.source_result_ref_sha256,
            "producer_attempt_id": self.producer_attempt_id,
            "roi_inventory": None if self.roi_inventory is None else self.roi_inventory.to_mapping(),
            "analysis_domain_path_object_id": self.analysis_domain_path_object_id,
        }


def parse_roi_parent_sources(value: object) -> tuple[RoiParentSource, ...]:
    """Decode the bounded, ordered parent selection without opening any table."""
    sources = []
    for raw in _sequence(value, "parent_sources"):
        row = dict(_closed(raw, _PARENT_SOURCE_FIELDS, "parent source"))
        if row["roi_inventory"] is not None:
            try:
                row["roi_inventory"] = parse_roi_inventory_ref(row["roi_inventory"])
            except RoiInventoryError as exc:
                raise BatchArtifactError("parent source ROI table reference is invalid") from exc
        sources.append(RoiParentSource(**row))
    ids = tuple(item.eligible_parent_id for item in sources)
    if not sources or len(set(ids)) != len(ids):
        raise BatchArtifactError("parent source selection is empty or duplicated")
    return tuple(sources)


@dataclass(frozen=True, slots=True)
class SecondaryRoiSources:
    predecessor_result_ref_sha256: str
    parent_sources: tuple[RoiParentSource, ...]


def needed_secondary_structure_ids(
    configured_structures: Sequence[SecondaryStructureSpec], structure_id: str,
) -> tuple[str, ...]:
    """Select the immediate mutation predecessor and named earlier parent stages."""
    configured = tuple(configured_structures)
    ids = tuple(item.structure_id for item in configured)
    if len(set(ids)) != len(ids) or structure_id not in ids:
        raise BatchArtifactError("secondary stage is absent or duplicated in configuration")
    index = ids.index(structure_id)
    earlier = ids[:index]
    parents = set(configured[index].eligible_parent_ids)
    selected = set(earlier) & parents
    if earlier:
        selected.add(earlier[-1])
    return tuple(item for item in earlier if item in selected)


def project_secondary_roi_sources(
    *, configured_structure: SecondaryStructureSpec,
    primary: ValidatedPrimaryClassifierInferenceResultArtifactRefProjection | None,
    prior_secondaries: Sequence[ValidatedSecondaryClassifierInferenceResultArtifactRefProjection],
    configured_structures: Sequence[SecondaryStructureSpec],
    accepted_domain_ref: BatchArtifactRef,
    primary_skip_ref: BatchArtifactRef | None = None,
    pipeline_run_id: str | None = None,
    target_project_identity_sha256: str | None = None,
) -> SecondaryRoiSources:
    """Join accepted references to exact selected parents and the physical predecessor.

    Parent object revisions are checked natively against the accepted tables.
    Reference annotation imports may legitimately change the saved project after
    its predecessor completed. The supplied prior set must be exactly the
    configured direct parent stages plus that predecessor, preventing arbitrary
    live objects from acquiring authority through matching metadata.
    """
    needed = needed_secondary_structure_ids(configured_structures, configured_structure.structure_id)
    configured_by_id = {item.structure_id: item for item in configured_structures}
    if configured_by_id.get(configured_structure.structure_id) != configured_structure:
        raise BatchArtifactError("secondary source projection selected a foreign configuration")
    previous = tuple(prior_secondaries)
    by_id = {item.structure_id: item for item in previous}
    if len(by_id) != len(previous) or set(by_id) != set(needed):
        raise BatchArtifactError("secondary input set differs from its exact predecessor and parent set")
    from .accepted_analysis_domain_records import validate_accepted_analysis_domain_artifact_ref
    domain = validate_accepted_analysis_domain_artifact_ref(accepted_domain_ref)
    if len(domain.domain_authorities) != 1:
        raise BatchArtifactError("Secondary inference requires one accepted analysis region")
    active = domain.domain_authorities[0]
    if primary is None:
        from .batch_training.planning import validate_primary_skip_artifact_ref
        route_ref = validate_primary_skip_artifact_ref(primary_skip_ref,
            domain_bindings=accepted_domain_ref.scope.analysis_domain_bindings)
        if not pipeline_run_id or not target_project_identity_sha256:
            raise BatchArtifactError("Secondary inference lacks its current run and project identity")
        primary_status = "active"
    else:
        if primary_skip_ref is not None:
            raise BatchArtifactError("Secondary inference cannot both use and skip primary classification")
        route_ref = primary.artifact_ref
        pipeline_run_id = primary.pipeline_run_id if pipeline_run_id is None else pipeline_run_id
        target_project_identity_sha256 = (primary.target_project_identity_sha256
            if target_project_identity_sha256 is None else target_project_identity_sha256)
        primary_status = primary.domain_status
    if primary is not None and accepted_domain_ref.scope != primary.artifact_ref.scope:
        raise BatchArtifactError("secondary source domain differs from the accepted primary")
    for item in previous:
        if (
            item.artifact_ref.scope != accepted_domain_ref.scope
            or item.artifact_ref.configuration_bindings != accepted_domain_ref.configuration_bindings
            or item.canonical_image_id != domain.canonical_image_id
            or item.canonical_image_sha256 != domain.canonical_image_sha256
            or item.tissue_generation_id != domain.tissue_generation_id
            or item.section_id != active.section_id
            or item.analysis_domain_path_object_id != active.source_path_object_id
            or item.pipeline_run_id != pipeline_run_id
            or item.target_project_identity_sha256 != target_project_identity_sha256
            or item.primary_inference_result_ref_sha256 != (None if primary is None else route_ref.artifact_ref_sha256)
            or (primary is None and route_ref.artifact_ref_sha256 not in item.artifact_ref.owning_identity["prerequisite_artifact_ref_sha256s"])
            or item.primary_domain_status != primary_status
            or item.eligible_parent_ids != configured_by_id[item.structure_id].eligible_parent_ids
        ):
            raise BatchArtifactError("secondary parent source has foreign accepted lineage")
    ids = tuple(item.structure_id for item in configured_structures)
    index = ids.index(configured_structure.structure_id)
    predecessor_ref = (accepted_domain_ref if primary is None else primary.artifact_ref) if index == 0 else by_id[ids[index - 1]].artifact_ref
    primary_classes = set() if primary is None else set(primary.primary_count_decision.outputs[0].class_ids)
    sources = []
    for parent_id in configured_structure.eligible_parent_ids:
        if parent_id == "AnalysisDomain":
            source = RoiParentSource(parent_id, "analysis_domain", accepted_domain_ref.artifact_ref_sha256,
                                     None, None, active.source_path_object_id)
        else:
            if parent_id in primary_classes:
                owner, role = primary, "primary"
            elif parent_id in by_id:
                owner, role = by_id[parent_id], "secondary"
            else:
                raise BatchArtifactError("secondary parent is not authenticated by accepted sources")
            source = RoiParentSource(parent_id, role, owner.artifact_ref.artifact_ref_sha256,
                                     owner.artifact_ref.producer_attempt_id, owner.roi_inventory, None)
        sources.append(source)
    return SecondaryRoiSources(predecessor_ref.artifact_ref_sha256, tuple(sources))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise BatchArtifactError("classifier inference evidence has a non-text key")
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise BatchArtifactError("classifier inference evidence is not passive JSON")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
        raise BatchArtifactError(
            "classifier inference evidence is not finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    result = _text(value, field)
    if _SHA256.fullmatch(result) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return result


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return value


def _closed(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} is not closed")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return tuple(value)


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPrimaryClassifierInferenceResultArtifactRefProjection:
    artifact_ref: BatchArtifactRef
    canonical_image_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    section_id: str
    analysis_domain_bindings: tuple[object, ...]
    analysis_domain_path_object_id: str
    classifier_record_set_ref_sha256: str
    classifier_record_set_sha256: str
    pipeline_run_id: str
    selected_output_id: str
    selected_classifier_record_sha256: str
    classifier_id: str
    classifier_version: str
    application_request_sha256: str
    target_project_identity_sha256: str
    deployment_binding_set_sha256: str
    target_evidence_set_sha256: str
    native_classifier_sha256: str
    native_classifier_byte_count: int
    pixel_classifier_definition_sha256: str
    roi_inventory: RoiInventoryRef
    primary_count_decision: PrimaryCountDecision
    domain_status: str
    primary_count_review_action: str | None
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("primary classifier inference projection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSecondaryClassifierInferenceResultArtifactRefProjection:
    artifact_ref: BatchArtifactRef
    canonical_image_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    section_id: str
    analysis_domain_bindings: tuple[object, ...]
    analysis_domain_path_object_id: str
    classifier_record_set_ref_sha256: str
    classifier_record_set_sha256: str
    pipeline_run_id: str
    selected_output_id: str
    selected_classifier_record_sha256: str
    classifier_id: str
    classifier_version: str
    application_request_sha256: str
    target_project_identity_sha256: str
    deployment_binding_set_sha256: str
    target_evidence_set_sha256: str
    native_classifier_sha256: str
    native_classifier_byte_count: int
    pixel_classifier_definition_sha256: str
    structure_id: str
    eligible_parent_ids: tuple[str, ...]
    parent_sources: tuple[RoiParentSource, ...]
    predecessor_result_ref_sha256: str
    primary_domain_status: str
    roi_inventory: RoiInventoryRef
    primary_inference_result_ref_sha256: str | None
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("secondary classifier inference projection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPrimaryClassifierInferenceResultProjection(
    ValidatedPrimaryClassifierInferenceResultArtifactRefProjection
):
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSecondaryClassifierInferenceResultProjection(
    ValidatedSecondaryClassifierInferenceResultArtifactRefProjection
):
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str


_CLASSIFIER_ROI_LIVE_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidatedClassifierRoiLiveAuthority:
    """Read-only C2 ROI authority reauthenticated for one selected domain."""

    primary: ValidatedPrimaryClassifierInferenceResultProjection | None
    classification_authority: BatchCommittedArtifactAuthority
    secondaries: tuple[ValidatedSecondaryClassifierInferenceResultProjection, ...]
    analysis_domain_binding: AnalysisDomainBinding
    pipeline_run_id: str
    primary_roi_inventory: RoiInventoryRef | None
    secondary_roi_inventories: tuple[tuple[str, RoiInventoryRef], ...]
    domain_status: str
    producer_output_dirs: Mapping[str, Path]
    _issuer: object

    @property
    def projections(self):
        return (() if self.primary is None else (self.primary,)) + self.secondaries

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedClassifierRoiLiveAuthority is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedClassifierRoiLiveAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedClassifierRoiLiveAuthority is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("ValidatedClassifierRoiLiveAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ValidatedClassifierRoiLiveAuthority is noncopyable")


def _projection(cls: type[object], **values: object):
    result = object.__new__(cls)
    for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def _kind_values(kind: Literal["primary", "secondary"]):
    if kind == "primary":
        return (
            "primary_classifier_inference",
            "primary_classifier",
            "primary_inference_result",
            "primary_compartment",
            QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value,
            primary_classifier_inference_result_identity_contract(),
        )
    return (
        "secondary_classifier_inference",
        "secondary_classifier",
        "secondary_inference_result",
        "secondary_structure",
        QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value,
        secondary_classifier_inference_result_identity_contract(),
    )


def _validated_sources(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    kind: Literal["primary", "secondary"],
):
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError(
            "classifier inference composition requires issuer-owned execution evidence"
        )
    try:
        validation_inputs = evidence.reopen_validation_inputs()
    except ProjectStageRecordError as exc:
        raise BatchArtifactError(
            "classifier inference evidence cannot reopen validation inputs"
        ) from exc
    node_kind, wire_stage, _artifact_kind, _classifier_kind, asset, _contract = _kind_values(kind)
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or context.authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
        or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
        or context.node_id != authorized.node_id
        or context.adapter_spec.node_kind != node_kind
        or authorized.wire_stage_id != wire_stage
        or authorized.terminal_stage_id != node_kind
        or authorized.request_codec_id != asset
        or authorized.request_codec_version != ("2" if kind == "primary" else "1")
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.attempt_authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or observation.saved_state_before_sha256 != authorized.saved_state_before_sha256
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
    ):
        raise BatchArtifactError("classifier inference evidence sources disagree")
    _sha256(evidence.validated_exchange_sha256, "validated_exchange_sha256")
    return context, authorized, observation, validation_inputs


def reconstruct_classifier_inference_invocation(
    request: Mapping[str, object],
    resolved_config: Mapping[str, object],
    *,
    project_entry_id: str,
) -> CurrentImageScientificInvocation:
    try:
        config = AnalysisConfig.from_mapping(_plain(resolved_config))
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("resolved classifier configuration is invalid") from exc
    revision = request["analysis_domain_revision_evidence"]
    if not isinstance(revision, Mapping):
        raise BatchArtifactError("classifier request domain revision is invalid")
    action = request["action"]
    action_context = _plain(request["action_context"])
    if (action is not None and type(action) is not str) or not isinstance(action_context, Mapping):
        raise BatchArtifactError("classifier request interaction fields are invalid")
    primary_setup = None
    if request["stage"] == "primary_classifier":
        primary_setup = _validated_primary_request_setup(request, config)
    state = {
        "preflight": {
            "canonical_image_id": request["canonical_image_id"],
            "canonical_image_sha256": "0" * 64,
        },
        "tissue_acceptance": {
            "generation_id": request["generation_id"],
            "sections": [
                {
                    "section_id": request["section_id"],
                    "roi_id": "authenticated-in-owner",
                    "path_object_id": "authenticated-in-owner",
                }
            ],
        },
        "analysis_domain_selection": {
            "selection_status": "accepted",
            "selected_annotations_locked": True,
            "domain_set_sha256": request["domain_set_sha256"],
            "analysis_domain_manifests": [{"revisions": [dict(revision)]}],
        },
    }
    state["classifier_review_status"] = request["classifier_review_status"]
    if primary_setup is not None:
        state["primary_setup"] = primary_setup
    if request.get("secondary_structure_settings") is not None:
        state["secondary_structure_settings"] = _plain(request["secondary_structure_settings"])
    return CurrentImageScientificInvocation(
        stage=str(request["stage"]),
        config=config,
        run_id=str(request["run_id"]),
        project_entry_id=project_entry_id,
        scientific_state=state,
        section_id=str(request["section_id"]),
        analysis_domain_id=str(request["analysis_domain_id"]),
        analysis_domain_revision=int(request["analysis_domain_revision"]),
        analysis_domain_revision_sha256=str(request["analysis_domain_revision_sha256"]),
        analysis_domain_path_object_id=str(request["analysis_domain_path_object_id"]),
        structure_id=(
            str(request["structure_id"]) if request["structure_id"] is not None else None
        ),
        eligible_parent_ids=tuple(
            str(item) for item in _sequence(request["eligible_parent_ids"], "eligible_parent_ids")
        ),
        action=action,
        action_context=action_context,
    )


def _validated_primary_request_setup(
    request: Mapping[str, object],
    config: AnalysisConfig,
) -> dict[str, object]:
    """Validate the selected pair and its accepted settings source."""

    try:
        selected = PrimaryClassifierSettings.from_mapping(request["primary_settings"])
    except (KeyError, ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("primary request settings are invalid") from exc
    pair = selected.to_mapping()
    if _plain(request["primary_settings"]) != pair:
        raise BatchArtifactError("primary request settings are not canonical")
    if _sha256(request.get("primary_settings_sha256"), "primary_settings_sha256") != _digest(pair):
        raise BatchArtifactError("primary request settings digest differs")
    source_ref = _sha256(
        request.get("primary_settings_source_ref_sha256"),
        "primary_settings_source_ref_sha256",
    )
    if (config.primary_classes is None) != (config.primary_classifier is None):
        raise BatchArtifactError("original primary settings pair is incomplete")
    if config.primary_classes is not None:
        original = PrimaryClassifierSettings(config.primary_classes, config.primary_classifier)
        try:
            require_primary_settings_compatible(original, selected)
        except ROIAnalysisError as exc:
            raise BatchArtifactError("selected primary settings differ from admission") from exc
    application = request.get("classifier_application")
    if (
        not isinstance(application, Mapping)
        or application.get("classifier_id") != selected.primary_classifier.classifier_id
        or _plain(request.get("primary_classifier_output_classes"))
        != list(selected.primary_classifier.output_classes)
    ):
        raise BatchArtifactError("selected primary classifier or outputs differ")
    return {"settings": pair, "settings_source_ref_sha256": source_ref}


@dataclass(frozen=True, slots=True, init=False)
class ValidatedClassifierOutput:
    """Operation-scoped native evidence with one authenticated table projection.

    This value is issued only after wire, process, domain and table checks.
    Artifact construction consumes these facts and a disposition over the exact
    count decision; it neither reopens the table nor reparses finalized JSON.
    """

    evidence: ProjectStageTargetExecutionEvidence
    invocation: CurrentImageScientificInvocation
    request: Mapping[str, object]
    roi_inventory: RoiInventoryRef
    primary_count_decision: PrimaryCountDecision | None
    identity: Mapping[str, object]
    artifact_fields: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError("validated classifier output is owner-issued")


def validate_classifier_inference_output(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    kind: Literal["primary", "secondary"],
) -> ValidatedClassifierOutput | BatchNodeExecutionResult:
    """Authenticate the native output and scan its one table once for this operation."""
    context, authorized, observation, validation_inputs = _validated_sources(evidence, kind=kind)
    node_kind, wire_stage, artifact_kind, classifier_kind, _asset, contract = _kind_values(kind)
    result = evidence.wire_stage_result
    if result.status is not StageStatus.COMPLETED:
        if result.stage != wire_stage or result.slide_id != authorized.slide_id:
            raise BatchArtifactError("non-completed classifier result has foreign scope")
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    request_fields = set(_COMMON_REQUEST_FIELDS)
    if kind == "primary":
        request_fields.update(
            {
                "primary_classifier_output_classes",
                "primary_settings",
                "primary_settings_sha256",
                "primary_settings_source_ref_sha256",
            }
        )
    else:
        request_fields.update(
            {
                "parent_sources",
                "predecessor_result_ref_sha256",
                "primary_domain_status",
            }
        )
        if validation_inputs.resolved_config["secondary_structures"] is None:
            request_fields.add("secondary_structure_settings")
    request = _closed(
        validation_inputs.scientific_request_body,
        frozenset(request_fields),
        "classifier scientific request",
    )
    application = _closed(
        request["classifier_application"],
        _APPLICATION_FIELDS,
        "classifier application",
    )
    inputs_by_kind: dict[str, list[BatchArtifactRef]] = {}
    for item in context.inputs:
        inputs_by_kind.setdefault(item.artifact_kind, []).append(item)
    expected_kinds = {
        "analysis_frame",
        "project_catalogue_entry",
        "accepted_analysis_domain",
        "classifier_record_set",
    }
    if kind == "secondary":
        routes = {"primary_inference_result", "training_plan_set"} & inputs_by_kind.keys()
        if len(routes) != 1:
            raise BatchArtifactError("secondary inference requires one accepted primary classification route")
        expected_kinds.update(routes)
    if kind == "secondary" and "secondary_inference_result" in inputs_by_kind:
        expected_kinds.add("secondary_inference_result")
    if set(inputs_by_kind) != expected_kinds or any(
        len(items) != 1 for key, items in inputs_by_kind.items() if key != "secondary_inference_result"
    ):
        raise BatchArtifactError("classifier inference input set is not exact")
    frame_ref = inputs_by_kind["analysis_frame"][0]
    domain_ref = inputs_by_kind["accepted_analysis_domain"][0]
    record_set_ref = inputs_by_kind["classifier_record_set"][0]
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("classifier analysis frame input is invalid") from exc
    bindings = tuple(authorized.analysis_domain_bindings)
    if (
        len(bindings) != 1
        or tuple(domain_ref.scope.analysis_domain_bindings) != bindings
        or frame_ref.scope.sample_id != authorized.sample_id
        or frame_ref.scope.slide_id != authorized.slide_id
        or request["sample_id"] != authorized.sample_id
        or request["slide_id"] != authorized.slide_id
        or request["canonical_image_id"] != frame.spatial_frame.canonical_image_id
        or request["run_id"] != authorized.pipeline_run_id
        or request["analysis_domain_id"] != bindings[0].analysis_domain_id
        or request["analysis_domain_revision"] != bindings[0].analysis_domain_revision
        or request["analysis_domain_revision_sha256"] != bindings[0].analysis_domain_revision_sha256
        or request["domain_set_sha256"] != bindings[0].domain_set_sha256
        or _sequence(request["generation_ids"], "generation_ids") != (request["generation_id"],)
        or application["classifier_kind"] != classifier_kind
        or application["classifier_record_set_ref_sha256"] != record_set_ref.artifact_ref_sha256
        or application["classifier_record_set_sha256"]
        != record_set_ref.owning_identity.get("classifier_record_set_sha256")
        or authorized.prerequisite_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    ):
        raise BatchArtifactError("classifier request or source authority differs")
    expected_configuration = (
        ArtifactConfigurationBinding(
            authorized.sample_id, validation_inputs.resolved_config_sha256
        ),
    )
    _validate_classifier_inference_input_configuration_bindings(
        {
            artifact_kind: tuple(binding for item in items for binding in item.configuration_bindings)
            for artifact_kind, items in inputs_by_kind.items()
        },
        expected_configuration[0],
    )

    output = result.outputs
    if not isinstance(output, Mapping):
        raise BatchArtifactError("completed classifier result has no object output")
    invocation = reconstruct_classifier_inference_invocation(
        request,
        validation_inputs.resolved_config,
        project_entry_id=authorized.project_entry_id,
    )
    try:
        validate_classifier_output_scope(_plain(output), invocation)
    except ROIAnalysisError as exc:
        raise BatchArtifactError("classifier biological output scope is invalid") from exc

    echo = {
        "classifier_record_set_ref_sha256": application["classifier_record_set_ref_sha256"],
        "classifier_record_set_sha256": application["classifier_record_set_sha256"],
        "selected_output_id": application["output_id"],
        "selected_classifier_record_sha256": application["classifier_record_sha256"],
        "classifier_id": application["classifier_id"],
        "classifier_version": application["classifier_version"],
        "classifier_kind": application["classifier_kind"],
        "application_request_sha256": application["application_request_sha256"],
        "target_project_identity_sha256": application["target_project_identity_sha256"],
        "deployment_binding_set_sha256": application["deployment_binding_set_sha256"],
        "target_evidence_set_sha256": application["target_evidence_set_sha256"],
        "project_relative_path": application["project_relative_path"],
        "native_classifier_sha256": application["native_classifier_sha256"],
        "native_classifier_byte_count": application["native_classifier_byte_count"],
        "pixel_classifier_definition_sha256": application["pixel_classifier_definition_sha256"],
        "classifier_definition": application["pixel_classifier_definition"],
    }
    if any(_plain(output.get(name)) != _plain(value) for name, value in echo.items()):
        raise BatchArtifactError("native classifier output echo differs")

    manifest = _closed(
        evidence.generation_manifest,
        _MANIFEST_FIELDS,
        "classifier generation manifest",
    )
    if (
        result.stage != wire_stage
        or result.slide_id != authorized.slide_id
        or manifest["schema_version"] != _MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != wire_stage
        or manifest["slide_id"] != authorized.slide_id
        or _sequence(manifest["generation_ids"], "manifest generation_ids")
        != (result.generation_id,)
        or _plain(manifest["object_outputs"]) != _plain(output)
        or observation.saved_state_after_sha256 is None
        or observation.saved_state_disposition
        not in {"authenticated_unchanged", "authenticated_changed"}
        or (observation.saved_state_before_sha256 == observation.saved_state_after_sha256)
        != (observation.saved_state_disposition == "authenticated_unchanged")
    ):
        raise BatchArtifactError("classifier result, manifest, or saved-state transition differs")

    try:
        roi_inventory = parse_roi_inventory_ref(output.get("roi_inventory"))
        allowed_classes = (
            _primary_settings_for_invocation(invocation)[0].primary_classifier.output_classes
            if kind == "primary" else (str(request["structure_id"]),)
        )
        with open_roi_inventory(
            roi_inventory, admitted_root=Path(validation_inputs.operational_output_dir),
            expected_member=roi_inventory_member(authorized.attempt_id),
            allowed_class_ids=allowed_classes,
        ) as table:
            counts_by_class = table.class_counts()
    except (RoiInventoryError, OSError) as exc:
        raise BatchArtifactError("classifier ROI table differs from its admitted output") from exc
    primary_decision = (
        _primary_count_decision(invocation, roi_inventory, counts_by_class)
        if kind == "primary" else None
    )
    identity: dict[str, object] = {
        "canonical_image_id": request["canonical_image_id"],
        "canonical_image_sha256": frame.spatial_frame.canonical_image_sha256,
        "tissue_generation_id": request["generation_id"],
        "section_id": request["section_id"],
        "analysis_domain_path_object_id": request["analysis_domain_path_object_id"],
        "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
        "classifier_record_set_ref_sha256": application["classifier_record_set_ref_sha256"],
        "classifier_record_set_sha256": application["classifier_record_set_sha256"],
        "selected_output_id": application["output_id"],
        "selected_classifier_record_sha256": application["classifier_record_sha256"],
        "classifier_id": application["classifier_id"],
        "classifier_version": application["classifier_version"],
        "classifier_kind": classifier_kind,
        "application_request_sha256": application["application_request_sha256"],
        "target_project_identity_sha256": application["target_project_identity_sha256"],
        "deployment_binding_set_sha256": application["deployment_binding_set_sha256"],
        "target_evidence_set_sha256": application["target_evidence_set_sha256"],
        "native_classifier_sha256": application["native_classifier_sha256"],
        "native_classifier_byte_count": application["native_classifier_byte_count"],
        "pixel_classifier_definition_sha256": application["pixel_classifier_definition_sha256"],
        "generation_ids": [result.generation_id],
        "roi_inventory": roi_inventory.to_mapping(),
        "object_outputs_sha256": _digest(output),
        "node_id": authorized.node_id,
        "attempt_authorization_sha256": authorized.attempt_authorization_sha256,
        "pipeline_run_id": authorized.pipeline_run_id,
        "input_artifact_set_sha256": authorized.input_artifact_set_sha256,
        "prerequisite_artifact_ref_sha256s": list(authorized.prerequisite_artifact_ref_sha256s),
        "target_request_sha256": authorized.target_request_sha256,
        "authorized_entry_sha256": authorized.authorized_entry_sha256,
        "generation_manifest_payload_sha256": (observation.generation_manifest_payload_sha256),
        "generation_manifest_byte_count": observation.generation_manifest_byte_count,
        "result_payload_sha256": observation.result_payload_sha256,
        "result_byte_count": observation.result_byte_count,
        "process_evidence_sha256": observation.process_evidence_sha256,
        "project_snapshot_sha256": observation.project_snapshot_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "saved_state_before_sha256": observation.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "scientific_request_body_sha256": (validation_inputs.scientific_request_body_sha256),
        "scientific_request_body_byte_count": (
            validation_inputs.scientific_request_body_byte_count
        ),
        "request_capability_sha256": validation_inputs.request_capability_sha256,
        "validation_input_authority_sha256": validation_inputs.authority_sha256,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "projection_target_sha256": context.projection_target_sha256,
        "current_image_identity_sha256": context.current_image_identity_sha256,
        "script_sha256": context.script_sha256,
        "project_identity_sha256": context.project_identity_sha256,
        "project_authority_head_sha256": context.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    if kind == "secondary":
        primary_ref = inputs_by_kind[next(iter(routes))][0]
        primary = (validate_primary_classifier_inference_result_artifact_ref(primary_ref)
                   if primary_ref.artifact_kind == "primary_inference_result" else None)
        domain_status = "active" if primary is None else primary.domain_status
        secondary_structures = secondary_structures_for_invocation(invocation)
        structure = next(item for item in secondary_structures if item.structure_id == invocation.structure_id)
        expected_sources = project_secondary_roi_sources(
            configured_structure=structure, primary=primary,
            prior_secondaries=tuple(validate_secondary_classifier_inference_result_artifact_ref(item)
                                    for item in inputs_by_kind.get("secondary_inference_result", ())),
            configured_structures=secondary_structures,
            accepted_domain_ref=domain_ref,
            primary_skip_ref=primary_ref if primary is None else None,
            pipeline_run_id=authorized.pipeline_run_id,
            target_project_identity_sha256=context.project_identity_sha256,
        )
        sources = parse_roi_parent_sources(request["parent_sources"])
        parents = tuple(request["eligible_parent_ids"])
        if (
            (primary is not None and (primary.pipeline_run_id != authorized.pipeline_run_id
            or primary.canonical_image_id != request["canonical_image_id"]
            or primary.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
            or primary.tissue_generation_id != request["generation_id"]
            or primary.section_id != request["section_id"]
            or primary.analysis_domain_path_object_id != request["analysis_domain_path_object_id"]))
            or sources != expected_sources.parent_sources
            or parse_roi_parent_sources(output.get("parent_sources")) != sources
            or request["predecessor_result_ref_sha256"] != expected_sources.predecessor_result_ref_sha256
            or output.get("predecessor_result_ref_sha256") != expected_sources.predecessor_result_ref_sha256
            or request["primary_domain_status"] != domain_status
            or output.get("primary_domain_status") != domain_status
            or output.get("eligible_parent_ids_sha256") != _digest(list(parents))
            or (domain_status != "active" and roi_inventory.row_count != 0)
        ):
            raise BatchArtifactError("secondary output parent commitments or disposition differ")
        identity.update(
            structure_id=request["structure_id"], eligible_parent_ids=list(parents),
            eligible_parent_ids_sha256=_digest(list(parents)),
            parent_sources=[item.to_mapping() for item in sources],
            predecessor_result_ref_sha256=expected_sources.predecessor_result_ref_sha256,
            primary_domain_status=domain_status,
            primary_inference_result_ref_sha256=None if primary is None else primary_ref.artifact_ref_sha256,
        )

    artifact_fields = dict(
        artifact_id=(
            ("primary-inference-" if kind == "primary" else "secondary-inference-")
            + str(identity["object_outputs_sha256"])[:48]
        ),
        artifact_kind=artifact_kind,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authorized.node_id,
        producer_attempt_id=authorized.attempt_id,
        producer_attempt_authorization_sha256=authorized.attempt_authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=(
                bindings[0].analysis_domain_id
                if domain_ref.scope.validation_domain_id is None
                else domain_ref.scope.validation_domain_id
            ),
            sample_id=authorized.sample_id,
            slide_id=authorized.slide_id,
            project_entry_id=authorized.project_entry_id,
            analysis_domain_bindings=bindings,
        ),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": _text(
                frame_ref.runtime_versions.get("qupath"),
                "analysis frame QuPath runtime",
            ),
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=expected_configuration,
    )
    return _projection(
        ValidatedClassifierOutput, evidence=evidence, invocation=invocation, request=request,
        roi_inventory=roi_inventory, primary_count_decision=primary_decision,
        identity=MappingProxyType(identity), artifact_fields=MappingProxyType(artifact_fields),
    )


def build_classifier_inference_result_execution_result(
    validated: ValidatedClassifierOutput,
    *, primary_disposition: PrimaryClassifierDisposition | None = None,
) -> BatchNodeExecutionResult:
    """Publish bounded ownership after resolving the exact table-bound decision."""
    if type(validated) is not ValidatedClassifierOutput:
        raise BatchArtifactError("classifier publication requires validated output authority")
    identity = dict(validated.identity)
    if validated.primary_count_decision is not None:
        if not isinstance(primary_disposition, PrimaryClassifierDisposition) or primary_disposition.decision is not validated.primary_count_decision:
            raise BatchArtifactError("primary disposition is not bound to this validated output")
        identity.update(
            primary_count_decision=primary_disposition.decision.to_mapping(),
            domain_status=primary_disposition.domain_status,
            primary_count_review_action=primary_disposition.review_action,
        )
    elif primary_disposition is not None:
        raise BatchArtifactError("secondary classifier cannot carry a primary decision")
    artifact = BatchArtifactRef(**validated.artifact_fields, owning_identity=identity)
    return BatchNodeExecutionResult(validated.evidence.authorized.attempt_authorization_sha256, (artifact,))


def _validate_ref(
    artifact_ref: BatchArtifactRef,
    *,
    kind: Literal["primary", "secondary"],
):
    """Validate ref for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    ref = validate_artifact_ref(artifact_ref)
    _node_kind, _wire, artifact_kind, classifier_kind, _asset, contract = _kind_values(kind)
    if (
        ref.artifact_kind != artifact_kind
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or set(ref.runtime_versions) != {"mif_pipeline", "qupath"}
        or ref.runtime_versions["mif_pipeline"] != MIF_PIPELINE_BATCH_RUNTIME_VERSION
        or ref.producer_attempt_id is None
        or ref.producer_attempt_authorization_sha256 is None
    ):
        raise BatchArtifactError("classifier inference ref lacks its locatorless owner contract")
    identity = ref.owning_identity
    bindings = ref.scope.analysis_domain_bindings
    if len(bindings) != 1:
        raise BatchArtifactError("classifier inference ref requires one domain binding")
    try:
        roi_inventory = parse_roi_inventory_ref(identity["roi_inventory"])
    except RoiInventoryError as exc:
        raise BatchArtifactError("classifier ROI table reference is invalid") from exc
    if roi_inventory.path != roi_inventory_member(ref.producer_attempt_id):
        raise BatchArtifactError("classifier table member differs from its accepted producer")
    prerequisite = tuple(
        _sha256(item, "prerequisite_artifact_ref_sha256s item")
        for item in _sequence(
            identity["prerequisite_artifact_ref_sha256s"],
            "prerequisite_artifact_ref_sha256s",
        )
    )
    for field in (
        "adapter_spec_sha256",
        "analysis_domain_binding_set_sha256",
        "application_request_sha256",
        "attempt_authorization_sha256",
        "authorized_entry_sha256",
        "canonical_image_sha256",
        "classifier_record_set_ref_sha256",
        "classifier_record_set_sha256",
        "current_image_identity_sha256",
        "deployment_binding_set_sha256",
        "execution_exchange_sha256",
        "generation_manifest_payload_sha256",
        "input_artifact_set_sha256",
        "native_classifier_sha256",
        "object_outputs_sha256",
        "process_evidence_sha256",
        "project_authority_head_sha256",
        "project_entry_projection_sha256",
        "project_identity_sha256",
        "project_qpproj_snapshot_sha256",
        "project_snapshot_sha256",
        "projection_target_sha256",
        "pixel_classifier_definition_sha256",
        "request_capability_sha256",
        "result_payload_sha256",
        "saved_state_after_sha256",
        "saved_state_before_sha256",
        "scientific_request_body_sha256",
        "script_sha256",
        "selected_classifier_record_sha256",
        "target_evidence_set_sha256",
        "target_project_identity_sha256",
        "target_request_sha256",
        "validated_exchange_sha256",
        "validation_input_authority_sha256",
    ):
        _sha256(identity[field], field)
    if (
        ref.scope.sample_id is None
        or ref.scope.slide_id is None
        or ref.scope.project_entry_id is None
        or ref.scope.validation_domain_id is None
        or identity["classifier_kind"] != classifier_kind
        or _digest([item.to_mapping() for item in bindings])
        != identity["analysis_domain_binding_set_sha256"]
        or identity["input_artifact_set_sha256"]
        != _digest({"artifact_ref_sha256s": list(prerequisite)})
        or ref.producer_node_id != identity["node_id"]
        or ref.producer_attempt_authorization_sha256 != identity["attempt_authorization_sha256"]
        or ref.compatibility_signature_sha256 is None
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != ref.scope.sample_id
        or ref.artifact_id
        != ("primary-inference-" if kind == "primary" else "secondary-inference-")
        + str(identity["object_outputs_sha256"])[:48]
    ):
        raise BatchArtifactError("classifier inference compact identity differs")
    common = {
        "artifact_ref": ref,
        "canonical_image_id": _text(identity["canonical_image_id"], "canonical_image_id"),
        "canonical_image_sha256": identity["canonical_image_sha256"],
        "tissue_generation_id": _text(identity["tissue_generation_id"], "tissue_generation_id"),
        "section_id": _text(identity["section_id"], "section_id"),
        "analysis_domain_bindings": bindings,
        "analysis_domain_path_object_id": _text(
            identity["analysis_domain_path_object_id"], "analysis_domain_path_object_id"
        ),
        "classifier_record_set_ref_sha256": identity["classifier_record_set_ref_sha256"],
        "classifier_record_set_sha256": identity["classifier_record_set_sha256"],
        "pipeline_run_id": _text(identity["pipeline_run_id"], "pipeline_run_id"),
        "selected_output_id": _text(identity["selected_output_id"], "selected_output_id"),
        "selected_classifier_record_sha256": identity["selected_classifier_record_sha256"],
        "classifier_id": _text(identity["classifier_id"], "classifier_id"),
        "classifier_version": _text(identity["classifier_version"], "classifier_version"),
        "application_request_sha256": identity["application_request_sha256"],
        "target_project_identity_sha256": identity["target_project_identity_sha256"],
        "deployment_binding_set_sha256": identity["deployment_binding_set_sha256"],
        "target_evidence_set_sha256": identity["target_evidence_set_sha256"],
        "native_classifier_sha256": identity["native_classifier_sha256"],
        "native_classifier_byte_count": _positive(
            identity["native_classifier_byte_count"], "native_classifier_byte_count"
        ),
        "pixel_classifier_definition_sha256": identity["pixel_classifier_definition_sha256"],
        "roi_inventory": roi_inventory,
        "saved_state_before_sha256": identity["saved_state_before_sha256"],
        "saved_state_after_sha256": identity["saved_state_after_sha256"],
        "execution_exchange_sha256": identity["execution_exchange_sha256"],
        "validated_exchange_sha256": identity["validated_exchange_sha256"],
    }
    if kind == "primary":
        try:
            decision = PrimaryCountDecision.from_mapping(_plain(identity["primary_count_decision"]))
            disposition = PrimaryClassifierDisposition(decision, identity["domain_status"], identity["primary_count_review_action"])
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchArtifactError("primary count decision or disposition is invalid") from exc
        if (
            decision.roi_inventory_sha256 != roi_inventory.sha256
            or decision.domain.analysis_domain_id != bindings[0].analysis_domain_id
            or decision.domain.slide_id != ref.scope.slide_id
            or decision.domain.section_id != common["section_id"]
            or decision.domain.tissue_generation_id != common["tissue_generation_id"]
            or decision.outputs[0].observed_count > roi_inventory.row_count
        ):
            raise BatchArtifactError("primary decision differs from its table or domain")
        return _projection(
            ValidatedPrimaryClassifierInferenceResultArtifactRefProjection, **common,
            primary_count_decision=decision, domain_status=disposition.domain_status,
            primary_count_review_action=disposition.review_action,
        )
    parents = tuple(_text(item, "eligible_parent_ids item") for item in _sequence(identity["eligible_parent_ids"], "eligible_parent_ids"))
    sources = parse_roi_parent_sources(identity["parent_sources"])
    if (
        tuple(item.eligible_parent_id for item in sources) != parents
        or identity["eligible_parent_ids_sha256"] != _digest(list(parents))
        or (identity["primary_inference_result_ref_sha256"] is not None
            and identity["primary_inference_result_ref_sha256"] not in prerequisite)
        or identity["predecessor_result_ref_sha256"] not in prerequisite
        or any(item.source_result_ref_sha256 not in prerequisite for item in sources)
        or any(item.source_kind == "analysis_domain" and item.analysis_domain_path_object_id != common["analysis_domain_path_object_id"] for item in sources)
        or identity["primary_domain_status"] not in {"active", "excluded_below_minimum", "excluded_by_primary_count_review"}
        or (identity["primary_domain_status"] != "active" and roi_inventory.row_count != 0)
    ):
        raise BatchArtifactError("secondary inference parent selection or disposition differs")
    return _projection(
        ValidatedSecondaryClassifierInferenceResultArtifactRefProjection, **common,
        structure_id=_text(identity["structure_id"], "structure_id"), eligible_parent_ids=parents,
        parent_sources=sources,
        predecessor_result_ref_sha256=_sha256(identity["predecessor_result_ref_sha256"], "predecessor_result_ref_sha256"),
        primary_domain_status=identity["primary_domain_status"],
        primary_inference_result_ref_sha256=(None if identity["primary_inference_result_ref_sha256"] is None else
            _sha256(identity["primary_inference_result_ref_sha256"], "primary_inference_result_ref_sha256")),
    )


def validate_primary_classifier_inference_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedPrimaryClassifierInferenceResultArtifactRefProjection:
    return _validate_ref(artifact_ref, kind="primary")


def validate_secondary_classifier_inference_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedSecondaryClassifierInferenceResultArtifactRefProjection:
    return _validate_ref(artifact_ref, kind="secondary")


def _decode(authority: BatchCommittedArtifactAuthority, *, kind: Literal["primary", "secondary"]):
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("classifier inference decoding requires committed state authority")
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    node_kind = _kind_values(kind)[0]
    if authority.producer_node_kind != node_kind:
        raise BatchArtifactError("classifier inference producer kind is invalid")
    compact = _validate_ref(authority.artifact_ref, kind=kind)
    if authority.execution_exchange_sha256 != compact.execution_exchange_sha256:
        raise BatchArtifactError("committed classifier inference exchange differs")
    values = {name: getattr(compact, name) for name in compact.__dataclass_fields__}
    values.update(
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )
    cls = (
        ValidatedPrimaryClassifierInferenceResultProjection
        if kind == "primary"
        else ValidatedSecondaryClassifierInferenceResultProjection
    )
    return _projection(cls, **values)


def decode_committed_primary_classifier_inference_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedPrimaryClassifierInferenceResultProjection:
    return _decode(authority, kind="primary")


def decode_committed_secondary_classifier_inference_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedSecondaryClassifierInferenceResultProjection:
    return _decode(authority, kind="secondary")


def _validate_live_roi_sample(
    resolved_sample: ResolvedBatchSample,
) -> ArtifactConfigurationBinding:
    if (
        type(resolved_sample) is not ResolvedBatchSample
        or type(resolved_sample.config) is not AnalysisConfig
        or not resolved_sample.enabled
        or resolved_sample.config.slide_id != resolved_sample.slide_id
        or _digest(resolved_sample.config.to_mapping()) != resolved_sample.resolved_config_sha256
    ):
        raise BatchArtifactError("classifier ROI authority sample is not resolved")
    return ArtifactConfigurationBinding(
        resolved_sample.sample_id, resolved_sample.resolved_config_sha256
    )


def _validate_live_roi_projection_scope(
    projection: (
        ValidatedPrimaryClassifierInferenceResultProjection
        | ValidatedSecondaryClassifierInferenceResultProjection
    ),
    *,
    binding: AnalysisDomainBinding,
    sample: ResolvedBatchSample,
    configuration_binding: ArtifactConfigurationBinding,
    primary: ValidatedPrimaryClassifierInferenceResultProjection,
) -> None:
    ref = projection.artifact_ref
    if (
        projection.analysis_domain_bindings != (binding,)
        or ref.scope.sample_id != sample.sample_id
        or ref.scope.slide_id != sample.slide_id
        or ref.scope.project_entry_id != binding.project_entry_id
        or ref.scope.validation_domain_id != sample.validation_domain_id
        or ref.configuration_bindings != (configuration_binding,)
        or projection.canonical_image_id != primary.canonical_image_id
        or projection.canonical_image_sha256 != primary.canonical_image_sha256
        or projection.tissue_generation_id != primary.tissue_generation_id
        or projection.section_id != primary.section_id
        or projection.analysis_domain_path_object_id != primary.analysis_domain_path_object_id
        or projection.pipeline_run_id != primary.pipeline_run_id
        or projection.state_head_sha256 != primary.state_head_sha256
        or projection.active_plan_sha256 != primary.active_plan_sha256
        or projection.target_project_identity_sha256 != primary.target_project_identity_sha256
    ):
        raise BatchArtifactError("classifier ROI authority lineage differs")


def _validate_skipped_live_roi_scope(
    projection, *, domain, binding, route_ref, state_head_sha256,
    active_plan_sha256, pipeline_run_id,
):
    active = next((item for item in domain.domain_authorities
                   if item.analysis_domain_id == binding.analysis_domain_id), None)
    if (active is None
            or projection.analysis_domain_bindings != (binding,)
            or projection.artifact_ref.configuration_bindings != domain.artifact_ref.configuration_bindings
            or projection.artifact_ref.scope.sample_id != domain.sample_id
            or projection.artifact_ref.scope.slide_id != domain.slide_id
            or projection.artifact_ref.scope.project_entry_id != domain.project_entry_id
            or projection.canonical_image_id != domain.canonical_image_id
            or projection.canonical_image_sha256 != domain.canonical_image_sha256
            or projection.tissue_generation_id != domain.tissue_generation_id
            or projection.section_id != active.section_id
            or projection.analysis_domain_path_object_id != active.source_path_object_id
            or projection.pipeline_run_id != pipeline_run_id
            or projection.state_head_sha256 != state_head_sha256
            or projection.active_plan_sha256 != active_plan_sha256
            or projection.primary_inference_result_ref_sha256 is not None
            or route_ref.artifact_ref_sha256 not in projection.artifact_ref.owning_identity["prerequisite_artifact_ref_sha256s"]
            or projection.primary_domain_status != "active"):
        raise BatchArtifactError("classifier ROI authority differs from the accepted skipped-primary region")


def bind_committed_classifier_roi_live_authority(
    primary_authority: BatchCommittedArtifactAuthority,
    secondary_authorities: Sequence[BatchCommittedArtifactAuthority],
    *,
    analysis_domain_binding: AnalysisDomainBinding,
    resolved_sample: ResolvedBatchSample,
    exchange_reopener: ProjectStageCommittedExchangeReopener | None = None,
    secondary_selection=None,
    accepted_domain_authority: BatchCommittedArtifactAuthority | None = None,
    pipeline_run_id: str | None = None,
    include_secondary: bool = True,
) -> ValidatedClassifierRoiLiveAuthority:
    """Bind accepted classifier tables to one current analysis region.

    Callers that read table bytes supply the committed exchange reopener, so
    the view retains each producer's recorded output directory. Segmentation
    requests need only the table references; native request issuance binds
    their directories separately without replaying these reads.
    """

    if type(primary_authority) is not BatchCommittedArtifactAuthority or not all(
        type(item) is BatchCommittedArtifactAuthority for item in tuple(secondary_authorities)
    ):
        raise BatchArtifactError("classifier ROI authority requires committed results")
    if type(analysis_domain_binding) is not AnalysisDomainBinding:
        raise BatchArtifactError("classifier ROI authority requires one domain binding")
    sample_binding = _validate_live_roi_sample(resolved_sample)
    route_ref = primary_authority.artifact_ref
    if route_ref.artifact_kind == "training_plan_set":
        from .batch_training.planning import validate_primary_skip_artifact_ref
        from .accepted_analysis_domain_records import decode_committed_accepted_analysis_domain
        validate_primary_skip_artifact_ref(route_ref, domain_bindings=(analysis_domain_binding,))
        if type(accepted_domain_authority) is not BatchCommittedArtifactAuthority or not pipeline_run_id:
            raise BatchArtifactError("Skipped primary classification requires its accepted analysis region and run")
        domain = decode_committed_accepted_analysis_domain(accepted_domain_authority)
        if (analysis_domain_binding not in domain.artifact_ref.scope.analysis_domain_bindings
                or domain.artifact_ref.configuration_bindings != (sample_binding,)
                or domain.sample_id != resolved_sample.sample_id
                or domain.slide_id != resolved_sample.slide_id
                or domain.project_entry_id != analysis_domain_binding.project_entry_id
                or domain.state_head_sha256 != primary_authority.state_head_sha256
                or domain.active_plan_sha256 != primary_authority.active_plan_sha256):
            raise BatchArtifactError("Skipped primary classification has foreign analysis-region authority")
        primary = None
        domain_status = "active"
        predecessor_ref = domain.artifact_ref
    else:
        primary = decode_committed_primary_classifier_inference_result(primary_authority)
        _validate_live_roi_projection_scope(
            primary, binding=analysis_domain_binding, sample=resolved_sample,
            configuration_binding=sample_binding, primary=primary,
        )
        pipeline_run_id = primary.pipeline_run_id
        domain_status = primary.domain_status
        predecessor_ref = primary.artifact_ref
    secondaries = tuple(
        decode_committed_secondary_classifier_inference_result(item)
        for item in secondary_authorities
    )
    from .batch_secondary_definitions import resolve_secondary_structures
    if not include_secondary and (secondary_authorities or secondary_selection is not None):
        raise BatchArtifactError("Primary-only ROI authority cannot include secondary results or definitions")
    structures = (resolve_secondary_structures(resolved_sample, secondary_selection)
                  if include_secondary else ())
    if secondary_selection is not None and (
            secondary_selection.state_head_sha256 != primary_authority.state_head_sha256
            or secondary_selection.active_plan_sha256 != primary_authority.active_plan_sha256):
        raise BatchArtifactError("Classifier ROI definitions belong to another execution revision")
    configured_structure_ids = tuple(item.structure_id for item in structures)
    by_structure = {item.structure_id: item for item in secondaries}
    if len(by_structure) != len(secondaries) or set(by_structure) != set(configured_structure_ids):
        raise BatchArtifactError("classifier ROI authority secondary structure scope differs")
    ordered_secondaries = tuple(by_structure[item] for item in configured_structure_ids)
    configured_by_structure = {
        item.structure_id: item for item in structures
    }
    for secondary in ordered_secondaries:
        if primary is None:
            _validate_skipped_live_roi_scope(
                secondary, domain=domain, binding=analysis_domain_binding, route_ref=route_ref,
                state_head_sha256=primary_authority.state_head_sha256,
                active_plan_sha256=primary_authority.active_plan_sha256,
                pipeline_run_id=pipeline_run_id,
            )
        else:
            _validate_live_roi_projection_scope(
                secondary, binding=analysis_domain_binding, sample=resolved_sample,
                configuration_binding=sample_binding, primary=primary,
            )
        if (
            secondary.primary_inference_result_ref_sha256
            != (None if primary is None else route_ref.artifact_ref_sha256)
            or secondary.eligible_parent_ids
            != configured_by_structure[secondary.structure_id].eligible_parent_ids
        ):
            raise BatchArtifactError("classifier ROI authority lineage differs")

    for secondary in ordered_secondaries:
        if (
            secondary.predecessor_result_ref_sha256 != predecessor_ref.artifact_ref_sha256
            or secondary.primary_domain_status != domain_status
        ):
            raise BatchArtifactError("classifier ROI authority predecessor or domain status differs")
        predecessor_ref = secondary.artifact_ref

    value = object.__new__(ValidatedClassifierRoiLiveAuthority)
    for name, item in {
        "primary": primary,
        "classification_authority": primary_authority,
        "secondaries": ordered_secondaries,
        "analysis_domain_binding": analysis_domain_binding,
        "pipeline_run_id": pipeline_run_id,
        "primary_roi_inventory": None if primary is None else primary.roi_inventory,
        "secondary_roi_inventories": tuple((item.structure_id, item.roi_inventory) for item in ordered_secondaries),
        "domain_status": domain_status,
        "producer_output_dirs": MappingProxyType({item.artifact_ref.producer_attempt_id: exchange_reopener.classifier_output_dir(item)
            for item in (() if exchange_reopener is None else
                         (() if primary is None else (primary_authority,)) + tuple(secondary_authorities))}),
        "_issuer": _CLASSIFIER_ROI_LIVE_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


__all__ = [
    "ValidatedClassifierRoiLiveAuthority",
    "ValidatedPrimaryClassifierInferenceResultArtifactRefProjection",
    "ValidatedPrimaryClassifierInferenceResultProjection",
    "ValidatedSecondaryClassifierInferenceResultArtifactRefProjection",
    "ValidatedSecondaryClassifierInferenceResultProjection",
    "RoiParentSource",
    "SecondaryRoiSources",
    "ValidatedClassifierOutput",
    "parse_roi_parent_sources",
    "project_secondary_roi_sources",
    "needed_secondary_structure_ids",
    "validate_classifier_inference_output",
    "build_classifier_inference_result_execution_result",
    "bind_committed_classifier_roi_live_authority",
    "decode_committed_primary_classifier_inference_result",
    "decode_committed_secondary_classifier_inference_result",
    "reconstruct_classifier_inference_invocation",
    "validate_primary_classifier_inference_result_artifact_ref",
    "validate_secondary_classifier_inference_result_artifact_ref",
]
