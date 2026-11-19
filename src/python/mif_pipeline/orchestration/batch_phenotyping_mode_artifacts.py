"""Authenticated phenotyping-mode selection artifacts for batch execution.

The `PhenotypingModeSelectionAuthority` record carries `selected_mode`, `source_kind`,
`project_entry_id`, `sample_id`, `slide_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import TYPE_CHECKING, Final
from types import MappingProxyType

from mif_pipeline.cell_analysis.phenotyping_population import (
    validate_population_settings, resolve_population_selection, slide_population_key,
)

from mif_pipeline.pipeline_core import (
    AnalysisConfig,
    InteractionAction,
    InteractionReason,
    PhenotypingMode,
    ROIAnalysisError,
    StageResult,
    validate_interaction_response,
)

from .batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from .batch_manifest import ResolvedBatchSample
from .batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)

if TYPE_CHECKING:
    from .batch_state import BatchStateStore


_CONTRACT_ID: Final[str] = "mif-batch-phenotyping-mode-selection-identity/v2"
_ARTIFACT_KIND: Final[str] = "phenotyping_mode"
_SCHEMA_NAME: Final[str] = "mif-batch-phenotyping-mode-selection"
_SCHEMA_VERSION: Final[int] = 2
_NODE_KIND: Final[str] = "phenotyping_mode_resolution"
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-phenotyping-mode/v2"
_INTERACTION_SCHEMA_VERSION: Final[str] = "phenotyping-interaction/v2"
_RESPONSE_VALIDATOR_ID: Final[str] = "roi-interaction-validator"
_RESPONSE_VALIDATOR_VERSION: Final[str] = "roi-interaction-actions/v2"
_SOURCE_CONFIG: Final[str] = "resolved_config"
_SOURCE_INTERACTION: Final[str] = "accepted_interaction"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_ISSUER = object()

_LINEAGE_FIELDS: Final[tuple[str, ...]] = (
    "interaction_target_id",
    "interaction_group_id",
    "interaction_group_sha256",
    "interaction_response_sha256",
    "interaction_request_id",
    "interaction_context_sha256",
    "interaction_checkpoint_id",
    "interaction_predecessor_attempt_id",
    "interaction_predecessor_authorization_sha256",
    "interaction_predecessor_terminal_record_sha256",
    "interaction_action",
    "interaction_response_validator_id",
    "interaction_response_validator_version",
)
_IDENTITY_TOP_LEVEL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "selected_mode",
        "population_selection",
        "source_kind",
        "project_entry_id",
        "sample_id",
        "slide_id",
        "validation_domain_id",
        "analysis_domain_binding",
        "resolved_config_sha256",
        "measurements_result_ref_sha256",
        "configured_mode",
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "admission_plan_sha256",
        "compatibility_signature_sha256",
        "selection_sha256",
        *_LINEAGE_FIELDS,
    }
)
PHENOTYPING_MODE_IDENTITY_FIELDS: Final[tuple[str, ...]] = tuple(
    sorted(
        (_IDENTITY_TOP_LEVEL_FIELDS - {"analysis_domain_binding", "population_selection"})
        | {
            "population_selection.kind",
            "population_selection.class_ids",
            "analysis_domain_binding.project_entry_id",
            "analysis_domain_binding.sample_id",
            "analysis_domain_binding.slide_id",
            "analysis_domain_binding.analysis_domain_id",
            "analysis_domain_binding.analysis_domain_revision",
            "analysis_domain_binding.analysis_domain_revision_sha256",
            "analysis_domain_binding.domain_set_sha256",
        }
    )
)


# ==============================================================================
#                        Batch phenotyping mode artifacts
# ==============================================================================


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise BatchArtifactError("phenotyping-mode mappings require string keys")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BatchArtifactError("phenotyping-mode material must be finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonempty canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _closed_mapping(value: object, *, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed mapping")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} is not the current closed schema")
    return value


@dataclass(frozen=True, slots=True, init=False)
class PhenotypingModeSelectionAuthority:
    """Issuer-only runtime authority for one authenticated mode selection."""

    selected_mode: PhenotypingMode
    population_selection: Mapping[str, object]
    source_kind: str
    project_entry_id: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    analysis_domain_binding: AnalysisDomainBinding
    resolved_config_sha256: str
    measurements_result_ref_sha256: str
    configured_mode: PhenotypingMode | None
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    admission_plan_sha256: str
    compatibility_signature_sha256: str
    interaction_target_id: str | None
    interaction_group_id: str | None
    interaction_group_sha256: str | None
    interaction_response_sha256: str | None
    interaction_request_id: str | None
    interaction_context_sha256: str | None
    interaction_checkpoint_id: str | None
    interaction_predecessor_attempt_id: str | None
    interaction_predecessor_authorization_sha256: str | None
    interaction_predecessor_terminal_record_sha256: str | None
    interaction_action: str | None
    interaction_response_validator_id: str | None
    interaction_response_validator_version: str | None
    selection_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("PhenotypingModeSelectionAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("PhenotypingModeSelectionAuthority cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PhenotypingModeSelectionAuthority is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPhenotypingInteractionLineageProjection:
    """Decoder-only view of the owner-validated interaction lineage."""

    interaction_target_id: str
    interaction_group_id: str
    interaction_group_sha256: str
    interaction_response_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    interaction_checkpoint_id: str
    interaction_predecessor_attempt_id: str
    interaction_predecessor_authorization_sha256: str
    interaction_predecessor_terminal_record_sha256: str
    interaction_action: str
    interaction_response_validator_id: str
    interaction_response_validator_version: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedPhenotypingInteractionLineageProjection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPhenotypingModeProjection:
    """Decoder-only view of a committed phenotyping-mode artifact."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    selected_mode: PhenotypingMode
    population_selection: Mapping[str, object]
    source_kind: str
    project_entry_id: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    analysis_domain_binding: AnalysisDomainBinding
    resolved_config_sha256: str
    measurements_result_ref_sha256: str
    configured_mode: PhenotypingMode | None
    selection_sha256: str
    interaction_lineage: ValidatedPhenotypingInteractionLineageProjection | None

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedPhenotypingModeProjection is decoder-only")


def _projection(**values: object) -> ValidatedPhenotypingModeProjection:
    result = object.__new__(ValidatedPhenotypingModeProjection)
    for name in ValidatedPhenotypingModeProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _lineage_projection(
    selection: PhenotypingModeSelectionAuthority,
) -> ValidatedPhenotypingInteractionLineageProjection | None:
    if selection.source_kind == _SOURCE_CONFIG:
        return None
    result = object.__new__(ValidatedPhenotypingInteractionLineageProjection)
    for name in ValidatedPhenotypingInteractionLineageProjection.__dataclass_fields__:
        object.__setattr__(result, name, getattr(selection, name))
    return result


def phenotyping_mode_identity_contract() -> ArtifactIdentityContract:
    """Return the current owner-controlled selection identity contract."""

    return ArtifactIdentityContract(
        contract_id=_CONTRACT_ID,
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=PHENOTYPING_MODE_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _selection_material(values: Mapping[str, object]) -> dict[str, object]:
    material: dict[str, object] = {}
    for key, value in values.items():
        if key in {"selection_sha256", "_issuer"}:
            continue
        material[key] = (
            value.to_mapping() if isinstance(value, AnalysisDomainBinding) else _plain(value)
        )
    return material


def _authority_values(authority: PhenotypingModeSelectionAuthority) -> dict[str, object]:
    return {
        name: getattr(authority, name)
        for name in PhenotypingModeSelectionAuthority.__dataclass_fields__
    }


def _validate_authority(
    authority: PhenotypingModeSelectionAuthority,
) -> PhenotypingModeSelectionAuthority:
    """Validate authority for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if (
        type(authority) is not PhenotypingModeSelectionAuthority
        or authority._issuer is not _AUTHORITY_ISSUER
    ):
        raise BatchArtifactError("phenotyping-mode selection authority issuer is invalid")
    if not isinstance(authority.selected_mode, PhenotypingMode):
        raise BatchArtifactError("selected phenotyping mode is invalid")
    source_kind = _text(authority.source_kind, "source_kind")
    for field in (
        "project_entry_id",
        "sample_id",
        "slide_id",
        "validation_domain_id",
        "node_id",
        "attempt_id",
    ):
        _text(getattr(authority, field), field)
    for field in (
        "resolved_config_sha256",
        "measurements_result_ref_sha256",
        "attempt_authorization_sha256",
        "admission_plan_sha256",
        "compatibility_signature_sha256",
    ):
        _sha256(getattr(authority, field), field)
    if not isinstance(authority.analysis_domain_binding, AnalysisDomainBinding):
        raise BatchArtifactError("authority requires one typed analysis-domain binding")
    binding = authority.analysis_domain_binding
    if (
        binding.project_entry_id != authority.project_entry_id
        or binding.sample_id != authority.sample_id
        or binding.slide_id != authority.slide_id
    ):
        raise BatchArtifactError("authority scope and analysis-domain binding disagree")
    lineage = {name: getattr(authority, name) for name in _LINEAGE_FIELDS}
    if source_kind == _SOURCE_CONFIG:
        if authority.configured_mode is not authority.selected_mode or any(
            value is not None for value in lineage.values()
        ):
            raise BatchArtifactError(
                "resolved-config mode authority has invalid interaction lineage"
            )
    elif source_kind == _SOURCE_INTERACTION:
        if (
            authority.configured_mode is not None
            or any(value is None for value in lineage.values())
            or authority.interaction_action != InteractionAction.SELECT_PHENOTYPING_MODE.value
            or authority.interaction_response_validator_id != _RESPONSE_VALIDATOR_ID
            or authority.interaction_response_validator_version != _RESPONSE_VALIDATOR_VERSION
        ):
            raise BatchArtifactError("accepted-interaction mode authority has incomplete lineage")
        digest_fields = {
            "interaction_group_sha256",
            "interaction_response_sha256",
            "interaction_context_sha256",
            "interaction_predecessor_authorization_sha256",
            "interaction_predecessor_terminal_record_sha256",
        }
        for field in digest_fields:
            _sha256(getattr(authority, field), field)
        for field in set(_LINEAGE_FIELDS) - digest_fields:
            _text(getattr(authority, field), field)
    else:
        raise BatchArtifactError("phenotyping-mode source kind is invalid")
    expected = _digest(_selection_material(_authority_values(authority)))
    if _sha256(authority.selection_sha256, "selection_sha256") != expected:
        raise BatchArtifactError("phenotyping-mode selection digest is not canonical")
    return authority


def _issue_authority(**values: object) -> PhenotypingModeSelectionAuthority:
    unsigned = dict(values)
    selection = validate_population_settings({"default": unsigned["population_selection"], "slide_overrides": {}})["default"]
    unsigned["population_selection"] = MappingProxyType({"kind": selection["kind"], "class_ids": tuple(selection["class_ids"])})
    unsigned["selection_sha256"] = _digest(_selection_material(unsigned))
    unsigned["_issuer"] = _AUTHORITY_ISSUER
    authority = object.__new__(PhenotypingModeSelectionAuthority)
    for name in PhenotypingModeSelectionAuthority.__dataclass_fields__:
        object.__setattr__(authority, name, unsigned[name])
    return _validate_authority(authority)


def _validated_context(
    context: BatchNodeExecutionContext,
    resolved_sample: ResolvedBatchSample,
    current_execution: BatchCurrentExecutionAuthority,
) -> tuple[BatchArtifactRef, AnalysisDomainBinding]:
    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("phenotyping-mode resolution requires exact execution context")
    if type(resolved_sample) is not ResolvedBatchSample:
        raise BatchArtifactError("phenotyping-mode resolution requires one resolved batch sample")
    if not isinstance(resolved_sample.config, AnalysisConfig):
        raise BatchArtifactError("resolved sample lacks a typed analysis configuration")
    validate_current_execution_authority(current_execution, expected_context=context)
    config_digest = _digest(resolved_sample.config.to_mapping())
    if (
        config_digest != resolved_sample.resolved_config_sha256
        or not resolved_sample.enabled
        or resolved_sample.config.slide_id != resolved_sample.slide_id
    ):
        raise BatchArtifactError("resolved sample configuration authority is invalid")
    contract = phenotyping_mode_identity_contract()
    if (
        current_execution.node_id != context.node_id
        or current_execution.node_kind != _NODE_KIND
        or current_execution.node_scope != "sample"
        or current_execution.validation_domain_id != resolved_sample.validation_domain_id
        or current_execution.participant_ids != (resolved_sample.sample_id,)
        or len(current_execution.project_entry_ids) != 1
        or len(current_execution.analysis_domain_bindings) != 1
        or current_execution.configuration_bindings
        != (
            ArtifactConfigurationBinding(
                resolved_sample.sample_id,
                resolved_sample.resolved_config_sha256,
            ),
        )
        or current_execution.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        or current_execution.adapter_spec_sha256 != context.adapter_spec.spec_sha256
        or current_execution.dispatch_binding_sha256
        != context.authorization.dispatch_binding.dispatch_binding_sha256
        or current_execution.node_material_sha256 != context.authorization.node_material_sha256
        or context.adapter_spec.node_kind != _NODE_KIND
        or tuple(context.adapter_spec.declared_outputs) != (_ARTIFACT_KIND,)
        or tuple(context.adapter_spec.output_identity_contracts) != (contract,)
        or context.authorization.dispatch_binding.participant_ids != (resolved_sample.sample_id,)
    ):
        raise BatchArtifactError("execution context lacks the phenotyping-mode owner boundary")
    if len(context.inputs) != 1 or context.inputs[0].artifact_kind != "measurements_result":
        raise BatchArtifactError("phenotyping-mode resolution requires one measurements result")
    measurement = validate_artifact_ref(context.inputs[0])
    if artifact_ref_set_sha256((measurement,)) != (context.authorization.input_artifact_set_sha256):
        raise BatchArtifactError("measurements result no longer matches authorization")
    requirements = tuple(context.adapter_spec.required_inputs)
    if (
        len(requirements) != 1
        or requirements[0].artifact_kind != "measurements_result"
        or context.adapter_spec.optional_inputs
    ):
        raise BatchArtifactError("phenotyping-mode adapter has an invalid input declaration")
    scope = measurement.scope
    if (
        (measurement.schema_name, measurement.schema_version) != ("roi-generation-manifest", 1)
        or scope.validation_domain_id != resolved_sample.validation_domain_id
        or scope.sample_id != resolved_sample.sample_id
        or scope.slide_id != resolved_sample.slide_id
        or scope.project_entry_id != current_execution.project_entry_ids[0]
        or not scope.analysis_domain_bindings
        or context.authorization.dispatch_binding.project_entry_ids != (scope.project_entry_id,)
        or len(measurement.configuration_bindings) != 1
        or measurement.configuration_bindings[0]
        != ArtifactConfigurationBinding(
            resolved_sample.sample_id, resolved_sample.resolved_config_sha256
        )
        or measurement.compatibility_signature_sha256 != context.compatibility_signature_sha256
    ):
        raise BatchArtifactError("measurements result scope or configuration is foreign")
    target = current_execution.analysis_domain_bindings[0]
    matches = tuple(
        binding
        for binding in scope.analysis_domain_bindings
        if binding.analysis_domain_id == target.analysis_domain_id
    )
    if len(matches) != 1 or matches[0] != target:
        raise BatchArtifactError("measurements result does not contain one exact target binding")
    binding = matches[0]
    return measurement, binding


def _known_biomarkers(measurement: ValidatedMeasurementsResultProjection) -> tuple[str, ...]:
    return tuple(
        biomarker
        for channel in measurement.canonical_channels
        if isinstance((biomarker := channel["biomarker"]), str) and biomarker.strip()
    )


def _available_modes(
    measurement: ValidatedMeasurementsResultProjection,
) -> tuple[PhenotypingMode, ...]:
    biomarkers = _known_biomarkers(measurement)
    if biomarkers:
        return (
            PhenotypingMode.MARKER_RULES,
            PhenotypingMode.SUPERVISED_CLASSIFIER,
            PhenotypingMode.UNSUPERVISED_CLUSTERING,
        )
    return (
        PhenotypingMode.UNSUPERVISED_CLUSTERING,
        PhenotypingMode.SUPERVISED_CLASSIFIER,
    )


def _mode_interaction(
    *,
    sample: ResolvedBatchSample,
    measurement: ValidatedMeasurementsResultProjection,
    binding: AnalysisDomainBinding,
) -> StageResult:
    available = _available_modes(measurement)
    biomarkers = _known_biomarkers(measurement)
    from mif_pipeline.cell_analysis.measurement_rows import MarkerIntensityRowsDescriptor, read_measurement_dataset_selection
    reference = measurement.measurement_rows
    header = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(path=reference.path, sha256=reference.sha256,
            byte_count=reference.byte_count, row_count=reference.row_count,
            content_schema_version=reference.content_schema_version),
        authenticated_output_root=reference.path.parent.parent.parent)
    kinds = ["all_segmented"]
    classes = {"primary_structure": [], "secondary_structure": []}
    if header["population_sources"] is not None:
        kinds.append("detected_tissue")
        for kind in classes:
            per_domain = [{class_id for source in header["population_sources"]
                if source["kind"] == kind and source["analysis_domain_id"] == domain
                for class_id in source["class_ids"]} for domain in header["analysis_domain_ids"]]
            classes[kind] = sorted(set.intersection(*per_domain) if per_domain else set())
            if classes[kind]:
                kinds.append(kind)
    options = {"slides": [{"image_identity": slide_population_key(sample.sample_id, binding.project_entry_id, sample.slide_id),
        "sample_id": sample.sample_id, "project_entry_id": binding.project_entry_id, "slide_id": sample.slide_id,
        "available_kinds": kinds,
        "primary_classes": [{"class_id": key, "label": key} for key in classes["primary_structure"]],
        "secondary_classes": [{"class_id": key, "label": key} for key in classes["secondary_structure"]]}]}
    effective = resolve_population_selection(sample.config.phenotyping.population,
        sample_id=sample.sample_id, project_entry_id=binding.project_entry_id, slide_id=sample.slide_id)
    resume_context = {
        "schema_version": _INTERACTION_SCHEMA_VERSION,
        "project_entry_id": binding.project_entry_id,
        "sample_id": sample.sample_id,
        "slide_id": sample.slide_id,
        "validation_domain_id": sample.validation_domain_id,
        "analysis_domain_binding_sha256": _digest(binding.to_mapping()),
        "resolved_config_sha256": sample.resolved_config_sha256,
        "measurements_result_ref_sha256": measurement.artifact_ref.artifact_ref_sha256,
        "configured_mode": None,
        "phenotyping_population": {"default": effective, "slide_overrides": {}},
        "population_options": options,
        "available_modes": [item.value for item in available],
        "recommended_mode": available[0].value,
        "known_biomarker_ids": list(biomarkers),
        "unavailable_modes": {} if biomarkers else {
            PhenotypingMode.MARKER_RULES.value: "Marker rules require at least one canonical biomarker."},
    }
    return StageResult.needs_interaction(
        stage=_NODE_KIND,
        slide_id=sample.slide_id,
        reason=InteractionReason.PHENOTYPING_MODE_REQUIRED,
        affected_object_ids=(binding.analysis_domain_id,),
        message="Choose which cells to phenotype, then how to assign their phenotypes.",
        allowed_actions=(
            InteractionAction.SELECT_PHENOTYPING_MODE.value,
            InteractionAction.ABORT.value,
        ),
        resume_context=resume_context,
        checkpoint_id="phenotyping-mode-" + _digest(resume_context)[:48],
    )


def _response_authority(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    expected_request: StageResult,
) -> Mapping[str, object] | None:
    from .batch_interactions import (
        INTERACTION_RESPONSE_SCHEMA_NAME,
        INTERACTION_RESPONSE_SCHEMA_VERSION,
    )
    from .batch_state import BatchStateStore, batch_interaction_request_id

    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("phenotyping-mode response requires the batch state owner")
    response = state_store.load_validated_interaction_response(context.authorization)
    authorization = context.authorization
    if response is None:
        if (
            authorization.interaction_response_sha256 is not None
            or authorization.interaction_predecessor_terminal_record_sha256 is not None
        ):
            raise BatchArtifactError("interaction authorization lacks its state-owned response")
        return None
    fields = frozenset(
        {
            "schema_name",
            "schema_version",
            "target_id",
            "group_id",
            "group_sha256",
            "node_id",
            "prior_attempt_id",
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "interaction_request_id",
            "interaction_context_sha256",
            "action",
            "response",
            "response_validator_id",
            "response_validator_version",
            "response_sha256",
        }
    )
    response = _closed_mapping(response, fields=fields, field="interaction response")
    unsigned = {key: value for key, value in response.items() if key != "response_sha256"}
    response_sha256 = _sha256(response["response_sha256"], "interaction response_sha256")
    if (
        response_sha256 != _digest(unsigned)
        or response_sha256 != authorization.interaction_response_sha256
        or response["schema_name"] != INTERACTION_RESPONSE_SCHEMA_NAME
        or response["schema_version"] != INTERACTION_RESPONSE_SCHEMA_VERSION
        or response["node_id"] != context.node_id
        or response["prior_terminal_record_sha256"]
        != authorization.interaction_predecessor_terminal_record_sha256
        or response["action"] != InteractionAction.SELECT_PHENOTYPING_MODE.value
        or response["response_validator_id"] != _RESPONSE_VALIDATOR_ID
        or response["response_validator_version"] != _RESPONSE_VALIDATOR_VERSION
        or authorization.retry_ordinal <= 0
    ):
        raise BatchArtifactError("interaction response authority or validator differs")
    interaction_context_sha256 = _sha256(
        response["interaction_context_sha256"], "interaction_context_sha256"
    )
    expected_context_sha256 = _digest(expected_request.to_mapping()["resume_context"])
    request_id = batch_interaction_request_id(
        node_id=context.node_id,
        attempt_id=_text(response["prior_attempt_id"], "prior_attempt_id"),
        attempt_authorization_sha256=_sha256(
            response["prior_attempt_authorization_sha256"],
            "prior_attempt_authorization_sha256",
        ),
        interaction_context_sha256=interaction_context_sha256,
    )
    if (
        interaction_context_sha256 != expected_context_sha256
        or response["interaction_request_id"] != request_id
    ):
        raise BatchArtifactError("interaction request or context identity differs")
    request = state_store.load_interaction_request(request_id)
    if request != expected_request:
        raise BatchArtifactError("interaction request or checkpoint is not current")
    validated_response = response["response"]
    if not isinstance(validated_response, Mapping):
        raise BatchArtifactError("validated phenotyping response is malformed")
    try:
        selected_mode = PhenotypingMode(validated_response.get("phenotyping_mode"))
        expected_validated = validate_interaction_response(
            request,
            InteractionAction.SELECT_PHENOTYPING_MODE.value,
            {"mode": selected_mode.value,
             "phenotyping_population": validated_response.get("phenotyping_population")},
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("phenotyping-mode interaction response is invalid") from exc
    if _plain(validated_response) != expected_validated:
        raise BatchArtifactError("phenotyping-mode interaction validation result differs")
    return response


def resolve_phenotyping_mode_selection(
    context: BatchNodeExecutionContext,
    *,
    resolved_sample: ResolvedBatchSample,
    state_store: BatchStateStore,
) -> PhenotypingModeSelectionAuthority | StageResult:
    """Resolve one mode solely from current configuration or state-owned response."""

    from .batch_state import BatchStateError, BatchStateStore

    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("phenotyping-mode resolution requires the exact state owner")
    try:
        current_execution = state_store.authenticate_current_execution_context(context)
    except BatchStateError as exc:
        raise BatchArtifactError("phenotyping-mode execution context is not current") from exc
    measurement_ref, binding = _validated_context(context, resolved_sample, current_execution)
    try:
        measurement_authority = state_store.authenticate_available_artifact(measurement_ref)
        measurement = decode_committed_measurements_result(measurement_authority)
    except (BatchStateError, BatchArtifactError) as exc:
        raise BatchArtifactError(
            "phenotyping mode requires current committed measurements"
        ) from exc
    if (
        measurement.artifact_ref != measurement_ref
        or measurement.state_head_sha256 != current_execution.state_head_sha256
        or measurement.active_plan_sha256 != current_execution.active_plan_sha256
        or measurement.sample_id != resolved_sample.sample_id
        or measurement.slide_id != resolved_sample.slide_id
        or measurement.project_entry_id != binding.project_entry_id
        or binding not in measurement.analysis_domain_bindings
    ):
        raise BatchArtifactError("phenotyping measurements projection differs from current target")
    expected_request = _mode_interaction(
        sample=resolved_sample, measurement=measurement, binding=binding
    )
    configured = resolved_sample.config.phenotyping.mode
    if configured is not None and not isinstance(configured, PhenotypingMode):
        raise BatchArtifactError("configured phenotyping mode is invalid")
    node = state_store._load_plan_snapshot(current_execution.active_plan_sha256).node_by_id[context.node_id]
    if "settled_review_revision" in node:
        # An explicit return reopens this region's choice even when the original
        # analysis supplied a default. Its prior decision remains saved history.
        state_store.settled_review_predecessor(context, execution=current_execution)
        configured = None
    if configured is not None and context.authorization.interaction_response_sha256 is not None:
        raise BatchArtifactError("phenotyping mode requires exactly one authoritative source")
    response = _response_authority(
        context, state_store=state_store, expected_request=expected_request
    )
    if configured is not None and response is not None:
        raise BatchArtifactError("phenotyping mode requires exactly one authoritative source")
    if configured is None and response is None:
        return expected_request

    lineage: dict[str, object]
    if configured is not None:
        selected = configured
        source_kind = _SOURCE_CONFIG
        lineage = {field: None for field in _LINEAGE_FIELDS}
    else:
        assert response is not None
        validated_response = response["response"]
        assert isinstance(validated_response, Mapping)
        selected = PhenotypingMode(validated_response["phenotyping_mode"])
        source_kind = _SOURCE_INTERACTION
        lineage = {
            "interaction_target_id": response["target_id"],
            "interaction_group_id": response["group_id"],
            "interaction_group_sha256": response["group_sha256"],
            "interaction_response_sha256": response["response_sha256"],
            "interaction_request_id": response["interaction_request_id"],
            "interaction_context_sha256": response["interaction_context_sha256"],
            "interaction_checkpoint_id": expected_request.checkpoint_id,
            "interaction_predecessor_attempt_id": response["prior_attempt_id"],
            "interaction_predecessor_authorization_sha256": response[
                "prior_attempt_authorization_sha256"
            ],
            "interaction_predecessor_terminal_record_sha256": response[
                "prior_terminal_record_sha256"
            ],
            "interaction_action": response["action"],
            "interaction_response_validator_id": response["response_validator_id"],
            "interaction_response_validator_version": response["response_validator_version"],
        }
    authorization = context.authorization
    settings = resolved_sample.config.phenotyping.population if configured is not None else validated_response["phenotyping_population"]
    population = resolve_population_selection(settings,
        sample_id=resolved_sample.sample_id, project_entry_id=binding.project_entry_id,
        slide_id=resolved_sample.slide_id)
    return _issue_authority(
        selected_mode=selected,
        population_selection=population,
        source_kind=source_kind,
        project_entry_id=binding.project_entry_id,
        sample_id=resolved_sample.sample_id,
        slide_id=resolved_sample.slide_id,
        validation_domain_id=resolved_sample.validation_domain_id,
        analysis_domain_binding=binding,
        resolved_config_sha256=resolved_sample.resolved_config_sha256,
        measurements_result_ref_sha256=measurement.artifact_ref.artifact_ref_sha256,
        configured_mode=configured,
        node_id=context.node_id,
        attempt_id=authorization.attempt_id,
        attempt_authorization_sha256=authorization.authorization_sha256,
        admission_plan_sha256=authorization.admission_plan_sha256,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        **lineage,
    )


def _identity(authority: PhenotypingModeSelectionAuthority) -> dict[str, object]:
    values = _authority_values(authority)
    return {
        name: (
            values[name].to_mapping() if name == "analysis_domain_binding" else _plain(values[name])
        )
        for name in _IDENTITY_TOP_LEVEL_FIELDS
    }


def build_phenotyping_mode_execution_result(
    authority: PhenotypingModeSelectionAuthority,
) -> BatchNodeExecutionResult:
    """Build the sole locatorless output from one issuer-owned selection."""

    authority = _validate_authority(authority)
    contract = phenotyping_mode_identity_contract()
    output = BatchArtifactRef(
        artifact_id="phenotyping-mode-" + authority.selection_sha256[:48],
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authority.node_id,
        producer_attempt_id=authority.attempt_id,
        producer_attempt_authorization_sha256=authority.attempt_authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=authority.validation_domain_id,
            sample_id=authority.sample_id,
            slide_id=authority.slide_id,
            project_entry_id=authority.project_entry_id,
            analysis_domain_bindings=(authority.analysis_domain_binding,),
        ),
        owning_identity=_identity(authority),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=authority.compatibility_signature_sha256,
        configuration_bindings=(
            ArtifactConfigurationBinding(authority.sample_id, authority.resolved_config_sha256),
        ),
    )
    return BatchNodeExecutionResult(
        authorization_sha256=authority.attempt_authorization_sha256,
        outputs=(output,),
    )


def _authority_from_identity(ref: BatchArtifactRef) -> PhenotypingModeSelectionAuthority:
    identity = _closed_mapping(
        ref.owning_identity,
        fields=_IDENTITY_TOP_LEVEL_FIELDS,
        field="phenotyping-mode owning identity",
    )
    try:
        selected = PhenotypingMode(identity["selected_mode"])
        configured = (
            None
            if identity["configured_mode"] is None
            else PhenotypingMode(identity["configured_mode"])
        )
        binding = AnalysisDomainBinding.from_mapping(identity["analysis_domain_binding"])
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("phenotyping-mode identity is invalid") from exc
    values = {
        **{name: identity[name] for name in _IDENTITY_TOP_LEVEL_FIELDS},
        "selected_mode": selected,
        "configured_mode": configured,
        "analysis_domain_binding": binding,
    }
    supplied_digest = values.pop("selection_sha256")
    selection = _issue_authority(**values)
    if selection.selection_sha256 != supplied_digest:
        raise BatchArtifactError("phenotyping-mode committed digest is tampered")
    return selection


def decode_committed_phenotyping_mode(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedPhenotypingModeProjection:
    """Decode one current state-issued selection into a typed read-only view."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("phenotyping-mode decoding requires committed state authority")
    validate_committed_artifact_authority_set((authority,))
    if authority.producer_node_kind != _NODE_KIND:
        raise BatchArtifactError("phenotyping-mode committed producer is invalid")
    ref = validate_artifact_ref(authority.artifact_ref)
    contract = phenotyping_mode_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or dict(ref.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
        or authority.execution_exchange_sha256 is not None
    ):
        raise BatchArtifactError("phenotyping-mode ref lacks its compact owner contract")
    selection = _authority_from_identity(ref)
    if (
        ref.artifact_id != "phenotyping-mode-" + selection.selection_sha256[:48]
        or selection.node_id != ref.producer_node_id
        or selection.attempt_id != ref.producer_attempt_id
        or selection.attempt_authorization_sha256 != ref.producer_attempt_authorization_sha256
        or selection.compatibility_signature_sha256 != ref.compatibility_signature_sha256
        or ref.scope
        != ArtifactScope(
            selection.validation_domain_id,
            selection.sample_id,
            selection.slide_id,
            selection.project_entry_id,
            (selection.analysis_domain_binding,),
        )
        or ref.configuration_bindings
        != (ArtifactConfigurationBinding(selection.sample_id, selection.resolved_config_sha256),)
    ):
        raise BatchArtifactError("phenotyping-mode ref scope or authorization differs")
    return _projection(
        artifact_ref=ref,
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
        selected_mode=selection.selected_mode,
        population_selection=selection.population_selection,
        source_kind=selection.source_kind,
        project_entry_id=selection.project_entry_id,
        sample_id=selection.sample_id,
        slide_id=selection.slide_id,
        validation_domain_id=selection.validation_domain_id,
        analysis_domain_binding=selection.analysis_domain_binding,
        resolved_config_sha256=selection.resolved_config_sha256,
        measurements_result_ref_sha256=selection.measurements_result_ref_sha256,
        configured_mode=selection.configured_mode,
        selection_sha256=selection.selection_sha256,
        interaction_lineage=_lineage_projection(selection),
    )


__all__ = [
    "PHENOTYPING_MODE_IDENTITY_FIELDS",
    "ValidatedPhenotypingInteractionLineageProjection",
    "PhenotypingModeSelectionAuthority",
    "ValidatedPhenotypingModeProjection",
    "build_phenotyping_mode_execution_result",
    "decode_committed_phenotyping_mode",
    "phenotyping_mode_identity_contract",
    "resolve_phenotyping_mode_selection",
]
