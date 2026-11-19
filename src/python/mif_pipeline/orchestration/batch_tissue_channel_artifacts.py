"""Immutable authenticated selection of the configured tissue channel.

The `ValidatedTissueChannelResultArtifactRefProjection` record carries `artifact_ref`,
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
import re
from typing import Final

from mif_pipeline.pipeline_core import AnalysisConfig
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.tissue_detection import (
    NativePyramidLevel,
    TissueResolutionSelection,
    native_pyramid_inventory_sha256,
)

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    BatchAnalysisPreflightError,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    DispatchMode,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from .batch_tissue_resolution_artifacts import (
    decode_committed_tissue_resolution_result,
    validate_tissue_resolution_result_artifact_ref,
)
from .batch_tissue_setup_artifacts import decode_committed_tissue_setup_result

_ARTIFACT_KIND: Final[str] = "tissue_channel_result"
_NODE_KIND: Final[str] = "tissue_channel"
_SCHEMA_NAME: Final[str] = "tissue-detection-channel-selection"
_SCHEMA_VERSION: Final[int] = 1
_EVIDENCE_SCHEMA_VERSION: Final[str] = "mif-batch-tissue-channel-selection/v1"
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-tissue-channel-artifacts/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_CHANNEL_FIELDS = frozenset({"channel_id", "name", "component", "fluorophore", "biomarker"})
_NATIVE_LEVEL_FIELDS = frozenset(
    {
        "native_resolution_index",
        "x_downsample",
        "y_downsample",
        "x_um_per_px",
        "y_um_per_px",
    }
)
_SELECTION_FIELDS = frozenset(
    {
        "schema_version",
        "selection_source",
        "native_pyramid_inventory_sha256",
        "native_resolution_index",
        "x_downsample",
        "y_downsample",
        "x_um_per_px",
        "y_um_per_px",
        "resolution_source",
        "coordinate_system",
        "level0_opt_in",
        "selection_record_sha256",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
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
        "tissue_resolution_result_ref_sha256",
        "tissue_resolution_result_sha256",
        "resolved_config_sha256",
        "tissue_setup_result_ref_sha256",
        "tissue_setup_result_sha256",
        "tissue_resolution_selection",
        "tissue_resolution_selection_sha256",
        "native_pyramid_inventory",
        "native_pyramid_inventory_sha256",
        "preflight_channel_ids",
        "preflight_channel_inventory_sha256",
        "selected_channel",
        "channel_id",
        "selection_source",
        "tissue_detection_config_sha256",
        "tissue_channel_result_sha256",
    }
)

TISSUE_CHANNEL_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(
        (
            _TOP_LEVEL_FIELDS
            - {
                "tissue_resolution_selection",
                "selected_channel",
                "native_pyramid_inventory",
            }
        )
        | {*(f"tissue_resolution_selection.{field}" for field in _SELECTION_FIELDS)}
        | {*(f"selected_channel.{field}" for field in _CHANNEL_FIELDS)}
        | {*(f"native_pyramid_inventory.{field}" for field in _NATIVE_LEVEL_FIELDS)}
    )
)


# ==============================================================================
#                         Batch tissue channel artifacts
# ==============================================================================


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("tissue-channel evidence is not finite canonical JSON") from exc


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha(value: object, field: str) -> str:
    result = _text(value, field)
    if _SHA256.fullmatch(result) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return result


def _closed(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed mapping")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} requires exact fields")
    return value


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueChannelResultArtifactRefProjection:
    """Strict channel selection projection without a claim of current state."""

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
    tissue_resolution_result_ref_sha256: str
    tissue_resolution_result_sha256: str
    resolved_config_sha256: str
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    selection: object
    native_pyramid_inventory: tuple[NativePyramidLevel, ...]
    native_pyramid_inventory_sha256: str
    preflight_channel_ids: tuple[str, ...]
    preflight_channel_inventory_sha256: str
    selected_channel: Mapping[str, object]
    channel_id: str
    selection_source: str
    tissue_detection_config_sha256: str
    tissue_channel_result_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedTissueChannelResultArtifactRefProjection is decoder-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedTissueChannelResultArtifactRefProjection is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueChannelResultProjection(ValidatedTissueChannelResultArtifactRefProjection):
    """State-authenticated tissue channel selection projection."""

    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedTissueChannelResultProjection is decoder-only")


def _projection(kind: type[object], **values: object) -> object:
    result = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def tissue_channel_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-tissue-channel-result-identity/v1",
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=TISSUE_CHANNEL_RESULT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _validated_inputs(
    context: BatchNodeExecutionContext,
    execution_authority: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    tissue_resolution_authority: BatchCommittedArtifactAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
) -> tuple[object, object, BatchArtifactRef, BatchArtifactRef]:
    if (
        type(context) is not BatchNodeExecutionContext
        or type(execution_authority) is not BatchCurrentExecutionAuthority
        or type(analysis_frame_authority) is not BatchCommittedArtifactAuthority
        or type(tissue_resolution_authority) is not BatchCommittedArtifactAuthority
        or type(tissue_setup_authority) is not BatchCommittedArtifactAuthority
    ):
        raise BatchArtifactError("tissue-channel requires exact current owner authorities")
    try:
        execution = validate_current_execution_authority(
            execution_authority, expected_context=context
        )
        sources = validate_committed_artifact_authority_set(
            (analysis_frame_authority, tissue_resolution_authority, tissue_setup_authority),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-channel current authorities are invalid") from exc
    by_kind = {item.artifact_ref.artifact_kind: item for item in sources}
    by_input = {item.artifact_kind: item for item in context.inputs}
    contract = tissue_channel_result_identity_contract()
    requirements = {item.artifact_kind: item for item in context.adapter_spec.required_inputs}
    if (
        execution.node_kind != _NODE_KIND
        or execution.node_scope != "sample"
        or execution.validation_domain_id is None
        or len(execution.participant_ids) != 1
        or len(execution.project_entry_ids) != 1
        or execution.analysis_domain_bindings
        or len(execution.configuration_bindings) != 1
        or context.adapter_spec.node_kind != _NODE_KIND
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.resource_kind != "scientific_compute"
        or context.adapter_spec.declared_outputs != (_ARTIFACT_KIND,)
        or context.adapter_spec.output_identity_contracts != (contract,)
        or context.adapter_spec.optional_inputs
        or set(requirements)
        != {"analysis_frame", "tissue_resolution_result", "tissue_setup_result"}
        or any(
            item.multiplicity is not ArtifactMultiplicity.SINGLE for item in requirements.values()
        )
        or set(by_kind) != set(requirements)
        or set(by_input) != set(requirements)
        or any(by_kind[kind].artifact_ref != by_input[kind] for kind in by_kind)
        or any(
            by_kind[kind].artifact_ref != context.inputs[index]
            for index, kind in enumerate(sorted(by_kind))
        )
        or any(item.state_head_sha256 != execution.state_head_sha256 for item in sources)
        or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in sources))
        != execution.input_artifact_ref_sha256s
        or analysis_frame_authority.producer_node_kind != "analysis_preflight"
        or tissue_resolution_authority.producer_node_kind != "tissue_resolution"
        or analysis_frame_authority.execution_exchange_sha256 is not None
        or tissue_resolution_authority.execution_exchange_sha256 is not None
        or any(
            item is not None
            for item in (
                context.current_image_identity_sha256,
                context.script_sha256,
                context.project_identity_sha256,
                context.project_authority_head_sha256,
                context.project_qpproj_snapshot_sha256,
                context.project_entry_projection_sha256,
            )
        )
    ):
        raise BatchArtifactError("tissue-channel execution or source identity differs")
    frame_ref = by_kind["analysis_frame"].artifact_ref
    resolution_ref = by_kind["tissue_resolution_result"].artifact_ref
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        resolution = decode_committed_tissue_resolution_result(tissue_resolution_authority)
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-channel upstream owner evidence is invalid") from exc
    if resolution.artifact_ref != resolution_ref:
        raise BatchArtifactError("tissue-channel resolution authority differs from current input")
    return execution, frame, frame_ref, resolution_ref


def build_tissue_channel_result_artifact_ref(
    context: BatchNodeExecutionContext,
    *,
    execution_authority: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    tissue_resolution_authority: BatchCommittedArtifactAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
    config: AnalysisConfig,
) -> BatchArtifactRef:
    """Freeze the configured channel; this stage never reads native pixels."""

    execution, frame, frame_ref, resolution_ref = _validated_inputs(
        context,
        execution_authority,
        analysis_frame_authority,
        tissue_resolution_authority,
        tissue_setup_authority,
    )
    if type(config) is not AnalysisConfig:
        raise BatchArtifactError("tissue-channel requires an exact resolved AnalysisConfig")
    resolution = decode_committed_tissue_resolution_result(tissue_resolution_authority)
    try:
        resolved_channels = resolve_analysis_channels(
            config,
            frame_ref,
            sample_id=resolution.sample_id,
            validation_domain_id=resolution.validation_domain_id,
        )
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError(
            "tissue-channel config, frame, or resolution authority differs"
        ) from exc
    config_digest = _digest(config.to_mapping())
    setup = decode_committed_tissue_setup_result(tissue_setup_authority)
    tissue = setup.tissue_detection
    channel_id = tissue.reference_channel_id
    selection_source = tissue.reference_channel_selection_source
    configured_channel_ids = tuple(item.channel_id for item in resolved_channels)
    if (
        channel_id is None
        or setup.resolved_config_sha256 != config_digest
        or setup.artifact_ref.configuration_bindings != frame_ref.configuration_bindings
        or setup.artifact_ref.artifact_ref_sha256 != resolution.tissue_setup_result_ref_sha256
        or setup.tissue_setup_result_sha256 != resolution.tissue_setup_result_sha256
        or setup.sample_id != resolution.sample_id
        or setup.slide_id != resolution.slide_id
        or setup.validation_domain_id != resolution.validation_domain_id
        or setup.project_entry_id != resolution.project_entry_id
        or setup.canonical_image_id != resolution.canonical_image_id
        or setup.canonical_image_sha256 != resolution.canonical_image_sha256
        or setup.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or setup.project_identity_sha256 != resolution.project_identity_sha256
        or setup.project_entry_projection_sha256 != resolution.project_entry_projection_sha256
        or setup.project_saved_state_attestation_sha256
        != resolution.project_saved_state_attestation_sha256
        or setup.native_pyramid_inventory != resolution.native_pyramid_inventory
        or resolution.selection.native_resolution_index
        not in setup.native_observation["compatible_native_resolution_indices"]
        or selection_source not in {"canonical_metadata", "user_override"}
        or config_digest != resolution.resolved_config_sha256
        or frame_ref.configuration_bindings != resolution.artifact_ref.configuration_bindings
        or frame_ref.configuration_bindings != execution.configuration_bindings
        or len(frame_ref.configuration_bindings) != 1
        or frame_ref.configuration_bindings[0].resolved_config_sha256 != config_digest
        or tuple(frame.ordered_channel_ids) != configured_channel_ids
        or channel_id not in configured_channel_ids
        or resolution.sample_id != frame_ref.scope.sample_id
        or resolution.slide_id != frame_ref.scope.slide_id
        or resolution.validation_domain_id != frame_ref.scope.validation_domain_id
        or resolution.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or resolution.canonical_image_id != frame.spatial_frame.canonical_image_id
        or resolution.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
        or execution.participant_ids != (resolution.sample_id,)
        or execution.project_entry_ids != (resolution.project_entry_id,)
        or execution.validation_domain_id != resolution.validation_domain_id
    ):
        raise BatchArtifactError("tissue-channel config, frame, or resolution authority differs")
    selected = next(item for item in resolved_channels if item.channel_id == channel_id)
    selected_mapping = selected.to_mapping()
    unsigned = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "sample_id": resolution.sample_id,
        "slide_id": resolution.slide_id,
        "validation_domain_id": resolution.validation_domain_id,
        "project_entry_id": resolution.project_entry_id,
        "canonical_image_id": resolution.canonical_image_id,
        "canonical_image_sha256": resolution.canonical_image_sha256,
        "project_identity_sha256": resolution.project_identity_sha256,
        "project_entry_projection_sha256": (resolution.project_entry_projection_sha256),
        "project_saved_state_attestation_sha256": (
            resolution.project_saved_state_attestation_sha256
        ),
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "tissue_resolution_result_ref_sha256": (resolution_ref.artifact_ref_sha256),
        "tissue_resolution_result_sha256": (resolution.tissue_resolution_result_sha256),
        "resolved_config_sha256": config_digest,
        "tissue_setup_result_ref_sha256": setup.artifact_ref.artifact_ref_sha256,
        "tissue_setup_result_sha256": setup.tissue_setup_result_sha256,
        "tissue_resolution_selection": resolution.selection.to_mapping(),
        "tissue_resolution_selection_sha256": (resolution.selection.selection_record_sha256),
        "native_pyramid_inventory": [
            item.to_mapping() for item in resolution.native_pyramid_inventory
        ],
        "native_pyramid_inventory_sha256": resolution.native_pyramid_inventory_sha256,
        "preflight_channel_ids": list(frame.ordered_channel_ids),
        "preflight_channel_inventory_sha256": frame.channel_inventory_sha256,
        "selected_channel": selected_mapping,
        "channel_id": channel_id,
        "selection_source": selection_source,
        "tissue_detection_config_sha256": _digest(tissue.to_mapping()),
    }
    identity = {**unsigned, "tissue_channel_result_sha256": _digest(unsigned)}
    contract = tissue_channel_result_identity_contract()
    ref = BatchArtifactRef(
        artifact_id=("tissue-channel-" + identity["tissue_channel_result_sha256"][:48]),
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
            resolution.validation_domain_id,
            resolution.sample_id,
            resolution.slide_id,
            resolution.project_entry_id,
            (),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=frame_ref.configuration_bindings,
    )
    validate_tissue_channel_result_artifact_ref(ref)
    return ref


def validate_tissue_channel_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedTissueChannelResultArtifactRefProjection:
    """Validate tissue channel result artifact ref for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if type(artifact_ref) is not BatchArtifactRef:
        raise BatchArtifactError("tissue-channel artifact must be an exact BatchArtifactRef")
    ref = validate_artifact_ref(artifact_ref)
    contract = tissue_channel_result_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.producer_version != _PRODUCER_VERSION
        or dict(ref.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
    ):
        raise BatchArtifactError(
            "tissue-channel artifact lacks its locatorless public owner contract"
        )
    identity = _closed(ref.owning_identity, _TOP_LEVEL_FIELDS, "tissue-channel identity")
    if identity["schema_version"] != _EVIDENCE_SCHEMA_VERSION:
        raise BatchArtifactError("tissue-channel schema_version is invalid")
    selection_mapping = _closed(
        identity["tissue_resolution_selection"],
        _SELECTION_FIELDS,
        "tissue_resolution_selection",
    )
    selected_channel = _closed(identity["selected_channel"], _CHANNEL_FIELDS, "selected_channel")
    channel_id = _text(identity["channel_id"], "channel_id")
    source = _text(identity["selection_source"], "selection_source")
    channel_ids_value = identity["preflight_channel_ids"]
    if isinstance(channel_ids_value, (str, bytes)) or not isinstance(
        channel_ids_value, (tuple, list)
    ):
        raise BatchArtifactError("preflight_channel_ids must be an array")
    channel_ids = tuple(_text(item, "preflight channel ID") for item in channel_ids_value)
    if (
        len(set(channel_ids)) != len(channel_ids)
        or not channel_ids
        or channel_id not in channel_ids
    ):
        raise BatchArtifactError("preflight channel inventory is invalid")
    try:
        selection = TissueResolutionSelection.from_mapping(selection_mapping)
        levels_value = identity["native_pyramid_inventory"]
        if isinstance(levels_value, (str, bytes)) or not isinstance(levels_value, (tuple, list)):
            raise BatchArtifactError("native_pyramid_inventory must be an array")
        levels = tuple(NativePyramidLevel.from_mapping(item) for item in levels_value)
        selection.validate_inventory_binding([item.to_mapping() for item in levels])
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-channel resolution selection is invalid") from exc
    if (
        selected_channel["channel_id"] != channel_id
        or source not in {"canonical_metadata", "user_override"}
        or _digest(list(channel_ids))
        != _sha(
            identity["preflight_channel_inventory_sha256"],
            "preflight_channel_inventory_sha256",
        )
        or _sha(
            identity["tissue_resolution_selection_sha256"],
            "tissue_resolution_selection_sha256",
        )
        != selection_mapping["selection_record_sha256"]
        or _sha(
            identity["native_pyramid_inventory_sha256"],
            "native_pyramid_inventory_sha256",
        )
        != native_pyramid_inventory_sha256([item.to_mapping() for item in levels])
        or _sha(
            identity["native_pyramid_inventory_sha256"],
            "native_pyramid_inventory_sha256",
        )
        != selection.native_pyramid_inventory_sha256
    ):
        raise BatchArtifactError("tissue-channel selection authority differs")
    unsigned = {key: identity[key] for key in identity if key != "tissue_channel_result_sha256"}
    result_sha = _sha(
        identity["tissue_channel_result_sha256"],
        "tissue_channel_result_sha256",
    )
    if result_sha != _digest(unsigned):
        raise BatchArtifactError("tissue-channel result digest differs from its evidence")
    values = {
        "sample_id": _text(identity["sample_id"], "sample_id"),
        "slide_id": _text(identity["slide_id"], "slide_id"),
        "validation_domain_id": _text(identity["validation_domain_id"], "validation_domain_id"),
        "project_entry_id": _text(identity["project_entry_id"], "project_entry_id"),
        "canonical_image_id": _text(identity["canonical_image_id"], "canonical_image_id"),
        "canonical_image_sha256": _sha(
            identity["canonical_image_sha256"], "canonical_image_sha256"
        ),
        "project_identity_sha256": _sha(
            identity["project_identity_sha256"], "project_identity_sha256"
        ),
        "project_entry_projection_sha256": _sha(
            identity["project_entry_projection_sha256"],
            "project_entry_projection_sha256",
        ),
        "project_saved_state_attestation_sha256": _sha(
            identity["project_saved_state_attestation_sha256"],
            "project_saved_state_attestation_sha256",
        ),
        "analysis_frame_ref_sha256": _sha(
            identity["analysis_frame_ref_sha256"],
            "analysis_frame_ref_sha256",
        ),
        "tissue_resolution_result_ref_sha256": _sha(
            identity["tissue_resolution_result_ref_sha256"],
            "tissue_resolution_result_ref_sha256",
        ),
        "tissue_resolution_result_sha256": _sha(
            identity["tissue_resolution_result_sha256"],
            "tissue_resolution_result_sha256",
        ),
        "resolved_config_sha256": _sha(
            identity["resolved_config_sha256"], "resolved_config_sha256"
        ),
        "tissue_setup_result_ref_sha256": _sha(
            identity["tissue_setup_result_ref_sha256"], "tissue_setup_result_ref_sha256"
        ),
        "tissue_setup_result_sha256": _sha(
            identity["tissue_setup_result_sha256"], "tissue_setup_result_sha256"
        ),
        "selection": selection,
        "native_pyramid_inventory": levels,
        "native_pyramid_inventory_sha256": _sha(
            identity["native_pyramid_inventory_sha256"],
            "native_pyramid_inventory_sha256",
        ),
        "preflight_channel_ids": channel_ids,
        "preflight_channel_inventory_sha256": _sha(
            identity["preflight_channel_inventory_sha256"],
            "preflight_channel_inventory_sha256",
        ),
        "selected_channel": dict(selected_channel),
        "channel_id": channel_id,
        "selection_source": source,
        "tissue_detection_config_sha256": _sha(
            identity["tissue_detection_config_sha256"],
            "tissue_detection_config_sha256",
        ),
        "tissue_channel_result_sha256": result_sha,
    }
    scope = ref.scope
    if (
        scope.validation_domain_id != values["validation_domain_id"]
        or scope.sample_id != values["sample_id"]
        or scope.slide_id != values["slide_id"]
        or scope.project_entry_id != values["project_entry_id"]
        or scope.analysis_domain_bindings
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != values["sample_id"]
        or ref.configuration_bindings[0].resolved_config_sha256 != values["resolved_config_sha256"]
        or ref.artifact_id != "tissue-channel-" + result_sha[:48]
    ):
        raise BatchArtifactError("tissue-channel scope, config, or artifact identity differs")
    return _projection(
        ValidatedTissueChannelResultArtifactRefProjection,
        artifact_ref=ref,
        **values,
    )  # type: ignore[return-value]


def decode_committed_tissue_channel_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueChannelResultProjection:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("tissue-channel decoding requires committed state authority")
    try:
        validate_committed_artifact_authority_set((authority,))
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-channel committed state authority is invalid") from exc
    if (
        authority.producer_node_kind != _NODE_KIND
        or authority.execution_exchange_sha256 is not None
    ):
        raise BatchArtifactError(
            "tissue-channel committed authority has a foreign producer or exchange"
        )
    scientific = validate_tissue_channel_result_artifact_ref(authority.artifact_ref)
    values = {
        name: getattr(scientific, name)
        for name in scientific.__dataclass_fields__
        if name != "artifact_ref"
    }
    return _projection(
        ValidatedTissueChannelResultProjection,
        artifact_ref=scientific.artifact_ref,
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
        **values,
    )  # type: ignore[return-value]


__all__ = [
    "TISSUE_CHANNEL_RESULT_IDENTITY_FIELDS",
    "ValidatedTissueChannelResultArtifactRefProjection",
    "ValidatedTissueChannelResultProjection",
    "build_tissue_channel_result_artifact_ref",
    "decode_committed_tissue_channel_result",
    "tissue_channel_result_identity_contract",
    "validate_tissue_channel_result_artifact_ref",
]
