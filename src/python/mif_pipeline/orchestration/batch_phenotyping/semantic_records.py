"""In-memory normalization of committed phenotype applications for semantics.

The `ValidatedPhenotypeApplicationProjection` record carries `source_artifact_ref`,
`source_artifact_kind`, `phenotype_mode`, `sample_id`, `project_entry_id` and its remaining
declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)

from mif_pipeline.datasets.references import freeze_component_reference

from ._common import BatchPhenotypingError, digest, sha256, text
from .application_codec import (
    decode_committed_cluster_label_import_result,
    decode_committed_phenotype_hierarchy_attestation,
)
from .marker_application_records import marker_section_application_identity_contract
from .supervised_records import supervised_phenotype_result_identity_contract

_PROJECTION_ISSUER = object()
_MARKER_IDENTITY_FIELDS = (
    "sample_id",
    "project_entry_id",
    "slide_id",
    "canonical_image_id",
    "canonical_image_sha256",
    "section_id",
    "tissue_generation_id",
    "segmentation_population_id",
    "measurement_generation_id",
    "validation_domain_id",
    "phenotype_generation_id",
    "cell_ids_sha256",
    "cell_to_domain_membership_sha256",
    "phenotype_view_artifact_sha256",
    "phenotype_view_lineage_sha256",
    "phenotype_view_inventory_sha256",
    "saved_state_before_sha256",
    "saved_state_after_sha256",
    "saved_state_disposition",
    "execution_exchange_sha256",
    "node_id",
    "attempt_id",
    "attempt_authorization_sha256",
    "request_codec_id",
    "request_codec_version",
)
_SUPERVISED_TEXT_FIELDS = (
    "sample_id",
    "project_entry_id",
    "slide_id",
    "section_id",
    "tissue_generation_id",
    "reviewer_id",
    "phenotype_generation_id",
    "node_id",
    "attempt_id",
    "request_codec_id",
    "request_codec_version",
)


# ==============================================================================
#                                Semantic records
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPhenotypeApplicationProjection:
    """Issuer-only common committed facts for later semantic composition."""

    source_artifact_ref: BatchArtifactRef
    component_descriptor: Mapping[str, object]
    source_artifact_kind: str
    phenotype_mode: str
    sample_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    phenotype_generation_id: str
    phenotype_view_artifact_sha256: str
    phenotype_view_lineage_sha256: str
    phenotype_view_inventory_sha256: str
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    saved_state_disposition: str
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("phenotype application projection is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("phenotype application projection is nonserializable")


def _projection_material(
    projection: ValidatedPhenotypeApplicationProjection,
) -> dict[str, object]:
    return {
        "projection_object_identity": id(projection),
        "source_artifact_ref_sha256": projection.source_artifact_ref.artifact_ref_sha256,
        "component_descriptor": dict(projection.component_descriptor),
        "source_artifact_kind": projection.source_artifact_kind,
        "phenotype_mode": projection.phenotype_mode,
        "sample_id": projection.sample_id,
        "project_entry_id": projection.project_entry_id,
        "slide_id": projection.slide_id,
        "section_id": projection.section_id,
        "tissue_generation_id": projection.tissue_generation_id,
        "analysis_domain_bindings": [
            item.to_mapping() for item in projection.analysis_domain_bindings
        ],
        "cell_ids_sha256": projection.cell_ids_sha256,
        "cell_to_domain_membership_sha256": projection.cell_to_domain_membership_sha256,
        "phenotype_generation_id": projection.phenotype_generation_id,
        "phenotype_view_artifact_sha256": projection.phenotype_view_artifact_sha256,
        "phenotype_view_lineage_sha256": projection.phenotype_view_lineage_sha256,
        "phenotype_view_inventory_sha256": projection.phenotype_view_inventory_sha256,
        "saved_state_before_sha256": projection.saved_state_before_sha256,
        "saved_state_after_sha256": projection.saved_state_after_sha256,
        "saved_state_disposition": projection.saved_state_disposition,
        "state_head_sha256": projection.state_head_sha256,
        "active_plan_sha256": projection.active_plan_sha256,
        "terminal_record_sha256": projection.terminal_record_sha256,
    }


def _issue_projection(**values: object) -> ValidatedPhenotypeApplicationProjection:
    result = object.__new__(ValidatedPhenotypeApplicationProjection)
    values["component_descriptor"] = freeze_component_reference(values["component_descriptor"])
    for name in ValidatedPhenotypeApplicationProjection.__dataclass_fields__:
        object.__setattr__(result, name, values.get(name))
    object.__setattr__(result, "_issuer", _PROJECTION_ISSUER)
    object.__setattr__(result, "_seal_sha256", digest(_projection_material(result)))
    return result


def validate_phenotype_application_projection(
    projection: ValidatedPhenotypeApplicationProjection,
) -> ValidatedPhenotypeApplicationProjection:
    """Reject copied or reconstructed projections before semantic composition."""

    if (
        type(projection) is not ValidatedPhenotypeApplicationProjection
        or projection._issuer is not _PROJECTION_ISSUER
        or projection._seal_sha256 != digest(_projection_material(projection))
    ):
        raise BatchPhenotypingError("phenotype application projection is invalid")
    return projection


def _committed_marker_projection(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedPhenotypeApplicationProjection:
    try:
        (validated,) = validate_committed_artifact_authority_set((authority,))
        artifact = validate_artifact_ref(validated.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker phenotype source is not current") from exc
    contract = marker_section_application_identity_contract()
    identity = artifact.owning_identity
    try:
        owner_differs = (
            validated.producer_node_kind != "marker_rule_application"
            or artifact.artifact_kind != "marker_phenotype_result"
            or (artifact.schema_name, artifact.schema_version)
            != ("mif-batch-marker-gate-section-application", 1)
            or artifact.identity_contract != contract
            or artifact.owning_identity_contract_sha256 != contract.contract_sha256
            or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
            or artifact.locator is not None
            or artifact.sha256 is not None
            or artifact.byte_count is not None
            or validated.execution_exchange_sha256 != identity["execution_exchange_sha256"]
            or artifact.scope.sample_id != identity["sample_id"]
            or artifact.scope.slide_id != identity["slide_id"]
            or artifact.scope.project_entry_id != identity["project_entry_id"]
            or artifact.scope.validation_domain_id != identity["validation_domain_id"]
            or tuple(item.sample_id for item in artifact.configuration_bindings)
            != (identity["sample_id"],)
            or artifact.producer_node_id != identity["node_id"]
            or artifact.producer_attempt_id != identity["attempt_id"]
            or artifact.producer_attempt_authorization_sha256
            != identity["attempt_authorization_sha256"]
        )
    except KeyError as exc:
        raise BatchPhenotypingError("marker phenotype result identity is invalid") from exc
    if owner_differs:
        raise BatchPhenotypingError("marker phenotype result owner is invalid")
    try:
        for field in _MARKER_IDENTITY_FIELDS:
            value = identity[field]
            if field.endswith("_sha256"):
                sha256(value, field)
            elif field not in {"saved_state_disposition"}:
                text(value, field)
        if identity["saved_state_disposition"] not in {
            "authenticated_unchanged",
            "authenticated_changed",
        }:
            raise BatchPhenotypingError("marker saved-state disposition is invalid")
        if (identity["saved_state_before_sha256"] == identity["saved_state_after_sha256"]) != (
            identity["saved_state_disposition"] == "authenticated_unchanged"
        ):
            raise BatchPhenotypingError("marker saved-state facts disagree")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker phenotype result identity is invalid") from exc
    return _issue_projection(
        source_artifact_ref=artifact,
        component_descriptor=identity["component_descriptor"],
        source_artifact_kind=artifact.artifact_kind,
        phenotype_mode="marker_rules",
        sample_id=identity["sample_id"],
        project_entry_id=identity["project_entry_id"],
        slide_id=identity["slide_id"],
        section_id=identity["section_id"],
        tissue_generation_id=identity["tissue_generation_id"],
        analysis_domain_bindings=artifact.scope.analysis_domain_bindings,
        cell_ids_sha256=identity["cell_ids_sha256"],
        cell_to_domain_membership_sha256=identity["cell_to_domain_membership_sha256"],
        phenotype_generation_id=identity["phenotype_generation_id"],
        phenotype_view_artifact_sha256=identity["phenotype_view_artifact_sha256"],
        phenotype_view_lineage_sha256=identity["phenotype_view_lineage_sha256"],
        phenotype_view_inventory_sha256=identity["phenotype_view_inventory_sha256"],
        saved_state_before_sha256=identity["saved_state_before_sha256"],
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        saved_state_disposition=identity["saved_state_disposition"],
        state_head_sha256=validated.state_head_sha256,
        active_plan_sha256=validated.active_plan_sha256,
        terminal_record_sha256=validated.terminal_record_sha256,
    )


def _committed_supervised_projection(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedPhenotypeApplicationProjection:
    """Decode one exact section result into the common projection."""

    try:
        (validated,) = validate_committed_artifact_authority_set((authority,))
        artifact = validate_artifact_ref(validated.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("supervised phenotype source is not current") from exc
    contract = supervised_phenotype_result_identity_contract()
    identity = artifact.owning_identity
    try:
        owner_differs = (
            validated.producer_node_kind != "supervised_phenotype_inference"
            or artifact.artifact_kind != "supervised_phenotype_result"
            or (artifact.schema_name, artifact.schema_version)
            != ("mif-batch-supervised-native-prediction", 1)
            or artifact.identity_contract != contract
            or artifact.owning_identity_contract_sha256 != contract.contract_sha256
            or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
            or artifact.locator is None
            or artifact.sha256 != identity["prediction_sha256"]
            or artifact.byte_count != identity["prediction_byte_count"]
            or artifact.producer_version != "mif-pipeline-batch-supervised-deployment/v1"
            or validated.execution_exchange_sha256 != identity["execution_exchange_sha256"]
            or artifact.scope.sample_id != identity["sample_id"]
            or artifact.scope.slide_id != identity["slide_id"]
            or artifact.scope.project_entry_id != identity["project_entry_id"]
            or tuple(item.sample_id for item in artifact.configuration_bindings)
            != (identity["sample_id"],)
            or artifact.producer_node_id != identity["node_id"]
            or artifact.producer_attempt_id != identity["attempt_id"]
            or artifact.producer_attempt_authorization_sha256
            != identity["attempt_authorization_sha256"]
        )
    except KeyError as exc:
        raise BatchPhenotypingError("supervised phenotype result identity is invalid") from exc
    if owner_differs:
        raise BatchPhenotypingError("supervised phenotype result owner is invalid")
    try:
        for field in _SUPERVISED_TEXT_FIELDS:
            text(identity[field], field)
        for field in contract.required_fields:
            if field.endswith("_sha256") and field != "source_ref_set_sha256" and not field.startswith("component_descriptor."):
                sha256(identity[field], field)
        source_refs = tuple(identity["source_ref_sha256s"])
        if (
            not source_refs
            or source_refs != tuple(sorted(set(source_refs)))
            or any(sha256(item, "source_ref_sha256s") != item for item in source_refs)
            or identity["source_ref_set_sha256"]
            != digest({"artifact_ref_sha256s": list(source_refs)})
            or type(identity["prediction_byte_count"]) is not int
            or identity["prediction_byte_count"] <= 0
            or type(identity["cell_count"]) is not int
            or identity["cell_count"] <= 0
            or isinstance(identity["confidence_threshold"], bool)
            or not isinstance(identity["confidence_threshold"], (int, float))
            or not math.isfinite(float(identity["confidence_threshold"]))
            or not 0.0 <= float(identity["confidence_threshold"]) <= 1.0
            or identity["saved_state_disposition"] != "authenticated_changed"
            or identity["saved_state_before_sha256"] == identity["saved_state_after_sha256"]
            or not artifact.scope.analysis_domain_bindings
            or any(
                (item.sample_id, item.slide_id, item.project_entry_id)
                != (
                    identity["sample_id"],
                    identity["slide_id"],
                    identity["project_entry_id"],
                )
                for item in artifact.scope.analysis_domain_bindings
            )
        ):
            raise BatchPhenotypingError("supervised phenotype result identity is invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("supervised phenotype result identity is invalid") from exc
    return _issue_projection(
        source_artifact_ref=artifact,
        component_descriptor=identity["component_descriptor"],
        source_artifact_kind="supervised_phenotype_result",
        phenotype_mode="supervised_classifier",
        sample_id=identity["sample_id"],
        project_entry_id=identity["project_entry_id"],
        slide_id=identity["slide_id"],
        section_id=identity["section_id"],
        tissue_generation_id=identity["tissue_generation_id"],
        analysis_domain_bindings=artifact.scope.analysis_domain_bindings,
        cell_ids_sha256=identity["cell_ids_sha256"],
        cell_to_domain_membership_sha256=(identity["cell_to_domain_membership_sha256"]),
        phenotype_generation_id=identity["phenotype_generation_id"],
        phenotype_view_artifact_sha256=(identity["phenotype_view_artifact_sha256"]),
        phenotype_view_lineage_sha256=(identity["phenotype_view_lineage_sha256"]),
        phenotype_view_inventory_sha256=(identity["phenotype_view_inventory_sha256"]),
        saved_state_before_sha256=identity["saved_state_before_sha256"],
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        saved_state_disposition=identity["saved_state_disposition"],
        state_head_sha256=validated.state_head_sha256,
        active_plan_sha256=validated.active_plan_sha256,
        terminal_record_sha256=validated.terminal_record_sha256,
    )


def _committed_cluster_projection(
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> ValidatedPhenotypeApplicationProjection:
    try:
        validated = validate_committed_artifact_authority_set(authorities)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("cluster phenotype sources are not current") from exc
    by_kind = {item.artifact_ref.artifact_kind: item for item in validated}
    if len(validated) != 2 or set(by_kind) != {
        "cluster_label_import_result",
        "phenotype_view_attestation",
    }:
        raise BatchPhenotypingError("cluster phenotype source kinds differ")
    try:
        imported = decode_committed_cluster_label_import_result(
            by_kind["cluster_label_import_result"]
        )
        attested = decode_committed_phenotype_hierarchy_attestation(
            by_kind["phenotype_view_attestation"]
        )
    except (BatchArtifactError, BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("cluster phenotype owner decoding failed") from exc
    source = imported.artifact_ref
    attestation = attested.attestation
    identity = imported.identity
    if (
        imported.state_head_sha256 != attested.state_head_sha256
        or imported.active_plan_sha256 != attested.active_plan_sha256
        or source.scope != attested.artifact_ref.scope
        or source.configuration_bindings != attested.artifact_ref.configuration_bindings
        or attestation.cluster_label_import_result_ref_sha256 != source.artifact_ref_sha256
        or attestation.sample_id != identity["sample_id"]
        or attestation.project_entry_id != identity["project_entry_id"]
        or attestation.slide_id != identity["slide_id"]
        or attestation.section_id != identity["section_id"]
        or attestation.canonical_image_sha256 != identity["canonical_image_sha256"]
        or attestation.tissue_generation_id != identity["tissue_generation_id"]
        or attestation.segmentation_population_id != identity["segmentation_population_id"]
        or attestation.phenotype_generation_id != identity["phenotype_generation_id"]
        or attestation.cell_ids_sha256 != identity["cell_ids_sha256"]
        or attestation.cell_to_domain_membership_sha256
        != identity["cell_to_domain_membership_sha256"]
        or attestation.phenotype_view_artifact_sha256 != identity["phenotype_view_artifact_sha256"]
        or attestation.phenotype_view_lineage_sha256 != identity["phenotype_view_lineage_sha256"]
        or attestation.saved_state_after_sha256 != identity["saved_state_after_sha256"]
    ):
        raise BatchPhenotypingError("cluster phenotype sources differ")
    return _issue_projection(
        source_artifact_ref=source,
        component_descriptor=identity["component_descriptor"],
        source_artifact_kind=source.artifact_kind,
        phenotype_mode="unsupervised_clustering",
        sample_id=identity["sample_id"],
        project_entry_id=identity["project_entry_id"],
        slide_id=identity["slide_id"],
        section_id=identity["section_id"],
        tissue_generation_id=identity["tissue_generation_id"],
        analysis_domain_bindings=source.scope.analysis_domain_bindings,
        cell_ids_sha256=identity["cell_ids_sha256"],
        cell_to_domain_membership_sha256=identity["cell_to_domain_membership_sha256"],
        phenotype_generation_id=identity["phenotype_generation_id"],
        phenotype_view_artifact_sha256=identity["phenotype_view_artifact_sha256"],
        phenotype_view_lineage_sha256=identity["phenotype_view_lineage_sha256"],
        phenotype_view_inventory_sha256=attestation.phenotype_view_inventory_sha256,
        saved_state_before_sha256=identity["saved_state_before_sha256"],
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        saved_state_disposition=identity["saved_state_disposition"],
        state_head_sha256=imported.state_head_sha256,
        active_plan_sha256=imported.active_plan_sha256,
        terminal_record_sha256=imported.terminal_record_sha256,
    )


def normalize_committed_phenotype_application(
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> ValidatedPhenotypeApplicationProjection:
    """Normalize one direct result or matching cluster-import/attestation pair."""

    if type(authorities) is not tuple:
        raise BatchPhenotypingError("phenotype application sources must be an exact tuple")
    if len(authorities) == 1:
        authority = authorities[0]
        if type(authority) is not BatchCommittedArtifactAuthority:
            raise BatchPhenotypingError("phenotype application source requires committed authority")
        kind = authority.artifact_ref.artifact_kind
        if kind == "marker_phenotype_result":
            return _committed_marker_projection(authority)
        if kind == "supervised_phenotype_result":
            return _committed_supervised_projection(authority)
        raise BatchPhenotypingError("single phenotype source kind is unsupported")
    return _committed_cluster_projection(authorities)


__all__ = [
    "ValidatedPhenotypeApplicationProjection",
    "normalize_committed_phenotype_application",
    "validate_phenotype_application_projection",
]
