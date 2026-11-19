"""Authenticated path-free batch results for direct registration modes.

The `ValidatedBatchRegistrationResultProjection` record carries `artifact_ref`, `mode`,
`sample_id`, `slide_id`, `validation_domain_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlsplit
from urllib.request import url2pathname

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult, StageStatus
from mif_pipeline.registration import (
    RegistrationImportScope,
    RegistrationRecord,
    project_registration_result_identity,
    registration_record_sha256,
    registration_result_identity_spec,
    resolve_registration_review,
)
from mif_pipeline.qupath_runtime.registration_session import CurrentRegistrationProjectSnapshot

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchAttemptAuthorization,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from .batch_manifest import (
    ResolvedBatchSample,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
)
from .batch_project import (
    BatchProjectCatalogue,
    BatchProjectCatalogueStore,
    BatchProjectEntry,
    NoMovingReferenceAuthority,
    PairedMovingReferenceAuthority,
    reopen_project_catalogue_entry_artifact,
)
from .batch_state import BatchStateHead, BatchStateStore, BatchWorkspaceLease
from .batch_tissue_artifacts import (
    ValidatedTissueAcceptanceResultProjection,
    decode_committed_tissue_acceptance_result,
)

_ARTIFACT_KIND: Final[str] = "registration_result"
_NODE_KIND: Final[str] = "registration"
_SCHEMA_NAME: Final[str] = "mif-batch-registration-result"
_SCHEMA_VERSION: Final[int] = 1
_WIRE_VERSION: Final[str] = "mif-batch-registration-result/v1"
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-registration/v1"
_DECISION_VERSION: Final[str] = "mif-batch-registration-decision/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_COMMON_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "sample_id",
        "slide_id",
        "validation_domain_id",
        "primary_project_entry_id",
        "primary_image_sha256",
        "analysis_frame_ref_sha256",
        "tissue_acceptance_result_ref_sha256",
        "resolved_config_sha256",
        "active_plan_sha256",
        "producing_state_head_sha256",
        "project_identity_sha256",
        "project_authority_head_sha256",
        "project_qpproj_snapshot_sha256",
        "project_catalogue_sha256",
        "primary_project_entry_projection_sha256",
        "primary_saved_state_attestation_sha256",
        "tissue_generation_id",
        "tissue_generation_content_sha256",
        "tissue_section_inventory_sha256",
        "tissue_section_ids_sha256",
        "tissue_roi_ids_sha256",
        "tissue_path_object_ids_sha256",
        "retained_moving_reference_descriptor_sha256",
        "saved_state_disposition",
        "registration_decision_sha256",
    }
)
_SIDE_BY_SIDE_FIELDS = frozenset(
    {
        "moving_project_entry_id",
        "moving_project_entry_projection_sha256",
        "moving_image_sha256",
        "moving_reference_modality",
        "moving_saved_state_attestation_sha256",
    }
)
_OWNER_WARPY_FIELDS = frozenset(registration_result_identity_spec().required_fields)
_WARPY_REFERENCE_FIELDS = frozenset(
    {
        "accepted_warpy_record_sha256",
        "accepted_registration_review_sha256",
        "accepted_transfer_review_sha256",
    }
)
_REGISTRATION_ONLY_WARPY_FIELDS = frozenset(
    {
        "accepted_warpy_identity",
        "accepted_warpy_record_sha256",
        "accepted_registration_review_sha256",
    }
)
_DIRECT_MODES = frozenset({"none", "skip", "side_by_side"})
_REGISTRATION_ROUTE_ISSUER = object()

BATCH_REGISTRATION_RESULT_REQUIRED_FIELDS: tuple[str, ...] = tuple(sorted(_COMMON_TOP_LEVEL_FIELDS))
BATCH_REGISTRATION_RESULT_OPTIONAL_FIELDS: tuple[str, ...] = tuple(
    sorted(
        _SIDE_BY_SIDE_FIELDS
        | _WARPY_REFERENCE_FIELDS
        | {"fallback_response_sha256"}
        | {f"accepted_warpy_identity.{field}" for field in _OWNER_WARPY_FIELDS}
    )
)


# ==============================================================================
#                               Batch registration
# ==============================================================================


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("registration evidence is not finite canonical JSON") from exc


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


def _projection(kind: type[object], **values: object) -> object:
    result = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def batch_registration_result_identity_contract() -> ArtifactIdentityContract:
    """Return the mode-neutral outer batch registration owner contract."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-registration-result-identity/v1",
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=BATCH_REGISTRATION_RESULT_REQUIRED_FIELDS,
        optional_fields=BATCH_REGISTRATION_RESULT_OPTIONAL_FIELDS,
        required_runtime_names=("mif_pipeline",),
    )


@dataclass(frozen=True, slots=True, init=False)
class ValidatedBatchRegistrationResultProjection:
    """Strict path-free registration decision projection."""

    artifact_ref: BatchArtifactRef
    mode: Literal["none", "skip", "side_by_side", "warpy"]
    sample_id: str
    slide_id: str
    validation_domain_id: str
    primary_project_entry_id: str
    primary_image_sha256: str
    project_identity_sha256: str
    tissue_generation_id: str
    tissue_path_object_ids_sha256: str
    moving_project_entry_id: str | None
    moving_image_sha256: str | None
    moving_reference_modality: str | None
    accepted_warpy_identity: Mapping[str, str] | None

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedBatchRegistrationResultProjection is decoder-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedBatchRegistrationResultProjection is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class RegistrationExecutionRoute:
    """Issuer-only result of the earliest direct-versus-Warpy routing gate."""

    disposition: Literal["direct_r1", "warpy_r2"]
    context: BatchNodeExecutionContext
    current_execution: BatchCurrentExecutionAuthority | None
    state_head: BatchStateHead | None
    catalogue: BatchProjectCatalogue | None
    sample: ResolvedBatchSample | None
    primary_entry: BatchProjectEntry | None
    moving_entry: BatchProjectEntry | None
    moving_authority: PairedMovingReferenceAuthority | None
    frame_ref: BatchArtifactRef | None
    tissue_ref: BatchArtifactRef | None
    tissue_result: object | None
    current_saved_state_sha256_by_id: Mapping[str, str] | None
    stage_result: StageResult | None
    execution_result: BatchNodeExecutionResult | None
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("registration execution routes are state-issued")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("registration execution routes are nonserializable")

    @classmethod
    def _issue(cls, **values: object) -> "RegistrationExecutionRoute":
        route = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(
                route,
                name,
                _REGISTRATION_ROUTE_ISSUER if name == "_issuer" else values[name],
            )
        return route


def authenticate_registration_execution_route(
    route: RegistrationExecutionRoute,
) -> RegistrationExecutionRoute:
    """Authenticate an issuer-only earliest-routing result."""

    if (
        type(route) is not RegistrationExecutionRoute
        or route._issuer is not _REGISTRATION_ROUTE_ISSUER
        or route.disposition not in {"direct_r1", "warpy_r2"}
        or type(route.context) is not BatchNodeExecutionContext
    ):
        raise BatchArtifactError("registration execution route is foreign")
    if route.disposition == "direct_r1":
        if (
            route.stage_result is None
            or route.execution_result is None
            or any(
                value is not None
                for value in (
                    route.current_execution,
                    route.state_head,
                    route.catalogue,
                    route.sample,
                    route.primary_entry,
                    route.moving_entry,
                    route.moving_authority,
                    route.frame_ref,
                    route.tissue_ref,
                    route.tissue_result,
                    route.current_saved_state_sha256_by_id,
                )
            )
        ):
            raise BatchArtifactError("direct registration route shape is invalid")
    elif (
        route.stage_result is not None
        or route.execution_result is not None
        or type(route.current_execution) is not BatchCurrentExecutionAuthority
        or type(route.state_head) is not BatchStateHead
        or type(route.catalogue) is not BatchProjectCatalogue
        or type(route.sample) is not ResolvedBatchSample
        or type(route.primary_entry) is not BatchProjectEntry
        or type(route.moving_authority) is not PairedMovingReferenceAuthority
        or type(route.moving_entry) is not BatchProjectEntry
        or type(route.frame_ref) is not BatchArtifactRef
        or type(route.tissue_ref) is not BatchArtifactRef
        or route.primary_entry.image_role != "primary_mif"
        or route.moving_entry.image_role != "moving_reference"
        or not isinstance(route.current_saved_state_sha256_by_id, Mapping)
    ):
        raise BatchArtifactError("Warpy registration route shape is invalid")
    return route


def _decision_material(identity: Mapping[str, object]) -> dict[str, object]:
    mode = identity["mode"]
    material = {
        "schema_version": _DECISION_VERSION,
        "mode": mode,
        "moving_reference_descriptor_sha256": identity[
            "retained_moving_reference_descriptor_sha256"
        ],
        "side_by_side": (
            {field: identity[field] for field in sorted(_SIDE_BY_SIDE_FIELDS)}
            if mode == "side_by_side"
            else None
        ),
        "fallback_evidence_sha256": identity.get("fallback_response_sha256"),
        "accepted_warpy_identity": None,
    }
    if mode == "warpy":
        material.update(
            {
                "moving_reference": {
                    field: identity[field] for field in sorted(_SIDE_BY_SIDE_FIELDS)
                },
                **{field: identity[field] for field in _REGISTRATION_ONLY_WARPY_FIELDS},
            }
        )
    return material


def validate_registration_result_artifact_ref(
    ref: BatchArtifactRef,
) -> ValidatedBatchRegistrationResultProjection:
    """Validate one outer result without claiming current state membership.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(ref) is not BatchArtifactRef:
        raise BatchArtifactError("registration result must be an exact BatchArtifactRef")
    validated = validate_artifact_ref(ref)
    contract = batch_registration_result_identity_contract()
    if (
        validated.artifact_kind != _ARTIFACT_KIND
        or (validated.schema_name, validated.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or validated.identity_contract != contract
        or validated.owning_identity_contract_sha256 != contract.contract_sha256
        or validated.origin is not ArtifactOrigin.INTERNAL_NODE
        or validated.producer_version != _PRODUCER_VERSION
        or dict(validated.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
        or validated.locator is not None
        or validated.sha256 is not None
        or validated.byte_count is not None
    ):
        raise BatchArtifactError("registration result lacks its locatorless public owner contract")
    identity_value = validated.owning_identity
    if not isinstance(identity_value, Mapping):
        raise BatchArtifactError("registration identity must be a mapping")
    mode = _text(identity_value.get("mode"), "registration mode")
    if mode not in _DIRECT_MODES | {"warpy"}:
        raise BatchArtifactError("registration result mode is unsupported")
    expected_fields = (
        _COMMON_TOP_LEVEL_FIELDS
        | (_SIDE_BY_SIDE_FIELDS if mode in {"side_by_side", "warpy"} else frozenset())
        | (_REGISTRATION_ONLY_WARPY_FIELDS if mode == "warpy" else frozenset())
        | (
            {"fallback_response_sha256"}
            if mode in {"skip", "side_by_side"} and "fallback_response_sha256" in identity_value
            else frozenset()
        )
    )
    identity = _closed(identity_value, expected_fields, "registration identity")
    if "fallback_response_sha256" in identity:
        _sha(identity["fallback_response_sha256"], "fallback_response_sha256")
    if identity["schema_version"] != _WIRE_VERSION:
        raise BatchArtifactError("registration result schema_version is invalid")
    for field in (
        "sample_id",
        "slide_id",
        "validation_domain_id",
        "primary_project_entry_id",
        "tissue_generation_id",
    ):
        _text(identity[field], field)
    if identity["saved_state_disposition"] != "authenticated_unchanged":
        raise BatchArtifactError("registration result must attest unchanged saved state")
    sha_fields = _COMMON_TOP_LEVEL_FIELDS - {
        "schema_version",
        "mode",
        "sample_id",
        "slide_id",
        "validation_domain_id",
        "primary_project_entry_id",
        "tissue_generation_id",
        "saved_state_disposition",
    }
    for field in sha_fields:
        _sha(identity[field], field)
    moving_entry_id: str | None = None
    if mode in {"side_by_side", "warpy"}:
        moving_entry_id = _text(identity["moving_project_entry_id"], "moving_project_entry_id")
        _text(identity["moving_reference_modality"], "moving_reference_modality")
        for field in _SIDE_BY_SIDE_FIELDS - {
            "moving_project_entry_id",
            "moving_reference_modality",
        }:
            _sha(identity[field], field)
    accepted_warpy_identity = None
    if mode == "warpy":
        accepted_warpy_identity = _closed(
            identity["accepted_warpy_identity"],
            _OWNER_WARPY_FIELDS,
            "accepted Warpy identity",
        )
        for field in _OWNER_WARPY_FIELDS - {
            "fixed_project_entry_id",
            "moving_project_entry_id",
            "direction",
        }:
            _sha(accepted_warpy_identity[field], field)
        for field in _REGISTRATION_ONLY_WARPY_FIELDS - {"accepted_warpy_identity"}:
            _sha(identity[field], field)
        if (
            accepted_warpy_identity["direction"] != "moving_to_fixed"
            or accepted_warpy_identity["fixed_project_entry_id"]
            != identity["primary_project_entry_id"]
            or accepted_warpy_identity["moving_project_entry_id"] != moving_entry_id
            or accepted_warpy_identity["fixed_image_sha256"] != identity["primary_image_sha256"]
            or accepted_warpy_identity["moving_image_sha256"] != identity["moving_image_sha256"]
            or moving_entry_id == identity["primary_project_entry_id"]
            or identity["moving_image_sha256"] == identity["primary_image_sha256"]
        ):
            raise BatchArtifactError("accepted Warpy scope differs from its primary/reference pair")
    if identity["registration_decision_sha256"] != _digest(_decision_material(identity)):
        raise BatchArtifactError(
            "registration decision digest differs from its exact mode evidence"
        )
    scope = validated.scope
    resolved_config_sha256 = str(identity["resolved_config_sha256"])
    if (
        scope.validation_domain_id != identity["validation_domain_id"]
        or scope.sample_id != identity["sample_id"]
        or scope.slide_id != identity["slide_id"]
        or scope.project_entry_id != identity["primary_project_entry_id"]
        or scope.analysis_domain_bindings
        or validated.configuration_bindings
        != (ArtifactConfigurationBinding(str(identity["sample_id"]), resolved_config_sha256),)
        or validated.artifact_id
        != "registration-"
        + _digest(
            {
                "decision": identity["registration_decision_sha256"],
                "producing_state": identity["producing_state_head_sha256"],
                "attempt_authorization": (validated.producer_attempt_authorization_sha256),
            }
        )[:48]
    ):
        raise BatchArtifactError(
            "registration result scope, configuration, or artifact identity differs"
        )
    return _projection(
        ValidatedBatchRegistrationResultProjection,
        artifact_ref=validated,
        mode=mode,
        sample_id=str(identity["sample_id"]),
        slide_id=str(identity["slide_id"]),
        validation_domain_id=str(identity["validation_domain_id"]),
        primary_project_entry_id=str(identity["primary_project_entry_id"]),
        primary_image_sha256=str(identity["primary_image_sha256"]),
        project_identity_sha256=str(identity["project_identity_sha256"]),
        tissue_generation_id=str(identity["tissue_generation_id"]),
        tissue_path_object_ids_sha256=str(identity["tissue_path_object_ids_sha256"]),
        moving_project_entry_id=moving_entry_id,
        moving_image_sha256=(
            str(identity["moving_image_sha256"]) if mode in {"side_by_side", "warpy"} else None
        ),
        moving_reference_modality=(
            str(identity["moving_reference_modality"])
            if mode in {"side_by_side", "warpy"}
            else None
        ),
        accepted_warpy_identity=accepted_warpy_identity,
    )  # type: ignore[return-value]


def decode_committed_registration_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedBatchRegistrationResultProjection:
    """Decode registration only from a state-owner-issued completion."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("registration decoding requires committed state authority")
    try:
        validate_committed_artifact_authority_set((authority,))
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("registration committed state authority is invalid") from exc
    result = validate_registration_result_artifact_ref(authority.artifact_ref)
    if (
        authority.producer_node_kind != _NODE_KIND
        or authority.execution_exchange_sha256 is not None
    ):
        raise BatchArtifactError(
            "registration committed authority has a foreign producer or exchange"
        )
    return result


def reopen_accepted_warpy_record(
    state_store: BatchStateStore,
    authorization: BatchAttemptAuthorization,
) -> RegistrationRecord:
    """Resolve the exact saved pending record through its authenticated review.

    This is a deterministic projection of immutable review inputs, not another
    QC run or a claim that the registration terminal has been committed.
    """

    if (
        type(state_store) is not BatchStateStore
        or type(authorization) is not BatchAttemptAuthorization
    ):
        raise BatchArtifactError("registration review requires exact state and attempt owners")
    response = state_store.load_validated_interaction_response(authorization)
    predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
    validated = response.get("response") if isinstance(response, Mapping) else None
    review = validated.get("response") if isinstance(validated, Mapping) else None
    if (
        predecessor_sha256 is None
        or authorization.interaction_response_sha256 is None
        or not isinstance(response, Mapping)
        or response.get("action") != "accept_registration"
        or not isinstance(validated, Mapping)
        or validated.get("action") != "accept_registration"
        or not isinstance(review, Mapping)
        or set(review) != {"reviewed_by"}
    ):
        raise BatchArtifactError("registration acceptance lacks its authenticated review response")
    predecessor = state_store.load_attempt(predecessor_sha256)
    result = predecessor.stage_result
    context = result.resume_context
    if isinstance(context, Mapping):
        context = context.get("service_context", context)
    if (
        predecessor.node_id != authorization.node_id
        or result.stage != "registration"
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.interaction_reason is not InteractionReason.REGISTRATION_REVIEW_REQUIRED
        or not isinstance(context, Mapping)
        or context.get("schema_version") != "registration-review-interaction/v1"
    ):
        raise BatchArtifactError("registration acceptance has a foreign review predecessor")
    try:
        pending = RegistrationRecord.from_mapping(context.get("pending_registration"))
        if registration_record_sha256(pending) != context.get("pending_registration_sha256"):
            raise BatchArtifactError("pending registration digest differs from the saved review")
        return resolve_registration_review(
            pending,
            review_state="accepted",
            reviewed_by=review["reviewed_by"],
        )
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("saved registration review is invalid") from exc


def _head_projection(head: BatchStateHead) -> tuple[object, ...]:
    if type(head) is not BatchStateHead:
        raise BatchArtifactError("registration requires an exact state head")
    fields = (
        "batch_id",
        "sha256",
        "active_plan_sha256",
        "original_manifest_sha256",
        "normalized_manifest_sha256",
        "project_path",
        "project_identity_sha256",
        "project_authority_head_sha256",
        "project_materialization_state",
        "project_qpproj_snapshot_sha256",
        "project_catalogue_sha256",
        "source_sha256_by_sample",
        "resolved_config_sha256_by_sample",
        "sample_project_entry_id_by_sample",
        "sample_project_entry_projection_sha256_by_sample",
    )
    try:
        return tuple(
            tuple(sorted(value.items())) if isinstance(value, Mapping) else value
            for value in (getattr(head, field) for field in fields)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise BatchArtifactError("registration state head is incomplete") from exc


def _current_catalogue(store: BatchProjectCatalogueStore) -> BatchProjectCatalogue:
    """Require the current catalogue admitted by its durable, lease-guarded store.

    The store checks current/generation bytes and constructs immutable entries.
    Registration requires that catalogue to exist; its callers compare the
    current project and registration identities without serializing it again.
    """
    value = store.load_current()
    if type(value) is not BatchProjectCatalogue:
        raise BatchArtifactError("registration requires one exact current project catalogue")
    return value


def _manifest_sample(
    manifest: ValidatedBatchManifest,
    execution: BatchCurrentExecutionAuthority,
    head: BatchStateHead,
) -> ResolvedBatchSample:
    if type(manifest) is not ValidatedBatchManifest:
        raise BatchArtifactError("registration requires an exact retained validated manifest")
    try:
        reopened = revalidate_retained_manifest(
            manifest.original_manifest_bytes, manifest.normalized_manifest_bytes
        )
    except (TypeError, ValueError) as exc:
        raise BatchArtifactError("registration retained manifest cannot be revalidated") from exc
    if (
        reopened.manifest != manifest.manifest
        or reopened.original_manifest_bytes != manifest.original_manifest_bytes
        or reopened.original_manifest_sha256 != manifest.original_manifest_sha256
        or reopened.normalized_manifest_bytes != manifest.normalized_manifest_bytes
        or reopened.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
        or reopened.samples != manifest.samples
        or head.original_manifest_sha256 != reopened.original_manifest_sha256
        or head.normalized_manifest_sha256 != reopened.normalized_manifest_sha256
        or len(execution.participant_ids) != 1
    ):
        raise BatchArtifactError(
            "registration manifest or state binding differs from retained bytes"
        )
    matches = tuple(
        item for item in reopened.enabled_samples if item.sample_id == execution.participant_ids[0]
    )
    if len(matches) != 1:
        raise BatchArtifactError(
            "registration dispatch participant is not one enabled manifest sample"
        )
    return matches[0]


def _moving_authority(
    sample: ResolvedBatchSample,
    moving: BatchProjectEntry | None,
):
    reference = sample.reference
    if reference is None:
        return NoMovingReferenceAuthority.create()
    if moving is None:
        raise BatchArtifactError(
            "registration retained moving reference is absent from the catalogue"
        )
    path = reference.source_path.resolve(strict=False)
    return PairedMovingReferenceAuthority.create(
        reference_id=reference.reference_id,
        source_path=path,
        canonical_uri=path.as_uri(),
        source_sha256=moving.image_sha256,
        modality=reference.modality,
        source_image_id=reference.source_image_id,
        source_metadata=reference.source_metadata,
    )


def _tissue_primary_matches(tissue_identity, primary, catalogue, catalogue_store):
    """Match the tissue's original image entry across later pair associations.

    Pair updates alter the catalogue projection, while the accepted tissue still
    refers to the same primary image. Find that exact projection in authenticated
    catalogue history and compare the image identity; hierarchy changes are
    checked separately through current saved-state evidence.
    """
    projection = tissue_identity["project_entry_projection_sha256"]
    seen = set()
    while True:
        if catalogue.catalogue_sha256 in seen:
            raise BatchArtifactError("registration catalogue history is cyclic")
        seen.add(catalogue.catalogue_sha256)
        if catalogue.project_identity_sha256 != tissue_identity["project_identity_sha256"]:
            return False
        try:
            original = catalogue.resolve(primary.project_entry_id)
        except KeyError:
            return False
        if original.entry_projection_sha256 == projection:
            identity_fields = ("project_entry_id", "sample_id", "slide_id",
                               "validation_domain_id", "image_role", "reference_modality",
                               "canonical_uri", "image_sha256")
            return all(getattr(original, key) == getattr(primary, key) for key in identity_fields)
        if catalogue.predecessor_catalogue_sha256 is None:
            return False
        catalogue = catalogue_store.load_generation(catalogue.predecessor_catalogue_sha256)


def _selected_moving_authority(context, execution, sample, moving, state_store):
    """Resolve a saved pair selection through its accepted preparation result.

    The manifest remains the authority for the original configuration. A revised
    pair is a separate plan decision, backed by its completed catalogue update.
    Reopening alignment reuses that result; it does not prepare the image again.
    """
    from .batch_settled_review_revision import SettledReviewRevision

    plan = state_store._load_plan_snapshot(execution.active_plan_sha256)
    node = plan.node_by_id[context.node_id]
    revision = node.get("settled_review_revision")
    selected = (None if revision is None else
                SettledReviewRevision.from_mapping(revision).paired_reference)
    prepared = tuple(ref for ref in context.inputs if ref.artifact_kind == "project_catalogue_entry")
    if selected is None:
        if prepared:
            raise BatchArtifactError("registration has preparation without a selected pair")
        return _moving_authority(sample, moving)
    if len(prepared) != 1:
        raise BatchArtifactError("registration requires the selected pair's completed preparation")
    authority = state_store.authenticate_available_artifact(prepared[0])
    producer = plan.node_by_id.get(prepared[0].producer_node_id, {})
    if (authority.producer_node_kind != "paired_image_preparation"
            or producer.get("paired_reference") != selected.to_mapping()):
        raise BatchArtifactError("registration pair input is not an accepted paired-image preparation")
    catalogue, primary = reopen_project_catalogue_entry_artifact(prepared[0])
    prepared_primary, prepared_moving = _sample_catalogue_entries(catalogue, sample)
    if primary != prepared_primary or moving is None or prepared_moving is None:
        raise BatchArtifactError("registration pair preparation belongs to another primary image")
    _validate_moving_pair(sample=sample, primary=primary, moving=prepared_moving, authority=selected)
    if prepared_moving.project_entry_id != moving.project_entry_id:
        raise BatchArtifactError("registration pair preparation is no longer selected")
    return selected


def registration_options_for_route(route):
    """Project the accepted pair into executor options without changing config history."""
    route = authenticate_registration_execution_route(route)
    if route.disposition != "warpy_r2":
        raise BatchArtifactError("registration executor options require a paired route")
    moving = route.moving_authority
    return replace(route.sample.config.registration, mode="warpy",
                   moving_image=moving.source_path,
                   moving_image_id=(moving.source_image_id or route.sample.config.registration.moving_image_id
                                    or moving.reference_id),
                   moving_modality=moving.modality,
                   moving_image_metadata=moving.source_metadata)


def _sample_catalogue_entries(
    catalogue: BatchProjectCatalogue, sample: ResolvedBatchSample
) -> tuple[BatchProjectEntry, BatchProjectEntry | None]:
    sample_entries = tuple(item for item in catalogue.entries if item.sample_id == sample.sample_id)
    primary = tuple(item for item in sample_entries if item.image_role == "primary_mif")
    moving = tuple(item for item in sample_entries if item.image_role == "moving_reference")
    if len(primary) != 1 or len(moving) > 1:
        raise BatchArtifactError("registration catalogue sample entry inventory is invalid")
    return primary[0], None if not moving else moving[0]


def _validate_moving_pair(
    *,
    sample: ResolvedBatchSample,
    primary: BatchProjectEntry,
    moving: BatchProjectEntry | None,
    authority: object,
) -> None:
    if type(authority) is NoMovingReferenceAuthority:
        if moving is not None or primary.paired_entry_id is not None:
            raise BatchArtifactError(
                "registration catalogue contains an undeclared moving reference"
            )
        return
    if type(authority) is not PairedMovingReferenceAuthority or moving is None:
        raise BatchArtifactError(
            "registration moving reference is absent from the current catalogue"
        )
    if (
        primary.paired_entry_id != moving.project_entry_id
        or moving.paired_entry_id != primary.project_entry_id
        or moving.sample_id != sample.sample_id
        or moving.slide_id != sample.slide_id
        or moving.validation_domain_id != sample.validation_domain_id
        or moving.canonical_uri != authority.canonical_uri
        or moving.image_sha256 != authority.source_sha256
        or moving.reference_modality != authority.modality
        or moving.saved_state_attestation_sha256 is None
    ):
        raise BatchArtifactError(
            "registration requires the exact reciprocal current catalogue pair"
        )


def _validate_registration_context(
    context: BatchNodeExecutionContext,
    execution: BatchCurrentExecutionAuthority,
) -> tuple[BatchArtifactRef, BatchArtifactRef]:
    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("registration requires an exact execution context")
    try:
        current = validate_current_execution_authority(execution, expected_context=context)
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("registration current execution authority is invalid") from exc
    spec = context.adapter_spec
    requirements = {item.artifact_kind: item for item in spec.required_inputs}
    inputs = {item.artifact_kind: item for item in context.inputs}
    if (
        current.node_kind != _NODE_KIND
        or current.node_scope != "shared_project"
        or current.validation_domain_id is None
        or len(current.participant_ids) != 1
        or len(current.project_entry_ids) != 1
        or current.analysis_domain_bindings
        or len(current.configuration_bindings) != 1
        or spec.node_kind != _NODE_KIND
        or spec.dispatch_mode is not DispatchMode.SINGLE
        or spec.resource_kind != "qupath_image_write"
        or spec.declared_outputs != (_ARTIFACT_KIND,)
        or spec.output_identity_contracts != (batch_registration_result_identity_contract(),)
        or tuple(item.artifact_kind for item in spec.optional_inputs) != ("project_catalogue_entry",)
        or set(requirements) != {"analysis_frame", "tissue_acceptance_result"}
        or any(
            item.multiplicity is not ArtifactMultiplicity.SINGLE for item in requirements.values()
        )
        or len(inputs) != len(context.inputs)
        or not set(requirements).issubset(inputs)
        or set(inputs) - set(requirements) - {"project_catalogue_entry"}
        or current.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        or context.current_image_identity_sha256 is not None
        or context.script_sha256 is not None
        or any(
            value is None
            for value in (
                context.project_identity_sha256,
                context.project_authority_head_sha256,
                context.project_qpproj_snapshot_sha256,
                context.project_entry_projection_sha256,
            )
        )
    ):
        raise BatchArtifactError("registration execution context differs from its direct contract")
    return inputs["analysis_frame"], inputs["tissue_acceptance_result"]


def _warpy_registration_route(
    context: BatchNodeExecutionContext,
    *,
    manifest: ValidatedBatchManifest,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: BatchWorkspaceLease,
) -> RegistrationExecutionRoute:
    """Authenticate the exact current pair before any project-provider call."""

    try:
        execution = state_store.authenticate_current_execution_context(context)
        frame_ref, tissue_ref = _validate_registration_context(context, execution)
        frame_authority = state_store.authenticate_available_artifact(frame_ref)
        tissue_authority = state_store.authenticate_available_artifact(tissue_ref)
        authorities = validate_committed_artifact_authority_set(
            (frame_authority, tissue_authority),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
        head = state_store.load_head()
        catalogue = _current_catalogue(catalogue_store)
        sample = _manifest_sample(manifest, execution, head)
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        tissue = decode_committed_tissue_acceptance_result(tissue_authority)
        review_return = "settled_review_revision" in state_store._load_plan_snapshot(execution.active_plan_sha256).node_by_id[context.node_id]
        current_saved = state_store.current_project_entry_saved_state_sha256_by_id(
            expected_head_sha256=head.sha256,
            lease=workspace_lease,
        )
    except BatchArtifactError:
        raise
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise BatchArtifactError("Warpy registration current owner evidence is invalid") from exc
    by_kind = {item.artifact_ref.artifact_kind: item for item in authorities}
    if (
        set(by_kind) != {"analysis_frame", "tissue_acceptance_result"}
        or by_kind["analysis_frame"].artifact_ref != frame_ref
        or by_kind["tissue_acceptance_result"].artifact_ref != tissue_ref
        or any(item.state_head_sha256 != execution.state_head_sha256 for item in authorities)
        or frame_authority.producer_node_kind != "analysis_preflight"
        or tissue_authority.producer_node_kind != "tissue_acceptance"
        or any(item.execution_exchange_sha256 is not None for item in authorities)
        or (sample.config.registration.mode != "warpy" and not review_return)
        or head.project_materialization_state != "materialized"
    ):
        raise BatchArtifactError("Warpy registration inputs are not the exact current owners")
    project_path = manifest.manifest.project_path or manifest.manifest.create_project_path
    if project_path is None:
        raise BatchArtifactError("registration manifest lacks one project")
    primary, moving = _sample_catalogue_entries(catalogue, sample)
    moving_authority = _selected_moving_authority(context, execution, sample, moving, state_store)
    if type(moving_authority) is not PairedMovingReferenceAuthority or moving is None:
        raise BatchArtifactError("Warpy registration requires one retained moving reference")
    saved_states = MappingProxyType(
        {
            entry.project_entry_id: _sha(
                current_saved.get(entry.project_entry_id),
                "current entry saved state",
            )
            for entry in (primary, moving)
        }
    )
    _validate_moving_pair(
        sample=sample,
        primary=primary,
        moving=moving,
        authority=moving_authority,
    )
    registration = sample.config.registration
    binding = (
        ArtifactConfigurationBinding(
            sample.sample_id,
            sample.resolved_config_sha256,
        ),
    )
    # We retain the import and producing-operation snapshots as provenance.
    # Current context, entry projection and saved hierarchy below bind this read.
    tissue_identity = tissue.artifact_ref.owning_identity
    if (
        (not review_return and (
        registration.moving_image is None
        or registration.moving_image.resolve(strict=False) != moving_authority.source_path
        or (
            moving_authority.source_image_id is not None
            and registration.moving_image_id != moving_authority.source_image_id
        )
        or registration.moving_modality != moving_authority.modality
        or _plain(registration.moving_image_metadata) != _plain(moving_authority.source_metadata)))
        or execution.state_head_sha256 != head.sha256
        or execution.active_plan_sha256 != head.active_plan_sha256
        or execution.validation_domain_id != sample.validation_domain_id
        or execution.participant_ids != (sample.sample_id,)
        or execution.project_entry_ids != (primary.project_entry_id,)
        or execution.configuration_bindings != binding
        or frame_ref.configuration_bindings != binding
        or tissue_ref.configuration_bindings != binding
        or head.batch_id != manifest.manifest.batch_id
        or Path(head.project_path).resolve(strict=False) != project_path.resolve(strict=False)
        or head.project_identity_sha256 != catalogue.project_identity_sha256
        or head.project_identity_sha256 != context.project_identity_sha256
        or head.project_authority_head_sha256 != context.project_authority_head_sha256
        or head.project_qpproj_snapshot_sha256 != context.project_qpproj_snapshot_sha256
        or head.project_catalogue_sha256 != catalogue.catalogue_sha256
        or catalogue.batch_id != manifest.manifest.batch_id
        or catalogue.project_uri != project_path.resolve(strict=False).as_uri()
        or head.resolved_config_sha256_by_sample.get(sample.sample_id)
        != sample.resolved_config_sha256
        or head.sample_project_entry_id_by_sample.get(sample.sample_id) != primary.project_entry_id
        or head.sample_project_entry_projection_sha256_by_sample.get(sample.sample_id)
        != primary.entry_projection_sha256
        or context.project_entry_projection_sha256 != primary.entry_projection_sha256
        or primary.sample_id != sample.sample_id
        or primary.slide_id != sample.slide_id
        or primary.validation_domain_id != sample.validation_domain_id
        or primary.image_sha256 != frame.spatial_frame.canonical_image_sha256
        or primary.saved_state_attestation_sha256 is None
        or moving.saved_state_attestation_sha256 is None
        or frame_ref.scope.validation_domain_id != sample.validation_domain_id
        or frame_ref.scope.sample_id != sample.sample_id
        or frame_ref.scope.slide_id != sample.slide_id
        or frame_ref.scope.project_entry_id is not None
        or tissue.artifact_ref != tissue_ref
        or tissue.sample_id != sample.sample_id
        or tissue.slide_id != sample.slide_id
        or tissue.project_entry_id != primary.project_entry_id
        or tissue.canonical_image_sha256 != primary.image_sha256
        or tissue.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or (not review_return and tissue.saved_state_after_sha256 != saved_states[primary.project_entry_id])
        or tissue_identity["project_identity_sha256"] != head.project_identity_sha256
        or not _tissue_primary_matches(tissue_identity, primary, catalogue, catalogue_store)
    ):
        raise BatchArtifactError(
            "Warpy registration sample, config, tissue, state, or pair differs"
        )
    return RegistrationExecutionRoute._issue(
        disposition="warpy_r2",
        context=context,
        current_execution=execution,
        state_head=head,
        catalogue=catalogue,
        sample=sample,
        primary_entry=primary,
        moving_entry=moving,
        moving_authority=moving_authority,
        frame_ref=frame_ref,
        tissue_ref=tissue_ref,
        tissue_result=tissue,
        current_saved_state_sha256_by_id=saved_states,
        stage_result=None,
        execution_result=None,
    )


def route_registration_execution(
    context: BatchNodeExecutionContext,
    *,
    manifest: ValidatedBatchManifest,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: BatchWorkspaceLease,
) -> RegistrationExecutionRoute:
    """Route direct modes to accepted R1; admit only exact Warpy pairs to R2."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(manifest) is not ValidatedBatchManifest
        or type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
    ):
        raise BatchArtifactError("registration routing requires its exact composed owners")
    participant_id = context.authorization.dispatch_binding.participant_ids
    if len(participant_id) != 1:
        raise BatchArtifactError("registration routing requires one participant")
    samples = tuple(
        sample for sample in manifest.enabled_samples if sample.sample_id == participant_id[0]
    )
    if len(samples) != 1:
        raise BatchArtifactError("registration routing participant is absent from the manifest")
    mode = samples[0].config.registration.mode
    head = state_store.load_head()
    node = state_store._load_plan_snapshot(head.active_plan_sha256).node_by_id[context.node_id]
    revision = node.get("settled_review_revision")
    paired_return = revision is not None and (revision.get("paired_reference") is not None
        or samples[0].reference is not None)
    if mode in _DIRECT_MODES and not paired_return:
        stage_result, execution_result = build_direct_registration_result_execution_result(
            context,
            manifest=manifest,
            state_store=state_store,
            catalogue_store=catalogue_store,
            workspace_lease=workspace_lease,
        )
        return RegistrationExecutionRoute._issue(
            disposition="direct_r1",
            context=context,
            current_execution=None,
            state_head=None,
            catalogue=None,
            sample=None,
            primary_entry=None,
            moving_entry=None,
            moving_authority=None,
            frame_ref=None,
            tissue_ref=None,
            tissue_result=None,
            current_saved_state_sha256_by_id=None,
            stage_result=stage_result,
            execution_result=execution_result,
        )
    if mode != "warpy" and not paired_return:
        raise BatchArtifactError(
            "prompt registration must resolve before project execution routing"
        )
    return _warpy_registration_route(
        context,
        manifest=manifest,
        state_store=state_store,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
    )


def build_direct_registration_result_execution_result(
    context: BatchNodeExecutionContext,
    *,
    manifest: ValidatedBatchManifest,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: BatchWorkspaceLease,
) -> tuple[StageResult, BatchNodeExecutionResult]:
    """Publish an unchanged decision for none, skip, or side-by-side mode."""

    if (
        type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
    ):
        raise BatchArtifactError(
            "registration requires its exact composed state and catalogue owners"
        )
    try:
        execution = state_store.authenticate_current_execution_context(context)
        frame_ref, tissue_ref = _validate_registration_context(context, execution)
        frame_authority = state_store.authenticate_available_artifact(frame_ref)
        tissue_authority = state_store.authenticate_available_artifact(tissue_ref)
        authorities = validate_committed_artifact_authority_set(
            (frame_authority, tissue_authority),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
        head = state_store.load_head()
        _head_projection(head)
        catalogue = _current_catalogue(catalogue_store)
        sample = _manifest_sample(manifest, execution, head)
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        tissue = decode_committed_tissue_acceptance_result(tissue_authority)
        current_saved = state_store.current_project_entry_saved_state_sha256_by_id(
            expected_head_sha256=head.sha256,
            lease=workspace_lease,
        )
    except BatchArtifactError:
        raise
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise BatchArtifactError("registration current owner evidence is invalid") from exc
    by_kind = {item.artifact_ref.artifact_kind: item for item in authorities}
    if (
        set(by_kind) != {"analysis_frame", "tissue_acceptance_result"}
        or by_kind["analysis_frame"].artifact_ref != frame_ref
        or by_kind["tissue_acceptance_result"].artifact_ref != tissue_ref
        or any(item.state_head_sha256 != execution.state_head_sha256 for item in authorities)
        or frame_authority.producer_node_kind != "analysis_preflight"
        or tissue_authority.producer_node_kind != "tissue_acceptance"
        or any(item.execution_exchange_sha256 is not None for item in authorities)
    ):
        raise BatchArtifactError(
            "registration committed input authorities differ from current execution"
        )
    mode = sample.config.registration.mode
    review_return = "settled_review_revision" in state_store._load_plan_snapshot(execution.active_plan_sha256).node_by_id[context.node_id]
    if mode not in _DIRECT_MODES:
        raise BatchArtifactError(
            "prompt and Warpy registration require their later reviewed executor"
        )
    if head.project_materialization_state != "materialized":
        raise BatchArtifactError("registration project is not materialized")
    project_path = manifest.manifest.project_path or manifest.manifest.create_project_path
    if project_path is None:
        raise BatchArtifactError("registration manifest lacks one project")
    primary, moving = _sample_catalogue_entries(catalogue, sample)
    saved_states = MappingProxyType(
        {
            entry.project_entry_id: _sha(
                current_saved.get(entry.project_entry_id),
                "current entry saved state",
            )
            for entry in (primary, moving)
            if entry is not None
        }
    )
    moving_authority = _selected_moving_authority(context, execution, sample, moving, state_store)
    if mode == "side_by_side":
        if type(moving_authority) is not PairedMovingReferenceAuthority:
            raise BatchArtifactError(
                "side-by-side registration requires one retained moving reference"
            )
        registration = sample.config.registration
        if (
            registration.moving_image is None
            or registration.moving_image.resolve(strict=False) != moving_authority.source_path
            or (
                moving_authority.source_image_id is not None
                and registration.moving_image_id != moving_authority.source_image_id
            )
            or registration.moving_modality != moving_authority.modality
            or _plain(registration.moving_image_metadata)
            != _plain(moving_authority.source_metadata)
        ):
            raise BatchArtifactError(
                "side-by-side configuration differs from the retained moving reference"
            )
    _validate_moving_pair(
        sample=sample,
        primary=primary,
        moving=moving,
        authority=moving_authority,
    )
    binding = (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
    # We retain the import and producing-operation snapshots as provenance.
    # Current context, entry projection and saved hierarchy below bind this read.
    tissue_identity = tissue.artifact_ref.owning_identity
    # The configured output location names the analysis selection. The accepted
    # frame and selected catalogue entry below identify its immutable image data.
    expected_project_uri = project_path.resolve(strict=False).as_uri()
    if (
        execution.state_head_sha256 != head.sha256
        or execution.active_plan_sha256 != head.active_plan_sha256
        or execution.validation_domain_id != sample.validation_domain_id
        or execution.participant_ids != (sample.sample_id,)
        or execution.project_entry_ids != (primary.project_entry_id,)
        or execution.configuration_bindings != binding
        or frame_ref.configuration_bindings != binding
        or tissue_ref.configuration_bindings != binding
        or head.batch_id != manifest.manifest.batch_id
        or Path(head.project_path).resolve(strict=False) != project_path.resolve(strict=False)
        or head.project_identity_sha256 != catalogue.project_identity_sha256
        or head.project_identity_sha256 != context.project_identity_sha256
        or head.project_authority_head_sha256 != context.project_authority_head_sha256
        or head.project_qpproj_snapshot_sha256 != context.project_qpproj_snapshot_sha256
        or head.project_catalogue_sha256 != catalogue.catalogue_sha256
        or catalogue.batch_id != manifest.manifest.batch_id
        or catalogue.project_uri != expected_project_uri
        or head.resolved_config_sha256_by_sample.get(sample.sample_id)
        != sample.resolved_config_sha256
        or _SHA256.fullmatch(str(head.source_sha256_by_sample.get(sample.sample_id, ""))) is None
        or head.sample_project_entry_id_by_sample.get(sample.sample_id) != primary.project_entry_id
        or head.sample_project_entry_projection_sha256_by_sample.get(sample.sample_id)
        != primary.entry_projection_sha256
        or context.project_entry_projection_sha256 != primary.entry_projection_sha256
        or primary.sample_id != sample.sample_id
        or primary.slide_id != sample.slide_id
        or primary.validation_domain_id != sample.validation_domain_id
        or primary.image_sha256 != frame.spatial_frame.canonical_image_sha256
        or primary.saved_state_attestation_sha256 is None
        or frame_ref.scope.validation_domain_id != sample.validation_domain_id
        or frame_ref.scope.sample_id != sample.sample_id
        or frame_ref.scope.slide_id != sample.slide_id
        or frame_ref.scope.project_entry_id is not None
        or tissue.artifact_ref != tissue_ref
        or tissue.sample_id != sample.sample_id
        or tissue.slide_id != sample.slide_id
        or tissue.project_entry_id != primary.project_entry_id
        or tissue.canonical_image_sha256 != primary.image_sha256
        or tissue.analysis_frame_ref_sha256 != frame_ref.artifact_ref_sha256
        or tissue.artifact_ref.scope.validation_domain_id != sample.validation_domain_id
        or (not review_return and tissue.saved_state_after_sha256 != saved_states[primary.project_entry_id])
        or tissue_identity["project_identity_sha256"] != head.project_identity_sha256
        or not _tissue_primary_matches(tissue_identity, primary, catalogue, catalogue_store)
    ):
        raise BatchArtifactError(
            "registration sample, config, frame, tissue, state, or catalogue differs"
        )
    return _project_registration_result(
        context=context,
        sample=sample,
        execution=execution,
        head=head,
        catalogue=catalogue,
        primary=primary,
        moving=moving,
        frame_ref=frame_ref,
        tissue_ref=tissue_ref,
        tissue=tissue,
        moving_authority=moving_authority,
        mode=mode,
        state_store=state_store,
        catalogue_store=catalogue_store,
        current_saved_state_sha256_by_id=saved_states,
    )


def _project_registration_result(
    *,
    context: BatchNodeExecutionContext,
    sample: ResolvedBatchSample,
    execution: BatchCurrentExecutionAuthority,
    head: BatchStateHead,
    catalogue: BatchProjectCatalogue,
    primary: BatchProjectEntry,
    moving: BatchProjectEntry | None,
    frame_ref: BatchArtifactRef,
    tissue_ref: BatchArtifactRef,
    tissue: ValidatedTissueAcceptanceResultProjection,
    moving_authority: NoMovingReferenceAuthority | PairedMovingReferenceAuthority,
    mode: str,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    current_saved_state_sha256_by_id: Mapping[str, str],
    extra_identity: Mapping[str, object] | None = None,
) -> tuple[StageResult, BatchNodeExecutionResult]:
    """Project shared identity bytes after the mode owner authenticates scope."""

    head_projection = _head_projection(head)
    binding = (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
    side_identity: dict[str, object] = {}
    if mode in {"side_by_side", "warpy"}:
        assert moving is not None
        side_identity = {
            "moving_project_entry_id": moving.project_entry_id,
            "moving_project_entry_projection_sha256": (moving.entry_projection_sha256),
            "moving_image_sha256": moving.image_sha256,
            "moving_reference_modality": moving.reference_modality,
            "moving_saved_state_attestation_sha256": (
                current_saved_state_sha256_by_id[moving.project_entry_id]
            ),
        }
    identity: dict[str, object] = {
        "schema_version": _WIRE_VERSION,
        "mode": mode,
        "sample_id": sample.sample_id,
        "slide_id": sample.slide_id,
        "validation_domain_id": sample.validation_domain_id,
        "primary_project_entry_id": primary.project_entry_id,
        "primary_image_sha256": primary.image_sha256,
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "tissue_acceptance_result_ref_sha256": tissue_ref.artifact_ref_sha256,
        "resolved_config_sha256": sample.resolved_config_sha256,
        "active_plan_sha256": execution.active_plan_sha256,
        "producing_state_head_sha256": execution.state_head_sha256,
        "project_identity_sha256": head.project_identity_sha256,
        "project_authority_head_sha256": head.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": head.project_qpproj_snapshot_sha256,
        "project_catalogue_sha256": catalogue.catalogue_sha256,
        "primary_project_entry_projection_sha256": (primary.entry_projection_sha256),
        "primary_saved_state_attestation_sha256": (
            current_saved_state_sha256_by_id[primary.project_entry_id]
        ),
        "tissue_generation_id": tissue.generation_id,
        "tissue_generation_content_sha256": tissue.generation_content_sha256,
        "tissue_section_inventory_sha256": tissue.section_inventory_sha256,
        "tissue_section_ids_sha256": _digest(list(tissue.section_ids)),
        "tissue_roi_ids_sha256": _digest(list(tissue.roi_ids)),
        "tissue_path_object_ids_sha256": _digest(list(tissue.path_object_ids)),
        "retained_moving_reference_descriptor_sha256": (moving_authority.descriptor_sha256),
        "saved_state_disposition": "authenticated_unchanged",
        **side_identity,
        **({} if extra_identity is None else extra_identity),
    }
    identity["registration_decision_sha256"] = _digest(_decision_material(identity))
    contract = batch_registration_result_identity_contract()
    artifact_id = (
        "registration-"
        + _digest(
            {
                "decision": identity["registration_decision_sha256"],
                "producing_state": identity["producing_state_head_sha256"],
                "attempt_authorization": context.authorization.authorization_sha256,
            }
        )[:48]
    )
    ref = BatchArtifactRef(
        artifact_id=artifact_id,
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
            sample.validation_domain_id,
            sample.sample_id,
            sample.slide_id,
            primary.project_entry_id,
            (),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=binding,
    )
    validate_registration_result_artifact_ref(ref)
    final_head = state_store.load_head()
    final_catalogue = _current_catalogue(catalogue_store)
    if _head_projection(final_head) != head_projection or final_catalogue != catalogue:
        raise BatchArtifactError(
            "registration state or project catalogue changed during projection"
        )
    stage_result = StageResult.completed(
        stage=_NODE_KIND,
        slide_id=sample.slide_id,
        generation_id=tissue.generation_id,
    )
    return stage_result, BatchNodeExecutionResult(
        authorization_sha256=context.authorization.authorization_sha256,
        outputs=(ref,),
    )


def build_warpy_fallback_registration_result_execution_result(
    route: RegistrationExecutionRoute,
    *,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
) -> tuple[StageResult, BatchNodeExecutionResult]:
    """Project only an explicitly offered, authenticated non-Warpy choice."""

    route = authenticate_registration_execution_route(route)
    if (
        route.disposition != "warpy_r2"
        or type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
    ):
        raise BatchArtifactError("Warpy fallback requires its exact current route and owners")
    sample, tissue = route.sample, route.tissue_result
    assert sample is not None and tissue is not None
    authorization = route.context.authorization
    try:
        response_sha256 = _sha(authorization.interaction_response_sha256, "fallback response")
        predecessor_sha256 = _sha(
            authorization.interaction_predecessor_terminal_record_sha256,
            "fallback predecessor",
        )
        if authorization.retry_ordinal <= 0:
            raise BatchArtifactError("Warpy fallback requires an authorized interaction retry")
        execution = state_store.authenticate_current_execution_context(route.context)
        head = state_store.load_head()
        catalogue = _current_catalogue(catalogue_store)
        if (
            execution != route.current_execution
            or _head_projection(head) != _head_projection(route.state_head)
            or catalogue != route.catalogue
        ):
            raise BatchArtifactError("Warpy fallback route is no longer current")
        response = state_store.load_validated_interaction_response(authorization)
        resumed = response.get("response") if isinstance(response, Mapping) else None
        action = response.get("action") if isinstance(response, Mapping) else None
        if (
            action not in ("side_by_side", "skip_moving_reference")
            or not isinstance(resumed, Mapping)
            or resumed.get("action") != action
            or not isinstance(resumed.get("response"), Mapping)
            or resumed["response"]
        ):
            raise BatchArtifactError("Warpy fallback lacks its exact empty-payload response")
        predecessor = state_store.load_attempt(predecessor_sha256)
        paused = predecessor.stage_result
        context = paused.resume_context
        if isinstance(context, Mapping):
            context = context.get("service_context", context)
        schemas = {
            InteractionReason.REGISTRATION_REVIEW_REQUIRED: "registration-review-interaction/v1",
            InteractionReason.WARPY_REGISTRATION_REQUIRED: "warpy-registration-interaction/v1",
        }
        if (
            predecessor.node_id != authorization.node_id
            or type(paused) is not StageResult
            or paused.status is not StageStatus.NEEDS_INTERACTION
            or paused.stage != "registration"
            or paused.slide_id != sample.slide_id
            or paused.interaction_reason not in schemas
            or action not in paused.allowed_actions
            or not isinstance(context, Mapping)
            or context.get("schema_version") != schemas[paused.interaction_reason]
            or (
                paused.generation_id != tissue.generation_id
                and not (
                    paused.interaction_reason is InteractionReason.WARPY_REGISTRATION_REQUIRED
                    and paused.generation_id is None
                )
            )
        ):
            raise BatchArtifactError("Warpy fallback has a foreign or unoffered pause predecessor")
        moving_authority = route.moving_authority
    except BatchArtifactError:
        raise
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise BatchArtifactError("Warpy fallback scope or response is invalid") from exc
    return _project_registration_result(
        context=route.context,
        sample=sample,
        execution=execution,
        head=head,
        catalogue=catalogue,
        primary=route.primary_entry,
        moving=route.moving_entry,
        frame_ref=route.frame_ref,
        tissue_ref=route.tissue_ref,
        tissue=tissue,
        moving_authority=moving_authority,
        mode="side_by_side" if action == "side_by_side" else "skip",
        state_store=state_store,
        catalogue_store=catalogue_store,
        current_saved_state_sha256_by_id=route.current_saved_state_sha256_by_id,
        extra_identity={"fallback_response_sha256": response_sha256},
    )


def build_warpy_registration_result_execution_result(
    route: RegistrationExecutionRoute,
    stage_result: StageResult,
    *,
    snapshot: CurrentRegistrationProjectSnapshot,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
) -> tuple[StageResult, BatchNodeExecutionResult]:
    """Project the exact accepted review without granting terminal authority."""

    # The session imports routing, so reuse its pair validator only after load.
    from .batch_registration_session import _validate_current_snapshot

    route = authenticate_registration_execution_route(route)
    if (
        route.disposition != "warpy_r2"
        or type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
        or type(snapshot) is not CurrentRegistrationProjectSnapshot
        or snapshot.transform.presence != "present"
        or type(stage_result) is not StageResult
    ):
        raise BatchArtifactError(
            "accepted Warpy projection requires exact current owners and transform"
        )
    sample, tissue = route.sample, route.tissue_result
    assert sample is not None and tissue is not None
    outputs = stage_result.outputs
    if (
        stage_result.status is not StageStatus.COMPLETED
        or stage_result.stage != "registration"
        or stage_result.slide_id != sample.slide_id
        or stage_result.generation_id != tissue.generation_id
        or not isinstance(outputs, Mapping)
        or outputs.get("mode") != "warpy"
        or set(outputs)
        != {
            "mode",
            "registration",
            "fixed_reference",
            "moving_reference",
            "transfer_qc_records",
            "transfer_attempts",
            "transfer_commit_outcomes",
            "transfer_commit_evidence",
            "transfer_cleanup_evidence",
        }
        or any(
            outputs[field] not in ([], ())
            for field in (
                "transfer_qc_records",
                "transfer_attempts",
                "transfer_commit_outcomes",
            )
        )
        or outputs["transfer_commit_evidence"] is not None
        or outputs["transfer_cleanup_evidence"] is not None
    ):
        raise BatchArtifactError(
            "accepted Warpy result must complete registration without transfer"
        )
    try:
        execution = state_store.authenticate_current_execution_context(route.context)
        head = state_store.load_head()
        catalogue = _current_catalogue(catalogue_store)
        if (
            execution != route.current_execution
            or _head_projection(head) != _head_projection(route.state_head)
            or catalogue != route.catalogue
        ):
            raise BatchArtifactError("accepted Warpy route is no longer current")
        _validate_current_snapshot(route, snapshot)
        accepted = reopen_accepted_warpy_record(state_store, route.context.authorization)
        if accepted != RegistrationRecord.from_mapping(outputs["registration"]):
            raise BatchArtifactError("executor registration differs from the exact accepted review")
        record_sections = tuple(
            sorted(
                zip(
                    accepted.tissue_section_ids,
                    accepted.tissue_roi_ids,
                    accepted.tissue_path_object_ids,
                    strict=True,
                )
            )
        )
        retained_sections = tuple(
            sorted(
                (row["section_id"], row["roi_id"], row["path_object_id"])
                for row in tissue.section_inventory
            )
        )
        if record_sections != retained_sections:
            raise BatchArtifactError(
                "accepted Warpy tissue associations differ from the retained sections"
            )
        fixed, moving = snapshot.fixed_entry, snapshot.moving_entry
        registration = registration_options_for_route(route)
        for observation, calibration in (
            (fixed, accepted.fixed_physical_calibration),
            (moving, accepted.moving_physical_calibration),
        ):
            if (
                calibration.x_um_per_px.hex() != observation.x_um_per_px_hex
                or calibration.y_um_per_px.hex() != observation.y_um_per_px_hex
            ):
                raise BatchArtifactError("accepted Warpy calibration differs from current entries")
        if accepted.moving_modality != registration.moving_modality or _plain(
            accepted.moving_image_metadata
        ) != _plain(registration.moving_image_metadata):
            raise BatchArtifactError(
                "accepted Warpy moving metadata differs from explicit configuration"
            )
        scope = RegistrationImportScope(
            fixed_project_entry_id=fixed.project_entry_id,
            moving_project_entry_id=moving.project_entry_id,
            fixed_image_id=tissue.canonical_image_id,
            moving_image_id=registration.moving_image_id,
            fixed_image_sha256=fixed.source_image_sha256,
            moving_image_sha256=moving.source_image_sha256,
            fixed_image_path=Path(url2pathname(urlsplit(fixed.source_uri).path)),
            moving_image_path=Path(url2pathname(urlsplit(moving.source_uri).path)),
            transform_file=Path(url2pathname(urlsplit(snapshot.transform.transform_uri).path)),
            transform_file_sha256=snapshot.transform.content_sha256,
            tissue_generation_id=tissue.generation_id,
            tissue_section_ids=accepted.tissue_section_ids,
            tissue_roi_ids=accepted.tissue_roi_ids,
            tissue_path_object_ids=accepted.tissue_path_object_ids,
        )
        predecessor = None
        predecessor_sha256 = None
        if accepted.revision > 1:
            terminal = state_store.load_attempt(
                route.context.authorization.interaction_predecessor_terminal_record_sha256
            )
            review_context = terminal.stage_result.resume_context
            if isinstance(review_context, Mapping):
                review_context = review_context.get("service_context", review_context)
            if not isinstance(review_context, Mapping):
                raise BatchArtifactError("accepted Warpy revision lacks saved lineage")
            predecessor = RegistrationRecord.from_mapping(
                review_context.get("predecessor_registration")
            )
            predecessor_sha256 = review_context.get("predecessor_registration_sha256")
        warpy_identity = project_registration_result_identity(
            accepted,
            expected_scope=scope,
            predecessor=predecessor,
            predecessor_sha256=predecessor_sha256,
        )
        moving_authority = route.moving_authority
    except BatchArtifactError:
        raise
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise BatchArtifactError("accepted Warpy result scope or review is invalid") from exc
    return _project_registration_result(
        context=route.context,
        sample=sample,
        execution=execution,
        head=head,
        catalogue=catalogue,
        primary=route.primary_entry,
        moving=route.moving_entry,
        frame_ref=route.frame_ref,
        tissue_ref=route.tissue_ref,
        tissue=tissue,
        moving_authority=moving_authority,
        mode="warpy",
        state_store=state_store,
        current_saved_state_sha256_by_id=route.current_saved_state_sha256_by_id,
        catalogue_store=catalogue_store,
        extra_identity={
            "accepted_warpy_identity": warpy_identity,
            "accepted_warpy_record_sha256": registration_record_sha256(accepted),
            "accepted_registration_review_sha256": route.context.authorization.interaction_response_sha256,
        },
    )


__all__ = [
    "BATCH_REGISTRATION_RESULT_OPTIONAL_FIELDS",
    "BATCH_REGISTRATION_RESULT_REQUIRED_FIELDS",
    "RegistrationExecutionRoute",
    "ValidatedBatchRegistrationResultProjection",
    "batch_registration_result_identity_contract",
    "build_direct_registration_result_execution_result",
    "build_warpy_fallback_registration_result_execution_result",
    "build_warpy_registration_result_execution_result",
    "decode_committed_registration_result",
    "authenticate_registration_execution_route",
    "route_registration_execution",
    "reopen_accepted_warpy_record",
    "validate_registration_result_artifact_ref",
]
