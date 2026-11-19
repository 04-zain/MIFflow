"""Locatorless owner contract for one batch tissue-resolution selection.

This module freezes and authenticates already-observed native-pyramid
scientific material.  Acquisition remains outside this scientific owner: the
batch tissue adapter composes the current state/catalogue owner with the
read-only QuPath project-entry pyramid exchange before calling this builder.

The `ValidatedTissueResolutionResultProjection` record carries `artifact_ref`,
`state_head_sha256`, `active_plan_sha256`, `terminal_record_sha256`, `sample_id` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Final

from mif_pipeline.tissue_detection import (
    NativePyramidLevel,
    TissueResolutionSelection,
    native_pyramid_inventory_sha256,
)

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactMultiplicity,
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
from .batch_project import reopen_project_catalogue_entry_artifact
from .batch_tissue_setup_artifacts import decode_committed_tissue_setup_result

_SCHEMA_NAME: Final[str] = "coarse-tissue-resolution-selection"
_SCHEMA_VERSION: Final[int] = 1
_EVIDENCE_SCHEMA_VERSION: Final[str] = "mif-batch-tissue-resolution-selection/v1"
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-tissue-resolution-artifacts/v1"
_ARTIFACT_KIND: Final[str] = "tissue_resolution_result"
_NODE_KIND: Final[str] = "tissue_resolution"
_MAX_NATIVE_PYRAMID_LEVELS: Final[int] = 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

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
        "project_catalogue_entry_ref_sha256",
        "resolved_config_sha256",
        "tissue_setup_result_ref_sha256",
        "tissue_setup_result_sha256",
        "tissue_resolution_selection",
        "tissue_resolution_selection_sha256",
        "native_pyramid_inventory",
        "native_pyramid_inventory_sha256",
        "native_level_read_attestation",
        "tissue_resolution_result_sha256",
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
_NATIVE_LEVEL_FIELDS = frozenset(
    {
        "native_resolution_index",
        "x_downsample",
        "y_downsample",
        "x_um_per_px",
        "y_um_per_px",
    }
)

TISSUE_RESOLUTION_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(
        (_TOP_LEVEL_FIELDS - {"tissue_resolution_selection", "native_pyramid_inventory"})
        | {
            *(f"tissue_resolution_selection.{field}" for field in _SELECTION_FIELDS),
            *(f"native_pyramid_inventory.{field}" for field in _NATIVE_LEVEL_FIELDS),
        }
    )
)


# ==============================================================================
#                       Batch tissue resolution artifacts
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueResolutionResultArtifactRefProjection:
    """Strict scientific projection without a claim of current batch state."""

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
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    selection: TissueResolutionSelection
    native_pyramid_inventory: tuple[NativePyramidLevel, ...]
    native_pyramid_inventory_sha256: str
    native_level_read_attestation: None
    tissue_resolution_result_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedTissueResolutionResultArtifactRefProjection is decoder-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedTissueResolutionResultArtifactRefProjection is nonserializable")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedTissueResolutionResultArtifactRefProjection cannot be subclassed")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTissueResolutionResultProjection:
    """State-authenticated tissue-resolution projection."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
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
    tissue_setup_result_ref_sha256: str
    tissue_setup_result_sha256: str
    selection: TissueResolutionSelection
    native_pyramid_inventory: tuple[NativePyramidLevel, ...]
    native_pyramid_inventory_sha256: str
    native_level_read_attestation: None
    tissue_resolution_result_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedTissueResolutionResultProjection is decoder-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedTissueResolutionResultProjection is nonserializable")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedTissueResolutionResultProjection cannot be subclassed")


def _artifact_ref_projection(
    **values: object,
) -> ValidatedTissueResolutionResultArtifactRefProjection:
    result = object.__new__(ValidatedTissueResolutionResultArtifactRefProjection)
    for name in ValidatedTissueResolutionResultArtifactRefProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _committed_projection(
    **values: object,
) -> ValidatedTissueResolutionResultProjection:
    result = object.__new__(ValidatedTissueResolutionResultProjection)
    for name in ValidatedTissueResolutionResultProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: object) -> str:
    from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes

    try:
        return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("tissue-resolution evidence is not finite canonical JSON") from exc


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed mapping")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} requires exact fields")
    return value


def _levels(value: object) -> tuple[NativePyramidLevel, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError("native pyramid inventory must be a sequence")
    if len(value) > _MAX_NATIVE_PYRAMID_LEVELS:
        raise BatchArtifactError("native pyramid inventory must contain at most 64 levels")
    try:
        return NativePyramidLevel.sequence_from_mappings(value)
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("native pyramid inventory is invalid") from exc


def tissue_resolution_result_identity_contract() -> ArtifactIdentityContract:
    """Return the exact public identity contract for a resolution selection."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-tissue-resolution-result-identity/v1",
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=TISSUE_RESOLUTION_RESULT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _validated_execution(
    context: BatchNodeExecutionContext,
    authority: BatchCurrentExecutionAuthority,
) -> BatchCurrentExecutionAuthority:
    if (
        type(context) is not BatchNodeExecutionContext
        or type(authority) is not BatchCurrentExecutionAuthority
    ):
        raise BatchArtifactError("tissue resolution requires exact current execution authority")
    try:
        authority = validate_current_execution_authority(authority, expected_context=context)
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "tissue-resolution current execution authority is invalid"
        ) from exc
    spec = context.adapter_spec
    contract = tissue_resolution_result_identity_contract()
    requirements = tuple(spec.required_inputs)
    by_kind = {item.artifact_kind: item for item in requirements}
    if (
        authority.node_kind != _NODE_KIND
        or authority.node_scope != "sample"
        or authority.validation_domain_id is None
        or authority.clustering_scope is not None
        or len(authority.participant_ids) != 1
        or len(authority.project_entry_ids) != 1
        or authority.analysis_domain_bindings
        or len(authority.configuration_bindings) != 1
        or spec.node_kind != _NODE_KIND
        or spec.dispatch_mode is not DispatchMode.SINGLE
        or spec.resource_kind != "qupath_image_read"
        or spec.declared_outputs != (_ARTIFACT_KIND,)
        or spec.output_identity_contracts != (contract,)
        or len(requirements) != 3
        or set(by_kind) != {"analysis_frame", "project_catalogue_entry", "tissue_setup_result"}
        or any(item.multiplicity is not ArtifactMultiplicity.SINGLE for item in requirements)
        or spec.optional_inputs
        or context.current_image_identity_sha256 is not None
        or context.script_sha256 is not None
        or context.project_identity_sha256 is not None
        or context.project_authority_head_sha256 is not None
        or context.project_qpproj_snapshot_sha256 is not None
        or context.project_entry_projection_sha256 is not None
    ):
        raise BatchArtifactError("tissue-resolution current execution scope is invalid")
    return authority


def _validated_sources(
    *,
    context: BatchNodeExecutionContext,
    execution: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    project_catalogue_entry_authority: BatchCommittedArtifactAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
) -> tuple[BatchArtifactRef, object, BatchArtifactRef, object]:
    if (
        type(analysis_frame_authority) is not BatchCommittedArtifactAuthority
        or type(project_catalogue_entry_authority) is not BatchCommittedArtifactAuthority
        or type(tissue_setup_authority) is not BatchCommittedArtifactAuthority
        or not all(type(item) is BatchArtifactRef for item in context.inputs)
    ):
        raise BatchArtifactError("tissue-resolution sources require exact committed authorities")
    try:
        sources = validate_committed_artifact_authority_set(
            (analysis_frame_authority, project_catalogue_entry_authority, tissue_setup_authority),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "tissue-resolution committed source authority set is invalid"
        ) from exc
    source_by_kind = {item.artifact_ref.artifact_kind: item for item in sources}
    context_by_kind = {item.artifact_kind: item for item in context.inputs}
    source_digests = tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in sources))
    if (
        len(source_by_kind) != 3
        or set(source_by_kind)
        != {"analysis_frame", "project_catalogue_entry", "tissue_setup_result"}
        or len(context_by_kind) != 3
        or set(context_by_kind) != set(source_by_kind)
        or any(item.state_head_sha256 != execution.state_head_sha256 for item in sources)
        or source_digests != execution.input_artifact_ref_sha256s
        or source_digests != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        or any(
            source_by_kind[kind].artifact_ref != context_by_kind[kind] for kind in context_by_kind
        )
        or analysis_frame_authority.producer_node_kind != "analysis_preflight"
        or project_catalogue_entry_authority.producer_node_kind != "project_upsert"
        or analysis_frame_authority.execution_exchange_sha256 is not None
        or project_catalogue_entry_authority.execution_exchange_sha256 is not None
    ):
        raise BatchArtifactError("tissue-resolution sources differ from current inputs")
    frame_ref = analysis_frame_authority.artifact_ref
    project_ref = project_catalogue_entry_authority.artifact_ref
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        _catalogue, entry = reopen_project_catalogue_entry_artifact(project_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-resolution committed upstream decoding failed") from exc
    return frame_ref, frame, project_ref, entry


def build_tissue_resolution_result_artifact_ref(
    context: BatchNodeExecutionContext,
    *,
    execution_authority: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    project_catalogue_entry_authority: BatchCommittedArtifactAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
    selection: TissueResolutionSelection,
    native_pyramid_inventory: Sequence[object],
) -> BatchArtifactRef:
    """Freeze owner-supplied selection material; does not acquire it from QuPath."""

    execution = _validated_execution(context, execution_authority)
    frame_ref, frame, project_ref, entry = _validated_sources(
        context=context,
        execution=execution,
        analysis_frame_authority=analysis_frame_authority,
        project_catalogue_entry_authority=project_catalogue_entry_authority,
        tissue_setup_authority=tissue_setup_authority,
    )
    setup = decode_committed_tissue_setup_result(tissue_setup_authority)
    if type(selection) is not TissueResolutionSelection:
        raise BatchArtifactError("tissue resolution requires an exact typed selection")
    levels = _levels(native_pyramid_inventory)
    inventory = tuple(item.to_mapping() for item in levels)
    try:
        selection.validate_inventory_binding(inventory)
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "tissue resolution selection differs from the native inventory"
        ) from exc
    base_level = levels[0]
    frame_evidence = frame.spatial_frame
    if abs(base_level.x_um_per_px - frame_evidence.x_um_per_px) > max(
        1e-9, frame_evidence.x_um_per_px * 1e-9
    ) or abs(base_level.y_um_per_px - frame_evidence.y_um_per_px) > max(
        1e-9, frame_evidence.y_um_per_px * 1e-9
    ):
        raise BatchArtifactError(
            "tissue-resolution native inventory calibration differs from the canonical frame"
        )
    frame_scope = frame_ref.scope
    project_scope = project_ref.scope
    if (
        frame_scope.validation_domain_id is None
        or frame_scope.sample_id is None
        or frame_scope.slide_id is None
        or frame_scope.project_entry_id is not None
        or frame_scope.analysis_domain_bindings
        or project_scope.validation_domain_id != frame_scope.validation_domain_id
        or project_scope.sample_id != frame_scope.sample_id
        or project_scope.slide_id != frame_scope.slide_id
        or project_scope.project_entry_id != entry.project_entry_id
        or project_scope.analysis_domain_bindings
        or entry.image_role != "primary_mif"
        or entry.validation_domain_id != frame_scope.validation_domain_id
        or entry.sample_id != frame_scope.sample_id
        or entry.slide_id != frame_scope.slide_id
        or entry.image_sha256 != frame_evidence.canonical_image_sha256
        or frame_ref.configuration_bindings != project_ref.configuration_bindings
        or frame_ref.configuration_bindings != execution.configuration_bindings
        or len(frame_ref.configuration_bindings) != 1
        or frame_ref.configuration_bindings[0].sample_id != frame_scope.sample_id
        or execution.validation_domain_id != frame_scope.validation_domain_id
        or execution.participant_ids != (frame_scope.sample_id,)
        or execution.project_entry_ids != (entry.project_entry_id,)
    ):
        raise BatchArtifactError(
            "tissue-resolution frame, project, config, or dispatch identity differs"
        )
    project_identity = project_ref.owning_identity
    saved_state = project_identity.get("saved_state_attestation_sha256")
    if saved_state is None:
        raise BatchArtifactError("tissue resolution requires primary project saved-state authority")
    resolved_config_sha256 = frame_ref.configuration_bindings[0].resolved_config_sha256
    if (
        setup.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or setup.project_catalogue_entry_ref_sha256 != project_ref.artifact_ref_sha256
        or setup.resolved_config_sha256 != resolved_config_sha256
        or setup.artifact_ref.configuration_bindings != frame_ref.configuration_bindings
        or setup.sample_id != frame_scope.sample_id
        or setup.slide_id != frame_scope.slide_id
        or setup.validation_domain_id != frame_scope.validation_domain_id
        or setup.project_entry_id != entry.project_entry_id
        or setup.canonical_image_id != frame_evidence.canonical_image_id
        or setup.canonical_image_sha256 != frame_evidence.canonical_image_sha256
        or setup.project_identity_sha256 != project_identity.get("project_identity_sha256")
        or setup.project_entry_projection_sha256 != project_identity.get("entry_projection_sha256")
        or setup.native_pyramid_inventory != levels
        or selection.native_resolution_index
        not in setup.native_observation["compatible_native_resolution_indices"]
    ):
        raise BatchArtifactError(
            "tissue resolution differs from accepted setup scope, inventory, or compatibility"
        )
    unsigned = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "sample_id": frame_scope.sample_id,
        "slide_id": frame_scope.slide_id,
        "validation_domain_id": frame_scope.validation_domain_id,
        "project_entry_id": entry.project_entry_id,
        "canonical_image_id": frame_evidence.canonical_image_id,
        "canonical_image_sha256": frame_evidence.canonical_image_sha256,
        "project_identity_sha256": _sha256(
            project_identity.get("project_identity_sha256"),
            "project_identity_sha256",
        ),
        "project_entry_projection_sha256": _sha256(
            project_identity.get("entry_projection_sha256"),
            "project_entry_projection_sha256",
        ),
        "project_saved_state_attestation_sha256": _sha256(
            setup.project_saved_state_attestation_sha256, "project_saved_state_attestation_sha256"
        ),
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "project_catalogue_entry_ref_sha256": project_ref.artifact_ref_sha256,
        "resolved_config_sha256": resolved_config_sha256,
        "tissue_setup_result_ref_sha256": setup.artifact_ref.artifact_ref_sha256,
        "tissue_setup_result_sha256": setup.tissue_setup_result_sha256,
        "tissue_resolution_selection": selection.to_mapping(),
        "tissue_resolution_selection_sha256": selection.selection_record_sha256,
        "native_pyramid_inventory": list(inventory),
        "native_pyramid_inventory_sha256": (native_pyramid_inventory_sha256(inventory)),
        # The bounded native read belongs to tissue candidate execution.  A
        # resolution selection must never fabricate that later attestation.
        "native_level_read_attestation": None,
    }
    identity = {
        **unsigned,
        "tissue_resolution_result_sha256": _digest(unsigned),
    }
    contract = tissue_resolution_result_identity_contract()
    ref = BatchArtifactRef(
        artifact_id=("tissue-resolution-" + identity["tissue_resolution_result_sha256"][:48]),
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            frame_scope.validation_domain_id,
            frame_scope.sample_id,
            frame_scope.slide_id,
            entry.project_entry_id,
            (),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=frame_ref.configuration_bindings,
    )
    # Exercise the public decoder before the owner returns its projection.
    validate_tissue_resolution_result_artifact_ref(ref)
    return ref


def validate_tissue_resolution_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedTissueResolutionResultArtifactRefProjection:
    """Validate owner evidence without claiming current state membership.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if type(artifact_ref) is not BatchArtifactRef:
        raise BatchArtifactError("tissue-resolution artifact must be an exact BatchArtifactRef")
    ref = validate_artifact_ref(artifact_ref)
    contract = tissue_resolution_result_identity_contract()
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
            "tissue-resolution artifact lacks its locatorless public owner contract"
        )
    identity = _closed_mapping(ref.owning_identity, _TOP_LEVEL_FIELDS, "tissue-resolution identity")
    if identity["schema_version"] != _EVIDENCE_SCHEMA_VERSION:
        raise BatchArtifactError("tissue-resolution schema_version is invalid")
    try:
        selection_mapping = _closed_mapping(
            identity["tissue_resolution_selection"],
            _SELECTION_FIELDS,
            "tissue_resolution_selection",
        )
        selection = TissueResolutionSelection.from_mapping(selection_mapping)
        levels = _levels(identity["native_pyramid_inventory"])
        inventory = tuple(item.to_mapping() for item in levels)
        selection.validate_inventory_binding(inventory)
    except BatchArtifactError:
        raise
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-resolution selection or inventory is invalid") from exc
    inventory_sha256 = _sha256(
        identity["native_pyramid_inventory_sha256"],
        "native_pyramid_inventory_sha256",
    )
    if (
        inventory_sha256 != native_pyramid_inventory_sha256(inventory)
        or inventory_sha256 != selection.native_pyramid_inventory_sha256
        or _sha256(
            identity["tissue_resolution_selection_sha256"],
            "tissue_resolution_selection_sha256",
        )
        != selection.selection_record_sha256
        or identity["native_level_read_attestation"] is not None
    ):
        raise BatchArtifactError(
            "tissue-resolution inventory, selection, or unread attestation differs"
        )
    unsigned = {key: identity[key] for key in identity if key != "tissue_resolution_result_sha256"}
    result_sha256 = _sha256(
        identity["tissue_resolution_result_sha256"],
        "tissue_resolution_result_sha256",
    )
    if result_sha256 != _digest(unsigned):
        raise BatchArtifactError("tissue-resolution result digest differs from its evidence")
    values = {
        "sample_id": _text(identity["sample_id"], "sample_id"),
        "slide_id": _text(identity["slide_id"], "slide_id"),
        "validation_domain_id": _text(identity["validation_domain_id"], "validation_domain_id"),
        "project_entry_id": _text(identity["project_entry_id"], "project_entry_id"),
        "canonical_image_id": _text(identity["canonical_image_id"], "canonical_image_id"),
        "canonical_image_sha256": _sha256(
            identity["canonical_image_sha256"], "canonical_image_sha256"
        ),
        "project_identity_sha256": _sha256(
            identity["project_identity_sha256"], "project_identity_sha256"
        ),
        "project_entry_projection_sha256": _sha256(
            identity["project_entry_projection_sha256"],
            "project_entry_projection_sha256",
        ),
        "project_saved_state_attestation_sha256": _sha256(
            identity["project_saved_state_attestation_sha256"],
            "project_saved_state_attestation_sha256",
        ),
        "analysis_frame_ref_sha256": _sha256(
            identity["analysis_frame_ref_sha256"],
            "analysis_frame_ref_sha256",
        ),
        "project_catalogue_entry_ref_sha256": _sha256(
            identity["project_catalogue_entry_ref_sha256"],
            "project_catalogue_entry_ref_sha256",
        ),
        "resolved_config_sha256": _sha256(
            identity["resolved_config_sha256"], "resolved_config_sha256"
        ),
        "tissue_setup_result_ref_sha256": _sha256(
            identity["tissue_setup_result_ref_sha256"], "tissue_setup_result_ref_sha256"
        ),
        "tissue_setup_result_sha256": _sha256(
            identity["tissue_setup_result_sha256"], "tissue_setup_result_sha256"
        ),
        "selection": selection,
        "native_pyramid_inventory": levels,
        "native_pyramid_inventory_sha256": inventory_sha256,
        "native_level_read_attestation": None,
        "tissue_resolution_result_sha256": result_sha256,
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
        or ref.artifact_id != "tissue-resolution-" + result_sha256[:48]
    ):
        raise BatchArtifactError("tissue-resolution scope, config, or artifact identity differs")
    return _artifact_ref_projection(artifact_ref=ref, **values)


def decode_committed_tissue_resolution_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedTissueResolutionResultProjection:
    """Decode only a resolution artifact authenticated by the current state."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("tissue-resolution decoding requires committed state authority")
    try:
        validate_committed_artifact_authority_set((authority,))
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-resolution committed state authority is invalid") from exc
    if (
        authority.producer_node_kind != _NODE_KIND
        or authority.execution_exchange_sha256 is not None
    ):
        raise BatchArtifactError(
            "tissue-resolution committed authority has a foreign producer or exchange"
        )
    scientific = validate_tissue_resolution_result_artifact_ref(authority.artifact_ref)
    values = {
        name: getattr(scientific, name)
        for name in scientific.__dataclass_fields__
        if name != "artifact_ref"
    }
    return _committed_projection(
        artifact_ref=scientific.artifact_ref,
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
        **values,
    )


__all__ = [
    "TISSUE_RESOLUTION_RESULT_IDENTITY_FIELDS",
    "ValidatedTissueResolutionResultArtifactRefProjection",
    "ValidatedTissueResolutionResultProjection",
    "build_tissue_resolution_result_artifact_ref",
    "decode_committed_tissue_resolution_result",
    "tissue_resolution_result_identity_contract",
    "validate_tissue_resolution_result_artifact_ref",
]
