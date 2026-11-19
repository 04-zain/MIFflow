"""Locatorless committed identity for a native tissue detector setup.

The `ValidatedTissueSetupResultArtifactRefProjection` record carries `artifact_ref`,
`sample_id`, `slide_id`, `validation_domain_id`, `project_entry_id` and its remaining
declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
from typing import Final
from urllib.parse import urlparse
from urllib.request import url2pathname

from mif_pipeline.pipeline_core import AnalysisConfig, InteractionReason, StageStatus
from mif_pipeline.pipeline_core.config import TissueDetectionConfig, TissueDetectionParameters
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.tissue_detection.resolution import NativePyramidLevel
from mif_pipeline.tissue_detection.setup import (
    MAX_TISSUE_CLASSIFIER_BYTES,
    build_tissue_setup_request,
    resolve_tissue_setup_observation,
)

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from .batch_project import reopen_project_catalogue_entry_artifact
from .project_stage_plan import ProjectStageRecordError, ProjectStageTargetExecutionEvidence

_ARTIFACT_KIND: Final[str] = "tissue_setup_result"
_NODE_KIND: Final[str] = "tissue_setup"
_SCHEMA_NAME: Final[str] = "tissue-detector-setup"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-tissue-setup-artifacts/v1"
_EVIDENCE_SCHEMA_VERSION: Final[str] = "mif-batch-tissue-setup/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_TOP_FIELDS = {
    "schema_version",
    "sample_id",
    "slide_id",
    "validation_domain_id",
    "project_entry_id",
    "canonical_image_id",
    "canonical_image_sha256",
    "project_identity_sha256",
    "project_entry_projection_sha256",
    "project_saved_state_attestation_sha256",
    "analysis_frame_ref_sha256",
    "project_catalogue_entry_ref_sha256",
    "resolved_config_sha256",
    "authorized_entry_sha256",
    "target_observation_sha256",
    "execution_exchange_sha256",
    "validated_exchange_sha256",
    "scientific_request_body_sha256",
    "tissue_setup_result_sha256",
}
_TISSUE_FIELDS = {
    "detector_id",
    "detector_version",
    "classifier_name",
    "classifier_sha256",
    "reference_channel_id",
    "reference_channel_selection_source",
    "native_level_selection_mode",
    "native_resolution_index",
    "resolution_um_per_px",
    "native_level_match_tolerance_fraction",
    "allow_level0_selection",
    "minimum_object_area_um2",
    "review_policy",
    "validation_domain_id",
    "stage_version",
}
_MATCHING_FIELDS = {
    "min_iou",
    "max_centroid_displacement_um",
    "max_relative_area_change",
    "min_uniqueness_margin",
}
_REQUEST_FIELDS = {
    "schema_version",
    "action",
    "classifier_name",
    "expected_classifier_sha256",
    "reference_channel_id",
    "channel_index",
    "channel_name",
}
_LEVEL_FIELDS = {
    "native_resolution_index",
    "x_downsample",
    "y_downsample",
    "x_um_per_px",
    "y_um_per_px",
}
_CHANNEL_FIELDS = {"channel_id", "name", "component", "fluorophore", "biomarker"}
_OBSERVATION_FIELDS = {
    "schema_version",
    "classifier_name",
    "classifier_sha256",
    "classifier_byte_count",
    "classifier_type",
    "reference_channel_id",
    "channel_index",
    "channel_name",
    "compatible_native_resolution_indices",
}
_LABEL_FIELDS = {"value", "name", "ignored"}

TISSUE_SETUP_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(
        _TOP_FIELDS
        | {f"tissue_detection.{field}" for field in _TISSUE_FIELDS}
        | {f"tissue_detection.matching.{field}" for field in _MATCHING_FIELDS}
        | {f"tissue_setup_request.{field}" for field in _REQUEST_FIELDS}
        | {f"tissue_setup_request.native_pyramid_inventory.{field}" for field in _LEVEL_FIELDS}
        | {f"tissue_setup_request.channels.{field}" for field in _CHANNEL_FIELDS}
        | {"tissue_setup_request.permitted_native_resolution_indices"}
        | {f"native_observation.{field}" for field in _OBSERVATION_FIELDS}
        | {"native_observation.input_resolution_um.x", "native_observation.input_resolution_um.y"}
        | {f"native_observation.classification_labels.{field}" for field in _LABEL_FIELDS}
    )
)
_OPTIONAL_FIELDS: tuple[str, ...] = tuple(
    sorted(
        {
            "tissue_setup_request.source",
            "tissue_setup_request.source.path",
            "tissue_setup_request.source.sha256",
            "tissue_setup_request.source.byte_count",
            "native_observation.source",
            "native_observation.source.sha256",
            "native_observation.source.byte_count",
        }
    )
)


# ==============================================================================
#                          Batch tissue setup artifacts
# ==============================================================================


def tissue_setup_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-tissue-setup-result-identity/v1",
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=TISSUE_SETUP_RESULT_IDENTITY_FIELDS,
        optional_fields=_OPTIONAL_FIELDS,
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _frozen(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_frozen(item) for item in value)
    return value


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("tissue setup identity is not canonical JSON") from exc


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return value


def _closed(value: object, fields: set[str], field: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise BatchArtifactError(f"{field} requires exact string-keyed fields")
    return value


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueSetupResultArtifactRefProjection:
    """Passive strict projection without a current-state claim."""

    artifact_ref: BatchArtifactRef
    sample_id: str
    slide_id: str
    validation_domain_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    project_identity_sha256: str
    project_entry_projection_sha256: str
    project_saved_state_attestation_sha256: str
    analysis_frame_ref_sha256: str
    project_catalogue_entry_ref_sha256: str
    resolved_config_sha256: str
    tissue_detection: TissueDetectionConfig
    tissue_setup_request: Mapping[str, object]
    native_observation: Mapping[str, object]
    native_pyramid_inventory: tuple[NativePyramidLevel, ...]
    authorized_entry_sha256: str
    target_observation_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    scientific_request_body_sha256: str
    tissue_setup_result_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("tissue setup projection is decoder-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tissue setup projection is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueSetupResultProjection(ValidatedTissueSetupResultArtifactRefProjection):
    """State-authenticated committed tissue setup projection."""

    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("committed tissue setup projection is decoder-only")


def _projection(kind: type[object], **values: object) -> object:
    result = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def _project_path(uri: str) -> str:
    parsed = urlparse(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        raise BatchArtifactError("tissue setup catalogue project URI is not local")
    return str(Path(url2pathname(parsed.path)))


def _validated_evidence(evidence: ProjectStageTargetExecutionEvidence):
    """Resolve the native tissue result against its selected image and request.

    We use the retained result returned by the exchange parser and reopen its
    current inputs once. Scope, image, configuration and saved-hierarchy checks
    connect that result to the tissue step. The returned records supply the
    selected detector or its required review form.
    """
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError("tissue setup requires issuer-owned execution evidence")
    try:
        inputs = evidence.reopen_validation_inputs()
    except ProjectStageRecordError as exc:
        raise BatchArtifactError("tissue setup validation inputs cannot be reopened") from exc
    context, authorized, observation = evidence.context, evidence.authorized, evidence.observation
    contract = tissue_setup_result_identity_contract()
    # We consume the parsed immutable result after reopening its current inputs.
    # The exchange parser already established the source bytes and result scope.
    if (
        type(context) is not BatchNodeExecutionContext
        or context.adapter_spec.node_kind != _NODE_KIND
        or context.adapter_spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        or context.adapter_spec.resource_kind != "qupath_image_write"
        or context.adapter_spec.declared_outputs != (_ARTIFACT_KIND,)
        or context.adapter_spec.output_identity_contracts != (contract,)
        or context.adapter_spec.optional_inputs
        or context.authorization.dispatch_binding.participant_ids != (authorized.sample_id,)
        or context.authorization.dispatch_binding.project_entry_ids
        != (authorized.project_entry_id,)
        or {item.artifact_kind for item in context.adapter_spec.required_inputs}
        != {"analysis_frame", "project_catalogue_entry"}
        or len(context.adapter_spec.required_inputs) != 2
        or len(context.inputs) != 2
        or {item.artifact_kind for item in context.inputs}
        != {"analysis_frame", "project_catalogue_entry"}
        or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
        or tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        != authorized.prerequisite_artifact_ref_sha256s
        or context.node_id != authorized.node_id
        or context.adapter_spec.spec_sha256 != authorized.adapter_spec_sha256
        or authorized.wire_stage_id != _NODE_KIND
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.image_role != "primary_mif"
        or authorized.analysis_domain_bindings
        or authorized.request_codec_id != QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value
        or authorized.request_codec_version != "1"
        or context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or observation.saved_state_before_sha256 != authorized.saved_state_before_sha256
        or observation.saved_state_disposition == "unattestable"
        or observation.saved_state_after_sha256 is None
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
        or evidence.generation_manifest is not None
        or observation.generation_manifest_ref != authorized.exchange.generation_manifest_ref
        or observation.generation_manifest_payload_sha256 is not None
        or observation.generation_manifest_byte_count is not None
    ):
        raise BatchArtifactError("tissue setup execution evidence scope or codec differs")
    by_kind = {item.artifact_kind: item for item in context.inputs}
    try:
        frame_ref = by_kind["analysis_frame"]
        catalogue_ref = by_kind["project_catalogue_entry"]
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        catalogue, entry = reopen_project_catalogue_entry_artifact(catalogue_ref)
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue setup source artifacts are invalid") from exc
    if (
        frame_ref.configuration_bindings != catalogue_ref.configuration_bindings
        or len(frame_ref.configuration_bindings) != 1
        or frame_ref.configuration_bindings[0].sample_id != authorized.sample_id
        or frame_ref.configuration_bindings[0].resolved_config_sha256
        != inputs.resolved_config_sha256
        or frame_ref.scope.sample_id != authorized.sample_id
        or frame_ref.scope.slide_id != authorized.slide_id
        or frame_ref.scope.validation_domain_id != entry.validation_domain_id
        or catalogue_ref.scope.project_entry_id != authorized.project_entry_id
        or entry.image_role != "primary_mif"
        or entry.sample_id != authorized.sample_id
        or entry.slide_id != authorized.slide_id
        or entry.image_sha256 != frame.spatial_frame.canonical_image_sha256
        or catalogue.project_identity_sha256 != context.project_identity_sha256
        or entry.entry_projection_sha256 != context.project_entry_projection_sha256
    ):
        raise BatchArtifactError("tissue setup frame, catalogue, or config binding differs")
    return context, authorized, observation, inputs, frame_ref, catalogue_ref, frame, catalogue


def build_tissue_setup_result_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    review_return: bool = False,
) -> BatchNodeExecutionResult:
    """Publish only a fully observed saved detector; a selection prompt has no artifact."""
    context, authorized, observation, inputs, frame_ref, catalogue_ref, frame, catalogue = (
        _validated_evidence(evidence)
    )
    body = _closed(
        _plain(inputs.scientific_request_body),
        {"schema_version", "sample_id", "tissue_parameters", "tissue_setup"},
        "tissue setup scientific body",
    )
    request = body["tissue_setup"]
    if (
        body["schema_version"] != "roi-workflow-invocation/v1"
        or body["sample_id"] != authorized.sample_id
        or not isinstance(request, Mapping)
    ):
        raise BatchArtifactError("tissue setup scientific request scope differs")
    try:
        config = AnalysisConfig.from_mapping(_plain(inputs.resolved_config))
        parameters = (
            None
            if body["tissue_parameters"] is None
            else TissueDetectionParameters.from_mapping(
                body["tissue_parameters"], field="tissue_detection"
            )
        )
        resolved_channels = resolve_analysis_channels(
            config,
            frame_ref,
            sample_id=authorized.sample_id,
            validation_domain_id=frame_ref.scope.validation_domain_id,
        )
        channels = [item.to_mapping() for item in resolved_channels]
        if (
            request.get("channels") != channels
            or tuple(item["channel_id"] for item in channels) != frame.ordered_channel_ids
        ):
            raise BatchArtifactError("tissue setup request channels differ from admitted frame")
        if config.tissue_detection is not None and not review_return:
            configured = config.tissue_detection.to_mapping()
            configured_hash = configured.pop("classifier_sha256")
            if parameters is None or parameters.to_mapping() != configured:
                raise BatchArtifactError(
                    "tissue setup parameters differ from admitted configuration"
                )
            if request.get("expected_classifier_sha256") != configured_hash:
                raise BatchArtifactError(
                    "tissue setup expected saved digest differs from admission"
                )
        if (parameters is None) != (request.get("action") == "request_selection"):
            raise BatchArtifactError("tissue setup request and parameters are inconsistent")
        levels = NativePyramidLevel.sequence_from_mappings(request.get("native_pyramid_inventory"))
        rebuilt = build_tissue_setup_request(
            parameters=parameters,
            channels=resolved_channels,
            native_pyramid_inventory=[item.to_mapping() for item in levels],
            source=request.get("source"),
            expected_classifier_sha256=request.get("expected_classifier_sha256"),
        )
        if rebuilt != request:
            raise BatchArtifactError("tissue setup native request differs from science")
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "tissue setup request or admitted configuration is invalid"
        ) from exc
    result = evidence.wire_stage_result
    if (
        result.stage != _NODE_KIND
        or result.slide_id != authorized.slide_id
        or result.generation_id is not None
    ):
        raise BatchArtifactError("tissue setup wire result has foreign stage or generation")
    if result.status is StageStatus.NEEDS_INTERACTION:
        if parameters is not None or result.outputs is not None:
            raise BatchArtifactError("concrete tissue setup cannot return initial prompt")
        prompt = _plain(result.resume_context)
        expected_context = {
            "schema_version": "tissue-detector-setup-interaction/v1",
            "available_classifiers": prompt.get("available_classifiers"),
            "channels": channels,
            "native_pyramid_inventory": [item.to_mapping() for item in levels],
            "project_path": _project_path(catalogue.project_uri),
        }
        if (
            result.interaction_reason is not InteractionReason.TISSUE_DETECTOR_SETUP_REQUIRED
            or result.allowed_actions
            != ("select_tissue_detector", "import_tissue_detector", "refresh_tissue_detectors")
            or result.message != "Choose a tissue detector and its settings."
            or not isinstance(prompt, Mapping)
            or set(prompt) != set(expected_context) | {"checkpoint_id"}
            or any(prompt.get(key) != value for key, value in expected_context.items())
            or prompt.get("checkpoint_id") != "tissue-setup-" + _digest(expected_context)[:48]
            or result.checkpoint_id != prompt.get("checkpoint_id")
        ):
            raise BatchArtifactError("tissue setup prompt differs from authenticated request")
        resources = prompt["available_classifiers"]
        if not isinstance(resources, list) or len(resources) > 256:
            raise BatchArtifactError("tissue setup resource inventory is invalid")
        names = []
        from mif_pipeline.pipeline_core.config import validate_tissue_classifier_name

        for row in resources:
            row = _closed(row, {"classifier_name", "sha256", "byte_count"}, "offered detector")
            names.append(validate_tissue_classifier_name(row["classifier_name"]))
            _sha(row["sha256"], "offered detector sha256")
            if type(row["byte_count"]) is not int or not 1 <= row["byte_count"] <= MAX_TISSUE_CLASSIFIER_BYTES:
                raise BatchArtifactError("offered detector byte_count is invalid")
        if names != sorted(set(names)):
            raise BatchArtifactError("offered detector names are not sorted and unique")
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    if (
        result.status is not StageStatus.COMPLETED
        or parameters is None
        or result.outputs is None
        or set(result.outputs) != {"tissue_setup_observation"}
    ):
        raise BatchArtifactError("tissue setup requires one completed native observation")
    native_observation = _plain(result.outputs["tissue_setup_observation"])
    try:
        tissue = resolve_tissue_setup_observation(
            parameters=parameters,
            request=request,
            observation=native_observation,
        )
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue setup native scientific observation is invalid") from exc
    spatial = frame.spatial_frame
    if spatial.canonical_image_sha256 is None:
        raise BatchArtifactError("tissue setup requires complete canonical image identity")
    unsigned = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "sample_id": authorized.sample_id,
        "slide_id": authorized.slide_id,
        "validation_domain_id": frame_ref.scope.validation_domain_id,
        "project_entry_id": authorized.project_entry_id,
        "canonical_image_id": spatial.canonical_image_id,
        "canonical_image_sha256": spatial.canonical_image_sha256,
        "project_identity_sha256": catalogue.project_identity_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "project_saved_state_attestation_sha256": observation.saved_state_after_sha256,
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "project_catalogue_entry_ref_sha256": catalogue_ref.artifact_ref_sha256,
        "resolved_config_sha256": inputs.resolved_config_sha256,
        "tissue_detection": tissue.to_mapping(),
        "tissue_setup_request": _plain(request),
        "native_observation": native_observation,
        "authorized_entry_sha256": authorized.authorized_entry_sha256,
        "target_observation_sha256": observation.observation_sha256,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "scientific_request_body_sha256": inputs.scientific_request_body_sha256,
    }
    identity = {**unsigned, "tissue_setup_result_sha256": _digest(unsigned)}
    contract = tissue_setup_result_identity_contract()
    artifact = BatchArtifactRef(
        artifact_id="tissue-setup-" + identity["tissue_setup_result_sha256"][:48],
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            frame_ref.scope.validation_domain_id,
            authorized.sample_id,
            authorized.slide_id,
            authorized.project_entry_id,
            (),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": frame.qupath_executable_version,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=frame_ref.configuration_bindings,
    )
    validate_tissue_setup_result_artifact_ref(artifact)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def validate_tissue_setup_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedTissueSetupResultArtifactRefProjection:
    """Recheck the closed scientific projection without granting live authority.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""
    if type(artifact_ref) is not BatchArtifactRef:
        raise BatchArtifactError("tissue setup artifact must be an exact BatchArtifactRef")
    ref = validate_artifact_ref(artifact_ref)
    contract = tissue_setup_result_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.producer_version != _PRODUCER_VERSION
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
    ):
        raise BatchArtifactError("tissue setup artifact owner or locator differs")
    identity = _closed(
        _plain(ref.owning_identity),
        _TOP_FIELDS | {"tissue_detection", "tissue_setup_request", "native_observation"},
        "tissue setup identity",
    )
    unsigned = {
        key: value for key, value in identity.items() if key != "tissue_setup_result_sha256"
    }
    digest = _sha(identity["tissue_setup_result_sha256"], "tissue setup result sha256")
    if _digest(unsigned) != digest or ref.artifact_id != "tissue-setup-" + digest[:48]:
        raise BatchArtifactError("tissue setup identity digest differs")
    for field in (
        "canonical_image_sha256",
        "project_identity_sha256",
        "project_entry_projection_sha256",
        "project_saved_state_attestation_sha256",
        "analysis_frame_ref_sha256",
        "project_catalogue_entry_ref_sha256",
        "resolved_config_sha256",
        "authorized_entry_sha256",
        "target_observation_sha256",
        "execution_exchange_sha256",
        "validated_exchange_sha256",
        "scientific_request_body_sha256",
    ):
        _sha(identity[field], field)
    if identity["schema_version"] != _EVIDENCE_SCHEMA_VERSION:
        raise BatchArtifactError("tissue setup evidence schema differs")
    try:
        tissue = TissueDetectionConfig.from_mapping(
            identity["tissue_detection"], field="tissue_detection"
        )
        request = identity["tissue_setup_request"]
        native = identity["native_observation"]
        parameters = TissueDetectionParameters.from_mapping(
            {
                key: value
                for key, value in tissue.to_mapping().items()
                if key != "classifier_sha256"
            },
            field="tissue_detection",
        )
        resolved = resolve_tissue_setup_observation(
            parameters=parameters, request=request, observation=native
        )
        levels = NativePyramidLevel.sequence_from_mappings(request["native_pyramid_inventory"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue setup scientific identity is invalid") from exc
    if (
        resolved != tissue
        or identity["validation_domain_id"] != tissue.validation_domain_id
        or identity["canonical_image_sha256"] is None
        or dict(ref.runtime_versions)
        != {
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": ref.runtime_versions.get("qupath"),
        }
        or not isinstance(ref.runtime_versions.get("qupath"), str)
        or ref.scope
        != ArtifactScope(
            identity["validation_domain_id"],
            identity["sample_id"],
            identity["slide_id"],
            identity["project_entry_id"],
            (),
        )
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != identity["sample_id"]
        or ref.configuration_bindings[0].resolved_config_sha256
        != identity["resolved_config_sha256"]
    ):
        raise BatchArtifactError("tissue setup scope, runtime, or configuration differs")
    return _projection(
        ValidatedTissueSetupResultArtifactRefProjection,
        artifact_ref=ref,
        **{
            key: identity[key]
            for key in _TOP_FIELDS
            if key not in {"schema_version", "tissue_setup_result_sha256"}
        },
        tissue_detection=tissue,
        tissue_setup_request=_frozen(request),
        native_observation=_frozen(native),
        native_pyramid_inventory=levels,
        tissue_setup_result_sha256=digest,
    )  # type: ignore[return-value]


def decode_committed_tissue_setup_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueSetupResultProjection:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("tissue setup decoding requires committed authority")
    validate_committed_artifact_authority_set((authority,))
    if (
        authority.producer_node_kind != _NODE_KIND
        or authority.execution_exchange_sha256
        != authority.artifact_ref.owning_identity.get("execution_exchange_sha256")
    ):
        raise BatchArtifactError("tissue setup committed producer or exchange differs")
    passive = validate_tissue_setup_result_artifact_ref(authority.artifact_ref)
    values = {name: getattr(passive, name) for name in passive.__dataclass_fields__}
    return _projection(
        ValidatedTissueSetupResultProjection,
        **values,
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )  # type: ignore[return-value]


__all__ = [
    "TISSUE_SETUP_RESULT_IDENTITY_FIELDS",
    "ValidatedTissueSetupResultArtifactRefProjection",
    "ValidatedTissueSetupResultProjection",
    "tissue_setup_result_identity_contract",
    "build_tissue_setup_result_execution_result",
    "validate_tissue_setup_result_artifact_ref",
    "decode_committed_tissue_setup_result",
]
